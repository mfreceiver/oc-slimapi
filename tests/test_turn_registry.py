"""Tests for the turn token fence contract (turn_registry + DigestFields +
GlobalHub stamp + proxy commit point).

Covers the 6 required scenarios from the implementation brief:

1. IncarnationStore persistence + fault tolerance.
2. TurnRegistry bump_turn monotonicity / per-sid independence / snapshot.
3. DigestFields.to_payload flat top-level emission (paired present/absent).
4. GlobalHub.publish ingest-time stamp (registry wired / not wired).
5. proxy forward bump (sid-keyed; prompt/abort only).
6. V10: ingest snapshot freezes the value — a later bump does not change an
   already-stamped entry; a new ingest stamps the new value.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import write_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.turn_registry import (
    extract_sid_from_path as _extract_sid_from_path,
    is_turn_bumping_path as _is_turn_bumping_path,
)
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub import Subscriber
from oc_slimapi.sse.hub_types import DigestFields
from oc_slimapi.turn_registry import IncarnationStore, IncarnationValue, TurnRegistry


# ── shared helpers (mirror tests/test_hub.py) ───────────────────────────────────


def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    payload: dict = {"type": event_type, "properties": properties or {}}
    return {"directory": directory, "payload": payload}


def parse_event(raw: bytes) -> tuple[str | None, dict]:
    text = raw.decode()
    event_name: str | None = None
    data_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


async def drain_queue(subscriber: Subscriber, timeout: float = 0.2) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is None:
            continue
        frames.append(item)
    return frames


async def _close_hub(hub: GlobalHub) -> None:
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── 1. IncarnationStore ─────────────────────────────────────────────────────────


def test_incarnation_store_first_run_returns_1(tmp_path):
    """New directory → load_or_bump returns 1 (persisted_last=0 +1)."""
    store = IncarnationStore(state_dir=str(tmp_path))
    assert store.load_or_bump() == 1
    # File persisted with the new value.
    assert (tmp_path / "incarnation").read_text().strip() == "1"


def test_incarnation_store_second_run_increments(tmp_path):
    """A second load reads the persisted value and adds one."""
    store = IncarnationStore(state_dir=str(tmp_path))
    first = store.load_or_bump()
    assert first == 1
    # A fresh store reading the same file must observe +1.
    store2 = IncarnationStore(state_dir=str(tmp_path))
    assert store2.load_or_bump() == 2
    store3 = IncarnationStore(state_dir=str(tmp_path))
    assert store3.load_or_bump() == 3


def test_incarnation_store_corrupt_file_does_not_crash(tmp_path):
    """A corrupt (non-integer) incarnation file degrades to fallback, no raise."""
    (tmp_path / "incarnation").write_text("not-an-int\n", encoding="utf-8")
    store = IncarnationStore(state_dir=str(tmp_path))
    # Fallback = 1; never raises.
    assert store.load_or_bump() == 1


def test_incarnation_store_unwritable_dir_does_not_crash(tmp_path):
    """An unwritable state dir degrades gracefully (warn, return fallback)."""
    store = IncarnationStore(state_dir=str(tmp_path / "missing" / "deep"))
    # Even with mkdir it must not crash; returns a positive int.
    inc = store.load_or_bump()
    assert isinstance(inc, int)
    assert inc >= 1


# ── 1b. IncarnationStore atomic-write regression (P0-4) ────────────────────────


def test_incarnation_write_is_atomic_via_temp_then_replace(tmp_path, monkeypatch):
    """P0-4: load_or_bump writes to a ``.tmp`` sibling then ``os.replace``.

    Regression guard for the half-write hazard: if the process is killed
    mid-write, the persisted file must be either the old value or the new
    value, never truncated. We assert the write goes through a temp file +
    ``os.replace`` (the atomic-commit primitive), not a direct truncate
    overwrite of the final path.
    """
    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(
        "oc_slimapi.turn_registry.os.replace", spy_replace
    )

    store = IncarnationStore(state_dir=str(tmp_path))
    assert store.load_or_bump() == 1

    # Exactly one atomic replace landed on the final path, sourced from a
    # .tmp sibling in the same directory.
    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert src.endswith("incarnation.tmp")
    assert str(tmp_path) in src
    assert dst == str(tmp_path / "incarnation")

    # The temp file must NOT linger after a successful commit.
    assert not (tmp_path / "incarnation.tmp").exists()
    # The final file holds the fully-written value.
    assert (tmp_path / "incarnation").read_text().strip() == "1"


def test_incarnation_write_tmp_cleaned_up_on_replace_failure(tmp_path, monkeypatch):
    """P0-4 + FIX-CORR-2r2 (INTENTIONAL behavior change): when os.replace
    fails (all 3 retry attempts), the orphan ``.tmp`` is cleaned up AND the
    returned value is marked **non-durable** — the process withholds the
    turn fence (snapshot → (None, None) → paired omission → Tier-2) instead
    of publishing an unconfirmed value. The in-memory value still takes the
    wall-clock floor (unpublished collision pad)."""
    fixed_now = 1_750_000_000
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.time.time", lambda: fixed_now
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    store = IncarnationStore(state_dir=str(tmp_path))
    # Seed a valid prior value so we can assert it survives a failed write.
    (tmp_path / "incarnation").write_text("7\n", encoding="utf-8")

    replace_calls = []

    def boom(src, dst):
        replace_calls.append((src, dst))
        raise OSError("rename denied (read-only filesystem)")

    monkeypatch.setattr("oc_slimapi.turn_registry.os.replace", boom)

    # load_or_bump retries the write 3 times, then degrades non-durable.
    inc = store.load_or_bump()
    assert inc == fixed_now + 1  # in-memory floor (NOT published)
    assert inc.durable is False
    assert len(replace_calls) == 3  # full retry budget exhausted
    reg = TurnRegistry(incarnation=inc)
    assert reg.durable is False
    assert reg.snapshot("s") == (None, None)
    # The prior value is untouched on disk (the atomic replace never landed).
    assert (tmp_path / "incarnation").read_text().strip() == "7"
    # The orphan temp was cleaned up by the failure path.
    assert not (tmp_path / "incarnation.tmp").exists()


def test_incarnation_write_crash_mid_write_leaves_prior_value(tmp_path, monkeypatch):
    """P0-4 + FIX-CORR-2r2: a crash (simulated as a failed open of the temp)
    cannot truncate the already-persisted file; after 3 failed attempts the
    store degrades non-durable (fence withheld, Tier-2). Pre-P0-4 a direct
    ``write_text`` would truncate the final path before writing; a crash
    there left an empty file → next restart reads 0 → incarnation 1 reused
    (fence reuse). Now the final file is only touched via the atomic rename."""
    fixed_now = 1_750_000_000
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.time.time", lambda: fixed_now
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    store = IncarnationStore(state_dir=str(tmp_path))
    (tmp_path / "incarnation").write_text("42\n", encoding="utf-8")

    # Simulate a crash by making the temp-file open raise.
    real_open = open

    def crash_on_temp_write(path, *args, **kwargs):
        if str(path).endswith("incarnation.tmp"):
            raise OSError("simulated crash before write")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", crash_on_temp_write)

    inc = store.load_or_bump()  # wall-clock floor, non-durable
    assert inc == fixed_now + 1
    assert inc.durable is False
    assert TurnRegistry(incarnation=inc).snapshot("s") == (None, None)
    # The final file is fully intact — prior value survives the crash.
    assert (tmp_path / "incarnation").read_text().strip() == "42"
    # And no temp lingers.
    assert not (tmp_path / "incarnation.tmp").exists()


# ── 1c. FIX-CORR-2: strict cross-process monotonicity under degraded writes ────


def test_same_second_double_write_failure_publishes_nothing(tmp_path, monkeypatch):
    """FIX-CORR-2r2 counterexample 1 (rev-2 gate): two processes failing in
    the SAME wall-clock second must not publish colliding incarnations.

    The round-1 floor design failed here — both processes returned the
    identical ``time+1`` and published it (fence reuse). Direction X
    ("don't publish unconfirmed") makes the collision constructionally
    impossible: both processes withhold the fence entirely (non-durable →
    snapshot (None, None) → paired field omission)."""
    T = 1_750_000_000
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.os.replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: T)
    # Keep the retry backoff from sleeping in-test.
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    (tmp_path / "incarnation").write_text("7\n", encoding="utf-8")

    inc_a = IncarnationStore(state_dir=str(tmp_path)).load_or_bump()
    inc_b = IncarnationStore(state_dir=str(tmp_path)).load_or_bump()  # same second, same disk

    # Both retries exhausted (3 attempts each) → both non-durable.
    assert inc_a.durable is False
    assert inc_b.durable is False
    # The in-memory values DO collide (this is precisely the round-1
    # defect) — proving the floor alone cannot fence. The fix is that
    # neither value is ever published:
    assert inc_a == T + 1
    assert inc_b == T + 1
    reg_a = TurnRegistry(incarnation=inc_a)
    reg_b = TurnRegistry(incarnation=inc_b)
    assert reg_a.snapshot("s") == (None, None)
    assert reg_b.snapshot("s") == (None, None)
    # Zero published values → the collision is unobservable on the wire.


def test_failed_then_recovered_write_no_published_regression(tmp_path, monkeypatch):
    """FIX-CORR-2r2 counterexample 2 (rev-2 gate, oracle-corrected
    assertions): after A fails to persist and B's write succeeds, B returns
    base+1 (the success path NEVER takes the floor) — and there is no
    regression because A never published anything, not because B > A."""
    T = 1_750_000_000
    real_replace = os.replace
    state = {"boom": True}
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.os.replace",
        lambda src, dst: real_replace(src, dst)
        if not state["boom"]
        else (_ for _ in ()).throw(OSError("rename denied")),
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    (tmp_path / "incarnation").write_text("7\n", encoding="utf-8")

    # Process A: persistence fails at time T (all 3 attempts).
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: T)
    inc_a = IncarnationStore(state_dir=str(tmp_path)).load_or_bump()
    assert inc_a.durable is False
    assert inc_a == T + 1  # floor kept in-memory — but NEVER published
    reg_a = TurnRegistry(incarnation=inc_a)
    assert reg_a.snapshot("s") == (None, None)  # A's value is withheld

    # Storage recovers; process B starts 10s later, reads disk base=7.
    state["boom"] = False
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: T + 10)
    inc_b = IncarnationStore(state_dir=str(tmp_path)).load_or_bump()

    # Success path returns base+1 (floor only exists on the failure
    # branch) and is durable — the published fence sequence contains ONLY
    # B's 8, so there is nothing to regress against.
    assert inc_b == 8
    assert inc_b.durable is True
    reg_b = TurnRegistry(incarnation=inc_b)
    assert reg_b.snapshot("s") == (8, 0)
    assert (tmp_path / "incarnation").read_text().strip() == "8"

    # Same-second variant: B also computes at time T — B still returns
    # base+1 == 8 on the success path (the floor is failure-branch only).
    (tmp_path / "incarnation").write_text("7\n", encoding="utf-8")
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: T)
    inc_c = IncarnationStore(state_dir=str(tmp_path)).load_or_bump()
    assert inc_c == 8
    assert inc_c.durable is True
    # Even though 8 == A's in-memory floor + ... would be below A's floor
    # (T+1), A never published: only C's 8 enters the fence sequence.


def test_floor_not_below_file_base(tmp_path, monkeypatch):
    """FIX-CORR-2b: the floor is a LOWER bound — a file value already
    above the clock keeps ``base + 1`` (the fence is opaque; it may be a
    huge integer). FIX-CORR-2r2: the value is non-durable (withheld)."""
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.os.replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: 1_750_000_000)
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    store = IncarnationStore(state_dir=str(tmp_path))
    (tmp_path / "incarnation").write_text("9000000000000\n", encoding="utf-8")

    inc = store.load_or_bump()
    assert inc == 9_000_000_000_001
    assert inc.durable is False


def test_durable_registry_unchanged_wire():
    """FIX-CORR-2r2 regression lock: a bare-int incarnation (legacy/test
    construction) is treated as durable — pre-r2 semantics unchanged."""
    reg = TurnRegistry(incarnation=5)
    assert reg.durable is True
    assert reg.snapshot("s") == (5, 0)
    reg.bump_turn("s")
    assert reg.snapshot("s") == (5, 1)


def test_retry_recovers_transient_failure(tmp_path, monkeypatch):
    """FIX-CORR-2r2: the startup write retries — a transient failure on
    the first two attempts recovers on the third, so the value is durable
    (published) and lands on disk."""
    fixed_now = 1_750_000_000
    real_replace = os.replace
    state = {"failures_left": 2}

    def flaky_replace(src, dst):
        if state["failures_left"] > 0:
            state["failures_left"] -= 1
            raise OSError("transient EIO")
        return real_replace(src, dst)

    monkeypatch.setattr("oc_slimapi.turn_registry.os.replace", flaky_replace)
    monkeypatch.setattr("oc_slimapi.turn_registry.time.time", lambda: fixed_now)
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    (tmp_path / "incarnation").write_text("7\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(tmp_path))
    inc = store.load_or_bump()

    # Third attempt succeeded → durable, base+1 (floor NOT applied).
    assert inc == 8
    assert inc.durable is True
    assert TurnRegistry(incarnation=inc).snapshot("s") == (8, 0)
    assert (tmp_path / "incarnation").read_text().strip() == "8"


def test_legacy_high_watermark_wins_over_corrupt_primary(tmp_path):
    """FIX-CORR-2c: a corrupt primary falls back to the legacy value via a
    high-watermark MAX over both files — the legacy value (9) must win,
    not the implicit 0 of the corrupt primary (pre-CORR-2 returned 2)."""
    (tmp_path / "primary").mkdir()
    (tmp_path / "legacy").mkdir()
    (tmp_path / "primary" / "incarnation").write_text("garbage\n", encoding="utf-8")
    (tmp_path / "legacy" / "incarnation").write_text("9\n", encoding="utf-8")

    store = IncarnationStore(
        state_dir=str(tmp_path / "primary"),
        legacy_state_dir=str(tmp_path / "legacy"),
    )
    assert store.load_or_bump() == 10
    # The overwrite attempt recovered the primary file.
    assert (tmp_path / "primary" / "incarnation").read_text().strip() == "10"


def test_legacy_lower_than_primary_ignored(tmp_path):
    """FIX-CORR-2c: when both files are valid, the max wins — a legacy
    value below the primary must not drag the base down (nor up)."""
    (tmp_path / "primary").mkdir()
    (tmp_path / "legacy").mkdir()
    (tmp_path / "primary" / "incarnation").write_text("12\n", encoding="utf-8")
    (tmp_path / "legacy" / "incarnation").write_text("9\n", encoding="utf-8")

    store = IncarnationStore(
        state_dir=str(tmp_path / "primary"),
        legacy_state_dir=str(tmp_path / "legacy"),
    )
    assert store.load_or_bump() == 13


def test_write_persisted_fsyncs_parent_dir(tmp_path, monkeypatch):
    """FIX-CORR-2a: a successful persist fsyncs BOTH the temp file and the
    parent directory (closing the rename-loss power-fail window)."""
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("oc_slimapi.turn_registry.os.fsync", spy_fsync)

    store = IncarnationStore(state_dir=str(tmp_path))
    assert store.load_or_bump() == 1

    # One fsync for the temp file, one for the parent directory fd.
    assert len(fsync_calls) == 2
    # The value landed atomically.
    assert (tmp_path / "incarnation").read_text().strip() == "1"


def test_parent_dir_fsync_failure_degrades_to_success(tmp_path, monkeypatch, caplog):
    """FIX-CORR-2a: the dir fsync is best-effort — its failure warns but
    does NOT turn the persist into a failure (the rename already landed)."""
    real_open = os.open

    def boom_on_dir_open(path, flags, *args, **kwargs):
        raise OSError("cannot open dir for fsync")

    monkeypatch.setattr("oc_slimapi.turn_registry.os.open", boom_on_dir_open)

    import logging

    store = IncarnationStore(state_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        inc = store.load_or_bump()

    assert inc == 1  # normal base+1 — NOT the degraded floor
    assert (tmp_path / "incarnation").read_text().strip() == "1"
    assert "parent-dir fsync failed" in caplog.text


def test_incarnation_reload_after_successful_atomic_write(tmp_path):
    """P0-4 end-to-end: a successful atomic write is observable by a fresh
    reader on the next process start — the value survives a real restart."""
    s1 = IncarnationStore(state_dir=str(tmp_path))
    first = s1.load_or_bump()
    assert first == 1
    # Simulate a restart by constructing a fresh store against the same dir.
    s2 = IncarnationStore(state_dir=str(tmp_path))
    assert s2.load_or_bump() == 2
    s3 = IncarnationStore(state_dir=str(tmp_path))
    assert s3.load_or_bump() == 3


# ── 2. TurnRegistry ─────────────────────────────────────────────────────────────


def test_bump_turn_is_monotonically_increasing():
    reg = TurnRegistry(incarnation=5)
    assert reg.bump_turn("s1") == 1
    assert reg.bump_turn("s1") == 2
    assert reg.bump_turn("s1") == 3


def test_bump_turn_per_sid_are_independent():
    reg = TurnRegistry(incarnation=5)
    reg.bump_turn("s1")  # 1
    reg.bump_turn("s1")  # 2
    reg.bump_turn("s2")  # different sid → independent counter
    assert reg.snapshot("s1") == (5, 2)
    assert reg.snapshot("s2") == (5, 1)


def test_snapshot_unobserved_sid_returns_inc_and_zero():
    """snapshot(sid) always returns a tuple; an unobserved sid → (inc, 0)."""
    reg = TurnRegistry(incarnation=1)
    assert reg.snapshot("never-bumped") == (1, 0)


def test_turns_map_is_lru_bounded():
    """B3: ``_turns`` is LRU-bounded by ``_TURNS_MAX``.

    Bumping more distinct sids than the cap evicts the least-recently-bumped;
    an evicted sid falls back to ``snapshot == (inc, 0)`` and, if re-bumped
    within the SAME incarnation, restarts at turn 1 — the disclosed
    within-incarnation regression (see ``_TURNS_MAX`` docstring)."""
    from oc_slimapi.turn_registry import _TURNS_MAX

    reg = TurnRegistry(incarnation=7)
    # Fill to the cap, then one more distinct sid forces a single eviction.
    for i in range(_TURNS_MAX + 1):
        reg.bump_turn(f"sid_{i}")
    # The map is capped (never unbounded).
    assert len(reg._turns) == _TURNS_MAX
    # sid_0 (first-bumped, least-recently-active) is evicted → snapshot falls
    # back to (inc, 0).
    assert reg.snapshot("sid_0") == (7, 0)
    # The most-recently-bumped sid is still resident with turn 1.
    assert reg.snapshot(f"sid_{_TURNS_MAX}") == (7, 1)
    # An evicted sid re-bumped within the same incarnation restarts at 1
    # (the disclosed regression — NOT a restart, which bumps incarnation).
    assert reg.bump_turn("sid_0") == 1


def test_lru_eviction_emits_warning(caplog, monkeypatch):
    """B7 (P1-23): LRU eviction emits an observability warning.

    Behaviour is unchanged (oracle ruled the eviction→new-incarnation cure
    expands the blast radius since incarnation is process-level frozen). The
    warning makes the practically-unreachable edge visible to ops. Uses a
    small cap via monkeypatch so the test is fast (the real _TURNS_MAX is
    exercised by ``test_turns_map_is_lru_bounded`` above)."""
    import logging

    monkeypatch.setattr("oc_slimapi.turn_registry._TURNS_MAX", 3)
    reg = TurnRegistry(incarnation=7)
    with caplog.at_level(logging.WARNING, logger="oc_slimapi.turn_registry"):
        # 4 distinct sids under cap=3 → exactly one eviction (sid_0).
        for i in range(4):
            reg.bump_turn(f"sid_{i}")
    evict_msgs = [r for r in caplog.records if "LRU evicted sid" in r.getMessage()]
    assert len(evict_msgs) == 1
    msg = evict_msgs[0].getMessage()
    assert "sid_0" in msg
    assert "incarnation 7" in msg
    # No eviction when under cap → no warning. caplog.records is cumulative
    # across with-blocks, so clear before the negative check.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="oc_slimapi.turn_registry"):
        reg2 = TurnRegistry(incarnation=1)
        reg2.bump_turn("only_sid")
    assert not [r for r in caplog.records if "LRU evicted sid" in r.getMessage()]


# ── 3. DigestFields.to_payload ──────────────────────────────────────────────────


def test_digest_fields_omits_turn_pair_when_both_none():
    payload = DigestFields().to_payload("s1")
    assert "turnIncarnation" not in payload
    assert "turn" not in payload


def test_digest_fields_emits_turn_pair_flat_top_level_when_set():
    fields = DigestFields(turn_incarnation=7, turn=3, status="busy")
    payload = fields.to_payload("s1")
    # CRITICAL wire-shape assertion: the fields are at the FLAT top level,
    # alongside sessionID/status — NOT nested in a sub-`properties` dict.
    assert payload["turnIncarnation"] == 7
    assert payload["turn"] == 3
    assert payload["sessionID"] == "s1"
    assert payload["status"] == "busy"
    # Flat sibling keys, not nested.
    assert "properties" not in payload


def test_digest_fields_omits_pair_when_either_is_none():
    # turn None → both omitted (paired presence).
    only_inc = DigestFields(turn_incarnation=7, turn=None)
    payload = only_inc.to_payload("s1")
    assert "turnIncarnation" not in payload
    assert "turn" not in payload
    # turnIncarnation None → both omitted.
    only_turn = DigestFields(turn_incarnation=None, turn=4)
    payload2 = only_turn.to_payload("s1")
    assert "turnIncarnation" not in payload2
    assert "turn" not in payload2


# ── 4. GlobalHub.publish ingest-time stamp ──────────────────────────────────────


async def test_publish_stamps_turn_when_scope_known():
    """session.status ingest stamps turn/inc onto the entry (registry wired)."""
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=42)
        reg.bump_turn("s1")  # turn = 1
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        hub.flush()

        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        data = digests[0]
        assert data["turnIncarnation"] == 42
        assert data["turn"] == 1
        assert data["status"] == "busy"
    finally:
        await _close_hub(hub)


async def test_publish_stamps_inc_zero_for_unobserved_sid():
    """An unobserved sid → snapshot always returns (inc, 0); the digest
    now always carries turnIncarnation/turn once a registry is wired
    (no header-gated degrade)."""
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=42)
        hub.set_turn_registry(reg)
        # No bump for s2 → snapshot returns (42, 0).

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s2", "status": "idle",
        }))
        hub.flush()

        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        data = digests[0]
        assert data["turnIncarnation"] == 42
        assert data["turn"] == 0
        assert data["status"] == "idle"
    finally:
        await _close_hub(hub)


async def test_publish_omits_turn_when_no_registry_wired():
    """No TurnRegistry injected → no stamping at all (legacy behaviour)."""
    hub = GlobalHub(client=None)
    try:
        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        hub.flush()
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert "turnIncarnation" not in digests[0]
        assert "turn" not in digests[0]
    finally:
        await _close_hub(hub)


async def test_non_durable_registry_digest_omits_turn_pair():
    """FIX-CORR-2r2: a wired but NON-DURABLE registry (persistence
    unconfirmed at startup) omits the paired turn fields on the digest —
    identical wire shape to "no registry wired" — so ocdroid degrades to
    Tier-2 instead of fencing on an unconfirmed value (contract §7.5
    paired-optional semantics)."""
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=IncarnationValue(42, durable=False))
        reg.bump_turn("s1")
        assert reg.durable is False
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        hub.flush()

        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert "turnIncarnation" not in digests[0]
        assert "turn" not in digests[0]
        # The digest itself is unaffected — only the fence pair is withheld.
        assert digests[0]["status"] == "busy"
    finally:
        await _close_hub(hub)


# ── 5. proxy forward bump (sid-keyed) ───────────────────────────────────────────


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-turn-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.include_router(write_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app


def _passthrough_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield b'{"ok":true}'

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    return handler


async def test_write_route_bumps_turn_on_prompt_async(upstream_factory):
    """POST /slimapi/session/{sid}/prompt_async?v=4 bumps turn (terminal:
    the bump moved from the retired catch-all forwarder into the annexed
    write pipeline — S2 bump-before-send semantics preserved)."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=4)
    app.state.turn_registry = reg  # inject into state (lifespan does this)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/session/ses_abc/prompt_async?v=4")
    assert response.status_code == 200
    assert reg.snapshot("ses_abc") == (4, 1)
    # Second prompt_async is monotonic.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/slimapi/session/ses_abc/prompt_async?v=4")
    assert reg.snapshot("ses_abc") == (4, 2)


