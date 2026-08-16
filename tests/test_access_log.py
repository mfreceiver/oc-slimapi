"""Unit tests for ``oc_slimapi.access_log`` (daily-rotated JSON-lines access log).

Covers :func:`setup_access_log` (DailyAccessHandler install, best-effort
failure, idempotent re-init), :func:`write_access_log` (new fields, disabled
no-op), :func:`hash_client_id`, :func:`compress_old_access_logs`,
:func:`prune_old_access_logs`, :func:`migrate_legacy_access_log`,
:class:`DailyAccessHandler`, and :func:`run_access_log_maintenance_loop`.

The access logger (``oc_slimapi.access``) is a process-global named logger.
An autouse fixture resets its handler/disabled state after every test so no
file handle leaks into other test modules.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from oc_slimapi.access_log import (
    DailyAccessHandler,
    compress_old_access_logs,
    get_access_logger,
    hash_client_id,
    migrate_legacy_access_log,
    prune_old_access_logs,
    run_access_log_maintenance_loop,
    setup_access_log,
    write_access_log,
)

from oc_slimapi.logging_config import setup_logging


def _local_midnight_ts(d: date) -> float:
    """Return the local-timezone epoch timestamp for midnight of *d*."""
    return datetime(d.year, d.month, d.day).timestamp()


def _make_record(msg: str, target_date: date | None = None) -> logging.LogRecord:
    """Build a LogRecord whose ``.created`` maps to *target_date*.

    When *target_date* is ``None``, ``created`` is left as the real current
    time (default logging behaviour).
    """
    record = logging.LogRecord(
        "oc_slimapi.access", logging.INFO, "", 0, msg, (), None,
    )
    if target_date is not None:
        record.created = _local_midnight_ts(target_date)
    return record


@pytest.fixture(autouse=True)
def _reset_access_logger():
    """Tear down any handlers the test installed so nothing leaks across tests."""
    yield
    logger = get_access_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.disabled = False


def _flush(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _kwargs():
    return dict(
        method="GET",
        path="/slimapi/messages/ses_x",
        bucket="messages",
        status=200,
        duration_ms=12.3456,
        down_in=5,
        down_out=7,
        up_in=9,
        up_out=11,
        request_id="req-123",
    )


def _kwargs_with_client():
    return dict(
        **_kwargs(),
        client="ocdroid",
        client_ver="2.1.0",
        client_id="abc123",
    )


# ---------------------------------------------------------------------------
# 1. DailyAccessHandler: basic write & read-back
# ---------------------------------------------------------------------------


def test_daily_handler_one_line(tmp_path):
    logger = get_access_logger()
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info('{"msg": "hello"}')
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"msg": "hello"}


def test_daily_handler_multi_lines_same_day(tmp_path):
    logger = get_access_logger()
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info('{"n": 1}')
    logger.info('{"n": 2}')
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1
    assert json.loads(lines[1])["n"] == 2


# ---------------------------------------------------------------------------
# 2. DailyAccessHandler: cross-day switching (mocked)
# ---------------------------------------------------------------------------


def test_daily_handler_cross_day_switch(tmp_path):
    """Simulate two days: first write on day 1, then write on day 2.

    After the switch, day 1's file should contain one line and day 2's file
    should contain one line. (The old file handle is closed before opening the
    new one, so both survive.)

    Uses ``_make_record`` with explicit ``target_date`` to override
    ``record.created`` (the handler derives the file date from that field,
    not from ``date.today()``).
    """
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))

    day1 = date(2026, 7, 28)
    day2 = date(2026, 7, 29)

    handler.emit(_make_record('{"day": 1}', target_date=day1))
    handler.emit(_make_record('{"day": 2}', target_date=day2))
    handler.flush()

    f1 = tmp_path / "access-2026-07-28.jsonl"
    f2 = tmp_path / "access-2026-07-29.jsonl"

    assert f1.exists(), f"Expected {f1} to exist"
    assert f2.exists(), f"Expected {f2} to exist"

    lines1 = f1.read_text(encoding="utf-8").splitlines()
    lines2 = f2.read_text(encoding="utf-8").splitlines()

    assert len(lines1) == 1
    assert len(lines2) == 1
    assert json.loads(lines1[0])["day"] == 1
    assert json.loads(lines2[0])["day"] == 2


# ---------------------------------------------------------------------------
# 3. setup_access_log  enabled + write → one line, all fields
# ---------------------------------------------------------------------------


def test_write_emits_one_json_line_with_all_fields(tmp_path):
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    write_access_log(logger, **_kwargs())
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    expected_keys = {
        "ts", "method", "path", "bucket", "status", "durationMs",
        "downIn", "downOut", "upIn", "upOut", "requestId",
        "client", "clientVer", "clientId",
        # v3 Batch A (§9.1) — additive tail fields, always present on rows
        # written by the upgraded producer; rows from older versions simply
        # lack them (consumers must tolerate the absence).
        "wireVersion", "selectorResult", "directoryForm", "recordType",
        "lifecycleId",
    }
    assert set(record) == expected_keys
    assert record["method"] == "GET"
    assert record["path"] == "/slimapi/messages/ses_x"
    assert record["bucket"] == "messages"
    assert record["status"] == 200
    assert isinstance(record["status"], int)
    assert record["durationMs"] == 12.346
    assert record["downIn"] == 5
    assert record["downOut"] == 7
    assert record["upIn"] == 9
    assert record["upOut"] == 11
    assert record["requestId"] == "req-123"
    assert record["client"] is None
    assert record["clientVer"] is None
    assert record["clientId"] is None
    assert isinstance(record["ts"], str) and len(record["ts"]) > 0


# ---------------------------------------------------------------------------
# 4. write_access_log: new client fields
# ---------------------------------------------------------------------------


def test_write_with_client_fields(tmp_path):
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    write_access_log(logger, **_kwargs_with_client())
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["client"] == "ocdroid"
    assert record["clientVer"] == "2.1.0"
    assert record["clientId"] == "abc123"


def test_write_client_fields_none_serialize_as_null(tmp_path):
    """When client/clientVer/clientId are None, JSON output has null, not omitted."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    write_access_log(logger, **_kwargs())  # defaults to None
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    raw = path.read_text(encoding="utf-8").strip()
    assert '"client":null' in raw
    assert '"clientVer":null' in raw
    assert '"clientId":null' in raw


