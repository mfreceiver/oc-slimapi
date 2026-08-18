# 设计方案：q/p 载荷核对（B0-4）

> **v4 设计稿（B0-4）**：逐字段核对 sidecar 转发的 q/p 帧 `properties` 与上游 `question.asked` / `permission.asked` 完整 payload。**本文件为 B0-4 核对报告 + 结论**，是 webui B5a-2 / ocdroid T-A4 的前置锁、`qpImmediateFull` 能力键语义冻结依据。
>
> 状态：**核对完成 — 结论：已完整**
> 性质：**只读核对报告**（核对对象为现行代码；改动文件仅本设计文档）
> 关联：上游锚点 `opencode-src/current`（**v1.18.18**）`packages/schema/src/question.ts` + `permission.ts`、`packages/core/src/question.ts` + `permission.ts`、`packages/opencode/src/event-v2-bridge.ts`；sidecar 锚点 `src/oc_slimapi/sse/global_hub.py`、`src/oc_slimapi/sse/hub_types.py`；权威基准 v2.2 行 155/164

## 修订记录

| 版本 | 内容 |
|---|---|
| v1（本稿） | B0-4 逐字段核对报告；结论：**properties 已完整** → B1b 零 wire 变更；`qpImmediateFull` 语义冻结 |

---

## 1. 核对路径（sidecar 如何收到 q/p 帧）

```
opencode EventV2 publish (question.asked / question.v2.asked / permission.asked / permission.v2.asked)
  └─▶ event-v2-bridge.ts:35-44  → GlobalBus.emit("event", payload: {id, type, properties: event.data})
  └─▶ GlobalHub.publish()  global_hub.py:513-535
         props = payload["properties"]          # global_hub.py:522 原样透传，零裁剪
         frame = {directory, type, properties}  # global_hub.py:526-530  ≤ IMMEDIATE 直推
  └─▶ SSE 帧: data: {directory, type, properties}   # 措辞校正（P1, B3b-5）：IMMEDIATE 直推帧**无 `event:` 头**（sse_frame 默认 event=None，raw 直推风格，客户端按 `data.type` 分发）——与行为锁一致
```

**关键事实（源码实证）**：

1. **上游侧（event-v2-bridge.ts:39-44）**：GlobalBus 帧 `payload = {id: event.id, type: event.type, properties: event.data}`——**`properties` 即 EventV2 的 `event.data` 完整业务对象**（envelope 的 `id`/`metadata`/`durable`/`location` 在 payload 顶层，不进 properties）。
2. **sidecar 侧（global_hub.py:513-535）**：`props = payload.get("properties")` **原样取用，无任何字段级裁剪/精简**；q/p 属 `IMMEDIATE` 集合（hub_types.py:71-75）→ 直推帧 `properties` = 上游 data 原样。
3. **发布点（core 层）**：`question.ts:93-110` ask() 发布 `Request = {id, ...input}` 完整对象；`permission.ts:164-174` request() 构造完整 `Request` 后 `create()` 发布——**发布的就是完整 Request，无子集**。

⟹ **转发路径 properties 字段完整性 = 上游 event.data 完整性**，核对等价于比对「上游 payload schema 全字段」vs「转发 properties 集合」。

---

## 2. 表 1：上游完整 payload 字段集（权威来源）

### 2.1 `question.asked` / `question.v2.asked`（事件 data = `QuestionV2.Request`）

> 现实现发 `question.v2.asked`（schema/question.ts:70）；`question.asked`（legacy）语义等价。data = `Request.fields`（schema/question.ts:52-58）。

