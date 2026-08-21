"""Route-level integration tests for ``routes/directories.py``.

Exercises ``GET /slimapi/directories`` (global directory catalog) end-to-end
through a mocked upstream. Discovery is driven by
``GET /experimental/session?roots=true&archived=true&limit=10000`` (opencode's
GLOBAL top-level session list — each session carries its REAL ``directory``
field). The app is constructed fresh per test (bypassing the module-level
lifespan) so the version gate and catch-all proxy are wired exactly as in
production. Mock/fixture pattern mirrors ``tests/test_questions_routes.py``.
"""
from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import discovery
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import directories
from oc_slimapi.traffic import bucketize
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}


def _settings(**overrides) -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        **overrides,
    )


def _build_app(upstream: httpx.AsyncClient, settings: Settings | None = None) -> FastAPI:
    """Construct a fresh FastAPI app with the directories router wired up.

    Mirrors the real app: version middleware → directories router (before
    catch-all) → catch-all proxy → coded-exception handler.
    """
    app = FastAPI(title="oc-slimapi-directories-test")
    settings = _settings() if settings is None else settings
    app.state.config = settings
    app.state.upstream = upstream
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(directories.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _session(
    directory: str,
    *,
    sid: str = "ses_x",
    title: str | None = "t",
    updated: int = 0,
    created: int = 0,
    archived: int | None = None,
) -> dict:
    """Build one top-level session for the upstream /experimental/session list."""
    time: dict = {"updated": updated, "created": created}
    if archived is not None:
        time["archived"] = archived
    s: dict = {"id": sid, "directory": directory, "time": time}
    if title is not None:
        s["title"] = title
    return s


def _sessions_body(*sessions: dict) -> bytes:
    return orjson.dumps(list(sessions))


def _discovery_handler(*sessions: dict):
    """A handler that returns the given sessions for /experimental/session."""
    body = _sessions_body(*sessions)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=body, headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    return handler


async def _get(upstream, settings: Settings | None = None, **client_headers) -> httpx.Response:
    app = _build_app(upstream, settings=settings)
    transport = httpx.ASGITransport(app=app)
    headers = {**VERSION_HEADERS, **client_headers}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/slimapi/directories", headers=headers)


# ---------------------------------------------------------------------------
# 1. Authoritative empty
# ---------------------------------------------------------------------------


async def test_empty_sessions_returns_authoritative_empty(upstream_factory):
    """Discovery returns [] (no sessions) → {items:[], discoveryComplete:true}, 200."""
    upstream = upstream_factory(_discovery_handler())
    response = await _get(upstream)
    assert response.status_code == 200
    assert response.json() == {"items": [], "discoveryComplete": True}


# ---------------------------------------------------------------------------
# 2. Normal: 2 directories, multiple sessions each → rootSessionCount correct
# ---------------------------------------------------------------------------


async def test_normal_aggregation_counts(upstream_factory):
    """/a has 2 sessions, /b has 1. Two rows, rootSessionCount 2 and 1."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/a", sid="a2", updated=20),
        _session("/b", sid="b1", updated=30),
    ))
    response = await _get(upstream)
    assert response.status_code == 200
    body = response.json()
    assert body["discoveryComplete"] is True
    by_dir = {it["directory"]: it for it in body["items"]}
    assert by_dir["/a"]["rootSessionCount"] == 2
    assert by_dir["/a"]["activeRootSessionCount"] == 2
    assert by_dir["/a"]["archivedRootSessionCount"] == 0
    assert by_dir["/a"]["archivedOnly"] is False
    assert by_dir["/b"]["rootSessionCount"] == 1


# ---------------------------------------------------------------------------
# 3. Duplicate directory (/a vs /a/) → normalize merges into one row
# ---------------------------------------------------------------------------


async def test_duplicate_directory_normalizes_and_merges(upstream_factory):
    """One session in /a, one in /a/ → merged into a single /a row (count 2)."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/a/", sid="a2", updated=20),
    ))
    response = await _get(upstream)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["directory"] == "/a"
    assert row["rootSessionCount"] == 2


# ---------------------------------------------------------------------------
# 4. Archived sessions → active/archived counts; all archived → archivedOnly
# ---------------------------------------------------------------------------


