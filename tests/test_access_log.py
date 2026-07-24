"""Unit tests for ``oc_slimapi.access_log`` (structured JSON-lines access log).

Covers :func:`setup_access_log` (rotating file handler install, idempotent
re-init, parent-dir auto-create, size-based rotation, disabled no-op) and
:func:`write_access_log` (one JSON line per call with the documented fields).

The access logger (``oc_slimapi.access``) is a process-global named logger.
An autouse fixture resets its handler/disabled state after every test so no
file handle leaks into other test modules.
"""

from __future__ import annotations

import json
import logging
import logging.handlers

import pytest

from oc_slimapi.access_log import (
    get_access_logger,
    setup_access_log,
    write_access_log,
)


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


# ---------------------------------------------------------------------------
# 1. enabled + write → one line, all fields correct
# ---------------------------------------------------------------------------


def test_write_emits_one_json_line_with_all_fields(tmp_path):
    path = tmp_path / "acc.jsonl"
    logger = setup_access_log(
        enabled=True, path=str(path), max_bytes=1_000_000, backups=1
    )
    write_access_log(logger, **_kwargs())
    _flush(logger)

    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    # Every documented field is present with the exact coerced value.
    assert set(record) == {
        "ts", "method", "path", "bucket", "status", "durationMs",
        "downIn", "downOut", "upIn", "upOut", "requestId",
    }
    assert record["method"] == "GET"
    assert record["path"] == "/slimapi/messages/ses_x"
    assert record["bucket"] == "messages"
    assert record["status"] == 200
    assert isinstance(record["status"], int)
    # durationMs rounded to 3 decimals.
    assert record["durationMs"] == 12.346
    assert record["downIn"] == 5
    assert record["downOut"] == 7
    assert record["upIn"] == 9
    assert record["upOut"] == 11
    # ts is a non-empty ISO string with a local offset.
    assert isinstance(record["ts"], str) and len(record["ts"]) > 0


# ---------------------------------------------------------------------------
# 2. enabled=False → logger disabled, write is a filesystem no-op
# ---------------------------------------------------------------------------


def test_disabled_logger_is_noop_and_creates_no_file(tmp_path):
    path = tmp_path / "disabled.jsonl"
    logger = setup_access_log(
        enabled=False, path=str(path), max_bytes=1_000_000, backups=1
    )

    assert logger.disabled is True
    assert len(logger.handlers) == 0  # no file handler installed

    write_access_log(logger, **_kwargs())
    _flush(logger)

    # File was never created.
    assert not path.exists()


# ---------------------------------------------------------------------------
# 3. Idempotent re-init: two setups same path → one handler, one line
# ---------------------------------------------------------------------------


def test_setup_is_idempotent_single_handler_single_line(tmp_path):
    path = tmp_path / "acc.jsonl"
    logger = setup_access_log(
        enabled=True, path=str(path), max_bytes=1_000_000, backups=1
    )
    logger = setup_access_log(
        enabled=True, path=str(path), max_bytes=1_000_000, backups=1
    )

    # Exactly one handler — the second setup cleared the first.
    rotating = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1

    write_access_log(logger, **_kwargs())
    _flush(logger)

    lines = path.read_text(encoding="utf-8").splitlines()
    # One write → exactly one line (no duplication from re-init).
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == "/slimapi/messages/ses_x"


# ---------------------------------------------------------------------------
# 4. propagate is False (never bubbles up to root / uvicorn)
# ---------------------------------------------------------------------------


def test_propagate_is_false_when_enabled(tmp_path):
    path = tmp_path / "acc.jsonl"
    logger = setup_access_log(
        enabled=True, path=str(path), max_bytes=1_000_000, backups=1
    )
    assert logger.propagate is False


def test_propagate_is_false_when_disabled(tmp_path):
    path = tmp_path / "acc.jsonl"
    logger = setup_access_log(
        enabled=False, path=str(path), max_bytes=1_000_000, backups=1
    )
    assert logger.propagate is False


# ---------------------------------------------------------------------------
# 5. Size-based rotation produces a .1 backup
# ---------------------------------------------------------------------------


def test_rotation_creates_backup_file(tmp_path):
    path = tmp_path / "acc.jsonl"
    # Tiny threshold: each JSON record is ~150 bytes, so the 2nd write rolls.
    logger = setup_access_log(enabled=True, path=str(path), max_bytes=64, backups=1)

    for i in range(20):
        write_access_log(
            logger,
            method="GET",
            path=f"/slimapi/messages/ses_{i}",
            bucket="messages",
            status=200,
            duration_ms=1.0,
            down_in=0,
            down_out=0,
            up_in=0,
            up_out=0,
        )
    _flush(logger)

    backup = path.with_name(path.name + ".1")
    assert backup.exists(), "rotation should have produced a .1 backup file"
    # The active file still exists with the most-recent record.
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["status"] == 200


# ---------------------------------------------------------------------------
# 6. Parent directories auto-created
# ---------------------------------------------------------------------------


def test_setup_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "sub" / "dir" / "acc.jsonl"
    assert not path.parent.exists()

    logger = setup_access_log(
        enabled=True, path=str(path), max_bytes=1_000_000, backups=1
    )
    write_access_log(logger, **_kwargs())
    _flush(logger)

    assert path.exists()
    assert path.parent.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# Extra: get_access_logger returns the named logger
# ---------------------------------------------------------------------------


def test_get_access_logger_named():
    logger = get_access_logger()
    assert logger.name == "oc_slimapi.access"
