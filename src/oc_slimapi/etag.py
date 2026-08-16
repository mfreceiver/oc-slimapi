"""Traffic plan Batch 2 / B1 — ETag / 304 conditional request support.

Authority: ``docs/ocmar/plans/2026-08-16-traffic-optimization-plan.md`` §4
(v1.3 unified spec text). Additive wire: the pipeline ALWAYS runs (ETag
never short-circuits the fetch/projection — Batch 1 keeps amortising the
upstream); a validator hit only saves the DOWNSTREAM transport body, and a
gzip hit additionally skips the canonical-hash compression.

Validator scheme (§4, the ONLY normative text):

* identity → STRONG   ``"<sha256hex(REP_VERSION \\0 b"identity" \\0 identity)>"``
* gzip     → WEAK     ``W/"<sha256hex(REP_VERSION \\0 b"gzip" \\0 identity)>"``

The canonical hash input is ALWAYS the identity body bytes plus the coding
id — never the compressed bytes. Validators are per-coding opaque tags, so
cross-coding reuse fails closed (a conservative 200).
"""
from __future__ import annotations

import hashlib
from typing import Protocol

from starlette.responses import Response

from .gzip_util import MIN_GZIP_BYTES, accepts_gzip

# ETag scheme version — bump when the validator derivation itself changes
# (distinct from the skeleton projection version below).
_ETAG_SCHEME_VERSION = b"etag-v1"

#: Skeleton projection version. Bump when the projection semantics change
#: (any change to what a skeleton/final body means). Together with the
#: config fingerprint below this forms REP_VERSION — flipping it rotates
#: EVERY validator, so a stale client can never receive a false 304.
#: Changing the gzip implementation (zlib level/version), MIN_GZIP_BYTES or
#: the compress-if-beneficial gate also changes which coding a response
#: actually carries — treat that as a representation change and bump
#: (rev-5 Batch 2 condition C1).
SKELETON_REPRESENTATION_VERSION = b"skeleton-v1"


class _ConfigLike(Protocol):
    """The subset of Settings that influences response representations."""

    skeleton_inline_output_max_bytes: int
    skeleton_inline_output_max_message_bytes: int


def representation_version(
    config: _ConfigLike, *, wire_view: int = 2,
) -> bytes:
    """REP_VERSION = projection version + config fingerprint.

    The config fingerprint covers everything that changes the bytes of a
    projected response (today: the skeleton inline limits). The Batch 4
    (B3) ``message_fingerprint_enabled`` switch is read via ``getattr``
    with a default of ``True`` so that flipping it later rotates every
    ETag without touching this module (it will start contributing a new
    component to the fingerprint, invalidating all prior validators).

    v3-contract §6.1 (Batch B): the fingerprint also carries a **wire-view
    marker** (``wire=v2`` / ``wire=v3``) — v2 and v3 validators are
    domain-isolated (the v3 envelope body differs, so a cross-view
    ``If-None-Match`` must never 304). Adding the marker rotates every
    existing v2 ETag once (one extra 304-miss round, by design — the same
    one-time cost as any representation change).
    """
    fingerprint_parts = [
        _ETAG_SCHEME_VERSION,
        SKELETON_REPRESENTATION_VERSION,
        str(config.skeleton_inline_output_max_bytes).encode(),
        str(config.skeleton_inline_output_max_message_bytes).encode(),
    ]
    # Batch 4 (B3): the fingerprint switch is live on Settings
    # (``message_fingerprint_enabled``, default true — config.py). Its state
    # is part of the representation: toggling it rotates every ETag (one
    # full 304-miss round, by design). getattr keeps pure-function callers
    # with ad-hoc config stand-ins working (tests).
    fingerprint_parts.append(
        b"fingerprint=on" if getattr(
            config, "message_fingerprint_enabled", True) else b"fingerprint=off")
    # v3-contract §6.1 (Batch B): wire-view domain marker — see docstring.
    fingerprint_parts.append(
        b"wire=v3" if wire_view == 3 else b"wire=v2")
    return b"\x00".join(fingerprint_parts)


