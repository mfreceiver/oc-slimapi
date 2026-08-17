"""``GET /slimapi/versions`` — v3-contract §3 discovery endpoint.

Producer-owned shape (consumers MUST ignore unknown fields for forward
compat; this endpoint never rejects requests over unknown anything — it takes
no parameters at all). Exempt from the selector judgement (v3-contract §2):
reachable without any ``v``, so a client can discover before it knows which
versions exist. Non-GET → 405 + ``Allow: GET`` (enforced by the selector
middleware with priority over everything).

Terminal state (3.0.0): ``available == [3]`` and the capability map carries
ONLY the ``"3"`` key — v2 is deleted.

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

router = APIRouter(prefix="/slimapi", tags=["versions"])

CURRENT_VERSION = 3
AVAILABLE_VERSIONS: list[int] = [3]

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
}


@router.get("/versions")
async def versions(request: Request):
    # capabilities["3"] carries the expand capability (design-expand §6):
    # the frozen §2.2 categories plus ``fragmentMaxBytes`` read live from
    # Settings at request time, so the advertisement always matches the
    # effective per-fragment response cap of the running process.
    capabilities = {
        "3": {
            **CAPABILITIES["3"],
            "expand": {
                "categories": EXPAND_CATEGORIES,
                "fragmentMaxBytes": settings.max_expand_response_bytes,
            },
        },
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
