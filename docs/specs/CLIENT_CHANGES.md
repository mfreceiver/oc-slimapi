> **Aligned with v2-contract.md (lite-v2 cleanup)**
>
> This document reflects the current wire surface. All endpoints not listed
> here have been deleted and return 404. See v2-contract.md for authoritative
> specification.

# ocdroid 客户端改动清单（仅文档，不修改 ocdroid）

## 模型

- `Part` 增加 `hasFull: Boolean? = null`、`omitted: List<String>? = null`。

## 消息加载与展开

- 历史页走 `/slimapi/messages/{sid}?mode=skeleton`，翻页用 `X-Next-Cursor`
  （sidecar 从 opencode `Link` 头透传 opaque cursor，原样回传 `?before=`）。
- 列表排序按 `created` 升序（oldest-first）。
- `mode=full` 参数已删除（静默忽略），始终返回 skeleton 形态。
- `hasFull=true` 的 part 首次展开时请求 `/slimapi/messages/{sid}/full/{mid}`，
  按 `messageId+partId` 替换，禁止追加重复 part。
- **partId 稳定性**：schema-valid 消息下 thin skeleton 的 part `id` 与 `/full/{mid}` 中的 part `id` **跨端点稳定**（真实 `prt_*`）。
- **placeholder → real 对齐（修「展开失败」）**：thin 在无可渲染 part 时仍可能注入 `id=thin_placeholder_{messageID}`。该 id **不会**出现在 `/full`。客户端判定：`partId.startsWith("thin_placeholder_")` → **message-level 整体替换**该 message 的 parts（禁止按 placeholder id 做 part-level lookup / `replaced=false`）。
- **`/full` 剥离 LSP `diagnostics`**：所有 `/full/{mid}` 路径服务端剥掉每个 part 的 `state.metadata.diagnostics`（opencode `edit`/`write` 写入的 LSP 诊断图）。**ocdroid 无需改动**——`Message.kt#parsePartState` 反序列化时本就无条件删除该键、从不消费；此变更对客户端功能零影响，纯下行流量 + parse/heap 节省。其余字段（output/text/files/metadata 其它键）原样保留，`/full`「完整 part」语义不变。`mode=skeleton` 路径不受影响（本就不带 diagnostics）。**唯一需留意**：`/full/{mid}` 现可能返回 **503 `transform_busy` + `Retry-After: 2`**（转换池饱和，与 skeleton 路径同语义）——若 `/full` 调用路径已有 skeleton 的 503 重试兜底，确认 full 路径走同一兜底即可。另：`/full` 响应头改由 sidecar 拥有（`Content-Type: application/json`、按 `Accept-Encoding` 的 `Content-Encoding`、`Vary: Accept-Encoding`；不再透传上游 body-content 头）。
- **第2类仍需 ocdroid**（本仓未改协议 revision 字段）：消息内容变更 watermark（revision / partCount / generation）、token stream idle/resync 不清空唯一可见内容、SSE 开/关统一 reconcile 三分法（空结果 vs 截断 vs 失败）。交接提示词：`docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md`。

### `/full/{mid}` 行为（lite-v2）

- `/slimapi/messages/{sid}/full/{mid}` **始终返回 200**（不再支持 304 Not Modified）。
- 已移除 `X-Message-Event-Seq` 响应头与 `?known.*` 条件参数。
- 已移除 ETag / 条件请求（`If-None-Match` 等被忽略）。
- 每次请求返回完整 `MessageWithParts` 正文。

## sessions 列表完整性头

- `GET /slimapi/sessions` 200 响应头：
  - **`X-Complete`**：`"true"` = 本页 `len < limit`（未满）；`"false"` = 可能截断。**禁止**当「权威全集 / 权威空 / 结束冷启动」——上游无 total、无前向 cursor；`start` 是 epoch-ms 时间戳水位（`time_updated >= start`），**非 offset**。
- 错误路径（502/503）**不**带 `X-Complete` 头。
- **`roots` 默认仍为 `false`**——客户端**应显式传** `roots=true` 以排除 subagent/task（ocdroid 已传）。
- 推荐：`limit=500` 兜底可保留，但用 `X-Complete` 判断是否可能截断，勿盲猜全集。
- 已移除 `X-Discovery-Directories` 与 `X-Discovery-Ready` 响应头（discovery 系统已删除）。

## 路由与失败策略

- thin 使用 stunnel 14097，direct 14096。
- GET circuit breaker：连续 3 次 transport/5xx 后禁 thin 5 分钟，再 half-open。
- mutation 只发一次，不因超时向 direct 重发。

