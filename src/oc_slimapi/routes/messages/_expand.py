"""design-expand §2.2 fragment family — the 12 extractors + registry
table + the two expand routes (F-302 three-family split of
``routes/messages.py``; pure move, zero behaviour change — the shared
single-flight fetch comes from :mod:`._full_merge` via
``_fetch_full_shared``).
"""

from __future__ import annotations

import time

import orjson
from fastapi import Request
from starlette.responses import Response

from ...errors import CodedHTTPException
from ...gzip_util import compress_if_beneficial, error_response
from ...skeleton import _files_from_diff_text
from ...traffic import EXPAND_CATEGORIES as _EXPAND_CATEGORIES
from ...traffic import EXPAND_CATEGORIES_SET as _EXPAND_CATEGORIES_SET
from ...transform import TransformBusy
from ...upstream_errors import raise_upstream_unavailable
from ._full_merge import _fetch_full_shared
from ._router import _busy_response, _resolve_messages_directory, router

# ---------------------------------------------------------------------------
# design-expand §2.2 — expand fragment endpoints.
#
# On-demand part/message expansion of a single upstream message body, served
# from the SAME single-flight full fetch as /full/{mid} (the key embeds
# (pool, sid, mid, directory)), so an expand request and a concurrent /full
# for the same message share ONE upstream GET (§3.5). The category is a
# plain str path param validated against a manual whitelist — a FastAPI Enum
# path param would surface as a raw 422, which the v3 contract does not
# want; the contract wire shape is 400 invalid_expand_category.
# ---------------------------------------------------------------------------

# §2.2 — the 12 frozen categories. SINGLE SOURCE OF TRUTH is
# ``oc_slimapi.traffic.EXPAND_CATEGORIES`` (design-expand §2.2 table order —
# order is part of the wire contract: validCategories / expectedTypes lists
# are emitted in this order). The versions route and the traffic accounting
# whitelist import the same constant; the route must never hold a private
# copy or category additions/removals would drift between the wire
# advertisement, the ledger and the endpoint (rev-gpt R1 M1).


# §2.2 level split: only ``info_summary_diffs`` is message-level.
_EXPAND_MESSAGE_LEVEL_CATEGORIES = frozenset({"info_summary_diffs"})

# §2.2 — applicable message-part types per part-level category.
_EXPAND_APPLICABLE_TYPES: dict[str, tuple[str, ...]] = {
    "part_text": ("text",),
    "part_reasoning": ("reasoning",),
    "part_state_output": ("tool",),
    "part_state_error": ("tool",),
    "part_state_input_full": ("tool",),
    "part_state_metadata_full": ("tool",),
    "part_state_attachments": ("tool",),
    "part_url": ("file",),
    "part_source": ("file",),
    "part_snapshot": ("step-start", "step-finish"),
    "compaction_full": ("compaction",),
}


def _expand_shape_error() -> None:
    """§3.3 — parsed-but-malformed upstream body → 502 upstream_invalid_shape."""
    raise CodedHTTPException(502, code="upstream_invalid_shape")


def _expand_locate_part(message: dict, part_id: str) -> dict:
    """§3.1 step 5 — locate ``part_id`` in the parsed message parts.

    parts missing / null / scalar / non-object element / duplicate partID →
    502 upstream_invalid_shape (parsed yet structurally malformed); a well
    formed list simply not containing ``part_id`` → 404 the contract's
    expand_target_not_found (reason: part_missing).
    """
    parts = message.get("parts")
    if not isinstance(parts, list):
        _expand_shape_error()
    found = None
    seen: set[str] = set()
    for item in parts:
        if not isinstance(item, dict):
            _expand_shape_error()
        pid = item.get("id")
        if not isinstance(pid, str) or not pid:
            # part without a usable id (missing / non-string / empty string,
            # rev-gpt R1 m1) — cannot match, same 502 as the other unusable
            # id forms (matches lane A's falsy-id skeleton guard semantics).
            _expand_shape_error()
        if pid in seen:
            _expand_shape_error()  # duplicate partID
        seen.add(pid)
        if pid == part_id:
            found = item
    if found is None:
        raise CodedHTTPException(
            404, code="expand_target_not_found", reason="part_missing",
        )
    return found


