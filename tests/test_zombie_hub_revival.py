"""BE-002 regression suite: zombie GlobalHub revival.

Two zombie entry points (both fixed centrally in ``GlobalHub.ensure_upstream``):

1. **events entry** — ``HubRegistry._remove_hub_after_grace`` finishes its
   gather, the post-gather re-check sees a consumer arrived → the abandon
   branch used to return without rebuilding any task → subscriber hung on a
   zero-task hub (zero frames, zero heartbeats until client timeout).
2. **token entry** — ``TokenStreamRegistry.subscribe`` calls
   ``cancel_pending_removal()`` FIRST (the removal task dies inside its
   gather → early CancelledError return, the abandon branch never runs),
   THEN ``hub.ensure_upstream()`` — whose old guard
   ``if not self.task or self.task.done()`` no-ops for a task that is
   cancelling-but-not-yet-done → zombie.

The fix: ``ensure_upstream`` arms ``_revive_after_group`` when the run task
is mid-cancel-unwind; the waiter rebuilds the group only after the WHOLE
old group (run + flush + heartbeat) quiesced, guarded by ``_closing``, a
stale-group identity check, and ``has_consumers()``.

All tests are deterministic: unwind timing is pinned with ``asyncio.Event``
gates (never sleep-race sampling).
"""

import asyncio
import contextlib

import pytest

from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import Subscriber
from oc_slimapi.sse.registry import HubRegistry
from oc_slimapi.sse.tokenstream.hub import TokenStreamHub
from oc_slimapi.sse.tokenstream.subscriber import TokenStreamRegistry


# ===========================================================================
# helpers
# ===========================================================================

def _ev(event_type: str, properties: dict | None = None) -> dict:
    """Build one upstream /global/event envelope."""
    return {
        "directory": "/p",
        "payload": {"type": event_type, "properties": properties or {}},
    }


def _make_gated_hub() -> tuple[GlobalHub, dict[str, asyncio.Event]]:
    """Build a GlobalHub whose run/flush/heartbeat park until released.

    Each loop parks on a 3600s sleep; on CancelledError it parks on its
    gate BEFORE re-raising — modelling a slow unwind (httpx connection
    teardown). Releasing a gate completes that task's unwind.
    """
    hub = GlobalHub(client=None)
    gates = {
        "run": asyncio.Event(),
        "flush": asyncio.Event(),
        "heartbeat": asyncio.Event(),
    }

    def gated(name: str):
        async def _gated():
            try:
                await asyncio.sleep(3600.0)
            except asyncio.CancelledError:
                await gates[name].wait()
                raise
        return _gated

    hub.run = gated("run")              # type: ignore[assignment]
    hub.flush_loop = gated("flush")     # type: ignore[assignment]
    hub.heartbeat_loop = gated("heartbeat")  # type: ignore[assignment]
    return hub, gates


async def _pump(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _cancel_trio(hub: GlobalHub) -> None:
    """Reproduce the removal's cancel list (run/flush/heartbeat)."""
    for task in (hub.task, hub.flush_task, hub.heartbeat_task):
        if task is not None and not task.done():
            task.cancel()


def _spawn_spy(hub: GlobalHub) -> list[int]:
    """Count _spawn_group invocations (any caller: revive/waiter/direct)."""
    calls: list[int] = []
    original = hub._spawn_group

    def spy() -> None:
        calls.append(1)
        original()

    hub._spawn_group = spy  # type: ignore[assignment]
    return calls


async def _shutdown_hub(hub: GlobalHub, gates: dict[str, asyncio.Event]) -> None:
    """Terminal cleanup: release gates, barrier, cancel everything, gather."""
    for gate in gates.values():
        gate.set()
    hub._closing = True
    tasks = [
        task for task in (
            hub.task, hub.flush_task, hub.heartbeat_task,
            hub.stop_task, hub._revive_task,
        )
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), 5.0,
            )


async def _shutdown_registry(
    registry: HubRegistry, gates: dict[str, asyncio.Event] | None = None,
) -> None:
    if gates is not None:
        for gate in gates.values():
            gate.set()
    await registry.close()


