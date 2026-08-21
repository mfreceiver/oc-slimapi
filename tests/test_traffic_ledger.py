"""Unit tests for ``oc_slimapi.traffic`` (full bidirectional byte ledger).

Covers :func:`bucketize`, :class:`TrafficLedger` (record_*/snapshot), and the
upstream-byte stash helpers (:func:`stash_up_in`, :func:`stash_up_out`,
:func:`_read_state_int`). All assertions target the *actual* source behaviour
in ``src/oc_slimapi/traffic.py``.
"""

from __future__ import annotations

import pytest

from oc_slimapi.traffic import (
    _UP_IN_KEY,
    _UP_OUT_KEY,
    TrafficLedger,
    _read_state_int,
    bucketize,
    stash_up_in,
    stash_up_out,
)


class _FakeRequest:
    """Minimal stand-in for a Starlette/FastAPI request exposing ``scope``."""

    def __init__(self, scope=None):
        self.scope = scope


# ---------------------------------------------------------------------------
# bucketize
# ---------------------------------------------------------------------------


class TestBucketize:
    """Verify path -> bucket mapping (order-sensitive prefix matching)."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            # empty / falsy
            ("", "other"),
            # health (exact match only)
            ("/slimapi/health", "health"),
            ("/slimapi/ready", "health"),
            # metrics
            ("/slimapi/metrics", "metrics"),
            ("/slimapi/metrics/foo", "metrics"),
            # token stream SSE (sessions/{sid}/stream)
            ("/slimapi/sessions/abc/stream", "token_stream_sse"),
            # events SSE
            ("/slimapi/events", "events_sse"),
            ("/slimapi/events/foo", "events_sse"),
            # messages
            ("/slimapi/messages", "messages"),
            ("/slimapi/messages/foo", "messages"),
            # generic sessions (list, /status, /children, ...)
            ("/slimapi/sessions", "sessions"),
            ("/slimapi/sessions/abc", "sessions"),
            ("/slimapi/sessions/abc/children", "sessions"),
            ("/slimapi/sessions/abc/status", "sessions"),
            # other slimapi
            ("/slimapi/unknown", "other"),
            ("/slimapi/", "other"),
            # proxy passthrough (anything not under /slimapi/)
            ("/session", "passthrough"),
            ("/session/x", "passthrough"),
            ("/", "passthrough"),
            ("/global/event", "passthrough"),
        ],
    )
    def test_buckets(self, path, expected):
        assert bucketize("GET", path) == expected

    def test_session_stream_wins_over_sessions_prefix(self):
        """token_stream_sse check must fire BEFORE the generic sessions prefix.

        /slimapi/sessions/{sid}/stream matches both branches; ordering must
        yield token_stream_sse (the SSE bucket), not sessions.
        """
        assert bucketize("GET", "/slimapi/sessions/sid123/stream") == "token_stream_sse"

    def test_session_children_is_sessions(self):
        assert bucketize("GET", "/slimapi/sessions/sid123/children") == "sessions"

    def test_messages_subpath(self):
        assert bucketize("GET", "/slimapi/messages/ses_x") == "messages"

    def test_catalog_buckets(self):
        """The additive catalog skeleton routes get their own buckets so
        per-endpoint savings are visible in metrics."""
        assert bucketize("GET", "/slimapi/command") == "command"
        assert bucketize("GET", "/slimapi/agent") == "agent"
        # prefix-tolerant (no sub-routes today, but consistent with siblings)
        assert bucketize("GET", "/slimapi/command/x") == "command"
        assert bucketize("GET", "/slimapi/agent/x") == "agent"

    def test_questions_bucket(self):
        """The cross-directory questions aggregation endpoint gets its own
        bucket (matches the command/agent catalog precedent)."""
        assert bucketize("GET", "/slimapi/questions") == "questions"
        # prefix-tolerant (consistent with siblings)
        assert bucketize("GET", "/slimapi/questions/x") == "questions"

    def test_messages_expand_bucket(self):
        """Expand endpoints (design-expand §2.1 / §8 read group 8
        "messages.expand") bucket separately from skeleton messages, both
        path forms (/{mid} and /{mid}/{partID})."""
        assert bucketize("GET", "/slimapi/messages/ses_x/expand/part_text/msg_x") == "messages.expand"
        assert bucketize("GET", "/slimapi/messages/ses_x/expand/part_text/msg_x/prt_y") == "messages.expand"
        # plain messages paths keep the generic bucket
        assert bucketize("GET", "/slimapi/messages/ses_x") == "messages"
        assert bucketize("GET", "/slimapi/messages") == "messages"

    def test_session_root_is_sessions(self):
        assert bucketize("GET", "/slimapi/sessions") == "sessions"

    def test_method_is_ignored(self):
        """method arg is currently unused; bucket depends only on path."""
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "WHATEVER"):
            assert bucketize(method, "/slimapi/health") == "health"
            assert bucketize(method, "/slimapi/messages/x") == "messages"

    def test_health_is_exact_not_prefix(self):
        """Only exact /slimapi/health and /slimapi/ready are 'health'."""
        assert bucketize("GET", "/slimapi/healthX") == "other"
        assert bucketize("GET", "/slimapi/ready/foo") == "other"

    def test_literal_ses_x_paths_are_other(self):
        """DISCREPANCY NOTE: the task description lists ``/slimapi/ses_x/stream``
        -> token_stream_sse and ``/slimapi/ses_x/children`` -> sessions, but the
        source requires ``startswith('/slimapi/sessions/...')``. The token
        ``ses_x`` does not match the ``sessions`` prefix, so both literal paths
        fall through to ``other``. The real session stream path
        ``/slimapi/sessions/{sid}/stream`` (tested above) is what exercises the
        intended ordering guarantee."""
        assert bucketize("GET", "/slimapi/ses_x/stream") == "other"
        assert bucketize("GET", "/slimapi/ses_x/children") == "other"

    # ---- Q6 (2026-08-22) discovery-bucket additions ----
    # 8h production-log evidence: GET /slimapi/permissions (x140), GET
    # /slimapi/versions (x53), GET /slimapi/actions (x7) + POST
    # /slimapi/actions/{id} (x5) all leaked into ``other``.

    def test_q6_read_buckets_exact(self):
        """GET-exact hits: permissions / versions / actions get their own
        buckets (health exact-match precedent)."""
        assert bucketize("GET", "/slimapi/permissions") == "permissions"
        assert bucketize("GET", "/slimapi/versions") == "versions"
        assert bucketize("GET", "/slimapi/actions") == "actions"

    def test_q6_read_buckets_subpaths_are_other(self):
        """Sub-paths keep falling to ``other`` (mirrors the health
        exact-not-prefix semantics)."""
        assert bucketize("GET", "/slimapi/permissions/x") == "other"
        assert bucketize("GET", "/slimapi/versions/x") == "other"
        assert bucketize("GET", "/slimapi/actions/x") == "other"

    def test_q6_read_buckets_wrong_methods_are_other(self):
        """Non-GET on the GET-only discovery endpoints is a FastAPI 405 and
        must not count as bucket traffic (write_question C2-gate mirror)."""
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert bucketize(method, "/slimapi/permissions") == "other"
            assert bucketize(method, "/slimapi/versions") == "other"
            assert bucketize(method, "/slimapi/actions") == "other"

    def test_q6_write_actions_bucket(self):
        """POST /slimapi/actions/{name} (manifest action invocation) gets the
        write_actions bucket — write_* naming per write_session /
        write_question."""
        assert bucketize("POST", "/slimapi/actions/compact") == "write_actions"
        # wrong method on the invocation sub-path is a 405 -> other
        assert bucketize("GET", "/slimapi/actions/compact") == "other"
        assert bucketize("DELETE", "/slimapi/actions/compact") == "other"
        # bare POST /slimapi/actions is not an invocation route -> other
        assert bucketize("POST", "/slimapi/actions") == "other"


# ---------------------------------------------------------------------------
# record_downstream
# ---------------------------------------------------------------------------


class TestRecordDownstream:
    def test_single_record(self):
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=100, resp_bytes=200, duration_ms=5.0,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["requests"] == 1
            assert entry["downIn"] == 100
            assert entry["downOut"] == 200
            assert entry["upIn"] == 0
            assert entry["upOut"] == 0

    def test_multiple_accumulate(self):
        ledger = TrafficLedger()
        for _ in range(3):
            ledger.record_downstream(
                bucket="messages", method="POST", status=200,
                req_bytes=10, resp_bytes=20, duration_ms=1.0,
            )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["requests"] == 3
            assert entry["downIn"] == 30
            assert entry["downOut"] == 60

    def test_buckets_isolated(self):
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=100, resp_bytes=50, duration_ms=1.0,
        )
        ledger.record_downstream(
            bucket="sessions", method="GET", status=200,
            req_bytes=5, resp_bytes=8, duration_ms=1.0,
        )
        with ledger._lock:
            assert ledger._buckets["messages"]["requests"] == 1
            assert ledger._buckets["sessions"]["requests"] == 1
            assert ledger._buckets["messages"]["downIn"] == 100
            assert ledger._buckets["sessions"]["downIn"] == 5
            assert ledger._buckets["messages"]["downOut"] == 50
            assert ledger._buckets["sessions"]["downOut"] == 8


# ---------------------------------------------------------------------------
# record_upstream
# ---------------------------------------------------------------------------


class TestRecordUpstream:
    def test_single_record_maps_req_to_upout_resp_to_upin(self):
        ledger = TrafficLedger()
        ledger.record_upstream(
            bucket="messages", method="POST", status=200,
            req_bytes=50, resp_bytes=300,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["upOut"] == 50   # req_bytes -> upOut
            assert entry["upIn"] == 300   # resp_bytes -> upIn
            assert entry["requests"] == 0  # upstream does not bump requests
            assert entry["downIn"] == 0
            assert entry["downOut"] == 0

    def test_accumulate(self):
        ledger = TrafficLedger()
        ledger.record_upstream(
            bucket="messages", method="POST", status=200,
            req_bytes=10, resp_bytes=100,
        )
        ledger.record_upstream(
            bucket="messages", method="POST", status=200,
            req_bytes=5, resp_bytes=25,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["upOut"] == 15
            assert entry["upIn"] == 125

    def test_buckets_isolated(self):
        ledger = TrafficLedger()
        ledger.record_upstream(
            bucket="messages", method="GET", status=200,
            req_bytes=1, resp_bytes=2,
        )
        ledger.record_upstream(
            bucket="projects", method="GET", status=200,
            req_bytes=10, resp_bytes=20,
        )
        with ledger._lock:
            assert ledger._buckets["messages"]["upIn"] == 2
            assert ledger._buckets["projects"]["upIn"] == 20
            assert ledger._buckets["messages"]["upOut"] == 1
            assert ledger._buckets["projects"]["upOut"] == 10


# ---------------------------------------------------------------------------
# record_sse_upstream / record_sse_downstream
# ---------------------------------------------------------------------------


class TestRecordSseUpstream:
    def test_accumulate_bytes_in(self):
        ledger = TrafficLedger()
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=100)
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=50)
        with ledger._lock:
            sse = ledger._sse["events_sse"]
            assert sse["bytesIn"] == 150
            assert sse["bytesOut"] == 0
            assert sse["framesEmitted"] == 0

    def test_token_stream_bucket_separate(self):
        ledger = TrafficLedger()
        ledger.record_sse_upstream(bucket="token_stream_sse", bytes_in=42)
        with ledger._lock:
            assert ledger._sse["token_stream_sse"]["bytesIn"] == 42
            assert "events_sse" not in ledger._sse


class TestRecordSseDownstream:
    def test_accumulate_bytes_out_and_frames(self):
        ledger = TrafficLedger()
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=10)
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=20)
        with ledger._lock:
            sse = ledger._sse["events_sse"]
            assert sse["bytesOut"] == 30
            assert sse["framesEmitted"] == 2
            assert sse["bytesIn"] == 0

    def test_token_stream_bucket(self):
        ledger = TrafficLedger()
        ledger.record_sse_downstream(bucket="token_stream_sse", bytes_out=5)
        with ledger._lock:
            assert ledger._sse["token_stream_sse"]["framesEmitted"] == 1
            assert ledger._sse["token_stream_sse"]["bytesOut"] == 5


# ---------------------------------------------------------------------------
# snapshot (enabled shape, totals, ratios)
# ---------------------------------------------------------------------------


class TestSnapshotEnabled:
    def test_empty_snapshot(self):
        ledger = TrafficLedger()
        snap = ledger.snapshot()
        assert snap["enabled"] is True
        assert snap["buckets"] == {}
        assert snap["totals"] == {
            "requests": 0, "downIn": 0, "downOut": 0, "upIn": 0, "upOut": 0,
        }
        assert snap["ratios"] == {}

    def test_shape_with_traffic(self):
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=10, resp_bytes=20, duration_ms=1.0,
        )
        ledger.record_upstream(
            bucket="messages", method="GET", status=200,
            req_bytes=5, resp_bytes=100,
        )
        snap = ledger.snapshot()
        assert snap["enabled"] is True
        b = snap["buckets"]["messages"]
        assert set(b) == {
            "requests", "downIn", "downOut", "upIn", "upOut",
            "errors4xx", "errors5xx", "latencyMs",
        }
        assert b["requests"] == 1
        assert b["downIn"] == 10
        assert b["downOut"] == 20
        assert b["upIn"] == 100
        assert b["upOut"] == 5
        # status 200 -> no errors; one latency sample (duration_ms=1.0)
        assert b["errors4xx"] == 0
        assert b["errors5xx"] == 0
        assert b["latencyMs"] == {"p50": 1.0, "p90": 1.0, "p99": 1.0, "count": 1}
        # totals are the sum across buckets
        assert snap["totals"] == {
            "requests": 1, "downIn": 10, "downOut": 20, "upIn": 100, "upOut": 5,
        }
        # ratios: upIn>0 -> downOut/upIn = 20/100 = 0.2
        assert "messages" in snap["ratios"]
        assert snap["ratios"]["messages"]["downOutOverUpIn"] == pytest.approx(0.2)

    def test_totals_sum_across_buckets(self):
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=10, resp_bytes=20, duration_ms=1.0,
        )
        ledger.record_downstream(
            bucket="sessions", method="GET", status=200,
            req_bytes=5, resp_bytes=8, duration_ms=1.0,
        )
        ledger.record_upstream(
            bucket="messages", method="GET", status=200,
            req_bytes=3, resp_bytes=100,
        )
        snap = ledger.snapshot()
        assert snap["totals"] == {
            "requests": 2, "downIn": 15, "downOut": 28, "upIn": 100, "upOut": 3,
        }

    def test_ratio_only_when_upin_positive(self):
        """ratios contains only buckets with upIn > 0; downOut=0 yields 0.0."""
        ledger = TrafficLedger()
        # messages: upIn == 0 -> excluded from ratios
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=10, resp_bytes=20, duration_ms=1.0,
        )
        # projects: upIn == 100, downOut == 0 -> included with ratio 0.0
        ledger.record_upstream(
            bucket="projects", method="GET", status=200,
            req_bytes=0, resp_bytes=100,
        )
        snap = ledger.snapshot()
        assert "messages" not in snap["ratios"]
        assert "projects" in snap["ratios"]
        assert snap["ratios"]["projects"]["downOutOverUpIn"] == pytest.approx(0.0)

    def test_snapshot_does_not_mutate_internal_state(self):
        """snapshot returns fresh dicts; calling it twice is stable."""
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=10, resp_bytes=20, duration_ms=1.0,
        )
        snap1 = ledger.snapshot()
        snap2 = ledger.snapshot()
        assert snap1 == snap2
        # mutating the returned dict must not corrupt the ledger
        snap1["buckets"]["messages"]["downIn"] = 999999
        snap3 = ledger.snapshot()
        assert snap3["buckets"]["messages"]["downIn"] == 10


# ---------------------------------------------------------------------------
# snapshot SSE merge semantics
# ---------------------------------------------------------------------------


class TestSnapshotSseMerge:
    def test_sse_merged_into_existing_bucket(self):
        """events_sse: record_downstream(resp_bytes=0) + sse_up + sse_down ->
        snapshot bucket has upIn from bytesIn, downOut from bytesOut, and a
        framesEmitted key."""
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="events_sse", method="GET", status=200,
            req_bytes=0, resp_bytes=0, duration_ms=1.0,
        )
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=500)
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=10)
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=20)
        snap = ledger.snapshot()
        b = snap["buckets"]["events_sse"]
        # framesEmitted is SSE-only; errors + latencyMs come from record_downstream
        assert set(b) == {
            "requests", "downIn", "downOut", "upIn", "upOut", "framesEmitted",
            "errors4xx", "errors5xx", "latencyMs",
        }
        assert b["requests"] == 1
        assert b["upIn"] == 500          # from SSE bytesIn
        assert b["downOut"] == 30        # from SSE bytesOut (10 + 20)
        assert b["framesEmitted"] == 2
        assert b["downIn"] == 0
        assert b["upOut"] == 0
        # ratio = downOut / upIn = 30 / 500
        assert snap["ratios"]["events_sse"]["downOutOverUpIn"] == pytest.approx(30 / 500)
        # totals include the merged values
        assert snap["totals"]["upIn"] == 500
        assert snap["totals"]["downOut"] == 30
        assert snap["totals"]["requests"] == 1

    def test_sse_only_bucket_no_http_entry(self):
        """An SSE bucket with no corresponding record_downstream still appears,
        with requests=0 and the framesEmitted key present."""
        ledger = TrafficLedger()
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=100)
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=50)
        snap = ledger.snapshot()
        b = snap["buckets"]["events_sse"]
        assert b["upIn"] == 100
        assert b["downOut"] == 50
        assert b["framesEmitted"] == 1
        assert b["requests"] == 0
        assert b["downIn"] == 0
        assert b["upOut"] == 0


# ---------------------------------------------------------------------------
# enabled=False
# ---------------------------------------------------------------------------


class TestDisabledLedger:
    def test_snapshot_disabled(self):
        ledger = TrafficLedger(enabled=False)
        assert ledger.snapshot() == {"enabled": False}

    def test_record_downstream_noop(self):
        ledger = TrafficLedger(enabled=False)
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=100, resp_bytes=200, duration_ms=1.0,
        )
        assert ledger._buckets == {}

    def test_record_upstream_noop(self):
        ledger = TrafficLedger(enabled=False)
        ledger.record_upstream(
            bucket="messages", method="GET", status=200,
            req_bytes=100, resp_bytes=200,
        )
        assert ledger._buckets == {}

    def test_record_sse_upstream_noop(self):
        ledger = TrafficLedger(enabled=False)
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=100)
        assert ledger._sse == {}

    def test_record_sse_downstream_noop(self):
        ledger = TrafficLedger(enabled=False)
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=100)
        assert ledger._sse == {}

    def test_enabled_property(self):
        assert TrafficLedger().enabled is True
        assert TrafficLedger(enabled=True).enabled is True
        assert TrafficLedger(enabled=False).enabled is False

    def test_disabled_after_traffic_still_disabled(self):
        """Even after record calls, a disabled ledger's snapshot stays minimal."""
        ledger = TrafficLedger(enabled=False)
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=1, resp_bytes=1, duration_ms=1.0,
        )
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=1)
        assert ledger.snapshot() == {"enabled": False}


