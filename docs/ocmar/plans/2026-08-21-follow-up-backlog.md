# oc-slimapi 后续任务清单（审计整改批次一之后）

> 基线：v4.5.0（d1b0dcd，2026-08-21）。来源：docs/audits/2026-08-20/04-final/refactor-backlog.md（23 项评分排序）+ 批次一计划「明确不在本批」登记 + rev-cgpt R4 N1。
> 批次一已完成 12 项（F-001/F-004/F-005/F-006/F-007/F-008/F-009/F-011/F-015/F-025/F-137/F-216/F-273），见 CHANGELOG [4.5.0]。

## 批次二（推荐下一步：机械主导，可复用「方案 → rev 门控 → 并行泳道」流程）

| # | 内容 | 发现 | 类型 | 备注 |
|---|---|---|---|---|
| B2-1 | **文档批量提交**：operations.md runbook 增补（19 条缺口清单：20+ env、503/degraded/订阅/重放/allowlist 场景、E-II 姿态）+ INTERFACE_MAP 头部 (3,4) 双版本声明 + F-346..F-353 + CLIENT_CHANGES v4 章节 + F-125/F-151/F-157 时效族 | F-339/F-123/F-346..353/F-124/F-125/F-151/F-157 | 纯文档 | 单泳道一次提交；约 20 条 P3 关闭；F-339 缺口清单已备（finding 内 19 条逐项） |
| B2-2 | **性能 offload**：messages 列表/merged 200 尾部 gzip+sha256 移入 worker job（read_passthrough 尾部同题归并） | F-201+F-271（+F-202 同族） | 性能/事件循环 | 必须先于 B3 大拆分（backlog 依赖声明） |
| B2-3 | **关停超时对齐**：uvicorn 5s + 维护排水 30s + dbaux 5s vs systemd TimeoutStopSec=15 | F-010/F-214 | 运维 | 与 deploy 模板 TimeoutStopSec 上调或排水预算收敛二选一 |
| B2-4 | **契约缺口收口批**：actions `invalid_request_body` 归宿（v2 §2 七码表外）、WS 501 `websocket_not_supported` 归宿、405 `method_not_allowed` 归宿、SSE resync reason 值域运行时门控、allowlist 字段位置措辞、v3-contract §11 测试矩阵标注 | F-154/F-153/F-152/F-122/F-155/F-156 | 契约文档 | 全部为「实现正确、契约未载」类，行为零改动 |
| B2-5 | **死代码清扫**：respx 死依赖、七项只写不读状态（_busy_sids/last_touch/recycle/directory_source/strip_hop_by_hop 等）、死符号群（SELECTOR_V2/_LAST_UPDATED_AT_BY_SID_MAX 等）、tokenstream 两处 TODO 解除（上游 camelCase 真值已定） | F-018/F-024/F-138/F-030 | 质量 | 纯删除 + 回归绿即可 |
| B2-6 | config.py `max_message_bytes` 下界（≥1）校验 | rev-cgpt R4 N1 | 小项 | 批次一 I1 不变量的正值前提补齐 |

## 裁决项（需 owner 先拍板，再入批次实施）

| # | 议题 | 发现 | 待裁决点 |
|---|---|---|---|
| R-1 | **E-II 面收敛组合拳** | F-251(P1)/F-252 | ① 回环绑定 vs 网络层强制（当前 0.0.0.0+零认证，Tailscale ACL 实效未验证）；② allowlist 提升全 directory 路由统一前置门 vs 维持局部门+文档明示边界 |
| R-2 | providers v3 透传敏感字段（api/key/env/options 全集暴露） | F-017 | v3 逐字节冻结面 vs 安全豁免的正式契约修订（v3 消费方=ocdroid 兼容影响） |
| R-3 | per-directory sessions 列表无 v4 等价物 | F-121 | §17 已堵服务端补齐 → CLIENT_CHANGES 客户端侧过滤指引成文（依赖 B2-1 的 F-124 载体） |
| R-4 | `question.replied/rejected`（含 v2）决议族策展 | F-001 同族注记 | 是否纳入 IMMEDIATE 直推（与 permission.replied 同理——上游真名存在、当前静默丢弃；改动属 wire 加性） |
| R-5 | F-216 丢弃计数的 metrics wire 暴露 | F-216 后续 | `/slimapi/metrics` §9.2 加性契约修订（v3 冻结面 vs 加性维度） |
| R-6 | **v3 退役推进** | 04-final/v3-retirement-plan.md | owner 终态已裁「(3,4) 永久双版本」；口径 b（收窄）为成本模型备案。前置依赖 ocdroid 迁移进度，5 项机械准备可随批次二/三顺带 |

## 批次三（大型重构，须在 B2-2 offload 之后避免 rebase）

| # | 内容 | 发现 | 规模 |
|---|---|---|---|
| B3-1 | tokenstream/hub.py（2190 行，全仓最大）五模块化拆分 | F-301 | 大 |
| B3-2 | messages.py（1643 行）三族端点拆包 | F-302 | 大 |
| B3-3 | questions/permissions 提取共享聚合框架（相似度 0.832） | F-304 | 中；测试锚点迁移 |

## 建议推进顺序

1. **批次二先行**（B2-1 文档批零风险速赢 → B2-5/B2-6 清扫 → B2-2 offload → B2-3/B2-4）；
2. 裁决项 R-1/R-2 安全面优先（E-II 是生产实际暴露面）——裁决后可并入批次二尾或独立小批；
3. 批次三大拆分最后（offload 落地后）；
4. 每批沿用已验证流程：实施计划（writing-plans）→ rev 门控 ≥9.0 → 写域互斥并行 fixer 泳道 → 编排者收尾（CHANGELOG/契约/check.sh）→ release.sh。
