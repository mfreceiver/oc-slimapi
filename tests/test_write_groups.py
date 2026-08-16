"""v3-contract §10.b — the 12 annexed WRITE endpoints (Batch C2).

``routes/write_groups.py`` gives every endpoint the frozen unified-write
pipeline:

* request **body + content-type** forwarded verbatim;
* **request-size cap** → 413 ``request_too_large`` before the upstream call
  (``max_message_bytes`` semantics — the contract's "既有 max_request_bytes
  语义" anchor; repo knob is ``max_message_bytes``);
* **query verbatim** forwarding (v stripped everywhere; ``directory``
  additionally stripped on v3 consuming routes — write routes all consume);
* **directory consumed on all 12** (upstream ``WorkspaceRoutingQuery``):
  v3 ``?directory=`` via the selector stash; v2 = the
  ``X-Opencode-Directory`` header channel (bound + validated);
  v2 ``?directory=`` values validated then forwarded verbatim;
* response **status verbatim** including 3xx (never followed — upstream
  client has ``follow_redirects=False``) and 204/202/201;
* response-header frozen set: ``Content-Type`` / ``Location`` /
  ``Retry-After`` / ``X-Request-ID`` / ``Last-Request-ID`` (present ones
  only); upstream ``Content-Encoding`` never passes;
* two-tier errors: upstream 4xx → status+body verbatim; 5xx / network →
  503 ``upstream_unavailable``; response cap → 413 ``response_too_large``;
* no ETag (write routes); ``Cache-Control: no-store`` + merged
  ``Vary: Accept-Encoding, X-Opencode-Directory`` (directory-sensitive set,
  §6.2); gzip re-encode via the repo benefit gate on success bodies;
* no transform-pool admission (no projection → no ``transform_busy``).

The harness mirrors ``tests/test_read_groups.py`` (MockTransport recording
handler + selector middleware) and additionally asserts the forwarded
method / body bytes / content-type on the recorded upstream request.
"""
from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import write_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware

IDENTITY = {"Accept-Encoding": "identity"}
V2_HEADERS = {"X-Slimapi-Version": "2", **IDENTITY}
DIRECTORY_HEADER = "X-Opencode-Directory"

