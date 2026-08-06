# 交付总结报告：reliability-hardening

> **日期**：2026-08-06
> **Workflow slug**：reliability-hardening
> **Base**：`a6cd924ba71ce406a189ceb62c3d0024cc21cafc`
> **状态**：✅ 全部 verified + final review APPROVED + fresh verifier EXIT=0

---

## 一、需求回顾

**原始一句话**：按 oracle 架构评审的 P0 + 快速 P1 改进 backlog 开展（"按上述需求开展"）。

**明确后 spec 要点**（6 项）：
1. messages list + full 端点非 list/dict body → 503 守卫〔P0〕
2. catch-all 反代 `httpx.RequestError` → 503 + CHANGELOG〔P0〕
3. 修四处文档语义漂移〔P0〕
4. 删 capabilities 死代码〔P1〕
5. sessions list 接 `read_with_cap` → 413〔P1〕
6. deploy unit 补 `MemoryMax=384M`〔P1〕

**YAGNI 边界**：不 bump wire 版本、不动 timeout、不加应用层鉴权、不加 CI/上游 pin 测试、不拆 tokenstream。

---

## 二、方案摘要

**文件结构**（13 文件，346 insertions / 263 deletions）：

| 文件 | 职责 | 改动 |
|---|---|---|
| `src/oc_slimapi/routes/messages.py` | list+full 守卫 | 元素级 dict 检查 + 路由 catch 合并 |
| `src/oc_slimapi/transform.py` | full worker 守卫 | 非 dict body → ValueError |
| `src/oc_slimapi/proxy.py` | catch-all 错误映射 | `client.send` 包 `httpx.RequestError` → 503 |
| `src/oc_slimapi/routes/sessions.py` | list 流式化 + 元素守卫 | `read_with_cap` + mid-stream 修复 + 标量元素守卫 |
| `deploy/oc-slimapi.service` | systemd 内存限 | `MemoryMax=384M` |
| `CHANGELOG.md` | 行为变更记录 | 3 条 Fixed 条目 |
| `docs/specs/INTERFACE_MAP.md` | 接口追踪 | 4 处语义同步 + sessions 413 + catch-all/api_version 修正 |
| `docs/specs/design-v2.md` | 设计文档 | smoke 保留表述 |
| `src/oc_slimapi/capabilities.py` | 死代码 | **删除**（-125 行）|
| `tests/test_capabilities.py` | 死代码测试 | **删除**（-88 行）|
| `tests/test_messages_routes.py` | 守卫测试 | +5 例（list 3 + full 2）|
| `tests/test_proxy.py` | catch-all 测试 | +2 例（ConnectError + ReadTimeout）|
| `tests/test_sessions_routes.py` | sessions 测试 | +3 例（413 + mid-stream + 标量元素）|

**关键设计决策**：
- 守卫放在 worker（数据接触点），路由层把 `(JSONDecodeError, ValueError)` 合并映射 503
- catch-all except 只覆盖 `send()` 调用，不含 mid-stream（走 finally aclose）
- sessions list 流式化后 mid-stream `ReadError` 用内层 `except httpx.RequestError` 覆盖 `aread()` + `read_with_cap()`
- grilling 扩展 Task 1 覆盖 full 端点（oracle 漏报），并修正了 full 端点实际行为（200 透传而非裸 500）

---

## 三、执行过程（从 ledger 读）

| Task | 实现 | Review | Attempt | 关键变更 |
|---|---|---|---|---|
| T1 messages 守卫 | fix-1 (fixer) | ora-1 ✅ | 1 | 元素级 dict 守卫；3 处 plan 偏离（均批准） |
| T2 catch-all 503 | fix-2 (fixer) | rev-2 ✅ | 1 | send() 包 RequestError；import httpx 修正 |
| T3 删 capabilities | orchestrator | rev-2 ✅ | 1 | git rm；零 import 残留 |
| T4 sessions 流式化 | fix-4/fix-5 (fixer-clm) | rev-2 ✅ | 3 | read_with_cap + mid-stream ReadError 修复 + 标量元素守卫 |
| T5 deploy MemoryMax | orchestrator | rev-2 ✅ | 1 | 单行 MemoryMax=384M |
| T6 文档同步 | orchestrator | rev-2 ✅ | 3 | 4 处同步 + 路由顺序 + 回调名 + 残留表述 |

