# oc-slimapi v4 消费者协议导航

> **规范权威声明**：[`v4-contract.md`](v4-contract.md) 仍是 wire 规范权威。
> 本文件面向 webui / ocdroid，汇总当前路由、DTO、恢复算法与接入清单，
> 不是第二套规范。若本文、注释或历史设计与事实冲突，按以下顺序裁决：
> **`v4-contract.md` → 本文件 → 注释 / 历史设计**。生产代码与 golden tests
> 是实现符合契约的证据；若实现与权威契约冲突，应修复实现或走正式契约修订，
> 不得以实现现状覆盖契约。
> `v2-contract.md`、`v3-contract.md` 仅是历史存档。

本文描述当前 **v4-only** 服务面，共 56 个 `/slimapi` method/path 组合。
示例中的所有 `/slimapi/**` 请求均应带 `?v=4`，唯一例外是
`GET /slimapi/versions`。

---

## 1. 通用 wire 规则

### 1.1 版本、编码与本地边界

- 版本选择器只有 query `v`。合法请求值只有 `4`；旧
  `X-Slimapi-Version` 请求头不参与协商。
- 缺 `v`、`v=1/2/3/5...` → 400
  `{"code":"unsupported_version","supported":[4]}`；词法错误或重复异值
  → 400 `invalid_version_selector`。
- `GET /slimapi/versions` 不解析 `v`；其它 method → 405，`Allow: GET`。
- 未注册路径、旧 `/session/**`、`/event`、`/global/event` 与旧 slim 路径
  均在本地返回 404 `{"code":"thin_route_not_found"}`，不访问上游。
- JSON 响应是 UTF-8。普通 JSON/文本路由可按 `Accept-Encoding` 选择 gzip；
  表示随编码变化时发 `Vary: Accept-Encoding`。SSE 恒 identity、无 `Vary`。
- 成功与 sidecar 生成的错误默认 `Cache-Control: no-store`；SSE 使用
  `no-cache, no-transform` 与 `X-Accel-Buffering: no`。
- 消费方必须忽略未知 JSON key 与未知 SSE `data.type`。

### 1.2 错误 envelope 与退避

sidecar 生成的业务错误是扁平对象：

```ts
type SlimError = {
  code: string
  hint?: string
  limit?: number | string
  current?: number
  limitBytes?: number
  retry_after?: number
  [documentedContext: string]: unknown
}
```

- 400：selector/directory/业务输入错误。
- 403：directory allowlist 拒绝，常见 code `directory_not_allowed`。
- 404：`thin_route_not_found`、`session_not_found`、`expand_target_not_found`
  或上游受控 4xx 原文。
- 409：`action_confirm_required`；429：`action_throttled`（含 `Retry-After`）。
- 413：`request_too_large`、`response_too_large`、`message_too_large`、
  `expand_source_too_large`、`expand_fragment_too_large`、
  `provider_projection_limit`。
- 422：FastAPI 参数/body 结构校验，或 `param_version_mismatch`。
- 502：确定性上游状态/形状错误，如 `upstream_http_N`、
  `upstream_invalid_shape`、`provider_upstream_malformed`、`raw_decode_failed`。
- 503：瞬态资源/上游/投影源错误，如 `upstream_unavailable`、
  `transform_busy`、`auxiliary_unavailable`、SSE subscriber limit。
- `Retry-After`：`transform_busy` 通常 2 秒；subscriber limit 5 秒；
  `auxiliary_unavailable` 30 秒；action throttle 使用响应给出的秒数。

受控 passthrough 路由的上游 4xx 可保留上游 status/body；不要假设所有
4xx 都有 `code`。上游 5xx 与网络错误不会原样暴露，统一收敛为 503。

### 1.3 ETag / 304 / Link

- identity JSON/文本表示使用强 ETag，gzip 表示使用弱 ETag `W/"…"`。
- 客户端可保存 200 body + ETag，并用 `If-None-Match` 弱比较重放；命中
  304 无 body，保留 `ETag`、`Vary: Accept-Encoding`、`Cache-Control:no-store`。
- `/slimapi/messages/{sid}` 的 `nextCursor` 来自上游 `Link rel="next"`
  中的 opaque `before`，但 sidecar 不向客户端透传上游 `Link` 头。
- `/full`、expand、SSE、write、versions 不提供 ETag/304。
- read-passthrough ETag 域保留历史 representation label，只影响 validator
  稳定性，不表示仍支持旧 wire。

### 1.4 directory selector 与错误优先级

客户端 canonical 通道只有 `?directory=`；入站 `X-Opencode-Directory`
已退役。selector 的前置优先级为：

1. method 405；
2. `v` selector 错误；
3. repeated `directory` 归一化后异值 → `invalid_directory_selector`；
4. query 与 header 归一化后异值 → `directory_conflict`；
5. header-only 或 query+同值 header → `directory_header_retired`；
6. 路由/业务错误；
7. 未注册路径 → `thin_route_not_found`。

路由集合：

- **消费 query selector**：messages list/full/expand、file/file-content/file-status/
  file-raw、vcs/vcs-status/vcs-diff、find-file、providers、session single、todo、
  children、diff、catalog 与部分 legacy write。
- **global sessions**：任何 directory 形态 → 400 `directory_retired_in_v4`。
- **token stream**：query-only 合法但仅校验、fanout no-op；header 按上表退役。
- **tolerant-ignore**：session context、sessions/details、session agent/model 与
  revert 三段式等按 sid 自路由的端点剥离 directory，不转发也不报错。