# ---------------------------------------------------------------------------
# negative-value clamping
# ---------------------------------------------------------------------------


class TestNegativeClamping:
    def test_downstream_negative_clamped_to_zero(self):
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=-100, resp_bytes=-200, duration_ms=1.0,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["requests"] == 1  # request still counted
            assert entry["downIn"] == 0
            assert entry["downOut"] == 0

    def test_upstream_negative_clamped_to_zero(self):
        ledger = TrafficLedger()
        ledger.record_upstream(
            bucket="messages", method="GET", status=200,
            req_bytes=-50, resp_bytes=-75,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["upOut"] == 0
            assert entry["upIn"] == 0

    def test_sse_upstream_negative_clamped(self):
        ledger = TrafficLedger()
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=-100)
        with ledger._lock:
            assert ledger._sse["events_sse"]["bytesIn"] == 0

    def test_sse_downstream_negative_bytes_clamped_but_frame_counted(self):
        """Negative bytes_out is clamped to 0 but framesEmitted still +1."""
        ledger = TrafficLedger()
        ledger.record_sse_downstream(bucket="events_sse", bytes_out=-100)
        with ledger._lock:
            assert ledger._sse["events_sse"]["bytesOut"] == 0
            assert ledger._sse["events_sse"]["framesEmitted"] == 1

    def test_negative_does_not_decrease_existing_accumulator(self):
        """A negative record after positive ones must not reduce totals."""
        ledger = TrafficLedger()
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=100, resp_bytes=200, duration_ms=1.0,
        )
        ledger.record_downstream(
            bucket="messages", method="GET", status=200,
            req_bytes=-1000, resp_bytes=-1000, duration_ms=1.0,
        )
        with ledger._lock:
            entry = ledger._buckets["messages"]
            assert entry["downIn"] == 100   # unchanged
            assert entry["downOut"] == 200  # unchanged
            assert entry["requests"] == 2