def _updated_props(sid, mid, pid, *, text=""):
    """Build message.part.updated props for tests."""
    return {
        "part": {
            "sessionID": sid, "messageID": mid, "id": pid,
            "type": "text", "text": text,
            "time": {},
        },
    }


def _delta_props(sid, mid, pid, *, delta="x"):
    """Build message.part.delta props for tests."""
    return {
        "field": "text", "sessionID": sid, "messageID": mid,
        "partID": pid, "delta": delta,
    }


# ===========================================================================
# 1 — revival waits for the ENTIRE old group
# ===========================================================================

class TestRevivalWaitsForFullGroup:
    async def test_new_group_waits_for_all_three(self):
        hub, gates = _make_gated_hub()
        try:
            hub.subscribers.add(Subscriber())
            hub.ensure_upstream()
            await _pump(2)  # park the gated trio at their 3600s sleeps
            old_run = hub.task
            old_flush = hub.flush_task
            old_heartbeat = hub.heartbeat_task
            spawns = _spawn_spy(hub)

            # Simulate the removal's cancel: whole trio mid-unwind.
            await _cancel_trio(hub)
            await _pump(2)  # deliver it; trio parks mid-unwind at the gates
            hub.ensure_upstream()  # arms the revival waiter
            assert hub._revive_task is not None
            assert spawns == []  # nothing spawned while unwinding

            # run finishes its unwind; flush + heartbeat still parked.
            gates["run"].set()
            await _pump()
            assert old_run.done()
            assert not old_flush.done() and not old_heartbeat.done()
            assert spawns == []
            assert hub.task is old_run  # still the old group

            # flush finishes; heartbeat still parked → still no spawn.
            gates["flush"].set()
            await _pump()
            assert old_flush.done()
            assert spawns == []

            # heartbeat finishes → waiter revives exactly once.
            gates["heartbeat"].set()
            await _pump(4)
            assert spawns == [1]
            assert hub.task is not old_run
            assert hub.flush_task is not old_flush
            assert hub.heartbeat_task is not old_heartbeat
            for fresh in (hub.task, hub.flush_task, hub.heartbeat_task):
                assert fresh is not None and not fresh.done()
            assert hub._revive_task is None
        finally:
            await _shutdown_hub(hub, gates)


# ===========================================================================
# 2 — repeated ensure_upstream builds ONE waiter / ONE group
# ===========================================================================

class TestSingleWaiter:
    async def test_repeated_ensure_upstream_single_waiter(self):
        hub, gates = _make_gated_hub()
        try:
            hub.subscribers.add(Subscriber())
            hub.ensure_upstream()
            await _pump(2)  # park the gated trio before cancelling
            old_run = hub.task
            spawns = _spawn_spy(hub)

            await _cancel_trio(hub)
            await _pump(2)  # deliver it; trio parks mid-unwind at the gates
            hub.ensure_upstream()
            waiter = hub._revive_task
            assert waiter is not None
            hub.ensure_upstream()
            hub.ensure_upstream()
            assert hub._revive_task is waiter  # same single waiter

            for gate in gates.values():
                gate.set()
            await _pump(4)
            assert spawns == [1]  # exactly one new group
            assert hub._revive_task is None
            assert hub.task is not old_run and not hub.task.done()
        finally:
            await _shutdown_hub(hub, gates)


# ===========================================================================
# 3 — stale waiter never touches the new group / its grace timer
# ===========================================================================

