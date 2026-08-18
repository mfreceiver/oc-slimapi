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

# Expand fragment categories (design-expand §2.2 frozen table, verbatim order).
# SINGLE SOURCE OF TRUTH for both the wire advertisement
# (routes/versions.py capabilities["3"]["expand"], §6) and the per-category
# accounting whitelist below — the versions route imports this constant
# (traffic.py has no imports back into routes/, so no cycle). The whitelist
# bounds the ledger's ``_expand`` key space: only these categories may open
# new keys, everything else collapses into the fixed ``invalid`` bucket
# (rev-gpt R1 M2 — arbitrary path segments must not grow memory unboundedly).
EXPAND_CATEGORIES: list[str] = [
    "info_summary_diffs",
    "part_text",
    "part_reasoning",
    "part_state_output",
    "part_state_error",
    "part_state_input_full",
    "part_state_metadata_full",
    "part_state_attachments",
    "part_url",
    "part_source",
    "part_snapshot",
    "compaction_full",
]
EXPAND_CATEGORIES_SET: Final[frozenset[str]] = frozenset(EXPAND_CATEGORIES)
# Fixed sink for any request path whose category segment is not in the
# whitelist (forged / malformed categories, empty segments) — keeps the
# ``_expand`` dict size bounded by (12 categories + 1) × observed statuses.
_EXPAND_INVALID_CATEGORY: Final[str] = "invalid"


def _normalize_expand_category(category: str) -> str:
    """Collapse a raw path category onto the §2.2 whitelist.

    Whitelisted categories round-trip unchanged; anything else (forged or
    malformed segments, including empty ones) counts under the fixed
    ``invalid`` key so an attacker rotating arbitrary category segments
    cannot grow the ledger's ``_expand`` dict without bound.
    """
    if category in EXPAND_CATEGORIES_SET:
        return category
    return _EXPAND_INVALID_CATEGORY


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
        # Stage-1 q/p sweep markers are shadow observations, not HTTP routes;
        # keep a reserved path shape bucketable for future ledger integrations.
        if path == "/slimapi/_shadow/sweep" or path.startswith("/slimapi/_shadow/sweep/"):
            return "sweep"
        # Token-stream SSE lives under /slimapi/sessions/{sid}/stream — must be
        # tested BEFORE the generic /slimapi/sessions prefix below.
        if path.startswith("/slimapi/sessions/") and path.endswith("/stream"):
            return "token_stream_sse"
        if path == "/slimapi/events" or path.startswith("/slimapi/events/"):
            return "events_sse"
        # Messages expand endpoints (design-expand §2.1 / §8 read group 8
        # "messages.expand"): /slimapi/messages/{sid}/expand/{category}/... get
        # their own bucket so per-fragment traffic (and error surface) stays
        # visible in /slimapi/metrics.traffic separate from the skeleton
        # projections (same per-endpoint precedent as command/agent).
        if _expand_tail(path) is not None:
            return "messages.expand"
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
        # §10.a read groups (v3 Batch C1) — distinct buckets per group so
        # annexed-read traffic stays visible in /slimapi/metrics.traffic.
        if path == "/slimapi/file" or path.startswith("/slimapi/file/"):
            return "file"
        if path == "/slimapi/vcs" or path.startswith("/slimapi/vcs/"):
            return "vcs"
        if path == "/slimapi/find" or path.startswith("/slimapi/find/"):
            return "find"
        if path.startswith("/slimapi/config/"):
            return "providers"
        # §10.b write routes (v3 Batch C2): /slimapi/session (POST) and
        # the write methods on /slimapi/session/{sid} (+ sub-actions) get
        # their own bucket; the §10.a session-single GET keeps its own.
        # Method-aware split BEFORE the generic session_single branch.
        if path == "/slimapi/session" and method.upper() == "POST":
            return "write_session"
        if path.startswith("/slimapi/session/") and method.upper() != "GET":
            # PATCH/DELETE on {sid} + prompt_async/abort/summarize/fork/
            # revert/permissions/{pid}/command sub-actions (all POST except
            # the PATCH/DELETE pair).
            return "write_session"
        if (
            path.startswith("/slimapi/question/")
            and method.upper() == "POST"
            and (path.endswith("/reply") or path.endswith("/reject"))
        ):
            # Method-aware (C2 gate): only the actual POST write endpoints
            # bucket as write_question — a GET on reply/reject is a FastAPI
            # 405 (routes registered for POST only) and must not count as
            # write traffic; it falls through to the generic buckets.
            return "write_question"
        # B4: GET /slimapi/session/{sid}/context — the v2 session group's
        # context endpoint (sessionContext). Its own bucket so context-read
        # traffic stays visible separately from the session_single projection
        # (same per-endpoint precedent as command/agent). Must fire BEFORE
        # the generic session_single branch below; the method-aware
        # write_session branch above already owns non-GET /session/**.
        if path.startswith("/slimapi/session/") and path.endswith("/context"):
            return "session_context"
        # /slimapi/session/{sid} (session single — NOT the plural sessions
        # surface above) and the two tolerant global reads.
        if path.startswith("/slimapi/session/"):
            return "session_single"
        if path == "/slimapi/api/session/active":
            return "session_active"
        if path == "/slimapi/global/health":
            return "global_health"
        return "other"
    # Anything else is the catch-all reverse proxy.
    return "passthrough"


