"""v4-contract §3.3 — ``capabilities["4"].readiness`` gate (2026-08-19 revision).
Ten-feature readiness gate for the v4 formal revision: each revision-face
feature (the §12-§17 semantics) is independently gated by its feature ID —
a feature's revised semantics are reachable **iff its ID ∈ SATISFIED**.
The aggregate ``ready`` boolean is a derived summary indicator, never a
global kill switch (owner ruling 2026-08-19, frozen).

Revision 2 (POST equivalence family) expanded the universe additively
9→10: the 10th ID ``session.post-actions.v4`` sits right after
``method.boundary.v4`` (the pair travels together — §16.3 combination
priority + §3.3 dependency implication). Transitional state (frozen §3.3
current-state note): the 10th ID is NOT yet satisfied — its implementation
batch lights it — so ``ready`` derives False in the transition, which is
the correct contract semantics, not a regression. Pre-landing 4.2.0
nine-ID payloads remain legal, non-retroactive.

Batch discipline (contract §3.3 current-state note): four of the original
nine IDs were satisfied the moment the readiness key was first advertised
(their behavior shipped with 4.0.0); the remaining five flipped to
satisfied with their implementation batches (release 4.2.0 integration
close-out) — flipping means editing ``SATISFIED`` below, nothing else.
No runtime state (DB, config, environment) ever enters the set:
readiness varies with code version only, keeping the static-capability
principle of §3.1 intact.

Server-side invariants (§3.3, frozen):

* ``REQUIRED ≡ U`` — the server always emits the full ten-ID universe;
  the deferred / non-goal boundaries of later sections are encoded by
  absence from the satisfied array, never by omitting IDs here.
* ``SATISFIED ⊆ REQUIRED`` must hold unconditionally — unknown IDs are
  REJECTED (RuntimeError), never silently ignored. Enforced at import
  time (module-level guard below) and at payload-build time
  (``readiness_payload`` validates before emitting).
* **Dependency implication (revision 2, contradiction ⑦)**:
  ``session.post-actions.v4 ∈ SATISFIED ⇒ method.boundary.v4 ∈
  SATISFIED``. A violating set fails construction (import-time guard) and
  emission (``readiness_payload``) — the server structurally never emits
  a ⑦-violating discovery payload.
* Normalization ``f(A)`` = dedupe → UTF-8 byte-order sort; both wire
  arrays are always emitted in normalized form.
* ``ready ⇔ f(REQUIRED) ⊆ f(SATISFIED)`` — derived both directions,
  never flipped alone.

Flip batches reassign ``SATISFIED`` in this module only; every consumer
below reads the module globals **at call time** (function-body lookups,
no def-time default freezing), so flips propagate wire-wide with zero
edits elsewhere.
"""
from __future__ import annotations

from typing import Iterable

