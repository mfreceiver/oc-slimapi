"""v4 sessions 投影 SQL 组装 + 行组装容忍 + 查询执行（B3a-B2）。

设计权威：docs/specs/design-v4-dbaux.md §8（组装容忍）/ §9（SQL 语义冻结）；
wire 契约：docs/specs/v4-contract.md §4.5/§4.6。要点：

- **一条 SQL 组装器**（:func:`build_sessions_query`）：全谓词参数化绑定
  （值绝不字符串拼接进 SQL 文本）；占位符用 SQLite 位置参数 ``?``——
  B1 ``DbAuxiliarySource.query(sql, params)`` 收位置参数序列（tuple 化），
  具名绑定会被 ``tuple(params)`` 破坏；设计文档谓词文本中的 ``:name``
  是语义冻结，绑定名是实现细节。
- 排序冻结 ``(time_updated DESC, id DESC)``；complete 判定 = 同一
  ``LIMIT ? + 1`` 窗口（§9.2：返回 limit+1 行 → complete:false）。
- search：``(? IS NULL OR s.title LIKE ? ESCAPE '\\')``，pattern =
  ``'%' + LIKE 字面转义(normalized_search) + '%'``——字面子串语义。
  SQLite 默认 LIKE 对 ASCII 大小写不敏感（与上游 ``like()`` 同源行为，
  等价性锚定因此成立）；escape 仅 ``%`` / ``_`` / ``\\`` 三字符。
- allowlist 子树谓词（§9.3 冻结，二进制前缀弃 LIKE）：每项
  ``(s.directory = ? OR substr(s.directory, 1, ?) = ?)``，prefix =
  ``d + '/'`` 独立绑定、prefix_len = ``len(d) + 1``；根 ``/`` 特例
  ``substr(s.directory, 1, 1) = '/'``；多项 OR 并集；``=``/``substr``
  默认 BINARY 比较大小写敏感、无 ``%``/``_`` 通配语义。空 directory 行
  在 allowlist 非空查询中天然排除（§9.4 legacy 空串按字面参与谓词）。
- cursor keyset 下推：``(s.time_updated < ? OR (s.time_updated = ? AND
  s.id < ?))``（§4.5 复合谓词的 OR 展开形；与 ``(t,i) < (?,?)`` 行值
  比较语义恒等，且不依赖 SQLite ≥3.15 的行值支持）。
- 组装容忍（§8，仅行级后处理，谓词层无此自由度）：project join 缺行 →
  ``p_*`` 为 NULL（LEFT JOIN，组装层投影为 ``project=null``）；JSON 列
  解析失败 → 跳行 + warning（带 sid）；行缺 id → 跳行。
- 查询执行经 B1 ``DbAuxiliarySource.query()`` 通道（§1.2 短事务快照：
  投影 + complete 判定同 snapshot 内完成）。

给 B4 泳道的接口（降级矩阵判定输入）：

- :func:`normalized_search` —— ``trim(raw)`` 唯一输入源（四消费点共用：
  SQL pattern 构造 / has_wildcard 判定 / 指纹 hash / HTTP 降级上游 query
  ——降级路径同样传 normalized，禁 DB 查 trim 值而上游收 raw）；
- :func:`has_wildcard` —— 确定性通配判定（含 ``%``/``_``/``\\`` 任一 →
  DB 不可用时 503）；
- :func:`search_hash` / :func:`allowlist_rev` —— cursor 指纹原料，**直接
  复用 B3 泳道 ``dbaux/cursor.py`` 的 canonical 实现**（§4.5
  f.search-hash / f.allowlist-rev；hash 输入 = trim 后、转义前；此处仅
  re-export，杜绝第二实现漂移）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import orjson
from ..logging_config import get_logger
from .cursor import allowlist_rev, search_hash
from .lifecycle import (
    DbAuxiliarySource,
    PROJECT_JOIN_COLUMNS,
    SESSION_PROJECTION_COLUMNS,
)

_LOGGER = get_logger("dbaux")

# project join 投影列（契约冻结 project={id,name,worktree}；§8 join 缺行 → NULL）
PROJECT_ALIASED_COLUMNS: tuple[str, ...] = (
    f"p.{PROJECT_JOIN_COLUMNS[0]} AS p_id",
    f"p.{PROJECT_JOIN_COLUMNS[1]} AS p_name",
    f"p.{PROJECT_JOIN_COLUMNS[2]} AS p_worktree",
)

# 查询结果行 → dict 的键序（与 SELECT 列序严格一致）
ROW_KEYS: tuple[str, ...] = SESSION_PROJECTION_COLUMNS + ("p_id", "p_name", "p_worktree")

# §8 JSON 列（真库 TEXT 存 JSON；解析失败 → 跳行 + warning）
JSON_COLUMNS: tuple[str, ...] = ("summary_diffs", "revert", "permission", "metadata")

ARCHIVED_STATES: tuple[str, ...] = ("omit", "only", "all")
# parent 保留态；其余非空字符串按 <sid> 字面处理（上游 sid 为 ses_* 格式，
# 与保留词无碰撞面——若未来 sid 格式变化碰撞，此处 fail-closed 拒绝）
PARENT_RESERVED_STATES: tuple[str, ...] = ("all", "none", "only")


# ---------------------------------------------------------------------------
# search 规范化 / 转义 / 指纹（§9.1 rev-3 冻结：normalized_search = trim(raw)
# 为四个消费点的唯一输入源；hash 输入 = trim 后、LIKE 转义前）
# ---------------------------------------------------------------------------

def normalized_search(raw: str | None) -> str | None:
    """§9.1 规范化：``normalized_search = trim(raw)``。

    ``None``（参数缺席）透传 ``None``；非字符串输入 TypeError（fail-fast，
    参数类型校验属路由层，此处只保护纯函数契约）。trim 语义 = Python
    ``str.strip()``（Unicode 空白；与上游 JS ``.trim()`` 同族）。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"search must be str or None, got {type(raw).__name__}")
    return raw.strip()