# Messages expand path segment (design-expand §2.1): the expand endpoints live
# at ``/slimapi/messages/{sid}/expand/{category}/{mid}[/{partID}]``. The
# segment-check helpers below share one source of truth for bucketizing AND
# per-category accounting, so a path can never be counted under a different
# category than the bucket it landed in.
_EXPAND_SEGMENT = "expand/"


def _expand_tail(path: str) -> str | None:
    """Return the substring after the ``expand/`` segment of a messages
    expand path, or ``None`` when the path is not an expand request.

    Segment-strict: ``{sid}`` must be a single NON-EMPTY path segment
    immediately followed by ``expand/`` — a stray ``expand/`` later in the
    path, a bare ``/slimapi/messages/{sid}/expand`` with no category, or an
    empty sid segment (``/slimapi/messages//expand/...``) does not match.
    """
    prefix = "/slimapi/messages/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    slash = rest.find("/")
    if slash <= 0:
        return None  # missing sid segment, or empty sid segment
    after_sid = rest[slash + 1:]
    if not after_sid.startswith(_EXPAND_SEGMENT):
        return None
    return after_sid[len(_EXPAND_SEGMENT):]


def expand_category_from_path(path: str) -> str | None:
    """Extract the raw expand ``category`` path segment (design-expand
    §2.1/§2.2), or ``None`` when the path is not an expand request.

    The raw segment is NOT whitelisted here — it may be an empty string
    (``/expand/{mid}``) or a forged value; :func:`_normalize_expand_category`
    (inside :meth:`TrafficLedger.record_expand`) collapses those onto the
    bounded ``invalid`` key. Returning ``None`` only for non-expand paths
    keeps "is this an expand request" (bucket/middleware) separate from
    "which category does it count under" (normalization).
    """
    tail = _expand_tail(path)
    if tail is None:
        return None
    category, _, _ = tail.partition("/")
    return category


# Per-request upstream-byte stash keys (stored under ``scope["state"]`` by
# route handlers; read at request end by the middleware).
_UP_IN_KEY: Final[str] = "traffic_up_in"
_UP_OUT_KEY: Final[str] = "traffic_up_out"
_CACHE_KEY: Final[str] = "traffic_cache"

