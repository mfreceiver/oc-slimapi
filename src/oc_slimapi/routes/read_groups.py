"""v3-contract §10.a read-group routes (Batch C1).

Seven annexed read groups as thin controlled proxies (no byte slimming —
governance only: selector ``v``/``directory`` consumption, cap, ETag,
gzip, structured errors):

=========  =====================================  =========================
Group      Route                                   Upstream (opencode)
=========  =====================================  =========================
file       GET /slimapi/file                       GET /file
file       GET /slimapi/file/content               GET /file/content
file       GET /slimapi/file/status                GET /file/status
vcs        GET /slimapi/vcs                        GET /vcs
vcs        GET /slimapi/vcs/status                 GET /vcs/status
vcs        GET /slimapi/vcs/diff                   GET /vcs/diff
find       GET /slimapi/find/file                  GET /find/file
providers  GET /slimapi/config/providers           GET /config/providers
session    GET /slimapi/session/{sid}              GET /session/{sid}
active     GET /slimapi/api/session/active         GET /api/session/active
global     GET /slimapi/global/health              GET /global/health
=========  =====================================  =========================

Upstream anchors (opencode v1.18.16, ``packages/opencode/src/server/routes/
instance/httpapi/``): ``groups/file.ts`` (FileQuery / FindFileQuery /
FilePaths; all file-group endpoints under ``WorkspaceRoutingMiddleware`` →
directory-sensitive), ``groups/instance.ts:49-60`` (vcs group,
``WorkspaceRoutingQuery``/``VcsDiffQuery`` → directory-sensitive),
``groups/config.ts:38-40`` (providers, ``WorkspaceRoutingQuery`` →
directory-sensitive), ``groups/session.ts:132-137`` (session single,
``WorkspaceRoutingQuery`` → directory-sensitive), protocol
``groups/session.ts:146-152`` (``/api/session/active`` — no query →
directory tolerant), ``groups/global.ts:76-80`` (``/global/health`` — no
query → directory tolerant).

Session single is the one projecting route: the upstream ``Session.Info``
is whitelisted through the exact same ``skeleton_session`` used by
``GET /slimapi/sessions`` (contract: "投影=skeleton_session 同款白名单").
"""

from __future__ import annotations

import sqlite3

import httpx
import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import etag as etag_mod
from .. import readiness as readiness_mod
from ..config import allowlist_roots, candidate_canonical, match_allowlist
from ..directory import validate_directory
from ..dbaux import (
    PROJECT_JOIN_COLUMNS,
    SESSION_PROJECTION_COLUMNS,
    AuxiliaryUnavailableError,
    rows_to_records,
)
from ..errors import CodedHTTPException
from ..gzip_util import error_response, json_response
from ..providers_projection import (
    ProviderProjectionLimit,
    ProviderUpstreamMalformed,
    project_and_pack,
    providers_rep_version,
)
from ..selector import (
    DIRECTORY_QUERY_PARAM,
    _strip_query_keys,
    resolve_route_directory,
    wire_view_from_scope,
)
from ..skeleton import (
    canonical_session_skeleton_v4,
    native_session_to_record,
    skeleton_session,
)
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ..upstream_errors import UPSTREAM_UNAVAILABLE, raise_upstream_unavailable
from ._catalog_common import busy_response, stream_upstream
from ._read_passthrough import (
    _raw_upstream_url,
    _read_error_body,
    _upstream_passthrough_headers,
    read_passthrough_get,
)
from .sessions import _aux_unavailable

router = APIRouter(prefix="/slimapi", tags=["read-groups"])


def _resolve(request: Request, directory: str | None) -> str | None:
    """Resolve the workspace directory for a read-group route.

    v3 (§5.2): the selector already consumed ``?directory=`` (query or
    compatible header) into the scope stash and stripped the query —
    ``resolve_route_directory`` returns that stashed value (validated).

    v2 (§5.2 frozen): the query — ``directory`` included — is forwarded
    VERBATIM in the upstream URL and is deliberately NOT consumed here.
    The v2 client's real channel is the ``X-Opencode-Directory`` header:
    bind + validate it (thin routes do not auto-forward client headers),
    or ``None`` when absent.
    """
    resolved = resolve_route_directory(request.scope, None)
    if resolved is not None:
        return validate_directory(resolved)
    header_dir = request.headers.get("x-opencode-directory")
    if header_dir:  # treat empty header as absent
        return validate_directory(header_dir)
    return None


