import json
import math
from pathlib import Path

import orjson

from oc_slimapi.config import settings as _skel_config
from oc_slimapi.skeleton import (
    FINGERPRINT_FIELD,
    REASONING_INLINE_MAX_BYTES,
    _compute_diffstats,
    _compute_diffstats_from_files,
    _field_byte_size,
    skeleton_messages,
)


FIXTURE = Path(__file__).parent / "fixtures" / "msg40.json"


def load_fixture():
    raw = FIXTURE.read_bytes()
    return raw, json.loads(raw)


def parts(messages, part_type):
    return [
        part
        for message in messages
        for part in message["parts"]
        if part["type"] == part_type
    ]


def test_skeleton_preserves_message_and_part_order_and_text_verbatim():
    _, source = load_fixture()
    result = skeleton_messages(source)

    assert [item["info"]["id"] for item in result] == [
        item["info"]["id"] for item in source
    ]
    # [3.2.0] TextPart.text is always inlined verbatim — no size threshold.
    result_text = [
        part for part in parts(result, "text")
        if not part["id"].startswith("thin_placeholder_")
    ]
    source_text = [part for part in parts(source, "text")]
    assert [part["id"] for part in result_text] == [
        part["id"] for part in source_text
    ]
    assert [part["text"] for part in result_text] == [
        part["text"] for part in source_text
    ]


def test_reasoning_text_is_preserved_verbatim():
    _, source = load_fixture()
    result = skeleton_messages(source)

    # expand §4.1: reasoning > 2 KiB UTF-8 bytes is projected as text:null +
    # expandRefs; inline ones stay byte-identical.
    inline_result = [
        part for part in parts(result, "reasoning") if part.get("text") is not None
    ]
    inline_source = [
        part for part in parts(source, "reasoning")
        if len(part["text"].encode("utf-8")) <= REASONING_INLINE_MAX_BYTES
    ]
    assert [part["text"] for part in inline_result] == [
        part["text"] for part in inline_source
    ]


def test_tool_state_is_reduced_to_contract_whitelists():
    _, source = load_fixture()
    result = skeleton_messages(source)
    allowed_input = {
        "path", "filePath", "file_path", "command", "agent", "description",
        "subagent_type", "todos",
    }
    allowed_metadata = {"sessionId", "sessionID", "description", "agent",
                        "diffStats", "files"}

    for tool in parts(result, "tool"):
        state = tool.get("state", {})
        # Always-omit heavy fields never appear in thin state.
        assert "structured" not in state
        assert "result" not in state
        assert "raw" not in state
        assert "attachments" not in state
        assert set(state.get("input", {})) <= allowed_input
        assert set(state.get("metadata", {})) <= allowed_metadata
        # _mark invariant: hasFull/omitted are present iff something was
        # omitted. A tool whose only omitted field was a small output (now
        # inlined) and has no other omissions must NOT carry hasFull.
        if tool.get("omitted"):
            assert tool["hasFull"] is True
        else:
            assert "hasFull" not in tool
        # output/error are either inlined (small, ≤ per-field cap) or in the
        # omitted list — never half-truncated. If present, the inlined value
        # fits the per-field cap.
        for key in ("output", "error"):
            if key in state:
                assert _field_byte_size(state[key]) <= _skel_config.skeleton_inline_output_max_bytes


def test_data_urls_are_removed_and_marked_but_short_http_urls_survive():
    source = [{
        "info": {"id": "m1", "role": "user"},
        "parts": [
            {"id": "p1", "type": "file", "messageID": "m1", "url": "data:image/png;base64,AAAA"},
            {"id": "p2", "type": "file", "messageID": "m1", "url": "https://example.test/a.png"},
        ],
    }]

    result = skeleton_messages(source)[0]["parts"]
    assert result[0]["url"] is None
    assert result[0]["hasFull"] is True
    assert "url" in result[0]["omitted"]
    assert result[1]["url"] == "https://example.test/a.png"


def test_empty_parts_receive_renderable_placeholder():
    source = [{"info": {"id": "m1", "role": "assistant"}, "parts": []}]
    result = skeleton_messages(source)

    # 修订八: placeholder part carries NO display copy — the machine marker is
    # exclusively the `thin_placeholder_` ID prefix; text is an empty string.
    assert result[0]["parts"] == [{
        "id": "thin_placeholder_m1",
        "messageID": "m1",
        "type": "text",
        "text": "",
        "hasFull": True,
        "omitted": ["parts"],
    }]


# ---------------------------------------------------------------------------
# P1-29: nested type defense — malformed upstream messages (info=None,
# parts=int/bool/string) must not crash skeleton_message. A single bad
# message degrades to a placeholder rather than a 500.
# ---------------------------------------------------------------------------

def test_skeleton_message_with_null_info_normalises_to_empty():
    """info=None → normalised to {} → placeholder uses 'unknown' id."""
    result = skeleton_messages([{"info": None, "parts": []}])
    assert result[0]["info"] == {}
    assert result[0]["parts"][0]["id"] == "thin_placeholder_unknown"


def test_skeleton_message_with_missing_info_normalises():
    """No info key at all → normalised to {}."""
    result = skeleton_messages([{"parts": []}])
    assert result[0]["info"] == {}
    assert result[0]["parts"][0]["messageID"] == "unknown"


def test_skeleton_message_with_non_dict_info_normalises():
    """info is a string/list/int → normalised to {}."""
    for bad_info in ("not-a-dict", [1, 2], 42, True):
        result = skeleton_messages([{"info": bad_info, "parts": []}])
        assert result[0]["info"] == {}


def test_skeleton_message_with_non_list_parts_normalises():
    """parts is an int/bool/string (not None, not list) → normalised to []."""
    for bad_parts in (1, True, "not-a-list", 3.14):
        result = skeleton_messages([{"info": {"id": "m1"}, "parts": bad_parts}])
        # Normalised to [] → no renderable parts → placeholder appended.
        assert len(result[0]["parts"]) == 1
        assert result[0]["parts"][0]["type"] == "text"
        assert result[0]["parts"][0]["messageID"] == "m1"


def test_skeleton_message_with_null_parts_normalises():
    """parts=None → normalised to [] → placeholder."""
    result = skeleton_messages([{"info": {"id": "m1"}, "parts": None}])
    assert len(result[0]["parts"]) == 1
    assert result[0]["parts"][0]["id"] == "thin_placeholder_m1"


def test_skeleton_messages_mixed_good_and_bad():
    """A bad message among good ones doesn't crash the batch — it degrades
    to a placeholder while good messages project normally."""
    source = [
        {"info": {"id": "good"}, "parts": [{"type": "text", "text": "hi", "id": "p1", "messageID": "good"}]},
        {"info": None, "parts": 1},  # bad: both info and parts malformed
        {"info": {"id": "good2"}, "parts": [{"type": "text", "text": "hi2", "id": "p2", "messageID": "good2"}]},
    ]
    result = skeleton_messages(source)
    assert len(result) == 3
    assert result[0]["parts"][0]["text"] == "hi"
    assert result[1]["info"] == {}
    assert result[1]["parts"][0]["id"] == "thin_placeholder_unknown"
    assert result[2]["parts"][0]["text"] == "hi2"


def test_golden_skeleton_is_bounded_by_the_content_preservation_floor():
    raw, source = load_fixture()
    encoded = orjson.dumps(skeleton_messages(source))

    # The v2 contract requires preserving text and reasoning.text. Those two
    # strings alone are 34.70% of this fixture, so the requested 15% raw-byte
    # target is mathematically impossible. 55% remains a strict, reproducible
    # bound while honoring the authoritative field contract. Thresholding now
    # inlines small tool outputs too (additive bytes), but the bound still holds.
    assert len(encoded) < len(raw) * 0.55


# ---------------------------------------------------------------------------
# Thresholded skeleton (additive). Small state.output/state.error is inlined;
# large or budget-spent fields are omitted + hasFull. hasFull is set ONLY when
# something is actually omitted — a fully-inlined tool shows no expand mark.
# ---------------------------------------------------------------------------

def _tool_part(output=None, error=None, *, tool="bash", extra_input=None):
    """Build a minimal tool part with whitelisted-only input (so the ONLY thing
    that can be omitted is the output/error itself — isolating thresholding)."""
    state = {"status": "completed", "title": "ran bash", "input": {"command": "ls"}}
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    if extra_input:
        state["input"].update(extra_input)
    return {
        "id": "p1", "type": "tool", "messageID": "m1", "tool": tool,
        "state": state,
    }


def _ascii_str_of_json_bytes(target_bytes: int) -> str:
    """ASCII string whose orjson JSON byte size == target_bytes.

    orjson.dumps(s) == len(s) + 2 for ASCII (the surrounding quotes), so we
    build ``'x' * (target - 2)`` and assert the invariant to stay robust
    against any future change in the byte-counting primitive."""
    assert target_bytes >= 2
    s = "x" * (target_bytes - 2)
    assert _field_byte_size(s) == target_bytes
    return s


def test_small_tool_output_is_inlined_without_hasfull():
    """Small output (≤ per-field cap) with no other omissions → output present
    in thin state and NO hasFull/omitted (nothing to expand)."""
    output = _ascii_str_of_json_bytes(100)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    assert tool["state"]["output"] == output
    assert "hasFull" not in tool
    assert "omitted" not in tool


