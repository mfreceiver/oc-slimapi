"""B3a-A dual-version window ((3, 4)) matrix — design-v4-selector §2/§3.

Covers the Phase-A core deltas:

* **A1** version gate: pinned constants (SERVER_API_VERSION=4,
  ACCEPTED_CLIENT_VERSIONS=(3, 4)) + the S-B04 config migration — the
  ``OC_SLIMAPI_SERVER_API_VERSION`` env no longer influences
  ``Settings.server_api_version`` (warning + ignore, startup intact) while
  the fail-closed pin on ``OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS`` is kept.
* **A2** selector dual-version: ``?v=4`` admitted + stashed
  (``selectorResult=v4`` / ``wireVersion="4"``), request-scope
  ``wire_view_from_scope`` (stash-read, default 3), the §5.2
  consuming-set fork (v4 = v3 − {global sessions list}), the uniform
  ``directory_retired_in_v4`` 400 over ALL four input forms (priority over
  the whole v3 validation ladder), and v3-unchanged behaviour on every
  non-retired route.
* **A3** versions/health dual view: discovery payload
  (current=4/available=[3, 4]/capabilities{"3","4"} with the four STATIC
  v4 keys — sseReplay/qpImmediateFull advertised same-batch by B3b-5,
  superseding B3a's interim absence face), the health
  v4 view (view triplet =4 + transient
  ``auxiliary={"available": false, "mode": "http"}``), the v3 view
  byte-regression, and the ready endpoint's zero-v4-difference freeze.

Existing-route scaffolding mirrors test_selector.py (minimal stack:
selector + health + versions + local echo routes on consuming paths so the
fork is observable without the full session router).
"""
from __future__ import annotations


import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from oc_slimapi import selector as sel
from oc_slimapi.config import Settings
from oc_slimapi.routes import health as health_routes
from oc_slimapi.routes import versions as versions_routes
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.selector import SlimapiSelectorMiddleware, wire_view_from_scope
from oc_slimapi.versioning import ACCEPTED_CLIENT_VERSIONS, SERVER_API_VERSION

DIRECTORY_HEADER = "X-Opencode-Directory"
IDENTITY = {"Accept-Encoding": "identity"}
RETIREMENT_BODY = {
    "code": "directory_retired_in_v4",
    "hint": (
        "v4 sessions is a global facade; remove the directory parameter "
        "(and the X-Opencode-Directory header). Token/per-session routes "
        "still accept ?directory=."
    ),
}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings | None = None,
               with_selector: bool = True) -> FastAPI:
    """Minimal selector stack + echo routes on the two fork-relevant paths.

    ``/slimapi/sessions`` is the v4-retired route; ``/slimapi/sessions/status``
    represents the consuming set that v4 does NOT retire. Both echo the
    downstream query string, the selector/directory stashes — everything the
    fork needs to prove.
    """
    app = FastAPI(title="v4-dual-window")
    if with_selector:
        app.add_middleware(SlimapiSelectorMiddleware)
    app.state.config = settings or _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health_routes.router)
    app.include_router(versions_routes.router)

    @app.get("/slimapi/sessions")
    async def sessions_echo(request: Request):
        state = dict(request.scope.get("state") or {})
        return {
            "query": request.scope.get("query_string", b"").decode("latin-1"),
            "selector": state.get(sel.SELECTOR_STATE_KEY),
            "directory": state.get(sel.V3_DIRECTORY_STATE_KEY, None),
        }

    @app.get("/slimapi/sessions/status")
    async def status_echo(request: Request):
        state = dict(request.scope.get("state") or {})
        return {
            "query": request.scope.get("query_string", b"").decode("latin-1"),
            "selector": state.get(sel.SELECTOR_STATE_KEY),
            "directory": state.get(sel.V3_DIRECTORY_STATE_KEY, None),
        }

    register_error_handlers(app)
    return app


def _build_ready_app(settings: Settings | None = None) -> FastAPI:
    """Stack with the mocked upstream /global/health for the /ready tests."""
    app = _build_app(settings)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://upstream",
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# A1 — version gate + S-B04 config migration
# ---------------------------------------------------------------------------

def test_pinned_constants_dual_window():
    assert SERVER_API_VERSION == 4
    assert ACCEPTED_CLIENT_VERSIONS == (3, 4)


def test_server_api_version_env_is_ignored_with_warning(monkeypatch, caplog):
    """S-B04: the env knob no longer influences the view — warning + ignore.

    Even a value that WOULD have been in-range (3) is ignored: the field is
    constant-pinned to SERVER_API_VERSION during the dual window, and
    validate() (i.e. startup) must not break — it only warns.
    """
    monkeypatch.setenv("OC_SLIMAPI_SERVER_API_VERSION", "3")
    settings = _settings()
    assert settings.server_api_version == 4
    with caplog.at_level("WARNING"):
        settings.validate()  # must not raise
    assert any(
        "OC_SLIMAPI_SERVER_API_VERSION" in r.message for r in caplog.records
    )


