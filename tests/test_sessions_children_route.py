"""TDD DRAFT tests for Batch 3 — ``GET /slimapi/sessions/{sid}/children``.

This module is the **authoritative specification** for the upcoming
``src/oc_slimapi/routes/sessions_children.py`` route. It is a TDD DRAFT: the
implementation does **not** exist yet, so importing this module triggers an
``ImportError`` at collection time. ``./scripts/check.sh`` is therefore
expected to be non-green (collection error) until the implementation (the
"fixer-bgpt" lane) lands both the route AND the ``ChildrenCache`` it depends
on. Existing tests outside this file are unaffected by that collection
error — they are still collected + executed when this file is excluded
(see verification commands in the change report).

The route behaviour is fixed by contract §2 ("children 投影与缓存") + §16
+ §7 (sid-aware error mapping):

* ``GET /slimapi/sessions/{sid}/children?directory=<optional>``
* 200 response body = child skeleton **array** (bare array, NO envelope).
* 200 response headers include ``X-Children-Version: <int>``.
* Errors (all routed through the registered ``CodedHTTPException`` handler,
  body = ``{"code": ...}``):
    * upstream 404 (parent missing) → 404 ``session_not_found`` (with
      ``sessionID`` field)
    * upstream other 4xx            → 502 ``upstream_http_N``
    * upstream 5xx / network / bad JSON / non-list → 503 ``upstream_unavailable``
* The route calls ``app.state.children.get_or_fetch(sid, directory)`` — the
  per-key cache is wired into ``app.state.children`` by ``app.py`` lifespan
  (dev's job). Tests inject a real ``ChildrenCache`` backed by a
  ``MockTransport`` upstream, or replace ``app.state.children`` with a fake.

This file is self-contained (its own fixtures, its own ``_build_app``) so it
touches neither ``conftest.py`` nor any existing ``test_*.py``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.children_cache import ChildrenCache
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.observability import BatchLedger
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, questions, sessions, sessions_children
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "1"}

# ---------------------------------------------------------------------------
# Self-contained fixtures (do NOT touch conftest.py)
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _make_session_info(
    sid: str, *, created: int | None = None, parent: str = "p1",
    directory: str = "/app",
) -> dict:
    info: dict[str, Any] = {"id": sid, "parentID": parent, "directory": directory}
    if created is not None:
        info["time"] = {"created": created, "updated": created}
    return info


def _ok_children(payload: list) -> httpx.Response:
    return httpx.Response(
        200, content=orjson.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


def _build_app(upstream: httpx.AsyncClient, *, with_real_cache: bool = True) -> FastAPI:
    """Construct a fresh FastAPI app mirroring ``oc_slimapi.app.lifespan`` but
    without running the smoke probe. Wires the children router + a real
    ``ChildrenCache`` (default) so end-to-end behaviour is exercised.

    Pass ``with_real_cache=False`` and set ``app.state.children`` per-test to
    a fake when you want to control cache return values directly."""
    settings = _settings()
    app = FastAPI(title="oc-slimapi-children-route-test")
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    app.state.allowlist_ready = False
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.hubs = HubRegistry(upstream)
    app.state.batch_ledger = BatchLedger(window_seconds=settings.opt_a_rollback_window_seconds)
    if with_real_cache:
        app.state.children = ChildrenCache(upstream)
    app.include_router(sessions.router)
    app.include_router(messages.router)
    app.include_router(questions.router)
    app.include_router(sessions_children.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


class _FakeChildrenCache:
    """Stand-in for ChildrenCache used by some route tests. Records calls and
    returns scripted (children, version) or raises scripted exceptions."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self.script: Any = (lambda sid, d: ([], 0))

    async def get_or_fetch(self, parent_sid: str, directory: str | None):
        self.calls.append((parent_sid, directory))
        if isinstance(self.script, BaseException):
            raise self.script
        if callable(self.script):
            return self.script(parent_sid, directory)
        return self.script

    async def aclose(self):
        pass


