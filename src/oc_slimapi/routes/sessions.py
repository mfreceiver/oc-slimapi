from __future__ import annotations

import asyncio
from typing import NoReturn
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Query, Request

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..skeleton import skeleton_session
from ..upstream import forward_directory_headers

router = APIRouter(prefix="/slimapi", tags=["sessions"])


async def load_products(app: FastAPI) -> list[dict]:
    client = app.state.upstream
    response = await client.get("/project")
    response.raise_for_status()
    projects = response.json()
    semaphore = asyncio.Semaphore(8)

    async def decorate(project: dict) -> dict:
        async with semaphore:
            result = await client.get(f"/project/{quote(str(project['id']), safe='')}/directories")
            result.raise_for_status()
            raw_directories = result.json()
        directories = []
        for item in raw_directories if isinstance(raw_directories, list) else []:
            if not isinstance(item, dict):
                continue
            path = item.get("directory", item.get("path"))
            if isinstance(path, str):
                directories.append({"path": path.rstrip("/") or "/", "strategy": item.get("strategy")})
        worktree = project.get("worktree")
        return {
            "id": project.get("id"),
            "name": project.get("name"),
            "worktree": worktree,
            "directories": directories,
        }

    output = await asyncio.gather(*(decorate(item) for item in projects if isinstance(item, dict)))
    allowlist = {
        path.rstrip("/") or "/"
        for project in output
        for path in ([project.get("worktree")] + [item["path"] for item in project["directories"]])
        if isinstance(path, str) and path.startswith("/")
    }
    app.state.directory_allowlist = allowlist
    return output


async def warm_allowlist(app: FastAPI) -> None:
    """Best-effort allowlist warm-up at startup. Swallows upstream errors so a
    not-yet-ready opencode does not block sidecar boot; lazy refresh via
    require_directory remains the fallback."""
    try:
        await load_products(app)
    except Exception:
        pass


def normalize_directory(directory: str) -> str:
    """Strip trailing slash (keep root '/'). Pure; no allowlist check."""
    return directory.rstrip("/") or "/"


async def require_directory(request: Request, directory: str) -> str:
    normalized = normalize_directory(directory)
    if normalized not in request.app.state.directory_allowlist:
        try:
            await load_products(request.app)
        except Exception as exc:
            raise CodedHTTPException(
                503, code="upstream_unavailable",
                message="cannot refresh directory allowlist",
            ) from exc
    if normalized not in request.app.state.directory_allowlist:
        raise CodedHTTPException(400, code="directory_not_allowed")
    return normalized


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
        directory = await require_directory(request, directory)
    params = {"limit": limit, "roots": str(roots).lower()}
    if directory is not None:
        params["directory"] = directory
    if start is not None:
        params["start"] = start
    if search is not None:
        params["search"] = search
    response = await request.app.state.upstream.get(
        "/session", params=params, headers=forward_directory_headers(directory)
    )
    if response.status_code >= 400:
        return json_response(
            response.json(),
            status_code=response.status_code,
            accept_encoding=request.headers.get("accept-encoding"),
        )
    return json_response(
        [skeleton_session(item) for item in response.json()],
        accept_encoding=request.headers.get("accept-encoding"),
    )


@router.get("/projects")
async def projects(request: Request):
    try:
        payload = await load_products(request.app)
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc)
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    return json_response(payload, accept_encoding=request.headers.get("accept-encoding"))


@router.get("/sessions/status")
async def statuses(request: Request, directory: str):
    directory = await require_directory(request, directory)
    response = await request.app.state.upstream.get(
        "/session/status",
        params={"directory": directory},
        headers=forward_directory_headers(directory),
    )
    return json_response(
        response.json(), status_code=response.status_code,
        accept_encoding=request.headers.get("accept-encoding"),
    )


def _raise_upstream_status(exc: httpx.HTTPStatusError, *, sid: str | None = None) -> NoReturn:
    """Map an upstream HTTPStatusError to a structured CodedHTTPException.

    404 on a session-discover call (sid provided) → session_not_found;
    other 4xx → 502 upstream_http_N; 5xx → 503 upstream_unavailable.
    """
    status = exc.response.status_code
    if status == 404 and sid is not None:
        raise CodedHTTPException(404, code="session_not_found", sessionID=sid)
    if status < 500:
        raise CodedHTTPException(502, code=f"upstream_http_{status}")
    raise CodedHTTPException(503, code="upstream_unavailable")


@router.get("/sessions/{sid}/status")
async def session_status(request: Request, sid: str):
    # Discover: GET /session/{sid}
    try:
        session_response = await request.app.state.upstream.get(f"/session/{sid}")
    except httpx.RequestError:
        raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        session_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc, sid=sid)
    try:
        directory = session_response.json().get("directory")
    except Exception:
        raise CodedHTTPException(503, code="upstream_unavailable")
    if not isinstance(directory, str):
        raise CodedHTTPException(503, code="upstream_unavailable")
    # F2: per-session status is a read keyed by sid (capability). allowlist is
    # not a security boundary (stunnel mTLS is); normalize without gating,
    # aligning with /messages/{sid} (G7-soft). Batch /sessions/status still gates.
    directory = normalize_directory(directory)

    # Status map: GET /session/status?directory=...
    try:
        result = await request.app.state.upstream.get(
            "/session/status", params={"directory": directory},
            headers=forward_directory_headers(directory),
        )
    except httpx.RequestError:
        raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        result.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc)
    try:
        mapping = result.json()
    except Exception:
        raise CodedHTTPException(503, code="upstream_unavailable")
    if not isinstance(mapping, dict):
        raise CodedHTTPException(503, code="upstream_unavailable")

    if sid in mapping:
        return json_response(mapping[sid], accept_encoding=request.headers.get("accept-encoding"))
    return json_response({"type": "idle"}, accept_encoding=request.headers.get("accept-encoding"))
