import time

from fastapi import APIRouter, Request

from .. import __version__
from ..features import FEATURES
from ..gzip_util import json_response
from ..traffic import stash_up_in
from ..upstream import forward_upstream_headers, request_id_from_scope

router = APIRouter(prefix="/slimapi", tags=["health"])

# v3-contract §3a terminal state: single v3 view. Every request that reaches
# these routes ran v3 semantics (?v=3 enforced by the selector) — there is no
# v2 view left to fork on.
WIRE_VIEW = 3


@router.get("/health")
async def health(request: Request):
    # One constant drives slimapi_contract, server.api_version AND
    # schema.version — a 3/2 combination is structurally impossible.
    # accepted_client_versions / clientMin / clientMax stay config-driven
    # (3.0.0: [3, 3]).
    view = WIRE_VIEW
    resp = {
        # lite-v2: expose the slim API contract revision as a top-level
        # field. Bumped ONLY on contract-breaking changes; additive wire
        # changes (e.g. optional fields) do NOT bump this. v3 terminal:
        # the single view is 3 (v3-contract §3a).
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
            # as server.api_version above (v3-contract §3a: v2 view=2, v3
            # view=3, never a 3/2 combination). Existing ``server.*`` keys are
            # preserved for back-compat.
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
        # does not bump ``X-Slimapi-Version`` (additive wire shape change).
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
    # S-E: optional deployment revision, omitted when None
    rev = request.app.state.deployment_revision
    if rev is not None:
        resp["server"]["deploymentRevision"] = rev
    return json_response(resp, accept_encoding=request.headers.get("accept-encoding"))


@router.get("/ready")
async def ready(request: Request):
    # v3-contract §3a terminal: same single-view constant as /health for the
    # schema triple — NO contract field on this endpoint (shape locked).
    view = WIRE_VIEW
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
