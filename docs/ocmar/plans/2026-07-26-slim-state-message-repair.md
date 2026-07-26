# Slim 状态/消息补传修复计划（第1类 + 第2类）

> **For agentic workers:** 第1类由 oc-slimapi 单方实施；第2类需 ocdroid 配合，提示词见 `docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md`。

**Goal:** 修复 slim 模式下会话状态、消息获取、增量补传与 SSE 失效恢复中 **sidecar 可单方完成** 的缺陷，并为双方协议改造预留兼容字段。

**Architecture:** 以 opencode `MessageV2.page()` 真实页序（DB newest-first 取窗 → `items.reverse()` → **页内 oldest-first**）为基准，重写 `/since/{ts}` 扫描/停扫；SSE 立即事件改为 per-sid flush；`/full` 异常路径结构化；加性响应头 `X-Since-Complete` 供未来客户端使用。

**Tech Stack:** FastAPI / httpx / orjson / pytest；上游对照 `ocdroid/opencode-src/current` → v1.18.4。

## 当前进展（2026-07-26）

**第1类已完成并通过终审，可作为合入候选。** 本轮由 4 路 `fixer-grok` 分域实施，终审收尾改由 `fixer-longcat` 完成；`rev-gpt` 结论为“有条件通过”，其指出的收尾项已处理。

已完成：

- `/since/{ts}` 按 opencode v1.18.4 页内 oldest-first 语义整页过滤，移除“首项不匹配即停止”的错误启发式。
- 新增 `X-Since-Complete: true|false`；`true` 表示本次扫描未因 `max_since_pages` 截断，不表示没有更多 cursor。
- SSE 立即 flush 改为 per-session；修复 `archived=0`、拒绝 bool；收紧 session ID 提取，移除 `payload.id` 误用。
- `/full` 非法/空 JSON 与 body 中途读取失败统一为结构化 503 `upstream_unavailable`；合法 wrong-shape JSON 仍保持 200。
- full strip 路径去掉刚解析对象上的无必要整树 `deepcopy`，改为 in-place 处理。
- 已同步 `v1-contract.md`、`design-v2.md`、`INTERFACE_MAP.md`、`CLIENT_CHANGES.md`、`CHANGELOG.md`、`AGENTS.md`，并写入评审报告和 ocdroid handoff 提示词。

验证证据：

```text
./scripts/check.sh → 1022 passed；19 条 /slimapi 路由与 INTERFACE_MAP 一致
git diff --check → 通过
rev-gpt → 有条件通过；无已确认 P0；收尾项已处理
```

当前明确未完成：

- 第2类消息内容变更 watermark：尚未引入 revision / partCount / generation。
- token stream 的 `session_idle` / resync 终态清理语义：尚未单方改变。
- ocdroid SSE 开/关统一 reconcile、失败与空结果区分、cursor fallback：等待双方协作。

**工作树仍为未提交改动；本轮未执行 commit、tag 或 release。**

## 联合计划门控状态（2026-07-26）

ocdroid 开发方已完成联合计划并通知：联合计划已通过 `rev-gpt` **9.5/10 PASS**，按 D-GATE 规则进入暂停状态，不实施第2类联调代码。

- 对方联合计划原文：`ocdroid/docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`（1097 行；以 ocdroid 仓库为联合计划主文档）。
- 本仓联合索引：`docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`。
- 当前状态：**双方计划已达成共识，门控通过，等待用户明确下一步/owner 授权。**

已冻结的关键规则：

1. 阶段 A：双方各自独立发布；客户端 `/since` 只 staging，不推进 `localApplied`、不清 dirty、不写 authoritative cache。
2. 阶段 B：冻结 `since-complete` capability 后，才允许 `/since` 权威提交。
3. `SlimDrainOutcome.Success` 只表示 cursor-null terminal；cap/partial/timeout 均不得推进 bookmark。
4. authoritative commit 是进程内 token-guarded commit，不声称跨重启恢复。
5. 集合 merge 复用现有 `MessageWithParts`，不新增第二套业务模型。
6. watermark 复用现有 nullable 字段，统一 `updated > 0L && id.isNotBlank()` 规则。
7. owner 必须由用户明确指定；orchestrator 不得自我授权实施联合任务。
8. 发布证据必须包含 commit、tag、artifact SHA-256 和不可变 check log。

门控后待处理、但不阻塞计划通过的两项实现前修订：

- 静态审计命令需精确区分 Retrofit 原始调用与旧 facade；
- full/cursor snapshot merge 的 union vs replacement 语义需在实施前冻结。

**本仓在用户明确下一步前暂停，不新增第2类 wire 字段、不启动联调、不生成发布 commit/tag。**

## 新会话待办与提示词

