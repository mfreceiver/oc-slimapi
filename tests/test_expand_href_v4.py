"""V4 formal revision §14 — expandRefs href generated per wire view.

v4-contract §14 (2026-08-19 frozen): every ``expandRefs`` href carries the
REQUEST's selector view — v4 requests emit ``?v=4`` (the v3 wire face was
retired with the (4,4) window; requests with a retired/unknown selector
are rejected by the selector middleware). This file locks:

* **v4 byte regression** — href is byte-identical ``?v=4`` (route-level
  rawbody assertions, not just parsed-field equality);
* **v4 fork** — ``?v=4`` in the messages list default mode AND
  ``mode=merged`` (message-level ``info_summary_diffs`` + part-level refs);
* **query key order** (§14 frozen) — ``v`` is the FIRST and, from the
  sidecar, ONLY query key; the client appends ``directory`` second; a
  sidecar-emitted href therefore never contains ``&``;
* **dedup / sort invariance** — expandRefs dedup + (category, partID) sort
  semantics are view-invariant (inherited unchanged from v3 §4a);
* **selector-less default v3** — stacks without the selector middleware
  keep the historical v3 hrefs;
* **expand endpoint bytes** — the /expand route renders the same frozen
  envelope on the v4 face (§14 inherits v3 §4b).

Pure-function tests thread ``wire_view`` explicitly; route tests go through
``SlimapiSelectorMiddleware`` with the ``?v=4`` selector.
"""
from __future__ import annotations

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
from oc_slimapi.skeleton import REASONING_INLINE_MAX_BYTES, skeleton_messages
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}

# ---------------------------------------------------------------------------
# Pure-function level (oc_slimapi.skeleton)
# ---------------------------------------------------------------------------

SID = "ses_s"


def _reasoning_part(text, pid="prt", mid="m1"):
    return {"id": pid, "type": "reasoning", "messageID": mid, "text": text}


def _rich_message(mid="m1", created=1000):
    """One message carrying a message-level ref AND part-level refs
    (reasoning / tool / file) — the full href surface in one projection."""
    return {
        "info": {
            "id": mid, "role": "assistant",
            "time": {"created": created, "updated": created},
            "summary": {"diffs": [{"file": "a.ts", "additions": 1,
                                   "deletions": 0}]},
        },
        "parts": [
            _reasoning_part(
                "x" * (REASONING_INLINE_MAX_BYTES + 1), mid=mid,
            ),
            {"id": "p_tool", "type": "tool", "messageID": mid,
             "tool": "bash",
             "state": {"status": "completed", "title": "t",
                       # two non-whitelist keys → ONE part_state_input_full
                       "input": {"command": "ls", "secret_key": "v",
                                 "other": "w"},
                       # two non-whitelist keys → ONE part_state_metadata_full
                       "metadata": {"sessionId": "s", "foreign": {"x": 1},
                                    "extra": 2},
                       "output": "o" * 5000,  # > field cap → part_state_output
                       "attachments": [{"mime": "text/plain",
                                        "url": "data:x"}]}},
            {"id": "p_file", "type": "file", "messageID": mid,
             "url": "data:image/png;base64,AAAA",
             "source": {"type": "file", "path": "a.ts"}},
        ],
    }


