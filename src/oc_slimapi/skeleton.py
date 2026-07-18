"""Pure v2-contract message/session projection functions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PLACEHOLDER_TEXT = "[内容已折叠，点开查看]"
PART_IDS = {"id", "type", "messageID", "sessionID"}
TOOL_KEYS = PART_IDS | {"tool", "callID"}
TOOL_INPUT_KEYS = {
    "path", "filePath", "file_path", "command", "agent", "description",
    "subagent_type", "todos",
}
TOOL_METADATA_KEYS = {"sessionId", "sessionID", "description", "agent"}
FILE_URL_LIMIT = 8 * 1024
COMPACTION_PART_LIMIT = 64 * 1024


def _pick(value: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _mark(part: dict[str, Any], omitted: list[str]) -> dict[str, Any]:
    if omitted:
        part["hasFull"] = True
        part["omitted"] = sorted(set(omitted))
    return part


def _tool(part: dict[str, Any]) -> dict[str, Any]:
    result = _pick(part, TOOL_KEYS)
    omitted: list[str] = []
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        source_input = state.get("input")
        if isinstance(source_input, dict):
            thin_input = _pick(source_input, TOOL_INPUT_KEYS)
            if thin_input:
                thin_state["input"] = thin_input
            omitted.extend(
                f"state.input.{key}" for key in source_input if key not in TOOL_INPUT_KEYS
            )
        elif source_input is not None:
            omitted.append("state.input")
        source_metadata = state.get("metadata")
        if isinstance(source_metadata, dict):
            thin_metadata = _pick(source_metadata, TOOL_METADATA_KEYS)
            if thin_metadata:
                thin_state["metadata"] = thin_metadata
            omitted.extend(
                f"state.metadata.{key}"
                for key in source_metadata if key not in TOOL_METADATA_KEYS
            )
        for key in ("output", "structured", "result", "raw", "attachments", "error"):
            if key in state:
                omitted.append(f"state.{key}")
        result["state"] = thin_state
    for key in part:
        if key not in TOOL_KEYS and key != "state":
            omitted.append(key)
    return _mark(result, omitted)


def _patch(part: dict[str, Any]) -> dict[str, Any]:
    result = _pick(part, PART_IDS)
    omitted: list[str] = []
    files = part.get("files")
    if isinstance(files, list):
        result["files"] = [
            _pick(item, {"path", "additions", "deletions", "status"})
            for item in files if isinstance(item, dict)
        ]
    metadata = part.get("metadata")
    if isinstance(metadata, dict) and "path" in metadata:
        result["metadata"] = {"path": deepcopy(metadata["path"])}
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        source_input = state.get("input")
        if isinstance(source_input, dict):
            path_input = _pick(source_input, {"path", "filePath", "file_path"})
            if path_input:
                thin_state["input"] = path_input
            omitted.extend(
                f"state.input.{key}"
                for key in source_input if key not in {"path", "filePath", "file_path"}
            )
        if "output" in state:
            omitted.append("state.output")
        result["state"] = thin_state
    for key in part:
        if key not in PART_IDS | {"files", "metadata", "state"}:
            omitted.append(key)
    return _mark(result, omitted)


def _file(part: dict[str, Any]) -> dict[str, Any]:
    result = _pick(part, PART_IDS | {"filename", "mime"})
    omitted: list[str] = []
    url = part.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) <= FILE_URL_LIMIT:
        result["url"] = url
    elif "url" in part:
        result["url"] = None
        omitted.append("url")
    if "source" in part:
        omitted.append("source")
    for key in part:
        if key not in PART_IDS | {"filename", "mime", "url", "source"}:
            omitted.append(key)
    return _mark(result, omitted)


def skeleton_part(part: dict[str, Any]) -> dict[str, Any]:
    part_type = part.get("type")
    if part_type == "text":
        return deepcopy(part)
    if part_type == "reasoning":
        result = _pick(part, PART_IDS | {"text"})
        return _mark(result, [key for key in part if key not in PART_IDS | {"text"}])
    if part_type == "tool":
        return _tool(part)
    if part_type == "patch":
        return _patch(part)
    if part_type == "file":
        return _file(part)
    if part_type in {"step-start", "step-finish"}:
        return _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS])
    if part_type == "compaction":
        copied = deepcopy(part)
        # Compaction is retained unless the single part violates its explicit cap.
        import orjson
        if len(orjson.dumps(copied)) <= COMPACTION_PART_LIMIT:
            return copied
        return _mark(_pick(part, PART_IDS), ["*"])
    return _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS] or ["*"])


def skeleton_message(message: dict[str, Any]) -> dict[str, Any]:
    result = {"info": deepcopy(message.get("info", {}))}
    source_parts = message.get("parts")
    thin_parts = [skeleton_part(part) for part in source_parts or [] if isinstance(part, dict)]
    if not any(_is_renderable(part) for part in thin_parts):
        message_id = result["info"].get("id", "unknown")
        thin_parts.append({
            "id": f"thin_placeholder_{message_id}",
            "messageID": message_id,
            "type": "text",
            "text": PLACEHOLDER_TEXT,
            "hasFull": True,
            "omitted": ["parts"],
        })
    result["parts"] = thin_parts
    return result


def skeleton_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [skeleton_message(message) for message in messages]


def _is_renderable(part: dict[str, Any]) -> bool:
    part_type = part.get("type")
    if part_type in {"text", "reasoning"}:
        return bool(part.get("text"))
    if part_type == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        return bool(part.get("tool") or state.get("title") or state.get("input"))
    if part_type == "patch":
        return bool(part.get("files") or part.get("metadata") or part.get("state"))
    if part_type == "file":
        return bool(part.get("filename") or part.get("url"))
    return False


SESSION_KEYS = {
    "id", "directory", "parentID", "projectID", "title", "agent", "model",
}


def skeleton_session(session: dict[str, Any]) -> dict[str, Any]:
    result = _pick(session, SESSION_KEYS)
    for key, allowed in (
        ("time", {"created", "updated", "archived"}),
        ("summary", {"additions", "deletions", "files"}),
        ("revert", {"messageID", "partID"}),
    ):
        value = session.get(key)
        if isinstance(value, dict):
            result[key] = _pick(value, allowed)
    return result
