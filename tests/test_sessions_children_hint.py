"""Tests for children hint fields on /slimapi/sessions (rev H).

Covers cache-hint additivity: childrenIDs[], childrenComplete, budget limit,
directory isolation, and regression on existing list behaviour.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.children_cache import ChildrenCache, CacheEntry
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.observability import BatchLedger
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, questions, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


# ---------------------------------------------------------------------------
# Self-contained fixtures (do NOT touch conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test."""
    clients: list[httpx.AsyncClient] = []

    def _make(handler, *, base_url: str = "http://127.0.0.1:4096"):
        client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app mirroring production but with given upstream."""
    settings = _settings()
    app = FastAPI(title="oc-slimapi-children-hint-test")
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
    app.state.batch_ledger = BatchLedger(window_seconds=settings.opt_a_rollback_window_seconds)
    app.state.children = ChildrenCache(upstream)
    app.include_router(sessions.router)
    app.include_router(messages.router)
    app.include_router(questions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


async def _get(upstream: httpx.AsyncClient, path: str) -> httpx.Response:
    """Helper: build app + ASGI client, issue a GET, return the response."""
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=VERSION_HEADERS)


def _upstream_passthrough() -> tuple:
    """Return a handler that echoes upstream passthrough and tracks seen requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b'[{"id":"ses_a"},{"id":"ses_b"}]',
                            headers={"Content-Type": "application/json"})

    return handler, seen


# ===========================================================================
# Tests
# ===========================================================================


async def test_hint_cache_hit_returns_children_ids_and_complete(upstream_factory):
    """Cache pre-filled → parent session gets childrenIDs+childrenComplete=true.

    We inject a cache entry for parent "ses_a" before the list call, then
    verify the list response includes the hint fields for that session only.
    """
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(upstream)

    # Inject cache entry for parent "ses_a"
    cache = app.state.children
    now = time.monotonic()
    cache._cache[("ses_a", "")] = CacheEntry(
        value=[{"id": "child1"}, {"id": "child2"}],
        version=1,
        generation=1,
        fetched_at=now,
        expires_at=now + 60.0,
        is_empty=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)

    assert response.status_code == 200
    body = response.json()
    # Should have 2 sessions
    assert len(body) == 2
    # ses_a should have hint fields
    ses_a = next(s for s in body if s.get("id") == "ses_a")
    assert ses_a.get("childrenComplete") is True
    assert ses_a.get("childrenIDs") == ["child1", "child2"]
    # ses_b should NOT have hint fields (no cache entry)
    ses_b = next(s for s in body if s.get("id") == "ses_b")
    assert ses_b.get("childrenComplete") is False
    assert "childrenIDs" not in ses_b


async def test_hint_cache_miss_omits_children_ids(upstream_factory):
    """No cache entry → all sessions get childrenComplete=false, no childrenIDs.

    Upstream returns fres data, but children cache is empty → no hint.
    """
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(upstream)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    for session in body:
        assert session.get("childrenComplete") is False
        assert "childrenIDs" not in session


async def test_hint_budget_exceeded_omits_ids_and_sets_complete_false(upstream_factory):
    """Cache entry has >32 children → budget exceeded → childrenComplete=false, no IDs.

    We inject an entry with 33 children (over CHILDREN_IDS_HINT_LIMIT=32).
    """
    handler, seen = _upstream_passthrough()
    upstream = upstream_factory(handler)
    app = _build_app(upstream)

    cache = app.state.children
    now = time.monotonic()
    many_children = [{"id": f"child{i}"} for i in range(33)]
    cache._cache[("ses_a", "")] = CacheEntry(
        value=many_children,
        version=1,
        generation=1,
        fetched_at=now,
        expires_at=now + 60.0,
        is_empty=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)

    assert response.status_code == 200
    body = response.json()
    ses_a = next(s for s in body if s.get("id") == "ses_a")
    assert ses_a.get("childrenComplete") is False
    assert "childrenIDs" not in ses_a
    # ses_b unaffected
    ses_b = next(s for s in body if s.get("id") == "ses_b")
    assert ses_b.get("childrenComplete") is False
    assert "childrenIDs" not in ses_b


async def test_hint_directory_isolation(upstream_factory):
    """Cache entries are keyed by (parent_sid, normalized_dir).

    Test that a cache entry for directory "/a" does NOT match a request with
    directory "/b" (or no directory).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[{"id":"ses_a"},{"id":"ses_b"}]',
                            headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)

    cache = app.state.children
    now = time.monotonic()
    # Cache entry for "ses_a" under directory "/app1"
    cache._cache[("ses_a", "/app1")] = CacheEntry(
        value=[{"id": "child1"}],
        version=1,
        generation=1,
        fetched_at=now,
        expires_at=now + 60.0,
        is_empty=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request with NO directory → cache miss (key uses "")
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code == 200
        body = response.json()
        ses_a = next(s for s in body if s.get("id") == "ses_a")
        assert ses_a.get("childrenComplete") is False
        assert "childrenIDs" not in ses_a

        # Request with directory "/app2" → cache miss
        response = await client.get(
            "/slimapi/sessions?directory=/app2", headers=VERSION_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        ses_a = next(s for s in body if s.get("id") == "ses_a")
        assert ses_a.get("childrenComplete") is False
        assert "childrenIDs" not in ses_a

        # Request with directory "/app1" → cache HIT
        response = await client.get(
            "/slimapi/sessions?directory=/app1", headers=VERSION_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        ses_a = next(s for s in body if s.get("id") == "ses_a")
        assert ses_a.get("childrenComplete") is True
        assert ses_a.get("childrenIDs") == ["child1"]


async def test_hint_does_not_regress_existing_behaviour(upstream_factory):
    """Existing list endpoint behaviour unchanged: fields, headers, error paths.

    We test that with an empty cache, the response still has the standard
    fields (id, directory, etc.), plus the completeness/discovery headers,
    and error paths (network, 4xx) still produce proper error codes.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[{"id":"ses_x","directory":"/app"}]',
                            headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    ses = body[0]
    assert "id" in ses
    assert "directory" in ses
    # childrenComplete should be present (False; cache miss)
    assert ses.get("childrenComplete") is False
    assert "childrenIDs" not in ses
    # Headers
    assert response.headers.get("X-Complete") == "true"
    assert response.headers.get("X-Discovery-Directories") is not None
    assert response.headers.get("X-Discovery-Ready") is not None
    # No extra unexpected keys
    unexpected = set(ses.keys()) - {"id", "directory", "time", "childrenComplete"}
    assert not unexpected, f"Unexpected keys: {unexpected}"

    # Also test error path: network error → 503
    def err_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    upstream2 = upstream_factory(err_handler)
    app2 = _build_app(upstream2)
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://test") as client2:
        response2 = await client2.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response2.status_code == 503
    assert response2.json() == {"code": "upstream_unavailable"}
