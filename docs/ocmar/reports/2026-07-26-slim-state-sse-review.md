# Slim 会话状态 / 消息推送 / SSE 失效 — 综合评审报告

**日期:** 2026-07-26  
**基线:** oc-slimapi `2f93e39` (v0.10.0) · 上游对照 `opencode-src/current` → **v1.18.4**（文档仍写 v1.18.3，存在漂移）  
**来源:** explorer 全链路审计 + rev-opus 独立综合（主题：状态/推送/获取/重试/SSE）

## 结论

**Slim 两种组合（SSE 开/关）当前不可作为完整生产替代路径。**  
非 slim 直连仍可靠但不省流。根因是 `/since/{ts}` 页序假设与上游相反，叠加消息级无可靠 content watermark、token stream 终态依赖坏补传。

## 四组合矩阵

| 组合 | 状态更新 | 消息获取 | 增量补传 | 判定 |
|---|---|---|---|---|
| slim + SSE 开 | digest 基本可用 | 冷启动/cursor 可用 | `/since` 稳态高风险空 | 🔴 |
| slim + SSE 关 | 轮询/status 可用 | 同上 | 仍走 `/since` | 🔴 |
| 非 slim + SSE 开 | 原始事件 | 直连 | 客户端 cursor | ✅ |
| 非 slim + SSE 关 | 轮询 | 直连 | cursor | ✅ |

## 上游页序（决定性）

`message-v2.ts` `MessageV2.page()`:

1. `orderBy(desc(time_created), desc(id))` 取最新窗  
2. `items.reverse()` → **页内 oldest-first**  
3. cursor 基于 slice tail（窗内最旧）

slimapi `messages.py` 注释与早停假定 newest→oldest，首个 `<ts` 即 break → 升序页上常 **恒空**。

## 问题优先级

### P0
1. `/since` 页序/早停错误 → 增量丢消息  
2. 消息内容变更无 watermark（仅不可变 created）— **需双方协议**  
3. token idle/resync 清 streamOwned 后依赖坏 `/since` — **需双方**

### P1
4. `max_since_pages` 截断无完整性信号 → 加 `X-Since-Complete`（服务端可先做）  
5. `flush()` 全局排空 → per-sid  
6. 测试夹具全降序 → 零证据  
7. `/full` 非法 JSON/空 body → 500；deepcopy 峰值  

### P2
8. 根 session.created 无 digest  
9. `_extract_session_id` fallback 到 payload.id  
10. `message.appended` 死事件  
11. 版本锚点 v1.18.3 vs 1.18.4  

## 第1类 vs 第2类

见 `docs/ocmar/plans/2026-07-26-slim-state-message-repair.md` 与  
`docs/ocmar/reports/2026-07-26-ocdroid-class2-handoff-prompt.md`。

