"""Shadow-only q/p sweep scheduling.

This module deliberately contains no upstream client and no HTTP operation.  A
touch records what a future real sweep *would* have done, which makes the
stage-1 scheduler safe to run in production while its cadence and budget are
observed.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any


class QpSweepShadow:
    """Round-robin, budgeted, dry-run q/p sweep scheduler."""

    ESTIMATED_DIRECTORY_BYTES = 2 * 1024

    def __init__(
        self,
        *,
        activity: dict[str, float] | None = None,
        directories: Iterable[str] = (),
        directory_source: Callable[[], Iterable[Any]] | None = None,
        interval_seconds: float = 1800.0,
        daily_budget: int = 100,
        enabled: bool = True,
        batch_size: int = 1,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if daily_budget < 0:
            raise ValueError("daily_budget must be >= 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        self._activity = activity if activity is not None else {}
        self._known: dict[str, None] = {}
        self._seen_at: dict[str, float] = {}
        self._directory_source = directory_source
        self.interval_seconds = interval_seconds
        self.daily_budget = daily_budget
        self.enabled = enabled
        self.batch_size = batch_size
        self._now = now
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self._cursor = 0
        self._budget_day: str | None = None
        self._budget_used = 0
        self._task: asyncio.Task[None] | None = None
        self.markers: list[dict[str, Any]] = []
        self._triggers_total = 0
        self._cold_hits = 0
        self._skips = 0
        self._budget_exhausted = 0
        self._est_bytes_total = 0
        for directory in directories:
            self.observe_directory(directory)

    @property
    def activity(self) -> dict[str, float]:
        return self._activity

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def observe_directory(self, directory: str, *, now: float | None = None) -> None:
        if not isinstance(directory, str) or not directory:
            return
        if directory not in self._known:
            self._known[directory] = None
            self._seen_at[directory] = self._now() if now is None else now

    def record_activity(self, directory: str, *, now: float | None = None) -> None:
        if not isinstance(directory, str) or not directory:
            return
        timestamp = self._now() if now is None else now
        self._activity[directory] = timestamp
        self.observe_directory(directory, now=timestamp)

    def record_request_activity(
        self, directories: Iterable[str], *, now: float | None = None
    ) -> None:
        """Record one aggregate questions/permissions request for its dirs."""
        timestamp = self._now() if now is None else now
        for directory in directories:
            self.record_activity(directory, now=timestamp)

    def _ingest_directory_source(self) -> None:
        for directory, timestamp in self._activity.items():
            self.observe_directory(directory, now=timestamp)
        if self._directory_source is None:
            return
        for item in self._directory_source():
            directory = item if isinstance(item, str) else getattr(item, "directory", None)
            self.observe_directory(directory)

    def next_delay(self) -> float:
        factor = min(1.2, max(0.8, float(self.jitter())))
        return self.interval_seconds * factor

    @staticmethod
    def _utc_day(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()

    def _reset_budget_if_needed(self, timestamp: float) -> None:
        day = self._utc_day(timestamp)
        if self._budget_day != day:
            self._budget_day = day
            self._budget_used = 0

    def _batch(self) -> list[str]:
        directories = list(self._known)
        if not directories:
            return []
        if self._cursor >= len(directories):
            self._cursor = 0
        batch = [
            directories[(self._cursor + offset) % len(directories)]
            for offset in range(min(self.batch_size, len(directories)))
        ]
        self._cursor = (self._cursor + len(batch)) % len(directories)
        return batch

    def run_once(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Run one shadow round and return the markers emitted by that round."""
        timestamp = self._now() if now is None else now
        self._ingest_directory_source()
        self._reset_budget_if_needed(timestamp)
        emitted: list[dict[str, Any]] = []
        for directory in self._batch():
            self._triggers_total += 1
            last_activity = self._activity.get(directory, self._seen_at.get(directory, timestamp))
            elapsed = max(0.0, timestamp - last_activity)
            if elapsed < self.interval_seconds * 3:
                decision = "skip"
                would_sweep = False
                self._skips += 1
            elif self._budget_used >= self.daily_budget:
                decision = "budget_exhausted"
                would_sweep = False
                self._budget_exhausted += 1
            else:
                decision = "cold"
                would_sweep = True
                self._cold_hits += 1
                self._budget_used += 1
                self._est_bytes_total += self.ESTIMATED_DIRECTORY_BYTES
            marker = {
                "ts": timestamp,
                "directory": directory,
                "decision": decision,
                "would_sweep": would_sweep,
            }
            self.markers.append(marker)
            emitted.append(marker)
        return emitted

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.next_delay())
            self.run_once()

    def start(self) -> asyncio.Task[None] | None:
        if not self.enabled or self.running:
            return self._task
        self._task = asyncio.create_task(self._run(), name="qp-sweep-shadow")
        return self._task

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def metrics(self) -> dict[str, int]:
        return {
            "triggers_total": self._triggers_total,
            "cold_hits": self._cold_hits,
            "skips": self._skips,
            "budget_exhausted": self._budget_exhausted,
            "est_bytes_total": self._est_bytes_total,
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.metrics(), "markers": list(self.markers)}
