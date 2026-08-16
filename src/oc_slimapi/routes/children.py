"""Read-only children thin route — T17 / traffic plan Batch 3 (C2a).

``GET /slimapi/sessions/{sid}/children`` → upstream
``GET /session/{sid}/children``.

Design: ``docs/specs/traffic-route-children-2026-08-10.md`` (approved). The
response is ``Session.Info[]`` — the SAME element type as the sessions list
route — so each child is projected by the existing ``skeleton_session()``
verbatim (heavy ``cost`` / ``tokens`` / ``location`` / ``subpath`` dropped;
design doc §2: strongest reuse signal in T17).

STATELESS re-add (design doc §6.2 guardrail — the v1 cache-coherence
machinery stays deleted): NO ``X-Children-Version`` header, NO
``childrenVersion`` digest field, NO ``childrenIDs[]`` /
``childrenComplete`` list hints, NO per-key cache / single-flight /
SSE-driven invalidation.

Not wired into: Batch 1 coalescing (YAGNI per plan §5) or Batch 2 ETag
(follow-up).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..directory import validate_directory
from ..selector import resolve_route_directory
from ..gzip_util import MIN_GZIP_BYTES
from ..skeleton import skeleton_session
from ..transform import TransformBusy, read_with_cap
from ._catalog_common import (
    busy_response,
    handle_catalog_request,
)

router = APIRouter(prefix="/slimapi/sessions/{sid}", tags=["children"])


def _project_children(items: list) -> list:
    """Project each child ``Session.Info`` through the sessions-list
    skeleton (``SESSION_KEYS`` whitelist + nested picks) — identical
    keep/drop semantics to ``GET /slimapi/sessions`` (design doc §2).

    rev-6 B2: per-item dict guard BEFORE ``skeleton_session()`` — a scalar
    element (``[1, null]``) would crash the projection with an
    ``AttributeError`` (unstructured 500); it is a malformed envelope and
    raises ``ValueError`` instead, which the shared handler maps to 503
    ``upstream_unavailable`` (mirrors sessions.py:302-318)."""
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(
                f"non-dict child session: {type(item).__name__}")
    return [skeleton_session(item) for item in items]


@router.get("/children")
async def session_children(request: Request, sid: str,
                           directory: str | None = None):
    """Child sessions of a parent — thin skeleton read (stateless re-add).

    ``directory`` is routing-only (selects the opencode workdir instance),
    forwarded as ``X-Opencode-Directory``. ADDITIVE route: older sidecars
    answer 404 ``thin_route_not_found`` from the catch-all and the client
    falls back to the passthrough ``GET /session/{sid}/children``.

    rev-6 B1/C2: opts OUT of the Batch 2 ETag wiring (``enable_etag=False``
    — plan §5): no ``ETag``, any ``If-None-Match`` ignored (always 200);
    merged ``Vary`` kept (directory variance is real); tiny-body gzip
    benefit gate applies (empty ``[]`` → identity).
    """
    # v3 (§5, Batch B): a consumed ``?directory=`` was validated + stripped
    # at dispatch — the stash replaces the (absent) query param here.
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    try:
        return await handle_catalog_request(
            request,
            upstream_path=f"/session/{sid}/children",
            directory=directory,
            project_fn=_project_children,
            read_with_cap=read_with_cap,
            err_label="children",
            read_timeout=None,
            sid=sid,
            enable_etag=False,
            merge_directory_vary=True,
            min_gzip_bytes=MIN_GZIP_BYTES,
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
