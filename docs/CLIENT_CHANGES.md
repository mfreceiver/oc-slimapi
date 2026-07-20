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
- foreground catch-up 使用 `/slimapi/messages/{sid}/since/{lastSeenUpdatedAt}`
  （`(info.time.updated or info.time.created) >= ts`，含边界；v1.18.3 无 message 级 `time.updated`，服务端实读 `created`，与 digest `updatedAt` 同源）；客户端按 messageID 本地去重边界；翻页用
  `X-Next-Cursor`（透传 opencode `Link` 头 opaque cursor）。无 409 resync。
- **per-session watermark 升级为 `(updatedAt, messageID)` 二元组字典序**（v0.2.1，ocdroid 缺口 1）：先 strict 比 `updatedAt`（严格 > 才推进时间维），相等时 strict 比 `messageID`（`msg_+ascending()` 单调，新消息 id 字典序更大 → 严格 > 才推进 id 维）。对齐上游 `(time_created DESC, id DESC)` 全序，解 strict-advance 在等时间戳不同 id 时的残留 bug。
- **无 watermark 的初始拉取推荐 cursor drain**（`?before` 游标分页，与 focus digest / resync 共享 reconciler）；`/since/0` 虽合法但非推荐路径（ocdroid 缺口 3 裁定）。

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
- **[Unreleased] directory allowlist gate 已移除**：slimapi 不再因 directory ∉ allowlist 返 400 `directory_not_allowed`。客户端任意 `?directory=`（包括未在 `/slimapi/projects` 列出的）会被原样规范化后透传给上游 opencode，由 opencode 决定能否服务。`directory_not_allowed` 错误码**仅**保留于 query `directory` 与 `X-Opencode-Directory` 头冲突的**结构性歧义**场景。其它结构性守卫未变：`invalid_directory_count`（显式 list 0 / >32）、`invalid_route_token`、版本门禁、upstream loopback SSRF guard。

### status 404 `session_not_found` 新分支

- `GET /slimapi/sessions/{sid}/status` 在 upstream discover 返 404 时，B1 起改为透传 **404 `{"code":"session_not_found","sessionID":"…"}`**（B1 前一律 503）。
- 客户端应区分：
  - **404 `session_not_found`** → 该 session 已被删除 / 不存在；UI 移除该会话行，**勿**当成可重试的网络错误。
  - **503 `upstream_unavailable`** → upstream 不可达 / 5xx / 坏 JSON；走 circuit breaker + 重试。
  - **502 `upstream_http_N`** → upstream 返非 404 的 4xx；按业务语义处理。
  - **400 `directory_not_allowed`** → **[Unreleased] slimapi 已完全移除 directory allowlist gate**：directory 不再因 ∉ allowlist 返 400；任意 directory 透传给上游 opencode。该错误码**仅**保留于 query `directory` 与 `X-Opencode-Directory` 头冲突的**结构性歧义**场景（messages `/**` 端点）。客户端此前因 allowlist miss 触发的 400 路径**不再发生**，opencode 自身的 4xx 会以 `upstream_http_N` 透传。

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
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}`，**无 replay**）。
- digest `lastError`：sticky 跨窗口，`status=busy` 清除（显式 `null` 帧）；客户端据此显隐 session 错误 banner。`MessageAbortedError` 被 sidecar 过滤，不下发。
- 客户端所有 `/slimapi/**` 请求（含 SSE）须带 `X-Slimapi-Version: 1`；连接时读 `/slimapi/health` 自检。

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
  - **显式 repeated `?directory=`**：去重保序，1–32；空/超 32 → 400 `invalid_directory_count`。[Unreleased] **不再因 directory ∉ allowlist 返 400**——任意 directory 透传给上游 opencode。
- **冷启动推荐顺序**（F3 暖机后仍建议遵循，避免竞态）：
  1. `GET /slimapi/health`（版本自检 + `schema.degraded`）
  2. `GET /slimapi/projects`（刷新 allowlist；F3 启动已 best-effort warm，本步仍是权威刷新）
  3. `GET /slimapi/sessions`（骨架列表）
  4. `GET /slimapi/questions` + `/permissions`（可 null directory）
  5. 按需 `GET /slimapi/messages/{sid}` / `/since/{ts}` / `/full?ids=`
  6. 再连 `GET /slimapi/events`
- routeToken reply/reject：[Unreleased] slimapi 已移除 allowlist gate，token 校验后 directory 直接 normalize 后透传给上游 opencode；冷启动空 allowlist 不再导致 400。`_token` 仅校验 HMAC 签名 + kind/requestID/sessionID/directory。
