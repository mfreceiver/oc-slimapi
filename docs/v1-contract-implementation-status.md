# oc-slimapi v1 契约实现状态报告

- **基准契约**：[`docs/specs/v1-contract.md`](specs/v1-contract.md)（唯一 wire 基准）
- **审计对象**：oc-slimapi 仓库 @ main（含至 **v0.4.0** / 契约 rev I 累计；现行状态见 `docs/specs/v1-contract.md` 修订日志）
- **审计方法**：逐条对照契约 §0–§11，交叉核验源码（`src/oc_slimapi/**`）+ 测试（`tests/`，**544 passed** @ 2026-07-22 v0.4.0）+ 配套文档（v1-impl-spec / INTERFACE_MAP / CHANGELOG / CLIENT_CHANGES）
- **状态图例**：✅ 完全实现 · 🟡 部分实现 · ⚫ 未实现 · 🔄 变更（相对契约前的行为变化）
- **给**：ocdroid 项目组

---

## 速查总表

| 契约节 | 主题 | 状态 | 备注 |
|---|---|---|---|
| §0 | 范围与架构 | ✅ | FastAPI+httpx+orjson+uvicorn 单 worker，loopback，不读 SQLite |
| §1 | 版本契约 | ✅ | `SERVER_API_VERSION=1`，`ACCEPTED_CLIENT_VERSIONS=(1,1)`，门闩齐全 |
| §2 | 端点表（16+ 路由） | ✅ + 🔄 | 全部就绪；B1 + 2026-07-19 加性变更（F1/F2/G6 等，见下） |
| §2 写路径 B2 | routeToken | ✅ | issue/verify + directory 注入 + 过期透明；**v0.3.0** allowlist gate 全面移除，`_token` 仅 normalize 后透传 |
| §3 | SSE 契约 | ✅ + 🔄 | digest(含 archived + **lastError?**) / **session.error G1** / asked 直推 / heartbeat / resync 全覆盖 |
| §4 | 冷启动 & resync | ✅ | sidecar 提供所需端点；resync 流程在客户端 |
| §5 | 拉消息 A2=A | ✅ + 🔄 | `/since/{ts}` 语义 `(info.time.updated or info.time.created) >= ts` + 分页（v0.2.1 勘误 + tie-break `(updatedAt,messageID)`） |
| §6 | 资源限制 T3 | ✅ | 订阅上限 / 字节预算 / 溢出清 queue / `/metrics` |
| §7 | 错误码 | ✅ + 🔄 | B1 加性扩充（thin 错误体 `{"detail":...}`→`{"code":...}`） |
| §8 | 客户端最小集 | ✅ | sidecar 全部就绪；剩余在 ocdroid 侧 |
| §9 | gzip | ✅ | 所有 JSON 路由转发 `accept-encoding`（含 health/ready） |
| §10 | 延后项 | ⚫ | 显式非 v1（见末节） |
| §11 | v1 待补缺口（5 项） | ✅ | 全部闭环（archived / /since / T3 / gzip / 写路径核验） |

**结论：契约 v1 范围 100% 落地。** 无部分实现 / 未实现项（§10 显式延后除外）。B1 阶段引入若干**加性 wire 变更**（不 bump `X-Slimapi-Version`），ocdroid 须识别——见「ocdroid 侧须关注」节。

---

## 访问拓扑与连接方式（ocdroid 接入）

sidecar 监听 host 范围由 `config.validate()` 控制：loopback（`127.0.0.1`/`::1`/`localhost`）或 `0.0.0.0`（明文直连入口）。upstream 仍强制 fixed loopback HTTP（SSRF guard 不放松）。两条+远程访问路径：

### 1. 本机 / 测试（明文 loopback，仅开发调试）
| 目标 | URL | 说明 |
|---|---|---|
| sidecar 省流 API | `http://127.0.0.1:4097/slimapi/**` | 须带 `X-Slimapi-Version: 1` 头 |
| opencode 直连（绕过 sidecar） | `http://127.0.0.1:4096/...` | legacy 写路径调试用 |

