# oc-slimapi × ocdroid 配合与契约：Token Stream R2（handoff / 协商）

> **对象**：ocdroid 侧（客户端集成 + 联合终审）。**服务端权威**：本文 §2 + [`docs/v1-contract.md`](v1-contract.md)（wire 基准；冲突以契约为准）。
> **生成**：2026-07-23　**服务端基线**：main `c21ca3b`（已 push）　**分支**：`dev`
> **完整任务计划**（本仓侧）：[`docs/ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md`](ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md)
> **R1 上下文**：v0.5.0 双边 handoff [`docs/ocdroid-token-stream-handoff.md`](ocdroid-token-stream-handoff.md)（R1 终审 GO 9.7，已发版）。

---

## 0. 一句话状态

R1（v0.5.0 token 批式 SSE）已发版+部署生效；R2 本轮**服务端先行**落地 S-1 拆包 + S-3a 观测 + S-2 method-B 半成品 + O1 re-entrancy 闭合。**R2 双边焦点**：① Method B 产品化（`token_memory_limit` 不重连恢复，需服务端再补 MB-P-S + ocdroid flip `triggersReconnect`）；② F-1 reasoning/tool-input 流式（wire 决策先行）；③ F-2 busy 占位（加性）。**所有 wire 变更加性优先不 bump**；服务端先行 + ocdroid dual-read fail-closed 永远降级安全。

---

## 1. 服务端 R2 终态（已落地，main `c21ca3b`）

| 项 | 状态 | 对 ocdroid 的可见性 |
|---|---|---|
| v0.5.0 token 批式 SSE | ✅ 发版+部署 | health `features.tokenStream=true` |
| S-1 拆包（`sse/tokenstream/`） | ✅ | 无（内部结构，import 路径不变） |
| S-3a 观测指标（5 个加性） | ✅ | `/slimapi/metrics` `sse.tokenStream.*` +5 key（运维面，非客户端契约） |
| S-2 method B 半成品 | ✅ | eviction 后对**非 current 的剩余 live part**重发 `snapshot{done:false}`（**flip 前冗余**：客户端 `triggersReconnect=true` 收 resync 即重连、旧连接正拆除；**MB-P flip 后方生效**） |
| O1 re-entrancy 闭合 | ✅ | `_reserve→evict` 排除 current key（内部正确性） |

**R1 恢复策略仍为 `triggersReconnect=true`**：`token_memory_limit` → 客户端重连 → handshake 重发全部 live snapshot（含 current key）。**Method B 的「现有 sub 重发」当前只覆盖非 current part；current key 仍靠重连恢复。**

---

## 2. 服务端 wire 变更提案（R2 契约 delta）

### 2.1 MB-P — Method B 产品化（**无 wire 变更**，客户端策略调整）

- **现状 wire**：`resync{token_memory_limit, sessionID}` 不变。客户端 `TOKEN_MEMORY_LIMIT.triggersReconnect=true`（R1 既有，两档 resync 的「重订阅」档）。
- **产品化**：ocdroid flip `triggersReconnect` true→false（改走「仅清态 + `/since`」档，不重连）。**注意**：S-2 的「现有 sub 重发锚点」能力在 flip **之前基本为 0/冗余**（客户端收到 resync 即重连，旧连接正被拆除，handshake 会重发全部 live snapshot）；**MB-P flip 后方生效**。
- **服务端前置 MB-P-S（必须先于 flip）**：闭合 current-key 锚点缺口（O1 的 `skip_key` 让 current key 在 eviction re-snapshot 时被跳过 → clear-only 下其客户端锚点不恢复）。
  - **提案 MB-P-S1（推荐；两份文档统一此变体）**：eviction re-snapshot 时把 current key（O1 当前跳过它）**重新纳入重发**，但带「截断不 drop」守卫：
    - current key 的 snapshot 帧 **≤ `max_frame_bytes`** → 发正常 `snapshot{done:false}`（保留实时动画）。
    - current key 的 snapshot 帧 **> `max_frame_bytes`**（近 1MiB part，文本+JSON 信封超帧上限）→ 发 `snapshot{truncated:true}`，客户端走 `/since` 拉权威全文（大 part 动画让位于权威真值）。
    - **「截断而不 `drop_part`」= 新增服务端发射路径**（现有 `_emit_snapshot_or_truncated` 超限时必走 `_truncate_part_for_all`→`drop_part`，正是 O1 的 re-entrancy 源）。本提案需一条「发 `truncated` 帧但**保留 LivePart、不 drop**」的新路径，从而不 invalidate 调用方 `_reserve`/`on_part_delta` 持有的 `live` 引用（无 gauge 上漂、无游离 delta）。**wire 帧复用 `snapshot{truncated:true}`（不 bump 正确），但服务端逻辑是新增的、且恰好绕开 O1 所依赖的那次 drop_part**——不是「复用现有 truncated 语义」。
    - **已知取舍（large-part 分支）**：即便服务端保留 LivePart，客户端收 `truncated` 后已清该 part、停 append → 服务端继续累计并 flush 的 delta 在客户端沦为 orphan 被丢。即 **large current key 的实时动画不可救（仍 blank 至 `/since`）；仅 small current key 的真 snapshot 分支保住动画**。此取舍交 §3 D-MB-P 由 ocdroid 裁定。
  - 备选 MB-P-S2：resync 携带 hint 触发客户端 `/since`（需新字段，不推荐）。
