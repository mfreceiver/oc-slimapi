# oc-slimapi v1 契约（唯一基准）

> **文档修订日志**
>
> | 修订日期 | wire 版本 | 文档 rev | 变更摘要 | 落地对照 |
> |---|---|---|---|---|
> | **2026-07-20** | **1（无 wire 变更）** | **E** | ocdroid §6「slimapi 侧待确认事项」3 项 slimapi 侧源码级确认（纯核验，无行为变更）：(1) messageID 全路径 verbatim 透传（含 SSE digest fan-out：`skeleton.py:142` info deepcopy / `hub.py:415` `message_id=info.id` / `questions.py:60` q/p 不触碰 id）；(2) `Partial+scope.directories==0` 结构不可能（Partial ⇒ ≥1 fetch ⇒ N≥1）、`Success+N==0` 真实可达（冷启动空 allowlist，§2 + 单测锚定），ocdroid N==0 retain-prior gate 设计正确；(3) `/sessions` 最小错误消费深度（log code + rethrow 原始）= 契约正确，slimapi 不要求差异化（codes 为 observability 面，非分支契约）。**无 wire 变更，不 bump**。确认报告：`docs/ocmar/reports/2026-07-20-v0.2.2-ocdroid-handoff-confirmation.md`。 | 本条 changelog（ocdroid §6 反向确认；无 wire 变更） |
> | **2026-07-20** | **1（additive，未 bump）** | **D** | **ocdroid 客户端适配完成并部署 v0.11.5**（ocdroid commit `807ed52` / tag `v0.11.5`，本地未 push）：tuple tie-break `(updatedAt, messageID)` 二元组字典序 watermark（`compareWatermark` @ onReconcileSuccess/needsReconcile/needsCatchUp/reduceSlimDigest 4 站点）+ q/p `scope.directories` 消费（修 N==0 误清 stale；Success **与** Partial 均 N==0 retain）+ `/sessions` 列表 coded-error observability（`parseErrorCode`→internal + log + rethrow 原始，不引入新异常类型）+ directory 客户端 normalize-dedup（`normalizeDirectory` + fan-out + onResync）。**纯客户端，无 wire 变更**；ocdroid final whole-branch review APPROVED (0C/0I) + fresh verifier EXIT=0/FAILURES=0（live rerun）。**F-1**「`/since` 真过滤 + tie-break」runtime 联调待确认（loopback `127.0.0.1:4097` / mTLS `opencode.vectory.cn:14097`）。ocdroid 报告：其仓 `docs/ocmar/reports/2026-07-20-slimapi-v022-client-adapt.md`。 | 本条 changelog（ocdroid 侧落地确认；无 wire 变更） |
> | **2026-07-20** | **1（additive，未 bump）** | **C** | ocdroid 契约遗留缺口 ratify（3 缺口 + 2 个 pre-existing 真 bug）：**Gap1** `/since/{ts}` 时间过滤 no-op 修复（`info.time.updated` 在 v1.18.3 不存在 → 改读 `updated or created`，与 digest `updatedAt` 对齐）+ 等时间戳 tie-break 规则 `(updatedAt, messageID)` 二元组字典序；**Gap2** `/slimapi/sessions` 列表 §7 偏离修复（原样透传 → `_raise_upstream_status`）+ q/p envelope 加 `scope.directories` 区分「scope 未就绪 / 权威空」；**Gap3** `/since/0` cursor drain 推荐。200 tests green。详见 §14.6。 | §5 / §2 / §7 / §12 / §14.6 |
> | **2026-07-20** | **1（additive，未 bump）** | **B** | ocdroid《slimapi 接口评审报告》§3–§6 原始发现 **F1–F5 + §5 文档重构** 全部落地；本仓审计扩展 **G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步）** 一并实现；另修 2 个 pre-existing SSE 生命周期 bug（teardown 计数泄漏 / queued_bytes 不扣账）+ G1 `error.name` 类型防御。全加性，`X-Slimapi-Version` 仍为 `1`。190 tests green（双独立门控 PASS）。逐条对照见 **§14**。 | §14 |
> | 2026-07-19 | 1（additive） | —（并入 B） | F1–F5/§5/G1/G6/D1–D8 实现提交日（同一批次，文档 rev B 统一收录）。 | §14 |
> | 2026-07-18 | 1（B1） | A | 初始收敛版（A1-A3 / B1-B3 / C1-C2 全定，A2=A 时间戳锚点）；thin 路由错误体 `{"code":...}`、G2 status 404-502-503 分裂、projects 5xx 502→503、新增 8 个错误码。详见 §7。 | — |
>
> **基准声明**：本文件是 wire 基准。实现侧加性/修复性变更在对应小节就地标注，并在本头部「文档修订日志」汇总。所有变更**不 bump `X-Slimapi-Version`** 除非另行说明。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。
>
> **同步纪律**：本文件 changelog 条目须同时列出受影响的 `docs/CLIENT_CHANGES.md` 小节。
>
> **详细变更内容（按修订日）**：
> - **2026-07-20 / 2026-07-19 · v1（additive）**：`/slimapi/questions`+`/permissions` 的 `directory` 改可选（null=聚合 allowlist，**F1**）；`/slimapi/sessions/{sid}/status` 放宽 allowlist（sid 自洽，**F2**）；sidecar 启动主动 warm `/project` 暖 allowlist（**F3a**）；routeToken 应答路径 allowlist miss 自动刷新（**F3b**）；`CLIENT_CHANGES.md` SSE 节同步（**F4**）；§1 `accepted:[1,1]` 闭区间说明（**F5**）；新增 §12 directory 三态语义表 + §13 allowlist 机制（**§5**）；**G1** `session.digest` 加 `lastError?` 三态字段 + 新 `event: session.error` session-less 帧 + 脱敏算法；**G6** `GET /slimapi/messages/{sid}/full?ids=` 批量展开端点（envelope + mid 级部分失败 + chunk-ledger 累计预算）；D1–D8 文档同步（§11 标 closed）。受影响 CLIENT_CHANGES：SSE（`lastError` / `session.error`）、消息批量展开、q/p null directory、cold-start 顺序、错误体形状。
> - **2026-07-18 · v1 B1（additive）**：`session_not_found`(404) / 顶层 `upstream_unavailable`(503) / 顶层 `upstream_http_N`(502) / `shell_not_allowed`(403) / `invalid_directory_count`(400) / `invalid_route_token`(400) / thin 路由错误体 `{"code":...}`（非 `{"detail":...}`） / G2 status 404-502-503 分裂 / projects 5xx 502→503。详见 §7。受影响 CLIENT_CHANGES：错误体形状。

