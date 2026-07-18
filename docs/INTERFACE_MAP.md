# oc-slimapi 权威接口映射

> 本文按当前实现生成，代码基线为 `src/oc_slimapi/`。它描述“现在实际做什么”，不把设计文档中的未实现项写成既有行为。

## 0. 全局约束

- sidecar 监听 `127.0.0.1:4097`；应用层没有 Basic/Bearer 鉴权。公网入站鉴权由 sidecar 前的 stunnel mTLS 完成。
- upstream 由 `config.py` 固定为 loopback HTTP，默认 `http://127.0.0.1:4096`，请求参数不能改 upstream，禁止 SQLite。
- `app.py` 先按 `health → sessions → messages → questions → events` 注册 `/slimapi/**`，最后调用 `install_proxy(app)` 注册 catch-all，故 thin route 不会被反代吞掉。
- **版本门闩**：每个 `/slimapi/**` REST/SSE 请求必须带 `X-Slimapi-Version:<int>`；当前接受闭区间 `[1,1]`。缺头或非整数→`400 {"code":"version_required","accepted":[1,1]}`；越界→`400 {"code":"version_incompatible","client":v,"accepted":[1,1]}`。非 slim catch-all 不检查版本头。
- `json_response()` honor `Accept-Encoding: gzip`，返回 `Content-Encoding:gzip` 和 `Vary:Accept-Encoding`；SSE、full 流式响应和多数上游错误透传不调用该 helper。
- `require_directory()` 将目录去尾斜杠后与 `/project`、`/project/{id}/directories` 构建的内存 allowlist 精确匹配；allowlist miss 时刷新一次。
- FastAPI 参数/body 校验失败的实际状态是 **422**；业务校验主动抛出的错误通常是 400/502/503/504。

