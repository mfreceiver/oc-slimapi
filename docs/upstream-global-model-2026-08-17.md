# opencode 上游 instance/global 模型真相（2026-08-17，exp-u 探索）

> 对象：opencode-src/current（v1.18.18）。用途：「单实例全局能力」可行性评估的上游证据。

## ① Instance 模型结论

**一个 instance = 一个 directory**。InstanceStore（`packages/opencode/src/project/instance-store.ts:108-124`）以 `FSUtil.resolve(directory)` 为 key 做进程内 LRU 缓存，每 directory 独立 boot（加载 project + bootstrap）。无"全局单实例"概念；重启全重建；无实例 ID——**directory 就是实例标识**。

- 路由：`?directory=` / `X-Opencode-Directory` / `process.cwd()` → `WorkspaceRoutingMiddleware`（workspace-routing.ts:86-88）→ `InstanceContextMiddleware`（instance-context.ts:23-35）→ `store.load({directory})`
- InstanceContext = `{directory, worktree, project: Project.Info}`（instance-context.ts:5-9）
- 销毁：`/global/dispose` → `disposeAll()` 遍历 cache（instance-store.ts:166-182）

## ② 全局能力矩阵

| 能力 | 端点 | 数据来源 | 限制 |
|---|---|---|---|
| 跨 project 全部 sessions | `GET /experimental/session`（groups/experimental.ts:224-234） | **单 SQLite 全表 SELECT**（`listGlobal` session.ts:557-596） | 无 directory 过滤时扫全表；默认排除 archived；返回 GlobalInfo（含 project 名） |
| 按 directory 精确过滤 | `?directory=...` | `WHERE directory = ?` | 精确匹配，无前缀/通配；**directory 列无索引** |
| 仅根会话 | `?roots=true` | `WHERE parent_id IS NULL` | — |
| 时间范围/分页 | `?start=&cursor=` | `WHERE time_updated >= start AND < cursor` | cursor 基于 time_updated，**非 stable**（并发更新可能丢页） |
| 标题搜索 | `?search=` | `LIKE '%…%'` | 仅标题 |
| **archived 过滤** | `?archived=true` | 默认 `time_archived IS NULL`；true 取消条件（含 archived 全返回） | — |
| 全局事件 SSE | `GET /global/event` | GlobalBus（进程 EventEmitter） | 所有 instance 的 disposed + durable sync events + heartbeat；**不含 instance 本地事件** |
| 实例事件 SSE | `GET /event` | EventV2Bridge 事件溯源 | 按 `instance.directory`+workspaceID 过滤（handlers/event.ts:35-38 硬编码） |
| instance 列表 | `GET /session` | `listByProject`（session.ts:957-1010） | **按 project_id 硬过滤；无 archived 条件（含 archived）** |
| 增量事件 | `POST /sync/history`（lastSeq）/ `GET /api/session/:sid/history`（after seq） | 事件溯源表 | 前者 instance-scoped，后者单 session |

### /session vs /experimental/session

| 维度 | GET /session | GET /experimental/session |
|---|---|---|
| scope | project_id 硬过滤 | 全表（跨 project） |
| 返回 | Session.Info[] | GlobalInfo[]（含 project 字段） |
| archived | 不过滤（全含） | 默认排除，?archived=true 含 |
| 分页 | 简单 limit | cursor（x-next-cursor 头） |

### /global/event vs /event

/global/event = GlobalBus 全局（无过滤，含 server.connected/heartbeat/instance.disposed/durable sync）；/event = 实例本地（message/session 事件，按 directory 过滤）。**消息级实时事件只在 /event。**

## ③ archived 语义

- `session` 表 `time_archived` 列（nullable int，sql.ts:59）；`ArchivedTimestamp = Schema.Finite` 允许负数（legacy）
- 写：`PATCH /session/:sid` body `time.archived` → `setArchived()`（session.ts:759-761）；undefined/null = 取消归档
- 读：全局列表默认排除；instance 列表含；**软删除语义，数据完整保留**

## ④ Storage 布局

**单文件 SQLite**：`~/.local/share/opencode/opencode.db`（database.ts:43-55，`OPENCODE_DB` 可覆盖）。所有 project 的 session/message/part/事件溯源表同库，`session.project_id` 有索引、**directory 无索引**。跨 directory 查询 = 同库扫表——**全局能力的物理基础已就绪**。

表：session（id/project_id/directory/workspace_id/parent_id/time_archived/time_updated）、message/part（v1 legacy）、session_message/session_input（v2 事件溯源）、session_context_epoch、todo、project。

## ⑤ 「全局单实例替代扇出」可行性

**上游原生支持**：① 单 DB 全局存储（跨 project 查询零聚合）② GlobalBus 全局事件 ③ 事件溯源增量原语 ④ workspace 路由中间件。

**上游障碍**：① InstanceContext 强绑定 directory——无 directory 的全局 handler 不存在 ② listByProject 硬编码 project 过滤 ③ /event 按 directory 过滤——全局流无消息级事件 ④ session 创建/操作全部经 InstanceContextMiddleware 需 directory 定位 ⑤ listGlobal 的 directory 过滤无索引 ⑥ project JOIN 缺行时 project=null。

| 场景 | 支持度 | 缺口 |
|---|---|---|
| 全局 session 列表/搜索/archived | ✅ 已实现 | 无 |
| 全局事件流 | ✅ /global/event | 无消息级事件 |
| 全局 session 操作（prompt/fork/delete） | ❌ | 需 directory 路由 |
| 全局增量同步 | ⚠️ 原语在 | 需聚合多 session events |
| 单 DB 替代多 instance | ⚠️ DB 全局 | 服务层强 per-directory |

**核心结论**：存储层天然全局，服务层强 per-directory。「全局化」的正确姿势 = **sidecar 层做全局门面**（利用 /experimental/session + /global/event + 单 DB 事实），而非改上游。
