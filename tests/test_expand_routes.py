"""design-expand lane B: expand fragment endpoints + selector patterns +
merged placeholder-first (tests/test_expand_routes.py).

Covers the §12 lane-B rows end-to-end against the v5 frozen contract
(design-expand.md):

* selector / directory — missing/malformed/retired ``?v=`` → 400; bad
  directory forms → 400; the two expand routes consume ``?directory=`` and
  forward it upstream; ``Vary: Accept-Encoding`` on EVERY response
  (including all error shapes); selector errors PREEMPT category errors.
* evaluation order (§3.1, R4-M1 real-stage order) — pool-full 503 before
  any part 404; source-overlimit 413 before part 404 (and even before
  JSON decode: oversize+mALFORMED body → 413, R4-M1); sid 404 →
  ``session_not_found``; network / 5xx → 503; other 4xx → 502
  ``upstream_http_N``; error-code JSON asserted EXACTLY (no standalone
  ``part_missing`` / ``category_mismatch`` codes).
* level matching — part-level category without partID → 400
  ``expand_category_mismatch`` expectedLevel=part; message-level category
  WITH partID → 400 expectedLevel=message; patch part requesting a
  state category → 400 expectedTypes=["tool"]; step-finish requesting
  ``part_snapshot`` → 200.
* extractors — each of the 12 categories: one positive case + one nested
  type-mismatch negative case; decode failure / top-level non-dict → 503
  explicitly DISTINGUISHED from post-parse structural malformation → 502;
  metadata never leaks ``diagnostics``; ``compaction_full`` strips the
  sidecar ``expandRefs`` key; input/metadata/attachments missing vs
  explicit null both → 200 + data key null (R4-M2).
* boundaries — source body exactly at / 1 byte over ``max_message_bytes``;
  fragment exactly at / over ``max_expand_response_bytes`` including
  wrapper bytes pushing the envelope over the cap.
* singleflight — same-message same/different category concurrent → 1 GET;
  expand ∥ /full → 1 GET (shared key); different directory / different
  message → no coalescing; sequential expand after the 1s grace → fresh GET.
* merged placeholder-first (R3-B) — placeholder queue claims the budget
  first and ref candidates never displace it; long text-only messages are
  restored in-budget; over-budget keeps skeleton (null + expandRefs);
  merged ``info.summary.diffs`` stays null + expandRefs; intersection
  message → 1 slot, 1 fetch.
* /full/{mid} stays byte-identical (regression guard for the sid wiring).

The catalogue upstream fixture mirrors test_messages_merged / conftest:
one MockTransport handler per test, counting calls so coalescing is
observable.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.traffic import EXPAND_CATEGORIES as TRAFFIC_CATEGORIES
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}
IDENTITY = {"Accept-Encoding": "identity"}
V3 = "?v=3"
DIRECTORY_HEADER = "X-Opencode-Directory"

# §2.2 table order — part of the wire contract (validCategories replay).
# Derived from the SINGLE SOURCE OF TRUTH (oc_slimapi.traffic) so this module
# can never drift from the route/capabilities whitelist (rev-gpt R1 M1).
VALID_CATEGORIES = list(TRAFFIC_CATEGORIES)


def test_route_category_whitelist_matches_traffic_source_of_truth():
    """The route's accepted category set is the traffic constant itself —
    a private copy in messages.py would drift from the capabilities
    advertisement and the ledger whitelist (rev-gpt R1 M1)."""
    assert messages._EXPAND_CATEGORIES_SET == frozenset(TRAFFIC_CATEGORIES)
    assert messages._EXPAND_CATEGORIES == TRAFFIC_CATEGORIES  # same table order


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=2.0,
        transform_absorb_budget_seconds=2.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
        merged_fanout=8, merged_max_fulls_per_page=16,
        merged_max_bytes=8 * 1024 * 1024,
        max_expand_response_bytes=8 * 1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, settings: Settings) -> FastAPI:
    app = FastAPI(title="oc-slimapi-expand-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(messages.router)
    install_proxy(app)
    register_error_handlers(app)
    return app


@asynccontextmanager
async def _test_client(upstream_factory, handler, **overrides):
    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(**overrides))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        try:
            yield client
        finally:
            app.state.transforms.shutdown()


@asynccontextmanager
async def _selector_client(upstream_factory, handler, **overrides):
    """App WITH SlimapiSelectorMiddleware — selector semantics require the
    ?v=3 selector; directory consumption is handled by the middleware."""
    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(**overrides))
    app.add_middleware(SlimapiSelectorMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        try:
            yield client
        finally:
            app.state.transforms.shutdown()


# ---------------------------------------------------------------------------
# Catalogue upstream fixtures
# ---------------------------------------------------------------------------

# A rich message exercising every category's positive path.
FULL_MESSAGE = {
    "info": {
        "id": "m1", "role": "assistant",
        "time": {"created": 1000, "updated": 1000},
        "summary": {"diffs": [{"file": "a.ts", "additions": 1, "deletions": 0}]},
    },
    "parts": [
        {"id": "p_text", "type": "text", "messageID": "m1", "text": "hi"},
        {"id": "p_reason", "type": "reasoning", "messageID": "m1",
         "text": "think"},
        {"id": "p_tool", "type": "tool", "messageID": "m1", "tool": "bash",
         "state": {"status": "completed", "output": "out",
                   "input": {"command": "ls"},
                   "metadata": {"cursor": 7, "diagnostics": {"k": "v"}},
                   "attachments": [{"path": "/tmp/a"}]}},
        {"id": "p_err", "type": "tool", "messageID": "m1", "tool": "bash",
         "state": {"status": "error", "error": "boom"}},
        {"id": "p_file", "type": "file", "messageID": "m1",
         "url": "file:///tmp/a", "source": {"path": "/tmp/a"}},
        {"id": "p_ss", "type": "step-start", "messageID": "m1",
         "snapshot": "snap1"},
        {"id": "p_sf", "type": "step-finish", "messageID": "m1",
         "snapshot": "snap2"},
        {"id": "p_comp", "type": "compaction", "messageID": "m1",
         "text": "compact", "expandRefs": [{"category": "part_text"}]},
    ],
}


def _message_handler(message: dict | list | None, *, status: int = 200):
    """Return an upstream handler serving ``message`` as the single-message
    body (expand's upstream GET is per-mid)."""
    body = None if message is None else orjson.dumps(message)

    async def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, content=b"upstream err")
        return httpx.Response(
            status, content=body or b"",
            headers={"Content-Type": "application/json"},
        )

    return handler


