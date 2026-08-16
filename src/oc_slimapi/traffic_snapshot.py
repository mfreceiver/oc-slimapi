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
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task 10 (P2-1): prune old daily snapshot files
# ---------------------------------------------------------------------------


def _snapshot_file_re(stem: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})\.jsonl(\.gz)?$")


def prune_old_snapshots(directory: Path, stem: str, retain_days: int, today: date) -> int:
    if retain_days <= 0:
        return 0
    deadline = date.fromordinal(today.toordinal() - retain_days)
    pattern = _snapshot_file_re(stem)
    count = 0
    for p in directory.glob(f"{stem}-*.jsonl*"):
        m = pattern.match(p.name)
        if not m:
            continue
        try:
            file_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if file_date < deadline:
            try:
                p.unlink()
                count += 1
            except OSError:
                logger.warning(
                    "traffic-snapshot prune: failed to remove %s", p, exc_info=True,
                )
    return count


# ---------------------------------------------------------------------------
# v3 observability aggregation (v3-contract §9.2, Batch A) — pure analysis
# ---------------------------------------------------------------------------

# §9.2 sseActive dims. rejected/exempt have no SSE endpoints — always 0.
_SSE_DIMS: tuple[str, ...] = ("v2", "v3", "absent", "not_applicable")


def _v3_row_key(row: dict) -> str:
    """Flat §9.2 matrix key for one access-log row."""
    status = row.get("status")
    status_class = "none" if isinstance(status, bool) or not isinstance(status, int) else f"{status // 100}xx"
    return "|".join((
        str(row.get("selectorResult") or "null"),
        str(row.get("wireVersion") or "null"),
        str(row.get("directoryForm") or "null"),
        str(row.get("recordType") or "request"),
        status_class,
        str(row.get("bucket") or "null"),
    ))


