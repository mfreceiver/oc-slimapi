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
        # v4-only window (2026-08-21 narrowing, v4-contract §3.1 revision):
        # current=4, available=[4]
        assert body["current"] == 4
        assert body["available"] == [4]
        # current ∈ available; available unique ascending
        assert body["current"] in body["available"]
        assert body["available"] == sorted(set(body["available"]))


async def test_versions_capabilities_map():
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        caps = body["capabilities"]
        # 2026-08-21 narrowing: the capability map is the v4-only face —
        # the "3" key left with the version window.
        assert set(caps.keys()) == {"4"}
        # B3b-5 + 2026-08-19 revision §3.3: capabilities["4"] carries the
        # four STATIC keys — B3a's globalSessions/auxiliaryFilters plus the
        # same-batch-advertised sseReplay (n1 frozen timing: B3a shipped "4"
        # without them, so the absence was the B3a-期 wire face) and
        # qpImmediateFull (semantics frozen as "already true",
        # design-v4-qp-payload) — followed by the ADDITIVE readiness gate
        # (§3.3) and, since the 4.2.0 close-out, the §14 expand block. The
        # four static keys are locked by value here; the readiness/expand
        # shapes are locked in test_versions_readiness.py.
        assert caps["4"]["globalSessions"] is True
        assert caps["4"]["auxiliaryFilters"] is True
        assert caps["4"]["sseReplay"] is True
        assert caps["4"]["qpImmediateFull"] is True
        # 4.11.0 revision five: two more static booleans advertised
        # same-batch with their implementations (§10.3 since differential,
        # §19 file/raw).
        assert caps["4"]["messagesSince"] is True
        assert caps["4"]["fileRaw"] is True
        # 4.2.0 close-out: readiness + expand both land (SATISFIED is the
        # full universe); shapes locked in test_versions_readiness.py.
        assert set(caps["4"].keys()) == {
            "globalSessions", "auxiliaryFilters",
            "sseReplay", "qpImmediateFull",
            "messagesSince", "fileRaw",
            "readiness", "expand",
        }


async def test_versions_caps4_meta_lane_same_source():
    """B3b-5: the versions lane and the v4 SSE meta lane cannot drift —
    every capability the meta first-frame advertises must also be
    advertised (same value) by capabilities["4"]. The meta summary is a
    stream-scoped subset (per-stream keys only), so the assertion is a
    keyed subset check, not equality."""
    from oc_slimapi.sse.replay_wire import META_CAPABILITY_KEYS

    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        caps4 = body["capabilities"]["4"]
        assert META_CAPABILITY_KEYS == {"sseReplay": True}
        for key, value in META_CAPABILITY_KEYS.items():
            assert key in caps4, f"meta advertises {key!r} but versions does not"
            assert caps4[key] == value
        # Same-source constant: the endpoint spreads it verbatim.
        assert caps4["sseReplay"] is META_CAPABILITY_KEYS["sseReplay"] is True


async def test_versions_caps4_static_key_order():
    """Producer-owned key order follows contract §3.1 verbatim (the six
    static keys in their frozen order, then the §3.3 readiness gate and
    the §14 expand block — consumers must not rely on it, but the
    producer shape stays byte-stable for golden comparisons)."""
    async with _client(_build_app()) as client:
        body = (await client.get("/slimapi/versions")).json()
        assert list(body["capabilities"]["4"].keys()) == [
            "globalSessions", "auxiliaryFilters",
            "sseReplay", "qpImmediateFull",
            "messagesSince", "fileRaw",
            "readiness", "expand",
        ]


async def test_versions_caps4_static_face_no_runtime_keys():
    """§3.1 static-key principle: the six §3.1 static keys of
    capabilities["4"] never carry runtime-injected values and every
    advertised value is a literal boolean True — replay-log configuration
    or DB state must not bleed into the advertisement. (2026-08-19
    revision: the face also carries the module-constant-derived readiness
    gate and — since the 4.2.0 close-out, messages.expand.v4 satisfied —
    the §14 expand block; 4.11.0 adds the messagesSince/fileRaw static
    booleans.)"""
    async with _client(_build_app()) as client:
        caps = (await client.get("/slimapi/versions")).json()["capabilities"]
        for key in ("globalSessions", "auxiliaryFilters", "sseReplay",
                    "qpImmediateFull", "messagesSince", "fileRaw"):
            assert caps["4"][key] is True
        assert set(caps["4"]) == {
            "globalSessions", "auxiliaryFilters",
            "sseReplay", "qpImmediateFull",
            "messagesSince", "fileRaw",
            "readiness", "expand",
        }


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
        assert body["available"] == [4]
