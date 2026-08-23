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

Not wired into: Batch 1 coalescing (YAGNI per plan §5). The Batch 2 ETag
wiring is enabled (4.11.0 Phase A / A2 — see below).
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
    forwarded as ``X-Opencode-Directory``. Current sidecars expose only this
    thin route; there is no passthrough fallback.

    4.11.0 Phase A / A2: opts INTO the Batch 2 ETag wiring
    (``enable_etag=True``): per-coding validators + 304 on a matching
    ``If-None-Match``; ``Cache-Control: no-store`` stays on every
    response (200 and 304 — revalidate every time, the validator only
    saves the transport body). Merged ``Vary`` kept (directory variance
    is real); tiny-body gzip benefit gate applies (empty ``[]`` →
    identity) and decides the served coding BEFORE the validator is
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
            upstream_path=f"/session/{sid}/children",
            directory=directory,
            project_fn=_project_children,
            read_with_cap=read_with_cap,
            err_label="children",
            read_timeout=None,
            sid=sid,
            enable_etag=True,
            merge_directory_vary=True,
            min_gzip_bytes=MIN_GZIP_BYTES,
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
