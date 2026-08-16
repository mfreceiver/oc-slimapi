"""Route-level tests for ``routes/children.py`` — T17 / traffic plan Batch 3 (C2a).

``GET /slimapi/sessions/{sid}/children`` — stateless re-add (v2 removal
reconciled per design doc ``docs/specs/traffic-route-children-2026-08-10.md``
§1.1/§6.2: NO X-Children-Version, NO childrenVersion digest field, NO list
hints, NO cache). Each child ``Session.Info`` is projected by the existing
``skeleton_session()`` (heavy fields ``cost`` / ``tokens`` / ``location`` /
``subpath`` dropped — identical keep/drop semantics to ``/slimapi/sessions``).

Mirrors the sessions-list route test suite (AC C2a-C1..C3).
"""
from __future__ import annotations

import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import children, health
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}


def _child(sid: str = "c1") -> dict:
    """A fully-populated ``Session.Info`` (14 fields incl. the heavy ones
    the skeleton drops)."""
    return {
        "id": sid,
        "parentID": "s1",
        "projectID": "p_1",
        "agent": "build",
        "model": "gpt/x",
        "cost": 0.42,
        "tokens": {
            "input": 1000, "output": 200, "reasoning": 50,
            "cache": {"read": 10, "write": 20},
        },
        "time": {"created": 1000, "updated": 2000},
        "title": f"child {sid}",
        "location": {"type": "file", "path": "/w/x", "remote": False},
        "subpath": "sub/dir",
    }


CHILDREN_BODY = orjson.dumps([_child("c1"), _child("c2")])


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
    app = FastAPI(title="oc-slimapi-children-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(health.router)
    app.include_router(children.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


# ---------------------------------------------------------------------------
# C2a-C1 — happy path + projection + gzip three states
# ---------------------------------------------------------------------------

async def test_children_happy_path_skeleton_projection(upstream_factory):
    """200 with each child projected by ``skeleton_session()`` verbatim:
    heavy fields (cost/tokens/location/subpath) dropped, UI fields kept —
    identical to the sessions-list projection (design doc §2)."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/s1/children"
        return httpx.Response(200, content=CHILDREN_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        projected = r.json()
        assert isinstance(projected, list) and len(projected) == 2
        for child, orig in zip(projected, orjson.loads(CHILDREN_BODY)):
            # dropped heavy fields
            for heavy in ("cost", "tokens", "location", "subpath"):
                assert heavy not in child
            # kept UI fields (SESSION_KEYS whitelist)
            for keep in ("id", "parentID", "projectID", "title", "agent",
                         "model"):
                assert child[keep] == orig[keep]
            assert child["time"] == {"created": 1000, "updated": 2000}


async def test_children_gzip_three_states(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CHILDREN_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r_gz = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r_gz.status_code == 200
        assert r_gz.headers.get("Content-Encoding") == "gzip"
        # httpx transparently decompresses; the projected JSON round-trips.
        assert orjson.loads(r_gz.content)[0]["title"] == "child c1"

        r_id = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS, "Accept-Encoding": "identity"})
        assert r_id.status_code == 200
        assert "content-encoding" not in r_id.headers
        assert orjson.loads(r_id.content)[1]["id"] == "c2"

        r_refuse = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip;q=0"})
        assert r_refuse.status_code == 200
        assert "content-encoding" not in r_refuse.headers
        assert orjson.loads(r_refuse.content)[0]["id"] == "c1"


async def test_children_empty_array(upstream_factory):
    """The majority response by T16 evidence: sessions with no children."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert r.json() == []


async def test_children_empty_array_skips_gzip(upstream_factory):
    """rev-6 C2 (T17 children design: 空 [] 跳过 gzip): empty ``[]`` +
    ``Accept-Encoding: gzip`` → identity (no Content-Encoding)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == b"[]"


# ---------------------------------------------------------------------------
# rev-6 B1 — ETag explicitly disabled on this route
# ---------------------------------------------------------------------------

async def test_children_no_etag_and_inm_always_200(upstream_factory):
    """B1: ① happy responses carry NO ``ETag`` header; ② any
    ``If-None-Match`` is ignored — always 200 with the full body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CHILDREN_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert "etag" not in r.headers

        r_star = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS, "If-None-Match": "*"})
        assert r_star.status_code == 200
        assert orjson.loads(r_star.content)[0]["id"] == "c1"

        r_tag = await client.get(
            "/slimapi/sessions/s1/children",
            headers={**VERSION_HEADERS,
                     "If-None-Match": '"deadbeef"',
                     "Accept-Encoding": "gzip"})
        assert r_tag.status_code == 200
        assert orjson.loads(r_tag.content)[0]["id"] == "c1"


# ---------------------------------------------------------------------------
# rev-6 B2 — per-item shape guard (scalar elements → 503, never a bare 500)
# ---------------------------------------------------------------------------

async def test_children_scalar_items_return_503(upstream_factory):
    """B2: ``[1, null]`` would crash ``skeleton_session()`` with an
    AttributeError → unstructured 500. The per-item dict guard (mirrors
    sessions.py:302-318) maps it to 503 ``upstream_unavailable``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, null]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_children_mixed_scalar_item_returns_503(upstream_factory):
    """B2: a single scalar mixed into an otherwise-valid list → 503
    (per-item check, not just the outer-list guard)."""
    body = orjson.dumps([_child("c1"), 5])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# C2a-C2 — error mapping
# ---------------------------------------------------------------------------

async def test_children_upstream_404_maps_session_not_found(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"nf"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 404
        assert r.json()["code"] == "session_not_found"
        assert r.json()["sessionID"] == "s1"


async def test_children_upstream_4xx_returns_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 502
        assert r.json()["code"] == "upstream_http_400"


async def test_children_upstream_5xx_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_children_network_error_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_children_bad_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_children_non_list_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"id": "not a list"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_children_cap_overflow_returns_413(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=8), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/children",
                             headers=VERSION_HEADERS)
        assert r.status_code == 413
        body = r.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == 8


async def test_children_transform_busy_returns_503_retry_after(
        upstream_factory, monkeypatch):
    """Pool saturated → 503 ``transform_busy`` + ``Retry-After: 2``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=CHILDREN_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(max_transforms=1, transform_wait_seconds=0.05), upstream)

    from oc_slimapi.routes import _catalog_common
    import asyncio

    async def slow_read(*args, **kwargs):
        await asyncio.sleep(0.15)
        return b"[]"

    monkeypatch.setattr(_catalog_common, "read_upstream_response", slow_read)

    async with _client(app) as client:
        results = await asyncio.gather(*[
            client.get("/slimapi/sessions/s1/children",
                       headers=VERSION_HEADERS)
            for _ in range(4)
        ])
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200
    busy = [r for r in results if r.status_code == 503]
    assert busy and all(r.json()["code"] == "transform_busy" for r in busy)
    assert all(r.headers.get("Retry-After") == "2" for r in busy)


# ---------------------------------------------------------------------------
# C2a-C3 — directory query validation + forwarding
# ---------------------------------------------------------------------------

async def test_children_directory_forwarded_as_header(upstream_factory):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/children",
            params={"directory": "/work/project"},
            headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["dir"] == "/work/project"


async def test_children_directory_rejects_traversal(upstream_factory):
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/children",
            params={"directory": "../etc"},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"


async def test_children_directory_rejects_overlong(upstream_factory):
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/children",
            params={"directory": "/" + "a" * 4096},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"
