"""L1-3 (F-011): HubRegistry grace-removal slot hygiene under failure/races.

Locks the identity-conditional slot clear (``_clear_removal_task_if_current``)
and the exception-proof teardown of ``HubRegistry._remove_hub_after_grace``:

1. A raising ``on_upstream_reconnect`` (or any teardown exception) is
   swallowed into a warning, the ``_removal_task`` slot is released, and a
   later idle period CAN re-arm (previously the dead task occupied the
   slot forever and arming was disabled process-wide).
2. A cancelled STALE task executing its exit path after a NEWER task was
   armed must not erase the newer task's reference (identity guard).
3. Normal path: both the hub reference and the slot are cleared; the task
   is named ``hub-grace-removal``.
4. Code-level (X2): no bare slot-nulling assignment remains anywhere in
   the coroutine body — every clear routes through the helper.

Plan: docs/ocmar/plans/2026-08-21-audit-fix-batch1.md §泳道 L1-3.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect

import pytest

from oc_slimapi.sse import registry as registry_module
from oc_slimapi.sse.registry import HubRegistry


@pytest.fixture()
def fast_grace(monkeypatch):
    monkeypatch.setattr(registry_module, "GRACE_SECONDS", 0.01)


class _BoomTokenHub:
    """Stand-in token hub whose reconnect hook blows up (F-011 use case)."""

    def on_upstream_reconnect(self) -> None:
        raise RuntimeError("reconnect observer boom")


async def test_teardown_exception_releases_slot_and_allows_rearm(fast_grace):
    registry = HubRegistry(None)
    registry.get_global()
    registry._token_hub = _BoomTokenHub()

    registry.maybe_arm_grace_if_idle()
    task = registry._removal_task
    assert task is not None
    # The exception is swallowed into a warning — awaiting must not raise.
    await task
    # F-011 lock: the slot no longer holds the dead task …
    assert registry._removal_task is None
    # … so a later idle period can arm again.
    registry.maybe_arm_grace_if_idle()
    task2 = registry._removal_task
    assert task2 is not None and task2 is not task

    registry.cancel_pending_removal()
    with contextlib.suppress(asyncio.CancelledError):
        await task2


async def test_cancelled_stale_task_does_not_clear_newer_task(fast_grace):
    registry = HubRegistry(None)
    registry.get_global()

    registry.maybe_arm_grace_if_idle()
    task1 = registry._removal_task
    await asyncio.sleep(0)  # let task1 actually enter its grace sleep
    # Control-plane arrival cancels + clears synchronously …
    registry.cancel_pending_removal()
    # … a following last-detach re-arms a NEW task before task1's
    # coroutine is ever scheduled to observe its cancellation.
    registry.maybe_arm_grace_if_idle()
    task2 = registry._removal_task
    assert task2 is not None and task2 is not task1

    # Stale task1 now runs its cancellation exit path; under the identity
    # guard it MUST NOT erase task2's reference from the slot.
    with contextlib.suppress(asyncio.CancelledError):
        await task1
    assert registry._removal_task is task2

    registry.cancel_pending_removal()
    with contextlib.suppress(asyncio.CancelledError):
        await task2


async def test_normal_path_clears_references_and_names_task(fast_grace):
    registry = HubRegistry(None)
    registry.get_global()

    registry.maybe_arm_grace_if_idle()
    task = registry._removal_task
    assert task is not None
    assert task.get_name() == "hub-grace-removal"
    await task

    assert registry._global is None
    assert registry._removal_task is None


def test_grace_body_has_no_bare_slot_assignment():
    """X2: every slot clear in the coroutine routes through the helper.

    ``inspect.getsource`` over ``_remove_hub_after_grace`` must contain no
    bare slot-nulling assignment — only identity-conditional helper calls.
    (The helper's own body, and the external SYNC paths
    ``cancel_pending_removal`` / ``close``, are out of scope here by
    design: they run in the canceller's frame, not the task's.)
    """
    src = inspect.getsource(HubRegistry._remove_hub_after_grace)
    assert "_removal_task = None" not in src
    assert "_clear_removal_task_if_current" in src
