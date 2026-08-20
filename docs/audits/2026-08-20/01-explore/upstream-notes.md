# E8 上游源码对照笔记（opencode v1.18.18）

> 审计 Phase 1 / E8 单元产物，2026-08-20。只读探索，未改动两仓任何源码。
> 上游根：`/home/mar/personal_projects/ocdroid/opencode-src/current`（下文引用一律 `packages/...:行号`）。
> sidecar 引用 `src/oc_slimapi/...:行号`。方法：全文精读关键 ts 文件 + rg 全仓定位 schema/manifest 真值。
> 结论速览：sidecar 两处 casing TODO（tokenstream/hub.py:663/:760）**均成立**（上游恒 camelCase）；
> IMMEDIATE 集合含 **2 个幽灵类型**（`permission.resolved` / `permission.v2.resolved`，上游实为 `*.replied`）；
> `message.appended` 在上游事件全集中**不存在**。详见 §7/§8。

---

## 1. 消息分页 + cursor（message-v2.ts）

上游事实（`packages/opencode/src/session/message-v2.ts`）：

- **Cursor 结构**：`{id: MessageID(string), time: Schema.Finite >= 0}`（:63-66）。编码 = `JSON.stringify` 后 `base64url`（:71-78）；解码失败抛异常（handler 侧转 400）。
- **`older()` before 语义**：`or(lt(time_created, row.time), and(eq(time_created, row.time), lt(id, row.id)))`（:95-96）——严格小于 `(time_created, id)` 二元组，**不含边界**（同一 time 靠 id 字符串比较打破平局）。
- **`page()` 参数**：`{sessionID, limit, before?}`（:425-429）。
- **查询排序**：`orderBy desc(time_created), desc(id)`，**fetch `limit + 1` 行**探测 more（:435-442）。
- **more/游标产出**：`more = rows.length > limit`；slice 后 `items.reverse()` → **页内升序**（最旧在前）；`cursor = more ? encode({id: tail.id, time: tail.time_created}) : undefined`，tail = slice 最后一项（**下一页边界 = 本页最旧一条**）（:457-466）。
- **会话不存在 → 404**：rows 为空时先查 SessionTable，无行则 `NotFoundError("Session not found: ${sessionID}")`（:443-450）；会话存在但无消息 → 200 `{items: [], more: false}`（:451-454）。
- **单消息 get**：`NotFoundError("Message not found: ${messageID}")`（:506-514）；parts 按 `PartTable.id` 排序（:492-504）。
- **info 投影**：`{...row.data, id: row.id, sessionID: row.session_id}`（:80-85）；part 投影同理加 `id/sessionID/messageID`（:87-93）。
- **时间戳字段名**：DB 列 `time_created`；Info schema 里是 `time.created`（见 §5）。注释明说 **id 只是 tie-breaker，导入消息的 ID 不保证单调**（:578-581）；`latest()` 排序键 = `(time.created, id)`（:600-604）。

handler 侧边界（`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`，路由 `GET /session/:id/message`）：

- `before` 出现但 `limit` 未给 → **400 BadRequest**（:110）；`before` 解码失败 → **400**（:111-117）。
- `limit === undefined || limit === 0` → **全量列表**（不分页数组，`session.messages()`，:119-121）。
- 有 cursor 时响应头：`Access-Control-Expose-Headers: Link, X-Next-Cursor`、`Link: <url?limit=&before=>; rel="next"`、`X-Next-Cursor: <cursor>`（:130-144）。URL 从请求 URL 重建（尊重 Host + x-forwarded-proto），query 仅设 `limit`/`before` 两参。
- 全量 `session.messages()`：50/页循环整扫，最终 **升序（时间正序）**（`packages/opencode/src/session/session.ts:830-853`，页间倒序累积后 `result.reverse()`）；带 `limit` 时 = 首页（该 limit 最新的 N 条，页内升序，:831-835）。

## 2. session handlers

### 2.1 legacy httpapi（sidecar 实际对接面）

文件：`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`；schema 在 `.../groups/session.ts`。

