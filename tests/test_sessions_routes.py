from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import sessions
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    upstream: httpx.AsyncClient,
    *,
    hubs: object | None = None,
    turn_registry: object | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-sessions-test")
    app.state.config = settings or _settings()
    app.state.upstream = upstream
    app.state.schema_degraded = False
    # Transform pool (mirrors the real app's setup; required for offload).
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=app.state.config.max_transforms,
        transform_wait_seconds=app.state.config.transform_wait_seconds,
        max_response_bytes=app.state.config.max_response_bytes,
    ))
    # Optional HubRegistry: when supplied, ``load_products`` notification
    # hits the spy / real hub instead of being a silent ``getattr(...,None)``.
    if hubs is not None:
        app.state.hubs = hubs
    # Optional TurnRegistry for /slimapi/sessions/status turn merge tests.
    # When omitted, getattr(request.app.state, "turn_registry", None) → None
    # (the degrade path: turn fields omitted, contract §3.y.1 paired missing).
    if turn_registry is not None:
        app.state.turn_registry = turn_registry
    app.include_router(sessions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _upstream(handler):
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )








async def test_sessions_list_upstream_4xx_returns_502(upstream_factory):
    """GET /slimapi/sessions upstream 4xx → 502 upstream_http_N (§7, sibling status pattern)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad request"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_http_400"


async def test_sessions_list_upstream_404_returns_502_upstream_http_404(upstream_factory):
    """rev-glm/rev-grok 🟡 consensus gap: GET /slimapi/sessions (list, no sid)
    with upstream /session returning 404 → HTTP 502 with code
    `upstream_http_404`, NOT `session_not_found`.

    The session_not_found mapping in _raise_upstream_status (sessions.py:157-158)
    only fires when sid is provided (single-session discover paths). The list
    handler calls _raise_upstream_status(exc) WITHOUT sid, so 404 falls through
    to the generic `status < 500` branch → 502 upstream_http_404. This test
    locks that distinction so a future refactor cannot accidentally start
    routing list-level 404s through session_not_found."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"not found"}')

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "upstream_http_404"
    # Key negative assertion: list-level 404 is NOT session_not_found.
    assert body["code"] != "session_not_found"
    assert "sessionID" not in body


async def test_sessions_list_upstream_5xx_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 5xx → 503 upstream_unavailable (§7)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_network_error_returns_503(upstream_factory):
    """GET /slimapi/sessions httpx.RequestError → 503 upstream_unavailable (§7)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_mid_stream_read_error_returns_503(upstream_factory):
    """sessions list: upstream returns 200 then disconnects mid-body → 503
    upstream_unavailable.

    Regression: streaming read_with_cap() must map httpx.ReadError to
    structured 503, not bare 500.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        async def failing_body():
            yield b'{"info":'
            raise httpx.ReadError("simulated mid-stream disconnect", request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=failing_body(),
        )

    upstream = upstream_factory(handler)
    app = _build_app(upstream, settings=_settings(max_response_bytes=64 * 1024))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_upstream_200_bad_json_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 200 but body not JSON → 503 upstream_unavailable.

    Regression: previously response.json() raised JSONDecodeError → escaped as
    unstructured FastAPI 500 lacking {code:...} (rev-13 must-fix B)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_list_upstream_200_non_array_json_returns_503(upstream_factory):
    """GET /slimapi/sessions upstream 200 but JSON is non-array (dict) → 503
    upstream_unavailable.

    Regression: previously `for item in payload` iterated dict keys (str),
    skeleton_session received a str → raised → unstructured 500 (rev-13 must-fix B)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"unexpected":"shape"}',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"




async def test_sessions_list_oversize_body_returns_413(upstream_factory):
    """sessions list upstream body > max_response_bytes → 413 response_too_large.
    Aligns sessions list with messages/agent/command cap behaviour (closes
    the known limitation noted at sessions.py:42-44)."""
    cap = 4 * 1024
    oversized = b"x" * (cap * 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, settings=_settings(max_response_bytes=cap))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "response_too_large"
    assert body["limit"] == cap









# ---------------------------------------------------------------------------
# slimapi no longer gates directories (regression coverage)
#
# (The two sessions-LIST directory locks — unknown ``?directory=/nope``
# passthrough and trailing-slash normalisation before forward — were
# removed with the V2b src teardown: the v4 facade ignores the directory
# axis entirely on the global list (the selector retires the channel
# pre-route in production; §5.2). Directory forwarding stays locked for
# the directory-consuming routes in test_selector.py / test_directory.py.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v6 §1.1 (v3 terminal): GET /slimapi/sessions envelope
#   * complete: true iff len(sessions) < limit (200 only; body field)
# Error responses (502 / 503) are NOT enveloped.
# ---------------------------------------------------------------------------

async def test_sessions_completeness_headers_absent_on_5xx(upstream_factory):
    """503 / 502 responses do NOT carry the completeness trio — the contract
    is "200 only". Body is still the coded error envelope."""
    for status in (500, 502, 503):
        def handler(request: httpx.Request, _s=status) -> httpx.Response:
            return httpx.Response(_s, content=b"boom")

        upstream = upstream_factory(handler)
        app = _build_app(upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code in (502, 503)
        # 503 path: "upstream_unavailable" — error bodies are not enveloped.
        assert "items" not in response.json()


async def test_sessions_x_complete_true_when_below_limit(upstream_factory):
    """len < limit → complete: true in the envelope (V2b: the v4 facade
    computes the same best-effort flag on the Class A HTTP fallback)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=orjson.dumps([
            {"id": "s1", "title": "t", "directory": "/a",
             "time": {"created": 1, "updated": 1}},
            {"id": "s2", "title": "t", "directory": "/a",
             "time": {"created": 2, "updated": 2}},
        ]), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?limit=10", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["complete"] is True


