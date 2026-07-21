# rev-bgpt 终审纪要 — 体验优先联合方案（GPT-Sol 视角）

> **日期**：2026-07-21  
> **评审对象**：ocdroid `docs/0.11-ux-first-joint-plan.md` + slimapi `docs/ocmar/plans/2026-07-21-ux-first-collab-reply.md` rev 1 + 联合计划  
> **评审者**：rev-bgpt（gpt-5.6-sol，独立 subagent）  
> **结论**：有条件可执行；3 Blocker + 10 Major + 4 Minor。slimapi 已据本纪要产出 collab-reply **rev 2**。  
> **档案用途**：供 ocdroid 侧方案调整后再次提交 rev-bgpt 复审时对照基线。

---

## 总体结论

「体验优先」方向正确，但当前版本不宜作为双方无条件执行基线。最大风险：G6 恢复模型两个未闭合结构性矛盾——413「两半」与「≤4 次」预算冲突；任一 mid 网络失败抛弃已成功结果、迫使整批重下。在修订 U2/U3、固定 `mode=full`、把 F-1 与网络安全恢复为发版前置前，不应宣称「展开可靠 + 弱网省流」闭环。

## Blocker

### B1 — U2「413 两半 MUST」与「≤4 次」预算冲突
- 服务端 413 整请求中止、不返 partial（`messages.py:553-568,604-618`；契约 §7）。
- ocdroid 现只递归前半（`OpenCodeRepository.kt:2294`），后半 residual 失败。
- 二分树最坏 `2N-1=39` 调用，与单动作 ≤4 冲突；singleton 须终态；413 分区与 503 重试预算未分账。
- **修**：契约只写服务端保证；恢复算法归客户端；互操作语言=「最终覆盖全部 ids」。

### B2 — mid 网络失败 → 整 503 丢弃成功 mid（与 P0 省流冲突）
- mid `httpx.RequestError` → `network_failed`（`messages.py:528-572`）；503 优先于 413，`succeeded` 不输出（`messages.py:604-618`）。
- ocdroid 503 整批重试（`OpenCodeRepository.kt:2307`）→ 重复下载；envelope 丢弃 code（`OpenCodeRepository.kt:2265`）无法分类。
- **修**：二选一——(A) mid RequestError 改 envelope 可重试 code（加性 wire）；(B) 不改服务端则撤回「零重复/仅失败 mid 重试」强验收。slimapi 不接受中间态。

### B3 — 展开必须 `mode=full`，不能只「建议」
- skeleton 省 tool output/raw/attachments/error（`skeleton.py:31-63`）；200 仍可能被标 Loaded 却无全文。
- **修**：用户展开 MUST `mode=full`；MockWebServer 断言 URL + 返回真含 omitted。

## Major（精选）

- **M1/S-B**：不应给所有 `upstream_unavailable` 编造固定 Retry-After；透传优先 + 保守最小建议；坏 JSON 不发。
- **M2/U2**：413 恢复是客户端算法，非服务端 wire 保证。
- **M3/U6**：真实 session id 不进 git/log；fixture 拆合成可提交 vs 临时脱敏。
- **M4/S-E**：runtime `git describe` 在 `0.0.0.0` 无鉴权下扩大指纹；改 build 时注入 deployment id。
- **M5**：旧 workplan 标覆盖但正文仍写「尚未进 APK 发版」，执行者会引错章节（slimapi 已修）。
- **M6/F-1**：`/since` 生产正确性风险，须设 v0.11.11 发版硬门禁。
- **M7/S-C**：2–3 合成 fixture 不足以支撑 reasoning 决策；须匿名生产聚合 median/P90。
- **M8**：G6 404 能力缓存须有失效机制（TTL/版本/server.connected）。
- **M9**：placeholder 去除须补退出标准。
- **M10**：去 gate + `0.0.0.0` + 明文叠加，安全收敛在体验叙事被淡化；须保留部署门禁。

## Minor

- **m1**：术语统一（partial / top-level terminal / residual / attempt 三种计数）。
- **m2**：`upstream_http_N` 可重试分类未闭合（5xx/429/4xx）。
- **m3**：too_large 须按端点/code 限定（envelope mid vs 顶层 response_too_large）。
- **m4**：reconfigured 不双发 ≠ 一次连接只冷启动一次（resync+connected 仍可能）。

## 给 ocdroid 的反馈（一句话）

同意体验优先方向，但请先闭合 413 全覆盖预算 + G6 mid 网络失败丢弃成功结果两个前置，并把 `mode=full` 与 F-1 设为 v0.11.11 发版硬门禁。