async def test_archived_counts_and_archived_only(upstream_factory):
    """/a: 1 active + 2 archived (archivedOnly false). /b: 2 archived only
    (archivedOnly true, activeRootSessionCount 0)."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/a", sid="a2", updated=20, archived=100),
        _session("/a", sid="a3", updated=30, archived=200),
        _session("/b", sid="b1", updated=40, archived=300),
        _session("/b", sid="b2", updated=50, archived=400),
    ))
    response = await _get(upstream)
    assert response.status_code == 200
    by_dir = {it["directory"]: it for it in response.json()["items"]}
    assert by_dir["/a"]["activeRootSessionCount"] == 1
    assert by_dir["/a"]["archivedRootSessionCount"] == 2
    assert by_dir["/a"]["archivedOnly"] is False
    assert by_dir["/b"]["activeRootSessionCount"] == 0
    assert by_dir["/b"]["archivedRootSessionCount"] == 2
    assert by_dir["/b"]["archivedOnly"] is True


# ---------------------------------------------------------------------------
# 5. Winner tie-break: same time.updated → time.created; same created → id
# ---------------------------------------------------------------------------


async def test_winner_tiebreak_updated_then_created_then_id(upstream_factory):
    """Two sessions same time.updated (100):
      - s1: created=5, id='aaa', title='from-aaa'
      - s2: created=9, id='zzz', title='from-zzz'
    winner = s2 (created larger). title must come from s2."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="aaa", title="from-aaa", updated=100, created=5),
        _session("/a", sid="zzz", title="from-zzz", updated=100, created=9),
    ))
    response = await _get(upstream)
    row = response.json()["items"][0]
    assert row["lastUpdated"] == 100
    assert row["title"] == "from-zzz", "winner = larger time.created → s2"

    # Same updated AND same created → winner = id字典序 max.
    upstream2 = upstream_factory(_discovery_handler(
        _session("/a", sid="aaa", title="from-aaa", updated=100, created=9),
        _session("/a", sid="zzz", title="from-zzz", updated=100, created=9),
    ))
    response2 = await _get(upstream2)
    row2 = response2.json()["items"][0]
    assert row2["title"] == "from-zzz", "winner = id字典序 max → zzz"


# ---------------------------------------------------------------------------
# 6. title missing / null / empty string → title null
# ---------------------------------------------------------------------------


