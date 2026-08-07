"""P0-1 regression tests: transactional startup rollback via AsyncExitStack.

The ``@asynccontextmanager`` lifespan only enters its ``finally`` block AFTER
``yield``. Before P0-1, an exception raised during resource setup (before
``yield``) would bypass cleanup → leaked httpx client, executor, hub tasks,
maintenance tasks. The AsyncExitStack refactor registers each cleanup callback
at resource-creation time so the stack unwinds in LIFO order on ANY exit path
(startup-failure, normal shutdown, cancellation).

Each test injects a failure at a specific assembly step and asserts that the
resources created *before* that step were cleaned up.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.app import _log_maint_task_exception, lifespan


def _test_settings(tmp_path, **overrides):
    """Construct a valid Settings suitable for lifespan testing."""
    from oc_slimapi.config import Settings

    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        smoke_session_id=None,
        access_log_enabled=True,
        access_log_dir=str(tmp_path),
        traffic_snapshot_enabled=False,
        traffic_metrics_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Core test: smoke() raises → upstream/transforms/hubs/token_hub cleaned up.
# ---------------------------------------------------------------------------

async def test_startup_failure_after_smoke_cleans_up_resources(monkeypatch, tmp_path):
    """P0-1: a RuntimeError in smoke() (called after all main resources are
    created) must trigger cleanup of upstream client, transform executor, hub
    registry, and token hub — proving the AsyncExitStack ran on the
    startup-failure path."""
    test_settings = _test_settings(tmp_path)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    # Inject failure in smoke — it runs after upstream/transforms/hubs/token_hub.
    async def _boom(app):
        raise RuntimeError("injected startup failure")
    monkeypatch.setattr("oc_slimapi.app.smoke", _boom)

    app = FastAPI()
    with pytest.raises(RuntimeError, match="injected startup failure"):
        async with lifespan(app):
            pytest.fail("yield body must not be reached on startup failure")

    # Upstream httpx client must be closed (async cleanup ran).
    assert app.state.upstream.is_closed

    # Transform executor must be shut down (sync cleanup ran).
    assert app.state.transforms._executor._shutdown

    # Access log handlers must be removed (sync cleanup ran).
    from oc_slimapi.access_log import get_access_logger
    assert len(get_access_logger().handlers) == 0


# ---------------------------------------------------------------------------
# Early failure: create_client raises → only earlier resources cleaned up.
# ---------------------------------------------------------------------------

async def test_startup_failure_before_upstream_cleans_partial(monkeypatch, tmp_path):
    """P0-1: a failure before upstream creation must still clean up resources
    created before that point (access log handler). The upstream must NOT
    exist on app.state."""
    test_settings = _test_settings(tmp_path)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    # Inject failure in create_client — runs after access_log + snapshotter.
    def _boom_client(settings):
        raise RuntimeError("client construction failed")
    monkeypatch.setattr("oc_slimapi.app.create_client", _boom_client)

    app = FastAPI()
    with pytest.raises(RuntimeError, match="client construction failed"):
        async with lifespan(app):
            pytest.fail("yield body must not be reached")

    # Upstream was never created.
    assert not hasattr(app.state, "upstream")

    # Access log handlers cleaned up (earliest registered cleanup).
    from oc_slimapi.access_log import get_access_logger
    assert len(get_access_logger().handlers) == 0


# ---------------------------------------------------------------------------
# Normal shutdown: yield exits cleanly → all cleanups run (no exception).
# ---------------------------------------------------------------------------

async def test_normal_shutdown_cleans_up_upstream(monkeypatch, tmp_path):
    """P0-1: normal shutdown (yield exits without exception) still triggers
    all cleanup callbacks — the AsyncExitStack does not regress the existing
    finally-block semantics."""
    test_settings = _test_settings(tmp_path, smoke_session_id=None)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    # Patch smoke to be a no-op so the lifespan reaches yield without hitting
    # a real upstream.
    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False
    monkeypatch.setattr("oc_slimapi.app.smoke", _noop_smoke)

    app = FastAPI()
    async with lifespan(app):
        # While inside yield — upstream is open.
        assert not app.state.upstream.is_closed

    # After yield exits — upstream must be closed.
    assert app.state.upstream.is_closed
    assert app.state.transforms._executor._shutdown

    from oc_slimapi.access_log import get_access_logger
    assert len(get_access_logger().handlers) == 0


# ---------------------------------------------------------------------------
# P1-38: maintenance task — exception recovery + CancelledError separation +
# graceful drain.
# ---------------------------------------------------------------------------

async def test_log_maint_task_exception_with_error(caplog):
    """P1-38: _log_maint_task_exception logs a non-cancelled task's exception."""
    async def _crash():
        raise RuntimeError("boom")

    task = asyncio.create_task(_crash())
    await asyncio.sleep(0.01)  # let the task run and crash
    assert task.done()
    with caplog.at_level("WARNING"):
        _log_maint_task_exception(task)
    assert any(
        "maintenance task exited with error" in r.message for r in caplog.records
    )


