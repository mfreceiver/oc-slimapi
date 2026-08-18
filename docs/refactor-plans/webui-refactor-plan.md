# oc-webui 侧完整改造方案（B5a / B5b）

> **owner 终态裁决 2026-08-18**：协议封顶 4 系，(3,4) 永久双版本，5.0.0/B6-2/v2 退役取消。
>
> 三项目并行开发体系第三份。基准：[`docs/system-architecture-proposal-2026-08-17.md`](../system-architecture-proposal-2026-08-17.md)（v2.2，下称「v2.2」）；现状依据：[`docs/webui-needs-audit-2026-08-17.md`](../webui-needs-audit-2026-08-17.md)（下称「audit」）+ 实读 oc-webui 源码（Vue 3 + TypeScript + Vite + Vitest + pnpm；文件:行号均指 `oc-webui/` 仓库）。
> 本文档是 oc-webui 侧唯一定稿入口，供 omni-orch 统管并行开发；oc-webui 仓库文件一律不改（本方案落 oc-slimapi 仓库）。
> **评审合并轮说明**：第一轮合并修复轮（rev-sgpt 8.7/oracle 8.8 → 7 大项 Blocking）已并入：SSE id: 冻结 v4-only（§2.2 B5b-2）、q/p directory 三级降级（§2.1 B5a-2）、收藏分组 consumer 状态机（§2.2 B5b-1）、digest changed 定向发现重写并移至 P2（§2.1 B5a-5）、/file 部署形态 (a)（§2.2 B5b-3）、capability 逐键检查（§2.1 B5a-1）、结构收敛（§1.2 / §3 / §6 / §7 / 开放问题）。**第三轮（rev-sgpt R2 8.6 未过门控：4 Blocking + 1 Major + Minor；oracle 9.6 通过退出，其 2 处一行修正随手合入）** 已并入：①wire 版本协商统一化——selectedWireVersion 三概念模型（§2.1 B5a-1 / §4.3，与 ocdroid 方案 D-lane 同构）；②/file 路由实读修正为 query 形态 `/slimapi/file/content?directory=&path=`（§2.2 B5b-3 / F6，path-segment 形态 404）；③digest changed 处理单位 = 全部去重 (directory,sid) 对（upsert/remove 含成员，§2.1 B5a-5）；④degraded 下 complete 仅 best-effort——精确判据 `complete===true && degraded!==true`（§2.2 B5b-1 / F5 / R5）；Major：逐目录 allowlist 无 wire 数据源 → 保守二义「不可用/未知」（§2.2 B5b-1 / §5.3 / 开放问题 Q3）；oracle 修正：resyncRecovery 归属 useMessages.ts:596-668（§2.2 B5b-2 / §3 / §6 R8）、F5 响应 shape 补 degraded?:true、Q2 就地关闭（truncated 非 null text vs omitted text=null 互斥，F3）；Minor：global_hub.py directory :517、降级分支编号体系统一（L 系列）。**第四轮（rev-sgpt R3 9.1 未过门控：1 Blocker + 2 Major）** 已并入：①`/slimapi/file/content` 响应实为 **JSON `LegacyContent {type, content}`（非原始文件字节）**——B5b-3 改为 **webui 自有 viewer/download 适配**（fetch + 解析 + 按 type 渲染/下载，不再把 API URL 当文件 URL；§2.2 B5b-3 / F6）；②**owner 裁决**（v2.2 §3.3 :168-170，[3.2.0] 契约决策）：**TextPart.text 永不截断、永远全量内联**，400 码点 merged 截断**废止**，裁剪仅两模式（模式 1 omitted+expandRefs 默认不加载 / 模式 2 未展开缩略信息如 diffStats）——B5a-3 由实现任务改写为**验证任务**、F3 互斥定义作废（§1.1 目标 2 / §2.1 B5a-3 / F3 / §4.3）；③**capability 缺失 = 该功能 fail-closed（禁用 + UI 提示，不发 v3 请求）**——[4] 服务端无 v3 通道；仅 available 同时含 3 时整体降回 3（全端点一致，非 per-feature 混用）（§2.1 B5a-1 / F4 / §6 R6）。

---

## 1. 目标与范围

### 1.1 目标（承接 v2.2 §6 webui 节，B5a/B5b 同构）

**B5a（P2，先于 sidecar 4.0.0，纯客户端 + 已发布 wire 的消费）**

1. **q/p asked 帧载荷直投**：把 usePendingCards 对 q/p 帧的「变更信号 → 2s trailing 全量重拉两聚合端点」改为「帧载荷直接进 UI」。前置 = sidecar B1b 的 properties 完整性核对（§4.2）；若核对结论为「完整」，**零 wire 变更**，纯客户端改动，先于 sidecar 4.0.0 可发。directory 定位按**三级降级**（§2.1 任务 B5a-2），不做任何 sid→路径推断。
2. **折叠内容两模式裁剪的 webui 兼容验证**：v2.2 §3.3 owner 裁决（[3.2.0] 契约决策）：**正文 TextPart.text 永不截断、永远全量内联**；裁剪对象 = 折叠内容，仅两模式（**模式 1** = omitted+expandRefs 默认不加载 / **模式 2** = 未展开缩略信息如 diffStats）。验证 webui 渲染路径对已发布的两模式裁剪**零改动受益**（正文全量内联无需处理）；「merged 400 码点截断」已废止（§2.1 任务 B5a-3 改写为验证任务）。
3. **wire 版本协商统一化（selectedWireVersion 三概念模型，评审 B1，与 ocdroid 方案 D-lane 同构）**：请求级统一版本状态 `selectedWireVersion = highest(clientSupported ∩ serverAvailable)`，**所有 /slimapi HTTP 与 SSE 端点（含 events/token stream）跟随**；B5a 阶段 clientSupported={3} 继续走 v3（含 legacy gate 保留），B5b 适配后 clientSupported={3,4} 在 [3,4]/[4] 上**成功选 4**；capability 逐键开关仅 `selectedWireVersion==4` 时生效（§2.1 任务 B5a-1 + §4.3）。
4. **digest changed 定向发现（从 B5b 移至 B5a/P2）**：changed 是 3.3.0 加性能力，消费不必等 4.0.0。真正价值 = **新 sid 定向发现**（现 useSessionList.ts:246 成员守卫把新会话 digest 静默丢弃，要等 resync 才出现），非「整表重拉替代」——现状 digest 后本就只做 status 精刷（§2.1 任务 B5a-5，动机重锚见该任务）。

**B5b（sidecar B3a/B3b 后，配合 wire v4）**

5. **收藏扇出 → 全局列表一次拉取 + 客户端分组**：useRecentSessions 的 30 目录 × 2 请求扇出（audit 痛点①）改为 v4 `parent=none` 全局列表一次拉取 + 客户端按 directory 分组（**`selectedWireVersion==4` 启用，B5a-1 协商**）；分组停止规则/空组判定按 **consumer 状态机冻结**（§2.2 任务 B5b-1）。
6. **SSE id: 重放消费**：**id:/Last-Event-ID 重放严格 v4-only**（v3 契约 §7 冻结语义不动）；v4 后 /slimapi/events 流带 `id:`，断线重连增量恢复替代全量 resync（§2.2 任务 B5b-2）。
7. **/file 通路**：定案形态 (a)——渲染 href 经 `fileUrl` 辅助统一产出 **query 形态** `/slimapi/file/content?directory=...&path=...`（path-segment 形态不存在，会 404）+ renderer 放行 `/slimapi/file/content` 前缀，**不新增 serve mount**；**响应为 JSON `LegacyContent {type, content}`（非原始字节）——经新增 FileViewer fetch 解析后按 type 渲染/下载，不把 API URL 当文件 URL（评审 R3 Blocker）**（§2.2 任务 B5b-3）。

### 1.2 不做什么（明确排除）

| 排除项 | 理由 |
|---|---|
| command 启用 / getCommands 死代码**功能化** | v2.2 §8 B6 之外无客户诉求；死代码清理归 B6（getCommands 仅在 probeEndpoints 探测，保留即可） |
| 图片内联渲染 | D9 已否决（audit §3.2），renderer 保持禁 image 规则（markdown/renderer.ts L2） |
| **ETag/304 客户端缓存** | **非目标（评审 Q3 转正）**：v2.2 无该 workstream；audit §2.6「低成本可做」观察仅记录，不纳入本方案，如需另行立项（omni-orch 裁决） |
| v2 模式退役 | v2 路径保留（v3 契约 §8 catch-all 终局后 v2 消费仍可用）；sidecar 5.0.0=(4,4) 删 v3 时再评估，B5 两阶段不碰 v2 |
| 目录选择器 UI 重构（directory picker） | 非省流问题，无 v2.2 workstream |
| zstd / 内容编码 | v2.2 明确不做 |
| 客户端消息水印/增量 SSE（/stream 帧重放） | v2.2 范围仅 /slimapi/events 聚合流 id: 重放（§3.2）；/stream 语义不变（v3 契约 §7 帧形零变化） |
| serve 新增 `/file` mount | **评审定案否决**：sidecar 无 `/file` 路由（catch-all 已关），新增 mount 反而 404；走现成 `/slimapi` mount 前缀保真 + query 形态 `/slimapi/file/content`（§2.2 任务 B5b-3） |

