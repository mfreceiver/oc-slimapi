"""Route-level integration tests for ``routes/agent.py``.

``GET /slimapi/agent`` — skeleton projection of upstream opencode's agent
catalog. Keeps only the ocdroid-consumed whitelist (``name`` /
``description`` / ``mode`` / ``hidden`` / ``native``), dropping the dominant
``prompt`` (full system prompt) and ``permission`` (the
``Permission.Ruleset`` list — no UI consumer). Live-measured ~95.8% raw byte
saving. Mirrors the ``routes/messages.py`` stream+cap+transform-pool pattern.

These tests exercise the wire contract end-to-end through a mocked upstream
(MockTransport). The app is constructed fresh per test (bypassing the
module-level lifespan) so transform-pool knobs can be dialled down without
touching env vars.
"""
from __future__ import annotations

import gzip

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import agent, health
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

# Wire version 2 is the current contract revision; the middleware accepts
# exactly [2, 2].
VERSION_HEADERS = {"X-Slimapi-Version": "2"}

# The whitelist the skeleton must keep (mirrors AGENT_SKELETON_KEYS).
_AGENT_WHITELIST = {"name", "description", "mode", "hidden", "native"}


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


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app with the agent router wired up and
    ``app.state`` pre-populated, mirroring ``oc_slimapi.app.lifespan`` but
    without running the smoke probe against the mocked upstream."""
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
    for router in (health.router, agent.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _sample_agents() -> list[dict]:
    """Three agent entries covering the full-field + sparse shapes.

    Entry 0 is a full agent with every opencode field (whitelist + the
    never-consumed prompt/permission/topP/temperature/color/variant/options/
    steps/model). Entry 1 is sparse (no hidden/native — optional keys). Entry
    2 has whitelist + a couple of extra dropped fields.
    """
    return [
        {
            "name": "build",
            "description": "Build specialist",
            "mode": "primary",
            "hidden": False,
            "native": True,
            "prompt": "y" * 18000,                 # dominant field → drop
            "permission": [{"tool": "bash"}],       # Ruleset list → drop
            "topP": 0.5,
            "temperature": 0.7,
            "color": "#fff",
            "variant": None,
            "options": {"reasoning": True},
            "steps": None,
            "model": "claude",
        },
        {
            # Sparse: hidden / native absent → skeleton omits them.
            "name": "plan",
            "description": "Planning agent",
            "mode": "all",
            "prompt": "z" * 4000,
            "permission": [],
            "temperature": 0.3,
        },
        {
            "name": "review",
            "description": "Code review",
            "mode": "subagent",
            "hidden": True,
            "native": False,
            "prompt": "x" * 2000,
            "color": "#000",
            "model": "gpt-x",
        },
    ]


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test.

    Mirrors ``oc_slimapi.upstream.create_client``: base_url must be set so
    relative upstream paths like ``/agent`` resolve under the MockTransport
    instead of being mis-parsed as absolute."""
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
    """Default app + a happy-path upstream returning the sample catalog."""
    payload = orjson.dumps(_sample_agents())

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
# 1. Skeleton projection — whitelist fields only, order preserved, headers.
# ---------------------------------------------------------------------------

async def test_agent_route_returns_projected_skeleton(app_and_client):
    app, _ = app_and_client
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert isinstance(body, list)
    assert len(body) == 3
    # Each entry carries ONLY the whitelist keys (sparse entries omit absent
    # optional keys rather than emitting key-shaped holes).
    for entry in body:
        assert set(entry.keys()) <= _AGENT_WHITELIST
    # Entry 0 (full): all five whitelist keys present.
    assert set(body[0].keys()) == _AGENT_WHITELIST
    assert body[0]["name"] == "build"
    assert body[0]["mode"] == "primary"
    assert body[0]["hidden"] is False
    assert body[0]["native"] is True
    # Entry 1 (sparse): hidden / native absent.
    assert set(body[1].keys()) == {"name", "description", "mode"}
    # Dominant / never-consumed fields must be gone on every entry.
    for entry in body:
        for dropped in ("prompt", "permission", "temperature", "topP",
                        "color", "variant", "options", "steps", "model"):
            assert dropped not in entry
    # Order preserved (no defensive sort).
    assert [e["name"] for e in body] == ["build", "plan", "review"]
    # Contract headers.
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Accept-Encoding"  # Batch 2/B1: directory merged into Vary


# ---------------------------------------------------------------------------
# 2. gzip negotiation — genuine gzip on the wire, gunzip == plain projection.
# ---------------------------------------------------------------------------