## 错误体形状（thin routes）

- **统一形状**：thin 路由（`/slimapi/sessions`、`/slimapi/messages/**`）的错误体由 FastAPI 默认的 `{"detail":"…"}` 改为：
  ```json
  {"code": "<snake_case_code>", "message"?: "<short human-readable>", ...}
  ```
  例：`{"code":"session_not_found","sessionID":"ses_…"}`、`{"code":"upstream_http_409"}`、`{"code":"upstream_unavailable"}`。
- **`code` 即机器可读判别字段**；客户端错误处理 / circuit breaker 触发 / 用户文案分发应基于 `code`（而非解析 `detail` 字符串）。
- **catch-all（非 `/slimapi/**`）错误体不变**：透传 upstream 原始 body；FastAPI 顶层异常可能仍为 500（无 `code`）。统一错误码表**仅适用于 thin 路由**。
- 客户端应区分：
  - **404 `session_not_found`**（sessions/messages 场景）→ 该 session 已被删除 / 不存在；UI 移除该会话行，**勿**当成可重试的网络错误。
  - **503 `upstream_unavailable`** → upstream 不可达 / 5xx / 坏 JSON；走 circuit breaker + 重试。
  - **502 `upstream_http_N`** → upstream 返非 404 的 4xx；按业务语义处理。
  - **503 `transform_busy`** → 转换池饱和，`Retry-After` 头指示重试间隔。

## SSE

