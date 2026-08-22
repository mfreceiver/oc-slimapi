"""4.11.0 Phase A / A4: Settings presets for the later phases (B/C) +
startup memory-budget validation extension.

Frozen (plan §2 A4 / §4.1):

* New knobs land NOW with validation and frozen defaults — no consumer in
  Phase A (B: since_cache; C: file_raw envelope cap).
* ``A_AMP = 4`` is a module-level constant (per-permit amplification:
  envelope + parsed/decoded + response + gzip candidate).
* Budget extension (aggregate 口径 unchanged at 512/576 MiB):

      effective_cap   = min(max_response_bytes, file_raw_max_envelope_bytes)
      file_raw_bound  = (A_AMP + 1) × max_transforms × effective_cap
      transform_bound = max(existing_transform_bound, file_raw_bound)
      raw_plus        = raw_fetch_max_bytes + transform_bound

  The file-raw worker shares the SAME W transform permits, so the two
  transform-side bounds take max() — never a double addition.
"""

from __future__ import annotations

import pytest

from oc_slimapi import config as config_mod
from oc_slimapi.config import Settings

MIB = 1024 * 1024


def _base(**overrides) -> Settings:
    kw = dict(host="127.0.0.1", upstream="http://127.0.0.1:4096")
    kw.update(overrides)
    return Settings(**kw)


# ---------------------------------------------------------------------------
# Frozen defaults + module constant
# ---------------------------------------------------------------------------


def test_phase_a_defaults_frozen_and_validate_clean():
    s = _base()
    assert s.since_cache_enabled is True
    assert s.since_cache_max_entries == 256
    assert s.since_cache_max_bytes == 64 * MIB
    assert s.since_cache_max_entry_bytes == 1 * MIB
    assert s.file_raw_max_envelope_bytes == 32 * MIB
    s.validate()  # defaults fit every budget by construction


def test_a_amp_module_constant_frozen_at_4():
    assert config_mod.A_AMP == 4


# ---------------------------------------------------------------------------
# Knob validation (positive / lower-bound checks)
# ---------------------------------------------------------------------------


def test_since_cache_max_entries_must_be_positive():
    with pytest.raises(RuntimeError, match=r"SINCE_CACHE_MAX_ENTRIES must be >= 1"):
        _base(since_cache_max_entries=0).validate()
    _base(since_cache_max_entries=1).validate()  # boundary: 1 is legal


def test_since_cache_max_bytes_must_be_positive():
    with pytest.raises(RuntimeError, match=r"SINCE_CACHE_MAX_BYTES must be > 0"):
        _base(since_cache_max_bytes=0).validate()


def test_since_cache_max_entry_bytes_must_be_positive():
    with pytest.raises(RuntimeError,
                       match=r"SINCE_CACHE_MAX_ENTRY_BYTES must be > 0"):
        _base(since_cache_max_entry_bytes=0).validate()


def test_file_raw_max_envelope_bytes_must_be_positive():
    with pytest.raises(
            RuntimeError,
            match=r"FILE_RAW_MAX_ENVELOPE_BYTES must be > 0"):
        _base(file_raw_max_envelope_bytes=0).validate()


# ---------------------------------------------------------------------------
# Startup memory-budget extension (512/576 MiB aggregate 口径)
# ---------------------------------------------------------------------------


def test_budget_file_raw_dominant_uses_max_not_sum():
    """W=3, max_response_bytes=32 MiB → existing transform bound
    3×32=96 MiB; file_raw bound 5×3×32=480 MiB dominates.
    aggregate = 64 (raw) + 480 = 544 MiB ≤ 576 → PASS.
    (A wrong double-addition 口径 — 64+96+480 = 640 MiB — would reject:
    this test pins the max() semantics.)"""
    _base(max_transforms=3, max_response_bytes=32 * MIB).validate()


def test_budget_file_raw_over_aggregate_fails_fast():
    """W=4, max_response_bytes=32 MiB → existing 128 MiB (≤512, P1-30
    passes); file_raw bound 5×4×32=640 MiB → aggregate 64+640=704 MiB
    > 576 → fail-fast at startup."""
    with pytest.raises(
            RuntimeError,
            match=r"raw-fetch and transform budgets peak concurrently"):
        _base(max_transforms=4, max_response_bytes=32 * MIB).validate()


def test_budget_effective_cap_is_min_with_envelope():
    """W=2, max_response_bytes=256 MiB (existing bound 512 MiB — the legacy
    edge that still passes); effective_cap is min(256, 32) = 32 MiB so the
    file_raw bound is 5×2×32=320 MiB, NOT 5×2×256. The aggregate stays at
    exactly 576 MiB and passes. (Without the min(), 64 + 2560 → reject.)"""
    _base(max_transforms=2, max_response_bytes=256 * MIB).validate()


def test_budget_transform_dominant_existing_bound_unchanged():
    """W=1, max_response_bytes=64 MiB (defaults): existing 64 MiB >
    file_raw bound 5×1×32=160? No — 160 dominates; this documents that
    even DEFAULT config now carries a 160 MiB transform-side bound
    (64 raw + 160 = 224 MiB ≤ 576, comfortably inside)."""
    _base(max_transforms=1, max_response_bytes=64 * MIB).validate()


def test_budget_file_raw_envelope_below_response_cap_is_legal():
    """file_raw_max_envelope_bytes ≤ max_response_bytes is NOT a hard
    constraint at Settings level (the effective_cap min() is Phase C's
    business) — a smaller envelope merely lowers the file_raw bound."""
    _base(max_transforms=2, max_response_bytes=64 * MIB,
          file_raw_max_envelope_bytes=8 * MIB).validate()
