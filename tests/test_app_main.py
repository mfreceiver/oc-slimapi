from types import SimpleNamespace

import oc_slimapi.app as app_mod


def test_main_passes_graceful_shutdown_timeout(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    # 用 SimpleNamespace 整体替换模块级 frozen settings：避免 monkeypatch
    # frozen dataclass 的 validate 属性在构造后不可靠（frozen 实例禁止赋值）。
    fake_settings = SimpleNamespace(host="127.0.0.1", port=4097, validate=lambda: None)
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(app_mod.uvicorn, "run", fake_run)
    app_mod.main()
    assert captured["kwargs"]["timeout_graceful_shutdown"] == 5.0
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 4097


async def test_qp_sweep_stop_failure_does_not_skip_later_cleanups(
    monkeypatch, tmp_path, caplog
):
    """F-007（L3-1）：_stop_qp_sweep 异常隔离——qp_sweep.stop() 抛 RuntimeError
    时 lifespan 关停不传播该异常，且 LIFO 中排在其后的清理回调（snapshotter
    stop / upstream aclose / transforms drain / access-log handler flush）
    仍全部执行。"""
    import httpx
    from fastapi import FastAPI

    from oc_slimapi.config import Settings
    from oc_slimapi.traffic_snapshot import TrafficSnapshotter

    test_settings = Settings(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        smoke_session_id=None,
        access_log_enabled=True,
        access_log_dir=str(tmp_path),
        traffic_snapshot_enabled=True,
        traffic_metrics_enabled=True,
        traffic_snapshot_path=str(tmp_path / "traffic-snapshot.jsonl"),
        traffic_snapshot_interval_s=300,
        qp_sweep_enabled=True,
    )
    monkeypatch.setattr(app_mod, "settings", test_settings)

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False

    monkeypatch.setattr(app_mod, "smoke", _noop_smoke)

    def _mock_client(settings):
        return httpx.AsyncClient(
            base_url=settings.upstream,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("no upstream"))
            ),
        )

    monkeypatch.setattr(app_mod, "create_client", _mock_client)

    stop_calls: list[str] = []

    class _FailingQpSweep:
        """Stub QpSweepShadow whose stop() always raises (injected failure)."""

        def __init__(self, **kwargs):
            pass

        def observe_directory(self, directory):
            pass

        def start(self):
            pass

        async def stop(self):
            stop_calls.append("qp_sweep")
            raise RuntimeError("injected qp sweep stop failure")

    monkeypatch.setattr(app_mod, "QpSweepShadow", _FailingQpSweep)

    class _RecordingSnapshotter(TrafficSnapshotter):
        async def stop(self):
            stop_calls.append("snapshotter")
            await super().stop()

    monkeypatch.setattr(app_mod, "TrafficSnapshotter", _RecordingSnapshotter)

    app = FastAPI()
    with caplog.at_level("WARNING"):
        # Exiting the block must NOT raise: _stop_qp_sweep catches the
        # injected RuntimeError, and every later LIFO cleanup still runs.
        async with app_mod.lifespan(app):
            assert app.state.qp_sweep is not None
            assert not app.state.upstream.is_closed

    # F-007 isolation path exercised: the failure was caught + logged.
    assert any("qp sweep stop failed" in r.message for r in caplog.records)

    # LIFO order: _stop_qp_sweep (registered late) runs BEFORE the
    # earlier-registered snapshotter stop — and both ran despite the failure.
    assert stop_calls == ["qp_sweep", "snapshotter"]

    # Every later LIFO cleanup still executed:
    assert app.state.upstream.is_closed
    assert app.state.transforms._executor._shutdown

    from oc_slimapi.access_log import get_access_logger

    assert len(get_access_logger().handlers) == 0
