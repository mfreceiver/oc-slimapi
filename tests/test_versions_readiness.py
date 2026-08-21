"""v4-contract §3.3 — ``capabilities["4"].readiness`` gate (2026-08-19 revision).

Contract anchors (docs/specs/v4-contract.md):

* **§3.3** — ten-feature ID universe U in frozen enumeration order
  (revision 2 expanded U 9→10 additively with the 10th ID
  ``session.post-actions.v4``, slotted after ``method.boundary.v4``);
  normalization ``f(A)`` = dedupe → UTF-8 byte-order sort (both wire arrays
  are emitted normalized); ``ready ⇔ f(required) ⊆ f(satisfied)`` derived
  both directions (never flipped alone); unknown IDs (∉ U) rejected, never
  silently ignored; ``SATISFIED ⊆ REQUIRED`` module-level invariant. Four
  IDs were satisfied at first advertisement (4.0.0 behavior); the 4.2.0
  integration close-out flipped the remaining five of the original nine.
  **Revision-2 activation (post-integration-batch default): the
  implementation batch lit ``session.post-actions.v4`` — SATISFIED now
  carries the full ten-ID universe and the derived ``ready`` is True again.
  The transitional nine-of-ten shape (ready:false) is preserved as a
  construction-level lock below (explicit set, no global-state dependency).**
* **§3.3 revision-2 dependency implication (contradiction ⑦)** —
  ``session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied``;
  a violating set is rejected at construction (module guard) and at
  payload-build time (the server structurally NEVER emits a ⑦-violating
  discovery payload — supply-side defense).
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

B12 (2026-08-21 v4 自包含 golden 化): the three former dynamic-equivalence
sites (expand-block ×2, three-view payload invariance) are literalized
against the module goldens ``EXPAND_BLOCK_GOLDEN`` /
``VERSIONS_PAYLOAD_GOLDEN`` — see the ``B12`` block comment above them;
the v3 halves survive only as annotated guard nets.
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

# §3.3 frozen enumeration order of the universe U (contract numbered list;
# revision 2 appended the 10th ID after method.boundary.v4).
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
    "session.post-actions.v4",
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
    "session.post-actions.v4",
    "session.single.projection.v4",
)

# Revision-2 pair (§3.3 implication / §16.3 combination priority).
CONTRACT_POST_ACTIONS_FEATURE = "session.post-actions.v4"
CONTRACT_BOUNDARY_FEATURE = "method.boundary.v4"

# The transitional SATISFIED (revision 2, §3.3 current-state note): the
# original nine IDs — U minus the 10th — in normalized wire form. Kept as
# the construction-level transitional lock (the integration batch has since
# lit the 10th; the global default is the full activated universe).
TRANSITIONAL_SATISFIED_NORMALIZED = (
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

# --- B12 v4 自包含 golden（2026-08-21 从实际 ?v=4 响应忠实转录） -------------
#
# 本文件原有三处「动态对照」等价断言（caps["4"].expand == caps["3"].expand
# ×2、versions 三视图 == 无 selector 兄弟响应）——B12 改造后 v4 期望字面钉
# 在此处，求值不再依赖任何 v3/兄弟请求路径；原等价关系仅以注记的
# v3 守护网形式保留（三分处置②，Phase 4 v3 面拆除前）。
#
# 转录口径：parsed 形状钉（== 比较，键序不敏感；caps4 键序由
# test_versions_caps4_readiness_emitted 单独锁）。两处非 v3 派生值按
# sidecar 自身单一源引用——fragmentMaxBytes 读全局 settings 旋钮（路由
# 同源）、sidecarVersion 读包版本 __version__——均非 v3 wire 路径。

EXPAND_BLOCK_GOLDEN = {
    "categories": [
        "info_summary_diffs", "part_text", "part_reasoning",
        "part_state_output", "part_state_error",
        "part_state_input_full", "part_state_metadata_full",
        "part_state_attachments", "part_url", "part_source",
        "part_snapshot", "compaction_full",
    ],
    "fragmentMaxBytes": settings.max_expand_response_bytes,
}

VERSIONS_PAYLOAD_GOLDEN = {
    "current": 4,
    "available": [3, 4],
    "capabilities": {
        "3": {                                # v3 terminal face（§0.5 冻结）
            "envelope": ["messages", "sessions"],
            "directoryQuery": True,
            "versionHeaderOptional": True,
            "writeRoutes": True,
            "readRoutes": [
                "file", "vcs", "find", "providers",
                "sessionSingle", "activeSessions", "globalHealth",
            ],
            "expand": EXPAND_BLOCK_GOLDEN,
        },
        "4": {                                # v4 differential face（§3.1）
            "globalSessions": True,
            "auxiliaryFilters": True,
            "sseReplay": True,
            "qpImmediateFull": True,
            "readiness": {
                "ready": True,
                "required": list(CONTRACT_REQUIRED_NORMALIZED),
                "satisfied": list(CONTRACT_REQUIRED_NORMALIZED),
            },
            "expand": EXPAND_BLOCK_GOLDEN,
        },
    },
    "sidecarVersion": __version__,
}


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
# §3.3 — universe U: ten IDs, frozen enumeration order (revision 2: 9→10)
# ---------------------------------------------------------------------------


def test_required_universe_ten_ids_frozen_order():
    """REQUIRED ≡ U in the contract's numbered enumeration order (§3.3);
    revision 2 appends ``session.post-actions.v4`` as the 10th ID,
    slotted right after ``method.boundary.v4`` (the implication /
    combination-priority pair travels together)."""
    assert readiness.REQUIRED == CONTRACT_REQUIRED_ORDER
    assert len(readiness.REQUIRED) == 10
    assert readiness.REQUIRED[9] == CONTRACT_POST_ACTIONS_FEATURE
    assert readiness.REQUIRED[8] == CONTRACT_BOUNDARY_FEATURE


def test_required_universe_unique_strings():
    assert len(set(readiness.REQUIRED)) == 10
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
# §3.3 — current SATISFIED (revision-2 ACTIVATED default) + module invariants
# ---------------------------------------------------------------------------


def test_satisfied_current_state_activated_full_ten():
    """§3.3 revision-2 close-out: the implementation batch lit
    ``session.post-actions.v4`` — SATISFIED now carries the FULL ten-ID
    universe and the derived ``ready`` is True (activation is the shipped
    default; flips only ever ADD, so the 4.2.0 nine and the historical
    4.0.0 four remain ⊆ SATISFIED)."""
    assert readiness.SATISFIED == readiness.REQUIRED_SET
    assert CONTRACT_POST_ACTIONS_FEATURE in readiness.SATISFIED
    assert CONTRACT_BOUNDARY_FEATURE in readiness.SATISFIED
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
    with pytest.raises(RuntimeError, match="bogus\\.v4"):
        readiness.validate(frozenset({"selector.v4", "bogus.v4"}))
    # The empty set is a LEGAL subset (k=0 in the loop above already proves
    # validate() accepts it): it is the all-unsatisfied state, ready=false.
    # §3.3 only mandates rejection of IDs outside U — not of empty sets.
    readiness.validate(frozenset())


def test_validate_rejects_non_string_elements():
    with pytest.raises(RuntimeError):
        readiness.validate(frozenset({"selector.v4", 42}))


# ---------------------------------------------------------------------------
# §3.3 revision 2 — dependency implication (contradiction ⑦ supply side)
# ---------------------------------------------------------------------------


def test_validate_dependencies_accepts_legal_combinations():
    """§3.3 implication (frozen): post-actions ∈ satisfied ⇒ boundary ∈
    satisfied. Every combination consistent with that implication passes
    construction: the full ten-ID set (post∈∧boundary∈), the transitional
    default (post∉, boundary either), both-out, the 4.0.0-era subsets, and
    the empty set."""
    legal_sets = [
        frozenset(CONTRACT_REQUIRED_ORDER),                     # both in
        readiness.REQUIRED_SET - {CONTRACT_POST_ACTIONS_FEATURE},
        readiness.REQUIRED_SET - {CONTRACT_POST_ACTIONS_FEATURE,
                                  CONTRACT_BOUNDARY_FEATURE},   # both out
        CONTRACT_INITIAL_SATISFIED,                             # 4.0.0 era
        frozenset(),
        frozenset({CONTRACT_POST_ACTIONS_FEATURE, CONTRACT_BOUNDARY_FEATURE}),
    ]
    for sat in legal_sets:
        readiness.validate_dependencies(sat)  # no raise
        payload = readiness.readiness_payload(sat)  # emission also legal
        assert payload["required"] == list(CONTRACT_REQUIRED_NORMALIZED)


def test_validate_dependencies_rejects_post_without_boundary():
    """The violating combination — post-actions ∈ satisfied while
    boundary ∉ — fails construction (RuntimeError naming the pair): the
    server never holds or emits a ⑦-violating set (§3.3 implication,
    §16.3 marks the fourth table cell unreachable)."""
    violating = readiness.REQUIRED_SET - {CONTRACT_BOUNDARY_FEATURE}
    assert CONTRACT_POST_ACTIONS_FEATURE in violating
    with pytest.raises(RuntimeError, match="session\\.post-actions\\.v4"):
        readiness.validate_dependencies(violating)
    # Smallest witness: the pair alone, boundary dropped.
    with pytest.raises(RuntimeError):
        readiness.validate_dependencies(
            frozenset({CONTRACT_POST_ACTIONS_FEATURE}))


def test_readiness_payload_never_emits_violating_set():
    """Emission boundary (⑦ supply-side defense): readiness_payload
    refuses to BUILD a payload from a violating set — the wire can never
    carry post∈satisfied ∧ boundary∉satisfied."""
    with pytest.raises(RuntimeError, match="implication"):
        readiness.readiness_payload(
            readiness.REQUIRED_SET - {CONTRACT_BOUNDARY_FEATURE})


def test_module_guard_ran_at_import_with_legal_set():
    """The module-level guard (validate + validate_dependencies) executed
    at import time — the module imported cleanly, proving the ACTIVATED
    SATISFIED passes BOTH checks (and would have failed import otherwise)."""
    readiness.validate(readiness.SATISFIED)
    readiness.validate_dependencies(readiness.SATISFIED)


# ---------------------------------------------------------------------------
# §3.3 — ready derivation: ready ⇔ f(required) ⊆ f(satisfied), both ways
# ---------------------------------------------------------------------------


def test_ready_true_in_activated_default():
    """Activation close-out: the full ten-ID SATISFIED derives
    ``ready=True`` (an indicator only — per-ID gating is what matters,
    §3.3)."""
    assert readiness.ready() is True


def test_ready_false_for_constructed_transitional_set():
    """Construction-level transitional lock (§3.3 current-state note,
    frozen): required expanded to ten IDs, satisfied carries nine — the
    aggregate ``ready`` is therefore False. Built from an EXPLICIT set
    (no global SATISFIED dependency): the transitional semantics stays
    test-locked even though the shipped default is now the activated
    full universe."""
    transitional = readiness.REQUIRED_SET - {CONTRACT_POST_ACTIONS_FEATURE}
    assert readiness.ready(satisfied=transitional) is False
    payload = readiness.readiness_payload(transitional)
    assert payload["ready"] is False
    assert payload["satisfied"] == list(TRANSITIONAL_SATISFIED_NORMALIZED)


def test_ready_true_only_when_all_ten_satisfied():
    assert readiness.ready(satisfied=readiness.REQUIRED_SET) is True
    # Dropping ANY single ID breaks the aggregate (exhaustive over 10).
    for missing in readiness.REQUIRED:
        satisfied = readiness.REQUIRED_SET - {missing}
        assert readiness.ready(satisfied=satisfied) is False, missing


def test_ready_derivation_exhaustive_all_subsets():
    """Both directions of the frozen formula, exhausted over all 2^10
    subsets of U (payload-level: the wire `ready` equals the subset
    judgement for every LEGAL satisfied set; ⑦-violating subsets are
    refused at construction — they never reach a payload)."""
    universe = readiness.REQUIRED
    for k in range(len(universe) + 1):
        for subset in combinations(universe, k):
            sat = frozenset(subset)
            if (CONTRACT_POST_ACTIONS_FEATURE in sat
                    and CONTRACT_BOUNDARY_FEATURE not in sat):
                with pytest.raises(RuntimeError):
                    readiness.readiness_payload(sat)
                continue
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
    """Activated wire shape (revision-2 close-out): required = satisfied =
    the full ten-ID normalized universe, ready = True (derived)."""
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
    without readiness = contradiction, §3.3). Revision 2: subsets that
    violate the ⑦ implication never reach the caps builder's payload at
    all — they are refused at construction (RuntimeError)."""
    universe = readiness.REQUIRED
    for k in range(len(universe) + 1):
        for subset in combinations(universe, k):
            sat = frozenset(subset)
            if (CONTRACT_POST_ACTIONS_FEATURE in sat
                    and CONTRACT_BOUNDARY_FEATURE not in sat):
                with pytest.raises(RuntimeError):
                    versions_mod._capabilities4(sat)
                continue
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
    frozen order in front of it, and the §14 expand block follows
    (messages.expand.v4 stays satisfied). Revision-2 ACTIVATED wire:
    required = satisfied = the ten-ID universe, ready=True (the
    transitional nine-of-ten shape is locked construction-level above)."""
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
    ["4"].expand — same-source single truth (EXPAND_CATEGORIES + settings
    cap).

    B12: the v4 expand block is pinned to the literal golden
    (self-contained); the former ``caps["4"]["expand"] ==
    caps["3"]["expand"]`` equivalence survives only as the annotated v3
    guard net below."""
    caps = await _get_caps()
    assert "expand" in caps["4"]
    assert caps["4"]["expand"] == EXPAND_BLOCK_GOLDEN
    # v3 守护网（Phase 4 拆除前保留）："3" 面自身 expand 块同落同一 golden
    # （原「4 面 == 3 面」等价断言；v4 已独立字面钉）。
    assert caps["3"]["expand"] == EXPAND_BLOCK_GOLDEN


