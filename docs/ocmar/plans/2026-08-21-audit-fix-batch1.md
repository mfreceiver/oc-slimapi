# oc-slimapi 审计整改批次一实施计划（L1–L4 并行 fixer-glm + 编排者收尾）— rev4

> **For agentic workers:** 本计划由编排者（omni-orch）调度执行：rev-cgpt 门控通过后，L1–L4 四条泳道并行派发 fixer-glm（写域互斥），完成后编排者统一收尾（CHANGELOG + 契约/文档修订 + check.sh + 终验）。步骤用 checkbox（`- [ ]`）追踪。
> rev2（2026-08-21）：按 rev-cgpt R1 评审修订——B1 重写预算测试与新不变量、B2 snapshotter 构造参数方案、B3 身份条件清引用、B4 生产 app 装配测试、B5 弃 metrics wire 改内部计数+采样日志、B6 共享有界 helper、B7 docs/specs 写域全收编排者、B8 路径纠正；N1–N6 一并落实。
> rev3（2026-08-21）：按 R2 评审修订——X1 I2 峰值不变量分段精确表述（M≥N 严格 N×share≤M / M<N 地板段松弛 ≤N）+ 极端小配置边界测试；X2 `_remove_hub_after_grace` 函数体内三处直接清引用（:292/:315/:325）统一改走身份条件 helper + 外部同步清引用路径豁免说明 + 代码级断言用例；X3 record_qp_activity 改 activity-LRU（更新既有键先删后插移队尾）+ 重复更新不被驱逐测试；X4 snapshotter 构造锚点勘误（app.py:291-295 构造，非 :720-722 的 start()）。
> rev4（2026-08-21）：按 R3 评审修订 X1-N1——裁决取「地板段接受 min(N,M) 个正 cap 启动」：I1 改精确表述（严格段 M≥N 全 N 启动；地板段 M<N 串行最坏启动数恰为 min(N,M)，承诺为防独占而非全启动）；地板测试改断言 full_calls==3（旧代码该场景 full_calls==1，防独占可观察）；删除纯公式重算测试（同表达式重算无验证力），严格段上界改由 e2e 峰值测试实证。

**Goal:** 修复 2026-08-20 全面审计（docs/audits/2026-08-20/）入选批次一的 12 项发现：P1×3（F-001/F-004/F-006）+ P2×8（F-007/F-008/F-009/F-011/F-015/F-025/F-137/F-216）+ P3×1（F-273）。

**Architecture:** 四条写域互斥的并行泳道 + 编排者收尾。L1=SSE/qp 域，L2=merged 预算，L3=关停/磁盘/装配/部署，L4=sessions 错误形状测试。**一切 `docs/specs/**` 修订（含 INTERFACE_MAP.md、design-v2.md、v4-contract.md）与 CHANGELOG.md 只由编排者收尾阶段执行。**

**Tech Stack:** Python 3.11 / FastAPI / httpx / pytest（asyncio auto）。

## Global Constraints

- **基线**：commit `77e6c49`（main HEAD）。发现证据锚点基于 `0b836e7` 快照，行号在当前 HEAD 有效；泳道执行时以符号名优先定位，行号漂移时以最近上下文为准。
- **校验**：`./scripts/check.sh` 收尾阶段由编排者跑一次全量；各泳道只跑自己的定向 pytest（命令见各任务），禁止跑全量。
- **fixer 禁改**：`CHANGELOG.md`、`docs/specs/**`（全部，含 INTERFACE_MAP.md/design-v2.md/v2-contract.md/v3-contract.md/v4-contract.md）、`pyproject.toml`、git 任何操作。发现 docs 回声需修改时，在完成报告中列出精确 file:line 与建议文本，由编排者收尾执行。
- **写域白名单**（并行互斥；qp_sweep.py 归 L1 独占）：
  - L1：`src/oc_slimapi/sse/hub_types.py`、`src/oc_slimapi/sse/global_hub.py`、`src/oc_slimapi/sse/registry.py`、`src/oc_slimapi/qp_sweep.py`、`tests/test_hub_behavior_lock.py`、`tests/test_global_hub_dropped_events.py`（新）、`tests/test_registry_grace_removal.py`（新）、`tests/test_qp_tables_bounded.py`（新）、`tests/test_b1b_sweep_shadow.py`
  - L2：`src/oc_slimapi/routes/messages.py`、`tests/test_messages_merged.py`
  - L3：`src/oc_slimapi/app.py`、`src/oc_slimapi/access_log.py`、`src/oc_slimapi/traffic_snapshot.py`、`deploy/oc-slimapi.service`、`docs/operations.md`、`docs/manual/traffic-accounting.md`、`docs/release.md`、`tests/test_app_main.py`、`tests/test_access_log.py`、`tests/test_app_assembly.py`（新）、`tests/test_traffic_snapshot*.py`（新或既有）
  - L4：`tests/test_sessions_v4_matrix.py`
- **行为语义红线**：不改任何 200 路径的既有 wire 形状；merged 修复保持契约 §4a.5「预算耗尽项保留 skeleton」降级语义；`/slimapi/metrics` wire 形状零改动（F-216 改内部观测，见 L1-2）。
- **门禁**：本计划经 rev-cgpt 审阅 ≥9.0 PASS 后才派发泳道。

---

