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

import pytest
from fastapi import FastAPI

from oc_slimapi.app import lifespan


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