def response_rep_version(
    config: _ConfigLike | None, *, wire_view: int = 2,
) -> bytes | None:
    """REP_VERSION for response emission, or ``None`` when ETag/304 is
    disabled (``etag_enabled=false`` → byte-identical legacy behaviour)."""
    if config is None or not getattr(config, "etag_enabled", True):
        return None
    return representation_version(config, wire_view=wire_view)


def compute_etag(identity_body: bytes, coding: str, rep_version: bytes) -> str:
    """Compute the validator for ``identity_body`` served as ``coding``.

    ``coding`` is ``"identity"`` (STRONG tag) or ``"gzip"`` (WEAK tag). The
    hash input is ALWAYS the identity bytes — the full sha256 hex digest is
    used, never truncated.
    """
    digest = hashlib.sha256(
        rep_version + b"\x00" + coding.encode("ascii") + b"\x00" + identity_body
    ).hexdigest()
    if coding == "gzip":
        return f'W/"{digest}"'
    return f'"{digest}"'


def if_none_match_matches(header_value: str | None, etag: str) -> bool:
    """RFC 9110 ``If-None-Match`` evaluation (weak comparison) + ``*``.

    Weak comparison: the ``W/`` prefix on either side is ignored and the
    opaque tags are compared. Malformed candidates are skipped; an empty
    header never matches.
    """
    if not header_value:
        return False
    value = header_value.strip()
    if not value:
        return False
    if value == "*":
        return True
    candidate = ""
    in_quotes = False
    candidates: list[str] = []
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
            candidate += ch
        elif ch == "," and not in_quotes:
            candidates.append(candidate)
            candidate = ""
        else:
            candidate += ch
    candidates.append(candidate)
    target = _opaque_tag(etag)
    for raw in candidates:
        opaque = _opaque_tag(raw.strip())
        if opaque and opaque == target:
            return True
    return False


def _opaque_tag(candidate: str) -> str | None:
    """Reduce a candidate to its opaque tag or ``None`` if malformed.

    RFC 9110: the weakness marker is the case-sensitive literal ``W/``
    (``weak = %s"W/"``). A lowercase ``w/`` prefix is NOT a weakness
    marker — such a candidate is malformed and skipped (never matches).
    """
    if len(candidate) < 2:
        return None
    if candidate.startswith("W/"):
        candidate = candidate[2:]
    if len(candidate) < 2 or not (
            candidate.startswith('"') and candidate.endswith('"')):
        return None
    inner = candidate[1:-1]
    if '"' in inner or not inner:
        return None
    return inner


MERGED_VARY_EXTRA = "X-Opencode-Directory"


def merged_vary(current: str | None) -> str:
    """Merge the directory dimension into an existing ``Vary`` value
    (append, never overwrite), keeping ``Accept-Encoding`` first."""
    parts = [p.strip() for p in (current or "").split(",") if p.strip()]
    if MERGED_VARY_EXTRA not in parts:
        parts.append(MERGED_VARY_EXTRA)
    return ", ".join(parts)


