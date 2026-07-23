# oc-slimapi 设计

> 硬约束：**纯 HTTP、禁 SQLite、只建在 legacy `/session`、loopback-only、stunnel mTLS、不替换 `List<MessageWithParts>` 形状**。
> 版本契约：路径固定为 `/slimapi/**`，版本走必填请求头 `X-Slimapi-Version: <int>`；版本门禁仅接受配置闭区间，catch-all 不受影响。

---

## 0. 关键设计决策

1. **tool skeleton 不发明 `inputSummary` 键**——客户端从 `state.input` 派生。保留**瘦身后 `state.input`**（白名单键）。
2. **reasoning skeleton 保留 `text`**（删 text 会触发 `isEffectivelyRenderableEmpty` 把整条纯思考消息过滤掉；保留全文是 UI 安全权衡，体积影响见 §6 实测）。
3. **file `url`**：`data:` 剥/null、`http(s)` 短则留。
4. **sessions 无 `cursor`**（legacy 用 `start`）；保留 `summary{additions,deletions,files}` + `revert{messageID,partID}`；Session 模型无 cost/tokens（在 Message 上）。
5. **写路径 directory**：用**持久 secret 签名的 stateless routeToken**（systemd `LoadCredential`，非 SQLite），抗 sidecar 重启；upstream 404→透传（已答）。
6. **增量单独成接口** `/messages/{sid}/since/{ts}`（A2=A 时间戳锚点）：禁静默截断。
7. **SSE replay 降级**：无 event store→重连发 `event: resync`，不承诺 Last-Event-ID 补发。
8. **SSE 桥（session.digest 合并帧）是核心实时通道**，非可选阶段。
9. **客户端模型必须扩字段**：`Part.hasFull/omitted`、`QuestionRequest.directory`、questions/permissions 返回 `{items,errors}`、SSEClient 裸帧归一化+404 回退。
10. **部署用双 stunnel**（14096 直连 + 14097 经 sidecar）保障真回退。
11. **sidecar 自 gzip** + `Vary: Accept-Encoding`；gzip 实测列上线门禁。
12. **skeleton 内存保护**：upstream body 上限（64MB）+ 转换并发（≤2）+ `MemoryMax=384M`。

---

## 1. 接口契约（参数 + 与 opencode 的精确转化）

### 1.1 `GET /slimapi/sessions`
- **参数**：`directory`(str?, 绝对路径；**v0.3.0** normalize 后透传，不 gate allowlist)、`roots`(bool)、`limit`(int 1–1000 默认100)、`start`(int? ms,透传)、`search`(str?)。**无 cursor**。
- **upstream**：`GET /session?directory=&roots=&limit=&start=&search=` + `X-Opencode-Directory`。
- **转化**（字段白名单）：
  - **留**：`id, directory, parentID, projectID, title, agent, model{id,providerID,variant?}, time{created,updated,archived}, summary{additions,deletions,files}, revert{messageID,partID}`
  - **剥**：`summary.diffs/summary_diffs, revert.snapshot, revert.diff, metadata, share, slug, version, path, permission`
- **响应**：`Session[]` 裸数组（不套 envelope）。

### 1.2 `GET /slimapi/projects`
- **参数**：无（始终含 directories）。
- **upstream**：`GET /project` → 对每个 id 并发 `GET /project/{id}/directories`（URL-encode，并发限流）。
- **转化**：每项 `{id, name, worktree, directories:[{path:<-upstream.directory>, strategy:<-upstream.strategy>}]}`。剥 `vcs/icon/time/sandboxes/commands`。
- **用途**：维护 discovery 数据集（worktree ∪ directories[].path，规范化去尾斜杠）供 `/projects` 展示与 q/p null-dir 聚合；**v0.3.0 起不再作写路径 allowlist gate**。

### 1.3 增量查询策略
增量走 §1.5 的时间戳锚点接口 `/slimapi/messages/{sid}/since/{ts}`；冷启动/resync 直接走 §1.4 + §1.5 + SSE digest 接力。**不提供**单独的"最后消息 ID 探针"端点。