class TestStaleWaiter:
    async def test_stale_waiter_leaves_new_group_alone(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 999.0)
        hub, gates = _make_gated_hub()
        try:
            hub.ensure_upstream()
            await _pump(2)  # park the gated trio before cancelling
            old_run = hub.task
            spawns = _spawn_spy(hub)

            await _cancel_trio(hub)
            await _pump(2)  # deliver it; trio parks mid-unwind at the gates
            hub.ensure_upstream()  # waiter armed on the old trio

            # While the waiter is pending, a NEW group appears (this is
            # what the supervisor exception path / a direct rebuild does).
            hub._spawn_group()
            assert spawns == [1]
            new_run = hub.task
            assert new_run is not old_run

            # A grace timer the new subscription cycle just established.
            stop_arm = asyncio.create_task(hub.stop_after_grace())
            hub.stop_task = stop_arm

            # Old trio finishes unwinding → waiter wakes → stale → no-op.
            for gate in gates.values():
                gate.set()
            await _pump(4)
            assert spawns == [1]  # no second spawn
            assert hub.task is new_run and not hub.task.done()
            assert hub.flush_task is not None and not hub.flush_task.done()
            assert hub.heartbeat_task is not None
            assert not hub.heartbeat_task.done()
            # Grace timer untouched (not cancelled by the stale waiter).
            assert hub.stop_task is stop_arm and not stop_arm.done()
            assert hub._revive_task is None
        finally:
            await _shutdown_hub(hub, gates)


# ===========================================================================
# 4 — pending revival + HubRegistry.close() → never revives
# ===========================================================================

class TestCloseBarrier:
    async def test_close_with_pending_revival_never_revives(self):
        registry = HubRegistry(client=None)
        # Build the hub through the registry so close() exercises its path.
        registry.subscribe()
        hub = registry.get_global()
        assert hub is not None
        # Swap in the gated group.
        gates = {
            "run": asyncio.Event(),
            "flush": asyncio.Event(),
            "heartbeat": asyncio.Event(),
        }

        def gated(name: str):
            async def _gated():
                try:
                    await asyncio.sleep(3600.0)
                except asyncio.CancelledError:
                    await gates[name].wait()
                    raise
            return _gated

        for task in (hub.task, hub.flush_task, hub.heartbeat_task):
            if task is not None:
                task.cancel()
        await _pump()
        hub.run = gated("run")               # type: ignore[assignment]
        hub.flush_loop = gated("flush")      # type: ignore[assignment]
        hub.heartbeat_loop = gated("heartbeat")  # type: ignore[assignment]
        hub._closing = False
        hub.ensure_upstream()
        await _pump(2)  # park the gated trio before cancelling
        await _cancel_trio(hub)
        await _pump(2)  # deliver it; trio parks mid-unwind at the gates
        hub.ensure_upstream()  # waiter armed mid-unwind
        waiter = hub._revive_task
        assert waiter is not None
        await _pump()  # let the waiter start (gather armed)
        old_run = hub.task
        spawns = _spawn_spy(hub)

        hub.subscribers.add(Subscriber())  # consumer present: would revive

        await registry.close()  # barrier FIRST, then cancel+gather all

        assert hub._closing is True
        assert registry._global is None
        assert waiter.cancelled() or waiter.done()
        assert hub._revive_task is None

        # Release the gates afterwards: nothing may come back to life.
        for gate in gates.values():
            gate.set()
        await asyncio.sleep(0.1)
        await _pump(4)
        assert spawns == []
        assert hub.task is old_run and hub.task.done()
        for task in (hub.flush_task, hub.heartbeat_task):
            assert task is not None and task.done()


# ===========================================================================
# 5 — consumer leaves while waiting → no revival, grace removal still works
# ===========================================================================

class TestConsumerLeavesDuringWait:
    async def test_no_revival_and_grace_removal_succeeds(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.global_hub.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)
        registry = HubRegistry(client=None)
        sub = registry.subscribe()
        hub = registry.get_global()
        assert hub is not None
        gates = {
            "run": asyncio.Event(),
            "flush": asyncio.Event(),
            "heartbeat": asyncio.Event(),
        }

        def gated(name: str):
            async def _gated():
                try:
                    await asyncio.sleep(3600.0)
                except asyncio.CancelledError:
                    await gates[name].wait()
                    raise
            return _gated

        for task in (hub.task, hub.flush_task, hub.heartbeat_task):
            if task is not None:
                task.cancel()
        await _pump()
        hub.run = gated("run")               # type: ignore[assignment]
        hub.flush_loop = gated("flush")      # type: ignore[assignment]
        hub.heartbeat_loop = gated("heartbeat")  # type: ignore[assignment]
        hub._closing = False
        hub.ensure_upstream()
        await _pump(2)  # park the gated trio before cancelling
        old_run = hub.task
        spawns = _spawn_spy(hub)

        # Removal cancels the trio (mid-unwind), then the LAST consumer
        # unsubscribes during the wait.
        await _cancel_trio(hub)
        await _pump(2)  # deliver it; trio parks mid-unwind at the gates
        hub.ensure_upstream()  # waiter armed (consumer still present)
        registry.unsubscribe(sub)  # consumer leaves → grace removal armed

        # The removal's cancel kills the gated unwind (second cancel at
        # the gate) — release the gates so everything can settle.
        for gate in gates.values():
            gate.set()
        removal = registry._removal_task
        assert removal is not None
        await removal
        await _pump(4)

        # Removal succeeded; the waiter did NOT revive the hub.
        assert registry._global is None
        assert hub.task is old_run and hub.task.done()
        assert spawns == []
        assert hub._revive_task is None


