# oc-slimapi v1 契约（唯一基准）

> 状态：契约收敛版（A1-A3/B1-B3/C1-C2 全定，A2=A 时间戳锚点）。配套原型（fix-3/9/10 + 现有 routes）已覆盖大部分；本文 🔒=原型已覆盖、🆕=v1 待补缺口。
> 权威性：本文件是正式实现的唯一基准。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。

## §0 范围与架构
- 纯 HTTP sidecar：FastAPI + httpx + orjson + uvicorn **单 worker**，loopback，stunnel mTLS 后。
- **不读 opencode SQLite**；仅 legacy `/session` API。
- v1 目标：**2-5 台同用户设备**（T3 硬化进 v1）。
- 客户端通过"切换服务器"进省流（R8：`mtls×slim` 两布尔→4 配置），非连接属性开关。

## §1 版本契约 🔒
- 头 `X-Slimapi-Version: <int>`，所有 `/slimapi/**` 必带。
- 门闩 `ACCEPTED_CLIENT_VERSIONS=(1,1)`：缺/非整数→400 `version_required`；越界→400 `version_incompatible`（带 `client`/`accepted`）。
- `/slimapi/health` 返回 `sidecar.ok` + `server.api_version` + `accepted_client_versions` + `schema.degraded`。
- bump 规则：整数，仅破坏性变更 bump；加性变更同版本。

## §2 端点

| 方法 | 路径 | 桶 | 状态 | 说明 |
|---|---|---|---|---|
| GET | `/slimapi/health` | A | 🔒 (gzip 🆕) | 版本+降级+self-check 信号 |
| GET | `/slimapi/ready` | A | 🔒 (gzip 🆕) | liveness |
| GET | `/slimapi/metrics` | A | 🆕 T3 | 订阅者/queue/hub 指标 |
| GET | `/slimapi/sessions` | A | 🔒 | 骨架 session 列表（`?directory/roots/limit/start/search`，默认排除 archived）；每条带 `directory` 字段，客户端侧过滤 |
| GET | `/slimapi/projects` | A | 🔒 | project/directory 发现 + allowlist |
| GET | `/slimapi/sessions/status` | A | 🔒 | 批量 status（`?directory`） |
| GET | `/slimapi/sessions/{sid}/status` | A | 🔒 | 单 ses status（id→directory 自洽） |
| GET | `/slimapi/messages/{sid}` | A | 🔒 | 骨架分页（`?limit/before/mode`） |
| GET | `/slimapi/messages/{sid}/since/{ts}` | A | 🔒 (语义 🆕) | **A2=A**：`time.updated >= ts` 的骨架；`?limit/before` 分页 |
| GET | `/slimapi/messages/{sid}/full/{mid}` | A | 🔒 | 单条全文（mode=full，展开某条） |
| GET | `/slimapi/questions` | A | 🔒 | 跨目录聚合 pending（`?directory` repeated 1-32），每条带 `routeToken` |
| GET | `/slimapi/permissions` | A | 🔒 | 同上 |
| POST | `/slimapi/questions/{qid}/reply` | A | 🔒 | routeToken 校验 + 注入 directory + 转发 opencode |
| POST | `/slimapi/questions/{qid}/reject` | A | 🔒 | 同上 |
| POST | `/slimapi/sessions/{sid}/permissions/{pid}` | A | 🔒 | 同上（`response: once/always/reject`） |
| GET | `/slimapi/events` | A | 🔒 (archived 🆕) | 实例级策展 SSE（见 §3） |
| * | `/{path}` (catch-all) | B | 🔒 | 透传 opencode（含发消息等写）；客户端发 `X-Opencode-Directory` 头过透传 |

### 写路径（B2）🔒
- q/p 应答：走 §2 的 routeToken 端点（routeToken 在 `/slimapi/questions`/`/permissions` 聚合响应里随条下发，绑 kind+requestID+sessionID+directory，HMAC ~1h）。
- 发消息/abort 等通用写：客户端走 catch-all 透传，自带 `X-Opencode-Directory` 头（现有 `DirectoryHeaderInterceptor`），slimapi 不剥（非 hop-by-hop）。
- routeToken 404/过期 → 透明（已应答/失效），客户端重取聚合。