def _expand_str_field(obj: dict, field: str) -> str | None:
    """§3.3 nested-type rule for string fields: missing/null → null key;
    present but non-string → 502 upstream_invalid_shape."""
    value = obj.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        _expand_shape_error()
    return value


def _extract_info_summary_diffs(message: dict, _part: dict | None) -> dict:
    """info.summary.diffs → {diffs: FileDiff[]|null} (§2.2, message-level)."""
    info = message.get("info")
    if info is None:
        return {"diffs": None}
    if not isinstance(info, dict):
        _expand_shape_error()
    summary = info.get("summary")
    if summary is None:
        return {"diffs": None}
    if not isinstance(summary, dict):
        _expand_shape_error()
    diffs = summary.get("diffs")
    if diffs is None:
        return {"diffs": None}
    if not isinstance(diffs, list) or not all(
        isinstance(d, dict) for d in diffs
    ):
        _expand_shape_error()
    return {"diffs": diffs}


def _extract_part_text(_message: dict, part: dict) -> dict:
    """part.text → {text: string|null} (text parts)."""
    return {"text": _expand_str_field(part, "text")}


def _extract_part_reasoning(_message: dict, part: dict) -> dict:
    """part.text → {text: string|null} (reasoning parts)."""
    return {"text": _expand_str_field(part, "text")}


def _expand_state(part: dict) -> dict | None:
    """Tool state accessor — state missing/null → None (null data key);
    present but not an object → 502 (state 标量, §3.3)."""
    state = part.get("state")
    if state is None:
        return None
    if not isinstance(state, dict):
        _expand_shape_error()
    return state


def _extract_part_state_output(_message: dict, part: dict) -> dict:
    """state.output → {output: string|null} (tool, ToolStateCompleted)."""
    state = _expand_state(part)
    return {"output": _expand_str_field(state, "output") if state else None}


def _extract_part_state_error(_message: dict, part: dict) -> dict:
    """state.error → {error: string|null} (tool, ToolStateError)."""
    state = _expand_state(part)
    return {"error": _expand_str_field(state, "error") if state else None}


def _extract_part_state_input_full(_message: dict, part: dict) -> dict:
    """state.input → {input: object|null}; input non-object → 502 (§3.3)."""
    state = _expand_state(part)
    if state is None:
        return {"input": None}
    value = state.get("input")
    if value is None:
        return {"input": None}
    if not isinstance(value, dict):
        _expand_shape_error()
    return {"input": value}


def _extract_part_state_metadata_full(_message: dict, part: dict) -> dict:
    """state.metadata → {metadata: object|null}, with the never-consumed LSP
    ``diagnostics`` map dropped (same strip as /full §2.1 / P3).

    §4d B2 (P1-3 + P1-N6): for ``edit`` parts whose source metadata carries
    no ``files`` of its own, the diff text is parsed and the COMPLETE
    synthetic files list (NO cap — the cap is skeleton-view only) is added
    under ``metadata.files``. Eligibility is the SAME guard as the skeleton
    projection: tool == edit, source has no ``files`` key,
    ``truncated`` is not true, diff parses. Additive — every other key is
    returned exactly as upstream sent it."""
    state = _expand_state(part)
    if state is None:
        return {"metadata": None}
    value = state.get("metadata")
    if value is None:
        return {"metadata": None}
    if not isinstance(value, dict):
        _expand_shape_error()
    metadata = {
        key: item for key, item in value.items() if key != "diagnostics"
    }
    if (part.get("tool") == "edit"
            and "files" not in value
            and value.get("truncated") is not True):
        parsed_files = _files_from_diff_text(value.get("diff"))
        if parsed_files:
            metadata["files"] = parsed_files
    return {"metadata": metadata}


