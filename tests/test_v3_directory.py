"""v3-contract §5 directory-query tests (Batch B, TDD).

Covers:

* Consuming set × full matrix — none / query-only / header-only /
  dual-present normalized-same / dual-present different → 400
  ``directory_conflict`` (frozen ``queryDirectory``/``headerDirectory``
  field names) / multi-value same folds / multi-value different → 400
  ``invalid_directory_selector``.
* v3 forwarding — the consumed directory (query OR compatible header)
  reaches upstream as ``X-Opencode-Directory``; the ``directory`` pairs
  are stripped from the downstream query (other params intact).
* v2 regression — directory query keeps its v2 semantics (no v3-style
  header-only forwarding for consuming routes' query paths; sessions v2
  still re-adds ``?directory=`` upstream).
* §5.6 stream exception — multi-value different → 400
  ``invalid_directory_selector`` (selector pre-check + route guard);
  query-only accepted (no-op); query+header different → 400
  ``directory_not_allowed`` (inherited v2 guard).
* §5.5 tolerant-ignore set — any directory form on non-consuming routes
  is ignored under v3 (no 400, no consumption).
* Invalid directory value under v3 → 400 ``invalid_directory``.
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from oc_slimapi.config import Settings
from oc_slimapi.errors import CodedHTTPException, register_error_handlers
from oc_slimapi.routes import (
    agent,
    children,
    command,
    diff,
    health,
    messages,
    sessions,
    todo,
    token_stream,
    versions,
)
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
V2_HEADERS = {"X-Slimapi-Version": "2", **IDENTITY}
DIRECTORY_HEADER = "X-Opencode-Directory"


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


def _build_app(handler, *, settings: Settings | None = None):
    """App with every consuming + tolerant route and a recording upstream."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/message"):
            return httpx.Response(
                200, content=_message_payload(),
                headers={"Content-Type": "application/json"})
        if path == "/session/status":
            return httpx.Response(
                200, content=b'{"s1": {"type": "idle"}}',
                headers={"Content-Type": "application/json"})
        if path == "/session":
            return httpx.Response(
                200, content=b'[{"id": "s1", "title": "one"}]',
                headers={"Content-Type": "application/json"})
        # agent / command / todo / children / diff / action — array bodies.
        return httpx.Response(
            200, content=b"[]",
            headers={"Content-Type": "application/json"})

    app = FastAPI(title="oc-slimapi-v3-directory-test")
    app.state.config = settings if settings is not None else _settings()
    app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler or recording),
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=app.state.config.max_transforms,
        transform_wait_seconds=app.state.config.transform_wait_seconds,
        max_response_bytes=app.state.config.max_response_bytes,
    ))
    for router in (
        health.router, versions.router, agent.router,
        command.router, sessions.router, todo.router, children.router,
        diff.router, messages.router,
    ):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


@pytest.fixture
async def stack():
    """Default recording app; the probe list is shared per test."""
    made: list[tuple[FastAPI, list]] = []

    async def make(handler=None, *, settings: Settings | None = None):
        app, seen = _build_app(handler, settings=settings)
        made.append((app, seen))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        return client, seen

    yield make
    for app, _ in made:
        app.state.transforms.shutdown()
        await app.state.upstream.aclose()


def _last_upstream(seen: list[httpx.Request]) -> httpx.Request:
    assert seen, "no upstream request was made"
    return seen[-1]


# ---------------------------------------------------------------------------
# §5.4 / §5.6 consuming-set full matrix (agent as the canonical route)
# ---------------------------------------------------------------------------

async def test_agent_matrix_none(stack):
    client, seen = await stack()
    try:
        response = await client.get("/slimapi/agent?v=3", headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) is None
    finally:
        await client.aclose()


