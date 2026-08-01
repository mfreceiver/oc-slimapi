import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

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
from .routes import events, health, messages, metrics, sessions, token_stream
from .sse.hub import HubRegistry
from .sse.token_hub import TokenStreamHub, TokenStreamRegistry
from .sse.tokenstream.hub import apply_debug_budget_overrides
from .traffic import TrafficLedger
from .traffic_snapshot import TrafficSnapshotter
from .transform import TransformConfig, TransformPool
from .upstream import create_client
from .versioning import SlimapiVersionMiddleware


async def smoke(app: FastAPI) -> None:
    sid = app.state.config.smoke_session_id
    if not sid:
        try:
            sessions = (await app.state.upstream.get("/session", params={"limit": 1})).json()
            sid = sessions[0].get("id") if sessions else None
        except Exception as exc:
            get_logger("app").warning("smoke: failed to fetch sessions list", exc_info=exc)
            sid = None
    if not sid:
        return
    try:
        response = await app.state.upstream.get(f"/session/{sid}/message", params={"limit": 1})
        payload = response.json()
        valid = response.status_code < 300 and isinstance(payload, list)
        if payload:
            valid = valid and isinstance(payload[0].get("info", {}).get("id"), str)
            valid = valid and all(isinstance(part.get("type"), str) for part in payload[0].get("parts", []))
        app.state.schema_degraded = not valid
    except Exception as exc:
        get_logger("app").warning("smoke: schema validation failed", exc_info=exc)
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
    # Backward-compat (阻断6): if the deprecated OC_SLIMAPI_ACCESS_LOG_PATH is
    # set to a non-default value, honor its parent dir with a warning.
    access_log_dir = settings.access_log_dir
    # Backward-compat (阻断6) + priority clarity (终审重要项): the deprecated
    # OC_SLIMAPI_ACCESS_LOG_PATH gets a say ONLY when the new
    # OC_SLIMAPI_ACCESS_LOG_DIR is left at its default. An explicitly-set new
    # dir always wins over the deprecated path (its parent is used as a
    # fallback + warning). Without this guard a stale legacy env would
    # silently override an explicit new dir.
    if access_log_dir == "logs" and settings.access_log_path != "logs/access.jsonl":
        legacy_dir = str(Path(settings.access_log_path).parent) or "."
        get_logger("app").warning(
            "OC_SLIMAPI_ACCESS_LOG_PATH is deprecated (ignored); using its "
            "parent dir %r — set OC_SLIMAPI_ACCESS_LOG_DIR explicitly",
            legacy_dir,
        )
        access_log_dir = legacy_dir
    setup_access_log(enabled=settings.access_log_enabled, dir=access_log_dir)
    # Daily-rotation maintenance at startup (best-effort): migrate any legacy
    # RotatingFileHandler files, compress non-today history, prune by retain.
    if settings.access_log_enabled:
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
    # this). Writes a total snapshot per tick; deltas are derived at analysis
    # time. Best-effort: start/stop failures warn + degrade (task-2 阻断7).
    app.state.traffic_snapshotter = TrafficSnapshotter(
        ledger=app.state.traffic_ledger,
        interval_s=settings.traffic_snapshot_interval_s,
        path=settings.traffic_snapshot_path,
    )
    # Debug/联调-only: override token-stream memory budget caps from env
    # (OC_SLIMAPI_TOKEN_STREAM_DEBUG_*). No-op when env vars are unset.
    apply_debug_budget_overrides(settings)
    app.state.upstream = create_client(settings)
    # Transform pool: admission semaphore + bounded worker executor, both
    # sized by OC_SLIMAPI_MAX_TRANSFORMS. Acquired by the skeleton routes
    # *before* their upstream GET so memory pressure stays bounded and the
    # parse/project/serialize/gzip chain never runs on this event loop.
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
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
    # Token-stream accumulator (design-token-stream.md §5.3). Constructed
    # unconditionally so publish() can route message.part.delta/updated
    # into it the moment the first upstream event arrives; subscriber /
    # flush / fan-out wiring lands in Stage B/C/D. Independent ledger —
    # does NOT consume MAX_TOTAL_SUBSCRIBERS.
    app.state.token_hub = TokenStreamHub()
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
    # transient upstream blip at startup does not justify failing the process.
    try:
        await app.state.upstream.get("/global/health")
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
        access_log_dir if settings.access_log_enabled else "disabled",
    )
    # Start background tasks after all state is wired. The access-log
    # maintenance loop (compress+prune) runs independent of restart so a
    # long-running process still compresses history; the snapshotter writes
    # its first frame immediately then ticks on interval.
    if settings.access_log_enabled:
        stop_event = asyncio.Event()
        app.state._access_log_stop_event = stop_event
        app.state._access_log_maintenance_task = asyncio.create_task(
            run_access_log_maintenance_loop(
                dir=access_log_dir,
                retain_days=settings.access_log_retain_days,
                interval_s=settings.access_log_maintenance_interval_s,
                stop_event=stop_event,
            )
        )
    if settings.traffic_snapshot_enabled and settings.traffic_metrics_enabled:
        try:
            await app.state.traffic_snapshotter.start()
        except Exception as exc:
            get_logger("app").warning(
                "traffic snapshotter start failed", exc_info=exc
            )
    try:
        yield
    finally:
        # Stop the access-log maintenance loop first (signal + cancel) so it
        # cannot race the ledger/hub teardown below.
        stop_event = getattr(app.state, "_access_log_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        maint_task = getattr(app.state, "_access_log_maintenance_task", None)
        if maint_task is not None and not maint_task.done():
            maint_task.cancel()
            try:
                await maint_task
            except (asyncio.CancelledError, Exception):
                pass
        # Each cleanup component is isolated so one failure does NOT skip the
        # others (task-2 阻断7: true per-component convergence, not a single
        # try that short-circuits and leaks upstream/transform). The final-
        # state snapshot below runs unconditionally after all are attempted.
        # NB-C4: token flush loop stop must precede hubs.close() so the
        # registry / hub stay coherent while the loop drains.
        try:
            app.state.token_hub.stop()
        except Exception as exc:
            get_logger("app").warning("token_hub.stop failed", exc_info=exc)
        try:
            await app.state.hubs.close()
        except Exception as exc:
            get_logger("app").warning("hubs.close failed", exc_info=exc)
        try:
            await app.state.upstream.aclose()
        except Exception as exc:
            get_logger("app").warning("upstream.aclose failed", exc_info=exc)
        try:
            # Drain in-flight transforms so a hot reload does not yank a
            # worker out from under an active gzip.
            app.state.transforms.shutdown()
        except Exception as exc:
            get_logger("app").warning("transforms.shutdown failed", exc_info=exc)
        # Close the access-log DailyAccessHandler so file handles flush +
        # release on graceful shutdown (re-init removes handlers, but a clean
        # lifespan shutdown should not rely on interpreter GC — 终审重要项).
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
        # Final-state snapshot (innermost — always runs after all accounting
        # components have been attempted, capturing the terminal ledger totals
        # even if any cleanup above raised).
        snapshotter = getattr(app.state, "traffic_snapshotter", None)
        if snapshotter is not None:
            try:
                await snapshotter.stop()
            except Exception as exc:
                get_logger("app").warning(
                    "final traffic snapshot failed", exc_info=exc
                )


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
for router in (health.router, sessions.router, messages.router, events.router, metrics.router, token_stream.router):
    app.include_router(router)
install_proxy(app)


def main() -> None:
    settings.validate()
    uvicorn.run("oc_slimapi.app:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