# ===========================================================================
# 6 — token attach failure rollback still re-arms grace removal
# ===========================================================================

class TestTokenAttachRollback:
    async def test_rollback_rearms_removal_with_pending_waiter(
        self, monkeypatch,
    ):
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)
        th = TokenStreamHub()
        hubs = HubRegistry(client=None)
        hubs.set_token_hub(th)
        reg = TokenStreamRegistry(
            th, hubs,
            max_subscribers=2, queue_items=64,
            buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
        )
        hub = hubs.get_global()
        assert hub is not None
        gates = {
            "run": asyncio.Event(),
            "flush": asyncio.Event(),
            "heartbeat": asyncio.Event(),
        }

        def gated(name: str):
            async def _gated():
                try:
                    await asyncio.sleep(3600.0)
                except asyncio.CancelledError:
                    await gates[name].wait()
                    raise
            return _gated

        for task in (hub.task, hub.flush_task, hub.heartbeat_task):
            if task is not None:
                task.cancel()
        await _pump()
        hub.run = gated("run")               # type: ignore[assignment]
        hub.flush_loop = gated("flush")      # type: ignore[assignment]
        hub.heartbeat_loop = gated("heartbeat")  # type: ignore[assignment]
        hub._closing = False
        hub.ensure_upstream()
        await _pump(2)  # park the gated trio before cancelling
        old_run = hub.task
        spawns = _spawn_spy(hub)

        # Removal already cancelled the trio (mid-unwind) — the exact
        # state a token subscribe lands in.
        await _cancel_trio(hub)
        await _pump(2)  # deliver it; trio parks mid-unwind at the gates

        def boom(*args, **kwargs):
            raise RuntimeError("attach boom")

        monkeypatch.setattr(th, "attach_subscriber", boom)
        with pytest.raises(RuntimeError):
            reg.subscribe("s1", wire_v4=True)

        # Rollback re-armed grace removal despite the pending waiter.
        removal = hubs._removal_task
        assert removal is not None
        for gate in gates.values():
            gate.set()
        await removal
        await _pump(4)
        assert hubs._global is None
        assert hub.task is old_run and hub.task.done()
        assert spawns == []          # waiter declined (no consumers)
        assert hub._revive_task is None
        assert th._flush_task is None or th._flush_task.done()


# ===========================================================================
# 7a — events entry: subscriber gets REAL frames + heartbeats after revival
# ===========================================================================