def _collect_hrefs(projected: list[dict]) -> list[str]:
    hrefs: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, dict):
            if "href" in node and isinstance(node["href"], str):
                hrefs.append(node["href"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(projected)
    return hrefs


def _assert_query_order_frozen(href: str, view: int) -> None:
    """§14: ``v`` is the first (and only sidecar-emitted) query key."""
    assert href.count("?") == 1
    query = href.split("?", 1)[1]
    assert query == f"v={view}"  # no '&', no other keys, `v` first


def test_projection_default_view_keeps_frozen_v3_bytes():
    """No wire_view passed → the pure functions keep their historical
    output: every href ends ``?v=3`` (byte-for-byte the v3 wire shape)."""
    out = skeleton_messages([_rich_message()], sid=SID)[0]
    hrefs = _collect_hrefs([out])
    assert len(hrefs) == 8  # 1 message-level + 4 tool + 2 file + 1 reasoning
    for href in hrefs:
        _assert_query_order_frozen(href, 3)
    assert out["info"]["expandRefs"] == [{
        "category": "info_summary_diffs",
        "messageID": "m1",
        "href": f"/slimapi/messages/{SID}/expand/info_summary_diffs/m1?v=3",
    }]


def test_projection_v4_view_swaps_all_hrefs():
    """wire_view=4 → every message-level AND part-level href is ``?v=4``."""
    out = skeleton_messages([_rich_message()], sid=SID, wire_view=4)[0]
    hrefs = _collect_hrefs([out])
    assert len(hrefs) == 8
    for href in hrefs:
        _assert_query_order_frozen(href, 4)
        assert href.endswith("?v=4")
    assert out["info"]["expandRefs"] == [{
        "category": "info_summary_diffs",
        "messageID": "m1",
        "href": f"/slimapi/messages/{SID}/expand/info_summary_diffs/m1?v=4",
    }]


def test_projection_dedup_and_sort_invariant_under_v4():
    """§14 inherits v3 §4a: dedup (multi-key collapse) + (category, partID)
    sort are IDENTICAL under v4 — only the ``?v=`` value differs.

    B12: both views are pinned to the SAME literal refs-shape golden —
    the invariance property follows transitively without evaluating the
    v4 expectation off a live v3 result (v3 half kept as guard net)."""
    golden = {
        "message": [{"category": "info_summary_diffs", "messageID": "m1"}],
        "parts": [
            ("prt", [{"category": "part_reasoning", "messageID": "m1",
                      "partID": "prt"}]),
            ("p_tool", [
                {"category": "part_state_attachments", "messageID": "m1",
                 "partID": "p_tool"},
                {"category": "part_state_input_full", "messageID": "m1",
                 "partID": "p_tool"},
                {"category": "part_state_metadata_full", "messageID": "m1",
                 "partID": "p_tool"},
                {"category": "part_state_output", "messageID": "m1",
                 "partID": "p_tool"},
            ]),
            ("p_file", [
                {"category": "part_source", "messageID": "m1",
                 "partID": "p_file"},
                {"category": "part_url", "messageID": "m1",
                 "partID": "p_file"},
            ]),
        ],
    }

    def _refs_shape(msg: dict):
        def _strip(nodes):
            return [
                {"category": r["category"], "messageID": r["messageID"],
                 **({"partID": r["partID"]} if "partID" in r else {})}
                for r in nodes
            ]
        return {
            "message": _strip(msg["info"]["expandRefs"]),
            "parts": [
                (p["id"], _strip(p["expandRefs"]))
                for p in msg["parts"] if "expandRefs" in p
            ],
        }

    v4 = skeleton_messages([_rich_message()], sid=SID, wire_view=4)[0]
    assert _refs_shape(v4) == golden

    tool = v4["parts"][1]["expandRefs"]
    cats = [r["category"] for r in tool]
    # dedup: input/metadata collapsed to one ref each → 4 distinct, sorted
    assert cats == ["part_state_attachments", "part_state_input_full",
                    "part_state_metadata_full", "part_state_output"]
    file_refs = v4["parts"][2]["expandRefs"]
    assert [r["category"] for r in file_refs] == ["part_source", "part_url"]
    for r in tool + file_refs + v4["parts"][0]["expandRefs"]:
        assert r["href"].endswith("?v=4")

    # v3 守护网（Phase 4 拆除前保留）：默认视图（v3）同形状——href 侧
    # 由 test_projection_default_view_keeps_frozen_v3_bytes 字节锁定。
    v3 = skeleton_messages([_rich_message()], sid=SID)[0]
    assert _refs_shape(v3) == golden


def test_projection_without_sid_emits_no_refs_either_view():
    """Without sid no href can be built — refs are dropped under BOTH views
    (the reductions themselves still apply; view never leaks a href)."""
    for view in (3, 4):
        out = skeleton_messages(
            [_rich_message()], sid=None, wire_view=view,
        )[0]
        assert _collect_hrefs([out]) == []
        assert "expandRefs" not in out["info"]


# ---------------------------------------------------------------------------
# Route level (SlimapiSelectorMiddleware + /slimapi/messages/{sid})
# ---------------------------------------------------------------------------

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
    app = FastAPI(title="oc-slimapi-expand-href-v4-test")
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


def _list_response(items: list[dict]):
    return httpx.Response(200, content=orjson.dumps(items),
                          headers={"Content-Type": "application/json"})


def _single_full(mid: str):
    return httpx.Response(
        200, content=orjson.dumps({
            "info": {"id": mid, "role": "user",
                     "time": {"created": 1000, "updated": 1000}},
            "parts": [{"id": "p_full", "type": "text", "messageID": mid,
                       "text": "full text content"}],
        }), headers={"Content-Type": "application/json"})


@pytest.fixture(autouse=True)
def _cleanup_global_singleflight():
    """Isolate the module-level singleflight registry between tests
    (mirrors tests/test_expand_routes.py)."""
    yield
    messages.fulls.shutdown()


@asynccontextmanager
async def _route_client(upstream_factory, handler, *, selector=True,
                        **overrides):
    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(**overrides))
    if selector:
        app.add_middleware(SlimapiSelectorMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        try:
            yield client
        finally:
            app.state.transforms.shutdown()


def _list_handler(items: list[dict], fulls: list[str] | None = None):
    """Upstream handler serving the messages LIST plus per-mid fulls."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message":
            return _list_response(items)
        if fulls is not None:
            fulls.append(request.url.path.rsplit("/", 1)[-1])
        return _single_full(request.url.path.rsplit("/", 1)[-1])
    return handler


async def test_route_v4_rawbody_href_bytes_frozen(upstream_factory):
    """§14 v4 byte regression: with the ``?v=4`` selector, the raw body
    carries the exact frozen v4 hrefs and NEVER a ``?v=3`` byte."""
    async with _route_client(
        upstream_factory, _list_handler([_rich_message()]),
    ) as client:
        r = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert b'"href":"/slimapi/messages/s1/expand/info_summary_diffs/m1?v=4"' \
        in r.content
    assert b'"href":"/slimapi/messages/s1/expand/part_reasoning/m1/prt?v=4"' \
        in r.content
    assert b'"href":"/slimapi/messages/s1/expand/part_state_output/m1/p_tool?v=4"' \
        in r.content
    assert b'"href":"/slimapi/messages/s1/expand/part_source/m1/p_file?v=4"' \
        in r.content
    assert b"?v=3" not in r.content


async def test_route_v4_default_mode_all_hrefs_v4(upstream_factory):
    """§14: ``?v=4`` selector → message-level + part-level hrefs all carry
    ``?v=4`` in the default list mode; not a single ``?v=3`` byte leaks."""
    async with _route_client(
        upstream_factory, _list_handler([_rich_message()]),
    ) as client:
        r = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
    assert r.status_code == 200
    items = orjson.loads(r.content)["items"]
    item = items[0]
    assert item["info"]["expandRefs"] == [{
        "category": "info_summary_diffs", "messageID": "m1",
        "href": "/slimapi/messages/s1/expand/info_summary_diffs/m1?v=4",
    }]
    hrefs = _collect_hrefs(items)
    assert len(hrefs) == 8
    for href in hrefs:
        _assert_query_order_frozen(href, 4)
    assert b"?v=3" not in r.content


async def test_route_v4_merged_message_level_href(upstream_factory):
    """§14 list/merged consistency: merged splices the fetched message's
    parts (part refs replaced by fulls) but the message-level
    ``info_summary_diffs`` ref survives with the request's view."""
    async with _route_client(
        upstream_factory, _list_handler([_rich_message()]),
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1?mode=merged&v=4", headers=IDENTITY,
        )
    assert r.status_code == 200
    item = orjson.loads(r.content)["items"][0]
    assert item["info"]["summary"]["diffs"] is None  # never restored
    assert item["info"]["expandRefs"] == [{
        "category": "info_summary_diffs", "messageID": "m1",
        "href": "/slimapi/messages/s1/expand/info_summary_diffs/m1?v=4",
    }]
    assert b"?v=3" not in r.content


async def test_route_v4_merged_part_level_href_survives_unspliced(
    upstream_factory,
):
    """Budget-capped merged (placeholder claims the single slot): the ref
    message keeps its skeleton part with a part-level ``?v=4`` href
    (placeholder-first, mirrors the R3-B contract test)."""
    ph = {"info": {"id": "m_ph", "role": "user",
                   "time": {"created": 1, "updated": 1}},
          "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
                     "text": ""}]}
    ref_msg = _rich_message(mid="m_ref", created=2)
    async with _route_client(
        upstream_factory, _list_handler([ph, ref_msg]),
        merged_max_fulls_per_page=1,
    ) as client:
        r = await client.get(
            "/slimapi/messages/s1?mode=merged&v=4", headers=IDENTITY,
        )
    assert r.status_code == 200
    items = orjson.loads(r.content)["items"]
    by_id = {item["info"]["id"]: item for item in items}
    # placeholder was fetched + spliced; the ref message kept its skeleton
    assert by_id["m_ph"]["parts"][0]["text"] == "full text content"
    ref_part = by_id["m_ref"]["parts"][0]
    assert ref_part["text"] is None
    assert ref_part["expandRefs"] == [{
        "category": "part_reasoning", "messageID": "m_ref",
        "partID": "prt",
        "href": "/slimapi/messages/s1/expand/part_reasoning/m_ref/prt?v=4",
    }]
    assert b"?v=3" not in r.content