### 1.3 技术栈事实修正

任务描述称 oc-webui 为 React+TS；**实读证明为 Vue 3 + TypeScript**（package.json: `vue ^3.5.40`、`vue-router`、`markdown-it`、`dompurify`、`shiki`；组件为 `.vue` SFC，composables 在 `src/composables/`，SSE 层在 `src/sse/`，API 层在 `src/api/`，渲染管线在 `src/markdown/`）。本方案所有文件路径/行号均按 Vue 3 代码库实读结果。

### 1.4 先行 micro-PR（oracle n5）

`src/api/protocol.ts`（类型）与 `src/api/endpoints.ts`（URL/query 构造）的类型与 URL 收敛**先行拆为独立 micro-PR 合入**，再并行 B5a/B5b 各 lane——消除 L1/L4/L6 三向 merge 对同一文件的冲突（§3 写域矩阵据此调整：protocol.ts/endpoints.ts 视为「已收敛基线」，各 lane 只追加增量类型/函数）。

---

## 2. 阶段执行计划

### 2.1 B5a（P2，先于 sidecar 4.0.0）

**依赖**：sidecar B1b 核对结论（q/p properties 完整性 + **两套字段表**，§4.2——唯一阻塞性外部依赖）；v3.1.0（折叠内容两模式裁剪，**已发**——正文 TextPart.text 全量内联，为 B5a-3 验证任务前置）；3.3.0（digest changed，B5a-5 消费）。**零 wire 变更**（若 B1b 核对完整）。**B5a 阶段 clientSupported={3}（§2.1 任务 B5a-1）**：selectedWireVersion 恒为 3（[3] / [3,4] 均选 3），并行期（wire (3,4)）探测能力键但**不触发任何 v4 行为**——B5a-1/2/3/4 可在 3.3.0 发版前按 B1b 核对结论并行开发；B5a-5 依赖 3.3.0 发布后实测。

#### 任务 B5a-1：wire 版本协商统一化（selectedWireVersion 三概念模型，评审 B1）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/api/health.ts`（checkVersionGate）、`src/api/endpoints.ts`（detectApiMode:84-143 + 全部端点 URL 构造）、`src/api/protocol.ts`（VersionsResponse/VersionCapabilities 类型 + 版本常量）、`src/sse/connect.ts`（SSE URL 构造） |
| 三概念模型（冻结） | ① **请求级统一版本状态**：`selectedWireVersion = highest(clientSupported ∩ serverAvailable)`——裸 `GET /slimapi/versions` 后一次计算，模块级持有（沿用 detectApiMode 模块级缓存语义，失败不缓存）。② **所有 `/slimapi` HTTP 与 SSE 端点跟随 selectedWireVersion**（含 `/slimapi/events` 与 `/slimapi/sessions/{sid}/stream` token stream）——**冻结「选 v4 后 events/token stream 也必须 `?v=4`」**：任何端点不得独立选版本（消除现计划中「B5b 宣称兼容 5.0.0 但 events 仍 ?v=3」的自相矛盾）。③ **capability 逐键开关仅 `selectedWireVersion==4` 时生效**：v3 连接下 `capabilities["4"]` 键**不触发任何行为**（v3 获得的是 v3 语义，capabilities["4"] 只描述 v4 能力存在性）。 |
| 实现要点 | ① protocol.ts：`CLIENT_SUPPORTED_WIRE: number[]` 常量——**B5a 阶段 `[3]`，B5b-1 合入时升为 `[3,4]`**（单一改动点）；`V4_CAPABILITY_KEYS = ['globalSessions','sseReplay','qpImmediateFull','auxiliaryFilters']`（对齐 v2.2 §7 四键）。② endpoints.ts detectApiMode 重写：versions 响应 → `selectedWireVersion = highest(CLIENT_SUPPORTED_WIRE ∩ available)`；**空交集 → legacy fail-closed**（仅 B5a 旧客户端 {3} 遇 v4-only sidecar 触发——保留原 checkVersionGate 失败语义于 legacy 路径，不静默降级）。③ **版本跟随实现**：modeGet/modePost/SSE URL 统一经 `wireVersionQuery()` 辅助（`?v=3` / `?v=4`），不再散落硬编码；token stream 与 events 的 URL 构造同一来源（connect.ts 新增参数化）。④ `v4FeatureEnabled(caps, key)`：**先判 `selectedWireVersion==4`，否则恒 false**；==4 时逐键检查（`globalSessions`→B5b-1、`sseReplay`→B5b-2、`qpImmediateFull`→B5a-2 v4 补全、`auxiliaryFilters`→v4 sessions 过滤）。**能力键缺失 = 该功能 fail-closed（评审 R3 Major2）**：**禁用 + UI 提示，绝不发 v3 请求**——[4] 服务端无 v3 通道，v4 selector 不能执行 v3 语义（directory/roots 已退役，无 per-feature 回退路径）；**仅当 available 同时含 3 与 4 时**，才允许整体重新协商 `selectedWireVersion` 降回 3（全端点一致切换，**非按功能混用**——可整体降、不可 per-feature 混跑）。⑤ health.ts checkVersionGate：**保留但语义更新**——B5a 旧客户端（CLIENT_SUPPORTED_WIRE=[3]）遇 [4] → fail-closed（legacy 路径）；B5b 后客户端（[3,4]）遇 [4] → 成功选 4 不 gate。 |
| 验收标准 | ① **B5a 阶段（clientSupported={3}）**：sidecar [3]→选 3；[3,4]→选 3（B5a 继续 v3，探测能力键但不切换）；[4]→fail-closed（legacy gate）。② **B5b 阶段（clientSupported={3,4}）**：[3]→选 3；[3,4]→选 4（并行期切 v4）；[4]→选 4（**成功**，不 gate 失败）。③ 版本跟随：选 4 后 events 与 token stream URL 均 `?v=4`（网络面板断言）；选 3 后全部 `?v=3`。④ 逐键隔离：selectedWireVersion==3 时 `capabilities["4"]` 存在也不触发任何行为；==4 时缺单键 → 该功能 **fail-closed（禁用 + UI 提示）不发 v3 请求**；available 同时含 3 时可整体降回 3（全端点一致切换），**无 per-feature v3 混用（评审 R3 Major2）**。 |
| 测试/验证 | `endpoints.test`：协商三模 × 两代客户端（[3]/[3,4]/[4] × B5a {3} / B5b {3,4}）共 6 分支；wireVersionQuery 端点跟随（events/token stream/HTTP 一致）；空交集 fail-closed。`health.test`：legacy gate（B5a 客户端 [4] → fail）。`connect.test`：SSE URL 版本参数跟随 selectedWireVersion。HomeView 门闩链集成（mock 序列断言版本状态与端点 URL）。**`endpoints.test`：[4] + 缺 globalSessions → 不发 v3 sessions 请求、功能禁用（评审 R3 Major2）**。 |
| 工作量 | S（~0.5 人日） |

#### 任务 B5a-2：q/p asked 帧载荷直投（directory 三级降级）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/composables/usePendingCards.ts`（核心，onEvent:155-180、refresh:95-119）、`src/api/protocol.ts`（QpControlFrame 收紧）、`src/views/HomeView.vue`（handleSseFrame q/p 分支 :289-301，仅事件透传已就绪，基本不动） |
| 实现要点 | ① 前置核对（§4.2）：sidecar B1b 产出**两套字段表**——完整直投字段集 / 最小可渲染字段集；global_hub.py:522 `props = payload.get("properties")` 原样透传已实证（帧顶层 `directory` 亦已透传，global_hub.py:513-529 区间，**directory 赋值实为 :517**）。核对结论「properties ≥ 最小集且关键字段（id/canonicalID）存在」→ 进入 ② 直投；「低于最小集」→ **走 L3 降级**（见 ② 的 L1/L2/L3 序）。② **直投路径（主线）**：`onEvent(asked)`——帧 `{directory, type, properties}` → 规范化为 QuestionEntry/PermissionEntry → **directory 三级降级（评审 B2，禁 sid 前缀推断——sid 是 opaque 不编码路径，不可实现）**：**L1** 帧顶层 `directory`（已透传，首选）→ **L2** 确定性 `sid → directory` 缓存（本会话内此前从 digest 帧 / 列表项建立过该 sid 的目录映射；仅查缓存，不推导）→ **L3** 两者皆不可用 → **不插卡片**，触发 authoritative `/questions`/`/permissions` 刷新（fail-open 到现状语义）。**L1/L2/L3 为冻结降级序，不跳跃**（编号体系：降级分支全用 L 系列，流程步骤全用数字系列，互不混引）。③ **最小集门槛**：字段达到最小可渲染集但非完整 → 插入卡片（可渲染部分 + 缺字段占位）；**低于最小集 → 不建半残卡片**（**L3 降级**，不插卡 + authoritative 刷新）。④ **去重**：本地 (directory, id) 二元组幂等覆盖；重复帧覆盖更新。⑤ **resolved 乐观移除保留**（已有 (directory,canonicalId) 二元组逻辑）→ resolved 后**保留**一次 2s trailing 兜底重拉（对账，成本低）。⑥ **冷启动/权威对账语义保留**：页面加载 refresh() 全量拉取两聚合端点不变；resync → 全量对账不变（v2.2 §3.2a「resync 驱动权威校准」）。⑦ 若 B1b 核对结论「properties 缺关键字段」→ 完整直投移 v4-only（`qpImmediateFull` 能力键，B3b），B5a-2 降级为部分直投 + 帧补全后 B5b 再切完整。 |
| 验收标准 | **帧到达 → 0 次聚合端点重拉**（浏览器网络面板/Performance entries 计数；audit 痛点③归零）；新 asked 卡片即时出现；resolved 乐观移除即时生效且兜底对账仍发生；**三级降级行为正确**：帧无顶层 directory 且 sid 无缓存 → 不插卡片 + authoritative 刷新；冷启动/resync 全量拉取行为不变。 |
| 测试/验证 | 重写 `usePendingCards.test`：asked 帧注入 → 断言 `getQuestions`/`getPermissions` **零调用** + 卡片插入；**降级分支**：缺顶层 directory（L2 缓存命中 / L3 缓存未命 → 刷新）；缺 canonical ID；达最小集但不完整（插占位）；**低于最小集（不插卡 + 刷新）**；resolved → 乐观移除 + 1 次兜底重拉；**authoritative 刷新失败不删除已有卡片**（F8 per-lane 错误缓存语义保持）；重复帧幂等。浏览器手测：DevTools Network 过滤 `/slimapi/questions|permissions`，触发一次 asked，断言 0 请求。 |
| 工作量 | M（~2 人日） |

