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

## 2026-07-18 — v1 B1（additive；不 bump `X-Slimapi-Version`）

> 本节为 v1 B1 run（spec 见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md`）落地的加性 wire 行为变更。所有条目均**加性**或为对既有契约 §11 的 bug 修正，未 bump wire API 版本。

- **status**：`GET /slimapi/sessions/{sid}/status` 错误语义分裂——upstream 404 → **404 `session_not_found`**（B1 前一律 503）；其它 4xx → **502 `upstream_http_N`**；网络/5xx/坏 JSON → **503 `upstream_unavailable`**；allowlist miss 仍 **400 `directory_not_allowed`**（body 改为结构化）。罕见边角：discover 200 但 session payload 无可用 `directory` 字段 → 503 `upstream_unavailable`。
- **projects**（行为变更，grill #5）：`GET /slimapi/projects` 任一发现步骤失败从"统一 502"分裂以对齐 §11——upstream 4xx → **502 `upstream_http_N`**；网络/5xx → **503 `upstream_unavailable`**；body 改为结构化 `{"code":…}`。**5xx/网络分支的状态码由 502 变为 503**（其余 4xx 分支只是 body 形状变化）。
- **messages**：`GET /slimapi/messages/**` 三条路径（list / since / full/{mid}）统一加 query `directory` allowlist 校验（G7-soft）；同时存在 `X-Opencode-Directory` header 且与 query 冲突 → 400。未传 query `directory` 时不拦（行为不变）。
- **messages full/{mid}**：G8 流式 cap——`client.send(stream=True)` + `read_with_cap` 边读边按解压字节累计，超 `max_message_bytes`(32 MiB) 立即中止并 **413 `message_too_large`**，`try/finally: await response.aclose()` 防连接泄漏；不再 `httpx.get()` 整 body 缓冲，单条极大消息不再打满 RSS。transform-busy 维持 **503 `transform_busy`**（与 list/since 归一；B1 前文档误写 502，代码实际一直为 503）。
- **shell/PTY deny-list**：catch-all 默认开启 deny-list——`/session/{sid}/shell`、`/pty/**`、`/api/pty/**` → **403 `shell_not_allowed`**，不连接 upstream。Ops 开关：`OC_SLIMAPI_SHELL_DENY_LIST_ENABLED`（默认 `1`=开）。WS 继续 501。**注意**：仅作 best-effort 第二道，真实隔离仍靠 stunnel mTLS + 网络边界。
- **thin-route 错误体形状**：sessions / questions 由 FastAPI 默认的 `{"detail":"…"}` 改为 **`{"code":string, "message"?:string, …}`**（与 messages/events/versioning 既有的 `{"code":…}` 形状对齐）。messages 已使用该形状，未变。
- **新增加性错误码（thin 路由）**：`invalid_directory_count`（400，questions directory 数量 1–32 守卫）；`invalid_route_token`（400，questions routeToken 校验失败）。两者均加入 `docs/v1-impl-spec.md` §11 统一错误码表，**加性，不 bump**。

## [Unreleased]

> 开发中、尚未打 tag 的变更写在这里；`release.sh` 发版时把本节内容折叠进新版本标题下。

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
