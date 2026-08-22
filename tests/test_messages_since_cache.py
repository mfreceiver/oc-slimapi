from __future__ import annotations

import base64

import orjson

from oc_slimapi.since_cache import SinceCache


def _cache(**overrides) -> SinceCache:
    values = {
        "enabled": True,
        "max_entries": 256,
        "max_bytes": 1024 * 1024,
        "max_entry_bytes": 1024 * 1024,
        "epoch": "epoch-1",
    }
    values.update(overrides)
    return SinceCache(**values)


def test_cas_identical_loser_reuses_generation_and_differing_loser_is_omitted():
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    first_observed = cache.observe(key)
    winner = cache.commit(key, first_observed, b"[1]", {"m1": "a"})

    identical_loser = cache.commit(
        key, first_observed, b"[1]", {"m1": "a"}
    )
    assert identical_loser.cas_loser is True
    assert identical_loser.omitted is False
    assert identical_loser.entry is not None
    assert identical_loser.entry.generation == winner.entry.generation

    same_observed = cache.observe(key)
    identical = cache.commit(key, same_observed, b"[1]", {"m1": "a"})
    assert identical.entry is not None
    assert identical.entry.generation == winner.entry.generation

    raced_observed = Observed = cache.observe(key)
    first = cache.commit(key, raced_observed, b"[2]", {"m1": "b"})
    loser = cache.commit(key, Observed, b"[3]", {"m1": "c"})
    assert first.entry is not None
    assert first.entry.generation == winner.entry.generation + 1
    assert loser.cas_loser is True
    assert loser.omitted is True
    assert cache.current(key).canonical_items == b"[2]"


def test_generation_does_not_roll_back_when_older_completion_loses():
    cache = _cache()
    key = ("s1", "q")
    observed = cache.observe(key)
    newer = cache.commit(key, observed, b"new", {"m": "new"})
    older = cache.commit(key, observed, b"old", {"m": "old"})

    assert newer.entry is not None
    assert older.omitted is True
    assert cache.current(key).canonical_items == b"new"


def test_retained_accounting_lru_and_oversized_bypass():
    cache = _cache(max_entries=2, max_bytes=10_000, max_entry_bytes=200)
    key1 = ("s1", "q1")
    key2 = ("s1", "q2")
    key3 = ("s1", "q3")
    for key, mid in ((key1, "a"), (key2, "bb")):
        cache.commit(key, cache.observe(key), b"[]", {mid: "x"})

    expected = 2 * len(b"[]") + (len("a".encode()) + 32 + 64) + (len("bb".encode()) + 32 + 64)
    assert cache.retained_bytes == expected
    cache.observe(key1)  # key2 is now the LRU entry.
    cache.commit(key3, cache.observe(key3), b"[]", {"ccc": "x"})
    assert cache.current(key2) is None
    assert cache.current(key1) is not None
    assert cache.current(key3) is not None

    oversized = _cache(max_entry_bytes=3)
    key = ("s1", "q")
    result = oversized.commit(key, oversized.observe(key), b"1234", {"m": "x"})
    assert result.bypassed is True
    assert oversized.current(key) is None
    assert oversized.retained_bytes == 0


def test_byte_budget_evicts_oldest_and_keeps_exact_retained_bytes():
    cache = _cache(max_entries=8, max_bytes=201, max_entry_bytes=200)
    entries = (
        (("s1", "q1"), "a"),
        (("s1", "q2"), "bb"),
        (("s1", "q3"), "ccc"),
    )

    for key, mid in entries:
        cache.commit(key, cache.observe(key), b"[]", {mid: "x"})

    assert cache.current(("s1", "q1")) is None
    assert cache.current(("s1", "q2")) is not None
    assert cache.current(("s1", "q3")) is not None
    assert cache.retained_bytes == 201


