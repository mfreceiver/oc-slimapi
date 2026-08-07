"""Tests for ``oc_slimapi.app.smoke`` schema-validation branches.

``smoke()`` reads ``app.state.config.smoke_session_id`` (if set, skips the
``/session`` discovery GET), then GETs ``/session/{sid}/message?limit=1`` and
sets ``app.state.schema_degraded = not valid``.

Each test constructs a minimal harness (no full lifespan) and asserts the
resulting ``schema_degraded`` flag.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.app import smoke


def _make_app(handler) -> FastAPI:
    """Build a minimal FastAPI app with smoke()-compatible state.

    ``app.state.config`` is a SimpleNamespace with ``smoke_session_id="s1"``
    so ``smoke()`` skips the /session discovery and goes straight to the
    /session/{sid}/message GET.
    """
    app = FastAPI()
    app.state.config = SimpleNamespace(smoke_session_id="s1")
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
    )
    app.state.schema_degraded = False
    return app


async def _run_and_cleanup(app: FastAPI) -> bool:
    """Run ``smoke(app)``, return ``app.state.schema_degraded``, close client."""
    try:
        await smoke(app)
        return app.state.schema_degraded
    finally:
        await app.state.upstream.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_smoke_happy():
    """Valid message list → schema_degraded False."""
    payload = [{"info": {"id": "m1"}, "parts": [{"type": "text", "text": "hi"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded = await _run_and_cleanup(app)
    assert degraded is False


async def test_smoke_non_list():
    """Upstream returns null (non-list) → schema_degraded True."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, content=b"null",
                              headers={"Content-Type": "application/json"})

    app = _make_app(handler)
    degraded = await _run_and_cleanup(app)
    assert degraded is True


async def test_smoke_missing_info_id():
    """Message entry missing info.id → schema_degraded True."""
    payload = [{"info": {}, "parts": [{"type": "text", "text": "hi"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded = await _run_and_cleanup(app)
    assert degraded is True


async def test_smoke_parts_type_non_str():
    """parts entry type is not a string → schema_degraded True."""
    payload = [{"info": {"id": "m1"}, "parts": [{"type": 123}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded = await _run_and_cleanup(app)
    assert degraded is True


async def test_smoke_exception():
    """Upstream connection error → schema_degraded True."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    app = _make_app(handler)
    degraded = await _run_and_cleanup(app)
    assert degraded is True
