# Slim 会话消息可靠性 阶段 B 设计冻结提案（oc-slimapi 视角）

**日期：** 2026-07-26
**状态：** v0.3 — 整合 rev-gpt 6.5/10 NEEDS-FIX + **用户约束：双方同步升级，不考虑旧版兼容**（可破坏性改、可 bump、客户端总信任新字段）；待 ocdroid 协作冻结
**作者视角：** oc-slimapi sidecar（**不是**最终协议；最终协议需双方共识冻结）
**关联：** 联合计划 `docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`；阶段 B 提示词 `docs/ocmar/reports/2026-07-26-slim-message-stage-b-integration-prompt.md`

**标注约定：** 🔒=sidecar 立场（已定）；🟡=待 ocdroid 共识（开放）。

**v0.3 关键简化（vs v0.2）：** 双方同步升级 → 删除所有"加性优先/不 bump/health capability 协商/字段缺失三态/旧客户端回退"论述。客户端总信任新 digest 字段、新响应头；`X-Slimapi-Version` 若需 bump 则 bump（不作为约束）。**但运行时可靠性问题（SSE 丢事件/sidecar 重启/恢复闭环）与版本无关，仍须解决。**

---

## §0 sidecar 设计哲学

1. **sidecar 是投影/动画层，不是跨页原子真值源** 🔒：`/since`/`/full` 返回 sidecar 从 opencode HTTP API 获取的持久化投影；**并发追加期间非跨页原子快照**。客户端须允许后续 digest/full 再次覆盖，不能把一次 drain 当永久稳定点。
2. **sidecar 进程内状态不跨重启** 🔒：进程内 generation/缓存/计数器重启归零，以 `server.connected`/`resync` 为边界重置 baseline（契约 §3 childrenVersion 已验证模式）。⚠️ 重置 baseline 只解决 revision 倒退，**不能恢复丢失的变更通知**（见 §4.2）。
3. **双方同步升级，不考虑旧版兼容** 🔒：可破坏性改协议、可 bump `X-Slimapi-Version`、客户端总信任新字段。新端点/新字段不需 capability 协商。
4. **删除语义明确** 🔒：只有 `session_deleted` 是删除；`resync`/`session_idle`/`backpressure`/`reconnect_no_replay` 都是"核对/重拉"。
5. **跨端点共享 identity，不共享 payload** 🔒：`messageID`/`partID` 跨端点稳定；skeleton（投影）/ full（strip 后权威 snapshot）/ token（text part + truncated）三种 payload 形状不同，merge 规则分别冻结（§5/§7）。

---

## §1 `since-complete` capability — **v0.3 简化**

**现状：** sidecar v0.11.0 下发 `X-Since-Complete: true|false`。

**sidecar 立场 🔒（简化）：** 双方同步升级 → **无需 health capability 协商、无需三态处理、无需异常值保守路径**。客户端直接读 `X-Since-Complete`（总存在，总有效）。可去掉 health `features.sinceComplete` 诊断键（无必要）。

**sidecar 代码改动：** **零**（头已下发，客户端直接用）。

---

## §2 `SlimDrainOutcome` 与 `/since` 响应头语义（修正 rev-gpt P0）

**sidecar 立场 🔒（X-Next-Cursor 缺失≠"无更多上游页"）：**
- `X-Next-Cursor` **存在** ⇔ 本次请求结果仍有 cursor continuation（可翻页）。
- `X-Next-Cursor` **缺失** ⇔ 本次请求**无可继续的 cursor continuation**——可能是：撞 ts floor / 上游无下一页 / 空页 / 结果未填满 limit / 扫描完成。**缺失 ≠ "上游无更多页面" ≠ "该 session 所有消息已扫描"**。
- `X-Since-Complete=true` ⇔ 未因 `max_since_pages` 截断；`=false` ⇔ 截断。

**`SlimDrainOutcome.Success`（ocdroid 拥有）🟡：** `X-Next-Cursor` 缺失 + `X-Since-Complete=true` → 可提交"本次 `/since` drain bookmark"。⚠️ **不代表** content watermark 已收敛 / full snapshot 已收敛 / 同 message 追加已覆盖（独立维度，见 §4/§5）。cap/partial/timeout/取消/失败 → 不推进 bookmark。

