"""v4-contract §3.3 — ``capabilities["4"].readiness`` gate (2026-08-19 revision).

Contract anchors (docs/specs/v4-contract.md):

* **§3.3** — nine-feature ID universe U in frozen enumeration order;
  normalization ``f(A)`` = dedupe → UTF-8 byte-order sort (both wire arrays
  are emitted normalized); ``ready ⇔ f(required) ⊆ f(satisfied)`` derived
  both directions (never flipped alone); unknown IDs (∉ U) rejected, never
  silently ignored; ``SATISFIED ⊆ REQUIRED`` module-level invariant. Four
  IDs were satisfied at first advertisement (4.0.0 behavior); the
  4.2.0 integration close-out flipped the remaining five — the default
  SATISFIED now carries the full universe and ``ready`` derives true
  (gate-closed states are reproduced in tests via monkeypatch).
* **§3.1 + §14** — ``capabilities["4"]`` additive extension keys:
  ``readiness`` (this batch) and ``expand`` (shape
  ``{categories, fragmentMaxBytes}``, emitted **iff**
  ``messages.expand.v4 ∈ satisfied``). Four-combination exhaustive
  invariant: ① present+∈ and ② absent+∉ are the only legal states; the
  server side structurally never produces ③ present+∉ or ④ absent+∈, nor
  ``expand`` without ``readiness``.
* **§0.5 / §3.1** — the v3 face stays byte-unchanged
  (``capabilities["3"]`` regression lock, including its expand block); the
  four STATIC v4 keys keep their frozen shape and order with ``readiness``
  appended after them.
"""
from __future__ import annotations

from itertools import combinations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi import __version__
from oc_slimapi import readiness
from oc_slimapi.config import settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import versions
from oc_slimapi.routes import versions as versions_mod
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.traffic import EXPAND_CATEGORIES

# §3.3 frozen enumeration order of the universe U (contract numbered list).
CONTRACT_REQUIRED_ORDER = (
    "selector.v4",
    "session.list.global.v4",
    "session.single.projection.v4",
    "messages.expand.v4",
    "providers.redacted.v4",
    "events.global.replay.v4",
    "events.token.replay.v4",
    "representation.vary.v4",
    "method.boundary.v4",
)

# §3.3 normalization f(): dedupe → UTF-8 byte-order sort of U.
CONTRACT_REQUIRED_NORMALIZED = (
    "events.global.replay.v4",
    "events.token.replay.v4",
    "messages.expand.v4",
    "method.boundary.v4",
    "providers.redacted.v4",
    "representation.vary.v4",
    "selector.v4",
    "session.list.global.v4",
    "session.single.projection.v4",
)

# The four IDs satisfied at first readiness advertisement (4.0.0 behavior).
CONTRACT_INITIAL_SATISFIED = frozenset({
    "selector.v4",
    "session.list.global.v4",
    "events.global.replay.v4",
    "events.token.replay.v4",
})
CONTRACT_INITIAL_SATISFIED_NORMALIZED = (
    "events.global.replay.v4",
    "events.token.replay.v4",
    "selector.v4",
    "session.list.global.v4",
)

EXPAND_FEATURE_ID = "messages.expand.v4"


