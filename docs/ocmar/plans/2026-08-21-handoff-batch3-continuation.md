# oc-slimapi 交接文档（HANDOFF）— 2026-08-21

> **交接对象**：接手继续执行的 agent。本文档是唯一权威交接载体；与其它文档冲突时以本文档 + 批次三计划（`docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`）为准。
> **交接时刻状态**：批次三 Wave 1 已闭环发版 v4.6.1 并部署本机；原指令为「Wave 1 完成后暂停，等用户通知再开 Wave 2」，**用户于移交时修订为：接手后立即按批次开展全部任务，直到全部完成**（2026-08-21 12:54 CST 同步入盘；据此 §5.1 的暂停令视为已解除）。

---

## 1. 项目坐标（30 秒上下文）

- **仓库**：`/home/mar/personal_projects/oc-slimapi`，分支 `main`，HEAD `406bbf3`（release: v4.6.1，已 push main + tag `v4.6.1`）。
- **角色**：ocdroid（Android 客户端）↔ opencode（上游 server）之间的 Python 省流 sidecar。FastAPI + httpx + orjson + uvicorn，src layout，包名 `oc_slimapi`。
- **版本双轨**：包版本 4.6.1（semver）；wire 版本 (3,4) 双版本窗口（`src/oc_slimapi/versioning.py`，`?v=` selector 协商）。
- **上游对照源码**：`/home/mar/personal_projects/ocdroid/opencode-src/current`（→ v1.18.18）。改 sidecar 行为前若涉及上游语义，先读上游源码（AGENTS.md 有路径表）。
- **必读**：`AGENTS.md`（入口索引）、`docs/specs/v3-contract.md` + `docs/specs/v4-contract.md`（wire 契约权威）、`CHANGELOG.md`（行为变更记录）。
- **硬规则**：任何 Python/契约改动后必须 `./scripts/check.sh` 全绿才算完成；发版只走 `./scripts/release.sh <patch|minor|major>`；wire 行为变更必须记 CHANGELOG；SQLite 绝无写入（只读投影 `mode=ro` + `query_only` 双保险）；多 agent 并行严守文件写域。

## 2. 历史脉络（为什么有这些任务）

1. **2026-08-20 全面审计**（方案 `docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md`，产物 `docs/audits/2026-08-20/`）：173 条发现（P1×4/P2×19/P3×150），总报告 `04-final/AUDIT-REPORT.md`，整改池 `04-final/refactor-backlog.md`（23 项冻结评分排序）。
2. **批次一**（v4.5.0，2026-08-21）：12 项发现修复（含 P1 F-001 幽灵事件名、F-006 merged 预算、F-004 deploy crash-loop）。
3. **批次二**（v4.6.0，2026-08-21）：六项 owner 裁决 R-1a/R-1b/R-3/R-4/R-5/R-6 落地（q/p 决议族直推、metrics droppedEventsByType、部署回环默认、CLIENT_CHANGES v4 迁移指南、两份评估文档）。
4. **批次三**（进行中）：总计划 `docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`（rev-cgpt 门控 5.8→9.4 PASS），四阶段：
   - **Wave 1** ✅ 已完成 → v4.6.1（commit `095e632` + `406bbf3`）
   - **Wave 2** ⏸ 挂起等用户通知
   - **Wave 3** ⏳ 排在 Wave 2 后
   - **v4 分支**（v3 完全移除）⏳ 前置条件已解锁（见 §4）

## 3. Wave 1 已完成内容（v4.6.1，勿重做）

四泳道全部回收并合入：死码清扫 −460 行（F-018 respx 依赖+test_upstream.py 整文件删、F-024 七项、F-138 三符号）、F-030 TODO 解除（camelCase 定论）、B2-6 `OC_SLIMAPI_MAX_MESSAGE_BYTES >= 1` 启动校验、F-122 resync reason 冻结集 + AST gate 测试（`tests/test_resync_reason_gate.py`）、F-010/F-214 TimeoutStopSec 15→60 + 全链算术注释 + operations.md shutdown 语义重写、14 文件文档漂移批（(3,4) 口径统一/env 全量覆盖/allowlist runbook/契约 §8 三码补录）。

