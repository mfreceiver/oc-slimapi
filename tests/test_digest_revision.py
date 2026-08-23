"""4.11.0 Phase A / A3 (P4): digest ``messagesRevision`` — process-wide
monotonic int.

Frozen semantics (plan §2 A3 / v4-contract revision lane — do not expand):

* ``_message_revision_seq`` is a PROCESS-LEVEL global (initial 0). Its
  lifecycle is the process: a restart zeroes it — clients MUST NOT compare
  revisions across processes/reconnects with different sidecar instances.
* Relevant events bump it — ``message.updated`` / ``message.appended``
  (the MESSAGE_EVENTS digest branch) and ``message.removed`` (AFTER the
  full existing semantic sequence: retired-gate write → cap/TTL prune →
  token-hub ``on_message_removed`` → bump LAST).
* 4.12.0（修订六）: ``message.part.updated`` / ``message.part.removed``
  ALSO bump (part-level completion-state visibility, via the unified
  ``_bump_message_revision`` helper — same debounce window semantics as
  message.updated/appended). ``message.part.delta`` still NEVER bumps.
  Session-only events never bump.
* The digest field is carried ONLY on message windows (a digest entry that
  includes message events). Session-only digests OMIT the key entirely.
* Same debounce window, multiple events → the seq bumps multiple times and
  the flushed digest carries the WINDOW-END value (per-entry stamp is
  overwritten on each relevant ingest).
* Upstream resync (``resync_all``) does NOT reset the seq — revisions stay
  comparable within one process lifetime.
* The bump is independent of subscribers: with zero subscribers the seq
  still advances (the value is observable on the next subscribed digest).

Self-contained: own helpers + fixtures (mirrors test_b1a_digest_changed.py);
does NOT touch tests/conftest.py.
"""

from __future__ import annotations

from conftest import current_replay_log

import asyncio
import json

import pytest

from oc_slimapi.sse import global_hub as global_hub_mod
from oc_slimapi.sse.hub import STOP, GlobalHub, Subscriber, sse_frame