async def test_title_missing_null_empty_becomes_null(upstream_factory):
    """winner with missing title, null title, or empty-string title → null."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", title=None, updated=10),      # title omitted
        _session("/b", sid="b1", title=None, updated=10),      # explicit null
        _session("/c", sid="c1", title="", updated=10),        # empty string
    ))
    response = await _get(upstream)
    by_dir = {it["directory"]: it for it in response.json()["items"]}
    assert by_dir["/a"]["title"] is None
    assert by_dir["/b"]["title"] is None
    assert by_dir["/c"]["title"] is None


# ---------------------------------------------------------------------------
# 7. Sort: lastUpdated DESC, tie-break directory ASC
# ---------------------------------------------------------------------------


async def test_sort_lastupdated_desc_directory_asc(upstream_factory):
    """/z lastUpdated=5, /a lastUpdated=5, /m lastUpdated=99.
    Expected order: /m (99), then /a (5), then /z (5) — directory ASC tie."""
    upstream = upstream_factory(_discovery_handler(
        _session("/z", sid="z1", updated=5),
        _session("/a", sid="a1", updated=5),
        _session("/m", sid="m1", updated=99),
    ))
    response = await _get(upstream)
    order = [it["directory"] for it in response.json()["items"]]
    assert order == ["/m", "/a", "/z"]


# ---------------------------------------------------------------------------
# 8. Discovery 4xx → 503 upstream_unavailable (status NOT leaked)
# ---------------------------------------------------------------------------


async def test_discovery_4xx_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json() == {"code": "upstream_unavailable"}


# ---------------------------------------------------------------------------
# 9. Discovery 5xx / network error / bad JSON / non-list body → 503
# ---------------------------------------------------------------------------


async def test_discovery_5xx_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_discovery_network_error_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_discovery_bad_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(200, content=b"not-json{")
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_discovery_non_list_body_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200, content=b'{"unexpected":"shape"}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 10. Malformed session (non-dict / empty directory / null directory) → 503
# ---------------------------------------------------------------------------


async def test_malformed_session_non_dict_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200,
                content=orjson.dumps([
                    {"id": "ok", "directory": "/good", "time": {"updated": 1}},
                    "not-a-dict",  # malformed element
                ]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_malformed_session_empty_directory_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200,
                content=orjson.dumps([
                    {"id": "bad", "directory": "", "time": {}},  # empty dir
                ]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_malformed_session_null_directory_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            return httpx.Response(
                200,
                content=orjson.dumps([
                    {"id": "bad", "directory": None, "time": {}},  # null dir
                ]),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    upstream = upstream_factory(handler)
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 11. read_with_cap exceeds cap (returns None) → 503 upstream_unavailable
# ---------------------------------------------------------------------------


async def test_discovery_cap_exceeded_returns_503(upstream_factory, monkeypatch):
    """Mock read_with_cap to return None (cap exceeded) → 503."""
    async def _fake_read_with_cap(response, max_bytes, *, on_read=None):
        return None, 0

    monkeypatch.setattr(discovery, "read_with_cap", _fake_read_with_cap)

    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=1),
    ))
    response = await _get(upstream)
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 12. Transform busy → 503 transform_busy + Retry-After:2 (no upstream GET)
# ---------------------------------------------------------------------------


async def test_transform_busy_returns_503_with_retry_after(upstream_factory):
    """Pre-acquire the single admission slot (max_transforms=1), then call the
    route — it must emit 503 transform_busy with Retry-After and must NOT hit
    upstream (admission is acquired BEFORE the GET)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    pool = app.state.transforms
    transport = httpx.ASGITransport(app=app)
    try:
        async with pool:  # saturate the single admission slot
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/slimapi/directories", headers=VERSION_HEADERS)
            assert response.status_code == 503
            body = response.json()
            assert body["code"] == "transform_busy"
            assert response.headers["Retry-After"] == "2"
        # admission-before-GET → zero upstream calls.
        assert calls["n"] == 0
    finally:
        app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# 13. gzip negotiation (Accept-Encoding: gzip → Content-Encoding + Vary)
# ---------------------------------------------------------------------------


async def test_gzip_negotiation_honored(upstream_factory):
    """Client sends Accept-Encoding: gzip → Content-Encoding: gzip + Vary."""
    # Enough sessions to exceed MIN_GZIP_BYTES (64) so compression is beneficial.
    sessions = [
        _session(f"/dir{i:04d}", sid=f"ses_{i:04d}", updated=i, title=f"title-{i}")
        for i in range(20)
    ]
    upstream = upstream_factory(_discovery_handler(*sessions))
    response = await _get(upstream, **{"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in response.headers.get("vary", "").lower()
    body = response.json()
    assert len(body["items"]) == 20


# ---------------------------------------------------------------------------
# 14. Traffic bucket: /slimapi/directories → "directories" bucket
# ---------------------------------------------------------------------------


def test_bucketize_directories():
    assert bucketize("GET", "/slimapi/directories") == "directories"
    assert bucketize("GET", "/slimapi/directories/") == "directories"


def test_bucketize_other_slimapi_unchanged():
    # Sanity: questions still buckets to questions, sessions to sessions.
    assert bucketize("GET", "/slimapi/questions") == "questions"
    assert bucketize("GET", "/slimapi/sessions") == "sessions"


# ---------------------------------------------------------------------------
# 15. discoveryComplete: count == limit → false; count < limit → true
# ---------------------------------------------------------------------------


async def test_discovery_complete_true_when_under_limit(upstream_factory):
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=1),
        _session("/b", sid="b1", updated=2),
    ))
    response = await _get(upstream)
    assert response.json()["discoveryComplete"] is True


async def test_discovery_complete_false_when_page_full(upstream_factory, monkeypatch):
    """Monkeypatch the limit binding on the directories module to a tiny value;
    exactly that many sessions → discoveryComplete false (possible truncation)."""
    monkeypatch.setattr(directories, "_DISCOVERY_LIMIT", 2)
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=1),
        _session("/b", sid="b1", updated=2),
    ))
    response = await _get(upstream)
    assert response.status_code == 200
    assert response.json()["discoveryComplete"] is False


# ---------------------------------------------------------------------------
# Bonus: version gate + additive (no directory param accepted)
# ---------------------------------------------------------------------------


