"""v3-contract §6.2 Vary-matrix fixes (rev-gpt gate B1 + C3).

Two findings, one principle:

* **B1** — ``GET /slimapi/messages/{sid}/full/{mid}`` consumes and forwards
  ``directory`` (the body varies with the selected workdir instance), but
  the shared ``transform._pack_json`` path only ever produced
  ``Vary: Accept-Encoding``. Directory-sensitive ⇒ Vary must carry both
  dimensions.
* **C3** — with ``OC_SLIMAPI_ETAG_ENABLED=false`` the messages/sessions
  tails and the CACHED agent/command path degrade to ``Vary:
  Accept-Encoding`` only. Vary is cache-correctness semantics, NOT an ETag
  accessory (repo precedent: Batch 3 ``merge_directory_vary`` merges even
  when ``rep_version=None``) — so directory-sensitive routes keep the
  directory dimension unconditionally, ETag switch notwithstanding.

Asserted across BOTH wire views (directory-sensitivity is a
representation property — it does not change with ``?v=``).
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.catalog_cache import CatalogCache
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import (
    agent,
    children,
    command,
    diff,
    health,
    messages,
    sessions,
    todo,
    versions,
)
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
V2_HEADERS = {"X-Slimapi-Version": "2", **IDENTITY}
DOUBLE_VARY = "Accept-Encoding"
DIRECTORY_DIMENSION = "X-Opencode-Directory"


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _message_list_payload() -> bytes:
    return orjson.dumps([
        {"info": {"id": "m1", "role": "user", "time": {"created": 1}},
         "parts": [{"id": "p1", "type": "text", "messageID": "m1",
                    "text": "hello"}]},
    ])


def _single_message_payload() -> bytes:
    return orjson.dumps({
        "info": {"id": "m1", "role": "user", "time": {"created": 1}},
        "parts": [{"id": "p1", "type": "text", "messageID": "m1",
                   "text": "hello"}],
        "state": {"metadata": {"diagnostics": {"never-consumed": True}}},
    })


def _sessions_payload() -> bytes:
    return orjson.dumps([{"id": "s1", "title": "one"}])


def _default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    json_headers = {"Content-Type": "application/json"}
    if "/message/" in path:  # /session/{sid}/message/{mid} — single /full
        return httpx.Response(200, content=_single_message_payload(),
                              headers=json_headers)
    if path.endswith("/message"):  # /session/{sid}/message — list
        return httpx.Response(200, content=_message_list_payload(),
                              headers=json_headers)
    if path == "/session":
        return httpx.Response(200, content=_sessions_payload(),
                              headers=json_headers)
    return httpx.Response(200, content=b"[]", headers=json_headers)


def _build_app(*, settings: Settings, cache: CatalogCache | None = None,
               handler=None) -> FastAPI:
    app = FastAPI(title="oc-slimapi-vary-directory-test")
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler or _default_handler),
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    if cache is not None:
        app.state.catalog_cache = cache
    for router in (
        health.router, versions.router, agent.router, command.router,
        sessions.router, todo.router, children.router, diff.router,
        messages.router,
    ):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


@pytest.fixture
async def stack():
    made: list[FastAPI] = []

    async def make(*, settings: Settings | None = None,
                   cache: CatalogCache | None = None,
                   handler=None) -> httpx.AsyncClient:
        app = _build_app(
            settings=settings or _settings(), cache=cache, handler=handler)
        made.append(app)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield make
    for app in made:
        app.state.transforms.shutdown()
        await app.state.upstream.aclose()


def _vary(response: httpx.Response) -> str:
    return response.headers.get("Vary", "")


def _has_directory_dimension(response: httpx.Response) -> bool:
    return DIRECTORY_DIMENSION in [
        p.strip() for p in _vary(response).split(",")]


# ---------------------------------------------------------------------------
# B1 — /full/{mid} Vary (unconditional; both wire views)
# ---------------------------------------------------------------------------

async def test_full_message_vary_v2_form_rejected(stack):
    """Terminal §2: the v2 form (header only) is retired — 400 before any
    Vary semantics apply."""
    client = await stack()
    try:
        response = await client.get(
            "/slimapi/messages/s1/full/m1", headers=V2_HEADERS)
        assert response.status_code == 400
        assert response.json()["code"] == "unsupported_version"
    finally:
        await client.aclose()


async def test_full_message_vary_double_v3(stack):
    client = await stack()
    try:
        response = await client.get(
            "/slimapi/messages/s1/full/m1?v=3", headers=IDENTITY)
        assert response.status_code == 200
        assert _vary(response) == DOUBLE_VARY
    finally:
        await client.aclose()


async def test_full_message_vary_double_with_directory_forwarded(stack):
    """The forwarded X-Opencode-Directory pairs with the declared Vary
    dimension (body genuinely varies with the workdir instance)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_single_message_payload(),
            headers={"Content-Type": "application/json"})

    client = await stack(handler=handler)
    try:
        response = await client.get(
            "/slimapi/messages/s1/full/m1?v=3&directory=/w",
            headers=IDENTITY)
        assert response.status_code == 200
        assert _vary(response) == DOUBLE_VARY
        assert seen[-1].headers.get(DIRECTORY_DIMENSION) == "/w"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# C3 — etag_enabled=false: directory-sensitive routes keep the directory
