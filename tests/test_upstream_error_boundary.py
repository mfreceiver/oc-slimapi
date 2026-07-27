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

import asyncio

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.observability import BatchLedger
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, sessions
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.upstream_errors import fetch_json_mapped

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
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
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
    app.include_router(sessions.router)
    app.include_router(messages.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


async def _get(upstream: httpx.AsyncClient, path: str) -> httpx.Response:
    """Helper: build app + ASGI client, issue a GET, return the response."""
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=VERSION_HEADERS)


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
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


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
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


# ===========================================================================
# fetch_json_mapped(expect=...) extension — Batch 3 prerequisite (rev H)
#
# Contract §2 ("children 投影与缓存") requires the children endpoint to call
# ``fetch_json_mapped(..., expect=list)`` so that an upstream 200 returning
# a non-list JSON body (dict / string / number / null) is mapped to 503
# ``upstream_unavailable`` (§7) rather than crashing or silently iterating
# the dict. The current ``fetch_json_mapped`` hard-codes
# ``isinstance(payload, dict)`` (upstream_errors.py:54) — it has no
# ``expect`` parameter yet.
#
# These tests are TDD for the additive ``expect: type = dict`` parameter
# (default ``dict`` keeps all existing callers unchanged; children path
# passes ``expect=list``). Each test is marked ``xfail(strict=False)``:
# until the parameter is added these fail with a TypeError ("unexpected
# keyword argument 'expect'"); once Batch 3 lands it they flip to XPASS
# and the markers can be removed.
#
# Default-dict behaviour is verified separately by the EXISTING tests above
# — those cover the unmodified default path and MUST stay green regardless.
# ===========================================================================


async def test_fetch_json_mapped_expect_list_accepts_list_payload(upstream_factory):
    """``fetch_json_mapped(..., expect=list)`` accepts a JSON list payload and
    returns it verbatim (no 503)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'[{"id":"a"},{"id":"b"}]',
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    payload = await fetch_json_mapped(
        upstream, "/session/p1/children", expect=list,
    )
    assert payload == [{"id": "a"}, {"id": "b"}]


@pytest.mark.parametrize(
    ("body", "label"),
    [
        (b'{"k":"v"}', "dict"),
        (b'"a string"', "string"),
        (b"42", "number"),
        (b"null", "null"),
    ],
    ids=["dict", "string", "number", "null"],
)
async def test_fetch_json_mapped_expect_list_rejects_non_list_payload(
    upstream_factory, body, label,
):
    """``fetch_json_mapped(..., expect=list)`` MUST raise
    ``CodedHTTPException(503, code="upstream_unavailable")`` for every
    non-list JSON top-level shape (dict / string / number / null). Each
    shape is parameterised so under ``xfail(strict=False)`` each reports
    its own line and flips independently when the fix lands."""
    from oc_slimapi.errors import CodedHTTPException

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body,
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    with pytest.raises(CodedHTTPException) as ei:
        await fetch_json_mapped(
            upstream, "/session/p1/children", expect=list,
        )
    assert ei.value.status_code == 503, f"shape={label!r}"
    assert ei.value.code == "upstream_unavailable", f"shape={label!r}"


async def test_fetch_json_mapped_default_dict_unchanged_with_expect_kwarg(upstream_factory):
    """Default behaviour MUST be unchanged when ``expect`` is added: omitting
    it (or passing ``expect=dict``) keeps the current dict-only contract
    that the rest of the codebase relies on. A non-dict payload MUST still
    raise 503."""
    from oc_slimapi.errors import CodedHTTPException

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'[1,2,3]',
            headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    # No expect kwarg → default dict behaviour → list payload rejected.
    with pytest.raises(CodedHTTPException) as ei_default:
        await fetch_json_mapped(upstream, "/session/status")
    assert ei_default.value.status_code == 503
    assert ei_default.value.code == "upstream_unavailable"

    # Explicit expect=dict → same behaviour as default.
    def handler2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'[1,2,3]',
            headers={"Content-Type": "application/json"},
        )

    upstream2 = upstream_factory(handler2)
    with pytest.raises(CodedHTTPException) as ei_explicit:
        await fetch_json_mapped(upstream2, "/session/status", expect=dict)
    assert ei_explicit.value.status_code == 503
    assert ei_explicit.value.code == "upstream_unavailable"
