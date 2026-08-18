"""B3a-B2 测试共享基建：七维度数据集 / 镜像 oracle / golden 文档装载。

S-B03 铁律的落地形态：行集 oracle 是**独立于 SQL 组装器**的 Python
镜像实现（过滤/排序/翻页逻辑单独重写，不 import 生产投影模块的任何
谓词函数；JSON 解析用 stdlib ``json`` 而非生产侧 ``orjson``，进一步
隔离实现面）。golden 文档由镜像 oracle 生成（生成器标识
``mirror-oracle-v1``）——**不是**跑 sidecar SQL 自证。

用法（生成/再生成 golden）::

    .venv/bin/python tests/v4_fixture.py --write-golden
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

# 生产投影列清单仅在测试里用于「键集合」对齐（非谓词逻辑），直接引用
# 常量不构成 oracle 依赖（依赖的是行为镜像，不是常量）。
from oc_slimapi.dbaux import ROW_KEYS

# 镜像侧 JSON 解析集——与生产 JSON_COLUMNS 对齐（R5 BLOCKER-1：model 是
# 真库 json 列，镜像容忍语义必须同步覆盖，否则 oracle 与生产漂移）。
JSON_COLS = ("summary_diffs", "revert", "permission", "metadata", "model")

ALIGNED_VERSION = "v1.18.18"  # AGENTS.md：opencode-src/current 对齐版本
GOLDEN_GENERATOR = "mirror-oracle-v1"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_PATH = GOLDEN_DIR / f"sessions-global-{ALIGNED_VERSION}.json"

# 「当前时刻」极端样本（design §10.3 七维度之七）：固定常数保证 golden
# 确定性（用真实 now 会让每次再生成漂移）。
FIXED_NOW_MS = 1_787_132_160_000  # 2026-08-16T00:00:00Z 邻近（ms）


# ---------------------------------------------------------------------------
# scripts/eqp_matrix.py 装载（B0 先例复用：DDL / 行集 oracle / EQP 解析）
# ---------------------------------------------------------------------------

_EQP_MODULE: Any = None


def load_eqp_matrix() -> Any:
    """importlib 装载 scripts/eqp_matrix.py（scripts/ 非包，按文件路径加载）。"""
    global _EQP_MODULE
    if _EQP_MODULE is None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "eqp_matrix.py"
        spec = importlib.util.spec_from_file_location("eqp_matrix_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _EQP_MODULE = module
    return _EQP_MODULE


# ---------------------------------------------------------------------------
# 固定数据集（design §10.3 七维度；golden 的 manifest 源）
# ---------------------------------------------------------------------------

def _row(
    sid: str,
    project_id: str,
    parent_id: str | None,
    directory: str,
    title: str,
    time_created: int,
    time_updated: int,
    *,
    time_archived: int | None = None,
    time_compacting: int | None = None,
    summary_diffs: str | None = '{"files":1,"additions":2,"deletions":3}',
    revert: str | None = None,
    permission: str | None = None,
    metadata: str | None = '{"origin":"fixture"}',
    agent: str = "agent-x",
    model: str | None = None,
) -> dict[str, Any]:
    # 确定性派生（hash() 跨进程随机化，不可用于 golden 数据集）
    n = int(hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8], 16) % 500
    if model is None:
        # R5 BLOCKER-1：model 列 = 真库形态 JSON 文本（drizzle json 列，
        # fromRow 对象语义）；按 sid 派生保证行间互异。
        model = json.dumps({
            "id": f"model-{sid}", "providerID": f"prov-{n % 3}",
        })
    return {
        "id": sid,
        "project_id": project_id,
        "parent_id": parent_id,
        "directory": directory,
        "title": title,
        "version": ALIGNED_VERSION.removeprefix("v"),
        "summary_additions": n % 7,
        "summary_deletions": n % 3,
        "summary_files": n % 2,
        "summary_diffs": summary_diffs,
        "revert": revert,
        "permission": permission,
        "time_created": time_created,
        "time_updated": time_updated,
        "time_compacting": time_compacting,
        "time_archived": time_archived,
        "agent": agent,
        "model": model,
        "tokens_input": 100 + n,
        "tokens_output": 50 + n,
        "tokens_reasoning": n % 11,
        "tokens_cache_read": n % 13,
        "tokens_cache_write": n % 17,
        "metadata": metadata,
    }


PROJECTS: list[dict[str, Any]] = [
    {"id": "prj_alpha", "name": "alpha", "worktree": "/wt/alpha"},
    {"id": "prj_beta", "name": "beta", "worktree": "/wt/beta"},
]

DATASET: list[dict[str, Any]] = [
    # 维度 1：tie-break——同 time_updated=6000 三行（id 字典序决定序）
    _row("ses_child_a", "prj_alpha", "ses_root_1", "/foo/sub",
         "fix the 100% bug", 1100, 6000),                      # search '%' 字面
    _row("ses_child_b", "prj_alpha", "ses_root_1", "/foo/bar/deep",
         "under_score path", 1200, 6000),                      # search '_' 字面
    _row("ses_child_c", "prj_beta", "ses_root_1", "/foo/baz",
         "back\\slash title", 1300, 6000),                     # search '\' 字面
    # 维度 2/3：archived 混合 + 父子（root/child/孙）
    _row("ses_root_1", "prj_alpha", None, "/foo", "root one plain", 1000, 5000),
    _row("ses_root_2", "prj_beta", None, "/foobar", "plain title too",
         1400, 7000, time_archived=7900),                      # /foobar 边界 + archived
    _row("ses_archived_child", "prj_alpha", "ses_root_2", "/foo/sub",
         "archived child", 3400, 8400, time_archived=8500),    # archived × parent=<sid>
    # 维度 5：allowlist 多子树（/foo 树、/a 树、/b 树）+ 对照面
    _row("ses_root_3", "prj_alpha", None, "/a", "grp1 alpha work",
         1500, 7100, time_compacting=1234),                    # time_compacting 置值
    _row("ses_a_pct", "prj_alpha", "ses_root_3", "/a/%20b/c",
         "a percent dir session", 1600, 7200),                 # 目录含 % 字面
    _row("ses_a_us", "prj_beta", "ses_root_3", "/a/dir_%_x",
         "an underscore dir", 1700, 7300, time_archived=8900),  # 目录含 _ 字面 + archived
    _row("ses_same_level", "prj_alpha", None, "/a%20b/c",
         "same level name", 1800, 7400),                       # 同层异名（/a 前缀不命中）
    _row("ses_case", "prj_beta", None, "/Foo/child",
         "case mismatch dir", 1900, 7500),                     # 大小写边界（BINARY）
    _row("ses_tie_a", "prj_alpha", None, "/b", "tie a", 3000, 8000),
    _row("ses_tie_b", "prj_beta", None, "/b/sub", "tie b",
         3000, 8000, time_archived=9100),                      # tie × archived 混合
    # 维度 6：legacy 空 directory + ${owner}-${host} 目录
    _row("ses_legacy_empty", "prj_alpha", None, "",
         "legacy empty directory", 2000, 7600),
    _row("ses_host_style", "prj_beta", None, "/srv/mar@host-1/proj",
         "owner host dir", 2100, 7700),
    # 维度 7：极端时间戳 0 / now
    _row("ses_time_zero", "prj_alpha", None, "/foo",
         "oldest epoch zero", 0, 0),
    _row("ses_time_now", "prj_beta", "ses_root_2", "/b/late",
         "now-ish late session", FIXED_NOW_MS, FIXED_NOW_MS + 123),
    # tie-break 第二组：time_updated=8000 三行（见 ses_tie_a/b）
    _row("ses_tie_c", "prj_alpha", "ses_root_1", "/foo", "tie c", 3000, 8000),
    # §8 组装容忍：project join 缺行 / JSON 解析失败跳行 / 可选列置值
    _row("ses_orphan_proj", "prj_missing", None, "/foo",
         "join miss project", 3100, 8100),                     # LEFT JOIN 缺行 → project null
    _row("ses_bad_json", "prj_alpha", None, "/foo",
         "broken json row", 3200, 8200,
         summary_diffs="not-json{"),                           # 解析失败 → 跳行
    _row("ses_bad_model", "prj_beta", None, "/foo",
         "broken model json row", 3250, 8700,
         model="not-json-model{"),  # R5 BLOCKER-1：model json 解析失败 → §8 跳行
    _row("ses_revert_full", "prj_beta", "ses_root_3", "/a",
         "with revert data", 3300, 8300,
         revert='{"messageID":"msg_9","partID":"prt_9"}',
         permission='{"session":"ask"}',
         metadata='{"k":"v"}'),                                # revert/permission/metadata 置值
    # search 对照：字面「1004 percent」不含「100%」；ASCII 大小写折叠面
    _row("ses_search_decoy", "prj_alpha", None, "/b",
         "1004 percent four", 3500, 8500),
    _row("ses_case_fold", "prj_alpha", None, "/b",
         "PLAIN TITLE UPPER", 3600, 8600),                     # search 'plain' 应命中（LIKE ASCII 折叠）
]


def dataset_manifest() -> dict[str, Any]:
    """数据集 manifest（golden 头部；对会话行做 canonical 序列化后取摘要）。"""
    canonical = json.dumps(
        sorted(DATASET, key=lambda r: r["id"]),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        "sessions": len(DATASET),
        "projects": len(PROJECTS),
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
    }


def dataset_fingerprint() -> str:
    manifest = dataset_manifest()
    payload = f"{GOLDEN_GENERATOR}:{manifest['digest']}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# fixture DB 构造（真库对齐 DDL 来自 scripts/eqp_matrix.py，B0 冻结）
# ---------------------------------------------------------------------------

def build_fixture_db(
    db_path: Path | str,
    *,
    session_rows: Sequence[dict[str, Any]] | None = None,
    project_rows: Sequence[dict[str, Any]] | None = None,
    column_rename: dict[str, str] | None = None,
) -> Path:
    """建只读语义的临时 fixture DB（schema 对齐真库 DDL）。

    ``column_rename`` 用于 EQ-008 schema 漂移哨兵：重命名 session 表
    列（模拟上游 schema 变更 → schema 门应失效）。
    """
    eqp = load_eqp_matrix()
    rows = list(DATASET if session_rows is None else session_rows)
    projects = list(PROJECTS if project_rows is None else project_rows)
    rename = column_rename or {}

    session_cols = [
        (rename.get(name, name), decl) for name, decl in eqp.SESSION_DDL_COLS
    ]
    session_names = [name for name, _ in session_cols]
    project_names = list(eqp.PROJECT_COL_NAMES)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE session ("
            + ", ".join(f'"{n}" {d}' for n, d in session_cols) + ")"
        )
        con.execute(
            "CREATE TABLE project ("
            + ", ".join(f'"{n}" {d}' for n, d in eqp.PROJECT_DDL_COLS) + ")"
        )
        con.executemany(
            "INSERT INTO session (" + ",".join(session_names) + ") VALUES ("
            + ",".join("?" * len(session_names)) + ")",
            # 数据集恒用原始列名取值；rename 只改目标列名（schema 漂移哨兵）
            [tuple(r[source] for source in eqp.SESSION_COL_NAMES) for r in rows],
        )
        con.executemany(
            "INSERT INTO project (" + ",".join(project_names) + ") VALUES ("
            + ",".join("?" * len(project_names)) + ")",
            [tuple(p[c] for c in project_names) for p in projects],
        )
        con.commit()
    finally:
        con.close()
    return Path(db_path)


# ---------------------------------------------------------------------------
# 镜像 oracle（S-B03：独立重写的过滤/排序/翻页，不 import 生产谓词）
# ---------------------------------------------------------------------------

def _ascii_fold(value: str) -> str:
    """SQLite LIKE 的 ASCII 大小写折叠语义（非 ASCII 不折叠）。"""
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in value)


def _like_contains(title: str, needle: str) -> bool:
    """字面子串（LIKE 通配全部被转义后的等价语义；两侧 ASCII 折叠）。"""
    return _ascii_fold(needle) in _ascii_fold(title)


def _allowlist_hit(directory: str, items: Sequence[str]) -> bool:
    """allowlist 子树谓词镜像：精确匹配或 ``d + '/'`` 前缀；根 ``/`` 特例。"""
    for item in items:
        if item == "/":
            if directory.startswith("/"):
                return True
        elif directory == item or directory.startswith(item + "/"):
            return True
    return False


def _valid_record(row: dict[str, Any]) -> bool:
    """§8 组装容忍镜像：JSON 列解析失败 → 该行不进结果集。"""
    for col in JSON_COLS:
        value = row.get(col)
        if isinstance(value, str):
            try:
                json.loads(value)
            except ValueError:
                return False
    return True


def _raw_records(
    session_rows: Sequence[dict[str, Any]] = DATASET,
    project_rows: Sequence[dict[str, Any]] = PROJECTS,
) -> list[dict[str, Any]]:
    """JOIN 后的全量原始记录（不做 JSON 解析/容忍过滤——镜像 SQL 管线次序）。"""
    projects = {p["id"]: p for p in project_rows}
    records: list[dict[str, Any]] = []
    for row in session_rows:
        if row.get("id") is None:
            continue
        record: dict[str, Any] = {key: row.get(key) for key in ROW_KEYS[:-3]}
        project = projects.get(row.get("project_id"))
        record["p_id"] = project["id"] if project else None
        record["p_name"] = project["name"] if project else None
        record["p_worktree"] = project["worktree"] if project else None
        records.append(record)
    return records


def _parse_record(record: dict[str, Any]) -> dict[str, Any]:
    """JSON 列解析（stdlib json——与生产 orjson 实现隔离）。"""
    parsed = dict(record)
    for col in JSON_COLS:
        value = parsed.get(col)
        if isinstance(value, str):
            parsed[col] = json.loads(value)
    return parsed


def mirror_page(
    *,
    archived: str = "omit",
    parent: str = "all",
    search: str | None = None,
    cursor: tuple[int, str] | None = None,
    limit: int = 100,
    allowlist: Sequence[str] = (),
    session_rows: Sequence[dict[str, Any]] = DATASET,
    project_rows: Sequence[dict[str, Any]] = PROJECTS,
) -> tuple[list[dict[str, Any]], bool]:
    """镜像 v4 sessions 全管线，次序与 SQL 严格一致：

    谓词过滤 → ``(time_updated, id) DESC`` 排序 → keyset → ``LIMIT+1``
    窗口（complete 判定在此）→ §8 组装容忍（JSON 跳行——**窗口后**）。

    返回 (records, complete)——与 ``dbaux.fetch_sessions_page`` 输出形状
    逐字段对齐；实现零共享（独立谓词/排序/翻页代码）。
    """
    normalized = search.strip() if isinstance(search, str) else None

    def _match(record: dict[str, Any]) -> bool:
        if archived == "omit" and record["time_archived"] is not None:
            return False
        if archived == "only" and record["time_archived"] is None:
            return False
        if parent == "none" and record["parent_id"] is not None:
            return False
        if parent == "only" and record["parent_id"] is None:
            return False
        if parent not in ("all", "none", "only") and record["parent_id"] != parent:
            return False
        if normalized is not None and not _like_contains(record["title"], normalized):
            return False
        if allowlist and not _allowlist_hit(record["directory"], list(allowlist)):
            return False
        return True

    matched = [r for r in _raw_records(session_rows, project_rows) if _match(r)]
    matched.sort(key=lambda r: (r["time_updated"], r["id"]), reverse=True)
    if cursor is not None:
        anchor_t, anchor_id = cursor
        matched = [
            r for r in matched
            if (r["time_updated"], r["id"]) < (anchor_t, anchor_id)
        ]
    windowed = matched[: limit + 1]  # LIMIT ? + 1
    complete = len(windowed) <= limit
    records: list[dict[str, Any]] = []
    for record in windowed[:limit]:
        if _valid_record(record):
            records.append(_parse_record(record))
    return records, complete


# ---------------------------------------------------------------------------
# golden 文档（权威源②的 fixture 驱动降级版——任务预声明偏离）
# ---------------------------------------------------------------------------

def response_fingerprint(records: Sequence[dict[str, Any]]) -> str:
    """响应指纹：golden 载荷 canonical JSON 的 sha256[:16]。

    独立于 dataset digest——数据集不变但投影管线（列集/键序）漂移时，
    响应指纹变化而数据集指纹不变，二者交叉定位漂移层。
    """
    canonical = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_golden_document() -> dict[str, Any]:
    """golden = 镜像 oracle 全量投影（生成器标识 + 指纹 + manifest 头）。

    头部（rev gate 增强后）：

    - ``version`` / ``generator`` / ``fingerprint`` / ``dataset_manifest``：
      既有四键（生成器 + 数据集身份）；
    - ``dataset_digest``：数据集摘要顶层便捷键（= manifest.digest）；
    - ``response_fingerprint``：**载荷**身份（投影输出字节身份，锚定
      「同一数据集 + 同一投影管线」的响应可复现性）；
    - ``upstream_locked``：生成时锁定的上游对齐版本（真进程权威源①
      不可用时，标明 golden 的上游语义基线）；
    - ``regenerate_hint``：再生成指引（design §10.6）。
    """
    records, complete = mirror_page(
        archived="all", parent="all", limit=len(DATASET) + 10
    )
    assert complete, "golden 全量窗口必须 complete"
    manifest = dataset_manifest()
    return {
        "version": ALIGNED_VERSION,
        "generator": GOLDEN_GENERATOR,
        "fingerprint": dataset_fingerprint(),
        "dataset_manifest": manifest,
        "dataset_digest": manifest["digest"],
        "response_fingerprint": response_fingerprint(records),
        "upstream_locked": ALIGNED_VERSION,
        "regenerate_hint": ".venv/bin/python tests/v4_fixture.py --write-golden",
        "query": {
            "archived": "all",
            "parent": "all",
            "limit": len(DATASET) + 10,
            "sort": "time_updated DESC, id DESC",
        },
        "sessions": records,
    }


def validate_golden(document: dict[str, Any]) -> tuple[bool, str]:
    """golden 头部 + 体校验。失败消息含再生成指引（design §10.6）。"""
    version = document.get("version")
    if version != ALIGNED_VERSION:
        return False, (
            f"golden version mismatch: file={version!r} aligned={ALIGNED_VERSION!r}"
        )
    if document.get("generator") != GOLDEN_GENERATOR:
        return False, f"golden generator mismatch: {document.get('generator')!r}"
    fingerprint = document.get("fingerprint")
    if fingerprint != dataset_fingerprint():
        return False, (
            f"golden fingerprint mismatch: file={fingerprint!r} "
            f"dataset={dataset_fingerprint()!r} — 数据集与 golden 漂移；"
            f"再生成：.venv/bin/python tests/v4_fixture.py --write-golden"
        )
    # rev gate 增强：头部声明键逐项校验（缺一即失效——强制再生成）。
    if document.get("dataset_digest") != dataset_manifest()["digest"]:
        return False, "golden dataset_digest mismatch or missing"
    if document.get("upstream_locked") != ALIGNED_VERSION:
        return False, (
            f"golden upstream_locked mismatch: file={document.get('upstream_locked')!r}"
        )
    if not document.get("regenerate_hint"):
        return False, "golden regenerate_hint missing"
    expected_records, _ = mirror_page(
        archived="all", parent="all", limit=len(DATASET) + 10
    )
    if document.get("sessions") != expected_records:
        return False, "golden sessions payload mismatch (mirror oracle drift)"
    if document.get("response_fingerprint") != response_fingerprint(expected_records):
        return False, "golden response_fingerprint mismatch (payload identity drift)"
    return True, ""


def load_golden() -> dict[str, Any]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    ok, reason = validate_golden(document)
    assert ok, f"golden 文档失效：{reason}"
    return document


# ---------------------------------------------------------------------------
# 真实上游 golden（rev gate BLOCKER-1：权威源①真进程 HTTP handler 生成）
# ---------------------------------------------------------------------------

REAL_UPSTREAM_BINARY = "/home/mar/.opencode/bin/opencode"
REAL_UPSTREAM_VERSION = "1.18.18"
REAL_GOLDEN_GENERATOR = f"real-upstream-http-{REAL_UPSTREAM_VERSION}"
REAL_GOLDEN_PATH = GOLDEN_DIR / f"sessions-global-real-{ALIGNED_VERSION}.json"

# 服务端赋值字段：真实实例自定 id / 时间戳 / 目录为绝对路径 / project 解析
# （这些字段跨环境不可复现——日常 CI 对它们降级为「存在性 + 排序一致性」，
# 其余字段仍全量比对；EQ-007 真实运行时全字段含时间戳精确比对）。
REAL_SERVER_ASSIGNED_FIELDS = [
    "id",
    "time_created",
    "time_updated",
    "directory",
    "project_id",
    "project",
]

REAL_GOLDEN_HINT = (
    "OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN=1 .venv/bin/python -m pytest "
    "tests/test_equivalence_anchor.py -k eq007_real_golden "
    "（需真实 opencode 1.18.18 发布二进制）"
)

# golden 载荷的权威查询面（builder 与 validator 共用——漂移即校验失败）
REAL_GOLDEN_QUERY = {
    "endpoint": "GET /experimental/session?archived=true&limit=1000",
    "sort": "time_updated DESC, id DESC",
}

# rev gate BLOCKER-2c：SQL 富化写入的列（值 = fixture 派生，非零且行间
# 互异——防「全零错列仍通过」）。provenance 头记录（2d）。
SQL_ENRICHED_FIELDS = [
    "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write",
    "summary_additions", "summary_deletions", "summary_files",
    "summary_diffs", "revert", "time_compacting",
]

# 富化替代值：ses_bad_json 的 summary_diffs 是「坏 JSON」容忍样本——真库
# 写入坏 JSON 会让上游 drizzle 读路径炸掉整列表（§8 跳行是 sidecar 侧
# 容忍语义，该维度由 fixture 面 + EQ-008 锚定）；真实面以 None 代。
REAL_ENRICH_SUBSTITUTES: dict[str, dict[str, Any]] = {
    "ses_bad_json": {"summary_diffs": None},
}

REAL_ENRICHMENT_NOTE = (
    "sql-enriched via direct UPDATE on test-owned ephemeral instance "
    "(fixture-derived values; tokens/summary/revert/time_compacting); "
    f"substitutes: {json.dumps(REAL_ENRICH_SUBSTITUTES, sort_keys=True)}"
)


def build_real_golden_document(
    sessions: Sequence[dict[str, Any]],
    injected: Sequence[dict[str, Any]],
    *,
    opencode_version: str,
    generated_at: str,
) -> dict[str, Any]:
    """真实 handler golden（GET /experimental/session?archived=true 全量）。

    ``sessions``：真实 HTTP GlobalInfo 经比较层归一的记录（time_updated
    DESC, id DESC 序——上游 listGlobal 冻结排序）。

    ``injected``：注入清单 ``{fixture_id, real_id, fixture_directory,
    real_directory, agent, model, permission}``——fixture 语义 ↔ 真实值
    的桥（CI 降级校验用它做非 server-assigned 字段的全量比对 + parent
    链接一致性；agent/model/permission 桥 = 条目自身记录的 API 注入值）。

    provenance 头额外记录 SQL 富化方法（rev gate 2d）。
    """
    manifest = dataset_manifest()
    return {
        "version": ALIGNED_VERSION,
        "generator": REAL_GOLDEN_GENERATOR,
        "generated_at": generated_at,
        "opencode_version": opencode_version,
        "dataset_digest": manifest["digest"],
        "dataset_manifest": manifest,
        "server_assigned_fields": list(REAL_SERVER_ASSIGNED_FIELDS),
        "sql_enriched_fields": list(SQL_ENRICHED_FIELDS),
        "enrichment": REAL_ENRICHMENT_NOTE,
        "regenerate_hint": REAL_GOLDEN_HINT,
        "injected_sessions": list(injected),
        "response_fingerprint": response_fingerprint(sessions),
        "query": dict(REAL_GOLDEN_QUERY),
        "sessions": list(sessions),
    }


def _canonical(value: Any) -> str:
    """比较桥：tuple/list 与 dict 键序差异归一（canonical JSON 文本）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# R5 BLOCKER-2：真实 golden 顶层 provenance 键集**冻结**——生成器增删键 →