- **路由表**（groups/session.ts:78-105）：list `GET /session`；messages `GET /session/:id/message`；单消息 `GET /session/:id/message/:messageID`；create `POST /session`；remove `DELETE /session/:id`；update `PATCH /session/:id`；等。
- **ListQuery**（groups/session.ts:30-38）：`scope?: "project"`、`path?`、`roots?`(bool)、`start?`(number)、`search?`、`limit?` + workspace routing 字段。成功 schema = `Schema.Array(Session.Info)`（:111-115）——**裸数组**。
- **MessagesQuery**（:43-47）：`limit?`（int ≥ 0 的 NumberFromString）、`before?`（string）。
- **PATCH 双 shape**：`UpdatePayload = {title?: string, metadata?: Record, permission?: Ruleset, time?: {archived?: ArchivedTimestamp}}`（:49-58）。handler（handlers/session.ts:183-204）：四个字段 **`!== undefined` 才各自生效**；`permission` 是 **merge 语义**（`Permission.merge(current.permission ?? [], payload.permission)`，:194-199）不是整体替换；`time.archived` → `session.setArchived({time})`（:200-202）；**返回 PATCH 后的完整 Session Info**（:203）。
  - `ArchivedTimestamp = Schema.Finite`，注释明示 **legacy 允许负值、仅排除非有限数**（session.ts:198-200）；即 `time.archived` 是**数字**（非字符串），可为 0/负/小数；PATCH 侧 `archived` 为 optional，**无法通过该端点显式清档**（handler 只在 `!== undefined` 时调 setArchived）。
  - `setArchived` → `patch(sessionID, {time: {archived: input.time}})`；`patch` 为浅合并并 **publish `session.updated`（携带完整合并后 info）**（session.ts:736-749, :759-761）。
- **DELETE**：handler `session.remove()` 后 **返回 `true`**（handlers/session.ts:178-181）。`remove` 语义（session.ts:608-629）：先递归 `children()` 逐个 `remove(child.id)`（:619-622，**每个子会话各自 publish 一条 `session.deleted`**）；父会话 publish `session.deleted {sessionID, info}` 后 `events.remove`（:624-625）；**整个体包 try/catch，异常仅 logError 吞掉**（:626-628）——即「递归删子 + 吞错」属实；删除前先取消运行中的 background jobs（:618，:940-955）。
- **404 形状**：所有 NotFound 经 `mapStorageNotFound` → `ApiNotFoundError`（handlers/session-errors.ts:6-8）。`ApiNotFoundError` 字段 = `{name: "NotFoundError", data: {message: string}}`，HTTP 404（`.../httpapi/errors.ts:169-188`，构造调用 :188-193）。message 文本来自存储层（"Session not found: xxx" / "Message not found: xxx"）。
- 单消息 GET = `MessageV2.get`（handlers/session.ts:147-153）。

### 2.2 新 server 组（packages/server，v2 面，对照用）

文件：`packages/server/src/handlers/session.ts`（`server.session` 组，非 sidecar 对接面）：

- `session.list`：cursor 经 `SessionsCursor` 解析（无效 → `InvalidCursorError`，:27-32）；默认 limit 50（:16）；返回 `{data: sessions[], cursor: {previous?, next?}}`，cursor anchor = `{id, time: DateTime.toEpochMillis(time.created), direction}`（:38-64）。
- `session.get` 404 → `SessionNotFoundError {sessionID, message}`（:91-105）；`session.message` 404 → `MessageNotFoundError`（:373-383）。
- `session.active` 返回 `{[sessionID]: {type: "running"}}` —— **对象信封**（:80-89）。

## 3. event / SSE handlers（/global/event 帧形）

### 3.1 `/global/event`（sidecar 唯一上游订阅源）

文件：`packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts` + `groups/global.ts`；总线 `packages/opencode/src/bus/global.ts`。

- **帧信封**：`GlobalEventSchema = {directory: string, project?: string, workspace?: string, payload: {id: EventID, type: <literal>, properties: <definition.data>} | InstanceDisposed | SyncEvent}`（groups/global.ts:35-48）。**业务字段一律在 `payload.properties` 下**（`:42` 明确 `properties: definition.data`）。
- **SSE 编码**：每帧 `event: message` + `data: <JSON>`，**无 `id:` 行**（handlers/global.ts:16-23）。响应头 `text/event-stream` / `Cache-Control: no-cache, no-transform` / `X-Accel-Buffering: no` / `X-Content-Type-Options: nosniff`（:56-63）。
- **首帧**：`{payload: {id, type: "server.connected", properties: {}}}` —— **注意：首帧/心跳帧没有 `directory` 键**（:49）。
- **心跳**：每 10s `{payload: {id, type: "server.heartbeat", properties: {}}}`，同样无 directory（:43-45）。是**数据帧**（`data:` JSON），不是 SSE 注释。
- **GlobalBus 自动补 id**：payload 无 `id` 键时自动生成 `evt_` 前缀升序 id（bus/global.ts:12-16）。事件 id 前缀 = `evt_`（`packages/schema/src/event.ts:9-13`）。
- **directory 注入**：EventV2Bridge 把实例 directory 挂到事件 location 并 `GlobalBus.emit("event", {directory, payload})`（`packages/opencode/src/event-v2-bridge.ts:19-47`）。

