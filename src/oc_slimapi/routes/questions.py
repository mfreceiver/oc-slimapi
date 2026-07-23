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
from ..traffic import stash_up_in, stash_up_out
from ..upstream import decoded_body_headers, forward_directory_headers
from ..directory import normalize_directory

router = APIRouter(prefix="/slimapi", tags=["pending"])


def _request_id(item: dict) -> str | None:
    value = item.get("id") or item.get("requestID")
    return value if isinstance(value, str) else None


async def _aggregate(request: Request, kind: Literal["question", "permission"], directories: list[str] | None):
    if directories is not None:
        # Normalize BEFORE dedupe: `/app` and `/app/` are the same directory
        # once trailing slashes are stripped. Deduping on raw strings would
        # fan-out duplicate upstream calls and inflate scope.directories.
        normalized = [normalize_directory(d) for d in directories]
        unique = list(dict.fromkeys(normalized))
        if not unique or len(unique) > 32:
            raise CodedHTTPException(400, code="invalid_directory_count")
        # slimapi no longer gates directories — pass through to upstream
        # opencode, which decides whether it can serve each directory.
        checked = unique
    else:
        # F1: null = aggregate the sidecar's whole scope (discovered from
        # ``/project``). NOT subject to the 1–32 guard — that constrains
        # client-supplied lists; null means "sidecar's whole scope" sized
        # by ops via opencode project list. The allowlist dataset survives
        # only as a discovery list for this null fan-out (no longer a gate).
        allowlist = request.app.state.directory_allowlist
        if not allowlist:
            try:
                from .sessions import load_products
                await load_products(request.app, traffic_request=request)
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
            # Traffic accounting: per-directory fan-out GET body. Stashed
            # on the parent request so the qp bucket sees aggregate upIn.
            # Must happen BEFORE the 4xx early return so error response
            # bodies are also counted (not just 2xx).
            stash_up_in(request, len(response.content))
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
    # scope only on 200 success envelope (Gap 2B): distinguishes cold-start
    # (directories==0) from authoritative empty (directories>0, items==[]).
    # 503 all-fail keeps the same envelope shape without scope.
    body: dict = {"items": items, "errors": errors}
    if status == 200:
        body["scope"] = {"directories": len(checked)}
    return json_response(body, status_code=status,
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
    # slimapi no longer gates the token's directory — normalize and forward.
    # The token was issued by slimapi for a specific directory, so we honour
    # that signature; upstream opencode decides whether it can serve the dir.
    return normalize_directory(payload["directory"])


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
    # Traffic accounting: the serialized request body + the upstream response
    # body. ``response.request.content`` is exactly what httpx sent on the
    # wire, so no re-serialisation drift.
    sent = getattr(getattr(response, "request", None), "content", None)
    if sent:
        stash_up_out(request, len(sent))
    stash_up_in(request, len(response.content))
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
