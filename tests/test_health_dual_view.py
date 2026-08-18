"""v3-contract §3a — /slimapi/health + /slimapi/ready, single v3 view
(terminal state: the dual view is collapsed — every admitted request ran
``?v=3``)."""
from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health
from oc_slimapi.selector import SlimapiSelectorMiddleware


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


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="health-single-view-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.upstream = upstream
    app.include_router(health.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://t")


def _upstream(ok: bool = True) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if ok else 500, json={"healthy": ok})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://u"
    )


async def test_health_single_v3_view():
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=3")
        assert r.status_code == 200
        body = r.json()
        # The view triplet is one constant — a 3/2 combination is
        # structurally impossible.
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["server"]["accepted_client_versions"] == [3, 4]
        assert body["schema"]["clientMin"] == 3
        assert body["schema"]["clientMax"] == 4


async def test_health_retired_header_cannot_change_view():
    """The retired X-Slimapi-Version header is not read — any value next to
    a valid ?v=3 keeps the single view."""
    app = _build_app(_upstream())
    async with _client(app) as client:
        for value in ("2", "3", "9"):
            r = await client.get("/slimapi/health?v=3",
                                 headers={"X-Slimapi-Version": value})
            assert r.status_code == 200
            body = r.json()
            assert body["slimapi_contract"] == 3
            assert body["schema"]["version"] == 3


async def test_health_no_v_rejected():
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/health",
                             headers={"X-Slimapi-Version": "2"})
        assert r.status_code == 400
        assert r.json() == {"code": "unsupported_version", "supported": [3, 4]}


async def test_ready_single_v3_view_no_contract_field():
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/ready?v=3")
        assert r.status_code == 200
        body = r.json()
        assert "slimapi_contract" not in body  # shape locked: no contract
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["schema"]["clientMin"] == 3
        assert body["schema"]["clientMax"] == 4
        assert body["server"]["accepted_client_versions"] == [3, 4]


async def test_ready_upstream_down_503():
    app = _build_app(_upstream(ok=False))
    async with _client(app) as client:
        r = await client.get("/slimapi/ready?v=3")
        assert r.status_code == 503
        assert r.json()["upstream"]["ok"] is False


async def test_health_deployment_revision_omitted():
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=3")
        assert "deploymentRevision" not in r.json()["server"]
