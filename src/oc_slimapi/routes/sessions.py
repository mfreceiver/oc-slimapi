from __future__ import annotations

import sqlite3

import httpx
import orjson
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ..dbaux import (
    AuxiliaryUnavailableError,
    fetch_sessions_page,
    has_wildcard,
    normalized_search,
)
from ..dbaux.cursor import (
    InvalidCursorError,
    build_fingerprint,
    decode_cursor,
    encode_cursor,
    fingerprint_mismatch,
)
from ..directory import validate_directory
from ..envelope import sessions_envelope_v4
from ..errors import CodedHTTPException
from .. import etag as etag_mod
from .. import readiness as readiness_mod
from ..gzip_util import accepts_gzip, json_response
from ..selector import resolve_route_directory
from ..skeleton import (
    canonical_session_skeleton_v4,
    native_session_to_record,
    project_rows_to_v4_skeletons,
)
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import raise_upstream_status, raise_upstream_unavailable
from ._catalog_common import busy_response, read_upstream_response

router = APIRouter(prefix="/slimapi", tags=["sessions"])


# ---------------------------------------------------------------------------
# Upstream-fetch coalescing (traffic plan Batch 1 / A3, §3.x join-first).
#
# (The sessions-LIST lease family — ``_canonical_sessions_query`` /
# ``_fetch_sessions_raw`` / ``_finalize_sessions_response`` /
# ``_sessions_via_lease`` — was removed with the V2b src teardown: the
# v4 facade (``_sessions_v4``) has no lease path, and under the v4-only
# (4, 4) window every request runs the facade. The registry itself stays
# alive for sessions/STATUS below, which keeps its own lease flight.)
#
# sessions/status shares ONLY the upstream GET (+ status mapping): the
# TurnRegistry merge stays per-caller. A full registry budget
# (``fetch_or_bypass`` → ``None``) falls back to the unchanged direct
# path.
# ---------------------------------------------------------------------------


async def _fetch_status_raw(
    request: Request, params: dict, directory: str | None,
) -> bytes:
    """Shared factory body: ONE upstream ``GET /session/status`` (including
    the status mapping — a 5xx fails the flight for every joiner)."""
    try:
        response = await request.app.state.upstream.get(
            "/session/status",
            params=params,
            headers=forward_directory_headers(directory),
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    stash_up_in(request, len(response.content))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc)
    return response.content


async def _status_via_lease(
    request: Request, registry, directory: str | None,
):
    """Join-first lease path for sessions/status. The TurnRegistry merge is
    deliberately OUTSIDE the factory (plan A3): turn state changes with
    time, so every caller merges the CURRENT registry state into the shared
    body — never frozen at factory time."""
    async def _factory() -> bytes:
        # §5.2 terminal (M3-2): header-only — identical to the direct path
        # below; ``directory`` never travels as an upstream query param.
        return await _fetch_status_raw(request, {}, directory)

    lease = await registry.fetch_or_bypass(
        ("sessions-status", id(request.app.state.upstream), directory),
        _factory,
        reserve_bytes=request.app.state.config.max_response_bytes,
    )
    if lease is None:
        return None
    async with lease:
        body = lease.body  # bytes are immutable — safe to parse post-release
    try:
        payload = orjson.loads(body)
    except (orjson.JSONDecodeError, ValueError) as exc:
        raise_upstream_unavailable(exc)
    if not isinstance(payload, dict):
        raise_upstream_unavailable()
    # Per-caller turn merge — identical to the direct path (contract §3.y.1).
    turn_registry = getattr(request.app.state, "turn_registry", None)
    if turn_registry is not None:
        for sid, info in payload.items():
            if isinstance(info, dict):
                inc, turn = turn_registry.snapshot(sid)
                info["turnIncarnation"] = inc
                info["turn"] = turn
    return json_response(
        payload,
        accept_encoding=request.headers.get("accept-encoding"),
    )


# ---------------------------------------------------------------------------
# v4 global sessions facade (B3a-B4; v4-contract §4 / design-v4-dbaux §7).
#
# The v3 path below is frozen byte-identical; the v4 fork deliberately
# DUPLICATES the upstream-fetch call shape instead of refactoring the
# shared v3 helpers (zero-touch rule for the v3 pipeline).
# ---------------------------------------------------------------------------

_V4_ARCHIVED_STATES = ("omit", "only", "all")
_V4_LIMIT_MAX = 500  # §4.1 v4 domain (v3 keeps 1000)
_AUX_RETRY_AFTER = "30"  # §4.2: same order as the breaker recovery probe

