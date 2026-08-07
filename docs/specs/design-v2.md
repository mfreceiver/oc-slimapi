> **Design document — aligned with v2-contract.md (lite-v2)**
>
> Sections describing deleted endpoints have been removed. See v2-contract.md
> for the authoritative wire specification.

# oc-slimapi 设计

> 硬约束：**纯 HTTP、禁 SQLite、只建在 legacy `/session`、loopback-only、stunnel mTLS、不替换 `List<MessageWithParts>` 形状**。
> 版本契约：路径固定为 `/slimapi/**`，版本走必填请求头 `X-Slimapi-Version: <int>`；版本门禁仅接受配置闭区间，catch-all 不受影响。

---

## 0. 关键设计决策

1. **tool skeleton 不发明 `inputSummary` 键**——客户端从 `state.input` 派生。保留**瘦身后 `state.input`**（白名单键）。
2. **reasoning skeleton 保留 `text`**（删 text 会触发 `isEffectivelyRenderableEmpty` 把整条纯思考消息过滤掉；保留全文是 UI 安全权衡，体积影响见 §6 实测）。
3. **file `url`**：`data:` 剥/null、`http(s)` 短则留。
4. **sessions 无 `cursor`**（legacy 用 `start`）；保留 `summary{additions,deletions,files}` + `revert{messageID,partID}`；Session 模型无 cost/tokens（在 Message 上）。
5. **SSE replay 降级**：无 event store→重连发 `event: resync`，不承诺 Last-Event-ID 补发。
6. **SSE 桥（session.digest 合并帧）是核心实时通道**，非可选阶段。
7. **客户端模型必须扩字段**：`Part.hasFull/omitted`、SSEClient 裸帧归一化+404 回退。
8. **部署用双 stunnel**（14096 直连 + 14097 经 sidecar）保障真回退。
9. **sidecar 自 gzip** + `Vary: Accept-Encoding`；gzip 实测列上线门禁。
10. **skeleton 内存保护**：upstream body 上限（64MB）+ 转换并发（≤2）+ `MemoryMax=384M`。

---

## 1. 接口契约（参数 + 与 opencode 的精确转化）

### 1.1 `GET /slimapi/sessions`
- **参数**：`directory`(str?, 绝对路径；normalize 后透传)、`roots`(bool)、`limit`(int 1–1000 默认100)、`start`(int? ms,透传)、`search`(str?)。**无 cursor**。
- **upstream**：`GET /session?directory=&roots=&limit=&start=&search=` + `X-Opencode-Directory`。
- **转化**（字段白名单）：
  - **留**：`id, directory, parentID, projectID, title, agent, model{id,providerID,variant?}, time{created,updated,archived}, summary{additions,deletions,files}, revert{messageID,partID}`
  - **剥**：`summary.diffs/summary_diffs, revert.snapshot, revert.diff, metadata, share, slug, version, path, permission`
- **响应**：`Session[]` 裸数组（不套 envelope）。
- **响应头**：`X-Complete`（`"true"` = 本页未满，`"false"` = 可能截断）。已移除 `X-Discovery-Directories` 与 `X-Discovery-Ready`。

### 1.2 `GET /slimapi/messages/{sid}`（历史分页，核心省流）
- **参数**：`sid`；`limit`(int **1–200** 默认40，**0→422** FastAPI ge=1)；`before`(str?, opaque, 原样透传)；`directory`(透传)。
- **`mode` 参数**：已移除 `mode=full`（静默忽略）；始终返回 skeleton 形态。
- **排序**：按消息 `created` 升序（oldest-first）。
- **upstream**：`GET /session/{sid}/message?limit=&before=` + `X-Opencode-Directory`。
- **响应**：`List<MessageWithParts>` **裸数组**（不套 envelope）；sidecar **解析上游 `Link: <...?before=CURSOR>; rel="next"` 头** → 下发 `X-Next-Cursor`（opaque base64url 字符串，不 decode/re-encode）；`Cache-Control:no-store`。
- **裁剪**：顺序/id/数量不变；按 §2 规则；`info` 保留 `tokens/cost`（上下文用量）。
- **保护**：upstream body >64MB 或转换并发超限 → 413/503；TransformPool admission（在 upstream GET 之前获取），池饱和→503 `transform_busy`。

