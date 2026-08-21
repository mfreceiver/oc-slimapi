"""B4-1/2/3 — 6 new annexed v3 routes (running agent/model switch, context
read, three-step revert) — additive alongside the existing single-step
``POST /slimapi/session/{sid}/revert``.

Routes under test (sidecar → upstream, v2 session group anchored to
``packages/protocol/src/groups/session.ts:173-305``):

* POST /slimapi/session/{sid}/agent     → POST /api/session/{sid}/agent  (204)
* POST /slimapi/session/{sid}/model     → POST /api/session/{sid}/model  (204)
* POST /slimapi/session/{sid}/revert/stage  → POST /api/session/{sid}/revert/stage  (200)
* POST /slimapi/session/{sid}/revert/clear  → POST /api/session/{sid}/revert/clear  (204)
* POST /slimapi/session/{sid}/revert/commit → POST /api/session/{sid}/revert/commit (204)
* GET  /slimapi/session/{sid}/context   → GET  /api/session/{sid}/context (200)

The upstream v2 session group resolves location per-sid via
sessionLocationMiddleware — directory **not consumed**: a client
``?directory=`` is tolerated and dropped (no upstream forwarding, no error).
The 5 write routes reuse ``routes/write_groups._write_passthrough`` (same
pipeline as the existing single-step revert); the context read uses the
``routes/_read_passthrough.read_passthrough_get`` raw-route helper (same as
the §10.a read groups).

Harness mirrors ``tests/test_write_groups.py`` (MockTransport recording +
selector middleware); both write_groups.router and read_groups.router are
mounted so the three-step revert can be regression-checked against the
existing single-step revert.
"""
from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import read_groups, write_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.traffic import bucketize

IDENTITY = {"Accept-Encoding": "identity"}
DIRECTORY_HEADER = "X-Opencode-Directory"

SESSION_BODY = orjson.dumps({
    "id": "s1", "title": "one", "parentID": None,
    "directory": "/w", "projectID": "p1", "agent": "build",
    "model": {"modelID": "m"},
    "time": {"created": 1, "updated": 2},
})
STAGE_BODY = orjson.dumps({"data": {"messageID": "m1"}})
CONTEXT_BODY = orjson.dumps({"data": [{"type": "text", "text": "hi"}]})
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
    """App mirroring production wiring for BOTH routers (write + read).

    Used for every test so the three-step revert coexists with the existing
    single-step revert and the context read coexists with session-single.
    """
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
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _ok(request: httpx.Request) -> httpx.Response:
    """Default handler: per-upstream-path canned success bodies."""
    path = request.url.path
    bodies = {
        "/api/session/s1/agent": (204, NOCONTENT),
        "/api/session/s1/model": (204, NOCONTENT),
        "/api/session/s1/revert/stage": (200, STAGE_BODY),
        "/api/session/s1/revert/clear": (204, NOCONTENT),
        "/api/session/s1/revert/commit": (204, NOCONTENT),
        "/api/session/s1/context": (200, CONTEXT_BODY),
        "/session/s1/revert": (200, SESSION_BODY),  # existing single-step
    }
    if path in bodies:
        status, body = bodies[path]
        headers = {"Content-Type": "application/json"} if body else {}
        return httpx.Response(status, content=body, headers=headers)
    return httpx.Response(404, content=b'{"error":"nf"}',
                          headers={"Content-Type": "application/json"})


@pytest.fixture
async def stack():
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        yield client, seen


# B4 write endpoints: (label, sidecar path minus /slimapi, upstream path)
WRITE_POSTS = [
    ("agent", "/session/s1/agent", "/api/session/s1/agent"),
    ("model", "/session/s1/model", "/api/session/s1/model"),
    ("stage", "/session/s1/revert/stage", "/api/session/s1/revert/stage"),
    ("clear", "/session/s1/revert/clear", "/api/session/s1/revert/clear"),
    ("commit", "/session/s1/revert/commit", "/api/session/s1/revert/commit"),
]

# Request bodies per endpoint (204-POSTs forward a payload too — the sidecar
# never parses; the upstream validates).
POST_BODIES = {
    "agent": orjson.dumps({"agent": "build"}),
    "model": orjson.dumps({"model": "anthropic/claude"}),
    "stage": orjson.dumps({"messageID": "m1", "files": True}),
    "clear": b"",
    "commit": b"",
}


def _send(client, method: str, path: str, *, content=b"",
          content_type="application/json", headers=None, extra_query=""):
    h = dict(headers or {})
    if content_type is not None:
        h.setdefault("Content-Type", content_type)
    h.setdefault("Accept-Encoding", "identity")
    url = f"/slimapi{path}?v=4{extra_query}"
    return client.request(method, url, content=content, headers=h)


# ===========================================================================
# happy passthrough (204 NoContent / 200 body verbatim / context 200)
# ===========================================================================

