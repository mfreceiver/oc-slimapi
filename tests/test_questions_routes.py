from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import questions as questions_route
from oc_slimapi.routes import sessions
from oc_slimapi.tokens import issue_route_token

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
    app = FastAPI(title="oc-slimapi-questions-test")
    app.state.config = _settings()
    app.state.route_secret = app.state.config.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set(allowlist or ())
    app.include_router(sessions.router)
    app.include_router(questions_route.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


async def test_questions_directory_count_bounds(upstream_factory):
    """0 or >32 directories → 400 invalid_directory_count."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 0 directories: FastAPI Query(...) requires ≥1 occurrence → 422.
        # Use 33 to hit our explicit 1-32 guard.
        dirs = "&".join(f"directory=/d{i}" for i in range(33))
        response = await client.get(f"/slimapi/questions?{dirs}", headers=VERSION_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_directory_count"


async def test_aggregate_empty_directories_invalid_count():
    """F11: direct call to _aggregate with 0 directories → CodedHTTPException
    invalid_directory_count. Covers the `not unique` branch that FastAPI 422s
    before reaching on the HTTP path (kept defensive for direct callers)."""
    from unittest.mock import MagicMock

    from oc_slimapi.errors import CodedHTTPException
    from oc_slimapi.routes.questions import _aggregate

    request = MagicMock()
    # The 0-directory branch raises before any app.state access, so a bare
    # MagicMock suffices (no AsyncMock setup needed).
    with pytest.raises(CodedHTTPException) as ei:
        await _aggregate(request, "question", [])
    assert ei.value.status_code == 400
    assert ei.value.code == "invalid_directory_count"


async def test_questions_bad_route_token(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/questions/q1/reply",
            headers=VERSION_HEADERS,
            json={"answers": [["a"]], "routeToken": "garbage"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_route_token"


async def test_questions_token_unknown_directory_passes_through(upstream_factory):
    """slimapi no longer gates the routeToken directory — the token's
    directory is forwarded to upstream opencode regardless of whether the
    sidecar's discovery allowlist knows about it. opencode (200 here) is
    authoritative; slimapi does not police directories.

    Previously this returned ``400 directory_not_allowed`` when the token's
    directory was outside the discovery allowlist; that gate is removed.
    """
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/question/q1/reply":
            seen["dir"] = request.headers.get("x-opencode-directory")
            seen["query"] = request.url.params.get("directory")
            return httpx.Response(204)
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set())  # discovery allowlist empty
    secret = app.state.route_secret
    token = issue_route_token(secret, kind="question", request_id="q1", session_id=None, directory="/gone")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/questions/q1/reply",
            headers=VERSION_HEADERS,
            json={"answers": [["a"]], "routeToken": token},
        )
    assert response.status_code == 204
    # Token directory normalised and forwarded to upstream verbatim.
    assert seen["query"] == "/gone"
    assert seen["dir"] == "/gone"


async def test_questions_mutation_timeout(upstream_factory):
    """Upstream POST timing out → 504 upstream_timeout (not retried)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated")

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    secret = app.state.route_secret
    token = issue_route_token(secret, kind="question", request_id="q1", session_id=None, directory="/app")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/questions/q1/reply",
            headers=VERSION_HEADERS,
            json={"answers": [["a"]], "routeToken": token},
        )
    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "upstream_timeout"
    assert body["message"] == "upstream mutation timed out; not retried"


async def test_token_cold_allowlist_then_reply(upstream_factory):
    """F3 (historic): a valid routeToken whose directory is in cold-cache
    allowlist produces 204. After [Unreleased] removed the allowlist gate,
    this same path also covers directories NOT in any allowlist — slimapi
    no longer polices directory membership, so the token's directory is
    forwarded to upstream opencode which authoritatively returns 204."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200,
                content=orjson.dumps([{"id": "p1", "worktree": "/app"}]),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        if request.url.path == "/question/q1/reply":
            return httpx.Response(204)
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set())
    secret = app.state.route_secret
    token = issue_route_token(secret, kind="question", request_id="q1", session_id=None, directory="/app")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/questions/q1/reply",
            headers=VERSION_HEADERS,
            json={"answers": [["a"]], "routeToken": token},
        )
    assert response.status_code == 204


async def test_questions_null_directory_aggregates_allowlist(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/question":
            return httpx.Response(200, content=orjson.dumps([{"id": "q1", "sessionID": "ses_1"}]),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/app"
    assert "routeToken" in body["items"][0]
    assert body["errors"] == []
    assert body["scope"] == {"directories": 1}


async def test_questions_null_directory_empty_allowlist_returns_empty_envelope(upstream_factory):
    """Cold-start: empty allowlist → scope.directories == 0 (scope not ready)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"items": [], "errors": [], "scope": {"directories": 0}}


async def test_questions_null_directory_populated_allowlist_empty_items_scope(upstream_factory):
    """Scope ready, authoritative empty: populated allowlist + upstream [] → directories == N."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app", "/foo"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []
    assert body["scope"] == {"directories": 2}


async def test_questions_explicit_directory_scope_count(upstream_factory):
    """Explicit directory list → scope.directories == unique dir count after dedupe."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app", "/foo"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/questions?directory=/app&directory=/app",
            headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []
    assert body["scope"] == {"directories": 1}


async def test_questions_explicit_directory_normalizes_before_dedupe(upstream_factory):
    """rev-13: `/app` and `/app/` are the same directory after trailing-slash
    normalization. Dedupe must run on normalized form, not raw strings —
    otherwise the sidecar fans out duplicate upstream calls and reports
    scope.directories == 2 instead of 1."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/question":
            calls["n"] += 1
            return httpx.Response(
                200, content=orjson.dumps([{"id": "q1", "sessionID": "ses_1"}]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/questions?directory=/app&directory=/app/",
            headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    # Normalized-then-deduped → exactly one directory in scope.
    assert body["scope"] == {"directories": 1}
    # Upstream fanned out exactly once (no duplicate `/question?directory=/app`).
    assert calls["n"] == 1
    # No duplicate items.
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/app"
    assert body["errors"] == []


async def test_questions_all_directories_fail_returns_503_without_scope(upstream_factory):
    """rev-glm/rev-grok 🟡 consensus gap: when EVERY directory's upstream fetch
    fails (here: explicit ?directory= list, each upstream /question returns 500),
    _aggregate returns 503 with the same {items, errors} envelope shape but
    WITHOUT the `scope` key — scope is a success-only signal (Gap 2B).

    Locks the line `if status == 200: body["scope"] = ...` (questions.py:82-83)
    so a future refactor cannot accidentally attach scope to the 503 branch."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/question":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app", "/foo"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/questions?directory=/app&directory=/foo",
            headers=VERSION_HEADERS,
        )
    assert response.status_code == 503
    body = response.json()
    # Envelope shape preserved on 503...
    assert body["items"] == []
    assert [e["code"] for e in body["errors"]] == ["upstream_http_500", "upstream_http_500"]
    assert [e["directory"] for e in body["errors"]] == ["/app", "/foo"]
    # ...but `scope` MUST be absent (key locking assertion).
    assert "scope" not in body


async def test_permissions_envelope_includes_scope(upstream_factory):
    """ /permissions shares _aggregate — same scope signal on 200 envelope."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []
    assert body["scope"] == {"directories": 1}