- **wire 影响**：**无**（纯客户端策略 + 服务端内部新发射路径，帧复用）。**不 bump**。

### 2.2 F-1 — reasoning / tool-input 流式（**wire 决策待协商**）

- **现状 wire**：`message.part.snapshot`/`message.part.delta` 仅含 text；非 text part（reasoning/tool-input）服务端**静默 drop**（C3）。
- **提案（二选一，需 ocdroid 裁定）**：
  - **F-1-A**：现有事件加 `partType` 字段（`"text"`/`"reasoning"`/`"tool_input"`）；客户端按 `(messageID, partID)` 分桶（sid 隐含，stream 已 sid-scoped，与 R1 text 路径一致）。加性，未知 partType 客户端 ignore → **建议不 bump**。
  - **F-1-B**：新事件名 `message.part.reasoning.*` / `message.part.tool_input.*`。语义更清晰但事件空间扩大；评估是否需 bump。
  - **键 / 判别器厘清（D-F-1 冻结时定）**：服务端 `PartKey=(sid,mid,pid)`，fanout 全程读 `key[0]=sid`，故服务端 LivePart 键**必须保留 sid**（扩为 `(sid,mid,pid[,field])`，不可裸 `(mid,pid,field)`，否则破坏 fanout）。判别维度：reasoning 上游复用 `field:"text"`，故判别器是 `part.type`（→ `partType`）**而非** `field`；`field` 维度大概率仅对 tool-input（非 text field）有意义。
- **需 ocdroid 决策**：A vs B；流哪些 type；是否 bump `X-Slimapi-Version`；非 text 是否计入 4+4 内存预算；服务端键是否扩 `field` 维度（见上「键/判别器厘清」）。
- **服务端实现前置**：决策冻结 → 停静默 drop → 白名单 type 建 LivePart。

### 2.3 F-2 — busy-open 占位（**加性字段**）

- **提案**：`attach_subscriber` 时若 `sid ∈ _busy_sids` 且无 live part → 首帧 `server.connected{sessionID, busy:true}`。
- **wire 影响**：`server.connected` 加可选 `busy` 字段（仅 busy+无 live part 时出现）。加性，未知键 ignore → **建议不 bump**。
- **需 ocdroid 确认**：字段名 `busy`、缺省（不 busy 时不带 vs `busy:false`，二选一冻结）、UX 消费语义。

### 2.4 不变项（重申，防漂移）

- 帧集：`server.connected` / `server.heartbeat`(15s) / `message.part.snapshot{done:false|true|truncated:true}` / `message.part.delta` / `resync{reason,sessionID}`。
- ResyncReason wire 5：`reconnect_no_replay` / `subscriber_backpressure` / `token_memory_limit` / `session_idle` / `session_deleted`；未知 → 客户端 `UNKNOWN`（不丢帧）。**不发** `part_too_large`。
- gzip：token stream 默认 gzip（OkHttp 透明）；`/events` 不 gzip。
- 无 replay：不发 `id:`；`Last-Event-ID` 忽略，仅触发首帧 `resync{reconnect_no_replay}`。

---

## 3. ocdroid 须配合 / 决策清单（请回传）

| ID | 项 | 服务端假设 | 请 ocdroid 决策 / 实施 |
|---|---|---|---|
| **D-MB-P** | Method B 产品化时序 + current-key UX 取舍 | MB-P-S1（§2.1 统一变体：small current key 真 snapshot 保动画；large 超 `max_frame_bytes` → truncated-without-drop + `/since`）服务端先做 | ① 接受 S1 变体 + current-key 可观测行为裁定（large-part 动画不可救、blank 至 `/since` 是否可接受）；② flip `triggersReconnect` 时机；③ flow 测「memory-limit → 清 overlay + `/since` + **接受同连接 post-resync 补发 snapshot 重锚（不重连）**，`openCount` 不增」 |
| **D-F-1** | F-1 wire 形态 + 键/判别器 | 待裁定 | A（partType）vs B（新事件）；流哪些 type；bump Y/N；预算计法；**服务端键保留 sid + 判别器 partType vs field**（§2.2） |
| **D-F-2** | F-2 字段语义 | `server.connected{...,busy:true}`（仅 busy+无 live 时） | 字段名；缺省策略；UX 消费 |
| **S-4** | ocdroid flow 测 | 服务端提供契约（本文 + R1 handoff） | 实施 idle/deleted/UNKNOWN flow 测 + token×`/events` digest eviction 顺序测 |
| **C-4** | 文档对齐 | R1 终态 wire 权威（本文 §2.4） | 对齐 `token-stream-client-design.md`/`dev-plan.md`/design V6 §5.1 → 5 reason/lever1/两档 resync/lever2 gzip |
| **V-B** | 生产长连实证 | 服务端 15s heartbeat + 防代理头 | 45s watchdog 对齐确认 + 生产抓包 |

