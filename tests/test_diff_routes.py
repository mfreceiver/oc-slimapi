"""Route-level tests for ``routes/diff.py`` — T18 (2026-08-16).

``GET /slimapi/sessions/{sid}/diff`` — read-only diff thin route, a
verbatim sibling of the gated T17 todo/children routes (Batch 3 / C2a
pattern). The upstream ``Snapshot.FileDiff`` struct is already minimal
(``file`` / ``patch`` / ``additions`` / ``deletions`` / ``status`` —
schema v1.18.16 ``packages/schema/src/file-diff.ts:6-13``), so the
projection is near-identity: the route's value is gzip + cap +
admission + structured errors (todo design doc §2's honest conclusion,
same reasoning).

The one structural difference from todo: the optional ``messageID``
query parameter, forwarded upstream verbatim (upstream
``DiffQuery = WorkspaceRoutingQueryFields + {messageID?: MessageID}``,
groups/session.ts:39-42; handler passes it to ``summary.diff``,
handlers/session.ts:99-103).

Mirrors test_todo_routes.py (AC: the T18 checklist — happy/gzip/errors/
directory/messageID two-state/scalar guard).
"""
from __future__ import annotations

import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import diff, health
from oc_slimapi.transform import TransformConfig, TransformPool

VERSION_HEADERS = {"X-Slimapi-Version": "2"}

DIFF_BODY = orjson.dumps([
    {
        "file": "src/app.py",
        "patch": "@@ -1,3 +1,4 @@\n+import new\n context line",
        "additions": 1,
        "deletions": 0,
        "status": "modified",
    },
    {
        "file": "README.md",
        "patch": None,
        "additions": 12,
        "deletions": 3,
        "status": "deleted",
    },
])


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    settings: Settings, upstream: httpx.AsyncClient,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-diff-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(health.router)
    app.include_router(diff.router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


# ---------------------------------------------------------------------------
# T18 — happy path + gzip negotiation (three states)
# ---------------------------------------------------------------------------

async def test_diff_happy_path_identity_projection(upstream_factory):
    """200 with the near-identity projected body (FileDiff.Info is already
    minimal — no field whitelist to apply; the big ``patch`` strings are
    UI-consumed and kept: gzip is the saving lever, same as todo §2)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.path == "/session/s1/diff"
        return httpx.Response(200, content=DIFF_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert r.json() == orjson.loads(DIFF_BODY)
        # catalog-chain Vary (Batch 2 onward: directory merged on 200s)
        assert r.headers["Vary"] == (
            "Accept-Encoding")
        assert seen == ["/session/s1/diff"]


async def test_diff_gzip_three_states(upstream_factory):
    """gzip negotiation mirrors the catalog pack worker (``accepts_gzip``):
    gzip header honoured, no-AE identity, explicit ``gzip;q=0`` refused."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=DIFF_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r_gz = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r_gz.status_code == 200
        assert r_gz.headers.get("Content-Encoding") == "gzip"
        assert r_gz.json() == orjson.loads(DIFF_BODY)

        r_id = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**VERSION_HEADERS, "Accept-Encoding": "identity"})
        assert r_id.status_code == 200
        assert "content-encoding" not in r_id.headers
        assert r_id.content == DIFF_BODY

        r_refuse = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip;q=0"})
        assert r_refuse.status_code == 200
        assert "content-encoding" not in r_refuse.headers
        assert r_refuse.content == DIFF_BODY


async def test_diff_empty_array(upstream_factory):
    """`[]` passes through fine (session with no file changes)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert r.json() == []


async def test_diff_empty_array_skips_gzip(upstream_factory):
    """Tiny-body benefit gate (rev-6 C2 / T18 mirrors todo): empty ``[]`` +
    ``Accept-Encoding: gzip`` → identity (no Content-Encoding)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.content == b"[]"


# ---------------------------------------------------------------------------
# T18 — messageID optional passthrough (two states)
# ---------------------------------------------------------------------------

async def test_diff_message_id_forwarded_when_present(upstream_factory):
    """``messageID`` present → forwarded upstream verbatim as the same-named
    query parameter (DiffQuery.messageID)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = str(request.url.params.get("messageID"))
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            params={"messageID": "msg_01HABC"},
            headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["path"] == "/session/s1/diff"
        assert seen["query"] == "msg_01HABC"


async def test_diff_message_id_absent_not_sent(upstream_factory):
    """``messageID`` absent → NOT sent upstream at all (None is omitted,
    not forwarded empty — upstream answers 200 ``[]`` per summary.ts
    ``if (!input.messageID) return []``; the empty body passes through
    verbatim)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.params.get("messageID"))
        seen["raw_query"] = request.url.query
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["query"] == "None"
        assert "messageID" not in (seen["raw_query"] or "")


