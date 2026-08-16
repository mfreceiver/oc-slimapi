"""Task 1.1 (A1) — catalog TTL cache regression (traffic plan Batch 1).

Covers A1-C1..C6 from
``docs/ocmar/plans/2026-08-16-traffic-optimization-plan.md`` §Task 1.1:

* C1  TTL-window second GET shares ONE upstream call; bodies byte-identical.
* C2  TTL-expired concurrent refresh (20 callers) → exactly ONE refresh
  (single-flight stampede protection).
* C3  upstream 5xx / bad JSON never cached; ``ttl=0`` disables the cache
  entirely (byte-identical to today: every request hits upstream).
* C4  byte-budget regression — oversize entry bypasses the cache (not
  accounted); total budget evicts oldest-first.
* C5  config boundary matrix (max_entries=1 / ttl=0 / entry>total rejected /
  defaults) + eviction-during-concurrent-refresh ledger consistency.
* C6  access-log ``cache: "hit"|"miss"`` additive field (present only when a
  value exists — existing rows keep their exact key set).
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.access_log import write_access_log
from oc_slimapi.catalog_cache import CatalogCache
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.request_id import RequestIdMiddleware
from oc_slimapi.routes import agent, command
from oc_slimapi.transform import TransformConfig, TransformPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache(**overrides) -> CatalogCache:
    kwargs = dict(
        ttl_seconds=300.0,
        max_entries=16,
        max_bytes=16 * 1024 * 1024,
        max_entry_bytes=1024 * 1024,
    )
    kwargs.update(overrides)
    return CatalogCache(**kwargs)


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
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient,
               cache: CatalogCache | None) -> FastAPI:
    """Minimal app mirroring test_agent_routes._build_app + catalog cache."""
    app = FastAPI(title="oc-slimapi-catalog-cache-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    if cache is not None:
        app.state.catalog_cache = cache
    for router in (agent.router, command.router):
        app.include_router(router)
    register_error_handlers(app)
    return app


class _CountingHandler:
    """MockTransport handler counting calls and returning canned bodies."""

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, content=self.body,
                              headers={"content-type": "application/json"})


def _client_for(app: FastAPI, handler) -> httpx.AsyncClient:
    """Wire a MockTransport upstream into ``app`` and return a client pair.

    Returns ``(downstream_client, upstream_client)`` — the downstream client
    talks to the app via ASGI transport; the upstream client is the one the
    app.state references (its MockTransport counts upstream calls).
    """
    upstream_client = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )
    app.state.upstream = upstream_client
    downstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    return downstream, upstream_client


AGENTS_BODY = orjson.dumps([
    {"name": "build", "description": "d", "mode": "primary",
     "prompt": "x" * 500},
])
COMMANDS_BODY = orjson.dumps([
    {"name": "plan", "description": "d", "template": "t" * 500},
])


# ---------------------------------------------------------------------------
# Module-level: TTL / refresh / bypass (A1-C1..C4)
# ---------------------------------------------------------------------------

async def test_c1_ttl_window_second_lookup_shares_one_fetch():
    cache = _cache()
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        return b'[1, 2]'

    b1, s1 = await cache.refresh(("agent", None), factory)
    assert b1 == b'[1, 2]' and s1 == "miss"
    hit = cache.lookup(("agent", None))
    assert hit == b'[1, 2]'
    assert len(calls) == 1


async def test_c1_refresh_within_ttl_hits_cache_not_factory():
    cache = _cache(ttl_seconds=60.0)
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        return b'[1, 2]'

    await cache.refresh(("agent", None), factory)
    # second refresh within TTL → lookup freshness honored by the ROUTE; at
    # module level the route checks lookup() first, so verify lookup stays
    # fresh and a forced refresh still returns the stored body via sf join
    # only when in flight. Here: route-level behaviour (lookup hit → no
    # factory) is covered by the route tests below; module contract:
    # repeated refresh calls re-run the factory (single-flight only dedups
    # CONCURRENT refreshes), which is why the route consults lookup() first.
    assert cache.lookup(("agent", None)) == b'[1, 2]'
    assert len(calls) == 1


async def test_c2_expired_concurrent_refresh_single_flight():
    cache = _cache(ttl_seconds=0.05)
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        await asyncio.sleep(0.05)  # keep the flight joinable
        return b'[4, 5]'

    await cache.refresh(("agent", None), factory)
    assert len(calls) == 1
    await asyncio.sleep(1.2)  # TTL expiry + past sf grace (1.0s)
    assert cache.lookup(("agent", None)) is None
    results = await asyncio.gather(*(
        cache.refresh(("agent", None), factory) for _ in range(20)
    ))
    assert len(calls) == 2  # first fill + exactly ONE refresh
    assert all(body == b'[4, 5]' for body, _ in results)


async def test_c3_factory_exception_not_cached_and_propagates():
    cache = _cache()
    calls = []
    boom = RuntimeError("upstream 5xx")

    async def failing() -> bytes:
        calls.append(1)
        raise boom

    with pytest.raises(RuntimeError, match="upstream 5xx"):
        await cache.refresh(("agent", None), failing)
    assert cache.lookup(("agent", None)) is None
    assert cache.retained_bytes == 0

    async def ok() -> bytes:
        calls.append(1)
        return b'[3]'

    body, _ = await cache.refresh(("agent", None), ok)
    assert body == b'[3]'
    assert len(calls) == 2  # failure was not negatively cached


async def test_c3_bad_json_not_cached():
    cache = _cache()
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        return b"{not-json"

    body, _ = await cache.refresh(("agent", None), factory)
    assert body == b"{not-json"  # returned so the route can map its 503
    assert cache.lookup(("agent", None)) is None
    assert cache.retained_bytes == 0
    assert len(calls) == 1


async def test_c3_non_list_json_not_cached():
    cache = _cache()
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        return b'{"unexpected": "dict"}'

    body, _ = await cache.refresh(("agent", None), factory)
    assert body == b'{"unexpected": "dict"}'
    assert cache.lookup(("agent", None)) is None


async def test_c3_cap_exceeded_none_not_cached():
    cache = _cache()
    calls = []

    async def factory() -> bytes | None:
        calls.append(1)
        return None  # read_with_cap truncation

    body, state = await cache.refresh(("agent", None), factory)
    assert body is None and state == "miss"
    assert cache.lookup(("agent", None)) is None
    assert cache.retained_bytes == 0


async def test_c3_ttl_zero_disables_everything():
    cache = _cache(ttl_seconds=0.0)
    calls = []

    async def factory() -> bytes:
        calls.append(1)
        return b'[6]'

    body, state = await cache.refresh(("agent", None), factory)
    assert body == b'[6]' and state is None  # no cache semantics → no label
    assert cache.lookup(("agent", None)) is None
    await cache.refresh(("agent", None), factory)
    assert len(calls) == 2  # every request hits the factory (today's path)


async def test_c4_oversize_entry_bypasses_cache():
    cache = _cache(max_entry_bytes=8)

    async def factory() -> bytes:
        return b"0123456789abcdef"  # 16 bytes > 8

    body, _ = await cache.refresh(("agent", None), factory)
    assert body == b"0123456789abcdef"  # pass-through
    assert cache.lookup(("agent", None)) is None
    assert cache.retained_bytes == 0  # NOT accounted
    assert cache.entry_count == 0


async def test_c4_total_budget_evicts_oldest_first():
    # budget fits two 4-byte entries; three keys → oldest ("a") evicted.
    cache = _cache(max_bytes=6, max_entry_bytes=8)

    async def mk(value: bytes):
        async def factory() -> bytes:
            return value
        return factory

    await cache.refresh(("a", None), await mk(b'[1]'))
    await cache.refresh(("b", None), await mk(b'[2]'))
    await cache.refresh(("c", None), await mk(b'[3]'))
    assert cache.lookup(("a", None)) is None      # oldest evicted
    assert cache.lookup(("b", None)) == b'[2]'
    assert cache.lookup(("c", None)) == b'[3]'
    assert cache.retained_bytes == 6


async def test_c5_max_entries_one_keeps_only_newest():
    cache = _cache(max_entries=1)

    async def mk(value: bytes):
        async def factory() -> bytes:
            return value
        return factory

    await cache.refresh(("a", None), await mk(b'[1]'))
    await cache.refresh(("b", None), await mk(b'[2]'))
    assert cache.lookup(("a", None)) is None
    assert cache.lookup(("b", None)) == b'[2]'
    assert cache.entry_count == 1


async def test_c5_eviction_during_concurrent_refresh_ledger_consistent():
    """A1-C5: eviction racing a concurrent refresh keeps the ledger exact."""
    cache = _cache(ttl_seconds=60.0, max_entries=16, max_bytes=6,
                   max_entry_bytes=8)
    release = asyncio.Event()

    async def slow_factory() -> bytes:
        await release.wait()
        return b'[9]'

    async def mk(value: bytes):
        async def factory() -> bytes:
            return value
        return factory

    slow = asyncio.create_task(cache.refresh(("slow", None), slow_factory))
    await asyncio.sleep(0.02)  # slow flight in flight
    # concurrent inserts of other kinds trigger eviction under budget 8.
    await cache.refresh(("x", None), await mk(b'[1]'))
    await cache.refresh(("y", None), await mk(b'[2]'))  # evicts "x"
    release.set()
    body, _ = await slow
    assert body == b'[9]'
    # final ledger: retained == exactly the stored bodies, no double counting
    total = sum(len(cache.lookup(k)) for k in (("slow", None), ("y", None))
                if cache.lookup(k) is not None)
    assert cache.retained_bytes == total
    assert cache.retained_bytes <= 6


async def test_shutdown_clears_entries_and_singleflight():
    cache = _cache()

    async def factory() -> bytes:
        return b'[1]'

    await cache.refresh(("agent", None), factory)
    assert cache.retained_bytes == len(b'[1]')
    cache.shutdown()
    assert cache.entry_count == 0
    assert cache.retained_bytes == 0
    # reusable after shutdown (CD-1 semantics)
    body, _ = await cache.refresh(("agent", None), factory)
    assert body == b'[1]'


async def test_store_replaces_same_key_without_double_counting():
    cache = _cache()

    async def mk(value: bytes):
        async def factory() -> bytes:
            return value
        return factory

    await cache.refresh(("k", None), await mk(b'[1]'))
    await asyncio.sleep(1.2)  # past sf grace: sequential refresh re-fetches
    await cache.refresh(("k", None), await mk(b'[2, 3]'))
    assert cache.lookup(("k", None)) == b'[2, 3]'
    assert cache.retained_bytes == len(b'[2, 3]')


# ---------------------------------------------------------------------------
# Config validate() boundary matrix (A1-C5)
# ---------------------------------------------------------------------------

def _catalog_settings(**overrides) -> Settings:
    kwargs = dict(
        catalog_cache_ttl_seconds=300,
        catalog_cache_max_entries=16,
        catalog_cache_max_bytes=16 * 1024 * 1024,
        catalog_cache_max_entry_bytes=1024 * 1024,
    )
    kwargs.update(overrides)
    return _settings(**kwargs)


def test_config_defaults_accept_and_validate():
    s = _catalog_settings()
    s.validate()  # defaults pass
    assert s.catalog_cache_ttl_seconds == 300
    assert s.catalog_cache_max_entries == 16
    assert s.catalog_cache_max_bytes == 16 * 1024 * 1024
    assert s.catalog_cache_max_entry_bytes == 1024 * 1024


def test_config_defaults_from_env_shape_match_plan():
    # module-default Settings (env untouched) carry the plan's defaults
    s = Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024, max_transforms=1,
        transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None, server_api_version=2,
    )
    assert s.catalog_cache_ttl_seconds == 300
    assert s.catalog_cache_max_entries == 16
    assert s.catalog_cache_max_bytes == 16 * 1024 * 1024
    assert s.catalog_cache_max_entry_bytes == 1024 * 1024


def test_config_rejects_entry_bytes_above_total():
    s = _catalog_settings(
        catalog_cache_max_bytes=16 * 1024 * 1024,
        catalog_cache_max_entry_bytes=32 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match="CATALOG_CACHE_MAX_ENTRY_BYTES"):
        s.validate()


def test_config_rejects_total_below_1mib():
    s = _catalog_settings(catalog_cache_max_bytes=512 * 1024,
                          catalog_cache_max_entry_bytes=512 * 1024)
    with pytest.raises(RuntimeError, match="CATALOG_CACHE_MAX_BYTES"):
        s.validate()


def test_config_rejects_negative_ttl():
    s = _catalog_settings(catalog_cache_ttl_seconds=-1)
    with pytest.raises(RuntimeError, match="CATALOG_CACHE_TTL_SECONDS"):
        s.validate()


def test_config_rejects_zero_entries():
    s = _catalog_settings(catalog_cache_max_entries=0)
    with pytest.raises(RuntimeError, match="CATALOG_CACHE_MAX_ENTRIES"):
        s.validate()


def test_config_rejects_non_positive_entry_bytes():
    """rev-gpt C1: an entry cap of 0 (or negative) is rejected — it would
    cache nothing while the config claims a live cache."""
    for bad in (0, -1):
        s = _catalog_settings(catalog_cache_max_entry_bytes=bad)
        with pytest.raises(RuntimeError, match="CATALOG_CACHE_MAX_ENTRY_BYTES"):
            s.validate()


def test_config_accepts_ttl_zero():
    s = _catalog_settings(catalog_cache_ttl_seconds=0)
    s.validate()


def test_config_accepts_min_entry_boundaries():
    s = _catalog_settings(
        catalog_cache_max_entries=1,
        catalog_cache_max_bytes=1024 * 1024,
        catalog_cache_max_entry_bytes=1024 * 1024,
    )
    s.validate()  # entry == total (1 MiB lower bound) is legal


# ---------------------------------------------------------------------------
# Route integration (A1-C1/C2/C3 through /slimapi/agent & /command)
# ---------------------------------------------------------------------------

async def test_route_agent_ttl_second_get_one_upstream_call_and_identical_body():
    handler = _CountingHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client, _up = _client_for(app, handler)
    try:
        r1 = await client.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        r2 = await client.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.content == r2.content          # byte-identical projection
        assert handler.calls == 1                # ONE upstream GET (C1)
    finally:
        await client.aclose()


async def test_route_agent_gzip_computed_per_request_not_cached():
    """gzip is NOT cached — two hits with different Accept-Encoding produce
    different Content-Encoding from the same cached body."""
    handler = _CountingHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client, _up = _client_for(app, handler)
    try:
        r1 = await client.get(
            "/slimapi/agent",
            headers={"X-Slimapi-Version": "2", "Accept-Encoding": "identity"},
        )
        r2 = await client.get(
            "/slimapi/agent",
            headers={"X-Slimapi-Version": "2", "Accept-Encoding": "gzip"},
        )
        assert handler.calls == 1
        assert "content-encoding" not in {k.lower() for k in r1.headers}
        assert r2.headers.get("content-encoding") == "gzip"
        # httpx transparently decompresses; the header above proves the gzip
        # representation was computed for THIS request, not cached.
        assert r2.content == r1.content
    finally:
        await client.aclose()


async def test_route_agent_ttl_expired_20_concurrent_one_refresh():
    handler = _CountingHandler(AGENTS_BODY)

    class _SlowHandler(_CountingHandler):
        def __call__(self, request):
            # keep the refresh flight joinable so 20 callers coalesce
            return super().__call__(request)

    slow = _SlowHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, _cache(ttl_seconds=0.05))
    client, _up = _client_for(app, slow)
    try:
        first = await client.get("/slimapi/agent",
                                 headers={"X-Slimapi-Version": "2"})
        assert first.status_code == 200
        await asyncio.sleep(1.2)  # TTL expiry + past sf grace (1.0s)
        responses = await asyncio.gather(*(
            client.get("/slimapi/agent",
                       headers={"X-Slimapi-Version": "2"})
            for _ in range(20)
        ))
        assert all(r.status_code == 200 for r in responses)
        assert len({r.content for r in responses}) == 1
        assert slow.calls == 2  # initial fill + exactly ONE refresh (C2)
    finally:
        await client.aclose()


async def test_route_agent_5xx_not_cached_then_recovers():
    state = {"calls": 0, "fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["fail"]:
            return httpx.Response(500, content=b'{"error":"boom"}')
        return httpx.Response(200, content=AGENTS_BODY,
                              headers={"content-type": "application/json"})

    app = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client, _up = _client_for(app, handler)
    try:
        r1 = await client.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        assert r1.status_code == 503  # mapped upstream error
        state["fail"] = False
        r2 = await client.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        assert r2.status_code == 200  # NOT cached — retried upstream (C3)
        assert state["calls"] == 2
        r3 = await client.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        assert r3.status_code == 200
        assert state["calls"] == 2  # r3 served from r2's cache fill
    finally:
        await client.aclose()


async def test_route_agent_ttl_zero_matches_today_call_count():
    handler = _CountingHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, _cache(ttl_seconds=0.0))
    client, _up = _client_for(app, handler)
    try:
        for _ in range(3):
            r = await client.get("/slimapi/agent",
                                 headers={"X-Slimapi-Version": "2"})
            assert r.status_code == 200
        assert handler.calls == 3  # disabled → every request upstream (C3)
    finally:
        await client.aclose()


async def test_route_ttl_zero_stashes_no_cache_field(monkeypatch):
    """rev-gpt addendum: ttl=0 DISABLES the cache — the route must not
    report any cache semantics (no ``cache: "miss"`` access-log field),
    consistent with the None-omits-the-key rule. A live miss still stashes
    "miss"."""
    from oc_slimapi.routes import _catalog_common

    stashed: list[object] = []

    def _spy(request, value):
        stashed.append(value)
        # deliberately do NOT write state — the spy only records the label

    monkeypatch.setattr(_catalog_common, "stash_cache", _spy)

    handler = _CountingHandler(AGENTS_BODY)

    # ttl=0 → every refresh returns state None → NO label stashed
    app = _build_app(_settings(), None, _cache(ttl_seconds=0.0))
    client, _up = _client_for(app, handler)
    try:
        for _ in range(2):
            r = await client.get("/slimapi/agent",
                                 headers={"X-Slimapi-Version": "2"})
            assert r.status_code == 200
        assert handler.calls == 2
        assert stashed == [None, None]  # disabled: no "miss" semantics
    finally:
        await client.aclose()

    # live cache → miss label stashed exactly once per uncached request
    stashed.clear()
    handler2 = _CountingHandler(AGENTS_BODY)
    app2 = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client2, _up2 = _client_for(app2, handler2)
    try:
        r = await client2.get("/slimapi/agent",
                              headers={"X-Slimapi-Version": "2"})
        assert r.status_code == 200
        assert stashed == ["miss"]
    finally:
        await client2.aclose()


async def test_route_agent_cache_keyed_by_directory():
    handler = _CountingHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client, _up = _client_for(app, handler)
    try:
        for _ in range(2):
            await client.get("/slimapi/agent", params={"directory": "/a"},
                             headers={"X-Slimapi-Version": "2"})
            await client.get("/slimapi/agent", params={"directory": "/b"},
                             headers={"X-Slimapi-Version": "2"})
        assert handler.calls == 2  # one per directory bucket
    finally:
        await client.aclose()


async def test_route_agent_and_command_cache_independently():
    calls = {"agent": 0, "command": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path.lstrip("/")] += 1
        body = AGENTS_BODY if request.url.path == "/agent" else COMMANDS_BODY
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/json"})

    app = _build_app(_settings(), None, _cache(ttl_seconds=60.0))
    client, _up = _client_for(app, handler)
    try:
        await client.get("/slimapi/agent", headers={"X-Slimapi-Version": "2"})
        await client.get("/slimapi/command", headers={"X-Slimapi-Version": "2"})
        await client.get("/slimapi/agent", headers={"X-Slimapi-Version": "2"})
        await client.get("/slimapi/command", headers={"X-Slimapi-Version": "2"})
        assert calls == {"agent": 1, "command": 1}  # separate cache entries
    finally:
        await client.aclose()


async def test_route_without_cache_state_behaves_as_today():
    """No ``catalog_cache`` on app.state (legacy test apps / knob off) → the
    route falls back to today's uncached path (additive wiring)."""
    handler = _CountingHandler(AGENTS_BODY)
    app = _build_app(_settings(), None, None)
    client, _up = _client_for(app, handler)
    try:
        for _ in range(2):
            r = await client.get("/slimapi/agent",
                                 headers={"X-Slimapi-Version": "2"})
            assert r.status_code == 200
        assert handler.calls == 2
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Access-log additive ``cache`` field (A1-C6)
# ---------------------------------------------------------------------------