_AUX_UNAVAILABLE_HINT = (
    "session projection is temporarily served from a degraded source; "
    "retry shortly"
)


def _raw_query_keys(request: Request) -> set[str]:
    """Keys present in the (post-selector-strip) raw query string.

    Presence-based (blank values count) — parameter-version policing must
    not depend on FastAPI's value-typed defaults (§4.1 S-B04: explicit
    declaration, no reliance on framework-silent ignoring).
    """
    from urllib.parse import parse_qsl

    raw = request.scope.get("query_string", b"") or b""
    try:
        return {key for key, _ in parse_qsl(raw.decode("latin-1"),
                                            keep_blank_values=True)}
    except Exception:  # pragma: no cover — parse_qsl on latin-1 cannot fail
        return set()


def _aux_unavailable() -> CodedHTTPException:
    # §4.2: flat body, no DB path / schema / allowlist detail leak (§8).
    return CodedHTTPException(
        503,
        code="auxiliary_unavailable",
        hint=_AUX_UNAVAILABLE_HINT,
        headers={"Retry-After": _AUX_RETRY_AFTER},
    )


def _fail_closed_503(request: Request) -> CodedHTTPException:
    """503 fail-closed 统一出口（rev gate：R2 观测泳道接口约定）。

    写 ``request.state.slimapi_degraded_503 = True``——R2 在 access log /
    snapshot / metrics 消费；Class A 降级 200 不置位（那是
    ``slimapi_sessions_source="http"`` 的辖区）。v3 路径两字段恒缺席。
    """
    request.state.slimapi_degraded_503 = True
    return _aux_unavailable()


def _http_session_to_v4(item: dict) -> dict:
    """Upstream session JSON (SessionInfo camelCase) → SessionSkeletonV4.

    Degraded-path projection (§4.2 Class A). Since D2-A the upstream call
    targets ``/experimental/session`` (sessions.listGlobal), whose rows DO
    carry a ``project`` join — but ``project`` stays frozen to ``None``:
    the gate-closed 4.0.0 published degraded item form is preserved
    byte-for-byte (the weakness is flagged by ``degraded: true``; the
    gate-open canonical projection ``native_session_to_record`` remains
    the schema authority). The listGlobal row is a superset of the legacy
    ``/session`` row, so the pick-list parser needs no shape adaptation.
    Tokens map from the nested HTTP shape to the flat real-DB column
    names (R2 freeze).
    """
    def _pick(source: dict, *keys: str):
        return {key: source[key] for key in keys if key in source}

    skeleton: dict = _pick(
        item, "id", "directory", "parentID", "projectID", "title", "agent",
        "model",
    )
    time_obj = item.get("time")
    if isinstance(time_obj, dict):
        skeleton["time"] = _pick(time_obj, "created", "updated", "archived")
    summary_obj = item.get("summary")
    if isinstance(summary_obj, dict):
        skeleton["summary"] = _pick(summary_obj, "additions", "deletions", "files")
    tokens_obj = item.get("tokens")
    if isinstance(tokens_obj, dict):
        cache_obj = tokens_obj.get("cache")
        cache = cache_obj if isinstance(cache_obj, dict) else {}
        skeleton["tokens_input"] = tokens_obj.get("input")
        skeleton["tokens_output"] = tokens_obj.get("output")
        skeleton["tokens_reasoning"] = tokens_obj.get("reasoning")
        skeleton["tokens_cache_read"] = cache.get("read")
        skeleton["tokens_cache_write"] = cache.get("write")
    revert_obj = item.get("revert")
    if isinstance(revert_obj, dict):
        skeleton["revert"] = _pick(revert_obj, "messageID", "partID")
    skeleton["project"] = None
    return skeleton


def _v4_allowlist_entries(request: Request) -> tuple[str, ...]:
    """Non-empty allowlist entries for the SQL predicate / fingerprint.

    Empty-string entries are config noise (cannot form a subtree
    predicate — same rule as ``allowlist_rev``); dropped here so the
    B2 assembler never sees them. ``None`` (unset) and ``[]`` (explicit
    empty) both mean "no allowlist axis" (three-state, P1 B4-4).
    """
    configured = request.app.state.config.directory_allowlist
    if not configured:
        return ()
    return tuple(entry for entry in configured if entry)