部署型 `OC_SLIMAPI_DIRECTORY_ALLOWLIST` 未配置时不改变 wire；显式空或非空
时会过滤 directory catalog/global SSE，并使 file 族超出范围的请求 fail-closed
为 403 `directory_not_allowed`。

---

## 2. 核心 DTO

### 2.1 Session

```ts
type SessionSkeletonV4 = {
  id: string
  directory: string
  parentID: string | null
  projectID: string | null
  project?: { id: string; name?: string; worktree: string } | null
  title: string
  agent: string | null
  model: { id: string; providerID: string; variant?: string } | null
  time: { created: number; updated: number; archived: number | null }
  summary: { additions: number; deletions: number; files: number } | null
  tokens_input: number | null
  tokens_output: number | null
  tokens_reasoning: number | null
  tokens_cache_read: number | null
  tokens_cache_write: number | null
  revert: { messageID: string; partID?: string } | null
  partial: boolean
  degraded: boolean
}

type SessionsV4 = {
  items: SessionSkeletonV4[]
  nextCursor: string | null
  complete: boolean
  degraded: boolean
}
```

`project` **absent iff** `projectID == null`；`projectID` 非空但 join 不可用时
`project:null, partial:true, degraded:true`。业务合法 null 不自动触发 partial。
`id/directory/title/time.created/time.updated` 不可表示时整响应 503，不发占位值。
当前没有 `/slimapi/projects` 路由；project/workspace UI 的事实源是这里的
`projectID/project`、session `directory` 与 `/slimapi/directories` catalog，不能把
历史 `/projects` discovery 当作可探测 fallback。

### 2.2 Message list / since

```ts
type MessagesPage = {
  items: MessageSkeleton[]
  nextCursor: string | null
  nextSince?: string
  removed?: string[]
}
```

`items` 按 `info.time.created` 升序。`before` 是 opaque 向后分页 cursor。
`since` 是进程域前向差分 token：有效 baseline 时 `items` 只含新增/指纹变化，
`removed` 只在有删除时出现；reset 返回正常 200 全量 + 新 `nextSince`，没有
单独 reset flag。`nextSince` 缺席时丢弃旧 token，下轮做全量。

`MessageSkeleton` 的当前精确外形是：

```ts
type MessageSkeleton = {
  info: UserMessageInfo | AssistantMessageInfo
  parts: CompactPart[]
  contentFingerprint?: string
}
type UserMessageInfo = {
  id: string; sessionID: string; role: "user"
  time: { created: number }
  format?: unknown
  summary?: { title?: string; body?: string; diffs: null }
  agent: string
  model: { providerID: string; modelID: string; variant?: string }
  system?: string
  tools?: Record<string, boolean>
  expandRefs?: ExpandRef[]
}
type AssistantMessageInfo = {
  id: string; sessionID: string; role: "assistant"
  time: { created: number; completed?: number }
  error?: unknown
  parentID: string
  modelID: string; providerID: string; mode: string; agent: string
  path: { cwd: string; root: string }
  summary?: boolean
  cost: number
  tokens: {
    total?: number; input: number; output: number; reasoning: number
    cache: { read: number; write: number }
  }
  structured?: unknown
  variant?: string; finish?: string
  expandRefs?: ExpandRef[]
}
```

sidecar 保留当前 upstream role union，只移除外来 `expandRefs`、把
`summary.diffs` 置为 `null`，并按需写入自己的 `expandRefs`。`error`、
`structured` 是 upstream-owned JSON，客户端必须宽容解析；上列是当前 upstream
schema 的允许字段。

compact part 只在 upstream 原对象存在时保留公共键
`id,type,messageID,sessionID`；按 type 可再带：

```ts
type CompactPart =
  | ({ type: "text"; text: string } & CompactMeta)
  | ({ type: "reasoning"; text: string | null } & CompactMeta)
  | ({ type: "tool"; tool?: string; callID?: string; state?: CompactToolState } & CompactMeta)
  | ({ type: "patch"; hash?: string; files?: PatchFile[]; filesTotal?: number; state?: object } & CompactMeta)
  | ({ type: "file"; filename?: string; mime?: string; url?: string } & CompactMeta)
  | ({ type: "step-start" | "step-finish" } & CompactMeta)
  | ({ type: "compaction"; auto?: boolean; overflow?: boolean; tail_start_id?: string } & CompactMeta)
  | CompactMeta
type CompactMeta = {
  id?: string; type?: string; messageID?: string; sessionID?: string
  hasFull?: true; omitted?: string[]; expandRefs?: ExpandRef[]
}
type CompactToolState = {
  status?: "pending" | "running" | "completed" | "error"
  title?: string
  time?: { start?: number; end?: number; compacted?: number }
  input?: object | null; metadata?: object | null
  output?: string | null; error?: string | null; outputBytes?: number
  diffStats?: { additions?: number; deletions?: number; files?: number }
  files?: PatchFile[]; filesTotal?: number
}
type PatchFile = { path: string; additions?: number; deletions?: number; status?: string }
```

text part 的 `text` 当前始终内联。大 reasoning、tool
output/error/input/metadata/attachments、file url/source、snapshot、compaction、
summary diffs 可用：

```ts
type ExpandRef = {
  category: ExpandCategory
  messageID: string
  partID?: string
  href: string          // 当前恒含 v=4；客户端按需追加 directory
}
```

