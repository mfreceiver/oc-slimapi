"""L1-2 (F-216): GlobalHub catch-all drop counters — bounded, sampled, off-wire.

Locks the internal observability added at the tail of ``GlobalHub.publish``
(the `# Drop text deltas, tool.*, message.part.*, and anything else`
fall-through):

1. Per-type counting: every event that falls through ALL curated branches
   increments ``upstream_dropped_events_total[type]`` exactly once.
2. Curated events never reach the counter (IMMEDIATE q/p, session.*,
   message.* all return early). The table is exposed on /slimapi/metrics
   as the additive ``droppedEventsByType`` hubs[] key (2026-08-21 R-5
   ruling, superseding the 4.5.0 internal-only decision — wire tests in
   tests/test_metrics_dropped_events.py).
3. Cardinality bound: beyond ``_DROPPED_TYPES_MAX`` distinct types, new
   types fold into the ``__other__`` bucket; a missing/non-string payload
   ``type`` also lands there instead of crashing.

Plan: docs/ocmar/plans/2026-08-21-audit-fix-batch1.md §泳道 L1-2.
"""

from __future__ import annotations

from conftest import current_replay_log

from oc_slimapi.sse import global_hub as global_hub_module
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.registry import HubRegistry


def ev(directory, event_type: str, properties: dict | None = None) -> dict:
    """Build one upstream /global/event frame."""
    return {"directory": directory, "payload": {"type": event_type, "properties": properties or {}}}


def test_per_type_drop_counts():
    hub = GlobalHub(None, replay_log=current_replay_log())
    hub.publish(ev(None, "todo.updated"))
    hub.publish(ev(None, "todo.updated"))
    hub.publish(ev(None, "file.edited"))
    assert hub.upstream_dropped_events_total == {
        "todo.updated": 2,
        "file.edited": 1,
    }
    # The total counter keeps counting everything (pre-existing behavior).
    assert hub.upstream_events_total == 3


def test_curated_events_are_not_counted_as_dropped():
    hub = GlobalHub(None, replay_log=current_replay_log())
    # Every curated family returns early — none may reach the catch-all
    # counter.
    hub.publish(ev("/p", "permission.asked", {"id": "p1"}))       # IMMEDIATE
    hub.publish(ev("/s", "session.updated", {"info": {"id": "s1"}}))  # SESSION_EVENTS
    hub.publish(ev("/s", "session.status", {"sessionID": "s1", "status": "busy"}))
    hub.publish(
        ev("/s", "message.updated", {"sessionID": "s1", "info": {"id": "m1"}})
    )  # MESSAGE_EVENTS
    assert hub.upstream_dropped_events_total == {}

    # shape 加性演进：droppedEventsByType（2026-08-21 R-5 裁决，取代 4.5.0
    # 内部-only 决定）——hubs[] 条目现在暴露该表（浅拷贝），既有键零改动。
    registry = HubRegistry(None, replay_log=current_replay_log())
    registry.get_global().publish(ev(None, "todo.updated"))
    hub_entry = registry.snapshot_metrics()["sse"]["hubs"][0]
    assert set(hub_entry) == {
        "subscribers",
        "upstreamConnected",
        "upstreamEventsTotal",
        "emittedFramesTotal",
        "reconnectsTotal",
        "droppedEventsByType",
    }
    assert hub_entry["droppedEventsByType"] == {"todo.updated": 1}
    assert registry.get_global().upstream_dropped_events_total == {"todo.updated": 1}


def test_drop_table_cardinality_is_bounded(monkeypatch):
    monkeypatch.setattr(global_hub_module, "_DROPPED_TYPES_MAX", 2)
    hub = GlobalHub(None, replay_log=current_replay_log())
    hub.publish(ev(None, "todo.updated"))
    hub.publish(ev(None, "file.edited"))
    hub.publish(ev(None, "tool.updated"))  # 3rd distinct type → __other__
    assert set(hub.upstream_dropped_events_total) == {
        "todo.updated", "file.edited", "__other__",
    }
    assert hub.upstream_dropped_events_total["__other__"] == 1
    # A missing payload ``type`` (event_type=None) must not crash and must
    # not grow the table — it lands in the same overflow bucket.
    hub.publish({"directory": None, "payload": {"properties": {}}})
    assert set(hub.upstream_dropped_events_total) == {
        "todo.updated", "file.edited", "__other__",
    }
    assert hub.upstream_dropped_events_total["__other__"] == 2
