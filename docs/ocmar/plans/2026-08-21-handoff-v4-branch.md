# oc-slimapi 交接文档（HANDOFF）— v4 分支执行中（2026-08-21 17:30）

> **交接对象**：接手继续执行的 agent。本文件是唯一权威交接载体；与其它文档冲突时以本文件 + 批次三计划（`docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`）为准。
> **交接时刻状态**：批次三 Wave 2/3 已在 main 全部闭环（v4.6.2 / v4.7.0，已发版 push 部署本机）；**v4 分支已拉出并推进两步**；V2a（版本窗收窄）**进行中——src + 35 个测试文件已改但未提交**，会话因 5 小时 agent 使用上限中断（2026-08-21 ~17:23 派发被拒；限额 19:36:23 重置，Bash 主会话不受限）。接手后第一件事：`git branch --show-current` 确认在 **v4**，核对下述 WIP 状态后继续。

---

## 1. 仓库坐标与版本双轨

- 仓库 `/home/mar/personal_projects/oc-slimapi`；**当前 checkout = `v4` 分支**，HEAD `bc72627`（F-126 契约自包含化）；main 于 `98a4d20`（release v4.7.0，本机服务部署态 = v4.7.0，健康检查 `accepted_client_versions:[3,4]`）。
- 包版本 v4.7.0；wire 版本 (3,4)——v4 分支目标 = 5.0.0 起 v4-only（major，merge 门通过后才由编排者在 main 发版）。
- 上游对照源码：`/home/mar/personal_projects/ocdroid/opencode-src/current`（→ v1.18.18）。
- 必读：`AGENTS.md`、`docs/specs/v3-contract.md` + `docs/specs/v4-contract.md`、`CHANGELOG.md`、批次三计划、`docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md`（merge 门/Phase 路线）。

## 2. 已完成（勿重做）

1. **Wave 2 → v4.6.2**（`2b816f4` + release `7c9192f`）：messages/read-group 响应尾部下放 worker（F-201/F-271/F-202）+ N3 金样（`tests/golden/offload-baseline-v1.json` 29 case）+ F-203..206 豁免记录；设计 `docs/ocmar/reviews/2026-08-21-wave2-offload-design.md`（评审 8.7→9.5 PASS）。
2. **Wave 3 → v4.7.0**（`09275e5`/`91fbf72`/`8e964d8` + release `98a4d20`）：W3-1 hub 五拆（Mixin，co_code 字节级自证）、W3-2 messages 包化（含 `scripts/check_routes_doc.py` 升级——rglob + 包 `_router.py` prefix 回退）、W3-3 `_aggregate_fanout.py` 共享提取；B12 测试侧完成（12 双视图 golden 化 + 106 函数三分处置：①30 改写 / ②37 守护网 / ③39 观测维度；白名单 `docs/ocmar/plans/2026-08-21-batch3-b12-whitelist.md`，`rg -l 'v=3|"3"' tests/` 43 文件与白名单逐一相等）；N6 金样 `tests/golden/refactor-baseline-v1.json`（**17 case**，含 token_frames 4 帧基线 + aggregate discovery/truncated 真触发 + 双态守卫）；设计 `docs/ocmar/reviews/2026-08-21-wave3-refactor-design.md`（评审 8.1→8.6→9.5 PASS）。
3. **金样体系**（此后一切 wire/refactor 判断的字节级锚）：
   - W2 金样：`OC_SLIMAPI_TEST_RECORD_GOLDEN=1 pytest tests/test_offload_equivalence.py -k golden`
   - N6 金样：`OC_SLIMAPI_TEST_RECORD_REFACTOR_GOLDEN=1 pytest tests/test_refactor_equivalence.py -k golden`
   - 两者均为 hashseed=0 子进程录制 + 默认回放；**金样 JSON 是 main 上的真门**。
4. **v4 分支基线**（两次 commit）：
   - `04c973a` B6 机械防线：`scripts/release.sh` 对 v4 分支非零退出（"v4 branch: merge gate not passed"）+ `docs/operations.md` 部署规则（仅 main HEAD + exact-match tag，v4 拒绝）。
   - `bc72627` **F-126 v4-contract 自包含化（v4 分支第一动作，done）**：13 处「继承/沿用 v3」规范引用清零，`v4-contract.md` 719→860 行（新增 §10.1 基线路由表 / §10.2 消息投影基线 / §14.1-14.5 expand 正文化）；残留 39 处 "v3" 字面全分类：**6 处窗口表述待 V2a**（:4/:15/:16/:347/:349 及 §0.3/§9.4）、7 历史注记、12 对比性、14 状态性。
