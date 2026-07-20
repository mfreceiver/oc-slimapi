"""Route-level integration tests for the skeleton/messages perf fix.

These exercise the FastAPI routes end-to-end through a mocked upstream so we
can assert the wire-level contract for the three fixes:

* skeleton path still returns projected JSON (regression baseline)
* response exceeding ``max_response_bytes`` returns 413 ``response_too_large``
  with the configured ``limit`` field
* pool saturated returns 503 ``transform_busy`` + ``Retry-After`` header
* a slow transform does NOT block the event loop: ``/slimapi/health`` stays
  responsive while a skeleton request churns in a worker thread

The app is constructed fresh per test (bypassing the module-level lifespan)
so we can dial down the transform-pool knobs without touching env vars.
"""
from __future__ import annotations

import asyncio
import gzip
import time

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, questions, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_json_bytes=64 * 1024 * 1024,
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        route_secret="x" * 32,
        route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app with the routers wired up and ``app.state``
    pre-populated, mirroring ``oc_slimapi.app.lifespan`` but without running
    the smoke probe against the mocked upstream."""
    app = FastAPI(title="oc-slimapi-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    app.state.schema_degraded = False
    app.state.hubs = HubRegistry(upstream)
    for router in (health.router, sessions.router, messages.router, questions.router, events.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _sample_upstream_payload() -> bytes:
    return orjson.dumps([
        {
            "info": {"id": "m1", "role": "user"},
            "parts": [
                {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
                {
                    "id": "p2", "type": "tool", "messageID": "m1", "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls", "debug": "drop me"},
                        "output": "huge output that skeleton must omit",
                    },
                },
            ],
        }
    ])


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test.

    Mirrors ``oc_slimapi.upstream.create_client``: base_url must be set so
    relative upstream paths like ``/session/{sid}/message`` resolve under the
    MockTransport instead of being mis-parsed as absolute."""
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


@pytest.fixture
async def app_and_client(upstream_factory):
    """Default app + a happy-path upstream returning the sample payload."""
    payload = _sample_upstream_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    try:
        yield app, upstream
    finally:
        app.state.transforms.shutdown()


async def test_skeleton_messages_route_returns_projected_json(app_and_client):
    app, _ = app_and_client
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/messages/s1?mode=skeleton",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
        )
    assert response.status_code == 200
    # Content-Encoding header is set when the worker applied gzip; httpx will
    # have transparently decompressed response.content for us, but the header
    # is what the wire contract guarantees to non-httpx clients.
    assert response.headers["Content-Encoding"] == "gzip"
    body = orjson.loads(response.content)
    # Skeleton contract: tool output dropped, command input kept.
    tool_part = body[0]["parts"][1]
    assert tool_part["state"]["input"] == {"command": "ls"}
    assert "output" not in tool_part["state"]
    assert response.headers["Vary"] == "Accept-Encoding"
    assert response.headers["Cache-Control"] == "no-store"


async def test_skeleton_single_message_route_returns_projected_json(upstream_factory):
    payload = orjson.dumps({
        "info": {"id": "m1", "role": "user"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
            {
                "id": "p2", "type": "tool", "messageID": "m1", "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls", "debug": "drop me"},
                    "output": "huge output that skeleton must omit",
                },
            },
        ],
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        assert body["info"] == {"id": "m1", "role": "user"}
        tool_part = body["parts"][1]
        assert tool_part["state"]["input"] == {"command": "ls"}
        assert "output" not in tool_part["state"]
    finally:
        app.state.transforms.shutdown()