> 状态：契约收敛版（A1-A3/B1-B3/C1-C2 全定，A2=A 时间戳锚点；rev B F1–F5/§5/G1/G6/D1–D8；rev C Gap1–3 ratify；rev D ocdroid 客户端适配完成 + 部署 v0.11.5；**rev E ocdroid §6 三项 slimapi 侧确认（messageID 透传 / Partial+N==0 / sessions 错误深度），F-1 runtime 联调待确认**）。配套原型与正式实现已覆盖；本文 🔒=已覆盖、🆕=历史缺口标注（§11 已闭环）。
> 权威性：本文件是正式实现的唯一基准。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。

## §0 范围与架构
- 纯 HTTP sidecar：FastAPI + httpx + orjson + uvicorn **单 worker**，loopback，stunnel mTLS 后。
- **不读 opencode SQLite**；仅 legacy `/session` API。
- v1 目标：**2-5 台同用户设备**（T3 硬化进 v1）。
- 客户端通过"切换服务器"进省流（R8：`mtls×slim` 两布尔→4 配置），非连接属性开关。

## §1 版本契约 🔒
- 头 `X-Slimapi-Version: <int>`，所有 `/slimapi/**` 必带。
- 门闩 `ACCEPTED_CLIENT_VERSIONS=(1,1)`：`accepted:[1,1]` 是闭区间 `[min,max]`，当前 `min=max=1`（仅接受整数 `1`）。缺/非整数→400 `version_required`；越界→400 `version_incompatible`（带 `client`/`accepted`）。
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
| GET | `/slimapi/sessions/status` | A | 🔒 | 批量 status（`?directory` 必填，∈allowlist） |
| GET | `/slimapi/sessions/{sid}/status` | A | 🔒 | 单 ses status（id→directory 自洽；**sid 为能力凭证，不受 allowlist 约束**） |
| GET | `/slimapi/messages/{sid}` | A | 🔒 | 骨架分页（`?limit/before/mode`） |
| GET | `/slimapi/messages/{sid}/since/{ts}` | A | 🔒 (语义 🆕；rev C 勘误) | **A2=A**：`(info.time.updated or info.time.created) >= ts` 的骨架（rev C：v1.18.3 无 message 级 `time.updated`，实读 `created`，与 digest `updatedAt` 同源）；`?limit/before` 分页；等时间戳 tie-break 见 §5 |
| GET | `/slimapi/messages/{sid}/full/{mid}` | A | 🔒 | 单条全文（mode=full，展开某条） |
| GET | `/slimapi/messages/{sid}/full?ids=` | A | 🔒 (G6 🆕) | 批量展开（1–20 mid，discover 先行，mid 级 envelope `errors[]`，累计 413） |
| GET | `/slimapi/questions` | A | 🔒 | 跨目录聚合 pending（`?directory` repeated 1-32 **可选**；null=聚合 allowlist 全部 dir），每条带 `routeToken`；200 envelope `{items,errors,scope:{directories:N}}`（rev C：N=有效 scope dir 数，区分 scope 未就绪/权威空，见 §12） |
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
- routeToken 应答路径：token 校验后走 `require_directory`（allowlist miss 自动刷新一次；仍 miss→400；刷新失败→503）。

