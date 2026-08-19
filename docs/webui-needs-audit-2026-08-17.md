# oc-webui API 消费与需求审计报告（2026-08-17）

> 审计对象：`/home/mar/personal_projects/oc-webui`（Vue 3 + TypeScript + Vite，M0–M5 已全部完成，生产经 tailscale serve 部署）
> 调研方式：fixer-ds 只读研究
> 用途：为 oc-slimapi（省流 sidecar :4097）下一批接口收编提供需求侧输入。全部证据为代码/文档 file:line 实证。

---

## 1. 连接拓扑：100% 经 sidecar :4097，无 4096 直连、无端口切换

**唯一路径**：浏览器同源相对路径请求 → tailscale serve（`https://mar-ubuntu.taild4b3b5.ts.net`）→ 反代到 loopback **127.0.0.1:4097（sidecar）**。

| 证据 | 位置 |
|---|---|
| serve 5 条 mount：`/`→dist 静态、`/slimapi`、`/session`、`/question`、`/permission` 全部 → `http://127.0.0.1:4097`（前缀保真）；**`/file` 不 mount → 404 屏蔽** | `docs/DEPLOYMENT.md:15-18`、`:37-41` |
| dev proxy 同 4 前缀 → 硬编码 `TAILNET_ORIGIN`，无 `/file` | `vite.config.ts:4-29` |
| 代码**零处** `4096`/`4097` 端口硬编码 | grep 结论 |
| 无任何 `VITE_` env、生产无 baseUrl 配置——`baseUrl` 恒 `''`（同源相对路径） | `src/api/health.ts:89` |

**配置切换情况**：无 4096↔4097 切换开关。webui 内部只有**协议双模 v2/v3 切换**（同一 sidecar）：

- 启动探一次 `GET /slimapi/versions`（裸 GET，200 且 `available` 含 3 → v3；404/400 `version_required` → v2；其余 fail-closed）——`endpoints.ts:84-141`（`detectApiMode`，模块级缓存，成功一次后不再重探）
- v2 携带 `X-Slimapi-Version: 2` 头（`client.ts:22`）+ `X-Opencode-Directory` 头（`client.ts:96-98`）；v3 用 `?v=3&directory=` query、零版本头（`protocol.ts:31-35`）
- SSE 用 `fetch + ReadableStream` 而非 `EventSource`（`src/sse/connect.ts:4-6`——EventSource 无法携带自定义头）
- **客户端标识：REST 与 SSE 全路径发 `X-Client-Name: oc-webui` + `X-Client-Version: 0.1.0`**（`client.ts:93-94`、`connect.ts:74-75`、`useTokenStream.ts:66-67`）
- 启动门闩链（`HomeView.vue:413-467`）：`detectApiMode` → `getHealth + checkVersionGate`（fail-closed）→ `probeEndpoints` 五端点探测（`health.ts:286-333`）→ 全过才建 SSE + 会话列表

**结论**：sidecar 收编任何新端点，webui 只需改码即可消费——无网络层阻碍；`/file` 例外（serve mount 缺位），启用在 serve 配置层。

---

## 2. 消费端点清单（按资源域）

### 只读 REST（11 个实际消费 + 1 个仅探测）

| METHOD + 路径 | 场景 | 频率 |
|---|---|---|
| `GET /slimapi/versions` | 模式探测（裸 GET 无版本标识） | 启动 1 次（缓存） |
| `GET /slimapi/health?v=3` | 启动自检 + 版本门闩 | 启动 1 次 |
| `GET /slimapi/directories` | 目录切换器下拉 | 启动 + resync + 手动刷新（低频） |
| `GET /slimapi/agent?directory=` | agent catalog（惰性 + 目录切换重拉） | 按需 |
| `GET /slimapi/sessions`（`search/roots/limit/start`） | 会话列表（`SESSION_LIMIT=100`）：冷启动、搜索 300ms 去抖、目录切换、digest 5s trailing、resync full | 常驻（事件驱动） |
| `GET /slimapi/sessions/status` | 会话状态表（与 sessions 并行拉取） | 常驻（同上触发） |
| `GET /slimapi/messages/{sid}`（`before/limit/mode=merged`） | 消息骨架：首屏 `INITIAL_LIMIT=20`（M5 lazy）、翻页/刷新 `PAGE_LIMIT=50`；merged 由 `health.features.serverMerge` 驱动 | 按需 + 会话活跃时刷新 |
| `GET /slimapi/messages/{sid}/full/{mid}` | placeholder 展开（点击"加载全文"）+ resync 重验证 | 按需（用户点击） |
| `GET /slimapi/questions` | pending 问题聚合（QpEnvelope partial 语义） | 冷启动 + `asked` 帧 2s trailing 重拉 |
| `GET /slimapi/permissions` | pending 权限聚合（`resolved` 帧乐观移除） | 同上 |
| `GET /slimapi/command` | **仅** `probeEndpoints` 能力探测（business 零调用） | 启动 1 次 |

