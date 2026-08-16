"""Read-only diff thin route — T18 (2026-08-16).

``GET /slimapi/sessions/{sid}/diff`` → upstream
``GET /session/{sid}/diff[?messageID=…]``.

A verbatim sibling of the gated T17 todo/children routes (traffic plan
Batch 3 / C2a pattern): the upstream ``Snapshot.FileDiff`` struct is
already minimal (``file`` / ``patch`` / ``additions`` / ``deletions`` /
``status`` — schema v1.18.16 ``packages/schema/src/file-diff.ts:6-13``),
so the projection is near-identity; the big ``patch`` strings are kept
(UI-consumed) — gzip is the saving lever, the same honest conclusion as
the todo design doc §2.

Upstream query (v1.18.16 anchors): ``DiffQuery`` =
``WorkspaceRoutingQueryFields`` + ``{messageID?: MessageID}``
(``groups/session.ts:39-42``, built from ``SessionSummary.DiffInput``
minus ``sessionID``, ``session/summary.ts:148-152``); the handler passes
``ctx.query.messageID`` straight to ``summary.diff``
(``handlers/session.ts:99-103``). Upstream semantics (verified at
``summary.ts:129-137``): ``messageID`` OMITTED → ``[]``; a ``messageID``
that does not exist or whose message role is not ``user`` → ``[]``; a
valid user ``messageID`` → that message's summary diffs (git path
unquoting applied upstream). This route forwards the same-named query
verbatim when present and omits it when absent — the ``[]`` cases pass
through as normal 200 ``[]`` bodies (NOT 4xx/5xx).

Not wired into: Batch 1 coalescing (anonymous-migration volume is small —
YAGNI per plan §5) or Batch 2 ETag (follow-up; mirrors todo/children).
Stateless — no cache, no version tags.
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

router = APIRouter(prefix="/slimapi/sessions/{sid}", tags=["diff"])


def _project_diff(items: list) -> list:
    """Near-identity projection: ``FileDiff.Info`` has exactly five fields
    (``file`` / ``patch`` optional strings, ``additions`` / ``deletions``
    required numbers, ``status`` optional enum), all UI-consumed — there is
    no never-consumed heavy field to whitelist away (todo design doc §2
    reasoning; the ``patch`` payload is the point of the route). Per-item
    dict guard (children rev-6 B2 precedent): a scalar element
    (``[1, null]``) is a malformed envelope and raises ``ValueError``,
    which the shared handler maps to 503 ``upstream_unavailable``, instead
    of surfacing an unstructured 500 downstream."""
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"non-dict diff item: {type(item).__name__}")
    return items


@router.get("/diff")
async def session_diff(request: Request, sid: str,
                       directory: str | None = None,
                       messageID: str | None = None):
    """File changes (diff) for a session — thin read (gzip + cap +
    admission).

    ``directory`` is routing-only (selects the opencode workdir instance),
    forwarded as ``X-Opencode-Directory`` — same semantics as the messages
    route. ``messageID`` (optional) is forwarded upstream verbatim as the
    same-named query parameter (selects which user message to diff).
    Upstream ``[]`` semantics (summary.ts:129-137): omitted ``messageID``,
    unknown message, or non-user role all answer 200 ``[]`` — empty results
    are normal bodies, not errors. ADDITIVE route: older sidecars answer
    404 ``thin_route_not_found`` from the catch-all and the client falls
    back to the passthrough ``GET /session/{sid}/diff``.

    Mirrors todo/children (rev-6 B1/C2): opts OUT of the Batch 2 ETag
    wiring (``enable_etag=False``): no ``ETag`` header, any
    ``If-None-Match`` is ignored (always 200). The directory variance is
    still real, so ``Vary`` keeps the merged form; the tiny-body gzip
    benefit gate applies (empty ``[]`` → identity).
    """
    # v3 (§5, Batch B): a consumed ``?directory=`` was validated + stripped
    # at dispatch — the stash replaces the (absent) query param here.
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    upstream_params: dict[str, str] | None = (
        {"messageID": messageID} if messageID is not None else None
    )
    try:
        return await handle_catalog_request(
            request,
            upstream_path=f"/session/{sid}/diff",
            directory=directory,
            project_fn=_project_diff,
            read_with_cap=read_with_cap,
            err_label="diff",
            read_timeout=None,
            sid=sid,
            enable_etag=False,
            merge_directory_vary=True,
            min_gzip_bytes=MIN_GZIP_BYTES,
            upstream_params=upstream_params,
        )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
