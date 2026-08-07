"""Explicit gzip helpers. Streaming/SSE responses never call this module.

The single negotiation primitive :func:`accepts_gzip` is the authority for
"should we gzip this JSON body?". Every gzip decision site in the sidecar
(thin routes, the transform worker, the token-stream route, the version gate)
MUST route through it so the ``Accept-Encoding`` semantics are interpreted
identically everywhere — in particular so an explicit ``gzip;q=0`` (RFC 7231
"do NOT use gzip") is honoured rather than matched as a substring.
"""

from __future__ import annotations

import gzip
from typing import Any

import orjson
from starlette.responses import Response


def accepts_gzip(accept_encoding: str | None) -> bool:
    """Return True iff the client's ``Accept-Encoding`` permits gzip.

    Parses the header per RFC 7231 §5.3.4 q-value semantics:

    * ``gzip`` / ``gzip;q=1`` → accepted.
    * ``gzip;q=0`` → explicitly REFUSED (the prior substring match ``"gzip"
      in header`` wrongly compressed these — the P3-1 correctness fix).
    * ``*;q=N`` wildcard → governs gzip only when no explicit ``gzip``
      coding is present (explicit ``gzip;q=0`` overrides a wildcard).
    * ``x-gzip`` is treated as ``gzip`` (legacy synonym; preserves prior
      lenient behaviour for the loopback sidecar).
    * Empty / None / no gzip-or-wildcard coding → False (sidecar never
      advertises gzip on its own).

    A malformed ``q=`` token is ignored (coding keeps the default q=1.0);
    this is best-effort negotiation, not a strict parser.
    """
    if not accept_encoding:
        return False
    gzip_q: float | None = None
    star_q: float | None = None
    for raw_part in accept_encoding.split(","):
        part = raw_part.strip()
        if not part:
            continue
        tokens = part.split(";")
        coding = tokens[0].strip().lower()
        q = 1.0
        for param in tokens[1:]:
            p = param.strip()
            if p[:2].lower() == "q=":
                try:
                    q = float(p[2:])
                except ValueError:
                    pass  # malformed q → keep default 1.0 (best-effort)
        if coding in ("gzip", "x-gzip"):
            gzip_q = q
        elif coding == "*":
            star_q = q
    if gzip_q is not None:
        return gzip_q > 0
    if star_q is not None:
        return star_q > 0
    return False


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
    if accepts_gzip(accept_encoding):
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
