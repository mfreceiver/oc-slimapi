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

# P1-31: minimum body size for gzip to be worth attempting. Bodies below this
# threshold are returned raw because gzip's fixed header/footer overhead
# (~18 bytes + deflate framing) almost always makes them LARGER. The version
# gate 400 body (~44 bytes) and short error codes (~31 bytes) are canonical
# examples. This is a CPU optimisation — the ``compress_if_beneficial`` size
# comparison below catches any larger-but-incompressible body regardless.
MIN_GZIP_BYTES = 64


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


def compress_if_beneficial(
    body: bytes, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Gzip ``body`` only when the client accepts it AND it is beneficial.

    Returns ``(payload, headers)`` where ``headers`` always includes
    ``Vary: Accept-Encoding`` and includes ``Content-Encoding: gzip`` only
    when compression was actually applied.

    Three gates (P1-31):

    1. **Negotiation**: the client must accept gzip (:func:`accepts_gzip`).
    2. **Minimum size**: the raw body must exceed :data:`MIN_GZIP_BYTES`.
       Below this, the gzip header/footer overhead makes the body larger.
    3. **Actual benefit**: the compressed result must be strictly smaller
       than the raw body. Incompressible data (even above the threshold) can
       expand under gzip; this check catches it and returns the raw body.

    NOTE: this function is always called on a freshly ``orjson.dumps``'d body
    — there is never a pre-existing ``Content-Encoding`` to double-compress.
    If a caller ever passes an already-compressed body, gate 3 prevents
    re-compression (compressed input is incompressible → result >= input).
    """
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    if not accepts_gzip(accept_encoding):
        return body, headers
    if len(body) < MIN_GZIP_BYTES:
        return body, headers
    compressed = gzip.compress(body, compresslevel=6)
    if len(compressed) >= len(body):
        return body, headers
    headers["Content-Encoding"] = "gzip"
    return compressed, headers


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
