# oc-slimapi Wire Contract v2

> **This is the AUTHORITATIVE wire contract.** [`v1-contract.md`](v1-contract.md) is deprecated
> and retained for historical reference only — do **not** use it for client integration.

> **文档修订日志**
>
> | 修订日期 | wire 版本 | 文档 rev | 变更摘要 | 落地对照 |
> |---|---|---|---|---|
> | **2026-08-05** | **2（不变）** | **v2+fix** | **`GET /slimapi/questions` 发现机制 bug 修复**：发现调用从 `GET /session?limit=10000`（误以为全局，实测 per-Location）改为 `GET /project`（ProjectTable 全表，真全局）。原实现漏报所有 `workdir ≠ process.cwd()` 的 pending question。字段 `directory`→`worktree`，跳过合成 global（`worktree=="/"`）。`discoveryComplete` 由"页未满时 true"改为**恒 true**（`/project` 无分页）。envelope 字段集不变，未 bump `X-Slimapi-Version`。详见 §2。 | §2 |
> | **2026-08-05** | **2（不变）** | **v2+additive** | **加性回归 `GET /slimapi/questions`（跨目录 pending question 聚合）**：lite-v2 曾在批量清理中删除此端点，现加回为跨目录聚合端点。上游 opencode `GET /question` 是 **per-Location**（按 `X-Opencode-Directory` 路由的 workdir instance；无 header 回落 `process.cwd()`），故 `workdir ≠ process.cwd()` 的 pending question 对冷启动客户端不可见。本端点先 `GET /session?limit=1000`（无 directory header，全局存储）发现所有 directory，再对每个 distinct directory 并发 `GET /question`（带 `X-Opencode-Directory`）并合并。返回 **envelope 对象** `{items, errors, authoritativeDirectories}`（非裸数组，以表达 partial 失败）：每条 item 为上游 entry 原样 + 追加 `directory` 字段；`errors` 为 per-dir 失败（isolated，单 dir 失败不中断整体）；`authoritativeDirectories` null=全成功（client replace-all），数组=partial（仅覆盖所列 dir）。发现调用失败（网络/5xx/4xx/坏 JSON/非 list）→ 整体 **503** `upstream_unavailable`（无 envelope）。**未 bump** `X-Slimapi-Version`（仍 2）。修复 slim-mode 冷启动 pending question 不可见回归。详见 §2 + §2「`/slimapi/questions` envelope」。（**2026-08-05 v2+fix**：本行的 `GET /session?limit=…` 发现机制已替换为 `GET /project`，见上一行。） | §2 / §4 |
> | **2026-08-03** | **2（不变）** | **v2+additive** | **加回 `GET /slimapi/sessions/status`（只读透传 + turn merge）**：透传上游 `GET /session/status`（返回 `Record<SessionID, {type:"busy"\|"idle"\|"retry"}>`）+ sidecar merge 每个条目的 `turnIncarnation`/`turn`（源自 `TurnRegistry.snapshot`，与 digest SSE 同源，§3.y）。端点只读、不写、不缓存；`directory` 必填；turn_registry 未装配时两字段缺省（配对缺失）。**加性回归**（lite-v2 删除面撤回此端点），**未 bump** `X-Slimapi-Version`（仍 2）。详见 §2。 | §0 / §2 / §8 / §10 / §11.1 |
> | **2026-08-01** | **2（不变）** | **v2+additive** | **turn token fence scope 简化为「仅 sid」**：turn 计数器分桶 key 由 (serverGroupFp, sid) 改为 sid 单值（单 sidecar 单后端下 sid 已唯一）；移除 X-Ocdroid-Server-Group-Fp 输入头依赖（header 若来则忽略）；snapshot 恒返回（未观测 sid → (inc,0)），digest 的 turnIncarnation/turn 现在对所有 session.status 恒输出（只要 turn_registry 装配）；移除 access log 的 serverGroupFp 字段。免疫多设备共享 session 的 scope 翻转冻结。**未 bump** X-Slimapi-Version（digest 字段集不变，加性兼容）。详见 §3.y。 | §3 / §3.y |
> | **2026-07-29** | **2（不变）** | **v2+additive** | **流量日志持久化 + 客户端标识（加性）**：access log 文件发现规则改为按天切分 `access-YYYY-MM-DD.jsonl`；内存账本快照按天切分 `traffic-snapshot-YYYY-MM-DD.jsonl`（均 ops 面）；新增可选输入头 `X-Client-Name`/`X-Client-Version`/`X-Client-Id`（不透传上游、缺省忽略）；access log 新增 `client`/`clientVer`/`clientId` 字段（缺省 `null`）。**`X-Slimapi-Version` 不 bump**（加性输入 + ops 面文件名，非 wire 协议破坏）。详见 §7、§12。 | §7 / §12 |
> | **2026-07-28** | **2（breaking bump）** | **v2** | **Rev v2 (lite-v2)**: deleted 10+ endpoints, removed routeToken/discovery/children/Stage-B-part-tracking/Opt-A/BatchLedger; simplified `/full/{mid}` and `/messages`; digest `updatedAt` now sidecar wall-clock; version gate `(2,2)`. **Breaking wire bump** from `X-Slimapi-Version: 1` → `2`.详见下文各节。 | §0 / §1 / §2 / §3 / §5 / §6 / §7 / §11 |
>
> **基准声明**：本文件是 v2 wire 基准。实现侧加性/修复性变更在对应小节就地标注，并在本头部「文档修订日志」汇总。所有加性变更**不 bump `X-Slimapi-Version`** 除非另行说明。与 design-v2/INTERFACE_MAP 冲突时以本文件为准；后者需随后同步。
>
> **同步纪律**：本文件 changelog 条目须同时列出受影响的 `docs/specs/CLIENT_CHANGES.md` 小节。

> 状态：v2 收敛版（lite-v2）。v1 历史 rev A–M 的逐条落地对照保留在 [`v1-contract.md`](v1-contract.md)；本文只描述 v2 当前态。本文 🔒=已覆盖。

## §0 范围与架构

- 纯 HTTP sidecar：FastAPI + httpx + orjson + uvicorn **单 worker**，host ∈ `{127.0.0.1, ::1, localhost, 0.0.0.0}`。
  - **已部署稳态**：绑定 `0.0.0.0:4097`（所有接口），**用户接受**；直接 `:4097` 明文访问须经网络边界（防火墙/Tailscale ACL）阻断；外部客户端经 `:14097` mTLS 隧道（stunnel `requireCert=yes verifyChain=yes`，复用既有证书）可达。
  - **`:14097`** 为公网唯一 mTLS 入口；upstream 始终固定 `127.0.0.1:4096`（SSRF guard 不随 host 放松）。
  - **loopback-only（`127.0.0.1:4097`）** 属更严格替代姿态，非当前部署；代码允许该配置。
- **不读 opencode SQLite**；仅 legacy `/session` API；upstream 始终固定 loopback HTTP（SSRF guard 不随 host 放松）。
- v2 目标：**2-5 台同用户设备**（T3 硬化进 v2）。
- 客户端通过"切换服务器"进省流（R8：`mtls×slim` 两布尔→4 配置），非连接属性开关。
- **v2 删减面（相对 v1）**：移除 `routeToken`、discovery allowlist 数据流、children 投影缓存、Stage B 单条 fingerprint（`_part_state`/`contentRevisions`/`X-Message-Event-Seq`/304/`?known.*`）、Opt-A partial-envelope、BatchLedger，以及 10+ 依赖性端点（`/projects`、`/permissions`、`/sessions/{sid}/children`、`/messages/{sid}/since/{ts}`、q/p 写端点、`/full?ids=` 批量）。（`GET /slimapi/sessions/status` 曾在 lite-v2 删除，2026-08-03 加性回归，见 §2。`GET /slimapi/questions` 曾在 lite-v2 删除，2026-08-05 加性回归为跨目录聚合端点，见 §2。）

## §1 版本契约 🔒

- 头 `X-Slimapi-Version: <int>`，所有 `/slimapi/**` 必带。
- **门闩 `ACCEPTED_CLIENT_VERSIONS=(2,2)`**：`accepted:[2,2]` 是闭区间 `[min,max]`，当前 `min=max=2`（**仅接受整数 `2`**）。缺/非整数→400 `version_required`；越界→400 `version_incompatible`（带 `client`/`accepted`）。
- `/slimapi/health` 与 `/slimapi/ready` 返回 `sidecar.ok`（health）/ `upstream`（ready）+ `server.api_version` + `accepted_client_versions` + `schema:{degraded, version, clientMin, clientMax}`（`version`/`clientMin`/`clientMax` 从 config 读，与 `server.*` 同源；**诊断用 wire 范围回显，非 feature discovery**）。**S-E**：`health` 响应 `server` 对象可选加 `deploymentRevision` 字段（当通过 `OC_SLIMAPI_DEPLOYMENT_REVISION(_FILE)` 设置时出现；未设置时整字段省略）。
- **不再有 Opt-A 能力头**：`X-Slimapi-Capabilities` 在 v2 中**忽略**（v1 的 `mid-partial-envelope=1` 已随 Opt-A 删除而失效；客户端仍可发，sidecar 不分支）。
- bump 规则：整数，仅破坏性变更 bump；加性变更同版本。v2 → v1 是破坏性 bump（端点删除 + 字段删除 + 版本门闩收紧）。