async def test_write_route_bumps_turn_on_abort(upstream_factory):
    """POST /slimapi/session/{sid}/abort?v=4 bumps (contract §3.y.3)."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=1)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/session/ses_abc/abort?v=4")
    assert response.status_code == 200
    assert reg.snapshot("ses_abc") == (1, 1)


async def test_retired_catch_all_prompt_no_longer_bumps(upstream_factory):
    """The retired legacy passthrough surface is closed (§8.2 3.0.0):
    POST /session/{sid}/prompt is NOT a collected write endpoint — it now
    returns 404 thin_route_not_found and never bumps the turn fence."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=3)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/session/ses_abc/prompt")
    assert response.status_code == 404
    assert reg.snapshot("ses_abc") == (3, 0)
    assert reg._turns == {}


async def test_write_route_does_not_bump_on_non_post_method(upstream_factory):
    """Method gate: GET on a bumping-looking path must NOT bump turn.
    GET /session/{sid}/prompt now 404s (closed surface, no bump); a
    subsequent POST prompt_async still bumps to 1 (no slot consumed)."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=2)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session/ses_abc/prompt")
    assert response.status_code == 404
    assert reg.snapshot("ses_abc") == (2, 0)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/slimapi/session/ses_abc/prompt_async?v=4")
    assert reg.snapshot("ses_abc") == (2, 1)


async def test_write_route_does_not_bump_on_non_bumping_session_request(upstream_factory):
    """A non-bumping GET write (session read of the write group's surface
    is not annexed — the closed catch-all 404s) does NOT bump turn."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=1)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session/ses_abc/message")
    assert response.status_code == 404
    # Snapshot still returns a tuple (unobserved sid → (inc, 0)); no bump
    # recorded in the _turns dict.
    assert reg.snapshot("ses_abc") == (1, 0)
    assert reg._turns == {}


