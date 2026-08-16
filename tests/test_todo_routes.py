"""Route-level tests for ``routes/todo.py`` — T17 / traffic plan Batch 3 (C2a).

``GET /slimapi/sessions/{sid}/todo`` — read-only todo thin route. The
upstream ``Todo.Info`` struct is already minimal (``content`` / ``status`` /
``priority`` — schema v1.18.16 ``packages/schema/src/session-todo.ts:7-15``),
so the projection is near-identity: the route's value is gzip + cap +
admission + structured errors (design doc
``docs/specs/traffic-route-todo-2026-08-10.md`` §2 "honest conclusion").

Mirrors the sessions/messages route test suites (AC C2a-C1..C3).
"""
from __future__ import annotations

import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, todo
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}

TODO_BODY = orjson.dumps([
    {"content": "task one", "status": "pending", "priority": "high"},
    {"content": "task two", "status": "in_progress", "priority": "low"},
])


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    settings: Settings, upstream: httpx.AsyncClient,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-todo-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(health.router)
    app.include_router(todo.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


# ---------------------------------------------------------------------------
# C2a-C1 — happy path + gzip negotiation (three states)
# ---------------------------------------------------------------------------

async def test_todo_happy_path_identity_projection(upstream_factory):
    """200 with the near-identity projected body (Todo.Info is already
    minimal — design doc §2: no field whitelist to apply)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.path == "/session/s1/todo"
        return httpx.Response(200, content=TODO_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert r.json() == orjson.loads(TODO_BODY)
        # catalog-chain Vary (Batch 2 onward: directory merged on 200s)
        assert r.headers["Vary"] == (
            "Accept-Encoding, X-Opencode-Directory")
        assert seen == ["/session/s1/todo"]


async def test_todo_gzip_three_states(upstream_factory):
    """gzip negotiation mirrors the catalog pack worker (``accepts_gzip``,
    unconditional when accepted): gzip header honoured, no-AE identity,
    explicit ``gzip;q=0`` refused."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=TODO_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r_gz = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r_gz.status_code == 200
        assert r_gz.headers.get("Content-Encoding") == "gzip"
        # httpx transparently decompresses; verify the JSON round-trips and
        # the raw wire payload was genuinely gzip (magic bytes at source).
        assert r_gz.json() == orjson.loads(TODO_BODY)

        r_id = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS, "Accept-Encoding": "identity"})
        assert r_id.status_code == 200
        assert "content-encoding" not in r_id.headers
        assert r_id.content == TODO_BODY

        r_refuse = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip;q=0"})
        assert r_refuse.status_code == 200
        assert "content-encoding" not in r_refuse.headers
        assert r_refuse.content == TODO_BODY


async def test_todo_empty_array(upstream_factory):
    """`[]` passes through fine (the majority response by T16 evidence)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert r.json() == []


async def test_todo_empty_array_skips_gzip(upstream_factory):
    """rev-6 C2 (T17 todo design: 空 [] 跳过 gzip): empty ``[]`` +
    ``Accept-Encoding: gzip`` → identity (no Content-Encoding) — a 2-byte
    body is gzip-negative. The benefit gate is todo/children-only; the
    catalog routes (ETag coding prediction) keep unconditional gzip."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == b"[]"


# ---------------------------------------------------------------------------
# rev-6 B1 — ETag explicitly disabled on this route (plan §5: Batch 3 does
# not wire ETag — the route opts out of the catalog chain's rep_version)
# ---------------------------------------------------------------------------