# ---------------------------------------------------------------------------
# stash_up_in / stash_up_out
# ---------------------------------------------------------------------------


class TestStashUpIn:
    def test_accumulate_twice_and_read_back(self):
        req = _FakeRequest(scope={"state": {}})
        stash_up_in(req, 100)
        stash_up_in(req, 50)
        assert _read_state_int(req.scope, _UP_IN_KEY) == 150

    def test_zero_and_negative_are_noop(self):
        req = _FakeRequest(scope={"state": {}})
        stash_up_in(req, 0)
        stash_up_in(req, -10)
        assert req.scope["state"] == {}  # nothing written

    def test_state_created_if_missing(self):
        req = _FakeRequest(scope={})
        stash_up_in(req, 50)
        assert req.scope["state"][_UP_IN_KEY] == 50

    def test_no_scope_attribute_is_safe_noop(self):
        class NoScope:
            pass

        req = NoScope()
        stash_up_in(req, 100)  # must not raise
        assert not hasattr(req, "scope") or req.scope is None or True

    def test_scope_not_dict_is_safe_noop(self):
        req = _FakeRequest(scope="not-a-dict")
        stash_up_in(req, 100)  # must not raise
        assert req.scope == "not-a-dict"

    def test_state_not_dict_is_safe_noop(self):
        req = _FakeRequest(scope={"state": "not-a-dict"})
        stash_up_in(req, 100)  # must not raise
        assert req.scope["state"] == "not-a-dict"