SESSION_BODY = orjson.dumps({
    "id": "s1", "title": "one", "parentID": None,
    "directory": "/w", "projectID": "p1", "agent": "build",
    "model": {"modelID": "m"},
    "time": {"created": 1, "updated": 2},
})
BOOLEAN_TRUE = b"true"
NOCONTENT = b""


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(handler, *, settings: Settings | None = None):
    """App mirroring production write-route wiring (selector → routes)."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream)
    app.state.schema_degraded = False
    app.include_router(write_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _ok(request: httpx.Request) -> httpx.Response:
    """Default handler: per-path canned success bodies."""
    path = request.url.path
    bodies = {
        "/session": (201, SESSION_BODY),
        "/session/s1": (200, SESSION_BODY),
        "/session/s1/abort": (200, BOOLEAN_TRUE),
        "/session/s1/summarize": (200, BOOLEAN_TRUE),
        "/session/s1/prompt_async": (202, NOCONTENT),
        "/session/s1/fork": (200, SESSION_BODY),
        "/session/s1/revert": (200, SESSION_BODY),
        "/session/s1/permissions/p1": (200, BOOLEAN_TRUE),
        "/session/s1/command": (200, SESSION_BODY),
        "/question/q1/reply": (200, BOOLEAN_TRUE),
        "/question/q1/reject": (200, BOOLEAN_TRUE),
    }
    if request.method == "DELETE" and path == "/session/s1":
        return httpx.Response(204)
    if path in bodies:
        status, body = bodies[path]
        return httpx.Response(status, content=body,
                              headers={"Content-Type": "application/json"})
    return httpx.Response(404, content=b'{"error":"nf"}',
                          headers={"Content-Type": "application/json"})


@pytest.fixture
async def stack():
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        yield client, seen


# All 12 endpoints: (label, sidecar path minus /slimapi, method, upstream path)
ENDPOINTS = [
    ("create", "/session", "POST", "/session"),
    ("update", "/session/s1", "PATCH", "/session/s1"),
    ("delete", "/session/s1", "DELETE", "/session/s1"),
    ("prompt_async", "/session/s1/prompt_async", "POST", "/session/s1/prompt_async"),
    ("abort", "/session/s1/abort", "POST", "/session/s1/abort"),
    ("summarize", "/session/s1/summarize", "POST", "/session/s1/summarize"),
    ("fork", "/session/s1/fork", "POST", "/session/s1/fork"),
    ("revert", "/session/s1/revert", "POST", "/session/s1/revert"),
    ("permission", "/session/s1/permissions/p1", "POST",
     "/session/s1/permissions/p1"),
    ("reply", "/question/q1/reply", "POST", "/question/q1/reply"),
    ("reject", "/question/q1/reject", "POST", "/question/q1/reject"),
    ("command", "/session/s1/command", "POST", "/session/s1/command"),
]

BODIES = {
    "create": (orjson.dumps({"title": "t"}), "application/json"),
    "update": (orjson.dumps({"title": "t2"}), "application/json"),
    "delete": (b"", None),
    "prompt_async": (orjson.dumps({"providerID": "x"}), "application/json"),
    "abort": (b"", None),
    "summarize": (orjson.dumps({"providerID": "x", "modelID": "m"}),
                  "application/json"),
    "fork": (orjson.dumps({"messageID": "m1"}), "application/json"),
    "revert": (orjson.dumps({"messageID": "m1", "partID": "p1"}),
               "application/json"),
    "permission": (orjson.dumps({"response": "once"}), "application/json"),
    "reply": (orjson.dumps({"options": {}}), "application/json"),
    "reject": (b"", None),
    "command": (orjson.dumps({"command": "ls"}), "application/json"),
}


def _send(client, label: str, path: str, method: str, *, v3: bool = True,
          headers: dict | None = None, content=None, content_type=None):
    body, default_ct = BODIES[label]
    if content is None:
        content = body
    if content_type is None and default_ct is not None:
        content_type = default_ct
    h = dict(headers or {})
    if content_type is not None:
        h.setdefault("Content-Type", content_type)
    h.setdefault("Accept-Encoding", "identity")
    url = f"/slimapi{path}" + ("?v=3" if v3 else "")
    return client.request(method, url, content=content, headers=h)


# ===========================================================================
# §11.10 per-endpoint happy: status + body verbatim + frozen headers
# ===========================================================================

@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_endpoint_happy_v3(stack, label, path, method, upstream):
    client, seen = stack
    resp = await _send(client, label, path, method)
    expected_status = {  # mirrors _ok
        "create": 201, "delete": 204, "prompt_async": 202,
    }.get(label, 200)
    assert resp.status_code == expected_status
    if label == "delete":
        assert resp.content == b""
    elif label == "prompt_async":
        assert resp.content == b""
    else:
        assert resp.content == (SESSION_BODY if label not in (
            "abort", "summarize", "permission", "reply", "reject")
            else BOOLEAN_TRUE)
    # exactly ONE upstream call, right method + path
    assert len(seen) == 1
    assert seen[0].method == method
    assert seen[0].url.path == upstream
    # frozen response headers on success
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("vary") == "Accept-Encoding, X-Opencode-Directory"
    assert "etag" not in resp.headers  # write routes: no ETag
    if label not in ("delete", "prompt_async"):
        assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_endpoint_body_and_content_type_forwarded(
        stack, label, path, method, upstream):
    """Request body + content-type forwarded verbatim (incl. non-JSON CT)."""
    client, seen = stack
    sentinel = b'{"x":"\xc3\xa9"}'
    resp = await _send(
        client, label, path, method,
        content=sentinel if label not in ("abort", "reject", "delete") else b"",
        content_type="application/x-custom+json" if label not in (
            "abort", "reject", "delete") else None,
    )
    assert resp.status_code < 500
    assert seen[0].url.path == upstream
    if label not in ("abort", "reject", "delete"):
        assert seen[0].read() == sentinel
        assert seen[0].headers["content-type"] == "application/x-custom+json"


# ===========================================================================
# directory: v2 header channel / v3 query consumption / invalid → 400
# ===========================================================================

@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_directory_v2_header_channel(stack, label, path, method,
                                                 upstream):
    """Converted (terminal §2): the v2 form is rejected before the route;
    §8.3 ② (selector) outranks ③ (retired header)."""
    client, seen = stack
    resp = await _send(
        client, label, path, method, v3=False,
        headers={**V2_HEADERS, DIRECTORY_HEADER: "/w"})
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "unsupported_version"
    assert not seen


@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_directory_v3_query_consumed_and_forwarded(
        stack, label, path, method, upstream):
    """v3 ``?directory=`` → consumed by the selector, forwarded as header;
    the upstream query carries NEITHER ``v`` NOR ``directory``."""
    client, seen = stack
    body_bytes = BODIES[label][0] or b""
    ct = {"Content-Type": "application/json"} if BODIES[label][1] else {}
    resp = await client.request(
        method, f"/slimapi{path}?v=3&directory=/w",
        content=body_bytes, headers=IDENTITY | ct,
    )
    assert resp.status_code < 500
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"
    assert seen[0].url.params.get("directory") is None
    assert seen[0].url.params.get("v") is None


@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_directory_v3_invalid_query_400(stack, label, path, method,
                                                    upstream):
    """v3 invalid ``?directory=`` → selector-layer 400 (invalid_directory)."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            method, f"/slimapi{path}?v=3&directory=../etc",
            content=BODIES[label][0] or b"",
            headers={**IDENTITY, **({"Content-Type": "application/json"} if BODIES[label][1] else {})})
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "invalid_directory"
    assert not seen  # nothing reached the upstream