### 1.3 `GET /slimapi/messages/{sid}/full/{mid}`（按需展开）
- **参数**：`sid`、`mid`。
- **upstream**：`GET /session/{sid}/message/{mid}`。
- **响应**：单 `MessageWithParts`；full 剥 `state.metadata.diagnostics`（其余原样）；`Cache-Control:no-store`、禁 body 日志、限并发。
- **始终返回 200**，无 304 Not Modified / ETag / 条件请求。
- 已移除 `X-Message-Event-Seq` 响应头与 `?known.*` 条件参数。
- **超限**：>32MB（对齐客户端 `ResponseSizeGuardInterceptor`）→`413 {"code":"message_too_large","limitBytes":..}`。
- **客户端合并**：按 `messageId+partId` 替换（非追加）。

### 1.4 `GET /slimapi/events`（策展 SSE：单全局连接 + digest 合并）

- **参数**：无（移除 `directory`、`sessionId`、`stream`）。**全实例、全目录**——客户端在本地按需过滤。
- **upstream**：sidecar 持**一条**进程级 `/global/event` 订阅（opencode GlobalBus，无 directory 过滤）。重连指数退避（1→30s）。
- **上游帧形状**：`{directory, project?, workspace?, payload:{id?, type, properties}}`——hub 在 `publish()` 解包。
- **吐出帧（仅以下）**：
  1. **`event: session.digest`**——debounce 250ms/session，每 session 一帧；窗口内有变化的 session 才发，字段按变化出现：
     ```
      event: session.digest
       data: {"sessionID":"...","directory":"/path","status?":"busy","messageID?":"msg_..","updatedAt?":<epoch_ms>,"archived?":<epoch_ms>,"deleted?":true,"lastError?":{"name":"...","message":"...","at":<epoch_ms>}|null,"turnIncarnation?":<int>,"turn?":<int>}
      ```
      - `status` ← `session.status`(idle/busy) 的 properties.status
      - `messageID` ← `message.updated`/`message.appended` 的 `info.id`（取最新）
      - `updatedAt` ← sidecar 收到事件时的 **wall-clock epoch-ms**（非上游时间戳）
      - `deleted=true` ← `session.deleted`（一旦为 true 持续到窗口结束）
      - `archived` ← `session.updated` 的 `info.time.archived`（epoch_ms int，一旦有值粘滞保留到窗口结束）
      - `directory` ← GlobalEvent 的 directory
      - `turnIncarnation?`/`turn?`（turn token fence，**配对出现/缺失**）：服务端因果标识，flat 顶层；turn_registry 装配（lifespan 级）时 stamp（未观测 sid → `(inc,0)`），未装配时两字段缺省。详见 `v2-contract.md` §3.y。
      - **`lastError`（G1-A）**←有 sid 的 `session.error` 经脱敏后的 `{name,message,at}`（`at`=sidecar 收到 epoch-ms）。**三态 wire**（与 sticky 共存，互不矛盾；权威见 `docs/specs/v2-contract.md` §3）：
       - **对象** `{name,message,at}`：本窗口新 error，或 flush 时该 sid 仍有 sticky（其它字段触发的后续 digest 会继续带出对象，直至 clear/deleted）；error 到达时**立即 flush**（不等 250ms）
       - **显式 `null`**：clear 帧——该 session 出现新 `status=busy` 时 pop sticky 并立即 flush
       - **省略**：本 digest 没有本窗口新 error 对象、也没有显式 clear（`null`），**且** 该 sid 当前不存在 sticky error；`deleted=true` 的 digest **强制省略**（pop sticky，**不**发 null）
       - abort（`error.name=="MessageAbortedError"`）静默丢弃（不写 lastError、不发 G1-B 帧）
     - `session.updated` 创建 pending 项；若 `info.time.archived` 有值则设 `archived`（见上）；无其它字段变化时 emit `{sessionID,directory}` 让客户端 refetch `/sessions`
      - 同 session 多次变化 → 合并取最新；窗口 flush 后清 pending（lastError sticky 经独立持久层跨窗口保留，见 `docs/specs/v2-contract.md` §3）。
   2. **`event: session.error`（G1-B）**——**无** `sessionID` 时**立即直推**（不进 debounce）：`data: {"directory"?,"name","message","at"}`。有 sid 的 `session.error` **不**走本帧，走 digest `lastError`（G1-A）。abort（`MessageAbortedError`）静默丢弃。wire 权威见 `docs/specs/v2-contract.md` §3。
   - **全部丢弃（不进 digest/不策展转发）**：`message.part.delta`/`.updated`/`.removed`（逐 token，仅路由到 token hub 供 token stream 消费）、`message.removed`（仅路由到 token hub 供 tombstone/token stream 维护）、`tool.*`、未知类型——省流核心。
