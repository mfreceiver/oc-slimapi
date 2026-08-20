# 审计过程验证日志（04-final/verification-log.md）

> 基线 BASELINE_HEAD=0b836e78c5de62d0c73b8593bf62c6650043dedf（0b836e7）；attempt=primary（新建分支）；脏基线快照副本 `/tmp/opencode/baseline-snapshot/0b836e78…/primary/`（261 文件 + COMPLETE）。全部 V 记录时区 +08:00，日期 2026-08-20/21。

## V0 快照边界重验（唯一入口 verify_baseline.py，调用前 hash 校验通过）

| seq | 时点 | ok | manifest/current | new_outside | disappeared | hash_fail | tomb_fail |
|---|---|---|---|---|---|---|---|
| 1 | Phase 0 完成边界 | ✅ | 261/261 | 0 | 0 | 0 | 0 |
| 2 | Phase 1 完成边界 | ✅ | 261/261 | 0 | 0 | 0 | 0 |
| 3 | Phase 2 完成边界 | ✅ | 261/261 | 0 | 0 | 0 | 0 |
| 4 | Phase 3 完成边界 | ✅ | 261/261 | 0 | 0 | 0 | 0 |

HEAD 全程未变（0b836e7）；审计期间工作区零外部干扰；墓碑集恒空。

## V1 全量复核（证据链重走）

- **范围**：全部 173 条发现（F-001..F-370 号段，实际存在 173 文件）。
- **方式**：三批复核 agent（种子+P1 深度批 31 文件；契约线+风险线批 68 文件；质量线+配套线批 75 文件）+ 终局机械归一（1 条漏网 P4）。
- **锚点复核结果**：主锚点 file:line 打开核对——**失效率 0/173（0%）**；含代码摘录的发现逐字节核对一致（抽样覆盖 F-121/F-210/F-238/F-239/F-240 等）。
- **精度注记（4 处轻微，不成立否证）**：F-286 孪生块行数 101/99（证据写 121）；F-289 `limit=` 发射点 10 处非 13；F-341 `status_code=0` 行号 :180 非 :181；F-339 operations.md:582 属动作名白名单。核心结论均不受影响。

## V2 自我证伪（双轨规则，P0/P1 全集）

P0：0 条（无 P0 级发现）。P1 全集 = 4 条，逐条三步证伪（符号全点 rg / 契约豁免检查 / 测试锁定方向检查）：

| 发现 | 终判 | 关键证伪过程与结论 |
|---|---|---|
| F-001 | **维持 confirmed P1** | 上游发布链直读闭合（core/permission.ts:225,239,276 → event-v2-bridge.ts:35-44 无过滤 → /global/event）；`rg "replied" src/` 零处理、`permission.resolved` 上游零命中；v3 §7 未逐成员枚举 IMMEDIATE（幽灵名载体为 v2-contract §3/CHANGELOG/INTERFACE_MAP 文档回声）；test_hub_behavior_lock.py:915 以合成幽灵输入锁机制——测试锁定的是机制不是事件名匹配，不可证伪错配。轨二成立（实现确实如此）且轨一无豁免 → 维持。 |
| F-004 | **维持 confirmed P1** | config.py:817-821 精确相等钉死（≠(3,4) 即 RuntimeError）；死亡行 = app.py validate（SystemExit(1)）+ lifespan 二道；deploy:33 为被拒非法输入载体；测试锁定的是**正确的** fail-closed 语义（非偏离）——缺陷在部署样本非代码语义，不升 P0（生产 unit 已清理，威胁面=新部署照抄模板）。 |
| F-006 | **维持 confirmed P1** | 逐行重钉 messages.py:655/657/658/671/674（单向门+refund 时序+无重试）；反证一：singleflight 暖 grace ≤1s 可避——但冷缓存翻页主导路径确定性必现；反证二：契约 §4a.5「预算耗尽是特性」——驳回（预留耗尽≠字节预算耗尽，16×100KiB 可全容纳却只内联 1 条）；测试注释自证刻意回避生产参数组合。 |
| F-251 | **维持 confirmed P1（部署边界未验证）** | H8 依赖解除：本机只读实测 `ss -tlnp` = 0.0.0.0:4097 活 LISTEN + systemctl active——E-II 为运行时事实非模板推定；反证：sidecar 层无任何认证/缓解（allowlist 默认 None 且覆盖受 F-252 限制）；「部署边界未验证」语义收窄为 Tailscale ACL 实效未验。 |

