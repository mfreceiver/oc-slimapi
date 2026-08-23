"""FIX-CORR-1: idle-grace hub teardown writes a cross-domain replay barrier.

The defect: ``HubRegistry._remove_hub_after_grace`` used to only call
``token_hub.on_upstream_reconnect()`` before nulling ``self._global``. The
idle window's opencode events were observed by nobody and never entered the
(process-wide) ReplayLog — yet the log's epoch and per-domain seq continue
across the hub rebuild. A client reconnecting with ``g:<epoch>:N`` where
``N == last_seq`` hit the §7.2 up_to_date branch and got a silently-empty
replay: the gap was invisible.

The fix: the teardown's no-await final segment now calls
``GlobalHub.notify_idle_recycle_loss()`` — resync_all + write_barrier(None)
(global + every token domain) + token-hub clear + epoch-invalidation
callbacks, every step best-effort — BEFORE ``self._global = None`` and
AFTER the task-group gather (no frame can append past the barrier
watermark; no new hub can append into the barrier's span).

Plan: docs/automatic/.work/20260823-1227_deep-code-quality/main/fix-plan-corr.md §1.
"""

from __future__ import annotations

import logging

import pytest

from oc_slimapi.sse import registry as registry_module
from oc_slimapi.sse.registry import HubRegistry
from oc_slimapi.sse.replay_log import (
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayLog,
    ReplayResync,
    RESYNC_RECONNECT_NO_REPLAY,
    token_domain,
)


@pytest.fixture()
def fast_grace(monkeypatch):
    monkeypatch.setattr(registry_module, "GRACE_SECONDS", 0.01)


class _BoomTokenHub:
    """Stand-in token hub whose reconnect hook blows up.

    ``subscriber_count = 0`` keeps :meth:`GlobalHub.has_consumers` happy
    (it reads the attribute when the hub has no control-plane subscribers).
    """

    subscriber_count = 0

    def on_upstream_reconnect(self) -> None:
        raise RuntimeError("reconnect observer boom")


def _armed_registry(log: ReplayLog | None, callbacks=()) -> HubRegistry:
    """Registry with a live hub created AFTER log/callback wiring.

    Mirrors app.py's lifespan ordering (set_replay_log /
    add_upstream_loss_callback BEFORE the first get()), so the hub receives
    the log via the ctor kwarg and the callbacks via get()'s forward loop.
    """
    registry = HubRegistry(None)
    if log is not None:
        registry.set_replay_log(log)
    for callback in callbacks:
        registry.add_upstream_loss_callback(callback)
    registry.get_global()
    return registry


async def _run_idle_removal(registry: HubRegistry) -> None:
    registry.maybe_arm_grace_if_idle()
    task = registry._removal_task
    assert task is not None
    await task


# ===========================================================================
# 1 — barrier spans global + token domains; old cursor → reconnect_no_replay
# ===========================================================================


async def test_idle_removal_writes_cross_domain_barrier(fast_grace):
    log = ReplayLog()
    registry = _armed_registry(log)

    for i in range(3):
        log.append(GLOBAL_DOMAIN, {"i": i})
    sid_domain = token_domain("s1")
    for i in range(2):
        log.append(sid_domain, {"i": i})
    g_last = log.last_seq(GLOBAL_DOMAIN)
    t_last = log.last_seq(sid_domain)
    assert log.barrier_watermark(GLOBAL_DOMAIN) is None
    assert log.barrier_watermark(sid_domain) is None

    await _run_idle_removal(registry)

    assert registry._global is None
    # The barrier watermarks pin each domain's last_seq at teardown time.
    assert log.barrier_watermark(GLOBAL_DOMAIN) == g_last
    assert log.barrier_watermark(sid_domain) == t_last

    # Global cursor AT the watermark — the exact defect form that used to
    # answer up_to_date with an empty replay.
    out = log.replay(GLOBAL_DOMAIN, after_seq=g_last, epoch=log.epoch)
    assert isinstance(out, ReplayResync)
    assert out.reason == RESYNC_RECONNECT_NO_REPLAY
    assert not isinstance(out, ReplayFrames)

    # Token cursor below the watermark → same resync decision.
    out_t = log.replay(sid_domain, after_seq=t_last - 1, epoch=log.epoch)
    assert isinstance(out_t, ReplayResync)
    assert out_t.reason == RESYNC_RECONNECT_NO_REPLAY


async def test_idle_removal_barrier_covers_cursor_at_last_seq_token_domain(
    fast_grace,
):
    """Token-domain cursor == last_seq (defect's original trigger shape)."""
    log = ReplayLog()
    registry = _armed_registry(log)

    sid_domain = token_domain("sid-42")
    for i in range(4):
        log.append(sid_domain, {"i": i})
    t_last = log.last_seq(sid_domain)

    await _run_idle_removal(registry)

    assert registry._global is None
    out = log.replay(sid_domain, after_seq=t_last, epoch=log.epoch)
    assert isinstance(out, ReplayResync)
    assert out.reason == RESYNC_RECONNECT_NO_REPLAY


# ===========================================================================
# 2 — epoch-invalidation callbacks fire (registered before hub creation)
# ===========================================================================


async def test_idle_removal_fires_loss_callbacks(fast_grace):
    log = ReplayLog()
    calls: list[str] = []
    registry = _armed_registry(
        log, callbacks=[lambda: calls.append("cb1"), lambda: calls.append("cb2")]
    )

    await _run_idle_removal(registry)

    assert registry._global is None
    # Both forwarded callbacks fired exactly once from the teardown path.
    assert sorted(calls) == ["cb1", "cb2"]


# ===========================================================================
# 3 — per-step best-effort: a raising token hub must not skip the rest
# ===========================================================================


