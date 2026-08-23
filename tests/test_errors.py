from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import CodedHTTPException, register_error_handlers
from oc_slimapi.routes import messages, sessions
from oc_slimapi.proxy import install_proxy
from oc_slimapi.selector import SlimapiSelectorMiddleware


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=4, accepted_client_versions=(4, 4),
    )


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-errors-test")
    app.state.config = _settings()
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.include_router(sessions.router)
    # messages router is needed for the selector's directory query/header
    # conflict path.
    from oc_slimapi.transform import TransformConfig, TransformPool
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
    ))
    app.include_router(messages.router)
    register_error_handlers(app)
    install_proxy(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


async def test_coded_exception_renders_code_body():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise")
    async def raise_it():
        raise CodedHTTPException(418, code="teapot", flavor="earl")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/raise")
    assert response.status_code == 418
    assert response.json() == {"code": "teapot", "flavor": "earl"}


async def test_directory_conflict_renders_code(upstream_factory):
    """v4 selector rejects differing query/header directories as a conflict."""
    def handler(request: httpx.Request) -> httpx.Response:
        # The conflict is detected before any upstream call, so this handler
        # should never be reached; the 200 is just a safe default.
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/messages/s1?v=4&directory=/app",
            headers={"X-Opencode-Directory": "/other"},
        )
    assert response.status_code == 400
    assert response.json() == {
        "code": "directory_conflict",
        "queryDirectory": "/app",
        "headerDirectory": "/other",
    }
