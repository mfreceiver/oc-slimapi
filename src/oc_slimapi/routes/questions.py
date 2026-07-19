from __future__ import annotations

import asyncio
from typing import Literal

import httpx
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..tokens import RouteTokenError, issue_route_token, verify_route_token
from ..upstream import decoded_body_headers, forward_directory_headers
from .sessions import require_directory, load_products

router = APIRouter(prefix="/slimapi", tags=["pending"])


def _request_id(item: dict) -> str | None:
    value = item.get("id") or item.get("requestID")
    return value if isinstance(value, str) else None


async def _aggregate(request: Request, kind: Literal["question", "permission"], directories: list[str] | None):
    if directories is not None:
        unique = list(dict.fromkeys(directories))
        if not unique or len(unique) > 32:
            raise CodedHTTPException(400, code="invalid_directory_count")
        checked = [await require_directory(request, d) for d in unique]
    else:
        # F1: null = aggregate the sidecar's whole scope (allowlist). NOT subject
        # to the 1–32 guard — that constrains client-supplied lists; null means
        # "sidecar's whole scope" sized by ops via opencode project list.
        allowlist = request.app.state.directory_allowlist
        if not allowlist:
            try:
                await load_products(request.app)
            except Exception:
                pass
            allowlist = request.app.state.directory_allowlist
        checked = sorted(allowlist)  # deterministic; may be []

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
                    request.app.state.route_secret, kind=kind,
                    request_id=_request_id(item), session_id=item.get("sessionID"),
                    directory=directory,
                )
                output.append(enriched)
            return output, None
        except httpx.TimeoutException:
            return None, {"directory": directory, "code": "upstream_timeout"}
        except Exception:
            return None, {"directory": directory, "code": "upstream_error"}

    results = await asyncio.gather(*(fetch(d) for d in checked))
    items = [item for group, _ in results if group for item in group]
    errors = [e for _, e in results if e]
    status = 503 if results and len(errors) == len(results) else 200
    return json_response({"items": items, "errors": errors}, status_code=status,
                         accept_encoding=request.headers.get("accept-encoding"))


@router.get("/questions")
async def questions(request: Request, directory: list[str] | None = Query(None)):
    return await _aggregate(request, "question", directory)


@router.get("/permissions")
async def permissions(request: Request, directory: list[str] | None = Query(None)):
    return await _aggregate(request, "permission", directory)


class ReplyBody(BaseModel):
    answers: list[list[str]]
    routeToken: str


class TokenBody(BaseModel):
    routeToken: str


class PermissionBody(BaseModel):
    response: Literal["once", "always", "reject"]
    routeToken: str


async def _token(
    request: Request, token: str, kind: str, request_id: str, session_id: str | None = None,
) -> str:
    try:
        payload = verify_route_token(
            token, request.app.state.route_secret, kind=kind,
            request_id=request_id, session_id=session_id,
        )
    except RouteTokenError as exc:
        raise CodedHTTPException(400, code="invalid_route_token") from exc
    return await require_directory(request, payload["directory"])


async def _post(request: Request, path: str, directory: str, body: dict):
    try:
        response = await request.app.state.upstream.post(
            path, params={"directory": directory},
            headers=forward_directory_headers(directory), json=body, timeout=30.0,
        )
    except httpx.TimeoutException as exc:
        raise CodedHTTPException(
            504, code="upstream_timeout",
            message="upstream mutation timed out; not retried",
        ) from exc
    if response.status_code == 404:
        return Response(response.content, 404, headers=decoded_body_headers(response.headers))
    if response.status_code == 400:
        return Response(response.content, 400, headers=decoded_body_headers(response.headers))
    if response.status_code >= 300:
        return Response(response.content, response.status_code, headers=decoded_body_headers(response.headers))
    return Response(status_code=204)


@router.post("/questions/{qid}/reply")
async def reply(request: Request, qid: str, body: ReplyBody):
    directory = await _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reply", directory, {"answers": body.answers})


@router.post("/questions/{qid}/reject")
async def reject(request: Request, qid: str, body: TokenBody):
    directory = await _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reject", directory, {})


@router.post("/sessions/{sid}/permissions/{pid}")
async def permission(request: Request, sid: str, pid: str, body: PermissionBody):
    directory = await _token(request, body.routeToken, "permission", pid, sid)
    return await _post(
        request, f"/session/{sid}/permissions/{pid}", directory,
        {"response": body.response},
    )