async def _sessions_v4(
    request: Request,
    *,
    roots: bool,
    limit: int,
    start: int | None,
    search: str | None,
    archived: str | None,
    parent: str | None,
    cursor: str | None,
) -> Response:
    # ---- ④ 参数版本不匹配（§8.3：先于 invalid_cursor/503） --------------
    raw_keys = _raw_query_keys(request)
    if "roots" in raw_keys or "start" in raw_keys:
        raise CodedHTTPException(
            422, code="param_version_mismatch",
            hint="roots/start are v3-only; v4 uses the parent filter axis",
        )
    if limit > _V4_LIMIT_MAX:
        raise CodedHTTPException(
            422, code="param_version_mismatch",
            hint=f"v4 limit domain is 1..{_V4_LIMIT_MAX}",
        )
    if archived is not None and archived not in _V4_ARCHIVED_STATES:
        raise CodedHTTPException(
            422, code="param_version_mismatch",
            hint=f"archived must be one of {_V4_ARCHIVED_STATES}",
        )
    if parent is not None and not parent:
        raise CodedHTTPException(
            422, code="param_version_mismatch", hint="parent must not be empty",
        )

    archived_state = archived or "omit"
    parent_state = parent or "all"
    normalized = normalized_search(search)
    allowlist = _v4_allowlist_entries(request)

    # ---- ⑤ invalid_cursor 400 优先于 503（§8.3；纯内存校验） -----------
    fingerprint = build_fingerprint(
        archived=archived, parent=parent, search=search, allowlist=allowlist,
    )
    try:
        cursor_payload = decode_cursor(cursor)
    except InvalidCursorError:
        raise CodedHTTPException(
            400, code="invalid_cursor",
            hint="cursor is malformed; restart pagination from the first page",
        )
    if cursor_payload is not None and fingerprint_mismatch(
        cursor_payload.f, fingerprint
    ):
        raise CodedHTTPException(
            400, code="invalid_cursor",
            hint="cursor filter context does not match this request; "
                 "restart pagination from the first page",
        )

    # ---- ⑥ 降级矩阵（§4.2 formula；db∈{disabled,tripped} 同形） --------
    dbaux = getattr(request.app.state, "dbaux", None)
    if dbaux is not None and dbaux.status().available:
        try:
            page = await fetch_sessions_page(
                dbaux,
                archived=archived_state,
                parent=parent_state,
                search=normalized,
                cursor=(cursor_payload.t, cursor_payload.i)
                if cursor_payload is not None else None,
                limit=limit,
                allowlist=allowlist,
            )
        except AuxiliaryUnavailableError:
            pass  # raced into unavailable between status and query → degrade
        except sqlite3.Error:
            # rev gate BLOCKER-1：busy 族及其他查询期 sqlite 异常（lifecycle
            # busy 分类原样上抛、不禁用连接）在路由边界统一转
            # auxiliary_unavailable 语义 → 503 fail-closed；不泄露 SQLite
            # 细节（§4.2），延迟已由 lifecycle 计入熔断器画像。
            raise _fail_closed_503(request) from None
        else:
            if _v4_session_single_revision_active():
                # §13 修订面（§13.1/§13.3）：items 经唯一 canonical
                # projector 装配（partial/degraded 标记 + required
                # nullable 恒发）；§13.2c——不可表示项不混入，整响应
                # fail-closed 503。
                items = []
                for record in page.records:
                    item = canonical_session_skeleton_v4(record)
                    if item is None:
                        raise _fail_closed_503(request) from None
                    items.append(item)
            else:
                # 门控关：4.0.0 已发布 item 形态逐字节保留
                items = project_rows_to_v4_skeletons(page.records)
            # BLOCKER-3：nextCursor 用**原始窗口锚点**（坏行不丢锚点——
            # items 可为空仍可前进；仅在 complete:false 且锚点存在时编码）。
            next_cursor = None
            if not page.complete and page.anchor is not None:
                next_cursor = encode_cursor(
                    page.anchor[0], page.anchor[1], fingerprint,
                )
            request.state.slimapi_sessions_source = "db"
            revision_active = _v4_session_single_revision_active()
            # §13.4 公式：envelope.degraded == any(item.degraded) ∨ fallback
            # （DB 常态路径无 fallback 位 → 纯 item 聚合：orphan join 失败
            # 等 partial item 聚合为 true）。门控关：4.0.0 稀疏形态
            # （degraded 键省略）逐字节保留——items 无标记键，短路不求值。
            return _v4_json_response(
                sessions_envelope_v4(
                    items, next_cursor, page.complete,
                    degraded=(revision_active
                              and any(item["degraded"] for item in items)),
                    degraded_required=revision_active,
                ),
                request,
            )
    # DB unavailable (or raced) → §4.2 degradation formula
    if allowlist:
        # fail-closed：白名单 ⊆ 结果集不可由上游保证（ora B-2 选②）
        raise _fail_closed_503(request)
    if has_wildcard(normalized):
        # %/_/\ 无法等价表达——过滤语义永不降级（§4.6 收窄）
        raise _fail_closed_503(request)
    if cursor_payload is not None:
        # 上游单键 cursor 无法兑现 (t,i) keyset 指纹
        raise _fail_closed_503(request)
    class_a = (
        archived_state in ("omit", "all") and parent_state in ("all", "none")
    )
    if not class_a:
        raise _fail_closed_503(request)

    # ---- Class A 200 + degraded:true（HTTP 降级） -----
    # 上游端点 = GET /experimental/session（契约 §4.2 指定的 schema 权威
    # 与降级路径）。此前误用 /session：上游该路由走 listByProject——恒含
    # eq(project_id) 且无 time_archived 过滤，降级响应会混入 archived
    # 会话、范围塌缩到单一 project，违约「过滤语义永不降级」。
    # /experimental/session 走 sessions.listGlobal：archived 参数缺省
    # （= omit）即追加 isNull(time_archived)，orderBy (time_updated DESC,
    # id DESC)，全局范围——降级响应与 DB 常态投影的过滤/排序语义一致。
    params: dict[str, object] = {"limit": limit}
    if parent_state == "none":
        params["roots"] = "true"  # §4.2: parent=none → roots=true 透传
    # archived 按状态透传（D2-A + rev-sgpt 终审补丁 2026-08-22）：
    # omit（默认态）→ 不传参——listGlobal 缺省追加 isNull(time_archived)
    # 即契约过滤语义；all → 显式 archived=true——上游仅在真值时包含
    # archived（session.ts:564 `if (!input?.archived)`），缺参必然排除，
    # 故 all 必须传真值（初版漏传致 all 退化 omit，违约「过滤语义永不
    # 降级」§4.2）。only 态在 class_a 判定已 fail-closed，不可达此。
    if archived_state == "all":
        params["archived"] = "true"
    if normalized is not None:
        params["search"] = normalized  # 第四消费点：降级传 normalized
    config = request.app.state.config
    try:
        async with request.app.state.transforms as pool:
            try:
                    response = await request.app.state.upstream.send(
                        request.app.state.upstream.build_request(
                            "GET", "/experimental/session",
                            params=params, headers=forward_directory_headers(None),
                        ),
                        stream=True,
                    )
            except httpx.RequestError as exc:
                raise_upstream_unavailable(exc)
            try:
                body = await read_upstream_response(
                    request, response,
                    cap=config.max_response_bytes,
                    read_with_cap=read_with_cap,
                )
                if body is None:
                    raise CodedHTTPException(
                        413, code="response_too_large",
                        limit=config.max_response_bytes,
                    )
                try:
                    payload = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise_upstream_unavailable(exc)
                if not isinstance(payload, list) or (
                    payload and not all(isinstance(s, dict) for s in payload)
                ):
                    raise_upstream_unavailable()
                if _v4_session_single_revision_active():
                    # §13 修订面：fallback items 同经唯一 canonical
                    # projector（§13.3——native 归一化后装配，非第二投影）
                    items = await pool.offload(
                        _project_http_sessions_v4_canonical, payload,
                    )
                else:
                    items = await pool.offload(
                        _project_http_sessions_v4, payload,
                    )
            finally:
                await response.aclose()
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
    # complete best-effort（§4.2 degraded 语义：上游无 LIMIT+1 窗口）；
    # nextCursor 恒 null——降级页无法用 (t,i) keyset 续读（cursor → 503）。
    complete = len(items) < limit
    # R2 观测接口（rev gate）：降级 200 标记数据面来源。
    request.state.slimapi_sessions_source = "http"
    return _v4_json_response(
        sessions_envelope_v4(
            items, None, complete,
            degraded=True,
            degraded_required=_v4_session_single_revision_active(),
        ),
        request,
    )


