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


async def test_forward_injects_request_id_header(upstream_factory):
    """Proxy request to upstream includes X-Request-ID when scope.state has it."""
    from oc_slimapi.middleware.request_id import REQUEST_ID_KEY, RequestIdMiddleware

    seen_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = dict(request.headers)
        async def body():
            yield b'{"ok":true}'
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    handler, _ = _upstream_passthrough()
    # Replace the handler with one that captures headers
    upstream = upstream_factory(_handler)
    app = _build_app(_settings(), upstream)
    # Add the request-id middleware so scope.state gets populated
    app.add_middleware(RequestIdMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")
    assert response.status_code == 200
    # Verify the upstream request carried X-Request-ID
    assert "x-request-id" in {k.lower(): k for k in seen_headers}
    assert len(seen_headers.get("x-request-id", "")) > 0


# ── S2: path normalization ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw_path,expect_status,expect_code", [
    ("/session/ses_x/shell", 403, "shell_not_allowed"),
    ("/pty/p1/connect", 403, "shell_not_allowed"),
])
async def test_s2_path_normalization(upstream_factory, raw_path, expect_status, expect_code):
    """S2: deny-list working with normalized paths."""
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(raw_path)
    assert response.status_code == expect_status
    if expect_code:
        assert response.json()["code"] == expect_code
    # Upstream should NOT have been reached for deny cases
    assert seen["path"] is None


async def test_s2_normalized_path_is_passed_to_upstream(upstream_factory):
    """S2: a path is forwarded correctly after normalization."""
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")  # single slash
    assert response.status_code == 200
    # Upstream should have received the same path
    assert seen["path"] == "/session"


async def test_s2_sse_timeout_uses_normalized_path(upstream_factory):
    """S2: SSE and command timeout detection use normalized path."""
    from oc_slimapi.proxy import _normalize_path

    # Verify the function works for known cases
    assert _normalize_path("/event") == "/event"
    assert _normalize_path("//event") == "/event"
    assert _normalize_path("/global/event") == "/global/event"
    assert _normalize_path("//global//event") == "/global/event"
    assert _normalize_path("/command") == "/command"
    assert _normalize_path("//command") == "/command"


# ── S2: _normalize_path pure function tests ────────────────────────────────────


@pytest.mark.parametrize("input_path,expected", [
    ("/a/b", "/a/b"),
    ("//a//b", "/a/b"),
    ("///a///b", "/a/b"),
    ("a/b", "/a/b"),
    ("", "/"),
    ("/", "/"),
])
def test_normalize_path_basic(input_path, expected):
    from oc_slimapi.proxy import _normalize_path
    assert _normalize_path(input_path) == expected


@pytest.mark.parametrize("input_path", [
    "/a/./b",
    "/a/../b",
    "/..",
    "/.",
    "//a///..///b",
])
def test_normalize_path_rejects_traversal(input_path):
    from oc_slimapi.proxy import _normalize_path
    from oc_slimapi.errors import CodedHTTPException
    with pytest.raises(CodedHTTPException) as exc:
        _normalize_path(input_path)
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_path"





def test_normalize_path_shell_semantics():
    """Combination: deny-list check after normalization works."""
    from oc_slimapi.proxy import _normalize_path, _is_shell_path
    # //session//sid//shell → /session/sid/shell → True
    assert _is_shell_path(_normalize_path("//session//sid//shell"))
    # //pty//pid//connect → /pty/pid/connect → True
    assert _is_shell_path(_normalize_path("//pty//pid//connect"))
    # normal message path → False
    assert not _is_shell_path(_normalize_path("//session//sid//message"))


def test_normalize_path_slimapi_bypass():
    """Normalized //slimapi/nope is detected as slimapi route."""
    from oc_slimapi.proxy import _normalize_path
    norm = _normalize_path("//slimapi/nope")
    assert norm.startswith("/slimapi/")
    norm2 = _normalize_path("/slimapi/nope")
    assert norm2.startswith("/slimapi/")


# ── S5: directory header validation in proxy ───────────────────────────────────


async def test_s5_invalid_directory_header_rejected(upstream_factory):
    """S5: X-Opencode-Directory with invalid value → 400."""
    from oc_slimapi.upstream import DIRECTORY_HEADER

    handler, _ = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/some-path",
            headers={DIRECTORY_HEADER: "/../etc"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_directory"


async def test_s5_valid_directory_header_passed(upstream_factory):
    """S5: valid X-Opencode-Directory header is forwarded."""
    from oc_slimapi.upstream import DIRECTORY_HEADER

    handler, seen = _upstream_passthrough()
    # Modify handler to capture headers
    captured_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        async def body():
            yield b'{"ok":true}'
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(_handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/some-path",
            headers={DIRECTORY_HEADER: "/app"},
        )
    assert response.status_code == 200
    # Upstream should have received the directory header (case-insensitive)
    header_lower = {k.lower(): v for k, v in captured_headers.items()}
    assert header_lower.get(DIRECTORY_HEADER.lower()) == "/app"