tool state compact 投影保留可渲染摘要；省略字段用 `hasFull`, `omitted`,
`expandRefs` 表达。patch `files` 最多 10 个，超限时有 `filesTotal`。≤64KiB 的
compaction part 可保留完整 upstream part；更大的 compaction 和未知 part type
只保留公共键并标 `omitted:["*"]`。没有可渲染 part 时服务端补 synthetic
placeholder text part。
`contentFingerprint` 是内容变化信号，不是跨 session 序号。

`GET .../messages/{mid}/full` 返回未阈值化的当前 upstream message-with-parts：

```ts
type MessageWithParts = {
  info: FullUserMessageInfo | FullAssistantMessageInfo
  parts: FullPart[]
}
type FullUserMessageInfo = Omit<UserMessageInfo, "summary" | "expandRefs"> & {
  summary?: { title?: string; body?: string; diffs: FileDiff[] }
}
type FullAssistantMessageInfo = Omit<AssistantMessageInfo, "expandRefs">
type FileDiff = {
  file: string; patch?: string; additions: number; deletions: number
  status?: "added" | "deleted" | "modified"
}
type PartBase = { id: string; sessionID: string; messageID: string }
type FullFilePart = PartBase & {
  type: "file"; mime: string; filename?: string; url: string; source?: FileSource
}
type FullPart =
  | (PartBase & { type: "snapshot"; snapshot: string })
  | (PartBase & { type: "patch"; hash: string; files: string[] })
  | (PartBase & { type: "text"; text: string; synthetic?: boolean; ignored?: boolean; time?: { start: number; end?: number }; metadata?: object })
  | (PartBase & { type: "reasoning"; text: string; metadata?: object; time: { start: number; end?: number } })
  | FullFilePart
  | (PartBase & { type: "agent"; name: string; source?: { value: string; start: number; end: number } })
  | (PartBase & { type: "compaction"; auto: boolean; overflow?: boolean; tail_start_id?: string })
  | (PartBase & { type: "subtask"; prompt: string; description: string; agent: string; model?: { providerID: string; modelID: string }; command?: string })
  | (PartBase & { type: "retry"; attempt: number; error: object; time: { created: number } })
  | (PartBase & { type: "step-start"; snapshot?: string })
  | (PartBase & { type: "step-finish"; reason: string; snapshot?: string; cost: number; tokens: TokenUsage })
  | (PartBase & { type: "tool"; callID: string; tool: string; state: ToolState; metadata?: object })
type FileSource =
  | { type: "file"; path: string; text: { value: string; start: number; end: number } }
  | { type: "symbol"; path: string; range: { start: Position; end: Position }; name: string; kind: number; text: { value: string; start: number; end: number } }
  | { type: "resource"; clientName: string; uri: string; text: { value: string; start: number; end: number } }
type Position = { line: number; character: number }
type TokenUsage = { total?: number; input: number; output: number; reasoning: number; cache: { read: number; write: number } }
type ToolState =
  | { status: "pending"; input: object; raw: string }
  | { status: "running"; input: object; title?: string; metadata?: object; time: { start: number } }
  | { status: "completed"; input: object; output: string; title: string; metadata: object; time: { start: number; end: number; compacted?: number }; attachments?: FullFilePart[] }
  | { status: "error"; input: object; error: string; metadata?: object; time: { start: number; end: number } }
```

该 `/full` 形状不带 sidecar `expandRefs/contentFingerprint/hasFull/omitted`，也不做
compact 阈值化；sidecar 只从 tool metadata（含 attachments）递归剥离
`diagnostics`。`format`、assistant `error/structured`、tool metadata 与 retry error
仍是 upstream-owned JSON。

### 2.3 Expand

```ts
type ExpandMessage = { category: string; messageID: string; data: object }
type ExpandPart = ExpandMessage & { partID: string }
```

| category | level / part type | `data` |
|---|---|---|
| `info_summary_diffs` | message | `{diffs: object[] | null}` |
| `part_text` | text | `{text: string | null}` |
| `part_reasoning` | reasoning | `{text: string | null}` |
| `part_state_output` | tool | `{output: string | null}` |
| `part_state_error` | tool | `{error: string | null}` |
| `part_state_input_full` | tool | `{input: object | null}` |
| `part_state_metadata_full` | tool | `{metadata: object | null}`，无 diagnostics |
| `part_state_attachments` | tool | `{attachments: object[] | null}` |
| `part_url` | file | `{url: string | null}` |
| `part_source` | file | `{source: object | null}` |
| `part_snapshot` | step-start/step-finish | `{snapshot: string | null}` |
| `compaction_full` | compaction | 完整 compaction part，剥 `expandRefs` |

缺失与显式 null 都返回对应 key=null；目标 part 已删返回
`expand_target_not_found`，类型变化返回 `expand_category_mismatch`。

### 2.4 Providers / catalogs

```ts
type ProviderResult = {
  providers: Array<{
    id: string
    name: string
    source?: string
    models: Array<{
      id: string
      name: string
      providerID: string
      status?: string
      variants?: string[]
      limit?: { context?: number; input?: number; output?: number }
    }>
  }>
  default: Record<string, string>
}

type AgentSkeleton = {
  name: string; description: string; mode: string
  hidden?: boolean; native?: boolean
}
type CommandSkeleton = {
  name: string; description: string; agent?: string | null; hints?: string[]
}
```

providers/models/variants/default 采用 UTF-8 byte order；未知嵌套字段丢弃。
Provider 上限 256、每 provider models 1024、每 model variants 64、投影后 8MiB，
超限不截断，返回 `provider_projection_limit`。

### 2.5 Aggregates / thin resources

```ts
type Aggregate<T> = {
  items: T[]
  errors: Array<{ directory: string; code: string }>
  authoritativeDirectories: string[] | null
  discoveryComplete: boolean
  truncated?: boolean
}
```

