"""Route-level integration tests for ``routes/permissions.py``.

Exercises ``GET /slimapi/permissions`` (cross-directory aggregation of pending
permission cards) end-to-end through a mocked upstream. Discovery is driven by
``GET /experimental/session?roots=true`` (opencode's GLOBAL top-level session
list — each session carries its REAL ``directory`` field, covering git repos,
non-git workdirs, and git-worktree subdirs alike). The app is constructed
fresh per test (bypassing the module-level lifespan) so the version gate and
catch-all proxy are wired exactly as in production.

Upstream shape (B1, opencode v1.18.16): ``GET /permission`` returns a **bare
array** ``PermissionV1.Request[]`` (NOT an ``{items:}`` wrapper). Field shape:
``{id, sessionID, permission, patterns, metadata, always, tool?}``. The
endpoint is per-Location, routed via ``X-Opencode-Directory`` — the sidecar
fans out across discovered directories and merges into the questions-style
envelope ``{items, errors, authoritativeDirectories, discoveryComplete}``.
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
from oc_slimapi.routes import permissions
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}

# B1-locked fixture: upstream `PermissionV1.Request` (bare array element).
# Fields locked from packages/schema/src/v1/permission.ts (v1.18.16):
# id (startsWith "per"), sessionID, permission, patterns, metadata, always,
# tool? (optional Struct{messageID, callID}).
PENDING_PERMISSION_FIXTURE = {
    "id": "per_01abc",
    "sessionID": "01HSESSION",
    "permission": "bash",
    "patterns": ["*"],
    "metadata": {"tool": "Bash"},
    "always": [],
    "tool": {"messageID": "msg_1", "callID": "call_1"},
}


def _settings(**overrides) -> Settings:
    """Build Settings with base defaults + per-test overrides.

    The base dict provides the minimal valid settings; calling tests may
    override any field (including the 3 permissions-budget fields which
    default to 2 MiB / 16 MiB / 8 so existing tests are unaffected).
    """
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        # Per-dir /permission read cap: match max_response_bytes so the
        # per-dir cap-exceeded test (>64 KiB body) still triggers.
        permissions_max_response_bytes=64 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, *, _settings_obj: Settings | None = None) -> FastAPI:
    """Construct a fresh FastAPI app with the permissions router wired up.

    Mirrors the real app: version middleware → permissions router (before
    catch-all) → catch-all proxy → coded-exception handler. The transform
    pool is attached even though this handler doesn't use it, in case other
    middleware touches it. Creates ``app.state.permissions_semaphore`` from
    settings for the per-request fan-out concurrency bound.

    If ``_settings_obj`` is provided, it is used directly; otherwise a default
    ``_settings()`` is created. This allows tests that override settings to
    construct the app with the same settings instance.
    """
    app = FastAPI(title="oc-slimapi-permissions-test")
    settings = _settings_obj if _settings_obj is not None else _settings()
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.permissions_semaphore = asyncio.Semaphore(settings.permissions_fanout)
    app.include_router(permissions.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _permission(pid: str, sid: str = "01HSESSION") -> dict:
    """A single upstream /permission entry (bare PermissionV1.Request shape)."""
    return {
        "id": pid,
        "sessionID": sid,
        "permission": "bash",
        "patterns": ["*"],
        "metadata": {"tool": "Bash"},
        "always": [],
        "tool": {"messageID": "msg_1", "callID": "call_1"},
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
    """Discovery returns sessions in /a and /b; /permission for each returns one
    card. items has both, each stamped with its directory; the
    X-Opencode-Directory header was forwarded per dir."""
    seen_dirs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            seen_dirs.append(d)
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_b")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    # Each entry carries the directory it came from.
    dirs_of_items = {item["directory"] for item in body["items"]}
    assert dirs_of_items == {"/a", "/b"}
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None
    # The /permission calls carried the correct X-Opencode-Directory header.
    assert sorted(seen_dirs) == ["/a", "/b"]


# ---------------------------------------------------------------------------
# 2. Authoritative empty (all dirs return [])
# ---------------------------------------------------------------------------


async def test_authoritative_empty_when_all_dirs_return_empty(upstream_factory):
    """Discovery returns sessions in /a,/b; all /permission calls return [].
    items==[], errors==[], authoritativeDirectories==null."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None
    assert body["discoveryComplete"] is True


