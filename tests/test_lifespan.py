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
    async def _crash_loop(*, dir, retain_days, interval_s, stop_event, extra_prune=None):
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

    async def _sleep_forever(*, dir, retain_days, interval_s, stop_event, extra_prune=None):
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


# ---------------------------------------------------------------------------
# NB-C4: token_hub shutdown must fully complete BEFORE hubs.close() starts.
#
# app.py registers _stop_token_hub (#12) AFTER _close_hubs (#10) so the
# AsyncExitStack LIFO unwind stops the token hub first — the flush task
# must have fully exited (cancellation processed, task done) by the time
# hubs.close() begins, while the hub registry is still coherent. These
# tests pin that invariant by OBSERVING ACTUAL EXECUTION, not registration:
# the real HubRegistry / TokenStreamHub objects are constructed normally and
# only their instance-level close()/stop()/stop_and_wait() methods are
# wrapped with event recorders. The AsyncExitStack itself is never replaced.
# Each wrapper appends ("component", "enter"/"exit") to a shared list at the
# moment the cleanup callback actually starts/completes the underlying call,
# so the list order is a faithful chronological execution trace (single
# event loop → append order == time order).
#
# Completion proof (P2-2): a REAL flush task is running when shutdown
# starts (started inside the yield for normal shutdown; test-only started
# in the construction factory for the startup-failure path, where yield is
# unreachable). Every wrapped stop path snapshots the task's done()-state
# AT ITS RETURN — a sync cancel-without-await deterministically shows
# done()==False there (cancellation needs a loop tick), so unrelated later
# callbacks reaping the task cannot fake a pass. The wrapped hubs.close()
# additionally snapshots done()-state AT ENTRY as the end-to-end check.
# Production starts the flush loop on first subscriber (none here), so the
# tests start it directly.
#
# Deliberately NOT asserted: the full 14-callback reverse order (conditional
# registrations 7/11/14 and inter-position of unrelated callbacks are
# implementation detail; only the cross-component NB-C4 invariant is frozen).
# ---------------------------------------------------------------------------


class _ExecutionEventLog:
    """Chronological log of cleanup-callback execution events.

    ``events`` is a list of ``(component, phase)`` tuples appended at actual
    execution time — the index in this list is the time sequence number.
    """

    def __init__(self):
        self.events = []

    def record(self, component: str, phase: str) -> None:
        self.events.append((component, phase))

    def __repr__(self):  # pragma: no cover - assertion debug aid
        return f"_ExecutionEventLog({self.events!r})"


class _FlushTaskTracker:
    """Holds the REAL flush task started by the test, plus two done-state
    snapshots proving completion:

    * ``stop_exit_snapshots`` — (stop-path, task.done()) recorded when each
      wrapped token-hub stop path RETURNS. This is the scheduling-proof
      lock: a sync ``stop()`` that only calls ``task.cancel()`` cannot have
      a done task at its synchronous return (cancellation needs at least
      one loop tick to be processed), while an awaited shutdown path
      returns strictly after the task finished. Unlike the close-enter
      snapshot this cannot be faked by unrelated later callbacks reaping
      the task.
    * ``task_done_at_close_enter`` — recorded by the wrapped ``hubs.close``
      at ENTRY; the end-to-end NB-C4 state (still coherent-registry).

    ``task`` is the Task object captured right after ``start()`` — a strong
    reference, so it stays inspectable after ``_flush_task`` is cleared.
    """

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.task_done_at_close_enter: bool | None = None
        self.stop_exit_snapshots: list[tuple[str, bool]] = []

    def capture(self, hub) -> None:
        assert hub._flush_task is not None, "flush task must be running"
        assert not hub._flush_task.done(), "flush task must be live when captured"
        self.task = hub._flush_task

    def _snapshot_done(self) -> bool:
        return self.task.done() if self.task is not None else False