class TestStashUpOut:
    def test_accumulate_and_read_back(self):
        req = _FakeRequest(scope={"state": {}})
        stash_up_out(req, 200)
        stash_up_out(req, 100)
        assert _read_state_int(req.scope, _UP_OUT_KEY) == 300

    def test_zero_and_negative_are_noop(self):
        req = _FakeRequest(scope={"state": {}})
        stash_up_out(req, 0)
        stash_up_out(req, -5)
        assert req.scope["state"] == {}

    def test_in_and_out_keys_are_isolated(self):
        """traffic_up_in and traffic_up_out accumulate independently."""
        req = _FakeRequest(scope={"state": {}})
        stash_up_in(req, 10)
        stash_up_out(req, 20)
        stash_up_in(req, 5)
        assert _read_state_int(req.scope, _UP_IN_KEY) == 15
        assert _read_state_int(req.scope, _UP_OUT_KEY) == 20


# ---------------------------------------------------------------------------
# _read_state_int boundaries
# ---------------------------------------------------------------------------


class TestReadStateInt:
    def test_bool_returns_zero(self):
        """bool is a subclass of int but must read back as 0."""
        assert _read_state_int({"state": {_UP_IN_KEY: True}}, _UP_IN_KEY) == 0
        assert _read_state_int({"state": {_UP_IN_KEY: False}}, _UP_IN_KEY) == 0

    def test_negative_returns_zero(self):
        assert _read_state_int({"state": {_UP_IN_KEY: -42}}, _UP_IN_KEY) == 0

    def test_zero_returns_zero(self):
        assert _read_state_int({"state": {_UP_IN_KEY: 0}}, _UP_IN_KEY) == 0

    def test_missing_key_returns_zero(self):
        assert _read_state_int({"state": {}}, _UP_IN_KEY) == 0

    def test_state_not_dict_returns_zero(self):
        assert _read_state_int({"state": "not-a-dict"}, _UP_IN_KEY) == 0

    def test_state_missing_returns_zero(self):
        assert _read_state_int({}, _UP_IN_KEY) == 0

    def test_positive_int_returned(self):
        assert _read_state_int({"state": {_UP_IN_KEY: 999}}, _UP_IN_KEY) == 999
