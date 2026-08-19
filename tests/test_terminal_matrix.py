"""v3-contract **terminal state** (sidecar 3.0.0 / M3) wire matrix —
dual-version window update (4.0.0 / B3a): supported set is now [3, 4].

The v2 pipeline is deleted. Frozen terminal clauses under test:

* §2 退役后: no ``v`` / ``v=2`` → 400 ``{"code":"unsupported_version",
  "supported":[3, 4]}`` (endpoint exists, version retired — never a silent
  404). ``v=3`` unchanged. Lexical garbage → ``invalid_version_selector``.
* §1 header retirement: ``X-Slimapi-Version`` is never read (any value,
  any presence); ``X-Opencode-Directory`` on a §5.3 consuming route →
  400 ``directory_header_retired`` (§5.7).
* §8.3 terminal error priority: ①405 → ②selector 400 → ③directory 400
  (multi-value → dual-present conflict → retired header) → ④404.
* §8.2 catch-all closed: every uncollected path → 404
  ``thin_route_not_found`` (no upstream call, no consumption).
* §3/§3a: ``available:[3]``, capabilities keyed "3" only; health single
  v3 view (contract/api_version/schema.version == 3, accepted [3,3]).
* §4 envelope always (messages/sessions lists); ``X-Next-Cursor`` /
  ``X-Complete`` never produced (§1).
* §6.2 Vary shrink: every JSON route ``Vary: Accept-Encoding`` only.
* §7 SSE: meta first frame always; ``X-Slimapi-Subscriber-ID`` never
  produced; no-``v`` SSE rejected before the stream opens.
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import (
    agent,
    health,
    messages,
    read_groups,
    sessions,
    token_stream,
    versions,
)
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
VERSION_HEADER = "X-Slimapi-Version"
DIRECTORY_HEADER = "X-Opencode-Directory"
V3 = {"v": "3"}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _message_payload() -> bytes:
    return orjson.dumps([
        {"info": {"id": "m1", "role": "user", "time": {"created": 1}},
         "parts": [{"id": "p1", "type": "text", "messageID": "m1",
                    "text": "hello"}]},
    ])


def _sessions_payload() -> bytes:
    return orjson.dumps([{"id": "s1", "title": "one"}])


def _build_app(handler, *, settings: Settings | None = None):
    """Full-ish app: selector + core routers + catch-all + mock upstream."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream)
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes))
    for router in (health.router, versions.router, sessions.router,
                   messages.router, agent.router, token_stream.router,
                   read_groups.router):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app, seen


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


# ---------------------------------------------------------------------------
# §2 selector terminal state machine
# ---------------------------------------------------------------------------

async def test_no_v_is_unsupported_version():
    app, seen = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version",
                               "supported": [3, 4]}
    assert seen == []  # never forwarded


async def test_v2_explicit_is_unsupported_version():
    app, seen = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "2"}, headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version",
                               "supported": [3, 4]}
    assert seen == []


