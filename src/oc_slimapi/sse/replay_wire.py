"""v4 SSE replay **wire layer** (design-v4-sse-replay §4 / v4-contract §7).

B3b-2 scope — everything the pure-data :mod:`oc_slimapi.sse.replay_log`
deliberately excluded:

* ``id:`` header generation (``g:<epoch>:<seq>`` / ``t:<sid>:<epoch>:<seq>``
  — the domain key already mirrors the label grammar, so the wire id is
  ``<domain>:<epoch>:<seq>`` for both);
* Last-Event-ID parsing + classification steps **① syntax** and
  **② endpoint/sid domain match** (steps ③ epoch / ④ barrier→window→gap
  live in :meth:`ReplayLog.replay` — this helper delegates to it in the
  frozen short-circuit order);
* the additive v4 ``slimapi.meta`` extension fields (B3b-4: capabilities
  summary + epoch + seqBase — the meta frame itself never carries an
  ``id:``);
* the periodic replay-log maintenance loop (TTL GC + barrier GC —
  design §3.4/§3.6), wired in ``app.py`` on the existing background-task
  pattern.

Frozen behaviours (v4-contract §7.0/§7.1/§7.2):

* ①② violations (malformed id / cross-endpoint label / cross-sid label)
  are **ignore + reset** — the connection proceeds with first-connect
  semantics, no error, no resync frame (a client protocol violation is
  not a server state change);
* replay frames (when any) are handed to the client in strictly
  increasing ``seq`` order, each with its ``id:`` line, BEFORE any
  post-connection frame — and the server NEVER sends snapshot frames;
* v3 scopes never reach this module (routes gate on the wire view).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from .hub_types import V4_RESYNC_REASONS
from .replay_log import (
    GLOBAL_DOMAIN,
    ReplayOutcome,
    RESYNC_EPOCH_CHANGED,
    RESYNC_REPLAY_EXPIRED,
    RESYNC_REPLAY_GAP,
    RESYNC_RECONNECT_NO_REPLAY,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from .replay_log import ReplayLog

__all__ = [
    "META_CAPABILITY_KEYS",
    "V4_RESYNC_REASONS",
    "classify_reconnect",
    "frame_with_id",
    "meta_v4_extension",
    "parse_last_event_id",
    "replay_sweep_loop",
    "sse_id_line",
]

# V4_RESYNC_REASONS is defined in :mod:`.hub_types` (W1-D leaf anchor for
# the resync reason value domain); re-exported here unchanged because the
# subscriber/hub v4 branches and the wire tests import it from this module.

# §7.1 syntax: epoch = exactly 16 lowercase hex chars; seq = decimal digits
# (leading zeros tolerated — the value domain is the integer, the ID is a
# string the server compares only by equality against its own grammar).
_EPOCH_RE = re.compile(r"^[0-9a-f]{16}$")
_SEQ_RE = re.compile(r"^[0-9]+$")

# Global ids have exactly 3 colon segments (``g:<epoch>:<seq>``); token ids
# have >= 4 (``t:<sid>:<epoch>:<seq>``) with the sid taken as everything
# between the label and the trailing epoch/seq pair (rsplit semantics —
# a sid containing colons stays round-trippable).
_GLOBAL_SEGMENTS = 3

# B3b-4: the capability keys a v4 SSE stream advertises in its leading
# ``slimapi.meta`` frame. ``sseReplay`` = this endpoint honours
# ``Last-Event-ID`` reconnect replay (§7.2). Kept as a constant dict so the
# versions-endpoint advertising lane (B3b-5) and the meta lane can never
# drift apart.
META_CAPABILITY_KEYS: dict[str, bool] = {"sseReplay": True}

# Periodic maintenance cadence for the replay log (TTL GC + barrier GC).
# Well below the 15-min default TTL so idle domains converge without a
# request arriving.
DEFAULT_SWEEP_INTERVAL_S = 60.0


def sse_id_line(domain: str, epoch: str, seq: int) -> bytes:
    """The ``id:`` SSE field line (INCLUDING the trailing ``\\n``) for one
    published frame.

    ``domain`` is the replay-log domain key — ``"g"`` for the global
    ``/events`` sequence, ``"t:<sid>"`` for a per-session token stream —
    which is byte-for-byte the leading segments of the wire id
    (§7.1: ``g:<epoch>:<seq>`` / ``t:<sid>:<epoch>:<seq>``).
    """
    return f"id: {domain}:{epoch}:{seq}\n".encode("ascii")


def frame_with_id(frame: bytes, domain: str, epoch: str, seq: int) -> bytes:
    """Prefix one already-serialized SSE frame block with its ``id:`` line.

    The caller hands the result to ASGI send verbatim; the frame itself is
    NOT re-serialized (byte-identity of the underlying frame is preserved —
    the id line is pure additive prefix).
    """
    return sse_id_line(domain, epoch, seq) + frame


def parse_last_event_id(
    header: str | None, *, token_sid: str | None = None,
) -> tuple[str, int] | None:
    """Classification ① syntax + ② endpoint/sid domain match.

    ``token_sid=None`` → the GLOBAL endpoint (``/events``): the only
    accepted grammar is ``g:<epoch>:<seq>``. ``token_sid="<sid>"`` → the
    token endpoint (``/slimapi/sessions/{sid}/stream``): the only accepted
    grammar is ``t:<sid>:<epoch>:<seq>`` with the id's sid equal to the
    path sid.

    Returns ``(epoch, seq)`` when both steps pass, ``None`` on ANY
    violation (malformed syntax, wrong label for the endpoint, sid
    mismatch, wrong segment count, non-hex epoch, non-decimal seq) — the
    caller treats ``None`` as ignore+reset (first-connect semantics; a
    client protocol violation is answered with silence, never a resync).
    """
    if not header:
        return None
    parts = header.split(":")
    if token_sid is None:
        # ① global grammar: exactly g:<epoch>:<seq> — extra segments are
        # syntax violations, not token-id fallbacks (② is judged on the
        # FIRST segment: a ``t:…`` id on /events is a cross-endpoint
        # violation regardless of what follows).
        if len(parts) != _GLOBAL_SEGMENTS or parts[0] != GLOBAL_DOMAIN:
            return None
        epoch, seq_text = parts[1], parts[2]
    else:
        # ① token grammar: t:<sid>:<epoch>:<seq> with >= 4 segments; the
        # sid is everything between the label and the trailing pair.
        if len(parts) < _GLOBAL_SEGMENTS + 1 or parts[0] != "t":
            return None
        sid = ":".join(parts[1:-2])
        # ② cross-sid: right label, right endpoint, wrong session.
        if sid != token_sid:
            return None
        epoch, seq_text = parts[-2], parts[-1]
    if _EPOCH_RE.match(epoch) is None or _SEQ_RE.match(seq_text) is None:
        return None
    return epoch, int(seq_text)


def classify_reconnect(
    header: str | None,
    replay: "ReplayLog",
    *,
    domain: str,
    token_sid: str | None = None,
) -> ReplayOutcome | None:
    """Full ①②③④ Last-Event-ID classification for one reconnecting scope.

    ``domain`` is the replay-log domain this connection reads (the global
    domain for ``/events``, ``token_domain(sid)`` for a token stream);
    ``token_sid`` selects the ② grammar (see :func:`parse_last_event_id`).

    Returns:

    * ``None`` — no header at all (first connect) OR ①② violation
      (ignore+reset): the connection proceeds with first-connect
      semantics; the caller emits nothing extra;
    * :class:`ReplayResync` — emit a leading ``resync{reason}`` frame
      (③ ``epoch_changed`` / ④ ``reconnect_no_replay`` /
      ``replay_expired`` / ``replay_gap``);
    * :class:`ReplayFrames` — emit each entry's frame WITH its ``id:``
      line, in strictly increasing seq order, before any live frame
      (an empty tuple = cursor already up to date — nothing to emit);
    * :class:`ReplayIgnoreReset` — ④ future-cursor violation (same wire
      behaviour as ``None``).

    The ③④ delegation goes through :meth:`ReplayLog.replay` so the
    short-circuit order and the §9.1 outcome counters stay in exactly one
    place.
    """
    if not header:
        return None
    parsed = parse_last_event_id(header, token_sid=token_sid)
    if parsed is None:
        # ①②: ignore + reset (never a resync — the client violated the
        # protocol; the server has not lost state).
        return None
    epoch, seq = parsed
    # ③④ live in the log layer (frozen order + outcome counters).
    return replay.replay(domain, after_seq=seq, epoch=epoch)


def meta_v4_extension(epoch: str, seq_base: int) -> dict[str, Any]:
    """B3b-4 additive v4 ``slimapi.meta`` fields (§7.0 终裁②).

    Merged into the v3 meta payload AFTER the frozen ``subscriberId`` /
    ``tokens`` keys (field order: ``subscriberId, tokens, capabilities,
    epoch, seqBase``). ``seq_base`` = the domain's current max published
    seq at connect time (0 for a fresh domain) — the first post-meta
    ``id:`` frame on a first connect is exactly ``seqBase + 1``.
    The meta frame itself carries NO ``id:`` (终裁②).
    """
    return {
        "capabilities": dict(META_CAPABILITY_KEYS),
        "epoch": epoch,
        "seqBase": seq_base,
    }


async def replay_sweep_loop(
    replay: "ReplayLog",
    *,
    interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodic replay-log maintenance (design §3.4/§3.6), wired once per
    process in ``app.py`` (same lifecycle pattern as the access-log
    maintenance loop / QpSweepShadow).

    Each tick: ``sweep()`` — TTL-evict expired frames in every domain +
    GC barriers whose replay window has fully passed the watermark.
    (Per-sid domain shells and their seq counters are never deleted within
    the process epoch — REPLAY-018; the real GC is process restart.)

    Best-effort: a sweep failure warns and the loop continues; a closed
    log (``RuntimeError``) exits quietly — shutdown raced a tick.
    """
    from ..logging_config import get_logger

    logger = get_logger("sse.replay")
    while True:
        if stop_event is None:
            await asyncio.sleep(interval_s)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
            else:
                return
        try:
            if replay.closed:
                # Log closed (shutdown raced the tick) — exit quietly.
                return
            replay.sweep()
        except RuntimeError:
            # Log closed (shutdown raced the tick) — exit quietly.
            return
        except Exception:  # noqa: BLE001 — best-effort maintenance
            logger.warning("replay sweep tick failed", exc_info=True)
