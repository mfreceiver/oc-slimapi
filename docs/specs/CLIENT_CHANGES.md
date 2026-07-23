# ocdroid 客户端改动清单（仅文档，不修改 ocdroid）

## 模型

- `Part` 墠加 `hasFull: Boolean? = null`、`omitted: List<String>? = null`。
- `QuestionRequest`、permission item 增加 `directory`、`routeToken`。
- 聚合返回模型改为 `{items,errors,scope:{directories:N}}`（v0.2.1：`scope.directories`=有效 scope dir 数，区分 scope 未就绪/权威空）；errors 非空不能显示成权威空状态。

## 消息加载与展开

- 历史页走 `/slimapi/messages/{sid}?mode=skeleton`，翻页用 `X-Next-Cursor`
  （sidecar 从 opencode `Link` 头透传 opaque cursor，原样回传 `?before=`）。
- `hasFull=true` 的 part 首次展开时请求 `/slimapi/messages/{sid}/full/{mid}?mode=full`，
  按 `messageId+partId` 替换，禁止追加重复 part。
- **partId 稳定性（rev F）**：schema-valid 消息下 thin skeleton 的 part `id` 与 `/full/{mid}` / `/full?ids=` 中的 part `id` **跨端点稳定**（真实 `prt_*`）。
- **placeholder → real 对齐（修「展开失败」）**：thin 在无可渲染 part 时仍可能注入 `id=thin_placeholder_{messageID}`。该 id **不会**出现在 `/full`。客户端判定：`partId.startsWith("thin_placeholder_")` → **message-level 整体替换**该 message 的 parts（禁止按 placeholder id 做 part-level lookup / `replaced=false`）。推荐同时迁 G6 batch（`/full?ids=`）。
- foreground catch-up 使用 `/slimapi/messages/{sid}/since/{lastSeenUpdatedAt}`
  （`(info.time.updated or info.time.created) >= ts`，含边界；v1.18.3 无 message 级 `time.updated`，服务端实读 `created`，与 digest `updatedAt` 同源）；客户端按 messageID 本地去重边界；翻页用
  `X-Next-Cursor`（透传 opencode `Link` 头 opaque cursor）。无 409 resync。
- **per-session watermark 升级为 `(updatedAt, messageID)` 二元组字典序**（v0.2.1，ocdroid 缺口 1）：先 strict 比 `updatedAt`（严格 > 才推进时间维），相等时 strict 比 `messageID`（`msg_+ascending()` 单调，新消息 id 字典序更大 → 严格 > 才推进 id 维）。对齐上游 `(time_created DESC, id DESC)` 全序，解 strict-advance 在等时间戳不同 id 时的残留 bug。
- **无 watermark 的初始拉取推荐 cursor drain**（`?before` 游标分页，与 focus digest / resync 共享 reconciler）；`/since/0` 虽合法但非推荐路径（ocdroid 缺口 3 裁定）。

## sessions 列表完整性头（rev F，新）

- `GET /slimapi/sessions` 200 响应头：
  - **`X-Complete`**：`"true"` = 本页 `len < limit`（未满）；`"false"` = 可能截断。**禁止**当「权威全集 / 权威空 / 结束冷启动」——上游无 total、无前向 cursor；`start` 是 epoch-ms 时间戳水位（`time_updated >= start`），**非 offset**。
  - **`X-Discovery-Directories`**：sidecar 发现目录数（allowlist 大小）。**不是**本次 query 命中数，**不是** q/p `scope.directories`。须与 Ready 联读。
  - **`X-Discovery-Ready`**：`"false"` = 尚无成功发现；`"true"` = 持有 last-known-good 快照（Directories 对该快照权威，**不保证实时**；后续刷新失败不复位）。
- 错误路径（502/503）**不**带三头。
- **`roots` 默认仍为 `false`**——客户端**应显式传** `roots=true` 以排除 subagent/task（ocdroid 已传）。
- 推荐：`limit=500` 兜底可保留，但用 `X-Complete` 判断是否可能截断，勿盲猜全集。

## 路由与失败策略

- thin 使用 stunnel 14097，direct 14096。
- GET circuit breaker：连续 3 次 transport/5xx 后禁 thin 5 分钟，再 half-open。
- mutation 只发一次，不因超时向 direct 重发。
- POST reply/reject/permission 回传 item 附带的 routeToken，不自行猜 directory。