## 1. REST 读接口

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/sessions`**<br>无应用层鉴权；可带 `Accept-Encoding`。参数：`directory:str?`、`roots:bool=false`、`limit:int=100`（1–1000）、`start:int?`（≥0）、`search:str?`；无 body。 | `GET http://127.0.0.1:4096/session`；query 始终有 `limit`、`roots`，其余非空才传。directory 存在时同时传 `?directory=` 和 `X-Opencode-Directory`。 | `Session[]` 裸数组；通常 200；上游也可能 4xx/5xx。 | directory 经 `require_directory()`；每项调用 `skeleton_session()`，只留 session 基础字段、`time{created,updated,archived}`、`summary{additions,deletions,files}`、`revert{messageID,partID}`。 | 200：`Session[]` 裸数组；支持 gzip，`Vary:Accept-Encoding`。上游非 2xx：尝试把上游 JSON 和状态码返回。业务 allowlist miss 400；allowlist 刷新失败 503；FastAPI 参数错误 422。 | 代码不接收 cursor；`limit=0` 在 sidecar 被校验拒绝，避免 legacy 把 0 解释为全量。上游错误分支假定 body 可解 JSON。
| **GET `/slimapi/projects`**<br>无参数、无 body；可带 `Accept-Encoding`。 | 先 `GET http://127.0.0.1:4096/project`；再对每个项目以 semaphore=8 并发 `GET /project/{url-encoded-id}/directories`。 | `/project` 返回 project 数组；directories 返回数组，元素含 `directory`/`path`、`strategy`；典型 200。 | `load_projects()` 转成 `{id,name,worktree,directories:[{path,strategy}]}`，更新进程内 directory allowlist。 | 200：project 裸数组；gzip + `Vary`。任一发现步骤失败统一 502。 | 每次调用会 fan-out；没有 TTL/cache 锁。allowlist 还会在目录首次使用时按需刷新。
| **GET `/slimapi/messages/{sid}`**<br>`limit:int=40`（1–200，0 被拒）、`before:str?` opaque、`mode:skeleton|full=skeleton`、`directory:str?`；无 body。 | `GET http://127.0.0.1:4096/session/{sid}/message?limit=&before=`；directory 作为 `X-Opencode-Directory`。 | `MessageWithParts[]` 裸数组；分页可能带 `Link: <...?before=CURSOR>; rel="next"`；典型 200/400/404。 | schema smoke degraded 时强制 full。skeleton：缓冲 body，64 MiB 上限，经转换 semaphore 后 `orjson.loads` + `skeleton_messages()`，**解析上游 `Link` 头中的 `?before=` cursor** → 下发 `X-Next-Cursor`（opaque base64url 字符串，不 decode/re-encode，**不再把 upstream `Link` 头原样复制给客户端**）；full：`aiter_raw()` 流式透传（含 upstream `Link` 头原样）。skeleton 规则见 §5。 | skeleton 200：裸数组、`Cache-Control:no-store`、下发 `X-Next-Cursor`（仅当上游给 `Link`），gzip+`Vary`。full：上游状态/body/编码流式透传并补 `Cache-Control:no-store`。skeleton body>64 MiB→413 `response_too_large`；转换槽立即拿不到→503 `transform_busy`；参数错误 422；上游错误原状态透传。 | `before` 不解析、不重建。skeleton 模式 sidecar 不再把 upstream 的 `Link` 头原样复制给客户端，而是从中抽取 cursor 改下发 `X-Next-Cursor`；full 模式仍原样透传 `Link`。full 不受 Python JSON body 上限，但客户端仍应有自己的响应上限。`directory` 没做 allowlist 校验，且只发 header。
| **GET `/slimapi/messages/{sid}/since/{ts}`**<br>`ts:int`（path，epoch ms，客户端本地该 ses 最大 `updatedAt`）；`limit:int=50`（1–200）、`before:str?` opaque（来自上一响应 `X-Next-Cursor`，原样透传）、`mode:skeleton|full=skeleton`、`directory:str?`；无 body。 | 在**单个 transform admission + 单个累计字节预算**（`MAX_RESPONSE_BYTES`）下翻最多 `max_since_pages` 页（不暴露）：每页 `GET http://127.0.0.1:4096/session/{sid}/message?limit=&before={opaque-cursor}`；cursor 来自上一页响应的 `Link` 头；`?before=` 原样转发客户端回传的 `X-Next-Cursor`；directory 作为 header。 | 每页 `MessageWithParts[]`，可能带 `Link: <...?before=CURSOR>; rel="next"`；典型 200/4xx。 | 过滤条件 **`info.time.updated >= ts`**（含边界；客户端按 messageID 去重边界）；skeleton 时调 `skeleton_messages()`；累计字节在单 admission 下统一计数。透传 opencode 响应 `Link` 头里的 **opaque base64url cursor** 作为 `X-Next-Cursor`（原样字符串，不 decode/re-encode）——仅在"填满 limit 且未撞 ts 地板且 opencode 给了 Link"时下发。**ts 地板**：扫描中遇到 `time.updated < ts` 的项 → 停（后续都更旧），抑制 `X-Next-Cursor`。 | 200：骨架裸数组、`Cache-Control:no-store`、`X-Next-Cursor`（仅当有续且未撞地板），gzip+`Vary`。累计字节超 `MAX_RESPONSE_BYTES`→413 `response_too_large`；转换槽立即拿不到→503 `transform_busy`；参数错误 422；上游错误原状态/body 透传。 | **v1 语义：A2=A 时间戳锚点**（v0 基于 message-id 回溯的增量协议整体废弃；分页参数统一为 `limit`）。full 模式在该接口仍缓冲/解析页面，不是流式。
| **GET `/slimapi/messages/{sid}/full/{mid}`**<br>`mode:skeleton|full=full`、`directory:str?`；无 body。 | `GET http://127.0.0.1:4096/session/{sid}/message/{mid}`；directory 作为 header。 | 单个 `MessageWithParts`；典型 200/404。 | 先缓冲完整上游 body；超过 32 MiB 立即 413。schema degraded 强制 full；skeleton 调 `skeleton_message()`，且共享转换 semaphore。 | full：上游对象/body/status，`Cache-Control:no-store`；skeleton 200：单对象、`Cache-Control:no-store`、gzip+`Vary`。>32 MiB→413 `message_too_large`；转换忙→502；上游 400/404/5xx 原状态/body 透传；参数错误 422。 | 路径段 `full` 仅作"展开全文"语义占位，并非 `mode=full` 的默认——`mode` 仍由 query 控制，默认 `full`。名为 full 但当前实现已由 httpx `.get()` 缓冲，不是流式；32 MiB 检查发生在下载完成后。客户端应按 messageId+partId 替换。
| **GET `/slimapi/sessions/status`**<br>`directory:str` 必填；无 body；可带 `Accept-Encoding`。 | `GET http://127.0.0.1:4096/session/status?directory=...` + `X-Opencode-Directory`。 | `Map<sid,Status>`；典型 200，空时 `{}`。 | `require_directory()` 后不裁剪 status map。 | 上游状态码 + map；gzip+`Vary`。directory 非 allowlist 400，刷新失败 503，参数缺失 422。 | 只在该 directory 查询成功时，空 map 才有 idle 语义；该批量接口自身不补 idle 项。
| **GET `/slimapi/sessions/{sid}/status`**<br>仅 `sid`；无 body。 | 先 `GET http://127.0.0.1:4096/session/{sid}` 取 directory；校验 allowlist；再 `GET /session/status?directory=` + `X-Opencode-Directory`。 | session 单对象，随后为 `Map<sid,Status>`；典型 200/404。 | 精确反查该 sid 的 directory；status map 有 sid 则返回其值，成功但缺 sid 才合成 `{"type":"idle"}`。 | 200：单 Status 对象或 idle 对象。session/directory 发现任意失败→503；status 请求/解析失败→503。 | 当前实现把 upstream session 404 也映射为 503，不透传 404；响应调用 `json_response()` 但未传入 Accept-Encoding，因此实际不 gzip，仍带 `Vary`。