### 3.2 `/event`（实例级，非 sidecar 消费，对照）

`.../httpapi/handlers/event.ts`：帧 = `{id, type, properties}`（map :40），首帧 server.connected（:70），10s 心跳（:63-66）；按 `location.directory === instance.directory` 过滤（:36-39）；special-case 注入 `server.instance.disposed`（:42-58；类型定义 `packages/opencode/src/server/event.ts`：`{id, type: "server.instance.disposed", properties: {directory}}`）。新 server 组 `packages/server/src/handlers/event.ts`：15s **SSE 注释心跳** `": heartbeat\n\n"`（:37），数据帧经 `OpenCodeEvent` schema 编码（:16）。

### 3.3 token part 载荷定论（sidecar 两处 TODO 的答案）

**结论：上游恒 camelCase，sidecar 假设全部成立。**

- **`message.part.updated`**：schema `{sessionID: SessionID, part: Part, time: Finite}`（`packages/schema/src/v1/session.ts:612-620`）。`Part` 基座 = `{id: PartID, sessionID: SessionID, messageID: MessageID}`（:81-85，**camelCase**，12 类 part 共用）。发布方 `updatePart`：`events.publish(PartUpdated, {sessionID, part: structuredClone(part), time: Date.now()})`（`packages/opencode/src/session/session.ts:637-645`）→ sidecar `tokenstream/hub.py:663` 读 `properties.part.{sessionID,messageID,id}` **正确**。
- **`message.part.delta`**：schema `{sessionID, messageID, partID, field: string, delta: string}`（session.ts(v1 schema):632-641，**flat + camelCase**）。发布方 `updatePartDelta` 原样转发 `{sessionID, messageID, partID, field, delta}`（opencode session.ts:879-887）→ sidecar `tokenstream/hub.py:760` 读法**正确**。
- **`field` 值域**：全仓仅两处调用 `updatePartDelta`，均 `field: "text"`（`packages/opencode/src/session/processor.ts:299-307, :503-511`）——sidecar 的 `field != "text"` 门与现存全部发布者一致（schema 本身 field 是开放 string，未来新增字段名会被静默丢，属契约级观察）。
- **`message.part.removed`**：flat `{sessionID, messageID, partID}`（schema :621-629；removePart 发布 opencode session.ts:866-877）——sidecar flat 提取正确。
- **`message.removed`**：flat `{sessionID, messageID}`（schema :604-611；removeMessage 发布 :855-864）。
- ID 前缀：MessageID `msg_`、PartID `prt_`（schema :17-27）。

### 3.4 session.status 形状定论

- `session.status` schema = `{sessionID: SessionID, status: Info}`，`Info` 是**对象 union**：`{"type":"idle"}` | `{"type":"retry", attempt, message, action?, next}` | `{"type":"busy"}`（`packages/schema/src/session-status-event.ts:9-41`）。**裸字符串形状在上游 schema 中不存在**；值域恰为 idle|retry|busy。
- `/session/status` 端点同用 `SessionStatus.Info`（groups/session.ts:48 `StatusMap = Record(String, SessionStatus.Info)`）。
- sidecar `normalize_session_status` 的 dict-`type` 分支（hub_types.py:398-399）与上游对象信封对齐；**str 直通分支无上游出处**（见 §8-3）。
- 附：`session.idle` 仍定义但标记 deprecated（session-status-event.ts:43-51）。

### 3.5 session.updated / session.deleted / session.error

- `session.updated` / `session.deleted` / `session.created` properties = `{sessionID, info: SessionInfo}`（schema :571-595）；`patch()` 每次 set* 都 publish Updated 且 info 为完整合并后对象（opencode session.ts:736-749）→ sidecar 读 `properties.info.time.archived` 路径正确。
- `session.error` properties = `{sessionID?: SessionID（**optional**）, error: AssistantError}`（schema :651-657）。发布方 prompt.ts 多处、promptAsync handler（`{sessionID, error}` / 无 sid 变体），实测均带 sessionID（prompt.ts:318,466,604,642,894,916,1175,1306；handlers/session.ts:320-323）。sidecar「sid 仅取 `props.sessionID`」与 schema 一致（session.error 无 info 字段，`_extract_session_id` 的 info 兜底本就不适用）。
- 错误对象形状：`{name: <literal>, data: {...}}`（namedError 构造，schema :29-34）；abort 名 = `"MessageAbortedError"`（:43）——sidecar `ABORT_NAME` 正确。