async def test_agent_gzip_negotiation_gunzips_to_plain_projection(upstream_factory):
    payload = orjson.dumps(_sample_agents())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload,
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Reference projection without gzip.
            plain = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
            assert plain.status_code == 200
            plain_body = orjson.loads(plain.content)

            # gzip request: read RAW wire bytes (httpx would auto-decompress
            # response.content) to verify the body is genuinely gzipped.
            async with client.stream(
                "GET", "/slimapi/agent",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["Content-Encoding"] == "gzip"
                assert resp.headers["Vary"] == "Accept-Encoding"  # Batch 2/B1: directory merged into Vary
                raw = b""
                async for chunk in resp.aiter_raw():
                    raw += chunk
        # Genuine gzip: manual gunzip yields valid JSON equal to the plain
        # projection (verifies real compression, not just a header claim).
        assert raw[:2] == b"\x1f\x8b"  # gzip magic number
        gunzipped = gzip.decompress(raw)
        gzip_body = orjson.loads(gunzipped)
        assert gzip_body == plain_body
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 3. directory forwarding — ?directory forwarded as X-Opencode-Directory.
# ---------------------------------------------------------------------------

async def test_agent_directory_forwarded_as_header(upstream_factory):
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        captured["path"] = request.url.path
        return httpx.Response(
            200, content=b"[]", headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/agent?directory=/foo", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        # Upstream sees GET /agent with the directory forwarded as a header.
        assert captured["path"] == "/agent"
        assert captured["dir"] == "/foo"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 4. directory validation — traversal segment rejected with 400.
# ---------------------------------------------------------------------------

async def test_agent_directory_traversal_rejected_400(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/agent?directory=../etc", headers=VERSION_HEADERS,
            )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_directory"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 5. upstream 4xx → 502 upstream_http_N.
# ---------------------------------------------------------------------------

async def test_agent_upstream_4xx_maps_to_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 502
        assert response.json()["code"] == "upstream_http_400"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 6. upstream 5xx → 503 upstream_unavailable.
# ---------------------------------------------------------------------------

async def test_agent_upstream_5xx_maps_to_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error":"boom"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 7. upstream network error → 503 upstream_unavailable.
# ---------------------------------------------------------------------------

async def test_agent_upstream_network_error_maps_to_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 8. oversize body → 413 response_too_large.
# ---------------------------------------------------------------------------

async def test_agent_oversize_body_returns_413(upstream_factory):
    cap = 100
    oversized = orjson.dumps([
        {"name": "x", "description": "y" * 400, "mode": "all", "prompt": "z" * 400},
    ])
    assert len(oversized) > cap  # sanity: body genuinely exceeds the cap

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=cap), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 9. non-list body → 503 upstream_unavailable.
# ---------------------------------------------------------------------------

async def test_agent_non_list_body_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        # Valid JSON, but a dict instead of a list — malformed catalog.
        return httpx.Response(
            200, content=orjson.dumps({"not": "a list"}),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 10. empty list → 200 [].
# ---------------------------------------------------------------------------

async def test_agent_empty_list_returns_200_empty(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"[]", headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 11. transform_busy — admission saturated → 503 + Retry-After, no upstream GET.
# ---------------------------------------------------------------------------

async def test_agent_transform_busy_when_admission_saturated(upstream_factory):
    """Pre-acquire the single admission slot (max_transforms=1), then call
    the route — it must emit 503 transform_busy with Retry-After and must
    NOT hit upstream (admission is acquired BEFORE the GET)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    pool = app.state.transforms
    transport = httpx.ASGITransport(app=app)
    try:
        async with pool:  # saturate the single admission slot
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
            assert response.status_code == 503
            body = response.json()
            assert body["code"] == "transform_busy"
            assert body["retry_after"] == 2
            assert response.headers["Retry-After"] == "2"
        # admission-before-GET → zero upstream calls.
        assert calls["n"] == 0
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 12. version gating — missing X-Slimapi-Version → 400 version_required.
# ---------------------------------------------------------------------------

async def test_agent_retired_version_header_ignored(app_and_client):
    """§1 terminal: X-Slimapi-Version is dead input; the route answers."""
    app, _ = app_and_client
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/agent",
                                    headers={"X-Slimapi-Version": "garbage"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 13. order preserved — no defensive sort (names in non-alphabetical order).
# ---------------------------------------------------------------------------

async def test_agent_order_preserved_no_sort(upstream_factory):
    # Deliberately non-alphabetical name order so a regression that adds a
    # defensive sort would be caught.
    catalog = [
        {"name": "zebra", "description": "d1", "mode": "all"},
        {"name": "apple", "description": "d2", "mode": "primary"},
        {"name": "mango", "description": "d3", "mode": "subagent"},
    ]
    payload = orjson.dumps(catalog)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # Exact upstream order preserved — NOT sorted.
        assert [e["name"] for e in body] == ["zebra", "apple", "mango"]
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# REV-GPT Fix 1 — mid-stream httpx.RequestError must NOT escape as a 500.
#
# Once response headers are received, ``response.aread()`` (the error-body
# drain) and ``read_with_cap`` → ``response.aiter_bytes()`` (the body read)
# can raise ``httpx.RequestError`` (ReadError / ReadTimeout / protocol
# error). Without the inner ``try/except httpx.RequestError`` wrapper those
# escape as an unhandled FastAPI 500. The fix wraps both in a single
# ``try/except`` that maps to 503 ``upstream_unavailable`` (mirrors
# ``messages.py`` /full/{mid}).
#
# MockTransport technique: a handler returning ``httpx.Response(200,
# content=<async_generator>)`` is NOT eagerly consumed (Response.__init__
# only calls ``self.read()`` for ``ByteStream`` i.e. bytes content). The
# async generator is preserved as a lazy ``AsyncIteratorByteStream`` so the
# error fires during the route's body read, AFTER headers succeed — a
# genuine mid-stream failure. (A *sync* generator would raise RuntimeError
# on the async client because ``aiter_raw`` requires an ``AsyncByteStream``.)
# ===========================================================================

async def test_agent_mid_stream_read_error_returns_503(upstream_factory):
    """An httpx.ReadError raised AFTER 200 headers are delivered (mid-body)
    must map to 503 upstream_unavailable, not escape as a bare FastAPI 500.

    The upstream delivers headers + partial body bytes, then the connection
    resets mid-stream. This exercises the ``read_with_cap`` →
    ``aiter_bytes`` path (the success/body-read branch).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        async def failing_body():
            # Yield partial bytes so the read enters the body-consumption
            # phase before failing.
            yield b'[{"name":"partial"'
            # Simulate a mid-stream connection reset AFTER headers.
            raise httpx.ReadError("connection reset mid-stream", request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=failing_body(),
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


async def test_agent_mid_stream_error_during_error_body_drain_returns_503(upstream_factory):
    """The error-body drain path (``response.aread()`` in the status>=400
    branch) is wrapped in the SAME ``try/except httpx.RequestError``, so a
    mid-stream failure while reading an upstream error body also maps to
    503 instead of a 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        async def failing_body():
            yield b'{"error":"partial'
            raise httpx.ReadError("reset while draining error body", request=request)
        return httpx.Response(
            400,
            headers={"Content-Type": "application/json"},
            content=failing_body(),
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# REV-GPT T2 — non-dict list item filtered (orchestrator skeleton change).
#
# ``skeleton_agents`` now skips non-dict items (mirrors ``skeleton_messages``
# which does ``for part in ... if isinstance(part, dict)``). So
# ``[null, {valid agent}]`` yields ``[{valid skeleton}]`` — the null is
# silently dropped, NOT a 503.
# ===========================================================================

async def test_agent_non_dict_item_filtered_returns_200(upstream_factory):
    """A non-dict item (null) in the catalog list is silently filtered by
    skeleton_agents (orchestrator change); the valid agent is projected.
    Response is 200, not 503."""
    payload = orjson.dumps([
        None,  # non-dict → filtered
        {"name": "build", "description": "Build specialist", "mode": "primary"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 200
        body = orjson.loads(response.content)
        # Exactly one entry (the null was dropped) with only whitelist keys.
        assert isinstance(body, list)
        assert len(body) == 1
        assert set(body[0].keys()) <= _AGENT_WHITELIST
        assert body[0]["name"] == "build"
        assert body[0]["mode"] == "primary"
    finally:
        app.state.transforms.shutdown()


# ===========================================================================
# REV-GPT T3 — empty / non-JSON body → 503 (orjson.JSONDecodeError path).
# ===========================================================================

@pytest.mark.parametrize(
    "body",
    [b"", b"not json{"],
    ids=["empty", "garbage"],
)
async def test_agent_empty_or_non_json_body_returns_503(upstream_factory, body: bytes):
    """Upstream 200 with empty / non-JSON body must not escape as a bare 500.
    ``_project_agent_and_pack`` raises ``orjson.JSONDecodeError``; the route
    maps it to 503 ``upstream_unavailable`` (same code as the non-list guard).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/agent", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json() == {"code": "upstream_unavailable"}
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# P0-6: agent catalog forwards X-Request-ID upstream (contract §7).
# agent.py shares _catalog_common.handle_catalog_request with command.py, so
# this is the symmetric lock-down of the same helper on the agent side.
# ---------------------------------------------------------------------------


async def test_agent_forwards_request_id_to_upstream(upstream_factory):
    """P0-6: ``GET /slimapi/agent`` must forward ``X-Request-ID`` upstream so
    the sidecar access log line correlates with opencode's logs (contract §7).
    The agent route shares ``_catalog_common.handle_catalog_request`` with
    command, so this also covers the helper's wiring on this side."""
    from oc_slimapi.middleware.request_id import RequestIdMiddleware

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["rid"] = request.headers.get("x-request-id")
        return httpx.Response(
            200, content=orjson.dumps(_sample_agents()),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    app.add_middleware(RequestIdMiddleware)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/agent",
                headers={**VERSION_HEADERS, "X-Request-ID": "agent-rid-9"},
            )
        assert response.status_code == 200
        assert captured["rid"] == "agent-rid-9"
    finally:
        app.state.transforms.shutdown()