## §3 SSE 契约（简化版，A1-A3 落定）🔒 + archived 🆕
- 上游：**一条** `/global/event`（进程级 GlobalBus，全实例跨目录，每事件自带 `directory`）。
- 帧：
  - `session.digest`（debounce 250ms/session，仅发有变化的字段）：`{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?}`。
    - `status`←`session.status`(idle/busy)；`messageID`+`updatedAt`←`message.updated`/`message.appended`（info.id + info.time.updated/created，取最新）；**`archived`←`session.updated` 的 `info.time.archived`（有值→true/时间戳）** 🆕；`deleted`←`session.deleted`。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`：**立即直推** `{directory, type, properties}`。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}`，无 replay）。
- 丢弃：`?stream`、text.delta、`message.part.*`、`tool.*`、`sessionId` 参数、per-directory hub。

## §4 冷启动 & resync（A1 + A3）🔒（客户端侧，暂停）
- 连接（及 resync）→ 客户端 GET `/slimapi/sessions` + `/slimapi/questions` + `/slimapi/permissions`（+ 当前打开 ses 的消息 `/since/{ts}`）→ SSE 接力增量。
- **resync = 复用冷启动流程**（同一"加载初始状态"代码路径）。

## §5 拉消息（A2=A 锁定）🔒 (语义 🆕)
- digest 推 `{messageID, updatedAt}`；客户端记本地该 ses 最大 `updatedAt`。
- 比对发现更新 → 拉 `/slimapi/messages/{sid}/since/{lastSeenUpdatedAt}`。
- 服务端返回 `time.updated >= lastSeenUpdatedAt` 的骨架；客户端按 messageID 去重边界。
- 分页：`?limit` + `?before` 游标。

## §6 资源限制（T3，C2=2-5 台进 v1）🆕
- `MAX_SUBSCRIBERS_PER_DIRECTORY=8`、`MAX_TOTAL_SUBSCRIBERS=16`。
- 每 subscriber buffer `2 MiB`、单帧 `256 KiB`；溢出→**立即清 queue/deltas/dirty** + 排 `resync{reason:subscriber_backpressure}` + STOP（替代当前"queue 尾排 STOP 继续发旧帧"）。
- admission 在 `HubRegistry.subscribe` 单一无 await 临界段；超限→503 `sse_subscriber_limit_directory`/`_total`（带 `limit`/`current`/`Retry-After`）。
- 转换池（fix-9 🔒）：`MAX_TRANSFORMS=1`，admission 在下载前，限长读 `MAX_RESPONSE_BYTES=64MiB`，parse/project/gzip offload worker thread。

## §7 错误码 🔒 + 🆕
- 400 `version_required` / `version_incompatible` / `thin_route_not_found` / routeToken 校验失败。
- 413 `response_too_large`（超 `MAX_RESPONSE_BYTES`）。
- 503 `transform_busy`（`Retry-After`）/ `sse_subscriber_limit_*` 🆕 / directory allowlist 刷新失败。
- 502/504 上游写超时/错误。

## §8 客户端 v1 最小集（C1，暂停 — ocdroid）
连接(R8)+版本头+health 自检(M2/fail-closed)+冷启动(sessions+q/p 快照)+SSE(digest+q/p)+digest 触发拉消息(`/since`)+发消息(X-Opencode-Directory 透传)+q/p 应答(routeToken)+resync=冷启动。**+ C3 health 改 `/slimapi/health`（fix-7 已落地）**。

## §9 gzip 🆕（小修）
所有 JSON 路由的 `json_response` 调用转发 `accept_encoding=request.headers.get("accept-encoding")`。sessions/questions 已做；health/ready 等补齐。

## §10 延后（非 v1）
skeleton 共享缓存（YAGNI，先指标）、多用户（独立 stack）、Part 展开 UI、sessions status 迁移、circuit breaker、metrics 之外的可观测。

## §11 v1 待补缺口清单（驱动 lane 派发）
1. 🆕 hub.py digest `archived` 字段（§3）。
2. 🆕 messages.py `/since` 语义 `time.updated >= ts` + `limit/before` 分页（§5）。
3. 🆕 T3 硬化：订阅上限 + buffer 字节预算 + 立即清式溢出 + `/slimapi/metrics`（§6）。
4. 🆕 gzip 清理：health/ready 等 JSON 路由转发 accept_encoding（§9）。
5. 核验（非实现）：发消息写路径经 catch-all + 客户端 `X-Opencode-Directory` 是否端到端 work（opencode `/session/{sid}/message` 是否认该头）。
