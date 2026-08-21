"""Structured provider-error classification for upstream ``session.error``.

Pure functions, zero I/O. :func:`classify_provider_error` maps the upstream
error (name + raw message + optional structured ``error.data`` dict) onto a
closed code enum (:data:`PROVIDER_ERROR_CODES`) plus a strict whitelist of
normalized, type-checked passthrough fields (``provider`` / ``model`` /
``retryAfter`` / ``quotaResetAt``). Nothing outside the whitelist is ever
echoed — raw message text, API keys, upstream stack frames etc. cannot leak
through the returned dict.

Classification signal priority (frozen):

1. structured ``data.code`` — exact :data:`PROVIDER_ERROR_CODES` membership
2. structured ``data.type`` — small whitelist map (lowercased compare)
3. structured ``data.status`` — {401, 402, 429} map (int, bool excluded)
4. text patterns over lowercased name + message (ordered category table)
5. fallback ``provider_error``

Classification input is the RAW (pre-sanitize) message text by design:
``_sanitize_error_message`` truncates to 512 chars and rewrites segments
(``<path>`` / ``<redacted>``), which could cut a trailing ``retry after Ns``
clause; since this module only emits enum codes + validated fields, using the
raw text leaks nothing.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

# Closed code enum (frozen). ``provider_error`` is the catch-all fallback —
# ``classify_provider_error`` ALWAYS returns a member of this set.
PROVIDER_ERROR_CODES = frozenset({
    "provider_rate_limited",
    "provider_quota_exceeded",
    "provider_model_overloaded",
    "provider_context_length_exceeded",
    "provider_unauthorized",
    "provider_model_not_found",
    "provider_error",
})

# Ordered TEXT classification table: first category whose ANY pattern
# regex-searches into the lowercased name OR message wins. ORDER IS
# LOAD-BEARING: quota (4) ahead of rate (5) so "exceeded quota ... retry
# after 30s" classifies as provider_quota_exceeded, not
# provider_rate_limited; "429" alone lands in rate_limited; unauthorized
# beats everything (a 401 is never retryable, so it must not be masked by
# an incidental "retry after"). Bare "401"/"429" carry digit/letter
# boundary lookarounds so substrings like "1401" / "x429y" never match.
_CATEGORY_ORDER: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("provider_unauthorized", (
        re.compile(r"unauthorized"),
        re.compile(r"(?<![0-9a-z])401(?![0-9a-z])"),
        re.compile(r"invalid api key"),
        re.compile(r"invalid_api_key"),
        re.compile(r"authentication"),
        re.compile(r"鉴权"),
    )),
    ("provider_model_not_found", (
        re.compile(r"model not found"),
        re.compile(r"model_not_found"),
        re.compile(r"no such model"),
        re.compile(r"unknown model"),
        re.compile(r"model does not exist"),
    )),
    ("provider_context_length_exceeded", (
        re.compile(r"context length"),
        re.compile(r"context_length"),
        re.compile(r"maximum context"),
        re.compile(r"context window"),
        re.compile(r"prompt is too long"),
        re.compile(r"too long"),
        re.compile(r"上下文长度"),
        re.compile(r"token limit"),
    )),
    ("provider_quota_exceeded", (
        re.compile(r"quota"),
        re.compile(r"insufficient quota"),
        re.compile(r"billing"),
        re.compile(r"额度"),
        re.compile(r"配额"),
        re.compile(r"限额"),
    )),
    ("provider_rate_limited", (
        re.compile(r"rate limit"),
        re.compile(r"rate_limit"),
        re.compile(r"(?<![0-9a-z])429(?![0-9a-z])"),
        re.compile(r"too many requests"),
        re.compile(r"请求过于频繁"),
        re.compile(r"retry after"),
    )),
    ("provider_model_overloaded", (
        re.compile(r"overloaded"),
        re.compile(r"overload"),
        re.compile(r"high load"),
        re.compile(r"负载"),
    )),
)

PROVIDER_ERROR_FALLBACK = "provider_error"

# Structured classification whitelist maps (frozen). ``data.type`` is
# lowercased before lookup; anything unmapped is ignored. ``data.status``
# must be a real int (bool excluded); unmapped statuses are ignored.
_TYPE_CODE_MAP: dict[str, str] = {
    "rate_limit_error": "provider_rate_limited",
    "overloaded_error": "provider_model_overloaded",
    "authentication_error": "provider_unauthorized",
    "insufficient_quota": "provider_quota_exceeded",
}

_STATUS_CODE_MAP: dict[int, str] = {
    401: "provider_unauthorized",
    429: "provider_rate_limited",
    402: "provider_quota_exceeded",
}

# Unified retryAfter text pattern (frozen v3), case-insensitive, searched
# against the RAW message text. Two-branch alternation after the digits:
#   unit branch:    [ \t]* (same-line whitespace only) + full unit word
#                   (seconds?/secs?/s — longest first) with a single
#                   (?![A-Za-z0-9]) guard — a following period, comma,
#                   space + arbitrary words are all fine
#                   ("30 seconds.", "30 seconds before retrying").
#   no-unit branch: (?![A-Za-z0-9.]) rejects "30ms"/"1e3s"/"30.5.5s";
#                   (?![ \t]*[A-Za-z]) rejects same-line space + letter
#                   unit words ("30 ms", "30 minutes") — NO \s*, so a
#                   newline after the digits never poisons the next line.
_RETRY_AFTER_TEXT_RE = re.compile(
    r"(?i)(?:retry|try again)\s+(?:in|after)\s+(\d+(?:\.\d+)?)"
    r"(?:[ \t]*(?:seconds?|secs?|s)(?![A-Za-z0-9])"
    r"|(?![A-Za-z0-9.])(?![ \t]*[A-Za-z]))"
)

_RETRY_AFTER_MIN_S = 1
_RETRY_AFTER_MAX_S = 86400  # 24h — anything larger is upstream nonsense

# provider/model token safety: str, 1..64 chars, charset [A-Za-z0-9._\-/:].
# Rejects spaces, "$", quotes, newlines — anything that could smuggle
# upstream free text (or a secret) into the curated frame.
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-/:]{1,64}")

# Credential-shaped token defenses (applied AFTER charset/length checks,
# lowercase prefix compare): known key/token prefixes are dropped even
# though they satisfy the charset. "eyj" is the lowercased form of the
# "eyJ" JWT-header prefix.
_CREDENTIAL_PREFIXES: tuple[str, ...] = (
    "sk-", "sk_", "pk-", "rk-",
    "ghp_", "gho_", "ghu_", "ghs_", "github_pat_",
    "xoxb-", "xoxp-", "xoxa-",
    "akia", "agpa", "aida", "aiza",
    "eyj",
)

# High-entropy heuristic: any contiguous alphanumeric run of 32+ chars
# drops the token (hex32 / Azure / random keys); real provider/model ids
# are separator-rich ("." / "-" / "/" / ":") with short segments.
_HIGH_ENTROPY_RUN_RE = re.compile(r"[A-Za-z0-9]{32,}")

# quotaResetAt ISO strings: bounded length, must parse as ISO-8601.
_QUOTA_RESET_MAX_LEN = 64

# orjson (the SSE wire serializer, see hub_types.sse_frame) only carries
# 64-bit integers — an int outside [-2**63, 2**63-1] would raise
# ``TypeError: Integer exceeds 64-bit range`` mid-publish (killing the
# G1-B direct frame / digest flush), so out-of-range ints are dropped.
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

# The complete set of keys classify_provider_error may ever return. Wire
# whitelist — anything else from upstream data is dropped.
_RETURNABLE_KEYS = frozenset({
    "code", "provider", "model", "retryAfter", "quotaResetAt",
})


def _lower_str(value: object) -> str:
    """Coerce to lowercased str; non-str (None/int/dict/...) → ``""``."""
    return value.lower() if isinstance(value, str) else ""


def _safe_token(value: object) -> str | None:
    """Validate a provider/model token: charset/length, then credential
    defenses (known-secret prefixes, 32+ char alphanumeric runs)."""
    if not isinstance(value, str):
        return None
    if not _SAFE_TOKEN_RE.fullmatch(value):
        return None
    if value.lower().startswith(_CREDENTIAL_PREFIXES):
        return None
    if _HIGH_ENTROPY_RUN_RE.search(value):
        return None
    return value


def _finite_number(value: object) -> int | float | None:
    """Numeric passthrough for quotaResetAt.

    int → returned VERBATIM at any magnitude (never routed through float:
    ints beyond 2**53 lose precision and ``math.isfinite`` itself would
    raise OverflowError on the float conversion). float → finite only
    (nan/inf dropped). Integral floats stay floats — no int coercion.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _clamp_retry_after(value: float) -> int:
    """ceil a FINITE float to whole seconds, then clamp into 1..86400."""
    return max(_RETRY_AFTER_MIN_S, min(_RETRY_AFTER_MAX_S, math.ceil(value)))