# ---------------------------------------------------------------------------
# 5. enabled=False → logger disabled, write is a filesystem no-op
# ---------------------------------------------------------------------------


def test_disabled_logger_is_noop_and_creates_no_file(tmp_path):
    logger = setup_access_log(enabled=False, dir=str(tmp_path))
    assert logger.disabled is True
    assert len(logger.handlers) == 0

    write_access_log(logger, **_kwargs())
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    assert not path.exists()


# ---------------------------------------------------------------------------
# 6. Idempotent re-init
# ---------------------------------------------------------------------------


def test_setup_is_idempotent_single_handler_single_line(tmp_path):
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    logger = setup_access_log(enabled=True, dir=str(tmp_path))

    daily_handlers = [h for h in logger.handlers if isinstance(h, DailyAccessHandler)]
    assert len(daily_handlers) == 1

    write_access_log(logger, **_kwargs())
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == "/slimapi/messages/ses_x"


# ---------------------------------------------------------------------------
# 7. propagate is False
# ---------------------------------------------------------------------------


def test_propagate_is_false_when_enabled(tmp_path):
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    assert logger.propagate is False


def test_propagate_is_false_when_disabled(tmp_path):
    logger = setup_access_log(enabled=False, dir=str(tmp_path))
    assert logger.propagate is False


# ---------------------------------------------------------------------------
# 8. Parent directories auto-created
# ---------------------------------------------------------------------------


def test_setup_creates_missing_parent_directories(tmp_path):
    sub = tmp_path / "sub" / "dir"
    logger = setup_access_log(enabled=True, dir=str(sub))
    write_access_log(logger, **_kwargs())
    _flush(logger)

    assert sub.exists()
    today_str = date.today().isoformat()
    path = sub / f"access-{today_str}.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# 9. setup_access_log best-effort: directory not writable
# ---------------------------------------------------------------------------


