# oc-slimapi × ocdroid 配合计划 R2（Token Stream 第 2 轮）

> **日期**：2026-07-23　**分支**：`dev`　**基线**：main `c21ca3b`（v0.5.0 + P3 r1 S-1/S-3a/S-2 + O1 已 push）
> **范围**：枚举 **所有需要 ocdroid 配合**的本仓后续任务，给出服务端/客户端分工、wire 影响、依赖与排序。
> **契约/协商文档**（双边权威）：[`docs/ocdroid-cooperation-r2-handoff.md`](../ocdroid-cooperation-r2-handoff.md)
> **R1 上下文**：v0.5.0 双边 handoff [`docs/ocdroid-token-stream-handoff.md`](../ocdroid-token-stream-handoff.md)；R1 P3-P4 计划 [`docs/ocmar/plans/2026-07-23-token-stream-p3-p4.md`](2026-07-23-token-stream-p3-p4.md)

---

## 0. 服务端终态（R2 起点）

main `c21ca3b` 已落地（均 push）：
- **v0.5.0**：token 批式 SSE（opt-in，lever1 done-marker + lever2 gzip）。
- **S-1**：`token_hub.py` 拆 `sse/tokenstream/` 包（纯结构）。
- **S-3a**：5 个加性观测指标（`sse.tokenStream.*`）。
- **S-2（method B 半成品）**：`_evict_part_for_memory` 驱逐后对**同 sid 剩余 live part**重发 `snapshot{done:false}`（**flip 前冗余**——客户端 `triggersReconnect=true` 收 resync 即重连、旧连接正拆除；**MB-P flip 后方生效**）。
- **O1**：`_reserve→evict` 排除 current key（`skip_key`），闭合 re-entrancy 截断。

**当前 `token_memory_limit` 恢复策略**：客户端 `triggersReconnect=true`（R1 既有）→ 重连 → handshake 重发全部 live snapshot（含 current key）。**method B 的「现有 sub 重发」是对非 current part 的优化；current key 仍靠重连恢复。**

---

## 1. 需 ocdroid 配合的任务全集

| ID | 任务 | 服务端 | ocdroid | wire | 双边? |
|---|---|---|---|---|---|
| **MB-P** | Method B 产品化（`token_memory_limit` clear-only 恢复，不重连） | **MB-P-S**（闭合 current-key 锚点缺口，见 §2） | flip `triggersReconnect` true→false + 测 | 无（客户端策略） | ✅ |
| **F-1** | reasoning / tool-input part 流式 | 停静默 drop；加 wire；键 `(sid,mid,pid[,field])` 建 LivePart（判别器 `partType`） | reducer + UI 消费 | **加性，待裁定是否 bump** | ✅ |
| **F-2** | busy-open 占位帧 | attach 时发 `server.connected{...,busy:true}` | UX 渲染 skeleton | 加性字段（建议不 bump） | ✅ |
| **F-3** | 自适应 flush 窗 | 动态 cadence（依赖 S-3a metrics） | 仅消费时延变化（无显式改动） | 无 | 边际（基本服务端） |
| **S-4** | ocdroid flow 级测 | 提供 contract/期望清单 | `TokenStreamCoordinator` 对 idle/deleted/UNKNOWN 的 flow 测 + token×`/events` digest eviction 顺序测 | 无 | ✅（ocdroid 实施） |
| **C-4** | ocdroid 客户端文档对齐 | 提供 R1 终态 wire 权威 | 对齐 `token-stream-client-design.md`/`dev-plan.md`/design V6 §5.1 → lever1 / 5 reason / 两档 resync / lever2 gzip | 无 | ✅（ocdroid 实施） |
| **V-B** | 生产长连 idle 实证 | 发 15s heartbeat + 防代理头 | 45s watchdog（3×15s）对齐 | 无 | ✅（运维 + ocdroid） |

**显式排除（本仓 unilateral，不需 ocdroid）**：S-3b（调参，需 harness 数据，纯服务端）、S-1 拆包（已完成）、S-3a（已完成）、O1（已完成）。

---

## 2. 各任务详情（服务端 / ocdroid / 前置 / 验收）

### MB-P — Method B 产品化（最高价值双边项）