收尾验证均通过：check.sh 全绿（3343 passed）、路由↔文档 54 条一致、CHANGELOG [4.6.1] 落盘（顺带修复了 [4.6.0] 错插 [4.5.0] 之后的顺序问题）、本机服务已更新（health `?v=4` 正常、仅 127.0.0.1:4097 LISTEN、非回环拒绝、journal 零 error）。

已知非阻塞残留（Wave 1 收尾时记录，归后续写域）：`src/oc_slimapi/sse/registry.py:305` docstring 仍提及已删的 `_busy_sids`；`tokenstream/subscriber.py:449`、`routes/events.py:211`、`routes/token_stream.py:290` 三处 resync 字面量未常量化；`src/skeleton.py:256` 历史引用；`docs/operations.md:329` 冗余对照句。

## 4. 待办任务全景（按优先序）

### 4.1 Wave 2：性能 offload（需用户通知后开工）

计划 §「Wave 2（:71）」：transform/gzip 工作线程池卸载（F-201/F-271/F-202 族），缓解维护排水 30s 尾延迟（Wave 1 的 TimeoutStopSec=60 已把该链纳入 systemd 覆盖，Wave 2 是根治）。单泳道，产出 v4.6.2（minor）。**门控要求**：实施前须 rev-cgpt 评审（门禁 9.0，需先 `review_prep` 制备 rid 注入）；字节等价性验证（计划 N3 冻结了等价门）。

### 4.2 Wave 3：大拆分 + B12（Wave 2 后）

计划 §「Wave 3（:82）」：上帝文件拆分（`sse/tokenstream/hub.py` 2190 行、`routes/messages.py` 1643 行、`config.py` 1158 行）+ B12（v4 分支开工门）+ 上述 Wave 1 残留清扫（这些文件的写域届时打开）。三泳道并行，产出 v4.7.0。**注意**：B12 是 v4 分支的开工门而非 merge 门（门控 R1-B3 裁定）。

### 4.3 v4 分支：v3 全面移除（Wave 3 后从 main HEAD 拉出）

计划 §「v4 分支（:91）」：**不发版不部署**，merge 回 main 后才 major 发版。内容：wire 面 v3 selector 移除、`ACCEPTED_CLIENT_VERSIONS` 收窄、契约 v3 视图退役标注、测试 v3 面清理。

**关键进展（2026-08-21 04:19Z ocdroid 通报）**：ocdroid **v4.1.0 已发版**（tag 已 push）——客户端全量 V4-only：`?v=4` 钉死、v3 fallback 代码全删、连接硬门禁。唯一存量 v3 流量来自旧版 3.2.0 客户端的 `/slimapi/sessions/status` 轮询，将随用户升级自然清零。

**merge 门（门控 R1-B4 冻结判据，勿自拟）**：对齐 `docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md` §6 的 owner 裁阈值 + **SSE active 订阅中 v3 数 = 0** + 直连面核查。v3 退役五阶段路线图也在该评估文档。

### 4.4 无需授权的观测项（接手后可立即做）

**v3 流量观测**（v3 退役 Phase 1）：生产 access log 落 `~/.local/state/oc-slimapi/logs/access-YYYY-MM-DD.jsonl`（RETAIN_DAYS=3），按 `wireVersion=="3"` 过滤聚合（方法见 `docs/manual/traffic-accounting.md`；也有 `/slimapi/metrics.traffic` 端点）。基线（交接前实测）：v3 流量全部来自 ocdroid 3.2.0 存量（混合态），Top = `/slimapi/sessions/status` 轮询；SSE 281 路全 v4。观测达标（v3→0 且持续）即可宣告 merge 门第一条件满足。**注意**：观测结论写入退役评估 §2.1 前例（commit `3573b97`）。

### 4.5 待用户裁决项（不得自行决定）

