"""Pure-ASGI traffic-accounting middleware.

Counts downstream HTTP req/resp bytes by wrapping ``receive`` / ``send``, and
attributes upstream bytes (stashed by route handlers via
:func:`oc_slimapi.traffic.stash_up_in` / :func:`stash_up_out`) at request end.
Writes one JSON-lines access log entry per request via the
``oc_slimapi.access`` logger.

Pure-ASGI (NOT :class:`starlette.middleware.base.BaseHTTPMiddleware`): the
BaseHTTPMiddleware re-reads the response body to dispatch it, which has known
issues with :class:`StreamingResponse` and long-lived SSE connections
(first-byte delay, buffering, leaked tasks). This implementation just wraps
the ``receive`` and ``send`` callables, accumulates ``len(chunk)``, and
forwards every chunk unmodified to the inner app — so SSE token streams and
the catch-all reverse proxy keep streaming exactly as before.

SSE buckets (``events_sse`` / ``token_stream_sse``): the middleware still
bumps ``requests`` + ``downIn`` and writes the access log line (so an operator
sees per-connection lifetime / wire bytes), but passes ``resp_bytes=0`` to
:meth:`TrafficLedger.record_downstream` so the SSE per-frame counters
(:meth:`record_sse_downstream`, called by the SSE generators) own the
``downOut`` aggregation without double-counting. Stashed ``up_in`` / ``up_out``
are also ignored for SSE buckets — upstream bytes for the shared
``/global/event`` stream are accounted by :meth:`record_sse_upstream` in the
hub.

When no ``traffic_ledger`` is wired into ``app.state`` (e.g. test apps that
don't construct one) the middleware is a pass-through: it still wraps
receive/send and writes the access log if the logger has handlers, but
``record_*`` calls are skipped. This keeps the surface fully additive to
existing test fixtures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from ..access_log import get_access_logger, write_access_log
from ..logging_config import get_logger
from ..traffic import SSE_BUCKETS, _read_state_int, _UP_IN_KEY, _UP_OUT_KEY, bucketize
from .request_id import REQUEST_ID_KEY

logger = get_logger("middleware.traffic_accounting")

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _ledger_from_scope(scope: dict[str, Any]) -> Any:
    """Best-effort lookup of the per-app TrafficLedger. Returns None if absent."""
    app = scope.get("app")
    if app is None:
        return None
    state = getattr(app, "state", None)
    if state is None:
        return None
    return getattr(state, "traffic_ledger", None)


class TrafficAccountingMiddleware:
    """Outermost pure-ASGI middleware counting downstream + upstream bytes."""

    __slots__ = ("app", "logger")

    def __init__(
        self,
        app: Any,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.app = app
        self.logger = logger if logger is not None else get_access_logger()

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            # lifespan / ws / etc. — pass through untouched.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "") or ""
        path = scope.get("path", "") or ""
        bucket = bucketize(method, path)
        is_sse = bucket in SSE_BUCKETS

        down_in = 0
        status_code = 0
        down_out = 0
        content_type: str | None = None
        start_perf = time.perf_counter()

        async def counted_receive() -> dict[str, Any]:
            nonlocal down_in
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if body:
                    # Only len() — never materialise the body.
                    down_in += len(body)
            return message

        async def counted_send(message: dict[str, Any]) -> None:
            nonlocal status_code, down_out, content_type
            mtype = message.get("type")
            if mtype == "http.response.start":
                status_code = int(message.get("status", 0) or 0)
                # Capture content-type so we can distinguish real SSE streams
                # (200 + text/event-stream) from error responses (e.g. 400/503)
                # on SSE paths which should still count downOut normally.
                raw_headers: list[tuple[bytes, bytes]] = message.get("headers") or []
                for name, value in raw_headers:
                    if name.lower() == b"content-type":
                        content_type = value.decode("utf-8", "replace").lower()
                        break
            elif mtype == "http.response.body":
                body = message.get("body", b"")
                if body:
                    down_out += len(body)
            await send(message)

        try:
            await self.app(scope, counted_receive, counted_send)
        except BaseException:
            # Always record best-effort before re-raising — disconnects / 500s
            # still count. ``status_code or 500`` matches the wire outcome on
            # an unhandled exception.
            _record(
                scope=scope,
                bucket=bucket,
                method=method,
                path=path,
                status=status_code or 500,
                down_in=down_in,
                down_out=down_out,
                start_perf=start_perf,
                is_sse=is_sse,
                content_type=content_type,
                logger=self.logger,
            )
            raise
        _record(
            scope=scope,
            bucket=bucket,
            method=method,
            path=path,
            status=status_code,
            down_in=down_in,
            down_out=down_out,
            start_perf=start_perf,
            is_sse=is_sse,
            content_type=content_type,
            logger=self.logger,
        )


def _record(
    *,
    scope: dict[str, Any],
    bucket: str,
    method: str,
    path: str,
    status: int,
    down_in: int,
    down_out: int,
    start_perf: float,
    is_sse: bool,
    content_type: str | None = None,
    logger: logging.Logger,
) -> None:
    """Emit access log + record downstream/upstream into the ledger.

    Swallows every exception — accounting is best-effort and must NEVER break
    a real request (the access log already ran the inner app to completion
    by the time we get here).

    For SSE buckets: ``resp_bytes=0`` is only passed when the response is a
    genuine SSE stream (``status == 200`` and ``content-type`` contains
    ``text/event-stream``), so the per-frame counters in
    :meth:`record_sse_downstream` own the ``downOut`` aggregation. Non-stream
    error responses on SSE paths (e.g. 400 ``directory_not_allowed``,
    503 ``sse_subscriber_limit_*``) are counted normally.
    """
    duration_ms = (time.perf_counter() - start_perf) * 1000.0
    up_in = _read_state_int(scope, _UP_IN_KEY)
    up_out = _read_state_int(scope, _UP_OUT_KEY)
    # Access log: always (when the logger is enabled). For SSE buckets we log
    # the wire-level down_out so an operator sees the real connection payload.
    try:
        request_id = scope.get("state", {}).get(REQUEST_ID_KEY)
        write_access_log(
            logger,
            method=method,
            path=path,
            bucket=bucket,
            status=status,
            duration_ms=duration_ms,
            down_in=down_in,
            down_out=down_out,
            up_in=up_in,
            up_out=up_out,
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning("write_access_log failed", exc_info=exc)

    ledger = _ledger_from_scope(scope)
    if ledger is None:
        return
    try:
        # SSE buckets: pass resp_bytes=0 only for genuine SSE streams
        # (200 + text/event-stream), where record_sse_downstream owns downOut.
        # Non-stream error responses on SSE paths (400/503) are counted
        # normally so the ledger does not lose those bytes.
        #
        # Invariant / double-count guard: this zeroing DEPENDS on every real
        # SSE response carrying ``content-type: text/event-stream``. Both SSE
        # generators (``routes/events.py`` and ``routes/token_stream.py``)
        # set ``media_type="text/event-stream"`` on their StreamingResponse,
        # so the wire downOut is owned by ``record_sse_downstream`` here. If
        # a future SSE variant forgot that header, this branch would fall
        # through to ``resp_for_ledger = down_out`` (counting wire bytes)
        # AND the generator's per-frame ``record_sse_downstream`` would
        # ALSO add bytes → silent double-count of downOut for that bucket.
        is_real_sse_stream = (
            is_sse
            and status == 200
            and content_type is not None
            and "text/event-stream" in content_type
        )
        resp_for_ledger = 0 if is_real_sse_stream else down_out
        ledger.record_downstream(
            bucket=bucket,
            method=method,
            status=status,
            req_bytes=down_in,
            resp_bytes=resp_for_ledger,
            duration_ms=duration_ms,
        )
        # Upstream bytes for SSE buckets come from record_sse_upstream in the
        # hub — ignore the stash so the single shared /global/event
        # connection is attributed exactly once.
        if not is_sse and (up_in > 0 or up_out > 0):
            ledger.record_upstream(
                bucket=bucket,
                method=method,
                status=status,
                req_bytes=up_out,
                resp_bytes=up_in,
            )
    except Exception as exc:
        logger.warning("record_upstream failed", exc_info=exc)