def _instrument_nb_c4_components(
    monkeypatch, log, tracker=None, start_flush_on_construct=False
):
    """Wrap the REAL HubRegistry/TokenStreamHub instances' cleanup methods.

    Patches the constructor symbols bound in ``oc_slimapi.app`` with
    factories that build the genuine object and then wrap ONLY the instance
    cleanup attributes: ``close`` (HubRegistry, async), ``stop``
    (TokenStreamHub, sync — last-detach API) and, when the hub provides it,
    ``stop_and_wait`` (the awaitable app-shutdown path). Everything else on
    the objects — including the AsyncExitStack the lifespan uses — is
    untouched, so the recorded order reflects how the production stack
    actually unwinds.

    ``tracker``: when given, the wrapped ``hubs.close`` snapshots
    ``tracker.task.done()`` at ENTRY (completion proof), and
    ``start_flush_on_construct=True`` test-only starts the flush loop in
    the factory (startup-failure path never reaches yield, so production
    startup behaviour is NOT changed — production starts on first
    subscriber-attach).
    """
    from oc_slimapi.sse.hub import HubRegistry
    from oc_slimapi.sse.token_hub import TokenStreamHub

    def _hub_registry_factory(*args, **kwargs):
        registry = HubRegistry(*args, **kwargs)
        real_close = registry.close

        async def _close():
            log.record("hubs.close", "enter")
            if tracker is not None:
                tracker.task_done_at_close_enter = (
                    tracker.task.done() if tracker.task is not None else None
                )
            try:
                return await real_close()
            finally:
                log.record("hubs.close", "exit")

        registry.close = _close
        return registry

    def _token_hub_factory(*args, **kwargs):
        hub = TokenStreamHub(*args, **kwargs)
        if start_flush_on_construct:
            hub.start()
            tracker.capture(hub)
        real_stop = hub.stop

        def _stop():
            log.record("token_hub.stop", "enter")
            try:
                return real_stop()
            finally:
                if tracker is not None:
                    tracker.stop_exit_snapshots.append(
                        ("sync-stop", tracker._snapshot_done())
                    )
                log.record("token_hub.stop", "exit")

        hub.stop = _stop
        # Awaitable shutdown path (absent in pre-P2-2 code — getattr so the
        # same instrumentation works in both RED and GREEN phases; wraps the
        # method app shutdown actually calls).
        real_stop_and_wait = getattr(hub, "stop_and_wait", None)
        if real_stop_and_wait is not None:
            async def _stop_and_wait():
                log.record("token_hub.stop", "enter")
                try:
                    return await real_stop_and_wait()
                finally:
                    if tracker is not None:
                        tracker.stop_exit_snapshots.append(
                            ("stop_and_wait", tracker._snapshot_done())
                        )
                    log.record("token_hub.stop", "exit")

            hub.stop_and_wait = _stop_and_wait
        return hub

    monkeypatch.setattr("oc_slimapi.app.HubRegistry", _hub_registry_factory)
    monkeypatch.setattr("oc_slimapi.app.TokenStreamHub", _token_hub_factory)


def _assert_nb_c4(log, tracker=None):
    """Strict partial order: token_hub.stop EXIT < hubs.close ENTER, and
    (when a tracker is given) the flush task was already DONE at
    hubs.close() entry.

    Uses max()/min() over all recorded invocations so the assertion stays
    correct even if a last-detach runtime ``stop()`` (subscriber.py:622)
    ever fires — with zero SSE subscribers in these tests there is exactly
    one of each event.
    """
    stop_exits = [
        i for i, (comp, phase) in enumerate(log.events)
        if comp == "token_hub.stop" and phase == "exit"
    ]
    close_enters = [
        i for i, (comp, phase) in enumerate(log.events)
        if comp == "hubs.close" and phase == "enter"
    ]
    # Non-vacuous: both cleanup callbacks must have actually executed.
    assert stop_exits, f"token_hub.stop never ran; events={log.events!r}"
    assert close_enters, f"hubs.close never ran; events={log.events!r}"
    assert max(stop_exits) < min(close_enters), (
        "NB-C4 violated: token_hub.stop() must fully complete before "
        f"hubs.close() starts; events={log.events!r}"
    )
    if tracker is not None:
        # P2-2 completion lock, part 1 (scheduling-proof): at the moment
        # every token-hub stop path RETURNED, the flush task must already
        # be done. A stop that only requests cancellation (sync stop()
        # without awaiting) deterministically fails here — the cancel needs
        # a loop tick to be processed, which cannot happen before a
        # synchronous return. This snapshot cannot be faked by later
        # callbacks reaping the task.
        assert tracker.stop_exit_snapshots, (
            "no token-hub stop path executed; snapshots="
            f"{tracker.stop_exit_snapshots!r}"
        )
        assert all(done for _path, done in tracker.stop_exit_snapshots), (
            "NB-C4 violated: token-hub shutdown returned while the flush "
            "task was still running — shutdown must AWAIT flush-task "
            f"completion, not merely request cancellation; snapshots="
            f"{tracker.stop_exit_snapshots!r}"
        )
        # P2-2 completion lock, part 2 (end-to-end): the flush task must
        # still be done at the MOMENT hubs.close() begins.
        assert tracker.task is not None, "flush task was never started"
        assert tracker.task_done_at_close_enter is True, (
            "NB-C4 violated: the flush task was still running when "
            "hubs.close() began — shutdown must AWAIT flush-task "
            "completion, not merely request cancellation "
            f"(done_at_close_enter={tracker.task_done_at_close_enter})"
        )


