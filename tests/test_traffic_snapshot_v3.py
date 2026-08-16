"""v3-contract §9.2 — snapshot aggregation matrix + sseActive carry (Batch A).

Pure aggregator over parsed access-log rows: the §9.2 count matrix
(date × selectorResult × wireVersion × directoryForm × recordType ×
statusClass × bucket) and the sseActive per-(day × selectorResult) series
with the carry-in formula

    sseActive[D+1, k] = sseActive[D, k] + sse_open[D, k] − matched_sse_close[D, k]

plus orphan-close correction. Also covers the TrafficLedger in-memory
sseActive counters.
"""
from __future__ import annotations

from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.traffic_snapshot import aggregate_v3_observability


def _row(ts: str, **kw) -> dict:
    row = {
        "ts": ts,
        "method": "GET",
        "path": "/slimapi/events",
        "bucket": "events_sse",
        "status": 200,
        "recordType": "request",
        "lifecycleId": None,
        "wireVersion": None,
        "selectorResult": None,
        "directoryForm": None,
    }
    row.update(kw)
    return row


def _flat(row: dict) -> str:
    status = row.get("status")
    sc = "none" if not isinstance(status, int) else f"{status // 100}xx"
    return "|".join((
        row.get("selectorResult") or "null",
        row.get("wireVersion") or "null",
        row.get("directoryForm") or "null",
        row.get("recordType") or "request",
        sc,
        str(row.get("bucket") or "null"),
    ))


# ---------------------------------------------------------------------------
# count matrix
# ---------------------------------------------------------------------------

