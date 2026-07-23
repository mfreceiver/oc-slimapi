# oc-slimapi 权威接口映射

> 本文按当前实现生成，代码基线为 `src/oc_slimapi/`。它描述“现在实际做什么”，不把设计文档中的未实现项写成既有行为。

## 0. 全局约束

- sidecar 监听 host 由 `OC_SLIMAPI_HOST` 决定（默认 `127.0.0.1:4097`，可选 `0.0.0.0:4097` 作为明文直连入口）；应用层没有 Basic/Bearer 鉴权。入站鉴权由 sidecar 前的 stunnel mTLS（`:14097` 推荐）完成；若用 `0.0.0.0` 明文直连，安全由 Tailscale ACL / 防火墙负责。
- upstream 由 `config.py` 固定为 loopback HTTP，默认 `http://127.0.0.1:4096`，请求参数不能改 upstream，禁止 SQLite。
- `app.py` 先按 `health → sessions → messages → questions → events` 注册 `/slimapi/**`，最后调用 `install_proxy(app)` 注册 catch-all，故 thin route 不会被反代吞掉。
- **版本门闩**：每个 `/slimapi/**` REST/SSE 请求必须带 `X-Slimapi-Version:<int>`；当前接受闭区间 `[1,1]`。缺头或非整数→`400 {"code":"version_required","accepted":[1,1]}`；越界→`400 {"code":"version_incompatible","client":v,"accepted":[1,1]}`。非 slim catch-all 不检查版本头。
- `json_response()` honor `Accept-Encoding: gzip`，返回 `Content-Encoding:gzip` 和 `Vary:Accept-Encoding`；SSE、full 流式响应和多数上游错误透传不调用该 helper。
- `require_directory()` **已删除**（**v0.3.0** directory allowlist gate 全面移除）。directory 经 `normalize_directory()`（去尾斜杠，根 `/`）后作为 `X-Opencode-Directory` 头 + `?directory=` query 透传给上游 opencode；opencode 决定能否服务。allowlist 数据集仍由 `load_products()` 维护，作 `/slimapi/projects` 展示 + q/p null-directory 聚合 fan-out 用途，**不再 gate**。保留的结构性守卫：显式 repeated `?directory=` 去重保序 + `invalid_directory_count`（1–32）+ messages `/**` 的 query `directory` 与 `X-Opencode-Directory` 头冲突 → 400 `directory_not_allowed`（结构性歧义）。
- FastAPI 参数/body 校验失败的实际状态是 **422**；业务校验主动抛出的错误通常是 400/502/503/504。

