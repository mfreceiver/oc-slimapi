"""Wire frame builders for the token stream (design §5.6).

Moved from :mod:`oc_slimapi.sse.token_hub`.
"""
from __future__ import annotations

import time
from typing import Any

import orjson

# Key for a single text part within a session+message.
PartKey = tuple[str, str, str]  # (sessionID, messageID, partID)


# Terminal sentinel enqueued by the overflow path so the SSE generator tears
# the connection down promptly (mirrors hub.STOP; kept local to avoid a
# runtime import cycle — hub.py imports this module only under TYPE_CHECKING).
STOP = object()


def _now_ms() -> int:
    """Epoch milliseconds.

    Duplicated from :mod:`oc_slimapi.sse.hub` deliberately: ``hub.py``
    references :class:`TokenStreamHub` only under ``TYPE_CHECKING``, but a
    runtime ``from .hub import _now_ms`` here would still create a cycle at
    import time. The helper is one line; keeping it local avoids the dance.
    """
    return int(time.time() * 1000)


def sse_frame(payload: dict[str, Any], event: str | None = None) -> bytes:
    """Serialize ``payload`` as one SSE frame.

    Duplicated from :mod:`oc_slimapi.sse.hub` for the same import-cycle
    reason as :func:`_now_ms` (and to keep this module's wire format
    self-contained). The format is stable: ``event: <name>\\n`` (optional) +
    ``data: <json>\\n\\n``. Both copies share :mod:`orjson` so JSON encoding
    (key order, UTF-8, escaping) is byte-identical.
    """
    prefix = f"event: {event}\n" if event else ""
    return prefix.encode() + b"data: " + orjson.dumps(payload) + b"\n\n"


# ---------------------------------------------------------------------------
# Wire frame builders (design §5.6). Payload key order matches the spec so
# snapshot/delta frames are byte-stable for snapshot tests. ``text`` is omitted
# from the terminal marker (lever 1).
# ---------------------------------------------------------------------------


def _snapshot_frame(key: PartKey, text: str | None, done: bool) -> bytes:
    payload: dict[str, Any] = {
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "done": done,
    }
    if text is not None:
        payload["text"] = text
    return sse_frame(payload, event="message.part.snapshot")


def _delta_frame(key: PartKey, text: str) -> bytes:
    return sse_frame(
        {"sessionID": key[0], "messageID": key[1], "partID": key[2], "text": text},
        event="message.part.delta",
    )


def _truncated_frame(key: PartKey, done: bool) -> bytes:
    return sse_frame(
        {
            "sessionID": key[0],
            "messageID": key[1],
            "partID": key[2],
            "truncated": True,
            "done": done,
        },
        event="message.part.snapshot",
    )


def _resync_frame(sid: str, reason: str) -> bytes:
    return sse_frame({"reason": reason, "sessionID": sid}, event="resync")


def _connected_frame(sid: str) -> bytes:
    return sse_frame({"sessionID": sid}, event="server.connected")


def _heartbeat_frame() -> bytes:
    return sse_frame({}, event="server.heartbeat")