## 错误体形状（thin routes）

> 对接 v1 B1（2026-07-18）后的形状变化；ocdroid 必须能解析新结构。

- **统一形状**：thin 路由（`/slimapi/sessions`、`/slimapi/sessions/{sid}/status`、`/slimapi/projects`、`/slimapi/questions`、`/slimapi/permissions`、`/slimapi/messages/**`）的错误体由 FastAPI 默认的 `{"detail":"…"}` 改为：
  ```json
  {"code": "<snake_case_code>", "message"?: "<short human-readable>", ...}
  ```
  例：`{"code":"session_not_found","sessionID":"ses_…"}`、`{"code":"directory_not_allowed"}`（仅 query/header 冲突场景，见下）、`{"code":"upstream_http_409"}`、`{"code":"upstream_unavailable"}`、`{"code":"shell_not_allowed"}`。
- **`code` 即机器可读判别字段**；客户端错误处理 / circuit breaker 触发 / 用户文案分发应基于 `code`（而非解析 `detail` 字符串）。
- **messages / events / versioning** 既已使用 `{"code":…}`，本次仅是 sessions / questions / projects 对齐；ocdroid 侧已有解析器无需重写，但需扩展识别以下新增 / 显式化的 code：`session_not_found`(404)、`upstream_http_N`(502)、`upstream_unavailable`(503)、`invalid_directory_count`(400)、`invalid_route_token`(400)、`shell_not_allowed`(403)。
- **catch-all（非 `/slimapi/**`）错误体不变**：透传 upstream 原始 body；FastAPI 顶层异常可能仍为 500（无 `code`）。统一错误码表**仅适用于 thin 路由**。
- **v0.2.1 修复**（ocdroid 缺口 2）：`GET /slimapi/sessions`（列表）失败路径此前**静默偏离**上述 coded 形状（upstream 4xx/5xx 原样透传 body、网络错落 FastAPI 默认 `{"detail":...}` 500）；现已对齐——4xx→502 `upstream_http_N`、5xx/网络→503 `upstream_unavailable`，body 为 `{"code":...}`。客户端若已按 `code` 解析（如 status/projects），sessions 列表现同样处理；若此前按"非 200 = 失败"粗判，行为不变。
- **v0.3.0** directory allowlist gate 已移除**：slimapi 不再因 directory ∉ allowlist 返 400 `directory_not_allowed`。客户端任意 `?directory=`（包括未在 `/slimapi/projects` 列出的）会被原样规范化后透传给上游 opencode，由 opencode 决定能否服务。`directory_not_allowed` 错误码**仅**保留于 query `directory` 与 `X-Opencode-Directory` 头冲突的**结构性歧义**场景。其它结构性守卫未变：`invalid_directory_count`（显式 list 0 / >32）、`invalid_route_token`、版本门禁、upstream loopback SSRF guard。

### status 404 `session_not_found` 新分支

- `GET /slimapi/sessions/{sid}/status` 在 upstream discover 返 404 时，B1 起改为透传 **404 `{"code":"session_not_found","sessionID":"…"}`**（B1 前一律 503）。
- 客户端应区分：
  - **404 `session_not_found`** → 该 session 已被删除 / 不存在；UI 移除该会话行，**勿**当成可重试的网络错误。
  - **503 `upstream_unavailable`** → upstream 不可达 / 5xx / 坏 JSON；走 circuit breaker + 重试。
  - **502 `upstream_http_N`** → upstream 返非 404 的 4xx；按业务语义处理。
  - **400 `directory_not_allowed`** → **v0.3.0** slimapi 已完全移除 directory allowlist gate**：directory 不再因 ∉ allowlist 返 400；任意 directory 透传给上游 opencode。该错误码**仅**保留于 query `directory` 与 `X-Opencode-Directory` 头冲突的**结构性歧义**场景（messages `/**` 端点）。客户端此前因 allowlist miss 触发的 400 路径**不再发生**，opencode 自身的 4xx 会以 `upstream_http_N` 透传。

### `/slimapi/projects` 5xx 502→503 状态码变更

