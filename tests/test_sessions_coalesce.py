"""Route-level coalescing tests for the sessions endpoints (traffic plan
Batch 1 / Task 1.3, A3-C1..C3).

* ``GET /slimapi/sessions`` — the upstream list GET + cap-read is shared
  join-first through ``app.state.raw_fetch_registry`` (key embeds upstream
  identity, directory and the canonical query); the skeleton projection and
  the ``X-Complete`` computation stay per-caller. No TTL: freshness matches
  the direct path (all callers within one poll interval join the same
  in-flight fetch; a new poll fetches anew).
* ``GET /slimapi/sessions/status`` — the upstream ``GET /session/status``
  body is shared (key embeds upstream identity + directory); the
  TurnRegistry merge runs per-caller AFTER the shared body, so a caller
  joining a grace-retained body still observes the CURRENT turn state
  (A3-C2's freshness lock — the merge is never frozen into the shared
  body).
"""
from __future__ import annotations

import asyncio

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.leased_singleflight import LeasedSingleFlight
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}


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
        raw_fetch_max_bytes=256 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
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
        return await client.get(path, headers=HDR)

    return await asyncio.gather(*(_one(p) for p in paths))


class _FakeTurnRegistry:
    """Minimal mutable stand-in for TurnRegistry: ``snapshot(sid)`` returns
    the current (incarnation, turn) cell so tests can advance turn state
    between callers."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[int, int]] = {}

    def snapshot(self, sid: str) -> tuple[int, int]:
        return self.values.get(sid, (0, 0))


SESSIONS_PAYLOAD = orjson.dumps([
    {"id": f"s{n}", "title": f"session {n}",
     "time": {"created": 1000 + n, "updated": 1000 + n}}
    for n in range(3)
])

STATUS_PAYLOAD = orjson.dumps({"s1": {"type": "idle"}})


# ---------------------------------------------------------------------------
# A3-C1 — sessions list coalescing.
# ---------------------------------------------------------------------------

async def test_sessions_list_one_get_burst(upstream_factory):
    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        await asyncio.sleep(0.05)  # ensure all 20 join the same flight
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/sessions?limit=100"] * 20)
        assert calls["list"] == 1, "burst coalesces to ONE upstream GET"
        assert all(r.status_code == 200 for r in responses)
        # per-caller projection: identical bodies, per-caller complete
        bodies = {r.content for r in responses}
        assert len(bodies) == 1
        assert {r.json()["complete"] for r in responses} == {True}
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_sessions_list_x_complete_false_per_caller(upstream_factory):
    """Projection stays per-caller: the SAME shared body projects
    ``X-Complete`` according to each caller's own ``limit`` — here a tight
    limit equal to the payload length flips it to false."""
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            big, tight = await asyncio.gather(
                client.get("/slimapi/sessions?limit=100", headers=HDR),
                client.get("/slimapi/sessions?limit=3", headers=HDR),
            )
        # limit=3 vs limit=100 are DIFFERENT keys → 2 GETs (not shared)
        assert calls["list"] == 2
        assert big.json()["complete"] is True
        assert tight.json()["complete"] is False
    finally:
        _teardown(app)


async def test_sessions_list_distinct_query_directory(upstream_factory):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.query))
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(client, [
                "/slimapi/sessions",
                "/slimapi/sessions?search=x",
                "/slimapi/sessions?directory=/a",
                "/slimapi/sessions?directory=/b",
            ])
        assert all(r.status_code == 200 for r in responses)
        assert len(calls) == 4, calls
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A3-C2 — sessions/status shared body + per-caller turn merge.
# ---------------------------------------------------------------------------

async def test_status_body_shared_turn_merge_per_caller(upstream_factory):
    """The upstream body is fetched once; the turn merge runs per-caller at
    read time. Turn state advancing MID-flight must be visible to every
    caller (the merge is not frozen into the shared body)."""
    calls = {"status": 0}
    turns = _FakeTurnRegistry()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["status"] += 1
        await asyncio.sleep(0.05)  # window for the turn state to advance
        return httpx.Response(200, content=STATUS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    app.state.turn_registry = turns

    async def _advance_turns():
        await asyncio.sleep(0.02)  # while the shared GET is still in flight
        turns.values["s1"] = (7, 3)

    try:
        async with _client(app) as client:
            responses, _ = await asyncio.gather(
                _get_many(client, ["/slimapi/sessions/status"] * 6),
                _advance_turns(),
            )
        assert calls["status"] == 1
        assert all(r.status_code == 200 for r in responses)
        for r in responses:
            payload = orjson.loads(r.content)
            assert payload["s1"]["turnIncarnation"] == 7
            assert payload["s1"]["turn"] == 3
    finally:
        _teardown(app)


async def test_status_grace_joiner_sees_fresh_turns(upstream_factory):
    """A3-C2 freshness lock: after the leader completes, turn state changes,
    and a caller joining the grace-retained body STILL sees the NEW turn
    values (per-caller merge) while the upstream is not hit again."""
    calls = {"status": 0}
    turns = _FakeTurnRegistry()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["status"] += 1
        return httpx.Response(200, content=STATUS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    app.state.turn_registry = turns
    try:
        async with _client(app) as client:
            turns.values["s1"] = (1, 0)
            first = await client.get("/slimapi/sessions/status", headers=HDR)
            assert first.status_code == 200
            assert orjson.loads(first.content)["s1"]["turn"] == 0

            # turn state advances AFTER the leader completed...
            turns.values["s1"] = (2, 1)
            # ...and the next caller joins the grace-retained body (< 1s)
            second = await client.get(
                "/slimapi/sessions/status", headers=HDR)
        assert calls["status"] == 1, "grace join — no second upstream GET"
        entry = orjson.loads(second.content)["s1"]
        assert entry["turnIncarnation"] == 2 and entry["turn"] == 1, (
            "the turn merge must read the registry per-caller, not the "
            "frozen shared body"
        )
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A3-C3 — isolation, error propagation, bypass switch.
# ---------------------------------------------------------------------------

async def test_status_cross_app_not_merged(upstream_factory):
    calls = {"a": 0, "b": 0}

    def handler_a(request: httpx.Request) -> httpx.Response:
        calls["a"] += 1
        return httpx.Response(200, content=STATUS_PAYLOAD)

    def handler_b(request: httpx.Request) -> httpx.Response:
        calls["b"] += 1
        return httpx.Response(200, content=STATUS_PAYLOAD)

    upstream_a = upstream_factory(handler_a)
    upstream_b = upstream_factory(handler_b)
    app_a = _build_app(_settings(), upstream_a)
    app_b = _build_app(_settings(), upstream_b)
    try:
        async with _client(app_a) as client_a, _client(app_b) as client_b:
            await asyncio.gather(
                _get_many(client_a, ["/slimapi/sessions"] * 3),
                _get_many(client_b, ["/slimapi/sessions"] * 3),
            )
        assert calls == {"a": 1, "b": 1}
    finally:
        _teardown(app_a)
        _teardown(app_b)


async def test_list_upstream_5xx_all_waiters_503(upstream_factory):
    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/sessions"] * 6)
        assert calls["list"] == 1
        assert all(r.status_code == 503 for r in responses)
        codes = {orjson.loads(r.content).get("code") for r in responses}
        assert codes == {"upstream_unavailable"}
    finally:
        _teardown(app)


async def test_coalesce_disabled_bypass(upstream_factory):
    list_calls = {"list": 0}
    status_calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            status_calls["status"] += 1
            return httpx.Response(200, content=STATUS_PAYLOAD)
        list_calls["list"] += 1
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(coalesce_enabled=False), upstream)
    assert getattr(app.state, "raw_fetch_registry", None) is None
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/sessions", "/slimapi/sessions/status"] * 3)
        assert all(r.status_code == 200 for r in responses)
        assert list_calls["list"] == 3, "bypass: list GETs == callers"
        assert status_calls["status"] == 3, "bypass: status GETs == callers"
    finally:
        _teardown(app)


async def test_status_upstream_5xx_all_waiters_503(upstream_factory):
    calls = {"status": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["status"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/sessions/status"] * 4)
        assert calls["status"] == 1
        assert all(r.status_code == 503 for r in responses)
        codes = {orjson.loads(r.content).get("code") for r in responses}
        assert codes == {"upstream_unavailable"}
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# rev-gpt B1 — lease path keeps the direct path's admission semantics.
# ---------------------------------------------------------------------------

async def test_list_projection_admission_busy_semantics_unchanged(
        upstream_factory, monkeypatch):
    """rev-gpt B1: the lease path must NOT bypass TransformPool admission.
    With a slow projection and a tight pool wait (max_transforms=1,
    transform_wait_seconds=0.05) coalesced callers get either the 200
    projection or the direct path's byte-identical 503 ``transform_busy``
    shape — while still sharing ONE upstream list GET. Busy callers release
    their lease refs (no shared-body budget leak)."""
    import time

    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        await asyncio.sleep(0.05)  # all four join the SAME in-flight fetch
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(max_transforms=1, transform_wait_seconds=0.05), upstream)
    registry = app.state.raw_fetch_registry

    real_project = sessions._project_sessions

    def slow_project(payload):
        time.sleep(0.15)  # hold the single transform slot past the 0.05s wait
        return real_project(payload)

    monkeypatch.setattr(sessions, "_project_sessions", slow_project)
    try:
        async with _client(app) as client:
            responses = await _get_many(client, ["/slimapi/sessions"] * 4)
        # join-first sharing is unaffected by the busy discipline
        assert calls["list"] == 1
        outcomes = set()
        for r in responses:
            if r.status_code == 200:
                outcomes.add("ok")
            else:
                # byte-identical busy shape vs the direct path
                assert r.status_code == 503, r.status_code
                body = orjson.loads(r.content)
                assert body["code"] == "transform_busy"
                assert r.headers.get("retry-after") == "2"
                outcomes.add("busy")
        # WITHOUT admission (the B1 defect) every caller queues unbounded
        # and outcomes == {"ok"}; WITH it, the slot holder projects while
        # the other three hit the direct path's busy 503.
        assert outcomes == {"ok", "busy"}
        # every caller released its lease ref — no shared-body budget leak
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_list_json_parse_inside_admission(upstream_factory, monkeypatch):
    """rev-gpt B1-residual: the lease path must expand the shared body only
    AFTER acquiring the transform slot — joiners queued on admission hold
    the raw shared bytes only, never a per-caller JSON object graph (plan
    :110,179; mirrors the direct path's fetch→parse→project-under-admission
    and messages.py:710-723)."""
    import time

    calls = {"list": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["list"] += 1
        await asyncio.sleep(0.05)  # all four join the SAME in-flight fetch
        return httpx.Response(200, content=SESSIONS_PAYLOAD)

    upstream = upstream_factory(handler)
    # defaults: max_transforms=1, transform_wait_seconds=0.5 → three of the
    # four callers QUEUE on admission while the slot holder projects.
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    real_pool = app.state.transforms
    real_orjson = orjson

    events: list[str] = []

    class _AdmissionSpy:
        async def __aenter__(self):
            events.append("acquire")
            return await real_pool.__aenter__()

        async def __aexit__(self, *exc):
            events.append("release")
            return await real_pool.__aexit__(*exc)

        def offload(self, *args, **kwargs):
            return real_pool.offload(*args, **kwargs)

        def shutdown(self):
            return real_pool.shutdown()

    app.state.transforms = _AdmissionSpy()

    class _OrjsonSpy:
        def loads(self, data):
            events.append("loads")
            return real_orjson.loads(data)

        def __getattr__(self, name):
            return getattr(real_orjson, name)

    monkeypatch.setattr(sessions, "orjson", _OrjsonSpy())

    real_project = sessions._project_sessions

    def slow_project(payload):
        time.sleep(0.08)  # hold the single slot so the others queue
        return real_project(payload)

    monkeypatch.setattr(sessions, "_project_sessions", slow_project)
    try:
        async with _client(app) as client:
            responses = await _get_many(client, ["/slimapi/sessions"] * 4)
        assert calls["list"] == 1
        assert all(r.status_code == 200 for r in responses)
        # Ordering lock: every ``orjson.loads`` happens only after its
        # caller holds an admission slot — at any prefix of the event
        # stream the number of parses never exceeds the number of
        # acquires. Pre-fix the FIRST caller parses before ANY acquire
        # (parse lived ahead of the pool section) and this fails.
        acquires = 0
        for ev in events:
            if ev == "acquire":
                acquires += 1
            elif ev == "loads":
                assert acquires >= 1, (
                    "JSON parsed before transform admission — queued "
                    "joiners would hold expanded object graphs"
                )
        assert events.count("loads") == 4  # one parse per caller, in-slot
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)
