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
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .logging_config import get_logger

_LOGGER_NAME = "oc_slimapi.access"
_setup_lock = threading.Lock()

# Cross-thread serialisation for the maintenance file operations (compress /
# prune / migrate). Startup runs on the main thread; the maintenance loop
# runs compress/prune via ``asyncio.to_thread`` in the default thread pool —
# so two maintenance operations CAN overlap (startup migrate + first loop
# tick, or a hot-reload re-init racing the loop). Without this lock the two
# would share fixed ``.gz.tmp`` names and clobber each other's archives.
# Granularity: whole-function hold — maintenance is inherently serial work
# (gzip is CPU-bound, the file set is small); holding the lock for the whole
# call is simpler than per-file critical sections and avoids TOCTOU windows
# between the existence checks and the writes.
_MAINT_LOCK = threading.Lock()

# Reference to the currently-installed DailyAccessHandler (set by
# setup_access_log), so maintenance functions can avoid unlinking the .jsonl
# that the live handler still holds open (see P1-25). ``None`` when no handler
# is installed (disabled, or before setup). Read-only by maintenance; only
# setup_access_log writes it (under _setup_lock).
_active_handler_ref: "DailyAccessHandler | None" = None

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

    # -- public read-only state (P1-25) -------------------------------------

    @property
    def current_path(self) -> "Path | None":
        """Return the full path of the file this handler currently holds open,
        or ``None`` if no file is open yet (no record emitted) or after
        :meth:`close`.

        Maintenance (:func:`compress_old_access_logs`) consults this via the
        module-level ``_active_handler_ref`` to avoid unlinking a .jsonl
        whose file descriptor the live handler still holds open across a
        cross-midnight idle gap — unlinking it would leak the inode (disk
        space not released until the next emit or process exit).
        """
        if self._current_fh is None or self._current_date is None:
            return None
        return Path(self._directory) / f"access-{self._current_date.isoformat()}.jsonl"

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
            self._current_fh.write(msg + "\n")   # P1-2: 单调用行写入，缩小两次调用间的半行窗口（best-effort，非 fsync/事务）
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
    global _active_handler_ref
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
        # Drop the stale active-handler reference whenever we tear down the
        # installed handlers (re-init / disable). Re-installed below if enabled.
        _active_handler_ref = None
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
            # Record the live handler so maintenance can avoid unlinking its
            # currently-open .jsonl across cross-midnight idle gaps (P1-25).
            _active_handler_ref = handler
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
    cache: str | None = None,
) -> None:
    """Emit one JSON-lines access record.

    Silently no-op when the logger is disabled (the
    ``OC_SLIMAPI_ACCESS_LOG_ENABLED=false`` path).

    ``client``, ``clientVer``, ``clientId`` are optional fields for client
    identity tracking.  When ``None`` these are written as JSON ``null`` so
    every row has a stable set of keys (``jq``-friendly). ``cache``
    ("hit"/"miss", traffic plan Batch 1 / A1) is the exception: it is only
    written when set, because rows from non-catalog routes (and deployments
    with the cache disabled) have no cache semantics at all.
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
    if cache is not None:
        record["cache"] = cache
    logger.info(json.dumps(record, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Compress / Prune / Migrate
# ---------------------------------------------------------------------------


def _unique_tmp_path(base: Path, suffix: str = ".tmp") -> Path:
    """Return a unique temp-file path in *base*'s directory.

    Includes the PID + a short random token so two concurrent maintenance
    invocations (startup main thread + a ``to_thread`` maintenance tick, or
    a hot-reload re-init) never share the same ``.tmp`` name and clobber
    each other's in-flight gzip writes. Only the caller (the process that
    created the name) is responsible for cleaning it up.
    """
    token = uuid.uuid4().hex[:8]
    return base.with_name(
        f"{base.name}.{suffix.lstrip('.')}.{os.getpid()}.{token}"
    )


def _cleanup_leftover_tmp(dir: str) -> None:
    """Remove orphaned ``.gz.tmp`` files from previous failed operations.

    Covers daily access log temp files (``access-*.jsonl.gz.tmp`` and the
    PID-scoped ``access-*.jsonl.gz.tmp.<pid>.<token>`` variant introduced
    by :func:`_unique_tmp_path`), as well as legacy migration temp files.
    """
    patterns = (
        "access-*.jsonl.gz.tmp",
        "access-*.jsonl.gz.tmp.*",   # PID-scoped unique tmp (P0-8)
        "access-legacy-*.jsonl.gz.tmp",
        "access-legacy-*.jsonl.gz.tmp.*",  # PID-scoped unique tmp (P0-8)
    )
    for pattern in patterns:
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
    * Writes to a unique ``.gz.tmp.<pid>.<token>`` → ``os.replace`` (atomic
      commit) → deletes the source ``.jsonl``.  If source deletion fails a
      warning is logged but the ``.gz`` is kept (already authoritative).
    * Orphaned ``.gz.tmp`` files are cleaned up at the start of every call.

    The whole call is serialised by :data:`_MAINT_LOCK` — maintenance is
    inherently serial work (gzip is CPU-bound, the file set is small), and
    the lock prevents startup-migrate / loop-tick / hot-reload re-init from
    racing on the same directory. Returns the number of successfully
    compressed files.
    """
    log = _get_maint_log()
    with _MAINT_LOCK:
        _cleanup_leftover_tmp(dir)
        # Snapshot the live handler's currently-held path once (under the
        # lock so setup_access_log cannot flip it mid-run). Comparing paths
        # by resolved string avoids unlinking a .jsonl whose fd the handler
        # still holds open across a cross-midnight idle gap (P1-25): the
        # inode would be freed only on next emit / process exit.
        active_path: "Path | None" = None
        handler = _active_handler_ref
        if handler is not None:
            try:
                active_path = handler.current_path
            except Exception:
                active_path = None
        active_path_str = (
            str(active_path.resolve()) if active_path is not None else None
        )
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

            # P1-25: never unlink the live handler's open source file —
            # defer to a later tick instead.
            try:
                p_resolved = str(p.resolve())
            except OSError:
                p_resolved = str(p)
            if active_path_str is not None and p_resolved == active_path_str:
                log.info(
                    "Skipping compress of %s — live handler still holds its fd; "
                    "deferring to next maintenance tick",
                    p,
                )
                continue

            tmp_path = _unique_tmp_path(gz_path, ".tmp")
            try:
                # Compress to the unique temporary file.
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
                # Clean up this caller's unique tmp on failure.
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

    Serialised by :data:`_MAINT_LOCK` so it cannot race with a concurrent
    :func:`compress_old_access_logs` / :func:`migrate_legacy_access_log` on
    the same directory. Returns the number of successfully deleted files.
    """
    if retain_days <= 0:
        return 0
    deadline = date.fromordinal(today.toordinal() - retain_days)
    log = _get_maint_log()
    count = 0
    with _MAINT_LOCK:
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

    Serialised by :data:`_MAINT_LOCK` (mirrors compress/prune) so a startup
    migration cannot race a concurrent maintenance tick on the same dir.
    Returns the number of successfully migrated files.
    """
    log = _get_maint_log()
    count = 0
    with _MAINT_LOCK:
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

    Uses a unique ``.tmp.<pid>.<token>`` (:func:`_unique_tmp_path`) +
    ``os.replace`` (same atomic-commit guarantee as
    :func:`compress_old_access_logs`, now with a non-shared temp name so two
    concurrent invocations cannot clobber each other's archive).

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
    tmp_path = _unique_tmp_path(dst_path, ".tmp")
    try:
        with open(path, "rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
            f_out.writelines(f_in)
        os.replace(str(tmp_path), str(dst_path))
    except BaseException:
        # Clean up this caller's unique tmp on failure.
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
    extra_prune: Callable[[date], int] | None = None,
) -> None:
    """Periodically compress + prune old access log files.

    Runs in a loop, sleeping *interval_s* seconds between runs (using
    ``asyncio.wait_for`` on *stop_event* so shutdown is prompt).  Catches and
    logs all exceptions — a single failure never kills the loop.

    Does **not** call :func:`migrate_legacy_access_log` (migration is done once
    at startup by the caller).

    Task 10 (P2-1): the optional ``extra_prune`` callable is invoked once per
    tick with the same ``today`` used for the access-log prune. It lets the
    traffic-snapshot prune piggyback on this loop without spawning a separate
    background task. The callable returns an ``int`` (count, ignored) and runs
    via :func:`asyncio.to_thread` like the access-log compress/prune.

    **Shutdown / cancellation contract (caller responsibility)**: each tick
    dispatches ``compress`` / ``prune`` via :func:`asyncio.to_thread`, which
    hands the blocking gzip work to the default thread pool.  When *stop_event*
    is set (or the task is cancelled), this coroutine unwinds promptly, but an
    already-running gzip thread is **not** joined here — it finishes on its own
    in the thread pool.  The caller (app.py lifespan) is responsible for
    awaiting any in-flight ``to_thread`` completion before process exit if it
    needs a clean drain.  This function does NOT own cancel-time thread drain.
    The per-operation :data:`_MAINT_LOCK` ensures that even if a drain is not
    performed, concurrent maintenance operations are still mutually exclusive.
    """
    log = _get_maint_log()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass  # Expected — timeout expired, run maintenance.
        if stop_event.is_set():
            break
        today = date.today()
        try:
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
        if extra_prune is not None:
            try:
                await asyncio.to_thread(extra_prune, today)
            except Exception:
                log.warning("Access log maintenance extra_prune failed", exc_info=True)
