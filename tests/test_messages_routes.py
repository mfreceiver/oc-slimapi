"""Route-level integration tests for ``routes/messages.py`` (lite-v2).

lite-v2 scope: the sidecar's messages router was simplified to two endpoints:

* ``GET /slimapi/messages/{sid}`` — skeleton projection (sorted ASC by
  ``info.time.created`` per §8). ``?mode`` is ignored (was ``skeleton|full``).
* ``GET /slimapi/messages/{sid}/full/{mid}`` — single-message on-demand
  expand, full projection (strip LSP diagnostics). No 304, no ``?known.*``
  short-circuit, no ``X-Message-Event-Seq`` header. ``?mode`` is ignored.

Removed endpoints (return 404 because the handlers are unregistered):

* ``GET /slimapi/messages/{sid}/since/{ts}``  — incremental sync
* ``GET /slimapi/messages/{sid}/full?ids=``    — batch multi-mid expand

These tests exercise the surviving wire contract end-to-end through a mocked
upstream. The app is constructed fresh per test (bypassing the module-level
lifespan) so we can dial down transform-pool knobs without touching env vars.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.config import settings as _cfg_settings
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        route_secret="x" * 32,
        route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
        # Opt-A fields are still defined on Settings; kept here so the dataclass
        # validates even though messages.py no longer consumes them.
        opt_a_partial_envelope_enabled=True,
        opt_a_auto_rollback_enabled=True,
        opt_a_rollback_window_seconds=3600,
        opt_a_rollback_min_sample=100,
        opt_a_rollback_envelope_5xx_zero_baseline_rate=0.01,
        opt_a_rollback_unknown_code_rate=0.05,
        opt_a_retry_after_ms_conservative=200,
        opt_a_retry_after_ms_cap=10000,
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
    app.state.allowlist_ready = False
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    for router in (health.router, sessions.router, messages.router, events.router):
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
                        "output": "x" * (_cfg_settings.skeleton_inline_output_max_bytes + 1000),
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


# ---------------------------------------------------------------------------
# Skeleton list endpoint — projection, pagination, errors.
# ---------------------------------------------------------------------------

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
                "/slimapi/messages/s1/full/m1",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 503
        assert response.json()["code"] == "transform_busy"


def _msg(mid: str, updated: int | None, *, text: str = "x" * 200) -> dict:
    """Build an upstream-shape message with ``info.time.updated`` set.

    When ``updated`` is None the ``info.time`` block is omitted entirely.
    """
    info: dict = {"id": mid, "role": "user"}
    if updated is not None:
        info["time"] = {"updated": updated, "created": updated}
    return {
        "info": info,
        "parts": [{"id": f"p-{mid}", "type": "text", "messageID": mid, "text": text}],
    }


async def test_health_stays_responsive_during_slow_transform(app_and_client, monkeypatch):
    """The headline fix: a slow transform in a worker thread must not block
    /slimapi/health. We monkey-patch the worker entrypoint to add a synthetic
    delay and measure health latency while the skeleton request is in flight."""
    app, _ = app_and_client

    import oc_slimapi.routes.messages as msgs_mod

    original_pack = msgs_mod._project_list_sorted_and_pack
    slow_packs_started = asyncio.Event()

    def slow_pack(body, *, accept_encoding):
        # Signal that the worker has picked up the job, then park it.
        slow_packs_started.set()
        time.sleep(0.5)
        return original_pack(body, accept_encoding=accept_encoding)

    monkeypatch.setattr(msgs_mod, "_project_list_sorted_and_pack", slow_pack)

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
# Contract §9: every JSON route (including 413 error paths) must honour
# client ``Accept-Encoding: gzip``.
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
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        body = orjson.loads(response.content)
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


async def test_full_message_413_negotiates_gzip(upstream_factory):
    """/full/{mid} 413 (message_too_large) compresses the error body when
    the client sends Accept-Encoding: gzip."""
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
                "/slimapi/messages/s1/full/m1",
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
# Empty path "" under the router prefix must hit directly (no 307).
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
# /full/{mid} — single-message on-demand expand (full projection).
# ---------------------------------------------------------------------------

async def test_full_message_default_mode_strips_diagnostics_preserves_rest(upstream_factory):
    """GET /slimapi/messages/s1/full/m1 always uses full projection: the
    complete part is preserved (no skeleton projection — debug input + tool
    output + metadata siblings all kept) EXCEPT the never-consumed LSP
    ``state.metadata.diagnostics`` map, which is stripped server-side."""
    payload = orjson.dumps({
        "info": {"id": "m1", "role": "user"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
            {
                "id": "p2", "type": "tool", "messageID": "m1", "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls", "debug": "skeleton would drop me"},
                    "metadata": {
                        "sessionId": "s1",
                        "description": "ran ls",
                        "diagnostics": [{"severity": 1, "message": "unused"}],
                    },
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
        tool_part = body["parts"][1]
        # diagnostics stripped ...
        assert "diagnostics" not in tool_part["state"]["metadata"]
        # ... but metadata siblings + debug input + full tool output kept.
        assert tool_part["state"]["metadata"] == {"sessionId": "s1", "description": "ran ls"}
        assert tool_part["state"]["input"] == {
            "command": "ls", "debug": "skeleton would drop me",
        }
        assert "output" in tool_part["state"]
    finally:
        app.state.transforms.shutdown()


async def test_full_single_returns_transform_busy_with_no_upstream_get(upstream_factory):
    """Admission is acquired BEFORE the upstream GET: with the pool saturated,
    /full/{mid} returns 503 transform_busy and never hits upstream."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"{}")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    pool = app.state.transforms
    try:
        async with pool:  # saturate the single admission slot
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS,
                )
            assert response.status_code == 503
            assert response.json()["code"] == "transform_busy"
            assert response.headers["Retry-After"] == "2"
        # admission-before-GET → zero upstream calls.
        assert calls["n"] == 0
    finally:
        app.state.transforms.shutdown()


