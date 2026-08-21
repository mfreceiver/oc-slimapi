"""Unit tests for oc_slimapi.sse.provider_errors (pure functions, no I/O).

Covers:
* classification matrix — all 7 codes, ≥2 cases each
* order sensitivity (quota before rate; 429 alone → rate_limited;
  unauthorized beats everything)
* structured classification beats text: data.code (exact enum) >
  data.type whitelist > data.status map > text patterns
* structured data passthrough + snake_case→camelCase normalization
  (retry_after → retryAfter, quota_reset_at → quotaResetAt)
* structural values beat text-derived ones (retryAfter); structural
  retryAfter is ALWAYS int seconds (ceil + clamp 1..86400)
* provider/model token safety (charset/length + credential prefixes +
  32+ char high-entropy runs)
* unified retryAfter text regex boundaries (30ms / 1e3s rejected)
* 401/429 text patterns carry digit/letter boundary guards (1401, x429y)
* purity — inputs never mutated; return keys ⊆ fixed whitelist
"""

from __future__ import annotations

import copy

import pytest

from oc_slimapi.sse.provider_errors import (
    PROVIDER_ERROR_CODES,
    classify_provider_error,
)

ALLOWED_KEYS = {"code", "provider", "model", "retryAfter", "quotaResetAt"}


def clf(name=None, message=None, data=None):
    return classify_provider_error(name, message, data)


# ---------------------------------------------------------------------------
# Classification matrix (7 categories × ≥2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,message,want", [
    # 1. provider_unauthorized
    ("UnauthorizedError", None, "provider_unauthorized"),
    (None, "401 invalid api key for this account", "provider_unauthorized"),
    ("APIError", "invalid_api_key: wrong credentials", "provider_unauthorized"),
    (None, "authentication failed for provider", "provider_unauthorized"),
    (None, "鉴权失败", "provider_unauthorized"),
    # 2. provider_model_not_found
    ("ModelError", "model not found: gpt-99", "provider_model_not_found"),
    (None, "no such model 'claude-does-not-exist'", "provider_model_not_found"),
    (None, "unknown model requested", "provider_model_not_found"),
    (None, "model_not_found: foo", "provider_model_not_found"),
    (None, "that model does not exist", "provider_model_not_found"),
    # 3. provider_context_length_exceeded
    (None, "This model's maximum context length is 128000 tokens",
     "provider_context_length_exceeded"),
    (None, "prompt is too long: 200000 tokens", "provider_context_length_exceeded"),
    (None, "input exceeds the context window limit", "provider_context_length_exceeded"),
    (None, "Request too long: maximum context exceeded",
     "provider_context_length_exceeded"),
    (None, "上下文长度超限", "provider_context_length_exceeded"),
    (None, "token limit reached", "provider_context_length_exceeded"),
    # 4. provider_quota_exceeded
    (None, "You exceeded your current quota, check your plan and billing",
     "provider_quota_exceeded"),
    (None, "insufficient quota on your billing account",
     "provider_quota_exceeded"),
    (None, "额度已用尽", "provider_quota_exceeded"),
    (None, "配额不足", "provider_quota_exceeded"),
    (None, "限额已达上限", "provider_quota_exceeded"),
    # 5. provider_rate_limited
    ("rate_limit_error", None, "provider_rate_limited"),
    (None, "429 too many requests", "provider_rate_limited"),
    (None, "rate_limit exceeded for tokens per minute", "provider_rate_limited"),
    (None, "请求过于频繁，请稍后再试", "provider_rate_limited"),
    # 6. provider_model_overloaded
    ("OverloadedError", None, "provider_model_overloaded"),
    (None, "the model is overloaded, try again later", "provider_model_overloaded"),
    (None, "provider is under high load", "provider_model_overloaded"),
    (None, "负载过高", "provider_model_overloaded"),
    # 7. provider_error (fallback)
    ("UnknownError", "boom", "provider_error"),
    (None, None, "provider_error"),
    ("WeirdError", "something odd happened", "provider_error"),
])
def test_classification_matrix(name, message, want):
    got = clf(name, message, None)
    assert got["code"] == want
    assert set(got) <= ALLOWED_KEYS


