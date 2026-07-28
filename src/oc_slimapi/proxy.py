import re

from fastapi import FastAPI, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from .directory import validate_directory
from .errors import CodedHTTPException
from .traffic import stash_up_in, stash_up_out
from .upstream import strip_hop_by_hop, DIRECTORY_HEADER

# B0 §1.3 shell/PTY route table (opencode v1.18.3). Hardcoded — do NOT infer.
# - POST /session/{sid}/shell: legacy direct command execution (spawn).
# - /pty, /api/pty: the two PTY trees (list/create/CRUD/connect-token/connect-WS).
# Method-agnostic: deny any method on these paths (defense in depth).
# WebSocket PTY (/pty/{id}/connect upgrade) is already blocked by the global
# WS catch-all (→ 501) below; this guard covers the HTTP-method variants.
_SHELL_PATH_RE = re.compile(r"^/session/[^/]+/shell/?$")


def _normalize_path(path: str) -> str:
    """Collapse duplicate slashes and reject path traversal segments.

    Mirrors opencode's ``ignoreDuplicateSlashes:true`` behaviour and adds
    security rejection of ``..`` / ``.`` segments. Ensures the result
    starts with ``/``.
    """
    # Ensure leading slash
    if not path.startswith("/"):
        path = "/" + path
    # Collapse consecutive slashes
    normalized = re.sub(r"/+", "/", path)
    # Check for path traversal segments
    for segment in normalized.split("/"):
        if segment in {".", ".."}:
            raise CodedHTTPException(400, code="invalid_path")
    return normalized


def _is_shell_path(path: str) -> bool:
    if path == "/pty" or path.startswith("/pty/"):
        return True
    if path == "/api/pty" or path.startswith("/api/pty/"):
        return True
    return _SHELL_PATH_RE.match(path) is not None


# Client-identity headers that must be stripped before forwarding upstream
# (device id could leak PII to opencode if forwarded).
_CLIENT_IDENT_HEADERS = {"x-client-name", "x-client-version", "x-client-id"}


def _strip_client_ident_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove X-Client-Name / X-Client-Version / X-Client-Id (case-insensitive).

    Operates on the already-validated proxy headers dict (returned by
    :func:`strip_hop_by_hop`). Returns a new dict with the client-ident headers
    removed.
    """
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _CLIENT_IDENT_HEADERS
    }


def install_proxy(app: FastAPI) -> None:
    @app.websocket("/{path:path}")
    async def websocket_not_supported(websocket: WebSocket, path: str):
        await websocket.accept()
        await websocket.send_json({"code": "websocket_not_supported", "status": 501})
        await websocket.close(code=1011)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        # Use the fully-normalized URL.path (FastAPI already collapses //)
        raw_path = request.url.path
        # Normalize the path (reject .. / .)
        try:
            norm_path = _normalize_path(raw_path)
        except CodedHTTPException:
            return JSONResponse({"code": "invalid_path"}, status_code=400)

        if norm_path.startswith("/slimapi/"):
            return JSONResponse({"code": "thin_route_not_found"}, status_code=404)

        # Validate X-Opencode-Directory header if present
        dir_header = request.headers.get(DIRECTORY_HEADER)
        if dir_header is not None:
            try:
                validate_directory(dir_header)
            except CodedHTTPException:
                return JSONResponse({"code": "invalid_directory"}, status_code=400)

        # Validate ?directory= query params (catch-all forwards them upstream)
        for dir_val in request.query_params.getlist("directory"):
            try:
                validate_directory(dir_val)
            except CodedHTTPException:
                return JSONResponse({"code": "invalid_directory"}, status_code=400)

        if request.app.state.config.shell_deny_list_enabled and _is_shell_path(norm_path):
            return JSONResponse({"code": "shell_not_allowed"}, status_code=403)
        client = request.app.state.upstream
        # Wrap the downstream request body stream so we count the bytes
        # actually forwarded upstream (``upOut``). Only ``len()`` per chunk —
        # body is not buffered.
        async def _counted_req_stream():
            n = 0
            try:
                async for chunk in request.stream():
                    n += len(chunk)
                    yield chunk
            finally:
                # finally ensures the stash runs even on client disconnect
                # (GeneratorExit/CancelledError mid-stream), preventing lost
                # upOut bytes on interrupted proxy requests.
                if n > 0:
                    stash_up_out(request, n)

        proxy_headers = strip_hop_by_hop(request.headers)
        proxy_headers = _strip_client_ident_headers(proxy_headers)
        rid = request.scope.get("state", {}).get("request_id")
        if rid is not None:
            proxy_headers["X-Request-ID"] = rid
        upstream_request = client.build_request(
            request.method,
            norm_path,  # norm_path already starts with /
            params=request.query_params.multi_items(),
            headers=proxy_headers,
            content=_counted_req_stream(),
        )
        is_sse = norm_path in {"/event", "/global/event"}
        is_command = norm_path.endswith("/command")
        upstream_request.extensions["timeout"] = {
            "connect": 5.0,
            "read": None if is_sse else (300.0 if is_command else 30.0),
            "write": 300.0,
            "pool": 5.0,
        }
        response = await client.send(upstream_request, stream=True)
        # Wrap the upstream response iterator so we count the bytes returned
        # to the client (``upIn`` — the upstream leg of THIS request). The
        # finally guarantees the count lands even on disconnect / error mid
        # stream. BackgroundTask still owns response.aclose().
        async def _counted_upstream_response():
            n = 0
            try:
                async for chunk in response.aiter_raw():
                    n += len(chunk)
                    yield chunk
            finally:
                if n > 0:
                    stash_up_in(request, n)

        return StreamingResponse(
            _counted_upstream_response(),
            status_code=response.status_code,
            headers=strip_hop_by_hop(response.headers),
            background=BackgroundTask(response.aclose),
        )