@pytest.fixture
async def app_with_real_cache():
    """Yield (app, upstream, cache, transport) wired with a real cache + a
    MockTransport-backed upstream whose handler the test rebinds."""
    def handler(req):
        return _ok_children([])
    upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )
    app = _build_app(upstream, with_real_cache=True)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield app, upstream, app.state.children, client
    finally:
        await client.aclose()
        await app.state.children.aclose()
        await upstream.aclose()


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.get(path, headers=VERSION_HEADERS)


# ===========================================================================
# 200 — bare array body + X-Children-Version header
# ===========================================================================


async def test_children_200_returns_bare_array_and_version_header(app_with_real_cache):
    """Spec §2: success response body is the child skeleton **array** (no
    envelope), with ``X-Children-Version: <int>`` header. The route MUST NOT
    wrap the array in an envelope (e.g. ``{"children": [...]}``)."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(lambda req: _ok_children([
        _make_session_info("c1", created=10),
        _make_session_info("c2", created=20),
    ]))
    # Rebuild app's view: the upstream object itself stays the same instance,
    # only the transport handler changes. The cache holds no entry yet so the
    # next call re-fetches through the new transport.

    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list), f"body must be bare array, got {type(body)}"
    assert len(body) == 2
    assert {c["id"] for c in body} == {"c1", "c2"}
    # Header is present and integral
    version_header = response.headers.get("X-Children-Version")
    assert version_header is not None, "X-Children-Version header missing"
    int(version_header)  # raises ValueError if not an int


# ===========================================================================
# ?directory pass-through
# ===========================================================================


async def test_children_directory_query_param_forwarded_to_upstream(app_with_real_cache):
    """Spec §2: ``?directory`` is forwarded to upstream as both the
    ``X-Opencode-Directory`` header and the ``?directory=`` query param, with
    the same normalize semantics as the rest of the routes."""
    app, upstream, _, client = app_with_real_cache
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        seen["headers"] = dict(req.headers)
        return _ok_children([])

    upstream._transport = httpx.MockTransport(handler)

    response = await _get(client, "/slimapi/sessions/p1/children?directory=/app")
    assert response.status_code == 200
    assert seen["path"].endswith("/session/p1/children")
    assert seen["params"].get("directory") == "/app"
    assert seen["headers"].get("x-opencode-directory") == "/app"


async def test_children_directory_trailing_slash_normalised(app_with_real_cache):
    """Spec §2 (normalize): ``?directory=/app/`` is normalized to ``/app``
    before forwarding (consistent with the rest of the surface)."""
    app, upstream, _, client = app_with_real_cache
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        seen["headers"] = dict(req.headers)
        return _ok_children([])

    upstream._transport = httpx.MockTransport(handler)
    response = await _get(client, "/slimapi/sessions/p1/children?directory=/app/")
    assert response.status_code == 200
    assert seen["params"].get("directory") == "/app"
    assert seen["headers"].get("x-opencode-directory") == "/app"


async def test_children_without_directory_omits_directory_header(app_with_real_cache):
    """Spec §2 (no directory): when ``?directory`` is absent, no
    ``X-Opencode-Directory`` header is forwarded and no ``?directory=`` param
    is added (matches ``forward_directory_headers`` semantics)."""
    app, upstream, _, client = app_with_real_cache
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        seen["headers"] = dict(req.headers)
        return _ok_children([])

    upstream._transport = httpx.MockTransport(handler)
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 200
    assert "directory" not in seen["params"]
    assert "x-opencode-directory" not in {k.lower() for k in seen["headers"]}


# ===========================================================================
# Sorting determinism at the route layer (cache + route both MUST preserve it)
# ===========================================================================


async def test_children_route_sorts_by_created_desc_then_id_asc(app_with_real_cache):
    """Spec §2 (slimapi-side stable sort): upstream returns children out of
    order; the route response MUST be sorted by ``time.created DESC`` then
    ``id ASC`` (tie-break). This is an additive wire guarantee."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(lambda req: _ok_children([
        _make_session_info("c1", created=10),
        _make_session_info("c2", created=30),
        _make_session_info("c3", created=20),
        _make_session_info("c4", created=30),  # tie with c2 → id asc
    ]))
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 200
    body = response.json()
    assert [c["id"] for c in body] == ["c2", "c4", "c3", "c1"]