`authoritativeDirectories:null` 仅在发现完整、无错误且未截断时成立，客户端可
replace-all；数组时只替换列出的成功目录，不删除其它目录的本地 pending 卡。

```ts
type DirectoryCatalog = {
  items: Array<{
    directory: string
    title: string | null
    lastUpdated: number
    rootSessionCount: number
    activeRootSessionCount: number
    archivedRootSessionCount: number
    archivedOnly: boolean
  }>
  discoveryComplete: boolean
}
```

Todo item 为 `{content,status,priority}`；session diff item 为
`{file?,patch?,additions,deletions,status?}`；children item 为 session skeleton。

---

## 3. REST 路由：发现、健康与操作面

| # | Method / path | 请求 | 200/成功 DTO | 主要错误 |
|---:|---|---|---|---|
| 1 | **GET `/slimapi/versions`** | 无 selector/body | `{current:4,available:[4],capabilities:{"4":CapabilityV4},sidecarVersion}`。CapabilityV4 含 `globalSessions,auxiliaryFilters,sseReplay,qpImmediateFull,messagesSince,fileRaw,tokenFrameSeq,readiness` 与可选 `expand` | 非 GET 405 |
| 2 | **GET `/slimapi/health`** | `?v=4` | `{slimapi_contract,sidecar:{ok,version},server:{api_version,accepted_client_versions,deploymentRevision?},schema:{degraded,version,clientMin,clientMax},features,auxiliary}` | 通常恒 200；字段状态表达降级 |
| 3 | **GET `/slimapi/ready`** | `?v=4` | `{upstream:{ok,latencyMs},server,schema}` | upstream 不可用时 503 同形状 |
| 4 | **GET `/slimapi/metrics`** | `?v=4` | 动态 ops 对象：`traffic`, `sse`, `dbaux`, `sessionsDegraded`, `expand` 等已接线块；未接线块 absent | 无上游；内部统计读取失败不得伪造业务 DTO |
| 5 | **GET `/slimapi/actions`** | `?v=4` | `{enabled:boolean,actions:[{name,kind:"query"|"exec",description,requireConfirm:boolean}]}` | 无；禁用时 enabled=false + 空数组 |
| 6 | **POST `/slimapi/actions/{name}`** | body 空/`{}`/`{confirm:boolean}`，raw ≤1KiB | query：`{kind:"query",ok,exit_code,duration_ms,message,markdown,truncated}`；exec：同形状但无 markdown/truncated | 404 `action_not_found`; 409 `action_confirm_required`; 413 `request_too_large`; 422 `invalid_request_body`; 429 `action_throttled`(+`Retry-After`); 503 `actions_disabled`/`action_busy`/`action_unavailable`; 504 `action_timeout` |
| 7 | **GET `/slimapi/directories`** | 无 directory/body | `DirectoryCatalog`；仅发现曾有 root session 的目录 | 413 `response_too_large`; 503 `transform_busy`/`upstream_unavailable` |

`features.tokenCoalesce=true` 是当前 health 保留能力标签；它不重新启用
`/events?tokens=1`。`metrics.traffic.v3`、snapshot `v3`、access-log 的
`wireVersion/selectorResult` 历史枚举也是稳定 ops schema，用于读取旧日志，
不表示运行时接收 v3。

---

## 4. REST 路由：sessions / messages

| # | Method / path | 请求 | 成功 DTO | 主要错误 |
|---:|---|---|---|---|
| 8 | **GET `/slimapi/sessions`** | `archived=omit|only|all`, `parent=all|none|only|<sid>`, `search?`, `cursor?`, `limit=1..500`; directory 禁止 | `SessionsV4`，排序 `(time.updated DESC,id DESC)` | 400 `invalid_cursor`/`directory_retired_in_v4`; 422 `param_version_mismatch`; 503 `auxiliary_unavailable`(+30)/`upstream_unavailable` |
| 9 | **GET `/slimapi/session/{sid}`** | `directory?` | 裸 `SessionSkeletonV4` | 404 `session_not_found`; 503 `auxiliary_unavailable`/`upstream_unavailable`; native 4xx verbatim |
| 10 | **POST `/slimapi/sessions/details`** | body `{sids:string[]}`，非空、去重后 ≤50、每项 `^[A-Za-z0-9_-]{1,128}$`; directory ignored | `{sessions:SessionSkeletonV4[],missing:string[]}`，首现顺序 | 400 `invalid_body`/`too_many_sids`; 503 `transform_busy`(+2)/`auxiliary_unavailable`(+30)/`upstream_unavailable` |
| 11 | **GET `/slimapi/sessions/status`** | optional `directory`；建议省略 | `Record<string,SessionStatus>`；retry 保留 attempt/message/next；turn pair 为 sidecar additive fence | 502 `upstream_http_N`; 503 `upstream_unavailable` |
| 12 | **GET `/slimapi/messages/{sid}`** | `limit=1..200`, `before?`, `since?`, `mode?`, `directory?`; `before` 与 `since` 互斥 | `MessagesPage`；只有字面 `mode=merged` 做 best-effort splice，其余值为 baseline | 400 `invalid_params`; 404 `session_not_found`; 413 `response_too_large`; 502 `upstream_http_N`; 503 `transform_busy`/`upstream_unavailable` |
| 13 | **GET `/slimapi/messages/{sid}/full/{mid}`** | `directory?`; mode/known 条件不改变 full | full `MessageWithParts`，仅剥 `state.metadata.diagnostics` | 404 `session_not_found`; 413 `message_too_large`; 502 `upstream_http_N`; 503 `transform_busy`/`upstream_unavailable` |
| 14 | **GET `/slimapi/messages/{sid}/expand/{category}/{mid}`** | 仅 message category `info_summary_diffs`; `directory?` | `ExpandMessage` | 400 `invalid_expand_category`/`expand_category_mismatch`; 404 `expand_target_not_found`/`session_not_found`; 413 两类 expand cap; 502 `upstream_invalid_shape`/`upstream_http_N`; 503 busy/upstream |
| 15 | **GET `/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`** | 11 个 part categories；`directory?` | `ExpandPart` | 同上；missing part 的 body 含 `reason:"part_missing"` |
| 16 | **GET `/slimapi/sessions/{sid}/todo`** | `directory?` | `{content,status,priority}[]` | 404 `session_not_found`; 413 `response_too_large`; 502 `upstream_http_N`; 503 busy/upstream |
| 17 | **GET `/slimapi/sessions/{sid}/children`** | `directory?` | session skeleton array | 同 todo |
| 18 | **GET `/slimapi/sessions/{sid}/diff`** | `directory?`, `messageID?` | `{file?,patch?,additions,deletions,status?}[]` | 同 todo |