# ---------------------------------------------------------------------------
# Order sensitivity
# ---------------------------------------------------------------------------

def test_quota_wins_over_rate_when_both_present():
    """'exceeded quota ... retry after 30s' must be quota, NOT rate-limited
    (quota is checked before rate), and still extracts retryAfter=30."""
    got = clf(None, "You exceeded quota, please retry after 30s", None)
    assert got["code"] == "provider_quota_exceeded"
    assert got["retryAfter"] == 30


def test_unauthorized_beats_everything():
    got = clf(None, "401 unauthorized: rate limit on invalid api key", None)
    assert got["code"] == "provider_unauthorized"


def test_429_alone_is_rate_limited():
    assert clf(None, "429", None)["code"] == "provider_rate_limited"


# ---------------------------------------------------------------------------
# 401/429 text substring boundary guards (m6)
# ---------------------------------------------------------------------------

def test_status_digits_need_boundaries():
    # "1401" must NOT trigger unauthorized — falls through to the real
    # signal in the message ("model not found").
    assert clf(None, "model not found, request 1401", None)[
        "code"] == "provider_model_not_found"
    # "429" delimited by space/colon boundaries → rate_limited.
    assert clf(None, "error 429:", None)["code"] == "provider_rate_limited"
    # "x429y" — no boundary on either side → no rate signal at all.
    assert clf(None, "x429y something odd happened", None)[
        "code"] == "provider_error"


def test_4290_not_a_rate_signal():
    assert clf(None, "http request failed with 4290", None)[
        "code"] == "provider_error"


def test_context_before_quota():
    # "context length ... quota" — context category is checked first.
    got = clf(None, "context length exceeded while quota message included", None)
    assert got["code"] == "provider_context_length_exceeded"


def test_name_checked_before_message_categories():
    # name hits unauthorized (priority 1) even though the message alone
    # would classify as overloaded.
    got = clf("UnauthorizedError", "model overloaded", None)
    assert got["code"] == "provider_unauthorized"


# ---------------------------------------------------------------------------
# Structured classification signals (B2): code > type > status > text
# ---------------------------------------------------------------------------

def test_structured_code_direct_enum_member_used():
    got = clf(None, "some opaque upstream failure", {
        "code": "provider_rate_limited",
    })
    assert got["code"] == "provider_rate_limited"


def test_structured_code_non_member_ignored_falls_to_text():
    # "not_a_member" is not in the enum → ignored; text still classifies.
    assert clf(None, "rate limit exceeded", {"code": "not_a_member"})[
        "code"] == "provider_rate_limited"
    # ...and with no text signal the fallback applies.
    assert clf(None, "boom", {"code": "provider_internal"})[
        "code"] == "provider_error"


@pytest.mark.parametrize("type_value,want", [
    ("rate_limit_error", "provider_rate_limited"),
    ("Rate_Limit_Error", "provider_rate_limited"),  # lowercased before map
    ("overloaded_error", "provider_model_overloaded"),
    ("authentication_error", "provider_unauthorized"),
    ("insufficient_quota", "provider_quota_exceeded"),
])
def test_structured_type_whitelist_mapping(type_value, want):
    assert clf(None, "boom", {"type": type_value})["code"] == want


def test_structured_type_unmapped_ignored():
    assert clf(None, "rate limit exceeded", {"type": "brand_new_error"})[
        "code"] == "provider_rate_limited"
    assert clf(None, None, {"type": "brand_new_error"})[
        "code"] == "provider_error"


