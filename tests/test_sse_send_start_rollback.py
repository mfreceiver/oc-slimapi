"""BUG-002 regression tests: SSE admission rollback on response-start failure.

Failure-injection FI-001 (report §BUG-002): the events and token-stream
routes admit a subscriber at handler time, but the ONLY cleanup path is the
body generator's ``finally``. Starlette's ``StreamingResponse`` sends
``http.response.start`` BEFORE iterating the generator, so an ASGI ``send``
failure at response-start leaves the generator never-started — its
``finally`` never runs and the admission slot leaks (one-shot leak; N
failures exhaust the cap and every later connection 503s).

Harness (mirrors ``tests/test_token_stream_route.py`` — no sibling-test
imports): a fresh FastAPI app with BOTH SSE routers wired like
``app.lifespan``, driven by hand over raw ASGI. The failure tests inject a
``send`` that raises ``RuntimeError("INJECTED_SEND_START_FAILURE")`` on
``http.response.start``; after the exception propagates out of the app the
admission ledgers must be back at zero (and the token flush task must be
torn down). Control tests drive the same routes with a working ``send``
and confirm the normal disconnect path still cleans up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import events, token_stream
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.sse.replay_log import ReplayLog
from oc_slimapi.sse.token_hub import TokenStreamHub, TokenStreamRegistry

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


# ---------------------------------------------------------------------------
# App wiring (parity with tests/test_token_stream_route.py::_build_app).
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
        max_subscribers_per_directory=8,
        max_total_subscribers=16,
        sse_queue_items=256,
        sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
        token_stream_max_subscribers=2,
        token_stream_queue_items=64,
        token_stream_buffer_bytes=512 * 1024,
        token_stream_max_frame_bytes=1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app() -> FastAPI:
    """Fresh FastAPI app with BOTH SSE routers (events + token stream)."""
    settings = _settings()
    app = FastAPI(title="oc-slimapi-sse-send-start-rollback-test")
    app.state.config = settings
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    upstream = httpx.AsyncClient()  # unused (hub client is None) but kept for parity
    app.state.upstream = upstream
    replay_log = ReplayLog()
    app.state.replay_log = replay_log
    app.state.replay_epoch = replay_log.epoch
    hubs = HubRegistry(
        client=None,
        replay_log=replay_log,
        max_subscribers_per_directory=settings.max_subscribers_per_directory,
        max_total_subscribers=settings.max_total_subscribers,
        queue_items=settings.sse_queue_items,
        buffer_bytes=settings.sse_buffer_bytes,
        max_frame_bytes=settings.sse_max_frame_bytes,
    )
    token_hub = TokenStreamHub(replay_log=replay_log)
    hubs.set_token_hub(token_hub)
    token_registry = TokenStreamRegistry(
        token_hub,
        hubs,
        max_subscribers=settings.token_stream_max_subscribers,
        queue_items=settings.token_stream_queue_items,
        buffer_bytes=settings.token_stream_buffer_bytes,
        max_frame_bytes=settings.token_stream_max_frame_bytes,
    )
    app.state.hubs = hubs
    app.state.token_hub = token_hub
    app.state.token_registry = token_registry
    app.include_router(events.router)
    app.include_router(token_stream.router)
    register_error_handlers(app)
    return app


async def _close_app(app: FastAPI) -> None:
    app.state.token_hub.stop()
    with contextlib.suppress(Exception):
        await app.state.hubs.close()
    app.state.replay_log.close()
    await app.state.upstream.aclose()


# ---------------------------------------------------------------------------
# Manual ASGI drivers.
# ---------------------------------------------------------------------------

def _scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in VERSION_HEADERS.items()
        ],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
        "extensions": {},
    }


async def _drive_send_start_failure(app: FastAPI, path: str) -> bool:
    """FI-001: ``send`` raises on ``http.response.start``.

    Returns whether any ``http.response.body`` message was ever handed to
    ASGI (must be False — the response never started). The injected
    ``RuntimeError`` must propagate out of the app coroutine.
    """
    scope = _scope(path)
    body_started = False
    body_delivered = False

    async def receive():
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal body_started
        if message["type"] == "http.response.start":
            raise RuntimeError("INJECTED_SEND_START_FAILURE")
        if message["type"] == "http.response.body":
            body_started = True

    task = asyncio.create_task(app(scope, receive, send))
    with pytest.raises(RuntimeError, match="INJECTED_SEND_START_FAILURE"):
        await task
    await asyncio.sleep(0)  # let any finally blocks flush
    return body_started


async def _drive_normal(app: FastAPI, path: str, *, park_timeout: float = 0.4):
    """Control driver: working ``send``; park, cancel, collect the stream.

    Mirrors ``tests/test_token_stream_route.py::_drive_stream`` — the
    cancellation unwinds the generator's ``finally`` (the production
    client-disconnect path).
    """
    scope = _scope(path)
    status_code = 0
    body = bytearray()
    got_response = False
    body_delivered = False

    async def receive():
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code, got_response
        if message["type"] == "http.response.start":
            status_code = message["status"]
            got_response = True
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=park_timeout)
    except asyncio.TimeoutError:
        pass  # expected: generator parked after the initial frames
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await asyncio.sleep(0)  # let finally blocks flush
    assert got_response, "no http.response.start received"
    return status_code, bytes(body)


def _first_event_name(body: bytes) -> str | None:
    """Name of the first SSE frame in a body (control cases: slimapi.meta)."""
    for block in body.split(b"\n\n"):
        block = block.strip(b"\n")
        if not block:
            continue
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                return line[6:].strip().decode()
        data_lines = [
            line[5:].strip() for line in block.split(b"\n")
            if line.startswith(b"data:")
        ]
        if data_lines:
            json.loads(b"\n".join(data_lines).decode())  # must be valid JSON
            return None
    return None


# ---------------------------------------------------------------------------
# FI-001 — send failure at http.response.start.
# ---------------------------------------------------------------------------

async def test_events_send_start_failure_rolls_back_admission():
    """/slimapi/events: response-start send failure must NOT leak the
    control-plane admission slot (event hub back at 0)."""
    app = _build_app()
    try:
        body_started = await _drive_send_start_failure(app, "/slimapi/events")
        assert not body_started, "no body chunk may be sent after start fails"
        assert app.state.hubs.total_subscribers == 0, (
            "BUG-002: events admission slot leaked on response-start failure"
        )
        # Cross-ledger check: the token ledger was never touched.
        assert app.state.token_registry.total_subscribers == 0
        assert app.state.token_hub._flush_task is None
    finally:
        await _close_app(app)


async def test_token_stream_send_start_failure_rolls_back_admission():
    """/slimapi/sessions/{sid}/stream: response-start send failure must NOT
    leak the token admission slot NOR keep the flush task alive."""
    app = _build_app()
    try:
        body_started = await _drive_send_start_failure(
            app, "/slimapi/sessions/s1/stream")
        assert not body_started, "no body chunk may be sent after start fails"
        assert app.state.token_registry.total_subscribers == 0, (
            "BUG-002: token admission slot leaked on response-start failure"
        )
        flush_task = app.state.token_hub._flush_task
        assert flush_task is None or flush_task.done(), (
            "BUG-002: token flush task must be torn down with the rollback"
        )
        # Cross-ledger check: the control-plane ledger was never touched.
        assert app.state.hubs.total_subscribers == 0
    finally:
        await _close_app(app)


async def test_send_start_failure_does_not_permanently_exhaust_cap():
    """Repeated FI-001 failures must not exhaust the admission cap: after
    several response-start failures a normal request is still admitted
    (the report's cap-exhaustion batch)."""
    app = _build_app()
    try:
        for _ in range(3):
            await _drive_send_start_failure(app, "/slimapi/sessions/s1/stream")
        assert app.state.token_registry.total_subscribers == 0
        # A subsequent normal request still gets its slot (and returns it
        # on disconnect).
        status, _body = await _drive_normal(app, "/slimapi/sessions/s1/stream")
        assert status == 200
        assert app.state.token_registry.total_subscribers == 0
    finally:
        await _close_app(app)


# ---------------------------------------------------------------------------
# Control cases — normal send keeps the existing lifecycle intact.
# ---------------------------------------------------------------------------

async def test_control_events_normal_send_still_cleans_up():
    app = _build_app()
    try:
        status, body = await _drive_normal(app, "/slimapi/events")
        assert status == 200
        assert _first_event_name(body) == "slimapi.meta"
        assert app.state.hubs.total_subscribers == 0
        assert app.state.token_registry.total_subscribers == 0
    finally:
        await _close_app(app)


async def test_control_token_stream_normal_send_still_cleans_up():
    app = _build_app()
    try:
        status, body = await _drive_normal(app, "/slimapi/sessions/s1/stream")
        assert status == 200
        assert _first_event_name(body) == "slimapi.meta"
        assert app.state.token_registry.total_subscribers == 0
        assert app.state.token_hub._flush_task is None
        assert app.state.hubs.total_subscribers == 0
    finally:
        await _close_app(app)
