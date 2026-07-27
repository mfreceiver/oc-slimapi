# oc-slimapi 权威接口映射

> 本文按当前实现生成，代码基线为 `src/oc_slimapi/`。它描述“现在实际做什么”，不把设计文档中的未实现项写成既有行为。

> **Aligned with v2-contract.md (lite-v2)**

## 0. 全局约束

- sidecar 监听 host 由 `OC_SLIMAPI_HOST` 决定（默认 `127.0.0.1:4097`，可选 `0.0.0.0:4097` 作为明文直连入口）；应用层没有 Basic/Bearer 鉴权。入站鉴权由 sidecar 前的 stunnel mTLS（`:14097` 推荐）完成；若用 `0.0.0.0` 明文直连，安全由 Tailscale ACL / 防火墙负责。
- upstream 由 `config.py` 固定为 loopback HTTP，默认 `http://127.0.0.1:4096`，请求参数不能改 upstream，禁止 SQLite。
- `app.py` 先按 `health → sessions → messages → questions → events` 注册 `/slimapi/**`，最后调用 `install_proxy(app)` 注册 catch-all，故 thin route 不会被反代吞掉。
- **版本门闩**：每个 `/slimapi/**` REST/SSE 请求必须带 `X-Slimapi-Version:<int>`；当前接受闭区间 `[2,2]`。缺头或非整数→`400 {"code":"version_required","accepted":[2,2]}`；越界→`400 {"code":"version_incompatible","client":v,"accepted":[2,2]}`。非 slim catch-all 不检查版本头。
- `json_response()` honor `Accept-Encoding: gzip`，返回 `Content-Encoding:gzip` 和 `Vary:Accept-Encoding`；SSE、full 流式响应和多数上游错误透传不调用该 helper。
- `require_directory()` **已删除**（**v0.3.0** directory allowlist gate 全面移除）。directory 经 `normalize_directory()`（去尾斜杠，根 `/`）后作为 `X-Opencode-Directory` 头 + `?directory=` query 透传给上游 opencode；opencode 决定能否服务。allowlist 数据集已移除，**不再 gate**。保留的结构性守卫：显式 repeated `?directory=` 去重保序 + `invalid_directory_count`（1–32）+ messages `/**` 的 query `directory` 与 `X-Opencode-Directory` 头冲突 → 400 `directory_not_allowed`（结构性歧义）。
- FastAPI 参数/body 校验失败的实际状态是 **422**；业务校验主动抛出的错误通常是 400/502/503/504。