todo/children/diff 支持 ETag/304。`messagesRevision` 变化适合触发 messages
`since` 差分；它不替代 `/full` 的终态确认。

---

## 5. REST 路由：catalog、file、VCS 与 passthrough reads

| # | Method / path | 请求 | 成功 DTO | 主要错误 |
|---:|---|---|---|---|
| 19 | **GET `/slimapi/agent`** | `directory?`（上游 catalog no-op） | `AgentSkeleton[]` | 413 response cap; 502 `upstream_http_N`; 503 `transform_busy`/`upstream_unavailable` |
| 20 | **GET `/slimapi/command`** | `directory?` | `CommandSkeleton[]` | 同 agent |
| 21 | **GET `/slimapi/config/providers`** | `directory?` | `ProviderResult` | 413 `response_too_large`/`provider_projection_limit`; 502 `provider_upstream_malformed`/`upstream_http_N`; 503 busy/upstream |
| 22 | **GET `/slimapi/file`** | required `path`, `directory?` | `LegacyEntry[]{name,path,absolute,type,ignored}` verbatim | 上游 4xx verbatim; 413 response cap; 503 upstream |
| 23 | **GET `/slimapi/file/content`** | required `path`, `directory?` | `LegacyContent{type:"text"|"binary",content,diff?,patch?,encoding?,mimeType?}` | 同 file |
| 24 | **GET `/slimapi/file/status`** | `directory?` | `LegacyStatus[]{path,added,removed,status}` | 同 file |
| 25 | **GET `/slimapi/file/raw`** | required `path`, `directory?` | binary：裸 bytes + validated MIME；text：`text/plain;charset=utf-8` | 400 `invalid_params`; 403 `directory_not_allowed`; 413 `response_too_large`; 502 `raw_decode_failed`; 503 busy/upstream; upstream 4xx verbatim |
| 26 | **GET `/slimapi/vcs`** | `directory?` | upstream `Vcs.Info` object verbatim | upstream 4xx verbatim; 413 cap; 503 upstream |
| 27 | **GET `/slimapi/vcs/status`** | `directory?` | upstream `Vcs.FileStatus[]` verbatim | 同 vcs |
| 28 | **GET `/slimapi/vcs/diff`** | `directory?`, `mode?`, `context?` | `{path,patch?,additions,deletions}[]` | 同 vcs |
| 29 | **GET `/slimapi/find/file`** | required `query`; `dirs?`, `type=file|directory?`, `limit=1..200?`, `directory?` | `string[]` | upstream 4xx verbatim; 413 cap; 503 upstream |
| 30 | **GET `/slimapi/api/session/active`** | 无业务 query | `{data:Record<string,SessionActive>}` verbatim | upstream 4xx verbatim; 413 cap; 503 upstream |
| 31 | **GET `/slimapi/global/health`** | 无业务 query | `{healthy:boolean,version:string}` verbatim | upstream 4xx verbatim; 413 cap; 503 upstream |
| 32 | **GET `/slimapi/session/{sid}/context`** | directory tolerant-ignore | `{data:unknown[]}` verbatim | upstream 4xx verbatim; 413 cap; 503 upstream |

上表中 current passthrough DTO 的完整 consumer 形状：

```ts
type LegacyEntry = {
  name: string; path: string; absolute: string
  type: "file" | "directory"; ignored: boolean
}
type LegacyPatch = {
  oldFileName: string; newFileName: string
  oldHeader?: string; newHeader?: string
  hunks: Array<{
    oldStart: number; oldLines: number; newStart: number; newLines: number
    lines: string[]
  }>
  index?: string
}
type LegacyContent = {
  type: "text" | "binary"; content: string
  diff?: string; patch?: LegacyPatch; encoding?: "base64"; mimeType?: string
}
type LegacyStatus = {
  path: string; added: number; removed: number
  status: "added" | "deleted" | "modified"
}
type VcsInfo = { branch?: string; default_branch?: string }
type VcsFileStatus = {
  file: string; additions: number; deletions: number
  status: "added" | "deleted" | "modified"
}
type VcsDiffEntry = {
  path: string; additions: number; deletions: number; patch?: string
}
type SessionActive = { type: "running" }
type SessionActiveResponse = { data: Record<string, SessionActive> }
type SessionStatus =
  | { type: "idle"; turnIncarnation?: number; turn?: number }
  | { type: "busy"; turnIncarnation?: number; turn?: number }
  | {
      type: "retry"; attempt: number; message: string; next: number
      turnIncarnation?: number; turn?: number
    }
```

