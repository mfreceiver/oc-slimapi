"""Route-level integration tests for ``routes/questions.py``.

Exercises ``GET /slimapi/questions`` (cross-directory aggregation of pending
questions) end-to-end through a mocked upstream. Discovery is driven by
``GET /project`` (returns ProjectTable in full — the ``worktree`` field of
each entry is the workdir). The app is constructed fresh per test (bypassing
the module-level lifespan) so the version gate and catch-all proxy are wired
exactly as in production.
"""
from __future__ import annotations

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


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=2, accepted_client_versions=(2, 2),
    )


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    """Construct a fresh FastAPI app with the questions router wired up.

    Mirrors the real app: version middleware → questions router (before
    catch-all) → catch-all proxy → coded-exception handler. The transform
    pool is attached even though this handler doesn't use it, in case other
    middleware touches it.
    """
    app = FastAPI(title="oc-slimapi-questions-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=(2, 2),
    )
    settings = _settings()
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
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


def _projects_body(*worktrees: str) -> bytes:
    """Build an upstream /project list payload with one project per worktree."""
    return orjson.dumps(
        [{"id": f"proj_{i:03d}", "worktree": w} for i, w in enumerate(worktrees)]
    )


# ---------------------------------------------------------------------------
# 1. Aggregates across directories
# ---------------------------------------------------------------------------


async def test_aggregates_across_directories(upstream_factory):
    """/project returns worktrees /a and /b; /question for each returns one
    question. items has both, each stamped with its directory; the
    X-Opencode-Directory header was forwarded per dir."""
    seen_dirs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
    """/project returns worktrees /a,/b; all /question calls return [].
    items==[], errors==[], authoritativeDirectories==null."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
# 5. Total failure (project list fails)
# ---------------------------------------------------------------------------


async def test_total_failure_project_list_5xx(upstream_factory):
    """/project returns 500. Expect HTTP 503 with body {"code":"upstream_unavailable"}."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


async def test_total_failure_project_list_non_list(upstream_factory):
    """/project returns 200 but a non-list body → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
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


async def test_total_failure_project_list_network_error(upstream_factory):
    """/project raises httpx.ConnectError → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            raise httpx.ConnectError("simulated", request=request)
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
    """/project returns 3 entries with the same worktree /a (artificial
    duplicates — the DB never returns dup worktrees, but this verifies the
    sidecar's defensive dedup); /question for /a must be called exactly
    ONCE. Expect 1 item."""
    call_count = {"question_a": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            # /project never returns duplicate worktrees in practice (DB
            # unique), but construct duplicates here to verify the defensive
            # dedup logic in the sidecar.
            return httpx.Response(
                200,
                content=orjson.dumps([
                    {"id": "proj_a1", "worktree": "/a"},
                    {"id": "proj_a2", "worktree": "/a"},
                    {"id": "proj_a3", "worktree": "/a"},
                ]),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
# Additional: zero projects → authoritative empty envelope
# ---------------------------------------------------------------------------


async def test_zero_projects_returns_authoritative_empty(upstream_factory):
    """/project returns [] (no projects at all). Expect envelope
    {items:[], errors:[], authoritativeDirectories:null, discoveryComplete:true}
    (discovery is always complete — /project returns ProjectTable in full)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a"),
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
# /project discovery completeness (P1): ProjectTable is returned in full
# (no pagination), so discovery is NEVER truncated — even for large project
# counts, no error → authoritativeDirectories stays null (global authority).
# ---------------------------------------------------------------------------


async def test_project_discovery_always_complete_no_truncation(upstream_factory):
    """/project returns ProjectTable in full (no limit/pagination). Even with
    a large number of projects, discovery is always complete: there is no
    truncation-downgrade path. With no per-dir errors, the envelope must
    claim global authority: discoveryComplete == true AND
    authoritativeDirectories == null."""
    # 50 distinct worktrees (/d0../d49) — well above any legacy limit, but
    # /project has no pagination so this is still complete.
    projects = [
        {"id": f"proj_{i:03d}", "worktree": f"/d{i}"} for i in range(50)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200, content=orjson.dumps(projects),
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
    # No pagination → always complete; no errors → global authority.
    assert body["discoveryComplete"] is True
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None
    assert body["items"] == []


async def test_complete_discovery_full_success_is_globally_authoritative(upstream_factory):
    """/project returns 5 projects (2 distinct worktrees) and every dir's
    /question succeeds. discoveryComplete == true AND
    authoritativeDirectories == null (global authority → client replace-all
    is safe)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            # 5 projects, 2 distinct worktrees (dups verify dedup).
            return httpx.Response(
                200, content=_projects_body("/a", "/b", "/a", "/b", "/a"),
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
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body("/a", "/b"),
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
# Fan-out concurrency bound (P1): more dirs than _FANOUT_CONCURRENCY still
# completes correctly (no deadlock, per-dir isolation intact).
# ---------------------------------------------------------------------------


async def test_fanout_concurrency_bound_completes_with_many_dirs(upstream_factory):
    """Discover more dirs than _FANOUT_CONCURRENCY; the semaphore must bound
    in-flight /question calls without deadlock. Every dir succeeds → all items
    merged, no errors, global authority (discovery complete)."""
    from oc_slimapi.routes.questions import _FANOUT_CONCURRENCY

    n_dirs = _FANOUT_CONCURRENCY + 8  # exceed the cap to exercise the wait path
    dirs = [f"/dir{i}" for i in range(n_dirs)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body(*dirs),
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
    app = _build_app(upstream)
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
    from oc_slimapi.routes.questions import _FANOUT_CONCURRENCY

    n_dirs = _FANOUT_CONCURRENCY + 4
    dirs = [f"/dir{i}" for i in range(n_dirs)]
    failing_dir = dirs[len(dirs) // 2]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(
                200, content=_projects_body(*dirs),
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
    app = _build_app(upstream)
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