def _project_session(raw: bytes) -> bytes:
    """skeleton_session whitelist for session single (malformed → 503)."""
    session = orjson.loads(raw)
    if not isinstance(session, dict):
        raise ValueError("session single payload is not an object")
    return orjson.dumps(skeleton_session(session))


def _authorized_file_directory(request: Request, directory: str | None) -> str | None:
    """403 gate returning the directory to forward upstream (rev-2 closure).

    * allowlist ``None`` → ungated: the original ``directory`` is returned
      verbatim (zero behaviour change).
    * allowlist set → the candidate is canonicalised in REALTIME (never
      cached — rev-2 sub-1), relative candidates fail closed with the
      uniform 403 ``directory_not_allowed`` (sub-2; no existence leak),
      and on a pass the CANONICAL (realpath) form is returned so the
      forwarded ``X-Opencode-Directory`` binds the upstream access to the
      exact object the authorization decision was made on (sub-3) — a
      symlink swapped between check and upstream resolution cannot
      retarget the lookup.
    """
    allowlist = request.app.state.config.directory_allowlist
    if allowlist is None:
        return directory
    canonical = candidate_canonical(directory)
    if not match_allowlist(allowlist_roots(allowlist), canonical):
        raise CodedHTTPException(403, code="directory_not_allowed")
    return canonical


# --- file group (FileQuery: workspace routing + path) ----------------------


@router.get("/file")
async def file_list(request: Request, path: str,
                    directory: str | None = None):
    """Upstream ``GET /file`` — ``LegacyEntry[]`` for the given path.

    §5.2: the raw query (post-selector ``v``/``directory`` fork) is
    forwarded verbatim — ``path`` travels in the URL bytes, unknown
    params/repeats/encodings included. The declaration keeps the 422 on a
    missing ``path``."""
    resolved = _resolve(request, directory)
    forward_directory = _authorized_file_directory(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file",
        directory=forward_directory,
    )


@router.get("/file/content")
async def file_content(request: Request, path: str,
                       directory: str | None = None):
    """Upstream ``GET /file/content`` — ``LegacyContent`` (text|binary)."""
    resolved = _resolve(request, directory)
    forward_directory = _authorized_file_directory(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file/content",
        directory=forward_directory,
    )


@router.get("/file/status")
async def file_status(request: Request, directory: str | None = None):
    """Upstream ``GET /file/status`` — ``LegacyStatus[]``."""
    resolved = _resolve(request, directory)
    forward_directory = _authorized_file_directory(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file/status",
        directory=forward_directory,
    )


# --- vcs group (WorkspaceRoutingQuery / VcsDiffQuery) ----------------------


@router.get("/vcs")
async def vcs_info(request: Request, directory: str | None = None):
    """Upstream ``GET /vcs`` — ``Vcs.Info``."""
    return await read_passthrough_get(
        request, upstream_path="/vcs",
        directory=_resolve(request, directory),
    )


@router.get("/vcs/status")
async def vcs_status(request: Request, directory: str | None = None):
    """Upstream ``GET /vcs/status`` — ``Vcs.FileStatus[]``."""
    return await read_passthrough_get(
        request, upstream_path="/vcs/status",
        directory=_resolve(request, directory),
    )


@router.get("/vcs/diff")
async def vcs_diff(request: Request, directory: str | None = None,
                   mode: str | None = None, context: int | None = None):
    """Upstream ``GET /vcs/diff`` — ``Vcs.FileDiff[]`` (VcsDiffQuery:
    ``mode`` + optional ``context``). §5.2: the raw query is forwarded
    verbatim — ``mode``/``context`` travel in the URL bytes, and absent
    params stay absent (the upstream's own validation surface; its 400
    passes through verbatim)."""
    return await read_passthrough_get(
        request, upstream_path="/vcs/diff",
        directory=_resolve(request, directory),
    )


# --- find (FindFileQuery: workspace routing + query/dirs/type/limit) -------


