"""Lane A: expand-design skeleton projection tests (design-expand.md v5).

Covers §4.1 threshold reductions (diffs / text / reasoning), §4.2 patch
string[] fix, §4.3 renderability extension, §4.4 fingerprint determinism,
and the frozen §5 expandRefs schema / §5.3 omitted→category mapping.
"""
import orjson

from oc_slimapi.skeleton import (
    COMPACTION_PART_LIMIT,
    FINGERPRINT_FIELD,
    REASONING_INLINE_MAX_BYTES,
    TEXT_INLINE_MAX_BYTES,
    skeleton_messages,
)

SID = "ses_s"
V3 = "?v=3"


def _text_part(text, pid="p1", mid="m1"):
    return {"id": pid, "type": "text", "messageID": mid, "text": text}


def _reasoning_part(text, pid="p1", mid="m1"):
    return {"id": pid, "type": "reasoning", "messageID": mid, "text": text}


def _msg(parts, info=None):
    info = {"id": "m1"} if info is None else info
    return skeleton_messages([{"info": info, "parts": parts}], sid=SID)[0]


# ---------------------------------------------------------------------------
# §4.2 P0: patch files string[] fix
# ---------------------------------------------------------------------------

def test_patch_files_string_array_preserved_verbatim_with_hash():
    src = {"id": "p1", "type": "patch", "messageID": "m1", "hash": "abc123",
           "files": ["src/a.ts", "src/b.ts", "src/c.ts"]}
    out = skeleton_messages([{"info": {"id": "m1"}, "parts": [src]}])[0]["parts"][0]

    assert out["files"] == ["src/a.ts", "src/b.ts", "src/c.ts"]  # verbatim, not []
    assert out["hash"] == "abc123"                                # hash preserved
    assert "omitted" not in out                                   # nothing else omitted
    assert "hasFull" not in out
    assert "expandRefs" not in out  # §5.3: patch files → no category


# ---------------------------------------------------------------------------
# §4.1 / §5: info.summary.diffs → null + message-level ref
# ---------------------------------------------------------------------------

def test_diffs_null_with_ref_and_other_summary_keys_preserved():
    info = {"id": "m1",
            "summary": {"diffs": [{"file": "a.ts", "additions": 1}],
                        "files": 1, "additions": 1, "deletions": 0}}
    out = _msg([], info=info)

    assert out["info"]["summary"]["diffs"] is None
    assert out["info"]["summary"]["files"] == 1
    assert out["info"]["summary"]["additions"] == 1
    assert out["info"]["summary"]["deletions"] == 0
    assert out["info"]["expandRefs"] == [{
        "category": "info_summary_diffs",
        "messageID": "m1",
        "href": f"/slimapi/messages/{SID}/expand/info_summary_diffs/m1{V3}",
    }]


def test_diffs_empty_or_null_no_message_ref():
    # m1: only a NON-EMPTY LIST of diffs is ref-eligible — no None/[]/{}/""/False.
    for diffs in (None, [], {}, "", False):
        info = {"id": "m1", "summary": {"diffs": diffs, "files": 0}}
        out = _msg([], info=info)
        assert out["info"]["summary"]["diffs"] is None
        assert "expandRefs" not in out["info"]


def test_diffs_missing_or_summary_non_dict_untouched():
    out = _msg([], info={"id": "m1"})  # no summary at all
    assert "expandRefs" not in out["info"]
    out2 = _msg([], info={"id": "m1", "summary": "not-a-dict"})
    assert out2["info"]["summary"] == "not-a-dict"
    assert "expandRefs" not in out2["info"]


# ---------------------------------------------------------------------------
# §4.1: text / reasoning 2 KiB threshold (UTF-8 encoded bytes)
# ---------------------------------------------------------------------------

def test_text_exactly_at_threshold_inlined_no_ref():
    inline = "a" * TEXT_INLINE_MAX_BYTES
    out = _msg([_text_part(inline)])["parts"][0]
    assert out["text"] == inline
    assert "expandRefs" not in out


def test_text_one_byte_over_threshold_null_with_ref():
    big = "a" * (TEXT_INLINE_MAX_BYTES + 1)
    out = _msg([_text_part(big, pid="prt")])["parts"][0]

    assert out["text"] is None          # whole-field omission, no truncation
    assert out["hasFull"] is True
    assert out["omitted"] == ["text"]
    assert out["expandRefs"] == [{
        "category": "part_text",
        "messageID": "m1",
        "partID": "prt",
        "href": f"/slimapi/messages/{SID}/expand/part_text/m1/prt{V3}",
    }]