### 1.4 `GET /slimapi/messages/{sid}`（历史分页，核心省流）
- **参数**：`sid`；`limit`(int **1–200** 默认40，**0→422** FastAPI ge=1)；`before`(str?, opaque, 原样透传)；`mode`(enum `skeleton`|`full` 默认 skeleton；`lite` 延后)；`directory`(透传)。
- **upstream**：`GET /session/{sid}/message?limit=&before=` + `X-Opencode-Directory`。
- **响应**：`List<MessageWithParts>` **裸数组**（不套 envelope）；skeleton 模式 sidecar **解析上游 `Link: <...?before=CURSOR>; rel="next"` 头** → 下发 `X-Next-Cursor`（opaque base64url 字符串，不 decode/re-encode），**不再把 upstream `Link` 头原样复制给客户端**；full 模式流式透传（含 upstream `Link` 原样）；`Cache-Control:no-store`。
- **裁剪**：顺序/id/数量不变；按 §2 规则；`info` 保留 `tokens/cost`（上下文用量）。
- **保护**：upstream body >64MB 或转换并发超限 → 413/503；`mode=full` 流式透传不缓冲。

### 1.5 `GET /slimapi/messages/{sid}/since/{ts}`（增量，A2=A 时间戳锚点）
- **参数**：`ts`(path, epoch ms，客户端本地该 ses 最大 `updatedAt`)；`limit`(int 1–200 默认50)；`before`(str?, opaque, 来自上一响应 `X-Next-Cursor`，原样透传)；`mode`(enum `skeleton`|`full` 默认 skeleton)；`directory`(透传)。
- **行为**：在**单个 transform admission + 单个累计字节预算**（`max_response_bytes`）下翻最多 `max_since_pages` 页（不暴露）；过滤条件 **`(info.time.updated or info.time.created) >= ts`**（含边界；客户端按 messageID 去重边界；v0.2.1 勘误：opencode v1.18.3 无 message 级 `time.updated`，实读 `created`，与 digest `updatedAt` 同源）；skeleton 时调 `skeleton_messages()`。
- **`X-Next-Cursor`**：透传 opencode 响应 `Link: <...?before=CURSOR>; rel="next"` 头里的 **opaque base64url cursor**（原样字符串，不 decode/re-encode）。仅在"填满 limit 且未撞 ts 地板且 opencode 给了 Link"时下发；客户端回传的 `X-Next-Cursor` 经 `?before=` 原样转发给 opencode。
- **ts 地板**：扫描中遇到 `(time.updated or time.created) < ts` 的项 → 停（后续都更旧），抑制 `X-Next-Cursor`。
- **响应**：200 骨架裸数组 + `Cache-Control:no-store` + `X-Next-Cursor`（仅当有续且未撞地板），gzip+`Vary`；累计字节超限→413 `response_too_large`。
- **注**：A2=A 锚点（时间戳），覆盖该 ses 自 `ts` 起所有更新；中段变更（revert/compaction）靠 SSE digest 或回前台 full resync。

### 1.6 `GET /slimapi/messages/{sid}/full/{mid}`（按需展开）
- **参数**：`sid`、`mid`、`mode`(默认 `full`——展开场景)。
- **upstream**：`GET /session/{sid}/message/{mid}`。
- **响应**：单 `MessageWithParts`；full 原样；`Cache-Control:no-store`、禁 body 日志、限并发。
- **超限**：>32MB（对齐客户端 `ResponseSizeGuardInterceptor`）→`413 {"code":"message_too_large","limitBytes":..}`。
- **客户端合并**：按 `messageId+partId` 替换（非追加）。
- **路径段 `full`**：仅作"展开全文"语义占位，并非 `mode=full` 的默认——`mode` 仍由 query 控制，默认 `full`。

### 1.7 `GET /slimapi/questions` / `GET /slimapi/permissions`（聚合）
- **参数**：`directory`(repeated，**可选**；显式传时 `?directory=/a&directory=/b` 规范化去重 ≤32 后透传；null=聚合 discovery allowlist 全部 dir)。**禁逗号拼接**。
- **upstream**：每 directory 并发 `GET /question`/`GET /permission` + `X-Opencode-Directory` + `?directory=`（2s 超时）。
- **响应**（聚合 envelope，允许——仅消息列表不套）：
  ```json
  {"items":[{"<原 question/permission 对象>,"directory":"/a","routeToken":"<sig>"}],
   "errors":[{"directory":"/b","code":"upstream_timeout"}],
   "scope":{"directories":2}}
  ```
  部分失败仍 200 + errors 非空；全失败→503（**不含** `scope`）。`scope.directories`=本次有效 dir 数（null 路径=allowlist 大小；显式=规范化去重后数；v0.2.1，区分 scope 未就绪/权威空）。
