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
# Native-v4 wire frame builders (design §5.6).
# ---------------------------------------------------------------------------


def _delta_frame(
    key: PartKey, text: str, part_revision: int | None = None,
    seq: int | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "text": text,
    }
    if part_revision is not None:
        # Every delta frame gets its own strictly-increasing revision.
        # Multiple deltas across multiple flush windows of one part
        # therefore carry distinct values (0, 1, 2, ...).
        payload["partEventRevision"] = part_revision
    if seq is not None:
        # 4.12.0 修订六 B-1: the replay-domain publish seq, stamped into
        # the payload by the reserve→encode→append path so the frame is
        # self-describing on native v4. Always equal to the last segment of
        # the ``id:`` line the frame is delivered with. ``None`` is retained
        # only for route-private/unit builder use; published deltas supply it.
        payload["seq"] = seq
    return sse_frame(payload, event="message.part.delta")


def _resync_frame(sid: str, reason: str, seq: int | None = None) -> bytes:
    # NOTE: keep the payload dicts inline — the N1 resync-reason gate
    # (tests/test_resync_reason_gate.py) only resolves literal dicts.
    if seq is None:
        return sse_frame({"reason": reason, "sessionID": sid}, event="resync")
    # 4.12.0 修订六 B-2: a REPLAYABLE business resync (currently only
    # ``token_memory_limit``) carries the payload seq exactly like a
    # delta frame — stamped by the reserve→encode→append path, equal
    # to the ``id:`` line's last segment on the v4 wire. Route-private
    # resync controls omit it; replayable business resyncs always supply it.
    return sse_frame(
        {"reason": reason, "sessionID": sid, "seq": seq}, event="resync",
    )
def _heartbeat_frame() -> bytes:
    return sse_frame({}, event="server.heartbeat")


def _message_removed_frame(sid: str, mid: str, seq: int | None = None) -> bytes:
    """Stage B v0.6 §P.4 (MAJOR 4 方案 C): tombstone frame for an upstream
    ``message.removed`` event. Tells token-stream subscribers to drop all
    local stream state for ``(sid, mid)`` — the message is gone upstream,
    further deltas for it would be orphan.

    Payload is the minimal ``{sessionID, messageID}`` (mirrors the upstream
    flat-props shape); no partID because the tombstone is message-scoped.
    ``seq`` is stamped by the mandatory ReplayLog publish path.
    """
    payload: dict[str, Any] = {"sessionID": sid, "messageID": mid}
    if seq is not None:
        payload["seq"] = seq
    return sse_frame(payload, event="message.removed")
