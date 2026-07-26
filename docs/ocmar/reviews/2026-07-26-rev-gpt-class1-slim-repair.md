# rev-gpt：第1类 Slim 修复评审

**日期：** 2026-07-26  
**对象：** oc-slimapi 当前未提交工作树  
**范围：** `/since` 页序与完整性、SSE hub、`/full` 错误处理、strip 内存路径、文档与测试  
**结论：** 有条件通过

## 总结

核心修复方向正确，未发现已确认的 P0 缺陷。`/since` 已移除“首个不匹配即停止”的错误启发式，按 opencode v1.18.4 `MessageV2.page()` 的页内 oldest-first 语义整页过滤；`flush_sid` 只处理目标 session；full 路径的非法 JSON、空 body 和中途读取失败统一为结构化 503；本轮没有单方面引入第2类的 revision、part watermark 或 token 终态 wire 语义。

## 评审确认

### `/since`

- 未发现残留的“首项即停”逻辑。
- 页内过滤不会因首个不匹配项而丢弃后续匹配消息。
- oldest-first 地板使用页首（页内最旧消息），在当前上游 message-level `created` 与排序一致的前提下成立。
- `X-Since-Complete` 的新增是加性行为，不要求旧客户端配合。

### `flush_sid`

`flush_sid(session_id)` 只执行目标 session 的 `pending.pop(session_id)`，不会排空其他 session；目标 digest 广播给所有 subscriber 是预期行为。

### full 错误语义

当前行为一致：

- 上游 200 + 空/非法 JSON → 503 `upstream_unavailable`；
- `send()` 或 body 迭代期间 `httpx.RequestError` → 503 `upstream_unavailable`；
- 合法但错误 shape → 继续 200 原样服务；
- 上游自身 >=400 → 维持既有状态/body 处理。

### 第2类边界

本轮没有新增 message revision、partCount、content generation，也没有改变 token idle/resync 的客户端清态语义。消息内容变化 watermark、token stream 终态和 ocdroid reconcile 仍属于双方配合改造。

## 收尾项及处理结果

| 评审项 | 处理 |
|---|---|
| `updated` 非单调的地板假设 | 已在测试/文档注释中明确：当前对齐上游 message-level 使用 `created`，并依赖其与排序一致；未擅自引入 revision |
| 上游 limit 不变量 | 已注明由上游 `limit(input.limit + 1)` 后 slice(limit) 保证 |
| `archived=True` 被 `int` 接受 | 已修为真实整数且排除 bool；`archived=0` 仍保留 |
| full-list 中途断流覆盖不足 | 已增加 response 返回后、body 异步迭代中抛出 `httpx.ReadError` 的测试 |
| `X-Since-Complete` 权威语义 | 已同步 `v1-contract.md`、design-v2、CLIENT_CHANGES：true 表示本次扫描未被 max pages 截断，不表示没有更多 cursor |
| CHANGELOG 不完整 | 已补记 full 错误归一、SSE per-sid flush、archived bool 防护 |

## 验证

```text
.venv/bin/pytest tests/test_hub.py tests/test_hub_behavior_lock.py tests/test_messages_routes.py -q
→ 309 passed

./scripts/check.sh
→ 1022 passed
→ 路由↔文档一致：19 条 /slimapi 路由

git diff --check
→ 无输出（通过）
```

## 剩余风险

1. `X-Since-Complete` 需要客户端与 `X-Next-Cursor` 联读；旧客户端会忽略新增头，因此不会因该加性字段回归，但旧客户端不会利用截断提示。
2. `/since` 地板正确性依赖当前上游版本的 message-level `created` 与排序不变量；若未来上游引入可变 `updated` 或改变排序，需要重新设计 watermark，而不是继续沿用当前地板逻辑。
3. body 读取异常映射依赖当前 httpx 传输栈将连接错误表现为 `httpx.RequestError`；当前部署路径符合该假设。
4. 第2类问题仍未解决：同一 assistant message 内容追加的可检测 watermark、token stream 正常完成后的 provisional 内容保留、SSE 开/关统一 reconcile，需与 ocdroid 联合改造。

**评审结论：** 第1类改造可作为已验证的兼容性修复合入候选；第2类协议/客户端改造不得由本仓单方面继续扩展。