# v4 sessions degraded-observability state markers (v4-contract §9.1/§9.2,
# rev-gate BLOCKER-4). WRITTEN by the routes/sessions.py ``_sessions_v4``
# handler (R1 lane) onto ``request.state`` — i.e. into ``scope["state"]``;
# READ at request end by the traffic-accounting middleware (this module's
# consumers). Single source of truth for the key spellings on both sides.
#   * ``slimapi_sessions_source`` = "db" | "http": DB 200 → "db";
#     Class A degraded 200 → "http"; v3 path / not written → attribute absent.
#   * ``slimapi_degraded_503`` = True: written on 503 fail-closed only;
#     absent = not a degraded 503.
SESSIONS_SOURCE_STATE_KEY: Final[str] = "slimapi_sessions_source"
DEGRADED_503_STATE_KEY: Final[str] = "slimapi_degraded_503"
# Accepted ``sessionsSource`` value set (defensive: garbage state values are
# ignored by the consumers rather than written to the access log).
SESSIONS_SOURCE_VALUES: Final[frozenset[str]] = frozenset({"db", "http"})


def stash_cache(request: Any, state_value: str | None) -> None:
    """Stash the catalog-cache outcome (``"hit"``/``"miss"``) for this request.

    Traffic plan Batch 1 / A1: catalog routes record whether the response
    body came from the TTL cache so the access-log row can attribute
    upstream traffic correctly. ``None`` (cache disabled / not applicable)
    is a no-op — rows without cache semantics keep their exact key set.
    """
    if state_value is None:
        return
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return
    state = scope.setdefault("state", {})
    if not isinstance(state, dict):
        return
    state[_CACHE_KEY] = state_value


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


def read_sessions_source(scope: dict) -> str | None:
    """Read the R1 ``slimapi_sessions_source`` marker (validated).

    Returns ``"db"`` | ``"http"`` or ``None`` (absent / non-string / outside
    the accepted value set — garbage is ignored, never logged).
    """
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(SESSIONS_SOURCE_STATE_KEY)
    return value if value in SESSIONS_SOURCE_VALUES else None