# ---------------------------------------------------------------------------
# 3. Partial failure (one dir 5xx)
# ---------------------------------------------------------------------------


async def test_partial_failure_one_dir_5xx(upstream_factory):
    """/a returns 1 card, /b returns HTTP 500. items has /a's card, errors has
    one entry for /b with code upstream_unavailable,
    authoritativeDirectories==['/a']."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert body["items"][0]["id"] == "per_a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# 4. Per-dir network error tolerated
# ---------------------------------------------------------------------------


async def test_per_dir_network_error_tolerated(upstream_factory):
    """/b raises httpx.ConnectError. /a's card is preserved; errors has an
    entry for /b with code upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200,
                content=orjson.dumps([_permission(f"per_{d[1:]}"), _permission(f"per2_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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


async def test_distinct_directories_dedup_permission_called_once(upstream_factory):
    """Discovery returns 3 sessions all in the same directory /a; /permission
    for /a must be called exactly ONCE (defensive dedup). Expect 1 item."""
    call_count = {"permission_a": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            # Multiple sessions sharing one directory (common: many sessions
            # per workdir). The sidecar must dedup to a single /permission call.
            return httpx.Response(
                200, content=_sessions_body("/a", "/a", "/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                call_count["permission_a"] += 1
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert call_count["permission_a"] == 1, "/permission for /a must be called exactly once"
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"


# ---------------------------------------------------------------------------
# 8. Inbound directory header ignored (sidecar discovers dirs itself)
# ---------------------------------------------------------------------------


async def test_inbound_directory_header_ignored(upstream_factory):
    """A real inbound X-Opencode-Directory header (the kind a client might send
    to a per-directory endpoint) must be ignored: the sidecar discovers workdirs
    itself and fans out /permission with the DISCOVERED directories only. The
    client's value must never reach upstream (otherwise a client could force
    routing to an arbitrary directory)."""
    # Discovery-derived dirs (from /experimental/session) — clearly distinct
    # from the client-sent value so a mistaken propagation is easy to spot.
    discovered = ("/srv/proj", "/srv/other")
    client_dir = "/client/sent/dir"
    seen_dirs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*discovered),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            seen_dirs.append(d)
            return httpx.Response(
                200, content=orjson.dumps([_permission(f"per_{d}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Client sends a REAL X-Opencode-Directory header, distinct from the
        # discovered dirs — the endpoint must ignore it and discover itself.
        response = await client.get(
            "/slimapi/permissions",
            headers={**VERSION_HEADERS, "X-Opencode-Directory": client_dir},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    # Every /permission call was routed by a DISCOVERED dir — the client's
    # value never appears.
    assert sorted(seen_dirs) == sorted(discovered)
    assert client_dir not in seen_dirs
    # Items are stamped with the discovered dirs, not the client's.
    assert {item["directory"] for item in body["items"]} == set(discovered)
    assert body["errors"] == []
    assert body["authoritativeDirectories"] is None


# ---------------------------------------------------------------------------
# 9. Version gate
# ---------------------------------------------------------------------------


async def test_retired_version_header_ignored_at_route_level(upstream_factory):
    """§1 terminal: X-Slimapi-Version is dead input; the route answers."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions",
                                    headers={"X-Slimapi-Version": "9"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Additional: per-dir 4xx mapped to upstream_http_N (contract §7)
# ---------------------------------------------------------------------------


async def test_per_dir_4xx_mapped_to_upstream_http_n(upstream_factory):
    """/a returns 1 card, /b returns HTTP 403. errors entry for /b has code
    upstream_http_403 (not upstream_unavailable); /a preserved."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
                    headers={"Content-Type": "application/json"},
                )
            if d == "/b":
                return httpx.Response(403, content=b'{"error":"forbidden"}')
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "items": [],
        "errors": [],
        "authoritativeDirectories": None,
        "discoveryComplete": True,
    }


# ---------------------------------------------------------------------------
# Additional: non-list /permission body for one dir → that dir fails, others ok
# ---------------------------------------------------------------------------


async def test_per_dir_non_list_permission_body_fails_that_dir(upstream_factory):
    """/a returns a list, /b returns a dict body (non-list). /b is treated as
    failed (upstream_unavailable); /a's cards preserved."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]


# ---------------------------------------------------------------------------
# B1/B-C2: whitelist projection — unknown fields dropped, known fields kept
# ---------------------------------------------------------------------------


async def test_whitelist_projection_drops_unknown_fields(upstream_factory):
    """An upstream /permission entry carrying extra/unknown fields is projected
    to the PermissionV1.Request whitelist (id, sessionID, permission, patterns,
    metadata, always, tool) — unknown fields are dropped (defensive slimming),
    known fields preserved in order, then directory stamped last."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            return httpx.Response(
                200,
                content=orjson.dumps([{
                    **PENDING_PERMISSION_FIXTURE,
                    "unexpectedExtra": "drop-me",
                    "anotherUnknown": 123,
                }]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == PENDING_PERMISSION_FIXTURE["id"]
    assert item["sessionID"] == PENDING_PERMISSION_FIXTURE["sessionID"]
    assert item["permission"] == PENDING_PERMISSION_FIXTURE["permission"]
    assert item["patterns"] == PENDING_PERMISSION_FIXTURE["patterns"]
    assert item["metadata"] == PENDING_PERMISSION_FIXTURE["metadata"]
    assert item["always"] == PENDING_PERMISSION_FIXTURE["always"]
    assert item["tool"] == PENDING_PERMISSION_FIXTURE["tool"]
    assert item["directory"] == "/a"
    assert "unexpectedExtra" not in item
    assert "anotherUnknown" not in item
    # Field order: whitelist fields then directory (defensive — client parses
    # by key, but the questions precedent stamps directory last).
    assert list(item.keys()) == [
        "id", "sessionID", "permission", "patterns", "metadata", "always",
        "tool", "directory",
    ]


async def test_whitelist_projection_optional_tool_omitted(upstream_factory):
    """`tool` is optional in PermissionV1.Request; when absent the projection
    simply omits it (no key invented)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            no_tool = {k: v for k, v in PENDING_PERMISSION_FIXTURE.items() if k != "tool"}
            return httpx.Response(
                200, content=orjson.dumps([no_tool]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "tool" not in item
    assert item["id"] == PENDING_PERMISSION_FIXTURE["id"]


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
        if request.url.path == "/permission":
            return httpx.Response(
                200, content=orjson.dumps([_permission("per_a")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/permissions",
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
    instance still holds pending cards is not dropped) — not /project and not
    bare /session."""
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
        if request.url.path == "/permission":
            return httpx.Response(
                200, content=b"[]", headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
# Fan-out concurrency bound: more dirs than the config fan-out concurrency cap
# still completes correctly (no deadlock, per-dir isolation intact).
# ---------------------------------------------------------------------------


async def test_fanout_concurrency_bound_completes_with_many_dirs(upstream_factory):
    """Discover more dirs than the fan-out concurrency cap; the semaphore must
    bound in-flight /permission calls without deadlock.

    The mock handler is ASYNC and opens an overlapping window (asyncio.sleep)
    while tracking a current/peak in-flight counter (enter +1, exit -1). If the
    fan-out knob did not actually take effect (e.g. the semaphore were missing
    or sized wrong), all n_dirs requests would overlap at once and peak would
    exceed ``permissions_fanout``. We assert ``peak <= fanout`` (the bound) and
    ``peak >= 2`` (the window genuinely overlapped — otherwise a serial
    implementation would trivially pass the bound with peak == 1). Completion
    is asserted under ``asyncio.wait_for`` so a deadlock surfaces as a timeout
    failure rather than a hang.
    """
    settings = _settings()
    fanout = settings.permissions_fanout
    n_dirs = fanout + 8  # exceed cap
    dirs = [f"/dir{i}" for i in range(n_dirs)]

    state = {"current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            # Enter the in-flight window (async — yields to the event loop so
            # concurrent /permission calls genuinely overlap).
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            try:
                await asyncio.sleep(0.05)
                d = request.headers.get("x-opencode-directory")
                return httpx.Response(
                    200, content=orjson.dumps([_permission(f"per_{d}")]),
                    headers={"Content-Type": "application/json"},
                )
            finally:
                # Exit the in-flight window — always, even on cancellation.
                state["current"] -= 1
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Timeout-wrapped: a deadlock in the fan-out scheduler fails fast.
        response = await asyncio.wait_for(
            client.get("/slimapi/permissions", headers=VERSION_HEADERS),
            timeout=10.0,
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == n_dirs
    assert body["errors"] == []
    assert body["discoveryComplete"] is True
    assert body["authoritativeDirectories"] is None
    # The concurrency bound held: at no point were more than `fanout` /permission
    # requests in flight simultaneously.
    assert state["peak"] <= fanout, (
        f"fan-out bound violated: peak in-flight {state['peak']} > "
        f"permissions_fanout {fanout}"
    )
    # The window genuinely overlapped (peak > 1) — a serial implementation would
    # never exercise the concurrency bound and would trivially satisfy peak <= 1.
    assert state["peak"] >= 2, (
        "test is meaningless: no overlapping /permission window formed "
        f"(peak in-flight {state['peak']})"
    )
    assert state["current"] == 0, "in-flight counter must drain to zero"
    # Each item stamped with its directory; all dirs covered exactly once.
    assert {item["directory"] for item in body["items"]} == set(dirs)


async def test_fanout_concurrency_bound_isolates_errors_with_many_dirs(upstream_factory):
    """With many dirs and one failing (5xx), the failing dir lands in errors[]
    and the rest succeed — proves per-dir isolation holds under the bounded
    fan-out."""
    settings = _settings()
    n_dirs = settings.permissions_fanout + 4
    dirs = [f"/dir{i}" for i in range(n_dirs)]
    failing_dir = dirs[len(dirs) // 2]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body(*dirs),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == failing_dir:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(
                200, content=orjson.dumps([_permission(f"per_{d}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings_obj=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            return httpx.Response(
                200, content=orjson.dumps([_permission(f"per_{d[1:]}")]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


# ---------------------------------------------------------------------------
# read_with_cap: per-dir cap exceeded → that dir in errors[], others succeed
# ---------------------------------------------------------------------------


async def test_per_dir_cap_exceeded_errors_that_dir(upstream_factory):
    """/a returns small /permission response, /b returns huge /permission
    response exceeding max_response_bytes. /b lands in errors[] with
    upstream_unavailable, /a's card preserved."""
    big_body = orjson.dumps([_permission(f"per_{i}") for i in range(4000)])
    assert len(big_body) > 64 * 1024, "test payload must exceed the per-dir cap"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=_sessions_body("/a", "/b"),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/permission":
            d = request.headers.get("x-opencode-directory")
            if d == "/a":
                return httpx.Response(
                    200, content=orjson.dumps([_permission("per_a")]),
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
        response = await client.get("/slimapi/permissions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/a"
    assert len(body["errors"]) == 1
    assert body["errors"][0] == {"directory": "/b", "code": "upstream_unavailable"}
    assert body["authoritativeDirectories"] == ["/a"]
