"""BUG-003 regression: ReplayLog seq burn on size_of failure.

If _size_of(payload) raises AFTER self._order += 1 / state.last_seq = seq
mutations, the seq is consumed but no entry is stored — a silent burn that
breaks seq continuity and corrupts replay.

Fix: compute size BEFORE the mutation block; on exception nothing is
mutated → exception propagates cleanly, the caller (which already handles
exceptions via the reserve→encode→append pattern) retries with the same
seq.

Reference: docs/automatic/.work/repair-plan.md Batch R3.
Probe: /tmp/opencode/repro_fi002_independent.py.
"""
from __future__ import annotations

from typing import Any

import pytest

from oc_slimapi.sse.replay_log import (
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayLog,
    ReplayResync,
)

_EPOCH = "0123456789abcdef"


class FailOnSecondSizer:
    """size_of that raises OSError on the 2nd call, simulating
    a transient storage/filesystem issue (mirrors the probe pattern)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, payload: Any) -> int:
        self.calls += 1
        if self.calls == 2:
            raise OSError("simulated size_of failure on 2nd call")
        return len(payload)


# ---------------------------------------------------------------------------
# fail-first: append#1 ok → append#2 raises → state unburned → append#3
# reuses seq 2 → replay from cursor 0 returns frames [1,2] with NO resync
# ---------------------------------------------------------------------------


def test_append_size_failure_does_not_burn_seq():
    sizer = FailOnSecondSizer()
    log = ReplayLog(epoch=_EPOCH, size_of=sizer)

    # --- append#1: succeeds (size_of call #1) ---------------------------
    e1 = log.append(GLOBAL_DOMAIN, b"frame-one")
    assert e1.seq == 1
    assert log.last_seq(GLOBAL_DOMAIN) == 1
    assert log._order == 1
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 1

    # --- append#2: size_of raises OSError (call #2) ---------------------
    with pytest.raises(OSError, match="simulated size_of failure"):
        log.append(GLOBAL_DOMAIN, b"frame-two")

    # ASSERT: no mutations leaked — seq and order are UNBURNED
    assert log.last_seq(GLOBAL_DOMAIN) == 1, "last_seq must NOT advance"
    assert log._order == 1, "_order must NOT advance"
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 1, "no entry stored"
    assert log.total_bytes > 0  # bytes from e1, unchanged

    # --- append#3: succeeds, REUSING seq 2 (no hole) -------------------
    e3 = log.append(GLOBAL_DOMAIN, b"frame-three")
    assert e3.seq == 2, f"expected seq 2 (reused), got {e3.seq}"
    assert log.last_seq(GLOBAL_DOMAIN) == 2
    assert log._order == 2
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 2

    # --- replay from cursor 0: must return [1,2] with NO resync --------
    outcome = log.replay(GLOBAL_DOMAIN, after_seq=0, epoch=_EPOCH)
    assert isinstance(outcome, ReplayFrames), (
        f"expected ReplayFrames, got {type(outcome).__name__}: {outcome}"
    )
    seqs = [e.seq for e in outcome.entries]
    assert seqs == [1, 2], f"expected seqs [1,2], got {seqs}"