async def test_idle_removal_token_hub_boom_still_drops_hub_and_callbacks(
    fast_grace, caplog
):
    log = ReplayLog()
    calls: list[str] = []
    registry = HubRegistry(None)
    registry.set_replay_log(log)
    registry.add_upstream_loss_callback(lambda: calls.append("cb"))
    registry._token_hub = _BoomTokenHub()
    registry.get_global()

    log.append(GLOBAL_DOMAIN, {"i": 0})
    g_last = log.last_seq(GLOBAL_DOMAIN)

    with caplog.at_level(logging.WARNING):
        # Must not raise out of the removal task (F-011 discipline).
        await _run_idle_removal(registry)

    # The hub reference is STILL dropped (teardown completes), the barrier
    # is STILL written, and the callback STILL ran — every step of
    # notify_idle_recycle_loss is individually guarded.
    assert registry._global is None
    assert log.barrier_watermark(GLOBAL_DOMAIN) == g_last
    assert calls == ["cb"]
    assert "token-hub clear on idle recycle failed" in caplog.text


# ===========================================================================
# 4 — the barrier must not swallow the NEXT hub's frames
# ===========================================================================


async def test_barrier_does_not_swallow_new_hub_frames(fast_grace):
    log = ReplayLog()
    registry = _armed_registry(log)
    old_hub = registry.get_global()

    log.append(GLOBAL_DOMAIN, {"i": 0})
    g_last = log.last_seq(GLOBAL_DOMAIN)

    await _run_idle_removal(registry)
    assert log.barrier_watermark(GLOBAL_DOMAIN) == g_last

    # A new subscriber arrives after the recycle: get() builds a fresh hub
    # over the SAME process-wide log (epoch/seq continue, watermark kept).
    new_hub = registry.get_global()
    assert new_hub is not old_hub

    e1 = log.append(GLOBAL_DOMAIN, {"i": "post-1"})  # seq = g_last + 1
    e2 = log.append(GLOBAL_DOMAIN, {"i": "post-2"})  # seq = g_last + 2

    # A pre-teardown cursor still resyncs (barrier is monotonic, sticky).
    out_old = log.replay(GLOBAL_DOMAIN, after_seq=g_last, epoch=log.epoch)
    assert isinstance(out_old, ReplayResync)
    assert out_old.reason == RESYNC_RECONNECT_NO_REPLAY

    # A cursor ABOVE the watermark (a frame the new hub published) falls
    # through to the normal window judgment and replays normally — the
    # barrier's span ends at the teardown watermark.
    out_new = log.replay(GLOBAL_DOMAIN, after_seq=e1.seq, epoch=log.epoch)
    assert isinstance(out_new, ReplayFrames)
    assert [entry.seq for entry in out_new.entries] == [e2.seq]


# ===========================================================================
# 5 — revive abandon path must NOT write a barrier (observation continued)
# ===========================================================================


async def test_revived_hub_does_not_write_barrier(fast_grace):
    """A consumer arriving during the grace window aborts the removal —
    the observation did not stop, so writing a barrier would be an
    over-signal.

    subscribe() is synchronous and runs BEFORE the removal task's first
    await, so by the time the task wakes from its grace sleep the first
    revive-check sees the consumer deterministically.
    """
    log = ReplayLog()
    registry = _armed_registry(log)
    hub = registry.get_global()

    registry.maybe_arm_grace_if_idle()
    task = registry._removal_task
    assert task is not None
    # Consumer arrives during the grace window (before the task runs).
    sub = registry.subscribe()

    log.append(GLOBAL_DOMAIN, {"i": 0})
    await task

    # has_consumers() was True at the first check → removal abandoned,
    # hub retained, NO barrier written.
    assert registry._global is hub
    assert log.barrier_watermark(GLOBAL_DOMAIN) is None

    out = log.replay(GLOBAL_DOMAIN, after_seq=0, epoch=log.epoch)
    assert isinstance(out, ReplayFrames)  # normal window judgment
    assert len(out.entries) == 1

    # Consumer leaves again → the next idle period tears the hub down and
    # NOW the barrier lands (watermark = last published seq).
    registry.unsubscribe(sub)
    await _run_idle_removal(registry)
    assert registry._global is None
    assert log.barrier_watermark(GLOBAL_DOMAIN) == log.last_seq(GLOBAL_DOMAIN)


# ===========================================================================
# 6 — no replay log wired: teardown still completes (v3-only / test stack)
# ===========================================================================


async def test_idle_removal_without_replay_log_still_tears_down(fast_grace):
    registry = _armed_registry(log=None)

    await _run_idle_removal(registry)

    assert registry._global is None
    assert registry._removal_task is None


async def test_barrier_write_failure_fails_open_with_error_log(
    fast_grace, monkeypatch, caplog
):
    """m3 (FIX-CORR-1r2): a raising ``write_barrier`` is fail-open by
    design — the hub is STILL released (teardown never wedges) and the
    reopened silent-loss gap is escalated to ERROR for operator
    visibility (the disconnect path keeps WARNING; it has a retry loop)."""
    log = ReplayLog()
    registry = _armed_registry(log=log)

    def boom_barrier(domain=None):
        raise RuntimeError("barrier write boom")

    monkeypatch.setattr(log, "write_barrier", boom_barrier)
    with caplog.at_level(logging.WARNING):
        await _run_idle_removal(registry)

    assert registry._global is None  # fail-open: teardown completed
    assert registry._removal_task is None
    assert "idle-recycle replay barrier write FAILED" in caplog.text
    # Escalation check: the barrier branch logs at ERROR level.
    assert any(
        rec.levelno == logging.ERROR
        and "replay barrier write FAILED" in rec.message
        for rec in caplog.records
    )
