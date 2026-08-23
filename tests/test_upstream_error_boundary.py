"""TDD tests for Batch 1 upstream error boundary fixes (contract §7).

Two upstream error mapping gaps currently violate the §7 unified coded-error
contract:

1. ``GET /slimapi/sessions/status`` (batch) in ``routes/sessions.py``:
   the route does a bare ``await upstream.get()`` + ``response.json()`` +
   passthrough — **no** ``httpx.RequestError`` guard, **no**
   ``raise_for_status``, **no** JSON-shape validation. Network errors,
   bad JSON, and non-dict bodies bubble up as unstructured FastAPI 500s;
   upstream 4xx/5xx pass through verbatim instead of being mapped to
   ``upstream_http_N`` / ``upstream_unavailable``.

2. ``routes/messages.py`` list / since / full single-message: the initial
   ``upstream.send(..., stream=True)`` is not wrapped in a
   ``try/except httpx.RequestError`` — an initial connection failure
   escapes as a 500 instead of a structured 503 ``upstream_unavailable``.

The implementation has NOT been fixed yet. Every test below is marked
``xfail(strict=False)`` so ``./scripts/check.sh`` stays green: these encode
the **post-fix** expected behaviour (TDD). Once the fixer lands the fix,
each test will ``xpass``; the xfail markers can then be removed.

This file is **self-contained**: it defines its own ``upstream_factory``
fixture (shadowing the shared conftest one) and its own ``_build_app`` so
it touches neither ``conftest.py`` nor any existing ``test_*.py``.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import CodedHTTPException, register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.upstream_errors import (
    raise_upstream_status,
    raise_upstream_status_code,
)


VERSION_HEADERS = {"X-Slimapi-Version": "1"}

# Every test in this file encodes behaviour the fixer has NOT implemented
# yet. The module-level marker keeps ``./scripts/check.sh`` green: tests
# that currently fail are reported as ``xfailed`` (not errors), and once
# the fix lands they become ``xpassed`` (still green under strict=False).
# ---------------------------------------------------------------------------
# Self-contained fixtures (do NOT touch conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test.

    Shadows the shared conftest fixture so this module is fully independent.
    """
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
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app mirroring ``oc_slimapi.app.lifespan``
    but without running the smoke probe. Wires sessions + messages routers
    plus the transform pool, error handlers, and catch-all proxy so every
    route under test resolves exactly as in production."""
    settings = _settings()
    app = FastAPI(title="oc-slimapi-error-boundary-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.hubs = HubRegistry(upstream)
    app.include_router(sessions.router)
    app.include_router(messages.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


async def _get(upstream: httpx.AsyncClient, path: str) -> httpx.Response:
    """Helper: build app + ASGI client, issue a GET, return the response.

    ``Accept-Encoding: identity`` so the raw response bytes can be locked
    (the coded-error bodies are orjson-compact — byte equality only holds
    without gzip negotiation)."""
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            path, headers={**VERSION_HEADERS, "Accept-Encoding": "identity"})


def _assert_upstream_unavailable_wire(response: httpx.Response) -> None:
    """B3b wire lock for the shared 503 ``upstream_unavailable`` mapping:
    raw identity body + JSON media type + negotiation Vary. This layer
    (``raise_upstream_unavailable`` behind the messages/sessions routes)
    intentionally carries NO ``Cache-Control`` — only the read-groups v4
    pipeline stamps no-store (§12.5.3). Do NOT add no-store here."""
    assert response.status_code == 503
    assert response.content == b'{"code":"upstream_unavailable"}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["vary"] == "Accept-Encoding"
    assert "cache-control" not in response.headers


# ===========================================================================
# routes/messages.py — initial send() network error boundary
#
# The list / since / full-single routes all issue an initial
# ``upstream.send(..., stream=True)`` (directly or via ``_stream_upstream``)
# WITHOUT a ``try/except httpx.RequestError`` wrapper. A connection failure
# on that initial send currently escapes as an unstructured FastAPI 500.
#
# Expected (post-fix): 503 upstream_unavailable with {"code": "..."} body,
# consistent with the G6 batch discover guard (``messages.py:550-561``) and
# the single-message full-mode mid-stream guard (``messages.py:852-853``).
# ===========================================================================


async def test_messages_list_initial_send_network_error_returns_503(upstream_factory):
    """``GET /slimapi/messages/{sid}`` (list, skeleton default): initial
    ``_stream_upstream`` raises httpx.RequestError → 503 upstream_unavailable.

    Currently uncaught → 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    response = await _get(upstream, "/slimapi/messages/ses_x")
    _assert_upstream_unavailable_wire(response)


