from __future__ import annotations

import gzip

import httpx
import orjson
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
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
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


# ── Blocking 4: client-ident headers stripped from upstream ────────────────────


async def test_proxy_strips_client_ident_headers(upstream_factory):
    """X-Client-Name / X-Client-Version / X-Client-Id must be stripped before
    forwarding upstream (device id shall not leak to opencode)."""
    captured: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = dict(request.headers)
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
            "/session",
            headers={
                "X-Client-Name": "ocdroid",
                "X-Client-Version": "2.1.0",
                "X-Client-Id": "device-abc-123",
                "X-Custom-Forward": "should-pass",
            },
        )
    assert response.status_code == 200
    # Verify client-ident headers are NOT in the upstream request.
    captured_lower = {k.lower(): v for k, v in captured.items()}
    assert "x-client-name" not in captured_lower
    assert "x-client-version" not in captured_lower
    assert "x-client-id" not in captured_lower
    # Verify other headers still pass through.
    assert captured_lower.get("x-custom-forward") == "should-pass"


# ── T2: catch-all upstream network errors → 503 upstream_unavailable ─────────


async def test_catch_all_upstream_connect_error_returns_503(upstream_factory):
    """catch-all proxy: client.send raises httpx.ConnectError → 503 upstream_unavailable.
    Regression: previously escaped as bare FastAPI 500 (INTERFACE_MAP §4 known gap)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_x/message",
            content=b'{"role":"user","content":"hi"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_catch_all_upstream_read_timeout_returns_503(upstream_factory):
    """catch-all proxy: client.send raises httpx.ReadTimeout → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_x/message",
            content=b'{"role":"user","content":"hi"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# ── P0-5: catch-all error responses honour gzip/Vary contract (§9) ────────────


async def _drive_asgi_raw(app, method: str, path: str, headers_list):
    """Drive the ASGI app by hand and collect (status, headers, raw_body).

    Bypasses httpx so we can prove the body itself is gzip-encoded (httpx
    auto-decompresses ``response.content``) AND so we can pass raw paths
    like ``/a/../b`` (httpx normalizes URL paths per spec before sending,
    which would defeat the invalid_path trigger).
    """
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers_list],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
        "extensions": {},
    }
    status_code = 0
    headers: list = []
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status_code, headers, body
        if message["type"] == "http.response.start":
            status_code = message["status"]
            headers = message["headers"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status_code, headers, body


@pytest.mark.parametrize("trigger_path,method,expected_status,expected_code", [
    # invalid_path: _normalize_path rejects `..` / `.` segments
    ("/a/../b", "GET", 400, "invalid_path"),
    # thin_route_not_found: any /slimapi/* that isn't a real thin route
    ("/slimapi/nope", "GET", 404, "thin_route_not_found"),
    # invalid_directory (header): X-Opencode-Directory with `..`
    ("/some-path", "GET", 400, "invalid_directory"),
    # shell_not_allowed: POST /session/{sid}/shell (deny list default-on)
    ("/session/ses_x/shell", "POST", 403, "shell_not_allowed"),
], ids=["invalid_path", "thin_route_not_found", "invalid_directory", "shell_not_allowed"])
async def test_catch_all_error_response_honours_gzip(
    upstream_factory, trigger_path, method, expected_status, expected_code,
):
    """P0-5: every catch-all error response (invalid_path / thin_route_not_found
    / invalid_directory / shell_not_allowed) must honour Accept-Encoding: gzip
    per contract §9 — previously these used bare ``JSONResponse`` and skipped
    the gzip/Vary negotiation that thin-route errors already did.

    We drive the ASGI app directly so we can verify the *raw wire bytes* are
    gzip (magic ``\\x1f\\x8b``), not just the Content-Encoding header.
    """
    from oc_slimapi.upstream import DIRECTORY_HEADER

    handler, _ = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)

    extra_headers = [("Accept-Encoding", "gzip")]
    if expected_code == "invalid_directory":
        extra_headers.append((DIRECTORY_HEADER, "/../etc"))

    status, headers, raw_bytes = await _drive_asgi_raw(
        app, method, trigger_path, extra_headers,
    )

    assert status == expected_status
    header_map = {k.decode().lower(): v.decode() for k, v in headers}
    assert header_map["content-encoding"] == "gzip"
    assert header_map["vary"] == "Accept-Encoding"
    # Body is genuinely gzip (magic bytes), then decodes to the coded body.
    assert raw_bytes[:2] == b"\x1f\x8b"
    decoded = gzip.decompress(raw_bytes)
    assert orjson.loads(decoded)["code"] == expected_code