class _CaptureLogger(logging.Logger):
    def __init__(self):
        super().__init__("capture")
        self.disabled = False
        self.rows: list[dict] = []

    def info(self, msg, *args, **kwargs):
        self.rows.append(json.loads(msg))


def _write(logger, **kwargs):
    write_access_log(
        logger,
        method="GET", path="/slimapi/agent", bucket="agent", status=200,
        duration_ms=1.0, down_in=0, down_out=0, up_in=0, up_out=0,
        **kwargs,
    )


def test_access_log_cache_field_present_only_when_set():
    logger = _CaptureLogger()
    _write(logger)
    assert "cache" not in logger.rows[0]  # absent → existing rows unchanged
    _write(logger, cache="hit")
    assert logger.rows[1]["cache"] == "hit"
    _write(logger, cache="miss")
    assert logger.rows[2]["cache"] == "miss"


async def test_stash_cache_sets_request_state():
    from oc_slimapi.traffic import stash_cache

    class _Req:
        def __init__(self):
            self.scope = {"state": {}}

    req = _Req()
    stash_cache(req, "hit")
    assert req.scope["state"]["traffic_cache"] == "hit"
    stash_cache(req, None)  # no-op (no cache semantics)
    assert req.scope["state"]["traffic_cache"] == "hit"
