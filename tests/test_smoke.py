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
    app.state.smoke_status = "not_run"
    return app


async def _run_and_cleanup(app: FastAPI) -> tuple[bool, str]:
    """Run ``smoke(app)``, return ``(schema_degraded, smoke_status)``, close client."""
    try:
        await smoke(app)
        return app.state.schema_degraded, app.state.smoke_status
    finally:
        await app.state.upstream.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_smoke_happy():
    """Valid message list → schema_degraded False, smoke_status=valid."""
    payload = [{"info": {"id": "m1"}, "parts": [{"type": "text", "text": "hi"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is False
    assert status == "valid"


async def test_smoke_non_list():
    """Upstream returns null (non-list) → invalid_schema (degraded True)."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, content=b"null",
                              headers={"Content-Type": "application/json"})

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is True
    assert status == "invalid_schema"


async def test_smoke_missing_info_id():
    """Message entry missing info.id → invalid_schema (degraded True)."""
    payload = [{"info": {}, "parts": [{"type": "text", "text": "hi"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is True
    assert status == "invalid_schema"


async def test_smoke_parts_type_non_str():
    """parts entry type is not a string → invalid_schema (degraded True)."""
    payload = [{"info": {"id": "m1"}, "parts": [{"type": 123}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/session/s1/message" in str(request.url.path)
        return httpx.Response(200, json=payload)

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is True
    assert status == "invalid_schema"


async def test_smoke_exception():
    """Upstream connection error → upstream_unavailable (NOT schema_degraded).

    P1-36: a connection error means the upstream is unreachable, not that the
    schema has regressed. ``schema_degraded`` stays False; ``smoke_status`` is
    ``upstream_unavailable`` so health diagnostics can distinguish the two.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is False
    assert status == "upstream_unavailable"


# ---------------------------------------------------------------------------
# P1-36 regression: non-2xx status → upstream_unavailable (not invalid_schema).
# ---------------------------------------------------------------------------

async def test_smoke_404_status():
    """A 404 (session gone) → upstream_unavailable, not schema regression."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is False
    assert status == "upstream_unavailable"


async def test_smoke_500_status():
    """A 5xx → upstream_unavailable, not schema regression."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"err": "boom"}')

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is False
    assert status == "upstream_unavailable"


async def test_smoke_json_decode_error():
    """Non-JSON body that can't be parsed → upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<<<not json>>>",
                              headers={"Content-Type": "application/json"})

    app = _make_app(handler)
    degraded, status = await _run_and_cleanup(app)
    assert degraded is False
    assert status == "upstream_unavailable"


# ---------------------------------------------------------------------------
# P1-36: session-list discovery failure → upstream_unavailable (not not_run).
# ---------------------------------------------------------------------------

async def test_smoke_no_explicit_sid_upstream_down():
    """No explicit sid + session-list fetch fails → upstream_unavailable."""
    app = FastAPI()
    app.state.config = SimpleNamespace(smoke_session_id=None)
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down"))),
        base_url="http://test",
    )
    app.state.schema_degraded = False
    app.state.smoke_status = "not_run"
    try:
        await smoke(app)
        assert app.state.schema_degraded is False
        assert app.state.smoke_status == "upstream_unavailable"
    finally:
        await app.state.upstream.aclose()


async def test_smoke_no_explicit_sid_empty_sessions():
    """No explicit sid + session list empty → not_run (no session to test)."""
    app = FastAPI()
    app.state.config = SimpleNamespace(smoke_session_id=None)
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
        base_url="http://test",
    )
    app.state.schema_degraded = False
    app.state.smoke_status = "not_run"
    try:
        await smoke(app)
        assert app.state.schema_degraded is False
        assert app.state.smoke_status == "not_run"
    finally:
        await app.state.upstream.aclose()
