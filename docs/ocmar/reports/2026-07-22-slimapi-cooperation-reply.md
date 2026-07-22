# oc-slimapi → ocdroid 配合回复（重构窗口期）

> **日期**：2026-07-22  
> **发起方**：oc-slimapi（本仓）  
> **接收方**：ocdroid  
> **回复对象**：`<ocdroid>/docs/ocmar/reports/2026-07-22-slimapi-cooperation-request.md`  
> **slimapi 完整方案**：`docs/ocmar/reports/2026-07-22-passthrough-convergence-refactor-review.md`（含 §9 实施计划）  
> **状态**：**双向认知已同步**；T0 + T-R1 可并行启动（等用户通知开工）

---

## 1. 对 ocdroid 配合项的确认（对应 §4.1 模板）

| # | 项 | 确认 | 备注 |
|---|---|---|---|
| **C1** | wire 契约冻结期 | ✅同意 | 全程 Batch 0–5 **不 bump** `X-Slimapi-Version`（仍 `1`）；仅加性演进；冻结窗口 = slimapi Batch 0–5 全程 |
| **C2** | messageID 纯透传 | ✅同意 | 已源码确认（CHANGELOG rev E 2026-07-20）：`skeleton.py:142` deepcopy / `hub.py:415` `message_id=info.id` / `questions.py:60` q-p 不触碰 id；**含 fan-out 不重映射、不重生 ID**。children 投影/Cache 为 session-level，**不触碰 messageID**。T1 前做 fresh 联调（= R6 gate） |
| **C3** | 事件归属澄清 | ✅同意（已裁决，见 §2） | 双方对齐：**两 SSE 流事件集不相交** |
| **C4** | G-F1 fixtures 维护 | ✅同意 | v0.3.1 已建（S-D）；slimapi 仓内共享路径；T0 发现 fixture 缺口则补充 |
| **C5** | metrics 端点保持 | ✅同意 | `batch` ledger + byte ratio（S-C）保持；Batch 3 若加 children cache hit/miss 指标（加性）会同步客户端消费侧 |
| **C6** | Opt-A 保持 | ✅同意 | 能力头 + B2 矩阵 + 非 opt-in legacy 等价保持；flag 调整/回滚告知 |
| **C7.1** | Partial+N==0 | ✅闭环 | **结构不可能**（Partial ⇒ ≥1 fetch ⇒ N≥1）；retain-prior gate 对 **Partial 多余但无害**，对 **Success+N==0**（冷启动空 allowlist，真实可达）正确 |
| **C7.2** | sessions 错误深度 | ✅闭环 | 最小深度（log code + rethrow 原始）= 契约正确；codes 为 observability 面，非分支契约；不强制差异化 |

---

## 2. C3 裁决结果（双方已对齐）

**结论：slimapi 的 SSE 策展范围是权威。两 SSE 流事件集不相交。**

| 流 | wire 端点 | 事件集 |
|---|---|---|
| **Legacy-only** | `/global/event`（直连 opencode `:4096`/`:14096`，或 catch-all 透传） | `message.updated` / `message.part.*` / `text.delta` / `tool.*` / legacy `session.status` 等 |
| **Slim-only** | `/slimapi/events`（slimapi hub 策展） | `session.digest` / `question.*` / `permission.*` / `server.connected\|heartbeat\|reconfigured` / `resync` / `session.error` |

- **"Shared" 真实含义** = 纯转换函数库可复用（如 `applyPartDelta`），**非 wire 共发**。
- **slimapi 确认**：不向 legacy 流注入 slim digest 帧（legacy 走 catch-all/直连，不经 slimapi SSE hub，**物理隔离**）；slim 流也不含 legacy 帧。
- **slim 域无 token 流式**：内容更新经 `session.digest` → REST `/slimapi/messages/**`（skeleton/full）拉取（省流设计）。
- **后果（供 ocdroid T1/T2 参考）**：`SseEventRouter` 不得期望 slim 流含 `message.part.*`；`streamingPartTexts` 所有权**按模式分 writer**（legacy=SSE 喂、slim=REST 喂）。此修正推翻前序"slim SSE 不丢 text.delta"的误判。

---

## 3. slimapi 需 ocdroid 反向配合清单（对应 §4.2 模板 + ocdroid task 映射）