**待 ocdroid 共识 🟡：** `limit` 口径（当前 sidecar 跨页累计 `collected` = 整个 drain 上限，非单页 limit），客户端须一致。

**sidecar 代码改动：** **零**（信号 v0.11.0 已具备；语义澄清是文档层）。

---

## §3 token-guarded authoritative commit

**sidecar 立场 🔒：**
- `/since`/`/full` 是持久化投影，非跨页原子快照；客户端须允许后续覆盖。
- token `done:true` **不得直接写 authoritative cache**（marker 无 text）；但 `done:true` **可触发受限 reconcile / full fetch**（帧含 sessionID/messageID/partID）——full/since 成功后才 authoritative commit。避免"token 结束但等不到可触发信号"空洞。
- 进程内状态重启归零。

**sidecar 代码改动：** **零**。

---

## §4 watermark：同 message 内容追加检测 + 恢复闭环

**exp-2 事实确认（opencode v1.18.4）：** V1 `Assistant.time` **无 updated 字段**（仅 created/completed）；追加 part 不发 `message.updated`，只发 `message.part.updated`（含 `time:Date.now()`）；sidecar 不消费 `message.part.updated`；partId=`PartID.ascending()` 时间有序可去重；`/since` 过滤键=created（永不变）。

**结论 🔒：现有 `(updatedAt=created, messageID)` watermark 无法检测同 message 内容追加。** `/since` 的 created 过滤与内容追加检测天然不匹配 → 内容追加检测走"直接拉该 message（G6/full）"，`/since` 只管新消息。

### §4.1 正常路径：digest 加 `partEventRevision` 🔒

sidecar 新订阅 `message.part.updated`，digest 加 `partEventRevision`（per-(sid,mid) 进程内单调计数，每次 part.updated +1，debounce 窗内折叠为"暴露值严格 > 客户端上次值"）。客户端比较 partEventRevision 严格 `>` → 直接拉该 message（G6/full）。命名诚实（"已观察 part 事件序号"，非"内容版本"）。复用 children generation 的"进程内计数 + 重启边界"模式。

**否决方案：** B4（复用 updatedAt）—— watermark 与 /since 过滤键脱钩（updatedAt 推进但 created 不变 → 第二次追加起 /since 拉不到）；B1/B2（partCount/lastPartID）—— part 删除误判。

### §4.2 恢复路径：事件丢失/重启/无 SSE 时发现变更 🟡【rev-gpt P0，最关键】

**问题：** B3 依赖 sidecar 实时收到 `message.part.updated`。控制面 SSE 无 replay、sidecar 重启 counter 归零、`/since` 看不到旧 message append、`/full` 只有客户端已知 dirty 才调。**不可收敛路径**：part.updated 丢失 → 客户端没收到 partEventRevision → resync → /since 按 created 查 → 旧 message 不返回 → 客户端永远不知道要 /full。违反"同 message 多次追加最终全文完整"验收。

**"server.connected/resync 重置 baseline"只解决 revision 倒退，不能恢复丢失的变更通知。**

**候选恢复策略：**

| 策略 | sidecar 改动 | 客户端 | 优劣 |
|---|---|---|---|
| **R1 resync/cold-start bounded G6 /full 校验** | 零（G6 已有） | resync/server.connected/cold-start 时对当前 session 活跃消息（最近 N 条，去抖，频率上限）G6 batch /full，比较 partId 集合检测变更 | 最小；客户端比较开销（全量 parts） |
| R2 /full 加 content fingerprint | /full 加 additive fingerprint（maxPartId/partCount） | 比较 fingerprint 减开销 | 减少 R1 比较；/full 加字段 |
| R3 新端点 GET /messages/{sid}/revisions?ids= | 新端点 + watermark map | 批量查 revision，比较 | 专门化、高效；新端点 |
| R4 /since 接受 per-message part watermark | /since 参数 + content index | /since 带 per-message cursor | /since 语义大改 |