## 2. Pending 聚合与写接口

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/questions`** / **GET `/slimapi/permissions`**<br>`directory:list[str]` 用 repeated query，必填；去重后 1–32；无 body；可带 `Accept-Encoding`。 | 每个目录并发 `GET http://127.0.0.1:4096/question?directory=...` 或 `/permission?directory=...`，同时发 `X-Opencode-Directory`，单请求 timeout=2s。 | 每目录返回原 question/permission 对象数组；典型 200，亦可能 timeout/4xx/5xx。 | 每目录先 `require_directory()`；`_aggregate()` fan-out。每个含 `id` 或 `requestID` 的 item 原字段保留并加 `directory`、`routeToken`。`issue_route_token()` 生成 HMAC-SHA256 token，payload 为 `{v,kind,requestID,sessionID,directory,iat,exp}`，exp=1h。 | 至少一个目录成功：200 `{"items":[...],"errors":[...]}`；全部失败：503 同 envelope。错误 code 为 `upstream_timeout`、`upstream_error` 或 `upstream_http_N`。gzip+`Vary`。非法目录 400；参数缺失/类型错误 422。 | repeated 参数名是 `directory`，不接受逗号串。没有 pending cache。无 id/requestID 的上游 item 被静默跳过。
| **POST `/slimapi/questions/{qid}/reply`**<br>body `{"answers":[[str,...],...],"routeToken":str}`；无额外 HTTP auth。 | token 还原 directory 后：`POST http://127.0.0.1:4096/question/{qid}/reply?directory=...` + `X-Opencode-Directory`；上游 body 仅 `{"answers":...}`，routeToken 已剥离；timeout 30s。 | 2xx 或错误 body；典型 200/204、400、404。 | `_token()` 调 `verify_route_token()` 校验签名、版本、iat/exp、kind=`question`、requestID=qid，并要求 token directory 仍在当前 allowlist。`_post()` 不重试 mutation。 | 上游任意 2xx→204 空 body；上游 400/404 原状态/body 透传；其他≥300原样透传；timeout→504；token 错/过期/目录失效→400；body 校验失败→422。 | routeToken 必填；sidecar 不接受 body directory，也不猜 process cwd。超时后不得由客户端自动双发。
| **POST `/slimapi/questions/{qid}/reject`**<br>body `{"routeToken":str}`。 | `POST http://127.0.0.1:4096/question/{qid}/reject?directory=...` + directory header；上游 body 实际为 `{}`。 | 同 reply。 | 与 reply 相同的 question token 校验；routeToken 剥离。 | 2xx→204；400/404透传；其他错误透传；timeout 504；token业务错误400；schema错误422。 | 实现确实向 reject upstream 发送空 JSON `{}`，不是无 body。
| **POST `/slimapi/sessions/{sid}/permissions/{pid}`**<br>body `{"response":"once"|"always"|"reject","routeToken":str}`。 | `POST http://127.0.0.1:4096/session/{sid}/permissions/{pid}?directory=...` + directory header；body 仅 `{"response":...}`。 | 2xx/400/404/其他错误。 | `verify_route_token()` 强制 kind=`permission`、requestID=pid、sessionID=sid、有效期/签名/目录 allowlist；routeToken 剥离。 | 2xx→204；400/404透传；其他≥300透传；timeout→504；token错误400；body枚举错误422。 | **按当前实现 routeToken 强制必填**；并未仅靠 sid 反查 directory。`always` 后没有额外 permission cache 可失效。

