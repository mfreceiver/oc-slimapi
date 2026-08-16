"""Route-level integration tests for ``routes/command.py`` (skeleton catalog).

Tests exercise the ``GET /slimapi/command`` endpoint end-to-end through a
mocked upstream. The app is constructed fresh per test (bypassing the module-
level lifespan) so we can dial down transform-pool knobs without touching
env vars.
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
from oc_slimapi.routes import command, health, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}


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
    """Construct a fresh FastAPI app with command router wired up."""
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
    for router in (health.router, sessions.router, command.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _sample_catalog() -> list[dict]:
    """Sample upstream command catalog with fields that skeleton keeps + drops."""
    return [
        {
            "name": "dev",
            "description": "General coding agent",
            "agent": None,
            "hints": [{"type": "mcp"}],
            "template": "x" * 3000,
            "source": "builtin",
            "model": "gpt-x",
            "subtask": False,
        },
        {
            "name": "plan",
            "description": "Plan mode",
            # agent absent — majority of commands have no agent
            "hints": [{"type": "step"}],
            "template": "y" * 5000,
            "source": "builtin",
        },
    ]


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test."""
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
    payload = orjson.dumps(_sample_catalog())

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
# Skeleton projection
# ---------------------------------------------------------------------------


async def test_skeleton_projection(app_and_client):
    """Skeleton projection keeps only {name,description,agent,hints} keys;
    template/source/model/subtask are dropped. Order is preserved."""
    app, _ = app_and_client
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Accept-Encoding"  # Batch 2/B1: directory merged into Vary
    body = orjson.loads(response.content)
    assert isinstance(body, list)
    assert len(body) == 2
    # First entry: all skeleton keys present; dropped keys absent.
    assert set(body[0].keys()) == {"name", "description", "agent", "hints"}
    assert body[0]["name"] == "dev"
    assert body[0]["description"] == "General coding agent"
    assert body[0]["agent"] is None
    assert body[0]["hints"] == [{"type": "mcp"}]
    for dropped in ("template", "source", "model", "subtask"):
        assert dropped not in body[0]
    # Second entry: agent absent (optional), only present keys survive.
    assert set(body[1].keys()) == {"name", "description", "hints"}
    assert body[1]["name"] == "plan"


async def test_order_preserved(app_and_client):
    """Catalog order is preserved (no sort)."""
    app, _ = app_and_client
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert [entry["name"] for entry in body] == ["dev", "plan"]


# ---------------------------------------------------------------------------
# Gzip negotiation
# ---------------------------------------------------------------------------