@pytest.mark.parametrize("status,want", [
    (401, "provider_unauthorized"),
    (429, "provider_rate_limited"),
    (402, "provider_quota_exceeded"),
])
def test_structured_status_mapping(status, want):
    assert clf(None, "boom", {"status": status})["code"] == want


@pytest.mark.parametrize("status", [500, 404, "429", 429.0, True, None])
def test_structured_status_unmapped_or_wrong_type_ignored(status):
    assert clf(None, "boom", {"status": status})["code"] == "provider_error"


def test_structured_priority_code_beats_type_beats_status():
    assert clf(None, "boom", {
        "code": "provider_model_not_found",
        "type": "rate_limit_error",
        "status": 401,
    })["code"] == "provider_model_not_found"
    assert clf(None, "boom", {
        "type": "overloaded_error",
        "status": 401,
    })["code"] == "provider_model_overloaded"


def test_structured_beats_text_on_conflict():
    # data.status=429 + message says "quota" → structured wins → rate.
    got = clf(None, "quota exceeded on your billing plan", {"status": 429})
    assert got["code"] == "provider_rate_limited"


# ---------------------------------------------------------------------------
# retryAfter text extraction + clamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message,want", [
    ("rate limit hit, retry after 30", 30),
    ("rate limit hit, retry after 30s", 30),
    # NOTE: "retry in" / "try again in" are NOT category-5 patterns — those
    # messages classify as the fallback — but retryAfter extraction is
    # independent of classification and still fires.
    ("retry in 27s", 27),
    ("try again in 12s", 12),
    ("retry in 1.5s", 2),  # ceil, never truncation
    ("rate limit, retry after 0", 1),  # clamp min
    ("rate limit, retry after 999999999", 86400),  # clamp max
    ("rate limit, try again in 0s", 1),
])
def test_retry_after_text_extraction(message, want):
    got = clf(None, message, None)
    assert got["retryAfter"] == want
    assert isinstance(got["retryAfter"], int)


# ---------------------------------------------------------------------------
# Unified retryAfter text regex boundaries (m5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message,want", [
    ("Retry After 30", 30),            # case-insensitive on RAW text
    ("try again in 5 seconds", 5),
    ("retry after 30 sec", 30),
    ("retry after 2.0s", 2),
    ("retry in 0.4s", 1),              # ceil then clamp min
])
def test_retry_after_unified_pattern_accepts(message, want):
    assert clf(None, message, None)["retryAfter"] == want


@pytest.mark.parametrize("message", [
    "retry after 30ms",      # unit must be s/sec/seconds — 'm' fails lookahead
    "retry after 1e3s",      # 'e' right after digits fails the lookahead
    "retry after 30.5.5s",   # stray '.' after the decimal fails the lookahead
    "retry 30",              # no in/after keyword
    "please retry later",    # no number at all
])
def test_retry_after_unified_pattern_rejects(message):
    assert "retryAfter" not in clf(None, message, None)


# ---------------------------------------------------------------------------
# N2: whitespace + letter unit words rejected by the second lookahead
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "retry after 30 ms",
    "retry after 30 minutes",
    "retry after 30  minutes",   # double space — \s* covers all forms
    "try again in 5 lightyears",
])
def test_retry_after_space_then_word_unit_rejected(message):
    assert "retryAfter" not in clf(None, message, None)


@pytest.mark.parametrize("message,want", [
    ("retry after 30, please wait", 30),   # comma delimiter — accepted
    ("Retry After 30", 30),
    ("try again in 5 Seconds", 5),         # unit, capitalized
])
def test_retry_after_delimited_or_unit_still_accepted(message, want):
    assert clf(None, message, None)["retryAfter"] == want


