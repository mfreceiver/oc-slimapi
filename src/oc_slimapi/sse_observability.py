"""SSE lifecycle observability (v3-contract §9.1, Batch A).

The two SSE endpoints (``/slimapi/events`` and
``/slimapi/sessions/{sid}/stream``) each emit TWO extra access-log rows per
connection — ``recordType: "sse_open"`` when the generator starts (the stream
actually opened with 200) and ``recordType: "sse_close"`` in the generator's
``finally`` (disconnect / close) — sharing one process-monotonic
``lifecycleId`` (open/close pairing; ``X-Request-ID`` may repeat across
connections and is only an auxiliary correlation key).

The same events bump the :class:`~oc_slimapi.traffic.TrafficLedger` sseActive
counters (per §9.2 dim: v2/v3/absent/not_applicable) so the periodic traffic
snapshot carries live SSE stock.

Everything here is best-effort: observability must never break the stream.
"""
from __future__ import annotations

import itertools
import threading
from typing import Any

from .access_log import get_access_logger, write_sse_lifecycle_log
from .selector import (
    DIRECTORY_FORM_STATE_KEY,
    SELECTOR_STATE_KEY,
    SSE_RESULT_DIMS,
)
from .traffic import TrafficLedger  # noqa: F401  (type reference in docstring)

_lifecycle_lock = threading.Lock()
_lifecycle_counter = itertools.count(1)


def next_lifecycle_id() -> int:
    """Process-wide monotonic SSE lifecycle id (starts at 1)."""
    with _lifecycle_lock:
        return next(_lifecycle_counter)


def _access_logger():
    """Indirection point for tests; production = the access-log singleton."""
    return get_access_logger()


def _dims(scope: dict[str, Any] | None) -> tuple[str | None, str | None, str | None, str]:
    """(selectorResult, wireVersion, directoryForm, sseActive-dim) for the
    scope. Missing selector state (legacy stacks) maps to the ``absent``
    dim — the honest classification for a no-selector deployment. A ``None``
    scope (direct route invocation in tests with mock requests lacking
    ``.scope``) yields all-null row fields and the ``absent`` ledger dim."""
    state = scope.get("state") if scope is not None else None
    state = state if isinstance(state, dict) else {}
    info = state.get(SELECTOR_STATE_KEY)
    info = info if isinstance(info, dict) else {}
    result = info.get("result")
    wire = info.get("wire")
    dform = state.get(DIRECTORY_FORM_STATE_KEY)
    dim = result if result in SSE_RESULT_DIMS else "absent"
    return result, wire, dform, dim


def _ledger_from_scope(scope: dict[str, Any] | None) -> Any | None:
    if scope is None:
        return None
    app = scope.get("app")
    state = getattr(app, "state", None)
    return getattr(state, "traffic_ledger", None) if state is not None else None


def _request_id(scope: dict[str, Any] | None) -> str | None:
    from .middleware.request_id import REQUEST_ID_KEY

    if scope is None:
        return None
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(REQUEST_ID_KEY)
    return value if isinstance(value, str) else None


def _emit(
    scope: dict[str, Any] | None,
    *,
    bucket: str,
    record_type: str,
    lifecycle_id: int,
    status: int | None,
) -> None:
    result, wire, dform, dim = _dims(scope)
    if scope is not None:
        try:
            write_sse_lifecycle_log(
                _access_logger(),
                method=scope.get("method", "") or "",
                path=scope.get("path", "") or "",
                bucket=bucket,
                record_type=record_type,
                lifecycle_id=lifecycle_id,
                wire_version=wire,
                selector_result=result,
                directory_form=dform,
                request_id=_request_id(scope),
                status=status,
            )
        except Exception:
            pass  # observability must never break the stream
    ledger = _ledger_from_scope(scope) if scope is not None else None
    if ledger is not None:
        try:
            ledger.record_sse_lifecycle(result=dim, opened=record_type == "sse_open")
        except Exception:
            pass


def sse_open(scope: dict[str, Any] | None, *, bucket: str) -> int:
    """Emit the ``sse_open`` row (stream started, status 200) and return the
    lifecycle id the matching ``sse_close`` call must pass back. A ``None``
    scope (mock requests without ``.scope`` in direct route-invocation tests)
    still returns a fresh id but writes nothing — observability requires a
    real ASGI scope."""
    lifecycle_id = next_lifecycle_id()
    _emit(scope, bucket=bucket, record_type="sse_open", lifecycle_id=lifecycle_id, status=200)
    return lifecycle_id


def sse_close(scope: dict[str, Any] | None, *, bucket: str, lifecycle_id: int) -> None:
    """Emit the ``sse_close`` row for the given lifecycle id."""
    _emit(scope, bucket=bucket, record_type="sse_close", lifecycle_id=lifecycle_id, status=None)