- **routeToken**：base64url(payload).base64url(hmac)；payload=`{v,kind,requestID,sessionID,directory,iat,exp}`；secret 经 systemd `LoadCredential`（持久、非 SQLite）；exp ~1h。

### 1.8 写：`POST /slimapi/questions/{qid}/reply|reject`、`POST /slimapi/sessions/{sid}/permissions/{pid}`
- **body**：reply `{answers:[[..]], routeToken}`；reject `{routeToken}`；permission `{response:"once|always|reject", routeToken}`。
- **校验**：token 签名/过期/kind/path-id 一致；directory 经 `normalize` 后透传上游（**v0.3.0 起不再** `directory∈allowlist` gate）。
- **upstream**：`POST /question/{qid}/reply|reject?directory=&X-Opencode-Directory:` + body(去 token，只 `{answers}`)；permission `POST /session/{sid}/permissions/{pid}` body `{response}`。
- **结果**：2xx→204；upstream NotFound→透传（已答/不存在，客户端刷新 pending）；400→400；timeout→504 不自动重试。
- **`always`** 后失效整个 directory 的 permission 缓存。

### 1.9 `GET /slimapi/sessions/status`
- **批量**：`?directory=`(必填) → `normalize` 后透传 upstream `GET /session/status?directory=` → 原 map（**v0.3.0 起不 gate allowlist**）。
- **单 sid**：`GET /slimapi/sessions/{sid}/status` → discover `/session/{sid}`（B1 三态：404→`session_not_found` / 其它 4xx→502 / 5xx→503）；normalize 不 gate（F2 + v0.3.0 全面去 gate）。
- **须实测**：未落盘 active 会话是否在 status map。

### 1.10 `GET /slimapi/events`（策展 SSE：单全局连接 + digest 合并）

- **参数**：无（移除 `directory`、`sessionId`、`stream`）。**全实例、全目录**——客户端在本地按需过滤。
- **upstream**：sidecar 持**一条**进程级 `/global/event` 订阅（opencode GlobalBus，无 directory 过滤）。重连指数退避（1→30s）。
- **上游帧形状**：`{directory, project?, workspace?, payload:{id?, type, properties}}`——hub 在 `publish()` 解包。
- **吐出帧（仅以下）**：
  1. **`event: session.digest`**——debounce 250ms/session，每 session 一帧；窗口内有变化的 session 才发，字段按变化出现：
     ```
     event: session.digest
      data: {"sessionID":"...","directory":"/path","status?":"busy","messageID?":"msg_..","updatedAt?":<epoch_ms>,"archived?":<epoch_ms>,"deleted?":true,"lastError?":{"name":"...","message":"...","at":<epoch_ms>}|null}
     ```
     - `status` ← `session.status`(idle/busy) 的 properties.status
     - `messageID` ← `message.updated`/`message.appended` 的 `info.id`（取最新）；`updatedAt` ← `info.time.updated`/`created`/事件到达时间 epoch_ms
     - `deleted=true` ← `session.deleted`（一旦为 true 持续到窗口结束）
     - `archived` ← `session.updated` 的 `info.time.archived`（epoch_ms int，一旦有值粘滞保留到窗口结束）
     - `directory` ← GlobalEvent 的 directory
     - **`lastError`（G1-A）**←有 sid 的 `session.error` 经脱敏后的 `{name,message,at}`（`at`=sidecar 收到 epoch-ms）。**三态 wire**（与 sticky 共存，互不矛盾；权威见 `docs/specs/v1-contract.md` §3）：
       - **对象** `{name,message,at}`：本窗口新 error，或 flush 时该 sid 仍有 sticky（其它字段触发的后续 digest 会继续带出对象，直至 clear/deleted）；error 到达时**立即 flush**（不等 250ms）
       - **显式 `null`**：clear 帧——该 session 出现新 `status=busy` 时 pop sticky 并立即 flush
       - **省略**：本 digest 没有本窗口新 error 对象、也没有显式 clear（`null`），**且** 该 sid 当前不存在 sticky error；`deleted=true` 的 digest **强制省略**（pop sticky，**不**发 null）
       - abort（`error.name=="MessageAbortedError"`）静默丢弃（不写 lastError、不发 G1-B 帧）
     - `session.updated` 创建 pending 项；若 `info.time.archived` 有值则设 `archived`（见上）；无其它字段变化时 emit `{sessionID,directory}` 让客户端 refetch `/sessions`
     - 同 session 多次变化 → 合并取最新；窗口 flush 后清 pending（lastError sticky 经独立持久层跨窗口保留，见 `docs/specs/v1-impl-spec.md` §7）。
  2. **`event: session.error`（G1-B）**——**无** `sessionID` 时**立即直推**（不进 debounce）：`data: {"directory"?,"name","message","at"}`。有 sid 的 `session.error` **不**走本帧，走 digest `lastError`（G1-A）。abort（`MessageAbortedError`）静默丢弃。实现细节见 `docs/specs/v1-impl-spec.md` §7；wire 权威见 `docs/specs/v1-contract.md` §3。
  3. **`question.*` / `permission.*`**——**立即推**（不进 debounce）：`question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.asked`/`permission.v2.resolved`，原样 `{"directory":..,"type":..,"properties":..}`。
  4. `event: server.connected`（订阅即吐首帧）+ `event: server.heartbeat`（10s）+ `event: resync` `{"reason":"reconnect_no_replay"}`（上游重连或客户端带 `Last-Event-ID` 时）。