def test_text_threshold_counts_utf8_bytes_not_chars():
    # 700 three-byte chars = 2100 UTF-8 bytes > 2048 → omitted (chars << limit).
    text = "中" * 700
    assert len(text) < TEXT_INLINE_MAX_BYTES
    assert len(text.encode("utf-8")) > TEXT_INLINE_MAX_BYTES
    out = _msg([_text_part(text)])["parts"][0]
    assert out["text"] is None
    assert out["expandRefs"][0]["category"] == "part_text"

    # 682 three-byte chars = 2046 UTF-8 bytes ≤ 2048 → inlined byte-identically.
    small = "中" * 682
    assert len(small.encode("utf-8")) <= TEXT_INLINE_MAX_BYTES
    out2 = _msg([_text_part(small)])["parts"][0]
    assert out2["text"] == small


def test_text_synthetic_ignored_time_omitted_full_only_no_refs():
    """rev-gpt R1-M2: synthetic/ignored/time are §2.3 /full-only — omitted,
    never inlined, never refs; the projection keeps only {id, type, text}."""
    part = _text_part("short", pid="p1")
    part.update({"synthetic": True, "ignored": False, "time": {"start": 1}})
    out = _msg([part])["parts"][0]
    assert out["text"] == "short"
    assert {"synthetic", "ignored", "time"}.isdisjoint(out)
    assert out["omitted"] == ["ignored", "synthetic", "time"]  # sorted(set)
    assert out["hasFull"] is True
    assert "expandRefs" not in out  # §2.3: /full-only, no refs


def test_reasoning_threshold_and_ref_metadata_time_full_only():
    big = "x" * (REASONING_INLINE_MAX_BYTES + 1)
    out = _msg([_reasoning_part(big, pid="prt")])["parts"][0]
    assert out["text"] is None
    assert out["omitted"] == ["text"]
    assert out["expandRefs"] == [{
        "category": "part_reasoning",
        "messageID": "m1",
        "partID": "prt",
        "href": f"/slimapi/messages/{SID}/expand/part_reasoning/m1/prt{V3}",
    }]

    # Inline reasoning with omitted metadata/time → no refs (/full-only §2.3).
    r = _reasoning_part("ok", pid="prt2")
    r["metadata"] = {"k": 1}
    r["time"] = {"start": 1}
    out2 = _msg([r])["parts"][0]
    assert out2["text"] == "ok"
    assert "metadata" not in out2 and "time" not in out2
    assert "expandRefs" not in out2


# ---------------------------------------------------------------------------
# §5.3: tool part multi-omission refs (≤5, sorted, deduped)
# ---------------------------------------------------------------------------

def test_tool_multiple_omissions_refs_sorted_and_deduped():
    state = {
        "status": "completed",
        "title": "t",
        # two non-whitelist keys → exactly ONE part_state_input_full ref
        "input": {"command": "ls", "secret_key": "v", "other": "w"},
        # two non-whitelist keys → exactly ONE part_state_metadata_full ref
        "metadata": {"sessionId": "s", "foreign": {"x": 1}, "extra": 2},
        "output": "o" * 5000,          # > field cap → omitted → part_state_output
        "attachments": [{"mime": "text/plain", "url": "data:x"}],  # always-omit
    }
    part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash", "state": state}
    out = _msg([part])["parts"][0]

    refs = out["expandRefs"]
    cats = [r["category"] for r in refs]
    # dedup: input/metadata collapsed to 1 each → 4 categories, all distinct.
    assert len(cats) == 4
    assert cats == sorted(cats)  # category 字典序 (deterministic)
    assert cats == ["part_state_attachments", "part_state_input_full",
                    "part_state_metadata_full", "part_state_output"]
    for r in refs:
        assert r["messageID"] == "m1"
        assert r["partID"] == "p1"
        assert r["href"].endswith(f"/expand/{r['category']}/m1/p1{V3}")
        assert r["href"].startswith(f"/slimapi/messages/{SID}/")
    assert out["hasFull"] is True


def test_tool_error_omission_ref():
    state = {"status": "error", "input": {"command": "ls"}, "error": "e" * 5000}
    part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash", "state": state}
    out = _msg([part])["parts"][0]
    assert [r["category"] for r in out["expandRefs"]] == ["part_state_error"]


