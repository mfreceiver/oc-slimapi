# oc-slimapi 候选省流接口评估与方案

> **日期**：2026-08-05
> **性质**：评估报告 + 独立评审结论 + 实施方案。**非契约**——wire 行为权威仍为 [`docs/specs/v2-contract.md`](../../specs/v2-contract.md)；本文件落选方案不生效，入选方案在实现时再走契约/CHANGELOG 流程。
> **证据链**：理论调查 3 lane（上游行为 / ocdroid 消费 / sidecar 约束）+ live 实测（opencode v1.18.13 @ :4096）+ 独立评审（rev-ogpt）。
> **实测产物**：`/tmp/opencode-probe/`（采样 JSON + `analyze.py`，可复跑）。

---

## 1. 摘要

当前 sidecar 已覆盖大 payload 读路径（slimapi 占 10% 请求、**87% 上游字节**）。剩余 passthrough 流量中，省流**字节**价值集中在两个 catalog 端点；省流**请求数/简化调用**价值集中在 status 批量化与 SSE/消息迁移核验。

**结论性数字（5 天真实流量 + live 实测）**：

| 端点 | live raw | skeleton 后(raw/gzip) | 实测省流比 | 处置 |
|---|---|---|---|---|
| `GET /command` | **292 KB** | 7.25 KB / 3.18 KB | **97.6% / 97.0%** | ✅ 采纳 S1 |
| `GET /agent` | **250 KB** | 10.7 KB / 3.57 KB | **95.8% / 89.4%** | ✅ 采纳 S2 |
| `GET /session/{sid}` | 556 B（626 次/5天） | 350 B / 275 B | 37.7% / 28.0% | ⏸ 延后（流量极小） |
| `GET /question` / `GET /permission` | 2 B（空） | — | 无收益 | ❌ 否决 |
| `GET /session/{sid}/message` | ~72 KB avg | 已有 `/slimapi/messages/{sid}` 等价 | — | 🔁 迁移核验 S5 |

**最高优先级不是新端点，而是 S5（SSE/消息迁移运行时核验）**——这是状态机基础，且 ocdroid 源码已含 slim SSE 路径（`SlimSseHandler`/`SseEventBridge`/`SkeletonReloadCoordinator`），需用 live access log 重新基线，不能据旧文档判断。

---

## 2. 背景与目标

`oc-slimapi` 是 ocdroid（Android）与 opencode（:4096 legacy `/session/**`）之间的省流 sidecar（:4097）。架构与契约权威见 [`AGENTS.md`](../../../AGENTS.md) 与 [`docs/specs/v2-contract.md`](../../specs/v2-contract.md)。

**评估目标**：找出还能做的高价值接口，高价值 = 简化 ocdroid 调用方式 + 降低流量。

**评估原则**：
- 字节价值以 **live 实测**为准（raw + gzip 双口径），不据源码 schema 推断下定论；
- 客户端价值以 **ocdroid 实际消费字段**为准（schema 存在 ≠ 客户端消费）；
- 契约影响必须明确分类（加性 / 破坏性）；
- 状态机/并发风险优先于字节收益。

---

## 3. 评估方法

| 阶段 | 主体 | 产出 |
|---|---|---|
| 理论调查 lane-1 | explorer（上游行为） | 6 端点 handler + schema 字段级分析 |
| 理论调查 lane-2 | explorer（ocdroid 消费） | 每端点调用点、消费字段、UI 场景、轮询循环清单 |
| 理论调查 lane-3 | explorer（sidecar 约束） | skeleton 复用性、状态机影响、契约分类、v2 设计哲学 |
| **live 实测** | fixer-zlm | 6 端点真实采样 + 字段字节占比 + skeleton 投影省流比 |
| **独立评审** | rev-ogpt | 逐方案可行性/价值/风险、实施顺序、盲点、ocdroid 协同提示词 |

---

## 4. 证据基础

### 4.1 真实流量（5 天 access log，233,223 请求）

