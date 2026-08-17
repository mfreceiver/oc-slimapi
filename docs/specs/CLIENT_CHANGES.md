> **Aligned with v2-contract.md (lite-v2 cleanup)**
>
> This document reflects the current wire surface. All endpoints not listed
> here have been deleted and return 404. See v2-contract.md for authoritative
> specification.

# ocdroid 客户端改动清单（仅文档，不修改 ocdroid）

## text 正文永远全量内联（3.2.0 — 包版本 minor，wire 仍 v3，2026-08-17）

> **放宽性变更，客户端零必改**：sidecar skeleton 投影中 `TextPart.text` 不再有任何字节阈值——**无论 UTF-8 字节数多少一律原样内联**，不再出现 `text:null + omitted + hasFull + part_text ref` 形态的 text part（3.1.x 曾按 >2048 折叠）。`ReasoningPart.text`（>2048 折叠为 `part_reasoning`）与其余折叠类目（tool state 5 类、`part_url`/`part_source`/`part_snapshot`/`info_summary_diffs`/`compaction_full`）**全部维持 3.1.0 规则不变**。expand 12 类目端点全部保留（`part_text` 端点留存，服务历史缓存响应与降级场景）。对已按 3.1.0 容忍/展开适配的客户端：全量内联是折叠的超集，双形态（3.1.x / 3.2.0）均正常。skeleton 字节变化 → `contentFingerprint`/ETag 自动失效旧缓存。

## expand 片段端点与 skeleton 投影缩减（3.1.0 — 包版本 minor（major 与 wire 协议版本绑定，wire 仍 v3 未 bump，不发 major）；wire 仍 v3，2026-08-17）

> **减性 + 加性并存**：wire 版本**不变**（仍 v3、`?v=3` selector 不变）——skeleton 投影缩减（**减性**）按 owner 决策直接在 v3 视图内生效，expand 片段端点（**加性**）配套补齐。设计稿 `docs/specs/design-expand.md` v5；**权威 wire 见 `docs/specs/v3-contract.md` §4a/§4b**。**部署顺序（关键）**：ocdroid 先发容忍版 → sidecar 3.1.0 后装（见下兼容矩阵）。

### 投影变更影响（新 sidecar 下 `/slimapi/messages/{sid}` 的 skeleton）

- **哪些字段会变 `null`（+ `omitted` + `hasFull` + `expandRefs`）**：
  - `info.summary.diffs` → **恒 `null`** + 消息级 `info.expandRefs[0].category="info_summary_diffs"`（非空 list 才 ref；`/full/{mid}` 照旧全量，语义不变）。
  - `text`/`reasoning` part：UTF-8 编码字节 **>2048** → 整字段 `null` + `omitted:["text"]` + `hasFull:true` + part 级 `expandRefs`（`part_text`/`part_reasoning`）——**整字段折叠，从不半截断**；≤2048 原样内联。（**[3.2.0] 起本条的 text 部分已废止**——`TextPart.text` 永远内联；reasoning 部分仍有效。）
  - `tool` state：`input`（`object|null`）/`metadata`（`object|null`）/`attachments`（`object[]|null`）折叠 → refs `part_state_input_full`/`part_state_metadata_full`/`part_state_attachments`；`output`/`error` 按现状 4 KB 阈值省略并补 refs。
  - `file` `url`/`source`、`step-start`/`step-finish` `snapshot` 折叠 → refs `part_url`/`part_source`/`part_snapshot`。
  - `patch` part：`files` 为 `string[]` **原样保留 verbatim**（P0 修复），永不折叠、无 ref。
- **`expandRefs` 如何消费**：每个 ref = `{category, messageID, partID?, href}`；`href` 已含 `?v=3`（`/slimapi/messages/{sid}/expand/...`），directory 由客户端按需追加。**列表滚动场景零散片段用 expand 单字段拉取**（`data.<key>` 可能为 `null`——缺失与显式 null 等价）；**详情页直接 `/full/{mid}`**（单消息展开 ≥4 片段累计已接近一次 /full，不划算）。
- **renderability 不受影响**：`text:null` 但带 `expandRefs` 的 part 仍计为**可渲染**（part 骨架 + 展开入口，不是整页 placeholder）；消息级 diffs 省略不参与可渲染判定。既有 `hasFull`/`omitted` 处理逻辑继续沿用，只是现多了 `expandRefs` 数组可读。

### expand 端点用法

- **发现（经 capabilities）**：`GET /slimapi/versions` → `capabilities["3"]["expand"] = {categories: [12 项], fragmentMaxBytes: <live，默认 8388608>}`。**该 key 存在 = sidecar 支持 expand**（勿用 `sidecarVersion` 字符串比较）；缺 key / 旧 sidecar（无路由）→ 404 `thin_route_not_found` → 现状渲染 + 回退（见下）。
- **调用**：`GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=3`（消息级，仅 `info_summary_diffs`）与 `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v=3`（part 级）；`directory` 语义与 messages 路由一致。200 envelope：`{category, messageID, data}`（part 级多 `partID`）+ `Cache-Control: no-store` + 无 ETag（恒 200）+ gzip 按协商。
- **错误处理**：`404 expand_target_not_found`（附 `reason:"part_missing"`）→ 目标 part/字段已删 → 刷新 skeleton；`400 expand_category_mismatch`（附 `expectedLevel`/`expectedTypes`）→ part 类型已变 → 刷新 skeleton；`413 expand_source_too_large`/`expand_fragment_too_large` → 内容超单片段上限 → 改走 `/full/{mid}`；`503 transform_busy`（+`Retry-After`）→ 沿用既有重试范式；`503 upstream_unavailable` / `502 upstream_http_N` / `404 session_not_found` → 与 messages 路由同语义处理（circuit breaker / 移除会话）。**回退规则**：**仅** 404 `thin_route_not_found`（旧 sidecar 无 expand 路由）→ 回退 `/full`；其余错误走重试/刷新，**绝不**静默降级。
- **merged 模式行为**：`?mode=merged` 现为 **placeholder-first + best-effort**——占位消息优先还原（行为与现状完全一致，不被 ref 候选挤占）；剩余预算按页面顺序 best-effort 还原 part 级 ref 候选；`info.summary.diffs` 在 merged 输出**恒为 null + expandRefs**（永不批量还原）；预算外/失败的消息保留 `text: null + expandRefs`——客户端保留展开入口兜底即合规（这**是特性而非缺陷**）。