## 1. REST 读接口

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/sessions`**<br>无应用层鉴权；可带 `Accept-Encoding`。参数：`directory:str?`、`roots:bool=false`（默认 false，客户端应显式 `roots=true`）、`limit:int=100`（1–1000）、`start:int?`（≥0，**epoch-ms 时间戳水位** `time_updated>=start`，非 offset）、`search:str?`；无 body。 | `GET http://127.0.0.1:4096/session`；query 始终有 `limit`、`roots`，其余非空才传。directory 存在时同时传 `?directory=` 和 `X-Opencode-Directory`。 | `Session[]` 裸数组；通常 200；上游也可能 4xx/5xx。 | directory 经 `normalize_directory()`（**v0.3.0** 不 gate）；每项调用 `skeleton_session()`。**rev F**：`isinstance(payload, list)` 守卫（非 list→503）；200 加头 `X-Complete`（`len<limit`）。 | 200：`Session[]` 裸数组 + `X-Complete` 头；gzip + `Vary`。上游 4xx→502 `upstream_http_N`；5xx/网络/坏 JSON/非 list→503 `upstream_unavailable`。FastAPI 参数错误 422。 | 无 cursor；`limit=0` 被拒。`X-Complete` **不得**当权威全集。`start` 非 offset。
| **GET `/slimapi/messages/{sid}`**<br>`limit:int=40`（1–200，0 被拒）、`before:str?` opaque、`mode:skeleton|full=skeleton`（`mode=full` silently ignored，always skeleton）、`directory:str?`；无 body。列表按 `time.created` 升序排列。 | `GET http://127.0.0.1:4096/session/{sid}/message?limit=&before=`；directory 作为 `X-Opencode-Directory`。 | `MessageWithParts[]` 裸数组；分页可能带 `Link: <...?before=CURSOR>; rel="next"`；典型 200/400/404。 | 缓冲 body，64 MiB 上限，经转换 semaphore 后 `orjson.loads` + `skeleton_messages()`，**解析上游 `Link` 头中的 `?before=` cursor** → 下发 `X-Next-Cursor`（opaque base64url 字符串，不 decode/re-encode，**不再把 upstream `Link` 头原样复制给客户端**）。skeleton 规则见 §5。 | skeleton 200：裸数组、`Cache-Control:no-store`、下发 `X-Next-Cursor`（仅当上游给 `Link`），gzip+`Vary`。body>64 MiB→413 `response_too_large`；转换槽立即拿不到→503 `transform_busy`；参数错误 422；上游错误原状态透传。 | `before` 不解析、不重建。`directory` 仅作 `X-Opencode-Directory` header 转发上游；G7-soft query allowlist 校验见 §7 G7-soft。
| **GET `/slimapi/messages/{sid}/full/{mid}`**<br>`mode:skeleton|full=full`、`directory:str?`；无 body。`known.*` params removed, no 304/ETag/`X-Message-Event-Seq`, always returns 200。 | `GET http://127.0.0.1:4096/session/{sid}/message/{mid}`；directory 作为 header。always 200（无 304 短路）。 | 单个 `MessageWithParts`；典型 200/404。 | G8 流式读 upstream body（`client.send(stream=True)` + `read_with_cap` + `try/finally: await response.aclose()`）；累计字节超 32 MiB 立即 413。schema degraded 强制 full；full 缓冲后剥 `state.metadata.diagnostics`（其余原样）并卸载到 worker；skeleton 调 `skeleton_message()`；两模式共享转换 admission（admission 在 upstream GET 之前）。 | full：剥 diagnostics 后重序列化，sidecar 拥有响应头（`application/json`、gzip+`Vary`、`Cache-Control:no-store`）；skeleton 200：单对象、`Cache-Control:no-store`、gzip+`Vary`。>32 MiB→413 `message_too_large`；转换忙→**503 `transform_busy`**（与 list 归一）；上游 400/404/5xx 原状态/body 透传；参数错误 422。 | 路径段 `full` 仅作"展开全文"语义占位。流式读取（`client.send(stream=True)` + `read_with_cap` + `try/finally: aclose()`）。始终返回 200（无 304/ETag/`X-Message-Event-Seq`）；`known.*` params removed。




## 3. Curated SSE

| sidecar 入口（本表所有 `/slimapi/**` 项均须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/events`**<br>无 query/body 参数；可带 `Last-Event-ID`。**v2 重写**：全实例、全目录、合并 digest；不再按 directory/sessionId 过滤。 | `HubRegistry` 持**一条**进程级 `GET http://127.0.0.1:4096/global/event` + `Accept:text/event-stream`；connect=5s/read无限。无 directory header、无 `?directory=`（opencode GlobalBus 全实例）。 | 上游 SSE data JSON = `{directory:str, project?, workspace?, payload:{id?, type, properties}}`（注意比旧 `/event` 多一层 `{directory, payload}` 包装）。事件类型：`session.status`(idle/busy)、`session.updated`、`session.deleted`、`session.error`（G1）；`message.updated`/`message.appended`（带 messageID）；`question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.asked`/`permission.v2.resolved`；`message.part.delta` + `tool.*` 等仍丢弃。 | `HubRegistry.get_global()` 返回单一 `GlobalHub`（`get(directory)` 兼容签名但忽略 directory）。`subscribe()` 立即在 subscriber queue 放 `server.connected` 首帧，再启动 run/flush_loop(250ms)/heartbeat_loop(10s) 任务。`publish()`：question/permission 立即扇出原帧（不进 debounce）；`session.*`/`message.updated`/`message.appended` 累积进 `pending: dict[sessionID, DigestFields]`，字段按变化合并（status/messageID/updatedAt 各取最新、archived 一旦有值粘滞保留时间戳、deleted 一旦 true 持续）；G1：`session.error` 有 sid → sticky `lastError` 进 digest 并立即 flush；无 sid → 立即直推 `event: session.error` `{directory?,name,message,at}`；`MessageAbortedError` 静默过滤。`message.part.updated` 通知 token hub（v0.6 §Q）；`message.part.removed`（flat props）→ digest 更新（通知 client partCount 变）；`message.removed`（flat props）→ **v0.6 §P** 路由到 token hub `on_message_removed`（tombstone 帧 + 重放队列）。其余类型静默丢弃。`flush()` 每 250ms 遍历 pending，每 session 吐一帧 `event: session.digest` `{"sessionID","directory","status"?,"messageID"?,"updatedAt"? (sidecar wall-clock epoch_ms),"archived"?(epoch_ms),"deleted"?,"lastError"?}`，清 pending。`heartbeat_loop` 每 10s 吐 `event: server.heartbeat` `{}`。`run()` 上游断开指数退避（1→30s）；重连后调用 `resync_all()` 向所有 subscriber 吐 `event: resync` `{"reason":"reconnect_no_replay"}`。每订阅 `asyncio.Queue(maxsize=256)` + 字节预算；溢出时 **close → 清空全部旧 queue → 发 `resync{reason:subscriber_backpressure}` → `STOP`** 断慢消费者（**不**丢最旧续灌）。末 subscriber 离开后 30s grace 再取消任务。 | 200 `text/event-stream`；头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`；**不 gzip**。客户端带 Last-Event-ID 时首帧为 `event: resync`；正常连接首帧为 `event: server.connected`。吐出帧：`session.digest`（含 `lastError?`）+ **`session.error`（G1-B，无 sid）** + q/p 直推 + connected/heartbeat/resync。`session.digest` 字段按窗口内变化出现（未变化的字段不在帧里；`lastError` sticky 跨窗口，`status=busy` 显式 `null` 清除）。**无 replay 承诺**——客户端收到 resync 后走 latest-id/catch-up。 | 不保存事件、不承诺 replay。`directory`/`sessionId`/`stream` 参数完全移除（v1→v2 演化，无 bump：客户端按新帧类型解析；旧 A 桶客户端未对接，无破坏）。`HubRegistry(client)` + `close()` 签名保持不变（app.py 零改动）。