- passthrough：90% 请求数 / **13% 上游字节（222 MB）**
- slimapi：10% 请求数 / **87% 上游字节（1,523 MB）** → 大 payload 读路径已被省流覆盖
- passthrough 字节 Top：`/command` 103 MB、`/session/{sid}/message` 76 MB、`/agent` 48 MB
- `/command` 与 `/agent` 请求数完全一致（953 ≈ 953/5天），疑似项目切换/重连时配对调用（构建命令面板 + agent 选择器）

### 4.2 live 实测数据（opencode :4096，v1.18.13）

| 端点 | 实测 raw | top 占字节字段（占比） |
|---|---|---|
| `GET /command` | **292 KB** | `template` **97.7%** · `description` 1.9% · `name` 0.2% |
| `GET /agent` | **250 KB** | `permission` **61.2%** · `prompt` **34.7%** · `description` 3.5% |
| `GET /session/{sid}` | 556 B avg | `tokens` 15.3% · `model` 12.6% · `time` 8.7% |
| `GET /question` | **2 B（空 `[]`）** | — |
| `GET /permission` | **2 B（空 `[]`）** | — |
| `GET /session/status` | 10.5 B avg（55,999 次） | 89% 返回空 `[]` |

### 4.3 ocdroid 实际消费（代码追溯）

| 端点 | 调用点 | 实际消费字段 | UI 场景 |
|---|---|---|---|
| `GET /command` | `StandardApi.kt:156` | `name, description, agent, hints` | `/`-command 自动补全 |
| `GET /agent` | `StandardApi.kt:146` | `name, description, mode, hidden, native` | agent 选择器 |
| `GET /session/{sid}` | `OpenCodeRepository.kt:1034` | `id, directory, title, parentId, time, agent, model` | 解析子 agent session |
| `GET /question` | `getPendingQuestions()` | 完整结构（含 options/description） | 后台通知 + question 卡片 |
| `GET /permission` | `getPendingPermissions()` | 完整结构（含 metadata） | 后台通知 + 授权卡片 |

### 4.4 sidecar 约束要点

- `skeleton_session()` 已实现（`src/oc_slimapi/skeleton.py:330-340`），纯投影无副作用。
- digest 状态机（`sse/global_hub.py`）纯事件驱动：处理 `session.*`/`message.*`；`question.asked`/`permission.asked` 直推不进 debounce；`message.part.delta`/`tool.*` 丢弃。
- v2 删 G6 批量全文不止去 envelope，还去掉了：discover、per-mid error、**累计 413**、单项 413、定序、TransformPool admission、fingerprint/304、BatchLedger、部分成功处理。
- 新增 `/slimapi` 路由必须同步进 `docs/specs/INTERFACE_MAP.md`，否则 `scripts/check.sh` 失败。

### 4.5 实测澄清的关键误判

> 以下三点直接改写了初步评估，是本报告与"纯源码推断"的关键差异。

1. **"9 B 异常"真相**：上一轮把 `/session/status`（55,999 次/10.5B，89% 空 `[]`）误归类为 `/session/{sid}`。真正的单 session 详情仅 **626 次/5天、avg 556 B** → S3 流量价值被高估。
2. **`/agent` 字段名修正**：理论据旧 schema 写的 `system`/`request.body`，实测真名是 **`prompt`**/`permission`。`permission`（agent 的 Permission.Ruleset）占 **61.2%** 是意外发现——比预期大得多。
3. **gzip 对 `/agent` 的消解**：raw 省流 95.8% → gzip 仅 89.4%（`permission` 重复字符串 gzip 压缩率高）。**验收必须用 gzip/downOut 口径，不能只宣传 raw 95.8%**。

---

## 5. 方案评估

### S1 — `GET /slimapi/command` skeleton  ✅ 采纳