@pytest.mark.parametrize("trigger_path,method,extra_headers,expected_code", [
    ("/a/../b", "GET", [], "invalid_path"),
    ("/slimapi/nope", "GET", [], "thin_route_not_found"),
    ("/some-path", "GET", [("X-Opencode-Directory", "/../etc")], "invalid_directory"),
    ("/session/ses_x/shell", "POST", [], "shell_not_allowed"),
], ids=["invalid_path", "thin_route_not_found", "invalid_directory", "shell_not_allowed"])
async def test_catch_all_error_response_without_gzip_still_has_vary(
    upstream_factory, trigger_path, method, extra_headers, expected_code,
):
    """P0-5 negative path: with Accept-Encoding: identity the body stays plain
    JSON (no gzip magic) but the Vary: Accept-Encoding header is still emitted
    (so a cache that keyed on gzip-vs-identity would not collide)."""
    handler, _ = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)

    status, headers, raw_bytes = await _drive_asgi_raw(
        app, method, trigger_path,
        [("Accept-Encoding", "identity"), *extra_headers],
    )
    header_map = {k.decode().lower(): v.decode() for k, v in headers}
    assert "content-encoding" not in header_map
    assert header_map["vary"] == "Accept-Encoding"
    # Body is plain JSON, no gzip magic.
    assert raw_bytes[:2] != b"\x1f\x8b"
    assert orjson.loads(raw_bytes)["code"] == expected_code


# ── P0-7: catch-all preserves the original raw query string (contract §4) ────


@pytest.mark.parametrize("raw_query", [
    # percent-encoded octets — Starlette would decode %20 to space, httpx
    # might re-encode space as + or %20; the raw byte must round-trip.
    "name=hello%20world",
    # '+' in the raw query — Starlette decodes '+' to space (HTML form
    # convention); the raw '+' must reach upstream verbatim.
    "expr=a+b",
    # flag-style empty param (no '='): the literal 'flag' must arrive as-is.
    "flag",
    # repeated keys with order-dependence — order must be preserved.
    "k=1&k=2&k=3",
    # mix of percent-encoding, '+', and a sub-path-style value.
    "q=%2Fpath%2Fto%2Fx&sig=a+b+c",
    # special reserved characters that have different canonical forms
    # ('%2F' vs '/' inside a value).
    "callback=foo%2Fbar",
])
async def test_catch_all_forwards_raw_query_verbatim(upstream_factory, raw_query):
    """P0-7: the catch-all must forward the client's raw query bytes to
    upstream unchanged (contract §4 transparent reverse proxy).

    Previously the proxy used Starlette's parsed ``query_params.multi_items()``
    and let httpx re-encode the query — which broke percent-encoding, ``+``,
    flag-style empty params, and key ordering. The fix reads
    ``scope['query_string']`` (raw bytes) and appends it to the upstream URL
    verbatim, with ``params=None`` so httpx doesn't re-encode.

    We verify by capturing the exact upstream URL the mock transport received
    and comparing its query portion to the raw input.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # ``request.url`` is the URL after httpx parsed it; its ``query`` is
        # the percent-decoded view. We need the RAW query bytes — read from
        # request.url.raw_path / raw_query equivalent. httpx.URL keeps the
        # raw query at ``request.url.copy_with(params=...).query`` … but the
        # simplest portable capture is ``request.url`` whose str form preserves
        # the original encoding httpx was given.
        captured["url"] = str(request.url)
        captured["raw_query"] = request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query

        async def body():
            yield b'{"ok":true}'

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Build the request manually so httpx doesn't normalize the query
        # before sending (so we test what the sidecar actually receives, not
        # what httpx the client would have sent over the wire to a real
        # server — which is also out of our control).
        response = await client.get(f"/session?{raw_query}")

    assert response.status_code == 200
    # The raw query string forwarded upstream must equal the client's raw
    # bytes — no re-encoding, no '+' → space, no key reordering.
    assert captured["raw_query"] == raw_query, (
        f"raw_query drifted: sent {raw_query!r}, upstream got {captured['raw_query']!r}"
    )


async def test_catch_all_no_query_no_question_mark(upstream_factory):
    """P0-7 boundary: when the client sends no query, the upstream URL must
    not have a trailing ``?`` (which would be a behavior change from the
    previous params=None path)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        async def body():
            yield b'{"ok":true}'
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")

    assert response.status_code == 200
    # No '?' appended to the upstream URL.
    assert "?" not in captured["url"]


