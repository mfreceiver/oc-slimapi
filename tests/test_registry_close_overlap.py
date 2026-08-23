"""BUG-001 regression: registry teardown must not re-cancel an unwinding task.

Invariant (repair-plan.md Batch R1): once a hub child task has begun
unwinding (``task.cancelling() > 0``), no second ``CancelledError`` may be
delivered to it — neither by a direct ``task.cancel()`` from a registry
teardown path nor by cancelling a pending grace-removal task whose
``gather()`` owns the unwind. Cleanup (upstream stream ``__aexit__``) must
be able to complete, and ``close()`` must not block ~30 s waiting out an
armed grace timer.

Scenarios (event-gated, mirroring the orchestrator probe
``/tmp/opencode/ss1_final.py`` and state-sequence.md SS-1):

1. *overlap* — grace removal parked in its gather while the hub run task
   is mid stream-exit cleanup; ``registry.close()`` overlaps. The removal
   task must be awaited (not cancelled): cleanup completes with exactly
   ONE cancel delivered, and close() returns only after the cleanup.
2. *duplicate-cancel* — the hub run task is already cancelling (external
   canceller, e.g. the supervisor path); ``registry.close()`` must skip
   it in its cancel pass instead of delivering a second CancelledError.
3. *control* — grace removal as the sole canceller: cleanup completes,
   exactly one cancel, ``_global`` dropped.
"""

from __future__ import annotations

import asyncio

from oc_slimapi.sse import registry as registry_module
from oc_slimapi.sse.registry import HubRegistry


class StubStreamHub:
    """Probe-shaped stub hub parked in stream-exit cleanup.

    ``fake_run`` models ``GlobalHub.run()``'s upstream-stream unwind: on
    the FIRST CancelledError it marks the stream exit reached and parks in
    cleanup (the gate). A SECOND CancelledError while parked truncates the
    cleanup (``cleanup_done`` stays False) — exactly how a re-cancelled
    httpx ``__aexit__`` behaves.
    """

    def __init__(self) -> None:
        self.cancel_count = 0
        self.cleanup_done = False
        self.reached_stream_exit = asyncio.Event()
        self.stream_exit_gate = asyncio.Event()
        self._closing = False
        self._revive_task: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self.flush_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.stop_task: asyncio.Task | None = None

    def has_consumers(self) -> bool:
        return False

    def notify_idle_recycle_loss(self) -> None:
        pass

    def ensure_upstream(self) -> None:
        pass


def _make_hub() -> StubStreamHub:
    hub = StubStreamHub()

    async def fake_run() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            hub.cancel_count += 1
            hub.reached_stream_exit.set()
            await asyncio.sleep(0)
            try:
                await hub.stream_exit_gate.wait()
            except asyncio.CancelledError:
                # Second cancel while unwinding: cleanup truncated.
                hub.cancel_count += 1
                raise
            hub.cleanup_done = True
            raise

    hub.task = asyncio.create_task(fake_run())
    return hub


def _fresh_registry(hub: StubStreamHub) -> HubRegistry:
    """Registry holding the stub hub with a grace-removal task armed."""
    registry = HubRegistry(client=None, replay_log=object())
    registry._global = hub  # type: ignore[assignment]
    registry.maybe_arm_grace_if_idle()
    return registry


async def _pump(n: int = 2) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ===========================================================================
# 1 — overlap: close() during grace removal's gather-owned unwind
# ===========================================================================

async def test_close_overlaps_grace_removal_cleanup_completes(monkeypatch):
    monkeypatch.setattr(registry_module, "GRACE_SECONDS", 0)
    hub = _make_hub()
    registry = _fresh_registry(hub)

    await asyncio.wait_for(hub.reached_stream_exit.wait(), 5)
    assert hub.cancel_count == 1  # first (and so far only) cancel

    close_task = asyncio.create_task(registry.close())
    await _pump(2)
    # close() must WAIT for the parked cleanup (via the removal task's
    # gather), never kill it — on the buggy code close() already returned
    # here after re-cancelling the run task.
    assert not close_task.done()

    hub.stream_exit_gate.set()  # cleanup finishes normally
    await asyncio.wait_for(close_task, 5)

    assert hub.cancel_count == 1  # no second CancelledError delivered
    assert hub.cleanup_done is True
    assert registry._global is None
    assert registry._removal_task is None
    assert hub.task.done()


# ===========================================================================
# 2 — duplicate-cancel: close() must skip an already-unwinding child
# ===========================================================================

async def test_close_does_not_recancel_already_unwinding_child(monkeypatch):
    monkeypatch.setattr(registry_module, "GRACE_SECONDS", 0)
    hub = _make_hub()
    registry = HubRegistry(client=None, replay_log=object())
    registry._global = hub  # type: ignore[assignment]

    # External first canceller (e.g. supervisor / ensure_upstream path)
    # — must land on a STARTED task (parked at its idle wait), so the
    # unwind enters fake_run's cleanup path like a real cancelled run().
    await _pump(2)
    hub.task.cancel()
    await asyncio.wait_for(hub.reached_stream_exit.wait(), 5)
    assert hub.cancel_count == 1

    await asyncio.wait_for(registry.close(), 5)

    # close() returned without a second cancel; the parked cleanup can
    # now finish on its own.
    hub.stream_exit_gate.set()
    await _pump(4)
    assert hub.cancel_count == 1
    assert hub.cleanup_done is True
    assert registry._global is None


# ===========================================================================
# 3 — control: grace removal alone (sole canceller)
# ===========================================================================

async def test_grace_removal_alone_completes_cleanup(monkeypatch):
    monkeypatch.setattr(registry_module, "GRACE_SECONDS", 0)
    hub = _make_hub()
    registry = _fresh_registry(hub)

    await asyncio.wait_for(hub.reached_stream_exit.wait(), 5)
    assert hub.cancel_count == 1

    hub.stream_exit_gate.set()  # let cleanup finish normally
    removal = registry._removal_task
    assert removal is not None
    await asyncio.wait_for(removal, 5)

    assert hub.cancel_count == 1
    assert hub.cleanup_done is True
    assert registry._global is None
    assert registry._removal_task is None
    assert hub.task.done()