async def test_version_header_never_read():
    """§1: the header is not read — no version_required, no gating, no
    error on any value; only ?v=3 decides."""
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        for header_value in ("2", "3", "9", "garbage"):
            resp = await client.get(
                "/slimapi/sessions", params={"v": "3"},
                headers={**IDENTITY, VERSION_HEADER: header_value})
            assert resp.status_code == 200, header_value
        # header alone (no v) still 400 — the header cannot substitute v.
        resp = await client.get("/slimapi/sessions",
                                headers={**IDENTITY, VERSION_HEADER: "3"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "unsupported_version"


@pytest.mark.parametrize("bad", ["0", "03", "+3", " 3", "3.0", "", "3a", "1e1"])
async def test_lexically_invalid_v_400(bad):
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get(f"/slimapi/sessions?v={bad}", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


@pytest.mark.parametrize("unsupported", ["1", "2", "5", "10", "999999"])
async def test_unsupported_v_reports_supported_3(unsupported):
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get(f"/slimapi/sessions?v={unsupported}",
                                headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version",
                               "supported": [3, 4]}


async def test_multi_value_same_folds_v3():
    app, seen = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions?v=3&v=3", headers=IDENTITY)
        assert resp.status_code == 200
    assert "v=" not in seen[0].url.query.decode("latin-1")  # v consumed


async def test_multi_value_different_400():
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions?v=3&v=2", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


# ---------------------------------------------------------------------------
# §8.3 terminal error priority chain
# ---------------------------------------------------------------------------

async def test_priority_405_over_everything():
    """① non-GET /versions → 405 even with lexically broken v."""
    app, seen = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.post("/slimapi/versions?v=abc",
                                 headers=IDENTITY)
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET"
        resp = await client.post("/slimapi/versions", headers=IDENTITY)
        assert resp.status_code == 405
    assert seen == []


async def test_priority_selector_over_directory():
    """② selector 400 wins over ③ directory 400 (v broken + header
    retired / conflict forms co-present)."""
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        # no v + retired header form → unsupported_version (not retired)
        resp = await client.get(
            "/slimapi/sessions", headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.json()["code"] == "unsupported_version"
        # lexically broken v + dual-present conflict → invalid_version_selector
        resp = await client.get(
            "/slimapi/sessions", params={"v": "03", "directory": "/a"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/b"})
        assert resp.json()["code"] == "invalid_version_selector"
        # v=2 (retired) + header → unsupported_version
        resp = await client.get(
            "/slimapi/sessions", params={"v": "2"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.json()["code"] == "unsupported_version"


async def test_priority_directory_chain_within_v3():
    """③ directory chain (§8.3): multi-value → dual-present conflict →
    retired header — each evaluated only after the previous passes."""
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        # multi-value different wins over conflict/retired co-presence
        resp = await client.get(
            "/slimapi/sessions",
            params={"v": "3", "directory": ["/a", "/b"]},
            headers={**IDENTITY, DIRECTORY_HEADER: "/c"})
        assert resp.json()["code"] == "invalid_directory_selector"
        # dual-present different → directory_conflict (before retired)
        resp = await client.get(
            "/slimapi/sessions", params={"v": "3", "directory": "/a"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/b"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "directory_conflict"
        assert body["queryDirectory"] == "/a"
        assert body["headerDirectory"] == "/b"
        # header alone → retired
        resp = await client.get(
            "/slimapi/sessions", params={"v": "3"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "directory_header_retired"}
        # dual-present normalized SAME → still retired (header presence)
        resp = await client.get(
            "/slimapi/sessions", params={"v": "3", "directory": "/w"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.json() == {"code": "directory_header_retired"}


async def test_blank_directory_header_is_retired():
    """§5.7 (M3-1): the retirement judgement is header PRESENCE, not a
    non-empty value — an empty or whitespace-only ``X-Opencode-Directory``
    on a consuming route is still retired input."""
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        for blank_value in ("", "   ", "\t "):
            resp = await client.get(
                "/slimapi/sessions", params={"v": "3"},
                headers={**IDENTITY, DIRECTORY_HEADER: blank_value})
            assert resp.status_code == 400, blank_value
            assert resp.json() == {"code": "directory_header_retired"}, (
                blank_value)


async def test_priority_selector_over_route_miss():
    """② selector 400 evaluated before ④ route miss: an unknown /slimapi
    path without v reports the version error, not 404."""
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/does/not/exist", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json()["code"] == "unsupported_version"
        resp = await client.get("/slimapi/does/not/exist",
                                params={"v": "3"}, headers=IDENTITY)
        assert resp.status_code == 404
        assert resp.json() == {"code": "thin_route_not_found"}


# ---------------------------------------------------------------------------
# §5.7 directory header retirement (consuming set)
# ---------------------------------------------------------------------------

async def test_directory_header_retired_on_consuming_routes():
    """Header on any consuming route → 400 directory_header_retired,
    including the read groups (§10.a) — query stays the only channel."""
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        for path in ("/slimapi/sessions", "/slimapi/messages/s1",
                     "/slimapi/agent", "/slimapi/file",
                     "/slimapi/vcs", "/slimapi/session/s1",
                     "/slimapi/session/s1/command"):
            resp = await client.get(
                path, params={"v": "3"},
                headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
            assert resp.status_code == 400, path
            assert resp.json() == {"code": "directory_header_retired"}, path


async def test_directory_query_still_works_and_strips():
    """The canonical ?directory= channel is unchanged under v3."""
    app, seen = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions", params={"v": "3", "directory": "/w"},
            headers=IDENTITY)
        assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"
    assert "directory" not in seen[0].url.query.decode("latin-1")


async def test_directory_tolerant_routes_ignore_header():
    """§5.5 tolerant set never consumes — header presence is not an
    error there (not in the consuming set ⇒ §5.7 does not apply)."""
    app, seen = _build_app(lambda r: httpx.Response(
        200, content=orjson.dumps({"healthy": True}),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/global/health", params={"v": "3"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 200
        assert seen[0].headers.get(DIRECTORY_HEADER) is None
        assert seen[0].url.query.decode("latin-1") == ""


async def test_stream_directory_terminal():
    """§5.6 stream under terminal: query-only accepted no-op;
    header presence → directory_header_retired; dual-present different
    → directory_conflict (§8.3 chain, endpoint guard pre-empted)."""
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions/s1/stream", params={"v": "3"},
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "directory_header_retired"}


# ---------------------------------------------------------------------------
# §8.2 catch-all closed
# ---------------------------------------------------------------------------

async def test_catch_all_closed_404_no_upstream_call():
    app, seen = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        for method in ("get", "post", "put", "patch", "delete", "head",
                       "options"):
            resp = await getattr(client, method)(
                "/session/abc",
                params={"v": "3", "directory": "/w"},
                headers=IDENTITY)
            assert resp.status_code == 404, method
            if method != "head":  # HEAD carries no body
                assert resp.json() == {"code": "thin_route_not_found"}
    assert seen == []  # never forwarded upstream


async def test_catch_all_sse_paths_closed():
    """/event and /global/event are no longer proxied — 404."""
    app, seen = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        for path in ("/event", "/global/event"):
            resp = await client.get(path, headers=IDENTITY)
            assert resp.status_code == 404, path
            assert resp.json() == {"code": "thin_route_not_found"}
    assert seen == []


# ---------------------------------------------------------------------------
# §3/§3a discovery + health single view
# ---------------------------------------------------------------------------

async def test_versions_terminal_shape():
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        # exempt: reachable without any v
        resp = await client.get("/slimapi/versions", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        # dual-version window: current=4, available=[3, 4] (v4-contract §3.1)
        assert body["current"] == 4
        assert body["available"] == [3, 4]
        assert set(body["capabilities"].keys()) == {"3", "4"}
        assert body["capabilities"]["3"]["directoryQuery"] is True
        # four static §3.1 keys by value + the additive §3.3 readiness key
        # and §14 expand block (2026-08-19 revision / 4.2.0 close-out;
        # payload shapes locked in test_versions_readiness.py)
        assert body["capabilities"]["4"]["globalSessions"] is True
        assert body["capabilities"]["4"]["auxiliaryFilters"] is True
        assert body["capabilities"]["4"]["sseReplay"] is True
        assert body["capabilities"]["4"]["qpImmediateFull"] is True
        assert set(body["capabilities"]["4"]) == {
            "globalSessions", "auxiliaryFilters",
            "sseReplay", "qpImmediateFull", "readiness", "expand",
        }


async def test_health_single_v3_view():
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/health",
                                params={"v": "3"}, headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        # v3 view: view values 3 byte-identical to the 3.x terminal shape;
        # accepted range is config-driven (dual window: [3, 4]).
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert body["server"]["accepted_client_versions"] == [3, 4]
        assert body["schema"]["clientMin"] == 3
        assert body["schema"]["clientMax"] == 4
        assert "auxiliary" not in body
        # no v → 400 (health is on the /slimapi surface)
        resp = await client.get("/slimapi/health", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json()["code"] == "unsupported_version"


# ---------------------------------------------------------------------------
# §4 envelope always + §1 X-Next-Cursor/X-Complete never produced
# ---------------------------------------------------------------------------

async def test_messages_envelope_always_no_cursor_header():
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=_message_payload(),
        headers={"Content-Type": "application/json",
                 "Link": '</session/s1/message?before=m1>; rel="next"'}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/messages/s1",
                                params={"v": "3"}, headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"items", "nextCursor"}
        assert body["nextCursor"] == "m1"  # cursor semantics live in envelope
        assert "x-next-cursor" not in resp.headers


async def test_sessions_envelope_always_no_complete_header():
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3"}, headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"items", "complete"}
        assert body["complete"] is True
        assert "x-complete" not in resp.headers


# ---------------------------------------------------------------------------
# §6.2 Vary shrink — Accept-Encoding only everywhere
# ---------------------------------------------------------------------------

async def test_vary_single_value_on_directory_consuming_routes():
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        for path in ("/slimapi/sessions", "/slimapi/messages/s1",
                     "/slimapi/agent", "/slimapi/vcs"):
            resp = await client.get(path, params={"v": "3"},
                                    headers=IDENTITY)
            assert resp.status_code == 200, path
            vary = resp.headers.get("vary", "")
            parts = {p.strip() for p in vary.split(",")}
            assert parts == {"Accept-Encoding"}, (path, vary)


# ---------------------------------------------------------------------------
# §7 SSE terminal: meta always, subscriber header never, no-v rejected
# ---------------------------------------------------------------------------

async def test_sse_no_v_rejected_before_stream():
    app, seen = _build_app(lambda r: httpx.Response(200, content=b"[]"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/events", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json()["code"] == "unsupported_version"
        assert "text/event-stream" not in resp.headers.get(
            "content-type", "")
    assert seen == []