#### 任务 B5a-3：折叠内容两模式裁剪的 webui 兼容验证（M6 回归）

| 项 | 内容 |
|---|---|
| 状态 | **验证任务（评审 R3 Major1，owner 裁决 v2.2 §3.3 :168-170）**：原「merged 400 码点截断 + truncated 标记消费」任务整体**废止**——权威冻结「正文 TextPart.text 永不截取、永远全量内联、不折叠、无阈值」；裁剪仅两模式（**模式 1** = 默认完全不加载的折叠内容 omitted+expandRefs（展开时经 expand 端点拉取）/ **模式 2** = 未展开缩略信息如 diffStats）。本任务降级为**纯验证 + 回归，无功能开发**。 |
| 涉及文件 | `src/chat/blocks.ts`（验证 classifyPart:135-168 折叠分类：omitted→conversation+placeholder、缩略信息类→Tag/摘要渲染）、`src/components/MessageView.vue`（验证折叠渲染 + 展开入口）、`src/components/ExpandCard.vue` + `expandCache.ts`（验证 expandRefs 恢复路径，3.1.0 体系复用）、`src/api/protocol.ts`（`truncated` 类型清理/标注 deprecated） |
| 实现要点 | ① **零改动受益确认**：v3.1.0 已发布两模式裁剪（模式 1 折叠内容默认不加载 + 模式 2 缩略信息）——webui 渲染路径（blocks.ts 已分类、ExpandCard 3.1.0 已支持展开恢复）**自动兼容，本任务不改任何请求/渲染逻辑**；首页 merged 载荷下降（折叠内容不再内联）自动发生。② **正文无需处理**：TextPart.text 全量内联永不截断——MessageView summarize(200) 缩略展示是**客户端展示层**行为（接收层已有全量正文），与 wire 裁剪无关，保持不变。③ **truncated 清理（评审 R3 Major1）**：`truncated:true` **不再是 TextPart.text 的合法 wire 状态**——原「truncated⇒text!=null / omitted⇒text=null 互斥定义」**作废**（F3 已按 owner 裁决改写）；若折叠内容预览摘要语境（模式 2）出现 truncated 标记则仅限「折叠内容预览有截断」语义，protocol.ts 移除 `truncated` 或标注 deprecated（无业务依赖确认）。④ **M6 历史长消息空展示回归**：根因 = omitted 折叠被 UI 当完整文本——placeholder「加载全文」入口已存在（row.id 路径），**回归验证覆盖**（非新增功能）。 |
| 验收标准 | 两模式裁剪下首页字节自动下降（DevTools Network 量级对比 3.1.0 前）；折叠内容**默认不加载、展开后经 expandRefs 恢复完整**；**正文完整显示、无任何截断**；无空展示回归（折叠内容必有展开入口或缩略信息）；`truncated` 无 UE 依赖（代码清理或 deprecated 标注通过）。 |
| 测试/验证 | `blocks.test` 回归（omitted 折叠分类不变、缩略信息类渲染）；`MessageView.test` 回归（placeholder 展开入口、expand 恢复）；`protocol.test`：MessagePartSkeleton 类型清理/标注 deprecated；人工：打开含超长 diff/tool state 的旧会话，断言正文完整 + 折叠内容可展开。合约回归（sidecar 单测已保证两模式裁剪语义，webui 侧只测消费）。 |
| 工作量 | S（~0.5 人日，由 1 人日降——实现任务已废止，仅验证+回归） |

#### 任务 B5a-4（并入 B5a）：status 单次全局调用（v2.2 决策2 同构）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/api/endpoints.ts`（getSessionStatus:323-330，v3 下 directory 参数可省）、`src/composables/useSessionList.ts`（refreshStatuses）、`src/composables/useRecentSessions.ts`（M4A-003 statuses 键 `directory\0sid`） |
| 实现要点 | v2.2 §1.2/决策2：上游 status 本就全局（sessions.py:350-368 转发上游全局 map，directory 不改变结果）——**getSessionStatus 一次无 directory 调用取代每目录调用**（useRecentSessions 扇出 30 次 status → 1 次）。**v3 下即做**（sidecar 0 变更，纯客户端）；v4 B5b-1 时合并进全局拉取形态。statuses 键保留 `directory\0sid`（数据是全局 map 拆分而来，消费端隔离语义不变，M4A-003 不动）。 |
| 验收标准 | 收藏 N 目录扇出从 2N 请求降至 N+1（sessions N + status 1）；status 结果与逐目录调用一致（上游 map 全局性实证）。 |
| 测试/验证 | `useRecentSessions.test`：断言 getSessionStatus 调用 1 次；`endpoints.test`：getSessionStatus 无 directory 时 URL 形态正确。 |
| 工作量 | S（~0.5 人日，与 B5b-1 文件重叠，注意写域时序 §3） |

#### 任务 B5a-5：digest changed 定向发现（从 B5b 移入，P2）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/api/protocol.ts`（DigestPayload 增 `changed?: string[]`）、`src/composables/useSessionList.ts`（consumeDigest:230-264）、`src/composables/useRecentSessions.ts`（onDigest 协同） |
| 动机重锚（评审 B4，实读证实） | 现状 digest 后**并非**整表重拉：consumeDigest:263 仅 `scheduleRecovery('status')`（status 精刷）；整表重拉只在 resync/full 路径。**changed 的真正价值 = 定向精拉**——:246 成员守卫 `if (!sessions.value.some((s) => s.id === sid)) return` 把**新会话**的 digest 静默丢弃（旧 sid 的 digest 因成员判定通过而走 status 精刷），新会话要等下一次 resync 才进入列表。HomeView.vue:277-279 的 digest 三投递中 2N 扇出（recent.onDigest 5s trailing）由 B5b-1 全局分组消解，**非本任务**。**处理范围（评审 B3）**：**不限非成员**——已成员 sid 同样精拉（现状 status 精刷只更新 status 不刷新 skeleton 标题/时间，skeleton 更新会丢失）；404 对成员同样触发移除（现状成员先被过滤 → 删除永不触发）。 |
| 实现要点 | ① **per-sid 精拉 API**：`GET /slimapi/session/{sid}?v=3&directory=<digest.directory>`（read_groups.py:190-205 session_single 单查路由，directory 可选 query，skeleton_session 投影，**v3 已可用**——`start` 是时间戳过滤非 SID、`roots` 语义 v4 已退役，均不用）。② **消费流程（处理单位 = changed 全部去重后的 (directory, sid) 对，评审 B3）**：digest 帧带 `changed:[sid…]`（v3 加性字段，3.3.0）→ 对**每个去重后的 (directory, sid) 对**（含已成员，不预过滤）做 per-sid 精拉：**200** → **upsert**（成员：刷新 skeleton——标题/时间等变更即获更新；非成员：插入列表——`:246` 守卫之前的定向入列）；返回 directory 与本地分组目录不同 → **移组**（B5b-1 分组模式下）；**404** → **remove**（成员/非成员一律移除，`skeleton 404 已删除` 语义）；digest 缺 directory → 查 sid→directory 缓存（L2 同源缓存），无缓存 → 回退全量刷新（fail-open）。③ **隔离与阈值**：单 sid 失败不破坏其他 changed 项（Promise.allSettled + F1 partial 语义先例）；`changed` 长度 > 阈值（**20**）→ 退化全量列表刷新（防风暴）。④ `changed` 缺失（旧 sidecar）→ 现状行为兜底（字段可忽略性保障兼容）。⑤ **协同 B5b-1**：全局分组模式下 digest changed 只重算受影响目录组（组内定向更新）；B5b-1 依赖 B5a-5 已落地的新 sid 入列语义（列表完整是分组准确的前提）。 |
| 验收标准 | digest 帧带 changed → **全部去重 (directory, sid) 对定向精拉**：新 sid 入列（**不等 resync**）；**已成员 sid skeleton 刷新（标题/时间变更即时呈现，评审 B3）**；404 → **成员/非成员一律移除**；目录变更 → 移组；单 sid 失败隔离；>20 → 全量刷新；旧 sidecar 行为不变。 |
| 测试/验证 | `useSessionList.test`：changed 分支（新 sid 入列、**成员 upsert skeleton 刷新**、**成员 404 移除**、非成员 200/404、目录变更移组、单失败隔离、阈值退化、缺 directory 回退、重复 (directory,sid) 去重）；`protocol.test`：DigestPayload 解析 changed；人工：运行中新会话出现 → 列表即时出现（无 resync 等待）。 |
| 工作量 | M（~1.5 人日） |

