from __future__ import annotations

from typing import Any, NoReturn

import httpx
from fastapi import APIRouter, FastAPI, Query, Request

from oc_slimapi.logging_config import get_logger

logger = get_logger(__name__)

from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..skeleton import skeleton_session
from ..traffic import stash_up_in
from ..transform import TransformBusy
from ..upstream import forward_directory_headers
from ..upstream_errors import raise_upstream_status

router = APIRouter(prefix="/slimapi", tags=["sessions"])


@router.get("/sessions")
async def sessions(
    request: Request,
    directory: str | None = None,
    roots: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    start: int | None = Query(None, ge=0),
    search: str | None = None,
):
    if directory is not None:
        # slimapi no longer gates directories — normalize and forward; the
        # upstream opencode decides whether it can serve the directory.
        directory = validate_directory(directory)
    params = {"limit": limit, "roots": str(roots).lower()}
    if directory is not None:
        params["directory"] = directory
    if start is not None:
        params["start"] = start
    if search is not None:
        params["search"] = search
    # Admission BEFORE the upstream GET (mirrors messages.py): bound
    # concurrent sessions-list requests (upstream body buffering + parse +
    # projection) by max_transforms so a burst cannot monopolise memory /
    # event-loop CPU. The slot is held across fetch→parse→project.
    # Known limitation vs messages: the single-response body is not yet
    # bounded by read_with_cap (no 413 on oversize) — pre-existing, out of
    # this batch's scope; concurrency is bounded here.
    try:
        async with request.app.state.transforms as pool:
            try:
                response = await request.app.state.upstream.get(
                    "/session", params=params, headers=forward_directory_headers(directory),
                )
            except httpx.RequestError as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            # Traffic accounting: sessions-list upstream body.
            stash_up_in(request, len(response.content))
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_upstream_status(exc)
            try:
                payload = response.json()
            except Exception as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            if not isinstance(payload, list):
                # v6 §1.1: dict / string / null etc. would have been silently
                # iterated by ``for item in payload`` and yielded a 200 with
                # ``X-Complete: true`` (the empty skeleton list). Treat non-list
                # bodies as a malformed upstream — same 503 as the sibling
                # ``response.json()`` failure path. No completeness headers on
                # this branch (the contract is: 200 only).
                raise CodedHTTPException(503, code="upstream_unavailable")
            # Offload skeleton projection to the worker so the event loop is
            # not blocked by deep copy of potentially many sessions.
            sessions = await pool.offload(
                _project_sessions,  # helper below
                payload,
            )
    except TransformBusy as exc:
        raise CodedHTTPException(503, code="transform_busy") from exc
    # v6 §1.1: completeness signal header (200-only — 503 / 502 paths above
    # do not emit it, by design).
    complete = len(sessions) < limit
    return json_response(
        sessions,
        headers={
            "X-Complete": "true" if complete else "false",
        },
        accept_encoding=request.headers.get("accept-encoding"),
    )


def _project_sessions(payload: list[dict]) -> list[dict]:
    """Worker-thread entry: project each session dict (no side effects)."""
    return [skeleton_session(item) for item in payload]



