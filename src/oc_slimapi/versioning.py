"""Integer client-version gate for the private slim API namespace."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SERVER_API_VERSION = 1
ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (2, 2)
VERSION_HEADER = "X-Slimapi-Version"


class SlimapiVersionMiddleware:
    """Require a compatible version header only for HTTP /slimapi/** calls."""

    def __init__(
        self,
        app: ASGIApp,
        accepted_client_versions: tuple[int, int] = ACCEPTED_CLIENT_VERSIONS,
    ) -> None:
        self.app = app
        self.accepted = accepted_client_versions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/slimapi/"):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw_version = headers.get(VERSION_HEADER.lower())
        try:
            client_version = int(raw_version) if raw_version is not None else None
        except ValueError:
            client_version = None

        minimum, maximum = self.accepted
        if client_version is None:
            await JSONResponse(
                {"code": "version_required", "accepted": [minimum, maximum]},
                status_code=400,
            )(scope, receive, send)
            return
        if not minimum <= client_version <= maximum:
            await JSONResponse(
                {
                    "code": "version_incompatible",
                    "client": client_version,
                    "accepted": [minimum, maximum],
                },
                status_code=400,
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)