绑 loopback 时仅本机可达。绑 `0.0.0.0` 时 `:4097` 成为**明文直连入口**——thin routes 无自身认证，安全模型依赖前置（stunnel mTLS :14097 推荐）或后置（Tailscale ACL / 主机防火墙）。除 loopback 与 `0.0.0.0` 外的任意 routable host（如 `192.168.x.x`）仍被 `config.validate()` 硬拒——这是**故意的安全 guard**，避免误绑到非预期的接口。

### 2. 远程 / 生产（mTLS via stunnel，ocdroid 实际接入路径）
| 路径 | URL | 链路 |
|---|---|---|
| **slim 省流**（默认） | `https://opencode.vectory.cn:14097/slimapi/**` | mTLS 终结 → `127.0.0.1:4097` (sidecar) → `127.0.0.1:4096` (opencode) |
| opencode 直连（回退） | `https://opencode.vectory.cn:14096/...` | 同 mTLS 信任织物，绕过 sidecar |

- **服务器证书**：SAN = `opencode.vectory.cn`（hostname-bound，端口无关；同一证书服务 14096 与 14097）。
- **客户端**：须持有本 CA 签发的 client cert/key + trust 本 CA；无证书在 TLS 握手阶段即拒（`requireCert = yes`、`verifyChain = yes`）。
- **DNS / 穿透**：`opencode.vectory.cn` 解析到 FRP 节点（内网穿透）。14096 已对外暴露；**14097 须在 FRP 节点上加配等价的端口映射规则**（本机 stunnel 已 `listen 0.0.0.0:14097`，FRP 规则是部署侧动作，非本仓库代码）。

### 当前部署实例（commit `7843f8e`，2026-07-18）
- sidecar：`127.0.0.1:4097`，systemd user service `oc-slimapi.service`（active，B1 新代码已生效）。
- opencode：`127.0.0.1:4096`（独立进程，opencode 自己监听 `0.0.0.0:4096`）。
- stunnel：`0.0.0.0:{14096, 14097}`，systemd user `stunnel-opencode.service`（mTLS；证书在 `~/.config/stunnel/certs/`，仓库不托管）。
- **mTLS 端到端自测通过**（14097）：`/slimapi/health` → 200；`/slimapi/sessions/ses_x/status` → 404 `{"code":"session_not_found",...}`；`POST /session/ses_x/shell` → 403 `{"code":"shell_not_allowed"}`。负向：无 client cert → TLS 握手即拒 ✓。

> ocdroid 侧接入步骤：把 `~/.config/stunnel/certs/{ca-cert.pem, client-cert.pem, client-key.pem}` 部署到设备 → 客户端 HTTPS 客户端配置 `cert=(client-cert.pem, client-key.pem)`、`verify=ca-cert.pem` → 指向 `https://opencode.vectory.cn:14097`。

---

## 逐节审计

### §0 范围与架构 — ✅ 完全实现
- 纯 HTTP sidecar，FastAPI + httpx + orjson + uvicorn，单 worker，host ∈ `{127.0.0.1, ::1, localhost, 0.0.0.0}`（`config.validate()` 强制；`0.0.0.0` 为明文直连入口，远程暴露依赖 Tailscale ACL / 防火墙；upstream 必须保持 fixed loopback HTTP）。
- 仅 legacy `/session/**` HTTP API，**不读** opencode SQLite（`upstream.py` 走 httpx）。
- T3 硬化已进 v1（见 §6）。
- stunnel mTLS 在 sidecar 前端（部署侧，非代码契约）；:14097 推荐，:4097 明文直连为可选。

### §1 版本契约 — ✅ 完全实现
- `versioning.py`：`SERVER_API_VERSION=1`，`ACCEPTED_CLIENT_VERSIONS=(1,1)`。
- `/slimapi/**` 全量门闩：缺/非整数头 → 400 `version_required`（带 `accepted:[1,1]`）；越界 → 400 `version_incompatible`（带 `client`/`accepted`）。
- `/slimapi/health` 返回 `sidecar.ok` + `server.api_version` + `accepted_client_versions` + `schema.degraded`。
- bump 规则遵守：本批次所有变更**加性，未 bump**。

### §2 端点 — ✅ 完全实现 + 🔄 B1 加性变更
全部 16 行端点表已挂载（`app.py:87` 注册 health/sessions/messages/questions/events/metrics + catch-all）。逐路：

