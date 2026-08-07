"""Tests for :class:`oc_slimapi.traffic_snapshot.TrafficSnapshotter`.

Covers the periodic cumulative snapshot loop: idempotent start/stop,
final-state guarantee, exception convergence, JSONL format, monotonic clock
usage, best-effort I/O handling, daily-rotated filenames, true inactive
semantics on first-frame failure, and accurate ledger data in snapshot rows.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path

import pytest

from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.traffic_snapshot import TrafficSnapshotter

# NOTE: Both TrafficSnapshotter and TrafficLedger use __slots__ —
# monkeypatch on the *class*, not the instance, to avoid AttributeError
# during undo teardown.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ledger_with_data() -> TrafficLedger:
    """Return a TrafficLedger with a few recorded requests."""
    ledger = TrafficLedger(enabled=True)
    ledger.record_downstream(
        bucket="health",
        method="GET",
        status=200,
        req_bytes=100,
        resp_bytes=200,
        duration_ms=5.0,
    )
    ledger.record_downstream(
        bucket="messages",
        method="GET",
        status=200,
        req_bytes=300,
        resp_bytes=4000,
        duration_ms=15.0,
    )
    ledger.record_upstream(
        bucket="messages",
        method="GET",
        status=200,
        req_bytes=500,
        resp_bytes=6000,
    )
    return ledger


def _snapshot_path(base_path: str | Path) -> Path:
    """Resolve the actual daily-rotated snapshot path from the constructor path.

    The constructor ``path`` parameter is a dir+stem template
    (e.g. ``logs/traffic-snapshot.jsonl`` → stem ``traffic-snapshot``),
    and the actual file written today is
    ``{dir}/{stem}-{YYYY-MM-DD}.jsonl``.
    """
    p = Path(base_path)
    today = datetime.date.today().isoformat()
    return p.parent / f"{p.stem}-{today}.jsonl"


def _read_lines(base_path: str | Path) -> list[dict]:
    """Read all JSON lines from today's snapshot file. Returns [] if missing."""
    p = _snapshot_path(base_path)
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# start writes first frame immediately
# ---------------------------------------------------------------------------