- B1 起 `/slimapi/projects` upstream 5xx / 网络异常由 **502 → 503 `upstream_unavailable`**（**状态码变更，非仅 body**）；upstream 4xx 仍走 502 `upstream_http_N`。
- 客户端若按精确 `502` 判 projects 失败（如 circuit breaker 硬编码 502），需改判 5xx-class（500-599），不要匹配精确 502。
- 与 `/slimapi/sessions/{sid}/status`（G2）5xx → 503 对齐；与 catch-all 透传 upstream 原状态码**不冲突**（catch-all 不重塑状态码）。

## SSE

- 连接单一 `GET /slimapi/events`（**无 query 参数**——`directory`/`sessionId`/`stream` 在 v2 重写后已完全移除；全实例、全目录聚合，每事件自带 `directory`）。
- curated 帧类型：
  - `session.digest`（debounce 250ms/session）：`{sessionID,directory,status?,messageID?,updatedAt?,archived?,deleted?,lastError?}`。
  - **`session.error`（G1，立即直推，无 sid 时）**：`{directory?,name,message,at}`。客户端 UI：有 sid 已含在 digest 的 `lastError`（该 session banner）；无 sid → 全局 toast。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`（立即直推）。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}` / 背压 `{"reason":"subscriber_backpressure"}`，**无 replay**）。
  - **`server.reconfigured`（rev F，新）**：`{reason:"discovery_changed", at:<epoch-ms>}`。**仅** discovery 变更（allowlist 集合变 **或** 就绪态 false→true）时直推。收到即作废本地 commitToken / stale，触发 cold-start（与 resync 同路径、幂等）。
- **`resync` 路径未改**：上游重连/掉线/背压/Last-Event-ID 仍发 `resync`；**不**与 `server.reconfigured` 重叠（前者连接层，后者 discovery 层）→ 无双重 cold-start。
- **连接建立期 coalescing**：带 `Last-Event-ID` 重连时同连接可能先 `resync` 再 `server.connected`（既有）。客户端 **SHOULD** 对同一连接建立期 cold-start 触发帧做 once-latch（至多一次 reconcile）。
- **`server.heartbeat` ≠ 上游健康**：仅证 sidecar + 订阅存活；outage 探测用 `/slimapi/ready` 或自然 fetch/write 失败。sidecar 重启后重连收 `server.connected` → **应** cold-start。
- digest `lastError`：sticky 跨窗口，`status=busy` 清除（显式 `null` 帧）；客户端据此显隐 session 错误 banner。`MessageAbortedError` 被 sidecar 过滤，不下发。
- 客户端所有 `/slimapi/**` 请求（含 SSE）须带 `X-Slimapi-Version: 1`；连接时读 `/slimapi/health` 自检（见下 schema 三键）。

## health schema 回显（rev F，新）

- `/slimapi/health` 与 `/slimapi/ready` 的 `schema` 节：`{degraded, version, clientMin, clientMax}`（从服务端 config 读）。
- 旧 `server.api_version` / `server.accepted_client_versions` **保留**。
- **定位**：诊断用 wire 兼容范围回显；**非** feature discovery（version 保持 1 时三元组不变，无法探测 cursor/reconfigured 等能力）。

## 批量展开（G6，新）

- `GET /slimapi/messages/{sid}/full?ids=m1,m2,...`（1–20 mid，逗号分隔，去重保序）：批量展开多条 message。
- 响应 envelope：`{"items":[...], "errors":[{"messageID":..,"code":"message_not_found|message_too_large|upstream_http_N|upstream_error"}]}`；**mid 级部分失败仍 200 + errors[]**；全 mid 404 仍 200。
- **`items[]` 顺序 = ids 去重保序**；**`errors[]` 顺序 = 完成序（不保证与 ids 一致）**。
- **Top-level 错误（非 envelope）**：
  - ids 缺失 → 422；空 / 超 20 / 解析失败 → 400 `invalid_ids`。
  - discover 404 → 404 `session_not_found`（0 mid 拉取）。
  - discover 其它 4xx → 502 `upstream_http_N`。
  - discover 或任一 mid **网络失败**（`httpx.RequestError`）/ discover 5xx / discover 坏 JSON → **503 `upstream_unavailable`**（整请求）。
  - 累计字节超限 → 413 `response_too_large`（整请求，非单 mid）。
  - `mode=skeleton` 转换池饱和 → **503 `transform_busy`** + `Retry-After` 头。
  - **503 优先于 413**（网络失败与累计超限同时成立时返 503）。
