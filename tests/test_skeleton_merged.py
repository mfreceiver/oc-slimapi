"""§5c.8 (plan v2.1): merged-view regression for the 4.9.0 derived fields.

P1-8 ruling: derived fields (§2 title / §3 outputBytes / §4a normalization /
§4b compact files / §4d synthetic files+diffStats) are **skeleton-view only**.
``mode=merged`` splices the upstream FULL parts back in (diagnostics
stripped, everything else verbatim) — the merged view restores the upstream
original: NO synthesized ``files`` / ``diffStats`` / ``outputBytes`` / title.
``_full_merge.py`` itself is untouched by the feature (zero-change ruling).

Three lanes:

* **successful splice** — an edit message whose skeleton carries synthetic
  ``metadata.files`` + ``diffStats`` (and the ``part_state_metadata_full``
  expandRef that makes it a merged candidate) is served with the upstream
  full parts verbatim: raw ``metadata.diff`` back, no derived keys.
* **budget-skip** — page cap 0 (progressive degrade): the item keeps its
  SKELETON projection, synthetic fields and all.
* **full-fetch failure** — upstream 500 on the detail: same skeleton keep.

Settings pinned explicitly (mirrors test_messages_merged.py) so assertions
are isolated from the developer's environment.
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
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}

LIST_LINK = (
    '<http://127.0.0.1:4096/session/s1/message?before=CURSOR123&limit=40>; '
    'rel="next"'
)

EDIT_DIFF = (
    "--- src/a.ts\t2026-08-21 10:00:00.000000000 +0800\n"
    "+++ src/a.ts\t2026-08-21 10:00:01.000000000 +0800\n"
    "@@ -1,3 +1,3 @@\n"
    " context\n"
    "-old line\n"
    "+new line\n"
)

# Upstream LIST body: a completed edit tool part. Its skeleton carries the
# §4d synthetic metadata.files/diffStats AND a part_state_metadata_full
# expandRef (``diff`` is non-whitelist omitted) — the expandRef is what makes
# this message a merged candidate.
MSG_EDIT = {
    "info": {"id": "msg_e", "role": "assistant",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "p_edit", "type": "tool", "messageID": "msg_e",
         "tool": "edit",
         "state": {"status": "completed",
                   "input": {"filePath": "src/a.ts"},
                   "metadata": {
                       "diff": EDIT_DIFF,
                       "diagnostics": {"lang": "ts", "items": [{"msg": "x"}]},
                   }}},
    ],
}

# Upstream FULL body: same edit part, diagnostics stripped — the exact parts
# the merged splice must restore (raw diff, NO synthetic files/diffStats).
FULL_MSG_EDIT_PARTS_STRIPPED = [
    {"id": "p_edit", "type": "tool", "messageID": "msg_e",
     "tool": "edit",
     "state": {"status": "completed",
               "input": {"filePath": "src/a.ts"},
               "metadata": {"diff": EDIT_DIFF}}},
]

# A compress-tool message exercising §2/§3 in the same page (title synth +
# outputBytes hint in skeleton; merged restores upstream title-less state).
COMPRESS_OUTPUT = "x" * 12000          # > 4 KiB field cap → omitted + hint
MSG_COMPRESS = {
    "info": {"id": "msg_c", "role": "assistant",
             "time": {"created": 1001, "updated": 1001}},
    "parts": [
        {"id": "p_comp", "type": "tool", "messageID": "msg_c",
         "tool": "compress",
         "state": {"status": "completed",
                   "input": {"content": [{"topic": "graph work"}]},
                   "output": COMPRESS_OUTPUT}},
    ],
}
FULL_MSG_COMPRESS_PARTS_STRIPPED = [
    {"id": "p_comp", "type": "tool", "messageID": "msg_c",
     "tool": "compress",
     "state": {"status": "completed",
               "input": {"content": [{"topic": "graph work"}]},
               "output": COMPRESS_OUTPUT}},
]


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        transform_absorb_budget_seconds=1.0,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        merged_fanout=8,
        merged_max_fulls_per_page=16,
        merged_max_bytes=8 * 1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, settings: Settings) -> FastAPI:
    app = FastAPI(title="oc-slimapi-merged-synth-test")
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


def _list_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, content=orjson.dumps(items),
        headers={"Content-Type": "application/json", "Link": LIST_LINK},
    )


def _items(resp) -> dict:
    assert resp.status_code == 200
    return {item["info"]["id"]: item for item in resp.json()["items"]}


def _skeleton_edit_part(item: dict) -> dict:
    return next(p for p in item["parts"] if p.get("id") == "p_edit")


# ---------------------------------------------------------------------------
# Lane 1 — successful splice: merged restores the upstream original.
# ---------------------------------------------------------------------------

async def test_merged_splice_restores_upstream_original_no_derived_fields(
        upstream_factory):
    """The edit message's SKELETON carries the synthetic fields; the MERGED
    response must NOT — parts are the upstream full verbatim (diagnostics
    stripped), i.e. the raw metadata.diff with no files/diffStats/outputBytes
    and no synthesized compress title (P1-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_EDIT, MSG_COMPRESS])
        if path == "/session/s1/message/msg_e":
            return httpx.Response(200, content=orjson.dumps(
                {"info": MSG_EDIT["info"],
                 "parts": FULL_MSG_EDIT_PARTS_STRIPPED}))
        if path == "/session/s1/message/msg_c":
            return httpx.Response(200, content=orjson.dumps(
                {"info": MSG_COMPRESS["info"],
                 "parts": FULL_MSG_COMPRESS_PARTS_STRIPPED}))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        plain = await client.get("/slimapi/messages/s1", headers=HDR)
        merged = await client.get(
            "/slimapi/messages/s1?mode=merged", headers=HDR)

    # Sanity — the default skeleton DOES carry the derived fields.
    sk_edit = _skeleton_edit_part(_items(plain)["msg_e"])
    sk_meta = sk_edit["state"]["metadata"]
    assert sk_meta["files"] == [
        {"path": "src/a.ts", "additions": 1, "deletions": 1}]
    assert sk_meta["diffStats"] == {
        "additions": 1, "deletions": 1, "files": 1}
    sk_comp = next(
        p for p in _items(plain)["msg_c"]["parts"] if p.get("id") == "p_comp")
    assert sk_comp["state"]["title"] == "graph work"          # §2 synth
    assert sk_comp["state"]["outputBytes"] == len(             # §3 hint
        orjson.dumps(COMPRESS_OUTPUT))
    assert "output" not in sk_comp["state"]

    # Merged — upstream original restored, zero derived fields.
    m_items = _items(merged)
    assert m_items["msg_e"]["parts"] == FULL_MSG_EDIT_PARTS_STRIPPED
    m_edit_meta = m_items["msg_e"]["parts"][0]["state"]["metadata"]
    assert m_edit_meta == {"diff": EDIT_DIFF}
    assert "files" not in m_edit_meta
    assert "diffStats" not in m_edit_meta
    assert "omitted" not in m_items["msg_e"]["parts"][0]

    m_comp = next(
        p for p in m_items["msg_c"]["parts"] if p.get("id") == "p_comp")
    assert m_comp["state"]["output"] == COMPRESS_OUTPUT        # full output
    assert "outputBytes" not in m_comp["state"]
    assert "title" not in m_comp["state"]                      # no §2 synth