## 泳道 L1（fixer-glm #1）：SSE 事件面 + hub/registry/qp 表

覆盖：F-001（P1）、F-216（P2）、F-011（P2）、F-015（P2）+ F-273（P3）+ F-007 半（qp_sweep 循环守卫）。

### Task L1-1：F-001 幽灵事件名纠正（wire 修复）

**Files:** `src/oc_slimapi/sse/hub_types.py:73-77`、`tests/test_hub_behavior_lock.py`

**语义依据**（审计 F-001 复核记录）：上游 v1.18.18 实际发布 `permission.replied`（packages/schema/src/v1/permission.ts:61-65）与 `permission.v2.replied`（packages/schema/src/permission.ts:43-45）；sidecar 现拼写的 `permission.resolved`/`permission.v2.resolved` 上游全树零命中——幽灵名从未对真实事件生效，改名**无消费方迁移成本**。v3-contract §7.2 未逐成员枚举 IMMEDIATE 集，**无需契约修订**，CHANGELOG 记行为修复（编排者收尾）。

- [ ] **Step 1：改 IMMEDIATE 集**——`hub_types.py:73-77`：`"permission.resolved"` → `"permission.replied"`；`"permission.v2.resolved"` → `"permission.v2.replied"`（集合其余成员与顺序不变；邻近注释提及旧名处一并更正）。
- [ ] **Step 2：更新既有锁定测试**——`tests/test_hub_behavior_lock.py:888-918` 的合成事件输入 `permission.resolved`/`permission.v2.resolved` → 改为 replied 双成员（该测试锁「IMMEDIATE 成员即转发」机制，改名后锁同一机制）。
- [ ] **Step 3：新增对齐上游真值的回归测试**（同文件）：

```python
async def test_permission_replied_upstream_name_forwarded(upstream_factory):
    """F-001 回归：真实上游事件名 permission.replied / permission.v2.replied 必须 IMMEDIATE 直推。"""
    # 订阅 /slimapi/events 后注入 {"type": "permission.replied", "directory": ...} → 收到该帧（类型原样）；
    # permission.v2.replied → 收到；
    # 反向：注入幽灵旧名 permission.resolved → 不产生直推帧（落 catch-all，计入 L1-2 丢弃计数）。
```

（沿用本文件既有订阅/注入工具；断言帧到达与否，不断言时序。）
- [ ] **Step 4：残留扫描与报告（不修改）**——`grep -rn "permission.resolved\|permission.v2.resolved" src/ tests/ docs/specs/v3-contract.md docs/specs/v4-contract.md docs/specs/INTERFACE_MAP.md docs/specs/design-v2.md`；**扫描范围明确排除** `docs/specs/v2-contract.md`（历史契约冻结存档，禁止改写）、`CHANGELOG.md` 历史条目、`docs/audits/**`（审计归档）。src/tests 命中处必须修复；docs/specs 命中处（预期 INTERFACE_MAP.md:81、design-v2.md:84）**只记录到完成报告**（file:line + 建议改文），由编排者收尾修改。
- [ ] **Step 5：定向验证**——`.venv/bin/python -m pytest tests/test_hub_behavior_lock.py -x -q` 绿；`grep -rn "permission.resolved\|permission.v2.resolved" src/ tests/` 零命中（golden 目录若命中且为事件名快照则同步更新并注明）。

**Acceptance：** L1-1-C1 真名双成员直推通过；L1-1-C2 幽灵旧名不再直推；L1-1-C3 src/tests 零残留 + docs/specs 命中清单入报告；L1-1-C4 定向 pytest 绿。

### Task L1-2：F-216 catch-all 丢弃计数（内部观测，wire 零改动）

**Files:** `src/oc_slimapi/sse/global_hub.py`（catch-all :975、`__init__` :146 附近）、`tests/test_global_hub_dropped_events.py`（新）

**设计决策（固定）**：`/slimapi/metrics` 的 `snapshot_metrics()` 形状是 v3-contract §9.2 冻结的严格形状，本批**不加 wire 字段**。观测落地面 = 有界 per-type 内部计数 + **限率采样日志**（≤1 条/60s，仅在有新增丢弃时打）——journald 可查、零契约面。metrics wire 暴露列为后续 backlog（需 §9.2 加性契约修订，owner 裁决）。

- [ ] **Step 1：计数器**——`GlobalHub.__init__`（`upstream_events_total` 声明处附近）：

```python
        self.upstream_dropped_events_total: dict[str, int] = {}
        self._DROPPED_TYPES_MAX = 256          # 上游事件类型全集 ~89，防御性上界
        self._dropped_last_log_ts = 0.0
        self._dropped_since_log = 0
```

catch-all 分支（`# Drop text deltas, tool.*, ...` 处，`event_type` 在作用域内）：

```python
            if event_type in self.upstream_dropped_events_total or len(self.upstream_dropped_events_total) < self._DROPPED_TYPES_MAX:
                key = event_type
            else:
                key = "__other__"              # 类型基数防御上界（N4）
            self.upstream_dropped_events_total[key] = self.upstream_dropped_events_total.get(key, 0) + 1
            self._dropped_since_log += 1
            now = time.time()
            if now - self._dropped_last_log_ts >= 60.0:
                top = sorted(self.upstream_dropped_events_total.items(), key=lambda kv: -kv[1])[:8]
                logger.info("upstream dropped events (top): %s", top)
                self._dropped_last_log_ts = now
                self._dropped_since_log = 0
```