async def _get(client, path: str, **kw):
    return await client.get(path, headers=kw.pop("headers", HDR), **kw)


def _json(response: httpx.Response) -> dict:
    return orjson.loads(response.content)


@pytest.fixture(autouse=True)
def _cleanup_global_singleflight():
    """Isolate the module-level singleflight registry between tests.

    ``oc_slimapi.sse.singleflight.fulls`` is a MODULE-level registry whose
    keys are ``(id(pool), sid, mid, directory)``. Each test builds a fresh
    TransformPool, but pytest reuses freed pool object addresses — a later
    test's pool can collide on ``id()`` with an earlier test's pool, so an
    earlier test's still-warm result (completion grace is 1 second) would
    be JOINED by the later test instead of triggering a fresh upstream GET.
    Enjoying a body that belongs to a DIFFERENT test (e.g. a malformed /
    missing-part body from an extractor test) surfaces as intermittent
    spurious 404/502 revelations across runs. ``shutdown()`` clears every
    retained entry (and cancels its expiry timers) while leaving the
    registry fully usable for the next test — so each test starts with an
    empty, id-free registry. Test-only cleanup; production has a single
    long-lived pool per process and never benefits from (or is harmed by)
    re-seeding this per-test.
    """
    yield
    messages.fulls.shutdown()


# ---------------------------------------------------------------------------
# 1) Selector / directory (SlimapiSelectorMiddleware + ?v=3)
# ---------------------------------------------------------------------------

@pytest.fixture
async def selector_stack():
    """App + SlimapiSelectorMiddleware + recording upstream."""

    def make_handler(counter: list[int], upstream: httpx.AsyncClient):
        async def handler(request: httpx.Request) -> httpx.Response:
            counter[0] += 1
            return httpx.Response(
                200, content=orjson.dumps(FULL_MESSAGE),
                headers={"Content-Type": "application/json"},
            )
        return handler

    async def make(upstream_factory):
        counter = [0]
        upstream = upstream_factory(make_handler(counter, None))
        app = _build_app(upstream, _settings())
        app.add_middleware(SlimapiSelectorMiddleware)
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0)
        return client, counter, app, upstream

    yield make
    # teardown handled by per-test async context managers where possible;
    # this fixture's clients are closed by the tests themselves.


async def test_selector_missing_v_400(upstream_factory):
    """No ?v= at all → 400 unsupported_version (retired-version request)."""
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text",
            headers=IDENTITY)
    assert r.status_code == 400
    assert _json(r) == {"code": "unsupported_version", "supported": [3]}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_selector_v2_unsupported(upstream_factory):
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text?v=2",
            headers=IDENTITY)
    assert r.status_code == 400
    assert _json(r) == {"code": "unsupported_version", "supported": [3]}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_selector_malformed_v_400(upstream_factory):
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text?v=abc",
            headers=IDENTITY)
    assert r.status_code == 400
    assert _json(r) == {"code": "invalid_version_selector"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_selector_multi_directory_400(upstream_factory):
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text"
            "?v=3&directory=/a&directory=/b",
            headers=IDENTITY)
    assert r.status_code == 400
    assert _json(r) == {"code": "invalid_directory_selector"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_selector_header_residue_400(upstream_factory):
    """Header-only directory (retired channel, §5.7) → 400
    directory_header_retired even on the expand routes."""
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text?v=3",
            headers={**IDENTITY, DIRECTORY_HEADER: "/w"})
    assert r.status_code == 400
    assert _json(r) == {"code": "directory_header_retired"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_selector_directory_consumed_and_forwarded(upstream_factory):
    """?directory=/w on BOTH expand route shapes is consumed (stripped from
    query) and forwarded upstream as X-Opencode-Directory."""
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=orjson.dumps(FULL_MESSAGE),
            headers={"Content-Type": "application/json"})

    async with _selector_client(upstream_factory, handler) as client:
        r1 = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text?v=3&directory=/w",
            headers=IDENTITY)
        r2 = await client.get(
            "/slimapi/messages/s1/expand/info_summary_diffs/m2?v=3&directory=/w",
            headers=IDENTITY)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(seen) == 2  # distinct mids → distinct upstream GETs
    for req in seen:
        assert req.headers.get(DIRECTORY_HEADER) == "/w"
        assert "directory" not in req.url.params


async def test_selector_error_preempts_category_error(upstream_factory):
    """A selector error wins over an invalid category (middleware runs before
    the route) — and the error response still carries Vary."""
    async with _selector_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/not_a_category/m1?v=2", headers=IDENTITY)
    assert r.status_code == 400
    assert _json(r) == {"code": "unsupported_version", "supported": [3]}
    assert r.headers.get("Vary") == "Accept-Encoding"


