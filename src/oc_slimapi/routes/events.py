from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..sse.hub import STOP, SubscriberCapacityError, sse_frame
from ..sse.replay_log import (
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayResync,
)
from ..sse.replay_wire import classify_reconnect, frame_with_id, meta_v4_extension
from ..sse_observability import sse_close, sse_open

router = APIRouter(prefix="/slimapi", tags=["events"])

#: §7.3 frozen retirement error body for ``?tokens=1`` on the v4 face.
TOKENS_STREAM_RETIRED_IN_V4 = {
    "code": "tokens_stream_retired_in_v4",
    "hint": "token 流请使用 /slimapi/sessions/{sid}/stream",
}


class _SendStartRollbackResponse(StreamingResponse):
    """StreamingResponse that rolls back route admission when the ASGI
    response never starts (BUG-002 / FI-001).

    The route admits its subscriber at handler time and detaches it in the
    body generator's ``finally``. Starlette sends ``http.response.start``
    BEFORE iterating the generator (``StreamingResponse.stream_response``),
    so a ``send`` failure at response-start leaves the generator
    never-started — its ``finally`` never runs and the admission slot leaks
    (one-shot leak; N failures exhaust the cap and later connections 503).
    This subclass wraps the ASGI send path: if an exception escapes before
    the first ``http.response.body`` message was handed to ASGI, the
    route's rollback hook runs the same idempotent detach routine the
    generator ``finally`` uses, then the exception re-raises unchanged.

    The guard is ``body_started`` (not "response started"): once any body
    chunk was submitted the generator HAS started (it yielded that chunk),
    so its own ``finally`` owns cleanup on that path — skipping the
    rollback there avoids a redundant (albeit membership-guarded) detach.
    """

    def __init__(self, content, *, on_start_failure, **kwargs):
        super().__init__(content, **kwargs)
        self._on_start_failure = on_start_failure

    async def __call__(self, scope, receive, send):
        body_started = False

        async def _tracked_send(message):
            nonlocal body_started
            if message["type"] == "http.response.body":
                body_started = True
            await send(message)

        try:
            await super().__call__(scope, receive, _tracked_send)
        except BaseException:
            if not body_started:
                rollback = self._on_start_failure
                self._on_start_failure = None
                if rollback is not None:
                    rollback()
            raise