### G6 批量展开（`GET /slimapi/messages/{sid}/full?ids=`）🔒
- **参数**：`ids` 必填（逗号分隔 mid，缺失→422 FastAPI）；解析后去重保序，长度 1–20，否则 400 `invalid_ids`；不校验 mid 字符集。可选 `mode=skeleton|full`（默认 full）、`directory`（G7-soft，见 §12）。
- **discover 先行**（top-level；**不拉任何 mid** 直至 discover 成功）：`GET /session/{sid}`（带 directory 头）→ 404→404 `session_not_found`；其它 4xx→502 `upstream_http_N`；5xx / 网络 / discover body 坏 JSON→503 `upstream_unavailable`。
- **mid 级 envelope**（无整请求 terminal 时 **HTTP 200**）：`{"items":[...],"errors":[{"messageID","code"},...]}`。
  - mid 2xx 且 body 可解析 → `items[]`。
  - mid 404 → `errors[]` `message_not_found`。
  - mid **≥400（含 4xx 与 5xx）** → `errors[]` `upstream_http_N`（**不**升级为整请求 5xx/502；整请求仍 200）。
  - mid body 超 `max_message_bytes` → `errors[]` `message_too_large`。
  - mid 2xx 但 body 不可解析（坏 JSON）→ `errors[]` `upstream_error`（envelope，非整请求 500）。
  - **全 mid 404 / 全 mid 失败仍 200 + 全 errors**（只要无整请求 terminal）。
- **整请求 terminal**（覆盖 envelope）：
  - 累计解码体超 `max_response_bytes` → 413 `response_too_large`，中止后续 mid。
  - 任一 mid `httpx.RequestError`（网络 / 流中断）→ **503 `upstream_unavailable`**；**503 优先于 413**（网络失败与累计超限同时成立时返 503）。
  - skeleton 模式 transform pool 饱和 → 503 `transform_busy`（`Retry-After`）。
- **定序**：`items[]` 顺序 = `ids` 去重后序（保证）；`errors[]` 顺序 = 并发完成序（**不保证**，客户端不得依赖）。
- **路由**：`/full` 注册先于 `/full/{mid}`。

## §3 SSE 契约（简化版，A1-A3 落定）🔒 + archived 🆕 + G1 🆕
- 上游：**一条** `/global/event`（进程级 GlobalBus，全实例跨目录，每事件自带 `directory`）。
- 帧：
  - `session.digest`（debounce 250ms/session，仅发有变化的字段）：`{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?}`。
    - `status`←`session.status`(idle/busy)；`messageID`+`updatedAt`←`message.updated`/`message.appended`（info.id + info.time.updated/created，取最新）；**`archived`←`session.updated` 的 `info.time.archived`（有值→epoch-ms 时间戳）** 🆕；`deleted`←`session.deleted`。
    - **`lastError`（G1-A）**←`session.error` 经脱敏后的 `{name,message,at}`（`at`=sidecar 收到时 epoch-ms）。**三态 wire**（与 sticky 共存，互不矛盾）：
      - **对象** `{name,message,at}`：本窗口新 error，或 flush 时该 sid 仍有 sticky（其它字段触发的后续 digest 会继续带出对象，直至 clear/deleted）。
      - **显式 `null`**：clear 帧——该 session 出现新 `status=busy` 时 pop sticky 并立即 flush。
      - **省略**：本 digest 没有本窗口新 error 对象、也没有显式 clear（`null`），**且** 该 sid 当前不存在 sticky error；`deleted=true` 的 digest **强制省略**（pop sticky，**不**发 null）。
      - abort（`error.name=="MessageAbortedError"`）静默丢弃（不写 lastError、不发 G1-B 帧）。
      - 脱敏：`message` 取首行→剥绝对路径→剥 stack frame→剥 secret→截断 ≤512；缺失回落 `name` 或 `"(no detail)"`；`name` 截断 ≤128。
  - `session.error`（G1-B，**无** `sessionID` 时立即直推，不走 debounce）：`{directory?, name, message, at}`。abort（`MessageAbortedError`）静默丢弃。有 sid 的 `session.error` **不**走本帧，走 digest `lastError`（G1-A 立即 flush）。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`：**立即直推** `{directory, type, properties}`。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}`，无 replay）。
- 丢弃：`?stream`、text.delta、`message.part.*`、`tool.*`、`sessionId` 参数、per-directory hub。

## §4 冷启动 & resync（A1 + A3）🔒
- **sidecar 启动暖机**：lifespan 在 smoke 后 best-effort 调一次 `/project` 预热 allowlist（`warm_allowlist`；失败仅吞错，不阻断启动；lazy `require_directory` 刷新仍为回退）。
- **客户端冷启动顺序**：
  1. 可选 `GET /slimapi/projects`（显式刷新 allowlist / 发现 project）；
  2. `GET /slimapi/sessions`（`directory` null OK，不过滤）；
  3. `GET /slimapi/questions` + `/permissions`（`directory` **可选**；null=聚合 allowlist 全部 dir；空 allowlist→200 空 envelope）；
  4. 当前打开 ses：`GET /slimapi/messages/{sid}/since/{ts}`。
- 之后 SSE 接力增量。
- **resync = 复用冷启动流程**（同一"加载初始状态"代码路径）。

