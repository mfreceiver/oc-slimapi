from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..selector import wire_view_from_scope
from ..sse.hub import STOP, SubscriberCapacityError, sse_frame
from ..sse.replay_log import (
    GLOBAL_DOMAIN,
    RESYNC_RECONNECT_NO_REPLAY,
    ReplayFrames,
    ReplayLog,
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


def _request_wire_v4(request: Request) -> bool:
    """True iff this request runs the v4 wire face (§2 selector stash).

    Selector-less stacks (direct route invocation in tests, mock requests
    without ``.scope``) observe the default v3 view — identical to
    :func:`~oc_slimapi.selector.wire_view_from_scope` semantics.
    """
    scope = getattr(request, "scope", None)
    if scope is None:
        return False
    return wire_view_from_scope(scope) == 4


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

    ``?tokens=1`` (L2-A, additive): additionally receive lean ``token``
    frames — coalesced per-``(sessionID, messageID, partID)`` window concats
    fanned from the token-stream flush loop (reusing its existing 100ms /
    4KiB coalescing). Absent (default) = current behaviour unchanged. Only
    the literal value ``"1"`` is legal; anything else → 400 ``invalid_tokens``
    (same strictness as the messages ``directory_not_allowed`` structural
    guard). ``tokens=1`` covers ALL sessions' coalesced deltas (no per-sid
    filter, MVP) and is mutually exclusive with the per-session token stream
    at the CLIENT (server does not force-disconnect a double-subscriber).

    v3 (§7.2, Batch D): a ``?v=3`` request gets a leading
    ``slimapi.meta`` first frame — ``{"subscriberId": <id>, "tokens": <bool>}``
    (``tokens`` = whether ``tokens=1`` was attached) — before ANY business
    frame, heartbeat, or Last-Event-ID resync replay; the response-header id
    frame replaces the retired ``X-Slimapi-Subscriber-ID`` header (§1:
    removed at 3.0.0 — never produced; the meta frame's ``subscriberId``
    is the sole id channel).

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

    v4 = _request_wire_v4(request)
    if v4 and tokens == "1":
        # §7.3: tokens=1 retired in v4 — flat coded error before the stream
        # opens (no SSE bytes, no subscriber slot consumed).
        raise CodedHTTPException(
            400,
            code=TOKENS_STREAM_RETIRED_IN_V4["code"],
            hint=TOKENS_STREAM_RETIRED_IN_V4["hint"],
        )

    # B3b-2: replay wiring (absent on minimal test apps → v4 degrades to the
    # id-less / un-replayed stream, mirroring the v3 shape).
    replay_log: ReplayLog | None = getattr(request.app.state, "replay_log", None)
    replay_epoch: str | None = getattr(request.app.state, "replay_epoch", None)
    if replay_epoch is None and replay_log is not None:
        replay_epoch = replay_log.epoch
    last_event_id = request.headers.get("last-event-id")

    # Replay classification MUST run BEFORE hubs.subscribe(): the outcome
    # freezes a snapshot of the log at time T0, and the subscriber joins the
    # fanout set at T1 > T0 — replay covers seq ≤ last_seq@T0 while the queue
    # carries every frame published after attach, so a frame can never be
    # delivered twice and none can fall in the gap.
    replay_plan = None
    if v4 and replay_log is not None:
        replay_plan = classify_reconnect(
            last_event_id, replay_log, domain=GLOBAL_DOMAIN,
        )
    elif v4 and last_event_id:
        # No replay infrastructure on this app (minimal/test stack): a
        # reconnect cursor we cannot evaluate is NEVER first-connect — the
        # client may be missing frames we have no record of. Fail safe with
        # the blanket resync (mirrors the v3 semantics).
        replay_plan = ReplayResync(RESYNC_RECONNECT_NO_REPLAY)

    try:
        # rev-gate BLOCKER-1 / condition 5: the wire version flows INTO
        # the subscription — v4 suppresses the connection-local
        # ``server.connected`` welcome frame (outside the frozen no-id
        # control set; must not bypass the replay log) and is stamped on
        # the subscriber so fanout frames carry ``id:`` from the start.
        subscriber = request.app.state.hubs.subscribe(wire_v4=v4)
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
    if v4 and replay_log is not None and replay_epoch is not None:
        meta_fields.update(
            meta_v4_extension(replay_epoch, replay_log.last_seq(GLOBAL_DOMAIN))
        )
    meta = sse_frame(meta_fields, event="slimapi.meta")
    # L2-A: opt-in token frames. The events subscriber becomes a first-class
    # consumer of the token flush loop (TokenStreamRegistry.events_tokens +
    # TokenStreamHub.events_tap) so token frames arrive at ~100ms cadence and
    # the flush loop stays alive even when no per-session stream is open.
    token_registry = getattr(request.app.state, "token_registry", None)
    if tokens == "1" and token_registry is not None:
        token_registry.attach_events_subscriber(subscriber)
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
            elif not v4 and request.headers.get("last-event-id"):
                # v3 (frozen): any Last-Event-ID → resync{reconnect_no_replay}.
                # v4 NEVER reaches this branch — a ①② violation is an
                # ignore+reset (no resync), NOT the v3 blanket resync.
                resync = sse_frame(
                    {"reason": RESYNC_RECONNECT_NO_REPLAY}, event="resync")
                await _accounted(resync)
                yield resync
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
            # L2-A: release the events-token ledger slot before the control
            # admission slot, so the flush loop stops on the true last-detach
            # (both ledgers empty) and GlobalHub grace re-arms symmetrically.
            if tokens == "1" and token_registry is not None:
                token_registry.detach_events_subscriber(subscriber)
            # Must go through HubRegistry.unsubscribe (not GlobalHub) so
            # total_subscribers is decremented — otherwise the registry
            # counter leaks and admission permanently 503s after the cap.
            request.app.state.hubs.unsubscribe(subscriber)
            sse_close(getattr(request, "scope", None), bucket="events_sse", lifecycle_id=lifecycle_id)

    response_headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=response_headers,
    )