def ev(
    directory: str | None,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
    return {
        "directory": directory,
        "payload": {"type": event_type, "properties": properties or {}},
    }


def msg_props(sid: str, mid: str) -> dict:
    """Properties for a message.updated / message.appended event."""
    return {
        "sessionID": sid,
        "info": {"id": mid, "time": {"updated": 1700000000000}},
    }


def part_updated_props(sid: str, mid: str, pid: str) -> dict:
    """Properties for a message.part.updated event (retired-gate shape)."""
    return {
        "sessionID": sid,
        "part": {
            "id": pid,
            "messageID": mid,
            "sessionID": sid,
            "type": "text",
            "time": {},
        },
        "time": {},
    }


def parse(raw: bytes) -> tuple[str | None, dict]:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw.decode().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


async def drain(sub: Subscriber, timeout: float = 0.2) -> list[bytes]:
    out: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is STOP:
            continue
        if isinstance(item, (bytes, bytearray)):
            out.append(bytes(item))
    return out


def only_digests(frames: list[bytes]) -> list[dict]:
    return [d for e, d in (parse(f) for f in frames) if e == "session.digest"]


def seq() -> int:
    """Read the process-wide revision counter."""
    return global_hub_mod._message_revision_seq


@pytest.fixture(autouse=True)
def _preserve_seq():
    """Snapshot/restore the process-wide counter around each test so the
    cross-test monotonic growth never leaks into later assertions (the
    counter itself never resets in production — this is test isolation
    only)."""
    start = seq()
    try:
        yield
    finally:
        global_hub_mod._message_revision_seq = start


async def _teardown_hub(hub: GlobalHub) -> None:
    me = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def hub():
    """Bare GlobalHub(client=None); teardown cancels all background tasks."""
    h = GlobalHub(client=None, replay_log=current_replay_log())
    try:
        yield h
    finally:
        await _teardown_hub(h)


@pytest.fixture
async def pair(hub: GlobalHub):
    sub = Subscriber()
    hub.subscribers.add(sub)
    return hub, sub


# ---------------------------------------------------------------------------
# Relevant events bump + the digest carries the revision
# ---------------------------------------------------------------------------


async def test_message_updated_bumps_and_digest_carries_revision(pair):
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    assert seq() == before + 1
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s1"
    assert digests[0]["messagesRevision"] == seq()


async def test_message_appended_bumps_and_digest_carries_revision(pair):
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.appended", msg_props("s1", "m2")))
    assert seq() == before + 1
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["messagesRevision"] == seq()


async def test_message_removed_bumps_and_digest_carries_revision(pair):
    """updated + removed in one window: the removed bump happens LAST in
    the semantic sequence, so the flushed digest carries the post-removal
    window-end value."""
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    mid = seq()
    hub.publish(ev("/p", "message.removed", {"sessionID": "s1", "messageID": "m1"}))
    assert seq() == mid + 1
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["messagesRevision"] == seq() == mid + 1


# ---------------------------------------------------------------------------
# Non-relevant events never bump; session-only digests omit the field
# ---------------------------------------------------------------------------


async def test_message_part_delta_does_not_bump(pair):
    """修订六（4.12.0）：part.delta 维持现状——per-chunk 高频事件不
    bump、不进 digest（part.updated/part.removed 的 bump 语义由
    TestPartRevision 组覆盖）。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.part.delta",
                   part_updated_props("s1", "m1", "p1")))
    assert seq() == before
    # part.delta produces no digest entry at all
    hub.flush()
    assert only_digests(await drain(sub)) == []


async def test_session_only_events_do_not_bump_and_digest_omits_field(pair):
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
    assert seq() == before
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["status"] == "busy"
    assert "messagesRevision" not in digests[0]


async def test_immediate_flush_sid_digest_omits_field_for_session_error(pair):
    """The flush_sid immediate path (session.error) is session-only: no
    messagesRevision key even though it emits a digest frame."""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    assert seq() == before
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["lastError"]["name"] == "UnknownError"
    assert "messagesRevision" not in digests[0]


async def test_mixed_window_only_message_sid_carries_field(pair):
    """s1 has a message event, s2 only a status event → exactly the s1
    digest carries messagesRevision (per-entry, message-window scoping)."""
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    hub.publish(ev("/p", "session.status", {"sessionID": "s2", "status": "idle"}))
    hub.flush()
    digests = only_digests(await drain(sub))
    by_sid = {d["sessionID"]: d for d in digests}
    assert set(by_sid) == {"s1", "s2"}
    assert by_sid["s1"]["messagesRevision"] == seq()
    assert "messagesRevision" not in by_sid["s2"]


# ---------------------------------------------------------------------------
# Window semantics: window-end value + monotonicity across windows
# ---------------------------------------------------------------------------


async def test_same_window_multiple_events_carry_window_end_value(pair):
    hub, sub = pair
    before = seq()
    for mid in ("m1", "m2", "m3"):
        hub.publish(ev("/p", "message.updated", msg_props("s1", mid)))
    assert seq() == before + 3  # every relevant event bumps
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1  # one debounce window → one merged digest
    # window-END value (the last bump), not the first
    assert digests[0]["messagesRevision"] == before + 3


async def test_cross_window_monotonic(pair):
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    hub.flush()
    first = only_digests(await drain(sub))
    assert len(first) == 1
    r1 = first[0]["messagesRevision"]

    hub.publish(ev("/p", "message.updated", msg_props("s1", "m2")))
    hub.flush()
    second = only_digests(await drain(sub))
    assert len(second) == 1
    r2 = second[0]["messagesRevision"]
    assert r2 == r1 + 1  # strictly monotonic, exactly one bump between


# ---------------------------------------------------------------------------
# message.removed: bump + existing semantics stay intact
# ---------------------------------------------------------------------------


async def test_removed_semantic_sequence_intact_after_bump(pair):
    """After a removed bump, the retired gate still swallows a late
    ``message.part.updated`` (no frame, no bump) — the bump did not
    disturb the gate/prune/token-hub sequence."""
    hub, sub = pair
    hub.publish(ev("/p", "message.removed", {"sessionID": "s1", "messageID": "m1"}))
    bumped = seq()
    await drain(sub, timeout=0.1)  # removed alone emits nothing digest-wise
    hub.publish(ev("/p", "message.part.updated",
                   part_updated_props("s1", "m1", "p1")))
    frames = await drain(sub, timeout=0.1)
    assert frames == []  # gate: late part event fully swallowed
    assert seq() == bumped  # retired-gated late part event does not bump
    # (ungated message.part.updated / part.removed DO bump — revision-6)


async def test_removed_without_pending_entry_bumps_global_only(pair):
    """A removed event with NO pending digest entry still bumps the global
    seq (observable on the next message digest); it does NOT fabricate a
    digest frame by itself."""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.removed", {"sessionID": "s9", "messageID": "mX"}))
    assert seq() == before + 1
    frames = await drain(sub, timeout=0.1)
    assert only_digests(frames) == []

    hub.publish(ev("/p", "message.updated", msg_props("s9", "mY")))
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["messagesRevision"] == seq() == before + 2


# ---------------------------------------------------------------------------
# Lifecycle: resync never resets; zero subscribers still bump
# ---------------------------------------------------------------------------


async def test_resync_does_not_reset_seq(pair):
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    r1 = seq()
    await drain(sub, timeout=0.1)  # drop any frames (incl. resync below)

    hub.resync_all()
    assert seq() == r1  # resync itself must not reset OR bump

    hub.publish(ev("/p", "message.updated", msg_props("s1", "m2")))
    hub.flush()
    digests = only_digests(await drain(sub))
    assert digests and digests[0]["messagesRevision"] == r1 + 1


async def test_zero_subscribers_seq_still_bumps(hub):
    """No subscriber attached — the counter still advances (observable on
    the next subscribed digest; also keeps resync reconciliation sound)."""
    before = seq()
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))
    hub.publish(ev("/p", "message.removed", {"sessionID": "s1", "messageID": "m1"}))
    hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
    assert seq() == before + 2  # updated + removed; status does not bump
    hub.flush()  # no crash without subscribers


async def test_q_p_immediate_push_frames_carry_no_revision(pair):
    """Raw passthrough q/p frames are byte-identical in shape — no
    messagesRevision key (they are not digests)."""
    hub, sub = pair
    hub.publish(ev("/p", "question.asked", {"id": "q1", "sessionID": "s1"}))
    frames = await drain(sub, timeout=0.1)
    assert len(frames) == 1
    _, data = parse(frames[0])
    assert data == {
        "directory": "/p",
        "type": "question.asked",
        "properties": {"id": "q1", "sessionID": "s1"},
    }


# ---------------------------------------------------------------------------
# Construction-level: to_payload field emission (hub_types unit lock)
# ---------------------------------------------------------------------------


def test_digest_fields_to_payload_conditional_revision():
    from oc_slimapi.sse.hub_types import DigestFields

    base = DigestFields(status="busy")
    payload = base.to_payload("s1")
    assert "messagesRevision" not in payload  # default None → omitted

    stamped = DigestFields(status="busy", messages_revision=7)
    payload2 = stamped.to_payload("s1")
    assert payload2["messagesRevision"] == 7


def test_sse_frame_unused_import_guard():
    """sse_frame is imported for symmetry with test_b1a; keep a trivial use
    so the import is not flagged dead while the file evolves."""
    assert sse_frame({}, event="server.heartbeat") is not None


# ---------------------------------------------------------------------------
# 修订六（4.12.0）：part 级 revision bump + flush-before-asked 因果闭合
# ---------------------------------------------------------------------------


def part_removed_props(sid: str, mid: str, pid: str) -> dict:
    """Properties for a message.part.removed event (flat schema:
    {sessionID, messageID, partID} — opencode session.ts:604-628)."""
    return {"sessionID": sid, "messageID": mid, "partID": pid}


def asked_props(sid: str) -> dict:
    """Properties for question.asked / question.v2.asked (flat
    sessionID — upstream Request schema v1/question.ts:35-42)."""
    return {"id": "q1", "sessionID": sid, "questions": []}


async def test_part_updated_bumps_and_window_merges_to_end_value(pair):
    """(a) part.updated 每 part 生命周期 2-4 次发射均 bump；同窗多事件
    debounce 合并单帧、携带窗末 revision 值，且与 message.updated 同组
    合并（同 debounce 语义）。"""
    hub, sub = pair
    before = seq()
    # 一个 part 生命周期内的典型三连发（text-start/text-end/cleanup）
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m2", "p2")))
    # 同组：message.updated 落进同一 debounce 窗
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m3")))
    assert seq() == before + 4
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1  # 同窗合并单帧
    assert digests[0]["messagesRevision"] == before + 4  # 窗末值
    assert digests[0]["messageID"] == "m3"  # 窗内最后 message_id 盖章
    assert digests[0]["sessionID"] == "s1"


async def test_part_removed_bumps_revision_revert_scenario(pair):
    """(b) part.removed（revert.ts:119 / session.ts:393 低频显式发射）同
    bump；同窗 N 个 part.removed 每个独立推进 revision、窗末值成帧。"""
    hub, sub = pair
    before = seq()
    # revert 场景：一条消息 N 个 part 同窗被逐个移除
    for pid in ("p1", "p2", "p3"):
        hub.publish(ev("/p", "message.part.removed",
                       part_removed_props("s1", "m1", pid)))
    assert seq() == before + 3  # 每事件推进 1（allocator 单点自增）
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["messagesRevision"] == before + 3
    assert digests[0]["messageID"] == "m1"


async def test_question_asked_digest_precedes_asked_frame(pair):
    """(c) flush-before-asked：question.asked 转发前 targeted flush 该 sid
    的 pending digest——同一 SSE 流先收 digest(rev=N) 再收 asked。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))  # pending
    hub.publish(ev("/p", "question.asked", asked_props("s1")))
    frames = await drain(sub)
    assert len(frames) == 2
    name0, data0 = parse(frames[0])
    assert name0 == "session.digest"
    assert data0["messagesRevision"] == before + 1
    assert data0["changed"] == ["s1"]
    name1, data1 = parse(frames[1])
    assert name1 is None  # IMMEDIATE 裸帧无 event 行
    assert data1["type"] == "question.asked"