| 维度 | 结论 |
|---|---|
| 可行性 | 高。纯 list projection，无状态机。**加性**契约（新路由，不 bump `X-Slimapi-Version`）。复用 TransformPool/gzip/上游错误处理模式。 |
| 价值 | live raw 292 KB → skeleton 7.25 KB（raw 省 97.6% / gzip 省 97.0%）。 |
| **白名单（评审修正）** | 保留 `{name, description, agent, hints}`。**初步方案的 `{name, description}` 过于激进**——ocdroid `CommandInfo` 明确消费 `agent`+`hints`（`hints` 为开放型 `JsonElement`）。剥离 `template`、`source`。`hints` 需单项+总大小限制，防未来把大段文档塞进 hints 吞掉收益。 |
| `/command/full` | **暂不做**。客户端 command 仅用于补全/名称/描述/参数提示，执行走 `POST /session/{sid}/command`，**不需要下载 template**。提前建 full 端点 = 重新暴露 292 KB + 缓存一致性 + 隐式依赖。确认有命令详情页/模板预览需求后再做。 |
| 风险 | ① **目录语义未确认**：command 是否随 directory/项目配置变化？若是，现有 cache whitelist 可能把 A 项目命令显示给 B 项目——新 route 默认 `no-store`，不要复制现有 cache 策略。② 新 route 需 body cap + 非 list 响应 503 + 坏 JSON 503 + TransformPool admission + worker-thread projection + `Vary: Accept-Encoding` + 不把 template 写日志。 |
| 客户端协同 | ocdroid 需新增 SlimApi 方法；**仅 404/thin_route_not_found 才 fallback** 到旧 `/command`；503/413/timeout 不得 fallback（避免流量翻倍）。 |

### S2 — `GET /slimapi/agent` skeleton  ✅ 采纳

| 维度 | 结论 |
|---|---|
| 可行性 | 高。同 S1。**加性**契约。 |
| 价值 | live raw 250 KB → skeleton 10.7 KB（raw 省 95.8% / **gzip 省 89.4%**）。 |
| **白名单（评审修正）** | 保留 `{name, description, mode, hidden, native}`。**初步方案漏了 `native`**——ocdroid `AgentInfo` 明确定义该字段。剥离 `prompt`、`permission`、`topP`、`temperature`、`color`。 |
| `permission` 占 61% 是否保留摘要？ | **不保留**。`agent.permission` 是 agent 规则集（非 pending card 的 `permission.metadata`），占大头只说明它是大字段，不说明 agent picker 需要它。摘要（如 `ruleCount`+`actions`）无法表达 wildcard/优先级/deny-ask-allow 语义，且当前 UI 无消费点。若未来 agent 详情页需要，设计独立摘要，**不能当作完整 policy**。 |
| `/agent/full` | **暂不做**。agent UI 是选择器，无证据需要 prompt/permission。若未来需要，优先"按 agent 名称查单条详情"，不直接提供全量 `/agent/full`。 |
| 风险 | 同 S1（目录语义、body cap、projection 错误处理）。验收用 gzip 口径。 |

### S3 — `GET /slimapi/sessions/{sid}` 单条 skeleton  ⏸ 延后

| 维度 | 结论 |
|---|---|
| 可行性 | 技术最简单——直接复用 `skeleton_session()`，纯 projection。 |
| 价值 | **低**。实测 626 次/5天、avg 556 B，总量约几百 KB。省流收益微乎其微，主要是接口对称/整洁。 |
| 处置 | **延后**，只有发现真实高频调用点后再做。优先级低于 status 批量化。 |

### S4 — 批量 status  ✅ 修改后采纳