## 1. REST 读接口

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/sessions`**<br>无应用层鉴权；可带 `Accept-Encoding`。参数：`directory:str?`、`roots:bool=false`（默认 false，客户端应显式 `roots=true`）、`limit:int=100`（1–1000）、`start:int?`（≥0，**epoch-ms 时间戳水位** `time_updated>=start`，非 offset）、`search:str?`；无 body。 | `GET http://127.0.0.1:4096/session`；query 始终有 `limit`、`roots`，其余非空才传。directory 存在时同时传 `?directory=` 和 `X-Opencode-Directory`。 | `Session[]` 裸数组；通常 200；上游也可能 4xx/5xx。 | directory 经 `normalize_directory()`（**v0.3.0** 不 gate）；每项调用 `skeleton_session()`。**rev F**：`isinstance(payload, list)` 守卫（非 list→503）；200 加三头 `X-Complete`（`len<limit`）、`X-Discovery-Directories`（allowlist 大小）、`X-Discovery-Ready`（`allowlist_ready` last-known-good）。 | 200：`Session[]` 裸数组 + 三头；gzip + `Vary`。上游 4xx→502 `upstream_http_N`；5xx/网络/坏 JSON/非 list→503 `upstream_unavailable`（**不**发三头）。FastAPI 参数错误 422。 | 无 cursor；`limit=0` 被拒。`X-Complete` **不得**当权威全集。`start` 非 offset。**v0.2.1 失败路径 coded**；rev F 三头 + shape 守卫。
| **GET `/slimapi/projects`**<br>无参数、无 body；可带 `Accept-Encoding`。 | 先 `GET http://127.0.0.1:4096/project`；再对每个项目以 semaphore=8 并发 `GET /project/{url-encoded-id}/directories`。 | `/project` 返回 project 数组；directories 返回数组，元素含 `directory`/`path`、`strategy`；典型 200。 | `load_products()`（**rev F**：`app.state.allowlist_lock` 全程串行；顶层 + per-directory 必须 list，非 list→整次失败保留 last-known-good；成功后可能发 `server.reconfigured{discovery_changed}`）转成 `{id,name,worktree,directories:[{path,strategy}]}`，更新 allowlist + `allowlist_ready`。 | 200：project 裸数组；gzip + `Vary`。任一发现步骤失败结构化分裂：upstream 4xx → 502 `upstream_http_N`；网络/5xx/非 list shape → 503 `upstream_unavailable`（body 均为 `{"code":…}`）。 | 每次调用会 fan-out；**rev F** 有进程内锁 + shape 守卫 + discovery 通知。
| **GET `/slimapi/messages/{sid}`**<br>`limit:int=40`（1–200，0 被拒）、`before:str?` opaque、`mode:skeleton|full=skeleton`、`directory:str?`；无 body。 | `GET http://127.0.0.1:4096/session/{sid}/message?limit=&before=`；directory 作为 `X-Opencode-Directory`。 | `MessageWithParts[]` 裸数组；分页可能带 `Link: <...?before=CURSOR>; rel="next"`；典型 200/400/404。 | schema smoke degraded 时强制 full。skeleton：缓冲 body，64 MiB 上限，经转换 semaphore 后 `orjson.loads` + `skeleton_messages()`，**解析上游 `Link` 头中的 `?before=` cursor** → 下发 `X-Next-Cursor`（opaque base64url 字符串，不 decode/re-encode，**不再把 upstream `Link` 头原样复制给客户端**）；full：`aiter_raw()` 流式透传（含 upstream `Link` 头原样）。skeleton 规则见 §5。 | skeleton 200：裸数组、`Cache-Control:no-store`、下发 `X-Next-Cursor`（仅当上游给 `Link`），gzip+`Vary`。full：上游状态/body/编码流式透传并补 `Cache-Control:no-store`。skeleton body>64 MiB→413 `response_too_large`；转换槽立即拿不到→503 `transform_busy`；参数错误 422；上游错误原状态透传。 | `before` 不解析、不重建。skeleton 模式 sidecar 不再把 upstream 的 `Link` 头原样复制给客户端，而是从中抽取 cursor 改下发 `X-Next-Cursor`；full 模式仍原样透传 `Link`。full 不受 Python JSON body 上限，但客户端仍应有自己的响应上限。`directory` 仅作 `X-Opencode-Directory` header 转发上游；G7-soft query allowlist 校验（与 sessions/questions 对齐）见 §7 G7-soft。
| **GET `/slimapi/messages/{sid}/since/{ts}`**<br>`ts:int`（path，epoch ms，客户端本地该 ses 最大 `updatedAt`）；`limit:int=50`（1–200）、`before:str?` opaque（来自上一响应 `X-Next-Cursor`，原样透传）、`mode:skeleton|full=skeleton`、`directory:str?`；无 body。 | 在**单个 transform admission + 单个累计字节预算**（`MAX_RESPONSE_BYTES`）下翻最多 `max_since_pages` 页（不暴露）：每页 `GET http://127.0.0.1:4096/session/{sid}/message?limit=&before={opaque-cursor}`；cursor 来自上一页响应的 `Link` 头；`?before=` 原样转发客户端回传的 `X-Next-Cursor`；directory 作为 header。 | 每页 `MessageWithParts[]`，可能带 `Link: <...?before=CURSOR>; rel="next"`；典型 200/4xx。 | 过滤条件 **`(info.time.updated or info.time.created) >= ts`**（含边界；客户端按 messageID 去重边界；v0.2.1 勘误：opencode v1.18.3 无 message 级 `time.updated`，实读 `created`，与 digest `updatedAt` 同源）；skeleton 时调 `skeleton_messages()`；累计字节在单 admission 下统一计数。透传 opencode 响应 `Link` 头里的 **opaque base64url cursor** 作为 `X-Next-Cursor`（原样字符串，不 decode/re-encode）——仅在"填满 limit 且未撞 ts 地板且 opencode 给了 Link"时下发。**ts 地板**：扫描中遇到 `(time.updated or time.created) < ts` 的项 → 停（后续都更旧），抑制 `X-Next-Cursor`。 | 200：骨架裸数组、`Cache-Control:no-store`、`X-Next-Cursor`（仅当有续且未撞地板），gzip+`Vary`。累计字节超 `MAX_RESPONSE_BYTES`→413 `response_too_large`；转换槽立即拿不到→503 `transform_busy`；参数错误 422；上游错误原状态/body 透传。 | **v1 语义：A2=A 时间戳锚点**（v0 基于 message-id 回溯的增量协议整体废弃；分页参数统一为 `limit`）。full 模式在该接口仍缓冲/解析页面，不是流式。
| **GET `/slimapi/messages/{sid}/full/{mid}`**<br>`mode:skeleton|full=full`、`directory:str?`；无 body。 | `GET http://127.0.0.1:4096/session/{sid}/message/{mid}`；directory 作为 header。 | 单个 `MessageWithParts`；典型 200/404。 | G8 流式读 upstream body（`client.send(stream=True)` + `read_with_cap` + `try/finally: await response.aclose()`）；累计字节超 32 MiB 立即 413。schema degraded 强制 full；skeleton 调 `skeleton_message()`，且共享转换 semaphore。 | full：上游对象/body/status，`Cache-Control:no-store`；skeleton 200：单对象、`Cache-Control:no-store`、gzip+`Vary`。>32 MiB→413 `message_too_large`；转换忙→**503 `transform_busy`**（与 list/since 归一）；上游 400/404/5xx 原状态/body 透传；参数错误 422。 | 路径段 `full` 仅作"展开全文"语义占位，并非 `mode=full` 的默认——`mode` 仍由 query 控制，默认 `full`。名为 full 但 G8 后已改为流式（`client.send(stream=True)` + `read_with_cap` + `try/finally: aclose()`），不再 `httpx.get()` 整 body 缓冲；RSS 不再因单条极大消息打满。客户端应按 messageId+partId 替换。`/full/{mid}` 的 413 code 随 mode 变：full→`message_too_large`(32MiB)；skeleton→`response_too_large`(64MiB)。
| **GET `/slimapi/messages/{sid}/full`**（G6 批量展开）<br>`ids:str` 必填（逗号分隔 mid，1–20，去重保序）；`mode:skeleton|full=full`、`directory:str?`；无 body。 | 先 `GET /session/{sid}` discover；再对每个 mid 并发（sem=4）`GET /session/{sid}/message/{mid}` 流式读。directory 作 header。 | discover：session 对象 200/404；每 mid：`MessageWithParts` 或 404/≥400（含 5xx）/2xx 坏 JSON。 | **discover 先行**（404→`session_not_found` top-level，不进 envelope）；ids 解析：split+strip+`dict.fromkeys` 去重保序；空/超 20→400 `invalid_ids`；ids 缺失→422。共享累计字节 ledger（pay-as-you-read，chunk 16KiB）；mid 级 404→`message_not_found`、per-mid 超 32MiB→`message_too_large`、mid ≥400（**含 5xx**）→`upstream_http_N`、mid 2xx 坏 JSON→`upstream_error` 进 `errors[]`；累计超 `MAX_RESPONSE_BYTES`→整请求 413；任一 mid 网络失败→503（**优先于 413**）；skeleton 转换池饱和→503 `transform_busy`+`Retry-After`。路由注册在 `/full/{mid}` **之前**。 | 200 envelope：`{"items":[...],"errors":[{"messageID","code":"message_not_found|message_too_large|upstream_http_N|upstream_error"}]}`（mid 级部分失败仍 200；全 mid 404 仍 200；**mid 5xx 不升级整请求**）；`items[]`=ids 去重保序，`errors[]`=完成序（不保证）。`Cache-Control:no-store`；gzip+`Vary`。discover 404→404 `session_not_found`；discover 其它 4xx→502；discover 5xx/网络/坏 JSON 或 mid 网络→503 `upstream_unavailable`；ids 缺失→422；空/超 20→400 `invalid_ids`；累计超限→413 `response_too_large`；转换忙（skeleton）→503 `transform_busy`+`Retry-After`。 | 推荐替代 N 并行 `/full/{mid}`。envelope 与 q/p 聚合同形。`mode=skeleton` 时共享 transform pool。单 mid 过大/5xx/坏 JSON 进 errors 不拖垮整批；累计超限整请求失败；**503 优先于 413**。
| **GET `/slimapi/sessions/status`**<br>`directory:str` 必填；无 body；可带 `Accept-Encoding`。 | `GET http://127.0.0.1:4096/session/status?directory=...` + `X-Opencode-Directory`。 | `Map<sid,Status>`；典型 200，空时 `{}`。 | **v0.3.0** `normalize_directory()` 后不裁剪 status map（不 gate allowlist）。 | 上游状态码 + map；gzip+`Vary`。参数缺失 422（directory 必填）。 | 只在该 directory 查询成功时，空 map 才有 idle 语义；该批量接口自身不补 idle 项。
| **GET `/slimapi/sessions/{sid}/status`**<br>仅 `sid`；无 body。 | 先 `GET http://127.0.0.1:4096/session/{sid}` 取 directory；`normalize_directory()` 仅规范化、**不 gate** allowlist；再 `GET /session/status?directory=` + `X-Opencode-Directory`。 | session 单对象，随后为 `Map<sid,Status>`；典型 200/404。 | 精确反查该 sid 的 directory（sid 自洽即能力）；status map 有 sid 则返回其值，成功但缺 sid 才合成 `{"type":"idle"}`。 | 200：单 Status 对象或 idle 对象。详细错误语义（G2 分离）见 §7：upstream 404→404 `session_not_found`；其它 4xx→502 `upstream_http_N`；网络/5xx/JSON 坏→503 `upstream_unavailable`。**不再**因 directory ∉ allowlist 返 400（F2）。 | 响应调用 `json_response()` 并传入 Accept-Encoding；支持 gzip，带 `Vary: Accept-Encoding`。**v0.3.0** 批量 `GET /sessions/status` 也不再 gate（与 per-session 对齐）。