## §2 端点

| 方法 | 路径 | 桶 | 状态 | 说明 |
|---|---|---|---|---|
| GET | `/slimapi/health` | A | 🔒 | 版本+降级+self-check；`schema:{degraded,version,clientMin,clientMax}` |
| GET | `/slimapi/ready` | A | 🔒 | liveness；同上 schema 三键 |
| GET | `/slimapi/metrics` | A | 🔒 T3 | 订阅者/queue/hub 指标；`batch` 字段恒为 `null`（BatchLedger 已移除，见 §6） |
| GET | `/slimapi/sessions` | A | 🔒 | 骨架 session 列表（`?directory/roots/limit/start/search`；`roots` 默认 **False**——客户端**应显式传** `roots=true` 以排除 subagent/task；`start` = epoch-ms **时间戳水位** `time_updated >= start`，**非 offset**，上游 legacy 不暴露前向 cursor、不保证 id tie-break）；200 加 `X-Complete` 头（见下）；每条带 `directory` 字段 |
| GET | `/slimapi/sessions/status` | A | 🔒 | **只读 status 投影**（`?directory=<必填>`）。透传上游 `GET /session/status`（`Record<SessionID, {type:"busy"\|"idle"\|"retry"}>`）+ sidecar merge 每条目的 flat 顶层 `turnIncarnation`/`turn`（源自 `TurnRegistry.snapshot`，与 digest SSE §3.y 同源；未观测 sid → `(inc,0)`；turn_registry 未装配时两字段配对缺省）。端点只读、不写、不缓存——同内存只读投影，不引入新状态机。上游非 dict body → 503 `upstream_unavailable`；上游 4xx → 502 `upstream_http_N`；5xx/网络 → 503 |
| GET | `/slimapi/questions` | A | 🔒 | **跨目录 pending question 聚合（加性回归，未 bump `X-Slimapi-Version`，仍 2）**。无参数（sidecar 自发现目录）。修复 slim-mode 冷启动看不到 `workdir ≠ process.cwd()` 的 pending question 的 bug（上游 `GET /question` per-Location）。先 `GET /project`（ProjectTable 全表，跨所有 workdir）发现 distinct worktree 集合（跳过合成 global `worktree=="/"`），再并发对每个 dir `GET /question`（带 `X-Opencode-Directory`）合并。返回 envelope `{items, errors, authoritativeDirectories}`（见 §2「`/slimapi/questions` envelope」）；每条 item = 上游 entry 原样 + `directory` 字段。发现失败 → 整体 503 `upstream_unavailable`（无 envelope）；per-dir 失败 isolated 进 `errors[]`（不中断整体）。pending question 应答仍走 catch-all + `X-Opencode-Directory` |
| GET | `/slimapi/messages/{sid}` | A | 🔒 | **骨架分页**（`?limit/before/mode/directory`）；**恒返回 skeleton 投影**——`?mode=full` 被静默忽略（不报错，仅返回 skeleton）；列表按 `time.created` **升序**；200 响应下发 **`X-Next-Cursor`** 头（opaque base64url 字符串，解析自 upstream `Link: <...?before=CURSOR>; rel="next"`），客户端用 `?before=<X-Next-Cursor>` 翻页向旧方向 drain |
| GET | `/slimapi/messages/{sid}/full/{mid}` | A | 🔒 | 单条全文（展开某条）；**v2 简化**：**恒 200** full 投影 body，**无 304**、**无 ETag**、**无 `X-Message-Event-Seq` 响应头**、**无 `?known.*` 查询参数**（Stage B fingerprint 全部移除） |
| GET | `/slimapi/events` | A | 🔒 | 实例级策展 SSE（见 §3） |
| GET | `/slimapi/sessions/{sid}/stream` | A | 🔒 | opt-in 实时 token stream SSE（见 §3.x；**gzip 默认[lever2，首个 SSE gzip 例外]**、独立 T3 账本[§6.x]、终态 done:true marker 无 text[lever1]） |
| * | `/{path}` (catch-all) | B | 🔒 | 透传 opencode（含发消息等写）；客户端发 `X-Opencode-Directory` 头过透传 |

### v2 删除的端点（相对 v1，仅供迁移参考）

以下端点在 v2 中**不存在**；客户端调用将因版本门闩返回 400（缺/坏版本头）或 404 `thin_route_not_found`（合法版本头但无对应路由 catch-all 拒绝），不再是 slimapi 一等公民：

- `GET /slimapi/projects` —— discovery/allowlist 展示端点删除（allowlist 数据流整体下线）。
- ~~`GET /slimapi/questions`~~、`GET /slimapi/permissions` —— 跨目录聚合 pending 端点删除。SSE 仍直推 `question.asked` / `permission.asked` 等事件（见 §3），客户端通过 catch-all 反代应答上游 opencode。**`GET /slimapi/questions` 于 2026-08-05 加性回归**（见 §2 端点表 + §2「`/slimapi/questions` envelope」）：现为跨目录 pending question 聚合端点（fan-out 各 directory 的 `GET /question` 并合并），修复 slim-mode 冷启动看不到非 `process.cwd()` 目录 pending question 的 bug；`X-Slimapi-Version` 仍 `2`（未 bump，加性）。`GET /slimapi/permissions` 仍删除。
- ~~`GET /slimapi/sessions/status`、`GET /slimapi/sessions/{sid}/status`~~ —— 批量 status 端点 **2026-08-03 加性回归**（见 §2 端点表）。`GET /slimapi/sessions/{sid}/status`（per-session discover+status）仍删除。
- `GET /slimapi/sessions/{sid}/children` —— children 投影端点删除（含 `X-Children-Version` 头、`childrenIDs[]` / `childrenComplete` list hint 全部移除）。
- `GET /slimapi/messages/{sid}/since/{ts}` —— watermark 增量过滤端点删除；客户端改用 `GET /slimapi/messages/{sid}` + `?before` cursor drain（见 §5）。
- `POST /slimapi/questions/{qid}/reply`、`POST /slimapi/questions/{qid}/reject`、`POST /slimapi/sessions/{sid}/permissions/{pid}` —— routeToken 写端点删除（routeToken 整体下线）；q/p 应答改走 catch-all + `X-Opencode-Directory` 透传上游 opencode。
- `GET /slimapi/messages/{sid}/full?ids=` —— G6 批量展开端点删除；客户端需 N 次 `GET /full/{mid}` 自行展开。

### 写路径（B2）🔒

- **所有写操作**（发消息、abort、q/p reply/reject、permission resolve 等）走 catch-all 反代，客户端自带 `X-Opencode-Directory` 头（现有 `DirectoryHeaderInterceptor`），slimapi 不剥（非 hop-by-hop）。
- **routeToken 在 v2 中不存在**：v1 的 `/slimapi/questions` / `/permissions` 聚合响应不再下发 `routeToken`；v1 的 `invalid_route_token` 错误码已删除（见 §7）。客户端 q/p 应答直接 POST 上游 opencode legacy URL（经 catch-all）。
- q/p SSE 事件仍推送（见 §3），但**仅作观察信号**——具体应答动作不经 slimapi 专门端点。

### `/slimapi/sessions` 完整性头

200 响应（仅成功路径）加响应头；502/503 等错误路径**不**发：

| 头 | 语义 |
|---|---|
| `X-Complete` | `"true"` 当且仅当 `len(sessions) < limit`（**本页未满**）。**强制语言**：客户端**不得**据此判定「权威全集」「权威空」「覆盖完整性」或「结束冷启动」——上游 legacy `/session` 无 total/快照、`start` 为时间戳水位、无前向 cursor。`false` 仅表示「≥ limit 条匹配，可能截断；可提高 `limit` 或收窄 `start` 复查」。 |

> **v2 删除**：v1 的 `X-Discovery-Directories`、`X-Discovery-Ready` 头已移除（discovery 数据流下线）。客户端**不应**再消费这两个头；旧客户端读取时获得 `undefined` 即可（无副作用）。