- **直推转发（观察信号）**：`question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`——立即扇出订阅者，不进 debounce；客户端用于驱动 UI 提示，具体应答走 catch-all + `X-Opencode-Directory`（v2 无专用写端点）。
- 上游 `session.error` **不**在此列：经 G1 处理（有 sid→digest `lastError`；无 sid→`event: session.error`；abort 过滤），见上吐出帧。
- **背压**：每订阅 `asyncio.Queue`（item 上限 + 字节预算）；溢出时 **立即断开慢消费者**：标记 `closed` → **清空全部旧 queue 帧** → 入队 `event: resync` `{"reason":"subscriber_backpressure"}` → 入队 `STOP`（**不**交付此前积压帧，**不**「丢最旧续发」）。
- **不承诺 replay**：无 event store；重连接收 `resync` 后走冷启动流程（sessions + messages）或前台 catch-up。
- **生命周期**：首 subscriber 到达→启动 upstream 任务；末 subscriber 离开后 30s grace 再取消任务；`HubRegistry.close()` 取消所有任务。
- **HubRegistry 接口**：`HubRegistry(client)` + `await close()`；内部维护单一 `_global: GlobalHub`。

### 1.5 `GET /slimapi/health` + `GET /slimapi/ready`
- `/health`：liveness，进程可服务→200。
- `/ready`：探 `GET /global/health`，upstream 不通→503 `{"upstream":{"ok":false}}`。
- 两者均暴露 `server.api_version`；health 另暴露 `server.accepted_client_versions`，客户端据此自检。
- 启动字段漂移 smoke 保留（app.py:35-56 运行消息字段校验，异常设 schema_degraded 供 health/ready 回显）。

### 1.6 透传反代（catch-all）
- 非 `/slimapi/**` → `http://127.0.0.1:4096/{path}`；method/query/body 流式透传。
- 剥 hop-by-hop（`Connection/Keep-Alive/TE/Trailer/Transfer-Encoding/Upgrade/Proxy-*/Host`）。
- **超时**：command ≥300s（客户端 commandApi 读超时）；SSE 无限 read + 禁缓冲 + `aiter_raw()` 保 `Content-Encoding`。
- **WebSocket**→501（不处理；PTY 需另上 nginx/Caddy）。
- **SSRF 防护**：upstream 固定 loopback，禁参数控制；禁 body 日志。

### 1.7 `GET /slimapi/questions`（跨目录 pending question 聚合，加性）

- **参数**：无（sidecar 自发现目录）。
- **upstream**：两阶段 fan-out——(1) `GET /experimental/session?roots=true&archived=true`（opencode 全局顶层 session 列表 + 含已归档 session，每个 session 携带真实 `directory` 字段——覆盖 git repo + 非-git目录 + git worktree 子目录 + archived-only workdir）发现 distinct directory 集合；(2) 并发（`asyncio.gather`）对每个 dir `GET /question`（带 `X-Opencode-Directory`）合并。（**2026-08-07**：发现源从 `GET /project` 改为 `/experimental/session?roots=true&archived=true`——根因 `/project` 把非-git workdir 归到合成 global（`worktree="/"`）被跳过，漏报非-git/临时目录 + git worktree 子目录的 pending question；`archived=true` 使发现集合成超集防 archived-only workdir 漏报。）
- **转化**：每条上游 entry 原样转发 + 追加 `directory` 字段（无 skeleton 投影、无转换池 admission）。
- **响应**：**envelope 对象** `{items, errors, authoritativeDirectories, discoveryComplete}`（非裸数组，表达 partial 失败）。`discoveryComplete` 为 `true` 除非发现页填满 `_DISCOVERY_LIMIT`(=10000)（`roots=true` 只返顶层 session，实际恒 `true`）。
- **客户端契约**：`authoritativeDirectories==null` → 全局 replace-all；数组 → 仅 partial replace 所列 dir（不得丢弃未覆盖 dir 的既有 pending question，否则数据丢失）。
- **保护**：发现调用失败 → 整体 503 `upstream_unavailable`（无 envelope）；per-dir 失败 isolated 进 `errors[]`（5xx→`upstream_unavailable`，4xx→`upstream_http_N`，不中断整体）。
- **加性**：未 bump `X-Slimapi-Version`（仍 2）；旧 sidecar→catch-all 404 `thin_route_not_found`。详见 `v2-contract.md` §2「`/slimapi/questions` envelope」。

### 1.8 `GET /slimapi/command` / `GET /slimapi/agent`（catalog skeleton，加性）

