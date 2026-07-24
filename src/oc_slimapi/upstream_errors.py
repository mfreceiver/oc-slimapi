"""Upstream GET with structured error mapping per contract §7.

Provides :func:`fetch_json_mapped` — a single-call helper that wraps upstream
GET + error mapping + JSON parsing + shape validation, raising
:class:`CodedHTTPException` on any issue. Success returns the parsed JSON dict.

Also provides :func:`raise_upstream_status` — mapping an HTTPStatusError to a
:class:`CodedHTTPException` (used by sessions and messages routes).

Used by batch status (no sid) and by Batch 3 children endpoints (with sid).
"""

from __future__ import annotations

from typing import Any, NoReturn

import httpx

from .errors import CodedHTTPException
from .traffic import stash_up_in


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




async def fetch_json_mapped(
    upstream: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    sid: str | None = None,
    expect: type = dict,
    traffic_request: Any = None,
) -> dict[str, Any] | list[Any]:
    """Fetch from upstream and map errors per contract §7.

    Raises CodedHTTPException on any error; returns the parsed JSON dict on
    success. The caller does NOT need to catch anything — just use the
    returned dict.

    For no-sid calls (batch / list): upstream 404 → 502 ``upstream_http_404``,
    **not** ``session_not_found``. For sid-scoped calls: upstream 404 → 404
    ``session_not_found`` (with ``sessionID`` field).

    ``traffic_request`` (optional): when given, the raw upstream response body
    byte length is stashed into the request's traffic accounting state so the
    ASGI middleware attributes ``upIn`` for the request's bucket at request
    end. Pass ``None`` (the default) for non-request-scoped callers such as
    background cache fills.
    """
    try:
        response = await upstream.get(path, params=params, headers=headers)
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    # Stash the upstream response body length BEFORE error mapping and
    # parsing (httpx has already buffered it for a non-streaming .get()).
    # Best-effort; ignore callers that pass traffic_request without a
    # scope (defensive). Counts both success and error response bodies
    # so upstream HTTP errors are not missing from the byte ledger.
    if traffic_request is not None:
        try:
            stash_up_in(traffic_request, len(response.content))
        except Exception as exc:
            pass  # swallow logging; no structured error needed
    if response.status_code >= 400:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404 and sid is not None:
                raise CodedHTTPException(404, code="session_not_found", sessionID=sid) from exc
            if status < 500:
                raise CodedHTTPException(502, code=f"upstream_http_{status}") from exc
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
        except Exception as exc:
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    if not isinstance(payload, expect):
        raise CodedHTTPException(503, code="upstream_unavailable")
    return payload