| 维度 | 结论 |
|---|---|
| 可行性 | **不修改现有 `/slimapi/sessions/status`**。新增独立路径 `GET /slimapi/sessions/status/batch?directory=<repeatable>`（repeatable query，**不用逗号拼接**——目录可能含逗号/转义）。 |
| **上游 fan-out 策略** | **先验证再决定**。上游 `/session/status?directory=` 可能返回全量 map（directory 参数对上游 no-op）。若是，sidecar 只发**一次** upstream 请求 + 一次 turn merge，不 fan-out。只有确认上游按目录过滤时才引入 bounded fan-out。 |
| 部分失败语义 | **必须用 envelope**，不能平铺 map。平铺 map 无法区分"目录空/请求失败/sid 不存在"。envelope：`{complete, snapshotAt, results:[{directory, ok, statuses|error}]}`。**不用 HTTP 207**（Retrofit 多半把非 2xx 直接判失败，丢失成功目录）。 |
| 价值 | status 55,999 次/5天、89% 空——价值在**省请求数/电量/RTT**，非字节。但需先核对 `BackgroundUnreadPoller`/`StatusPollOrchestrator` 等多循环是否重复查同目录，否则只是换形式重复。 |

### S5 — 消息列表迁移确认 + SSE 迁移核验  🔁 发布闸门（最高优先级）

| 维度 | 结论 |
|---|---|
| 状态 | 消息列表代码层已部分迁移（`getSlimapiMessagesSkeleton`、`SlimSessionSource`）；**SSE 源码已含 slim 路径**（`SSEClient.slimMode`、`SlimSseHandler`、`SseEventBridge`、`SkeletonReloadCoordinator`）。文档"0/8 未对接"已严重滞后。 |
| **必须做** | 用 **live access log** 核对 APK 实际打 `/slimapi/events` 还是 `/global/event`；核对 76 MB passthrough message 流量来自哪些调用点（冷启动/resync/前后台/child 切换/token stream 完成后 reconcile/full 展开 fallback）。**不能据旧文档重判。** |
| **关键状态机风险** | `SseEventBridge.isControlEvent()` 须确保 slim 控制面事件 `session.digest`/`session.error`/`resync`/`server.heartbeat`/`server.connected` 进**不可丢失 control 路径**，不能因 delta overflow 丢弃——否则 digest 丢→消息不 reload、resync 丢→客户端持旧状态、heartbeat 丢→watchdog 误判断线。 |
| q/p | 直推不进 250ms debounce 是对的。但 q/p 事件只是观察信号，SSE 断线期间会丢，客户端必须 authoritative REST fetch，且 fetch 与 SSE 间 race window 要去重。`/question`/`/permission` 完整 payload 不可 skeleton。 |
| 处置 | **这是多数新接口之前的状态机基础，提前推进。** |

### S6 — 简化版批量全文 `POST /slimapi/messages/{sid}/full`  ❌ 否决（条件性重审）

评审否决了"无 envelope 简单并发聚合不违背 v2"的判断。v2 删 G6 不止去 envelope，还去了**累计 413 / 单项 413 / TransformPool admission / 资源保护**。"简化版"会绕过这些资源保护，多个 full 合并可造成数百 MB 内存峰值。

**替代**：先做客户端 bounded parallelism（2–4 并发 full），据真实延迟/资源指标再决定是否重新设计 batch endpoint。若重做，必须先建立：mid 数量上限、单条/累计 logical bytes 上限、response serialized bytes 上限、并发抓取数、顺序、失败语义、admission 粒度、中途断开取消策略。

### S7 — `/question`+`/permission` skeleton  ❌ 否决

live 实测日常 2 B（空），skeleton 无字节收益；且 `permission.metadata` 是授权决策所需，裁了破坏功能。省请求数应通过 polling coalescing，不是裁字段。

### S8 — digest 扩展 children/question/permission  ❌ 否决

children 无稳定上游事件源（需轮询，破坏 digest 纯事件驱动模型）；q/p 有实时性要求（不适合 250ms debounce）；三类信息职责不同，混合后 reducer 复杂度和丢失语义都增加。

### S9 — sessions+status 合并  ❌ 否决当前方案

改 `/slimapi/sessions` 裸数组响应形状 = **破坏性变更**，不能套"加性"说法。即便可选参数也会：同路径多 shape、list 成功但 status 失败耦合、强制每次查 status、破坏旧客户端解析。若未来需要冷启动快照，设计独立 `GET /slimapi/sessions/snapshot`。