### 部署顺序与兼容矩阵（2×2）

1. **ocdroid 先发**容忍版：对旧 sidecar 零改动（现状全文渲染）；对新 sidecar 的折叠字段按 `expandRefs` 展开或显示"查看全文"（`/full` 不变）。
2. **sidecar 3.1.0 后装**：`?v=3` selector 不变、wire 不 bump；唯一消费方已具备容忍/渲染能力。

| 客户端 \ sidecar | 旧 sidecar | 新 sidecar（3.1.0+） |
|---|---|---|
| **旧客户端** | 基线：现状全文渲染（不受影响） | **已知减性影响（owner 接受）**：长 text/reasoning 显示为空 + "查看全文"仍可用（`/full` 不变）；diffs 缺失不影响列表基础功能 |
| **新客户端** | 无 `expandRefs`/expand 路由 → 现状全文渲染；expand 请求 404 → 回退 `/full` | 完整 expand 体验 |

### 回退路径

- 无 `expandRefs` 字段（旧 sidecar / 开关关闭）→ 按现状渲染全文（字段本就在 skeleton 内或走 `/full`）。
- expand 请求 404 `thin_route_not_found`（旧 sidecar 无此路由）→ 回退 `/full/{mid}` 整条拉取。
- **不降级触发条件**：除 404 `thin_route_not_found` 外（503/413/timeout/版本错误/鉴权错误）一律重试/刷新——走 `/full` 会让流量翻倍 + 掩盖问题（沿用全局 fallback 规则）。

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

## pending question 跨目录聚合（/slimapi/questions，加性 — 2026-08-05）

> **加性 / 向后兼容**：wire 版本未 bump（仍 2）。旧 sidecar 无此路由 → 404 `thin_route_not_found`，客户端透明回退既有 SSE 直推 + catch-all 单目录 `GET /question`（行为不变）。权威 envelope 语义见 `docs/specs/v2-contract.md` §2「`/slimapi/questions` envelope」。

`GET /slimapi/questions` 修复 slim-mode 冷启动看不到 `workdir ≠ process.cwd()` 目录 pending question 的 bug（上游 `GET /question` per-Location——按 `X-Opencode-Directory` 路由的 workdir instance，无 header 回落 `process.cwd()`）。**无参数**（sidecar 自发现目录）。

### 客户端必须

- **两阶段 fan-out（sidecar 侧，客户端透明）**：sidecar 先 `GET /experimental/session?roots=true&archived=true`（opencode 全局顶层 session 列表 + 含已归档 session，每个 session 携带真实 `directory` 字段——覆盖 git repo + 非-git目录 + git worktree 子目录 + archived-only workdir）发现 distinct directory 集合，再并发对每个 dir `GET /question`（带 `X-Opencode-Directory`）合并。（**2026-08-07**：发现源从 `GET /project` 改为 `GET /experimental/session?roots=true&archived=true`——根因 `/project` 把非-git workdir 归到合成 global（`worktree="/"`）被跳过，导致非-git/临时目录 + git worktree 子目录的 pending question 漏报；`archived=true` 使发现集合成超集防 archived-only workdir 漏报。）
- **envelope 形状**（非裸数组）`{items, errors, authoritativeDirectories, discoveryComplete}`：
  - `items`：合并的 question entry，每条 = 上游 entry 原样 + 追加 `directory` 字段。
  - `errors`：per-dir 失败（isolated，单 dir 失败不中断整体）。
  - `authoritativeDirectories`：`null` = 全成功且发现完整 → **全局 replace-all**；数组 = partial（per-dir 失败或发现截断）→ 仅覆盖所列 dir。
  - `discoveryComplete`：`true` 除非发现页填满 `_DISCOVERY_LIMIT`(=10000)（可能截断）；`roots=true` 只返顶层 session，实际恒 `true`；客户端可忽略。
- **客户端契约（关键）**：`authoritativeDirectories==null` → 用本次 `items` 完整替换本地 pending question 集合（replace-all，不在 `items` 中的旧 question 视为已不再 pending）；为数组 → **仅**对数组所列 dir 做 replace，**不得**丢弃未覆盖 dir 的既有 pending question（否则丢失数据）。
- **total failure**：发现调用失败 → 整体 503 `upstream_unavailable`（无 envelope）；客户端保留既有状态并重试，**不可**据此推断 pending question 集合为空。
- **应答不经本端点**：pending question 的应答仍走 catch-all + `X-Opencode-Directory`（见 v2-contract §2 写路径）；本端点只读聚合。

### 错误码（thin 路由统一 `{"code":"..."}`）

`400 version_required`/`version_incompatible`、`404 thin_route_not_found`（旧 sidecar，回退信号）、`503 upstream_unavailable`（发现调用 total failure，无 envelope）。per-dir `/question` 失败 isolated 进 `errors[]`（非 HTTP 错误码，不中断整体）。

## Catalog skeleton（command / agent，加性 — 2026-08-05）

> **加性 / 向后兼容**：wire 版本未 bump（仍 2）。旧 sidecar 无此路由 → 404 `thin_route_not_found`，客户端透明回退 catch-all 透传 `GET /command`、`GET /agent`（行为不变）。旧 ocdroid 继续走透传，零回归。完整集成告知：`docs/ocmar/reports/2026-08-05-ocdroid-catalog-skeleton-integration.md`。

两个 catalog 读接口走 slim skeleton 投影（实测省流 command ~97.6% / agent ~95.8% raw）：