# ---------------------------------------------------------------------------
# P2: regex v3 — unit-branch / no-unit-branch split (no over-rejection)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message,want", [
    ("retry after 30 seconds.", 30),              # trailing period after unit
    ("retry after 30 seconds before retrying", 30),  # unit then more words
    ("retry after 30\nPlease check quota", 30),   # newline after digits —
                                                  # no \s* to poison next line
    ("retry after 30 seconds, then retry", 30),
    ("retry after 30 secs", 30),
    ("retry after 30 s", 30),
])
def test_retry_after_v3_unit_branch_tolerant(message, want):
    assert clf(None, message, None)["retryAfter"] == want


@pytest.mark.parametrize("message", [
    "retry after 30 ms",        # same-line space + letter → no-unit branch rejects
    "retry after 30 minutes",
    "retry after 30  minutes",  # multiple spaces still rejected
    "retry after 1e3s",         # no-unit branch first lookahead
    "retry after 30.5.5s",
])
def test_retry_after_v3_rejections_hold(message):
    assert "retryAfter" not in clf(None, message, None)


def test_retry_after_absent_when_no_pattern():
    got = clf(None, "the model is overloaded", None)
    assert "retryAfter" not in got


def test_raw_text_survives_past_512_char_boundary():
    """Classification uses the RAW message: a retry-after clause beyond the
    512-char sanitize truncation point still yields retryAfter (locks in
    the classify-on-raw-msg decision in global_hub)."""
    message = "rate limit reached " + "x" * 600 + " retry after 45s"
    assert clf(None, message, None)["retryAfter"] == 45


# ---------------------------------------------------------------------------
# Structured data passthrough + normalization
# ---------------------------------------------------------------------------

def test_structured_snake_case_normalized_to_camel_case():
    got = clf(None, "429", {"retry_after": 45, "quota_reset_at": 1755302400000})
    assert got["retryAfter"] == 45
    assert got["quotaResetAt"] == 1755302400000
    assert "retry_after" not in got
    assert "quota_reset_at" not in got


def test_structured_camel_case_keys_accepted_directly():
    got = clf(
        None, None,
        {"retryAfter": 60, "quotaResetAt": "2026-08-22T00:00:00+00:00"},
    )
    assert got["retryAfter"] == 60
    assert got["quotaResetAt"] == "2026-08-22T00:00:00+00:00"


def test_structured_retry_after_beats_text():
    got = clf(None, "retry after 5 seconds", {"retryAfter": 60})
    assert got["retryAfter"] == 60


def test_structured_provider_model_passthrough():
    got = clf(None, "429", {"provider": "openai", "model": "gpt-4o-mini"})
    assert got["provider"] == "openai"
    assert got["model"] == "gpt-4o-mini"
    assert got["code"] == "provider_rate_limited"


def test_quota_reset_at_iso_string_passthrough():
    got = clf(None, "quota exceeded", {"quota_reset_at": "2026-08-22T00:00:00Z"})
    assert got["quotaResetAt"] == "2026-08-22T00:00:00Z"


def test_quota_reset_at_invalid_string_dropped():
    got = clf(None, "quota exceeded", {"quota_reset_at": "not-a-date"})
    assert "quotaResetAt" not in got


def test_quota_reset_at_non_number_non_string_dropped():
    got = clf(None, "quota exceeded", {"quota_reset_at": ["2026"]})
    assert "quotaResetAt" not in got


# ---------------------------------------------------------------------------
# quotaResetAt contract lock (M3 — implementation unchanged, tests frozen)
# ---------------------------------------------------------------------------

def test_quota_reset_at_fractional_float_preserved_verbatim():
    got = clf(None, "quota", {"quotaResetAt": 1755800000.5})
    assert got["quotaResetAt"] == 1755800000.5
    assert type(got["quotaResetAt"]) is float


def test_quota_reset_at_overlong_iso_string_dropped():
    # Parses as ISO-8601 (verified) but exceeds the 64-char cap → dropped
    # by the length rule alone.
    overlong = "2026-08-22T00:00:00." + "1" * 50 + "+00:00"
    assert len(overlong) > 64
    got = clf(None, "quota", {"quotaResetAt": overlong})
    assert "quotaResetAt" not in got


