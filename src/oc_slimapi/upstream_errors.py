"""Upstream GET with structured error mapping per contract §7.

Provides :func:`fetch_json_mapped` — a single-call helper that wraps upstream
GET + error mapping + JSON parsing + shape validation, raising
:class:`CodedHTTPException` on any issue. Success returns the parsed JSON dict.

Used by batch status (no sid) and by Batch 3 children endpoints (with sid).
"""

from __future__ import annotations

from typing import Any

import httpx

from .errors import CodedHTTPException


async def fetch_json_mapped(
    upstream: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    sid: str | None = None,
    expect: type = dict,
) -> dict[str, Any] | list[Any]:
    """Fetch from upstream and map errors per contract §7.

    Raises CodedHTTPException on any error; returns the parsed JSON dict on
    success. The caller does NOT need to catch anything — just use the
    returned dict.

    For no-sid calls (batch / list): upstream 404 → 502 ``upstream_http_404``,
    **not** ``session_not_found``. For sid-scoped calls: upstream 404 → 404
    ``session_not_found`` (with ``sessionID`` field).
    """
    try:
        response = await upstream.get(path, params=params, headers=headers)
    except httpx.RequestError:
        raise CodedHTTPException(503, code="upstream_unavailable")
    if response.status_code >= 400:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404 and sid is not None:
                raise CodedHTTPException(404, code="session_not_found", sessionID=sid)
            if status < 500:
                raise CodedHTTPException(502, code=f"upstream_http_{status}")
            raise CodedHTTPException(503, code="upstream_unavailable")
        except Exception:
            raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        payload = response.json()
    except Exception:
        raise CodedHTTPException(503, code="upstream_unavailable")
    if not isinstance(payload, expect):
        raise CodedHTTPException(503, code="upstream_unavailable")
    return payload
