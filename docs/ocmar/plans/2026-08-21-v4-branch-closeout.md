# v4 分支收尾报告（V2a+V2b 全链闭环）— 2026-08-21

> 接续 `2026-08-21-handoff-v4-branch.md`。工程面全部完成，merge 门余两项 owner 裁定。**本报告即 owner 终审输入。**

## 1. 提交链（main..v4，13 commits，工作树 clean）

| commit | 内容 |
|---|---|
| `04c973a` / `bc72627` / `f6a4c49` | （继承）B6 机械防线 / F-126 契约自包含 / handoff 文档 |
| `bbf92ea` | **V2a**：版本窗 (3,4)→(4,4)——src 4 文件 + 测试 35 文件 + 契约四件 + 双金样重录（ETag wire=v4） |
| `0351280` | V2b：test_sse_replay_wire v3 半区删 + R3 族 v4-only 自锁重建 |
| `ed3d201` | V2b：selector v3 准入死代码清除（tests-first 第一刀） |
| `9851f61` | V2b：A2a — 8 红 v3 测试文件清理（28 红全清） |
| `39ed840` | V2b：A2b — 12 守护锁文件拆除（40 守护测试，全量首绿） |
| `30b6262` | V2b：v4-contract 现行规范段 v4-only 化 32 处（评审 BLOCKER#1 整改） |
| `8c7fe03` | （并行会话）provider-error 特性：digest lastError / session.error 结构化字段（§7.6，自有评审 9.6 PASS） |
| `ee36f73` | **V2b(src) 二阶段**：路由 v3 半区物理拆除 + wire_view_from_scope 恒返 4（+325/−985，24 文件） |
| `b6bf72d` | docs：CHANGELOG [Unreleased] 校准至终态 + §14 expand 生效注记 |
| `aec417e` | test：pgrep 锚定加固（防外部进程误匹配假红） |

## 2. 质量门（全过）

- `./scripts/check.sh`：**3481 passed / 0 failed** + 54 路由↔INTERFACE_MAP 一致 + compileall ✓（快照 aec417e）
- 独立评审（rev-sgpt，rid=rv_20260821-140309）：首审 7.4 FAIL → 三项整改 → 复审 **9.4/10 PASS**，零 BLOCKER/MAJOR，1 MINOR（历史命名技术债，不阻塞）
- 金样 JSON：W2/N6 双金样已重录（wire=v4 域标记），此后未再改动
- 保留项裁量均获评审背书：hub 双栈（底层能力，非 v3 入口）、观测 v3 冻结命名（§9 schema）、`_read_passthrough` wire_view=3（ETag 域标签，金样冻结字节）、sessions status lease（并发机制非版本面）、read_groups gate-off 回落腿（§3.3 readiness 语义）

## 3. merge 门五条件终态

| # | 条件 | 状态 | 证据 |
|---|---|---|---|
| ① | ocdroid 迁移完成 | ✓（handoff 已裁定） | ocdroid v4.1.0 已发 |
| ② | 观测判据 | **部分满足，待 owner** | SSE v3 请求 = 0 连续两日（08-20/08-21）✓；v3 占比 99.7%→69.5%→36.8%（08-19/20/21）持续降但非零——残留全部为 ocdroid 旧版本设备（clientVer 3.2.0；anonymous 与 oc-webui 通道 08-21 v3 = 0）；**阈值与窗长未裁** |
| ③ | 直连/匿名残留核查 | ✓（时点快照） | `ss` 现查 :14096 零建立连接；:4097（sidecar）2 条、:4096（上游）10 条；匿名流量 395 req/日且 v3 = 0 |
| ④ | B12 完成 | ✓ | 测试侧 Wave 3 + 契约侧 F-126（bc72627），守护锁已随 V2b 拆除 |
| ⑤ | owner 终审 | **待 owner** | 本报告 |

## 4. 待 owner 裁定项

1. **② 占比阈值与窗长**：当前事实基线 = SSE v3 已零两日；v3 请求占比 36.8%（08-21，全部 ocdroid 旧设备）。若裁定「SSE v3=0 连续两日 + 非 ocdroid 通道 v3=0」即为达标，则 ② 现已满足；若要求占比绝对值（如 <5%），需等待设备侧升级渗透。
2. **⑤ 终审 + 发版放行**：过门后动作链（编排者执行）= merge v4→main → CHANGELOG `[Unreleased]`→`[5.0.0] - YYYY-MM-DD` → `./scripts/release.sh major` → 本机部署。用户已指示错误展示特性（`8c7fe03`）随本次一并发版。

## 5. 发版注意

- release.sh 对 v4 分支的 preflight 拒绝（B6，`04c973a`）在 merge 回 main 后自动失效，无需改代码。
- v3-contract.md 已挂退役存档章、INTERFACE_MAP 已 v4-only 化——发版后三者与 main 现态一致，无追加动作。
- 已知 MINOR 技术债（不阻塞）：src 内 ~120 处历史注记/命名（`V3_DIRECTORY_STATE_KEY`、skeleton `wire_view: int = 3` 默认值等），留独立清理任务。