## 3. Curated SSE

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/events`**<br>无 query/body 参数；可带 `Last-Event-ID`。**v2 重写**：全实例、全目录、合并 digest；不再按 directory/sessionId 过滤。 | `HubRegistry` 持**一条**进程级 `GET http://127.0.0.1:4096/global/event` + `Accept:text/event-stream`；connect=5s/read无限。无 directory header、无 `?directory=`（opencode GlobalBus 全实例）。 | 上游 SSE data JSON = `{directory:str, project?, workspace?, payload:{id?, type, properties}}`（注意比旧 `/event` 多一层 `{directory, payload}` 包装）。事件类型：`session.status`(idle/busy)、`session.updated`、`session.deleted`；`message.updated`/`message.appended`（带 messageID）；`question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.asked`/`permission.v2.resolved`；`message.part.*`/`tool.*` 等其它事件**丢弃**。 | `HubRegistry.get_global()` 返回单一 `GlobalHub`（`get(directory)` 兼容签名但忽略 directory）。`subscribe()` 立即在 subscriber queue 放 `server.connected` 首帧，再启动 run/flush_loop(250ms)/heartbeat_loop(10s) 任务。`publish()`：question/permission 立即扇出原帧（不进 debounce）；`session.*`/`message.updated`/`message.appended` 累积进 `pending: dict[sessionID, DigestFields]`，字段按变化合并（status/messageID/updatedAt 各取最新、archived 一旦有值粘滞保留时间戳、deleted 一旦 true 持续）；其余类型静默丢弃。`flush()` 每 250ms 遍历 pending，每 session 吐一帧 `event: session.digest` `{"sessionID","directory","status"?,"messageID"?,"updatedAt"?(epoch_ms),"archived"?(epoch_ms),"deleted"?}`，清 pending。`heartbeat_loop` 每 10s 吐 `event: server.heartbeat` `{}`。`run()` 上游断开指数退避（1→30s）；重连后调用 `resync_all()` 向所有 subscriber 吐 `event: resync` `{"reason":"reconnect_no_replay"}`。每订阅 `asyncio.Queue(maxsize=256)`，满则丢最旧 + `STOP` 断慢消费者。末 subscriber 离开后 30s grace 再取消任务。 | 200 `text/event-stream`；头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`；**不 gzip**。客户端带 Last-Event-ID 时首帧为 `event: resync`；正常连接首帧为 `event: server.connected`。`session.digest` 字段按窗口内变化出现（未变化的字段不在帧里）。**无 replay 承诺**——客户端收到 resync 后走 latest-id/catch-up。 | 不保存事件、不承诺 replay。`directory`/`sessionId`/`stream` 参数完全移除（v1→v2 演化，无 bump：客户端按新帧类型解析；旧 A 桶客户端未对接，无破坏）。`HubRegistry(client)` + `close()` 签名保持不变（app.py 零改动）。

## 4. Health 与透明反代