| 端点 | 状态 | 备注 |
|---|---|---|
| `GET /slimapi/health` | ✅ | 含版本+降级+self-check；B1 起支持 gzip |
| `GET /slimapi/ready` | ✅ | liveness；B1 起支持 gzip |
| `GET /slimapi/metrics` | ✅ | 订阅者/queue/hub 指标（§11.3 闭环） |
| `GET /slimapi/sessions` | ✅ + 🔄 | 骨架列表 + `?directory/roots/limit/start/search`，排除 archived，每条带 `directory`；**v0.2.1：失败路径对齐 §7 coded**（4xx→502 `upstream_http_N` / 5xx·网络→503 / 200+坏 JSON·坏 shape→503；原静默透传 upstream body / 网络→FastAPI 500） |
| `GET /slimapi/projects` | 🔄 | 发现 + allowlist；**B1：upstream 5xx/网络 502→503**（状态码变更，见下） |
| `GET /slimapi/sessions/status` | ✅ | 批量 status；B1 起错误体结构化 |
| `GET /slimapi/sessions/{sid}/status` | 🔄 | **B1：404/502/503 三态分裂**；**🔄 F2：放宽 allowlist**（`normalize_directory` 不 gate） |
| `GET /slimapi/messages/{sid}` | 🔄 | 骨架分页；**B1：query `directory` 软 allowlist**（见下） |
| `GET /slimapi/messages/{sid}/since/{ts}` | ✅ + 🔄 | A2=A 语义（§11.2 闭环）；B1 directory 可选透传（**v0.3.0 无 allowlist gate**） |
| `GET /slimapi/messages/{sid}/full/{mid}` | 🔄 | **B1：oversized → 413 流式 cap**（不再整缓冲，见下）；full 剥 `state.metadata.diagnostics`（加性，经 TransformPool admission；见 CHANGELOG [Unreleased]） |
| `GET /slimapi/messages/{sid}/full?ids=` | 🆕 G6 | **批量展开**（1–20 mid，discover 先行，mid 级 envelope `errors[]`，累计 413）；full items 已剥 `state.metadata.diagnostics`（见 CHANGELOG [Unreleased]） |
| `GET /slimapi/questions` | 🔄 F1 + v0.2.1 | 跨目录聚合；**directory 可选**（null=聚合 allowlist；显式**规范化后去重** 1–32），每条带 `routeToken`；200 envelope 含 `scope.directories`（区分 scope 未就绪/权威空） |
| `GET /slimapi/permissions` | 🔄 F1 | 同上 |
| `POST /slimapi/questions/{qid}/reply` | ✅ | routeToken 校验 + directory 注入 + 转发 |
| `POST /slimapi/questions/{qid}/reject` | ✅ | 同上 |
| `POST /slimapi/sessions/{sid}/permissions/{pid}` | ✅ | 同上（`response: once/always/reject`） |
| `GET /slimapi/events` | ✅ + 🔄 | 策展 SSE（§3）；**G1：`lastError?` + `session.error`** |
| `* /{path}` catch-all | 🔄 | 透传 opencode；**B1：shell/PTY 路径 403 拒绝**（见下） |

#### 🔄 B1 加性变更明细（ocdroid 须识别）

**1. `GET /slimapi/sessions/{sid}/status` — 404/502/503 分裂**（contract §7）
- upstream discover **404** → HTTP **404** `{"code":"session_not_found","sessionID":"<sid>"}`
- upstream discover 其它 **4xx**（401/403/409…）→ HTTP **502** `{"code":"upstream_http_<N>"}`
- upstream **5xx / 网络错误 / JSON 解析失败 / 非映射 JSON / 200 但 directory 不可用** → HTTP **503** `{"code":"upstream_unavailable"}`
- **F2（2026-07-19）放宽 allowlist**：per-session status 用 `normalize_directory` **仅规范化、不 gate**；discover 得到的 directory ∉ allowlist 时**仍 200**（继续查 status map / 合成 idle），**不再** 400 `directory_not_allowed`。**v0.3.0** 批量 `GET /slimapi/sessions/status` 也不再 gate，与 per-session 行为对齐。
- **客户端影响**：原先统一 502 的失败现在按上游语义分裂；按 `code` 分发即可。F2 后客户端勿再把 per-session status 的 400 当成 allowlist miss 主路径。**v0.3.0** 后 allowlist gate 全面移除，所有端点的 directory 一律透传。