def test_tool_no_omissions_no_refs():
    state = {"status": "completed", "title": "t",
             "input": {"command": "ls"}, "output": "small"}
    part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash", "state": state}
    out = _msg([part])["parts"][0]
    assert "expandRefs" not in out
    assert "hasFull" not in out


def test_tool_full_only_fields_no_refs():
    # structured/result/raw are /full-only (§2.3); empty attachments → no ref.
    state = {"status": "completed", "input": {"command": "ls"},
             "structured": {"a": 1}, "result": {"b": 2}, "raw": "c",
             "attachments": []}
    part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash", "state": state}
    out = _msg([part])["parts"][0]
    assert "expandRefs" not in out
    assert out["hasFull"] is True


# ---------------------------------------------------------------------------
# §5.3: file url/source refs
# ---------------------------------------------------------------------------

def test_file_url_and_source_refs():
    part = {"id": "p1", "type": "file", "messageID": "m1",
            "url": "data:image/png;base64,AAAA",
            "source": {"type": "file", "path": "a.ts"}}
    out = _msg([part])["parts"][0]
    assert out["url"] is None
    assert [r["category"] for r in out["expandRefs"]] == ["part_source", "part_url"]

    # Short http url inlined → no part_url ref; no source → nothing omitted.
    part2 = {"id": "p2", "type": "file", "messageID": "m1",
             "url": "https://example.test/a.png"}
    out2 = _msg([part2])["parts"][0]
    assert out2["url"] == "https://example.test/a.png"
    assert "expandRefs" not in out2


def test_file_null_or_empty_source_no_ref():
    part = {"id": "p1", "type": "file", "messageID": "m1",
            "url": "data:image/png;base64,AAAA", "source": None}
    out = _msg([part])["parts"][0]
    assert [r["category"] for r in out["expandRefs"]] == ["part_url"]  # no part_source


# ---------------------------------------------------------------------------
# §5.3: step-start / step-finish snapshot ref
# ---------------------------------------------------------------------------

def test_step_snapshot_omission_ref():
    step = {"id": "p1", "type": "step-start", "messageID": "m1", "snapshot": "tree ..."}
    out = _msg([step])["parts"][0]
    assert out["hasFull"] is True
    assert out["omitted"] == ["snapshot"]
    assert out["expandRefs"] == [{
        "category": "part_snapshot", "messageID": "m1", "partID": "p1",
        "href": f"/slimapi/messages/{SID}/expand/part_snapshot/m1/p1{V3}",
    }]


def test_step_finish_reason_cost_tokens_no_snapshot_ref():
    step = {"id": "p2", "type": "step-finish", "messageID": "m1",
            "reason": "done", "cost": 1.0}
    out = _msg([step])["parts"][0]
    assert out["omitted"] == ["cost", "reason"]  # recorded, but /full-only (§2.3)
    assert "expandRefs" not in out


# ---------------------------------------------------------------------------
# §5.2: compaction ["*"] → /full-only, no ref
# ---------------------------------------------------------------------------

def test_compaction_over_limit_star_no_ref():
    big = {"id": "p1", "type": "compaction", "messageID": "m1", "auto": True,
           "overflow": "x" * (COMPACTION_PART_LIMIT + 1)}
    out = _msg([big])["parts"][0]
    assert out["omitted"] == ["*"]
    assert "expandRefs" not in out


# ---------------------------------------------------------------------------
# §4.3: text:null + expandRefs counts as renderable (no thin_placeholder)
# ---------------------------------------------------------------------------

def test_text_null_with_expand_refs_is_renderable_no_placeholder():
    big = "x" * (TEXT_INLINE_MAX_BYTES + 1)
    out = _msg([_text_part(big)])
    assert len(out["parts"]) == 1
    assert not any(p["id"].startswith("thin_placeholder_") for p in out["parts"])
    assert out["parts"][0]["text"] is None
    assert out["parts"][0]["expandRefs"][0]["category"] == "part_text"


def test_reasoning_null_with_expand_refs_is_renderable_no_placeholder():
    big = "y" * (REASONING_INLINE_MAX_BYTES + 1)
    out = _msg([_reasoning_part(big)])
    assert len(out["parts"]) == 1
    assert not any(p["id"].startswith("thin_placeholder_") for p in out["parts"])
    assert out["parts"][0]["expandRefs"][0]["category"] == "part_reasoning"