@router.get("/find/file")
async def find_file(request: Request, query: str,
                    directory: str | None = None,
                    dirs: str | None = None,
                    type: str | None = None,  # noqa: A002 — upstream name
                    limit: int | None = None):
    """Upstream ``GET /find/file`` — ``string[]`` of matching paths.

    §5.2: the raw query is forwarded verbatim (``query``/``dirs``/``type``/
    ``limit`` travel in the URL bytes); the declaration keeps the 422 on a
    missing ``query``."""
    return await read_passthrough_get(
        request, upstream_path="/find/file",
        directory=_resolve(request, directory),
    )


# --- providers (groups/config.ts:38-40, WorkspaceRoutingQuery) -------------


async def _handle_providers_v4(request: Request,
                               directory: str | None) -> Response:
    """§12 provider safe projection pipeline (``?v=4`` only).

    Twelve-step evaluation order is FROZEN (v4-contract §12.5.2) — the
    numbering below cites the contract steps:

    * ③ upstream fetch + status mapping (async I/O, NO transform permit
      held): network error → 503 ``upstream_unavailable``; non-200 →
      drain the error body cap-protected (drain failure / over-cap →
      503; ``max_response_bytes`` runtime value, ``>`` boundary) then
      5xx → 503 ``upstream_unavailable``, 3xx/4xx → 502
      ``upstream_http_<N>``, other 2xx (incl 204) → 502
      ``provider_upstream_malformed``;
    * ④ source-body byte cap BEFORE parse (``read_with_cap``) → 413
      ``response_too_large`` with the §12.5.3 ``limitBytes`` field;
    * ⑤ transform permit acquired AFTER the body-cap check and BEFORE
      the worker submit — the network wait above never holds a permit,
      so 503 ``transform_busy`` is only possible at this submit point;
    * ⑥-⑪ ONE worker job (:func:`project_and_pack` — strict decode,
      validation, projection, count limits, canonical serialization,
      body-byte limit, gzip + ETag derivation, all off the event loop);
    * ⑫ conditional judgment + emission in THIS (main) context — zero
      serialization/compression here, only the ``If-None-Match`` compare
      against the worker-produced validator.

    All error responses carry ``Cache-Control: no-store`` (§12.5.3).
    """
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")
    if_none_match = request.headers.get("if-none-match")
    rep_version = providers_rep_version(config)
    pool = request.app.state.transforms
    upstream_url = _raw_upstream_url(request, "/config/providers")

    def _v4_error(code: str, status: int, **fields) -> Response:
        response = error_response(
            code, status, accept_encoding=accept_encoding, **fields)
        response.headers["Cache-Control"] = "no-store"
        return response

    # ③ fetch + status mapping — permit NOT held during the network wait.
    try:
        response = await stream_upstream(request, upstream_url, directory)
    except httpx.RequestError as exc:
        raise CodedHTTPException(
            503, code=UPSTREAM_UNAVAILABLE,
            headers={"Cache-Control": "no-store"}) from exc
    try:
        status = response.status_code
        if status != 200:
            # Drain for connection reuse; the drain itself is
            # cap-protected (over-cap / read failure → 503, §12.5.1).
            # §12.5.3 "全部错误 no-store": the shared helpers raise
            # CodedHTTPException WITHOUT Cache-Control — stamp it here so
            # every raised error of this route carries no-store without
            # touching the shared pipeline modules.
            try:
                await _read_error_body(request, response)
            except CodedHTTPException as exc:
                exc.headers = {**(exc.headers or {}),
                               "Cache-Control": "no-store"}
                raise
            if status >= 500:
                raise CodedHTTPException(
                    503, code=UPSTREAM_UNAVAILABLE,
                    headers={"Cache-Control": "no-store"})
            if 200 < status < 300:
                raise CodedHTTPException(
                    502, code="provider_upstream_malformed",
                    headers={"Cache-Control": "no-store"})
            raise CodedHTTPException(
                502, code=f"upstream_http_{status}",
                headers={"Cache-Control": "no-store"})
        # ④ source body cap — BEFORE any parse (a big malformed body is
        # still 413, not 502).
        try:
            body, _total = await read_with_cap(
                response, config.max_response_bytes,
                on_read=lambda n: stash_up_in(request, n))
        except httpx.RequestError as exc:
            raise CodedHTTPException(
                503, code=UPSTREAM_UNAVAILABLE,
                headers={"Cache-Control": "no-store"}) from exc
        if body is None:
            return _v4_error(
                "response_too_large", 413,
                limitBytes=config.max_response_bytes)
    finally:
        await response.aclose()

    # ⑤ permit — only now; ⑥-⑪ the single offloaded worker job.
    try:
        async with pool:
            encoded, extra = await pool.offload(
                project_and_pack, body,
                accept_encoding=accept_encoding,
                rep_version=rep_version,
            )
    except TransformBusy:
        # §12.5.3: stamp no-store onto the shared busy body too.
        busy = busy_response(accept_encoding)
        busy.headers["Cache-Control"] = "no-store"
        return busy
    except ProviderUpstreamMalformed:
        return _v4_error("provider_upstream_malformed", 502)
    except ProviderProjectionLimit as exc:
        return _v4_error(
            "provider_projection_limit", 413,
            limit=exc.limit, limitValue=exc.limit_value)

    # ⑫ conditional judgment + emission (main context; worker already
    # produced the validator on the canonical = served identity bytes).
    etag_value = extra.get("ETag")
    if (rep_version is not None and etag_value is not None
            and etag_mod.if_none_match_matches(if_none_match, etag_value)):
        return etag_mod.not_modified_response(
            etag_value, extra.get("Vary", "Accept-Encoding"))
    return Response(
        encoded, status_code=200, media_type="application/json",
        headers={"Cache-Control": "no-store", **extra},
    )


