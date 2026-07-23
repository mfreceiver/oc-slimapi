# oc-slimapi v1 契约（唯一基准）

> **文档修订日志**
>
> | 修订日期 | wire 版本 | 文档 rev | 变更摘要 | 落地对照 |
> |---|---|---|---|---|
> | **2026-07-21** | **1（additive，未 bump）** | **F** | ocdroid v0.11.7 契约反馈落地（设计 v6 + 实现）：**§1** `/slimapi/sessions` 200 加三头 `X-Complete` / `X-Discovery-Directories` / `X-Discovery-Ready`；`start` 语义勘误为 epoch-ms 时间戳水位（非 offset）；非 list body→503；**roots 默认保持 False**。**§2** partId 跨 thin/`/full` 稳定（schema-valid）ratify；placeholder 保留为 message-level 兜底（去 placeholder 转 backlog）。**§3** 新 SSE 帧 `server.reconfigured{reason:"discovery_changed",at}`（仅 discovery 集合变或就绪态 false→true）；`resync` 路径完全不动；`load_products` 全程锁 + 双层 list shape 守卫 + last-known-good。**§4** `/health`+`/ready` schema 加 `version`/`clientMin`/`clientMax`。247 tests green。设计稿：`docs/ocmar/specs/2026-07-21-ocdroid-v0.11.7-feedback-design.md`。 | §1 / §2 / §3 / §4 / §5 / §13 |
> | **2026-07-21** | **1（additive，未 bump）** | **G** | **Opt-A partial-envelope（体验优先 patch）**：能力头 `X-Slimapi-Capabilities`（grammar: comma-split,trim,single `=`,name case-insensitive,value literal,unknown/malformed ignored,dup-conflict fail-closed） + B2 六行响应矩阵（success/partial/errors-only/terminal-envelope-completion/top-503） + C1 累计 413 顶层一致（不返 partial）+ per-mid `upstream_unavailable` 映射（opt-in 仅，non-opt-in 仍顶层 503）+ Retry-After（顶层 HTTP + envelope `retryAfterMs` bounds）+ feature flag + rollback thresholds（5xx>2×baseline / baseline=0→>1% / unknown-code>5% / sample≥100 / 1h window）+ `/metrics` batch ledger（`optA{disabledLatched,disabledReason}`, `counters{...}`, `rollbackWindow{...}`, `byteSamples{...}`）。**Additive，未 bump** `X-Slimapi-Version`（仍为 `1`）。落地对照 §2（G6 批处理依赖能力头）、§7（`upstream_unavailable` per-mid envelope）、§6（指标 ledger 扩展）、**new §15**。**S-C** byte-ratio aggregation (median/P90) added to `byteSamples`; **S-E** optional deployment revision env-or-file injected into `/slimapi/health`. | §2 / §7 / §6 / §15 |
> | **2026-07-23** | **1（additive，未 bump）** | **J** | **Token-stream SSE（opt-in 实时流）**：新可选端点 `GET /slimapi/sessions/{sid}/stream`（`text/event-stream`；progressive text-part 流式渲染）。加性 wire 帧：`message.part.snapshot{done:false}`（握手锚点）/ `message.part.delta{text}` / `message.part.snapshot{done:true}`（**杠杆1：仅完成 marker，无 text**——权威全文走 `/since`）/ `message.part.snapshot{truncated:true}` / `resync{reason,sessionID}`（reason ∈ `reconnect_no_replay`/`subscriber_backpressure`/`token_memory_limit`/`session_idle`/`session_deleted`）/ `server.connected{sessionID}` / `server.heartbeat{}`(15s)。**不发 SSE `id:`、无 replay buffer**；`Last-Event-ID` 值忽略、仅触发首帧 resync。终态顺序不变式：同 part 所有 delta 必先于 `snapshot{done:true}`。**§3.x**（端点 + 帧 + 不变式 + `/since` 真值 + gzip）+ **§6.x addendum**（token T3 信封：独立账本不占 `MAX_TOTAL_SUBSCRIBERS`、预算「同时最多 1 条前台 stream」、内存预算 **Option B 拆 4+4 不双计**（4MiB live + 4MiB pending）、admission 溢出 503 `sse_token_subscriber_limit` + `Retry-After`、**token stream 默认 gzip = 首个 SSE gzip 例外**——控制面 `/slimapi/events` 仍不 gzip）+ **§2** 端点表加 stream 行 + **§7** 加 `sse_token_subscriber_limit` code + health 根级 `features.tokenStream` 加性字段。**Additive，未 bump** `X-Slimapi-Version`（仍 `1`）。设计权威 `docs/specs/design-token-stream.md` v4。落地对照 §2 / §3.x / §6.x / §7。受影响 CLIENT_CHANGES：Token stream SSE 节（杠杆1 done:true marker 无 text）。 |
> | **2026-07-22** | **1（additive，未 bump）** | **I** | **session.created→父 digest childrenVersion（X-main 失效）**：hub 新增 `session.created` 处理——提取子 `info.parentID`→`children_cache.invalidate(parentID)`（bump generation + 驱逐父 cache）+ 触发**父** digest 带 `childrenVersion=generation_of(parentID)`；`DigestFields` 加 `children_version` 字段；`HubRegistry.set_children_cache` 接线（仿 `set_transforms`）。客户端 digest 收到更大 childrenVersion→重拉 `GET /slimapi/sessions/{sid}/children`（缓存已失效→fresh fetch）。**Additive，未 bump** `X-Slimapi-Version`。落地对照 §3（digest childrenVersion）/ §16（invalidate 语义）。受影响 CLIENT_CHANGES：session.digest childrenVersion 消费。 |
> | **2026-07-22** | **1（additive，未 bump）** | **H** | **children 投影端点 + per-key 缓存**：新端点 `GET /slimapi/sessions/{sid}/children`（child skeleton **数组** + 响应头 `X-Children-Version`；sid 感知错误映射复用 §7；slimapi 侧稳定排序 `time.created DESC, id ASC`）；`GET /slimapi/sessions` 每条加性 `childrenIDs[]`/`childrenComplete` hint（纯缓存回填、budget 32、超限省略，杜绝 N× 放大）；`fetch_json_mapped` 加 `expect` 参数（默认 dict、children 用 list）；per-key 缓存（TTL 30s/空 5s、single-flight per-waiter Future、generation 守卫防旧覆盖新、shutdown 取消+await）。**Additive，未 bump** `X-Slimapi-Version`（仍 `1`）。落地对照 §2（children 行 + 小节）/ §7 / **新 §16**。受影响 CLIENT_CHANGES：children 端点消费 / sessions hint / childrenVersion 与 digest 比对（Batch4 接入）。 | §2 / §7 / §16 |
> | **2026-07-20** | **1（无 wire 变更）** | **E** | ocdroid §6「slimapi 侧待确认事项」3 项 slimapi 侧源码级确认（纯核验，无行为变更）：(1) messageID 全路径 verbatim 透传（含 SSE digest fan-out：`skeleton.py:142` info deepcopy / `hub.py:415` `message_id=info.id` / `questions.py:60` q/p 不触碰 id）；(2) `Partial+scope.directories==0` 结构不可能（Partial ⇒ ≥1 fetch ⇒ N≥1）、`Success+N==0` 真实可达（冷启动空 allowlist，§2 + 单测锚定），ocdroid N==0 retain-prior gate 设计正确；(3) `/sessions` 最小错误消费深度（log code + rethrow 原始）= 契约正确，slimapi 不要求差异化（codes 为 observability 面，非分支契约）。**无 wire 变更，不 bump**。确认报告：`docs/ocmar/reports/2026-07-20-v0.2.2-ocdroid-handoff-confirmation.md`。 | 本条 changelog（ocdroid §6 反向确认；无 wire 变更） |
> | **2026-07-20** | **1（additive，未 bump）** | **D** | **ocdroid 客户端适配完成并部署 v0.11.5**（ocdroid commit `807ed52` / tag `v0.11.5`，本地未 push）：tuple tie-break `(updatedAt, messageID)` 二元组字典序 watermark（`compareWatermark` @ onReconcileSuccess/needsReconcile/needsCatchUp/reduceSlimDigest 4 站点）+ q/p `scope.directories` 消费（修 N==0 误清 stale；Success **与** Partial 均 N==0 retain）+ `/sessions` 列表 coded-error observability（`parseErrorCode`→internal + log + rethrow 原始，不引入新异常类型）+ directory 客户端 normalize-dedup（`normalizeDirectory` + fan-out + onResync）。**纯客户端，无 wire 变更**；ocdroid final whole-branch review APPROVED (0C/0I) + fresh verifier EXIT=0/FAILURES=0（live rerun）。**F-1**「`/since` 真过滤 + tie-break」runtime 联调待确认（loopback `127.0.0.1:4097` / mTLS `opencode.vectory.cn:14097`）。ocdroid 报告：其仓 `docs/ocmar/reports/2026-07-20-slimapi-v022-client-adapt.md`。 | 本条 changelog（ocdroid 侧落地确认；无 wire 变更） |
> | **2026-07-20** | **1（additive，未 bump）** | **C** | ocdroid 契约遗留缺口 ratify（3 缺口 + 2 个 pre-existing 真 bug）：**Gap1** `/since/{ts}` 时间过滤 no-op 修复（`info.time.updated` 在 v1.18.3 不存在 → 改读 `updated or created`，与 digest `updatedAt` 对齐）+ 等时间戳 tie-break 规则 `(updatedAt, messageID)` 二元组字典序；**Gap2** `/slimapi/sessions` 列表 §7 偏离修复（原样透传 → `_raise_upstream_status`）+ q/p envelope 加 `scope.directories` 区分「scope 未就绪 / 权威空」；**Gap3** `/since/0` cursor drain 推荐。200 tests green。详见 §14.6。 | §5 / §2 / §7 / §12 / §14.6 |
> | **2026-07-20** | **1（additive，未 bump）** | **B** | ocdroid《slimapi 接口评审报告》§3–§6 原始发现 **F1–F5 + §5 文档重构** 全部落地；本仓审计扩展 **G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步）** 一并实现；另修 2 个 pre-existing SSE 生命周期 bug（teardown 计数泄漏 / queued_bytes 不扣账）+ G1 `error.name` 类型防御。全加性，`X-Slimapi-Version` 仍为 `1`。190 tests green（双独立门控 PASS）。逐条对照见 **§14**。 | §14 |
> | 2026-07-19 | 1（additive） | —（并入 B） | F1–F5/§5/G1/G6/D1–D8 实现提交日（同一批次，文档 rev B 统一收录）。 | §14 |
> | 2026-07-18 | 1（B1） | A | 初始收敛版（A1-A3 / B1-B3 / C1-C2 全定，A2=A 时间戳锚点）；thin 路由错误体 `{"code":...}`、G2 status 404-502-503 分裂、projects 5xx 502→503、新增 8 个错误码。详见 §7。 | — |
>
> **基准声明**：本文件是 wire 基准。实现侧加性/修复性变更在对应小节就地标注，并在本头部「文档修订日志」汇总。所有变更**不 bump `X-Slimapi-Version`** 除非另行说明。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。
>
> **同步纪律**：本文件 changelog 条目须同时列出受影响的 `docs/specs/CLIENT_CHANGES.md` 小节。
>
> **详细变更内容（按修订日）**：
> - **2026-07-21 · v1（additive，rev G）**：**Opt-A partial-envelope（体验优先 patch）**：能力头 `X-Slimapi-Capabilities`（grammar: comma-split,trim,single `=`,name case-insensitive,value literal,unknown/malformed ignored,dup-conflict fail-closed） + B2 六行响应矩阵（success/partial/errors-only/terminal-envelope-completion/top-503） + C1 累计 413 顶层一致（不返 partial）+ per-mid `upstream_unavailable` 映射（opt-in 仅，non-opt-in 仍顶层 503）+ Retry-After（顶层 HTTP + envelope `retryAfterMs` bounds）+ feature flag + rollback thresholds（5xx>2×baseline / baseline=0→>1% / unknown-code>5% / sample≥100 / 1h window）+ `/metrics` batch ledger（`optA{disabledLatched,disabledReason}`, `counters{...}`, `rollbackWindow{...}`, `byteSamples{...}`）。**Additive，未 bump** `X-Slimapi-Version`（仍为 `1`）。落地对照 §2（G6 批处理依赖能力头）、§7（`upstream_unavailable` per-mid envelope）、§6（指标 ledger 扩展）、**new §15**。受影响 CLIENT_CHANGES：能力协商、envelope shape、Retry-After、B1 预算、G-F1 cursor-walk。详见本节各小节 + `docs/specs/CLIENT_CHANGES.md` 相应更新。
> - **2026-07-20 / 2026-07-19 · v1（additive）**：`/slimapi/questions`+`/permissions` 的 `directory` 改可选（null=聚合 allowlist，**F1**）；`/slimapi/sessions/{sid}/status` 放宽 allowlist（sid 自洽，**F2**）；sidecar 启动主动 warm `/project` 暖 allowlist（**F3a**）；routeToken 应答路径 allowlist miss 自动刷新（**F3b**）；`CLIENT_CHANGES.md` SSE 节同步（**F4**）；§1 `accepted:[1,1]` 闭区间说明（**F5**）；新增 §12 directory 三态语义表 + §13 allowlist 机制（**§5**）；**G1** `session.digest` 加 `lastError?` 三态字段 + 新 `event: session.error` session-less 帧 + 脱敏算法；**G6** `GET /slimapi/messages/{sid}/full?ids=` 批量展开端点（envelope + mid 级部分失败 + chunk-ledger 累计预算）；D1–D8 文档同步（§11 标 closed）。受影响 CLIENT_CHANGES：SSE（`lastError` / `session.error`）、消息批量展开、q/p null directory、cold-start 顺序、错误体形状。
> - **2026-07-18 · v1 B1（additive）**：`session_not_found`(404) / 顶层 `upstream_unavailable`(503) / 顶层 `upstream_http_N`(502) / `shell_not_allowed`(403) / `invalid_directory_count`(400) / `invalid_route_token`(400) / thin 路由错误体 `{"code":...}`（非 `{"detail":...}`） / G2 status 404-502-503 分裂 / projects 5xx 502→503。详见 §7。受影响 CLIENT_CHANGES：错误体形状。

