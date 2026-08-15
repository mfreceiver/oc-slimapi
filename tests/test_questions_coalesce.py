"""Route-level coalescing tests for questions / permissions aggregation
(traffic plan Batch 1 / Task 1.4, A4-C1..C2).

Two-level dedup per plan §3.x + Task 1.4:

* LEVEL 1 — discovery: the ``GET /experimental/session?roots=true`` call is
  single-flighted under a FIXED key (``("discovery", id(upstream), limit)``),
  so concurrent /questions, /permissions — and any mix of the two — share
  ONE discovery GET.
* LEVEL 2 — per-dir: each directory's ``GET /question`` / ``GET /permission``
  is single-flighted under
  ``("question-dir"|"permission-dir", id(upstream), dir)``.

The envelope aggregation stays per-caller (sliding window, budgets,
``errors[]`` isolation, pack offload) — byte-identical to the direct path.
Per-dir upstream failures raise inside the flight factory so the flight
FAILS (immediate refund, never grace-retained / negative-cached) while every
joiner isolates the SAME error code into its own envelope.
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
from oc_slimapi.routes import permissions, questions
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

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
        server_api_version=2,
        accepted_client_versions=(2, 2),
        coalesce_enabled=True,
        raw_fetch_concurrency=8,
        # discovery + per-dir flights all reserve max_response_bytes; this
        # budget holds 4 concurrent flights (dial down for exhaustion tests).
        raw_fetch_max_bytes=4 * 64 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.questions_semaphore = asyncio.Semaphore(
        settings.questions_fanout_concurrency,
    )
    app.state.permissions_semaphore = asyncio.Semaphore(
        settings.permissions_fanout,
    )
    if settings.coalesce_enabled:
        app.state.raw_fetch_registry = LeasedSingleFlight(
            max_bytes=settings.raw_fetch_max_bytes,
            network_concurrency=settings.raw_fetch_concurrency,
        )
    app.include_router(questions.router)
    app.include_router(permissions.router)
    register_error_handlers(app)
    install_proxy(app)
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


def _question(qid: str) -> dict:
    return {
        "id": qid,
        "sessionID": "01HSESSION",
        "questions": [{"id": qid, "name": "confirm", "text": "proceed?"}],
        "tool": {"name": "Bash"},
    }


def _permission(qid: str) -> dict:
    return {"id": qid, "sessionID": "01HSESSION", "pattern": {"bash": "*"}}


def _sessions_body(*directories: str) -> bytes:
    return orjson.dumps([
        {
            "id": f"ses_{i:04d}",
            "directory": d,
            "time": {"updated": 0, "created": 0},
        }
        for i, d in enumerate(directories)
    ])


class _CountingHandler:
    """Upstream mock: counts discovery + per-dir GETs, serves /question and
    /permission per directory, optionally failing selected dirs and gating
    responses so concurrent callers genuinely overlap in flight."""

    def __init__(self, *, directories=("/a", "/b"), fail_dirs=(),
                 gate_seconds: float = 0.0):
        self.directories = directories
        self.fail_dirs = set(fail_dirs)
        self.gate_seconds = gate_seconds
        self.calls = {"discovery": 0, "question": {}, "permission": {}}

    async def _maybe_gate(self):
        if self.gate_seconds:
            await asyncio.sleep(self.gate_seconds)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        directory = request.headers.get("x-opencode-directory", "")
        if path == "/experimental/session":
            self.calls["discovery"] += 1
            await self._maybe_gate()
            return httpx.Response(200, content=_sessions_body(*self.directories))
        if path == "/question":
            self.calls["question"][directory] = (
                self.calls["question"].get(directory, 0) + 1)
            await self._maybe_gate()
            if directory in self.fail_dirs:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"q_{directory}")]))
        if path == "/permission":
            self.calls["permission"][directory] = (
                self.calls["permission"].get(directory, 0) + 1)
            await self._maybe_gate()
            if directory in self.fail_dirs:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(
                200, content=orjson.dumps([_permission(f"p_{directory}")]))
        return httpx.Response(404, content=b"not found")


# ---------------------------------------------------------------------------
# A4-C1 — questions two-level dedup.
# ---------------------------------------------------------------------------

async def test_questions_two_level_dedup(upstream_factory):
    handler = _CountingHandler(gate_seconds=0.05)
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/questions"] * 3)
        assert all(r.status_code == 200 for r in responses)
        # LEVEL 1: one discovery GET shared by all three requests
        assert handler.calls["discovery"] == 1
        # LEVEL 2: one /question GET per directory (budget available)
        assert handler.calls["question"] == {"/a": 1, "/b": 1}
        # envelope aggregation correct and identical across callers
        bodies = {r.content for r in responses}
        assert len(bodies) == 1
        envelope = orjson.loads(responses[0].content)
        assert envelope["discoveryComplete"] is True
        assert envelope["authoritativeDirectories"] is None
        assert envelope["errors"] == []
        assert len(envelope["items"]) == 2
        assert {item["directory"] for item in envelope["items"]} == {"/a", "/b"}
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_questions_budget_exhaustion_correct_responses(upstream_factory):
    """Capacity-1 budget: discovery leases, the per-dir fetches bypass
    direct — every response still correct and the ledger stays bounded."""
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    settings = _settings(raw_fetch_max_bytes=64 * 1024)  # exactly 1 reserve
    app = _build_app(settings, upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/questions", headers=HDR)
        assert response.status_code == 200
        envelope = orjson.loads(response.content)
        assert len(envelope["items"]) == 2  # both dirs fetched (direct)
        assert envelope["errors"] == []
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_questions_per_dir_error_isolated_and_shared(upstream_factory):
    """/a fails with 5xx → the flight FAILS (never negative-cached) and every
    concurrent request isolates the SAME error code for /a while /b's items
    survive."""
    handler = _CountingHandler(
        directories=("/a", "/b"), fail_dirs={"/a"}, gate_seconds=0.05,
    )
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/questions"] * 3)
        assert all(r.status_code == 200 for r in responses)
        bodies = {r.content for r in responses}
        assert len(bodies) == 1
        envelope = orjson.loads(responses[0].content)
        assert envelope["errors"] == [
            {"directory": "/a", "code": "upstream_unavailable"},
        ]
        assert [item["directory"] for item in envelope["items"]] == ["/b"]
        assert envelope["authoritativeDirectories"] == ["/b"]
    finally:
        _teardown(app)


async def test_questions_discovery_total_failure(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/questions"] * 3)
        assert all(r.status_code == 503 for r in responses)
        codes = {orjson.loads(r.content).get("code") for r in responses}
        assert codes == {"upstream_unavailable"}
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A4-C2 — permissions isomorphic + shared discovery + bypass.
# ---------------------------------------------------------------------------

async def test_permissions_two_level_dedup(upstream_factory):
    handler = _CountingHandler(gate_seconds=0.05)
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/permissions"] * 3)
        assert all(r.status_code == 200 for r in responses)
        assert handler.calls["discovery"] == 1
        assert handler.calls["permission"] == {"/a": 1, "/b": 1}
        assert len({r.content for r in responses}) == 1
        envelope = orjson.loads(responses[0].content)
        assert envelope["errors"] == []
        assert len(envelope["items"]) == 2
        registry.shutdown()
        assert registry.leased_bytes == 0
    finally:
        _teardown(app)


async def test_questions_and_permissions_share_discovery(upstream_factory):
    """The FIXED discovery key means a concurrent /questions + /permissions
    burst shares ONE discovery GET (cross-route level-1 dedup)."""
    handler = _CountingHandler(gate_seconds=0.05)
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            responses = await _get_many(client, [
                "/slimapi/questions",
                "/slimapi/permissions",
                "/slimapi/questions",
            ])
        assert all(r.status_code == 200 for r in responses)
        assert handler.calls["discovery"] == 1
        # each route's per-dir level still fetches its own resource
        assert handler.calls["question"] == {"/a": 1, "/b": 1}
        assert handler.calls["permission"] == {"/a": 1, "/b": 1}
    finally:
        _teardown(app)


async def test_coalesce_disabled_bypass(upstream_factory):
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(coalesce_enabled=False), upstream)
    assert getattr(app.state, "raw_fetch_registry", None) is None
    try:
        async with _client(app) as client:
            responses = await _get_many(
                client, ["/slimapi/questions", "/slimapi/permissions"] * 2)
        assert all(r.status_code == 200 for r in responses)
        # bypass: every request does its own discovery + per-dir GETs
        assert handler.calls["discovery"] == 4
        assert handler.calls["question"] == {"/a": 2, "/b": 2}
        assert handler.calls["permission"] == {"/a": 2, "/b": 2}
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# Final-review C2 — discovery lease/memory invariants (raw-bytes sharing).
# The shared flight value must be the CAPPED RAW BODY (≤ reserve_bytes), the
# per-caller parse must happen INSIDE the lease, and the expanded JSON graph
# must NOT outlive the lease release (plan §3.x GET→caller-consumption
# accounting invariant; final review B1 fix, 2026-08-16).
# ---------------------------------------------------------------------------

async def test_c2_1_shared_discovery_value_is_capped_raw_bytes(
    upstream_factory, monkeypatch,
):
    """① The value stored in the discovery flight is the capped raw body
    (``bytes``), never the expanded ``list[dict]`` session graph."""
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    captured: list[tuple] = []

    original = registry.fetch_or_bypass

    async def _spy(key, factory, reserve_bytes):
        lease = await original(key, factory, reserve_bytes)
        if lease is not None and key[0] == "discovery":
            captured.append(lease.body)
        return lease

    monkeypatch.setattr(registry, "fetch_or_bypass", _spy)
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/questions", headers=HDR)
        assert response.status_code == 200
        assert handler.calls["discovery"] == 1
        assert len(captured) == 1
        body, complete = captured[0]
        assert isinstance(body, bytes)         # raw bytes, NOT a JSON graph
        assert not isinstance(body, list)
        assert complete is True
        assert orjson.loads(body) == orjson.loads(_sessions_body("/a", "/b"))
        # raw body is within the flight's reserve accounting
        assert len(body) <= _settings().max_response_bytes
    finally:
        _teardown(app)


async def test_c2_2_directories_derived_while_lease_held(
    upstream_factory, monkeypatch,
):
    """② Budget ownership covers the caller-side consumption: at the moment
    the caller derives its directory list from the shared body, its lease
    reference is still outstanding (discovery entry caller_refs ≥ 1)."""
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    probe: dict = {}

    real = questions._directories_from_sessions

    def _spy(payload):
        for key, entries in registry.snapshot().items():
            if key[0] == "discovery":
                probe["refs_at_derive"] = entries[0][2]
                probe["state_at_derive"] = entries[0][3]
        return real(payload)

    monkeypatch.setattr(questions, "_directories_from_sessions", _spy)
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/questions", headers=HDR)
        assert response.status_code == 200
        # lease outstanding while the caller consumes the shared body
        assert probe["refs_at_derive"] >= 1
        # successful flight already converted to grace at handout time
        assert probe["state_at_derive"] == "grace"
        # after the response the caller's reference is released (refs == 0;
        # the entry itself may sit out its grace window or already be gone)
        await asyncio.sleep(0)
        disc = [
            entry
            for key, entries in registry.snapshot().items()
            if key[0] == "discovery"
            for entry in entries
        ]
        assert all(entry[2] == 0 for entry in disc)
    finally:
        _teardown(app)


async def test_c2_3_expanded_graph_dropped_after_lease_release(
    upstream_factory, monkeypatch,
):
    """③ The caller's expanded session graph does not outlive the lease:
    after the response completes, every route-side parse result is
    unreachable (weakref probe — covers the discovery graph AND the per-dir
    graphs)."""
    import gc
    import types
    import weakref

    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)

    class _WList(list):
        """weakref-able list stand-in for a parsed JSON array."""

    refs: list[weakref.ref] = []
    real_loads = orjson.loads

    def _loads(data):
        out = _WList(real_loads(data))
        refs.append(weakref.ref(out))
        return out

    # namespace swap: questions.py's `orjson.loads` resolves to the probe
    # (dumps is still the real one for the envelope pack worker)
    monkeypatch.setattr(
        questions, "orjson",
        types.SimpleNamespace(loads=_loads, dumps=orjson.dumps),
    )
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/questions", headers=HDR)
        assert response.status_code == 200
        assert len(refs) >= 3  # discovery + two per-dir parses went through
        gc.collect()
        assert all(ref() is None for ref in refs)
    finally:
        _teardown(app)


async def test_c2_4_per_dir_level2_shares_raw_bytes_not_graphs(
    upstream_factory, monkeypatch,
):
    """④ LEVEL 2 audit lock-in: per-dir flights share the capped raw body
    (``bytes``) — the parse + directory stamping stays per-caller (already
    compliant since Batch 1; this pins it against regression)."""
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    captured: list[tuple] = []

    original = registry.fetch_or_bypass

    async def _spy(key, factory, reserve_bytes):
        lease = await original(key, factory, reserve_bytes)
        if lease is not None and key[0] == "question-dir":
            captured.append(lease.body)
        return lease

    monkeypatch.setattr(registry, "fetch_or_bypass", _spy)
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/questions", headers=HDR)
        assert response.status_code == 200
        assert len(captured) == 2  # /a + /b shared flights
        for body, total in captured:
            assert isinstance(body, bytes)
            assert not isinstance(body, list)
            assert len(body) <= _settings().max_response_bytes
    finally:
        _teardown(app)


async def test_c2_5_permissions_discovery_invariants_mirror(
    upstream_factory, monkeypatch,
):
    """① + ③ mirrored on /slimapi/permissions (isomorphic fix): the shared
    discovery value is capped raw bytes and no caller-side parse graph
    outlives the request."""
    import gc
    import types
    import weakref

    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    registry = app.state.raw_fetch_registry
    captured: list[tuple] = []

    original = registry.fetch_or_bypass

    async def _spy(key, factory, reserve_bytes):
        lease = await original(key, factory, reserve_bytes)
        if lease is not None and key[0] == "discovery":
            captured.append(lease.body)
        return lease

    monkeypatch.setattr(registry, "fetch_or_bypass", _spy)

    class _WList(list):
        pass

    refs: list[weakref.ref] = []
    real_loads = orjson.loads

    def _loads(data):
        out = _WList(real_loads(data))
        refs.append(weakref.ref(out))
        return out

    monkeypatch.setattr(
        permissions, "orjson",
        types.SimpleNamespace(loads=_loads, dumps=orjson.dumps),
    )
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/permissions", headers=HDR)
        assert response.status_code == 200
        # ① shared value is raw bytes
        assert len(captured) == 1
        body, complete = captured[0]
        assert isinstance(body, bytes)
        assert not isinstance(body, list)
        assert complete is True
        # ③ no caller-side parse graph survives the request
        assert len(refs) >= 3
        gc.collect()
        assert all(ref() is None for ref in refs)
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# Final review rev-1 probe — post-release / pre-fanout window: with the
# fan-out blocked, the shared discovery raw body must be unreachable once
# the lease is released and its grace entry reaped (the route's local
# `lease` handle is dead; the registry-level release cut makes even a live
# released Lease body-free). Probe = refcount comparison against a fresh
# calibration object (bytes subclasses are not weakref-able, so a weakref
# probe is not available for the exact shared object — pragmatic object
# size probe per rev-1). Contrast: reachable pre-release (at derive time).
# ---------------------------------------------------------------------------


async def _blocked_fanout_probe(
    app, route_module, route_path, monkeypatch,
):
    """Shared probe body for questions/permissions: block the fan-out after
    the discovery lease is released, then assert the shared raw body has no
    references beyond the probe's own holder (refcount == calibration)
    while the request is still in flight."""
    import gc
    import sys

    old_registry = app.state.raw_fetch_registry
    # tiny grace so the discovery entry is reaped deterministically
    probe_registry = LeasedSingleFlight(
        max_bytes=old_registry._max_bytes,
        network_concurrency=old_registry._network_sem._value
        if old_registry._network_sem is not None else None,
        result_grace_seconds=0.05,
    )
    app.state.raw_fetch_registry = probe_registry

    entered = asyncio.Event()
    unblock = asyncio.Event()
    holder: dict = {}
    probe: dict = {}

    real_raw = route_module.fetch_global_root_sessions_raw

    async def _wrapped_raw(upstream_client, request, *, limit):
        body, complete = await real_raw(upstream_client, request, limit=limit)
        if "body" not in holder:
            # capture the EXACT shared bytes object + a guaranteed-fresh
            # calibration object (nobody else can reference it; content
            # equality irrelevant — it only measures the refcount floor).
            # NB: b"".join((body,)) / body + b"" may return the SAME
            # object in CPython — appending a byte guarantees a copy.
            holder["body"] = body
            holder["calib"] = body + b"\x00"
        return body, complete

    monkeypatch.setattr(
        route_module, "fetch_global_root_sessions_raw", _wrapped_raw)

    real_derive = route_module._directories_from_sessions

    def _derive_spy(payload):
        # contrast: pre-release the body is referenced (lease held + route
        # local + entry) — strictly above the calibration floor
        probe["derive_refcount"] = sys.getrefcount(holder["body"])
        probe["calib_refcount"] = sys.getrefcount(holder["calib"])
        return real_derive(payload)

    monkeypatch.setattr(
        route_module, "_directories_from_sessions", _derive_spy)

    async def _blocked_collect(*args, **kwargs):
        entered.set()  # lease already released before Step 3 starts
        await unblock.wait()
        return [], [], [], False  # empty fan-out result

    monkeypatch.setattr(
        route_module, "_collect_with_byte_budget", _blocked_collect)

    async with _client(app) as client:
        task = asyncio.create_task(client.get(route_path, headers=HDR))
        await entered.wait()
        try:
            # post-release / pre-fanout window: wait past the 0.05s grace
            # so the zero-ref discovery entry is reaped + refunded, then
            # collect cycles
            await asyncio.sleep(0.15)
            gc.collect()
            # contrast: pre-release the shared body had MORE refs than the
            # calibration floor (lease + route local + entry)
            assert (
                probe["derive_refcount"] > probe["calib_refcount"]), probe
            # mid-fanout: nothing but the probe's holder references the
            # body — the released Lease (registry cut) and the deleted
            # route locals hold nothing
            base = sys.getrefcount(holder["calib"])
            assert sys.getrefcount(holder["body"]) == base, (
                "shared raw body still referenced post-release/pre-fanout")
        finally:
            unblock.set()  # never leave the request task dangling
            response = await task
        assert response.status_code == 200

    probe_registry.shutdown()
    assert probe_registry.leased_bytes == 0


async def test_c2_6_questions_raw_body_unreferenced_during_blocked_fanout(
    upstream_factory, monkeypatch,
):
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        await _blocked_fanout_probe(
            app, questions, "/slimapi/questions", monkeypatch)
    finally:
        _teardown(app)


async def test_c2_7_permissions_raw_body_unreferenced_during_blocked_fanout(
    upstream_factory, monkeypatch,
):
    handler = _CountingHandler()
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        await _blocked_fanout_probe(
            app, permissions, "/slimapi/permissions", monkeypatch)
    finally:
        _teardown(app)
