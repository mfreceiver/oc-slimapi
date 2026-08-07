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
