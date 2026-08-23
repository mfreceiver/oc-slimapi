"""B2 compat verification: [3.2.0] TextPart.text is ALWAYS inlined verbatim.

Owner decision 2026-08-17 (contract 3.2.0): TextPart.text is never folded —
no size threshold, any byte count is inlined as-is, including parts with a
missing / empty part id. The former ``>2048 bytes → text:null + omitted +
hasFull + part_text expandRefs`` path is REMOVED (the ``part_text`` endpoint
survives only for historical 3.1.x responses). This file locks the new
behavior in BOTH projection modes and pins the unchanged invariants:

* skeleton mode (``skeleton_messages``) — oversized text inline verbatim;
* ``?mode=merged`` — placeholder messages expanded from /full keep their
  oversized text verbatim too (splice is field-faithful);
* ReasoningPart.text > 2048 bytes STILL folds to ``text:null`` + omitted +
  hasFull + ``part_reasoning`` ref (3.1.0 rule, not reverted);
* ``info.summary.diffs`` is always projected ``null`` + a message-level
  ``info_summary_diffs`` ref only when the original was a non-empty list;
* tool ``state.output`` thresholding + ``part_state_output`` ref unchanged.
"""
from __future__ import annotations

import orjson
import httpx
from fastapi import FastAPI

from oc_slimapi.config import Settings, settings as _skel_config
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages
from oc_slimapi.skeleton import (
    REASONING_INLINE_MAX_BYTES,
    TEXT_INLINE_MAX_BYTES,
    skeleton_messages,
)
from oc_slimapi.transform import TransformConfig, TransformPool

SID = "ses_s"
V4 = "?v=4"  # v4-only expand href face
HDR = {"X-Slimapi-Version": "2"}


# ---------------------------------------------------------------------------
# skeleton-mode helpers (mirror test_skeleton_expand.py)
# ---------------------------------------------------------------------------

def _text_part(text, pid="p1", mid="m1"):
    return {"id": pid, "type": "text", "messageID": mid, "text": text}


def _reasoning_part(text, pid="p1", mid="m1"):
    return {"id": pid, "type": "reasoning", "messageID": mid, "text": text}


def _tool_part(output=None, *, pid="p1"):
    """Minimal tool part with whitelisted-only input (the ONLY field that can
    be omitted is output — isolating the state.output thresholding)."""
    state = {"status": "completed", "title": "ran bash",
             "input": {"command": "ls"}}
    if output is not None:
        state["output"] = output
    return {"id": pid, "type": "tool", "messageID": "m1",
            "tool": "bash", "state": state}


def _msg(parts, info=None):
    info = {"id": "m1"} if info is None else info
    return skeleton_messages([{"info": info, "parts": parts}], sid=SID)[0]


# ---------------------------------------------------------------------------
# 1. skeleton mode: TextPart.text > 2048 bytes inlined verbatim —
#    no omitted / hasFull / part_text ref.
# ---------------------------------------------------------------------------

def test_skeleton_text_greater_2048_inlined_verbatim_no_ref():
    big = "x" * (TEXT_INLINE_MAX_BYTES + 1)  # 2049 bytes, far past the former cap
    out = _msg([_text_part(big, pid="prt")])["parts"][0]

    assert out["text"] == big
    assert "omitted" not in out
    assert "hasFull" not in out
    assert "expandRefs" not in out  # part_text endpoint retired from projection


# ---------------------------------------------------------------------------
# 2. merged mode: same full inline — a /full-expanded placeholder keeps its
#    > 2048-byte text verbatim (splice is field-faithful).
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=2.0,
        transform_absorb_budget_seconds=2.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        merged_fanout=8,
        merged_max_fulls_per_page=16,
        merged_max_bytes=8 * 1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, settings: Settings) -> FastAPI:
    app = FastAPI(title="oc-slimapi-b2-test")
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


BIG_TEXT = "x" * (TEXT_INLINE_MAX_BYTES + 1)

# Skeleton-collapsed upstream message: the ONLY part has empty text →
# non-renderable → skeleton_message appends the thin_placeholder marker →
# merged must expand it from /full.
MSG_PLACEHOLDER = {
    "info": {"id": "m_ph", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [{"id": "p_empty", "type": "text", "messageID": "m_ph",
               "text": ""}],
}

# Full body for m_ph: an oversized text part (plus a sibling text part and a
# never-consumed LSP diagnostics map, which the merge strips).
FULL_MSG_PH = {
    "info": {"id": "m_ph", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "p_big", "type": "text", "messageID": "m_ph", "text": BIG_TEXT},
        {"id": "part_tool", "type": "tool", "messageID": "m_ph", "tool": "bash",
         "state": {"status": "completed", "input": {"command": "ls"},
                   "metadata": {"diagnostics": {"lang": "ts"},
                                "cursor": 7},
                   "output": "file1\nfile2"}},
        {"id": "p_short", "type": "text", "messageID": "m_ph",
         "text": "short sibling"},
    ],
}