## 2. Pending 聚合与写接口

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/questions`** / **GET `/slimapi/permissions`**<br>`directory:list[str]?` repeated query，**可选**；显式传去重 1–32；null=聚合 allowlist；无 body；可带 `Accept-Encoding`。 | 每个目录并发 `GET http://127.0.0.1:4096/question?directory=...` 或 `/permission?directory=...`，同时发 `X-Opencode-Directory`，单请求 timeout=2s。 | 每目录返回原 question/permission 对象数组；典型 200，亦可能 timeout/4xx/5xx。 | 显式 list：每目录 `normalize_directory()`（**v0.3.0** 不 gate）；null：取 `directory_allowlist`（空则 best-effort `load_products` 再取，可仍为 `[]`），**不受 1–32 上限**（ops 范围由 opencode project list 决定）。`_aggregate()` fan-out。每个含 `id` 或 `requestID` 的 item 原字段保留并加 `directory`、`routeToken`。`issue_route_token()` 生成 HMAC-SHA256 token，payload 为 `{v,kind,requestID,sessionID,directory,iat,exp}`，exp=1h。 | 至少一个目录成功：200 `{"items":[...],"errors":[...],"scope":{"directories":N}}`（N=有效 scope dir 数，v0.2.1 区分 scope 未就绪/权威空）；全部失败：503 `{"items":[...],"errors":[...]}`（**不含** scope）；null 且 allowlist 空→200 `{"items":[],"errors":[],"scope":{"directories":0}}`。错误 code 为 `upstream_timeout`、`upstream_error` 或 `upstream_http_N`。gzip+`Vary`。显式 list 空/超 32→400 `invalid_directory_count`；**v0.3.0** 不再因 directory ∉ allowlist 返 400。 | repeated 参数名是 `directory`，不接受逗号串。没有 pending cache。无 id/requestID 的上游 item 被静默跳过。F1 消除 cold-start 必填 422。
| **POST `/slimapi/questions/{qid}/reply`**<br>body `{"answers":[[str,...],...],"routeToken":str}`；无额外 HTTP auth。 | token 还原 directory 后：`POST http://127.0.0.1:4096/question/{qid}/reply?directory=...` + `X-Opencode-Directory`；上游 body 仅 `{"answers":...}`，routeToken 已剥离；timeout 30s。 | 2xx 或错误 body；典型 200/204、400、404。 | `_token()` 调 `verify_route_token()` 校验签名、版本、iat/exp、kind=`question`、requestID=qid；**v0.3.0** 不再查 allowlist，normalize 后透传。`_post()` 不重试 mutation。 | 上游任意 2xx→204 空 body；上游 400/404 原状态/body 透传；其他≥300原样透传；timeout→504；token 错/过期→400；body 校验失败→422。 | routeToken 必填；sidecar 不接受 body directory，也不猜 process cwd。超时后不得由客户端自动双发。
| **POST `/slimapi/questions/{qid}/reject`**<br>body `{"routeToken":str}`。 | `POST http://127.0.0.1:4096/question/{qid}/reject?directory=...` + directory header；上游 body 实际为 `{}`。 | 同 reply。 | 与 reply 相同的 question token 校验；routeToken 剥离。 | 2xx→204；400/404透传；其他错误透传；timeout 504；token业务错误400；schema错误422。 | 实现确实向 reject upstream 发送空 JSON `{}`，不是无 body。
| **POST `/slimapi/sessions/{sid}/permissions/{pid}`**<br>body `{"response":"once"|"always"|"reject","routeToken":str}`。 | `POST http://127.0.0.1:4096/session/{sid}/permissions/{pid}?directory=...` + directory header；body 仅 `{"response":...}`。 | 2xx/400/404/其他错误。 | `verify_route_token()` 强制 kind=`permission`、requestID=pid、sessionID=sid、有效期/签名；**v0.3.0** 不查 allowlist，normalize 后透传；routeToken 剥离。 | 2xx→204；400/404透传；其他≥300透传；timeout→504；token错误400；body枚举错误422。 | **按当前实现 routeToken 强制必填**；并未仅靠 sid 反查 directory。`always` 后没有额外 permission cache 可失效。

