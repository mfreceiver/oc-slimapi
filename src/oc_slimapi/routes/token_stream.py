"""``GET /slimapi/sessions/{sid}/stream`` — token-stream SSE (design §5.1).

Generates a per-session ``text/event-stream`` of in-flight text-part deltas,
tombstones and resync controls sourced from the process-wide
:class:`TokenStreamHub` accumulator.

Stage-D scope (design-token-stream.md §5.1 / §5.5 / §5.6 / §7):

* Path is exact ``/slimapi/sessions/{sid}/stream`` — it lives under
  ``/slimapi/**`` so the version selector terminal applies for
  free (no route-level ``Depends``).
* Admission runs in :meth:`TokenStreamRegistry.subscribe` under the token
  cap (independent ledger, NOT ``MAX_TOTAL_SUBSCRIBERS``); overflow → 503
  ``sse_token_subscriber_limit`` + ``Retry-After``.
* Replay semantics (v4-contract §7): business frames
  (delta / message.removed tombstone / replayable resync)
  carry ``id: t:<sid>:<epoch>:<seq>``; a ``Last-Event-ID:
  t:<sid>:<epoch>:<seq>`` reconnect replays the window after the cursor
  (frames strictly seq-increasing before any new frame) per the frozen
  four-way classification (① syntax / ② endpoint+sid / ③ epoch /
  ④ barrier→window→gap); meta / route-private resync / heartbeat frames
  never carry an id.
* **Identity-only stream (§7.2 terminal, v4-contract §7 "SSE 恒
  identity")**: the body is NEVER content-encoded — meta /
  business / resync frames are yielded raw regardless of
  ``Accept-Encoding`` — so the response emits no ``Vary`` header and
  does not participate in Accept-Encoding negotiation (an
  AE-independent representation needs no variance marker; the stream is
  no-cache anyway). The route is identity-only because its frames are
  latency-sensitive token deltas, not because of a version fork.
* Lifecycle: ``subscribe`` starts the flush loop on first-attach; the
  generator's ``finally`` detaches on disconnect / client-close, and the
  last-detach stops the loop (NB-C4). Idempotent and leak-free.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..sse.replay_log import (
    ReplayFrames,
    ReplayResync,
    token_domain,
)
from ..sse.replay_wire import classify_reconnect, frame_with_id, meta_v4_extension
from ..sse.token_hub import (
    STOP,
    TokenSubscriberCapacityError,
    _resync_frame,
    sse_frame,
)
from ..sse_observability import sse_close, sse_open

router = APIRouter(prefix="/slimapi", tags=["token-stream"])


class _SendStartRollbackResponse(StreamingResponse):
    """StreamingResponse that rolls back route admission when the ASGI
    response never starts (BUG-002 / FI-001).

    The route admits its subscriber at handler time and detaches it in the
    body generator's ``finally``. Starlette sends ``http.response.start``
    BEFORE iterating the generator (``StreamingResponse.stream_response``),
    so a ``send`` failure at response-start leaves the generator
    never-started — its ``finally`` never runs and the token admission slot
    (plus the flush task the first-attach started) leaks; N failures
    exhaust the cap and later connections 503. This subclass wraps the
    ASGI send path: if an exception escapes before the first
    ``http.response.body`` message was handed to ASGI, the route's
    rollback hook runs the same idempotent detach routine the generator
    ``finally`` uses, then the exception re-raises unchanged.

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


def _validate_directory_query(request: Request, directory: str | None) -> None:
    """Validate the no-op ``directory`` query without reading retired headers.

    Differing normalised values in a repeated ``?directory=`` query produce
    400 ``invalid_directory_selector``; a single value still passes through
    :func:`validate_directory`.  The validated value does not participate in
    token fanout because the accumulator keys on globally unique ``sid``.

    In production, header-only and query+header directory errors are emitted
    by ``SlimapiSelectorMiddleware`` before route dispatch.  Selector-less
    callers of this helper therefore validate query input only and must not
    inspect the retired inbound directory header.
    """
    getlist = getattr(request.query_params, "getlist", None)
    if getlist is None:  # mock/direct-invocation requests (httpx QueryParams)
        getlist = lambda key: list(request.query_params.get_list(key))  # noqa: E731
    values = [value for value in getlist("directory")]
    if len({(value.rstrip("/") or "/") for value in values}) > 1:
        raise CodedHTTPException(400, code="invalid_directory_selector")
    if directory is None:
        return
    validate_directory(directory)