def escape_like(value: str) -> str:
    """§9.1 字面转义（冻结）：``\\`` → ``\\\\``、``%`` → ``\\%``、``_`` → ``\\_``。

    反斜杠必须最先替换（否则后续引入的 ``\\`` 会被二次转义）。配合 SQL
    ``ESCAPE '\\'`` 后 ``%``/``_`` 失去通配语义——search = 字面子串匹配。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def has_wildcard(normalized: str | None) -> bool:
    """§9.1 确定性通配判定：含 ``%`` / ``_`` / ``\\`` 任一字符 → True。

    与指纹规范化共用同一判定（同输入两次执行结果一致）。DB 不可用时
    含通配字符的 search → 503（B4 路由层消费）；``\\`` 属保守加宽
    （design §9.1 rev-1：理论可等价，纳入 503 换规则单一）。
    """
    return isinstance(normalized, str) and any(c in normalized for c in "%_\\")


# ---------------------------------------------------------------------------
# SQL 组装器（§9 冻结谓词族；全参数化绑定）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionsQuery:
    """一条组装完成的投影 SQL（SQL 文本 + 位置参数元组，序严格对应）。"""

    sql: str
    params: tuple[Any, ...]


def build_sessions_query(
    *,
    archived: str = "omit",
    parent: str = "all",
    search: str | None = None,
    cursor: tuple[int, str] | None = None,
    limit: int = 100,
    allowlist: Sequence[str] = (),
) -> SessionsQuery:
    """组装 v4 sessions 投影 SQL（§9 四条冻结 + LIMIT+1 同窗口 complete）。

    参数：
      - ``archived``：omit（默认，``time_archived IS NULL``）/ only / all；
      - ``parent``：all（默认）/ none（``parent_id IS NULL``）/ only
        （``parent_id IS NOT NULL``，R6 冻结）/ 任意非保留词字符串 =
        ``parent_id = ?`` 字面 sid 绑定；
      - ``search``：raw 输入（内部 normalized_search 规范化；None = 无
        search 轴，谓词以 ``? IS NULL`` 形式恒真保留——SQL 形状稳定，
        EQP 特征不随参数缺席漂移）；
      - ``cursor``：keyset 锚点 ``(time_updated, id)``（opaque 编解码属
        B3 dbaux/cursor.py；此处只承接解出的锚点做下推）；
      - ``limit``：窗口大小（SQL 内 ``LIMIT ? + 1``；1..500 域校验属
        路由层 B4，此处只做下界防御）；
      - ``allowlist``：目录白名单（B4 入口层已 resolve 规范化；空序列 =
        无谓词零影响）。空串项 ValueError（fail-closed——空 directory 的
        语义是 legacy 行，不是可白名单的目录）。

    返回值绝不包含用户输入拼接的 SQL 文本（值全部经 ``?`` 绑定）。
    """
    if archived not in ARCHIVED_STATES:
        raise ValueError(f"archived must be one of {ARCHIVED_STATES}, got {archived!r}")
    if not isinstance(parent, str) or not parent:
        raise ValueError(f"parent must be a non-empty string, got {parent!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")

    preds: list[str] = []
    params: list[Any] = []

    # archived 三态（§4.1）
    if archived == "omit":
        preds.append("s.time_archived IS NULL")
    elif archived == "only":
        preds.append("s.time_archived IS NOT NULL")

    # parent 四态（§4.1；only = IS NOT NULL，R6 实证冻结无空串哨兵）
    if parent == "none":
        preds.append("s.parent_id IS NULL")
    elif parent == "only":
        preds.append("s.parent_id IS NOT NULL")
    elif parent != "all":
        params.append(parent)
        preds.append("s.parent_id = ?")

    # search（§9.1 冻结谓词形；轴恒在，None 时 ? IS NULL 恒真）
    norm = normalized_search(search)
    if norm is None:
        preds.append("(? IS NULL OR s.title LIKE ? ESCAPE '\\')")
        params.extend((None, None))
    else:
        pattern = "%" + escape_like(norm) + "%"
        preds.append("(? IS NULL OR s.title LIKE ? ESCAPE '\\')")
        params.extend((pattern, pattern))

    # allowlist 子树谓词（§9.3 冻结：二进制前缀弃 LIKE）
    items = tuple(allowlist)
    if items:
        branches: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item:
                raise ValueError(
                    "allowlist items must be non-empty normalized absolute paths, "
                    f"got {item!r}"
                )
            if item == "/":
                # 根特例：匹配所有非空绝对路径；不与 '//' 前缀规则混算
                branches.append("substr(s.directory, 1, 1) = '/'")
            else:
                branches.append("(s.directory = ? OR substr(s.directory, 1, ?) = ?)")
                params.extend((item, len(item) + 1, item + "/"))
        preds.append("(" + " OR ".join(branches) + ")")

    # cursor keyset 下推（§4.5 复合谓词 OR 展开形）
    if cursor is not None:
        if len(cursor) != 2:
            raise ValueError(f"cursor anchor must be (time_updated, id), got {cursor!r}")
        anchor_t, anchor_id = cursor
        if isinstance(anchor_t, bool) or not isinstance(anchor_t, int):
            raise ValueError(f"cursor anchor time must be int, got {type(anchor_t).__name__}")
        if not isinstance(anchor_id, str) or not anchor_id:
            raise ValueError(f"cursor anchor id must be a non-empty string, got {anchor_id!r}")
        preds.append("(s.time_updated < ? OR (s.time_updated = ? AND s.id < ?))")
        params.extend((anchor_t, anchor_t, anchor_id))

    select_columns = (
        ", ".join(f"s.{column}" for column in SESSION_PROJECTION_COLUMNS)
        + ", "
        + ", ".join(PROJECT_ALIASED_COLUMNS)
    )
    sql = (
        f"SELECT {select_columns}\n"
        "FROM session s LEFT JOIN project p ON s.project_id = p.id\n"
        "WHERE " + " AND ".join(preds) + "\n"
        "ORDER BY s.time_updated DESC, s.id DESC\n"
        "LIMIT ? + 1"
    )
    return SessionsQuery(sql=sql, params=tuple(params) + (limit,))


# ---------------------------------------------------------------------------
# 行组装（§8 容忍：project null / JSON 跳行 / 缺 id 跳行）
# ---------------------------------------------------------------------------

def rows_to_records(rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """SELECT 行 → 投影记录 dict（键 = :data:`ROW_KEYS`，序一致）。

    行级容忍（§8）：
      - ``id`` 缺失/NULL → 跳行 + warning（keyset/排序参与性破坏）；
      - JSON 列（:data:`JSON_COLUMNS`）字符串解析失败 → 跳行 + warning
        （带 sid 便于定位；不 500）；NULL / 非字符串原样保留。
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(zip(ROW_KEYS, row))
        sid = record.get("id")
        if sid is None:
            _LOGGER.warning("dbaux projection: skip row with missing id")
            continue
        invalid_column: str | None = None
        for column in JSON_COLUMNS:
            value = record.get(column)
            if isinstance(value, str):
                try:
                    record[column] = orjson.loads(value)
                except orjson.JSONDecodeError:
                    invalid_column = column
                    break
        if invalid_column is not None:
            _LOGGER.warning(
                "dbaux projection: skip row sid=%s (invalid JSON in %s)",
                sid,
                invalid_column,
            )
            continue
        records.append(record)
    return records


@dataclass(frozen=True)
class SessionsPage:
    """一页投影结果：records（≤ limit 行）+ complete（§9.2 LIMIT+1 窗口判定）。"""

    records: list[dict[str, Any]]
    complete: bool


async def fetch_sessions_page(
    source: DbAuxiliarySource,
    *,
    archived: str = "omit",
    parent: str = "all",
    search: str | None = None,
    cursor: tuple[int, str] | None = None,
    limit: int = 100,
    allowlist: Sequence[str] = (),
) -> SessionsPage:
    """经 B1 ``query()`` 通道执行投影（同事务快照内完成投影 + complete 判定）。

    不可用（禁用/熔断）→ :class:`AuxiliaryUnavailableError` 上抛（B4 映射
    503 auxiliary_unavailable）；busy 原样 sqlite3.Error（B4 §7 处理）。
    """
    query = build_sessions_query(
        archived=archived,
        parent=parent,
        search=search,
        cursor=cursor,
        limit=limit,
        allowlist=allowlist,
    )
    rows = await source.query(query.sql, query.params)
    complete = len(rows) <= limit
    return SessionsPage(records=rows_to_records(rows[:limit]), complete=complete)
