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

from conftest import current_replay_log
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.singleflight import LeasedSingleFlight
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, sessions
from oc_slimapi.selector import SlimapiSelectorMiddleware
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
    app.state.hubs = HubRegistry(
        upstream, replay_log=current_replay_log())
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
    {"id": f"s{n}", "title": f"session {n}", "directory": "w",
     "time": {"created": 1000 + n, "updated": 1000 + n}}
    for n in range(3)
])
# (V2b note: ``directory`` added so the items stay representable under the
# v4 canonical projector — the sessions list now runs the v4 facade on
# every scope and an item without a directory is fail-closed 503, §13.2a.)

STATUS_PAYLOAD = orjson.dumps({"s1": {"type": "idle"}})


# ---------------------------------------------------------------------------
# A3-C1 — sessions list coalescing.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# A3-C1 — sessions list coalescing. (REMOVED with the V2b src teardown: the
# sessions list runs the v4 facade on every scope under the v4-only (4, 4)
# window and the facade has no lease/coalesce path — the three A3-C1 list
# tests asserted the retired join-first flight. The v4 list behavior is
# locked by tests/test_sessions_v4_matrix.py; the registry itself stays
# alive for sessions/STATUS below.)
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


async def test_status_lease_directory_header_only_upstream(upstream_factory):
    """M3-2 (terminal §5.2): the coalesced status lease path must be
    header-only — the upstream ``GET /session/status`` sees the
    ``X-Opencode-Directory`` header and NO ``directory`` (or ``v``) query
    parameter, exactly like the direct path."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.query
        captured["directory_header"] = request.headers.get(
            "x-opencode-directory")
        return httpx.Response(200, content=STATUS_PAYLOAD)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    # Production stack: the selector consumes v + directory (query →
    # stash + strip) before the route runs.
    app.add_middleware(SlimapiSelectorMiddleware)
    try:
        async with _client(app) as client:
            resp = await client.get(
                "/slimapi/sessions/status?v=4&directory=/w")
        assert resp.status_code == 200
        # The lease path was taken (registry present + coalesce_enabled).
        assert app.state.raw_fetch_registry is not None
        # Upstream sees the header channel only — no directory/v query.
        assert captured["directory_header"] == "/w"
        query = captured["query"]
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        assert query == ""
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
                _get_many(client_a, ["/slimapi/sessions/status"] * 3),
                _get_many(client_b, ["/slimapi/sessions/status"] * 3),
            )
        assert calls == {"a": 1, "b": 1}
    finally:
        _teardown(app_a)
        _teardown(app_b)
        # (V2b note: this was originally a sessions-LIST cross-app test;
        # the list lease flight was removed with the v4-only teardown, so
        # it now guards the STATUS flight's cross-app isolation instead.)


# (test_list_upstream_5xx_all_waiters_503 — the LIST half of the 5xx
# propagation family — was removed with the V2b src teardown: the list
# lease flight no longer exists; the v4 facade's per-caller 5xx → 503
# mapping is locked by tests/test_sessions_v4_matrix.py. The STATUS
# waiters variant below keeps guarding the shared-flight failure shape.)


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
# (REMOVED with the V2b src teardown: both tests drove the sessions-LIST
# lease path via monkeypatched ``sessions._project_sessions`` / the
# orjson spy — the list lease no longer exists under the v4-only window
# and the helper was physically removed. The v4 facade's own
# admission-under-busy discipline is locked by tests/test_sessions_v4_matrix.py.)
# ---------------------------------------------------------------------------
