from __future__ import annotations

from fastapi import APIRouter, Request

from ..directory import validate_directory
from ..selector import resolve_route_directory
from ..skeleton import skeleton_commands
from ..transform import TransformBusy, read_with_cap
from ._catalog_common import (
    busy_response,
    handle_catalog_request,
)

router = APIRouter(prefix="/slimapi", tags=["catalog"])


@router.get("/command")
async def command(request: Request, directory: str | None = None):
    """Skeleton projection of upstream opencode's command catalog.

    Proxies upstream ``GET /command`` and keeps only the ocdroid-consumed
    whitelist (``name`` / ``description`` / ``agent`` / ``hints``), dropping
    the dominant ``template`` (~97.7% of bytes) and ``source``. Live-measured
    ~97.6% raw byte saving (292 KB -> 7.25 KB; gzip 3.18 KB).

    ``directory`` is accepted for slimapi API consistency and forwarded as
    ``X-Opencode-Directory``; the command catalog is global so upstream
    ignores it (harmless). Current sidecars expose only this thin route; there
    is no passthrough fallback.
    """
    # v4: a consumed ``?directory=`` was validated + stripped
    # at dispatch — the stash replaces the (absent) query param here.
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    try:
        return await handle_catalog_request(
            request,
            upstream_path="/command",
            directory=directory,
            project_fn=skeleton_commands,
            read_with_cap=read_with_cap,
            err_label="command", merge_directory_vary=True,
            read_timeout=300.0,
            cache=getattr(request.app.state, "catalog_cache", None),
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