def test_large_tool_output_is_omitted_with_hasfull():
    """Large output (> per-field cap) → output omitted, hasFull true, omitted
    contains state.output."""
    output = _ascii_str_of_json_bytes(_skel_config.skeleton_inline_output_max_bytes + 4096)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    assert "output" not in tool["state"]
    assert tool["hasFull"] is True
    assert "state.output" in tool["omitted"]


def test_boundary_output_exactly_at_threshold_is_inlined():
    """Output whose JSON byte size == the cap is inlined (≤ is inclusive)."""
    output = _ascii_str_of_json_bytes(_skel_config.skeleton_inline_output_max_bytes)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    assert tool["state"]["output"] == output
    assert "hasFull" not in tool


def test_boundary_output_one_byte_over_threshold_is_omitted():
    """Output at cap+1 JSON bytes is omitted."""
    output = _ascii_str_of_json_bytes(_skel_config.skeleton_inline_output_max_bytes + 1)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    assert "output" not in tool["state"]
    assert tool["hasFull"] is True
    assert "state.output" in tool["omitted"]


def test_per_message_budget_falls_back_to_omit():
    """Cumulative inlined bytes across parts are capped per-message; once the
    cap is spent, later parts (in order) omit even small outputs."""
    # Each output is well under the per-field cap, but together they exceed the
    # per-message cap. Parts are processed in order; the first N fit, the rest
    # fall back to omit + hasFull.
    per_field = _skel_config.skeleton_inline_output_max_bytes  # 4096
    per_message = _skel_config.skeleton_inline_output_max_message_bytes  # 16384
    # 8 parts × ~3000 JSON bytes each ≈ 24 KiB > 16 KiB per-message cap.
    n = 8
    size_each = per_field - 1000  # comfortably under per-field; sums > per_message
    assert size_each * n > per_message
    parts_ = [
        {"id": f"p{i}", "type": "tool", "messageID": "m1", "tool": "bash",
         "state": {"status": "completed", "input": {"command": "ls"},
                   "output": _ascii_str_of_json_bytes(size_each)}}
        for i in range(n)
    ]
    source = [{"info": {"id": "m1"}, "parts": parts_}]
    tools = skeleton_messages(source)[0]["parts"]

    inlined = [t for t in tools if "output" in t["state"]]
    omitted = [t for t in tools if "output" not in t["state"]]
    # Budget is exhausted somewhere in the middle: at least one inlined and at
    # least one omitted (the tail). Total inlined bytes stay within the cap.
    assert inlined and omitted
    total_inlined = sum(_field_byte_size(t["state"]["output"]) for t in inlined)
    assert total_inlined <= per_message
    # Omitted ones carry hasFull + state.output.
    for t in omitted:
        assert t["hasFull"] is True
        assert "state.output" in t["omitted"]
    # Inlined ones have no output-driven omission (whitelisted input only).
    for t in inlined:
        assert "state.output" not in t.get("omitted", [])


def test_structured_result_raw_attachments_are_always_omitted():
    """Heavy nested fields are never inlined regardless of size."""
    state = {
        "status": "completed", "input": {"command": "ls"},
        "structured": {"a": 1}, "result": {"b": 2}, "raw": "c", "attachments": [],
        "output": "small",
    }
    source = [{"info": {"id": "m1"},
               "parts": [{"id": "p1", "type": "tool", "messageID": "m1",
                          "tool": "bash", "state": state}]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    thin = tool["state"]
    assert thin["output"] == "small"  # small enough to inline
    for key in ("structured", "result", "raw", "attachments"):
        assert key not in thin
        assert f"state.{key}" in tool["omitted"]
    assert tool["hasFull"] is True


def test_utf8_multibyte_output_byte_counting_is_consistent():
    """Multibyte / emoji output is measured by UTF-8 wire bytes, not char
    count — a field that looks short in chars but is large in bytes is omitted."""
    # One emoji == 4 UTF-8 bytes (orjson emits raw UTF-8, not \uXXXX escapes).
    # Grow the string until its JSON byte size exceeds the per-field cap; its
    # CHAR count stays well below the cap, proving we count bytes not chars.
    emoji = "😀"
    output = emoji * (_skel_config.skeleton_inline_output_max_bytes // 4 + 1)
    assert _field_byte_size(output) > _skel_config.skeleton_inline_output_max_bytes
    assert len(output) < _skel_config.skeleton_inline_output_max_bytes  # chars << bytes

    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert "output" not in tool["state"]
    assert "state.output" in tool["omitted"]

    # And a small multibyte output is inlined byte-identically.
    small = "画像処理"  # multibyte but tiny
    assert _field_byte_size(small) <= _skel_config.skeleton_inline_output_max_bytes
    source2 = [{"info": {"id": "m1"}, "parts": [_tool_part(output=small)]}]
    assert skeleton_messages(source2)[0]["parts"][0]["state"]["output"] == small


def test_small_state_error_is_inlined():
    """Small state.error (e.g. a short failure message) is inlined; a large
    one is omitted exactly like output."""
    small_err = "FileNotFoundError: /tmp/x"
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(error=small_err)]}]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert tool["state"]["error"] == small_err
    # Whitelisted input only → no omission → no hasFull.
    assert "hasFull" not in tool

    big_err = _ascii_str_of_json_bytes(_skel_config.skeleton_inline_output_max_bytes + 1)
    source2 = [{"info": {"id": "m1"}, "parts": [_tool_part(error=big_err)]}]
    tool2 = skeleton_messages(source2)[0]["parts"][0]
    assert "error" not in tool2["state"]
    assert "state.error" in tool2["omitted"]
    assert tool2["hasFull"] is True


def test_inlined_output_still_reports_hasfull_when_other_fields_omitted():
    """hasFull means 'more fields are fetchable via /full', NOT 'this content
    is hidden'. A tool with an inlined (small) output AND an omitted structured
    field carries hasFull for the structured field while output stays visible."""
    state = {
        "status": "completed", "input": {"command": "ls"},
        "output": "small visible result",
        "structured": {"huge": "x" * 100},
    }
    source = [{"info": {"id": "m1"},
               "parts": [{"id": "p1", "type": "tool", "messageID": "m1",
                          "tool": "bash", "state": state}]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    # Output is visible AND hasFull is set (because structured is omitted).
    assert tool["state"]["output"] == "small visible result"
    assert tool["hasFull"] is True
    assert "state.output" not in tool["omitted"]
    assert "state.structured" in tool["omitted"]


# ---------------------------------------------------------------------------
# compact diffStats projection (批次4). Tool parts derive from upstream
# ``state.metadata.filediff`` (single ``Snapshot.FileDiff`` dict); patch parts
# derive from ``files[]`` (list of per-file diff items). diffStats is injected
# AFTER thresholding so it is never elligible for omission.
# ---------------------------------------------------------------------------

from oc_slimapi.skeleton import (
    _compute_diffstats as _cd,
    _compute_diffstats_from_files as _cdf,
)


def test_diffstats_from_single_filediff():
    """Single filediff dict → additions/deletions extracted, files=1."""
    result = _cd({"file": "src/foo.ts", "additions": 42, "deletions": 12})
    assert result == {"additions": 42, "deletions": 12, "files": 1}


def test_diffstats_from_filediff_missing_additions_deletions():
    """Filediff dict without ANY valid additions/deletions → all-malformed
    entry → None (P1-N3: garbage must not masquerade as {0,0,1}; the caller
    falls through to the ② files / ③ diff-parse derivations)."""
    assert _cd({"file": "src/foo.ts"}) is None


def test_diffstats_from_filediff_zero_counts_are_valid():
    """Explicit zeros ARE valid counts (>=0 int) — a real diff touching no
    lines (rename) still derives {0, 0, 1}."""
    result = _cd({"file": "src/foo.ts", "additions": 0, "deletions": 0})
    assert result == {"additions": 0, "deletions": 0, "files": 1}


def test_diffstats_from_filediff_malformed_values_never_raise():
    """P1-N3 exception-safety: string garbage / inf / nan / bool / negative /
    float count values never raise; invalid values contribute 0; at least
    one valid count on ANY entry keeps the derivation alive."""
    inf, nan = float("inf"), float("nan")
    # entry 1 all-garbage, entry 2 carries the single valid count
    result = _cd([
        {"file": "a.ts", "additions": "garbage", "deletions": inf},
        {"file": "b.ts", "additions": True, "deletions": 3},
    ])
    assert result == {"additions": 0, "deletions": 3, "files": 2}
    # floats (even integral 1.0) and negatives are invalid — not coerced;
    # a negative only poisons its own value (→0), the OTHER valid count of
    # the same entry still derives
    assert _cd([{"file": "a.ts", "additions": 1.5, "deletions": 2.0}]) is None
    assert _cd({"file": "a.ts", "additions": -1, "deletions": -2}) is None
    assert _cd({"file": "a.ts", "additions": -1, "deletions": 2}) == {
        "additions": 0, "deletions": 2, "files": 1}
    # non-dict list entries are skipped entirely
    assert _cd(["garbage", 5, None]) is None
    assert _cd(["garbage", {"file": "a.ts", "additions": 4}]) == {
        "additions": 4, "deletions": 0, "files": 1}


def test_diffstats_from_filediff_partial_numbers():
    """Filediff with only additions → deletions=0."""
    result = _cd({"file": "src/foo.ts", "additions": 5})
    assert result == {"additions": 5, "deletions": 0, "files": 1}


def test_diffstats_from_filediff_none():
    """None → None."""
    assert _cd(None) is None


def test_diffstats_from_filediff_empty_list():
    """Empty list → None (no files to count)."""
    assert _cd([]) is None


def test_diffstats_from_filediff_list():
    """List of filediff dicts → summed across all items."""
    result = _cd([
        {"file": "a.ts", "additions": 10, "deletions": 3},
        {"file": "b.ts", "additions": 5, "deletions": 1},
    ])
    assert result == {"additions": 15, "deletions": 4, "files": 2}


def test_diffstats_from_files_empty():
    """Empty files list → None."""
    assert _cdf([]) is None


def test_diffstats_from_files_none():
    """None → None."""
    assert _cdf(None) is None


def test_diffstats_from_files_non_list():
    """Non-list → None."""
    assert _cdf("not-a-list") is None


def test_diffstats_from_files_single():
    """Single file with additions/deletions."""
    result = _cdf([{"path": "a.ts", "additions": 5, "deletions": 2}])
    assert result == {"additions": 5, "deletions": 2, "files": 1}


def test_diffstats_from_files_multiple():
    """Multiple files → summed."""
    result = _cdf([
        {"path": "a.ts", "additions": 5, "deletions": 2},
        {"path": "b.ts", "additions": 10, "deletions": 0},
    ])
    assert result == {"additions": 15, "deletions": 2, "files": 2}


def test_diffstats_from_files_missing_additions():
    """Item missing additions → treated as 0."""
    result = _cdf([{"path": "a.ts", "deletions": 3}])
    assert result == {"additions": 0, "deletions": 3, "files": 1}


def test_tool_with_filediff_injects_diffstats_into_metadata():
    """Tool part with filediff → diffStats appears in state.metadata."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
            "state": {
                "status": "completed",
                "title": "src/foo.ts",
                "input": {"filePath": "/abs/src/foo.ts", "oldString": "a", "newString": "b"},
                "output": "Edit applied successfully.",
                "metadata": {
                    "filediff": {"file": "src/foo.ts", "additions": 12, "deletions": 4},
                    "diagnostics": {"severity": 1},
                    "diff": "--- a/src/foo.ts\n+++ b/src/foo.ts\n@@ -1 +1 @@\n-old\n+new",
                },
            },
        }],
    }]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert tool["state"]["metadata"]["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}
    # Other metadata keys (sessionId etc.) are absent from this fixture; only
    # diffStats and the always-present whitelist keys survive.
    assert "filediff" not in tool["state"]["metadata"]
    assert "diagnostics" not in tool["state"]["metadata"]
    assert "diff" not in tool["state"]["metadata"]


def test_tool_without_filediff_no_diffstats():
    """Tool part without filediff (bash/read/glob) → no diffStats injected."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "bash",
            "state": {
                "status": "completed",
                "title": "ls -la",
                "input": {"command": "ls -la"},
                "output": "total 42\n",
                "metadata": {"exit": 0, "output": "total 42\n", "truncated": False},
            },
        }],
    }]
    tool = skeleton_messages(source)[0]["parts"][0]
    metadata = tool["state"].get("metadata")
    # metadata is whitelist-only; bash has no whitelist keys, so it's absent.
    # Even if it were present (e.g. sessionId), diffStats must NOT be there.
    if metadata is not None:
        assert "diffStats" not in metadata