def judge_conditional(
    identity_body: bytes,
    if_none_match: str | None,
    rep_version: bytes,
    *,
    accept_encoding: str | None,
    min_gzip_bytes: int = MIN_GZIP_BYTES,
) -> str | None:
    """rev-5 coding-specific 304 judgment for the benefit-gated routes.

    Replaces the rev-4 dual-candidate scheme (which violated §4 :222-229
    coding-specific validators and B1-C5's conservative-200 rule). Returns:

    * ``None``          → no hit (or no header): serve the 200.
    * ``"*"``           → ``If-None-Match: *`` hit: the caller compresses
      ONCE (rare request — the compression cost is acceptable) and echoes
      the tag of the coding it will ACTUALLY serve.
    * a tag string      → 304, echoing exactly that validator. Zero
      compression happened to reach this verdict.

    Judgment rules (accepts_gzip + benefit-gated compressor routes only —
    the messages list/merged tails):

    1. Request can only be served identity (AE without gzip, or body below
       ``min_gzip_bytes`` — the min gate forces identity): single-candidate
       EXACT judgment on the identity strong tag. A gzip tag in the header
       cannot match (different opaque hash) → conservative 200 (C5 rule).
       ``*`` → 304 echoing the identity strong tag.
    2. Accepts gzip AND len >= min: the served coding is NOT statically
       knowable (the benefit gate's "compressed result not smaller"
       fallback requires actually compressing — which a 304 must never
       do). Single candidate = the gzip weak tag:

       * INM weak-matches the gzip tag → 304, echo the gzip tag. Echo
         soundness: a client LAWFULLY holding a gzip tag received gzip for
         THIS content last time ⟹ the content compresses; the content is
         unchanged (hash match) ⟹ this response would also be served
         gzip — the echo does not mislabel. (A hand-forged gzip tag for
         an incompressible body cannot arise from a lawful exchange: the
         server never emitted one.)
       * INM carries an identity strong tag → ALWAYS 200. The server
         cannot distinguish "client's last request was identity-only"
         from "benefit-gate fallback" history, so the conservative answer
         is a full 200 (C5 "reverse direction likewise").
       * ``*`` → 304 after one real compression, echoing the actual
         coding's tag.
    """
    if not if_none_match or not if_none_match.strip():
        return None
    if if_none_match.strip() == "*":
        return "*"
    if not (
        accepts_gzip(accept_encoding)
        and len(identity_body) >= min_gzip_bytes
    ):
        # Served coding is statically identity (rules 1/2): exact
        # single-candidate judgment on the identity strong tag.
        tag = compute_etag(identity_body, "identity", rep_version)
        return tag if if_none_match_matches(if_none_match, tag) else None
    gzip_tag = compute_etag(identity_body, "gzip", rep_version)
    if if_none_match_matches(if_none_match, gzip_tag):
        return gzip_tag
    # Identity-tag (or no) match under a gzip-capable request: the served
    # coding is unknowable without compressing — conservative 200.
    return None


def not_modified_response(
    etag_value: str, vary_value: str,
    aux: dict[str, str] | None = None,
) -> Response:
    """304 construction for the pre-compression single-candidate path.

    Same header set as :func:`conditional_304` (ETag + Vary + no-store +
    auxiliary headers; no body, no ``Content-Encoding``) — split out so
    the candidate-matching routes can pass the matched tag directly.
    """
    headers = {
        "ETag": etag_value,
        "Vary": vary_value,
        "Cache-Control": "no-store",
    }
    if aux:
        headers.update(aux)
    return Response(b"", status_code=304, headers=headers)


def conditional_304(
    final_headers: dict[str, str],
    if_none_match: str | None,
    aux: dict[str, str] | None = None,
):
    """Build the 304 response when the client's ``If-None-Match`` matches.

    Returns ``None`` when ETag is disabled (no ``ETag`` in headers — the
    pipeline just serves the packed 200) or the validator does not match.

    The 304 header set (plan §4): the SAME ``ETag`` and ``Vary`` values as
    the 200 would carry, ``Cache-Control: no-store``, plus the route's
    auxiliary headers (``aux`` — e.g. ``X-Next-Cursor`` /
    ``X-Complete``, computed by THIS pipeline run). No body, no
    ``Content-Encoding``.
    """
    etag_value = final_headers.get("ETag")
    if not etag_value:
        return None
    if not if_none_match_matches(if_none_match, etag_value):
        return None
    headers = {
        "ETag": etag_value,
        "Vary": final_headers.get("Vary", "Accept-Encoding"),
        "Cache-Control": "no-store",
    }
    if aux:
        headers.update(aux)
    return Response(b"", status_code=304, headers=headers)