def test_server_api_version_env_out_of_range_still_ignored(monkeypatch):
    """A would-be-invalid env value also only warns — it cannot break boot
    nor move the constant."""
    monkeypatch.setenv("OC_SLIMAPI_SERVER_API_VERSION", "9")
    settings = _settings()
    assert settings.server_api_version == 4
    settings.validate()  # warning only, no raise


def test_server_api_version_constant_without_env(monkeypatch):
    monkeypatch.delenv("OC_SLIMAPI_SERVER_API_VERSION", raising=False)
    settings = _settings()
    assert settings.server_api_version == 4
    settings.validate()


def test_validate_fail_closed_pin_blocks_widening():
    """The accepted-range pin survives the migration untouched: no
    widening of (3, 4) via the accepted-versions knob."""
    with pytest.raises(RuntimeError, match=r"must be \(3, 4\)"):
        _settings(accepted_client_versions=(3, 5)).validate()


def test_validate_fail_closed_pin_blocks_narrowing():
    with pytest.raises(RuntimeError, match=r"must be \(3, 4\)"):
        _settings(accepted_client_versions=(4, 4)).validate()


# ---------------------------------------------------------------------------
# A2 — selector dual-version matrix
# ---------------------------------------------------------------------------

async def test_v4_health_dual_view():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=4", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["slimapi_contract"] == 4
        assert body["server"]["api_version"] == 4
        assert body["schema"]["version"] == 4
        # Stage-A transient placeholder (B1/B5 wire the real dbaux state).
        assert body["auxiliary"] == {"available": False, "mode": "http"}
        # allowlist view field present in the v4 view too.
        assert body["features"]["allowlist"] == {"enabled": False}


async def test_v3_health_view_regression_no_auxiliary():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/health?v=3", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        # v3 view: byte-identical terminal shape — view triplet 3 and NO
        # auxiliary key.
        assert body["slimapi_contract"] == 3
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert "auxiliary" not in body
        assert body["features"]["allowlist"] == {"enabled": False}


async def test_v4_wire_stash():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions?v=4", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["selector"] == {"result": "v4", "wire": "4"}
        assert body["directory"] is None
        # `v` (and nothing else) was stripped downstream.
        assert body["query"] == ""


async def test_v3_wire_stash_unchanged():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions?v=3&x=1", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["selector"] == {"result": "v3", "wire": "3"}
        assert body["query"] == "x=1"