## §5 拉消息（A2=A 锁定）🔒 (语义 🆕；rev C 勘误 + tie-break + cursor drain)
- digest 推 `{messageID, updatedAt}`；客户端记本地该 ses 的 **watermark = `(updatedAt, messageID)` 二元组**（见下 tie-break）。
- 比对发现更新 → 拉 `/slimapi/messages/{sid}/since/{lastSeenUpdatedAt}`。
- 服务端返回 **`(info.time.updated or info.time.created) >= lastSeenUpdatedAt`** 的骨架；客户端按 messageID 去重边界。
  - **勘误（rev C）**：原述 `time.updated >= ts` 引用了 opencode v1.18.3 **不存在**的 message 级 `info.time.updated`（仅 `SessionInfo.time` 有 `updated`；message 级 `User.time={created}`、`Assistant.time={created,completed?}`）。服务端实际过滤键 = `info.time.updated or info.time.created`，与 digest `updatedAt` 推导（§3：`updated or created or now`）**去掉 now 兜底**一致；v1.18.3 下两者都解析到 `info.time.created`。修复前该过滤是 no-op（已修，见 CHANGELOG `[0.2.1]`）。
- **等时间戳 tie-break**（opencode 不保证 per-session `time` 严格单调——同毫秒批量 = 同时间戳；上游 `orderBy(desc(time_created), desc(id))` 显式以 `id` 为次键）：客户端 watermark 必须用 **`(updatedAt, messageID)` 二元组字典序**比较：
  1. 先 strict 比 `updatedAt`（严格 `>` 才推进时间维）；
  2. 时间相等时 strict 比 `messageID`（`MessageID = msg_+ascending()` 单调递增、字典序可排序 → 新消息 id 字典序更大 → 严格 `>` 才推进 id 维）。
  - 此规则与上游 `(time_created DESC, id DESC)` + cursor `older()` 全序**完全对齐**，复用上游单调 `MessageID` 作天然 tie-break，零契约创新。
- 分页：`?limit` + `?before` 游标。
- **无 watermark 的初始拉取推荐 cursor drain（`?before` 游标分页）而非 `/since/0`**（rev C，ocdroid 缺口 3 裁定）：focus digest + resync 统一走 cursor drain 共享 reconciler；`/since/{ts}` 语义为"基于 watermark 的增量过滤"，`ts=0` 虽合法（返回全部）但非推荐路径。
- 全文：单条 `/full/{mid}`；批量 `/full?ids=`（§2 G6）。

## §6 资源限制（T3，C2=2-5 台进 v1）🆕
- `MAX_SUBSCRIBERS_PER_DIRECTORY=8`、`MAX_TOTAL_SUBSCRIBERS=16`。
- 每 subscriber buffer `2 MiB`、单帧 `256 KiB`；溢出→**立即清 queue/deltas/dirty** + 排 `resync{reason:subscriber_backpressure}` + STOP（替代当前"queue 尾排 STOP 继续发旧帧"）。
- admission 在 `HubRegistry.subscribe` 单一无 await 临界段；超限→503 `sse_subscriber_limit_directory`/`_total`（带 `limit`/`current`/`Retry-After`）。
- 转换池（fix-9 🔒）：`MAX_TRANSFORMS=1`，admission 在下载前，限长读 `MAX_RESPONSE_BYTES=64MiB`，parse/project/gzip offload worker thread。

## §7 错误码 🔒 + 🆕 (additive, no X-Slimapi-Version bump)

> v1 B1（2026-07-18）扩充：thin 路由错误体由 FastAPI 默认 `{"detail":…}` 改为 `{"code":…}`，并新增以下 code；均为加性、不 bump `X-Slimapi-Version`。详见 `docs/v1-impl-spec.md` §11 + `docs/CLIENT_CHANGES.md`「错误体形状」。
> 2026-07-19 加性：G6 `invalid_ids` / envelope `message_not_found` / envelope `upstream_error`（mid 坏 JSON）；F2 收窄 `directory_not_allowed` 适用范围。
> 2026-07-20 加性（rev C）：`GET /slimapi/sessions` 列表端点失败路径对齐 §7（原静默偏离：upstream 4xx/5xx 原样透传 body、网络错落 FastAPI 默认 `{"detail":...}` 500；现统一 4xx→502 `upstream_http_N`、5xx/网络→503 `upstream_unavailable`，body 为 `{"code":...}`）。
>
> **top-level vs envelope**：下列 code 默认指 thin 路由 **HTTP 状态 + body `{"code":…}`**。G6 另有 **envelope 语境**（整请求通常仍 200，code 出现在 `errors[]` 的 mid 项）。**同一 code 名两语境含义不同**，见各条标注。

- 400 `version_required` / `version_incompatible` / `directory_not_allowed` / `invalid_directory_count` / `invalid_route_token` / **`invalid_ids`**（G6 top-level：`ids` 空 / 超 20 / 解析后无有效 mid）
  - **`directory_not_allowed` 适用范围**：sessions 显式 `?directory=` / 批量 `GET /sessions/status` / q/p **显式** directory / messages G7-soft（query 非法或 query≠header）/ routeToken 刷新后仍 miss。**不再适用** per-session `GET /sessions/{sid}/status`（F2：sid 自洽，仅 normalize）。