def test_tool_with_filediff_no_additions_defaults():
    """Filediff without ANY valid count → all-malformed → derivation falls
    through the ① leg; no lower tier available → no diffStats injected
    (P1-N3: never a fabricated {0,0,1})."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
            "state": {
                "status": "completed",
                "title": "src/foo.ts",
                "input": {"filePath": "/abs/src/foo.ts"},
                "output": "Edit applied successfully.",
                "metadata": {
                    "filediff": {"file": "src/foo.ts"},
                },
            },
        }],
    }]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert "diffStats" not in tool["state"].get("metadata", {})


def test_patch_with_files_injects_diffstats():
    """Patch part with files[] → diffStats in state.metadata, matching file data."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "metadata": {"path": "src/foo.ts"},
            "files": [
                {"path": "src/foo.ts", "additions": 12, "deletions": 4, "status": "modified"},
            ],
            "state": {"status": "completed", "title": "Patch src/foo.ts"},
        }],
    }]
    result = skeleton_messages(source)[0]["parts"][0]
    assert result["state"]["metadata"]["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}
    assert "diffStats" not in result  # never at top level (ocdroid reads state.metadata)
    # files[] is still projected as before
    assert result["files"][0]["path"] == "src/foo.ts"
    assert result["files"][0]["additions"] == 12


def test_patch_without_files_no_diffstats():
    """Patch without files[] → no diffStats (neither top-level nor state.metadata)."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "metadata": {"path": "src/foo.ts"},
            "state": {"status": "completed"},
        }],
    }]
    result = skeleton_messages(source)[0]["parts"][0]
    assert "diffStats" not in result
    assert "diffStats" not in result.get("state", {}).get("metadata", {})


def test_diffstats_survives_thresholding_no_false_omit():
    """diffStats is a tiny object (~50 B) and is injected AFTER thresholding,
    so even when output/error are omitted due to size, diffStats stays."""
    big_output = _ascii_str_of_json_bytes(_skel_config.skeleton_inline_output_max_bytes + 1)
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
            "state": {
                "status": "completed",
                "title": "src/foo.ts",
                "input": {"filePath": "/abs/src/foo.ts"},
                "output": big_output,
                "metadata": {
                    "filediff": {"file": "src/foo.ts", "additions": 42, "deletions": 12},
                },
            },
        }],
    }]
    tool = skeleton_messages(source)[0]["parts"][0]
    # output is omitted (large)
    assert "output" not in tool["state"]
    assert "state.output" in tool["omitted"]
    # diffStats is still present
    assert tool["state"]["metadata"]["diffStats"] == {"additions": 42, "deletions": 12, "files": 1}


def test_diffstats_consistent_with_patch_files():
    """For a multi-file patch, diffStats aggregated from files[] equals
    the sum of per-file additions/deletions."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "metadata": {"path": "src/"},
            "files": [
                {"path": "a.ts", "additions": 5, "deletions": 2, "status": "modified"},
                {"path": "b.ts", "additions": 10, "deletions": 0, "status": "modified"},
                {"path": "c.ts", "additions": 3, "deletions": 8, "status": "modified"},
            ],
            "state": {"status": "completed", "title": "Patch 3 files"},
        }],
    }]
    result = skeleton_messages(source)[0]["parts"][0]
    assert result["state"]["metadata"]["diffStats"] == {"additions": 18, "deletions": 10, "files": 3}
    assert "diffStats" not in result  # never at top level
    # Each file item still carries its individual stats
    assert result["files"][0]["additions"] == 5
    assert result["files"][1]["deletions"] == 0
    assert result["files"][2]["additions"] == 3


def test_patch_with_files_but_no_state_creates_state_metadata():
    """Patch with files[] but NO upstream state → still creates state.metadata.diffStats,
    so the client read path (state.metadata?.get) does not chain-break."""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "metadata": {"path": "src/foo.ts"},
            "files": [
                {"path": "src/foo.ts", "additions": 7, "deletions": 2, "status": "modified"},
            ],
            # NOTE: no "state" key — verify minimal container is created.
        }],
    }]
    result = skeleton_messages(source)[0]["parts"][0]
    assert result["state"]["metadata"]["diffStats"] == {"additions": 7, "deletions": 2, "files": 1}
    assert "diffStats" not in result  # never at top level


def test_patch_and_tool_diffstats_same_wire_location():
    """Within one message, a tool part (filediff) and a patch part (files[]) both
    surface diffStats at the SAME wire location — state.metadata.diffStats — so the
    client consumes both with one read path."""
    source = [{
        "info": {"id": "m1"},
        "parts": [
            {"id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
             "state": {"status": "completed", "metadata": {
                 "filediff": {"file": "a.ts", "additions": 3, "deletions": 1}}},
             },
            {"id": "p2", "type": "patch", "messageID": "m1",
             "files": [{"path": "b.ts", "additions": 5, "deletions": 2}],
             "state": {"status": "completed"},
             },
        ],
    }]
    parts = skeleton_messages(source)[0]["parts"]
    assert parts[0]["state"]["metadata"]["diffStats"] == {"additions": 3, "deletions": 1, "files": 1}
    assert parts[1]["state"]["metadata"]["diffStats"] == {"additions": 5, "deletions": 2, "files": 1}
    # Neither carries a top-level diffStats (the pre-fix patch bug location).
    assert "diffStats" not in parts[0]
    assert "diffStats" not in parts[1]


# ---------------------------------------------------------------------------
# Catalog skeleton projections (command / agent). These are pure whitelist
# picks over a list of catalog entries — no part-typing, no hasFull/omitted
# (catalog listings have no per-entry expand endpoint).
# ---------------------------------------------------------------------------