async def test_agent_matrix_query_only_consumed_and_forwarded(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=/w", headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
        assert "directory" not in upstream.url.params
    finally:
        await client.aclose()


async def test_agent_matrix_header_only_retired(stack):
    """Terminal §5.7: the header-only channel is retired — ``?directory=``
    is the sole canonical form on consuming routes."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "directory_header_retired"
        assert not seen
    finally:
        await client.aclose()


async def test_agent_matrix_dual_same_normalized_retired(stack):
    """Terminal §5.7: a normalized-equal dual presence is still rejected —
    the conflict check (§5.4) runs first, then the retired-header rule."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=/w",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w/"})
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "directory_header_retired"
        assert not seen
    finally:
        await client.aclose()


async def test_agent_matrix_dual_different_conflict_fields_frozen(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=/w",
            headers={**IDENTITY, DIRECTORY_HEADER: "/p"})
        assert response.status_code == 400
        assert orjson.loads(response.content) == {
            "code": "directory_conflict",
            "queryDirectory": "/w",
            "headerDirectory": "/p",
        }
        assert not seen  # rejected before any upstream call
    finally:
        await client.aclose()


async def test_agent_matrix_multi_same_folds(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=/w&directory=/w&marker=1",
            headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
        # every directory pair stripped (agent forwards no other query —
        # param-preservation is asserted on the diff/messages routes).
        assert "directory" not in upstream.url.params
    finally:
        await client.aclose()


async def test_agent_matrix_multi_different_rejected(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=/w&directory=/p", headers=IDENTITY)
        assert response.status_code == 400
        assert orjson.loads(response.content) == {
            "code": "invalid_directory_selector"}
        assert not seen
    finally:
        await client.aclose()


async def test_agent_matrix_invalid_value_rejected(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=3&directory=../etc", headers=IDENTITY)
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "invalid_directory"
        assert not seen
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Consuming set spot-checks (query-only consumption + strip + forward)
# ---------------------------------------------------------------------------

_CONSUMING_CASES = [
    ("/slimapi/messages/s1", "messages"),
    ("/slimapi/sessions", "sessions"),
    ("/slimapi/sessions/status", "status"),
    ("/slimapi/sessions/s1/todo", "todo"),
    ("/slimapi/sessions/s1/children", "children"),
    ("/slimapi/sessions/s1/diff", "diff"),
    ("/slimapi/command", "command"),
]


@pytest.mark.parametrize("path,label", _CONSUMING_CASES)
async def test_consuming_routes_v3_consume_strip_forward(stack, path, label):
    client, seen = await stack()
    try:
        response = await client.get(
            f"{path}?v=3&directory=/w", headers=IDENTITY)
        assert response.status_code == 200, label
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w", label
        assert "directory" not in upstream.url.params, label
    finally:
        await client.aclose()


async def test_messages_keeps_other_params_after_strip(stack):
    """messages forwards limit/before upstream — `directory` disappears
    while the sibling routing params survive."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/messages/s1?v=3&directory=/w&limit=7&before=abc",
            headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
        assert upstream.url.params.get("limit") == "7"
        assert upstream.url.params.get("before") == "abc"
        assert "directory" not in upstream.url.params
    finally:
        await client.aclose()


async def test_diff_forwards_messageid_without_directory(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/sessions/s1/diff?v=3&directory=/w&messageID=m1",
            headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
        assert upstream.url.params.get("messageID") == "m1"
        assert "directory" not in upstream.url.params
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# v2 regression — directory semantics unchanged
# ---------------------------------------------------------------------------

async def test_v2_agent_directory_query_form_unsupported(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?directory=/w", headers=V2_HEADERS)
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "unsupported_version"
        assert not seen
    finally:
        await client.aclose()


async def test_v2_agent_header_only_unsupported(stack):
    """Terminal: the selector error (②) outranks the retired-header rule
    (③) — the v2 form is rejected before the header is even examined."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent",
            headers={**V2_HEADERS, DIRECTORY_HEADER: "/w"})
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "unsupported_version"
        assert not seen
    finally:
        await client.aclose()


async def test_v2_sessions_form_unsupported(stack):
    """Terminal: the frozen v2 sessions re-add behavior is gone with the
    v2 pipeline itself — the form is rejected at the selector."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/sessions?directory=/w", headers=V2_HEADERS)
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "unsupported_version"
        assert not seen
    finally:
        await client.aclose()


async def test_v3_sessions_header_only_upstream_query_clean(stack):
    """v3 sessions canonical form: directory rides the header only."""
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/sessions?v=3&directory=/w", headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.url.params.get("directory") is None
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# §5.6 stream exception
# ---------------------------------------------------------------------------

def _stream_request(query: str, *, v3: bool, header: str | None = None):
    headers: list[tuple[bytes, bytes]] = []
    if header is not None:
        headers.append((b"x-opencode-directory", header.encode()))
    state: dict = {}
    if v3:
        from oc_slimapi.selector import SELECTOR_STATE_KEY
        state[SELECTOR_STATE_KEY] = {"result": "v3", "wire": "3"}
    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/slimapi/sessions/s1/stream",
        "raw_path": b"/slimapi/sessions/s1/stream",
        "query_string": query.encode(),
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("t", 80),
        "state": state,
    }
    return Request(scope)


def test_stream_v3_multi_different_invalid_directory_selector():
    from oc_slimapi.routes.token_stream import _resolve_directory_conflict
    request = _stream_request("directory=/w&directory=/p", v3=True)
    with pytest.raises(CodedHTTPException) as excinfo:
        _resolve_directory_conflict(request, "/p")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "invalid_directory_selector"


def test_stream_v3_multi_same_folds_to_single_value():
    from oc_slimapi.routes.token_stream import _resolve_directory_conflict
    request = _stream_request("directory=/w&directory=/w", v3=True)
    # no raise — folded, then the single value flows through the v2 guard
    _resolve_directory_conflict(request, "/w")


def test_stream_v3_query_only_accepted_noop():
    from oc_slimapi.routes.token_stream import _resolve_directory_conflict
    request = _stream_request("directory=/w", v3=True)
    _resolve_directory_conflict(request, "/w")


def test_stream_v3_dual_different_directory_not_allowed():
    """§5.6: after single-valuing, the inherited v2 guard owns the
    both-present-different rejection (existing code ``directory_not_allowed``)."""
    from oc_slimapi.routes.token_stream import _resolve_directory_conflict
    request = _stream_request("directory=/w", v3=True, header="/p")
    with pytest.raises(CodedHTTPException) as excinfo:
        _resolve_directory_conflict(request, "/w")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "directory_not_allowed"


def test_stream_v3_header_only_noop():
    from oc_slimapi.routes.token_stream import _resolve_directory_conflict
    request = _stream_request("", v3=True, header="/w")
    _resolve_directory_conflict(request, None)


async def test_stream_selector_precheck_rejects_multi_different():
    """The selector-level §5.6 pre-check (before the route guard) rejects
    multi-value-different on the stream path with the same code."""
    from oc_slimapi.selector import SlimapiSelectorMiddleware

    class _Capture:
        def __init__(self) -> None:
            self.scope = None

        async def __call__(self, scope, receive, send) -> None:
            self.scope = scope
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b"[]"})

    capture = _Capture()
    middleware = SlimapiSelectorMiddleware(
        capture)
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "path": "/slimapi/sessions/s1/stream",
        "raw_path": b"/slimapi/sessions/s1/stream",
        "query_string": b"v=3&directory=/w&directory=/p",
        "headers": [], "client": ("127.0.0.1", 1), "server": ("t", 80),
        "state": {},
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
    import json as _json
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert orjson.loads(body) == {"code": "invalid_directory_selector"}


async def test_stream_selector_single_value_not_consumed():
    """§5.6: a single-valued directory on stream is NOT stash-consumed and
    NOT stripped by the selector — the route guard judges it."""
    from oc_slimapi.selector import (
        SELECTOR_STATE_KEY,
        SlimapiSelectorMiddleware,
        V3_DIRECTORY_STATE_KEY,
    )

    class _Capture:
        def __init__(self) -> None:
            self.scope = None

        async def __call__(self, scope, receive, send) -> None:
            self.scope = scope
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b"[]"})

    capture = _Capture()
    middleware = SlimapiSelectorMiddleware(
        capture)
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "path": "/slimapi/sessions/s1/stream",
        "raw_path": b"/slimapi/sessions/s1/stream",
        "query_string": b"v=3&directory=/w",
        "headers": [], "client": ("127.0.0.1", 1), "server": ("t", 80),
        "state": {},
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        pass

    await middleware(scope, receive, send)
    assert capture.scope is not None
    assert capture.scope["state"][SELECTOR_STATE_KEY]["result"] == "v3"
    assert V3_DIRECTORY_STATE_KEY not in capture.scope["state"]
    # `v` stripped (sidecar-reserved), `directory` preserved verbatim for
    # the route guard.
    assert capture.scope["query_string"] == b"directory=/w"


# ---------------------------------------------------------------------------
# §5.5 tolerant-ignore set
# ---------------------------------------------------------------------------

async def test_tolerant_routes_ignore_any_directory_form(stack):
    client, seen = await stack()
    try:
        cases = [
            "/slimapi/health?v=3&directory=../etc",
            "/slimapi/health?v=3&directory=/w&directory=/p",
            "/slimapi/versions?v=3&directory=x&directory=y",
            "/slimapi/versions?v=3&directory=/w",
        ]
        for url in cases:
            response = await client.get(url, headers=IDENTITY)
            assert response.status_code == 200, url
            body = orjson.loads(response.content)
            if "/health" in url:
                assert body["slimapi_contract"] == 3  # v3 view, no 400
            else:
                assert body["current"] == 3
        # local tolerant routes never touched the upstream probe
        assert not seen
    finally:
        await client.aclose()


async def test_selector_never_consumes_tolerant_paths():
    """Unit: the consuming-set table excludes the tolerant routes —
    questions / permissions / events included."""
    from oc_slimapi.selector import SlimapiSelectorMiddleware

    class _Sink:
        def __init__(self) -> None:
            self.scope = None

        async def __call__(self, scope, receive, send) -> None:
            self.scope = scope
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b"{}"})

    for path in (
        "/slimapi/questions/s1",
        "/slimapi/permissions/s1",
        "/slimapi/events",
        "/slimapi/health",
        "/slimapi/versions",
        "/slimapi/directories",
    ):
        sink = _Sink()
        middleware = SlimapiSelectorMiddleware(
            sink)
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": path, "raw_path": path.encode(),
            "query_string": b"v=3&directory=../etc&directory=/other",
            "headers": [], "client": ("127.0.0.1", 1), "server": ("t", 80),
            "state": {},
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            pass

        await middleware(scope, receive, send)
        assert sink.scope is not None, path
        # No 400, no directory strip — only the reserved `v` disappears.
        assert sink.scope["query_string"] == b"directory=../etc&directory=/other", path


# ---------------------------------------------------------------------------
# directoryForm observability (§9.1) — dynamic judgment under v3
# ---------------------------------------------------------------------------

async def test_directory_form_observable_values_v3(stack):
    """access-log ``directoryForm`` reflects the actual client form on
    consuming routes (query/header/both/absent) — asserted through the
    selector's stashed form (the traffic layer reads exactly this)."""
    from oc_slimapi.selector import DIRECTORY_FORM_STATE_KEY

    client, seen = await stack()
    try:
        forms: dict[str, str | None] = {}

        class _Probe:
            def __init__(self) -> None:
                self.scope = None

            async def __call__(self, scope, receive, send) -> None:
                self.scope = scope
                await send({
                    "type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b"[]"})

        # Drive the selector directly for each form and read the stash.
        # Terminal §5.7: the header channel is retired — the header/both
        # forms are rejected (400) but their directoryForm is still stashed
        # (the form is computed before the rejection); query/absent proceed.
        cases = [
            ("query", 200, b"v=3&directory=/w", None),
            ("header", 400, b"v=3", "/w"),
            ("both", 400, b"v=3&directory=/w", "/w"),
            ("absent", 200, b"v=3", None),
        ]
        for expected, expected_status, query, header in cases:
            probe = _Probe()
            middleware = SlimapiSelectorMiddleware(
                probe)
            headers: list[tuple[bytes, bytes]] = []
            if header is not None:
                headers.append((b"x-opencode-directory", header.encode()))
            status_holder: list[int] = []
            scope = {
                "type": "http", "http_version": "1.1", "method": "GET",
                "path": "/slimapi/agent", "raw_path": b"/slimapi/agent",
                "query_string": query, "headers": headers,
                "client": ("127.0.0.1", 1), "server": ("t", 80),
                "state": {},
            }

            async def receive() -> dict:
                return {"type": "http.request", "body": b"",
                        "more_body": False}

            async def send(message: dict) -> None:
                if message["type"] == "http.response.start":
                    status_holder.append(message["status"])

            await middleware(scope, receive, send)
            assert status_holder == [expected_status]
            forms[expected] = scope["state"].get(DIRECTORY_FORM_STATE_KEY)

        assert forms["query"] == "query"
        assert forms["header"] == "header"
        assert forms["both"] == "both"
        assert forms["absent"] == "absent"
    finally:
        await client.aclose()


async def test_directory_form_null_on_tolerant_route(stack):
    from oc_slimapi.selector import DIRECTORY_FORM_STATE_KEY

    client, seen = await stack()
    try:
        probe_scope: dict = {}

        class _Probe:
            async def __call__(self, scope, receive, send) -> None:
                probe_scope.update(scope)
                await send({
                    "type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b"{}"})

        middleware = SlimapiSelectorMiddleware(
            _Probe())
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/slimapi/health", "raw_path": b"/slimapi/health",
            "query_string": b"v=3&directory=/w",
            "headers": [(b"x-opencode-directory", b"/w")],
            "client": ("127.0.0.1", 1), "server": ("t", 80), "state": {},
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            pass

        await middleware(scope, receive, send)
        assert probe_scope["state"].get(DIRECTORY_FORM_STATE_KEY) is None
    finally:
        await client.aclose()