### S10 — 图片代理  ⚠️ 另立安全工单，不进省流主线

67 KB/5天不足以证明新增代理层。SSRF/DNS rebinding/重定向/metadata service/Content-Type 欺骗/凭证泄漏等风险高。先做现有图片路径正确性+安全审计（C 桶违规），不为低流量收益新增代理。

---

## 6. 实施顺序（评审修正后）

```
阶段 0  运行时事实核验（前置闸门）
  ├─ live opencode 版本（实测 v1.18.13 vs 仓库对照 v1.18.4，需重新基线）
  ├─ ocdroid APK 实际打 /slimapi/events 还是 /global/event
  ├─ 76 MB passthrough message 流量来自哪些调用点
  ├─ /command、/agent cache hit/miss + 是否目录相关
  ├─ status 是否被多循环重复拉取
  └─ token stream 是否由 health feature 控制
        ‖
阶段 1  双 lane 并行
  ├─ Lane A: sidecar + ocdroid catalog skeleton（S1 + S2）
  │    ├─ command skeleton（白名单 {name,description,agent,hints}，暂无 full）
  │    ├─ agent skeleton（白名单 {name,description,mode,hidden,native}，暂无 full）
  │    ├─ feature flag / 404 capability probe（加性 ≠ 旧 sidecar 支持）
  │    ├─ ocdroid fallback：仅 404 才 fallback；503/413/timeout 不 fallback
  │    └─ 监控 raw/gzip/downOut + fallback 次数 + projection 失败
  └─ Lane B: 消息/SSE 状态机（S5）
       ├─ 确认所有消息列表访问走 slim（含冷启动/resync/前后台/child 切换/reconcile）
       ├─ 确认实际 SSE 路径 + 修复 digest/resync/error/heartbeat 可靠通道
       ├─ 验证 q/p race window + resync 冷启动
       └─ 验证 token stream 与 control SSE 生命周期分离
        ↓
阶段 2  S4 batch status（/slimapi/sessions/status/batch，先验证上游是否全量再决定 fan-out）
        ↓
阶段 3  S3 single-session skeleton（仅发现高频调用后）
        ↓
阶段 4  full endpoints（仅 ocdroid 确认 prompt/template/permission 真实需求后）
        ↓
阶段 5  S6（仅客户端 bounded parallelism 实验后条件性重审）
```

**S7/S8/S9/S10 不进入当前省流主线。**

---

## 7. 风险与盲点

### 重大风险
1. **资料与源码漂移**：live probe v1.18.13 vs 仓库对照 v1.18.4，ocdroid 已存在 slim SSE 代码——必须按运行时证据重新基线。
2. **白名单误删客户端已定义字段**：command 的 `agent/hints`、agent 的 `native` 不可静默删。
3. **SSE control 事件可能被 delta overflow 丢弃**：digest/resync/heartbeat 不能走可丢弃路径。
4. **batch status 平铺 map 无法表达部分失败**——必须 envelope。
5. **S6 会绕过 v2 累计资源保护**——故否决。
6. **加性 endpoint ≠ 旧 sidecar 支持**：需 health feature flag 或 404 capability probe，不能只看版本号。
7. **目录不是鉴权边界**：新 route 仍需 mTLS/网络边界 + directory 输入校验。

### 重要风险
- command/agent cache 可能错误假定全局无目录依赖；
- 多目录 fan-out 可能放大 upstream 压力（故先验证上游是否全量）；
- q/p 空响应采样不能代表 pending 高峰态；
- raw 省流 ≠ gzip/TLS 实际省流（`/agent` 已证）；
- 同一 sid 跨目录 batch status 可能覆盖；
- full fallback 在网络差环境下可能流量风暴（故 503/413 不 fallback）；
- 新 route 必须同步更新 `INTERFACE_MAP.md` + `CHANGELOG.md` + 测试，否则 `check.sh` 阻断。

---

## 8. 给 ocdroid 项目组的协同改造与确认提示词

