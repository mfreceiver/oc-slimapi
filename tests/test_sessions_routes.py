from __future__ import annotations

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
        max_json_bytes=64 * 1024 * 1024, max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(upstream: httpx.AsyncClient, *, allowlist: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="oc-slimapi-sessions-test")
    app.state.config = _settings()
    app.state.route_secret = app.state.config.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set(allowlist or ())
    app.state.schema_degraded = False
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