async def test_route_selectorless_stack_defaults_to_v4(upstream_factory):
    """Selector-less stack (no middleware, direct route invocation):
    ``wire_view_from_scope`` is constant 4 (V2b default flip) — v4 hrefs."""
    async with _route_client(
        upstream_factory, _list_handler([_rich_message()]), selector=False,
    ) as client:
        r = await client.get("/slimapi/messages/s1", headers=IDENTITY)
    assert r.status_code == 200
    assert b'"href":"/slimapi/messages/s1/expand/info_summary_diffs/m1?v=4"' \
        in r.content
    assert b"?v=3" not in r.content


async def test_expand_endpoint_v4_envelope_bytes_frozen(upstream_factory):
    """§14 inherits v3 §4b: the /expand endpoint renders the frozen
    envelope bytes + no-store on the v4 face (it never renders an href,
    so the view question does not arise)."""
    single = {
        "info": {"id": "m1", "role": "assistant",
                 "summary": {"diffs": [{"file": "a.ts", "additions": 1}]}},
        "parts": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps(single),
            headers={"Content-Type": "application/json"},
        )

    async with _route_client(upstream_factory, handler) as client:
        r4 = await client.get(
            "/slimapi/messages/s1/expand/info_summary_diffs/m1?v=4",
            headers=IDENTITY,
        )
    assert r4.status_code == 200
    assert r4.content == orjson.dumps({
        "category": "info_summary_diffs", "messageID": "m1",
        "data": {"diffs": [{"file": "a.ts", "additions": 1}]},
    })
    assert r4.headers.get("Cache-Control") == "no-store"
