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

import orjson
from fastapi import APIRouter, Request

from ..config import directory_allowed
from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..selector import (
    DIRECTORY_QUERY_PARAM,
    _strip_query_keys,
    resolve_route_directory,
)
from ..skeleton import skeleton_session
from ._read_passthrough import read_passthrough_get

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


def _enforce_file_allowlist(request: Request, directory: str | None) -> None:
    allowlist = request.app.state.config.directory_allowlist
    # rev-sgpt MAJOR-1: canonical (realpath) subtree match via the shared
    # config helper — a symlinked directory that lexically sits inside an
    # allowed root but resolves outside is rejected with the same uniform
    # 403 (no existence leak).
    if allowlist is not None and not directory_allowed(allowlist, directory):
        raise CodedHTTPException(403, code="directory_not_allowed")


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
    _enforce_file_allowlist(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file",
        directory=resolved,
    )


@router.get("/file/content")
async def file_content(request: Request, path: str,
                       directory: str | None = None):
    """Upstream ``GET /file/content`` — ``LegacyContent`` (text|binary)."""
    resolved = _resolve(request, directory)
    _enforce_file_allowlist(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file/content",
        directory=resolved,
    )


@router.get("/file/status")
async def file_status(request: Request, directory: str | None = None):
    """Upstream ``GET /file/status`` — ``LegacyStatus[]``."""
    resolved = _resolve(request, directory)
    _enforce_file_allowlist(request, resolved)
    return await read_passthrough_get(
        request, upstream_path="/file/status",
        directory=resolved,
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


@router.get("/config/providers")
async def config_providers(request: Request, directory: str | None = None):
    """Upstream ``GET /config/providers`` — provider catalog map."""
    return await read_passthrough_get(
        request, upstream_path="/config/providers",
        directory=_resolve(request, directory),
    )


# --- session single (groups/session.ts:132-137, skeleton projection) ------


@router.get("/session/{sid}")
async def session_single(request: Request, sid: str,
                         directory: str | None = None):
    """Upstream ``GET /session/{sid}`` projected through
    ``skeleton_session`` (identical whitelist to ``GET /slimapi/sessions``
    — drops ``cost``/``tokens``/``location``/``subpath``/``repoPath``/
    ``commit``/``branch``/``status``/``version``)."""
    return await read_passthrough_get(
        request, upstream_path=f"/session/{sid}",
        directory=_resolve(request, directory),
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