### 回传格式（建议）
```text
D-MB-P: 接受 S1 变体 + current-key UX 裁定（large-part blank 是否可接受）/ flip 时机 / re-anchor 联调
D-F-1: A|B + 流式 type 集 + bump Y/N + 预算 + 服务端键/判别器（partType vs field）
D-F-2: 字段名 + 缺省 + UX 说明
S-4/C-4/V-B: 是否做 + 时点
```

---

## 4. wire 版本策略（双边共识基线）

- **加性优先不 bump** `X-Slimapi-Version`（仍 `1`）：F-2（`busy`）、F-1-A（`partType`，若选）均为加性。
- **破坏性才 bump**：F-1-B（新事件）若判定语义破坏 → 走 `docs/release.md` 正式 bump。
- **每个 wire 变更**：服务端同步 `v1-contract.md` + `CHANGELOG.md` + `CLIENT_CHANGES.md`；ocdroid 以本仓契约为准（ocdroid 文档滞后时以本仓 + CHANGELOG 为准）。

---

## 5. 排序与发布（服务端先行 + dual-read fail-closed）

```text
旁路（任意时点）: C-4 ∥ S-4 ∥ V-B
Wave 1 服务端先行（加性/无 wire）:
  MB-P-S（current-key 锚点闭合，MB-P 前置）
  F-2（busy 占位，加性字段）
Wave 2 ocdroid 消费 Wave 1:
  MB-P（flip triggersReconnect，依赖 MB-P-S）
  F-2 UX 消费
Wave 3 最大双边（独立窗口）:
  F-1（wire 决策 D-F-1 先行）
```

- **dual-read fail-closed**：ocdroid 对 health `features.tokenStream` 及新字段（`busy`/`partType`）dual-read；缺/404/405 → 降级「完成后整条出现」（零回归）。
- **服务端永远先行**：ocdroid 任何消费都建立在服务端已 push 该加性变更之后。

---

## 6. 回滚

- **wire 加性变更**：单向回退安全（客户端 dual-read 兼容旧服务端）。
- **MB-P**：若 flip 后异常，ocdroid 回退 `triggersReconnect=true`（R1 既有路径，零数据风险）。
- **F-1**：白名单 type 可收窄回「仅 text」（恢复 C3 静默 drop）。
- **feature 软关闭**：`features.tokenStream` 硬编码 True；部署级关闭需加 `OC_SLIMAPI_TOKEN_STREAM_ENABLED` flag（不在本轮）。

---

## 7. 风险与开放项

| 风险 / 开放项 | 状态 | 动作 |
|---|---|---|
| MB-P current-key 锚点缺口（O1 `skip_key` 副作用） | **开放**，flip 前必修 | MB-P-S1 落地（服务端）→ ocdroid 验证 → flip |
| F-1 wire 形态未定 | **开放**（D-F-1） | ocdroid 回传 A/B + bump 决策 |
| 生产长连 heartbeat 穿透未实证 | 开放（V-B） | 生产抓包 |
| F-1 非 text 计入 4+4 预算？ | 开放 | 随 D-F-1 一并定 |

---

## 8. 联合终审（双边齐后）

1. wire 对照（本文 §2 vs ocdroid reducer/parser）—— 尤其 F-1 新形态 / F-2 busy / MB-P clear-only。
2. 零回归：服务端 769 + ocdroid 既有测试绿。
3. 评委：服务端建议 **rev-opus**（R1 终审经验）；ocdroid 自定。
4. 通过后：服务端按 `docs/release.md` 发版（加性不 bump；破坏性 bump + CHANGELOG）；ocdroid 随配合 commit 出货。

---

## 9. 历史/参考

- R1 v0.5.0 handoff（双边终态 wire + 联审轨迹）：[`ocdroid-token-stream-handoff.md`](ocdroid-token-stream-handoff.md)
- wire 契约权威：[`v1-contract.md`](v1-contract.md)
- 接口变更记录：[`CHANGELOG.md`](CHANGELOG.md) / [`docs/CLIENT_CHANGES.md`](CLIENT_CHANGES.md)
- 本仓 R2 完整计划：[`docs/ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md`](ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md)