- **Mid 级 envelope（整请求仍 200）**：
  - mid 404 → `message_not_found`。
  - mid ≥400（**含 5xx**）→ `upstream_http_N`（mid 5xx **不**升级整请求）。
  - mid 超 `max_message_bytes` → `message_too_large`。
  - mid 2xx 坏 JSON → `upstream_error`。
- 推荐使用此端点替代「N 并行 `/full/{mid}`」（ocdroid 现走 404 fallback，升级后首调即 200）。

## q/p 聚合 directory 可选（F1，新）+ 冷启动顺序

- `GET /slimapi/questions` / `GET /slimapi/permissions`：query `directory` 由**必填改可选**。
  - **不传**（null）：聚合 sidecar allowlist 全部目录（不受 1–32 上限；allowlist 空 → 200 空 `{items:[],errors:[],scope:{directories:0}}`，不再 cold-start 422）。
- **envelope `scope` 字段**（v0.2.1，ocdroid 缺口 2）：`/questions` + `/permissions` 的 200 响应含 `scope: {directories: N}`（N = 有效 scope dir 数：null 路径=allowlist 大小，显式路径=去重后 dir 数）。
  - `scope.directories == 0` → **scope 未就绪**（allowlist 空，sidecar 启动早于 opencode）：冷启动**不**清本地 stale，等 scope 就绪后重拉。
  - `scope.directories > 0 && items == []` → **scope 就绪、权威空**：可清本地 stale。
  - `scope.directories > 0 && items != []` → 正常 pending 列表。
  - 503 全失败响应**不含** `scope`（失败时语义无意义）。
  - **显式 repeated `?directory=`**：去重保序，1–32；空/超 32 → 400 `invalid_directory_count`。**v0.3.0** **不再因 directory ∉ allowlist 返 400**——任意 directory 透传给上游 opencode。
- **冷启动推荐顺序**（F3 暖机后仍建议遵循，避免竞态）：
  1. `GET /slimapi/health`（版本自检 + `schema.degraded` + `schema.version/clientMin/clientMax`）
  2. `GET /slimapi/projects`（刷新 allowlist；F3 启动已 best-effort warm，本步仍是权威刷新）
  3. `GET /slimapi/sessions`（骨架列表；消费 `X-Complete` / `X-Discovery-*` 三头）
  4. `GET /slimapi/questions` + `/permissions`（可 null directory）
  5. 按需 `GET /slimapi/messages/{sid}` / `/since/{ts}` / `/full?ids=`
  6. 再连 `GET /slimapi/events`（消费 `server.reconfigured` + 连接建立期 coalescing）
- routeToken reply/reject：**v0.3.0** slimapi 已移除 allowlist gate，token 校验后 directory 直接 normalize 后透传给上游 opencode；冷启动空 allowlist 不再导致 400。`_token` 仅校验 HMAC 签名 + kind/requestID/sessionID/directory。

---

## Opt-A 体验优先（v0.3.1，wire 保持 1）

### 能力协商 (Opt-A)

客户端通过 HTTP 头选择是否 opt-in partial-envelope：

- **发送**：`X-Slimapi-Capabilities: mid-partial-envelope=1`（加性，非 wire bump）。
- **语法**：逗号切分 token，trim 空白，每个 token 须恰含一个 `=`，name 大小写不敏感，value 字面比较。未知/格式错误 token 忽略。重复且值冲突 → fail-closed（该 capability 按非 opt-in 处理，并计入服务器 `capabilityConflicts` 指标）。
- **旧客户端**（不传能力头）保持现有行为（legacy 语义）。

### Envelope 形状（B2）

部分成功（partial）、全部失败（errors-only）、整批网络失败等场景的响应形状：

```text
# 成功（items 非空，errors 空）
200 { "items": [...], "errors": [] }

# partial（items 与 errors 均非空）
200 { "items": [...], "errors": [ { "messageID": "...", "code": "upstream_http_500" }, ... ] }

# errors-only（items 空，errors 非空）——可为 terminal 或含可重试 code
200 { "items": [], "errors": [ { "messageID": "...", "code": "message_not_found" }, ... ] }

# 整批 network 失败（全部 ids 均为 RequestError，无任何其它 envelope error）
503 upstream_unavailable  （仅 opt-in 全失败场景；非 opt-in 同样 503）
```

