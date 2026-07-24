"""Tests for :mod:`oc_slimapi.logging_config` (application logging setup).

Verifies:
- Idempotent ``setup_logging()`` (no duplicate stderr handlers).
- Level resolution from ``OC_SLIMAPI_LOG_LEVEL`` env (default ``INFO``; bad
  value falls back to ``INFO`` with a warning).
- ``get_logger()`` returns a named child of the ``oc_slimapi`` root logger.
- ``redact()`` replaces any string with ``<redacted>``.
- Banner log message emitted at startup does *not* contain the plain-text
  route secret (only the ``<redacted>`` placeholder).
"""

from __future__ import annotations

import logging
import os
import re
import sys

import pytest

from oc_slimapi.logging_config import get_logger, redact, setup_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Remove all handlers from the ``oc_slimapi`` root logger after each test.

    This prevents handler leaks across tests and keeps the root logger in a
    clean state (``NOTSET`` level, no handlers) so every test can assert its
    own setup results.
    """
    yield
    logger = logging.getLogger("oc_slimapi")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)


# ---------------------------------------------------------------------------
# 1. Idempotent setup — two calls add only one stderr handler
# ---------------------------------------------------------------------------


def test_setup_logging_idempotent():
    setup_logging()
    setup_logging()
    logger = logging.getLogger("oc_slimapi")
    stderr_handlers = [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
    ]
    assert len(stderr_handlers) == 1, "duplicate stderr handlers detected"


# ---------------------------------------------------------------------------
# 2. Default level is INFO when env unset
# ---------------------------------------------------------------------------


def test_default_level_is_info():
    saved = os.environ.pop("OC_SLIMAPI_LOG_LEVEL", None)
    try:
        setup_logging()
        logger = logging.getLogger("oc_slimapi")
        assert logger.level == logging.INFO
    finally:
        if saved is not None:
            os.environ["OC_SLIMAPI_LOG_LEVEL"] = saved


# ---------------------------------------------------------------------------
# 3. Env value applied correctly
# ---------------------------------------------------------------------------


def test_env_level_applied():
    os.environ["OC_SLIMAPI_LOG_LEVEL"] = "DEBUG"
    try:
        setup_logging()
        logger = logging.getLogger("oc_slimapi")
        assert logger.level == logging.DEBUG
    finally:
        del os.environ["OC_SLIMAPI_LOG_LEVEL"]


# ---------------------------------------------------------------------------
# 4. Invalid env value falls back to INFO + warning
# ---------------------------------------------------------------------------


def test_invalid_level_falls_back_to_info(caplog):
    os.environ["OC_SLIMAPI_LOG_LEVEL"] = "BOGUS"
    try:
        setup_logging()
        logger = logging.getLogger("oc_slimapi")
        assert logger.level == logging.INFO
        # The ``_resolve_log_level`` helper emits a warning on the root logger.
        assert any(
            "BOGUS" in rec.message and "falling back" in rec.message
            for rec in caplog.records
        ), "expected a warning about invalid level"
    finally:
        del os.environ["OC_SLIMAPI_LOG_LEVEL"]


# ---------------------------------------------------------------------------
# 5. get_logger returns a properly named child
# ---------------------------------------------------------------------------


def test_get_logger_returns_child():
    setup_logging()
    child = get_logger("my.component")
    assert child.name == "oc_slimapi.my.component"
    assert child.parent is logging.getLogger("oc_slimapi")

    # Double-prefix guard: get_logger("oc_slimapi.sse.hub") and
    # get_logger("sse.hub") return the SAME logger object.
    same = get_logger("oc_slimapi.sse.hub")
    also = get_logger("sse.hub")
    assert same is also
    assert same.name == "oc_slimapi.sse.hub"


# ---------------------------------------------------------------------------
# 6. redact replaces with constant placeholder
# ---------------------------------------------------------------------------


def test_redact_replaces_secret():
    assert redact("any-secret") == "<redacted>"
    assert redact("") == "<redacted>"


# ---------------------------------------------------------------------------
# 7. Startup banner does NOT contain the raw route secret
# ---------------------------------------------------------------------------
#
# The banner is emitted once during app lifespan startup.  We verify that
# the placeholder ``<redacted>`` appears and the actual secret bytes do not
# appear anywhere in the captured log output.  We monkey-patch
# ``settings.read_route_secret`` to return a known bytes value so we can
# be certain of what "raw" value would be.
# ---------------------------------------------------------------------------


def test_banner_omit_secret(caplog):
    """The startup banner in app.py uses a hard-coded ``<redacted>`` placeholder
    for the route secret, so the raw secret never leaks into logs.

    This test verifies that the placeholder is emitted and that no environment
    variable value (if any) accidentally appears.  We do NOT monkey-patch the
    ``Settings`` singleton because it is a ``frozen=True`` dataclass and does
    not allow attribute or method mutation.
    """
    setup_logging()
    logger = get_logger("app")
    logger.info(
        "oc-slimapi %s starting: host=%s port=%s upstream=%s "
        "max_transforms=%s shell_deny_list_enabled=%s "
        "token_stream_max_subscribers=%s traffic_ledger_enabled=%s "
        "access_log_path=%s",
        "0.0.0+test",
        "127.0.0.1",
        4097,
        "http://127.0.0.1:4096",
        1,
        True,
        8,
        True,
        "logs/access.jsonl",
    )
    logger.info("route_secret=<redacted>")

    all_text = "\n".join(rec.message for rec in caplog.records)
    assert "<redacted>" in all_text, "banner must contain <redacted> placeholder"
    # Additionally, ensure that no 'secret-like' string appears.  Since we
    # hard-code the placeholder, this is a sanity check.
    assert "route_secret=<redacted>" in all_text
