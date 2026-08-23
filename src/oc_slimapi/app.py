import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date

from fastapi import FastAPI
import uvicorn

from . import __version__
from .access_log import (
    compress_old_access_logs,
    get_access_logger,
    migrate_legacy_access_log,
    prune_old_access_logs,
    run_access_log_maintenance_loop,
    setup_access_log,
)
from .actions import load_registry as actions_load_registry
from .catalog_cache import CatalogCache
from .config import settings
from .dbaux import DbAuxiliarySource, resolve_db_path
from .errors import register_error_handlers
from .logging_config import get_logger, setup_logging
from .middleware.request_id import RequestIdMiddleware
from .middleware.traffic_accounting import TrafficAccountingMiddleware
from .proxy import install_proxy
from .qp_sweep import QpSweepShadow
from .routes import actions, agent, children, command, directories, events, file_raw, health, messages, metrics, permissions, questions, read_groups, sessions, todo, token_stream, versions, write_groups
from .routes import diff as diff_routes
from .selector import SlimapiSelectorMiddleware
from .since_cache import SinceCache
from .singleflight import LeasedSingleFlight, fulls
from .sse.hub import HubRegistry
from .sse.replay_log import ReplayLog, new_epoch
from .sse.replay_wire import replay_sweep_loop
from .sse.token_hub import TokenStreamHub, TokenStreamRegistry
from .sse.tokenstream.hub import apply_debug_budget_overrides
from .traffic import TrafficLedger
from .traffic_snapshot import TrafficSnapshotter
from .transform import TransformConfig, TransformPool
from .upstream import create_client


# ---------------------------------------------------------------------------
# Smoke-probe status (P1-36): distinguishes *why* smoke could not validate the
# upstream message schema, so health/ready diagnostics can tell apart "upstream
# is down" (upstream_unavailable) from "upstream is up but the shape changed"
# (invalid_schema). ``schema_degraded`` is True ONLY for ``invalid_schema`` —
# an unreachable upstream is not a schema regression.
# ---------------------------------------------------------------------------
SMOKE_NOT_RUN = "not_run"
SMOKE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
SMOKE_INVALID_SCHEMA = "invalid_schema"
SMOKE_VALID = "valid"

# Short read timeout for startup smoke + health probes (P1-37). The default
# httpx client timeout is 30 s — an unreachable upstream would stall systemd
# readiness / hot reload for multiple 30 s windows. 5 s aligns with the
# routes' /ready health-check timeout. Failure is tolerated (non-blocking),
# but non-blocking ≠ long-timeout.
_SMOKE_TIMEOUT = 5.0

# Graceful drain timeout for the access-log maintenance task on shutdown
# (P1-38). The maintenance loop dispatches gzip/prune work via
# asyncio.to_thread; setting stop_event lets the loop finish its current
# to_thread then exit cleanly. This timeout bounds how long we wait for
# that graceful drain before forcing a cancel. A running to_thread thread
# cannot be safely cancelled — it finishes on its own in the thread pool
# (bounded gzip work + per-operation _MAINT_LOCK in access_log.py).
_MAINT_DRAIN_TIMEOUT = 30.0

# Graceful drain timeout for the transform pool on shutdown (P1-41). The
# pool's shutdown() cancels pending futures and waits for in-flight workers
# bounded by this timeout. A running transform (large gzip / pathological
# input) that doesn't finish within this window is abandoned (the daemon
# drain thread continues in the background). 10s aligns with the typical
# uvicorn graceful-shutdown window so a hot reload / systemd stop is not
# stalled by a single slow worker.
_TRANSFORM_DRAIN_TIMEOUT = 10.0

# B3a-B1: bounded drain for the dbaux single-worker executor on shutdown.
# Shorter than the transform pool — dbaux queries are read-only projections
# with a 5s busy_timeout ceiling, so 5s covers the worst legitimate case
# without stalling the uvicorn graceful-shutdown window.
_DBAUX_DRAIN_TIMEOUT = 5.0

# B3b-2: replay-log maintenance cadence — TTL GC + barrier GC + expired
# token-domain recycle (design-v4-sse-replay §3.4). 60s keeps expired-frame
# memory bounded without measurable sweep cost; the TTL itself (15 min
# default) is the wire-visible window, this is purely bookkeeping cadence.
_REPLAY_SWEEP_INTERVAL_S = 60.0

# Graceful shutdown timeout for uvicorn's active-connection (SSE) drain
# (P0-1). uvicorn waits this long for active connections to finish before
# forcing close. This is only the FIRST leg of the SIGTERM chain: the
# lifespan AsyncExitStack LIFO cleanups after it need up to ~45s more
# (see the drain constants above; F-010/F-214), which is why systemd's
# TimeoutStopSec=60 in deploy/oc-slimapi.service covers the full chain.
_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0