| 字段 | 类型 | 必选 | 来源行号 | 说明 |
|---|---|---|---|---|
| `id` | String（`que_` 前缀） | ✓ | schema/question.ts:10, 53 | 请求 ID，`ID.ascending()` 生成 |
| `sessionID` | SessionID | ✓ | schema/question.ts:54 | 所属会话 |
| `questions` | Array(Info) | ✓ | schema/question.ts:55 | 问题卡片数组 |
| `questions[].question` | String | ✓ | schema/question.ts:29（base） | 完整问题文本 |
| `questions[].header` | String | ✓ | schema/question.ts:30 | 短标签（≤30 字符） |
| `questions[].options` | Array(Option) | ✓ | schema/question.ts:31 | 选项列表 |
| `questions[].options[].label` | String | ✓ | schema/question.ts:23 | 选项显示文本 |
| `questions[].options[].description` | String | ✓ | schema/question.ts:24 | 选项说明 |
| `questions[].multiple` | Boolean | ✗ | schema/question.ts:32 | 是否多选 |
| `questions[].custom` | Boolean | ✗ | schema/question.ts:36-39（Info） | 允许自定义答案 |
| `tool` | Tool | ✗ | schema/question.ts:56 | 触发工具元数据 |
| `tool.messageID` | String | ✓（tool 存在时） | schema/question.ts:47 | 工具调用消息 |
| `tool.callID` | String | ✓（tool 存在时） | schema/question.ts:48 | 工具调用 ID |

发布实证：`core/question.ts:93-110` `ask()` → `request = {id, ...input}`（input 含 sessionID/questions/tool）→ `events.publish(Event.Asked, request)` 发布**完整 Request**。

### 2.2 `permission.asked` / `permission.v2.asked`（事件 data = `PermissionV2.Request`）

> 现实现发 `permission.v2.asked`（schema/permission.ts:43）；`permission.asked`（legacy）同 Load 语义。data = `Request.fields`（schema/permission.ts:34-38）。

| 字段 | 类型 | 必选 | 来源行号 | 说明 |
|---|---|---|---|---|
| `id` | String（`per_` 前缀） | ✓ | schema/permission.ts:10, 35 | 请求 ID |
| `sessionID` | SessionID | ✓ | schema/permission.ts:26 | 所属会话 |
| `action` | String | ✓ | schema/permission.ts:27 | 权限动作（如 file.write / shell.*） |
| `resources` | Array(String) | ✓ | schema/permission.ts:28 | 资源路径列表 |
| `save` | Array(String) | ✗ | schema/permission.ts:29 | 持久化规则 |
| `metadata` | Record(String, Unknown) | ✗ | schema/permission.ts:30 | 附加元数据 |
| `source` | Source | ✗ | schema/permission.ts:31 | 触发来源 |
| `source.type` | Literal("tool") | ✓（source 存在时） | schema/permission.ts:17 | 当前仅 tool |
| `source.messageID` | String | ✓（source 存在时） | schema/permission.ts:18 | 工具调用消息 |
| `source.callID` | String | ✓（source 存在时） | schema/permission.ts:19 | 工具调用 ID |

发布实证：`core/permission.ts:164-174, 183-184` `request()` 构造 `{id, sessionID, action, resources, save, metadata, source}` 完整对象 → `create()` 发布 `Event.Asked`。

---

## 3. 表 2：sidecar 当前转发字段集 + 比对结论

### 3.1 转发路径逐字段（sidecar 锚点）

| 帧字段 | 值来源 | 行号锚点 | 说明 |
|---|---|---|---|
| `directory` | GlobalBus 帧顶层 `directory`（bridge 侧取 `event.location?.directory`） | global_hub.py:517, 527 | 事件目录（envelope 层，非 properties） |
| `type` | `payload.type` | global_hub.py:521, 528 | = 事件类型字符串（`question.v2.asked` 等） |
| `properties` | `payload.properties` **原样**（= event.data） | global_hub.py:522, 529 | **完整业务对象，零裁剪** |

IMMEDIATE 集合实证：`hub_types.py:71-75` = `{question.asked, question.v2.asked, permission.asked, permission.resolved, permission.v2.asked, permission.v2.resolved}`——四类 asked 事件全部走「原样直推」路径，无 debounce、无字段处理。

### 3.2 比对结论

| 维度 | 结论 |
|---|---|
| `question.asked`/`question.v2.asked` properties 完整性 | **✓ 完整直投**：`id/sessionID/questions(question/header/options(label,description)/multiple/custom)/tool(messageID/callID)` 全 10 字段与上游 Request schema 逐一对应，无缺失、无改名、无裁剪 |
| `permission.asked`/`permission.v2.asked` properties 完整性 | **✓ 完整直投**：`id/sessionID/action/resources/save/metadata/source(type/messageID/callID)` 全 10 字段逐一对应，无缺失 |
| 语义等价 | ✓ `properties` = 上游 event.data 原样（bridge 构造 + hub 透传双环节均无变换）；发布端发完整 Request（core 层实证） |

