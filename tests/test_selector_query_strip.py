"""v3-contract §2/§5.2 — selector consumes & strips ``?v`` on ``/slimapi/**``.

``v`` is a sidecar-reserved parameter: after the selector judges it, the
``v`` parameter pairs are REMOVED from the downstream ``query_string`` and
never forwarded — while every remaining parameter keeps its original bytes
(encoding, order, repeats, empty segments, trailing separators) verbatim.

Terminal note: only ``?v=4`` forwards at all (every other form 400s before
stripping matters); the strip tests below ride the admitted path.

Scope (§2): stripping happens on ``/slimapi/**`` only — non-slim paths keep
the raw query byte-identical (re-asserted here once for the strip context).
"""
from __future__ import annotations

import logging

import httpx
import pytest
from httpx import ASGITransport
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from oc_slimapi.selector import SlimapiSelectorMiddleware


async def _echo(request):
    """Echo the RAW downstream query bytes (what a route would see)."""
    qs = (request.scope.get("query_string") or b"").decode("latin-1")
    return JSONResponse({"qs": qs})


def _app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/slimapi/probe", _echo),
            Route("/slimapi/versions", _echo),  # same normalized path as the
            # real discovery endpoint — exercises the exemption branch.
            Route("/probe", _echo),  # non-/slimapi stand-in
        ]
    )
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


async def _get(app, path, headers=None) -> dict:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=headers)
    return {"status": response.status_code, "qs": response.json().get("qs")}


# ---------------------------------------------------------------------------
# /slimapi/** — v stripped, everything else byte-identical
# ---------------------------------------------------------------------------


async def test_v3_strips_all_v_pairs_preserving_bytes():
    out = await _get(_app(), "/slimapi/probe?v=4&a=1&v=4&b=%20x")
    assert out["status"] == 200
    # duplicate v pairs both removed; a→b order, %20 encoding preserved.
    assert out["qs"] == "a=1&b=%20x"


async def test_rejected_requests_do_not_reach_routes():
    """A 400 (here: v=2, retired) never forwards — nothing is stripped or
    echoed because the route never runs."""
    out = await _get(_app(), "/slimapi/probe?v=2&a=1")
    assert out["status"] == 400
    assert out["qs"] is None


async def test_versions_exemption_still_strips_v():
    # GET /slimapi/versions is exempt from the selector judgement — but §5.2
    # stripping is unconditional on /slimapi/**; ?v=99 is NOT exempted from
    # consumption.
    out = await _get(_app(), "/slimapi/versions?v=99&a=2")
    assert out["status"] == 200
    assert out["qs"] == "a=2"


async def test_lookalike_keys_not_stripped():
    out = await _get(_app(), "/slimapi/probe?vv=1&av=9&v=4")
    assert out["status"] == 200
    assert out["qs"] == "vv=1&av=9"


async def test_percent_encoded_v_key_is_consumed_like_v():
    # parse_qsl (selector judgement) decodes %76 → "v"; the strip must agree,
    # else the judged parameter would leak downstream.
    out = await _get(_app(), "/slimapi/probe?%76=4&a=1")
    assert out["status"] == 200  # judged as v=4 → admitted
    assert out["qs"] == "a=1"


async def test_plus_and_percent_values_untouched():
    out = await _get(_app(), "/slimapi/probe?v=4&b=a+b&c=%2B1")
    assert out["status"] == 200
    assert out["qs"] == "b=a+b&c=%2B1"


async def test_empty_segments_and_trailing_separator_preserved():
    out = await _get(_app(), "/slimapi/probe?a=1&&b=2&v=4&")
    assert out["status"] == 200
    assert out["qs"] == "a=1&&b=2&"


# ---------------------------------------------------------------------------
# non-/slimapi — zero stripping (§2 scope)
# ---------------------------------------------------------------------------


async def test_catchall_query_untouched():
    out = await _get(_app(), "/probe?v=4&a=1&v=2")
    assert out["status"] == 200
    assert out["qs"] == "v=4&a=1&v=2"


# ---------------------------------------------------------------------------
# functional smoke on the real app surface (v present + consumed, route OK)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["?v=4", "?v=4&x=1"],
)
async def test_health_functional_with_v(query):
    # Real-route smoke: v is consumed; health answers the single v3 view and
    # never chokes on the (stripped) residual query.
    from tests.test_access_log_v3_fields import _build_app  # noqa: F401

    app = _build_app(logging.getLogger("oc_slimapi.test.capture"))
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/slimapi/health{query}")
    assert response.status_code == 200
    assert response.json()["slimapi_contract"] == 4
