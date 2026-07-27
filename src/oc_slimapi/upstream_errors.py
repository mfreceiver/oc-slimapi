"""Upstream error mapping per contract §7.

Provides :func:`raise_upstream_status` — mapping an HTTPStatusError to a
:class:`CodedHTTPException` (used by sessions and messages routes).
"""

from __future__ import annotations

from typing import NoReturn

import httpx

from .errors import CodedHTTPException


def raise_upstream_status(exc: httpx.HTTPStatusError, *, sid: str | None = None) -> NoReturn:
    """Map an upstream HTTPStatusError to a structured CodedHTTPException.

    404 on a session-discover call (sid provided) → session_not_found;
    other 4xx → 502 upstream_http_N; 5xx → 503 upstream_unavailable.
    """
    status = exc.response.status_code
    if status == 404 and sid is not None:
        raise CodedHTTPException(404, code="session_not_found", sessionID=sid) from exc
    if status < 500:
        raise CodedHTTPException(502, code=f"upstream_http_{status}") from exc
    raise CodedHTTPException(503, code="upstream_unavailable") from exc