## 3. Curated SSE

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/events`**<br>无 query/body 参数；可带 `Last-Event-ID`。**v2 重写**：全实例、全目录、合并 digest；不再按 directory/sessionId 过滤。 | `HubRegistry` 持**一条**进程级 `GET http://127.0.0.1:4096/global/event` + `Accept:text/event-stream`；connect=5s/read无限。无 directory header、无 `?directory=`（opencode GlobalBus 全实例）。 | 上游 SSE data JSON = `{directory:str, project?, workspace?, payload:{id?, type, properties}}`（注意比旧 `/event` 多一层 `{directory, payload}` 包装）。事件类型：`session.status`(idle/busy)、`session.updated`、`session.deleted`、`session.error`（G1）；`message.updated`/`message.appended`（带 messageID）；`question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.asked`/`permission.v2.resolved`；`message.part.*`/`tool.*` 等其它事件**丢弃**。 | `HubRegistry.get_global()` 返回单一 `GlobalHub`（`get(directory)` 兼容签名但忽略 directory）。`subscribe()` 立即在 subscriber queue 放 `server.connected` 首帧，再启动 run/flush_loop(250ms)/heartbeat_loop(10s) 任务。`publish()`：question/permission 立即扇出原帧（不进 debounce）；`session.*`/`message.updated`/`message.appended` 累积进 `pending: dict[sessionID, DigestFields]`，字段按变化合并（status/messageID/updatedAt 各取最新、archived 一旦有值粘滞保留时间戳、deleted 一旦 true 持续）；G1：`session.error` 有 sid → sticky `lastError` 进 digest 并立即 flush；无 sid → 立即直推 `event: session.error` `{directory?,name,message,at}`；`MessageAbortedError` 静默过滤。其余类型静默丢弃。`flush()` 每 250ms 遍历 pending，每 session 吐一帧 `event: session.digest` `{"sessionID","directory","status"?,"messageID"?,"updatedAt"?(epoch_ms),"archived"?(epoch_ms),"deleted"?,"lastError"?}`，清 pending。`heartbeat_loop` 每 10s 吐 `event: server.heartbeat` `{}`。`run()` 上游断开指数退避（1→30s）；重连后调用 `resync_all()` 向所有 subscriber 吐 `event: resync` `{"reason":"reconnect_no_replay"}`。每订阅 `asyncio.Queue(maxsize=256)` + 字节预算；溢出时 **close → 清空全部旧 queue → 发 `resync{reason:subscriber_backpressure}` → `STOP`** 断慢消费者（**不**丢最旧续灌）。末 subscriber 离开后 30s grace 再取消任务。 | 200 `text/event-stream`；头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`；**不 gzip**。客户端带 Last-Event-ID 时首帧为 `event: resync`；正常连接首帧为 `event: server.connected`。吐出帧：`session.digest`（含 `lastError?`）+ **`session.error`（G1-B，无 sid）** + q/p 直推 + connected/heartbeat/resync。`session.digest` 字段按窗口内变化出现（未变化的字段不在帧里；`lastError` sticky 跨窗口，`status=busy` 显式 `null` 清除）。**无 replay 承诺**——客户端收到 resync 后走 latest-id/catch-up。 | 不保存事件、不承诺 replay。`directory`/`sessionId`/`stream` 参数完全移除（v1→v2 演化，无 bump：客户端按新帧类型解析；旧 A 桶客户端未对接，无破坏）。`HubRegistry(client)` + `close()` 签名保持不变（app.py 零改动）。

