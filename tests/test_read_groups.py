"""v3-contract §10.a read-group annexation tests (Batch C1, TDD).

Controlled-proxy pipeline for the 7 read groups:

* file — GET /slimapi/file, /slimapi/file/content, /slimapi/file/status
* vcs — GET /slimapi/vcs, /slimapi/vcs/status, /slimapi/vcs/diff
* find — GET /slimapi/find/file
* providers — GET /slimapi/config/providers
* session single — GET /slimapi/session/{sid} (skeleton_session projection)
* active — GET /slimapi/api/session/active (directory tolerant)
* globalHealth — GET /slimapi/global/health (directory tolerant)

Per group: happy 200 verbatim + directory two-state + 4xx verbatim
passthrough + 5xx→503 upstream_unavailable + Vary assertion. Plus ETag
(§10.a all-GET enablement), gzip negotiation, cap→413, v2/v3 validator
isolation, and selector consuming-set integration (v3 strip / dual-present
conflict / multi-value) for the new routes.
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import read_groups
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
        server_api_version=2, accepted_client_versions=(2, 3),
    )
    base.update(overrides)
    return Settings(**base)


def _read_payloads() -> dict[str, bytes]:
    return {
        "/file": orjson.dumps([
            {"name": "readme.md", "path": "readme.md",
             "absolute": "/w/readme.md", "type": "file", "ignored": False},
        ]),
        "/file/content": orjson.dumps(
            {"type": "text", "content": "hello"}),
        "/file/status": orjson.dumps([
            {"path": "a.py", "added": 1, "removed": 2, "status": "modified"},
        ]),
        "/vcs": orjson.dumps(
            {"sourceControl": {"agent": False, "workflow": False}}),
        "/vcs/status": orjson.dumps([{"path": "a.py", "status": "modified"}]),
        "/vcs/diff": orjson.dumps([
            {"path": "a.py", "patch": "@@", "additions": 1, "deletions": 2},
        ]),
        "/find/file": orjson.dumps(["a.txt", "b/c.txt"]),
        "/config/providers": orjson.dumps(
            {"current": [{"id": "anthropic"}], "default": "anthropic",
             "custom": []}),
        "/session/s1": orjson.dumps({
            "id": "s1", "title": "one", "parentID": None,
            "directory": "/w", "projectID": "p1", "agent": "build",
            "model": {"modelID": "m"},
            "time": {"created": 1, "updated": 2},
            "repoPath": "/w", "commit": "c", "branch": "main",
            "status": "ACTIVE", "version": "v1",
            "cost": {"total": 9}, "tokens": {"input": 9, "output": 9},
            "location": {"repo": "/w", "subpath": "/"}, "subpath": "/",
        }),
        "/api/session/active": orjson.dumps(
            {"data": {"ses_1": {"type": "running"}}}),
        "/global/health": orjson.dumps(
            {"healthy": True, "version": "1.18.16"}),
    }


def _build_app(handler, *, settings: Settings | None = None):
    seen: list[httpx.Request] = []
    payloads = _read_payloads()

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(handler, dict):
            status = handler.get(request.url.path, 200)
            body = handler.get("body", b'{"error": {"code": "X"}}')
            return httpx.Response(status, content=body,
                                  headers={"Content-Type": "application/json"})
        return handler(request, payloads)

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream)
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes))
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(
        SlimapiSelectorMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
        v3_enabled=True)
    return app, seen


def _default_handler(request: httpx.Request, payloads: dict[str, bytes]):
    body = payloads.get(request.url.path)
    if body is None:
        return httpx.Response(404, content=b'{"error": "nf"}',
                              headers={"Content-Type": "application/json"})
    return httpx.Response(200, content=body,
                          headers={"Content-Type": "application/json"})


@pytest.fixture
async def stack():
    app, seen = _build_app(_default_handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        yield client, seen


# ---------------------------------------------------------------------------
# file group
# ---------------------------------------------------------------------------

async def test_file_list_v3_happy_passthrough(stack):
    client, seen = stack
    resp = await client.get("/slimapi/file?v=3&path=readme.md",
                            headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/file"]
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["cache-control"] == "no-store"
    assert "etag" in resp.headers
    assert "x-opencode-directory" in resp.headers["vary"].lower()
    upstream = seen[0]
    assert upstream.url.path == "/file"
    assert upstream.url.params["path"] == "readme.md"


async def test_file_v2_query_directory_forwards_verbatim_not_converted(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?path=readme.md&directory=/w", headers=V2_HEADERS)
    assert resp.status_code == 200
    upstream = seen[0]
    # §5.2 (v2): directory is NOT consumed — it forwards in the raw query,
    # byte-identical; the sidecar does not convert it into a header.
    assert upstream.url.query.decode("latin-1") == "path=readme.md&directory=/w"
    assert upstream.headers.get(DIRECTORY_HEADER) is None


async def test_file_v2_header_directory_channel_forwarded(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?path=readme.md",
        headers={**V2_HEADERS, DIRECTORY_HEADER: "/w"})
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"
    assert seen[0].url.query.decode("latin-1") == "path=readme.md"


async def test_file_v2_invalid_directory_header_400(stack):
    client, _ = stack
    resp = await client.get(
        "/slimapi/file?path=r",
        headers={**V2_HEADERS, DIRECTORY_HEADER: "../escape"})
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "invalid_directory"


async def test_file_v2_query_and_header_both_forwarded(stack):
    client, seen = stack
    # v2 read groups follow catch-all semantics: both channels forward,
    # no dual-present conflict check (§5.4 is a v3 consuming-set rule).
    resp = await client.get(
        "/slimapi/file?path=r&directory=/q",
        headers={**V2_HEADERS, DIRECTORY_HEADER: "/h"})
    assert resp.status_code == 200
    assert seen[0].url.query.decode("latin-1") == "path=r&directory=/q"
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/h"


async def test_v2_unknown_duplicate_encoded_query_verbatim(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?path=r&a=1&a=2&b=%2F&c=a+b", headers=V2_HEADERS)
    assert resp.status_code == 200
    # Byte fidelity (§5.2 / proxy.py:182-203 semantics): unknown params,
    # repeats, percent-encodings and '+' survive verbatim.
    assert seen[0].url.query.decode("latin-1") == "path=r&a=1&a=2&b=%2F&c=a+b"


async def test_v3_unknown_duplicate_encoded_query_verbatim(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?v=3&path=r&a=1&a=2&b=%2F&c=a+b", headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].url.query.decode("latin-1") == "path=r&a=1&a=2&b=%2F&c=a+b"


async def test_v2_explicit_selector_strips_v_rest_verbatim(stack):
    client, seen = stack
    resp = await client.get("/slimapi/vcs?v=2&a=1", headers=V2_HEADERS)
    assert resp.status_code == 200
    assert seen[0].url.query.decode("latin-1") == "a=1"


async def test_file_v3_directory_query_consumed_and_stripped(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?v=3&path=readme.md&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    upstream = seen[0]
    assert upstream.headers.get(DIRECTORY_HEADER) == "/w"
    assert upstream.url.query.decode("latin-1") == "path=readme.md"  # v AND directory gone


async def test_file_content_happy(stack):
    client, seen = stack
    resp = await client.get("/slimapi/file/content?v=3&path=readme.md",
                            headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/file/content"]
    assert upstream_path(seen) == "/file/content"


def upstream_path(seen):
    return seen[-1].url.path


async def test_file_status_directory_two_states(stack):
    client, seen = stack
    resp = await client.get("/slimapi/file/status?v=3", headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) is None
    resp = await client.get("/slimapi/file/status?v=3&directory=/w",
                            headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[1].headers.get(DIRECTORY_HEADER) == "/w"
    assert seen[1].url.query.decode("latin-1") == ""  # v + directory stripped, nothing left


# ---------------------------------------------------------------------------
# vcs group
# ---------------------------------------------------------------------------

async def test_vcs_happy(stack):
    client, seen = stack
    resp = await client.get("/slimapi/vcs?v=3&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/vcs"]
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


async def test_vcs_status_happy(stack):
    client, seen = stack
    resp = await client.get("/slimapi/vcs/status?v=3", headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/vcs/status"]


async def test_vcs_diff_forwards_mode_context(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/vcs/diff?v=3&mode=working&context=5", headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/vcs/diff"]
    params = seen[0].url.params
    assert params.get("mode") == "working"
    assert params.get("context") == "5"
    assert "directory" not in params


# ---------------------------------------------------------------------------
# find / providers
# ---------------------------------------------------------------------------

async def test_find_file_forwards_query_params(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/find/file?v=3&query=readme&dirs=true&type=file&limit=10",
        headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/find/file"]
    params = seen[0].url.params
    assert params["query"] == "readme"
    assert params["dirs"] == "true"
    assert params["type"] == "file"
    assert params["limit"] == "10"


async def test_providers_directory_sensitive(stack):
    client, seen = stack
    resp = await client.get("/slimapi/config/providers?v=3",
                            headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/config/providers"]
    assert "x-opencode-directory" in resp.headers["vary"].lower()
    resp = await client.get("/slimapi/config/providers?v=3&directory=/w",
                            headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[1].headers.get(DIRECTORY_HEADER) == "/w"


# ---------------------------------------------------------------------------
# session single
# ---------------------------------------------------------------------------

async def test_session_single_skeleton_projection(stack):
    client, _seen = stack
    resp = await client.get("/slimapi/session/s1?v=3", headers=IDENTITY)
    assert resp.status_code == 200
    projected = orjson.loads(resp.content)
    assert projected["id"] == "s1"
    assert projected["title"] == "one"
    assert projected["time"]["created"] == 1
    assert projected["model"]["modelID"] == "m"
    # Whitelist drops heavy/never-consumed fields (skeleton_session).
    for dropped in ("cost", "tokens", "location", "subpath", "repoPath",
                    "commit", "branch", "status", "version"):
        assert dropped not in projected


async def test_session_single_directory(stack):
    client, seen = stack
    await client.get("/slimapi/session/s1?v=3&directory=/w", headers=IDENTITY)
    assert seen[0].url.path == "/session/s1"
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


# ---------------------------------------------------------------------------
# active / globalHealth (directory tolerant-ignore)
# ---------------------------------------------------------------------------

async def test_active_session_happy_and_directory_tolerant(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/api/session/active?v=3&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/api/session/active"]
    assert seen[0].headers.get(DIRECTORY_HEADER) is None
    # §5.5 tolerant-ignore: no consumption, no strip — the directory query
    # forwards verbatim with the rest of the raw query; the upstream (no
    # query schema on /api/session/active) ignores it.
    assert seen[0].url.query.decode("latin-1") == "directory=/w"
    # Vary single-value: not directory-sensitive.
    assert "x-opencode-directory" not in resp.headers["vary"].lower()


async def test_global_health_happy_and_directory_tolerant(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/global/health?v=3&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.content == _read_payloads()["/global/health"]
    assert seen[0].headers.get(DIRECTORY_HEADER) is None
    assert seen[0].url.query.decode("latin-1") == "directory=/w"


async def test_active_and_global_health_304_revalidation(stack):
    # §10.a ETag enablement covers the tolerant GETs too (§6.3 "全集").
    client, _ = stack
    for path in ("/slimapi/api/session/active", "/slimapi/global/health"):
        first = await client.get(f"{path}?v=3", headers=IDENTITY)
        assert "etag" in first.headers, path
        second = await client.get(
            f"{path}?v=3",
            headers={**IDENTITY, "If-None-Match": first.headers["etag"]})
        assert second.status_code == 304, path
        assert second.content == b""
        assert second.headers["etag"] == first.headers["etag"]
        assert second.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# error mapping (two-tier): 4xx verbatim / 5xx→503
# ---------------------------------------------------------------------------

async def test_upstream_4xx_verbatim_passthrough():
    app, seen = _build_app(
        {"/file": 400, "body": b'{"error": {"code": "bad_path"}}'})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file?v=3&path=x", headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.content == b'{"error": {"code": "bad_path"}}'
        assert resp.headers["content-type"].startswith("application/json")


async def test_upstream_404_verbatim_on_session_single():
    app, _ = _build_app({"/session/s9": 404,
                         "body": b'{"error": "not_found"}'})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/session/s9?v=3", headers=IDENTITY)
        assert resp.status_code == 404
        assert resp.content == b'{"error": "not_found"}'


async def test_upstream_5xx_maps_to_503_upstream_unavailable():
    for path in ("/file", "/file/content", "/file/status", "/vcs",
                 "/vcs/status", "/vcs/diff",
                 "/find/file", "/config/providers", "/session/s1",
                 "/api/session/active", "/global/health"):
        app, _ = _build_app({path: 503, "body": b"oops"})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as client:
            resp = await client.get(f"/slimapi{path}?v=3&path=x&query=q",
                                    headers=IDENTITY)
            assert resp.status_code == 503, path
            assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


async def test_network_error_maps_to_503():
    def failing(request, payloads):
        raise httpx.ConnectError("nope")

    app, _ = _build_app(failing)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 503
        assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# ETag (§10.a all-GET enablement) + gzip + cap
# ---------------------------------------------------------------------------

async def test_etag_present_and_304_revalidation(stack):
    client, _ = stack
    first = await client.get("/slimapi/file?v=3&path=r", headers=IDENTITY)
    assert "etag" in first.headers
    etag = first.headers["etag"]
    second = await client.get(
        "/slimapi/file?v=3&path=r",
        headers={**IDENTITY, "If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "no-store"
    assert "vary" in second.headers
    # §6.4 v3 304 header set: no routing aux headers on read groups anyway.
    assert "x-next-cursor" not in second.headers
    assert "x-complete" not in second.headers


async def test_etag_v2_v3_validator_isolation(stack):
    client, _ = stack
    v2 = await client.get("/slimapi/file?path=r", headers=V2_HEADERS)
    assert "etag" in v2.headers
    v3 = await client.get(
        "/slimapi/file?v=3&path=r",
        headers={**IDENTITY, "If-None-Match": v2.headers["etag"]})
    assert v3.status_code == 200  # cross-domain validator: no 304
    v3b = await client.get("/slimapi/file?v=3&path=r", headers=IDENTITY)
    back = await client.get(
        "/slimapi/file?path=r",
        headers={**V2_HEADERS, "If-None-Match": v3b.headers["etag"]})
    assert back.status_code == 200


async def test_etag_disabled_config_yields_no_etag():
    app, _ = _build_app(_default_handler, settings=_settings(etag_enabled=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file?v=3&path=r", headers=IDENTITY)
        assert resp.status_code == 200
        assert "etag" not in resp.headers


async def test_gzip_negotiation_weak_etag():
    big = orjson.dumps({"sourceControl": {"notes": "n" * 4096}})

    def big_handler(request, payloads):
        return httpx.Response(200, content=big,
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(big_handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3",
                                headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert resp.headers["etag"].startswith('W/')
        # httpx transparently decodes gzip — a successful decode proves
        # the wire body was valid gzip.
        assert resp.content == big
        assert len(big) > 64  # above MIN_GZIP_BYTES gate


async def test_response_cap_maps_to_413():
    big = b"x" * 4096

    def big_handler(request, payloads):
        return httpx.Response(200, content=big,
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(big_handler,
                        settings=_settings(max_response_bytes=1024))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file?v=3&path=r", headers=IDENTITY)
        assert resp.status_code == 413
        assert orjson.loads(resp.content)["code"] == "response_too_large"


async def test_content_type_passthrough_non_json():
    def text_handler(request, payloads):
        return httpx.Response(200, content=b"plain text body",
                              headers={"Content-Type": "text/plain"})

    app, _ = _build_app(text_handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file/content?v=3&path=r",
                                headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.content == b"plain text body"
        assert resp.headers["content-type"] == "text/plain"


# ---------------------------------------------------------------------------
# selector consuming-set integration for the new routes
# ---------------------------------------------------------------------------

async def test_v3_dual_present_conflict_400_directory_conflict(stack):
    client, _ = stack
    resp = await client.get(
        "/slimapi/file?v=3&path=r&directory=/a",
        headers={**IDENTITY, DIRECTORY_HEADER: "/b"})
    assert resp.status_code == 400
    body = orjson.loads(resp.content)
    assert body["code"] == "directory_conflict"
    assert body["queryDirectory"] == "/a"
    assert body["headerDirectory"] == "/b"


async def test_v3_dual_present_same_value_ok(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/file?v=3&path=r&directory=/w",
        headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


async def test_v3_multi_value_directory_conflict(stack):
    client, _ = stack
    resp = await client.get(
        "/slimapi/file?v=3&path=r&directory=/a&directory=/b",
        headers=IDENTITY)
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "invalid_directory_selector"


async def test_v3_multi_value_same_directory_folds(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/vcs?v=3&directory=/w&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


async def test_v3_header_only_directory_consumed(stack):
    client, seen = stack
    resp = await client.get("/slimapi/vcs?v=3",
                            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


async def test_v3_invalid_directory_selector_on_find(stack):
    client, _ = stack
    resp = await client.get(
        "/slimapi/find/file?v=3&query=q&directory=/a&directory=/b",
        headers=IDENTITY)
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "invalid_directory_selector"


async def test_v3_directory_not_stripped_on_tolerant_route(stack):
    client, seen = stack
    # Non-consuming route: directory query survives (tolerant-ignore, no
    # consumption) and forwards upstream verbatim with the raw query.
    resp = await client.get(
        "/slimapi/global/health?v=3&directory=/w", headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) is None
    assert seen[0].url.query.decode("latin-1") == "directory=/w"


async def test_v2_directory_not_consumed_by_selector(stack):
    client, seen = stack
    resp = await client.get(
        "/slimapi/session/s1?directory=/w", headers=V2_HEADERS)
    assert resp.status_code == 200
    # v2 (§5.2): directory query forwards verbatim in the raw query and is
    # NOT converted to a header — no sidecar smart handling.
    assert seen[0].url.query.decode("latin-1") == "directory=/w"
    assert seen[0].headers.get(DIRECTORY_HEADER) is None


async def test_v3_missing_selector_on_read_route_gated(stack):
    client, _ = stack
    resp = await client.get("/slimapi/file?path=r", headers=IDENTITY)
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "version_required"


async def test_missing_required_query_params_422(stack):
    client, _ = stack
    resp = await client.get("/slimapi/file?v=3", headers=IDENTITY)
    assert resp.status_code == 422
    resp = await client.get("/slimapi/find/file?v=3", headers=IDENTITY)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# §10.a frozen response-header passthrough set (C3)
# ---------------------------------------------------------------------------

def _frozen_headers_handler(request, payloads):
    return httpx.Response(
        200, content=payloads.get(request.url.path) or b"{}",
        headers={"Content-Type": "application/json",
                 "Location": "https://upstream.example/next",
                 "Retry-After": "3",
                 "X-Request-ID": "up-req-1",
                 "Last-Request-ID": "up-req-0"})


async def test_2xx_frozen_header_passthrough_set():
    app, _ = _build_app(_frozen_headers_handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.headers["location"] == "https://upstream.example/next"
        assert resp.headers["retry-after"] == "3"
        assert resp.headers["x-request-id"] == "up-req-1"
        assert resp.headers["last-request-id"] == "up-req-0"
        assert resp.headers["cache-control"] == "no-store"
        # Upstream Content-Encoding is NOT in the frozen set: identity
        # upstream body + identity client ⇒ no coding header leaks.
        assert "content-encoding" not in resp.headers


async def test_4xx_frozen_header_passthrough_set():
    def handler(request, payloads):
        return httpx.Response(
            429, content=b'{"error": "slow_down"}',
            headers={"Content-Type": "application/json",
                     "Retry-After": "30",
                     "Location": "/retry",
                     "X-Request-ID": "up-req-9"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 429
        assert resp.content == b'{"error": "slow_down"}'
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers["retry-after"] == "30"
        assert resp.headers["location"] == "/retry"
        assert resp.headers["x-request-id"] == "up-req-9"


async def test_upstream_gzip_entity_decoded_recoded_not_passed_through():
    import gzip as _gzip

    raw = orjson.dumps({"sourceControl": {"notes": "n" * 4096}})
    gz = _gzip.compress(raw)

    def handler(request, payloads):
        return httpx.Response(
            200, content=gz,
            headers={"Content-Type": "application/json",
                     "Content-Encoding": "gzip"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        # httpx decoded the upstream entity; identity client ⇒ the sidecar
        # re-emits the decoded bytes with NO coding header (entity-byte
        # semantics — the upstream's own coding header never leaks).
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.content == raw
        assert "content-encoding" not in resp.headers
        # gzip client ⇒ the sidecar re-compresses under its own gate with
        # its own weak validator (§6.1 sidecar-owned ETag domain).
        resp2 = await client.get("/slimapi/vcs?v=3",
                                 headers={"Accept-Encoding": "gzip"})
        assert resp2.headers.get("content-encoding") == "gzip"
        assert resp2.headers["etag"].startswith("W/")
        assert resp2.content == raw


# ---------------------------------------------------------------------------
# B2: upstream success status passthrough (§10: status + body verbatim)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 202, 206])
async def test_upstream_success_status_and_body_verbatim(status):
    body = orjson.dumps({"note": "x" * 80})

    def handler(request, payloads):
        return httpx.Response(status, content=body,
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == status
        assert resp.content == body


async def test_upstream_204_empty_body_no_etag_no_gzip():
    def handler(request, payloads):
        return httpx.Response(204, headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 204
        assert resp.content == b""
        # Empty body: gzip benefit gate skips coding; no entity → no ETag.
        assert "content-encoding" not in resp.headers
        assert "etag" not in resp.headers


async def test_upstream_301_not_followed_location_passthrough():
    seen_requests: list[httpx.Request] = []

    def handler(request, payloads):
        seen_requests.append(request)
        if request.url.path == "/vcs":
            return httpx.Response(301, content=b"moved",
                                  headers={"Location": "/vcs/status",
                                           "Content-Type": "text/plain"})
        return httpx.Response(200, content=b"should-not-be-fetched")

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 301
        assert resp.content == b"moved"
        assert resp.headers["location"] == "/vcs/status"
        # §10: sidecar does not follow redirects — exactly one upstream hit.
        assert len(seen_requests) == 1
        assert seen_requests[0].url.path == "/vcs"


# ---------------------------------------------------------------------------
# B3: v2 query directory validation on consuming routes (v2-contract:483)
# ---------------------------------------------------------------------------

async def test_v2_invalid_directory_query_400():
    def handler(request, payloads):
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file?path=r&directory=../..",
                                headers=V2_HEADERS)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_directory"


async def test_v2_multi_directory_any_invalid_400():
    def handler(request, payloads):
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/file?path=r&directory=/w1&directory=../x",
                                headers=V2_HEADERS)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_directory"


async def test_v2_multi_directory_all_legal_still_verbatim():
    def handler(request, payloads):
        return httpx.Response(200, content=b"[]",
                              headers={"Content-Type": "application/json"})

    app, seen = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get(
            "/slimapi/file?path=r&directory=/w1&directory=/w2",
            headers=V2_HEADERS)
        assert resp.status_code == 200
        # Validation is NOT a rebuild: legal multi-values stay verbatim.
        assert seen[0].url.query.decode("latin-1") == \
            "path=r&directory=/w1&directory=/w2"


async def test_v3_tolerant_route_invalid_directory_passthrough():
    def handler(request, payloads):
        return httpx.Response(200, content=orjson.dumps(
            {"healthy": True, "version": "1.2.3"}),
            headers={"Content-Type": "application/json"})

    app, seen = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        # §5.5 tolerant-ignore = no consumption, no validation: the raw
        # bytes pass through untouched (aligned with events etc.).
        resp = await client.get("/slimapi/global/health?v=3&directory=../..",
                                headers=IDENTITY)
        assert resp.status_code == 200
        assert seen[0].url.query.decode("latin-1") == "directory=../.."
        assert seen[0].headers.get(DIRECTORY_HEADER) is None


# ---------------------------------------------------------------------------
# B4: projection routes occupy the transform pool (admission frozen, §10.a)
# ---------------------------------------------------------------------------

async def test_session_single_pool_saturation_transform_busy():
    """Pool full → 503 transform_busy (Retry-After: 2), no upstream GET,
    no event-loop projection — admission BEFORE the fetch (§10.a:141)."""
    def handler(request, payloads):
        return httpx.Response(200, content=payloads["/session/s1"],
                              headers={"Content-Type": "application/json"})

    app, seen = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        async with app.state.transforms:  # occupy the single slot
            resp = await client.get("/slimapi/session/s1?v=3",
                                    headers=IDENTITY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "transform_busy"
        assert resp.headers["retry-after"] == "2"
        assert seen == []  # admission before the upstream GET


async def test_raw_route_unaffected_by_pool_saturation():
    """§10.a: pure-raw controlled proxies do NOT occupy the pool — a
    saturated pool must not turn raw routes into transform_busy."""
    payloads = _read_payloads()

    def handler(request, _):
        return httpx.Response(200, content=payloads["/vcs"],
                              headers={"Content-Type": "application/json"})

    app, seen = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        async with app.state.transforms:  # occupy the single slot
            resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.content == payloads["/vcs"]
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# B5: projection only for 2xx + legal JSON object (§10.a:141)
# ---------------------------------------------------------------------------

def _spy_project(monkeypatch):
    calls: list[bytes] = []

    def spy(raw: bytes) -> bytes:
        calls.append(raw)
        return read_groups._project_session(raw)

    monkeypatch.setattr(read_groups, "_project_session", spy)
    return calls


async def test_session_single_204_empty_body_verbatim_no_projection(monkeypatch):
    calls = _spy_project(monkeypatch)

    def handler(request, payloads):
        return httpx.Response(204,
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/session/s1?v=3", headers=IDENTITY)
        assert resp.status_code == 204
        assert resp.content == b""
        assert calls == []  # never projected


async def test_session_single_301_non_json_verbatim_no_projection(monkeypatch):
    calls = _spy_project(monkeypatch)

    def handler(request, payloads):
        return httpx.Response(301, content=b"moved",
                              headers={"Location": "/session/s2",
                                       "Content-Type": "text/plain"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/session/s1?v=3", headers=IDENTITY)
        assert resp.status_code == 301
        assert resp.content == b"moved"
        assert resp.headers["location"] == "/session/s2"
        assert calls == []  # never projected


async def test_session_single_200_bad_json_503():
    def handler(request, payloads):
        return httpx.Response(200, content=b'{"id": ',
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/session/s1?v=3", headers=IDENTITY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"


async def test_session_single_200_non_dict_503():
    def handler(request, payloads):
        return httpx.Response(200, content=b"[1, 2]",
                              headers={"Content-Type": "application/json"})

    app, _ = _build_app(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/session/s1?v=3", headers=IDENTITY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# C1: cap-protected error-body reads (§10.a:141 — over-limit degrades to 503)
# ---------------------------------------------------------------------------

async def test_upstream_4xx_oversize_error_body_degrades_503():
    big = b"x" * 4096

    def handler(request, payloads):
        return httpx.Response(400, content=big,
                              headers={"Content-Type": "text/plain"})

    app, _ = _build_app(handler, settings=_settings(max_response_bytes=256))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        # Resource protection wins over the verbatim duty (§10.a frozen).
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"


async def test_upstream_5xx_oversize_error_body_degrades_503():
    big = b"x" * 4096

    def handler(request, payloads):
        return httpx.Response(500, content=big,
                              headers={"Content-Type": "text/plain"})

    app, _ = _build_app(handler, settings=_settings(max_response_bytes=256))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as client:
        resp = await client.get("/slimapi/vcs?v=3", headers=IDENTITY)
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"
