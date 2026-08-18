"""v4 sessions DB 投影源基础设施（B3a-B1）。

子模块：

- :mod:`oc_slimapi.dbaux.path_resolution` — DB 路径解析（design-v4-dbaux
  §3：explicit env / OPENCODE_DB / 候选发现，fail-closed）。
- :mod:`oc_slimapi.dbaux.lifecycle` — 连接生命周期（§1/§2 S-B02 线程
  亲和）、schema 门（§6）、P99 熔断（§2.3-6）、inode 校验 swap（§4.1）、
  错误分类重探（§4.2）。
- :mod:`oc_slimapi.dbaux.cursor` — v4 sessions keyset 翻页 cursor
  （v4-contract §4.5：编解码 / 过滤上下文指纹 / 语法校验优先于 503）。
- :mod:`oc_slimapi.dbaux.projection` — v4 sessions 投影 SQL 组装器 +
  行组装容忍 + 查询执行（B3a-B2；design-v4-dbaux §8/§9；``search_hash``
  /``allowlist_rev`` 由 cursor 模块 canonical 提供，此处经 projection
  re-export）。

阶段 B 后续泳道（B2 投影 SQL / B3 cursor / B4 路由分叉 / B5 指标）在此
之上构建。sidecar 对上游 SQLite 绝无写入（mode=ro + query_only 双层，
AGENTS.md 硬规则「SQLite 写域」）。
"""
from .cursor import (
    ARCHIVED_DEFAULT,
    PARENT_DEFAULT,
    CursorFingerprint,
    CursorPayload,
    InvalidCursorError,
    allowlist_rev,
    build_fingerprint,
    decode_cursor,
    encode_cursor,
    fingerprint_mismatch,
    normalize_archived,
    normalize_parent,
    search_hash,
)
from .lifecycle import (
    AuxiliaryUnavailableError,
    DbAuxStatus,
    DbAuxiliarySource,
    LatencyBreaker,
    PROJECT_JOIN_COLUMNS,
    SESSION_PROJECTION_COLUMNS,
    classify_sqlite_error,
    schema_gate_missing,
)
from .path_resolution import (
    DisabledResolution,
    ResolvedPath,
    resolve_db_path,
    stat_inode_marker,
)
from .projection import (
    ARCHIVED_STATES,
    JSON_COLUMNS,
    PARENT_RESERVED_STATES,
    ROW_KEYS,
    SessionsPage,
    SessionsQuery,
    build_sessions_query,
    escape_like,
    fetch_sessions_page,
    has_wildcard,
    normalized_search,
    rows_to_records,
)

__all__ = [
    "ARCHIVED_DEFAULT",
    "ARCHIVED_STATES",
    "AuxiliaryUnavailableError",
    "CursorFingerprint",
    "CursorPayload",
    "DbAuxStatus",
    "DbAuxiliarySource",
    "DisabledResolution",
    "InvalidCursorError",
    "JSON_COLUMNS",
    "LatencyBreaker",
    "PARENT_DEFAULT",
    "PARENT_RESERVED_STATES",
    "PROJECT_JOIN_COLUMNS",
    "ResolvedPath",
    "ROW_KEYS",
    "SESSION_PROJECTION_COLUMNS",
    "SessionsPage",
    "SessionsQuery",
    "allowlist_rev",
    "build_fingerprint",
    "build_sessions_query",
    "classify_sqlite_error",
    "decode_cursor",
    "encode_cursor",
    "escape_like",
    "fetch_sessions_page",
    "fingerprint_mismatch",
    "has_wildcard",
    "normalize_archived",
    "normalize_parent",
    "normalized_search",
    "resolve_db_path",
    "rows_to_records",
    "schema_gate_missing",
    "search_hash",
    "stat_inode_marker",
]