def test_setup_best_effort_mkdir_fails(tmp_path, capsys):
    """When mkdir raises, setup does not propagate the exception."""
    bad_dir = str(tmp_path / "no-parent" / "logs")
    with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
        logger = setup_access_log(enabled=True, dir=bad_dir)

    assert logger.disabled is True
    assert len(logger.handlers) == 0
    # The warning goes to stderr via logging.lastResort (propagate=False
    # means caplog can't capture it, but stderr will have the message).
    captured = capsys.readouterr()
    assert "Failed to set up" in captured.err


# ---------------------------------------------------------------------------
# 10. hash_client_id
# ---------------------------------------------------------------------------


def test_hash_client_id_no_salt_stable():
    h1 = hash_client_id("device-42")
    h2 = hash_client_id("device-42")
    assert h1 == h2
    assert len(h1) == 16
    assert h1 == hashlib_sha256("device-42")[:16]


def hashlib_sha256(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_hash_client_id_with_salt():
    h1 = hash_client_id("device-42", salt="pepper")
    h2 = hash_client_id("device-42", salt="pepper")
    assert h1 == h2
    assert len(h1) == 16

    # Different salt → different hash
    h3 = hash_client_id("device-42", salt="salt-2")
    assert h3 != h1


def test_hash_client_id_salt_vs_no_salt_differ():
    """Same raw value with and without salt produces different hashes."""
    h_none = hash_client_id("device-42")
    h_salt = hash_client_id("device-42", salt="pepper")
    assert h_none != h_salt


# ---------------------------------------------------------------------------
# 11. compress_old_access_logs
# ---------------------------------------------------------------------------


def test_compress_skips_today(tmp_path):
    """Today's file is not compressed (still being written to)."""
    today_str = date.today().isoformat()
    today_path = tmp_path / f"access-{today_str}.jsonl"
    today_path.write_text('{"today": true}\n')

    count = compress_old_access_logs(str(tmp_path), date.today())
    assert count == 0
    assert today_path.exists()  # not deleted
    assert not (tmp_path / f"access-{today_str}.jsonl.gz").exists()


def test_compress_old_file(tmp_path):
    """An old file is compressed, source deleted, .gz created."""
    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"old": true}\n')

    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 1

    assert not old_path.exists()
    gz_path = tmp_path / "access-2026-07-28.jsonl.gz"
    assert gz_path.exists()

    # Verify content
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        assert f.read().strip() == '{"old": true}'


def test_compress_skips_if_gz_exists(tmp_path):
    """If .gz already exists, skip the .jsonl (conservative)."""
    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"old": true}\n')
    gz_path = tmp_path / "access-2026-07-28.jsonl.gz"
    gz_path.write_text("corrupt")

    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 0
    assert old_path.exists()  # not touched


def test_compress_skips_non_matching_files(tmp_path):
    """Files that don't match the strict naming pattern are skipped."""
    (tmp_path / "random.jsonl").write_text("data\n")
    (tmp_path / "access-foo.jsonl").write_text("data\n")

    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 0
    assert (tmp_path / "random.jsonl").exists()
    assert (tmp_path / "access-foo.jsonl").exists()


def test_compress_cleans_leftover_tmp(tmp_path):
    """Stale .gz.tmp files are cleaned up before compression."""
    leftover = tmp_path / "access-2026-07-27.jsonl.gz.tmp"
    leftover.write_text("stale")

    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"data": 1}\n')

    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 1
    assert not leftover.exists()  # cleaned


def test_compress_multiple_files(tmp_path):
    """Multiple old files are all compressed."""
    for day in (25, 26, 27):
        (tmp_path / f"access-2026-07-{day:02d}.jsonl").write_text(f'{{"d": {day}}}\n')

    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 3

    for day in (25, 26, 27):
        assert not (tmp_path / f"access-2026-07-{day:02d}.jsonl").exists()
        assert (tmp_path / f"access-2026-07-{day:02d}.jsonl.gz").exists()


# ---------------------------------------------------------------------------
# 12. prune_old_access_logs
# ---------------------------------------------------------------------------


def test_prune_retain_days_zero_is_noop(tmp_path):
    """retain_days=0 → never delete."""
    old_path = tmp_path / "access-2026-07-01.jsonl"
    old_path.write_text("data\n")
    count = prune_old_access_logs(str(tmp_path), 0, date(2026, 7, 29))
    assert count == 0
    assert old_path.exists()