## 4. 事件类型全集（manifest 真值）

入口链：`packages/schema/src/event-manifest.ts`（`Definitions` :63-82 = foundation + live + feature + 其余全部组；`Latest = Event.latest(Definitions)` :83）。`/global/event` 的 payload union 用 `EventManifest.Latest.values()`（groups/global.ts:40-43）。`define()` 产物字段 = `{id, metadata?, type, durable?, location?, data}`（`packages/schema/src/event.ts:42-70`）。

**全集（88 型，按模块）**：

| 模块（schema/src/...） | 事件类型 | 行号 |
|---|---|---|
| v1/session.ts（durable，7） | session.created / session.updated / session.deleted / message.updated / message.removed / message.part.updated / message.part.removed | :572-629 |
| v1/session.ts（live，3） | message.part.delta / session.diff / session.error | :632-657 |
| session-status-event.ts（2） | session.status / session.idle(deprecated) | :35-49 |
| session-event.ts（32） | session.next.{agent.switched, model.switched, moved, prompted, prompt.admitted, context.updated, synthetic, shell.started, shell.ended, step.started, step.ended, step.failed, text.started, text.delta, text.ended, reasoning.started, reasoning.delta, reasoning.ended, tool.input.started, tool.input.delta, tool.input.ended, tool.called, tool.progress, tool.success, tool.failed, retried, compaction.started, compaction.delta, compaction.ended, revert.staged, revert.cleared, revert.committed}（v2 引擎事件） | :55-442 |
| models-dev.ts（1） | models-dev.refreshed | :6 |
| integration.ts（2） | integration.updated / integration.connection.updated | :80-84 |
| catalog.ts（1） | catalog.updated | :5 |
| filesystem.ts（1） | file.edited | :9 |
| reference.ts（1） | reference.updated | :8 |
| permission.ts（2，v2） | **permission.v2.asked / permission.v2.replied** | :43-45 |
| v1/permission.ts（2，v1） | **permission.asked / permission.replied** | :61-65 |
| question.ts（3，v2） | question.v2.asked / question.v2.replied / question.v2.rejected | :70-80 |
| v1/question.ts（3，v1） | question.asked / question.replied / question.rejected | :58-60 |
| plugin.ts（1） | plugin.added | :10 |
| project-directories.ts（1） | project.directories.updated | :7 |
| filesystem-watcher.ts（1） | file.watcher.updated | :7 |
| pty.ts（4） | pty.created / pty.updated / pty.exited / pty.deleted | :34-37 |
| session-todo.ts（1） | todo.updated | :19 |
| installation-event.ts（2） | installation.updated / installation.update-available | :7-14 |
| lsp-event.ts（1） | lsp.updated | :5 |
| tui-event.ts（4） | tui.prompt.append / tui.command.execute / tui.toast.show / tui.session.select | :11-53 |
| mcp-event.ts（2） | mcp.tools.changed / mcp.browser.open.failed | :7-14 |
| v1/legacy-event.ts（1） | command.executed | :5-13 |
| project.ts（1） | project.updated | :43 |
| session-compaction-event.ts（1） | session.compacted | :7 |
| vcs-event.ts（1） | vcs.branch.updated | :8 |
| workspace-event.ts（3） | workspace.ready / workspace.failed / workspace.status | :14-28 |
| worktree-event.ts（2） | worktree.ready / worktree.failed | :8-16 |
| server-event.ts（2） | server.connected / global.disposed | :5-6 |

另有 handler 合成的非 manifest 帧：`server.heartbeat`（/global/event、/event 各自注入）、`server.instance.disposed`（仅 /event special-case，`packages/opencode/src/server/event.ts` InstanceDisposed）。

**sidecar 16 类分帧 vs 全集**：IMMEDIATE 6 + SESSION 3 + MESSAGE 2 + session.error + token 4 = 16。其中 `permission.resolved`、`permission.v2.resolved` **不在全集**（上游为 `*.replied`）；`message.appended` **不在全集**。反之被 catch-all 静默丢弃的真实类型包括（客户端可见性影响待 draft 评估）：`session.created`（legacy create 恒发布，opencode session.ts:537）、`permission.replied`/`permission.v2.replied`/`question.replied`/`question.rejected`（v1+v2 共 6 个决议事件）、`session.idle`、`session.diff`、`session.compacted`、`todo.updated`、`file.edited`、`pty.*`、`command.executed`、`installation.*`、`integration.*`、`models-dev.refreshed`、`catalog.updated`、`project.*`、`lsp.updated`、`mcp.*`、`tui.*`、`vcs.branch.updated`、`reference.updated`、`plugin.added`、`workspace.*`、`worktree.*`、`session.next.*`×32（v2 引擎）。

