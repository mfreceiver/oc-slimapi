from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import CodedHTTPException, register_error_handlers
from oc_slimapi.routes import sessions
from oc_slimapi.proxy import install_proxy


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_json_bytes=64 * 1024 * 1024, max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-errors-test")
    app.state.config = _settings()
    app.state.route_secret = app.state.config.route_secret.encode()
    app.state.upstream = upstream
    app.state.directory_allowlist = set()
    app.include_router(sessions.router)
    register_error_handlers(app)
    install_proxy(app)
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


async def test_require_directory_miss_renders_code(upstream_factory):
    """allowlist miss → 400 {"code":"directory_not_allowed"}."""
    def handler(request: httpx.Request) -> httpx.Response:
        # /project returns empty list → allowlist stays empty → miss.
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?directory=/nope",
            headers={"X-Slimapi-Version": "1"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "directory_not_allowed"


async def test_require_directory_refresh_failure_renders_code(upstream_factory):
    """load_projects upstream failure → 503 {"code":"upstream_unavailable",...}."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?directory=/nope",
            headers={"X-Slimapi-Version": "1"},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "upstream_unavailable"
    assert body["message"] == "cannot refresh directory allowlist"