def test_quota_reset_at_non_iso_string_dropped():
    assert "quotaResetAt" not in clf(
        None, "quota", {"quotaResetAt": "next tuesday maybe"})


@pytest.mark.parametrize("bad", [{"ts": 1}, [1755800000], None])
def test_quota_reset_at_dict_list_none_dropped(bad):
    assert "quotaResetAt" not in clf(None, "quota", {"quotaResetAt": bad})


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_quota_reset_at_nonfinite_float_dropped(bad):
    assert "quotaResetAt" not in clf(None, "quota", {"quotaResetAt": bad})


@pytest.mark.parametrize("huge", [10**20, 10**400, 2**63, -(2**63) - 1])
def test_quota_reset_at_out_of_int64_range_dropped(huge):
    """P1: ints beyond the orjson-serializable 64-bit range are dropped —
    carrying them through would raise TypeError mid-publish (sse_frame's
    orjson.dumps) and kill the G1-B frame / digest flush."""
    assert "quotaResetAt" not in clf(None, "quota", {"quotaResetAt": huge})


@pytest.mark.parametrize("ok,expected_type", [
    (2**62, int),       # largest sane epoch-ish value, well inside range
    (-2**63, int),      # inclusive lower bound
    (2**63 - 1, int),   # inclusive upper bound
    (1755800000, int),
])
def test_quota_reset_at_int64_range_passthrough(ok, expected_type):
    got = clf(None, "quota", {"quotaResetAt": ok})
    assert got["quotaResetAt"] == ok
    assert type(got["quotaResetAt"]) is expected_type


def test_quota_reset_at_integral_float_stays_float():
    """N3: integral floats are NOT coerced to int — 1755800000.0 keeps
    its float type (and its .0 in JSON serialization)."""
    got = clf(None, "quota", {"quotaResetAt": 1755800000.0})
    assert got["quotaResetAt"] == 1755800000.0
    assert type(got["quotaResetAt"]) is float


def test_quota_reset_at_int_stays_int():
    got = clf(None, "quota", {"quotaResetAt": 1755800000})
    assert got["quotaResetAt"] == 1755800000
    assert type(got["quotaResetAt"]) is int


def test_structured_retry_after_zero_clamped_to_min():
    # Frozen B1 semantics: every finite number is accepted, ceil'd, then
    # clamped into 1..86400 — 0 no longer drops (no text fallback either).
    got = clf(None, "429", {"retryAfter": 0})
    assert got["retryAfter"] == 1


def test_structured_retry_after_negative_clamped_to_min():
    assert clf(None, "429", {"retry_after": -5})["retryAfter"] == 1


def test_structured_retry_after_huge_clamped():
    got = clf(None, "429", {"retryAfter": 10**9})
    assert got["retryAfter"] == 86400


def test_structured_retry_after_bool_dropped():
    got = clf(None, "429", {"retryAfter": True})
    assert "retryAfter" not in got


@pytest.mark.parametrize("structured,want", [
    (1.5, 2),   # ceil, never passthrough
    (2.0, 2),   # integral float → int
    (0.4, 1),   # ceil then clamp min
    (30, 30),
    (30.0, 30),
])
def test_structured_retry_after_always_int_seconds(structured, want):
    """B1: structured retryAfter is ALWAYS int seconds (ceil + clamp)."""
    got = clf(None, "429", {"retryAfter": structured})
    assert got["retryAfter"] == want
    assert type(got["retryAfter"]) is int


def test_structured_retry_after_str_dropped_text_wins():
    # "30" is a string — never parsed structurally (the regex path owns
    # text); the message's own clause extracts 7.
    got = clf(None, "rate limit, retry after 7s", {"retryAfter": "30"})
    assert got["retryAfter"] == 7


# ---------------------------------------------------------------------------
# N1: numeric defense — no inf/OverflowError can ever escape to publish
# ---------------------------------------------------------------------------

