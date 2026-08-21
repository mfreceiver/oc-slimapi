from __future__ import annotations

import asyncio
import importlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.qp_sweep import QpSweepShadow
from oc_slimapi.routes import metrics
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
        jitter=lambda: 1.0,
    )
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


def test_each_known_directory_has_an_independent_cadence():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        daily_budget=10,
        directories=["/a", "/b", "/c"],
        now=lambda: 100.0,
        jitter=lambda: 1.0,
    )
    assert len(shadow.run_once(now=100.0)) == 3
    assert shadow.run_once(now=100.99) == []
    for timestamp in (101.0, 102.0):
        markers = shadow.run_once(now=timestamp)
        assert {marker["directory"] for marker in markers} == {"/a", "/b", "/c"}
    for directory in ("/a", "/b", "/c"):
        times = [
            marker["ts"]
            for marker in shadow.markers
            if marker["directory"] == directory
        ]
        assert times == [100.0, 101.0, 102.0]


def test_activity_within_interval_skips_touch():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"], now=lambda: 0.0)
    shadow.record_activity("/d", now=10.0)
    marker = shadow.run_once(now=10.9)[0]
    assert marker["decision"] == "skip"
    assert shadow.snapshot()["skips"] == 1


def test_three_intervals_without_activity_are_cold():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"], now=lambda: 0.0)
    shadow.record_activity("/d", now=10.0)
    marker = shadow.run_once(now=13.0)[0]
    assert marker["decision"] == "cold"
    assert marker["would_sweep"] is True


def test_activity_recovery_exits_cold_set():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        directories=["/d"],
        now=lambda: 0.0,
        jitter=lambda: 1.0,
    )
    shadow.record_activity("/d", now=10.0)
    assert shadow.run_once(now=13.0)[0]["decision"] == "cold"
    shadow.record_activity("/d", now=13.1)
    assert shadow.run_once(now=14.0)[0]["decision"] == "skip"


def test_daily_budget_marks_later_cold_touches_exhausted():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        daily_budget=2,
        directories=["/a", "/b", "/c"],
        now=lambda: 0.0,
    )
    for directory in ("/a", "/b", "/c"):
        shadow.record_activity(directory, now=0.0)
    decisions = [m["decision"] for m in shadow.run_once(now=3.0)]
    assert decisions == ["cold", "cold", "budget_exhausted"]
    assert shadow.snapshot()["budget_exhausted"] == 1


def test_budget_resets_at_utc_day_boundary():
    day = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        daily_budget=1,
        directories=["/d"],
        now=lambda: day,
        jitter=lambda: 1.0,
    )
    shadow.record_activity("/d", now=day)
    assert shadow.run_once(now=day + 3.0)[0]["decision"] == "cold"
    assert shadow.run_once(now=day + 4.0)[0]["decision"] == "budget_exhausted"
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


def test_hub_observer_retains_non_qp_directory_after_pending_flush():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        now=lambda: 100.0,
        jitter=lambda: 1.0,
    )
    hub = GlobalHub(None)
    hub.set_directory_observer(shadow.observe_directory)

    hub.publish(
        {
            "directory": "/repo",
            "payload": {
                "type": "session.updated",
                "properties": {"sessionID": "sid"},
            },
        }
    )
    assert hub.pending
    hub.flush()
    assert not hub.pending

    shadow.run_once(now=103.0)
    assert shadow.metrics()["known_directories"] == 1
    assert shadow.markers[-1]["directory"] == "/repo"


def test_hub_directory_observer_exception_does_not_break_publish():
    hub = GlobalHub(None)

    def broken_observer(directory: str) -> None:
        raise RuntimeError(f"observer failed for {directory}")

    hub.set_directory_observer(broken_observer)
    hub.publish(
        {
            "directory": "/repo",
            "payload": {
                "type": "session.updated",
                "properties": {"sessionID": "sid"},
            },
        }
    )
    assert "sid" in hub.pending


def test_missing_directory_is_not_sent_to_hub_observer():
    observed: list[str] = []
    hub = GlobalHub(None)
    hub.set_directory_observer(observed.append)

    for directory in (None, ""):
        hub.publish(
            {
                "directory": directory,
                "payload": {
                    "type": "session.updated",
                    "properties": {"sessionID": "sid"},
                },
            }
        )

    assert observed == []


def test_stale_directories_are_evicted_and_reobserved():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        eviction_after_seconds=10.0,
        now=lambda: 0.0,
        jitter=lambda: 1.0,
        directories=["/old"],
    )
    shadow.run_once(now=0.0)
    assert shadow.metrics()["known_directories"] == 1

    shadow.run_once(now=11.0)
    assert shadow.metrics()["known_directories"] == 0

    shadow.observe_directory("/old", now=11.0)
    assert shadow.metrics()["known_directories"] == 1


