"""Read-only todo thin route — T17 / traffic plan Batch 3 (C2a).

``GET /slimapi/sessions/{sid}/todo`` → upstream ``GET /session/{sid}/todo``.

Design: ``docs/specs/traffic-route-todo-2026-08-10.md`` (approved). The
upstream ``Todo.Info`` struct is already minimal (``content`` / ``status`` /
``priority`` — schema v1.18.16 ``packages/schema/src/session-todo.ts:7-15``),
so the projection is near-identity; the route's value is gzip + cap +
admission-before-GET + structured sid-aware errors, mirroring the catalog
chain (design doc §2's honest conclusion: no whitelist lever here).

Not wired into: Batch 1 coalescing (anonymous-migration volume is small —
YAGNI per plan §5). Stateless — no cache, no version tags; the Batch 2
ETag/304 wiring is enabled (4.11.0 Phase A / A2 — see below).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..directory import validate_directory
from ..selector import resolve_route_directory
from ..gzip_util import MIN_GZIP_BYTES
from ..transform import TransformBusy, read_with_cap
from ._catalog_common import (
    busy_response,
    handle_catalog_request,
)

router = APIRouter(prefix="/slimapi/sessions/{sid}", tags=["todo"])


def _project_todo(items: list) -> list:
    """Near-identity projection: ``Todo.Info`` has exactly three string
    fields (``content`` / ``status`` / ``priority``), all UI-consumed —
    there is no never-consumed heavy field to whitelist away (design doc
    §2). rev-6 B2: a per-item dict guard runs BEFORE the passthrough — a
    scalar element (``[1, null]``) is a malformed envelope and raises
    ``ValueError``, which the shared handler maps to 503
    ``upstream_unavailable`` (mirrors sessions.py's per-item guard) instead
    of surfacing an unstructured 500 downstream."""
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"non-dict todo item: {type(item).__name__}")
    return items


@router.get("/todo")
async def session_todo(request: Request, sid: str,
                       directory: str | None = None):
    """Todo list for a session — thin read (gzip + cap + admission).

    ``directory`` is routing-only (selects the opencode workdir instance),
    forwarded as ``X-Opencode-Directory`` — same semantics as the messages
    route. Current sidecars expose only this thin route; there is no
    passthrough fallback.

    4.11.0 Phase A / A2: this route opts INTO the Batch 2 ETag wiring
    (``enable_etag=True``): per-coding validators + 304 on a matching
    ``If-None-Match``; ``Cache-Control: no-store`` stays on every
    response (200 and 304 — revalidate every time, the validator only
    saves the transport body). The directory variance keeps the merged
    ``Vary`` form; the tiny-body gzip benefit gate applies (empty ``[]``
    → identity) and decides the served coding BEFORE the validator is
    derived, so the judged coding always equals the served coding.
    """
    # v4: a consumed ``?directory=`` was validated + stripped
    # at dispatch — the stash replaces the (absent) query param here.
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    try:
        return await handle_catalog_request(
            request,
            upstream_path=f"/session/{sid}/todo",
            directory=directory,
            project_fn=_project_todo,
            read_with_cap=read_with_cap,
            err_label="todo",
            read_timeout=None,
            sid=sid,
            enable_etag=True,
            merge_directory_vary=True,
            min_gzip_bytes=MIN_GZIP_BYTES,
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