async def test_messages_route_returns_413_when_upstream_body_exceeds_cap(upstream_factory):
    """Critical fix: streaming cap-read must 413 BEFORE buffering the entire body."""
    cap = 4 * 1024
    oversized = b"x" * (cap * 16)  # 16x the cap; classic OOM-upstream shape.

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    upstream = upstream_factory(handler)
    settings = _settings(max_response_bytes=cap, transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


async def test_messages_route_returns_503_when_transform_admission_times_out(app_and_client):
    """Pre-acquire the single admission slot, then call the route — it must
    emit 503 transform_busy with a Retry-After header matching the spec."""
    app, _ = app_and_client
    pool = app.state.transforms

    # Hold the only admission slot for the duration of the request.
    async with pool:
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "transform_busy"
        assert body["retry_after"] == 2
        assert response.headers["Retry-After"] == "2"


async def test_messages_route_returns_503_for_single_message_when_admission_saturated(app_and_client):
    app, _ = app_and_client
    pool = app.state.transforms

    async with pool:
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 503
        assert response.json()["code"] == "transform_busy"


def _msg(mid: str, updated: int | None, *, text: str = "x" * 200) -> dict:
    """Build an upstream-shape message with ``info.time.updated`` set.

    When ``updated`` is None the ``info.time`` block is omitted entirely so
    we exercise the defensive branch of the A2=A filter (missing timestamp).
    """
    info: dict = {"id": mid, "role": "user"}
    if updated is not None:
        info["time"] = {"updated": updated, "created": updated}
    return {
        "info": info,
        "parts": [{"id": f"p-{mid}", "type": "text", "messageID": mid, "text": text}],
    }


async def test_messages_since_returns_only_items_at_or_above_ts(upstream_factory):
    """① A2=A filter (contract §5): only items with ``time.updated >= ts``
    are returned; the scan stops at the first item below the ts floor."""
    page = [
        _msg("m1", updated=200),
        _msg("m2", updated=150),
        _msg("m3", updated=100),  # == ts, included (boundary, see ②)
        _msg("m4", updated=50),   # < ts, excluded — also stops the scan
        _msg("m5", updated=10),   # < ts, never reached
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/100?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        assert [item["info"]["id"] for item in body] == ["m1", "m2", "m3"]
        # Hit the ts floor → no next cursor (no more matching items possible).
        assert "X-Next-Cursor" not in response.headers
    finally:
        app.state.transforms.shutdown()


def _msg_created_only(mid: str, created: int, *, text: str = "x" * 200) -> dict:
    """Upstream-shape message with only ``info.time.created`` (no ``updated``).

    Mirrors opencode v1.18.3 message schema: User.time={created},
    Assistant.time={created, completed?}. Used to lock the Gap-1 regression
    where ``_item_updated`` only read ``updated`` and made /since a no-op.
    """
    return {
        "info": {
            "id": mid,
            "role": "user",
            "time": {"created": created},
        },
        "parts": [{"id": f"p-{mid}", "type": "text", "messageID": mid, "text": text}],
    }


async def test_messages_since_filters_by_created_when_updated_absent(upstream_factory):
    """Gap 1 regression: /since/{ts} must filter on ``updated or created``.

    opencode v1.18.3 messages have no ``info.time.updated``; watermark is
    ``created`` (same expression as digest updatedAt, without now fallback).
    Old code returned None → always included → no-op filter (FAIL this test).
    """
    # Newest-first page (opencode order). created: 3000, 2000, 1000.
    page = [
        _msg_created_only("m3", 3000),
        _msg_created_only("m2", 2000),
        _msg_created_only("m1", 1000),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # /since/2000 → only created >= 2000 (m3, m2); m1 (1000) excluded.
            response = await client.get(
                "/slimapi/messages/s1/since/2000?mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert response.status_code == 200
            body = orjson.loads(response.content)
            ids = [item["info"]["id"] for item in body]
            assert ids == ["m3", "m2"]
            assert "m1" not in ids
            assert len(body) == 2

            # /since/0 → all items (watermark >= 0 for every real message).
            response_all = await client.get(
                "/slimapi/messages/s1/since/0?mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert response_all.status_code == 200
            body_all = orjson.loads(response_all.content)
            assert [item["info"]["id"] for item in body_all] == ["m3", "m2", "m1"]
            assert len(body_all) == 3
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_boundary_includes_equal_ts(upstream_factory):
    """② Boundary: items with ``time.updated == ts`` are included (``>=``, not ``>``).
    The contract pins A2=A and relies on client-side messageID dedup."""
    page = [
        _msg("m1", updated=101),
        _msg("m2", updated=100),  # exactly == ts → MUST be included
        _msg("m3", updated=99),   # < ts → excluded
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/100?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        assert [item["info"]["id"] for item in body] == ["m1", "m2"]
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_before_cursor_paginates(upstream_factory):
    """③ ``?before`` cursor: first request fills ``limit`` and advertises
    ``X-Next-Cursor`` (= opencode's opaque Link cursor, NOT a messageID); the
    client passes it back as ``?before`` on the next call, which sidecar
    forwards to opencode verbatim (native before support)."""
    # No-before page: 3 items all >= ts=100, plus opencode's Link cursor
    # signalling more pages exist (our limit=2 fills before we walk it).
    page_no_before = [_msg("m1", 300), _msg("m2", 250), _msg("m3", 200)]
    # before=XYZopaque page: 1 older matching item, no further Link.
    page_with_before = [_msg("m4", 150)]
    seen_befores: list[str | None] = []
    link_no_before = (
        '<http://127.0.0.1:4096/session/s1/message?before=XYZopaque&limit=2>; '
        'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        before = request.url.params.get("before")
        seen_befores.append(before)
        if before is None:
            return httpx.Response(
                200, content=orjson.dumps(page_no_before),
                headers={"Content-Type": "application/json", "Link": link_no_before},
            )
        assert before == "XYZopaque"
        return httpx.Response(
            200, content=orjson.dumps(page_with_before),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.get(
                "/slimapi/messages/s1/since/100?limit=2&mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert r1.status_code == 200
            body1 = orjson.loads(r1.content)
            assert [item["info"]["id"] for item in body1] == ["m1", "m2"]
            # Filled limit without hitting ts floor → advertise opencode's
            # opaque Link cursor verbatim (NOT a messageID — that would 400
            # at upstream on the next call).
            assert r1.headers.get("X-Next-Cursor") == "XYZopaque"
            assert r1.headers.get("X-Next-Cursor") not in ("m1", "m2", "m3")

            r2 = await client.get(
                "/slimapi/messages/s1/since/100?limit=2&before=XYZopaque&mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert r2.status_code == 200
            body2 = orjson.loads(r2.content)
            assert [item["info"]["id"] for item in body2] == ["m4"]
            # Under limit + no upstream Link → no more data.
            assert "X-Next-Cursor" not in r2.headers
        # Sidecar forwarded the client's opaque before verbatim to opencode.
        assert seen_befores == [None, "XYZopaque"]
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_uses_single_admission_for_full_scan(upstream_factory):
    """Multi-page timestamp scan walks 2 upstream pages and returns the merged
    filtered set. Admission is acquired once for the whole scan (structural:
    the ``async with pool:`` wraps the page loop in messages.py — see
    `test_messages_route_returns_503_when_transform_admission_times_out` for
    the admission-itself assertion). opencode advertises page 2 via the Link
    header (RFC 5988 rel="next"); sidecar extracts the opaque cursor and
    continues the scan under the same admission."""
    page1 = [_msg("m1", 300), _msg("m2", 250)]
    page2 = [_msg("m3", 200)]
    pages = iter([page1, page2])
    upstream_calls = {"count": 0}
    link_page1 = (
        '<http://127.0.0.1:4096/session/s1/message?before=page2cursor&limit=5>; '
        'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls["count"] += 1
        try:
            page = next(pages)
        except StopIteration:  # pragma: no cover - test bug guard
            return httpx.Response(404)
        headers = {"Content-Type": "application/json"}
        # Page 1 advertises a next cursor so the scan continues to page 2.
        if request.url.params.get("before") is None:
            headers["Link"] = link_page1
        return httpx.Response(200, content=orjson.dumps(page), headers=headers)

    upstream = upstream_factory(handler)
    settings = _settings(transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/0?limit=5&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # 2 pages merged newest→oldest, all >= ts=0.
        assert [item["info"]["id"] for item in body] == ["m1", "m2", "m3"]
        # Walked exactly 2 upstream pages under one admission.
        assert upstream_calls["count"] == 2
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_enforces_cumulative_byte_budget(upstream_factory):
    """④ The whole multi-page scan shares one ``max_response_bytes`` budget
    (contract §7). Each individual upstream page fits comfortably under the
    cap, but the cumulative total crosses it on page 2 → 413. The scan must
    have made exactly 2 upstream calls — proving the budget is enforced
    ACROSS pages under a single admission, not per-page."""
    cap = 8 * 1024
    # Each item serialises to ~1.1 KiB; 4 items ≈ 4.4 KiB per page. That is
    # well under the 8 KiB cap individually, but 2 pages together (~8.8 KiB)
    # exceed it — so page 2's read tips the cumulative budget into 413.
    page = [{"info": {"id": f"m{i}"}, "parts": [
        {"id": f"p{i}", "type": "text", "messageID": f"m{i}", "text": "x" * 1024},
    ]} for i in range(4)]
    # Sanity-check the byte math so a future orjson size drift turns into a
    # clear assertion failure rather than a silent test-gap.
    page_bytes = len(orjson.dumps(page))
    assert page_bytes < cap, f"page alone exceeds cap ({page_bytes} >= {cap})"
    assert 2 * page_bytes > cap, (
        f"two pages do not exceed cap ({2 * page_bytes} <= {cap}); test would "
        "not exercise cross-page cumulative budget"
    )
    link = (
        '<http://127.0.0.1:4096/session/s1/message?before=page2cursor&limit=50>; '
        'rel="next"'
    )
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        headers = {"Content-Type": "application/json"}
        # Page 1 advertises an opaque Link cursor so the scan continues to
        # page 2 (where the cumulative budget then trips).
        if calls["count"] == 1:
            headers["Link"] = link
        return httpx.Response(200, content=orjson.dumps(page), headers=headers)

    upstream = upstream_factory(handler)
    settings = _settings(max_response_bytes=cap, transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/0?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
        # EXACTLY 2 upstream calls — page 1 returned (under cap), page 2's
        # read tipped the cumulative total over the cap. This is the lock
        # that proves the budget is shared across pages under one admission
        # (a per-page budget would 413 on page 1, giving count == 1).
        assert calls["count"] == 2, (
            f"expected 2 upstream pages under one admission, got {calls['count']}"
        )
    finally:
        app.state.transforms.shutdown()


async def test_health_stays_responsive_during_slow_transform(app_and_client, monkeypatch):
    """The headline fix: a slow transform in a worker thread must not block
    /slimapi/health. We monkey-patch the worker entrypoint to add a synthetic
    delay and measure health latency while the skeleton request is in flight."""
    app, _ = app_and_client

    import oc_slimapi.routes.messages as msgs_mod

    original_pack = msgs_mod.project_and_pack
    slow_packs_started = asyncio.Event()

    def slow_pack(body, *, single, accept_encoding):
        # Signal that the worker has picked up the job, then park it.
        # set() is sync; scheduling it via the running loop is fine here
        # because we are still inside the worker thread, but the event is
        # bound to the event loop created by the test's asyncio.run.
        slow_packs_started.set()
        time.sleep(0.5)
        return original_pack(body, single=single, accept_encoding=accept_encoding)

    monkeypatch.setattr(msgs_mod, "project_and_pack", slow_pack)

    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        skeleton_task = asyncio.create_task(client.get(
            "/slimapi/messages/s1?mode=skeleton",
            headers=VERSION_HEADERS,
        ))

        # Wait until the worker is actually churning before firing the health
        # probe — otherwise the test could pass trivially.
        await asyncio.wait_for(slow_packs_started.wait(), timeout=2.0)
        # Yield once more to ensure the worker thread is genuinely in time.sleep.
        await asyncio.sleep(0.02)

        health_start = time.monotonic()
        health_response = await client.get("/slimapi/health", headers=VERSION_HEADERS)
        health_elapsed = time.monotonic() - health_start

        skeleton_response = await skeleton_task

    assert health_response.status_code == 200
    assert skeleton_response.status_code == 200  # transform still completes
    # Health must return well inside the 0.5s worker sleep. Generous slack for
    # CI jitter, but tight enough to catch a regression that re-runs the
    # transform on the event loop (would push this to ~0.5s).
    assert health_elapsed < 0.2, (
        f"health took {health_elapsed:.3f}s during a 0.5s transform — "
        "event loop appears blocked"
    )


# ---------------------------------------------------------------------------
# ③a — Contract §9: every JSON route (including 413 error paths) must honour
# client ``Accept-Encoding: gzip``. The three 413 branches in messages.py are
# exercised separately below (list / since / full).
# ---------------------------------------------------------------------------

async def test_messages_list_413_negotiates_gzip(upstream_factory):
    """/messages list 413 (response_too_large) compresses the error body
    when the client sends Accept-Encoding: gzip."""
    cap = 4 * 1024
    oversized = b"x" * (cap * 16)  # 16x the cap; classic OOM-upstream shape.

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    upstream = upstream_factory(handler)
    settings = _settings(max_response_bytes=cap, transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            )
        assert response.status_code == 413
        # Content-Encoding is the wire contract; httpx auto-decompressed
        # response.content but the header is what non-httpx clients see.
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        body = orjson.loads(response.content)
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_413_negotiates_gzip(upstream_factory):
    """/since cumulative byte budget 413 (response_too_large) compresses the
    error body when the client sends Accept-Encoding: gzip."""
    cap = 8 * 1024
    # Items without info.time.updated → defensive-include branch, so the scan
    # keeps paging until the cumulative byte budget trips.
    big_page = [{"info": {"id": f"m{i}"}, "parts": [
        {"id": f"p{i}", "type": "text", "messageID": f"m{i}", "text": "x" * 1024},
    ]} for i in range(8)]  # ~8 KiB per page, exceeds cap on first read

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(big_page),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings(max_response_bytes=cap, transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/0?mode=skeleton",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            )
        assert response.status_code == 413
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        body = orjson.loads(response.content)
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


async def test_full_message_413_negotiates_gzip(upstream_factory):
    """/full/{mid} 413 (message_too_large, default mode=full) compresses the
    error body when the client sends Accept-Encoding: gzip."""
    cap = 4 * 1024
    oversized_msg = orjson.dumps({
        "info": {"id": "m1", "role": "user"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": "m1", "text": "x" * (cap * 4)},
        ],
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=oversized_msg,
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings(max_message_bytes=cap)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1",  # no ?mode= → default full
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            )
        assert response.status_code == 413
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        body = orjson.loads(response.content)
        assert body["code"] == "message_too_large"
        assert body["limitBytes"] == cap
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# ② + §9 — 503 transform_busy must also honour gzip now that _busy_response
# routes through error_response.
# ---------------------------------------------------------------------------

async def test_503_transform_busy_negotiates_gzip(app_and_client):
    """The 503 transform_busy response must honour gzip when the client asks
    for it — _busy_response now goes through error_response (contract §9)."""
    app, _ = app_and_client
    pool = app.state.transforms

    # Hold the only admission slot for the duration of the request.
    async with pool:
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            )
        assert response.status_code == 503
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        # Retry-After survives the move out of JSONResponse.
        assert response.headers["Retry-After"] == "2"
        body = orjson.loads(response.content)
        assert body["code"] == "transform_busy"
        assert body["retry_after"] == 2


# ---------------------------------------------------------------------------
# ③b — Empty path "" under the router prefix must hit directly (no 307).
# ---------------------------------------------------------------------------

async def test_messages_list_empty_path_does_not_307(app_and_client):
    """The /messages list uses ``@router.get("")`` with prefix
    ``/slimapi/messages/{sid}``. ``GET /slimapi/messages/s1`` must resolve to
    the route directly — no 307 redirect, no trailing slash required."""
    app, _ = app_and_client
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
    assert response.status_code != 307
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# ③c — /full/{mid} default mode is full (verbatim passthrough).
# ---------------------------------------------------------------------------

async def test_full_message_default_mode_is_full_passthrough(upstream_factory):
    """GET /slimapi/messages/s1/full/m1 (no ?mode=) defaults to full mode:
    verbatim passthrough of upstream's single-message body — no skeleton
    projection, so tool output / debug fields are preserved. The path
    migration must not have silently changed the default projection."""
    payload = orjson.dumps({
        "info": {"id": "m1", "role": "user"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
            {
                "id": "p2", "type": "tool", "messageID": "m1", "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls", "debug": "skeleton would drop me"},
                    "output": "huge output that skeleton would omit but full keeps",
                },
            },
        ],
    })

    def handler(request: httpx.Request) -> httpx.Response:
        # Full mode hits /session/{sid}/message/{mid}, not the listing.
        assert request.url.path == "/session/s1/message/m1"
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # Full mode: NO skeleton projection — debug input + tool output kept.
        tool_part = body["parts"][1]
        assert tool_part["state"]["input"] == {
            "command": "ls", "debug": "skeleton would drop me",
        }
        assert "output" in tool_part["state"]
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# ③e — cursor passthrough (Q1/Q2): sidecar translates opencode's RFC 5988
# ``Link: <...?before=...>; rel="next"`` header into the sidecar's
# ``X-Next-Cursor`` (opaque string, verbatim). Never synthesises a cursor
# from a messageID. Forwards client ?before verbatim to upstream.
# ---------------------------------------------------------------------------

async def test_messages_list_passes_through_opencode_link_cursor(upstream_factory):
    """Case 1 (more pages): opencode response carries a Link header with
    rel="next"; sidecar extracts the opaque before cursor and surfaces it
    verbatim as X-Next-Cursor. The upstream Link header itself must NOT
    leak through (sidecar's pagination contract is X-Next-Cursor)."""
    payload = orjson.dumps([_msg("m1", 100), _msg("m2", 90)])
    link = (
        '<http://127.0.0.1:4096/session/s1/message?before=ABCopaqueXYZ&limit=40>; '
        'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Opaque cursor passes through verbatim — NOT a synthesised messageID.
        assert response.headers.get("X-Next-Cursor") == "ABCopaqueXYZ"
        assert response.headers.get("X-Next-Cursor") not in ("m1", "m2")
        # Sidecar's pagination contract is X-Next-Cursor only; opencode's
        # Link header must not bleed through.
        assert "Link" not in response.headers
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_no_link_header_means_no_cursor(upstream_factory):
    """Case 2 (no more pages): opencode signals end-of-data by omitting the
    Link header. Sidecar must NOT invent a cursor (the prior synthesised
    messageID cursor was a bug — it would 400 at opencode on next call)."""
    payload = orjson.dumps([_msg("m1", 100)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert "X-Next-Cursor" not in response.headers
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_omits_cursor_when_ts_floor_hit(upstream_factory):
    """Case 3 (/since ts floor): opencode advertises a next page (Link header
    present), but the current page already contains items below the ts floor.
    Sidecar must NOT advertise a cursor — older pages would only carry items
    with time.updated < ts."""
    page = [_msg("m1", 200), _msg("m2", 50)]  # m2 < ts=100 → floor
    link = (
        '<http://127.0.0.1:4096/session/s1/message?before=ZZZopaque&limit=50>; '
        'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/100?mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # m1 kept (>= ts=100); m2 dropped (< ts).
        assert [item["info"]["id"] for item in body] == ["m1"]
        # opencode said "more pages exist" but the floor guarantees older
        # items are all sub-ts → suppress the cursor.
        assert "X-Next-Cursor" not in response.headers
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_forwards_client_before_cursor_verbatim(upstream_factory):
    """Case 4 (transparent before): client passes ``?before=<opaque cursor>``;
    sidecar forwards it to opencode unchanged (no decode / re-encode). This
    is what makes pagination actually work end-to-end."""
    payload = orjson.dumps([_msg("m1", 100)])
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["before"] = request.url.params.get("before")
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?before=ABCopaqueXYZ&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Verbatim: sidecar must not touch the opaque value at all.
        assert captured["before"] == "ABCopaqueXYZ"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Round-2 MAJOR: the verbatim-extraction regression net. The cursor carries
# percent-escapes AND a literal ``+``; parse_qs / unquote_plus would corrupt
# both. These tests lock the wire-level opaque contract end-to-end.
# ---------------------------------------------------------------------------

# A cursor whose wire form would be CHANGED by parse_qs/unquote_plus:
#   ``%2B`` → ``+`` (percent-decode)
#   ``%2F`` → ``/`` (percent-decode)
#   ``+``   → `` `` (form-query convention)
#   ``%3D`` → ``=`` (percent-decode)
# A verbatim extractor must return this string byte-for-byte; a parse_qs-based
# one would silently turn it into ``abc+def/ghi jkl=``.
_ESCAPED_CURSOR = "abc%2Bdef%2Fghi+jkl%3D"
# What parse_qs/unquote_plus WOULD corrupt it to — asserted against explicitly
# so a regression can't sneak through under a "looks similar" guise.
_ESCAPED_CURSOR_DECODED = "abc+def/ghi jkl="


async def test_messages_list_x_next_cursor_is_byte_for_byte_verbatim(upstream_factory):
    """Case A (regression net): opencode's Link header carries a cursor with
    percent-escapes (``%2B``, ``%2F``, ``%3D``) AND a literal ``+``. Sidecar
    MUST surface it as ``X-Next-Cursor`` byte-for-byte — no percent-decoding,
    no ``+``→space substitution. parse_qs/unquote_plus would corrupt this."""
    payload = orjson.dumps([_msg("m1", 100)])
    link = (
        f'<http://127.0.0.1:4096/session/s1/message?before={_ESCAPED_CURSOR}&limit=40>; '
        f'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Wire-level opaque lock: byte-for-byte equality, NOT "decoded contains".
        assert response.headers.get("X-Next-Cursor") == _ESCAPED_CURSOR
        # Explicit guard against the parse_qs regression — if the parser
        # ever goes back to parse_qs/unquote_plus, this assertion fires.
        assert response.headers.get("X-Next-Cursor") != _ESCAPED_CURSOR_DECODED
    finally:
        app.state.transforms.shutdown()


async def test_messages_since_x_next_cursor_is_byte_for_byte_verbatim(upstream_factory):
    """Case A on /since/{ts}: same regression net as the list endpoint, but
    exercises the multi-page scan's cursor emission path. opencode's Link
    cursor with percent-escapes must surface verbatim as X-Next-Cursor."""
    # Page fills limit=1 without hitting the ts floor → cursor is advertised.
    page = [_msg("m1", 200)]
    link = (
        f'<http://127.0.0.1:4096/session/s1/message?before={_ESCAPED_CURSOR}&limit=1>; '
        f'rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/100?limit=1&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Byte-for-byte equality with the original wire form.
        assert response.headers.get("X-Next-Cursor") == _ESCAPED_CURSOR
        assert response.headers.get("X-Next-Cursor") != _ESCAPED_CURSOR_DECODED
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_client_before_round_trips_percent_escapes(upstream_factory):
    """Case B (wire-level round-trip): client takes the percent-laden
    X-Next-Cursor from case A and passes it back as ``?before``. Sidecar
    forwards it to opencode byte-for-byte on the wire — assert via the raw
    query string captured in the upstream handler, NOT via the httpx-decoded
    ``params`` view (which would obscure any re-encoding)."""
    payload = orjson.dumps([_msg("m1", 100)])
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # request.url.query is the RAW query bytes httpx put on the wire —
        # percent-escapes preserved, no decoding. params.get() would decode
        # and hide a re-encode regression.
        captured["raw_query"] = request.url.query
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/slimapi/messages/s1?before={_ESCAPED_CURSOR}&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Wire-level: the raw upstream query bytes contain the cursor exactly
        # as the client sent it. If sidecar decoded & re-encoded with a
        # different encoder (e.g. %20 for space instead of +), this fires.
        assert b"before=" + _ESCAPED_CURSOR.encode() in captured["raw_query"], (
            f"upstream wire query = {captured['raw_query']!r}; "
            f"expected before={_ESCAPED_CURSOR} byte-for-byte"
        )
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# Cursor verbatim characterization (round-3 user ruling)
# ===========================================================================
#
# opencode's pagination cursor is a base64url-encoded JSON envelope (charset
# ``[A-Za-z0-9_-]`` plus optional ``=`` padding). The FastAPI+httpx pipeline
# is a FIXED POINT on this charset, so end-to-end cursor handling is safe
# for the real opencode format:
#
#   • Outbound (opencode ``Link`` header → sidecar ``X-Next-Cursor``):
#     ``_parse_link_next_cursor`` / ``_extract_before_verbatim`` slice the
#     raw query substring with NO decoding — so the cursor is byte-for-byte
#     verbatim for ANY input, percent-encoded or otherwise. Asymmetric by
#     design: outbound is always opaque.
#
#   • Inbound (client ``?before`` → sidecar → opencode wire): FastAPI's
#     query-param binding percent-decodes the value (``%2B``→``+``,
#     ``%20``→space, ``+``→space, ``%41``→``A``), and httpx then re-encodes
#     via ``quote_plus``. On the base64url charset this round-trip is a
#     no-op (no ``%``, no ``+``, no whitespace to canonicalise), so opencode
#     receives exactly what the client sent. On percent-encoded forms
#     OUTSIDE base64url, the round-trip NORMALISES to canonical form
#     (e.g. ``%2b`` → ``%2B``, ``%20`` → ``+``, ``%41`` → ``A``).
#
# This is the user's round-3 decision: base64url is opencode's documented
# cursor format and is safe end-to-end. The tests below pin BOTH halves of
# that contract — the safe base64url case AND the normalisation behaviour
# for non-base64url inputs — as a regression net. If opencode ever adopts
# a cursor format containing percent-escapes or ``+``, the inbound path
# must be switched to ``request.scope["query_string"]`` raw bytes + manual
# upstream URL construction (bypassing httpx ``params=`` encoding). That
# follow-up is OUT OF SCOPE for this round.
# ===========================================================================

# Real opencode-style base64url cursor (JSON: {"id":"msg_123","time":1234567890}).
# Pure ``[A-Za-z0-9_]`` — no ``%``, ``+``, ``=``, or whitespace — so it sits
# in the safe fixed-point of the FastAPI decode + httpx re-encode pipeline.
_BASE64URL_CURSOR = "eyJpZCI6Im1zZ18xMjMiLCJ0aW1lIjoxMjM0NTY3ODkwfQ"


async def test_outbound_base64url_cursor_is_verbatim_on_list(upstream_factory):
    """Outbound, /messages list: opencode's base64url cursor in the Link
    header surfaces byte-for-byte as sidecar's X-Next-Cursor. The raw-slice
    parser never decodes, so any charset — including the safe base64url one
    opencode actually uses — round-trips exactly."""
    payload = orjson.dumps([_msg("m1", 100)])
    link = (
        f'<http://127.0.0.1:4096/session/s1/message'
        f'?before={_BASE64URL_CURSOR}&limit=40>; rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert response.headers.get("X-Next-Cursor") == _BASE64URL_CURSOR
    finally:
        app.state.transforms.shutdown()


async def test_outbound_base64url_cursor_is_verbatim_on_since(upstream_factory):
    """Outbound, /since/{ts}: same regression as the list variant — the
    multi-page scan's cursor emission path also surfaces the base64url
    cursor byte-for-byte."""
    page = [_msg("m1", 200)]
    link = (
        f'<http://127.0.0.1:4096/session/s1/message'
        f'?before={_BASE64URL_CURSOR}&limit=1>; rel="next"'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(page),
            headers={"Content-Type": "application/json", "Link": link},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/since/100?limit=1&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert response.headers.get("X-Next-Cursor") == _BASE64URL_CURSOR
    finally:
        app.state.transforms.shutdown()


async def test_inbound_base64url_cursor_round_trips_byte_for_byte(upstream_factory):
    """Inbound (the user's actual concern): client passes a real opencode
    base64url cursor as ?before; sidecar forwards it to opencode byte-for-
    byte on the wire. The FastAPI decode + httpx re-encode chain is a no-op
    on the base64url charset (no ``%``/``+``/whitespace to canonicalise) —
    this test is the proof. Asserted via the RAW upstream query bytes so a
    future encoder swap can't hide a regression."""
    payload = orjson.dumps([_msg("m1", 100)])
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_query"] = request.url.query
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/slimapi/messages/s1?before={_BASE64URL_CURSOR}&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Byte-for-byte wire equality — base64url is the fixed point of the
        # FastAPI decode + httpx re-encode pipeline, so the upstream query
        # contains the cursor unchanged.
        assert b"before=" + _BASE64URL_CURSOR.encode() in captured["raw_query"], (
            f"upstream wire query = {captured['raw_query']!r}; "
            f"expected base64url cursor byte-for-byte"
        )
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize(
    ("non_canonical_input", "expected_upstream_form", "description"),
    [
        # %2b (lowercase hex) → FastAPI decodes to "+", httpx re-encodes to
        # canonical uppercase %2B. Hex case is normalised away.
        ("%2b", "%2B", "lowercase hex percent-encoding normalised to uppercase"),
        # %41 → "A" (alphanumeric, unreserved per RFC 3986 — never re-encoded).
        # The encoded form is replaced by the literal char.
        ("%41", "A", "encoded unreserved char normalised to literal"),
        # %20 → space → "+" (form-query convention from quote_plus). Note the
        # asymmetry: space encodes as ``+``, not ``%20``, on the wire to
        # opencode — clients expecting %20 will see ``+`` instead.
        ("%20", "+", "encoded space normalised to form-query '+'"),
    ],
    ids=["lowercase-hex-2b", "encoded-unreserved-41", "encoded-space-20"],
)
async def test_inbound_non_base64url_cursor_is_normalised(
    upstream_factory,
    non_canonical_input: str,
    expected_upstream_form: str,
    description: str,
):
    """Characterization (NOT a contract): non-base64url cursors carrying
    percent-escapes get NORMALISED by the FastAPI decode + httpx re-encode
    pipeline. This locks the actual normalisation behaviour per sample so a
    future change to either library surfaces visibly.

    IMPORTANT: this is a documented EDGE, not a bug. opencode's real cursor
    format is base64url (see ``test_inbound_base64url_cursor_round_trips_byte_for_byte``
    for the proof that the safe path is byte-for-byte). These samples
    characterise what would happen IF opencode ever emitted a non-base64url
    cursor — the answer is "normalised, not verbatim" — and the follow-up
    fix is documented in the module docstring above (read raw ASGI
    query_string, bypass httpx params encoding)."""
    payload = orjson.dumps([_msg("m1", 100)])
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_query"] = request.url.query
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/slimapi/messages/s1?before={non_canonical_input}&mode=skeleton",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # If this assertion fails, FastAPI or httpx changed its encoding
        # behaviour. UPDATE ``expected_upstream_form`` to the newly observed
        # value — this is a characterization lock, not a wire contract.
        assert b"before=" + expected_upstream_form.encode() in captured["raw_query"], (
            f"{description}: input {non_canonical_input!r} → expected "
            f"upstream {expected_upstream_form!r}, got raw query "
            f"{captured['raw_query']!r}"
        )
        # Sanity-lock the divergence: input MUST NOT round-trip unchanged.
        # If it did, this sample would not characterise the normalisation
        # edge — pick a non-fixed-point input.
        assert non_canonical_input != expected_upstream_form, (
            f"sample {non_canonical_input!r} round-trips unchanged — it does "
            "not characterise the normalisation edge; choose a non-fixed-point"
            " input"
        )
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# Follow-up ① (round-3 review): mid-page skip regression lock.
# ===========================================================================
#
# INVARIANT: opencode honours ``?limit=`` as the max items per response page
# AND emits a ``Link: ...; rel="next"`` header only when more pages exist
# (omits it on the final, partial page). Under this invariant, sidecar's
# pagination is gap-free across batches: each opencode page either fills
# the client's limit exactly (cursor emitted = page's Link, which points
# past the last item returned) or returns fewer items without a Link
# (sidecar stops, no cursor). The two outcomes align the emission boundary
# with the page boundary, so the client's next ``?before=<X-Next-Cursor>``
# picks up at the strict-older neighbour of the last item it received.
#
# Sidecar's INTERNAL multi-page scan within a single request only triggers
# if opencode violates the invariant (returns a partial page with a Link).
# In that violation case, sidecar's cursor logic has a known limitation:
# the emitted cursor points past the upstream page's last item, NOT past
# the last item returned to the client — which can skip items if the
# violation happens mid-page. That edge case is OUT OF SCOPE here because
# it requires opencode to break its documented contract.
# ===========================================================================


async def test_messages_since_multi_batch_pagination_has_no_gap_under_opencode_limit_invariant(
    upstream_factory,
):
    """Mid-page skip regression lock (round-3 follow-up ①).

    Under opencode's documented ``?limit=`` contract, sidecar's pagination
    chains batches gap-free. This test simulates 10 items all matching
    ts=0 with client limit=4; three batches walk all 10 contiguously and
    each cursor lets the next batch pick up exactly where the prior ended.

    NOTE: sidecar's internal multi-page scan is NOT exercised here — it
    only fires on opencode contract violation (partial page + Link).
    """
    # 10 items all pass ts=0. m1.updated=999, m2.updated=998, ... m10.updated=990.
    all_items = [_msg(f"m{i}", 1000 - i) for i in range(1, 11)]
    # opencode-style opaque cursors mapping to slice offsets. Opaque string
    # values prove sidecar doesn't synthesise a cursor from a messageID.
    cursor_to_start = {
        None: 0,
        "opaqueAfterM4": 4,
        "opaqueAfterM8": 8,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        before = request.url.params.get("before")
        limit = int(request.url.params.get("limit", "40"))
        slice_start = cursor_to_start.get(before, len(all_items))
        page = all_items[slice_start:slice_start + limit]
        headers = {"Content-Type": "application/json"}
        # opencode honours ?limit= as max page size AND only emits Link when
        # more pages exist. This is the invariant under test.
        if slice_start + limit < len(all_items):
            next_start = slice_start + limit
            next_cursor = f"opaqueAfterM{next_start}"
            headers["Link"] = (
                f'<http://127.0.0.1:4096/session/s1/message'
                f'?before={next_cursor}&limit={limit}>; rel="next"'
            )
        return httpx.Response(200, content=orjson.dumps(page), headers=headers)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Batch 1: no before → m1..m4 + cursor to m4.
            r1 = await client.get(
                "/slimapi/messages/s1/since/0?limit=4&mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert r1.status_code == 200
            body1 = orjson.loads(r1.content)
            assert [i["info"]["id"] for i in body1] == ["m1", "m2", "m3", "m4"]
            # Cursor is opencode's opaque string — NOT a synthesised messageID
            # (round-1 regression defense baked into the no-gap lock).
            assert r1.headers.get("X-Next-Cursor") == "opaqueAfterM4"
            assert r1.headers.get("X-Next-Cursor") not in (
                "m1", "m2", "m3", "m4",
            )

            # Batch 2: client passes the opaque cursor verbatim → m5..m8.
            # If cursor pointed anywhere other than past m4, this would
            # either miss items or duplicate them.
            r2 = await client.get(
                "/slimapi/messages/s1/since/0?limit=4&before=opaqueAfterM4&mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert r2.status_code == 200
            body2 = orjson.loads(r2.content)
            assert [i["info"]["id"] for i in body2] == ["m5", "m6", "m7", "m8"]
            assert r2.headers.get("X-Next-Cursor") == "opaqueAfterM8"

            # Batch 3: tail — under limit, opencode emits no Link → no cursor.
            r3 = await client.get(
                "/slimapi/messages/s1/since/0?limit=4&before=opaqueAfterM8&mode=skeleton",
                headers=VERSION_HEADERS,
            )
            assert r3.status_code == 200
            body3 = orjson.loads(r3.content)
            assert [i["info"]["id"] for i in body3] == ["m9", "m10"]
            assert "X-Next-Cursor" not in r3.headers

            # All 10 items returned across 3 batches, contiguous, no gap/dup.
            seen = (
                [i["info"]["id"] for i in body1]
                + [i["info"]["id"] for i in body2]
                + [i["info"]["id"] for i in body3]
            )
            assert seen == [f"m{i}" for i in range(1, 11)], (
                f"pagination lost or duplicated items: {seen}"
            )
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# Follow-up ② (round-3 review): Link parser hardening unit tests.
# ===========================================================================


def test_parse_link_next_cursor_real_opencode_shape():
    """Lock the parser against drift on opencode's actual emitted Link
    format: base64url-style cursor, ``rel="next"``, full URL with limit."""
    from oc_slimapi.routes.messages import _parse_link_next_cursor

    link = (
        '<http://127.0.0.1:4096/session/sid/message'
        '?before=eyJpZ18xMjM&limit=50>; rel="next"'
    )
    # Cursor extracted verbatim — base64url chars untouched.
    assert _parse_link_next_cursor(link) == "eyJpZ18xMjM"


def test_parse_link_next_cursor_handles_multi_token_rel():
    """RFC 5988 allows multiple relation types separated by whitespace
    (``rel="prev next"``). Parser must still recognise the entry as a next
    link when ``next`` appears as any token."""
    from oc_slimapi.routes.messages import _parse_link_next_cursor

    link = '<http://x/y?before=ABCopaque>; rel="prev next"'
    assert _parse_link_next_cursor(link) == "ABCopaque"

    # Reverse order also works.
    link_reversed = '<http://x/y?before=ABCopaque>; rel="next prev"'
    assert _parse_link_next_cursor(link_reversed) == "ABCopaque"


def test_parse_link_next_cursor_rel_match_is_case_insensitive():
    """RFC 5988 §3: relation types are case-insensitive tokens. The param
    name ``rel`` and the token ``next`` both match in any case."""
    from oc_slimapi.routes.messages import _parse_link_next_cursor

    # Mixed-case token.
    assert _parse_link_next_cursor('<http://x/y?before=ABC>; rel="Next"') == "ABC"
    assert _parse_link_next_cursor('<http://x/y?before=ABC>; rel="NEXT"') == "ABC"
    # Mixed-case param name.
    assert _parse_link_next_cursor('<http://x/y?before=ABC>; REL="next"') == "ABC"
    assert _parse_link_next_cursor('<http://x/y?before=ABC>; Rel="NEXT"') == "ABC"
    # Multi-token with mixed case.
    assert (
        _parse_link_next_cursor('<http://x/y?before=ABC>; rel="Prev NEXT"') == "ABC"
    )


def test_parse_link_next_cursor_does_not_match_rel_inside_other_param_value():
    """Defense: a ``rel=next`` substring tucked inside another param's
    quoted value (``title="rel=next"``) MUST NOT fool the parser into
    treating the entry as a next link."""
    from oc_slimapi.routes.messages import _parse_link_next_cursor

    link = '<http://x/y?before=ABC>; title="rel=next"; rel="prev"'
    # Real rel is "prev", so this is NOT a next link → no cursor extracted.
    assert _parse_link_next_cursor(link) is None


async def test_messages_list_unknown_directory_passes_through(upstream_factory):
    """slimapi no longer gates directories — ``?directory=/nope`` is forwarded
    to upstream opencode normalised as ``X-Opencode-Directory``. Previously
    this returned ``400 directory_not_allowed``; the allowlist gate is
    removed and opencode now decides whether it can serve the directory.

    Handler returns an empty message list (200), which slimapi surfaces
    verbatim; we additionally assert the directory was forwarded.
    """
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?directory=/nope",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Directory normalised (no trailing slash) and forwarded to upstream.
        assert captured["dir"] == "/nope"
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_query_header_conflict_400(upstream_factory):
    """G7-soft: query directory ≠ X-Opencode-Directory header → 400 even if both allowed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    app.state.directory_allowlist = {"/app", "/other"}  # both allowed; conflict still 400
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?directory=/app",
                headers={**VERSION_HEADERS, "X-Opencode-Directory": "/other"},
            )
        assert response.status_code == 400
        assert response.json()["code"] == "directory_not_allowed"
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_no_directory_passes(upstream_factory):
    """G7-soft: no query directory → not blocked."""
    payload = orjson.dumps([])
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
        assert response.status_code == 200
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize("path", ["/slimapi/messages/s1/since/1", "/slimapi/messages/s1/full/m1"])
async def test_messages_since_and_full_unknown_directory_passes_through(upstream_factory, path):
    """slimapi no longer gates directories — applies uniformly to /since and
    /full/{mid}. ``?directory=/nope`` is forwarded normalised; allowlist gate
    (formerly 400 ``directory_not_allowed``) removed."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        # Return a shape that both /since (array) and /full/{mid} (object) accept.
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"{path}?directory=/nope", headers=VERSION_HEADERS)
        # Either 200 (since returns [], or full/{mid} drains upstream body) —
        # both indicate slimapi forwarded to upstream rather than 400'ing.
        assert response.status_code in (200, 404)
        assert captured["dir"] == "/nope"
    finally:
        app.state.transforms.shutdown()


async def test_full_message_413_oversized(upstream_factory):
    """G8: full-mode caps at max_message_bytes via streaming, not buffering."""
    cap = 4 * 1024
    oversized = b"x" * (cap * 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    upstream = upstream_factory(handler)
    settings = _settings(max_message_bytes=cap)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS)
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "message_too_large"
        assert body["limitBytes"] == cap
    finally:
        app.state.transforms.shutdown()


# NOTE: T4-C2 (streaming, no full buffer) and T4-C3 (aclose anti-leak) are NOT
# unit-testable — httpx.MockTransport materialises the full response content
# eagerly in the handler, so no in-test observable proves the sidecar stopped
# reading early. Both are locked by code review at the final gate:
#   • full-mode branch must use read_with_cap (aiter_bytes), NOT response.content/aread
#   • full-mode branch must wrap the response in try/finally: await response.aclose()


async def test_full_message_under_cap_passthrough(upstream_factory):
    payload = orjson.dumps({"info": {"id": "m1"}, "parts": []})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["info"] == {"id": "m1"}
    finally:
        app.state.transforms.shutdown()


async def test_full_message_upstream_error_passthrough(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"missing"}', headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS)
        assert response.status_code == 404
        # Body and content-type pass through verbatim (thin-route contract).
        assert response.content == b'{"error":"missing"}'
        assert response.headers["Content-Type"] == "application/json"
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# F5e / F5f (B1 review hardening): G7-soft positive + query/header conflict
# on /since and /full. The existing /list conflict test already locks the
# conflict path on the listing endpoint; these add the symmetric coverage
# on /since and /full, plus a positive (allowed) case proving the directory
# is normalised and forwarded.
# ===========================================================================


async def test_messages_list_allowed_directory_forwarded_normalized(upstream_factory):
    """G7-soft positive (T3-C1): allowed query directory passes AND is
    forwarded upstream normalised — ``?directory=/app/`` (trailing slash)
    must reach upstream as ``X-Opencode-Directory: /app`` (no trailing
    slash). ``require_directory`` normalises via ``rstrip("/")``."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    app.state.directory_allowlist = {"/app"}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?directory=/app/", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Normalised: trailing slash stripped before forwarding.
        assert captured["dir"] == "/app"
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize(
    "path",
    ["/slimapi/messages/s1/since/1", "/slimapi/messages/s1/full/m1"],
)
async def test_messages_since_and_full_query_header_conflict_400(upstream_factory, path):
    """G7-soft: query ``directory`` conflicting with ``X-Opencode-Directory``
    header → 400 ``directory_not_allowed`` on /since and /full too (not just
    /list). Both directories are in the allowlist, so this isolates the
    conflict check from the allowlist check."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    app.state.directory_allowlist = {"/app", "/other"}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"{path}?directory=/app",
                headers={**VERSION_HEADERS, "X-Opencode-Directory": "/other"},
            )
        assert response.status_code == 400
        assert response.json()["code"] == "directory_not_allowed"
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# G6 — GET /slimapi/messages/{sid}/full?ids= batch multi-mid expand
# ===========================================================================


async def test_g6_ids_missing_returns_422(upstream_factory):
    """T7-C1: missing ``ids`` → FastAPI 422 validation error."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full", headers=VERSION_HEADERS)
        assert response.status_code == 422
    finally:
        app.state.transforms.shutdown()


async def test_g6_ids_invalid_count(upstream_factory):
    """T7-C2: empty / >20 ids → 400 ``invalid_ids``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 空（仅逗号/空白）
            r1 = await client.get(
                "/slimapi/messages/s1/full?ids=,,", headers=VERSION_HEADERS,
            )
            assert r1.status_code == 400 and r1.json()["code"] == "invalid_ids"
            # >20
            big = ",".join(f"m{i}" for i in range(21))
            r2 = await client.get(
                f"/slimapi/messages/s1/full?ids={big}", headers=VERSION_HEADERS,
            )
            assert r2.status_code == 400 and r2.json()["code"] == "invalid_ids"
    finally:
        app.state.transforms.shutdown()


async def test_g6_session_not_found_no_mid_fetch(upstream_factory):
    """T7-C3: discover 404 → 404 ``session_not_found``, zero mid fetches."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, content=b'{"error":"no session"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS,
            )
        assert r.status_code == 404 and r.json()["code"] == "session_not_found"
        assert calls["count"] == 1  # 只 discover，没拉 mid
    finally:
        app.state.transforms.shutdown()


async def test_g6_partial_mid_failure(upstream_factory):
    """T7-C4: partial mid 404 → 200 + items[] + errors[] message_not_found."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        if request.url.path == "/session/s1/message/m_ok":
            return httpx.Response(200, content=orjson.dumps(_msg("m_ok", 100)))
        if request.url.path == "/session/s1/message/m_missing":
            return httpx.Response(404, content=b"{}")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m_ok,m_missing",
                headers=VERSION_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert any(
            e["code"] == "message_not_found" and e["messageID"] == "m_missing"
            for e in body["errors"]
        )
    finally:
        app.state.transforms.shutdown()


async def test_g6_all_mid_missing_still_200(upstream_factory):
    """T7-C5: all mid 404 → still 200 + all errors[] (no whole-response fail)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert len(body["errors"]) == 2
    finally:
        app.state.transforms.shutdown()


async def test_g6_cumulative_byte_budget(upstream_factory):
    """T7-C6: concurrent chunk-ledger cumulative budget → 413.

    8 mids, each ~40KiB, max_response_bytes=64KiB. A barrier holds the first
    4 streams until all 4 sem slots are occupied (max_in_flight==4). After the
    first wave's chunks trip the shared ledger, mids 5–8 must NOT be fetched
    (aborted blocks queue). Proves TOCTOU fix: debit is per-chunk, not after
    full body.
    """
    cap = 64 * 1024
    payload = orjson.dumps(_msg("m", 100, text="y" * 40000))
    assert len(payload) < cap
    assert 2 * len(payload) > cap

    discover_calls = {"n": 0}
    mid_calls = {"n": 0}
    in_flight = {"n": 0, "max": 0}
    barrier_ready = asyncio.Event()
    barrier_count = {"n": 0}
    # First 4 mid streams wait here before yielding any body bytes so all 4
    # concurrent slots are held before the ledger is debited.
    first_wave_size = 4

    class BarrierStream(httpx.AsyncByteStream):
        def __init__(self, data: bytes, *, join_barrier: bool):
            self._data = data
            self._join = join_barrier

        async def __aiter__(self):
            if self._join:
                barrier_count["n"] += 1
                if barrier_count["n"] >= first_wave_size:
                    barrier_ready.set()
                await barrier_ready.wait()
            # Yield in small chunks so the ledger can trip mid-stream.
            chunk = 8 * 1024
            for i in range(0, len(self._data), chunk):
                yield self._data[i:i + chunk]
                # Yield to the event loop so sibling streams interleave.
                await asyncio.sleep(0)

        async def aclose(self):
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            discover_calls["n"] += 1
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        mid_calls["n"] += 1
        in_flight["n"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["n"])
        # First 4 mids join the barrier; later ones (if any slip through)
        # stream immediately — they should not exist under a correct abort.
        join = mid_calls["n"] <= first_wave_size
        # Track in_flight release via a wrapper that decrements on aclose.
        stream = BarrierStream(payload, join_barrier=join)

        class CountingStream(httpx.AsyncByteStream):
            def __init__(self, inner: httpx.AsyncByteStream):
                self._inner = inner

            async def __aiter__(self):
                async for c in self._inner:
                    yield c

            async def aclose(self):
                in_flight["n"] -= 1
                await self._inner.aclose()

        return httpx.Response(200, stream=CountingStream(stream))

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=cap), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = ",".join(f"m{i}" for i in range(1, 9))
            r = await client.get(
                f"/slimapi/messages/s1/full?ids={ids}", headers=VERSION_HEADERS,
            )
        assert r.status_code == 413
        body = r.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
        assert discover_calls["n"] == 1
        # Initial 4 start; queued 5–8 never fire once budget_exceeded trips.
        assert mid_calls["n"] == 4, (
            f"expected 4 mid fetches (sem wave only), got {mid_calls['n']}"
        )
        assert in_flight["max"] == 4
    finally:
        app.state.transforms.shutdown()


async def test_g6_no_under_fetch_small_messages(upstream_factory):
    """Anti-regression: 20 small mids must all succeed under a generous cap."""
    mid_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        mid_calls["n"] += 1
        mid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200, content=orjson.dumps(_msg(mid, 100, text="x" * 900)),
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=64 * 1024), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = ",".join(f"m{i}" for i in range(20))
            r = await client.get(
                f"/slimapi/messages/s1/full?ids={ids}", headers=VERSION_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 20
        assert body["errors"] == []
        assert mid_calls["n"] == 20
    finally:
        app.state.transforms.shutdown()


async def test_g6_message_too_large_charges_budget(upstream_factory):
    """Rev-5: per-mid too-large must still debit the shared ledger.

    Deterministic order (m2 stream waits for m1 aclose):
      m1: chunk0 (16KiB) accepted; chunk1 charges then trips
      message_too_large → charged = 32KiB. m2 (~12KiB) then trips
      budget (32+12 > 40) → whole-response 413.

    Old code skipped debit on the per-mid-triggering chunk → charged
    only 16KiB → 16+12 < 40 → 200 + envelope (the bug this locks).
    """
    # BATCH_CHUNK_SIZE = 16KiB. max_msg between 1 and 2 chunks so the
    # second chunk is the per-mid trigger; max_resp > 2 chunks so that
    # second chunk hits per-mid (not budget) and still charges.
    max_msg = 20 * 1024
    max_resp = 40 * 1024
    big_payload = orjson.dumps(_msg("m1", 100, text="B" * 40_000))  # ~2.5 chunks
    small_payload = orjson.dumps(_msg("m2", 100, text="s" * 11_000))  # ~12KiB
    assert len(big_payload) > max_msg
    assert len(small_payload) < max_msg
    assert len(small_payload) < max_resp
    charged_m1_with_fix = 32 * 1024  # two BATCH_CHUNK debits
    charged_m1_old_bug = 16 * 1024     # only first chunk if trigger not debited
    assert charged_m1_with_fix + len(small_payload) > max_resp
    assert charged_m1_old_bug + len(small_payload) <= max_resp

    m1_finished = asyncio.Event()

    class OrderedStream(httpx.AsyncByteStream):
        def __init__(self, data: bytes, *, is_m1: bool):
            self._data = data
            self._is_m1 = is_m1

        async def __aiter__(self):
            if not self._is_m1:
                await m1_finished.wait()
            chunk = 16 * 1024  # match BATCH_CHUNK_SIZE for predictable debit
            for i in range(0, len(self._data), chunk):
                yield self._data[i:i + chunk]
                await asyncio.sleep(0)

        async def aclose(self):
            if self._is_m1:
                m1_finished.set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        if request.url.path.endswith("/m1"):
            return httpx.Response(200, stream=OrderedStream(big_payload, is_m1=True))
        if request.url.path.endswith("/m2"):
            return httpx.Response(200, stream=OrderedStream(small_payload, is_m1=False))
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(max_message_bytes=max_msg, max_response_bytes=max_resp),
        upstream,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS,
            )
        # m1 charged 16KiB then message_too_large; m2 then trips budget → 413.
        # Without the debit, m2 would fit under max_resp and the route would
        # return 200 with items=[m2] + errors=[m1 too_large].
        assert r.status_code == 413, (
            f"expected 413 from ledger charge of too-large mid, got "
            f"{r.status_code}: {r.text[:200]}"
        )
        assert r.json()["code"] == "response_too_large"
        assert r.json()["limit"] == max_resp
    finally:
        app.state.transforms.shutdown()


async def test_g6_mid_malformed_json_envelope_error(upstream_factory):
    """Mid 200 + illegal JSON body → errors[] upstream_error; whole request 200."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        if request.url.path.endswith("/m1"):
            return httpx.Response(200, content=b"not json{")
        if request.url.path.endswith("/m2"):
            return httpx.Response(200, content=orjson.dumps(_msg("m2", 100)))
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["info"]["id"] == "m2"
        assert any(
            e["messageID"] == "m1" and e["code"] == "upstream_error"
            for e in body["errors"]
        )
    finally:
        app.state.transforms.shutdown()


async def test_g6_network_error_returns_503(upstream_factory):
    """Network-layer httpx.RequestError on a mid stream → 503, never 413."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        if request.url.path.endswith("/m_bad"):
            raise httpx.ConnectError("simulated mid stream failure")
        mid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=orjson.dumps(_msg(mid, 100)))

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m_ok,m_bad",
                headers=VERSION_HEADERS,
            )
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"
        assert r.status_code != 413
    finally:
        app.state.transforms.shutdown()


async def test_g6_discover_malformed_json_returns_503(upstream_factory):
    """Discover 200 + illegal JSON → 503; zero mid fetches."""
    mid_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=b"not json{")
        mid_calls["n"] += 1
        return httpx.Response(200, content=orjson.dumps(_msg("m1", 100)))

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS,
            )
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"
        assert mid_calls["n"] == 0
    finally:
        app.state.transforms.shutdown()


async def test_g6_items_strict_order(upstream_factory):
    """T7-C7: items[] strict order of deduped ids (dict.fromkeys order)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        mid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=orjson.dumps(_msg(mid, 100)))

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m3,m1,m3,m2",
                headers=VERSION_HEADERS,
            )
        body = r.json()
        ids = [m["info"]["id"] for m in body["items"]]
        assert ids == ["m3", "m1", "m2"]  # 去重保序
    finally:
        app.state.transforms.shutdown()


async def test_g6_route_not_shadowed(upstream_factory):
    """T7-C8: GET /full?ids= is not swallowed by /full/{mid}.

    Runtime check: request hits batch envelope. Static check: router
    registration order places ``/full`` before ``/full/{mid}``.
    """
    from fastapi.routing import APIRoute

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        return httpx.Response(200, content=orjson.dumps(_msg("m1", 100)))

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/slimapi/messages/s1/full?ids=m1", headers=VERSION_HEADERS,
            )
        # 若被 /full/{mid} 吞，会 422（{mid} 缺）或不同行为；这里断言 200 + envelope
        assert r.status_code == 200
        assert "items" in r.json()

        # Static registration order (spec MUST): /full before /full/{mid}.
        paths = [
            route.path for route in app.router.routes
            if isinstance(route, APIRoute)
        ]
        # Also check the messages router directly (app may wrap routes).
        from oc_slimapi.routes import messages as msgs_mod
        msg_paths = [
            route.path for route in msgs_mod.router.routes
            if isinstance(route, APIRoute)
        ]
        full_idx = next(
            i for i, p in enumerate(msg_paths)
            if p.endswith("/full") and "{mid}" not in p
        )
        full_mid_idx = next(
            i for i, p in enumerate(msg_paths) if p.endswith("/full/{mid}")
        )
        assert full_idx < full_mid_idx, (
            f"/full must register before /full/{{mid}}: {msg_paths}"
        )
    finally:
        app.state.transforms.shutdown()