- **`GET /slimapi/command`**：须带 `X-Slimapi-Version: 2`（建议 `Accept-Encoding: gzip`）；query `directory?`（可选，仅作 `X-Opencode-Directory` 头转发，catalog 全局）。200 返回裸数组，每项白名单 `{name, description, agent?, hints?}`（agent/hints 可选，缺则不出现）；**已丢弃** `template`(~97.7%)/`source`/`model`/`subtask`。
- **`GET /slimapi/agent`**：同上头与 directory 语义。200 返回裸数组，每项白名单 `{name, description, mode, hidden?, native?}`（hidden/native 可选，可能为 null/false）；**已丢弃** `prompt`(~34.7%)/`permission`(~61.2%，`Permission.Ruleset` 规则集——**非** pending permission card)/`topP`/`temperature`/`color`/`variant`/`options`/`steps`/`model`。

### 客户端必须

- **能力探测**：加性路由 ≠ 旧 sidecar 支持。用 health feature flag 或一次性 404 探测（`thin_route_not_found` = 不支持）；探测结果缓存，勿每连接重复。
- **fallback 规则（关键）**：**仅** `404 thin_route_not_found` → 回退 catch-all 透传 `GET /command`、`GET /agent`。**绝不**对 `503`(upstream_unavailable/transform_busy)/`413`/timeout/版本错误/鉴权错误回退（会流量翻倍 + 掩盖问题；503 走 circuit breaker + Retry-After 重试）。
- **字段消费**：command 读 name/description/agent/hints；agent 读 name/description/mode/hidden/native（`native` 务必解析）。**不要**从 slim 端点期望被裁字段；若 UI 真需 template/prompt/permission → 走 passthrough（本批无 `/slimapi/command|agent/full`）。
- **灰度**：feature flag 启用；关 flag = 立即回退 passthrough，无需改代码。

### 错误码（thin 路由统一 `{"code":"..."}`）

`400 version_required`/`version_incompatible`、`400 invalid_directory`、`404 thin_route_not_found`（旧 sidecar，回退信号）、`413 response_too_large`、`502 upstream_http_N`、`503 upstream_unavailable`、`503 transform_busy`(+`Retry-After:2`)、`422`。catalog 非 session 级，无 `session_not_found`。catalog 条目**无** `hasFull`/`omitted`（那是 message part 概念）。

### 监控

`GET /slimapi/metrics` 响应的 `traffic` 块已有独立 `command` / `agent` 桶（upIn/downOut/省流比）；access log 每条带 `bucket` 字段。

### 未做（待需求确认）

`/slimapi/command|agent/full`（仅当确认 UI 真需 template/prompt/permission；优先「按名查单条详情」而非全量 full）；`hints` 单项/总量 cap（当前原样保留，live 值 14B，省流比为实测非保证）。

## 全局 directory catalog（/slimapi/directories，加性 — 2026-08-08）

> **加性 / 向后兼容**：wire 版本未 bump（仍 2）。旧 sidecar 无此路由 → 404 `thin_route_not_found`，客户端透明回退（不渲染项目切换器，或用 `/slimapi/sessions` 兜底）。权威 envelope 语义见 `docs/specs/v2-contract.md` §2「`/slimapi/directories` envelope」。

`GET /slimapi/directories` 列出 opencode 已知的工作目录（directory），供客户端渲染"项目切换器"。**无 query 参数**（全局发现语义，sidecar 自发现，客户端不传 directory）。发现源与 `/slimapi/questions` 共用 `GET /experimental/session?roots=true&archived=true`（每个 session 携带真实 `directory` 字段）。

### 客户端必须

- **envelope 形状**（非裸数组）`{items, discoveryComplete}`：
  - `items`：每个 distinct directory 一行（`/a` 与 `/a/` 经归一合并成同一行），排序 `lastUpdated` DESC + tie-break `directory` ASC。每行字段：
    - `directory`：归一后的 workdir 绝对路径。
    - `title`：该 dir 下 winner session 的 `title`（非非空 string → `null`）。
    - `lastUpdated`：winner session 的 `time.updated`（数字；缺失/非数字→0）。
    - `rootSessionCount` / `activeRootSessionCount` / `archivedRootSessionCount`：该 dir 顶层 session 总数 / 未归档 / 已归档。
    - `archivedOnly`：`activeRootSessionCount == 0`（该 dir 顶层 session 全已归档）。
  - `discoveryComplete`：`true` 除非发现页填满 `_DISCOVERY_LIMIT`(=10000)（可能截断；`roots=true` 只返顶层 session，实际恒 `true`，客户端可忽略）。
- **fallback 规则（关键）**：**仅** `404 thin_route_not_found` → 回退（旧 sidecar 无此路由，或用 `/slimapi/sessions` 兜底）。**绝不**对 `503`(upstream_unavailable/transform_busy)/`413`/timeout/版本错误/鉴权错误回退（会流量翻倍 + 掩盖问题；503 走 circuit breaker + Retry-After 重试）。
- **total failure**：发现调用失败 → 整体 503 `upstream_unavailable`（无 envelope）；客户端保留既有项目列表并重试，**不可**据此推断"无任何 workdir"。
- **被动发现局限（诚实，客户端需知晓）**：本端点**仅覆盖至少有一条顶层 session 的 workdir**。**从未建过 session 的 workdir 不可见**（用户须先在该 workdir 发起一次会话才会出现）；**不扫文件系统**；**返回目录不代表目录仍存在**于文件系统（workdir 可能已被删除，旧 session 仍记录其 path）。客户端"新建项目/打开文件夹"仍需走既有路径（本地文件选择 + 发首条消息建 session）。
- **`archivedOnly` 含义**：`true` = 该 workdir 所有顶层 session 都已归档（用户在该 workdir 无活跃会话）。UI 可据此弱化/折叠该条目，但不应隐藏（用户可能仍想切换回去继续）。

### 错误码（thin 路由统一 `{"code":"..."}`）