def _patch_nb_c4_base(monkeypatch, tmp_path, smoke_impl):
    """Common plumbing: settings + smoke + instant-fail upstream client."""
    test_settings = _test_settings(tmp_path)
    monkeypatch.setattr("oc_slimapi.app.settings", test_settings)
    monkeypatch.setattr("oc_slimapi.app.smoke", smoke_impl)

    # Mock upstream client that fails instantly (no real socket, no 5s wait).
    def _mock_client(settings):
        return httpx.AsyncClient(
            base_url=settings.upstream,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("no upstream"))
            ),
        )

    monkeypatch.setattr("oc_slimapi.app.create_client", _mock_client)


async def test_nb_c4_token_hub_stops_before_hubs_close_normal_shutdown(
    monkeypatch, tmp_path
):
    """NB-C4 on normal shutdown: the exit-stack unwind (LIFO) must fully
    stop the token hub — flush task cancellation processed and awaited to
    completion — BEFORE hubs.close() begins."""

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False

    log = _ExecutionEventLog()
    tracker = _FlushTaskTracker()
    _instrument_nb_c4_components(monkeypatch, log, tracker=tracker)
    _patch_nb_c4_base(monkeypatch, tmp_path, _noop_smoke)

    app = FastAPI()
    async with lifespan(app):
        # Both components are live while inside yield.
        assert app.state.token_hub is not None
        assert app.state.hubs is not None
        assert not log.events  # no cleanup ran before shutdown began
        # P2-2: start the REAL flush task inside the yield (production
        # starts it on first subscriber-attach; these tests have none) and
        # prove it is live — the shutdown path must await its exit before
        # hubs.close() runs.
        app.state.token_hub.start()
        tracker.capture(app.state.token_hub)
        assert tracker.task is not None and not tracker.task.done()

    _assert_nb_c4(log, tracker)


async def test_nb_c4_token_hub_stops_before_hubs_close_startup_failure(
    monkeypatch, tmp_path
):
    """NB-C4 on startup failure: smoke() raises AFTER both token_hub (:598)
    and hubs (:516) are constructed and their cleanups registered (:611/:552),
    so the transactional unwind must still fully stop the token hub before
    hubs.close(). (NB-C4 is fully reachable on this path — no fallback
    assertion needed.)

    The yield is unreachable here, so the REAL flush task is test-only
    started inside the construction factory (``start_flush_on_construct``) —
    production startup behaviour is unchanged."""

    async def _boom(app):
        raise RuntimeError("injected startup failure")

    log = _ExecutionEventLog()
    tracker = _FlushTaskTracker()
    _instrument_nb_c4_components(
        monkeypatch, log, tracker=tracker, start_flush_on_construct=True
    )
    _patch_nb_c4_base(monkeypatch, tmp_path, _boom)

    app = FastAPI()
    with pytest.raises(RuntimeError, match="injected startup failure"):
        async with lifespan(app):
            pytest.fail("yield body must not be reached on startup failure")

    # Non-vacuous: the factory really had a live flush task running when
    # the unwind began.
    assert tracker.task is not None
    _assert_nb_c4(log, tracker)


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