（logger 用本文件既有 logger 获取方式；采样频率 60s 常量写死并注释——热路径每帧只做 dict 增量与一次 time.time 比较。）
- [ ] **Step 2：测试**（新文件）：

```python
async def test_catchall_drop_counted_per_type(...):
    # 注入未知类型 "todo.updated" ×2、"file.edited" ×1 →
    # assert hub.upstream_dropped_events_total == {"todo.updated": 2, "file.edited": 1}
async def test_curated_events_not_counted(...):
    # IMMEDIATE/SESSION/MESSAGE 族各一帧 → dropped 计数不变
async def test_dropped_type_cardinality_bounded(...):
    # monkeypatch _DROPPED_TYPES_MAX=2 → 注入 3 个未知类型 → dict 键集 = {前两个, "__other__"}，__other__ == 1
```

- [ ] **Step 3：定向验证**——`.venv/bin/python -m pytest tests/test_global_hub_dropped_events.py -x -q` 绿；确认未新增 HTTP 端点、`snapshot_metrics()` 未改动（`git diff src/oc_slimapi/sse/registry.py` 中 snapshot_metrics 无变化）。

**Acceptance：** L1-2-C1 逐型计数正确；L1-2-C2 策展内事件不计；L1-2-C3 类型基数有界（`__other__` 桶）；L1-2-C4 wire/metrics 形状零改动；L1-2-C5 定向 pytest 绿。

### Task L1-3：F-011 registry 宽限拆除兜底（身份条件清引用）

**Files:** `src/oc_slimapi/sse/registry.py:183-185,258-325`、`tests/test_registry_grace_removal.py`（新）

**设计（B3 修订）**：无条件 `finally: self._removal_task = None` 会在「旧 task 被取消 → 立即重新 arm 新 task → 旧 task 的 finally 晚于新 task 创建执行」时误清新 task 引用。清引用必须**身份条件化**。

- [ ] **Step 1：实现**——新增辅助方法 + 改造 `_remove_hub_after_grace`：

```python
    def _clear_removal_task_if_current(self) -> None:
        if self._removal_task is asyncio.current_task():
            self._removal_task = None
```

```python
    async def _remove_hub_after_grace(self, hub: GlobalHub) -> None:
        try:
            await asyncio.sleep(GRACE_SECONDS)
        except asyncio.CancelledError:
            self._clear_removal_task_if_current()
            return
        try:
            ...（拆除体：gather/cancel/close/on_upstream_reconnect/置 _global=None——
            其中函数体内原有三处直接 `_removal_task = None` 赋值（registry.py:292、
            :315、:325，含 gather 被 cancel 的提前返回分支与正常完成尾）**全部删除**，
            一律改调 `self._clear_removal_task_if_current()`——X2：函数体内一切
            清引用统一走身份条件路径，禁止残留任何裸赋值）
        except Exception:
            <本文件既有 logger>.warning("hub grace removal failed", exc_info=True)
        finally:
            self._clear_removal_task_if_current()
```

task 创建处（:185）：`self._removal_task = asyncio.create_task(self._remove_hub_after_grace(hub), name="hub-grace-removal")`。
**不改**的外部清引用路径（同步 cancel+clear 之间无 await、事件循环步内原子，clear 的是自己刚 cancel 的 task，身份条件不适用且安全）：registry.py:157-159（`cancel_pending_removal`）、:392-394 与 :400-402（`close()` 关停路径）。
- [ ] **Step 2：测试**（新文件）：
  - 用例 1（收尾异常恢复）：monkeypatch `_token_hub.on_upstream_reconnect` 抛 RuntimeError → 宽限到期后 `_removal_task is None` 且再次 `maybe_arm_grace_if_idle` 能 arm 新 task；
  - 用例 2（B3 竞态回归）：arm task1 → 取消 task1（走 :145-160 取消路径，其内引用已清并 arm task2）→ `await task1`（其 finally 执行）→ 断言 `registry._removal_task is task2`（旧 task 的 finally 未误清新引用）；
  - 用例 3：正常拆除路径 `_global is None`、引用清除（既有语义回归）；
  - 用例 4（代码级断言，X2）：`inspect.getsource(HubRegistry._remove_hub_after_grace)` 文本中 `_removal_task = None` 裸赋值零命中（一切清引用必经 `_clear_removal_task_if_current`）。
- [ ] **Step 3：定向验证**——`.venv/bin/python -m pytest tests/test_registry_grace_removal.py -x -q` 绿 + 既有 registry/生命周期相关测试（`tests/test_batch3_lifecycle.py`）不红。

**Acceptance：** L1-3-C1 收尾异常后引用必清且 arming 恢复；L1-3-C2 旧 task 取消竞态不清新 task 引用；L1-3-C3 正常路径语义不变；L1-3-C4 函数体内裸清引用零残留（用例 4 代码级断言）。

### Task L1-4：F-015+F-273 qp 活动表有界 + 逐出连带（双写点覆盖）