### 2.2 B5b（sidecar B3a/B3b 后，wire v4）

**依赖**：sidecar **4.0.0（wire (3,4)）**；v4 sessions DB 投影源（B3a）；SSE id:/重放（B3b，/slimapi/events 流）；/file allowlist（B4，3.3.0 已含 §3.5）；digest `changed`（B1a，3.3.0 已含，**消费已在 B5a-5 落地**）。**版本协商（评审 B1）**：B5b-1 合入时 `CLIENT_SUPPORTED_WIRE` 升为 `[3,4]`（§2.1 任务 B5a-1 单一改动点）——sidecar [3]→选 3 / [3,4]→选 4 / [4]→选 4（**均成功**，不 gate 失败）；选 v4 后**全部端点（含 events/token stream）跟随 `?v=4`**；B5b 各任务在 `selectedWireVersion==4` 才启用对应能力。

#### 任务 B5b-1：收藏扇出 → 全局列表一次拉取 + 客户端分组（consumer 状态机冻结）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/api/endpoints.ts`（getSessions v4 形态）、`src/api/protocol.ts`（SessionSkeletonV4/Page 复用 + v4 query 构造）、`src/composables/useRecentSessions.ts`（**重写核心**：refresh():134-158）、`src/composables/useSessionList.ts`（v4 适配：parent=none + cursor 翻页）、`src/composables/useProjectFavorites.ts`（仅依赖 normalizeDirKey，不动）、`src/components/Sidebar.vue`（收藏行数据源切换：ensureLightSessions:285-337 轻量拉取 → 全局分组数据） |
| 实现要点 | ① **v4 请求形态**：`getSessions({parent:'none'})`（v2.2 §3.1：parent=none 承接 roots=true；v4 零 directory 参数——`directory_retired_in_v4`；cursor keyset + limit≤500）。② **全局分组**：一次（或按需多页）拉取全部根会话 → 客户端按 `item.directory` 分组 → 收藏目录各取 top-N（RECENT_TOP=10 保留）+ Sidebar count 用 `activeRootSessionCount`（Sidebar.vue:206-210 字段语义）。③ **consumer 状态机（评审 B3/B4，冻结）**：
  - **停止规则（评审 B4，精确判据冻结）**：**精确 count 与空组判定仅 `complete === true && degraded !== true` 后成立**（`degraded?: true` 已在响应 shape，F5；degraded 下 complete 为 best-effort，架构 §3.1 :111-123/:137）；Top-N 模式 = 所有收藏组满 N 可提前停；**任一组不足 N → 继续读至精确终点**（complete && !degraded）。**degraded 响应上禁止精确空组/count 结论**——UI 显示「数据可能不完整」。
  - **首屏直接 degraded（评审 B4 补用例）**：**首次无 cursor 请求也可能直接 `200 + degraded:true + complete:true`**（降级矩阵：archived=omit|all + parent∈{all,none} + 无 cursor + search 任意 → 200+degraded，v2.2 §3.1——不经历 503）——该响应同样**不得**作精确空组/count 判据（标 partial/degraded，UI「数据可能不完整」；需精确计数时经用户显式触发的整体版本重协商降回 v3 fan-out 对账——§6 R1 开关语义，非 per-feature 混发）。
  - **页数硬上限**：5 页（2500 行）——超限未 complete 的组标 `partial` + UI「加载更多」入口（手动续拉），不静默截断。
  - **cursor 三分支**：**503 auxiliary_unavailable**（带 cursor 请求）→ 清 cursor 取无 cursor 降级页 + 明确标 `partial/degraded` + **不清零未覆盖收藏组**（组内空不解释为「无会话」）+ 需精确计数时**经用户显式触发的整体版本重协商降回 v3**（覆写 selectedWireVersion=3、全端点一致切 v3，可用 available 含 3 为前提——§6 R1 开关语义，非 per-feature 混发）；**400 invalid_cursor**（keyset 指纹失配）→ **丢 cursor 重拉首页 + 指数退避防循环**（评审补，原方案缺失）；正常 → 续拉 nextCursor。
  - **degraded:true UI 呈现**（一行）：收藏区显示「数据源降级，排序可能非全局精确」（v2.2 决策9：degraded 仅表数据源降级+强度弱化，过滤语义不降级；配合「数据可能不完整」防误判）。
  - **组内空 vs 未加载 vs 不可访问（评审 Major 保守二义）**：**精确空组**（complete && !degraded 后为空）= 「无会话」；未加载/未 complete = 「继续加载」；**/directories 缺失或请求失败 → 统一「不可用/未知」**——不区分「未授权 vs 空目录 vs 未加载」（无 wire 数据源可区分：health 仅广播 allowlist 非空布尔、无逐目录授权 map，架构 §3.5）；**「逐目录授权信号」记为 sidecar 开放问题（未来 wire 信号），本计划不实现**（开放问题 Q3）。
  ④ **Sidebar 收藏行零独立拉取**：展开收藏行数据源改为全局分组结果（LIGHT_LIMIT=50 轻量拉取退役或降级为仅非收藏目录用）；digest/resync 后重算只影响受影响目录组（B5a-5 changed 协同）。 |
| 验收标准 | **扇出归零**：收藏 N 目录 → 请求计数 2N（现状）→ ≤2+⌈页⌉（全局 sessions + 1 status + 续页）；Sidebar 展开收藏行 0 独立请求；**状态机行为正确**：总量>500 自动续拉；某组不足 10 条仍读到精确终点；真空组（complete && !degraded 后）显示「无会话」；未加载组显示「继续加载」；**首屏直接 degraded:true（无 cursor）→ 不判精确空组/count、UI「数据可能不完整」**（评审 B4 补）；第二页 503 → partial+degraded 呈现 + 未覆盖组不清零；搜索词变更触发 400 → 重拉首页+退避。 |
| 测试/验证 | 重写 `useRecentSessions.test`（状态机用例全列）：全局数据 → 分组断言；**总量>500（两页）**；**收藏目录仅在第二页出现**；**某组不足 10 条**；**真空组 vs 未加载到**（complete && !degraded 前/后判定）；**首屏直接 degraded:true**（无 cursor 请求 → 200+degraded+complete，断言不判精确空组、标 partial、「数据可能不完整」）；**第二页 503**（清 cursor 降级页 + partial）；**降级首页不覆盖全部收藏目录**（组不清零）；**搜索词变更触发 400**（丢 cursor 重拉 + 退避）。`endpoints.test`：v4 getSessions URL/query 形态（?v=4&parent=none、cursor、limit=500）+ **degraded?:true 响应解析**。浏览器：DevTools Network 计数 + 收藏 30 目录手测。 |
| 工作量 | L（~4 人日，核心改造） |

#### 任务 B5b-2：SSE id: 重放消费（v4-only 冻结）

