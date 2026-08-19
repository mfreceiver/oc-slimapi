"""v4-contract §3.3 — ``capabilities["4"].readiness`` gate (2026-08-19 revision).

Nine-feature readiness gate for the v4 formal revision: each revision-face
feature (the §12-§17 semantics) is independently gated by its feature ID —
a feature's revised semantics are reachable **iff its ID ∈ SATISFIED**.
The aggregate ``ready`` boolean is a derived summary indicator, never a
global kill switch (owner ruling 2026-08-19, frozen).

Batch discipline (contract §3.3 current-state note): four of the nine IDs
are satisfied the moment the readiness key is first advertised (their
behavior shipped with 4.0.0); the remaining five flip to satisfied by the
implementation batch that lands them — flipping means editing ``SATISFIED``
below, nothing else. No runtime state (DB, config, environment) ever
enters the set: readiness varies with code version only, keeping the
static-capability principle of §3.1 intact. All five have now flipped
(release 4.2.0 integration close-out): ``SATISFIED`` carries the full
universe and ``ready`` derives true.

Server-side invariants (§3.3, frozen):

* ``REQUIRED ≡ U`` — the server always emits the full nine-ID universe;
  the deferred / non-goal boundaries of later sections are encoded by
  absence from the satisfied array, never by omitting IDs here.
* ``SATISFIED ⊆ REQUIRED`` must hold unconditionally — unknown IDs are
  REJECTED (RuntimeError), never silently ignored. Enforced at import
  time (module-level guard below) and at payload-build time
  (``readiness_payload`` validates before emitting).
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
# list; the wire form additionally normalizes via f()).
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
)

REQUIRED_SET: frozenset[str] = frozenset(REQUIRED)

# The four IDs satisfied at first readiness advertisement (4.0.0-shipped
# behavior). The remaining five flip as their implementation batches land:
#   session.single.projection.v4 / messages.expand.v4 / providers.redacted.v4
#   / representation.vary.v4 / method.boundary.v4
#
# 2026-08-19 integration close-out (release 4.2.0): all five revision-face
# batches have landed and their per-feature gates are wired
# (read_groups / messages / write_groups / sessions) — SATISFIED now
# carries the full nine-ID universe and the derived ``ready`` is true.
# As before, no runtime state enters the set: readiness varies with code
# version only.
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
    return {
        "ready": ready(satisfied=satisfied),
        "required": list(normalize(REQUIRED)),
        "satisfied": list(normalize(satisfied)),
    }


# Module-level guard: SATISFIED ⊆ REQUIRED must hold unconditionally — a
# typo'd ID introduced by a future flip batch fails at import, not on the
# wire.
validate(SATISFIED)