**Files:** `src/oc_slimapi/sse/hub_types.py`（新 helper）、`src/oc_slimapi/sse/global_hub.py:109,678`、`src/oc_slimapi/qp_sweep.py:109-122,155-164,222-238`、`tests/test_qp_tables_bounded.py`（新）、`tests/test_b1b_sweep_shadow.py`

**设计（B6 修订）**：`qp_last_activity` dict 有**两个写点**（global_hub.py:678 IMMEDIATE 分支 + qp_sweep.py:113 `record_activity`，经 app.py:504-505 共享同一引用）——cap 逻辑必须封装为共享 helper，两写点统一走它。

- [ ] **Step 1：共享有界 helper**——`hub_types.py`（叶子模块）新增：

```python
QP_LAST_ACTIVITY_MAX = 10_000  # activity-LRU cap（对齐 sticky 表 P1-21 加固；审计 F-015）

def record_qp_activity(table: dict[str, float], directory: str, now: float) -> None:
    """Bounded activity-LRU write shared by both writers (global_hub + qp_sweep).

    驱逐序 = 最久未活动优先：更新既有目录先 pop 再插入（plain dict 保序，等价
    move-to-end）——持续活跃目录始终在队尾，不会被新键写入驱逐（X3）。
    """
    table.pop(directory, None)  # 先删后插 = move-to-end（activity-LRU，X3）
    table[directory] = now
    if len(table) > QP_LAST_ACTIVITY_MAX:
        table.pop(next(iter(table)))
```

`global_hub.py:678` → `record_qp_activity(self.qp_last_activity, directory, time.time())`（删原裸赋值；注释注 F-015）。`qp_sweep.py:113`（`record_activity`）→ `record_qp_activity(self._activity, directory, timestamp)`（其后的 `observe_directory` 调用保留）。`:109` 的裸声明 `self.qp_last_activity: dict[str, float] = {}` 保留（有界性由 helper 保证，注释指向 helper）。
- [ ] **Step 2：F-273 逐出连带**——`qp_sweep.py` `_evict_stale_directories`：逐出某 directory 时同时 `self._activity.pop(directory, None)`（`_activity` 与 hub 的 dict 同引用）。活跃目录（ingest 持续刷新 seen_at）不逐出为预期语义。**不改** `_ingest_directory_source`。
- [ ] **Step 3：F-007 半（qp_sweep 循环守卫）**——`qp_sweep.py` `_run` 循环体对 `run_once()` 包 `try/except Exception: <logger>.warning("qp sweep run_once failed", exc_info=True)` 后照常 sleep 续跑（CancelledError 向上传播不变）。
- [ ] **Step 4：测试**（新文件）：
  - cap（混合写点）：monkeypatch `QP_LAST_ACTIVITY_MAX`（经 hub_types 模块引用，两处 import 均需生效——直接改 `hub_types.QP_LAST_ACTIVITY_MAX` 并确认两个消费方是运行时查值或 monkeypatch 两处模块属性，以实现为准保证两写点都受限）→ 经 hub IMMEDIATE 写 3 个 + 经 `QpSweepShadow.record_activity` 写 2 个（共 > cap）→ 表长 == cap 且最老键被弹（**证明双写点都被 cap**）；
  - 活跃更新移尾（X3）：cap=3 下写入 d1/d2/d3 → 反复 `record_qp_activity(table, "d1", t)` 多次 → 写入 d4 → 被弹的是 **d2**（最久未活动）而非 d1——更新既有键必须移至队尾，活跃目录不被驱逐；
  - 逐出：`_activity` 含超期目录（seen_at 早于 30d）→ `run_once()` 后 `_known_dirs`/`_seen_at`/`_next_run`/`_activity` 四处键均消失；
  - 活跃保护：ingest 刷新 seen_at 的目录不被逐出；
  - 循环守卫：monkeypatch `run_once` 抛 RuntimeError → `_run` 存活并进入下一轮 sleep。
  - 回归：`tests/test_b1b_sweep_shadow.py` 全绿。
- [ ] **Step 5：泳道总验证**——`.venv/bin/python -m pytest tests/test_hub_behavior_lock.py tests/test_global_hub_dropped_events.py tests/test_registry_grace_removal.py tests/test_qp_tables_bounded.py tests/test_b1b_sweep_shadow.py tests/test_batch3_lifecycle.py -q` 全绿。

**Acceptance：** L1-4-C1 双写点混合超限均被 cap；L1-4-C2 超期目录四处连带逐出；L1-4-C3 活跃目录保留；L1-4-C4 run_once 异常不杀 task；L1-4-C5 泳道套件全绿。

---

## 泳道 L2（fixer-glm #2）：F-006 merged 预算均分预约

**Files:** `src/oc_slimapi/routes/messages.py:649-676`、`tests/test_messages_merged.py`

