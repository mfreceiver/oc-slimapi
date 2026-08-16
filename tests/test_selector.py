"""v3-contract §2 selector state machine (Batch A).

Exercises the SlimapiSelectorMiddleware end-to-end through httpx.ASGITransport
with a minimal app wiring selector + health + versions routers (mirrors the
production stack order: RequestId → Traffic → Selector(→gate) → routes, minus
the accounting layers that have their own tests in
test_access_log_v3_fields.py).
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware

V2_HEADER = {"X-Slimapi-Version": "2"}


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
        server_api_version=2,
        accepted_client_versions=(2, 3),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(*, v3_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="selector-test")
    app.add_middleware(
        SlimapiSelectorMiddleware,
        accepted_client_versions=(2, 3),
        v3_enabled=v3_enabled,
    )
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health.router)
    app.include_router(versions.router)

    @app.get("/passthrough")
    async def passthrough(request: Request):
        # Echo the RAW query string + scope state so tests can prove the
        # selector does not touch non-/slimapi paths (catch-all parity).
        return {
            "query": request.scope.get("query_string", b"").decode("latin-1"),
            "state": dict(request.scope.get("state") or {}),
        }

    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


# ---------------------------------------------------------------------------
# no `v` → v2 pipeline including the X-Slimapi-Version header gate
# ---------------------------------------------------------------------------

async def test_no_v_no_header_rejected_by_gate():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health")
        assert r.status_code == 400
        assert r.json()["code"] == "version_required"


async def test_no_v_valid_header_passes_gate():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health", headers=V2_HEADER)
        assert r.status_code == 200


async def test_no_v_header_3_now_accepted():
    """2.0.0 widens the accepted header range to [2, 3]."""
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health", headers={"X-Slimapi-Version": "3"})
        assert r.status_code == 200


async def test_no_v_out_of_range_header_rejected():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health", headers={"X-Slimapi-Version": "9"})
        assert r.status_code == 400
        body = r.json()
        assert body["code"] == "version_incompatible"
        assert body["client"] == 9
        assert body["accepted"] == [2, 3]


# ---------------------------------------------------------------------------
# v=2 explicit → same v2 pipeline (gate still applies)
# ---------------------------------------------------------------------------

async def test_v2_with_header_passes():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=2", headers=V2_HEADER)
        assert r.status_code == 200


async def test_v2_without_header_still_requires_header():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=2")
        assert r.status_code == 400
        assert r.json()["code"] == "version_required"


async def test_v2_view_body_fields():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=2", headers=V2_HEADER)
        assert r.status_code == 200
        body = r.json()
        assert body["slimapi_contract"] == 2
        assert body["server"]["api_version"] == 2
        assert body["schema"]["version"] == 2


# ---------------------------------------------------------------------------
# v=3 → v3 view; version header ignored when present
# ---------------------------------------------------------------------------

async def test_v3_without_header_bypasses_gate():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3")
        assert r.status_code == 200


async def test_v3_with_incompatible_header_still_ok():
    """§2: v=3 request with a simultaneously-present version header → header
    IGNORED, no error."""
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3", headers={"X-Slimapi-Version": "9"})
        assert r.status_code == 200


async def test_v3_with_valid_header_still_ok():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3", headers=V2_HEADER)
        assert r.status_code == 200


async def test_v3_view_body_fields():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3")
        assert r.status_code == 200
        body = r.json()
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        # synced — no 3/2 combination
        assert body["server"]["api_version"] == body["schema"]["version"]
        assert body["server"]["api_version"] == body["slimapi_contract"]


# ---------------------------------------------------------------------------
# lexical boundaries — every invalid form → 400 invalid_version_selector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "03", "+3", " 3", "3.0", "", "3a", "-3", "٣", "1e1"])
async def test_lexically_invalid_selector_rejected(bad):
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health", params={"v": bad})
        assert r.status_code == 400, f"v={bad!r} should be lexically invalid"
        body = r.json()
        assert body["code"] == "invalid_version_selector"


async def test_bare_v_flag_is_invalid():
    """`?v` (no `=`) parses to an empty value → lexically invalid."""
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v")
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_version_selector"


async def test_lexically_invalid_rejected_even_with_valid_header():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=03", headers=V2_HEADER)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_version_selector"


# ---------------------------------------------------------------------------
# multi-value: same value folds, differing values → invalid
# ---------------------------------------------------------------------------

async def test_multi_same_value_v3_folds():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3&v=3")
        assert r.status_code == 200
        assert r.json()["slimapi_contract"] == 3


async def test_multi_same_value_v2_folds():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=2&v=2", headers=V2_HEADER)
        assert r.status_code == 200


async def test_multi_differing_values_rejected():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3&v=2")
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_version_selector"
        r = await client.get("/slimapi/health?v=2&v=3")
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_version_selector"


async def test_multi_one_lexically_invalid_rejected():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=3&v=03")
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_version_selector"


# ---------------------------------------------------------------------------
# lexically valid but unsupported → 400 unsupported_version supported=[2,3]
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v", ["1", "4", "5", "10", "999999"])
async def test_unsupported_version_rejected(v):
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health", params={"v": v})
        assert r.status_code == 400
        body = r.json()
        assert body["code"] == "unsupported_version"
        assert body["supported"] == [2, 3]


async def test_unsupported_multi_same_folds_then_rejects():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=4&v=4")
        assert r.status_code == 400
        assert r.json()["code"] == "unsupported_version"


async def test_unsupported_rejected_even_with_valid_header():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/health?v=4", headers=V2_HEADER)
        assert r.status_code == 400
        assert r.json()["code"] == "unsupported_version"


# ---------------------------------------------------------------------------
# GET /slimapi/versions — unconditional exemption; 405 priority
# ---------------------------------------------------------------------------

async def test_versions_get_exempt_from_gate_and_selector():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/versions")
        assert r.status_code == 200
        assert r.json()["current"] == 3


async def test_versions_get_exempt_with_bad_selector():
    """Exemption is unconditional — even ?v=0 (would-be invalid) passes."""
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/versions?v=0")
        assert r.status_code == 200
        r = await client.get("/slimapi/versions?v=99")
        assert r.status_code == 200


async def test_versions_get_exempt_with_bad_header():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/versions", headers={"X-Slimapi-Version": "99"})
        assert r.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def test_versions_non_get_405_with_allow_header(method):
    async with _client(_build_app()) as client:
        r = await client.request(method, "/slimapi/versions")
        assert r.status_code == 405
        assert r.headers.get("allow") == "GET"


async def test_versions_405_priority_over_selector():
    """405 wins over selector judgement (?v=abc would be a 400)."""
    async with _client(_build_app()) as client:
        r = await client.post("/slimapi/versions?v=abc")
        assert r.status_code == 405
        assert r.headers.get("allow") == "GET"


async def test_versions_405_priority_over_gate():
    """405 wins over the version header gate (no header would be a 400)."""
    async with _client(_build_app()) as client:
        r = await client.post("/slimapi/versions")
        assert r.status_code == 405


# ---------------------------------------------------------------------------
# v3_selector_enabled=false → v=3 downgraded to v2 pipeline + absent view
# ---------------------------------------------------------------------------

async def test_v3_disabled_runs_v2_pipeline():
    async with _client(_build_app(v3_enabled=False)) as client:
        # No header → gate 400 (v2 pipeline applies).
        r = await client.get("/slimapi/health?v=3")
        assert r.status_code == 400
        assert r.json()["code"] == "version_required"
        # With header → 200 but the VIEW stays v2 (selector disabled).
        r = await client.get("/slimapi/health?v=3", headers=V2_HEADER)
        assert r.status_code == 200
        assert r.json()["slimapi_contract"] == 2


async def test_v3_disabled_still_rejects_invalid_lexical():
    """Disabled selector = full rollback: `v` ignored entirely, no 400s."""
    async with _client(_build_app(v3_enabled=False)) as client:
        r = await client.get("/slimapi/health?v=03", headers=V2_HEADER)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# catch-all (non /slimapi) zero-touch: ?v=2/3 forwarded verbatim
# ---------------------------------------------------------------------------

async def test_non_slimapi_query_forwarded_verbatim():
    async with _client(_build_app()) as client:
        for qs in ("v=2", "v=3", "v=2&v=3", "v=0&directory=/a"):
            r = await client.get(f"/passthrough?{qs}")
            assert r.status_code == 200, qs
            body = r.json()
            assert body["query"] == qs


async def test_non_slimapi_no_gate_no_selector():
    async with _client(_build_app()) as client:
        r = await client.get("/passthrough")
        assert r.status_code == 200
        state = r.json()["state"]
        sel = state.get("slimapi_selector") or {}
        assert sel.get("result") == "not_applicable"
        assert sel.get("wire") is None
        assert state.get("slimapi_directory_form") is None


# ---------------------------------------------------------------------------
# path normalisation (P1-14 parity): //slimapi/health still gated
# ---------------------------------------------------------------------------

async def test_double_slash_health_still_gated():
    """P1-14 parity: ``//slimapi/health`` collapses for the gate decision.
    Absolute URL so httpx does not treat the leading ``//`` as a netloc.
    With a valid header the gate PASSES; this minimal app has no catch-all
    proxy so the raw path then misses the router → 404 (production would
    normalise + forward through the proxy)."""
    async with _client(_build_app()) as client:
        r = await client.get("http://test//slimapi/health")
        assert r.status_code == 400
        assert r.json()["code"] == "version_required"
        r = await client.get("http://test//slimapi/health", headers=V2_HEADER)
        assert r.status_code == 404  # gate passed; router miss (no proxy here)