**不进入 properties 的声明（明确边界）**：EventV2 **envelope** 字段（`id`=evt_ 前缀事件 id、`metadata`、`durable`、`location`）位于 GlobalBus payload 顶层而非 data 内，v3 帧契约本就不转发（帧形冻结于 v2.2 行 153 / v3-contract §7）——**不属于 properties 缺失**；客户端如需事件级溯源可从帧 `type` + `properties.id`（业务侧 que_/per_ id）自建关联，不阻塞本结论。

### 3.3 最小可渲染字段集（webui / ocdroid 渲染 q/p 卡片）

> 由「上游 schema 必选字段 + 客户端渲染需求」推导；因核对结论=已完整，**渲染所需字段全部已在转发帧内**，客户端无需回查。

| 渲染元素 | 所需字段 | 来源 |
|---|---|---|
| 卡片标题/正文 | `questions[].header` / `questions[].question`（或 permission `action` + `resources`） | data 内 ✓ |
| 选项列表 | `questions[].options[].label` + `description`；`multiple` 决定单选/多选 | data 内 ✓ |
| 自定义输入 | `questions[].custom` | data 内 ✓ |
| 会话归属 | `sessionID` | data 内 ✓ |
| 回复回填 | `id`（que_/per_ 请求 ID，回填 `permission.reply` / `session.question.reply` 用） | data 内 ✓ |
| 工具来源上下文 | `tool.messageID`/`tool.callID`（question）`source.messageID`/`source.callID`（permission） | data 内 ✓ |
| `messageId`/`partID`（消息域锚点） | 不在 q/p 帧内——属于消息域；经 `sessionID` 关联，由客户端按需查询（非渲染必需，不阻塞直投） | — |

---

## 4. 核对结论（交付锁）

**结论：已完整。**

- **B1b 零 wire 变更**：q/p 帧 `properties` 已是上游完整业务对象，webui/ocdroid 可直投渲染（B5a-2 / T-A4 纯客户端改动）；无需 B3b-3 的帧补全路径（refactor-plan §4.1 中 q/p 帧补全为条件性任务，条件=「B0-4 核对缺字段」，本结论该条件不成立）。
- **qpImmediateFull 能力键语义冻结**：`qpImmediateFull: true` ⇒ 客户端可依赖 q/p 帧独立渲染完整卡片（`properties` = 上游完整 payload），无需回查 `/question`、`/permission` 端点；语义**现状已成立**（B0-4 核对证实）。能力键随实现同批广告（B3b，refactor-plan §4.1 时序）；B3a `capabilities["4"] = {globalSessions, auxiliaryFilters}` 不含此键。

### 4.1 自洽性校验（行 140 静态键约束）

`qpImmediateFull` 广告与语义终态同批落地（B3b）：语义=现状已成立（本报告冻结）→ 键发布时语义已终态、不随 DB 抖动 → 静态性满足（v2.2 行 140）。

---

## 5. 观察项 / 待裁决

| 编号 | 事项 | 状态 |
|---|---|---|
| 1 | envelope 字段（evt_ id / metadata / durable）不进 properties——若未来客户端需要事件级溯源，v4-contract 需显式声明「不转发 envelope」或另设计；当前建议维持现状（帧形冻结约束下零变更） | 观察项，不阻塞 |
| 2 | `permission.resolved` / `question` 的答复类事件（`permission.replied` 等）不在本核对范围（B0-4 仅 asked 方向）——如后续需 `permission.resolved` 字段核对，另立核对项 | 范围说明 |
| 3 | AGENTS.md 记 `opencode-src/current → v1.18.16`，实际 readlink 为 **v1.18.18**（与 v2.2 一致）——文档滞后 | **已于 B0 批修订**（AGENTS.md 当前对齐版本更新为 v1.18.18） |