def test_retry_after_astronomical_int_digits_clamped_without_conversion():
    # 400-digit run: >9 int digits → straight to 86400, float() never called.
    got = clf(None, "rate limit, retry after " + "9" * 400 + "s", None)
    assert got["retryAfter"] == 86400


def test_retry_after_long_decimal_clamped_without_conversion():
    # int part alone is 30 digits (>9) → straight to 86400.
    assert clf(None, "retry after " + "9" * 30 + ".5", None)[
        "retryAfter"] == 86400


@pytest.mark.parametrize("digits", [
    "9" * 10,                    # 10 int digits — just over the 9-digit line
    "1234567890123456",          # 16-char decimal-less int > 15
    "123456789.5678901",         # int part ≤9 but decimal total 17 > 15
])
def test_retry_after_just_over_length_lines_clamp(digits):
    assert clf(None, "retry after " + digits, None)["retryAfter"] == 86400


@pytest.mark.parametrize("digits,want", [
    ("999999999", 86400),        # 9 int digits — still converted, then clamped
    ("123456789.56789", 86400),  # 15 chars total — converted, then clamped
    ("30", 30),
    ("1.5", 2),
])
def test_retry_after_within_length_lines_convert_normally(digits, want):
    assert clf(None, "retry after " + digits, None)["retryAfter"] == want


def test_structured_retry_after_huge_int_clamped_without_float():
    # Pure-int comparison path — 10**400 can't even be represented as float.
    assert clf(None, "429", {"retryAfter": 10**400})["retryAfter"] == 86400


@pytest.mark.parametrize("bad", [float("inf"), float("nan"),
                                 float("-inf"), -float("inf")])
def test_structured_retry_after_nonfinite_float_dropped(bad):
    assert "retryAfter" not in clf(None, "429", {"retryAfter": bad})


def test_classify_never_raises_on_numeric_extremes():
    """Sweep: no combination of numeric extremes may raise (an exception
    bubbling out of hub.publish would kill subscriber connections)."""
    extremes = [
        (None, "retry after " + "9" * 400 + "s", {"retryAfter": 10**400}),
        (None, "429", {"retryAfter": float("inf")}),
        (None, "429", {"retryAfter": float("nan")}),
        (None, "quota", {"quotaResetAt": 10**400}),
        (None, "quota", {"quotaResetAt": float("inf")}),
        (None, "quota", {"quotaResetAt": float("nan")}),
        (None, "retry after " + "9" * 30 + ".5", {"quotaResetAt": 10**20}),
    ]
    for name, message, data in extremes:
        clf(name, message, data)  # implicit no-raise assertion


# ---------------------------------------------------------------------------
# provider/model token safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "x" * 65,                       # >64 chars
    "bad provider",                 # space
    "evil$provider",                # $
    'evil"provider',                # quote
    "evil\nprovider",               # newline
    "",                             # empty
    123,                            # non-str
    {"a": 1},                       # non-str
    None,
])
def test_invalid_provider_dropped(bad):
    got = clf(None, None, {"provider": bad})
    assert "provider" not in got


def test_invalid_model_dropped():
    got = clf(None, None, {"model": "model with spaces"})
    assert "model" not in got


def test_provider_model_max_length_boundary_ok():
    # 64 chars exactly, separator-rich so no 32+ alnum run (the entropy
    # defense would drop "a"*64).
    ok = "-".join(["a" * 12] * 5)
    assert len(ok) == 64
    got = clf(None, None, {"provider": ok, "model": ok})
    assert got["provider"] == ok
    assert got["model"] == ok


def test_provider_model_charset_allows_path_like_ids():
    got = clf(None, None, {"provider": "azure/east-us", "model": "gpt-4o:2024-08"})
    assert got["provider"] == "azure/east-us"
    assert got["model"] == "gpt-4o:2024-08"