**设计与新不变量（B1 修订，固定）**：采用审计建议 (a) 均分预约。新不变量组：
- **I1 防饿死（分段精确表述，rev4 裁决：防独占 ≠ 极端配置全启动）**：每候选启动配额 `cap = min(max_message_bytes, remaining, share)`，`share = max(1, M // N)`（M=merged_max_bytes、N=len(pairs)）。**正值前提（rev-cgpt R4 N1）**：M、N、max_message_bytes 均 ≥ 1 时上式成立；`max_message_bytes` 的下界（≥1）校验当前 config.py 未设，列为后续小项（见文末 backlog），不在本批。**严格段 M ≥ N**（含生产默认 8MiB/16 槽）：`N × share ≤ M` ⇒ remaining 在第 N 个候选过闸前不耗尽 ⇒ **全部 N 个候选获得正 cap 启动**（无候选因「先到者独占」而零启动——旧缺陷机制消除）。**地板段 M < N**（极端小配置）：share 地板=1 ⇒ 串行最坏情形下恰有 **min(N, M)** 个候选各得 1B 配额启动（并发退款只增不减）——地板段承诺是**防独占**（旧代码首候选可独占全部 M 字节致其余 N−1 个零启动；新代码启动数从 1 提升到 min(N,M)），**不承诺全启动**（M 字节物理上无法给 N > M 个候选各分正配额）。
- **I2 峰值上界（分段精确表述，X1）**：设候选数 N、页预算 M=merged_max_bytes、share = max(1, M // N)。并发在飞预留总和 ≤ N × share；**M ≥ N 时 N × share ≤ M 严格成立**（生产默认 M=8MiB ≥ 16 槽，严格段）；M < N 的极端小配置下 share 地板=1 引入至多 N−M 字节松弛（总和 ≤ N）——本计划只在严格段承诺 ≤ M。
- **I3 退款记账**：预留-实读差额回补 `remaining`（代码保留；均分下页内不再是跨项启用条件，但保持记账正确供后续策略与累计 splice 判定）。
- **I4 降级语义（§4a.5 不变）**：超配额读取截断 → 该项 skeleton，页 200。

**已知代价（CHANGELOG 由编排者记录）**：单条 full body > share 的候选在均分制下截断降级（默认组合 share=512KiB/候选）；旧行为在该参数区是「第 1 条可独占 8MiB、其余 15 条必饿死」——以牺牲单条超额换取全员公平启动。

- [ ] **Step 1：预约公式**——`messages.py` `_fetch_one` 内：

```python
            cap = min(
                config.max_message_bytes,
                remaining[0],
                max(1, config.merged_max_bytes // max(1, len(pairs))),
            )
```

（`len(pairs)` 闭包外可得；单候选页 share=merged_max_bytes，行为与现状一致；注释注明 F-006 防饿死 + I2 峰值论证。）**refund/gather/splice 逻辑零改动。**
- [ ] **Step 2：重写预算测试一**——`test_merged_byte_budget_caps_fetch_buffers`（:497 附近，4 候选 / max_message_bytes=8000 / merged_max_bytes=10000 / body≈6KiB）→ 重命名为 `test_merged_budget_equal_share_all_start_and_peak_capped`，新断言：`full_calls == 4`（**旧代码 C/D 零请求——本断言即 F-006 防饿死回归**）；`inlined == 0`（每项在 share=2500 截断 → 全降级，I4）；`degraded == 4`；保留并发在飞 sleep（4×2500=10000 峰值论证，I2）。docstring 更新为新不变量。
- [ ] **Step 3：新增小体用例**（同参数组合）——`test_merged_budget_equal_share_small_bodies_all_inline`：body≈2000 < share=2500 → `inlined == 4`、`full_calls == 4`。
- [ ] **Step 4：重写预算测试二**——`test_merged_budget_refund_lets_serial_items_proceed`（:543 附近，8000/10000 组合）→ 重命名 `test_merged_serial_completion_no_starvation`：去掉 sleep 的同步完成场景（旧代码 A 完成退款后 B 才预约）→ 新断言：两项均启动均 inline（`inlined == 2`、`full_calls == 2`）——锁「启动保证与完成时序无关」（I1 在串行时序下成立）；docstring 注明 refund 记账保留理由（I3）。
- [ ] **Step 5：新增默认参数回归**（沿 `_settings` 基线，参照 :126-142）：
  - `test_merged_default_params_two_candidates_both_inline`：默认 32MiB/8MiB × 2 候选（各 ~4KiB）→ 双 inline（旧代码第 2 条必饿死）；
  - `test_merged_default_params_sixteen_candidates_page_cap`：默认参数 × 16 候选（各 ~10KiB）→ 16 条全 inline（16 槽语义默认组合可达）；
  - `test_merged_oversized_candidate_degrades_page_ok`：小 merged fixture（如 merged_max_bytes=6000，2 候选 share=3000），其一 body 4000 > share → 该项 skeleton、另一项 inline、页 200（I4）；
  - `test_merged_tiny_budget_floor_share_spread`（X1 边界，rev4 改断言）：merged_max_bytes=3、max_message_bytes=8、4 候选（body 各 10B，handler sleep 保持全程 in-flight 串行确定性）→ share=1、**full_calls == 3 == min(N, M)**（前 3 候选各得 1B 配额、第 4 候选 remaining=0 不发起请求——对照旧代码同场景首候选独占 3B 致 full_calls==1，防独占提升可观察）、4 项全部降级（3 截断 + 1 未启动）、页 200——锁地板段语义（I1 地板段承诺）；
  - ~~test_merged_share_formula_bounds~~（rev4 删除——纯公式重算无验证力，评审 X1-N1）：严格段 N×share ≤ M 上界改由上述重写的峰值测试**实证**（full_calls==4、4×2500=10000=M 在飞峰值）；地板段由 share_spread 测试实证。
- [ ] **Step 6：定向验证**——`.venv/bin/python -m pytest tests/test_messages_merged.py -q` 全绿（含未触及的既有用例——page-cap 用例 :250-258 pin 256KiB 组合下 cap=min(256KiB, …, 512KiB)=256KiB，行为不变，必须保持绿）。

**Acceptance：** L2-C1 `full_calls==4` 防饿死断言落地（I1）；L2-C2 峰值上界与启动数分段实证（严格段全 N 启动且 N×share=M 在飞峰值 / 地板段启动数==min(N,M) 且无独占）与全降级语义（I4）断言落地；L2-C3 默认参数 × 2/× 16 全 inline；L2-C4 超 share 候选降级页 200；L2-C5 实现改动恰好一处（cap 公式），refund/gather/splice 零改动；L2-C6 全文件 pytest 绿。

---

## 泳道 L3（fixer-glm #3）：关停隔离 + 磁盘生命周期 + 装配面 + 部署模板

覆盖：F-007（app 半）、F-008、F-009、F-137、F-004（+F-005 顺带）。

### Task L3-1：F-007 关停回调隔离

**Files:** `src/oc_slimapi/app.py:514-517`、`tests/test_app_main.py`

- [ ] **Step 1**：对齐同文件其余回调模式（对照 app.py:326-337 样式）：

```python
        async def _stop_qp_sweep():
            try:
                await app.state.qp_sweep.stop()
            except Exception as exc:
                get_logger("app").warning("qp sweep stop failed", exc_info=exc)
```

- [ ] **Step 2：测试**——`tests/test_app_main.py` 追加：构造 qp_sweep.stop 抛 RuntimeError 的 app 状态，触发关停 → 其后回调（upstream close / access-log flush 等 mock 记录）仍全部执行。
- [ ] **Step 3**：`.venv/bin/python -m pytest tests/test_app_main.py -q` 绿。

### Task L3-2：F-008 legacy 档案纳入保留窗口

**Files:** `src/oc_slimapi/access_log.py:79,566-570`、`docs/operations.md:230,239`、`docs/manual/traffic-accounting.md:186`、`tests/test_access_log.py`

- [ ] **Step 1：prune 分支**——新增 `_ACCESS_LEGACY_RE = re.compile(r"^access-legacy-(\d{4})(\d{2})(\d{2})-\d+\.jsonl\.gz$")`；prune 循环中 `_ACCESS_LOG_RE` 拒配的文件若命中 legacy RE → 按名内 `date(Y,M,D)` 计龄，超 `retain_days` 删除（与标准日名同一判据；`.tmp`/`.tmp.*` 排除逻辑不变）。
- [ ] **Step 2：文档同步**——operations.md:230/:239 与 traffic-accounting.md:186「永久保留/手动处理」→「legacy 档案按名内日期纳入 RETAIN_DAYS 同一保留窗口自动清理」。
- [ ] **Step 3：测试**——`access-legacy-20200101-1.jsonl.gz`（超窗）→ 删；`access-legacy-<today>-1.jsonl.gz` → 留；标准日名回归不红。
- [ ] **Step 4**：`.venv/bin/python -m pytest tests/test_access_log.py tests/test_access_log_v3_fields.py -q` 绿。

### Task L3-3：F-009 snapshot 清理自持（含配置传递方案）

**Files:** `src/oc_slimapi/traffic_snapshot.py`（`TrafficSnapshotter` :289-、`prune_old_snapshots` :79-101）、`src/oc_slimapi/app.py:291-295,655-674,720-722`（**291-295 = `TrafficSnapshotter(...)` 构造调用处**、720-722 = `start()` 调用处——X4 勘误）、`docs/operations.md` §5.3、`docs/manual/traffic-accounting.md` §6/§9.1、tests

**配置传递（B2 修订，冻结）**：
- `TrafficSnapshotter.__init__` 新增两参数：`retain_days: int = 0`、`today_fn: Callable[[], date] = date.today`；`__slots__` 追加 `"_retain_days"`、`"_today_fn"`。
- `_loop` **每 tick 顶部**（首次写入前同样执行）调用 `prune_old_snapshots(self._dir, self._stem, self._retain_days, self._today_fn())`，整体包 `try/except Exception: logger.warning(..., exc_info=True)`，失败不中断循环。
- app.py：在**构造调用处 app.py:291-295**（lifespan 内 `app.state.traffic_snapshotter = TrafficSnapshotter(...)`）追加参数 `retain_days=settings.traffic_snapshot_retain_days`（**不是** :720-722 的 `start()` 调用——X4）；**删除** :668-674 的 `extra_prune` partial 挂靠（`prune_old_snapshots` 唯一生产调用点移入 snapshotter）；:720-722 的 start() 调用保持不变。
- snapshotter 启动/构造条件不变（`traffic_snapshot_enabled and traffic_metrics_enabled`）。

- [ ] **Step 1：实现**上述三点。
- [ ] **Step 2：可观测性**——snapshotter 启动处打一条 info：「traffic snapshot retention is self-managed by the snapshotter loop (independent of ACCESS_LOG_ENABLED)」。
- [ ] **Step 3：文档同步**——operations.md §5.3 与 traffic-accounting.md §6/§9.1：snapshot 清理语义改为「snapshotter 每 tick 自持清理，不受 ACCESS_LOG_ENABLED 影响」。
- [ ] **Step 4：测试**：① 预置超窗旧快照 → **首个 tick** 即被清（tick 顶部语义）；② `ACCESS_LOG_ENABLED=false` + snapshot 开 → tick 后仍删（原缺陷场景）；③ prune 抛异常 → 循环存活下一 tick 正常；④ 关停路径回归（`tests/test_app_main.py`）。
- [ ] **Step 5**：`.venv/bin/python -m pytest tests/test_traffic_snapshot*.py tests/test_app_main.py -q` 绿。

### Task L3-4：F-137 关闭默认 docs/openapi 面（测生产 app）

**Files:** `src/oc_slimapi/app.py:734`、`tests/test_app_assembly.py`（新）

**测试对象（B4 修订）**：`tests/test_proxy.py` 的 `_build_app` 自建默认 FastAPI，不载生产配置——docs 断裂测试**必须打生产 app**。

- [ ] **Step 1**：`app = FastAPI(title="oc-slimapi", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)`。
- [ ] **Step 2：新测试文件** `tests/test_app_assembly.py`：

```python
async def test_default_docs_routes_disabled_on_production_app():
    """F-137：生产 app 的 /docs /redoc /openapi.json /docs/oauth2-redirect 必须落入 catch-all 404。"""
    from oc_slimapi.app import app
    # httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    # 不进入 lifespan（不发 startup）——catch-all 在 import 期已安装，404 不触上游。
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = await client.get(path)
        assert r.status_code == 404
        assert r.json()["code"] == "thin_route_not_found"   # 证明是 sidecar 边界帧，非上游响应
    r = await client.head("/docs")
    assert r.status_code == 404
```

（若既有测试基建已有生产 app 的 client fixture 则复用；`code` 字段断言即「上游零接触」证明——上游不可能返回该错误体。）
- [ ] **Step 3**：`.venv/bin/python -m pytest tests/test_app_assembly.py tests/test_proxy.py tests/test_check_routes_doc.py -q` 绿（未增删 /slimapi 路由，INTERFACE_MAP 一致性不受影响）；`tests/test_proxy.py` 本任务零改动。

### Task L3-5：F-004+F-005 部署模板与文档纠偏

**Files:** `deploy/oc-slimapi.service:32-33`、`docs/operations.md:92-94,122`、`docs/release.md`

- [ ] **Step 1**：deploy 模板删除 `:32`（`OC_SLIMAPI_SERVER_API_VERSION=2`）与 `:33`（`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`），原位替换一行注释：`# 版本窗自 4.0.0 起由代码钉死 (3,4)（config.validate fail-closed）——勿设置版本相关 env`。
- [ ] **Step 2**：operations.md:92-94 更正：「设置无效」→「**启动即 RuntimeError 拒绝**（fail-closed 钉死 (3,4)）」；「模板不再示例」改为与事实一致（模板已清理，版本 env 不可配置）。
- [ ] **Step 3**：`docs/release.md` 发版 checklist 追加：「deploy 模板 env 集 ⊆ config.py 读取 env 集，且值合法」。
- [ ] **Step 4**：`grep -n "2,2" deploy/ docs/operations.md` 零残留（无新测试——纯配置/文档）。

**Acceptance（L3）：** L3-C1 stop 抛错后续回调全执行；L3-C2 legacy 超窗删/窗内留；L3-C3 snapshot 首个 tick 即清 + 脱离 ACCESS_LOG 开关 + prune 异常存活；L3-C4 生产 app 四路径 GET + HEAD /docs 全 404 `thin_route_not_found`；L3-C5 deploy/operations/release 三处一致零 v2 env 残留；L3-C6 泳道定向 pytest 全绿。

---

## 泳道 L4（fixer-glm #4）：F-025 错误形状测试锁定（代码零改动）

**Files:** `tests/test_sessions_v4_matrix.py`（路径勘误 B8：路由是 **`/slimapi/sessions`**，sessions.py:678；v4-contract 修订由编排者收尾）

- [ ] **Step 1：扩展 501 用例**——`test_v4_limit_501_422_and_500_ok`（:471）追加：

```python
    assert resp_501.status_code == 422
    assert resp_501.json()["code"] == "param_version_mismatch"
    assert "v4 limit domain is 1.." in resp_501.json()["hint"]
```

- [ ] **Step 2：新增用例**（GET `/slimapi/sessions?…&v=4`；N6：只断言键存在性，不断言框架文案全文）：

```python
async def test_v4_limit_1001_framework_422_shape(...):
    """F-025：limit=1001 在 FastAPI 声明域外 → 框架 422（{"detail": [...]}，无 code）。"""
    # assert r.status_code == 422; body = r.json()
    # assert isinstance(body.get("detail"), list) and "code" not in body

async def test_v4_archived_invalid_coded_422(...):
    """F-025：archived 非三态值 → 422 param_version_mismatch（coded 形状）。"""

async def test_v4_parent_empty_coded_422(...):
    """F-025（N2）：parent="" → 422 param_version_mismatch（coded 形状）。"""
```

- [ ] **Step 3：v3 侧**——`test_v3_limit_1000_domain`（:575）追加 `isinstance(body.get("detail"), list)` 断言。
- [ ] **Step 4**：`.venv/bin/python -m pytest tests/test_sessions_v4_matrix.py -q` 绿；`git diff --stat` 确认零 src 改动、单测试文件。

**Acceptance：** L4-C1 四类形状断言（501 coded / 1001 框架 / archived 非法 coded / parent 空 coded）+ v3 detail 形状全部落地且绿；L4-C2 diff 仅一个测试文件；L4-C3 测试命中真实 sessions handler（非 404 路径——断言状态码 422 即证明）。

---

## 收尾阶段（编排者，泳道全部回收后）

- [ ] **S1 合并复核**：`git diff --stat` 对照四泳道白名单 + fixer 禁改清单；越界打回修正。
- [ ] **S2 CHANGELOG.md**：新增 `[Unreleased]` 节逐条记录（含 N3 补全）：
  - **Fixed（wire）**：SSE q/p IMMEDIATE 集拼写纠正 `permission.resolved`→`permission.replied`（含 v2 成员）——上游真实决议事件此前被静默丢弃（F-001）；幽灵名从未有真实帧上 wire，消费方零迁移。`question.*` 决议族维持不透传（策展边界不变，另行决策）。
  - **Fixed（行为）**：merged 预算预约改按候选数均分——默认参数组合不再确定性退化为每页至多 1 条 inline（F-006）；代价：单条 > 均分额（默认 512KiB/候选）截断降级为 skeleton（§4a.5 语义内）。`/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` 关闭 → 404 `thin_route_not_found`（F-137）。
  - **Added（观测，内部）**：上游事件 catch-all 丢弃 per-type 有界计数 + 60s 采样日志（F-216；metrics wire 面不变）。
  - **Fixed（内部/运维）**：关停回调隔离与 qp_sweep 循环守卫（F-007）、hub 宽限拆除兜底（F-011）、qp 活动表 FIFO 上界 + 逐出连带清理（F-015/F-273）、access-legacy 档案纳入 RETAIN_DAYS 窗口（F-008）、snapshot 清理自持脱离 ACCESS_LOG 开关（F-009）、deploy 模板移除残留 v2 版本 env（F-004/F-005）。
  - **Docs（契约澄清）**：v4-contract §4.1/§4.3/§8.1 补全 limit 域外/archived 非法/parent 空串错误归宿并命名 `param_version_mismatch`（F-025，行为零改动，测试锁定现状）。
- [ ] **S3 契约与文档回声修订**（编排者独占写域）：
  - v4-contract.md：§4.1 limit 行后补域外归宿句（501..1000 → 422 `param_version_mismatch`；>1000/≤0/非 int → 框架 422 `{"detail":[...]}`——**只冻结状态码 + detail 数组存在 + 无 code 字段**，不冻结框架文案，N6）；§4.3 422 触发枚举补全（+ limit 超域/archived 非法/parent 空串）；§8.1 错误表命名 code 字面量。不动 v3-contract.md。
  - INTERFACE_MAP.md:81 与 design-v2.md:84 的 `permission.resolved`/`permission.v2.resolved` 回声 → replied（按 L1 报告的 file:line 清单；v2-contract.md 历史契约不改）。
- [ ] **S4 全量校验**：`./scripts/check.sh` 全绿（pytest 全量 + 路由↔文档一致性——INTERFACE_MAP 改动只涉事件名文字，不涉路由表）。
- [ ] **S5 终验报告**：C 矩阵核对 + `git diff` 总览 + F-216 metrics 暴露 backlog 项登记；向 owner 汇报，等 owner 决定是否 `./scripts/release.sh`（编排者不自行发版）。

## Criterion Ownership Matrix

| Criterion | 发现 | Owner | 验证（命令+期望） |
|---|---|---|---|
| L1-1-C1..C4 | F-001 | L1 | 定向 pytest 绿 + grep 零残留 + 报告清单 |
| L1-2-C1..C5 | F-216 | L1 | 新测试绿 + snapshot_metrics 零改动 |
| L1-3-C1..C3 | F-011 | L1 | 新测试（含取消竞态用例）绿 |
| L1-4-C1..C5 | F-015/F-273/F-007(半) | L1 | 新测试（含双写点混合）绿 |
| L2-C1..C6 | F-006 | L2 | test_messages_merged.py 全绿（含重写×2 + 新增×4） |
| L3-C1..C6 | F-007(半)/F-008/F-009/F-137/F-004 | L3 | 泳道定向 pytest + grep |
| L4-C1..C3 | F-025 | L4 | matrix 测试绿 + diff 单文件 |
| S1..S5 | 全部 + 文档回声 | 编排者 | check.sh 全绿 + CHANGELOG/契约落盘 |

## 明确不在本批（后续批次候选）

F-251（E-II 面收敛，依赖部署边界决策）、F-252（allowlist 提升全局门，语义变更需 owner 裁决）、F-339（runbook 大规模增补）、F-010（关停超时对齐）、F-201/F-271（gzip/sha256 offload）、F-301/F-302（大拆分）、F-017（providers v3 敏感面，契约裁决项）、F-006 后续 (b) 退款驱动重试、F-001 同族 question.* 策展决策、F-216 metrics wire 暴露（需 §9.2 加性契约修订，owner 裁决）、**config.py `max_message_bytes` 下界（≥1）校验补齐**（rev-cgpt R4 N1，非阻塞小项）。