## 5. session 核心 / schema

### 5.1 packages/core/src/session.ts（SessionV2 服务；AGENTS.md 指的 list() 在此）

- `list()`（:268-303）：默认 `order=desc`、direction=next；排序键 **`(time_created, id)`**（asc/desc 随 order，:295-298）；anchor = keyset 严格大于/小于（:278-290）；过滤条件仅 `directory`/`workspaceID`/`project`/`search`(title LIKE)（:274-277）——**无 archive 过滤**；limit 未给 = 全量（:299）；direction=previous 时行序反转返回（:302）。
- `get()`（:263-267）NotFound → `{sessionID}`。
- `messages()`（:304-337）：cursor 按 `SessionMessageTable.seq`（事件表 seq，非 time_created）；cursor 指向不存在 id → 返回 `[]`（:319）。
- create：tokens 初始 `{input:0, output:0, reasoning:0, cache:{read:0, write:0}}`、time `{created: now, updated: now}`（:237-239）。

### 5.2 legacy 服务（sidecar /session 对接的实现层）

文件：`packages/opencode/src/session/session.ts`。

- `listByProject`（:957-1010，`GET /session` 实现）：恒按 project_id 过滤；可选 workspaceID / path（eq + `path/%` LIKE，directory 兜底）/ directory（scope=project 时跳过）/ roots（`parent_id IS NULL`）/ start（`time_updated >= start`）/ search（title LIKE）；**排序 `desc(time_updated)`，无 id tiebreaker**（:1003）；**默认 limit 100**（:997）；**不过滤 archived**。
- `listGlobal`（:557-596）：额外 **默认排除 archived**（`!input.archived → time_archived IS NULL`，:564）；排序 `desc(time_updated), desc(id)`（:574）；返回 GlobalInfo = Info + `project`（可 null）。
- `create`（:537）publish `session.created`；`patch`（:736-749）见 §2.1。
- `children`（:598-606）：`parent_id = X` 直查。

### 5.3 SessionInfo 字段全集（v4 §13 parity 真值）

`packages/schema/src/v1/session.ts:543-568`（legacy 镜像 `packages/opencode/src/session/session.ts:224-245` + `fromRow` :59-105）：

| 字段 | 类型 | 可空性/备注 |
|---|---|---|
| id | SessionID（`ses_` 前缀 brand） | 必填 |
| slug | string | 必填 |
| projectID | Project.ID | 必填 |
| workspaceID | WorkspaceID | optional |
| directory | string | 必填 |
| path | string | optional |
| parentID | SessionID | optional |
| summary | {additions: Finite, deletions: Finite, files: Finite, diffs?: FileDiff[]} | optional（fromRow：三个标量全 NULL 才省略，:60-68） |
| cost | Finite | optional（fromRow 恒有值） |
| tokens | {input: Finite, output: Finite, reasoning: Finite, cache: {read: Finite, write: Finite}} | optional；**SessionTokens 无 total 字段**（:516-524；fromRow 恒构造，:98-105） |
| share | {url: string} | optional |
| title | string | 必填（默认 "New session - "/"Child session - "+ISO 时间，isDefaultTitle :51-55） |
| agent | string | optional |
| model | {id: Model.ID, providerID: Provider.ID, variant?: string} | optional |
| version | string | 必填（InstallationVersion） |
| metadata | Record<string, any> | optional |
| time | {created: NonNegativeInt, updated: NonNegativeInt, compacting?: NonNegativeInt, archived?: **Finite**} | time 必填；created/updated 恒在；**archived=Finite（数字 ms，允许负/小数，非字符串）**（:560-565 + session.ts:198-206） |
| permission | PermissionV1.Ruleset（Rule 数组） | optional |
| revert | {messageID, partID?, snapshot?, diff?} | optional |

消息侧（同文件）：`User`（:332-354）`time.created: Timestamp(Finite≥0)`；`Assistant`（:453-485）`time {created: NonNegativeInt, completed?}`、`tokens {total?: Finite, input, output, reasoning, cache{read, write}}`（**total 仅 Assistant 有且 optional**，:472-481）、`cost: Finite`、`parentID` 必填、`error?`（named union，discriminator=name）、`finish?`、`mode`、`path{cwd, root}`、`structured?`、`variant?`。`StepFinishPart.tokens` 同 Assistant（含 optional total，:246-255）。Part 全集 12 类（:357-370）：text/subtask/reasoning/file/tool/step-start/step-finish/snapshot/patch/agent/retry/compaction。