- 上游 body 非 list（dict/string/null 等）→ 503 `upstream_unavailable`（与坏 JSON 同路径），不发 `X-Complete`。
- `start` 参数：epoch-ms 时间戳水位（上游 `time_updated >= start`），**非 offset**；默认不传 = 全部；**不**支持前向分页 cursor（上游 legacy HTTP 不暴露）。
- `roots` 默认 **False**（不翻）；客户端**应显式传** `roots=true` 以排除 subagent/task 子会话。

### `/slimapi/questions` envelope（加性，2026-08-05；未 bump `X-Slimapi-Version`，仍 2）

`GET /slimapi/questions` 返回一个 **envelope 对象**（非裸数组），以表达 partial 失败。形状：

```json
{
  "items": [ {"id":"que_…","sessionID":"01H…","questions":[…],"tool":{…},"directory":"/some/dir"} ],
  "errors": [ {"directory":"/failed/dir","code":"upstream_http_500"} ],
  "authoritativeDirectories": null | ["/succeeded/dir", ...],
  "discoveryComplete": true | false
}
```

- **`items`**：来自**所有已发现** directory 的合并 question entry。每条 entry 为上游 `/question` entry **原样**（字段 `id` / `sessionID` / `questions` / `tool` 顺序保留）**再追加 `directory` 字段**（值为该 entry 来源 directory）。
- **`errors`**：per-directory 失败。每条 `{directory, code, message?}`。`code` 遵循 §7：网络/5xx/非 list body → `upstream_unavailable`；4xx（含 unlikely 404）→ `upstream_http_N`。**单个 directory 失败绝不中断整体请求**（isolated）。
- **`authoritativeDirectories`**（客户端契约依赖）：
  - **`null`** 当 `errors` 为空 **且** `discoveryComplete == true` → **全成功 + 发现完整，全局权威**：客户端用 **replace-all 语义**（用本次 `items` 完整替换本地 pending question 集合；不在本次 `items` 中的旧 question 应视为已不在 pending）。
  - **目录数组**（仅成功 directory，first-seen 保序）当 `errors` 非空 → **partial**（注：`discoveryComplete` 现恒 `true`，见下——`/project` 无分页，故 partial 只由 per-dir `/question` 失败触发，不再有截断驱动）：客户端**仅可**对数组中所列 directory 做 replace（丢弃这些 directory 中不在 `items` 的旧 question）；**不得**对未列出的 directory 做 replace（它们的既有 pending question 必须保留——这些是 per-dir `/question` 调用失败的 directory，sidecar 未取得其当前 pending 集合，错误地 replace-all 会丢失它们的 pending question → **数据丢失**）。
- **`discoveryComplete`**（加性诊断，客户端可忽略 if absent-aware）：`true`（恒）—— `/project` 返回 ProjectTable 全表，无分页/截断概念。字段保留为加性诊断，客户端可忽略 if absent-aware。

**边界**：
- 无任何 project（`/project` 返 `[]`）→ envelope `{items:[], errors:[], authoritativeDirectories:null, discoveryComplete:true}`（**权威空**：无 workdir 即无 pending question；replace-all 安全）。
- **total failure**（发现调用 `GET /project` 失败：网络/5xx/4xx/坏 JSON/非 list）→ HTTP **503** `{"code":"upstream_unavailable"}`（**无 envelope**；客户端不可据此推断 pending question 集合，应保留既有状态并重试）。
- pending question 的**应答动作**不经本端点（仍走 catch-all + `X-Opencode-Directory`，见 §2 写路径）；本端点只做读聚合。
- **不读** opencode SQLite；上游 `/question` 是 legacy `:4096` server 挂载路径（per-Location）。

## §3 SSE 契约 🔒