- **参数**：`directory?`（可选，仅作 `X-Opencode-Directory` header 转发；catalog 全局，上游忽略）。
- **upstream**：`GET /command` / `GET /agent`（透传）。
- **转化**（白名单投影）：
  - `/command`：留 `{name,description,agent?,hints?}`，丢 `template`/`source`/`model`/`subtask`（raw 省 ~97.6%）。
  - `/agent`：留 `{name,description,mode,hidden?,native?}`，丢 `prompt`/`permission`/`topP`/`temperature`/`color`/`variant`/`options`/`steps`/`model`（raw 省 ~95.8%）。
- **响应**：裸数组；catalog 无 `hasFull`/`omitted`（无 per-entry expand 端点）。
- **保护**：转换池 admission 先于 upstream GET + 流式 `read_with_cap`（超 `max_response_bytes`→413）+ worker gzip；错误映射同 messages thin 路由（4xx→502 `upstream_http_N`；5xx/网络/坏 JSON/非 list→503 `upstream_unavailable`；转换池满→503 `transform_busy`+`Retry-After:2`；参数错误 422）。
- **加性**：未 bump `X-Slimapi-Version`（仍 2）；旧 sidecar→catch-all 404，回退透传 `GET /command`/`GET /agent`。详见 `v2-contract.md` §2。

---

## 2. 骨架字段规则（mode=skeleton）

`MessageWithParts` 的 `info` 完整保留（含 `tokens/cost`）。parts：

| part.type | 处理 | 留 | 删 |
|---|---|---|---|
| `text` | 全留 | 全部 | — |
| `reasoning` | **留 `text`** | `id,type,messageID,sessionID,text` | （不删 text，否则触发消息过滤） |
| `tool` | 瘦 `state.input`+留元数据+**阈值化 output/error** | `id,type,tool,callID,messageID,sessionID`；`state{status,title,time}`；**`state.input` 白名单键**(path/filePath/file_path/command/agent/description/subagent_type/todos)；`state.metadata{sessionId,sessionID,description,agent}`；**`state.output`/`state.error` 阈值内联**（见下"阈值化 skeleton"） | `state.structured/result/raw/attachments`（**始终删**）、`state.input.{newString,oldString,content,patchText}`、`metadata.diagnostics`；output/error 超阈值（或 message 预算耗尽）时亦入 omitted |
| `patch` | 留 files+路径+**阈值化 output/error** | `files[{path,additions,deletions,status}]`、`metadata.path`、瘦 `state.input.path`；**`state.output`/`state.error` 阈值内联**（同 tool） | output/error 超阈值 → omitted+`hasFull` |
| `file` | 按 url 类型 | `filename,mime`；`url`：`http(s)`短则留、`data:`则 null + `hasFull` | `source`(base64) |
| `step-start` | ids | `id,type,messageID,sessionID` | snapshot |
| `step-finish` | ids | `id,type,messageID,sessionID`（finish 可留 reason/cost/tokens） | snapshot |
| `compaction` | 全留（设单 part 上限） | 全部 | — |
| 未知 | ids+标记 | `id,type,messageID,sessionID` | 大字段（受总响应上限保护） |

**阈值化 skeleton（加性，wire 版本仍 2；默认常开，无 opt-in）**：tool/patch 的 `state.output` 与 `state.error` 按 **JSON 字节**（`orjson.dumps` 序列化长度，即上线字节，含引号/多字节）计：
- per-field ≤ `SKELETON_INLINE_OUTPUT_MAX_BYTES`（默认 4 KiB）**且** 该 message 累计内联 ≤ `SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES`（默认 16 KiB）→ **原样内联**进 thin state（不进 omitted）。
- 超任一阈值 → **整字段 omit**（**绝不半截断**）+ `omitted` 记 `state.output`/`state.error`，可经 `/full` 取回完整值。
- `state.structured/result/raw/attachments` **始终 omit**（巨型嵌套 JSON 无内联价值）。
- **`hasFull` 语义**：仅当该 part 仍有 omitted 字段才置 `true`；某 part 所有 output/error 都内联且无其他删字段 → **不设** `hasFull`（客户端 UI 不出现展开按钮）。`hasFull` 只表示"还有可经 full 取回的字段"，**绝不**表示"当前内容不可见"。
- env 调参（不改契约）：`OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES` / `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES`。外层仍受 `max_response_bytes` 约束（阈值化不绕过）。`/slimapi/health` 的 `features.thresholdedSkeleton`/`skeletonInlineOutputMaxBytes` 仅供诊断，行为不依赖客户端识别。

**所有被裁剪的 part 加标记**（需客户端扩模型，见 §3）：
```json
{"hasFull":true,"omitted":["state.output","state.input.newString"]}
```
若裁剪后某 message 无可渲染 part，sidecar 注入 1 个 text 占位 part（`"[内容已折叠，点开查看]"`），**禁返回空 parts 数组**（否则 `isEffectivelyRenderableEmpty` 过滤整条）。