## 6. config / providers

- **端点**：`GET /provider` → `Provider.ListResult = {all: Info[], default: Record<string,string>, connected: string[]}`（groups/provider.ts + `packages/opencode/src/provider/provider.ts:1066-1071`；handler 组装 `.../handlers/provider.ts:40-63`：catalog 全集 ∪ connected，`all` 逐项 `toPublicInfo`）。`GET /config/providers` → `ConfigProvidersResult = {providers: Info[], default: Record<string,string>}`（provider.ts:1073-1077；`.../handlers/config.ts:24-29`，**仅 connected providers**）——sidecar v4 §12 投影输入形状与此一致。
- **Provider.Info**（provider.ts:1053-1062）：`{id, name, source: "env"|"config"|"custom"|"api", env: string[], key?: string, options: Record<string,any>, models: Record<string, Model>}` —— **api/key/env/options 字段确实存在**（= v3 透传暴露面真值）。注意：catalog 来源（fromModelsDevProvider :1282-1289）`source="custom"`、`options={}`、**无 key**；`key` 只有 connected（env/config）provider 才带。
- **Provider.Model**（provider.ts:1036-1050）：`{id, providerID, api: ProviderApiInfo, name, family?, capabilities, cost, limit: ProviderLimit, status, options: Record, headers: Record<string,string>, release_date: string, variants?}`；`ProviderLimit = {context: Finite, input?: Finite, output: Finite}`（:1030-1034）。
- **Model（v2 schema）limit**：`{context: Schema.Int, input?: Schema.Int, output: Schema.Int}`（`packages/schema/src/model.ts:81-85`）——**v4 契约 §12 所引行号与形状成立**（schema 层 Int；provider 投影层 Finite）。
- **default map 真值**：`defaultModelIDs` = 每 provider 取 `sort(models)[0].id`（provider.ts:1095-1097）；`sort` 比较器 = 优先级子串匹配 `["gpt-5","claude-sonnet-4","big-pickle","gemini-3-pro"]` desc → `latest` 优先 → id desc（:1986-1995）。default 值域 = **该 provider 的 model id 字符串**；key = provider id。确定性由该比较器冻结。
- **toPublicInfo**（:1079-1093）：JSON round-trip——过滤 function/symbol/undefined、bigint→string；models 先过 Model schema 校验（不过校验的模型被**剔除**）。

## 7. 假设 ↔ 出处对照表（核心交付）

sidecar 对上游形状的假设点 → 上游 file:line 出处。✓=假设成立；△=部分成立/有细节偏差；✗=无出处（转 §8）。