- **errors 项结构**：`{ "messageID": "<mid>", "code": "<snake_case_code>", "retryAfterMs"?: <int> }`。
- **codes 分类**：
  - **mid-terminal**：`message_not_found`、`message_too_large`、及按契约归类为终态的 4xx。
  - **mid-retryable**：`upstream_http_5xx`、`upstream_http_429`、及 Opt-A 映射出的 `upstream_unavailable`（前半成功场景）。
  - **network error**（仅 opt-in 映射）：`upstream_unavailable`（后半无成功场景不映射，仍顶层 503）。
- **invariant**：items/errors 按 messageID 互斥幂等，顺序无关。

### Retry-After

- **顶层 503**（opt-in 全失败）：HTTP 响应头 `Retry-After: 1`（秒），或透传上游 int-seconds。客户端视作整体预算的一部分。非 opt-in 503 **不** 含 `Retry-After`（回归 legacy 语义）。
- **Per-mid envelope**（opt-in 且存在成功 item 或其它 envelope error）：`errors[].retryAfterMs`（毫秒，≤10000）。典型值：
  - `upstream_unavailable`（network）→ 200ms。
  - `upstream_http_429`/`upstream_http_5xx` → passthrough upstream Retry-After(ms,capped 10000) 或 200。
- **客户端 cap**：`retryAfterMs` 超 10s 时裁为 10s。
- **backoff**：首次重试 200ms，二次 400ms（±30% 抖动）；遵循 §5 P0-A 预算表。

### B1 预算（客户端侧 413 恢复）

- **服务器保证**：顶层 413 `response_too_large`、不返 partial、不泄露完成态。
- **客户端恢复算法**：halve（拆半）+ merge（合并）+ singleton（单 mid 副本不重复拉）。详细分区公式、并发上限、重试次数等参见 ocdroid 侧预算模型（rev 6 §5 P0-A）。slimapi 侧不改变此。

### G-F1 cursor-walk 降级

当 `/since` 检测到异常（重复 cursor、nextCursor 返回但页内无新 mid 经 dedup 后为空、或 digest 不一致）时，客户端应降级为 cursor-walk：

- **端点**：`GET /slimapi/messages/{sid}`（`?before` cursor，无 timestamp 过滤）。
- **机制**：复用已有 `fetchSlimInitialWindowBounded`（T11 round-4，`bumpBookmarkOnPartialFailure=false`）。maxPages 公式、wall-clock 30s、dedup by messageID HashSet。
- **触发源**：digest-probe 不一致、`server.connected` 新 generation、`server.reconfigured`、用户手动刷新。自动合并触发（同 connection generation 最多 1 in-flight + 1 trailing，最小间隔 15min）。
- **诚实标注**：该 endpoint 与 `/since` 共用上游 newest-first 排序/tie-break，故仅规避 `/since` 的 timestamp-filter 边界，不抵御上游排序 bug——由 G-F1 fixture + 周期 re-sync 兜底。

---

> **更新纪律**：以上 Opt-A 行为变更须同步反映在 `docs/specs/v1-contract.md` §15 及 `CHANGELOG.md` v0.3.1 节。

---

## Token stream SSE（Stages A–E 落地，opt-in 实时流 — design-token-stream.md §9/§10）

> **状态**：服务端 Stages A–E 已落地（A 地基 9.5 / B 生命周期 9.5 / C flush 9.5 / D 端点 9.6 / E 文档+预算 4+4）。未随当前发版出货前 `GET /slimapi/health` 根级 `features.tokenStream` 缺省，ocdroid 走既有「完成后整条出现」路径，**零回归**。本节是 ocdroid 侧改动清单（对应设计 §9 的 8 项 + §10 硬约束），供客户端预读。设计权威以 `docs/specs/design-token-stream.md` 为准；wire 以 `docs/specs/v1-contract.md` §3.x + §6.x 为准。

### capability 探测（必须）

