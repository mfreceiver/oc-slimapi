"""lite-v2 §8 list family — sort / skeleton projection / cursor / Link
parsing / lease fetch + the ``GET /slimapi/messages/{sid}`` route (F-302
three-family split of ``routes/messages.py``; pure move, zero behaviour
change — the merged fan-out splice comes from :mod:`._full_merge`).
"""

from __future__ import annotations

import math
import re
from urllib.parse import urlparse

import orjson
import httpx
from fastapi import Query, Request
from starlette.responses import Response

from ... import etag as etag_mod
from ...envelope import messages_envelope_bytes
from ...gzip_util import compress_if_beneficial, error_response
# (V2b: the ``wire_view_from_scope`` import was removed with the v4-only
# teardown; D5 (2026-08-22) then made the §14 href face a constant 4 — see
# ``_expand_wire_view`` below.)
from ...skeleton import SkeletonLimits, skeleton_messages
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
    """§14 href view selector: constant 4 in BOTH readiness gate states.

    D5 adjudication (2026-08-22, owner): under the v4-only ``(4,4)``
    selector window the historical gate-off fold to 3 minted
    self-rejecting dead links — a v4 response's expandRefs href carried
    ``?v=3`` and the selector middleware 400-rejects exactly that value.
    The href face is therefore 4 regardless of
    ``messages.expand.v4 ∈ SATISFIED`` (that gate now decides only the
    capabilities face in versions.py). The readiness branch was removed:
    its sole consumer was this return value, threaded into the projection
    for href generation; the ``scope`` parameter is kept for the call
    sites' shape."""
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

def _created_sort_key(msg: dict) -> int | float:
    """Sort key: ``info.time.created`` ASC.

    Defaults to ``0`` for missing / malformed fields so degenerate upstream
    rows sort first under Python's stable sort instead of crashing the worker.
    """
    info = msg.get("info") if isinstance(msg, dict) else None
    if not isinstance(info, dict):
        return 0
    time_obj = info.get("time")
    if not isinstance(time_obj, dict):
        return 0
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
        return 0
    if isinstance(raw, (int, float)) and math.isfinite(raw):
        return raw
    return 0


def _parse_sort_project(
    body: bytes, *, limits: SkeletonLimits, sid: str | None = None,
    wire_view: int = 3,
) -> list[dict]:
    """Worker entry: parse + sort by ``info.time.created`` ASC + skeleton
    project (no serialization).

    Shared by the default pack worker (:func:`_project_list_sorted_and_pack`)
    and the L2-CD-2 merged path, which needs the projected dicts (to detect
    placeholder messages and later splice inlined fulls) before packing.

    ``sid`` is threaded through to ``skeleton_messages`` so the projection
    emits ``expandRefs`` (design-expand §5.2) — without it, lane A's refs
    never reach the wire and the merged ref candidate set is empty.

    v4 §14: ``wire_view`` is threaded by the ROUTE so every expandRefs href
    carries the request's view. Since the V2b default flip it is constant 4
    (``wire_view_from_scope`` no longer forks) — selector-less stacks emit
    the same v4 hrefs.

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
    return skeleton_messages(
        parsed, limits=limits, sid=sid, wire_view=wire_view,
    )


def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None, limits: SkeletonLimits,
    sid: str | None = None, wire_view: int = 3,
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

    v4 §14: ``wire_view`` is threaded to the projection so expandRefs hrefs
    carry the request's selector view (default 3 — historical bytes).
    """
    projected = _parse_sort_project(
        body, limits=limits, sid=sid, wire_view=wire_view,
    )
    return orjson.dumps(projected)


