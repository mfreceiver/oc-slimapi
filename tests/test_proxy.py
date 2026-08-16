"""Terminal catch-all boundary tests (v3-only terminal state, §8.2 3.0.0).

The v2-era transparent reverse proxy is CLOSED: every HTTP path that is
not a collected /slimapi route — including the retired legacy passthrough
surface (/session/**, /event, /global/event, …) — returns
404 ``thin_route_not_found``. WebSocket upgrades keep the 501 stub.

These tests replace the retired forwarder's behavioural suite (raw-query
fidelity, header stripping, timeout classification, shell deny list,
directory validation, upstream error mapping, SSE observability — none of
which can exist without forwarding).
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-proxy-test")
    app.state.config = settings
    app.state.upstream = upstream
    register_error_handlers(app)
    install_proxy(app)
    return app


def _recording_upstream():
    """Mock upstream that records every request it (never) receives."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler, seen


@pytest.mark.parametrize("method", [
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
])
async def test_closed_surface_returns_thin_route_not_found(
        upstream_factory, method):
    """§8.2 3.0.0: every method on a non-slimapi path → 404, and the
    upstream is never contacted."""
    handler, seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, "/session/ses_abc/message")
    assert response.status_code == 404
    if method != "HEAD":  # HEAD responses carry no body
        body = orjson.loads(response.content)
        assert body["code"] == "thin_route_not_found"
    assert seen == []


@pytest.mark.parametrize("path", [
    "/event",               # retired SSE passthrough
    "/global/event",        # retired SSE passthrough
    "/session/ses_abc/prompt",           # never annexed as a write endpoint
    "/session/ses_abc/prompt_async",     # annexed at /slimapi/... only
    "/session",             # retired session list passthrough
    "/config/providers",    # annexed at /slimapi/... only
    "/anything/else?x=1",
])
async def test_retired_paths_are_404_never_forwarded(upstream_factory, path):
    handler, seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path)
    assert response.status_code == 404
    assert orjson.loads(response.content)["code"] == "thin_route_not_found"
    assert seen == []


@pytest.mark.parametrize("path", [
    "/slimapi/does/not/exist",
    "/slimapi/session",                 # route miss (list lives at /sessions)
    "/slimapi/does-not-exist?v=3",
    "/slimapi",
])
async def test_slimapi_route_miss_returns_thin_route_not_found(
        upstream_factory, path):
    """`/slimapi/**` route misses keep the same 404 shape (unchanged from
    v2 — the catch-all's /slimapi branch is the surviving behaviour)."""
    handler, seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert orjson.loads(response.content)["code"] == "thin_route_not_found"
    assert seen == []


async def test_shell_paths_now_404_not_403(upstream_factory):
    """The shell/PTY deny list was forwarder machinery; with the surface
    closed those paths are plain route misses (404, no upstream call)."""
    handler, seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/session/ses_abc/shell", "/pty", "/api/pty/x"):
            response = await client.post(path)
            assert response.status_code == 404
            assert orjson.loads(response.content)["code"] == "thin_route_not_found"
    assert seen == []


async def test_directory_channels_no_longer_validated_on_closed_surface(
        upstream_factory):
    """Retired forwarder validations (X-Opencode-Directory header /
    ?directory= query) no longer exist on the closed surface — any shape
    404s uniformly; nothing reaches the upstream."""
    handler, seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get(
            "/session", headers={"X-Opencode-Directory": "../escape"})
        r2 = await client.get("/session?directory=../escape")
        r3 = await client.get(
            "/session", headers={"X-Opencode-Directory": "/w"})
    for r in (r1, r2, r3):
        assert r.status_code == 404
        assert orjson.loads(r.content)["code"] == "thin_route_not_found"
    assert seen == []


async def test_catch_all_404_honours_gzip(upstream_factory):
    """The closed-surface error responses honour the gzip/Vary contract
    (same error_response path as thin routes)."""
    handler, _seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/session/ses_abc/message",
            headers={"Accept-Encoding": "gzip"},
        )
    assert response.status_code == 404
    assert response.headers["Content-Encoding"] == "gzip"
    assert "accept-encoding" in response.headers.get("Vary", "").lower()
    # httpx transparently decodes the gzip entity — content is the JSON.
    body = orjson.loads(response.content)
    assert body["code"] == "thin_route_not_found"


async def test_catch_all_404_without_gzip_still_has_vary(upstream_factory):
    handler, _seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/session/ses_abc/message",
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 404
    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in response.headers.get("Vary", "").lower()
    assert orjson.loads(response.content)["code"] == "thin_route_not_found"


def test_websocket_route_still_mounted(upstream_factory):
    """The WebSocket catch-all stub is still mounted (501 behaviour is the
    pre-existing global WS policy; route presence is asserted here since
    httpx cannot perform an upgrade handshake)."""
    handler, _seen = _recording_upstream()
    app = _build_app(_settings(), upstream_factory(handler))
    ws_routes = [
        route for route in app.routes
        if type(route).__name__ == "APIWebSocketRoute"
    ]
    assert ws_routes, "expected the /{path:path} WebSocket 501 stub route"
