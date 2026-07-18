"""Route-level integration tests for ``GET /slimapi/metrics`` (Lane-H / T3).

Exercises the metrics endpoint end-to-end through a real FastAPI app + the
version-gate middleware so the wire-level contract is asserted:

* version header present + happy path → 200 with the §2/§6 snapshot shape
* missing version header → 400 ``version_required`` (the middleware gate
  covers ``/slimapi/**`` automatically, so metrics inherits it for free)
* ``Accept-Encoding: gzip`` negotiates gzip on the response

The app is constructed fresh per test (bypassing the module-level lifespan)
so we can pre-populate ``app.state`` without env vars.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import metrics
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
        max_subscribers_per_directory=8,
        max_total_subscribers=16,
        sse_queue_items=256,
        sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings) -> tuple[FastAPI, HubRegistry, httpx.AsyncClient]:
    app = FastAPI(title="oc-slimapi-metrics-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    upstream = httpx.AsyncClient()
    app.state.config = settings
    app.state.upstream = upstream
    transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.transforms = transforms
    hubs = HubRegistry(
        upstream,
        max_subscribers_per_directory=settings.max_subscribers_per_directory,
        max_total_subscribers=settings.max_total_subscribers,
        queue_items=settings.sse_queue_items,
        buffer_bytes=settings.sse_buffer_bytes,
        max_frame_bytes=settings.sse_max_frame_bytes,
    )
    hubs.set_transforms(transforms)
    app.state.hubs = hubs
    app.include_router(metrics.router)
    register_error_handlers(app)
    return app, hubs, upstream


async def test_metrics_route_returns_snapshot_with_version_header():
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics", headers=VERSION_HEADERS)
        assert response.status_code == 200
        data = response.json()
        # Contract §2/§6 shape.
        assert set(data) == {"sse", "skeleton"}
        sse = data["sse"]
        assert set(sse) == {"subscribers", "hubs", "clients"}
        assert sse["subscribers"] == {
            "current": 0,
            "limit": 16,
            "rejectedTotal": 0,
        }
        # No subscribers yet → empty hubs and clients arrays.
        assert sse["hubs"] == []
        assert sse["clients"] == []
        skeleton = data["skeleton"]
        assert set(skeleton) == {"activeTransforms", "waitingTransforms", "cacheEnabled"}
        assert skeleton["cacheEnabled"] is False
        # Pool is idle: 0 active, 0 waiting.
        assert skeleton["activeTransforms"] == 0
        assert skeleton["waitingTransforms"] == 0
        assert response.headers.get("Vary") == "Accept-Encoding"
    finally:
        await hubs.close()
        transforms_shutdown = app.state.transforms.shutdown
        transforms_shutdown()
        await upstream.aclose()


async def test_metrics_route_reports_live_subscriber_after_subscribe():
    """Once a subscriber is admitted, the snapshot surfaces it in both the
    hubs entry and the clients entry (contract §2)."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        subscriber = hubs.subscribe()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics", headers=VERSION_HEADERS)
        data = response.json()
        assert data["sse"]["subscribers"]["current"] == 1
        assert len(data["sse"]["hubs"]) == 1
        assert data["sse"]["hubs"][0]["subscribers"] == 1
        assert len(data["sse"]["clients"]) == 1
        assert data["sse"]["clients"][0]["subscriberId"] == subscriber.id
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_metrics_route_rejects_missing_version_header():
    """The /slimapi/** version gate covers metrics too — a missing header
    yields 400 ``version_required`` rather than the snapshot."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics")
        assert response.status_code == 400
        assert response.json()["code"] == "version_required"
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_metrics_route_supports_gzip_when_requested():
    """gzip_util.json_response honours Accept-Encoding; the metrics endpoint
    threads the header through so a client that asks for gzip gets it
    (contract §9 — full coverage is Lane-D's job, but metrics is Lane-H's
    and the same plumbing applies)."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/metrics",
                headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "gzip"
        assert response.headers.get("Vary") == "Accept-Encoding"
        # Body still parses (httpx transparently decompresses for .content/.json()).
        data = response.json()
        assert "sse" in data and "skeleton" in data
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_metrics_route_without_gzip_when_not_requested():
    """Default (no gzip in Accept-Encoding) → no Content-Encoding header, raw JSON.

    httpx auto-injects ``Accept-Encoding: gzip, deflate, br`` on every
    request, so to exercise the "client did not ask for gzip" branch we
    have to override it with ``identity`` explicitly.
    """
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/metrics",
                headers={**VERSION_HEADERS, "Accept-Encoding": "identity"},
            )
        assert response.status_code == 200
        assert "Content-Encoding" not in response.headers
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()
