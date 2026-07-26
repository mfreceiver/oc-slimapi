# ocdroid 阶段 A 新会话执行提示词

> 将下方提示词交给 ocdroid 新会话。执行前必须由用户明确指定 owner、目标版本和是否允许生成 commit/tag；未明确时只能勘察，不得写代码或发布。

```text
你负责 ocdroid Slim 会话消息可靠性联合计划的阶段 A 自身任务。

先阅读 ocdroid 仓库中的联合主计划：
- docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md

并对照 oc-slimapi 协作材料（若本机可访问）：
- oc-slimapi/docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md
- oc-slimapi/docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md

联合计划状态：rev-gpt 9.5/10 PASS，D-GATE 已暂停；当前只执行阶段 A 自身任务，不执行阶段 B 联调，不新增协议 revision。

先精确定位并记录源码符号：
- Slim SSE reducer；
- since/cursor drain/full fetch；
- SessionSyncCoordinator / SlimSessionReconciler 或等价组件；
- localApplied、remote watermark、dirty、needsReconcile；
- token stream done/idle/resync/backpressure/reconnect_no_replay；
- messageID 去重与分页。

阶段 A 必须实现的客户端安全语义：

1. `/since` 结果先 staging：
   - 不把 HTTP 200 + empty items 直接视为补传成功；
   - 支持 `X-Since-Complete: true|false`；缺失时兼容旧 sidecar，但 remote watermark 领先 localApplied 时不得误清 dirty；
   - 只有 cursor-null terminal / 明确 complete 的完整结果才允许推进 bookmark/localApplied/authoritative cache。
2. `SlimDrainOutcome.Success` 只表示 cursor-null terminal；cap、partial、timeout、取消、无效响应不推进 bookmark。
3. `/since`、cursor drain、full fetch 的 503/429/413/timeout/invalid JSON/空 body/中途断流：
   - 不清已有消息；
   - 不清唯一可见 token 内容；
   - 不把失败转换为空结果；
   - 保留 dirty 并使用 bounded retry/backoff。
4. 普通 `reconnect_no_replay`、backpressure、`session_idle` 只触发核对，不等于删除；只有明确 `session_deleted` 才能清理已确认删除对象。
5. token done/idle 后保留 provisional 内容，权威消息成功后再替换；不得先清空唯一可见内容再等待 `/since`。
6. SSE 开启和关闭、polling、manual refresh、token done、resync 最终进入同一 reconcile 规则。

阶段 A 不得做：
- 不新增 message revision、partCount、session generation 或其他第2类 wire 字段；
- 不假定 `createdAt` 能表示同一 assistant message 的内容追加；
- 不擅自决定 full/cursor snapshot merge 是 union 还是 replacement；
- 不把 session_idle 当删除；
- 不开始与 oc-slimapi 的真实联调；
- 不生成发布 commit/tag，除非用户明确授权 owner、版本和发布范围。

测试至少覆盖：
- since=0、localApplied>0、oldest-first、跨页、空结果、X-Since-Complete true/false/缺失；
- cursor fallback、503/429/413/timeout/invalid/空 body；
- reconnect/backpressure/idle/deleted；
- token 正常 done、失败后内容保留；
- 同 messageID 多次追加、重复/乱序 digest；
- dirty 不误清且最终不会无界重试。

交付格式：
1. 精确文件/符号/状态时序；
2. 阶段 A 修改与测试证据；
3. 版本、commit/tag、artifact SHA-256、不可变 check log（若获授权）；
4. 是否满足进入阶段 B 的客户端侧条件；
5. 明确声明未实施阶段 B，也未冻结新的 watermark/merge wire。
```
