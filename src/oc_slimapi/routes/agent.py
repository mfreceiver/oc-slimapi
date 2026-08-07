from __future__ import annotations

from fastapi import APIRouter, Request

from ..directory import validate_directory
from ..skeleton import skeleton_agents
from ..transform import TransformBusy, read_with_cap
from ._catalog_common import (
    busy_response,
    handle_catalog_request,
)

router = APIRouter(prefix="/slimapi", tags=["catalog"])


@router.get("/agent")
async def agent(request: Request, directory: str | None = None):
    """Skeleton projection of upstream opencode's agent catalog.

    Proxies upstream ``GET /agent`` and keeps only the ocdroid-consumed
    whitelist (``name`` / ``description`` / ``mode`` / ``hidden`` /
    ``native``), dropping the dominant ``prompt`` (the full system prompt,
    ~34.7%) and ``permission`` (the ``Permission.Ruleset`` list, ~61.2% —
    NOT the pending permission card; no UI consumer). Live-measured ~95.8%
    raw byte saving (250 KB -> 10.7 KB; gzip 3.57 KB — note gzip has some
    消解 because ``permission`` repeats rule strings that compress well).

    ``directory`` is accepted for slimapi API consistency and forwarded as
    ``X-Opencode-Directory``; the agent catalog is global so upstream
    ignores it (harmless). This is an ADDITIVE route: a client on an older
    sidecar without it gets 404 ``thin_route_not_found`` from the catch-all
    proxy and falls back to the passthrough ``GET /agent``.
    """
    if directory is not None:
        directory = validate_directory(directory)
    try:
        return await handle_catalog_request(
            request,
            upstream_path="/agent",
            directory=directory,
            project_fn=skeleton_agents,
            read_with_cap=read_with_cap,
            err_label="agent",
            read_timeout=None,
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