**2. `GET /slimapi/projects` — 5xx 状态码 502→503**（contract §7）
- upstream 5xx/网络错误由原 **502** 改为 **503 `upstream_unavailable`**；4xx 仍走 502 `upstream_http_N`。
- **客户端影响**：若 ocdroid 的 circuit breaker 硬编码 `/slimapi/projects` 失败=精确 502，须改为按 5xx 类（502/503）分发。

**3. `GET /slimapi/messages/{sid}` + `/since/{ts}` + `/full/{mid}` + `/full?ids=`（G6）— query `directory` 转发**（contract §2/§7/§12）
- **四路由**统一经 `_resolve_messages_directory`：**directory 可选**——未传 query 时**不拦**（用 upstream 默认 directory）；显式传入则 `normalize_directory` 后作 `X-Opencode-Directory` 透传。**v0.3.0** **不再**走 allowlist gate——directory ∉ allowlist 不再 400；仅 query 与 `X-Opencode-Directory` 头冲突仍 400 `directory_not_allowed`（结构性歧义）。G6 `message_batch` 与单 mid `/full/{mid}` 同行为。
- **客户端影响**：可省略 `?directory`（依赖 upstream 默认）；显式传则任意值（包括 `/projects` 未列出的）都会被原样规范化后透传，由上游 opencode 决定能否服务。

**4. `GET /slimapi/messages/{sid}/full/{mid}` — oversized 流式 cap**（contract §7，G8）
- 单条全文超过 `max_message_bytes`（默认 32 MiB）→ HTTP **413** `{"code":"message_too_large","limitBytes":<cap>}`。
- 实现为**流式读 + 早停**（`read_with_cap` + `try/finally: await response.aclose()`），不再整缓冲上游 body；mid-stream `httpx.RequestError` → 503 `upstream_unavailable`。
- **客户端影响**：新增 413 `message_too_large` 分支。`/full/{mid}?mode=skeleton` 仍用 `response_too_large`（64 MiB）——同端点 413 code 随 `mode` 变。

**5. catch-all `POST /session/{sid}/shell`、`/pty/**`、`/api/pty/**` → 403**（contract §7，shell deny-list）
- 命中 → HTTP **403** `{"code":"shell_not_allowed"}`；regex 已容忍尾斜杠。
- Ops 破玻璃：`OC_SLIMAPI_SHELL_DENY_LIST_ENABLED=0` 关闭（**非安全保证**）。
- **客户端影响**：ocdroid 不应再调 shell/PTY 路径（省流模式本就不需要）。

**6. thin 路由错误体 `{"detail":...}` → `{"code":...}`**（contract §7）
- 所有 `/slimapi/**` 路由的错误响应统一为 `{"code":string, "message"?:string, ...fields}`。
- **客户端影响**：**破坏性解析变更**——须把 `response.json()["detail"]` 改为 `response.json()["code"]` 分发。详见 [`docs/specs/CLIENT_CHANGES.md`](specs/CLIENT_CHANGES.md)。

### §2 写路径（B2）— ✅ 完全实现
- routeToken：`tokens.py` HMAC 签发/校验，绑 `kind+requestID+sessionID+directory`，~1h TTL。
- 聚合响应里随条下发（`questions.py:45`）；reply/reject/permissions 端点校验后注入 directory 转发。
- 校验失败 → 400 `invalid_route_token`；mutation 超时 → 504 `upstream_timeout`。**v0.3.0** **不再**因 directory 离开 allowlist 返 400 `directory_not_allowed`——token 校验后 directory 直接 normalize 后透传给上游 opencode。
- routeToken 404/过期透明（已应答/失效）→ 客户端重取聚合。
- 通用写（发消息/abort）走 catch-all，客户端带 `X-Opencode-Directory` 头，sidecar 不剥（`forward_directory_headers`）。

