from __future__ import annotations

import asyncio
import importlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from oc_slimapi.qp_sweep import QpSweepShadow
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.traffic import bucketize


def test_shadow_touch_never_calls_upstream_transport():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    shadow = QpSweepShadow(
        interval_seconds=0.05,
        daily_budget=2,
        now=lambda: 10.0,
        directories=["/cold"],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shadow.run_once(now=10.2)
    shadow.run_once(now=10.3)
    assert calls == 0
    assert shadow.snapshot()["cold_hits"] == 2
    assert shadow.markers[-1]["would_sweep"] is True


def test_interval_drives_multiple_scheduler_rounds():
    async def scenario():
        shadow = QpSweepShadow(
            interval_seconds=0.005,
            daily_budget=10,
            directories=["/d"],
            jitter=lambda: 1.0,
        )
        shadow.start()
        await asyncio.sleep(0.018)
        await shadow.stop()
        return shadow.snapshot()["triggers_total"]

    assert asyncio.run(scenario()) >= 2


def test_jitter_is_bounded_to_twenty_percent():
    values = []
    shadow = QpSweepShadow(interval_seconds=10.0, jitter=lambda: 1.0)
    for factor in (0.8, 1.0, 1.2):
        shadow.jitter = lambda factor=factor: factor
        values.append(shadow.next_delay())
    assert values == [8.0, 10.0, 12.0]
    assert all(8.0 <= value <= 12.0 for value in values)


def test_round_robin_processes_only_a_small_batch_each_round():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        daily_budget=10,
        directories=["/a", "/b", "/c"],
        batch_size=1,
        now=lambda: 100.0,
    )
    assert [m["directory"] for m in shadow.run_once(now=104.0)] == ["/a"]
    assert [m["directory"] for m in shadow.run_once(now=104.0)] == ["/b"]
    assert [m["directory"] for m in shadow.run_once(now=104.0)] == ["/c"]


def test_activity_within_interval_skips_touch():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"])
    shadow.record_activity("/d", now=10.0)
    marker = shadow.run_once(now=10.9)[0]
    assert marker["decision"] == "skip"
    assert shadow.snapshot()["skips"] == 1


def test_three_intervals_without_activity_are_cold():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"])
    shadow.record_activity("/d", now=10.0)
    marker = shadow.run_once(now=13.0)[0]
    assert marker["decision"] == "cold"
    assert marker["would_sweep"] is True


def test_activity_recovery_exits_cold_set():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"])
    shadow.record_activity("/d", now=10.0)
    assert shadow.run_once(now=13.0)[0]["decision"] == "cold"
    shadow.record_activity("/d", now=13.1)
    assert shadow.run_once(now=13.9)[0]["decision"] == "skip"


def test_daily_budget_marks_later_cold_touches_exhausted():
    shadow = QpSweepShadow(interval_seconds=1.0, daily_budget=2, directories=["/a", "/b", "/c"])
    for directory in ("/a", "/b", "/c"):
        shadow.record_activity(directory, now=0.0)
    decisions = [m["decision"] for m in shadow.run_once(now=3.0) + shadow.run_once(now=3.0) + shadow.run_once(now=3.0)]
    assert decisions == ["cold", "cold", "budget_exhausted"]
    assert shadow.snapshot()["budget_exhausted"] == 1


def test_budget_resets_at_utc_day_boundary():
    day = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    shadow = QpSweepShadow(interval_seconds=1.0, daily_budget=1, directories=["/d"])
    shadow.record_activity("/d", now=day)
    assert shadow.run_once(now=day + 3.0)[0]["decision"] == "cold"
    assert shadow.run_once(now=day + 3.0)[0]["decision"] == "budget_exhausted"
    assert shadow.run_once(now=day + 86400.0 + 3.0)[0]["decision"] == "cold"


def test_global_hub_immediate_qp_event_updates_activity():
    hub = GlobalHub(None)
    before = time.time()
    hub.publish({"directory": "/repo", "payload": {"type": "question.asked", "properties": {}}})
    assert before <= hub.qp_last_activity["/repo"] <= time.time()


def test_non_qp_hub_event_does_not_update_qp_activity():
    hub = GlobalHub(None)
    hub.publish({"directory": "/repo", "payload": {"type": "session.updated", "properties": {}}})
    assert "/repo" not in hub.qp_last_activity


def test_other_immediate_hub_event_does_not_update_qp_activity():
    hub = GlobalHub(None)
    hub.publish({"directory": "/repo", "payload": {"type": "server.connected", "properties": {}}})
    assert "/repo" not in hub.qp_last_activity


def test_request_activity_updates_shared_activity_tracker():
    activity: dict[str, float] = {}
    shadow = QpSweepShadow(activity=activity, interval_seconds=1.0)
    shadow.record_request_activity(["/questions", "/permissions"], now=5.0)
    assert activity == {"/questions": 5.0, "/permissions": 5.0}


def test_external_global_hub_activity_becomes_a_known_directory():
    activity = {"/repo": 10.0}
    shadow = QpSweepShadow(activity=activity, interval_seconds=1.0)
    assert shadow.run_once(now=10.9)[0]["decision"] == "skip"


def test_markers_and_counters_have_observable_shape():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"])
    shadow.record_activity("/d", now=0.0)
    shadow.run_once(now=3.0)
    snapshot = shadow.snapshot()
    assert set(snapshot) >= {"triggers_total", "cold_hits", "skips", "budget_exhausted", "est_bytes_total", "markers"}
    assert set(snapshot["markers"][0]) == {"ts", "directory", "decision", "would_sweep"}


def test_metrics_shape_can_be_exposed_as_sweep_block():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"])
    assert shadow.metrics() == {
        "triggers_total": 0,
        "cold_hits": 0,
        "skips": 0,
        "budget_exhausted": 0,
        "est_bytes_total": 0,
    }


def test_shadow_marker_path_has_sweep_traffic_bucket():
    assert bucketize("GET", "/slimapi/_shadow/sweep") == "sweep"


def test_known_digest_directory_source_is_included():
    pending = {"sid": SimpleNamespace(directory="/digest")}
    shadow = QpSweepShadow(interval_seconds=1.0, directory_source=lambda: pending.values())
    shadow.run_once(now=3.0)
    assert shadow.markers[0]["directory"] == "/digest"


def test_disabled_shadow_does_not_start_task():
    shadow = QpSweepShadow(interval_seconds=0.01, enabled=False, directories=["/d"])
    shadow.start()
    assert shadow.task is None
    assert shadow.running is False


def test_config_defaults_and_env_override(monkeypatch):
    import oc_slimapi.config as config_mod

    assert config_mod.Settings().qp_sweep_enabled is True
    assert config_mod.Settings().qp_sweep_interval_seconds == 1800.0
    monkeypatch.setenv("OC_SLIMAPI_QP_SWEEP_ENABLED", "false")
    monkeypatch.setenv("OC_SLIMAPI_QP_SWEEP_INTERVAL_SECONDS", "0.25")
    reloaded = importlib.reload(config_mod)
    assert reloaded.Settings().qp_sweep_enabled is False
    assert reloaded.Settings().qp_sweep_interval_seconds == 0.25
    importlib.reload(config_mod)


@pytest.mark.parametrize("field,value", [("qp_sweep_interval_seconds", 0), ("qp_sweep_daily_budget", -1)])
def test_config_rejects_invalid_sweep_values(field, value):
    from oc_slimapi.config import Settings

    with pytest.raises(RuntimeError):
        Settings(**{field: value}).validate()