async def test_log_maint_task_exception_cancelled_no_warn(caplog):
    """P1-38: a cancelled task does NOT trigger the exception warning."""
    async def _sleep():
        await asyncio.Event().wait()

    task = asyncio.create_task(_sleep())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    with caplog.at_level("WARNING"):
        _log_maint_task_exception(task)
    assert not any(
        "maintenance task exited" in r.message for r in caplog.records
    )


def _patch_lifespan_for_shutdown_test(monkeypatch, tmp_path, maint_coro):
    """Wire common monkeypatches so the lifespan reaches yield quickly."""
    test_settings = _test_settings(tmp_path)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False
    monkeypatch.setattr("oc_slimapi.app.smoke", _noop_smoke)

    # Mock upstream client that fails instantly (no 5s connect timeout).
    def _mock_client(settings):
        return httpx.AsyncClient(
            base_url=settings.upstream,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("no upstream"))
            ),
        )
    monkeypatch.setattr("oc_slimapi.app.create_client", _mock_client)

    monkeypatch.setattr(
        "oc_slimapi.app.run_access_log_maintenance_loop", maint_coro
    )


async def test_maintenance_crash_exception_recovered_on_shutdown(
    monkeypatch, tmp_path, caplog
):
    """P1-38: maintenance task dies with an unhandled exception → the exception
    is recovered (logged + consumed via task.exception()) on shutdown."""
    async def _crash_loop(*, dir, retain_days, interval_s, stop_event):
        raise RuntimeError("maintenance loop crashed")

    _patch_lifespan_for_shutdown_test(monkeypatch, tmp_path, _crash_loop)

    app = FastAPI()
    with caplog.at_level("WARNING"):
        async with lifespan(app):
            # Give the maintenance task a chance to run and crash.
            await asyncio.sleep(0.05)

    assert any(
        "maintenance task exited with error" in r.message for r in caplog.records
    )


async def test_maintenance_cancelled_cleanly(monkeypatch, tmp_path):
    """P1-38: a sleeping maintenance task is gracefully drained then cancelled
    (CancelledError handled separately, no unhandled error)."""
    # Short drain timeout so the test does not wait 30 s.
    monkeypatch.setattr("oc_slimapi.app._MAINT_DRAIN_TIMEOUT", 0.1)

    async def _sleep_forever(*, dir, retain_days, interval_s, stop_event):
        await asyncio.Event().wait()  # never returns on its own

    _patch_lifespan_for_shutdown_test(monkeypatch, tmp_path, _sleep_forever)

    app = FastAPI()
    async with lifespan(app):
        await asyncio.sleep(0.05)

    # Task must be done/cancelled — if it were still pending the lifespan
    # would have hung (the test would time out).
    task = app.state._access_log_maintenance_task
    assert task.done()


# ---------------------------------------------------------------------------
# P1-39: access-log handler failure gate — maintenance suppressed when the
# DailyAccessHandler install fails (directory not writable), even though
# access_log_enabled is True in config.
# ---------------------------------------------------------------------------

async def test_access_log_setup_failure_suppresses_maintenance(monkeypatch, tmp_path):
    """P1-39: when setup_access_log fails (logger.disabled), the maintenance
    task must NOT be created and startup maintenance must NOT run."""
    # A path that cannot be created (parent is a file, not a dir) forces
    # setup_access_log to fail → logger.disabled = True.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    unwritable_dir = str(blocker / "subdir")

    test_settings = _test_settings(tmp_path, access_log_dir=unwritable_dir)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False
    monkeypatch.setattr("oc_slimapi.app.smoke", _noop_smoke)

    def _mock_client(settings):
        return httpx.AsyncClient(
            base_url=settings.upstream,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("no upstream"))
            ),
        )
    monkeypatch.setattr("oc_slimapi.app.create_client", _mock_client)

    app = FastAPI()
    async with lifespan(app):
        await asyncio.sleep(0.01)

    # No maintenance task created.
    assert not hasattr(app.state, "_access_log_maintenance_task")


async def test_access_log_disabled_no_maintenance(monkeypatch, tmp_path):
    """P1-39: access_log_enabled=False → no maintenance task (sanity check)."""
    test_settings = _test_settings(tmp_path, access_log_enabled=False)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False
    monkeypatch.setattr("oc_slimapi.app.smoke", _noop_smoke)

    def _mock_client(settings):
        return httpx.AsyncClient(
            base_url=settings.upstream,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("no upstream"))
            ),
        )
    monkeypatch.setattr("oc_slimapi.app.create_client", _mock_client)

    app = FastAPI()
    async with lifespan(app):
        await asyncio.sleep(0.01)

    assert not hasattr(app.state, "_access_log_maintenance_task")