- `/slimapi/health` 根级 **`features.tokenStream === true`** 才启用 stream 客户端；缺字段 / 404 / 405 → 降级为既有「完成后整条出现」（`/since` 拉权威全文），**不得**尝试连 stream 端点。
- 路径与版本头以**本仓库** `docs/specs/v1-contract.md` + `CHANGELOG.md` 为准（端点 `GET /slimapi/sessions/{sid}/stream`，仍带 `X-Slimapi-Version: 1`，**不 bump**）。

### stream 客户端生命周期（必须）

- 前台 opt-in 连 `GET /slimapi/sessions/{sid}/stream`；切后台 / 换 session / 关页面 → **立即断开**（token 订阅独立 T3 账本，预算「同时最多 1 条前台 stream」）。
- 连接独立于控制面 `/slimapi/events`——两条连接，互不替代。
- `Last-Event-ID` 可带但**值被忽略**，仅触发首帧 `resync{reason:"reconnect_no_replay",sessionID}`。stream **不发 SSE `id:`、无 replay buffer**——客户端不得依赖 `id:` 续传。

### streamOwned 渲染算法（必须）

收到帧按 part（`(messageID, partID)`）维护本地「streamOwned」缓冲：

- **`message.part.snapshot{done:false}`**（订阅首帧 / 握手锚点）→ **替换**该 part 本地缓冲为 `text`、标 `streamOwned=true`、未完成。
- **`message.part.delta{text}`** → 仅当该 part 已 `streamOwned` **且未完成**时 **append** `text`；否则丢弃（不应发生；若发生视为乱序，忽略）。
- **`message.part.snapshot{done:true}`**（终态，**杠杆1**）→ **仅完成 marker，无 text**——客户端**不再从该帧取 text**；标**完成**。权威全文走 `/since`（持久化真值，幂等且**凌驾**所有 token 帧）。此后该 part 不再收 delta（违反则忽略）。
- **`/slimapi/messages/**` / `/since`**：part 已 `streamOwned` 且**未完成** → **忽略**持久化拉取的该 part text（stream 为准）；part 已 `streamOwned` 且**已完成** → 仅允许 `/since` 覆盖（`/since` 是持久化真值，幂等且**凌驾**所有 token 帧）。

### truncated / 降级（必须）

- 收 **`message.part.snapshot{truncated:true}`**（`done:false` 或 `done:true` 均可能）→ 清该 part `streamOwned`、停 append、走 `/since` 拉权威（可能被上游截断，但那是真值）。单 part >1MiB **不**走 resync，而是本路径。

### resync 处理（必须）— 两档恢复

收 **`resync{reason, sessionID}`** 时，**一律**先：丢弃该 sid 全部 token 渲染态（所有 streamOwned part 清空）→ `/since` 重拉权威。是否 **重订阅** stream 按 reason 分档：

| reason | 清态 + `/since` | 重订阅 stream | 说明 |
|---|---|---|---|
| `reconnect_no_replay` | 是 | **是** | 无 replay；新连接拿 handshake snapshot |
| `subscriber_backpressure` | 是 | **是** | 慢消费者被断；须重连 |
| `token_memory_limit` | 是 | **是** | 服务端 LRU 驱逐一个 LivePart 后**保持连接**、**不**对现有 sub 重发 snapshot；仅清态会让后续 delta 成 orphan → **必须**重连以 `attach_subscriber` 重建锚点 |
| `session_idle` | 是 | 否 | 上游 idle，该 sid live parts 已 retire；socket 可留 |
| `session_deleted` | 是 | 否 | 会话终态；eviction 由 `/events` digest 独立驱动 |
| 未知 reason（客户端 fallback） | 是 | 否（建议） | 与 idle 同保守路径；勿静默丢帧 |

- token resync **恒带 `sessionID`**；若极端情况收到无 `sessionID` 的 resync，从**连接**推断 sid（token 流每连接绑单 sid）。
- **不发** `part_too_large`（超限走 `snapshot{truncated:true}`）。

### 终态对齐（必须）

- digest `message.updated`（step-finish）→ `/slimapi/messages/{sid}/since/{ts}` 拉权威全文，**幂等覆盖**该 message 所有 part（含 token streamOwned 已完成的）。客户端可接受 digest 完成先于 / 晚于 token `snapshot{done:true}`；`/since` 替换幂等且凌驾所有 token 帧。

