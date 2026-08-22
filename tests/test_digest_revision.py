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
* ``message.part.*`` events NEVER bump. Session-only events never bump.
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
    h = GlobalHub(client=None)
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


async def test_message_part_events_do_not_bump(pair):
    hub, sub = pair
    before = seq()
    hub.publish(ev("/p", "message.part.updated",
                   part_updated_props("s1", "m1", "p1")))
    hub.publish(ev("/p", "message.part.delta",
                   part_updated_props("s1", "m1", "p1")))
    assert seq() == before
    # part events produce no digest entry at all
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
    assert seq() == bumped  # part events never bump


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
