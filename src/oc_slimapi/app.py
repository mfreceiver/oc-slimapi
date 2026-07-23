import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from . import __version__
from .access_log import setup_access_log
from .capabilities import parse_capabilities
from .children_cache import ChildrenCache
from .config import settings
from .errors import register_error_handlers
from .middleware.traffic_accounting import TrafficAccountingMiddleware
from .observability import BatchLedger
from .proxy import install_proxy
from .routes import events, health, messages, metrics, questions, sessions, sessions_children, token_stream
from .sse.hub import HubRegistry
from .sse.token_hub import TokenStreamHub, TokenStreamRegistry
from .sse.tokenstream.hub import apply_debug_budget_overrides
from .traffic import TrafficLedger
from .transform import TransformConfig, TransformPool
from .upstream import create_client
from .versioning import SlimapiVersionMiddleware


async def smoke(app: FastAPI) -> None:
    sid = app.state.config.smoke_session_id
    if not sid:
        try:
            sessions = (await app.state.upstream.get("/session", params={"limit": 1})).json()
            sid = sessions[0].get("id") if sessions else None
        except Exception:
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
    except Exception:
        app.state.schema_degraded = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    app.state.config = settings
    # Structured access log (RotatingFileHandler on the oc_slimapi.access
    # logger). Idempotent — safe under uvicorn hot reload. Disabled-logger
    # path skips all filesystem work.
    setup_access_log(
        enabled=settings.access_log_enabled,
        path=settings.access_log_path,
        max_bytes=settings.access_log_max_bytes,
        backups=settings.access_log_backups,
    )
    # Full bidirectional byte ledger (traffic accounting). Single shared
    # instance — read by the ASGI middleware (down/up per-request), the SSE
    # generators (SSE downstream per-frame), and GlobalHub.run (SSE upstream
    # per-line). When disabled, record_* are no-ops and snapshot reports
    # ``{"enabled": False}``.
    app.state.traffic_ledger = TrafficLedger(enabled=settings.traffic_metrics_enabled)
    # Debug/联调-only: override token-stream memory budget caps from env
    # (OC_SLIMAPI_TOKEN_STREAM_DEBUG_*). No-op when env vars are unset.
    # (placed before parse_capabilities import; here for proximity to other
    # budget-tuning.)
    apply_debug_budget_overrides(settings)
    app.state.batch_ledger = BatchLedger(
        window_seconds=settings.opt_a_rollback_window_seconds
    )
    app.state.route_secret = settings.read_route_secret()
    app.state.upstream = create_client(settings)
    app.state.children = ChildrenCache(app.state.upstream)
    # Transform pool: admission semaphore + bounded worker executor, both
    # sized by OC_SLIMAPI_MAX_TRANSFORMS. Acquired by the skeleton routes
    # *before* their upstream GET so memory pressure stays bounded and the
    # parse/project/serialize/gzip chain never runs on this event loop.
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    # discovery readiness signal (v6 §1.1): False until first successful
    # load_products; subsequent failures retain last-known-good (do not reset).
    app.state.allowlist_ready = False
    # serialises concurrent load_products callers (warm_allowlist,
    # /projects, q-p null-dir fan-out) so a slow stale fetch cannot
    # overwrite a fast fresh one.
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    # S-E: best-effort deployment revision (env or file, swallow errors)
    try:
        app.state.deployment_revision = settings.read_deployment_revision()
    except Exception:
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
    app.state.hubs.set_children_cache(app.state.children)
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
    await smoke(app)
    # F3: best-effort allowlist warm-up so the first routeToken-bearing reply
    # does not hit a cold allowlist. Failure is non-fatal (lazy refresh fallback).
    await sessions.warm_allowlist(app)
    try:
        yield
    finally:
        await app.state.children.aclose()
        # NB-C4: stop the token flush loop on shutdown (idempotent; no-op if
        # already stopped on last-detach). Must precede hubs.close() so the
        # registry / hub are still coherent while the loop drains.
        app.state.token_hub.stop()
        await app.state.hubs.close()
        await app.state.upstream.aclose()
        # Drain in-flight transforms before letting the process exit so a
        # hot reload does not yank a worker out from under an active gzip.
        app.state.transforms.shutdown()


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
# token_stream is registered alongside the other /slimapi routers and BEFORE
# install_proxy's catch-all (design §5.1: route must precede the reverse
# proxy). Its path ``/slimapi/sessions/{sid}/stream`` does not shadow
# ``/{sid}/status`` or ``/{sid}/children`` (different literal suffixes).
for router in (health.router, sessions.router, sessions_children.router, messages.router, questions.router, events.router, metrics.router, token_stream.router):
    app.include_router(router)
install_proxy(app)


def main() -> None:
    settings.validate()
    uvicorn.run("oc_slimapi.app:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
