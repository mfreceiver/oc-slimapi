# ocdroid 客户端改动清单（仅文档，不修改 ocdroid）

## 模型

- `Part` 墠加 `hasFull: Boolean? = null`、`omitted: List<String>? = null`。
- `QuestionRequest`、permission item 增加 `directory`、`routeToken`。
- 聚合返回模型改为 `{items,errors}`；errors 非空不能显示成权威空状态。

## 消息加载与展开

- 历史页走 `/slimapi/messages/{sid}?mode=skeleton`，翻页用 `X-Next-Cursor`
  （sidecar 从 opencode `Link` 头透传 opaque cursor，原样回传 `?before=`）。
- `hasFull=true` 的 part 首次展开时请求 `/slimapi/messages/{sid}/full/{mid}?mode=full`，
  按 `messageId+partId` 替换，禁止追加重复 part。
- foreground catch-up 使用 `/slimapi/messages/{sid}/since/{lastSeenUpdatedAt}`
  （`time.updated >= ts`，含边界）；客户端按 messageID 本地去重边界；翻页用
  `X-Next-Cursor`（透传 opencode `Link` 头 opaque cursor）。无 409 resync。

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
  例：`{"code":"session_not_found","sessionID":"ses_…"}`、`{"code":"directory_not_allowed"}`、`{"code":"upstream_http_409"}`、`{"code":"upstream_unavailable"}`、`{"code":"shell_not_allowed"}`。
- **`code` 即机器可读判别字段**；客户端错误处理 / circuit breaker 触发 / 用户文案分发应基于 `code`（而非解析 `detail` 字符串）。
- **messages / events / versioning** 既已使用 `{"code":…}`，本次仅是 sessions / questions / projects 对齐；ocdroid 侧已有解析器无需重写，但需扩展识别以下新增 / 显式化的 code：`session_not_found`(404)、`upstream_http_N`(502)、`upstream_unavailable`(503)、`invalid_directory_count`(400)、`invalid_route_token`(400)、`shell_not_allowed`(403)。
- **catch-all（非 `/slimapi/**`）错误体不变**：透传 upstream 原始 body；FastAPI 顶层异常可能仍为 500（无 `code`）。统一错误码表**仅适用于 thin 路由**。

### status 404 `session_not_found` 新分支

- `GET /slimapi/sessions/{sid}/status` 在 upstream discover 返 404 时，B1 起改为透传 **404 `{"code":"session_not_found","sessionID":"…"}`**（B1 前一律 503）。
- 客户端应区分：
  - **404 `session_not_found`** → 该 session 已被删除 / 不存在；UI 移除该会话行，**勿**当成可重试的网络错误。
  - **503 `upstream_unavailable`** → upstream 不可达 / 5xx / 坏 JSON；走 circuit breaker + 重试。
  - **502 `upstream_http_N`** → upstream 返非 404 的 4xx；按业务语义处理。
  - **400 `directory_not_allowed`** → directory 不在 allowlist；客户端不应重试同一 directory。

### `/slimapi/projects` 5xx 502→503 状态码变更

- B1 起 `/slimapi/projects` upstream 5xx / 网络异常由 **502 → 503 `upstream_unavailable`**（**状态码变更，非仅 body**）；upstream 4xx 仍走 502 `upstream_http_N`。
- 客户端若按精确 `502` 判 projects 失败（如 circuit breaker 硬编码 502），需改判 5xx-class（500-599），不要匹配精确 502。
- 与 `/slimapi/sessions/{sid}/status`（G2）5xx → 503 对齐；与 catch-all 透传 upstream 原状态码**不冲突**（catch-all 不重塑状态码）。

## SSE

- Phase 0 先支持 `/event` 裸帧归一化及 400/404/405/501 global 回退。
- Phase 2 改连 `/slimapi/events?directory=...&sessionId=...`；需要实时文本时显式
  `stream=1`。
- reducer 支持 curated 类型：`status.changed`、`message.appended`、
  `thin.session.dirty`、question/permission；SSE `event: resync` 触发
  latest-id/catch-up。
- sidecar 的 curated SSE 已是 `SSEEvent{directory,payload}`，不要再假设原始
  opencode part.updated 一定存在完整 part。
- 客户端所有 `/slimapi/**` 请求（含 SSE）须带 `X-Slimapi-Version: 1`；连接时读取
  `/slimapi/health` 的 `server.api_version` 与 `server.accepted_client_versions` 自检。
- OkHttp SSE 可直接设置版本头；浏览器 EventSource 若未来需要 query 兜底，可另行设计
  `?version=`，本版本不支持该兜底。
