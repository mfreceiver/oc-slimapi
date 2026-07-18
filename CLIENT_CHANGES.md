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