def test_message_level_diffs_omission_not_part_of_renderability():
    # §4.3: message-level omission (diffs) does NOT make a message renderable —
    # an empty-parts message still gets the placeholder even with info.expandRefs.
    info = {"id": "m1", "summary": {"diffs": [{"file": "a"}]}}
    out = _msg([], info=info)
    assert out["info"]["expandRefs"][0]["category"] == "info_summary_diffs"
    assert out["parts"][0]["id"].startswith("thin_placeholder_")


# ---------------------------------------------------------------------------
# §4.4 / §5.2: determinism (fingerprint + wire bytes, same input → same output)
# ---------------------------------------------------------------------------

def test_fingerprint_and_wire_deterministic_with_expand_refs():
    msgs = [{
        "info": {"id": "m1", "summary": {"diffs": [{"file": "a.ts"}]}},
        "parts": [_text_part("x" * (TEXT_INLINE_MAX_BYTES + 1)),
                  _reasoning_part("y" * (REASONING_INLINE_MAX_BYTES + 1))],
    }]
    a = skeleton_messages(msgs, sid=SID, fingerprint=True)
    b = skeleton_messages(msgs, sid=SID, fingerprint=True)

    assert a[0][FINGERPRINT_FIELD] == b[0][FINGERPRINT_FIELD]
    assert orjson.dumps(a, option=orjson.OPT_SORT_KEYS) == orjson.dumps(b, option=orjson.OPT_SORT_KEYS)


def test_no_sid_reductions_apply_but_refs_suppressed():
    """Routes pass sid; pure callers without sid still get the reductions
    (diffs null, text null) but no expandRefs (no href can be built)."""
    out = skeleton_messages([{
        "info": {"id": "m1", "summary": {"diffs": [1]}},
        "parts": [_text_part("x" * (TEXT_INLINE_MAX_BYTES + 1))],
    }])[0]
    assert out["info"]["summary"]["diffs"] is None
    assert "expandRefs" not in out["info"]
    assert out["parts"][0]["text"] is None
    assert "expandRefs" not in out["parts"][0]


# ---------------------------------------------------------------------------
# rev-gpt R1 review fixes (string[] diffStats / text field omission / ID
# guards / type-aware diffs / expandRefs key ownership)
# ---------------------------------------------------------------------------

def test_patch_string_array_no_fabricated_diffstats():
    """R1-M1: a pure string[] files carries no per-file stat data — deriving
    diffStats would fabricate {0,0,N}. Not injected; state not created."""
    src = {"id": "p1", "type": "patch", "messageID": "m1", "hash": "h",
           "files": ["src/a.ts", "src/b.ts"]}
    out = skeleton_messages([{"info": {"id": "m1"}, "parts": [src]}])[0]["parts"][0]

    assert out["files"] == ["src/a.ts", "src/b.ts"]
    assert out["hash"] == "h"
    assert "state" not in out          # no fabricated state.metadata.diffStats
    assert "diffStats" not in out
    assert "omitted" not in out


def test_oversized_text_without_part_id_omits_but_no_ref():
    """R1-M3: a part-level ref requires a non-empty part id — without one the
    reduction applies but no unusable part-level ref is emitted."""
    big = "x" * (TEXT_INLINE_MAX_BYTES + 1)
    part = {"type": "text", "messageID": "m1", "text": big}  # no "id"
    out = _msg([part])["parts"][0]
    assert out["text"] is None
    assert out["hasFull"] is True
    assert out["omitted"] == ["text"]
    assert "expandRefs" not in out


def test_message_without_id_omits_diffs_but_no_info_ref():
    """R1-M3: the ''unknown'' id fallback must never yield an info ref."""
    info = {"summary": {"diffs": [{"file": "a.ts"}]}}  # no "id"
    out = _msg([], info=info)
    assert out["info"]["summary"]["diffs"] is None
    assert "expandRefs" not in out["info"]
    assert out["parts"][0]["id"] == "thin_placeholder_unknown"


