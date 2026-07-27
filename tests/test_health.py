"""Route-level integration tests for the health/ready gzip cleanup (§9).

Contract §9 requires every JSON ``json_response`` call to forward
``accept_encoding=request.headers.get("accept-encoding")`` so a client
advertising ``Accept-Encoding: gzip`` actually gets a gzip-coded body.

The app is constructed fresh per test (mirroring ``oc_slimapi.app.lifespan``
without running the smoke probe) so the routes can be exercised end-to-end
through ``httpx.ASGITransport`` with a mocked upstream.
"""
from __future__ import annotations

import asyncio
import gzip

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health
from oc_slimapi.versioning import SlimapiVersionMiddleware

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
        route_secret="x" * 32,
        route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(2, 2),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app with only the health router wired up,
    pre-populating ``app.state`` the same way ``oc_slimapi.app.lifespan`` does
    but without running the smoke probe."""
    app = FastAPI(title="oc-slimapi-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health.router)
    register_error_handlers(app)
    return app


@pytest.fixture
def upstream_factory():
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

    async def _close_all():
        for client in clients:
            await client.aclose()

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_close_all())
    except RuntimeError:
        pass


def _make_upstream_ok():
    """Upstream handler that reports a healthy /global/health."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"healthy": true}',
                              headers={"Content-Type": "application/json"})
    return handler


async def _get(app: FastAPI, path: str, extra_headers: dict[str, str] | None = None):
    headers = dict(VERSION_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


# ---------------------------------------------------------------------------
# /slimapi/health
# ---------------------------------------------------------------------------

async def test_health_with_accept_encoding_gzip_returns_gzip(upstream_factory):
    """① /slimapi/health + Accept-Encoding: gzip → Content-Encoding: gzip."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/health",
                          extra_headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"
    # httpx auto-decompresses response.content; verify the wire payload round-trips.
    raw = response.raw_response.read() if hasattr(response, "raw_response") else None
    # Decoding the (already decompressed) content must match a fresh gzip round-trip.
    body = orjson.loads(response.content)
    assert body["sidecar"]["ok"] is True
    assert body["server"]["accepted_client_versions"] == [2, 2]
    assert body["schema"]["degraded"] is False
    assert body["slimapi_contract"] == 2


async def test_health_without_accept_encoding_is_not_gzipped(upstream_factory):
    """② /slimapi/health with Accept-Encoding: identity → no Content-Encoding.

    httpx's AsyncClient defaults to ``Accept-Encoding: gzip, deflate``, so we
    must send an explicit ``identity`` to actually exercise the no-gzip branch
    of the contract."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/health",
                          extra_headers={"Accept-Encoding": "identity"})

    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") is None
    assert response.headers["Vary"] == "Accept-Encoding"
    body = orjson.loads(response.content)
    assert body["sidecar"]["ok"] is True


# ---------------------------------------------------------------------------
# /slimapi/ready
# ---------------------------------------------------------------------------

async def test_ready_with_accept_encoding_gzip_returns_gzip(upstream_factory):
    """① /slimapi/ready + Accept-Encoding: gzip → Content-Encoding: gzip.

    Covers the Lane-D gap: ``/ready`` previously omitted the
    ``accept_encoding`` kwarg, so gzip was never applied even when the client
    asked for it.
    """
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/ready",
                          extra_headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"
    body = orjson.loads(response.content)
    assert body["upstream"]["ok"] is True
    assert isinstance(body["upstream"]["latencyMs"], int)


async def test_ready_without_accept_encoding_is_not_gzipped(upstream_factory):
    """② /slimapi/ready with Accept-Encoding: identity → no Content-Encoding.

    See ``test_health_without_accept_encoding_is_not_gzipped`` for why we use
    ``identity`` rather than omitting the header."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/ready",
                          extra_headers={"Accept-Encoding": "identity"})

    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") is None
    assert response.headers["Vary"] == "Accept-Encoding"
    body = orjson.loads(response.content)
    assert body["upstream"]["ok"] is True


# ---------------------------------------------------------------------------
# Negative case: gzip still honoured on /ready's 503 path.
# ---------------------------------------------------------------------------

async def test_ready_503_path_also_negotiates_gzip(upstream_factory):
    """Even when upstream is down (→ 503), the gzip negotiation must still
    fire: the contract §9 says *all* JSON routes forward ``accept_encoding``,
    not only the happy path."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"err": "boom"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/ready",
                          extra_headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 503
    assert response.headers["Content-Encoding"] == "gzip"
    body = orjson.loads(response.content)
    assert body["upstream"]["ok"] is False


# ---------------------------------------------------------------------------
# Regression guard: version gate still enforced.
# ---------------------------------------------------------------------------