> 状态：契约收敛版（A1-A3/B1-B3/C1-C2 全定，A2=A 时间戳锚点；rev B F1–F5/§5/G1/G6/D1–D8；rev C Gap1–3 ratify；rev D ocdroid 客户端适配完成 + 部署 v0.11.5；rev E ocdroid §6 三项 slimapi 侧确认；**rev F ocdroid v0.11.7 反馈落地（sessions 三头 + discovery_changed SSE + health schema + partId ratify）**；**rev G Opt-A partial-envelope（体验优先 patch）**；**rev H children 投影端点 + per-key 缓存**；**rev I session.created→父 digest childrenVersion（X-main 失效）**；**rev J token-stream SSE（opt-in 实时流；杠杆1 done:true marker 无 text + 杠杆2 gzip 首个 SSE 例外 + 独立 T3 账本 + 内存预算 Option B 4+4）**）。配套原型与正式实现已覆盖；本文 🔒=已覆盖、🆕=历史缺口标注（§11 已闭环）。
> 权威性：本文件是正式实现的唯一基准。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。

## §0 范围与架构
- 纯 HTTP sidecar：FastAPI + httpx + orjson + uvicorn **单 worker**，host ∈ `{127.0.0.1, ::1, localhost, 0.0.0.0}`。
  - **已部署稳态**：绑定 `0.0.0.0:4097`（所有接口），**用户接受**；直接 `:4097` 明文访问须经网络边界（防火墙/Tailscale ACL）阻断；外部客户端经 `:14097` mTLS 隧道（stunnel `requireCert=yes verifyChain=yes`，复用既有证书）可达。
> **（2026-07-21 cdba40d：G-ACL 部署姿态由「收紧 loopback」改为「0.0.0.0:4097 用户接受稳态 + 14097 mTLS」——用户最终决定不收紧 loopback，复用既有 mTLS 证书；详见 `docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md` §4.3。非 wire 变更。）**
  - **`:14097`** 为公网唯一 mTLS 入口；upstream 始终固定 `127.0.0.1:4096`（SSRF guard 不随 host 放松）。
  - **loopback-only（`127.0.0.1:4097`）** 属更严格替代姿态，非当前部署；代码允许该配置。
- **不读 opencode SQLite**；仅 legacy `/session` API；upstream 始终固定 loopback HTTP（SSRF guard 不随 host 放松）。
- v1 目标：**2-5 台同用户设备**（T3 硬化进 v1）。
- 客户端通过"切换服务器"进省流（R8：`mtls×slim` 两布尔→4 配置），非连接属性开关。

## §1 版本契约 🔒
- 头 `X-Slimapi-Version: <int>`，所有 `/slimapi/**` 必带。
- 门闩 `ACCEPTED_CLIENT_VERSIONS=(1,1)`：`accepted:[1,1]` 是闭区间 `[min,max]`，当前 `min=max=1`（仅接受整数 `1`）。缺/非整数→400 `version_required`；越界→400 `version_incompatible`（带 `client`/`accepted`）。
- `/slimapi/health` 与 `/slimapi/ready` 返回 `sidecar.ok`（health）/ `upstream`（ready）+ `server.api_version` + `accepted_client_versions` + `schema:{degraded, version, clientMin, clientMax}`（rev F：`version`/`clientMin`/`clientMax` 从 config 读，与 `server.*` 同源；**诊断用 wire 范围回显，非 feature discovery**——additive 时 version 保持 1，三元组不变）。**S-E**：`health` 响应 `server` 对象可选加 `deploymentRevision` 字段（当通过 `OC_SLIMAPI_DEPLOYMENT_REVISION(_FILE)` 设置时出现；未设置时整字段省略）。
- **能力头 Opt-A 不属于版本 bump**：`X-Slimapi-Capabilities: mid-partial-envelope=1` 是加性 HTTP 头，**不**改变 `X-Slimapi-Version`（仍为 `1`）。详见 §15。
- bump 规则：整数，仅破坏性变更 bump；加性变更同版本。