def _log_maint_task_exception(task: asyncio.Task) -> None:
    """Log an unobserved exception from a finished maintenance task (P1-38).

    Without this, a task that died with an unhandled exception (a bug in the
    loop's own code — the loop itself catches compress/prune exceptions) would
    leave the exception unobserved until GC. Calling ``task.exception()`` marks
    it as consumed.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        get_logger("app").warning(
            "access log maintenance task exited with error", exc_info=exc
        )


def _log_directory_allowlist(settings) -> None:
    if settings.directory_allowlist == []:
        get_logger("app").warning(
            "directory allowlist enabled but empty; /slimapi/file/** will 403"
        )
    elif settings.directory_allowlist is not None:
        get_logger("app").info(
            "directory allowlist enabled with %d entries",
            len(settings.directory_allowlist),
        )


async def smoke(app: FastAPI) -> None:
    """Validate the upstream message-list schema and record a diagnostic status.

    Sets ``app.state.smoke_status`` to one of the ``SMOKE_*`` constants and
    ``app.state.schema_degraded`` to ``True`` **only** when the upstream
    responded with a parseable body whose field shape does not match the
    expected message schema (``invalid_schema``). Connection errors / timeouts
    / non-2xx status → ``upstream_unavailable`` (schema_degraded stays False).
    """
    sid = app.state.config.smoke_session_id
    if not sid:
        try:
            sessions = (
                await app.state.upstream.get(
                    "/session", params={"limit": 1}, timeout=_SMOKE_TIMEOUT
                )
            ).json()
            sid = sessions[0].get("id") if sessions else None
        except Exception as exc:
            get_logger("app").warning("smoke: failed to fetch sessions list", exc_info=exc)
            app.state.smoke_status = SMOKE_UPSTREAM_UNAVAILABLE
            app.state.schema_degraded = False
            return
        if not sid:
            app.state.smoke_status = SMOKE_NOT_RUN
            app.state.schema_degraded = False
            return
    try:
        response = await app.state.upstream.get(
            f"/session/{sid}/message", params={"limit": 1}, timeout=_SMOKE_TIMEOUT
        )
        payload = response.json()
    except Exception as exc:
        get_logger("app").warning("smoke: upstream unavailable", exc_info=exc)
        app.state.smoke_status = SMOKE_UPSTREAM_UNAVAILABLE
        app.state.schema_degraded = False
        return
    # Upstream responded + body parsed. A non-2xx status means the endpoint
    # is not usable (404 = session gone, 5xx = upstream broken) — this is an
    # upstream-availability issue, not a schema regression.
    if response.status_code >= 300:
        get_logger("app").warning(
            "smoke: upstream returned status %s for session %s",
            response.status_code, sid,
        )
        app.state.smoke_status = SMOKE_UPSTREAM_UNAVAILABLE
        app.state.schema_degraded = False
        return
    valid = isinstance(payload, list)
    if payload:
        valid = valid and isinstance(payload[0].get("info", {}).get("id"), str)
        valid = valid and all(isinstance(part.get("type"), str) for part in payload[0].get("parts", []))
    if valid:
        app.state.smoke_status = SMOKE_VALID
        app.state.schema_degraded = False
    else:
        app.state.smoke_status = SMOKE_INVALID_SCHEMA
        app.state.schema_degraded = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application logging (stderr StreamHandler on the oc_slimapi root logger).
    # Must come before any other logging setup so sub-loggers see the level /
    # handler from the start.  Also before settings.validate() so config errors
    # are also logged.
    setup_logging()
    settings.validate()
    app.state.config = settings
    _log_directory_allowlist(settings)
    # Structured access log (DailyAccessHandler on the oc_slimapi.access
    # logger). Files are named access-YYYY-MM-DD.jsonl under ``access_log_dir``,
    # rotated by calendar day; startup compresses history and a background loop
    # re-runs compress+prune. Best-effort — a failure degrades to a disabled
    # logger and NEVER crashes lifespan (traffic-log-persistence task-2 阻断2).
    # Backward-compat + priority clarity (P1-34): the deprecated
    # OC_SLIMAPI_ACCESS_LOG_PATH gets a say ONLY when the new
    # OC_SLIMAPI_ACCESS_LOG_DIR is left unset (at default). An explicitly-set
    # new dir always wins. The env-source check lives in Settings so the
    # lifespan body stays declarative.
    access_log_dir, used_deprecated = settings.effective_access_log_dir()
    if used_deprecated:
        get_logger("app").warning(
            "OC_SLIMAPI_ACCESS_LOG_PATH is deprecated (ignored); using its "
            "parent dir %r — set OC_SLIMAPI_ACCESS_LOG_DIR explicitly",
            access_log_dir,
        )
    # ------------------------------------------------------------------
    # P0-1: transactional startup via AsyncExitStack.  Every resource that
    # needs cleanup registers its cleanup callback IMMEDIATELY after creation.
    # The stack exits in LIFO order on BOTH normal shutdown AND startup-failure
    # (exception before yield) — so a partial startup cannot leak an httpx
    # client, executor, hub task, or maintenance task.  Each callback is
    # individually try/except-isolated so one failure does NOT skip the others
    # (mirrors the per-component convergence the old finally block enforced).
    #
    # Registration order (and thus reverse-cleanup order) — all 14
    # exit-stack registrations, anchored by cleanup-callback name (line
    # numbers are the v4.12.0 / HEAD a4cc717 snapshot and WILL drift as
    # this file edits; the names are the stable anchor):
    #
    #   1. access-log handlers    _close_access_log_handlers    (callback)
    #   2. snapshotter            _stop_snapshotter             (push_async_callback)
    #   3. upstream client        _aclose_upstream              (push_async_callback)
    #   4. transform pool         _shutdown_transforms          (callback)
    #   5. single-flight fulls    _shutdown_fulls               (callback)
    #   6. catalog cache          _shutdown_catalog_cache       (callback)
    #   7. raw-fetch registry     _shutdown_raw_fetch_registry  (callback; only when coalesce_enabled)
    #   8. replay log             _close_replay_log             (callback)
    #   9. replay sweep task      _stop_replay_sweep            (push_async_callback)
    #  10. hub registry           _close_hubs                   (push_async_callback)
    #  11. qp sweep shadow        _stop_qp_sweep                (push_async_callback; only when qp_sweep_enabled)
    #  12. token hub              _stop_token_hub               (callback; registered AFTER hubs — NB-C4)
    #  13. dbaux source           _stop_dbaux                   (push_async_callback)
    #  14. access-log maintenance _stop_maintenance             (push_async_callback; only when access_log_active)
    #
    # Cleanup runs in exact LIFO: maintenance → dbaux → token_hub →
    # qp_sweep → hubs → replay_sweep → replay_log → raw_fetch_registry →
    # catalog_cache → fulls → transforms → upstream → snapshotter →
    # access-log handlers. The three conditional registrations (7/11/14)
    # drop out when their gate is off; the relative order of the rest is
    # unchanged.
    # The only ordering constraint with cross-component semantics is
    # token_hub.stop() BEFORE hubs.close() (NB-C4: flush loop must drain
    # while the registry / hub are still coherent); that is satisfied by
    # registering token_hub AFTER hubs (#12 > #10). Each callback is
    # individually try/except-isolated so one failure does NOT skip the
    # others (mirrors the per-component convergence of the old finally).
    # ------------------------------------------------------------------
    async with AsyncExitStack() as stack:
        access_logger = setup_access_log(
            enabled=settings.access_log_enabled, dir=access_log_dir
        )
        # P1-39: gate maintenance on the ACTUAL install result, not the config
        # flag. setup_access_log is best-effort — if the directory is not
        # writable, it disables the logger (logger.disabled = True). Gating
        # on settings.access_log_enabled alone would start a maintenance loop
        # that repeatedly hits a failed directory (wasted IO + noise). Only
        # run maintenance when the handler is actually live.
        access_log_active = settings.access_log_enabled and not access_logger.disabled
        if settings.access_log_enabled and not access_log_active:
            get_logger("app").warning(
                "access log enabled in config but handler install failed; "
                "maintenance loop suppressed"
            )
        # Close the access-log DailyAccessHandler so file handles flush +
        # release on graceful shutdown (re-init removes handlers, but a clean
        # lifespan shutdown should not rely on interpreter GC — 终审重要项).
        def _close_access_log_handlers():
            try:
                access_logger = get_access_logger()
                for h in list(access_logger.handlers):
                    try:
                        h.close()
                    except Exception:
                        pass
                    access_logger.removeHandler(h)
            except Exception:
                pass
        stack.callback(_close_access_log_handlers)
        # Daily-rotation maintenance at startup (best-effort): migrate any
        # legacy RotatingFileHandler files, compress non-toay history, prune
        # by retain.
        if access_log_active:
            try:
                migrate_legacy_access_log(access_log_dir)
                if settings.access_log_compress_on_startup:
                    compress_old_access_logs(access_log_dir, date.today())
                prune_old_access_logs(
                    access_log_dir, settings.access_log_retain_days, date.today()
                )
            except Exception as exc:
                get_logger("app").warning(
                    "access-log startup maintenance failed", exc_info=exc
                )
        # Full bidirectional byte ledger (traffic accounting). Single shared
        # instance — read by the ASGI middleware (down/up per-request), the SSE
        # generators (SSE downstream per-frame), and GlobalHub.run (SSE upstream
        # per-line). When disabled, record_* are no-ops and snapshot reports
        # ``{"enabled": False}``.
        app.state.traffic_ledger = TrafficLedger(enabled=settings.traffic_metrics_enabled)
        # Periodic cumulative snapshot of the in-memory ledger — the ONLY
        # persistent source for real SSE upstream cost (lost on restart without
        # this). Writes a total snapshot per tick; deltas are derived at
        # analysis time. Best-effort: start/stop failures warn + degrade.
        app.state.traffic_snapshotter = TrafficSnapshotter(
            ledger=app.state.traffic_ledger,
            interval_s=settings.traffic_snapshot_interval_s,
            path=settings.traffic_snapshot_path,
            # F-009: retention window for daily snapshot files. The
            # snapshotter's own loop prunes expired files at the top of
            # every tick — self-managed, independent of ACCESS_LOG_ENABLED.
            retain_days=settings.traffic_snapshot_retain_days,
        )

        async def _stop_snapshotter():
            try:
                await app.state.traffic_snapshotter.stop()
            except Exception as exc:
                get_logger("app").warning(
                    "final traffic snapshot failed", exc_info=exc
                )
        stack.push_async_callback(_stop_snapshotter)
        # Debug/联调-only: override token-stream memory budget caps from env
        # (OC_SLIMAPI_TOKEN_STREAM_DEBUG_*). No-op when env vars are unset.
        apply_debug_budget_overrides(settings)
        app.state.upstream = create_client(settings)

        async def _aclose_upstream():
            try:
                await app.state.upstream.aclose()
            except Exception as exc:
                get_logger("app").warning("upstream.aclose failed", exc_info=exc)
        stack.push_async_callback(_aclose_upstream)
        # Transform pool: admission semaphore + bounded worker executor, both
        # sized by OC_SLIMAPI_MAX_TRANSFORMS. Acquired by the skeleton routes
        # *before* their upstream GET so memory pressure stays bounded and the
        # parse/project/serialize/gzip chain never runs on this event loop.
        app.state.transforms = TransformPool(TransformConfig(
            max_transforms=settings.max_transforms,
            transform_wait_seconds=settings.transform_wait_seconds,
            max_response_bytes=settings.max_response_bytes,
        ))

        def _shutdown_transforms():
            # P1-41: drain in-flight transforms bounded by a timeout so a
            # hot reload does not yank a worker out from under an active
            # gzip, but also does not stall the event loop past the uvicorn
            # graceful-shutdown window if a worker is stuck.
            try:
                app.state.transforms.shutdown(
                    wait_seconds=_TRANSFORM_DRAIN_TIMEOUT,
                )
            except Exception as exc:
                get_logger("app").warning("transforms.shutdown failed", exc_info=exc)
        stack.callback(_shutdown_transforms)
        # rev-9 (L2-CD): converge the process-level single-flight registry
        # (direct /full + merged fan-out shared upstream GETs) — cancel the
        # pending grace-expiry timers and drop retained bodies so a stopped
        # app leaves no stale app-domain bytes or timer callbacks behind.
        # Registered AFTER transforms → LIFO cleanup runs BEFORE
        # transforms.shutdown and upstream.aclose: in-flight fetches may
        # still be awaiting upstream GETs, so the registry must converge
        # while the upstream client is still open (its consumers sit
        # "inside" the transform pool, which drains afterwards). Isolated
        # try/except so a failure here cannot break the rest of the chain.
        def _shutdown_fulls():
            try:
                fulls.shutdown()
            except Exception as exc:
                get_logger("app").warning(
                    "singleflight shutdown failed", exc_info=exc
                )
        stack.callback(_shutdown_fulls)
        # Traffic plan Batch 1 / A1: catalog TTL body cache for
        # /slimapi/agent + /slimapi/command. Holds its own plain SingleFlight
        # for refresh-stampede protection. Teardown converges both (clears
        # retained bodies + cancels grace timers) while upstream is still
        # open — same LIFO rationale as _shutdown_fulls above.
        app.state.catalog_cache = CatalogCache(
            ttl_seconds=settings.catalog_cache_ttl_seconds,
            max_entries=settings.catalog_cache_max_entries,
            max_bytes=settings.catalog_cache_max_bytes,
            max_entry_bytes=settings.catalog_cache_max_entry_bytes,
        )

        # Phase B: process-local single-snapshot lineage for messages
        # ``?since=``.  The cache is deliberately independent from the raw
        # fetch registry: since projection/diff work remains per request after
        # a shared upstream flight completes.
        app.state.since_cache = SinceCache(
            enabled=settings.since_cache_enabled,
            max_entries=settings.since_cache_max_entries,
            max_bytes=settings.since_cache_max_bytes,
            max_entry_bytes=settings.since_cache_max_entry_bytes,
        )

        def _shutdown_catalog_cache():
            try:
                app.state.catalog_cache.shutdown()
            except Exception as exc:
                get_logger("app").warning(
                    "catalog cache shutdown failed", exc_info=exc
                )
        stack.callback(_shutdown_catalog_cache)

        # Traffic plan Batch 1 / A2-A4: per-app raw-fetch coalescing registry
        # (LeasedSingleFlight) for list-route upstream GETs. Attached only
        # when enabled — ``coalesce_enabled=false`` leaves the attribute
        # absent so routes take the unchanged direct path. Teardown calls
        # shutdown() (clears active entries, cancels grace timers; detached
        # in-flight entries converge in the retired layer) while upstream is
        # still open — same LIFO rationale as _shutdown_fulls above.
        if settings.coalesce_enabled:
            app.state.raw_fetch_registry = LeasedSingleFlight(
                max_bytes=settings.raw_fetch_max_bytes,
                network_concurrency=settings.raw_fetch_concurrency,
            )

            def _shutdown_raw_fetch_registry():
                try:
                    app.state.raw_fetch_registry.shutdown()
                except Exception as exc:
                    get_logger("app").warning(
                        "raw fetch registry shutdown failed", exc_info=exc
                    )
            stack.callback(_shutdown_raw_fetch_registry)
        app.state.schema_degraded = False
        # Questions fan-out semaphore (T5): global per-request /question
        # concurrency cap. Sized by config (1..16); no close/cleanup needed.
        app.state.questions_semaphore = asyncio.Semaphore(
            settings.questions_fanout_concurrency
        )
        # Permissions fan-out semaphore (L2-B): global per-request /permission
        # concurrency cap. Mirrors the questions semaphore; sized by config
        # (1..16); no close/cleanup needed.
        app.state.permissions_semaphore = asyncio.Semaphore(
            settings.permissions_fanout
        )
        # P1-36: smoke status defaults to not_run; smoke() updates it.
        app.state.smoke_status = SMOKE_NOT_RUN
        # S-E: best-effort deployment revision (env or file, swallow errors)
        try:
            app.state.deployment_revision = settings.read_deployment_revision()
        except Exception as exc:
            get_logger("app").warning("failed to read deployment revision", exc_info=exc)
            app.state.deployment_revision = None
        # /slimapi/actions: configuration-driven admin actions (spec §5).
        # Best-effort manifest load — mirrors the access-log pattern: an unset
        # / missing / unreadable / invalid manifest disables the feature with a
        # WARNING/ERROR log and NEVER crashes lifespan. ``load_registry`` does
        # not raise; the async Semaphore it constructs binds lazily to the
        # running loop, which is always live here (lifespan).
        app.state.actions_registry = actions_load_registry(settings)
        app.state.replay_epoch = new_epoch()
        app.state.replay_log = ReplayLog(
            epoch=app.state.replay_epoch,
            max_count=settings.replay_max_count,
            max_bytes=settings.replay_max_bytes_kb * 1024,
            ttl_s=settings.replay_ttl_s,
        )

        def _close_replay_log():
            # No background tasks / file handles — close() just releases
            # retained frames deterministically (mirrors the dbaux/
            # token_hub best-effort teardown pattern).
            try:
                app.state.replay_log.close()
            except Exception as exc:  # noqa: BLE001 — best-effort
                get_logger("app").warning("replay_log.close failed", exc_info=exc)
        stack.callback(_close_replay_log)
        # B3b-2: periodic replay-log maintenance (TTL GC + barrier GC +
        # expired token-domain recycle — design-v4-sse-replay §3.4). The
        # sweep task stops BEFORE replay_log.close() (LIFO: this cleanup is
        # registered after _close_replay_log) and is force-cancelled if it
        # does not drain within the grace window.
        replay_sweep_stop = asyncio.Event()
        replay_sweep_task = asyncio.create_task(
            replay_sweep_loop(
                app.state.replay_log,
                interval_s=_REPLAY_SWEEP_INTERVAL_S,
                stop_event=replay_sweep_stop,
            )
        )
        app.state._replay_sweep_task = replay_sweep_task

        async def _stop_replay_sweep():
            replay_sweep_stop.set()
            if replay_sweep_task.done():
                return
            replay_sweep_task.cancel()
            try:
                await replay_sweep_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — best-effort
                get_logger("app").warning(
                    "replay sweep task error during shutdown", exc_info=exc
                )
        stack.push_async_callback(_stop_replay_sweep)
        # T3-hardened hub registry (contract §6): per-subscriber byte budget,
        # per-frame ceiling, and per-directory / total admission caps all flow
        # in from Settings so an operator can tune them via env without code.
        # ``traffic_ledger`` is forwarded onto the lazily-created GlobalHub so
        # SSE upstream bytes (single shared /global/event) are counted exactly
        # once at the upstream consume site (run()).
        app.state.hubs = HubRegistry(
            app.state.upstream,
            max_subscribers_per_directory=settings.max_subscribers_per_directory,
            max_total_subscribers=settings.max_total_subscribers,
            queue_items=settings.sse_queue_items,
            buffer_bytes=settings.sse_buffer_bytes,
            max_frame_bytes=settings.sse_max_frame_bytes,
            traffic_ledger=app.state.traffic_ledger,
        )
        # B3b-2: share the process replay log with every lazily-created
        # GlobalHub — business frames append to the global domain (v4
        # subscribers get id-stamped deliveries) and confirmed upstream
        # loss writes cross-domain barriers (design-v4-sse-replay §3.4).
        app.state.hubs.set_replay_log(app.state.replay_log)
        # 4.10.1 (B): deterministic catalog-cache invalidation on upstream
        # epoch loss — the hub's canonical once-per-epoch upstream-loss hook
        # (_notify_upstream_loss) fires this callback, so a restarted
        # opencode does not serve stale catalog bodies for up to the
        # remaining TTL (default 300s); staleness shrinks to ≤ one SSE
        # reconnect period. Ordering: catalog_cache is constructed earlier
        # in lifespan, so the bound method always exists before any hub
        # run-loop can fire it. Best-effort: callback failure is swallowed
        # with a warning in the hub; invalidate() keeps the cache fully
        # operational (single-flight untouched, TTL unchanged).
        app.state.hubs.add_upstream_loss_callback(
            app.state.catalog_cache.invalidate
        )
        app.state.hubs.get_global().set_directory_allowlist(
            settings.directory_allowlist
        )

        async def _close_hubs():
            try:
                await app.state.hubs.close()
            except Exception as exc:
                get_logger("app").warning("hubs.close failed", exc_info=exc)
        stack.push_async_callback(_close_hubs)
        if settings.qp_sweep_enabled:
            global_hub = app.state.hubs.get_global()

            app.state.qp_sweep = QpSweepShadow(
                activity=global_hub.qp_last_activity,
                interval_seconds=settings.qp_sweep_interval_seconds,
                daily_budget=settings.qp_sweep_daily_budget,
            )
            # Hub ingest observes every valid event directory synchronously;
            # this avoids losing non-q/p directories between pending flushes.
            global_hub.set_directory_observer(app.state.qp_sweep.observe_directory)
            app.state.qp_sweep.start()

            async def _stop_qp_sweep():
                # F-007: exception-isolated like every other exit-stack
                # callback — a failing qp sweep stop must not skip the
                # LIFO cleanups registered after this one.
                try:
                    await app.state.qp_sweep.stop()
                except Exception as exc:
                    get_logger("app").warning(
                        "qp sweep stop failed", exc_info=exc
                    )

            stack.push_async_callback(_stop_qp_sweep)
            get_logger("app").info(
                "qp sweep shadow enabled (interval=%ss, budget=%s/day); "
                "zero real upstream requests",
                settings.qp_sweep_interval_seconds,
                settings.qp_sweep_daily_budget,
            )
        else:
            app.state.qp_sweep = None
        # Token-stream accumulator (design-token-stream.md §5.3). Constructed
        # unconditionally so publish() can route message.part.delta/updated
        # into it the moment the first upstream event arrives; subscriber /
        # flush / fan-out wiring lands in Stage B/C/D. Independent ledger —
        # does NOT consume MAX_TOTAL_SUBSCRIBERS.
        # INV-5 (P1-17): pass max_frame_bytes from Settings so the hub's
        # snapshot/truncation ceiling matches the subscriber's oversized
        # ceiling (same source: settings.token_stream_max_frame_bytes).
        # B3b-2: the token hub shares the process replay log — live-fanout
        # delta/done-marker/truncated frames and message.removed tombstones
        # append to the sid's token domain; v4 subscribers get id-stamped
        # deliveries (design-v4-sse-replay §3.4).
        app.state.token_hub = TokenStreamHub(
            max_frame_bytes=settings.token_stream_max_frame_bytes,
            replay_log=app.state.replay_log,
        )

        def _stop_token_hub():
            try:
                app.state.token_hub.stop()
            except Exception as exc:
                get_logger("app").warning("token_hub.stop failed", exc_info=exc)
        # NB-C4: registered AFTER hubs so LIFO cleanup runs token_hub.stop()
        # BEFORE hubs.close() (flush loop must drain while the registry stays
        # coherent).
        stack.callback(_stop_token_hub)
        app.state.hubs.set_token_hub(app.state.token_hub)
        # Stage D (design-token-stream.md §5.1 / §6): independent admission
        # ledger for token-stream subscribers. Own cap
        # (token_stream_max_subscribers) — does NOT consume
        # MAX_TOTAL_SUBSCRIBERS. Holds a back-reference to HubRegistry so
        # subscribe() can ensure_upstream() + cancel a pending grace-removal
        # (NB-B1). The flush loop is started on first-attach / stopped on
        # last-detach (NB-C4); see TokenStreamRegistry.subscribe/unsubscribe.
        app.state.token_registry = TokenStreamRegistry(
            app.state.token_hub,
            app.state.hubs,
            max_subscribers=settings.token_stream_max_subscribers,
            queue_items=settings.token_stream_queue_items,
            buffer_bytes=settings.token_stream_buffer_bytes,
            max_frame_bytes=settings.token_stream_max_frame_bytes,
        )
        # Wire the transform pool into the registry so /slimapi/metrics can
        # report activeTransforms / waitingTransforms without the hub module
        # importing transform.py (would be a circular import via skeleton.py).
        app.state.hubs.set_transforms(app.state.transforms)
        # S5+S2: turn token fence — incarnation persistence (strategy A) +
        # in-process turn registry. Scope is the sid alone (single sidecar +
        # single opencode backend → sid is globally unique); no header gate.
        # Reuses the access_log dir as the state dir for the single incarnation
        # file (best-effort persistence; corrupt/unwritable → fallback
        # incarnation, never crashes lifespan). Injected onto the global hub so
        # publish() can stamp turnIncarnation/turn onto session.digest at ingest
        # time (S9).
        from .turn_registry import IncarnationStore, TurnRegistry

        inc_store = IncarnationStore(
            state_dir=settings.state_dir,
            legacy_state_dir=access_log_dir,
        )
        incarnation = inc_store.load_or_bump()
        app.state.turn_registry = TurnRegistry(incarnation=incarnation)
        app.state.hubs.set_turn_registry(app.state.turn_registry)
        # B3a-B1: v4 sessions DB auxiliary source — read-only projection
        # infrastructure (design-v4-dbaux §1-§6). Resolve path → mode=ro
        # open + query_only → schema gate; ANY failure disables the
        # auxiliary (never crashes lifespan — full-degradation HTTP) and
        # the shared periodic task (probe interval) retries: inode/mtime
        # checks, circuit half-open probes, disabled reprobes. The v4
        # sessions routes that consume it land in B4; until then this runs
        # as background infra whose real state surfaces via /slimapi/health
        # ``auxiliary`` (v4 view) and the B5 metrics lane.
        dbaux_resolution = resolve_db_path()
        app.state.dbaux = DbAuxiliarySource(
            dbaux_resolution,
            probe_interval_s=settings.dbaux_probe_interval_s,
        )
        await app.state.dbaux.start()

        async def _stop_dbaux():
            # Bounded drain mirrors _shutdown_transforms (P1-41 pattern):
            # in-flight worker queries finish naturally, but shutdown never
            # stalls the event loop past the drain window.
            try:
                await app.state.dbaux.stop(
                    drain_seconds=_DBAUX_DRAIN_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                get_logger("app").warning("dbaux.stop failed", exc_info=exc)
        stack.push_async_callback(_stop_dbaux)
        # B3b-1/B3b-2: v4 SSE replay log (app.state.replay_epoch +
        # app.state.replay_log + the periodic sweep task) is constructed
        # EARLIER — before the hub registry / token hub — so both can share
        # the log from their first creation (see the block above).
        await smoke(app)
        # Best-effort upstream health check (contract §4). Non-blocking; failure
        # is tolerated — the smoke() call already proved the client works, and a
        # transient upstream blip at startup does not justify failing the
        # process. P1-37: short timeout so an unreachable upstream does not
        # stall systemd readiness.
        try:
            await app.state.upstream.get("/global/health", timeout=_SMOKE_TIMEOUT)
        except Exception:
            pass
        # ------------------------------------------------------------------
        # Startup banner: log concise operational summary (redacting secrets).
        # ------------------------------------------------------------------
        logger = get_logger("app")
        logger.info(
            "oc-slimapi %s starting: host=%s port=%s upstream=%s "
            "max_transforms=%s shell_deny_list_enabled=%s "
            "token_stream_max_subscribers=%s traffic_ledger_enabled=%s "
            "access_log_dir=%s",
            __version__,
            settings.host,
            settings.port,
            settings.upstream,
            settings.max_transforms,
            settings.shell_deny_list_enabled,
            settings.token_stream_max_subscribers,
            settings.traffic_metrics_enabled,
            access_log_dir if access_log_active else "disabled",
        )
        # Start background tasks after all state is wired. The access-log
        # maintenance loop (compress+prune) runs independent of restart so a
        # long-running process still compresses history; the snapshotter writes
        # its first frame immediately then ticks on interval.
        # P1-39: gate on access_log_active (actual install result) so a failed
        # directory does not spawn a maintenance loop hitting a dead path.
        if access_log_active:
            stop_event = asyncio.Event()
            app.state._access_log_stop_event = stop_event
            # F-009: the traffic-snapshot prune no longer piggybacks on
            # this loop as ``extra_prune`` — the snapshotter's own loop
            # prunes expired daily files at the top of every tick
            # (self-managed retention, independent of ACCESS_LOG_ENABLED;
            # see TrafficSnapshotter.__init__ retain_days). This loop now
            # only handles the access-log compress+prune.
            maint_task = asyncio.create_task(
                run_access_log_maintenance_loop(
                    dir=access_log_dir,
                    retain_days=settings.access_log_retain_days,
                    interval_s=settings.access_log_maintenance_interval_s,
                    stop_event=stop_event,
                )
            )
            app.state._access_log_maintenance_task = maint_task

            async def _stop_maintenance():
                # Stop the maintenance loop first so it cannot race the
                # ledger/hub teardown below.
                stop_event.set()
                if maint_task.done():
                    _log_maint_task_exception(maint_task)
                    return
                # Graceful drain: let the loop's current to_thread (gzip/prune)
                # finish, then the loop checks stop_event and exits cleanly.
                done, _ = await asyncio.wait(
                    {maint_task}, timeout=_MAINT_DRAIN_TIMEOUT
                )
                if maint_task in done:
                    _log_maint_task_exception(maint_task)
                    return
                # Drain timeout — force cancel. A running to_thread thread is
                # NOT joined here (cannot safely cancel a thread); it finishes
                # on its own in the thread pool.
                get_logger("app").warning(
                    "access log maintenance did not drain within %ss; "
                    "cancelling (in-flight to_thread continues in background)",
                    _MAINT_DRAIN_TIMEOUT,
                )
                maint_task.cancel()
                try:
                    await maint_task
                except asyncio.CancelledError:
                    pass  # expected — we just cancelled
                except Exception as exc:
                    get_logger("app").warning(
                        "access log maintenance task error during cancel",
                        exc_info=exc,
                    )
            stack.push_async_callback(_stop_maintenance)
        if settings.traffic_snapshot_enabled and settings.traffic_metrics_enabled:
            try:
                await app.state.traffic_snapshotter.start()
                # F-009 observability: retention ownership moved into the
                # snapshotter loop — state it explicitly so operators do not
                # expect the access-log maintenance loop to cover snapshots.
                get_logger("app").info(
                    "traffic snapshot retention is self-managed by the "
                    "snapshotter loop (independent of ACCESS_LOG_ENABLED); "
                    "retain_days=%s",
                    settings.traffic_snapshot_retain_days,
                )
            except Exception as exc:
                get_logger("app").warning(
                    "traffic snapshotter start failed", exc_info=exc
                )
        yield
        # AsyncExitStack.__aexit__ runs all registered cleanups in LIFO order.
        # This executes on normal shutdown, startup-failure (exception before
        # yield), AND cancellation — guaranteeing resource cleanup in every
        # exit path (P0-1).


# F-137: the default FastAPI docs/openapi surface (/docs, /redoc,
# /openapi.json, /docs/oauth2-redirect) is DISABLED — a loopback-exposed
# sidecar must not serve an API explorer or schema description. Without
# this, those paths fall through to the catch-all 404
# (``thin_route_not_found``) like every other unadopted route.
app = FastAPI(
    title="oc-slimapi",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
register_error_handlers(app)
# Dual-version window (4.0.0, B3a-A) — the version selector (the retired
# Batch-A gate was removed with the v2 pipeline) sits at this position in
# the stack.
# Version selector — v4-only window (2026-08-21 narrowing): only ``?v=4``
# is admitted (scope-state marked with the wire view; `v` stripped;
# directory consumed per §5.1/§5.2 — v4 additionally retires directory on
# the global sessions list). Every other /slimapi/** version form —
# including the retired ``?v=3`` — is a 400; GET /slimapi/versions is
# unconditionally exempt (non-GET there → 405 + Allow: GET, first
# priority). Catch-all (non /slimapi) requests pass through untouched (and
# are answered 404 by the closed proxy). (Historical: the 4.0.0 (3, 4)
# dual window also admitted ?v=3 onto the unchanged v3 pipeline.)
app.add_middleware(SlimapiSelectorMiddleware)
# Traffic-accounting middleware. Added AFTER the version gate so it is the
# OUTERMOST middleware — it wraps every HTTP route including the version
# gate's own 400 ``version_required`` responses and the catch-all reverse
# proxy. Pure-ASGI (NOT BaseHTTPMiddleware) so SSE / StreamingResponse keep
# streaming untouched.
app.add_middleware(TrafficAccountingMiddleware)
# Request-id middleware (outermost — wraps everything including traffic accounting).
app.add_middleware(RequestIdMiddleware)
# token_stream is registered alongside the other /slimapi routers and BEFORE
# install_proxy's catch-all (design §5.1: route must precede the reverse
# proxy). Its path ``/slimapi/sessions/{sid}/stream`` does not shadow
# ``/{sid}/status`` or ``/{sid}/children`` (different literal suffixes).
for router in (health.router, versions.router, actions.router, agent.router, command.router, sessions.router, children.router, todo.router, diff_routes.router, messages.router, events.router, metrics.router, questions.router, permissions.router, directories.router, token_stream.router, read_groups.router, write_groups.router, file_raw.router):
    app.include_router(router)
install_proxy(app)


def main() -> None:
    # P1-35: surface configuration errors with a clear, field-named message
    # rather than a raw traceback. setup_logging() runs first so the error
    # is visible on stderr; then we exit with a non-zero status.
    setup_logging()
    try:
        settings.validate()
    except RuntimeError as exc:
        get_logger("app").error("configuration error: %s", exc)
        raise SystemExit(1)
    uvicorn.run(
        "oc_slimapi.app:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT,
    )


if __name__ == "__main__":
    main()