from oc_slimapi.skeleton import (
    AGENT_SKELETON_KEYS,
    COMMAND_SKELETON_KEYS,
    skeleton_agent,
    skeleton_agents,
    skeleton_command,
    skeleton_commands,
)


def test_skeleton_command_keeps_whitelist_drops_rest():
    src = {
        "name": "dev",
        "description": "General coding agent",
        "agent": None,  # optional, often null
        "hints": [{"type": "mcp"}],
        "template": "x" * 3000,   # never consumed → drop
        "source": "builtin",
        "model": "gpt-x",
        "subtask": False,
    }
    out = skeleton_command(src)
    assert set(out.keys()) == COMMAND_SKELETON_KEYS
    assert out["name"] == "dev"
    assert out["description"] == "General coding agent"
    assert out["agent"] is None          # preserved verbatim (incl. null)
    assert out["hints"] == [{"type": "mcp"}]
    for dropped in ("template", "source", "model", "subtask"):
        assert dropped not in out


def test_skeleton_command_omits_absent_optional_keys():
    # agent / hints absent (majority of commands have neither) → sparse skeleton
    out = skeleton_command({"name": "n", "description": "d", "template": "t"})
    assert out == {"name": "n", "description": "d"}


def test_skeleton_commands_projects_list_in_order():
    src = [
        {"name": "a", "description": "da"},
        {"name": "b", "description": "db", "agent": "plan"},
    ]
    out = skeleton_commands(src)
    assert [item["name"] for item in out] == ["a", "b"]
    assert out[1]["agent"] == "plan"


def test_skeleton_agent_keeps_whitelist_drops_rest():
    src = {
        "name": "build",
        "description": "Build specialist",
        "mode": "primary",
        "hidden": False,
        "native": True,
        "prompt": "y" * 18000,      # largest field → drop
        "permission": [{"tool": "bash"}],  # Ruleset, no UI consumer → drop
        "topP": 0.5,
        "temperature": 0.7,
        "color": "#fff",
        "variant": None,
        "options": {},
        "steps": None,
        "model": "claude",
    }
    out = skeleton_agent(src)
    assert set(out.keys()) == AGENT_SKELETON_KEYS
    assert out["name"] == "build"
    assert out["mode"] == "primary"
    assert out["hidden"] is False
    assert out["native"] is True
    for dropped in (
        "prompt", "permission", "topP", "temperature", "color",
        "variant", "options", "steps", "model",
    ):
        assert dropped not in out


def test_skeleton_agent_omits_absent_optional_keys():
    # native/hidden absent → sparse skeleton (only present keys kept)
    out = skeleton_agent({"name": "n", "description": "d", "mode": "all"})
    assert out == {"name": "n", "description": "d", "mode": "all"}


def test_skeleton_agents_projects_list_in_order():
    src = [
        {"name": "a", "description": "da", "mode": "all", "hidden": False, "native": False},
        {"name": "b", "description": "db", "mode": "primary", "hidden": True, "native": True},
    ]
    out = skeleton_agents(src)
    assert [item["name"] for item in out] == ["a", "b"]
    assert out[1]["native"] is True
    assert out[1]["hidden"] is True


def test_skeleton_catalogs_filter_non_dict_items():
    """A malformed upstream catalog entry (null / string / number) is silently
    dropped rather than reaching ``_pick`` (which would TypeError on
    ``key in value``). Mirrors ``skeleton_messages``'s ``isinstance(part, dict)``
    filter — one bad row degrades to a shorter skeleton, not a 500."""
    command_src = [None, "bad", 42, {"name": "dev", "description": "d"}, {"template": "t"}]
    out_c = skeleton_commands(command_src)
    # Only the two dict entries survive; projected to their whitelists.
    assert len(out_c) == 2
    assert out_c[0] == {"name": "dev", "description": "d"}
    assert out_c[1] == {}  # {"template":"t"} has no whitelist key -> empty pick

    agent_src = [None, "x", {"name": "build", "mode": "primary"}]
    out_a = skeleton_agents(agent_src)
    assert len(out_a) == 1
    assert out_a[0] == {"name": "build", "mode": "primary"}


def test_skeleton_limits_injectable_no_cross_app_leak():
    """T8-C1: SkeletonLimits is injectable per-call — two invocations of the
    same pure function with different ``limits`` produce different projections,
    and the second call is NOT contaminated by the first (no module-level /
    app-level leak). ``state.output`` sized between the small and big caps is
    omitted under small caps and inlined under big caps."""
    from oc_slimapi.skeleton import SkeletonLimits, skeleton_messages

    output = "x" * (2 * 1024)  # 2 KiB — between the two caps below
    msg = {"info": {"id": "m1"}, "parts": [{
        "id": "p1", "type": "tool", "messageID": "m1", "tool": "bash",
        "state": {"status": "completed", "output": output},
    }]}
    small = SkeletonLimits(field_bytes=512, message_bytes=512)  # 2KiB > 512 -> omit
    big = SkeletonLimits(field_bytes=8 * 1024, message_bytes=8 * 1024)  # 2KiB < 8KiB -> inline

    small_result = skeleton_messages([msg], limits=small)[0]["parts"][0]
    big_result = skeleton_messages([msg], limits=big)[0]["parts"][0]

    # Small caps -> output omitted (hasFull + state.output recorded).
    assert "output" not in small_result["state"]
    assert small_result.get("hasFull") is True
    assert "state.output" in small_result.get("omitted", [])
    # Big caps -> output inlined (no hasFull iff it was the only field).
    assert "output" in big_result["state"]
    assert big_result.get("hasFull") is not True or "state.output" not in big_result.get("omitted", [])


# ---------------------------------------------------------------------------
# §2 compress title synthesis (plan v2.1 §2). FROZEN evaluation order:
# input is dict → content non-empty list → content[0] is dict → only then
# topic → summary → segment-count fallback. Any miss → NO title at all.
# ---------------------------------------------------------------------------

def _compress_part(state_input, *, title=None):
    state = {"status": "completed", "input": state_input}
    if title is not None:
        state["title"] = title
    return {"id": "p1", "type": "tool", "messageID": "m1", "tool": "compress",
            "state": state}


def _compress_title(state_input, **kw):
    src = [{"info": {"id": "m1"}, "parts": [_compress_part(state_input, **kw)]}]
    return skeleton_messages(src)[0]["parts"][0]["state"].get("title")


def test_compress_title_topic_has_priority():
    assert _compress_title(
        {"content": [{"topic": "Graph optimizations", "summary": "sum"}]}
    ) == "Graph optimizations"


def test_compress_title_summary_when_topic_missing():
    assert _compress_title(
        {"content": [{"summary": "Only a summary"}]}
    ) == "Only a summary"


def test_compress_title_segment_count_fallback():
    assert _compress_title(
        {"content": [{}, {"topic": "ignored"}]}   # only content[0] is read
    ) == "压缩 2 段"


def test_compress_title_abandons_when_input_not_dict():
    # never .get() on a non-dict input — each shape misses step (1)
    for bad in (None, ["x"], "str", 42):
        assert _compress_title(bad) is None


def test_compress_title_abandons_when_content_not_a_list():
    for bad in (None, "text", {"topic": "t"}, 0):
        assert _compress_title({"content": bad}) is None


def test_compress_title_abandons_when_content_empty_list():
    assert _compress_title({"content": []}) is None


def test_compress_title_abandons_when_first_segment_not_dict():
    for bad in (None, "text", 7):
        assert _compress_title({"content": [bad, {"topic": "t"}]}) is None


def test_compress_existing_title_never_overwritten():
    assert _compress_title(
        {"content": [{"topic": "synthetic"}]}, title="upstream title"
    ) == "upstream title"
    # an EMPTY-string title counts as missing → synthesis applies
    assert _compress_title(
        {"content": [{"topic": "synthetic"}]}, title=""
    ) == "synthetic"


def test_compress_title_missing_keys_no_fabricated_text():
    # content[0] is a dict but carries neither topic nor summary → the ONLY
    # allowed fallback is the segment count, nothing else
    assert _compress_title({"content": [{}]}) == "压缩 1 段"


def test_non_compress_tool_never_synthesizes_title():
    """Counter-example (§5c.1): a non-compress tool carrying the exact same
    input shape gets NO title synthesis — the whitelist for tool state stays
    untouched and no top-level topic/summary key is invented."""
    src = [{"info": {"id": "m1"},
            "parts": [{"id": "p1", "type": "tool", "messageID": "m1",
                       "tool": "bash",
                       "state": {"status": "completed",
                                 "input": {"content": [{"topic": "t"}],
                                           "command": "ls"}}}]}]
    tool = skeleton_messages(src)[0]["parts"][0]
    assert "title" not in tool["state"]
    assert "topic" not in tool and "summary" not in tool
    # and the content sub-key is omitted (not whitelisted) + expandable
    assert "state.input.content" in tool["omitted"]


def test_compress_title_clip_semantics():
    # whitespace-only → MISSING → falls through to the next candidate
    assert _compress_title(
        {"content": [{"topic": "   ", "summary": "real summary"}]}
    ) == "real summary"
    # non-str candidates (None / numbers) are missing, not str()-coerced
    assert _compress_title({"content": [{"topic": 42}]}) == "压缩 1 段"
    # truncation is by CHARACTERS to 160, no ellipsis appended
    long_topic = "a" * 300
    got = _compress_title({"content": [{"topic": long_topic}]})
    assert got == "a" * 160
    # stripping happens BEFORE truncation
    assert _compress_title({"content": [{"topic": "  padded  "}]}) == "padded"
    # multibyte truncation counts characters, not bytes
    cjk = "汉" * 200
    assert _compress_title({"content": [{"topic": cjk}]}) == "汉" * 160


