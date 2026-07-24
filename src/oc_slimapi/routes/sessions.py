from __future__ import annotations

import asyncio
from typing import Any, NoReturn
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Query, Request

from oc_slimapi.logging_config import get_logger

logger = get_logger(__name__)

from ..directory import validate_directory
from ..discovery import load_products
from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..skeleton import skeleton_session
from ..traffic import stash_up_in
from ..transform import TransformBusy
from ..upstream import forward_directory_headers
from ..upstream_errors import fetch_json_mapped, raise_upstream_status

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
    # v6 §1.1: completeness + discovery readiness signal headers. These are
    # 200-only — the 503 / 502 paths above do not emit them, by design.
    complete = len(sessions) < limit
    discovery_directories = len(request.app.state.directory_allowlist)
    discovery_ready = bool(getattr(request.app.state, "allowlist_ready", False))
    # children hint (rev H): per-session additive fields, pure cache peek
    children = getattr(request.app.state, "children", None)
    if children is not None:
        for session in sessions:
            sid = session.get("id")
            if sid is not None:
                hint = children.peek(sid, directory)
                if hint is not None:
                    ids, compl = hint
                    session["childrenComplete"] = compl
                    if compl:
                        session["childrenIDs"] = ids
                else:
                    session["childrenComplete"] = False
                # else: cache miss → omit both keys (childrenComplete defaults false)
    return json_response(
        sessions,
        headers={
            "X-Complete": "true" if complete else "false",
            "X-Discovery-Directories": str(discovery_directories),
            "X-Discovery-Ready": "true" if discovery_ready else "false",
        },
        accept_encoding=request.headers.get("accept-encoding"),
    )


@router.get("/projects")
async def projects(request: Request):
    try:
        payload = await load_products(request.app, traffic_request=request)
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc)
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    return json_response(payload, accept_encoding=request.headers.get("accept-encoding"))


@router.get("/sessions/status")
async def statuses(request: Request, directory: str):
    # slimapi no longer gates directories — normalize and forward. opencode
    # decides whether it can serve the directory (returns its own 4xx if not).
    directory = validate_directory(directory)
    payload = await fetch_json_mapped(
        request.app.state.upstream,
        "/session/status",
        params={"directory": directory},
        headers=forward_directory_headers(directory),
        traffic_request=request,
    )
    return json_response(
        payload,
        accept_encoding=request.headers.get("accept-encoding"),
    )


def _project_sessions(payload: list[dict]) -> list[dict]:
    """Worker-thread entry: project each session dict (no side effects)."""
    return [skeleton_session(item) for item in payload]


@router.get("/sessions/{sid}/status")
async def session_status(request: Request, sid: str):
    # Discover: GET /session/{sid}
    try:
        session_response = await request.app.state.upstream.get(f"/session/{sid}")
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    # Traffic accounting: discover body.
    stash_up_in(request, len(session_response.content))
    try:
        session_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc, sid=sid)
    try:
        directory = session_response.json().get("directory")
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    if not isinstance(directory, str):
        raise CodedHTTPException(503, code="upstream_unavailable")
    # F2 (historic): per-session status is a read keyed by sid (capability).
    # slimapi no longer gates directories at all — normalize purely for
    # forwarding consistency with the rest of the surface. Batch
    # /sessions/status likewise only normalizes now.
    directory = validate_directory(directory)

    # Status map: GET /session/status?directory=...
    try:
        result = await request.app.state.upstream.get(
            "/session/status", params={"directory": directory},
            headers=forward_directory_headers(directory),
        )
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    # Traffic accounting: status-map body.
    stash_up_in(request, len(result.content))
    try:
        result.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc)
    try:
        mapping = result.json()
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    if not isinstance(mapping, dict):
        raise CodedHTTPException(503, code="upstream_unavailable")

    if sid in mapping:
        return json_response(mapping[sid], accept_encoding=request.headers.get("accept-encoding"))
    return json_response({"type": "idle"}, accept_encoding=request.headers.get("accept-encoding"))
