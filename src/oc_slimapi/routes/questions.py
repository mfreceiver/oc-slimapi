from __future__ import annotations

import asyncio
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from ..gzip_util import json_response
from ..tokens import RouteTokenError, issue_route_token, verify_route_token
from ..upstream import decoded_body_headers, forward_directory_headers
from .sessions import require_directory

router = APIRouter(prefix="/slimapi", tags=["pending"])


def _request_id(item: dict) -> str | None:
    value = item.get("id") or item.get("requestID")
    return value if isinstance(value, str) else None


async def _aggregate(request: Request, kind: Literal["question", "permission"], directories: list[str]):
    unique = list(dict.fromkeys(directories))
    if not unique or len(unique) > 32:
        raise HTTPException(400, "directory must be repeated 1-32 times")
    checked = [await require_directory(request, directory) for directory in unique]

    async def fetch(directory: str):
        try:
            response = await request.app.state.upstream.get(
                f"/{kind}", params={"directory": directory},
                headers=forward_directory_headers(directory), timeout=2.0,
            )
            if response.status_code >= 400:
                return None, {"directory": directory, "code": f"upstream_http_{response.status_code}"}
            output = []
            for item in response.json():
                if not isinstance(item, dict) or not _request_id(item):
                    continue
                enriched = dict(item)
                enriched["directory"] = directory
                enriched["routeToken"] = issue_route_token(
                    request.app.state.route_secret,
                    kind=kind,
                    request_id=_request_id(item),
                    session_id=item.get("sessionID"),
                    directory=directory,
                )
                output.append(enriched)
            return output, None
        except httpx.TimeoutException:
            return None, {"directory": directory, "code": "upstream_timeout"}
        except Exception:
            return None, {"directory": directory, "code": "upstream_error"}

    results = await asyncio.gather(*(fetch(directory) for directory in checked))
    items = [item for group, _ in results if group for item in group]
    errors = [error for _, error in results if error]
    status = 503 if len(errors) == len(results) else 200
    return json_response(
        {"items": items, "errors": errors}, status_code=status,
        accept_encoding=request.headers.get("accept-encoding"),
    )


@router.get("/questions")
async def questions(request: Request, directory: list[str] = Query(...)):
    return await _aggregate(request, "question", directory)


@router.get("/permissions")
async def permissions(request: Request, directory: list[str] = Query(...)):
    return await _aggregate(request, "permission", directory)


class ReplyBody(BaseModel):
    answers: list[list[str]]
    routeToken: str


class TokenBody(BaseModel):
    routeToken: str


class PermissionBody(BaseModel):
    response: Literal["once", "always", "reject"]
    routeToken: str


def _token(request: Request, token: str, kind: str, request_id: str, session_id: str | None = None):
    try:
        payload = verify_route_token(
            token, request.app.state.route_secret, kind=kind,
            request_id=request_id, session_id=session_id,
        )
    except RouteTokenError as exc:
        raise HTTPException(400, str(exc)) from exc
    directory = payload["directory"]
    if directory not in request.app.state.directory_allowlist:
        raise HTTPException(400, "token directory is no longer allowed")
    return directory


async def _post(request: Request, path: str, directory: str, body: dict):
    try:
        response = await request.app.state.upstream.post(
            path, params={"directory": directory},
            headers=forward_directory_headers(directory), json=body, timeout=30.0,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "upstream mutation timed out; not retried") from exc
    if response.status_code == 404:
        return Response(response.content, 404, headers=decoded_body_headers(response.headers))
    if response.status_code == 400:
        return Response(response.content, 400, headers=decoded_body_headers(response.headers))
    if response.status_code >= 300:
        return Response(response.content, response.status_code, headers=decoded_body_headers(response.headers))
    return Response(status_code=204)


@router.post("/questions/{qid}/reply")
async def reply(request: Request, qid: str, body: ReplyBody):
    directory = _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reply", directory, {"answers": body.answers})


@router.post("/questions/{qid}/reject")
async def reject(request: Request, qid: str, body: TokenBody):
    directory = _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reject", directory, {})


@router.post("/sessions/{sid}/permissions/{pid}")
async def permission(request: Request, sid: str, pid: str, body: PermissionBody):
    directory = _token(request, body.routeToken, "permission", pid, sid)
    return await _post(
        request, f"/session/{sid}/permissions/{pid}", directory,
        {"response": body.response},
    )