def test_prune_deletes_expired_jsonl(tmp_path):
    old = tmp_path / "access-2026-07-01.jsonl"
    old.write_text("data\n")

    count = prune_old_access_logs(str(tmp_path), 7, date(2026, 7, 29))
    # deadline = 2026-07-22, 2026-07-01 < 2026-07-22 → deleted
    assert count == 1
    assert not old.exists()


def test_prune_deletes_expired_gz(tmp_path):
    gz = tmp_path / "access-2026-07-01.jsonl.gz"
    gz.write_text("data")

    count = prune_old_access_logs(str(tmp_path), 7, date(2026, 7, 29))
    assert count == 1
    assert not gz.exists()


def test_prune_keeps_boundary(tmp_path):
    """Files exactly at the boundary (== today - retain_days) are kept."""
    boundary = tmp_path / "access-2026-07-22.jsonl"
    boundary.write_text("data\n")

    count = prune_old_access_logs(str(tmp_path), 7, date(2026, 7, 29))
    assert count == 0
    assert boundary.exists()


def test_prune_skips_non_matching_names(tmp_path):
    (tmp_path / "random.jsonl").write_text("data\n")
    (tmp_path / "access-foo.jsonl").write_text("data\n")

    count = prune_old_access_logs(str(tmp_path), 7, date(2026, 7, 29))
    assert count == 0


# ---------------------------------------------------------------------------
# 13. migrate_legacy_access_log
# ---------------------------------------------------------------------------


def test_migrate_main_file(tmp_path):
    main = tmp_path / "access.jsonl"
    main.write_text("legacy\n")

    count = migrate_legacy_access_log(str(tmp_path))
    assert count == 1
    assert not main.exists()

    # A .gz file was created with legacy naming
    gz_files = list(tmp_path.glob("access-legacy-*.jsonl.gz"))
    assert len(gz_files) == 1
    assert "-current" in gz_files[0].name

    with gzip.open(gz_files[0], "rt", encoding="utf-8") as f:
        assert f.read().strip() == "legacy"


def test_migrate_numbered_backups(tmp_path):
    for n in (1, 2):
        p = tmp_path / f"access.jsonl.{n}"
        p.write_text(f"backup-{n}\n")

    count = migrate_legacy_access_log(str(tmp_path))
    assert count == 2

    for n in (1, 2):
        assert not (tmp_path / f"access.jsonl.{n}").exists()

    gz_files = sorted(tmp_path.glob("access-legacy-*.jsonl.gz"))
    assert len(gz_files) == 2


def test_migrate_skips_existing_dst(tmp_path):
    """If the legacy archive already exists, skip (no double-migrate)."""
    main = tmp_path / "access.jsonl"
    main.write_text("data\n")

    # First migration
    count1 = migrate_legacy_access_log(str(tmp_path))
    assert count1 == 1
    gz_count_after_first = len(list(tmp_path.glob("access-legacy-*.jsonl.gz")))

    # Re-create source and run again — dst exists, so skip
    main.write_text("data\n")
    count2 = migrate_legacy_access_log(str(tmp_path))
    assert count2 == 0  # skipped because dst already exists

    gz_count_after_second = len(list(tmp_path.glob("access-legacy-*.jsonl.gz")))
    assert gz_count_after_second == gz_count_after_first  # no new archives


def test_migrate_skips_non_backup_files(tmp_path):
    """Files like access.jsonl.gz are not migrated."""
    (tmp_path / "access.jsonl.gz").write_text("data")
    (tmp_path / "access.jsonl.abc").write_text("data")

    count = migrate_legacy_access_log(str(tmp_path))
    assert count == 0  # no valid legacy files


