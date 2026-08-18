"""``GET /slimapi/versions`` — v4-contract §3 discovery endpoint.

Producer-owned shape (consumers MUST ignore unknown fields for forward
compat; this endpoint never rejects requests over unknown anything — it takes
no parameters at all). Exempt from the selector judgement (§2): reachable
without any ``v``, so a client can discover before it knows which versions
exist. Non-GET → 405 + ``Allow: GET`` (enforced by the selector middleware
with priority over everything).

Dual-version window (4.0.0, B3a-A3): ``available == [3, 4]``,
``current == 4`` (S-B04: during the (3,4) window the current view is
always the newest major) and the capability map carries BOTH keys — the
``"3"`` shape verbatim from the 3.x terminal state, plus the ``"4"``
differential face (§3.1). ``sseReplay`` / ``qpImmediateFull`` are
deliberately ABSENT from ``"4"`` until B3b lands them — the acceptance
criteria assert the absence, and capability keys are STATIC (never vary
with runtime/DB state).

Response constraints (§3, frozen):

* ``current`` ∈ ``available``; ``available`` unique ascending.
* ``capabilities`` keyed by version string.
* ``sidecarVersion`` = the installed package version (importlib.metadata via
  ``oc_slimapi.__version__`` — single source of truth, release.sh bumps
  propagate).
* ``Cache-Control: no-store``; **no ETag** (this endpoint is deliberately not
  part of the ETag/304 family — discovery must always be revalidated).
* gzip family = the ``json_response`` negotiated-compression family;
  ``Vary: Accept-Encoding`` always.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..config import settings
from ..gzip_util import json_response
from ..traffic import EXPAND_CATEGORIES
from ..versioning import ACCEPTED_CLIENT_VERSIONS, SERVER_API_VERSION

router = APIRouter(prefix="/slimapi", tags=["versions"])

# S-B04: current = the pinned SERVER_API_VERSION (newest major during the
# dual window); available = the accepted range, ascending and unique.
CURRENT_VERSION = SERVER_API_VERSION
AVAILABLE_VERSIONS: list[int] = list(
    range(ACCEPTED_CLIENT_VERSIONS[0], ACCEPTED_CLIENT_VERSIONS[1] + 1)
)

# Capability map keyed by version STRING (contract §3 shape, verbatim).
CAPABILITIES: dict[str, dict] = {
    "3": {
        "envelope": ["messages", "sessions"],
        "directoryQuery": True,
        "versionHeaderOptional": True,
        "writeRoutes": True,
        "readRoutes": [
            "file",
            "vcs",
            "find",
            "providers",
            "sessionSingle",
            "activeSessions",
            "globalHealth",
        ],
    },
    # v4 differential face (§3.1): STATIC capability keys only — the wire
    # deltas the 4.0 window actually ships at this stage. sseReplay /
    # qpImmediateFull are NOT advertised here (B3b owns them; acceptance
    # asserts their absence). No runtime-dependent keys ever appear.
    "4": {
        "globalSessions": True,
        "auxiliaryFilters": True,
    },
}


@router.get("/versions")
async def versions(request: Request):
    # capabilities["3"] carries the expand capability (design-expand §6):
    # the frozen §2.2 categories plus ``fragmentMaxBytes`` read live from
    # Settings at request time, so the advertisement always matches the
    # effective per-fragment response cap of the running process.
    # capabilities["4"] stays static per §3.1 (no runtime-injected keys).
    capabilities = {
        "3": {
            **CAPABILITIES["3"],
            "expand": {
                "categories": EXPAND_CATEGORIES,
                "fragmentMaxBytes": settings.max_expand_response_bytes,
            },
        },
        "4": dict(CAPABILITIES["4"]),
    }
    return json_response(
        {
            "current": CURRENT_VERSION,
            "available": AVAILABLE_VERSIONS,
            "capabilities": capabilities,
            "sidecarVersion": __version__,
        },
        headers={"Cache-Control": "no-store"},
        accept_encoding=request.headers.get("accept-encoding"),
    )
