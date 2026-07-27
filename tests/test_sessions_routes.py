from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import sessions
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(
    upstream: httpx.AsyncClient,
    *,
    hubs: object | None = None,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-sessions-test")
    app.state.config = _settings()
    app.state.upstream = upstream
    app.state.schema_degraded = False
    # Transform pool (mirrors the real app's setup; required for offload).
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=app.state.config.max_transforms,
        transform_wait_seconds=app.state.config.transform_wait_seconds,
        max_response_bytes=app.state.config.max_response_bytes,
    ))
    # Optional HubRegistry: when supplied, ``load_products`` notification
    # hits the spy / real hub instead of being a silent ``getattr(...,None)``.
    if hubs is not None:
        app.state.hubs = hubs
    app.include_router(sessions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _upstream(handler):
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )








async def test_sessions_list_upstream_4xx_returns_502(upstream_factory):
    """GET /slimapi/sessions upstream 4xx → 502 upstream_http_N (§7, sibling status pattern)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad request"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_http_400"


async def test_sessions_list_upstream_404_returns_502_upstream_http_404(upstream_factory):
    """rev-glm/rev-grok 🟡 consensus gap: GET /slimapi/sessions (list, no sid)
    with upstream /session returning 404 → HTTP 502 with code
    `upstream_http_404`, NOT `session_not_found`.

    The session_not_found mapping in _raise_upstream_status (sessions.py:157-158)
    only fires when sid is provided (single-session discover paths). The list
    handler calls _raise_upstream_status(exc) WITHOUT sid, so 404 falls through
    to the generic `status < 500` branch → 502 upstream_http_404. This test
    locks that distinction so a future refactor cannot accidentally start
    routing list-level 404s through session_not_found."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"not found"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "upstream_http_404"
    # Key negative assertion: list-level 404 is NOT session_not_found.
    assert body["code"] != "session_not_found"
    assert "sessionID" not in body


async def test_sessions_list_upstream_5xx_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 5xx → 503 upstream_unavailable (§7)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_network_error_returns_503(upstream_factory):
    """GET /slimapi/sessions httpx.RequestError → 503 upstream_unavailable (§7)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_upstream_200_bad_json_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 200 but body not JSON → 503 upstream_unavailable.

    Regression: previously response.json() raised JSONDecodeError → escaped as
    unstructured FastAPI 500 lacking {code:...} (rev-13 must-fix B)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_upstream_200_non_array_json_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 200 but JSON is non-array (dict) → 503
    upstream_unavailable.

    Regression: previously `for item in payload` iterated dict keys (str),
    skeleton_session received a str → raised → unstructured 500 (rev-13 must-fix B)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"unexpected":"shape"}',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"














# ---------------------------------------------------------------------------
# slimapi no longer gates directories (regression coverage)
# ---------------------------------------------------------------------------

async def test_sessions_list_unknown_directory_passes_through(upstream_factory):
    """``GET /slimapi/sessions?directory=/nope`` used to 400 with
    ``directory_not_allowed`` when ``/nope`` was outside the discovery
    allowlist. slimapi now normalises and forwards; opencode decides.

    Locks the new passthrough on the sessions-list endpoint specifically
    (batch /sessions/status and per-session /sessions/{sid}/status are
    covered by sibling tests).
    """
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            captured["dir"] = request.headers.get("x-opencode-directory")
            captured["query"] = request.url.params.get("directory")
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?directory=/nope", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    # Forwarded as both query and X-Opencode-Directory header.
    assert captured["query"] == "/nope"
    assert captured["dir"] == "/nope"


async def test_sessions_list_normalizes_trailing_slash_before_forward(upstream_factory):
    """``?directory=/app/`` (trailing slash) is forwarded normalised as
    ``/app`` in both query and header — the allowlist gate is gone but
    ``normalize_directory`` stays for forwarding consistency."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            captured["dir"] = request.headers.get("x-opencode-directory")
            captured["query"] = request.url.params.get("directory")
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?directory=/app/", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert captured["query"] == "/app"
    assert captured["dir"] == "/app"


# ---------------------------------------------------------------------------
# v6 §1.1: GET /slimapi/sessions response headers
#   * X-Complete        : "true" iff len(sessions) < limit (200 only)
# (lite-v2: X-Discovery-Directories / X-Discovery-Ready removed per §6)
# Error responses (502 / 503) must NOT carry X-Complete.
# ---------------------------------------------------------------------------

async def test_sessions_completeness_headers_absent_on_5xx(upstream_factory):
    """503 / 502 responses do NOT carry the completeness trio — the contract
    is "200 only". Body is still the coded error envelope."""
    for status in (500, 502, 503):
        def handler(request: httpx.Request, _s=status) -> httpx.Response:
            return httpx.Response(_s, content=b"boom")

        upstream = upstream_factory(handler)
        app = _build_app(upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code in (502, 503)
        # 503 path: "upstream_unavailable" with no completeness headers.
        assert "X-Complete" not in response.headers


async def test_sessions_x_complete_true_when_below_limit(upstream_factory):
    """len < limit → X-Complete: true (not "full page")."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=orjson.dumps([
            {"id": "s1", "directory": "/a"},
            {"id": "s2", "directory": "/a"},
        ]), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?limit=10", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.headers["X-Complete"] == "true"


async def test_sessions_x_complete_false_at_limit(upstream_factory):
    """len == limit → X-Complete: false (page is full; raise limit to recheck)."""
    def handler(request: httpx.Request) -> httpx.Response:
        # 5 items, limit=5 → full.
        return httpx.Response(200, content=orjson.dumps([
            {"id": f"s{i}", "directory": "/a"} for i in range(5)
        ]), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?limit=5", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.headers["X-Complete"] == "false"


async def test_sessions_roots_default_unchanged_false(upstream_factory):
    """``roots`` Query default is False — upstream must see ``roots=false``
    when the client omits the param. v6 explicitly does NOT flip the default;
    clients are advised to pass ``roots=true`` to exclude subagent sessions."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["roots"] = request.url.params.get("roots")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # No roots= param → default.
        r = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert r.status_code == 200
    assert captured["roots"] == "false"

    # Explicit roots=true still passes through.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/slimapi/sessions?roots=true", headers=VERSION_HEADERS)
    assert captured["roots"] == "true"


async def test_sessions_non_list_payload_returns_503_no_completeness_headers(upstream_factory):
    """v6 §1.1 isinstance guard: dict / string / null bodies (200) → 503
    upstream_unavailable, with NO completeness trio (200-only contract).
    Regression for the pre-v6 silent ``for item in payload`` that produced
    an empty skeleton list + X-Complete: true."""
    cases: list[bytes] = [
        b'{"unexpected":"shape"}',  # dict
        b'"a string"',              # str
        b"null",                    # None
        b"42",                      # number
    ]
    for bad_body in cases:
        def handler(request: httpx.Request, body: bytes = bad_body) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code == 503, f"body={bad_body!r}"
        assert response.json()["code"] == "upstream_unavailable"
        assert "X-Complete" not in response.headers










