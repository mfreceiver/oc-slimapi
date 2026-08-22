"""4.11.0 Phase A / A2 (P2): thin routes join the Batch 2 ETag wiring —
``GET /slimapi/sessions/{sid}/todo|children|diff`` flip
``enable_etag=False → True``.

Frozen semantics (plan §2 A2):

* The ONLY wire change is 恒 200 → possibly 304 on a matching
  ``If-None-Match``. ``Cache-Control: no-store`` stays on EVERY response
  (200 and 304 alike) — the client must revalidate every time; the ETag
  merely saves the transport body, never freshness.
* Validators follow the §4 unified scheme: identity STRONG, gzip WEAK
  (``W/``), canonical hash over the identity bytes + coding id. The
  tiny-body gzip benefit gate (``min_gzip_bytes=MIN_GZIP_BYTES``) decides
  the served coding BEFORE the validator is derived, so the judged coding
  always equals the served coding (exact single-candidate 304).
* ``Vary: Accept-Encoding`` keeps the merged single-value form.
* agent/command regression: their 200/304 responses still carry
  ``Cache-Control: no-store`` (this file must never let a thin-route
  change leak the catalog routes' no-store discipline).
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import (
    agent as agent_routes,
    children as children_routes,
    command as command_routes,
    diff as diff_routes,
    health,
    todo as todo_routes,
)
from oc_slimapi.skeleton import skeleton_session
from oc_slimapi.transform import TransformConfig, TransformPool

HDR_IDENTITY = {
    "X-Slimapi-Version": "2",
    # Pin the identity coding (httpx advertises gzip by default).
    "Accept-Encoding": "identity",
}

# Bodies comfortably above MIN_GZIP_BYTES (64) so the gzip tests exercise
# the benefit-gated weak-validator path, and small enough for the test
# response cap (64 KiB).
TODO_BODY = orjson.dumps([
    {"content": f"task {n:03d} — write the phase A thin-route ETag suite",
     "status": "pending", "priority": "high"}
    for n in range(24)
])
CHILDREN_BODY = orjson.dumps([
    {"id": f"c{n:03d}", "name": f"child-{n:03d}", "path": f"/proj/child-{n:03d}",
     "type": "file"}
    for n in range(24)
])
DIFF_BODY = orjson.dumps([
    {"path": f"/proj/file_{n:03d}.py", "status": "modified",
     "before": n, "after": n + 1}
    for n in range(24)
])
AGENTS_BODY = orjson.dumps([
    {"name": "build", "description": "b", "mode": "primary", "prompt": "x"},
    {"name": "plan", "description": "p", "mode": "special", "prompt": "y"},
])
COMMANDS_BODY = orjson.dumps([
    {"name": "cmd", "description": "c", "agent": None, "hints": {}},
])

THIN_ROUTES = (
    # (label, route, upstream_path, expected_served_object)
    # todo/diff serve the upstream body verbatim; children serves the
    # skeleton projection of each Session.Info element.
    ("todo", "/slimapi/sessions/s1/todo", "/session/s1/todo",
     orjson.loads(TODO_BODY)),
    ("children", "/slimapi/sessions/s1/children",
     "/session/s1/children",
     [skeleton_session(i) for i in orjson.loads(CHILDREN_BODY)]),
    ("diff", "/slimapi/sessions/s1/diff", "/session/s1/diff",
     orjson.loads(DIFF_BODY)),
)


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        coalesce_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _handler():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/todo":
            return httpx.Response(200, content=TODO_BODY)
        if path == "/session/s1/children":
            return httpx.Response(200, content=CHILDREN_BODY)
        if path == "/session/s1/diff":
            return httpx.Response(200, content=DIFF_BODY)
        if path == "/agent":
            return httpx.Response(200, content=AGENTS_BODY)
        if path == "/command":
            return httpx.Response(200, content=COMMANDS_BODY)
        raise AssertionError(f"unexpected upstream path {path}")
    return handler


@pytest.fixture
async def upstream_factory():
    clients: list[httpx.AsyncClient] = []

    def _make(handler):
        client = httpx.AsyncClient(
            base_url="http://127.0.0.1:4096",
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.aclose()


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-thin-etag-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    for router in (health.router, todo_routes.router,
                   children_routes.router, diff_routes.router,
                   agent_routes.router, command_routes.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test")


def _teardown(app: FastAPI) -> None:
    app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Full header set: 200 identity strong validator + 304 replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_200_headers_and_304_replay(
        upstream_factory, label, route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(route, headers=HDR_IDENTITY)
            assert r1.status_code == 200
            etag_value = r1.headers["ETag"]
            assert etag_value.startswith('"') and not etag_value.startswith("W/")
            assert r1.headers["Vary"] == "Accept-Encoding"
            assert r1.headers["Cache-Control"] == "no-store"
            assert orjson.loads(r1.content) == expected

            r2 = await client.get(
                route, headers={**HDR_IDENTITY, "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert r2.content == b""
            assert r2.headers["ETag"] == etag_value
            assert r2.headers["Vary"] == "Accept-Encoding"
            assert r2.headers["Cache-Control"] == "no-store"
            assert "content-length" not in r2.headers
            assert "content-encoding" not in r2.headers
    finally:
        _teardown(app)


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_same_body_stable_strong_etag(
        upstream_factory, label, route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(route, headers=HDR_IDENTITY)
            r2 = await client.get(route, headers=HDR_IDENTITY)
            assert r1.status_code == r2.status_code == 200
            assert r1.headers["ETag"] == r2.headers["ETag"]
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# If-None-Match: * / list / absent / stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_star_304(upstream_factory, label, route,
                                   upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                route, headers={**HDR_IDENTITY, "If-None-Match": "*"})
            assert r.status_code == 304
            assert r.headers["Cache-Control"] == "no-store"
            assert r.headers["ETag"].startswith('"')
    finally:
        _teardown(app)


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_multi_validator_list_304(
        upstream_factory, label, route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(route, headers=HDR_IDENTITY)
            etag_value = r1.headers["ETag"]
            r2 = await client.get(route, headers={
                **HDR_IDENTITY,
                "If-None-Match": f'"deadbeef", {etag_value}, "zzz"',
            })
            assert r2.status_code == 304
            assert r2.headers["ETag"] == etag_value
    finally:
        _teardown(app)


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_no_inm_always_200(upstream_factory, label, route,
                                            upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            for _ in range(2):
                r = await client.get(route, headers=HDR_IDENTITY)
                assert r.status_code == 200
                assert r.headers["ETag"]
                assert orjson.loads(r.content) == expected
    finally:
        _teardown(app)


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_stale_validator_200(upstream_factory, label,
                                              route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                route, headers={**HDR_IDENTITY, "If-None-Match": '"0000"'})
            assert r.status_code == 200
            assert orjson.loads(r.content) == expected
            assert r.headers["Cache-Control"] == "no-store"
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# gzip: weak validator + weak comparison; coding domains never mix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_gzip_weak_validator_and_304(
        upstream_factory, label, route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            hdr = {**HDR_IDENTITY, "Accept-Encoding": "gzip"}
            r1 = await client.get(route, headers=hdr)
            assert r1.status_code == 200
            assert r1.headers["Content-Encoding"] == "gzip"
            # httpx transparently decodes the gzip transport body
            assert orjson.loads(r1.content) == expected
            weak = r1.headers["ETag"]
            assert weak.startswith('W/"')

            # replay the weak validator → 304 echoing it
            r2 = await client.get(
                route, headers={**hdr, "If-None-Match": weak})
            assert r2.status_code == 304
            assert r2.headers["ETag"] == weak
            assert r2.headers["Cache-Control"] == "no-store"

            # RFC 9110 weak comparison: the opaque-tag form (no W/ prefix)
            # of the SAME tag also matches → 304 (echo stays the weak form).
            strong_form = '"' + weak[3:]
            r3 = await client.get(
                route, headers={**hdr, "If-None-Match": strong_form})
            assert r3.status_code == 304
            assert r3.headers["ETag"] == weak
    finally:
        _teardown(app)


@pytest.mark.parametrize("label,route,upstream_path,expected", THIN_ROUTES)
async def test_thin_route_validator_domains_never_mix(
        upstream_factory, label, route, upstream_path, expected):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            gzip_hdr = {**HDR_IDENTITY, "Accept-Encoding": "gzip"}
            # identity strong tag …
            r_id = await client.get(route, headers=HDR_IDENTITY)
            strong = r_id.headers["ETag"]
            assert not strong.startswith("W/")
            # … presented under a gzip-capable request → conservative 200
            r_mixed = await client.get(
                route, headers={**gzip_hdr, "If-None-Match": strong})
            assert r_mixed.status_code == 200

            # gzip weak tag …
            r_gz = await client.get(route, headers=gzip_hdr)
            weak = r_gz.headers["ETag"]
            assert weak.startswith('W/"')
            # … presented under an identity-only request → conservative 200
            r_mixed2 = await client.get(
                route, headers={**HDR_IDENTITY, "If-None-Match": weak})
            assert r_mixed2.status_code == 200
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# Regression: agent/command keep Cache-Control: no-store (200 + 304)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/slimapi/agent", "/slimapi/command"])
async def test_agent_command_no_store_regression(upstream_factory, route):
    upstream = upstream_factory(_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(route, headers=HDR_IDENTITY)
            assert r1.status_code == 200
            assert r1.headers["ETag"]
            assert r1.headers["Vary"] == "Accept-Encoding"
            assert r1.headers["Cache-Control"] == "no-store"

            r2 = await client.get(
                route, headers={**HDR_IDENTITY,
                                "If-None-Match": r1.headers["ETag"]})
            assert r2.status_code == 304
            assert r2.headers["ETag"] == r1.headers["ETag"]
            assert r2.headers["Cache-Control"] == "no-store"
    finally:
        _teardown(app)
