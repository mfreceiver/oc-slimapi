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
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from .logging_config import get_logger
from .sse.hub_types import record_qp_activity

logger = get_logger(__name__)


_EVICTION_AFTER = 30 * 86400.0
_MAX_SLEEP_SECONDS = 30.0


class QpSweepShadow:
    """Budgeted, dry-run q/p sweep scheduler.

    Scheduling is per directory rather than per global round: each directory
    has its own ``next_run`` deadline and receives an independently jittered
    interval after every evaluation.  The scheduler sleeps until the nearest
    deadline, with a short wake cap for newly observed directories; it never
    makes the directory count part of a directory's cadence.
    """

    ESTIMATED_DIRECTORY_BYTES = 2 * 1024

    def __init__(
        self,
        *,
        activity: dict[str, float] | None = None,
        directories: Iterable[str] = (),
        interval_seconds: float = 1800.0,
        daily_budget: int = 100,
        enabled: bool = True,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] | None = None,
        eviction_after_seconds: float = _EVICTION_AFTER,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if daily_budget < 0:
            raise ValueError("daily_budget must be >= 0")
        if eviction_after_seconds <= 0:
            raise ValueError("eviction_after_seconds must be > 0")
        self._activity = activity if activity is not None else {}
        # Monotonic within the process: a directory remains eligible for
        # evaluation even after the shared activity table stops seeing it.
        self._known_dirs: set[str] = set()
        self._seen_at: dict[str, float] = {}
        self._next_run: dict[str, float] = {}
        self.interval_seconds = interval_seconds
        self.eviction_after_seconds = eviction_after_seconds
        self.daily_budget = daily_budget
        self.enabled = enabled
        self._now = now
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self._budget_day: str | None = None
        self._budget_used = 0
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self.markers: deque[dict[str, Any]] = deque(maxlen=256)
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
        timestamp = self._now() if now is None else now
        if directory in self._known_dirs:
            self._seen_at[directory] = timestamp
            return
        self._known_dirs.add(directory)
        self._seen_at[directory] = timestamp
        # New directories are evaluated on the next scheduler scan (or the
        # next explicit run_once call), then move onto their normal cadence.
        self._next_run[directory] = timestamp
        if self.running:
            self._wake_event.set()

    def record_activity(self, directory: str, *, now: float | None = None) -> None:
        if not isinstance(directory, str) or not directory:
            return
        timestamp = self._now() if now is None else now
        # F-015: funnel through the shared activity-LRU helper (same one
        # the hub's IMMEDIATE branch uses — both write points share this
        # dict reference by app.py construction) so the table stays
        # bounded and re-touches move to the tail.
        record_qp_activity(self._activity, directory, timestamp)
        self.observe_directory(directory, now=timestamp)

    def record_request_activity(
        self, directories: Iterable[str], *, now: float | None = None
    ) -> None:
        """Record one aggregate questions/permissions request for its dirs."""
        timestamp = self._now() if now is None else now
        for directory in directories:
            self.record_activity(directory, now=timestamp)

    def _ingest_activity_directories(self) -> None:
        for directory, timestamp in self._activity.items():
            self.observe_directory(directory, now=timestamp)

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

    def _due_directories(self, timestamp: float) -> list[str]:
        due = [
            directory
            for directory in sorted(self._known_dirs)
            if self._next_run.get(directory, timestamp) <= timestamp
        ]
        return due

    def _evict_stale_directories(self, timestamp: float) -> None:
        stale = [
            directory
            for directory, seen_at in self._seen_at.items()
            if timestamp - seen_at >= self.eviction_after_seconds
        ]
        for directory in stale:
            self._known_dirs.discard(directory)
            self._seen_at.pop(directory, None)
            self._next_run.pop(directory, None)
            # F-273: the sweep and the hub share ONE activity dict (app.py
            # wires ``activity=global_hub.qp_last_activity``). Evicting a
            # directory here without popping the activity entry left the
            # shared table growing without bound — the eviction was a
            # no-op from the hub's memory point of view. (Redundant with
            # the QP_LAST_ACTIVITY_MAX LRU cap as defense in depth; this
            # path removes entries PROMPTLY at eviction time instead of
            # waiting for the cap to be reached.)
            self._activity.pop(directory, None)

    def _next_sleep(self, timestamp: float) -> float:
        self._evict_stale_directories(timestamp)
        if not self._next_run:
            return min(self.interval_seconds, _MAX_SLEEP_SECONDS)
        deadline = min(self._next_run.values())
        return min(max(0.0, deadline - timestamp), _MAX_SLEEP_SECONDS)

    def run_once(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Run one shadow round and return the markers emitted by that round."""
        timestamp = self._now() if now is None else now
        self._ingest_activity_directories()
        self._evict_stale_directories(timestamp)
        self._reset_budget_if_needed(timestamp)
        emitted: list[dict[str, Any]] = []
        for directory in self._due_directories(timestamp):
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
            # Every evaluation gets a fresh independent jitter sample,
            # including skips and budget-exhausted decisions.
            self._next_run[directory] = timestamp + self.next_delay()
        return emitted

    async def _run(self) -> None:
        while True:
            delay = self._next_sleep(self._now())
            if delay > 0:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            # F-007 (half): the scheduler loop must survive a run_once
            # blow-up. Any bug in the shadow evaluation previously
            # killed the task for the rest of the process lifetime —
            # silently stopping all shadow scheduling. Log-and-continue
            # mirrors the app.py exit-stack isolation; CancelledError is
            # a BaseException and still propagates so stop() keeps
            # working.
            try:
                self.run_once()
            except Exception:
                logger.warning("qp sweep run_once failed", exc_info=True)

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
            "known_directories": len(self._known_dirs),
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.metrics(), "markers": list(self.markers)}