async def test_question_v2_asked_digest_precedes_asked_frame(pair):
    """(c) v2 双名并存（hub_types IMMEDIATE 同列）——同等因果闭合。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.part.updated",
                   part_updated_props("s1", "m1", "p1")))  # pending 经 part 入口
    hub.publish(ev("/p", "question.v2.asked", asked_props("s1")))
    frames = await drain(sub)
    assert len(frames) == 2
    name0, data0 = parse(frames[0])
    assert name0 == "session.digest"
    assert data0["messagesRevision"] == before + 1
    _, data1 = parse(frames[1])
    assert data1["type"] == "question.v2.asked"


async def test_question_asked_other_sid_pending_stays_in_window(pair):
    """flush-before-asked 是 targeted：asked 的 sid 无 pending 时无操作，
    其他 sid 的 entry 留在 debounce 窗（不提前、不丢失）。"""
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s2", "mX")))  # s2 pending
    hub.publish(ev("/p", "question.asked", asked_props("s1")))       # s1 asked
    frames = await drain(sub)
    assert len(frames) == 1
    _, data = parse(frames[0])
    assert data["type"] == "question.asked"  # s1 无 pending → asked 独立转发
    hub.flush()  # s2 的 pending 到窗末正常成帧
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s2"


async def test_retired_message_late_part_events_no_digest(pair):
    """(d) whole-removal 后迟到的 part.updated/part.removed 被
    _retired_messages gate 拦截——不 bump、不造 digest。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.removed", {"sessionID": "s1", "messageID": "m1"}))
    assert seq() == before + 1  # message.removed 自身 bump（不造 entry）
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "message.part.removed", part_removed_props("s1", "m1", "p1")))
    assert seq() == before + 1  # 迟到 part 事件零推进
    hub.flush()
    assert only_digests(await drain(sub)) == []