# 校验失败（防止生成器悄悄扩面/缩面绕过校验链）。
REAL_GOLDEN_TOP_LEVEL_KEYS = frozenset({
    "version", "generator", "generated_at", "opencode_version",
    "dataset_digest", "dataset_manifest", "server_assigned_fields",
    "sql_enriched_fields", "enrichment", "regenerate_hint",
    "injected_sessions", "response_fingerprint", "query", "sessions",
})


def _expected_enriched(row: dict[str, Any]) -> dict[str, Any]:
    """fixture 行 → SQL 富化写入真库的期望值（含替代值）。

    summary/revert 置性镜像上游 fromRow（session.ts:59-83）：三列全
    null → summary undefined；revert JSON 非空才暴露。
    """
    fid = row["id"]
    diffs_raw = REAL_ENRICH_SUBSTITUTES.get(fid, {}).get(
        "summary_diffs", row["summary_diffs"]
    )
    diffs = json.loads(diffs_raw) if isinstance(diffs_raw, str) else diffs_raw
    summary = (
        None
        if row["summary_additions"] is None
        and row["summary_deletions"] is None
        and row["summary_files"] is None
        else [
            row["summary_additions"], row["summary_deletions"],
            row["summary_files"], diffs,
        ]
    )
    revert = None
    if row["revert"]:
        doc = json.loads(row["revert"])
        revert = [
            doc.get("messageID"), doc.get("partID"),
            doc.get("snapshot"), doc.get("diff"),
        ]
    return {
        "tokens": [
            row["tokens_input"], row["tokens_output"],
            row["tokens_reasoning"], row["tokens_cache_read"],
            row["tokens_cache_write"],
        ],
        "summary": summary,
        "revert": revert,
        "time_compacting": row["time_compacting"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
    }


def validate_real_golden_ci(document: dict[str, Any]) -> list[str]:
    """日常 CI（无真进程）对真实 golden 的校验；返回问题清单。

    rev gate BLOCKER-1b 增强后三层覆盖：
    - **载荷身份**：按 canonical sessions **重算 response_fingerprint**
      并核对——任何 sessions 字段篡改（含 tokens/project/metadata）先在
      这层失败；
    - **provenance 全键**：version/generator/opencode_version/
      generated_at/dataset_digest（= 当前 manifest）/dataset_manifest
      摘要一致/query 端点+排序/sql_enriched_fields/regenerate_hint/
      server_assigned_fields/injected 清单 ↔ 可注入 fixture 行集；
    - **稳定语义字段全量**（server-assigned 之外逐行比对）：title、
      archived 置性+值、parent 链接、directory、tokens 五列、summary
      族、revert、time_compacting、metadata（= SQL 富化写入的 fixture
      派生值）；agent/model/permission（= 注入清单条目记录的 API 注入值）。
    """
    problems: list[str] = []
    # R5 BLOCKER-2：顶层 provenance 键集冻结（增删键 → 失败）
    key_drift = set(document) ^ set(REAL_GOLDEN_TOP_LEVEL_KEYS)
    if key_drift:
        problems.append(f"顶层键集漂移：{sorted(key_drift)}")
    if document.get("version") != ALIGNED_VERSION:
        problems.append(f"version mismatch: {document.get('version')!r}")
    if document.get("generator") != REAL_GOLDEN_GENERATOR:
        problems.append(f"generator mismatch: {document.get('generator')!r}")
    if document.get("opencode_version") != REAL_UPSTREAM_VERSION:
        problems.append(
            f"opencode_version mismatch: {document.get('opencode_version')!r}"
        )
    manifest = dataset_manifest()
    if document.get("dataset_digest") != manifest["digest"]:
        problems.append("dataset_digest mismatch（数据集与 golden 漂移，需再生成）")
    # R5 BLOCKER-2：dataset_manifest **全字典相等**（不只 digest——
    # sessions/projects 计数篡改必须检出）。
    if document.get("dataset_manifest") != manifest:
        problems.append(
            f"dataset_manifest 全字典不等：file={document.get('dataset_manifest')!r}"
        )
    # R5 BLOCKER-2：generated_at 必须是**带时区的 ISO 时间戳**
    # （fromisoformat 解析 + tzinfo 非空——拒 "T" 字样伪 ISO 串）。
    generated_at = document.get("generated_at")
    parsed_at = None
    if isinstance(generated_at, str):
        try:
            parsed_at = datetime.fromisoformat(generated_at)
        except ValueError:
            parsed_at = None
    if parsed_at is None or parsed_at.tzinfo is None:
        problems.append(f"generated_at 缺失或非带时区 ISO: {generated_at!r}")
    if document.get("query") != REAL_GOLDEN_QUERY:
        problems.append(f"query 漂移: {document.get('query')!r}")
    if document.get("sql_enriched_fields") != SQL_ENRICHED_FIELDS:
        problems.append("sql_enriched_fields 声明漂移")
    # R5 BLOCKER-2：hint / enrichment 与生成器常量对账相等（非空即可的
    # 弱校验改为字面冻结——篡改可检出）。
    if document.get("regenerate_hint") != REAL_GOLDEN_HINT:
        problems.append("regenerate_hint 与 REAL_GOLDEN_HINT 常量不等")
    if document.get("enrichment") != REAL_ENRICHMENT_NOTE:
        problems.append("enrichment 与 REAL_ENRICHMENT_NOTE 常量不等")
    if document.get("server_assigned_fields") != REAL_SERVER_ASSIGNED_FIELDS:
        problems.append("server_assigned_fields 声明漂移")
    sessions = document.get("sessions") or []
    if sessions and document.get("response_fingerprint") != response_fingerprint(sessions):
        problems.append("response_fingerprint 校验失败（载荷篡改或漂移）")
    injected = document.get("injected_sessions") or []
    if not injected or not sessions:
        problems.append("injected_sessions / sessions 空")
        return problems
    if len(sessions) != len(injected):
        problems.append(
            f"sessions({len(sessions)}) != injected({len(injected)}) 行数漂移"
        )
    fixture_by_id = {row["id"]: row for row in DATASET}
    injected_by_fixture = {e["fixture_id"]: e for e in injected}
    if set(injected_by_fixture) != {
        row["id"] for row in DATASET if row.get("directory")
    }:
        problems.append("injected 清单与可注入 fixture 行集不一致")
    real_to_fixture = {e["real_id"]: e["fixture_id"] for e in injected}
    if len(real_to_fixture) != len(injected):
        problems.append("real_id 重复")
    # 排序一致性：time_updated (, id) DESC 单调不增
    keys = [(s.get("time_updated"), s.get("id")) for s in sessions]
    if any(a is None or b is None for a, b in keys):
        problems.append("server-assigned 字段缺失（time_updated / id 存在性）")
    elif keys != sorted(keys, reverse=True):
        problems.append("sessions 序违反 time_updated DESC, id DESC")
    for entry in sessions:
        rid = entry.get("id")
        fid = real_to_fixture.get(rid)
        if fid is None:
            problems.append(f"未知 real_id {rid!r}")
            continue
        row = fixture_by_id[fid]
        inj = injected_by_fixture[fid]
        # --- 经注入清单桥接的稳定语义字段 -----------------------------
        if entry.get("title") != row.get("title"):
            problems.append(f"{fid}: title 全量比对失败")
        arch_golden = entry.get("time_archived")
        if (arch_golden is not None) != (row.get("time_archived") is not None):
            problems.append(f"{fid}: archived 置性不一致")
        if (arch_golden is not None) and arch_golden != row.get("time_archived"):
            problems.append(f"{fid}: time_archived 值不一致")
        parent = entry.get("parent_id")
        if (parent is not None) != (row.get("parent_id") is not None):
            problems.append(f"{fid}: parent 链接置性不一致")
        if parent is not None and real_to_fixture.get(parent) != row.get("parent_id"):
            problems.append(f"{fid}: parent 链接映射不一致")
        if entry.get("directory") != inj["real_directory"]:
            problems.append(f"{fid}: directory 与注入清单不一致")
        # --- SQL 富化字段（fixture 派生值全量比对）-------------------
        expected = _expected_enriched(row)
        for field in ("tokens", "summary", "revert", "time_compacting", "metadata"):
            if _canonical(entry.get(field)) != _canonical(expected[field]):
                problems.append(f"{fid}: {field} 与 fixture 派生富化值不一致")
        # --- API 注入字段（桥 = 注入清单条目自身记录）----------------
        if entry.get("agent") != inj.get("agent"):
            problems.append(f"{fid}: agent 与注入值不一致")
        if _canonical(entry.get("model")) != _canonical(inj.get("model")):
            problems.append(f"{fid}: model 与注入值不一致")
        if _canonical(entry.get("permission")) != _canonical(inj.get("permission")):
            problems.append(f"{fid}: permission 与注入值不一致")
    return problems


def load_real_golden() -> dict[str, Any]:
    """无条件装载真实 golden 并跑 CI 校验（BLOCKER-1a）。

    **不依赖**真实二进制 / ``real_upstream`` fixture——二进制缺席只影响
    真进程测试，golden 权威校验永不被 skip。
    """
    assert REAL_GOLDEN_PATH.is_file(), (
        f"真实 golden 缺失：{REAL_GOLDEN_PATH}（再生成：{REAL_GOLDEN_HINT}）"
    )
    document = json.loads(REAL_GOLDEN_PATH.read_text(encoding="utf-8"))
    problems = validate_real_golden_ci(document)
    assert not problems, f"真实 golden CI 校验失败：{problems}"
    return document


def build_db_from_real_golden(
    document: dict[str, Any], db_path: Path | str
) -> Path:
    """从真实 golden 内容重建确定性 DB fixture（BLOCKER-1c）。

    server-assigned 字段（id / 时间戳 / directory / project 解析）**显式
    映射写入** fixture 行——EQ-001..006 以 real golden 为期望对生产
    ``fetch_sessions_page`` 做投影等价断言（mirror oracle 降为辅助）。
    归一形状（tuple/list 混存）→ DDL 列值的逆映射在此集中。
    """
    project_rows: dict[str, dict[str, Any]] = {}
    session_rows: list[dict[str, Any]] = []
    for s in document["sessions"]:
        project = s.get("project")
        if project is not None:
            project_rows[project[0]] = {
                "id": project[0], "name": project[1], "worktree": project[2],
            }
        # R5 MAJOR-1：project_id **独立字段恢复** FK——不从 project 三元组
        # 反推（两字段语义独立：project_id 非空 + project=null 同现 =
        # orphan 维度；golden 每行独立保存该字段）。
        project_id = s.get("project_id")
        tokens = s.get("tokens") or (None,) * 5
        summary = s.get("summary")
        if summary is None:
            additions = deletions = files = diffs = None
        else:
            additions, deletions, files, diffs = (
                list(summary) + [None] * (4 - len(summary))
            )[:4]
        revert = s.get("revert")
        revert_doc = None
        if revert is not None:
            revert_doc = {
                k: v for k, v in zip(
                    ("messageID", "partID", "snapshot", "diff"), list(revert))
                if v is not None
            }
        permission = s.get("permission")
        model = s.get("model")
        metadata = s.get("metadata")
        session_rows.append({
            "id": s["id"],
            "project_id": project_id,
            "parent_id": s.get("parent_id"),
            "directory": s["directory"],
            "title": s["title"],
            "version": s["version"],
            "summary_additions": additions,
            "summary_deletions": deletions,
            "summary_files": files,
            "summary_diffs": (
                json.dumps(diffs) if isinstance(diffs, (dict, list)) else diffs
            ),
            "revert": json.dumps(revert_doc) if revert_doc else None,
            "permission": (
                json.dumps([
                    {"permission": a, "pattern": b, "action": c}
                    for a, b, c in (list(r) for r in permission)
                ]) if permission else None
            ),
            "time_created": s["time_created"],
            "time_updated": s["time_updated"],
            "time_compacting": s.get("time_compacting"),
            "time_archived": s.get("time_archived"),
            "agent": s.get("agent"),
            "model": (
                json.dumps({
                    k: v for k, v in zip(("id", "providerID", "variant"),
                                         list(model) + [None] * (3 - len(model)))
                    if v is not None
                }) if model else None
            ),
            "tokens_input": tokens[0],
            "tokens_output": tokens[1],
            "tokens_reasoning": tokens[2],
            "tokens_cache_read": tokens[3],
            "tokens_cache_write": tokens[4],
            "metadata": json.dumps(metadata) if metadata is not None else None,
        })
    return build_fixture_db(
        db_path, session_rows=session_rows,
        project_rows=list(project_rows.values()),
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--write-golden" in argv:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        document = build_golden_document()
        GOLDEN_PATH.write_text(
            json.dumps(document, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {GOLDEN_PATH} ({len(document['sessions'])} sessions)")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
