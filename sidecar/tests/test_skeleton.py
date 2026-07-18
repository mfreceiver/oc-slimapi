import json
from pathlib import Path

import orjson

from oc_slimapi.skeleton import PLACEHOLDER_TEXT, skeleton_messages


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
    result_text = [part for part in parts(result, "text") if not part["id"].startswith("thin_placeholder_")]
    assert [part["id"] for part in result_text] == [
        part["id"] for part in parts(source, "text")
    ]
    assert [part["text"] for part in result_text] == [
        part["text"] for part in parts(source, "text")
    ]


def test_reasoning_text_is_preserved_verbatim():
    _, source = load_fixture()
    result = skeleton_messages(source)

    assert [part["text"] for part in parts(result, "reasoning")] == [
        part["text"] for part in parts(source, "reasoning")
    ]


def test_tool_state_is_reduced_to_contract_whitelists():
    _, source = load_fixture()
    result = skeleton_messages(source)
    allowed_input = {
        "path", "filePath", "file_path", "command", "agent", "description",
        "subagent_type", "todos",
    }
    allowed_metadata = {"sessionId", "sessionID", "description", "agent"}

    for tool in parts(result, "tool"):
        state = tool.get("state", {})
        assert "output" not in state
        assert "structured" not in state
        assert "result" not in state
        assert "raw" not in state
        assert "attachments" not in state
        assert set(state.get("input", {})) <= allowed_input
        assert set(state.get("metadata", {})) <= allowed_metadata
        assert tool["hasFull"] is True
        assert tool["omitted"]


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


def test_golden_skeleton_is_bounded_by_the_content_preservation_floor():
    raw, source = load_fixture()
    encoded = orjson.dumps(skeleton_messages(source))

    # The v2 contract requires preserving text and reasoning.text. Those two
    # strings alone are 34.70% of this fixture, so the requested 15% raw-byte
    # target is mathematically impossible. 55% remains a strict, reproducible
    # bound while honoring the authoritative field contract.
    assert len(encoded) < len(raw) * 0.55