`find/file` 的其它成功 body 分别是 `string[]`、`LegacyEntry[]` 或上列对象；
VCS status 的 upstream 键仍是 `file`，VCS diff 投影输出 `VcsDiffEntry[]`。
optional key 缺失与 JSON `null` 不等价；上列未标 optional 的 key 必须存在。
read-passthrough 的 schema authority 仍是 upstream，客户端必须忽略未来新增字段。
`/sessions/status` 的 top-level 是 `Record<string,SessionStatus>`；
`turnIncarnation/turn` 要么成对出现，要么成对缺失。sidecar 对 upstream
schema-violation 的非 object entry 做 verbatim passthrough，客户端须宽容跳过。

---

## 6. REST 路由：questions / permissions

```ts
type QuestionOption = { label: string; description: string }
type QuestionInfo = {
  question: string; header: string; options: QuestionOption[]
  multiple?: boolean; custom?: boolean
}
type Question = {
  id: string; sessionID: string; questions: QuestionInfo[]
  tool?: { messageID: string; callID: string }
  directory: string
}
type ReplyPayload = { answers: string[][] }
type Permission = {
  id: string; sessionID: string; permission: string; patterns: string[]
  metadata: Record<string, unknown>; always: string[]
  tool?: { messageID: string; callID: string }
  directory: string
}
```

`Question/Permission` 仅比 upstream 当前对象多 `directory`；上列未标 optional 的
key 必须存在。`ReplyPayload.answers[i]` 对应 `questions[i]`，每个元素是该题所选
label 或 custom 文本的字符串数组；reject body 不增加 Slimapi 自有 DTO。

| # | Method / path | 请求 | 成功 DTO | 主要错误 |
|---:|---|---|---|---|
| 33 | **GET `/slimapi/questions`** | 无 directory | `Aggregate<Question>`；Question 保留 upstream `id,sessionID,questions,tool` 等字段并追加 `directory` | 发现失败整响应 503；单目录失败进入 errors；预算触发 truncated |
| 34 | **GET `/slimapi/permissions`** | 无 directory | `Aggregate<Permission>`；Permission exact whitelist `{id,sessionID,permission,patterns,metadata,always,tool?,directory}` | 同 questions |
| 35 | **POST `/slimapi/question/{request_id}/reply`** | `ReplyPayload` verbatim；optional `directory` 由 v4 selector 消费 | upstream success/status/body verbatim | 413 request/response cap; upstream 4xx verbatim; 503 upstream |
| 36 | **POST `/slimapi/question/{request_id}/reject`** | body verbatim/通常空 | upstream success/status/body verbatim | 同 reply |

questions/permissions 的 `authoritativeDirectories` 规则必须按 §2.5 执行；
部分 fan-out 成功不是 replace-all 授权。

---

## 7. REST 路由：controlled writes

所有 write 先读有界请求 body，再向 loopback upstream 发起对应 method/path。
2xx/3xx/4xx 的 status/body 按受控 header 集透传；5xx/网络 → 503
`upstream_unavailable`；请求超限 → 413 `request_too_large`；响应超限 →
413 `response_too_large`。成功 body 由下表所列 upstream action DTO 权威；204
没有 JSON body。所有 write 无 ETag。

当前 write 使用的可直接编码 DTO：

```ts
type SessionInfo = {
  id: string; slug: string; projectID: string; workspaceID?: string
  directory: string; path?: string; parentID?: string
  summary?: { additions: number; deletions: number; files: number; diffs?: object[] }
  cost?: number
  tokens?: {
    input: number; output: number; reasoning: number
    cache: { read: number; write: number }
  }
  share?: { url: string }
  title: string; agent?: string
  model?: { id: string; providerID: string; variant?: string }
  version: string
  metadata?: unknown
  time: { created: number; updated: number; compacting?: number; archived?: number }
  permission?: unknown
  revert?: { messageID: string; partID?: string; snapshot?: string; diff?: string }
}
type SessionPatchPayload = {
  title?: string; metadata?: unknown; permission?: unknown
  time?: { archived?: number }
}
type ForkPayload = { messageID?: string }
type SummarizePayload = { providerID: string; modelID: string; auto?: boolean }
type RevertPayload = { messageID: string; partID?: string }
type PermissionResponsePayload = { response: "once" | "always" | "reject" }
```

`metadata`、`permission`、CreateInput、PromptPayload、CommandPayload 与普通 action
response 是 upstream-owned JSON；sidecar 对其执行有界字节透传而不增加字段白名单。
客户端不得把本导航中的样例对象误当作由 Slimapi 冻结的第二套 schema。archive 的
合成 timestamp 是 epoch milliseconds。

