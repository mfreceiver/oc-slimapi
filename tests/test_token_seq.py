"""4.12.0 修订六 Lane B — token-stream seq 域 + token_memory_limit 恢复语义.

B-1 (reserve→encode→append→fanout) + B-2 (replayable resync /
fail-closed) against the frozen three-round-reviewed design. Hub-level
tests pin the semantics task list a–h:

a. payload ``seq`` strictly monotonic, equal to the SSE ``id:`` last
   segment (continuous delta stream);
b. replay preserves the payload seq; the ``message.removed`` tombstone
   consumes a seq;
c. B-1 atomicity — injected append failure drops the frame, rolls the
   seq back (next success contiguous — no hole), bumps the counter, and
   leaks NO id-less frame to a v4 subscriber;
d. ``token_memory_limit`` eviction → replayable resync (id line +
   payload seq), appended to the log, replayable on reconnect, stream
   does NOT terminate, and the evicted part's late frames die at the
   disabled gate;
e. 🔴 fail-closed — resync publish failure after the eviction cleared
   state terminates EVERY subscriber of the sid (+ best-effort barrier);
f. route-private four-value resyncs stay id-less / un-logged / seq-free
   (the two reason sets are disjoint by construction);
g. v3 subscribers consume seq-bearing frames (payload field present, no
   id line) and keep receiving the token_memory_limit frame;
h. same-epoch reconnect: the domain seq ledger continues (no reset).

R2 gate (round-4) additions:

* sticky invalidation flag — fail-closed aftermath forces NO-cursor
  reconnects into ``resync{reconnect_no_replay}`` (persistent marker;
  terminate now carries the reason too);
* reservation primitive matrix (Blocking 2, rev-specified six cases);
* route-level fresh-domain first-seq + zero-subscriber variants live in
  tests/test_sse_replay_wire.py (test_r3b / test_r3c).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from oc_slimapi.sse.replay_log import (
    FRAME_KIND_BUSINESS,
    FRAME_KIND_TOMBSTONE,
    RESYNC_RECONNECT_NO_REPLAY,
    RESYNC_REPLAY_GAP,
    ReplayFrames,
    ReplayLog,
    ReplayResync,
    token_domain,
)
from oc_slimapi.sse.replay_wire import V4_RESYNC_REASONS, classify_reconnect
from oc_slimapi.sse.token_hub import TokenStreamHub
from oc_slimapi.sse.tokenstream.fanout import REPLAYABLE_RESYNC_REASONS
from oc_slimapi.sse.tokenstream.frames import STOP, _resync_frame
from oc_slimapi.sse.tokenstream.subscriber import TokenSubscriber

EPOCH = "0123456789abcdef"
SID = "s1"


# ---------------------------------------------------------------------------
# Helpers (per-file inline pattern — mirrors tests/test_sse_replay_wire.py)
# ---------------------------------------------------------------------------

def _text_start(sid: str, mid: str, pid: str, text: str = "") -> dict:
    return {
        "part": {
            "sessionID": sid,
            "messageID": mid,
            "id": pid,
            "type": "text",
            "time": {"end": None},
            "text": text,
        }
    }


def _delta(sid: str, mid: str, pid: str, text: str) -> dict:
    return {
        "sessionID": sid,
        "messageID": mid,
        "partID": pid,
        "field": "text",
        "delta": text,
    }


def _text_end(sid: str, mid: str, pid: str, text: str) -> dict:
    """text-end PartUpdated（插件可改写全文——`text` 为终态权威文本，
    仅落库走 REST，从不上 v4 wire）。"""
    return {
        "part": {
            "sessionID": sid,
            "messageID": mid,
            "id": pid,
            "type": "text",
            "time": {"end": 1700000000000},
            "text": text,
        }
    }


def _parse(block: bytes) -> tuple[str | None, dict]:
    """(event, data) for one SSE frame block (no ``id:`` handling)."""
    event: str | None = None
    data: dict = {}
    for line in block.split(b"\n"):
        if line.startswith(b"event: "):
            event = line[len(b"event: "):].decode()
        elif line.startswith(b"data: "):
            data = json.loads(line[len(b"data: "):].decode())
    return event, data


def _split_id(raw: bytes) -> tuple[str, bytes]:
    """Split one v4 delivery (``id:`` line + frame) → (id, block)."""
    id_line, _, block = raw.partition(b"\n")
    assert id_line.startswith(b"id: "), raw
    return id_line[len(b"id: "):].decode(), block


def _drain(sub: TokenSubscriber) -> list:
    """Every queued item in order (STOP sentinel included verbatim)."""
    out: list = []
    while True:
        try:
            out.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _seq_of(id_: str) -> int:
    return int(id_.rsplit(":", 1)[1])


def _v4_sub(th: TokenStreamHub, sid: str = SID) -> TokenSubscriber:
    sub = TokenSubscriber(session_id=sid, metrics=th._metrics)
    th.attach_subscriber(sid, sub, wire_v4=True)
    return sub


def _v3_sub(th: TokenStreamHub, sid: str = SID) -> TokenSubscriber:
    sub = TokenSubscriber(session_id=sid, metrics=th._metrics)
    th.attach_subscriber(sid, sub)
    return sub


def _delta_frames(items: list) -> list[tuple[str, dict]]:
    out = []
    for item in items:
        if not isinstance(item, bytes):
            continue
        block = _split_id(item)[1] if item.startswith(b"id: ") else item
        event, data = _parse(block)
        if event == "message.part.delta":
            out.append((event, data))
    return out


# ---------------------------------------------------------------------------
# a. seq monotonic + id last segment
# ---------------------------------------------------------------------------

async def test_seq_strictly_monotonic_and_matches_id_last_segment():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        for text in ("aa", "bb", "cc"):
            th.on_part_delta(_delta(SID, "m1", "p1", text))
            th.flush()
        items = _drain(sub)
        # every v4 delivery carries an id line (no id-less leak, ever)
        assert all(isinstance(i, bytes) and i.startswith(b"id: ") for i in items)
        seqs: list[int] = []
        for raw in items:
            id_, block = _split_id(raw)
            assert id_.startswith(f"t:{SID}:{EPOCH}:")
            event, data = _parse(block)
            assert event == "message.part.delta"
            # payload seq == SSE id last segment (B-1 core invariant)
            assert data["seq"] == _seq_of(id_)
            seqs.append(data["seq"])
        assert seqs == [1, 2, 3]  # strictly monotonic, dense from 1
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# b. replay preserves payload seq; tombstone consumes seq
# ---------------------------------------------------------------------------

async def test_replay_preserves_payload_seq_and_tombstone_consumes_seq():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "x"))
        th.flush()  # seq 1 (delta)
        th.on_message_removed(SID, "m1")  # seq 2 (tombstone)
        assert log.last_seq(token_domain(SID)) == 2
        outcome = log.replay(token_domain(SID), after_seq=0, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        assert [e.seq for e in outcome.entries] == [1, 2]
        assert [e.kind for e in outcome.entries] == [
            FRAME_KIND_BUSINESS, FRAME_KIND_TOMBSTONE,
        ]
        # replayed payloads keep the seq stamped at encode time
        for entry in outcome.entries:
            event, data = _parse(entry.payload)
            assert data["seq"] == entry.seq
        # the live v4 wire saw the same bytes (id last segment == payload)
        ids = [_seq_of(_split_id(i)[0]) for i in _drain(sub)]
        assert ids == [1, 2]
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# c. B-1 atomicity — append failure drops + rolls back + counts
# ---------------------------------------------------------------------------

async def test_b1_append_failure_drops_frame_keeps_seq_contiguous(monkeypatch):
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "aa"))
        th.flush()  # seq 1
        assert len(_drain(sub)) == 1

        # inject ONE append failure (the historical degradation would have
        # fanned the raw frame with no id — abolished by B-1).
        orig_append = log.append
        fail_next = {"armed": True}

        def flaky_append(domain, payload, *, kind=FRAME_KIND_BUSINESS, seq=None):
            if fail_next["armed"]:
                fail_next["armed"] = False
                raise RuntimeError("injected append failure")
            return orig_append(domain, payload, kind=kind, seq=seq)

        monkeypatch.setattr(log, "append", flaky_append)
        th.on_part_delta(_delta(SID, "m1", "p1", "bb"))
        th.flush()  # publish fails → frame dropped
        monkeypatch.undo()

        # nothing reached the v4 subscriber for the failed window
        assert _drain(sub) == []
        assert th._metrics.seq_publish_failures_total == 1
        # the reservation was rolled back — no hole burned
        assert log.last_seq(token_domain(SID)) == 1

        # the next successful frame takes the rolled-back seq (contiguous)
        th.on_part_delta(_delta(SID, "m1", "p1", "cc"))
        th.flush()
        items = _drain(sub)
        assert len(items) == 1
        id_, block = _split_id(items[0])
        _, data = _parse(block)
        assert _seq_of(id_) == data["seq"] == 2
        assert th._metrics.seq_publish_failures_total == 1  # no new failure
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# d. token_memory_limit → replayable resync (the B-2 body)
# ---------------------------------------------------------------------------

async def test_token_memory_limit_replayable_resync_semantics(monkeypatch):
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 1,
    )
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        # part A resident (1 byte == the cap)
        th.on_part_updated(_text_start(SID, "mA", "p1"))
        th.on_part_delta(_delta(SID, "mA", "p1", "a"))
        th.flush()  # seq 1 (delta a)
        # part B's admission evicts A → replayable resync (seq 2);
        # B's own delta publishes after it (seq 3) — stream NOT terminated.
        th.on_part_updated(_text_start(SID, "mB", "p1"))
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))
        th.flush()  # seq 3 (delta b)

        items = _drain(sub)
        parsed = []
        for raw in items:
            id_, block = _split_id(raw)
            event, data = _parse(block)
            parsed.append((event, _seq_of(id_), data))
        assert [(e, s) for e, s, _ in parsed] == [
            ("message.part.delta", 1),
            ("resync", 2),
            ("message.part.delta", 3),
        ]
        # the resync carries reason + sessionID + payload seq == id segment
        assert parsed[1][2] == {
            "reason": "token_memory_limit", "sessionID": SID, "seq": 2,
        }
        assert th.token_memory_limit_total == 1
        # appended to the log; NO barrier (the frame is the R4 guarantee)
        assert log.last_seq(token_domain(SID)) == 3
        assert log.barrier_watermark(token_domain(SID)) is None

        # replayable: a reconnect from cursor 1 receives the resync itself
        outcome = log.replay(token_domain(SID), after_seq=1, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        assert [e.seq for e in outcome.entries] == [2, 3]
        _, resync_data = _parse(outcome.entries[0].payload)
        assert resync_data["reason"] == "token_memory_limit"

        # the evicted part never resumes: late deltas die at the gate and
        # consume no seq (disabled-gate semantics, B-2 修正 2)
        th.on_part_delta(_delta(SID, "mA", "p1", "late"))
        th.on_part_updated(_text_start(SID, "mA", "p1", text="resurrect"))
        th.flush()
        assert log.last_seq(token_domain(SID)) == 3
        assert ("s1", "mA", "p1") not in th.live_parts
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# e. 🔴 fail-closed — resync publish failure terminates the sid's subs
# ---------------------------------------------------------------------------

async def test_failclosed_resync_failure_terminates_sid_subscribers(monkeypatch):
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 1,
    )
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub1 = _v4_sub(th)
    sub2 = TokenSubscriber(session_id=SID, metrics=th._metrics)
    th.attach_subscriber(SID, sub2, wire_v4=True)
    try:
        th.on_part_updated(_text_start(SID, "mA", "p1"))
        th.on_part_delta(_delta(SID, "mA", "p1", "a"))
        th.flush()  # seq 1
        _drain(sub1)
        _drain(sub2)

        # fail ONLY the eviction resync's append (state is already cleared
        # when it fails — the fail-closed precondition)
        orig_append = log.append

        def resync_poison(domain, payload, *, kind=FRAME_KIND_BUSINESS, seq=None):
            if b'"token_memory_limit"' in payload:
                raise RuntimeError("injected resync append failure")
            return orig_append(domain, payload, kind=kind, seq=seq)

        monkeypatch.setattr(log, "append", resync_poison)
        th.on_part_updated(_text_start(SID, "mB", "p1"))
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))  # evicts A → fails
        monkeypatch.undo()

        # BOTH subscribers of the sid were force-terminated
        assert sub1.closed and sub2.closed
        # round-4 Blocking 1: the termination now CARRIES the reason —
        # reconnect_no_replay is inside the frozen v4 domain, so BOTH
        # wires get resync{reconnect_no_replay} + STOP (not a bare STOP).
        forced = _resync_frame(SID, RESYNC_RECONNECT_NO_REPLAY)
        assert _drain(sub1) == [forced, STOP]
        assert _drain(sub2) == [forced, STOP]
        # counted — generic publish failure + the fail-closed counter
        assert th._metrics.seq_publish_failures_total == 1
        assert th._metrics.seq_resync_failclosed_total == 1
        # best-effort barrier landed: reconnects WITH a cursor resolve to
        # reconnect_no_replay → HTTP alignment (no stale-window replay)
        assert log.barrier_watermark(token_domain(SID)) == 1
        # round-4 Blocking 1 (persistent marker): the sticky invalidation
        # flag is set — a NO-cursor reconnect is ALSO forced into HTTP
        # alignment (a barrier cannot intercept a no-Last-Event-ID
        # client). The read is non-destructive (stays flagged).
        assert log.first_connect_invalidated(token_domain(SID)) is True
        assert log.first_connect_invalidated(token_domain(SID)) is True
        # the eviction itself still happened (state cleared, counted)
        assert th.token_memory_limit_total == 1
        assert ("s1", "mA", "p1") not in th.live_parts
        # the stream keeps publishing for other parts: B's delta reuses
        # the rolled-back seq slot (no hole)
        th.flush()
        assert log.last_seq(token_domain(SID)) == 2
        # flag lifecycle (round-3): STICKY — a successful publish does
        # NOT clear it ("sequence resumed" ≠ "invalidation delivered";
        # the evicted part is REST-owned and client recovery is
        # unobservable). Persists for the rest of the epoch.
        assert log.first_connect_invalidated(token_domain(SID)) is True
    finally:
        th.detach_subscriber(SID, sub1)
        th.detach_subscriber(SID, sub2)
        th.stop()


# ---------------------------------------------------------------------------
# f. route-private four-value resyncs stay control frames
# ---------------------------------------------------------------------------

async def test_route_private_resyncs_stay_idless_unlogged_seqfree():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th._fanout_resync(SID, RESYNC_REPLAY_GAP)
        items = _drain(sub)
        assert len(items) == 1
        assert isinstance(items[0], bytes)
        assert not items[0].startswith(b"id: ")  # no id line
        event, data = _parse(items[0])
        assert event == "resync"
        assert "seq" not in data  # no payload seq
        # never logged, never consumed a seq
        assert log.domain_frame_count(token_domain(SID)) == 0
        assert log.last_seq(token_domain(SID)) == 0

        # the separation is structural: the two reason sets are disjoint
        # and the replayable path REFUSES route-private reasons.
        assert REPLAYABLE_RESYNC_REASONS & V4_RESYNC_REASONS == frozenset()
        with pytest.raises(ValueError):
            th._fanout_replayable_resync(SID, RESYNC_REPLAY_GAP)
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# g. v3 subscribers — payload seq visible, behavior unchanged
# ---------------------------------------------------------------------------

async def test_v3_subscriber_consumes_seq_bearing_frames(monkeypatch):
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v3_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "aa"))
        th.flush()  # seq 1
        items = _drain(sub)
        # v3 wire: no id line, but the additive payload seq field is there
        delta_items = [i for i in items if isinstance(i, bytes)
                       and i.startswith(b"event: message.part.delta")]
        assert len(delta_items) == 1
        event, data = _parse(delta_items[0])
        assert data["seq"] == 1
        assert data["text"] == "aa"
        assert "partEventRevision" in data

        # v3 keeps receiving the token_memory_limit frame (raw, seq field)
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 1,
        )
        th.on_part_updated(_text_start(SID, "mB", "p1"))  # evicts m1/p1
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))
        monkeypatch.undo()
        th.flush()
        after = _drain(sub)
        resync_items = [i for i in after if isinstance(i, bytes)
                        and i.startswith(b"event: resync")]
        assert len(resync_items) == 1
        assert not resync_items[0].startswith(b"id: ")
        event, data = _parse(resync_items[0])
        assert event == "resync"
        assert data == {
            "reason": "token_memory_limit", "sessionID": SID, "seq": 2,
        }
        # the v3 stream continues after the eviction (B delta seq 3)
        deltas = _delta_frames(after)
        assert [d["seq"] for _, d in deltas] == [3]
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ---------------------------------------------------------------------------
# h. same-epoch reconnect — the seq ledger continues
# ---------------------------------------------------------------------------

async def test_same_epoch_reconnect_seq_ledger_continues():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        for text in ("aa", "bb"):
            th.on_part_delta(_delta(SID, "m1", "p1", text))
            th.flush()  # seq 1, 2
        assert log.last_seq(token_domain(SID)) == 2
        # reconnect: same epoch, detach + re-attach (client ledger intact)
        th.detach_subscriber(SID, sub)
        sub2 = _v4_sub(th)
        th.on_part_delta(_delta(SID, "m1", "p1", "cc"))
        th.flush()  # seq 3 — the domain ledger did NOT reset
        ids = [_seq_of(_split_id(i)[0]) for i in _drain(sub2)]
        assert ids == [3]
        assert log.epoch == EPOCH
        outcome = log.replay(token_domain(SID), after_seq=2, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        assert [e.seq for e in outcome.entries] == [3]
        th.detach_subscriber(SID, sub2)
    finally:
        th.stop()


# ---------------------------------------------------------------------------
# R2 gate (round-4 Blocking 1) — sticky invalidation flag on the wire path
# ---------------------------------------------------------------------------

def test_classify_reconnect_nocursor_sticky_flag():
    """No-``Last-Event-ID`` connect on a flagged domain → forced
    ``resync{reconnect_no_replay}`` (HTTP alignment), never a plain
    meta-only first-connect; fresh domains keep baseline first-connect
    semantics; the frozen ①② ignore+reset path never consults the flag.

    Round-3: the flag is STICKY — an unrelated successful append in the
    same domain must NOT re-open plain first-connect semantics (rev-2
    counter-example: sequence-resumed ≠ invalidation-delivered)."""
    log = ReplayLog(epoch=EPOCH)
    dom = token_domain(SID)
    # fresh domain — plain first-connect
    assert classify_reconnect(None, log, domain=dom, token_sid=SID) is None
    assert classify_reconnect("", log, domain=dom, token_sid=SID) is None
    # flagged → forced alignment; NON-destructive across reads
    log.mark_invalidated(dom)
    for _ in range(2):
        outcome = classify_reconnect(None, log, domain=dom, token_sid=SID)
        assert isinstance(outcome, ReplayResync)
        assert outcome.reason == RESYNC_RECONNECT_NO_REPLAY
    # the forced alignments are visible in the §9.1 outcome counters
    assert log.replay_outcomes_total["reconnect_no_replay"] == 2
    # round-3: a later successful publish does NOT clear the flag —
    # the no-cursor connect is STILL forced into HTTP alignment
    log.append(dom, b"x")
    outcome = classify_reconnect(None, log, domain=dom, token_sid=SID)
    assert isinstance(outcome, ReplayResync)
    assert outcome.reason == RESYNC_RECONNECT_NO_REPLAY
    # a ①②-violating header stays ignore+reset even on a flagged domain
    # (frozen §7.2: a client protocol violation is answered with silence)
    assert classify_reconnect("garbage", log, domain=dom, token_sid=SID) is None


def test_invalidation_flag_survives_recycle_and_reads():
    """The flag is sticky domain metadata: successful appends do NOT
    clear it (round-3), recycle_domain (idle sweep) keeps it (same
    fail-safe reasoning as barrier retention), and unknown domains are
    never flagged."""
    log = ReplayLog(epoch=EPOCH)
    dom = token_domain(SID)
    assert log.first_connect_invalidated(dom) is False  # unknown → clean
    log.mark_invalidated(dom)
    assert log.recycle_domain(dom) is True
    assert log.first_connect_invalidated(dom) is True  # survives recycle
    log.append(dom, b"resumed publishing")  # does NOT clear (sticky)…
    assert log.first_connect_invalidated(dom) is True


# ---------------------------------------------------------------------------
# R2 gate (round-4 Blocking 2) — ReplayLog reservation primitive matrix
# ---------------------------------------------------------------------------

def test_reservation_primitive_matrix():
    """The six-case reservation contract matrix (rev-specified,
    primitive-level): confirm-append, duplicate rejection, rollback+reuse,
    published-is-final, cross-domain rejection, and the multi-outstanding
    misuse contract (single-outstanding itself is a sync-scope
    convention, untestable — case 6 locks the loud-failure behaviour)."""
    log = ReplayLog(epoch=EPOCH)
    dom = token_domain(SID)

    # 1. reserve → append(seq=) succeeds and publishes exactly that seq
    seq = log.reserve_seq(dom)
    entry = log.append(dom, b"frame-a", seq=seq)
    assert entry.seq == seq == 1
    assert log.last_seq(dom) == 1

    # 2. appending the SAME seq again → rejected (nothing lands)
    with pytest.raises(ValueError):
        log.append(dom, b"frame-a-dup", seq=seq)
    assert log.domain_frame_count(dom) == 1

    # 3. reserve → rollback → a STALE append of the rolled-back seq is
    #    rejected, and the NEXT reserve hands the rolled-back value back
    #    (no hole burned — the reuse contract behind test c's contiguity)
    seq2 = log.reserve_seq(dom)
    assert seq2 == 2
    assert log.rollback_seq(dom, seq2) is True
    with pytest.raises(ValueError):
        log.append(dom, b"frame-b-stale", seq=seq2)
    assert log.reserve_seq(dom) == seq2
    log.append(dom, b"frame-b", seq=seq2)
    assert log.last_seq(dom) == 2

    # 4. rollback AFTER the seq was published → False (published is final)
    assert log.rollback_seq(dom, seq2) is False
    assert log.last_seq(dom) == 2

    # 5. append(seq=) into a domain with NO outstanding reservation →
    #    rejected (the value was never handed out for that domain)
    other = token_domain("s-other")
    with pytest.raises(ValueError):
        log.append(other, b"frame-x", seq=1)

    # 6. two outstanding reservations. NOTE the contract boundary this
    #    case locks: single-outstanding is a SYNCHRONOUS-SCOPE
    #    CONVENTION (reserve + confirm in one event-loop step, no await
    #    between) — it cannot be enforced by a test; what IS locked
    #    here is the misuse behaviour: reserves burn FORWARD (3, 4);
    #    appending the OLDER one is rejected while the LATEST still
    #    lands — i.e. misuse FAILS LOUDLY instead of silently
    #    double-publishing a seq position.
    r1 = log.reserve_seq(dom)
    r2 = log.reserve_seq(dom)
    assert (r1, r2) == (3, 4)
    with pytest.raises(ValueError):
        log.append(dom, b"frame-c-stale", seq=r1)
    log.append(dom, b"frame-c", seq=r2)
    assert log.last_seq(dom) == 4


# ---------------------------------------------------------------------------
# 修订六终门控返工（rev-2 Blocking 2 闭合）— 恢复协议 v2 驱逐矩阵
#
# 协议 v2（advisory resync + revision 收敛，§7.7）：驱逐不 rebase 流基线
# ——seq 域连续、其余 part 增量不受扰、被逐 part 无任何后续流帧。矩阵②
# （text-end 前驱逐 + 收敛通道 revision bump 锚点）在
# tests/test_digest_revision.py（需 GlobalHub 组合）。实现序锚：
# drop_part（被逐 key 的 pending 尾部**丢弃不发布**，budgets.py:260→:404）
# → flush_sid（该 sid 其余 part 的 pending，占 seq）→ replayable resync。
# ---------------------------------------------------------------------------


async def test_eviction_matrix_midstream_pending_tail_discarded(monkeypatch):
    """矩阵①中途流驱逐：被逐 part 未 flush 的 pending 尾部 delta 随
    drop_part **丢弃、不发布**（契约按实现记——仅同 sid 其余 part 的
    pending 先于 resync flush）；resync 占 seq 1、B 增量占 seq 2，流 seq
    连续；被逐 part 迟到 delta 死于 disabled gate（零帧、零 seq）。"""
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 1,
    )
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "mA", "p1"))
        th.on_part_delta(_delta(SID, "mA", "p1", "a"))  # pending，未 flush
        # B 准入驱逐 A：drop_part 先弹 A 的 pending 累加器（尾部 "a"
        # 丢弃）→ 无 A delta 帧；resync 占 seq 1；B delta 占 seq 2。
        th.on_part_updated(_text_start(SID, "mB", "p1"))
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))
        th.flush()
        parsed = []
        for raw in _drain(sub):
            id_, block = _split_id(raw)
            event, data = _parse(block)
            parsed.append((event, _seq_of(id_), data))
        assert [(e, s) for e, s, _ in parsed] == [
            ("resync", 1),
            ("message.part.delta", 2),
        ]
        assert parsed[0][2] == {
            "reason": "token_memory_limit", "sessionID": SID, "seq": 1,
        }
        assert parsed[1][2]["text"] == "b"  # B（其余 part）增量不受扰
        assert th.token_memory_limit_total == 1
        assert ("s1", "mA", "p1") not in th.live_parts
        assert ("s1", "mA", "p1") in th._disabled_parts
        # A 迟到 delta 死于 gate：零帧、零 seq 消耗
        th.on_part_delta(_delta(SID, "mA", "p1", "late"))
        th.flush()
        assert log.last_seq(token_domain(SID)) == 2
        assert _delta_frames(_drain(sub)) == []
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_eviction_matrix_after_textend_plugin_rewrite(monkeypatch):
    """矩阵③text-end 插件改写后驱逐：终态全文（插件改写后）**从不上
    v4 wire**——done marker 为 v3-only 无 seq（lever 1：权威全文走 REST），
    wire 只见积累 delta；其后另一 part 被驱逐 → resync，seq 域连续、
    已完成 part 不受扰、被逐 part 无后续流帧。
    （旋钮注：用 count cap 驱逐——byte cap=1 会让首个 delta 在 ingest
    即自逐该 part，测不到「完成后另一 part 被逐」场景。）"""
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVE_PARTS_MAX", 1,
    )
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = _v4_sub(th)
    try:
        th.on_part_updated(_text_start(SID, "mA", "p1"))
        th.on_part_delta(_delta(SID, "mA", "p1", "pre"))    # 流上积累文本
        # text-end（插件改写终态 "REWRITTEN"）：finish_part 同步 drain
        # residual "pre" → delta seq 1；done marker v3-only（无 seq、不入
        # 日志、不上 v4 wire）；A retire。
        th.on_part_updated(_text_end(SID, "mA", "p1", "REWRITTEN"))
        th.on_part_updated(_text_start(SID, "mB", "p1"))
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))
        th.flush()  # B delta → seq 2
        # C 准入驱逐 B（A 已 retire，不在 live 候选）→ resync seq 3
        th.on_part_updated(_text_start(SID, "mC", "p1"))
        th.on_part_delta(_delta(SID, "mC", "p1", "c"))
        th.flush()  # C delta → seq 4
        parsed = []
        for raw in _drain(sub):
            id_, block = _split_id(raw)
            event, data = _parse(block)
            parsed.append((event, _seq_of(id_), data))
        assert [(e, s) for e, s, _ in parsed] == [
            ("message.part.delta", 1),
            ("message.part.delta", 2),
            ("resync", 3),
            ("message.part.delta", 4),
        ]
        # 积累文本上 wire；改写终态 "REWRITTEN" 不在任何帧（REST 独有）
        texts = [d.get("text") for _, _, d in parsed
                 if "text" in d]
        assert texts == ["pre", "b", "c"]
        assert all("REWRITTEN" != t for t in texts)
        assert th.token_memory_limit_total == 1
        # B 迟到 delta 死于 gate；A 早已 disabled——迟到事件零帧零 seq
        th.on_part_delta(_delta(SID, "mB", "p1", "late-b"))
        th.on_part_updated(_text_end(SID, "mA", "p1", "again"))
        th.flush()
        assert log.last_seq(token_domain(SID)) == 4
        assert _delta_frames(_drain(sub)) == []
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_eviction_matrix_before_client_connect(monkeypatch):
    """矩阵④客户端连接前驱逐：零订阅者时驱逐照常发生——resync 写入
    重放日志占 seq（B-1 发布与订阅者存在性无关）；后来客户端连接（游标
    0 重放）在窗口内见到该 advisory resync；连接后同域 seq 继续。
    （旋钮注：count cap——byte cap 会让后续 delta 自逐 B，attach 后无帧。）"""
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVE_PARTS_MAX", 1,
    )
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)  # 无订阅者
    try:
        th.on_part_updated(_text_start(SID, "mA", "p1"))
        th.on_part_delta(_delta(SID, "mA", "p1", "a"))
        th.flush()  # seq 1 —— 零订阅者：投递丢弃但入日志
        th.on_part_updated(_text_start(SID, "mB", "p1"))  # 驱逐 A → resync
        th.on_part_delta(_delta(SID, "mB", "p1", "b"))
        th.flush()  # seq 3
        assert th.token_memory_limit_total == 1
        assert log.last_seq(token_domain(SID)) == 3
        # 客户端现在才连：路由层握手重放（Last-Event-ID 游标 0）→
        # 窗口内三帧，含驱逐信号本身（advisory resync 可重放）。
        outcome = log.replay(token_domain(SID), after_seq=0, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        assert [e.seq for e in outcome.entries] == [1, 2, 3]
        assert [e.kind for e in outcome.entries] == [
            FRAME_KIND_BUSINESS, FRAME_KIND_BUSINESS, FRAME_KIND_BUSINESS,
        ]
        _, resync_data = _parse(outcome.entries[1].payload)
        assert resync_data == {
            "reason": "token_memory_limit", "sessionID": SID, "seq": 2,
        }
        # 连接后照常消费：B（仍 live）新增量以同域 seq 4 到达
        sub = _v4_sub(th)
        th.on_part_delta(_delta(SID, "mB", "p1", "b2"))
        th.flush()
        items = _drain(sub)
        ids = [_seq_of(_split_id(i)[0]) for i in items]
        assert ids == [4]
        _, data = _parse(_split_id(items[0])[1])
        assert data["text"] == "b2"
        th.detach_subscriber(SID, sub)
    finally:
        th.stop()

