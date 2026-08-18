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


def sessions_envelope_payload(
    sessions: list[dict], complete: bool,
) -> dict[str, object]:
    """``{"items":[...],"complete":<bool>}`` (§4.2).

    The payload dict preserves the contract key order (items, complete);
    ``complete`` inherits the v2 ``X-Complete`` semantics verbatim,
    including its non-authoritative nature (never a statement about the
    authoritative full set). Serialized with the route's normal
    ``json_response`` so gzip / Vary behave exactly like the v2 path.
    """
    return {"items": sessions, "complete": complete}


def sessions_envelope_v4(
    items: list[dict],
    next_cursor: str | None,
    complete: bool,
    *,
    degraded: bool = False,
) -> dict[str, object]:
    """v4 sessions envelope (v4-contract §4.1): key order frozen
    ``(items, nextCursor, complete[, degraded])``.

    ``degraded`` is ONLY present (and true) on the DB-unavailable Class A
    fallback (§4.2) — never a key on the DB-path 200. No ETag/Vary/304 on
    any v4 sessions response (§4.4).
    """
    payload: dict[str, object] = {
        "items": items,
        "nextCursor": next_cursor,
        "complete": complete,
    }
    if degraded:
        payload["degraded"] = True
    return payload
