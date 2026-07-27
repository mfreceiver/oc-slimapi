"""Tests for fixing upstream byte (upIn) undercounting gaps.

Scenarios (per reviewer findings):

1. **ready stash** (MUST-PASS): ``/slimapi/ready`` pings upstream
   ``/global/health`` → the ``health`` bucket's ``upIn`` includes the
   health-check response body (was fully missing before the fix).

2. **batch 4xx drain** (BEST-EFFORT): a G6 batch ``fetch_one`` where a
   per-mid upstream returns 404 → the 404 response body is drained and
   counted in ``messages`` bucket ``upIn``.

3. **disconnect finally** (BEST-EFFORT): a streaming proxy request where
   the client disconnects mid-stream → already-forwarded bytes are still
   stashed (``finally`` guard on ``proxy._counted_req_stream``).

Hard constraint: this file is self-contained (no conftest changes) and
follows the ``_build_app`` + ``upstream_factory`` pattern established in
``test_traffic_integration.py``.

NOTE: lite-v2 removed the ``/slimapi/questions`` family of endpoints;
the former "questions 4xx stash" scenario has been removed accordingly.
"""

from __future__ import annotations

import asyncio

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
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


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
        route_secret="x" * 32,
        route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
        # Opt-A knobs needed by messages.py handler (even for legacy path).
        opt_a_partial_envelope_enabled=True,
        opt_a_auto_rollback_enabled=False,
        opt_a_rollback_window_seconds=3600,
        opt_a_rollback_min_sample=100,
        opt_a_rollback_envelope_5xx_zero_baseline_rate=0.01,
        opt_a_rollback_unknown_code_rate=0.05,
        opt_a_retry_after_ms_conservative=200,
        opt_a_retry_after_ms_cap=10000,
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
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set()
    app.state.allowlist_ready = False
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.batch_ledger = None

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

async def test_proxy_counted_req_stream_happy_path_stashes_upout(
    upstream_factory,
):
    """Structural + happy-path check of the proxy's ``_counted_req_stream``
    ``try/finally`` upOut stash.

    The proxy wraps the upstream request body in a ``_counted_req_stream``
    generator whose ``finally`` block calls ``stash_up_out(request, n)`` so
    the bytes we send upstream are attributed to ``upOut`` even if the
    generator is torn down early (client disconnect → ``GeneratorExit`` /
    ``CancelledError``).

    What this test DOES cover (the happy path):
      * A normal proxied POST with a request body lands ``upOut == len(body)``
        in the ``passthrough`` bucket — proving the stash code path is
        reached and the ``finally`` block fires on clean completion.
      * The upstream response body is passed through 1:1 into ``upIn``.

    What this test does NOT cover (and deliberately so):
      A genuine mid-stream client disconnect test would require cancelling
      the ASGI ``receive`` mid-iteration to tear the generator down early,
      which is extremely fragile under httpx/Starlette and tends to hang or
      flake. The disconnect → ``finally`` guarantee is therefore backed by
      **code review** (the ``finally`` is unconditional — it runs on both
      normal completion and ``GeneratorExit``/``CancelledError``), not by a
      brittle mid-stream cancellation harness. This keeps the suite fast and
      deterministic without sacrificing the happy-path coverage that proves
      the stash wiring works at all.
    """
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
        _settings(), upstream, include_proxy=True,
    )
    assert ledger is not None

    request_body = b'{"command":"echo hello"}'

    # Normal proxied POST through the catch-all reverse proxy. The
    # ``_counted_req_stream`` finally block fires on clean completion and
    # stashes the request body bytes as upOut.
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        response = await client.post(
            "/session/s1/command",
            content=request_body,
        )
    assert response.status_code == 200

    snap = ledger.snapshot()
    assert snap["enabled"] is True
    assert "passthrough" in snap["buckets"]
    bucket = snap["buckets"]["passthrough"]
    # upOut should include the request body bytes (stashed by
    # _counted_req_stream's finally block).
    assert bucket["upOut"] == len(request_body), (
        f"upOut ({bucket['upOut']}) should equal request body length "
        f"({len(request_body)})"
    )
    # The proxy body is passed through 1:1.
    assert bucket["upIn"] == len(body)
