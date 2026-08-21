"""Route-level coalescing tests for the messages list endpoint (traffic plan
Batch 1 / Task 1.2, A2-C1..C6).

These lock the join-first ``LeasedSingleFlight`` integration of
``GET /slimapi/messages/{sid}``:

* A2-C1 — concurrent same-key callers share ONE upstream list GET
  (join-first, constructive — does not depend on completion grace);
* A2-C2 — distinct queries / sids / app instances never merge;
* A2-C3 — upstream errors propagate to every waiter (same mapped 503);
  leader cancellation re-leads for survivors; ``coalesce_enabled=false``
  bypasses the registry entirely (upstream calls == callers);
* A2-C4 — ledger assertions at the route level: budget-exhaustion bypass,
  ``leased_bytes`` bounded throughout + final zero, same-key callers share
  ONE reserve, network concurrency cap, validate() combined memory bound;
* A2-C5 — slow projection keeps "N callers, one GET" (join-first).

The module-level lifecycle/cancellation/ledger protocol (A2-C4 item 6, the
nine release-path classes, and the A2-C6 dual-waiter interleave) is covered
by ``tests/test_leased_singleflight.py`` — this file covers the ROUTE
integration only.

``mode=merged`` list pages go through the registry too; their phase-B full
fetches keep the process-level ``singleflight.fulls`` registry (verified
here by the full-GET dedup still holding).
"""
from __future__ import annotations

import asyncio

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.singleflight import LeasedSingleFlight
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}

MSG_PLAIN = {
    "info": {"id": "msg_2", "role": "user",
             "time": {"created": 2000, "updated": 2000}},
    "parts": [
        {"id": "p_plain", "type": "text", "messageID": "msg_2",
         "text": "plain"},
    ],
}

# A skeleton-collapsed message: the only part is an empty text part →
# ``thin_placeholder_msg_1`` marker in the projection (merged test).
MSG_PLACEHOLDER = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "p_empty", "type": "text", "messageID": "msg_1", "text": ""},
    ],
}

FULL_MSG_1 = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "part_text", "type": "text", "messageID": "msg_1",
         "text": "hello full"},
    ],
}

