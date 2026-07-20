# ocdroid v0.11.7 slimapi 契约反馈与完善建议

> 客户端视角，基于 ocdroid **v0.11.7**（2026-07-21 发版）本会话修复与联调发现。
> 对照 `v1-contract.md` / `CLIENT_CHANGES.md`。每条结构：【客户端观测】→【已落地契约（确认已读）】→【建议，★标注最理想选项】→【接口新增/调整要求】。
> 所有建议默认**加性、不 bump `X-Slimapi-Version`**（沿用既有 additive 模式），除非另注。

---

## §0. 已闭环（契约成熟度高，仅确认）

| 项 | 状态 | 说明 |
|---|---|---|
| G1 `session.error` / `digest.lastError` 三态 | ✅ 已落地 | ocdroid 已消费。ocdroid `LastErrorTest` 同时支持 nested/top-level 是为兼容 **legacy opencode 直连**，slimapi 路径形态干净（session-less top-level + digest.lastError），**无契约歧义**。 |
| G6 批量展开 `/slimapi/messages/{sid}/full?ids=` | ✅ 已落地 | envelope + mid 级 `errors[]` + 413/503 优先级清晰。**但 ocdroid 尚未迁移**（仍走单条 `/full/{mid}` 404 fallback，见 §2）。 |
| `/since` tie-break `(updatedAt, messageID)` + cursor drain | ✅ 已 ratify | Gap1/3 闭环。 |
| q/p `scope.directories` 三态 | ✅ 已落地 | ocdroid 已消费（区分 scope 未就绪 / 权威空）。 |
| 错误体 `{code}` 统一 | ✅ 已落地 | circuit breaker 友好。 |

---

## §1. `GET /slimapi/sessions` — 完整性标记 + 分页 + roots 默认 ★最理想（最希望推进）

**【客户端观测】**
- cold-start snapshot 曾因默认 `limit=100` 误判为「全集」，用 100 **覆盖** 本地 374 条（ocdroid `6bf7bf7` 改 defensive merge 止损）；现 `7dc5134` 显式传 `roots=true + limit=500` 兜底。
- 但客户端**仍无法判断**返回的 N 条是否为该 directory 集合的**权威全集** —— q/p 有 `scope.directories` 标记，**sessions 没有**（`sessions.py:116-119` 返回裸 JSON 数组，无 envelope / scope / cursor）。
- `roots` 默认 `False`（`sessions.py:85`）→ 默认返回 subagent/task 子会话（实测 ~244 子行扇出），客户端必须显式传 `roots=true` 才排除。

**【已落地契约】** `?directory/roots/limit/start/search`（v1-contract §2）；`start` 偏移分页；无 `X-Next-Cursor`（仅 `/messages` 有）。

**【建议】** 给 `/sessions` 200 响应加**完整性标记 + cursor**；`roots` 默认改 `True`。两种 wire 形态：
- **★方案 A（最理想，加性、零 body 破坏）**：保留裸数组 body，改用**响应头**回传完整性：
  - `X-Scope-Directories: N`（与 q/p 一致：0=scope 未就绪）
  - `X-Complete: true|false`（true=该 directory 集合权威全集）
  - `X-Next-Cursor: <opaque>`（存在则还有更多页）
- 方案 B（一致性高，但破坏性）：body 改 envelope `{items, scope:{directories, complete, nextCursor}}`，与 q/p 对齐。

**【接口新增/调整要求】**
1. **调整**：`GET /slimapi/sessions` 200 响应增加完整性信号 —— **★推荐方案 A（响应头）**，零 body 破坏、加性。
2. **调整**：`roots` 默认 `False → True`（子会话改 opt-in，如 `?children=true`；符合「列表默认根会话」的客户端直觉）。
3. **新增**：`X-Next-Cursor` 响应头（与 `/messages` 一致），逐步替代 `start` 偏移。
4. **文档化**：`limit` 默认/上限（现 100/1000）、`X-Complete` 语义、cursor 消费规则。

---

## §2. partId 稳定性 + G6 迁移（确认 + 客户端跟进）★最理想