# ---------------------------------------------------------------------------
# Credential-shaped provider/model defenses (M4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret_like", [
    # Pure prefix hits: charset-clean, all runs <32 — ONLY the prefix
    # blacklist drops them.
    "sk-" + "a1b2c3d4e5f6g7h8",                  # OpenAI-style sk-
    "sk_" + "a1b2c3d4e5f6g7h8",                  # underscore variant
    "AKIA" + "ABCDEFGHIJKLMNOP",                 # AWS access key id
    "ghp_" + "a1b2c3d4e5f6g7h8i9",               # GitHub PAT
    "xoxb-" + "1234567890-abcdef",               # Slack bot token
    "eyJhbGciO.k9",                              # JWT header prefix (short)
    # Realistic long forms: prefix AND entropy both fire.
    "sk-Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z",       # sk- + 35-char random
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x.y",  # full JWT shape
])
def test_credential_prefix_blacklist_drops(secret_like):
    assert "provider" not in clf(None, None, {"provider": secret_like})
    assert "model" not in clf(None, None, {"model": secret_like})


@pytest.mark.parametrize("high_entropy", [
    "a" * 32,            # 32 contiguous alnum — hex32/Azure key shape
    "deadbeef" * 4,      # 32 contiguous hex
    "ok-name-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",  # long trailing run
])
def test_high_entropy_run_drops(high_entropy):
    # No blacklisted prefix — only the ≥32 contiguous-alnum rule fires.
    assert "provider" not in clf(None, None, {"provider": high_entropy})


@pytest.mark.parametrize("legit", [
    "gpt-4o-2024-08-06",
    "azure/east-us",
    "anthropic/claude-3-5-sonnet",
])
def test_real_world_ids_survive_credential_defenses(legit):
    got = clf(None, None, {"provider": legit, "model": legit})
    assert got["provider"] == legit
    assert got["model"] == legit


# ---------------------------------------------------------------------------
# Whitelist / no upstream leakage
# ---------------------------------------------------------------------------

def test_no_keys_outside_whitelist_ever_returned():
    data = {
        "api_key": "sk-supersecret", "stack": "at /src/app.ts:1:1",
        "provider": "openai", "model": "gpt-4o", "retryAfter": 30,
        "quotaResetAt": 1755302400000, "internal_debug": {"heap": [...]},
    }
    got = clf(None, None, data)
    assert set(got) == {"provider", "model", "retryAfter", "quotaResetAt", "code"}
    assert "sk-supersecret" not in repr(got)
    assert "app.ts" not in repr(got)


def test_code_always_enum_member():
    for name, message in [(None, None), ("X", "y"), (42, 42), ({}, [1])]:
        got = clf(name, message, None)
        assert got["code"] in PROVIDER_ERROR_CODES


# ---------------------------------------------------------------------------
# Purity / defensive input handling
# ---------------------------------------------------------------------------

def test_inputs_never_mutated():
    name = "RateLimitError"
    message = "429 retry after 30s"
    data = {"retry_after": 30, "provider": "openai"}
    name_copy, message_copy, data_copy = name, message, copy.deepcopy(data)
    clf(name, message, data)
    assert name == name_copy
    assert message == message_copy
    assert data == data_copy


def test_non_str_name_and_message_do_not_crash():
    got = clf({"weird": True}, 12345, None)
    assert got["code"] == "provider_error"
    assert set(got) == {"code"}


def test_non_dict_data_tolerated():
    assert clf(None, "429", "not-a-dict")["code"] == "provider_rate_limited"
    assert clf(None, "429", [1, 2])["code"] == "provider_rate_limited"


def test_return_key_order_deterministic():
    got = clf(
        None, "429",
        {"provider": "openai", "model": "gpt-4o", "retryAfter": 5,
         "quotaResetAt": 1},
    )
    assert list(got) == ["code", "provider", "model", "retryAfter", "quotaResetAt"]