- 上游：**一条** `/global/event`（进程级 GlobalBus，全实例跨目录，每事件自带 `directory`）。
- 帧：
  - `session.digest`（debounce 250ms/session，仅发有变化的字段）：`{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?, turnIncarnation?, turn?}`。
    - **`updatedAt`（v2 语义变更）**：sidecar **wall-clock 时间戳**（epoch ms），由 digest 发射时刻确定，**不再是**上游 `info.time.updated`。理由：v1.18.x 的 message 级 `info.time.updated` 不可靠（多数情况回落到 `created`），改用 sidecar wall-clock 让 digest 时间戳真实反映「sidecar 看到变化的那一刻」。
    - **跨窗口严格单调性不保证**：同一 session 的两次 digest `updatedAt` 不保证 strict `>`（debounce 窗口合并、时钟分辨率、sidecar 进程重启、NTP 跳变等都可能让 `updatedAt` 相等或回退）。客户端 watermark 比较必须用 **`(updatedAt, messageID)` 二元组字典序**（见 §5），**且**对 `updatedAt` 回退/相等做幂等处理（同 messageID 不重复拉取；时间回退时不删除已有数据，仅作 reconcile）。
    - `status`←`session.status`(idle/busy)；`messageID`←`message.updated`/`message.appended` 的 `info.id`（取最新）；**`archived`**←`session.updated` 的 `info.time.archived`（有值→epoch-ms 时间戳）；`deleted`←`session.deleted`。
    - **`turnIncarnation?` / `turn?`（turn-token fence，配对出现/缺失）**：sidecar 派生的**服务端因果标识**，供 ocdroid 做 lex 单调 fence（丢弃旧 incarnation / 旧 turn 的过期 digest）。两字段位于 digest data payload 的 **flat 顶层**（与 `sessionID`/`status`/`archived`/`deleted`/`lastError` 同层），**不**嵌套进 `properties` 子对象。配对规则：两者都非 None → 同时输出；任一为 None → **两者都不输出**（ocdroid 降级 Tier-2）。类型为 JSON integer ≥0。**只要 turn_registry 装配（lifespan 级部署配置）就对所有 `session.status` 事件恒输出**（未观测 sid → `(inc, 0)`）；turn_registry 未装配时两字段缺省。完整语义见 §3.y。
    - **`lastError`（G1-A，v2 保留）**←`session.error` 经脱敏后的 `{name,message,at}`（`at`=sidecar 收到时 epoch-ms）。**三态 wire**（与 sticky 共存，互不矛盾）：
      - **对象** `{name,message,at}`：本窗口新 error，或 flush 时该 sid 仍有 sticky。
      - **显式 `null`**：clear 帧——该 session 出现新 `status=busy` 时 pop sticky 并立即 flush。
      - **省略**：本 digest 没有本窗口新 error 对象、也没有显式 clear（`null`），**且** 该 sid 当前不存在 sticky error；`deleted=true` 的 digest **强制省略**（pop sticky，**不**发 null）。
      - abort（`error.name=="MessageAbortedError"`）静默丢弃（不写 lastError、不发 G1-B 帧）。
      - 脱敏：`message` 取首行→剥绝对路径→剥 stack frame→剥 secret→截断 ≤512；缺失回落 `name` 或 `"(no detail)"`；`name` 截断 ≤128。
    - **v2 字段删除**：`childrenVersion?`、`contentRevisions?` 已移除（children 投影缓存与 Stage B fingerprint 全部下线）。客户端**不应**再消费这两个字段。
  - `session.error`（G1-B，**无** `sessionID` 时立即直推，不走 debounce）：`{directory?, name, message, at}`。abort（`MessageAbortedError`）静默丢弃。有 sid 的 `session.error` **不**走本帧，走 digest `lastError`（G1-A 立即 flush）。
  - **q/p 阻塞信号（完整枚举 6 个帧名）**：`question.asked`、`question.v2.asked`、`permission.asked`、`permission.resolved`、`permission.v2.asked`、`permission.v2.resolved`——**立即直推** `{directory, type, properties}`。事件名作为 data payload 的 **`type` 字段值**下发（**无** SSE `event:` 字段；`type` 字段值取上游 opencode 事件名 **verbatim**，sidecar 不重命名/不映射，见 `hub_types.IMMEDIATE` + `global_hub.publish` 直通路径）。**客户端必须同时处理两种形式**（不可只订阅其中一种）：
    - **legacy**：`type=="question.asked"` / `type=="permission.asked"` / `type=="permission.resolved"`；
    - **v2 namespaced**：`type=="question.v2.asked"` / `type=="permission.v2.asked"` / `type=="permission.v2.resolved"`。
    - 当前 upstream opencode (v1.18.x) 主要下发 **namespaced** 形式，但 legacy 形式仍在 sidecar 识别集合中（二者均触发立即直推）；客户端按 `type` 字段值分发，两种形式都必须接收。
    - **注意**：v2 删除了 q/p 写端点与 routeToken，但 SSE 仍直推这些事件作观察信号；客户端应答 q/p 走 catch-all + `X-Opencode-Directory`（见 §2 写路径）。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}` / 背压 `{"reason":"subscriber_backpressure"}`，无 replay）。
- **v2 删除的帧**：
  - `server.reconfigured`（v1 rev F）已移除——其触发条件 `load_products` 成功 + allowlist 集合变化在 v2 中不再发生（discovery 数据流下线）。
  - Stage B v0.4 hub 事件路由（`message.part.updated` → `_part_state` + `contentRevisions`；`message.part.removed`；`message.removed` 触发 digest）已移除——`_part_state` / pending `contentRevisions` 全部下线；这些上游事件不再触发 digest（digest 仅由 `session.*` / `message.updated` / `message.appended` 驱动）。
- 丢弃：`?stream`、text.delta（`message.part.delta` 仅路由到 token stream hub，**不**进 digest）、`tool.*`、`sessionId` 参数、per-directory hub。
- **连接建立期 coalescing**：带 `Last-Event-ID` 重连时，同连接可能先收 `resync{reconnect_no_replay}` 再收队列内 `server.connected`（既有行为）。客户端 **SHOULD** 对同一 SSE 连接建立期的 cold-start 触发帧做 once-latch coalescing（至多一次 reconcile；reconcile 幂等）。
- **heartbeat ≠ 上游健康**：`server.heartbeat` 仅证 sidecar + 订阅连接存活；上游 outage 探测委托 `GET /slimapi/ready` 或自然 fetch/write 失败。sidecar 进程重启 = 连接断开；客户端重连收 `server.connected`，**应**视为 cold-start 触发。

## §3.x Token stream SSE（opt-in 实时流）

> **状态**：v2 保留 token stream（与 v1 rev J 一致），**不 bump** 内部子版本。设计权威 `docs/specs/design-token-stream.md` v4。客户端能力探测：`GET /slimapi/health` 根级 `features.tokenStream===true`；缺/404/405 → 降级既有「完成后整条出现」（`/messages/{sid}` 拉权威全文），**零回归**。

### §3.x.1 端点

- `GET /slimapi/sessions/{sid}/stream?directory=<optional>`；`text/event-stream`；响应头 `Cache-Control:no-cache,no-transform`、`X-Accel-Buffering:no`、`X-Slimapi-Subscriber-ID:<ephemeral>`。
- `/slimapi/**` 版本门禁复用 `SlimapiVersionMiddleware`（v2 `X-Slimapi-Version:2` 必带）；无 route-level `Depends`。
- `directory` 可选 query；仅校验 query 与 `X-Opencode-Directory` 头冲突（trailing-slash 归一后不等）→ 400 `directory_not_allowed`（NB-D7 结构性守卫，与 messages 路由镜像）；directory **本身对 token-stream fanout 是 no-op**——累加器以 sessionID 为键（单用户 T3 全局唯一），directory 不改变订阅者接收的帧集。**不开第二条上游连接**；sid 全局唯一、directory 无关（单用户 T3）。路由注册在 catch-all 反代之前；不遮蔽 `/{sid}/stream`。
- **opt-in**：客户端前台/动画层才连；切背景/换 session 应断开（详见 §6.x token T3 信封「同时最多 1 条前台 stream」）。连接独立于控制面 `/slimapi/events`——两条连接，互不替代。
- **P1 范围**：仅 text part（reasoning / tool-input 延后 P2+）；不做二进制流。

### §3.x.2 Wire 帧

```
# 1) 订阅首帧：活跃 part 累计全文锚点
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<累计全文>","done":false}

# 2) 批式增量（100ms / 4KiB flush；§5.4 design）
event: message.part.delta
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<本窗拼接>"}

# 3) 终态 marker（杠杆1：去终态全文——仅完成标记，无 text；权威全文走 /messages/{sid} 或 /full/{mid}）
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

- **不发 SSE `id:` 字段**；`Last-Event-ID` 仅触发首帧 `resync{reconnect_no_replay,sessionID}`，**值忽略**。
- **`event: message.removed`（token-stream 保留，非控制面）**：v2 删除了控制面 Stage B part-tracking（`_part_state`/`contentRevisions`/fingerprint），但 token-stream 的 `message.removed` 帧**保留**——属 token-stream 动画层协议，与控制面 part-tracking 是两回事。token hub 收到上游 `message.removed` 后（`TokenStreamHub.on_message_removed`，hub.py line 639）：(1) 清理该 message 的累加器/修订状态；(2) 向当前订阅者 fan-out `message.removed{sessionID,messageID}` 帧；(3) 记入有界回放队列（cap 1000 / TTL 24h）。客户端在**握手期**可能收到回放的 `message.removed` 帧（`server.connected` → tombstones 回放 → snapshot → fanout），在运行时也可能收到实时 fan-out 帧。收到后应丢弃该 message 的 live 渲染态（streamOwned）。控制面 `session.digest` 的 `deleted=true` 是独立信号，二者共存、不替代。队列不受 `resync_all` / `on_upstream_reconnect` 影响。
- **终态顺序不变式（wire 强约束）**：对同一 `(sid,mid,pid)`，所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不许再发 delta。
- **杠杆1（决定性）**：终态 `snapshot{done:true}` 是**仅完成 marker，不带 text**——取消上游 `part.text` 终态重发。**权威全文走 `/messages/{sid}` + `/full/{mid}`**（持久化真值）；token stream 是动画层，`/messages/{sid}` / `/full/{mid}` 幂等覆盖且凌驾所有 token 帧。客户端可接受 digest 完成先于/晚于 token 终态帧。
- **resync reasons**（token 流均带 `sessionID`）：`reconnect_no_replay`（上游重连）/ `subscriber_backpressure`（订阅者 T3 溢出）/ `token_memory_limit`（全局累加器上限）/ `session_idle`（生成结束清理）/ `session_deleted`（会话被删除）。**单 part >1MiB 不走 resync**，而是 `message.part.snapshot{truncated:true}`（见上）——客户端清该 part streamOwned、走 `/messages/{sid}` 或 `/full/{mid}`。
- **truncated 处理**：收 `snapshot{truncated:true}`（done:false 或 done:true 均可能）→ 客户端清该 part streamOwned、停 append、走 `/full/{mid}`。
- **reasoning/tool part**（`part.type!="text"`）的 delta **静默 drop+计数**（C3），不 resync；field≠"text" 的 delta 丢弃。
- **per-frame `partEventRevision`（v2 简化保留）**：token stream 帧的 `partEventRevision` 由 token hub **per-frame** 维护（每帧唯一递增）；客户端按 strict `>` 去重。v1 Stage B v0.6 的 per-part revision 独立计数器（`TokenStreamHub._part_revisions`）保留作 token hub 内部去重机制；**wire `partEventRevision` 字段名不变**。

### §3.x.3 gzip（杠杆2 — 首个 SSE gzip 例外）

- token stream **默认 gzip**（流式 zlib `Z_SYNC_FLUSH`，`Content-Encoding: gzip`）；按 `Accept-Encoding` 协商。
- **首个 SSE gzip 例外**：此前「SSE 永不 gzip」（§9 + §1 [0.1.0]）的唯一破例；控制面 `/slimapi/events` **仍不 gzip**。
- **实测性能**（详见 `docs/specs/design-token-stream.md` §11，harness `scripts/measure_token_overhead.py`，12 trace、30 tok/s × 100ms）：原批式 ~12x 开销 → 杠杆1+2 后 gzip 中位 **1.47x**（**达成 re-anchor ~1.5x 中位目标**；1/3 trace <1.0x）。残余 ~0.3x（短消息/低冗余内容）记 Stage E 可选调参（flush 窗 100→200ms、gzip flush cadence、level），post-release。

### §3.x.4 与控制面 / `/messages` 的关系

- 控制面 `/slimapi/events`（§3）**一行不改**——token 流消费上游 `message.part.delta`/`updated`（控制面此前丢弃），与控制面队列隔离（独立 T3 账本，§6.x）。
- part/message 完成仍走既有路径：`message.updated`(step-finish) → digest → 客户端 `/messages/{sid}` 或 `/full/{mid}` 拉权威全文。
- token stream `snapshot{done:true}` 是「流视角完成」；digest + `/messages/{sid}` / `/full/{mid}` 是「持久化真值」。不一致以后者为准（幂等覆盖，凌驾所有 token 帧）。

## §3.y Turn token fence（服务端因果标识，未 bump `X-Slimapi-Version`，仍 `2`）🔒

> **跨项目 SSOT**：完整因果语义、术语、不变量、消费侧 lex 比较规则见 **`ocdroid/docs/2026-07-31-oc-slimapi-turn-token-contract.md`**（双方共同阅读的权威契约）。本节记录 **sidecar 侧的 wire 行为**（发什么字段、何时 stamp），与该 SSOT 一致。

sidecar 作为 ocdroid 与 opencode 之间的 Python 中继层，可观察所有 `POST /session/{sid}/prompt` / `/abort` forward 流量，因此是「轮次起点」的权威观察者。它派生服务端因果标识 `(turnIncarnation, turn)`，stamp 进转发的 `session.digest`（§3），供 ocdroid 做 lexicographic 严格单调 fence（丢弃旧 incarnation / 旧 turn 的过期 digest）。**加性 / 向后兼容**：digest 字段集不变，从「有时输出」变「恒输出」，对 ocdroid 处理 present/absent 两种情况的客户端加性兼容；turn_registry 未装配（lifespan 级部署配置缺失）时两字段缺省 → ocdroid 降级 Tier-2 启发式确认门（系统正常工作）。

### §3.y.1 wire 字段（digest data flat 顶层）

| 字段 | 类型 | 语义 | 缺失语义 |
|---|---|---|---|
| `turnIncarnation` | integer ≥0 | sidecar **生命周期 epoch**（进程级，per-instance）。启动时 `persisted_last + 1`（持久化跨 restart 单调）；进程生命周期内恒定。 | 视为「无 incarnation 信息」→ ocdroid 该事件不参与 inc fence（降级 Tier-2） |
| `turn` | integer ≥0 | per-`sid` 单调计数（0 起，bump-before-send，§3.y.3）。**事件 ingest 时快照**进 digest entry（ingest 后、flush 前的 bump 不回溯改已 stamp 的值）。 | 视为「无 turn 信息」→ 不参与 turn fence（降级 Tier-2） |

- **配对规则**：两字段**必须同时出现或同时缺失**。ocdroid 仅当两者都非 null 时构造 `ServerRound(inc, turn)`；任一缺失 → `serverRound=null` → Tier-2。
- **位于 digest data payload 的 flat 顶层**（与 `sessionID`/`status`/`archived`/`deleted`/`lastError` 同层），**不**嵌套进子对象。（ocdroid 把整个 digest data 当作 `properties` 消费，故 flat 顶层 = ocdroid 可读。）
- `updatedAt` 排序 tie-break 不受影响（`(updatedAt, messageID)` 二元组仍是 digest 排序键）。

### §3.y.2 scope = sid（无输入头依赖）

- **scope key = `sid`（字符串）**。单 sidecar + 单 opencode 后端下 `sid` 全局唯一，turn 计数器按 `sid` 分桶即可，**不需要任何身份头**。
- sidecar **不再读取** `X-Ocdroid-Server-Group-Fp` header。客户端若仍发来该 header，sidecar 直接忽略（不读、不记、不再落 access log 字段）。catch-all 反代会把它当作普通未知 header 透传上游（不在 hop-by-hop / client-ident 剥离集内），但 sidecar 自身的 turn 逻辑与它无关。

### §3.y.3 turn 计数（bump-before-send commit point，S2）

- **何时 bump**：catch-all 反代在构造 upstream request 之后、`await client.send()` **之前**，对两类 forward bump turn（`turn = prev + 1`，per-`sid` 单调不减）：
  - `POST /session/{sid}/prompt`（含 `_async` 变体）—— 新执行轮次起点；
  - `POST /session/{sid}/abort` —— abort 也是一次轮次（产生自己的 turn 号）。
- **连接级失败 = hole**：若 `send()` 抛异常（连接级失败）或上游返回非 2xx，turn **已 bump 但无 upstream 工作**——产生 **hole**（不回退 / 不 decrement）。这是对「不 increment on failure」的**已批准放宽**：httpx stream 单 await 栈下，bump-before-send 是唯一保证 in-flight fence 正确的做法；ocdroid 的 lex 比较天然容忍 hole（hole 事件携带更高 turn，DROP 的是旧 turn，正确性不破）。
- **其它带 sid 的 session 请求**（如 `GET /session/{sid}/message`）**不 bump**（也不注册任何 scope——scope 概念已随 serverGroupFp 一并移除）。

### §3.y.4 incarnation（持久化 epoch，策略 A）

- 启动时 `IncarnationStore` 从 state dir（复用 `OC_SLIMAPI_ACCESS_LOG_DIR`）下的 `incarnation` 文件 read persisted → `+1` → write 回 → 返回新值。单进程单事件循环，无文件锁。
- 文件不存在（首次启动）= `persisted_last=0` → inc=1；损坏/不可写 → best-effort 兜底返回 1（warn 不 crash lifespan）。
- 进程生命周期内恒定；restart → inc bump（新值严格大于旧进程的 inc，故旧进程 stamp 的 digest 在 ocdroid 侧 `inc < knownIncarnation` → DROP，不复活）。
- **turn registry 不持久化**（restart 归零），由 incarnation bump 兜底正确性。

### §3.y.5 stamp 时机（ingest-time snapshot，S9）

- `GlobalHub.publish()` 在 `session.status` 事件 **ingest 时**（而非 flush 时）把当前 `(incarnation, turn)` 快照 stamp 到 `DigestFields` entry 上。entry 存 Python int（值拷贝），故 ingest 后、flush 前若有新 forward bump turn，已 stamp 的 entry **不受影响**（冻结于 ingest 时刻值）。
- **gate = 「turn_registry 是否装配」**（lifespan 级，部署配置），不再是「header 是否存在」。`global_hub.publish` 里 `if self._turn_registry is not None` 这层保留；snapshot 恒返回 `(incarnation, turn)` 元组（未观测 sid → `(inc, 0)`），故只要 registry 装配，digest 对所有 `session.status` 事件恒输出两字段。
- **已知上限竞态（前瞻性披露，接受）**：前一轮 forward 的**迟到** SSE 事件到达 GlobalHub 时，registry 已被后续 forward increment → 读到更高 turn → 被过 stamp 成新轮次。这在快速连续 forward（prompt→abort）+ SSE 延迟下可达，**仅靠 per-sid 单调计数器无法闭合**。本契约**接受**此限制：ocdroid lex fence 对「过 stamp 的高 turn」不 DROP（该事件因果上属前一轮，ocdroid 表现为「过早 busy」而非 stale 复活，liveness 不破）。详见跨项目 SSOT §4.4 / 开放问题 O6。

### §3.y.6 单实例语义（O3）+ 无锁

- 无 `instanceFp`；scope key = `sid`。单进程 / 单 asyncio 事件循环，`TurnRegistry` 所有方法为同步纯 dict 操作，无需锁（跨项目 SSOT §7.2 单调可见性不变量）。
- **免疫多设备共享 session**：所有设备对同一 `sid` 的 prompt 共享同一单调计数器，读请求不翻转 scope（原 `(serverGroupFp, sid)` 分桶 + `register_scope` 的「最后写入者覆盖」会在多设备续看同一会话时翻转 scope、破坏 turn 单调性，导致 ocdroid 误判后续 digest 为 stale 而 DROP → session UI 冻结；sid-only 计数从根上消除该 bug）。

## §4 冷启动 & resync 🔒

- **sidecar 启动暖机**：lifespan 在 smoke 后 best-effort 健康检查 upstream（`/ready` 路径用）；失败仅吞错，不阻断启动。
- **v2 删除**：v1 的 `warm_allowlist(app)` / `load_products` 启动暖机已移除（allowlist 数据流下线）；不再有 discovery-related 启动暖机路径。
- **客户端冷启动顺序**：
  1. `GET /slimapi/sessions`（`directory` null OK，不过滤；消费 `X-Complete` 头）；
  2. 当前打开 ses：`GET /slimapi/messages/{sid}`（拉骨架初始集；按 `time.created` 升序）。
- 之后 SSE 接力增量（含 `resync` / `server.connected` → 复用冷启动）。
- **resync / server.connected = 复用冷启动流程**（同一"加载初始状态"代码路径；幂等）。
- **v2 删除**：v1 的 `GET /slimapi/projects`（步骤 1 可选显式刷新 allowlist）与 `GET /slimapi/permissions`（步骤 3）已从冷启动顺序中移除——这些端点在 v2 中不存在。客户端如需 q/p 快照，通过 SSE 订阅后实时推流获取（§3 的 6 个 q/p 帧在连接建立后仍直推）；**SSE 无 backlog replay**——订阅之前已到达的 pending q/p 不会重放。
- **pending question 跨目录冷启动补拉（2026-08-05 加性）**：`GET /slimapi/questions`（§2）已加性回归为**跨目录聚合端点**，用于冷启动/SSE 重连后补拉**所有 directory** 的 pending question（每条带 `directory` 字段）。客户端冷启动顺序可在步骤 2 后加：`GET /slimapi/questions` → 按 envelope 语义（§2「`/slimapi/questions` envelope」）做 replace-all（`authoritativeDirectories==null`）或 partial replace。该端点修复 slim-mode 冷启动看不到 `workdir ≠ process.cwd()` 目录 pending question 的 bug（上游 `GET /question` per-Location；sidecar fan-out 各 dir 合并）。permission 无等价聚合端点。
- **pending q/p 主动补拉示例（catch-all 透传上游 legacy）**：SSE 无 backlog；冷启动或重连后若需补拉 pending q/p，经 catch-all 反代（§2 `/{path}`）请求上游 opencode legacy session 端点：
  ```
  GET /session/{sid}/question        # 请求头带 X-Opencode-Directory: <dir>
  GET /session/{sid}/permission      # 请求头带 X-Opencode-Directory: <dir>
  ```
  这些是 **opencode legacy `/session/**` 端点**，经 sidecar catch-all **透传 verbatim**——slimapi 不解析、不聚合、不记忆 q/p 状态，响应体即上游 opencode 原样返回（ocdroid D-wire 已验证可用）。客户端按上游 legacy schema 解析；sid 经 §11.1 合法渠道（list / SSE）获知。

## §5 拉消息 🔒

- digest 推 `{messageID, updatedAt}`；客户端记本地该 ses 的 **watermark = `(updatedAt, messageID)` 二元组**（见下 tie-break）。
- 比对发现更新 → **拉 `GET /slimapi/messages/{sid}`**（skeleton；按 `time.created` 升序），客户端按 messageID 去重边界。
  - **v2 删除**：v1 的 `GET /slimapi/messages/{sid}/since/{ts}` 端点不存在；客户端**不再**做 watermark-时间戳增量过滤，而是直接 refetch `/messages/{sid}` 骨架列表（受 `?limit` 分页约束），客户端自行按 messageID 去重合并。
  - **updatedAt 语义变更（§3）**：`updatedAt` 现为 sidecar wall-clock；客户端比对时**不得**假定 strict 单调（见 §3 跨窗口单调性说明）。
- **等时间戳 tie-break**（opencode 不保证 per-session `time` 严格单调——同毫秒批量 = 同时间戳；上游 `orderBy(desc(time_created), desc(id))` 显式以 `id` 为次键）：客户端 watermark 必须用 **`(updatedAt, messageID)` 二元组字典序**比较：
  1. 先 strict 比 `updatedAt`（严格 `>` 才推进时间维）；时间相等或回退→不推进时间维，但**不删除**已有消息（幂等）；
  2. 时间相等时 strict 比 `messageID`（`MessageID = msg_+ascending()` 单调递增、字典序可排序 → 新消息 id 字典序更大 → 严格 `>` 才推进 id 维）。
  - 此规则与上游 `(time_created DESC, id DESC)` + cursor `older()` 全序**完全对齐**，复用上游单调 `MessageID` 作天然 tie-break，零契约创新。
- **分页**：`?limit` + `?before` 游标（向旧方向 drain）。200 响应带 `X-Next-Cursor` 头（opaque base64url 字符串，解析自 upstream `Link: <...?before=CURSOR>; rel="next"`）；客户端用 `?before=<X-Next-Cursor>` 翻向更旧页。无 `X-Next-Cursor` 头 = 当前页已是最后结果。
- **v2 删除**：v1 的 `X-Since-Complete` 头（专配 `/since` 端点）已移除（`/since` 端点不存在）。
- **初始拉取推荐 cursor drain（`?before` 游标分页）**（v1 rev C 裁定延续）：focus digest + resync 统一走 `/messages/{sid}` cursor drain 共享 reconciler。
- 全文：单条 `GET /slimapi/messages/{sid}/full/{mid}`（**恒 200，无 304/ETag/`X-Message-Event-Seq`/`?known.*`**，见 §2）。
  - **v2 删除**：v1 的 G6 批量 `GET /slimapi/messages/{sid}/full?ids=` 端点不存在；客户端需 N 次单条 `/full/{mid}` 自行展开。
- **partId 稳定性（v1 rev F ratify，v2 保留）**：schema-valid 的 `MessageWithParts` 下，thin skeleton（`mode=skeleton`，v2 中 `mode=full` 被静默忽略）经 `_pick(part, PART_IDS)` 保留每个 part 的真实 `id`，与 `/full/{mid}` 中的 part `id` **跨端点稳定**。sidecar **不**校验缺失/坏 shape id（仅复制存在的字段）。
- **placeholder（v1 rev F，v2 保留）**：无可渲染 part 时 thin 仍注入合成 part `id=thin_placeholder_{messageID}`、`type=text`、`text="[内容已折叠，点开查看]"`、`hasFull:true`、`omitted:["parts"]`。该 id **不参与** `/full` 的 `messageId+partId` 对齐；客户端展开 `/full` 后应**整体替换该 message 的 parts**（判定：`partId.startsWith("thin_placeholder_")` → message-level replace，禁止按 placeholder id 做 part-level lookup）。
- **阈值化 skeleton（v2 保留；默认常开，单用户产品无 opt-in）**：tool/patch 的 `state.output`/`state.error` 不再无条件剥离——按 **JSON 字节**（`orjson.dumps` 序列化长度 = 上线字节）阈值化：per-field ≤ 4 KiB **且** 该 message 累计内联 ≤ 16 KiB → **原样内联**进 thin state；超任一阈值 → **整字段 omit**（**绝不半截断**）+ `omitted`，可经 `/full/{mid}` 取回完整值。`state.structured/result/raw/attachments` **始终 omit**。**`hasFull` 仅当该 part 仍有 omitted 字段才置 `true`**。env 调参 `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES`/`_MAX_MESSAGE_BYTES`（不改契约）；外层仍受 `max_response_bytes` 约束。`/slimapi/health` 加性 `features.thresholdedSkeleton=true` + `skeletonInlineOutputMaxBytes=4096`（**仅诊断**）。字段表见 `design-v2.md` §2。

## §6 资源限制（T3，C2=2-5 台进 v2）🔒

- `MAX_SUBSCRIBERS_PER_DIRECTORY=8`、`MAX_TOTAL_SUBSCRIBERS=16`。
- 每 subscriber buffer `2 MiB`、单帧 `256 KiB`；溢出→**立即清 queue/deltas/dirty** + 排 `resync{reason:subscriber_backpressure}` + STOP（替代当前"queue 尾排 STOP 继续发旧帧"）。
- admission 在 `HubRegistry.subscribe` 单一无 await 临界段；超限→503 `sse_subscriber_limit_directory`/`_total`（带 `limit`/`current`/`Retry-After`）。
- 转换池（fix-9 🔒）：`MAX_TRANSFORMS=1`，admission 在下载前，限长读 `MAX_RESPONSE_BYTES`，parse/project/gzip offload worker thread。
  - **v2 可配置**：`MAX_RESPONSE_BYTES` 现可通过环境变量 `OC_SLIMAPI_MAX_RESPONSE_BYTES` 配置（默认仍 `64 MiB`）；运行时改 env 重启生效。
- **`/slimapi/metrics` 的 `batch` 字段恒为 `null`**（v2）：BatchLedger（v1 Opt-A 计数器 / rollback window / byte samples）已移除。`/slimapi/metrics` 仍返回订阅者/queue/hub/T3 指标，但顶层 `batch` key 存在且值为 `null`（保持响应 shape 向后字段名稳定，便于客户端旧解析逻辑容忍）。

### §6.x Token stream T3 信封（v2 保留）

> 设计权威 `docs/specs/design-token-stream.md` §6。token 订阅**独立账本**，与控制面 SSE T3 隔离——避免 token 高吞吐挤掉 q/p 或误触控制面 `subscriber_backpressure`。**控制面 `MAX_TOTAL_SUBSCRIBERS=16` / `MAX_SUBSCRIBERS_PER_DIRECTORY=8` 等既有上限一行不改**。

- **独立 admission 账本**：token 订阅**不**占用控制面 `MAX_TOTAL_SUBSCRIBERS=16`；自有 `token_stream_max_subscribers=8`、`token_stream_queue_items=64`、`token_stream_buffer_bytes=512KiB/sub`、`token_stream_max_frame_bytes=1MiB`、`token_stream_handshake_buffer_bytes=8MiB/sub`。
- **同时最多 1 条前台 stream**（客户端预算，对应设计 §9 #7；token stream 每连接绑单 sid）。
- **内存预算 = Option B（拆 4+4，不双计）+ handshake buffer**：
  - `TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live `LivePart.chunks` 累计字节）
  - `TOKEN_PENDING_MAX_BYTES=4MiB`（pending `DeltaAccumulator` 累计字节，与 live **不双计**——同一 delta chunk 不在两个池同时占额度）
  - `TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB/sub`：每个新 subscriber 握手期间（`attach_subscriber` snapshot 构造阶段）构造的 handshake 帧暂存入独立 handshake deque（与 runtime queue **物理分离**），消费端逐帧消费过程中保留；**不与 live/pending 双计**。
  - 单 part 上限 `TOKEN_PART_MAX_BYTES=1MiB`；全局活跃 part 数 `TOKEN_LIVE_PARTS_MAX=32`。
  - **worst-case**：`8 × (512KiB queue + 8MiB handshake) + 4MiB live + 4MiB pending = 76MiB`；runtime 正常态无 handshake 占用的峰值仅 `8 × 512KiB + 4MiB + 4MiB = 12MiB`。
  - `_reserve` 处理 delta 超剩余预算 → 退役最旧 part（按 `last_delta_ms`）+ `resync{token_memory_limit,sessionID}`。
- **admission 溢出** → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。
- **handshake buffer overflow** → 503 `{"code":"sse_token_handshake_overflow","limit":8,"current":N,"bufferBytes":8388608}` + `Retry-After:5`。触发条件：handshake deque items 超 `TOKEN_HANDSHAKE_ITEMS=2048` 或 bytes 超 `TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB`。
- **gzip**：token stream 默认 gzip（杠杆2，§3.x.3，首个 SSE gzip 例外）；控制面 `/slimapi/events` 仍不 gzip。

## §7 错误码 🔒

> v2 错误码集是 v1 的**子集**：所有 routeToken / G6 批量 / Opt-A 相关 code 已删除；其余 thin 路由 HTTP 状态 + body `{"code":…}` 模式不变。所有错误码加性于 v1 → v2 bump 不引入新 code。

- 400 `version_required` / `version_incompatible` / `directory_not_allowed` / `invalid_directory_count`（**v2 无独立生产路径**——q/p 聚合路由删除后此码无触发路径；保留作结构性守卫文档参考，实现中无对应 wire 输出）
  - **`invalid_path`**（catch-all 反代）：归一化后路径含 `..` / `.` 段 → 400（与 `//` 折叠同在 `_normalize_path`；defense-in-depth，合法路径不含此类段）。
  - **`invalid_directory`**：thin 路由与 catch-all 的 `?directory=` query / `X-Opencode-Directory` 头含 `..` 段 / NUL / 控制字符 / 长度 > 4096 → 400（`validate_directory()`；安全守卫，不 gate 合法 directory）。
  - **`directory_not_allowed` 适用范围**：**仅** messages `/**`（list / full/{mid}）当 query `directory` 与 `X-Opencode-Directory` 头同时存在且冲突时返 400——这是结构性歧义（slimapi 不能猜该透传哪个），与上游能否服务无关。其它结构性守卫（`invalid_directory_count` 显式 list 0 / >32、版本门禁）不变。
  - **v2 删除**：v1 的 `invalid_route_token`（routeToken 校验失败）已删除（routeToken 整体下线）；v1 的 `invalid_ids`（G6 top-level：`ids` 空 / 超 20 / 解析后无有效 mid）已删除（G6 端点不存在）。
- 403 `shell_not_allowed`（catch-all shell/PTY deny-list；ops 可关，非安全保证）
- 404 `session_not_found`（`GET /slimapi/messages/{sid}` 与 `/full/{mid}` 的 upstream 404；top-level，带 `sessionID`）；`thin_route_not_found`
  - **v2 删除**：v1 的 `message_not_found`（G6 envelope mid 级 code）已删除（G6 端点不存在）；客户端单条 `/full/{mid}` upstream 404 仍走 `session_not_found` 或上游原始 404 透传。
- 413 `response_too_large`（top-level：超 `MAX_RESPONSE_BYTES`）
  - **`message_too_large`**：**top-level** 于 `GET .../full/{mid}`（单条流式 cap→413）。
  - **v2 删除**：v1 的 envelope 语境（G6 mid body 超 `max_message_bytes`）已删除（G6 端点不存在）。
- 502 `upstream_http_N`（top-level：thin 路由对 upstream **非 404 的 4xx** → 502）
  - **v2 删除**：v1 的 G6 envelope 语境（mid ≥400 → `errors[]`）已删除；v1 的 G6 discover 语境已删除。
- 503 `transform_busy`（`Retry-After`；含 `GET /slimapi/sessions` 列表 projection 池饱和）/ `upstream_unavailable`（含 allowlist 刷新失败——v2 中 allowlist 已无独立刷新路径，此 code 主要覆盖 upstream 网络/5xx/坏 JSON）/ `sse_subscriber_limit_*`
  - **`GET /slimapi/questions` 发现调用例外（2026-08-05 加性）**：该端点的目录发现调用（`GET /project`）是**内部派生调用**，其**任一**失败（网络/5xx/**4xx**/坏 JSON/非 list）统一映射为 **503 `upstream_unavailable`**（整体 total failure，无 envelope），**不走**通用的 4xx→502 `upstream_http_N` 规则——发现是 sidecar 自身 fan-out 的前提，泄漏上游状态码会误导客户端认为某个 directory 失败。per-dir `/question` 失败仍遵循通用 §7 映射（5xx→`upstream_unavailable`，4xx→`upstream_http_N`，isolated 进 `errors[]`）。（**2026-08-05 v2+fix**：发现调用从原 `GET /session?limit=…` 改为 `GET /project`，映射规则不变。）
