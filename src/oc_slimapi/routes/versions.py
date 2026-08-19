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
differential face (§3.1). B3b landed and advertised the remaining two
keys in the same batch as their implementations (n1 frozen timing):
``sseReplay`` (Last-Event-ID reconnect replay, §7.2) spreads from the
same-source ``META_CAPABILITY_KEYS`` constant the v4 SSE ``slimapi.meta``
frame advertises, and ``qpImmediateFull`` is frozen as "already true"
(design-v4-qp-payload §2/§3 — zero wire change). Capability keys remain
STATIC (never vary with runtime/DB state — replay-log configuration does
not alter the advertisement, §3.1).

2026-08-19 revision batch: the ``"4"`` face gains two ADDITIVE keys —
``readiness`` (§3.3 nine-ID readiness gate, always advertised) and
``expand`` (§14, emitted iff ``messages.expand.v4`` is satisfied; the
``"3"`` face keeps its own expand block unconditionally). Both are
assembled by ``_capabilities4`` below; the four STATIC keys stay frozen
verbatim in front of them, and the ``"3"`` face stays byte-identical
(v3 freeze, §0.5).

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

from typing import Iterable

from fastapi import APIRouter, Request

from .. import __version__
from .. import readiness as readiness_mod
from ..config import settings
from ..gzip_util import json_response
from ..sse.replay_wire import META_CAPABILITY_KEYS
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
    # deltas the 4.0 window ships. B3b advertises sseReplay /
    # qpImmediateFull in the same batch as their implementations (n1
    # frozen timing: B3a shipped "4" WITHOUT them, so the absence was the
    # B3a-期 wire face). sseReplay spreads from META_CAPABILITY_KEYS — the
    # same-source constant the v4 SSE meta frame advertises — so the
    # versions lane and the meta lane can never drift apart. No
    # runtime-dependent keys ever appear (replay-log env changes never
    # alter this advertisement).
    "4": {
        "globalSessions": True,
        "auxiliaryFilters": True,
        **META_CAPABILITY_KEYS,
        "qpImmediateFull": True,
    },
}

# §14 feature ID gating the additive ``expand`` key of the "4" face.
EXPAND_FEATURE_ID = "messages.expand.v4"


def _capabilities4(satisfied: Iterable[str] | None = None) -> dict:
    """Assemble ``capabilities["4"]`` (§3.1 static keys + §3.3 + §14).

    ``readiness`` (§3.3) is always advertised since this revision batch;
    ``expand`` (§14) is emitted **iff** ``messages.expand.v4`` ∈ the
    satisfied set — the double-sided invariant, so the two-illegal-state
    combinations (present+∉ / absent+∈) are structurally unreachable.

    ``satisfied`` defaults to the LIVE module state (dynamic global
    lookup at request time — a flip batch that reassigns
    ``readiness.SATISFIED`` propagates here with zero edits to this
    file); tests pass explicit sets to exercise the iff matrix. Unknown
    IDs are rejected inside ``readiness.readiness_payload`` before any
    emission (server never emits values outside U). Still no
    runtime/DB-derived state: readiness varies with code version only.
    """
    if satisfied is None:
        satisfied = readiness_mod.SATISFIED
    sat = frozenset(satisfied)
    caps4: dict = dict(CAPABILITIES["4"])
    caps4["readiness"] = readiness_mod.readiness_payload(sat)
    if EXPAND_FEATURE_ID in sat:
        # Same-source shape as capabilities["3"].expand (§14): the frozen
        # twelve-category ordered list plus the live fragment cap.
        caps4["expand"] = {
            "categories": EXPAND_CATEGORIES,
            "fragmentMaxBytes": settings.max_expand_response_bytes,
        }
    return caps4


@router.get("/versions")
async def versions(request: Request):
    # capabilities["3"] carries the expand capability (design-expand §6):
    # the frozen §2.2 categories plus ``fragmentMaxBytes`` read live from
    # Settings at request time, so the advertisement always matches the
    # effective per-fragment response cap of the running process.
    # capabilities["4"] = static §3.1 keys + the additive §3.3 readiness
    # gate (+ §14 expand, iff its feature ID is satisfied).
    capabilities = {
        "3": {
            **CAPABILITIES["3"],
            "expand": {
                "categories": EXPAND_CATEGORIES,
                "fragmentMaxBytes": settings.max_expand_response_bytes,
            },
        },
        "4": _capabilities4(),
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