- 403 `shell_not_allowed`（catch-all shell/PTY deny-list；ops 可关，非安全保证）
- 404 `session_not_found`（`GET /slimapi/sessions/{sid}/status` 与 G6 **discover** 的 upstream 404；top-level，带 `sessionID`）；`thin_route_not_found`
  - **`message_not_found`**：**仅 G6 envelope** mid 级 code（HTTP 仍 200；**非整请求 404**）
- 413 `response_too_large`（top-level：超 `MAX_RESPONSE_BYTES`；含 G6 累计）
  - **`message_too_large`**：**top-level** 于 `GET .../full/{mid}?mode=full`（单条流式 cap→413）；**G6 envelope** 于 mid body 超 `max_message_bytes`（整请求仍 200）
- 502 `upstream_http_N`
  - **top-level**：G2 status / projects / G6 **discover** 等对 upstream **非 404 的 4xx** → 502（discover 5xx 走 503，见上）
  - **G6 envelope**：mid **≥400（含 5xx）** → `errors[]` `upstream_http_N`，**整请求仍 200**（mid 5xx **不**升级为整请求 5xx）
- 503 `transform_busy`（`Retry-After`；含 G6 skeleton pool 饱和）/ `upstream_unavailable`（含 G6：discover 5xx·网络·坏 JSON；**任一 mid 网络失败**——且 **优先于** 累计 413）/ allowlist 刷新失败 / `sse_subscriber_limit_*` 🆕
- **`upstream_error`**：**G6 envelope** mid 2xx body 不可解析（坏 JSON）；亦见 q/p fan-out 单 dir 失败项。非整请求 500。
- 504 `upstream_timeout`（q/p mutation）
- thin 路由错误体统一：`{"code":string, "message"?:string, ...}`（非 `{"detail":...}`）
- FastAPI 参数缺失/类型错误仍为 422（如 G6 缺 `ids`）

## §8 客户端 v1 最小集（C1，暂停 — ocdroid）
连接(R8)+版本头+health 自检(M2/fail-closed)+冷启动(sessions+q/p 快照，见 §4)+SSE(digest+q/p+`lastError`/`session.error`)+digest 触发拉消息(`/since`)+全文(`/full/{mid}` 或 G6 batch)+发消息(X-Opencode-Directory 透传)+q/p 应答(routeToken)+resync=冷启动。**+ C3 health 改 `/slimapi/health`（fix-7 已落地）**。

## §9 gzip 🆕（小修）
所有 JSON 路由的 `json_response` 调用转发 `accept_encoding=request.headers.get("accept-encoding")`。sessions/questions 已做；health/ready 等补齐。

## §10 延后（非 v1）
skeleton 共享缓存（YAGNI，先指标）、多用户（独立 stack）、Part 展开 UI、sessions status 迁移、circuit breaker、metrics 之外的可观测。

## §11 v1 待补缺口清单（已闭环）
1. ✅ 已闭环（`docs/v1-contract-implementation-status.md` §11）：hub.py digest `archived` 字段（§3）。
2. ✅ 已闭环（`docs/v1-contract-implementation-status.md` §11）：messages.py `/since` 语义 `(info.time.updated or info.time.created) >= ts` + `limit/before` 分页（§5；rev C 勘误：原述 `time.updated` 在 v1.18.3 不存在）。
3. ✅ 已闭环（`docs/v1-contract-implementation-status.md` §11）：T3 硬化：订阅上限 + buffer 字节预算 + 立即清式溢出 + `/slimapi/metrics`（§6）。
4. ✅ 已闭环（`docs/v1-contract-implementation-status.md` §11）：gzip 清理：health/ready 等 JSON 路由转发 accept_encoding（§9）。
5. ✅ 已闭环（`docs/v1-contract-implementation-status.md` §11）：核验（非实现）：发消息写路径经 catch-all + 客户端 `X-Opencode-Directory` 是否端到端 work（opencode `/session/{sid}/message` 是否认该头）。

## §12 directory 三态语义表

跨端点对 query `directory` 的统一语义（**null = 未传 / 空列表语义按端点**；规范化 = 去尾斜杠，根保留 `/`）。

| 端点 | null / 未传 | 显式且 ∈ allowlist | 显式且 ∉ allowlist（刷新后） |
|---|---|---|---|
| `GET /slimapi/sessions` | 200，不过滤（upstream 默认） | 透传 `?directory=` + header | 400 `directory_not_allowed` |
| `GET /slimapi/sessions/status` | **必填**（缺→422） | 透传批量 status map | 400 `directory_not_allowed` |
| `GET /slimapi/sessions/{sid}/status` | **无** directory 参数；discover sid→directory，**仅 normalize，不 gate allowlist**（F2） | — | —（不因 allowlist 400） |
| `GET /slimapi/questions` / `/permissions` | **可选**；null=聚合 allowlist **全部** dir（不受 1–32 守卫）；空 allowlist→200 `{"items":[],"errors":[],"scope":{"directories":0}}`（F1；`scope.directories` 区分 scope 未就绪/权威空，见 §2） | 去重后 1–32 项 fan-out；每条带 `routeToken` | 任一项 miss→400 `directory_not_allowed`；0 或 >32→400 `invalid_directory_count` |
| `GET /slimapi/messages/**`（含 G6 `/full?ids=`） | **不拦**（G7-soft；upstream 默认） | `require_directory` 后作 `X-Opencode-Directory` | 400；query 与 header 冲突亦 400 |
| `POST` q/p reply/reject/permission | directory 来自 routeToken，不接受 body directory | token 校验后 `require_directory`（miss 自动刷新，F3） | 刷新后仍 miss→400 |