| # | sidecar 假设（位置） | 上游出处 | 判定 |
|---|---|---|---|
| 1 | part.updated 读 `properties.part.{sessionID,messageID,id}` camelCase（tokenstream/hub.py:663 TODO） | schema/src/v1/session.ts:81-85（partBase）、:612-620（PartUpdated）；opencode session/session.ts:637-645（发布） | ✓ |
| 2 | part.delta 读 flat `{sessionID,messageID,partID,field,delta}` camelCase（tokenstream/hub.py:760 TODO） | schema/src/v1/session.ts:632-641；opencode session/session.ts:879-887；field 现存发布者恒 "text"（processor.ts:299-307,:503-511） | ✓ |
| 3 | part.removed flat `{sessionID,messageID,partID}`（global_hub.py:936 附近） | schema/src/v1/session.ts:621-629；opencode session.ts:866-877 | ✓ |
| 4 | message.removed flat `{sessionID,messageID}`（global_hub.py retired gate 写入） | schema/src/v1/session.ts:604-611；opencode session.ts:855-864 | ✓ |
| 5 | `normalize_session_status` 对象信封 `{"type":...}`（hub_types.py:398-399 dict 分支） | schema/src/session-status-event.ts:9-41（对象 union idle/retry/busy） | ✓ |
| 6 | `normalize_session_status` 裸字符串直通分支（legacy "busy"） | 上游 schema 无裸字符串形状 | ✗（§8-3） |
| 7 | digest.status 值域未冻结 / busy 精确匹配清 sticky（global_hub.py:720） | 值域 = idle\|retry\|busy（session-status-event.ts:9-32）；大小写恒小写 literal | ✓（值域可冻结为三值） |
| 8 | session.updated 读 `properties.info.time.archived`，int 非 bool 含 0（global_hub.py:744-762） | schema/src/v1/session.ts:560-565（optional(Finite)）；opencode session.ts:198-206（Finite，**允许负/小数**）、:736-749（patch 全量 info publish） | △（Finite⊃int：非整数 archived 会被 sidecar isinstance(int) 丢弃；实践为 Date.now() 整数） |
| 9 | session.error sid 仅取 `props.sessionID`（global_hub.py:804-805） | schema/src/v1/session.ts:651-657（sessionID optional，无 info 字段）；prompt.ts:318 等 8 处均带 sid | ✓ |
| 10 | `ABORT_NAME="MessageAbortedError"`（hub_types.py:33） | schema/src/v1/session.ts:43 | ✓ |
| 11 | message.updated 恒带 `props.sessionID`（`_extract_session_id` 首选） | schema/src/v1/session.ts:596-603（schema 必填）；opencode session.ts:631-635 | ✓ |
| 12 | `_extract_session_id` 兜底 `props.info.sessionID` / `props.info.id`（hub_types.py:349-373） | message.* 的 info 有 sessionID（schema :327-330）✓；session.* 的 info=SessionInfo **无 sessionID 有 id**（:543-568）——兜底 2 对两类均死码、兜底 3 仅 session.* 有效 | △（兜底 2 死码；首选 props.sessionID 恒存在使兜底不可达） |
| 13 | IMMEDIATE 含 `permission.resolved`/`permission.v2.resolved`（hub_types.py:73-77） | 上游无此二类型；实为 permission.replied（schema/src/v1/permission.ts:61-65）/ permission.v2.replied（schema/src/permission.ts:43-45） | ✗（§8-1） |
| 14 | MESSAGE_EVENTS 含 `message.appended`（hub_types.py:88-90，注释自认兼容保留） | 上游事件全集无此类型（§4） | ✗（§8-2） |
| 15 | session.status/deleted 镜像 token hub（global_hub.py:768-781） | schema（§3.4）+ status service 发布链 | ✓ |
| 16 | tokens 嵌套 `{input,output,reasoning,cache{read,write}}`（skeleton/metrics 消费） | schema/src/v1/session.ts:472-481（Assistant，**total optional**）、:246-255（step-finish，同）、:516-524（SessionTokens **无 total**） | ✓（注意三处 total 差异） |
| 17 | 消息列表排序（updatedAt, messageID）→ 上游真实排序 | DB 排序 `desc(time_created), desc(id)`（message-v2.ts:439）；wire 返回**升序**（页内 reverse :460；全量 :830-853）；排序键是 **time.created 不是 time.updated** | △（sidecar digest 的 updatedAt 是自产墙钟，与上游排序键不同源——契约层已自洽，非 bug） |
| 18 | cursor 透传（`_extract_before_verbatim` 不 decode；`_parse_link_next_cursor` 取 Link rel=next 的 before） | cursor = base64url(JSON{id,time})（message-v2.ts:63-78）；Link `<url?limit&before>; rel="next"` + X-Next-Cursor（handlers/session.ts:130-144） | ✓ |
| 19 | before 需配 limit；坏 cursor 400；limit=0 全量 | handlers/session.ts:110（400）、:111-117（400）、:119-121（全量） | ✓ |
| 20 | 上游 404 → `session_not_found` 映射所依据的形状 | ApiNotFoundError `{name:"NotFoundError", data:{message}}` 404（httpapi/errors.ts:169-193；session-errors.ts:6-8）；文本 "Session not found: …"（message-v2.ts:450）/ "Message not found: …"（:514） | ✓ |
| 21 | `/global/event` 帧 `{directory, payload:{id,type,properties}}`（global_hub.run 组帧） | groups/global.ts:35-48；handlers/global.ts:16-23；bus/global.ts:12-16（无 id 自动补 evt_） | ✓ |
| 22 | connected/heartbeat 帧（sidecar catch-all 丢弃 server.heartbeat/server.connected） | handlers/global.ts:43-49（10s、无 directory 键） | ✓（首帧/心跳确无 directory，sidecar `directory=None` 处理正确） |
| 23 | 非 dict JSON 帧防御缺失（e1-05 疑问 2：`[1,2]` 毒帧） | 上游恒发 `{...}` 对象帧（Sse.encode + eventData JSON.stringify）；但 GlobalEventSchema 校验是否在 wire 层强制未知——schema 声明在 API 组，SSE 流式 handler 手工构造未逐帧 decode | △（上游源码层恒对象；防线缺省属 sidecar 侧加固项） |
| 24 | default provider map（providers_projection §12.3 三元组校验依据） | provider.ts:1095-1097（defaultModelIDs）、:1986-1995（排序冻结）；handlers/config.ts:24-29（/config/providers 形状） | ✓ |
| 25 | providers 透传暴露面（v3 verbatim：api/key/env/options 等字段存在） | provider.ts:1053-1062（Info）、:1036-1050（Model 含 options/headers/release_date）、:1079-1093（toPublicInfo JSON round-trip） | ✓（catalog 项无 key：:1282-1289） |
| 26 | Model.limit `{context, input?, output}`（v4 §12 修订三） | schema/src/model.ts:81-85（Int）；provider.ts:1030-1034（Finite） | ✓ |
| 27 | session 列表默认排序/limit（sessions 投影依据） | listByProject：desc(time_updated) **无 id tiebreak**、limit 默认 100、不滤 archived（opencode session.ts:957-1010）；listGlobal：默认排 archived、desc(time_updated,id)（:557-596） | △（listByProject 同秒并列时顺序不确定） |
| 28 | PATCH 双 shape（title/metadata/permission/time.archived） | groups/session.ts:49-58；handlers/session.ts:183-204（permission=**merge**；返回全量 Info） | ✓ |
| 29 | DELETE 递归删子吞错、返回 true | opencode session.ts:608-629（递归+try/catch logError；每个子会话各发 session.deleted）；handlers/session.ts:178-181 | ✓ |
| 30 | 16 类分帧覆盖上游「需要策展」的完整集合 | manifest 全集 88 型 + 合成 3 型（§4）；sidecar 未覆盖且未计数的真实类型 ≥70（含 session.created、q/p 决议事件 6 型等） | △（漂移检测缺失的量化依据） |
| 31 | `session.created` 不策展（依赖 HTTP 冷同步） | legacy create 恒发布（opencode session.ts:537）；sidecar SESSION_EVENTS 无此类型 | △（设计取舍，draft 评估） |
| 32 | SSE 上游帧 `event: message` 名 + 无 id 行（replay/组帧假设） | handlers/global.ts:16-23（event:"message"，id: undefined） | ✓ |
| 33 | `/session/status` 端点形状（若 sidecar/客户端消费） | StatusMap = Record<String, SessionStatus.Info>（对象信封，groups/session.ts:48；handlers/session.ts:77-79） | ✓ |