async def test_sessions_x_complete_false_at_limit(upstream_factory):
    """len == limit → complete: false (page is full; raise limit to recheck)."""
    def handler(request: httpx.Request) -> httpx.Response:
        # 5 items, limit=5 → full.
        return httpx.Response(200, content=orjson.dumps([
            {"id": f"s{i}", "title": "t", "directory": "/a",
             "time": {"created": i, "updated": i}}
            for i in range(5)
        ]), headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions?limit=5", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["complete"] is False


# (test_sessions_roots_default_unchanged_false was removed with the V2b src
# teardown: it locked the v3 leg's ``roots=false`` default forwarding (and
# the explicit ``roots=true`` passthrough). Under the v4-only facade
# ``roots`` is a v3-only parameter — present → 422 param_version_mismatch
# (locked in tests/test_sessions_v4_matrix.py), absent → not forwarded.)


async def test_sessions_list_scalar_element_list_returns_503(upstream_factory):
    """sessions list: upstream returns [1, null] (list of non-dict) → 503
    upstream_unavailable.

    Regression: skeleton_session would call .get() on non-dict → bare 500.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[1, null]',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_non_list_payload_returns_503_no_completeness_headers(upstream_factory):
    """v6 §1.1 isinstance guard: dict / string / null bodies (200) → 503
    upstream_unavailable, with NO completeness trio (200-only contract).
    Regression for the pre-v6 silent ``for item in payload`` that produced
    an empty skeleton list + X-Complete: true."""
    cases: list[bytes] = [
        b'{"unexpected":"shape"}',  # dict
        b'"a string"',              # str
        b"null",                    # None
        b"42",                      # number
    ]
    for bad_body in cases:
        def handler(request: httpx.Request, body: bytes = bad_body) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
        assert response.status_code == 503, f"body={bad_body!r}"
        assert response.json()["code"] == "upstream_unavailable"
        assert "X-Complete" not in response.headers


# ---------------------------------------------------------------------------
# GET /slimapi/sessions/status (additive re-add)
#
# Passthrough of upstream GET /session/status (Record<SessionID,{type}>)
# + sidecar merge of TurnRegistry (turnIncarnation/turn) per sid.
# Read-only, no caching; same in-memory turn source as digest SSE (§3.y).
# directory is OPTIONAL (additive; upstream ignores it — returns the global
# map — so callers may omit it). turn_registry is lifespan-wired in
# production; when absent both fields are omitted.
# ---------------------------------------------------------------------------


def _status_handler_factory(captured: dict | None = None, body: bytes = b"{}"):
    """Build an upstream mock handler returning ``body`` for /session/status.

    Captures the directory query + header when a ``captured`` dict is given
    so forwarding tests can assert both legs. Falls through to an empty 200
    list for /session (so smoke/session-list calls don't noise the handler).
    """
    import orjson as _orjson

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            if captured is not None:
                captured["query"] = request.url.params.get("directory")
                captured["header"] = request.headers.get("x-opencode-directory")
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=_orjson.dumps([]),
                              headers={"Content-Type": "application/json"})
    return handler


async def test_sessions_status_registered_returns_200_not_thin_route_not_found(upstream_factory):
    """Regression: the endpoint EXISTS (200), not 404 ``thin_route_not_found``.

    lite-v2 originally deleted /slimapi/sessions/status; this locks the
    additive re-add so a future regression (route dropped or shadowed by
    catch-all) surfaces immediately."""
    upstream = upstream_factory(_status_handler_factory(body=b"{}"))
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    assert response.json() == {}


async def test_sessions_status_directory_optional_omitted_returns_200(upstream_factory):
    """``directory`` is OPTIONAL (additive). Omitting it → 200 with the
    global status map (upstream ignores directory anyway); the sidecar
    forwards NEITHER ``?directory=`` query NOR ``X-Opencode-Directory``
    header when it isn't supplied. See s4-batch-status-research.md."""
    captured: dict[str, str | None] = {}
    upstream = upstream_factory(_status_handler_factory(captured, b'{"s1":{"type":"busy"}}'))
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/status", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"s1": {"type": "busy"}}
    # No directory supplied → neither leg forwarded to upstream.
    assert captured["query"] is None
    assert captured["header"] is None


async def test_sessions_status_directory_validated_and_forwarded(upstream_factory):
    """Directory is normalized + validated before forwarding: both
    ``?directory=`` query and ``X-Opencode-Directory`` header reach upstream
    (mirrors /slimapi/sessions). Trailing slash is stripped."""
    from oc_slimapi.turn_registry import TurnRegistry
    captured: dict[str, str | None] = {}
    upstream = upstream_factory(_status_handler_factory(captured, b"{}"))
    app = _build_app(upstream, turn_registry=TurnRegistry(incarnation=7))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app/", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    # v3 terminal: ?directory is consumed by the sidecar and forwarded as
    # the X-Opencode-Directory header only (no upstream query re-add).
    assert captured["query"] is None
    assert captured["header"] == "/app"


async def test_sessions_status_invalid_directory_returns_400(upstream_factory):
    """Directory containing ``..`` segment → 400 ``invalid_directory``
    (validate_directory security guard, never forwarded)."""
    upstream = upstream_factory(_status_handler_factory())
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/../etc", headers=VERSION_HEADERS,
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_directory"


async def test_sessions_status_merges_idle_busy_retry_turn_fields(upstream_factory):
    """Sparse upstream status map (idle / busy / retry shapes) + sidecar
    TurnRegistry → each entry gains paired ``turnIncarnation``/``turn`` at
    the flat top level; the retry entry's extra fields are preserved; an
    unobserved sid yields turn=0 (snapshot contract §3.y.1)."""
    from oc_slimapi.turn_registry import TurnRegistry
    body = orjson.dumps({
        "s_idle": {"type": "idle"},
        "s_busy": {"type": "busy"},
        "s_retry": {
            "type": "retry", "attempt": 2, "message": "rate limited",
            "next": 3,
        },
    })
    reg = TurnRegistry(incarnation=5)
    reg.bump_turn("s_busy")  # one prompt forwarded through sidecar for s_busy
    reg.bump_turn("s_busy")
    upstream = upstream_factory(_status_handler_factory(body=body))
    app = _build_app(upstream, turn_registry=reg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    # idle: unobserved → (inc, 0)
    assert data["s_idle"] == {"type": "idle", "turnIncarnation": 5, "turn": 0}
    # busy: bumped twice → turn=2
    assert data["s_busy"] == {"type": "busy", "turnIncarnation": 5, "turn": 2}
    # retry: extra fields preserved + turn merge (unobserved → turn=0)
    assert data["s_retry"]["type"] == "retry"
    assert data["s_retry"]["attempt"] == 2
    assert data["s_retry"]["message"] == "rate limited"
    assert data["s_retry"]["next"] == 3
    assert data["s_retry"]["turnIncarnation"] == 5
    assert data["s_retry"]["turn"] == 0


async def test_sessions_status_turn_reflects_concurrent_bump(upstream_factory):
    """Live read: two status calls around a ``bump_turn`` observe turn 0 then
    turn 1 — the merge reads the registry at call time (no caching). This is
    the 'concurrent bump' / live-projection guarantee."""
    from oc_slimapi.turn_registry import TurnRegistry
    body = orjson.dumps({"s1": {"type": "busy"}})
    reg = TurnRegistry(incarnation=3)
    upstream = upstream_factory(_status_handler_factory(body=body))
    app = _build_app(upstream, turn_registry=reg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
        assert r1.json()["s1"]["turn"] == 0
        # A prompt forward bumps the turn (simulating catch-all commit point).
        reg.bump_turn("s1")
        r2 = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
        assert r2.json()["s1"]["turn"] == 1
        assert r2.json()["s1"]["turnIncarnation"] == 3


async def test_sessions_status_no_registry_omits_turn_fields(upstream_factory):
    """When ``turn_registry`` is not wired on app.state, turn fields are
    omitted (paired missing → ocdroid Tier-2 degrade, contract §3.y.1).
    The upstream status shape is otherwise untouched."""
    body = orjson.dumps({"s1": {"type": "idle"}, "s2": {"type": "busy"}})
    upstream = upstream_factory(_status_handler_factory(body=body))
    app = _build_app(upstream)  # no turn_registry on state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data == {"s1": {"type": "idle"}, "s2": {"type": "busy"}}
    assert "turnIncarnation" not in data["s1"]
    assert "turn" not in data["s1"]


async def test_sessions_status_bad_shape_non_dict_returns_503(upstream_factory):
    """Upstream 200 but top-level JSON is not a Record (dict) → 503
    upstream_unavailable (mirrors the sessions-list non-array guard)."""
    for bad_body in (
        b'["not", "a", "dict"]',   # list
        b'"a string"',             # str
        b"null",                   # None
        b"42",                     # number
    ):
        def handler(request: httpx.Request, body: bytes = bad_body) -> httpx.Response:
            if request.url.path == "/session/status":
                return httpx.Response(200, content=body,
                                      headers={"Content-Type": "application/json"})
            return httpx.Response(200, content=b"[]",
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
            )
        assert response.status_code == 503, f"body={bad_body!r}"
        assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_status_upstream_4xx_returns_502(upstream_factory):
    """Upstream 4xx on /session/status → 502 upstream_http_N (no sid, so
    no session_not_found mapping — same as the list-level 4xx path)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            return httpx.Response(400, content=b'{"error":"bad"}')
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_http_400"


async def test_sessions_status_upstream_5xx_and_network_error_return_503(upstream_factory):
    """Upstream 5xx and connection-level error both → 503
    upstream_unavailable (§7)."""
    # 5xx
    def handler_5xx(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler_5xx)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"

    # network error
    def handler_net(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler_net)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_status_upstream_200_bad_json_returns_503(upstream_factory):
    """Upstream 200 but body not JSON → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/status":
            return httpx.Response(200, content=b"not json",
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_sessions_status_non_dict_entry_value_passed_through(upstream_factory):
    """Defensive: if upstream returns a Record whose value is not a dict
    (schema violation), the entry is passed through unchanged (no turn
    merge on it) rather than crashing the whole response."""
    from oc_slimapi.turn_registry import TurnRegistry
    body = orjson.dumps({"s_ok": {"type": "idle"}, "s_bad": "busy"})
    reg = TurnRegistry(incarnation=2)
    upstream = upstream_factory(_status_handler_factory(body=body))
    app = _build_app(upstream, turn_registry=reg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/sessions/status?directory=/app", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["s_ok"] == {"type": "idle", "turnIncarnation": 2, "turn": 0}
    # Non-dict value passed through verbatim (no crash, no merge).
    assert data["s_bad"] == "busy"


# ---------------------------------------------------------------------------
# TransformBusy admission saturation → 503 + Retry-After (T4)
# ---------------------------------------------------------------------------

async def test_sessions_transform_busy_returns_retry_after_without_upstream_call(upstream_factory):
    """Pre-acquire the single admission slot (max_transforms=1), then call
    GET /slimapi/sessions — must emit 503 transform_busy with Retry-After
    header and body retry_after field, and must NOT hit upstream (admission
    is acquired BEFORE the GET)."""
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, settings=_settings())
    pool = app.state.transforms
    transport = httpx.ASGITransport(app=app)
    try:
        async with pool:  # saturate the single admission slot
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
            assert response.status_code == 503
            body = response.json()
            assert body["code"] == "transform_busy"
            assert body["retry_after"] == 2
            assert response.headers["Retry-After"] == "2"
        # admission-before-GET → zero upstream calls.
        assert calls["n"] == 0
    finally:
        app.state.transforms.shutdown()


