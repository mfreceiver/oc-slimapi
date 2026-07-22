from __future__ import annotations

import asyncio

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, questions, sessions
from oc_slimapi.errors import register_error_handlers

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(
    upstream: httpx.AsyncClient,
    *,
    allowlist: set[str] | None = None,
    hubs: object | None = None,
    allowlist_ready: bool = False,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-sessions-test")
    app.state.config = _settings()
    app.state.route_secret = app.state.config.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set(allowlist or ())
    # v6 §1.3 fixture sync: initialise the same way lifespan does so the
    # ``load_products`` lock + readiness flag the routes read are always
    # present (avoids AttributeError on ``allowlist_lock`` / ``allowlist_ready``).
    app.state.allowlist_ready = allowlist_ready
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    # Optional HubRegistry: when supplied, ``load_products`` notification
    # hits the spy / real hub instead of being a silent ``getattr(...,None)``.
    if hubs is not None:
        app.state.hubs = hubs
    app.include_router(sessions.router)
    app.include_router(messages.router)
    app.include_router(questions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _upstream(handler):
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


async def test_status_upstream_404_returns_session_not_found(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"not found"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "session_not_found"
    assert body["sessionID"] == "ses_x"


async def test_status_upstream_409_returns_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, content=b'{"error":"conflict"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_http_409"


async def test_status_upstream_500_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_status_discover_bad_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_status_allowlist_miss_relaxed_returns_status(upstream_factory):
    """T4-C1/F2: per-session status 放宽 allowlist —— sid 自洽即能力。
    discover 得 /secret（非白名单）+ status map 有效 → 200，不再 400。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/secret"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(200, content=orjson.dumps({"ses_x": {"type": "busy"}}),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)  # allowlist 默认空；/secret 不在内
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"type": "busy"}


async def test_status_map_missing_sid_returns_idle(upstream_factory):
    """discover ok + allowlist ok + status map has no sid → 200 idle."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/app"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(200, content=orjson.dumps({"ses_other": {"type": "busy"}}),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"type": "idle"}


async def test_status_map_4xx_returns_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/app"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(403, content=b"forbidden")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_http_403"


async def test_status_map_non_mapping_json_returns_503(upstream_factory):
    """Status-map returns valid-but-non-mapping JSON (e.g. null/array) → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/app"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(200, content=orjson.dumps([]),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


async def test_projects_failure_renders_code(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/projects", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


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


async def test_status_discover_network_error_returns_503(upstream_factory):
    """G2 discover raises httpx.RequestError (network) → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_status_discover_missing_directory_returns_503(upstream_factory):
    """G2 discover 200 but body lacks `directory` key → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=orjson.dumps({"id": "ses_x"}),
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_status_discover_non_string_directory_returns_503(upstream_factory):
    """G2 discover 200 but `directory` is non-string → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": 123}),
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_status_map_5xx_returns_503(upstream_factory):
    """discover ok + allowlist ok + status-map 5xx → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/app"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_projects_4xx_returns_502_upstream_http_n(upstream_factory):
    """projects() 4xx (non-404) → 502 upstream_http_N (T2-C6)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, content=b'{"error":"conflict"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/projects", headers=VERSION_HEADERS)
    assert response.status_code == 502
    assert response.json() == {"code": "upstream_http_409"}


async def test_batch_status_passthrough_unknown_directory(upstream_factory):
    """slimapi no longer gates directories — ``?directory=/nope`` is forwarded
    to upstream opencode normalized; whatever opencode returns (status map +
    status code) passes through unchanged. Allowlist miss used to 400 with
    ``directory_not_allowed``; that gate is removed and opencode now decides.

    The handler below returns an empty status map with 200, mirroring what a
    real opencode would respond for an unknown directory it cannot serve
    politely; slimapi surfaces it verbatim.
    """
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            captured["dir"] = request.headers.get("x-opencode-directory")
            captured["query"] = request.url.params.get("directory")
            return httpx.Response(200, content=b"{}",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)  # allowlist 默认空；不再 gate
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/status?directory=/nope", headers=VERSION_HEADERS)
    assert response.status_code == 200
    # Directory is normalised and forwarded both as query and header.
    assert captured["query"] == "/nope"
    assert captured["dir"] == "/nope"


async def test_load_products_takes_app_state(upstream_factory):
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([{"id": "p1", "worktree": "/app"}]),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        return httpx.Response(404, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    result = await sessions_mod.load_products(app)
    assert any(p["id"] == "p1" for p in result)
    assert app.state.directory_allowlist == {"/app"}


async def test_warm_allowlist_swallows_upstream_error(upstream_factory):
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    await sessions_mod.warm_allowlist(app)
    assert app.state.directory_allowlist == set()


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
    app = _build_app(upstream, allowlist=set())  # discovery allowlist empty
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
    app = _build_app(upstream, allowlist=set())
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
#   * X-Discovery-Directories : len(directory_allowlist)
#   * X-Discovery-Ready : allowlist_ready (last-known-good boolean)
# Error responses (502 / 503) must NOT carry these.
# ---------------------------------------------------------------------------

async def test_sessions_response_has_completeness_headers(upstream_factory):
    """200 OK: all three headers present with sensible values."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Return 3 items so X-Complete is "true" for limit=5.
        return httpx.Response(200, content=orjson.dumps([
            {"id": "s1", "directory": "/a"},
            {"id": "s2", "directory": "/a"},
            {"id": "s3", "directory": "/a"},
        ]), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/a", "/b"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?limit=5", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.headers["X-Complete"] == "true"
    assert response.headers["X-Discovery-Directories"] == "2"
    # Default fixture has allowlist_ready=False; X-Discovery-Ready must say so.
    assert response.headers["X-Discovery-Ready"] == "false"


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
        assert "X-Discovery-Directories" not in response.headers
        assert "X-Discovery-Ready" not in response.headers


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


async def test_sessions_discovery_ready_three_states(upstream_factory):
    """X-Discovery-Ready mirrors the three states the field can hold:
    False (initial), True with non-empty allowlist, True with empty allowlist
    (last-known-good, "权威空" — startup warm-up failed then found nothing)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)

    # State 1: ready=False, allowlist empty → "false" / "0".
    app = _build_app(upstream, allowlist=set(), allowlist_ready=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert r.headers["X-Discovery-Ready"] == "false"
    assert r.headers["X-Discovery-Directories"] == "0"

    # State 2: ready=True, allowlist non-empty.
    app = _build_app(upstream, allowlist={"/a", "/b", "/c"}, allowlist_ready=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert r.headers["X-Discovery-Ready"] == "true"
    assert r.headers["X-Discovery-Directories"] == "3"

    # State 3: ready=True, allowlist empty (last-known-good found nothing).
    app = _build_app(upstream, allowlist=set(), allowlist_ready=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert r.headers["X-Discovery-Ready"] == "true"
    assert r.headers["X-Discovery-Directories"] == "0"


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
        app = _build_app(upstream, allowlist={"/a"}, allowlist_ready=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code == 503, f"body={bad_body!r}"
        assert response.json()["code"] == "upstream_unavailable"
        assert "X-Complete" not in response.headers
        assert "X-Discovery-Directories" not in response.headers
        assert "X-Discovery-Ready" not in response.headers


# ---------------------------------------------------------------------------
# v6 §3.3: load_products failure isolation
#   * Top-level /project non-list → refresh failure, last-known-good preserved
#   * Per-directory /project/{id}/directories non-list → refresh failure,
#     last-known-good preserved
# ---------------------------------------------------------------------------

async def test_load_products_top_level_non_list_is_refresh_failure(upstream_factory):
    """A non-list body on /project must NOT overwrite allowlist or flip
    allowlist_ready=True. Pre-v5 this would have set the empty set as
    authoritative and possibly emitted a fake discovery_changed."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=b'{"not": "a list"}',
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/preexisting"}, allowlist_ready=True)
    # Pre-refresh: state is exactly what we set.
    assert app.state.directory_allowlist == {"/preexisting"}
    assert app.state.allowlist_ready is True

    with pytest.raises(ValueError, match=r"not a list"):
        await sessions_mod.load_products(app)

    # Refresh failed → state preserved untouched.
    assert app.state.directory_allowlist == {"/preexisting"}
    assert app.state.allowlist_ready is True


async def test_load_products_top_level_non_list_keeps_ready_false_on_first_call(upstream_factory):
    """First call: ready starts False; bad shape must NOT flip it to True."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=b"null",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)  # allowlist_ready defaults to False
    assert app.state.allowlist_ready is False

    with pytest.raises(ValueError, match=r"not a list"):
        await sessions_mod.load_products(app)

    # Still cold.
    assert app.state.allowlist_ready is False
    assert app.state.directory_allowlist == set()


async def test_load_products_per_directory_non_list_is_refresh_failure(upstream_factory):
    """v6 §3.3 #4 per-directory: any /project/{id}/directories returning a
    non-list aborts the WHOLE refresh (gather propagates). Pre-v6 this
    silently treated bad shape as empty dirs, fabricating a possibly
    incomplete new_set and triggering a fake discovery_changed."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
                {"id": "p2", "worktree": "/other"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=orjson.dumps([
                {"directory": "/app/sub"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p2/directories":
            # Bad shape: dict instead of list. Should abort the refresh.
            return httpx.Response(200, content=b'{"oops": true}',
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404, content=b"")

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/preexisting"}, allowlist_ready=True)

    with pytest.raises(ValueError, match=r"directories body is not a list"):
        await sessions_mod.load_products(app)

    # Last-known-good preserved — neither p1's /app+sub nor p2's /other
    # leaked into the allowlist.
    assert app.state.directory_allowlist == {"/preexisting"}
    assert app.state.allowlist_ready is True


async def test_load_products_per_directory_non_list_keeps_state_cold(upstream_factory):
    """Same as above but on the very first call: ready must stay False."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([{"id": "p1"}]),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b'"not a list"',
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    assert app.state.allowlist_ready is False

    with pytest.raises(ValueError, match=r"directories body is not a list"):
        await sessions_mod.load_products(app)

    assert app.state.allowlist_ready is False
    assert app.state.directory_allowlist == set()


# ---------------------------------------------------------------------------
# v6 §3.1 + §3.2: load_products → server.reconfigured
#   * set changes → notify (when a subscriber is listening)
#   * ready False→True (even with empty set) → notify
#   * set same + ready already True → no notify
#   * no hub / no subscribers → no-op (no lazy hub creation)
# ---------------------------------------------------------------------------

class _SpyHubs:
    """Spy HubRegistry: records every ``notify_reconfigured_if_active`` call.

    Mirrors only the methods ``load_products`` actually uses; the real
    ``HubRegistry`` is exercised by ``test_hub.py``."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    def notify_reconfigured_if_active(self, reason: str) -> int:
        self.calls.append(reason)
        return 0


async def test_load_products_set_change_emits_discovery_changed(upstream_factory):
    """Successful refresh with a *changed* allowlist calls
    ``notify_reconfigured_if_active("discovery_changed")`` exactly once."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=orjson.dumps([
                {"directory": "/app/sub"},
            ]), headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    spy = _SpyHubs()
    app = _build_app(
        upstream, allowlist=set(), allowlist_ready=True, hubs=spy,
    )
    await sessions_mod.load_products(app)

    assert spy.calls == ["discovery_changed"]
    # State after first refresh.
    assert app.state.directory_allowlist == {"/app", "/app/sub"}


async def test_load_products_ready_false_to_true_with_empty_set_still_emits(upstream_factory):
    """v6 §3.3 #2: ready False→True (with an empty set) MUST still notify —
    otherwise a startup that found no projects would leave clients
    permanently stale."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            # Valid list, but empty → set stays empty AND ready flips.
            return httpx.Response(200, content=b"[]",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    spy = _SpyHubs()
    app = _build_app(
        upstream, allowlist=set(), allowlist_ready=False, hubs=spy,
    )
    await sessions_mod.load_products(app)

    # Notification fires on the readiness transition, not on set diff.
    assert spy.calls == ["discovery_changed"]
    assert app.state.directory_allowlist == set()
    assert app.state.allowlist_ready is True


async def test_load_products_no_change_no_notify(upstream_factory):
    """Same set + already ready=True → no notify (the main suppression path
    from v6 §3.2; protects against spurious re-fires on harmless refreshes)."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    spy = _SpyHubs()
    # Pre-seed with the exact set the refresh will produce, plus ready=True.
    app = _build_app(
        upstream, allowlist={"/app"}, allowlist_ready=True, hubs=spy,
    )
    await sessions_mod.load_products(app)

    # Set is unchanged AND ready was already True → no notification.
    assert spy.calls == []
    # And the set/ready survived untouched.
    assert app.state.directory_allowlist == {"/app"}
    assert app.state.allowlist_ready is True


async def test_load_products_first_success_with_set_change_emits_once(upstream_factory):
    """First call from cold (ready=False) when the new set is non-empty
    matches BOTH conditions (set changed AND ready False→True) — but the
    notification must still fire exactly once (not twice)."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    spy = _SpyHubs()
    app = _build_app(
        upstream, allowlist=set(), allowlist_ready=False, hubs=spy,
    )
    await sessions_mod.load_products(app)

    # Exactly one notification — both predicate arms may be true but the
    # implementation must coalesce to a single emit.
    assert spy.calls == ["discovery_changed"]
    assert app.state.directory_allowlist == {"/app"}
    assert app.state.allowlist_ready is True


async def test_load_products_no_hubs_attr_is_silent_noop(upstream_factory):
    """If ``app.state.hubs`` is missing (legacy / future code path) the
    notification must NOT crash — discovery state is still written,
    ``getattr(..., None)`` short-circuits the call."""
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set(), allowlist_ready=False)
    # Explicitly REMOVE hubs so we hit the ``getattr(...,None)`` branch.
    if hasattr(app.state, "hubs"):
        delattr(app.state, "hubs")
    # Must not raise.
    await sessions_mod.load_products(app)
    assert app.state.directory_allowlist == {"/app"}
    assert app.state.allowlist_ready is True


async def test_load_products_lock_serializes_concurrent_calls(upstream_factory):
    """v6 §3.3 #1: ``allowlist_lock`` is held for the full fetch + commit
    window, so two coroutines that both want to refresh cannot tear the
    allowlist into a half-written intermediate state. Concrete: hold the
    lock in coroutine A, then start B. B cannot enter the critical section
    until A releases. After both complete, the final set is one of the
    two committed values, not a torn mix of the two paths."""
    app = _build_app(upstream_factory(lambda req: httpx.Response(200, b"[]")),
                     allowlist=set(), allowlist_ready=False)

    async def fake_load(allowlist_value: str) -> None:
        async with app.state.allowlist_lock:
            await asyncio.sleep(0)  # yield so the other coroutine interleaves
            app.state.directory_allowlist = {allowlist_value}
            app.state.allowlist_ready = True

    await asyncio.gather(fake_load("/a"), fake_load("/b"))
    # Final state is one of the two commits, not a torn mix.
    assert app.state.directory_allowlist in ({"/a"}, {"/b"})
    assert app.state.allowlist_ready is True


# ---------------------------------------------------------------------------
# v6 §3.1: end-to-end reconfigured frame via load_products
# ---------------------------------------------------------------------------

async def test_load_products_reconfigured_frame_lands_on_subscriber(upstream_factory):
    """With a real HubRegistry and one active subscriber, a successful
    ``load_products`` that changes the set pushes a ``server.reconfigured``
    frame onto that subscriber's queue. End-to-end wire test (not just the
    notify_reconfigured call count)."""
    from oc_slimapi.routes import sessions as sessions_mod
    from oc_slimapi.sse.hub import GlobalHub, HubRegistry, Subscriber

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([
                {"id": "p1", "worktree": "/app"},
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=orjson.dumps([
                {"directory": "/app/sub"},
            ]), headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    registry = HubRegistry(client=None)
    # Pre-create the hub + one subscriber so the notify has someone to push to.
    sub = registry.subscribe()
    hub = registry.get_global()

    app = _build_app(
        upstream, allowlist=set(), allowlist_ready=False, hubs=registry,
    )

    await sessions_mod.load_products(app)

    # Drain the welcome frame.
    welcome = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
    assert b"event: server.connected" in welcome

    # Next frame is the reconfigured notification.
    frame = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
    assert b"event: server.reconfigured" in frame
    # orjson dumps without whitespace between key and value.
    assert b'"reason":"discovery_changed"' in frame
    # ``at`` is an int — just check the key is present.
    assert b'"at":' in frame
    # State after.
    assert app.state.directory_allowlist == {"/app", "/app/sub"}
    assert app.state.allowlist_ready is True

    # Cleanup the hub so the test event loop doesn't yell about pending tasks.
    await registry.close()