- 503 `sse_token_subscriber_limit`（token stream admission 溢出；带 `{"limit":8,"current":N}` + `Retry-After:5`；**独立账本**，不占控制面 `MAX_TOTAL_SUBSCRIBERS`，见 §6.x）
- 503 `sse_token_handshake_overflow`（token stream handshake buffer overflow；带 `{"limit":8,"current":N,"bufferBytes":8388608}` + `Retry-After:5`；触发于单次 handshake items 超 `TOKEN_HANDSHAKE_ITEMS=2048` 或 bytes 超 `TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB`，见 §6.x）
- **v2 删除**：v1 的 `upstream_unavailable`（envelope per-mid，Opt-A opt-in）已删除（Opt-A 整体下线）；v1 的 `upstream_error`（G6 envelope mid 2xx body 不可解析 + q/p fan-out 单 dir 失败项）已删除（G6 / q/p 端点不存在）；v1 的 q/p mutation `upstream_timeout` 504 已删除（q/p 写端点不存在，所有写经 catch-all 反代时由上游 opencode 自身语义决定）。
- thin 路由错误体统一：`{"code":string, "message"?:string, ...}`（非 `{"detail":...}`）
- FastAPI 参数缺失/类型错误仍为 422。
- **可观测性（v2 保留，加性，不 bump、不构成协议契约依赖）**：每请求由最外层中间件生成/透传 `X-Request-ID`（请求头 + 响应头回显，并透传上游 opencode；入站值含 CR/LF/控制字符/空白/超 128 则丢弃改生成）；access log 每条记录含 `requestId` 字段。客户端可 echo 该头做跨 sidecar↔opencode 关联诊断。
  - **access log 文件发现规则（2026-07-29 加性，ops 面，非 wire 协议）**：旧 `logs/access.jsonl`（`RotatingFileHandler`，size 10MiB×5）改为 **按天切分** `access-YYYY-MM-DD.jsonl`（`YYYY-MM-DD` = `date.today().isoformat()`）。启动时将**早于今天的**（日期 `< today`）未压缩 `.jsonl` 原子压缩为独立 `.gz`（写 `.gz.tmp` → rename），并迁移**当前 `access_log_dir` 内**的无日期文件（旧 `access.jsonl`/`access.jsonl.N`）为 `access-legacy-{mtimeYYYYMMDD}-{N}.jsonl.gz`（**不跨目录迁移**；生产从旧相对目录升到 `StateDirectory` 时旧位置历史日志需运维手动移动）。后台周期 maintenance loop 执行 compress + prune（`OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS`，默认 `0` = 不删；prune 严格匹配 `access-YYYY-MM-DD.jsonl(.gz)`，**不含** `access-legacy-*.jsonl.gz`——后者永久保留、由运维清理）。旧 env `OC_SLIMAPI_ACCESS_LOG_PATH`/`_MAX_BYTES`/`_BACKUPS` 标 **deprecated**（字段保留兼容；`_PATH` 若设非默认值，取其 parent dir 兜底）。本条是 **ops 面**（非客户端 wire 行为），因旧路径曾在本契约记录故同步更新；`X-Slimapi-Version` **不 bump**。
  - **可选客户端标识输入头（2026-07-29 加性）**：客户端**可选**发送 `X-Client-Name`（app 名，如 `ocdroid`）、`X-Client-Version`（如 `1.2.3`）、`X-Client-Id`（设备标识）。sidecar 读取后落入 access log 的 `client` / `clientVer` / `clientId` 字段（缺省 `null`，向后兼容）。**这三个头不透传给 opencode**（catch-all 反代在 `strip_hop_by_hop` 之后显式 pop，大小写不敏感）。校验规则：空/空白 → 忽略；UTF-8 字节数 > 128 → 忽略（**拒绝不截断**，避免合并不同 id）；含控制字符（`<0x20` 或 `0x7f`）→ 忽略（防注入）；重复同名 header → 取第一个有效值（lenient）。设备 id（`clientId`）默认经 **SHA-256 截断 16 hex 字符** hash（`sha256(raw)[:16]`）后落盘（防日志明文直出）；设置 `OC_SLIMAPI_CLIENT_ID_SALT` 时升级为 **HMAC-SHA256 截断 16 hex 字符**（`hmac_sha256(salt, raw)[:16]`，更强）；`client`/`clientVer` **不 hash**（需明文筛选，可能含 PII）。**诚实标注**：默认级（无 salt）是「防明文直出」，非抗对手强匿名。**`X-Slimapi-Version` 不 bump**（加性输入，缺省忽略，不影响既有客户端）。