#       Vary dimension unconditionally
# ---------------------------------------------------------------------------

async def test_messages_list_etag_off_vary_double_v3(stack):
    client = await stack(settings=_settings(etag_enabled=False))
    try:
        v2 = await client.get("/slimapi/messages/s1", headers=V2_HEADERS)
        v3 = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        assert v2.status_code == 400  # terminal: v2 form retired
        assert v3.status_code == 200
        assert _vary(v3) == DOUBLE_VARY
        # ETag really is off (this is the degradation under repair).
        assert "ETag" not in v3.headers
    finally:
        await client.aclose()


async def test_sessions_list_etag_off_vary_double_v3(stack):
    client = await stack(settings=_settings(etag_enabled=False))
    try:
        v2 = await client.get("/slimapi/sessions", headers=V2_HEADERS)
        v3 = await client.get("/slimapi/sessions?v=3", headers=IDENTITY)
        assert v2.status_code == 400  # terminal: v2 form retired
        assert v3.status_code == 200
        assert _vary(v3) == DOUBLE_VARY
        assert "ETag" not in v3.headers
    finally:
        await client.aclose()


@pytest.mark.parametrize("path", ["/slimapi/agent", "/slimapi/command"])
async def test_catalog_cached_etag_off_vary_double(stack, path):
    """Cached agent/command path (miss + hit) — the C3 report's cached-path
    degradation."""
    cache = CatalogCache(ttl_seconds=300.0, max_entries=16,
                         max_bytes=16 * 1024 * 1024, max_entry_bytes=1024 * 1024)
    client = await stack(settings=_settings(etag_enabled=False), cache=cache)
    try:
        miss = await client.get(f"{path}?v=3", headers=IDENTITY)
        hit = await client.get(f"{path}?v=3", headers=IDENTITY)
        assert miss.status_code == hit.status_code == 200
        assert _vary(miss) == DOUBLE_VARY
        assert _vary(hit) == DOUBLE_VARY
        assert "ETag" not in miss.headers
    finally:
        await client.aclose()


@pytest.mark.parametrize("path", ["/slimapi/agent", "/slimapi/command"])
async def test_catalog_uncached_etag_off_vary_double(stack, path):
    """Uncached agent/command (no app.state.catalog_cache) — regression for
    the path that already merged (Batch 3 precedent)."""
    client = await stack(settings=_settings(etag_enabled=False))
    try:
        response = await client.get(f"{path}?v=3", headers=IDENTITY)
        assert response.status_code == 200
        assert _vary(response) == DOUBLE_VARY
    finally:
        await client.aclose()


@pytest.mark.parametrize("path", [
    "/slimapi/sessions/s1/todo",
    "/slimapi/sessions/s1/children",
    "/slimapi/sessions/s1/diff",
])
async def test_todo_children_diff_vary_double_regression(stack, path):
    """todo/children/diff opt out of ETag by design but always merged —
    must stay double (they are directory-sensitive too)."""
    client = await stack()
    try:
        response = await client.get(f"{path}?v=3", headers=IDENTITY)
        assert response.status_code == 200
        assert _vary(response) == DOUBLE_VARY
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Non-directory routes must NOT gain the dimension (AE-only or no Vary)
# ---------------------------------------------------------------------------

async def test_non_directory_routes_never_gain_dimension(stack):
    client = await stack(settings=_settings(etag_enabled=False))
    try:
        health_response = await client.get(
            "/slimapi/health?v=3", headers=IDENTITY)
        versions_response = await client.get(
            "/slimapi/versions?v=3", headers=IDENTITY)
        for response in (health_response, versions_response):
            assert response.status_code == 200
            assert not _has_directory_dimension(response)
            if "Vary" in response.headers:
                assert _vary(response) == "Accept-Encoding"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# ETag-on regression — exact merged value, no double-append, ETag present
# ---------------------------------------------------------------------------

async def test_etag_on_regression_exact_vary_and_etag(stack):
    client = await stack()
    try:
        cases = [
            ("/slimapi/messages/s1?v=3", "ETag"),
            ("/slimapi/sessions?v=3", "ETag"),
            ("/slimapi/agent?v=3", "ETag"),
            ("/slimapi/command?v=3", "ETag"),
            ("/slimapi/messages/s1/full/m1?v=3", None),  # /full never ETag
        ]
        for path, etag_key in cases:
            response = await client.get(path, headers=IDENTITY)
            assert response.status_code == 200, path
            assert _vary(response) == DOUBLE_VARY, path
            if etag_key:
                assert etag_key in response.headers, path
            else:
                assert "ETag" not in response.headers, path
    finally:
        await client.aclose()


async def test_etag_on_sessions_304_vary_exact(stack):
    client = await stack()
    try:
        first = await client.get("/slimapi/sessions?v=3", headers=IDENTITY)
        reval = await client.get(
            "/slimapi/sessions?v=3",
            headers={**IDENTITY, "If-None-Match": first.headers["ETag"]})
        assert reval.status_code == 304
        assert _vary(reval) == DOUBLE_VARY
    finally:
        await client.aclose()