5. **v3 退役观测**（评估 §2.1 注记，`520a3b5`）：v3 占比 08-19 99.7% → 08-20 69.5% → 08-21 49.1% 且 **SSE active v3 = 0 连续两日**；08-21 v3 流量 100% 归属 ocdroid 3.2.0 存量。

## 3. 当前 WIP（V2a 进行中——未提交，接手先核对）

`git status --porcelain`：**src 4 文件 + tests 35 文件已改（未提交）** = V2a 版本窗收窄的中间态：

- ✅ **已做**：`versioning.py`（`ACCEPTED_CLIENT_VERSIONS (4,4)`、注释收敛）、`selector.py`（`?v=3`/无 v → 400 `unsupported_version supported:[4]`；目录梯子收敛为单一现行语义）、`routes/health.py` + `routes/versions.py`（输出 v4-only 面）；35 个测试文件翻转（断言 3→4 / 400 期望翻转）。
- ✅ **一致性已验证（2026-08-21 17:2x 实测）**：已改 35 文件定向跑 = **1288 passed**；**17 failed 全部集中在两个 v3 守护网文件**（属 V2b 拆除对象，勿改）：
  - `tests/test_access_log_v3_fields.py` ×4（v3 行期望 200 现 400——③观测维度，V2b 改/删）
  - `tests/test_sse_replay_wire.py` ×13（v3 字节锚 + R3 交叉版本族——②守护网，V2b 删 v3 半区）
- ❌ **未做（V2a 剩余，接手继续）**：
  1. v4-contract §0.3/§9.4 + 6 处窗口表述正式修订（"(3,4) 永久双版本" → "5.0.0 起 v4-only"，注明 2026-08-21 修订 + owner 方向指令）；
  2. v3-contract.md 头部退役章（正文零改动，存档地位不变）；
  3. INTERFACE_MAP.md 全局头 v4-only；
  4. CHANGELOG 顶部新增 `## [Unreleased]` 节（B5：恒用 [Unreleased] 无 -draft 后缀；merge 后由编排者转 `## [5.0.0] - YYYY-MM-DD`）；
  5. **双金样测试请求 `?v=3` → `?v=4` 并重录金样**（ETag wire=v{view} 域标记 ⇒ ?v=4 的 ETag/字节必变 ⇒ 必须重录；金样 JSON 由编排者 RECORD，测试代码改 `?v=4` 后**先录后验**）；顺序：**先**把 `tests/test_offload_equivalence.py` 与 `tests/test_refactor_equivalence.py` 中的 `?v=3` 请求改 `?v=4`（read-group/vcs/session-single 场景），**然后**编排者按 §2.3 命令重录两份金样，再全量跑。
  6. 全量 `./scripts/check.sh` 绿（届时 17 红清单应只剩 V2b 范畴，由 V2b 收）。

## 4. 待办（V2a 之后，按序）

### V2b：v3 面物理拆除 + 测试清理（计划 v4 分支节 :95；v3-retirement-plan §3.1 B1-B11）
- src：v3-only 分支逻辑物理删除（selector 残留 v3 面、providers v3 passthrough、v3 ETag wire=v3 validator 域、SSE v3 半区如握手预填/blanket resync——**注意**：SSE v3 半区在 tokenstream/events/read routes，W3-1 拆分后其归属模块已变，按符号定位）；`routes/versions.py` caps["3"] 面删除；约 12 路径 400-600 行。
- tests：上述 17 红（test_access_log_v3_fields ③改写为 v4-only 行断言或删、test_sse_replay_wire ②删除 v3 半区+重建 R3 交叉版本族为 v4-only 自锁）；B12 白名单 ②类文件随拆（test_v3_rawbody_regression 字节锚／test_v3_envelope／test_v3_etag_domain／test_v3_sse_meta／test_health_dual_view 的 v3 半区）；C1a/C1b 双视图文件的 v3 半区删除；全仓 v3 字面清零（白名单文档标注完成态）。
- 契约：v3-contract 退役章已在 V2a 加（此处不重复）。
- 验收：分支上全量 check.sh 绿 + `rg -n "v3|v=3|\"3\"" src/` 只余历史注记类 + 白名单更新。

### v4 分支收尾
- 分支上全量 check.sh 绿为准入；定期 rebase main（若 main 有新提交）。
- **merge 门五条件（门控 R1-B4 冻结，勿自拟）**：
  ① ocdroid W4 已发版——**✓ 已满足**（v4.1.0，2026-08-21 04:19Z）；
  ② 观测判据：`recordType=="request"` 下 wireVersion v3 占比持续低于 **owner 书面裁定的阈值/窗长（未裁！需 owner 决定）** 且 SSE active v3 == 0（**连续两日满足**，评估 §2.1 注记）+ 分维度查询样例见 `docs/manual/traffic-accounting.md`；
  ③ 非 ocdroid 残留流量核查（匿名消费方 + :14096 直连面退役状态，评估 §7 风险 5）；
  ④ B12 完成——**✓ 已满足**（测试侧 Wave 3；契约侧 F-126 = bc72627）；
  ⑤ owner 终审。
