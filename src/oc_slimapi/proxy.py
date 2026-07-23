import re

from fastapi import FastAPI, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from .traffic import stash_up_in, stash_up_out
from .upstream import strip_hop_by_hop

# B0 §1.3 shell/PTY route table (opencode v1.18.3). Hardcoded — do NOT infer.
# - POST /session/{sid}/shell: legacy direct command execution (spawn).
# - /pty, /api/pty: the two PTY trees (list/create/CRUD/connect-token/connect-WS).
# Method-agnostic: deny any method on these paths (defense in depth).
# WebSocket PTY (/pty/{id}/connect upgrade) is already blocked by the global
# WS catch-all (→ 501) below; this guard covers the HTTP-method variants.
_SHELL_PATH_RE = re.compile(r"^/session/[^/]+/shell/?$")


def _is_shell_path(path: str) -> bool:
    if path == "/pty" or path.startswith("/pty/"):
        return True
    if path == "/api/pty" or path.startswith("/api/pty/"):
        return True
    return _SHELL_PATH_RE.match(path) is not None


def install_proxy(app: FastAPI) -> None:
    @app.websocket("/{path:path}")
    async def websocket_not_supported(websocket: WebSocket, path: str):
        await websocket.accept()
        await websocket.send_json({"code": "websocket_not_supported", "status": 501})
        await websocket.close(code=1011)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        if request.url.path.startswith("/slimapi/"):
            return JSONResponse({"code": "thin_route_not_found"}, status_code=404)
        if request.app.state.config.shell_deny_list_enabled and _is_shell_path(request.url.path):
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

        upstream_request = client.build_request(
            request.method,
            f"/{path}",
            params=request.query_params.multi_items(),
            headers=strip_hop_by_hop(request.headers),
            content=_counted_req_stream(),
        )
        is_sse = request.url.path in {"/event", "/global/event"}
        is_command = request.url.path.endswith("/command")
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
