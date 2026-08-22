"""4.11.0 Phase A / A1 (P3): readiness flag ``sessions.details.v4``.

Frozen (plan §2 A1): REQUIRED grows additively 10 → 11 with the new ID
slotted right after ``session.post-actions.v4``. ``SATISFIED =
frozenset(REQUIRED)`` auto-includes it (default ready stays True). NO new
dependency implication — ``validate_dependencies()`` is untouched. The
normalize invariants (dedupe → UTF-8 byte-order sort) are unchanged.
"""

from __future__ import annotations

from oc_slimapi import readiness

NEW_FEATURE_ID = "sessions.details.v4"
POST_ACTIONS_FEATURE = "session.post-actions.v4"
BOUNDARY_FEATURE = "method.boundary.v4"


def test_required_contains_new_id_slotted_after_post_actions():
    assert NEW_FEATURE_ID in readiness.REQUIRED
    idx_new = readiness.REQUIRED.index(NEW_FEATURE_ID)
    idx_post = readiness.REQUIRED.index(POST_ACTIONS_FEATURE)
    assert idx_new == idx_post + 1


def test_required_universe_now_eleven_unique_ids():
    assert len(readiness.REQUIRED) == 11
    assert len(set(readiness.REQUIRED)) == 11
    assert readiness.REQUIRED_SET == frozenset(readiness.REQUIRED)


def test_satisfied_auto_includes_new_id_and_ready_true():
    assert readiness.SATISFIED == readiness.REQUIRED_SET
    assert NEW_FEATURE_ID in readiness.SATISFIED
    assert readiness.ready(readiness.SATISFIED) is True


def test_default_payload_carries_new_id_ready_true():
    payload = readiness.readiness_payload(readiness.SATISFIED)
    assert payload["ready"] is True
    assert NEW_FEATURE_ID in payload["required"]
    assert NEW_FEATURE_ID in payload["satisfied"]
    # both wire arrays stay in normalized (byte-order) form with 11 IDs
    assert len(payload["required"]) == 11
    assert payload["required"] == sorted(
        payload["required"], key=lambda s: s.encode("utf-8"))
    assert payload["required"] == payload["satisfied"]


def test_normalize_byte_order_sessions_details_sorts_after_session_dot_ids():
    """UTF-8 byte order: ``session.<x>`` < ``sessions.details.v4`` because
    the 8th byte ``.`` (0x2E) sorts before ``s`` (0x73) — the new ID is the
    LAST element of the normalized universe."""
    normalized = readiness.normalize(readiness.REQUIRED)
    assert normalized[-1] == NEW_FEATURE_ID
    assert readiness.normalize(
        (NEW_FEATURE_ID, "session.single.projection.v4",
         "session.post-actions.v4")) == (
        "session.post-actions.v4", "session.single.projection.v4",
        NEW_FEATURE_ID)


def test_no_new_dependency_implication():
    """A1 adds the ID with NO dependency edge: a satisfied set that has the
    new ID absent while every legacy implication holds must stay legal
    (ready=False — simply unimplemented), and the module-level guard
    (validate_dependencies(SATISFIED)) keeps passing untouched."""
    # legacy implications still enforced (validate_dependencies raises
    # RuntimeError on the ⑦ violation)
    violating = readiness.REQUIRED_SET - {BOUNDARY_FEATURE}
    try:
        readiness.validate_dependencies(violating)
    except RuntimeError:
        pass
    else:  # pragma: no cover - the legacy ⑦ implication must still fire
        raise AssertionError("legacy post-actions→boundary implication lost")
    # the new ID alone adds no implication: SATISFIED minus ONLY the new ID
    # is a legal (ready=False) set
    without_new = readiness.REQUIRED_SET - {NEW_FEATURE_ID}
    readiness.validate(without_new)
    readiness.validate_dependencies(without_new)
    # NB: ready()'s FIRST positional is `required` — pass satisfied by kw.
    assert readiness.ready(satisfied=without_new) is False