async def test_message_full_single_initial_send_network_error_returns_503(upstream_factory):
    """``GET /slimapi/messages/{sid}/full/{mid}`` (full default): initial
    ``upstream.send(..., stream=True)`` raises httpx.RequestError → 503
    upstream_unavailable.

    Currently uncaught → 500. (The mid-stream guard at ``messages.py:852``
    only covers ``aread``/``aiter_bytes`` AFTER ``send`` succeeds, not the
    ``send`` call itself.)"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    response = await _get(upstream, "/slimapi/messages/ses_x/full/m1")
    _assert_upstream_unavailable_wire(response)


async def test_message_full_single_mid_read_network_error_returns_503(upstream_factory):
    """Mid-read boundary: the initial send SUCCEEDS but the 200 body stream
    dies with an ``httpx.RequestError`` during ``read_with_cap`` → 503
    upstream_unavailable via the shared mapping (P0-9 catalog guard)."""
    def handler(request: httpx.Request) -> httpx.Response:
        async def broken_body():
            yield b'{"partial": '
            raise httpx.ReadError("mid-read boom")
        return httpx.Response(200, content=broken_body(),
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    response = await _get(upstream, "/slimapi/messages/ses_x/full/m1")
    _assert_upstream_unavailable_wire(response)


# ===========================================================================
# session_not_found — the two §7 mapping paths in upstream_errors.py
# (upstream 404 + sid → sid-scoped 404 session_not_found), locked at unit
# level including the exception-chaining contract the B3b session-not-found
# exception factory must preserve, plus one route-level manifestation.
# ===========================================================================


def test_raise_upstream_status_code_404_sid_is_session_not_found_no_cause():
    """Mapping path A (``raise_upstream_status_code`` — construction point
    WITHOUT a cause): 404 + sid → 404 session_not_found + sessionID field."""
    with pytest.raises(CodedHTTPException) as ei:
        raise_upstream_status_code(404, sid="ses_x")
    assert ei.value.status_code == 404
    assert ei.value.code == "session_not_found"
    assert ei.value.fields == {"sessionID": "ses_x"}
    assert ei.value.headers is None
    assert ei.value.__cause__ is None


def test_raise_upstream_status_404_sid_is_session_not_found_chained():
    """Mapping path B (``raise_upstream_status`` — construction point WITH a
    cause): 404 + sid → 404 session_not_found raised ``from exc``."""
    request = httpx.Request("GET", "http://127.0.0.1:4096/session/s1/message")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("404 Not Found", request=request,
                                response=response)
    with pytest.raises(CodedHTTPException) as ei:
        raise_upstream_status(exc, sid="ses_x")
    assert ei.value.status_code == 404
    assert ei.value.code == "session_not_found"
    assert ei.value.fields == {"sessionID": "ses_x"}
    assert ei.value.headers is None
    assert ei.value.__cause__ is exc


async def test_messages_list_upstream_404_maps_session_not_found(upstream_factory):
    """Route-level manifestation of mapping path A: an upstream 404 on the
    message-list fetch maps to the sid-scoped 404 ``session_not_found``
    (NOT verbatim passthrough, NOT 502)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    upstream = upstream_factory(handler)
    response = await _get(upstream, "/slimapi/messages/ses_x")
    assert response.status_code == 404
    assert response.content == b'{"code":"session_not_found","sessionID":"ses_x"}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["vary"] == "Accept-Encoding"
    assert "cache-control" not in response.headers



