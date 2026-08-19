# ocdroid 业务需求全景报告（2026-08-17，exp-o 探索）

> 对象：/home/mar/personal_projects/ocdroid（Android Kotlin，排除 opencode-src/）
> 用途：体系架构重塑的需求侧输入之一（消费者 1：移动端代表）

## ① Feature→API 映射表

| 功能 | 界面 | API 端点 | 数据期望 | 状态管理 |
|---|---|---|---|---|
| 会话列表（首页） | `SessionsScreen`→`SessionsHomeContent`（Recent+Attached Projects 两段） | `GET /slimapi/sessions?directory=&roots=&limit=&search=` | `SlimapiSessionsEnvelope{items, complete}` skeleton Session | `SessionListState` + `authorityFlow` + `unreadFlow` |
| **会话归档** | SessionsScreen 长按→确认框 | `PATCH /slimapi/session/{id}` body `time:{archived:epochMs}` | 更新后 Session | **客户端过滤** `recentSessionsInWorkdirScope` (`.filter { !it.isArchived }`) |
| 会话重命名/删除 | 长按菜单 | `PATCH`（title）/ `DELETE /slimapi/session/{id}` | — | 本地 state |
| 创建会话 | + 按钮→workdir picker（≥2 目录时） | `POST /slimapi/session` + `X-Opencode-Directory` header | Session | `createSessionInWorkdir` |
| 子会话列表 | `SessionTree.kt`/`ChatSubAgentCard.kt` | `GET /slimapi/sessions/{sid}/children`（404-sticky 回退裸路径） | `List<Session>` | childSessions + buildSessionTree |
| 会话状态 busy/idle | SessionCard 指示器 | `GET /slimapi/sessions/status?directory=`（Plan-A，404-sticky 回退 `/session/status`） | `Map<sid,SessionStatus>`（busy/idle/retry+turn/incarnation） | `AuthorityState` via authorityFlow |
| 活跃会话（unread） | `UnreadSoakController` **30s 轮询** | `GET /slimapi/api/session/active` | `Map<sid,ActiveSession>` | unreadFlow |
| 消息骨架 | `ChatScaffold`→`ChatMessageRow` | `GET /slimapi/messages/{sid}?limit=&before=&mode=skeleton` | `SlimapiMessagesEnvelope{items,nextCursor}` | `MessagesPage` cursor 分页 |
| 消息展开 | hasFull 部件展开钮 | `GET /slimapi/messages/{sid}/full/{mid}` | MessageWithParts（32MiB cap） | `PartExpandState` |
| merged 展开（L2-C） | 同上路径 | `GET /slimapi/messages/{sid}?mode=merged` + per-id /full 回退 | merged envelope | `expandMerged()` flag-gated |
| 消息探针 | MessageLoader/刷新编排 | `GET /slimapi/messages/{sid}?limit=1` | ProbeResult(ok,mid,updatedAt) | data class |
| 发消息 | `Composer.kt` | `POST /slimapi/session/{id}/prompt_async` + directory header | 202（SSE 送结果） | mutationApi（防双发） |
| 中止 | ChatTopBar | `POST /slimapi/session/{id}/abort` | — | mutationApi |
| 上下文压缩 | `ChatContextUsageDialog` | `POST /slimapi/session/{id}/summarize` | Boolean（false=server 拒绝） | SummarizeServerRejectedException |
| Fork/Revert | 会话管理 | `POST .../fork` / `POST .../revert` | Session | mutationApi |
| **Slash 命令** | `Composer.kt` `/` 触发补全 | `GET /slimapi/command`（404-sticky）+ `POST /slimapi/session/{id}/command` | `List<CommandInfo>` | CatalogGateway 缓存 |
| Agent/Model 选择 | `PickerSheets.kt` | `GET /slimapi/agent` / `GET /slimapi/config/providers` | AgentInfo/ProvidersResponse | CatalogGateway（providers 不缓存） |
| **文件浏览** | `FilesScreen`→`FileBrowserPane`（树） | `GET /slimapi/file?path=&directory=` | `List<FileNode>` | FilesUiState |
| 文件内容 | `FilePreviewPane`（text/image/md） | `GET /slimapi/file/content` | FileContent | selectedFileContent |
| 文件状态 | FileBrowserPane 徽标 | `GET /slimapi/file/status` | `List<FileStatusEntry>` | fileStatuses |
| VCS | `GitScreen`/`SessionDiffCard` | `GET /slimapi/vcs` + `/vcs/status` + `/vcs/diff` + `/slimapi/sessions/{sid}/diff` | VcsInfo/Status/FileDiff | FileVcsGateway/SessionGateway |
| Question 卡片 | QuestionCardView 等 | `GET /slimapi/questions`（跨目录聚合）+ `POST /slimapi/question/{id}/reply|reject` | `SlimapiQuestionsEnvelope` | InteractionGateway 404-sticky 回退 per-dir fan-out |
| Permission 卡片 | ChatPermissionCard | `GET /slimapi/permissions` + `POST /slimapi/session/{id}/permissions/{pid}` | `SlimapiPermissionsEnvelope` | flag-gated `l2.permissionEvents` |
| SSE 事件流 | `SSEClient`→`SseEventBridge` | `GET /slimapi/events?v=3` | digest/q/p/connected/heartbeat/resync/STOP | retryWhen 指数退避≤10 次+30s 心跳看门狗 |
| Token 流 | `TokenStreamClient` | `GET /slimapi/sessions/{sid}/stream?v=3` | TokenStreamFrame | Cold Flow + Stage D 协调 |
| 健康检查 | ConnectionBootstrap/主机测试钮 | `GET /slimapi/health` + `/slimapi/ready` | sidecar/server 双探 | ConnectionGateway |
| 目录列表 | `PastProjectsSheet` | `GET /slimapi/directories`（404-sticky） | DirectoriesEnvelope | CatalogGateway |
| 管理动作 | `ActionsSheet` | `GET /slimapi/actions` + `POST /slimapi/actions/{name}` | discovery/invoke | 404-sticky |
| 图片加载 | `HttpImageHolder`（markdown 图） | **独立 OkHttpClient 直连（C1 违规，不经 slimapi）** | Bitmap | Lru+disk cache |

