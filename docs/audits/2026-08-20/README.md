# oc-slimapi v4 全面审计（2026-08-20）— rev10 执行产物根

## attempt 元数据

- 方案：`docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md`（rev10，无人值守执行版）
- BASELINE_HEAD：`0b836e78c5de62d0c73b8593bf62c6650043dedf`（短 `0b836e7`）
- 判定：**新建分支**（`docs/audits/2026-08-20/` 不存在 → 排他创建；U0.0 symlink 预检全部通过，无违例）
- AUDIT_ROOT：本目录；attempt-id = `primary`
- 时间窗起始：2026-08-20T22:46+08:00
- 命名空间（`manifest/namespaces.json`）：
  - probes = `/tmp/opencode/probes/<HEAD>/primary`
  - runtime-cache = `/tmp/opencode/runtime-cache/<HEAD>/primary`
  - baseline-snapshot / venv-source：惰性启用（初始 unused）
- 脏基线判定：**dirty**（untracked 非忽略输入 1 个：方案文件自身；无 tracked 修改、无墓碑）→ baseline-snapshot 副本启用（见 U0.2(b)）
- 证据引用约定：所有发现/报告引用 `file:line` 均相对 BASELINE_HEAD=0b836e7 快照

## 进度清单

### Phase 0
- [x] U0.0 安全预检与 AUDIT_ROOT 选定（symlink 链全通过；新建分支；骨架已建）
- [x] U0.1 快照锚点（HEAD/porcelain/log5 → manifest/baseline-head.txt）
- [x] U0.2 工作目录/基线冻结/ignored 基线（probes+runtime-cache 命名空间、run_isolated、freeze 三件套）
- [x] U0.3 环境自检与运行模式持久化（三检 → runtime-mode.json provisional）
- [x] U0.4 绿色基线（check.sh → logs/check-baseline.txt；runtime-mode → final）
- [x] U0.5 耗时登记
- [x] U0.6 机器可读 inventory（gen_inventory.py → 01-explore/inventory.json）
- [x] U0.7 上游与部署文件自检
- [x] U0.8 时间戳标记
- [x] U0.9 symlink 验证记录归档
- [x] U0.10 hash manifest 归位确认
- [x] U0.11 计数复核命令块
- [x] U0.12 阻塞清单初始化
- [x] 00-baseline.md 汇总

### Phase 1
- [x] E1 文件级精读 → 01-explore/file-cards.md
- [x] E2 路由普查 → route-census.csv/md + expected-keys.csv + applicability 表
- [x] E3 配置普查 → config-census.md
- [x] E4 状态机清单 → state-machines.md
- [x] E5 数据流追踪 → dataflows.md
- [x] E6 文档与部署面精读 → docs-notes.md
- [x] E7 测试普查 → test-census.md
- [x] E8 上游源码对照 → upstream-notes.md
- [x] 自由探索日志（§9 探索将在 Phase 2/3 持续追加） → exploration-log.md（§9）

### Phase 2
- [x] A1 v4 完备性矩阵 → D01
- [x] A2 v3 可淘汰性 → D02
- [x] A3 legacy/透传遗留 → D03
- [x] A4 契约清晰性与完整性 → D04
- [x] A5 并发/singleflight/缓存 → D05
- [x] A6 SSE 状态机 → D06
- [x] A7 dbaux → D07
- [x] A8 安全（三入口）→ D08
- [x] A9 性能与资源 → D09
- [x] A10 代码质量 → D10
- [x] A11 模块化 → D11
- [x] A12 测试质量 → D12
- [x] A13 可观测性与运维 → D13
- [x] A14 文档漂移 → D14
- [x] A15 发布与供应链 → D15

### Phase 3
- [x] V0 边界重验（每 Phase 边界；manifest/phase-verify/）
- [x] V1 全量复核
- [x] V2 自我证伪（P0/P1 双轨）
- [x] V3 一致性冲突消解
- [x] V4 基线复跑
- [x] V5 数字定稿
- [x] V6 覆盖自查

### Phase 4
- [x] 04-final/AUDIT-REPORT.md
- [x] 04-final/v3-retirement-plan.md
- [x] 04-final/refactor-backlog.md
- [x] 04-final/test-gap-matrix.csv（+ validation.txt）
- [x] 04-final/verification-log.md
- [x] §10.2 终止条件自查（2026-08-21 全过：manifest/交付物/六脚本 hash/validators/git/C13/V0×5）

## 终态摘要（2026-08-21）

- **审计完成**：四阶段全 DONE，零 BLOCKED、零 BLOCKED-STUB、零 coverage-degraded。
- 发现 173（confirmed 170 / refuted 3）：P1×4（F-001 幽灵事件、F-004 deploy crash-loop、F-006 merged 退化、F-251 E-II 无认证面[部署边界未验证]）+ P2×19 + P3×150。
- 机器产物全 VALID：census 54 行 / applicability 337 行 / expected-keys 580 / gap-matrix 580（双向集合差空）。
- check.sh 首尾绿→绿（3316 passed ×2）；V0×5 全过；工作区零污染（C13 ignored 零新增）。
- 入口：04-final/AUDIT-REPORT.md（总报告）→ §8 整改 backlog（refactor-backlog.md Top23）。

## blockers（同步于 01-explore/exploration-log.md）

（空——全程无阻塞）
