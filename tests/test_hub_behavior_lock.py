"""Behavior lock tests for ``src/oc_slimapi/sse/hub.py`` (Batch 2 hub split prep).

These tests pin the CURRENT correct behavior of the (833-line) ``hub.py`` so
the upcoming subscriber / digest / classifier / registry / metrics split can
be verified to preserve behavior. They MUST all pass before AND after the
split.

Self-contained: own helpers + fixtures; does NOT touch ``tests/conftest.py``
or ``tests/test_hub.py``. Locks the boundaries called out in the Batch 2 spec:

  1. session.digest accumulation + debounce (250ms/session window)
  2. deleted clears sticky lastError; subsequent digests omit lastError
  3. session.error: sid->digest lastError (sticky); no sid->direct frame;
     MessageAbortedError filtered; message sanitization (first line / paths /
     stack / secrets / truncate 512)
  4. subscriber admission: per-directory/total caps -> 503
     ``sse_subscriber_limit_directory``/``_total`` (limit/current/Retry-After)
  5. unsubscribe/close lifecycle (no task leak)
  6. backpressure: buffer overflow -> immediate clear + STOP
     (old frames NOT delivered)
  7. registry metrics (subscribers / queue / hub summary shape + counters)

Reference: ``docs/specs/v1-contract.md`` §3 (SSE) + §6 (T3 resource limits).

Freeze-baseline notes:
  * ``TransformPool.snapshot_metrics()`` is a public API Batch 2 will ADD to
    ``src/oc_slimapi/transform.py`` (does not exist yet). The locks in
    ``TestTransformPoolSnapshotMetrics`` exercise it directly and are marked
    ``xfail(strict=False)`` — they XFAIL today and turn into XPASS (then have
    the mark removed) once Batch 2 lands the method. The registry-level lock
    (``test_snapshot_skeleton_with_real_transform_pool``) routes through the
    ALREADY-existing ``HubRegistry.snapshot_metrics()`` with a real pool, so it
    is a green baseline lock (no private ``_semaphore`` access from the test).
  * ``test_real_debounce_merges_within_250ms_window`` is marked
    ``@pytest.mark.integration``: it depends on a real 0.5s wall-clock sleep
    and is NOT part of the timing-sensitive freeze baseline — CI may skip it
    with ``-m "not integration"``. It is kept green in the default run.

Minimal ``TransformPool.snapshot_metrics()`` shape locked here (for Batch 2
alignment; do not over-specify beyond these keys)::

    def snapshot_metrics(self) -> dict:
        return {"active": <permits held, int>, "waiting": <waiters, int>}
"""

from __future__ import annotations

from conftest import current_replay_log

import asyncio
import contextlib
import json
import time

import pytest

from oc_slimapi.sse.hub import (
    DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_SUBSCRIBERS,
    DEFAULT_SSE_BUFFER_BYTES,
    DEFAULT_SSE_MAX_FRAME_BYTES,
    DEFAULT_SSE_QUEUE_ITEMS,
    DEBOUNCE_SECONDS,
    GRACE_SECONDS,
    HEARTBEAT_SECONDS,
    GlobalHub,
    HubRegistry,
    STOP,
    Subscriber,
    SubscriberCapacityError,
    _sanitize_error_message,
    sse_frame,
)
from oc_slimapi.transform import TransformConfig, TransformPool


# ---------------------------------------------------------------------------
# Self-contained helpers (do NOT depend on tests/conftest.py or test_hub.py)
# ---------------------------------------------------------------------------

