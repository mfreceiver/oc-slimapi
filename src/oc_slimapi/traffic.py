"""Full bidirectional byte ledger + bucket derivation (traffic accounting).

Records downstream (client ↔ sidecar) and upstream (sidecar ↔ opencode) byte
flows per logical bucket, plus SSE-specific counters for the curated events
stream and the per-session token stream. Single uvicorn worker, single event
loop — a :class:`threading.Lock` guards mutations for honesty (the lock is
belt-and-suspenders under the single-loop model; the prior reference to a
``BatchLedger`` class is stale — that class was removed in lite-v2).

Counting strategy (no double-count):

* **Downstream HTTP req/resp bytes** are counted by the pure-ASGI middleware
  (:mod:`oc_slimapi.middleware.traffic_accounting`) which wraps ``receive`` /
  ``send``. It owns ``downIn`` (client request body) and ``downOut`` (response
  body sent to the client) for **non-SSE** buckets, and ``requests`` + ``downIn``
  for SSE buckets (the SSE ``downOut`` is owned by :meth:`record_sse_downstream`
  so multi-subscriber fanout is attributed correctly without double-counting the
  same wire bytes).
* **Upstream HTTP req/resp bytes** are stashed by route handlers via
  :func:`stash_up_in` / :func:`stash_up_out` (the middleware reads the stash at
  request end and calls :meth:`record_upstream`).
* **SSE upstream** (single shared ``/global/event``) bytes are counted inline by
  ``GlobalHub.run`` via :meth:`record_sse_upstream` (bucket ``events_sse``).
* **SSE downstream** per-frame bytes are counted inline by the SSE generators
  (``routes/events.py``, ``routes/token_stream.py``) via
  :meth:`record_sse_downstream`.

When ``enabled=False`` (``OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=false``) every
``record_*`` is a no-op and :meth:`snapshot` returns ``{"enabled": False}``.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Final

# Bound on retained per-bucket latency samples (oldest evicted once exceeded).
# Keeps /slimapi/metrics snapshot percentile cost bounded.
_LATENCY_SAMPLES: Final[int] = 1024


# Logical bucket names. The SSE buckets are owned by ``record_sse_*``; the
# middleware passes ``resp_bytes=0`` to ``record_downstream`` for them so their
# ``downOut`` is sourced exclusively from ``record_sse_downstream`` (per emitted
# frame, including multi-subscriber fanout).
SSE_BUCKETS: Final[frozenset[str]] = frozenset({"events_sse", "token_stream_sse"})


def bucketize(method: str, path: str) -> str:
    """Map an HTTP request path to a logical traffic bucket.

    Order matters: more specific prefixes are tested first. The catch-all
    reverse-proxy paths (anything not under ``/slimapi/**``) collapse to
    ``passthrough`` so an operator sees aggregate passthrough traffic
    separately from the curated thin API.
    """
    if not path:
        return "other"
    if path.startswith("/slimapi/"):
        # Most specific first.
        if path in ("/slimapi/health", "/slimapi/ready"):
            return "health"
        if path == "/slimapi/metrics" or path.startswith("/slimapi/metrics/"):
            return "metrics"
        # Token-stream SSE lives under /slimapi/sessions/{sid}/stream — must be
        # tested BEFORE the generic /slimapi/sessions prefix below.
        if path.startswith("/slimapi/sessions/") and path.endswith("/stream"):
            return "token_stream_sse"
        if path == "/slimapi/events" or path.startswith("/slimapi/events/"):
            return "events_sse"
        if path.startswith("/slimapi/messages"):
            return "messages"
        # Catalog skeleton routes (additive). Distinct buckets so each
        # endpoint's raw/gzip/downOut savings are visible in
        # /slimapi/metrics.traffic (the handoff calls for per-endpoint
        # saving validation).
        if path == "/slimapi/command" or path.startswith("/slimapi/command/"):
            return "command"
        if path == "/slimapi/agent" or path.startswith("/slimapi/agent/"):
            return "agent"
        # Cross-directory questions aggregation endpoint (additive catalog style).
        if path == "/slimapi/questions" or path.startswith("/slimapi/questions/"):
            return "questions"
        # Global directory catalog endpoint (additive catalog style).
        if path == "/slimapi/directories" or path.startswith("/slimapi/directories/"):
            return "directories"
        # Generic /slimapi/sessions/**.
        if path.startswith("/slimapi/sessions"):
            return "sessions"
        return "other"
    # Anything else is the catch-all reverse proxy.
    return "passthrough"


# Per-request upstream-byte stash keys (stored under ``scope["state"]`` by
# route handlers; read at request end by the middleware).
_UP_IN_KEY: Final[str] = "traffic_up_in"
_UP_OUT_KEY: Final[str] = "traffic_up_out"


def stash_up_in(request: Any, n: int) -> None:
    """Accumulate ``n`` upstream-response bytes against this request.

    Route handlers call this after consuming upstream response bodies so the
    ASGI middleware can attribute upstream bytes to the request's bucket and
    access log line at request end. Additive; ignores ``n <= 0``; silently
    no-op when the request has no ``scope`` (defensive — never thrice).
    """
    if n <= 0:
        return
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return
    state = scope.setdefault("state", {})
    if not isinstance(state, dict):
        return
    state[_UP_IN_KEY] = state.get(_UP_IN_KEY, 0) + n


def stash_up_out(request: Any, n: int) -> None:
    """Accumulate ``n`` upstream-request bytes (body sent to upstream)."""
    if n <= 0:
        return
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return
    state = scope.setdefault("state", {})
    if not isinstance(state, dict):
        return
    state[_UP_OUT_KEY] = state.get(_UP_OUT_KEY, 0) + n


def _read_state_int(scope: dict, key: str) -> int:
    """Read an integer accumulated by ``stash_up_*`` from ``scope["state"]``."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return 0
    value = state.get(key, 0)
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) and value > 0 else 0


class TrafficLedger:
    """Thread-safe bidirectional byte ledger.

    All ``record_*`` methods are no-ops when ``enabled=False`` (the
    ``OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=false`` path); :meth:`snapshot` then
    returns ``{"enabled": False}``.
    """

    __slots__ = (
        "_lock",
        "_enabled",
        "_buckets",     # bucket -> {requests, downIn, downOut, upIn, upOut, errors4xx, errors5xx}
        "_sse",         # bucket -> {bytesIn, bytesOut, framesEmitted}
        "_latencies",   # bucket -> deque[float] (bounded duration_ms samples)
    )

    def __init__(self, *, enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._buckets: dict[str, dict[str, int]] = {}
        self._sse: dict[str, dict[str, int]] = {}
        self._latencies: dict[str, deque] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- HTTP downstream (called by ASGI middleware) ----

    def record_downstream(
        self,
        *,
        bucket: str,
        method: str,
        status: int,
        req_bytes: int,
        resp_bytes: int,
        duration_ms: float,
    ) -> None:
        """Record one completed downstream HTTP request.

        ``req_bytes`` is the client request body length (``downIn``) and
        ``resp_bytes`` is the response body length we sent back (``downOut``).
        For SSE buckets the middleware passes ``resp_bytes=0`` so the SSE
        per-frame counters own ``downOut``.

        ``status`` drives per-bucket error counters (``errors4xx`` /
        ``errors5xx``) and ``duration_ms`` is retained as a bounded sample for
        per-bucket latency percentiles in :meth:`snapshot`. ``method`` remains
        unused (no per-method split).
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._buckets.setdefault(bucket, self._new_bucket())
            entry["requests"] += 1
            entry["downIn"] += max(0, req_bytes)
            entry["downOut"] += max(0, resp_bytes)
            if 400 <= status < 500:
                entry["errors4xx"] += 1
            elif status >= 500:
                entry["errors5xx"] += 1
            self._latencies.setdefault(bucket, deque(maxlen=_LATENCY_SAMPLES)).append(duration_ms)

    # ---- HTTP upstream (called by ASGI middleware from stashed state) ----

    def record_upstream(
        self,
        *,
        bucket: str,
        method: str,
        status: int,
        req_bytes: int,
        resp_bytes: int,
    ) -> None:
        """Record the upstream leg of one HTTP request.

        ``req_bytes`` is what we sent to upstream (``upOut``) and
        ``resp_bytes`` is what upstream returned (``upIn``).

        .. note::
           ``method`` and ``status`` are accepted but **reserved/unused** —
           status is logged separately via the access log; no per-method/
           per-status bucket split exists.
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._buckets.setdefault(bucket, self._new_bucket())
            entry["upOut"] += max(0, req_bytes)
            entry["upIn"] += max(0, resp_bytes)

    # ---- SSE: shared upstream /global/event consumption ----

    def record_sse_upstream(self, *, bucket: str, bytes_in: int) -> None:
        """Accumulate ``bytes_in`` consumed from the shared upstream SSE stream.

        Currently only ``bucket="events_sse"`` is fed here (the single
        ``/global/event`` subscription serves both the curated events stream
        and the token stream; upstream bytes are attributed to ``events_sse``
        to avoid double-counting the one connection).

        .. note::
           The ``token_stream_sse`` bucket has **no** ``downOutOverUpIn``
           entry in ``ratios`` (its ``upIn`` is zero — the upstream cost is
           charged to ``events_sse``). This is intentional: attributing the
           same shared ``/global/event`` bytes to both SSE buckets would
           double-count a single connection.
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._sse.setdefault(bucket, self._new_sse())
            entry["bytesIn"] += max(0, bytes_in)

    # ---- SSE: per-frame downstream emission ----

    def record_sse_downstream(self, *, bucket: str, bytes_out: int) -> None:
        """Accumulate ``bytes_out`` for one emitted SSE frame.

        Called per yielded frame by the SSE generators (events + token stream).
        Bumps both ``bytesOut`` (→ bucket ``downOut``) and ``framesEmitted``.
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._sse.setdefault(bucket, self._new_sse())
            entry["bytesOut"] += max(0, bytes_out)
            entry["framesEmitted"] += 1

    @staticmethod
    def _new_bucket() -> dict[str, int]:
        return {
            "requests": 0,
            "downIn": 0,
            "downOut": 0,
            "upIn": 0,
            "upOut": 0,
            "errors4xx": 0,
            "errors5xx": 0,
        }

    @staticmethod
    def _new_sse() -> dict[str, int]:
        return {"bytesIn": 0, "bytesOut": 0, "framesEmitted": 0}

    # ---- snapshot for /slimapi/metrics ----

    def snapshot(self) -> dict:
        """Return the ``traffic`` block for ``GET /slimapi/metrics``.

        Shape:

        .. code-block:: python

            {
              "enabled": True,
              "buckets": {
                "messages":         {"requests", "downIn", "downOut",
                                     "upIn", "upOut"},
                "events_sse":       {"requests", "downIn", "downOut",
                                     "upIn", "upOut", "framesEmitted"},
                ... every bucket that has seen traffic ...
              },
              "totals":  {"requests", "downIn", "downOut", "upIn", "upOut"},
              "ratios":  {bucket: {"downOutOverUpIn": float}, ...},
            }

        Each bucket view also carries ``errors4xx`` / ``errors5xx`` counts and,
        when latency samples exist, ``latencyMs`` (``p50`` / ``p90`` / ``p99`` /
        ``count`` from a bounded deque of recent ``duration_ms`` samples).

        ``ratios`` only includes buckets with ``upIn > 0``. For SSE buckets,
        ``downOutOverUpIn`` is the *aggregate-delivery / shared-upstream-cost*
        ratio: ``downOut`` accumulates per-subscriber per-frame (can exceed 1.0
        under N-subscriber fanout), while ``upIn`` counts the single shared
        ``/global/event`` connection once. This is **not** a single-connection
        省流 ratio; the real per-subscriber 省流 evidence is that with one
        subscriber, ``downOut`` ≪ ``upIn`` (the sidecar projects a thin subset
        of the upstream stream).

        .. note::
           **Totals heterogeneity**: ``totals`` sums byte counters across all
           buckets, but the byte semantics are **heterogeneous**.  Proxy
           buckets (``passthrough``) may have ``upIn`` counted as
           gzip-compressed wire bytes, while curated buckets count decoded
           logical bytes.  Therefore per-bucket ``downOutOverUpIn`` ratios are
           more meaningful than the aggregate ``totals`` ratio.

        .. note::
           **SSE snapshot timing**: For SSE buckets, ``requests`` and
           ``downIn`` are recorded only when the middleware sees the connection
           **close** (they stay 0 during the active long-lived connection),
           while ``downOut``, ``upIn`` and ``framesEmitted`` are **real-time**
           accumulators.  Consequently a snapshot taken during an active SSE
           session shows ``downOut > 0`` with ``requests == 0``.  This is a
           known口径 difference, not a bug.

        Returns ``{"enabled": False}`` when the ledger is disabled.
        """
        with self._lock:
            if not self._enabled:
                return {"enabled": False}
            # Start from HTTP bucket counters; merge SSE entries on top.
            buckets: dict[str, dict[str, int]] = {}
            for name, entry in self._buckets.items():
                buckets[name] = dict(entry)
            for name, sse in self._sse.items():
                merged = buckets.setdefault(name, self._new_bucket())
                merged["upIn"] = merged.get("upIn", 0) + sse["bytesIn"]
                merged["downOut"] = merged.get("downOut", 0) + sse["bytesOut"]
                # framesEmitted is SSE-only; stored alongside the bucket dict
                # so the snapshot emits it only for SSE buckets.
                merged["framesEmitted"] = merged.get("framesEmitted", 0) + sse["framesEmitted"]
            totals = {"requests": 0, "downIn": 0, "downOut": 0, "upIn": 0, "upOut": 0}
            ratios: dict[str, dict[str, float]] = {}
            out_buckets: dict[str, dict[str, int]] = {}
            for name, entry in buckets.items():
                requests = entry.get("requests", 0)
                down_in = entry.get("downIn", 0)
                down_out = entry.get("downOut", 0)
                up_in = entry.get("upIn", 0)
                up_out = entry.get("upOut", 0)
                totals["requests"] += requests
                totals["downIn"] += down_in
                totals["downOut"] += down_out
                totals["upIn"] += up_in
                totals["upOut"] += up_out
                view: dict[str, int] = {
                    "requests": requests,
                    "downIn": down_in,
                    "downOut": down_out,
                    "upIn": up_in,
                    "upOut": up_out,
                    "errors4xx": entry.get("errors4xx", 0),
                    "errors5xx": entry.get("errors5xx", 0),
                }
                if "framesEmitted" in entry:
                    view["framesEmitted"] = entry["framesEmitted"]
                samples = list(self._latencies.get(name, ()))
                if samples:
                    samples.sort()
                    n = len(samples)
                    view["latencyMs"] = {
                        "p50": samples[min(n - 1, int(n * 0.50))],
                        "p90": samples[min(n - 1, int(n * 0.90))],
                        "p99": samples[min(n - 1, int(n * 0.99))],
                        "count": n,
                    }
                out_buckets[name] = view
                if up_in > 0:
                    ratios[name] = {"downOutOverUpIn": down_out / up_in}
            return {
                "enabled": True,
                "buckets": out_buckets,
                "totals": totals,
                "ratios": ratios,
            }