async def test_health_rejects_missing_version_header(upstream_factory):
    """Cross-check: the version gate fires on /slimapi/health so future gzip
    refactors can't accidentally bypass it."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/health")  # no version header

    assert response.status_code == 400
    assert response.json()["code"] == "version_required"


# ---------------------------------------------------------------------------
# Wire-level gzip sanity: invoke the ASGI app directly so we can inspect the
# raw response bytes (httpx auto-decompresses response.content, hiding the
# gzip magic from us). This proves the body — not just the header — is gzip.
# ---------------------------------------------------------------------------

async def _asgi_call_raw(app: FastAPI, method: str, path: str,
                         headers_list: list[tuple[str, str]]) -> tuple[int, list, bytes]:
    """Drive the ASGI app by hand and collect (status, headers, raw_body).

    Bypasses httpx entirely so the body is exactly what the route wrote."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers_list],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
        "extensions": {},
    }
    status_code = 0
    headers: list = []
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status_code, headers, body
        if message["type"] == "http.response.start":
            status_code = message["status"]
            headers = message["headers"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status_code, headers, body


async def test_health_gzip_body_is_genuinely_gzip_encoded(upstream_factory):
    """Decode the raw ASGI response bytes through gzip to prove the body —
    not just the Content-Encoding header — was compressed."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    status, headers, raw_bytes = await _asgi_call_raw(
        app, "GET", "/slimapi/health",
        [("X-Slimapi-Version", "2"), ("Accept-Encoding", "gzip")],
    )

    assert status == 200
    header_map = {k.decode().lower(): v.decode() for k, v in headers}
    assert header_map["content-encoding"] == "gzip"
    # gzip magic bytes — the body is genuinely compressed.
    assert raw_bytes[:2] == b"\x1f\x8b"
    decoded = gzip.decompress(raw_bytes)
    body = orjson.loads(decoded)
    assert body["sidecar"]["ok"] is True


async def test_health_identity_body_is_not_gzip_encoded(upstream_factory):
    """Symmetric wire-level proof: with Accept-Encoding: identity the raw body
    is plaintext JSON (no gzip magic) and no Content-Encoding header is set."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    status, headers, raw_bytes = await _asgi_call_raw(
        app, "GET", "/slimapi/health",
        [("X-Slimapi-Version", "2"), ("Accept-Encoding", "identity")],
    )

    assert status == 200
    header_map = {k.decode().lower(): v.decode() for k, v in headers}
    assert "content-encoding" not in header_map
    # Raw body parses directly as JSON → it was not compressed.
    assert raw_bytes[:2] != b"\x1f\x8b"
    body = orjson.loads(raw_bytes)
    assert body["sidecar"]["ok"] is True


# ---------------------------------------------------------------------------
# v6 §4: schema section now exposes version / clientMin / clientMax.
# Old ``server.*`` fields are preserved for back-compat.
# ---------------------------------------------------------------------------

async def test_health_schema_includes_version_and_client_range(upstream_factory):
    """`/slimapi/health` schema carries the wire-version triplet from config."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/health")
    assert response.status_code == 200
    body = response.json()
    # New keys exist and are read from config (not hard-coded).
    assert body["schema"] == {
        "degraded": False,
        "version": 1,
        "clientMin": 2,
        "clientMax": 2,
    }
    # Old ``server.*`` keys still there for back-compat.
    assert body["server"]["api_version"] == 1
    assert body["server"]["accepted_client_versions"] == [2, 2]
    # lite-v2: static contract revision.
    assert body["slimapi_contract"] == 2


async def test_health_schema_reflects_non_default_config(upstream_factory):
    """With a wider accepted range, clientMin/clientMax follow the config.

    Locks that the new keys are NOT hard-coded to 1,1 — they come from
    ``Settings.accepted_client_versions`` at request time.
    """
    upstream = upstream_factory(_make_upstream_ok())
    settings = _settings(accepted_client_versions=(1, 3))
    app = _build_app(settings, upstream)

    response = await _get(app, "/slimapi/health")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"]["clientMin"] == 1
    assert body["schema"]["clientMax"] == 3
    assert body["server"]["accepted_client_versions"] == [1, 3]


async def test_ready_schema_includes_version_and_client_range(upstream_factory):
    """`/slimapi/ready` mirrors the same schema expansion as `/health`."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == {
        "degraded": False,
        "version": 1,
        "clientMin": 2,
        "clientMax": 2,
    }


async def test_ready_503_path_preserves_schema_fields(upstream_factory):
    """Schema fields are present on the 503 path too (diagnostic, not gated
    on upstream health)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["schema"]["version"] == 1
    assert body["schema"]["clientMin"] == 2
    assert body["schema"]["clientMax"] == 2
    # Old fields still there.
    assert body["server"]["api_version"] == 1
    assert body["server"]["accepted_client_versions"] == [2, 2]


async def test_health_schema_reflects_schema_degraded_state(upstream_factory):
    """``schema.degraded`` is dynamic — flip it on the app and re-read."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)
    app.state.schema_degraded = True

    response = await _get(app, "/slimapi/health")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"]["degraded"] is True
    # Other keys still present.
    assert body["schema"]["version"] == 1


# ---------------------------------------------------------------------------
# Thresholded skeleton diagnostic (additive; default-on, no version bump).
# features.thresholdedSkeleton + skeletonInlineOutputMaxBytes are diagnostic
# only — behaviour does not depend on a client reading them.
# ---------------------------------------------------------------------------

async def test_health_advertises_thresholded_skeleton_feature(upstream_factory):
    """``features.thresholdedSkeleton`` is True and the numeric cap is reported
    alongside tokenStream (root-level, parallel — not nested under server.*)."""
    upstream = upstream_factory(_make_upstream_ok())
    app = _build_app(_settings(), upstream)

    response = await _get(app, "/slimapi/health")
    assert response.status_code == 200
    features = response.json()["features"]
    # tokenStream still present (unchanged).
    assert features["tokenStream"] is True
    # New diagnostic keys.
    assert features["thresholdedSkeleton"] is True
    assert features["skeletonInlineOutputMaxBytes"] == 4096