### 3.1 Token stream SSE（**Stages A–E 落地，opt-in 实时流**）

> 行为权威：`docs/specs/design-token-stream.md` §5.1（端点）/ §5.4（批式）/ §5.5（握手）/ §5.6（wire 帧，**杠杆1 done:true marker 无 text**）/ §5.8（背压/重连）/ §6（T3 信封，**Option B 拆 4+4**）/ §7（**杠杆2 gzip 首个 SSE 例外**）。wire 契约：`docs/specs/v2-contract.md` §3.x + §6.x。下表为已落地行为。

| sidecar 入口（须带 `X-Slimapi-Version`） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/sessions/{sid}/stream`**<br>`directory:str?`（optional query）；可带 `Last-Event-ID`（值忽略，仅触发首帧 resync）。**opt-in**：前台/动画层才连；切后台/换 session 应断开。 | **不开新上游连接**。复用 `HubRegistry` 进程级单一 `GET http://127.0.0.1:4096/global/event`（与控制面 `/slimapi/events` 共享，§5.2）。directory 仅过滤 GlobalBus 事件（进程级），不作为 query/header 打上游。 | 同 `/slimapi/events` 上游 SSE 包装；sidecar 仅消费 `message.part.delta`（逐 token，`field:"text"`）+ `message.part.updated`（text-start/end 边界）+ `message.part.removed`（flat props，v0.6 路由到 token hub 退役 part 状态）+ `message.removed`（flat props，v0.6 §P tombstone 路由）；其它事件由控制面消费，**互不干扰**。 | `TokenStreamHub`（`sse/token_hub.py`）：`part.type=="text"` 累积门控——text-start 建 `LivePart`（chunk-list，与订阅者无关）；逐 delta 入 `DeltaAccumulator`；`flush_loop` 100ms / 4KiB 早刷（§5.4）；text-end → `finish_part` 同步 drain 残余 delta → fanout `snapshot{done:true}` **marker（无 text，杠杆1）** → 退役。订阅握手（§5.5）：`server.connected` → **v0.6 §P** 该 session 未过期 `message.removed` tombstones 按时间重放 → flush 现有订阅者 → 对新者发 `snapshot{done:false}`（累计全文=`join(chunks)`）→ 入 fanout。`safe_put` 先 size-check，超 `token_stream_max_frame_bytes`(1MiB) → `snapshot{truncated:true}`（不静默 drop）。`session.status=idle`（reason `session_idle`）/`session.deleted`/重连清该 sid live_parts + 扇 `resync{...,sessionID}`（**v0.6 §P**：重放队列不受重连影响）。 | 200 `text/event-stream`；头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`。**gzip 默认（lever2，首个 SSE gzip 例外；`Accept-Encoding` 协商，流式 zlib Z_SYNC_FLUSH）**。帧：`message.part.snapshot{done:false\|truncated:true}`（含 text）/ `message.part.snapshot{done:true}`（**marker 无 text，杠杆1**）/ `message.part.delta{text}` / `resync{reason,sessionID}` / `server.connected{sessionID}` / `server.heartbeat{}`(15s) / **`message.removed{sessionID,messageID}`（v0.6 §P tombstone）**。admission 满 → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`；handshake buffer overflow → 503 `{"code":"sse_token_handshake_overflow","limit":8,"current":N,"bufferBytes":8388608}` + `Retry-After:5`。 | **不发 SSE `id:`**；`Last-Event-ID` 仅触发首帧 `resync{reconnect_no_replay,sessionID}`。**v0.6 §P 重放队列**（cap 1000，TTL 24h，FIFO）：`message.removed` 事件记入队列，握手期按 session 过滤重放。终态顺序不变式：同 part 所有 `delta` 必先于 `snapshot{done:true}`；`done:true` 后该 part 不再发 delta。**T3 独立账本**（不占 `MAX_TOTAL_SUBSCRIBERS`）：8 subs × 64 queue items × 512KiB/sub + **4MiB live + 4MiB pending（不双计，Option B）累加器** = worst-case 12MiB。reasoning/tool part（`part.type!="text"`）的 delta **静默 drop+计数**（C3），不 resync。客户端权威仍走 `GET /slimapi/messages/{sid}`（token `snapshot{done:true}` 仅流视角 marker）。控制面 `/slimapi/events` **仍不 gzip**（lever2 例外只限 token stream）。 |