### 策展 SSE（2 路）

| 端点 | 消费帧 | 频率 |
|---|---|---|
| `GET /slimapi/events(?v=3)` | `session.digest` / `server.connected` / `server.heartbeat` / `resync` + q/p 六种 IMMEDIATE 帧 | 长连（指数退避 1s→30s 重连） |
| `GET /slimapi/sessions/{sid}/stream(?v=3)` | `message.part.snapshot/delta/removed` / `resync(reason=session_idle ⇒ 终态)` / `heartbeat`；503 `token_subscriber_limit` 按 Retry-After 退避；隐藏 tab 主动断流 | 长连（进入会话时） |

### 写端点（sidecar 已收编 12 写端点，webui 实际用 5 条路径）

全经 `modePost`（`endpoints.ts:612-645`：**空 directory 抛 TypeError fail-closed**）：

| 动作 | 路径 | 触发 |
|---|---|---|
| 发消息 | `POST /session/{sid}/prompt_async`（body 仅 text part + 可选 agent） | 用户发送（MessageView.vue:350-383） |
| 中止 | `POST /session/{sid}/abort` | 停止按钮（:389-419） |
| 答问题 | `POST /question/{qid}/reply` `{answers}` | 卡片按钮（HomeView.vue:142-161） |
| 拒问题 | `POST /question/{qid}/reject`（无 body） | （:163-181） |
| 答权限 | v2 `POST /permission/{pid}/reply` / v3 `POST /slimapi/session/{sid}/permissions/{pid}`（`reply ∈ once/always/reject`） | （:183-206） |

> 未消费 sidecar 已收编的其他 7 条写端点——需求侧空白。

---

## 3. 痛点挖掘（sidecar 收编机会，按省流价值排序）

### 3.1 轮询扇出（可被增量/聚合/直推替代）—— 最重

1. **收藏目录跨目录扇出（MAX）**：`useRecentSessions.ts:134-158`——收藏目录（上限 30 个）**逐目录并行** `getSessions({directory, roots:true, limit:20}) + getSessionStatus(directory)`，任何 digest/resync 后 5s trailing **整组重拉**。全仓最重放大点：N 目录 × 2 请求。
2. **digest 全表重拉**：`useSessionList.ts:152`——每次 `load` 必 `Promise.all([getSessions(limit=100), refreshStatuses()])`。digest 帧只有 sid，无法定位变更，只能整表重拉。
3. **q/p 全量重拉**：`usePendingCards.ts:95-119`——`asked` 帧到达后 2s trailing **全量重拉两个聚合端点**（IMMEDIATE 帧已带补充字段却被当"变更信号"用，载荷未充分利用）。
4. **Sidebar 轻量缓存**：digest/resync → 5s trailing 重拉展开的收藏行（HANDOFF M4A-002 三表所有权模型）。

**收编建议**：① 单会话增量或 digest 帧携带定位字段（`directory`+变更 sid 列表）；② cross-directory 聚合"近期任务"端点；③ q/p IMMEDIATE 帧 payload 直接插入客户端（已含 `{directory,type,properties}`——`hub_types.py:71-76`）。

### 3.2 拉大 body 只用小部分（投影收编直接命中）

1. **merged 首页是头号案例**：生产画像首页拉取 **2× ~110KB**（`HANDOFF.md:89`），而渲染端 `MessageView.vue:199-203` 用 `summarize(text, 200)` 只显示 **200 字摘要**——全量文本下载、约 1% 上屏。**建议**：skeleton 投影对超长 text 内联截断（200–400 chars + `truncated` 标记），完整文本下沉 `/full`。
2. **ToolRun/PartCards 折叠态**：`PartCards.vue:35-85`——单 part 摘要卡只读 `state.input` 白名单字段 + `state.metadata.diffStats` + `patch.files[]`（前 3 个）+ `reasoning clip(120)` + `error clip(160)`。**建议**：`/full` 支持字段裁剪（`?fields=`）或折叠态改用 skeleton 投影。
3. **直读大字段全清单**：`MessageView.vue:199-203`（skeleton text → 200 字）、`PartCards.vue:35-85`（state/reasoning/error → 80–160 字、files 前 3）。reasoning 在 THINKING 卡恒 clip 120。