`400 version_required`/`version_incompatible`、`404 thin_route_not_found`（旧 sidecar，回退信号）、`503 upstream_unavailable`（发现调用 total failure / 严格 schema 守卫失败 / 超响应 cap / 网络 5xx，无 envelope）、`503 transform_busy`(+`Retry-After:2`)、`422`。directories 非 session 级，无 `session_not_found`。**注意**：discovery 4xx 也映射为 `upstream_unavailable`（不泄漏 upstream status——experimental 端点 4xx 意 opencode 不支持）。

### 监控

`GET /slimapi/metrics` 响应的 `traffic` 块已有独立 `directories` 桶（upIn/downOut/省流比）；access log 每条带 `bucket` 字段。

### 非 slim 模式 / 旧 sidecar 降级方案

`/slimapi/directories` 仅在 sidecar ≥ v1.2.0 的 slim 模式下可用。两种降级场景与推荐实现：

**场景 1 — 旧 sidecar（< v1.2.0，slim 模式）**：调 `/slimapi/directories` 返回 404 `thin_route_not_found` → 触发降级（探测结果缓存，勿每连接重复）。

**场景 2 — 非 slim 模式（ocdroid 直连 opencode，不经 sidecar）**：无 `/slimapi/**` 路由面。

> **ocdroid 实际采用（2026-08-08 双向确认）**：
> - **降级地板 = 方案 2（MRU 本地列表）**，复用既有 `recentWorkdirs`（EncryptedSharedPreferences per-fingerprint、cap 30），仅用户选中时并入（不批量灌入，防驱逐本机最近条目）；禁 DataStore（项目规范硬约束）。
> - **不上方案 1**（`/experimental/session` 自聚合代价过高，且 legacy 模式直接隐藏功能无需它）。方案 1/3 保留为参考实现（其他客户端或未来选项）。
> - legacy（slim=false）隐藏整个项目切换器功能；slim + 旧 sidecar 时入口图标仍在、sheet 降级 MRU 标「本机最近项目」。
> - capability 探测：一次性 404 probe + sticky flag（复用 `ServerCompatProfile.supportsSlimDirectories`），不引入 feature flag 系统。
> - **normalize 一致性（关键）**：客户端「已连接禁选集」去重的 `normalize()` 必须与 sidecar `normalize_directory` 语义一致——`s.rstrip("/") or "/"`（去所有尾部斜杠，根 `/` 保留；`/a/b/`→`/a/b`、`/`→`/`），否则 sidecar 下发的已归一 `directory` 与客户端 normalize 结果不匹配 → 禁选集漏判 / 重复添加。
> - `transform_busy` + `Retry-After`：ocdroid 已有 `retryAfterHeaderToMs` + 重试范式，本端点复用（sidecar 仍发 `Retry-After:2`，无改动）。

**降级实现（参考，按覆盖面排序）**：

1. **直连上游自聚合（推荐，覆盖面与 slim 版相同）**：ocdroid 直接请求 opencode `GET /experimental/session?roots=true&archived=true&limit=10000`（走 ocdroid direct 配置端口，经 :14096 mTLS），客户端自行 group-by-directory 聚合——算法与 sidecar `_aggregate_and_pack` 一致：
   - group key = `directory`（去尾斜杠，根 `/` 保留）；
   - 每 dir：`rootSessionCount` / `activeRootSessionCount`（`time.archived` 非数字）/ `archivedRootSessionCount`（`time.archived` 数字，排除 bool）/ `archivedOnly`（active==0）；
   - winner = `(time.updated, time.created, id)` 字典序 max；`title` + `lastUpdated` 取自同一 winner（缺失数字→0 排最后）；
   - items 排序 `lastUpdated` DESC + tie-break `directory` ASC。
   - **代价**：客户端拉完整 `Session.Info` 对象（不省流；`roots=true` 只返顶层 session，量级 ≈ workdir 数，单次通常可接受）+ 自实现聚合。
   - **依赖**：opencode ≥ v1.18.x 提供 `/experimental/session`（experimental 端点，跨版本兼容性需注意；若 opencode 不支持或返 4xx → 降级到方案 2）。

2. **本地维护 directory 列表（兜底，零网络依赖）**：客户端持久化"用户访问过的 workdir 路径"（历史记录 + 手动添加）。完全不依赖服务端发现，最可靠；但不自动发现新 workdir（用户须先在该 workdir 发起会话，客户端记录其 `directory`）。建议与方案 1 叠加：方案 1 拉到的 directory 并入本地列表，方案 1 不可用时退回本地列表。

3. **legacy `/session` per-Location（不足以做跨目录项目切换器）**：`GET /session` 受 `X-Opencode-Directory` 路由，只能看当前 workdir，无法跨目录发现——仅适合"确认当前 workdir 可达"，不适合渲染跨目录切换器。

**不降级触发条件**：与 fallback 规则一致——**仅** 404 `thin_route_not_found`（场景 1）或确认处于非 slim 模式（场景 2）才走降级。**绝不**对 503（`upstream_unavailable` / `transform_busy`）/413/timeout/版本错误降级（走重试 + circuit breaker + `Retry-After`）。

## 通用管理动作（/slimapi/actions，加性 — 2026-08-09）

> **加性 / 向后兼容**：wire 版本未 bump（仍 2）。旧 sidecar 无此路由 → 404 `thin_route_not_found`，客户端透明回退（不展示管理动作 UI）。

`/slimapi/actions` 提供服务器端管理能力。配置驱动（TOML manifest），两类动作：

- **exec**：触发服务器端命令，回显 `{kind:"exec", ok, exit_code, duration_ms, message}`（**`message` 字段始终出现**，成功时为 `null`；非零退出为固定短串 `"non-zero exit"`）。
- **query**：触发命令，回显待渲染 markdown `{kind:"query", ok, markdown, exit_code, duration_ms, truncated, message}`（**`message` 字段始终出现**，成功时为 `null`）。

### 客户端必须

