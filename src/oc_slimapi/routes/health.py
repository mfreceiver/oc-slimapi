import time

from fastapi import APIRouter, Request

from .. import __version__
from ..gzip_util import json_response

router = APIRouter(prefix="/slimapi", tags=["health"])


@router.get("/health")
async def health(request: Request):
    return json_response({
        "sidecar": {"ok": True, "version": __version__},
        "server": {
            "api_version": request.app.state.config.server_api_version,
            "accepted_client_versions": list(request.app.state.config.accepted_client_versions),
        },
        "schema": {"degraded": request.app.state.schema_degraded},
    }, accept_encoding=request.headers.get("accept-encoding"))


@router.get("/ready")
async def ready(request: Request):
    started = time.monotonic()
    try:
        response = await request.app.state.upstream.get("/global/health", timeout=5.0)
        ok = response.status_code < 300
    except Exception:
        ok = False
    return json_response({
        "upstream": {"ok": ok, "latencyMs": round((time.monotonic() - started) * 1000)},
        "server": {
            "api_version": request.app.state.config.server_api_version,
            "accepted_client_versions": list(request.app.state.config.accepted_client_versions),
        },
        "schema": {"degraded": request.app.state.schema_degraded},
    }, status_code=200 if ok else 503, accept_encoding=request.headers.get("accept-encoding"))