当前已从“计划门控”进入“等待用户授权执行”阶段。执行顺序固定为：

1. oc-slimapi 与 ocdroid 分别执行阶段 A 自身任务，可独立发布；
2. 双方交换发布证据（commit/tag/artifact SHA-256/不可变 check log）；
3. 只有证据齐全后，启动阶段 B 联调；
4. 联调前冻结 `SlimDrainOutcome`、watermark、snapshot merge 和 token 终态语义；
5. 本联合计划已通过 `rev-gpt 9.5/10`，若不改变协议/owner/范围，不重复门控；若发生变化，必须重新评审。

可直接交给新会话的提示词：

- oc-slimapi 自身阶段 A：`docs/ocmar/reports/2026-07-26-slimapi-stage-a-execution-prompt.md`
- ocdroid 自身阶段 A：`docs/ocmar/reports/2026-07-26-ocdroid-stage-a-execution-prompt.md`
- 双方阶段 B 联调：`docs/ocmar/reports/2026-07-26-slim-message-stage-b-integration-prompt.md`

## Global Constraints

- 不改 `X-Slimapi-Version`（仍为 1）；仅加性/修复性 wire。
- 不要求 ocdroid 同步发版即可受益（旧客户端忽略新响应头）。
- 契约权威：`docs/specs/v1-contract.md`；实现冲突先改实现或正式 bump。
- 改动后必须 `./scripts/check.sh` 通过。
- 禁止静默偏离契约；文档与 INTERFACE_MAP 同步。

---

## 分类总览

### 第1类 — oc-slimapi 单方（已完成）

| ID | 项 | 文件 |
|---|---|---|
| C1-1 | `/since` 页序/早停修复 + 完整页过滤 | `routes/messages.py` |
| C1-2 | `X-Since-Complete` 加性头 | `routes/messages.py` + docs |
| C1-3 | SSE `flush_sid` / archived 类型防护 / sessionID 提取收紧 | `sse/hub.py` |
| C1-4 | `/full` 非法 JSON/空 body/中途断连 → 结构化错误；strip 去多余 deepcopy | `messages.py` / `transform.py` / `skeleton.py` |
| C1-5 | 升序夹具测试 + 文档勘误（newest→oldest 错误表述） | tests + specs |

### 第2类 — 需 ocdroid 配合（本仓只文档/预备，不改 wire 破坏语义）

| ID | 项 |
|---|---|
| C2-1 | 消息内容变更 watermark（revision / partCount / generation） |
| C2-2 | token stream 终态：idle/resync 不清空唯一可见内容 |
| C2-3 | SSE 开/关统一 reconcile；空结果 vs 截断 vs 失败三分法 |
| C2-4 | cursor fallback 触发条件与 dirty 收敛 |

**下一步顺序：**

1. 将 `docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md` 交给 ocdroid 侧，先完成客户端 reconcile 源码勘察。
2. 双方先落地兼容性保护：补传失败/不完整不清 dirty、不清已有 token 内容；支持 `X-Since-Complete`，缺失时兼容旧 sidecar。
3. 共同选择内容变化 watermark 方案；协议确认前不新增 revision/partCount/generation wire 字段。
4. 联调 slim + SSE 开、slim + SSE 关两个组合，要求最终消息集合和状态一致，再评估是否需要 `X-Slimapi-Version` bump。
5. 联调通过后，单独制定第2类实现计划，不把第2类改动混入本轮第1类候选。

---

### Task 1: `/since` 页序正确性 + `X-Since-Complete`（已完成）

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py` (`messages_since`)
- Modify: `tests/test_messages_routes.py`
- Modify: `docs/specs/design-v2.md` §1.5、`INTERFACE_MAP.md` since 行、`CHANGELOG.md`

**Acceptance Criteria:**
- `T1-C1`: 升序页（oldest-first）+ `ts=中位` 返回全部 `watermark>=ts` 项，非空
- `T1-C2`: 升序页首项 `<ts` 时仍收集本页后续 `>=ts` 项，再停扫更旧页
- `T1-C3`: 响应含 `X-Since-Complete: true|false`（完整扫描 vs max_since_pages 截断）
- `T1-C4`: 降序夹具仍不丢项（防御）
- `T1-C5`: `./scripts/check.sh` 相关 since 测试全绿

**实现要点:**
1. 取消「首个不满足项立即 break 且假定后续更旧」的 newest-first 启发式。
2. 每页完整过滤 `_passes_ts_filter`。
3. 停扫更旧页条件（基于 oldest-first）：本页非空且**最旧项**（页首）watermark 可比较且 `< ts`（本页已过滤完）；或无 Link；或 collected>=limit；或 max pages。
4. `X-Since-Complete=false` 当因 `max_since_pages` 耗尽且未证明 ts 地板且可能还有页。
5. `X-Next-Cursor`：填满 limit 且仍可能有更新匹配项且有 upstream Link 时下发；撞地板后抑制。

**完成证据：** 已加入 oldest-first、边界、分页、截断和兼容夹具；全量 `./scripts/check.sh` 通过。

---

### Task 2: SSE hub 稳定性（已完成）

**Files:**
- Modify: `src/oc_slimapi/sse/hub.py`
- Modify: `tests/test_hub.py` / `tests/test_hub_behavior_lock.py` 按需

**Acceptance Criteria:**
- `T2-C1`: `session.status=busy` 清 sticky 与 `session.error` 立即 flush **仅目标 sid**，不排空其他 pending
- `T2-C2`: `archived=0` 若出现则写入 digest（`is not None` 且 int）
- `T2-C3`: `_extract_session_id` 不再把 `payload.id`（GlobalBus event id）当 sessionID
- `T2-C4`: 相关 hub 测试通过

**完成证据：** 已覆盖 `flush_sid` 目标隔离、`archived=0`、bool 拒绝及 event id 不误挂；相关测试通过。

---

### Task 3: `/full` 健壮性 + strip 内存（已完成）

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py` (full list/single)
- Modify: `src/oc_slimapi/transform.py` / `skeleton.py`
- Modify: 相关 tests