class TestStartFirstFrame:
    """start() writes an immediate first frame."""

    async def test_writes_first_frame(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()
        await snap.stop()

        lines = _read_lines(path)
        assert len(lines) == 2  # first frame + stop final frame

        first = lines[0]
        assert first["enabled"] is True
        assert isinstance(first["uptimeS"], float)
        assert first["uptimeS"] >= 0
        assert "ts" in first
        assert "bootTs" in first
        assert "runId" in first
        assert "pid" in first
        assert "buckets" in first
        assert "totals" in first
        assert "ratios" in first
        assert first["totals"]["requests"] == 2

    async def test_all_fields_present(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = TrafficLedger(enabled=True)
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()
        await snap.stop()

        lines = _read_lines(path)
        assert len(lines) >= 1
        for line in lines:
            # Every line must have the full schema
            assert isinstance(line["ts"], str)
            assert isinstance(line["bootTs"], str)
            assert isinstance(line["runId"], str)
            assert isinstance(line["uptimeS"], (int, float))
            assert isinstance(line["pid"], int)
            assert isinstance(line["enabled"], bool)
            assert isinstance(line["buckets"], dict)
            assert isinstance(line["totals"], dict)
            assert isinstance(line["ratios"], dict)


# ---------------------------------------------------------------------------
# Periodic writes
# ---------------------------------------------------------------------------


class TestPeriodicWrites:
    """Snapshots are written periodically by the background loop."""

    async def test_lines_increase_over_time(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)
        await snap.start()  # writes frame 1 synchronously
        assert len(_read_lines(path)) == 1

        await asyncio.sleep(0.12)  # 2+ periodic cycles
        n = len(_read_lines(path))
        assert n >= 3, f"expected >= 3 lines after 0.12s, got {n}"

        await snap.stop()

    async def test_uptime_monotonic(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)
        await snap.start()
        await asyncio.sleep(0.15)
        await snap.stop()

        lines = _read_lines(path)
        uptimes = [line["uptimeS"] for line in lines]
        for i in range(1, len(uptimes)):
            assert uptimes[i] > uptimes[i - 1], (
                f"uptimeS must be strictly monotonic: {uptimes}"
            )


# ---------------------------------------------------------------------------
# Stop final state + idempotency
# ---------------------------------------------------------------------------


class TestStopFinalState:
    """stop() writes a final-state frame and is idempotent."""

    async def test_final_frame_written(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)
        await snap.start()
        await asyncio.sleep(0.05)
        before = len(_read_lines(path))
        await snap.stop()
        after = len(_read_lines(path))
        assert after == before + 1, (
            f"stop must write one final frame: before={before}, after={after}"
        )

    async def test_stop_idempotent(self, tmp_path: Path) -> None:
        """Second stop must not crash and must not add extra lines (幂等)."""
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()
        await snap.stop()
        n1 = len(_read_lines(path))
        await snap.stop()  # second stop — inactive, returns immediately
        n2 = len(_read_lines(path))
        assert n1 == n2, "second stop must not add extra lines (inactive)"

    async def test_stop_without_start(self, tmp_path: Path) -> None:
        """Calling stop() without start() is a safe no-op (inactive)."""
        ledger = _make_ledger_with_data()
        path = str(tmp_path / "snap.jsonl")
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.stop()
        assert not _snapshot_path(path).exists(), (
            "stop without start must not write anything"
        )

    async def test_stop_after_task_dead(self, tmp_path: Path) -> None:
        """Stop writes final state even if the background task already died."""
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()  # frame 1

        # Kill the background task manually
        assert snap._task is not None
        snap._task.cancel()
        try:
            await snap._task
        except asyncio.CancelledError:
            pass

        # Task is now dead. stop() must still write a final frame.
        await snap.stop()
        lines = _read_lines(path)
        assert len(lines) == 2, (
            f"expected 2 lines (first + final), got {len(lines)}"
        )


# ---------------------------------------------------------------------------
# Start idempotent
# ---------------------------------------------------------------------------


class TestStartIdempotent:
    """start() is idempotent — no duplicate task on re-call."""

    async def test_double_start_no_duplicate_task(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()
        task1 = snap._task
        await snap.start()  # should be no-op
        task2 = snap._task
        assert task1 is task2, "second start must not create a new task"
        await snap.stop()


# ---------------------------------------------------------------------------
# Ledger disabled — no writes
# ---------------------------------------------------------------------------


class TestLedgerDisabled:
    """Disabled or None ledger produces no snapshot lines."""

    async def test_disabled_ledger_writes_nothing(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = TrafficLedger(enabled=False)
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        await snap.start()
        assert len(_read_lines(path)) == 0, "disabled ledger must not write"
        assert snap.active is False

        await asyncio.sleep(0.1)
        assert len(_read_lines(path)) == 0

        await snap.stop()
        assert len(_read_lines(path)) == 0

    async def test_none_ledger_writes_nothing(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        snap = TrafficSnapshotter(ledger=None, interval_s=0.05, path=path)

        await snap.start()
        assert len(_read_lines(path)) == 0
        assert snap.active is False

        await snap.stop()
        assert len(_read_lines(path)) == 0


# ---------------------------------------------------------------------------
# First-frame failure → inactive (阻断5)
# ---------------------------------------------------------------------------


class TestFirstFrameFailure:
    """If the first _write_once call fails, snapshotter stays inactive."""

    async def test_open_failure_stays_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        def broken_open(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "open", broken_open)

        with caplog.at_level(logging.WARNING):
            await snap.start()

        assert snap.active is False, "must stay inactive after failed first frame"
        assert snap._task is None, "must not create a background task"
        assert any("first frame failed" in msg for msg in caplog.messages)

        # Stop must be a safe no-op when inactive.
        await snap.stop()
        assert snap.active is False

    async def test_snapshot_exception_stays_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When ledger.snapshot() throws, start() stays inactive."""
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        def broken_snapshot(self):
            raise RuntimeError("ledger corrupted")

        monkeypatch.setattr(TrafficLedger, "snapshot", broken_snapshot)

        with caplog.at_level(logging.WARNING):
            await snap.start()

        assert snap.active is False
        await snap.stop()  # must not crash


# ---------------------------------------------------------------------------
# Background loop exception convergence
# ---------------------------------------------------------------------------


class TestExceptionConvergence:
    """Per-iteration exceptions are caught; the loop continues."""

    async def test_write_once_exception_recovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flaky _write_once must not kill the background loop."""
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        call_count: list[int] = [0]
        original_write = TrafficSnapshotter._write_once

        def flaky_write(self):
            call_count[0] += 1
            if call_count[0] == 2:  # second call (first periodic) throws
                raise RuntimeError("simulated disk error")
            return original_write(self)

        monkeypatch.setattr(TrafficSnapshotter, "_write_once", flaky_write)
        await snap.start()  # call 1 — writes frame
        await asyncio.sleep(0.12)  # at least 2 more cycles (call 2 throws, call 3 recovers)
        await snap.stop()

        lines = _read_lines(path)
        assert len(lines) >= 2, (
            f"must survive a _write_once exception; "
            f"got {len(lines)} lines, call_count={call_count[0]}"
        )


# ---------------------------------------------------------------------------
# Stop after loop task error
# ---------------------------------------------------------------------------


class TestStopAfterTaskError:
    """stop() writes final state even if the background loop errored."""

    async def test_stop_after_loop_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        async def broken_loop(self):
            raise RuntimeError("loop crashed on first iteration")

        monkeypatch.setattr(TrafficSnapshotter, "_loop", broken_loop)
        await snap.start()  # writes frame 1, then creates broken task

        # Let the event loop run so the broken task can crash.
        if snap._task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(snap._task), timeout=0.5
                )
            except (asyncio.TimeoutError, RuntimeError):
                pass

        n_before = len(_read_lines(path))
        await snap.stop()
        n_after = len(_read_lines(path))
        assert n_after == n_before + 1, (
            f"stop must write final state after a crashed loop; "
            f"before={n_before}, after={n_after}"
        )


# ---------------------------------------------------------------------------
# Parent directory auto-creation
# ---------------------------------------------------------------------------


class TestMkdir:
    """Parent directory created automatically (best-effort)."""

    async def test_deep_path_created(self, tmp_path: Path) -> None:
        path = str(tmp_path / "a" / "b" / "c" / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        await snap.start()
        await snap.stop()
        lines = _read_lines(path)
        assert len(lines) >= 1, "deep parent dir must be auto-created"


# ---------------------------------------------------------------------------
# Write / mkdir failure does not throw
# ---------------------------------------------------------------------------


class TestWriteErrorHandling:
    """I/O failures must warn, not crash the service."""

    async def test_mkdir_failure_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        def broken_mkdir(self, *args: object, **kwargs: object) -> None:
            raise PermissionError("simulated mkdir failure")

        monkeypatch.setattr(Path, "mkdir", broken_mkdir)

        # Should not raise; warning is logged internally.  The file write
        # still succeeds because tmp_path already exists as parent dir.
        await snap.start()
        assert snap.active is True
        await snap.stop()


# ---------------------------------------------------------------------------
# Daily rotation
# ---------------------------------------------------------------------------


class TestDailyRotation:
    """Files are split per day using the <stem>-YYYY-MM-DD.jsonl pattern."""

    async def test_same_day_same_file(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        await snap.start()
        await asyncio.sleep(0.12)
        await snap.stop()

        daily = _snapshot_path(path)
        assert daily.exists(), f"daily file {daily} must exist"
        lines = _read_lines(path)
        assert len(lines) >= 3

    async def test_cross_day_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Frames across a date boundary land in different daily files."""
        # Replace date.today with a controllable fake.
        class _FakeDate(datetime.date):
            _current = datetime.date(2026, 7, 29)

            @classmethod
            def today(cls):
                return cls._current

        monkeypatch.setattr("oc_slimapi.traffic_snapshot.date", _FakeDate)

        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        await snap.start()  # writes to snap-2026-07-29.jsonl
        await asyncio.sleep(0.06)

        # Advance to next day.
        _FakeDate._current = datetime.date(2026, 7, 30)
        await asyncio.sleep(0.06)

        await snap.stop()  # writes to snap-2026-07-30.jsonl

        day1 = tmp_path / "snap-2026-07-29.jsonl"
        day2 = tmp_path / "snap-2026-07-30.jsonl"
        assert day1.exists(), f"{day1} must exist"
        assert day2.exists(), f"{day2} must exist"

        lines1 = [json.loads(l) for l in day1.read_text().splitlines() if l.strip()]
        lines2 = [json.loads(l) for l in day2.read_text().splitlines() if l.strip()]
        assert len(lines1) >= 1
        assert len(lines2) >= 1


# ---------------------------------------------------------------------------
# P1-27: single write call — no half-line on crash
# ---------------------------------------------------------------------------


class _CountingFile:
    """Wrap a text file obj to record each ``write`` call's argument.

    Used to assert (P1-27) that a snapshot frame is emitted via a SINGLE
    ``write(json + "\n")`` call rather than two separate ``write(json)`` /
    ``write("\n")`` calls (the pre-fix shape that could leave a half-line
    on crash). Optionally raises on write to simulate a mid-write crash.

    Context-manager dunders are defined explicitly because Python looks up
    dunder methods on the type, not the instance — ``__getattr__`` would
    not be consulted for ``__enter__``/``__exit__``.
    """

    def __init__(self, fh, *, writes: list[str], raise_on_write: bool = False):
        self._fh = fh
        self._writes = writes
        self._raise_on_write = raise_on_write
        self.name = getattr(fh, "name", "")

    def write(self, s):
        self._writes.append(s)
        if self._raise_on_write:
            raise OSError("simulated crash during write")
        return self._fh.write(s)

    def __enter__(self):
        # Mirror the wrapped file's context-manager enter so the snapshotter's
        # ``with path.open(...) as f:`` block works.
        self._fh.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._fh.__exit__(exc_type, exc, tb)

    def __getattr__(self, item):
        return getattr(self._fh, item)


class TestSingleWriteAtomicity:
    """P1-27: a frame is written via ONE ``write()`` call, not two.

    Pre-P1-27 the code did ``f.write(json); f.write("\n")`` — a crash
    between them left a line without a trailing newline, breaking offline
    ``json.loads`` of the whole file (a half-line has no newline delimiter
    and merges with the next line on read). The fix collapses both into a
    single ``write(json + "\n")`` so a crash at any point leaves either the
    prior complete line or the new complete line, never a half-line.
    """

    async def test_single_write_call_per_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        writes: list[str] = []
        real_open = Path.open

        def spy_open(self, *args, **kwargs):
            fh = real_open(self, *args, **kwargs)
            # Only wrap the daily snapshot file; leave other I/O alone.
            if "snap-" in self.name and self.name.endswith(".jsonl"):
                return _CountingFile(fh, writes=writes)
            return fh

        monkeypatch.setattr(Path, "open", spy_open)
        await snap.start()
        await snap.stop()

        assert len(writes) >= 1, "expected at least one frame written"
        # Every captured write must be a COMPLETE line (json + trailing
        # newline) — never a bare json blob without the newline. Pre-fix
        # the json and "\n" were two separate writes, the first of which
        # would NOT end in "\n".
        for w in writes:
            assert w.endswith("\n"), (
                f"P1-27 violation: write missing trailing newline: {w!r}"
            )
        # And there is exactly one write per frame (start frame + stop frame).
        assert len(writes) == 2, (
            f"P1-27: expected exactly 1 write call per frame (2 frames = 2 "
            f"writes), got {len(writes)} writes: {writes!r}"
        )

    async def test_no_half_line_after_simulated_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-27: a crash DURING the single write leaves the file with the
        prior complete lines only (no half-line appended). Pre-fix, a crash
        between the two writes left a bare json blob without a newline,
        corrupting the file for offline parsing."""
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        # Write one good frame first (so we have a known-good prior line).
        await snap.start()
        await snap.stop()
        daily = _snapshot_path(path)
        raw_before = daily.read_text()
        assert raw_before.endswith("\n")  # the prior file is well-formed

        real_open = Path.open

        def crashing_open(self, *args, **kwargs):
            fh = real_open(self, *args, **kwargs)
            if "snap-" in self.name and self.name.endswith(".jsonl"):
                return _CountingFile(fh, writes=[], raise_on_write=True)
            return fh

        monkeypatch.setattr(Path, "open", crashing_open)

        # A direct _write_once with the crash must (a) return False and
        # (b) leave the file content unchanged (no half-line appended).
        result = snap._write_once()
        assert result is False, "crashed write must report failure"

        raw_after = daily.read_text()
        assert raw_after == raw_before, (
            "file must be unchanged after a crashed write (no half-line)"
        )
        # Every line still parses as valid JSON — the file is not corrupted.
        for line in raw_after.splitlines():
            if line.strip():
                json.loads(line)


# ---------------------------------------------------------------------------
# Identity stability
# ---------------------------------------------------------------------------


class TestIdentityStability:
    """bootTs and runId are stable per instance; differ across instances."""

    async def test_same_instance_same_ids(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        await snap.start()
        await asyncio.sleep(0.1)
        await snap.stop()

        lines = _read_lines(path)
        boot_ts = lines[0]["bootTs"]
        run_id = lines[0]["runId"]
        for line in lines[1:]:
            assert line["bootTs"] == boot_ts
            assert line["runId"] == run_id

    async def test_different_instances_different_run_id(
        self, tmp_path: Path
    ) -> None:
        path1 = str(tmp_path / "snap1.jsonl")
        path2 = str(tmp_path / "snap2.jsonl")
        ledger = _make_ledger_with_data()

        snap1 = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path1)
        snap2 = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path2)

        await snap1.start()
        await snap2.start()
        await snap1.stop()
        await snap2.stop()

        lines1 = _read_lines(path1)
        lines2 = _read_lines(path2)
        assert lines1[0]["runId"] != lines2[0]["runId"]


# ---------------------------------------------------------------------------
# runId length (64-bit → 16 hex chars)
# ---------------------------------------------------------------------------


class TestRunIdLength:
    """runId is 64-bit → 16 hex characters."""

    async def test_run_id_attribute_length(self, tmp_path: Path) -> None:
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(
            ledger=ledger, interval_s=300, path=str(tmp_path / "snap.jsonl")
        )
        assert len(snap._run_id) == 16, (
            f"expected 16-char runId, got {len(snap._run_id)}: {snap._run_id!r}"
        )

    async def test_run_id_in_snapshot(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)
        await snap.start()
        await snap.stop()
        lines = _read_lines(path)
        assert len(lines[0]["runId"]) == 16


# ---------------------------------------------------------------------------
# JSON parsability
# ---------------------------------------------------------------------------


class TestJsonParsable:
    """Every line in the snapshot file is valid JSON."""

    async def test_each_line_parsable(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        await snap.start()
        await asyncio.sleep(0.15)
        await snap.stop()

        daily = _snapshot_path(path)
        raw = daily.read_text()
        for i, line in enumerate(raw.splitlines(), 1):
            parsed = json.loads(line)  # raises on invalid JSON
            assert isinstance(parsed, dict), f"line {i} is not a dict"


# ---------------------------------------------------------------------------
# Real ledger data reflected in snapshot
# ---------------------------------------------------------------------------


class TestRealLedgerData:
    """Snapshot rows contain correct cumulative ledger data."""

    async def test_buckets_present(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        await snap.start()
        await snap.stop()

        lines = _read_lines(path)
        first = lines[0]
        assert "health" in first["buckets"]
        assert "messages" in first["buckets"]
        assert first["totals"]["requests"] == 2
        assert first["buckets"]["health"]["requests"] == 1
        assert first["buckets"]["messages"]["requests"] == 1
        assert first["buckets"]["messages"]["upIn"] == 6000
        assert first["buckets"]["messages"]["upOut"] == 500

    async def test_ratios_included(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=300, path=path)

        await snap.start()
        await snap.stop()

        lines = _read_lines(path)
        first = lines[0]
        # messages bucket has upIn > 0 → ratio should exist
        assert "messages" in first["ratios"]
        ratio = first["ratios"]["messages"]["downOutOverUpIn"]
        assert ratio == 4000 / 6000
        # health has no upstream bytes → no ratio entry
        assert "health" not in first["ratios"]


# ---------------------------------------------------------------------------
# Active property
# ---------------------------------------------------------------------------


class TestActiveProperty:
    """active property accurately reflects background task state."""

    async def test_active_while_running(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = _make_ledger_with_data()
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        assert snap.active is False
        await snap.start()
        assert snap.active is True
        await snap.stop()
        assert snap.active is False

    async def test_active_with_disabled_ledger(self, tmp_path: Path) -> None:
        path = str(tmp_path / "snap.jsonl")
        ledger = TrafficLedger(enabled=False)
        snap = TrafficSnapshotter(ledger=ledger, interval_s=0.05, path=path)

        assert snap.active is False
        await snap.start()
        assert snap.active is False  # no task created