def read_degraded_503(scope: dict) -> bool:
    """Read the R1 ``slimapi_degraded_503`` marker (strict ``is True``)."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return False
    return state.get(DEGRADED_503_STATE_KEY) is True


class SessionsDegradedCounters:
    """Per-response v4 sessions degraded counters (v4-contract §9.1).

    The review point these exist for: the dbaux ``disables``/``trips``
    counters count **state-machine events** (one disable can serve an
    arbitrary number of degraded responses), while an operator needs
    **per-response** truth — three degraded 503s must read as 3, not 1.

    * ``degraded_200``: responses served from the HTTP fallback
      (``sessionsSource == "http"``, 2xx) — Class A degraded successes.
    * ``fail_closed_503``: fail-closed degraded responses (the
      ``degraded_503`` marker, 5xx).

    Mounted lazily on ``app.state.sessions_degraded`` by
    :func:`ensure_sessions_degraded_counters` (middleware writes, the
    ``/slimapi/metrics`` route reads) — deliberately OUTSIDE the
    TrafficLedger so degraded observability does not ride the
    ``OC_SLIMAPI_TRAFFIC_METRICS_ENABLED`` switch. Thread-safe (lock) and
    race-free under the single-event-loop model: the lazy get-then-set in
    :func:`ensure_sessions_degraded_counters` is fully synchronous, so no
    interleaving point exists on the loop.
    """

    __slots__ = ("_lock", "_degraded_200", "_fail_closed_503")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._degraded_200 = 0
        self._fail_closed_503 = 0

    def record_degraded_200(self) -> None:
        with self._lock:
            self._degraded_200 += 1

    def record_fail_closed_503(self) -> None:
        with self._lock:
            self._fail_closed_503 += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "degraded_200": self._degraded_200,
                "fail_closed_503": self._fail_closed_503,
            }


# app.state attribute the counters live under.
SESSIONS_DEGRADED_STATE_ATTR: Final[str] = "sessions_degraded"


def ensure_sessions_degraded_counters(state: Any) -> SessionsDegradedCounters:
    """Return the app-state degraded counters, creating them if absent.

    Used by both the traffic middleware (write side) and the metrics route
    (read side, so a freshly-booted app reports the zero state instead of
    omitting the block). Synchronous get-then-set — no await between the
    check and the assignment, so the single event loop cannot interleave a
    second creator; a hypothetical loser instance is simply garbage-collected.
    """
    counters = getattr(state, SESSIONS_DEGRADED_STATE_ATTR, None)
    if counters is None:
        counters = SessionsDegradedCounters()
        try:
            setattr(state, SESSIONS_DEGRADED_STATE_ATTR, counters)
        except Exception:
            # Read-only / frozen state object: return the transient instance
            # (best-effort — counts live only for this call). Never raise:
            # observability must not break requests.
            return counters
    return counters


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
        "_v3_matrix",   # flat "selectorResult|wireVersion|directoryForm|recordType|statusClass|bucket" -> count (v3 §9.2)
        "_v3_sse",      # sseActive dim -> {opens, closes, active, orphanCloses} (v3 §9.2)
        "_expand",      # "category|status" -> {requests, bytes} (design-expand §11 P4)
        "_v4_degraded", # flat "degraded|kind|statusClass|bucket" -> count (v4 §9.2 degraded path)
    )

    def __init__(self, *, enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._buckets: dict[str, dict[str, int]] = {}
        self._sse: dict[str, dict[str, int]] = {}
        self._latencies: dict[str, deque] = {}
        self._v3_matrix: dict[str, int] = {}
        self._v3_sse: dict[str, dict[str, int]] = {}
        self._expand: dict[str, dict[str, int]] = {}
        self._v4_degraded: dict[str, int] = {}

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

    # ---- v3 observability (v3-contract §9.2, Batch A) ----

    @staticmethod
    def _v3_status_class(status: int | None) -> str:
        if isinstance(status, bool) or not isinstance(status, int):
            return "none"
        return f"{status // 100}xx"

    def record_selector_request(
        self,
        *,
        bucket: str,
        status: int | None,
        selector_result: str | None = None,
        wire_version: str | None = None,
        directory_form: str | None = None,
        record_type: str = "request",
    ) -> None:
        """Count one access row into the §9.2 aggregation matrix.

        Key = ``selectorResult|wireVersion|directoryForm|recordType|
        statusClass|bucket`` with ``null`` placeholders for absent dims.
        Cumulative since boot (the cross-day series is derived at analysis
        time from the access log via
        :func:`oc_slimapi.traffic_snapshot.aggregate_v3_observability`).
        """
        if not self._enabled:
            return
        key = "|".join((
            selector_result or "null",
            wire_version or "null",
            directory_form or "null",
            record_type,
            self._v3_status_class(status),
            bucket,
        ))
        with self._lock:
            self._v3_matrix[key] = self._v3_matrix.get(key, 0) + 1

    def record_sse_lifecycle(self, *, result: str, opened: bool) -> None:
        """Count one SSE open/close for the per-dim sseActive stock.

        ``result`` is the §9.2 dim (v2|v3|v4|absent|not_applicable — the caller
        normalizes; rejected/exempt have no SSE endpoints). ``active`` is the
        live stock; a close with no tracked open (restart loss / window-start
        carry) clamps at zero and counts in ``orphanCloses`` — never negative.
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._v3_sse.setdefault(
                result, {"opens": 0, "closes": 0, "active": 0, "orphanCloses": 0}
            )
            if opened:
                entry["opens"] += 1
                entry["active"] += 1
            else:
                entry["closes"] += 1
                if entry["active"] > 0:
                    entry["active"] -= 1
                else:
                    entry["orphanCloses"] += 1


    # ---- expand per-category observability (design-expand §11 P4) ----

    def record_expand(
        self,
        *,
        category: str,
        status: int,
        resp_bytes: int,
    ) -> None:
        """Count one expand request by ``category`` and ``status``.

        ``resp_bytes`` is the downstream response body length (same口径 as
        ``downOut``). Key = ``normalized_category|status`` where
        ``normalized_category`` is the §2.2 whitelist membership via
        :func:`_normalize_expand_category` — categories outside the 12
        (forged or empty segments) always collapse onto the fixed
        ``invalid`` key (rev-gpt R1 M2): the ``_expand`` dict stays bounded
        by (12 + 1) categories × observed statuses regardless of the request
        path, so an attacker rotating arbitrary category segments cannot grow
        sidecar memory. The flat-key style mirrors the v3 matrix
        (``record_selector_request``) and is what the rate-limit / cache
        evaluation (design-expand §11 follow-up) needs to split expand
        traffic per category without re-parsing access logs.

        Additive cross-cut: the request is ALSO counted in its HTTP bucket
        (``messages.expand``) by :meth:`record_downstream`; this counter is a
        separate dimension, not a replacement.
        """
        if not self._enabled:
            return
        key = f"{_normalize_expand_category(category)}|{status}"
        with self._lock:
            entry = self._expand.setdefault(key, {"requests": 0, "bytes": 0})
            entry["requests"] += 1
            entry["bytes"] += max(0, resp_bytes)

    # ---- v4 sessions degraded observability (v4-contract §9.1/§9.2) ----

    def record_sessions_degraded(self, *, kind: str, bucket: str, status: int) -> None:
        """Count one degraded v4 sessions response into the flat matrix.

        Key = ``degraded|kind|statusClass|bucket`` with ``kind``:

        * ``"http"`` — Class A degraded success (served from the HTTP
          fallback, ``sessionsSource == "http"``, 2xx).
        * ``"fail_closed"`` — fail-closed degraded response (the
          ``degraded_503`` marker, 5xx).

        Per-RESPONSE truth (three degraded 503s → +3), deliberately distinct
        from the dbaux ``disables``/``trips`` STATE-machine event counters
        (one disable can produce any number of degraded responses — the
        rev-gate BLOCKER-4 finding). Flat-key style mirrors
        :meth:`record_selector_request` / :meth:`record_expand`; the value
        set is bounded (2 kinds × status classes × the sessions bucket
        family), so no whitelisting is needed. Cumulative since boot — the
        per-day series is derived at analysis time from the access log
        (``traffic_snapshot.aggregate_v3_observability`` degraded section).
        """
        if not self._enabled:
            return
        key = "|".join((
            "degraded",
            kind,
            self._v3_status_class(status),
            bucket,
        ))
        with self._lock:
            self._v4_degraded[key] = self._v4_degraded.get(key, 0) + 1

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
            out = {
                "enabled": True,
                "buckets": out_buckets,
                "totals": totals,
                "ratios": ratios,
                # v3 Batch A (§9.2): additive observability section —
                # cumulative matrix counters + live SSE stock per dim since
                # boot. The per-day series (window-start carry) is analysis
                # time over the daily access logs.
                "v3": {
                    "matrix": dict(self._v3_matrix),
                    "sseLifecycle": {
                        dim: dict(entry) for dim, entry in self._v3_sse.items()
                    },
                    "sseActive": {
                        dim: entry["active"] for dim, entry in self._v3_sse.items()
                    },
                },
                # design-expand §11 P4: expand requests counted per
                # ``category|status`` with downstream response bytes.
                "expand": {
                    key: dict(entry) for key, entry in self._expand.items()
                },
            }
            # v4 §9.1/§9.2 (rev-gate BLOCKER-4): degraded-path flat matrix —
            # per-response counts keyed ``degraded|kind|statusClass|bucket``.
            # Sparse like ``framesEmitted`` (emitted only when the bucket has
            # SSE semantics): the key appears only after the first degraded
            # response, keeping the additive zero-knowledge contract for
            # existing exact-shape consumers (``set(traffic) == {...}``).
            if self._v4_degraded:
                out["v4"] = {"degradedMatrix": dict(self._v4_degraded)}
            return out
