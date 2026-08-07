"""Application logging setup for ``oc_slimapi``.

Provides a root-level ``oc_slimapi`` logger with a stderr ``StreamHandler``,
configured from ``OC_SLIMAPI_LOG_LEVEL`` env var.  Idempotent — safe under
uvicorn hot reload.

This module does **not** touch the existing ``oc_slimapi.access`` logger
(which has its own :class:`~oc_slimapi.access_log.DailyAccessHandler` in
:mod:`oc_slimapi.access_log`).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading

# ---------------------------------------------------------------------------
# The root logger for the whole ``oc_slimapi`` package.  All sub-loggers
# (e.g. ``oc_slimapi.sse``, ``oc_slimapi.middleware``) will inherit the
# level / handler from this root — except the ``oc_slimapi.access`` logger
# which is configured by :mod:`oc_slimapi.access_log` with ``propagate=False``.
# ---------------------------------------------------------------------------
_ROOT_LOGGER_NAME = "oc_slimapi"
_setup_lock = threading.Lock()


def _resolve_log_level() -> int:
    """Read ``OC_SLIMAPI_LOG_LEVEL`` env, validate, and return the numeric level.

    Defaults to ``INFO``.  Falls back to ``INFO`` with a warning when the env
    value is present but not a valid level name (case-insensitive).

    Only standard level names (``CRITICAL``, ``ERROR``, ``WARNING``, ``INFO``,
    ``DEBUG``, ``NOTSET``) are accepted; custom int values or other strings
    cause a fallback.
    """
    raw = os.environ.get("OC_SLIMAPI_LOG_LEVEL", "INFO").strip().upper()
    # Use _nameToLevel (stdlib private) to avoid getattr match of non-level attrs.
    try:
        return logging._nameToLevel[raw]
    except KeyError:
        import logging as _lg  # alias to avoid shadowing
        _lg.getLogger(_ROOT_LOGGER_NAME).warning(
            "OC_SLIMAPI_LOG_LEVEL=%r is not a valid logging level; "
            "falling back to INFO",
            raw,
        )
        return logging.INFO


def setup_logging() -> None:
    """Configure the ``oc_slimapi`` root logger.

    Idempotent — subsequent calls are no-ops (no duplicate handlers).  Must be
    called **before** any sub-logger is used by the application so that level /
    handler propagation is consistent.

    The handler writes to **stderr** with a formatter that includes
    timestamp / level / logger name / message.  It does **not** touch the
    ``oc_slimapi.access`` logger (which is already managed by
    :mod:`oc_slimapi.access_log`).
    """
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    level = _resolve_log_level()
    logger.setLevel(level)

    with _setup_lock:
        # Avoid piling up duplicate handlers under hot reload / repeated calls.
        if any(
            isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            for h in logger.handlers
        ):
            return  # already set up

        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(logging.DEBUG)  # let the logger's level do the gate
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger whose name is relative to the ``oc_slimapi`` tree.

    Example: ``get_logger("middleware.request_id")`` returns the logger named
    ``oc_slimapi.middleware.request_id``.

    If ``name`` already is ``oc_slimapi`` or starts with ``oc_slimapi.``, the
    name is used verbatim.  This prevents accidental double prefix.
    """
    if name == _ROOT_LOGGER_NAME or name.startswith(_ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


def redact(secret: str) -> str:
    """Replace a secret string with the literal ``<redacted>``.

    Test-only utility: no production call site (the startup banner logs no
    secrets). Retained so ``tests/test_logging_config`` can pin the contract.
    """
    return "<redacted>"