## §8 客户端 v2 最小集

连接(R8) + 版本头（**`X-Slimapi-Version: 2`**）+ health 自检(M2/fail-closed) + 冷启动（`/sessions` + 当前 ses `/messages/{sid}` 骨架，见 §4）+ SSE（digest + q/p asked + `lastError`/`session.error`）+ digest 触发拉消息（`/messages/{sid}` 骨架 + `/full/{mid}` 全文）+ 发消息/q/p 应答/abort（**经 catch-all + `X-Opencode-Directory` 透传**，无 routeToken）+ resync=冷启动。

**v2 删除面**：routeToken 消费、`/projects` cold-start、`/questions` + `/permissions` 聚合、`/sessions/{sid}/children` + `childrenVersion` 比对、`/messages/{sid}/since/{ts}` 增量、`/full?ids=` 批量、Opt-A 能力协商与 partial-envelope 处理、Stage B fingerprint（304短路 / `X-Message-Event-Seq` / `?known.*`）—— 客户端**不应**再实现这些代码路径。（`GET /slimapi/sessions/status` 已于 2026-08-03 加性回归，客户端可恢复消费；`GET /slimapi/questions` 已于 2026-08-05 加性回归为跨目录聚合端点，客户端可恢复消费，详见 §2。）

## §9 gzip 🔒（小修）