## §2 端点

| 方法 | 路径 | 桶 | 状态 | 说明 |
|---|---|---|---|---|
| GET | `/slimapi/health` | A | 🔒 (gzip 🆕；rev F schema 🆕) | 版本+降级+self-check；`schema:{degraded,version,clientMin,clientMax}` |
| GET | `/slimapi/ready` | A | 🔒 (gzip 🆕；rev F schema 🆕) | liveness；同上 schema 三键 |
| GET | `/slimapi/metrics` | A | 🆕 T3 | 订阅者/queue/hub 指标 |
| GET | `/slimapi/sessions` | A | 🔒 (rev F 头 🆕) | 骨架 session 列表（`?directory/roots/limit/start/search`；`roots` 默认 **False**——客户端**应显式传** `roots=true` 以排除 subagent/task；`start` = epoch-ms **时间戳水位** `time_updated >= start`，**非 offset**，上游 legacy 不暴露前向 cursor、不保证 id tie-break）；200 加三头见下；每条带 `directory` 字段 |
| GET | `/slimapi/projects` | A | 🔒 | project/directory 发现 + allowlist |
| GET | `/slimapi/sessions/status` | A | 🔒 | 批量 status（`?directory` 必填；v0.3.0 起 normalize 透传，不 gate allowlist） |
| GET | `/slimapi/sessions/{sid}/status` | A | 🔒 | 单 ses status（id→directory 自洽；**sid 为能力凭证，不受 allowlist 约束**） |
| GET | `/slimapi/sessions/{sid}/children` | A | 🆕 rev H | 子会话 skeleton 列表（`?directory` 可选透传；body=child skeleton 数组 + 响应头 `X-Children-Version`；per-key 缓存 + single-flight，见 §16；排序 `time.created DESC, id ASC`） |
| GET | `/slimapi/messages/{sid}` | A | 🔒 | 骨架分页（`?limit/before/mode`） |
| GET | `/slimapi/messages/{sid}/since/{ts}` | A | 🔒 (语义 🆕；rev C 勘误) | **A2=A**：`(info.time.updated or info.time.created) >= ts` 的骨架（rev C：v1.18.3 无 message 级 `time.updated`，实读 `created`，与 digest `updatedAt` 同源）；`?limit/before` 分页；等时间戳 tie-break 见 §5 |
| GET | `/slimapi/messages/{sid}/full/{mid}` | A | 🔒 | 单条全文（mode=full，展开某条） |
| GET | `/slimapi/messages/{sid}/full?ids=` | A | 🔒 (G6 🆕) | 批量展开（1–20 mid，discover 先行，mid 级 envelope `errors[]`，累计 413） |
| GET | `/slimapi/questions` | A | 🔒 | 跨目录聚合 pending（`?directory` repeated 1-32 **可选**；null=聚合 allowlist 全部 dir），每条带 `routeToken`；200 envelope `{items,errors,scope:{directories:N}}`（rev C：N=有效 scope dir 数，区分 scope 未就绪/权威空，见 §12） |
| GET | `/slimapi/permissions` | A | 🔒 | 同上 |
| POST | `/slimapi/questions/{qid}/reply` | A | 🔒 | routeToken 校验 + 注入 directory + 转发 opencode |
| POST | `/slimapi/questions/{qid}/reject` | A | 🔒 | 同上 |
| POST | `/slimapi/sessions/{sid}/permissions/{pid}` | A | 🔒 | 同上（`response: once/always/reject`） |
| GET | `/slimapi/events` | A | 🔒 (archived 🆕；rev F reconfigured 🆕) | 实例级策展 SSE（见 §3；含 `server.reconfigured`） |
| GET | `/slimapi/sessions/{sid}/stream` | A | 🆕 rev J | opt-in 实时 token stream SSE（见 §3.x；**gzip 默认[lever2，首个 SSE gzip 例外]**、独立 T3 账本[§6.x]、终态 done:true marker 无 text[lever1]） |
| * | `/{path}` (catch-all) | B | 🔒 | 透传 opencode（含发消息等写）；客户端发 `X-Opencode-Directory` 头过透传 |

### 写路径（B2）🔒
- q/p 应答：走 §2 的 routeToken 端点（routeToken 在 `/slimapi/questions`/`/permissions` 聚合响应里随条下发，绑 kind+requestID+sessionID+directory，HMAC ~1h）。
- 发消息/abort 等通用写：客户端走 catch-all 透传，自带 `X-Opencode-Directory` 头（现有 `DirectoryHeaderInterceptor`），slimapi 不剥（非 hop-by-hop）。
- routeToken 404/过期 → 透明（已应答/失效），客户端重取聚合。
- routeToken 应答路径：token 校验后 normalize directory 并作 `?directory=` + `X-Opencode-Directory` 透传上游。**v0.3.0** 不再 gate allowlist，刷新失败也不返 503。

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
  - **任一 mid `httpx.RequestError`（网络 / 流中断）→ 依赖能力头**：
    - **非 opt-in / opt-in 且全部 mids 均为 RequestError** → **503 `upstream_unavailable`**（整请求 503，优先于 413）。
    - **opt-in 且存在至少一个成功 item 或其它 envelope error** → part of `errors[]`（envelope 200，不触发整请求 503）。详见 §15。
  - skeleton 模式 transform pool 饱和 → 503 `transform_busy`（`Retry-After`）。
- **定序**：`items[]` 顺序 = `ids` 去重后序（保证）；`errors[]` 顺序 = 并发完成序（**不保证**，客户端不得依赖）。
- **路由**：`/full` 注册先于 `/full/{mid}`。

### `/slimapi/sessions` 完整性头（rev F 🆕）

200 响应（仅成功路径）加三响应头；502/503 等错误路径**不**发：

| 头 | 语义 |
|---|---|
| `X-Complete` | `"true"` 当且仅当 `len(sessions) < limit`（**本页未满**）。**强制语言**：客户端**不得**据此判定「权威全集」「权威空」「覆盖完整性」或「结束冷启动」——上游 legacy `/session` 无 total/快照、`start` 为时间戳水位、无前向 cursor。`false` 仅表示「≥ limit 条匹配，可能截断；可提高 `limit` 或收窄 `start` 复查」。 |
| `X-Discovery-Directories` | `len(app.state.directory_allowlist)`（sidecar 发现目录数）。**不是**本次 query 命中 dir 数（`/sessions` 不 fan-out），**不是** q/p `scope.directories`（fan-out 命中数）。须与 `X-Discovery-Ready` 联读。 |
| `X-Discovery-Ready` | `"true"` = 至少一次成功 `load_products`（持有 last-known-good 快照；Directories 对该快照权威，**不保证实时**）；`"false"` = 尚无成功发现（暖机未成 / 全失败）。后续刷新失败**不**复位 ready（保留 last-known-good）。 |

- 上游 body 非 list（dict/string/null 等）→ 503 `upstream_unavailable`（与坏 JSON 同路径），不发三头。
- `start` 参数：epoch-ms 时间戳水位（上游 `time_updated >= start`），**非 offset**；默认不传 = 全部；**不**支持前向分页 cursor（上游 legacy HTTP 不暴露）。
- `roots` 默认 **False**（不翻）；客户端**应显式传** `roots=true` 以排除 subagent/task 子会话。

### 端点 × 发现/scope 字段对照（rev F）

| 端点 | 字段/头 | 取值 | 语义 |
|---|---|---|---|
| `GET /slimapi/sessions` | `X-Discovery-Directories` | allowlist 大小 | 全局发现集大小（readiness 联读） |
| `GET /slimapi/sessions` | `X-Discovery-Ready` | true/false | 是否存在 last-known-good 发现快照 |
| `GET /slimapi/questions` / `/permissions` | `scope.directories` | fan-out 有效 dir 数 | null 路径=allowlist 大小；显式路径=去重后 dir 数 |

### children 投影与缓存（rev H 🆕）