| # | Method / path | body / 等价动作 | 成功 body |
|---:|---|---|---|
| 37 | **POST `/slimapi/session`** | upstream CreateInput；directory-consuming | upstream `Session.Info`（通常 201） |
| 38 | **PATCH `/slimapi/session/{session_id}`** | `SessionPatchPayload` verbatim | upstream updated `SessionInfo`/action response |
| 39 | **DELETE `/slimapi/session/{session_id}`** | body 也按字节透传 | upstream delete response，常 204 |
| 40 | **POST `/slimapi/session/{session_id}`** | 与 PATCH 逐字等效 | 与 PATCH 相同 |
| 41 | **POST `/slimapi/session/{session_id}/archive`** | 空 body 合成 `{"time":{"archived":<epoch-ms>}}`；非空 body 原样走 PATCH | 与 PATCH 相同 |
| 42 | **POST `/slimapi/session/{session_id}/delete`** | 与 DELETE 等效，含 body/content-type | 与 DELETE 相同 |
| 43 | **POST `/slimapi/session/{session_id}/prompt_async`** | PromptPayload | upstream action response，常 204 |
| 44 | **POST `/slimapi/session/{session_id}/abort`** | 通常空 body | upstream action response |
| 45 | **POST `/slimapi/session/{session_id}/summarize`** | `SummarizePayload` | upstream action response |
| 46 | **POST `/slimapi/session/{session_id}/fork`** | `ForkPayload` | forked `SessionInfo` |
| 47 | **POST `/slimapi/session/{session_id}/revert`** | `RevertPayload` | upstream revert response |
| 48 | **POST `/slimapi/session/{session_id}/permissions/{permission_id}`** | `PermissionResponsePayload` | upstream permission response |
| 49 | **POST `/slimapi/session/{session_id}/command`** | CommandPayload | upstream command response |
| 50 | **POST `/slimapi/session/{session_id}/agent`** | `{agent:string}`；directory ignored | 204 |
| 51 | **POST `/slimapi/session/{session_id}/model`** | `{model:string}`；directory ignored | 204 |
| 52 | **POST `/slimapi/session/{session_id}/revert/stage`** | `{messageID:string,files?:boolean}`；directory ignored | `{data:unknown}` |
| 53 | **POST `/slimapi/session/{session_id}/revert/clear`** | 通常空 body；directory ignored | 204 |
| 54 | **POST `/slimapi/session/{session_id}/revert/commit`** | 通常空 body；directory ignored | 204 |

DELETE 的递归子删除/部分失败语义来自 upstream；sidecar 不增加 cascade 编排、
重试或部分失败 envelope。archive 只更新单 session，不级联。

---

## 8. SSE 路由与客户端 reconciliation

### 8.1 通用格式

两个 SSE 路由均是 `text/event-stream` identity 表示。连接建立后第一帧必须是：

```text
event: slimapi.meta
data: {"subscriberId":"...","tokens":false|true,
       "capabilities":{"sseReplay":true,...},
       "epoch":"0123456789abcdef","seqBase":123}

```

meta、heartbeat、控制 resync 没有 SSE `id:`。`epoch` 是同一进程共享的 16
hex boot nonce，不比较大小。global/token 使用独立 seq ledger；不能拿 global
seq 去重 token，也不能跨 sid 复用 token ledger。

`Last-Event-ID` 必须对应当前端点域：global 为 `g:<epoch>:<seq>`；token 为
`t:<sid>:<epoch>:<seq>`。分类先于 subscriber attach。窗口可用时先 replay，
随后才读 live queue；否则首个控制帧为：

```text
event: resync
data: {"reason":"epoch_changed|replay_expired|replay_gap|reconnect_no_replay"}
```

控制 resync 后执行 HTTP authoritative reconciliation，再继续/重连。服务端不会
发送 snapshot。upstream disconnect、idle recycle、session invalidation 会写 replay
barrier；barrier 后携旧 cursor 重连得到 `reconnect_no_replay`，不得跨观察缺口重放。

### 8.2 Global SSE

| # | Method / path | 请求 | 帧 |
|---:|---|---|---|
| 55 | **GET `/slimapi/events`** | 无 directory；`tokens=1`→400 `tokens_stream_retired_in_v4`；其它 tokens 值→400 `invalid_tokens`; optional `Last-Event-ID` | meta `{tokens:false,capabilities:{sseReplay:true},epoch,seqBase}`；heartbeat；`session.digest`；question/permission asked/replied/rejected；selected session/message errors；control resync |

global business frame 带 `id:g:<epoch>:<seq>`。`data` 是：

```ts
type GlobalFrame = {
  directory: string
  type: string
  properties: object
}

type SessionDigestProperties = {
  sessionID: string
  directory: string
  status?: string
  messageID?: string
  updatedAt?: number
  archived?: number
  deleted?: true
  lastError?: object
  turnIncarnation?: number
  turn?: number
  changed?: string[]
  messagesRevision?: number
}
```

`changed` 是本 digest 合并窗内的 compact 字段名集合。`messagesRevision` 仅在
message-domain digest 出现，是同一 sid/同一进程的变化信号；allocator 跨 sid
全局递增但 **跨 sid 比较无语义**，禁止用其它 sid 的 max 丢帧。question asked
前服务端先 flush 同 sid pending digest，故同连接观察顺序为 digest → asked。

### 8.3 Token SSE

| # | Method / path | 请求 | 帧 |
|---:|---|---|---|
| 56 | **GET `/slimapi/sessions/{sid}/stream`** | optional query `directory` 校验/no-op；optional token `Last-Event-ID` | meta `{tokens:true,capabilities:{sseReplay:true,tokenFrameSeq:true},epoch,seqBase}`；`message.part.delta`；`message.removed`；replayable `resync{token_memory_limit}`；heartbeat；冻结 control resync；STOP-only lifecycle disconnect |

sequenced business frame 带 `id:t:<sid>:<epoch>:<seq>`，且 payload `seq` 必须等于
ID 末段。客户端账本键是 `(epoch,sid)`，只应用 `seq > lastAppliedSeq`；允许跳号
推进，不把跳号解释为客户端可见数据空洞。

当前业务集合恰为：