def test_compress_content_stays_omitted_with_expand_ref():
    """``content`` is NOT added to TOOL_INPUT_KEYS — it stays omitted with a
    part_state_input_full ref (title synthesis reads it, never copies it)."""
    src = [{"info": {"id": "m1"},
            "parts": [_compress_part({"command": "summarize",
                                      "content": [{"topic": "t"}]})]}]
    tool = skeleton_messages(src, sid="ses_s")[0]["parts"][0]
    assert "content" not in tool["state"]["input"]
    assert "state.input.content" in tool["omitted"]
    _ref_categories = {r["category"] for r in tool["expandRefs"]}
    assert "part_state_input_full" in _ref_categories


# ---------------------------------------------------------------------------
# §3 outputBytes — an omitted real output attaches a size hint in the SAME
# wire-byte metric as the caps (len(orjson.dumps(value))). error has no
# counterpart.
# ---------------------------------------------------------------------------

def test_outputbytes_present_when_output_over_cap():
    output = _ascii_str_of_json_bytes(
        _skel_config.skeleton_inline_output_max_bytes + 1)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]

    assert "output" not in tool["state"]
    assert tool["state"]["outputBytes"] == len(orjson.dumps(output))
    # outputBytes is a hint, not a fetchable field: it never lands in omitted
    assert "state.outputBytes" not in tool.get("omitted", [])


def test_outputbytes_present_when_message_budget_exhausted():
    """Budget-exhausted omission (output small enough per-field but the
    per-message cap is spent) carries the hint too — same else-branch."""
    from oc_slimapi.skeleton import SkeletonLimits

    per_field = _skel_config.skeleton_inline_output_max_bytes
    limits = SkeletonLimits(field_bytes=per_field, message_bytes=per_field)
    out_a = _ascii_str_of_json_bytes(per_field - 10)   # fits, spends budget
    out_b = _ascii_str_of_json_bytes(100)              # small but no budget
    src = [{"info": {"id": "m1"}, "parts": [
        {"id": "pa", "type": "tool", "messageID": "m1", "tool": "bash",
         "state": {"status": "completed", "input": {"command": "ls"},
                   "output": out_a}},
        {"id": "pb", "type": "tool", "messageID": "m1", "tool": "bash",
         "state": {"status": "completed", "input": {"command": "ls"},
                   "output": out_b}},
    ]}]
    tools = skeleton_messages(src, limits=limits)[0]["parts"]

    assert tools[0]["state"]["output"] == out_a
    assert "outputBytes" not in tools[0]["state"]          # inlined → no hint
    assert "output" not in tools[1]["state"]
    assert tools[1]["state"]["outputBytes"] == len(orjson.dumps(out_b))


def test_outputbytes_absent_when_output_inlined():
    output = _ascii_str_of_json_bytes(100)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=output)]}]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert tool["state"]["output"] == output
    assert "outputBytes" not in tool["state"]


def test_outputbytes_counts_utf8_wire_bytes():
    """Multibyte outputBytes uses wire bytes (orjson raw UTF-8), not chars."""
    emoji = "😀" * (_skel_config.skeleton_inline_output_max_bytes + 1)
    assert _field_byte_size(emoji) == len(emoji.encode("utf-8")) + 2
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(output=emoji)]}]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert "output" not in tool["state"]
    assert tool["state"]["outputBytes"] == len(orjson.dumps(emoji))


def test_error_omission_has_no_errorbytes():
    big_err = _ascii_str_of_json_bytes(
        _skel_config.skeleton_inline_output_max_bytes + 1)
    source = [{"info": {"id": "m1"}, "parts": [_tool_part(error=big_err)]}]
    tool = skeleton_messages(source)[0]["parts"][0]
    assert "error" not in tool["state"]
    assert "errorBytes" not in tool["state"]


# ---------------------------------------------------------------------------
# §4a patch files normalization — string → {"path"}, cap 10, filesTotal =
# SOURCE count (invalid entries included), _valid_count as the single
# numeric validator.
# ---------------------------------------------------------------------------

def _patch_msg(files):
    return {"info": {"id": "m1"},
            "parts": [{"id": "p1", "type": "patch", "messageID": "m1",
                       "hash": "h", "files": files}]}


def _patch_out(files):
    return skeleton_messages([_patch_msg(files)])[0]["parts"][0]


def test_patch_files_cap_boundary_ten_without_files_total():
    files = [f"src/f{i}.ts" for i in range(10)]
    out = _patch_out(files)
    assert out["files"] == [{"path": f"src/f{i}.ts"} for i in range(10)]
    assert "filesTotal" not in out            # exactly at cap → no hint


def test_patch_files_cap_eleven_gets_files_total():
    files = [f"src/f{i}.ts" for i in range(11)]
    out = _patch_out(files)
    assert len(out["files"]) == 10
    assert out["filesTotal"] == 11


def test_patch_files_total_counts_source_including_invalid():
    """filesTotal 触发口径 = 有效映射条目数（P2-25 / §10.2 修订四）；
    触发时 filesTotal 的值 = len(源数组)（含被跳过的非 str/dict 条目）。

    源 12 条仅 2 条无效 → 有效映射 10 ≤ cap → 不截断、不附 filesTotal
    （回归钉：旧口径按源长度触发，此处曾错附 filesTotal=12）。"""
    out = _patch_out(["a.ts", 42, None, {"path": "b.ts"}])
    assert out["files"] == [{"path": "a.ts"}, {"path": "b.ts"}]
    assert "filesTotal" not in out             # 4 ≤ 10 → no hint
    files = ["f.ts", 42, None] + [f"g{i}.ts" for i in range(9)]
    out = _patch_out(files)
    # 12 source entries (2 invalid) → valid mapped 10 ≤ cap → no hint
    assert "filesTotal" not in out
    assert len(out["files"]) == 10


def test_patch_files_trigger_is_valid_mapped_count():
    """P2-25 边界三态（§10.2 修订四：有效映射条目超 10 才截断+附计数，
    未超限不附；附时值 = 源计数）。

    ① 源 15 条畸形混合仅 8 条合法 → 映射 8 ≤ 10：不截断、不附
       filesTotal（旧口径按源长度 15 触发——回归钉）；
    ② 源 15 条全合法 → 截为前 10 + filesTotal = 15（值 = 源计数）；
    ③ 源 8 条全合法 → 8 条全发、无 filesTotal。
    """
    # ① mixed-malformed: 8 valid (6 str + 2 dict) + 7 invalid entries
    files = [f"src/f{i}.ts" for i in range(6)] \
        + [42, None, True, 3.5, [1], [2], [3]] \
        + [{"path": f"d{i}.ts"} for i in range(2)]
    assert len(files) == 15
    out = _patch_out(files)
    assert len(out["files"]) == 8               # no truncation (8 ≤ cap)
    assert out["files"][-2:] == [{"path": "d0.ts"}, {"path": "d1.ts"}]
    assert "filesTotal" not in out              # valid mapped 8 ≤ 10
    # ② all-valid 15 → capped 10 + source-count filesTotal
    out = _patch_out([f"src/f{i}.ts" for i in range(15)])
    assert len(out["files"]) == 10
    assert out["filesTotal"] == 15
    # ③ all-valid 8 → no hint
    out = _patch_out([f"src/f{i}.ts" for i in range(8)])
    assert len(out["files"]) == 8
    assert "filesTotal" not in out


def test_patch_files_overflow_mixed_keeps_source_count_value():
    """源 15 条（11 合法 + 4 畸形）→ 有效映射 11 超 cap：截前 10，
    filesTotal = 源计数 15（含畸形条目——true breadth，非有效数 11）。"""
    files = [f"src/f{i}.ts" for i in range(11)] + [42, None, True, [1]]
    assert len(files) == 15
    out = _patch_out(files)
    assert len(out["files"]) == 10
    assert out["filesTotal"] == 15


def test_patch_files_malformed_counts_no_exception():
    """P1-5/N4: bool/negative/+inf/nan/float counts never raise. The legacy
    dict pick keeps entries VERBATIM (plan §4a — pick only), and the
    diffStats derivation re-validates: invalid values contribute 0;
    diffStats is injected only when ≥1 valid count exists anywhere (a
    normalized {path}-only array injects nothing)."""
    out = _patch_out([
        {"path": "a.ts", "additions": True, "deletions": -3},
        {"path": "b.ts", "additions": 1.5, "deletions": float("inf")},
        {"path": "c.ts", "additions": float("nan"), "deletions": 2.0},
        {"path": "d.ts", "additions": 5, "deletions": 1},
    ])
    files_out = out["files"]
    assert files_out[0] == {"path": "a.ts", "additions": True,
                            "deletions": -3}
    assert files_out[1] == {"path": "b.ts", "additions": 1.5,
                            "deletions": float("inf")}
    assert files_out[2]["path"] == "c.ts"
    assert math.isnan(files_out[2]["additions"])   # nan survives pick verbatim
    assert files_out[2]["deletions"] == 2.0
    assert files_out[3] == {"path": "d.ts", "additions": 5, "deletions": 1}
    # one valid entry (5/1) survives the guard: invalid values → 0
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 5, "deletions": 1, "files": 4}
    # all-malformed → guard rejects, no fabricated {0,0,N}
    out2 = _patch_out([{"path": "a.ts", "additions": -1},
                       {"path": "b.ts", "deletions": 1.5}])
    assert "state" not in out2


