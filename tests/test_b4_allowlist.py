"""B4-4 directory allowlist fail-closed coverage (S-B05)."""

from __future__ import annotations

import logging

import httpx
import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.app import _log_directory_allowlist
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health, read_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import Subscriber


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
        directory_allowlist=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_file_app(settings: Settings, handler) -> tuple[FastAPI, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _file_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=b'{"sentinel":"byte-identical"}',
        headers={"Content-Type": "application/json"},
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _event(directory, event_type="session.status", properties=None):
    return {
        "directory": directory,
        "payload": {
            "type": event_type,
            "properties": properties or {"sessionID": "s1", "status": "busy"},
        },
    }


async def _close_hub(hub: GlobalHub) -> None:
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await __import__("asyncio").gather(*tasks, return_exceptions=True)


def test_directory_allowlist_env_has_three_states(monkeypatch):
    monkeypatch.delenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", raising=False)
    assert Settings().directory_allowlist is None

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "")
    assert Settings().directory_allowlist == []

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "/a/:/b/../b")
    assert Settings().directory_allowlist == ["/a", "/b"]


def test_directory_allowlist_env_rejects_blank_and_relative_entries(monkeypatch):
    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "/a::/b")
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_DIRECTORY_ALLOWLIST"):
        Settings().validate()

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "relative")
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_DIRECTORY_ALLOWLIST"):
        Settings().validate()


async def test_unconfigured_file_route_is_byte_identical():
    app, seen = _build_file_app(_settings(), _file_ok)
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=3&path=foo&directory=/outside",
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 200
    assert response.content == b'{"sentinel":"byte-identical"}'
    assert seen[0].url.path == "/file"


@pytest.mark.parametrize("path", ["/slimapi/file", "/slimapi/file/content", "/slimapi/file/status"])
@pytest.mark.parametrize("directory", ["/outside", None])
async def test_empty_allowlist_blocks_all_file_routes(path, directory):
    app, seen = _build_file_app(_settings(directory_allowlist=[]), _file_ok)
    async with _client(app) as client:
        query = "?v=3"
        if directory is not None:
            query += "&directory=" + directory
        if path.endswith("/file") or path.endswith("/file/content"):
            query += "&path=foo"
        response = await client.get(path + query)
    assert response.status_code == 403
    assert response.json() == {"code": "directory_not_allowed"}
    assert "outside" not in response.text
    assert not seen


async def test_allowlist_file_route_allows_directory_subtree_and_blocks_prefix_trap():
    app, seen = _build_file_app(_settings(directory_allowlist=["/a", "/ab"]), _file_ok)
    async with _client(app) as client:
        allowed = await client.get("/slimapi/file?v=3&path=foo&directory=/a/b")
        exact = await client.get("/slimapi/file/status?v=3&directory=/ab")
        blocked = await client.get("/slimapi/file/content?v=3&path=foo&directory=/abc")
    assert allowed.status_code == 200
    assert exact.status_code == 200
    assert blocked.status_code == 403
    assert len(seen) == 2


async def test_empty_allowlist_does_not_filter_sse():
    hub = GlobalHub(client=None, directory_allowlist=[])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside"))
        hub.flush()
        assert subscriber.queue.qsize() == 1
        assert hub.allowlist_dropped_events == 0
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_drops_outside_digest_and_counts_once():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside"))
        hub.flush()
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 1
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_drops_outside_immediate_and_unknown_frames():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside", "question.asked", {"id": "q1"}))
        hub.publish(_event(None, "permission.asked", {"id": "p1"}))
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 2
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_allows_subtree_and_preserves_changed_field():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/allowed/nested"))
        hub.flush()
        frame = await subscriber.queue.get()
        assert b'"directory":"/allowed/nested"' in frame
        assert b'"changed":["s1"]' in frame
        assert hub.allowlist_dropped_events == 0
    finally:
        await _close_hub(hub)


async def test_health_reports_allowlist_and_dropped_event_count():
    app = FastAPI()
    settings = _settings(directory_allowlist=["/allowed"])
    app.state.config = settings
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    hub = GlobalHub(client=None, directory_allowlist=settings.directory_allowlist)
    hub.allowlist_dropped_events = 3

    class Registry:
        def get_global(self):
            return hub

    app.state.hubs = Registry()
    app.include_router(health.router)
    register_error_handlers(app)
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/health")
        assert response.status_code == 200
        assert response.json()["features"]["allowlist"] == {
            "enabled": True,
            "droppedEvents": 3,
        }
    finally:
        await _close_hub(hub)


def test_empty_allowlist_startup_warning(caplog):
    settings = _settings(directory_allowlist=[])
    with caplog.at_level(logging.WARNING, logger="oc_slimapi.app"):
        _log_directory_allowlist(settings)
    assert "directory allowlist enabled but empty" in caplog.text
    assert "/slimapi/file/** will 403" in caplog.text
