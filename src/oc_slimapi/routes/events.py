from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..sse.hub import STOP, SubscriberCapacityError, sse_frame
from ..sse_observability import sse_close, sse_open

router = APIRouter(prefix="/slimapi", tags=["events"])


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
    """

    if tokens is not None and tokens != "1":
        raise CodedHTTPException(400, code="invalid_tokens")

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

    async def generate():
        # v3 §9.1 (Batch A): sse_open row when the stream actually starts
        # (generator first runs = 200 stream open); sse_close + same
        # lifecycleId in the finally below. Best-effort observability —
        # mock requests without ``.scope`` (direct route-invocation tests)
        # no-op inside the helper.
        lifecycle_id = sse_open(getattr(request, "scope", None), bucket="events_sse")
        try:
            # §7.2 terminal: meta FIRST — before any business frame,
            # heartbeat, and the Last-Event-ID resync replay below. Frame
            # bytes are counted like every other handed-to-ASGI-send frame.
            meta = sse_frame(
                {"subscriberId": subscriber_id, "tokens": tokens == "1"},
                event="slimapi.meta",
            )
            if traffic_ledger is not None:
                try:
                    traffic_ledger.record_sse_downstream(
                        bucket="events_sse", bytes_out=len(meta),
                    )
                except Exception:
                    pass
            yield meta
            if request.headers.get("last-event-id"):
                resync = sse_frame({"reason": "reconnect_no_replay"}, event="resync")
                if traffic_ledger is not None:
                    try:
                        traffic_ledger.record_sse_downstream(
                            bucket="events_sse", bytes_out=len(resync),
                        )
                    except Exception:
                        pass
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
                if traffic_ledger is not None:
                    try:
                        traffic_ledger.record_sse_downstream(
                            bucket="events_sse", bytes_out=len(item),
                        )
                    except Exception:
                        pass
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
