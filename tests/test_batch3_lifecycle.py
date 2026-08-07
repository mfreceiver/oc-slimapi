"""Batch 3 — SSE/Token lifecycle state machine tests.

Each test class maps to a checklist step (§1–§8) from the oracle-approved
design (``.ocmar/workflows/2026-08-08-code-quality-overhaul/batch3-lifecycle-statemachine.md``).

These tests focus on task-lifecycle closure: every task has an owner, every
cancel has a corresponding await, every exception path recovers atomically,
and epoch switches are strictly serial.
"""

from __future__ import annotations

import asyncio

import pytest

from oc_slimapi.config import TOKEN_FLUSH_SECONDS
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import Subscriber
from oc_slimapi.sse.registry import HubRegistry
from oc_slimapi.sse.tokenstream.hub import TokenStreamHub
from oc_slimapi.sse.tokenstream.models import LivePart
from oc_slimapi.sse.tokenstream.subscriber import (
    TokenStreamRegistry,
    TokenSubscriber,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _close_hub(hub: GlobalHub) -> None:
    tasks = [
        t for t in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if t is not None
    ]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    hub.task = None
    hub.flush_task = None
    hub.heartbeat_task = None
    hub.stop_task = None


async def _close_registry(hubs: HubRegistry) -> None:
    """Cancel + await the registry's removal task and its hub tasks."""
    removal = hubs._removal_task
    hubs._removal_task = None
    tasks: list = []
    if removal is not None:
        tasks.append(removal)
    if hubs._global is not None:
        for t in (hubs._global.task, hubs._global.flush_task,
                   hubs._global.heartbeat_task, hubs._global.stop_task):
            if t is not None:
                tasks.append(t)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    hubs._global = None


async def _pump_callbacks(n: int = 3) -> None:
    """Yield to the event loop so done_callbacks fire."""
    for _ in range(n):
        await asyncio.sleep(0)


# ===========================================================================
# Step 1 — INV-1: supervisor + token flush watchdog
# ===========================================================================

class TestInv1Supervisor:
    """GlobalHub run / flush / heartbeat task group supervisor."""

    async def test_flush_death_rebuilds_group_atomically(self):
        """flush_loop dies with an exception → siblings cancelled + group
        rebuilt (new run / flush / heartbeat). No orphan or duplicate."""
        hub = GlobalHub(client=None)
        try:
            sub = Subscriber()
            hub.subscribers.add(sub)  # has_consumers() == True

            calls = {"n": 0}
            real_flush_loop = hub.flush_loop

            async def dying_flush_loop():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("flush boom")
                await real_flush_loop()

            hub.flush_loop = dying_flush_loop  # type: ignore[assignment]
            hub.ensure_upstream()
            old_run = hub.task
            old_flush = hub.flush_task
            old_hb = hub.heartbeat_task
            assert old_flush is not None

            # Let the dying flush run, its callback fire, and rebuild.
            await asyncio.sleep(0.05)
            await _pump_callbacks(3)

            # Group rebuilt: all three slots replaced.
            assert hub.task is not old_run
            assert hub.flush_task is not old_flush
            assert hub.heartbeat_task is not old_hb
            # Old siblings cancelled.
            assert old_run is not None and old_run.cancelled()
            assert old_hb is not None and old_hb.cancelled()
            # New group alive (not done).
            assert hub.task is not None and not hub.task.done()
            assert hub.flush_task is not None and not hub.flush_task.done()
            assert hub.heartbeat_task is not None and not hub.heartbeat_task.done()
            # Exactly one rebuild (no cascading duplicate flush / heartbeat).
            assert calls["n"] == 2
        finally:
            await _close_hub(hub)

    async def test_heartbeat_death_rebuilds_group_atomically(self):
        """heartbeat_loop dies → siblings cancelled + group rebuilt."""
        hub = GlobalHub(client=None)
        try:
            sub = Subscriber()
            hub.subscribers.add(sub)

            calls = {"n": 0}
            real_hb = hub.heartbeat_loop

            async def dying_hb():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("heartbeat boom")
                await real_hb()

            hub.heartbeat_loop = dying_hb  # type: ignore[assignment]
            hub.ensure_upstream()
            old_run = hub.task
            old_flush = hub.flush_task
            old_hb = hub.heartbeat_task

            await asyncio.sleep(0.05)
            await _pump_callbacks(3)

            assert hub.task is not old_run
            assert hub.flush_task is not old_flush
            assert hub.heartbeat_task is not old_hb
            assert old_run is not None and old_run.cancelled()
            assert old_flush is not None and old_flush.cancelled()
            assert calls["n"] == 2
        finally:
            await _close_hub(hub)

    async def test_run_death_rebuilds_group_atomically(self):
        """run dies with an exception → siblings cancelled + group rebuilt."""
        hub = GlobalHub(client=None)
        try:
            sub = Subscriber()
            hub.subscribers.add(sub)

            calls = {"n": 0}
            real_run = hub.run

            async def dying_run():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("run boom")
                await real_run()

            hub.run = dying_run  # type: ignore[assignment]
            hub.ensure_upstream()
            old_run = hub.task
            old_flush = hub.flush_task
            old_hb = hub.heartbeat_task

            await asyncio.sleep(0.05)
            await _pump_callbacks(3)

            assert hub.task is not old_run
            assert hub.flush_task is not old_flush
            assert hub.heartbeat_task is not old_hb
            assert old_flush is not None and old_flush.cancelled()
            assert old_hb is not None and old_hb.cancelled()
            assert calls["n"] == 2
        finally:
            await _close_hub(hub)

    async def test_member_death_no_rebuild_without_consumers(self):
        """A member dies but has_consumers() is False → siblings cancelled,
        NO rebuild (nothing to serve)."""
        hub = GlobalHub(client=None)
        try:
            # No subscribers → has_consumers() == False.
            calls = {"n": 0}
            real_flush_loop = hub.flush_loop

            async def dying_flush_loop():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("flush boom")
                await real_flush_loop()

            hub.flush_loop = dying_flush_loop  # type: ignore[assignment]
            hub.ensure_upstream()
            old_run = hub.task
            old_flush = hub.flush_task

            await asyncio.sleep(0.05)
            await _pump_callbacks(3)

            # No rebuild: run exited normally (no consumers) before the
            # flush callback could rebuild. Either way, no NEW group.
            assert calls["n"] == 1  # flush died once, never rebuilt
            # The old run should have exited (has_consumers False) or been
            # cancelled by the flush callback.
            assert old_run is not None and old_run.done()
            assert old_flush is not None and old_flush.done()
        finally:
            await _close_hub(hub)

    async def test_run_normal_exit_cancels_flush_and_heartbeat(self):
        """run() exits normally (has_consumers False) → its done_callback
        cancels flush + heartbeat immediately (small pre-grace leak fix)."""
        hub = GlobalHub(client=None)
        try:
            # ensure_upstream starts the group; run() will see
            # has_consumers()==False and exit immediately on the first tick.
            hub.ensure_upstream()
            flush_ref = hub.flush_task
            hb_ref = hub.heartbeat_task
            assert flush_ref is not None
            assert hb_ref is not None

            # Let run() check has_consumers() (False) → exit → callback.
            await asyncio.sleep(0.05)
            await _pump_callbacks(3)

            # run exited normally.
            assert hub.task is not None and hub.task.done()
            assert not hub.task.cancelled()  # normal exit, not cancelled
            # flush + heartbeat cancelled by run's done_callback.
            assert flush_ref.cancelled()
            assert hb_ref.cancelled()
        finally:
            await _close_hub(hub)

    async def test_stale_group_callback_is_noop(self):
        """A done_callback for an OLD group must not touch the NEW group.

        Scenario: flush dies → callback cancels siblings + rebuilds. The
        old siblings' own done_callbacks must see the stale-group guard
        and return without cascading another rebuild."""
        hub = GlobalHub(client=None)
        try:
            sub = Subscriber()
            hub.subscribers.add(sub)

            calls = {"n": 0}
            real_flush_loop = hub.flush_loop

            async def dying_flush_loop():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("flush boom")
                await real_flush_loop()

            hub.flush_loop = dying_flush_loop  # type: ignore[assignment]
            hub.ensure_upstream()

            await asyncio.sleep(0.05)
            await _pump_callbacks(5)

            # Exactly ONE rebuild (flush created twice). If stale callbacks
            # had cascaded, calls["n"] would be > 2.
            assert calls["n"] == 2
        finally:
            await _close_hub(hub)


class TestInv1TokenFlushWatchdog:
    """TokenStreamHub _flush_task watchdog."""

    async def test_flush_death_rebuilds_when_subscribers_present(self):
        """flush_loop dies (flush raises) while subscriber_count > 0 →
        watchdog logs CRITICAL and rebuilds _flush_task."""
        th = TokenStreamHub()
        try:
            # Fake a subscriber so subscriber_count > 0.
            class _FakeSub:
                def put(self, frame):
                    pass
            th._subs_by_sid.setdefault("s1", set()).add(_FakeSub())
            assert th.subscriber_count > 0

            calls = {"n": 0}
            real_flush = th.flush

            def dying_flush():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("flush boom")
                real_flush()

            th.flush = dying_flush  # type: ignore[assignment]
            th.start()
            old_task = th._flush_task
            assert old_task is not None

            # flush_loop sleeps TOKEN_FLUSH_SECONDS before calling flush().
            # After death + rebuild, the new loop sleeps another
            # TOKEN_FLUSH_SECONDS before its first flush().
            await asyncio.sleep(2 * TOKEN_FLUSH_SECONDS + 0.05)
            await _pump_callbacks(3)

            # Watchdog rebuilt.
            assert th._flush_task is not None
            assert th._flush_task is not old_task
            assert not th._flush_task.done()
            assert calls["n"] >= 2  # died once, then rebuilt + ran at least once
        finally:
            th.stop()
            if th._flush_task is not None:
                with __import__("contextlib").suppress(Exception):
                    await th._flush_task

    async def test_flush_death_no_rebuild_without_subscribers(self):
        """flush_loop dies but subscriber_count == 0 → no rebuild (the next
        first-attach start() will create a fresh task)."""
        th = TokenStreamHub()
        try:
            assert th.subscriber_count == 0

            calls = {"n": 0}
            real_flush = th.flush

            def dying_flush():
                calls["n"] += 1
                raise RuntimeError("flush boom")

            th.flush = dying_flush  # type: ignore[assignment]
            th.start()
            old_task = th._flush_task

            await asyncio.sleep(TOKEN_FLUSH_SECONDS + 0.05)
            await _pump_callbacks(3)

            # No rebuild.
            assert th._flush_task is old_task or th._flush_task is None
            assert old_task is not None and old_task.done()
        finally:
            th.stop()

    async def test_flush_death_then_new_start_after_rebuild(self):
        """After watchdog rebuilds, the new _flush_task survives and flushes
        correctly (no infinite death loop)."""
        th = TokenStreamHub()
        try:
            class _FakeSub:
                def put(self, frame):
                    pass
            th._subs_by_sid.setdefault("s1", set()).add(_FakeSub())

            calls = {"n": 0}
            real_flush = th.flush

            def dying_flush():
                calls["n"] += 1
                if calls["n"] <= 1:
                    raise RuntimeError("flush boom")
                real_flush()

            th.flush = dying_flush  # type: ignore[assignment]
            th.start()

            await asyncio.sleep(3 * TOKEN_FLUSH_SECONDS + 0.05)
            await _pump_callbacks(3)

            # Rebuilt once, then stable.
            assert calls["n"] >= 2
            assert th._flush_task is not None
            assert not th._flush_task.done()
        finally:
            th.stop()


# ===========================================================================
# Step 2 — INV-2: grace serial + epoch cleanup
# ===========================================================================

class TestInv2GraceSerialEpochCleanup:
    """registry._remove_hub_after_grace: await gather + re-check +
    token_hub.on_upstream_reconnect (preserving _part_revisions /
    _removed_messages)."""

    async def test_grace_removal_clears_token_hub_old_epoch_state(self, monkeypatch):
        """grace removal fires → token_hub.on_upstream_reconnect() called →
        live_parts / _session_status / _busy_sids / _retired_messages
        cleared; _global nulled."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        th = TokenStreamHub()
        # Populate old-epoch state.
        th._session_status["s1"] = "busy"
        th._busy_sids.add("s1")
        th.live_parts[("s1", "m1", "p1")] = LivePart()
        th._retired_messages.add(("s1", "m1"))

        registry = HubRegistry(client=None)
        registry.set_token_hub(th)

        sub = registry.subscribe()
        hub = registry.get_global()
        registry.unsubscribe(sub)
        removal = registry._removal_task
        assert removal is not None
        await removal  # grace fires → teardown
        await _pump_callbacks(3)

        # _global nulled.
        assert registry._global is None
        # Token hub old-epoch state cleared by on_upstream_reconnect.
        assert len(th._session_status) == 0
        assert len(th._busy_sids) == 0
        assert len(th.live_parts) == 0
        assert len(th._retired_messages) == 0

    async def test_part_revisions_preserved_across_epoch_cleanup(self, monkeypatch):
        """CRITICAL 1: _part_revisions survives on_upstream_reconnect
        (ocdroid strict-`>` watermark invariant). Also _removed_messages
        (replay queue) survives."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        th = TokenStreamHub()
        th._part_revisions[("s1", "m1", "p1")] = 42
        th._removed_messages[("s1", "m1")] = 99999

        registry = HubRegistry(client=None)
        registry.set_token_hub(th)

        sub = registry.subscribe()
        registry.unsubscribe(sub)
        removal = registry._removal_task
        assert removal is not None
        await removal
        await _pump_callbacks(3)

        # CRITICAL 1: _part_revisions preserved.
        assert ("s1", "m1", "p1") in th._part_revisions
        assert th._part_revisions[("s1", "m1", "p1")] == 42
        # Replay queue preserved.
        assert ("s1", "m1") in th._removed_messages

    async def test_grace_removal_awaits_hub_tasks(self, monkeypatch):
        """INV-2: after cancelling hub tasks, the removal awaits their full
        exit (gather). Verified by asserting the old run task is .done()
        after removal completes (not still winding down)."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        registry = HubRegistry(client=None)
        sub = registry.subscribe()
        hub = registry.get_global()
        run_ref = hub.task
        flush_ref = hub.flush_task
        hb_ref = hub.heartbeat_task
        registry.unsubscribe(sub)
        removal = registry._removal_task
        assert removal is not None
        await removal

        # All old hub tasks fully done (not just cancelled — gathered).
        assert run_ref is not None and run_ref.done()
        assert flush_ref is not None and flush_ref.done()
        assert hb_ref is not None and hb_ref.done()

    async def test_recheck_aborts_removal_if_consumer_arrives(self, monkeypatch):
        """INV-2 re-check: if a subscriber arrives during the gather (reviving
        the hub), removal is abandoned (_global stays, _removal_task cleared).

        We simulate this by making the hub's run task slow to cancel (it
        parks on a long sleep), so the gather window is wide enough for a
        new subscribe to land."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        registry = HubRegistry(client=None)
        sub = registry.subscribe()
        hub = registry.get_global()
        # Replace run with a slow-to-cancel task so the gather has a wide
        # window. The supervisor done_callback is bypassed (cancelled path).
        if hub.task is not None:
            hub.task.cancel()

            async def slow_run():
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    # Simulate slow unwind (httpx connection teardown).
                    await asyncio.sleep(0.05)
                    raise

            hub.task = asyncio.create_task(slow_run())
        registry.unsubscribe(sub)
        removal = registry._removal_task
        assert removal is not None

        # Schedule a new subscribe to land during the gather. The removal
        # task cancels hub.task, then enters gather. We yield once so the
        # removal starts its gather, then subscribe (which revives the hub
        # via ensure_upstream → _spawn_group).
        await asyncio.sleep(0.02)  # let removal enter gather
        # A new subscriber arrives → revive hub.
        new_sub = registry.subscribe()
        try:
            await removal  # removal completes (re-check aborts)
            await _pump_callbacks(3)
            # Hub was revived: _global still points to the hub.
            assert registry._global is hub
            assert hub.has_consumers()
        finally:
            registry.unsubscribe(new_sub)
            await _close_hub(hub)

    async def test_cancel_during_gather_returns_cleanly(self, monkeypatch):
        """If cancel_pending_removal fires during the gather (e.g. token
        subscribe), the removal task returns cleanly without nulling."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        registry = HubRegistry(client=None)
        sub = registry.subscribe()
        hub = registry.get_global()
        # Slow-to-cancel run task.
        if hub.task is not None:
            hub.task.cancel()

            async def slow_run():
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)
                    raise

            hub.task = asyncio.create_task(slow_run())
        registry.unsubscribe(sub)
        removal = registry._removal_task
        assert removal is not None

        await asyncio.sleep(0.02)  # let removal enter gather
        registry.cancel_pending_removal()  # cancels removal during gather

        with __import__("contextlib").suppress(asyncio.CancelledError):
            await removal
        await _pump_callbacks(3)
        # _removal_task cleared by cancel_pending_removal.
        assert registry._removal_task is None
        # _global NOT nulled (removal was cancelled mid-gather).
        assert registry._global is hub
        await _close_hub(hub)


# ===========================================================================
# Step 3 — INV-3: subscribe full-exception rollback
# ===========================================================================

class TestInv3SubscribeRollback:
    """TokenStreamRegistry.subscribe: ANY exception in ensure_upstream /
    start / attach triggers symmetric rollback."""

    def _build_registry(self) -> tuple:
        th = TokenStreamHub()
        hubs = HubRegistry(client=None)
        hubs.set_token_hub(th)
        reg = TokenStreamRegistry(
            th, hubs,
            max_subscribers=2,
            queue_items=64,
            buffer_bytes=512 * 1024,
            max_frame_bytes=1024 * 1024,
        )
        return reg, th, hubs

    async def test_attach_exception_rolls_back_flush_loop(self, monkeypatch):
        """attach_subscriber raises a non-closed exception (e.g. QueueFull) →
        flush loop stopped, grace re-armed, total_subscribers not incremented,
        exception propagated."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)
        reg, th, hubs = self._build_registry()
        try:
            def raising_attach(sid, sub):
                raise RuntimeError("attach boom")

            th.attach_subscriber = raising_attach  # type: ignore[assignment]

            with pytest.raises(RuntimeError, match="attach boom"):
                reg.subscribe("s1")

            # total_subscribers NOT incremented.
            assert reg.total_subscribers == 0
            # Flush loop STOPPED (rollback stopped it — no ghost task).
            assert th._flush_task is None or th._flush_task.done()
            # No ghost subscriber in _subs_by_sid.
            assert len(th._subs_by_sid) == 0
            # Grace re-armed (rollback called maybe_arm_grace_if_idle).
            # With GRACE_SECONDS=0.0, the removal task fires immediately.
            assert hubs._removal_task is not None or hubs._global is None
        finally:
            th.stop()
            if hubs._global is not None:
                await _close_registry(hubs)

    async def test_attach_exception_does_not_increment_rejected_for_capacity(self):
        """The rejected_total is bumped on rollback (the sub was rejected),
        but the code is NOT a capacity error — the exception propagates as-is."""
        reg, th, hubs = self._build_registry()
        try:
            def raising_attach(sid, sub):
                raise RuntimeError("attach boom")

            th.attach_subscriber = raising_attach  # type: ignore[assignment]

            with pytest.raises(RuntimeError):
                reg.subscribe("s1")

            assert reg.rejected_total == 1
            assert reg.total_subscribers == 0
        finally:
            th.stop()
            if hubs._global is not None:
                await _close_registry(hubs)

    async def test_cancelled_error_reraised_not_swallowed(self):
        """CancelledError is re-raised without being caught by the broad
        except Exception."""
        reg, th, hubs = self._build_registry()
        try:
            def cancelling_attach(sid, sub):
                raise asyncio.CancelledError()

            th.attach_subscriber = cancelling_attach  # type: ignore[assignment]

            with pytest.raises(asyncio.CancelledError):
                reg.subscribe("s1")

            assert reg.total_subscribers == 0
        finally:
            th.stop()
            if hubs._global is not None:
                await _close_registry(hubs)

    async def test_successful_subscribe_still_works(self):
        """Sanity: the refactor doesn't break the happy path."""
        reg, th, hubs = self._build_registry()
        try:
            sub = reg.subscribe("s1")
            assert reg.total_subscribers == 1
            assert th._flush_task is not None and not th._flush_task.done()
            assert th.has_subscriber("s1", sub)
            reg.unsubscribe(sub)
            assert reg.total_subscribers == 0
        finally:
            th.stop()
            if hubs._global is not None:
                await _close_registry(hubs)

    async def test_attach_exception_then_successful_subscribe(self):
        """After a rolled-back attach failure, a subsequent subscribe
        succeeds (no stale state from the failure)."""
        reg, th, hubs = self._build_registry()
        try:
            calls = {"n": 0}
            real_attach = th.attach_subscriber

            def fail_once(sid, sub):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("first attach boom")
                real_attach(sid, sub)

            th.attach_subscriber = fail_once  # type: ignore[assignment]

            with pytest.raises(RuntimeError):
                reg.subscribe("s1")

            # Second subscribe succeeds.
            sub = reg.subscribe("s1")
            assert reg.total_subscribers == 1
            assert th.has_subscriber("s1", sub)
            reg.unsubscribe(sub)
        finally:
            th.stop()
            if hubs._global is not None:
                await _close_registry(hubs)


