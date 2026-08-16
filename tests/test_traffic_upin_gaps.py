"""Tests for fixing upstream byte (upIn / upOut) undercounting gaps.

Scenarios:

1. **ready stash** (MUST-PASS): ``/slimapi/ready`` pings upstream
   ``/global/health`` → the ``health`` bucket's ``upIn`` includes the
   health-check response body (was fully missing before the fix).

2. **cap-bail upIn** (B1): ``/slimapi/sessions`` and
   ``/slimapi/messages/{sid}`` oversize upstream bodies → 413 cap-bail
   STILL attributes the bytes read to the respective bucket's ``upIn``
   (stash runs before the None check — unified convention).

3. **disconnect finally** (BEST-EFFORT): a streaming proxy request where
   the client disconnects mid-stream → already-forwarded bytes are still
   stashed (``finally`` guard on ``proxy._counted_req_stream``).

Hard constraint: this file is self-contained (no conftest changes) and
follows the ``_build_app`` + ``upstream_factory`` pattern established in
``test_traffic_integration.py``.
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}


# ---------------------------------------------------------------------------
# Settings + app helpers (self-contained, no conftest changes).
# ---------------------------------------------------------------------------

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


def _build_app(
    settings: Settings,
    upstream: httpx.AsyncClient,
    *,
    include_health: bool = False,
    include_messages: bool = False,
    include_proxy: bool = False,
) -> tuple[FastAPI, TrafficLedger]:
    """Construct a FastAPI app with the traffic ledger + middleware wired up.

    Routers are opt-in via flags so each scenario mounts only what it needs.
    Uses the same approach as ``test_traffic_integration._build_app_with_traffic``.
    """
    app = FastAPI(title="oc-slimapi-upin-gaps-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))

    ledger = TrafficLedger()
    app.state.traffic_ledger = ledger

    if include_health:
        app.include_router(health.router)
    if include_messages:
        app.include_router(sessions.router)
        app.include_router(messages.router)
    if include_proxy:
        install_proxy(app)

    register_error_handlers(app)
    app.add_middleware(TrafficAccountingMiddleware)
    return app, ledger


async def _shutdown(app: FastAPI) -> None:
    """Best-effort teardown."""
    app.state.transforms.shutdown()
    await app.state.hubs.close()


# ===========================================================================
# Scenario 1 — ready stash (MUST-PASS)
# ===========================================================================

async def test_ready_stashes_upin(upstream_factory):
    """``/slimapi/ready`` pings upstream ``/global/health`` → the ``health``
    bucket's ``upIn`` includes the health-check response body bytes (were fully
    missing before the fix).
    """
    HEALTH_BODY = b'{"healthy":true}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/global/health"
        return httpx.Response(200, content=HEALTH_BODY,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app, ledger = _build_app(
        _settings(), upstream, include_health=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            response = await client.get(
                "/slimapi/ready",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 200
        body = response.json()
        assert body["upstream"]["ok"] is True

        snap = ledger.snapshot()
        assert snap["enabled"] is True
        assert "health" in snap["buckets"], (
            f"expected health bucket, got {set(snap['buckets'])}"
        )
        bucket = snap["buckets"]["health"]
        assert bucket["upIn"] == len(HEALTH_BODY), (
            f"upIn ({bucket['upIn']}) should equal the health-check body "
            f"({len(HEALTH_BODY)}) — ready endpoint did not stash upstream bytes"
        )
        assert bucket["requests"] == 1
    finally:
        await _shutdown(app)


async def test_ready_503_path_stashes_upin(upstream_factory):
    """Even when upstream returns non-200 (→503 response), the health-check
    body bytes are still stashed (``stash_up_in`` runs unconditionally after
    the GET, before the ok check)."""
    HEALTH_BODY = b'{"error":"down"}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/global/health"
        return httpx.Response(500, content=HEALTH_BODY,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app, ledger = _build_app(
        _settings(), upstream, include_health=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            response = await client.get(
                "/slimapi/ready",
                headers=VERSION_HEADERS,
            )
        assert response.status_code == 503
        body = response.json()
        assert body["upstream"]["ok"] is False

        snap = ledger.snapshot()
        assert snap["enabled"] is True
        assert "health" in snap["buckets"]
        bucket = snap["buckets"]["health"]
        # The 500 error body is still counted.
        assert bucket["upIn"] == len(HEALTH_BODY), (
            f"upIn ({bucket['upIn']}) should equal the 500 health body "
            f"({len(HEALTH_BODY)}) — even error bodies must be stashed"
        )
        assert bucket["requests"] == 1
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 3 — proxy try/finally upOut stash (structural + happy-path)
# ===========================================================================

async def test_write_route_stashes_upout(upstream_factory):
    """Terminal: the annexed write pipeline attributes the buffered request
    body to ``upOut`` (accounting parity with the retired catch-all
    forwarder's _counted_req_stream)."""
    body = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        async def body_iter():
            yield body

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body_iter()),
            headers={"Content-Type": "application/octet-stream"},
        )

    upstream = upstream_factory(handler)
    app, ledger = _build_app(
        _settings(), upstream, include_proxy=False,
    )
    assert ledger is not None

    # Add the write router + selector (the bumped/bodied POST surface).
    from oc_slimapi.routes import write_groups
    from oc_slimapi.selector import SlimapiSelectorMiddleware
    app.include_router(write_groups.router)
    app.add_middleware(SlimapiSelectorMiddleware)

    request_body = b'{"command":"echo hello"}'

    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        response = await client.post(
            "/slimapi/session/s1/command?v=3",
            content=request_body,
        )
    assert response.status_code == 200

    snap = ledger.snapshot()
    assert snap["enabled"] is True
    # /slimapi/session/{sid}/command bucketizes under write_session.
    assert "write_session" in snap["buckets"]
    bucket = snap["buckets"]["write_session"]
    # upOut includes the request body bytes (stashed at send time).
    assert bucket["upOut"] == len(request_body), (
        f"upOut ({bucket['upOut']}) should equal request body length "
        f"({len(request_body)})"
    )
    # The upstream response body is counted into upIn (cap-read on_read).
    assert bucket["upIn"] == len(body)


