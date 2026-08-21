"""v3 envelope helpers (v3-contract §4, Batch B).

The v3 envelope wraps the v2 bare-array payload WITHOUT re-parsing it:
``items`` is the byte-identical v2 serialization spliced between the
envelope keys, so a v3 body is exactly ``{"items":<v2 bytes>,…}`` — same
projection, same ordering, same bytes inside the array (contract §4.1
"逐字"). ``nextCursor`` carries the ``X-Next-Cursor`` value (string) or
``null``; the sessions envelope is built from the projected list with its
``X-Complete`` boolean.

Errors are never enveloped (§4.4) and 304 responses carry no body (§6.4) —
both are structural properties of the routes that call these helpers, not
of this module.
"""

from __future__ import annotations

import orjson


def messages_envelope_bytes(items_bytes: bytes, next_cursor: str | None) -> bytes:
    """``{"items":<v2 array bytes>,"nextCursor":<string|null>}`` (§4.1).

    Splices the packed v2 identity bytes verbatim — no re-parse, no
    re-serialization, key order fixed (items, nextCursor). This is also the
    canonical ETag input for the v3 messages route (§6.3).
    """
    cursor = orjson.dumps(next_cursor) if next_cursor is not None else b"null"
    return b'{"items":' + items_bytes + b',"nextCursor":' + cursor + b"}"


def sessions_envelope_v4(
    items: list[dict],
    next_cursor: str | None,
    complete: bool,
    *,
    degraded: bool = False,
    degraded_required: bool = False,
) -> dict[str, object]:
    """v4 sessions envelope (v4-contract §4.1 + §13.1 修订面)。

    Key order frozen ``(items, nextCursor, complete[, degraded])``。
    两种发布形态由 ``degraded_required`` 切换（§3.3 门控
    ``session.single.projection.v4``）：

    * ``degraded_required=False``（4.0.0 已发布形态）：``degraded`` 仅在
      DB-unavailable Class A fallback（§4.2）时出现且恒 true——DB-path
      200 无此键。
    * ``degraded_required=True``（§13.1 修订面）：``degraded`` 为 required
      布尔恒发（含 false；§4.1 可选形态随修订废止）。
    """
    payload: dict[str, object] = {
        "items": items,
        "nextCursor": next_cursor,
        "complete": complete,
    }
    if degraded or degraded_required:
        payload["degraded"] = bool(degraded)
    return payload