def test_upstream_info_expand_refs_junk_replaced_or_removed():
    """R1-m2: expandRefs is sidecar-owned — upstream junk is replaced when the
    sidecar generates a ref and dropped when it generates none."""
    info = {"id": "m1", "expandRefs": [{"category": "bogus"}],
            "summary": {"diffs": [{"file": "a.ts"}]}}
    out = _msg([], info=info)
    assert out["info"]["expandRefs"] == [{
        "category": "info_summary_diffs", "messageID": "m1",
        "href": f"/slimapi/messages/{SID}/expand/info_summary_diffs/m1{V3}",
    }]

    info2 = {"id": "m1", "expandRefs": [{"category": "bogus"}],
             "summary": {"diffs": []}}
    out2 = _msg([], info=info2)
    assert "expandRefs" not in out2["info"]


def test_upstream_part_expand_refs_junk_stripped():
    """R1-m2: junk expandRefs never survives on parts — not in output, not in
    omitted/hasFull, not through the compaction whole-part deepcopy."""
    state = {"status": "completed", "input": {"command": "ls"},
             "output": "o" * 5000}
    part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash",
            "expandRefs": [{"category": "bogus"}], "state": state}
    out = _msg([part])["parts"][0]
    assert [r["category"] for r in out["expandRefs"]] == ["part_state_output"]

    part2 = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash",
             "expandRefs": [{"category": "bogus"}],
             "state": {"status": "completed", "title": "t"}}
    out2 = _msg([part2])["parts"][0]
    assert "expandRefs" not in out2
    assert "expandRefs" not in out2.get("omitted", [])

    small = {"id": "p1", "type": "compaction", "messageID": "m1",
             "expandRefs": [{"category": "bogus"}], "auto": True, "text": "ok"}
    out3 = _msg([small])["parts"][0]
    assert out3["type"] == "compaction" and out3["text"] == "ok"
    assert "expandRefs" not in out3


def test_text_threshold_contract_literal_2048():
    """R1: pin the contract value itself (no implementation-constant import):
    2048 bytes inline, 2049 -> null + ref."""
    out = _msg([_text_part("a" * 2048)])["parts"][0]
    assert out["text"] == "a" * 2048
    assert "expandRefs" not in out

    out2 = _msg([_text_part("a" * 2049)])["parts"][0]
    assert out2["text"] is None
    assert out2["hasFull"] is True
    assert out2["expandRefs"][0]["category"] == "part_text"


def test_reasoning_threshold_contract_literal_2048():
    out = _msg([_reasoning_part("a" * 2048)])["parts"][0]
    assert out["text"] == "a" * 2048
    assert "expandRefs" not in out

    out2 = _msg([_reasoning_part("a" * 2049)])["parts"][0]
    assert out2["text"] is None
    assert out2["hasFull"] is True
    assert out2["expandRefs"][0]["category"] == "part_reasoning"


def test_projection_does_not_mutate_or_alias_source():
    """Deepcopy isolation: mutating the projected result never affects the
    original input message tree."""
    src_summary = {"diffs": [{"file": "a.ts", "additions": 3}], "files": 1}
    src_part = {"id": "p1", "type": "tool", "messageID": "m1", "tool": "bash",
                "state": {"status": "completed",
                          "input": {"command": "ls", "arg": "-la"},
                          "output": "small"}}
    source = [{"info": {"id": "m1", "summary": src_summary}, "parts": [src_part]}]
    out = skeleton_messages(source, sid=SID)[0]

    # mutate the projection deeply
    out["info"]["summary"]["files"] = 999
    out["info"]["summary"]["diffs"] = "mutated"
    out["parts"][0]["state"]["input"]["command"] = "mutated"
    out["parts"][0]["state"]["output"] = "mutated"

    assert source[0]["info"]["summary"]["diffs"] == [{"file": "a.ts", "additions": 3}]
    assert source[0]["info"]["summary"]["files"] == 1
    assert source[0]["parts"][0]["state"]["input"]["command"] == "ls"
    assert source[0]["parts"][0]["state"]["output"] == "small"


def test_source_summary_diffs_untouched_by_projection():
    """Nested source values stay in place: the projected diffs is null, the
    original summary object (and its diffs list) is byte-for-byte intact."""
    src_summary = {"diffs": [{"file": "a.ts", "additions": 3}], "files": 1}
    source = [{"info": {"id": "m1", "summary": src_summary}, "parts": []}]
    out = skeleton_messages(source, sid=SID)[0]

    assert out["info"]["summary"]["diffs"] is None
    assert source[0]["info"]["summary"] is src_summary
    assert source[0]["info"]["summary"]["diffs"] == [{"file": "a.ts", "additions": 3}]
    assert source[0]["info"]["summary"]["files"] == 1