async def test_closed_catch_all_stashes_no_upstream_bytes(upstream_factory):
    """Terminal §8.2: the closed catch-all never forwards — a /session POST
    records the request with zero upOut/upIn (the mid-stream upIn machinery
    of the retired forwarder is unreachable by construction)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("closed surface must never reach the upstream")

    upstream = upstream_factory(handler)
    app, ledger = _build_app(
        _settings(), upstream, include_proxy=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            try:
                await client.post("/session/s1/command", content=b"{}")
            except httpx.HTTPError:
                pass
        snap = ledger.snapshot()
        assert "passthrough" in snap["buckets"]
        bucket = snap["buckets"]["passthrough"]
        assert bucket["upOut"] == 0
        assert bucket["upIn"] == 0
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 2 — cap-bail upIn (B1)
# ===========================================================================

async def test_sessions_cap_bail_stashes_upin(upstream_factory):
    """B1: when /slimapi/sessions upstream body exceeds max_response_bytes,
    the 413 cap-bail STILL attributes the bytes read to the ``sessions``
    bucket ``upIn`` (stash runs before the None check — unified convention)."""
    # Body larger than max_response_bytes (64 KiB). Content need not be valid
    # JSON: read_with_cap bails before the route ever parses it.
    oversize = b"x" * (200 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session"
        return httpx.Response(200, content=oversize,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app, ledger = _build_app(_settings(), upstream, include_messages=True)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code == 413
        assert response.json()["code"] == "response_too_large"
        snap = ledger.snapshot()
        bucket = snap["buckets"]["sessions"]
        assert bucket["upIn"] >= 64 * 1024, (
            f"cap-bail upIn ({bucket['upIn']}) must still attribute the oversize "
            f"read — B1 unified stash-before-None convention"
        )
    finally:
        await _shutdown(app)


async def test_messages_cap_bail_stashes_upin(upstream_factory):
    """B1: when /slimapi/messages/{sid} upstream body exceeds
    max_response_bytes, the 413 cap-bail STILL attributes the bytes read to
    the ``messages`` bucket ``upIn``."""
    oversize = b"x" * (200 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/s1/message"
        return httpx.Response(200, content=oversize,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app, ledger = _build_app(_settings(), upstream, include_messages=True)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
        assert response.status_code == 413
        assert response.json()["code"] == "response_too_large"
        snap = ledger.snapshot()
        bucket = snap["buckets"]["messages"]
        assert bucket["upIn"] >= 64 * 1024, (
            f"cap-bail upIn ({bucket['upIn']}) must still attribute the oversize "
            f"read — B1 unified stash-before-None convention"
        )
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 4 — proxy mid-stream upstream response aclose via finally (P1-10)
# ===========================================================================

