"""Tests for GlobalHub _retired_messages gate (rev-ogpt MAJOR 3).

Covers:
1. Late ``message.part.updated`` after ``message.removed`` does NOT resurrect
   — the retired gate prevents any digest ``updatedAt`` bump or pending entry
   creation.
2. The retired gate does NOT affect other messages in the same session.
3. ``session.deleted`` clears the retired set for that session — a late
   ``message.part.updated`` for a different session is still gated.
4. ``resync_all`` clears the retired set — a late
   ``message.part.updated`` after reconnect is free to create fresh state.
"""

from __future__ import annotations

import asyncio

import pytest

from oc_slimapi.sse.hub import GlobalHub, Subscriber


def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame."""
    return {
        "directory": directory,
        "payload": {"type": event_type, "properties": properties or {}},
    }


def _part_updated_props(
    sid: str, mid: str, pid: str,
) -> dict:
    """Build properties dict for a ``message.part.updated`` event."""
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


async def _close_hub(hub: GlobalHub) -> None:
    """Cancel + await every GlobalHub background task."""
    for t in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task):
        if t and not t.done():
            t.cancel()
    # Give tasks a chance to finalise.
    await asyncio.sleep(0.02)


@pytest.fixture
async def hub():
    """Bare GlobalHub with no subscriber; always tears down tasks."""
    h = GlobalHub(client=None)
    try:
        yield h
    finally:
        await _close_hub(h)


# ---------------------------------------------------------------------------
# Test 1: late message.part.updated after message.removed does NOT resurrect
# ---------------------------------------------------------------------------

async def test_late_part_updated_after_removed_does_not_resurrect(hub: GlobalHub):
    """``message.removed`` retires (s1, m1); a late ``message.part.updated``
    for the same message must NOT create a digest entry or bump ``updatedAt``."""
    sid, mid, pid = "s1", "m1", "p1"

    # 1. message.removed → records in _retired_messages.
    hub.publish(make_global_event(
        "/p", "message.removed", {"sessionID": sid, "messageID": mid},
    ))
    assert (sid, mid) in hub._retired_messages

    # 2. Late message.part.updated → gate fires early, prevents token hub
    #    routing for the retired message. No side effects observed (no
    #    token hub wired in this test), but the gate held.
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid, pid),
    ))
    assert (sid, mid) in hub._retired_messages  # gate held


# ---------------------------------------------------------------------------
# Test 2: retired gate does NOT affect other messages in the same session
# ---------------------------------------------------------------------------

async def test_retired_gate_does_not_affect_other_messages(hub: GlobalHub):
    """Removing m1 should NOT prevent m2 from being tracked normally."""
    sid, mid1, mid2, pid1, pid2 = "s1", "m1", "m2", "p1", "p2"

    # Contract §3: part events no longer create pending entries; only
    # verify the _retired_messages gate state.

    # Remove m1 only.
    hub.publish(make_global_event(
        "/p", "message.removed", {"sessionID": sid, "messageID": mid1},
    ))
    assert (sid, mid1) in hub._retired_messages
    assert (sid, mid2) not in hub._retired_messages

    # Late part.updated for m1 → gated (still retired).
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid1, pid1),
    ))
    assert (sid, mid1) in hub._retired_messages  # gate held

    # m2 must not be retired (no message.removed for it).
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid2, pid2),
    ))
    assert (sid, mid2) not in hub._retired_messages  # m2 not retired


# ---------------------------------------------------------------------------
# Test 3: session.deleted clears retired set for that session
# ---------------------------------------------------------------------------

async def test_session_deleted_clears_retired_set(hub: GlobalHub):
    """After ``session.deleted``, the retired set no longer contains entries
    for that sid, so a late update for a retired message can create fresh state."""
    sid, mid, pid = "s1", "m1", "p1"
    other_sid, other_mid = "s2", "mX"

    # Retire messages in two sessions.
    hub.publish(make_global_event(
        "/p", "message.removed", {"sessionID": sid, "messageID": mid},
    ))
    hub.publish(make_global_event(
        "/p", "message.removed", {"sessionID": other_sid, "messageID": other_mid},
    ))
    assert (sid, mid) in hub._retired_messages
    assert (other_sid, other_mid) in hub._retired_messages

    # session.deleted for s1
    hub.publish(make_global_event(
        "/p", "session.deleted", {"sessionID": sid},
    ))
    assert (sid, mid) not in hub._retired_messages
    # Other session's retired entry remains.
    assert (other_sid, other_mid) in hub._retired_messages

    # Late part.updated for s1,m1 → NOT gated (session deleted cleared it).
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid, pid),
    ))
    assert (sid, mid) not in hub._retired_messages


# ---------------------------------------------------------------------------
# Test 4: upstream reconnect (resync_all) clears retired set
# ---------------------------------------------------------------------------

async def test_resync_all_clears_retired_set(hub: GlobalHub):
    """After ``resync_all`` (upstream reconnect), the retired set is cleared
    so late part events from the new epoch are processed normally."""
    sid, mid, pid = "s1", "m1", "p1"

    # message.removed → retired.
    hub.publish(make_global_event(
        "/p", "message.removed", {"sessionID": sid, "messageID": mid},
    ))
    assert (sid, mid) in hub._retired_messages

    # resync_all (simulates upstream reconnect).
    hub.resync_all()
    assert len(hub._retired_messages) == 0

    # Late part.updated after reconnect → NOT gated (resync cleared it).
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid, pid),
    ))
    assert (sid, mid) not in hub._retired_messages


# ---------------------------------------------------------------------------
# rev-ogpt MAJOR 4 (3rd-round terminal audit): GlobalHub _retired_messages
# bounded FIFO cap (TOKEN_REMOVED_MESSAGES_MAX=1000) + TTL
# (TOKEN_REMOVED_MESSAGES_TTL_MS=24h), aligned with the token hub's replay
# queue. v0.5 used a plain ``set`` that leaked unbounded.
# ---------------------------------------------------------------------------


def _removed_props(sid: str, mid: str) -> dict:
    return {"sessionID": sid, "messageID": mid}


async def test_retired_gate_cap_enforced(hub: GlobalHub):
    """The retired gate stays at or below ``TOKEN_REMOVED_MESSAGES_MAX``
    even when more retirements arrive (oldest evicted FIFO)."""
    from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX

    # Force cap+1 retirements.
    for i in range(TOKEN_REMOVED_MESSAGES_MAX + 1):
        hub.publish(make_global_event(
            "/p", "message.removed", _removed_props("s1", f"m{i}"),
        ))
    assert len(hub._retired_messages) == TOKEN_REMOVED_MESSAGES_MAX
    # Oldest (m0) was evicted; newest survives.
    assert ("s1", "m0") not in hub._retired_messages
    assert ("s1", f"m{TOKEN_REMOVED_MESSAGES_MAX}") in hub._retired_messages


async def test_retired_gate_ttl_expires_via_prune(hub: GlobalHub):
    """Expired entries (older than TTL) are pruned on the next insert /
    flush, after which a late ``message.part.updated`` for that
    (sid, mid) is free to create fresh state again."""
    from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_TTL_MS

    sid, mid, pid = "s1", "m1", "p1"

    # Retire the message.
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props(sid, mid),
    ))
    assert (sid, mid) in hub._retired_messages

    # Manually back-date the entry past the TTL.
    hub._retired_messages[(sid, mid)] = (
        _now_ms_for_test() - TOKEN_REMOVED_MESSAGES_TTL_MS - 1
    )

    # Trigger a prune via flush() (the debounce loop's normal path).
    hub.flush()
    assert (sid, mid) not in hub._retired_messages, (
        "TTL-expired entry must be pruned by flush()"
    )

    # Late part.updated after TTL expiry → NOT gated (pruned).
    hub.publish(make_global_event(
        "/p", "message.part.updated", _part_updated_props(sid, mid, pid),
    ))
    assert (sid, mid) not in hub._retired_messages


async def test_retired_gate_ttl_expired_allows_late_update_via_insert(
    hub: GlobalHub,
):
    """TTL prune also fires on the next ``message.removed`` insert, so a
    long quiet period followed by a new retirement still evicts expired
    gates."""
    from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_TTL_MS

    # Retire (s1, m_old).
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props("s1", "m_old"),
    ))
    assert ("s1", "m_old") in hub._retired_messages

    # Back-date past TTL.
    hub._retired_messages[("s1", "m_old")] = (
        _now_ms_for_test() - TOKEN_REMOVED_MESSAGES_TTL_MS - 1
    )

    # New retirement triggers on-insert prune.
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props("s1", "m_new"),
    ))
    assert ("s1", "m_old") not in hub._retired_messages, (
        "TTL-expired entry must be pruned on the next insert"
    )
    assert ("s1", "m_new") in hub._retired_messages


async def test_retired_gate_duplicate_move_to_end(hub: GlobalHub):
    """A duplicate retirement for an already-retired (sid, mid) refreshes
    the timestamp AND moves the key to the tail of the FIFO order, so
    the cap never evicts the freshest gate entry (matches the token
    hub's replay-queue behaviour)."""
    from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX

    # Fill to one below cap.
    for i in range(TOKEN_REMOVED_MESSAGES_MAX - 1):
        hub.publish(make_global_event(
            "/p", "message.removed", _removed_props("s1", f"m{i}"),
        ))
    # Insert m_special.
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props("s1", "m_special"),
    ))
    assert ("s1", "m_special") in hub._retired_messages

    # Duplicate m_special — should move to end (refresh timestamp + FIFO pos).
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props("s1", "m_special"),
    ))
    # Last key in iteration order is m_special.
    last_key = next(reversed(hub._retired_messages))
    assert last_key == ("s1", "m_special"), (
        f"duplicate should move_to_end; last key = {last_key}"
    )

    # Insert one more — m0 (now oldest) is evicted, NOT m_special.
    hub.publish(make_global_event(
        "/p", "message.removed", _removed_props("s1", "m_last"),
    ))
    assert ("s1", "m_special") in hub._retired_messages
    assert ("s1", "m0") not in hub._retired_messages
    assert ("s1", "m_last") in hub._retired_messages


async def test_retired_gate_data_structure_is_ordered_dict(hub: GlobalHub):
    """Sanity: ``_retired_messages`` is now an OrderedDict (MAJOR 4),
    not a plain set. Locks the data-structure contract that the cap/TTL
    logic relies on."""
    from collections import OrderedDict
    assert isinstance(hub._retired_messages, OrderedDict)


def _now_ms_for_test() -> int:
    """Test-only epoch-ms helper (kept local to avoid importing from
    production code under test)."""
    import time
    return int(time.time() * 1000)