# --- §3.3 per-feature gates (2026-08-19 integration close-out) -------------
#
# 镜像 sessions.py::_v4_representation_revision_active 的动态读法：模块级
# feature ID 常量 + 调用时读 readiness_mod.SATISFIED（不冻结 def-time
# 值）——readiness 翻转/测试 monkeypatch 无需改本文件即可 wire 级生效。
# 门控关闭态（feature ID ∉ SATISFIED）：v4 面维持 4.0.0 已发布行为
# （providers = v3 同款透传；session single = v3 skeleton 投影路径）。

_V4_PROVIDERS_FEATURE = "providers.redacted.v4"


def _V4_PROVIDERS_REVISION_ACTIVE() -> bool:
    """§3.3 门控：``providers.redacted.v4 ∈ SATISFIED`` 时 §12 修订面生效。"""
    return _V4_PROVIDERS_FEATURE in readiness_mod.SATISFIED


_V4_SESSION_SINGLE_FEATURE = "session.single.projection.v4"


def _V4_SESSION_SINGLE_REVISION_ACTIVE() -> bool:
    """§3.3 门控：``session.single.projection.v4 ∈ SATISFIED`` 时 §13 生效。"""
    return _V4_SESSION_SINGLE_FEATURE in readiness_mod.SATISFIED


@router.get("/config/providers")
async def config_providers(request: Request, directory: str | None = None):
    """Upstream ``GET /config/providers`` — provider catalog map.

    ``?v=4`` (§12): the §12 safe-projection pipeline above. ``?v=3`` /
    selector-less default: the byte-identical v3 controlled-proxy
    passthrough (v3 is frozen — zero change)."""
    resolved = _resolve(request, directory)
    if (wire_view_from_scope(request.scope) == 4
            and _V4_PROVIDERS_REVISION_ACTIVE()):
        return await _handle_providers_v4(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/config/providers",
        directory=resolved,
    )


# --- session single (groups/session.ts:132-137, skeleton projection) ------
#
# v4（§13 正式修订）：``?v=4`` 走 dbaux 点查 + **唯一 canonical projector**
# （``canonical_session_skeleton_v4``——与 v4 列表 items 同函数装配，
# §13.3 冻结不变量：同输入逐字段同值）；dbaux 不可用 → 整响应 native
# HTTP 回退（上游 ``/session/{sid}`` 经 ``native_session_to_record``
# 归一化——键 presence 保留三态——再喂同一 projector，§13.2 fallback
# 列）+ ``degraded:true``；required 不可表示 / 行不可投影 → 整响应 503
# ``auxiliary_unavailable``（禁跨源拼接）。``?v=3`` / selector-less /
# 门控关（§3.3）：v3 skeleton 原路径，逐字节不变。ETag/Vary 属批次 3A，
# 本路由 v4 分支不加 ETag。


