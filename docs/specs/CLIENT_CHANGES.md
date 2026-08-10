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
