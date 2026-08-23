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


def _snapshot_frame(
    key: PartKey, text: str | None, done: bool,
    part_revision: int | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "done": done,
    }
    if text is not None:
        payload["text"] = text
    if part_revision is not None:
        # Stage B (P0-3 partEventRevision): per-part frame-level
        # revision so a token-only subscriber can detect drift (vs. the
        # digest's per-message watermark). Omitted when the sidecar has
        # no cached revision (cold start / post reconnect) — preserves
        # the historical frame shape for back-compat.
        #
        # rev-ogpt CRITICAL 1 (Option B — per-FRAME): each emitted
        # frame (snapshot / delta / done marker / truncated) consumes
        # the next strictly-increasing revision for its part. No two
        # frames ever share a value, so a client using strict ``>`` on
        # ``partEventRevision`` reliably accepts every delivery (no
        # false-dedup). The field name is event-level in form but the
        # value is per-frame; the wire contract guarantees strict
        # monotonicity across consecutive deliveries for the same
        # ``(sessionID, messageID, partID)``.
        payload["partEventRevision"] = part_revision
    return sse_frame(payload, event="message.part.snapshot")


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
        # rev-ogpt CRITICAL 1 (Option B): see ``_snapshot_frame`` —
        # every delta frame gets its own strictly-increasing revision.
        # Multiple deltas across multiple flush windows of one part
        # therefore carry distinct values (0, 1, 2, ...).
        payload["partEventRevision"] = part_revision
    if seq is not None:
        # 4.12.0 修订六 B-1: the replay-domain publish seq, stamped into
        # the payload by the reserve→encode→append path so the frame is
        # self-describing on every wire version (additive JSON field —
        # v3 clients ignore unknown keys; same shape family as the
        # 4.11.0 partEventRevision additive). Always equal to the last
        # segment of the v4 ``id:`` line the frame is delivered with.
        # ``None`` (no replay log wired) omits the key — the v3-only
        # stack keeps its historical byte-identical frame shape.
        payload["seq"] = seq
    return sse_frame(payload, event="message.part.delta")


def _truncated_frame(
    key: PartKey, done: bool, part_revision: int | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "truncated": True,
        "done": done,
    }
    if part_revision is not None:
        # rev-ogpt CRITICAL 1 (Option B): the truncated frame consumes
        # its own revision (strictly greater than the previous delivery
        # for this part). When emitted after an oversized snapshot, the
        # snapshot's revision is "wasted" (frame never delivered) and
        # the truncated frame carries the NEXT value — clients using
        # strict ``>`` accept it because it is strictly greater than
        # their last-seen revision.
        payload["partEventRevision"] = part_revision
    return sse_frame(payload, event="message.part.snapshot")


def _resync_frame(sid: str, reason: str, seq: int | None = None) -> bytes:
    # NOTE: keep the payload dicts inline — the N1 resync-reason gate
    # (tests/test_resync_reason_gate.py) only resolves literal dicts.
    if seq is None:
        return sse_frame({"reason": reason, "sessionID": sid}, event="resync")
    # 4.12.0 修订六 B-2: a REPLAYABLE business resync (currently only
    # ``token_memory_limit``) carries the payload seq exactly like a
    # delta frame — stamped by the reserve→encode→append path, equal
    # to the ``id:`` line's last segment on the v4 wire, omitted on
    # no-replay-log stacks.
    return sse_frame(
        {"reason": reason, "sessionID": sid, "seq": seq}, event="resync",
    )


def _connected_frame(sid: str) -> bytes:
    return sse_frame({"sessionID": sid}, event="server.connected")


def _heartbeat_frame() -> bytes:
    return sse_frame({}, event="server.heartbeat")


def _message_removed_frame(sid: str, mid: str, seq: int | None = None) -> bytes:
    """Stage B v0.6 §P.4 (MAJOR 4 方案 C): tombstone frame for an upstream
    ``message.removed`` event. Tells token-stream subscribers to drop all
    local stream state for ``(sid, mid)`` — the message is gone upstream,
    further deltas / snapshots for it would be orphan.

    Payload is the minimal ``{sessionID, messageID}`` (mirrors the upstream
    flat-props shape); no partID because the tombstone is message-scoped.
    Stamped into the bounded replay queue (``_removed_messages``) so a
    client that attaches AFTER the removal still learns about it during
    the handshake (``server.connected`` → ``message.removed`` batch →
    snapshot live → enter fanout).

    4.12.0 修订六 B-1: ``seq`` (replay-domain publish seq, additive
    payload field) is stamped when a replay log is wired; ``None`` keeps
    the historical v3-only handshake-replay shape byte-identical.
    """
    payload: dict[str, Any] = {"sessionID": sid, "messageID": mid}
    if seq is not None:
        payload["seq"] = seq
    return sse_frame(payload, event="message.removed")