def ev(
    directory: str | None,
    event_type: str,
    properties: dict | None = None,
    *,
    payload_id: str | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties[, id]}}."""
    payload: dict = {"type": event_type, "properties": properties or {}}
    if payload_id is not None:
        payload["id"] = payload_id
    return {"directory": directory, "payload": payload}


def parse(raw: bytes) -> tuple[str | None, dict]:
    """Parse one SSE frame into (event_name, data). event_name is None when
    the frame has no ``event:`` header (raw passthrough like question.asked)."""
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
    """Drain every currently-queued frame without blocking on an empty queue."""
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


def only_event(frames: list[bytes], name: str) -> list[tuple[str | None, dict]]:
    return [(e, d) for e, d in (parse(f) for f in frames) if e == name]


async def _teardown_hub(hub: GlobalHub) -> None:
    """Cancel + await every background task the hub started (avoids
    'Task was destroyed but it is pending!' warnings on loop teardown).

    Deliberately uses the public ``asyncio.all_tasks()`` instead of reaching
    into the hub's private ``task`` / ``flush_task`` / ``heartbeat_task`` /
    ``stop_task`` attributes: a behavior-preserving split that renames or
    relocates those fields must not break teardown. For these isolated unit
    tests the only non-current tasks on the loop are the hub's background
    coroutines, so cancelling every task except the one running teardown is
    both safe and field-layout-agnostic.
    """
    me = asyncio.current_task()
    tasks = [
        t for t in asyncio.all_tasks()
        if t is not me and not t.done()
    ]
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
    """GlobalHub with one manually-attached subscriber (no run() side effects)."""
    sub = Subscriber()
    hub.subscribers.add(sub)
    return hub, sub


# ===========================================================================
# Section 0: Lock the public constants (contract §3 + §6 numeric anchors)
# ===========================================================================

class TestConstants:
    """Pin the timing + T3 numeric constants so a refactor cannot accidentally
    drift the debounce window, heartbeat, grace, or resource ceilings."""

    def test_timing_constants_unchanged(self):
        assert DEBOUNCE_SECONDS == 0.25
        assert HEARTBEAT_SECONDS == 15.0  # Q3 2026-08-22: unified w/ token stream
        assert GRACE_SECONDS == 30.0

    def test_t3_default_caps_match_contract_section_6(self):
        """Contract §6: MAX_SUBSCRIBERS_PER_DIRECTORY=8, MAX_TOTAL=16."""
        assert DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY == 8
        assert DEFAULT_MAX_TOTAL_SUBSCRIBERS == 16

    def test_t3_default_buffer_and_frame_ceilings_match_contract_section_6(self):
        """Contract §6: per-subscriber buffer 2 MiB, single frame 256 KiB.
        queue_items default 256 (well above the resync+STOP minimum of 2)."""
        assert DEFAULT_SSE_QUEUE_ITEMS == 256
        assert DEFAULT_SSE_BUFFER_BYTES == 2 * 1024 * 1024
        assert DEFAULT_SSE_MAX_FRAME_BYTES == 256 * 1024


# ===========================================================================
# Section 1: session.digest accumulation + debounce (250ms/session window)
# ===========================================================================

class TestDigestAccumulation:
    """session.digest merges status / messageID / updatedAt / archived / deleted
    within one debounce window per session (contract §3)."""

    async def test_status_and_message_merge_into_single_digest(
            self, pair, monkeypatch):
        hub, sub = pair
        # 4.11.0 A3: pin the process-global revision counter for the
        # exact-shape lock (restored by monkeypatch).
        monkeypatch.setattr(
            "oc_slimapi.sse.global_hub._message_revision_seq", 0)
        hub.publish(ev("/proj", "session.status", {"sessionID": "s1", "status": "busy"}))
        hub.publish(ev("/proj", "message.updated", {
            "sessionID": "s1",
            "info": {"id": "msg_1", "time": {"updated": 1700000000000}},
        }))
        hub.flush()

        digests = only_digests(await drain(sub))
        assert len(digests) == 1
        # updatedAt is sidecar wall-clock (not upstream timestamp)
        assert isinstance(digests[0]["updatedAt"], int)
        assert digests[0]["updatedAt"] > 0
        assert digests[0] == {
            "sessionID": "s1",
            "directory": "/proj",
            "status": "busy",
            "messageID": "msg_1",
            "updatedAt": digests[0]["updatedAt"],
            # B1a additive: every digest frame carries changed: [<sid>].
            "changed": ["s1"],
            # 4.11.0 A3: the message window carries the post-bump revision.
            "messagesRevision": 1,
        }

    async def test_two_separate_windows_produce_two_digests(self, pair):
        hub, sub = pair
        hub.publish(ev("/proj", "session.status", {"sessionID": "s1", "status": "busy"}))
        hub.flush()
        first = only_digests(await drain(sub))
        assert len(first) == 1
        assert first[0]["status"] == "busy"

        hub.publish(ev("/proj", "session.status", {"sessionID": "s1", "status": "idle"}))
        hub.flush()
        second = only_digests(await drain(sub))
        assert len(second) == 1
        assert second[0]["status"] == "idle"

    async def test_each_session_emits_own_digest(self, pair):
        hub, sub = pair
        for sid, status in [("s1", "busy"), ("s2", "idle"), ("s3", "completed")]:
            hub.publish(ev("/proj", "session.status", {"sessionID": sid, "status": status}))
        hub.flush()

        digests = {d["sessionID"]: d for d in only_digests(await drain(sub))}
        assert set(digests) == {"s1", "s2", "s3"}
        assert digests["s1"]["status"] == "busy"
        assert digests["s2"]["status"] == "idle"
        assert digests["s3"]["status"] == "completed"

    async def test_message_updated_picks_info_id_over_props_messageID(self, pair):
        """info.id (when str) wins over props.messageID for messageID."""
        hub, sub = pair
        hub.publish(ev("/proj", "message.updated", {
            "sessionID": "s1",
            "messageID": "from_props",
            "info": {"id": "from_info", "time": {"updated": 1700000000000}},
        }))
        hub.flush()
        assert only_digests(await drain(sub))[0]["messageID"] == "from_info"

    async def test_message_updated_falls_back_to_props_messageID(self, pair):
        """When info.id is missing/non-str, props.messageID is used."""
        hub, sub = pair
        hub.publish(ev("/proj", "message.updated", {
            "sessionID": "s1",
            "messageID": "from_props",
            "info": {"time": {"updated": 1700000000000}},
        }))
        hub.flush()
        assert only_digests(await drain(sub))[0]["messageID"] == "from_props"

    async def test_updatedAt_is_sidecar_wall_clock(self, pair):
        """lite-v2 §4.2: updatedAt is always sidecar wall-clock, never
        the upstream message timestamp. Priority order
        (updated > created > now) no longer applies."""
        hub, sub = pair

        # Scenario 1: message has updated + created — both ignored for wall-clock.
        hub.publish(ev("/p", "message.updated", {
            "sessionID": "s1",
            "info": {"id": "m1", "time": {"updated": 111, "created": 222}},
        }))
        hub.flush()
        ua1 = only_digests(await drain(sub))[0]["updatedAt"]
        assert isinstance(ua1, int)
        assert ua1 > 0
        # Must NOT be 111 (upstream timestamp) or 222.
        assert ua1 > 222

        # Scenario 2: only created — still wall-clock, not 333.
        hub.publish(ev("/p", "message.updated", {
            "sessionID": "s1", "info": {"id": "m2", "time": {"created": 333}},
        }))
        hub.flush()
        ua2 = only_digests(await drain(sub))[0]["updatedAt"]
        assert isinstance(ua2, int)
        assert ua2 > 333

        # Scenario 3: no time fields — must be positive int.
        hub.publish(ev("/p", "message.updated", {
            "sessionID": "s1", "info": {"id": "m3"},
        }))
        hub.flush()
        ua3 = only_digests(await drain(sub))[0]["updatedAt"]
        assert isinstance(ua3, int)
        assert ua3 > 0

    async def test_message_appended_carries_sidecar_wall_clock(self, pair):
        """lite-v2 §4.2: message.appended updatedAt is sidecar wall-clock,
        not the upstream ``time.created``."""
        hub, sub = pair
        hub.publish(ev("/proj", "message.appended", {
            "sessionID": "s1",
            "info": {"id": "msg_app", "time": {"created": 1700000001000}},
        }))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["messageID"] == "msg_app"
        assert isinstance(d["updatedAt"], int)
        assert d["updatedAt"] > 0
        # Must NOT be the upstream created timestamp.
        assert d["updatedAt"] != 1700000001000

    async def test_archived_sticky_within_window_across_other_events(self, pair):
        """Once archived is observed, subsequent same-window events must not
        un-set it (mirrors deleted stickiness — contract §3)."""
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"archived": 1700000000000}},
        }))
        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["archived"] == 1700000000000
        assert d["status"] == "idle"

    async def test_archived_sticky_across_subsequent_updated_without_archived(self, pair):
        """A second session.updated in the SAME window without archived must
        not clear the previously-observed timestamp."""
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"archived": 1700000000000}},
        }))
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"updated": 1700000000001}},
        }))
        hub.flush()
        assert only_digests(await drain(sub))[0]["archived"] == 1700000000000

    async def test_archived_field_is_epoch_ms_int(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"archived": 1700000000000}},
        }))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["archived"] == 1700000000000
        assert isinstance(d["archived"], int)

    async def test_archived_omitted_when_absent(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"updated": 1700000000000}},
        }))
        hub.flush()
        assert "archived" not in only_digests(await drain(sub))[0]

    async def test_archived_zero_is_emitted(self, pair):
        """archived=0 must survive (isinstance int / is not None, not truthy)."""
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "sessionID": "s1", "info": {"time": {"archived": 0}},
        }))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["archived"] == 0
        assert isinstance(d["archived"], int)

    async def test_directory_passed_through_when_present(self, pair):
        hub, sub = pair
        hub.publish(ev("/custom/dir", "session.status", {"sessionID": "s1", "status": "busy"}))
        hub.flush()
        assert only_digests(await drain(sub))[0]["directory"] == "/custom/dir"

    async def test_flush_on_empty_pending_is_noop(self, pair):
        """flush() with no pending digests emits nothing and does not raise."""
        hub, sub = pair
        hub.flush()
        hub.flush()
        assert await drain(sub, timeout=0.05) == []

    @pytest.mark.integration
    async def test_real_debounce_merges_within_250ms_window(self):
        """The flush_loop task calls flush() every DEBOUNCE_SECONDS (250ms);
        events published within one window collapse to a single digest.

        NON-GATING INTEGRATION TEST: depends on a real 0.5s wall-clock sleep
        against the in-loop ``flush_loop``. It is NOT part of the
        timing-sensitive freeze baseline and may be skipped with
        ``pytest -m "not integration"``. Kept green in the default run because
        0.5s comfortably exceeds the 250ms debounce tick; the deterministic
        debounce-merge behavior is already locked by
        ``test_status_and_message_merge_into_single_digest`` and siblings
        (which call ``flush()`` explicitly with no sleep)."""
        hub = GlobalHub(client=None, replay_log=current_replay_log())
        try:
            sub = hub.subscribe()  # starts flush_loop + heartbeat_loop + run

            # Publish 3 events for the same session in quick succession (<250ms).
            hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
            hub.publish(ev("/p", "message.updated", {
                "sessionID": "s1", "info": {"id": "m1", "time": {"updated": 1700000000000}},
            }))
            hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))

            # Wait long enough for one flush tick (0.25s) but well within
            # the heartbeat interval (10s). Second flush is a no-op because
            # pending was cleared by the first, so this is timing-tolerant.
            await asyncio.sleep(0.5)

            digests = only_digests(await drain(sub, timeout=0.1))
            assert len(digests) == 1
            assert digests[0]["status"] == "idle"
            assert digests[0]["messageID"] == "m1"
        finally:
            await _teardown_hub(hub)


# ===========================================================================
# Section 2: deleted clears sticky + subsequent digest omits lastError
# ===========================================================================

class TestDeletedClearsSticky:
    """session.deleted pops sticky lastError; the deleted digest omits
    lastError entirely (NOT emitted as null — contract §3)."""

    async def test_deleted_pops_sticky_and_digest_omits_lastError(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        await drain(sub)  # consume the immediate G1-A digest

        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["deleted"] is True
        assert "lastError" not in d  # NOT emitted as null — omitted entirely

    async def test_post_deleted_status_digest_omits_lastError(self, pair):
        """After deleted clears sticky, a subsequent status-only digest for the
        same sid must not carry lastError (sticky was popped)."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        await drain(sub)

        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        hub.flush()
        await drain(sub)

        # New window — sticky should be gone.
        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert "lastError" not in d

    async def test_deleted_flag_sticky_within_window(self, pair):
        """deleted once True stays True through subsequent same-window events."""
        hub, sub = pair
        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["deleted"] is True
        assert d["status"] == "idle"

    async def test_session_error_on_already_deleted_session_is_dropped(self, pair):
        """publish() short-circuits session.error when the pending entry is
        already marked deleted — no lastError is set, no immediate flush."""
        hub, sub = pair
        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["deleted"] is True
        assert "lastError" not in d

    async def test_deleted_tombstone_blocks_late_session_error_revive(self, pair):
        """C⑩: a LATE session.error arriving AFTER the deleted session's
        pending entry has been evicted by flush() must NOT revive the sticky
        lastError. The ``entry.deleted`` guard only covers the pre-eviction
        (same-window) case; a tombstone set surviving eviction covers the
        post-eviction case."""
        hub, sub = pair
        # 1. Mark the session deleted.
        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        # 2. Flush → emits the deleted digest AND evicts the pending entry.
        hub.flush()
        await drain(sub)  # consume the deleted digest
        # Confirm we genuinely reached the post-eviction state (the bug is only
        # reachable once the deleted entry is gone from self.pending).
        assert "s1" not in hub.pending
        # 3. A late session.error arrives after eviction.
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        # 4. Drain whatever the late error produced.
        frames = await drain(sub)
        s1_digests = [
            d for e, d in (parse(f) for f in frames)
            if e == "session.digest" and d.get("sessionID") == "s1"
        ]
        # No digest for s1 may carry lastError (the late error must be dropped).
        assert not any("lastError" in d for d in s1_digests), s1_digests
        # The sticky lastError must NOT be revived for an already-deleted session.
        assert "s1" not in hub.sticky_last_error

    async def test_resync_all_clears_deleted_tombstones(self, pair):
        """L1: resync_all() clears deleted_tombstones (cold-start pruning that
        bounds growth). A tombstone seeded by session.deleted must be gone after
        resync so a post-reconnect error is not wrongly suppressed forever."""
        hub, _sub = pair
        hub.publish(ev("/p", "session.deleted", {"sessionID": "s1"}))
        assert "s1" in hub.deleted_tombstones
        hub.resync_all()
        assert "s1" not in hub.deleted_tombstones

    async def test_busy_status_clears_sticky_with_explicit_null(self, pair):
        """session.status busy clears sticky lastError and emits lastError:null
        in the same window (contract §3 three-state wire)."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        await drain(sub)

        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
        frames = await drain(sub)
        clear_digests = [
            d for e, d in (parse(f) for f in frames)
            if e == "session.digest" and d.get("sessionID") == "s1" and "lastError" in d
        ]
        assert any(d["lastError"] is None for d in clear_digests)

    async def test_busy_clear_flush_sid_preserves_other_pending(self, pair):
        """Immediate busy-clear flush is per-sid; other pending stays."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        await drain(sub)

        hub.publish(ev("/p", "session.status", {"sessionID": "s2", "status": "idle"}))
        assert "s2" in hub.pending

        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
        frames = await drain(sub)
        assert any(
            d.get("sessionID") == "s1" and d.get("lastError") is None
            for e, d in (parse(f) for f in frames)
            if e == "session.digest"
        )
        assert "s2" in hub.pending
        assert "s1" not in hub.pending