async def test_selector_less_stack_defaults_to_v3_view():
    """Backward-compat ironclad: without the selector (direct route
    invocation), the observed wire view is 3 — v4 capability is only
    reachable through the explicit selector."""
    app = _build_app(with_selector=False)
    async with _client(app) as client:
        # No ?v needed — no selector in the stack; the route runs the
        # default v3 view.
        resp = await client.get("/slimapi/health", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["slimapi_contract"] == 3
        assert body["schema"]["version"] == 3
        assert "auxiliary" not in body


def test_wire_view_from_scope_unit():
    assert wire_view_from_scope({}) == 3
    assert wire_view_from_scope({"state": {}}) == 3
    assert wire_view_from_scope(
        {"state": {sel.SELECTOR_STATE_KEY: {"result": "v3", "wire": "3"}}}
    ) == 3
    assert wire_view_from_scope(
        {"state": {sel.SELECTOR_STATE_KEY: {"result": "v4", "wire": "4"}}}
    ) == 4
    # rejected / exempt / not_applicable carry no wire → default 3.
    assert wire_view_from_scope(
        {"state": {sel.SELECTOR_STATE_KEY: {"result": "rejected", "wire": None}}}
    ) == 3


async def test_v4_sessions_directory_query_single_retired():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4&directory=/w", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == RETIREMENT_BODY


async def test_v4_sessions_directory_query_multi_retired():
    """Retirement outranks the v3 multi-value validation (invalid_directory_
    selector) — uniform single error body."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4&directory=/a&directory=/b", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == RETIREMENT_BODY


async def test_v4_sessions_directory_header_only_retired():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 400
        assert resp.json() == RETIREMENT_BODY


async def test_v4_sessions_directory_query_plus_header_retired():
    """Mixed form — including the same-value variant that v3 would class as
    directory_header_retired — is uniformly retired."""
    app = _build_app()
    async with _client(app) as client:
        for query in ("directory=/w", "directory=/a&directory=/w"):
            resp = await client.get(
                f"/slimapi/sessions?v=4&{query}",
                headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
            assert resp.status_code == 400, query
            assert resp.json() == RETIREMENT_BODY


async def test_v4_sessions_directory_blank_query_value_retired():
    """Key PRESENCE retires — a degenerate ``?directory=`` still exercises
    the retired channel (no existence leak, uniform body)."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4&directory=", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == RETIREMENT_BODY


async def test_v4_sessions_no_directory_forwards_untouched():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4&cursor=abc", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["selector"] == {"result": "v4", "wire": "4"}
        assert body["directory"] is None
        assert body["query"] == "cursor=abc"


async def test_v3_sessions_directory_ladder_unchanged():
    """v3 on the same route: the three frozen error codes + consumption —
    byte-identical to the terminal semantics."""
    app = _build_app()
    async with _client(app) as client:
        # 1. multi-value distinct
        resp = await client.get(
            "/slimapi/sessions?v=3&directory=/a&directory=/b", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_directory_selector"}
        # 2. dual-present different
        resp = await client.get(
            "/slimapi/sessions?v=3&directory=/a",
            headers={**IDENTITY, DIRECTORY_HEADER: "/b"})
        assert resp.status_code == 400
        assert resp.json() == {
            "code": "directory_conflict",
            "queryDirectory": "/a",
            "headerDirectory": "/b",
        }
        # 3. header-only
        resp = await client.get(
            "/slimapi/sessions?v=3",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "directory_header_retired"}
        # 4. query-only single → consumed + stashed + stripped
        resp = await client.get(
            "/slimapi/sessions?v=3&directory=/w&cursor=c", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["directory"] == "/w"
        assert body["query"] == "cursor=c"


async def test_v4_non_retired_consuming_route_keeps_v3_semantics():
    """sessions/status is in the v4 consuming set (NOT retired): a v4
    request consumes ``?directory=`` exactly like v3."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions/status?v=4&directory=/w", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["selector"] == {"result": "v4", "wire": "4"}
        assert body["directory"] == "/w"
        assert body["query"] == ""
        # and the v3 error ladder still applies on it for bad forms:
        resp = await client.get(
            "/slimapi/sessions/status?v=4&directory=/a&directory=/b",
            headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_directory_selector"}
        resp = await client.get(
            "/slimapi/sessions/status?v=4",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "directory_header_retired"}


def test_directory_fork_is_set_difference():
    """Design invariant: the v4 fork is a SET DIFFERENCE over the shared v3
    pattern source — exactly one pattern (the global sessions list) leaves,
    every other consuming route stays consuming for wire 4."""
    retired = [p.pattern for p in sel._DIRECTORY_V4_RETIRED_PATTERNS]
    assert retired == [r"^/slimapi/sessions$"]
    total = len(sel._DIRECTORY_CONSUMING_PATTERNS)
    assert total == 25  # v3 source of truth
    # Kept list below instantiates every remaining pattern family.
    kept = [
        "/slimapi/messages/s1",
        "/slimapi/messages/s1/full/f1",
        "/slimapi/messages/s1/expand/file/f1",
        "/slimapi/messages/s1/expand/file/f1/p1",
        "/slimapi/sessions/status",
        "/slimapi/sessions/s1/todo",
        "/slimapi/sessions/s1/children",
        "/slimapi/sessions/s1/diff",
        "/slimapi/sessions/s1/stream",
        "/slimapi/agent",
        "/slimapi/command",
        "/slimapi/file",
        "/slimapi/file/content",
        "/slimapi/file/status",
        "/slimapi/vcs",
        "/slimapi/vcs/status",
        "/slimapi/vcs/diff",
        "/slimapi/find/file",
        "/slimapi/config/providers",
        "/slimapi/session",
        "/slimapi/session/s1",
        "/slimapi/session/s1/prompt_async",
        "/slimapi/session/s1/abort",
        "/slimapi/session/s1/summarize",
        "/slimapi/session/s1/fork",
        "/slimapi/session/s1/revert",
        "/slimapi/session/s1/command",
        "/slimapi/session/s1/permissions/p1",
        "/slimapi/question/q1/reply",
        "/slimapi/question/q1/reject",
    ]
    for path in kept:
        assert sel._directory_consuming_for(path, 3), path
        assert sel._directory_consuming_for(path, 4), path
    # The retired route: consuming under v3, retired under v4.
    assert sel._directory_consuming_for("/slimapi/sessions", 3)
    assert not sel._directory_consuming_for("/slimapi/sessions", 4)
    # Tolerant routes stay tolerant under both views.
    for path in ("/slimapi/versions", "/slimapi/health", "/slimapi/ready",
                 "/slimapi/metrics.traffic"):
        assert not sel._directory_consuming_for(path, 3), path
        assert not sel._directory_consuming_for(path, 4), path


async def test_v4_repeated_same_value_folds():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=4&v=4&directory=/w", headers=IDENTITY)
        # Folds to v4 — directory still retires (not a version error).
        assert resp.status_code == 400
        assert resp.json() == RETIREMENT_BODY
        resp = await client.get("/slimapi/health?v=4&v=4", headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.json()["schema"]["version"] == 4


async def test_v4_multi_differing_rejected_before_directory():
    """Cross-version conflict (?v=3&v=4) is a version-family 400 — priority
    above the directory family."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions?v=3&v=4&directory=/w", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "invalid_version_selector"}


async def test_unsupported_v5_reports_dual_supported_set():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions?v=5", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json() == {"code": "unsupported_version", "supported": [3, 4]}


async def test_versions_405_outranks_v4_directory_retirement():
    """§8.3 ①: 405 on non-GET versions outranks everything — even the v4
    retirement error."""
    app = _build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/slimapi/versions?v=4&directory=/w", headers=IDENTITY)
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET"


# ---------------------------------------------------------------------------
# A3 — versions / health dual views
# ---------------------------------------------------------------------------

async def test_versions_payload_dual_window():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/versions", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert list(body.keys()) == [
            "current", "available", "capabilities", "sidecarVersion",
        ]
        assert body["current"] == 4
        assert body["available"] == [3, 4]


async def test_versions_v4_capabilities_four_static_keys():
    app = _build_app()
    async with _client(app) as client:
        body = (await client.get("/slimapi/versions", headers=IDENTITY)).json()
        caps = body["capabilities"]
        assert set(caps.keys()) == {"3", "4"}
        # B3b-5: same-batch advertising (n1 frozen timing) — sseReplay and
        # qpImmediateFull landed WITH the B3b implementation; B3a's absence
        # assertions are superseded by the four-key face.
        assert caps["4"] == {
            "globalSessions": True,
            "auxiliaryFilters": True,
            "sseReplay": True,
            "qpImmediateFull": True,
        }


async def test_versions_v3_capabilities_shape_unchanged():
    app = _build_app()
    async with _client(app) as client:
        body = (await client.get("/slimapi/versions", headers=IDENTITY)).json()
        cap3 = body["capabilities"]["3"]
        assert cap3["envelope"] == ["messages", "sessions"]
        assert cap3["directoryQuery"] is True
        assert cap3["versionHeaderOptional"] is True
        assert cap3["writeRoutes"] is True
        assert cap3["readRoutes"] == [
            "file", "vcs", "find", "providers",
            "sessionSingle", "activeSessions", "globalHealth",
        ]
        assert "expand" in cap3


async def test_versions_payload_independent_of_wire_view():
    """Discovery is version-independent: ?v=3, ?v=4 and no v answer the
    exact same payload (the endpoint is selector-exempt)."""
    app = _build_app()
    async with _client(app) as client:
        base = (await client.get("/slimapi/versions", headers=IDENTITY)).json()
        for query in ("?v=3", "?v=4"):
            resp = await client.get(f"/slimapi/versions{query}", headers=IDENTITY)
            assert resp.status_code == 200
            assert resp.json() == base


async def test_health_allowlist_enabled_in_both_views():
    settings = _settings(directory_allowlist=["/w"])
    app = _build_app(settings)
    async with _client(app) as client:
        for v, expected_view in (("3", 3), ("4", 4)):
            resp = await client.get(
                f"/slimapi/health?v={v}", headers=IDENTITY)
            assert resp.status_code == 200
            body = resp.json()
            assert body["schema"]["version"] == expected_view
            assert body["server"]["api_version"] == expected_view
            assert body["slimapi_contract"] == expected_view
            assert body["features"]["allowlist"]["enabled"] is True


async def test_ready_zero_v4_difference():
    """Contract §3.2/§12: ready is frozen — shape AND values stay the v3
    terminal ones even for a v4 wire request."""
    app = _build_ready_app()
    async with _client(app) as client:
        resp = await client.get("/slimapi/ready?v=4", headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert "slimapi_contract" not in body
        assert body["server"]["api_version"] == 3
        assert body["schema"]["version"] == 3
        assert "auxiliary" not in body
        # ...and byte-equal to the v3 request's answer.
        v3 = await client.get("/slimapi/ready?v=3", headers=IDENTITY)
        assert v3.status_code == 200
        v3_body = v3.json()
        v3_body.pop("upstream")
        body.pop("upstream")  # latencyMs may differ between the two pings
        assert body == v3_body