---

## 3. 客户端改动清单

1. **`Part` 扩字段**：`hasFull:Boolean?=null`、`omitted:List<String>?=null`（`ignoreUnknownKeys=true` 当前会丢弃这些标记 → 无展开 affordance）。
2. **展开 hook**：`hasFull && omitted` 的 part，首次展开→`GET /slimapi/messages/{sid}/full/{mid}` → 按 `messageId+partId` 替换；loading/失败内联状态。
3. **`SSEClient.kt`**：连接单一 `/slimapi/events`（**无 query 参数**，全实例聚合）；curated 帧解析（`session.digest` / `session.error` / `heartbeat` / `resync`）。
4. **GET 侧 circuit breaker**：连续 3 次 sidecar transport/5xx→禁用 thin 5min→half-open 探测；**mutation 不双发**（POST 可能已被 upstream 接收）。
5. **增量 reducer**：处理 `session.digest`（debounced 时间戳水位推进）、`event:resync`（前台 catch-up）、`event:session.error`（G1，UI banner/toast）。

---

## 4. 部署

- **双 stunnel 入口**（都 mTLS + 同 CA/客户端证书）：
  - `14096 → 127.0.0.1:4096`（直连 opencode，回退用）
  - `14097 → 127.0.0.1:4097`（经 sidecar）
- **sidecar** 默认 `127.0.0.1:4097`；可选 `0.0.0.0:4097` 作为明文直连入口（Tailscale 直达，依赖 Tailscale ACL / 防火墙；非 routable 主机仍启动 assert 拒绝）。upstream 始终固定 loopback HTTP；systemd user unit，`Restart=on-failure`，`MemoryMax=384M`。
- 框架：**FastAPI+httpx+orjson+uvicorn**（typed 校验降低契约错误）；单 worker（共享 SSE hub）。
- SSE 长连接：stunnel `TIMEOUTidle=0 或 43200` + `TCP_NODELAY`+`SO_KEEPALIVE`。

---

## 5. gzip 实测（上线门禁）

REST 字节是原始 JSON（opencode ≥1KB 自动 gzip，OkHttp 自动解压）；**控制面 SSE 不 gzip**（真实 wire）；**token stream SSE 默认 gzip**（杠杆2，首个 SSE gzip 例外，见 §3.x.3）。**sidecar 必须自 gzip 响应 + `Vary: Accept-Encoding`**，否则手机拿原始 40KB。

测法：`curl --compressed -o /dev/null -w '%{size_download}'`（=下载字节）vs `Accept-Encoding: identity`（=原始）；确认 `Content-Encoding: gzip`；SSE 确认无 `Content-Encoding`。必测：messages(limit=1/10/40, skeleton)、sessions、single message。Android 端用 `TrafficCountingInterceptor` 校准（含 TLS/header）。
**gzip 后 full vs skeleton wire 差从 11× 降到 ~5–8×，仍显著**；收益表须 raw/gzip 双口径。

---

## 6. 实测基准与骨架体积现实

骨架体积取决于会话内容（reasoning-heavy vs text/tool-heavy）；保留 `reasoning.text` 全文是 UI 安全权衡（删了会触发 `isEffectivelyRenderableEmpty` 过滤整条纯思考消息），故 raw 骨架不可能到理论下限。

对 golden fixture `msg40.json`（reasoning 偏重的真实会话）实测：

| 口径 | 字节 | 占原始 |
|---|---|---:|---:|
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
4. 服务端当前 `SERVER_API_VERSION=2`；`ACCEPTED_CLIENT_VERSIONS=(2,2)` 为闭区间，可由 `OC_SLIMAPI_SERVER_API_VERSION` 与 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=min,max` 配置。
5. 缺头或非整数：`400 {"code":"version_required","accepted":[min,max]}`；区间外：`400 {"code":"version_incompatible","client":v,"accepted":[min,max]}`。
6. `/slimapi/health` 返回 `server:{api_version,accepted_client_versions}`；`/slimapi/ready` 至少返回 `server.api_version`。客户端连接时读取 health 做双向兼容性自检，但 health 本身也必须带版本头。
7. `/slimapi/events` 受同一版本门禁约束；OkHttp SSE 设置 `X-Slimapi-Version`。浏览器 EventSource 若未来需要，可另加 `?version=` query 兜底；本版本不实现。
8. 非 `/slimapi/**` 的 catch-all 反代完全绕过版本门禁，例如无头访问 `/global/health` 仍转发 opencode。