说明：
- **sid 能力凭证**：客户端仅从 list / SSE / routeToken 合法渠道获知 sid；per-session status 与 messages 无 query 路径对齐，不把 allowlist 当安全边界。
- **null 聚合（q/p）**：fan-out 规模 = allowlist 大小（ops 经 opencode project 列表控制），**不**受 1–32 客户端列表守卫约束。

## §13 allowlist 机制

- **用途**：约束客户端**显式**声明的 directory（写应答、批量 status、sessions 过滤、messages 显式 query、q/p 显式列表），防止误指向未知路径。**不是**多租户隔离边界——隔离靠 stunnel mTLS + loopback 网络边界。
- **构建**：`load_products(app)` → `GET /project` + 并发 `GET /project/{id}/directories`；allowlist = 各 project 的 `worktree` ∪ `directories[].path|directory`，经 `normalize_directory`（去尾斜杠，根 `/`）后写入 `app.state.directory_allowlist: set[str]`。
- **启动暖机（F3）**：lifespan smoke 后 `warm_allowlist(app)` best-effort 调一次 `load_products`；upstream 失败吞掉，不阻断启动。
- **按需刷新**：`require_directory(request, directory)`：
  1. normalize；
  2. miss → 尝试 `load_products` 刷新一次；
  3. 刷新抛错 → 503 `upstream_unavailable`（`message: cannot refresh directory allowlist`）；
  4. 刷新后仍 miss → 400 `directory_not_allowed`；
  5. hit → 返回 normalized 字符串。
- **routeToken 路径（F3）**：`_token` 校验 HMAC 后 `await require_directory(...)`，故冷启动空 allowlist 时合法 token 的 dir 可自动刷新成功；不可发现 dir 仍 400。
- **显式发现**：`GET /slimapi/projects` 始终走完整 `load_products` 并更新 allowlist（失败见 §7 502/503 分裂）。
- **不 gate 的路径**：`GET /sessions/{sid}/status`（F2）；messages **未传** query `directory`；q/p **未传** directory（改走 allowlist 聚合，见 §12）。

## §14 ocdroid 评审要求落地对照（2026-07-20）

> 本节对照 ocdroid《slimapi 接口评审报告》§3–§6 的**原始发现**逐条回应落地情况。**F1–F5 + §5** = ocdroid 原始要求；**G1 / G6 / D1–D8** = 本仓 v1 文档遗留审计发现（经用户确认纳入同批实现，非 ocdroid 原始 ask，一并列出便于全景追溯）。
>
> **原始报告来源**：ocdroid 仓库 `.ocmar/workflows/slimapi-client-v1/`（接口评审 / B1 兼容审计报告 §3–§6，F1–F5 + §5 文档建议）。本表据该报告发现 + 本仓 spec `docs/ocmar/specs/2026-07-19-ocdroid-findings-evaluation-design.md` §3（逐条核验源码）落地。
>
> **验证总览**：`./scripts/check.sh` → **200 passed, EXIT=0**（v0.2.0 = 190；v0.2.1 rev C 缺口 ratify +10）。final rev-cgpt 整审 + SSE 验证双门控 PASS；rev C 再经 rev-cgpt review（必修项已闭环）。全加性，`X-Slimapi-Version` 仍为 `1`。

### 14.1 ocdroid 原始要求（F1–F5 + §5）