def test_patch_files_legacy_dict_pick_unchanged():
    out = _patch_out([{"path": "a.ts", "additions": 2, "deletions": 3,
                       "status": "modified", "file": "ignored"}])
    assert out["files"] == [{"path": "a.ts", "additions": 2, "deletions": 3,
                             "status": "modified"}]
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 2, "deletions": 3, "files": 1}


# ---------------------------------------------------------------------------
# §4b tool metadata.files compact projection + aggregate diffStats priority
# chain (⓪ source → ① filediff → ② files → ③ edit diff → ④ none).
# ---------------------------------------------------------------------------

def _tool_msg(metadata, *, tool="applypatch"):
    return {"info": {"id": "m1"},
            "parts": [{"id": "p1", "type": "tool", "messageID": "m1",
                       "tool": tool,
                       "state": {"status": "completed",
                                 "input": {"command": "apply"},
                                 "metadata": metadata}}]}


def _tool_meta_out(metadata, **kw):
    return skeleton_messages([_tool_msg(metadata, **kw)])[0]["parts"][0]


def _apply_patch_files_entry(path, **extra):
    entry = {"filePath": "/abs/" + path, "relativePath": path,
             "type": "modified", "patch": "@@ big body @@"}
    entry.update(extra)
    return entry


def test_tool_metadata_files_compact_mapping():
    out = _tool_meta_out({"files": [
        _apply_patch_files_entry("a.ts", additions=2, deletions=3),
        _apply_patch_files_entry("b.ts"),        # no counts at all
    ]})
    files = out["state"]["metadata"]["files"]
    assert files == [
        {"path": "a.ts", "additions": 2, "deletions": 3, "status": "modified"},
        {"path": "b.ts", "status": "modified"},
    ]
    # patch bodies / filePath / relativePath / type are stripped
    assert "patch" not in files[0] and "filePath" not in files[0]
    # aggregate diffStats from the compact entries (②-leg)
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 2, "deletions": 3, "files": 2}


def test_tool_metadata_files_relative_path_preferred():
    entry = {"filePath": "/abs/x.ts", "relativePath": "rel/x.ts",
             "type": "added"}
    out = _tool_meta_out({"files": [entry]})
    assert out["state"]["metadata"]["files"][0]["path"] == "rel/x.ts"
    # filePath only, when relativePath is absent/non-str
    out2 = _tool_meta_out({"files": [
        {"filePath": "/abs/y.ts", "type": "added"},
        {"relativePath": 42, "filePath": "/abs/z.ts"},
    ]})
    assert [f["path"] for f in out2["state"]["metadata"]["files"]] == [
        "/abs/y.ts", "/abs/z.ts"]


def test_tool_metadata_files_cap_and_ref_boundaries():
    """1 / 10 / 11 entries: cap fires above 10; the metadata_full ref is
    alive in all three tiers (source metadata.files non-empty list)."""
    for n in (1, 10, 11):
        files = [_apply_patch_files_entry(f"f{i}.ts", additions=1)
                 for i in range(n)]
        out = skeleton_messages([_tool_msg({"files": files})],
                                sid="ses_s")[0]["parts"][0]
        thin_meta = out["state"]["metadata"]
        assert len(thin_meta["files"]) == min(n, 10)
        if n > 10:
            assert thin_meta["filesTotal"] == n
        else:
            assert "filesTotal" not in thin_meta
        assert any(r["category"] == "part_state_metadata_full"
                   and r["partID"] == "p1" for r in out["expandRefs"])


def test_tool_metadata_files_all_malformed_keeps_ref_alive():
    """P2-N1 源值判定: a non-empty SOURCE files list whose entries all fail
    the mapping (non-dicts) → mapped list absent, but the
    part_state_metadata_full ref STILL fires (expand must reach the source)."""
    out = skeleton_messages([_tool_msg({"files": [42, "junk", None]})],
                            sid="ses_s")[0]["parts"][0]
    assert "files" not in out["state"].get("metadata", {})
    assert "filesTotal" not in out["state"].get("metadata", {})
    assert any(r["category"] == "part_state_metadata_full"
               and r["partID"] == "p1" for r in out["expandRefs"])
    # no valid counts anywhere → no diffStats either
    assert "diffStats" not in out["state"].get("metadata", {})


def test_tool_metadata_files_trigger_is_valid_mapped_count():
    """P2-25（§10.2 修订四）metadata 侧同款口径：触发条件 = 有效映射
    条目数（dict 条目）超 cap，非源数组长度；附时值 = 源计数。

    ① 源 15 条（8 dict + 7 非 dict 畸形）→ 映射 8 ≤ 10：不截断、
       不附 filesTotal（旧口径按源长度 15 触发——回归钉）；
    ② 源 15 条（12 dict + 3 畸形）→ 截前 10 + filesTotal = 15。
    """
    malformed = [42, None, "str", True, [1], [2], [3]]  # non-dict entries
    files = [_apply_patch_files_entry(f"f{i}.ts", additions=1)
             for i in range(8)] + malformed
    assert len(files) == 15
    out = skeleton_messages([_tool_msg({"files": files})],
                            sid="ses_s")[0]["parts"][0]
    thin_meta = out["state"]["metadata"]
    assert len(thin_meta["files"]) == 8          # no truncation (8 ≤ cap)
    assert "filesTotal" not in thin_meta         # valid mapped 8 ≤ 10
    # source files non-empty → metadata_full ref alive (P2-N1 源值判定)
    assert any(r["category"] == "part_state_metadata_full"
               and r["partID"] == "p1" for r in out["expandRefs"])

    files = [_apply_patch_files_entry(f"f{i}.ts", additions=1)
             for i in range(12)] + [42, None, [1]]
    assert len(files) == 15
    out = skeleton_messages([_tool_msg({"files": files})],
                            sid="ses_s")[0]["parts"][0]
    thin_meta = out["state"]["metadata"]
    assert len(thin_meta["files"]) == 10         # capped at 10
    assert thin_meta["filesTotal"] == 15         # value = SOURCE count


def test_tool_metadata_mixed_malformed_counts_no_exception():
    out = _tool_meta_out({"files": [
        _apply_patch_files_entry("a.ts", additions=True, deletions=-1),
        _apply_patch_files_entry("b.ts", additions=1.0, deletions=2.5),
        _apply_patch_files_entry("c.ts", additions=3, deletions=0),
    ]})
    files = out["state"]["metadata"]["files"]
    assert files[0] == {"path": "a.ts", "status": "modified"}
    assert files[1] == {"path": "b.ts", "status": "modified"}
    assert files[2] == {"path": "c.ts", "additions": 3, "deletions": 0,
                        "status": "modified"}
    # c.ts carries the only valid counts; a/b contribute 0 to the sums but
    # count toward files (valid mapped entries)
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 3, "deletions": 0, "files": 3}


def test_tool_metadata_priority_source_diffstats_never_overridden():
    """⓪-leg: a structurally valid source diffStats is kept verbatim — no
    derived value overrides it, even when filediff/files COULD derive."""
    source_stats = {"additions": 100, "deletions": 50, "files": 9}
    out = _tool_meta_out({
        "diffStats": source_stats,
        "filediff": {"additions": 1, "deletions": 1},
        "files": [_apply_patch_files_entry("a.ts", additions=5, deletions=5)],
    })
    assert out["state"]["metadata"]["diffStats"] == source_stats


def test_tool_metadata_priority_filediff_beats_files():
    out = _tool_meta_out({
        "filediff": {"additions": 2, "deletions": 1},
        "files": [_apply_patch_files_entry("a.ts", additions=50, deletions=50)],
    })
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 2, "deletions": 1, "files": 1}


def test_tool_metadata_priority_malformed_filediff_falls_to_files():
    """Non-empty but ALL-malformed filediff (string garbage / inf / bool
    values) → no exception, falls through to the ② files leg."""
    out = _tool_meta_out({
        "filediff": [{"additions": "garbage", "deletions": float("inf")},
                     {"additions": True, "deletions": "x"}],
        "files": [_apply_patch_files_entry("a.ts", additions=7, deletions=2)],
    })
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 7, "deletions": 2, "files": 1}


def test_tool_metadata_priority_files_beats_edit_diff_parse():
    """② beats ③: an edit part carrying BOTH files and a parseable diff
    derives from files, not the text."""
    diff_text = ("Index: src/a.ts\n===\n--- src/a.ts\tt\n+++ src/a.ts\tt\n"
                 "@@ -1,1 +1,2 @@\n ctx\n+added\n")
    out = _tool_meta_out({
        "files": [_apply_patch_files_entry("src/b.ts", additions=9, deletions=9)],
        "diff": diff_text,
    }, tool="edit")
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 9, "deletions": 9, "files": 1}
    # and files shows the SOURCE projection, not the parsed diff
    assert out["state"]["metadata"]["files"][0]["path"] == "src/b.ts"


