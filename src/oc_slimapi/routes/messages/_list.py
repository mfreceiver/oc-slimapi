"""lite-v2 §8 list family — sort / skeleton projection / cursor / Link
parsing / lease fetch + the ``GET /slimapi/messages/{sid}`` route (F-302
three-family split of ``routes/messages.py``; pure move, zero behaviour
change — the merged fan-out splice comes from :mod:`._full_merge`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlparse

import orjson
import httpx
from fastapi import Query, Request
from starlette.responses import Response

from ... import etag as etag_mod
from ...envelope import messages_envelope_bytes
from ...gzip_util import compress_if_beneficial, error_response
from ...skeleton import SkeletonLimits, skeleton_messages
from ...since_cache import CacheEntry, CommitResult, ObservedSnapshot, SinceCache
from ...transform import TransformBusy, read_with_cap
from ...upstream import forward_directory_headers
from ...upstream_errors import raise_upstream_unavailable
from .._catalog_common import read_upstream_response
from ._full_merge import _merge_fulls
from ._router import _busy_response, _resolve_messages_directory, router

# --- §3.3 per-feature gate → D5 adjudication (2026-08-22) -------------------
#
# 历史：``messages.expand.v4 ∈ SATISFIED`` 曾在此选择 §14 href view
# （satisfied → ``?v=4``；否则折回 3——4.0.0 已发布 href 面）。v4-only
# ``(4,4)`` selector 窗口下该折回自产自拒死链：v4 响应的 expandRefs
# href 带 ``?v=3``，而 v4-only selector 恰好 400 拒绝 ``?v=3``。
# 2026-08-22 owner 裁决（D5）：href wire view 双态恒 4，readiness 分支
# 删除（其唯一消费方就是本返回值——href 生成路径）；该 feature ID 的
# 门控语义仅存于 capabilities 面（versions.py ``expand`` block 发射）。
#
# ``_V4_EXPAND_FEATURE`` 保留：``messages/__init__.py`` 兼容 re-export
# 仍在导入（F-302 迁移面），亦作为 feature ID 的谱系锚点。

_V4_EXPAND_FEATURE = "messages.expand.v4"


def _expand_wire_view(scope) -> int:
    """Compatibility re-export for callers that still inspect the href view.

    Projection itself is now versionless and natively emits v4 hrefs.
    """
    del scope
    return 4


# ---------------------------------------------------------------------------
# lite-v2 §8 — skeleton list ordering contract.
# ---------------------------------------------------------------------------
#
# The list endpoint MUST return messages sorted by ``info.time.created`` ASC.
# Sidecar sorts defensively after parse and before skeleton projection — it
# does NOT rely on upstream opencode's default ordering. The contract holds
# even if opencode's ``orderBy`` ever changes; clients merging paginated
# skeleton pages depend on the strict-ASC guarantee.

def _created_value(msg: dict) -> int | float | None:
    """Explicit ``info.time.created`` parse — ``None`` when malformed.

    BE-001: this is the VALIDITY predicate, deliberately distinct from
    :func:`_created_sort_key`'s ``0`` sentinel. The sentinel overloads
    "malformed" with a legal sort position (schema allows a legitimate
    ``created == 0`` epoch), so boundary logic must NOT reuse it — a
    degenerate row must be distinguishable from a well-formed ``0`` row.
    Only :func:`_created_sort_key` maps ``None`` back to ``0``.
    """
    info = msg.get("info") if isinstance(msg, dict) else None
    if not isinstance(info, dict):
        return None
    time_obj = info.get("time")
    if not isinstance(time_obj, dict):
        return None
    raw = time_obj.get("created")
    # Q7-P3-19 (owner adjudication 2026-08-22): accept int OR finite float.
    # Upstream practice only ever writes int epochs (ms) — this widening is
    # purely defensive alignment with the §14 ordering contract: a JSON
    # number is grammatically allowed to be fractional, and the old
    # ``isinstance(raw, int)`` predicate judged a finite float epoch
    # malformed (key 0 → page head), silently breaking strict-ASC for that
    # row. int/float mixtures compare numerically natively. bool stays
    # malformed (bool is an int subclass — exclusion carried over from the
    # historical predicate); nan/inf floats stay malformed (nan breaks
    # total ordering, inf would clamp to one end; note orjson already
    # rejects both at parse time — defense-in-depth) as do str/None.
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and math.isfinite(raw):
        return raw
    return None


def _created_sort_key(msg: dict) -> int | float:
    """Sort key: ``info.time.created`` ASC.

    Defaults to ``0`` for missing / malformed fields (see
    :func:`_created_value`) so degenerate upstream rows sort first under
    Python's stable sort instead of crashing the worker. This float-to-top
    behaviour is intentional §8 wire surface and is NOT changed by BE-001.
    """
    value = _created_value(msg)
    return 0 if value is None else value


def _parse_sort_project(
    body: bytes, *, limits: SkeletonLimits, sid: str | None = None,
) -> list[dict]:
    """Worker entry: parse + sort by ``info.time.created`` ASC + skeleton
    project (no serialization).

    Shared by the default pack worker (:func:`_project_list_sorted_and_pack`)
    and the L2-CD-2 merged path, which needs the projected dicts (to detect
    placeholder messages and later splice inlined fulls) before packing.

    ``sid`` is threaded through to ``skeleton_messages`` so the projection
    emits ``expandRefs`` (design-expand §5.2) — without it, lane A's refs
    never reach the wire and the merged ref candidate set is empty.

    Batch 4 / B3: the fingerprint switch rides on ``limits.fingerprint``
    (built by the route from config) so this worker's signature is
    unchanged — the projection injects ``contentFingerprint`` at completion
    time; merged splices overwrite it later (``_merge_fulls_and_pack``).
    """
    parsed = orjson.loads(body)
    if not isinstance(parsed, list) or not all(
        isinstance(m, dict) for m in parsed
    ):
        # Mirrors sessions.py non-list guard: a non-list body (dict/null)
        # OR a list with non-dict elements (scalar-list like [1,2,"x"])
        # would make skeleton_messages / _created_sort_key call .get() on
        # the wrong type → AttributeError. Treat as malformed upstream →
        # route maps to 503.
        raise ValueError("upstream message body is not a list of message dicts")
    parsed.sort(key=_created_sort_key)
    return skeleton_messages(parsed, limits=limits, sid=sid)


def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None, limits: SkeletonLimits,
    sid: str | None = None,
) -> bytes:
    """Worker entry: parse + sort + project + serialize to identity bytes.

    lite-v2 §8: skeleton list endpoint must return messages sorted by
    ``info.time.created`` ASC. Sort defensively rather than relying on
    upstream opencode's default ordering. Mirrors ``transform._pack_json``
    inline (kept private to that module) so this stays self-contained.

    ``limits`` carries the per-call inline caps (built by the route from
    ``request.app.state.config``) so two apps with different Settings project
    the same upstream body differently — the worker never reads module-level
    config (T8-C1 / T8-C6).

    Traffic plan Batch 2 / B1 (pre-compression validator): compression moved
    OUT of the worker to the route, which derives the canonical ETag from
    the identity bytes, judges ``If-None-Match`` (a gzip hit = canonical
    hash only — zero compression, plan §4) and only then compresses.
    ``accept_encoding`` is retained in the signature for call-site symmetry
    (and existing slow-pack monkeypatches); the route owns coding choice.

    Expand hrefs are generated natively as v4 by the projection.
    """
    projected = _parse_sort_project(body, limits=limits, sid=sid)
    return orjson.dumps(projected)


def _judge_pack_tail(
    identity: bytes, *, accept_encoding: str | None,
    if_none_match: str | None, rep_version: bytes | None,
) -> tuple[str | None, bytes, dict[str, str], str | None]:
    """F-201/F-271 off-loop response tail: 304 judgment + gzip + validator.

    Thin delegation to :func:`oc_slimapi.etag.encode_conditional_tail`
    (ARCH-2: the judge→compress→validator pipeline exists exactly once).
    The list/merged routes produce the envelope identity bytes in the
    transform worker and run this tail via ``pool.offload`` WITHOUT
    holding admission (a slot here would 503 requests that already
    finished projecting; design §1.3) — pure CPU, wire bytes identical
    to the historical inline tail.

    Messages-envelope semantics: ``judge_empty_body=True`` — the
    ``orjson.dumps`` envelope is never empty and the historical tail
    judged unconditionally. ``compress`` resolves THIS module's
    ``compress_if_beneficial`` global at call time — the W3-2 test seam
    spies that binding (tests/test_etag.py::
    test_b1_4_gzip_hit_does_not_compress_messages). 304-branch
    placeholders are ``(b"", {})`` (was ``(None, None)`` — never read
    by callers; both call sites return ``not_modified_response``
    immediately on a verdict).

    Kept as a named module-level wrapper (not inlined at the call
    sites) so the offload identity proofs keep observing this exact
    function object (tests/test_offload_equivalence.py::
    test_messages_tail_offload_proof).
    """
    return etag_mod.encode_conditional_tail(
        identity,
        accept_encoding=accept_encoding,
        if_none_match=if_none_match,
        rep_version=rep_version,
        judge_empty_body=True,
        compress=compress_if_beneficial,
    )


_REL_PARAM_RE = re.compile(
    # Match the ``rel`` link-param anywhere in a Link entry's attribute
    # string. Pre-anchor on start/whitespace/``;`` so we don't false-match
    # a ``rel=`` substring tucked inside another param's quoted value
    # (e.g. ``title="rel=next"``). IGNORECASE covers the param NAME;
    # value tokens are lowercased by the caller per RFC 5988 §3.
    r'(?:^|[\s;])rel\s*=\s*(?:"([^"]*)"|([^;\s]+))',
    re.IGNORECASE,
)


def _link_rel_tokens(attrs: str) -> list[str]:
    """Extract the ``rel`` link-param value as a list of lowercased tokens.

    RFC 5988 §3 allows multiple whitespace-separated relation types
    (``rel="prev next"``) and treats relation types as case-insensitive.
    Returns ``[]`` when ``rel`` is absent or has no value.
    """
    match = _REL_PARAM_RE.search(attrs)
    if not match:
        return []
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return [tok.lower() for tok in raw.split() if tok]


def _extract_before_verbatim(query: str) -> str | None:
    """Return the raw ``before=<value>`` substring from a URL query string
    WITHOUT any percent-decoding or ``+``→space substitution.

    opencode's cursor is an opaque base64url JSON envelope that the upstream
    ``cursor.decode`` consumes byte-for-byte. ``parse_qs`` / ``unquote_plus``
    would transform ``%2B``→``+`` and ``+``→space, silently corrupting the
    round-trip — the value might happen to look unchanged on the base64url
    charset today, but that would be luck, not design. We MUST return the
    original substring as it appeared on the wire.

    Returns None when the param is absent OR present without a value
    (``?before`` with no ``=``) — both are treated as "no cursor" so the
    caller fails safe (no X-Next-Cursor emitted).
    """
    if not query:
        return None
    for part in query.split("&"):
        if part == "before":
            # ``?before`` with no value → treat as absent (fail-safe).
            return None
        if part.startswith("before="):
            # Slice after ``before=`` to end of this ``&``-segment. Any
            # percent-escapes / ``+`` are preserved verbatim.
            return part[len("before="):]
    return None


def _parse_link_next_cursor(link_header: str | None) -> str | None:
    """RFC 5988 ``Link`` parser — extract the ``before`` query param from the
    ``rel="next"`` URL. Returns the opaque cursor string verbatim, or None
    when no next-page link is present.

    opencode advertises pagination via
    ``Link: <...?before=<opaque>&limit=N>; rel="next"`` and recognises the
    same opaque cursor as ``?before`` on the next request. We surface that
    cursor verbatim as our own ``X-Next-Cursor`` response header so clients
    never need to know opencode's cursor format — and crucially, never
    substitute a bare messageID (opencode rejects those with 400 because
    the cursor is a base64url JSON envelope, not a raw id).

    The ``before`` value is sliced out of the raw query string via
    :func:`_extract_before_verbatim` — never ``parse_qs`` / ``unquote``,
    which would percent-decode and corrupt the opaque token.

    ``rel`` matching follows RFC 5988 §3: case-insensitive on both the param
    name and the relation-type tokens, and supports multi-token values
    (``rel="prev next"`` is recognised as a next link). Entries are still
    split on ``,`` — safe because opencode's base64url cursors do not
    contain commas.
    """
    if not link_header:
        return None
    for raw in link_header.split(","):
        segment = raw.strip()
        if not segment.startswith("<"):
            continue
        end = segment.find(">")
        if end < 0:
            continue
        url = segment[1:end]
        attrs = segment[end + 1:]
        # RFC 5988 §3: relation types are case-insensitive tokens. Match
        # any entry whose ``rel`` value contains ``next`` as one of its
        # whitespace-separated tokens — handles ``rel="next"``,
        # ``rel="prev next"``, ``rel=next``, ``REL="Next"`` uniformly.
        if "next" not in _link_rel_tokens(attrs):
            continue
        # urlparse splits structurally without decoding the query — safe.
        cursor = _extract_before_verbatim(urlparse(url).query)
        if cursor is not None:
            return cursor
    return None


async def _stream_upstream(
    request: Request, path: str, params: dict, directory: str | None,
):
    """Build & send a streaming GET so we can cap-read the body instead of buffering.

    Caller MUST ``await response.aclose()`` — typically inside a ``finally``
    block — to release the underlying httpx connection.
    """
    upstream_request = request.app.state.upstream.build_request(
        "GET", path, params=params,
        headers=forward_directory_headers(directory),
    )
    try:
        return await request.app.state.upstream.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)


# ---------------------------------------------------------------------------
# Upstream-fetch coalescing (traffic plan Batch 1 / A2, §3.x join-first).
#
# The SHARED unit is the upstream list GET + cap-read (NOT the projection):
# callers first obtain the raw body through the per-app
# ``LeasedSingleFlight`` registry (``app.state.raw_fetch_registry``), then
# each runs its own pool admission + offload projection — byte-identical to
# the direct path. A full registry budget returns ``None`` and the caller
# falls back to the unchanged admission-first direct path below.
# ---------------------------------------------------------------------------

def _canonical_list_query(limit: int, before: str | None, mode: str | None) -> str:
    """Deterministic, sorted query string for the coalescing key.

    Key-order normalisation (sorted) prevents semantically identical queries
    from splitting the flight key. ``mode`` participates even though it is
    not forwarded upstream: ``mode=merged`` callers keep their own flight so
    merged and default pages never observe each other's shared bodies (the
    upstream resource is identical, but A2-C2 locks the conservative split).
    """
    parts: dict[str, str] = {"limit": str(limit)}
    if before is not None:
        parts["before"] = before
    if mode is not None:
        parts["mode"] = mode
    return "&".join(f"{name}={parts[name]}" for name in sorted(parts))


def _messages_list_key(
    request: Request, sid: str, directory: str | None,
    limit: int, before: str | None, mode: str | None,
) -> tuple:
    """Flight key for the list GET: embeds the upstream client identity
    (defense-in-depth against cross-app sharing, mirroring
    ``full_fetch_key``'s ``id(scope)`` convention), the session, the
    directory (same resource under another directory = another upstream
    resource) and the canonical query."""
    return (
        "messages-list", id(request.app.state.upstream), sid, directory,
        _canonical_list_query(limit, before, mode),
    )


def _since_cq_hash(limit: int, directory: str | None, mode: str | None) -> str:
    """Canonical query identity frozen by Phase B §3.2/§3.3."""
    effective_directory = directory or ""
    normalized_mode = "merged" if mode == "merged" else "baseline"
    return f"v1:{limit}:{effective_directory}:{normalized_mode}"


def _invalid_since_params() -> None:
    from ...errors import CodedHTTPException

    raise CodedHTTPException(400, code="invalid_params")


@dataclass(frozen=True)
class _SinceRequest:
    cache: SinceCache | None
    key: tuple[str, str] | None
    observed: ObservedSnapshot | None
    baseline: CacheEntry | None
    since: str | None
    before_present: bool

    @property
    def requested(self) -> bool:
        return self.since is not None

    @property
    def needs_artifact(self) -> bool:
        return self.requested or (
            self.cache is not None
            and self.cache.enabled
            and not self.before_present
        )


def _since_request_state(
    request: Request,
    sid: str,
    directory: str | None,
    limit: int,
    before: str | None,
    mode: str | None,
    since: str | None,
) -> _SinceRequest:
    """Validate since/before multiplicity and capture the CAS lineage."""
    since_values = request.query_params.getlist("since")
    before_values = request.query_params.getlist("before")
    if len(since_values) > 1 or len(before_values) > 1:
        _invalid_since_params()
    if since_values:
        since = since_values[0]
    before_present = bool(before_values) or before is not None
    if since is not None and before_present:
        _invalid_since_params()

    cache = getattr(request.app.state, "since_cache", None)
    if since is not None and not isinstance(cache, SinceCache):
        _invalid_since_params()
    if cache is None:
        return _SinceRequest(None, None, None, None, since, before_present)

    cq_hash = _since_cq_hash(limit, directory, mode)
    key = (sid, cq_hash)
    # observed_snapshot is deliberately captured for every no-before request,
    # including a request without since, and before any upstream await.
    observed = None if before_present else cache.observe(key)
    baseline = None
    if since is not None:
        check = cache.check_token(since, sid=sid, cq_hash=cq_hash)
        # v6.1 adjudication (2026-08-22): ``invalid`` (400) covers only
        # syntax/shape/version/length errors and a sid mismatch.  A cq_hash
        # mismatch (limit/directory/mode axis change) classifies as ``reset``
        # — the request falls through with no baseline, so the response is
        # the full projection and commit() issues a fresh nextSince token.
        if check.kind == "invalid":
            _invalid_since_params()
        if (
            check.kind == "valid"
            and observed is not None
            and observed.entry is not None
            and observed.generation == check.generation
        ):
            baseline = observed.entry
    return _SinceRequest(cache, key, observed, baseline, since, before_present)


@dataclass(frozen=True)
class _ProjectionArtifact:
    array_bytes: bytes
    canonical_items: bytes
    changed_bytes: bytes
    fingerprints: dict[str, str]
    changed: list[dict]
    removed: list[str]
    cacheable: bool


def _message_id(item: dict) -> str | None:
    info = item.get("info")
    mid = info.get("id") if isinstance(info, dict) else None
    return mid if isinstance(mid, str) and mid else None


def _boundary_key(item: dict, mid: str | None) -> tuple[int | float, str] | None:
    if mid is None:
        return None
    created = _created_value(item)
    if created is None:
        # BE-001: a degenerate row refuses to serve as a diff boundary.
        # The historical ``(_created_sort_key(item), mid)`` minted the
        # ``(0, deg_mid)`` sentinel tuple, which compares strictly SMALLER
        # than every well-formed baseline key — so in a non-exhausted
        # window every absent baseline mid judged ``newer`` and the whole
        # window false-positived as removed. §10.3 freezes "removed must
        # never show false positives"; returning None makes the caller's
        # ``fresh_oldest_key is not None`` guard skip removal inference
        # for the window entirely (conservative false-negative path —
        # contract-tolerated). A legitimate ``created == 0`` row still
        # returns ``(0, mid)`` and compares normally.
        return None
    return (created, mid)


def _artifact_from_array_bytes(
    array_bytes: bytes,
    *,
    baseline: CacheEntry | None,
    next_cursor: str | None,
    before_present: bool,
) -> _ProjectionArtifact:
    """Build canonical cache material and the since diff in a worker."""
    items = orjson.loads(array_bytes)
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("projected message body is not a list of objects")

    fingerprints: dict[str, str] = {}
    fresh_mids: set[str] = set()
    cacheable = True
    for item in items:
        canonical_item = orjson.dumps(item, option=orjson.OPT_SORT_KEYS)
        mid = _message_id(item)
        if mid is None or mid in fingerprints:
            cacheable = False
            continue
        fresh_mids.add(mid)
        fingerprints[mid] = sha256(canonical_item).hexdigest()
    canonical_items = orjson.dumps(items, option=orjson.OPT_SORT_KEYS)

    changed = list(items)
    removed: list[str] = []
    if baseline is not None:
        changed = [
            item for item in items
            if (
                _message_id(item) is None
                or _message_id(item) not in baseline.fingerprints
                or fingerprints.get(_message_id(item))
                != baseline.fingerprints.get(_message_id(item))
            )
        ]
        fresh_oldest = items[0] if items else None
        fresh_oldest_key = _boundary_key(fresh_oldest, _message_id(fresh_oldest)) if fresh_oldest else None
        window_exhausted = not before_present and next_cursor is None
        baseline_items = orjson.loads(baseline.canonical_items)
        baseline_by_mid = {
            _message_id(item): item
            for item in baseline_items
            if isinstance(item, dict) and _message_id(item) is not None
        }
        for mid in baseline.fingerprints:
            if mid in fresh_mids:
                continue
            newer = False
            if not window_exhausted and fresh_oldest_key is not None:
                old_key = _boundary_key(baseline_by_mid.get(mid, {}), mid)
                newer = old_key is not None and old_key > fresh_oldest_key
            if window_exhausted or newer:
                removed.append(mid)

    return _ProjectionArtifact(
        array_bytes=array_bytes,
        canonical_items=canonical_items,
        changed_bytes=orjson.dumps(changed),
        fingerprints=fingerprints,
        changed=changed,
        removed=removed,
        cacheable=cacheable,
    )


def _project_list_artifact(
    body: bytes,
    *,
    accept_encoding: str | None,
    limits: SkeletonLimits,
    sid: str | None,
    baseline: CacheEntry | None,
    next_cursor: str | None,
    before_present: bool,
) -> _ProjectionArtifact:
    """Existing projection seam plus cache/diff admission work."""
    array_bytes = _project_list_sorted_and_pack(
        body, accept_encoding=accept_encoding, limits=limits, sid=sid,
    )
    return _artifact_from_array_bytes(
        array_bytes, baseline=baseline, next_cursor=next_cursor,
        before_present=before_present,
    )


_ENVELOPE_MISSING = object()


def _messages_envelope_with_since(
    items_bytes: bytes,
    next_cursor: str | None,
    *,
    removed: list[str] | object = _ENVELOPE_MISSING,
    next_since: str | object = _ENVELOPE_MISSING,
) -> bytes:
    """Append Phase B fields without changing the existing envelope helper."""
    identity = messages_envelope_bytes(items_bytes, next_cursor)
    extras: list[bytes] = []
    if removed is not _ENVELOPE_MISSING:
        extras.append(b'"removed":' + orjson.dumps(removed))
    if next_since is not _ENVELOPE_MISSING:
        extras.append(b'"nextSince":' + orjson.dumps(next_since))
    if not extras:
        return identity
    return identity[:-1] + b"," + b",".join(extras) + b"}"


def _publish_since(
    state: _SinceRequest,
    artifact: _ProjectionArtifact | None,
) -> CommitResult | None:
    if (
        state.cache is None
        or state.key is None
        or state.observed is None
        or artifact is None
    ):
        return None
    return state.cache.commit(
        state.key,
        state.observed,
        artifact.canonical_items,
        artifact.fingerprints,
        cacheable=artifact.cacheable,
    )


def _next_since_for(
    state: _SinceRequest,
    result: CommitResult | None,
) -> str | None:
    if (
        state.cache is None
        or result is None
        or result.entry is None
        or result.omitted
        or state.key is None
        or state.before_present
    ):
        return None
    sid, cq_hash = state.key
    return state.cache.issue_token(sid, cq_hash, result.entry.generation)


async def _fetch_list_raw(
    request: Request, sid: str, params: dict, directory: str | None,
    *, cap: int,
) -> tuple[bytes | None, str | None]:
    """Shared factory body: ONE upstream list GET + cap-read, plus the
    Link→cursor capture that must happen before ``aclose()``. The result
    tuple is what every joiner of the flight receives — same upstream
    response, same opaque cursor."""
    response = await _stream_upstream(
        request, f"/session/{sid}/message", params, directory,
    )
    try:
        body = await read_upstream_response(
            request, response,
            cap=cap,
            read_with_cap=read_with_cap,
            sid=sid,
        )
        next_cursor = _parse_link_next_cursor(response.headers.get("Link"))
        return body, next_cursor
    finally:
        await response.aclose()


async def _messages_via_lease(
    request: Request, registry, pool, config, sid: str,
    directory: str | None, params: dict,
    limit: int, before: str | None, mode: str | None,
    since_state: _SinceRequest,
    *, merged_mode: bool,
) -> Response | None:
    """Join-first lease path (plan §3.x): fetch the raw list body through
    the registry, then run the caller's OWN pool admission + offload
    projection — identical shapes, headers, error mappings and busy
    semantics to the direct path.

    Returns ``None`` when the registry budget is full (``fetch_or_bypass``
    bypass) — the caller then takes the unchanged admission-first direct
    path for this request.
    """
    accept_encoding = request.headers.get("accept-encoding")

    async def _factory() -> tuple[bytes | None, str | None]:
        return await _fetch_list_raw(
            request, sid, params, directory, cap=config.max_response_bytes,
        )

    lease = await registry.fetch_or_bypass(
        _messages_list_key(request, sid, directory, limit, before, mode),
        _factory,
        reserve_bytes=config.max_response_bytes,
    )
    if lease is None:
        return None  # budget full → direct path (plan §3.x bypass rule)
    async with lease:
        body, next_cursor = lease.body
        if body is None:
            return error_response(
                "response_too_large", 413,
                limit=config.max_response_bytes,
                accept_encoding=accept_encoding,
            )
        limits = SkeletonLimits(
            field_bytes=config.skeleton_inline_output_max_bytes,
            message_bytes=config.skeleton_inline_output_max_message_bytes,
            fingerprint=config.message_fingerprint_enabled,
        )
        projected: list[dict] | None = None
        identity: bytes | None = None
        artifact: _ProjectionArtifact | None = None
        try:
            # The caller's own admission + offload — the same
            # admission-before-projection discipline (and the same
            # ``transform_busy`` 503 shape) as the direct path; only the
            # raw GET moved out (join-first, plan §3.x).
            async with pool:
                try:
                    if merged_mode:
                        projected = await pool.offload(
                            _parse_sort_project, body, limits=limits,
                            sid=sid,
                        )
                    else:
                        if since_state.needs_artifact:
                            artifact = await pool.offload(
                                _project_list_artifact, body,
                                accept_encoding=accept_encoding,
                                limits=limits, sid=sid,
                                baseline=since_state.baseline,
                                next_cursor=next_cursor,
                                before_present=since_state.before_present,
                            )
                            identity = artifact.array_bytes
                        else:
                            identity = await pool.offload(
                                _project_list_sorted_and_pack, body,
                                accept_encoding=accept_encoding,
                                limits=limits, sid=sid,
                            )
                except (orjson.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                    raise_upstream_unavailable(exc)
            if projected is not None:
                # Merged phases B+C: fan-out via the UNCHANGED
                # ``singleflight.fulls`` registry, then one splice offload
                # (oracle §C-2 — two-level dedup, no interference).
                identity = await _merge_fulls(
                    request, pool, config, projected, sid, directory,
                    accept_encoding=accept_encoding,
                    fingerprint=config.message_fingerprint_enabled,
                )
            if since_state.needs_artifact and artifact is None:
                async with pool:
                    artifact = await pool.offload(
                        _artifact_from_array_bytes, identity,
                        baseline=since_state.baseline,
                        next_cursor=next_cursor,
                        before_present=since_state.before_present,
                    )
            result = _publish_since(since_state, artifact)
            response_items = (
                artifact.changed_bytes
                if since_state.requested and artifact is not None
                else identity
            )
            next_since = _next_since_for(since_state, result)
            # §4.1 terminal (v3-only): the packed bare array is spliced
            # into the envelope verbatim BEFORE any validator work — the
            # envelope bytes ARE the canonical ETag input (§6.3). The
            # X-Next-Cursor header is retired (§1): the client reads
            # ``nextCursor`` from the cached envelope (§6.4).
            identity = _messages_envelope_with_since(
                response_items, next_cursor,
                # Gate-MAJOR-1 (§10.3 freeze): ``removed`` appears ONLY on a
                # genuine diff response — i.e. this request carried a since
                # AND resolved a valid diff baseline. Every reset family
                # (cq_hash/epoch mismatch, miss, LRU eviction) runs with
                # baseline=None, so the key must stay ABSENT there, not
                # ``[]``.
                removed=(artifact.removed if artifact is not None else [])
                if since_state.requested and since_state.baseline is not None
                else _ENVELOPE_MISSING,
                next_since=next_since if next_since is not None else _ENVELOPE_MISSING,
            )
            base_headers: dict[str, str] = {"Cache-Control": "no-store"}
            # Batch 2 / B1-1R (rev-5): coding-specific SINGLE-candidate 304
            # judgment, pre-compression (plan §4 :222-229 — a validator hit
            # is zero compression). Identity-only / sub-min requests judge
            # the identity strong tag exactly; gzip-capable requests judge
            # ONLY the gzip weak tag (an identity tag gets a conservative
            # 200 — B1-C5 reverse direction). ``*`` compresses once and
            # echoes the actual coding's tag. The 200 below labels its
            # validator with the coding it ACTUALLY carries (B1-1R).
            # F-201/F-271: the judge+gzip+validator tail runs in the
            # transform worker (``_judge_pack_tail``) — off the event loop,
            # admission NOT held (a slot here would 503 requests that
            # already finished projecting; design §1.3).
            rep_version = etag_mod.response_rep_version(
                config, wire_view=4)
            # D6 owner 裁决 2026-08-22：messages ETag 域标签统一为窗口
            # 版本 4；代价是一次性 validator 轮换（客户端全量重拉一轮，
            # 与 4.9.0 REP_VERSION 轮换同类）。与 sessions 侧
            #（sessions.py / _catalog_common.py）域标签一致。
            # §6.2 (gate C3): directory-sensitive route — the directory
            # Vary dimension is unconditional (cache-correctness semantics,
            # NOT an ETag accessory; Batch 3 merge_directory_vary precedent).
            vary_value = etag_mod.merged_vary("Accept-Encoding")
            # 304 never carries aux headers (§6.4 terminal: the
            # X-Next-Cursor channel is retired; the cached envelope
            # carries the cursor).
            verdict, encoded, c_headers, etag_value = await pool.offload(
                _judge_pack_tail, identity,
                accept_encoding=accept_encoding,
                if_none_match=request.headers.get("if-none-match"),
                rep_version=rep_version,
            )
            if verdict is not None:
                return etag_mod.not_modified_response(
                    etag_value, vary_value, aux=None)
            final_headers = dict(c_headers)
            final_headers["Vary"] = vary_value
            if etag_value is not None:
                final_headers["ETag"] = etag_value
            return Response(
                encoded, status_code=200, media_type="application/json",
                headers={**base_headers, **final_headers},
            )
        except TransformBusy:
            return _busy_response(accept_encoding)


@router.get("")
async def messages(
    request: Request,
    sid: str,
    limit: int = Query(40, ge=1, le=200),
    before: str | None = None,
    directory: str | None = None,
    mode: str | None = None,
    since: str | None = Query(None),
):
    """Skeleton projection of upstream opencode's message listing.

    lite-v2 §2: ``?mode=full`` list branch removed; only skeleton projection
    remains. ``?mode`` values other than the literal ``merged`` are silently
    ignored (clients in transition may still send them; oracle §C-1 keeps
    this additive — never a 400). ``?limit=`` and ``?before=`` pagination
    are preserved (cursor forwarded verbatim).

    L2-CD-2: ``?mode=merged`` (literal, case-sensitive) additionally expands
    the page's skeleton-collapsed messages in place: their placeholder parts
    are replaced by the messages' full projections (diagnostics stripped),
    fetched fan-out style under ``merged_fanout`` / ``merged_max_fulls_per_page``
    / ``merged_max_bytes`` budgets and deduped with concurrent direct /full
    requests via ``singleflight.fulls``. Over-budget / failed items
    progressively keep their skeleton projection; ``X-Next-Cursor`` and all
    non-placeholder messages are byte-identical to the default mode. The
    fan-out holds no per-full transform-pool slot (oracle §C-2), so a merged
    page cannot starve concurrent transforms — see ``_merge_fulls``.

    lite-v2 §8: response is sorted by ``info.time.created`` ASC — see
    ``_project_list_sorted_and_pack``.
    """
    directory = await _resolve_messages_directory(request, directory)
    since_state = _since_request_state(
        request, sid, directory, limit, before, mode, since,
    )
    params = {"limit": limit}
    if before:
        # `before` is opencode's opaque base64url pagination cursor (a
        # base64url JSON envelope — see _extract_before_verbatim /
        # _parse_link_next_cursor). base64url uses only [-_A-Za-z0-9] (no
        # "+" / "/" / space), so FastAPI's percent-decoding of this query
        # param round-trips safely; forward it verbatim to upstream.
        params["before"] = before
    config = request.app.state.config
    pool = request.app.state.transforms
    merged_mode = mode == "merged"
    # Join-first coalescing (plan §3.x / A2): try the registry FIRST; a
    # ``None`` return (budget full, disabled, or no registry on old app
    # instances) falls through to the unchanged admission-first path.
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    if registry is not None and config.coalesce_enabled:
        leased = await _messages_via_lease(
            request, registry, pool, config, sid, directory, params,
            limit, before, mode, since_state, merged_mode=merged_mode,
        )
        if leased is not None:
            return leased
    projected: list[dict] | None = None
    identity: bytes | None = None
    artifact: _ProjectionArtifact | None = None
    next_cursor: str | None = None
    try:
        # Admission BEFORE the upstream GET: this is the key fix. The prior
        # code buffered the entire upstream body and only then tried to
        # acquire the semaphore, so N concurrent clients could each buffer a
        # large body before any of them failed admission.
        async with pool:
            response = await _stream_upstream(
                request, f"/session/{sid}/message", params, directory,
            )
            try:
                # Shared drain-or-cap-read skeleton (status mapping with sid +
                # read_with_cap + mid-stream RequestError → 503).
                body = await read_upstream_response(
                    request, response,
                    cap=config.max_response_bytes,
                    read_with_cap=read_with_cap,
                    sid=sid,
                )
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                # Translate opencode's Link header into our X-Next-Cursor
                # (opaque passthrough). Capture BEFORE aclose() for clarity,
                # though httpx headers remain readable afterward. Never
                # leak the upstream Link header itself — the sidecar's
                # pagination contract is X-Next-Cursor, so clients see one
                # consistent shape whether they consume list or since.
                next_cursor = _parse_link_next_cursor(response.headers.get("Link"))
                # Full parse + sort (§8) + project (+ serialize/gzip for the
                # default mode) in the worker pool. SkeletonLimits is built
                # per-request from this app's config so two apps with
                # different caps project the same upstream body differently
                # (P1-3 config de-double-tracking; T8-C4 / T8-C6).
                #
                # L2-CD-2: merged keeps the PROJECTED dicts (no pack yet) —
                # the fan-out + splice + pack run AFTER this admission is
                # released (see _merge_fulls; oracle §C-2: the fan-out must
                # not hold the slot across per-full network GETs).
                #
                try:
                    if merged_mode:
                        projected = await pool.offload(
                            _parse_sort_project, body,
                            limits=SkeletonLimits(
                                field_bytes=config.skeleton_inline_output_max_bytes,
                                message_bytes=config.skeleton_inline_output_max_message_bytes,
                                fingerprint=config.message_fingerprint_enabled,
                            ),
                            sid=sid,
                        )
                    else:
                        limits = SkeletonLimits(
                            field_bytes=config.skeleton_inline_output_max_bytes,
                            message_bytes=config.skeleton_inline_output_max_message_bytes,
                            fingerprint=config.message_fingerprint_enabled,
                        )
                        if since_state.needs_artifact:
                            artifact = await pool.offload(
                                _project_list_artifact, body,
                                accept_encoding=request.headers.get("accept-encoding"),
                                limits=limits, sid=sid,
                                baseline=since_state.baseline,
                                next_cursor=next_cursor,
                                before_present=since_state.before_present,
                            )
                            identity = artifact.array_bytes
                        else:
                            identity = await pool.offload(
                                _project_list_sorted_and_pack, body,
                                accept_encoding=request.headers.get("accept-encoding"),
                                limits=limits, sid=sid,
                            )
                except (orjson.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                    raise_upstream_unavailable(exc)
            finally:
                await response.aclose()
        if projected is not None:
            # Merged phases B+C: fan-out (no slot) → single splice offload
            # under admission with the EXISTING busy semantics.
            identity = await _merge_fulls(
                request, pool, config, projected, sid, directory,
                accept_encoding=request.headers.get("accept-encoding"),
                fingerprint=config.message_fingerprint_enabled,
            )
        if since_state.needs_artifact and artifact is None:
            async with pool:
                artifact = await pool.offload(
                    _artifact_from_array_bytes, identity,
                    baseline=since_state.baseline,
                    next_cursor=next_cursor,
                    before_present=since_state.before_present,
                )
        result = _publish_since(since_state, artifact)
        response_items = (
            artifact.changed_bytes
            if since_state.requested and artifact is not None
            else identity
        )
        next_since = _next_since_for(since_state, result)
        # §4.1 terminal (v3-only — same tail as the lease path above):
        # envelope splice before validator work; X-Next-Cursor retired
        # (§1) — the client reads ``nextCursor`` from the cached envelope
        # (§6.4).
        identity = _messages_envelope_with_since(
            response_items, next_cursor,
            # Gate-MAJOR-1 (§10.3 freeze, same as the lease tail): the
            # ``removed`` key exists only on a genuine diff response — a
            # since request that resolved a valid baseline. Reset families
            # run with baseline=None and must NOT carry the key.
            removed=(artifact.removed if artifact is not None else [])
            if since_state.requested and since_state.baseline is not None
            else _ENVELOPE_MISSING,
            next_since=next_since if next_since is not None else _ENVELOPE_MISSING,
        )
        base_headers: dict[str, str] = {"Cache-Control": "no-store"}
        # Batch 2 / B1-1R (rev-5, same tail as the lease path): coding-
        # specific SINGLE-candidate pre-compression judgment (identity-only
        # / sub-min → exact identity tag; gzip-capable → gzip tag only,
        # identity tag gets a conservative 200 per B1-C5; ``*`` compresses
        # once and echoes the actual coding). Zero compression on every
        # non-star 304. The 200 labels its validator with the coding it
        # ACTUALLY carries. Aux header value comes from THIS run.
        # F-201/F-271: same offloaded tail worker as the lease path.
        rep_version = etag_mod.response_rep_version(
            config, wire_view=4)
        # D6 owner 裁决 2026-08-22：域标签统一为窗口版本（同 lease 路径
        # 注记），一次性 validator 轮换。
        # §6.2 (gate C3): unconditional directory Vary — same as the lease
        # tail; directory-sensitivity does not depend on validator support.
        vary_value = etag_mod.merged_vary("Accept-Encoding")
        # 304 never carries aux headers (§6.4 terminal — same as the lease
        # tail).
        verdict, encoded, c_headers, etag_value = await pool.offload(
            _judge_pack_tail, identity,
            accept_encoding=request.headers.get("accept-encoding"),
            if_none_match=request.headers.get("if-none-match"),
            rep_version=rep_version,
        )
        if verdict is not None:
            return etag_mod.not_modified_response(
                etag_value, vary_value, aux=None)
        final_headers = dict(c_headers)
        final_headers["Vary"] = vary_value
        if etag_value is not None:
            final_headers["ETag"] = etag_value
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={**base_headers, **final_headers},
        )
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))