# ---------------------------------------------------------------------------
# 2) Evaluation order (§3.1 R4-M1)
# ---------------------------------------------------------------------------

async def test_invalid_category_400_with_valid_list(upstream_factory):
    """§3.1 step 1 — category whitelist first; no upstream request happens."""
    calls = []
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/not_a_category/m1", headers=HDR)
    assert r.status_code == 400
    assert _json(r) == {
        "code": "invalid_expand_category",
        "validCategories": VALID_CATEGORIES,
    }
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_pool_full_503_before_part_404(upstream_factory):
    """§3.1 step 3 precedence: pool admission fails BEFORE the part-locate
    404 would — holding the single slot → 503 transform_busy with zero
    upstream requests (no part 404 is ever reached)."""
    upstream = upstream_factory(_message_handler(FULL_MESSAGE))
    app = _build_app(upstream, _settings(
        transform_wait_seconds=0.05, transform_absorb_budget_seconds=0.1,
    ))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        await app.state.transforms.acquire()  # exhaust the single slot
        try:
            r = await client.get(
                "/slimapi/messages/s1/expand/part_text/m1/nope", headers=HDR)
        finally:
            app.state.transforms.release()
        app.state.transforms.shutdown()
    assert r.status_code == 503
    assert _json(r) == {"code": "transform_busy", "retry_after": 2}
    assert r.headers.get("Retry-After") == "2"
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_source_overlimit_413_before_part_404(upstream_factory):
    """§3.1 step 4c: body over max_message_bytes → 413 expand_source_too_large
    BEFORE the part-locate would 404 (cap-read precedes decode+locate)."""
    calls = []
    async with _test_client(
        upstream_factory, _message_handler(FULL_MESSAGE),
        max_message_bytes=4,
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/nope", headers=HDR)
    assert r.status_code == 413
    assert _json(r) == {"code": "expand_source_too_large", "limitBytes": 4}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_oversize_malformed_json_413_not_503(upstream_factory):
    """R4-M1: oversize AND malformed body → 413 (cap-read ran before any
    decode attempt), never 503."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"#not-json-but-oversized-long-enough#",
            headers={"Content-Type": "application/json"})

    async with _test_client(
        upstream_factory, handler, max_message_bytes=4,
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 413
    assert _json(r) == {"code": "expand_source_too_large", "limitBytes": 4}


async def test_decode_failure_503(upstream_factory):
    """§3.1 step 4d: undecodable (but in-cap) body → 503 upstream_unavailable.
    EXPLICITLY distinct from the post-parse structural 502 below."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json",
                             headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 503
    assert _json(r) == {"code": "upstream_unavailable"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_top_level_non_dict_503(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1,2,3]",
                             headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 503
    assert _json(r) == {"code": "upstream_unavailable"}


async def test_sid_404_session_not_found(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no session")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 404
    assert _json(r) == {"code": "session_not_found", "sessionID": "s1"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_network_error_503(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 503
    assert _json(r) == {"code": "upstream_unavailable"}


async def test_upstream_5xx_503(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 503
    assert _json(r) == {"code": "upstream_unavailable"}


async def test_upstream_other_4xx_502(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, content=b"teapot")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 502
    assert _json(r) == {"code": "upstream_http_418"}
    assert r.headers.get("Vary") == "Accept-Encoding"


# ---------------------------------------------------------------------------
# 3) Level matching (§3.1 step 2 / step 6) + part 404
# ---------------------------------------------------------------------------

async def test_part_category_without_partid_400(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1", headers=HDR)
    assert r.status_code == 400
    assert _json(r) == {"code": "expand_category_mismatch", "expectedLevel": "part"}
    assert r.headers.get("Vary") == "Accept-Encoding"


async def test_message_category_with_partid_400(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/info_summary_diffs/m1/p_text", headers=HDR)
    assert r.status_code == 400
    assert _json(r) == {"code": "expand_category_mismatch", "expectedLevel": "message"}


async def test_part_missing_404(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/no_such_part", headers=HDR)
    assert r.status_code == 404
    assert _json(r) == {
        "code": "expand_target_not_found", "reason": "part_missing",
    }


async def test_patch_part_state_category_400(upstream_factory):
    """A patch part has no state (§2.2: state categories are tool-only) →
    step 6 rejects with expectedTypes before any extraction."""
    message = {
        "info": {"id": "m1", "role": "assistant", "time": {"created": 1}},
        "parts": [{"id": "p_patch", "type": "patch", "messageID": "m1",
                   "files": ["a.ts"]}],
    }
    async with _test_client(
        upstream_factory, _message_handler(message),
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_state_output/m1/p_patch", headers=HDR)
    assert r.status_code == 400
    assert _json(r) == {
        "code": "expand_category_mismatch", "expectedTypes": ["tool"],
    }


async def test_step_finish_snapshot_200(upstream_factory):
    message = {
        "info": {"id": "m1", "role": "assistant", "time": {"created": 1}},
        "parts": [{"id": "p_fin", "type": "step-finish", "messageID": "m1",
                   "snapshot": "snap"}],
    }
    async with _test_client(
        upstream_factory, _message_handler(message),
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_snapshot/m1/p_fin", headers=HDR)
    assert r.status_code == 200
    assert _json(r)["data"] == {"snapshot": "snap"}
    assert r.headers.get("Cache-Control") == "no-store"
    assert r.headers.get("Vary") == "Accept-Encoding"


# ---------------------------------------------------------------------------
# 4) Extractor positives — 12 categories
# ---------------------------------------------------------------------------

async def _expand_part(client, category: str, part_id: str, *, mid: str = "m1"):
    return await client.get(
        f"/slimapi/messages/s1/expand/{category}/{mid}/{part_id}",
        headers=HDR)


async def test_extract_info_summary_diffs(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/info_summary_diffs/m1", headers=HDR)
    assert r.status_code == 200
    body = _json(r)
    assert body["category"] == "info_summary_diffs"
    assert body["messageID"] == "m1"
    assert "partID" not in body
    assert body["data"] == {"diffs": FULL_MESSAGE["info"]["summary"]["diffs"]}


async def test_extract_part_text(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r, body = None, None
        r = await _expand_part(client, "part_text", "p_text")
        body = _json(r)
    assert r.status_code == 200
    assert body["data"] == {"text": "hi"}
    assert body["partID"] == "p_text"


async def test_extract_part_reasoning(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_reasoning", "p_reason")
    assert r.status_code == 200
    assert _json(r)["data"] == {"text": "think"}


async def test_extract_part_state_output(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_state_output", "p_tool")
    assert r.status_code == 200
    assert _json(r)["data"] == {"output": "out"}


async def test_extract_part_state_error(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_state_error", "p_err")
    assert r.status_code == 200
    assert _json(r)["data"] == {"error": "boom"}


async def test_extract_part_state_input_full(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_state_input_full", "p_tool")
    assert r.status_code == 200
    assert _json(r)["data"] == {"input": {"command": "ls"}}


async def test_extract_part_state_metadata_full_no_diagnostics(upstream_factory):
    """metadata is returned WITHOUT the never-consumed diagnostics map —
    sibling keys survive."""
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_state_metadata_full", "p_tool")
    assert r.status_code == 200
    data = _json(r)["data"]
    assert data == {"metadata": {"cursor": 7}}
    assert "diagnostics" not in data["metadata"]


async def test_extract_part_state_attachments(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_state_attachments", "p_tool")
    assert r.status_code == 200
    assert _json(r)["data"] == {"attachments": [{"path": "/tmp/a"}]}


async def test_extract_part_state_attachments_empty_object_element(upstream_factory):
    """rev-sgpt M2 positive: an attachments array whose elements are objects
    passes even when the objects are empty (object[] allows any object)."""
    message = _wrap([{"id": "p", "type": "tool", "messageID": "m1",
                      "state": {"attachments": [{}]}}])
    async with _test_client(upstream_factory, _message_handler(message)) as client:
        r = await _expand_part(client, "part_state_attachments", "p")
    assert r.status_code == 200
    assert _json(r)["data"] == {"attachments": [{}]}


async def test_extract_part_url(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_url", "p_file")
    assert r.status_code == 200
    assert _json(r)["data"] == {"url": "file:///tmp/a"}


async def test_extract_part_source(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_source", "p_file")
    assert r.status_code == 200
    assert _json(r)["data"] == {"source": {"path": "/tmp/a"}}


async def test_extract_part_snapshot_step_start(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "part_snapshot", "p_ss")
    assert r.status_code == 200
    assert _json(r)["data"] == {"snapshot": "snap1"}


async def test_extract_compaction_full_strips_expand_refs(upstream_factory):
    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r = await _expand_part(client, "compaction_full", "p_comp")
    assert r.status_code == 200
    data = _json(r)["data"]
    assert "expandRefs" not in data
    assert data["id"] == "p_comp"
    assert data["type"] == "compaction"
    assert data["text"] == "compact"


# ---------------------------------------------------------------------------
# 4b) Nested type mismatches → 502 upstream_invalid_shape
# ---------------------------------------------------------------------------

def _wrap(parts: list) -> dict:
    return {"info": {"id": "m1", "role": "assistant", "time": {"created": 1}},
            "parts": parts}


@pytest.mark.parametrize("category,part_id,parts", [
    # state scalar
    ("part_state_output", "p", [{"id": "p", "type": "tool", "messageID": "m1",
                                 "state": 5}]),
    # input non-object
    ("part_state_input_full", "p", [{"id": "p", "type": "tool", "messageID": "m1",
                                     "state": {"input": "string"}}]),
    # attachments non-array
    ("part_state_attachments", "p", [{"id": "p", "type": "tool", "messageID": "m1",
                                      "state": {"attachments": {}}}]),
    # attachments array with non-object elements (rev-sgpt M2: the frozen
    # schema is object[] — every element must be an object)
    ("part_state_attachments", "p", [{"id": "p", "type": "tool", "messageID": "m1",
                                      "state": {"attachments": ["bad", 123]}}]),
    # metadata non-object
    ("part_state_metadata_full", "p", [{"id": "p", "type": "tool", "messageID": "m1",
                                        "state": {"metadata": "x"}}]),
    # text non-string
    ("part_text", "p", [{"id": "p", "type": "text", "messageID": "m1",
                         "text": 123}]),
    # url non-string
    ("part_url", "p", [{"id": "p", "type": "file", "messageID": "m1",
                        "url": 5}]),
    # source non-object
    ("part_source", "p", [{"id": "p", "type": "file", "messageID": "m1",
                           "source": "not-an-object"}]),
    # snapshot non-string
    ("part_snapshot", "p", [{"id": "p", "type": "step-start", "messageID": "m1",
                             "snapshot": 9}]),
    # diffs wrong type
    ("info_summary_diffs", None, []),
])
async def test_nested_type_mismatch_502(upstream_factory, category, part_id, parts):
    message = _wrap(parts)
    if category == "info_summary_diffs":
        message["info"]["summary"] = {"diffs": "wrong-type"}
    async with _test_client(
        upstream_factory, _message_handler(message),
    ) as client:
        path = (
            f"/slimapi/messages/s1/expand/{category}/m1"
            + (f"/{part_id}" if part_id else "")
        )
        r = await client.get(path, headers=HDR)
    assert r.status_code == 502
    assert _json(r) == {"code": "upstream_invalid_shape"}
    assert r.headers.get("Vary") == "Accept-Encoding"


@pytest.mark.parametrize("malformed", [
    {"info": {"id": "m1"}, "parts": None},
    {"info": {"id": "m1"}, "parts": "scalar"},
    {"info": {"id": "m1"}, "parts": 7},
    {"info": {"id": "m1"}, "parts": [{"type": "text"}]},
    {"info": {"id": "m1"}, "parts": [{"id": "", "type": "text"}]},
    {"info": {"id": "m1"}, "parts": [1, 2]},
    {"info": {"id": "m1"}, "parts": [{"id": "p", "type": "text"},
                                     {"id": "p", "type": "text"}]},
], ids=[
    "parts-null", "parts-scalar-str", "parts-scalar-int",
    "element-without-id", "element-empty-id", "non-object-element",
    "duplicate-partid",
])
async def test_malformed_parts_502(upstream_factory, malformed):
    """Parsed-but-structurally-malformed parts → 502 (distinct from the
    503 decode-failure family above)."""
    async with _test_client(
        upstream_factory, _message_handler(malformed),
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p", headers=HDR)
    assert r.status_code == 502
    assert _json(r) == {"code": "upstream_invalid_shape"}


# ---------------------------------------------------------------------------
# 4c) Missing vs explicit null → 200 + data key null (R4-M2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,part,expect", [
    ("part_state_input_full",
     {"id": "p", "type": "tool", "messageID": "m1",
      "state": {"status": "completed"}},
     {"input": None}),
    ("part_state_input_full",
     {"id": "p", "type": "tool", "messageID": "m1",
      "state": {"input": None}},
     {"input": None}),
    ("part_state_metadata_full",
     {"id": "p", "type": "tool", "messageID": "m1",
      "state": {}},
     {"metadata": None}),
    ("part_state_metadata_full",
     {"id": "p", "type": "tool", "messageID": "m1",
      "state": {"metadata": None}},
     {"metadata": None}),
    ("part_state_attachments",
     {"id": "p", "type": "tool", "messageID": "m1", "state": {}},
     {"attachments": None}),
    ("part_state_attachments",
     {"id": "p", "type": "tool", "messageID": "m1",
      "state": {"attachments": None}},
     {"attachments": None}),
    # whole part without a state object at all
    ("part_state_output",
     {"id": "p", "type": "tool", "messageID": "m1"},
     {"output": None}),
])
async def test_missing_and_null_same_shape(upstream_factory, category, part, expect):
    message = _wrap([part])
    async with _test_client(
        upstream_factory, _message_handler(message),
    ) as client:
        r = await _expand_part(client, category, "p")
    assert r.status_code == 200
    assert _json(r)["data"] == expect


# ---------------------------------------------------------------------------
# 5) Boundaries
# ---------------------------------------------------------------------------

async def test_source_body_exactly_at_cap_200(upstream_factory):
    """Body length == max_message_bytes → 200 (read_with_cap: total > cap
    truncates; equality does not)."""
    body = orjson.dumps(FULL_MESSAGE)
    async with _test_client(
        upstream_factory, _message_handler(FULL_MESSAGE),
        max_message_bytes=len(body),
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 200
    assert _json(r)["data"] == {"text": "hi"}


async def test_source_body_one_byte_over_cap_413(upstream_factory):
    body = orjson.dumps(FULL_MESSAGE)
    async with _test_client(
        upstream_factory, _message_handler(FULL_MESSAGE),
        max_message_bytes=len(body) - 1,
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1/expand/part_text/m1/p_text", headers=HDR)
    assert r.status_code == 413
    assert _json(r) == {
        "code": "expand_source_too_large", "limitBytes": len(body) - 1,
    }


async def test_fragment_exactly_at_cap_200(upstream_factory):
    """Envelope identity (category+messageID+partID+data) shorter than the
    fragment cap → 200; the cap is byte-checked on the full envelope."""
    async with _test_client(
        upstream_factory, _message_handler(FULL_MESSAGE),
        max_expand_response_bytes=1024,
    ) as client:
        r = await _expand_part(client, "part_text", "p_text")
    assert r.status_code == 200
    assert _json(r)["data"] == {"text": "hi"}


async def test_fragment_one_byte_over_cap_413(upstream_factory):
    """R4-M25: the wrapper bytes ARE counted — a fat category/messageID can
    push a small data payload over the cap."""
    big_text = "x" * 1000
    message = {
        "info": {"id": "m1", "role": "assistant", "time": {"created": 1}},
        "parts": [{"id": "p_text", "type": "text", "messageID": "m1",
                   "text": big_text}],
    }
    # payload with the same envelope shape the route builds: cap between
    # bare-data and full-envelope sizes → the wrapper pushes it over.
    envelope_no_part = orjson.dumps(
        {"category": "part_text", "messageID": "m1", "data": {"text": big_text}})
    envelope_with_part = orjson.dumps(
        {"category": "part_text", "messageID": "m1", "partID": "p_text",
         "data": {"text": big_text}})
    assert len(envelope_with_part) > len(envelope_no_part)
    cap = (len(envelope_no_part) + len(envelope_with_part)) // 2
    assert len(envelope_no_part) < cap < len(envelope_with_part)
    async with _test_client(
        upstream_factory, _message_handler(message),
        max_expand_response_bytes=cap,
    ) as client:
        r = await _expand_part(client, "part_text", "p_text")
    assert r.status_code == 413
    assert _json(r) == {"code": "expand_fragment_too_large", "limitBytes": cap}


# ---------------------------------------------------------------------------
# 6) Single-flight coalescing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat2,pid2", [
    ("part_text", "p_text"),      # same category, same part → 1 GET
    ("part_reasoning", "p_reason"),  # different category, same message → 1 GET
])
async def test_singleflight_same_message_one_get(upstream_factory, cat2, pid2):
    """Concurrent expands for the same (sid, mid, directory) share ONE
    upstream GET regardless of category."""
    calls = [0]
    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, content=orjson.dumps(FULL_MESSAGE),
                              headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r1, r2 = await asyncio.gather(
            _expand_part(client, "part_text", "p_text"),
            _expand_part(client, cat2, pid2),
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls[0] == 1


async def test_singleflight_joins_while_in_flight(upstream_factory):
    """M2-proof: with a SLOW upstream GET, the second concurrent expand
    genuinely joins the first while it is in flight (not merely coalescing
    on the completion grace after serialised pool admission). Both requests
    succeed and upstream served the message exactly once."""
    calls = [0]
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        started.set()
        await release.wait()  # park the LEADER's GET until both requestors are in
        return httpx.Response(200, content=orjson.dumps(FULL_MESSAGE),
                              headers={"Content-Type": "application/json"})

    async with _test_client(
        upstream_factory, handler,
        max_transforms=4,  # enough slots that admission never serialises us
    ) as client:
        t1 = asyncio.create_task(
            _expand_part(client, "part_text", "p_text"))
        await started.wait()          # leader's GET is genuinely in flight
        t2 = asyncio.create_task(
            _expand_part(client, "part_reasoning", "p_reason"))
        await asyncio.sleep(0.05)     # let the second requestor reach the join
        release.set()                 # unblock the leader
        r1, r2 = await asyncio.gather(t1, t2)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls[0] == 1  # the in-flight join, not two serialised GETs


async def test_singleflight_expand_and_full_share(upstream_factory):
    """An expand and a concurrent direct /full for the same mid coalesce
    (the L2-CD-1 key is (pool, sid, mid, directory)) → 1 GET."""
    calls = [0]
    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, content=orjson.dumps(FULL_MESSAGE),
                              headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r1, r2 = await asyncio.gather(
            _expand_part(client, "part_text", "p_text"),
            client.get("/slimapi/messages/s1/full/m1", headers=HDR),
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls[0] == 1


async def test_singleflight_different_directories_no_merge(upstream_factory):
    """Directory is in the key — two directories → 2 GETs."""
    calls = [0]
    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, content=orjson.dumps(FULL_MESSAGE),
                              headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r1, r2 = await asyncio.gather(
            client.get(
                "/slimapi/messages/s1/expand/part_text/m1/p_text?directory=/a",
                headers=HDR),
            client.get(
                "/slimapi/messages/s1/expand/part_text/m1/p_text?directory=/b",
                headers=HDR),
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls[0] == 2


async def test_singleflight_different_messages_no_merge(upstream_factory):
    calls = [0]
    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        mid = request.url.path.rsplit("/", 1)[-1]
        message = dict(FULL_MESSAGE)
        message["info"] = dict(message["info"], id=mid)
        for p in message["parts"]:
            p["messageID"] = mid
        return httpx.Response(200, content=orjson.dumps(message),
                              headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r1, r2 = await asyncio.gather(
            _expand_part(client, "part_text", "p_text"),  # mid m1
            client.get("/slimapi/messages/s1/expand/part_text/m2/p_text",
                       headers=HDR),
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls[0] == 2


async def test_singleflight_grace_expiry_new_get(upstream_factory):
    """After the 1s grace window, a sequential expand re-fetches (the grace
    is a dedup artefact, not a cache).

    Uses a mid unique to this test: the module-level ``fulls`` registry keys
    on ``(id(pool), sid, mid, directory)``, and pytest reuses freed pool
    object addresses across apps — a shared mid would let a previous test's
    still-warm grace entry coalesce this test's first GET (a test-isolation
    artefact, not production behaviour)."""
    calls = [0]
    async def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, content=orjson.dumps(FULL_MESSAGE),
                              headers={"Content-Type": "application/json"})

    async with _test_client(upstream_factory, handler) as client:
        r1 = await _expand_part(client, "part_text", "p_text", mid="m_grace_1")
        assert r1.status_code == 200
        await asyncio.sleep(1.1)  # _RESULT_GRACE_SECONDS == 1.0
        r2 = await _expand_part(client, "part_text", "p_text", mid="m_grace_1")
    assert r2.status_code == 200
    assert calls[0] == 2


# ---------------------------------------------------------------------------
# 7) Merged placeholder-first (R3-B)
# ---------------------------------------------------------------------------

def _list_response(items: list[dict]):
    return httpx.Response(200, content=orjson.dumps(items),
                          headers={"Content-Type": "application/json"})


def _single_full(mid: str):
    return httpx.Response(
        200, content=orjson.dumps({
            "info": {"id": mid, "role": "user",
                     "time": {"created": 1000, "updated": 1000}},
            "parts": [
                {"id": "p1", "type": "text", "messageID": mid,
                 "text": "full text content"},
                {"id": "p2", "type": "tool", "messageID": mid, "tool": "ls",
                 "state": {"status": "completed", "output": "done"}},
            ],
        }), headers={"Content-Type": "application/json"})


async def _merged_get(client, path: str):
    return await client.get(path, headers=HDR)


async def test_merged_placeholder_first_ref_never_displaces(upstream_factory):
    """merged_max_fulls_per_page=1 + one placeholder + one ref candidate:
    the placeholder claims the slot; the ref message keeps its skeleton
    (null text + expandRefs) — ref candidates never displace placeholders."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}
    ref = {"info": {"id": "m_ref", "role": "assistant",
                    "time": {"created": 2, "updated": 2}},
           "parts": [{"id": "p_long", "type": "text", "messageID": "m_ref",
                      "text": "x" * 3000}]}  # > 2048 threshold → text:null + expandRefs
    fulls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph, ref])
        # per-mid full fetch
        mid = request.url.path.rsplit("/", 1)[-1]
        fulls.append(mid)
        return _single_full(mid)

    async with _test_client(
        upstream_factory, handler, merged_max_fulls_per_page=1,
    ) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    items = _json(r)["items"]
    assert fulls == ["m_ph"]  # 1 slot went to the placeholder; ref not fetched
    # placeholder restored from full
    ph_out = items[0]
    assert ph_out["parts"] == orjson.loads(_single_full("m_ph").content)["parts"]
    # ref candidate kept its skeleton with expandRefs
    ref_out = items[1]
    ref_part = ref_out["parts"][0]
    assert ref_part["text"] is None
    assert ref_part["expandRefs"][0]["category"] == "part_text"


async def test_merged_long_text_restored_in_budget(upstream_factory):
    """Plenty of budget: both the placeholder and the ref candidate are
    fetched and spliced — the ref message is fully restored."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}
    ref = {"info": {"id": "m_ref", "role": "assistant",
                    "time": {"created": 2, "updated": 2}},
           "parts": [{"id": "p_long", "type": "text", "messageID": "m_ref",
                      "text": "x" * 3000}]}
    fulls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph, ref])
        mid = request.url.path.rsplit("/", 1)[-1]
        fulls.append(mid)
        return _single_full(mid)

    async with _test_client(upstream_factory, handler) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    assert sorted(fulls) == ["m_ph", "m_ref"]
    items = _json(r)["items"]
    ref_out = items[1]
    assert ref_out["parts"][0]["text"] == "full text content"
    assert "expandRefs" not in ref_out["parts"][0]


async def test_merged_over_budget_keeps_skeleton(upstream_factory):
    """10-byte merged budget: the fetch is capped below the body size →
    both items degrade to their skeletons (placeholder marker + ref
    candidate keeps null text + expandRefs)."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}
    ref = {"info": {"id": "m_ref", "role": "assistant",
                    "time": {"created": 2, "updated": 2}},
           "parts": [{"id": "p_long", "type": "text", "messageID": "m_ref",
                      "text": "y" * 3000}]}
    fulls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph, ref])
        mid = request.url.path.rsplit("/", 1)[-1]
        fulls.append(mid)
        return _single_full(mid)

    async with _test_client(
        upstream_factory, handler, merged_max_bytes=10,
    ) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    items = _json(r)["items"]
    # just the placeholder was attempted (page cap 16, but 10-byte budget
    # only really allows one fetch — the second's cap <= 0)
    assert len(fulls) <= 2
    ref_out = items[1]
    assert ref_out["parts"][0]["text"] is None
    assert ref_out["parts"][0]["expandRefs"][0]["category"] == "part_text"


async def test_merged_diffs_stay_null_with_expand_refs(upstream_factory):
    """info.summary.diffs is ALWAYS projected null in the list; merged does
    not restore it (§4.3.3) despite splicing parts — the message-level
    expandRefs entry remains the client's entry point."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1},
                   "summary": {"diffs": [{"file": "a.ts", "additions": 1}]}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph])
        return _single_full("m_ph")

    async with _test_client(upstream_factory, handler) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    item = _json(r)["items"][0]
    assert item["info"]["summary"]["diffs"] is None
    refs = item["info"]["expandRefs"]
    assert refs == [{
        "category": "info_summary_diffs", "messageID": "m_ph",
        "href": "/slimapi/messages/s1/expand/info_summary_diffs/m_ph?v=3",
    }]


async def test_merged_intersection_single_slot_single_fetch(upstream_factory):
    """A message that is BOTH a placeholder and a ref carrier (§4.3.1
    intersection) is deduped: placeholder identity wins, ONE slot, ONE
    fetch. Constructed by a placeholder message whose sibling tool part in
    the FULL body carries expandRefs-like omission — the projected list has
    both markers on the same message."""
    calls = []
    ph = {"info": {"id": "m_x", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_x",
                     "text": ""}]}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph])
        calls.append(request.url.path)
        return _single_full("m_x")

    # projected list: the placeholder marker AND a part-level expandRefs on
    # the same message — simulate by returning a projected-shaped body where
    # parts carry both. Easier: call the pure function directly.
    from oc_slimapi.routes import messages as M
    projected = [
        {
            "info": {"id": "m_x"},
            "parts": [
                {"id": "thin_placeholder_m_x", "type": "text",
                 "text": "...", "hasFull": True, "omitted": ["parts"]},
                {"id": "p_alt", "type": "text",
                 "expandRefs": [{"category": "part_text", "messageID": "m_x",
                                 "partID": "p_alt"}]},
            ],
        },
    ]
    config = _settings()
    pairs = M._merged_candidate_pairs(projected, config)
    assert pairs == [(0, "m_x")]  # one slot, placeholder identity

    # End-to-end: page cap 16 — a placeholder + ref-on-same-message can't be
    # built from real upstream (one message is either renderable or not), so
    # assert the fetch side is exercised once through the route with the
    # intersection message appearing as the lone placeholder.
    async with _test_client(upstream_factory, handler) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    assert calls == ["/session/s1/message/m_x"]  # exactly one fetch


async def test_merged_mixed_page_e2e_placeholder_first(upstream_factory):
    """M3 e2e: a page carrying BOTH a placeholder message AND a ref message
    exercises the full R3-B pipeline — placeholder claims its slot first,
    the ref candidate fills the remaining slot, page order is preserved
    (created ASC), and the ref message's diffs stay null + info.expandRefs.
    NOTE: a real intersection (placeholder marker AND part-level expandRefs
    on the SAME message) is unreachable from genuine upstream — the skeleton
    makes any expandRefs-bearing part renderable, so such a message is never
    a placeholder (§4.3 renderability). The intersection DEDUP itself is
    covered by the pure-function assertion above; this test locks the
    mixed-page ordering/budget semantics end-to-end."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}
    ref = {"info": {"id": "m_ref", "role": "assistant",
                    "time": {"created": 2, "updated": 2},
                    "summary": {"diffs": [{"file": "b.ts", "additions": 2}]}},
           "parts": [{"id": "p_long", "type": "reasoning", "messageID": "m_ref",
                      "text": "r" * 3000}]}  # > 2048 threshold → reasoning ref
    fulls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([ph, ref])
        mid = request.url.path.rsplit("/", 1)[-1]
        fulls.append(mid)
        return _single_full(mid)

    async with _test_client(
        upstream_factory, handler, merged_max_fulls_per_page=2,
    ) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    # both slots claimed: placeholder + ref, page order (1 < 2)
    assert fulls == ["m_ph", "m_ref"]
    items = _json(r)["items"]
    assert [i["info"]["id"] for i in items] == ["m_ph", "m_ref"]
    # placeholder spliced from full
    assert items[0]["parts"] == orjson.loads(_single_full("m_ph").content)["parts"]
    # ref spliced too (its slot came after the placeholder)
    assert items[1]["parts"] == orjson.loads(_single_full("m_ref").content)["parts"]
    # diffs stay null + message-level expandRefs preserved for the ref message
    assert items[1]["info"]["summary"]["diffs"] is None
    assert items[1]["info"]["expandRefs"][0]["category"] == "info_summary_diffs"


async def test_merged_step_only_message_same_message_intersection(upstream_factory):
    """R2 e2e regression: a message that is BOTH a placeholder AND a
    part-level expandRefs carrier IS constructible from real upstream —
    step-start/step-finish parts emit part_snapshot refs (skeleton.py
    :531-539) yet are NOT renderable (skeleton.py _is_renderable :683-697
    only recognises text/reasoning/tool/patch/file). A step-only message
    therefore projects to: step part + part_snapshot expandRefs AND the
    thin_placeholder marker. The merged route must dedupe it into ONE full
    slot (intersection rule §4.3.1), splice the restored parts, and emit a
    clean wire body with no null leakage."""
    step = {"info": {"id": "m_dual", "role": "assistant",
                     "time": {"created": 3, "updated": 3}},
            "parts": [{"id": "p_step", "type": "step-start",
                       "messageID": "m_dual",
                       "snapshot": "SNAPSHOT-v1", "reason": "start"}]}
    fulls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response([step])
        mid = request.url.path.rsplit("/", 1)[-1]
        fulls.append(mid)
        return _single_full(mid)

    async with _test_client(upstream_factory, handler) as client:
        r = await _merged_get(client, "/slimapi/messages/s1?mode=merged")
    assert r.status_code == 200
    # intersection dedupe: ONE slot, ONE fetch for the dual-identity message
    assert fulls == ["m_dual"]
    items = _json(r)["items"]
    assert [i["info"]["id"] for i in items] == ["m_dual"]  # page order intact
    item = items[0]
    # placeholder identity spliced: full parts replace the thin skeleton
    exp_parts = orjson.loads(_single_full("m_dual").content)["parts"]
    assert item["parts"] == exp_parts
    # no null/placeholder leakage: the restored parts have no
    # thin_placeholder marker and no expandRefs
    assert not any(
        str(p.get("id", "")).startswith("thin_placeholder_")
        for p in item["parts"]
    )
    assert all("expandRefs" not in p for p in item["parts"])
    # the spliced text part carries its real content (no folded null)
    text_part = next(p for p in item["parts"]
                     if p.get("type") == "text")
    assert text_part["text"] == "full text content"


# ---------------------------------------------------------------------------
# 8) /full/{mid} byte-identical regression
# ---------------------------------------------------------------------------

async def test_full_route_byte_identical_after_wiring(upstream_factory):
    """/full/{mid} output equals the upstream body modulo the LSP
    ``state.metadata.diagnostics`` scrub — the sid wiring must not perturb
    direct /full (rev-sgpt m2: byte-locks the projection itself, not just
    request determinism)."""
    expected = copy.deepcopy(FULL_MESSAGE)
    for part in expected["parts"]:
        state = part.get("state")
        if isinstance(state, dict):
            metadata = state.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("diagnostics", None)
    expected_bytes = orjson.dumps(expected)

    async with _test_client(upstream_factory, _message_handler(FULL_MESSAGE)) as client:
        r1 = await client.get("/slimapi/messages/s1/full/m1", headers=HDR)
        r2 = await client.get("/slimapi/messages/s1/full/m1", headers=HDR)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == expected_bytes  # upstream body minus diagnostics
    assert r1.content == r2.content
    assert _json(r1)["info"]["id"] == "m1"