**sidecar 倾向：B3（正常通知）+ R1（恢复校验）** 🟡 —— 正常路径靠 digest partEventRevision 触发直接拉；异常路径客户端对活跃消息做 bounded G6 /full 校验（复用已有端点，零新端点）。若 ocdroid 认为 R1 开销大，可叠加 R2（/full fingerprint）或改 R3。**恢复策略选择 + bounded 参数 + fingerprint 形态是跨仓决策。**

**待 ocdroid 共识 🟡：**
1. 恢复策略（R1 / R1+R2 / R3 / R4）—— **最关键，决定可靠性闭环**。
2. part mutation 模型（part 可删/可重排/同 partID 可改/完成后可追加/重复 part.updated）—— 影响 merge（§5）+ partId 比较可靠性。
3. partEventRevision 生命周期（初值/折叠规则/删除清理/重启 baseline）。
4. 内容变更触发 full 拉取的预算/重试（batch 上限、429/503/413/timeout、退避、防 fetch storm、多 part.updated 合并）。
5. **运行时兼容**（非版本兼容）：控制面 SSE 断线期间 append、sidecar 在 part.updated 后 digest flush 前重启、sidecar 启动时上游已有正在追加的 message、poll-only 无 SSE、token-only（控制面关）—— token stream 是否也携带 content-change signal？idle 后是否必须 bounded refetch？

**sidecar 代码改动：** 中等 —— hub 新订阅 `message.part.updated` + digest 加 partEventRevision + per-(sid,mid) 计数生命周期（重启归零/删除清理/debounce 折叠）+ 测试 + 契约/CLIENT_CHANGES/CHANGELOG 同步；若选 R2/R3 则额外 /full 字段或新端点。

---

## §5 snapshot merge：分三类 + 不替 ocdroid ratify

**sidecar 立场 🔒（修正 rev-gpt P0）：**
- sidecar **不替 ocdroid ratify** merge 决策（ocdroid "窗口 merge" 是其单方 proposal，双方 ratification pending）。
- "无 message-level tombstone"只说明**增量缺失≠删除**；**不说明显式 full snapshot 不能 replacement**。两个独立问题。

**三类 merge 须分别冻结 🟡：**
1. **`/since` 增量消息集合 merge**（新消息加入；缺失≠删除——ocdroid 窗口 merge 在此类成立）。
2. **`/full/{mid}`/G6 单 message parts merge**（content change 触发的 full refresh）：union 保留上游已删 part；replacement 须定义是否覆盖 token streamOwned provisional、full 失败时是否保留旧内容。
3. **skeleton placeholder/omitted 展开 merge**（CLIENT_CHANGES：placeholder → message-level 整体替换）。

**待 ocdroid 共识 🟡：** 三类各自 union vs replacement + 失败/placeholder/provisional 覆盖边界。是 §4 内容变更触发 full 拉取的前提。

**sidecar 代码改动：** 取决于共识（可能零）。

---

## §6 token done/idle/resync/backpressure/reconnect

**sidecar 立场 🔒：** `done:true` 无 text 不清空；`resync`（4 种非删除 reason）是"重拉核对"非删除；只有 `session_deleted` 是删除；`truncated` 是 part 级"清该 part streamOwned + 走 /since"。

**事件优先级表（待冻结精确状态转移）🟡：**
```
session_deleted > truncated > authoritative /since|full success > resync/idle/backpressure > done marker
```
每种事件定义：visible text / streamOwned / authoritative / dirty / next fetch / 是否接受后续 delta。

**token-only 恢复路径 🟡：** 客户端只开 token stream（控制面关）时如何发现内容追加——见 §4.2 待共识 5。

**sidecar 代码改动：** **零**（除非共识决定 token stream 携带 content-change signal）。

---

## §7 跨端点 identity 与 payload 🔒

`messageID`/`partID` 跨端点稳定（identity 共享）；skeleton/full/token 三种 payload 形状不同（非同一 merge 输入）。ocdroid 按 §5 分别处理。**sidecar 代码改动：零。**

---

## §8 sidecar 阶段 B 改动估计

取决于 §4 共识：