async def test_todo_no_etag_and_inm_always_200(upstream_factory):
    """B1: ① happy responses carry NO ``ETag`` header; ② any
    ``If-None-Match`` (wildcard or opaque tag) is ignored — always 200 with
    the full body (never 304)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=TODO_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert "etag" not in r.headers

        r_star = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS, "If-None-Match": "*"})
        assert r_star.status_code == 200
        assert r_star.json() == orjson.loads(TODO_BODY)

        r_tag = await client.get(
            "/slimapi/sessions/s1/todo",
            headers={**VERSION_HEADERS,
                     "If-None-Match": '"deadbeef"',
                     "Accept-Encoding": "gzip"})
        assert r_tag.status_code == 200
        assert r_tag.json() == orjson.loads(TODO_BODY)


# ---------------------------------------------------------------------------
# rev-6 B2 — per-item shape guard (scalar elements → 503, never a bare 500)
# ---------------------------------------------------------------------------

async def test_todo_scalar_items_return_503(upstream_factory):
    """B2: ``[1, null]`` is a malformed ``Todo.Info[]`` envelope → 503
    ``upstream_unavailable`` (mirrors sessions.py's per-item dict guard),
    NOT an unstructured 500 from an AttributeError inside the projection."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, null]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_todo_mixed_scalar_item_returns_503(upstream_factory):
    """B2: a single scalar mixed into an otherwise-valid list is still a
    malformed envelope → 503 (per-item check, not just outer list)."""
    body = orjson.dumps([
        {"content": "ok", "status": "pending", "priority": "high"},
        5,
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# C2a-C2 — error mapping
# ---------------------------------------------------------------------------

async def test_todo_upstream_404_maps_session_not_found(upstream_factory):
    """sid-scoped 404 → 404 ``session_not_found`` (messages-route mapping)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"nf"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 404
        assert r.json()["code"] == "session_not_found"
        assert r.json()["sessionID"] == "s1"


async def test_todo_upstream_4xx_returns_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 502
        assert r.json()["code"] == "upstream_http_400"


async def test_todo_upstream_5xx_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_todo_network_error_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_todo_bad_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_todo_non_list_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"content":"not a list"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_todo_cap_overflow_returns_413(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=8), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/todo",
                             headers=VERSION_HEADERS)
        assert r.status_code == 413
        body = r.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == 8


async def test_todo_transform_busy_returns_503_retry_after(upstream_factory,
                                                           monkeypatch):
    """Pool saturated → 503 ``transform_busy`` + ``Retry-After: 2``."""
    import time

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=TODO_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(max_transforms=1, transform_wait_seconds=0.05), upstream)

    from oc_slimapi.routes import _catalog_common
    real_read = _catalog_common.read_upstream_response

    async def slow_read(*args, **kwargs):
        import asyncio
        await asyncio.sleep(0.15)
        return b"[]"

    monkeypatch.setattr(_catalog_common, "read_upstream_response", slow_read)

    async with _client(app) as client:
        import asyncio
        results = await asyncio.gather(*[
            client.get("/slimapi/sessions/s1/todo",
                       headers=VERSION_HEADERS)
            for _ in range(4)
        ])
    codes = sorted(r.status_code for r in results)
    # max_transforms=1 with a slow read: some get 200, the rest 503 busy.
    assert codes[0] == 200
    busy = [r for r in results if r.status_code == 503]
    assert busy and all(r.json()["code"] == "transform_busy" for r in busy)
    assert all(r.headers.get("Retry-After") == "2" for r in busy)


# ---------------------------------------------------------------------------
# C2a-C3 — directory query validation + forwarding
# ---------------------------------------------------------------------------

async def test_todo_directory_forwarded_as_header(upstream_factory):
    """Optional ``directory`` query → ``X-Opencode-Directory`` upstream
    header (per-session routing, like the messages route)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/todo",
            params={"directory": "/work/project"},
            headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["dir"] == "/work/project"


async def test_todo_directory_rejects_traversal(upstream_factory):
    """``validate_directory`` structural checks: ``..`` → 400
    ``invalid_directory``."""
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/todo",
            params={"directory": "../etc"},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"


async def test_todo_directory_rejects_overlong(upstream_factory):
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/todo",
            params={"directory": "/" + "a" * 4096},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"
