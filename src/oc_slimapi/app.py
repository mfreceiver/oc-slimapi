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
from .config import settings
from .errors import register_error_handlers
from .logging_config import get_logger, setup_logging
from .middleware.request_id import RequestIdMiddleware
from .middleware.traffic_accounting import TrafficAccountingMiddleware
from .proxy import install_proxy
from .routes import agent, command, events, health, messages, metrics, questions, sessions, token_stream
from .sse.hub import HubRegistry
from .sse.token_hub import TokenStreamHub, TokenStreamRegistry
from .sse.tokenstream.hub import apply_debug_budget_overrides
from .traffic import TrafficLedger
from .traffic_snapshot import TrafficSnapshotter
from .transform import TransformConfig, TransformPool
from .upstream import create_client
from .versioning import SlimapiVersionMiddleware


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
    # Registration order (and thus reverse-cleanup order) is:
    #   access-log handler → snapshotter → upstream → transforms → hubs →
    #   token_hub → maintenance task
    # Cleanup runs in LIFO: maintenance → token_hub → hubs → transforms →
    #   upstream → snapshotter → access-log handler.
    # The only ordering constraint with cross-component semantics is
    # token_hub.stop() BEFORE hubs.close() (NB-C4: flush loop must drain while
    # the registry / hub are still coherent); that is satisfied by registering
    # token_hub AFTER hubs.
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
        app.state.schema_degraded = False
        # P1-36: smoke status defaults to not_run; smoke() updates it.
        app.state.smoke_status = SMOKE_NOT_RUN
        # S-E: best-effort deployment revision (env or file, swallow errors)
        try:
            app.state.deployment_revision = settings.read_deployment_revision()
        except Exception as exc:
            get_logger("app").warning("failed to read deployment revision", exc_info=exc)
            app.state.deployment_revision = None
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

        async def _close_hubs():
            try:
                await app.state.hubs.close()
            except Exception as exc:
                get_logger("app").warning("hubs.close failed", exc_info=exc)
        stack.push_async_callback(_close_hubs)
        # Token-stream accumulator (design-token-stream.md §5.3). Constructed
        # unconditionally so publish() can route message.part.delta/updated
        # into it the moment the first upstream event arrives; subscriber /
        # flush / fan-out wiring lands in Stage B/C/D. Independent ledger —
        # does NOT consume MAX_TOTAL_SUBSCRIBERS.
        # INV-5 (P1-17): pass max_frame_bytes from Settings so the hub's
        # snapshot/truncation ceiling matches the subscriber's oversized
        # ceiling (same source: settings.token_stream_max_frame_bytes).
        app.state.token_hub = TokenStreamHub(
            max_frame_bytes=settings.token_stream_max_frame_bytes,
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

        inc_store = IncarnationStore(state_dir=access_log_dir)
        incarnation = inc_store.load_or_bump()
        app.state.turn_registry = TurnRegistry(incarnation=incarnation)
        app.state.hubs.set_turn_registry(app.state.turn_registry)
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
            except Exception as exc:
                get_logger("app").warning(
                    "traffic snapshotter start failed", exc_info=exc
                )
        yield
        # AsyncExitStack.__aexit__ runs all registered cleanups in LIFO order.
        # This executes on normal shutdown, startup-failure (exception before
        # yield), AND cancellation — guaranteeing resource cleanup in every
        # exit path (P0-1).


app = FastAPI(title="oc-slimapi", version=__version__, lifespan=lifespan)
register_error_handlers(app)
app.add_middleware(
    SlimapiVersionMiddleware,
    accepted_client_versions=settings.accepted_client_versions,
)
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
for router in (health.router, agent.router, command.router, sessions.router, messages.router, events.router, metrics.router, questions.router, token_stream.router):
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
    uvicorn.run("oc_slimapi.app:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