### 预算与 UX（建议 / 可选）

- **建议**：同时最多 1 条前台 stream 连接（独立于 `/events`）。
- **可选**（busy-open UX）：打开 busy session 先占位（skeleton / 进度指示），直到 stream 首帧 `snapshot{done:false}` 到达再开始流式渲染。

### 批大小调参：ocdroid 无需配合（§10 硬约束）

- **不需要**任何 ocdroid 侧调整。`TOKEN_FLUSH_SECONDS`(100ms) / `TOKEN_FLUSH_BYTES`(4KiB) 是**服务端 env knob**，不进 wire，服务端可单方面调。
- **硬约束**：渲染须**对任意 batch 稳健**——每帧 `message.part.delta` 当作「待追加文本段」处理（append `text`），**不按 token 计数**、**不假定帧间隔**、**不假定单帧 token 数**。批式参数服务端调，ocdroid 无需跟随改动。

---

## status / active 轮询降频（收敛步骤 1 · 规划，slimapi 零改动）

> **状态：规划中**（透传接口收敛 · 步骤 1）。slimapi 侧**无代码 / 契约 / wire 变更**；本节是 ocdroid 侧行为优化建议，待联合确认。依据：access log 7d 频次 + exp-1 上游事件源码确认（fork/status/active）。

### 现状（access log 近 7 天）
- `GET /session/status` 3740 次 + `GET /api/session/active` 3038 次 = **6782 次**透传（ocdroid 经 Tailscale 1560 + 本机 5222），约 4s 均匀轮询。
- 两者经 catch-all 透传 opencode，**未走 slimapi 省流路径**。

### 上游事实（exp-1 源码确认）
- **`session.status` 事件覆盖率完整**：所有 idle↔busy↔retry 迁移（含 prompt 执行 / cancel / abort / retry）均经 `SessionStatus.set` 发 `session.status` GlobalBus 事件（`packages/opencode/src/session/status.ts:39-48` + `processor.ts` / `run-state.ts` / `prompt.ts` 全路径）。**无遗漏路径**。
- **digest wire 承诺范围（收窄，rev-gpt 终审校正）**：上方是**上游事件事实**；但 hub 会原样存储 status 字符串（接受任意值），**契约 §3 digest `status` 只承诺 `idle|busy`**。客户端**不得**把 digest `status` 当完整状态机枚举；未知值应保守处理（fallback 受控轮询或 cold-start 重拉），不得静默假定语义。`active`/`running` 只按 `busy` 推导。
- **`/api/session/active` 是 v2 协议端点**，返回 `Record<sid,{type:"running"}>`，语义 = `/session/status` 的 `busy` **子集**（`packages/protocol/src/groups/session.ts:146-155`）。slimapi `/slimapi/sessions/status` 已返回全量 `{idle|busy|retry}` map，客户端过滤 `type=="busy"` 即等价覆盖，**无需新增端点**。

### 客户端改造（ocdroid）
1. **停止 4s 轮询** `/session/status` 与 `/api/session/active`（透传路径）。
2. **冷启动一次** `GET /slimapi/sessions/status`（全量 status map）建立基线；`active` 的 `running` 语义改由本地按 `type=="busy"` 过滤推导。
3. **SSE digest `status` 增量接力**：`session.digest` 早已带 `status`（契约 §3）；客户端按 digest 更新对应 session 的本地 status。
4. **断连 fallback**：SSE 断连期降级为受控轮询（建议 10–30s，非 4s）；重连收 `resync` / `server.connected` → cold-start 全量刷新，不丢失 busy 状态。

### 收益
- ocdroid：1560 次/7d 轮询归零（**省电 + 弱网稳定**，干掉持续 RTT）。
- 本机 / upstream：5222 次/7d upstream QPS 归零（`/session/status` + `/active` 不再打 opencode）。
- slimapi：**零改动**（digest `status` + `/sessions/status` 早已就绪）。

### 前置与风险
- 无 wire / 契约变更；无 slimapi 代码改动。
- 唯一前提：信任 SSE `session.status` 覆盖率（exp-1 已源码确认完整）+ 断连 fallback 不丢 busy。

(End of file)
