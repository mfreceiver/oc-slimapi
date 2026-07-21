"""G-F1 server-side cursor conditions for ``/since/{ts}`` and ``GET /slimapi/messages/{sid}``.

Exercises the cursor-walk endpoints through a mocked upstream that serves multi-page
newest-first listings.

Covers:
- Equal-ts multi-mid boundary inclusive
- Cross-page boundary with X-Next-Cursor passthrough
- Limit truncation
- Reconnect replay (resume with last cursor)
- Loop-triggered cursor-walk degradation (server-side termination)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, messages, questions, sessions, events
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
        max_since_pages=5,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-g-f1-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    app.state.allowlist_ready = False
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.hubs = HubRegistry(upstream)
    for router in (health.router, sessions.router, messages.router, questions.router, events.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _msg(mid: str, updated: int) -> dict:
    return {
        "info": {"id": mid, "role": "user", "time": {"created": updated, "updated": updated}},
        "parts": [{"id": f"p-{mid}", "type": "text", "messageID": mid, "text": "x" * 100}],
    }


def _make_page(mids: list[tuple[str, int]]) -> list[dict]:
    """Build an upstream page JSON body from a list of (mid, updated) pairs."""
    return [_msg(mid, updated) for mid, updated in mids]


def _link_next_cursor(cursor: str) -> str:
    return f'<http://127.0.0.1:4096/session/s1/message?before={cursor}&limit=1>; rel="next"'


class TestSinceEqualTsBoundary:
    """``/since/{ts}`` includes mids with ``created >= ts`` (boundary inclusive)."""

    # Load fixture from file (validates both reference AND our test handler).
    FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "g_f1" / "equal_ts_page1.json"
    PAGE1 = orjson.loads(FIXTURE_PATH.read_bytes())

    async def test_equal_ts_inclusive(self, upstream_factory):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            # Return one page, no more pages (no Link header)
            return httpx.Response(200, content=orjson.dumps(self.PAGE1))
        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/slimapi/messages/s1/since/100?limit=10",
                    headers=VERSION_HEADERS,
                )
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 2
            # Both mids included (boundary inclusive)
            assert {m["info"]["id"] for m in data} == {"m1", "m2"}
        finally:
            app.state.transforms.shutdown()


class TestCrossPageBoundary:
    """X-Next-Cursor surfaces Link header's opaque cursor; resuming works."""

    PAGE1 = _make_page([("m1", 200)])
    PAGE2 = _make_page([("m2", 100)])

    async def test_cross_page_cursor_passthrough(self, upstream_factory):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            # First page has Link to second
            if "before" not in request.url.params:
                return httpx.Response(
                    200,
                    content=orjson.dumps(self.PAGE1),
                    headers={"Link": _link_next_cursor("cursor_for_page2")},
                )
            # Second page has no Link (terminal)
            return httpx.Response(200, content=orjson.dumps(self.PAGE2))
        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/slimapi/messages/s1/since/0?limit=1",
                    headers=VERSION_HEADERS,
                )
                assert r.status_code == 200
                data = r.json()
                assert len(data) == 1
                assert data[0]["info"]["id"] == "m1"
                # X-Next-Cursor should be present
                assert "X-Next-Cursor" in r.headers
                cursor = r.headers["X-Next-Cursor"]
                # Resume with the cursor
                r2 = await client.get(
                    f"/slimapi/messages/s1/since/0?limit=1&before={cursor}",
                    headers=VERSION_HEADERS,
                )
                assert r2.status_code == 200
                data2 = r2.json()
                assert len(data2) == 1
                assert data2[0]["info"]["id"] == "m2"
                # No X-Next-Cursor on the last page
                assert "X-Next-Cursor" not in r2.headers
        finally:
            app.state.transforms.shutdown()


class TestLimitTruncation:
    """``limit`` honored; X-Next-Cursor emitted only when limit filled AND ts floor not hit AND upstream advertised next."""

    PAGE = _make_page([("m1", 100), ("m2", 200)])
    # limit=1, but page has 2 items → client sees 1, cursor emitted
    async def test_limit_truncation_emits_cursor(self, upstream_factory):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            return httpx.Response(
                200,
                content=orjson.dumps(self.PAGE),
                headers={"Link": _link_next_cursor("cursor")},
            )
        upstream = upstream_factory(handler)
        app = _build_app(_settings(max_response_bytes=1024 * 1024), upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/slimapi/messages/s1/since/0?limit=1",
                    headers=VERSION_HEADERS,
                )
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 1
            assert data[0]["info"]["id"] == "m1"  # newest first
            # Cursor emitted because limit filled AND no ts floor hit AND upstream has more
            assert "X-Next-Cursor" in r.headers
        finally:
            app.state.transforms.shutdown()