async def test_diff_message_id_and_directory_combined(upstream_factory):
    """Both optional query params coexist: ``directory`` routes the
    upstream instance (header), ``messageID`` selects the diff base
    (upstream query)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["dir"] = request.headers.get("x-opencode-directory") or ""
        seen["query"] = str(request.url.params.get("messageID"))
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            params={"messageID": "msg_02", "directory": "/work/project"},
            headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["dir"] == "/work/project"
        assert seen["query"] == "msg_02"


# ---------------------------------------------------------------------------
# 4.11.0 Phase A / A2 — ETag now ENABLED on this route
# (enable_etag=True; full matrix in tests/test_thin_etag.py)
# ---------------------------------------------------------------------------

async def test_diff_etag_enabled_inm_304(upstream_factory):
    """A2: ① happy responses carry a strong ``ETag`` (identity coding,
    pinned) + merged ``Vary`` + ``Cache-Control: no-store``; ② ``*`` /
    matching validator → 304 (no body); ③ a stale opaque tag → 200 with
    the full body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=DIFF_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        hdr = {**VERSION_HEADERS, "Accept-Encoding": "identity"}
        r = await client.get("/slimapi/sessions/s1/diff", headers=hdr)
        assert r.status_code == 200
        etag = r.headers["ETag"]
        assert etag.startswith('"')
        assert r.headers["Vary"] == "Accept-Encoding"
        assert r.headers["Cache-Control"] == "no-store"

        r_replay = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**hdr, "If-None-Match": etag})
        assert r_replay.status_code == 304
        assert r_replay.content == b""
        assert r_replay.headers["ETag"] == etag
        assert r_replay.headers["Cache-Control"] == "no-store"

        r_star = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**hdr, "If-None-Match": "*"})
        assert r_star.status_code == 304

        r_tag = await client.get(
            "/slimapi/sessions/s1/diff",
            headers={**hdr, "If-None-Match": '"deadbeef"'})
        assert r_tag.status_code == 200
        assert r_tag.json() == orjson.loads(DIFF_BODY)


# ---------------------------------------------------------------------------
# per-item shape guard (scalar elements → 503, never a bare 500)
# ---------------------------------------------------------------------------

async def test_diff_scalar_items_return_503(upstream_factory):
    """``[1, null]`` is a malformed ``FileDiff.Info[]`` envelope → 503
    ``upstream_unavailable`` (children precedent), NOT an unstructured 500
    from an AttributeError inside the projection."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, null]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_diff_mixed_scalar_item_returns_503(upstream_factory):
    """A single scalar mixed into an otherwise-valid list is still a
    malformed envelope → 503 (per-item check, not just outer list)."""
    body = orjson.dumps([
        {"file": "ok.py", "additions": 1, "deletions": 0},
        5,
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------

async def test_diff_upstream_404_maps_session_not_found(upstream_factory):
    """sid-scoped 404 → 404 ``session_not_found`` (messages-route mapping)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error":"nf"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 404
        assert r.json()["code"] == "session_not_found"
        assert r.json()["sessionID"] == "s1"


async def test_diff_upstream_4xx_returns_502(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 502
        assert r.json()["code"] == "upstream_http_400"


async def test_diff_upstream_5xx_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_diff_network_error_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_diff_bad_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_diff_non_list_json_returns_503(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"file":"not a list"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 503
        assert r.json()["code"] == "upstream_unavailable"


async def test_diff_cap_overflow_returns_413(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=8), upstream)
    async with _client(app) as client:
        r = await client.get("/slimapi/sessions/s1/diff",
                             headers=VERSION_HEADERS)
        assert r.status_code == 413
        body = r.json()
        assert body["code"] == "response_too_large"
        assert body["limit"] == 8


async def test_diff_transform_busy_returns_503_retry_after(upstream_factory,
                                                           monkeypatch):
    """Pool saturated → 503 ``transform_busy`` + ``Retry-After: 2``."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=DIFF_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(max_transforms=1, transform_wait_seconds=0.05), upstream)

    from oc_slimapi.routes import _catalog_common

    async def slow_read(*args, **kwargs):
        await asyncio.sleep(0.15)
        return b"[]"

    monkeypatch.setattr(_catalog_common, "read_upstream_response", slow_read)

    async with _client(app) as client:
        results = await asyncio.gather(*[
            client.get("/slimapi/sessions/s1/diff",
                       headers=VERSION_HEADERS)
            for _ in range(4)
        ])
    codes = sorted(r.status_code for r in results)
    # max_transforms=1 with a slow read: some get 200, the rest 503 busy.
    assert codes[0] == 200
    busy = [r for r in results if r.status_code == 503]
    assert busy and all(r.json()["code"] == "transform_busy" for r in busy)
    assert all(r.headers.get("Retry-After") == "2" for r in busy)


# ---------------------------------------------------------------------------
# directory query validation + forwarding
# ---------------------------------------------------------------------------

async def test_diff_directory_forwarded_as_header(upstream_factory):
    """Optional ``directory`` query → ``X-Opencode-Directory`` upstream
    header (per-session routing, like the messages route)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["dir"] = request.headers.get("x-opencode-directory")
        return httpx.Response(200, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            params={"directory": "/work/project"},
            headers=VERSION_HEADERS)
        assert r.status_code == 200
        assert seen["dir"] == "/work/project"


async def test_diff_directory_rejects_traversal(upstream_factory):
    """``validate_directory`` structural checks: ``..`` → 400
    ``invalid_directory``."""
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            params={"directory": "../etc"},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"


async def test_diff_directory_rejects_overlong(upstream_factory):
    upstream = upstream_factory(
        lambda request: httpx.Response(200, content=b"[]"))
    app = _build_app(_settings(), upstream)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/sessions/s1/diff",
            params={"directory": "/" + "a" * 4096},
            headers=VERSION_HEADERS)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_directory"
