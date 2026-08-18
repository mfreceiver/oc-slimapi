import time

from fastapi import APIRouter, Request

from .. import __version__
from ..features import FEATURES
from ..gzip_util import json_response
from ..selector import wire_view_from_scope
from ..traffic import stash_up_in
from ..upstream import forward_upstream_headers, request_id_from_scope

router = APIRouter(prefix="/slimapi", tags=["health"])

# v4-contract §3.2: /health is DUAL-VIEW — the wire view comes from the
# selector stash of the running request (?v=3 → 3, ?v=4 → 4; selector-less
# direct invocation defaults to 3). /ready is NOT version-forked (contract
# §12 route table: 零 v4 差异) — shape AND values stay the terminal v3 ones
# regardless of the requested wire version.
READY_VIEW = 3


@router.get("/health")
async def health(request: Request):
    # One per-request view drives slimapi_contract, server.api_version AND
    # schema.version — a mismatched 3/4 combination is structurally
    # impossible (S-B04: the value is the selector stash itself, the single
    # source the request was dispatched on).
    # accepted_client_versions / clientMin / clientMax stay config-driven
    # (dual window: [3, 4]).
    view = wire_view_from_scope(request.scope)
    resp = {
        # lite-v2: expose the slim API contract revision as a top-level
        # field. Bumped ONLY on contract-breaking changes; additive wire
        # changes (e.g. optional fields) do NOT bump this. Dual window: the
        # contract revision follows the requested wire view (§3.2).
        "slimapi_contract": view,
        "sidecar": {"ok": True, "version": __version__},
        "server": {
            "api_version": view,
            "accepted_client_versions": list(request.app.state.config.accepted_client_versions),
        },
        "schema": {
            "degraded": request.app.state.schema_degraded,
            # v6 §4: diagnostic re-exposure of the wire-version triplet. These
            # are *view values*, NOT a feature-discovery surface — same source
            # as server.api_version above (§3.2: never a 3/4 combination).
            # Existing ``server.*`` keys are preserved for back-compat.
            "version": view,
            "clientMin": request.app.state.config.accepted_client_versions[0],
            "clientMax": request.app.state.config.accepted_client_versions[1],
        },
        # Q1 冻结 (design-token-stream.md §7 / §8): token-stream capability
        # is advertised at the ROOT level ``features.tokenStream`` (top-level,
        # parallel to sidecar/server/schema — NOT nested under server.*).
        # ocdroid dual-reads root/server during the rollout; the server is
        # pinned to root. Absence → ocdroid degrades to "whole message on
        # completion" with zero regression.
        #
        # ``thresholdedSkeleton`` is a DIAGNOSTIC-only flag (single-user
        # product; default-on, no opt-in / capability negotiation). The
        # behaviour does NOT depend on a client acknowledging it — small
        # ``state.output``/``state.error`` is inlined regardless. The numeric
        # ``skeletonInlineOutputMaxBytes`` lets ops confirm the tuned cap; it
        # does not bump the view constant (additive wire shape change).
        "features": {
            # L2-T0: static all-true announcements for the consolidated
            # capabilities (tokenCoalesce / permissionEvents / serverMerge /
            # transformAbsorb). Same release train — not gradual flags.
            **FEATURES,
            "tokenStream": True,
            "thresholdedSkeleton": True,
            "skeletonInlineOutputMaxBytes": request.app.state.config.skeleton_inline_output_max_bytes,
        },
    }
    if view >= 4:
        # v4-contract §3.2: v4 view adds the TRANSIENT auxiliary field —
        # availability of the DB auxiliary source. Stage A (B3a): dbaux is
        # not landed, so this is a frozen placeholder {available: False,
        # mode: "http"} until the B1/B5 lanes wire the real state. The v3
        # view carries NO auxiliary key (byte-identical terminal shape).
        resp["auxiliary"] = {"available": False, "mode": "http"}
    # S-E: optional deployment revision, omitted when None
    rev = request.app.state.deployment_revision
    if rev is not None:
        resp["server"]["deploymentRevision"] = rev
    allowlist_feature = {
        "enabled": request.app.state.config.directory_allowlist is not None,
    }
    hubs = getattr(request.app.state, "hubs", None)
    if hubs is not None:
        try:
            hub = hubs.get_global()
        except Exception:
            hub = None
        if hub is not None:
            allowlist_feature["droppedEvents"] = hub.allowlist_dropped_events
    resp["features"]["allowlist"] = allowlist_feature
    return json_response(resp, accept_encoding=request.headers.get("accept-encoding"))


@router.get("/ready")
async def ready(request: Request):
    # v4-contract §3.2/§12: ready is 零 v4 差异 — shape AND values frozen to
    # the terminal v3 view; no contract field on this endpoint.
    view = READY_VIEW
    started = time.monotonic()
    try:
        # P0-6: forward X-Request-ID so the sidecar access log line can be
        # correlated with opencode's /global/health log entry (contract §7).
        # ``client.get`` builds the request internally; pass headers through.
        response = await request.app.state.upstream.get(
            "/global/health",
            timeout=5.0,
            headers=forward_upstream_headers(
                directory=None,
                request_id=request_id_from_scope(request.scope),
            ),
        )
        # Traffic accounting: stash the health-check response body so the
        # health bucket's upIn reflects the upstream ping bytes.
        stash_up_in(request, len(response.content))
        ok = response.status_code < 300
    except Exception:
        ok = False
    return json_response({
        "upstream": {"ok": ok, "latencyMs": round((time.monotonic() - started) * 1000)},
        "server": {
            "api_version": view,
            "accepted_client_versions": list(request.app.state.config.accepted_client_versions),
        },
        "schema": {
            "degraded": request.app.state.schema_degraded,
            "version": view,
            "clientMin": request.app.state.config.accepted_client_versions[0],
            "clientMax": request.app.state.config.accepted_client_versions[1],
        },
    }, status_code=200 if ok else 503, accept_encoding=request.headers.get("accept-encoding"))