def aggregate_v3_observability(records: list[dict]) -> dict:
    """Aggregate parsed access-log rows into the §9.2 matrix + sseActive series.

    Input: chronological (append-ordered, as written by the access log) rows
    — any ``recordType`` (request / sse_open / sse_close). Rows from BEFORE
    the v3 upgrade simply lack the new fields and land in the ``null`` dims —
    the additive-fields contract (§9.1) means consumers tolerate exactly that.

    Output shape (all day-scoped maps keyed ``"YYYY-MM-DD"``):

    * ``counts``: cumulative flat-key ``selectorResult|wireVersion|
      directoryForm|recordType|statusClass|bucket`` → count (all days).
    * ``countsByDate``: same keys per day.
    * ``sseActive[date][dim]``: **window-start** live SSE stock for each day —
      the first row seen on a date freezes the running stock as that day's
      opening balance. Satisfies the §9.2 formula
      ``sseActive[D+1,k] = sseActive[D,k] + sse_open[D,k] − matched_sse_close[D,k]``.
    * ``sseOpens`` / ``sseMatchedCloses`` per (date, dim): lifecycle pairing
      by ``lifecycleId`` (§11.8) — a close matches iff its id is in the
      dim's still-unmatched open set (pairing crosses day boundaries:
      cross-day streams carry; the match counts on the close's day). A
      matching close is removed from the set; a close whose id is unknown
      (restart emptied the set / open predates the window / id missing) is
      an **orphan** — counted, stock untouched.
    * ``sseOrphanCloses``: closes with no pairable prior open — never
      decrement ``sseActive`` (孤儿补记 close 校正; a mismatched close must
      not drain another live connection's slot).
    * ``sseLive[dim]``: end-of-window running stock.
    """
    counts: dict[str, int] = {}
    counts_by_date: dict[str, dict[str, int]] = {}
    day_order: list[str] = []
    opens: dict[str, dict[str, int]] = {}
    matched: dict[str, dict[str, int]] = {}
    orphan: dict[str, dict[str, int]] = {}
    active: dict[str, int] = {}
    day_start_stock: dict[str, dict[str, int]] = {}
    # §11.8 pairing state: per-dim set of open-but-not-yet-closed lifecycle
    # ids visible in the aggregation window.
    open_ids: dict[str, set[int]] = {}

    def _bump(target: dict, date: str, dim: str) -> None:
        per_day = target.setdefault(date, {})
        per_day[dim] = per_day.get(dim, 0) + 1

    def _full_dims(stock: dict[str, int]) -> dict[str, int]:
        # jq-friendly stability: every day's map carries all four dims.
        return {dim: stock.get(dim, 0) for dim in _SSE_DIMS}

    for row in records:
        date = str(row.get("ts", ""))[:10] or "unknown"
        if date not in counts_by_date:
            counts_by_date[date] = {}
            day_order.append(date)
            # Window-start stock: freeze the running live stock at the FIRST
            # row of the date (this IS the carry-in from the previous day).
            day_start_stock[date] = _full_dims(active)
            # jq-friendly: every day carries all four dims in the lifecycle
            # maps, even all-zero.
            opens[date] = _full_dims({})
            matched[date] = _full_dims({})
            orphan[date] = _full_dims({})
        key = _v3_row_key(row)
        counts[key] = counts.get(key, 0) + 1
        counts_by_date[date][key] = counts_by_date[date].get(key, 0) + 1

        record_type = row.get("recordType") or "request"
        if record_type in ("sse_open", "sse_close"):
            dim = row.get("selectorResult")
            dim = dim if dim in _SSE_DIMS else "absent"
            lifecycle_id = row.get("lifecycleId")
            if record_type == "sse_open":
                active[dim] = active.get(dim, 0) + 1
                _bump(opens, date, dim)
                if isinstance(lifecycle_id, int):
                    open_ids.setdefault(dim, set()).add(lifecycle_id)
            else:
                # §11.8 pairing: match by lifecycleId within the dim — a
                # stock-count decrement would let an unmatched close drain
                # another live connection's slot.
                ids = open_ids.get(dim)
                if isinstance(lifecycle_id, int) and ids and lifecycle_id in ids:
                    ids.discard(lifecycle_id)
                    active[dim] -= 1
                    _bump(matched, date, dim)
                else:
                    _bump(orphan, date, dim)

    return {
        "counts": counts,
        "countsByDate": counts_by_date,
        "sseActive": day_start_stock,
        "sseOpens": opens,
        "sseMatchedCloses": matched,
        "sseOrphanCloses": orphan,
        "sseLive": _full_dims(active),
    }


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

    def _path_repr(self) -> str:
        """Human-friendly representation of the path template."""
        return str(self._dir / f"{self._stem}.jsonl")

    def _write_once(self) -> bool:
        """Snapshot the ledger and append one JSON line to the daily file.

        Returns ``True`` on success, ``False`` on failure (I/O / serialization
        error).  Failures are logged as warnings but never propagated.
        Silent no-op when the ledger snapshot reports ``enabled=False``
        (safety net — returns ``True`` because nothing went wrong).

        **Single time sample point (P1-26)**: the wall-clock ``now`` is
        captured once at the top of the call and used to derive BOTH the
        record's ``ts`` field AND the daily output path's date. Previously
        these were two separate samples (``datetime.now()`` for ``ts`` and
        ``date.today()`` for the path), which could straddle midnight and
        assign a near-midnight frame to the wrong daily file (the ``ts``
        field said day N+1 but the file name said day N). One sample
        eliminates the cross-midnight mis-bucketing window.
        """
        # Single sampling point for the whole frame.
        now = datetime.now().astimezone()

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
            "ts": now.isoformat(),
            "bootTs": self._boot_ts,
            "runId": self._run_id,
            "uptimeS": time.monotonic() - self._start_monotonic,
            "pid": self._pid,
            "enabled": snap.get("enabled", True),
            "buckets": snap.get("buckets", {}),
            "totals": snap.get("totals", {}),
            "ratios": snap.get("ratios", {}),
            # v3 §9 (long-term retirement evidence): the daily JSONL is the
            # ONLY ≥7-day carrier of the selector/sseActive evidence (the
            # access log retains ~3 days), so every frame carries the
            # same-source v3 node from ledger.snapshot() — matrix (7-dim
            # flat counters) / sseLifecycle (per-dim open-close pairing) /
            # sseActive (4-dim live stock). Additive tail: legacy field
            # names and order unchanged.
            "v3": snap.get(
                "v3", {"matrix": {}, "sseLifecycle": {}, "sseActive": {}}),
        }

        # Derive the daily path from the SAME `now` so the ts field and the
        # file name always agree on a date (single sample point).
        path = self._dir / f"{self._stem}-{now.date().isoformat()}.jsonl"
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