async def test_versions_route_reads_live_satisfied(monkeypatch):
    """Flip-batch propagation: the route reads the module-level SATISFIED
    at request time (no import-time freezing), so a future batch that
    reassigns it changes the wire with zero route edits."""
    monkeypatch.setattr(readiness, "SATISFIED", readiness.REQUIRED_SET)
    caps = await _get_caps()
    caps4 = caps["4"]
    assert caps4["readiness"]["ready"] is True
    assert caps4["readiness"]["satisfied"] == list(CONTRACT_REQUIRED_NORMALIZED)
    # Combination ① at wire level — B12: v4 expand block literally pinned
    # (was ``caps4["expand"] == caps["3"]["expand"]``).
    assert caps4["expand"] == EXPAND_BLOCK_GOLDEN
    # v3 守护网（Phase 4 拆除前保留）："3" 面 expand 同落同一 golden。
    assert caps["3"]["expand"] == EXPAND_BLOCK_GOLDEN
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
    """Discovery is selector-exempt: no selector, ``?v=3`` and ``?v=4``
    carry the exact same payload — the additive readiness key lands in the
    shared discovery face, not in any v3-scoped route behavior (§2
    exemption, §3.1 consumers ignore unknown keys).

    B12: the comparison basis is the literal module golden
    (``VERSIONS_PAYLOAD_GOLDEN``) — transitive invariance: each view is
    independently pinned, none derives its expectation from a live
    sibling response anymore; the ``?v=3`` leg doubles as the v3 guard
    net. ready/expand/sidecarVersion assertions of the former tail are
    all implied by the golden equality."""
    async with _client(_build_app()) as client:
        base = (await client.get("/slimapi/versions")).json()
        assert base == VERSIONS_PAYLOAD_GOLDEN
        for query in ("?v=3", "?v=4"):
            resp = await client.get(f"/slimapi/versions{query}")
            assert resp.status_code == 200
            assert resp.json() == VERSIONS_PAYLOAD_GOLDEN


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
