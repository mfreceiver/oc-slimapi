"""Structured JSON-lines access log with size-based rotation.

One line per request, written by the traffic-accounting middleware at request
end. Uses stdlib :mod:`logging` with a :class:`RotatingFileHandler` so the
access log rotates at ``OC_SLIMAPI_ACCESS_LOG_MAX_BYTES`` and keeps
``OC_SLIMAPI_ACCESS_LOG_BACKUPS`` older files. The logger is named
``oc_slimapi.access`` and has ``propagate=False`` so it never bubbles up to
the root / uvicorn access log.

:func:`setup_access_log` is safe to call multiple times — it clears and
re-installs handlers (so tests can re-target a temp path).

**Semantic note — ``downOut`` vs the SSE ledger bucket.** The access log's
``downOut`` field reflects **wire-level** response bytes as measured by the
middleware's ``counted_send`` wrapper (the raw ASGI ``send`` call, before any
application-level frame parsing). By contrast, the traffic ledger's SSE bucket
``downOut`` is accumulated via ``record_sse_downstream``, which counts
**frame-level** bytes per subscriber per SSE event (aggregated across all
connected subscribers). For fan-out SSE endpoints, the frame-level aggregation
in the ledger can exceed the single-wire-stream byte count seen by the
middleware. These two metrics have **different collection scopes** (wire vs
frame) and should not be directly compared. The authoritative per-SSE-stream
statistics live in ``/slimapi/metrics.traffic`` (the ledger snapshot).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path

_LOGGER_NAME = "oc_slimapi.access"
_setup_lock = threading.Lock()


def get_access_logger() -> logging.Logger:
    """Return the named access logger (no handler install)."""
    return logging.getLogger(_LOGGER_NAME)


def setup_access_log(
    *,
    enabled: bool,
    path: str,
    max_bytes: int,
    backups: int,
) -> logging.Logger:
    """Install the rotating file handler on the access logger.

    Clears any previously installed handlers first so re-init is safe (tests,
    hot reload). When ``enabled=False`` the logger is marked ``disabled=True``
    (and gets no file handler) so :func:`write_access_log` is a clean no-op
    without touching the filesystem.

    Parent directories of ``path`` are created automatically (mirroring the
    contract for systemd-free local dev).
    """
    logger = logging.getLogger(_LOGGER_NAME)
    with _setup_lock:
        logger.setLevel(logging.INFO)
        # Never bubble up to root / uvicorn's access log — keeps the operator's
        # existing log stream clean.
        logger.propagate = False
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
        path_obj = Path(path)
        parent = path_obj.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            str(path_obj),
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


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
) -> None:
    """Emit one JSON-lines access record.

    ``downOut`` is the **wire-level** response byte count as measured by the
    middleware's ``counted_send`` wrapper (raw ASGI ``send``). This differs
    from the traffic ledger's SSE bucket ``downOut``, which is a frame-level
    per-subscriber-per-event aggregation — see the module docstring for the
    full semantic distinction.

    Silently no-op when the logger is disabled (the
    ``OC_SLIMAPI_ACCESS_LOG_ENABLED=false`` path).
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
    }
    logger.info(json.dumps(record, separators=(",", ":")))
