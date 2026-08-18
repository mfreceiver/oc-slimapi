"""``GET /slimapi/metrics`` — T3 observability endpoint (contract §2 / §6).

Surfaces subscriber counts, per-hub upstream counters, per-client queue
health, and the transform-pool's active / waiting slots so an operator can
see why a client got a 503 or why the upstream connection was reopened.

The version selector (``?v=3`` terminal) already covers every
``/slimapi/**`` route, so this handler does nothing beyond delegating to
:meth:`HubRegistry.snapshot_metrics` and negotiating gzip with the caller.
"""

from fastapi import APIRouter, Request

from ..gzip_util import json_response
from ..traffic import SESSIONS_DEGRADED_STATE_ATTR

router = APIRouter(prefix="/slimapi", tags=["metrics"])


@router.get("/metrics")
async def metrics(request: Request):
    hubs_snapshot = request.app.state.hubs.snapshot_metrics()
    hubs_snapshot["batch"] = None
    # Stage D (design §7): expose ``sse.tokenStream.*`` when a token-stream
    # registry is wired. The block sits under the existing ``sse`` umbrella
    # alongside the control-plane ``subscribers`` / ``hubs`` / ``clients``
    # entries. Absent (no registry) in test apps that do not wire one, so
    # the control-plane metrics shape is unchanged there.
    token_registry = getattr(request.app.state, "token_registry", None)
    if token_registry is not None:
        hubs_snapshot["sse"]["tokenStream"] = token_registry.snapshot_token_metrics()
    # Traffic-accounting ledger (additive). Only emitted when a ledger is
    # wired into app.state so existing test fixtures that do not construct
    # one continue to see the original ``{sse, skeleton, batch}`` shape
    # untouched (zero-knowledge additive).
    traffic_ledger = getattr(request.app.state, "traffic_ledger", None)
    if traffic_ledger is not None:
        hubs_snapshot["traffic"] = traffic_ledger.snapshot()
    # B1b stage 1 is shadow-only: no HTTP sweep is issued, but the scheduler's
    # bounded counters are observable here when production lifespan wires it.
    # Disabled/test apps intentionally omit the additive block.
    qp_sweep = getattr(request.app.state, "qp_sweep", None)
    if qp_sweep is not None and getattr(qp_sweep, "enabled", True):
        hubs_snapshot["sweep"] = qp_sweep.metrics()
    # DB auxiliary observability (B3a-B5, v4-contract §9.1): state +
    # sliding-window latency percentiles + event counters. Additive —
    # test apps without a wired source keep the original shape. The
    # resolved DB path is deliberately NOT echoed here (same no-leak
    # posture as the health auxiliary view); ``source`` tags the
    # resolution channel only.
    dbaux = getattr(request.app.state, "dbaux", None)
    if dbaux is not None:
        snap = dbaux.snapshot()
        breaker = snap["breaker"]
        hubs_snapshot["dbaux"] = {
            "available": snap["available"],
            "mode": snap["mode"],
            "reason": snap["reason"],
            "generation": snap["generation"],
            "source": snap["source"],
            "latency": {
                "p50_ms": breaker["p50_ms"],
                "p99_ms": breaker["p99_ms"],
                "samples": breaker["samples"],
                "total": breaker["total"],
            },
            "breaker_open": breaker["open"],
            "counters": snap["counters"],
        }
    # v4 sessions degraded per-response counters (v4-contract §9.1, rev-gate
    # BLOCKER-4): distinct from the dbaux state-machine event counters above
    # — one disable/trip can serve any number of degraded responses, so the
    # operator-facing truth is counted per response by the traffic
    # middleware. Zero-knowledge additive: the block appears only once the
    # middleware has mounted the counters on app.state (any first request
    # through the stack does); absent on a freshly-booted app, matching the
    # dbaux/traffic/sweep convention. The metrics handler runs before the
    # middleware records the current request, so a first-ever GET /slimapi
    # metrics sees the pre-mount state (no block) — stable either way.
    degraded_counters = getattr(
        request.app.state, SESSIONS_DEGRADED_STATE_ATTR, None
    )
    if degraded_counters is not None:
        hubs_snapshot["sessionsDegraded"] = degraded_counters.snapshot()
    return json_response(
        hubs_snapshot,
        accept_encoding=request.headers.get("accept-encoding"),
    )
