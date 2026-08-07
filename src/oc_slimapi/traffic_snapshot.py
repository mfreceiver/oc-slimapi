"""Periodic cumulative snapshot of the in-memory TrafficLedger.

The :class:`TrafficSnapshotter` runs an asyncio background loop that
periodically calls :meth:`TrafficLedger.snapshot` and appends a JSON line
to a file on disk. This preserves the only source of real SSE upstream byte
cost (which is lost on sidecar restart since the in-memory ledger volatiles).

Design decisions (task-2 阻断7 / rev-gpt design review):
  * **Cumulative (total) snapshot per tick** — we do NOT store a "last seen"
    value and compute deltas. Each frame writes the full ledger state as
    returned by ``ledger.snapshot()``. Delta derivation is deferred to analysis
    time (offline tooling).
  * **Monotonic clock for uptime** — ``time.monotonic()`` is used for
    ``uptimeS`` so that NTP clock corrections (forward or backward) never
    cause a negative or spiky uptime delta.
  * **Shutdown final-state guarantee** — :meth:`stop` cancels the background
    loop task (if alive), then *always* writes one final snapshot regardless
    of whether the loop task had already errored out. This ensures the last
    recorded state before graceful shutdown is always on disk.
  * **Exception convergence** — the background loop catches every per-iteration
    exception individually (disk full, serialization error, …) and logs a
    warning before continuing to the next sleep cycle. A single I/O error
    never kills the entire background task.
  * **Best-effort parent directory creation** — the write path attempts
    ``parent.mkdir(parents=True, exist_ok=True)`` before each write, wrapped
    in a try/except. Failure is warned, not fatal.
  * **Handle discipline + daily rotation** — each frame opens its daily file
    in append mode, writes one line, and closes.  The daily filename is
    ``{dir}/{stem}-YYYY-MM-DD.jsonl``, derived from the ``path`` constructor
    argument (``dir=parent, stem=stem``) so naming is consistent with the
    access log's ``DailyAccessHandler`` convention.
  * **True inactive on first-frame failure** — if the first ``_write_once``
    call in :meth:`start` fails (I/O error, mkdir failure, …), the snapshotter
    stays **inactive**: no background task is created, ``active`` is
    ``False``, and :meth:`stop` returns immediately.  Only a successful first
    write transitions to active state.

Usage (app.py lifespan):

    snapshotter = TrafficSnapshotter(
        ledger=ledger,
        interval_s=settings.traffic_snapshot_interval_s,
        path=settings.traffic_snapshot_path,  # e.g. "logs/traffic-snapshot.jsonl"
    )
    await snapshotter.start()       # startup
    # ... run app ...
    await snapshotter.stop()        # shutdown

The ``path`` argument serves as a **dir + stem template** (not a literal file
path).  The actual file written is ``{parent}/{stem}-YYYY-MM-DD.jsonl`` using
the date of each write call.  This mirrors the access log's per-day rotation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TrafficSnapshotter:
    """Periodic cumulative snapshot writer for a :class:`TrafficLedger`.

    Parameters
    ----------
    ledger : TrafficLedger or None
        The in-memory ledger to snapshot. May be ``None`` (e.g. traffic
        accounting disabled) — :meth:`start` and :meth:`stop` then no-op.
    interval_s : int
        Seconds between periodic snapshots (validated >= 1 by config layer).
    path : str
        **Dir + stem template** (not literal file path).  The actual file
        written each day is ``{parent(stem)}-YYYY-MM-DD.jsonl``.
        Example: ``"logs/traffic-snapshot.jsonl"`` produces
        ``logs/traffic-snapshot-2026-07-29.jsonl``.
    """

    __slots__ = (
        "_ledger",
        "_interval_s",
        "_dir",
        "_stem",
        "_boot_ts",
        "_run_id",
        "_pid",
        "_start_monotonic",
        "_task",
    )

    def __init__(
        self,
        *,
        ledger: Any,  # TrafficLedger (avoid circular dep at type level)
        interval_s: int,
        path: str,
    ) -> None:
        self._ledger = ledger
        self._interval_s = interval_s
        # Derive dir + stem from the path template.
        path_obj = Path(path)
        self._dir: Path = path_obj.parent
        self._stem: str = path_obj.stem
        self._boot_ts: str = datetime.now().astimezone().isoformat()
        self._run_id: str = uuid.uuid4().hex[:16]
        self._pid: int = os.getpid()
        self._start_monotonic: float = time.monotonic()
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic snapshot loop.

        Idempotent — if a background task is already running this is a no-op.

        Writes an immediate first frame synchronously.  **If the first frame
        write fails** (I/O error, mkdir failure, …), the snapshotter stays
        **inactive**: no background task is created, :attr:`active` is
        ``False``, and :meth:`stop` returns immediately.  A warning is logged.

        If the ledger is ``None`` or disabled (``enabled is False``), no
        frames are written and no background task is created.
        """
        if self._task is not None and not self._task.done():
            return  # already running
        if self._ledger is None or not self._ledger.enabled:
            return  # ledger inactive — nothing to snapshot

        # Write the first frame synchronously.  If it fails we stay inactive
        # — no background task, no retry, honest inactive state.
        if not self._write_once():
            logger.warning(
                "traffic snapshot first frame failed — staying inactive; "
                "path=%s, dir=%s, stem=%s",
                self._path_repr(),
                self._dir,
                self._stem,
            )
            return

        # Start the background loop.
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the periodic snapshot loop and write a final frame.

        Idempotent — safe to call multiple times.  The final state is written
        *after* the background task is fully stopped (or if it never ran),
        ensuring the last cumulative ledger state is captured on disk even
        if the loop had previously errored out.

        If the snapshotter is **inactive** (never started, first-frame write
        failed, or ledger disabled), this returns immediately without writing.
        """
        task = self._task
        if task is None:
            return  # never started or first-frame write failed — inactive

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

        # Always attempt a final snapshot (even if the task previously
        # errored).  This guarantees the shutdown state is captured.
        if self._ledger is not None and self._ledger.enabled:
            self._write_once()  # best-effort, return value ignored

    @property
    def active(self) -> bool:
        """``True`` while the background snapshot loop is running."""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Background asyncio loop: sleep → snapshot → repeat.

        Every iteration is individually exception-guarded so that a single
        I/O or serialization error never kills the entire loop.  Only
        :class:`asyncio.CancelledError` propagates (task cancellation).
        """
        while True:
            try:
                await asyncio.sleep(self._interval_s)
                self._write_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "traffic snapshot iteration failed",
                    exc_info=True,
                )

    def _daily_path(self) -> Path:
        """Return the daily-rotated output path for today."""
        return self._dir / f"{self._stem}-{date.today().isoformat()}.jsonl"

    def _path_repr(self) -> str:
        """Human-friendly representation of the path template."""
        return str(self._dir / f"{self._stem}.jsonl")

    def _write_once(self) -> bool:
        """Snapshot the ledger and append one JSON line to the daily file.

        Returns ``True`` on success, ``False`` on failure (I/O / serialization
        error).  Failures are logged as warnings but never propagated.
        Silent no-op when the ledger snapshot reports ``enabled=False``
        (safety net — returns ``True`` because nothing went wrong).
        """
        try:
            snap = self._ledger.snapshot()  # type: ignore[union-attr]
        except Exception:
            logger.warning(
                "traffic snapshot ledger.snapshot() failed",
                exc_info=True,
            )
            return False

        if not snap.get("enabled"):
            return True

        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(),
            "bootTs": self._boot_ts,
            "runId": self._run_id,
            "uptimeS": time.monotonic() - self._start_monotonic,
            "pid": self._pid,
            "enabled": snap.get("enabled", True),
            "buckets": snap.get("buckets", {}),
            "totals": snap.get("totals", {}),
            "ratios": snap.get("ratios", {}),
        }

        path = self._daily_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning(
                "traffic snapshot mkdir failed for %s",
                path.parent,
                exc_info=True,
            )
            # Continue — the open will fail too, which is caught below.

        try:
            with path.open("a", encoding="utf-8") as f:
                # Single write call: serialise + append the newline into one
                # string before writing (P1-27). Two separate write() calls
                # left a crash window between them that produced a half-line
                # (no trailing "\n"), which broke offline json.loads on the
                # whole file. One call collapses that window to the OS-level
                # write atomicity (small writes are effectively atomic under
                # POSIX), so a crash at any point leaves either the prior
                # complete line or the new complete line — never a half-line.
                #
                # We deliberately do NOT fsync here: the per-frame cost of a
                # synchronous disk flush every interval (default 300s, plus
                # shutdown final frame) is not justified for best-effort
                # cumulative snapshots that are already redundant with the
                # in-memory ledger (lost frames are recoverable from the
                # surrounding frames by delta derivation). The OS page cache
                # + the close()-on-exit flush is the chosen durability /
                # performance trade-off; fsync would be the lever if a
                # stronger power-loss guarantee were ever required.
                line = json.dumps(record, separators=(",", ":")) + "\n"
                f.write(line)
            return True
        except Exception:
            logger.warning(
                "traffic snapshot write failed for %s",
                path,
                exc_info=True,
            )
            return False