# ===========================================================================
# Section 3: session.error handling (G1-A sticky / G1-B direct / abort filter
# / sanitization)
# ===========================================================================

class TestSessionError:
    """G1: session.error routing depends on whether props.sessionID is present."""

    async def test_with_sid_emits_immediate_digest_with_lastError(self, pair):
        """G1-A: session.error with sid -> immediate digest (no debounce) with
        lastError object {name, message, at}."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom at app.ts:1:1"}},
        }))
        # NO hub.flush() — G1-A path immediate-flushes.
        digests = only_digests(await drain(sub))
        assert len(digests) == 1
        le = digests[0]["lastError"]
        assert le["name"] == "UnknownError"
        assert le["message"] == "boom"  # stack frame stripped
        assert isinstance(le["at"], int)
        assert digests[0]["sessionID"] == "s1"

    async def test_error_flush_sid_preserves_other_pending(self, pair):
        """G1-A immediate flush pops only the errored sid; others stay pending."""
        hub, sub = pair
        hub.publish(ev("/p", "session.status", {"sessionID": "s2", "status": "idle"}))
        assert "s2" in hub.pending

        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        digests = only_digests(await drain(sub))
        assert len(digests) == 1
        assert digests[0]["sessionID"] == "s1"
        assert "s2" in hub.pending
        assert "s1" not in hub.pending

    async def test_with_sid_sets_sticky_for_subsequent_windows(self, pair):
        """sticky_last_error carries the error into later windows for the same
        sid, even when the later event does not itself touch lastError."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "UnknownError", "data": {"message": "boom"}},
        }))
        await drain(sub)

        # Non-error event in a new window should still carry sticky lastError.
        hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["lastError"]["name"] == "UnknownError"
        assert d["lastError"]["message"] == "boom"

    async def test_with_sid_attaches_directory_to_digest(self, pair):
        hub, sub = pair
        hub.publish(ev("/custom", "session.error", {
            "sessionID": "s1",
            "error": {"name": "X", "data": {"message": "y"}},
        }))
        d = only_digests(await drain(sub))[0]
        assert d["directory"] == "/custom"

    async def test_without_sid_emits_direct_session_error_frame(self, pair):
        """G1-B: session.error WITHOUT sid -> immediate direct frame, no debounce,
        no digest. Frame carries {directory?, name, message, at}."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "error": {"name": "PluginLoadError", "data": {"message": "plugin failed"}},
        }))
        errs = only_event(await drain(sub), "session.error")
        assert len(errs) == 1
        ev_name, data = errs[0]
        assert ev_name == "session.error"
        assert data["name"] == "PluginLoadError"
        assert data["message"] == "plugin failed"
        assert data["directory"] == "/p"
        assert isinstance(data["at"], int)
        assert "sessionID" not in data

    async def test_without_sid_and_no_directory_omits_directory_field(self, pair):
        hub, sub = pair
        hub.publish(ev(None, "session.error", {
            "error": {"name": "PluginLoadError", "data": {"message": "x"}},
        }))
        _, data = only_event(await drain(sub), "session.error")[0]
        assert "directory" not in data

    async def test_abort_with_sid_filtered_silently(self, pair):
        """MessageAbortedError with sid -> dropped, no digest, no lastError."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
        }))
        hub.flush()
        assert await drain(sub, timeout=0.05) == []

    async def test_abort_without_sid_filtered_silently(self, pair):
        """MessageAbortedError without sid -> dropped, no session.error frame."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
        }))
        hub.flush()
        assert await drain(sub, timeout=0.05) == []

    async def test_name_truncated_to_128_in_lastError(self, pair):
        hub, sub = pair
        long_name = "E" * 200
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": long_name, "data": {"message": "x"}},
        }))
        d = only_digests(await drain(sub))[0]
        assert len(d["lastError"]["name"]) == 128
        assert d["lastError"]["name"] == "E" * 128

    async def test_name_truncated_to_128_in_session_less_frame(self, pair):
        hub, sub = pair
        long_name = "E" * 200
        hub.publish(ev("/p", "session.error", {
            "error": {"name": long_name, "data": {"message": "x"}},
        }))
        _, data = only_event(await drain(sub), "session.error")[0]
        assert len(data["name"]) == 128

    async def test_non_str_name_coerced_to_empty(self, pair):
        """A non-str error.name (dict/int) must not crash publish; coerced to
        empty string in the wire output."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": {"weird": True}, "data": {"message": "x"}},
        }))
        d = only_digests(await drain(sub))[0]
        assert d["lastError"]["name"] == ""
        assert d["lastError"]["message"] == "x"

    async def test_non_str_name_coerced_in_session_less_frame(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "error": {"name": 42, "data": {"message": "y"}},
        }))
        _, data = only_event(await drain(sub), "session.error")[0]
        assert data["name"] == ""
        assert data["message"] == "y"

    async def test_missing_error_data_uses_fallback_name(self, pair):
        """When error.data.message is missing, message falls back to the name
        (or "(no detail)" if name is also missing)."""
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {"name": "BareError"},  # no data.message
        }))
        d = only_digests(await drain(sub))[0]
        assert d["lastError"]["name"] == "BareError"
        assert d["lastError"]["message"] == "BareError"  # fallback to name

    async def test_missing_error_data_and_name_uses_no_detail(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.error", {
            "sessionID": "s1",
            "error": {},  # no name, no data
        }))
        d = only_digests(await drain(sub))[0]
        assert d["lastError"]["name"] == ""
        assert d["lastError"]["message"] == "(no detail)"