- **全部丢弃**：`message.part.delta`/`.updated`/`.removed`（逐 token）、`tool.*`、`message.removed`、未知类型——省流核心。上游 `session.error` **不**在此列：经 G1 处理（有 sid→digest `lastError`；无 sid→`event: session.error`；abort 过滤），见上吐出帧 + `docs/specs/v1-contract.md` §3 / `docs/specs/v1-impl-spec.md` §7。
- **背压**：每订阅 `asyncio.Queue`（item 上限 + 字节预算）；溢出时 **立即断开慢消费者**：标记 `closed` → **清空全部旧 queue 帧** → 入队 `event: resync` `{"reason":"subscriber_backpressure"}` → 入队 `STOP`（**不**交付此前积压帧，**不**「丢最旧续发」）。
- **不承诺 replay**：无 event store；重连接收 `resync` 后走冷启动流程（sessions + questions + permissions + `/since/{ts}`）或前台 catch-up。
- **生命周期**：首 subscriber 到达→启动 upstream 任务；末 subscriber 离开后 30s grace 再取消任务；`HubRegistry.close()` 取消所有任务。
- **HubRegistry 接口**：`HubRegistry(client)` + `await close()`；内部维护单一 `_global: GlobalHub`。`get(directory)` 保留兼容签名但忽略 directory；新增 `get_global()`。

### 1.11 `GET /slimapi/health` + `GET /slimapi/ready`
- `/health`：liveness，进程可服务→200。
- `/ready`：探 `GET /global/health`，upstream 不通→503 `{"upstream":{"ok":false}}`。
- 两者均暴露 `server.api_version`；health 另暴露 `server.accepted_client_versions`，客户端据此自检。
- **启动字段漂移 smoke**：拉一个已知 sid 的 `limit=1` message 校验 `info.id`/`parts[].type` 存在；失败→degraded，`/messages` 自动降级 `mode=full` 透传。

### 1.12 透传反代（catch-all）
- 非 `/slimapi/**` → `http://127.0.0.1:4096/{path}`；method/query/body 流式透传。
- 剥 hop-by-hop（`Connection/Keep-Alive/TE/Trailer/Transfer-Encoding/Upgrade/Proxy-*/Host`）。
- **超时**：command ≥300s（客户端 commandApi 读超时）；SSE 无限 read + 禁缓冲 + `aiter_raw()` 保 `Content-Encoding`。
- **WebSocket**→501（不处理；PTY 需另上 nginx/Caddy）。
- **SSRF 防护**：upstream 固定 loopback，禁参数控制；禁 body 日志。

### 1.13 `GET /slimapi/messages/{sid}/full?ids=`（批量展开，G6）
- **参数**：`sid`；`ids`(query, 必填, 逗号分隔 messageId 1–20, 去重保序)；`mode`(skeleton|full 默认 full)；`directory`(soft)。
- **discover 先行**：`GET /session/{sid}`（带 directory 头）；404→404 `session_not_found`（不拉 mid）；其它 4xx→502；5xx→503。
- **并发 ≤4** 拉 N mid；mid 404→`errors[]` `message_not_found`；mid >`max_message_bytes`→`errors[]` `message_too_large`；累计 >`max_response_bytes`→413 `response_too_large` 中止；全 mid 404 仍 200+全 errors。
- **items[]** 严格按 `ids` 去重后序；`Cache-Control:no-store`。

