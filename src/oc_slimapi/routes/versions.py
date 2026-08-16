"""``GET /slimapi/versions`` — v3-contract §3 discovery endpoint (Batch A).

Producer-owned shape (consumers MUST ignore unknown fields for forward
compat; this endpoint never rejects requests over unknown anything — it takes
no parameters at all). Exempt from the version header gate AND the selector
(v3-contract §2): reachable headerless, so a client can discover before it
knows which versions exist. Non-GET → 405 + ``Allow: GET`` (enforced by the
selector middleware with priority over everything).

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
from ..gzip_util import json_response

router = APIRouter(prefix="/slimapi", tags=["versions"])

CURRENT_VERSION = 3
AVAILABLE_VERSIONS: list[int] = [2, 3]

# Capability map keyed by version STRING (contract §3 shape, verbatim).
CAPABILITIES: dict[str, dict] = {
    "2": {
        "etag": True,
        "contentFingerprint": True,
        "thinRoutes": ["todo", "children", "diff"],
    },
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
    return json_response(
        {
            "current": CURRENT_VERSION,
            "available": AVAILABLE_VERSIONS,
            "capabilities": CAPABILITIES,
            "sidecarVersion": __version__,
        },
        headers={"Cache-Control": "no-store"},
        accept_encoding=request.headers.get("accept-encoding"),
    )