LIST_LINK = (
    '<http://127.0.0.1:4096/session/s1/message?before=CURSOR123&limit=40>; '
    'rel="next"'
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
        coalesce_enabled=True,
        raw_fetch_concurrency=4,
        # default test budget: 4 concurrent leased flights at the 64 KiB
        # reserve — generous for single-key tests, dialled down for the
        # budget-exhaustion test.
        raw_fetch_max_bytes=256 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    """Mirror ``oc_slimapi.app.lifespan`` (fresh app, no smoke probe) and
    attach the raw-fetch registry exactly when the real lifespan would
    (``coalesce_enabled=true``)."""
    app = FastAPI(title="oc-slimapi-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    if settings.coalesce_enabled:
        app.state.raw_fetch_registry = LeasedSingleFlight(
            max_bytes=settings.raw_fetch_max_bytes,
            network_concurrency=settings.raw_fetch_concurrency,
        )
    for router in (health.router, sessions.router, messages.router, events.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _teardown(app: FastAPI) -> None:
    registry = getattr(app.state, "raw_fetch_registry", None)
    if registry is not None:
        registry.shutdown()
    app.state.transforms.shutdown()


@pytest.fixture
async def upstream_factory():
    clients: list[httpx.AsyncClient] = []

    def _make(handler, *, base_url: str = "http://127.0.0.1:4096"):
        client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


async def _get_many(client: httpx.AsyncClient, paths: list[str]):
    async def _one(path: str):
        try:
            return await client.get(path, headers=HDR)
        except httpx.TransportError as exc:  # ASGITransport cancel artefacts
            return exc

    return await asyncio.gather(*(_one(p) for p in paths))


# ---------------------------------------------------------------------------
# A2-C1 — one GET per key, join-first.
# ---------------------------------------------------------------------------

async def test_same_key_concurrent_callers_single_get(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 20)
        assert calls["list"] == 1, "join-first must coalesce to ONE GET"
        bodies = {r.content for r in responses}
        assert len(responses) == 20
        assert all(r.status_code == 200 for r in responses)
        assert len(bodies) == 1  # byte-identical projections
    finally:
        _teardown(app)


async def test_cursor_header_shared_across_callers(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload, headers={
            "Content-Type": "application/json", "Link": LIST_LINK,
        })

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1?limit=40"] * 5)
        assert calls["list"] == 1
        cursors = {r.json()["nextCursor"] for r in responses}
        assert cursors == {"CURSOR123"}  # Link captured inside the factory
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A2-C2 — key discrimination.
# ---------------------------------------------------------------------------

async def test_distinct_queries_sids_not_merged(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path + "?" + str(request.url.query))
        return httpx.Response(200, content=payload,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    paths = [
        "/slimapi/messages/s1?limit=40",       # baseline
        "/slimapi/messages/s1?limit=10",       # different limit
        "/slimapi/messages/s1?mode=merged",    # different mode
        "/slimapi/messages/s2?limit=40",       # different sid
    ]
    try:
        async with _client(app) as client:
            responses = await _get_many(client, paths)
        assert all(r.status_code == 200 for r in responses)
        assert len(seen) == 4, f"each distinct key must GET once: {seen}"
    finally:
        _teardown(app)


async def test_directory_participates_in_key(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(client, [
                "/slimapi/messages/s1?directory=/a",
                "/slimapi/messages/s1?directory=/b",
            ])
        assert all(r.status_code == 200 for r in responses)
        assert calls["list"] == 2  # same resource, different directory
    finally:
        _teardown(app)


async def test_cross_app_instances_do_not_merge(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"a": 0, "b": 0}

    def make(counter):
        def handler(request: httpx.Request) -> httpx.Response:
            counter_key = "a" if counter is calls["a"] else "b"
            return httpx.Response(200, content=payload)
        return handler

    # simpler: two handlers each counting into its own dict slot
    def handler_a(request: httpx.Request) -> httpx.Response:
        calls["a"] += 1
        return httpx.Response(200, content=payload)

    def handler_b(request: httpx.Request) -> httpx.Response:
        calls["b"] += 1
        return httpx.Response(200, content=payload)

    del make

    upstream_a = upstream_factory(handler_a)
    upstream_b = upstream_factory(handler_b)
    app_a = _build_app(_settings(), upstream_a)
    app_b = _build_app(_settings(), upstream_b)
    try:
        async with _client(app_a) as client_a, _client(app_b) as client_b:
            await asyncio.gather(
                _get_many(client_a, ["/slimapi/messages/s1"] * 3),
                _get_many(client_b, ["/slimapi/messages/s1"] * 3),
            )
        # 1 GET per app instance — same (sid, query) never crosses apps
        assert calls["a"] == 1, calls
        assert calls["b"] == 1, calls
    finally:
        _teardown(app_a)
        _teardown(app_b)


# ---------------------------------------------------------------------------
# A2-C3 — error propagation, leader cancellation, bypass switch.
# ---------------------------------------------------------------------------

async def test_upstream_5xx_all_waiters_503_one_get(upstream_factory):
    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        # hold the (failing) response open briefly so concurrent callers
        # genuinely join the same in-flight GET before it fails
        await asyncio.sleep(0.05)
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 6)
        assert calls["list"] == 1, "one coalesced failing GET"
        assert all(r.status_code == 503 for r in responses)
        codes = {orjson.loads(r.content).get("code") for r in responses}
        assert codes == {"upstream_unavailable"}
    finally:
        _teardown(app)


async def test_leader_cancel_survivor_releads(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            leader = asyncio.create_task(client.get(
                "/slimapi/messages/s1", headers=HDR))
            await entered.wait()  # leader's factory is mid-GET
            survivor = asyncio.create_task(client.get(
                "/slimapi/messages/s1", headers=HDR))
            await asyncio.sleep(0.05)  # survivor joins the leader's flight
            leader.cancel()
            with pytest.raises((asyncio.CancelledError, httpx.TransportError)):
                await leader
            release.set()  # the (cancelled) shared GET finishes/fails
            response = await survivor
        assert response.status_code == 200
        assert orjson.loads(response.content)  # well-formed projection
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        release.set()
        _teardown(app)


async def test_coalesce_disabled_bypass(upstream_factory):
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(coalesce_enabled=False), upstream)
    assert getattr(app.state, "raw_fetch_registry", None) is None
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 3)
        assert calls["list"] == 3, "bypass: upstream calls == callers"
        assert all(r.status_code == 200 for r in responses)
    finally:
        _teardown(app)


async def test_missing_registry_state_degrades_to_direct(upstream_factory):
    """Legacy test-apps (no raw_fetch_registry on app.state) must keep the
    admission-first direct path."""
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    del app.state.raw_fetch_registry  # simulate pre-coalesce app
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 3)
        assert calls["list"] == 3
        assert all(r.status_code == 200 for r in responses)
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A2-C4 — route-level ledger assertions.
# ---------------------------------------------------------------------------

async def test_budget_exhaustion_bypass_direct(upstream_factory):
    """Capacity-1 budget: the first distinct key leases, the other three
    bypass direct — every caller still gets a correct response."""
    payload = orjson.dumps([MSG_PLAIN])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.query))
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    settings = _settings(raw_fetch_max_bytes=64 * 1024)  # exactly 1 reserve
    app = _build_app(settings, upstream)
    registry = app.state.raw_fetch_registry
    paths = [f"/slimapi/messages/s1?limit={n}" for n in (40, 10, 5, 1)]
    try:
        async with _client(app) as client:
            responses = await _get_many(client, paths)
        assert all(r.status_code == 200 for r in responses)
        assert len(calls) == 4, "excess keys degrade to direct GETs"
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_ledger_bounded_shared_reserve_slow_projection(
        upstream_factory, monkeypatch):
    """A2-C4 items 2+3 and A2-C5: under a deliberately slow projection, N
    same-key callers share ONE reserve (ledger peak == one
    ``max_response_bytes``), stay ``<= raw_fetch_max_bytes`` throughout, and
    the ledger returns to zero afterwards — while the upstream is hit once
    (join-first is constructive)."""
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    settings = _settings(max_transforms=1, transform_wait_seconds=5)
    app = _build_app(settings, upstream)
    registry = app.state.raw_fetch_registry

    # W3-2 (F-302): worker now resolves from the _list submodule namespace.
    real = messages._list._project_list_sorted_and_pack

    def slow(*args, **kwargs):
        import time
        time.sleep(0.08)  # admission queue forms behind the pool
        return real(*args, **kwargs)

    monkeypatch.setattr(messages._list, "_project_list_sorted_and_pack", slow)

    samples: list[int] = []
    stop = asyncio.Event()

    async def sampler():
        while not stop.is_set():
            samples.append(registry.leased_bytes)
            await asyncio.sleep(0.001)

    sampler_task = asyncio.create_task(sampler())
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 12)
        assert all(r.status_code == 200 for r in responses)
        assert calls["list"] == 1, "one GET even with slow projections"
    finally:
        stop.set()
        await sampler_task
        _teardown(app)

    assert samples, "sampler must have run"
    assert max(samples) <= settings.raw_fetch_max_bytes
    # same-key callers share ONE reserve: the peak IS the single reserve,
    # never a multiple of it
    assert max(samples) == settings.max_response_bytes
    assert registry.leased_bytes == 0  # every caller released