| R# | 项 | 优先级 | 期望 ocdroid | ocdroid task 映射 | 期望时点 |
|---|---|---|---|---|---|
| **R1** | status/active 降频 | 🔴 | 停 4s 轮询 `/session/status`+`/api/session/active`；cold-start 改 `/slimapi/sessions/status` + SSE digest `status` 接力（`busy` 推 `active`，未知值保守重拉）；断连降级 10–30s | **T-R1（新增，与 T0 并行）** | 立即（slimapi 零改动） |
| **R2** | children hint 消费 | 🟠 | 列表刷新消费 `childrenIDs`/`childrenComplete`；缺失调 `/slimapi/sessions/{sid}/children`；停透传 `/session/{sid}/children` | T3 | Batch 3 上线 |
| **R3** | childrenVersion 处理 | 🟠 | 同 server generation 内比较；`server.connected`/`resync` 后 cold-start 清基线；**禁跨进程比大小**；Y 兜底（收 `session.created` 子→刷父，reconciler 去重） | T1（version reducer）+ T3 | Batch 4 上线 |
| **R4** | 双轨迁移 | 🟡 | `/question`→`/slimapi/questions`；`reply` 走 routeToken；`/global/health`→`/slimapi/health`；`/session`→`/slimapi/sessions` | T3 | 随时 |
| **R5** | C3 事件归属（反向已闭环） | 🟠 | `SseEventRouter` 勿期望 slim 流含 `message.part.*`（见 §2） | T2（已纳入） | T0/T2 前 |
| **R6** | messageID fresh 核验 | 🟢 | T1 前双方 fresh 联调核验（slimapi 提供透传测试用例锚定） | T1 gate | T1 前 |
| **R7** | Batch 1 错误映射适配 | 🟡 | slimapi Batch 1 修复 batch status 错误体（网络→503 / 4xx→502 / 坏JSON/非dict→503）后，若依赖原始 body 须按 §7 coded 适配 | T3（rev-4 DRAFT 为输入） | Batch 1 上线 |

---

## 4. rev-4 / rev-5 测试处置（slimapi 侧）

slimapi 侧已存在两个 fixer 产出的测试文件（T0 前置 TDD/行为锁定，已落盘，`./scripts/check.sh` **430 passed + 8 xfailed / EXIT=0**）：

| 文件 | 状态 | 处置 |
|---|---|---|
| `tests/test_upstream_error_boundary.py`（315 行，8 xfail） | **DRAFT** | test-first 未实现行为，不断言现状，加性；**Batch 1 正式启动须经 fixer 复核 + rev-gpt ≥9.5 升 frozen contract**；作 R7 适配输入 |
| `tests/test_hub_behavior_lock.py`（770 行，111 pass） | **DRAFT** | 行为锁定，当前 pass；**同标准**：DRAFT → 门控 → 冻结；不中途杀 |

两者不改 `src/` 实现代码，不影响现有行为。

---

## 5. 同步状态与下一步

- **双向认知已同步**：C1–C7 全部确认/闭环；C3 裁决归档；R1–R7 与 ocdroid task 树互映（T-R1/T0/T1/T2/T3）。
- **并行启动 gate**：**T0**（ocdroid 测试冻结）+ **T-R1**（status 降频，slimapi 零改动）可并行启动（fixer 写测试冻结）。
- slimapi 侧 **Batch 1–5**（见完整方案 §9）**等用户明确通知开工**后按 Batch 1→2→3→4→5 顺序推进（fixer 测试 + fixer-zlm 开发 + fixer-bgpt 并发兜底 + `_priv-verifier`/rev-gpt 验证）。

---

## 引用

- ocdroid cooperation-request：`<ocdroid>/docs/ocmar/reports/2026-07-22-slimapi-cooperation-request.md`
- ocdroid 完整重构方案：`<ocdroid>/docs/ocmar/specs/2026-07-22-full-refactor-plan.md`（§7.8 协同结果）
- slimapi 完整方案 + §9 实施计划：`docs/ocmar/reports/2026-07-22-passthrough-convergence-refactor-review.md`
- slimapi 契约：`docs/v1-contract.md`（当前 rev F；后续加性演进就地标注，不 bump wire）