async def test_retired_version_header_ignored_at_route_level(upstream_factory):
    """§1 terminal: X-Slimapi-Version is dead input; discovery answers."""
    upstream = upstream_factory(_discovery_handler())
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/directories",
                                    headers={"X-Slimapi-Version": "9"})
    assert response.status_code == 200


async def test_directory_query_param_ignored_no_filter(upstream_factory):
    """This endpoint accepts NO directory param (global discovery). A client
    that erroneously sends ?directory= is still served the GLOBAL catalog
    (the param is simply not declared, so FastAPI ignores unknown query)."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=1),
        _session("/b", sid="b1", updated=2),
    ))
    app = _build_app(upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/slimapi/directories?directory=/a", headers=VERSION_HEADERS,
        )
    assert response.status_code == 200
    by_dir = {it["directory"]: it for it in response.json()["items"]}
    # Both dirs returned — the bogus ?directory= did not filter.
    assert "/a" in by_dir
    assert "/b" in by_dir


# ---------------------------------------------------------------------------
# 16. Allowlist overlay (owner ruling D1-A): three states + canonical match
# ---------------------------------------------------------------------------


async def test_allowlist_nonempty_filters_rows_and_counts(upstream_factory):
    """Non-empty allowlist ["/a"]: /a (2 sessions) and /a/sub (1) survive;
    boundary trap /ab (lexical prefix, NOT a subtree) and unrelated /c
    vanish entirely. Row counts/titles reflect ONLY allowlisted sessions."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10, title="winner-a"),
        _session("/a", sid="a2", updated=5),
        _session("/a/sub", sid="s1", updated=30, title="winner-sub"),
        _session("/ab", sid="ab1", updated=99, title="must-not-appear"),
        _session("/c", sid="c1", updated=40),
    ))
    response = await _get(upstream, settings=_settings(directory_allowlist=["/a"]))
    assert response.status_code == 200
    body = response.json()
    by_dir = {it["directory"]: it for it in body["items"]}
    assert set(by_dir) == {"/a", "/a/sub"}
    assert by_dir["/a"]["rootSessionCount"] == 2
    assert by_dir["/a"]["title"] == "winner-a"
    assert by_dir["/a/sub"]["rootSessionCount"] == 1
    assert by_dir["/a/sub"]["title"] == "winner-sub"
    # discoveryComplete is NOT recomputed by the filter (upstream page was
    # not full → still true even though rows were filtered away).
    assert body["discoveryComplete"] is True


async def test_allowlist_unset_no_filtering(upstream_factory):
    """None (unset) → "no allowlist axis" → zero behavior change (golden)."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/b", sid="b1", updated=20),
    ))
    response = await _get(upstream, settings=_settings(directory_allowlist=None))
    assert response.status_code == 200
    assert {it["directory"] for it in response.json()["items"]} == {"/a", "/b"}


async def test_allowlist_explicit_empty_no_filtering(upstream_factory):
    """[] (explicit empty) mirrors the sessions-list family: "no allowlist
    axis" → no filtering (NOT the /slimapi/file** reject-all semantics).
    Also pins the config-noise rule: [""] drops to no axis as well."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/b", sid="b1", updated=20),
    ))
    response = await _get(upstream, settings=_settings(directory_allowlist=[]))
    assert response.status_code == 200
    assert {it["directory"] for it in response.json()["items"]} == {"/a", "/b"}

    response_noise = await _get(upstream, settings=_settings(directory_allowlist=[""]))
    assert response_noise.status_code == 200
    assert {it["directory"] for it in response_noise.json()["items"]} == {"/a", "/b"}


async def test_allowlist_root_slash_matches_all_absolute(upstream_factory):
    """/ root special case: matches every non-empty ABSOLUTE path; a
    relative directory fails closed (rev-2 sub-2) and leaves no row."""
    upstream = upstream_factory(_discovery_handler(
        _session("/a", sid="a1", updated=10),
        _session("/deep/nested/dir", sid="n1", updated=20),
        _session("relative/dir", sid="r1", updated=30),
    ))
    response = await _get(upstream, settings=_settings(directory_allowlist=["/"]))
    assert response.status_code == 200
    dirs = {it["directory"] for it in response.json()["items"]}
    assert dirs == {"/a", "/deep/nested/dir"}