async def test_network_concurrency_cap(upstream_factory):
    """A2-C4 item 4: in-flight upstream GETs never exceed
    ``raw_fetch_concurrency`` regardless of caller count."""
    payload = orjson.dumps([MSG_PLAIN])
    in_flight = {"now": 0, "peak": 0}
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        entered.set()
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    settings = _settings(
        raw_fetch_concurrency=2,
        raw_fetch_max_bytes=6 * 64 * 1024,  # budget for all 6 keys
    )
    app = _build_app(settings, upstream)
    paths = [f"/slimapi/messages/s1?limit={n}" for n in (40, 10, 5, 1, 2, 3)]
    try:
        async with _client(app) as client:
            responses = await _get_many(client, paths)
        await entered.wait()
        assert all(r.status_code == 200 for r in responses)
        assert in_flight["peak"] <= 2, in_flight
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A2-C4 item 5 — validate() combined memory bound (route-facing config).
# ---------------------------------------------------------------------------

def test_validate_combined_memory_bound():
    # boundary: transform total == 512 MiB + default raw 64 MiB == 576 MiB
    _settings(
        max_transforms=2, max_response_bytes=256 * 1024 * 1024,
        raw_fetch_max_bytes=64 * 1024 * 1024,
    ).validate()

    with pytest.raises(RuntimeError):
        _settings(
            max_transforms=2, max_response_bytes=256 * 1024 * 1024,
            raw_fetch_max_bytes=128 * 1024 * 1024,  # 640 MiB > bound
        ).validate()


