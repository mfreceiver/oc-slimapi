"""Tests that SSE hub emits expected diagnostic logs on key events."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import sys

import httpx
import orjson
import pytest

from oc_slimapi.logging_config import setup_logging
from oc_slimapi.sse.hub import GlobalHub, Subscriber
from test_token_hub_lifecycle import _FakeClient, _FakeStreamCtx, _BlockingStreamCtx


@pytest.fixture(autouse=True)
def _ensure_logging():
    """Ensure logging is configured before each test."""
    setup_logging()


def _capture_logger(name: str):
    """Return (logger, handler, buf) for the given logger name."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, handler, buf


@pytest.mark.asyncio
async def test_subscriber_attach_detach_logs():
    """subscribe and unsubscribe emit logger.info with subscriber_id."""
    hub = GlobalHub(client=None)
    logger, handler, buf = _capture_logger("oc_slimapi.sse.hub")
    try:
        sub = hub.subscribe()
        hub.unsubscribe(sub)
        buf.seek(0)
        lines = buf.readlines()
        assert any("sse subscriber attach" in l for l in lines)
        assert any("sse subscriber detach" in l for l in lines)
    finally:
        logger.removeHandler(handler)
        for t in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task):
            if t is not None and not t.done():
                t.cancel()


@pytest.mark.asyncio
async def test_backpressure_forced_disconnect_logs():
    """Backpressure overflow → logger.warning with subscriber_id."""
    hub = GlobalHub(client=None)
    logger, handler, buf = _capture_logger("oc_slimapi.sse.hub")
    try:
        sub = hub.subscribe()
        # Fill the queue with many frames each under max_frame_bytes but
        # cumulatively exceeding buffer_bytes to force backpressure.
        small_frame = b"x" * (sub.max_frame_bytes // 2)  # half of max
        assert len(small_frame) < sub.max_frame_bytes  # sanity
        # Push enough to exceed buffer_bytes.
        needed = sub.buffer_bytes // len(small_frame) + 2
        for _ in range(needed):
            sub.put(small_frame)
        buf.seek(0)
        lines = buf.readlines()
        assert any("forced disconnect" in l for l in lines)
        assert sub.closed is True
        assert sub.forced_disconnects >= 1
    finally:
        logger.removeHandler(handler)
        for t in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task):
            if t is not None and not t.done():
                t.cancel()


@pytest.mark.asyncio
async def test_json_decode_error_logs(caplog):
    """Malformed SSE JSON body → logger.debug with exc_info (no crash)."""
    # Feed a malformed JSON line + an empty line separator so run() attempts
    # to parse and fails.
    outcomes = [
        _FakeStreamCtx(lines=['data: {"bad json', '']),
    ]
    client = _FakeClient(outcomes)
    hub = GlobalHub(client=client)
    hub.subscribers.add(Subscriber())  # ensures has_consumers → loop keeps going

    # Start the run loop.
    hub.task = asyncio.create_task(hub.run())
    await asyncio.sleep(0.05)
    hub.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hub.task

    # Verify the debug log was emitted.
    hub_logs = [r for r in caplog.records if r.name == "oc_slimapi.sse.hub"]
    assert any(
        "upstream sse malformed frame dropped" in r.message
        and r.levelno == logging.DEBUG
        for r in hub_logs
    )


@pytest.mark.asyncio
async def test_traffic_accounting_failure_logs(caplog):
    """Traffic ledger raises → logger.warning with exc_info (no crash)."""
    class _RogueLedger:
        def record_sse_upstream(self, **kwargs):
            raise RuntimeError("simulated ledger failure")

    # Feed a valid line + empty line so run() attempts to parse, which
    # triggers the accounting path (record_sse_upstream) before publish.
    outcomes = [
        _FakeStreamCtx(lines=['data: {"valid": "json"}', '']),
    ]
    client = _FakeClient(outcomes)
    hub = GlobalHub(client=client, traffic_ledger=_RogueLedger())
    hub.subscribers.add(Subscriber())

    hub.task = asyncio.create_task(hub.run())
    await asyncio.sleep(0.05)
    hub.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hub.task

    hub_logs = [r for r in caplog.records if r.name == "oc_slimapi.sse.hub"]
    assert any(
        "sse traffic accounting failed" in r.message
        and r.levelno == logging.WARNING
        for r in hub_logs
    )
