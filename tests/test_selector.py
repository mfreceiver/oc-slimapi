"""v3-contract §2 selector state machine — **terminal state**.

Exercises the SlimapiSelectorMiddleware end-to-end through httpx.ASGITransport
with a minimal app wiring selector + health + versions routers (mirrors the
production stack order: RequestId → Traffic → Selector → routes, minus the
accounting layers that have their own tests in test_access_log_v3_fields.py).

Terminal semantics under test (dual-version window: ``?v=4`` also admitted):

* ``?v=3`` / ``?v=4`` are the admitted pipelines; the ``X-Slimapi-Version``
  header is never read (any value, present or absent, changes nothing).
* no ``v`` / ``v=2`` / unsupported → 400 ``unsupported_version`` [3, 4].
* lexical garbage / differing multi-value → 400 ``invalid_version_selector``.
* ``GET /slimapi/versions`` exempt; non-GET → 405 (+``Allow: GET``) with
  priority above the selector.
* non-/slimapi paths: zero-touch passthrough.

B12（2026-08-21 v4 自包含 golden 化清点）：本文件**无**「先发 ?v=3 求
期望再比 v4」形态——所有断言本就内联字面；``?v=3`` admitted 半区
（test_v3_without_header_ok 等）是 v3 守护网（三分处置②，Phase 4 v3 面
拆除前保留），v4 admitted 的正向覆盖在 tests/test_v4_dual_window.py。
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

VERSION_HEADER = {"X-Slimapi-Version": "2"}


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


def _build_app() -> FastAPI:
    app = FastAPI(title="selector-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health.router)
    app.include_router(versions.router)

    @app.get("/passthrough")
    async def passthrough(request: Request):
        # Echo the RAW query string + scope state so tests can prove the
        # selector does not touch non-/slimapi paths.
        return {
            "query": request.scope.get("query_string", b"").decode("latin-1"),
            "state": dict(request.scope.get("state") or {}),
        }

    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# §2 退役后 — the retired-version requests
# ---------------------------------------------------------------------------

async def test_no_v_is_unsupported_version():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health")
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


async def test_no_v_with_header_still_unsupported():
    """The header is never read — it cannot substitute the selector."""
    app = _build_app()
    async with _client(app) as client:
        for headers in (VERSION_HEADER, {"X-Slimapi-Version": "3"},
                        {"X-Slimapi-Version": "9"}):
            resp = await client.get("/slimapi/health", headers=headers)
            assert resp.status_code == 400, headers
            assert resp.json() == {"code": "unsupported_version",
                                   "supported": [4]}


async def test_v2_explicit_is_unsupported_version():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health", params={"v": "2"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


async def test_v2_with_header_also_unsupported():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health", params={"v": "2"},
                                headers=VERSION_HEADER)
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


# ---------------------------------------------------------------------------
# v3 retired (2026-08-21 narrowing) — the former admission half of the
# dual-view matrix now answers the unsupported-version 400 face. V2b scope:
# consolidate/delete the v3 half once the guard net goes.
# ---------------------------------------------------------------------------

async def test_v3_without_header_ok():
    """v3 was the 3.x terminal admission view; since the 2026-08-21 window
    narrowing (shipped 4.8.0) it answers the unsupported-version 400."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health", params={"v": "3"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


async def test_v3_with_any_header_ignored():
    """§1: the retired header is not read — it cannot rescue a ?v=3 request
    across the window narrowing either."""
    app = _build_app()
    async with _client(app) as client:
        for value in ("2", "3", "9", "garbage"):
            resp = await client.get("/slimapi/health", params={"v": "3"},
                                    headers={"X-Slimapi-Version": value})
            assert resp.status_code == 400, value
            assert resp.json() == {"code": "unsupported_version", "supported": [4]}


# ---------------------------------------------------------------------------
# lexical rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad", ["0", "03", "+3", "%203", "3.0", "3a", "1e1", "-3", "٣"]
)
async def test_lexically_invalid_selector_rejected(bad):
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(f"/slimapi/health?v={bad}")
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


async def test_bare_v_flag_is_invalid():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=")
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


async def test_lexically_invalid_rejected_even_with_header():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=03", headers=VERSION_HEADER)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


async def test_multi_same_value_folds():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=4&v=4")
        assert resp.status_code == 200


async def test_multi_differing_values_rejected():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=3&v=2")
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


async def test_multi_one_lexically_invalid_rejected():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=3&v=03")
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


# ---------------------------------------------------------------------------
# unsupported versions
# ---------------------------------------------------------------------------

# Version-window narrowing (2026-08-21): 4 is the only admitted version;
# the unsupported set is everything outside {4} — including the retired 3.
@pytest.mark.parametrize("v", ["1", "2", "3", "5", "10", "999999"])
async def test_unsupported_version_rejected(v):
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(f"/slimapi/health?v={v}")
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


async def test_unsupported_multi_same_rejects():
    # v=4&v=4 folds + routes (v4-only window) — the unsupported-same-repeat
    # semantics are pinned with a value outside {4}.
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=5&v=5")
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


async def test_unsupported_rejected_even_with_valid_header():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=5",
                                headers={"X-Slimapi-Version": "3"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}


# ---------------------------------------------------------------------------
# /versions exemption + 405 priority
# ---------------------------------------------------------------------------

async def test_versions_get_exempt_from_selector():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/versions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] == [4]
        assert body["current"] == 4


async def test_versions_get_exempt_with_bad_selector():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/versions?v=0")
        assert resp.status_code == 200
        assert resp.json()["available"] == [4]


async def test_versions_get_exempt_with_bad_header():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/versions",
                                headers={"X-Slimapi-Version": "9"})
        assert resp.status_code == 200


@pytest.mark.parametrize(
    "method", ["post", "put", "delete", "patch", "head", "options"]
)
async def test_versions_non_get_405_with_allow_header(method):
    app = _build_app()
    async with _client(app) as client:
        resp = await getattr(client, method)("/slimapi/versions")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET"


async def test_versions_405_priority_over_selector():
    """405 wins even over a lexically broken selector value."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.post("/slimapi/versions?v=abc")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET"


# ---------------------------------------------------------------------------
# non-/slimapi passthrough (zero-touch)
# ---------------------------------------------------------------------------

async def test_non_slimapi_query_forwarded_verbatim():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/passthrough?v=3&v=2&directory=/w&a=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "v=3&v=2&directory=/w&a=1"
        assert body["state"]["slimapi_selector"] == {
            "result": "not_applicable", "wire": None}
        assert body["state"]["slimapi_directory_form"] is None


async def test_non_slimapi_no_selector_decision():
    """No v on a non-slimapi path is NOT a version error (not our domain)."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/passthrough")
        assert resp.status_code == 200
        assert resp.json()["state"]["slimapi_selector"] == {
            "result": "not_applicable", "wire": None}


async def test_double_slash_health_still_judged():
    """Slash-collapse parity: //slimapi/health cannot bypass the selector."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("http://t//slimapi/health")
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [4]}
        resp = await client.get("http://t//slimapi/health?v=4")
        # The selector ADMITTED the request (no 400) — routing in this
        # minimal stack has no catch-all, so the un-normalised // path is a
        # route miss (404). The point under test: the selector judged the
        # path; it did not bypass.
        assert resp.status_code == 404