def test_validate_raw_fetch_knobs():
    with pytest.raises(RuntimeError):
        _settings(raw_fetch_concurrency=0).validate()
    with pytest.raises(RuntimeError):
        _settings(raw_fetch_max_bytes=0).validate()


# ---------------------------------------------------------------------------
# Merged mode — list page via registry, phase-B fulls untouched.
# ---------------------------------------------------------------------------

async def test_merged_list_via_registry_fulls_untouched(upstream_factory):
    list_payload = orjson.dumps([MSG_PLACEHOLDER, MSG_PLAIN])
    full_payload = orjson.dumps(FULL_MSG_1)
    calls = {"list": 0, "full": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/message/msg_1"):
            calls["full"] += 1
            return httpx.Response(200, content=full_payload)
        calls["list"] += 1
        return httpx.Response(200, content=list_payload)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1?mode=merged"] * 4)
        assert all(r.status_code == 200 for r in responses)
        assert calls["list"] == 1, "list page coalesced to ONE GET"
        assert calls["full"] == 1, "phase-B fulls dedup via singleflight.fulls"
        bodies = {r.content for r in responses}
        assert len(bodies) == 1  # identical merged projections
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# Projection admission semantics unchanged under the lease path.
# ---------------------------------------------------------------------------

async def test_projection_admission_busy_semantics_unchanged(
        upstream_factory, monkeypatch):
    """With a slow projection and a tight pool wait, callers either get the
    200 projection or the unchanged 503 ``transform_busy`` shape — and they
    still share ONE upstream GET (join-first)."""
    payload = orjson.dumps([MSG_PLAIN])
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=payload)

    upstream = upstream_factory(handler)
    settings = _settings(
        max_transforms=1, transform_wait_seconds=0.05,
    )
    app = _build_app(settings, upstream)
    registry = app.state.raw_fetch_registry

    # W3-2 (F-302): worker now resolves from the _list submodule namespace.
    real = messages._list._project_list_sorted_and_pack

    def slow(*args, **kwargs):
        import time
        time.sleep(0.15)
        return real(*args, **kwargs)

    monkeypatch.setattr(messages._list, "_project_list_sorted_and_pack", slow)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/messages/s1"] * 4)
        assert calls["list"] == 1
        outcomes = set()
        for r in responses:
            if r.status_code == 200:
                outcomes.add("ok")
            else:
                assert r.status_code == 503, r.status_code
                assert orjson.loads(r.content)["code"] == "transform_busy"
                outcomes.add("busy")
        assert outcomes <= {"ok", "busy"}
        assert "ok" in outcomes
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)