def test_token_classification_distinguishes_invalid_epoch_and_current_generation():
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    installed = cache.commit(key, cache.observe(key), b"[]", {})
    token = cache.issue_token("s1", key[1], installed.entry.generation)

    assert cache.check_token(token, sid="s1", cq_hash=key[1]).kind == "valid"
    assert cache.check_token(token, sid="other", cq_hash=key[1]).kind == "invalid"
    assert cache.check_token("broken", sid="s1", cq_hash=key[1]).kind == "invalid"

    other_epoch = _cache(epoch="epoch-2").issue_token("s1", key[1], 1)
    assert cache.check_token(other_epoch, sid="s1", cq_hash=key[1]).kind == "reset"


def test_cq_hash_mismatch_is_reset_but_sid_mismatch_is_invalid():
    # v6.1 adjudication (2026-08-22): the cq_hash encodes the
    # limit/directory/mode query axis.  A token minted under a different
    # axis is format-valid but semantically stale — a safe reset (full
    # projection + freshly issued nextSince), not a 400.  A sid mismatch
    # keeps its hard-invalid classification.
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    installed = cache.commit(key, cache.observe(key), b"[]", {})
    token = cache.issue_token("s1", key[1], installed.entry.generation)

    check = cache.check_token(token, sid="s1", cq_hash="v1:41::baseline")
    assert check.kind == "reset"
    assert check.generation == installed.entry.generation

    # A sid mismatch wins over the axis change when both differ: the token
    # names another session, which is never a reset candidate.
    assert (
        cache.check_token(token, sid="other", cq_hash="v1:41::baseline").kind
        == "invalid"
    )


def test_boolean_token_version_is_invalid():
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    token = cache.issue_token("s1", key[1], 1)
    payload = orjson.loads(
        base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    )
    payload["v"] = True
    boolean_version = base64.urlsafe_b64encode(
        orjson.dumps(payload)
    ).rstrip(b"=").decode()

    assert cache.check_token(
        boolean_version, sid="s1", cq_hash=key[1]
    ).kind == "invalid"


def test_non_object_and_wrongly_typed_token_payloads_are_invalid():
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    payloads = (
        [],
        "token",
        {"v": 1},
        {"v": 1, "epoch": "epoch-1", "sid": "s1", "cq_hash": key[1], "gen": True},
        {"v": 1, "epoch": "epoch-1", "sid": 1, "cq_hash": key[1], "gen": 1},
        {"v": 1, "epoch": "epoch-1", "sid": "s1", "cq_hash": [], "gen": 1},
    )

    for payload in payloads:
        token = base64.urlsafe_b64encode(orjson.dumps(payload)).rstrip(b"=").decode()
        assert cache.check_token(token, sid="s1", cq_hash=key[1]).kind == "invalid"


def test_missing_or_stale_generation_is_reset():
    cache = _cache()
    key = ("s1", "v1:40::baseline")
    installed = cache.commit(key, cache.observe(key), b"[]", {})
    current = cache.issue_token("s1", key[1], installed.entry.generation)
    stale = cache.issue_token("s1", key[1], installed.entry.generation + 1)
    empty = _cache()
    missing = empty.issue_token("s1", key[1], 1)

    assert cache.check_token(current, sid="s1", cq_hash=key[1]).kind == "valid"
    assert cache.check_token(stale, sid="s1", cq_hash=key[1]).kind == "reset"
    assert empty.check_token(missing, sid="s1", cq_hash=key[1]).kind == "reset"


def test_default_epoch_and_generation_are_process_scoped():
    first = SinceCache(
        enabled=True,
        max_entries=8,
        max_bytes=1024,
        max_entry_bytes=1024,
    )
    second = SinceCache(
        enabled=True,
        max_entries=8,
        max_bytes=1024,
        max_entry_bytes=1024,
    )

    first_entry = first.commit(
        ("s1", "q1"), first.observe(("s1", "q1")), b"[1]", {}
    ).entry
    second_entry = second.commit(
        ("s2", "q2"), second.observe(("s2", "q2")), b"[2]", {}
    ).entry

    assert first.epoch == second.epoch
    assert second_entry.generation > first_entry.generation
