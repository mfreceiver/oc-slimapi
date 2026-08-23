# 20260823-1227 · deep-code-quality — 全仓工程质量与正确性深度审计

- **审计基线**：HEAD `a4cc717`（v4.12.0，2026-08-23）；main 分支；工作区干净（AGENTS.md 用户内容除外，审计期间未触碰）。
- **审计范围**：全仓 `src/oc_slimapi/**` + `tests/**` + 契约文档；重点热点（08-18 以来 churn）：`sse/global_hub.py`、`sse/tokenstream/*`、`sse/registry.py`、`dbaux/*`、`turn_registry.py`、`etag.py`、`app.py`。
- **方法**：4 并行子 Agent（架构可维护性 / 正确性边界 / 测试盲区 / 对抗反证）+ 主 Agent 一手复核（MAIN_AGENT_RECHECKED）+ 状态机推进至 VERIFIED；上轮 backend-reliability 排除清单 BE-001..BE-013 未重复报告。
- **验证命令**：`./scripts/check.sh`（pytest + 路由↔文档一致性 + compileall）；基线 3859 passed / 56 路由一致。

## Executive Summary

- 三项正确性缺陷经对抗反证独立确认并全部修复：**CORR-1（MAJOR）** hub 空闲回收无 replay barrier → 旧 cursor 重连假 `up_to_date` 静默漏帧；**CORR-2（MEDIUM）** `turnIncarnation` 持久化未确认仍发布 fence → 重启后 fence 值可复用/倒退；**CORR-3（MEDIUM）** dbaux 熔断 `SELECT 1` 半开探针不代表故障查询 → false close 绕过保护。
- 四项可维护性缺陷修复（ARCH-1/2/3/6，零 wire 变更）；四项记入长期 Remediation（ARCH-4/5/7/8）。
- 测试盲区四项靶向复现全部 HOLDS（纯覆盖缺口非缺陷）；新增 37 测试固化（3859 → 3896）。
- 修复全流程：fixer-glm 方案（经 oracle 五轮审阅）→ 逐批 rev-sgpt 门控（ARCH 9.7 / CORR r1→r4：7.8→9.0→9.2→9.7）→ 全批终审（首轮 NO-GO 文档收口后放行）→ **v4.12.1** 发版部署。

## Verified Findings（全部已修复于 4.12.1）

### CORR-1 · hub idle-grace 拆除后重连假 up_to_date（MAJOR）— VERIFIED→FIXED
- **证据链**：`registry.py` `_remove_hub_after_grace` 终段只调 `token_hub.on_upstream_reconnect()` 后置 `_global=None`，全程无 barrier/loss 通知；ReplayLog 进程级共享（domain epoch/seq 跨 hub 重建不变）→ 客户端旧 cursor `after_seq==last_seq` 命中 `up_to_date` 空重放（`replay_log.py:535-538`）；对照 `_notify_upstream_loss`（`global_hub.py:630-680`）在 upstream EOF 时 resync_all+跨域 `write_barrier(None)`——idle 回收制造同类未观察缺口却不走该路径。token 域同样暴露（Wave-2 独立确认）。
- **修复**：拆除终段接入 `notify_idle_recycle_loss()`（registry 任务组退出后、引用释放前，中间无 await），跨域写 barrier（全局域+全部 token 域）；旧 cursor 重连首帧 `resync{reason:"reconnect_no_replay"}`，客户端按 §7.2 既有语义 HTTP 全量对齐。代价：首个旧 cursor 客户端多付一次全量拉取（此前是静默漏帧）。

### CORR-2 · turnIncarnation 持久化降级破坏严格递增（MEDIUM）— VERIFIED→FIXED
- **证据链**：`turn_registry.py:137-149` 写失败仍返回 N+1 并照常发布（旧测试将该 best-effort 行为固化）；无父目录 fsync；legacy 回退无新旧高水位比较（新文件损坏→回退 legacy L→L+1 可能低于已发布值）。消费后果（Wave-2）：ocdroid 字典序 fence `(incarnation, turn)`，旧进程 (8,100) vs 新进程复用 8 从 (8,0) 起步 → 新 digest 持续被丢弃直至 turn 追平。
- **修复（方向 X「不确认不发布」，r2 起）**：启动写盘最多 3 次写入尝试；仍未确认 → 本进程 digest 与 `GET /slimapi/sessions/status` **配对省略** `turnIncarnation`/`turn`（契约 §7.5 既有可选字段语义，ocdroid 降 Tier-2，客户端零改动）；写成功路径取值不变（主/legacy 双文件高水位合并；原子写补父目录 fsync 尽力缩小 rename 掉电窗口——失败仅告警）。残余风险（如实声明）：已确认落盘文件事后遭外部损坏回退属先存双重故障类，维持接受。

