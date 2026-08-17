# Changelog

本文件记录 **oc-slimapi 的接口与行为变更**，供 **ocdroid** 对接与运维查阅。

格式 loosely 遵循 [Keep a Changelog](https://keepachangelog.com/)，版本遵循 [SemVer](https://semver.org/)。

## 版本双轨（必读）

| 轨道 | 是什么 | 何时变 |
|---|---|---|
| **包版本** `vX.Y.Z`（本文件标题 + git tag + `pyproject.toml`） | 产品发版版本 | 每次 `./scripts/release.sh` |
| **Wire API 版本**（整数，`versioning.py` / 契约 §1） | 协议兼容门禁 | 仅**破坏性** wire 变更 bump；加性变更 **不** bump。**3.0.0 起 `X-Slimapi-Version` 头已删除**：版本协商唯一机制 = `?v=` selector + `GET /slimapi/versions` 发现端点 |

ocdroid 对接时：

1. 读本文件了解**行为**变更；
2. 读 `docs/specs/v2-contract.md` 了解**当前完整契约**；
3. 用 `/slimapi/health` 的 `server.api_version` / `accepted_client_versions` 做运行时兼容自检。

### 维护规约

- **每次**用户可见 / 客户端可观测的 wire 行为变更，必须在对应版本下增加条目（Added / Changed / Fixed / Removed / Security）。
- 条目写**行为与路径**，不写实现细节（避免“改了哪行 Python”）。
- 破坏性变更：同时更新 `docs/specs/v2-contract.md` + bump wire API 版本 + 在本文件 **Changed** 中显式写 `X-Slimapi-Version` 与客户端必改点。
- 发版时由 `./scripts/release.sh` 校验本文件含有目标版本标题（见 `docs/release.md`）。

---

## [4.0.0] - 2026-08-17 — expand 片段端点 + skeleton 投影缩减（包版本 MAJOR；wire 版本**不变**，仍 v3——缩减按 owner 决策收进 v3 视图，无 `?v=4`，ocdroid 同步发版承接）

> 设计稿：`docs/specs/design-expand.md`（v5，rev-sgpt R4 APPROVED WITH CHANGES 已采纳）。**ocdroid 必改点**：渲染 skeleton 列表时按 `omitted`/`hasFull`/`expandRefs` 处理被折叠字段，按需经 expand 端点拉取；`text` 整字段折叠为 `null`（从不半截断），超大内容改走 `part_text`/`part_reasoning` 拉取。

### Changed（wire 行为变更：skeleton 投影缩减——wire 仍 v3，无新 selector）

- **`GET /slimapi/messages/{sid}` 与 merged 模式的 skeleton 投影收紧**（design-expand §2）：
  - `text`/`reasoning` part：UTF-8 字节 **>2048** → `text: null` + `omitted:["text"]` + `hasFull:true` + `expandRefs:[{category:"part_text"|"part_reasoning",…}]`（**整字段折叠，从不半截断**；≤2048 原样内联）。
  - `text` 的 `synthetic`/`ignored`/`time` 与 `reasoning` 的 metadata/time：**/full-only**——skeleton 中 `omitted`（无 refs；`/full` 照旧全量）。
  - `file` part：`url` 为空/缺 → `url:null`+omitted；非空折叠 → ref `part_url`；`source` 折叠 → ref `part_source`。
  - `step-start`/`step-finish`：`snapshot` 折叠 → ref `part_snapshot`（仅折叠时刻非空才 ref）；`reason`/`cost`/`tokens` /full-only。
  - `tool` state：`input`/`metadata`/`attachments` 折叠 → refs `part_state_input_full`/`part_state_metadata_full`/`part_state_attachments`（缺失 vs 显式 null 均折叠不 ref）。
  - `info.summary.diffs`：skeleton 恒 `null` + 消息级 `info.expandRefs[0].category="info_summary_diffs"`（非空 list 才 ref；`/full` 照旧全量）。
  - **`expandRefs` 为 sidecar 拥有键**：上游同名 junk 一律剥离/确定性替换，永不透传、不进 `omitted`。
  - 无可用 id 的 part/message：缩减照常应用但不生成（不可用的）refs。
  - `?mode=merged` 扩展：placeholder 优先填槽，剩余槽位由页内 part 级 `expandRefs` 候选填充（消息级 diffs ref **不参与**候选）；页序保持、预算耗尽单项降级保持折叠（不 500 整页）。
- **对账语义**：expand 返回**当前态**字段值（非快照）；part 已删 → 404（`reason:"part_missing"`），类型已变 → 400——客户端据此刷新 skeleton。

### Added（加性 wire：expand 片段端点 + 配置/能力/记账）

- **2 个 GET 端点**（design-expand §2.2/§3）：`GET /slimapi/messages/{sid}/expand/{category}/{mid}`（message-level，仅 `info_summary_diffs`）与 `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`（part-level，11 类目）。12 类目白名单单一事实源 = `traffic.py::EXPAND_CATEGORIES`（表序冻结）。
- **响应 envelope**：`{category, messageID, data}`（part 级多 `partID`）+ `Cache-Control:no-store` + gzip+`Vary`；缺失字段 vs 显式 null → 200 + `data` 键 `null`。
- **求值序冻结**（§3.1）：白名单 400 → level 400 → 池 admission 503（**先于** part 级错误）→ 共享 single-flight GET（与 `/full`/merged 同键去重）→ 源 cap 413（先于解码）→ decode 503 → 定位 502/404 → 类型 400 → 提取 → 片段 cap 413（gzip 前）。错误码仅 `expand_target_not_found`（附 `reason`）/`expand_category_mismatch` 两命名 + `invalid_expand_category`/`expand_source_too_large`/`expand_fragment_too_large`/`upstream_invalid_shape`/`transform_busy`/`upstream_unavailable`。
- **配置**：`OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES`（默认 8388608=8 MiB，1 KiB–32 MiB 含边界校验，非法值启动即 RuntimeError 命名变量）；转换池聚合 envelope 上限自动取 `max(max_response_bytes, max_expand_response_bytes)×max_transforms`。
- **能力广告**：`GET /slimapi/versions` → `capabilities["3"]["expand"] = {validCategories:[12 项表序], fragmentMaxBytes:<live>}`。
- **流量记账**：ledger/snapshot/metrics 新增 `expand` 块（按 category×status 聚合）；伪造/非法 category 折叠进固定 `invalid` 桶（防基数 DoS）；`traffic-snapshot-YYYY-MM-DD.jsonl` 持久化含 expand。

---

## [2.0.0] - 2026-08-16 — v3 wire 全表面（M1 中间版：版本选择器 + envelope + directory query + ETag 域隔离 + SSE meta + 读 11 路由 + 写 12 端点收编；全部加性，v2 逐字节不变）；未 bump `X-Slimapi-Version`，仍 2

### Added（加性 wire：v3 Batch C2——写路径 12 端点收编；未 bump `X-Slimapi-Version`，仍 2）

> 权威契约：`docs/specs/v3-contract.md`（2.0.0 实施基线）§10.b。**ocdroid 写流量从 catch-all 盲转迁移到一等 `/slimapi` 路由**（受控代理：校验 + 上限 + 审计 + directory 路由）；v2 语义请求仍可走 catch-all（3.0.0 前不关闭）。

- **12 个写端点**（body/Content-Type 透传，请求超 `max_message_bytes` → 413 `request_too_large` **先于上游调用**）：`POST /slimapi/session`（创建）、`PATCH /slimapi/session/{session_id}`（**双 shape**：title/metadata/permission 与 `time.archived`）、`DELETE /slimapi/session/{session_id}`、`POST /slimapi/session/{sid}/prompt_async`、`POST /slimapi/session/{sid}/abort`、`POST /slimapi/session/{sid}/summarize`、`POST /slimapi/session/{sid}/fork`（`messageID` 为**可选 body 字段**，非 query）、`POST /slimapi/session/{sid}/revert`、`POST /slimapi/session/{sid}/permissions/{permission_id}`（respond）、`POST /slimapi/question/{request_id}/reply`（body `answers`）、`POST /slimapi/question/{request_id}/reject`、`POST /slimapi/command`。
- **错误两级制（契约 §10.a）**：上游 4xx → 状态 + body 逐字透传；上游 5xx/网络错误 → 503 `upstream_unavailable`。**错误 body 同样受 `max_response_bytes` cap**——超限统一降级 503（资源保护优先于逐字义务）。2xx/3xx 状态逐字（3xx 不跟随重定向）。
- **响应头冻结集**：仅透传上游实有头（`content-type`/`location`/`retry-after`/`x-request-id`/`last-request-id`）——上游无 `Content-Type` 时**不注入缺省值**；成功响应 gzip 受益门 + `Cache-Control: no-store` + `Vary: Accept-Encoding, X-Opencode-Directory`。
- **query 逐字透传**（`v`/`directory` 消费剥离后其余参数逐字节保持）；**directory 全 12 端点消费**（v2 = 头通道；v3 = `?directory=` canonical）；无 ETag/304、不占转换池。
- **观测**：`write_session`/`write_question` 桶（method-aware：POST 才落写桶，GET 405 落 `other`）。

### Added（加性 wire：v3 Batch C1——读路径 7 组 11 路由收编；未 bump `X-Slimapi-Version`，仍 2）

> 权威契约：`docs/specs/v3-contract.md`（2.0.0 实施基线）§10.a。**匿名/ocdroid 读流量从 catch-all 盲转迁移到一等 `/slimapi` 路由**；旧 sidecar 无这些路由 → catch-all 404 `thin_route_not_found`，客户端可回退透传。

- **11 个 GET 读路由**：`/slimapi/file`、`/slimapi/file/content`、`/slimapi/file/status`、`/slimapi/vcs`、`/slimapi/vcs/status`、`/slimapi/vcs/diff`、`/slimapi/find/file`、`/slimapi/config/providers`、`/slimapi/session/{sid}`（单查，**经 `skeleton_session()` 白名单投影**，与列表逐字同 keep/drop）、`/slimapi/api/session/active`（宽容）、`/slimapi/global/health`（宽容——上游 global health，非 sidecar `/slimapi/health`）。
- **统一管线**：流式 GET → 两级错误（4xx 状态+body 逐字 / 5xx·网络 → 503 `upstream_unavailable`，**错误 body 同受 cap**，超限 503）→ response cap（413 `response_too_large`）→ **ETag/304**（validator 含 wire 版本域，v2/v3 隔离）→ gzip 受益门 64B。
- **投影门控（契约 §10.a 冻结）**：仅 2xx + 合法 JSON object 投影（session 单查）；204 空 body / 3xx 非 JSON 逐字透传；坏 JSON → 503；投影经转换池 offload（池满 503 `transform_busy` + `Retry-After: 2`）。raw 路由（file/vcs/find 等）零投影零池。
- **query 逐字透传**（v2/v3 均 `v` 剥离后逐字节）；directory：v2 = 头通道（query 中 `directory` 非法值 → 400 `invalid_directory`）；v3 = `?directory=` canonical；宽容路由（active/global-health）v2/v3 均不校验。
- **上游状态透传**：成功分支透传上游实际状态码（201/202/204/206/3xx 逐字，不跟随重定向）。

### Added（加性 wire：v3 Batch D——SSE meta 首帧；未 bump `X-Slimapi-Version`，仍 2）

> 权威契约：`docs/specs/v3-contract.md`（2.0.0 实施基线）§7。**`X-Slimapi-Subscriber-ID` 响应头的 v3 替代**（3.0.0 全头退役前置）。v2 流逐字节不变。

- **v3 SSE 开流首帧**：`event: slimapi.meta`，data `{"subscriberId": "<id>", "tokens": <bool>}`——`/slimapi/events` 的 `tokens` 反映 `?tokens=1` 实际值；`/slimapi/sessions/{sid}/stream` 恒 `true`。首帧早于任何业务帧/heartbeat/`Last-Event-ID` resync 回放；meta 帧计入 `downOut` 流量记账。
- **v3 不再下发 `X-Slimapi-Subscriber-ID` 响应头**（v2 照旧）。
- **v3 `/stream` 恒 identity**（token 帧字节原样冻结，`Content-Encoding`/`Vary` 不产出）；gzip 压缩降为 v2-only lever（`/events` v3 仍按 Accept-Encoding 协商）。
- 配套测试 `tests/test_v3_sse_meta.py`（18 用例，含 v2 字节级回归）。

### Added（加性 wire：v3 Batch B——envelope + directory query + ETag 域隔离；未 bump `X-Slimapi-Version`，仍 2）

> 权威契约：`docs/specs/v3-contract.md`（rev11）§4/§5/§6。**仅 `?v=3` 请求**获得下列 v3 视图；无 `v`/`?v=2` 的 wire 形状逐字节不变。

- **Envelope（契约 §4）**：`GET /slimapi/messages/{sid}?v=3` → `{"items":[<v2 裸数组逐字节>],"nextCursor":<string|null>}`（`nextCursor` 语义同 v2 `X-Next-Cursor`，游标不回退照旧；无 `complete`）；`GET /slimapi/sessions?v=3` → `{"items":[…],"complete":<bool>}`（`complete` 语义同 v2 `X-Complete`，继承非权威性；无 `nextCursor`）。v3 200 **不再下发** `X-Next-Cursor`/`X-Complete` 头（值移入 envelope）。`GET /slimapi/sessions/status?v=3` **不 envelope**；错误响应不 envelope；304 无 body。
- **Directory query（契约 §5）**：v3 canonical = `?directory=` query（语义同 v2 头：选工作目录实例）。消费集（本批既有路由）：`messages/{sid}`、`sessions` 列表+`status`、`todo/children/diff`、`agent/command`（`stream` 见下条）。仅 v3：query 与头双现——归一化（尾斜杠）同值正常；异值 → 400 `directory_conflict`（字段 `queryDirectory`/`headerDirectory` 冻结）；query 多值同值折叠、异值 → 400 `invalid_directory_selector`。消费到的 directory（query **或兼容头**，含 header-only）→ 上游 `X-Opencode-Directory` 头转发，且 query 中的 `directory` 参数对**剥离不转发**（其余参数逐字节保持）。v2 请求：不消费不剥离 v3 语义（`?directory=` 照旧按 v2 路由逻辑转发；sessions v2 仍补上游 query）。宽容忽略集（questions/permissions/events/health/versions/directories/actions）：任何形式不报错不消费。
- **Stream 特例（契约 §5.6）**：`GET /slimapi/sessions/{sid}/stream` 守卫逐字继承 v2 现逻辑（query-only 接受 no-op；query+header 异值 → 400 `directory_not_allowed` 现有码）+ v3 前置多值异值 → 400 `invalid_directory_selector`（选择器层前置拦截，路由守卫兜底同码）。stream 不做 v3 stash/剥离——单值 query 原样流经守卫。
- **ETag 域隔离（契约 §6.1）**：`representation_version` 指纹输入加入 wire 版本标记（`wire=v2`/`wire=v3`）——v2/v3 validator 互不匹配，防 envelope 语义跨视图误 304。**副作用（设计内）**：本次升级后所有既有 v2 ETag 轮换一次（指纹域变更）；客户端缓存条目一次性 revalidate。v3 304 头集合仅 `ETag`+`Vary`+`Cache-Control: no-store`（**不复制** `X-Next-Cursor`/`X-Complete`——客户端从缓存 envelope 取）；v3 200 envelope 路由 ETag canonical 输入 = envelope body（envelope 内容变 → ETag 变）。Vary 并行期保持 `Accept-Encoding, X-Opencode-Directory`（4 条 directory 敏感路由）；`?v=`/`?directory=` 属 URI 进 cache key 不加 Vary。
- **directoryForm 观测（契约 §9.1）**：v3 消费集按实际 client 形态记 `query|header|both|absent`（含 header-only 消费）；非消费路由 `null` 不变。
- **Vary 矩阵修正（契约 §6.2，门控 B1/C3；缓存正确性修复，行为变化仅限 Vary 头加值）**：directory-sensitive 路由的 `Vary` **无条件**含 `X-Opencode-Directory`，与 ETag 开关无关——修复 `messages/{sid}/full/{mid}` 此前仅 `Vary: Accept-Encoding`（该路由消费转发 directory、body 随工作目录实例变化），以及 `OC_SLIMAPI_ETAG_ENABLED=false` 时 messages/sessions 列表与 cached agent/command 退化成 AE-only。affected：`full/{mid}`（恒）、etag 关闭时的 messages 列表（v2/v3）、sessions 列表（v2/v3）、agent/command（cached 与 uncached）。非 directory 路由（questions/permissions/health/versions 等）Vary 不变（AE-only 或无 Vary）。

### Added（加性 wire：v3 Batch A——版本选择器 + 发现端点 + health 双视图 + 观测字段；未 bump `X-Slimapi-Version`，仍 2）

> 权威契约：`docs/specs/v3-contract.md`（2.0.0 实施基线，rev11）。本批为 v3 奠基：**`?v=3` 请求的响应 body 仍是 v2 形状**（envelope 等 v3 视图改造属 Batch B/C/D）；本批只加选择器判定、发现端点、health 双视图与观测。

- **版本选择器（契约 §2）**：`/slimapi/**` 路由支持查询参数 `v` 选择 wire 语义。`v=3` → v3 语义（`X-Slimapi-Version` 头若同时出现**忽略不报错**，头门禁豁免）；`v=2` 或无 `v` → 现行 v2 管线（头门禁照旧：缺头 → 400 `version_required`）。词法非法（`0`/`03`/`+3`/`3.0`/空串/多值异值）→ 400 `invalid_version_selector`；合法但不支持（`1`/`4`/`5`…）→ 400 `unsupported_version`（`supported:[2,3]`）。多值同值折叠。**`v` 消费剥离（契约 §5.2）**：判定后所有转发的 `/slimapi/**` 请求（v2/v3/`versions` 豁免/回退模式）的 `v` 参数对从下游 query **无条件剥离、永不转发**，其余参数逐字节保持（编码/顺序/重复项不变）。catch-all（非 `/slimapi`）零改动，不消费 `v`/`directory`，`?v=2/3` 逐字透传上游。
- **头门禁范围放宽（加性）**：`X-Slimapi-Version` 接受范围由 `[2,2]` → `[2,3]`（`/slimapi/health` 的 `accepted_client_versions` 与 400 错误体的 `accepted` 同步）。缺头仍 400 `version_required`（v2 语义请求不变）。
- **发现端点（契约 §3）**：`GET /slimapi/versions` 返回 `{current:3, available:[2,3], capabilities:{2:{etag,contentFingerprint,thinRoutes:[todo,children,diff]}, 3:{envelope:[messages,sessions],directoryQuery,versionHeaderOptional,writeRoutes,readRoutes:[file,vcs,find,providers,sessionSingle,activeSessions,globalHealth]}}, sidecarVersion}`；`Cache-Control: no-store`、无 ETag、gzip 协商族。该端点**无条件豁免**（不要头、不吃 `v`）；**非 GET → 405 + `Allow: GET`**（优先级高于选择器判定）。
- **health 双视图（契约 §3a）**：`GET /slimapi/health` 根级 `slimapi_contract`、`server.api_version`、`schema.version` 随请求语义双视图——v2 语义请求见 2，`?v=3` 请求见 3（三字段同源同步，禁止 3/2 组合）；`accepted_client_versions`/`schema.clientMin/clientMax` 恒 `[2,3]`。`GET /slimapi/ready` 无 contract 字段，`schema` 三字段同款双视图。
- **观测字段（契约 §9，加性 ops 面）**：access log 新增尾缀字段 `wireVersion`（`"2"|"3"|null`）、`selectorResult`（`absent|v2|v3|rejected|exempt|not_applicable`；catch-all=`not_applicable`）、`directoryForm`（`query|header|both|absent|null`；非 directory 消费路由=`null`）、`recordType`（`request|sse_open|sse_close`；**字节/请求数统计须过滤 `recordType=="request"`**）、`lifecycleId`（进程内单调，SSE open/close 行配对键）；`/slimapi/events`、`/slimapi/sessions/{sid}/stream` **及 catch-all 透传 SSE（`/event`、`/global/event`，`not_applicable` 维度）** 建立断开各记一行生命周期行。`/slimapi/metrics` 的 `traffic` 块与 traffic snapshot 新增加性 `v3` 节（`matrix` 七维计数 + `sseLifecycle` + `sseActive` 四维 `{v2,v3,absent,not_applicable}`）；离线对账的 matched/orphan close **按 `lifecycleId` 配对**（配不到 → 孤儿补记，不冲减存量）。旧行无新字段——消费方须容忍缺失。查询口径见 `docs/manual/traffic-accounting.md` §5.1/§9.4。
- **Ops 开关**：`OC_SLIMAPI_V3_SELECTOR_ENABLED`（默认 `true`；`false` 时 `?v=3` 也走 v2 管线，观测记 `selectorResult=absent`——灰度回滚用）。

## [1.6.0] - 2026-08-16 — T18 `diff` thin 路由（匿名读侧收编最后缺口；加性）；未 bump `X-Slimapi-Version`，仍 2

## [1.5.0] - 2026-08-16 — 上游流量优化四批（catalog TTL 缓存 + single-flight 去重 / ETag/304 / todo+children thin 路由 / 消息内容指纹；全部加性）；未 bump `X-Slimapi-Version`，仍 2

### Added（内部性能 + 两点行为披露；无 wire 契约变更，`X-Slimapi-Version` 不变）

- **上游流量优化（内部，响应字节不变）**：
  - **Catalog TTL 缓存**：`/slimapi/agent`、`/slimapi/command` 的成功响应按 `(kind, directory)` 缓存（默认 TTL 300s，字节/条目双预算；`OC_SLIMAPI_CATALOG_CACHE_*` 可调）。仅缓存成功（200）响应；错误不缓存。
  - **行为披露 ①（catalog 新鲜度）**：catalog 响应可能滞后上游至多 TTL 秒（默认 300s）。需要强新鲜度的运维场景设 `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS=0` 关闭（行为回到逐请求直取）。
  - **上游去重（join-first single-flight）**：messages 列表、sessions 列表、sessions/status、questions/permissions（discovery + per-dir 两级）的相同上游 GET 在并发时合并为一次抓取；预算/开关见 `OC_SLIMAPI_COALESCE_ENABLED` / `OC_SLIMAPI_RAW_FETCH_*`（默认容量下同时只有 1 个去重 flight，超额自动降级直取，详见 `docs/operations.md` §5.5）。
  - **行为披露 ②（join-first 等待语义）**：去重路径下，同 key 并发 caller 共同等待**一次**上游 GET——上游 hang 时这些 caller 一起等到 httpx 超时（映射为 503 `upstream_unavailable`），而非部分 caller 更早收到 503 `transform_busy`。正常路径响应字节与直取一致。
  - **Access log 加性字段**：`/slimapi/agent`、`/slimapi/command` 的 access log 记录新增可选字段 `cache: "hit" | "miss"`（仅 catalog 缓存路径且 `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS > 0` 时写入；TTL=0 禁用缓存则不产生该字段），供省流审计使用。

### Added（加性 wire：ETag/304 条件请求 — 流量优化 Batch 2/B1；未 bump `X-Slimapi-Version`，仍 2）

- **4 个 GET 端点支持 `ETag` + `If-None-Match` → 304**：`/slimapi/sessions`（列表）、`/slimapi/messages/{sid}`（列表，`mode=merged` 同样适用，以最终 splice 后 body 为准）、`/slimapi/agent`、`/slimapi/command`。验证器按实际 coding 派生（identity 强 ETag / gzip 弱 ETag，sha256 输入恒为 identity body + coding id；`REP_VERSION` 含投影版本与 config 指纹，投影/配置变化 → 全部验证器轮换，构造上不可能误 304）。304 头集合 = `ETag` 同值 + `Vary` 与 200 一致 + `Cache-Control: no-store` + 路由辅助头复制（`X-Next-Cursor` / `X-Complete`，值来自本次管线计算），无 body。`If-None-Match` 弱比较 + `*`。**管线照常执行不短路**（上游 GET/投影照跑，上游去重照常摊薄；命中仅省下行传输体，gzip 命中 = canonical hash 后零压缩零传输）。4xx/5xx 不带 `ETag` 不参与 304。
- **Vary 合并（加性）**：上述 4 端点 200/304 的 `Vary` 由 `Accept-Encoding` 追加合并为 `Accept-Encoding, X-Opencode-Directory`（directory 影响上游 workspace 路由与 catalog/list 投影）。
- **Ops 开关**：`OC_SLIMAPI_ETAG_ENABLED`（默认 `true`）；`false` → 不输出 `ETag`、不判 304、`Vary` 不合并（逐字节等价开关关闭前行为）。客户端接入指引见 `docs/specs/CLIENT_CHANGES.md`「ETag 接入」。

### Added（加性 wire：T17 todo/children thin 路由 — 流量优化 Batch 3/C2a；未 bump `X-Slimapi-Version`，仍 2）

- **`GET /slimapi/sessions/{sid}/todo`**：透传路由 thin 化。上游 `Todo.Info[]`（`{content,status,priority}`）schema 已最小 → **近恒等投影**（无字段可省，路由价值 = gzip 协商 + `max_response_bytes` cap + 转换池 admission + 结构化错误映射）。省流依据 T16 top#1（3d ~1.35 MiB passthrough，gzip 杠杆 ~22-30%）。
- **`GET /slimapi/sessions/{sid}/children`**：加性回归（v1 曾有、lite-v2 删、无状态重加——`/slimapi/sessions/status` 同款先例）。每项经既有 `skeleton_session()` 投影，与 `/slimapi/sessions` 列表**逐字相同** keep/drop（丢 `cost`/`tokens`/`location`/`subpath`）。**状态量护栏**（设计稿 §6.2）：不引入 `X-Children-Version`、`childrenVersion` digest、`childrenIDs[]` list hints、cache/single-flight/SSE 失效——纯读。省流依据 T16 top#2。
- **共性**：`directory` 可选 query（`validate_directory` 400 `invalid_directory`）+ `X-Opencode-Directory` 转发；转换池 admission 先于上游 GET；错误映射与 sessions/messages 对齐（上游 404→404 `session_not_found`（带 `sessionID`）；其他 4xx→502 `upstream_http_N`；5xx/网络/坏 JSON/非 list→503 `upstream_unavailable`；超 cap→413 `response_too_large`；池满→503 `transform_busy`+`Retry-After:2`）。旧 sidecar 无此路由 → 客户端 capability detection：404 `thin_route_not_found` 时 fallback 透传 `GET /session/{sid}/todo|children`。设计稿：`docs/specs/traffic-route-todo-2026-08-10.md`、`docs/specs/traffic-route-children-2026-08-10.md`。

### Added（加性 wire：diff thin 路由 — T18；未 bump `X-Slimapi-Version`，仍 2）

- **`GET /slimapi/sessions/{sid}/diff`**：会话文件变更（diff）thin 读路由，todo/children（T17）同款管线（转换池 admission 先于上游 GET + `read_with_cap` 上限 413 + worker gzip + 结构化错误）。投影近恒等（上游 `Snapshot.FileDiff` `{file?,patch?,additions,deletions,status?}` schema 已最小，`patch` 大字段保留——gzip 是省流杠杆）。**`messageID` 可选 query 同名原样透传上游**（缺省不发，上游语义 = 返回 `[]`：缺省/未知消息/非 user 角色均答 200 `[]`，空结果为正常 body 非错误）；`directory` 可选仅 header 转发。空 `[]` 跳过 gzip（<64B 受益门）；本路由无 ETag/304（`If-None-Match` 忽略恒 200）。旧 sidecar 无此路由→catch-all 404 `thin_route_not_found`，客户端可回退透传 `GET /session/{sid}/diff`。

### Added（加性 wire：消息内容指纹 contentFingerprint — 流量优化 Batch 4/B3；未 bump `X-Slimapi-Version`，仍 2）

- **`GET /slimapi/messages/{sid}`（缺省与 `mode=merged`）每条消息 skeleton 加性字段 `contentFingerprint: string`**，格式 `"<vN>:<sha256hex>"`（全量 hex 不截断）。定位：`(updatedAt, messageId)` 双水印去重的**补充证据**（内容级等价性判定），不替代、不改变 digest 帧或水印推进规则（消费侧推进规则见 ocdroid 联合计划）。生成位置：缺省列表 = skeleton 投影完成时；`mode=merged` = placeholder 内联 splice 完成后**重算覆盖**（full 抓取失败/预算不足/坏响应等降级路径**不重算**，保留 skeleton 期指纹）。规范化：排除字段自身 + `sort_keys` 序列化（parts 保持上游序、无数值规范化）后 SHA-256。
- **终态语义**：同输入（同投影/规范化/`vN`）→ 恒同指纹（跨进程重启确定性）；指纹不同 ⟹ 输入不同；指纹相同 → 输入相同（以 SHA-256 碰撞可忽略为前提）。**不提供单调性**（非序号/revision，不得用于排序或版本比较）。**跨表示模式不可比较**：缺省与 merged 对同一消息产生不同指纹（merged 输入含 full 内容），`vN` 前缀不区分模式，客户端仅在同模式内比较。
- **Ops 开关**：`OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED`（默认 `true`）；`false` → 不输出该字段（逐字节等价开关关闭前行为）；开关状态参与 ETag `REP_VERSION`（关闭 → 全部 ETag 验证器轮换，304 全 miss 一次属预期）。设计稿：`docs/specs/design-message-watermark.md`；客户端接入指引见 `docs/specs/CLIENT_CHANGES.md`「内容指纹消费」。

### Fixed（内部可靠性：questions/permissions 去重共享内存不变数 — 终审 B1；无 wire 变更，响应字节不变）

- **questions/permissions 的 discovery 去重共享值由展开 JSON 改为 capped raw bytes**：并发去重下，各 caller 在租约窗口内自行解析、派生自有目录字符串副本后即丢弃展开图——消除"joiner 持有可达数倍于 raw 上限的共享 JSON 图跨 await"的内存不变数违规。per-dir 去重（`/question`、`/permission`）本就共享 raw bytes（审计确认，回归测试钉住）。行为/响应字节不变，纯内部修复。
- **租约释放切断引用（终审复评 rev-1）**：`Lease` 释放时切断对共享 body/entry 的引用（释放后 `lease.body` 恒 `None`）——已释放租约句柄不再跨后续 await 持有旧 generation raw body；messages/sessions/questions/permissions 全部租约路径统一免疫。行为/响应字节不变，纯内部修复。

## [1.4.0] - 2026-08-15

### Added

- **`GET /slimapi/events?tokens=1` 可选下发 lean `token` 帧（L2-A，加性变更，未 bump `X-Slimapi-Version`，仍 2）**：新增可选 query 参数 `tokens`，**仅字面 `"1"` 合法**，其他任何值 → 400 `invalid_tokens`（与 messages `directory_not_allowed` 同款严格性）；缺省（无参数）= 行为逐字节不变（零 `token` 帧，客户端零影响）。激活时：在既有策展 SSE 流之外，客户端额外收到 lean `{type:"token", sessionID, messageID, partID, delta}` 帧——载荷为既有 token-stream flush loop（100ms / 4KiB 窗口）对每个 `(sessionID, messageID, partID)` 完成窗口拼接的增量 concat，**覆盖所有 session 的合并 delta（MVP，无 per-sid 过滤）**。帧无 `event:` 头，客户端按 `data.type=="token"` 分发（动画层消费）。**背压**：复用既有 `Subscriber.put` T3 守卫，无新路径——`token` 帧溢出时与 per-session token stream 相同语义：`resync{reason:"subscriber_backpressure"}` + 断连。**生命周期**：events-token 消费者计入 token flush loop 合并账本——即使没有任何 per-session token stream 打开，仅 events-token 消费者也能保活 flush loop（~100ms 节奏持续收帧）；最后一个连接断开（两账本皆空）flush loop 停止。客户端侧与 per-session token stream 互斥（服务端不强制踢双订阅者）。
- **`GET /slimapi/messages/{sid}?mode=merged` 服务端合并（L2-CD-2，加性变更，未 bump `X-Slimapi-Version`，仍 2）**：新增可选 query 参数 `mode`，**仅字面 `merged`（大小写敏感）激活**；`mode=full` 及任何其他值**静默忽略**（行为与缺省逐字节一致，oracle §C-1，不 400——过渡期客户端零影响）。激活时：当页 skeleton 投影中被折叠为 `thin_placeholder` 的消息（无可渲染 part）在服务端内联展开——placeholder parts 被替换为该消息的 full 投影（剥 LSP diagnostics，与 `/full` 同一投影）；**其余消息与 `X-Next-Cursor` 与缺省模式逐字节一致**。内部 full 抓取与并发 direct `/full` 同键去重（复用 CD-1 single-flight，上游仅 1 次 GET）；fan-out 并发受 `OC_SLIMAPI_MERGED_FANOUT`（默认 8）限制、每页最多 `OC_SLIMAPI_MERGED_MAX_FULLS_PER_PAGE`（默认 16）条展开、抓取预算 `OC_SLIMAPI_MERGED_MAX_BYTES`（默认 8 MiB）按「预留/退款」记账（每项读取上限 = min(单消息上限, 剩余预算)，预算耗尽的未开始项不发起抓取。**可证明上界的适用范围**：`merged_max_bytes + merged_fanout × 64KiB 读取块`（默认 ≈ 8.5 MiB）仅约束 **merged 自领导抓取**产生的增量缓冲，**不是**整页峰值——single-flight 键不含预算，merged 小预算 waiter 可能并入 direct `/full` 领导、按单消息上限（默认 32 MiB）读取的共享 flight（或其 ≤1s 保留结果），完整顺风车 body 会在聚合结果中**短暂持有至 splice 阶段排除**，即页面瞬时持有缓冲可达单消息上限量级、超出该公式；对顺风车 body 的唯一保证是 **splice 后响应内联总量 ≤ `merged_max_bytes`**——响应大小受控 ≠ 页面持有缓冲峰值受控）——**超页数/超预算/单项截断或失败均渐进降级保持 skeleton 原样**（不 413、不报错，客户端仍可按需 `/full`）。fan-out 期间**不持有 per-full transform 池槽**（oracle §C-2：默认 `max_transforms=1` 下 per-full 持槽会把 16 个网络 GET 串行化并饿死并发 transform / direct /full）；最终打包为单次 offload，争用时遵循既有 `transform_busy` 503 语义（形状不变）。**并发 direct `/full` 不受 merged 预算影响**：direct 的读取上限始终是自身单消息上限；即使连续加入被 merged 小预算截断的共享 flight，重试耗尽后也会独立兜底再取一次——413 仅当 body 真超单消息上限，**绝不因 merged 预算竞争假 413**（最坏代价一次额外上游 GET）。
- **`GET /slimapi/messages/{sid}/full/{mid}` single-flight 共享抓取 + transform_busy 预算吸收（L2-CD-1，加性变更，未 bump `X-Slimapi-Version`，仍 2）**：(1) **single-flight**——并发同 `(sid, mid, directory)` 的 /full 请求（及后续 merged fan-out 抓取，CD-2）合并为**一次**上游 GET（per-key in-flight 去重 + 完成后 ≤1s 的 join 窗口；transform 仍各自做）。可观测差异：同键 /full 在 1s 内重复请求可能复用共享抓取结果（body 与首次抓取一致，不重复打上游；上游计数/access log `upIn` 只记一次）。(2) **预算吸收**——/full 的 transform 槽等待不再单次 `transform_wait_seconds`（默认 2s）即 503，而是在总预算 `OC_SLIMAPI_TRANSFORM_ABSORB_BUDGET_SECONDS`（默认 2.5s，T0 引入）内按剩余预算收窄重试（oracle §D-1：最坏累计等待不超预算）：瞬时占用（>2s 且 <2.5s）现返回 **200**（原先 503）；占用超预算才 503，**503 形状逐字节不变**（`{"code":"transform_busy","retry_after":2}` + `Retry-After: 2`）。503 时上游请求照旧不发出（admission 先于 GET，吸收重试不放大上游负载）。消息列表端点行为不变。健康公告 `features.transformAbsorb` 自 T0 起已置 true，本条落地其行为。
- **`GET /slimapi/permissions` 跨目录 pending permission 聚合端点（L2-B，未 bump `X-Slimapi-Version`，仍 2）**：镜像 `/slimapi/questions` 两阶段 fan-out（`/experimental/session?roots=true&archived=true` 发现 → 每 distinct directory 并发 `GET /permission` + `X-Opencode-Directory`），聚合 pending permission 卡片，解决 v2 删除聚合端点后的冷启动/重连恢复缺口（此前只能轮询 catch-all `GET /permission`，仅见 `process.cwd()` 实例）。上游响应为**裸数组** `PermissionV1.Request[]`（`{id,sessionID,permission,patterns,metadata,always,tool?}`），sidecar 做白名单投影 + 逐条 stamp `directory`，questions 同款 envelope `{items,errors,authoritativeDirectories,discoveryComplete}`——某 directory 失败时 items 聚合成功目录、errors 记账、`authoritativeDirectories` 仅含成功目录（防客户端 replace-all 丢 pending 卡 → session 卡死）。错误语义复用 questions 模式（per-dir 隔离、4xx→`upstream_http_N`、5xx/网络→`upstream_unavailable`、发现失败→503 total failure）。内部预算三旋钮（`permissions_max_response_bytes`/`permissions_fanout`/`permissions_max_aggregate_bytes`）为 ops 面，非 wire。健康公告 `features.permissionEvents`（permission 面完整：events 直推【已有】+ 聚合端点【新】）。

### Ops

- **traffic snapshot 按 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` 自动 prune（P2-1，运维行为，未 bump `X-Slimapi-Version`，仍 2）**：此前 `traffic-snapshot-YYYY-MM-DD.jsonl`（内存账本周期快照，用于跨重启的长期省流趋势分析）**不经任何自动清理**，长期累积需运维手动 `rm`。现新增 ops knob `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS`（**代码默认 `0` = 不删**，本地开发/测试；**生产 systemd unit 配置 `30`**——见 `deploy/oc-slimapi.service`），按天 prune 早于 N 天的 `traffic-snapshot-YYYY-MM-DD.jsonl` 与 `.jsonl.gz`。实现复用既有 access-log maintenance loop（`run_access_log_maintenance_loop` 新增 keyword-only 参数 `extra_prune: Callable[[date], int] | None`）：每个 tick 与 access-log prune **共享同一 `today`**，调用 `prune_old_snapshots(directory, stem, retain_days, today)`，**不另起后台 task**、不引入新的 shutdown 漂移面。**边界保留**：`today - retain_days` 当天的文件**保留**（与 access-log prune 同语义），仅严格更老的文件被删；**不压缩** snapshot（仅按天清理，compress 仍只对 `access-` 前缀）。`Settings.validate()` 新增 `>= 0` 校验。仅运维行为变更，非 wire；客户端无感知。详见 `docs/manual/traffic-accounting.md` §6/§9 + `docs/operations.md` §5.3/§5.4。
- incarnation 状态文件迁移至独立的 `OC_SLIMAPI_STATE_DIR`（默认 `state/`；生产 systemd unit 设为 `%S/oc-slimapi`）。首次启动从旧 access-log 目录原子迁移（单调 +1），旧文件保留不删；新路径损坏回退读取旧路径。运维行为变更，不涉及 wire。
- **sidecar 现以显式 graceful shutdown 超时运行（未 bump `X-Slimapi-Version`，仍 2）**：SIGTERM 后 uvicorn 给活跃连接（含 SSE 订阅者 drain）最多 5s 宽限（`timeout_graceful_shutdown`），然后强制关闭未完成连接。systemd `TimeoutStopSec=15` 作为上限（高于 uvicorn 5s，覆盖默认 90s SIGKILL），避免连接残留导致长时间阻塞。

### Fixed

- **`GET /slimapi`（精确根，无尾斜杠）现在与 `/slimapi/**` 一致返回 404 `thin_route_not_found`（未 bump `X-Slimapi-Version`，仍 2）**：此前精确 `/slimapi`（不带尾斜杠）的请求因 catch-all 路径前缀匹配不满足 `norm_path.startswith("/slimapi/")` 而透传上游 opencode，绕过 sidecar 的路由门禁。现统一为 `norm_path == "/slimapi" or norm_path.startswith("/slimapi/")`，确保精确根路径与子路径均拒绝透传。
- **`GET /slimapi/sessions` 转换池饱和时与 catalog 路由一致返回 503 + `retry_after:2`（加性，未 bump `X-Slimapi-Version`，仍 2）**：转换池饱和时此前返回裸 503 `{"code":"transform_busy"}`，无 `retry_after` body 字段与 `Retry-After` 响应头。现已改用 catalog 公共的 `busy_response` 辅助，返回 `503 {"code":"transform_busy","retry_after":2}` + 响应头 `Retry-After: 2`。**加性 wire**（新增可选 body 字段 + 头，客户端可忽略，不 bump `X-Slimapi-Version`，仍 2）。

### Internal

- **`/slimapi/questions` 内存预算与 sliding window scheduler（P1-1，未 bump `X-Slimapi-Version`，仍 2）**：消除 16×64MiB 病态上界。新增 3 个内部 ops knob（`OC_SLIMAPI_QUESTIONS_MAX_RESPONSE_BYTES` 默认 2 MiB、`OC_SLIMAPI_QUESTIONS_MAX_AGGREGATE_BYTES` 默认 16 MiB、`OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY` 默认 8，范围 1..16）——均非 wire，仅影响 sidecar 内存/并发。模块级 `_FANOUT_CONCURRENCY`/`_fanout_sem` 移除，改用 `app.state.questions_semaphore`（全局跨请求并发上限）。fan-out 从 `asyncio.gather` 改为 sliding window scheduler（`_collect_with_byte_budget`）：严格原序消费、在途任务 ≤ `questions_fanout_concurrency`、按 `_MAX_AGGREGATE_ITEMS`（10000，保留为第二层 item cap）与 aggregate byte cap 双预算触发截断。任一预算触发时复用既有加性字段 `truncated:true` + `authoritativeDirectories` 降级为 succeeded 列表（partial-replace），不 bump wire 版本。截断路径保证所有已打开 response 被 `aclose`（含已取消的 in-flight 任务）。`config.validate()` 新增四项校验（concurrency [1,16]；per_dir > 0；aggregate >= per_dir；aggregate ≤ 128 MiB）。详见 `docs/operations.md` §5.5 与 `docs/specs/INTERFACE_MAP.md` questions 行。

### Security

- /slimapi/actions 子进程改为 fail-closed 环境 allowlist：仅继承 PATH/HOME/LANG/LC_ALL/LC_CTYPE/TMPDIR/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS，剔除 OC_SLIMAPI_* 等 sidecar 专属变量。安全加固，不涉及 wire（X-Slimapi-Version 仍 2）。

### Docs

- **直连退役说明（目标态：ocdroid 仅经 slimapi；`:14096` 保留至 C1/C3 前置完成；未 bump `X-Slimapi-Version`，仍 2）**：ocdroid 将**退役直连路径**（`:14096` mTLS → opencode `:4096`），仅走 slimapi（`:14097` → sidecar `:4097`）。实证背景：4 天 access log 40853 reqs **100%** 走 `/slimapi/**`、0 passthrough；客户端无 slim→direct 自动回退。**前置条件（ocdroid 侧，本仓只锁契约）**：C1 `HttpImageHolder` 图片加载改走 sidecar catch-all `GET /file`（带 `X-Opencode-Directory`）；C3 `checkHealthFor` 连接自检改打 `GET /slimapi/health`（带 `X-Slimapi-Version: 2`）。**完成前 `:14096` 仍是 ocdroid 的回退路径**（`slim=false` / Manual 连接源仍可用），前置完成后方退役。**退役范围**：删 ocdroid `slim` 开关（`slim=false` 分支）、Manual 连接源、`StreamingMode.Standard` 轴、`4097→14097` 迁移残留、`4096` 默认值；L2 四能力（`tokenCoalesce`/`permissionEvents`/`serverMerge`/`transformAbsorb`，见上方 Added）落地后删对应客户端协调器（per-session stream 客户端 / permission 轮询 / 本地合并协调器 / transform_busy 处理路径）。退役后直连保留给**匿名消费方**（非 ocdroid 的其它 HTTP 客户端仍可直连，sidecar 不干预）。实施由 ocdroid 仓库承担；详细清单见 `docs/specs/CLIENT_CHANGES.md`「直连退役」章节。

---

## [1.3.2] - 2026-08-09 — `/slimapi/actions` 客户端回退语义文档澄清（未 bump `X-Slimapi-Version`，仍 2）

### Docs

- **`docs/specs/CLIENT_CHANGES.md` §"通用管理动作" 补 slim-fail-open 回退语义说明（未 bump `X-Slimapi-Version`，仍 2）**：明确两种"无可用动作"信号对客户端**等价**——(1) 旧 sidecar（pre-v1.3.0）`GET /slimapi/actions` → `404 thin_route_not_found`；(2) 新 sidecar（v1.3.0+）未配 manifest → `200 {"enabled":false,"actions":[]}`。两种情况下管理动作入口**常驻** UI、渲染为空状态即**合规透明回退**，无需据信号类型条件性显隐。仅文档澄清，无 wire 行为变化。

---

## [1.3.1] - 2026-08-09 — patch/apply_patch 工具卡 diffStats 徽章修复（wire 行为修复，未 bump `X-Slimapi-Version`，仍 2）

### Fixed

- **patch 类工具卡（apply_patch / 多文件 write）现在正确显示 diffStats 徽章（未 bump `X-Slimapi-Version`，仍 2）**：骨架投影（`GET /slimapi/messages/{sid}`）中，patch part 的 `diffStats = {additions, deletions, files}` 此前被写到一个客户端从不读取的顶层位置（part 根级 `diffStats`），导致 patch/apply_patch 工具卡的增删行数徽章从未出现；而 edit 类工具（走不同代码路径）一直正常。现已统一到客户端唯一读入口 `state.metadata.diffStats`，与 edit 类一致。**加性修复**：旧顶层位置无任何消费者（ocdroid `displayDiffStats` 只读 `state.metadata?.get("diffStats")`），故对已知客户端无破坏性；patch 工具卡从"徽章缺失"变为"徽章正常"。`files[]` per-file 明细保留不变。注：`/slimapi/messages/{sid}/full/{mid}` 走独立投影（仅删 diagnostics，不注入计算型 diffStats），不在本修复范围。

---

## [1.3.0] - 2026-08-09 — `/slimapi/actions` 通用能力框架（exec/query 两类，配置驱动 manifest，加性）；未 bump `X-Slimapi-Version`，仍 2

### Added

- **`/slimapi/actions` 通用能力框架（加性新增，未 bump `X-Slimapi-Version`，仍 2）**：配置驱动的服务器端管理端点，TOML manifest（`OC_SLIMAPI_ACTIONS_FILE`）声明动作清单，客户端按名调用（argv 由 manifest 固定，无可变参数）。两类动作：**exec**（`{kind:"exec", ok, exit_code, duration_ms, message}`）与 **query**（`{kind:"query", ok, markdown, exit_code, duration_ms, truncated, message}`；`message` 成功时为 `null`）。`GET /slimapi/actions` 发现、`POST /slimapi/actions/{name}` 调用。7 个错误码：`action_not_found`/`action_confirm_required`/`action_throttled`/`actions_disabled`/`action_unavailable`/`action_busy`/`action_timeout`。sidecar 本地端点，不走 catch-all 反代。详见 `docs/specs/v2-contract.md` §2。

### Security

- **`/slimapi/actions` POST 请求体上限 1 KiB（rev-13，新错误码 `request_too_large`，未 bump `X-Slimapi-Version`，仍 2）**：`POST /slimapi/actions/{name}` 的请求体在 **admission 前**被大小门禁——`Content-Length` 声明 >1024 字节或 chunked 实读超 1 KiB → **413 `{"code":"request_too_large"}`**（此前 body 被全量缓冲后才解析，明文端点存在内存 DoS）。合法 body 恒为空或 `{"confirm":true}`（~17 字节），1 KiB 门禁不影响任何有效调用。新错误码已登记 v2-contract §7 + INTERFACE_MAP。**加性**，不 bump `X-Slimapi-Version`。

### Fixed

- **`/slimapi/actions` 进程生命周期统一清理（rev-13，未 bump `X-Slimapi-Version`，仍 2）**：(1) **stderr 日志 cap 改按字节计**——此前按 chunk 数（`<65536` chunks）判，每 chunk ≤4 KiB → 实际可累积 ~256 MiB 才触发声称的 64 KiB journald cap；现按累计字节（≤64 KiB）截断保留缓冲，仍**始终 drain 到 EOF**（防 pipe 阻塞致假超时）。(2) **drain-hang 根治**——asyncio 的 `Process.wait()` 仅在 stdout/stderr pipe 也断开时才返回（`base_subprocess.py` `_try_finish`），孙进程持 pipe 写端时即使子进程已退出 `wait()` 也会挂死、drain 无 deadline；现子进程退出检测改用 `returncode` 轮询（与 pipe 无关），退出后**立即 killpg**（杀掉持 pipe 的孙进程释放管道），并给 drain 加 **5s 硬 deadline**（超时 force-cancel drain、warning、以已收部分作答、query 标 `truncated`）。(3) **统一 try/finally 清理**——`finally` 无条件 killpg(SIGKILL) + reap + 失败路径审计，覆盖正常退出/timeout/**drain 期取消**等所有路径；semaphore 等待期取消也补审计（原 Bug E：无审计记录）。(4) spawn 的 `ValueError`（argv 含 NUL / 坏 cwd）也映射 `action_unavailable`。均为内部生命周期修复；wire 可见变化仅 query `truncated` 可能因 drain-deadline 置真（edge case）与新增 `request_too_large`（见上）。

---

## [1.2.0] - 2026-08-08 — 加性新端点 `GET /slimapi/directories`（全局 directory catalog，项目切换器）；未 bump `X-Slimapi-Version`，仍 2

### Added

- **`GET /slimapi/directories`（全局 directory catalog，加性新增，未 bump `X-Slimapi-Version`，仍 2）**：全新端点，列出 opencode 已知的工作目录（directory），供客户端渲染"项目切换器"。发现源 `GET /experimental/session?roots=true&archived=true&limit=10000`（复用 questions 的全局顶层 session 发现——每个 session 携带真实 `directory` 字段，覆盖 git repo + 非-git目录 + git worktree 子目录 + archived-only workdir）。**无 query 参数**（全局发现语义，干脆不接受 directory，避免误导——与 catalog 端点的 no-op `directory` 不同）。按 `normalize_directory` 归一聚合：每 dir 一行 `{directory,title,lastUpdated,rootSessionCount,activeRootSessionCount,archivedRootSessionCount,archivedOnly}`（`title`+`lastUpdated` 来自同源 winner——按 `(time.updated,time.created,id)` 取 max；数字字段缺失/非数字→0 排最后）；返回 **envelope 对象** `{items, discoveryComplete}`（非裸数组；`discoveryComplete=len(sessions)<10000`，items 排序 `lastUpdated` DESC + tie-break `directory` ASC）。**严格 schema 守卫**：任一 session 非 dict、或 `directory` 非非空 string → 整体 **503** `upstream_unavailable`（不静默跳过——跳过会让"看似完整"列表缺目录）。错误映射同 questions 发现调用（网络/5xx/**4xx**/坏 JSON/非 list/超 cap→503 `upstream_unavailable`，不泄漏 upstream status；experimental 端点 4xx 意 opencode 不支持）；转换池 admission 先于 upstream GET + 流式 `read_with_cap` + worker gzip；转换池满→503 `transform_busy`+`Retry-After:2`。**被动发现局限（诚实）**：仅能看到至少有一条顶层 session 的 workdir；**从未建过 session 的 workdir 不可见**；**不扫文件系统**；**返回目录不代表目录仍存在**（可能已删）。**加性**：旧 sidecar 无此路由→catch-all 404 `thin_route_not_found`，客户端 fallback（仅对 404 `thin_route_not_found` 降级，**不**对 503/413 fallback）。详见 `docs/specs/v2-contract.md` §2 + §2「`/slimapi/directories` envelope」；新增独立 traffic bucket `directories`（`/slimapi/metrics.traffic` + access log `bucket` 字段）。

---

## [1.1.6] - 2026-08-08 — 多维度代码质量评审完善（7 批次，未 bump `X-Slimapi-Version`，仍 2）

> 基于 5 维度独立代码质量评审（4× rev-gpt A/B/C/D + explorer E）汇总 → oracle 审定 → 7 批次实施。消除 9 P0 + ~30 P1 + 2 P2，+193 回归测试（1055→1248），0 回归。批次 3（SSE/token 生命周期，最高风险）经 oracle 状态机稿审定 + rev-grok(9/10)/rev-glm(8.5/10) 双终审 gate 放行。多数为内部资源安全 / 契约收敛修复；wire 可见项见下方 Added/Changed/Fixed。设计 / 决策记录见 `.ocmar/workflows/2026-08-08-code-quality-overhaul/`（plan.md + batch3-lifecycle-statemachine.md）。

### Added

- **`GET /slimapi/questions` 聚合超预算标记 `truncated`（P1-28，加性诊断字段，未 bump `X-Slimapi-Version`，仍 2）**：跨目录 fan-out 聚合结果累计 items 数超 `_MAX_AGGREGATE_ITEMS`（10000）时，envelope 新增 `truncated: true` 诊断字段，后续目录不再 extend。`authoritativeDirectories` 同步降级为 succeeded list（partial-replace，与 discovery 截断相同语义），客户端不会因聚合截断丢弃未跳过目录的 pending questions。客户端可忽略该字段（加性）。

### Changed

- **version gate 生产固定 v2（P1-13，防 env 放宽，未 bump `X-Slimapi-Version`，仍 2）**：`Settings.validate()` 现拒绝 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` 解析结果不为 `(2, 2)` 的配置——env 可被解析（格式错误仍 fail-fast），但值必须恰好是 `(2, 2)`。此前 `validate()` 只查 `minimum >= 1 and minimum <= maximum`，env `1,2` 或 `1,1` 即可让生产 sidecar 接受 v1，破坏 fail-closed 版本策略。`/slimapi/health` 回显的 `accepted_client_versions` 现始终为 `[2, 2]`（与权威 v2 契约一致）。**无 dev override**：env 本身是被加固的攻击面，提供 env-based escape hatch 会自相矛盾；需测试 v1 的开发者可临时编辑 `versioning.py` 常量。
- **request-id 限可打印 ASCII（P1-15，未 bump `X-Slimapi-Version`，仍 2）**：`RequestIdMiddleware` 的 `_find_request_id` 现仅接受可打印 ASCII（0x20–0x7e）的入站 `X-Request-ID` 值。非 ASCII（含多字节 Unicode 如中文）→ 视为非法，生成新 uuid。此前用 `decode("utf-8","replace")` 接受非 ASCII Unicode，catch-all 反代将其写入 httpx header 时 `client.build_request()` 在 send try 之前抛编码异常 → 裸 500。**行为变更**：此前发送非 ASCII request-id 的客户端会收到该值回显（+ 上游 500）；现收到新生成的 uuid hex（+ 正常响应或结构化错误）。
- **明确 `downIn` / `downOut` 为 ASGI 传输层字节口径（含 early-reject 说明）**：流量记账中间件（`middleware/traffic_accounting.py`）的模块 docstring 现显式声明 `downIn` / `downOut` 统计 ASGI 传输层实际收发字节（与上游 `upIn` / `upOut` 对称的「全链路双向计费」）。据此，version gate 等中间件 early-reject 的请求，因 app 未调用 `receive` 消费 body，该 body 不计入 `downIn` —— 这是 wire 口径的真实反映（app 未接收即未传输到 app），不为计一个被拒请求付出真实 I/O 代价（不 drain）。口径本身无行为变更（中间件始终只计 app 实际 `receive` 的字节）；此条为可观测面口径的显式文档化。
- **路由层 stream+cap+error 样板去重 + upstream error 映射统一（批次 6，内部重构，非 wire 行为变更，未 bump `X-Slimapi-Version`，仍 2）**：(1) `upstream_errors.py` 现提供完整公共 helper——`raise_upstream_unavailable`（网络/5xx/非 list/坏 JSON → 503）、`raise_upstream_status_code(status, sid=)`（drain 后状态映射：404+sid→`session_not_found`，其余 4xx→`upstream_http_N`，5xx→`upstream_unavailable`）、`upstream_error_code_for_status`（返回 code 字符串供 envelope 隔离用，如 questions fan-out）；sessions/messages/questions/_catalog_common 各自内联的 ~20 处 `CodedHTTPException(503, code="upstream_unavailable")` 字面量及复制的状态映射逻辑改用这些 helper。每条 route 的错误码语义**完全不变**（重构非行为变更）。(2) sessions/messages/catalog 共有的「drain-or-cap-read + 状态映射 + mid-stream RequestError→503」骨架提取到 `_catalog_common.read_upstream_response`（不关闭 response，caller 保留各自 try/finally aclose / TransformBusy 处理）；admission 获取、upstream send（headers/params/timeout 因 route 而异）、TransformBusy 处理保留 per-route（非完全相同，不强求统一）。
- **`check_routes_doc.py` 增强 method 校验（批次 6，工具增强，非 wire 行为变更）**：路由↔文档一致性门禁此前只查「路径字符串是否出现在 INTERFACE_MAP」，现收窄并强化为：(a) 只解析**当前接口表行**（`|` 开头），prose/历史段/删除区中的路径提及不再满足存在性校验；(b) 校验 **HTTP method 一致**——代码侧 `@router.<method>` 与文档表行 `**<METHOD> \`<path>\`**` 的 method 必须匹配（此前 GET 改 POST 仍通过）；(c) 代码侧改用 `ast` 收集声明路由，覆盖多行装饰器 / `@router.api_route(methods=[...])` / `@router.options`（旧单行正则漏这些）。保留 `routes/*.py` 静态扫描（非 `app.routes` 运行时遍历，避免 import 副作用），已知局限已在脚本顶部 docstring 注明。`check.sh` 调用方式不变。

### Fixed

- **turn-registry LRU eviction 现记录 warning（B7 / P1-23，纯可观测，非 wire 行为变更，未 bump `X-Slimapi-Version`，仍 2）**：`TurnRegistry.bump_turn` 在 `_TURNS_MAX`（10000）LRU eviction 实际发生时记录 warning（含被 evict 的 sid + 当前 incarnation），便于运维观测这个 practically-unreachable 边缘（需 >10000 个 distinct bumped sid 在单进程内）。**行为不变**：eviction 维持——oracle 裁定"eviction→新 incarnation"的 cure 会扩大爆炸半径（incarnation 是进程级冻结），故采纳"维持 LRU + 加可观测 warning"。
- **边界 + 门禁硬化（批次 5，内部资源安全修复，未 bump `X-Slimapi-Version`，仍 2）**：修复 8 处资源边界 / 门禁 / 输入校验 / 错误映射问题（除上述 P1-13/P1-15/P1-28 已在 Changed/Added 记录的 wire 可见项外，其余为内部硬化）：
  - **P1-14（version gate 路径归一化）**：version middleware 此前直接检查原始 `scope["path"].startswith("/slimapi/")`；ASGI server 若不折叠 `//`，`//slimapi/foo` 不过版本门禁但 catch-all 反代归一化后路由到 /slimapi/ 端点 → 门禁绕过。`/slimapi` 精确根路径也不满足 `/slimapi/` 前缀。现 `_is_slimapi_path()` 折叠重复斜杠后判断，同时识别根路径与子路径。`scope["path"]` 不变（不影响下游路由）。
  - **P1-29（skeleton 嵌套类型防守）**：`skeleton_message` 此前 `message.get("info").get("id")` 在 info 为 None 时 AttributeError；`for part in message.get("parts")` 在 parts 为 int/bool 时 TypeError → 单坏消息致整页 500。现 info 非 dict → `{}`，parts 非 list → `[]`。`routes/messages.py` 两处 skeleton 调用增加 `(TypeError, AttributeError)` → 503 `upstream_unavailable` 映射（与既有 JSONDecodeError 映射对齐）。
  - **P1-30（TransformPool RSS 上界）**：`config.validate()` 加 `max_transforms × max_response_bytes > 512 MiB` → raise（防误配置 OOM）。`transform.py` 模块 docstring 文档化 RSS 内存模型：最坏 ≈ `max_transforms × (max_response_bytes + projection overhead)`，建议生产 `max_transforms=1`。
  - **P1-31（gzip 小响应阈值）**：transform worker pack 函数（`_pack_json` / `_project_list_sorted_and_pack` / questions envelope）现经 `compress_if_beneficial()`：body < `MIN_GZIP_BYTES`（64）或压缩后 ≥ raw → 返回 raw 不加 Content-Encoding（gzip header/footer 开销反使小响应变大，增加 downOut + access-log 计费）。`json_response` / `error_response` 不变（契约 §9 要求所有 JSON 路由含小错误体统一 gzip 协商）。
  - **P1-41（TransformPool shutdown 超时）**：`shutdown(wait_seconds=10.0)` 加超时参数：cancel pending futures → daemon thread bounded wait → 超时返回（不 drain 在途 worker）。此前 `executor.shutdown(wait=True)` 无超时，hot reload 遇大响应/异常 worker 时阻塞事件循环超 uvicorn graceful 窗口。`app.py` `_shutdown_transforms` 传 `_TRANSFORM_DRAIN_TIMEOUT`（10s）。
  - **P1-28（questions 聚合序列化 offload）**：`/slimapi/questions` 最终 envelope 的 `orjson.dumps` + gzip 现 offload 到 TransformPool executor（`pool.offload`），不再在 event loop 阻塞 SSE 心跳。聚合不获取 admission（已由 `_MAX_AGGREGATE_ITEMS` + per-dir cap 内存限制）。

- **`session.deleted` 现服务端终止 token 订阅（INV-4 / P0-3，未 bump `X-Slimapi-Version`，仍 2）**：`GET /slimapi/sessions/{sid}/stream`（token SSE）此前在收到上游 `session.deleted` 时只入一个 deferred resync 帧（由 flush loop 下 tick drain），**不发 STOP**——token route generator 永远等下一帧，订阅者连接不释放（`total_subscribers` 不减、`_subs_by_sid` 不移、flush loop 不停）。资源释放完全依赖客户端主动关连接。现 `on_session_deleted` 对该 sid 的每个订阅者**同步逐个** `sub.terminate("session_deleted")`——发 `resync{session_deleted}` → `STOP` 严格此序，generator 收 STOP 退出 → finally 走正常 unsubscribe（detach + 减计数 + last-detach stop flush + grace arm）。客户端现在**一定**会收到 `resync{session_deleted}` + 连接断开（此前可能永远收不到断开信号）。**加性 wire 修正（多了一个确定的服务端断开信号），不 bump** `X-Slimapi-Version`（客户端原本就需处理 resync + 连接关闭）。
- **SSE/Token 生命周期状态机闭合（批次 3，内部资源安全修复，未 bump `X-Slimapi-Version`，仍 2）**：修复 7 处 task 生命周期 / epoch 串行 / 资源安全问题（均非 wire 可见，仅影响内部资源安全）：
  - **INV-1（P1-19）**：GlobalHub run/flush/heartbeat 三 task 现为原子组（supervisor done_callback）；任一非 cancel 死亡 → cancel 兄弟 + `has_consumers()` 时重建；TokenStreamHub `_flush_task` 同款看门狗。修掉 run 异常后孤儿/重复 flush/heartbeat。
  - **INV-2（P0-2）**：grace removal 现 cancel+`gather` hub 全部 task（等旧 run 完全退出释放 `/global/event` 连接）+ re-check（gather 期间新订阅可放弃 removal）+ 同步段 `token_hub.on_upstream_reconnect()` 清旧 epoch 状态（`_part_revisions` / `_removed_messages` 保留）。修掉跨 epoch 脏数据。
  - **INV-3（P1-20）**：`TokenStreamRegistry.subscribe` 现包 ensure_upstream / start / attach 整段 try/except——任一异常（QueueFull / 序列化等，非仅 closed）触发对称 rollback（flush 停 + grace 重 arm + 计数不增）。
  - **INV-5（P1-17）**：`app.py` 构造 `TokenStreamHub` 现传 `max_frame_bytes=settings.token_stream_max_frame_bytes`（hub 与 subscriber frame ceiling 同源）。
  - **INV-6（P1-18）**：上游正常 EOF（aiter_lines 正常结束）现视为 upstream loss（notify + sleep + 退避），修掉 EOF 热循环；重连分支 `_notify_upstream_loss()` 加 `not _upstream_loss_notified` 守卫，修掉 EOF/异常/重连三路径双重 notify。
  - **P1-22**：新增 deleted-sid gate（bounded + TTL），`on_session_deleted` 后晚到的同 sid part 事件直接 drop（不重建 LivePart）。
  - **P1-21**：`_session_status` / `_busy_sids`（token hub）、`sticky_last_error` / `deleted_tombstones`（global hub）加 FIFO cap（对齐 `_LAST_UPDATED_AT_BY_SID_MAX` = 10k），防高 churn 无限增长。

- **`read_with_cap` 中途断连已读字节正确计入 `upIn`（P0-9）**：`GET /slimapi/sessions`、`GET /slimapi/messages/{sid}`（list）、`GET /slimapi/messages/{sid}/full/{mid}`、`GET /slimapi/agent`、`GET /slimapi/command`、`GET /slimapi/questions`（发现调用 + per-dir fan-out）等 thin 路由在流式读取上游 body 时，若上游中途断连（`httpx.RequestError`），此前已读字节的 `stash_up_in` 在异常退出路径中不执行 → 该请求的 `upIn` 漏计（记 0）。现 `read_with_cap` 通过 `on_read` 回调在逐 chunk 读取时即时记账（覆盖成功 / cap 超出 / 异常三条路径），中途断连已读字节不再丢失。错误码 / 状态码不变；cap-bail upIn 口径（B1）不变。

- **`GET /slimapi/messages/{sid}`（list）上游中途异常现映射 503 `upstream_unavailable`（P1-24）**：messages list 分支此前只有 `finally: await response.aclose()`，无 `except httpx.RequestError`，上游 body 读取阶段的中途断连（`ReadError` / `ReadTimeout`）逃逸为裸 FastAPI 500。现与同文件 `/full/{mid}` 分支、sessions、catalog 路由对齐，映射为结构化 `503 {"code":"upstream_unavailable"}`。加性硬化，不 bump `X-Slimapi-Version`（无 client 依赖现有裸 500）。
- **catch-all 反代 upstream response 中途流异常时保证关闭（P1-10）**：catch-all 反代（`proxy.py`）的 upstream response 关闭此前依赖 `StreamingResponse` 的 `BackgroundTask(response.aclose)`，但 BackgroundTask 在 generator 异常退出（客户端中途断连 / 上游中途错误）时不保证执行 → 连接池连接泄漏。现 `_counted_upstream_response` 的 `finally` 追加 `await response.aclose()`（与 BackgroundTask 幂等共存，httpx aclose 可重入），异常路径由 finally 兜底，正常路径仍由 BackgroundTask 关闭（幂等无副作用）。
- **incarnation 持久化改原子写（P0-4，落盘原子性 / 运维面）**：`IncarnationStore._write_persisted` 此前用 `path.write_text` 直接 truncate 覆盖最终路径，无 fsync、无原子替换——进程崩溃 / 掉电可能留空文件或半写文件 → 下次 `_read_persisted()` 按 0（损坏兜底）处理 → 重启复用 incarnation 1（复用旧 turn fence，破坏因果边界）。现改为写 sibling `.tmp` → `flush` + `os.fsync` → `os.replace` 原子提交，崩溃后磁盘要么是旧值要么是新值，永不半写；失败路径清理 `.tmp`，best-effort 不 crash lifespan 的容错保留（写失败 warn + 返回 False，in-memory 继续）。
- **access log 维护操作加进程内锁 + 唯一临时文件名（P0-8，落盘原子性 / 运维面）**：`compress_old_access_logs` / `prune_old_access_logs` / `migrate_legacy_access_log` 此前用固定 `.gz.tmp` 路径且无跨线程串行化——startup（主线程）与 maintenance loop（`to_thread` 线程池）或 hot-reload 并发时，后者删 / 写前者的 tmp → 破坏 gzip 归档。现加模块级 `threading.Lock`（`_MAINT_LOCK`）串行化三者的文件操作，临时文件改唯一名（`.{suffix}.{pid}.{8-hex token}`，仅由创建者清理）。`_cleanup_leftover_tmp` 同步覆盖新旧两种 tmp 命名 glob。`run_access_log_maintenance_loop` docstring 注明 cancel 时不 drain 已启动的 to_thread gzip 线程（调用方 app.py 负责，批次 4）。
- **access log compress 跳过活跃 handler 持有的源文件（P1-25，落盘原子性 / 运维面）**：`DailyAccessHandler` 只在下次 emit 时按 `record.created` 切日，跨午夜空闲（无新请求）时仍持有昨天 `.jsonl` 的 fd。维护 compress 昨天 file（unlink 源）后，fd 仍开，inode 空间不释放（直到下次请求触发换日或进程退出）。现 `DailyAccessHandler` 加只读属性 `current_path`（返回当前持有的完整路径或 None），`setup_access_log` 安装时记录模块级 `_active_handler_ref`，`compress_old_access_logs` 在 unlink 源前检查该文件是否等于活跃 handler 持有的路径——是则**跳过本次 compress**（保留 `.jsonl`，下次维护周期再 compress）。权衡：跨午夜后首条维护少压缩一个文件，但避免删活跃 fd 的源。
- **traffic snapshot 单次 write + 单一时间采样点（P1-27 + P1-26，落盘原子性 / 运维面）**：`TrafficSnapshotter._write_once` 此前 `f.write(json); f.write("\n")` 两次 write，crash 在中间留半行（无尾换行）→ 离线 `json.loads` 整文件失败（半行无换行分隔符与下行合并）。现合并为单次 `f.write(json + "\n")`（POSIX 小 write 原子性更好，Python 层消除半行窗口；不加 fsync，权衡见代码注释——快照本为 best-effort 冗余）。同时 `ts` 字段（`datetime.now()`）与 daily path（`date.today()`）此前是两个采样点，跨午夜可归类错日（ts 标 day N+1 但文件名 day N）——现 `_write_once` 开头单次采样 `now = datetime.now().astimezone()`，`ts` 与 daily path 同源派生（`now.isoformat()` / `now.date()`），消除跨午夜归类错位；移除随之失效的 `_daily_path` helper 与未用的 `date` import。
- **catch-all 反代错误响应遵守 Accept-Encoding gzip 协商（P0-5，契约 §9，未 bump `X-Slimapi-Version`，仍 2）**：catch-all 反代（`proxy.py`）的 4 类 early-reject 错误响应——`invalid_path`（400）/ `thin_route_not_found`（404）/ `invalid_directory`（400，header 与 query 两路径）/ `shell_not_allowed`（403）——此前直接 `JSONResponse({...})` 不经 gzip 协商，违反契约 §9「所有 JSON 路由统一 gzip」（thin 路由 errors 与 1.1.5-C3 版本门禁 400 已走 gzip）。现统一走 `gzip_util.error_response(code, status, accept_encoding=request.headers.get("accept-encoding"))`，body 形态 `{"code":"..."}` 不变（仅加 `Content-Encoding: gzip` + `Vary: Accept-Encoding`，客户端不发 gzip 时仍为明文）。**加性 wire 一致性修正，不 bump** `X-Slimapi-Version`（无破坏性，仅给原有能力补上）。
- **catalog（agent/command）与 /ready 上游请求透传 X-Request-ID（P0-6，契约 §7，未 bump `X-Slimapi-Version`，仍 2）**：thin 路由 catalog（`agent` / `command` 经 `_catalog_common.stream_upstream`）与 `/slimapi/ready` 的上游 opencode 请求此前缺失 `X-Request-ID` 头，破坏契约 §7「sidecar access log 与 opencode 日志靠 request-id 关联」的关联性（catch-all 反代已有此头，但 thin 路由未补齐）。新增 `upstream.forward_upstream_headers(directory, request_id)` helper（合并 directory + request-id，二者 header 名天然不冲突），catalog 与 ready 经之注入 request-id（从 `scope.state[REQUEST_ID_KEY]` 读，由 `RequestIdMiddleware` 注入）。其它 thin 路由（messages / sessions / questions）的 directory header 转发逻辑不变（其它批次负责补齐 request-id）。**加性可观测面补齐，不 bump** `X-Slimapi-Version`。
- **catch-all 反代保留原始 query string 透传（P0-7，契约 §4 透明反代，未 bump `X-Slimapi-Version`，仍 2）**：catch-all 反代此前用 `params=request.query_params.multi_items()` 让 Starlette 解码再由 httpx 重编码 query string，破坏 percent-encoding / `+` / 空参（`?flag`）/ 顺序保真，违反契约 §4「透传 verbatim」。现改读 ASGI scope 原始 query bytes（`scope["query_string"]`）拼到上游 URL，`build_request` 时 `params=None`（不再重编码）。`?directory=` 的安全校验（`validate_directory`）仍基于解析后的 `query_params`，不变。**对客户端透明（仅影响上游收到的 URL 形态），不 bump** `X-Slimapi-Version`。
- **catch-all 不再误删 `Content-Length` + 不丢重复响应 header（P1-11，契约 §4 透明反代，未 bump `X-Slimapi-Version`，仍 2）**：`upstream.strip_hop_by_hop` 此前把 `content-length` 列入 hop-by-hop 集合——它非 hop-by-hop（RFC 7230 §6.1，是 representation/framing header），catch-all response 误删它导致下游客户端看不到上游报告的字节数；同时 `Mapping.items()` 把 httpx.Headers 的重复字段（如多 `Set-Cookie`）合并成单值，破坏多值 header。现：(a) 从 HOP_BY_HOP 集合移除 `content-length`（request 与 response 透传都受益；StreamingResponse framing 由 uvicorn/httpx 按流处理）；(b) 读端改用 `multi_items()`（httpx.Headers 与 starlette Headers 都支持，plain dict 回退 `.items()`），重复键按 RFC 7230 §3.2.2 逗号合并进单 slot（Starlette Response headers 的固有限制——对 `Set-Cookie` 不完美但优于丢值，代码注释已说明）；(c) 顺手把 `proxy-connection`（部署中常见的非标准连接级 header）加入 blocked 集合。**加性透明性补齐，不 bump** `X-Slimapi-Version`。
- **catch-all timeout 分类容忍 trailing slash（P1-12，未 bump `X-Slimapi-Version`，仍 2）**：catch-all per-request timeout 分类（SSE → read=None / command → 300s / 其它 → 30s）此前基于 `norm_path` 直接判断，而 `_normalize_path` 只折叠 `//` 不去尾斜杠 → `/event/`、`/command/` 不进 SSE/command 长超时，套用 30s 默认，可能误杀长连接。现分类前 `classified = norm_path.rstrip("/") or "/"`，仅用于 timeout 分类，**不影响转发路径**（转发仍用 `norm_path`）。**纯内部 timeout 调度修正，无 wire 变更，不 bump** `X-Slimapi-Version`。
- **启动生命周期 + 配置硬化（批次 4，内部资源安全修复，未 bump `X-Slimapi-Version`，仍 2）**：修复 8 处启动生命周期 / 配置校验 / 错误边界问题（均非 wire 可见，仅影响内部资源安全 / 运维诊断面）：
  - **P0-1（启动事务式回滚）**：`@asynccontextmanager` lifespan 在 yield 前发生异常时**不进入** finally → 跳过统一清理 → httpx client / executor / hub task / maintenance task 泄漏。现用 `contextlib.AsyncExitStack` 重构——每个需清理的资源创建后立即注册清理 callback，AsyncExitStack 退出时按 LIFO 自动清理，**无论 yield 前异常还是正常 shutdown 都执行**。per-component isolation 保留（每个 callback 内部 try/except，一个失败不跳过其它）。关键约束 `token_hub.stop()` BEFORE `hubs.close()`（NB-C4）通过注册顺序保证。
  - **P1-35（Settings 校验完整性）**：`validate()` 补 `port` 范围 [1,65535]（port=0 不支持——固定端口客户端配置）、`max_message_bytes` / `max_response_bytes` 上界 ≤ 256 MiB（防 OOM）、`server_api_version` ∈ `accepted_client_versions` 区间一致性（server 广播的版本必须在自身接受的范围内）。`main()` 启动入口捕获 `validate()` 的 `RuntimeError`，经 `setup_logging` 后输出明确配置错误再 `SystemExit(1)`，而非裸 traceback。import-time `int(os.getenv)` 解析失败（如 `PORT=abc`）文档化为已知 fail-fast 边缘（不在本批次做配置工厂大重构）。
  - **P1-34（deprecated access-log path 优先级）**：值比较 `access_log_dir == "logs"` 无法区分「`OC_SLIMAPI_ACCESS_LOG_DIR` 未设置（用默认）」与「显式设置 `=logs`」。当两 env 同时存在且新变量显式设为 `logs` 时，deprecated `ACCESS_LOG_PATH` 仍覆盖新配置（与注释承诺相反）。现 `Settings.effective_access_log_dir()` 用 `"OC_SLIMAPI_ACCESS_LOG_DIR" in os.environ` 判断显式性——显式设新 dir 始终优先；仅新 dir 未设 + deprecated 非默认时才用 deprecated parent fallback + warning。
  - **P1-36（smoke 状态拆分）**：smoke() 所有异常（连接失败/超时/404/JSON 解码失败/sid 不存在）此前都标 `schema_degraded=True`，且「无显式 sid + session list 失败」时 `schema_degraded` 保持 False——同类上游不可用在不同配置下得相反诊断。现拆成可区分的 `app.state.smoke_status` ∈ {`not_run`, `upstream_unavailable`, `invalid_schema`, `valid`}。只有「成功收到可解析上游响应且字段形状不符」才 `invalid_schema`；上游不可用 / sid 不存在 / 非 2xx → `upstream_unavailable`（`schema_degraded` 保持 False）。health/ready 据此可区分诊断。**注意**：此前 smoke 连接错误标 `schema_degraded=True`，现改为 `False`（上游不可达 ≠ schema 回退）——这是诊断面修正（`schema_degraded` 的语义更精确），`/slimapi/health` 的 `schema.degraded` 字段值在纯上游不可达场景下从 `true` 变 `false`（加性诊断精确化，客户端不应依赖「上游宕机时 degraded=true」）。
  - **P1-37（启动暖机探针短 timeout）**：smoke 的 upstream.get 和 lifespan 的 `/global/health` probe 用 create_client 默认 read timeout 30s——upstream 不可达时启动连续等待多个 30s，拖慢 systemd readiness / hot reload。现用显式短 timeout 5s（对齐 routes 的 health timeout）。failure tolerated 但 non-blocking ≠ 长 timeout。
  - **P1-38（maintenance task 回收 + CancelledError 分离）**：(a) `except (asyncio.CancelledError, Exception)` 混捕吞了不该吞的——CancelledError 现单独 except（预期分支），Exception 记 warning；(b) task done + 携 exception 时现 `task.exception()` 读取 + warning（回收未观察 task exception）；(c) graceful drain：shutdown 时先 set stop_event + wait `_MAINT_DRAIN_TIMEOUT`（30s）让 maintenance loop 当前 `to_thread` gzip/prune 完成，超时才 cancel（to_thread 线程不可安全 cancel，继续在后台跑完，bounded work + `_MAINT_LOCK` 保证互斥）。
  - **P1-39（access-log handler 失败门禁）**：`setup_access_log` best-effort，失败时禁用 logger；但 app 忽略安装结果，后续仍按 `settings.access_log_enabled` 判断启动 maintenance（目录不可写时 maintenance 反复访问失败目录，无效 IO + 噪声）。现检查 `access_logger.disabled`（实际安装结果），lifespan 据此（而非 config flag）决定 startup maintenance + 后台 task。失败时记录一次明确降级状态。
  - **P1-40（deployment revision 错误可观测）**：`read_deployment_revision` 的 `except Exception: return None` 吞所有读取错误；env 值纯空白时 `if value` 为真返回 ""；空 revision file 阻断 `CREDENTIALS_DIRECTORY` fallback。现先 strip 再判空；区分「未设置/文件不存在」（静默 None）与「权限/编码/路径错误」（warning 一次保留原因）。

---

## [1.1.5] - 2026-08-07 — P2/P3 改进 + 死代码清理（未 bump `X-Slimapi-Version`，仍 2）

> rev-kimi 代码质量评审（`docs/ocmar/reviews/2026-08-07-rev-kimi-code-quality-review.md`）Handoff 清单 P2/P3/清理项落地。多为内部重构/清理/测试加固；两项行为口径修正（B1/C1）记录如下。**未 bump** `X-Slimapi-Version`（无破坏性协议变更，仍 2）。

### Fixed

- **cap-bail `upIn` 记账口径统一（B1，省流审计面，未 bump `X-Slimapi-Version`，仍 2）**：`GET /slimapi/sessions` 与 `GET /slimapi/messages/{sid}`（list）在 upstream body 超 `max_response_bytes` 触发 413 `response_too_large` 时，此前 `stash_up_in(n_read)` 在 cap-bail 返回**之后**执行 → 超限读字节不计入 bucket `upIn`（与 `/full/{mid}`、`/agent`、`/command` 的「先 stash 后判 None」不一致）。现四处统一为「先 stash 后判 None」，超限读仍归因到对应 bucket `upIn`，省流审计口径一致。错误码/状态码/body 不变。补 cap-bail upIn 断言测试（`tests/test_traffic_upin_gaps.py`）。
- **gzip 协商修正 `gzip;q=0`（C1，协议正确性，未 bump `X-Slimapi-Version`，仍 2）**：gzip 协商此前为子串匹配（`"gzip" in accept_encoding.lower()`），客户端显式 `Accept-Encoding: gzip;q=0`（RFC 7231「拒绝 gzip」）时仍被压缩。现新增 `gzip_util.accepts_gzip()` 按 q-value 解析（`gzip;q=0`→不压缩；`*` 通配；显式 `gzip;q=0` 覆盖通配；`x-gzip` 同义；大小写不敏感），统一用于所有 JSON gzip 决策点（`json_response` / transform `_pack_json` / messages list / agent+command catalog / token stream / 版本门禁 400）。补边界测试（`tests/test_gzip_negotiation.py`）。实际客户端几乎不发 `gzip;q=0`，属协议正确性瑕疵修正。

### Changed

- **版本门禁 400 协商 gzip（C3）**：`SlimapiVersionMiddleware` 的 400（`version_required`/`version_incompatible`）此前直接 `JSONResponse` 不经 gzip 协商；现走 `gzip_util.json_response`，与「所有 JSON 路由 honor `Accept-Encoding`」一致（契约 §9 字面覆盖）。body 极小，纯一致性变更。

### Internal（无 wire 变更，简记）

- **agent/command 去重（B2）**：`routes/agent.py` + `routes/command.py`（~95% 逐行重复）抽取公共骨架 `routes/_catalog_common.py`（参数化 path/projection/timeout）；各自保留 docstring 省流实测数据。行为不变；`read_with_cap` 经参数注入保留 `test_command_routes` 的 monkeypatch 面。
- **`TurnRegistry._turns` 加 LRU 上限（B3）**：唯一无界状态点加 `_TURNS_MAX=10_000` LRU cap（对齐 `sse/global_hub.py` `_LAST_UPDATED_AT_BY_SID_MAX` 模式）。**已知上限 trade-off（前瞻性披露，接受）**：若某 sid 被逐出后**在同一 incarnation 内**再次 bump，其 turn 从 1 重起——这是**同 incarnation 内的 turn 回退**（字典序更低），ocdroid fence 会将后续 digest 视为 stale 直到 turn 重新爬过旧值。**这不等于 restart hole**（restart 会 bump incarnation，本机制不 bump）。实际不可达：单进程内需 >10,000 个不同 sid 有 bump 活动后旧 sid 回归，远超单用户 sidecar 工作集（参考契约 v2-contract.md「已知上限竞态」披露范式）。
- **`smoke()` 单测（B4）**：补 `app.smoke()` schema 校验四分支 + happy path 单测（`tests/test_smoke.py`）。
- **`check_routes_doc.py` 语义校验（B5）**：路由↔文档一致性门禁增加错误码关键词语义校验（白名单：sessions / messages list / messages full / command / agent 的 `session_not_found` / `upstream_http_` / `upstream_unavailable` / `transform_busy`），防 P1-2 那类「路由存在但错误映射描述漂移」；存在性校验保留。
- **死代码清理（D1-D5）**：删 `upstream.decoded_body_headers()`（零调用）；`logging_config.redact()` 注明 test-only；删 config 残留字段 `access_log_max_bytes` / `access_log_backups`（RotatingFileHandler 时代遗留，无消费方）+ 其 env 读取 + validate + `test_traffic_integration` 引用 + INTERFACE_MAP deprecated 描述同步；`GlobalHub.unsubscribe` / `stop_after_grace` 注明 test-only；`traffic.py` docstring 修悬空的 `BatchLedger` 引用。
- **测试夹具对齐 v2（D6）**：`tests/test_traffic_upin_gaps.py` 夹具从 `X-Slimapi-Version: 1` / `accepted_client_versions=(1,1)` 对齐到生产 v2（`2` / `(2,2)`），docstring 去除过时的 G6/questions 描述。
- **`health.py` 走 `app.state.config`（C2）**：`/slimapi/health` 的 `features.skeletonInlineOutputMaxBytes` 从模块级 `settings` 单例改为 `request.app.state.config`（与同文件其余字段一致）；生产无 wire 变化（`app.state.config is settings`），消除测试期自定义 config 覆盖不一致。
- **`?before=` 前置条件注释锚定（C4）**：messages list 路由加注释锚定「opencode cursor 为 base64url」假设（不含 `+` / 空格，故 unquote_plus 往返安全）。

## [1.1.4] - 2026-08-07 — questions 内存防线 + traffic 桶修正 + 文档纠错（未 bump `X-Slimapi-Version`，仍 2）

### Fixed

- **`GET /slimapi/questions` 加 `read_with_cap` 内存防线（加性硬化，未 bump `X-Slimapi-Version`，仍 2，2026-08-07）**：questions 路由的两处上游调用——发现调用 `GET /experimental/session?roots=true&archived=true`（`limit=10000`，全量 SessionInfo）与 per-dir fan-out `GET /question`——此前用非流式 `client.get()` + 全 buffer `response.json()`，无 body cap（v1.1.2 刚为 `/slimapi/sessions` 补上流式 `read_with_cap`，但 v1.1.0 引入的 questions 路由未享受同一硬化）。现两处均改为流式 `client.send(stream=True)` + `read_with_cap(config.max_response_bytes)` + `try/finally: response.aclose()`，与 sessions/messages/agent/command 的内存防线对齐（mirrors `routes/sessions.py`）。**行为映射**：发现调用超限→**503 `upstream_unavailable`**（total failure，无 envelope，contract §7 discovery exception——发现是内部派生调用，泄漏上游状态会误导客户端）；per-dir `/question` 超限→该 dir 计入 envelope `errors[]`（`code:"upstream_unavailable"`，isolated，不中断整体）。mid-stream `httpx.RequestError`（aread/read_with_cap 阶段）→ 同路径 503 / errors[]。上游错误状态码仍抽干 body 记 traffic + 复用连接。**加性变更，不 bump** `X-Slimapi-Version`（无 client 依赖现有全 buffer 行为；新行为是 thin 路由既有 `read_with_cap` 防线的补齐）。实现：`src/oc_slimapi/routes/questions.py`；测试：`tests/test_questions_routes.py`（发现 cap→503、per-dir cap→errors[] 两条新用例）。
- **traffic 桶表修正：`bucketize()` 加 `questions` 桶 + 手册同步（2026-08-07）**：`src/oc_slimapi/traffic.py` 的 `bucketize()` 此前无 `questions` 桶，`/slimapi/questions` 实际落入 `"other"`；而 `docs/manual/traffic-accounting.md` §3.2 文档了不存在的 `quiz` 桶（含 v2 已删的 `/permissions`）——文档↔实现漂移。现 `bucketize` 加 `questions` 桶（`/slimapi/questions` + `/slimapi/questions/**`，与 command/agent 平权），手册 §3.2 修正为 `questions` 行、删去 v2 不存在的 `projects`/`permissions`。省流口径变更（ops 可观测面，非客户端 wire 契约）；既有桶名不变。实现：`src/oc_slimapi/traffic.py` + `docs/manual/traffic-accounting.md`；测试：`tests/test_traffic_ledger.py`（`test_questions_bucket`）。
- **文档纠错：README「范围」节 + INTERFACE_MAP 错误映射（doc-fix，2026-08-07）**：(1) README「范围」节三处与实现矛盾的描述纠正——删去 v2 已删的 `since` 端点误述、token stream SSE 从"永不 gzip"改为"默认 gzip（lever2，首个 SSE gzip 例外）"、补齐 `questions`（跨目录聚合）与 `sessions/status` 加性回归端点。(2) `docs/specs/INTERFACE_MAP.md` 两处 messages 路由（`/slimapi/messages/{sid}` 与 `/full/{mid}`）的上游错误映射从 v1 残留"原状态透传 / 原状态 body 透传"改为契约 §7 的正确映射：404→404 `session_not_found`（带 `sessionID`）；其他 4xx→502 `upstream_http_N`；5xx/网络→503 `upstream_unavailable`（与 sessions 行写法一致）。纯文档纠错，无行为变更。

---

## [1.1.3] - 2026-08-07 — questions 发现机制根治（/project → /experimental/session?roots=true&archived=true），未 bump `X-Slimapi-Version`，仍 2

### Fixed

- **`GET /slimapi/questions` 发现机制彻底修复（未 bump `X-Slimapi-Version`，仍 2）**：发现源从 `GET /project` 改为 `GET /experimental/session?roots=true`。**根因（v1.1.1/v1.1.2 仍未根治）**：opencode `project.resolve()`（`packages/core/src/project.ts:110-122`）把**非 git repo** 的 workdir 归到合成 global project（`worktree="/"`），sidecar 此前显式跳过 `worktree=="/"`（CHANGELOG v1.1.1：「跳过合成 global 项目，无真实 session/question」——**该假设错误**），导致所有非-git workdir（自定义工作目录如 `opencode_wd`、`/tmp` 临时目录）以及 git worktree 子目录（`ocdroid/.slim/worktrees/wave0-*`）的 pending question **全部漏报**。实测：pending 在 `/home/mar/opencode_wd`（非-git），`/project` 列表不含该目录 → 聚合恒 `items:[]`；带 `X-Opencode-Directory: /home/mar/opencode_wd` 直查上游却能取到。**`GET /experimental/session?roots=true`** 是 opencode 的**全局顶层 session 列表**（`roots=true` ⇒ `parentID==null` only，源码 `packages/app/src/utils/server-compat.ts:147` 确证），每个 session 携带**真实 `directory` 字段**（创建该 session 的 workdir 原始路径），覆盖 git repo + 非-git目录 + git worktree 子目录（app/tui/acp/cli/SDK 全在用的 v2 正式端点，非 deprecated `/api/`）。借鉴 qq-ocbot `fetch_questions`（`src/core/opencode.py:170`）的成熟方案。**删去 `worktree=="/"` 跳过逻辑**（session.directory 恒为真实路径，无合成 global 问题）。**发现调用带 `archived=true`**——使发现集合成为**超集**（含已归档 session），消除"某 workdir 顶层 session 全部归档但实例仍存活、pending question 仍在内存"的盲区（`/question` 是内存态，与归档状态无关；最多 fan-out 到已死实例产生 isolated `errors[]`，无副作用）。`discoveryComplete` 语义从「恒 true（`/project` 无分页）」改为「页未满 `_DISCOVERY_LIMIT`(=10000) 时 true，页满降级 false」——`/experimental/session` 接受 `limit`，`roots=true` 只返顶层 session（数量 ≈ workdir 数），实际不会截断；截断时 `authoritativeDirectories` 降级为 succeeded 数组（复用 v1.1.0 既有逻辑，防 replace-all 丢弃未发现目录 pending）。`authoritativeDirectories` 其余语义不变；envelope 字段集 `{items,errors,authoritativeDirectories,discoveryComplete}` **不变**；所有错误码不变；total failure（发现失败 → 503 `upstream_unavailable`）不变。**加性硬化，不 bump** `X-Slimapi-Version`。**上游依赖**：需 opencode ≥ v1.18.x（`/experimental/session` v2 端点；server 端 query schema `public.ts:59-64` + `isNull(parent_id)` 实现 `session.ts:560,987` 已核实；app/tui/acp/cli/SDK 共用）。**已知 trade-off**：发现调用 payload 体积较 `/project` 上升（每条 session 携带完整 SessionInfo，sidecar 只用 `id`+`directory`；本地回环单次调用 + 顶层 session 数量小，可接受）。实现：`src/oc_slimapi/routes/questions.py`；测试：`tests/test_questions_routes.py`（重写发现 mock 为 `/experimental/session` + 新增非-git 盲区 / git worktree 子目录 / archived-only 盲区 / 截断降级 / `roots=true`+`archived=true` 契约 / 缺失 directory 字段跳过 / discovery 4xx→503 等用例，共 27 用例）。评审：rev-ds 9/10 APPROVE + rev-glm 8/10 APPROVE（均独立经 opencode server 源码确证方案根基）。

---

## [1.1.2] - 2026-08-06 — 错误面硬化 + sessions 内存防线 + 文档语义同步（未 bump `X-Slimapi-Version`，仍 2）

### Fixed

- **catch-all 反代上游网络异常 → 503 `upstream_unavailable`（加性硬化，未 bump `X-Slimapi-Version`，仍 2，2026-08-06）**：catch-all 反代（`proxy.py` 的 `client.send`）在 `httpx.RequestError`（connect / read / write timeout、pool failure、`RemoteProtocolError` 等连接与请求建立阶段错误）时此前逃逸为 FastAPI 默认裸 `500 Internal Server Error`，现统一映射为结构化 `503 {"code":"upstream_unavailable"}`，与 thin 路由（sessions/messages/agent/command/questions）的错误面对齐（INTERFACE_MAP §4 known gap 收口）。**加性变更，不 bump** `X-Slimapi-Version`（无 client 依赖现有裸 500；新行为是 thin 路由既有行为的补齐）。**边界**：except 仅覆盖 `send()` 调用本身；mid-stream 断开（send 已返回 StreamingResponse 之后）走 `_counted_upstream_response` 的 finally，不经过此 except。**catch-all per-request timeout 不变**（SSE=None / command=300s / 其他=30s，`proxy.py:172-177`）。**turn-fence 语义不变**：send 失败产生的 turn hole（bump-before-send 已 advance）由 ocdroid lex 容忍，无 rollback。实现：`src/oc_slimapi/proxy.py`；测试：`tests/test_proxy.py`（ConnectError / ReadTimeout → 503）。
- **`GET /slimapi/messages/{sid}/full/{mid}` 非 dict body → 503 `upstream_unavailable`（加性硬化，未 bump `X-Slimapi-Version`，仍 2，2026-08-06）**：`/full/{mid}` 端点期待单条 message dict；上游返回非 dict body（list / null / scalar）时，`strip_diagnostics_message`（skeleton.py）的 shape-robustness 守卫此前将其**原样透传**，把畸形上游 200 作为令人困惑的 200 返回客户端。现 worker 在数据接触点加 `isinstance(parsed, dict)` 守卫抛 `ValueError`，路由层 catch 扩展为 `(orjson.JSONDecodeError, ValueError)` 统一映射 `503 {"code":"upstream_unavailable"}`（畸形 body 不应透传客户端）。**加性变更，不 bump** `X-Slimapi-Version`（无 client 依赖现有 200 透传畸形 body；新行为是 thin 路由既有 JSONDecodeError 守卫的补齐）。与 messages list 非 list body 守卫同范式。实现：`src/oc_slimapi/transform.py` + `src/oc_slimapi/routes/messages.py`；测试：`tests/test_messages_routes.py`（list body / null body → 503）。
- **`GET /slimapi/sessions` body 超 `max_response_bytes` → 413 `response_too_large`（加性硬化，未 bump `X-Slimapi-Version`，仍 2，2026-08-06）**：sessions list 端点原用非流式 `upstream.get` + `response.json()` 全 buffer，无 body cap（known limitation，`sessions.py:42-44`）。现改为流式 `build_request + send(stream=True)` + `read_with_cap(config.max_response_bytes)`，超限 → 413 `response_too_large`（+ `limit` 字段），与 messages/agent/command 的 64MiB 内存防线对齐。mid-stream `httpx.ReadError`/`ReadTimeout` → 503 `upstream_unavailable`（内层 `except httpx.RequestError` 覆盖 `aread()` + `read_with_cap()`）。**加性变更，不 bump** `X-Slimapi-Version`（无 client 依赖现有全 buffer 行为）。实现：`src/oc_slimapi/routes/sessions.py`；测试：`tests/test_sessions_routes.py`（oversize → 413 / mid-stream ReadError → 503）。

---

## [1.1.1] - 2026-08-06 — questions 目录发现 bug 修复（`GET /session` → `GET /project`），未 bump `X-Slimapi-Version`，仍 `2`

### Fixed

- **`GET /slimapi/questions` 目录发现机制 bug 修复（未 bump `X-Slimapi-Version`，仍 2）**：发现调用从 `GET /session?limit=10000`（无 directory header）改为 `GET /project`。原实现误以为 `/session` 是"全局存储"（实测为 **per-Location**——按 `X-Opencode-Directory` 路由的 workdir instance，无 header 回落 `process.cwd()`，只返回 cwd workdir 的 session），导致 `workdir ≠ process.cwd()` 的所有 pending question **漏报**（实测：cwd workdir 37 条 session vs ocdroid workdir 193 条 + pending question 全不可见）。`GET /project` 返回 ProjectTable 全表（实测 27 个 workdir，跨所有 workdir，与 directory header 无关，handler 为 `db.select().from(ProjectTable).all()`），是真正的全局发现机制。字段映射：session 的 `directory` → project 的 `worktree`；跳过合成 "global" 项目（`worktree=="/"`，无真实 session/question）。**`discoveryComplete` 语义变化**：从「发现页未满（`len < limit`）时 true」改为「**恒 true**」（`/project` 无分页/截断概念，ProjectTable 一次全表返回）——故 `authoritativeDirectories` 现仅由 per-dir errors 决定（无 error → `null` 全局权威 replace-all；有 error → 成功 dir 数组 partial）。envelope 字段集 `{items, errors, authoritativeDirectories, discoveryComplete}` **不变**；所有错误码不变；total failure（发现失败 → 503 `upstream_unavailable`）不变。**已知 trade-off**：`/project` 只列出每个 project 的 worktree 根，**不**含 git worktree 子目录（如 `ocdroid/.slim/worktrees/wave0-*`）与临时目录（如 `/tmp/...`）——这些场景如有 pending question 仍会漏（次优方案 `/api/session` 旧版能覆盖但引入 `/api/` 命名空间，未采纳）。实现：`src/oc_slimapi/routes/questions.py`；测试：`tests/test_questions_routes.py`。

---

## [1.1.0] - 2026-08-05 — 批量加性端点（questions 跨目录聚合 / command / agent skeleton / sessions-status 回归），未 bump `X-Slimapi-Version`，仍 `2`

### Added

- **`GET /slimapi/questions`（加性回归，跨目录 pending question 聚合，未 bump `X-Slimapi-Version`，仍 2）**：lite-v2 曾在批量清理中删除此端点，现加回为**跨目录聚合端点**。**修复 slim-mode 冷启动回归**：pending question 在 `workdir ≠ process.cwd()` 的目录中时对客户端不可见（上游 opencode `GET /question` 是 per-Location——按 `X-Opencode-Directory` 路由的 workdir instance，无 header 回落 `process.cwd()`；sidecar 此前只透明反代、从不跨目录聚合）。语义：先 `GET /session?limit=1000`（无 directory header，session 是全局存储）发现所有 distinct directory（first-seen 保序去重），再并发（`asyncio.gather`）对每个 dir `GET /question`（带 `X-Opencode-Directory: <dir>`）合并。返回 **envelope 对象**（非裸数组，以表达 partial 失败）`{items, errors, authoritativeDirectories}`：每条 `items` entry 为上游 entry 原样（字段序 id/sessionID/questions/tool 保留）+ 追加 `directory` 字段；`errors` 为 per-dir 失败（**isolated**——单 dir 失败不中断整体；code 遵循契约 §7：网络/5xx/非 list → `upstream_unavailable`，4xx → `upstream_http_N`）；`authoritativeDirectories` null = 全成功（client **replace-all** 语义）/ 数组 = partial（仅 replace 所列 dir）。发现调用失败（网络/5xx/4xx/坏 JSON/非 list）→ 整体 **503** `{"code":"upstream_unavailable"}`（无 envelope，total failure）。无 sessions → `{items:[],errors:[],authoritativeDirectories:null}`（权威空）。无 skeleton 投影、无转换池 admission（entry 原样转发 + directory）。**客户端契约**：`authoritativeDirectories==null` → 全局权威 replace-all；为数组 → 仅覆盖所列 dir（不丢弃未覆盖 dir 的既有 pending question）。pending question 的**应答**仍走 catch-all + `X-Opencode-Directory`（§2 写路径，本端点只读）。**加性**：旧 sidecar 无此路由→catch-all 404 `thin_route_not_found`，客户端可 fallback。实现：`src/oc_slimapi/routes/questions.py`；测试：`tests/test_questions_routes.py`（聚合 / 权威空 / partial 5xx / per-dir 网络错误 / total failure / directory stamp / dedup / 忽略入站 directory / 版本门闩 / per-dir 4xx / 零 session / 非 list per-dir / gzip）。envelope 语义见 `docs/specs/v2-contract.md` §2。**硬化（同条目内）**：(1) 发现 `limit` 提升到 `10_000`（上游 `/session` 无前向 cursor，提限是唯一杠杆）；(2) **截断安全**——当发现页满（`len == limit`，可能截断）时 `authoritativeDirectories` 降级为**成功 dir 数组**（即使无 per-dir 错误），防止客户端 replace-all 丢弃未发现目录的 pending question（**数据丢失**）；`null` 仅在「无 errors 且发现完整」时给出；(3) 加性诊断字段 `discoveryComplete`（bool，页未满时 true）——客户端可忽略 if absent-aware；(4) per-dir fan-out 用模块级 `asyncio.Semaphore(16)` 限并发，避免对共享 upstream client（`max_connections=32`）排无限 in-flight；(5) gather 结果循环改 `isinstance(result, Exception)` 并显式 re-raise `asyncio.CancelledError`（不再吞取消）。`X-Slimapi-Version` 仍 `2`（未 bump；两个加性 wire 字段 + 权威规则收紧，无破坏性）。
- **`GET /slimapi/sessions/status`（加性回归，未 bump `X-Slimapi-Version`，仍 2）**：lite-v2 曾在批量清理中删除此端点，现加回性回归。语义：透传上游 opencode `GET /session/status`（返回 `Record<SessionID, {type:"busy"|"idle"|"retry"}>`）+ sidecar merge 每个条目的 flat 顶层 `turnIncarnation`/`turn`（源自 `TurnRegistry.snapshot`，与 digest SSE §3.y **同源内存**，未观测 sid → `(inc, 0)`）。`directory` 必填（v1 契约 §11.1 延续）；端点**只读、不写、不缓存**——纯同内存只读投影，不引入新状态机/新缓存/新持久化。turn_registry 未装配（lifespan 级）时两字段配对缺省（ocdroid 降级 Tier-2）。错误映射：上游 4xx → 502 `upstream_http_N`；5xx/网络/坏 JSON/非 dict body → 503 `upstream_unavailable`；缺 directory → FastAPI 422。**加性**：ocdroid 侧可恢复 v1 同名端点的既有消费逻辑（回归成本低）。实现：`src/oc_slimapi/routes/sessions.py`；测试：`tests/test_sessions_routes.py`（idle/busy/retry turn merge、并发 bump live read、坏 shape、endpoint-registered 非 404、directory 必填/校验、上游错误、无 registry 降级）。
- **`GET /slimapi/sessions/status` 的 `directory` 改为可选（加性，未 bump `X-Slimapi-Version`，仍 2）**：上游 `GET /session/status` 的 `directory` 是 **no-op**（handler 零参数，`statusSvc.list()` 返全量 `Map<SID,Info>`；源码确证 `handlers/session.ts:77-79` + `session/status.ts:35-37`），故本端点恒返**全局状态图**。`directory` 由必填放宽为可选：不传→200 全局 map + 不转发 directory（query/header 均不发）；传→normalize+透传（上游仍 no-op，纯兼容）。**动机**：消除 ocdroid 按目录冗余 fan-out（每次返回同一全局 map）；ocdroid 应改单次全局调用 + 客户端侧过滤。调研：`docs/ocmar/specs/2026-08-05-s4-batch-status-research.md`。原 v1 §11.1 必填约束已放宽，加性兼容（旧调用方传 directory 仍正常）。
- **`GET /slimapi/command`（加性 catalog skeleton，未 bump `X-Slimapi-Version`，仍 2）**：透传上游 opencode `GET /command`，白名单投影每项保留 `{name,description,agent,hints}`，丢弃 `template`(~97.7% 字节)/`source`/`model`/`subtask`。实测 raw 省 ~97.6%（292KB→7.25KB；gzip 3.18KB）。`directory` 可选，仅作 `X-Opencode-Directory` header 转发（command catalog 全局，上游忽略）。转换池 admission 先于 upstream GET；流式读 + `read_with_cap`（超 `max_response_bytes`→413 `response_too_large`）；gzip 在 worker 内；非 list body→503 `upstream_unavailable`。无 `hasFull`/`omitted`（catalog 无 per-entry expand 端点）。错误映射：上游 4xx→502 `upstream_http_N`；5xx/网络/坏 JSON→503 `upstream_unavailable`；转换槽满→503 `transform_busy`+`Retry-After:2`；参数错误 422。`agent` 为可选字段（少数 command 有，常 `null`）原样保留。**加性**：旧 sidecar 无此路由→catch-all 404 `thin_route_not_found`，ocdroid fallback 到透传 `GET /command`。实现：`src/oc_slimapi/routes/command.py`；投影：`skeleton_commands()`；测试：`tests/test_command_routes.py`、`tests/test_skeleton.py`。**注**：`hints` 为开放型 JSON，当前原样保留未做单项/总量大小 cap；省流比例为实测值（opencode v1.18.13 数据，hints 实测 14B），依赖当前数据形状而非实现保证；per-entry/aggregate `hints` cap 列为后续 follow-up（见评估报告 §5.S1）。
- **`GET /slimapi/agent`（加性 catalog skeleton，未 bump `X-Slimapi-Version`，仍 2）**：透传上游 opencode `GET /agent`，白名单投影每项保留 `{name,description,mode,hidden,native}`，丢弃 `prompt`(~34.7%)/`permission`(~61.2%，`Permission.Ruleset` 列表——**非** pending permission card，无 UI 消费者)/`topP`/`temperature`/`color`/`variant`/`options`/`steps`/`model`。实测 raw 省 ~95.8%（250KB→10.7KB；gzip 3.57KB——注意 gzip 对 `permission` 重复 rule 串有消解，验收须用 gzip/downOut 口径）。`directory` 可选，仅作 `X-Opencode-Directory` header 转发（agent catalog 全局）。转换池 admission + 流式 `read_with_cap` + worker 内 gzip（同 command）。`native`/`hidden` 可选（缺则 skeleton 稀疏，不补键）。错误映射同 command。**加性**：旧 sidecar 无此路由→catch-all 404 `thin_route_not_found`，ocdroid fallback 到透传 `GET /agent`。实现：`src/oc_slimapi/routes/agent.py`；投影：`skeleton_agents()`；测试：`tests/test_agent_routes.py`、`tests/test_skeleton.py`。

### Changed

- **turn token fence scope 简化为「仅 sid」**：turn 计数器分桶 key 由 `(serverGroupFp, sid)` 改为 `sid`（单 sidecar + 单 opencode 后端下 sid 已全局唯一）。移除 `X-Ocdroid-Server-Group-Fp` 输入头依赖（sidecar 不再读取该 header；客户端若仍发送会被忽略）；`register_scope`/`_sid_scope` 移除；`snapshot(sid)` 恒返回 `(incarnation, turn)`（未观测 sid → `(inc,0)`），故 digest 的 `turnIncarnation`/`turn` 现在对所有 `session.status` 事件恒输出（只要 turn_registry 装配），不再是 header-gated。**修正多设备共享 session 的 liveness bug**：原 `register_scope` 在每个带身份头的 session 请求上「最后写入者覆盖」，跨设备续看同一会话（哪怕只读 GET）会翻转 scope、破坏 turn 单调性，导致 ocdroid 误判后续 digest 为 stale 而 DROP（session UI 冻结）；sid-only 让所有设备对同一 sid 共享同一单调计数器，读请求不再翻转 scope。**未 bump** `X-Slimapi-Version`（digest 字段集不变，从「有时输出」变「恒输出」，加性兼容）。

### Removed

- **access log 撤回 `serverGroupFp` 字段**（未发布的加性改动，回滚）：该字段曾在 2026-07-31 作为未提交工作树改动短暂上线（经服务重启），但从未经 `release.sh` 正式发布。现 scope 改为仅 sid，该字段无数据来源；设备归属已有 `clientId` 字段。

---

## [1.0.1] - 2026-07-31 — turn token fence（服务端因果标识，加性 wire，未 bump `X-Slimapi-Version`，仍 `2`）

> turn token 强 fence 契约：在转发的 `session.digest` SSE 事件里附加 `turnIncarnation`(int) + `turn`(int) 两个**可选** flat 顶层字段，供 ocdroid 做因果 fence（丢弃来自旧 incarnation / 旧 turn 的过期 digest）。ocdroid 侧解析已就绪并 merged。**全加性 / 向后兼容，未 bump** `X-Slimapi-Version`。

### Added

- **`session.digest` 新增可选 flat 顶层字段 `turnIncarnation` / `turn`（加性，配对出现/缺失）**：两个字段位于 digest payload 的 **flat 顶层**（与 `sessionID`/`status`/`archived`/`deleted`/`lastError` 同层），**不**嵌套进子 `properties` dict（契约 §3.3 的嵌套示意图是 opencode 上游帧形状的思维投射；slimapi 的 `session.digest` 是 event-typed 帧，ocdroid 把整个 data 对象当作 `properties`，故 flat 顶层 = ocdroid 可读）。配对规则：两者都非 None → 同时输出；任一为 None → **两者都不输出**（配对缺失，ocdroid 降级）。字段类型为 JSON integer ≥0（64-bit 范围）。`updatedAt` 排序 tie-break 不受影响（`(updatedAt, messageID)` 二元组仍是 digest 排序键）。
- **header-gated scope 身份（O1）**：sidecar 仅当请求带 `X-Ocdroid-Server-Group-Fp` header 时才维护 turn 状态并 stamp digest。header 缺失 → 完全不 stamp（两字段缺省，安全降级）。该 header 经 catch-all 反代自动透传上游（不在 hop-by-hop / client-ident 剥离集内），无需改反代。ocdroid 通过该 header 透传 scope 身份。（**2026-08-01 起 scope 简化为仅 sid，header-gate 移除，详见 [Unreleased]**）
- **`turnIncarnation`（incarnation 策略 A，进程生命周期 epoch）**：启动时 `IncarnationStore` 从 StateDirectory（复用 `OC_SLIMAPI_ACCESS_LOG_DIR`）下的 `incarnation` 文件 read persisted → `+1` → write 回 → 返回新值（单进程单事件循环，无文件锁）。文件不存在（首次启动）= `persisted_last=0` → inc=1；损坏/不可写 → best-effort 兜底返回 1（warn 不 crash lifespan，参考 traffic_snapshot 容错风格）。此后进程生命周期内恒定。
- **`turn`（per-`(serverGroupFp, sid)` 单调计数，S2 send-before-bump commit point）**：catch-all 反代在构造 upstream request 之后、`await client.send()` **之前**，对两类 forward（`POST /session/{sid}/prompt` 与 `POST /session/{sid}/abort`，契约 §4.1）bump turn（`turn = prev + 1`，单调不减）。连接级失败（`send()` 抛异常）→ 产生 **hole**（turn 已 bump 但无 upstream 工作，不回退/decrement）——这是对契约 §4.2「不 increment」的**已批准放宽**；ocdroid lex 比较天然处理 hole，正确性不破。对其它带 header + sid 的 session 请求（如 `GET /session/{sid}/message`）仅注册 scope（`register_scope`），不 bump，最大化 scope 已知概率让后续 digest stamp 命中。turn registry **不持久化**（restart 归零，incarnation bump 兜底）。
- **ingest-time snapshot stamp（S9，契约 §7.4 / V10）**：`GlobalHub.publish()` 在 `session.status` 事件 ingest 时（而非 flush 时）把当前 `(incarnation, turn)` 快照 stamp 到 `DigestFields` entry 上。entry 存 Python int（值拷贝），故 ingest 后、flush 前若有新 forward bump turn，已 stamp 的 entry **不受影响**（冻结于 ingest 时刻值）。新一次 ingest stamp 当前新值。

### Notes

- **单实例语义（O3）**：无 instanceFp；scope key = `(serverGroupFp, sid)`。单进程 / 单 asyncio 事件循环，`TurnRegistry` 所有方法为同步纯 dict 操作，无需锁（契约 §7.2 单调可见性）。
- **不 bump `X-Slimapi-Version`**（仍 `2` / `ACCEPTED_CLIENT_VERSIONS=(2,2)`）：纯加性可选字段 + 可选输入 header，非破坏性协议变更。
- **未新增 `/slimapi` 路由**；`scripts/check_routes_doc.py` 仍一致（8 条）。无新增配置项（复用 `OC_SLIMAPI_ACCESS_LOG_DIR` 作为 incarnation 文件 state dir）。
- 受影响实现文件：`src/oc_slimapi/turn_registry.py`（新增）、`src/oc_slimapi/sse/hub_types.py`（DigestFields）、`src/oc_slimapi/sse/global_hub.py`（publish stamp + setter）、`src/oc_slimapi/proxy.py`（commit point + scope 注册 + path 辅助）、`src/oc_slimapi/sse/registry.py`（HubRegistry 转发）、`src/oc_slimapi/app.py`（lifespan 装配）。ocdroid 解析见 `SessionSyncCoordinator.kt:1021-1022`（`props.turnIncarnation` / `props.turn`，props = slimapi flat root）。**wire 契约**：`docs/specs/v2-contract.md` §3.y（本仓库权威）；完整因果语义 / 术语 / 不变量见跨项目 SSOT `ocdroid/docs/2026-07-31-oc-slimapi-turn-token-contract.md`。

---

## [1.0.0] - 2026-07-29 — lite-v2 major cleanup

### 流量日志持久化 + 客户端标识（加性，未 bump `X-Slimapi-Version`，仍 `2`）

> 评审权威：`.ocmar/workflows/traffic-log-persistence/lanes/design/review-task-2-orchestrator-20260729071740.md`。修复 access log 在 systemd `ProtectSystem=strict` 下不可写的部署 bug + 加性客户端标识 + 内存账本持久化快照。**全部加性 / 运维面，不影响既有客户端 wire 行为**。

#### Added

- **access log 新增 `client` / `clientVer` / `clientId` 字段（加性，缺省 `null`，向后兼容）**：三个字段均为可选；未发送客户端标识头时整字段为 `null`，旧解析逻辑容忍。
- **新增可选输入头 `X-Client-Name` / `X-Client-Version` / `X-Client-Id`（加性）**：客户端可发送这三个 request header 标识来源 app / 版本 / 设备。sidecar 读取后落 access log；**不透传给 opencode**（catch-all 显式剥离）。校验：空/空白→忽略；UTF-8 字节 > 128→忽略（拒绝不截断）；含控制字符（`<0x20`/`0x7f`）→忽略；重复同名→取首个有效值。设备 id 默认 SHA-256 截断 16 hex 字符 hash（`OC_SLIMAPI_CLIENT_ID_SALT` 设置时升级 HMAC-SHA256）。详见 `docs/specs/v2-contract.md` §7。
- **内存账本周期快照 `traffic-snapshot-YYYY-MM-DD.jsonl`（加性 ops 面）**：`TrafficSnapshotter` 后台周期（默认 300s）将内存 ledger 的 cumulative 视角（含 SSE 真实成本 `upIn`/`downOut`）写入 JSONL 文件，**按天切分** `traffic-snapshot-YYYY-MM-DD.jsonl`（命名与 access log `<stem>-YYYY-MM-DD.jsonl` 统一；`OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` 为 stem）。每帧含 `ts`/`bootTs`（进程启动固定时间戳）/`runId`（进程内 16-hex）/`uptimeS`（`time.monotonic` 差，抗 NTP 回拨）/`pid`/`enabled`/`buckets`/`totals`/`ratios`。跨进程（重启）靠 `bootTs`/`runId`/`uptimeS` 分段，分析侧算 delta。shutdown 写终态帧。snapshot **不经 access log 的 compress/prune 维护**（不自动压缩/清理）。best-effort：**首帧写入失败 → inactive**（不创建后台 task、不周期重试，需重启恢复），不挂 lifespan。详见 `docs/manual/traffic-accounting.md`。

#### Changed

- **access log 切分策略：`RotatingFileHandler`（size 10MiB×5）→ 按天切分 `access-YYYY-MM-DD.jsonl`**：文件名含日期（`date.today().isoformat()`）。启动时将**早于今天的**（`< today`）未压缩 `.jsonl` 原子压缩为独立 `.gz`（`.gz.tmp` → rename，源删失败不回滚），并迁移**当前 `access_log_dir` 内**的无日期文件（`access.jsonl`/`access.jsonl.N`）为 `access-legacy-{mtimeYYYYMMDD}-{N}.jsonl.gz`（**不跨目录迁移**；生产从旧相对目录升到 `StateDirectory` 时旧位置历史日志需运维手动移动）。后台 maintenance loop（默认 1h 周期）执行 compress + prune；`access-legacy-*.jsonl.gz` **不受 retain 自动清理**（prune 严格匹配 `access-YYYY-MM-DD.jsonl(.gz)`），永久保留、由运维清理。**ops 面，非 wire 协议破坏**。
- **旧 env 标 deprecated（保留兼容，不删字段）**：`OC_SLIMAPI_ACCESS_LOG_PATH`（若设非默认值 `logs/access.jsonl`，取其 parent dir 兜底）、`OC_SLIMAPI_ACCESS_LOG_MAX_BYTES`、`OC_SLIMAPI_ACCESS_LOG_BACKUPS`（后两者 unused since daily rotation，validate 校验保留无害）。

#### 新增配置项

| env | 默认 | 作用 |
|---|---|---|
| `OC_SLIMAPI_ACCESS_LOG_DIR` | `logs` | access log 目录（按天文件落在其下）；生产 systemd 覆盖为 `%S/oc-slimapi/logs` |
| `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | `1` | 启动时压缩早于今天（`< today`）的 `.jsonl` → `.gz` |
| `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0` | prune 早于 N 天的 `access-YYYY-MM-DD.jsonl(.gz)`；`0`=不删。**不含** `access-legacy-*.jsonl.gz` |
| `OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S` | `3600` | 后台 compress+prune 周期（≥60） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `1` | 内存账本快照总开关 |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S` | `300` | 快照周期（≥1） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `logs/traffic-snapshot.jsonl` | 快照文件名 stem（按天生成 `<stem>-YYYY-MM-DD.jsonl`）；生产 systemd 覆盖为 `%S/oc-slimapi/logs/traffic-snapshot.jsonl` |
| `OC_SLIMAPI_CLIENT_ID_HASH` | `1` | 设备 id hash 开关（fail-closed 默认开） |
| `OC_SLIMAPI_CLIENT_ID_SALT` | `None` | HMAC salt（非空时升级 sha256→hmac_sha256） |

#### 部署（systemd）

- **unit 加 `StateDirectory=oc-slimapi`**：user service → systemd 自动建 `~/.local/state/oc-slimapi`，在 `ProtectSystem=strict` + `ProtectHome=read-only` 下允许写入。env 覆盖 access log 目录与 snapshot 路径到 `%S/oc-slimapi/logs`。修复此前 access log 在生产 sandbox 下不可写的 bug。代码默认仍为相对 `logs/`（本地开发 cwd 可写）。

#### 受影响文档 / 路由

- `docs/specs/v2-contract.md` §7（access log 文件发现规则 + client header）、§12（流量查询入口更新）。
- `docs/specs/CLIENT_CHANGES.md`「客户端标识头（可选，加性）」节。
- `docs/specs/INTERFACE_MAP.md` §0（client header 约定 + access log 文件名规则）。
- `docs/operations.md` §5（日志策略修正：access log 与 snapshot 落 StateDirectory）。
- `docs/manual/traffic-accounting.md`（snapshot 章节 + access log 更新 + 配置表）。
- `deploy/oc-slimapi.service`（StateDirectory + env 覆盖）。
- 未新增 `/slimapi` 路由；`scripts/check_routes_doc.py` 仍一致。**`X-Slimapi-Version` 不 bump**（加性输入 + ops 面文件名，非 wire 协议破坏）。

---

### Breaking (wire behavior)
- Wire version bumped: `X-Slimapi-Version: 2` required (ACCEPTED_CLIENT_VERSIONS = (2,2))
- Deleted 10+ endpoints (all return 404): projects, questions, permissions, since,
  session children/status, batch expand (G6), and all POST q/p reply/reject
- `/slimapi/messages/{sid}/full/{mid}`: removed 304/ETag/X-Message-Event-Seq,
  removed ?known.* params, always returns 200
- `/slimapi/messages/{sid}`: ?mode=full silently ignored, always skeleton;
  list now sorted by time.created ascending
- Digest fields: removed `childrenVersion`, `contentRevisions`;
  `updatedAt` now sidecar wall-clock (was upstream info.time)
- Removed headers: X-Discovery-Directories, X-Discovery-Ready
- Metrics: `batch` key always null (BatchLedger removed)

### Removed (internal, no wire impact)
- Deleted source: routes/questions.py, routes/sessions_children.py, tokens.py,
  discovery.py, children_cache.py, observability.py
- Deleted config: route_secret, max_since_pages, 8 opt_a_* fields
- Deleted SSE: server.reconfigured frame, notify_reconfigured
- Deleted transform: project_and_pack, project_messages_and_pack, single=False branches
- Stage B part-level tracking removed (_part_state, fingerprint, contentRevisions)

### Documentation (contract clarification, no wire change — implementation already correct)
- `docs/specs/v2-contract.md` §3: enumerated the complete set of 6 q/p blocking-signal frame names (`question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.asked`/`permission.v2.resolved`) carried verbatim as the `type` field of the data payload; clients must handle both legacy and v2 namespaced forms (the slash shorthand was ambiguous).
- `docs/specs/v2-contract.md` §11.1: `GET /slimapi/sessions/{sid}/stream` explicit-`directory` row corrected to **no-op** (consistent with §3.x.1 — accumulator keys on sessionID); the old "normalize 后过滤进程级 GlobalBus 事件" wording contradicted §3.x.1.
- `docs/specs/v2-contract.md` §4: added concrete pending-q/p catch-all pull examples (`GET /session/{sid}/question` + `GET /session/{sid}/permission` via `X-Opencode-Directory`), noting catch-all passes upstream legacy endpoints through verbatim (slimapi does not parse/aggregate/remember q/p state).

---

## [0.12.0] - 2026-07-27

> 开发中、尚未打 tag 的变更写在这里；`release.sh` 发版时把本节内容折叠进新版本标题下。

### Stage B v0.6 — per_part_revision 独立递增 + message.removed tombstone 重放队列（加性 wire，未 bump `X-Slimapi-Version`，仍 `1`）

> 双方 v0.6 冻结（2026-07-27）：rev-ogpt 三审 7.7/10 NEEDS-FIX（新 MAJOR per_part_rev 回退 + MAJOR 4 双方冻结方案 C）→ delta spec `docs/ocmar/specs/2026-07-27-stage-b-impl-spec-v0.6-delta.md`。叠加 v0.5 实现，冲突处 v0.6 为准。

#### Changed

- **per_part_revision 独立递增 + per-frame revision（新 MAJOR 修复）**：`TokenStreamHub.on_part_updated()` 忽略 `GlobalHub.publish` 传入的 `part_revision` 参数，自己维护 `_part_revisions[key]`。此前 GlobalHub 的 `_part_state` 在 LRU cap 淘汰 message entry 后，同一 PartKey 再 `message.part.updated` 会把 per_part_rev 当成 0 → 覆盖 token hub 中更高的 revision → client strict `>` 漏帧。修复后 token hub 独立维护 revision，且每个 token 帧（snapshot/delta/done/truncated）在发射时获得**唯一递增**的 `partEventRevision`（per-frame revision）。客户端 strict `>` 去重正确——每帧 revision 不同，不会误丢。**wire `partEventRevision` 字段名不变**。
- **`message.removed` token stream tombstone + 重放队列（MAJOR 4 方案 C 增强版）**：`GlobalHub.publish` 收到 `message.removed`（flat props `{sessionID, messageID}`）时，除既有 `_part_state`/pending 清理外，路由到 `TokenStreamHub.on_message_removed(sid, mid)`。Token hub 向该 session 的当前 token subscribers 发送 SSE event `message.removed`（payload `{sessionID, messageID}`），并记入全局 FIFO 重放队列（cap 1000，TTL 24h）。`attach_subscriber` 握手时序改为：`server.connected` → 该 session 未过期 tombstones 按时间重放 → `flush_sid` → live snapshot → 入 fanout。`resync_all` / `on_upstream_reconnect` **不清**重放队列（队列专为 reconnect 服务，仍受 cap/TTL 限制）。新增 wire 帧 `message.removed`（event name `message.removed`，payload `{sessionID, messageID}`）。
- **新 503 错误码 `sse_token_handshake_overflow`**：token stream handshake buffer overflow（handshake deque items/bytes 超 `TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB`）。返回值同 `sse_token_subscriber_limit` 锁 503 但 payload 多 `bufferBytes` 字段，触发条件不同（握手帧集过大 vs 订阅者容量超限）。**加性 wire**，不 bump `X-Slimapi-Version`。

#### 受影响 CLIENT_CHANGES / 路由↔文档

- `docs/specs/v1-contract.md` §3.x token stream 帧加 `message.removed` + 握手时序加 tombstone 重放；§3.S 生命周期补充；§7 加 `sse_token_handshake_overflow`；§6.x 加 handshake overflow 行为。
- `docs/specs/INTERFACE_MAP.md` `/slimapi/events` 行 + token stream 行（message.removed 路由 + 重放队列 + handshake overflow）。
- `docs/specs/CLIENT_CHANGES.md` Stage B v0.6 段（client 收 `message.removed` 帧后清该 message 本地 streamOwned + 重拉 `/since`）+ 新增 `sse_token_handshake_overflow` 错误码说明。
- 未新增 `/slimapi` 路由；`scripts/check_routes_doc.py` 仍一致。

### Stage B v0.5 — messageEventSeq per-session 全局单调 + removal 自愈 + header 稳定性（加性 wire，未 bump `X-Slimapi-Version`，仍 `1`）

> 双方 v0.5 重新冻结（2026-07-27）：rev-ogpt 二审 6.5/10 NEEDS-FIX（新 CRITICAL 1 + MAJOR 2/3）→ delta spec `docs/ocmar/specs/2026-07-27-stage-b-impl-spec-v0.5-delta.md`。叠加 v0.4 实现，冲突处 v0.5 为准。**MAJOR 4（message.removed wire tombstone）待 ocdroid council，v0.5 不含**——sidecar 仍按 v0.4 清缓存（不回退）。

#### Changed

- **`messageEventSeq` 改 per-session 全局单调序号（CRITICAL 1 修复）**：v0.4 的 `_bump_message_seq` 为 per-message 计数器，LRU 淘汰 message 后重触及 → seq 归 1，破坏单调（client 持 seq=10 → `1>10` false 漏检）+ ABA 错误 304（重建三元组等于旧 known）。v0.5 改用 per-session 全局 counter（`GlobalHub._session_event_seq: dict[str, int]`）：每次该 session 任意 message 的 `message.part.updated` / `message.part.removed` → 全局 counter +1；该 message 的 seq = 当前全局值（赋给 `_part_state[sid][mid]["seq"]` + `content_revisions[mid]`）。淘汰 message 不重置全局 counter；重触及 seq = 当前全局值（远大于旧，单调）。`resync_all()` 清 `_session_event_seq`（reconnect = 新 epoch，client 不信任 → R1）。`session.deleted` 也清该 sid 的全局 counter。**wire 形态不变**：`contentRevisions` value 仍 int（语义改全局单调）。per-part revision（token 帧去重）独立计数器不受影响。
- **`message.part.removed` of unknown message 也产生 digest（MAJOR 2 修复）**：v0.4 仅当 `_part_state[sid][mid]` 存在时推进；cap 淘汰后 message 未知 → removal 静默丢弃 → client 永久保留已删 part。v0.5 即使 message 不存在（被淘汰/未知）也 bump 全局 seq + `entry.content_revisions[mid] = seq`（client strict `>` 检测 → R1 → `/full/{mid}?known=` 无缓存 fingerprint → 200，client 拿最新 parts 自愈）。msg_entry 存在时仍 pop partID（v0.4 逻辑）+ bump seq。

#### Fixed

- **`X-Message-Event-Seq` body 前后稳定性（MAJOR 3 修复）**：v0.4 header 在 upstream await 前取样；body 拉取/转换期间 part event → header seq 不对应返回 body。v0.5 `/full/{mid}` handler 改为 body 前 `seq_pre = fp[2] if (fp:=hub.get_part_fingerprint(sid,mid)) else 0`；body 后（transform 完成后、Response 构造前）`seq_post` 同样取样；`seq_pre == seq_post` → 发 `seq_post`（可信）；不一致 → 发 `0`（client 视为无 baseline → R1）。304 短路路径不变。两路（full + skeleton）均实现。

#### 不含

- **MAJOR 4（message.removed wire tombstone）**：待 ocdroid council 确认设计（/since 自愈 vs digest `removedMessages` 字段）。v0.5 保留 v0.4 的 message.removed sidecar cache 清理（不回退），仅 wire tombstone 待定。

#### 受影响 CLIENT_CHANGES / 路由↔文档

- `docs/specs/v1-contract.md` §3.S（messageEventSeq 改 per-session 全局；removal 自愈；header 稳定性）。
- `docs/specs/INTERFACE_MAP.md` `/full/{mid}` + `/slimapi/events` 行（X-Message-Event-Seq 稳定性；unknown removal 自愈）。
- `docs/specs/CLIENT_CHANGES.md` Stage B v0.5 段（client 严格 `>` 检测 messageEventSeq + 视 `0` 为不可信）。
- 未新增 `/slimapi` 路由；`scripts/check_routes_doc.py` 仍 19 条一致。

### Stage B v0.4 — partEventRevision → messageEventSeq + /full 304 fingerprint + removal（加性 wire，未 bump `X-Slimapi-Version`，仍 `1`）

> 双方 v0.4 重新冻结（2026-07-27）：rev-ogpt 评审 5.0/10 NEEDS-FIX（2 CRITICAL + MAJOR 5）→ delta spec `docs/ocmar/specs/2026-07-27-stage-b-impl-spec-v0.4-delta.md`。叠加 fix-1 v1 实现，冲突处以 v0.4 为准。

#### Added

- **digest `contentRevisions`（CRITICAL 2 修复）**：`session.digest` 加 optional 字段 `contentRevisions: {messageID → messageEventSeq_int}`。`messageEventSeq` 是 message-level 严格单调事件序号（message 首次 part 事件触及 → 1；每 `message.part.updated` / `message.part.removed` +1），替代 v1 的 `max(per-part revision)`（多 part 下加新 part 会把 max 拉低，非单调——CRITICAL 2）。client 用 strict `>` 检测 message 内容变更。空 map → 字段省略（向后兼容：无 part 事件的 digest wire 形态不变）。
- **`/full/{mid}?known.maxPartId=&known.partCount=&known.messageEventSeq=` 304 短路（CRITICAL 1 修复）**：三者齐全 + sidecar `_part_state` 缓存命中 + `(maxPartId, partCount, messageEventSeq)` 三元组全一致 → `304 Not Modified`（空 body，`Cache-Control: no-store`）。任一不一致 / 部分参数 / 无缓存 → 正常 200 full body。v1 的 `maxPartId+partCount` 二元组**不足以** 304（同 part 文本追加不改此二元组但内容已变；CRITICAL 1）。
- **`X-Message-Event-Seq: <int>` 响应头（§D）**：`/full/{mid}` 200 响应必带；值=该 message 的 messageEventSeq；无缓存（冷启动 / reconnect / session.deleted / message.removed）→ `0`（client 视为"无信息"→ R1）。304 路径不发该头。
- **`message.part.removed` / `message.removed` hub 路由（MAJOR 5 修复）**：上游 `message.part.removed`（flat props）→ `_part_state` parts.pop + seq+1 + digest `contentRevisions` 更新（通知 client partCount 变）；`message.removed`（flat props）→ `_part_state` 删该 message + 清 pending contentRevisions 该条目（digest 不带已删 message 的 contentRevision）。opencode v1.18.4 schema session.ts:604-628 确认。

#### Changed

- **digest wire 字段名**：v1 `partEventRevisions`（value=`max(per-part rev)`）→ v0.4 `contentRevisions`（value=`messageEventSeq`）。**字段重命名 + 语义变更**——client 必须从 `partEventRevisions` 切换到 `contentRevisions` 并改用 strict `>` 比较。ocdroid 对接以本字段为准。
- **token 帧的 `partEventRevision` 字段语义**：保持 per-part revision（token 帧去重用），与 digest `contentRevisions`（messageEventSeq）**独立递增**——同名 wire 字段，不同 scope（per-part vs per-message）。client token stream 渲染仍按 partID 索引。

#### Fixed

- **truncated 帧顺序（MAJOR 4 修复）**：`_truncate_part_for_all` / oversized handshake 路径先捕获 per_part_rev 再 `drop_part`（后者清缓存），保证 truncated 帧携带 `partEventRevision`（v1 此处 silently dropped it）。
- **reconnect pending 泄漏（MAJOR 3 修复）**：`resync_all()` 除清 `_part_state`，还清所有 pending entry 的 `contentRevisions`——防 `message.part.updated` 进 debounce 后 reconnect → client 收 resync 后再收旧 epoch 的 contentRevisions。
- **`_part_state` LRU cap 500 message/session（MAJOR 5）**：超限淘汰最旧 message（FIFO ≈ LRU for creation-order traffic），防长会话无限增长。

#### 受影响 CLIENT_CHANGES / 路由↔文档

- `docs/specs/v1-contract.md` §3 digest payload 加 `contentRevisions?` + §3.S（R2 304 + X-Message-Event-Seq 详细）；§2 `/full/{mid}` 行加 `known.*` 三参数 + 304 + X-Message-Event-Seq 标注。
- `docs/specs/INTERFACE_MAP.md` `/full/{mid}` 行 + `/slimapi/events` 行（digest 加 `contentRevisions?`、事件路由修订）。
- `docs/specs/CLIENT_CHANGES.md` Stage B v0.4 wire 变更小节。
- 未新增 `/slimapi` 路由（query 参数 + 头不算新路由）；`scripts/check_routes_doc.py` 仍 19 条一致。

---

（其它历史变更折叠在下方版本标题下。）

---

## [0.11.0] - 2026-07-26

> Slim 会话消息可靠性联合计划 **阶段 A 第1类**（oc-slimapi 单方，无需 ocdroid 配合即可受益）：`/since/{ts}` 页序/早停修复（按 opencode v1.18.4 `MessageV2.page` 页内 oldest-first 整页过滤）、加性响应头 `X-Since-Complete`、SSE per-sid immediate flush、`archived` bool 防护、`/full` 非法/空 JSON 与中途读取失败统一 503 `upstream_unavailable`、full strip in-place（去多余 deepcopy）。**全加性/修复性 wire，`X-Slimapi-Version` 仍 `1`，不 bump**。第2类（消息内容 watermark revision/partCount/generation、token idle/resync 清态、SSE 开/关统一 reconcile 三分法）**未合入本仓 wire**，待双方阶段 B 联调；本轮亦未实施阶段 B。计划：`docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`、`docs/ocmar/plans/2026-07-26-slim-state-message-repair.md`；评审：`docs/ocmar/reviews/2026-07-26-rev-gpt-class1-slim-repair.md`（有条件通过→收尾项已处理）。

### Added

- **`X-Since-Complete: true|false`（`GET /slimapi/messages/{sid}/since/{ts}`，加性响应头）**：标明本次增量扫描是否完整结束（`true`）或因 `max_since_pages` 截断且可能仍有匹配页（`false`）。旧客户端可忽略。**不 bump** `X-Slimapi-Version`（仍 `1`）。设计/对接：`docs/specs/design-v2.md` §1.5、`docs/specs/CLIENT_CHANGES.md`；计划与评审：`docs/ocmar/plans/2026-07-26-slim-state-message-repair.md`、`docs/ocmar/reports/2026-07-26-slim-state-sse-review.md`。

### Fixed

- **`/since/{ts}` 页序与早停（第1类）**：纠正「upstream 页内 newest→oldest、首项 watermark `< ts` 即停」的错误假定。opencode `MessageV2.page` 为 **页内 oldest-first**（DB newest-first 取窗后 reverse）；sidecar **整页过滤**匹配项，并用**页内最旧 watermark** 判断是否值得再扫更旧页。修复升序页上增量常空、丢消息的问题。**加性/修复性 wire，不 bump** `X-Slimapi-Version`。
- **`/full` 上游非法/空 JSON 与读取中断统一 503 `upstream_unavailable`**：`GET /slimapi/messages/{sid}?mode=full` 与 `/full/{mid}` 的 `read_with_cap` 异步 body 迭代过程中发生的 `httpx.RequestError`（上游响应已返回后流中断，非 `send` 阶段）现统一映射为 503 `upstream_unavailable`（与空/坏 JSON 同 code），不再逃逸为 FastAPI 默认 500。**加性/修复性 wire，不 bump** `X-Slimapi-Version`。
- **SSE per-sid flush（G1-A `session.error` / busy clear）**：`hub.publish()` 对 `session.error`（有 sid）和 `status=busy`（清 sticky lastError）改为调用 `flush_sid(sid)` 仅排空该 sid 的 pending digest，其余 sid 仍留在 250ms debounce 窗口内，避免全局 flush 把未到期 digest 提前吐给订阅者。**纯服务端行为，不 bump** `X-Slimapi-Version`。
- **archived bool 防护**：`session.updated` 的 `info.time.archived` 接受整数 epoch-ms（含 0），但显式拒绝 `bool`（`bool` 是 `int` 子类，`True` 不再被错当 epoch-ms 1 写入 digest）。**防御性修复，不 bump** `X-Slimapi-Version`。

### Notes（第2类，未合入本仓 wire）

- 消息内容变更 watermark（revision / partCount / generation）、token idle/resync 清态安全语义、SSE 开/关统一 reconcile 三分法仍需 **ocdroid** 配合；本仓**未**改协议 revision 字段。handoff：`docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md`。

---

## [0.10.0] - 2026-07-24

> `/full` 路径服务端剥离 LSP `state.metadata.diagnostics`（ocdroid `Message.kt#parsePartState` 反序列化时本就无条件删除、从不消费）——纯下行流量 + parse/heap 节省，客户端功能零影响。`/full` 由「verbatim 流式透传」改为「缓冲 → 解析 → 剥 diagnostics → 重序列化（经 `TransformPool` admission，与 skeleton 同路径；admission 在 upstream GET 之前获取）」。**全加性 wire 行为，`X-Slimapi-Version` 仍 `1`，不 bump**；ocdroid 对接：list/single/batch 的 `mode=full` 现可能 503 `transform_busy`（池饱和，与 skeleton 一致）、`/full` 响应头改由 sidecar 拥有、list-full 仍透传上游 `Link` 头。

### Added

- **`/full` 路径剥离 LSP `diagnostics`**：所有 `mode=full` 消息路径（`GET /slimapi/messages/{sid}?mode=full`、`GET /slimapi/messages/{sid}/full/{mid}`、`GET /slimapi/messages/{sid}/full?ids=`）现从每个 part 剥去 `state.metadata.diagnostics`（opencode `edit`/`write` 工具写入、ocdroid `Message.kt#parsePartState` 反序列化时无条件删除、从不消费的 LSP 诊断图）；其余字段（output/text/files/metadata 其它键等）原样保留，`/full`「完整 part」语义不变。`/full` 由「verbatim 流式透传」改为「缓冲 → 解析 → 剥 diagnostics → 重序列化（经 `TransformPool` admission，与 skeleton 同路径；admission 在 upstream GET 之前获取）」。**加性裁剪，wire 向后兼容，不 bump `X-Slimapi-Version`**。客户端影响：(1) list/single 的 `mode=full` 现可能返回 **503 `transform_busy`**（池饱和时，与 skeleton 一致，带 `Retry-After`）；(2) full 响应头现由 sidecar 拥有（`Content-Type: application/json`、按 `Accept-Encoding` 决定的 `Content-Encoding`、`Vary: Accept-Encoding`），不再原样透传上游 body-content 头（body 已被改写，上游 ETag/Content-Length 等会 stale）；(3) list full 仍原样透传上游 `Link` 头（full 分页契约不变，不下发 `X-Next-Cursor`）；(4) 上游 200 但非 MessageWithParts 形状（如非 dict/list）原样服务（strip 对无可剥 shape 为 no-op，不升 500，与原 passthrough 一致）。

---

## [0.9.0] - 2026-07-24

> 可观测性地基（应用日志 + `X-Request-ID` 关联 + access log `requestId` + startup banner）+ 安全加固（catch-all 路径归一闭合 `//session//shell` 绕过、directory 校验、剥 `X-Forwarded-*`/cookie）+ 指标增强（traffic 每桶 latency 分位 / 错误计数、sessions 列表 transform admission）+ 架构整洁（路由解耦、配置收编、异常链）。**全加性 wire 行为，`X-Slimapi-Version` 仍 `1`，不 bump**；ocdroid 对接：新增 400 `invalid_path`/`invalid_directory`、sessions 列表高并发可能 503 `transform_busy`、`X-Request-ID` 头与 access `requestId`（诊断用，非契约依赖）。

### Added

- **可观测性地基**：新增应用级日志（env `OC_SLIMAPI_LOG_LEVEL`，默认 INFO，stderr handler；不触碰 access JSONL logger）；**`X-Request-ID`** 关联——最外层纯 ASGI 中间件为每请求生成/透传 `X-Request-ID`（入站值含 CR/LF/控制字符/空白/超 128 则丢弃改生成 uuid），响应头回显（去重），并作为上游头透传 opencode；access log（`logs/access.jsonl`）每条记录新增 `requestId` 字段；startup banner 记录生效配置（route secret 输出 `<redacted>`）；多处静默 `except` 补 `logger.warning` 与 `raise … from exc`。**加性运维/诊断，不 bump `X-Slimapi-Version`**。
- **`proxy.py` 路径归一化与防遍历（S2）**：catch-all 反代新增 `_normalize_path()`，折叠 `//` 并拒绝 `..`/`.` 段（→ **400 `invalid_path`**）；deny-list 检查与上游转发共用同一归一化路径，闭合 `//session//{sid}/shell` 等逃逸缺口；`/slimapi/` 命名空间判断移至归一化之后。**加性安全加固，不 bump `X-Slimapi-Version`**。
- **`directory.py` `validate_directory()` 校验（S5）**：新增 `validate_directory()`，拒绝 `..`/NUL/控制字符/超长（>4096）→ **400 `invalid_directory`**，替代 `normalize_directory()` 作为路由层入参守卫。路由层 6 处入口（sessions/messages/questions/token_stream/children/catch-all proxy）均已迁移。**加性安全加固，不 bump `X-Slimapi-Version`**。
- **`upstream.py` 扩展 `strip_hop_by_hop`（M1）**：新增剥去 `x-forwarded-*`、`x-real-*`、`x-real-ip` 及 `cookie` 头（opencode 不依赖 cookie）。保留 `x-request-id`（批次1 关联头）。**加性安全加固，不 bump `X-Slimapi-Version`**。
- **`/slimapi/metrics` 流量分位与错误计数（M5）**：traffic ledger 新增每桶 `errors4xx`/`errors5xx` 计数与 `latencyMs`（`p50`/`p90`/`p99`/`count`，取自最近 1024 个 `duration_ms` 样本）。**加性诊断字段，不 bump `X-Slimapi-Version`**。

### Changed

- **sessions 列表纳入 transform admission（M8）**：`/slimapi/sessions` 列表的 upstream 取数→解析→投影现由 `TransformPool`（`max_transforms`）门控，skeleton 投影卸载到 worker 池；池饱和时返回 **503 `transform_busy`**（与 messages 路径一致）。客户端：高并发列表可能收到 `transform_busy`，按既有重试语义处理。**非破坏性（新增饱和退路），不 bump `X-Slimapi-Version`**。

### Security

- **S2/S5/M1**：拒绝畸形 path/directory、不冒传伪造客户端 IP/转发链头、不冒传 cookie。均为加性安全加固，不影响合法客户端，不需 bump wire API 版本。
- **catch-all directory 校验语义**：catch-all 反代对 `?directory=` query / `X-Opencode-Directory` 头做**安全门**校验（拒绝 `..`/NUL/控制字符/超长），但作为透明反代**原样转发**原始值（不归一化）；thin 路由因自行构造上游调用而归一化。合法 directory 二者语义一致。

### Known Limitations

- **`/slimapi/sessions` 列表单响应 body 未做 `read_with_cap`**：TransformPool admission 限制**并发**列表请求数（`max_transforms`），但不限制单个 upstream 响应体大小；`max_transforms=1` 时单个超大 `/session` 响应仍可能占用较多内存。后续可对齐 messages 的流式 `read_with_cap`。
- **`latencyMs` 分位口径**：`/slimapi/metrics` 的 `p50`/`p90`/`p99` 采用**高偏 nearest-rank**（`samples[min(n-1, int(n*p/100))]`，`int` 截断），非线性插值；跨系统比较时注意口径差异。

---

## [0.8.0] - 2026-07-24

> 阈值化 skeleton（缺陷 B 服务端修复：tool/patch 的 `state.output`/`state.error` 按字节阈值内联，ocdroid slim 用户重见工具输出）+ T3 ops 桶名归一（`qp`→`quiz`、`proxy_passthrough`→`passthrough`）+ 路由↔文档漂移门禁（`scripts/check_routes_doc.py`，接入 `check.sh`）。**全加性 wire 行为，`X-Slimapi-Version` 仍 `1`，不 bump**；ocdroid 对接：默认常开、无 opt-in，`/slimapi/health` 加性诊断键 `features.thresholdedSkeleton`/`skeletonInlineOutputMaxBytes`（仅诊断，行为不依赖客户端识别）。

### Added

- **阈值化 skeleton（加性，wire 版本仍 `1`，不 bump `X-Slimapi-Version`）**：tool/patch 部件的 `state.output`/`state.error` 不再无条件剥离——按 **JSON 字节**（`orjson.dumps` 序列化长度 = 上线字节，含引号/多字节）阈值化：per-field ≤ 4 KiB **且** 该 message 累计内联 ≤ 16 KiB → **原样内联**进 thin state；超任一阈值 → **整字段 omit**（**绝不半截断**）+ `omitted`，可经 `/full` 取回完整值。`state.structured/result/raw/attachments` **始终 omit**。修复 ocdroid slim 用户看不到任何工具输出（diff/文件内容/命令结果/子任务结果）的缺陷。
  - **`hasFull` 语义收紧**：仅当该 part 仍有 omitted 字段才置 `true`；某 part 所有 output/error 都内联且无其他删字段 → **不设** `hasFull`（客户端 UI 不出现展开按钮）。`hasFull` 只表示"还有可经 full 取回的字段"，**绝不**表示"当前内容不可见"。
  - **默认常开，无 opt-in**（单用户产品；行为不依赖客户端识别 flag）。
  - **`GET /slimapi/health` 加性诊断键**：`features.thresholdedSkeleton=true` + `features.skeletonInlineOutputMaxBytes=4096`（与 `tokenStream` 并列于 root `features`）。**仅诊断/日志**，不参与能力协商，不 bump 版本。
  - **env 调参（不改契约）**：`OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES`（默认 `4096`）、`OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES`（默认 `16384`）。外层响应仍受 `max_response_bytes` 约束（阈值化不绕过）。
  - **expand 路径不变**：仍 `GET /slimapi/messages/{sid}/full?ids=`（message-level，part id 透传 upstream，无 slimPartKey）；full 响应含完整 `state.output`、无骨架标记、`hasFull` 清除。upstream `part.id` 全程稳定。

### Changed

- **`GET /slimapi/metrics` `traffic.buckets` 桶名 `qp` → `quiz`**：questions / permissions 路由前缀归入的桶标识符改名（"快问快答"，覆盖 question 待答 + permission 快速批准/拒绝两类交互）。**破坏性 key 改名，但 `/slimapi/metrics` 为 T3 ops 端点、非客户端契约（ocdroid 不消费），依 [0.7.0] 先例不 bump `X-Slimapi-Version`（仍 `1`）**；读取该 key 的 ops 工具/仪表盘需改键名。`ratios` / `totals` 口径不变。
- **`GET /slimapi/metrics` `traffic.buckets` 桶名 `proxy_passthrough` → `passthrough`**：catch-all 反代路由（`/**` 写操作等透传）桶标识符改名。同上：T3 ops 端点、非客户端契约，**不 bump** `X-Slimapi-Version`（仍 `1`）；读取该 key 的 ops 工具/仪表盘需改键名。`ratios` / `totals` 口径不变。

---

## [0.7.0] - 2026-07-24

> 流量记录与分析（省流实证）。新增全量双向字节账本 + 结构化 access log，作为 `GET /slimapi/metrics` 的加性 `traffic` 键暴露。**全加性 ops 可观测能力**，**未 bump** `X-Slimapi-Version`（仍 `1`）；ocdroid 对接无变化（`/slimapi/metrics` 为 T3 ops 端点，非客户端契约）。两轮三评委评审（GLM/Grok/GPT）全票 APPROVE-WITH-NITS，无阻塞。

### Added

- **`GET /slimapi/metrics` 加性 `traffic` 键**：全量双向字节账本——按路由桶（`events_sse` / `token_stream_sse` / `messages` / `sessions` / `qp` / `proxy_passthrough` / `health` / `projects` / `metrics` / `other`）记录 `upIn`（从 opencode 拉的字节=成本）、`downOut`（下发给客户端=省流后）、`downIn`/`upOut`、`requests`，及 `totals` 与 `ratios.{bucket}.downOutOverUpIn`。未接线 ledger 时 `/metrics` 形状零变化（纯加性）。**加性，未 bump**。
  - **downstream 计数**：纯 ASGI middleware（包 `receive`/`send`，O(1) `len(chunk)`，不缓冲/不延迟首字节）；SSE 桶的 `downOut` 由 per-frame `record_sse_downstream` 拥有——middleware 对真 SSE 流（`200` + `text/event-stream`）传 `resp_bytes=0`，SSE 路径上的 400/503 错误响应正常计 downOut。
  - **upstream 计数**：各消费点 `stash_up_in`（含 4xx/5xx 错误响应 drain+stash、`/ready` 探活、`load_products` 发现、batch per-mid 404/4xx）；proxy 请求流 `try/finally` 保断连时 `upOut` 不丢。
  - **`ratios` 语义**：`downOutOverUpIn` 对非 SSE 桶 = 下发/成本省流比（`messages` 骨架投影 <1.0 即省流）；对 SSE 桶 = **聚合下发/共享上游成本**（多订阅 fanout 下可 >1.0，**非单连接省流比**；单订阅 `downOut ≪ upIn` 才是真省流证据）。
- **结构化 access log**：JSON-lines 落盘（默认 `logs/access.jsonl`，`RotatingFileHandler` 轮转），每请求一行 `{ts,method,path,bucket,status,durationMs,downIn,downOut,upIn,upOut}`；logger 名 `oc_slimapi.access`、`propagate=False`（不污染 uvicorn 日志）、disabled 时纯 no-op。**加性，未 bump**。
- **配置 env**：`OC_SLIMAPI_TRAFFIC_METRICS_ENABLED`（默认 `1`，内存账本总开关，关时 `traffic`=`{enabled:false}`）、`OC_SLIMAPI_ACCESS_LOG_ENABLED`（默认 `1`）、`OC_SLIMAPI_ACCESS_LOG_PATH`（默认 `logs/access.jsonl`）、`OC_SLIMAPI_ACCESS_LOG_MAX_BYTES`（默认 `10485760`）、`OC_SLIMAPI_ACCESS_LOG_BACKUPS`（默认 `5`）。

### 运维/已知限制（非 wire 变更，已 docstring 文档化）

- **SSE upstream 字节为 LF 行尾估算**：计数抽成纯函数 `_upstream_line_bytes(line)` = `len(line.encode)+1`；CRLF 上游每行少计 1 字节（保守偏向，让省流比看起来更少；opencode `/global/event` 预期为 LF）。
- **children-cache fetch 不归属 per-bucket upIn**：single-flight coalescing 下归属不公，有意不计；`snapshot()` docstring 标注此盲区（sessions/children 桶省流比略偏乐观）。
- **access log `downOut`（wire 级）与 ledger SSE 桶 `downOut`（per-subscriber-per-frame 聚合）口径不同**，不应直接对照；SSE 统计以 `/slimapi/metrics.traffic` 为准。
- **SSE 桶快照时间口径**：`requests`/`downIn` 在 SSE 连接关闭时才落账（活跃长连接期间为 0），而 `downOut`/`upIn`/`framesEmitted` 实时累加。
- **`record_downstream`/`record_upstream` 的 `method`/`status`/`duration_ms` 为 reserved/unused**（status 由 access log 另记；metrics 无 per-method/per-status 细分）。

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

> Token 批式 SSE（opt-in 实时流）上线。**全加性 wire 行为**，**未 bump** `X-Slimapi-Version`（仍 `1`）。设计 `docs/specs/design-token-stream.md` v4；契约 `docs/specs/v1-contract.md` rev J。双边联合终审 re-gate **GO 9.7**（rev-bgpt）；ocdroid 已 shipped（commit `1986567`）。

### Added

- **Token 批式 SSE（opt-in 实时流）**：新可选端点 `GET /slimapi/sessions/{sid}/stream`——生成中实时推送 in-flight text part 的渐进文本，解决「打开 busy session 看到半截且冻住」（上游 `message.part.delta` 不落库，sidecar 此前丢弃）。**全加性 wire 行为**，**不 bump** `X-Slimapi-Version`（仍 `1`）。设计权威 `docs/specs/design-token-stream.md` v4（架构级 PASS）；契约落地 `docs/specs/v1-contract.md` §3.x（端点+帧+gzip）+ §6.x（token T3 信封）。
  - **端点**：`GET /slimapi/sessions/{sid}/stream?directory=<optional>`；`text/event-stream`；响应头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`；版本门禁复用 `SlimapiVersionMiddleware`（无 route-level `Depends`）。directory 仅过滤进程级 GlobalBus 事件，**不开第二条上游连接**；sid 全局唯一、directory 无关（单用户 T3）。路由注册在 catch-all 反代之前。
  - **帧类型**（§5.6）：订阅首帧 `message.part.snapshot{done:false}`（累计全文锚点）+ 批式 `message.part.delta{text}`（100ms / 4KiB flush，§5.4）+ 终态 `message.part.snapshot{done:true}`（**杠杆1：仅完成 marker，无 text**——权威全文走 `/since`，取消 upstream `part.text` 终态重发）+ `message.part.snapshot{truncated:true}`（>1MiB，不静默 drop）+ `resync` + `server.connected` / `server.heartbeat`（15s）。**不发 SSE `id:` 字段**、**无 replay buffer**；`Last-Event-ID` 仅触发首帧 resync，值忽略。
  - **resync reasons**（token 流均带 `sessionID`）：`reconnect_no_replay`（上游重连）、`subscriber_backpressure`（订阅者 T3 溢出）、`token_memory_limit`（全局累加器上限）、`session_idle`（生成结束清理）、`session_deleted`（会话被删除）。单 part >1MiB 走 `snapshot{truncated:true}`（非 resync）。
  - **终态顺序不变式**（wire 强约束）：同一 `(sid,mid,pid)` 所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不再发 delta。
  - **权威对齐**：stream `snapshot{done:true}` 是「流视角完成」（**marker 无 text**）；digest + `/since` 拉取的是「持久化真值」——不一致以 `/since` 为准（幂等覆盖）。客户端可接受 digest 完成先于/晚于 token 终态帧。
  - **杠杆2：gzip 首个 SSE 例外**：token stream **默认 gzip**（流式 zlib `Z_SYNC_FLUSH`，`Content-Encoding: gzip`，按 `Accept-Encoding` 协商）。**首个 SSE gzip 例外**——此前「SSE 永不 gzip」（§9）的唯一破例；控制面 `/slimapi/events` **仍不 gzip**。实测（harness `scripts/measure_token_overhead.py`，12 trace、30 tok/s × 100ms）：原批式 ~12x → 杠杆1+2 后 gzip 中位 **1.47x**（达成 re-anchor ~1.5x 中位目标；1/3 trace <1.0x）；残余调参（flush 窗 / gzip cadence）可选 post-release。
  - **health 加性字段**：`GET /slimapi/health` 根级 `features.tokenStream:true`（Q1 冻结路径：top-level `features`，与 `sidecar`/`server`/`schema` 并列；客户端可 dual-read root/server 过渡，服务端固定 root）。`features.tokenStream` 缺/404/405 → ocdroid 降级「完成后整条出现」（零回归）。
  - **T3 独立信封（Option B 拆 4+4）**：token 订阅独立账本（`token_stream_max_subscribers=8`、`token_stream_queue_items=64`、`token_stream_buffer_bytes=512KiB/sub`、`token_stream_max_frame_bytes=1MiB`），**不**消费既有 `MAX_TOTAL_SUBSCRIBERS=16`；**内存预算 Option B**（拆 4+4，**不双计**）+ **handshake buffer**：`TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live）+ `TOKEN_PENDING_MAX_BYTES=4MiB`（pending）+ `TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB/sub`；worst-case `8 × (512KiB queue + 8MiB handshake) + 4MiB live + 4MiB pending = 76MiB`（runtime 正常态无 handshake 占用时仅 12MiB）。admission 失败 → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。handshake buffer overflow → 503 `{"code":"sse_token_handshake_overflow","limit":8,"current":N,"bufferBytes":N}` + `Retry-After:5`。
  - **控制面零回归**：`/slimapi/events`（控制面）一行不改；token 流消费上游 `message.part.delta`/`updated`（控制面此前丢弃），与控制面队列隔离（避免 token 高吞吐挤掉 q/p 或误触 `subscriber_backpressure`）。
  - **P1 范围**：仅 text part（reasoning / tool-input 延后 P2+）；不做二进制流。
  - **依赖与状态**：服务端 Stages A–E（§14）落地（A 地基 9.5 / B 生命周期 9.5 / C flush 9.5 / D 端点 9.6 / E 文档+预算 4+4）；本版本随 0.5.0 出货，双边联合终审 re-gate GO 9.7。ocdroid 配合清单见 `docs/specs/CLIENT_CHANGES.md`「Token stream SSE」节。批式参数（`TOKEN_FLUSH_SECONDS`/`TOKEN_FLUSH_BYTES`）为服务端 env knob，**不进 wire**，ocdroid 无需跟随调整。

## [0.4.0] - 2026-07-22

> 透传收敛 + 重构（Batch 0–5）。多批加性 wire 行为变更，**未 bump** `X-Slimapi-Version`（仍 `1`）。契约权威 `docs/specs/v1-contract.md` rev I。

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
- **新增加性错误码（thin 路由）**：`invalid_directory_count`（400，questions directory 数量 1–32 守卫）；`invalid_route_token`（400，questions routeToken 校验失败）。两者均加入 `docs/specs/v1-impl-spec.md` §11 统一错误码表，**加性，不 bump**。

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

> 本批次（2026-07-20 rev C）ratify ocdroid 契约遗留 3 缺口（**Gap1** 等时间戳 tie-break + **Gap2** 空/失败区分 + **Gap3** `/since/0` cursor drain）+ 查证中发现的 2 个 pre-existing 真 bug（`/since` 过滤 no-op + `/sessions` 列表 §7 偏离）+ 2 处防御缺口（q/p 规范化去重 + `/sessions` 坏 JSON→503）。全加性，**不** bump `X-Slimapi-Version`（仍为 `1`）。逐条对照见 `docs/specs/v1-contract.md` §14.6。

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

> 本批次（2026-07-20）所有变更加性，**不** bump `X-Slimapi-Version`（仍为 `1`）。ocdroid《slimapi 接口评审报告》原始发现 F1–F5 + §5 文档建议全部落地；本仓扩展 G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步）一并实现；另修 2 个 pre-existing SSE 生命周期 bug + G1 `error.name` 类型防御。逐条对照见 `docs/specs/v1-contract.md` §14。

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

- 契约：[`docs/specs/v3-contract.md`](docs/specs/v3-contract.md)（≤2.x 历史：`docs/specs/v2-contract.md`）
- 发版：[`docs/release.md`](docs/release.md)
- 客户端清单：[`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md)

## [3.0.0] - 2026-08-17 — M3 v3-only 终态（v2 wire 语义整体退役；破坏性，`X-Slimapi-Version` 头删除非 bump——版本协商唯一机制 = `?v=3` selector + `/slimapi/versions`）

### Removed（权威= `docs/specs/v3-contract.md` §1/§2/§5.7/§6.2/§8.2/§8.3）

> 本次为**破坏性变更集**。升级前置条件（生产 systemd 单元）：`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=3,3`（原 `2,3`）后**重启** oc-slimapi；ocdroid 必须已全部走 `?v=3` 形态。

- **v2 管线删除**：`/slimapi/**` 无 `v`、`v=2`、合法但不支持的值 → 400 `{"code":"unsupported_version","supported":[3]}`（端点存在、协议版本退役；绝不静默 404）。SSE 端点同表（400 先于流打开）。
- **`X-Slimapi-Version` 头退役**：不再读取、不解读（出现不报错）；`versioning.py::SlimapiVersionMiddleware` 门禁类删除。`accepted_client_versions` 终态 `[3,3]`（`/slimapi/health` 回显）；`OC_SLIMAPI_V3_SELECTOR_ENABLED` 灰度开关删除（v2 管线已不存在，回退无意义）。
- **`X-Opencode-Directory` 请求头退役（消费集）**：消费集路由上该头出现 → 400 `{"code":"directory_header_retired"}`（canonical 通道 = `?directory=`）；双现同值亦 400（头通道整体退役，无"同值放行"）。
- **响应头退役**：`X-Next-Cursor`（messages 列表改由 envelope `nextCursor` 字段承载）、`X-Complete`（sessions 列表改由 envelope `complete` 字段承载）、`X-Slimapi-Subscriber-ID`（SSE 改由首帧 `slimapi.meta` 的 `subscriberId` 承载）永不产出。
- **SSE 终态**：`/slimapi/events` 与 `/slimapi/sessions/{sid}/stream` 首帧恒 `event: slimapi.meta`（`{"subscriberId","tokens"}`，先于业务帧/心跳/重放）；token stream 恒 identity（lever2 gzip 压缩路径移除）。
- **Vary 收缩**：全路由 `Vary: Accept-Encoding` 单值（directory 维度随头退役退出 Vary）。
- **catch-all 反代关闭**：未收编路径（含原 `GET /event`、`GET /global/event`、`/session/**` 透传等）全方法 → 404 `{"code":"thin_route_not_found"}`；WebSocket 仍 501 stub。**turn fence 迁移**：`prompt_async`/`abort` 写路由的 `turnIncarnation`/`turn` bump-before-send 随收编路由保留（原 catch-all 前递器职责迁至 `write_groups.py`）。
- **终态错误优先级链（§8.3）**：① 非 GET `/slimapi/versions` → 405 → ② selector 400（`invalid_version_selector`/`unsupported_version`）→ ③ directory 400（多值 `invalid_directory_selector` → 双现异值 `directory_conflict` → 消费集头 `directory_header_retired`）→ ④ 路由未命中 404 `thin_route_not_found`。
- **观测不变式**：access log/snapshot 字段与枚举（`selectorResult` 六值、`sseActive` 四维）**不变**——`absent`/`v2` 维度自然归零是预期（不再有 v2 语义请求）。

## [Unreleased]