```ts
type TokenDelta = {
  type: "message.part.delta"
  properties: {
    sessionID: string
    messageID: string
    partID: string
    field: string
    delta: string
    seq: number
    partEventRevision: number
  }
}

type TokenRemoved = {
  type: "message.removed"
  properties: { sessionID: string; messageID: string; seq: number }
}

type TokenMemoryResync = {
  type: "resync"
  properties: { sessionID: string; reason: "token_memory_limit"; seq: number }
}
```

`partEventRevision` 只属于 active-v4 `message.part.delta`；不是 global seq，也不是
`messagesRevision`。`token_memory_limit` 是可重放 advisory business frame，不终止
连接、不清空其它 live parts、不要求专用 GET。被驱逐 part 改为 REST-owned，待
part 完成引发 `messagesRevision` 后走常规 since/full 收敛。`session_idle` 与
`session_deleted` 在 active-v4 原连接上均为 STOP-only terminal disconnect，**不发送**
同名 resync；删除权威信号来自 global `session.digest{deleted:true}`。两者先写
token-domain replay barrier，barrier 后携旧 cursor 重连收到无 id/seq 的冻结控制
resync `reconnect_no_replay`，再走 authoritative HTTP reconciliation。其它冻结
control reason 仍只有 `epoch_changed|replay_expired|replay_gap|reconnect_no_replay`。

### 8.4 Authoritative full-message 合并

token 动画与 REST 不允许双源盲覆盖：

1. delta 到达后 part 标记 `streamOwned`；
2. digest `messagesRevision` 变化 → `?since=` 定位变化 message；
3. 若 message 含 unfinished streamOwned part，取 `/full/{mid}`；
4. 同 part 有非空 `time.end` → REST text 覆盖 live text，标完成并解除
   `streamOwned`；
5. 无 `time.end` → REST 是中间态，保留 live text；
6. skeleton 不含 part `time`，缺席绝不能推断完成/未完成。

global replay append 失败的保留行为是 warning + live id-less fallback；token append
失败则 rollback/drop/fail-closed，不会泄露 id-less token business frame。

---

## 9. webui / ocdroid 实现 checklist

- [ ] 启动先读 `/slimapi/versions`，确认 `available == [4]`；所有其它请求带
      `?v=4`，不发送旧版本头。
- [ ] directory 只走 query；不发送 `X-Opencode-Directory`。
- [ ] JSON parser 忽略未知 key；SSE dispatcher 忽略未知 `data.type`。
- [ ] sessions 使用 envelope/cursor；按 item.directory 做客户端 workdir 过滤。
- [ ] messages 保存 `nextSince`；正确区分 diff、reset、nextSince absent；
      `mode=merged` 只当 best-effort，不假设所有 placeholder 都展开。
- [ ] 根据 `expandRefs` 拉单片段；413 expand 可改走 `/full`；仅
      `thin_route_not_found` 表示端点不存在。
- [ ] ETag 路由缓存 200 body 并回发 validator；304 复用本地 body；不要给
      `/full`/expand/SSE/write 自造条件请求语义。
- [ ] questions/permissions 只在 `authoritativeDirectories:null` 时 replace-all。
- [ ] global 与每个 token sid 维护独立 replay/seq ledger；meta 永远先处理。
- [ ] control resync 做 HTTP reconciliation；不等待服务端 snapshot。
- [ ] token `seq` strict-greater 去重；`token_memory_limit` 不清流，靠 revision
      和终态 full 合并收敛。
- [ ] `messagesRevision` 只同 sid 比较；asked 卡片前接受 digest flush 顺序。
- [ ] 503 按 `Retry-After` 退避；422/400 修请求；502 当确定性 upstream shape
      问题；413 不无限重试同一 payload。
- [ ] 旧路由 404 不直连 sidecar catch-all；若产品仍保留 opencode 直连回退，
      该回退必须由客户端显式策略管理，而不是依赖本服务转发。

---

## 10. Source / golden anchors

- selector/version：`src/oc_slimapi/selector.py`、`src/oc_slimapi/versioning.py`、
  `src/oc_slimapi/routes/versions.py`。
- route inventory gate：`scripts/check_routes_doc.py`、
  [`INTERFACE_MAP.md`](INTERFACE_MAP.md)。
- session DTO/projector：`src/oc_slimapi/skeleton.py::canonical_session_skeleton_v4`、
  `src/oc_slimapi/routes/sessions.py`、`src/oc_slimapi/routes/read_groups.py`。
- messages/since/expand：`src/oc_slimapi/routes/messages/`、
  `src/oc_slimapi/skeleton.py`、`src/oc_slimapi/traffic.py::EXPAND_CATEGORIES`。
- provider DTO：`src/oc_slimapi/providers_projection.py`。
- global/token SSE：`src/oc_slimapi/routes/events.py`、
  `src/oc_slimapi/routes/token_stream.py`、`src/oc_slimapi/sse/replay_log.py`、
  `src/oc_slimapi/sse/replay_wire.py`、`src/oc_slimapi/sse/global_hub.py`、
  `src/oc_slimapi/sse/tokenstream/`。
- write map：`src/oc_slimapi/routes/write_groups.py`、
  `src/oc_slimapi/routes/actions.py`。
- wire/golden regression：`tests/test_v4_native_runtime.py`、
  `tests/test_sse_replay_wire.py`、`tests/test_refactor_equivalence.py`、
  `tests/test_offload_equivalence.py`、`tests/golden/refactor-baseline-v1.json`、
  `tests/golden/offload-baseline-v1.json`。