### 3.1 Token stream SSE（**Stages A–E 落地，opt-in 实时流**）

> 行为权威：`docs/specs/design-token-stream.md` §5.1（端点）/ §5.4（批式）/ §5.5（握手）/ §5.6（wire 帧，**杠杆1 done:true marker 无 text**）/ §5.8（背压/重连）/ §6（T3 信封，**Option B 拆 4+4**）/ §7（**杠杆2 gzip 首个 SSE 例外**）。wire 契约：`docs/specs/v1-contract.md` §3.x + §6.x。下表为已落地行为。

| sidecar 入口（须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/sessions/{sid}/stream`**<br>`directory:str?`（optional query）；可带 `Last-Event-ID`（值忽略，仅触发首帧 resync）。**opt-in**：前台/动画层才连；切后台/换 session 应断开。 | **不开新上游连接**。复用 `HubRegistry` 进程级单一 `GET http://127.0.0.1:4096/global/event`（与控制面 `/slimapi/events` 共享，§5.2）。directory 仅过滤 GlobalBus 事件（进程级），不作为 query/header 打上游。 | 同 `/slimapi/events` 上游 SSE 包装；sidecar 仅消费 `message.part.delta`（逐 token，`field:"text"`）+ `message.part.updated`（text-start/end 边界）；其它事件由控制面消费，**互不干扰**。 | `TokenStreamHub`（`sse/token_hub.py`）：`part.type=="text"` 累积门控——text-start 建 `LivePart`（chunk-list，与订阅者无关）；逐 delta 入 `DeltaAccumulator`；`flush_loop` 100ms / 4KiB 早刷（§5.4）；text-end → `finish_part` 同步 drain 残余 delta → fanout `snapshot{done:true}` **marker（无 text，杠杆1）** → 退役。订阅握手（§5.5）：先 flush 现有订阅者 → 对新者发 `snapshot{done:false}`（累计全文=`join(chunks)`）→ 入 fanout。`safe_put` 先 size-check，超 `token_stream_max_frame_bytes`(1MiB) → `snapshot{truncated:true}`（不静默 drop）。`session.status=idle`（reason `session_idle`）/`session.deleted`/重连清该 sid live_parts + 扇 `resync{...,sessionID}`。 | 200 `text/event-stream`；头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`。**gzip 默认（lever2，首个 SSE gzip 例外；`Accept-Encoding` 协商，流式 zlib Z_SYNC_FLUSH）**。帧：`message.part.snapshot{done:false\|truncated:true}`（含 text）/ `message.part.snapshot{done:true}`（**marker 无 text，杠杆1**）/ `message.part.delta{text}` / `resync{reason,sessionID}` / `server.connected{sessionID}` / `server.heartbeat{}`(15s)。admission 满 → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。 | **不发 SSE `id:`、无 replay buffer**；`Last-Event-ID` 仅触发首帧 `resync{reconnect_no_replay,sessionID}`。终态顺序不变式：同 part 所有 `delta` 必先于 `snapshot{done:true}`；`done:true` 后该 part 不再发 delta。**T3 独立账本**（不占 `MAX_TOTAL_SUBSCRIBERS`）：8 subs × 64 queue items × 512KiB/sub + **4MiB live + 4MiB pending（不双计，Option B）累加器** = worst-case 12MiB。reasoning/tool part（`part.type!="text"`）的 delta **静默 drop+计数**（C3），不 resync。客户端权威仍走 `/since`（token `snapshot{done:true}` 仅流视角 marker）。控制面 `/slimapi/events` **仍不 gzip**（lever2 例外只限 token stream）。 |