def _session_single_sql() -> str:
    """单行点查 SQL（与 ``dbaux.projection.build_sessions_query`` 同一
    SELECT 形状：session 投影列 + project LEFT JOIN 别名列，行集键与
    ``rows_to_records`` 的 ``ROW_KEYS`` 对齐）。"""
    columns = ", ".join(f"s.{c}" for c in SESSION_PROJECTION_COLUMNS)
    joined = ", ".join(f"p.{c} AS p_{c}" for c in PROJECT_JOIN_COLUMNS)
    return (f"SELECT {columns}, {joined}\n"
            "FROM session s\n"
            "LEFT JOIN project p ON s.project_id = p.id\n"
            "WHERE s.id = ?")


_SESSION_SINGLE_SQL = _session_single_sql()


def _project_native_session_single(raw: bytes) -> dict | None:
    """native 回退体 → §13.1 裸对象（malformed → ValueError → 503）。

    ``native_session_to_record`` 归一化（键 presence = §13.2b 三态载体：
    显式 null ≠ 键缺席）后喂唯一 canonical projector（§13.3），
    ``fallback=True`` → item degraded 恒 true（§13.4 平凡推论）。
    """
    session = orjson.loads(raw)
    if not isinstance(session, dict):
        raise ValueError("session single payload is not an object")
    return canonical_session_skeleton_v4(
        native_session_to_record(session), fallback=True,
    )


async def _session_single_native_fallback(
    request: Request, sid: str, directory: str | None,
) -> Response:
    """dbaux 不可用态的整响应 native HTTP 回退（§13.2 fallback 列）。

    上游 ``GET /session/{sid}``（两层错误规则继承 v3：5xx/网络 → 503
    ``upstream_unavailable``，4xx → status+body 逐字）；200 → cap-read、
    transform-pool offload 投影（同构平铺 + §13.1 装配）→ 裸对象 +
    ``degraded:true``；required 不可表示 → 503 ``auxiliary_unavailable``。
    """
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")
    skeleton: dict | None = None
    try:
        async with request.app.state.transforms as pool:
            try:
                response = await stream_upstream(
                    request, f"/session/{sid}", directory)
            except httpx.RequestError as exc:
                raise_upstream_unavailable(exc)
            try:
                status = response.status_code
                if status >= 500:
                    await _read_error_body(request, response)
                    raise_upstream_unavailable(
                        RuntimeError(f"upstream {status}"))
                if status >= 400:
                    # native 4xx 逐字（继承；cap 保护的 verbatim 职责）
                    err = await _read_error_body(request, response)
                    return Response(
                        err,
                        status_code=status,
                        headers=_upstream_passthrough_headers(response),
                    )
                try:
                    body, _ = await read_with_cap(
                        response, config.max_response_bytes,
                        on_read=lambda n: stash_up_in(request, n))
                except httpx.RequestError as exc:
                    raise_upstream_unavailable(exc)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        accept_encoding=accept_encoding,
                        limit=config.max_response_bytes,
                    )
                try:
                    skeleton = await pool.offload(
                        _project_native_session_single, body)
                except ValueError:
                    raise_upstream_unavailable(
                        RuntimeError("malformed upstream body"))
            finally:
                await response.aclose()
    except TransformBusy:
        return busy_response(accept_encoding)
    if skeleton is None:
        # §13.2a：required 字段不可表示 → 整响应失败，不混装
        raise _aux_unavailable()
    return json_response(
        skeleton, accept_encoding=accept_encoding,
        headers={"Cache-Control": "no-store"},
    )