# ===========================================================================
# 404 session_not_found  (sid-aware error mapping)
# ===========================================================================


async def test_children_upstream_404_returns_session_not_found(app_with_real_cache):
    """Spec §7 (sid-aware): upstream 404 → 404 ``session_not_found`` with
    ``sessionID`` field set to the path sid."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(
        lambda req: httpx.Response(404, content=b'{"error":"nf"}')
    )
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "session_not_found"
    assert body["sessionID"] == "p1"
    # No X-Children-Version on error responses
    assert "X-Children-Version" not in response.headers


# ===========================================================================
# 4xx (non-404) → 502 upstream_http_N
# ===========================================================================


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
async def test_children_upstream_4xx_returns_502(app_with_real_cache, status):
    """Spec §7: any non-404 4xx → 502 ``upstream_http_N`` (NO sessionID
    field — only the 404 path is sid-aware)."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(
        lambda req: httpx.Response(status, content=b'{"error":"x"}')
    )
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 502
    body = response.json()
    assert body == {"code": f"upstream_http_{status}"}
    assert "sessionID" not in body


# ===========================================================================
# 5xx / network / bad JSON / non-list → 503 upstream_unavailable
# ===========================================================================


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_children_upstream_5xx_returns_503(app_with_real_cache, status):
    """Spec §7: upstream 5xx → 503 ``upstream_unavailable``."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(
        lambda req: httpx.Response(status, content=b"boom")
    )
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


async def test_children_upstream_network_error_returns_503(app_with_real_cache):
    """Spec §7: httpx.RequestError on the upstream GET → 503."""
    app, upstream, _, client = app_with_real_cache
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=req)
    upstream._transport = httpx.MockTransport(handler)
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


async def test_children_upstream_200_bad_json_returns_503(app_with_real_cache):
    """Spec §7: 200 + non-JSON body → 503."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=b"not-json",
        headers={"Content-Type": "application/json"},
    ))
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


@pytest.mark.parametrize(
    "body,label",
    [
        (b'{"not":"a list"}', "dict"),
        (b'"a string"', "string"),
        (b"42", "number"),
        (b"null", "null"),
    ],
    ids=["dict", "string", "number", "null"],
)
async def test_children_upstream_200_non_list_json_returns_503(
    app_with_real_cache, body, label,
):
    """Spec §7 (expect=list): 200 + JSON non-list (dict/string/number/null)
    → 503 ``upstream_unavailable``. The children path MUST validate that the
    upstream payload is a list (via ``fetch_json_mapped(expect=list)``)."""
    app, upstream, _, client = app_with_real_cache
    upstream._transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=body, headers={"Content-Type": "application/json"},
    ))
    response = await _get(client, "/slimapi/sessions/p1/children")
    assert response.status_code == 503, f"shape={label!r} did not 503"
    assert response.json() == {"code": "upstream_unavailable"}, (
        f"shape={label!r} body mismatch"
    )


# ===========================================================================
# Cache integration — single-flight at the route layer
# ===========================================================================