- 连接单一 `GET /slimapi/events`（**无 query 参数**——`directory`/`sessionId`/`stream` 在 v2 重写后已完全移除；全实例、全目录聚合，每事件自带 `directory`）。
- curated 帧类型：
  - `session.digest`（debounce 250ms/session）：`{sessionID,directory,status?,messageID?,updatedAt?,archived?,deleted?,lastError?}`。
    - **`updatedAt`** = sidecar 收到事件时的 wall-clock epoch-ms（非上游时间戳）。
  - **`session.error`（立即直推，无 sid 时）**：`{directory?,name,message,at}`。客户端 UI：有 sid 已含在 digest 的 `lastError`（该 session banner）；无 sid → 全局 toast。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}` / 背压 `{"reason":"subscriber_backpressure"}`，**无 replay**）。
- **`resync` 路径未改**：上游重连/掉线/背压/Last-Event-ID 仍发 `resync`。
- **连接建立期 coalescing**：带 `Last-Event-ID` 重连时同连接可能先 `resync` 再 `server.connected`（既有）。客户端 **SHOULD** 对同一连接建立期 cold-start 触发帧做 once-latch（至多一次 reconcile）。
- **`server.heartbeat` ≠ 上游健康**：仅证 sidecar + 订阅存活；outage 探测用 `/slimapi/ready` 或自然 fetch/write 失败。sidecar 重启后重连收 `server.connected` → **应** cold-start。
- digest `lastError`：sticky 跨窗口，`status=busy` 清除（显式 `null` 帧）；客户端据此显隐 session 错误 banner。`MessageAbortedError` 被 sidecar 过滤，不下发。
- 客户端所有 `/slimapi/**` 请求（含 SSE）须带 `X-Slimapi-Version: 2`；连接时读 `/slimapi/health` 自检（见下 schema 三键）。
- **仍推送帧类型（仅作观察信号）**：`question.asked` / `v2.asked`、`permission.asked` / `resolved` / `v2.asked` / `v2.resolved`——这些帧仍通过 SSE 直推，但 v2 已删除 q/p 写端点与 routeToken；客户端应答 q/p 走 catch-all + `X-Opencode-Directory`（见 v2-contract §2 写路径）。帧的 wire 形态不变。
- **已移除帧类型**：`server.reconfigured`（对应 discovery 数据流整体下线）。

## health schema 回显

- `/slimapi/health` 与 `/slimapi/ready` 的 `schema` 节：`{degraded, version, clientMin, clientMax}`（从服务端 config 读）。
- 旧 `server.api_version` / `server.accepted_client_versions` **保留**。
- **定位**：诊断用 wire 兼容范围回显；**非** feature discovery。

## Token stream SSE（Stages A–E 落地，opt-in 实时流 — design-token-stream.md §9/§10）

> **状态**：服务端 Stages A–E 已落地（A 地基 9.5 / B 生命周期 9.5 / C flush 9.5 / D 端点 9.6 / E 文档+预算 4+4）。未随当前发版出货前 `GET /slimapi/health` 根级 `features.tokenStream` 缺省，ocdroid 走既有「完成后整条出现」路径，**零回归**。本节是 ocdroid 侧改动清单（对应设计 §9 的 8 项 + §10 硬约束），供客户端预读。设计权威以 `docs/specs/design-token-stream.md` 为准；wire 以 `docs/specs/v2-contract.md` §3.x + §6.x 为准。

### capability 探测（必须）

- `/slimapi/health` 根级 **`features.tokenStream === true`** 才启用 stream 客户端；缺字段 / 404 / 405 → 降级为既有「完成后整条出现」（`/slimapi/messages/{sid}` 重拉权威全文），**不得**尝试连 stream 端点。
- 路径与版本头以**本仓库** `docs/specs/v2-contract.md` + `CHANGELOG.md` 为准（端点 `GET /slimapi/sessions/{sid}/stream`，仍带 `X-Slimapi-Version: 2`，**不 bump**）。

### stream 客户端生命周期（必须）

- 前台 opt-in 连 `GET /slimapi/sessions/{sid}/stream`；切后台 / 换 session / 关页面 → **立即断开**（token 订阅独立 T3 账本，预算「同时最多 1 条前台 stream」）。
- 连接独立于控制面 `/slimapi/events`——两条连接，互不替代。
- `Last-Event-ID` 可带但**值被忽略**，仅触发首帧 `resync{reason:"reconnect_no_replay",sessionID}`。stream **不发 SSE `id:`、无 replay buffer**——客户端不得依赖 `id:` 续传。

### streamOwned 渲染算法（必须）

收到帧按 part（`(messageID, partID)`）维护本地「streamOwned」缓冲：

- **`message.part.snapshot{done:false}`**（订阅首帧 / 握手锚点）→ **替换**该 part 本地缓冲为 `text`、标 `streamOwned=true`、未完成。
- **`message.part.delta{text}`** → 仅当该 part 已 `streamOwned` **且未完成**时 **append** `text`；否则丢弃（不应发生；若发生视为乱序，忽略）。
- **`message.part.snapshot{done:true}`**（终态）→ **仅完成 marker，无 text**——客户端**不再从该帧取 text**；标**完成**。权威全文走 `/slimapi/messages/{sid}` skeleton 列表或 `/full/{mid}` 展开（持久化真值，幂等且**凌驾**所有 token 帧）。此后该 part 不再收 delta（违反则忽略）。
- **`/slimapi/messages/{sid}` / `/full/{mid}`**：part 已 `streamOwned` 且**未完成** → **忽略**持久化拉取的该 part text（stream 为准）；part 已 `streamOwned` 且**已完成** → 仅允许 skeleton / full 覆盖（skeleton / full 是持久化真值，幂等且**凌驾**所有 token 帧）。

### truncated / 降级（必须）

- 收 **`message.part.snapshot{truncated:true}`**（`done:false` 或 `done:true` 均可能）→ 清该 part `streamOwned`、停 append、走 `/slimapi/messages/{sid}` 重拉权威（可能被上游截断，但那是真值）。单 part >1MiB **不**走 resync，而是本路径。

### resync 处理（必须）— 两档恢复

收 **`resync{reason, sessionID}`** 时，**一律**先：丢弃该 sid 全部 token 渲染态（所有 streamOwned part 清空）→ `GET /slimapi/messages/{sid}` 重拉权威。是否 **重订阅** stream 按 reason 分档：

| reason | 清态 + 重拉消息 | 重订阅 stream | 说明 |
|---|---|---|---|
| `reconnect_no_replay` | 是 | **是** | 无 replay；新连接拿 handshake snapshot |
| `subscriber_backpressure` | 是 | **是** | 慢消费者被断；须重连 |
| `token_memory_limit` | 是 | **是** | 服务端 LRU 驱逐一个 LivePart 后**保持连接**、**不**对现有 sub 重发 snapshot；仅清态会让后续 delta 成 orphan → **必须**重连以 `attach_subscriber` 重建锚点 |
| `session_idle` | 是 | 否 | 上游 idle，该 sid live parts 已 retire；socket 可留 |
| `session_deleted` | 是 | 否 | 会话终态；eviction 由 `/events` digest 独立驱动 |
| 未知 reason（客户端 fallback） | 是 | 否（建议） | 与 idle 同保守路径；勿静默丢帧 |

- token resync **恒带 `sessionID`**；若极端情况收到无 `sessionID` 的 resync，从**连接**推断 sid（token 流每连接绑单 sid）。
- **不发** `part_too_large`（超限走 `snapshot{truncated:true}`）。

### `message.removed` 帧处理（必须）

- `event: message.removed` payload `{sessionID,messageID}` 可在 token-stream 连接握手期（回放）或运行时（fan-out）收到。收到后应立即丢弃该 message 的所有 live 渲染态（streamOwned parts），该 message 已从上游 opencode 删除，后续 `/slimapi/messages/{sid}` 骨架列表中将不再出现该 message。**控制面 `session.digest` 的 `deleted=true` 是独立信号**，二者互不替代。
- **握手期回放**：`server.connected` → 该 session 未过期 `message.removed` tombstones 按时间先于 snapshot 回放，客户端可在首次 snapshot 到达前清理已删除消息的状态。
- 该帧**不存在**于控制面 `/slimapi/events` 连接中。

### 客户端实现避坑（V2 token-stream，来自 ocdroid 升级实战）

以下两项是 ocdroid V2 升级中踩过并修复的实现坑，**契约 §3.x 已规定正确行为**，此处补充客户端实现要点，帮助其他 V2 客户端避坑。

#### 1. `snapshot{done:true}` 仅是完成 marker，**不得取 text**（契约 §3.x 杠杆1）
- **契约**：终态 `message.part.snapshot{done:true}` 是**仅完成标记，不带 text**——上游 `part.text` 终态重发已被取消；**权威全文走 `/messages/{sid}` 或 `/full/{mid}`**（持久化真值）。
- **坑**：ocdroid D-wire 初版 `TokenStreamReducer` 在 `done:true` 时取 `frame.text ?: existing?.text ?: ""` 作为终态值——与契约冲突。
- **正确**：`done:true` 帧仅用于 ① 标记该 part 渲染完成、② 触发权威 fetch；**不得从该帧取 text**。REST skeleton/full 的全文**凌驾所有 token 帧**（幂等覆盖；客户端可接受 digest 完成先于/晚于 token 终态帧）。

#### 2. `partEventRevision` 必须 **strict `>` 去重**（非 no-op）+ 原子更新 + 生命周期回收（契约 §3.x.2）
- **契约**：token-stream 帧的 `partEventRevision` 由 token hub per-frame 维护（每帧唯一递增）；客户端按 **strict `>`** 去重。
- **坑（ocdroid D-wire 初版两连）**：① 去重函数无条件 `return true`（**根本没去重**，no-op）；② 初版修复用非原子 get/check/put，存在 **TOCTOU 竞态**（并发 delta 帧竞争 last-revision）。
- **正确实现要点**：
  - **原子 compare-and-set** 做 strict `>` 更新（如 `ConcurrentHashMap.compute()` 或等价 CAS），杜绝 TOCTOU；
  - **per-`(sid, mid, pid)` last-revision** 存储（不是全局单一计数器）；
  - **生命周期回收**：part `done:true` / `truncated` / 连接 close / `resync` / session 切换时清对应条目，防泄漏；
  - **有界存储**防内存增长（LRU；ocdroid 用 32 cap 防饥饿）。

### 终态对齐（必须）

- digest `message.updated`（step-finish）→ 客户端应重新拉取 `/slimapi/messages/{sid}` skeleton 列表以获取权威全文，**幂等覆盖**该 message 所有 part（含 token streamOwned 已完成的）。客户端可接受 digest 完成先于 / 晚于 token `snapshot{done:true}`；重拉 skeleton 替换幂等且凌驾所有 token 帧。

### 预算与 UX（建议 / 可选）

- **建议**：同时最多 1 条前台 stream 连接（独立于 `/events`）。
- **可选**（busy-open UX）：打开 busy session 先占位（skeleton / 进度指示），直到 stream 首帧 `snapshot{done:false}` 到达再开始流式渲染。

### 批大小调参：ocdroid 无需配合（§10 硬约束）

- **不需要**任何 ocdroid 侧调整。`TOKEN_FLUSH_SECONDS`(100ms) / `TOKEN_FLUSH_BYTES`(4KiB) 是**服务端 env knob**，不进 wire，服务端可单方面调。
- **硬约束**：渲染须**对任意 batch 稳健**——每帧 `message.part.delta` 当作「待追加文本段」处理（append `text`），**不按 token 计数**、**不假定帧间隔**、**不假定单帧 token 数**。批式参数服务端调，ocdroid 无需跟随改动。