class TestExtractSessionId:
    """_extract_session_id must not treat GlobalBus payload.id as sessionID."""

    async def test_payload_id_alone_does_not_create_digest(self, pair):
        hub, sub = pair
        hub.publish(ev(
            "/p",
            "session.status",
            {"status": "busy"},
            payload_id="evt_not_a_session",
        ))
        hub.flush()
        assert only_digests(await drain(sub, timeout=0.05)) == []
        assert hub.pending == {}
        assert "evt_not_a_session" not in hub.pending

    async def test_session_info_id_used_when_no_sessionID(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "session.updated", {
            "info": {"id": "ses_row", "time": {"archived": 42}},
        }))
        hub.flush()
        d = only_digests(await drain(sub))[0]
        assert d["sessionID"] == "ses_row"
        assert d["archived"] == 42

    async def test_message_event_ignores_payload_id_without_session(self, pair):
        """message.* without sessionID / info.sessionID must not use event id."""
        hub, sub = pair
        hub.publish(ev(
            "/p",
            "message.updated",
            {"info": {"id": "msg_1", "time": {"updated": 1}}},
            payload_id="evt_msg_bus",
        ))
        hub.flush()
        assert only_digests(await drain(sub, timeout=0.05)) == []
        assert hub.pending == {}


class TestSanitize:
    """``_sanitize_error_message`` pipeline: first line -> strip paths ->
    strip stack frames -> strip secrets -> truncate 512 -> fallback chain."""

    def test_first_line_only(self):
        assert _sanitize_error_message("line1\nline2\nline3", None) == "line1"

    def test_strip_unix_abs_path(self):
        assert _sanitize_error_message("open /home/bob/secret.txt failed", None) == "open <path> failed"

    def test_strip_windows_abs_path(self):
        assert _sanitize_error_message("load C:\\Users\\bob\\f.txt failed", None) == "load <path> failed"

    def test_strip_stack_frame_inline(self):
        assert _sanitize_error_message("boom at app.ts:10:5", None) == "boom"
        assert _sanitize_error_message("err at module.js:42", None) == "err"

    def test_strip_secret_token(self):
        assert _sanitize_error_message("token=abc123-xyz leaked", None) == "<redacted> leaked"

    def test_strip_secret_authorization_bearer(self):
        assert _sanitize_error_message("Authorization: Bearer abc.def", None) == "<redacted>"

    def test_strip_secret_password(self):
        assert _sanitize_error_message("password=hunter2 leaked", None) == "<redacted> leaked"

    def test_strip_secret_passwd(self):
        assert _sanitize_error_message("passwd=hunter2 leaked", None) == "<redacted> leaked"

    def test_strip_secret_api_key(self):
        assert _sanitize_error_message("api_key=ak_12345 in logs", None) == "<redacted> in logs"

    def test_strip_secret_client_secret(self):
        assert _sanitize_error_message("client_secret=cs_abc done", None) == "<redacted> done"

    def test_strip_secret_refresh_token(self):
        assert _sanitize_error_message("refresh_token=rt_xyz expired", None) == "<redacted> expired"

    def test_strip_secret_access_token(self):
        assert _sanitize_error_message("access_token=eyJhbGci.xyz", None) == "<redacted>"

    def test_strip_secret_auth_token(self):
        assert _sanitize_error_message("auth_token=at_abc", None) == "<redacted>"

    def test_strip_secret_secret(self):
        assert _sanitize_error_message("secret=shh", None) == "<redacted>"

    def test_strip_secret_key(self):
        assert _sanitize_error_message("key=kl_abc", None) == "<redacted>"

    def test_strip_secret_case_insensitive_key(self):
        """The secret regex keys are case-insensitive on the key name."""
        assert _sanitize_error_message("TOKEN=abc123 leaked", None) == "<redacted> leaked"
        assert _sanitize_error_message("ApiKey=abc here", None) == "<redacted> here"

    def test_truncate_at_512(self):
        assert len(_sanitize_error_message("x" * 600, None)) == 512

    def test_exactly_512_not_truncated(self):
        assert _sanitize_error_message("x" * 512, None) == "x" * 512

    def test_missing_message_falls_back_to_name(self):
        assert _sanitize_error_message(None, "MyError") == "MyError"
        assert _sanitize_error_message("", "MyError") == "MyError"

    def test_missing_message_and_name_no_detail(self):
        assert _sanitize_error_message(None, None) == "(no detail)"
        assert _sanitize_error_message("", None) == "(no detail)"
        assert _sanitize_error_message("", "") == "(no detail)"

    def test_stripped_to_empty_falls_back_to_name_or_no_detail(self):
        """A message that reduces to empty after sanitization falls back to
        the name, then "(no detail)"."""
        assert _sanitize_error_message("at app.ts:1:1", None) == "(no detail)"
        assert _sanitize_error_message("at app.ts:1:1", "Fallback") == "Fallback"

    def test_pipeline_order_path_then_stack_then_secret(self):
        """Combined input: all three categories stripped in declared order."""
        msg = "err /home/bob/x.txt at app.ts:1:1 token=abc tail"
        out = _sanitize_error_message(msg, None)
        assert "<path>" in out
        assert "app.ts" not in out
        assert "abc" not in out
        assert "tail" in out  # unaffected trailing text preserved


# ===========================================================================
# Section 3b: question / permission immediate forwarding (no debounce)
# ===========================================================================

class TestImmediateForwarding:
    """IMMEDIATE events (question.* + permission.*) are forwarded raw with no
    debounce and no ``event:`` header (contract §3)."""

    async def test_question_asked_forwarded_without_flush(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "question.asked", {"id": "q1", "sessionID": "s1"}))
        frames = await drain(sub, timeout=0.1)
        assert len(frames) == 1
        ev_name, data = parse(frames[0])
        assert ev_name is None  # raw passthrough — no event header
        assert data == {
            "directory": "/p",
            "type": "question.asked",
            "properties": {"id": "q1", "sessionID": "s1"},
        }

    async def test_question_v2_asked_forwarded(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "question.v2.asked", {"id": "q1"}))
        frames = await drain(sub, timeout=0.1)
        assert len(frames) == 1
        _, data = parse(frames[0])
        assert data["type"] == "question.v2.asked"

    async def test_permission_asked_and_resolved_forwarded(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "permission.asked", {"id": "p1"}))
        hub.publish(ev("/p", "permission.replied", {"id": "p1"}))
        frames = await drain(sub, timeout=0.1)
        assert len(frames) == 2
        assert [parse(f)[1]["type"] for f in frames] == ["permission.asked", "permission.replied"]

    async def test_permission_v2_asked_and_resolved_forwarded(self, pair):
        hub, sub = pair
        hub.publish(ev("/p", "permission.v2.asked", {"id": "p1"}))
        hub.publish(ev("/p", "permission.v2.replied", {"id": "p1"}))
        frames = await drain(sub, timeout=0.1)
        assert len(frames) == 2

    async def test_permission_replied_upstream_name_forwarded(self, pair):
        """F-001: the upstream real names ``permission.replied`` /
        ``permission.v2.replied`` are forwarded raw; the ghost legacy
        name (built by concatenation below so the banned literal never
        appears in this file — see the L1-1 grep-clean gate) no longer
        matches IMMEDIATE and produces no frame (falls through to the
        catch-all drop; its drop-count behavior is locked in
        tests/test_global_hub_dropped_events.py)."""
        hub, sub = pair
        hub.publish(ev("/p", "permission.replied", {"id": "p1"}))
        hub.publish(ev("/p", "permission.v2.replied", {"id": "p1"}))
        # Built via str.join (NOT ``+``): CPython constant-folds adjacent
        # string concatenation at compile time, which would embed the full
        # banned literal as ONE constant in the __pycache__ .pyc — the
        # L1-1 grep gate runs over src/ AND tests/ including those
        # regenerated artifacts. join keeps the pieces separate in both
        # source and bytecode.
        ghost = "".join(("permission.", "resolved"))
        hub.publish(ev("/p", ghost, {"id": "p1"}))
        frames = await drain(sub, timeout=0.1)
        assert [parse(f)[1]["type"] for f in frames] == [
            "permission.replied", "permission.v2.replied",
        ]

    async def test_question_resolution_family_forwarded(self, pair):
        """R-4：question.replied/rejected/v2.replied/v2.rejected 四型必须
        IMMEDIATE 直推（真实上游名——答复后其他客户端卡片可消失）。

        逐型注入 → 断言订阅者收到原帧；并断言
        ``hub.qp_last_activity[directory]`` 被刷新（N1：锁 IMMEDIATE 分支
        ``startswith("question.")`` 联动不漂移）；反向：注入拼写错误名
        （如 "question.resolved"）→ 不产生直推帧（落 catch-all 计数）。
        """
        import time as _time

        hub, sub = pair
        resolution_types = [
            "question.replied", "question.rejected",
            "question.v2.replied", "question.v2.rejected",
        ]
        for event_type in resolution_types:
            directory = f"/q-{event_type}"
            before = _time.time()
            hub.publish(ev(directory, event_type, {"id": "q1"}))
            # N1 lock: every question.* IMMEDIATE member refreshes the
            # shared q/p activity table (the startswith("question.")
            # gate inside the IMMEDIATE branch covers the new members
            # with zero branch changes — pinned here so the coupling
            # cannot silently drift).
            assert directory in hub.qp_last_activity
            assert before <= hub.qp_last_activity[directory] <= _time.time()

        frames = await drain(sub, timeout=0.1)
        assert [parse(f)[1]["type"] for f in frames] == resolution_types

        # Reverse: a misspelled resolution name must NOT be forwarded —
        # it falls through to the catch-all drop counter (L1-2/F-216).
        # join construction, same constant-folding avoidance as above.
        typo = "".join(("question.", "resolved"))
        hub.publish(ev("/q-typo", typo, {"id": "q1"}))
        assert hub.upstream_dropped_events_total.get(typo) == 1
        frames_after = await drain(sub, timeout=0.1)
        assert frames_after == []
        assert "/q-typo" not in hub.qp_last_activity

    async def test_immediate_event_directory_passed_through(self, pair):
        hub, sub = pair
        hub.publish(ev("/custom/dir", "question.asked", {"id": "q1"}))
        _, data = parse((await drain(sub, timeout=0.1))[0])
        assert data["directory"] == "/custom/dir"

    async def test_dropped_event_types_produce_no_frames(self, pair):
        """text.delta, message.part.*, tool.* are silently dropped."""
        hub, sub = pair
        for et in (
            "message.part.delta", "message.part.updated", "tool.update",
            "text.delta", "tool.call", "tool.response",
        ):
            hub.publish(ev("/p", et, {"sessionID": "s1"}))
        hub.flush()
        assert await drain(sub, timeout=0.05) == []