- **端点** `GET /slimapi/sessions/{sid}/children`（A 桶、version 门禁）：
  - query `directory` 可选，经 `normalize_directory` 后作 `X-Opencode-Directory` + `?directory=` 透传上游 `GET /session/{sid}/children`（§12/§13 同语义；上游返回 `Session.Info[]`，父 sid 不存在→404，无子→`[]`）。
  - 响应 body = child skeleton **数组**（投影自 `Session.Info[]`，复用 `skeleton_session`，含 `parentID`/`directory`/`time.created`）；**响应头 `X-Children-Version: <int>`** = 该 parentSid 的单调 generation（§16），客户端与后续 digest `childrenVersion` 比对（Batch4）。
  - **排序**（slimapi 侧稳定排序 = 加性 wire 保证）：`time.created DESC`、`id ASC` tie-break（上游 children 查询无 ORDER BY，slimapi 缓存命中/miss 交替须稳定输出；缺失 `created` 归 0 排尾）。
  - 错误映射复用 §7（sid 感知，经 `fetch_json_mapped(sid=sid, expect=list)`）：upstream 404 → 404 `session_not_found`（带 `sessionID`）；其它 4xx → 502 `upstream_http_N`；5xx / 网络 / 坏 JSON / 非 list → 503 `upstream_unavailable`。
  - **per-key 缓存 + single-flight**（§16）：同 `(parentSid, normalized_dir)` 并发请求合并为一次上游 fetch；TTL 命中直接返；缓存对客户端不可见（除 `X-Children-Version`）。
- **列表 hint**（`GET /slimapi/sessions` 每条 session 加性字段）：
  - `childrenIDs[]`：该 session 作为 parent 的子 sid 列表——**纯缓存回填**（仅当 children 缓存已有该 parent 条目才下发，**不**触发新上游调用，杜绝 N× 放大）。
  - `childrenComplete`：`true` = hint 权威（缓存命中且 child 数 ≤ `CHILDREN_IDS_HINT_LIMIT=32`）；`false`/省略 = 缓存未命中或超 budget，客户端应调权威端点 `/slimapi/sessions/{sid}/children`。

## §3 SSE 契约（简化版，A1-A3 落定）🔒 + archived 🆕 + G1 🆕 + reconfigured 🆕
- 上游：**一条** `/global/event`（进程级 GlobalBus，全实例跨目录，每事件自带 `directory`）。
- 帧：
  - `session.digest`（debounce 250ms/session，仅发有变化的字段）：`{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?, childrenVersion?}`。
    - `childrenVersion`（rev I 🆕，**仅父会话**）：当某 parent 的子会话集变更时，该 **parent** 的 digest 携带 `childrenVersion` = 其单调 generation（与 `GET /slimapi/sessions/{sid}/children` 的 `X-Children-Version` 同源、同键）。客户端记本地该 parent 的 version，digest 收到更大值 → 重拉 `/slimapi/sessions/{sid}/children`（缓存已被 invalidate，必 fresh fetch）。**来源**：上游 `session.created`（fork/create 子会话，子 `Info.parentID` 指向父）→ slimapi hub `invalidate(parentID)`（bump generation + 驱逐父 cache）+ 触发父 digest 带 `childrenVersion=generation_of(parentID)`。进程重启 generation 归零——客户端以 `server.connected`/resync 为 server generation 边界重置 baseline（R3/OC-3）。
    - `status`←`session.status`(idle/busy)；`messageID`+`updatedAt`←`message.updated`/`message.appended`（info.id + info.time.updated/created，取最新）；**`archived`←`session.updated` 的 `info.time.archived`（有值→epoch-ms 时间戳）** 🆕；`deleted`←`session.deleted`。
    - **`lastError`（G1-A）**←`session.error` 经脱敏后的 `{name,message,at}`（`at`=sidecar 收到时 epoch-ms）。**三态 wire**（与 sticky 共存，互不矛盾）：
      - **对象** `{name,message,at}`：本窗口新 error，或 flush 时该 sid 仍有 sticky（其它字段触发的后续 digest 会继续带出对象，直至 clear/deleted）。
      - **显式 `null`**：clear 帧——该 session 出现新 `status=busy` 时 pop sticky 并立即 flush。
      - **省略**：本 digest 没有本窗口新 error 对象、也没有显式 clear（`null`），**且** 该 sid 当前不存在 sticky error；`deleted=true` 的 digest **强制省略**（pop sticky，**不**发 null）。
      - abort（`error.name=="MessageAbortedError"`）静默丢弃（不写 lastError、不发 G1-B 帧）。
      - 脱敏：`message` 取首行→剥绝对路径→剥 stack frame→剥 secret→截断 ≤512；缺失回落 `name` 或 `"(no detail)"`；`name` 截断 ≤128。
  - `session.error`（G1-B，**无** `sessionID` 时立即直推，不走 debounce）：`{directory?, name, message, at}`。abort（`MessageAbortedError`）静默丢弃。有 sid 的 `session.error` **不**走本帧，走 digest `lastError`（G1-A 立即 flush）。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`：**立即直推** `{directory, type, properties}`。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}` / 背压 `{"reason":"subscriber_backpressure"}`，无 replay）。
  - **`server.reconfigured`（rev F 🆕）**：payload `{reason: "discovery_changed", at: <epoch-ms>}`。**仅** discovery 变更时直推（不走 debounce）：
    - 触发：`load_products` 成功提交后，`(new_allowlist != old_allowlist) OR (old_ready is False AND new_ready is True)`（集合变 **或** 就绪态 false→true，即便集合仍空）。
    - **不**在上游重连/掉线时发（那些路径继续发既有 `resync`——**无双重 cold-start**）。
    - 无活跃订阅者时 no-op（不惰性创建 hub）。
    - 客户端：收到即作废本地 commitToken / stale，触发 cold-start 重拉。
- 丢弃：`?stream`、text.delta、`message.part.*`、`tool.*`、`sessionId` 参数、per-directory hub。
- **连接建立期 coalescing（rev F）**：带 `Last-Event-ID` 重连时，同连接可能先收 `resync{reconnect_no_replay}` 再收队列内 `server.connected`（既有行为）。客户端 **SHOULD** 对同一 SSE 连接建立期的 cold-start 触发帧做 once-latch coalescing（至多一次 reconcile；reconcile 幂等）。
- **heartbeat ≠ 上游健康**：`server.heartbeat` 仅证 sidecar + 订阅连接存活；上游 outage 探测委托 `GET /slimapi/ready` 或自然 fetch/write 失败。sidecar 进程重启 = 连接断开；客户端重连收 `server.connected`，**应**视为 cold-start 触发。

## §3.x Token stream SSE（opt-in 实时流，rev J 🆕）

> **状态**：加性 wire 行为，**不 bump** `X-Slimapi-Version`（仍 `1`）。设计权威 `docs/specs/design-token-stream.md` v4。客户端能力探测：`GET /slimapi/health` 根级 `features.tokenStream===true`（Q1 冻结路径：top-level `features`，与 `sidecar`/`server`/`schema` 并列）；缺/404/405 → 降级既有「完成后整条出现」（`/since` 拉权威全文），**零回归**。

### §3.x.1 端点