## ② Single-Directory 模型实现位置

- **WorkdirPrefs**（util/WorkdirPrefs.kt）：`currentWorkdir`（ESP key）+ per-host `recentWorkdirs` MRU 上限 30
- **DirectoryHeaderInterceptor**（http/DirectoryHeaderInterceptor.kt:46-124）：`X-Opencode-Directory` ↔ `/slimapi/` 路径 `?directory=` query 双形态转换；写路径保 header；`X-Opencode-Skip-Dir: 1` 跳过注入
- 耦合点：SessionsScreen.kt:298-307（创建时目录选择）、WorkdirGroups.kt:146-214（**列表可见性 = recentWorkdirs ∪ 草稿目录**）、WorkdirGroups.kt:22-38（scope 过滤）、InteractionGateway 全部写方法带 directory 参数、FileVcsGateway 全部带 directory
- **设计决策**（slim-mode-api-routing.md §1.2）：省流 = 切换 `HostConfig.baseUrl`（非 slimMode 标志）；L3 波1（2026-08-15）直连退役，slimapi 为唯一上游，`HostProfile.slim` 恒 true

## ③ API 层痛点（TODO/WORKAROUND 实证）

TODO 6 处（sse-rest-fallback×3、structured/outputPaths 不解析、测试确定性、缓存大小指示器）。

**典型 WORKAROUND（API 补偿）**：
1. `CommandRequest.arguments` 类型修复（OpenCodeApi.kt:117-121）——1.17.11 要求 JSON string 而非 object，默认 `""`
2. DirectoryHeaderInterceptor 双值冲突透传（让 sidecar 400）
3. sendMessage 仅 DNS/ConnectException 重试一次（防双发）
4. SSEClient 三形状解析（legacy payload/flat q/p/event-typed）
5. expandMerged 双路径（window miss + budget-degraded 回退）
6. 404-sticky 能力探测（thin→catch-all 回退）
7. permissions 三条件 replace-all（errors空+discoveryComplete+!truncated）
8. HttpImageHolder 图片绕过 slimapi（C1 违规）

## ④ 补偿逻辑清单（opencode API 限制造成）

| 补偿逻辑 | 位置 | 说明 |
|---|---|---|
| 404-sticky 能力探测 | SessionGateway/InteractionGateway/CatalogGateway | `ThinRouteCapabilityFlags` + `ServerCompatProfile` |
| transform_busy 503 重试 | SessionGateway.kt:262-302 | 指数退避 2-3 次 + Retry-After 优先 |
| SSE 心跳看门狗 30s | SSEClient.kt:373-399 | 移动 NAT 半开检测 |
| SSE 重连预算 ≤10 | SSEClient.kt:161-195 | 1s→30s ±30% jitter → UI 横幅 |
| SSE 背压 STOP 处理 | SSEClient.kt | 2MiB buffer/256KiB 单帧溢出→STOP+resync 帧 |
| digest updatedAt 非单调 | Contract §5.5/M6 | `(updatedAt, messageID)` 二元组字典序 watermark |
| thin_placeholder_ 整消息替换 | MessageGateway.kt:248 | 禁 part-level lookup |
| 阈值化 skeleton 语义 | Contract §5.4 | 整字段 omit + omitted 标记 |
| merged 单拉+per-id 回退 | MessageGateway.kt:214-289 | L2-C flag |
| mutation 防双发 | InteractionGateway/OpenCodeApi | retryOnConnectionFailure(false) |
| arguments 空串默认 | OpenCodeApi.kt:121 | 1.17.11 类型变更 |
| **C1** 图片直连 | HttpImageHolder.kt:143-148,282-316 | 独立 client，URL rewriting 未实现 |
| **C2** 证书捕获直探 /global/health | OpenCodeRepository | mTLS/TOFU——应改 /slimapi/health |
| **C3** host 测试直探 /global/health | OpenCodeRepository | sidecar 挂时误报——应改 /slimapi/ready |

## ⑤ slim-mode-api-routing.md 规约要点（V2/V3）

- 四桶：A slim-direct 8 / B slim-passthrough 36 / **C direct-opencode 5（C1-C4 违规待迁移）** / D external 4
- V2 破坏性：版本门闩 (2,2)、删 10+ 端点、routeToken 下线、updatedAt 改 sidecar wall-clock、delta 二元组字典序
- V3 现状：`?v=3` selector（V3SelectorInterceptor 统一追加）、thin 路由+聚合 q/p 全面使用；遗留 C1-C3