- **发现可调用动作**：`GET /slimapi/actions` → 返回 `{"enabled":bool,"actions":[{"name","kind","description","requireConfirm"}]}`。未配置 manifest 时 `enabled:false`，actions 为空数组。
- **调用动作**：`POST /slimapi/actions/{name}`，body `{}` 或空 body（`require_confirm=true` 的 exec 动作须 `{"confirm":true}`，否则 409）。
- **query 响应 `markdown` 字段安全渲染**（关键）：query 类响应的 `markdown` 字段是脚本 stdout 投影，可能含**任意内容**（由管理员定义的 action 脚本输出生成）。ocdroid 渲染 `markdown` **必须用 sandboxed markdown renderer**，禁用 raw HTML、脚本执行、远程图片加载，防止 XSS 或意外内容执行。
- **exec 响应 **：`ok:true` 仅表示子进程 exit 0，**不**代表 opencode 就绪。客户端须轮询 `/slimapi/ready` 确认 opencode 可达（如果 exec 动作涉及重启 opencode 等操作）。
- **无可变参数**：所有动作参数（argv）由 manifest 固定，客户端不可传可变参数。name 仅作白名单键查找。
- **请求体 ≤ 1 KiB**：body 恒为空或 `{"confirm":true}`；**不得**发送更大的 body——超限将被拒 **413** `request_too_large`（admission 前）。
- **错误码**：413 `request_too_large`（body 超 1 KiB）；404 `action_not_found` → 动作名不存在；409 `action_confirm_required` → 缺 confirm 字段；429 `action_throttled`（+ `Retry-After`）→ 动作级限频，等待后重试；503 `actions_disabled`/`action_unavailable`/`action_busy`（+ `Retry-After:2`）→ 功能禁用/spawn 失败/服务级并发满；504 `action_timeout`（+ `timeout_s`）→ 超时。

### 错误码（thin 路由统一 `{"code":"..."}`）

`400 version_required`/`version_incompatible`、`404 thin_route_not_found`（旧 sidecar，回退信号）、`404 action_not_found`、`409 action_confirm_required`、`413 request_too_large`、`429 action_throttled`+`Retry-After`、`503 actions_disabled`/`action_unavailable`/`action_busy`+`Retry-After:2`、`504 action_timeout`+`timeout_s`、`422`。

### slim-fail-open 授权（入口常驻 + 空状态 = 透明回退合规，omni 裁决 SSOT）

管理动作存在**两种"无可用动作"信号**，客户端均授权做**透明回退（fail-open）**，且**管理动作入口在客户端 UI 常驻**（不随信号有无而出现/消失）：

1. **旧 sidecar（pre-v1.3.0）**：`GET /slimapi/actions` → **404 `thin_route_not_found`**（路由不存在）。
2. **新 sidecar（v1.3.0+）但未配置 manifest**：`GET /slimapi/actions` → **200 `{"enabled":false,"actions":[]}`**（空 catalog）。

两者对客户端**等价**：入口常驻、渲染为 **disabled / 空状态**即合规（不展示管理动作列表，不报错、不崩溃）。客户端**无需（也不应）**根据信号类型条件性显示/隐藏入口——这是 **omni 裁决**，作为本节"管理动作入口是否常驻"的**单一事实源（SSOT）**，消除未来文档/实现分歧。

**不降级触发条件**（与全局 fallback 规则一致）：**仅** 404 `thin_route_not_found`（场景 1）走透明回退；**绝不**对 503（`actions_disabled` / `action_unavailable` / `action_busy`）/413/timeout/版本错误隐藏入口或清空列表——这些是临时故障，入口保持、按错误码提示/重试。

## ETag 接入（可选，推荐 — 2026-08-16）

`/slimapi/sessions`、`/slimapi/messages/{sid}`（含 `mode=merged`）、`/slimapi/agent`、`/slimapi/command` 的 200 响应带 `ETag` 头；下次请求以 `If-None-Match: <上次 ETag>` 回发，命中时服务端返回 **304**（无 body）——省下行传输体。接入建议：

1. **验证器内存保留**：只需保留上次请求的 **ETag 字符串**（<100B，可按 URL 做 per-URL 内存 LRU），**无需保留响应 body**。304 命中时继续用本地已有 body 渲染。服务端管线照常执行（ETag 不跳过抓取/投影），命中仅省下行字节。
2. **OkHttp HTTP cache 不适用**：本仓全部响应 `Cache-Control: no-store`，OkHttp `Cache` 不会存储——验证器须在**应用层自存**（OkHttp 层配了也不生效，勿依赖）。
3. **coding 固定建议**：同一 URL 的请求尽量固定同一 `Accept-Encoding`（identity 或 gzip）。验证器按 coding 派生（identity 强 tag / gzip 弱 tag），跨 coding 交叉必得保守 200（等于放弃命中）。
4. **弱比较语义**：`If-None-Match` 为 RFC 9110 弱比较（服务端忽略 `W/` 前缀），回发原样字符串即可；`*` 匹配任意当前验证器。
5. **辅助头照常读**：304 响应同样携带 `X-Next-Cursor` / `X-Complete`（值来自本次服务端管线计算），客户端分页/完整性逻辑无需因 304 特判。

## 内容指纹消费（建议随 B2 digest 驱动接入 — 2026-08-16，traffic Batch 4）

`GET /slimapi/messages/{sid}`（缺省与 `mode=merged`）每条消息含加性字段 `contentFingerprint`（`"<vN>:<sha256hex>"`）。定位：**内容级等价性证据**，与 digest 驱动的 `(updatedAt, messageId)` 双水印去重**配合**使用——水印判定「可能是同一条」时，指纹相同（同表示模式内）⟹ 内容未变可跳过重渲染；指纹不同 ⟹ 内容确实变了需刷新。