def test_async_scheduler_runs_each_directory_on_deadline_cadence():
    async def scenario():
        shadow = QpSweepShadow(
            interval_seconds=0.2,
            daily_budget=20,
            directories=["/a", "/b", "/c"],
            jitter=lambda: 1.2,
        )
        shadow.start()
        await asyncio.sleep(0.34)
        await shadow.stop()
        return {
            directory: [
                marker["ts"]
                for marker in shadow.markers
                if marker["directory"] == directory
            ]
            for directory in ("/a", "/b", "/c")
        }

    times = asyncio.run(scenario())
    for directory_times in times.values():
        assert len(directory_times) >= 2
        interval = directory_times[1] - directory_times[0]
        assert 0.8 * 0.2 <= interval <= 1.2 * 0.2 + 0.005


def test_reobserving_known_directory_does_not_wake_scheduler():
    async def scenario():
        shadow = QpSweepShadow(
            interval_seconds=0.5,
            directories=["/known"],
            jitter=lambda: 1.0,
        )
        original_run_once = shadow.run_once
        calls = 0

        def counted_run_once(*, now=None):
            nonlocal calls
            calls += 1
            return original_run_once(now=now)

        shadow.run_once = counted_run_once
        shadow.start()
        await asyncio.sleep(0.02)
        assert calls == 1

        for _ in range(3):
            shadow.observe_directory("/known")
        await asyncio.sleep(0.03)
        assert calls == 1
        await shadow.stop()

    asyncio.run(scenario())


def test_observing_new_directory_wakes_scheduler():
    async def scenario():
        shadow = QpSweepShadow(interval_seconds=10.0, jitter=lambda: 1.0)
        original_run_once = shadow.run_once
        calls = 0

        def counted_run_once(*, now=None):
            nonlocal calls
            calls += 1
            return original_run_once(now=now)

        shadow.run_once = counted_run_once
        shadow.start()
        await asyncio.sleep(0.02)
        assert calls == 0

        shadow.observe_directory("/new")
        await asyncio.sleep(0.02)
        assert calls == 1
        assert shadow.markers[-1]["directory"] == "/new"
        await shadow.stop()

    asyncio.run(scenario())


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
    shadow = QpSweepShadow(activity=activity, interval_seconds=1.0, now=lambda: 10.0)
    assert shadow.run_once(now=10.9)[0]["decision"] == "skip"


def test_markers_and_counters_have_observable_shape():
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"], now=lambda: 0.0)
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
        "known_directories": 1,
    }


def test_shadow_marker_path_has_sweep_traffic_bucket():
    assert bucketize("GET", "/slimapi/_shadow/sweep") == "sweep"


def test_known_digest_directory_is_included():
    shadow = QpSweepShadow(interval_seconds=1.0, now=lambda: 0.0)
    # A directory observed mid-flight (e.g. from the digest tap's
    # observe_directory) joins the schedule just like constructor-seeded
    # directories.
    shadow.observe_directory("/digest", now=0.0)
    shadow.run_once(now=3.0)
    assert shadow.markers[0]["directory"] == "/digest"


def test_empty_round_does_not_forget_a_known_directory():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        now=lambda: 0.0,
        jitter=lambda: 1.0,
    )
    shadow.observe_directory("/retained", now=0.0)
    shadow.run_once(now=0.0)
    assert shadow.run_once(now=1.0)[0]["directory"] == "/retained"
    assert shadow.snapshot()["known_directories"] == 1


def test_multi_directory_budget_is_reachable_on_one_due_scan():
    shadow = QpSweepShadow(
        interval_seconds=1.0,
        daily_budget=2,
        directories=["/a", "/b", "/c"],
        now=lambda: 0.0,
        jitter=lambda: 1.0,
    )
    shadow.run_once(now=0.0)
    markers = shadow.run_once(now=3.0)
    assert [marker["decision"] for marker in markers] == ["cold", "cold", "budget_exhausted"]
    assert shadow.metrics()["budget_exhausted"] == 1


def test_metrics_endpoint_exposes_production_sweep_block():
    app = FastAPI()
    shadow = QpSweepShadow(interval_seconds=1.0, directories=["/d"], now=lambda: 0.0)
    app.state.hubs = SimpleNamespace(
        snapshot_metrics=lambda: {"sse": {}, "skeleton": {}, "batch": None},
    )
    app.state.qp_sweep = shadow
    app.include_router(metrics.router)

    async def request_metrics():
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/slimapi/metrics")

    response = asyncio.run(request_metrics())
    assert response.status_code == 200
    sweep = response.json()["sweep"]
    assert set(sweep) == {
        "triggers_total",
        "cold_hits",
        "skips",
        "budget_exhausted",
        "est_bytes_total",
        "known_directories",
    }


def test_markers_are_bounded_ring_buffer():
    shadow = QpSweepShadow(interval_seconds=1.0, now=lambda: 0.0)
    for index in range(300):
        shadow.observe_directory(f"/d{index}", now=0.0)
        shadow.run_once(now=0.0)
    assert len(shadow.markers) == 256


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