def test_tool_metadata_no_derivation_no_injection():
    """④-leg: nothing derivable → no diffStats key fabricated at all."""
    out = _tool_meta_out({"cursor": 3})
    assert "metadata" not in out["state"] or (
        "diffStats" not in out["state"]["metadata"])


# ---------------------------------------------------------------------------
# §4d B2 `_files_from_diff_text` parser unit tests. Production format =
# jsdiff createTwoFilesPatch: `Index: <path>` / `===` / bare ---/+++ paths
# with \t<timestamp> suffixes / `@@` hunks.
# ---------------------------------------------------------------------------

def _pfd(text):
    from oc_slimapi.skeleton import _files_from_diff_text
    return _files_from_diff_text(text)


def test_parse_production_edit_format():
    """The exact jsdiff createTwoFilesPatch shape (bare paths, ===, tab
    timestamps)."""
    text = (
        "Index: /home/u/repo/src/a.ts\n"
        "===\n"
        "--- /home/u/repo/src/a.ts\t2026-08-21 10:00:00.000000000 +0800\n"
        "+++ /home/u/repo/src/a.ts\t2026-08-21 10:00:01.000000000 +0800\n"
        "@@ -1,2 +1,3 @@\n"
        " context\n"
        "-old line\n"
        "+new line\n"
        "+added line\n"
    )
    assert _pfd(text) == [
        {"path": "/home/u/repo/src/a.ts", "additions": 2, "deletions": 1}]


def test_parse_headerless_text_is_none():
    for bad in ("", "just text\nno headers\n", "===\n===\n", "@@ -1,1 +1,1 @@\n+x\n"):
        assert _pfd(bad) is None


def test_parse_non_string_is_none():
    for bad in (None, 42, {"diff": "x"}, ["a"], b"bytes"):
        assert _pfd(bad) is None


def test_parse_isolated_index_line_is_none():
    """P1-N5: an isolated `Index:` never validates a section — zero-hunk
    segments need PAIRED ---/+++ headers."""
    assert _pfd("Index: src/a.ts\n===\nnothing else\n") is None
    assert _pfd("Index: src/a.ts\nIndex: src/b.ts\n") is None


def test_parse_multi_file_with_index_lines():
    text = (
        "Index: /src/a.py\n"
        "===\n"
        "--- /src/a.py\tt1\n"
        "+++ /src/a.py\tt2\n"
        "@@ -1,1 +1,2 @@\n"
        " ctx\n"
        "+add\n"
        "Index: /src/b.py\n"
        "===\n"
        "--- /src/b.py\tt1\n"
        "+++ /src/b.py\tt2\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert _pfd(text) == [
        {"path": "/src/a.py", "additions": 1, "deletions": 0},
        {"path": "/src/b.py", "additions": 1, "deletions": 1},
    ]


def test_parse_no_newline_marker_ignored():
    text = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "+tail\n"
    )
    # budget 3 = 1(-old) + 2(+new,+tail); the \\ marker is neither counted
    # nor budgeted
    assert _pfd(text) == [{"path": "x.ts", "additions": 2, "deletions": 1}]


def test_parse_dev_null_sides():
    delete = ("--- a/gone.ts\tt\n+++ /dev/null\tt\n@@ -1,1 +0,0 @@\n-old\n")
    assert _pfd(delete) == [{"path": "gone.ts", "additions": 0, "deletions": 1}]
    add = ("--- /dev/null\tt\n+++ b/new.ts\tt\n@@ -0,0 +1,2 @@\n+a\n+b\n")
    assert _pfd(add) == [{"path": "new.ts", "additions": 2, "deletions": 0}]


def test_parse_truncated_text_undercounts_not_misattributes():
    """Truncated mid-hunk (budget NOT exhausted, no boundary): the lines seen
    so far are attributed — 宁少计不误归属."""
    text = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,5 +1,5 @@\n"       # budget 10, only 3 body lines arrive
        " ctx\n"
        "-removed\n"
        "+added\n"
    )                                    # EOF closes the section as-is
    assert _pfd(text) == [{"path": "x.ts", "additions": 1, "deletions": 1}]


def test_parse_header_like_lines_inside_hunk_body_are_body():
    """Before budget exhaustion, header-like lines are hunk BODY (git
    semantics) unless they form an ADJACENT ---/+++ PAIR (the ② boundary).
    * a stray ``+++`` with no pending ``---`` counts as a body addition;
    * a ``--- x`` followed by a NON-pair line counts as a body deletion and
      its pair candidacy is cleared by the intervening line."""
    stray_plus = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,6 +1,6 @@\n"        # budget 12, far from exhausted
        " ctx\n"                    # 2
        "+++ literal too\n"         # 1 (body ADDITION — no pending ---)
        " ctx\n"                    # 2
    )
    assert _pfd(stray_plus) == [
        {"path": "x.ts", "additions": 1, "deletions": 0}]

    broken_pair = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,6 +1,6 @@\n"
        "--- literal context\n"     # 1 (body deletion, pending)
        " ctx\n"                    # 2 (clears the pending pair candidacy)
        "+++ not a pair\n"          # 1 (body addition)
    )
    assert _pfd(broken_pair) == [
        {"path": "x.ts", "additions": 1, "deletions": 1}]


def test_parse_adjacent_pair_boundary_transfers_section():
    """②-boundary: a NEW paired ---/+++ while the hunk budget is NOT
    exhausted closes the current segment (counts kept) and opens the next."""
    text = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,9 +1,9 @@\n"       # budget 18, far from exhausted
        "-x\n"
        "+y\n"
        "--- a/y.ts\n"            # adjacent pair start → boundary
        "+++ b/y.ts\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert _pfd(text) == [
        {"path": "x.ts", "additions": 1, "deletions": 1},
        {"path": "y.ts", "additions": 1, "deletions": 1},
    ]


def test_parse_no_index_two_files_via_diff_git_boundary():
    """Multi-file without Index: lines — hunk exhaustion + `diff --git`
    boundary move the section along."""
    text = (
        "diff --git a/one.ts b/one.ts\n"
        "--- a/one.ts\n"
        "+++ b/one.ts\n"
        "@@ -1,1 +1,1 @@\n"
        "-o\n"
        "+n\n"
        "diff --git a/two.ts b/two.ts\n"
        "--- a/two.ts\n"
        "+++ b/two.ts\n"
        "@@ -1,1 +1,1 @@\n"
        "-o2\n"
        "+n2\n"
    )
    assert _pfd(text) == [
        {"path": "one.ts", "additions": 1, "deletions": 1},
        {"path": "two.ts", "additions": 1, "deletions": 1},
    ]


def test_parse_zero_hunk_rename_only_segment():
    """Paired headers with NO @@ (rename-only) count with ±0."""
    text = "--- a/old.ts\n+++ b/new.ts\n"
    assert _pfd(text) == [{"path": "new.ts", "additions": 0, "deletions": 0}]


def test_parse_hunk_header_lines_never_counted():
    """`@@` headers themselves (and trailing context after the section) are
    never counted — only leading +/- body lines are."""
    text = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
        "@@ -5,1 +5,1 @@\n"
        "-c\n"
        "+d\n"
    )
    assert _pfd(text) == [{"path": "x.ts", "additions": 2, "deletions": 2}]


def test_parse_context_line_budget_consumes_two():
    """A context line consumes one old + one new budget slot (2)."""
    text = (
        "--- a/x.ts\n"
        "+++ b/x.ts\n"
        "@@ -1,2 +1,2 @@\n"      # budget 4
        " ctx\n"                  # 2
        " ctx\n"                  # 2 → exhausted; following + is OUTSIDE
        "+late addition\n"
    )
    assert _pfd(text) == [{"path": "x.ts", "additions": 0, "deletions": 0}]


def test_parse_crlf_line_endings_tolerated():
    text = (
        "--- a/x.ts\r\n"
        "+++ b/x.ts\r\n"
        "@@ -1,1 +1,1 @@\r\n"
        "-old\r\n"
        "+new\r\n"
    )
    assert _pfd(text) == [{"path": "x.ts", "additions": 1, "deletions": 1}]


# ---------------------------------------------------------------------------
# §4d B2 injection — edit parts get synthetic metadata.files + diffStats in
# the skeleton (capped 10 + filesTotal), gated on truncated.
# ---------------------------------------------------------------------------

def _edit_part(metadata, pid="p1", mid="m1"):
    return {"id": pid, "type": "tool", "messageID": mid, "tool": "edit",
            "state": {"status": "completed", "input": {"filePath": "x"},
                      "metadata": metadata}}


def _edit_out(metadata, **kw):
    src = [{"info": {"id": "m1"}, "parts": [_edit_part(metadata, **kw)]}]
    return skeleton_messages(src)[0]["parts"][0]


def _edit_diff(path="src/a.ts", body=" ctx\n-old\n+new\n", spec="-1,3 +1,3"):
    return (f"--- {path}\tt1\n+++ {path}\tt2\n@@ {spec} @@\n{body}")


