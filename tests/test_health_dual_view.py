"""v3-contract §3a — /slimapi/health + /slimapi/ready dual views (Batch A)."""
from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health
from oc_slimapi.selector import SlimapiSelectorMiddleware

V2_HEADER = {"X-Slimapi-Version": "2"}


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
        accepted_client_versions=(2, 3),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="health-dual-view-test")
    app.add_middleware(
        SlimapiSelectorMiddleware,
        accepted_client_versions=(2, 3),
        v3_enabled=True,
    )
    app.state.config = _settings()
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


# ---------------------------------------------------------------------------
# /slimapi/health — v2 view
# ---------------------------------------------------------------------------

async def test_health_v2_view():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        body = (await client.get("/slimapi/health", headers=V2_HEADER)).json()
        assert body["slimapi_contract"] == 2
        assert body["server"]["api_version"] == 2
        assert body["schema"]["version"] == 2
        # No 3/2 combination: all three version fields synced.
        assert body["slimapi_contract"] == body["server"]["api_version"] == body["schema"]["version"]


async def test_health_v2_view_explicit_v2():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        body = (await client.get("/slimapi/health?v=2", headers=V2_HEADER)).json()
        assert body["slimapi_contract"] == 2
        assert body["server"]["api_version"] == 2
        assert body["schema"]["version"] == 2


async def test_health_accepted_range_widened():
    """2.0.0: accepted_client_versions / clientMin / clientMax = [2, 3]."""
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        body = (await client.get("/slimapi/health", headers=V2_HEADER)).json()
        assert body["server"]["accepted_client_versions"] == [2, 3]
        assert body["schema"]["clientMin"] == 2
        assert body["schema"]["clientMax"] == 3


# ---------------------------------------------------------------------------
# /slimapi/health — v3 view
# ---------------------------------------------------------------------------

async def test_health_v3_view():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        body = (await client.get("/slimapi/health?v=3")).json()
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["slimapi_contract"] == body["server"]["api_version"] == body["schema"]["version"]
        # Accepted range unchanged across views.
        assert body["server"]["accepted_client_versions"] == [2, 3]
        assert body["schema"]["clientMin"] == 2
        assert body["schema"]["clientMax"] == 3


async def test_health_v3_view_with_header_2_selector_wins():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        body = (await client.get("/slimapi/health?v=3", headers=V2_HEADER)).json()
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3


# ---------------------------------------------------------------------------
# /slimapi/ready — no contract field; schema triple dual-view
# ---------------------------------------------------------------------------

def _upstream_ok() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


async def test_ready_v2_view_no_contract_field():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        r = await client.get("/slimapi/ready", headers=V2_HEADER)
        assert r.status_code == 200
        body = r.json()
        assert "slimapi_contract" not in body
        assert body["server"]["api_version"] == 2
        assert body["schema"]["version"] == 2
        assert body["server"]["api_version"] == body["schema"]["version"]
        assert body["schema"]["clientMin"] == 2
        assert body["schema"]["clientMax"] == 3


async def test_ready_v3_view_no_contract_field():
    app = _build_app(_upstream_ok())
    async with _client(app) as client:
        r = await client.get("/slimapi/ready?v=3")
        assert r.status_code == 200
        body = r.json()
        assert "slimapi_contract" not in body
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["server"]["api_version"] == body["schema"]["version"]