def _code_from_data(data: dict) -> str | None:
    """Structured classification: ``code`` > ``type`` > ``status``.

    Returns ``None`` when no structured signal hits (→ text classification).
    """
    # 1. data.code: only exact enum members are honored (no arbitrary map).
    code = data.get("code")
    if isinstance(code, str) and code in PROVIDER_ERROR_CODES:
        return code
    # 2. data.type: whitelist map over the lowercased value.
    mapped = _TYPE_CODE_MAP.get(_lower_str(data.get("type")))
    if mapped is not None:
        return mapped
    # 3. data.status: int only (bool excluded), known statuses only.
    status = data.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return _STATUS_CODE_MAP.get(status)


def _code_from_text(name_l: str, message_l: str) -> str:
    for candidate, patterns in _CATEGORY_ORDER:
        if any(p.search(name_l) or p.search(message_l) for p in patterns):
            return candidate
    return PROVIDER_ERROR_FALLBACK


def _retry_after_from_text(message: str) -> int | None:
    """Extract retry seconds from RAW text; ceil'd int clamped 1..86400.

    Astronomical digit runs short-circuit straight to the 86400 cap —
    the captured string is NEVER converted through ``float`` when its
    integer part exceeds 9 digits or its decimal form exceeds 15 chars
    total, so no inf/OverflowError can escape into the publish path.
    """
    match = _RETRY_AFTER_TEXT_RE.search(message)
    if not match:
        return None
    digits = match.group(1)
    int_part = digits.split(".", 1)[0]
    if len(int_part) > 9 or ("." in digits and len(digits) > 15):
        return _RETRY_AFTER_MAX_S
    return _clamp_retry_after(float(digits))