# Contract §3.3 frozen enumeration order of the universe U (the numbered
# list; the wire form additionally normalizes via f()). Revision 2
# appended the 10th ID — session.post-actions.v4 (POST equivalence family)
# — immediately after method.boundary.v4.
REQUIRED: tuple[str, ...] = (
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

REQUIRED_SET: frozenset[str] = frozenset(REQUIRED)

# Revision-2 §3.3 dependency implication pair (contradiction ⑦): the POST
# equivalence family may only light up on top of the method-boundary face.
_POST_ACTIONS_FEATURE = "session.post-actions.v4"
_IMPLICATION_PAIR = (_POST_ACTIONS_FEATURE, "method.boundary.v4")

# The four IDs satisfied at first readiness advertisement (4.0.0-shipped
# behavior); the other five of the original nine flipped with their
# implementation batches (4.2.0 integration close-out).
#
# Revision-2 transitional state (frozen §3.3 current-state note): the
# universe is now ten IDs but session.post-actions.v4 stays OUT of
# SATISFIED until its implementation batch lights it — ready therefore
# derives False in the transition (correct semantics, not a regression).
# As before, no runtime state enters the set: readiness varies with code
# version only.
#
# Revision-2 activation close-out (2026-08-19): the POST equivalent action
# family implementation batch landed (three routes in write_groups + selector
# two-condition gate), so session.post-actions.v4 lights up — SATISFIED is
# again the full universe and ready derives True.
SATISFIED: frozenset[str] = frozenset(REQUIRED)


def normalize(ids: Iterable[str]) -> tuple[str, ...]:
    """f() — dedupe → UTF-8 byte-order sort (§3.3 normalization rule).

    Deterministic total order independent of locale/case folding; for the
    ASCII feature IDs this coincides with plain code-point ordering, but
    the byte-order key is applied explicitly per contract wording.
    """
    return tuple(sorted(set(ids), key=lambda s: s.encode("utf-8")))


def validate(satisfied: Iterable[str]) -> None:
    """Server-side guard: reject satisfied sets that are not ⊆ REQUIRED.

    Raises RuntimeError naming the offenders in normalized order — unknown
    IDs (∉ U) are never silently dropped, and non-string elements are a
    malformed set (wire arrays carry strings only). Also serves as the
    module-level invariant check for SATISFIED.
    """
    offenders = []
    for feature_id in satisfied:
        if not isinstance(feature_id, str) or feature_id not in REQUIRED_SET:
            offenders.append(feature_id if isinstance(feature_id, str)
                             else repr(feature_id))
    if offenders:
        raise RuntimeError(
            "readiness satisfied set contains unknown/malformed feature IDs "
            "(∉ required universe): " + ", ".join(normalize(map(str, offenders)))
        )


def validate_dependencies(satisfied: Iterable[str]) -> None:
    """§3.3 revision-2 dependency implication (contradiction ⑦, frozen):
    ``session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈
    satisfied``.

    A violating set fails construction — enforced here at import time
    (module-level guard below) and at payload-build time inside
    ``readiness_payload``, so the server never holds and never emits a
    ⑦-violating discovery payload (the §16.3 fourth combination cell is
    unreachable on the wire by construction).
    """
    sat = set(satisfied)
    head, tail = _IMPLICATION_PAIR
    if head in sat and tail not in sat:
        raise RuntimeError(
            "readiness satisfied set violates the §3.3 dependency "
            f"implication (contradiction ⑦): {head} ∈ satisfied requires "
            f"{tail} ∈ satisfied"
        )


def ready(
    required: Iterable[str] | None = None,
    satisfied: Iterable[str] | None = None,
) -> bool:
    """Aggregate readiness: ``f(required) ⊆ f(satisfied)`` (§3.3 formula).

    Defaults resolve the CURRENT module globals at call time (flip batches
    reassign SATISFIED and every subsequent call sees the new value).
    """
    if required is None:
        required = REQUIRED
    if satisfied is None:
        satisfied = SATISFIED
    return set(normalize(required)) <= set(normalize(satisfied))


def readiness_payload(satisfied: Iterable[str] | None = None) -> dict:
    """Build the §3.3 ReadinessGate wire object.

    Shape (fixed key order, producer-owned): ``{ready, required,
    satisfied}`` — both arrays in normalized form, ``ready`` derived by
    the frozen formula. Unknown IDs are rejected before any emission
    (the server never emits values outside U).
    """
    if satisfied is None:
        satisfied = SATISFIED
    validate(satisfied)
    validate_dependencies(satisfied)
    return {
        "ready": ready(satisfied=satisfied),
        "required": list(normalize(REQUIRED)),
        "satisfied": list(normalize(satisfied)),
    }


# Module-level guard: SATISFIED ⊆ REQUIRED must hold unconditionally AND
# the §3.3 dependency implication must hold (revision 2) — a typo'd ID or
# a ⑦-violating combination introduced by a future flip batch fails at
# import, not on the wire.
validate(SATISFIED)
validate_dependencies(SATISFIED)