**【客户端观测】**
- ocdroid thin 列表拿到 placeholder partId（`thin_placeholder_*`），展开时 placeholder→real 对齐仍 `replaced=false`（**用户可感知「展开失败」**，本会话最大未完成项）。
- ocdroid 现仍走单条 `/full/{mid}`（404 fallback），**未迁移到 G6 batch**。

**【已落地契约】** G6 batch `/full?ids=` + 「按 `messageId+partId` 替换」语义（CLIENT_CHANGES 消息加载节）。

**【建议】** 契约显式保证 partId 跨端点稳定，二选一：
- **★方案 A（最理想）**：thin skeleton（`/messages/{sid}?mode=skeleton`）**直接返回真实 partId**（`prt_*`），去掉 `thin_placeholder_*` 层；展开按 `messageId+partId` 原地替换，零额外对齐字段。
- 方案 B：thin 必须用 placeholder（体积考虑）→ placeholder 带 `correlationId`，`/full` 响应回显 `replaces: correlationId`，客户端据此对齐。

**【接口要求】**
1. **确认/调整**：`/messages/{sid}?mode=skeleton` 的 part `id` 是否稳定且 = `/full`（单条 + batch）返回的 part `id`。若当前是 placeholder，按方案 A 改真实 id，或按方案 B 加 correlation 字段。
2. **客户端跟进**（独立，不阻塞契约）：ocdroid 迁移 G6 batch + 实现 placeholder→real 可靠替换。

---

## §3. reconfigure 主动失效信号（建议新增）★最理想

**【客户端观测】**
- 换 host/slim 配置（reconfigure）时，in-flight 请求失效靠 ocdroid **本地** `captureSlimCommitToken` + `commitIfSlimTokenCurrent` 比对（`d5d486a` 加固了 stale-token / session-switch gate，修了多个相关 bug）。
- 服务端**无主动 reconfigure 信号**，客户端被动检测；窗口期内 stale 结果可能误用。

**【已落地契约】** SSE `resync` 事件 `{reason:"reconnect_no_replay"}`（重连用）；**无** reconfigure/配置变更事件。

**【建议 ★最理想】** 新增 SSE 帧 `server.reconfigured`：sidecar/opencode 配置变更（进程重启 / allowlist 变更 / 上游重启）时主动推 `{reason, at}`，客户端收到即作废本地 token + 触发 cold-start 重拉。

**【接口要求】**
1. **新增 SSE event**：`server.reconfigured`（curated 帧类型，立即直推，不走 debounce）。
2. **文档化触发条件**：sidecar 重启、allowlist 变更、上游 opencode 重启。
3. 加性（curated 帧加法），不 bump version。

---

## §4. 版本协商 min/max 回显（建议完善）★最理想

**【客户端观测】** ocdroid fail-closed 自检 `X-Slimapi-Version:1`，但 client min/max 三元组**硬编码客户端侧**；契约 additive 变更不 bump version → 客户端无法动态探测服务端能力。

**【已落地契约】** `X-Slimapi-Version:1`（必带）+ `/health` 自检。

**【建议 ★最理想】** `/slimapi/health` 200 回显 `schema:{version, clientMin, clientMax}`，客户端动态协商；additive 变更时服务端更新 `clientMax` 而非 bump version。

**【接口要求】**
1. **调整**：`/health` 200 body 加 `schema.clientMin/clientMax`（当前 `version=1` 区间）。
2. **文档化**：additive 变更如何更新 `clientMax`（兼容性契约）。

---

## §5. 总结（优先级）

| § | 优先级 | 阻塞 ocdroid？ | 类型 |
|---|---|---|---|
| §1 sessions 完整性标记 + cursor + roots 默认 | **高** | 否（已兜底），但去掉兜底依赖它 | 加性（★响应头方案）/ 默认值变更 |
| §3 reconfigure 主动失效事件 | **高** | 否（本地 token 兜底），但显著降脆弱性 | 加性（新 SSE 帧） |
| §2 partId 稳定 + G6 迁移 | 中 | §2-A 落地后 ocdroid 可修「展开失败」 | 确认/加性 + 客户端跟进 |
| §4 version min/max 回显 | 低 | 否 | 加性 |

> §1 与 §3 是本次最希望推进的两项；§2 主要是确认 partId 稳定性 + ocdroid 侧迁移；§4 锦上添花。
