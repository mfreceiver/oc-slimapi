# oc-slimapi lite-v2 对接告知（致 ocdroid 项目组）

> 权威细节以本仓 `docs/specs/v2-contract.md` + `CHANGELOG.md` 为准；本告知 @ commit `128ab3a`。

## 1. 概要
oc-slimapi 已完成 **lite-v2** 改造：精简为 skeleton 投影 + digest + token stream 透传，删除精确同步协议。**Wire API 版本升至 2（breaking）**，经分领域确认 + 多轮 rev 评审 + glm 9.5 门控通过，已推送。ocdroid 须按本告知对齐。

## 2. 联调基线
- 仓库 `oc-slimapi` · 分支 **`lite-v2-dev`** · 冻结 commit **`128ab3a`**（含 `a21e0fd` 确认 pass + `128ab3a` 契约更正）
- 仓库内部署 unit 已对齐 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`、`OC_SLIMAPI_SERVER_API_VERSION=2`（**线上运行的服务需重新部署才生效**）

## 3. ocdroid 必须对齐的 wire 变更

### 3.1 版本门禁（必改）
- 所有 `/slimapi/**` 请求必带 **`X-Slimapi-Version: 2`**
- 门闩 `(2,2)`：缺/非整数→400 `version_required`；发 `1` 或其它→400 `version_incompatible`（**v1 客户端将被拒**）
- 能力探测读 `/slimapi/health` 根级 **`slimapi_contract: 2`**（**不要**依赖 `server.api_version`）

### 3.2 digest 帧（session.digest）字段收敛
字段集（**已删 `contentRevisions`、`childrenVersion`，不再消费**）：
`sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?`
- **`updatedAt` 语义变更**：现为 **sidecar wall-clock**，**跨窗口不保证严格单调**（debounce 合并 / 进程重启 / NTP 都可能相等或回退）
- **比对规则（必改）**：watermark 用 **`(updatedAt, messageID)` 二元组字典序**——先 strict 比 `updatedAt`（`>` 才推进时间维；相等/回退**不删**已有消息）；时间相等时 strict 比 `messageID`。**不得**假定 updatedAt 严格递增
- **part 事件不触发 digest**：digest 仅由 `session.*` / `message.updated` / `message.appended` 驱动；`message.part.updated` / `message.part.removed` 不再产生 digest

### 3.3 拉消息端点
- `GET /slimapi/messages/{sid}`：恒 **skeleton 投影**；`?mode=full` 被静默忽略（不报错）；按 `time.created` **升序**；分页 `?limit` + `?before`，200 响应带 **`X-Next-Cursor`** 头（opaque 游标，解析自 upstream Link；**无该头 = 末页**）
- `GET /slimapi/messages/{sid}/full/{mid}`：**恒 200** full body，**无 304 / 无 ETag / 无 `X-Message-Event-Seq` / 无 `?known.*`**
- 更新触发：digest 推 `(messageID,updatedAt)` → 重拉 `/messages/{sid}` skeleton，按 messageID 去重合并

### 3.4 token stream（`GET /slimapi/sessions/{sid}/stream`）
- **整体保留不变**（gzip 默认开、独立 T3 账本、opt-in）
- **`message.removed` 帧保留**（重要）：token-stream 连接在握手期（回放）或运行时（fan-out）可能收到 `event: message.removed {sessionID,messageID}` → ocdroid 须**丢弃该 message 的全部 live 渲染态**（streamOwned parts）。控制面 digest `deleted=true` 是独立信号，二者不替代
- `directory` query 对 fanout 是 **no-op**（仅 query 与 `X-Opencode-Directory` 头冲突时返 400 `directory_not_allowed`）

### 3.5 错误码（结构化）
- upstream 404 → 404 `session_not_found`（带 `sessionID`）
- upstream 其它 4xx → 502 `upstream_http_N`
- upstream 5xx / 坏 JSON → 503 `upstream_unavailable`
- thin 路由错误体统一 `{"code":...}`（非 `{"detail":...}`）

## 4. ocdroid 不应再实现的路径（已删除）
- **routeToken 全下线**：无 `/questions`、`/permissions` 聚合，无 `invalid_route_token`；q/p 应答改走 **catch-all 反代 + `X-Opencode-Directory` 头**直连上游 opencode
- **删除端点**（调用将 404）：`/projects`、`/questions`、`/permissions`、`/sessions/status`、`/sessions/{sid}/status`、`/sessions/{sid}/children`、`/messages/{sid}/since/{ts}`、`/messages/{sid}/full?ids=`（批量）、q/p 写端点
- **discovery / allowlist 数据流下线**：无 `/projects`、`X-Discovery-Directories`、`X-Discovery-Ready` 头、`server.reconfigured` 帧
- **Stage B fingerprint 全删**：304 短路、`X-Message-Event-Seq`、`?known.*`、`X-Since-Complete`、`/since` 增量过滤
- **q/p SSE 事件仍直推**（`question.asked` / `permission.asked` 等），但仅作**观察信号**——应答不经 slimapi 专门端点
- **Opt-A 能力协商**：`X-Slimapi-Capabilities` 在 v2 被忽略（仍可发，sidecar 不分支）

## 5. 写路径
所有写（发消息、abort、q/p reply/reject、permission resolve）走 **catch-all 反代**，ocdroid 自带 `X-Opencode-Directory` 头（slimapi 不剥）。

## 6. 冷启动顺序（v2 最小集）
1. `GET /slimapi/health`（自检，fail-closed）
2. `GET /slimapi/sessions`（消费 `X-Complete` 头）
3. 当前 session：`GET /slimapi/messages/{sid}`（骨架初始集，升序）
4. 接 SSE `/slimapi/events`（digest + q/p asked + lastError）；`resync` / `server.connected` = 复用冷启动

## 7. 权威文档（本仓）
- **Wire 契约（唯一基准）**：`docs/specs/v2-contract.md`（`v1-contract.md` 已 deprecated）
- **ocdroid 配套改动清单**：`docs/specs/CLIENT_CHANGES.md`（含 token-stream `message.removed` 处理必须项）
- **接口行为变更记录**：`CHANGELOG.md`

## 8. 协调契约（双方对齐）
`X-Slimapi-Version: 2` ／ digest 字段集（删 contentRevisions/childrenVersion）／ `(updatedAt,messageID)` 二元组 tie-break ／ skeleton 升序 ／ token stream 透传（message.removed 保留）／ catch-all 透传。

## 9. 联调说明
- `lite-v2-dev` 为 **breaking wire bump**，ocdroid 须切到 `128ab3a` 联调
- **回滚必须双边同步**：P0 时 oc-slimapi 回到 `pre-lite-v2`（main 仍为 v0.12.0 `0de95bc`）、ocdroid 同步回滚（**不能只回一端**）
- 后续合 main：按计划 §11 双方同时合并，合并前 oc-slimapi 侧打 tag `pre-lite-v2-<date>`
