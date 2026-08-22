"""Tests for 4.10.1 C: sliding-window 5xx burst WARNING observability.

Unit-level on :mod:`oc_slimapi.burst_watch` — the middleware hook is a
three-line gated call at the same wire-status point the ledger sees; these
tests pin the watcher's window/threshold/debounce/4xx semantics.
"""

from __future__ import annotations

import pytest

from oc_slimapi import burst_watch


@pytest.fixture(autouse=True)
def _clean_window_state():
    burst_watch._reset()
    yield
    burst_watch._reset()


def _burst_records(caplog):
    return [r for r in caplog.records if "upstream_5xx_burst" in r.message]


class _FakeClock:
    """Deterministic monotonic clock for window tests."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_five_5xx_in_window_logs_exactly_one_warning(caplog):
    clock = _FakeClock()
    with caplog.at_level("WARNING"):
        for i in range(4):
            burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
            assert not _burst_records(caplog)
        burst_watch.record_5xx(503, "/slimapi/session/ses_b", clock=clock)
    records = _burst_records(caplog)
    assert len(records) == 1
    message = records[0].message
    assert "count=5" in message
    assert 'window_s=60' in message
    assert '{"503":5}' in message
    # top paths: both seen paths appear
    assert "/slimapi/session/ses_a" in message
    assert "/slimapi/session/ses_b" in message


def test_four_5xx_in_window_logs_nothing(caplog):
    clock = _FakeClock()
    with caplog.at_level("WARNING"):
        for _ in range(4):
            burst_watch.record_5xx(503, "/slimapi/sessions", clock=clock)
    assert not _burst_records(caplog)


def test_trigger_resets_window_retrigger_needs_five_more(caplog):
    clock = _FakeClock()
    with caplog.at_level("WARNING"):
        for _ in range(5):
            burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
        assert len(_burst_records(caplog)) == 1
        # Same burst continues (4 more inside the window) → still one line.
        for _ in range(4):
            burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
        assert len(_burst_records(caplog)) == 1
        # A fresh full window → second line.
        for _ in range(5):
            burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
    records = _burst_records(caplog)
    assert len(records) == 2
    assert "count=5" in records[1].message


def test_4xx_never_counts():
    clock = _FakeClock()
    for status in (400, 401, 404, 405, 409, 413, 422, 499):
        burst_watch.record_5xx(status, "/slimapi/sessions", clock=clock)
    # Only 5xx feed the window: zero events after the 4xx noise.
    assert len(burst_watch._events) == 0


def test_window_expiry_drops_old_events():
    clock = _FakeClock()
    # 4 events at t=1000.
    for _ in range(4):
        burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
    # 61s later the old events are outside the 60s window.
    clock.now = 1_061.0
    burst_watch.record_5xx(503, "/slimapi/session/ses_a", clock=clock)
    assert len(burst_watch._events) == 1


def test_mixed_5xx_codes_distribution(caplog):
    clock = _FakeClock()
    with caplog.at_level("WARNING"):
        for status in (503, 503, 503, 500, 504):
            burst_watch.record_5xx(status, "/slimapi/session/ses_a", clock=clock)
    records = _burst_records(caplog)
    assert len(records) == 1
    assert '"503":3' in records[0].message
    assert '"500":1' in records[0].message
    assert '"504":1' in records[0].message


def test_top_paths_capped_at_three(caplog):
    clock = _FakeClock()
    paths = [f"/slimapi/session/ses_{i}" for i in range(5)]
    with caplog.at_level("WARNING"):
        for path in paths:
            burst_watch.record_5xx(503, path, clock=clock)
    records = _burst_records(caplog)
    assert len(records) == 1
    message = records[0].message
    # paths= payload carries at most 3 distinct paths (most-seen first;
    # all tied here, so only membership + cap are assertable).
    import orjson

    payload = orjson.loads(message.split("paths=", 1)[1])
    assert len(payload) <= 3
    assert set(payload).issubset(set(paths))


# ---------------------------------------------------------------------------
# rev-sgpt 4.10.1 MINOR-2: synthetic 500 (exception before response start)
# must NOT feed the burst watcher. Middleware-level, raw pure-ASGI.
# ---------------------------------------------------------------------------

_MINIMAL_HTTP_SCOPE = {
    "type": "http",
    "method": "GET",
    "path": "/slimapi/sessions",
    "headers": [],
    "query_string": b"",
    "state": {},
}


async def _noop_receive():
    return {"type": "http.disconnect"}


async def _noop_send(message):
    pass


async def test_synthetic_500_from_exception_path_not_counted(caplog):
    from oc_slimapi.middleware.traffic_accounting import (
        TrafficAccountingMiddleware,
    )

    async def boom_app(scope, receive, send):
        raise RuntimeError("kaboom")  # before any response.start

    outer = TrafficAccountingMiddleware(boom_app)
    with caplog.at_level("WARNING"):
        for _ in range(5):  # a full burst-worth of synthetic 500s
            with pytest.raises(RuntimeError):
                await outer(dict(_MINIMAL_HTTP_SCOPE), _noop_receive, _noop_send)
    # No actual 5xx reached the wire → the watcher stays empty.
    assert len(burst_watch._events) == 0
    assert not _burst_records(caplog)


async def test_real_5xx_response_through_middleware_counts(caplog):
    from oc_slimapi.middleware.traffic_accounting import (
        TrafficAccountingMiddleware,
    )

    async def error_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    outer = TrafficAccountingMiddleware(error_app)
    with caplog.at_level("WARNING"):
        await outer(dict(_MINIMAL_HTTP_SCOPE), _noop_receive, _noop_send)
    # Contrast case: a genuine wire 503 response DOES feed the watcher.
    assert len(burst_watch._events) == 1
    assert not _burst_records(caplog)  # one event is below threshold