async def test_write_route_no_turn_registry_in_state_still_works(upstream_factory):
    """Absence of app.state.turn_registry (getattr default None) → no-op,
    no crash, write still completes."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    # Deliberately do NOT set app.state.turn_registry.

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/slimapi/session/ses_abc/prompt_async?v=4")
    assert response.status_code == 200


# ── path helpers ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected_sid", [
    ("/session/ses_abc/prompt", "ses_abc"),
    ("/session/ses_abc/abort", "ses_abc"),
    ("/session/01HQXXXX/message", "01HQXXXX"),
    ("/session/ses_abc", "ses_abc"),
    ("/session", None),
    ("/global/event", None),
    ("/slimapi/health", None),
])
def test_extract_sid_from_path(path, expected_sid):
    assert _extract_sid_from_path(path) == expected_sid


@pytest.mark.parametrize("path,expected", [
    # M3-3: plain sync /prompt is NOT collected (§8.2) — the classifier
    # must not match it; only prompt_async + abort bump.
    ("/session/ses_abc/prompt", False),
    ("/session/ses_abc/prompt/", False),
    ("/session/ses_abc/prompt_async", True),   # ocdroid's production send path
    ("/session/ses_abc/prompt_async/", True),
    ("/session/ses_abc/abort", True),
    ("/session/ses_abc/abort/", True),
    ("/session/ses_abc/message", False),
    ("/session/ses_abc", False),
    ("/session/ses_abc/prompt/sub", False),
    ("/session/ses_abc/prompting", False),  # prefix guard
    ("/global/event", False),
])
def test_is_turn_bumping_path(path, expected):
    assert _is_turn_bumping_path(path) is expected


# ── 6. V10: ingest snapshot freezes the value ───────────────────────────────────


async def test_v10_ingest_snapshot_freezes_value_against_later_bump():
    """A bump AFTER ingest must NOT change an already-stamped entry's value.

    Contract §7.4 / V10: stamp happens at ingest (publish), reading the
    *current* turn int. The entry stores a Python int (value copy), so a
    later bump cannot retroactively mutate it. A subsequent ingest stamps
    the new (higher) value.
    """
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=5)
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)

        # Bump to turn=3, then ingest a session.status → entry frozen at 3.
        for _ in range(3):
            reg.bump_turn("s1")
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        entry = hub.pending["s1"]
        assert entry.turn_incarnation == 5
        assert entry.turn == 3  # frozen

        # Now bump to turn=4 AFTER the stamp. The pending entry must NOT
        # change (no reference held — it's a copied int).
        reg.bump_turn("s1")
        assert reg.snapshot("s1") == (5, 4)  # registry moved on
        assert entry.turn == 3  # still frozen at the ingest-time value

        # Flush the first window — the emitted digest carries the frozen 3.
        hub.flush()
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert digests[0]["turn"] == 3
        assert digests[0]["turnIncarnation"] == 5

        # A NEW session.status ingest stamps the now-current turn=4.
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "idle",
        }))
        new_entry = hub.pending["s1"]
        assert new_entry.turn == 4
    finally:
        await _close_hub(hub)


async def test_v10_busy_flush_carries_frozen_stamp():
    """The G1 busy-clears-sticky flush_sid path also carries the frozen stamp.

    The stamp runs BEFORE the flush_sid() call in publish(), so the
    immediate flush emits the stamped turn/inc. Regression guard for the
    ordering of the stamp block relative to the busy flush.
    """
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=8)
        reg.bump_turn("s1")  # turn=1
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        # Establish a sticky lastError first so the busy path clears it.
        hub.sticky_last_error["s1"] = {
            "name": "err", "message": "boom", "at": 1,
        }
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        # busy → flush_sid immediate (no debounce wait).
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert digests[0]["turnIncarnation"] == 8
        assert digests[0]["turn"] == 1
        # busy clear sets lastError to explicit null.
        assert digests[0]["lastError"] is None
    finally:
        await _close_hub(hub)


# ── 7. T9 (P1-4): incarnation state dir split + legacy migration ────────────────
#
# IncarnationStore now takes (state_dir, legacy_state_dir=None). The new
# state_dir wins; the legacy (old access_log dir) is consulted only when
# the new path is missing/corrupt — monotonic migration without reset,
# without deleting the legacy file.


def test_legacy_migration_preserves_monotonicity(tmp_path):
    """Only legacy file present (value=5) → load_or_bump returns 6; the new
    path file is written with 6; the legacy file is left at 5."""
    new_dir = tmp_path / "state"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "incarnation").write_text("5\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(new_dir), legacy_state_dir=str(legacy_dir))
    inc = store.load_or_bump()
    assert inc == 6
    # New path now carries the migrated value.
    assert (new_dir / "incarnation").read_text(encoding="utf-8").strip() == "6"
    # Legacy file untouched.
    assert (legacy_dir / "incarnation").read_text(encoding="utf-8").strip() == "5"


def test_new_path_preferred_over_legacy(tmp_path):
    """Both new (10) and legacy (5) present → new wins → load_or_bump returns 11."""
    new_dir = tmp_path / "state"
    legacy_dir = tmp_path / "legacy"
    new_dir.mkdir()
    legacy_dir.mkdir()
    (new_dir / "incarnation").write_text("10\n", encoding="utf-8")
    (legacy_dir / "incarnation").write_text("5\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(new_dir), legacy_state_dir=str(legacy_dir))
    assert store.load_or_bump() == 11


def test_corrupt_new_path_falls_back_to_legacy(tmp_path):
    """New path corrupt ("abc"), legacy=5 → fallback to legacy → returns 6
    (no reset to 1)."""
    new_dir = tmp_path / "state"
    legacy_dir = tmp_path / "legacy"
    new_dir.mkdir()
    legacy_dir.mkdir()
    (new_dir / "incarnation").write_text("abc\n", encoding="utf-8")
    (legacy_dir / "incarnation").write_text("5\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(new_dir), legacy_state_dir=str(legacy_dir))
    # Returns legacy+1=6 (NOT 1 — corrupt new path must not reset incarnation).
    assert store.load_or_bump() == 6


def test_legacy_file_remains_after_migration(tmp_path):
    """After migration the legacy file is preserved (never deleted)."""
    new_dir = tmp_path / "state"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "incarnation").write_text("5\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(new_dir), legacy_state_dir=str(legacy_dir))
    store.load_or_bump()
    # The legacy file must still exist on disk after migration.
    assert (legacy_dir / "incarnation").exists()
    # And its content is unchanged (monotonic migration, not destructive move).
    assert (legacy_dir / "incarnation").read_text(encoding="utf-8").strip() == "5"


def test_unwritable_new_path_returns_computed_inc(tmp_path, monkeypatch):
    """New path unwritable (write fails) → still returns a computed inc,
    does not crash, does not return a fixed fallback.

    FIX-CORR-2r2 (INTENTIONAL behavior change): the computed value under a
    failed persist is the wall-clock floor max(base+1, time+1) marked
    **non-durable** — the process withholds the fence (Tier-2) instead of
    publishing a value the disk never learned."""
    fixed_now = 1_750_000_000
    monkeypatch.setattr(
        "oc_slimapi.turn_registry.time.time", lambda: fixed_now
    )
    monkeypatch.setattr("oc_slimapi.turn_registry.time.sleep", lambda s: None)
    new_dir = tmp_path / "state"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "incarnation").write_text("5\n", encoding="utf-8")

    store = IncarnationStore(state_dir=str(new_dir), legacy_state_dir=str(legacy_dir))
    # Force the write to fail — simulates a read-only / unavailable state dir.
    monkeypatch.setattr(store, "_write_persisted", lambda inc: False)
    # Legacy base = 5 → bare inc would be 6; the floor dominates
    # (fixed_now + 1) — and the value is withheld (non-durable).
    inc = store.load_or_bump()
    assert inc == fixed_now + 1  # NOT a fixed fallback like 1, NOT bare 6
    assert inc.durable is False