所有 JSON 路由的 `json_response` 调用转发 `accept_encoding=request.headers.get("accept-encoding")`。sessions 已做；health/ready 等补齐。

> **SSE gzip 例外（v1 rev J，v2 保留）**：历史「SSE 永不 gzip」由 token stream 打破——`GET /slimapi/sessions/{sid}/stream` **默认 gzip**（杠杆2，首个 SSE gzip 例外，详见 §3.x.3）；控制面 `GET /slimapi/events` **仍不 gzip**。

## §10 延后（非 v2）

skeleton 共享缓存（YAGNI，先指标）、多用户（独立 stack）、Part 展开 UI、circuit breaker、metrics 之外的可观测、Stage E token stream gzip 调参。

## §11 directory 语义与转发

> **v2 整合 v1 §12 + §13**。directory 处理在 v2 中显著简化：allowlist 数据流整体下线，slimapi 不再做目录警察、也不再维护 discovery set。

### §11.1 跨端点 directory 三态语义

| 端点 | null / 未传 | 显式 directory |
|---|---|---|
| `GET /slimapi/sessions` | 200，不过滤（upstream 默认） | 透传 `?directory=` + `X-Opencode-Directory`（normalize 后） |
| `GET /slimapi/sessions/status` | **400/422——`directory` 必填**（FastAPI 缺参→422） | normalize 后透传 `?directory=` + `X-Opencode-Directory`；上游 `GET /session/status` 返回全量 status map（directory 参数对上游是 no-op，但 slimapi 仍必填 + 透传以与 v1 契约一致） |
| `GET /slimapi/messages/**`（含 `/full/{mid}`） | **不拦**（upstream 默认） | normalize 后作 `X-Opencode-Directory`；query 与 header 冲突 → 400 `directory_not_allowed` |
| `GET /slimapi/sessions/{sid}/stream` | 不过滤（订阅所有 directory 事件） | **no-op**——directory 不改变订阅者接收的帧集（累加器以 sessionID 为键，单用户 T3 全局唯一；与 §3.x.1 一致）；仅 query 与 `X-Opencode-Directory` 头冲突 → 400 `directory_not_allowed`（结构性守卫，NB-D7） |
| catch-all `/{path}` | upstream 默认 | 客户端自带 `X-Opencode-Directory` 头透传；slimapi 不剥（非 hop-by-hop） |