| 共识结果 | sidecar 改动 |
|---|---|
| B3 + R1（推荐） | hub 订阅 part.updated + digest 加 partEventRevision + per-(sid,mid) 计数生命周期 + 测试 + 文档同步 |
| B3 + R2 | 上述 + /full 加 fingerprint |
| B3 + R3 | 上述 + 新 /revisions 端点 + INTERFACE_MAP + check.sh 路由一致 |

任何 wire 变更同步 `v1-contract.md`/`CLIENT_CHANGES.md`/`CHANGELOG.md`（+ `INTERFACE_MAP.md` 若新路由）+ 测试。`X-Slimapi-Version` 按需 bump（同步升级，不作为约束）。

**`message.part.updated` 接入 digest 工程考量（rev-gpt P1）：** 当前 hub 将 `message.part.*` 视作控制面丢弃事件。接入 digest 须冻结：debounce 250ms 内事件折叠、part.updated 高频下 CPU/内存、per-sid pending entry 生命周期、无合法 sid/messageID 事件处理、session.deleted/resync 时 watermark map 清理。

---

## §9 待 ocdroid 共识清单

**🔴 P0 必须冻结才能 B-IMPL：**
1. **§4.2 恢复策略**（R1/R1+R2/R3/R4）—— 可靠性闭环是否成立；覆盖 SSE 断线期间 append、part.updated 后 flush 前重启、sidecar 启动时已有追加中 message、poll-only、server.connected/resync 后发现旧 message 新 parts。
2. **§5 三类 merge 作用域**（/since 集合 / /full parts / placeholder 展开）—— union vs replacement + 失败/placeholder/provisional 覆盖边界。
3. **§2 X-Next-Cursor 与 Success 语义**（sidecar 已澄清，待客户端状态机对齐确认）。

**🟠 P1 必须冻结：**
4. **part mutation 模型**（可删/可重排/同 partID 可改/完成后可追加/重复 part.updated/partID 跨重启稳定）。
5. **partEventRevision 生命周期**（初值/折叠/清理/重启 baseline）。
6. **§6 事件优先级表** + token-only 恢复路径。
7. **digest debounce 语义**（窗口内 part.updated 折叠、revision 口径、flush 与 session_idle/message.updated 先后、多 message 同时追加 per-sid ordering）。
8. **内容变更触发 full 拉取预算/重试**（batch 上限、429/503/413/timeout、退避、合并、防 fetch storm）。

> v0.3 已删除 v0.2 的"无 SSE/旧客户端兼容矩阵"项（双方同步升级，无版本兼容问题）；保留运行时兼容（SSE 断线/重启/poll-only，归入 §4.2 恢复策略）。

---

## §10 最关键风险（rev-gpt 排序，sidecar 认同）

1. **🔴 B3 在 SSE 丢失/sidecar 重启后不可恢复**（§4.2）—— 正常 signal OK，恢复闭环缺失。必须 §9.1 共识后才算可靠性协议。
2. **🔴 full/snapshot merge 语义未冻结**（§5）—— content revision 触发的 full refresh：union 保留已删 part；replacement 须定义覆盖边界。
3. **🔴 X-Next-Cursor 语义误读**（§2，已修正）—— 防客户端错误推进 bookmark / 误判收敛。

---

## §11 流程

**rev-gpt 评审（2026-07-26）：** 6.5/10 NEEDS-FIX。确认 upstream 事实核查正确、B4 否决成立、B3 比 B1/B2 适合、/since 与追加检测边界正确。缺口（v0.2）：B3 局部 signal 非完整协议（恢复闭环缺失）、X-Next-Cursor 语义误读、full merge 作用域未冻结。v0.3 已整合全部 P0/P1 + 删除版本兼容冗余。

**下一步：**
1. 本 v0.3 送 ocdroid 协作冻结 §9 开放问题（重点 P0 三项）。
2. 双方冻结后：sidecar B-IMPL（§8）→ rev-gpt 二审 → check.sh → commit/tag/artifact/check log → 联调验收（不重启服务，等对面通知）。
3. 在 ocdroid 阶段 A 证据齐全 + §9 共识冻结前，sidecar 不写 B-IMPL 代码。
