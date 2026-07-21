"""Unit tests for ``oc_slimapi.observability.BatchLedger``.

Tests counter increments, rolling window, and rollback evaluation logic.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from oc_slimapi.observability import BatchLedger


def _ledger(window_seconds: int = 3600) -> BatchLedger:
    return BatchLedger(window_seconds=window_seconds)


class TestRecordOptInOutcome:
    """Verify counters increment correctly per outcome type."""

    def test_success(self):
        ledger = _ledger()
        ledger.record_opt_in_outcome(
            outcome="success", envelope_5xx=False, unknown_codes=0,
            network_mid_errors=0, items_count=2, errors_count=0,
            bytes_fetched=100, bytes_delivered_skeleton=50,
            mode="skeleton", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            assert ledger._opt_in_total == 1
            assert ledger._opt_in_success_envelope == 1
            assert ledger._opt_in_partial == 0
            assert ledger._opt_in_errors_only == 0
            assert ledger._opt_in_top_level_503 == 0
            assert ledger._network_mid_errors_total == 0
            assert ledger._unknown_code_total == 0

    def test_partial(self):
        ledger = _ledger()
        ledger.record_opt_in_outcome(
            outcome="partial", envelope_5xx=False, unknown_codes=1,
            network_mid_errors=2, items_count=1, errors_count=2,
            bytes_fetched=200, bytes_delivered_skeleton=80,
            mode="full", retry_after_ms_emitted=3,
        )
        with ledger._lock:
            assert ledger._opt_in_total == 1
            assert ledger._opt_in_partial == 1
            assert ledger._opt_in_success_envelope == 0
            assert ledger._network_mid_errors_total == 2
            assert ledger._unknown_code_total == 1
            assert ledger._retry_after_ms_emitted_count == 3

    def test_errors_only(self):
        ledger = _ledger()
        ledger.record_opt_in_outcome(
            outcome="errors_only", envelope_5xx=False, unknown_codes=0,
            network_mid_errors=0, items_count=0, errors_count=2,
            bytes_fetched=0, bytes_delivered_skeleton=20,
            mode="skeleton", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            assert ledger._opt_in_errors_only == 1

    def test_top_level_503(self):
        ledger = _ledger()
        ledger.record_opt_in_outcome(
            outcome="top_level_503", envelope_5xx=True, unknown_codes=0,
            network_mid_errors=3, items_count=0, errors_count=0,
            bytes_fetched=0, bytes_delivered_skeleton=100,
            mode="skeleton", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            assert ledger._opt_in_top_level_503 == 1

    def test_top_level_413(self):
        ledger = _ledger()
        ledger.record_opt_in_outcome(
            outcome="top_level_413", envelope_5xx=False, unknown_codes=0,
            network_mid_errors=0, items_count=0, errors_count=0,
            bytes_fetched=0, bytes_delivered_skeleton=50,
            mode="skeleton", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            assert ledger._opt_in_total == 1  # counts as opt-in total


class TestRecordLegacyOutcome:
    def test_legacy_503(self):
        ledger = _ledger()
        ledger.record_legacy_outcome(top_level_503=True, mode="full")
        with ledger._lock:
            assert ledger._legacy_total == 1
            assert ledger._legacy_top_level_503 == 1

    def test_legacy_no_503(self):
        ledger = _ledger()
        ledger.record_legacy_outcome(top_level_503=False, mode="skeleton")
        with ledger._lock:
            assert ledger._legacy_total == 1
            assert ledger._legacy_top_level_503 == 0


class TestRecordCapabilityParse:
    def test_conflict(self):
        ledger = _ledger()
        ledger.record_capability_parse(conflict=True, malformed_tokens=0)
        with ledger._lock:
            assert ledger._capability_conflicts == 1

    def test_malformed(self):
        ledger = _ledger()
        ledger.record_capability_parse(conflict=False, malformed_tokens=2)
        with ledger._lock:
            assert ledger._capability_malformed_tokens == 2


class TestRollingWindow:
    def test_trim_stale_events(self):
        """Events older than window_seconds are trimmed."""
        ledger = _ledger(window_seconds=1)
        t0 = time.monotonic()
        with patch("time.monotonic", return_value=t0):
            ledger.record_opt_in_outcome(
                outcome="success", envelope_5xx=False, unknown_codes=0,
                network_mid_errors=0, items_count=1, errors_count=0,
                bytes_fetched=10, bytes_delivered_skeleton=5,
                mode="full", retry_after_ms_emitted=0,
            )
        t1 = t0 + 1.5  # 1.5s later → outside 1s window
        with patch("time.monotonic", return_value=t1):
            ledger.record_opt_in_outcome(
                outcome="success", envelope_5xx=False, unknown_codes=0,
                network_mid_errors=0, items_count=1, errors_count=0,
                bytes_fetched=10, bytes_delivered_skeleton=5,
                mode="full", retry_after_ms_emitted=0,
            )
        with ledger._lock:
            assert len(ledger._window_events) == 1  # only the recent one
            assert ledger._window_events[0][1] is False  # envelope_5xx

    def test_window_events_accumulate(self):
        ledger = _ledger(window_seconds=10)
        for _ in range(5):
            ledger.record_opt_in_outcome(
                outcome="success", envelope_5xx=True, unknown_codes=0,
                network_mid_errors=0, items_count=1, errors_count=0,
                bytes_fetched=10, bytes_delivered_skeleton=5,
                mode="full", retry_after_ms_emitted=0,
            )
        with ledger._lock:
            assert len(ledger._window_events) == 5
            assert sum(1 for _, is_env, _ in ledger._window_events if is_env) == 5


class TestEvaluateRollback:
    """Test the auto-rollback evaluation logic."""

    def _populate_window(self, ledger: BatchLedger, n: int, envelope_5xx_rate: float):
        """Add n events, with a given fraction of envelope_5xx=True."""
        envelope_count = int(n * envelope_5xx_rate)
        for i in range(n):
            ledger.record_opt_in_outcome(
                outcome="success" if i >= envelope_count else "top_level_503",
                envelope_5xx=(i < envelope_count),
                unknown_codes=0,
                network_mid_errors=0,
                items_count=1,
                errors_count=0,
                bytes_fetched=10,
                bytes_delivered_skeleton=5,
                mode="full",
                retry_after_ms_emitted=0,
            )

    def test_auto_disabled(self):
        ledger = _ledger()
        disabled, reason = ledger.evaluate_rollback(
            auto_enabled=False, min_sample=5,
            envelope_5xx_zero_baseline_rate=0.0,
            unknown_code_rate_threshold=0.0,
        )
        assert disabled is False
        assert reason is None

    def test_insufficient_sample(self):
        ledger = _ledger()
        self._populate_window(ledger, 3, 0.0)
        disabled, reason = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=5,
            envelope_5xx_zero_baseline_rate=0.0,
            unknown_code_rate_threshold=0.0,
        )
        assert disabled is False
        assert reason is None

    def test_envelope_5xx_above_baseline(self):
        ledger = _ledger(window_seconds=100)
        self._populate_window(ledger, 100, 0.05)  # 5% envelope_5xx
        disabled, reason = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=0.01,  # 1% threshold
            unknown_code_rate_threshold=1.0,  # never trip
        )
        assert disabled is True
        assert "envelope_5xx" in reason

    def test_unknown_code_above_threshold(self):
        ledger = _ledger(window_seconds=100)
        # Populate with known codes (no unknown)
        for _ in range(80):
            ledger.record_opt_in_outcome(
                outcome="success", envelope_5xx=False, unknown_codes=0,
                network_mid_errors=0, items_count=1, errors_count=0,
                bytes_fetched=10, bytes_delivered_skeleton=5,
                mode="full", retry_after_ms_emitted=0,
            )
        # Add 20 with unknown codes
        for _ in range(20):
            ledger.record_opt_in_outcome(
                outcome="partial", envelope_5xx=False, unknown_codes=1,
                network_mid_errors=0, items_count=1, errors_count=1,
                bytes_fetched=10, bytes_delivered_skeleton=5,
                mode="full", retry_after_ms_emitted=0,
            )
        disabled, reason = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=1.0,  # never trip
            unknown_code_rate_threshold=0.1,  # 10% threshold
        )
        assert disabled is True
        assert "unknown_code" in reason

    def test_no_trip_when_below_threshold(self):
        ledger = _ledger(window_seconds=100)
        self._populate_window(ledger, 100, 0.005)  # 0.5% envelope_5xx
        disabled, reason = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=0.01,  # 1% threshold
            unknown_code_rate_threshold=1.0,
        )
        assert disabled is False

    def test_latched_sticky(self):
        ledger = _ledger(window_seconds=100)
        self._populate_window(ledger, 100, 0.05)  # trip
        disabled1, reason1 = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=0.01,
            unknown_code_rate_threshold=1.0,
        )
        assert disabled1 is True
        # Even if subsequent events are clean, latch stays.
        ledger2 = _ledger(window_seconds=100)
        self._populate_window(ledger2, 100, 0.0)  # clean
        disabled2, reason2 = ledger2.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=0.01,
            unknown_code_rate_threshold=1.0,
        )
        assert disabled2 is False  # new ledger, not latched
        # But the first ledger still latched:
        disabled1b, _ = ledger.evaluate_rollback(
            auto_enabled=True, min_sample=50,
            envelope_5xx_zero_baseline_rate=0.01,
            unknown_code_rate_threshold=1.0,
        )
        assert disabled1b is True


class TestSnapshot:
    def test_shape_has_keys(self):
        ledger = _ledger()
        snap = ledger.snapshot()
        assert set(snap) == {"optA", "counters", "rollbackWindow", "byteSamples"}
        assert set(snap["optA"]) == {"enabled", "disabledLatched", "disabledReason"}
        assert set(snap["counters"]) == {
            "optInRequestsTotal", "optInSuccessEnvelope", "optInPartial",
            "optInErrorsOnly", "optInTopLevel503", "legacyRequestsTotal",
            "legacyTopLevel503", "capabilityConflicts", "capabilityMalformedTokens",
            "networkMidErrorsTotal", "unknownCodeTotal", "modeFullRequests",
            "modeSkeletonRequests", "bytesFetchedTotal", "bytesDeliveredSkeletonTotal",
            "retryAfterMsEmittedCount",
        }
        assert set(snap["rollbackWindow"]) == {"windowSeconds", "optInEvents", "envelope5xxInWindow", "unknownCodesInWindow"}
        assert set(snap["byteSamples"]) == {"count", "capacity", "ratioMedian", "ratioP90"}


class TestByteRatioStats:
    """Test the byte-ratio median/P90 computation."""

    def test_empty_returns_none(self):
        ledger = _ledger()
        median, p90 = ledger._compute_byte_ratio_stats()
        assert median is None
        assert p90 is None

    def test_single_sample_returns_values(self):
        ledger = _ledger()
        ledger._byte_samples.append((100, 50))
        median, p90 = ledger._compute_byte_ratio_stats()
        # n=1 valid sample: median = single value, P90 = single value
        assert median == pytest.approx(0.5)
        assert p90 == pytest.approx(0.5)

    def test_all_fetched_zero_excluded(self):
        ledger = _ledger()
        ledger._byte_samples.append((0, 50))
        ledger._byte_samples.append((0, 100))
        median, p90 = ledger._compute_byte_ratio_stats()
        assert median is None
        assert p90 is None

    def test_basic_computation(self):
        ledger = _ledger()
        # (fetched, delivered)
        samples = [(100, 30), (200, 80), (150, 60), (50, 25), (300, 120)]
        for f, d in samples:
            ledger._byte_samples.append((f, d))
        # Ratios: 0.3, 0.4, 0.4, 0.5, 0.4
        # Sorted: [0.3, 0.4, 0.4, 0.4, 0.5]
        # Median = 0.4 (middle of odd count)
        # P90: n=5, ceil(0.9*5)=5, index=4 → 0.5
        median, p90 = ledger._compute_byte_ratio_stats()
        assert median == pytest.approx(0.4)
        assert p90 == pytest.approx(0.5)

    def test_median_p90_with_even_count(self):
        ledger = _ledger()
        samples = [(100, 30), (200, 80), (150, 60), (50, 25)]
        for f, d in samples:
            ledger._byte_samples.append((f, d))
        # Ratios: 0.3, 0.4, 0.4, 0.5
        # Sorted: [0.3, 0.4, 0.4, 0.5]
        # Median = avg of two middle = (0.4+0.4)/2 = 0.4
        # P90: n=4, ceil(0.9*4)=4, index=3 → 0.5
        median, p90 = ledger._compute_byte_ratio_stats()
        assert median == pytest.approx(0.4)
        assert p90 == pytest.approx(0.5)

    def test_skips_fetched_zero(self):
        ledger = _ledger()
        samples = [(100, 30), (0, 0), (200, 80), (0, 50)]
        for f, d in samples:
            ledger._byte_samples.append((f, d))
        # Valid ratios: 0.3, 0.4 → sorted [0.3, 0.4]
        # n=2 → median = (0.3+0.4)/2 = 0.35, P90: ceil(0.9*2)=2, index=1 → 0.4
        median, p90 = ledger._compute_byte_ratio_stats()
        assert median == pytest.approx(0.35)
        assert p90 == pytest.approx(0.4)

    def test_ratio_in_snapshot(self):
        ledger = _ledger()
        ledger._byte_samples.append((100, 50))
        snap = ledger.snapshot()
        # n=1 valid → median and P90 are both 0.5
        assert snap["byteSamples"]["ratioMedian"] == pytest.approx(0.5)
        assert snap["byteSamples"]["ratioP90"] == pytest.approx(0.5)
        ledger._byte_samples.append((200, 100))
        snap = ledger.snapshot()
        # n=2 valid → median avg 0.5, P90 second value 0.5
        assert snap["byteSamples"]["ratioMedian"] == pytest.approx(0.5)
        assert snap["byteSamples"]["ratioP90"] == pytest.approx(0.5)
        ledger._byte_samples.append((150, 60))
        snap = ledger.snapshot()
        # Ratios: 0.5, 0.5, 0.4 → sorted [0.4, 0.5, 0.5]
        # median = 0.5, P90 = 0.5
        assert snap["byteSamples"]["ratioMedian"] == pytest.approx(0.5)
        assert snap["byteSamples"]["ratioP90"] == pytest.approx(0.5)


class TestTopLevel413Exclusion:
    """Verify top_level_413 outcome is excluded from rolling window + byte samples."""

    def test_413_excluded_from_window_and_byte_samples(self):
        ledger = _ledger()
        # Record a 413 outcome
        ledger.record_opt_in_outcome(
            outcome="top_level_413", envelope_5xx=False, unknown_codes=0,
            network_mid_errors=0, items_count=0, errors_count=0,
            bytes_fetched=100, bytes_delivered_skeleton=50,
            mode="full", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            # optInRequestsTotal increments
            assert ledger._opt_in_total == 1
            # But window events and byte samples are empty
            assert len(ledger._window_events) == 0
            assert len(ledger._byte_samples) == 0
        # Now record a normal outcome
        ledger.record_opt_in_outcome(
            outcome="success", envelope_5xx=False, unknown_codes=0,
            network_mid_errors=0, items_count=1, errors_count=0,
            bytes_fetched=50, bytes_delivered_skeleton=25,
            mode="full", retry_after_ms_emitted=0,
        )
        with ledger._lock:
            assert len(ledger._window_events) == 1
            assert len(ledger._byte_samples) == 1
