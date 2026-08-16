"""Integer client-version gate for the private slim API namespace."""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

from .gzip_util import json_response

SERVER_API_VERSION = 2
# v3 Batch A (2.0.0): accepted client range widens to (2, 3) — the X-Slimapi-
# Version gate admits header=3 alongside header=2 (a client that adopted
# sending header 3 before switching to the ?v=3 selector). The gate LOGIC is
# unchanged (version_required / version_incompatible); only the range moves.
# Config.validate() pins accepted_client_versions to exactly this constant —
# widening/narrowing via env stays forbidden (P1-13 posture preserved).
ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (2, 3)
VERSION_HEADER = "X-Slimapi-Version"

# P1-14: collapse duplicate slashes for the version-gate path decision only.
# ASGI servers may not fold ``//`` → ``/`` (unlike FastAPI's URL parsing), so
# ``//slimapi/foo`` could bypass the version gate (raw path does not start
# with ``/slimapi/``) while the catch-all proxy normalises it later and routes
# it to a /slimapi/ endpoint. This regex is used to normalise the path for the
# GATE DECISION ONLY — ``scope["path"]`` is left unchanged so downstream
# routing (FastAPI / Starlette) sees the original path exactly as the ASGI
# server delivered it.
_SLASH_RE = re.compile(r"/+")


def _is_slimapi_path(path: str) -> bool:
    """Return True if ``path`` targets the ``/slimapi`` namespace.

    Collapses duplicate slashes first (``//slimapi/foo`` → ``/slimapi/foo``)
    so a double-slash prefix cannot bypass the gate. Recognises BOTH the
    exact root ``/slimapi`` and any sub-path ``/slimapi/...`` — the root
    must NOT bypass the gate (it either matches a router or 404s through the
    proxy, but in both cases the version header is checked first).
    """
    normalised = _SLASH_RE.sub("/", path)
    return normalised == "/slimapi" or normalised.startswith("/slimapi/")


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
        if scope["type"] != "http" or not _is_slimapi_path(scope.get("path", "")):
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

        # Negotiate gzip on the 400 body (contract §9 "all JSON routes") via
        # gzip_util.json_response so a client sending Accept-Encoding: gzip
        # gets a gzipped error body, consistent with every other JSON route.
        accept_encoding = headers.get("accept-encoding")
        minimum, maximum = self.accepted
        if client_version is None:
            await json_response(
                {"code": "version_required", "accepted": [minimum, maximum]},
                status_code=400,
                accept_encoding=accept_encoding,
            )(scope, receive, send)
            return
        if not minimum <= client_version <= maximum:
            await json_response(
                {
                    "code": "version_incompatible",
                    "client": client_version,
                    "accepted": [minimum, maximum],
                },
                status_code=400,
                accept_encoding=accept_encoding,
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)