**Final review**：rev-2 (rev-ogpt) 首审 NEEDS_FIX (7.8/10, 4 Important) → 修复 4 项 → rev-4 (rev-gpt) 重审 **APPROVED (9.2/10)**。

---

## 四、测试结果

- **Verifier**：`_priv-verifier`（fresh，live rerun `-p no:cacheprovider`）
- **结果**：`EXIT=0 FAILURES=0`
- **日志**：`.ocmar/workflows/reliability-hardening/verify-final.log`
- **测试数**：1019 passed（基线 1024 − 14 删 capabilities + 8 新增 + 1 标量元素 = 1019，算术自洽）
- **路由↔文档一致性**：12 条 /slimapi 路由均已在 INTERFACE_MAP 记录 ✅

---

## 五、评审结论

| Gate | Reviewer | Verdict | 分数 | Critical | Important | Minor |
|---|---|---|---|---|---|---|
| review-task-1 | ora-1 (oracle) | APPROVED | — | 0 | 0 | 4 |
| review-task-2 | rev-2 (rev-ogpt) | APPROVED | — | 0 | 0 | 2 |
| review-task-3 | rev-2 (rev-ogpt) | APPROVED | — | 0 | 0 | 0 |
| review-task-4 | rev-2 (rev-ogpt) | APPROVED (R2) | — | 0 | 0 | 2 |
| review-task-5 | rev-2 (rev-ogpt) | APPROVED | — | 0 | 0 | 0 |
| review-task-6 | rev-2 (rev-ogpt) | APPROVED (R3) | — | 0 | 0 | 0 |
| **final-review** | **rev-4 (rev-gpt)** | **APPROVED** | **9.2** | **0** | **0** | **5** |
| final-verification | _priv-verifier | PASS | — | — | — | — |

---

## 六、最终状态

**Working-tree 改动**：13 文件，346 insertions / 263 deletions（未 commit，ocmar 默认）。

**是否 commit**：否（用户未显式要求 commit；工作树改动保留供 review）。

**后续建议**：
- P2 backlog（未在本 workflow 范围）：ruff + mypy + 清理 pytest warnings；tokenstream/hub.py 拆分评审；上游契约 pin 测试包；GitHub Actions CI
- sessions 413 流量账本 upIn 低估（Minor，rev-gpt 确认可接受）
- aread() 错误 body 无 cap（Minor，既有模板残留）

**已知遗留**（5 个 Minor，final review 确认可接受）：
1. sessions 超限分支 `stash_up_in` 在 413 之后，upIn 低估
2. sessions 错误状态码 `aread()` 无 body cap
3. `check_routes_doc.py` 只校验存在性不校验语义
4. catch-all mid-stream 异常的资源关闭依赖 Starlette 调度（既有残留）
5. verifier 只读权限无法独立复跑 check.sh（证据来自 workflow）

---

## 七、可审计引用

- **ocmar-state ledger**：`.ocmar/workflows/reliability-hardening/state.json`
- **Spec**：`docs/ocmar/specs/2026-08-06-reliability-hardening-design.md`
- **Plan**：`docs/ocmar/plans/2026-08-06-reliability-hardening.md`
- **Verifier 日志**：`.ocmar/workflows/reliability-hardening/verify-final.log`
- **Final review**：rev-4 (rev-gpt, ses_0294ca58fffet0P6ChJNlxaR2A)
- **Gates**：parallel-admission(SERIAL) + review-task-1~6 + final-review + final-verification