| 项 | 内容 |
|---|---|
| 涉及文件 | `src/sse/connect.ts`（客户端已就绪：per-connection `lastEventId` :117、重连 Last-Event-ID 头 :177-179 已实现）、`src/sse/parse.ts`（id: 解析已实现 :84-90，`SseFrame.id?: string` :23-30 **已暴露给 dispatch 消费方**，oracle n3 核实确认）、`src/composables/useSessionList.ts`（全局 events 流恢复面：**onResync/scheduleRecovery——oracle 修正，useSessionList 侧恢复路径非「resyncRecovery」函数**）、`src/composables/useMessages.ts`（**resyncRecovery:596-668 实属此文件**——token stream 侧权威恢复实现，gap→全量 resync 回退的模式参考）、`src/views/HomeView.vue`（onResync 分发）、`src/composables/usePendingCards.ts`（resync 对账保留） |
| **v4-only 冻结语义（评审 B1，零偏离 v2.2 §3.2/§7）** | **v3 流绝不输出 `id:` 字段**；v3 客户端（含本 webui 在 v3 模式下）对 Last-Event-ID 一律不解释（服务端忽略，客户端 reconnect 语义不变——`reconnect_no_replay` 全量 resync，现状路径不动）；**v4 才启用** `id:`/Last-Event-ID 寻址/有界回放。**不得提前到 3.3.0**（v3 契约 §7「帧名/帧形/Last-Event-ID/resync/heartbeat 零变化」冻结）。 |
| 实现要点 | **版本前提（评审 B1）**：本任务仅在 `selectedWireVersion==4` 启用（B5a-1 协商）；选 v4 后 events 与 token stream URL 均经 `wireVersionQuery()` 带 `?v=4`（版本跟随冻结，无独立选择）。① **去重键 = `(streamScope, epoch, seq)` 三元组（评审 B1）**：streamScope 区分连接实例（`'global'` / `'token'`——connect.ts:117 的 lastEventId 为 per-connection 闭包，天然隔离，测试确认该不变量）；epoch 为 sidecar 进程代（v4 重放协议，epoch 变化须重置去重状态）；seq 单调序——**只比 seq 不够，epoch 变化 seq 归零须重置**。② v4 重放（B3b，/slimapi/events 流）：重连带 Last-Event-ID → sidecar 回放缺口帧 → 客户端**按帧增量更新**（B5a-5 的定向消费语义复用）而非全量 resync 冷启动；`server.connected` 后回放完成 → 正常续流。③ **gap 语义**：重放日志有界（进程 epoch + 单调 seq + 环形，v2.2 §3.2）——sidecar 返回 gap/resync → 客户端回退现有全量 resync + snapshot 对账（保留现路径，勿删；**实现参考 useMessages.ts:596-668 resyncRecovery 模式**——drain + 锚点校验 + 恢复完成信号）。 |
| 验收标准 | **断线恢复 O(缺口)**：断线 30s 后重连 → 网络面板请求/帧数 ∝ 缺口事件数（非全量 sessions 重拉）；Last-Event-ID 头携带正确；**去重正确**：同 epoch 序号回退（乱序）→ 抑制；epoch 改变且 seq 归零 → 不误抑制；gap 场景 → 全量 resync 回退无死循环；**v3 模式重连 → 完整 resync**（id: 不出现、Last-Event-ID 无服务端响应、行为与现状一致）。 |
| 测试/验证 | `connect.test`：Last-Event-ID 头断言（现有 :177-179 实现补强）；**去重用例**：同 epoch 序号回退 / epoch 改变且 seq 归零 / **global 与 token stream ID 状态互不污染**（per-connection 隔离断言）；**v3 重连走完整 resync**（无 id: 帧 → 行为等同现状）。`useSessionList.test`：重放增量分支 vs resync 全量分支；gap 回退。人工：DevTools offline 30s → 恢复，计数恢复期间帧数。 |
| 工作量 | M（~1.5 人日；大量是验证「客户端已就绪」，增量开发集中在去重键/gap 回退） |

#### 任务 B5b-3：/file 通路（部署形态定案 (a) — query 形态）