async def test_agent_happy_204(stack):
    """agent → 204 NoContent, empty body, single upstream call to
    /api/session/{sid}/agent."""
    client, seen = stack
    resp = await _send(client, "POST", "/session/s1/agent",
                       content=POST_BODIES["agent"])
    assert resp.status_code == 204
    assert resp.content == b""
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/session/s1/agent"
    assert resp.headers.get("cache-control") == "no-store"


async def test_model_happy_204(stack):
    client, seen = stack
    resp = await _send(client, "POST", "/session/s1/model",
                       content=POST_BODIES["model"])
    assert resp.status_code == 204
    assert resp.content == b""
    assert len(seen) == 1
    assert seen[0].url.path == "/api/session/s1/model"


async def test_revert_stage_happy_200_body_verbatim(stack):
    """stage → 200 with the upstream body byte-verbatim (incl. content-type)."""
    client, seen = stack
    resp = await _send(client, "POST", "/session/s1/revert/stage",
                       content=POST_BODIES["stage"])
    assert resp.status_code == 200
    assert resp.content == STAGE_BODY
    assert resp.headers["content-type"].startswith("application/json")
    assert seen[0].url.path == "/api/session/s1/revert/stage"


async def test_revert_clear_and_commit_happy_204(stack):
    client, seen = stack
    for label, path in (("clear", "/session/s1/revert/clear"),
                        ("commit", "/session/s1/revert/commit")):
        resp = await _send(client, "POST", path)
        assert resp.status_code == 204, label
        assert resp.content == b"", label
    assert [r.url.path for r in seen] == [
        "/api/session/s1/revert/clear",
        "/api/session/s1/revert/commit",
    ]


async def test_context_happy_200(stack):
    """GET context → 200 with the upstream {data: [...]} body verbatim."""
    client, seen = stack
    resp = await _send(client, "GET", "/session/s1/context")
    assert resp.status_code == 200
    assert resp.content == CONTEXT_BODY
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["cache-control"] == "no-store"
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/session/s1/context"


# ===========================================================================
# upstream 404 → status + body verbatim
# ===========================================================================

@pytest.mark.parametrize(
    "label,path,upstream", WRITE_POSTS + [("context", "/session/s1/context",
                                           "/api/session/s1/context")])
async def test_upstream_404_verbatim(stack, label, path, upstream):
    """Upstream 404 → status + body byte-verbatim (frozen header set only)."""
    err_body = b'{"error":{"code":"SessionNotFound"}}'
    app, seen = _build_app(lambda r: httpx.Response(
        404, content=err_body,
        headers={"Content-Type": "application/json", "X-Custom": "no"}))
    async with _client(app) as client:
        if label == "context":
            resp = await _send(client, "GET", path)
        else:
            resp = await _send(client, "POST", path,
                               content=POST_BODIES[label])
    assert resp.status_code == 404
    assert resp.content == err_body
    assert resp.headers["content-type"].startswith("application/json")
    assert "x-custom" not in resp.headers  # frozen set only
    assert seen[0].url.path == upstream


# ===========================================================================
# upstream 5xx → 503 upstream_unavailable / network error → 503
# ===========================================================================

async def test_upstream_5xx_503():
    app, _ = _build_app(lambda r: httpx.Response(
        500, content=b"boom",
        headers={"Content-Type": "text/plain"}))
    async with _client(app) as client:
        resp = await _send(client, "POST", "/session/s1/agent",
                           content=POST_BODIES["agent"])
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


async def test_context_upstream_5xx_503():
    app, _ = _build_app(lambda r: httpx.Response(502, content=b"bad"))
    async with _client(app) as client:
        resp = await _send(client, "GET", "/session/s1/context")
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


async def test_network_error_503():
    def net_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")
    app, _ = _build_app(net_err)
    async with _client(app) as client:
        resp = await _send(client, "POST", "/session/s1/revert/commit")
    assert resp.status_code == 503
    assert orjson.loads(resp.content)["code"] == "upstream_unavailable"


# ===========================================================================
# request body + content-type forwarded verbatim (incl. stage files optional)
# ===========================================================================

async def test_body_and_content_type_forwarded():
    """agent body + custom content-type forwarded verbatim, byte-identical."""
    app, seen = _build_app(_ok)
    sentinel = b'{"agent":"x\xc3\xa9"}'
    async with _client(app) as client:
        resp = await _send(client, "POST", "/session/s1/agent",
                           content=sentinel,
                           content_type="application/x-custom+json")
    assert resp.status_code == 204
    assert seen[0].read() == sentinel
    assert seen[0].headers["content-type"] == "application/x-custom+json"