@router.get("/events")
async def events(request: Request, tokens: str | None = None):
    """Process-wide curated SSE stream.

    No directory / sessionId / stream parameters: the sidecar holds one
    /global/event subscription and every connected client sees every session
    across every directory. Clients filter locally.

    Admission runs in ``HubRegistry.subscribe`` under T3 caps (contract §6);
    exceeding per-directory or total caps raises ``SubscriberCapacityError``
    which we map to a 503 with ``Retry-After`` so the client backs off and
    retries instead of burning an upstream connection.

    ``?tokens=1`` is RETIRED on the v4-only face — flat 400
    ``tokens_stream_retired_in_v4`` (§7.3) before the stream opens; only
    the literal value ``"1"`` reaches the retirement check (anything else
    → 400 ``invalid_tokens``). A first frame ``slimapi.meta`` —
    ``{"subscriberId": <id>, "tokens": false}`` — always precedes ANY
    business frame, heartbeat, or replay (the ``tokens`` key is the
    frozen meta field set; it is always ``false`` now that ``tokens=1``
    is retired — the v3-only L2-A token attach was removed with the V2b
    src teardown). The meta frame's ``subscriberId`` is the sole id
    channel (the ``X-Slimapi-Subscriber-ID`` header was removed at
    3.0.0 — never produced).

    v4 (B3b-2, v4-contract §7 / design-v4-sse-replay):

    * ``?tokens=1`` is RETIRED — 400
      ``{"code": "tokens_stream_retired_in_v4", "hint": ...}`` before the
      stream opens (§7.3). The per-session stream
      (``/slimapi/sessions/{sid}/stream``) is the only token channel.
    * Business frames (digest / q / p / error) carry
      ``id: g:<epoch>:<seq>`` (stamped by the hub fanout); meta, resync and
      heartbeat frames never carry an id (REPLAY-014).
    * ``Last-Event-ID: g:<epoch>:<seq>`` reconnects replay the window after
      the cursor — frames are yielded strictly seq-increasing BEFORE any new
      frame (REPLAY-002); four-way classification per design §4
      (① syntax / ② endpoint-domain / ③ epoch / ④ barrier→window→gap).
    * meta extends additively (§7.0②): ``capabilities`` + ``epoch`` +
      ``seqBase``; the meta frame itself has NO id.
    """

    if tokens is not None and tokens != "1":
        raise CodedHTTPException(400, code="invalid_tokens")

    # v4-only (4, 4) window: every request runs the v4 wire face (the
    # selector-less default-3 leg was removed with the V2b src teardown —
    # ``wire_view_from_scope`` is constant 4 now).
    if tokens == "1":
        # §7.3: tokens=1 retired in v4 — flat coded error before the stream
        # opens (no SSE bytes, no subscriber slot consumed).
        raise CodedHTTPException(
            400,
            code=TOKENS_STREAM_RETIRED_IN_V4["code"],
            hint=TOKENS_STREAM_RETIRED_IN_V4["hint"],
        )

    # v4-native assembly requires the one lifespan-owned ReplayLog shared by
    # both SSE domains. Missing state is an invalid application assembly,
    # not a selector-less/no-log runtime mode.
    replay_log = request.app.state.replay_log
    replay_epoch = replay_log.epoch
    last_event_id = request.headers.get("last-event-id")

    # Replay classification MUST run BEFORE hubs.subscribe(): the outcome
    # freezes a snapshot of the log at time T0, and the subscriber joins the
    # fanout set at T1 > T0 — replay covers seq ≤ last_seq@T0 while the queue
    # carries every frame published after attach, so a frame can never be
    # delivered twice and none can fall in the gap.
    replay_plan = classify_reconnect(
        last_event_id, replay_log, domain=GLOBAL_DOMAIN,
    )

    try:
        subscriber = request.app.state.hubs.subscribe()
    except SubscriberCapacityError as exc:
        return json_response(
            {"code": exc.code, "limit": exc.limit, "current": exc.current},
            status_code=503,
            headers={"Retry-After": "5"},
            accept_encoding=request.headers.get("accept-encoding"),
        )

    subscriber_id = subscriber.id
    # The meta payload is frozen HERE (handler time) for v4: ``seqBase``
    # must describe the same log snapshot the replay classification froze
    # above — building it lazily in the generator would let a fanout frame
    # published between subscribe() and the first yield shift seqBase
    # ahead of the replay plan the client is about to receive.
    meta_fields: dict = {
        "subscriberId": subscriber_id,
        "tokens": tokens == "1",
    }
    meta_fields.update(
        meta_v4_extension(replay_epoch, replay_log.last_seq(GLOBAL_DOMAIN))
    )
    meta = sse_frame(meta_fields, event="slimapi.meta")
    # (L2-A v3 face: ``tokens=1`` attached the events subscriber to the
    # token flush loop (TokenStreamRegistry.events_tokens / events_tap) so
    # lean ``token`` frames arrived at ~100ms cadence. Removed with the
    # V2b src teardown — ?tokens=1 is a flat 400 on the v4-only face, so
    # no stream can ever open with a token consumer.)
    # Pull the traffic ledger here (not in the generator) so a missing /
    # disabled ledger does not crash the SSE path on the first yield.
    traffic_ledger = getattr(request.app.state, "traffic_ledger", None)

    async def _accounted(frame: bytes) -> None:
        if traffic_ledger is not None:
            try:
                traffic_ledger.record_sse_downstream(
                    bucket="events_sse", bytes_out=len(frame),
                )
            except Exception:
                pass

    async def generate():
        # v3 §9.1 (Batch A): sse_open row when the stream actually starts
        # (generator first runs = 200 stream open); sse_close + same
        # lifecycleId in the finally below. Best-effort observability —
        # mock requests without ``.scope`` (direct route-invocation tests)
        # no-op inside the helper.
        lifecycle_id = sse_open(getattr(request, "scope", None), bucket="events_sse")
        try:
            # §7.2 terminal: meta FIRST — before any business frame,
            # heartbeat, replay frames, and the Last-Event-ID resync below.
            # Frame bytes are counted like every other handed-to-ASGI-send
            # frame. The meta frame itself never carries an id: (§7.0②).
            await _accounted(meta)
            yield meta
            if replay_plan is not None:
                # v4 (§7.2): reconnect handling — replay frames / resync,
                # yielded strictly before any new (queue) frame. Resync
                # frames never carry an id and the server NEVER sends a
                # snapshot frame (the client does an HTTP full fetch).
                if isinstance(replay_plan, ReplayResync):
                    resync = sse_frame({"reason": replay_plan.reason}, event="resync")
                    await _accounted(resync)
                    yield resync
                elif isinstance(replay_plan, ReplayFrames):
                    for entry in replay_plan.entries:
                        frame = frame_with_id(
                            entry.payload, GLOBAL_DOMAIN, replay_epoch, entry.seq,
                        )
                        await _accounted(frame)
                        yield frame
                # ReplayIgnoreReset → first-connect semantics: nothing.
            # (The v3 blanket-resync branch — any Last-Event-ID on the v3
            # face → leading resync{reconnect_no_replay} — was removed with
            # the V2b src teardown: under the v4-only face the handler-time
            # classification above ALWAYS yields a plan when a cursor is
            # present, so there is no reachable generator-side leg left.)
            while True:
                item = await subscriber.queue.get()
                if item is STOP:
                    break
                # Mirror put(): only sized frames bump queued_bytes; STOP is a
                # control sentinel and never entered the byte ledger.
                subscriber.ack(item)
                # Traffic accounting: per-frame downstream bytes (the curated
                # SSE ``downOut`` owner — see TrafficLedger.snapshot).
                # Semantics: ``record_sse_downstream`` counts bytes handed to
                # the ASGI ``send`` path (the yielded frame). This is the same
                #口径 as the middleware's ``downOut`` for non-SSE buckets: both
                # count bytes submitted to the ASGI send callable, before any
                # possible send-failure from a client disconnect. If ASGI send
                # fails the frame has already been counted — this is intentional
                # and consistent (no send-failure rollback exists elsewhere).
                await _accounted(item)
                yield item
        finally:
            # (The L2-A events-token ledger detach — released before the
            # control admission slot on the v3 face — was removed with the
            # attach above: no token consumer can exist on the v4 face.)
            # Must go through HubRegistry.unsubscribe (not GlobalHub) so
            # total_subscribers is decremented — otherwise the registry
            # counter leaks and admission permanently 503s after the cap.
            request.app.state.hubs.unsubscribe(subscriber)
            sse_close(getattr(request, "scope", None), bucket="events_sse", lifecycle_id=lifecycle_id)

    response_headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }

    def _rollback_admission() -> None:
        # BUG-002: same detach as the generator ``finally`` (idempotent,
        # membership-guarded in HubRegistry.unsubscribe) — runs only when
        # the generator never got to execute its own ``finally`` because
        # the ASGI response failed to start. No sse_close here: sse_open
        # lives inside the generator and never ran.
        request.app.state.hubs.unsubscribe(subscriber)

    return _SendStartRollbackResponse(
        generate(),
        on_start_failure=_rollback_admission,
        media_type="text/event-stream",
        headers=response_headers,
    )