---

## 2. 骨架字段规则（mode=skeleton）

`MessageWithParts` 的 `info` 完整保留（含 `tokens/cost`）。parts：

| part.type | 处理 | 留 | 删 |
|---|---|---|---|
| `text` | 全留 | 全部 | — |
| `reasoning` | **留 `text`** | `id,type,messageID,sessionID,text` | （不删 text，否则触发消息过滤） |
| `tool` | 瘦 `state.input`+留元数据 | `id,type,tool,callID,messageID,sessionID`；`state{status,title,time}`；**`state.input` 白名单键**(path/filePath/file_path/command/agent/description/subagent_type/todos)；`state.metadata{sessionId,sessionID,description,agent}` | `state.output/structured/result/raw/attachments`、`state.input.{newString,oldString,content,patchText}`、`metadata.diagnostics` |
| `patch` | 留 files+路径 | `files[{path,additions,deletions,status}]`、`metadata.path`、瘦 `state.input.path` | `state.output` |
| `file` | 按 url 类型 | `filename,mime`；`url`：`http(s)`短则留、`data:`则 null + `hasFull` | `source`(base64) |
| `step-start` | ids | `id,type,messageID,sessionID` | snapshot |
| `step-finish` | ids | `id,type,messageID,sessionID`（finish 可留 reason/cost/tokens） | snapshot |
| `compaction` | 全留（设单 part 上限） | 全部 | — |
| 未知 | ids+标记 | `id,type,messageID,sessionID` | 大字段（受总响应上限保护） |

**所有被裁剪的 part 加标记**（需客户端扩模型，见 §3）：
```json
{"hasFull":true,"omitted":["state.output","state.input.newString"]}
```
若裁剪后某 message 无可渲染 part，sidecar 注入 1 个 text 占位 part（`"[内容已折叠，点开查看]"`），**禁返回空 parts 数组**（否则 `isEffectivelyRenderableEmpty` 过滤整条）。

---

## 3. 客户端改动清单

1. **`Part` 扩字段**：`hasFull:Boolean?=null`、`omitted:List<String>?=null`（`ignoreUnknownKeys=true` 当前会丢弃这些标记 → 无展开 affordance）。
2. **`QuestionRequest`/permission 加 `directory:String?=null`**；questions/permissions 返回类型改 `{items,errors}` + 每 item 带 `directory/routeToken`。
3. **展开 hook**：`hasFull && omitted` 的 part，首次展开→`GET /slimapi/messages/{sid}/full/{mid}`（默认 mode=full）→按 `messageId+partId` 替换；loading/失败内联状态。
4. **`SSEClient.kt`**：连接单一 `/slimapi/events`（**无 query 参数**，v2 全实例聚合）；curated 帧解析（`session.digest` / `session.error` / `question.*` / `permission.*` / `heartbeat` / `resync`）。
5. **GET 侧 circuit breaker**：连续 3 次 sidecar transport/5xx→禁用 thin 5min→half-open 探测；**mutation 不双发**（POST 可能已被 upstream 接收）。
6. **增量 reducer**：处理 `session.digest`（debounced 时间戳锚点拉取 `/since/{ts}`）、`event:resync`（前台 catch-up）、`event:session.error`（G1，UI banner/toast）。

---

## 4. 部署

- **双 stunnel 入口**（都 mTLS + 同 CA/客户端证书）：
  - `14096 → 127.0.0.1:4096`（直连 opencode，回退用）
  - `14097 → 127.0.0.1:4097`（经 sidecar）
- **sidecar** 默认 `127.0.0.1:4097`；可选 `0.0.0.0:4097` 作为明文直连入口（Tailscale 直达，依赖 Tailscale ACL / 防火墙；非 routable 主机仍启动 assert 拒绝）。upstream 始终固定 loopback HTTP；systemd user unit，`Restart=on-failure`，`MemoryMax=384M`，`LoadCredential=route-secret:`。
- 框架：**FastAPI+httpx+orjson+uvicorn**（typed 校验降低契约错误）；单 worker（共享 SSE hub）。
- SSE 长连接：stunnel `TIMEOUTidle=0 或 43200` + `TCP_NODELAY`+`SO_KEEPALIVE`。