async def test_stage_files_field_optional():
    """stage: files=false and absent both forward to the same upstream path
    (sidecar never parses; the upstream validates)."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        r1 = await _send(client, "POST", "/session/s1/revert/stage",
                         content=orjson.dumps({"messageID": "m1", "files": False}))
        r2 = await _send(client, "POST", "/session/s1/revert/stage",
                         content=orjson.dumps({"messageID": "m2"}))
    assert r1.status_code == 200 and r2.status_code == 200
    assert orjson.loads(seen[0].read()) == {"messageID": "m1", "files": False}
    assert orjson.loads(seen[1].read()) == {"messageID": "m2"}
    assert [r.url.path for r in seen] == [
        "/api/session/s1/revert/stage"] * 2


# ===========================================================================
# directory: tolerant-ignore (not consumed → not forwarded, never an error)
# ===========================================================================

@pytest.mark.parametrize("label,path,upstream", WRITE_POSTS)
async def test_directory_tolerant_ignored_not_forwarded(stack, label, path,
                                                        upstream):
    """?directory= on a B4 route: accepted, dropped — the upstream request
    carries NO directory in URL query NOR as a header (non-consuming set)."""
    client, seen = stack
    resp = await _send(client, "POST", path,
                       content=POST_BODIES[label],
                       extra_query="&directory=/w&zz=keep")
    assert resp.status_code < 500
    assert seen[0].url.path == upstream
    assert seen[0].url.params.get("directory") is None
    assert seen[0].url.params.get("v") is None
    assert "zz=keep" in seen[0].url.query.decode("latin-1")  # rest verbatim
    assert seen[0].headers.get(DIRECTORY_HEADER) is None


async def test_context_directory_tolerant_ignored(stack):
    client, seen = stack
    resp = await _send(client, "GET", "/session/s1/context",
                       extra_query="&directory=/w&zz=keep")
    assert resp.status_code == 200
    assert seen[0].url.params.get("directory") is None
    assert "zz=keep" in seen[0].url.query.decode("latin-1")
    assert seen[0].headers.get(DIRECTORY_HEADER) is None


async def test_directory_invalid_value_tolerated_no_400():
    """Tolerant non-consuming set: an INVALID ?directory= value is NOT a 400
    (no consumption → no validation) — forwarded neither upstream."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        resp = await _send(client, "POST", "/session/s1/agent",
                           content=POST_BODIES["agent"],
                           extra_query="&directory=../etc")
    assert resp.status_code == 204  # no 400, no error
    assert seen[0].url.params.get("directory") is None
    assert seen[0].headers.get(DIRECTORY_HEADER) is None


# ===========================================================================
# three-step revert coexists with the existing single-step revert
# ===========================================================================

async def test_three_step_coexists_with_single_step_revert():
    """stage/clear/commit and the legacy single-step revert each hit their
    OWN upstream path — no route shadowing, no cross-forwarding."""
    app, seen = _build_app(_ok)
    async with _client(app) as client:
        r_stage = await _send(client, "POST", "/session/s1/revert/stage",
                              content=orjson.dumps({"messageID": "m1"}))
        r_single = await _send(client, "POST", "/session/s1/revert",
                               content=orjson.dumps(
                                   {"messageID": "m1", "partID": "p1"}))
        r_clear = await _send(client, "POST", "/session/s1/revert/clear")
        r_commit = await _send(client, "POST", "/session/s1/revert/commit")
    assert r_stage.status_code == 200
    assert r_single.status_code == 200
    assert r_clear.status_code == 204
    assert r_commit.status_code == 204
    assert [r.url.path for r in seen] == [
        "/api/session/s1/revert/stage",
        "/session/s1/revert",            # existing single-step, unchanged
        "/api/session/s1/revert/clear",
        "/api/session/s1/revert/commit",
    ]


# ===========================================================================
# request-size cap → 413 before any upstream call
# ===========================================================================

async def test_request_body_over_cap_413():
    app, seen = _build_app(_ok, settings=_settings(max_message_bytes=16))
    big = b'{"messageID":"' + b"x" * 32 + b'"}'
    async with _client(app) as client:
        resp = await _send(client, "POST", "/session/s1/revert/stage",
                           content=big)
    assert resp.status_code == 413
    assert orjson.loads(resp.content)["code"] == "request_too_large"
    assert not seen  # rejected before the upstream call


# ===========================================================================
# traffic bucketize: 5 POSTs → write_session, context GET → session_context
# ===========================================================================

def test_bucketize_b4_write_posts_write_session():
    for _label, path, _upstream in WRITE_POSTS:
        assert bucketize("POST", f"/slimapi{path}") == "write_session"


def test_bucketize_context_get_session_context():
    assert bucketize("GET", "/slimapi/session/s1/context") == "session_context"
    # non-GET on context (no such route → 405) stays write_session
    assert bucketize("POST", "/slimapi/session/s1/context") == "write_session"


def test_bucketize_session_single_unchanged():
    """Regression: the §10.a session-single GET keeps its own bucket."""
    assert bucketize("GET", "/slimapi/session/s1") == "session_single"
    assert bucketize("GET", "/slimapi/session/s1/revert") == "session_single"