| 项 | 内容 |
|---|---|
| 涉及文件 | **oc-webui 侧**：`src/markdown/renderer.ts`（L3 validateLink:184-200 + FORBIDDEN_PATH_PREFIXES:161）、`src/api/endpoints.ts`（fileUrl 辅助）、**`src/components/FileViewer.vue`（新增：LegacyContent 解析与按 type 渲染/下载）**、错误体解析（403 directory_not_allowed）；**oc-slimapi 侧（只读依赖，终检 C4 裁决：`docs/operations.md`/部署文档由 S 方案 B4 独占写入——webui 侧仅按其部署形态消费，不修改 oc-slimapi 仓库任何文件）**：部署文档（OC_SLIMAPI_DIRECTORY_ALLOWLIST systemd 环境变量）部署形态参照 |
| 部署形态定案（评审 B1 实读修正 + 评审 R3 Blocker 响应形态修正） | **URL 形态 = query 形态：`GET /slimapi/file/content?directory=<enc>&path=<enc>`**——实读 `routes/read_groups.py:86-110` 证实 `path` 是 FastAPI **query 参数**（`async def file_content(request, path: str, directory: str | None = None)`；路由表 :1-20 另有 `/slimapi/file`、`/slimapi/file/status`）；v3-contract.md:198 确认。**path-segment 形态不存在**（catch-all 已关 → 404 `thin_route_not_found`）。**响应形态（评审 R3 Blocker，本期最关键修正）：`/slimapi/file/content` 返回 JSON `LegacyContent {type, content}`，非原始文件字节**——证据：tests/test_read_groups.py:55-56（响应形状）、:117-123（上游 Content-Type=application/json，sidecar 原样透传）、:236-242（JSON 透传断言）；src/oc_slimapi/routes/_read_passthrough.py:270-277（纯透传，无字节转换）。**`type` 仅 `"text" | "binary"` 两个合法值（评审 R4 实读：read_groups.py:101-108 注释；INTERFACE_MAP.md:40 `{type:"text"|"binary", content, diff?, patch?, encoding?, mimeType?}`；上游 file.ts:62-85 schema + :110-123 实际行为——不存在 `type:"image"`）**；真实图片形态 = `{"type":"binary","content":"<base64>","encoding":"base64","mimeType":"image/png"}`。**直接新标签页打开会造成「打开 JSON 文档」体验**、binary 无 `body` 内联展示——故 URL 仅作 API fetch 目标，不作浏览器文件 URL（下一行 viewer 适配）。**删除 serve 新增 `/file` mount 步骤**（sidecar 无 /file 路由）。 |
| 实现要点 | 三前置（v2.2 §3.5）+ **viewer/download 适配（核心新增）**：**① allowlist 配置**（sidecar `OC_SLIMAPI_DIRECTORY_ALLOWLIST`，**fail-closed**：空 → /slimapi/file/** 全 403 directory_not_allowed + 启动 warning；运维文档注明「未配置 allowlist 时 /file 请求天然 403，属安全默认」）→ **②（无需 serve 变更）** 现成 `/slimapi` mount 已覆盖新 URL 形态（DEPLOYMENT.md 同步 URL 形态说明）→ **③ renderer 放行**：`FORBIDDEN_PATH_PREFIXES` 移除 `/file` 改为放行 **`/slimapi/file/content` 前缀**（query 形态；含 dot-segment 归一化防 `../` 绕过，renderer.ts:184-200 现逻辑保留扩展）；`fileUrl(directory, path)` 辅助统一产出 query 形态（**directory 与 path 均 URL 编码**）→ **④ viewer 适配（评审 R3 Blocker + R4 分类修正）**：`FileViewer.vue` **fetch** `/slimapi/file/content?directory=<enc>&path=<enc>` → 解析 `LegacyContent` → **按真实 wire 值四种行为分类（评审 R4：`type` 仅 `"text"|"binary"`，不存在 `type:"image"`）**：**1)** `type==="text"` → 纯文本节点展示 `content`（非 JSON wrapper）；**2)** `type==="binary" && encoding==="base64" && mimeType ∈ 预览白名单` → base64 解码 + 正确 MIME 转 data URL 预览（**真实图片形态即此类**）；**3)** 其他合法 binary（非白名单 MIME / 非 base64 编码）→ Blob + `a[download]` 下载；**4)** 未知 type / 未知 encoding / 缺失或非法 mimeType → **fail-safe 不预览**（降级提示，不白屏）。**MIME 预览白名单显式枚举：PNG/JPEG/WebP/GIF**；**不默认预览 SVG/HTML**（不信任任意上游 mimeType——FileViewer 是独立展示路径，**不自动继承 markdown L1/L2/L4 防线**，白名单外一律下载或降级）。**Blob URL 用后 revoke**（内存卫生）。**「过大」分流（评审 R4 Major）**：a) **渲染阈值超限但 fetch 已成功、客户端持有 content** → 可构造 Blob 下载（预览入口降级为下载按钮）；b) **sidecar `max_response_bytes` 超限** → `/file/content` 返回 **413**（INTERFACE_MAP.md:39-40：file/content 继承 body 超限→413），**客户端无内容、无法构造下载** → 显示错误提示 + 指引（联系运维调大上限/走直连），**非下载路径**。**降级（不白屏）**：403 →「目录不可用」；413 → 错误提示+指引；其余解析失败 → fail-safe 降级提示。渲染安全：viewer 输出经 DOMPurify 或纯文本节点（防注入）；markdown 渲染管线四道防线（L1 html:false / L2 禁 image / L4 DOMPurify）对 /file 仍保持（D9 内联渲染否决不变，viewer 为独立展示路径非 markdown 内联）。 |
| 验收标准 | **① `type==="text"` 展示解包后 content 而非 JSON wrapper**（独立断言）；**② 真实图片形态 `type:"binary" + encoding:"base64" + mimeType:"image/png"` 解码预览正确**（data URL 渲染可见）；**③ binary 非白名单 MIME（SVG/HTML 等）→ 下载而非预览**；**④ 未知 type / 未知 encoding / 缺失或非法 mimeType → fail-safe 不预览**（降级提示，不白屏）。**「过大」分流**：渲染阈值超限（已持有 content）→ 下载可用；**413（max_response_bytes 超限）→ 错误提示 + 指引，非下载**。**端到端 200 保留为可达性前置**（allowlist 配置后请求实际 200，对准真实 query 路由），**渲染正确性为独立断言**（200 只是入口，展示对才算过）。未配置（空）→ 403 降级提示；`../` 绕过、scheme 注入、dot-segment 逃逸仍被拒（回归）；收藏不可访问目录 → 「不可用/未知」（与 B5b-1 联动）；部署文档更新（oc-slimapi 侧）。 |
| 测试/验证 | `FileViewer.test`（**fixture 全部用真实 wire 形态，删除 `type:"image"` 构造——评审 R4**）：`type:"text"` 纯文本展示；`{type:"binary", content: <base64>, encoding:"base64", mimeType:"image/png"}` → 解码预览（data URL 断言）；`{type:"binary", ..., mimeType:"image/svg+xml"}`（白名单外）→ 下载而非预览；binary + 未知 encoding → 下载或降级；未知 type / 缺失 mimeType → fail-safe 不预览；**Blob URL revoke 断言**；**413 响应 → 错误提示+指引（断言无下载动作）** 与 **渲染阈值超限（已持有 content）→ 下载可用** 分用例；403 body 解析；`renderer.test`：`/slimapi/file/content?` 合法前缀允许、`path=../etc` 拒绝（query 值内 dot-segment）、非 http(s) scheme 拒绝（现有用例扩展）；`endpoints.test`：fileUrl query 构造（编码断言）；**端到端手测**：allowlist 目录内（200 + 正确渲染）/外（403 + 降级）各一次；收藏不可访问目录「不可用」呈现。 |
| 工作量 | M（~1.5 人日，viewer 适配新增 +0.5） |

---

## 3. 写域矩阵（omni-orch 并行开发用）

> 按「文件归属」切分：同一文件多任务 = 串行（或同一 lane）；不同文件可并行 lane。L = lane（独立工作流，可并行人手）。**micro-PR（§1.4）先行合入 protocol.ts/endpoints.ts 基线后**，各 lane 只追加增量，冲突面收敛。

| 任务 | 主要文件 | 次要文件 | 并行性 | 说明 |
|---|---|---|---|---|
| **micro-PR（先行）** | `api/protocol.ts`、`api/endpoints.ts` | — | **先序合入** | 类型/URL 收敛（V4_CAPABILITY_KEYS、v4 query 构造、changed 字段、SseFrame 引用）——消除 L1/L4/L6 三向 merge（oracle n5） |
| B5a-1 capabilities 探测 | `api/health.ts`、`api/endpoints.ts`(detectApiMode) | `api/protocol.ts`(常量) | **L1 独立** | 只读现逻辑 + 新增导出，无冲突 |
| B5a-2 q/p 直投 | `composables/usePendingCards.ts` | `api/protocol.ts`(QpControlFrame)、`views/HomeView.vue`(仅确认) | **L2 独立** | 与 L1 无文件冲突 |
| B5a-3 折叠两模式验证 | `chat/blocks.ts`、`components/MessageView.vue`（验证+回归） | `components/ExpandCard.vue`(复用验证)、`api/protocol.ts`(truncated 类型清理) | **L3 独立** | 验证任务（0/少量增量）；与 L1/L2 无冲突 |
| B5a-4 status 全局化 | `api/endpoints.ts`(getSessionStatus)、`composables/useSessionList.ts` | `composables/useRecentSessions.ts` | **并入 L2 lane** | 小改动；useRecentSessions 附近域与 L2 共享 |
| B5a-5 digest changed 定向发现 | `composables/useSessionList.ts`(consumeDigest)、`api/protocol.ts`(changed) | `composables/useRecentSessions.ts` | **L4 前导，独立 lane**（依赖 3.3.0 发布） | 与 L1/L2/L3 无冲突；useRecentSessions 域与 B5b-1 交接点需任务卡片注明 |
| B5b-1 全局分组 + 状态机 | `composables/useRecentSessions.ts`(重写)、`composables/useSessionList.ts`(v4 适配)、`api/endpoints.ts`(v4 getSessions) | `components/Sidebar.vue` | **L5 大 lane** | **依赖 B5a-5 先合入**（新 sid 入列语义是分组准确前提）；与 B5a 各 lane 无冲突 |
| B5b-2 SSE 重放 | `sse/connect.ts`(验证)、`sse/parse.ts`(验证)、`composables/useSessionList.ts`(gap 回退) | `views/HomeView.vue` | **L6 独立**（sse/ 域） | 与 L5 冲突点：useSessionList 的**恢复面（onResync/scheduleRecovery——oracle 修正，非「resyncRecovery」函数；resyncRecovery:596-668 属 useMessages.ts）**——函数级拆分（重放分支 vs 全量 resync 分支）在任务卡片注明，建议 L6 于 L5 中后期启动 |
| B5b-3 /file | `markdown/renderer.ts`(webui) | `api/endpoints.ts`(fileUrl) | **L7 独立** | renderer 域独享；fileUrl 走 micro-PR 基线追加；**oc-slimapi `docs/operations.md` 等部署文档 = 只读依赖（S 方案 B4 独占写域，本方案不写——终检 C4）** |

**串行依赖链**：`micro-PR → B5a-5 → B5b-1`（useSessionList/useRecentSessions 域，新 sid 入列 → 分组）；`B5b-2` 与 `B5b-1` 在 useSessionList 恢复路径有交集（**全量 resync 恢复（onResync/scheduleRecovery）vs 重放分支；resyncRecovery:596-668 属 useMessages.ts**）——**L6 于 L5 中后期启动或同 lane 顺序执行**。`L7（/file）` 完全独立可并行。

**omni-orch 建议**：micro-PR 先行（1 人日内）；B5a 四 lane（L1/L2/L3/B5a-5）+ L7 并行（3.3.0 发版前 L1/L2/L3 按 B1b 核对结论推进，B5a-5 于 3.3.0 发布后启动）；L5 于 B5a-5 合入后启动；L6 于 L5 中后期并行（函数级拆分约定注明）。

---

## 4. 与 oc-slimapi 的接口冻结点

> 每条 = webui 依赖的 wire 契约条目 + 版本时序。标注「B1b 核对产物」的条目在核对完成前**不得**作为定稿依赖。

### 4.1 冻结条目

| # | 契约条目 | 来源 | 版本 | webui 消费方 | 状态 |
|---|---|---|---|---|---|
| F1 | digest `changed:[sid…]` 可忽略字段 | v2.2 §3.2（B1a） | **3.3.0** | useSessionList/useRecentSessions（B5a-5 定向发现） | 冻结（加性；v3 契约 §7 修订） |
| F2 | q/p properties 完整性 + **两套字段表** | v2.2 §3.2a（B1b 核对） | 3.3.0 核对 / v4 补全 | usePendingCards（B5a-2） | **⚠ B1b 核对产物——完整直投字段集 / 最小可渲染字段集，见开放问题 Q1** |
| F3 | 折叠内容两模式裁剪（正文全量内联） | v2.2 §3.3（owner 裁决 [3.2.0] 契约决策） | **3.1.0 已发** | blocks/MessageView/ExpandCard（B5a-3 验证） | **冻结（评审 R3 Major1 改写）**：**正文 TextPart.text 永不截断、永远全量内联**（chat 核心内容不折叠、无阈值）；裁剪对象 = 折叠内容（tool state/reasoning/diff/patch/attachments 等），仅两模式——**模式 1** = 默认完全不加载（omitted + expandRefs，展开时经 expand 端点拉取）、**模式 2** = 未展开状态提供缩略信息（diffStats / 预览摘要类）。**「merged 400 码点截断」废止**（B2 按两模式重设计）。原「`truncated:true`⇒text!=null / `omitted`⇒text=null 互斥定义」**作废**——truncated 不再是 TextPart.text 的合法 wire 状态；若保留 truncated 引用，仅限折叠内容预览摘要语境（模式 2） |
| F4 | capabilities["4"] 能力键：globalSessions/sseReplay/qpImmediateFull/auxiliaryFilters（静态） | v2.2 §7 | 4.0.0 起 available 含 4 | endpoints/health（B5a-1 版本协商 + 逐键检查；B5b 切 v4 判定） | 冻结（静态能力键，不随瞬态抖动；**缺键 = 消费方该功能 fail-closed 禁用 + UI 提示，不发 v3 请求——[4] 服务端无 v3 通道；available 含 3 时整体降回 3（全端点一致），非 per-feature 混用——评审 R3 Major2**；capability 逐键开关仅 `selectedWireVersion==4` 生效——v3 连接下 capabilities["4"] 不触发任何行为，评审 B1） |
| F5 | v4 sessions：`parent=none`（承接 roots=true）、archived 三态、cursor keyset、limit 1..500、`directory_retired_in_v4`、降级矩阵（200+degraded / 503 auxiliary_unavailable / 400 invalid_cursor）、响应 `{items,nextCursor,complete,degraded?}`（**degraded?: true 入 shape，评审 oracle 修正——degraded 下 complete 为 best-effort，精确判据须 `complete===true && degraded!==true`**） | v2.2 §3.1（B3a） | **4.0.0** | endpoints/useRecentSessions/useSessionList（B5b-1） | 冻结（DB 投影源为 schema 权威 + HTTP 降级路径；**400 invalid_cursor 客户端须丢 cursor 重拉+退避**） |
| F6 | /file allowlist 403 语义：`/slimapi/file/**` fail-closed、空 allowlist → 403 directory_not_allowed、403 不泄露目录存在性、health 广播 allowlist 非空状态 | v2.2 §3.5（B4） | **3.3.0** | renderer/endpoints（B5b-3） | 冻结；**URL 形态（评审 B1，实读修正）= query 形态 `GET /slimapi/file/content?directory=<enc>&path=<enc>`**（read_groups.py:86-110 `path: str` 为 FastAPI query 参数；另有 `/slimapi/file`、`/slimapi/file/status`；v3-contract.md:198 确认）。**path-segment 形态 `/slimapi/file/{directory}/{path}` 不存在，会 404 thin_route_not_found（catch-all 已关）——禁止使用** |
| F7 | SSE `id:`/Last-Event-ID 重放 | v2.2 §3.2（B3b） | **严格 4.0.0** | sse/connect + useSessionList（B5b-2） | **冻结（v4-only）**：v3 流绝不输出 id:、Last-Event-ID 在 v3 无服务端语义（忽略/不解释，reconnect_no_replay 全量 resync 不变）；v4 才启用 id:/Last-Event-ID 寻址/有界回放。**禁止提前开放**（v3 契约 §7 冻结）。**版本跟随（评审 B1）**：选 v4 后 events 与 token stream 均须 `?v=4`（无独立版本选择，杜绝「HTTP 走 v4 而 events 仍 ?v=3」） |
| F8 | status 全局化（上游本就全局，消费端单次全局调用） | v2.2 §1.2 + 决策2 | **v3 即刻**（sidecar 零变更） | useRecentSessions/useSessionList（B5a-4） | 冻结（实证：sessions.py:350-368 转发上游全局 map） |
| F9 | q/p 聚合路由保留 + resync 对账 + 30min sweep（60s 定时废除） | v2.2 §3.2a | 3.3.0（B1b） | usePendingCards 冷启动/resync/降级 L3（B5a-2 保留路径） | 冻结（客户端行为依赖，非 wire 形变） |
| F10 | per-sid 精拉：`GET /slimapi/session/{sid}`（directory 可选 query，skeleton_session 投影） | 既有 v3 收编路由（read_groups.py:190-205，selector.py:150） | **v3 已可用** | useSessionList（B5a-5 定向发现） | 冻结（已存在，非新增；B5a-5 消费其 v3 形态） |

### 4.2 B1b 核对对象（sidecar 待产出，B5a-2 前置）

核对物 = `src/oc_slimapi/sse/global_hub.py:513-529` publish() IMMEDIATE 分支的转发帧 `{directory, type, properties: props}`——**帧顶层 `directory` 已透传（global_hub.py:517 `directory = global_event.get("directory")`，无需推断）**；`props = payload.get("properties")` 原样透传（:522）。B1b 逐字段比对上游 `question.asked` / `question.v2.asked` / `permission.asked` / `permission.v2.asked` payload，**产出两套字段表（评审 B2）**：①**完整直投字段集**（webui QuestionEntry/PermissionEntry 全字段）②**最小可渲染字段集**（id/canonicalID + 渲染所需最小集；低于最小集不建半残卡片，走 L3 刷新）。

**结论分叉**：properties ≥ 最小集且关键字段存在 → B5a-2 主线直投（零 wire 变更）；低于最小集 → B5a-2 降级（不插卡 + authoritative 刷新）；完整直投需补字段 → 移 v4 `qpImmediateFull`。**omni-orch 必须在 B5a-2 lane 启动前锁该结论**。

### 4.3 版本时序矩阵

> **版本协商总则（评审 B1，三概念模型）**：`selectedWireVersion = highest(clientSupported ∩ serverAvailable)`；全部端点跟随；capability 逐键仅 `selectedWireVersion==4` 生效。**B5a 客户端 clientSupported={3}**：在 [3]/[3,4] 均选 3（并行期继续 v3）；遇 v4-only [4] → legacy fail-closed（不静默降级）。**B5b 后客户端 clientSupported={3,4}**：任意 available 组合（[3]/[3,4]/[4]）均**成功选版本**（[3]→3、[3,4]→4、[4]→4），无失败路径。

| 版本 | wire | sidecar 交付 | webui 动作 | 时序 |
|---|---|---|---|---|
| 3.1.0 | v3-only | 折叠内容两模式裁剪（F3；正文全量内联，400 码点截断方案废止于 [3.2.0] owner 裁决） | B5a-3 验证两模式兼容（即刻，纯验证） | **已发** |
| 3.3.0（P1） | v3-only | digest changed（F1）+ B1b 核对结论+两套字段表（F2）+ /file allowlist（F6）+ B4 路由 | **B5a 发布窗口**：B5a-1（版本协商：clientSupported={3}，[3,4] 下仍选 3）+ B5a-2/3/4 + **B5a-5（changed 定向发现，3.3.0 发布后实测）**；micro-PR 先行 | 对齐 3.3.0 发布 |
| 4.0.0（P3） | (3,4) 双版本 | v4 sessions DB（F5）+ SSE id:/重放（F7，v4-only） | **B5b 全部**：clientSupported 升 {3,4} → [3,4] 自动选 4（全部端点 ?v=4）；B5b-1 全局分组（状态机）、B5b-2 重放消费、B5b-3 /file 完成 | 4.0.0 发布后 |
| 5.0.0（P4） | (4,4) 删 v3 | v4-only | **B5b 后客户端 [4] → 成功选 4**（B5a-1 协商无失败路径）；**仅未升级的 B5a 旧客户端（clientSupported={3}）遇 [4] → fail-closed**（legacy gate，提示升级）；验证全 v4 路径（B5b 已就绪）；R1 重协商回退开关自然失效挂门（available 不再含 3 → 开关不可达，§6） | 按指标退役（v2.2 §7） |

---

## 5. 测试与验收策略

### 5.1 扇出归零证明（audit 痛点①②③④ 归零）

- **方法**：浏览器 DevTools Network 面板（或 Performance entries API）计数请求。脚本化：`performance.getEntriesByType('resource').filter(r => r.name.includes('/slimapi/sessions'))`，事件触发前后计数差。
- **阈值**：
  - 收藏 30 目录场景：现状 60 请求/事件风暴（30 sessions + 30 status，useRecentSessions.ts:134-158）→ B5a-4 后 31 → B5b-1 后 ≤2+⌈页⌉（全局 sessions + 1 status）；
  - asked 帧触发：现状 2 请求（questions+permissions 全量）→ B5a-2 后 **0**（直投；降级 L3 时 ≥1 次 authoritative 刷新为预期）；
  - digest 帧触发：现状 1 次 status 精刷（useSessionList.ts:263）→ B5a-5 后 O(changed **去重 (directory,sid) 对数**) 定向精拉（含成员 upsert/404 remove，非整表）；
  - Sidebar 展开收藏行：现状 N 轻量拉取（Sidebar.vue:285-337）→ B5b-1 后 0（全局分组数据源）。
- **CI 自动化**：vitest 层断言 mock 调用计数（各 composable test），浏览器计数作人工验收。

### 5.2 断线恢复 O(缺口) 验证（v4-only）

- DevTools offline 模拟断线 30s（期间触发 3-5 个事件）→ 恢复 → 断言：重连请求带 `Last-Event-ID` 头（**且 URL 带 `?v=4`——版本跟随断言，评审 B1**）；恢复流量 = 缺口帧数（≈断线期间事件数），**无**全量 sessions/status 重拉（Network 计数 + 请求类型白名单）。
- **去重正确性**：同 epoch 序号回退（乱序）→ 抑制；epoch 改变且 seq 归零 → 不误抑制；global 与 token stream ID 状态互不污染（per-connection 隔离断言）；**v3 模式重连 → 完整 resync**（id: 不出现、Last-Event-ID 无服务端语义、行为与现状一致——冻结语义验收）。
- gap 场景：重放日志过期（重启 sidecar）→ 断言回退全量 resync + snapshot 对账成功、无死循环（connect.ts 退避 1s→30s 现逻辑已保证）。
- 自动化：connect.test 补 Last-Event-ID 断言；useSessionList.test 重放分支 vs resync 分支双路径。

### 5.3 回归基线 + 回滚开关验证

- oc-webui 现有 vitest 套件全绿（渲染管线 renderer.test、blocks.test、composables 各 test）——每任务并入时先跑基线。
- **feature-flag 回滚测试（rev-sgpt n5；终检 C1 统一裁决）**：B5b-1 的 `VITE_LEGACY_FAVORITE_FANOUT=1` 是**整体版本重协商开关而非 per-feature 回退**——切换 = 覆写 `selectedWireVersion=3`（仅 available 含 3 时可达；全端点一致切 v3，含 events/token stream），非「v4 全局路径下混发 v3 directory 扇出」。切回后断言：① directory 参数正确（v3 形态带 directory query 而非 v4 无参）；② 不同时发新全局请求（两路径互斥，无双发）；③ 全部端点（含 SSE）`?v=3`——**无 v3/v4 混跑（对齐 F4/B5a-1 冻结语义）**；④ cache 不误清空（useProjectFavorites localStorage 数据不受切换影响）。
- **allowlist 收藏展示（评审 Major 保守二义）**：本地收藏了不可访问目录 → 收藏区统一显示「不可用/未知」（**不区分**未授权 vs 空目录 vs 未加载——无逐目录授权 wire 数据源）；/directories 缺失或请求失败同文案。与 B5b-1 二义判定共用 UI（精确空组 =「无会话」/ 未加载 =「继续加载」/ 其余 =「不可用/未知」，Q3 记 sidecar 开放问题）。
- 契约回归：`./scripts/check.sh`（oc-slimapi 侧）保证 wire 契约/路由文档一致性在方案实施期不漂移；webui 侧改动**不**触碰 oc-slimapi 仓库任何 wire 代码（本方案文件除外）。

---

## 6. 风险与回退

| # | 风险 | 影响 | 缓解 / 回退 |
|---|---|---|---|
| R1 | **无自愈的陈旧收藏列表**（全局分组拉取失败 → 收藏区数据缺失；现状扇出有 partial/anyOk 语义 M4A-004） | 收藏面板空白 | 保留 partial 语义（v4 全局拉取失败目录组标记 partial + 重试退避）；**回退**：`VITE_LEGACY_FAVORITE_FANOUT=1` = **整体版本重协商开关（终检 C1 裁决）**——仅 available 含 3 时可达，切换 = 覆写 `selectedWireVersion=3`、全端点（含 SSE）一致切 v3 后走 per-directory 扇出路径（§5.3 回滚测试），**非 v4 会话中的 per-feature v3 混发**。**删除量化标准（评审 n4）**：不以「请求计数」作退役判据，改为——**验收全过 + 生产观察 ≥7 天无 partial 告警 + 挂 sidecar 5.0.0 门**（5.0.0=(4,4) v4-only 时 available 不再含 3，重协商不可达，开关自然失效方可删除） |
| R2 | **/file 解禁后安全边界**（allowlist 未配置 → fail-closed 403；配置后校验绕过） | 目录泄露 / 路径逃逸 | 三前置顺序强依赖：**allowlist 先行**（sidecar fail-closed，空=全 403 属安全默认，v2.2 §3.5）；URL 形态定案 (a) query 形态走现成 `/slimapi` mount（无 serve 新增面）；renderer 解禁保持四道防线 + `/slimapi/file/content` query 形态前缀白名单 + dot-segment 归一化（query 值内同样校验）；403 不泄露目录存在性（契约）；**回退**：/file 链接渲染开关（`VITE_ENABLE_FILE_LINKS` 默认关，安全缺口时一键回禁） |
| R3 | **SSE 重放 gap**（环形日志有界，长时间断线 → 回放不完整） | 恢复不完整 | 协议已定义 gap → resync 回退（v2.2 §3.2）；客户端必须实现并测试该路径（B5b-2 验收含 gap 场景）；**回退**：Last-Event-ID 不发送（connect.ts 现行为）→ 回到全量恢复（现状，功能不损）；v3 模式本就走全量（冻结语义，无额外回退面） |
| R4 | **B1b 核对结论延迟/不完整**（properties 低于最小集 → B5a-2 无法零 wire 直投） | B5a-2 卡点 | 结论分叉已预案（§4.2：不插卡 + authoritative 刷新；v4 `qpImmediateFull` 补全）；omni-orch 锁结论时序在 B5a-2 lane 前 |
| R5 | **翻页完整性误判**（complete:false 时误判空组 / Top-N 提前停 / **degraded 下把 best-effort complete 当精确判据，评审 B4**） | 收藏列表缺失项 | **状态机冻结**（§2.2 B5b-1 ③）：**精确 count/空组判定仅 `complete===true && degraded!==true` 后成立**；不足 N 组续读至精确终点；degraded 响应禁精确空组/count（UI「数据可能不完整」）；页数硬上限 5 页 → partial + 加载更多；验收含全部状态机用例（含首屏直接 degraded） |
| R6 | **版本协商误判**（能力键抖动 / v4-only sidecar 误失败 / 缺个别键整体失败 / 端点版本不一致） | 启动门闩 / 路径错乱 | **selectedWireVersion 三概念模型（§2.1 B5a-1）**：能力键静态不随瞬态抖动（v2.2 §7）；**所有端点（含 events/token stream）跟随 selectedWireVersion**（选 v4 后 events 也 ?v=4——消除 HTTP/SSE 版本分叉）；capability 逐键仅 `selectedWireVersion==4` 生效（**缺键 = 该功能 fail-closed 禁用 + UI 提示，不发 v3 请求——[4] 无 v3 通道；available 含 3 时整体降回 3（全端点一致切换），非 per-feature 混用——评审 R3 Major2**）；**B5b 后客户端 [4]→成功选 4**（无失败路径）；仅 B5a 旧客户端 {3} 遇 [4] → legacy fail-closed（提示升级，不静默降级）；**回退**：v2/v3 模式开关（开放问题 Q2 注记：B5b 后无需，legacy gate 已覆盖） |
| R7 | **digest changed 定向精拉与旧 sidecar 兼容**（3.3.0 前无 changed 字段） | 精拉不触发 → 新会话延迟出现 | changed 字段可忽略性（契约 F1）：缺失 → 现状行为兜底（新 sid 等 resync，功能不损）；版本门：仅当已确认 changed 支持才走精拉 |
| R8 | **写域冲突**（L5/L6 共用 useSessionList 恢复路径） | 并行合入冲突 | 写域矩阵 §3 已列串行链 + micro-PR 先行收敛；任务卡片注明函数级拆分（**全量 resync 恢复 onResync/scheduleRecovery vs 重放分支；resyncRecovery:596-668 属 useMessages.ts，非冲突面**），omni-orch 调度遵守 |
| R9 | **400 invalid_cursor 循环**（keyset 指纹失配反复触发） | 翻页风暴 | 客户端丢 cursor 重拉首页 + 指数退避（B5b-1 ③ cursor 分支；评审补）；上限后降级无 cursor 全量拉取（limit 500） |