## 4. Health 与透明反代

| sidecar 入口（`/slimapi/**` 项须带版本头；catch-all 不需要） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/health`**<br>须带 `X-Slimapi-Version:int`；无参数/body；可带 `Accept-Encoding`。 | 无 upstream 请求。 | 不适用。 | 读取进程版本、服务端 API 版本/接受区间及启动 smoke 设置的 `schema_degraded`。**rev F**：`schema` 加 `version`/`clientMin`/`clientMax`（从 config 读）。 | 200 `{"sidecar":{"ok":true,"version":"<pkg>"},"server":{"api_version":1,"accepted_client_versions":[1,1]},"schema":{"degraded":bool,"version":1,"clientMin":1,"clientMax":1}}`；gzip + `Vary`。缺/坏/越界版本头→门闩400。 | liveness，不代表 opencode 可达；schema 三键为**诊断回显**（非 feature discovery）。
| **GET `/slimapi/ready`**<br>须带 `X-Slimapi-Version:int`；无参数/body。 | `GET http://127.0.0.1:4096/global/health`，timeout 5s。 | health JSON；典型 200，或连接/timeout/5xx。 | 仅以 upstream status<300 判 ready，测 latency；不转发上游 health body；附 API 版本与接受区间。**rev F**：同 health 的 schema 三键。 | upstream<300→200；否则/异常→503。body 含 `upstream` + `server` + `schema:{degraded,version,clientMin,clientMax}`；gzip + `Vary`；版本头错误优先400。 | schema degraded 不改变 ready 状态；它只让消息 skeleton 路由自动降级 full。
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