def test_edit_injects_synthetic_files_and_diffstats():
    out = _edit_out({"diff": _edit_diff()})
    meta = out["state"]["metadata"]
    assert meta["files"] == [{"path": "src/a.ts", "additions": 1,
                              "deletions": 1}]
    assert meta["diffStats"] == {"additions": 1, "deletions": 1, "files": 1}
    # synthetic entries carry NO status — the diff text has no type info
    assert "status" not in meta["files"][0]


def test_edit_synthetic_files_cap_eleven():
    diffs = "\n".join(_edit_diff(f"src/f{i}.ts") for i in range(11))
    src = [{"info": {"id": "m1"}, "parts": [_edit_part({"diff": diffs})]}]
    out = skeleton_messages(src, sid="ses_s")[0]["parts"][0]
    meta = out["state"]["metadata"]
    assert len(meta["files"]) == 10
    assert meta["filesTotal"] == 11
    assert meta["diffStats"]["files"] == 11    # aggregate over ALL parsed
    assert any(r["category"] == "part_state_metadata_full"
               and r["partID"] == "p1" for r in out["expandRefs"])


def test_edit_truncated_metadata_skips_all_synthesis():
    out = _edit_out({"diff": _edit_diff(), "truncated": True})
    assert "files" not in out["state"].get("metadata", {})
    assert "diffStats" not in out["state"].get("metadata", {})


def test_edit_source_files_win_over_diff_parse():
    """Source metadata.files present → compact projection of the SOURCE,
    never the parsed diff (diff only eligible for the ③ diffStats leg)."""
    out = _edit_out({
        "diff": _edit_diff(),
        "files": [_apply_patch_files_entry("src/real.ts", additions=4,
                                           deletions=4)],
    })
    assert out["state"]["metadata"]["files"] == [
        {"path": "src/real.ts", "additions": 4, "deletions": 4,
         "status": "modified"}]
    # priority chain: files (②) also beat the diff parse (③) for diffStats
    assert out["state"]["metadata"]["diffStats"] == {
        "additions": 4, "deletions": 4, "files": 1}


def test_edit_non_diff_text_injects_nothing():
    out = _edit_out({"diff": "not a diff at all\n", "cursor": 1})
    assert "files" not in out["state"].get("metadata", {})
    assert "diffStats" not in out["state"].get("metadata", {})


def test_edit_synthetic_injection_survives_thresholding():
    """Injected fields are added AFTER the inline/omit threshold pass —
    never eligible for omission even with tiny limits."""
    from oc_slimapi.skeleton import SkeletonLimits

    tiny = SkeletonLimits(field_bytes=8, message_bytes=8)
    src = [{"info": {"id": "m1"},
            "parts": [_edit_part({"diff": _edit_diff(),
                                  "output": "x" * 100})]}]
    out = skeleton_messages(src, limits=tiny)[0]["parts"][0]
    assert out["state"]["metadata"]["files"][0]["path"] == "src/a.ts"
    assert out["state"]["metadata"]["diffStats"]["files"] == 1


# ---------------------------------------------------------------------------
# §5c.9-11 — purity, renderability, fingerprint.
# ---------------------------------------------------------------------------

def test_projection_does_not_mutate_source_and_is_idempotent():
    """§5c.9: skeleton projection never mutates/aliases the input; running
    it twice yields identical results (no state backfill)."""
    from copy import deepcopy

    src = [{"info": {"id": "m1"}, "parts": [
        _edit_part({"diff": _edit_diff()}),
        _compress_part({"content": [{"topic": "t"}]}),
        {"id": "p3", "type": "patch", "messageID": "m1", "hash": "h",
         "files": ["a.ts"]},
    ]}]
    frozen = deepcopy(src)
    first = skeleton_messages(src)
    second = skeleton_messages(src)
    assert first == second
    assert src == frozen            # no mutation, no backfill into source


def test_outputbytes_and_synthetic_fields_do_not_drive_renderability():
    """§5c.10: derived fields alone never make a part renderable —
    _is_renderable keeps reading only upstream-visible fields."""
    from oc_slimapi.skeleton import _is_renderable

    # a tool with NO tool name, title or input is a placeholder even if it
    # somehow acquired derived fields
    part = {"id": "p1", "type": "tool", "state": {
        "status": "completed",
        "outputBytes": 9999,
        "metadata": {"diffStats": {"additions": 1, "deletions": 1, "files": 1},
                     "files": [{"path": "a.ts"}]},
    }}
    assert _is_renderable(part) is False
    # and real content still renders
    assert _is_renderable({"id": "p2", "type": "tool", "tool": "bash",
                           "state": {}}) is True


def test_derived_fields_naturally_enter_fingerprint():
    """§5c.11: derived fields (synthetic files / diffStats / outputBytes /
    synthesized title) are covered by contentFingerprint — no
    FINGERPRINT_VERSION bump needed; equal sources → equal fingerprints."""
    src_a = [{"info": {"id": "m1"}, "parts": [_edit_part({"diff": _edit_diff()})]}]
    src_b = [{"info": {"id": "m1"}, "parts": [_edit_part({"diff": _edit_diff()})]}]
    out_a = skeleton_messages(src_a, fingerprint=True)[0]
    out_b = skeleton_messages(src_b, fingerprint=True)[0]
    assert out_a.get(FINGERPRINT_FIELD) == out_b.get(FINGERPRINT_FIELD)
    assert out_a.get(FINGERPRINT_FIELD)            # fingerprint present

    # different VISIBLE derived content (2 additions vs 1) → different
    # fingerprint. NOTE: a diff differing ONLY in omitted body text while
    # projecting to identical counts yields the SAME fingerprint — the
    # fingerprint is a function of the final representation (design §4.3).
    src_c = [{"info": {"id": "m1"},
              "parts": [_edit_part({"diff": _edit_diff(
                  body=" ctx\n-old\n+new\n+extra\n",
                  spec="-1,4 +1,4")})]}]
    out_c = skeleton_messages(src_c, fingerprint=True)[0]
    assert out_a.get(FINGERPRINT_FIELD) != out_c.get(FINGERPRINT_FIELD)
    assert out_c["parts"][0]["state"]["metadata"]["diffStats"] == {
        "additions": 2, "deletions": 1, "files": 1}


# ---------------------------------------------------------------------------
# Q7-P3-20 — 混合 NULL summary 行形状统一：canonical projector 与
# project_rows_to_v4_skeletons（gate 关遗留稀疏路径）同输入同形状。
# 契约 §13.1/:744 冻结 summary 为 {additions,deletions,files:number}|null
# （:777「对象时三子键均为数值」）→ 含 null 子值对象不合法，统一 null；
# 不模仿上游 fromRow（session.ts:59-68）的 ?? 0 填充——sidecar compact
# 语义不伪造计数。
# ---------------------------------------------------------------------------

from oc_slimapi.skeleton import (  # noqa: E402  (分区 import，同文件风格)
    canonical_session_skeleton_v4,
    project_rows_to_v4_skeletons,
)


def _summary_row(additions, deletions, files):
    """两路径通吃的 DB 记录形态（列恒在场，None = 业务 null）。"""
    return {
        "id": "ses_sum", "directory": "/foo", "parent_id": None,
        "project_id": None, "title": "t", "agent": None,
        "model": None, "time_created": 1, "time_updated": 2,
        "time_archived": None,
        "summary_additions": additions, "summary_deletions": deletions,
        "summary_files": files,
        "tokens_input": 10, "tokens_output": 20, "tokens_reasoning": 0,
        "tokens_cache_read": 0, "tokens_cache_write": 0, "revert": None,
    }


def test_summary_mixed_null_rows_unified_null_both_paths():
    """混合 NULL 行（部分列 NULL 部分有值）：两路径 summary 均为 null。
    canonical 另置 partial:true（§13.2b 来源不完整）；稀疏路径无
    partial 标记键（4.0.0 形态），summary 值形状一致即达成统一。"""
    for additions, deletions, files in (
        (5, None, 2),          # deletions NULL
        (None, 7, None),       # additions/files NULL
        (0, 3, None),          # 真实 0 与 NULL 混合
    ):
        row = _summary_row(additions, deletions, files)
        single = canonical_session_skeleton_v4(row)
        assert single is not None
        assert single["summary"] is None           # 禁发含 null 子值对象
        assert single["partial"] is True
        assert single["degraded"] is True
        [sparse] = project_rows_to_v4_skeletons([row])
        assert sparse["summary"] is None           # 两路径同形状
        assert "partial" not in sparse             # 稀疏形态无标记键


def test_summary_all_null_and_full_object_shapes_match():
    """全 NULL = 业务合法 null（两路径 null，canonical 不 partial）；
    三列全数值 = 完整对象（两路径同发三子键对象）。"""
    row = _summary_row(None, None, None)
    single = canonical_session_skeleton_v4(row)
    assert single["summary"] is None
    assert single["partial"] is False              # 业务 null ≠ 来源缺失
    [sparse] = project_rows_to_v4_skeletons([row])
    assert sparse["summary"] is None               # 旧代码发全 null 子值对象

    row = _summary_row(4, 9, 2)
    single = canonical_session_skeleton_v4(row)
    assert single["summary"] == {"additions": 4, "deletions": 9, "files": 2}
    assert single["partial"] is False
    [sparse] = project_rows_to_v4_skeletons([row])
    assert sparse["summary"] == {"additions": 4, "deletions": 9, "files": 2}