async def _handle_session_single_v4(
    request: Request, sid: str, directory: str | None,
) -> Response:
    """§13 v4 单查：dbaux 点查优先，native 回退兜底。"""
    dbaux = getattr(request.app.state, "dbaux", None)
    if dbaux is not None and dbaux.status().available:
        try:
            rows = await dbaux.query(_SESSION_SINGLE_SQL, (sid,))
        except AuxiliaryUnavailableError:
            # 查询期竞态（disable/trip）→ 降级 native 回退（§4.2 矩阵）
            pass
        except sqlite3.Error:
            # fail-closed：不回退、不泄 SQLite 细节（列表 BLOCKER-1 同规）
            raise _aux_unavailable()
        else:
            if not rows:
                raise CodedHTTPException(
                    404, code="session_not_found", sessionID=sid)
            records = rows_to_records(rows)
            if not records:
                # 行存在但不可投影（坏 JSON 列）→ §13.2c 整响应失败
                raise _aux_unavailable()
            skeleton = canonical_session_skeleton_v4(records[0])
            if skeleton is None:
                # §13.2a：required 字段不可表示 → 整响应失败
                # （列表 items 同函数同判定——§13.3 同一 projector）
                raise _aux_unavailable()
            return json_response(
                skeleton,
                accept_encoding=request.headers.get("accept-encoding"),
                headers={"Cache-Control": "no-store"},
            )
    return await _session_single_native_fallback(request, sid, directory)


@router.get("/session/{sid}")
async def session_single(request: Request, sid: str,
                         directory: str | None = None):
    """Upstream ``GET /session/{sid}`` projected through
    ``skeleton_session`` (identical whitelist to ``GET /slimapi/sessions``
    — drops ``cost``/``tokens``/``location``/``subpath``/``repoPath``/
    ``commit``/``branch``/``status``/``version``).

    ``?v=4`` (§13): dbaux 点查 → 裸 SessionSkeletonV4（同一 canonical
    projector as v4 列表）；dbaux 不可用 → native 回退 + degraded；
    required 不可表示 → 503。``?v=3`` / selector-less default: the
    byte-identical v3 controlled-proxy passthrough (v3 is frozen — zero
    change)."""
    resolved = _resolve(request, directory)
    if (wire_view_from_scope(request.scope) == 4
            and _V4_SESSION_SINGLE_REVISION_ACTIVE()):
        return await _handle_session_single_v4(request, sid, resolved)
    return await read_passthrough_get(
        request, upstream_path=f"/session/{sid}",
        directory=resolved,
        project=_project_session,
    )


# --- active / globalHealth (no query → directory tolerant-ignore) ---------


@router.get("/api/session/active")
async def session_active(request: Request):
    """Upstream ``GET /api/session/active`` —
    ``{data: Record<SessionID, SessionActive>}`` verbatim."""
    return await read_passthrough_get(
        request, upstream_path="/api/session/active",
    )


@router.get("/global/health")
async def global_health(request: Request):
    """Upstream ``GET /global/health`` — ``{healthy, version}`` verbatim."""
    return await read_passthrough_get(
        request, upstream_path="/global/health",
    )


# --- B4: session context (v2 /api/session/{sid}/context, directory N/A) ----


def _strip_directory_query(request: Request) -> None:
    """B4 non-consuming directory tolerance: drop ``directory`` from the raw
    query bytes before the upstream URL is built.

    The v2 session group resolves location per-sid via
    sessionLocationMiddleware (``protocol/groups/session.ts:173-305``) — it
    does NOT participate in directory routing. The client's ``?directory=``
    is ignored (never forwarded upstream, never an error) — same semantics
    as the questions/permissions non-consuming set. The selector leaves
    tolerant routes' ``directory`` in ``scope["query_string"]``, so this
    route strips it here (same byte-preserving scan as the selector)."""
    request.scope["query_string"] = _strip_query_keys(
        request.scope.get("query_string", b"") or b"",
        frozenset({DIRECTORY_QUERY_PARAM}),
    )


@router.get("/session/{sid}/context")
async def session_context(request: Request, sid: str):
    """Upstream ``GET /api/session/{sid}/context`` — ``{data: [...]}``
    verbatim (no projection).

    B4 annex (v2 session group, ``protocol/groups/session.ts``). Directory
    is NOT consumed: ``?directory=`` is tolerated and dropped (not forwarded,
    not an error)."""
    _strip_directory_query(request)
    return await read_passthrough_get(
        request, upstream_path=f"/api/session/{sid}/context",
    )