_V4_REPRESENTATION_FEATURE = "representation.vary.v4"

_V4_SESSION_SINGLE_FEATURE = "session.single.projection.v4"


def _v4_session_single_revision_active() -> bool:
    """§3.3 门控：``session.single.projection.v4 ∈ SATISFIED`` 时 §13
    修订面生效（单查 canonical + 列表 item/envelope canonical 形状——
    同 feature 接线，read_groups 单查路由共用同 ID）。

    调用时读模块全局（与 versions.py 同款动态读法）——readiness 翻转
    无需改本文件即可 wire 级生效；未 satisfied 时 v4 sessions 维持
    4.0.0 已发布行为（item 无标记 / envelope 稀疏 degraded）。
    """
    return _V4_SESSION_SINGLE_FEATURE in readiness_mod.SATISFIED


def _v4_representation_revision_active() -> bool:
    """§3.3 门控：``representation.vary.v4 ∈ SATISFIED`` 时 §15 修订面生效。

    调用时读模块全局（与 versions.py 同款动态读法）——readiness 翻转
    无需改本文件即可 wire 级生效；未 satisfied 时 v4 sessions 维持
    4.0.0 已发布行为（§4.4：无 ETag / 无 Vary / INM 不判定）。
    """
    return _V4_REPRESENTATION_FEATURE in readiness_mod.SATISFIED