---

## 5. gzip 实测（上线门禁）

REST 字节是原始 JSON（opencode ≥1KB 自动 gzip，OkHttp 自动解压）；**SSE 不 gzip**（真实 wire）。**sidecar 必须自 gzip 响应 + `Vary: Accept-Encoding`**，否则手机拿原始 40KB。

测法：`curl --compressed -o /dev/null -w '%{size_download}'`（=下载字节）vs `Accept-Encoding: identity`（=原始）；确认 `Content-Encoding: gzip`；SSE 确认无 `Content-Encoding`。必测：messages(limit=1/10/40, skeleton/full)、sessions、single message、questions/permissions、project、status、`/event`/`/global/event`。Android 端用 `TrafficCountingInterceptor` 校准（含 TLS/header）。
**gzip 后 full vs skeleton wire 差从 11× 降到 ~5–8×，仍显著**；收益表须 raw/gzip 双口径。

---

## 6. 实测基准与骨架体积现实

骨架体积取决于会话内容（reasoning-heavy vs text/tool-heavy）；保留 `reasoning.text` 全文是 UI 安全权衡（删了会触发 `isEffectivelyRenderableEmpty` 过滤整条纯思考消息），故 raw 骨架不可能到理论下限。

对 golden fixture `msg40.json`（reasoning 偏重的真实会话）实测：

| 口径 | 字节 | 占原始 |
|---|---:|---:|
| 原始 `/session/:id/message?limit=40` | 443,179 | 100% |
| 其中 text 裸字符串 | 35,938 | 8.1% |
| 其中 reasoning.text 裸字符串 | 117,861 | 26.6% |
| **skeleton（raw JSON）** | **218,240** | **49.2%** |
| **skeleton（gzip wire）** | **66,672** | **15.0%（相对原始 raw）** |

含义：
- **raw 骨架 ~50%**（reasoning-heavy 会话）；text/tool-heavy 会话更低（~15–25%）。
- **sidecar 自 gzip 后，手机实际收 ~67KB（相对原始 443KB raw 省 85%，相对原始 gzip 约 5–8× 收益）**。
- **不要再立"raw skeleton <15%"的验收线**——它与"保留 reasoning.text"互斥。验收改为：`raw < 55%` + `gzip wire 显著低于 full gzip` + 字段契约断言（见 `tests/test_skeleton.py`）。
- 若日后要把 raw 也压到 <15%，**唯一现实做法**是允许 reasoning 按需展开（骨架里 reasoning→占位 + `hasFull`，点开再 `mode=full` 拉全文）——但这要求客户端扩 `Part` 模型 + 展开 hook 先到位，且须给纯 reasoning 消息注入非空占位 part（否则触发过滤）。属可选后续优化。

---

## 7. 版本契约

1. thin API 路径固定为 `/slimapi/**`，不把版本嵌入 URL。
2. 每个 thin REST 与 SSE 请求必须携带 `X-Slimapi-Version: <int>`；缺头不给默认。
3. 版本是单调递增整数。仅破坏性 wire/API 变更 bump；加性字段/端点变更保持同版本兼容。
4. 服务端当前 `SERVER_API_VERSION=1`；`ACCEPTED_CLIENT_VERSIONS=(1,1)` 为闭区间，可由 `OC_SLIMAPI_SERVER_API_VERSION` 与 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=min,max` 配置。
5. 缺头或非整数：`400 {"code":"version_required","accepted":[min,max]}`；区间外：`400 {"code":"version_incompatible","client":v,"accepted":[min,max]}`。
6. `/slimapi/health` 返回 `server:{api_version,accepted_client_versions}`；`/slimapi/ready` 至少返回 `server.api_version`。客户端连接时读取 health 做双向兼容性自检，但 health 本身也必须带版本头。
7. `/slimapi/events` 受同一版本门禁约束；OkHttp SSE 设置 `X-Slimapi-Version`。浏览器 EventSource 若未来需要，可另加 `?version=` query 兜底；本版本不实现。
8. 非 `/slimapi/**` 的 catch-all 反代完全绕过版本门禁，例如无头访问 `/global/health` 仍转发 opencode。