### §3 SSE 契约 — ✅ 完全实现 + 🔄 G1
- 上游：单一 `/global/event` 进程级 GlobalBus，全实例跨目录，每事件自带 `directory`。
- 帧类型全覆盖（`hub.py`）：
  - `session.digest`（debounce 250ms/session）：`{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?}`。**`archived` 字段已落地**（§11.1 闭环：取 `info.time.archived`，永久状态）。**G1 `lastError?`**：`{name,message,at}` sticky 跨窗口；`status=busy` 显式 `null` 清除；`deleted` 后不保留。
  - **`session.error`（G1-B，立即直推，无 sid 时）**：`{directory?, name, message, at}`。有 sid 时错误进 digest 的 `lastError`（G1-A 立即 flush），不下发独立 `session.error` 帧。`MessageAbortedError` 静默过滤；message 脱敏（首行/剥路径/剥 stack/剥 secret/截断 512）。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`：立即直推 `{directory, type, properties}`。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（`{"reason":"reconnect_no_replay"}`，无 replay）。
- 丢弃：`?stream`、text.delta、`message.part.*`、`tool.*`、`sessionId` 参数、per-directory hub。

### §4 冷启动 & resync — ✅ 完全实现（sidecar 侧）
- sidecar 提供 `/slimapi/sessions` + `/questions` + `/permissions` + `/messages/{sid}/since/{ts}`；resync=冷启动流程在客户端编排。
- 落实契约的"resync 不 replay"由 §3 的 SSE 无 replay 语义保证。

### §5 拉消息（A2=A）— ✅ 完全实现
- digest 推 `{messageID, updatedAt}`；客户端记本地最大 `updatedAt`。
- `/slimapi/messages/{sid}/since/{ts}` 返回 `(info.time.updated or info.time.created) >= ts` 的骨架（`messages.py:215` `_item_updated`/`_passes_ts_filter`；v0.2.1 勘误：原读 `info.time.updated` 在 v1.18.3 不存在→过滤 no-op，已修）。
- 缺/非 int 的 `time.updated` 防御性包含（客户端按 messageID 去重边界）。
- 分页 `?limit` + `?before` 游标；累计 `max_response_bytes` 预算跨页执行。

### §6 资源限制（T3）— ✅ 完全实现
- `MAX_SUBSCRIBERS_PER_DIRECTORY=8`、`MAX_TOTAL_SUBSCRIBERS=16`（`hub.py`）。
- 每 subscriber buffer 2 MiB、单帧 256 KiB；溢出 → 立即清 queue/deltas/dirty + 排 `resync{reason:subscriber_backpressure}` + STOP（不再尾排 STOP 续发旧帧）。
- admission 在 `HubRegistry.subscribe` 单一无 await 临界段；超限 → 503 `sse_subscriber_limit_directory`/`_total`（带 `limit`/`current`/`Retry-After`）。
- 转换池：`MAX_TRANSFORMS=1`，admission 在下载前，限长读 `MAX_RESPONSE_BYTES=64 MiB`，parse/project/gzip offload worker thread。
- `GET /slimapi/metrics` 暴露订阅者/queue/hub 指标（§11.3 闭环）。

### §7 错误码 — ✅ 完全实现 + 🔄 B1 / 2026-07-19（G6）扩充
完整码表（含 B1 + G6 加性扩充）。**top-level** = HTTP 状态 + body `{"code":…}`；**envelope** = 整请求通常仍 200，code 在 `errors[]` mid 项。**同一 code 名两语境含义不同**。

| 语境 | HTTP | code | 来源 |
|---|---|---|---|
| top-level | 400 | `version_required` / `version_incompatible` | §1 版本门闩 |
| top-level | 400 | `directory_not_allowed` | **v0.3.0** **仅** messages `/**` query `directory` 与 `X-Opencode-Directory` 头冲突（结构性歧义）；allowlist gate 已移除 |
| top-level | 400 | `invalid_directory_count` | 🆕 B1 questions directory count guard（1-32） |
| top-level | 400 | `invalid_route_token` | 🆕 B1 routeToken 校验失败 |
| top-level | 400 | `invalid_ids` | 🆕 G6：`ids` 空 / 超 20 / 解析后无有效 mid（缺 `ids`→422） |
| top-level | 403 | `shell_not_allowed` | 🆕 B1 catch-all shell/PTY deny-list |
| top-level | 404 | `session_not_found`（带 `sessionID`） | 🆕 B1 G2 status discover 404；**G6 discover** 404（0 mid 拉取） |
| top-level | 404 | `thin_route_not_found` | `/slimapi/**` 未知 |
| top-level | 413 | `response_too_large` | 超 `MAX_RESPONSE_BYTES`（含 **G6 累计**） |
| top-level | 413 | `message_too_large` | 🆕 B1 `/full/{mid}?mode=full` 流式 cap |
| top-level | 502 | `upstream_http_N` | 🆕 B1 G2/projects 非 404 的 4xx；**G6 discover** 其它 4xx |
| top-level | 503 | `transform_busy`（带 `Retry-After`） | 转换池饱和（含 **G6 skeleton**） |
| top-level | 503 | `upstream_unavailable` | 🆕 B1（5xx/网络/坏 JSON/…）；**G6 discover** 5xx·网络·坏 JSON；**G6 任一 mid 网络**（**优先于** 累计 413） |
| top-level | 503 | `sse_subscriber_limit_directory` / `_total` | §6 订阅上限 |
| top-level | 503 | allowlist 刷新失败 | `/project` 发现失败 |
| top-level | 504 | `upstream_timeout` | q/p mutation 超时 |
| **G6 envelope** | 200 | `message_not_found` | 🆕 G6 mid 404（**非整请求 404**） |
| **G6 envelope** | 200 | `upstream_http_N` | 🆕 G6 mid **≥400（含 5xx）**（**不**升级整请求） |
| **G6 envelope** | 200 | `message_too_large` | 🆕 G6 mid body > `max_message_bytes`（非整请求 413） |
| **G6 envelope** | 200 | `upstream_error` | 🆕 G6 mid 2xx 坏 JSON **或合法 JSON 非 MessageWithParts 形状**（skeleton/full 一致；原 skeleton 逃逸 500、full 入 items[]） |
| q/p envelope | 200/503 | `upstream_http_N` / `upstream_timeout` / `upstream_error` | questions/permissions fan-out 单 dir 失败项 |

- thin 路由错误体统一 `{"code":string, "message"?:string, ...}`（非 `{"detail":...}`）。
- **双语境复用**：`upstream_http_N` / `message_too_large` / `upstream_error` 等在 top-level 与 envelope 均出现——按端点 + 是否在 `errors[]` 分发（契约 §7；v1-impl-spec §11 双行）。

### §8 客户端 v1 最小集 — ✅ 完全实现（sidecar 侧）
- sidecar 已支持：版本头 + health 自检（`schema.degraded` fail-closed 信号）+ 冷启动端点 + SSE(digest+q/p) + `/since` 拉消息 + catch-all 写 + routeToken 应答 + resync 语义。
- `/slimapi/health` 已落地（fix-7，C3）。
- **剩余编排在 ocdroid 侧**（连接 R8 + M2 fail-closed + 冷启动流程 + SSE 接力）。

### §9 gzip — ✅ 完全实现
- 所有 JSON 路由的 `json_response` 调用转发 `accept_encoding=request.headers.get("accept-encoding")`。
- sessions / questions / messages / **health / ready / metrics** 全覆盖（§11.4 闭环）。
- 带 `Vary: Accept-Encoding`。

### §10 延后（非 v1）— ⚫ 显式未实现
按契约显式延后到 v2+：
- skeleton 共享缓存（YAGNI，先看指标）
- 多用户（独立 stack）
- Part 展开 UI
- sessions status 迁移
- circuit breaker
- metrics 之外的可观测

**原因**：v1 目标=2-5 台同用户设备（§0），上述均非该规模必需。

### §11 v1 待补缺口清单 — ✅ 全部闭环
| # | 缺口 | 状态 | 证据 |
|---|---|---|---|
| 1 | hub.py digest `archived` 字段 | ✅ | `hub.py:105/118/334-344`：取 `info.time.archived`，永久状态，进 digest payload |
| 2 | messages.py `/since` 语义 `(info.time.updated or info.time.created) >= ts` + 分页 | ✅ | `messages.py:215` `_item_updated`/`_passes_ts_filter`；`?limit` + `?before`（v0.2.1 勘误：原读 `time.updated` 在 v1.18.3 不存在→no-op，已修） |
| 3 | T3 硬化（订阅上限 + 字节预算 + 立即清式溢出 + `/metrics`） | ✅ | `hub.py` MAX_SUBSCRIBERS / `subscriber_backpressure`；`routes/metrics.py` |
| 4 | gzip 清理（health/ready 等转发 accept_encoding） | ✅ | `routes/health.py:20/38` |
| 5 | 写路径核验（catch-all + `X-Opencode-Directory`） | ✅ | `forward_directory_headers` 在 messages/proxy 转发；端到端 work |

---

## ocdroid 侧须关注（Wire-visible 变更，须客户端配合）

按风险降序：

1. **错误体形状变更（最高优先）**：thin 路由 `{"detail":...}` → `{"code":...}`。**破坏性解析**——所有错误分发须改 `json()["code"]`。详见 `docs/specs/CLIENT_CHANGES.md`。
2. **`/slimapi/sessions/{sid}/status` 三态分裂**：原统一 502，现 404(`session_not_found`)/502(`upstream_http_N`)/503(`upstream_unavailable`)。建议按 `code` 而非 HTTP 状态分发。
3. **`/slimapi/projects` 5xx 状态码 502→503**：circuit breaker 不应硬编码 502。
4. **messages 四路由 query `directory` 转发**（list / since / full/{mid} / **G6 full?ids=**）：`directory` **可选**——未传不拦（upstream 默认）；显式传则 normalize 后透传（**v0.3.0** 不再 gate allowlist）；与 `X-Opencode-Directory` 头冲突会 400 `directory_not_allowed`。
5. **新增 413 `message_too_large`**（`/full/{mid}` 默认 mode）；`?mode=skeleton` 仍用 `response_too_large`。G6 累计超限整请求 413 `response_too_large`；单 mid 过大进 envelope。
6. **新增 403 `shell_not_allowed`**：省流模式不应调 shell/PTY。
7. **新增 400 `invalid_directory_count` / `invalid_route_token`**：q/p 聚合与 reply 错误分支。
8. **G6 批量展开 + F1 q/p null directory + G1 lastError/session.error**：见 `CLIENT_CHANGES.md` 对应节；wire 版本仍为 1。

## 已知 best-effort / 非保证（契约外诚实声明）

- **shell/PTY deny-list 是 best-effort，非安全保证**：路径大小写变体、`/./`、`/../` 段、双重编码**不归一化**；真实隔离靠 stunnel mTLS + 网络边缘。Ops 可 `OC_SLIMAPI_SHELL_DENY_LIST_ENABLED=0` 关闭。
- **thin 路由 mid-stream upstream 异常**：list/since 路径的 `read_with_cap` 在上游中途断流时按 pre-existing 行为可能 500（spec §7 承认）；`/full/{mid}` 已包裹为 503 `upstream_unavailable`。
- **routeToken-allowlist 时序**：**v0.3.0** slimapi 已**完全移除 directory allowlist gate**；`_token` 仅校验 HMAC + normalize directory，不再查 allowlist、不再调 `load_products`。冷启动空 allowlist 不再影响 routeToken 路径。`warm_allowlist` 保留用于 `/projects` 展示与 q/p null-directory 聚合 fan-out。

## 审计产物
- 契约：`docs/specs/v1-contract.md`（含头部「变更记录」+ §7 加性小节，wire 权威同步实现）
- 实现细节追踪：`docs/specs/v1-impl-spec.md`（§11 双行：顶层/envelope）
- 端点级坑表：`docs/specs/INTERFACE_MAP.md`
- 客户端改动清单：`docs/specs/CLIENT_CHANGES.md`
- 接口行为变更记录：`CHANGELOG.md`（2026-07-18 v1 B1 条目）
- 交付报告：`docs/ocmar/reports/2026-07-18-v1-b0-b1.md`

## 校验
- `./scripts/check.sh` → **544 passed**, EXIT=0（@ 2026-07-22 **v0.4.0**；含 Batch 1–5 透传收敛：错误边界 / children 投影+缓存 / childrenVersion 失效 / G6 shape / deleted tombstone + 既有 SSE lifecycle 回归）。
- 基线：working tree at `9373550` + 本批未提交改动（ocdroid-findings-evaluation + SSE lifecycle fix）。
