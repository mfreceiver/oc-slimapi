"""``GET /slimapi/metrics`` — T3 observability endpoint (contract §2 / §6).

Surfaces subscriber counts, per-hub upstream counters, per-client queue
health, and the transform-pool's active / waiting slots so an operator can
see why a client got a 503 or why the upstream connection was reopened.

The version gate (``SlimapiVersionMiddleware``) already covers every
``/slimapi/**`` route, so this handler does nothing beyond delegating to
:meth:`HubRegistry.snapshot_metrics` and negotiating gzip with the caller.
"""

from fastapi import APIRouter, Request

from ..gzip_util import json_response

router = APIRouter(prefix="/slimapi", tags=["metrics"])


@router.get("/metrics")
async def metrics(request: Request):
    hubs_snapshot = request.app.state.hubs.snapshot_metrics()
    batch_ledger = getattr(request.app.state, "batch_ledger", None)
    if batch_ledger is not None:
        hubs_snapshot["batch"] = batch_ledger.snapshot()
    else:
        hubs_snapshot["batch"] = None
    return json_response(
        hubs_snapshot,
        accept_encoding=request.headers.get("accept-encoding"),
    )