# ---------------------------------------------------------------------------
# Lane 2 — budget-skip: page cap 0 keeps the skeleton (synthetic fields
# present). Progressive degrade, never an error.
# ---------------------------------------------------------------------------

async def test_merged_budget_skip_keeps_skeleton_with_derived_fields(
        upstream_factory):
    calls = {"full": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_EDIT])
        if path == "/session/s1/message/msg_e":
            calls["full"] += 1
            return httpx.Response(200, content=orjson.dumps(
                {"info": MSG_EDIT["info"],
                 "parts": FULL_MSG_EDIT_PARTS_STRIPPED}))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
            upstream_factory, handler, merged_max_fulls_per_page=0) as client:
        merged = await client.get(
            "/slimapi/messages/s1?mode=merged", headers=HDR)

    assert calls["full"] == 0                       # cap 0 → no fan-out
    edit = _skeleton_edit_part(_items(merged)["msg_e"])
    meta = edit["state"]["metadata"]
    # The kept SKELETON still carries the synthetic projection.
    assert meta["files"] == [
        {"path": "src/a.ts", "additions": 1, "deletions": 1}]
    assert meta["diffStats"] == {
        "additions": 1, "deletions": 1, "files": 1}
    assert edit["hasFull"] is True
    assert "state.metadata.diff" in edit["omitted"]


# ---------------------------------------------------------------------------
# Lane 3 — full-fetch failure: the item degrades to its skeleton (synthetic
# fields present); the page stays 200.
# ---------------------------------------------------------------------------

async def test_merged_full_fetch_failure_keeps_skeleton(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_EDIT])
        if path == "/session/s1/message/msg_e":
            return httpx.Response(500, content=b"boom")
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
            upstream_factory, handler, transform_wait_seconds=0.05,
            transform_absorb_budget_seconds=0.1) as client:
        merged = await client.get(
            "/slimapi/messages/s1?mode=merged", headers=HDR)

    edit = _skeleton_edit_part(_items(merged)["msg_e"])
    meta = edit["state"]["metadata"]
    assert meta["files"] == [
        {"path": "src/a.ts", "additions": 1, "deletions": 1}]
    assert meta["diffStats"] == {
        "additions": 1, "deletions": 1, "files": 1}