async def test_gzip_negotiation(upstream_factory):
    """Accept-Encoding: gzip → genuine gzip at the wire level (gzip magic
    bytes, manual gunzip yields valid JSON equal to the plain projection)."""
    catalog = orjson.dumps(_sample_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=catalog, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings()
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Reference projection without gzip.
            plain = await client.get(
                "/slimapi/command",
                headers={**VERSION_HEADERS, "Accept-Encoding": "identity"},
            )
            assert plain.status_code == 200
            assert "Content-Encoding" not in plain.headers
            plain_body = orjson.loads(plain.content)

            # gzip request: read RAW wire bytes to verify genuine compression.
            async with client.stream(
                "GET", "/slimapi/command",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["Content-Encoding"] == "gzip"
                assert resp.headers["Vary"] == "Accept-Encoding"  # Batch 2/B1: directory merged into Vary
                raw = b""
                async for chunk in resp.aiter_raw():
                    raw += chunk
        # Genuine gzip: magic bytes + manual decompress yields valid JSON.
        assert raw[:2] == b"\x1f\x8b", "gzip magic number missing"
        gunzipped = gzip.decompress(raw)
        gzip_body = orjson.loads(gunzipped)
        assert gzip_body == plain_body
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Directory forwarding and validation
# ---------------------------------------------------------------------------


async def test_directory_forwarding(upstream_factory):
    """?directory=/foo → X-Opencode-Directory: /foo forwarded to upstream."""
    captured: dict[str, str | None] = {}
    catalog = orjson.dumps(_sample_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=catalog, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/command?directory=/foo", headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        assert captured["dir"] == "/foo"
    finally:
        app.state.transforms.shutdown()


async def test_directory_validation(upstream_factory):
    """?directory=../etc → 400 invalid_directory."""
    catalog = orjson.dumps(_sample_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=catalog, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/command?directory=../etc", headers=VERSION_HEADERS,
            )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_directory"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# P0-6: X-Request-ID forwarded upstream on catalog requests (contract §7)
# ---------------------------------------------------------------------------


async def test_command_forwards_request_id_to_upstream(upstream_factory):
    """P0-6: GET /slimapi/command forwards X-Request-ID to upstream opencode so
    the sidecar access log line can be correlated with opencode's own logs
    (contract §7). Inbound ``X-Request-ID`` is preserved by
    ``RequestIdMiddleware`` (stored in scope.state) and re-emitted upstream
    alongside the directory header."""
    from oc_slimapi.middleware.request_id import RequestIdMiddleware

    captured: dict[str, str | None] = {}
    catalog = orjson.dumps(_sample_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        captured["rid"] = request.headers.get("x-request-id")
        return httpx.Response(200, content=catalog, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    # Without the middleware, scope.state has no request_id → header would be
    # omitted. The middleware is what production wires up.
    app.add_middleware(RequestIdMiddleware)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/command", headers={**VERSION_HEADERS, "X-Request-ID": "req-xyz-789"},
            )
        assert response.status_code == 200
        # The inbound X-Request-ID flows through the middleware into scope.state
        # and back out as the upstream request header.
        assert captured["rid"] == "req-xyz-789"
    finally:
        app.state.transforms.shutdown()


async def test_command_forwards_directory_and_request_id_together(upstream_factory):
    """P0-6 regression: directory header and X-Request-ID must NOT collide —
    they have distinct header names and must both arrive at upstream. Earlier
    catalog code forwarded only the directory header; the request_id helper
    adds the second header without clobbering the first."""
    from oc_slimapi.middleware.request_id import RequestIdMiddleware

    captured: dict[str, str | None] = {}
    catalog = orjson.dumps(_sample_catalog())

    def handler(request: httpx.Request) -> httpx.Response:
        captured["dir"] = request.headers.get("x-opencode-directory")
        captured["rid"] = request.headers.get("x-request-id")
        return httpx.Response(200, content=catalog, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    app.add_middleware(RequestIdMiddleware)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/command?directory=/foo",
                headers={**VERSION_HEADERS, "X-Request-ID": "abc-123"},
            )
        assert response.status_code == 200
        assert captured["dir"] == "/foo"
        assert captured["rid"] == "abc-123"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Upstream error mapping
# ---------------------------------------------------------------------------


async def test_upstream_4xx(upstream_factory):
    """Upstream 400 → 502 upstream_http_400."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}', headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 502
        assert response.json()["code"] == "upstream_http_400"
    finally:
        app.state.transforms.shutdown()


async def test_upstream_5xx(upstream_factory):
    """Upstream 500 → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'internal error')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


async def test_upstream_network_error(upstream_factory):
    """Upstream network error (ConnectError) → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Oversize body cap
# ---------------------------------------------------------------------------


async def test_oversize_body(upstream_factory):
    """Body exceeds max_response_bytes → 413 response_too_large."""
    cap = 100
    oversized = orjson.dumps([{"name": "x", "description": "y" * (cap * 2)}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    settings = _settings(max_response_bytes=cap, transform_wait_seconds=2.0)
    app = _build_app(settings, upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 413
        body = response.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == cap
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Non-dict item filtering (orchestrator's skeleton.py now filters non-dict
# items, so a list with nulls yields 200 + filtered result, not 503).
# ---------------------------------------------------------------------------


async def test_non_dict_item_filtered(upstream_factory):
    """Upstream returns ``[null, {valid command}]`` → 200 with one entry
    projected to whitelist (the null is silently dropped by skeleton.py)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps([None, {"name": "dev", "description": "d", "template": "x"}]),
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert set(body[0].keys()) == {"name", "description"}
        assert body[0]["name"] == "dev"
        assert "template" not in body[0]
    finally:
        app.state.transforms.shutdown()


async def test_non_list_body(upstream_factory):
    """Upstream returns a JSON dict (non-list) → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=orjson.dumps({"error": "oops"}), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Empty list
# ---------------------------------------------------------------------------


async def test_empty_list(upstream_factory):
    """Upstream returns [] → 200 []."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Mid-stream network error (Fix 1 guard: aread / read_with_cap raises
# httpx.RequestError → 503, not 500).
# ---------------------------------------------------------------------------


async def test_mid_stream_read_error_returns_503(upstream_factory, monkeypatch):
    """A mid-stream httpx.RequestError during the body read (after headers
    received) must surface as 503 upstream_unavailable, not a bare 500.
    We monkey-patch ``read_with_cap`` in the command module to raise.

    NOTE: MockTransport materialises the entire body eagerly in the handler,
    so we cannot simulate a mid-stream body failure with a normal handler.
    The monkey-patch approach proves that the inner ``try/except
    httpx.RequestError`` guard (mirroring messages.py's /full/{mid} pattern)
    catches the error and maps it to a structured 503. This same guard also
    covers the ``aread()`` error-drain path (4xx/5xx body reads)."""
    import oc_slimapi.routes.command as cmd_mod

    async def _raising_read_with_cap(*args, **kwargs):
        raise httpx.ReadError("mid-stream failure")

    monkeypatch.setattr(cmd_mod, "read_with_cap", _raising_read_with_cap)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json() == {"code": "upstream_unavailable"}
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Empty body / non-JSON body → 503 upstream_unavailable (orjson.JSONDecodeError
# caught by the route).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [b"", b"not json{{{"],
    ids=["empty", "garbage"],
)
async def test_invalid_json_body_returns_503(upstream_factory, body):
    """Upstream 200 with empty or non-JSON body → 503 upstream_unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json() == {"code": "upstream_unavailable"}
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Transform busy (pool saturation)
# ---------------------------------------------------------------------------


async def test_transform_busy(upstream_factory):
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
    transport = httpx.ASGITransport(app)
    try:
        async with pool:  # saturate the single admission slot
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/slimapi/command", headers=VERSION_HEADERS)
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
# Version gating
# ---------------------------------------------------------------------------


async def test_retired_version_header_ignored_at_route_level(upstream_factory):
    """§1 terminal: X-Slimapi-Version is dead input (selector owns
    admission); the route answers regardless of its value."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/command",
                                        headers={"X-Slimapi-Version": "9"})
        assert response.status_code == 200
    finally:
        app.state.transforms.shutdown()