### 3.3 状态/链路遗留

- **ETag/304 文档漂移**：`README.md:16` 宣称"ETag/304 复用"，但 `endpoints.ts:32` 明确范围外，全仓无 ETag 客户端实现。sidecar 端已有 catalog 等 ETag 能力，webui 可低成本启用（sessions/directories/catalog 三处受益）。
- **detectApiMode 模块级缓存**（`HANDOFF.md:70`）：sidecar 运行中升级 v2→v3 需整页重载。
- **catalog 300s TTL 陈旧窗口**（同 :70）。
- **`slimapi.meta` 帧**：v3 契约有、sidecar 现网零实现（`useTokenStream.ts:23,279` 注释）。
- **Last-Event-ID 恒空**：sidecar `sse_frame` 从不发 `id:`（`connect.ts:111-117`），断线重连必须全量恢复（两路 SSE 皆然）。

### 3.4 TODO / 注释掉的调用

- 全仓无 TODO 标注的 API 调用。
- **`getCommands` 死代码**（api 层完整实现含 `POST /session/{sid}/command` body 形状，UI 零引用——`useCatalogs.ts:1-3` 注明 M5 移除）；但 `probeEndpoints` 仍探测 `/slimapi/command` 保底。

---

## 4. 未来拓展线索

| 线索 | 出处 | 对 sidecar 的需求 |
|---|---|---|
| **M4C `/file` 右侧栏文件树（延后，确定方向）** | `docs/m4-plan.md:85-87`（D12） | **三前置 = ① slimapi 提供 `directory` 白名单**（防 tailnet 任意目录读）→ ② serve 加 mount → ③ renderer L3 解禁（`renderer.ts:156-161` 现 `FORBIDDEN_PATH_PREFIXES=['/file']`）。渲染层四道防线（markdown-it html:false → 禁 image 规则 → validateLink 白名单 → DOMPurify）保持屏蔽 |
| 图片内联已否决（D9），但用户原话"保留，后续会希望浏览文件"（`HANDOFF.md:22`） | 同上 | 文件浏览是**确定**未来项；图片内联**不做** |
| command 执行 `POST /session/{sid}/command` 未启用 | `mvp-plan.md:259` | 启用时需加入合法集合——sidecar 可预置收编 |
| sidecar 3.0.0 删 v2/自定义头/catch-all | `mvp-plan.md:190` | webui 纯 v3 客户端零影响——**下一批收编应聚焦 v3 写路由形态** |
| slimapi.meta 帧、SSE `id:` 支持 | §3.3 | 契约已有、实现空缺 |

---

## 5. 移动端 / 省流约束现状

- **无客户端压缩层**：`Accept-Encoding` 是 Fetch forbidden header，浏览器透明 gzip——所有瘦身**全依赖 sidecar 投影**。
- **已落地省流手段（客户端节流，非内容裁剪）**：skeleton 投影、merged 内联、按需 `/full`、首屏 limit=20 lazy（M5）、`content-visibility` 屏外跳渲染、token 流增量帧、digest/asked 5s/2s trailing 节流、catalog 惰性加载、隐藏 tab 断流。
- 移动端呈现：`<60rem` 二态布局；无图片、无大二进制。

---

## 收编优先级建议（需求侧证据强度）

1. **merged/skeleton 长 text 截断下沉 `/full`**——直接砍首页 2×110KB 大头，webui 零改动可受益
2. **跨目录"近期任务"聚合或 digest 增量定位**——解决 30 目录 × 2 请求扇出
3. **q/p IMMEDIATE 帧载荷直接投递**——免全量重拉两聚合端点
4. **`/full` 字段裁剪**——折叠态大 part 免传全量
5. **SSE `id:` + `/slimapi/events` 重连增量**——重连从全量恢复降为增量
6. **`/file` + directory 白名单**——唯一"用户已表达确定意愿"的新端点，需安全前置
7. **`/slimapi/command` 收编**——probe 已在探，启用即需

**附带文档漂移**（webui 仓自身）：`README.md:16` "ETag/304 复用" 与实现不符。