| # | ocdroid 原始发现 | 落地动作 | 状态 | 契约落点 | 实现证据（测试 / 文档） |
|---|---|---|---|---|---|
| **F1** | `/questions`+`/permissions` 的 `directory` 必填，cold-start 不传 → 422 | `directory` 改 `Query(None)` 可选；null = 聚合 allowlist 全部 dir（不受 1–32 守卫）；空 allowlist → 200 空 envelope | ✅ 已落地 | §2 端点表 / §12 三态表 / §4 cold-start | `test_questions_null_directory_aggregates_allowlist`、`test_questions_null_directory_empty_allowlist_returns_empty_envelope` |
| **F2** | per-session `/sessions/{sid}/status` 对非白名单 dir 的 sid 返 400（listed-but-rejected，与 messages soft 的同 sid 200 不一致） | 放宽：`session_status` 改用 `normalize_directory`（仅规范化，不 gate allowlist）；sid 自洽即能力凭证。批量 status 行为不变 | ✅ 已落地 | §2 / §7 `directory_not_allowed` 适用范围 / §12 / §13 | `test_status_allowlist_miss_relaxed_returns_status`；`test_batch_status_allowlist_miss_renders_code` 仍 PASS |
| **F3** | routeToken 应答路径在冷启动空 allowlist 时，合法 token 的 dir 也被 400（`_token` 直接查空 set 不刷新） | (a) `_token` 改 async + `await require_directory`（miss 自动刷新一次）；(b) lifespan `warm_allowlist` 启动暖机（best-effort 吞错） | ✅ 已落地 | §4 cold-start / §13 allowlist 机制 | `test_token_refreshes_cold_allowlist_then_reply`（204）、`test_warm_allowlist_swallows_upstream_error`、`test_questions_token_directory_not_allowed`（不可发现仍 400） |
| **F4** | `CLIENT_CHANGES.md` SSE 节写 `?directory=...&sessionId=...&stream=...`，与 INTERFACE_MAP §3「参数完全移除」矛盾 | SSE 节整节重写：单一 `/slimapi/events` 无 query 参数；curated 帧类型含新增 `session.error`；同步 `lastError` 语义 | ✅ 已落地 | §3 SSE 契约（CLIENT_CHANGES 同步纪律） | `docs/CLIENT_CHANGES.md` SSE 节不再出现过期 query 参数 |
| **F5** | 响应体 `accepted:[1,1]` 未标注是闭区间，客户端可能误解为单值或枚举 | §1 加闭区间说明：`accepted:[1,1]` 是 `[min,max]`，当前 `min=max=1`（仅接受整数 `1`） | ✅ 已落地 | §1 版本契约 | contract §1 第二段 |
| **§5** | 文档结构建议：directory 跨端点语义不一、allowlist 机制分散、CLIENT_CHANGES 易漂移 | (1) 新增 §12 directory 三态语义表（跨 6 端点）；(2) 新增 §13 allowlist 机制独立节；(3) 头部加 CLIENT_CHANGES 同步纪律；(4) §4 cold-start 顺序写明含暖机 | ✅ 已落地 | §4 / §12 / §13 + 头部同步纪律 | cross-doc rev-cgpt 一致性评审 PASS |

### 14.2 本仓审计扩展（G1 / G6 / D1–D8，非 ocdroid 原始 ask）

| # | 项 | 最初问题 | 落地动作 | 状态 | 契约落点 |
|---|---|---|---|---|---|
| **G1** | 错误可见性 | impl-spec §7 承诺的 `session.error` → digest `lastError` + session-less 帧未实现；hub.py catch-all 丢弃 `session.error`；客户端拿不到任何错误可见性 | `digest.lastError` 三态（对象 / 显式 null clear / 省略）+ sticky 跨窗口 + busy clear + deleted 清除 + abort 过滤 + 新 `event: session.error` session-less 帧 + 脱敏算法（首行/剥路径/剥 stack/剥 secret/截断 512） | ✅ 已落地 | §3 SSE 契约（digest `lastError?` + `session.error` 帧） |
| **G6** | 批量展开 | impl-spec §8 承诺的 batch 端点未实现；ocdroid 调 `/full?ids=` → 404 → N 并行 `/full/{mid}` 回退 | 新端点 `GET /slimapi/messages/{sid}/full?ids=`：discover 先行 + mid 级 envelope `errors[]` + chunk-ledger 累计字节预算（同步扣账）+ 503 优先 413 + 路由注册先于 `/full/{mid}` | ✅ 已落地 | §2 端点表 G6 行 + §2「G6 批量展开」节 + §7 错误码 |
| **D1–D8** | 文档同步 | design-v2（limit 400→实 422 / q/p 必填 / status 503 / SSEClient 路径 / thin.session.dirty）、impl-spec（B0 决策 pending / G1·G6 未标）、AGENTS（对齐版本 v1.17.20≠实链 v1.18.3）、contract §11 待补缺口仍带 🆕 | D1–D5 design-v2 各节修正；D6 §11 全标 closed；D7 B0 决策记录 GO + G1·G6 标已实现；D8 AGENTS 对齐 v1.18.3 | ✅ 已落地 | §11 + 配套 design-v2 / v1-impl-spec / AGENTS.md |

### 14.3 终审扩展（pre-existing 生产 bug，用户批准一并修）

| 项 | 最初问题 | 落地动作 | 状态 | 证据 |
|---|---|---|---|---|
| 🔴 SSE teardown 计数泄漏 | `events.py` generator finally 调 `GlobalHub.unsubscribe`（只删 subscribers set，不减 `HubRegistry.total_subscribers`）→ 达订阅上限后永久 503 | 改走 `request.app.state.hubs.unsubscribe(subscriber)`（registry 级，减计数 + 末订阅触发 hub removal grace） | ✅ 已落地 | `test_events_teardown_releases_registry_slot`（5 轮 connect/aclose，`total_subscribers` 回落 0） |
| 🔴 `queued_bytes` 消费不扣账 | `Subscriber.put` 入队增字节，但消费端无对称扣减 → 健康消费者累计误判背压 → 误触 `subscriber_backpressure` resync | 新增 `Subscriber.ack()`（镜像 put 的 size 计算，STOP 不计），`events.py` 消费时扣；overflow `_clear_queue` 重置 | ✅ 已落地 | `test_subscriber_queued_bytes_decrements_on_consume` |
| 🟠 G1 `error.name` 类型防御 | `publish()` 对 truthy 非字符串 name（dict/int）执行切片 → TypeError 逃出 → SSE run/reconnect 扰动 | coerce 非字符串 name → None（`(name or "")[:128]` 前置 isinstance 检查） | ✅ 已落地 | `test_g1_publish_non_string_error_name_does_not_crash` |
| — config 硬约束 | overflow 的 resync + STOP 需同时入队，`sse_queue_items=1` 会丢 | 启动校验 `sse_queue_items >= 2`，否则拒绝 | ✅ 已落地 | config `queue_items=1` 拒绝 / `=2` 接受 测试 |