---

## 7. 工作量估计

| 任务 | 规模 | 估计 |
|---|---|---|
| micro-PR（protocol.ts/endpoints.ts 收敛） | S | ~0.5 人日 |
| B5a-1 版本协商（selectedWireVersion + 逐键检查） | S | ~0.5 人日 |
| B5a-2 q/p 帧载荷直投（L1/L2/L3 降级 + 最小集门槛） | M | ~2 人日 |
| B5a-3 折叠两模式兼容验证 + M6 回归 | S | ~0.5 人日（由 1 人日降——实现任务废止，仅验证+回归，评审 R3 Major1） |
| B5a-4 status 单次全局调用 | S | ~0.5 人日 |
| B5a-5 digest changed 定向发现（自 B5b 移入；**全量 (directory,sid) 对 upsert/remove，评审 B3——per-sid 分支扩展 + 测试增项，较上轮 +0.5 人日**） | M | ~2 人日 |
| **B5a 合计** | | **~6.0 人日**（含 micro-PR 与测试；B5a-3 降 0.5 后从 6.5 下调） |
| B5b-1 全局列表 + consumer 状态机（核心；**含 degraded 精确判据与首屏 degraded 用例，评审 B4**） | L | ~4 人日 |
| B5b-2 SSE id: 重放消费（v4-only 冻结 + 去重键 + 版本跟随） | M | ~1.5 人日 |
| B5b-3 /file 通路（query 形态 + FileViewer 适配） | M | ~1.5 人日（viewer 适配新增 +0.5，评审 R3 Blocker） |
| **B5b 合计** | | **~7.0 人日**（B5b-3 增 0.5 后从 6.5 上调） |
| **总计** | | **~13 人日**（含测试与验收；净持平：12.5 基准 + 0.5 B5a-5 增 − 0.5 B5a-3 降 + 0.5 B5b-3 viewer 增） |