**Acceptance Criteria:**
- `T3-C1`: full list/single 上游 200 + 非法 JSON / 空 body → 结构化 503（非裸 500）
- `T3-C2`: full-list 中途 `httpx.RequestError` → 503 `upstream_unavailable`（与 single 对齐）
- `T3-C3`: 生产 strip 路径不对刚解析对象做无必要 deepcopy（或提供 in-place 路径）；单测仍可验证非共享别名安全
- `T3-C4`: 既有 wrong-shape 合法 JSON 仍 200 原样服务

**完成证据：** 已覆盖 single/list 非法 JSON、空 body、body 迭代中途断流、wrong-shape 和 in-place strip；全量校验通过。

---

### Task 4: 文档与 handoff（已完成）

**Files:**
- Create: 本计划、评审报告、ocdroid 第2类提示词
- Modify: design-v2 / INTERFACE_MAP / CHANGELOG（第1类加性说明）

**完成证据：** 已创建本计划、综合评审报告、rev-gpt 评审报告和 ocdroid 第2类提示词；权威契约及相关说明已同步。

---

### Task 5: ocdroid 第2类联合勘察（下一步，阻塞协议设计）

**Owner:** ocdroid 侧；oc-slimapi 提供接口、fixture 和联调支持。  
**Input:** `docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md`

**交付物：**

- Slim SSE 开/关、token stream、polling、cursor drain、`localApplied`、dirty/reconcile 的源码级链路图。
- 明确 `session_idle`、`reconnect_no_replay`、backpressure、失败、空结果的当前客户端语义。
- 至少两种 content watermark 方案比较及推荐方案。
- 客户端安全保护的测试计划：失败不清内容、空结果不误收敛、bounded cursor fallback、不产生无界重试。

**阻塞条件：** 未完成此勘察和双方协议确认前，oc-slimapi 不新增 revision/partCount/generation 等第2类 wire 字段。

---

### Task 6: 双方协议冻结与联调（后续）

**双方共同确认：**

- content watermark 字段、单调性、乱序/重复 digest 处理；
- `X-Since-Complete` 缺失/true/false 的客户端行为；
- token stream done/idle 是否保留 provisional 内容；
- `/since` 失败、空结果、截断、cursor fallback 的状态机；
- 删除、重连、背压和 session idle 的最终一致性语义；
- 是否需要 `X-Slimapi-Version` bump。

**验收条件：**

- slim + SSE 开与 slim + SSE 关最终消息集合一致；
- 同一 assistant message 多次追加后最终全文完整；
- SSE 重连、背压、token done 后不清空唯一可见内容；
- 失败/不完整补传不会错误清 dirty；
- dirty 最终收敛且无无界重试。

---

## Criterion Ownership Matrix

| Criterion ID | Spec requirement | Owner task | Verification |
|---|---|---|---|
| T1-C1..C5 | /since 正确性 + complete 头 | Task 1 | pytest since + check.sh |
| T2-C1..C4 | SSE flush/archived/sid | Task 2 | pytest hub |
| T3-C1..C4 | /full 错误与内存 | Task 3 | pytest messages/transform |
| Docs | 分类计划与 handoff | Task 4 | 文件存在 |
| C1 status | 第1类实现、文档和验证证据 | 本进展节 | `./scripts/check.sh` → 1022 passed |
| C2-1..C4 | watermark/token/reconcile 联合改造 | Task 5/6 | 双方评审与联调矩阵 | Y |