1. **仅在同一表示模式内比较**：缺省列表与 `mode=merged` 对同一消息产生不同指纹（merged splice 后输入含 full 内容），`vN` 前缀不区分模式——跨模式比较无意义，客户端按模式分桶存储。
2. **不提供单调性**：指纹非序号/revision，不得用于排序或版本先后判断；仅做等价/不等价判定。
3. **存储建议**：per-(sid, mid, mode) 保留最近一次指纹字符串即可（<80B）；与 ETag 验证器同样属应用层自存（响应均 `no-store`）。
4. **联调项指引**（详见 ocdroid 仓联合计划 `docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md` §4.3/§4.4/§7）：token idle/resync 触发策略、resync 后 reconcile 三分法（full 重拉/列表重拉/跳过）等消费侧推进规则**不在本仓冻结范围**——本仓只冻结指纹的生成与语义（确定性、终态语义、跨模式约束、降级不重算），推进规则由联合计划冻结。
5. **降级兼容**：full 抓取失败/预算不足等降级路径下 merged 指纹回落为 skeleton 期指纹（内容仍是列表级投影）；服务端 ops 关闭指纹（`OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED=false`）时字段整体消失——客户端对「无该字段」必须已有兼容（加性字段缺省处理）。

## T17 thin 路由：todo / children（加性 — 2026-08-16，traffic Batch 3）

两条新 GET 薄路由，承接此前走 catch-all 透传的等价上游调用（配合「直连退役」的上游去载）：

- `GET /slimapi/sessions/{sid}/todo` → 200 `Todo.Info[]` 裸数组 `{content,status,priority}`（近恒等投影——上游 schema 已最小，无字段被删）。
- `GET /slimapi/sessions/{sid}/children` → 200 children skeleton 裸数组（每项 `skeleton_session()` 投影，与 `/slimapi/sessions` 列表同 keep/drop：丢 `cost`/`tokens`/`location`/`subpath`，留 `id`/`parentID`/`projectID`/`title`/`agent`/`model`/`time`）。**无状态重加**：v1 的 `X-Children-Version` / `childrenVersion` / `childrenIDs[]` 等 list hints **不回归**——纯读。

### 客户端必须

- 迁移：把 todo / children 拉取从 catch-all 透传（`GET /session/{sid}/todo`、`GET /session/{sid}/children`）切到 thin 路由；旧 sidecar 404 `thin_route_not_found` 时回退透传（capability detection，同 catalog 路由模式）。
- `directory` 可选 query（与 messages 路由同语义，sidecar 转发为 `X-Opencode-Directory`）。
- 空 `[]` 响应为 identity（<64B 跳过 gzip）；非空按 `Accept-Encoding` 协商。

### 无 ETag（本批）

两路由**不在**「ETag 接入」清单内：响应无 `ETag` 头，`If-None-Match` 被忽略（恒 200）——不要为它们保存验证器（后续批次接入时另行公告）。

### 错误码（thin 路由统一 `{"code":"..."}`）

- 404 `session_not_found`（上游 404，带 `sessionID`）；502 `upstream_http_N`（其他上游 4xx）；503 `upstream_unavailable`（5xx/网络/坏 JSON/形状非法——含逐项非 dict）；413 `response_too_large`（超 `max_response_bytes`）；503 `transform_busy` + `Retry-After: 2`（池满）。

## 路由与失败策略

- thin 使用 stunnel 14097，direct 14096。
- GET circuit breaker：连续 3 次 transport/5xx 后禁 thin 5 分钟，再 half-open。
- mutation 只发一次，不因超时向 direct 重发。

## 客户端标识头（可选，加性 — 2026-07-29）

> **加性 / 向后兼容**：不传这些头，行为完全不变。不需要 bump wire 版本。给 sidecar access log 提供来源区分（哪个 app / 哪个版本 / 哪台设备发的请求），用于运维排障与省流分析。

ocdroid 可在每个请求（含 SSE）**可选**附加三个 request header：

| 头 | 含义 | 示例 |
|---|---|---|
| `X-Client-Name` | app 名 | `ocdroid` |
| `X-Client-Version` | app 版本 | `1.2.3` |
| `X-Client-Id` | 设备标识（见下生成建议） | 随机 UUID 或用户自定义名 |

### sidecar 侧处理（客户端无需关心细节，仅供理解）

- sidecar 读取后落入 access log 的 `client` / `clientVer` / `clientId` 字段（缺省 `null`）。
- **不透传给 opencode**（catch-all 反代显式剥离这三个头）。
- **设备 id 默认 hash**：sidecar 默认对 `X-Client-Id` 做 **SHA-256 截断 16 hex 字符**（`sha256(raw)[:16]`）后落盘，防日志明文直出设备标识；运维侧分析时靠 hash 稳定区分设备（同一 raw id → 同一 hash）。
- **`X-Client-Name` / `X-Client-Version` 不 hash**（需明文按版本筛选，注意可能含 PII，勿放敏感信息）。
- 校验：空/空白 → 忽略；UTF-8 字节 > 128 → 忽略（拒绝不截断）；含控制字符 → 忽略。

### 设备 id 生成与配置建议

1. **首次启动生成 UUID v4**，持久化到 Android `DataStore` / `SharedPreferences`（卸载重装会变，单用户产品可接受）。
2. **用户可在设置里自定义覆盖**（如 `mar-phone`、`tablet-2`，便于多设备命名区分）；留空则回退随机值。
3. **不传** → sidecar 落 `clientId: null`（完全可接受，向后兼容）。

### 不需要客户端做的事

- 不需要 bump wire 版本。
- 不需要校验响应中是否有这三个头的回显（sidecar 不回显）。
- 不需要保证 id 全局唯一（hash 碰撞概率极低，且仅用于日志区分）。

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
- 连接独立于控制面 `/slimapi/events`——两条连接，互不替代。**gzip 三层语义（勿笼统说「SSE 都不 gzip」）**：(1) 控制面 `/slimapi/events` 恒 identity、不 gzip；(2) 本 token stream 是 SSE 但**允许 gzip**（按 `Accept-Encoding: gzip` 协商，流式 zlib `Z_SYNC_FLUSH`，首个 SSE gzip 例外）——建议带 `Accept-Encoding: gzip`；(3) 普通 JSON/catalog 响应按 `Accept-Encoding` 内容协商。即「SSE」≠「不 gzip」：控制面 SSE 不 gzip、token 面 SSE 可 gzip。
- `Last-Event-ID` 可带但**值被忽略**，仅触发首帧 `resync{reason:"reconnect_no_replay",sessionID}`。stream **不发 SSE `id:`、无 replay buffer**——客户端不得依赖 `id:` 续传。