规模约定：S ≤ 0.5d；M ≤ 2d；L ≤ 4d。总计含测试/验收/回归，不含 omni-orch 调度开销。

---

## 开放问题（不擅自决定，供 omni-orch / 契约修订裁决）

- **Q1（B5a-2 前置阻塞）**：B1b 核对结论未出——q/p properties 是否 ≥ 最小可渲染字段集？两套字段表（完整直投 / 最小可渲染）由 sidecar lane 产出后锁定（§4.2 分叉已预案）。
- **Q2（oracle n6 联动注记）**：**搜索过滤语义裁决先于 B5b-1 冻结**——v4 全局列表 + 客户端分组模式下，搜索框的过滤是服务端 search 查询（契约支持，F5）还是客户端过滤？**与 B5a-5 联动**：搜索过滤中的「列表新 sid 入列判定」——changed 定向发现的目标是「当前列表」的新 sid，搜索过滤下不在当前搜索结果的 sid 是否入列？（B5a-5 实现要点②的成员判定语义受此裁决影响；B5a-5 自身可先按「全量列表视角」实现，搜索语义裁决后再收敛）。另：v4-only sidecar（5.0.0）下用户手动强制 v3 模式的开关是否必要？（评审 B1 版本协商后：B5b 后客户端 [4]→成功选 4 无失败路径，开关仅对**未升级的 B5a 旧客户端**有意义——legacy gate fail-closed 已覆盖，默认不建开关）
- **Q3**：**逐目录授权信号（评审 Major 转开放问题）**——sidecar 现仅广播 allowlist 非空布尔（health），**无逐目录 authorization map 的 wire 数据源**；webui 侧已收敛为保守二义「不可用/未知」（§2.2 B5b-1 三分类：无会话/继续加载/不可用未知）。是否在 v4 wire 增加逐目录授权信号（如 health 扩展或 /directories 增字段）？**本计划不实现**，供契约修订裁决。
- **Q4**：ETag/304 客户端缓存是否另行立项？（已转 §1.2 非目标，audit §2.6 观察记录；omni-orch 可另行裁决）
- **Q5**：v4 sessions 的 degraded 呈现强度（决策9：过滤语义永不降级）——webui 收藏区「数据源降级」横幅的出现频率与措辞需产品确认（防噪音；配合「数据可能不完整」判据，评审 B4）。

---

*本文档为 oc-webui 侧改造唯一入口；wire 冲突以 `docs/specs/v3-contract.md`（及后续 v4-contract）为准，实现冲突以本方案 + v2.2 为准。*