async def test_write_directory_v2_query_values_unsupported():
    """Converted (terminal): v=2 is a retired protocol version."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session/s1/abort?v=2&directory=/w",
            headers=V2_HEADERS)
    assert resp.status_code == 400
    assert orjson.loads(resp.content) == {
        "code": "unsupported_version", "supported": [3]}
    assert not seen


async def test_write_directory_v2_query_invalid_unsupported():
    """§8.3: the selector error (②) outranks the directory error (③)."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session/s1/abort?v=2&directory=../etc",
            headers=V2_HEADERS)
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "unsupported_version"
    assert not seen


async def test_write_directory_conflict_dual_present():
    """v3 query + header with different values → 400 directory_conflict."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session/s1/abort?v=3&directory=/w",
            headers={**IDENTITY, DIRECTORY_HEADER: "/other"})
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "directory_conflict"
    assert not seen


async def test_write_directory_multi_value_400():
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session/s1/abort?v=3&directory=/w&directory=/x",
            headers=IDENTITY)
    assert resp.status_code == 400
    assert orjson.loads(resp.content)["code"] == "invalid_directory_selector"
    assert not seen


# ===========================================================================
# two-tier errors: 4xx verbatim / 5xx → 503 / network → 503
# ===========================================================================

@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_upstream_4xx_verbatim(stack, label, path, method, upstream):
    client, seen = stack
    err_body = b'{"error":{"code":"Validation"}}'
    app, seen2 = _build_app(lambda r: httpx.Response(
        422, content=err_body, headers={"Content-Type": "application/json",
                                        "X-Custom": "no"}))
    async with _client(app) as client:
        resp = await _send(client, label, path, method)
    assert resp.status_code == 422
    assert resp.content == err_body
    assert resp.headers["content-type"].startswith("application/json")
    assert "x-custom" not in resp.headers  # frozen set only


@pytest.mark.parametrize("label,path,method,upstream", ENDPOINTS)
async def test_write_upstream_5xx_503(stack, label, path, method, upstream):
    app, _ = _build_app(lambda r: httpx.Response(
        500, content=b"boom",
        headers={"Content-Type": "text/plain"}))
    async with _client(app) as client:
        resp = await _send(client, label, path, method)
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


async def test_write_network_error_503():
    def net_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")
    app, _ = _build_app(net_err)
    async with _client(app) as client:
        resp = await _send(client, "abort", "/session/s1/abort", "POST")
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


# ===========================================================================
# horizontal: 3xx not followed / request cap / response cap / query verbatim
# ===========================================================================

async def test_write_3xx_status_and_body_verbatim():
    """Upstream 301 → status+body+Location pass through untouched (the
    sidecar neither follows nor rewrites)."""
    app, _ = _build_app(lambda r: httpx.Response(
        301, content=b"moved",
        headers={"Location": "/session/new", "Content-Type": "text/plain"}))
    async with _client(app) as client:
        resp = await _send(client, "create", "/session", "POST")
    assert resp.status_code == 301
    assert resp.content == b"moved"
    assert resp.headers.get("location") == "/session/new"


async def test_write_request_body_over_cap_413():
    """Request body over ``max_message_bytes`` → 413 request_too_large
    BEFORE any upstream call."""
    app, seen = _build_app(_ok, settings=_settings(max_message_bytes=16))
    big = b"x" * 32
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session?v=3", content=big,
            headers={"Content-Type": "application/json", **IDENTITY})
    assert resp.status_code == 413
    assert orjson.loads(resp.content)["code"] == "request_too_large"
    assert not seen  # rejected before the upstream call


async def test_write_response_over_cap_413():
    big = b"y" * (64 * 1024 + 1)
    app, _ = _build_app(lambda r: httpx.Response(
        200, content=big, headers={"Content-Type": "text/plain"}))
    async with _client(app) as client:
        resp = await _send(client, "create", "/session", "POST")
    assert resp.status_code == 413
    assert orjson.loads(resp.content)["code"] == "response_too_large"


async def test_write_query_verbatim_unknown_and_repeats():
    """Unknown / repeated query params forward byte-identically (v3 —
    the byte-fidelity semantics moved wholesale to the v3 channel)."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST",
            "/slimapi/session/s1/abort?v=3&zz=a%20b&zz=c&x=1&x=2",
            headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].url.query.decode("latin-1") == "zz=a%20b&zz=c&x=1&x=2"