转换后若 `_is_renderable()` 判定所有 part 均不可渲染，会**追加**一个 text 占位：`[内容已折叠，点开查看]`，ID 为 `thin_placeholder_{messageID}`，并带 `hasFull:true, omitted:["parts"]`；原 part 不会被丢弃。**rev F**：该 placeholder id **不参与** `/full` 的 `messageId+partId` 对齐；客户端应按 `partId.startsWith("thin_placeholder_")` 做 **message-level 整体替换**。schema-valid 下真实 part `id` 跨 thin/`/full` 稳定。

## 6. RouteToken 精确规则

- `issue_route_token()`：规范 JSON payload 使用 `orjson.OPT_SORT_KEYS`，签名为 `HMAC-SHA256(secret,payload)`，wire 为 `base64url(payload).base64url(signature)`，base64 padding 删除。
- payload：`v=1`、kind、requestID、sessionID、directory、iat、`exp=iat+3600`。
- `verify_route_token()`：恢复 padding并解码；constant-time 比较签名；验证 v、过期、iat 不超过当前时间60s、kind、requestID、可选 sessionID、directory 类型。
- secret 来自环境或 systemd `LoadCredential`，至少32 bytes，不随机生成；token 因而能跨 sidecar 重启验证。

## 7. B1 实现态补充（G2 / G7-soft / G8 / shell deny-list）

