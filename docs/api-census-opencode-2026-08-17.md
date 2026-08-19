# opencode v1.18.x 上游 API 普查报告（2026-08-17）

> 调研根：`/home/mar/personal_projects/ocdroid/opencode-src/current`（树内实际版本 **v1.18.18**，package.json；AGENTS.md 所述 1.18.16 略滞后）
> 调研方式：fixer-ds 只读研究（explorer 泳道当日不可用，改派 fixer-ds 完成）
> 用途：为 oc-slimapi 下一批接口收编提供上游能力面决策输入

---

## 1. 两套 server 包关系

**一句话**：不是新旧二选一——`packages/opencode/src/server/routes/instance/httpapi/`（"Experimental HttpApi" 实例层，无前缀 `/session/**` legacy 风格）与 `packages/server/src/`（v2 协议层，`/api/**` 前缀 + cursor 分页）**同时挂载在同一个 :4096 端口**上，由 `HttpApiApp.createRoutes()` 合并 7 条路由层。

证据链：
- 实际监听者：`packages/opencode/src/server/server.ts:101` `HttpRouter.serve(HttpApiApp.createRoutes(opts))`；`:117-122` `startWithPortFallback` 优先 4096 端口（即 :4096 = 本服务）。
- 层合并：`packages/opencode/src/server/routes/instance/httpapi/server.ts:141-181` `createRoutes` 合并 `rootApiRoutes`（Root）、`eventApiRoutes`（/event）、`ptyConnectApiRoutes`（WS）、`instanceRoutes`（无前缀 legacy 实例路由）、`serverRoutes`（=`@opencode-ai/server` 的 `Api`，v2 `/api/*`）、`docRoute`（GET /doc OpenAPI）、`uiRoute`（GET /* UI catch-all）。
- v2 层来源：`packages/server/src/api.ts:5` `Api = makeDefaultApi({...})`；`packages/server/src/routes.ts:54` `HttpApiBuilder.layer(Api, {openapiPath: "/openapi.json"})`。`makeDefaultApi` 在 `packages/protocol/src/api.ts:78-86`，由 `makeApiFromGroup`（`:37-55`）组 18 个 group（Health→ProjectCopy），全部走 `/api/**` 前缀。
- 实例层组合：`packages/opencode/src/server/routes/instance/httpapi/api.ts:79-94` `OpenCodeHttpApi = RootHttpApi + EventApi + InstanceHttpApi + ServerApi + PtyConnectApi`；`:54-59` Root = Control + ControlPlane + Global；`:61-77` Instance = 15 个 group。

**运维含义**：oc-slimapi 若要把 `/api/**` v2 端点也收编，上游就在同一端口、同一路由树，无需新增直连。

---

## 2. 按资源域分组的端点清单

量级标注：**S** = 小（KB 级，配置/状态/单对象）｜**M** = 中（列表，几十~几百项）｜**L** = 大（全量消息/文件内容/diff，可达 MB 级）
增量/流式：`cursor=` 表示游标分页，`SSE` 表示流，`WS` 表示 WebSocket。

### 2a. 实例层（`routes/instance/httpapi/groups/*`，无前缀 legacy 风格 — ocdroid 现行对接面）

**session（groups/session.ts:462）**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| GET `/session` | 会话列表（query: scope/path/roots/start/search/limit） | M | — |
| GET `/session/status` | 全部会话状态（ocdroid 轮询热区） | M | — |
| GET `/session/:sessionID` | 单会话元数据 | S | — |
| GET `/session/:sessionID/children` | 子会话列表（fork/compact 链） | S | — |
| GET `/session/:sessionID/todo` | 待办列表 | M | — |
| GET `/session/:sessionID/diff` | 文件 diff 数组（query messageID） | L | — |
| GET `/session/:sessionID/message` | 消息列表（query limit/before） | **L**（无 limit 时全量 WithParts） | `cursor=`（before + Link 头 + X-Next-Cursor） |
| GET `/session/:sessionID/message/:messageID` | 单消息（含全部 parts，含内联文件内容） | L | — |
| POST `/session` | 创建会话 | S | — |
| DELETE `/session/:sessionID` | 删除会话 | S | — |
| PATCH `/session/:sessionID` | 更新（title/metadata/permission/time.archived） | S | — |
| POST `/session/:sessionID/fork` | fork 会话 | S | — |
| POST `/session/:sessionID/abort` | 中止运行 | S | — |
| POST `/session/:sessionID/init` | 初始化（providerID/modelID/messageID） | S | — |
| POST/DELETE `/session/:sessionID/share` | 分享/取消分享 | S | — |
| POST `/session/:sessionID/summarize` | **手动 compaction**（revert cleanup + compaction + 重跑 loop） | M | — |
| POST `/session/:sessionID/message` | 发 prompt（响应为 **streaming JSON** 消息流） | M/L | SSE 式流 |
| POST `/session/:sessionID/prompt_async` | 异步 prompt（后台子代理） | S | — |
| POST `/session/:sessionID/command` | 执行内置 command | S | — |
| POST `/session/:sessionID/shell` | shell 交互 | S | — |
| POST `/session/:sessionID/revert` / `/unrevert` | 回滚/取消回滚 | S | — |
| POST `/session/:sessionID/permissions/:permissionID` | 权限响应（**deprecated:true**） | S | — |
| DELETE `/session/:sessionID/message/:messageID` | 删消息 | S | — |
| DELETE/PATCH `/session/:sessionID/message/:messageID/part/:partID` | 删/改 part（updatePart 校验 id/messageID/sessionID 一致） | S | — |

**event（groups/event.ts:29）**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| GET `/event` | 全局 SSE 事件流（帧 `event:message` + `data:{id,type,properties}`；首帧 server.connected；10s heartbeat；按 instance.directory 过滤） | 持续流 | **SSE** |

**global / control / config（Root 层）**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| GET `/global/health` | 健康检查（healthy+version） | S | — |
| GET `/global/event` | 全局事件 SSE（GlobalBus，跨实例/项目/workspace） | 持续流 | **SSE** |
| GET/PATCH `/global/config` | 全局配置读写（ConfigV1.Info） | M | — |
| POST `/global/dispose` / `/global/upgrade` | 关闭 / 自升级 | S | — |
| PUT/DELETE `/auth/:providerID` | 设置/清除 provider auth（Auth.Info） | S | — |
| POST `/log` | 客户端日志上报 | S | — |
| GET/PATCH `/config` | 实例配置读写 | M | — |
| GET `/config/providers` | provider 配置列表 | M | — |

**instance / file / vcs**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| POST `/instance/dispose` | 释放实例 | S | — |
| GET `/path` | home/state/config/worktree/directory 路径 | S | — |
| GET `/vcs` | 当前分支信息（branch/default_branch） | S | — |
| GET `/vcs/status` | 文件状态数组（FileStatus） | M | — |
| GET `/vcs/diff` | 文件 diff 数组（query mode/context） | **L** | — |
| GET `/vcs/diff/raw` | **raw patch 纯文本**（text/x-diff） | **L** | — |
| POST `/vcs/apply` | 应用 diff | S | — |
| GET `/command` / `/agent` / `/skill` | command/agent/skill 元数据列表 | M | — |
| GET `/lsp` / `/formatter` | LSP/formatter 状态 | S | — |
| GET `/find` | ripgrep 全文搜索（LegacyMatch: path/lines/line_number/submatches） | M | — |
| GET `/find/file` | 文件名搜索（limit≤200） | M | — |
| GET `/find/symbol` | 符号搜索（LSP） | M | — |
| GET `/file` | 目录列表（LegacyEntry） | M | — |
| GET `/file/content` | **文件内容**（text|binary，可选 diff/patch{hunks}，base64+mimeType） | **L** | — |
| GET `/file/status` | 文件增删状态（LegacyStatus） | M | — |

**project / mcp / provider / pty / question / permission**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| GET `/project` / `/project/current` | 项目列表/当前项目 | M | — |
| POST `/project/git/init` | git init | S | — |
| PATCH `/project/:projectID` | 改项目（name/icon/commands） | S | — |
| GET `/project/:projectID/directories` | 项目目录 | M | — |
| GET `/mcp` | MCP server 状态 map | M | — |
| POST `/mcp` | 添加 MCP | S | — |
| POST/DELETE `/mcp/:name/auth`、`/auth/callback`、`/auth/authenticate` | MCP OAuth 授权流 | S | — |
| POST `/mcp/:name/connect` / `/disconnect` | 连接管理 | S | — |
| GET `/provider` / `/provider/auth` | provider 列表/授权方法 | M | — |
| POST `/provider/:providerID/oauth/authorize` / `/callback` | provider OAuth | S | — |
| GET `/pty/shells`、GET/POST `/pty`、GET/PUT/DELETE `/pty/:ptyID`、POST `/pty/:ptyID/connect-token` | 伪终端管理 | S/M | — |
| GET `/pty/:ptyID/connect` | PTY 数据通道（PtyConnectApi，ticket 鉴权） | 持续流 | **WS** |
| GET `/question`、POST `/question/:requestID/reply` / `reject` | 交互问题队列 | S | — |
| GET `/permission`、POST `/permission/:requestID/reply` | 权限请求 | S | — |

**sync / tui / experimental（新增域）**

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| POST `/sync/start` | 开始事件同步 | S | — |
| POST `/sync/replay` | 重放事件历史（directory+events） | L | — |
| POST `/sync/steal` | 接管 session | S | — |
| POST `/sync/history` | **增量事件同步**（传 `Record<aggregateID,lastSeq>`，只返回 seq>value 的事件） | M | `cursor=`（seq） |
| GET/POST `/tui/…`（13 端点） | TUI 控制 | S | — |
| GET `/experimental/capabilities` | 能力探测（backgroundSubagents） | S | — |
| GET `/experimental/console`、`/console/orgs`、POST `/console/switch` | 控制台/组织切换 | S | — |
| GET `/experimental/tool`、`/tool/ids` | 工具元数据 | M | — |
| GET/POST/DELETE `/experimental/worktree`、POST `/worktree/reset` | worktree 管理 | S | — |
| GET `/experimental/session` | **跨项目全局会话列表**（query roots/start/cursor/search/limit/archived） | M | `cursor=` |
| POST `/experimental/session/:sessionID/background` | 后台子代理 | S | — |
| GET `/experimental/resource` | MCP resources map | M | — |
| POST `/experimental/control-plane/move-session` | 会话迁移（control-plane） | S | — |
| POST `/experimental/project/:projectID/copy/generate-name` | 项目复制命名 | S | — |

### 2b. v2 协议层（`packages/protocol/src/groups/*` → `packages/server/src/handlers/*`，`/api/**` 前缀，`{data, cursor}` 包装）

| METHOD path | 用途 | 量级 | 增量/流式 |
|---|---|---|---|
| GET `/api/health` | 健康检查 | S | — |
| GET `/api/location` | 位置信息 | S | — |
| GET `/api/agent` | agent 列表 | M | — |
| GET/POST `/api/session` | 会话列表/创建（SessionsQuery，limit=50） | M | `cursor=`（base64url JSON 快照，anchor{id,time,direction}） |
| GET `/api/session/active` | 运行中会话 map | S | — |
| GET `/api/session/:sessionID` | 单会话 | S | — |
| POST `/api/session/:sessionID/agent` / `/model` | 切换 agent/model | S | — |
| POST `/api/session/:sessionID/prompt` | 发 prompt（返回 {data: Admitted}） | S | — |
| POST `/api/session/:sessionID/compact` | compaction | S | — |
| POST `/api/session/:sessionID/wait` | 阻塞等待空闲 | S | — |
| POST `/api/session/:sessionID/revert/stage` / `clear` / `commit` | 回滚三段式 | S | — |
| GET `/api/session/:sessionID/context` | **活跃上下文消息全量**（最近 compaction 后） | **L** | — |
| GET `/api/session/:sessionID/history` | 持久事件序列（query limit≤100/after，after=exclusive seq） | M | `cursor=`（seq，hasMore） |
| GET `/api/session/:sessionID/event` | 会话事件流（query after） | 持续流 | **SSE** |
| POST `/api/session/:sessionID/interrupt` | 中断 | S | — |
| GET `/api/session/:sessionID/message/:messageID` | 单消息 | M | — |
| GET `/api/session/:sessionID/message` | 消息分页（limit 1-200 / order asc|desc / cursor） | **L** | `cursor=`（不透明 cursor，{data, cursor{previous,next}}） |
| GET `/api/event` | 全局事件 SSE（V2Event union） | 持续流 | **SSE** |
| GET `/api/model` / `/api/provider` / `/api/provider/:id` | 模型/provider | M | — |
| GET `/api/integration` 等 | 集成管理 | S/M | — |
| PATCH/DELETE `/api/credential/:credentialID` | 凭据管理 | S | — |
| `/api/permission/**`（request/saved/session 级） | 权限系统（v2 版） | S | — |
| GET `/api/fs/read/*` | **文件字节读取**（Uint8Array） | **L** | — |
| GET `/api/fs/list`（query path） | 目录列表 | M | — |
| GET `/api/fs/find`（query query/type/limit） | 文件搜索 | M | — |
| GET `/api/command` / `/api/skill` / `/api/reference` | 元数据列表 | M | — |
| `/api/pty/**` | PTY 管理 + WS 数据通道 | S/M | **WS** |
| `/api/question/**` | 问题交互（v2） | S | — |
| GET `/doc`（实例层）、GET `/openapi.json`（v2 层） | OpenAPI 文档 | M | — |

---

## 3. 「大 body 高频」sidecar 收编候选 Top 12

按省流收益排序（大 body + 高频 + 可投影/可增量）：

1. **GET `/session/:sessionID/message`** — 无 limit 时返回全量消息 + 全部 parts（内联文件内容），是 ocdroid 主视图最大单一响应；已有 skeleton 投影基础，可扩展 cursor 透传。`handlers/session.ts:106-145` + `message-v2.ts:425-465`
2. **GET `/api/session/:sessionID/context`** — compaction 后活跃上下文全量重放，L 级且每次 compaction 后必拉。
3. **GET `/file/content`** — 文件全文 + 可选 diff/patch（base64），`/file/status` 高频轮询后必跟。
4. **GET `/vcs/diff` 与 `/vcs/diff/raw`** — diff 数组 / raw patch 纯文本，L 级；可只投影元数据（文件列表 + 增删行数）。
5. **GET `/session/:sessionID/diff`** — 按 messageID 的文件 diff 数组，每轮对话都会出现。
6. **GET `/api/session/:sessionID/history`** — 持久事件序列，增量 seq（after）已内建——天然收编点，侧车可缓存 seq 只拉增量。
7. **GET `/sync/history`** — 传 lastSeq 只取增量事件的同步原语，客户端离线恢复热区，响应体与消息规模相当。`groups/sync.ts:113`
8. **POST `/session/:sessionID/message`**（流式 prompt 响应）— streaming JSON 消息流，正是策展 SSE（session.digest + q/p 直推）的替换对象；`handlers/session.ts:295-309`
9. **GET `/session/status`** — ocdroid 高频轮询（全量会话状态）；可侧车聚合 + 降频/缓存。
10. **GET `/api/session/:sessionID/message`** — v2 cursor 分页版（limit≤200），可作为向 ocdroid 迁移的标准分页通道。`protocol/groups/message.ts:51`
11. **GET `/experimental/session`** — 跨项目全局会话列表，起点是全部 workspace roots，L/M 级；`cursor=` 已就绪。
12. **GET `/event` / `/api/event` / `/api/session/:sessionID/event`** — 三条 SSE 流，帧体 `{id,type,properties}`；侧车已做 /global/event 策展，可扩展至 instance 级（按 directory 过滤）。`handlers/event.ts:69-85`

---

## 4. 关键文件引用清单

**路由定义**
- `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts`（462 行，legacy 实例会话端点全集）
- `packages/opencode/src/server/routes/instance/httpapi/groups/experimental.ts:275`、`sync.ts:113`、`workspace.ts:141`、`tui.ts:208`、`instance.ts:206`、`file.ts:185`、`mcp.ts:156`、`pty.ts:172`、`provider.ts:101`、`global.ts:136`、`control.ts:76`、`config.ts:65`、`project.ts:93`、`question.ts:74`、`permission.ts:61`、`event.ts:29`、`control-plane.ts:35`、`project-copy.ts:32`
- `packages/protocol/src/groups/`：`session.ts:379`、`message.ts:51`、`event.ts:56`、`fs.ts:68`、`permission.ts:137`、`model.ts:29`、`provider.ts:45`、`integration.ts:130`、`credential.ts:37`、`pty.ts:143`、`question.ts:84`、`project-copy.ts:56`

**组装与挂载**
- `packages/opencode/src/server/routes/instance/httpapi/server.ts:141-181`（createRoutes 7 层合并）
- `packages/opencode/src/server/routes/instance/httpapi/api.ts:54-94`（RootHttpApi / InstanceHttpApi / OpenCodeHttpApi 组合）
- `packages/opencode/src/server/server.ts:101,117-122`（HttpRouter.serve + 4096 端口回退）
- `packages/protocol/src/api.ts:37-86`（makeApiFromGroup / makeDefaultApi，18 group 顺序）
- `packages/server/src/api.ts:5`、`packages/server/src/routes.ts:54`（v2 Api 层 + /openapi.json）

**分页 / 流式核心**
- `packages/opencode/src/session/message-v2.ts:63-78`（cursor 编解码）、`:95-96`（older() 复合序）、`:425-486`（page()/allPages()）
- `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:106-145`（messages：Link 头 + X-Next-Cursor）、`:273-309`（summarize/prompt 流式）
- `packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts:69-85`（SSE 头与 heartbeat）
- `packages/server/src/handlers/session.ts`（v2 SessionsCursor.parse/make、DefaultSessionsLimit=50）

**schema**
- `packages/schema/src/v1/session.ts`、`message.ts`、`config.ts`、`project.ts`、`vcs.ts`、`packages/protocol/src/groups/event.ts:15-27`（EventSchema 强制 server.connected）

---

## 5. v1.18.x 新增/改动端点 — 结论

**v1.18.x（v1.18.0→v1.18.18）HTTP API 面无新增、无改动**。证据：GitHub `compare/v1.17.20...v1.18.0` 共 172 文件变更，全部落在 `packages/app|desktop|session-ui|tui|ui|i18n`（Desktop v2 UI 迁移期），零 server/协议/路由改动；`session.ts` 源码对比 v1.17.20（462 行）与当前树完全一致。

**此前已存在（非 v1.18 引入）**：`children`、`summarize`/compaction、`diff`、`todo`、`revert/unrevert`、`updatePart`、`permissionRespond`、v2 `/api/**` cursor 分页 —— 至少 v1.14.37 已有。

> 对 oc-slimapi 的决策输入：legacy `/session/**` 与 v2 `/api/**` 在同一端口共存 → 收编 v2 端点零额外上游改动；v2 message/context/history 自带 cursor/seq 增量语义，是 skeleton 投影 + 增量同步的自然演进通道；`/sync/history` 与 `/api/session/:sessionID/history` 是两条可复用的增量事件通道。
