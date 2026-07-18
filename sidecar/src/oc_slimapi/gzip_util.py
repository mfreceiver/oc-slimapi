"""Explicit gzip helpers. Streaming/SSE responses never call this module."""

from __future__ import annotations

import gzip
from typing import Any

import orjson
from starlette.responses import Response


def json_response(
    value: Any,
    *,
    accept_encoding: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    body = orjson.dumps(value)
    output_headers = dict(headers or {})
    output_headers["Vary"] = "Accept-Encoding"
    if "gzip" in (accept_encoding or "").lower():
        body = gzip.compress(body, compresslevel=6)
        output_headers["Content-Encoding"] = "gzip"
    return Response(body, status_code=status_code, media_type="application/json", headers=output_headers)


def error_response(
    code: str,
    status_code: int,
    *,
    accept_encoding: str | None = None,
    **fields: Any,
) -> Response:
    """JSON error body with optional gzip negotiation (contract §9).

    ``accept_encoding`` defaults to ``None`` so existing call sites keep the
    pre-gzip behaviour (no Content-Encoding). When truthy and containing
    ``gzip``, the body is compressed via the same path as :func:`json_response`
    — no duplicated gzip logic.
    """
    return json_response(
        {"code": code, **fields},
        status_code=status_code,
        accept_encoding=accept_encoding,
    )