**销案（refuted，3 条，均按「defect 且测试锁定契约一致行为」）**：
- F-002 `message.appended`：v2-contract §3:279/298 逐字冻结 + 3 处测试锁定 + 零 wire 后果 → 契约明载的 digest 驱动器，非死码缺陷。
- F-003 `normalize_session_status` 裸字符串分支：v3 §7.6 冻结「字符串或对象信封两种 wire 形态」+ test:241-251 裸字符串用例 → 契约保留形态的防御归一化。
- F-021 /full 404 映射：test_messages_routes.py:661-683 显式锁定 + 契约 §8.1 该码为唯一 404 归宿 → 未来加性设计注记。

**降级/证伪记录**：F-016（transform 许可泄漏）——A5 以 30,000 次取消竞态实验 + CPython Semaphore.acquire 补偿语义源码对照**实证证伪**（Python 3.14.4 零泄漏），降 P3（仅 3.11 名义部署面残留）；F-013 P2→P3（可达但影响限内）；F-014 P2→P3；F-028 P2→P3（上游 schema NOT NULL 关闭破口）。

## V3 一致性冲突消解（7 条）

1. F-013（A6 P3 vs A8 P2 语境）→ 以 D06 P3 为准，D08 引用无 P2 标签存活。
2. F-015（A6/A9 双辖）→ 以 D09 P2 为准，四处记载一致。
3. F-137（A3 P2 vs A8 MEDIUM）→ 统一 P2（E-II 维度 MEDIUM），INDEX 注记。
4. F-016（A5 证伪）→ P3 + 「verified-negative-on-3.14」注记归档复核记录。
5. F-151 ↔ F-125（v3 契约 [3] 滞后双立项）→ 双文件交叉注记，最终报告合并计数一次。
6. F-201 ↔ F-271（A5/A9 同主题）→ 归并提示，backlog 同批执行。
7. F-102 ↔ F-155（v4 §3.2 同锚两 facet）→ 交叉注记，单句勘误可同闭。
另：F-142 ↔ F-153 WS facet 为互补非重复（注记）。

## V4 基线复跑

- 执行方式：统一执行器（runtime-mode.json final 态，check_argv=["./scripts/check.sh"]，cwd=仓库根，隔离 env r-8）。
- 结果：**rc=0，绿→绿一致**——`3316 passed, 18806 warnings in 116.48s`（基线 127.47s，仅时长差）；路由↔文档一致性 54 条 ✅；compileall ✅。输出全文 `logs/check-final.txt`。
- 审计零污染实证：见 V0 seq4 + C13。

## V5 数字定稿

| 指标 | inventory | 终局复跑 | 一致 |
|---|---|---|---|
| src .py 文件 | 71 | 71 | ✅ |
| src 行数 | 26,452 | 26,452 | ✅ |
| 测试函数（rg） | 2,642 | 2,642 | ✅ |
| pytest 通过用例 | 3,316（基线） | 3,316（复跑） | ✅ |
| 路由 | 54 | 54（check_routes_doc 同口径） | ✅ |
| tracked 文件 | 260 | 260 | ✅ |
- 已知口径差（非不一致）：inventory error_codes=34 源于生成器单行正则漏多行构造（`provider_projection_limit`、`sse_*_limit` 族、`invalid_expand_category` 等）；全量真值以 route-census 错误码列 + A4 三向对账（实现 40 code 含 upstream_http_<N> 族）为准——inventory 局限已在 00-baseline 与 route-census.md 声明。

## V6 覆盖自查

见 AUDIT-REPORT.md §9 验收清单表（C1–C16 逐项）。

## 补充验证记录

- 六个基础验证脚本全部归档 `logs/probes/` 且每次调用前 hash 校验通过；gen_expected_keys 重放逐字节一致（REPLAY_BYTE_IDENTICAL）。
- expected-keys.csv 580 行 = A1 矩阵覆盖 = A12 test-gap-matrix.csv 580 行（validate_gap_matrix RESULT: VALID，双向集合差为空）。
- route-census / applicability 双 validation：census_ok=true、appl_ok=true、跨表一致（0 cross problems）。
- BLOCKED 单元：**0**（全部 15 个 A 项 + 8 个 E 项 DONE；无 unverified_due_to_blocker 发现；无 BLOCKED-STUB 产物——全部机器产物 VALID）。
- 附录 E 残余风险抽查：E1 applicability 337 行且 evidence_ref 非空 ✅；E4 报告 §0 BLOCKED 清单为空 ↔ 全文 coverage-degraded 标注 0 处 ✅ 一致；E5 无 recovery 事件（logs/superseded 归档仅常规 README/manifest 更新）。