async def test_full_single_wrong_shape_2xx_served(upstream_factory):
    """A malformed-shape 200 body (non-dict) is served as-is — the strip is a
    no-op on shapes it can't scrub, matching prior verbatim passthrough (no 500)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/slimapi/messages/s1/full/m1", b""),
        ("/slimapi/messages/s1/full/m1", b"not-json{{{"),
    ],
    ids=["single-empty", "single-garbage"],
)
async def test_full_invalid_json_returns_503_upstream_unavailable(
    upstream_factory, path, body,
):
    """Upstream 200 with empty / non-JSON body must not escape as a bare 500.

    ``strip_diagnostics_and_pack`` raises ``orjson.JSONDecodeError``; the full
    branch maps it to 503 ``upstream_unavailable`` (same code as sessions
    bad-JSON handling).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(path, headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json() == {"code": "upstream_unavailable"}
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


# ---------------------------------------------------------------------------
# Cursor passthrough (Q1/Q2): sidecar translates opencode's RFC 5988
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
# for the real opencode format.
# ===========================================================================

# Real opencode-style base64url cursor (JSON: {"id":"msg_123","time":1234567890}).
_BASE64URL_CURSOR = "eyJpZCI6Im1zZ18xMjMiLCJ0aW1lIjoxMjM0NTY3ODkwfQ"


async def test_outbound_base64url_cursor_is_verbatim_on_list(upstream_factory):
    """Outbound, /messages list: opencode's base64url cursor in the Link
    header surfaces byte-for-byte as sidecar's X-Next-Cursor."""
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


async def test_inbound_base64url_cursor_round_trips_byte_for_byte(upstream_factory):
    """Inbound: client passes a real opencode base64url cursor as ?before;
    sidecar forwards it to opencode byte-for-byte on the wire."""
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
        assert b"before=" + _BASE64URL_CURSOR.encode() in captured["raw_query"], (
            f"upstream wire query = {captured['raw_query']!r}; "
            f"expected base64url cursor byte-for-byte"
        )
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize(
    ("non_canonical_input", "expected_upstream_form", "description"),
    [
        ("%2b", "%2B", "lowercase hex percent-encoding normalised to uppercase"),
        ("%41", "A", "encoded unreserved char normalised to literal"),
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
    pipeline. This locks the actual normalisation behaviour per sample."""
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
        assert b"before=" + expected_upstream_form.encode() in captured["raw_query"], (
            f"{description}: input {non_canonical_input!r} → expected "
            f"upstream {expected_upstream_form!r}, got raw query "
            f"{captured['raw_query']!r}"
        )
        assert non_canonical_input != expected_upstream_form, (
            f"sample {non_canonical_input!r} round-trips unchanged — it does "
            "not characterise the normalisation edge; choose a non-fixed-point"
            " input"
        )
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# Link parser hardening unit tests.
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


# ===========================================================================
# Directory handling (G7-soft).
# ===========================================================================


async def test_messages_list_unknown_directory_passes_through(upstream_factory):
    """slimapi no longer gates directories — ``?directory=/nope`` is forwarded
    to upstream opencode normalised as ``X-Opencode-Directory``."""
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


async def test_full_message_unknown_directory_passes_through(upstream_factory):
    """slimapi no longer gates directories — applies to /full/{mid} too.
    ``?directory=/nope`` is forwarded normalised; allowlist gate removed."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(
            200, content=orjson.dumps({"info": {"id": "m1"}, "parts": []}),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1?directory=/nope", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert captured["dir"] == "/nope"
    finally:
        app.state.transforms.shutdown()


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


async def test_full_message_query_header_conflict_400(upstream_factory):
    """G7-soft: query ``directory`` conflicting with ``X-Opencode-Directory``
    header → 400 ``directory_not_allowed`` on /full/{mid} too. Both
    directories are in the allowlist, so this isolates the conflict check
    from the allowlist check."""
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
                "/slimapi/messages/s1/full/m1?directory=/app",
                headers={**VERSION_HEADERS, "X-Opencode-Directory": "/other"},
            )
        assert response.status_code == 400
        assert response.json()["code"] == "directory_not_allowed"
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# lite-v2 §9.3 — new behaviour contracts.
# ===========================================================================


async def test_full_message_known_params_ignored_always_200_no_seq_header(upstream_factory):
    """lite-v2 §2 + §9.3: /full/{mid} no longer short-circuits on ``?known.*``
    fingerprint params. A client in transition sending them MUST NOT get a
    422 / 304 — the params are silently ignored, the response is always 200
    with a full body, and the deprecated ``X-Message-Event-Seq`` header is
    absent."""
    payload = orjson.dumps({"info": {"id": "m1", "role": "user"}, "parts": []})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1/full/m1"
                "?known.maxPartId=p3&known.partCount=2&known.messageEventSeq=42",
                headers=VERSION_HEADERS,
            )
        # Always 200 — no 304 short-circuit, no 422 for unknown params.
        assert response.status_code == 200
        # Deprecated header MUST NOT be emitted on the lite-v2 wire.
        assert "X-Message-Event-Seq" not in response.headers
        assert response.json()["info"] == {"id": "m1", "role": "user"}
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_mode_full_ignored_returns_skeleton_projection(upstream_factory):
    """lite-v2 §2 + §9.3: ``?mode=full`` list branch removed. Sending
    ``?mode=full`` MUST be silently tolerated and the response MUST be a
    skeleton projection (tool output dropped). The param is ignored, not
    rejected."""
    # Output intentionally LARGER than ``skeleton_inline_output_max_bytes``
    # so the skeleton projection drops it (small outputs would be inlined).
    big_output = "x" * (_cfg_settings.skeleton_inline_output_max_bytes + 1000)
    payload = orjson.dumps([{
        "info": {"id": "m1", "role": "user"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
            "state": {
                "status": "completed",
                "input": {"command": "ls", "debug": "skeleton drops me"},
                "output": big_output,
            },
        }],
    }])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=full", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # Skeleton projection applied — debug input + tool output dropped,
        # not the full-mode passthrough that used to keep them.
        state = body[0]["parts"][0]["state"]
        assert "output" not in state
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_skeleton_returns_created_ascending(upstream_factory):
    """lite-v2 §8 + §9.3: skeleton list endpoint MUST return messages
    sorted by ``info.time.created`` ASC. Sidecar sorts defensively — the
    test mocks upstream returning SHUFFLED created timestamps and asserts
    the response is strictly ascending. If this fails, the sidecar's
    sort contract is broken."""
    # Deliberately shuffled order: 3000, 1000, 2000.
    shuffled = [
        {
            "info": {"id": "m3", "role": "user", "time": {"created": 3000}},
            "parts": [{"id": "p3", "type": "text", "messageID": "m3", "text": "x" * 200}],
        },
        {
            "info": {"id": "m1", "role": "user", "time": {"created": 1000}},
            "parts": [{"id": "p1", "type": "text", "messageID": "m1", "text": "x" * 200}],
        },
        {
            "info": {"id": "m2", "role": "user", "time": {"created": 2000}},
            "parts": [{"id": "p2", "type": "text", "messageID": "m2", "text": "x" * 200}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        # Always return the shuffled page regardless of cursor — the test
        # only fires one request.
        return httpx.Response(
            200, content=orjson.dumps(shuffled),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = orjson.loads(response.content)
        createds = [m["info"]["time"]["created"] for m in body]
        # Strictly ascending — this is the §8 contract.
        assert createds == sorted(createds), (
            f"response not sorted ASC by time.created: {createds}"
        )
        # Concrete order pinned: m1 → m2 → m3 by created.
        assert [m["info"]["id"] for m in body] == ["m1", "m2", "m3"], body
        assert createds == [1000, 2000, 3000]
    finally:
        app.state.transforms.shutdown()


@pytest.mark.parametrize(
    ("path", "description"),
    [
        ("/slimapi/messages/s1/full?ids=m1,m2", "batch multi-mid expand (/full?ids=)"),
        ("/slimapi/messages/s1/since/100", "incremental sync (/since/{ts})"),
    ],
    ids=["full-ids", "since-ts"],
)
async def test_deleted_messages_endpoints_return_404(upstream_factory, path, description):
    """lite-v2 §1 + §9.3: removed endpoints return 404 because the handlers
    are no longer registered. Covers both ``/full?ids=`` (batch) and
    ``/since/{ts}`` (incremental sync) — the two endpoints deleted from
    messages.py per the lite-v2 plan."""
    # Handler should never be called: the route does not exist.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(path, headers=VERSION_HEADERS)
        # Unregistered route → FastAPI's default 404 (no handler matched).
        # Note: this is 404, NOT 405, because the path template itself is
        # gone — there is no method mismatch, the resource does not exist.
        assert response.status_code == 404, (
            f"{description}: expected 404 for deleted endpoint, got "
            f"{response.status_code}"
        )
    finally:
        app.state.transforms.shutdown()
