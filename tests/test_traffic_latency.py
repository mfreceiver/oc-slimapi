"""Tests for TrafficLedger latency percentiles + per-bucket error counts (M5)."""
from __future__ import annotations

from oc_slimapi.traffic import TrafficLedger


def test_latency_percentiles_and_error_counts() -> None:
    led = TrafficLedger(enabled=True)
    # 10 requests: durations 10..100 ms.
    # statuses: one 404, one 500, rest 200.
    for i in range(1, 11):
        status = 404 if i == 3 else (500 if i == 7 else 200)
        led.record_downstream(
            bucket="messages",
            method="GET",
            status=status,
            req_bytes=0,
            resp_bytes=0,
            duration_ms=float(i * 10),
        )
    snap = led.snapshot()
    assert snap["enabled"] is True
    b = snap["buckets"]["messages"]
    assert b["requests"] == 10
    assert b["errors4xx"] == 1
    assert b["errors5xx"] == 1
    lat = b["latencyMs"]
    assert lat["count"] == 10
    # sorted samples = [10,20,...,100]
    # p50 index = min(9, int(10*0.5)=5) -> 60 ; p90 index = min(9, 9) -> 100
    assert lat["p50"] == 60.0
    assert lat["p90"] == 100.0
    assert lat["p99"] == 100.0


def test_latency_samples_bounded() -> None:
    led = TrafficLedger(enabled=True)
    for i in range(2000):
        led.record_downstream(
            bucket="sessions", method="GET", status=200,
            req_bytes=0, resp_bytes=0, duration_ms=float(i),
        )
    snap = led.snapshot()
    # deque maxlen caps retained samples at 1024
    assert snap["buckets"]["sessions"]["latencyMs"]["count"] == 1024


def test_no_latency_key_when_no_samples() -> None:
    led = TrafficLedger(enabled=True)
    # SSE-only bucket via record_sse_downstream (no record_downstream calls)
    led.record_sse_downstream(bucket="events_sse", bytes_out=42)
    snap = led.snapshot()
    b = snap["buckets"]["events_sse"]
    assert "latencyMs" not in b
    assert b["errors4xx"] == 0
    assert b["errors5xx"] == 0


def test_disabled_ledger_snapshot() -> None:
    led = TrafficLedger(enabled=False)
    led.record_downstream(
        bucket="x", method="GET", status=500,
        req_bytes=0, resp_bytes=0, duration_ms=1.0,
    )
    assert led.snapshot() == {"enabled": False}


def test_latency_small_sample_index_convention() -> None:
    """Lock the percentile index convention so a future refactor does not
    silently change observed values.

    Convention: ``samples[min(n-1, int(n*p/100))]`` on the sorted samples — a
    high-biased nearest-rank (``int`` truncation, not interpolation).
    """
    # n=1 -> the single sample for all percentiles.
    led = TrafficLedger(enabled=True)
    led.record_downstream(
        bucket="a", method="GET", status=200,
        req_bytes=0, resp_bytes=0, duration_ms=5.0,
    )
    assert led.snapshot()["buckets"]["a"]["latencyMs"] == {
        "p50": 5.0, "p90": 5.0, "p99": 5.0, "count": 1,
    }

    # n=2 -> sorted [10,20]; int(2*0.5)=1 -> p50=20; int(2*0.9)=1 -> p90=20.
    led2 = TrafficLedger(enabled=True)
    for d in (10.0, 20.0):
        led2.record_downstream(
            bucket="b", method="GET", status=200,
            req_bytes=0, resp_bytes=0, duration_ms=d,
        )
    lat2 = led2.snapshot()["buckets"]["b"]["latencyMs"]
    assert lat2["p50"] == 20.0
    assert lat2["p90"] == 20.0
    assert lat2["p99"] == 20.0
    assert lat2["count"] == 2

    # n=3 -> sorted [10,20,30]; int(3*0.5)=1 -> p50=20; int(3*0.9)=2 -> p90=30.
    led3 = TrafficLedger(enabled=True)
    for d in (10.0, 20.0, 30.0):
        led3.record_downstream(
            bucket="c", method="GET", status=200,
            req_bytes=0, resp_bytes=0, duration_ms=d,
        )
    lat3 = led3.snapshot()["buckets"]["c"]["latencyMs"]
    assert lat3["p50"] == 20.0
    assert lat3["p90"] == 30.0
    assert lat3["p99"] == 30.0
    assert lat3["count"] == 3
