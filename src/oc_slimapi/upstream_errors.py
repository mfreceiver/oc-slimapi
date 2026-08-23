"""Upstream error mapping per contract §7.

Centralises the sidecar's upstream→structured-error mapping so every route
module shares a single source of truth for the §7 codes instead of inlining
``CodedHTTPException(...)`` literals:

* network failure / 5xx / non-list body / JSON decode failure →
  503 ``upstream_unavailable`` — :func:`raise_upstream_unavailable`;
* HTTP status mapping **after draining the error body** —
  :func:`raise_upstream_status_code` (4xx → 502 ``upstream_http_N``;
  5xx → 503 ``upstream_unavailable``; 404 with ``sid`` → 404
  ``session_not_found`` via the :func:`session_not_found_error` factory);
* the legacy :func:`raise_upstream_status` (takes an ``HTTPStatusError``
  from ``response.raise_for_status()``) — used by the buffered
  ``upstream.get()`` routes (e.g. ``GET /slimapi/sessions/status``).

Routes that isolate per-call failures into an envelope instead of raising
(``GET /slimapi/questions`` fan-out) use :func:`upstream_error_code_for_status`
to obtain the code *string*.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

import httpx

from .errors import CodedHTTPException

# Contract §7 code string — single source of truth (referenced by routes that
# stamp the code into an envelope without raising, e.g. questions fan-out).
UPSTREAM_UNAVAILABLE = "upstream_unavailable"

# Contract §7 session-scoped miss — single source of truth for the 404
# ``session_not_found`` shape (body code + ``sessionID`` field). Used by both
# status-mapping helpers below AND the read-groups DB point-query miss.
SESSION_NOT_FOUND = "session_not_found"


def session_not_found_error(sid: str) -> CodedHTTPException:
    """Build the §7 sid-scoped miss: 404 ``session_not_found`` (+ sessionID).

    Exception FACTORY (does not raise): every construction site keeps control
    of its own cause semantics — ``raise session_not_found_error(sid)`` where
    no underlying exception exists (drained body / DB point-query miss) and
    ``raise session_not_found_error(sid) from exc`` where traceback continuity
    matters (``raise_for_status`` routes).
    """
    return CodedHTTPException(404, code=SESSION_NOT_FOUND, sessionID=sid)


def raise_upstream_unavailable(
    exc: BaseException | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> NoReturn:
    """Raise 503 ``upstream_unavailable``, optionally chained from ``exc``.

    Covers every non-status upstream failure: initial-send network errors
    (``httpx.RequestError``), mid-stream read failures, JSON decode errors,
    non-list / non-dict bodies, and explicit 5xx-after-drain. Passing
    ``exc`` preserves ``raise ... from exc`` exception chaining for
    diagnostics (the route sites that currently chain keep chaining).

    ``headers`` (keyword-only) attaches extra response headers — e.g. the
    §12.5.3 ``Cache-Control: no-store`` stamp the read-groups v4 pipeline
    requires on every error. The mapping is COPIED (``dict(headers)``) so
    no caller-shared mutable mapping can leak between raises; omitted (the
    default) keeps the prior bare-exception shape byte-identical.
    """
    extra: dict[str, dict[str, str]] = {}
    if headers is not None:
        extra["headers"] = dict(headers)
    if exc is not None:
        raise CodedHTTPException(503, code=UPSTREAM_UNAVAILABLE, **extra) from exc
    raise CodedHTTPException(503, code=UPSTREAM_UNAVAILABLE, **extra)


def upstream_error_code_for_status(status: int) -> str:
    """Return the contract §7 code *string* for an upstream HTTP status.

    5xx → ``upstream_unavailable``; 4xx → ``upstream_http_N``. Used by routes
    that isolate per-call failures into an envelope (e.g. the
    ``/slimapi/questions`` per-directory fan-out) instead of raising — the
    caller stamps the returned string into its ``errors[]`` array. Raising
    routes use :func:`raise_upstream_status_code` instead.
    """
    if status >= 500:
        return UPSTREAM_UNAVAILABLE
    return f"upstream_http_{status}"


def raise_upstream_status_code(
    status: int, *, sid: str | None = None,
) -> NoReturn:
    """Map a drained upstream HTTP status to a structured CodedHTTPException.

    Call this AFTER reading (draining) the upstream error body for
    connection reuse. ``sid`` is the session id for session-scoped endpoints:
    a 404 with ``sid`` → 404 ``session_not_found`` (so the client can map a
    stale session); any other 4xx → 502 ``upstream_http_N``; 5xx → 503
    ``upstream_unavailable``. No ``sid`` (catalog / list endpoints) ⇒ 404 is
    reported as ``upstream_http_404`` like any other 4xx.

    This is the non-chaining variant: the error body has already been
    consumed, so there is no natural ``exc`` to chain. The
    ``HTTPStatusError``-bearing variant :func:`raise_upstream_status` chains
    ``from exc`` for traceback continuity.
    """
    if status == 404 and sid is not None:
        raise session_not_found_error(sid)
    if status < 500:
        raise CodedHTTPException(502, code=f"upstream_http_{status}")
    raise_upstream_unavailable()


def raise_upstream_status(
    exc: httpx.HTTPStatusError, *, sid: str | None = None,
) -> NoReturn:
    """Map an upstream ``HTTPStatusError`` (from ``response.raise_for_status()``)
    to a structured CodedHTTPException.

    Same status mapping as :func:`raise_upstream_status_code` but takes the
    exception directly and chains ``from exc`` (used by the buffered
    ``upstream.get()`` + ``raise_for_status()`` pattern, e.g.
    ``GET /slimapi/sessions/status``). Streaming routes that drain the error
    body first should call :func:`raise_upstream_status_code` with the raw
    status code instead.
    """
    status = exc.response.status_code
    if status == 404 and sid is not None:
        raise session_not_found_error(sid) from exc
    if status < 500:
        raise CodedHTTPException(502, code=f"upstream_http_{status}") from exc
    raise CodedHTTPException(503, code=UPSTREAM_UNAVAILABLE) from exc
