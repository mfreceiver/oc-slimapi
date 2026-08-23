"""v4 directory-query policy tests.

B12 (2026-08-21) three-way split: the consumer-ladder functions below now
drive the ``?v=4`` face — v4-contract §5.1 inherits the v3 §5 consumption /
tolerance / error semantics verbatim on every non-retired route, so the
ladder assertions are v4-equivalent and were rewritten selector 3→4.
V2b (2026-08-21 Phase-4 guard teardown) deleted the v3-only guardians
(sessions-list consumption face, v2-form rejections). The §9.1
observability dims (§9 frozen schema naming) stay.

Covers:

* Consuming set × full matrix — none / query-only / header-only /
  dual-present normalized-same / dual-present different → 400
  ``directory_conflict`` (frozen ``queryDirectory``/``headerDirectory``
  field names) / multi-value same folds / multi-value different → 400
  ``invalid_directory_selector``.
* v4 forwarding — the consumed query directory
  reaches upstream as ``X-Opencode-Directory``; the ``directory`` pairs
  are stripped from the downstream query (other params intact).
* §5.6 stream exception — production selector precedence: query-only is an
  accepted no-op; header-only and dual-present same are retired; dual-present
  different conflicts; repeated distinct query values are invalid.
* §5.5 tolerant-ignore set — any directory form on non-consuming routes
  is ignored (no 400, no consumption).
* Invalid directory value → 400 ``invalid_directory``.
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import (
    agent,
    children,
    command,
    diff,
    health,
    messages,
    sessions,
    todo,
    versions,
)
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
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
        response = await client.get("/slimapi/agent?v=4", headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) is None
    finally:
        await client.aclose()


async def test_agent_matrix_query_only_consumed_and_forwarded(stack):
    client, seen = await stack()
    try:
        response = await client.get(
            "/slimapi/agent?v=4&directory=/w", headers=IDENTITY)
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
            "/slimapi/agent?v=4",
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
            "/slimapi/agent?v=4&directory=/w",
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
            "/slimapi/agent?v=4&directory=/w",
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
            "/slimapi/agent?v=4&directory=/w&directory=/w&marker=1",
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
            "/slimapi/agent?v=4&directory=/w&directory=/p", headers=IDENTITY)
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
            "/slimapi/agent?v=4&directory=../etc", headers=IDENTITY)
        assert response.status_code == 400
        assert orjson.loads(response.content)["code"] == "invalid_directory"
        assert not seen
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Consuming set spot-checks (query-only consumption + strip + forward)
# ---------------------------------------------------------------------------

# The only admitted v4 face drives these consumption checks. The global
# sessions list intentionally omits directory (locked in test_v4_only_window.py).
_CONSUMING_CASES = [
    ("/slimapi/messages/s1", "messages"),
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
            f"{path}?v=4&directory=/w", headers=IDENTITY)
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
            "/slimapi/messages/s1?v=4&directory=/w&limit=7&before=abc",
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
            "/slimapi/sessions/s1/diff?v=4&directory=/w&messageID=m1",
            headers=IDENTITY)
        assert response.status_code == 200
        upstream = _last_upstream(seen)
        assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
        assert upstream.url.params.get("messageID") == "m1"
        assert "directory" not in upstream.url.params
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# §5.6 stream exception — production selector precedence
# ---------------------------------------------------------------------------

class _StreamCapture:
    def __init__(self) -> None:
        self.scope = None

    async def __call__(self, scope, receive, send) -> None:
        self.scope = scope
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b"[]"})


async def _run_stream_selector(
    query: str, *, header: str | None = None,
) -> tuple[int, dict | list, dict | None]:
    capture = _StreamCapture()
    middleware = SlimapiSelectorMiddleware(capture)
    headers: list[tuple[bytes, bytes]] = []
    if header is not None:
        headers.append((b"x-opencode-directory", header.encode()))
    raw_query = f"v=4&{query}" if query else "v=4"
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "path": "/slimapi/sessions/s1/stream",
        "raw_path": b"/slimapi/sessions/s1/stream",
        "query_string": raw_query.encode(),
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("t", 80),
        "state": {},
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], orjson.loads(body), capture.scope


async def test_stream_query_only_is_allowed_noop():
    status, body, scope = await _run_stream_selector("directory=/w")
    assert status == 200
    assert body == []
    assert scope is not None
    assert scope["state"]["slimapi_selector"]["result"] == "v4"
    assert "slimapi_directory" not in scope["state"]
    assert scope["query_string"] == b"directory=/w"


async def test_stream_header_only_is_retired():
    status, body, scope = await _run_stream_selector("", header="/w")
    assert status == 400
    assert body == {"code": "directory_header_retired"}
    assert scope is None


async def test_stream_query_plus_same_header_is_retired():
    status, body, scope = await _run_stream_selector(
        "directory=/w", header="/w/",
    )
    assert status == 400
    assert body == {"code": "directory_header_retired"}
    assert scope is None


async def test_stream_query_plus_different_header_conflicts():
    status, body, scope = await _run_stream_selector(
        "directory=/w", header="/p",
    )
    assert status == 400
    assert body == {
        "code": "directory_conflict",
        "queryDirectory": "/w",
        "headerDirectory": "/p",
    }
    assert scope is None


async def test_stream_repeated_distinct_query_is_invalid():
    status, body, scope = await _run_stream_selector(
        "directory=/w&directory=/p",
    )
    assert status == 400
    assert body == {"code": "invalid_directory_selector"}
    assert scope is None


# ---------------------------------------------------------------------------
# §5.5 tolerant-ignore set
# ---------------------------------------------------------------------------

async def test_tolerant_routes_ignore_any_directory_form(stack):
    client, seen = await stack()
    try:
        cases = [
            "/slimapi/health?v=4&directory=../etc",
            "/slimapi/health?v=4&directory=/w&directory=/p",
            "/slimapi/versions?v=4&directory=x&directory=y",
            "/slimapi/versions?v=4&directory=/w",
        ]
        for url in cases:
            response = await client.get(url, headers=IDENTITY)
            assert response.status_code == 200, url
            body = orjson.loads(response.content)
            if "/health" in url:
                assert body["slimapi_contract"] == 4  # v4 view, no 400
            else:
                assert body["current"] == 4  # versions: selector-exempt
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
            "query_string": b"v=4&directory=../etc&directory=/other",
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
            ("query", 200, b"v=4&directory=/w", None),
            ("header", 400, b"v=4", "/w"),
            ("both", 400, b"v=4&directory=/w", "/w"),
            ("absent", 200, b"v=4", None),
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
            "query_string": b"v=4&directory=/w",
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
