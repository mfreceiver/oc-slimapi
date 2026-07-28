"""Structured JSON-lines access log with daily rotation, compression, and maintenance.

One line per request, written by the traffic-accounting middleware at request
end. Uses a :class:`DailyAccessHandler` that writes to
``access-YYYY-MM-DD.jsonl`` files, one per day.

Compression and pruning are handled by standalone functions (not tied to log
rotation), so an operator can also trigger them manually. The async maintenance
loop runs them periodically.

**Why not stdlib ``TimedRotatingFileHandler``?**

We need fine-grained control over compression (gzip instead of
``closeAndReopen`` with external ``gzip``), atomic ``.gz.tmp`` + rename,
cross-day glob-based cleanup, and prune-by-age.  Combining all of these into a
single handler would fight stdlib internals.  Standalone compress/prune
functions are also testable without mounting a full logger hierarchy.

**Best-effort everywhere:**

Every write / compress / prune failure is caught, logged as a warning, and
never propagated — the access log must never crash the application.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path

from .logging_config import get_logger

_LOGGER_NAME = "oc_slimapi.access"
_setup_lock = threading.Lock()

# Maintenance logger — separate from the access logger so diagnostic warnings
# never leak into ``access-*.jsonl`` files (which are parsed by jq).
_MAINT_LOG = None  # lazy-init via get_logger
_MAINT_LOG_NAME = "access_log.maintenance"


def _get_maint_log() -> logging.Logger:
    global _MAINT_LOG
    if _MAINT_LOG is None:
        _MAINT_LOG = get_logger(_MAINT_LOG_NAME)
    return _MAINT_LOG

# Strict regex for daily access log files — only matches ``access-YYYY-MM-DD.jsonl``
# and ``access-YYYY-MM-DD.jsonl.gz``.
_ACCESS_LOG_RE = re.compile(r"^access-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_access_logger() -> logging.Logger:
    """Return the named access logger (no handler install)."""
    return logging.getLogger(_LOGGER_NAME)


def hash_client_id(raw: str, salt: str | None = None) -> str:
    """Hash a client device id for privacy-safe logging.

    * ``salt=None``: ``sha256(raw)`` → first 16 hex chars (plain hash,
      prevents direct plaintext exposure in logs).
    * ``salt`` set: ``hmac-sha256(salt, raw)`` → first 16 hex chars (stronger,
      cross-deployment unlinkability).
    """
    data = raw.encode("utf-8")
    if salt:
        return hmac.new(salt.encode("utf-8"), data, hashlib.sha256).hexdigest()[:16]
    return hashlib.sha256(data).hexdigest()[:16]


# ---------------------------------------------------------------------------
# DailyAccessHandler
# ---------------------------------------------------------------------------


class DailyAccessHandler(logging.Handler):
    """A logging handler that writes to ``access-YYYY-MM-DD.jsonl`` per day.

    Uses ``record.created`` (the epoch timestamp set by the logging framework)
    to determine the target date — see :meth:`emit`.  When the date changes
    (crossing midnight) the old file handle is closed and a new one opened.
    Files are opened in append (``"a"``) mode.

    Thread-safety is provided by the :class:`logging.Handler` lock mechanism —
    callers do not need additional synchronisation.
    """

    def __init__(self, directory: str) -> None:
        super().__init__()
        self._directory = directory
        self._current_date: date | None = None
        self._current_fh = None  # Optional[TextIO]

    def __del__(self) -> None:
        try:
            self._close_current_fh()
        except Exception:
            pass

    # -- internal helpers ---------------------------------------------------

    def _ensure_dir(self) -> None:
        Path(self._directory).mkdir(parents=True, exist_ok=True)

    def _open_file(self, today: date) -> object:
        path = Path(self._directory) / f"access-{today.isoformat()}.jsonl"
        return open(str(path), "a", encoding="utf-8")  # noqa: SIM115

    def _close_current_fh(self) -> None:
        fh = self._current_fh
        if fh is not None:
            self._current_fh = None
            try:
                fh.close()
            except Exception:
                pass

    # -- logging.Handler API ------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Write one JSON line to the daily file.

        Determines the target file date from ``record.created`` (the epoch
        timestamp set by the logging framework when the record was created)
        rather than calling ``date.today()`` independently.  This eliminates
        the cross-midnight window where the access JSONL ``ts`` field and the
        file name could belong to different dates.

        When the date has changed since the last emit, the previous file handle
        is closed and a new one opened.
        """
        try:
            today = datetime.fromtimestamp(record.created).date()
            if self._current_date != today:
                self._close_current_fh()
                self._ensure_dir()
                self._current_fh = self._open_file(today)
                self._current_date = today
            msg = self.format(record)
            if self._current_fh is None:
                return  # defensive — should not happen after _open_file above
            self._current_fh.write(msg)
            self._current_fh.write("\n")
            self._current_fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Close the current file handle and release resources."""
        self._close_current_fh()
        super().close()


# ---------------------------------------------------------------------------
# setup_access_log  —  single responsibility: install DailyAccessHandler
# ---------------------------------------------------------------------------


def setup_access_log(*, enabled: bool, dir: str) -> logging.Logger:
    """Install a :class:`DailyAccessHandler` on the access logger.

    *Clears* any previously installed handlers first so re-init is safe (tests,
    hot reload).  When ``enabled=False`` the logger is marked ``disabled=True``
    (and gets no file handler) so :func:`write_access_log` is a clean no-op
    without touching the filesystem.

    **Best-effort**: if directory creation or handler installation fails, a
    warning is logged and the logger is disabled — the exception is **never**
    propagated (fixes the existing bug where setup failure would crash the app
    lifespan).
    """
    logger = get_access_logger()
    with _setup_lock:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        # Clear old handlers (idempotent — repeated setup is safe).
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        if not enabled:
            logger.disabled = True
            return logger
        logger.disabled = False
        # Best-effort directory creation + handler install.
        try:
            Path(dir).mkdir(parents=True, exist_ok=True)
            handler = DailyAccessHandler(directory=dir)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        except Exception:
            logger.warning(
                "Failed to set up DailyAccessHandler in %r; disabling access log",
                dir,
                exc_info=True,
            )
            logger.disabled = True
    return logger


# ---------------------------------------------------------------------------
# write_access_log
# ---------------------------------------------------------------------------


def write_access_log(
    logger: logging.Logger,
    *,
    method: str,
    path: str,
    bucket: str,
    status: int,
    duration_ms: float,
    down_in: int,
    down_out: int,
    up_in: int,
    up_out: int,
    request_id: str | None = None,
    client: str | None = None,
    client_ver: str | None = None,
    client_id: str | None = None,
) -> None:
    """Emit one JSON-lines access record.

    Silently no-op when the logger is disabled (the
    ``OC_SLIMAPI_ACCESS_LOG_ENABLED=false`` path).

    ``client``, ``clientVer``, ``clientId`` are optional fields for client
    identity tracking.  When ``None`` they are written as JSON ``null`` so
    every row has a stable set of keys (``jq``-friendly).
    """
    if logger.disabled:
        return
    record = {
        # ISO 8601 with local timezone offset — operator-friendly for grep.
        "ts": datetime.now().astimezone().isoformat(),
        "method": method,
        "path": path,
        "bucket": bucket,
        "status": int(status),
        "durationMs": round(float(duration_ms), 3),
        "downIn": int(down_in),
        "downOut": int(down_out),
        "upIn": int(up_in),
        "upOut": int(up_out),
        "requestId": request_id,
        "client": client,
        "clientVer": client_ver,
        "clientId": client_id,
    }
    logger.info(json.dumps(record, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Compress / Prune / Migrate
# ---------------------------------------------------------------------------


def _cleanup_leftover_tmp(dir: str) -> None:
    """Remove orphaned ``.gz.tmp`` files from previous failed operations.

    Covers both daily access log temp files (``access-*.jsonl.gz.tmp``) and
    legacy migration temp files (``access-legacy-*.jsonl.gz.tmp``).
    """
    for pattern in ("access-*.jsonl.gz.tmp", "access-legacy-*.jsonl.gz.tmp"):
        for p in Path(dir).glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


def compress_old_access_logs(dir: str, today: date) -> int:
    """Gzip-compress daily access log files older than *today*.

    * Globs ``access-*.jsonl``, validates the strict naming pattern via
      :data:`_ACCESS_LOG_RE`.
    * Skips files whose date is **>= *today*** (still being written, or
      future-dated from clock skew — never compress those).
    * Skips files whose ``.gz`` sibling already exists (conservative — a
      damaged ``.gz`` from a previous run is not re-compressed).
    * Writes to ``.gz.tmp`` → ``os.replace`` (atomic commit) → deletes the
      source ``.jsonl``.  If source deletion fails a warning is logged but
      the ``.gz`` is kept (already authoritative).
    * Orphaned ``.gz.tmp`` files are cleaned up at the start of every call.

    Returns the number of successfully compressed files.
    """
    _cleanup_leftover_tmp(dir)
    log = _get_maint_log()
    count = 0
    for p in sorted(Path(dir).glob("access-*.jsonl")):
        m = _ACCESS_LOG_RE.match(p.name)
        if not m:
            continue
        try:
            file_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if file_date >= today:
            continue

        gz_path = p.with_name(p.name + ".gz")
        if gz_path.exists():
            continue

        tmp_path = p.with_name(p.name + ".gz.tmp")
        try:
            # Compress to temporary file.
            with open(p, "rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
                f_out.writelines(f_in)
            # Atomic replace — the .gz is now authoritative.
            os.replace(str(tmp_path), str(gz_path))
            # Best-effort removal of the source .jsonl.
            try:
                p.unlink()
            except OSError:
                log.warning(
                    "Compressed %s but failed to delete source; .gz is authoritative",
                    p,
                    exc_info=True,
                )
            count += 1
        except Exception:
            log.warning(
                "Failed to compress %s", p, exc_info=True,
            )
            # Clean up leftover tmp on failure.
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
    return count


def prune_old_access_logs(dir: str, retain_days: int, today: date) -> int:
    """Delete daily access log files older than *retain_days*.

    * ``retain_days=0`` → no-op (never prune).
    * Deletes both ``.jsonl`` and ``.jsonl.gz`` files whose embedded date is
      **strictly less than** ``today - retain_days`` (boundary is kept).
    * Only files matching the strict naming pattern (:data:`_ACCESS_LOG_RE`)
      are considered.

    Returns the number of successfully deleted files.
    """
    if retain_days <= 0:
        return 0
    deadline = date.fromordinal(today.toordinal() - retain_days)
    log = _get_maint_log()
    count = 0
    for pattern in ("access-*.jsonl", "access-*.jsonl.gz"):
        for p in Path(dir).glob(pattern):
            m = _ACCESS_LOG_RE.match(p.name)
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
                except Exception:
                    log.warning(
                        "Failed to prune %s", p, exc_info=True,
                    )
    return count


def migrate_legacy_access_log(dir: str) -> int:
    """Migrate old-style ``access.jsonl`` / ``access.jsonl.N`` files.

    These files (from the ``RotatingFileHandler`` era) have no date in their
    names.  Each file is gzip-archived as
    ``access-legacy-{mtime:%Y%m%d}-{N}.jsonl.gz`` using the file's mtime for
    the date component.  The main ``access.jsonl`` gets suffix ``current``.

    Best-effort — each file is tried independently; failures are logged and
    do not stop processing the remaining files.

    Returns the number of successfully migrated files.
    """
    log = _get_maint_log()
    count = 0

    # Main file: access.jsonl
    main_path = Path(dir) / "access.jsonl"
    if main_path.exists():
        try:
            if _migrate_one(main_path, "current", log):
                count += 1
        except Exception:
            log.warning("Failed to migrate %s", main_path, exc_info=True)

    # Numbered backups: access.jsonl.N  (N = integer ≥ 1)
    for p in sorted(Path(dir).glob("access.jsonl.*")):
        # Skip false positives like "access.jsonl.gz" or "access.jsonl.abc".
        suffix = p.name[len("access.jsonl."):]
        if not suffix.isdigit() or int(suffix) < 1:
            continue
        try:
            if _migrate_one(p, suffix, log):
                count += 1
        except Exception:
            log.warning("Failed to migrate %s", p, exc_info=True)

    return count


def _migrate_one(path: Path, label: str, log: logging.Logger) -> bool:
    """Helper: gzip *path* into a legacy-named archive, then delete *path*.

    Uses atomic ``.gz.tmp`` + ``os.replace`` (same as
    :func:`compress_old_access_logs`) so an interrupted migration never leaves
    a truncated ``.gz`` as the target.

    Returns ``True`` if the file was actually migrated, ``False`` if skipped
    (destination already exists).
    """
    mtime = path.stat().st_mtime
    mtime_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d")
    dst_name = f"access-legacy-{mtime_str}-{label}.jsonl.gz"
    dst_path = path.with_name(dst_name)
    if dst_path.exists():
        log.info("Legacy archive %s already exists, skipping", dst_name)
        return False
    tmp_path = path.with_name(dst_name + ".tmp")
    try:
        with open(path, "rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
            f_out.writelines(f_in)
        os.replace(str(tmp_path), str(dst_path))
    except BaseException:
        # Clean up tmp on failure.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise
    path.unlink()
    return True


# ---------------------------------------------------------------------------
# Async maintenance loop
# ---------------------------------------------------------------------------


async def run_access_log_maintenance_loop(
    *,
    dir: str,
    retain_days: int,
    interval_s: int,
    stop_event: asyncio.Event,
) -> None:
    """Periodically compress + prune old access log files.

    Runs in a loop, sleeping *interval_s* seconds between runs (using
    ``asyncio.wait_for`` on *stop_event* so shutdown is prompt).  Catches and
    logs all exceptions — a single failure never kills the loop.

    Does **not** call :func:`migrate_legacy_access_log` (migration is done once
    at startup by the caller).
    """
    log = _get_maint_log()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass  # Expected — timeout expired, run maintenance.
        if stop_event.is_set():
            break
        try:
            today = date.today()
            # Run compress/prune via to_thread to avoid blocking the event
            # loop with synchronous gzip I/O (a single worker process must
            # not freeze request/SSE handling during maintenance).
            await asyncio.to_thread(compress_old_access_logs, dir, today)
        except Exception:
            log.warning("Access log maintenance compress failed", exc_info=True)
        try:
            await asyncio.to_thread(prune_old_access_logs, dir, retain_days, today)
        except Exception:
            log.warning("Access log maintenance prune failed", exc_info=True)