> 以下三节为 v1 B1 run（2026-07-18）落地行为的明确化补充。所有变更均为**加性**，未 bump `X-Slimapi-Version`。背景与代码地图见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md`（§2 reality 表 + §3 各项落点）。

### `GET /slimapi/sessions/{sid}/status`（G2 错误语义分离）

§1 表中该路径已指向本节。现行错误语义（取代 B1 前的"任意 discover 异常→503、不透传 404"）：

| 条件 | HTTP | body |
|---|---|---|
| upstream discover `/session/{sid}` 返 404 / 明确 not found | **404** | `{"code":"session_not_found","sessionID":"…"}` |
| discover 得到的 directory ∉ allowlist | **200**（F2 放宽：`normalize_directory` 不 gate；继续查 status map） | 正常 Status / idle；**不再** 400 `directory_not_allowed` |
| upstream 其它 4xx（401/403/409 等） | **502** | `{"code":"upstream_http_N"}` |
| upstream 超时 / 5xx / JSON 解析失败 | **503** | `{"code":"upstream_unavailable"}` |
| discover 与 status map 均成功，map 无 sid | **200** | `{"type":"idle"}`（假 idle 风险：session 已删但 status map 滞后可能误报 idle） |

实现：对 upstream 用 `httpx.HTTPStatusError` 精确判 404；其它 4xx → 502；仅网络/5xx/解析失败 → 503。批量 `GET /slimapi/sessions/status`（透传 upstream status + map）同样不 gate allowlist（**v0.3.0** 对齐）。

> **罕见边角（grill #4）**：discover `/session/{sid}` 返 **200 但 session payload 无可用 `directory` 字段** → **503 `upstream_unavailable`**。opencode 正常 session 始终带 directory；该分支视为 upstream payload 不可用，与 B1 前的"任意 discover 异常→503"行为一致，未引入新失败模式。

### `GET /slimapi/messages/**`（directory 转发）

`/slimapi/messages/{sid}`、`/slimapi/messages/{sid}/since/{ts}`、`/slimapi/messages/{sid}/full/{mid}`、**`/slimapi/messages/{sid}/full?ids=`（G6）** 四条消息路由统一经 `_resolve_messages_directory` 处理 directory。**v0.3.0** allowlist gate 已移除**：directory 不再因 ∉ allowlist 返 400，仅规范化后透传给上游 opencode。复用的 `normalize_directory()` 仍保留以保持转发一致：

| 条件 | 行为 |
|---|---|
| 未传 query `directory` | 不拦（依赖上游默认）；v1 不强制必填 |
| query `directory`（任意值，包括 `/projects` 未列出的） | normalize 后作 `X-Opencode-Directory` 头透传；由上游 opencode 决定能否服务 |
| 同时存在 `X-Opencode-Directory` header 且与 query 冲突 | **400** `{"code":"directory_not_allowed"}`（结构性歧义，slimapi 不猜该透传哪个） |

校验源：`_resolve_messages_directory()`（messages.py）。**directory 合法性 ≠ 多租户隔离**——隔离仍靠 stunnel mTLS（:14097）/ Tailscale ACL + 防火墙（:4097）+ loopback upstream 等网络边界。

### shell/PTY deny-list（catch-all 加固）

`install_proxy(app)` 注册的 catch-all 在转发前对 HTTP 路径做 deny-list 检查；命中即 **403 `{"code":"shell_not_allowed"}`**，不连接 upstream。WebSocket 继续走全局 WS→501（`proxy.py:9-13`），不动。

| 路径模式 | 方法 | 类别 | 来源 |
|---|---|---|---|
| `/session/{sid}/shell` | 任意（method-agnostic） | **任意命令执行**（spawn 子进程） | opencode `groups/session.ts:356`，handler `handlers/session.ts:341-347` |
| `/pty/**` | 任意 | shell/PTY 树（8 条 HTTP 变体：`/pty/shells`、`/pty`、`/pty/{id}` CRUD、`/pty/{id}/connect-token`） | opencode `groups/pty.ts:30-37` |
| `/api/pty/**` | 任意 | v2 PTY 树（7 条同构） | opencode `protocol/src/groups/pty.ts:23-119` |

匹配策略：前缀匹配 `/pty` 与 `/api/pty`；正则 `^/session/[^/]+/shell$` 匹配 `/session/{sid}/shell`。路径表**写死**（来自 B0 全量扫表），不臆造，也不暴露为配置字段。

**间接命令执行入口**（`/session/{id}/prompt`、`/prompt_async`、`/command`、`/tui/execute-command`、`/api/session/{id}/prompt`）**不 deny**——它们是 agent-mediated，且 `prompt_async` 是 §4 透传矩阵的主发送路径。

**Ops 开关**：`OC_SLIMAPI_SHELL_DENY_LIST_ENABLED`（默认 `1`=开）。关闭后 deny-list 不生效，**不构成安全保证**——真实隔离靠 stunnel mTLS + 网络边界 + upstream 权限；deny-list 是 best-effort 第二道。

**已知未覆盖（best-effort）**：路径大小写变体、`/./`、`/../` 段、双重编码。命中策略是精确/前缀字面匹配，不是 URL 规范化防火墙。

> **诚实限制**：catch-all 不识别路径语义是结构性事实；漏路径即可绕过。