async def test_children_route_single_flight_two_concurrent_calls(app_with_real_cache):
    """Spec §16 (single-flight): two concurrent client requests for the same
    (sid, directory) share ONE upstream GET. The cache is hit on the second
    call (no extra upstream)."""
    app, upstream, cache, client = app_with_real_cache
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _ok_children([_make_session_info("c1", created=10)])

    upstream._transport = httpx.MockTransport(handler)
    r1, r2 = await asyncio.gather(
        _get(client, "/slimapi/sessions/p1/children?directory=/app"),
        _get(client, "/slimapi/sessions/p1/children?directory=/app"),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    # Note: MockTransport handler call_count reflects the actual upstream
    # hits. With single-flight, it MUST be 1 (the cache absorbed the second).
    assert call_count["n"] == 1, (
        f"single-flight violated: upstream hit {call_count['n']} times"
    )
    # Both responses carry the same X-Children-Version (data + version 同源).
    assert r1.headers["X-Children-Version"] == r2.headers["X-Children-Version"]


async def test_children_route_serves_second_call_from_cache(app_with_real_cache):
    """Spec §16 (TTL hit): sequential calls — the second is served from cache,
    upstream hit count stays at 1."""
    app, upstream, cache, client = app_with_real_cache
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _ok_children([_make_session_info("c1", created=10)])

    upstream._transport = httpx.MockTransport(handler)
    r1 = await _get(client, "/slimapi/sessions/p1/children?directory=/app")
    r2 = await _get(client, "/slimapi/sessions/p1/children?directory=/app")
    assert call_count["n"] == 1
    assert r1.status_code == 200 and r2.status_code == 200
    # Same version on both (cached)
    assert r1.headers["X-Children-Version"] == r2.headers["X-Children-Version"]


async def test_children_route_shutdown_returns_structured_503(app_with_real_cache):
    """A request waiting on a closing cache reaches the coded error handler."""
    app, upstream, cache, client = app_with_real_cache
    started = asyncio.Event()
    release = asyncio.Event()

    async def stuck_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()
        return _ok_children([])

    upstream.get = stuck_get  # type: ignore[method-assign]
    request_task = asyncio.create_task(
        _get(client, "/slimapi/sessions/p1/children?directory=/app")
    )
    await started.wait()
    await cache.aclose()
    response = await request_task
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


# ===========================================================================
# Skeleton projection at the route layer
# ===========================================================================


async def test_children_route_applies_skeleton_session(app_with_real_cache):
    """Spec §2: children are projected through ``skeleton_session`` — non-
    whitelisted top-level keys are stripped before serialization."""
    app, upstream, _, client = app_with_real_cache
    info = _make_session_info("c1", created=10)
    info["hugeBlob"] = "x" * 1000
    upstream._transport = httpx.MockTransport(lambda req: _ok_children([info]))
    response = await _get(client, "/slimapi/sessions/p1/children")
    body = response.json()
    assert len(body) == 1
    assert "hugeBlob" not in body[0]
    for key in ("id", "parentID", "directory"):
        assert key in body[0]


# ===========================================================================
# Cache injection via app.state.children (fake) — error pass-through
# ===========================================================================


async def test_children_route_propagates_coded_exception_from_cache():
    """Spec §16 + §7: if the cache raises ``CodedHTTPException`` (e.g.
    upstream mapped error during fetch), the route MUST let it bubble to the
    registered handler → ``{"code": ...}`` body with the right status."""
    upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(lambda req: _ok_children([])),
    )
    app = _build_app(upstream, with_real_cache=False)
    fake = _FakeChildrenCache()
    from oc_slimapi.errors import CodedHTTPException
    fake.script = CodedHTTPException(404, code="session_not_found", sessionID="p1")
    app.state.children = fake

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/p1/children", headers=VERSION_HEADERS,
        )
    assert response.status_code == 404
    body = response.json()
    assert body == {"code": "session_not_found", "sessionID": "p1"}
    # The cache was called exactly once with the sid + normalized directory.
    assert fake.calls == [("p1", None)]
    await upstream.aclose()


async def test_children_route_passes_normalized_directory_to_cache():
    """Spec §2 (contract on get_or_fetch args): the route MUST pass the
    normalized directory to the cache. None when ``?directory`` is absent;
    ``"/app"`` when ``?directory=/app/`` is sent (trailing slash stripped)."""
    upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(lambda req: _ok_children([])),
    )
    app = _build_app(upstream, with_real_cache=False)
    fake = _FakeChildrenCache()
    app.state.children = fake

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/slimapi/sessions/p1/children?directory=/app/",
                         headers=VERSION_HEADERS)
        await client.get("/slimapi/sessions/p2/children", headers=VERSION_HEADERS)
    # Normalised: /app/ → /app ; absent → None
    assert fake.calls == [("p1", "/app"), ("p2", None)]
    await upstream.aclose()