@router.get("/sessions/{sid}/stream")
async def token_stream(request: Request, sid: str, directory: str | None = None):
    """Per-session token-stream SSE.

    The optional ``directory`` query is validated but remains a fanout NO-OP:
    the accumulator keys on globally unique ``sid``.  In production, the
    selector handles retired directory-header errors before route dispatch;
    this handler does not read that header itself.
    """
    # Query validation remains before replay classification and admission.
    _validate_directory_query(request, directory)
    registry = request.app.state.token_registry
    last_event_id = request.headers.get("last-event-id")

    replay_log = request.app.state.replay_log
    replay_epoch = replay_log.epoch

    # Replay classification MUST run BEFORE registry.subscribe(): the
    # outcome freezes a log snapshot at T0 while the subscriber joins the
    # fanout set at T1 > T0 — replay covers seq ≤ last_seq@T0, the queue
    # carries everything published after attach; no frame twice, no gap.
    replay_plan = classify_reconnect(
        last_event_id, replay_log,
        domain=token_domain(sid), token_sid=sid,
    )

    try:
        subscriber = registry.subscribe(sid)
    except TokenSubscriberCapacityError as exc:
        body = {"code": exc.code, "limit": exc.limit, "current": exc.current}
        return json_response(
            body,
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
    meta_fields: dict = {"subscriberId": subscriber_id, "tokens": True}
    meta_fields.update(
        meta_v4_extension(
            replay_epoch, replay_log.last_seq(token_domain(sid)),
        )
    )
    meta_fields["capabilities"] = {
        **meta_fields["capabilities"], "tokenFrameSeq": True,
    }
    meta_frame = sse_frame(meta_fields, event="slimapi.meta")
    # §7.2 terminal: the meta first frame is the id channel (the
    # retired X-Slimapi-Subscriber-ID header is never produced), AND the
    # frozen "SSE 流不做 content-encoding（帧字节原样）" clause — the
    # stream is ALWAYS identity regardless of Accept-Encoding
    # (meta/business/resync frames raw).
    # Pull the traffic ledger here so a missing / disabled ledger does not
    # crash the SSE path on the first yield.
    traffic_ledger = getattr(request.app.state, "traffic_ledger", None)

    async def generate():
        def _account(out_bytes: bytes) -> None:
            # Traffic accounting: the on-the-wire frame bytes — the
            # token_stream_sse ``downOut`` owner.
            # Semantics: counts bytes handed to the ASGI ``send`` path
            # (the yielded frame — §7.2 terminal: the stream is identity,
            # so wire bytes == frame bytes). This is the same
            #口径 as the middleware's ``downOut`` for non-SSE buckets: both
            # count bytes submitted to ASGI send, before any possible
            # send-failure from a client disconnect. If ASGI send fails
            # the frame has already been counted — this is intentional and
            # consistent (no send-failure rollback exists elsewhere).
            if traffic_ledger is None:
                return
            try:
                traffic_ledger.record_sse_downstream(
                    bucket="token_stream_sse", bytes_out=len(out_bytes),
                )
            except Exception:
                pass

        try:
            # Open the traffic lifecycle when the stream actually starts;
            # close it with the same lifecycleId in the finally below.
            # ``getattr`` — direct route-invocation tests may pass mock
            # requests without ``.scope`` (helper then no-ops).
            lifecycle_id = sse_open(getattr(request, "scope", None), bucket="token_stream_sse")
            # §7.2 terminal: meta FIRST, before replay / Last-Event-ID
            # handling and newly published frames. ``tokens`` is true on /stream
            # (a token stream always carries tokens). Identity stream —
            # frames are yielded raw (§7.2 terminal). v4 (§7.0②) extends
            # meta additively (capabilities/epoch/seqBase); the meta frame
            # itself never carries an id.
            out = meta_frame
            _account(out)
            yield out
            # v4 (§7.2): reconnect handling — replay frames / resync are
            # yielded strictly before any new frame. Resync frames never
            # carry an id; the client does an HTTP full fetch after resync.
            if isinstance(replay_plan, ReplayResync):
                out = _resync_frame(sid, replay_plan.reason)
                _account(out)
                yield out
            elif isinstance(replay_plan, ReplayFrames):
                for entry in replay_plan.entries:
                    frame = frame_with_id(
                        entry.payload, token_domain(sid), replay_epoch, entry.seq,
                    )
                    out = frame
                    _account(out)
                    yield out
            # ReplayIgnoreReset → first-connect semantics: nothing.
            while True:
                item = await subscriber.queue.get()
                if item is STOP:
                    break
                # Mirror put(): only sized frames bump queued_bytes; STOP is a
                # control sentinel never entered into the byte ledger.
                subscriber.ack(item)
                out = item
                _account(out)
                yield out
        finally:
            # Detach via the registry so total_subscribers is decremented and
            # the flush loop is stopped on last-detach (NB-C4). Idempotent.
            registry.unsubscribe(subscriber)
            sse_close(getattr(request, "scope", None), bucket="token_stream_sse", lifecycle_id=lifecycle_id)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    # §7.2 terminal: frames are always identity → the representation does
    # not depend on Accept-Encoding, so no Vary is emitted (an
    # AE-independent representation needs no AE variance marker; the stream
    # is no-cache anyway). ``subscriberId`` is carried by the first
    # slimapi.meta frame rather than an HTTP response header.

    def _rollback_admission() -> None:
        # BUG-002: same detach as the generator ``finally`` (idempotent,
        # membership-guarded in TokenStreamRegistry.unsubscribe) — runs only
        # when the generator never got to execute its own ``finally``
        # because the ASGI response failed to start. registry.unsubscribe
        # also stops the flush task on last-detach (NB-C4), so the
        # first-attach task is torn down with the slot. No sse_close here:
        # sse_open lives inside the generator and never ran.
        registry.unsubscribe(subscriber)

    return _SendStartRollbackResponse(
        generate(),
        on_start_failure=_rollback_admission,
        media_type="text/event-stream",
        headers=headers,
    )