def _judge_pack_tail(
    identity: bytes, *, accept_encoding: str | None,
    if_none_match: str | None, rep_version: bytes | None,
) -> tuple[str | None, bytes | None, dict[str, str], str | None]:
    """F-201/F-271 off-loop response tail: 304 judgment + gzip + validator.

    The list/merged routes produce the envelope identity bytes in the
    transform worker, but their POST-projection tail (up to two full-body
    sha256 passes for the conditional judgment + gzip level-6 on the merged
    8 MiB worst case) used to run on the event loop, stalling every SSE
    heartbeat for tens to hundreds of milliseconds. This worker is that
    tail, executed via ``pool.offload`` — WITHOUT holding admission (a slot
    here would 503 requests that already finished projecting; see the
    design doc §1.3). Pure CPU; same functions, same order, same inputs as
    the historical inline tail → wire bytes are identical.

    Returns ``(verdict, encoded, coding_headers, etag_value)``:

    * ``verdict is None`` → serve 200: ``encoded``/``coding_headers`` ready,
      ``etag_value`` the validator of the coding actually carried (``None``
      when ``rep_version`` is None — ETag disabled, byte-identical legacy).
    * ``verdict == "*"`` → 304: compressed once to label the coding it
      would serve; ``etag_value`` is that coding's validator.
    * other ``verdict`` string → 304 echoing exactly that validator; zero
      compression happened.
    """
    verdict: str | None = None
    if rep_version is not None:
        verdict = etag_mod.judge_conditional(
            identity, if_none_match, rep_version,
            accept_encoding=accept_encoding,
        )
        if verdict == "*":
            _, c_headers = compress_if_beneficial(identity, accept_encoding)
            actual = (
                "gzip" if "Content-Encoding" in c_headers else "identity")
            return (
                "*", None, None,
                etag_mod.compute_etag(identity, actual, rep_version),
            )
        if verdict is not None:
            return verdict, None, None, verdict
    encoded, c_headers = compress_if_beneficial(identity, accept_encoding)
    etag_value: str | None = None
    if rep_version is not None:
        actual = (
            "gzip" if "Content-Encoding" in c_headers else "identity")
        etag_value = etag_mod.compute_etag(identity, actual, rep_version)
    return None, encoded, c_headers, etag_value


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
        # v4 §14: the expandRefs href ``?v=`` value is read on the event
        # loop (the worker threads have no scope access). D5 (2026-08-22):
        # constant 4 in BOTH gate states — a self-minted ``?v=3`` href is
        # 400-rejected by the v4-only selector.
        wire_view = _expand_wire_view(request.scope)
        projected: list[dict] | None = None
        identity: bytes | None = None
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
                            sid=sid, wire_view=wire_view,
                        )
                    else:
                        identity = await pool.offload(
                            _project_list_sorted_and_pack, body,
                            accept_encoding=accept_encoding,
                            limits=limits, sid=sid,
                            wire_view=wire_view,
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
            # §4.1 terminal (v3-only): the packed bare array is spliced
            # into the envelope verbatim BEFORE any validator work — the
            # envelope bytes ARE the canonical ETag input (§6.3). The
            # X-Next-Cursor header is retired (§1): the client reads
            # ``nextCursor`` from the cached envelope (§6.4).
            identity = messages_envelope_bytes(identity, next_cursor)
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
            # 版本 4（此前 wire_view=3 为金样冻结保留）；代价是一次性
            # validator 轮换（客户端全量重拉一轮，与 4.9.0 REP_VERSION
            # 轮换同类）。与 sessions 侧（sessions.py / _catalog_common.py）
            # 域标签一致。
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
            limit, before, mode, merged_mode=merged_mode,
        )
        if leased is not None:
            return leased
    projected: list[dict] | None = None
    identity: bytes | None = None
    try:
        # Admission BEFORE the upstream GET: this is the key fix. The prior
        # code buffered the entire upstream body and only then tried to
        # acquire the semaphore, so N concurrent clients could each buffer a
        # large body before any of them failed admission.
        async with pool:
            response = await _stream_upstream(
                request, f"/session/{sid}/message", params, directory,
            )
            next_cursor: str | None = None
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
                # v4 §14: view read on the loop, threaded to the projection
                # for view-correct expandRefs hrefs. D5 (2026-08-22):
                # constant 4 in BOTH gate states (self-minted ``?v=3``
                # would be 400-rejected by the v4-only selector).
                wire_view = _expand_wire_view(request.scope)
                try:
                    if merged_mode:
                        projected = await pool.offload(
                            _parse_sort_project, body,
                            limits=SkeletonLimits(
                                field_bytes=config.skeleton_inline_output_max_bytes,
                                message_bytes=config.skeleton_inline_output_max_message_bytes,
                                fingerprint=config.message_fingerprint_enabled,
                            ),
                            sid=sid, wire_view=wire_view,
                        )
                    else:
                        identity = await pool.offload(
                            _project_list_sorted_and_pack, body,
                            accept_encoding=request.headers.get("accept-encoding"),
                            limits=SkeletonLimits(
                                field_bytes=config.skeleton_inline_output_max_bytes,
                                message_bytes=config.skeleton_inline_output_max_message_bytes,
                                fingerprint=config.message_fingerprint_enabled,
                            ),
                            sid=sid, wire_view=wire_view,
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
        # §4.1 terminal (v3-only — same tail as the lease path above):
        # envelope splice before validator work; X-Next-Cursor retired
        # (§1) — the client reads ``nextCursor`` from the cached envelope
        # (§6.4).
        identity = messages_envelope_bytes(identity, next_cursor)
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