def _extract_part_state_attachments(_message: dict, part: dict) -> dict:
    """state.attachments → {attachments: object[]|null}; non-array or a
    non-object element → 502 (design-expand §3.3 frozen schema: the array
    shape is object[] — each element must be an object, element-level
    validation mirrors the other nested-type checks, rev-sgpt M2)."""
    state = _expand_state(part)
    if state is None:
        return {"attachments": None}
    value = state.get("attachments")
    if value is None:
        return {"attachments": None}
    if not isinstance(value, list):
        _expand_shape_error()
    if not all(isinstance(item, dict) for item in value):
        _expand_shape_error()  # every element must be an object
    return {"attachments": value}


def _extract_part_url(_message: dict, part: dict) -> dict:
    """part.url → {url: string|null} (file parts)."""
    return {"url": _expand_str_field(part, "url")}


def _extract_part_source(_message: dict, part: dict) -> dict:
    """part.source → {source: object|null}; non-object → 502."""
    source = part.get("source")
    if source is None:
        return {"source": None}
    if not isinstance(source, dict):
        _expand_shape_error()
    return {"source": source}


def _extract_part_snapshot(_message: dict, part: dict) -> dict:
    """part.snapshot → {snapshot: string|null} (step-start/step-finish)."""
    return {"snapshot": _expand_str_field(part, "snapshot")}


def _extract_compaction_full(_message: dict, part: dict) -> dict:
    """The COMPLETE compaction part, minus the sidecar-injected
    ``expandRefs`` key (§2.2 / §3.3 — whitelist-built, never blacklist)."""
    return {key: value for key, value in part.items() if key != "expandRefs"}


_EXPAND_EXTRACTORS = {
    "info_summary_diffs": _extract_info_summary_diffs,
    "part_text": _extract_part_text,
    "part_reasoning": _extract_part_reasoning,
    "part_state_output": _extract_part_state_output,
    "part_state_error": _extract_part_state_error,
    "part_state_input_full": _extract_part_state_input_full,
    "part_state_metadata_full": _extract_part_state_metadata_full,
    "part_state_attachments": _extract_part_state_attachments,
    "part_url": _extract_part_url,
    "part_source": _extract_part_source,
    "part_snapshot": _extract_part_snapshot,
    "compaction_full": _extract_compaction_full,
}