## 8. 无出处假设（draft 发现种子）

1. **`permission.resolved` / `permission.v2.resolved` 幽灵类型**（hub_types.py:73-77 IMMEDIATE）：上游 v1.18.18 事件全集无此二类型；真实决议事件是 `permission.replied`（schema/src/v1/permission.ts:61-65）/ `permission.v2.replied`（schema/src/permission.ts:43-45），它们不在 IMMEDIATE，落 catch-all **被静默丢弃**（无计数无日志）。效果：q/p 直推只推「问」，「答」永不达客户端；`qp_last_activity` 对 replied 不刷新。同类：`question.replied`/`question.rejected`/`question.v2.replied`/`question.v2.rejected` 也全部丢弃。draft 应判定：是补 replied 进 IMMEDIATE 还是确认契约有意只推 asked。
2. **`message.appended` 幽灵类型**（hub_types.py:88-90 MESSAGE_EVENTS）：上游无此类型（新消息走 `message.updated`，opencode session.ts:631-635）；该分支为永久死码（sidecar 注释已自认，此处给出 manifest 级证据）。
3. **`normalize_session_status` 裸字符串分支无上游出处**（hub_types.py:398-399）：上游 session.status 的 status 恒为对象信封 `{"type":"idle"|"retry"|"busy"}`（session-status-event.ts:9-41），无任何发布者发裸字符串；str 直通分支为死路径（但也是无害防御）。附带收益：digest.status 值域可据上游 schema 冻结为 idle|retry|busy 三值。
4. （次级，记录不计数）**archived 类型窄化**：sidecar 按 `isinstance(int)` 收 archived（global_hub.py:761），上游 Finite 允许负数/小数（opencode session.ts:198-200 注释明示 legacy 接受负值）——负数可通过（int），**非整数 float 会被丢弃**；属理论边缘，是否值得放宽到 Finite 由 draft 裁决。

---

## 附：与 sidecar 修正相关的行号索引（sidecar 侧）

- `src/oc_slimapi/sse/hub_types.py:73-90`（IMMEDIATE/SESSION/MESSAGE 集合）、:33（ABORT_NAME）、:349-373（sid 提取）、:398-399（status 归一）
- `src/oc_slimapi/sse/global_hub.py:658-800`（publish 分类器）、:744-762（archived 透传）、:804-858（session.error）
- `src/oc_slimapi/sse/tokenstream/hub.py:663-671`（part casing TODO）、`:760-776`（delta casing TODO）
- `src/oc_slimapi/providers_projection.py`（§12 投影，形状依据 §6）