def test_matrix_counts_one_per_row():
    rows = [
        _row("2026-08-16T10:00:00+08:00", selectorResult="v2", wireVersion="2"),
        _row("2026-08-16T10:00:01+08:00", selectorResult="v2", wireVersion="2"),
        _row("2026-08-16T10:00:02+08:00", selectorResult="v3", wireVersion="3"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["counts"][_flat(rows[0])] == 2
    assert out["counts"][_flat(rows[2])] == 1


def test_matrix_dimensions_distinct():
    rows = [
        _row("2026-08-16T10:00:00+08:00", bucket="health", status=400,
             selectorResult="rejected", wireVersion=None, directoryForm=None),
        _row("2026-08-16T10:00:00+08:00", bucket="health", status=200,
             selectorResult="v2", wireVersion="2", directoryForm=None),
        _row("2026-08-16T10:00:00+08:00", bucket="sessions", status=200,
             selectorResult="absent", wireVersion="2", directoryForm="query"),
    ]
    out = aggregate_v3_observability(rows)
    for r in rows:
        assert out["counts"][_flat(r)] == 1


def test_matrix_splits_by_date():
    rows = [
        _row("2026-08-16T23:59:59+08:00", selectorResult="v2", wireVersion="2"),
        _row("2026-08-17T00:00:01+08:00", selectorResult="v2", wireVersion="2"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["countsByDate"]["2026-08-16"][_flat(rows[0])] == 1
    assert out["countsByDate"]["2026-08-17"][_flat(rows[1])] == 1


# ---------------------------------------------------------------------------
# sseActive: same-day open/close, carry-in, orphan close
# ---------------------------------------------------------------------------

def _sse(ts: str, rt: str, lid: int, result: str = "absent") -> dict:
    return _row(ts, recordType=rt, lifecycleId=lid, selectorResult=result)


def test_sse_active_same_day_open_close():
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1),
        _sse("2026-08-16T10:05:00+08:00", "sse_close", 1),
        _sse("2026-08-16T11:00:00+08:00", "sse_open", 2),
    ]
    out = aggregate_v3_observability(rows)
    # Window-start stock for the day = 0 (the first open happens during it).
    assert out["sseActive"]["2026-08-16"]["absent"] == 0
    # End-of-window live stock: 1 open − 1 close + 1 open = 1.
    assert out["sseLive"]["absent"] == 1


def test_sse_active_carry_in_sequence_1_open_crosses_day_unclosed():
    """Sequence A: opened on day D, still open across midnight into D+1."""
    rows = [
        _sse("2026-08-16T23:00:00+08:00", "sse_open", 1),
        _row("2026-08-17T00:00:30+08:00", bucket="health"),  # day rollover
    ]
    out = aggregate_v3_observability(rows)
    # Formula: sseActive[D+1] = sseActive[D] + opens[D] − matched_closes[D]
    assert out["sseActive"]["2026-08-16"]["absent"] == 0
    assert out["sseActive"]["2026-08-17"]["absent"] == 1
    assert out["sseOpens"]["2026-08-16"]["absent"] == 1
    assert out["sseMatchedCloses"]["2026-08-16"].get("absent", 0) == 0
    assert out["sseLive"]["absent"] == 1


def test_sse_active_carry_in_sequence_2_closes_after_day_boundary():
    """Sequence B: opened on day D, closed on day D+1 (matched cross-day)."""
    rows = [
        _sse("2026-08-16T23:00:00+08:00", "sse_open", 1),
        _sse("2026-08-17T00:01:00+08:00", "sse_close", 1),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseActive"]["2026-08-16"]["absent"] == 0
    assert out["sseActive"]["2026-08-17"]["absent"] == 1
    # Day D+1 matched the close against the carried-in open.
    assert out["sseMatchedCloses"]["2026-08-17"]["absent"] == 1
    assert out["sseLive"]["absent"] == 0
    # Formula check for D+1's window end: 1 − 1 = 0 carried to a D+2.
    rows2 = rows + [_row("2026-08-18T00:00:30+08:00", bucket="health")]
    out2 = aggregate_v3_observability(rows2)
    assert out2["sseActive"]["2026-08-18"]["absent"] == 0


def test_sse_active_orphan_close_correction():
    """A close with no visible open (opened before the window) clamps at 0
    and is counted as an orphan close — never negative stock."""
    rows = [
        _row("2026-08-16T09:00:00+08:00", bucket="health"),
        _sse("2026-08-16T09:00:01+08:00", "sse_close", 99),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOrphanCloses"]["2026-08-16"]["absent"] == 1
    assert out["sseActive"]["2026-08-16"]["absent"] == 0
    assert out["sseLive"]["absent"] == 0


# ---------------------------------------------------------------------------
# §11.8 lifecycle pairing: closes match by lifecycleId, not by stock count
# ---------------------------------------------------------------------------


def test_sse_close_mismatched_lifecycle_id_is_orphan_not_drain():
    """A close whose lifecycleId matches NO prior open must NOT decrement
    active — decrementing by stock count would wrongly drain another live
    connection's slot (B3)."""
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1),
        _sse("2026-08-16T10:00:01+08:00", "sse_open", 2),
        _sse("2026-08-16T10:30:00+08:00", "sse_close", 999),  # unknown id
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOpens"]["2026-08-16"]["absent"] == 2
    assert out["sseMatchedCloses"]["2026-08-16"].get("absent", 0) == 0
    assert out["sseOrphanCloses"]["2026-08-16"]["absent"] == 1
    # Both real connections stay live — the bogus close drained nothing.
    assert out["sseLive"]["absent"] == 2


def test_sse_close_after_restart_is_all_orphan():
    """Restart simulation: the aggregation window starts with an empty open
    set — closes for connections opened before the restart never match and
    must all be orphans (no matched, no negative stock)."""
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_close", 5),
        _sse("2026-08-16T10:00:01+08:00", "sse_close", 7),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOrphanCloses"]["2026-08-16"]["absent"] == 2
    assert out["sseMatchedCloses"]["2026-08-16"].get("absent", 0) == 0
    assert out["sseLive"]["absent"] == 0


def test_sse_close_without_lifecycle_id_is_orphan():
    """A close row lacking lifecycleId (unpairable by construction) is an
    orphan — it must not consume a live slot either."""
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1),
        _sse("2026-08-16T10:05:00+08:00", "sse_close", None),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOrphanCloses"]["2026-08-16"]["absent"] == 1
    assert out["sseMatchedCloses"]["2026-08-16"].get("absent", 0) == 0
    assert out["sseLive"]["absent"] == 1


def test_sse_pairing_is_per_dim():
    """An id that matches an open in ANOTHER dim does not pair — dims keep
    independent open sets."""
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1, result="v3"),
        _sse("2026-08-16T10:00:01+08:00", "sse_close", 1, result="absent"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOrphanCloses"]["2026-08-16"]["absent"] == 1
    assert out["sseMatchedCloses"]["2026-08-16"].get("v3", 0) == 0
    assert out["sseLive"]["v3"] == 1
    assert out["sseLive"]["absent"] == 0


def test_sse_matched_pairing_regression_normal_flow():
    """Normal pairing (same id open→close, incl. cross-day) still matches —
    the id-set must be consumed on match (double close of one id = orphan)."""
    rows = [
        _sse("2026-08-16T23:00:00+08:00", "sse_open", 1),
        _sse("2026-08-17T00:01:00+08:00", "sse_close", 1),
        # duplicate close of the already-consumed id → orphan now
        _sse("2026-08-17T00:02:00+08:00", "sse_close", 1),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseMatchedCloses"]["2026-08-17"]["absent"] == 1
    assert out["sseOrphanCloses"]["2026-08-17"]["absent"] == 1
    assert out["sseLive"]["absent"] == 0


def test_sse_active_four_dims_independent():
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1, result="v3"),
        _sse("2026-08-16T10:00:01+08:00", "sse_open", 2, result="absent"),
        _sse("2026-08-16T10:00:02+08:00", "sse_open", 3, result="v2"),
        # not_applicable: catch-all SSE (/event, /global/event) — the fourth
        # dim now has a real producer (B2).
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseLive"]["v3"] == 1
    assert out["sseLive"]["absent"] == 1
    assert out["sseLive"]["v2"] == 1
    assert out["sseLive"].get("not_applicable", 0) == 0


def test_sse_not_applicable_dim_nonzero_carry():
    """Catch-all SSE rows (selectorResult=not_applicable, from /event and
    /global/event passthrough) aggregate into non-zero opens / matched
    closes / window-start sseActive (B2)."""
    rows = [
        _row("2026-08-16T09:00:00+08:00", bucket="passthrough"),  # day-1 anchor
        _sse("2026-08-16T09:00:01+08:00", "sse_open", 1, result="not_applicable"),
        _row("2026-08-16T09:00:02+08:00", bucket="passthrough"),
        # day 2: the open is still live at window start, then closes.
        _sse("2026-08-17T00:00:30+08:00", "sse_close", 1, result="not_applicable"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOpens"]["2026-08-16"]["not_applicable"] == 1
    assert out["sseActive"]["2026-08-17"]["not_applicable"] == 1
    assert out["sseMatchedCloses"]["2026-08-17"]["not_applicable"] == 1
    assert out["sseLive"]["not_applicable"] == 0


def test_sse_open_without_v_maps_to_absent():
    """No-v SSE request (old client) → selectorResult absent on the SSE rows."""
    rows = [
        _sse("2026-08-16T10:00:00+08:00", "sse_open", 1, result="absent"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseOpens"]["2026-08-16"]["absent"] == 1


def test_rejected_and_exempt_never_counted_as_sse_dims():
    rows = [
        _row("2026-08-16T10:00:00+08:00", selectorResult="rejected", status=400),
        _row("2026-08-16T10:00:01+08:00", selectorResult="exempt", status=200,
             bucket="other"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseLive"].get("rejected", 0) == 0
    assert out["sseLive"].get("exempt", 0) == 0


# ---------------------------------------------------------------------------
# TrafficLedger in-memory counterparts
# ---------------------------------------------------------------------------

def test_ledger_sse_lifecycle_counters():
    ledger = TrafficLedger(enabled=True)
    ledger.record_sse_lifecycle(result="v3", opened=True)
    ledger.record_sse_lifecycle(result="v3", opened=True)
    snap = ledger.snapshot()["v3"]
    assert snap["sseActive"]["v3"] == 2
    assert snap["sseLifecycle"]["v3"]["opens"] == 2
    ledger.record_sse_lifecycle(result="v3", opened=False)
    snap = ledger.snapshot()["v3"]
    assert snap["sseActive"]["v3"] == 1
    assert snap["sseLifecycle"]["v3"]["closes"] == 1


def test_ledger_sse_lifecycle_orphan_close():
    ledger = TrafficLedger(enabled=True)
    ledger.record_sse_lifecycle(result="absent", opened=False)
    snap = ledger.snapshot()["v3"]
    assert snap["sseActive"]["absent"] == 0  # clamped, never negative
    assert snap["sseLifecycle"]["absent"]["orphanCloses"] == 1


def test_ledger_selector_request_matrix():
    ledger = TrafficLedger(enabled=True)
    ledger.record_selector_request(
        bucket="health", status=200,
        selector_result="v2", wire_version="2",
        directory_form=None, record_type="request",
    )
    ledger.record_selector_request(
        bucket="health", status=400,
        selector_result="rejected", wire_version=None,
        directory_form=None, record_type="request",
    )
    snap = ledger.snapshot()["v3"]
    assert snap["matrix"]["v2|2|null|request|2xx|health"] == 1
    assert snap["matrix"]["rejected|null|null|request|4xx|health"] == 1


def test_ledger_v3_section_absent_when_disabled():
    ledger = TrafficLedger(enabled=False)
    ledger.record_sse_lifecycle(result="v3", opened=True)
    ledger.record_selector_request(bucket="health", status=200)
    snap = ledger.snapshot()
    assert snap == {"enabled": False}
