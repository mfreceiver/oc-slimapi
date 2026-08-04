# ocdroid 候选省流接口协同改造 — 交接提示词

> 将下方「提示词正文」整段交给 ocdroid 侧 agent/开发者。
> 配套评估报告（含完整证据链、实测数据、评审修正）：[`../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md`](../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md)。
> 本轮 oc-slimapi 侧尚未动代码；**双方先冻结设计 + 回答确认问题后再各自实现**，避免单方破坏 wire。

---

## 提示词正文

```text
请配合 oc-slimapi 推进下一阶段省流接口与状态机核验。本轮目标是冻结 S1/S2/S4/S5 的设计与客户端改造点，回答待确认问题，再各自实现。先只读分析现有代码与运行时 access log，不要凭假设改协议。

背景（评估已完成，结论已用 live 实测验证）：

1. oc-slimapi 已覆盖大 payload 读路径（slimapi 占 10% 请求、87% 上游字节）。剩余省流价值集中在：
   - 字节：两个 catalog 端点（/command、/agent）
   - 请求数/电量：status 批量化 + SSE/消息迁移运行时核验
2. live 实测（opencode v1.18.13 @ :4096，采样产物 /tmp/opencode-probe/）：
   - GET /command  raw 292 KB，template 占 97.7%；skeleton {name,description,agent,hints} → 7.25 KB / gzip 3.18 KB，省 97.6% / 97.0%
   - GET /agent    raw 250 KB，permission 61.2% + prompt 34.7%；skeleton {name,description,mode,hidden,native} → 10.7 KB / gzip 3.57 KB，省 95.8% / 89.4%
   - 注意：gzip 对 /agent 有消解（permission 重复字符串压缩率高），验收必须用 gzip/downOut 口径
   - GET /question、GET /permission 日常 2 B（空），skeleton 无收益；/question、/permission 完整结构 UI 必需，不裁
   - GET /session/{sid} 仅 626 次/5天、avg 556 B（"9B 异常"实为 /session/status 的空响应），单 session skeleton 价值低，延后
3. ocdroid 实际消费（代码追溯）：
   - /command：UI 仅消费 name/description/agent/hints（/-command 补全），不消费 template
   - /agent：UI 仅消费 name/description/mode/hidden/native（agent 选择器），不消费 prompt/permission
4. 资料与源码漂移：仓库对照 opencode v1.18.4，live 为 v1.18.13；ocdroid 源码已含 slimMode/SlimSseHandler/SseEventBridge/SkeletonReloadCoordinator。所有"未对接"判断必须用运行时 access log 重新核验，不能据旧文档。

一、sidecar 计划端点（加性 wire change，不 bump X-Slimapi-Version）

1) GET /slimapi/command（skeleton）
   Headers: X-Slimapi-Version: 2
   Query: directory=<optional，目录语义待双方确认>
   返回裸数组，保留字段：name、description、agent、hints
   不返回：template、source 及其他大字段
   说明：
   - sidecar 负责 list 校验、body cap、projection、gzip、worker-thread projection、Vary: Accept-Encoding、不把 template 写日志
   - 当前不新增 /slimapi/command/full（客户端无 template 消费需求；确认有命令详情页/预览需求后再做）
   - hints 为开放型 JsonElement，sidecar 会对单项+总大小设限，防止未来把大段文档塞进 hints 吞掉收益

2) GET /slimapi/agent（skeleton）
   Headers: X-Slimapi-Version: 2
   Query: directory=<optional，目录语义待双方确认>
   返回裸数组，保留字段：name、description、mode、hidden、native
   不返回：prompt、permission、topP、temperature、color
   说明：
   - permission 占 61% 是 agent 的 Permission.Ruleset（非 pending card 的 metadata），UI 无消费点，不保留摘要
   - 当前不新增 /slimapi/agent/full（确认 agent 详情页真实需要 prompt/permission 后，优先"按 agent 名称查单条详情"，不提供全量 full）

3) GET /slimapi/sessions/status/batch（新增独立路径，不改现有 /slimapi/sessions/status）
   Headers: X-Slimapi-Version: 2
   Query: directory=<repeatable>（用 repeatable query，不用逗号拼接——目录可能含逗号/转义）
   示例：?directory=%2Fwork%2Fa&directory=%2Fwork%2Fb
   说明：
   - sidecar 先确认上游 /session/status 是否返回全量 map；若是，优先一次 upstream 请求 + 一次 turn merge，不 fan-out；只有确认上游按目录过滤时才引入 bounded fan-out
   - 部分失败必须用 envelope（{complete, snapshotAt, results:[{directory, ok, statuses|error}]}），不用平铺 map（无法区分"目录空/请求失败/sid 不存在"），不用 HTTP 207（Retrofit 多半把非 2xx 直接判失败）

4) 既有消息端点（已存在，本轮是核验而非新增）
   GET /slimapi/messages/{sid}（skeleton）+ GET /slimapi/messages/{sid}/full/{mid}
   - X-Next-Cursor 原样传回 before；不解析、不重建 cursor
   - placeholder part 用 message-level 整体替换；hasFull/omitted 按契约处理
   - transform_busy 按 Retry-After 重试；413 不无条件退回原始大接口
   - full 失败保留 skeleton，不把临时失败误判为 session 删除

二、ocdroid 需要配合

1. 新增 command/agent skeleton API 方法
   - 自动附加 X-Slimapi-Version: 2
   - command 解析 agent/hints；agent 解析 native
   - 不依赖 prompt/permission/template
   - fallback 规则：旧 sidecar 返回 404/thin_route_not_found 才 fallback 到旧 /command、/agent；503/413/timeout/版本错误/鉴权错误不得当作"不支持"立即 fallback（避免流量翻倍）
   - fallback 能力结果可缓存，避免每次连接重复探测

2. 审计消息访问路径（确认全部走 slim，不绕过）
   覆盖：冷启动 / resync / 前后台切换 / child-session 切换 / token stream 完成后 reconcile / full 展开 / 失败恢复
   目标：解释 5 天 76 MB 的 GET /session/{sid}/message passthrough 流量来自哪些调用点

3. 审计 SSE 实际运行路径（关键状态机风险）
   - 用 live access log 确认 APK 实际打 /slimapi/events 还是 /global/event
   - 确认 SseEventBridge.isControlEvent() 把以下事件归入"不可丢失 control 路径"，不能因 delta overflow 丢弃：
       session.digest / session.error / resync / server.connected / server.heartbeat / question.* / permission.*
     否则：digest 丢→消息不 reload；resync 丢→客户端持旧状态；heartbeat 丢→watchdog 误判断线
   - token stream 与 control SSE 使用独立生命周期；token done=true marker 不携带最终文本；最终文本以 REST 持久化真值为准
   - q/p 事件后继续 authoritative REST fetch；SSE 与 REST race window 有去重

4. batch status 消费（若采用）
   - ok=true + statuses={} 视为权威空结果；ok=false 不得解析为空 map
   - 失败目录保留 last-known 状态；不因部分失败删 session；不因缺失项自动判 idle
   - turnIncarnation 与 turn 必须成对处理
   - 确认同一 sid 跨目录时的合并规则

三、请确认以下问题（冻结设计前必须回答）

1. Agent 详情页或隐藏 UI 是否需要 prompt？
2. 是否有任何 UI 需要完整 agent.permission？
3. command template 是否在客户端渲染、预览或本地解释？
4. CommandInfo.agent 和 hints 是否必须保留？（ocdroid 当前消费，默认保留）
5. AgentInfo.native 是否有当前或近期消费者？
6. command/agent 是否随目录、项目配置或用户身份变化？（影响 cache 策略与 directory 参数语义）
7. 当前 APK 实际连接的是 /slimapi/events 还是 /global/event？（用 access log 回答）
8. status poller 实际每轮请求多少目录？
9. 是否存在 StatusPollOrchestrator 与 BackgroundUnreadPoller 重复查询同目录？
10. batch status 部分失败时，产品要求保留旧状态还是整体标记 Unknown？
11. 当前 full message 请求的批大小、单条大小和延迟分布如何？（用于评估是否需服务端 batch）
12. 需要兼容哪些旧版本 sidecar？
13. 是否接受新客户端在旧 sidecar 上通过 404 fallback？

四、兼容和灰度要求

1. 先部署 sidecar 新路由，再启用客户端调用
2. 所有 slim 请求继续发送 X-Slimapi-Version: 2
3. additive route ≠ 旧 sidecar 支持；客户端用 health feature flag 或一次性 404 capability probe，不能只看版本号
4. 404/thin_route_not_found 才表示"不支持该 feature"
5. 客户端通过 feature flag 灰度启用；异常时关 flag 即回退，不动旧 /command、/agent、/global/event
6. 监控指标：skeleton raw/gzip/downOut bytes、404 fallback 次数、projection/parse 失败、status batch timeout/partial、SSE reconnect/resync、control event dropped、TransformPool busy、upstream concurrency、Android cache hit/miss

五、实施顺序建议（双方对齐）

阶段 0  运行时事实核验：live opencode 版本、APK 实际 SSE 路径、76 MB message passthrough 来源、/command+/agent cache hit/miss + 目录相关性、status 多循环是否重复、token stream 是否由 health feature 控制
阶段 1  双 lane 并行：
   - Lane A（catalog skeleton）：sidecar /command+/agent skeleton 路由 + ocdroid SlimApi 方法 + 404 fallback + feature flag
   - Lane B（消息/SSE 状态机）：消息路径全走 slim 核验 + digest/resync/error/heartbeat 可靠通道修复 + q/p race 去重 + resync 冷启动验证
阶段 2  batch status（先验上游是否全量）
阶段 3  single-session skeleton（仅发现高频调用后）
阶段 4  full endpoints（仅确认 prompt/template/permission 真实需求后）
阶段 5  服务端 batch full（仅客户端 bounded parallelism 实验后条件性重审）

六、交付证据

1. 运行时核验结论（含 access log 数据）
2. ocdroid 改动文件清单与接口差异
3. 消息/SSE 状态机修复点与测试矩阵
4. 对 13 个确认问题的答复
5. 兼容旧 sidecar 策略
6. 是否需要 X-Slimapi-Version bump 及理由
7. commit、tag、artifact SHA-256、check log

在双方确认前：不要把 session_idle 当删除；不要把补传失败当空结果；不要清除唯一可见 token 文本；不要单方改 wire 字段。
```

---

## 本仓将单方推进的部分（不阻塞 ocdroid）

- `GET /slimapi/command` skeleton 路由（projection + body cap + gzip + `INTERFACE_MAP.md`/测试同步）
- `GET /slimapi/agent` skeleton 路由（同上；白名单含 `native`）
- `GET /slimapi/sessions/status/batch`（先验证上游 `/session/status` 是否全量 map，再决定 fan-out 策略；部分失败用 envelope）
- `/command`、`/agent`、`/global/event` 旧路径保持不变（灰度回退锚点）

涉及 wire 形状的字段白名单、batch envelope schema、capability probe 形式 **等双方确认后再定稿**，避免单方破坏契约。

---

## 相关文档

- 评估报告（完整证据链 + 实测 + 评审）：[`../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md`](../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md)
- 契约权威：[`../../specs/v2-contract.md`](../../specs/v2-contract.md)
- 客户端配套说明：[`../../specs/CLIENT_CHANGES.md`](../../specs/CLIENT_CHANGES.md)
- 实测采样产物：`/tmp/opencode-probe/`（`command.json`、`agent.json`、`analyze.py` 等）