### CORR-3 · dbaux 熔断 SELECT 1 探针不代表故障查询（MEDIUM）— VERIFIED→FIXED
- **证据链**：`lifecycle.py` `note_probe()` 单样本 P99<10ms 即关断（无 min_samples 门）；探针仅 `SELECT 1`；触发故障的真实投影是无 time_updated 索引的 join+ORDER BY（临时 B-tree O(N)，design-v4-dbaux.md:270-278 自认）——复杂度完全无关。后果：false close 后需 ~9 个慢查询重新积累窗口才能再 trip；低请求速率下可能永不复 trip（Wave-2 反证确认）。
- **修复（r1→r4 迭代，两段式+probation）**：`SELECT 1` 仅作连接存活预检（延迟不参与判据）；关断判据 = 路径同构 canary（`session LEFT JOIN project` 强制保留 join，按 `time_updated DESC, id DESC`，mode=ro 纯 SELECT，30s 半开 tick 至多一次）**连续 K=3 次**低于恢复阈值 → **仅进 probation 试用期**：真实查询恢复放行但逐查询标量判定，任一 ≥20ms **立即复 trip**（不依赖探针间隔/样本窗口时序）；连续 N=3 个好真实查询才正式 graduation（回常规 P99/最小样本判定）。canary 残余便宜度所致误闭合最坏暴露面**限 probation 误恢复期间 = 1 个慢真实查询**；r4 起 probation 复 trip 后已排队请求于**出队执行前**二次检查熔断器（dequeue-recheck），走与入口拒绝逐字节一致的降级（不执行 SQL，暴露面不含队列深度）。门控缺陷迭代史：r1 时间地板无跨进程高水位（BLOCKER）→ r2 K=3×30s 探针与 60s 滑窗互消（MAJOR）→ r3 probation 未限制已排队并发（MAJOR）→ r4 全闭合（9.7 PASS）。

## Architecture Observations（Agent1 + 主 Agent 复核裁决）

| ID | 发现 | 裁决 | 处置 |
|---|---|---|---|
| ARCH-1 | lifespan 注释列 7 项 vs 实际 14 个 teardown callback | 确认 | ✅ 4.12.1 注释全量枚举（零代码变更） |
| ARCH-2 | `_judge_pack_tail`≈`_tail_encode` 双拷贝已漂移（bodiless 语义差异为有意） | 确认 | ✅ 共享 `encode_conditional_tail`（body-optional 参数化）+ 两具名包装器，wire 字节不变 |
| ARCH-3 | `busy_response` + `TRANSFORM_RETRY_AFTER_SECONDS` 双定义 | 确认（原表述不准：两处均命名常量） | ✅ 合并至 `_catalog_common` 单一定义 |
| ARCH-4 | `publish()` ~377 行 10+ 职责 | 结构确认 | 不修：修订六刚冻结，拆分回归风险>收益；长期项 |
| ARCH-5 | `app.state` ~23 属性无 Protocol | 确认 | 不修（长期项） |
| ARCH-6 | `wire_view=3` 默认值 12 处 | **部分驳回**：`_expand_wire_view` 恒返 4（D5），默认 3 从不上 wire；`test_projection_default_view_keeps_frozen_v3_bytes` 金样显式冻结默认 3（设计冻结非疏漏）；`_read_passthrough.py:244` literal 3 为 ETag validator 域冻结（D6） | ✅ 仅加防呆注释（skeleton.py 7 处 + `_list.py`），默认值不动 |
| ARCH-7 | 30+ inline `CodedHTTPException` | 确认 | 不修（风格类，错误形状已有一致性保证） |
| ARCH-8 | `config.py` TOKEN_* 双源 | 确认 | 不修（消费方梳理风险>收益） |