> 以下内容可直接转发。

```text
请 ocdroid 项目组配合确认 oc-slimapi 下一阶段省流接口与状态机改造。

一、sidecar 计划

1. Command skeleton

GET /slimapi/command
Headers:
  X-Slimapi-Version: 2

Query:
  directory=<optional，需双方确认是否存在目录语义>

成功返回裸数组：

[
  {
    "name": "...",
    "description": "...",
    "agent": "...",
    "hints": ...
  }
]

保留字段：
  name、description、agent、hints

不返回：
  template、source 以及其他大字段

说明：
  - additive wire change，不 bump X-Slimapi-Version；
  - sidecar 负责 list 校验、body cap、projection、gzip；
  - 当前不新增 /slimapi/command/full；
  - 旧 sidecar 对该路由返回 404/thin_route_not_found 时，客户端才 fallback 到旧 GET /command；
  - 503、413、超时不得直接 fallback 到旧大接口，避免流量翻倍。

2. Agent skeleton

GET /slimapi/agent
Headers:
  X-Slimapi-Version: 2

Query:
  directory=<optional，需双方确认是否存在目录语义>

成功返回裸数组：

[
  {
    "name": "...",
    "description": "...",
    "mode": "primary",
    "hidden": false,
    "native": false
  }
]

保留字段：
  name、description、mode、hidden、native

不返回：
  prompt、permission、topP、temperature、color 以及其他详情字段

当前不新增 /slimapi/agent/full。

只有在确认 agent 详情页真实需要 prompt 或完整 permission 后，才重新设计按 agent 名称展开的详情接口。

3. Existing messages

GET /slimapi/messages/{sid}
Query:
  limit=<1..200>
  before=<opaque X-Next-Cursor>
  mode=<可保留发送，但 sidecar 恒返回 skeleton>
  directory=<optional>

返回：
  MessageWithParts[] skeleton

客户端要求：
  - X-Next-Cursor 原样传回 before；
  - 不解析、不重建 cursor；
  - placeholder part 使用 message-level 整体替换；
  - hasFull/omitted 按契约处理；
  - transform_busy 按 Retry-After 重试；
  - 413 不应无条件退回原始大接口。

GET /slimapi/messages/{sid}/full/{mid}

返回：
  单个 MessageWithParts full projection，成功 HTTP 200。

客户端只在按需展开时调用。full 失败时保留 skeleton，不把临时失败误判为 session 删除。

4. Batch status 候选接口

建议新增独立路径：

GET /slimapi/sessions/status/batch
Headers:
  X-Slimapi-Version: 2

Query:
  directory=<repeatable>

示例：
  ?directory=%2Fwork%2Fa&directory=%2Fwork%2Fb

不修改现有：

GET /slimapi/sessions/status?directory=<single>

对于当前 opencode 版本，sidecar 将优先确认 /session/status 是否返回全量 map；如果是，则优先一次 upstream 请求，而不是每个目录 fan-out。

如果未来需要部分失败，响应必须明确区分：

  - 成功且为空；
  - 请求失败；
  - 本次没有该 session。

禁止用平铺 map 中的“缺项”表示失败。

二、ocdroid 需要配合

1. 增加 command/agent skeleton API 方法。

要求：
  - 自动附加 X-Slimapi-Version: 2；
  - command 解析 agent/hints；
  - agent 解析 native；
  - 不依赖 prompt/permission；
  - 旧 sidecar 404 时 fallback；
  - 503/413/timeout 不立即 fallback；
  - fallback 能力结果可缓存，避免每次连接重复探测。

2. 审计消息访问路径。

确认以下路径均不再绕过 slim：

  - 冷启动；
  - resync；
  - 前后台切换；
  - child/session 切换；
  - token stream 完成后的 reconcile；
  - full 展开；
  - 失败恢复。

3. 审计 SSE 实际运行路径。

请使用运行时 access log 确认 APK 实际访问：

  /slimapi/events
还是：
  /global/event

当前源码已经存在 slimMode、SlimSseHandler、SseEventBridge 和 SkeletonReloadCoordinator，因此请按当前代码核验，不要按旧文档重新实现已删除的状态机。

必须确认以下事件进入可靠 control 路径，不能因 delta overflow 丢失：

  session.digest
  session.error
  resync
  server.connected
  server.heartbeat
  question.*
  permission.*

4. SSE 状态机要求。

必须保证：

  - session.digest 丢失不会静默发生；
  - resync 必然触发 sessions/messages reconcile；
  - heartbeat 不会因错误通道分类导致假断线；
  - q/p 事件仍即时到达；
  - q/p 事件之后继续 authoritative REST fetch；
  - SSE 与 REST race window 有去重；
  - token stream 与 control SSE 使用独立生命周期；
  - token stream done=true marker 不携带最终文本；
  - 最终文本以 REST 持久化真值为准。

5. Batch status 消费要求。

如果采用 batch endpoint：

  - ok=true + statuses={} 视为权威空结果；
  - ok=false 不得解析为空 map；
  - 失败目录保留 last-known 状态；
  - 不因部分失败删除 session；
  - 不因缺失项自动判 idle；
  - turnIncarnation 与 turn 必须成对处理；
  - 需确认同一 sid 跨目录时的合并规则。

三、请确认以下问题

1. Agent 详情页或隐藏 UI 是否需要 prompt？
2. 是否有任何 UI 需要完整 agent.permission？
3. command template 是否在客户端渲染、预览或本地解释？
4. CommandInfo.agent 和 hints 是否必须保留？
5. AgentInfo.native 是否有当前或近期消费者？
6. command/agent 是否随目录、项目配置或用户身份变化？
7. 当前 APK 实际连接的是 /slimapi/events 还是 /global/event？
8. status poller 实际每轮请求多少目录？
9. 是否存在 StatusPollOrchestrator 与 BackgroundUnreadPoller 重复查询？
10. batch status 部分失败时，产品要求保留旧状态还是整体标记 Unknown？
11. 当前 full message 请求的批大小、单条大小和延迟分布如何？
12. 需要兼容哪些旧版本 sidecar？
13. 是否接受新客户端在旧 sidecar 上通过 404 fallback？

四、兼容和灰度要求

1. 先部署 sidecar 新路由，再启用客户端调用。
2. 所有 slim 请求继续发送 X-Slimapi-Version: 2。
3. additive route 不自动意味着旧 sidecar 支持；应使用：
   - health 中明确定义 feature flag，或；
   - 一次性 404 capability probe。
4. 404/thin_route_not_found 才表示“不支持该 feature”。
5. 503、413、版本错误、鉴权错误不得当作“不支持”。
6. 客户端通过 feature flag 灰度启用。
7. 监控：
   - skeleton raw/gzip/downOut bytes；
   - 404 fallback 次数；
   - projection/parse 失败；
   - status batch timeout/partial；
   - SSE reconnect/resync；
   - control event dropped；
   - TransformPool busy；
   - upstream concurrency；
   - Android cache hit/miss。
8. 如出现异常，关闭新 feature flag 即可回退，不修改旧 /command、/agent、/global/event 路径。
9. 双方确认字段、目录、运行时路径、fallback 和状态机语义后，才能默认开启。
```

---

## 附录：评估产物索引

| 产物 | 位置 |
|---|---|
| live 采样响应（command/agent/session/question/permission/messages） | `/tmp/opencode-probe/*.json` |
| 字段级字节分析 + skeleton 投影脚本 | `/tmp/opencode-probe/analyze.py` |
| 历史流量 access log | `~/.local/state/oc-slimapi/logs/access-*.jsonl(.gz)` |
| 流量查询手册 | [`docs/manual/traffic-accounting.md`](../manual/traffic-accounting.md) |
| 契约权威（实现时以此处为准） | [`docs/specs/v2-contract.md`](../../specs/v2-contract.md) |