### streamOwned 渲染算法（必须）

收到帧按 part（`(messageID, partID)`）维护本地「streamOwned」缓冲：

- **`message.part.snapshot{done:false}`**（订阅首帧 / 握手锚点）→ **替换**该 part 本地缓冲为 `text`、标 `streamOwned=true`、未完成。
- **`message.part.delta{text}`** → 仅当该 part 已 `streamOwned` **且未完成**时 **append** `text`；否则丢弃（不应发生；若发生视为乱序，忽略）。
- **`message.part.snapshot{done:true}`**（终态）→ **仅完成 marker，无 text**——客户端**不再从该帧取 text**；标**完成**。权威全文走 `/slimapi/messages/{sid}` skeleton 列表或 `/full/{mid}` 展开（持久化真值，幂等且**凌驾**所有 token 帧）。此后该 part 不再收 delta（违反则忽略）。
- **`/slimapi/messages/{sid}` / `/full/{mid}`**：part 已 `streamOwned` 且**未完成** → **忽略**持久化拉取的该 part text（stream 为准）；part 已 `streamOwned` 且**已完成** → 仅允许 skeleton / full 覆盖（skeleton / full 是持久化真值，幂等且**凌驾**所有 token 帧）。

### truncated / 降级（必须）

- 收 **`message.part.snapshot{truncated:true}`**（`event: message.part.snapshot`，`done:false` 或 `done:true` 均可能，携带自身的 `partEventRevision`——该帧消费**自己**的 revision，严格大于该 part 上一帧，故用 strict `>` 比较的客户端可直接接受）→ 这是**单 part 级**截断（part 累计文本超 `token_stream_max_frame_bytes`≈1MiB，或 part 被服务端 drop_part）：清该 part `streamOwned`、停 append、走 `/slimapi/messages/{sid}` 重拉权威（可能被上游截断，但那是真值）。**不影响**该 session 其它 part；单 part >1MiB **不**走 resync，而是本路径。

### resync 处理（必须）— 两档恢复

收 **`resync{reason, sessionID}`** 时，**一律**先：丢弃该 sid 全部 token 渲染态（所有 streamOwned part 清空）→ `GET /slimapi/messages/{sid}` 重拉权威。是否 **重订阅** stream 按 reason 分档：

| reason | 清态 + 重拉消息 | 重订阅 stream | 说明 |
|---|---|---|---|
| `reconnect_no_replay` | 是 | **是** | 无 replay；新连接拿 handshake snapshot |
| `subscriber_backpressure` | 是 | **是** | 慢消费者被断；须重连 |
| `token_memory_limit` | 是 | **是** | 服务端 LRU 驱逐一个 LivePart 后**保持连接**、**不**对现有 sub 重发 snapshot；仅清态会让后续 delta 成 orphan → **必须**重连以 `attach_subscriber` 重建锚点 |
| `session_idle` | 是 | 否 | 上游 idle，该 sid live parts 已 retire；socket 可留 |
| `session_deleted` | 是 | 否 | 会话**终态**——服务端在发完 `resync{session_deleted}` 后**主动关闭该 token-stream 连接**（`sub.terminate` → STOP，非软 resync），客户端收 STOP 即断；清态 + 重拉权威消息后**不得**重订阅该 sid 的 stream。会话删除本身由控制面 `/events` 的 `session.digest{deleted:true}` 独立驱动，两条信号不互相替代 |
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

## 四能力接入（L1–L3 slim 整合，加性 — 2026-08-15）

> **加性 / 向后兼容**：wire 版本未 bump（仍 2）。四能力各自经 `/slimapi/health` 根级 `features` 新 flag 门控（`tokenCoalesce` / `permissionEvents` / `serverMerge` / `transformAbsorb`）；flag 缺省/为 false → 对应能力走既有路径，**零回归**。能力探测结果缓存，勿每连接重复。权威 wire 见 `docs/specs/v2-contract.md` §2 / §3 / §5。

### 1. tokenCoalesce（A）—— `/slimapi/events?tokens=1` 取代 per-session stream

- **门控 flag**：`features.tokenCoalesce === true`。
- **消费方式**：`GET /slimapi/events?tokens=1`（仅字面 `"1"` 合法，其他→400 `invalid_tokens`）。在既有策展 SSE 流内**额外**收到 lean `token` 帧 `{type:"token", sessionID, messageID, partID, delta}`（无 `event:` 头，按 `data.type=="token"` 分发；**无 `partEventRevision`、无 `directory`**）。`delta` 为 token flush loop（100ms/4KiB 窗口）对每个 `(sessionID, messageID, partID)` 完成窗口拼接的**增量 concat**，覆盖所有 session（MVP 无 per-sid 过滤）。
- **迁移约束**：
  - 与 per-session token stream（`GET /slimapi/sessions/{sid}/stream`）在**客户端互斥**——**不要**同时连两者（双份投递；服务端不强制踢）。迁移完成即可弃用 per-session stream（保留其连接=双份成本）。
  - `token` 帧是**动画层 only**：丢帧/乱序由 digest + `/messages` 兜底；权威全文/修订仍走 `/slimapi/messages/{sid}` + `/full/{mid}`（幂等覆盖，凌驾所有 token 帧）。
  - 背压：`token` 帧溢出与 per-session stream 同语义——`resync{reason:"subscriber_backpressure"}` + 断连（复用控制面 T3 守卫）。
  - 无 replay：`token` 帧无 `id:`，重连后靠 digest + `/messages` catch-up；不依赖流内续传。

### 2. permissionEvents（B）—— `/slimapi/permissions` 聚合端点