# What merged must inline for m_ph: FULL parts with ONLY the diagnostics key
# removed — the oversized text is untouched.
FULL_MSG_PH_PARTS_STRIPPED = [
    {"id": "p_big", "type": "text", "messageID": "m_ph", "text": BIG_TEXT},
    {"id": "part_tool", "type": "tool", "messageID": "m_ph", "tool": "bash",
     "state": {"status": "completed", "input": {"command": "ls"},
               "metadata": {"cursor": 7},
               "output": "file1\nfile2"}},
    {"id": "p_short", "type": "text", "messageID": "m_ph",
     "text": "short sibling"},
]


async def test_merged_inlines_text_greater_2048_verbatim(upstream_factory):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return httpx.Response(
                200, content=orjson.dumps([MSG_PLACEHOLDER]),
                headers={"Content-Type": "application/json"},
            )
        if path == "/session/s1/message/m_ph":
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_PH))
        raise AssertionError(f"unexpected upstream path {path}")

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0,
        ) as client:
            merged = await client.get(
                "/slimapi/messages/s1?mode=merged", headers=HDR,
            )
    finally:
        app.state.transforms.shutdown()

    assert merged.status_code == 200
    parts = merged.json()["items"][0]["parts"]
    assert parts == FULL_MSG_PH_PARTS_STRIPPED
    # The big text part is INLINE, verbatim, with no fold markers at all.
    assert parts[0]["text"] == BIG_TEXT
    assert "omitted" not in parts[0]
    assert "hasFull" not in parts[0]
    assert "expandRefs" not in parts[0]


# ---------------------------------------------------------------------------
# 3. unchanged invariant: ReasoningPart.text > 2048 → text:null + omitted +
#    hasFull + part_reasoning ref (3.1.0 rule locked, not reverted).
# ---------------------------------------------------------------------------

def test_reasoning_greater_2048_still_folds_part_reasoning():
    big = "y" * (REASONING_INLINE_MAX_BYTES + 1)
    out = _msg([_reasoning_part(big, pid="prt")])["parts"][0]

    assert out["text"] is None
    assert out["omitted"] == ["text"]
    assert out["hasFull"] is True
    assert out["expandRefs"] == [{
        "category": "part_reasoning",
        "messageID": "m1",
        "partID": "prt",
        "href": f"/slimapi/messages/{SID}/expand/part_reasoning/m1/prt{V4}",
    }]


# ---------------------------------------------------------------------------
# 4. unchanged invariant: info.summary.diffs always null in skeleton + a
#    message-level info_summary_diffs ref ONLY when the original was a
#    non-empty list.
# ---------------------------------------------------------------------------

def test_diffs_skeleton_always_null_ref_only_when_nonempty_list():
    # Non-empty list → null + message-level ref.
    info = {"id": "m1", "summary": {"diffs": [{"file": "a.ts", "additions": 3}],
                                    "files": 1}}
    out = _msg([], info=info)
    assert out["info"]["summary"]["diffs"] is None
    assert out["info"]["expandRefs"] == [{
        "category": "info_summary_diffs",
        "messageID": "m1",
        "href": f"/slimapi/messages/{SID}/expand/info_summary_diffs/m1{V4}",
    }]

    # Empty / falsy / non-list → diffs still null, but NO ref (m1 rule).
    for diffs in (None, [], {}, "", False):
        out2 = _msg([], info={"id": "m1", "summary": {"diffs": diffs}})
        assert out2["info"]["summary"]["diffs"] is None
        assert "expandRefs" not in out2["info"]


# ---------------------------------------------------------------------------
# 5. [3.2.0] missing / empty part id does not matter for text — the former
#    id-guarded reduction path is gone, text inlines verbatim.
# ---------------------------------------------------------------------------

def test_text_no_or_empty_part_id_inlined_verbatim():
    big = "x" * (TEXT_INLINE_MAX_BYTES + 1)
    for part in (
        {"type": "text", "messageID": "m1", "text": big},          # no id
        {"id": "", "type": "text", "messageID": "m1", "text": big},  # empty id
        {"type": "text", "text": big},                             # no id, no mid
    ):
        out = _msg([part])["parts"][0]
        assert out["text"] == big
        assert "omitted" not in out
        assert "hasFull" not in out
        assert "expandRefs" not in out


# ---------------------------------------------------------------------------
# 6. unchanged invariant: tool state.output thresholding + part_state_output
#    ref (5-class fold set unchanged; [3.2.0] touched only TextPart.text).
# ---------------------------------------------------------------------------

def test_tool_state_output_folding_and_ref_unchanged():
    cap = _skel_config.skeleton_inline_output_max_bytes
    # Small output → inlined; nothing omitted → no hasFull, no ref.
    out_small = _msg([_tool_part(output="ok")])["parts"][0]
    assert out_small["state"]["output"] == "ok"
    assert "hasFull" not in out_small
    assert "expandRefs" not in out_small

    # Large output → omitted + hasFull + part_state_output ref (unchanged).
    large = "x" * (cap + 1)
    out_large = _msg([_tool_part(output=large)])["parts"][0]
    assert "output" not in out_large["state"]
    assert out_large["hasFull"] is True
    assert "state.output" in out_large["omitted"]
    assert out_large["expandRefs"] == [{
        "category": "part_state_output",
        "messageID": "m1",
        "partID": "p1",
        "href": f"/slimapi/messages/{SID}/expand/part_state_output/m1/p1{V4}",
    }]