| sidecar 入口（`/slimapi/**` 项须带版本头；catch-all 不需要） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/health`**<br>须带 `X-Slimapi-Version:int`；无参数/body；可带 `Accept-Encoding`。 | 无 upstream 请求。 | 不适用。 | 读取进程版本、服务端 API 版本/接受区间及启动 smoke 设置的 `schema_degraded`。 | 200 `{"sidecar":{"ok":true,"version":"0.1.0"},"server":{"api_version":1,"accepted_client_versions":[1,1]},"schema":{"degraded":bool}}`；gzip + `Vary`。缺/坏/越界版本头→门闩400。 | 这是 liveness，不代表 opencode 可达；health 自身也受门闩保护，客户端以其 server 字段做后续自检。
| **GET `/slimapi/ready`**<br>须带 `X-Slimapi-Version:int`；无参数/body。 | `GET http://127.0.0.1:4096/global/health`，timeout 5s。 | health JSON；典型 200，或连接/timeout/5xx。 | 仅以 upstream status<300 判 ready，测 latency；不转发上游 health body；附 API 版本与接受区间。 | upstream<300→200；否则/异常→503。body `{"upstream":{"ok":bool,"latencyMs":n},"server":{"api_version":1,"accepted_client_versions":[1,1]},"schema":{"degraded":bool}}`。调用 `json_response()` 但未传 Accept-Encoding，因此实际 identity body + `Vary`；版本头错误优先400。 | schema degraded 不改变 ready 状态；它只让消息 skeleton 路由自动降级 full。
| **HTTP catch-all `/{path:path}`**<br>方法 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS；原 query/header/body。`/slimapi/**` 未命中路径被显式挡住。 | `METHOD http://127.0.0.1:4096/{path}`；重复 query 保留；body 用 `request.stream()`；剥 hop-by-hop/Connection token。`/event`、`/global/event` read timeout=None；以 `/command` 结尾 read=300s；其他 read=30s；write=300s。 | 任意 opencode HTTP 响应；SSE 为流；普通接口 JSON/文件等。 | upstream 固定 loopback，不能由请求控制；`client.send(...,stream=True)` + `aiter_raw()`，剥响应 hop-by-hop，完成后关闭 upstream response。 | 非 slim 路径：上游状态、raw body、Content-Encoding 和非 hop-by-hop headers 流式返回，不额外 gzip。未知 `/slimapi/**`→sidecar 404 `{"code":"thin_route_not_found"}`，不会透传。 | catch-all 必须最后注册，当前 `app.py` 已保证。原始 `/event`/`/global/event` 不缓冲。异常没有统一映射，可能成为 500。
| **WebSocket catch-all `/{path:path}`**<br>任意 WS path。 | 不连接 upstream。 | 不适用。 | 接受 WebSocket，发送 `{"code":"websocket_not_supported","status":501}`，随后以 code 1011 关闭。 | WebSocket 消息内表达 501；不是 HTTP 501 response。 | PTY/WS 需另上 nginx/Caddy；当前行为与“HTTP WS→501”字面契约并不完全相同。

## 5. Skeleton 精确规则

消息由 `skeleton_messages()` / `skeleton_message()` 转换；`info` 深拷贝完整保留，parts 由 `skeleton_part()` 分派：

| part.type | 当前处理 |
|---|---|
| `text` | 整 part 深拷贝，全留。 |
| `reasoning` | 留 `id,type,messageID,sessionID,text`；删除其他字段时用 `_mark()` 加 `hasFull:true` 和排序去重后的 `omitted`。 |
| `tool` | `_tool()` 留 ids/tool/callID；state 留 status/title/time；input 仅 `path,filePath,file_path,command,agent,description,subagent_type,todos`；metadata 仅 `sessionId,sessionID,description,agent`；删 output/structured/result/raw/attachments/error 及其他键并标记。 |
| `patch` | `_patch()` 留 ids、files 的 path/additions/deletions/status、metadata.path、state status/title/time 及 input 路径键；裁掉 output/其他输入并标记。 |
| `file` | `_file()` 留 ids/filename/mime；≤8KiB 的 `http(s)` URL 保留；`data:`、过长或其他 URL 置 null 并标记；source 删除。 |
| `step-start` / `step-finish` | 只留 ids；若删除字段则标记。 |
| `compaction` | orjson 编码≤64KiB 时整 part 保留；超限降为 ids + `hasFull/omitted:["*"]`。 |
| 未知 | 只留 ids；`hasFull:true`，omitted 为实际删除键，若无其他键则为 `["*"]`。 |

转换后若 `_is_renderable()` 判定所有 part 均不可渲染，会**追加**一个 text 占位：`[内容已折叠，点开查看]`，ID 为 `thin_placeholder_{messageID}`，并带 `hasFull:true, omitted:["parts"]`；原 part 不会被丢弃。

## 6. RouteToken 精确规则

- `issue_route_token()`：规范 JSON payload 使用 `orjson.OPT_SORT_KEYS`，签名为 `HMAC-SHA256(secret,payload)`，wire 为 `base64url(payload).base64url(signature)`，base64 padding 删除。
- payload：`v=1`、kind、requestID、sessionID、directory、iat、`exp=iat+3600`。
- `verify_route_token()`：恢复 padding并解码；constant-time 比较签名；验证 v、过期、iat 不超过当前时间60s、kind、requestID、可选 sessionID、directory 类型。
- secret 来自环境或 systemd `LoadCredential`，至少32 bytes，不随机生成；token 因而能跨 sidecar 重启验证。