def _v4_json_response(payload: dict, request: Request) -> Response:
    """v4 sessions 200/304 响应尾（§4.4 门控关闭态 / §15 修订冻结态）。

    调用点在 DB 投影与 Class A 降级判定**之后**（两路径共用本尾）——
    ETag 管线不短路：条件请求照常 fresh 计算，最后才判 304（§15）。

    - 门控关（§3.3，4.0.0 已发布行为逐字保留）：无 ETag，摘 Vary。
    - 门控开（§15）：``Vary: Accept-Encoding`` 恒在（修摘除 bug）；
      200/304 均 ``Cache-Control: no-store``；ETag = sha256(
      REP_VERSION + NUL + coding + NUL + canonical identity bytes)，
      identity 强 ``"…"`` / gzip 弱 ``W/"…"``（coding 派生）；
      ``If-None-Match`` 弱比较命中 → 304（头集合 = ETag + Vary +
      no-store；envelope 自含 nextCursor/complete，无 aux 头）。
      REP_VERSION 经 ``wire_view=4`` 与 v3 validator 域隔离；降级
      200（degraded:true）无 §15 例外条款——canonical = 降级 envelope
      body，同样发 ETag/Vary。
    - ``OC_SLIMAPI_ETAG_ENABLED=false`` → 无 ETag / 无 304 判定，但
      Vary（与 no-store）仍发——表示可变性与 ETag 正交（§12.6 口径）。
    """
    accept_encoding = request.headers.get("accept-encoding")
    if not _v4_representation_revision_active():
        # 4.0.0 已发布行为（§4.4 冻结，门控关闭态）。
        response = json_response(payload, accept_encoding=accept_encoding)
        if "Vary" in response.headers:
            del response.headers["Vary"]
        return response

    vary = etag_mod.merged_vary("Accept-Encoding")
    rep = etag_mod.response_rep_version(
        request.app.state.config, wire_view=4)
    if rep is None:
        # etag 关闭：无 validator / 无 304；Vary 恒发。
        response = json_response(payload, accept_encoding=accept_encoding)
        response.headers["Vary"] = vary
        response.headers["Cache-Control"] = "no-store"
        return response

    # canonical identity bytes 即 wire body（json_response 内部对同一
    # payload dict 再跑一次 orjson.dumps——确定性 key 序，两次字节相同；
    # 与 v3 _finalize_sessions_response 同款代价口径）。
    identity = orjson.dumps(payload)
    coding = "gzip" if accepts_gzip(accept_encoding) else "identity"
    etag_value = etag_mod.compute_etag(identity, coding, rep)
    not_modified = etag_mod.conditional_304(
        {"ETag": etag_value, "Vary": vary},
        request.headers.get("if-none-match"),
    )
    if not_modified is not None:
        return not_modified
    response = json_response(payload, accept_encoding=accept_encoding)
    response.headers["ETag"] = etag_value
    response.headers["Vary"] = vary
    response.headers["Cache-Control"] = "no-store"
    return response


def _project_http_sessions_v4(payload: list[dict]) -> list[dict]:
    """Worker-thread entry: degraded-path HTTP → SessionSkeletonV4."""
    return [_http_session_to_v4(item) for item in payload]


