"""v3-contract §3a — /slimapi/health + /slimapi/ready.

B12 (2026-08-21) three-way split: /ready is 零 v4 分叉 (v4-contract §12 —
shape AND values frozen to the terminal v3 view regardless of the
requested wire version) and the deploymentRevision omission holds on both
health views, so those three functions were rewritten to the ``?v=4``
face. V2b (2026-08-21 Phase-4 teardown) removed the remaining v3-face
guards (the ?v=3 view lock, the retired-header-next-to-?v=3 rejection and
the header-only no-?v rejection) — version-window 400 coverage for the
window itself lives in the selector test-suite.
"""
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


async def test_ready_v4_request_keeps_frozen_v3_view_no_contract_field():
    """B12 ①: /ready is 零 v4 分叉 — even an admitted ``?v=4`` request is
    answered with the frozen terminal-v3 view values and no contract
    field."""
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/ready?v=4")
        assert r.status_code == 200
        body = r.json()
        assert "slimapi_contract" not in body  # shape locked: no contract
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["schema"]["clientMin"] == 4
        assert body["schema"]["clientMax"] == 4
        assert body["server"]["accepted_client_versions"] == [4, 4]


async def test_ready_upstream_down_503():
    app = _build_app(_upstream(ok=False))
    async with _client(app) as client:
        r = await client.get("/slimapi/ready?v=4")
        assert r.status_code == 503
        assert r.json()["upstream"]["ok"] is False


async def test_health_deployment_revision_omitted():
    app = _build_app(_upstream())
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=4")
        assert "deploymentRevision" not in r.json()["server"]