## 4. Health 与透明反代

| sidecar 入口（`/slimapi/**` 项须带版本头；catch-all 不需要） | 构造上游请求 | 上游预期返回 | sidecar 处理 | 返回请求方 | 坑 / 约束 |
|---|---|---|---|---|---|
| **GET `/slimapi/health`**<br>须带 `X-Slimapi-Version:int`；无参数/body；可带 `Accept-Encoding`。 | 无 upstream 请求。 | 不适用。 | 读取进程版本、服务端 API 版本/接受区间及启动 smoke 设置的 `schema_degraded`。**rev F**：`schema` 加 `version`/`clientMin`/`clientMax`（从 config 读）。 | 200 `{"sidecar":{"ok":true,"version":"<pkg>"},"server":{"api_version":2,"accepted_client_versions":[2,2]},"schema":{"degraded":bool,"version":2,"clientMin":2,"clientMax":2}}`；gzip + `Vary`。缺/坏/越界版本头→门闩400。 | liveness，不代表 opencode 可达；schema 三键为**诊断回显**（非 feature discovery）。
| **GET `/slimapi/ready`**<br>须带 `X-Slimapi-Version:int`；无参数/body。 | `GET http://127.0.0.1:4096/global/health`，timeout 5s。 | health JSON；典型 200，或连接/timeout/5xx。 | 仅以 upstream status<300 判 ready，测 latency；不转发上游 health body；附 API 版本与接受区间。**rev F**：同 health 的 schema 三键。 | upstream<300→200；否则/异常→503。body 含 `upstream` + `server` + `schema:{degraded,version,clientMin,clientMax}`；gzip + `Vary`；版本头错误优先400。 | schema degraded 不改变 ready 状态；它只让消息 skeleton 路由自动降级 full。
| **GET `/slimapi/metrics`**<br>须带 `X-Slimapi-Version:int`；无参数/body；可带 `Accept-Encoding`。 | 无 upstream 请求。 | 不适用。 | T3 观测面：`HubRegistry.snapshot_metrics()`（`sse`/`subscribers`/`hubs`/`clients`）+ `batch` ledger 快照（`batch` key 始终为 null，因 BatchLedger 已移除）；可选 `sse.tokenStream`（token registry 接线时）+ `traffic`（traffic ledger 接线时，**加性**）。 | 200：`{sse, skeleton, batch: null, sse.tokenStream?, traffic?}`；gzip+`Vary`。缺/坏/越界版本头→门闩 400。 | T3 ops 观测端点（契约 §2/§6），**非客户端契约**。形状**加性**：未接线的块省略（旧 fixture/客户端形状不变）。`traffic` 块详见 `docs/manual/traffic-accounting.md`。
| **HTTP catch-all `/{path:path}`**<br>方法 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS；原 query/header/body。`/slimapi/**` 未命中路径被显式挡住。 | `METHOD http://127.0.0.1:4096/{path}`；重复 query 保留；body 用 `request.stream()`；剥 hop-by-hop/Connection token。`/event`、`/global/event` read timeout=None；以 `/command` 结尾 read=300s；其他 read=30s；write=300s。 | 任意 opencode HTTP 响应；SSE 为流；普通接口 JSON/文件等。 | upstream 固定 loopback，不能由请求控制；`client.send(...,stream=True)` + `aiter_raw()`，剥响应 hop-by-hop，完成后关闭 upstream response。 | 非 slim 路径：上游状态、raw body、Content-Encoding 和非 hop-by-hop headers 流式返回，不额外 gzip。未知 `/slimapi/**`→sidecar 404 `{"code":"thin_route_not_found"}`，不会透传。 | catch-all 必须最后注册，当前 `app.py` 已保证。原始 `/event`/`/global/event` 不缓冲。异常没有统一映射，可能成为 500。
| **WebSocket catch-all `/{path:path}`**<br>任意 WS path。 | 不连接 upstream。 | 不适用。 | 接受 WebSocket，发送 `{"code":"websocket_not_supported","status":501}`，随后以 code 1011 关闭。 | WebSocket 消息内表达 501；不是 HTTP 501 response。 | PTY/WS 需另上 nginx/Caddy；当前行为与“HTTP WS→501”字面契约并不完全相同。

