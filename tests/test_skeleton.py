import json
from pathlib import Path

import orjson

from oc_slimapi.config import settings as _skel_config
from oc_slimapi.skeleton import (
    PLACEHOLDER_TEXT,
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
    allowed_metadata = {"sessionId", "sessionID", "description", "agent", "diffStats"}

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

    assert result[0]["parts"] == [{
        "id": "thin_placeholder_m1",
        "messageID": "m1",
        "type": "text",
        "text": PLACEHOLDER_TEXT,
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
    """Filediff dict missing additions/deletions → defaults to 0."""
    result = _cd({"file": "src/foo.ts"})
    assert result == {"additions": 0, "deletions": 0, "files": 1}


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
    """Filediff without additions/deletions → diffStats with 0s."""
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
    assert tool["state"]["metadata"]["diffStats"] == {"additions": 0, "deletions": 0, "files": 1}


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