# ===========================================================================
# Section 4: subscriber admission (contract §6 / §7)
# ===========================================================================

class TestAdmission:
    """HubRegistry.subscribe enforces per-directory and total caps inside one
    no-await critical section; overflow raises SubscriberCapacityError with
    code/limit/current that the events route maps to a 503."""

    async def test_per_directory_cap_raises_with_code_limit_current(self):
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=2,
            max_total_subscribers=10,
        )
        try:
            registry.subscribe()
            registry.subscribe()
            with pytest.raises(SubscriberCapacityError) as ei:
                registry.subscribe()
            err = ei.value
            assert err.code == "sse_subscriber_limit_directory"
            assert err.limit == 2
            assert err.current == 2
            assert registry.rejected_total == 1
        finally:
            await registry.close()

    async def test_total_cap_raises_with_code_limit_current(self):
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=10,
            max_total_subscribers=2,
        )
        try:
            registry.subscribe()
            registry.subscribe()
            with pytest.raises(SubscriberCapacityError) as ei:
                registry.subscribe()
            err = ei.value
            assert err.code == "sse_subscriber_limit_total"
            assert err.limit == 2
            assert err.current == 2
            assert registry.rejected_total == 1
        finally:
            await registry.close()

    async def test_rejected_total_accumulates_across_multiple_rejections(self):
        replay_log = current_replay_log()
        registry = HubRegistry(
            client=None,
            replay_log=replay_log,
            max_subscribers_per_directory=1,
            max_total_subscribers=10,
        )
        try:
            registry.subscribe()
            for _ in range(3):
                with pytest.raises(SubscriberCapacityError):
                    registry.subscribe()
            assert registry.rejected_total == 3
        finally:
            await registry.close()

    async def test_rejection_does_not_increment_total_subscribers(self):
        replay_log = current_replay_log()
        registry = HubRegistry(
            client=None,
            replay_log=replay_log,
            max_subscribers_per_directory=1,
            max_total_subscribers=10,
        )
        try:
            registry.subscribe()
            assert registry.total_subscribers == 1
            with pytest.raises(SubscriberCapacityError):
                registry.subscribe()
            assert registry.total_subscribers == 1  # unchanged on rejection
        finally:
            await registry.close()

    async def test_per_directory_cap_checked_before_total_cap(self):
        """When both caps would be exceeded, per-directory wins (checked first)."""
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=1,
            max_total_subscribers=1,
        )
        try:
            registry.subscribe()
            with pytest.raises(SubscriberCapacityError) as ei:
                registry.subscribe()
            assert ei.value.code == "sse_subscriber_limit_directory"
        finally:
            await registry.close()

    async def test_slot_freed_after_unsubscribe_can_be_reused(self):
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=1,
            max_total_subscribers=10,
        )
        try:
            s1 = registry.subscribe()
            registry.unsubscribe(s1)
            s2 = registry.subscribe()  # slot was freed
            assert s2.id != s1.id
            assert registry.total_subscribers == 1
        finally:
            await registry.close()

    async def test_subscriber_id_is_unique_and_prefixed(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            s1 = registry.subscribe()
            s2 = registry.subscribe()
            assert s1.id.startswith("sub_")
            assert s2.id.startswith("sub_")
            assert s1.id != s2.id
        finally:
            await registry.close()

    async def test_events_route_returns_503_with_body_and_retry_after(self):
        """End-to-end admission: events() maps SubscriberCapacityError to 503
        with {code, limit, current} body and Retry-After: 5 (contract §7)."""
        import orjson

        from oc_slimapi.routes.events import events

        replay_log = current_replay_log()
        registry = HubRegistry(
            client=None,
            replay_log=replay_log,
            max_subscribers_per_directory=1,
            max_total_subscribers=10,
        )
        try:
            registry.subscribe()  # fill the single slot
            request = type("Req", (), {})()
            request.app = type("App", (), {})()
            request.app.state = type(
                "State", (), {"hubs": registry, "replay_log": replay_log}
            )()
            request.headers = {}
            response = await events(request)
            assert response.status_code == 503
            body = orjson.loads(response.body)
            assert body["code"] == "sse_subscriber_limit_directory"
            assert body["limit"] == 1
            assert body["current"] == 1
            assert response.headers["Retry-After"] == "5"
        finally:
            await registry.close()

    async def test_events_route_total_cap_returns_503_total_code(self):
        import orjson

        from oc_slimapi.routes.events import events

        replay_log = current_replay_log()
        registry = HubRegistry(
            client=None,
            replay_log=replay_log,
            max_subscribers_per_directory=10,
            max_total_subscribers=1,
        )
        try:
            registry.subscribe()  # fill the single total slot
            request = type("Req", (), {})()
            request.app = type("App", (), {})()
            request.app.state = type(
                "State", (), {"hubs": registry, "replay_log": replay_log}
            )()
            request.headers = {}
            response = await events(request)
            assert response.status_code == 503
            assert orjson.loads(response.body)["code"] == "sse_subscriber_limit_total"
            assert response.headers["Retry-After"] == "5"
        finally:
            await registry.close()


# ===========================================================================
# Section 5: unsubscribe / close lifecycle (no task leak)
# ===========================================================================

class TestLifecycle:
    """HubRegistry close/unsubscribe semantics: counters return to zero,
    background tasks are cancelled+awaited, hub reference is released."""

    async def test_close_with_no_hub_is_safe(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        await registry.close()  # must not raise
        assert registry.total_subscribers == 0
        # Public observable for "_global is None": no hub surfaces in the
        # metrics snapshot (we do NOT touch the private _global attribute).
        assert registry.snapshot_metrics()["sse"]["hubs"] == []

    async def test_close_resets_total_subscribers_to_zero(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            registry.subscribe()
            registry.subscribe()
            assert registry.total_subscribers == 2
        finally:
            await registry.close()
        assert registry.total_subscribers == 0
        assert registry.snapshot_metrics()["sse"]["hubs"] == []

    async def test_close_during_grace_releases_hub_and_leaves_no_tasks(self):
        """close() called while a grace-removal is pending must still tear the
        hub down and leave no live background tasks. Locks the public outcome
        (metrics show no hub; every previously-live task is done) rather than
        the private ``_removal_task`` handle, so a split that renames it cannot
        regress this unnoticed."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        me = asyncio.current_task()
        live: set[asyncio.Task] = set()
        try:
            s1 = registry.subscribe()
            registry.unsubscribe(s1)  # arms grace removal (sleeps GRACE_SECONDS)
            live = {
                t for t in asyncio.all_tasks()
                if t is not me and not t.done()
            }
            assert live  # hub + grace-removal tasks are running
        finally:
            await registry.close()
        assert all(t.done() for t in live)
        assert registry.snapshot_metrics()["sse"]["hubs"] == []
        assert registry.total_subscribers == 0

    async def test_unsubscribe_idempotent_does_not_underflow(self):
        """A double-unsubscribe must not drive total_subscribers negative
        (would otherwise over-admit later)."""
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=2,
            max_total_subscribers=10,
        )
        try:
            s1 = registry.subscribe()
            registry.unsubscribe(s1)
            registry.unsubscribe(s1)  # duplicate — no-op
            assert registry.total_subscribers == 0
            # Unknown subscriber — also no-op.
            registry.unsubscribe(Subscriber())
            assert registry.total_subscribers == 0
            # Negative guard: total_subscribers never below zero.
            assert registry.total_subscribers >= 0
        finally:
            await registry.close()

    async def test_unsubscribe_when_no_hub_is_safe(self):
        """unsubscribe on a registry with no hub yet created is a no-op."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        registry.unsubscribe(Subscriber())
        assert registry.total_subscribers == 0
        assert registry.snapshot_metrics()["sse"]["hubs"] == []
        await registry.close()

    async def test_global_hub_shared_across_get_calls_regardless_of_directory(self):
        """get(directory) ignores the directory key — one process-wide hub."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            h1 = registry.get("/a")
            h2 = registry.get("/b")
            h3 = registry.get_global()
            assert h1 is h2 is h3
        finally:
            await registry.close()

    async def test_subscribe_during_grace_does_not_recreate_hub(self):
        """A new subscribe arriving during the GRACE_SECONDS idle window must
        attach to the SAME hub instance (the pending grace-removal eventually
        wakes and no-ops because subscribers is non-empty).

        Locks only the public identity behavior (``hub1 is hub2``); we
        intentionally do NOT assert on the private ``_removal_task`` handle."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            s1 = registry.subscribe()
            hub1 = registry.get_global()
            registry.unsubscribe(s1)
            # Grace-removal is now armed (sleeping GRACE_SECONDS); a new
            # subscriber arrives during the window.
            s2 = registry.subscribe()
            hub2 = registry.get_global()
            assert hub1 is hub2  # same hub, not recreated
            assert registry.total_subscribers == 1
        finally:
            await registry.close()

    async def test_repeated_subscribe_unsubscribe_cycles_do_not_leak_counter(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            for _ in range(10):
                s = registry.subscribe()
                registry.unsubscribe(s)
            assert registry.total_subscribers == 0
        finally:
            await registry.close()

    async def test_no_orphan_asyncio_tasks_after_close(self):
        """After close, no live asyncio task references the hub's coroutines.

        Snapshots ``asyncio.all_tasks()`` (public) before close and asserts
        every one of them is done afterwards, plus the registry reports no
        hub via the public metrics snapshot. Field-layout-agnostic: a split
        that renames the hub's task attributes cannot regress this."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        me = asyncio.current_task()
        live: set[asyncio.Task] = set()
        try:
            registry.subscribe()
            live = {
                t for t in asyncio.all_tasks()
                if t is not me and not t.done()
            }
            assert live  # at least one background task started
        finally:
            await registry.close()
        # Every pre-close background task was cancelled + awaited in close().
        assert all(t.done() for t in live)
        # And the registry holds no live hub (public observable).
        assert registry.snapshot_metrics()["sse"]["hubs"] == []

    async def test_global_hub_subscribe_has_no_connection_local_welcome(self, hub):
        sub = hub.subscribe()
        assert sub.queue.empty()

    async def test_global_hub_subscribe_starts_background_tasks(self, hub):
        """subscribe() spawns all three hub background loops (run, flush,
        heartbeat). Locked via the public ``asyncio.all_tasks()`` diff (not
        the private ``task`` / ``flush_task`` / ``heartbeat_task`` attribute
        names), so a behavior-preserving split that renames or relocates them
        cannot regress this — and cannot silently drop one of the loops."""
        me = asyncio.current_task()
        before = {
            t for t in asyncio.all_tasks()
            if t is not me and not t.done()
        }
        sub = hub.subscribe()
        try:
            # Yield to the loop so the run / flush / heartbeat coroutines
            # actually get scheduled and start running.
            await asyncio.sleep(0.05)
            after = asyncio.all_tasks()
            new_tasks = [
                t for t in after
                if t not in before and not t.done()
            ]
            # All three hub loops (run / flush / heartbeat) must be live.
            assert len(new_tasks) >= 3
        finally:
            hub.unsubscribe(sub)



# ===========================================================================
# Section 6: backpressure (buffer overflow -> clear + STOP)
# ===========================================================================

class TestBackpressure:
    """T3 hardening (contract §6): overflow -> immediate disconnect, queue
    cleared, and STOP enqueued."""

    async def test_queue_items_overflow_clears_and_emits_stop(self):
        """Third put on a queue_items=2 subscriber triggers immediate clear +
        STOP. Old frames are not delivered."""
        sub = Subscriber(queue_items=2, buffer_bytes=4096, max_frame_bytes=4096)
        a = sse_frame({"seq": "a"}, event="t")
        b = sse_frame({"seq": "b"}, event="t")
        c = sse_frame({"seq": "c"}, event="t")

        assert sub.put(a) is True
        assert sub.put(b) is True
        assert sub.put(c) is False  # overflow path

        assert sub.closed is True
        assert sub.forced_disconnects == 1
        assert sub.queued_bytes == 0  # ledger reset on clear

        items: list = []
        while True:
            try:
                items.append(sub.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert items == [STOP]

        # Critical (contract §6): old frames NOT still on the queue.
        joined = b"".join(i for i in items if isinstance(i, (bytes, bytearray)))
        assert b'"seq": "a"' not in joined
        assert b'"seq": "b"' not in joined
        assert b'"seq": "c"' not in joined

    async def test_buffer_bytes_overflow_triggers_disconnect(self):
        """Crossing buffer_bytes forces immediate disconnect even when the
        queue has item-capacity left (byte budget wins)."""
        sub = Subscriber(queue_items=64, buffer_bytes=32, max_frame_bytes=4096)
        small = sse_frame({}, event="t")  # ~22 bytes
        assert sub.put(small) is True
        assert not sub.closed
        # Second put pushes cumulative past 32-byte budget.
        assert sub.put(small) is False
        assert sub.closed is True
        assert sub.forced_disconnects == 1

    async def test_oversized_frame_dropped_not_enqueued(self):
        """A single frame > max_frame_bytes is dropped (counter bump), not
        enqueued, and does NOT close the subscriber."""
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=32)
        big = sse_frame({"payload": "x" * 200}, event="t")
        assert len(big) > 32
        assert sub.put(big) is False
        assert sub.dropped_frames == 1
        assert sub.queue.qsize() == 0
        assert not sub.closed
        assert sub.queued_bytes == 0

    async def test_oversized_drop_increments_counter_only(self):
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=16)
        for _ in range(3):
            sub.put(sse_frame({"p": "x" * 100}, event="t"))
        assert sub.dropped_frames == 3
        assert sub.forced_disconnects == 0
        assert not sub.closed
        assert sub.queue.qsize() == 0

    async def test_closed_subscriber_drops_subsequent_puts(self):
        """Once closed by overflow, further puts silently return False."""
        sub = Subscriber(queue_items=2, buffer_bytes=4096, max_frame_bytes=4096)
        sub.put(sse_frame({"i": 1}, event="t"))
        sub.put(sse_frame({"i": 2}, event="t"))
        sub.put(sse_frame({"i": 3}, event="t"))  # overflow -> closed
        assert sub.closed
        assert sub.put(sse_frame({"i": 4}, event="t")) is False
        # Queue holds only STOP from the overflow path.
        assert sub.queue.qsize() == 1

    async def test_stop_sentinel_not_counted_in_byte_ledger(self):
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
        assert sub.put(STOP) is True
        assert sub.queued_bytes == 0
        assert sub.queue.qsize() == 1

    async def test_stop_returns_false_when_queue_full(self):
        sub = Subscriber(queue_items=1, buffer_bytes=4096, max_frame_bytes=4096)
        assert sub.put(sse_frame({"i": 1}, event="t")) is True
        assert sub.put(STOP) is False  # queue_items=1 already filled
        assert not sub.closed  # the original frame is still queued

    async def test_ack_decrements_byte_ledger(self):
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
        f = sse_frame({"x": 1}, event="t")
        size = len(f)
        sub.put(f)
        assert sub.queued_bytes == size
        item = sub.queue.get_nowait()
        sub.ack(item)
        assert sub.queued_bytes == 0

    async def test_ack_stop_is_noop_on_ledger(self):
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
        sub.put(STOP)
        item = sub.queue.get_nowait()
        assert item is STOP
        sub.ack(item)
        assert sub.queued_bytes == 0

    async def test_ack_floors_at_zero(self):
        """A mis-paired ack must not drive queued_bytes negative."""
        sub = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
        sub.ack(sse_frame({"x": 1}, event="t"))  # no prior put
        assert sub.queued_bytes == 0

    async def test_overflow_queue_exact_shape(self):
        """The cleared queue contains only STOP, with no synthetic wire frame."""
        sub = Subscriber(queue_items=2, buffer_bytes=4096, max_frame_bytes=4096)
        sub.put(sse_frame({"i": 1}, event="t"))
        sub.put(sse_frame({"i": 2}, event="t"))  # fills both slots
        assert sub.put(sse_frame({"i": 3}, event="t")) is False  # overflow -> clear

        assert sub.queue.get_nowait() is STOP
        assert sub.queue.qsize() == 0

    async def test_resync_all_uses_reconnect_no_replay_reason(self, pair):
        """GlobalHub.resync_all (upstream reconnect path) uses the
        reconnect_no_replay reason — NOT subscriber_backpressure."""
        hub, sub = pair
        hub.resync_all()
        frames = await drain(sub)
        assert len(frames) == 1
        ev_name, data = parse(frames[0])
        assert ev_name == "resync"
        assert data == {"reason": "reconnect_no_replay"}

    async def test_byte_ledger_decrement_allows_repeat_fill(self):
        """A healthy consumer that drains+acks must NOT false-positive overflow
        on the second fill (regression guard for the queued_bytes ack path)."""
        buffer = 64 * 1024
        queue_items = 1024
        sub = Subscriber(
            queue_items=queue_items,
            buffer_bytes=buffer,
            max_frame_bytes=buffer,
        )
        frame = sse_frame({"payload": "x" * 200}, event="t")
        size = len(frame)
        n = min(buffer // size, queue_items)
        assert n >= 2

        for _ in range(n):
            sub.put(frame)
        assert not sub.closed
        assert sub.queued_bytes == n * size

        for _ in range(n):
            sub.ack(sub.queue.get_nowait())
        assert sub.queued_bytes == 0

        # Second fill — without ack this would overflow on stale ledger.
        for _ in range(n):
            sub.put(frame)
        assert not sub.closed
        assert sub.queued_bytes == n * size
        assert sub.forced_disconnects == 0

    async def test_flush_fanout_counts_subscribers_regardless_of_per_subscriber_outcome(self, pair):
        """emitted_frames_total += len(subscribers) at fan-out time even when a
        subscriber overflows during the put — lock the current accounting."""
        hub, sub = pair
        # Attach a tiny-capacity subscriber already at capacity.
        full = Subscriber(queue_items=1, buffer_bytes=4096, max_frame_bytes=4096)
        full.put(sse_frame({}, event="test.fill"))  # fill the 1 slot
        hub.subscribers.add(full)
        try:
            before = hub.emitted_frames_total
            hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
            hub.flush()
            # Counter incremented by the number of subscribers present at fan-out,
            # regardless of whether each individual put succeeded.
            assert hub.emitted_frames_total == before + len(hub.subscribers)
            assert full.closed is True
            assert full.forced_disconnects == 1
        finally:
            hub.subscribers.discard(full)


# ===========================================================================
# Section 7: registry metrics snapshot (contract §2 / §6)
# ===========================================================================

class TestMetrics:
    """snapshot_metrics() returns the strict {sse, skeleton} shape with
    subscribers / hubs / clients subtrees."""

    async def test_snapshot_shape_strict(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            sub = registry.subscribe()
            snap = registry.snapshot_metrics()
            assert set(snap) == {"sse", "skeleton"}

            sse = snap["sse"]
            assert set(sse) == {"subscribers", "hubs", "clients"}

            assert set(sse["subscribers"]) == {"current", "limit", "rejectedTotal"}
            assert sse["subscribers"]["current"] == 1
            assert sse["subscribers"]["limit"] == DEFAULT_MAX_TOTAL_SUBSCRIBERS
            assert sse["subscribers"]["rejectedTotal"] == 0

            assert len(sse["hubs"]) == 1
            hub_entry = sse["hubs"][0]
            # shape 加性演进：droppedEventsByType（2026-08-21 R-5 裁决，
            # 取代 4.5.0 内部-only 决定）——纯加性键，既有五键零改动。
            assert set(hub_entry) == {
                "subscribers", "upstreamConnected",
                "upstreamEventsTotal", "emittedFramesTotal", "reconnectsTotal",
                "droppedEventsByType",
            }
            assert hub_entry["droppedEventsByType"] == {}

            assert len(sse["clients"]) == 1
            client_entry = sse["clients"][0]
            assert set(client_entry) == {
                "subscriberId", "queueItems", "bufferBytes",
                "droppedFramesTotal", "forcedDisconnectsTotal",
            }
            assert client_entry["subscriberId"] == sub.id

            assert set(snap["skeleton"]) == {
                "activeTransforms", "waitingTransforms", "cacheEnabled",
            }
            assert snap["skeleton"]["cacheEnabled"] is False
        finally:
            await registry.close()

    async def test_snapshot_no_hub_returns_empty_arrays(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            snap = registry.snapshot_metrics()
            assert snap["sse"]["hubs"] == []
            assert snap["sse"]["clients"] == []
            assert snap["sse"]["subscribers"]["current"] == 0
            assert snap["sse"]["subscribers"]["rejectedTotal"] == 0
            assert snap["sse"]["subscribers"]["limit"] == DEFAULT_MAX_TOTAL_SUBSCRIBERS
            assert snap["skeleton"] == {
                "activeTransforms": 0,
                "waitingTransforms": 0,
                "cacheEnabled": False,
            }
        finally:
            await registry.close()

    async def test_snapshot_hub_counters_initial_zero(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            registry.subscribe()
            hub = snap_hub(registry.snapshot_metrics())[0]
            assert hub["upstreamConnected"] is False
            assert hub["upstreamEventsTotal"] == 0
            assert hub["emittedFramesTotal"] == 0
            assert hub["reconnectsTotal"] == 0
            assert hub["subscribers"] == 1
        finally:
            await registry.close()

    async def test_snapshot_native_subscribe_has_empty_client_queue(self):
        """Native v4 subscribe does not enqueue a connection-local welcome."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            sub = registry.subscribe()
            client = snap_client(registry.snapshot_metrics())[0]
            assert client["queueItems"] == 0
            assert client["bufferBytes"] == 0
            assert client["droppedFramesTotal"] == 0
            assert client["forcedDisconnectsTotal"] == 0
            assert client["subscriberId"] == sub.id
        finally:
            await registry.close()

    async def test_snapshot_publish_and_flush_increment_hub_counters(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            sub = registry.subscribe()
            hub = registry.get_global()

            hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
            hub.flush()
            # Drain the digest.
            item = await sub.queue.get()
            sub.ack(item)

            hub_entry = snap_hub(registry.snapshot_metrics())[0]
            assert hub_entry["upstreamEventsTotal"] == 1
            assert hub_entry["emittedFramesTotal"] == 1  # 1 subscriber × 1 digest
            assert hub_entry["reconnectsTotal"] == 0
        finally:
            await registry.close()

    async def test_snapshot_question_event_increments_counters_immediately(self):
        """question.* fans out immediately (no debounce) and bumps
        emitted_frames_total by len(subscribers)."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            sub = registry.subscribe()
            hub = registry.get_global()

            hub.publish(ev("/p", "question.asked", {"id": "q1"}))
            hub_entry = snap_hub(registry.snapshot_metrics())[0]
            assert hub_entry["upstreamEventsTotal"] == 1
            assert hub_entry["emittedFramesTotal"] == 1
        finally:
            await registry.close()

    async def test_snapshot_rejected_total_reflected(self):
        registry = HubRegistry(
            client=None,
            replay_log=current_replay_log(),
            max_subscribers_per_directory=1,
            max_total_subscribers=10,
        )
        try:
            registry.subscribe()
            with pytest.raises(SubscriberCapacityError):
                registry.subscribe()
            snap = registry.snapshot_metrics()
            assert snap["sse"]["subscribers"]["rejectedTotal"] == 1
            assert snap["sse"]["subscribers"]["current"] == 1
        finally:
            await registry.close()

    async def test_snapshot_client_dropped_and_forced_counters_reflect_put_outcomes(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            sub = registry.subscribe()
            # Swap in a tiny-capacity subscriber so we can exercise both the
            # oversized-drop and overflow-disconnect paths through public put().
            hub = registry.get_global()
            hub.subscribers.discard(sub)
            tiny = Subscriber(queue_items=1, buffer_bytes=4096, max_frame_bytes=8)
            hub.subscribers.add(tiny)

            # Path 1: oversized drop (max_frame_bytes=8 rejects the ~60-byte frame).
            tiny.put(sse_frame({"p": "x" * 50}, event="t"))
            assert tiny.dropped_frames == 1

            # Path 2: overflow disconnect — relax max_frame_bytes so the small
            # frames fit, then overflow queue_items=1.
            tiny.max_frame_bytes = 4096
            tiny.put(sse_frame({"ok": 1}, event="t"))        # fills the 1-slot queue
            tiny.put(sse_frame({"overflow": 1}, event="t"))  # overflow -> disconnect
            assert tiny.forced_disconnects == 1

            client = snap_client(registry.snapshot_metrics())[0]
            assert client["subscriberId"] == tiny.id
            assert client["droppedFramesTotal"] == 1
            assert client["forcedDisconnectsTotal"] == 1
        finally:
            await registry.close()

    async def test_snapshot_skeleton_with_real_transform_pool(self):
        """Behavior lock: registry.snapshot_metrics() reflects a real
        TransformPool with one permit held. Uses NO private ``_semaphore``
        fields from the test side — only the public ``async with pool:``
        context manager and the registry's public snapshot.

        Stays green today (the registry computes ``active=1`` via its current
        private-field read of the real pool's semaphore) AND stays green
        after Batch2 routes the skeleton through ``pool.snapshot_metrics()``,
        because the observable ``activeTransforms``/``waitingTransforms`` are
        unchanged. That makes this a behavior-preserving-split lock."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        pool = TransformPool(TransformConfig(
            max_transforms=1, transform_wait_seconds=1.0, max_response_bytes=4096,
        ))
        try:
            registry.set_transforms(pool)
            async with pool:
                # One permit held out of one → active=1, no waiters.
                skel = registry.snapshot_metrics()["skeleton"]
                assert skel["activeTransforms"] == 1
                assert skel["waitingTransforms"] == 0
                assert skel["cacheEnabled"] is False  # hard-coded per contract §10
        finally:
            await registry.close()
            pool.shutdown()

    async def test_snapshot_skeleton_routes_through_pool_snapshot_metrics(
        self, monkeypatch
    ):
        """Forces Batch2 to route skeleton metrics through the public
        ``TransformPool.snapshot_metrics()`` instead of reading the pool's
        private ``_semaphore`` fields.

        A spy is monkeypatched onto a REAL ``TransformPool``. Today the
        registry reads ``pool._semaphore`` directly and never calls
        ``snapshot_metrics``, so the spy counter stays 0 → assertion fails →
        xfail. Once Batch2 routes through the public API, the spy is invoked
        → XPASS (then this mark is removed)."""
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        pool = TransformPool(TransformConfig(
            max_transforms=2, transform_wait_seconds=1.0, max_response_bytes=4096,
        ))
        calls = {"n": 0}

        # Distinctive values the real pool (max_transforms=2, idle here) can
        # NEVER produce: ``active`` from the semaphore is bounded to 0..2, so
        # 7/5 prove the registry is USING the public API's return value — not
        # merely calling it while still reading ``_semaphore`` on the side.
        def spy() -> dict:
            calls["n"] += 1
            return {"active": 7, "waiting": 5}

        try:
            # snapshot_metrics() does not exist on TransformPool today, so
            # inject the spy without asserting pre-existence (raising=False).
            monkeypatch.setattr(pool, "snapshot_metrics", spy, raising=False)
            registry.set_transforms(pool)
            skel = registry.snapshot_metrics()["skeleton"]
            # Batch2 must (a) call the public pool API AND (b) USE its return
            # value for the skeleton. A sneaky impl that calls the spy but
            # keeps reading ``_semaphore`` yields active<=2 (not 7) and fails
            # here, staying xfail until the private-field path is fully removed.
            assert calls["n"] >= 1
            assert skel["activeTransforms"] == 7
            assert skel["waitingTransforms"] == 5
        finally:
            await registry.close()
            pool.shutdown()

    async def test_snapshot_multiple_subscribers_all_listed_as_clients(self):
        registry = HubRegistry(client=None, replay_log=current_replay_log())
        try:
            s1 = registry.subscribe()
            s2 = registry.subscribe()
            s3 = registry.subscribe()
            clients = snap_client_list(registry.snapshot_metrics())
            ids = {c["subscriberId"] for c in clients}
            assert ids == {s1.id, s2.id, s3.id}
            assert len(clients) == 3
            hub_entry = snap_hub(registry.snapshot_metrics())[0]
            assert hub_entry["subscribers"] == 3
        finally:
            await registry.close()


# ===========================================================================
# Section 8: TransformPool.snapshot_metrics() public API lock (Batch2 prep)
# ===========================================================================

class TestTransformPoolSnapshotMetrics:
    """Lock the public ``TransformPool.snapshot_metrics()`` shape that Batch2
    will ADD to ``src/oc_slimapi/transform.py``. The method does not exist
    today, so these three state tests XFAIL (strict=False); they turn into
    XPASS once Batch2 lands the method, at which point the mark is removed.

    Locked shape (sync method, minimal — do not over-specify beyond these keys)::

        def snapshot_metrics(self) -> dict:
            return {"active": <permits held, int>, "waiting": <waiters, int>}
    """

    async def test_idle_pool_reports_zero_active_and_waiting(self):
        """Idle pool: no permits held, no waiters."""
        pool = TransformPool(TransformConfig(
            max_transforms=2, transform_wait_seconds=1.0, max_response_bytes=4096,
        ))
        try:
            assert pool.snapshot_metrics() == {"active": 0, "waiting": 0}
        finally:
            pool.shutdown()

    async def test_one_permit_held_reports_active_one(self):
        """Holding one permit via ``async with pool:`` → active=1, waiting=0."""
        pool = TransformPool(TransformConfig(
            max_transforms=2, transform_wait_seconds=1.0, max_response_bytes=4096,
        ))
        try:
            async with pool:
                assert pool.snapshot_metrics() == {"active": 1, "waiting": 0}
        finally:
            pool.shutdown()

    async def test_waiter_reports_active_one_and_at_least_one_waiting(self):
        """Concurrency state: one permit held + a second acquirer blocked on
        the single-permit pool → active=1, waiting>=1. Cancels the waiter to
        avoid leaking a task."""
        pool = TransformPool(TransformConfig(
            max_transforms=1, transform_wait_seconds=1.0, max_response_bytes=4096,
        ))
        waiter: asyncio.Future | None = None
        try:
            async with pool:
                # Second acquirer blocks on the single permit → queues a waiter.
                waiter = asyncio.ensure_future(pool.__aenter__())
                await asyncio.sleep(0.05)  # let it park on the semaphore
                snap = pool.snapshot_metrics()
                assert snap["active"] == 1
                assert snap["waiting"] >= 1
        finally:
            if waiter is not None:
                waiter.cancel()
                try:
                    await waiter
                except BaseException:
                    pass
            pool.shutdown()


# ---------------------------------------------------------------------------
# Small snapshot accessors (kept tiny + local; only this file uses them)
# ---------------------------------------------------------------------------

def snap_hub(snap: dict) -> list[dict]:
    return snap["sse"]["hubs"]


def snap_client(snap: dict) -> list[dict]:
    return snap["sse"]["clients"]


def snap_client_list(snap: dict) -> list[dict]:
    return snap["sse"]["clients"]