- 全过 → 一次性 merge 回 main → CHANGELOG [Unreleased]→[5.0.0] 转换（**编排者**）→ `./scripts/release.sh major` → 本机部署。
- **禁止**：v4 分支上跑 release.sh / 部署本机（B6 已在 release.sh + operations.md 双保险机械强制）。

### 待 owner 裁决项（不得自行决定）
- merge 门 ② 的**阈值与窗长**（v4-contract §0.3 原文「占比持续低于阈值」——数字 owner 书面裁）。
- allowlist 漂移三选一 + R-1b 全局门（评估 `docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`，推荐观测先行）。
- R-6 节奏（Phase 2/3 时点）。
- config.py 拆分（F-303，P3 未入池；交接 §4.2 提及但计划无此泳道——已如实记录，未做）。
- skeleton `_pick` 键序候选修法（P3 立案，follow-up backlog；会改 rawbody 逐字节基线需 --capture 回填 + review）。

## 5. 执行纪律（接手 agent 必须遵守）

1. **先 `git branch --show-current`**——必须在 v4；main 上的发布命令一律不执行。
2. 门控：每个实施波次前独立评审 ≥9.0（本会话以 general-purpose 只读 agent 等价替代 rev-cgpt，效果与记录在 `docs/ocmar/reviews/2026-08-21-wave{2,3}-*.md`；Wave 2 与 Wave 3 均经多轮对抗评审收敛到 9.5）。
3. 写域互斥：并行泳道先机器校验交集 ∅；本会话因后台 agent 不支持 + 5h 限额按前台串行执行。
4. 金样纪律：**金样 JSON 不手改**；RECORD 由编排者在口径变更时执行一次并记录理由；hashseed=0 子进程模式已固化。
5. CHANGELOG 坑（两次踩过）：新版本节插入时**必须把旧节标题行一并带上**，否则旧节沉底；顺序最新在上；release 前 `grep -n "^## \["` 复核。
6. flaky 先例：`tests/test_actions.py::test_drain_deadline_preserves_partial_output`。
7. 契约修订模式：加性 `[X.Y.Z] 追加`行内条目；v3-contract 正文零改动（V2a 的头部退役章是唯一例外，已授权）。
8. 本机服务保持 main 发布态 v4.7.0；v4 分支代码**永不部署本机**。

## 6. 快速命令

```bash
git branch --show-current                    # 必须 = v4
git status --porcelain                       # WIP 清单（V2a 中间态，见 §3）
./scripts/check.sh                           # 改动后必做（pytest + 路由门 + compileall）
# 金样重录（双金样测试改 ?v=4 后执行一次）：
OC_SLIMAPI_TEST_RECORD_GOLDEN=1 .venv/bin/python -m pytest tests/test_offload_equivalence.py -k golden -q
OC_SLIMAPI_TEST_RECORD_REFACTOR_GOLDEN=1 .venv/bin/python -m pytest tests/test_refactor_equivalence.py -k golden -q
.venv/bin/python -m pytest tests/test_offload_equivalence.py tests/test_refactor_equivalence.py -q -p no:cacheprovider   # 回放
journalctl --user -u oc-slimapi -f           # 服务日志（main 态 v4.7.0）
ls ~/.local/state/oc-slimapi/logs/           # v3 观测数据（merge 门②）
./scripts/release.sh major                   # 仅 merge 回 main 后、编排者执行
```

## 7. 交接清单核对

- [x] main 上 Wave 2/3 全链闭环 commit 链（`2b816f4`...`98a4d20`）+ 双金样体系
- [x] v4 分支现状：HEAD `bc72627` + WIP（§3 状态与 17 红清单已实测）
- [x] F-126 自包含化完成记录 + 残留 39 处分类
- [x] V2a 剩余清单（契约三件 + CHANGELOG [Unreleased] + 金样切 v4 重录）
- [x] V2b 提示词要点（§4）+ B12 白名单衔接
- [x] merge 门五条件 + 待 owner 阈值（未裁）
- [x] 执行纪律（分支/金样/CHANGELOG/评审门/写域）
- [ ]（接手第一动作）`git branch --show-current` 确认为 v4，复核 WIP 与本 §3 描述一致后再继续