async def test_write_v3_strips_v_and_directory_only():
    """v3 strips exactly ``v``+``directory``; everything else verbatim."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session/s1/abort?v=3&directory=/w&zz=%2Fkeep",
            headers=IDENTITY)
    assert resp.status_code == 200
    assert seen[0].url.query.decode("latin-1") == "zz=%2Fkeep"
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


# ===========================================================================
# PATCH dual-shape + fork messageID-as-body specifics (§11.10)
# ===========================================================================

async def test_patch_update_payload_shape(stack):
    client, seen = stack
    payload = orjson.dumps({"title": "t", "metadata": {"k": 1}})
    resp = await _send(client, "update", "/session/s1", "PATCH",
                       content=payload)
    assert resp.status_code == 200
    assert seen[0].read() == payload


async def test_patch_time_archived_shape(stack):
    """Second legal PATCH shape (time.archived) — sidecar does not
    distinguish; upstream sees the body verbatim."""
    client, seen = stack
    payload = orjson.dumps({"time": {"archived": 123456}})
    resp = await _send(client, "update", "/session/s1", "PATCH",
                       content=payload)
    assert resp.status_code == 200
    assert seen[0].read() == payload


async def test_fork_message_id_is_body_field(stack):
    """fork's ``messageID`` is a BODY JSON field (not a query param)."""
    client, seen = stack
    payload = orjson.dumps({"messageID": "m9"})
    resp = await _send(client, "fork", "/session/s1/fork", "POST",
                       content=payload)
    assert resp.status_code == 200
    assert seen[0].read() == payload
    assert "messageid" not in seen[0].url.params