- `GET /slimapi/sessions/{sid}/stream?directory=<optional>`；`text/event-stream`；响应头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`。
- `/slimapi/**` 版本门禁复用 `SlimapiVersionMiddleware`（仍 `X-Slimapi-Version:1`，**不 bump**）；无 route-level `Depends`。
- `directory` 可选 query；`normalize_directory()`；query 与 `X-Opencode-Directory` 头冲突（trailing-slash 归一后不等）→ 400 `directory_not_allowed`。directory 仅过滤进程级 GlobalBus 事件，**不开第二条上游连接**；sid 全局唯一、directory 无关（单用户 T3）。路由注册在 catch-all 反代之前；不遮蔽 `/{sid}/status`、`/{sid}/children`。
- **opt-in**：客户端前台/动画层才连；切后台/换 session 应断开（详见 §6.x token T3 信封「同时最多 1 条前台 stream」）。连接独立于控制面 `/slimapi/events`——两条连接，互不替代。
- **P1 范围**：仅 text part（reasoning / tool-input 延后 P2+）；不做二进制流。

### §3.x.2 Wire 帧

```
# 1) 订阅首帧：活跃 part 累计全文锚点
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<累计全文>","done":false}

# 2) 批式增量（100ms / 4KiB flush；§5.4 design）
event: message.part.delta
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<本窗拼接>"}

# 3) 终态 marker（杠杆1：去终态全文——仅完成标记，无 text；权威全文走 /since）
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","done":true}

# 4) 大 part 超 1MiB（done:false 或 done:true 均可能）——不静默 drop
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","truncated":true,"done":false|true}

# 5) resync（背压/重连/超大/内存上限/生成结束清理；token resync 恒带 sessionID）
event: resync
data: {"reason":"subscriber_backpressure|reconnect_no_replay|token_memory_limit|session_idle|session_deleted","sessionID":"…"}

# 6) server.connected{sessionID} / server.heartbeat{}（15s）
```

- **不发 SSE `id:` 字段**、**无 replay buffer**；`Last-Event-ID` 仅触发首帧 `resync{reconnect_no_replay,sessionID}`，**值忽略**。
- **终态顺序不变式（wire 强约束）**：对同一 `(sid,mid,pid)`，所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不许再发 delta。
- **杠杆1（决定性）**：终态 `snapshot{done:true}` 是**仅完成 marker，不带 text**——取消上游 `part.text` 终态重发。**权威全文走 `/since`**（持久化真值）；token stream 是动画层，`/since` 幂等覆盖且凌驾所有 token 帧。客户端可接受 digest 完成先于/晚于 token 终态帧。
- **resync reasons**（token 流均带 `sessionID`）：`reconnect_no_replay`（上游重连）/ `subscriber_backpressure`（订阅者 T3 溢出）/ `token_memory_limit`（全局累加器上限）/ `session_idle`（生成结束清理）/ `session_deleted`（会话被删除）。**单 part >1MiB 不走 resync**，而是 `message.part.snapshot{truncated:true}`（见上）——客户端清该 part streamOwned、走 `/since`。
- **truncated 处理**：收 `snapshot{truncated:true}`（done:false 或 done:true 均可能）→ 客户端清该 part streamOwned、停 append、走 `/since`。
- **reasoning/tool part**（`part.type!="text"`）的 delta **静默 drop+计数**（C3），不 resync；field≠"text" 的 delta 丢弃。

### §3.x.3 gzip（杠杆2 — 首个 SSE gzip 例外）

- token stream **默认 gzip**（流式 zlib `Z_SYNC_FLUSH`，`Content-Encoding: gzip`）；按 `Accept-Encoding` 协商。
- **首个 SSE gzip 例外**：此前「SSE 永不 gzip」（§9 + §1 [0.1.0]）的唯一破例；控制面 `/slimapi/events` **仍不 gzip**。
- **实测性能**（详见 `docs/specs/design-token-stream.md` §11，harness `scripts/measure_token_overhead.py`，12 trace、30 tok/s × 100ms）：原批式 ~12x 开销 → 杠杆1+2 后 gzip 中位 **1.47x**（**达成 re-anchor ~1.5x 中位目标**；1/3 trace <1.0x）。残余 ~0.3x（短消息/低冗余内容）记 Stage E 可选调参（flush 窗 100→200ms、gzip flush cadence、level），post-release。

### §3.x.4 与控制面 / `/since` 的关系

- 控制面 `/slimapi/events`（§3）**一行不改**——token 流消费上游 `message.part.delta`/`updated`（控制面此前丢弃），与控制面队列隔离（独立 T3 账本，§6.x）。
- part/message 完成仍走既有路径：`message.updated`(step-finish) → digest → 客户端 `/since` 拉权威全文。
- token stream `snapshot{done:true}` 是「流视角完成」；digest + `/since` 是「持久化真值」。不一致以 `/since` 为准（幂等覆盖，凌驾所有 token 帧）。

## §4 冷启动 & resync（A1 + A3）🔒
- **sidecar 启动暖机**：lifespan 在 smoke 后 best-effort 调一次 `/project` 预热 `app.state.directory_allowlist` + `allowlist_ready`（`warm_allowlist`；失败仅吞错，不阻断启动，ready 保持 False）。**v0.3.0** allowlist 已不作 gate，暖机仅供 `/slimapi/projects` 展示与 q/p null-directory 聚合 fan-out；不再有"lazy `require_directory` 刷新"回退路径。
- **客户端冷启动顺序**：
  1. 可选 `GET /slimapi/projects`（显式刷新 allowlist / 发现 project；成功则 `X-Discovery-Ready` 后续为 true）；
  2. `GET /slimapi/sessions`（`directory` null OK，不过滤；消费三完整性头）；
  3. `GET /slimapi/questions` + `/permissions`（`directory` **可选**；null=聚合 allowlist 全部 dir；空 allowlist→200 空 envelope）；
  4. 当前打开 ses：`GET /slimapi/messages/{sid}/since/{ts}`。
- 之后 SSE 接力增量（含 `server.reconfigured` → 复用冷启动）。
- **resync / server.reconfigured / server.connected = 复用冷启动流程**（同一"加载初始状态"代码路径；幂等）。

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
- **partId 稳定性（rev F ratify）**：schema-valid 的 `MessageWithParts` 下，thin skeleton（`mode=skeleton`）经 `_pick(part, PART_IDS)` 保留每个 part 的真实 `id`，与 `/full/{mid}`（单条）及 `/full?ids=`（batch）中的 part `id` **跨端点稳定**。sidecar **不**校验缺失/坏 shape id（仅复制存在的字段）。
- **placeholder（rev F）**：无可渲染 part 时 thin 仍注入合成 part `id=thin_placeholder_{messageID}`、`type=text`、`text="[内容已折叠，点开查看]"`、`hasFull:true`、`omitted:["parts"]`。该 id **不参与** `/full` 的 `messageId+partId` 对齐；客户端展开 `/full` 后应**整体替换该 message 的 parts**（判定：`partId.startsWith("thin_placeholder_")` → message-level replace，禁止按 placeholder id 做 part-level lookup）。去 placeholder（thin 直接返真实折叠 part）转入 backlog，待与 ocdroid 协调发版。

## §6 资源限制（T3，C2=2-5 台进 v1）🆕
- `MAX_SUBSCRIBERS_PER_DIRECTORY=8`、`MAX_TOTAL_SUBSCRIBERS=16`。
- 每 subscriber buffer `2 MiB`、单帧 `256 KiB`；溢出→**立即清 queue/deltas/dirty** + 排 `resync{reason:subscriber_backpressure}` + STOP（替代当前"queue 尾排 STOP 继续发旧帧"）。
- admission 在 `HubRegistry.subscribe` 单一无 await 临界段；超限→503 `sse_subscriber_limit_directory`/`_total`（带 `limit`/`current`/`Retry-After`）。
- 转换池（fix-9 🔒）：`MAX_TRANSFORMS=1`，admission 在下载前，限长读 `MAX_RESPONSE_BYTES=64MiB`，parse/project/gzip offload worker thread。

### §6.x Token stream T3 信封（rev J 🆕，加性）

> 设计权威 `docs/specs/design-token-stream.md` §6。token 订阅**独立账本**，与控制面 SSE T3 隔离——避免 token 高吞吐挤掉 q/p 或误触控制面 `subscriber_backpressure`。**控制面 `MAX_TOTAL_SUBSCRIBERS=16` / `MAX_SUBSCRIBERS_PER_DIRECTORY=8` 等既有上限一行不改**。

- **独立 admission 账本**：token 订阅**不**占用控制面 `MAX_TOTAL_SUBSCRIBERS=16`；自有 `token_stream_max_subscribers=8`、`token_stream_queue_items=64`、`token_stream_buffer_bytes=512KiB/sub`、`token_stream_max_frame_bytes=1MiB`。
- **同时最多 1 条前台 stream**（客户端预算，对应设计 §9 #7；token stream 每连接绑单 sid）。
- **内存预算 = Option B（拆 4+4，不双计）**：
  - `TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live `LivePart.chunks` 累计字节）
  - `TOKEN_PENDING_MAX_BYTES=4MiB`（pending `DeltaAccumulator` 累计字节，与 live **不双计**——同一 delta chunk 不在两个池同时占额度）
  - 单 part 上限 `TOKEN_PART_MAX_BYTES=1MiB`；全局活跃 part 数 `TOKEN_LIVE_PARTS_MAX=32`。
  - **裁定**：Option B（拆 4+4）优于 Option A（合并 8MiB 单池），因 pending 独立上限更防御（pending 突发不挤掉 live 退役预算）；worst-case 与 Option A 同上限但内部更难同时打满。
  - **worst-case**：`8 × 512KiB 订阅队列 + 4MiB live + 4MiB pending = 12MiB`。
  - `_reserve` 处理 delta 超剩余预算 → 退役最旧 part（按 `last_delta_ms`）+ `resync{token_memory_limit,sessionID}`。
- **admission 溢出** → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。
- **gzip**：token stream 默认 gzip（杠杆2，§3.x.3，首个 SSE gzip 例外）；控制面 `/slimapi/events` 仍不 gzip。

## §7 错误码 🔒 + 🆕 (additive, no X-Slimapi-Version bump)

> v1 B1（2026-07-18）扩充：thin 路由错误体由 FastAPI 默认 `{"detail":…}` 改为 `{"code":…}`，并新增以下 code；均为加性、不 bump `X-Slimapi-Version`。详见 `docs/specs/v1-impl-spec.md` §11 + `docs/specs/CLIENT_CHANGES.md`「错误体形状」。
> 2026-07-19 加性：G6 `invalid_ids` / envelope `message_not_found` / envelope `upstream_error`（mid 坏 JSON）；F2 收窄 `directory_not_allowed` 适用范围。
> 2026-07-20 加性（rev C）：`GET /slimapi/sessions` 列表端点失败路径对齐 §7（原静默偏离：upstream 4xx/5xx 原样透传 body、网络错落 FastAPI 默认 `{"detail":...}` 500；现统一 4xx→502 `upstream_http_N`、5xx/网络→503 `upstream_unavailable`，body 为 `{"code":...}`）。
> **v0.3.0** 加性：**完全移除 directory allowlist gate**——directory ∉ allowlist 不再 400；slimapi 把 normalized directory 作为 `X-Opencode-Directory` + `?directory=` 透传，由上游 opencode 决定能否服务。`directory_not_allowed` 错误码保留，仅用于 messages `/**` query `directory` 与 `X-Opencode-Directory` 头冲突的结构性歧义。
>
> **top-level vs envelope**：下列 code 默认指 thin 路由 **HTTP 状态 + body `{"code":…}`**。G6 另有 **envelope 语境**（整请求通常仍 200，code 出现在 `errors[]` 的 mid 项）。**同一 code 名两语境含义不同**，见各条标注。

- 400 `version_required` / `version_incompatible` / `directory_not_allowed` / `invalid_directory_count` / `invalid_route_token` / **`invalid_ids`**（G6 top-level：`ids` 空 / 超 20 / 解析后无有效 mid）
  - **`directory_not_allowed` 适用范围**（**v0.3.0** 收窄）：**仅** messages `/**`（list / since / full/{mid} / full?ids=）当 query `directory` 与 `X-Opencode-Directory` 头同时存在且冲突时返 400——这是结构性歧义（slimapi 不能猜该透传哪个），与上游能否服务无关。**不再**因 directory ∉ allowlist 触发；其它结构性守卫（`invalid_directory_count` 显式 list 0 / >32、`invalid_route_token`、版本门禁）不变。
- 403 `shell_not_allowed`（catch-all shell/PTY deny-list；ops 可关，非安全保证）
- 404 `session_not_found`（`GET /slimapi/sessions/{sid}/status` 与 G6 **discover** 的 upstream 404；top-level，带 `sessionID`）；`thin_route_not_found`
  - **`message_not_found`**：**仅 G6 envelope** mid 级 code（HTTP 仍 200；**非整请求 404**）
- 413 `response_too_large`（top-level：超 `MAX_RESPONSE_BYTES`；含 G6 累计）
  - **`message_too_large`**：**top-level** 于 `GET .../full/{mid}?mode=full`（单条流式 cap→413）；**G6 envelope** 于 mid body 超 `max_message_bytes`（整请求仍 200）
- 502 `upstream_http_N`
  - **top-level**：G2 status / projects / G6 **discover** 等对 upstream **非 404 的 4xx** → 502（discover 5xx 走 503，见上）
  - **G6 envelope**：mid **≥400（含 5xx）** → `errors[]` `upstream_http_N`，**整请求仍 200**（mid 5xx **不**升级为整请求 5xx）
- 503 `transform_busy`（`Retry-After`；含 G6 skeleton pool 饱和）/ `upstream_unavailable`（含 G6：discover 5xx·网络·坏 JSON；**任一 mid 网络失败**——且 **优先于** 累计 413）/ allowlist 刷新失败 / `sse_subscriber_limit_*` 🆕
- 503 `sse_token_subscriber_limit`（rev J 🆕，token stream admission 溢出；带 `{"limit":8,"current":N}` + `Retry-After:5`；**独立账本**，不占控制面 `MAX_TOTAL_SUBSCRIBERS`，见 §6.x）
- **`upstream_unavailable`（envelope per-mid，仅 Opt-A opt-in 且存在成功 item 或其它 envelope error）**：mid 网络失败（`httpx.RequestError`）在 envelope 中映射为此 code，同时可选携带 `retryAfterMs`（ms，≤10000）。整请求仍 200，items 含成功项。非 opt-in 或全部 mids 网络失败时仍为顶层 503 `upstream_unavailable`。详见 §15。
- **`upstream_error`**：**G6 envelope** mid 2xx body 不可解析（坏 JSON）；亦见 q/p fan-out 单 dir 失败项。非整请求 500。
- 504 `upstream_timeout`（q/p mutation）
- thin 路由错误体统一：`{"code":string, "message"?:string, ...}`（非 `{"detail":...}`）
- FastAPI 参数缺失/类型错误仍为 422（如 G6 缺 `ids`）

## §8 客户端 v1 最小集（C1，暂停 — ocdroid）
连接(R8)+版本头+health 自检(M2/fail-closed)+冷启动(sessions+q/p 快照，见 §4)+SSE(digest+q/p+`lastError`/`session.error`)+digest 触发拉消息(`/since`)+全文(`/full/{mid}` 或 G6 batch)+发消息(X-Opencode-Directory 透传)+q/p 应答(routeToken)+resync=冷启动。**+ C3 health 改 `/slimapi/health`（fix-7 已落地）**。

## §9 gzip 🆕（小修）
所有 JSON 路由的 `json_response` 调用转发 `accept_encoding=request.headers.get("accept-encoding")`。sessions/questions 已做；health/ready 等补齐。

> **SSE gzip 例外（rev J 🆕）**：历史「SSE 永不 gzip」由 token stream 打破——`GET /slimapi/sessions/{sid}/stream` **默认 gzip**（杠杆2，首个 SSE gzip 例外，详见 §3.x.3）；控制面 `GET /slimapi/events` **仍不 gzip**。

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

> **v0.3.0**：slimapi 已**完全移除 directory allowlist gate**——任意 directory 经 `normalize_directory` 后透传给上游 opencode（由 opencode 决定能否服务）。下表「显式且 ∉ allowlist」一列从「400」改为「透传」。

| 端点 | null / 未传 | 显式 directory（∈ 或 ∉ allowlist 同行为） |
|---|---|---|
| `GET /slimapi/sessions` | 200，不过滤（upstream 默认） | 透传 `?directory=` + `X-Opencode-Directory`（normalize 后） |
| `GET /slimapi/sessions/status` | **必填**（缺→422） | 透传批量 status map |
| `GET /slimapi/sessions/{sid}/status` | **无** directory 参数；discover sid→directory，仅 normalize 后透传 | — |
| `GET /slimapi/questions` / `/permissions` | **可选**；null=聚合 allowlist **全部** dir（不受 1–32 守卫）；空 allowlist→200 `{"items":[],"errors":[],"scope":{"directories":0}}`（F1；`scope.directories` 区分 scope 未就绪/权威空，见 §2） | 去重保序后 1–32 项 fan-out；每条带 `routeToken`；0 或 >32→400 `invalid_directory_count` |
| `GET /slimapi/messages/**`（含 G6 `/full?ids=`） | **不拦**（upstream 默认） | normalize 后作 `X-Opencode-Directory`；query 与 header 冲突 → 400 `directory_not_allowed` |
| `POST` q/p reply/reject/permission | directory 来自 routeToken，不接受 body directory | token HMAC 校验后 normalize 透传（不再 gate allowlist） |

说明：
- **slimapi 不再做目录警察**：directory 的合法性由上游 opencode 决定；opencode 自身的 4xx 会经 §7 透传（如 `upstream_http_N`）。
- **sid 能力凭证**：客户端仅从 list / SSE / routeToken 合法渠道获知 sid。
- **null 聚合（q/p）**：fan-out 规模 = allowlist 大小（ops 经 opencode project 列表控制），**不**受 1–32 客户端列表守卫约束。

## §13 directory 发现与转发

> **v0.3.0**：allowlist 已**不再是 gate**——slimapi 不再做目录警察。本节描述 directory 数据流的现状。

- **用途**：directory 的合法性由**上游 opencode** 决定；slimapi 把客户端传入的 directory 经 `normalize_directory` 规范化后，作为 `X-Opencode-Directory` 头 + `?directory=` query **透传**给上游。slimapi 自身仅保留**结构性守卫**：显式 repeated `?directory=` 的去重保序 + `invalid_directory_count`（1–32）+ query 与 `X-Opencode-Directory` 头冲突 400（见 §7、§12）。隔离靠 stunnel mTLS（:14097）/ Tailscale ACL + 防火墙（:4097 明文直连）+ loopback upstream 等网络边界。
- **发现数据集（保留作展示 / fan-out 用途）**：`load_products(app)` → `GET /project` + 并发 `GET /project/{id}/directories`；allowlist = 各 project 的 `worktree` ∪ `directories[].path|directory`，经 `normalize_directory`（去尾斜杠，根 `/`）后写入 `app.state.directory_allowlist: set[str]`。**该 set 不再 gate 任何端点**；它支撑：
  1. `GET /slimapi/projects` 端点的展示响应；
  2. q/p **未传** directory（null）时的聚合 fan-out 范围；
  3. `/slimapi/sessions` 的 `X-Discovery-Directories` / `X-Discovery-Ready`（rev F）。
- **`load_products` 并发与完整性（rev F）**：全程 `app.state.allowlist_lock`（函数内部持锁；warm/`/projects`/q-p null-dir 三调用方共享）；顶层 `/project` 与每个 `/project/{id}/directories` 响应**必须为 list**，任一非 list → **整次刷新失败**（保留 last-known-good set + `allowlist_ready`，不通知）；成功提交后 `(new_set != old_set) OR (old_ready is False AND new_ready is True)` 才发 `server.reconfigured{reason:"discovery_changed"}`。
- **`allowlist_ready` 生命周期（rev F）**：初值 False；首次成功 `load_products` 置 True；后续失败**不**复位（last-known-good）。`ready=true` 仅表示「至少成功过一次发现」，**非**实时准确。
- **启动暖机**：lifespan smoke 后 `warm_allowlist(app)` best-effort 调一次 `load_products`；upstream 失败吞掉，不阻断启动（null 聚合 fallback 为空 fan-out → 200 空 envelope；ready 保持 False）。
- **显式刷新**：`GET /slimapi/projects` 始终走完整 `load_products` 并更新 `app.state.directory_allowlist`（失败见 §7 502/503 分裂）。
- **routeToken 路径**：`_token` 校验 HMAC + kind/requestID/sessionID/directory 后，对 payload 中的 directory 调 `normalize_directory` 透传；不再查 allowlist，故冷启动空 allowlist 时合法 token 的 dir 也直接转发给 opencode。

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
| **F4** | `CLIENT_CHANGES.md` SSE 节写 `?directory=...&sessionId=...&stream=...`，与 INTERFACE_MAP §3「参数完全移除」矛盾 | SSE 节整节重写：单一 `/slimapi/events` 无 query 参数；curated 帧类型含新增 `session.error`；同步 `lastError` 语义 | ✅ 已落地 | §3 SSE 契约（CLIENT_CHANGES 同步纪律） | `docs/specs/CLIENT_CHANGES.md` SSE 节不再出现过期 query 参数 |
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

- G1 脱敏 regex 边缘（Bearer-no-space / 自然语言 stack 误剥 / Unicode path 带空格）— defense-in-depth on loopback / Tailscale 直连，非主安全边界。
- ~~G6 mid body 形状错误（合法 JSON 但非 MessageWithParts）未 envelope 映射（保持 500）；仅 JSON 解析错映射 `upstream_error`~~。 **✅ 已修复（Batch5a / C⑨，2026-07-22）**：skeleton/full 两模式一致映射 per-mid `upstream_error` envelope（整请求仍 200）；见 CHANGELOG §Fixed。
- ~~G1 deleted flush 后迟到 `session.error` 可能重建 entry（无 durable tombstone）~~。 **✅ 已修复（Batch5a / C⑩，2026-07-22）**：`deleted_tombstones` 集合 survive pending 驱逐（`resync_all` 清理）；见 CHANGELOG §Fixed。
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

---

## §15 Opt-A partial-envelope（体验优先 patch）🆕

> 本节内容为 **v0.3.1 体验优先 patch** 的全面说明。所有行为均**加性**，不 bump `X-Slimapi-Version`（仍为 `1`）。依据：`docs/ocmar/reports/2026-07-21-ux-first-consensus-archive.md` §2 / §6 + ocdroid `docs/0.11-ux-first-joint-plan.md` rev 6 §6 B2。

### 15.1 能力头协商

客户端通过 HTTP 头 `X-Slimapi-Capabilities` 选择 opt-in partial-envelope：

| 头 | 值 | 解释 |
|---|---|---|
| `X-Slimapi-Capabilities` | `mid-partial-envelope=1` | opt-in；不传 / `=0` / 其它 → 非 opt-in |

**语法（I-R4-CAP-GRAMMAR + I-R5-CAP-DUPLICATES）**：
- 逗号切分 token，trim 两侧 ASCII whitespace。
- 每个 token 必须恰含一个 `=`，name 大小写不敏感，value trim 后字面比较。
- 未知 name / 格式错误 token 忽略，**不**导致整请求失败。
- 同一 capability 重复且值冲突 → **fail-closed**：该 capability 按非 opt-in 处理，并计入 `capabilityConflicts` 指标。
- 能力头是加性 HTTP 头，**不** bump wire API 版本。

### 15.2 六行响应矩阵（B2）

摘自 joint-plan rev 6 §6 B2，完整响应矩阵如下：

| 请求能力 | 成功 items | RequestError network mids | 其它 envelope errors | 响应 |
|---|---|---|---|---|
| **非 opt-in** | 任意 | 任意 | 任意 | **完整执行 legacy 分支**：不得先应用 Opt-A 映射；响应状态、body 与错误优先级保持部署前语义（R4-B2-OLD-SEMANTICS）。 |
| opt-in | ≥1 | 0 | 0 | **200 success envelope**：`items[]` 非空，`errors[]` 为空。 |
| opt-in | ≥1 | 任意 | 任意非空 | **200 partial**：成功 mid 进 `items[]`；network、mid-retryable、mid-terminal errors 均进 `errors[]`。客户端立即 merge items，仅重试可重试 errors。 |
| opt-in | 0 | 0 | ≥1 | **200 errors-only envelope**：保持已有 per-mid HTTP error 语义。客户端按 code 分类；若全为 mid-terminal code → terminal envelope completion；若含 `upstream_http_5xx`/429 等 mid-retryable code → 仅 bounded retry 这些 mids。 |
| opt-in | 0 | ≥1 | ≥1 | **200 errors-only envelope**：RequestError mids 映射为可重试 code，其它 errors 保持原 code；客户端仅 bounded retry network/mid-retryable mids，mid-terminal mids 不重试。 |
| opt-in | 0 | ≥1（覆盖全部请求 IDs） | 0 | **顶层 503 `upstream_unavailable`**：所有请求 IDs 均 unresolved，客户端按顶层 503 预算整批重试。 |

**规则提炼**：
1. 非 opt-in 请求**必须在 RequestError→envelope 映射之前进入 legacy 分支**，逐场景保持部署前语义。
2. opt-in 请求只要存在至少一个可交付或可分类的确定结果（成功 item，或非 RequestError 形成的 envelope error），就返回 HTTP 200 envelope。
3. 仅当全部请求 IDs 都因 RequestError 失败、无任何成功 item、也无任何其它 envelope error 时，返回顶层 503。
4. HTTP 200 ≠ 所有 mids 均成功或终态；客户端必须按 `items[]` 和每个 `errors[].code` 分别 merge、retry 或标终态。
5. `partial` 仅指 `items[]` 与 `errors[]` 均非空；errors-only 响应统一称 `errors-only envelope`（§1）。

### 15.3 C1 累计 413 一致

**关键共识（C1）**：Opt-A 变更面 = **仅 mid `httpx.RequestError` 映射为 envelope 可重试 code**。累计字节超限 `response_too_large`（顶层整请求 413）对 opt-in / 非 opt-in **一致**——保持顶层 413、`succeeded` 不输出、B1 分区恢复统一适用（**不返 200 partial**）。per-mid `message_too_large` 与 mid HTTP≥400 同理对 opt-in/非 opt-in 一致。

理由：413 = batch 过大 → 确定性分区（B1）；Opt-A = 网络瞬态 → 保 partial 防重下，失败模式与恢复策略不同；保持 413 顶层使 B1 分区契约对 opt-in/非 opt-in 统一，最小变更面，且不重开 rev-bgpt 已 CLOSED 的 B1 契约（「顶层 413 不返 partial」）。

### 15.4 Retry-After

| 层级 | 适用场景 | 值 | 客户端行为 |
|---|---|---|---|
| **顶层 HTTP**（响应 `upstream_unavailable` 503） | opt-in 全 RequestError / discover 503（opt-in） | `Retry-After: 1`（秒）或 passthrough upstream int-seconds | 优先用上游直值，否则保守 `1`s；遵 top-level 分区预算 |
| **Envelope per-mid**（`retryAfterMs`） | opt-in 且存在成功 item 或其它 envelope error | `int ∈ [0,10000]`，network `upstream_unavailable`→200ms，`upstream_http_429`/`upstream_http_5xx` → passthrough upstream Retry-After(ms,capped 10000) 或 200 | 客户端 cap 10s，backoff 200ms/400ms ±30% jitter |

**保守值**：`OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CONSERVATIVE=200`；cap `OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CAP=10000`。

> **non-opt-in 的 `upstream_unavailable` 503 从不发 Retry-After（Opt-A 信号对旧客户端不泄露）。注：`transform_busy` 503 的 `Retry-After: 2` 为既有 pool-saturation 行为（v0.2.0，与 Opt-A 无关），对 opt-in / non-opt-in 客户端一致发出，不在此约束内。

### 15.5 Feature Flag 与回滚

| 项 | 默认 | 说明 |
|---|---|---|
| **Feature flag** `OC_SLIMAPI_OPT_A_PARTIAL_ENVELOPE_ENABLED` | `1`（on） | 全局开关；关闭后所有客户端（无论能力头）走非 opt-in 旧语义 |
| **Auto-rollback flag** `OC_SLIMAPI_OPT_A_AUTO_ROLLBACK_ENABLED` | `1`（on） | 自动关闭 Feature，按阈值触发 |
| **Rollback window** `OC_SLIMAPI_OPT_A_ROLLBACK_WINDOW_SECONDS` | `3600`（1h） | 滑动时间窗用于基线比较 |
| **Min sample** `OC_SLIMAPI_OPT_A_ROLLBACK_MIN_SAMPLE` | `100` | 窗内样本数 < 100 时仅告警不自动关闭 |
| **Envelope 5xx 阈值**（零基线特例） | `OC_SLIMAPI_OPT_A_ROLLBACK_ENVELOPE_5XX_ZERO_BASELINE_RATE=0.01`（1%） | 基线率=0 时，当前窗出现率 >1% 触发关闭 |
| **Unknown code 阈值** | `OC_SLIMAPI_OPT_A_ROLLBACK_UNKNOWN_CODE_RATE=0.05`（5%） | 窗内 unknown-code 占比 >5% 触发关闭 |
| **Client expand-failure 阈值**（仅 ops 侧） | 客户端上报展开失败率 >5%（样本≥100） | 若监控到则 ops 人工介入，非自动 |

**回滚行为**：latched sticky disable（进程生命周期），进行中 operation 已 merged 的 partial items **不撤回**；manual override 通过 feature flag。

> **>2×baseline 通例已延迟**：通用 x2 历史基线阈值需存储 24h 历史 baseline，当前尚无持久化层——仅零基线特例（§15.10）有效。待历史基线采集机制就绪后再启用 >2×baseline 回滚规则。

### 15.6 Invariant

- envelope 内 `items[]` 与 `errors[]` 按 messageID **互斥**：同 messageID 不会同时出现在 items 和 errors。
- idempotent merge：同 messageID 第二次出现（如重试）应被合并/去重。
- 顺序无关：`items[]` 与 `errors[]` 各自的顺序由服务端决定（items 按 ids 去重保序，errors 按完成序），客户端不得依赖。

### 15.7 可观测性（`/slimapi/metrics`）

新增定制子对象 `batch`：

```json
{
  "batch": {
    "optA": {
      "disabledLatched": false,
      "disabledReason": ""
    },
    "counters": {
      "optInRequestsTotal": 0,
      "optInSuccessEnvelope": 0,
      "optInPartial": 0,
      "optInErrorsOnly": 0,
      "optInTopLevel503": 0,
      "legacyRequestsTotal": 0,
      "legacyTopLevel503": 0,
      "capabilityConflicts": 0,
      "capabilityMalformedTokens": 0,
      "networkMidErrorsTotal": 0,
      "unknownCodeTotal": 0,
      "modeFullRequests": 0,
      "modeSkeletonRequests": 0,
      "bytesFetchedTotal": 0,
      "bytesDeliveredSkeletonTotal": 0,
      "retryAfterMsEmittedCount": 0
    },
    "rollbackWindow": {
      "windowSeconds": 3600,
      "optInEvents": 0,
      "envelope5xxInWindow": 0,
      "unknownCodesInWindow": 0
    },
    "byteSamples": {
      "count": 0,
      "capacity": 0,
      "ratioMedian": null,
      "ratioP90": null
    }
  }
}
```

- `optA.disabledLatched` = true 表示 Opt-A 已被自动回滚关闭。
- `counters` 区分 opt-in / legagy 请求量、成功/partial/errors-only/503、能力头冲突/格式错误、network mid 失败数、unknown-code 数、mode=full/skeleton 请求数、字节 fetch/deliver 总量、retryAfterMs 发出数。
- `rollbackWindow` 记录窗内 opt-in 事件数、envelope 5xx 计数、unknown-code 计数（用于自动回滚判断）。
- `byteSamples` 用于字节比统计（匿名聚合 median/P90），`capacity` 为采样桶容量。

### 15.8 与 §2 G6 批量展开的关系

G6 批量展开 endpoint（`GET /slimapi/messages/{sid}/full?ids=`）的 `httpx.RequestError` 行为已与能力头绑定（见 §2）：非 opt-in 或全部 mids 网络失败仍保持整请求 503；opt-in 且存在至少一个成功 item 或其它 envelope error 时，mid 网络失败进入 envelope `errors[]`（可选 `retryAfterMs`），整请求仍 200。

### 15.9 与 §7 错误码的关系

`upstream_unavailable` 在非 opt-in 或全部 mids 网络失败时仍为顶层 503；opt-in 且存在成功 item 或其它 envelope error 时变为 per-mid envelope error（可选 `retryAfterMs`）。`retryAfterMs` 仅在此场景的 envelope 中出现。

### 15.10 回滚零基线特例

当 baseline rate = 0（部署后从未出现 envelope 5xx），当前 1h 窗内 envelope 5xx 出现率 >1% 且样本 ≥100 → 触发自动关闭。此条件与常规 >2×baseline 不同，目的是在初期无数据时也防御突发问题。

### 15.11 客户端侧 B1 预算（仅文档参考）

Opt-A 不改变 B1 分区预算：客户端 413 恢复仍为 halve + merge + singleton，服务器保证（顶层 413、无 partial、不泄露完成态）不变。客户端预算公式、分区节点数、瞬态重试次数、并行度、wall-clock 等参见 §5 P0-A / CLIENT_CHANGES § B1 budget。

---

> **更新纪律**：影响以上节的行为变更（能力头 grammar / 矩阵行 / 回滚阈值）须同步更新 `docs/specs/CLIENT_CHANGES.md` 相关节。

---

## §16 children 投影缓存（rev H 🆕）

> per-key 缓存 + single-flight，消除 children 透传的 N× 放大（本机观测 75k/7d）。全加性，不 bump wire。实现：`src/oc_slimapi/children_cache.py`（`ChildrenCache`，纯 asyncio，对齐 `HubRegistry` 无锁 house pattern：所有变更段无 await → 单 worker 单 loop 天然原子）。

- **键** `(parent_sid, normalized_dir)`；**粒度** = 完整 child skeleton 数组 + version。
- **TTL**：正缓存（非空）`30s`；空负缓存（上游 200 + `[]`）`5s`。惰性过期（命中时判 `fresh()`），**无** janitor task（YAGNI）。
- **single-flight**：同 key 并发请求合并到一个上游 fetch task；每个 waiter 持独立 `asyncio.Future`；leader 完成后广播结果/异常给所有 waiter。
- **generation 守卫**：每个 parentSid 单调 generation 计数；fetch 启动采样 generation，完成时仅当 `inflight.generation >= generation_of(sid)` 才写 cache（慢 fetch 不覆盖已失效条目）。响应 `X-Children-Version` = fetch 启动时的 generation（**数据与版本同源**——禁止响应时取当前 generation，那会造成 version 与数据不匹配）。
- **waiter 取消**：单 waiter 取消（客户端断开）只摘自己，**不**取消共享 fetch（fetch 不 await 任何 waiter，结构隔离；不用 `asyncio.shield`）。
- **异常**：fetch 失败 → 广播 coded 错误给所有 waiter，**不**写 cache；下个请求重试。
- **失效**（Batch4 接入；Batch3 先立 `invalidate(parent_sid)` 签名 + 单测）：同步 bump generation + 驱逐该 sid **所有 dir** 的 cache entry（in-flight **不**取消，由 generation 守卫拦截其写入）。
- **shutdown**：`aclose()` 置 `_closed`（新请求立即 503）→ 取消所有 in-flight task → `await asyncio.gather(*tasks, return_exceptions=True)`（fetch 收 cancel 后先向 waiter 广播 503 再 re-raise）；lifespan 顺序：`children.aclose()` 先于 `upstream.aclose()`（在途 fetch 用着 upstream client）。
- **容量**：`MAX_ENTRIES=4096` 软上限，写时惰性清理（先清过期，仍超则按 `fetched_at` 最旧驱逐）。
- **不变量**（rev-bgpt 深审基准）：INV-1 `_cache[key]` 与 `_inflight[key]` 不同时存在；INV-2 `entry.version == entry.generation ≤ generation_of(sid)`；INV-3 `inflight.generation` 创建后不变；INV-4 generation 单调、进程内不 reset；INV-5 waiter Future 只属一个 inflight；INV-6 `_closed=True` 后不起新 task/entry。
- **诚实限制（非阻塞）**：(1) 上游 children 数组无 body cap（`fetch_json_mapped` 整 body 解析；上线后观察 `/slimapi/metrics` 再定是否加 streaming cap）；(2) 空负缓存 5s 窗口内 fork 的新子会话不可见（Batch4 invalidate 是主路径，5s 仅 SSE 丢失兜底）；(3) generation 进程重启归零——客户端须以 `server.connected`/resync 为 server generation 边界重置 baseline（Batch4 + R3/OC-3）。

(End of file)