- **/slimapi/directories allowlist 契约-实现漂移**：v4-contract §5.2:214 声称 allowlist 过滤，实现零命中（批次二 Lane D 新发现，详见 `docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`）。三选一：修实现 / 修契约 / 按 R-1b 观测先行。评估文档推荐「观测先行」路径一。
- **R-1b allowlist 提升全局门**：三路径评估已在上述文档，推荐路径一（观测先行）。
- **R-6 v3 退役推进节奏**：五阶段路线图待 owner 确认 Phase 2/3 时点（Phase 1 观测可先行，见 4.4）。

### 4.6 低分值 backlog（可并入后续泳道）

`docs/audits/2026-08-20/04-final/refactor-backlog.md` 23 项中未入批次一/二/三的其余项 + `docs/ocmar/plans/2026-08-21-follow-up-backlog.md` 的批次三重构项（W2/W3 已吸收进总计划，剩余为长期项）。不阻塞任何主线。

## 5. 执行纪律（接手 agent 必须遵守）

1. **暂停令仍有效**：用户明示「Wave1 完成后暂停，等我通知再开 W2」——未获用户明示前不开 Wave 2/3/v4 分支；观测项（§4.4）与用户已裁决项除外。
2. **门控流程**：每个 Wave 实施计划改动前经 rev-cgpt 评审（门禁 9.0）；派 rev-cgpt 前**必须先 `review_prep`** 制备 binding 并把 rid 注入其上下文（它是不可信 reviewer，git_ro 单通道，缺 binding 会 NO_BINDING 废一轮）。
3. **写域互斥**：并行泳道文件集必须先机器校验交集为 ∅ 再派发（Wave 1 曾因 W1-B/W1-D 重叠合并为 W1-BD）。
4. **泳道回收**：每条泳道回收后核对写域合规（`git status --porcelain` 全在白名单并集内）+ 定向 pytest 统计行；全部回收后编排者做：CHANGELOG 条目 → `./scripts/check.sh` 全量 → commit → `./scripts/release.sh` → push → 本机服务更新（`.venv/bin/pip install -e .` + `systemctl --user restart oc-slimapi`）→ 四件套终验（ss 监听 127.0.0.1 且 pid==MainPID / health `?v=4` / 非回环连接拒绝 / journal 零 error）。
5. **flaky 处置先例**：`tests/test_actions.py::test_drain_deadline_preserves_partial_output` 是时序敏感 flaky（全量偶发失败、隔离复跑即过）——遇失败先隔离复跑判定，勿立即归因回归。
6. **契约修订模式**：加性变更用「[X.Y.Z] 追加：…」行内条目（先例：v3-contract §8 第 4 条、§9 条目 4）；v3-contract 非用户明确要求不改正文（行内括注「严格前缀追加」模式除外）。
7. **跨会话协作**：ocdroid 侧主会话 `ses_fe200174cffedlvmqOD5rgJBGa`（v4 迁移会话）可用 `session_send` 互通；本仓评审/泳道历史会话见 Background Job Board（rev-1=rev-cgpt、fix-1/fix-2/fix-4 可复用续作）。
8. **简单文书任务**可派 fixer-clm（用户已授权先例：operations.md patch 合入）；实质代码改动派 fixer-glm 系。

## 6. 快速命令速查

```bash
./scripts/check.sh                          # 改动后必做（pytest + 路由↔文档 + compileall）
systemctl --user restart oc-slimapi         # 本机服务重启
journalctl --user -u oc-slimapi -f          # 应用日志
ls ~/.local/state/oc-slimapi/logs/          # access log + traffic snapshot
./scripts/release.sh patch                  # 发版（唯一合法途径）
```

## 7. 交接清单核对

- [x] 批次三总计划（Wave 2/3/v4 分支全部细节）：`docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`
- [x] v3 退役五阶段路线图 + merge 门判据：`docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md`
- [x] allowlist 全局门评估 + directories 漂移：`docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`
- [x] 审计总报告 + backlog：`docs/audits/2026-08-20/04-final/`
- [x] 后续待办池：`docs/ocmar/plans/2026-08-21-follow-up-backlog.md`
- [x] 当前 HEAD `406bbf3` = v4.6.1，工作区干净，本机服务运行 v4.6.1