def _retry_after_from_data(value: object) -> int | None:
    """Structural retryAfter: int/float (bool/str excluded) → int seconds.

    int: pure-int comparison first — never converted through float, so
    arbitrarily large ints cannot overflow (>86400 → 86400; <1 → 1).
    float: must be finite (nan/inf → field dropped); finite floats are
    always safe to ceil + clamp into 1..86400.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        if value > _RETRY_AFTER_MAX_S:
            return _RETRY_AFTER_MAX_S
        if value < _RETRY_AFTER_MIN_S:
            return _RETRY_AFTER_MIN_S
        return value
    if not math.isfinite(value):
        return None
    return _clamp_retry_after(value)


def _quota_reset_from_data(value: object) -> int | float | str | None:
    """Structural quotaResetAt: epoch-ish number or ISO-8601 string.

    Numbers pass through with their numeric type preserved, EXCEPT ints
    outside the orjson-serializable 64-bit range [-2**63, 2**63-1] —
    those are dropped rather than risking ``TypeError`` during SSE frame
    serialization. Floats need only be finite (orjson carries any finite
    double). Strings must be ≤64 chars and ``datetime.fromisoformat``
    -parseable; everything else is dropped.
    """
    number = _finite_number(value)
    if number is not None:
        if isinstance(number, int) and not (_INT64_MIN <= number <= _INT64_MAX):
            return None
        return number
    if isinstance(value, str) and 0 < len(value) <= _QUOTA_RESET_MAX_LEN:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value
    return None


def classify_provider_error(
    name: str | None,
    message: str | None,
    data: dict | None,
) -> dict:
    """Classify an upstream session.error into structured additive fields.

    Args:
        name: upstream ``error.name`` (non-str tolerated → ignored).
        message: RAW (pre-sanitize) ``error.data.message`` text — pattern
            words and ``retry after N`` clauses survive here even when the
            sanitized 512-char wire message would have truncated them.
        data: upstream ``error.data`` dict for structured classification
            (``code`` / ``type`` / ``status``) and passthrough (non-dict
            tolerated → ignored).

    Returns:
        Always ``{"code": <PROVIDER_ERROR_CODES member>}``, plus optional
        whitelisted keys ``provider`` / ``model`` / ``retryAfter`` /
        ``quotaResetAt`` (insertion order fixed). Structured signals beat
        text-derived ones for both ``code`` and ``retryAfter``; no key
        outside ``{"code","provider","model","retryAfter","quotaResetAt"}``
        is ever returned.
    """
    data = data if isinstance(data, dict) else {}
    name_l = _lower_str(name)
    message_l = _lower_str(message)
    raw_message = message if isinstance(message, str) else ""

    # Code: structured (code > type > status) beats text patterns.
    code = _code_from_data(data)
    if code is None:
        code = _code_from_text(name_l, message_l)

    extra: dict = {"code": code}

    for out_key, in_key in (("provider", "provider"), ("model", "model")):
        token = _safe_token(data.get(in_key))
        if token is not None:
            extra[out_key] = token

    # retryAfter: structural (data) wins over text extraction. Always an
    # int (ceil'd seconds, clamped 1..86400).
    retry = _retry_after_from_data(data.get("retryAfter"))
    if retry is None:
        retry = _retry_after_from_data(data.get("retry_after"))
    if retry is None:
        retry = _retry_after_from_text(raw_message)
    if retry is not None:
        extra["retryAfter"] = retry

    quota_reset = _quota_reset_from_data(data.get("quotaResetAt"))
    if quota_reset is None:
        quota_reset = _quota_reset_from_data(data.get("quota_reset_at"))
    if quota_reset is not None:
        extra["quotaResetAt"] = quota_reset

    return extra