def _project_http_sessions_v4_canonical(payload: list[dict]) -> list[dict]:
    """Worker-thread entry：§13 修订面 fallback → canonical items。

    native item 先经 ``native_session_to_record`` 归一化（键 presence =
    三态载体），再喂唯一 canonical projector（§13.3 同一 projector）；
    任一 item 不可表示（§13.2a）→ 抛 503，整响应 fail-closed（§13.2c
    禁不可表示项混入 items）。
    """
    items: list[dict] = []
    for source in payload:
        item = canonical_session_skeleton_v4(
            native_session_to_record(source), fallback=True,
        )
        if item is None:
            raise _aux_unavailable()
        items.append(item)
    return items


@router.get("/sessions")
async def sessions(
    request: Request,
    directory: str | None = None,
    roots: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    start: int | None = Query(None, ge=0),
    search: str | None = None,
    archived: str | None = None,
    parent: str | None = None,
    cursor: str | None = None,
):
    # v4 facade (B3a-B4, v4-contract §4) — unconditional under the v4-only
    # (4, 4) window: the selector stashes "4" for admitted ``?v=4`` requests
    # and every other scope observes the V2b-flipped default 4, so the wire
    # view is structurally always 4. (The historical v3 leg below the fork —
    # directory resolution, the lease/coalesce join, the raw ``GET /session``
    # pipeline and the ``{"items": …, "complete": …}`` v3 envelope — was
    # removed with the V2b src teardown; ``directory`` never reaches here
    # anyway — the selector retires it pre-route, §5.2.)
    return await _sessions_v4(
        request, roots=roots, limit=limit, start=start, search=search,
        archived=archived, parent=parent, cursor=cursor,
    )


@router.get("/sessions/status")
async def sessions_status(request: Request, directory: str | None = None):
    """GET /slimapi/sessions/status?directory=<optional>.

    Additive re-add (lite-v2 originally deleted this; brought back as a
    read-only projection). Passthrough of upstream opencode
    ``GET /session/status`` (returns ``Record<SessionID, {type:"busy"|
    "idle"|"retry"}>``) with a sidecar merge of the turn-token fence
    fields (``turnIncarnation``/``turn``) per sid from
    :class:`TurnRegistry`. No caching, no new state — same in-memory
    sources the digest SSE already stamps from (contract §3.y).

    ``directory`` is OPTIONAL (additive). Upstream ``GET /session/status``
    ignores ``directory`` entirely — its handler takes no args and
    ``statusSvc.list()`` returns the full in-memory ``Map<SessionID, Info>``
    regardless (the param exists only for ``WorkspaceRoutingMiddleware``
    routing). So this endpoint always returns the GLOBAL status map no
    matter what directory is (or isn't) supplied; callers SHOULD omit
    ``directory`` and call once for the whole map (see
    ``docs/ocmar/specs/2026-08-05-s4-batch-status-research.md``). When
    supplied, it is validated + forwarded as the ``X-Opencode-Directory``
    header ONLY (§5.2 terminal — same channel on the coalesced lease path
    and the direct path).
    """
    # v3 (§5, Batch B): stash substitutes the stripped query param (see
    # the sessions-list handler above).
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    params: dict[str, str] = {}
    # §5.2 terminal (v3-only): canonical header only — see the
    # sessions-list handler above.
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    if registry is not None and request.app.state.config.coalesce_enabled:
        leased = await _status_via_lease(request, registry, directory)
        if leased is not None:
            return leased
        # budget full → unchanged direct path below
    try:
        response = await request.app.state.upstream.get(
            "/session/status",
            params=params,
            headers=forward_directory_headers(directory),
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    stash_up_in(request, len(response.content))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc)
    try:
        payload = response.json()
    except Exception as exc:
        raise_upstream_unavailable(exc)
    if not isinstance(payload, dict):
        # Upstream contract is Record<SessionID, Info> — a non-dict body is
        # malformed. Mirrors the sessions-list non-array guard (503).
        raise_upstream_unavailable()
    # Read-only turn merge (contract §3.y.1: paired turnIncarnation/turn at
    # the flat top level of each entry). Unobserved sid → (inc, 0). The
    # registry is lifespan-wired in production; when absent both fields are
    # omitted (paired missing → ocdroid Tier-2 degrade). Entries whose value
    # is not a dict (upstream schema violation) are passed through unchanged.
    turn_registry = getattr(request.app.state, "turn_registry", None)
    if turn_registry is not None:
        for sid, info in payload.items():
            if isinstance(info, dict):
                inc, turn = turn_registry.snapshot(sid)
                info["turnIncarnation"] = inc
                info["turn"] = turn
    return json_response(
        payload,
        accept_encoding=request.headers.get("accept-encoding"),
    )