- **现状**：S-2 让服务端在 eviction 后对**非 current 的剩余 live part**重发 snapshot。但 **current key（正在 reserve 的）被 O1 的 `skip_key` 排除**——其客户端锚点在 resync 清态后**不**由 method B 恢复，仍依赖 `triggersReconnect=true` 的重连 handshake。
- **服务端剩余工作（MB-P-S，前置）— ✅ 已发版+部署 v0.6.0**（main `d97f701`/tag `v0.6.0`，2026-07-23；含 S-2+O1+MB-P-S1+Q4 debug env；rev-grok APPROVED；792 passed；生产 health 0.6.0）：闭合 current-key 锚点缺口（O1 的 `skip_key` 让 current key 在 eviction re-snapshot 时被跳过 → clear-only 下其客户端锚点不恢复），使 clear-only（不重连）也安全。**统一变体 MB-P-S1（推荐，与 handoff §2.1 一致）**：eviction re-snapshot 时把 current key 重新纳入重发，带「截断不 drop」守卫——
  - current key snapshot 帧 **≤ `max_frame_bytes`** → 发正常 `snapshot{done:false}`（保留动画）。
  - **> `max_frame_bytes`**（近 1MiB part）→ 发 `snapshot{truncated:true}` + 客户端 `/since`（大 part 动画让位于权威真值）。
  - **「截断而不 `drop_part`」= 新增服务端发射路径**（现有 `_emit_snapshot_or_truncated` 超限必 `_truncate_part_for_all`→`drop_part`，正是 O1 re-entrancy 源）。需一条「发 `truncated` 帧但保留 LivePart、不 drop」的新路径，从而不 invalidate 调用方 `live` 引用（无 gauge 上漂、无游离 delta）。**wire 帧复用（不 bump 正确），但服务端逻辑是新增、且恰好绕开 O1 那次 drop_part——非「复用现有 truncated 语义」**。
  - **已知取舍（large-part 分支）**：即便服务端保留 LivePart，客户端收 `truncated` 后清该 part、停 append → 服务端继续累计并 flush 的 delta 在客户端 orphan 被丢。即 **large current key 动画不可救（仍 blank 至 `/since`）；仅 small current key 真 snapshot 分支保住动画**。取舍交 D-MB-P 由 ocdroid 裁定。
  - 备选 MB-P-S2：resync 携带 hint 触发客户端 `/since`（需新字段，不推荐）。
- **ocdroid 工作**：`TokenStreamFrame.TOKEN_MEMORY_LIMIT.triggersReconnect` true→false；reducer 在该 resync 下走 ClearPartState + TriggerSinceFetch（authoritative）+ **不** Reconnect；flow 测。
- **前置**：~~MB-P-S 落地 + 测绿~~ ✅（v0.6.0 已发版+部署，792 passed）→ ocdroid 已 flip（v0.13.2 `da47fe3`，gate 9.8 GO）。**MB-P 双边完成。**
- **验收**：服务端单测「evict current key 附近 → 现有 sub 收 truncated/since 恢复，无重连」；ocdroid flow 测「memory-limit → clear + /since，openCount 不增」。
- **wire**：无变化（triggersReconnect 是客户端策略）。**不 bump**。
- **价值**：消除 memory-limit 时的重连抖动（实时流不中断），显著改善高负载/大 part 场景体验。

### F-1 — reasoning / tool-input 流式

- **现状**：`on_part_updated` 对 `type != "text"` 记墓碑静默 drop（C3 故意）；`on_part_delta` 对非 text field 静默 drop。模块 docstring 明确「非 text 静默 drop」。
- **服务端工作**：扩 wire（见契约文档 §2 F-1）；按白名单 type 建 LivePart；改 drop 为流式。
- **ocdroid 工作**：reducer 解析新事件/partType；UI 流式渲染 reasoning/tool-input。
- **前置**：产品 go + wire 决策（partType vs 新 event + 是否 bump）+ 非是否计入 4+4 预算。
- **验收**：双边联调：reasoning delta 实时到客户端；text 路径零回归。
- **wire**：**待协商**（见契约文档）。**估工**：L（双边最大）。

### F-2 — busy-open 占位

- **服务端工作**：`attach_subscriber` 时若 `sid ∈ _busy_sids` 且无 live part → 发 `server.connected{sessionID, busy:true}`。
- **ocdroid 工作**：UX 检 `busy:true` → 立即渲染 skeleton（不等首帧 snapshot）。
- **前置**：产品 go；字段名/缺省冻结（契约文档）。
- **验收**：busy 会话 attach → 首帧 `server.connected{...,busy:true}`；idle/非 busy 行为不变。
- **wire**：加性字段（未知键客户端 ignore）。**建议不 bump**。
- **估工**：S（服务端）+ ocdroid UX。

### F-3 — 自适应 flush 窗（基本 unilateral）

- **服务端工作**：按 sub 数 / 观测时延动态调 `flush_loop` cadence（min/max 护栏）。
- **ocdroid**：仅被动消费时延变化（无显式改动）；若 ocdroid 有超时假设需复核。
- **前置**：S-3a metrics + 产品 go。
- **wire**：无。**基本不需 ocdroid 显式配合**（列入仅为完整性）。