## 5. Skeleton 精确规则

消息由 `skeleton_messages()` / `skeleton_message()` 转换；`info` 深拷贝完整保留，parts 由 `skeleton_part()` 分派：

| part.type | 当前处理 |
|---|---|
| `text` | 整 part 深拷贝，全留。 |
| `reasoning` | 留 `id,type,messageID,sessionID,text`；删除其他字段时用 `_mark()` 加 `hasFull:true` 和排序去重后的 `omitted`。 |
| `tool` | `_tool()` 留 ids/tool/callID；state 留 status/title/time；input 仅 `path,filePath,file_path,command,agent,description,subagent_type,todos`；metadata 仅 `sessionId,sessionID,description,agent`；**`output`/`error` 阈值内联**（JSON 字节 ≤4KiB 且 message 累计 ≤16KiB → 留，否则整字段 omit + `hasFull`，绝不半截断）；`structured/result/raw/attachments` 始终删并标记。 |
| `patch` | `_patch()` 留 ids、files 的 path/additions/deletions/status、metadata.path、state status/title/time 及 input 路径键；**`output`/`error` 同 tool 阈值内联**（超 → omit + `hasFull`）。 |
| `file` | `_file()` 留 ids/filename/mime；≤8KiB 的 `http(s)` URL 保留；`data:`、过长或其他 URL 置 null 并标记；source 删除。 |
| `step-start` / `step-finish` | 只留 ids；若删除字段则标记。 |
| `compaction` | orjson 编码≤64KiB 时整 part 保留；超限降为 ids + `hasFull/omitted:["*"]`。 |
| 未知 | 只留 ids；`hasFull:true`，omitted 为实际删除键，若无其他键则为 `["*"]`。 |

转换后若 `_is_renderable()` 判定所有 part 均不可渲染，会**追加**一个 text 占位：`[内容已折叠，点开查看]`，ID 为 `thin_placeholder_{messageID}`，并带 `hasFull:true, omitted:["parts"]`；原 part 不会被丢弃。**rev F**：该 placeholder id **不参与** `/full` 的 `messageId+partId` 对齐；客户端应按 `partId.startsWith("thin_placeholder_")` 做 **message-level 整体替换**。schema-valid 下真实 part `id` 跨 thin/`/full` 稳定。

## 7. B1 实现态补充（G7-soft / G8 / shell deny-list）

### `GET /slimapi/messages/**`（directory 转发）。背景与代码地图见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md`（§2 reality 表 + §3 各项落点）。

### `GET /slimapi/messages/**`（directory 转发）

`/slimapi/messages/{sid}`、`/slimapi/messages/{sid}/full/{mid}` 两条消息路由统一经 `_resolve_messages_directory` 处理 directory。**v0.3.0** allowlist gate 已移除**：directory 不再因 ∉ allowlist 返 400，仅规范化后透传给上游 opencode。复用的 `normalize_directory()` 仍保留以保持转发一致：

| 条件 | 行为 |
|---|---|
| 未传 query `directory` | 不拦（依赖上游默认）；v2 不强制必填 |
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