async def test_fork_empty_body_nocontent_shape(stack):
    """ForkPayload is optional (NoContent) — an empty body forwards as-is."""
    client, seen = stack
    resp = await _send(client, "fork", "/session/s1/fork", "POST",
                       content=b"")
    assert resp.status_code == 200
    assert seen[0].read() == b""


# ===========================================================================
# gzip re-encode on success bodies (benefit gate, sidecar-owned coding)
# ===========================================================================

# ===========================================================================
# C2 gate follow-ups: cap-protected error bodies / present-only CT / calls
# ===========================================================================

async def test_write_4xx_error_body_over_cap_503():
    """§10.a:141 frozen (applies to §10.b): an oversized 4xx error body
    cannot be passed verbatim — the cap-protected read degrades to 503
    upstream_unavailable (resource protection wins)."""
    big = b"e" * (64 * 1024 + 1)
    app, seen = _build_app(lambda r: httpx.Response(422, content=big))
    async with _client(app) as client:
        resp = await _send(client, "abort", "/session/s1/abort", "POST")
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"
    assert len(seen) == 1  # the upstream WAS called once (response cap)


async def test_write_5xx_error_body_over_cap_503():
    big = b"e" * (64 * 1024 + 1)
    app, _ = _build_app(lambda r: httpx.Response(503, content=big))
    async with _client(app) as client:
        resp = await _send(client, "abort", "/session/s1/abort", "POST")
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


async def test_write_4xx_without_upstream_content_type_no_ct_added():
    """Present-only frozen set: upstream 4xx WITHOUT a Content-Type must
    NOT gain one from the sidecar (no setdefault injection)."""
    app, _ = _build_app(lambda r: httpx.Response(422, content=b'{"e":1}'))
    async with _client(app) as client:
        resp = await _send(client, "abort", "/session/s1/abort", "POST")
    assert resp.status_code == 422
    assert resp.content == b'{"e":1}'
    assert "content-type" not in resp.headers


async def test_write_success_without_upstream_content_type_no_ct_added():
    app, _ = _build_app(lambda r: httpx.Response(200, content=b"payload"))
    async with _client(app) as client:
        resp = await _send(client, "abort", "/session/s1/abort", "POST")
    assert resp.status_code == 200
    assert resp.content == b"payload"
    assert "content-type" not in resp.headers


async def test_write_request_cap_zero_upstream_calls():
    """Explicit call-count form of the request-cap rejection (seen == 0)."""
    app, seen = _build_app(_ok, settings=_settings(max_message_bytes=8))
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session?v=3", content=b"123456789",
            headers={"Content-Type": "application/json", **IDENTITY})
    assert resp.status_code == 413
    assert orjson.loads(resp.content)["code"] == "request_too_large"
    assert len(seen) == 0


async def test_write_success_gzip_reencode():
    """Large success body + AE:gzip → sidecar gzip re-encode (upstream's
    own Content-Encoding never passes; entity bytes only)."""
    big = orjson.dumps({"blob": "z" * 4096})
    app, _ = _build_app(lambda r: httpx.Response(
        201, content=big,
        headers={"Content-Type": "application/json",
                 "Content-Encoding": "br"}))  # upstream coding: dropped
    async with _client(app) as client:
        resp = await client.request(
            "POST", "/slimapi/session?v=3", content=b"{}",
            headers={"Content-Type": "application/json",
                     "Accept-Encoding": "gzip"})
    assert resp.status_code == 201
    assert resp.headers.get("content-encoding") == "gzip"  # sidecar's own
    # httpx transparently decompressed resp.content — the entity bytes
    # arrived intact through the sidecar's own re-encode.
    assert resp.content == big
