"""``GET /slimapi/versions`` — v4-contract §3 discovery endpoint.

Producer-owned shape (consumers MUST ignore unknown fields for forward
compat; this endpoint never rejects requests over unknown anything — it takes
no parameters at all). Exempt from the selector judgement (§2): reachable
without any ``v``, so a client can discover before it knows which versions
exist. Non-GET → 405 + ``Allow: GET`` (enforced by the selector middleware
with priority over everything).

Version-window narrowing (2026-08-21, shipped 4.8.0): ``available == [4]``,
``current == 4`` and the capability map carries ONLY the ``"4"`` face —
the ``"3"`` capability key is gone with the window collapse (the ``?v=3``
pipeline itself answers 400 ``unsupported_version`` ``supported:[4]``).
B3b advertised ``sseReplay`` (Last-Event-ID reconnect replay, §7.2)
from the same-source ``META_CAPABILITY_KEYS`` constant the v4 SSE
``slimapi.meta`` frame advertises, and ``qpImmediateFull`` is frozen as
"already true" (design-v4-qp-payload §2/§3 — zero wire change).
Capability keys remain STATIC (never vary with runtime/DB state —
replay-log configuration does not alter the advertisement, §3.1).

2026-08-19 revision batch: the ``"4"`` face gains two ADDITIVE keys —
``readiness`` (§3.3 nine-ID readiness gate, always advertised) and
``expand`` (§14, emitted iff ``messages.expand.v4`` is satisfied). Both
are assembled by ``_capabilities4`` below and the STATIC keys stay
frozen verbatim in front of them.

4.11.0 (revision five, same-batch): the static face grows two more
ADDITIVE boolean keys — ``messagesSince`` (§10.3 messages ``?since=``
forward differential, nextSince/removed response keys) and ``fileRaw``
(§19 ``GET /slimapi/file/raw``). Both ship unconditionally true with
their implementations (no single-key-on state exists); key absence =
older sidecar without the capability (§3.1 probe semantics).

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

# S-B04: current = the pinned SERVER_API_VERSION (the only admitted
# major since the window collapse); available = the accepted range,
# ascending and unique.
CURRENT_VERSION = SERVER_API_VERSION
AVAILABLE_VERSIONS: list[int] = list(
    range(ACCEPTED_CLIENT_VERSIONS[0], ACCEPTED_CLIENT_VERSIONS[1] + 1)
)

# Capability map keyed by version STRING (contract §3 shape, verbatim).
# 2026-08-21 narrowing: the "3" key is REMOVED — the capability map is
# the v4-only face (available == [4]).
CAPABILITIES: dict[str, dict] = {
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
        # 4.11.0 revision five, same-batch with their implementations
        # (§10.3 / §19): unconditional static booleans, same pattern as
        # qpImmediateFull — key absence on older sidecars = capability
        # unavailable, clients must not pre-depend.
        "messagesSince": True,
        "fileRaw": True,
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
    # 2026-08-21 narrowing: the payload is the v4-only face — the "3"
    # capability key (and its expand block) left with the version window.
    # capabilities["4"] = static §3.1 keys + the additive §3.3 readiness
    # gate (+ §14 expand, iff its feature ID is satisfied; the fragment
    # cap ``fragmentMaxBytes`` is read live from Settings at request time
    # so the advertisement always matches the effective per-fragment
    # response cap of the running process).
    capabilities = {"4": _capabilities4()}
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
