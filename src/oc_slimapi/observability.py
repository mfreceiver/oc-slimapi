"""In-memory opt-A observability counters and rollback decision logic.

Provides :class:`BatchLedger` which tracks opt-in batch outcomes and a rolling
time-window for the rollback decision. Single-worker uvicorn assumption: all
``record_*`` / ``evaluate_rollback`` calls happen on the event loop; a
:class:`threading.Lock` is used for honesty around mutations (the transform pool
offloads parse to worker threads, though recording happens post-``await`` on the
loop).
"""

from __future__ import annotations

import collections
import statistics
import threading
import time

from typing import Final


class BatchLedger:
    """In-memory ledger for opt-A batch observability + rollback latched state.

    :param window_seconds: Rolling window width for rate evaluation (seconds).
    """

    __slots__ = (
        "_lock",
        "_window_seconds",
        "_disabled",
        "_disabled_reason",
        "_opt_in_total",
        "_opt_in_success_envelope",
        "_opt_in_partial",
        "_opt_in_errors_only",
        "_opt_in_top_level_503",
        "_legacy_total",
        "_legacy_top_level_503",
        "_capability_conflicts",
        "_capability_malformed_tokens",
        "_network_mid_errors_total",
        "_unknown_code_total",
        "_mode_full_requests",
        "_mode_skeleton_requests",
        "_bytes_fetched_total",
        "_bytes_delivered_skeleton_total",
        "_retry_after_ms_emitted_count",
        "_window_events",
        "_byte_samples",
    )

    def __init__(self, *, window_seconds: int) -> None:
        self._lock = threading.Lock()
        self._window_seconds = window_seconds
        self._disabled = False
        self._disabled_reason: str | None = None
        # Counters
        self._opt_in_total = 0
        self._opt_in_success_envelope = 0
        self._opt_in_partial = 0
        self._opt_in_errors_only = 0
        self._opt_in_top_level_503 = 0
        self._legacy_total = 0
        self._legacy_top_level_503 = 0
        self._capability_conflicts = 0
        self._capability_malformed_tokens = 0
        self._network_mid_errors_total = 0
        self._unknown_code_total = 0
        self._mode_full_requests = 0
        self._mode_skeleton_requests = 0
        self._bytes_fetched_total = 0
        self._bytes_delivered_skeleton_total = 0
        self._retry_after_ms_emitted_count = 0
        # Rolling window: deque of (monotonic_ts, is_envelope_5xx, unknown_codes)
        self._window_events: collections.deque[tuple[float, bool, int]] = collections.deque()
        # Byte-ratio samples: bounded deque of (bytes_fetched, bytes_delivered_skeleton)
        self._byte_samples: collections.deque[tuple[int, int]] = collections.deque(maxlen=20000)

    # ---- public record methods ----

    def record_opt_in_outcome(
        self,
        *,
        outcome: str,
        envelope_5xx: bool,
        unknown_codes: int,
        network_mid_errors: int,
        items_count: int,
        errors_count: int,
        bytes_fetched: int,
        bytes_delivered_skeleton: int,
        mode: str,
        retry_after_ms_emitted: int = 0,
    ) -> None:
        """Record an opt-in batch outcome.

        :param outcome: One of ``"success"``, ``"partial"``, ``"errors_only"``,
            ``"top_level_503"``.
        :param envelope_5xx: Whether the response was the opt-in top-level 503 path
            (the "envelope-related 5xx" rollback signal).
        :param unknown_codes: Count of codes emitted into ``errors[]`` outside the
            known set.
        :param network_mid_errors: Count of network-level mid errors.
        :param items_count: Total items in the batch.
        :param errors_count: Count of errored items.
        :param bytes_fetched: Total bytes fetched from upstream.
        :param bytes_delivered_skeleton: Total bytes in the skeleton response.
        :param mode: ``"full"`` or ``"skeleton"``.
        :param retry_after_ms_emitted: Count of per-mid ``retryAfterMs`` values
            emitted in this batch (default 0).
        """
        with self._lock:
            self._opt_in_total += 1
            if outcome == "success":
                self._opt_in_success_envelope += 1
            elif outcome == "partial":
                self._opt_in_partial += 1
            elif outcome == "errors_only":
                self._opt_in_errors_only += 1
            elif outcome == "top_level_503":
                self._opt_in_top_level_503 += 1
            # else: ignore (should not happen)
            self._network_mid_errors_total += network_mid_errors
            self._unknown_code_total += unknown_codes
            if mode == "full":
                self._mode_full_requests += 1
            elif mode == "skeleton":
                self._mode_skeleton_requests += 1
            self._bytes_fetched_total += bytes_fetched
            self._bytes_delivered_skeleton_total += bytes_delivered_skeleton
            self._retry_after_ms_emitted_count += retry_after_ms_emitted

            # top_level_413 is deterministic (response_too_large), not an Opt-A
            # regression signal, so exclude from the rolling window + byte samples.
            if outcome != "top_level_413":
                # Push event to rolling window
                now = time.monotonic()
                self._window_events.append((now, envelope_5xx, unknown_codes))
                # Trim stale entries
                cutoff = now - self._window_seconds
                while self._window_events and self._window_events[0][0] < cutoff:
                    self._window_events.popleft()

                # Store byte sample (for P1 S-C)
                self._byte_samples.append((bytes_fetched, bytes_delivered_skeleton))

    def record_legacy_outcome(self, *, top_level_503: bool, mode: str) -> None:
        """Record a legacy (non-opt-in) batch outcome."""
        with self._lock:
            self._legacy_total += 1
            if top_level_503:
                self._legacy_top_level_503 += 1
            if mode == "full":
                self._mode_full_requests += 1
            elif mode == "skeleton":
                self._mode_skeleton_requests += 1

    def record_capability_parse(self, *, conflict: bool, malformed_tokens: int) -> None:
        """Record capability header parse telemetry."""
        with self._lock:
            if conflict:
                self._capability_conflicts += 1
            self._capability_malformed_tokens += malformed_tokens

    # ---- rollback evaluation ----

    def evaluate_rollback(
        self,
        *,
        auto_enabled: bool,
        min_sample: int,
        envelope_5xx_zero_baseline_rate: float,
        unknown_code_rate_threshold: float,
    ) -> tuple[bool, str | None]:
        """Evaluate whether auto-rollback should trip.

        When ``auto_enabled=False``, NEVER disables (advisory mode) — just returns
        current latched state. When the window has fewer than ``min_sample`` opt-in
        events, returns ``(False, None)`` — "warn only, no auto-close". Otherwise
        evaluates:

        - ``envelope_5xx_rate = envelope_5xx_in_window / opt_in_events_in_window``
        - ``unknown_code_rate = unknown_codes_in_window / opt_in_events_in_window``
        - Trip if ``envelope_5xx_rate > envelope_5xx_zero_baseline_rate`` (baseline=0
          special case, since opt-in path is new) OR
          ``unknown_code_rate > unknown_code_rate_threshold``.

        Once tripped, latches ``disabled=True`` forever (process lifetime); reason
        recorded.

        :returns: ``(disabled, reason)``.
        """
        with self._lock:
            if self._disabled:
                return True, self._disabled_reason

            if not auto_enabled:
                # Advisory mode: never close, just return current state
                return False, None

            # Trim stale events first
            now = time.monotonic()
            cutoff = now - self._window_seconds
            while self._window_events and self._window_events[0][0] < cutoff:
                self._window_events.popleft()

            opt_in_window = len(self._window_events)
            if opt_in_window < min_sample:
                return False, None

            # Count envelope_5xx and unknown_codes in window
            envelope_5xx_window = sum(1 for _, is_env, _ in self._window_events if is_env)
            unknown_codes_window = sum(unc for _, _, unc in self._window_events)

            envelope_5xx_rate = envelope_5xx_window / opt_in_window
            unknown_code_rate = unknown_codes_window / opt_in_window

            reason_parts: list[str] = []
            if envelope_5xx_rate > envelope_5xx_zero_baseline_rate:
                reason_parts.append(
                    f"envelope_5xx_rate={envelope_5xx_rate:.4f} > "
                    f"baseline={envelope_5xx_zero_baseline_rate}"
                )
            if unknown_code_rate > unknown_code_rate_threshold:
                reason_parts.append(
                    f"unknown_code_rate={unknown_code_rate:.4f} > "
                    f"threshold={unknown_code_rate_threshold}"
                )

            if reason_parts:
                self._disabled = True
                self._disabled_reason = "; ".join(reason_parts)
                return True, self._disabled_reason

            return False, None

    # ---- properties ----

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    # ---- byte-ratio statistics ----

    def _compute_byte_ratio_stats(self) -> tuple[float | None, float | None]:
        """Compute median and P90 of skeleton-delivered/fetched byte ratios.

        Only samples where ``fetched > 0`` are considered. Returns
        ``(ratioMedian, ratioP90)`` where each is ``None`` if fewer than 2
        valid samples.
        """
        valid_ratios: list[float] = []
        for fetched, delivered in self._byte_samples:
            if fetched > 0:
                valid_ratios.append(delivered / fetched)

        n = len(valid_ratios)
        if n == 0:
            return None, None

        sorted_ratios = sorted(valid_ratios)
        median = statistics.median(sorted_ratios)

        # P90: nearest-rank method — index = ceil(0.90 * n) - 1
        import math
        p90_index = math.ceil(0.90 * n) - 1
        p90 = sorted_ratios[p90_index]

        return median, p90

    # ---- snapshot ----

    def snapshot(self) -> dict:
        """Return a snapshot dict for ``/slimapi/metrics``.

        Shape (binding — ``routes/metrics.py`` nests this under ``"batch"``):

        .. code-block:: python

            {
              "optA": {
                "enabled": bool,              # config flag? NO — ledger's view
                "disabledLatched": bool,
                "disabledReason": str | None,
              },
              "counters": {
                "optInRequestsTotal": int,
                "optInSuccessEnvelope": int,
                "optInPartial": int,
                "optInErrorsOnly": int,
                "optInTopLevel503": int,
                "legacyRequestsTotal": int,
                "legacyTopLevel503": int,
                "capabilityConflicts": int,
                "capabilityMalformedTokens": int,
                "networkMidErrorsTotal": int,
                "unknownCodeTotal": int,
                "modeFullRequests": int,
                "modeSkeletonRequests": int,
                "bytesFetchedTotal": int,
                "bytesDeliveredSkeletonTotal": int,
                "retryAfterMsEmittedCount": int,
              },
              "rollbackWindow": {
                "windowSeconds": int,
                "optInEvents": int,
                "envelope5xxInWindow": int,
                "unknownCodesInWindow": int,
              },
              "byteSamples": {
                "count": int,
                "capacity": int,
                "ratioMedian": float | None,
                "ratioP90": float | None,
              },
            }
        """
        with self._lock:
            opt_in_events_window = len(self._window_events)
            envelope_5xx_window = sum(1 for _, is_env, _ in self._window_events if is_env)
            unknown_codes_window = sum(unc for _, _, unc in self._window_events)

            ratio_median, ratio_p90 = self._compute_byte_ratio_stats()

            return {
                "optA": {
                    "enabled": not self._disabled,  # ledger's view: not disabled
                    "disabledLatched": self._disabled,
                    "disabledReason": self._disabled_reason,
                },
                "counters": {
                    "optInRequestsTotal": self._opt_in_total,
                    "optInSuccessEnvelope": self._opt_in_success_envelope,
                    "optInPartial": self._opt_in_partial,
                    "optInErrorsOnly": self._opt_in_errors_only,
                    "optInTopLevel503": self._opt_in_top_level_503,
                    "legacyRequestsTotal": self._legacy_total,
                    "legacyTopLevel503": self._legacy_top_level_503,
                    "capabilityConflicts": self._capability_conflicts,
                    "capabilityMalformedTokens": self._capability_malformed_tokens,
                    "networkMidErrorsTotal": self._network_mid_errors_total,
                    "unknownCodeTotal": self._unknown_code_total,
                    "modeFullRequests": self._mode_full_requests,
                    "modeSkeletonRequests": self._mode_skeleton_requests,
                    "bytesFetchedTotal": self._bytes_fetched_total,
                    "bytesDeliveredSkeletonTotal": self._bytes_delivered_skeleton_total,
                    "retryAfterMsEmittedCount": self._retry_after_ms_emitted_count,
                },
                "rollbackWindow": {
                    "windowSeconds": self._window_seconds,
                    "optInEvents": opt_in_events_window,
                    "envelope5xxInWindow": envelope_5xx_window,
                    "unknownCodesInWindow": unknown_codes_window,
                },
                "byteSamples": {
                    "count": len(self._byte_samples),
                    "capacity": self._byte_samples.maxlen,
                    "ratioMedian": ratio_median,
                    "ratioP90": ratio_p90,
                },
            }