### 14.4 非阻塞保留项（诚实声明，非任何原始要求的验收条件）

以下为执行中识别的边界，**不在** ocdroid 原始要求 / 本批 plan 验收条件内，已记入最终报告 §5 供后续关注：

- G1 脱敏 regex 边缘（Bearer-no-space / 自然语言 stack 误剥 / Unicode path 带空格）— defense-in-depth on loopback，非主安全边界。
- G6 mid body 形状错误（合法 JSON 但非 MessageWithParts）未 envelope 映射（保持 500）；仅 JSON 解析错映射 `upstream_error`。
- G1 deleted flush 后迟到 `session.error` 可能重建 entry（无 durable tombstone）。
- G6 真实 HTTP streaming 早停 / 取消（MockTransport 未完全证明 chunk-ledger 行为）。
- rev-13 🟡 维护项：端到端 events-body-iterator ack 测试增强 / `test_hub.py:369` 滞后注释。

### 14.5 一句话结论

> ocdroid 原始评审 F1–F5 + §5 **全部落地**；本仓扩展 G1 / G6 / D1–D8 **全部落地**；终审发现的 2 个 pre-existing SSE 生产 bug + G1 类型防御 **一并修复**。190 tests green（双独立门控），全加性不 bump wire。

### 14.6 ocdroid 契约遗留缺口 ratify（2026-07-20 rev C，v0.2.1）

> 来源：ocdroid 对 rev B（commit `22ddc3a`）的核对（ocdroid `.ocmar/workflows/slimapi-client-v1/problem-report-wip.md`，oracle T11 设计审查 D5/D2/I2）。F1–F5/§5/G1/G6/D1–D8 已在 14.1–14.4 核实；本节为**仍未被契约 ratify** 的 3 个开放项 + 查证中发现的 2 个 pre-existing 真 bug + 2 处防御缺口。

| # | 缺口 | 查证结论（explorer 源码核验） | 落地动作 | 状态 | 契约落点 |
|---|---|---|---|---|---|
| **Gap 1** | 等时间戳不同 messageId 的 tie-break 未定义 | opencode **不保证** per-session `time` 严格单调（同毫秒批量=同时间戳；上游 `messages-pagination.test.ts:258-272` 显式证）；`MessageID`=`msg_+ascending()` **严格单调**；上游 `orderBy(desc(time_created), desc(id))` + cursor `older()` 已是 `(time,id)` 全序 | tie-break = **`(updatedAt, messageID)` 二元组字典序**（复用上游单调 id，零契约创新）；**另发现** §5 引用的 `info.time.updated` 在 v1.18.3 **不存在**（message 级只有 `created`）→ `/since` 过滤 no-op，修 `_item_updated` 读 `updated or created` | ✅ 已落地 | §5（勘误 + tie-break） / §2 /since 行 |
| **Gap 2** | cold-start "成功空" vs "失败" wire 层不可区分 | **q/p**：缺口**不成立**（200 空 vs 503 全失败 vs 200+非空 `errors[]`，已可区分；是客户端未按 §7 处理）；**`/sessions` 列表**：发现真实 §7 偏离（upstream 4xx/5xx 原样透传、网络错→FastAPI 默认 500、零测试覆盖） | (a) `/sessions` 列表对齐 sibling（`_raise_upstream_status` + RequestError→503 + 200+坏 JSON→503）；(b) q/p envelope 加 `scope.directories`（区分 scope 未就绪/权威空，加性，不破坏 F1） | ✅ 已落地 | §7（/sessions coded）/ §2 + §12（q/p scope） |
| **Gap 3** | `/since/0` 允许性与 cursor-drain 一致性 | 文档级：ocdroid 已单边裁定 cursor drain；契约 §5 未表态 | §5 补注：无 watermark 初始拉取**推荐 cursor drain**（`?before` 分页）而非 `/since/0` | ✅ 已落地 | §5 |

**额外修复（查证中发现，用户批准一并修）**：
- 🔴 q/p 显式 directory **规范化后去重**（`/app`+`/app/` 不再算 2 scope dir / 双 fan-out；rev-13 review 捕获）。
- 🔴 `/sessions` 列表 200+坏 JSON/坏 shape → 503 `upstream_unavailable`（复刻 sibling `/projects` 防御；rev-13 review 捕获）。

**验证**：本批 +10 测试（Gap1 `/since` 过滤 1 + Gap2 sessions 失败路径 3 + 坏 JSON 2 + q/p scope 3 + normalize-dedup 1）；`./scripts/check.sh` → **200 passed**, EXIT=0。全加性，wire 仍 `1`。