# ===========================================================================
# Step 4 — INV-4: session.deleted server-side termination
# ===========================================================================

class _FakeSub:
    """Minimal subscriber stub for hub-level INV-4 tests."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.frames: list = []
        self._in_handshake = False
        self.closed = False

    def begin_handshake(self) -> None:
        self._in_handshake = True

    def end_handshake(self) -> None:
        self._in_handshake = False

    def put(self, frame) -> bool:
        self.frames.append(frame)
        return True

    def terminate(self, reason: str) -> None:
        from oc_slimapi.sse.tokenstream.frames import STOP, _resync_frame
        self.closed = True
        self.frames.append(_resync_frame(self.session_id, reason))
        self.frames.append(STOP)


class TestInv4SessionDeletedTermination:
    """on_session_deleted: directly terminate subscribers via
    TokenSubscriber.terminate (resync{session_deleted} → STOP)."""

    def test_subscriber_receives_resync_then_stop_in_order(self):
        """A subscriber for the deleted sid receives resync{session_deleted}
        followed by STOP — strict order, delivered synchronously (not via
        flush loop)."""
        from oc_slimapi.sse.tokenstream.frames import STOP
        th = TokenStreamHub()
        sub = _FakeSub(session_id="s1")
        th.attach_subscriber("s1", sub)
        sub.frames.clear()
        th.on_session_deleted("s1")
        # resync frame delivered.
        resync_frames = [
            f for f in sub.frames
            if isinstance(f, bytes) and b"resync" in f
        ]
        assert len(resync_frames) == 1
        # STOP delivered.
        assert STOP in sub.frames
        # Strict order: resync BEFORE STOP.
        resync_idx = sub.frames.index(resync_frames[0])
        stop_idx = sub.frames.index(STOP)
        assert resync_idx < stop_idx
        # Sub closed.
        assert sub.closed is True

    def test_no_enqueue_session_resync(self):
        """INV-4: on_session_deleted does NOT call _enqueue_session_resync
        (the old deferred flush-loop path is removed)."""
        th = TokenStreamHub()
        th.on_session_deleted("s1")
        assert th._pending_session_resinks == []

    def test_hub_does_not_detach_subscriber(self):
        """INV-4: on_session_deleted terminates the subscriber but does NOT
        detach it from _subs_by_sid — the generator's finally → unsubscribe
        relies on has_subscriber() == True to run the normal cleanup."""
        th = TokenStreamHub()
        sub = _FakeSub(session_id="s1")
        th.attach_subscriber("s1", sub)
        th.on_session_deleted("s1")
        # Sub still in fanout (not detached by on_session_deleted).
        assert th.has_subscriber("s1", sub)

    def test_other_sid_subscribers_not_terminated(self):
        """Only subscribers for the deleted sid are terminated; subscribers
        for other sids are untouched."""
        from oc_slimapi.sse.tokenstream.frames import STOP
        th = TokenStreamHub()
        sub1 = _FakeSub(session_id="s1")
        sub2 = _FakeSub(session_id="s2")
        th.attach_subscriber("s1", sub1)
        th.attach_subscriber("s2", sub2)
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_session_deleted("s1")
        # s1 terminated.
        assert sub1.closed is True
        assert STOP in sub1.frames
        # s2 NOT terminated.
        assert sub2.closed is False
        assert STOP not in sub2.frames

    def test_live_parts_cleared_for_deleted_sid(self):
        """Existing cleanup is preserved: _retire_session clears live_parts."""
        th = TokenStreamHub()
        th._subs_by_sid.setdefault("s1", set()).add(_FakeSub(session_id="s1"))
        th.live_parts[("s1", "m1", "p1")] = LivePart()
        th.on_session_deleted("s1")
        assert ("s1", "m1", "p1") not in th.live_parts

    def test_multiple_subscribers_all_terminated(self):
        """Multiple subscribers for the same deleted sid are all terminated."""
        from oc_slimapi.sse.tokenstream.frames import STOP
        th = TokenStreamHub()
        sub_a = _FakeSub(session_id="s1")
        sub_b = _FakeSub(session_id="s1")
        th.attach_subscriber("s1", sub_a)
        th.attach_subscriber("s1", sub_b)
        sub_a.frames.clear()
        sub_b.frames.clear()
        th.on_session_deleted("s1")
        assert sub_a.closed is True
        assert STOP in sub_a.frames
        assert sub_b.closed is True
        assert STOP in sub_b.frames

    async def test_generator_finally_unsubscribes_after_stop(self, monkeypatch):
        """Integration: after on_session_deleted, the route generator
        receives STOP → breaks → finally → unsubscribe (normal path with
        has_subscriber True → detach + decrement + stop flush + grace arm)."""
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)

        th = TokenStreamHub()
        hubs = HubRegistry(client=None)
        hubs.set_token_hub(th)
        reg = TokenStreamRegistry(
            th, hubs,
            max_subscribers=2,
            queue_items=64,
            buffer_bytes=512 * 1024,
            max_frame_bytes=1024 * 1024,
        )
        try:
            sub = reg.subscribe("s1")
            assert reg.total_subscribers == 1
            assert th.has_subscriber("s1", sub)

            # Simulate session.deleted arriving from upstream.
            th.on_session_deleted("s1")

            # Generator finally: unsubscribe (normal path — sub still in fanout).
            reg.unsubscribe(sub)
            assert reg.total_subscribers == 0
            # Sub detached (membership guard passed because sub was still in fanout).
            assert not th.has_subscriber("s1", sub)
        finally:
            th.stop()
            await _close_registry(hubs)


# ===========================================================================
# Step 5 — P1-22: deleted-sid gate
# ===========================================================================

def _updated_props(sid, mid, pid, *, text="", ptype="text"):
    """Build message.part.updated props for tests."""
    return {
        "part": {
            "sessionID": sid, "messageID": mid, "id": pid,
            "type": ptype, "text": text,
            "time": {},
        },
    }


def _delta_props(sid, mid, pid, *, delta="x"):
    """Build message.part.delta props for tests."""
    return {
        "field": "text", "sessionID": sid, "messageID": mid,
        "partID": pid, "delta": delta,
    }


class TestP1_22DeletedSidGate:
    """Late part events for a deleted session are dropped (no LivePart
    resurrection)."""

    def test_deleted_sid_blocks_part_updated(self):
        """After on_session_deleted, a late message.part.updated for the same
        sid does NOT create a LivePart."""
        th = TokenStreamHub()
        th.on_session_deleted("s1")
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="hello"))
        assert ("s1", "m1", "p1") not in th.live_parts

    def test_deleted_sid_blocks_part_delta(self):
        """After on_session_deleted, a late message.part.delta is dropped."""
        th = TokenStreamHub()
        # Create a LivePart first, then delete the session.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        assert ("s1", "m1", "p1") in th.live_parts
        th.on_session_deleted("s1")
        # LivePart cleared by retire_session.
        assert ("s1", "m1", "p1") not in th.live_parts
        # Late delta does NOT recreate the LivePart.
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="late"))
        assert ("s1", "m1", "p1") not in th.live_parts

    def test_deleted_sid_blocks_part_removed(self):
        """After on_session_deleted, a late message.part.removed is a no-op."""
        th = TokenStreamHub()
        th.on_session_deleted("s1")
        # Should not crash or create state.
        th.on_part_removed("s1", "m1", "p1")
        assert ("s1", "m1", "p1") not in th.live_parts

    def test_other_sid_not_blocked(self):
        """Part events for a DIFFERENT sid are not blocked by a deleted sid."""
        th = TokenStreamHub()
        th.on_session_deleted("s1")
        th.on_part_updated(_updated_props("s2", "m2", "p2", text="hello"))
        assert ("s2", "m2", "p2") in th.live_parts

    def test_gate_cleared_on_reconnect(self):
        """on_upstream_reconnect clears the deleted-sid gate (new epoch)."""
        th = TokenStreamHub()
        th.on_session_deleted("s1")
        assert "s1" in th._deleted_sids
        th.on_upstream_reconnect()
        assert len(th._deleted_sids) == 0
        # After reconnect, part events for s1 are accepted again.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="hello"))
        assert ("s1", "m1", "p1") in th.live_parts

    def test_gate_is_bounded(self):
        """The deleted-sid gate has a FIFO cap (TOKEN_REMOVED_MESSAGES_MAX)."""
        from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX
        th = TokenStreamHub()
        for i in range(TOKEN_REMOVED_MESSAGES_MAX + 50):
            th.on_session_deleted(f"sid_{i}")
        # Cap enforced.
        assert len(th._deleted_sids) <= TOKEN_REMOVED_MESSAGES_MAX
        # Oldest evicted.
        assert "sid_0" not in th._deleted_sids
        # Newest kept.
        assert f"sid_{TOKEN_REMOVED_MESSAGES_MAX + 49}" in th._deleted_sids


# ===========================================================================
# Step 6 — INV-5: config frame ceiling
# ===========================================================================

class TestInv5ConfigFrameCeiling:
    """TokenStreamHub.max_frame_bytes sourced from Settings (not hardcoded)."""

    def test_hub_uses_configured_max_frame_bytes(self):
        """A hub constructed with max_frame_bytes=512KiB truncates a 700KiB
        snapshot instead of silently accepting it (the default 1MiB would
        accept it)."""
        config_bytes = 512 * 1024
        th = TokenStreamHub(max_frame_bytes=config_bytes)
        assert th._max_frame_bytes == config_bytes

        # Create a LivePart with large seed BEFORE attaching (so the
        # handshake snapshot picks it up).
        big_text = "x" * (700 * 1024)
        th.on_part_updated({
            "part": {
                "sessionID": "s1", "messageID": "m1", "id": "p1",
                "type": "text", "text": big_text,
                "time": {},
            },
        })
        assert ("s1", "m1", "p1") in th.live_parts

        # Attach subscriber — handshake snapshot exceeds 512KiB frame cap.
        sub = _FakeSub(session_id="s1")
        th.attach_subscriber("s1", sub)

        truncated_frames = [
            f for f in sub.frames
            if isinstance(f, bytes) and b"truncated" in f
        ]
        assert len(truncated_frames) > 0, (
            "INV-5: a snapshot exceeding the configured max_frame_bytes "
            "must be truncated, not silently accepted"
        )

    def test_default_hub_uses_1mib(self):
        """Without explicit config, the hub uses the 1MiB default."""
        from oc_slimapi.config import DEFAULT_TOKEN_MAX_FRAME_BYTES
        th = TokenStreamHub()
        assert th._max_frame_bytes == DEFAULT_TOKEN_MAX_FRAME_BYTES
