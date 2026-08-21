"""Shared router + cross-family helpers for the ``/slimapi/messages/{sid}``
endpoint package (F-302 three-family split of the historical single
``routes/messages.py``; pure move, zero behaviour change).

The three family modules — :mod:`._list`, :mod:`._full_merge` and
:mod:`._expand` — all decorate THIS single shared ``router`` object, so
``messages.router`` exposes the same route set as the pre-split module.
Helpers consumed by more than one family (``_busy_response``,
``_resolve_messages_directory``) live here, the shared ancestor with no
sibling-module imports.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from ...errors import CodedHTTPException
from ...gzip_util import error_response
from ...selector import resolve_route_directory
from ...directory import validate_directory

router = APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])

# Fixed Retry-After for transform admission timeouts. Kept as a module constant
# so tests and the route agree on the wire contract.
TRANSFORM_RETRY_AFTER_SECONDS = 2


def _busy_response(accept_encoding: str | None = None) -> Response:
    """503 + ``Retry-After`` — emitted when the transform pool admission
    times out.

    Routed through :func:`error_response` so the body honours gzip
    negotiation (contract §9) when the client sent ``Accept-Encoding: gzip``.
    ``error_response`` sets ``Vary: Accept-Encoding`` (and Content-Encoding
    when gzip is negotiated); ``Retry-After`` is appended afterward because
    it is a transport header, not a body field.
    """
    response = error_response(
        "transform_busy", 503,
        accept_encoding=accept_encoding,
        retry_after=TRANSFORM_RETRY_AFTER_SECONDS,
    )
    response.headers["Retry-After"] = str(TRANSFORM_RETRY_AFTER_SECONDS)
    return response


async def _resolve_messages_directory(request: Request, directory: str | None) -> str | None:
    """Resolve query ``directory`` to a normalised value to forward upstream.

    slimapi no longer gates directories — any directory is forwarded to
    upstream opencode (which decides whether it can serve it). The two
    structural checks below are kept:

    - ``directory is None`` → not blocked (returns None; upstream default applies).
      v1 only trusts query ``directory``; a lone ``X-Opencode-Directory`` header
      is not validated and not forwarded (unchanged behaviour).
    - query present AND header present AND they differ → 400 ``directory_not_allowed``
      (defensive: the conflict is structurally ambiguous, regardless of which
      directories are involved — slimapi refuses to guess which one to forward).

    Returns the normalised directory to forward (or None).

    v3 (§5, Batch B): the dispatch selector already consumed + validated the
    ``?directory=`` query on consuming routes and stripped it from the query
    — ``resolve_route_directory`` substitutes that stash for the (absent)
    query param, so the rest of this resolver runs unchanged on an
    already-validated value (header conflicts were decided at dispatch).
    """
    directory = resolve_route_directory(request.scope, directory)
    if directory is None:
        return None
    header_dir = request.headers.get("x-opencode-directory")
    if header_dir:  # treat empty header as absent
        if (header_dir.rstrip("/") or "/") != (directory.rstrip("/") or "/"):
            raise CodedHTTPException(400, code="directory_not_allowed")
    return validate_directory(directory)
