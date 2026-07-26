# ocdroid 第2类配合改造 — 交接提示词

> 将下方「提示词正文」整段交给 ocdroid 侧 agent/开发者。  
> oc-slimapi 第1类（`/since` 页序、`X-Since-Complete`、SSE flush、/full 健壮性）由本仓单方推进，**不阻塞**对方先做客户端安全语义。

---

## 提示词正文

```text
请对 ocdroid 的 Slim 模式消息同步链路进行协议级修复设计与实现评审，目标是与 oc-slimapi 配合解决状态更新、消息补传、SSE 失效恢复问题。先只读分析现有代码与测试，不要凭假设改协议。

背景：

1. Slim SSE 开/关最终都依赖消息 reconcile：
   - SSE 开：digest/resync → GET /slimapi/messages/{sid}/since/{ts}
   - SSE 关：poll/status/probe → 同一 /since
2. oc-slimapi 正在单方修复 /since 的上游页序假设（opencode MessageV2.page 为页内 oldest-first）。在修复落地前/后，客户端都不得把「空 items」直接当成补传成功。
3. opencode 消息级通常只有 created/completed，没有可靠的每次内容变更 updatedAt；assistant 同 messageID 会持续追加 part。messageID+createdAt 不能作为完整 content watermark。
4. token stream 正常结束路径可能是：实时 delta → session_idle/resync → 清 streamOwned → /since 拉权威。若 /since 空或失败，可能最终空白。

请完成：

一、代码勘察（精确路径/符号/时序）
- Slim SSE reducer
- since / cursor drain / full
- SessionSyncCoordinator / SlimSessionReconciler（或等价）
- token stream done、idle、resync、backpressure、reconnect_no_replay
- localApplied / remote watermark / dirty / needsReconcile
- messageID 去重与分页
- session.status busy/idle 是否触发重拉

二、客户端安全语义（可先于协议升级落地）
1. 200 + items=[] 不得自动等同补传完成：
   - 支持并读取 X-Since-Complete（sidecar 加性头；缺失时兼容旧行为）
   - remote watermark 仍领先 localApplied 时不得清 dirty
2. /since 空但 remote 仍领先：保留 dirty 与可见内容；bounded cursor drain fallback；去重、最大页数、退避，防风暴
3. 失败（503/429/413/timeout/invalid）：不删已有消息、不删 token 可见内容、不把失败当空结果
4. resync：reconnect_no_replay/backpressure/session_idle 是「需核对」不是「删本地」；仅 session_deleted 可清已确认删除会话
5. token done/idle：不得先清唯一可见文本再等 /since；provisional 保留 → 权威成功后再替换

三、设计 content watermark（至少比较 2 种）
- A message revision
- B partCount / lastPartID
- C session generation
- D busy→idle 作为补充 final refetch
说明 sidecar 字段、客户端比较、旧版兼容、删/乱序/重复、是否 bump X-Slimapi-Version、如何验证同 message 多次追加最终完整。

四、统一 SSE 开/关 reconcile 状态机
同一套规则：保留内容 → since/cursor/full → 完整成功才推进 watermark；空结果仅 complete=true 且 remote 不再领先才收敛；失败保留 dirty；messageID 去重；禁止 watermark 倒退。

五、测试矩阵（至少）
首次 since=0；localApplied>0 增量；oldest-first；跨页边界；空+remote 领先；X-Since-Complete true/false/缺失；cursor fallback；503/429/413/timeout/invalid/空 body；reconnect/backpressure/idle/deleted；token 正常 done；同 messageID 多次追加；digest 乱序重复；SSE 开/关最终集合一致；失败不清空；dirty 收敛无无界重试。

六、交付
1) 当前链路图 2) bug 与触发条件 3) 推荐 watermark 4) 需 sidecar 的字段/接口
5) 客户端实施计划 6) 兼容策略 7) 测试名 8) 是否 bump 版本 9) 待与 oc-slimapi 确认项

在双方确认前：不要把 session_idle 当删除；不要把补传失败当空结果；不要清除唯一可见 token 文本。
```

---

## 本仓将提供的配合（第1类落地后）

- `/since` 在 oldest-first 下不因错误早停丢消息  
- `X-Since-Complete: true|false`  
- 文档勘误页序与完整扫描语义  
- `/full` 非法 JSON 等不再裸 500（若已合入）  

协议级 revision 字段 **等双方确认后再加**，避免单方破坏 wire。
