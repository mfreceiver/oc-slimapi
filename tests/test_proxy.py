from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.proxy import install_proxy
from oc_slimapi.errors import register_error_handlers


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-proxy-test")
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set()
    register_error_handlers(app)
    install_proxy(app)
    return app


def _upstream_passthrough():
    """Mock upstream that records the request and returns a streamable 200 OK.

    Note: httpx.Response(content=...) marks ``is_stream_consumed=True`` at
    construction, which the sidecar's streaming proxy cannot re-iterate
    (``aiter_raw`` would raise ``StreamConsumed``). Using ``stream=`` keeps the
    body iterable so the proxy's ``StreamingResponse`` path works under
    MockTransport just as it does against the real HTTP transport in prod.
    """
    seen = {"path": None, "method": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method

        async def body():
            yield b'{"ok":true}'

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    return handler, seen


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"])
async def test_shell_endpoint_denied(upstream_factory, method):
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, "/session/ses_x/shell")
    assert response.status_code == 403
    # HEAD responses have no body per RFC 7231 §4.3.2; the 403 status + the
    # upstream-never-reached check below are the security-relevant assertions.
    if method != "HEAD":
        assert response.json()["code"] == "shell_not_allowed"
    assert seen["path"] is None  # upstream never reached


@pytest.mark.parametrize("path", ["/session/ses_x/shell", "/session/ses_x/shell/"])
async def test_shell_endpoint_trailing_slash_denied(upstream_factory, path):
    """F1: trailing-slash variant must also be denied (regex /?$)."""
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path)
    assert response.status_code == 403
    assert response.json()["code"] == "shell_not_allowed"
    assert seen["path"] is None


@pytest.mark.parametrize("path", [
    "/pty", "/pty/shells", "/pty/p1", "/api/pty", "/api/pty/p1", "/api/pty/p1/connect",
])
async def test_pty_tree_denied(upstream_factory, path):
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 403
    assert response.json()["code"] == "shell_not_allowed"
    assert seen["path"] is None


async def test_normal_route_proxied(upstream_factory):
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")
    assert response.status_code == 200
    assert seen["path"] == "/session"


async def test_shell_deny_disable_opt_out(upstream_factory):
    """shell_deny_list_enabled=False → shell path proxied (NOT a security guarantee)."""
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(shell_deny_list_enabled=False), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/session/ses_x/shell")
    assert response.status_code == 200
    assert seen["path"] == "/session/ses_x/shell"


async def test_slimapi_unknown_still_404(upstream_factory):
    handler, _ = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "thin_route_not_found"
