"""v3-contract §3 — GET /slimapi/versions discovery endpoint (terminal)."""
from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi import __version__
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import versions
from oc_slimapi.selector import SlimapiSelectorMiddleware


def _build_app() -> FastAPI:
    app = FastAPI(title="versions-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.include_router(versions.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


async def test_versions_shape_exact():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/versions")
        assert r.status_code == 200
        body = r.json()
        # Field ORDER follows contract §3 verbatim (producer-owned shape).
        assert list(body.keys()) == [
            "current", "available", "capabilities", "sidecarVersion",
        ]
        # Dual-version window (v4-contract §3.1): current=4, available=[3, 4]
        assert body["current"] == 4
        assert body["available"] == [3, 4]
        # current ∈ available; available unique ascending
        assert body["current"] in body["available"]
        assert body["available"] == sorted(set(body["available"]))


async def test_versions_capabilities_map():
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        caps = body["capabilities"]
        assert set(caps.keys()) == {"3", "4"}
        # B3a-A3: capabilities["4"] carries exactly the two static keys;
        # sseReplay / qpImmediateFull are deliberately absent (B3b owns them).
        assert caps["4"] == {"globalSessions": True, "auxiliaryFilters": True}
        assert caps["3"]["envelope"] == ["messages", "sessions"]
        assert caps["3"]["directoryQuery"] is True
        assert caps["3"]["versionHeaderOptional"] is True
        assert caps["3"]["writeRoutes"] is True
        assert caps["3"]["readRoutes"] == [
            "file", "vcs", "find", "providers",
            "sessionSingle", "activeSessions", "globalHealth",
        ]


async def test_versions_sidecar_version_is_package_version():
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        assert body["sidecarVersion"] == __version__
        assert isinstance(body["sidecarVersion"], str)
        assert body["sidecarVersion"]  # non-empty


async def test_versions_cache_control_no_store_and_no_etag():
    async with _client(_build_app()) as client:
        r = await client.get("/slimapi/versions")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"
        assert "etag" not in r.headers


async def test_versions_gzip_family():
    """json_response negotiation family: gzip when accepted + Vary always."""
    async with _client(_build_app()) as client:
        r = await client.get(
            "/slimapi/versions", headers={"Accept-Encoding": "gzip"}
        )
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert r.headers.get("vary") == "Accept-Encoding"
        # httpx transparently decodes; the JSON still round-trips.
        assert r.json()["current"] == 4
        # Without gzip (explicit identity — httpx auto-adds gzip otherwise)
        # → identity body, Vary still present.
        r = await client.get("/slimapi/versions", headers={"Accept-Encoding": "identity"})
        assert r.headers.get("content-encoding") is None
        assert r.headers.get("vary") == "Accept-Encoding"


async def test_versions_unknown_fields_tolerated_by_consumer():
    """Forward-compat: the producer may add fields; consumers ignore them.
    Simulated by asserting the response parses and the known subset holds
    regardless of any extra keys (nothing is rejected client-side here —
    the tolerance requirement is on consumers, this endpoint is producer)."""
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        assert body["current"] == 4
        assert body["available"] == [3, 4]
