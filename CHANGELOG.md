# Changelog

本文件记录 **oc-slimapi 的接口与行为变更**，供 **ocdroid** 对接与运维查阅。

格式 loosely 遵循 [Keep a Changelog](https://keepachangelog.com/)，版本遵循 [SemVer](https://semver.org/)。

## 版本双轨（必读）

| 轨道 | 是什么 | 何时变 |
|---|---|---|
| **包版本** `vX.Y.Z`（本文件标题 + git tag + `pyproject.toml`） | 产品发版版本 | 每次 `./scripts/release.sh` |
| **Wire API 版本** `X-Slimapi-Version`（整数，见 `versioning.py` / 契约 §1） | 协议兼容门禁 | **仅破坏性** wire 变更 bump；加性变更 **不** bump |

ocdroid 对接时：

1. 读本文件了解**行为**变更；
2. 读 `docs/v1-contract.md` 了解**当前完整契约**；
3. 用 `/slimapi/health` 的 `server.api_version` / `accepted_client_versions` 做运行时兼容自检。

### 维护规约

- **每次**用户可见 / 客户端可观测的 wire 行为变更，必须在对应版本下增加条目（Added / Changed / Fixed / Removed / Security）。
- 条目写**行为与路径**，不写实现细节（避免“改了哪行 Python”）。
- 破坏性变更：同时更新 `docs/v1-contract.md` + bump wire API 版本 + 在本文件 **Changed** 中显式写 `X-Slimapi-Version` 与客户端必改点。
- 发版时由 `./scripts/release.sh` 校验本文件含有目标版本标题（见 `docs/release.md`）。

---

## [Unreleased]

> 开发中、尚未打 tag 的变更写在这里；`release.sh` 发版时把本节内容折叠进新版本标题下。

---

## [0.6.0] - 2026-07-23

> Token-stream method-B 产品化（`token_memory_limit` clear-only 不重连恢复）+ O1 正确性闭合。**全加性 wire 行为**（memory-limit resync 现向既有 subscriber 同流重发 surviving + current-key snapshot/truncated），**未 bump** `X-Slimapi-Version`（仍 `1`）。ocdroid v0.13.2 flip `TOKEN_MEMORY_LIMIT.triggersReconnect` true→false 的服务端硬前置（双边 D-MB-P 已确认接受 S1 变体）。

### Added

- **Memory-limit 同流重发 surviving parts（S-2）**：`token_memory_limit` eviction 后，sidecar 现向该 sid 的**既有 subscriber**（非新 `attach_subscriber`）同流重发剩余 live part 的 `snapshot{done:false}`，使客户端在 resync 清态后于**同一连接**重建锚点（此前仅 handshake 重发）。这是 method-B（clear-only，不重连）的产品化基础。
- **current-key 锚点闭合（MB-P-S1）**：eviction re-snapshot 现重新纳入「正在 reserve 的 current key」，经新增「截断不 drop」发射路径 `_emit_snapshot_or_truncated_nodrop`：
  - current key 帧 ≤ `max_frame_bytes` → 真 `snapshot{done:false}`（保实时动画）。
  - current key 帧 > `max_frame_bytes` → `snapshot{truncated:true}` + **不 `drop_part`**（客户端 `/since` 拉权威全文；帧走原 token stream 同 `sub.put` 通道，`event: message.part.snapshot` + `data:{…,truncated:true,done:false}` 无 text）。
  - O1 不变量继续成立：current key 绝不被 `drop_part` mid-reserve（nodrop 路径保留 LivePart，不 invalidate 调用方持有的 `live` 引用 → 无 gauge 漂移、无 orphan delta）。
  - large-part 取舍（ocdroid D-MB-P 已接受）：large current key 实时动画不可救（客户端收 `truncated` 后清该 part 停 append → 服务端后续 delta 在客户端 orphan，blank 至 `/since`）；仅 small current key 真 snapshot 分支保住动画。

### Fixed

- **O1 `_reserve→evict` re-entrancy**：current key 在 eviction re-snapshot 时不再被超帧 truncate→`drop_part`（消除调用方 stale `live` 引用导致的 `_total_live_bytes` 漂移 + orphan delta）。

### 运维/联调（非 wire 变更）

- **Debug/联调-only 内存预算 env 覆盖**：新增 `OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_BUDGET_BYTES` / `_PART_MAX_BYTES` / `_LIVE_PARTS_MAX`（可选 int，默认 unset = off），在 app lifespan startup 经 `apply_debug_budget_overrides` 覆盖 token-stream 的 LIVE 预算 cap，使 memory-limit eviction 能用小数据量触发（联调 MB-P-S1 current-key nodrop 路径）。**默认 off = 零行为变化；生产不应设置**。纯服务端阈值，非 wire 变更，不 bump `X-Slimapi-Version`。联调须走真实 app lifespan（route 单测的 `_build_app` 不读 DEBUG env）。

---

## [0.5.0] - 2026-07-23

> Token 批式 SSE（opt-in 实时流）上线。**全加性 wire 行为**，**未 bump** `X-Slimapi-Version`（仍 `1`）。设计 `docs/design-token-stream.md` v4；契约 `docs/v1-contract.md` rev J。双边联合终审 re-gate **GO 9.7**（rev-bgpt）；ocdroid 已 shipped（commit `1986567`）。

### Added

- **Token 批式 SSE（opt-in 实时流）**：新可选端点 `GET /slimapi/sessions/{sid}/stream`——生成中实时推送 in-flight text part 的渐进文本，解决「打开 busy session 看到半截且冻住」（上游 `message.part.delta` 不落库，sidecar 此前丢弃）。**全加性 wire 行为**，**不 bump** `X-Slimapi-Version`（仍 `1`）。设计权威 `docs/design-token-stream.md` v4（架构级 PASS）；契约落地 `docs/v1-contract.md` §3.x（端点+帧+gzip）+ §6.x（token T3 信封）。
  - **端点**：`GET /slimapi/sessions/{sid}/stream?directory=<optional>`；`text/event-stream`；响应头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`；版本门禁复用 `SlimapiVersionMiddleware`（无 route-level `Depends`）。directory 仅过滤进程级 GlobalBus 事件，**不开第二条上游连接**；sid 全局唯一、directory 无关（单用户 T3）。路由注册在 catch-all 反代之前。
  - **帧类型**（§5.6）：订阅首帧 `message.part.snapshot{done:false}`（累计全文锚点）+ 批式 `message.part.delta{text}`（100ms / 4KiB flush，§5.4）+ 终态 `message.part.snapshot{done:true}`（**杠杆1：仅完成 marker，无 text**——权威全文走 `/since`，取消 upstream `part.text` 终态重发）+ `message.part.snapshot{truncated:true}`（>1MiB，不静默 drop）+ `resync` + `server.connected` / `server.heartbeat`（15s）。**不发 SSE `id:` 字段**、**无 replay buffer**；`Last-Event-ID` 仅触发首帧 resync，值忽略。
  - **resync reasons**（token 流均带 `sessionID`）：`reconnect_no_replay`（上游重连）、`subscriber_backpressure`（订阅者 T3 溢出）、`token_memory_limit`（全局累加器上限）、`session_idle`（生成结束清理）、`session_deleted`（会话被删除）。单 part >1MiB 走 `snapshot{truncated:true}`（非 resync）。
  - **终态顺序不变式**（wire 强约束）：同一 `(sid,mid,pid)` 所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不再发 delta。
  - **权威对齐**：stream `snapshot{done:true}` 是「流视角完成」（**marker 无 text**）；digest + `/since` 拉取的是「持久化真值」——不一致以 `/since` 为准（幂等覆盖）。客户端可接受 digest 完成先于/晚于 token 终态帧。
  - **杠杆2：gzip 首个 SSE 例外**：token stream **默认 gzip**（流式 zlib `Z_SYNC_FLUSH`，`Content-Encoding: gzip`，按 `Accept-Encoding` 协商）。**首个 SSE gzip 例外**——此前「SSE 永不 gzip」（§9）的唯一破例；控制面 `/slimapi/events` **仍不 gzip**。实测（harness `scripts/measure_token_overhead.py`，12 trace、30 tok/s × 100ms）：原批式 ~12x → 杠杆1+2 后 gzip 中位 **1.47x**（达成 re-anchor ~1.5x 中位目标；1/3 trace <1.0x）；残余调参（flush 窗 / gzip cadence）可选 post-release。
  - **health 加性字段**：`GET /slimapi/health` 根级 `features.tokenStream:true`（Q1 冻结路径：top-level `features`，与 `sidecar`/`server`/`schema` 并列；客户端可 dual-read root/server 过渡，服务端固定 root）。`features.tokenStream` 缺/404/405 → ocdroid 降级「完成后整条出现」（零回归）。
  - **T3 独立信封（Option B 拆 4+4）**：token 订阅独立账本（`token_stream_max_subscribers=8`、`token_stream_queue_items=64`、`token_stream_buffer_bytes=512KiB/sub`、`token_stream_max_frame_bytes=1MiB`），**不**消费既有 `MAX_TOTAL_SUBSCRIBERS=16`；**内存预算 Option B**（拆 4+4，**不双计**）：`TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live）+ `TOKEN_PENDING_MAX_BYTES=4MiB`（pending）；worst-case `8 × 512KiB 订阅队列 + 4MiB live + 4MiB pending = 12MiB`（与 Option A 同上限，但 pending 独立上限更防御）。admission 失败 → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。
  - **控制面零回归**：`/slimapi/events`（控制面）一行不改；token 流消费上游 `message.part.delta`/`updated`（控制面此前丢弃），与控制面队列隔离（避免 token 高吞吐挤掉 q/p 或误触 `subscriber_backpressure`）。
  - **P1 范围**：仅 text part（reasoning / tool-input 延后 P2+）；不做二进制流。
  - **依赖与状态**：服务端 Stages A–E（§14）落地（A 地基 9.5 / B 生命周期 9.5 / C flush 9.5 / D 端点 9.6 / E 文档+预算 4+4）；本版本随 0.5.0 出货，双边联合终审 re-gate GO 9.7。ocdroid 配合清单见 `docs/CLIENT_CHANGES.md`「Token stream SSE」节。批式参数（`TOKEN_FLUSH_SECONDS`/`TOKEN_FLUSH_BYTES`）为服务端 env knob，**不进 wire**，ocdroid 无需跟随调整。

## [0.4.0] - 2026-07-22

> 透传收敛 + 重构（Batch 0–5）。多批加性 wire 行为变更，**未 bump** `X-Slimapi-Version`（仍 `1`）。契约权威 `docs/v1-contract.md` rev I。

### Added
- **batch status 错误边界（Batch1）**：`GET /slimapi/sessions/status`（批量）补齐 §7 coded-error——upstream 网络错 / 5xx / 坏 JSON / 非 dict → **503 `upstream_unavailable`**；4xx（含 404；batch 无 path sid → **非** `session_not_found`）→ **502 `upstream_http_N`**（原裸透传：网络错冒泡 500、4xx/5xx 原样透传）。
- **messages 初始 send 错误边界（Batch1）**：`GET /slimapi/messages/{sid}`(list) / `/since/{ts}` / `/full/{mid}` 初始 `upstream.send` 的 `httpx.RequestError` → **503 `upstream_unavailable`**（原逃逸 500）。
- **children 投影端点（Batch3）**：新端点 `GET /slimapi/sessions/{sid}/children`——child skeleton **数组** + 响应头 `X-Children-Version`；sid 感知错误映射（404→`session_not_found`、4xx→`upstream_http_N`、5xx/网络/坏JSON/非list→`upstream_unavailable`）；slimapi 侧稳定排序 `time.created DESC, id ASC`；per-key 缓存 + single-flight（契约 §16）。
- **sessions 列表 hint（Batch3）**：`GET /slimapi/sessions` 每条加性 `childrenIDs[]` + `childrenComplete`（纯缓存回填、budget 32、超限省略，杜绝 N× 放大）。
- **session.created→父 digest childrenVersion（Batch4，X-main 失效）**：hub 新增 `session.created` 处理——子 `info.parentID` → `children_cache.invalidate(parentID)`（bump generation + 驱逐父 cache）+ **父** digest 加性字段 `childrenVersion`（= parentSid 单调 generation，与 `X-Children-Version` 同源）；客户端 digest 收更大值 → 重拉 `/slimapi/sessions/{sid}/children`（缓存已 fresh）。`session.created` 仍**不**经 `/slimapi/events` 原样转发（curated stream 不变；X-main childrenVersion 是唯一子会话变更信号）。

### Changed（内部，无 wire 变更——仅记录）
- `TransformPool.snapshot_metrics()` 公开 API 取代 `HubRegistry` 直读 `_semaphore._value/_waiters`（Batch2；metrics wire 输出形状不变）。

### Fixed
- **G6 mid 形状错误 envelope 收敛（Batch5a，C⑨）**：`GET /slimapi/messages/{sid}/full?ids=`（G6 批量）单个 mid 返回**合法 JSON 但非 MessageWithParts 形状**（非 dict / 缺 `info`·`parts` / 字段类型错）时，不再逃逸 **500**（skeleton 模式）或塞入 `items[]`（full 模式）；改为**两模式一致**映射到 per-mid `errors[]` 的 `upstream_error`（整请求仍 **200**），兑现 batch partial-failure 语义。复用既有 `upstream_error` 码——**无新错误码、不 bump `X-Slimapi-Version`**。
- **deleted durable tombstone（Batch5a，C⑩）**：`session.deleted` digest 被 flush 驱逐后，迟到的 `session.error` 不再经 `setdefault` 重建 sticky `lastError`（已删除会话错误"复活"）；新增 `deleted_tombstones` 集合（survive pending 驱逐；`resync_all` 清理）。digest 流上已删除会话不再出现伪 `lastError`。

## 2026-07-18 — v1 B1（additive；不 bump `X-Slimapi-Version`）

> 本节为 v1 B1 run（spec 见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md`）落地的加性 wire 行为变更。所有条目均**加性**或为对既有契约 §11 的 bug 修正，未 bump wire API 版本。

- **status**：`GET /slimapi/sessions/{sid}/status` 错误语义分裂——upstream 404 → **404 `session_not_found`**（B1 前一律 503）；其它 4xx → **502 `upstream_http_N`**；网络/5xx/坏 JSON → **503 `upstream_unavailable`**；allowlist miss 仍 **400 `directory_not_allowed`**（body 改为结构化）。罕见边角：discover 200 但 session payload 无可用 `directory` 字段 → 503 `upstream_unavailable`。
- **projects**（行为变更，grill #5）：`GET /slimapi/projects` 任一发现步骤失败从"统一 502"分裂以对齐 §11——upstream 4xx → **502 `upstream_http_N`**；网络/5xx → **503 `upstream_unavailable`**；body 改为结构化 `{"code":…}`。**5xx/网络分支的状态码由 502 变为 503**（其余 4xx 分支只是 body 形状变化）。
- **messages**：`GET /slimapi/messages/**` 三条路径（list / since / full/{mid}）统一加 query `directory` allowlist 校验（G7-soft）；同时存在 `X-Opencode-Directory` header 且与 query 冲突 → 400。未传 query `directory` 时不拦（行为不变）。
- **messages full/{mid}**：G8 流式 cap——`client.send(stream=True)` + `read_with_cap` 边读边按解压字节累计，超 `max_message_bytes`(32 MiB) 立即中止并 **413 `message_too_large`**，`try/finally: await response.aclose()` 防连接泄漏；不再 `httpx.get()` 整 body 缓冲，单条极大消息不再打满 RSS。transform-busy 维持 **503 `transform_busy`**（与 list/since 归一；B1 前文档误写 502，代码实际一直为 503）。
- **shell/PTY deny-list**：catch-all 默认开启 deny-list——`/session/{sid}/shell`、`/pty/**`、`/api/pty/**` → **403 `shell_not_allowed`**，不连接 upstream。Ops 开关：`OC_SLIMAPI_SHELL_DENY_LIST_ENABLED`（默认 `1`=开）。WS 继续 501。**注意**：仅作 best-effort 第二道，真实隔离仍靠 stunnel mTLS + 网络边界。
- **thin-route 错误体形状**：sessions / questions 由 FastAPI 默认的 `{"detail":"…"}` 改为 **`{"code":string, "message"?:string, …}`**（与 messages/events/versioning 既有的 `{"code":…}` 形状对齐）。messages 已使用该形状，未变。
- **新增加性错误码（thin 路由）**：`invalid_directory_count`（400，questions directory 数量 1–32 守卫）；`invalid_route_token`（400，questions routeToken 校验失败）。两者均加入 `docs/v1-impl-spec.md` §11 统一错误码表，**加性，不 bump**。

## [0.3.1] - 2026-07-21

> 体验优先 patch（Opt-A partial-envelope）。**全加性** wire / 部署行为，**未 bump** `X-Slimapi-Version`（仍为 `1`）。移交：`docs/ocmar/reports/2026-07-21-ux-first-consensus-archive.md`。

### Added

- **能力头 `X-Slimapi-Capabilities`（Opt-A）**：客户端 opt-in partial-envelope 的加性 HTTP 头。语法：逗号切分 token，trim，单 `=`，name 大小写不敏感，value 字面比较；未知/格式错误 token 忽略；重复值冲突 fail-closed。**Additive，未 bump**。
- **B2 六行响应矩阵（Opt-A）**：success / partial / errors-only / terminal-envelope-completion / top-503 全场景。invariant（items/errors 按 messageID 互斥幂等）。**Additive，未 bump**。
- **Retry-After**：顶层 HTTP `Retry-After`（秒）+ per-mid envelope `retryAfterMs`（ms，≤10000）。保守值 200ms，cap 10s。**Additive，未 bump**。
- **Feature flag + 回滚阈值**：`OC_SLIMAPI_OPT_A_PARTIAL_ENVELOPE_ENABLED`（默认 1）；auto-rollback 1h 窗口，5xx >2×baseline 或 baseline=0→>1%、unknown-code >5%、min sample 100、latched sticky disable、in-flight not reverted、manual override。**Additive，未 bump**。（零基线 >1% 活跃；>2×baseline 通例暂延迟，待历史基线采集就绪）
- **`/slimapi/metrics` batch ledger**：新子对象 `batch`，含 `optA{disabledLatched,disabledReason}`、`counters{...}`、`rollbackWindow{...}`、`byteSamples{...}`。**Additive，未 bump**。
- **G-F1 fixtures**：循环触发 cursor-walk 降级（复用 `GET /slimapi/messages/{sid}`），事件驱动 + 15min 最小间隔 + single-flight。**Additive，未 bump**。
- **S-C `/slimapi/metrics` byte-ratio 聚合**：`batch.byteSamples` 新增 `ratioMedian`/`ratioP90`（匿名 median/P90 的 skeleton-delivered/fetched 字节比率；fetched≤0 的样本不计）。**Additive，未 bump**。
- **S-E deployment revision**：`OC_SLIMAPI_DEPLOYMENT_REVISION(_FILE)` env-or-file 注入 → `health` 响应 `server.deploymentRevision`（可选；未设置时整个字段省略）。**Additive，未 bump**。

### Changed

- **C1 累计 413 一致**：累计字节超限 `response_too_large`（顶层 413）对 opt-in / 非 opt-in **一致**，不返 partial。per-mid `message_too_large` 同理。**Additive 行为对齐，未 bump**（非 opt-in 已有行为不变）。
- **非 opt-in 零改变**：旧客户端（不传能力头）所有行为保持部署前语义（legacy 等价）。
- **G-ACL 部署姿态**：`0.0.0.0:4097` + `:14097` mTLS 隧道（stunnel `requireCert=yes verifyChain=yes`，复用既有证书）为**用户接受的稳态**；直接 `:4097` 明文访问由网络边界（防火墙/Tailscale ACL）阻断，外部客户端经 `:14097` mTLS。代码无需改（`config.py` 默认 `127.0.0.1`；部署覆盖为 `0.0.0.0` 由 ops 控制）；边界验证 runbook 见 `docs/operations.md` §10。**无 wire 变更，无代码变更**——仅 posture 文档更新。

### Fixed

- **Legacy ledger 记录完整性**：`/slimapi/metrics` 的 `counters` 对象此前仅区分 opt-in/legacy 总量；现增加 `capabilityConflicts`、`capabilityMalformedTokens`、`networkMidErrorsTotal`、`unknownCodeTotal` 等细项，与 Opt-A 回滚联动。

---

## [0.3.0] - 2026-07-21

> ocdroid v0.11.7 反馈 rev F / 实现 v6 + 接入放开。**全加性** wire / 部署行为，**未 bump** `X-Slimapi-Version`（仍为 `1`）。移交：`docs/ocmar/reports/2026-07-21-v0.11.7-feedback-handoff.md`。

### Added

- **`GET /slimapi/sessions` 完整性 + discovery readiness 响应头**（ocdroid v0.11.7 §1）：200 成功路径加 `X-Complete`（本页未满：`len < limit`；**不得**当权威全集）、`X-Discovery-Directories`（`len(directory_allowlist)`，非 query 命中数）、`X-Discovery-Ready`（是否存在 last-known-good 发现快照）。502/503 等错误路径**不**发三头。非 list 上游 body → 503 `upstream_unavailable`。**加性，未 bump** `X-Slimapi-Version`。
- **SSE `server.reconfigured`**（ocdroid v0.11.7 §3）：payload `{reason:"discovery_changed", at:<epoch-ms>}`。仅 discovery 变更时直推——`load_products` 成功后 `(new_set != old_set) OR (old_ready is False AND new_ready is True)`。上游重连/掉线/背压/Last-Event-ID **仍发既有 `resync`**（路径不动，无双重 cold-start）。无活跃订阅者时 no-op。客户端收到应作废本地 commitToken 并 cold-start。**加性，未 bump**。
- **`/slimapi/health` + `/slimapi/ready` schema 三键**（ocdroid v0.11.7 §4）：`schema.version` / `schema.clientMin` / `schema.clientMax`（从 config 读）；旧 `server.api_version` / `server.accepted_client_versions` 保留。定位为**诊断用 wire 范围回显**（非 feature discovery）。**加性，未 bump**。
- **`load_products` 并发护栏 + 双层 shape 守卫**：`app.state.allowlist_lock` 全程串行；顶层 `/project` 与每个 `/project/{id}/directories` 响应必须为 list，任一非 list → 整次刷新失败（保留 last-known-good set/`allowlist_ready`，不通知）。`allowlist_ready` 首次成功置 True，后续失败不复位。

### Changed

- **`:4097` 放开为明文直连入口（可绑 `0.0.0.0`）**：`OC_SLIMAPI_HOST` 接受值由 `{127.0.0.1, ::1, localhost}` 扩展为 `{127.0.0.1, ::1, localhost, 0.0.0.0}`。绑 `0.0.0.0` 后客户端可通过 Tailscale 地址**直接**访问 `:4097`，**不强制 mTLS**——安全边界由 Tailscale ACL / 主机防火墙负责。`:14097` 仍为推荐的 mTLS 入口；任意 routable host（如 `192.168.x.x`）仍被 `config.validate()` 拒绝。**Upstream SSRF guard 不放松**：`OC_SLIMAPI_UPSTREAM` 仍必须为 fixed loopback HTTP，与 host 选择无关。`X-Slimapi-Version` 版本门禁未改动。**加性，未 bump** `X-Slimapi-Version`。

- **完全移除 directory allowlist gate（slimapi 不再做目录警察）**：`require_directory()` 已删除；directory 不再因 ∉ allowlist 返 400 `directory_not_allowed`。涉及端点：`/slimapi/sessions`（列表）、`/slimapi/sessions/status`（批量）、`/slimapi/sessions/{sid}/status`（早已不 gate）、`/slimapi/questions`、`/slimapi/permissions`、`/slimapi/messages/**`（list/since/full/full?ids=）、routeToken 写端点（reply/reject/permission）。所有 directory 现统一行为：经 `normalize_directory` 规范化后作为 `X-Opencode-Directory` 头 + `?directory=` query **透传**给上游 opencode，由 opencode 自行决定能否服务。slimapi 保留：`normalize_directory`、显式 repeated `?directory=` 的去重保序 + `invalid_directory_count`（1–32 结构限制）、query `directory` 与 `X-Opencode-Directory` 头冲突 → 400 `directory_not_allowed`（结构性歧义，仍由 slimapi 拒绝）、`X-Slimapi-Version` 版本门禁、upstream 必须 loopback 的 SSRF guard。`/slimapi/projects` 仍返回发现到的项目；`app.state.directory_allowlist` 数据结构保留作 `/projects` 展示与 q/p null-directory 聚合 fan-out 用途，**不再作 gate**。**加性，未 bump** `X-Slimapi-Version`（错误码 `directory_not_allowed` 保留作 query/header 冲突场景，未删除）。

### Fixed

- **契约 §2 `start` 语义 stale 勘误**：`GET /slimapi/sessions` 的 `start` 是上游 legacy 的 epoch-ms **时间戳水位**（`time_updated >= start`），**非 offset 偏移分页**；上游不暴露前向 cursor、不保证 id tie-break。文档与实现透传行为对齐（代码未改透传逻辑）。
- **partId 稳定性文档 ratify**：schema-valid 下 thin/`/full` 跨端点 part `id` 稳定；`thin_placeholder_*` 为 message-level UI 兜底，不参与 `/full` part-level 对齐（客户端应 message-level 整体替换）。去 placeholder 转 backlog。

---

## [0.2.2] - 2026-07-20

> v0.2.1 三审门控（rev-gpt 9.0 / rev-glm 9.0 / rev-grok 9.3 → 均 NEEDS-FIX）发现的发布级文档 stale 修复 + 2 回归测试增强。**无 wire 行为变更**（纯文档一致性 + 测试加固），`X-Slimapi-Version` 仍为 `1`。

### Fixed

- **v1-contract.md 修订日志 rev C 测试数 stale**（`197`→`200`，对齐 §14.6 / impl-status / check.sh 实跑 202）+ **§14.6 测试拆解算术**（"+10 各分项"对齐：messages 1 + sessions 3 + 坏 JSON 2 + q/p scope 3 + normalize-dedup 1）。
- **release.md §5 当前语义示例**：`time.updated >= ts` → `(info.time.updated or info.time.created) >= ts`；**v1-contract-implementation-status** 审计 commit ref 刷新（`9373550` working tree → main 累计 `0752beb`+`340378b`）。
- **messages.py `messages_since` docstring**：ts 地板字段 `time.updated` → `(time.updated or time.created)`。
- **CHANGELOG `[0.1.0]` 历史条目**加 v0.2.1 勘误脚注（避免后人按历史条目重新引入 no-op）。
- **CHANGELOG `[0.2.1]` Fixed** 补 q/p 规范化去重条目（`invalid_directory_count` 守卫语义改为按规范化后 fan-out 数，客户端可观测）。

### Added

- **2 回归测试**（rev-glm + rev-grok 🟡 共识缺口）：q/p 全 dir 失败 503 **不含 `scope`**（`test_questions_all_directories_fail_returns_503_without_scope`）；`/sessions` list upstream 404 → **502 `upstream_http_404`**（非 `session_not_found`，`test_sessions_list_upstream_404_returns_502_upstream_http_404`）。

---

## [0.2.1] - 2026-07-20

> 本批次（2026-07-20 rev C）ratify ocdroid 契约遗留 3 缺口（**Gap1** 等时间戳 tie-break + **Gap2** 空/失败区分 + **Gap3** `/since/0` cursor drain）+ 查证中发现的 2 个 pre-existing 真 bug（`/since` 过滤 no-op + `/sessions` 列表 §7 偏离）+ 2 处防御缺口（q/p 规范化去重 + `/sessions` 坏 JSON→503）。全加性，**不** bump `X-Slimapi-Version`（仍为 `1`）。逐条对照见 `docs/v1-contract.md` §14.6。

### Added

- **q/p envelope `scope` 字段**（ocdroid 缺口 2）：`GET /slimapi/questions` / `/permissions` 的 200 响应加 `scope: {directories: N}`（N = 本次请求有效 scope 的 dir 数：null 路径=allowlist 大小，显式路径=去重后 dir 数）。`N == 0` = scope 未就绪（allowlist 空，sidecar 启动早于 opencode）；`N > 0 && items == []` = scope 就绪、权威空。客户端据此决定冷启动是否清本地 stale。加性，不破坏 F1（仍 200 + items/errors）。

### Changed

- **`/since/{ts}` 时间过滤真正生效 + tie-break 规则**（ocdroid 缺口 1）：`_item_updated` 从只读 `info.time.updated`（opencode v1.18.3 无此字段）改为读 `info.time.updated or info.time.created`，与 digest `updatedAt` 推导对齐。修复前 `>= ts` 过滤是 no-op（对任何 ts 返回最新 N 条）；修复后返回真过滤子集。客户端 per-session watermark 升级为 `(updatedAt, messageID)` 二元组字典序（等时间戳 tie-break，复用上游单调 `MessageID`，对齐 `(time_created DESC, id DESC)` 全序）。

### Fixed

- **`/slimapi/sessions` 列表 §7 偏离**（ocdroid 缺口 2）：upstream 4xx/5xx 不再原样透传 body、网络错（`httpx.RequestError`）不再落 FastAPI 默认 `{"detail":...}` 500；统一对齐 sibling（`/sessions/{sid}/status`、`/projects`）：4xx→502 `upstream_http_N`、5xx/网络→503 `upstream_unavailable`，body 为 `{"code":...}`。补 3 测试（原零覆盖）。
- **契约 §5 字段勘误 + `/since/0` 推荐**（ocdroid 缺口 1 + 3）：§5 原述 `time.updated >= ts` 引用了 v1.18.3 不存在的 message 级字段，勘误为 `(info.time.updated or info.time.created) >= ts`；并补注无 watermark 的初始拉取推荐 cursor drain（`?before` 分页）而非 `/since/0`。
- **q/p 显式 directory 规范化后去重**（rev-13 review 捕获；客户端可观测）：显式 `?directory=` 先 `normalize_directory` 再去重，消除 `/app`+`/app/` 双 fan-out；`invalid_directory_count` 守卫语义随之改为按**规范化后 fan-out 数**判定（33 个 raw dir 去重 ≤32 → 200，旧 raw-dedup 行为 → 400）。

---

## [0.2.0] - 2026-07-20

> 本批次（2026-07-20）所有变更加性，**不** bump `X-Slimapi-Version`（仍为 `1`）。ocdroid《slimapi 接口评审报告》原始发现 F1–F5 + §5 文档建议全部落地；本仓扩展 G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步）一并实现；另修 2 个 pre-existing SSE 生命周期 bug + G1 `error.name` 类型防御。逐条对照见 `docs/v1-contract.md` §14。

### Added

- **F1 `/slimapi/questions` + `/permissions` null directory 聚合**：`directory` 由必填改可选；不传时聚合 allowlist 全部 dir。消除 cold-start 422。
- **F3 allowlist 启动暖机**：`lifespan` 启动主动 `load_products`（best-effort）。
- **G1 错误可见性**：`session.digest` 加 `lastError?` 字段（`{name,message,at}`，sticky，`status=busy` 清除，`deleted` 后不保留）；新 `event: session.error` session-less 帧（无 sid 时立即直推）；`MessageAbortedError` 静默过滤；message 脱敏（首行/剥路径/剥 stack/剥 secret/截断 512）。
- **G6 批量展开**：`GET /slimapi/messages/{sid}/full?ids=`（1–20 mid，discover 先行，mid 级 envelope errors[]，累计 413）。
  - **discover 错误分裂**（top-level，0 mid 拉取）：404→`session_not_found`；其它 4xx→502 `upstream_http_N`；5xx / 网络 / 坏 JSON→503 `upstream_unavailable`。
  - **mid 级 envelope**（整请求仍 200）：`message_not_found`(mid 404) / `upstream_http_N`(mid ≥400 含 5xx，**不**升级整请求) / `message_too_large` / `upstream_error`(mid 2xx 坏 JSON)。
  - **整请求终端**：`invalid_ids`(400) / 累计 413 `response_too_large` / mid 网络 503 `upstream_unavailable`（**优先于** 413）/ skeleton 池饱和 503 `transform_busy`+`Retry-After`。
  - **定序**：`items[]` = ids 去重保序（保证）；`errors[]` = 并发完成序（**不**保证）。

### Changed

- **F2 `/slimapi/sessions/{sid}/status` 放宽 allowlist**：sid 自洽即能力，`normalize_directory` 不 gate；与 messages soft 对齐。批量 status 不变。
- **F3 routeToken 应答 allowlist 刷新**：`_token` 走 `require_directory`（miss 自动刷新）。

### Fixed

- **F4 文档**：`CLIENT_CHANGES.md` SSE 节同步 INTERFACE_MAP §3。
- **F5 文档**：契约 §1 `accepted:[1,1]` 闭区间说明。
- **§5 文档**：契约新增 directory 三态语义表 + allowlist 机制节 + cold-start 暖机 + CLIENT_CHANGES 同步纪律。
- **D1–D8 文档**：design-v2（§1.4 limit 422 / §1.7 q/p 可选 / §1.9 status / §1.10 删 session.error / §3 SSEClient + 删 thin.session.dirty）、impl-spec（B0 决策记录 GO / G1·G6 标已实现）、AGENTS.md（对齐版本 v1.18.3）、契约 §11 标 closed。
- **版本报告**：`/slimapi/health` 的 `sidecar.version` 与 OpenAPI `version` 改从 `importlib.metadata` 读取（单一真源 = `pyproject.toml`），随 `release.sh` 自动更新；此前 `__version__` 与 `app.py` 各自硬编码 `0.1.0`，发版后 health 不刷新。

### Removed

---

## [0.1.0] - 2026-07-18

首个可交付 v1 收敛版。Wire API 版本 = **1**（`X-Slimapi-Version: 1`）。

### Added

- **版本门禁**：所有 `/slimapi/**`（含 SSE）必须带整数头 `X-Slimapi-Version: 1`；缺/非整数 → `400 version_required`；越界 → `400 version_incompatible`（带 `client`/`accepted`）。
- **健康检查**：`GET /slimapi/health`、`GET /slimapi/ready`（均受版本门禁）；health 暴露 `server.api_version`、`accepted_client_versions`、`schema.degraded` 等。
- **会话 / 项目 / 状态**：`GET /slimapi/sessions`、`GET /slimapi/projects`、`GET /slimapi/sessions/status`、`GET /slimapi/sessions/{sid}/status`（骨架裁剪 + directory allowlist）。
- **消息（扁平路径，契约 §2）**：
  - `GET /slimapi/messages/{sid}` — 骨架分页（`?limit`/`before`/`mode=skeleton|full`）。
  - `GET /slimapi/messages/{sid}/since/{ts}` — **A2=A**：返回 `info.time.updated >= ts` 的骨架（含边界）；`?limit`（默认 50，上限 200）+ `?before`；多页扫描共用单 transform admission + 累计字节预算；超限 → `413 response_too_large`。 _(勘误于 v0.2.1：opencode v1.18.3 无 message 级 `info.time.updated`，实读 `created`；见 `[0.2.1]` Changed）_
  - `GET /slimapi/messages/{sid}/full/{mid}` — 单条按需展开（默认 `mode=full`）。
- **分页游标**：`X-Next-Cursor` = opencode 响应 **`Link: rel="next"`** 中 `before=` 的 **opaque 字符串原样透传**（不 decode/re-encode）。客户端翻页：`?before=<X-Next-Cursor>`。opencode cursor 为 base64url；含 percent-encoding 的非规范 cursor 经 FastAPI/httpx 会规范化（见契约实现边界）。
- **SSE 策展**：`GET /slimapi/events` — 单上游 `/global/event`；吐 `session.digest`（debounce）+ question/permission 直推 + `server.connected`/`heartbeat`/`resync`；丢弃 text.delta / part.* / tool.*。
- **digest `archived`**：`session.updated` 的 `info.time.archived` → digest 字段 **`archived` = epoch ms int**（粘滞；无值则不输出该键）。客户端据此本地隐藏 ses。
- **T3 资源限制**：订阅上限（per-directory / total）、每 subscriber buffer 字节预算与单帧上限、溢出立即清 + `resync{reason:subscriber_backpressure}` + STOP；超限建立订阅 → `503 sse_subscriber_limit_*` + `Retry-After`。
- **指标**：`GET /slimapi/metrics`（订阅者 / hub / transform 摘要）。
- **q/p 聚合与写**：`GET /slimapi/questions`、`GET /slimapi/permissions`；`POST .../reply|reject`、`POST /slimapi/sessions/{sid}/permissions/{pid}`（routeToken）。
- **gzip §9**：JSON 路由按 `Accept-Encoding` 协商 gzip（含错误体 `error_response` 可选协商）；SSE **永不** gzip。
- **catch-all**：非 `/slimapi/**` 流式反代 opencode（写路径客户端自带 `X-Opencode-Directory`）。

### Changed

- （相对早期原型）消息路径由嵌套 `/slimapi/sessions/{sid}/messages/...` **改为** 契约扁平路径（见上）。
- （相对早期原型）`/since` 由 anchor/messageID 探测改为 **`/since/{ts}` 时间戳锚点**；不再使用 `X-Sync-Snapshot-Latest` / `X-Anchor-Found` / `409 resync_required`（锚点语义）。
- skeleton 模式下 **不再** 把上游 `Link` 头原样复制给客户端；改为解析后下发 `X-Next-Cursor`。

### Removed

- **`GET .../latest-message-id`**：契约 §2 未纳入；客户端未使用，已删除。冷启动 / resync 用 sessions + q/p + `/since/{ts}` + SSE digest，不再需要单独 ID 探针。

### Fixed

- SSE 慢消费者：queue/buffer 溢出改为**立即清**并下发 `resync` + STOP（不再尾部排 STOP 后继续灌旧帧）。
- 测试卫生：hub 订阅 teardown 避免 `Task was destroyed but it is pending`。

### Security

- sidecar **仅 loopback** 监听；公网认证依赖 stunnel mTLS（双入口 14096 直连 / 14097 经 sidecar）。
- routeToken：HMAC 签名、绑 kind+requestID+sessionID+directory、约 1h 过期；secret 经 `OC_SLIMAPI_ROUTE_SECRET_FILE` / systemd credential，**禁止**入库。

---

## 链接

- 契约：[`docs/v1-contract.md`](docs/v1-contract.md)
- 发版：[`docs/release.md`](docs/release.md)
- 客户端清单：[`docs/CLIENT_CHANGES.md`](docs/CLIENT_CHANGES.md)
