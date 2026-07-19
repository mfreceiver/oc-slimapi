from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..gzip_util import json_response
from ..sse.hub import STOP, SubscriberCapacityError, sse_frame

router = APIRouter(prefix="/slimapi", tags=["events"])


@router.get("/events")
async def events(request: Request):
    """Process-wide curated SSE stream.

    No directory / sessionId / stream parameters: the sidecar holds one
    /global/event subscription and every connected client sees every session
    across every directory. Clients filter locally.

    Admission runs in ``HubRegistry.subscribe`` under T3 caps (contract §6);
    exceeding per-directory or total caps raises ``SubscriberCapacityError``
    which we map to a 503 with ``Retry-After`` so the client backs off and
    retries instead of burning an upstream connection.
    """

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

    async def generate():
        try:
            if request.headers.get("last-event-id"):
                yield sse_frame({"reason": "reconnect_no_replay"}, event="resync")
            while True:
                item = await subscriber.queue.get()
                if item is STOP:
                    break
                # Mirror put(): only sized frames bump queued_bytes; STOP is a
                # control sentinel and never entered the byte ledger.
                subscriber.ack(item)
                yield item
        finally:
            # Must go through HubRegistry.unsubscribe (not GlobalHub) so
            # total_subscribers is decremented — otherwise the registry
            # counter leaks and admission permanently 503s after the cap.
            request.app.state.hubs.unsubscribe(subscriber)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            # Ephemeral per-connection id; clients echo it back in logs /
            # support tickets so an operator can correlate it with the
            # ``sse.clients[]`` entry on /slimapi/metrics. Not an auth
            # identity.
            "X-Slimapi-Subscriber-ID": subscriber_id,
        },
    )