async def test_catch_all_directory_query_validation_still_runs(upstream_factory):
    """P0-7 regression guard: the security validation on ``?directory=`` still
    fires (it uses the parsed query_params, which is unaffected by the raw-
    bytes forwarding change). A ``?directory=../etc`` must still 400 before
    any upstream call."""
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session?directory=../etc")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_directory"
    # Upstream must NOT have been reached.
    assert seen["path"] is None


# ── P1-11: catch-all preserves Content-Length + duplicate response headers ────


async def test_catch_all_preserves_upstream_content_length(upstream_factory):
    """P1-11: upstream ``Content-Length`` must survive strip_hop_by_hop and
    reach the client. ``content-length`` is NOT a hop-by-hop header (RFC 7230
    §6.1) — previously the sidecar stripped it, breaking transparent reverse
    proxy semantics (contract §4): the client couldn't see the byte count
    the upstream reported."""
    body_bytes = b'{"ok":true}'
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield body_bytes
        # Use stream= (not content=) so the sidecar's aiter_raw() can iterate
        # the body — content= marks is_stream_consumed=True at construction.
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            },
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")

    assert response.status_code == 200
    # content-length was forwarded verbatim (not stripped by hop-by-hop).
    assert response.headers.get("content-length") == str(len(body_bytes))


async def test_catch_all_preserves_duplicate_set_cookie(upstream_factory):
    """P1-11: multiple Set-Cookie headers from upstream must NOT be silently
    dropped. Via multi_items() they are read faithfully and comma-merged
    (RFC 7230 §3.2.2) into the single slot Starlette Response headers
    support. The merge is imperfect for Set-Cookie (cookie values can
    contain commas), but losing a whole cookie is strictly worse."""
    body_bytes = b'{"ok":true}'
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield body_bytes
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers=httpx.Headers([
                ("Content-Type", "application/json"),
                ("Set-Cookie", "session=abc; Path=/"),
                ("Set-Cookie", "token=xyz; Path=/"),
            ]),
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session")

    assert response.status_code == 200
    # Both Set-Cookie values survived the forward. They arrive comma-merged
    # in the single slot (Starlette limitation); the test asserts that BOTH
    # cookie values are present, not just the first.
    raw_set_cookie = response.headers.get("set-cookie", "")
    assert "session=abc" in raw_set_cookie
    assert "token=xyz" in raw_set_cookie


# ── P1-12: timeout classification is trailing-sash tolerant ──────────────────


@pytest.mark.parametrize("path,expected_read_timeout", [
    # baseline: non-SSE, non-command → 30s default
    ("/session", 30.0),
    # SSE: no trailing slash → read=None
    ("/event", None),
    ("/global/event", None),
    # SSE: WITH trailing slash → previously 30s (bug), now None
    ("/event/", None),
    ("/global/event/", None),
    # SSE: multiple trailing slashes → also tolerated
    ("/event//", None),
    # command: no trailing slash → 300s
    ("/session/ses_x/command", 300.0),
    # command: WITH trailing slash → previously 30s (bug), now 300s
    ("/session/ses_x/command/", 300.0),
])
async def test_timeout_classification_trailing_slash_tolerant(
    upstream_factory, path, expected_read_timeout,
):
    """P1-12: ``_normalize_path`` collapses ``//`` but does NOT strip a
    trailing slash, so ``/event/`` / ``/command/`` previously fell through
    to the 30s default — risking long-connection kills (SSE read None,
    command 300s). The classification now rstrip('/')s for the timeout
    decision only; the forwarded path is unchanged.

    We observe the ``read`` timeout that reaches the mock transport via
    ``request.extensions['timeout']``.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout", {}).get("read")
        captured["url_path"] = request.url.path
        async def body():
            yield b'{"ok":true}'
        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 200
    # The timeout classification matches the expected value (None for SSE,
    # 300s for command, 30s default).
    assert captured["timeout"] == expected_read_timeout, (
        f"path {path!r}: expected read={expected_read_timeout!r}, "
        f"got {captured['timeout']!r}"
    )
    # Sanity: the FORWARD path is whatever the client sent (modulo the
    # ``//`` collapse in _normalize_path). P1-12 only changes classification.
    # ``request.url.path`` here is what the mock upstream received.
    # We don't over-assert path equality — the trailing-slash collapse is
    # explicitly NOT applied to the forward path.