- **门控 flag**：`features.permissionEvents === true`。
- **消费方式**：冷启动/SSE 重连后补拉 pending permission 卡片用 `GET /slimapi/permissions`（跨目录聚合，questions 同款 envelope `{items, errors, authoritativeDirectories, discoveryComplete}`）；每条 item = 白名单投影 `PermissionV1.Request`（`id/sessionID/permission/patterns/metadata/always/tool?`）+ 追加 `directory`。**替换**对 catch-all `GET /permission` 的轮询（此前仅见 `process.cwd()` 实例）。
- **迁移约束**：
  - `authoritativeDirectories==null`（**errors 空 + `discoveryComplete==true` + `truncated!=true` 三项同时满足**，实现见 `src/oc_slimapi/routes/permissions.py`）→ 全局 replace-all；为数组 → 仅对所列 dir 做 replace，**不得**丢弃未覆盖 dir 的既有 pending 卡（否则用户无法批准 → session 卡死）。**截断风险**：`truncated==true`（聚合超 `_MAX_AGGREGATE_ITEMS` 或 aggregate byte cap）时 `authoritativeDirectories` **必然**为数组（succeeded 列表），**绝不为 null**——客户端判据若漏掉 `truncated` 检查，会在截断响应下误判 null 而错误执行全量 replace-all，丢弃未覆盖 dir 的 pending 卡。判据务必三项齐全。
  - 发现失败 → 503 `upstream_unavailable`（无 envelope）→ 保留既有状态并重试，**不可**据此推断"无 pending"。
  - pending permission 应答仍走 catch-all + `X-Opencode-Directory`（本端点只读聚合）。

### 3. serverMerge（C）—— `mode=merged` 删本地合并协调器

- **门控 flag**：`features.serverMerge === true`。
- **消费方式**：`GET /slimapi/messages/{sid}?mode=merged`（仅字面 `merged`，大小写敏感）。页内 `thin_placeholder_` 折叠消息由服务端按预算内联展开为 full 投影（剥 LSP diagnostics）；其余消息与 `X-Next-Cursor` 与缺省**逐字节一致**。
- **迁移约束**：
  - **`mode=full` 及任何非字面 `merged` 值静默忽略**（与缺省逐字节一致，不 400）——过渡期客户端可保持 `mode=full` 旧行为，直到确认支持 `merged` 再切换。
  - 超预算/超页数/单项失败 → 该项**渐进降级**保持 skeleton 原样（不 413、不报错）；客户端仍可按需 `/full`。
  - 与 direct `/full` 共享 single-flight：同键并发只打一次上游（≤1s join 窗口）——上游计数/access log `upIn` 只记一次，客户端勿据 `upIn` 推断逐请求上游命中。
  - 删除本地合并协调器（原 `hasFull` + N 次 `/full/{mid}` 自行展开逻辑）——`serverMerge` 开启后由服务端承担。

### 4. transformAbsorb（D）—— 删 `/full` 的 503 处理路径

- **门控 flag**：`features.transformAbsorb === true`。
- **消费方式**：无新请求/新字段。行为变化：`/full/{mid}` 的 transform 槽等待在总预算 `OC_SLIMAPI_TRANSFORM_ABSORB_BUDGET_SECONDS`（默认 2.5s）内**吸收**瞬时占用——瞬时占用（>2s 且 <2.5s）现返回 **200**（原先 503）；占用超预算才 503，**503 形状逐字节不变**（`{"code":"transform_busy","retry_after":2}` + `Retry-After: 2`）。
- **迁移约束**：
  - 客户端删除对 `/full` 场景 `transform_busy` 的**专门重试路径**（吸收后 503 只在真超预算时出现，且 `Retry-After:2` 仍是重试信号）。若已有 skeleton 的 503 兜底，确认 full 走同一兜底即可。
  - 503 时上游请求照旧不发出（admission 先于 GET）——客户端**不得**因 503 猜测"上游被打过"。

## 直连退役（目标态：ocdroid 仅经 slimapi；14096 保留至 C1/C3 前置完成）

> **背景（实证）**：ocdroid 现全部流量经 `/slimapi/**`（sidecar），4 天 access log 40853 reqs **100%** slim、0 passthrough；客户端无 slim→direct 自动回退（直连仅供 Manual 连接源 / 非 slim 模式）。直连 `:14096`（mTLS → opencode `:4096`）**目标态：不再服务 ocdroid**——但**在 ocdroid 完成下方 C1/C3 前置前，`:14096` 仍是 ocdroid 的回退路径**（`slim=false` / Manual 连接源仍可用）；前置完成后仅保留给**匿名消费方**（非 ocdroid 的其它 HTTP 客户端仍可直连，sidecar 不干预）。
>
> **本仓职责**：锁接口契约与前置条件；**实施由 ocdroid 仓库承担**（见 ocdroid 侧 `docs/slim-mode-api-routing.md` 演进）。本清单供 ocdroid 开发者对照。

### 前置条件（退役前必须完成，均为 ocdroid 侧改动）

- **C1 — 图片走 catch-all `GET /file`**：`HttpImageHolder`（消息图片加载）从直连 opencode 改为走 sidecar catch-all（带 `X-Opencode-Directory` 头，`GET /file` 经反代透传）——图片加载不再依赖 `:14096` 直连。
- **C3 — 连接自检改打 `/slimapi/health`**：`checkHealthFor`（连接/健康自检）从直连 health 改为 `GET /slimapi/health`（带 `X-Slimapi-Version: 2`；读 `sidecar.ok`/`server.api_version`/`schema.version`）——健康检查不再依赖 `:14096` 直连。

### 退役范围（L2 四能力落地后执行）

- 删除 ocdroid `slim` 开关（`slim=false` 分支）、Manual 连接源、`StreamingMode.Standard` 轴、`4097→14097` 迁移残留、`4096` 默认值。
- L2 能力落地后删除对应客户端协调器（本节四能力接入章节的迁移约束对应删减：per-session stream 客户端 / permission 轮询 / 本地合并协调器 / transform_busy 处理路径）。
- 直连保留：仅限匿名消费方（非 ocdroid）。`:14096` stunnel 端口若需下线由部署侧处理（不在本仓 wire 契约内）。