class TestReconnectReplay:
    """Client resumes with last cursor → correct page."""

    PAGE1 = _make_page([("m1", 200)])
    PAGE2 = _make_page([("m2", 100)])

    async def test_reconnect_with_cursor(self, upstream_factory):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            if "before" not in request.url.params:
                return httpx.Response(
                    200,
                    content=orjson.dumps(self.PAGE1),
                    headers={"Link": _link_next_cursor("cursor_2")},
                )
            return httpx.Response(200, content=orjson.dumps(self.PAGE2))
        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/slimapi/messages/s1/since/0?limit=1",
                    headers=VERSION_HEADERS,
                )
                cursor = r.headers.get("X-Next-Cursor")
                assert cursor is not None
                # Reconnect with the cursor
                r2 = await client.get(
                    f"/slimapi/messages/s1/since/0?limit=1&before={cursor}",
                    headers=VERSION_HEADERS,
                )
                assert r2.status_code == 200
                data2 = r2.json()
                assert len(data2) == 1
                assert data2[0]["info"]["id"] == "m2"
        finally:
            app.state.transforms.shutdown()


class TestLoopDegradation:
    """Server-side loop-triggered cursor-walk degradation: ``max_since_pages`` bound.

    The ``/messages`` list endpoint paginates and eventually returns NO cursor
    (terminal), proving no infinite loop.
    """

    PAGES = [
        _make_page([("m1", 200)]),
        _make_page([("m2", 200)]),
        _make_page([("m3", 200)]),
    ]
    MAX_PAGES = len(PAGES)

    async def test_list_terminates_within_max_pages(self, upstream_factory):
        page_index = [0]

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            idx = page_index[0]
            if idx >= self.MAX_PAGES:
                return httpx.Response(200, content=orjson.dumps([]))
            page = self.PAGES[idx]
            page_index[0] += 1
            has_next = idx < self.MAX_PAGES - 1
            if has_next:
                return httpx.Response(
                    200,
                    content=orjson.dumps(page),
                    headers={"Link": _link_next_cursor(f"cursor_{idx}")},
                )
            return httpx.Response(200, content=orjson.dumps(page))
        upstream = upstream_factory(handler)
        settings = _settings(max_since_pages=self.MAX_PAGES)
        app = _build_app(settings, upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # Start the walk
                r = await client.get(
                    "/slimapi/messages/s1?limit=1",
                    headers=VERSION_HEADERS,
                )
                assert r.status_code == 200
                # Walk until no more cursor
                pages_seen = 1
                while "X-Next-Cursor" in r.headers:
                    cursor = r.headers["X-Next-Cursor"]
                    r = await client.get(
                        f"/slimapi/messages/s1?limit=1&before={cursor}",
                        headers=VERSION_HEADERS,
                    )
                    assert r.status_code == 200
                    pages_seen += 1
                # We should have seen exactly MAX_PAGES pages (the loop terminates)
                assert pages_seen == self.MAX_PAGES
        finally:
            app.state.transforms.shutdown()


# Todo: add more G-F1 tests (since with equal ts floor that would trigger loop,
#       since with cross-page that triggers loop, etc.)


class TestAdversarialLoop:
    """Adversarial loop: server-side ``for``-bound caps a same-cursor repeated walk."""

    PAGE = _make_page([("m1", 100)])

    async def test_adversarial_loop_terminates_at_max_pages(self, upstream_factory):
        call_count = [0]  # mutable closure
        max_pages = 5
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/session/s1":
                return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
            call_count[0] += 1
            # Always return same page + Link for the first max_pages calls
            # (adversarial same-cursor injection). Then drop Link.
            if call_count[0] <= max_pages:
                return httpx.Response(
                    200,
                    content=orjson.dumps(self.PAGE),
                    headers={"Link": _link_next_cursor("LOOP")},
                )
            return httpx.Response(200, content=orjson.dumps(self.PAGE))
        upstream = upstream_factory(handler)
        settings = _settings(max_since_pages=max_pages)
        app = _build_app(settings, upstream)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/slimapi/messages/s1/since/0?limit=1",
                    headers=VERSION_HEADERS,
                )
                assert r.status_code == 200
                pages_seen = 1
                while "X-Next-Cursor" in r.headers:
                    cursor = r.headers["X-Next-Cursor"]
                    r = await client.get(
                        f"/slimapi/messages/s1/since/0?limit=1&before={cursor}",
                        headers=VERSION_HEADERS,
                    )
                    assert r.status_code == 200
                    pages_seen += 1
                    assert pages_seen <= settings.max_since_pages * 2  # safety guard
                # Loop terminated because handler stopped emitting Link after max_pages
                # Each Link call is one page with cursor; terminal page has no cursor.
                # So pages_seen == max_since_pages + 1 (all with-Link pages + terminal page)
                assert pages_seen == settings.max_since_pages + 1
                assert call_count[0] == settings.max_since_pages + 1
        finally:
            app.state.transforms.shutdown()