def _build_app() -> FastAPI:
    app = FastAPI(title="versions-readiness-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.include_router(versions.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


async def _get_caps() -> dict:
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        return body["capabilities"]


# ---------------------------------------------------------------------------
# §3.3 — universe U: nine IDs, frozen enumeration order
# ---------------------------------------------------------------------------


def test_required_universe_nine_ids_frozen_order():
    """REQUIRED ≡ U in the contract's numbered enumeration order (§3.3)."""
    assert readiness.REQUIRED == CONTRACT_REQUIRED_ORDER
    assert len(readiness.REQUIRED) == 9


def test_required_universe_unique_strings():
    assert len(set(readiness.REQUIRED)) == 9
    assert all(isinstance(i, str) and i for i in readiness.REQUIRED)
    assert readiness.REQUIRED_SET == frozenset(CONTRACT_REQUIRED_ORDER)


def test_required_matches_contract_normalized_form():
    """The universe under f() matches the §3.3 byte-sorted wire form."""
    assert readiness.normalize(readiness.REQUIRED) == CONTRACT_REQUIRED_NORMALIZED


# ---------------------------------------------------------------------------
# §3.3 — normalization f(): dedupe → UTF-8 byte-order sort
# ---------------------------------------------------------------------------


def test_normalize_dedupes():
    ids = ["selector.v4", "selector.v4", "selector.v4"]
    assert readiness.normalize(ids) == ("selector.v4",)


def test_normalize_sorts_utf8_byte_order():
    # Byte order is NOT case-insensitive/locale order: "B" (0x42) < "a" (0x61).
    assert readiness.normalize(("a.v4", "B.v4", "b.v4")) == ("B.v4", "a.v4", "b.v4")
    # Contract IDs under f() land in the frozen byte-sorted order.
    shuffled = tuple(reversed(CONTRACT_REQUIRED_ORDER))
    assert readiness.normalize(shuffled) == CONTRACT_REQUIRED_NORMALIZED


def test_normalize_accepts_any_iterable_and_is_idempotent():
    once = readiness.normalize(CONTRACT_REQUIRED_ORDER)
    assert readiness.normalize(list(once)) == once
    assert readiness.normalize(frozenset(once)) == once
    assert readiness.normalize(tuple(once)) == once


def test_normalize_empty():
    assert readiness.normalize(()) == ()


# ---------------------------------------------------------------------------
# §3.3 — current SATISFIED (4.2.0 close-out: all nine) + module invariants
# ---------------------------------------------------------------------------


def test_satisfied_current_state_all_nine():
    """§3.3 final state (4.2.0 integration close-out): the five revision
    batches all landed and their per-feature gates are wired — SATISFIED
    carries the full universe, nothing pending. The historical 4.0.0 four
    remain ⊆ SATISFIED (flips only ever ADD)."""
    assert readiness.SATISFIED == readiness.REQUIRED_SET
    assert readiness.REQUIRED_SET - readiness.SATISFIED == frozenset()
    assert CONTRACT_INITIAL_SATISFIED <= readiness.SATISFIED


def test_module_invariant_satisfied_subset_of_required():
    """Server-side guard: SATISFIED ⊆ REQUIRED holds unconditionally —
    unknown IDs are rejected, never silently ignored (§3.3). Import-time
    enforcement already ran (the module imported cleanly), so this also
    proves the initial set passes the guard."""
    assert readiness.SATISFIED <= readiness.REQUIRED_SET
    # The guard itself accepts every legal subset...
    for k in range(10):
        for subset in combinations(readiness.REQUIRED, k):
            readiness.validate(frozenset(subset))  # no raise
    # ...and rejects anything outside U, naming the offender.
    with pytest.raises(RuntimeError, match="bogus\.v4"):
        readiness.validate(frozenset({"selector.v4", "bogus.v4"}))
    # The empty set is a LEGAL subset (k=0 in the loop above already proves
    # validate() accepts it): it is the all-unsatisfied state, ready=false.
    # §3.3 only mandates rejection of IDs outside U — not of empty sets.
    readiness.validate(frozenset())


def test_validate_rejects_non_string_elements():
    with pytest.raises(RuntimeError):
        readiness.validate(frozenset({"selector.v4", 42}))


# ---------------------------------------------------------------------------
# §3.3 — ready derivation: ready ⇔ f(required) ⊆ f(satisfied), both ways
# ---------------------------------------------------------------------------


def test_ready_true_with_all_nine():
    """Current state (4.2.0 close-out): all nine satisfied → aggregate
    ready=True (an indicator only — per-ID gating is what matters, §3.3)."""
    assert readiness.ready() is True


def test_ready_true_only_when_all_nine_satisfied():
    assert readiness.ready(satisfied=readiness.REQUIRED_SET) is True
    # Dropping ANY single ID breaks the aggregate (exhaustive over 9).
    for missing in readiness.REQUIRED:
        satisfied = readiness.REQUIRED_SET - {missing}
        assert readiness.ready(satisfied=satisfied) is False, missing


def test_ready_derivation_exhaustive_all_subsets():
    """Both directions of the frozen formula, exhausted over all 2^9
    subsets of U (payload-level: the wire `ready` equals the subset
    judgement for every possible satisfied set)."""
    universe = readiness.REQUIRED
    for k in range(len(universe) + 1):
        for subset in combinations(universe, k):
            sat = frozenset(subset)
            payload = readiness.readiness_payload(sat)
            expected = readiness.REQUIRED_SET <= sat
            assert payload["ready"] is expected, sat
            assert readiness.ready(satisfied=sat) is expected, sat


def test_ready_explicit_required_argument():
    """ready() also accepts an explicit `required` (formula generalizes;
    the wire always uses the full universe)."""
    two = ("selector.v4", "session.list.global.v4")
    assert readiness.ready(required=two, satisfied={"selector.v4"}) is False
    assert readiness.ready(required=two,
                           satisfied={"selector.v4", "session.list.global.v4"}) is True


# ---------------------------------------------------------------------------
# §3.3 — ReadinessGate payload shape {ready, required, satisfied}
# ---------------------------------------------------------------------------


def test_readiness_payload_shape_current_state():
    payload = readiness.readiness_payload()
    assert list(payload.keys()) == ["ready", "required", "satisfied"]
    assert payload["ready"] is True
    assert payload["required"] == list(CONTRACT_REQUIRED_NORMALIZED)
    assert payload["satisfied"] == list(CONTRACT_REQUIRED_NORMALIZED)
    assert all(isinstance(i, str) for i in payload["required"])
    assert all(isinstance(i, str) for i in payload["satisfied"])


def test_readiness_payload_normalizes_arbitrary_input():
    """Unsorted / duplicated input still emits normalized arrays (§3.3:
    the server always emits both arrays in normalized form)."""
    payload = readiness.readiness_payload(
        ["selector.v4", "selector.v4", "events.token.replay.v4"]
    )
    assert payload["satisfied"] == ["events.token.replay.v4", "selector.v4"]
    assert payload["required"] == list(CONTRACT_REQUIRED_NORMALIZED)


def test_readiness_payload_rejects_unknown_ids():
    with pytest.raises(RuntimeError, match="unknown"):
        readiness.readiness_payload({"selector.v4", "not.in.universe.v4"})


def test_readiness_payload_reads_live_module_state():
    """Default arguments read the CURRENT module globals at call time —
    flip batches that reassign SATISFIED propagate without touching any
    caller (no def-time default freezing)."""
    original = readiness.SATISFIED
    try:
        readiness.SATISFIED = readiness.REQUIRED_SET
        assert readiness.ready() is True
        assert readiness.readiness_payload()["ready"] is True
        assert readiness.readiness_payload()["satisfied"] == \
            list(CONTRACT_REQUIRED_NORMALIZED)
    finally:
        readiness.SATISFIED = original


# ---------------------------------------------------------------------------
# §14 — expand key emission: present iff messages.expand.v4 ∈ satisfied
# (four-combination exhaustive; server structurally never produces ③/④)
# ---------------------------------------------------------------------------


def test_caps4_helper_expand_iff_exhaustive_all_subsets():
    """For EVERY subset S ⊆ U: `expand` present ⇔ messages.expand.v4 ∈ S
    (combinations ①/② legal), ③ present+∉ and ④ absent+∈ are structurally
    unreachable on the server side; `readiness` is always present (expand
    without readiness = contradiction, §3.3)."""
    universe = readiness.REQUIRED
    for k in range(len(universe) + 1):
        for subset in combinations(universe, k):
            sat = frozenset(subset)
            caps4 = versions_mod._capabilities4(sat)
            assert ("expand" in caps4) == (EXPAND_FEATURE_ID in sat), sat
            assert "readiness" in caps4, sat


def test_caps4_helper_expand_shape_when_satisfied():
    """Combination ①: with messages.expand.v4 ∈ satisfied the expand key
    carries the §14 shape — the twelve-category ordered list from the
    single source of truth plus the live fragment cap."""
    sat = readiness.REQUIRED_SET
    caps4 = versions_mod._capabilities4(sat)
    assert list(caps4.keys()) == [
        "globalSessions", "auxiliaryFilters", "sseReplay",
        "qpImmediateFull", "readiness", "expand",
    ]
    assert caps4["expand"]["categories"] == EXPAND_CATEGORIES
    assert len(EXPAND_CATEGORIES) == 12
    assert caps4["expand"]["fragmentMaxBytes"] == settings.max_expand_response_bytes


def test_caps4_helper_current_state_expand_emitted():
    """Combination ① as the DEFAULT state since the 4.2.0 close-out:
    messages.expand.v4 ∈ SATISFIED → the expand key is present, same-source
    shape as capabilities["3"] (gate-closed absence is covered by the
    exhaustive iff tests and the wire-level flip test below)."""
    caps4 = versions_mod._capabilities4()
    assert list(caps4.keys()) == [
        "globalSessions", "auxiliaryFilters", "sseReplay",
        "qpImmediateFull", "readiness", "expand",
    ]
    assert "expand" in caps4
    assert caps4["expand"]["categories"] == EXPAND_CATEGORIES
    assert len(EXPAND_CATEGORIES) == 12
    assert caps4["expand"]["fragmentMaxBytes"] == settings.max_expand_response_bytes


def test_caps4_helper_gate_off_expand_absent():
    """Combination ② reproduced via monkeypatch: excluding the expand
    feature from SATISFIED removes the key (readiness stays)."""
    caps4 = versions_mod._capabilities4(
        readiness.REQUIRED_SET - {EXPAND_FEATURE_ID})
    assert list(caps4.keys()) == [
        "globalSessions", "auxiliaryFilters", "sseReplay",
        "qpImmediateFull", "readiness",
    ]
    assert "expand" not in caps4


def test_caps4_helper_rejects_unknown_ids():
    with pytest.raises(RuntimeError):
        versions_mod._capabilities4(frozenset({"selector.v4", "ghost.v4"}))


# ---------------------------------------------------------------------------
# Wire level: GET /slimapi/versions
# ---------------------------------------------------------------------------


async def test_versions_caps4_readiness_emitted():
    """The wire emits the readiness gate; the payload equals the
    module-derived shape byte-for-byte, the four STATIC keys keep their
    frozen order in front of it, and — since the 4.2.0 close-out — the
    §14 expand block follows (default SATISFIED is the full universe)."""
    caps = await _get_caps()
    caps4 = caps["4"]
    assert list(caps4.keys()) == [
        "globalSessions", "auxiliaryFilters", "sseReplay",
        "qpImmediateFull", "readiness", "expand",
    ]
    assert caps4["readiness"] == readiness.readiness_payload()
    assert caps4["readiness"] == {
        "ready": True,
        "required": list(CONTRACT_REQUIRED_NORMALIZED),
        "satisfied": list(CONTRACT_REQUIRED_NORMALIZED),
    }


async def test_versions_caps4_static_four_keys_byte_unchanged():
    """§3.1: the four STATIC capability keys and their values are frozen
    verbatim (readiness is purely additive on top)."""
    caps = await _get_caps()
    caps4 = caps["4"]
    assert caps4["globalSessions"] is True
    assert caps4["auxiliaryFilters"] is True
    assert caps4["sseReplay"] is True
    assert caps4["qpImmediateFull"] is True


async def test_versions_caps3_face_zero_change():
    """§0.5 v3 freeze: capabilities["3"] keeps its exact terminal shape
    (including the expand block) — the revision touches only the "4"
    face."""
    caps = await _get_caps()
    assert caps["3"] == {
        "envelope": ["messages", "sessions"],
        "directoryQuery": True,
        "versionHeaderOptional": True,
        "writeRoutes": True,
        "readRoutes": [
            "file", "vcs", "find", "providers",
            "sessionSingle", "activeSessions", "globalHealth",
        ],
        "expand": {
            "categories": EXPAND_CATEGORIES,
            "fragmentMaxBytes": settings.max_expand_response_bytes,
        },
    }


async def test_versions_wire_expand_present_current_state():
    """Since the 4.2.0 close-out the default wire carries capabilities
    ["4"].expand, byte-equal to the capabilities["3"] expand block
    (same-source single truth: EXPAND_CATEGORIES + settings cap)."""
    caps = await _get_caps()
    assert "expand" in caps["4"]
    assert caps["4"]["expand"] == caps["3"]["expand"]


async def test_versions_route_reads_live_satisfied(monkeypatch):
    """Flip-batch propagation: the route reads the module-level SATISFIED
    at request time (no import-time freezing), so a future batch that
    reassigns it changes the wire with zero route edits."""
    monkeypatch.setattr(readiness, "SATISFIED", readiness.REQUIRED_SET)
    caps = await _get_caps()
    caps4 = caps["4"]
    assert caps4["readiness"]["ready"] is True
    assert caps4["readiness"]["satisfied"] == list(CONTRACT_REQUIRED_NORMALIZED)
    # Combination ① at wire level, same-source shape as capabilities["3"]:
    assert caps4["expand"] == caps["3"]["expand"]
    assert caps4["expand"]["categories"] == EXPAND_CATEGORIES

    # And the transitional legal state (②) at wire level: five satisfied
    # but not the expand feature → readiness present, expand absent.
    monkeypatch.setattr(
        readiness, "SATISFIED", readiness.REQUIRED_SET - {EXPAND_FEATURE_ID}
    )
    caps = await _get_caps()
    assert caps["4"]["readiness"]["ready"] is False
    assert "expand" not in caps["4"]
    assert "readiness" in caps["4"]


async def test_versions_payload_identical_across_views():
    """Discovery is selector-exempt: ?v=3, ?v=4 and no selector carry the
    exact same payload — the additive readiness key lands in the shared
    discovery face, not in any v3-scoped route behavior (§2 exemption,
    §3.1 consumers ignore unknown keys)."""
    async with _client(_build_app()) as client:
        base = (await client.get("/slimapi/versions")).json()
        for query in ("?v=3", "?v=4"):
            resp = await client.get(f"/slimapi/versions{query}")
            assert resp.status_code == 200
            assert resp.json() == base
        assert base["capabilities"]["4"]["readiness"]["ready"] is True
        assert "expand" in base["capabilities"]["4"]
        assert base["sidecarVersion"] == __version__


async def test_versions_route_rejects_unknown_satisfied_never_emits():
    """Server-side unknown-ID rejection at the assembly boundary: the
    caps4 builder refuses to emit a payload with an ID outside U (the
    wire can never carry ∉ U values — §3.3 'server must not emit')."""
    with pytest.raises(RuntimeError):
        async with _client(_build_app()) as client:
            # Build the app first; the rejection happens during assembly,
            # proven directly on the helper here (route path shares it).
            caps4 = versions_mod._capabilities4(frozenset({"ghost.v4"}))
            assert caps4  # unreachable
