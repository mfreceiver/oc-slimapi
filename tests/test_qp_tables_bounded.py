"""L1-4 (F-015 + F-273 + F-007 half): bounded, LRU q/p activity tables.

Locks the shared q/p activity-table semantics funnelled through
``hub_types.record_qp_activity`` (both write points — GlobalHub's IMMEDIATE
branch and ``QpSweepShadow.record_activity`` — share ONE dict reference by
app.py construction):

1. Mixed write-point overflow: whichever side grows the table, the cap
   holds and the OLDEST entry is evicted (F-015).
2. Activity-LRU move-to-end: refreshing an old directory and then
   overflowing evicts the SECOND-oldest, not the refreshed one (X3).
3. Sweep eviction cascade (F-273): a directory evicted as stale disappears
   from ALL FOUR sweep tables — including the shared activity dict —
   while a freshly-touched directory survives.
4. Scheduler loop guard (F-007 half): a ``run_once`` blow-up is logged
   and the loop keeps scheduling; ``CancelledError`` still propagates.

Plan: docs/ocmar/plans/2026-08-21-audit-fix-batch1.md §泳道 L1-4.
"""

from __future__ import annotations

from conftest import current_replay_log

import asyncio

from oc_slimapi.qp_sweep import QpSweepShadow
from oc_slimapi.sse import hub_types
from oc_slimapi.sse.global_hub import GlobalHub


def ev(directory: str, event_type: str, properties: dict | None = None) -> dict:
    """Build one upstream /global/event frame."""
    return {"directory": directory, "payload": {"type": event_type, "properties": properties or {}}}


def test_dual_write_points_share_one_bounded_table(monkeypatch):
    """F-015: hub-side and sweep-side writes hit the SAME table; the cap
    holds regardless of which side grew it, evicting oldest-first."""
    monkeypatch.setattr(hub_types, "QP_LAST_ACTIVITY_MAX", 5)
    hub = GlobalHub(None, replay_log=current_replay_log())
    sweep = QpSweepShadow(activity=hub.qp_last_activity, interval_seconds=1.0)
    assert sweep.activity is hub.qp_last_activity

    # Write point A: GlobalHub IMMEDIATE q/p branch.
    for directory in ("/d1", "/d2", "/d3"):
        hub.publish(ev(directory, "permission.asked", {"id": "p1"}))
    # Write point B: QpSweepShadow.record_activity.
    sweep.record_activity("/d4")
    sweep.record_activity("/d5")
    assert len(hub.qp_last_activity) == 5

    # One more write on either side evicts the oldest (/d1).
    hub.publish(ev("/d6", "question.asked", {"id": "q1"}))
    sweep.record_activity("/d7")
    assert len(hub.qp_last_activity) == 5
    assert "/d1" not in hub.qp_last_activity
    assert "/d2" not in hub.qp_last_activity
    assert set(hub.qp_last_activity) == {"/d3", "/d4", "/d5", "/d6", "/d7"}


def test_activity_refresh_moves_entry_to_tail(monkeypatch):
    """X3: a re-touched directory keeps its recency — the eviction victim
    is the second-oldest, not the refreshed entry."""
    monkeypatch.setattr(hub_types, "QP_LAST_ACTIVITY_MAX", 3)
    table: dict[str, float] = {}
    sweep = QpSweepShadow(activity=table, interval_seconds=1.0)

    sweep.record_activity("/d1", now=1.0)
    sweep.record_activity("/d2", now=1.1)
    sweep.record_activity("/d3", now=1.2)
    sweep.record_activity("/d1", now=2.0)  # refresh the OLDEST entry
    sweep.record_activity("/d4", now=3.0)  # overflow → evict LRU (/d2)

    assert "/d1" in table
    assert "/d2" not in table
    assert list(table) == ["/d3", "/d1", "/d4"]


def test_stale_directory_eviction_pops_activity_too():
    """F-273: eviction removes the directory from all four tables,
    including the SHARED activity dict; fresh directories survive."""
    table: dict[str, float] = {}
    clock = [0.0]
    sweep = QpSweepShadow(
        activity=table,
        interval_seconds=1.0,
        eviction_after_seconds=30 * 86400.0,
        now=lambda: clock[0],
        jitter=lambda: 1.0,
    )
    sweep.record_activity("/stale", now=0.0)
    fresh_ts = 30 * 86400.0
    sweep.record_activity("/fresh", now=fresh_ts)

    sweep.run_once(now=fresh_ts + 1.0)

    for store in (sweep._known_dirs, sweep._seen_at, sweep._next_run, sweep._activity):
        assert "/stale" not in store
    assert "/fresh" in sweep._activity
    assert "/fresh" in sweep._known_dirs


async def test_scheduler_loop_survives_run_once_exception(monkeypatch):
    """F-007 (half): one poisoned run_once must not kill the scheduler
    task — the loop logs and keeps scheduling."""
    sweep = QpSweepShadow(interval_seconds=0.01, daily_budget=10)
    calls: list[int] = []
    original = sweep.run_once

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("poisoned directory source")
        return original(*args, **kwargs)

    monkeypatch.setattr(sweep, "run_once", flaky)
    task = sweep.start()
    try:
        await asyncio.sleep(0.1)
        assert len(calls) >= 2  # the first blow-up did not end the loop
        assert not task.done()
    finally:
        await sweep.stop()