## 测试盲区（Agent3）与复现判定

- TEST-1（Subscriber.put 背压溢出）、TEST-3（barrier GC×append 交错）、TEST-4（B-1 delta×B-2 eviction 同域交错 seq 孔洞）、TEST-6（CatalogCache invalidate×飞行 refresh）：靶向复现脚本判定**全部 HOLDS**（exit 0）→ 纯覆盖缺口，非真实缺陷；断言已随修复批次固化进 pytest。
- TEST-2（lifespan LIFO 顺序断言）、TEST-5（generation 跨测不重置）、TEST-7（breaker 浮点边界）、TEST-8（跨域 barrier 一致性）：未复现（低/中优先级），记入 Remediation 测试积压。
- 体系性风险：~30 处 real-time sleep 有 flake 风险（singleflight 0.12s×17+、test_actions 4s）——本轮 release.sh 门禁即遭遇 test_actions 一次 flake（隔离重跑通过，与本批改动无关联）。

## Rejected Hypotheses

- **ARCH-6 原案**（默认值 3→4 防呆改写）：被金样 `test_expand_href_v4.py:113` 驳回——默认 3 是纯函数历史冻结；驳回后处置降级为仅注释。
- **CORR-2 r1 时间地板方案**：门控 7.8 FAIL——同秒复用+失败后倒退反例（无跨进程高水位），被 r2「不确认不发布」替代。
- **CORR-3 r2 直接闭合方案**：门控 9.0 FAIL——K=3×30s 探针恢复与 60s 滑窗互消使「首慢查询复 trip」失效，被 r3 probation 三态替代。
- **CORR-3 r3 无队列回查方案**：门控 9.2 FAIL——probation 期间 executor 队列无 admission，暴露面=队列深度，被 r4 dequeue-recheck 替代。
- **TEST-1/3/4/6 为真实缺陷假设**：复现全部 HOLDS，驳回。

## Inconclusive

- 无。全部发现均已推进至 VERIFIED 或 REJECTED。

## Tests / 覆盖

- 基线 3859 → 终态 **3896 passed**（+37：ARCH-2 4 / CORR-1 8 / CORR-2 11 / CORR-3 14）；56 条 `/slimapi` 路由↔INTERFACE_MAP 一致；compileall 通过。
- 修复后 `./scripts/check.sh` 双重实证（提交前 + release.sh 内置门禁各一次全绿；release 门禁中 test_actions 单例 flake 隔离重跑通过）。

## 修复流程审计（Audit Integrity）

1. 方案：fixer-glm 起草（ARCH 批 + CORR r1-r4），oracle 每轮审阅（5 轮全 APPROVE，必改项均已吸收）。
2. 门控：rev-sgpt 逐批评分——ARCH 9.7 / CORR r1 7.8 → r2 9.0 → r3 9.2 → r4 **9.7**（阈值 9.5，r1-r3 FAIL 项全部返修闭合）。
3. 全批终审（rev-sgpt）：代码交叉一致 ✅、测试数学闭合 ✅、CHANGELOG/契约一致 ✅；首轮 NO-GO 的 6 项文档收口（2 处失效 SSOT 路径→archive、CHANGELOG 去 .work 引用、unreleased→日期、重试措辞、暴露面限定 probation）已全部落实。
4. 发版：`854215a`（修复批次，22 文件 +2077/−225，`.work/` 审计产物未入库）→ `./scripts/release.sh patch` → **v4.12.1**（`f48c370` + tag）。
5. 部署：editable 安装元数据刷新至 4.12.1，`systemctl --user restart oc-slimapi`，`/slimapi/health?v=4` 确认 `sidecar.version=4.12.1`、schema 非 degraded。

## Remediation Plan（长期项，未排期）

1. ARCH-4：`publish()` 拆分（需在下一非冻结窗口专项设计）。
2. ARCH-5：`app.state` 类型契约（Protocol）。
3. ARCH-7：inline `CodedHTTPException` 收敛为工厂。
4. ARCH-8：`config.py` TOKEN_* 双源梳理。
5. 测试积压：TEST-2/5/7/8 固化；~30 处 real-time sleep 换虚拟时钟（flake 根治）。