### S-4 — ocdroid flow 级测（ocdroid 实施）

- **ocdroid 工作**：`TokenStreamCoordinator` 对 `session_idle`/`session_deleted`/`UNKNOWN` 的 flow 测；token stream × `/events` digest eviction 顺序/重复测（`session_deleted` 不发 `EvictSession`，依赖 digest 独立路径）。
- **服务端角色**：提供 wire 契约 + 期望清单（本计划 + handoff）。
- **前置**：无（可任意时点）。
- **价值**：双边安全网，防 R2 wire 变更回归。

### C-4 — ocdroid 文档对齐（ocdroid 实施）

- **ocdroid 工作**：`token-stream-client-design.md`（旧 4-reason）、`dev-plan.md`、design V6 §5.1「SSE 不 gzip」→ 对齐 R1 终态：5 reason、lever1 done-marker 无 text、两档 resync（triggersReconnect true/false）、lever2 默认 gzip。
- **前置**：无。**估工**：S。

### V-B — 生产长连 idle 实证（运维 + ocdroid）

- **工作**：生产 `:14097` mTLS 抓 ≥30s 静默流 → 确认 15s `server.heartbeat` 穿透 stunnel（客户端 45s watchdog 对齐）。
- **服务端**：已发 heartbeat + `X-Accel-Buffering:no` / `Cache-Control:no-cache,no-transform`。
- **ocdroid**：确认 45s watchdog 不误断；抓包配合。
- **前置**：生产路径可达。**估工**：ops（非码）。

---

## 3. 排序与波次

```text
旁路（任意时点，零写冲突）:
  C-4（ocdroid 文档） ∥ S-4（ocdroid flow 测） ∥ V-B（运维实证）

Wave 1（服务端先行，加性 wire / 无 wire，dual-read fail-closed）:
  MB-P-S（current-key 锚点闭合）  ← MB-P 的前置
  F-2（busy 占位，加性字段）

Wave 2（ocdroid 消费 Wave 1）:
  MB-P（flip triggersReconnect）  ← 依赖 MB-P-S
  F-2 UX 消费

Wave 3（最大双边，独立窗口）:
  F-1（reasoning/tool-input，wire 决策先行）  ← 产品 go + 双边协调

边际: F-3（基本服务端，依赖 S-3a，可并入 Wave 1 或独立）
```

**关键路径**：MB-P-S → MB-P（method B 价值兑现的最短路径）。
**最大项**：F-1（独立双边窗口，最后）。

---

## 4. wire 版本策略（跨项）

- **MB-P**：无 wire 变化（客户端策略）→ **不 bump** `X-Slimapi-Version`。
- **F-2**：加性字段（`busy`）→ 客户端未知键 ignore → **建议不 bump**。
- **F-1**：扩 event/partType → **待协商**（加性优先不 bump；若语义破坏才 bump，走 `docs/release.md`）。
- **总原则**：加性优先不 bump；破坏性才 bump。每个 wire 变更必须记 `CHANGELOG.md` + `CLIENT_CHANGES.md`。

---

## 5. 发布/回滚（沿用 v0.5.0 模式）

- **dual-read fail-closed**：客户端对 health `features.tokenStream` 及新字段做 dual-read，缺/404/405 → 降级「完成后整条出现」（零回归）。
- **feature 软关闭**：`features.tokenStream` 当前硬编码 True；如需部署级关闭，加 `OC_SLIMAPI_TOKEN_STREAM_ENABLED` flag（不在本轮）。
- **回滚**：各 task 独立 diff；wire 加性变更可单向回退；ocdroid 侧 dual-read 保证服务端回退时客户端不崩。

---

## 6. 风险

| 风险 | 缓解 |
|---|---|
| MB-P current-key 锚点缺口未闭合即 flip | MB-P-S 是硬前置；flip 前必须落地 + 测 |
| F-1 wire 决策反复 | 契约文档先冻结 partType vs event + bump 与否，再实现 |
| 双边时序错配（ocdroid 先于服务端） | 服务端先行 + dual-read fail-closed；ocdroid 永远降级安全 |
| 生产长连被代理断（V-B 未实证） | heartbeat + 防代理头已就位；V-B 抓包确认为 ops 前置 |

---

## 7. 不在本计划（unilateral 服务端，仅备注）

- **S-3b**（调参）：纯服务端，需 harness 数据，与 ocdroid 无关。
- 任何仅触观测/内部重构的后续项。