# ---------------------------------------------------------------------------
# 14. run_access_log_maintenance_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_loop_compresses_old_files(tmp_path):
    """The loop compresses old access log files."""
    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"test": true}\n')

    stop_event = asyncio.Event()

    with patch("oc_slimapi.access_log.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 29)
        mock_date.fromisoformat = date.fromisoformat

        task = asyncio.create_task(
            run_access_log_maintenance_loop(
                dir=str(tmp_path),
                retain_days=1,
                interval_s=0.05,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.12)
        stop_event.set()
        await task

    assert not old_path.exists()
    assert (tmp_path / "access-2026-07-28.jsonl.gz").exists()


@pytest.mark.asyncio
async def test_maintenance_loop_prunes_old_files(tmp_path):
    """The loop prunes expired access log files."""
    expired = tmp_path / "access-2026-07-20.jsonl"
    expired.write_text('{"old": true}\n')

    stop_event = asyncio.Event()

    with patch("oc_slimapi.access_log.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 29)
        mock_date.fromisoformat = date.fromisoformat

        task = asyncio.create_task(
            run_access_log_maintenance_loop(
                dir=str(tmp_path),
                retain_days=7,
                interval_s=0.05,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.12)
        stop_event.set()
        await task

    assert not expired.exists()


@pytest.mark.asyncio
async def test_maintenance_loop_stop_event_exits_promptly(tmp_path):
    """Setting stop_event makes the loop exit before the next interval."""
    stop_event = asyncio.Event()
    stop_event.set()

    # With a longer interval, the loop should exit immediately since stop_event
    # is already set.
    task = asyncio.create_task(
        run_access_log_maintenance_loop(
            dir=str(tmp_path),
            retain_days=0,
            interval_s=3600,
            stop_event=stop_event,
        )
    )
    await asyncio.wait_for(task, timeout=1.0)
    # If this completes, the loop exited as expected.


# ---------------------------------------------------------------------------
# 15. get_access_logger returns the named logger
# ---------------------------------------------------------------------------


def test_get_access_logger_named():
    logger = get_access_logger()
    assert logger.name == "oc_slimapi.access"


# ---------------------------------------------------------------------------
# 16. write_access_log is no-op when logger disabled
# ---------------------------------------------------------------------------


def test_write_access_log_disabled_noop(tmp_path):
    logger = setup_access_log(enabled=False, dir=str(tmp_path))
    # Add a handler to verify that write_access_log checks logger.disabled,
    # not just handler existence.
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    write_access_log(logger, **_kwargs_with_client())
    _flush(logger)

    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    # Should be empty because disabled logger short-circuits
    # (the handler would have written if called)
    assert not path.exists()


# ---------------------------------------------------------------------------
# 17. JSONL purity: maintenance logs never leak into access JSONL
# ---------------------------------------------------------------------------


def test_maintenance_logs_do_not_pollute_access_jsonl(tmp_path):
    """Maintenance warnings go to ``oc_slimapi.access_log.maintenance``
    (separate logger), so ``access-*.jsonl`` files remain valid JSON even
    when maintenance operations produce diagnostics."""
    # Set up the root oc_slimapi logger so the maintenance logger has
    # somewhere to propagate to (prevents lastResort stderr noise).
    setup_logging()

    logger = setup_access_log(enabled=True, dir=str(tmp_path))

    # Write one real access log entry.
    write_access_log(logger, **_kwargs())
    _flush(logger)

    # Perform a migration that produces a maintenance "skipping" info.
    main = tmp_path / "access.jsonl"
    main.write_text("legacy\n")
    migrate_legacy_access_log(str(tmp_path))
    # Run a second time — "Legacy archive already exists, skipping" fires
    # on the maintenance logger, NOT on oc_slimapi.access.
    migrate_legacy_access_log(str(tmp_path))

    # Verify: access JSONL is still clean (every line is valid JSON).
    today_str = date.today().isoformat()
    path = tmp_path / f"access-{today_str}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # only the real access log line
    for line in lines:
        record = json.loads(line)
        assert record["method"] == "GET"


# ---------------------------------------------------------------------------
# 18. Event loop responsiveness: to_thread does not block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_loop_does_not_block_event_loop(tmp_path):
    """compress/prune via ``asyncio.to_thread`` keeps the event loop
    responsive so other async tasks can advance concurrently."""
    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"test": true}\n')

    stop_event = asyncio.Event()
    counter = 0

    async def advance():
        nonlocal counter
        while not stop_event.is_set():
            await asyncio.sleep(0.02)
            counter += 1

    with patch("oc_slimapi.access_log.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 29)
        mock_date.fromisoformat = date.fromisoformat

        adv_task = asyncio.create_task(advance())
        maint_task = asyncio.create_task(
            run_access_log_maintenance_loop(
                dir=str(tmp_path),
                retain_days=1,
                interval_s=0.05,
                stop_event=stop_event,
            )
        )
        await asyncio.sleep(0.15)
        stop_event.set()
        await maint_task
        adv_task.cancel()

    # The counter advanced during maintenance → event loop was responsive.
    assert counter > 0
    # The old file was also compressed as expected.
    assert not old_path.exists()
    assert (tmp_path / "access-2026-07-28.jsonl.gz").exists()


# ---------------------------------------------------------------------------
# 19. Migrate interruption recovery: leftover .gz.tmp cleaned up
# ---------------------------------------------------------------------------


def test_migrate_cleanup_leftover_tmp(tmp_path):
    """Orphaned legacy ``.gz.tmp`` files from a failed migration are cleaned
    up at the start of the next compress run."""
    legacy_tmp = tmp_path / "access-legacy-20260728-current.jsonl.gz.tmp"
    legacy_tmp.write_text("partial")

    # compress_old_access_logs calls _cleanup_leftover_tmp at the start,
    # which now globs ``access-legacy-*.jsonl.gz.tmp`` too.
    count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    assert count == 0  # no daily files to compress
    assert not legacy_tmp.exists()  # cleaned up


# ---------------------------------------------------------------------------
# 20. Compress <today boundary: future dates are never compressed
# ---------------------------------------------------------------------------


def test_compress_skips_future_date(tmp_path):
    """Future-dated files (>= today) are NOT compressed."""
    future = tmp_path / "access-2026-07-30.jsonl"
    future.write_text('{"future": true}\n')

    today = date(2026, 7, 29)
    count = compress_old_access_logs(str(tmp_path), today)
    assert count == 0
    assert future.exists()
    assert not (tmp_path / "access-2026-07-30.jsonl.gz").exists()


# ---------------------------------------------------------------------------
# 21. P0-8: maintenance serialisation lock + unique temp names
# ---------------------------------------------------------------------------


def test_compress_uses_unique_pid_scoped_tmp_name(tmp_path):
    """P0-8: the compress temp file is PID+token scoped, not a fixed
    ``.gz.tmp``. Two concurrent invocations must not share the same temp
    name (the original bug: a fixed ``.gz.tmp`` let a second invocation
    unlink/overwrite the first's in-flight gzip)."""
    old_path = tmp_path / "access-2026-07-28.jsonl"
    old_path.write_text('{"old": true}\n')

    seen_tmp_names: list[str] = []
    real_gzip_open = gzip.open

    def spy_gzip_open(path, mode):
        # Capture the temp path used for the gzip output.
        name = Path(path).name
        if ".tmp." in name:
            seen_tmp_names.append(name)
        return real_gzip_open(path, mode)

    with patch("oc_slimapi.access_log.gzip.open", spy_gzip_open):
        count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))

    assert count == 1
    assert len(seen_tmp_names) == 1
    tmp_name = seen_tmp_names[0]
    # PID-scoped: contains the current PID and a random token.
    assert str(os.getpid()) in tmp_name
    assert tmp_name.startswith("access-2026-07-28.jsonl.gz.tmp.")
    # Token suffix is present (something after the pid).
    after_pid = tmp_name.split(f".{os.getpid()}.", 1)[1]
    assert len(after_pid) >= 1
    # No fixed-name tmp lingers (the old bug left a shared .gz.tmp).
    assert not (tmp_path / "access-2026-07-28.jsonl.gz.tmp").exists()


def test_compress_concurrent_invocations_are_serialised(tmp_path):
    """P0-8: two threads calling compress simultaneously do not corrupt the
    archive — the module-level ``_MAINT_LOCK`` serialises them. We assert
    (a) both succeed, (b) no torn .gz is produced, (c) the resulting archive
    is valid gzip whose content matches the source.

    Before the lock + unique tmp, a fixed ``.gz.tmp`` let the second thread
    race the first's write (truncated .gz, or one thread unlinking the
    other's temp before os.replace landed)."""
    import threading

    # Two distinct old days to compress — each thread owns its own file but
    # they share the same directory (the race window the lock closes).
    for d in (27, 28):
        (tmp_path / f"access-2026-07-{d:02d}.jsonl").write_text(
            f'{{"day": {d}}}\n'
        )

    errors: list[Exception] = []

    def run():
        try:
            compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent compress raised: {errors}"
    # Both archives exist and are valid gzip with the right content.
    for d in (27, 28):
        gz = tmp_path / f"access-2026-07-{d:02d}.jsonl.gz"
        assert gz.exists(), f"{gz} missing after concurrent compress"
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            assert f.read().strip() == f'{{"day": {d}}}'
    # No leftover temp files of either flavour.
    leftovers = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob("*.tmp.*"))
    assert leftovers == [], f"leftover temp files: {leftovers}"


def test_compress_concurrent_same_file_only_one_wins(tmp_path):
    """P0-8: even when two threads target the SAME source file, the lock +
    existence-check (``gz_path.exists()`` skip) means exactly one compress
    lands and the .gz is authoritative (no torn double-write)."""
    import threading

    src = tmp_path / "access-2026-07-28.jsonl"
    src.write_text('{"x": 1}\n')

    counts: list[int] = []

    def run():
        counts.append(compress_old_access_logs(str(tmp_path), date(2026, 7, 29)))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread did the actual compress (the rest saw gz exists).
    assert sum(counts) == 1, f"expected exactly 1 compress, got {counts}"
    gz = tmp_path / "access-2026-07-28.jsonl.gz"
    assert gz.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        assert f.read().strip() == '{"x": 1}'


# ---------------------------------------------------------------------------
# 22. P1-25: compress skips the live handler's open source file
# ---------------------------------------------------------------------------


def test_compress_skips_active_handler_open_source(tmp_path):
    """P1-25: when the live DailyAccessHandler still holds yesterday's .jsonl
    fd open (cross-midnight idle gap — no emit yet today), compress must NOT
    unlink that source. unlinking it would leave the fd pointing at a deleted
    inode (disk space not released until next emit / process exit). Instead
    compress defers it to a later tick.

    We simulate this by installing a handler that has emitted on yesterday's
    date (so its ``current_path`` is yesterday's file) and asserting that a
    compress run targeting that same file skips it."""
    import oc_slimapi.access_log as mod

    yesterday = date(2026, 7, 28)
    src = tmp_path / "access-2026-07-28.jsonl"
    src.write_text('{"yesterday": true}\n')

    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Force the handler to open yesterday's file by emitting a record whose
    # .created maps to yesterday midnight — without this current_path is None.
    handler.emit(_make_record('{"warmup": 1}', target_date=yesterday))

    saved_ref = mod._active_handler_ref
    mod._active_handler_ref = handler
    try:
        assert handler.current_path == src, (
            f"handler should hold {src}, holds {handler.current_path}"
        )
        count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    finally:
        mod._active_handler_ref = saved_ref
        handler.close()

    # The live source was deferred — not compressed this tick.
    assert count == 0
    assert src.exists(), "live handler's source must not be unlinked"
    assert not (tmp_path / "access-2026-07-28.jsonl.gz").exists()


def test_compress_compresses_inactive_file_alongside_active(tmp_path):
    """P1-25 boundary: when the active handler holds day N, a DIFFERENT old
    day (day N-1) with no open fd is still compressed normally — only the
    active handler's exact held path is deferred."""
    import oc_slimapi.access_log as mod

    held_day = date(2026, 7, 28)
    other_day = date(2026, 7, 27)
    held_src = tmp_path / "access-2026-07-28.jsonl"
    other_src = tmp_path / "access-2026-07-27.jsonl"
    held_src.write_text('{"held": true}\n')
    other_src.write_text('{"other": true}\n')

    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(_make_record('{"warmup": 1}', target_date=held_day))

    saved_ref = mod._active_handler_ref
    mod._active_handler_ref = handler
    try:
        count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    finally:
        mod._active_handler_ref = saved_ref
        handler.close()

    # Only the non-held file was compressed (1); the held one was deferred.
    assert count == 1
    assert held_src.exists(), "held source deferred"
    assert not other_src.exists(), "other source compressed + unlinked"
    assert (tmp_path / "access-2026-07-27.jsonl.gz").exists()
    assert not (tmp_path / "access-2026-07-28.jsonl.gz").exists()


def test_compress_no_active_handler_compresses_all(tmp_path):
    """P1-25 boundary: with no live handler installed (``_active_handler_ref``
    is None — disabled, or pre-setup), every old file is compressed as
    before. The skip only applies to the handler's held path."""
    import oc_slimapi.access_log as mod

    for d in (27, 28):
        (tmp_path / f"access-2026-07-{d:02d}.jsonl").write_text(f'{{"d": {d}}}\n')

    saved_ref = mod._active_handler_ref
    mod._active_handler_ref = None
    try:
        count = compress_old_access_logs(str(tmp_path), date(2026, 7, 29))
    finally:
        mod._active_handler_ref = saved_ref

    assert count == 2
    for d in (27, 28):
        assert not (tmp_path / f"access-2026-07-{d:02d}.jsonl").exists()
        assert (tmp_path / f"access-2026-07-{d:02d}.jsonl.gz").exists()


def test_daily_handler_current_path_is_none_before_first_emit(tmp_path):
    """P1-25: a freshly-constructed handler (no emit yet) reports
    ``current_path`` as None — no file held open."""
    handler = DailyAccessHandler(directory=str(tmp_path))
    try:
        assert handler.current_path is None
    finally:
        handler.close()


def test_daily_handler_current_path_tracks_open_file(tmp_path):
    """P1-25: after an emit, ``current_path`` returns the open file's path."""
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.emit(_make_record('{"x": 1}', target_date=date(2026, 7, 28)))
        assert handler.current_path == tmp_path / "access-2026-07-28.jsonl"
        # After closing, no path is held.
        handler.close()
        assert handler.current_path is None
    finally:
        try:
            handler.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 23. P0-8/P1-25: setup_access_log registers / clears the active handler ref
# ---------------------------------------------------------------------------


def test_setup_registers_active_handler_ref(tmp_path):
    """P0-8/P1-25: ``setup_access_log(enabled=True)`` installs the handler as
    the module-level ``_active_handler_ref`` so maintenance can consult it."""
    import oc_slimapi.access_log as mod

    setup_access_log(enabled=True, dir=str(tmp_path))
    assert mod._active_handler_ref is not None
    assert isinstance(mod._active_handler_ref, DailyAccessHandler)
    # Re-init clears the old ref and installs a fresh one.
    first = mod._active_handler_ref
    setup_access_log(enabled=True, dir=str(tmp_path))
    assert mod._active_handler_ref is not first
    # Disabling clears the ref entirely.
    setup_access_log(enabled=False, dir=str(tmp_path))
    assert mod._active_handler_ref is None


def test_emit_writes_single_call_with_newline(tmp_path):
    handler = DailyAccessHandler(directory=str(tmp_path))
    writes: list[str] = []
    flush_calls = 0
    class Spy:
        def write(self, s: str) -> None:
            writes.append(s)
        def flush(self) -> None:
            nonlocal flush_calls
            flush_calls += 1
    handler._current_date = date.today()
    handler._current_fh = Spy()
    record = logging.LogRecord("oc_slimapi.access", logging.INFO, "", 0, "{}", None, None)
    handler.emit(record)
    assert writes == ["{}\n"]
    assert flush_calls == 1  # 若 spy 缺 flush，emit 会捕获 AttributeError 假绿，必须同时断言 flush


# ---------------------------------------------------------------------------
# Task 10 (P2-1): extra_prune hook — snapshot prune reuses access-log loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_loop_calls_extra_prune(tmp_path):
    """The maintenance loop invokes the optional ``extra_prune`` callable
    once per tick, passing the same ``today`` (a ``date``) it uses for the
    access-log prune. This lets the traffic-snapshot prune piggyback on the
    existing loop without a separate background task."""
    stop_event = asyncio.Event()
    extra_calls: list = []  # captured arg(s)

    def _spy_extra_prune(today_arg):
        extra_calls.append(today_arg)

    with patch("oc_slimapi.access_log.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 10)
        mock_date.fromisoformat = date.fromisoformat

        task = asyncio.create_task(
            run_access_log_maintenance_loop(
                dir=str(tmp_path),
                retain_days=0,
                interval_s=0.05,
                stop_event=stop_event,
                extra_prune=_spy_extra_prune,
            )
        )
        await asyncio.sleep(0.12)
        stop_event.set()
        await task

    assert len(extra_calls) >= 1
    # The arg is the same `date` the loop computed for access-log prune.
    assert all(isinstance(c, date) for c in extra_calls)
    assert extra_calls[0] == date(2026, 8, 10)


