"""Route-level integration tests for ``routes/questions.py``.

Exercises ``GET /slimapi/questions`` (cross-directory aggregation of pending
questions) end-to-end through a mocked upstream. Discovery is driven by
``GET /experimental/session?roots=true`` (opencode's GLOBAL top-level session
list — each session carries its REAL ``directory`` field, covering git repos,
non-git workdirs, and git-worktree subdirs alike). The app is constructed
fresh per test (bypassing the module-level lifespan) so the version gate and
catch-all proxy are wired exactly as in production.
"""
from __future__ import annotations

import asyncio

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import questions
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "2"}


def _settings(**overrides) -> Settings:
    """Build Settings with base defaults + per-test overrides.

    The base dict provides the minimal valid settings; calling tests may
    override any field (including the 3 new questions-budget fields which
    default to 2 MiB / 16 MiB / 8 so existing tests are unaffected).
    """
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=2, accepted_client_versions=(2, 2),
        # Per-dir /question read cap: match max_response_bytes so the existing
        # test_per_dir_cap_exceeded_errors_that_dir (>64 KiB body) still triggers.
        questions_max_response_bytes=64 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, *, _settings_obj: Settings | None = None) -> FastAPI:
    """Construct a fresh FastAPI app with the questions router wired up.

    Mirrors the real app: version middleware → questions router (before
    catch-all) → catch-all proxy → coded-exception handler. The transform
    pool is attached even though this handler doesn't use it, in case other
    middleware touches it. Creates ``app.state.questions_semaphore`` from
    settings for the per-request fan-out concurrency bound.

    If ``_settings_obj`` is provided, it is used directly; otherwise a default
    ``_settings()`` is created. This allows tests that override settings to
    construct the app with the same settings instance.
    """
    app = FastAPI(title="oc-slimapi-questions-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=(2, 2),
    )
    settings = _settings_obj if _settings_obj is not None else _settings()
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.questions_semaphore = asyncio.Semaphore(settings.questions_fanout_concurrency)
    app.include_router(questions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _question(qid: str, sid: str = "01HSESSION") -> dict:
    """A single upstream /question entry (shape: id, sessionID, questions, tool)."""
    return {
        "id": qid,
        "sessionID": sid,
        "questions": [{"id": qid, "name": "confirm", "text": "proceed?"}],
        "tool": {"name": "Bash"},
    }


def _sessions_body(*directories: str) -> bytes:
    """Build an upstream /experimental/session payload: one top-level session
    per directory. Each session carries its REAL ``directory`` field (the
    workdir it was created in) — the field the sidecar discovers on."""
    return orjson.dumps([
        {
            "id": f"ses_{i:04d}",
            "directory": d,
            "time": {"updated": 0, "created": 0},
        }
        for i, d in enumerate(directories)
    ])


# ---------------------------------------------------------------------------
# 1. Aggregates across directories
# ---------------------------------------------------------------------------


async def test_aggregates_across_directories(upstream_factory):
    """Discovery returns sessions in /a and /b; /question for each returns one
    question. items has both, each stamped with its directory; the
    X-Opencode-Directory header was forwarded per dir."""
    seen_dirs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            seen_dirs.append(d)
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_b")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    # Each entry carries the directory it came from.
    dirs_of_items = {item["directory"] for item in body["items"]}
    assert dirs_of_items == {"/a", "/b"}
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None
    # The /question calls carried the correct X-Opencode-Directory header.
    assert sorted(seen_dirs) == ["/a", "/b"]


# ---------------------------------------------------------------------------
# 2. Authoritative empty (all dirs return [])
# ---------------------------------------------------------------------------


async def test_authoritative_empty_when_all_dirs_return_empty(upstream_factory):
    """Discovery returns sessions in /a,/b; all /question calls return [].
    items==[], errors==[], authoritativeDirectories==null."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None


# ---------------------------------------------------------------------------
# 3. Partial failure (one dir 5xx)
# ---------------------------------------------------------------------------


async def test_partial_failure_one_dir_5xx(upstream_factory):
    """/a returns 1 question, /b returns HTTP 500. items has /a's question,
    errors has one entry for /b with code upstream_unavailable,
    authoritativeDirectories==['/a']."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert body["items"][0]["id"] == "que_a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# 4. Per-dir network error tolerated
# ---------------------------------------------------------------------------


async def test_per_dir_network_error_tolerated(upstream_factory):
    """/b raises httpx.ConnectError. /a's question is preserved; errors has
    an entry for /b with code upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# 5. Total failure (discovery list fails)
# ---------------------------------------------------------------------------


async def test_total_failure_discovery_5xx(upstream_factory):
    """Discovery (/experimental/session) returns 500. Expect HTTP 503 with
    body {"code":"upstream_unavailable"}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


async def test_total_failure_discovery_non_list(upstream_factory):
    """Discovery returns 200 but a non-list body → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=b'{"unexpected":"shape"}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_total_failure_discovery_network_error(upstream_factory):
    """Discovery raises httpx.ConnectError → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_total_failure_discovery_4xx(upstream_factory):
    """Discovery returns 4xx → 503 upstream_unavailable (NOT upstream_http_N —
    discovery is an internal derived call; contract §7 exception)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 6. Stamps directory on each entry (explicit)
# ---------------------------------------------------------------------------


async def test_stamps_directory_on_each_entry(upstream_factory):
    """Every item has a `directory` field equal to the dir it came from."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200,
                content=orjson.dumps([_question(f"que_{d[1:]}"), _question(f"que2_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 4
    for item in body["items"]:
        assert "directory" in item
        assert item["directory"] in {"/a", "/b"}
        # Verify the stamped directory matches the entry's id origin.
        assert item["id"].endswith(item["directory"][1:])


# ---------------------------------------------------------------------------
# 7. Distinct directories only (dedup)
# ---------------------------------------------------------------------------


async def test_distinct_directories_dedup_question_called_once(upstream_factory):
    """Discovery returns 3 sessions all in the same directory /a; /question
    for /a must be called exactly ONCE (defensive dedup). Expect 1 item."""
    call_count = {"question_a": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            # Multiple sessions sharing one directory (common: many sessions
            # per workdir). The sidecar must dedup to a single /question call.
            return httpx.Response(
                200, content=_sessions_body("/a", "/a", "/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                call_count["question_a"] += 1
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert call_count["question_a"] == 1, "/question for /a must be called exactly once"
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"


# ---------------------------------------------------------------------------
# 8. Inbound directory header ignored (sidecar discovers dirs itself)
# ---------------------------------------------------------------------------


async def test_inbound_directory_header_ignored(upstream_factory):
    """The client may send X-Opencode-Skip-Dir or no directory header; the
    endpoint ignores it and still fans out by discovered dirs."""
    seen_dirs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            seen_dirs.append(d)
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Client sends an unrelated header; sidecar must ignore it.
        response = await client.get(
            "/slimapi/questions",
            headers={**VERSION_HEADERS, "X-Opencode-Skip-Dir": "1"},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert sorted(seen_dirs) == ["/a", "/b"]
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None


# ---------------------------------------------------------------------------
# 9. Version gate
# ---------------------------------------------------------------------------


async def test_version_gate_no_header_returns_400(upstream_factory):
    """Request without X-Slimapi-Version → 400 version_required (the middleware
    covers /slimapi/questions)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions")  # no version header
    assert response.status_code == 400
    assert response.json()["code"] == "version_required"


# ---------------------------------------------------------------------------
# Additional: per-dir 4xx mapped to upstream_http_N (contract §7)
# ---------------------------------------------------------------------------


async def test_per_dir_4xx_mapped_to_upstream_http_n(upstream_factory):
    """/a returns 1 question, /b returns HTTP 403. errors entry for /b has
    code upstream_http_403 (not upstream_unavailable); /a preserved."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(403, content=b'{"error":"forbidden"}')
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_http_403"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# Additional: zero sessions → authoritative empty envelope
# ---------------------------------------------------------------------------


async def test_zero_sessions_returns_authoritative_empty(upstream_factory):
    """Discovery returns [] (no sessions at all). Expect envelope
    {items:[], errors:[], authoritativeDirectories:null, discoveryComplete:true}
    (page not full → complete; authoritative empty)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "items": [],
        "errors": [],
        "authoritativeDirectories": None,
        "discoveryComplete": True,
    }


# ---------------------------------------------------------------------------
# Additional: non-list /question body for one dir → that dir fails, others ok
# ---------------------------------------------------------------------------


async def test_per_dir_non_list_question_body_fails_that_dir(upstream_factory):
    """/a returns a list, /b returns a dict body (non-list). /b is treated as
    failed (upstream_unavailable); /a's questions preserved."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(
                    200, content=b'{"unexpected":"shape"}',
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# Additional: gzip negotiation honored (Accept-Encoding: gzip)
# ---------------------------------------------------------------------------


async def test_gzip_negotiation_honored(upstream_factory):
    """Client sends Accept-Encoding: gzip → response is gzip-encoded."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=orjson.dumps([_question("que_a")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/questions",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
        )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    body = response.json()
    assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# Discovery call shape: uses /experimental/session with roots=true&limit
# ---------------------------------------------------------------------------


async def test_discovery_uses_experimental_session_with_roots(upstream_factory):
    """The discovery call MUST hit /experimental/session with roots=true
    (top-level sessions only) and archived=true (superset — include archived
    sessions so a workdir whose top-level sessions are all archived but whose
    instance still holds pending questions is not dropped) — not /project and
    not bare /session. This is the contract that guarantees coverage of
    non-git workdirs AND archived-only workdirs."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            captured["hit"] = True
            captured["roots"] = request.url.params.get("roots")
            captured["archived"] = request.url.params.get("archived")
            captured["limit"] = request.url.params.get("limit")
            return httpx.Response(
                200, content=_sessions_body("/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/project":
            captured["project_hit"] = True  # legacy discovery, must NOT be called
        if request.url.path == "/question":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert captured.get("hit") is True
    assert captured.get("roots") == "true"
    assert captured.get("archived") == "true", (
        "archived=true MUST be sent so archived-only workdirs are not dropped"
    )
    assert captured.get("limit") is not None
    assert captured.get("project_hit") is not True, (
        "/project must NOT be used for discovery (it normalizes non-git workdirs to '/')"
    )


# ---------------------------------------------------------------------------
# CORE REGRESSION: non-git workdir discovered (the bug this fix targets)
# ---------------------------------------------------------------------------


async def test_non_git_workdir_discovered(upstream_factory):
    """REGRESSION GUARD: a session whose workdir is NOT a git repo (e.g. a
    custom working dir like /home/user/opencode_wd, or a /tmp scratch dir)
    MUST be discovered and its pending questions aggregated.

    Root cause this guards: opencode's project.resolve() maps non-git
    workdirs to the synthetic global project (worktree="/"), so the former
    /project-based discovery skipped them (worktree=="/" filter) and their
    pending questions were silently dropped. /experimental/session carries
    the REAL session.directory, so non-git workdirs are now covered.

    Upstream /project is also mocked here (returning only git workdirs) to
    prove the sidecar no longer depends on it — and would still find the
    non-git workdir's question even if /project omitted it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            # A non-git workdir (opencode_wd) + a normal git repo dir.
            return httpx.Response(
                200,
                content=_sessions_body("/home/user/opencode_wd", "/home/user/gitproj"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/project":
            # /project would ONLY list the git repo — opencode_wd is invisible
            # here (synthetic global). Proves discovery no longer relies on it.
            return httpx.Response(
                200,
                content=orjson.dumps([{"id": "p1", "worktree": "/home/user/gitproj"}]),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/home/user/opencode_wd":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_nongit", "ses_nongit")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/home/user/gitproj":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_git", "ses_git")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # BOTH workdirs discovered — the non-git one is no longer dropped.
    dirs_of_items = {item["directory"] for item in body["items"]}
    assert "/home/user/opencode_wd" in dirs_of_items, (
        "non-git workdir MUST be discovered (regression: was dropped by /project)"
    )
    assert "/home/user/gitproj" in dirs_of_items
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None


async def test_git_worktree_subdir_discovered(upstream_factory):
    """REGRESSION GUARD: a session in a git-worktree SUBDIR (e.g.
    /repo/.slim/worktrees/wave0-foo) MUST be discovered. /project lists only
    the worktree ROOT (/repo), not its git-worktree children — those were
    dropped by /project-based discovery too. /experimental/session carries
    the real subdir path."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200,
                content=_sessions_body(
                    "/repo",
                    "/repo/.slim/worktrees/wave0-deadcode",
                    "/repo/.slim/worktrees/wave0-ci",
                ),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d.split('/')[-1]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    dirs_of_items = {item["directory"] for item in body["items"]}
    assert dirs_of_items == {
        "/repo",
        "/repo/.slim/worktrees/wave0-deadcode",
        "/repo/.slim/worktrees/wave0-ci",
    }


# ---------------------------------------------------------------------------
# REGRESSION: archived-only workdir still discovered (archived=true superset)
# ---------------------------------------------------------------------------


async def test_archived_only_workdir_still_discovered(upstream_factory):
    """REGRESSION GUARD (rev-ds MINOR-1 / rev-glm M1): a workdir whose
    top-level sessions are ALL archived but whose instance still holds pending
    questions MUST still be discovered. Discovery sends archived=true so the
    session list is a superset; /question is an in-memory store independent
    of archive state, so the pending question is still retrievable.

    Without archived=true, /experimental/session excludes archived sessions
    (opencode listGlobal default `!archived`) and this workdir would vanish
    from the discovery set → pending question dropped."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            # Upstream honors archived=true → returns the archived session too.
            # (If the sidecar forgot archived=true, upstream would omit it and
            # the workdir would not be discovered.)
            assert request.url.params.get("archived") == "true"
            return httpx.Response(
                200,
                content=_sessions_body("/home/user/archived-only-wd"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/home/user/archived-only-wd":
                # Instance still alive, pending question still in memory.
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_archived", "ses_arch")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/home/user/archived-only-wd"
    assert body["items"][0]["id"] == "que_archived"
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# Discovery truncation: discoveryComplete flips when the page fills exactly
# ---------------------------------------------------------------------------


async def test_discovery_complete_when_page_not_full(upstream_factory):
    """Normal case: fewer sessions than _DISCOVERY_LIMIT → discoveryComplete
    is true, and with no per-dir errors → authoritativeDirectories null
    (global authority, replace-all safe)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["discoveryComplete"] is True
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None


async def test_discovery_incomplete_when_page_full_degrades_authority(upstream_factory, monkeypatch):
    """When the discovery page fills EXACTLY at _DISCOVERY_LIMIT (possible
    truncation), discoveryComplete flips to false. Even with NO per-dir
    errors, authoritativeDirectories must degrade to the succeeded list (NOT
    null) — so the client does NOT replace-all and drop pending questions in
    undiscovered dirs. Uses a tiny monkeypatched limit to avoid building 10k
    sessions."""
    monkeypatch.setattr(questions, "_DISCOVERY_LIMIT", 3)
    # Exactly 3 sessions (= limit) → truncation suspected.
    dirs = ["/d0", "/d1", "/d2"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["discoveryComplete"] is False
    # No errors BUT discovery incomplete → succeeded list, not null.
    assert body["errors"] == []
    assert body["authoritativeDirectories"] == dirs


async def test_complete_discovery_full_success_is_globally_authoritative(upstream_factory):
    """Discovery returns sessions in 2 distinct dirs and every dir's
    /question succeeds. discoveryComplete == true AND
    authoritativeDirectories == null (global authority → client replace-all
    is safe)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["discoveryComplete"] is True
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None
    assert len(body["items"]) == 2


async def test_partial_failure_with_complete_discovery_lists_succeeded(upstream_factory):
    """Complete discovery + one per-dir failure → authoritativeDirectories is
    the succeeded list (NOT null), discoveryComplete == true. Confirms the
    partial-authority rule also fires on per-dir errors, not just truncation."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["discoveryComplete"] is True
    assert body["authoritativeDirectories"] == ["/a"]
    assert len(body["errors"]) == 1


# ---------------------------------------------------------------------------
# Fan-out concurrency bound: more dirs than the config fan-out concurrency cap
# still completes correctly (no deadlock, per-dir isolation intact).
# ---------------------------------------------------------------------------


async def test_fanout_concurrency_bound_completes_with_many_dirs(upstream_factory):
    """Discover more dirs than the fan-out concurrency cap; the semaphore must
    bound in-flight /question calls without deadlock. Every dir succeeds →
    all items merged, no errors, global authority (discovery complete)."""
    settings = _settings()
    n_dirs = settings.questions_fanout_concurrency + 8  # exceed cap
    dirs = [f"/dir{i}" for i in range(n_dirs)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == n_dirs
    assert body["errors"] == []
    assert body["discoveryComplete"] is True
    assert body["authoritativeDirectories"] is None
    # Each item stamped with its directory; all dirs covered exactly once.
    assert {item["directory"] for item in body["items"]} == set(dirs)


async def test_fanout_concurrency_bound_isolates_errors_with_many_dirs(upstream_factory):
    """With many dirs and one failing (5xx), the failing dir lands in errors[]
    and the rest succeed — proves per-dir isolation holds under the bounded
    fan-out."""
    settings = _settings()
    n_dirs = settings.questions_fanout_concurrency + 4
    dirs = [f"/dir{i}" for i in range(n_dirs)]
    failing_dir = dirs[len(dirs) // 2]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == failing_dir:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == n_dirs - 1
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": failing_dir, "code": "upstream_unavailable"}
    # Partial authority (one error) → succeeded list, not null.
    assert body["authoritativeDirectories"] is not None
    assert failing_dir not in body["authoritativeDirectories"]


# ---------------------------------------------------------------------------
# Sessions missing/blank directory field are skipped defensively
# ---------------------------------------------------------------------------


async def test_sessions_missing_directory_field_skipped(upstream_factory):
    """A session whose `directory` is missing/empty/non-string is skipped
    (defensive — malformed upstream); well-formed sessions are still
    discovered."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200,
                content=orjson.dumps([
                    {"id": "s1", "directory": "/good"},
                    {"id": "s2", "directory": ""},            # empty → skip
                    {"id": "s3"},                              # missing → skip
                    {"id": "s4", "directory": None},           # non-string → skip
                    {"id": "s5", "directory": "/also-good"},
                ]),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    dirs_of_items = {item["directory"] for item in body["items"]}
    assert dirs_of_items == {"/good", "/also-good"}
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# read_with_cap: discovery cap exceeded → 503 total failure (no envelope)
# ---------------------------------------------------------------------------


async def test_discovery_cap_exceeded_returns_503(upstream_factory):
    """Discovery returns a 200 body exceeding max_response_bytes (64 KiB) →
    503 upstream_unavailable (total failure, no envelope)."""
    # Build a sessions body large enough to exceed the 64 KiB cap.
    many_dirs = [f"/dir{i:04d}" for i in range(3000)]
    big_body = _sessions_body(*many_dirs)
    assert len(big_body) > 64 * 1024, "test payload must exceed max_response_bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=big_body,
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


# ---------------------------------------------------------------------------
# read_with_cap: per-dir cap exceeded → that dir in errors[], others succeed
# ---------------------------------------------------------------------------


async def test_per_dir_cap_exceeded_errors_that_dir(upstream_factory):
    """/a returns small /question response, /b returns huge /question response
    exceeding max_response_bytes. /b lands in errors[] with
    upstream_unavailable, /a's question preserved."""
    big_questions = [_question(f"que_{i:04d}") for i in range(5000)]
    big_body = orjson.dumps(big_questions)
    assert len(big_body) > 64 * 1024, "test payload must exceed max_response_bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(
                    200, content=big_body,
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert body["items"][0]["id"] == "que_a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# P1-28: aggregate item budget + serialise offload
# ---------------------------------------------------------------------------


async def test_aggregate_truncation_marks_envelope(upstream_factory, monkeypatch):
    """When the merged item count exceeds _MAX_AGGREGATE_ITEMS, the envelope
    is marked ``truncated: true`` and authoritativeDirectories degrades to
    the succeeded list (NOT null — partial-replace so the client does not
    discard pending questions from skipped dirs)."""
    # Set a tiny cap so we can trigger truncation with a small test.
    monkeypatch.setattr(questions, "_MAX_AGGREGATE_ITEMS", 2)
    dirs = ["/a", "/b", "/c"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # Items from /a and /b fill the cap (2); /c is skipped.
    assert len(body["items"]) <= 2
    assert body.get("truncated") is True
    # Partial-replace: authoritative is the succeeded list, not null.
    assert body["authoritativeDirectories"] is not None
    assert body["errors"] == []


async def test_no_truncation_when_under_budget(upstream_factory):
    """Normal case: items under _MAX_AGGREGATE_ITEMS → no truncated field."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=orjson.dumps([_question("que")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert "truncated" not in body


async def test_serialise_offload_returns_valid_response(upstream_factory):
    """P1-28: the final envelope serialisation is offloaded to the transform
    executor. The response must still be valid JSON with the expected shape."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=orjson.dumps([_question("que_a")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert body["authoritativeDirectories"] is None


# ---------------------------------------------------------------------------
# T5-C1: fan-out concurrency respects config.questions_fanout_concurrency
# ---------------------------------------------------------------------------


class _CountingSem:
    """Semaphore wrapper that records the peak number of concurrent holders.

    Each ``__aenter__`` increments ``_active`` and updates ``high`` (peak),
    then yields control via ``sleep(0)`` so sibling tasks in the initial
    scheduler window can also enter before any of them proceed to the actual
    work (making the peak deterministic for the test).
    """

    def __init__(self, n: int):
        self._sem = asyncio.Semaphore(n)
        self._active = 0
        self.high = 0

    async def __aenter__(self):
        await self._sem.acquire()
        self._active += 1
        self.high = max(self.high, self._active)
        await asyncio.sleep(0)   # deterministic overlap: let siblings enter
        return self

    async def __aexit__(self, *exc):
        self._active -= 1
        self._sem.release()


async def test_fanout_concurrency_respects_config_cap(upstream_factory):
    """T5-C1: single-request fanout never exceeds config.questions_fanout_concurrency.

    Build app with fanout=3, replace the semaphore with _CountingSem(3),
    discover 8 dirs. The sliding window scheduler launches 3 tasks initially;
    all 3 acquire the semaphore → high reaches exactly 3. All 8 items present,
    no errors.
    """
    settings = _settings(questions_fanout_concurrency=3)
    dirs = [f"/d{i}" for i in range(8)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_d{dirs.index(request.headers.get('x-opencode-directory',''))}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    app.state.questions_semaphore = _CountingSem(3)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # Peak concurrency reached exactly 3 (initial window).
    assert app.state.questions_semaphore.high == 3
    assert len(body["items"]) == 8
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# T5-C2: per-dir read cap applies questions_max_response_bytes
# ---------------------------------------------------------------------------


async def test_per_dir_read_cap_applies_questions_budget(upstream_factory):
    """T5-C2: a dir whose /question body exceeds questions_max_response_bytes
    → that dir in errors[] (upstream_unavailable) with body_bytes=0 (does NOT
    occupy aggregate budget). The aggregate cap is set large enough to prove
    the per-dir cap is the binding constraint and the failed dir doesn't
    trigger truncation.
    """
    settings = _settings(
        questions_max_response_bytes=256,
        questions_max_aggregate_bytes=4096,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                # Small body (~80 bytes) — under per-dir cap
                return httpx.Response(
                    200, content=orjson.dumps([_question("que_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                # Body > 256 bytes — exceeds per-dir cap
                large_text = "X" * 500
                return httpx.Response(
                    200, content=orjson.dumps([{
                        "id": "que_b", "sessionID": "01HSESSION",
                        "questions": [{"id": "que_b", "name": "confirm", "text": large_text}],
                        "tool": {"name": "Bash"},
                    }]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # /a's question present; /b in errors
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    # Aggregate cap not triggered → no truncated field
    assert "truncated" not in body
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# T5-C3: aggregate byte budget truncation + cancellation
# ---------------------------------------------------------------------------


def _approx_250_byte_question(qid: str) -> bytes:
    """Return a /question response body of exactly 250 bytes (1 question with padding).

    Validated empirically: ``text="pad_" + "X" * 130`` produces a 250-byte
    JSON array body. This gives deterministic budget arithmetic in tests."""
    text = "pad_" + "X" * 130  # 134 chars, total body = 250 bytes
    return orjson.dumps([{
        "id": qid, "sessionID": "01HSESSION",
        "questions": [{"id": qid, "name": "confirm", "text": text}],
        "tool": {"name": "Bash"},
    }])


async def test_aggregate_byte_budget_truncates_and_cancels_remaining(upstream_factory):
    """T5-C3: aggregate byte cap exceeded → truncated:true, current dir not
    added, remaining dirs not started, in-flight unconsumed tasks cancelled."""
    settings = _settings(
        questions_max_response_bytes=4096,
        questions_max_aggregate_bytes=500,
        questions_fanout_concurrency=3,
    )
    dirs = [f"/d{i}" for i in range(6)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            return httpx.Response(
                200, content=_approx_250_byte_question("que"),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # d0 (250) + d1 (250) = 500 accepted; d2 (250) triggers 500+250>500
    assert body.get("truncated") is True
    assert len(body["items"]) == 2
    assert body["errors"] == []
    assert body["authoritativeDirectories"] == ["/d0", "/d1"]


# ---------------------------------------------------------------------------
# T5-C4: item-cap truncation degrades authority
# ---------------------------------------------------------------------------


async def test_aggregate_truncation_degrades_authority(upstream_factory, monkeypatch):
    """T5-C4: item cap (_MAX_AGGREGATE_ITEMS) triggers truncated + partial
    authority. Cancelled/un-started dirs absent from both errors and items."""
    monkeypatch.setattr(questions, "_MAX_AGGREGATE_ITEMS", 2)
    dirs = ["/a", "/b", "/c"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_question(f"que_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # /a (1 item) + /b (1 item) accepted; /c triggers 1+1+1 > 2 → truncate
    assert body.get("truncated") is True
    assert body["authoritativeDirectories"] == ["/a", "/b"]
    assert body["errors"] == []
    assert len(body["items"]) == 2
    assert {item["directory"] for item in body["items"]} == {"/a", "/b"}


# ---------------------------------------------------------------------------
# T5-C5: cancellation closes all in-flight responses
# ---------------------------------------------------------------------------


async def test_cancel_closes_inflight_responses(upstream_factory):
    """T5-C5: on truncation/cancellation, every opened response is aclose()d
    (no leak), including cancelled in-flight tasks."""
    settings = _settings(
        questions_max_response_bytes=4096,
        questions_max_aggregate_bytes=500,
        questions_fanout_concurrency=3,
    )
    dirs = [f"/d{i}" for i in range(6)]

    aclose_count = 0

    class _SpiedResponse(httpx.Response):
        async def aclose(self):
            nonlocal aclose_count
            aclose_count += 1
            await super().aclose()

    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/question":
            requests_seen.append(request.headers.get("x-opencode-directory", "?"))
            return _SpiedResponse(
                200, content=_approx_250_byte_question("que"),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    # Every opened response must be closed — success, overflow, AND cancelled.
    # Scheduler: initial window launches d0,d1,d2; consumes d0 (launches d3),
    # d1 (launches d4), d2 → truncate (cancels d3,d4). So 5 requests total.
    assert aclose_count == len(requests_seen), (
        f"aclose_count={aclose_count} != requests_seen={len(requests_seen)}"
    )
    body = response.json()
    assert body.get("truncated") is True