def _expand_fragment_worker(
    body: bytes, *, category: str, mid: str, part_id: str | None,
    limit: int, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """§3.1 steps 4d-7 + §3.2/§6 — parse, locate, extract, serialize, gzip.

    Runs off-thread under pool admission (mirrors /full). Decode failure or
    top-level non-dict → propagates ValueError/JSONDecodeError which the
    route maps to 503 upstream_unavailable; parsed-but-malformed structures
    raise CodedHTTPException 502/404/400/413 lines directly.
    """
    try:
        message = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise ValueError("expand source body is not JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("expand source body is not a dict")
    part: dict | None = None
    if part_id is not None:
        part = _expand_locate_part(message, part_id)
        # §3.1 step 6 — type fitness for the requested category.
        applicable = _EXPAND_APPLICABLE_TYPES[category]
        if part.get("type") not in applicable:
            raise CodedHTTPException(
                400, code="expand_category_mismatch",
                expectedTypes=list(applicable),
            )
    data = _EXPAND_EXTRACTORS[category](message, part)
    envelope: dict = {
        "category": category,
        "messageID": mid,
        "data": data,
    }
    if part_id is not None:
        envelope["partID"] = part_id
    identity = orjson.dumps(envelope)
    # §3.2 — fragment byte cap on the serialized identity (before gzip).
    if len(identity) > limit:
        raise CodedHTTPException(
            413, code="expand_fragment_too_large", limitBytes=limit,
        )
    return compress_if_beneficial(identity, accept_encoding)


async def _expand_fragment(
    request: Request, sid: str, category: str, mid: str,
    part_id: str | None, directory: str | None,
) -> Response:
    """Shared implementation for the two expand routes (§3.1 strict order)."""
    accept_encoding = request.headers.get("accept-encoding")

    # §3.1 step 1 — category whitelist (plain str, never FastAPI Enum).
    if category not in _EXPAND_CATEGORIES_SET:
        return error_response(
            "invalid_expand_category", 400,
            validCategories=list(_EXPAND_CATEGORIES),
            accept_encoding=accept_encoding,
        )

    # §3.1 step 2 — level match: message-level category without partID,
    # part-level category with a partID, or level/category mismatch.
    if category in _EXPAND_MESSAGE_LEVEL_CATEGORIES:
        if part_id is not None:
            return error_response(
                "expand_category_mismatch", 400,
                expectedLevel="message",
                accept_encoding=accept_encoding,
            )
    elif part_id is None:
        return error_response(
            "expand_category_mismatch", 400,
            expectedLevel="part",
            accept_encoding=accept_encoding,
        )

    directory = await _resolve_messages_directory(request, directory)
    config = request.app.state.config
    pool = request.app.state.transforms
    # §3.2 — fragment cap; lane C adds the real config value, keep the
    # fallback until integration (config.py is out of this lane's write set).
    fragment_limit = getattr(
        config, "max_expand_response_bytes", 8 * 1024 * 1024,
    )
    try:
        # §3.1 step 3 — transform pool admission (mirrors /full absorb loop:
        # pool-full 503 transform_busy precedes every part-level 40x).
        deadline = time.monotonic() + config.transform_absorb_budget_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransformBusy()
            try:
                await pool.acquire(min(config.transform_wait_seconds, remaining))
            except TransformBusy:
                continue  # narrow the next attempt to the remaining budget
            break
        try:
            # §3.1 step 4 — shared single-flight upstream GET ((pool, sid,
            # mid, directory) key — dedupes with concurrent /full).
            body = await _fetch_full_shared(request, pool, sid, mid, directory)
            if body is None:
                # §3.1 4c — source body over max_message_bytes: 413 BEFORE
                # any JSON decode (oversize + malformed body still 413, the
                # cap-read ran first — R4-M1).
                return error_response(
                    "expand_source_too_large", 413,
                    limitBytes=config.max_message_bytes,
                    accept_encoding=accept_encoding,
                )
            # §3.1 4d-7 — decode/locate/extract/serialize/gzip off-thread.
            try:
                encoded, extra = await pool.offload(
                    _expand_fragment_worker, body,
                    category=category, mid=mid, part_id=part_id,
                    limit=fragment_limit, accept_encoding=accept_encoding,
                )
            except (orjson.JSONDecodeError, ValueError) as exc:
                # §3.1 4d — decode failure / top-level non-dict → 503.
                raise_upstream_unavailable(exc)
        finally:
            pool.release()
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={"Cache-Control": "no-store", **extra},
        )
    except TransformBusy:
        return _busy_response(accept_encoding)


@router.get("/expand/{category}/{mid}")
async def expand_message_fragment(
    request: Request, sid: str, category: str, mid: str,
    directory: str | None = None,
):
    """design-expand §2 — message-level expand fragment.

    Only ``info_summary_diffs`` is message-level; every other category needs
    a partID and 400s here (expand_category_mismatch, expectedLevel=part).
    """
    return await _expand_fragment(
        request, sid, category, mid, None, directory,
    )


@router.get("/expand/{category}/{mid}/{partID}")
async def expand_part_fragment(
    request: Request, sid: str, category: str, mid: str, partID: str,
    directory: str | None = None,
):
    """design-expand §2 — part-level expand fragment.

    ``info_summary_diffs`` with a partID 400s (expand_category_mismatch,
    expectedLevel=message); unknown partIDs → 404 expand_target_not_found.
    """
    return await _expand_fragment(
        request, sid, category, mid, partID, directory,
    )