说明：
- **slimapi 不做目录警察**：directory 的合法性由上游 opencode 决定；opencode 自身的 4xx 会经 §7 透传（如 `upstream_http_N`）。
- **sid 能力凭证**：客户端仅从 list / SSE 合法渠道获知 sid。
- **v2 删除**：v1 §12 的 `/sessions/{sid}/status`、`/questions` + `/permissions`（repeated 1-32 + null 聚合）、POST q/p reply/reject/permission（directory 来自 routeToken）行已移除——这些端点在 v2 中不存在。（`GET /slimapi/sessions/status` 于 2026-08-03 加性回归，见 §2；v1 的 per-session `/sessions/{sid}/status` 仍删除。）
- **v2 删除**：v1 §12「null 聚合（q/p）」「scope.directories 区分 scope 未就绪/权威空」语义已删除（q/p 聚合端点不存在）。

### §11.2 directory 发现与 allowlist 状态（v2 下线）

- **v2 删除**：v1 的 `load_products(app)` / `app.state.directory_allowlist` / `allowlist_ready` 数据流**整体下线**——`/slimapi/projects` 展示端点、q/p null-directory 聚合 fan-out、`/sessions` 的 `X-Discovery-Directories` / `X-Discovery-Ready` 头三个消费者均已删除，allowlist 在 v2 中无任何 wire 用途。
- **slimapi 不再有 discovery 启动暖机**（lifespan 不再调 `load_products` / `warm_allowlist`）。
- **slimapi 不再有 `server.reconfigured{reason:"discovery_changed"}` SSE 帧**（触发条件不再发生）。
- **结构性守卫保留**：slimapi 仍保留 `normalize_directory`（去尾斜杠，根 `/`）、`invalid_directory_count`（显式 repeated list 1–32）、`invalid_directory`（路径段安全）、query 与 `X-Opencode-Directory` 头冲突 400 等**结构性守卫**——这些与 allowlist 无关，纯防御性。隔离靠 stunnel mTLS（:14097）/ Tailscale ACL + 防火墙（:4097 明文直连）+ loopback upstream 等网络边界。

## §12 流量/省流查询与 accounting（运维诊断）

> 客户端 / 运维查询"哪些请求未省流"或"省流比率"的入口：access log（按天切分 `access-YYYY-MM-DD.jsonl`，早于今天的文件启动压缩为 `.gz`；每条记录含 `method` / `path` / `bucket`（`slimapi` / `passthrough`）/ `requestId` / 可选 `client`/`clientVer`/`clientId` / `bytes` / `status` / 时延）+ `/slimapi/metrics`（T3 订阅者/queue/hub 指标，`batch` 恒 `null`）+ 内存账本周期快照 `traffic-snapshot-YYYY-MM-DD.jsonl`（按天切分、不经自动压缩/prune；cumulative，含 SSE 真实成本，跨进程靠 `bootTs`/`runId`/`uptimeS` 分段）。详见 §7 access log 文件发现规则与 `docs/manual/traffic-accounting.md`。

- 查"哪些请求未省流"：按 `bucket=="passthrough"` 过滤 access log、聚合 `method+path`，再对照本契约 §2 端点表看有无 `/slimapi` 等价省流路由。
- `/slimapi/metrics.traffic`（如有实现，加性诊断端点）提供聚合视角；本契约不强制要求该端点存在，详见 `docs/manual/traffic-accounting.md`。
- access log 文件命名、压缩、retain 与 client header 详见 §7 可观测性条目；均为 ops 面，`X-Slimapi-Version` 不 bump。

---

> **更新纪律**：影响以上节的行为变更（端点删除 / 字段删除 / wire 状态码变更）须同步更新 `docs/specs/CLIENT_CHANGES.md` 相关节与 `CHANGELOG.md`，并按 `docs/release.md` 评估是否需要进一步 bump。