async def test_malformed_part_events_no_bump_no_crash(pair):
    """(e) 缺 sessionID/messageID/partID 的畸形 part 事件不 bump 不崩
    （落入 token-hub 尾路由，无 token hub 即 no-op）。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.part.updated", {"sessionID": "s1"}))  # 缺 part dict
    hub.publish(ev("/p", "message.part.updated",
                   {"part": {"id": "p1", "sessionID": "s1"}}))          # 缺 messageID
    hub.publish(ev("/p", "message.part.removed",
                   {"sessionID": "s1", "messageID": "m1"}))             # 缺 partID
    assert seq() == before
    hub.flush()
    assert only_digests(await drain(sub)) == []


# ---------------------------------------------------------------------------
# 修订六返工（rev-sgpt Blocking 1-3 + non-blocking）
# ---------------------------------------------------------------------------


def sse_id_seq(raw: bytes) -> int | None:
    """Extract the wire ``id: <domain>:<epoch>:<seq>`` seq from one SSE
    frame. None when the frame carries no id
    line."""
    for line in raw.decode().split("\n"):
        if line.startswith("id: "):
            return int(line.rstrip().rsplit(":", 1)[1])
    return None


async def test_gate_blocks_token_route_and_disabled_key(pair):
    """(Blocking 1) GlobalHub retired gate 命中时：TokenHub 不被调用
    （spy 断言 call 级阻断——两 gate 独立清理，不能依赖 TokenHub 自身
    gate 兜底）、不产生 disabled key、不 bump digest。正对照：未 gate
    的 part.removed 正常路由且写入 disabled（防止过度拦截）。"""
    from oc_slimapi.sse.tokenstream.hub import TokenStreamHub

    hub, sub = pair
    th = TokenStreamHub(replay_log=current_replay_log())
    hub.set_token_hub(th)
    removed_calls: list[tuple[str, str, str]] = []
    orig_removed = th.on_part_removed

    def spy_removed(sid: str, mid: str, pid: str) -> None:
        removed_calls.append((sid, mid, pid))
        orig_removed(sid, mid, pid)

    th.on_part_removed = spy_removed
    updated_calls: list[dict] = []
    orig_updated = th.on_part_updated

    def spy_updated(props: dict) -> None:
        updated_calls.append(props)
        orig_updated(props)

    th.on_part_updated = spy_updated

    before = seq()
    hub.publish(ev("/p", "message.removed", {"sessionID": "s1", "messageID": "m1"}))
    assert seq() == before + 1  # message.removed 自身 bump
    # gate 现持有 (s1, m1)：迟到的 part 事件在 GlobalHub 侧被拦
    hub.publish(ev("/p", "message.part.removed", part_removed_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    assert removed_calls == []       # TokenHub.on_part_removed 未被调用
    assert updated_calls == []       # 对称：on_part_updated 同样被拦
    assert ("s1", "m1", "p1") not in th._disabled_parts  # 无 disabled key
    assert seq() == before + 1       # gate 命中零推进
    hub.flush()
    assert only_digests(await drain(sub)) == []
    # 正对照：未 gate 的 (s1, m2) 正常路由 → disabled key 写入（常规退役）
    hub.publish(ev("/p", "message.part.removed", part_removed_props("s1", "m2", "p9")))
    assert removed_calls == [("s1", "m2", "p9")]
    assert ("s1", "m2", "p9") in th._disabled_parts


async def test_v4_sse_id_digest_precedes_question_asked(hub):
    """(Blocking 2) v4 订阅者：解析 ``id:`` 行，断言 digest 的
    SSE id seq 严格小于 asked 的——真实线序（非仅队列位置）。"""
    sub = Subscriber()
    hub.subscribers.add(sub)
    before = seq()
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))  # pending
    hub.publish(ev("/p", "question.asked", asked_props("s1")))
    frames = await drain(sub)
    assert len(frames) == 2
    ids = [sse_id_seq(f) for f in frames]
    assert ids[0] is not None and ids[1] is not None
    assert ids[0] < ids[1]  # digest SSE id < asked SSE id
    name0, data0 = parse(frames[0])
    assert name0 == "session.digest"
    assert data0["messagesRevision"] == before + 1
    _, data1 = parse(frames[1])
    assert data1["type"] == "question.asked"


async def test_v4_sse_id_digest_precedes_question_v2_asked(hub):
    """(Blocking 2) question.v2.asked 同等 SSE id 线序覆盖（pending 经
    part.updated 入口造，顺带锁 part 路径的 id 线序）。"""
    sub = Subscriber()
    hub.subscribers.add(sub)
    before = seq()
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "question.v2.asked", asked_props("s1")))
    frames = await drain(sub)
    assert len(frames) == 2
    ids = [sse_id_seq(f) for f in frames]
    assert ids[0] is not None and ids[1] is not None
    assert ids[0] < ids[1]
    name0, data0 = parse(frames[0])
    assert name0 == "session.digest"
    assert data0["messagesRevision"] == before + 1
    _, data1 = parse(frames[1])
    assert data1["type"] == "question.v2.asked"


async def test_asked_bad_sessionid_pending_not_flushed(pair):
    """(Blocking 3) asked 的 sessionID 缺失/空/非 str → 无 targeted
    flush：已有 pending 留在 debounce 窗内（不提前、不丢失）。"""
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))  # pending
    for bad in ({}, {"sessionID": ""}, {"sessionID": 123}):
        props = {"id": "q1"}
        props.update(bad)
        hub.publish(ev("/p", "question.asked", props))
    frames = await drain(sub)
    assert len(frames) == 3  # 三条 asked 裸帧，无 digest 插入
    assert only_digests(frames) == []
    hub.flush()  # pending 到窗末正常成帧
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s1"


async def test_question_resolution_family_never_flushes_pending(pair):
    """(Blocking 3) question.replied/rejected（含 v2）即使带有效 sid 也
    不触发 flush-before-asked——因果闭合仅 asked 渲染族需要。"""
    hub, sub = pair
    hub.publish(ev("/p", "message.updated", msg_props("s1", "m1")))  # pending
    for event_type in (
        "question.replied", "question.rejected",
        "question.v2.replied", "question.v2.rejected",
    ):
        hub.publish(ev("/p", event_type,
                       {"sessionID": "s1", "requestID": "q1", "answers": []}))
    frames = await drain(sub)
    assert len(frames) == 4  # 四条裸帧，无 digest 插入
    assert only_digests(frames) == []
    hub.flush()
    assert len(only_digests(await drain(sub))) == 1  # pending 留窗成帧


async def test_part_events_cross_window_updated_at_monotonic(pair):
    """(non-blocking) part.updated 与 part.removed 分落两个 debounce 窗：
    第二窗 digest 的 updatedAt 严格大于第一窗（per-session 跨窗单调，
    _bump_updated_at 的 max(now, prev+1) 保证）。"""
    hub, sub = pair
    hub.publish(ev("/p", "message.part.updated", part_updated_props("s1", "m1", "p1")))
    hub.flush()
    first = only_digests(await drain(sub))
    assert len(first) == 1
    hub.publish(ev("/p", "message.part.removed", part_removed_props("s1", "m1", "p1")))
    hub.flush()
    second = only_digests(await drain(sub))
    assert len(second) == 1
    assert second[0]["updatedAt"] > first[0]["updatedAt"]


# ---------------------------------------------------------------------------
# 修订六终门控返工（rev-2）— §7.7 恢复协议 v2 收敛通道锚点 + §7.5 跨 sid
# ---------------------------------------------------------------------------


def part_textend_props(sid: str, mid: str, pid: str, text: str) -> dict:
    """Properties for a text-end ``message.part.updated``（time.end 已置、
    part.text 为插件可改写后的终态权威全文——仅落库走 REST）。"""
    return {
        "sessionID": sid,
        "part": {
            "id": pid,
            "messageID": mid,
            "sessionID": sid,
            "type": "text",
            "time": {"end": 1700000000000},
            "text": text,
        },
        "time": {},
    }


async def test_evicted_part_late_textend_still_bumps_revision(pair, monkeypatch):
    """（终门控 Blocking 2 闭合① / 驱逐矩阵② text-end 前驱逐）被逐
    part 迟到的 text-end part.updated 被 tokenstream disabled gate 拦截
    （零流帧、零 seq 消耗），但 GlobalHub 侧 revision **仍 bump** 并成
    digest——收敛通道锚点（§7.7 协议 v2 / §7.5：digest bump 与
    tokenstream 驱逐 gate 相互独立，`_retired_messages` gate 仅拦
    whole-removal）。被逐 part 终态由此经 revision → digest → 常规
    ?since/GET 对账收敛。"""
    from oc_slimapi.sse.replay_log import token_domain
    from oc_slimapi.sse.tokenstream.hub import TokenStreamHub

    hub, sub = pair
    log = current_replay_log()
    th = TokenStreamHub(replay_log=log)
    hub.set_token_hub(th)
    monkeypatch.setattr(
        "oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 1,
    )
    # token 侧铺场：A resident（live bytes == cap）+ 已 flush delta（seq 1）
    th.on_part_updated(part_updated_props("s1", "mA", "p1"))
    th.on_part_delta({"sessionID": "s1", "messageID": "mA", "partID": "p1",
                      "field": "text", "delta": "a"})
    th.flush()
    # B 准入驱逐 A → advisory resync（seq 2）+ B delta（seq 3）
    th.on_part_updated(part_updated_props("s1", "mB", "p1"))
    th.on_part_delta({"sessionID": "s1", "messageID": "mB", "partID": "p1",
                      "field": "text", "delta": "b"})
    th.flush()
    assert th.token_memory_limit_total == 1
    seq_before = log.last_seq(token_domain("s1"))

    # 被逐 part 的 text-end（插件改写终态 "REWRITTEN"）迟到：
    # tokenstream 侧 disabled gate 拦截（零帧零 seq）；
    # GlobalHub 侧不受该 gate 影响——bump 照常发生。
    before = seq()
    hub.publish(ev("/p", "message.part.updated",
                   part_textend_props("s1", "mA", "p1", "REWRITTEN")))
    assert seq() == before + 1  # 收敛通道锚点：bump 不被驱逐拦截
    assert log.last_seq(token_domain("s1")) == seq_before  # 零流帧零 seq
    hub.flush()
    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s1"
    assert digests[0]["messagesRevision"] == before + 1  # 终态变化可见


async def test_cross_sid_wire_order_inverse_revision(pair):
    """（终门控 Blocking 4 闭合）sid-A 分配 N、sid-B 分配 N+1：先
    asked-flush B（targeted flush）、后批量 flush A → wire 序
    ``[N+1, N]`` 且两帧均为该 sid 的有效 digest——allocator 事件循环内
    全局递增（§7.5 ①），但跨 sid wire 顺序可合法逆序（§7.5 ③：禁止用
    其他 sid 的 max revision 丢弃/去重当前 sid digest）。"""
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.updated", msg_props("sA", "mA")))  # → N
    hub.publish(ev("/p", "message.updated", msg_props("sB", "mB")))  # → N+1
    assert seq() == before + 2
    n_a, n_b = before + 1, before + 2
    # B 先 asked → targeted flush：sB digest（revision N+1）先上线
    hub.publish(ev("/p", "question.asked", asked_props("sB")))
    # A 后批量 flush：sA digest（revision N）后上线
    hub.flush()
    digests = only_digests(await drain(sub))
    assert [d["sessionID"] for d in digests] == ["sB", "sA"]
    assert [d["messagesRevision"] for d in digests] == [n_b, n_a]  # [N+1, N]
    # 两帧各自携带本 sid 的 window-end 修订（有效 digest，非乱序可丢弃）
    assert n_b == n_a + 1  # 分配序全局递增 ≠ wire 序