class TestEventsEntryRealFrames:
    async def test_subscriber_receives_digest_and_heartbeat(
        self, monkeypatch,
    ):
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)
        monkeypatch.setattr("oc_slimapi.sse.global_hub.HEARTBEAT_SECONDS", 0.05)
        registry = HubRegistry(client=None)
        sub = registry.subscribe()
        hub = registry.get_global()
        assert hub is not None

        # Slow-to-cancel run task (wide gather window), real flush/hb.
        if hub.task is not None:
            hub.task.cancel()

            async def slow_run():
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)  # slow unwind
                    raise

            hub.task = asyncio.create_task(slow_run())
        slow_task = hub.task
        old_flush = hub.flush_task
        old_heartbeat = hub.heartbeat_task

        registry.unsubscribe(sub)  # grace removal armed (GRACE=0)
        await _pump(4)
        # Deterministic precondition: removal cancelled the run task and
        # is parked in its gather (the zombie window).
        assert slow_task.cancelling() and not slow_task.done()

        # New subscriber lands INSIDE the gather window (events entry).
        new_sub = registry.subscribe()
        removal = registry._removal_task
        assert removal is not None
        await removal
        await _pump(4)

        assert registry._global is hub
        assert hub.task is not slow_task and not hub.task.done()
        assert hub.flush_task is not old_flush and not hub.flush_task.done()
        assert (hub.heartbeat_task is not old_heartbeat
                and not hub.heartbeat_task.done())

        # REAL frames through the NEW group: publish a session event and
        # let the new flush_loop debounce (0.25s) deliver a digest frame.
        hub.publish(_ev("session.updated", {"info": {"id": "s1"}}))
        await asyncio.sleep(0.45)

        frames: list = []
        while new_sub.queue is not None and new_sub.queue.qsize():
            frames.append(new_sub.queue.get_nowait())
        assert any(b"session.digest" in frame for frame in frames)
        # REAL heartbeat from the new heartbeat_loop.
        assert any(b"event: server.heartbeat" in frame for frame in frames)

        registry.unsubscribe(new_sub)
        removal = registry._removal_task
        if removal is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await removal
        await _shutdown_registry(registry)


# ===========================================================================
# 7b — token entry: cancel_pending_removal + ensure_upstream revives
# ===========================================================================

class TestTokenEntryRevival:
    async def test_cancel_removal_then_ensure_upstream_revives(
        self, monkeypatch,
    ):
        monkeypatch.setattr("oc_slimapi.sse.registry.GRACE_SECONDS", 0.0)
        th = TokenStreamHub()
        hubs = HubRegistry(client=None)
        hubs.set_token_hub(th)
        reg = TokenStreamRegistry(
            th, hubs,
            max_subscribers=2, queue_items=64,
            buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
        )
        events_sub = hubs.subscribe()
        hub = hubs.get_global()
        assert hub is not None

        # Slow-to-cancel run task; real flush/heartbeat keep running.
        if hub.task is not None:
            hub.task.cancel()

            async def slow_run():
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)  # slow unwind
                    raise

            hub.task = asyncio.create_task(slow_run())
        slow_task = hub.task
        old_flush = hub.flush_task
        old_heartbeat = hub.heartbeat_task

        hubs.unsubscribe(events_sub)  # removal armed (GRACE=0)
        await _pump(4)
        # Removal is parked in its gather with the trio cancelled.
        assert slow_task.cancelling() and not slow_task.done()

        # Token subscribe: cancel_pending_removal() FIRST (kills the
        # removal task inside its gather — the abandon branch never runs),
        # then ensure_upstream() — pre-fix this no-op'd → zombie.
        tok_sub = reg.subscribe("s1", wire_v4=True)
        assert not tok_sub.closed
        assert hubs._removal_task is None  # cancelled + slot cleared
        assert hub._revive_task is not None  # revival waiter armed

        # Let the slow unwind finish → waiter rebuilds the group.
        await asyncio.sleep(0.3)
        await _pump(4)
        assert hub.task is not slow_task and not hub.task.done()
        assert hub.flush_task is not old_flush and not hub.flush_task.done()
        assert (hub.heartbeat_task is not old_heartbeat
                and not hub.heartbeat_task.done())
        assert hub._revive_task is None
        assert hubs._global is hub

        # REAL frames: start a live part, then a delta must reach the
        # token subscriber on the next flush tick (TOKEN_FLUSH_SECONDS).
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="hello"))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="world"))
        deadline = asyncio.get_running_loop().time() + 3.0
        saw_delta = False
        while asyncio.get_running_loop().time() < deadline and not saw_delta:
            try:
                frame = await asyncio.wait_for(tok_sub.queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            if b"message.part.delta" in frame:
                saw_delta = True
        assert saw_delta

        reg.unsubscribe(tok_sub)
        removal = hubs._removal_task
        if removal is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await removal
        await _shutdown_registry(hubs)
