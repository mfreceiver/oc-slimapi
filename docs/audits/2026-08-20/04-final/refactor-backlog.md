# 整改 backlog（04-final/refactor-backlog.md）

> §8.4 冻结评分：`score = severity_weight × impact ÷ cost`；severity_weight P1=10/P2=6/P3=1；
> impact 3=消费方直接触达面或核心数据路径 / 2=内部可靠性、运维 / 1=局部；cost 1=单文件纯机械 / 2=多文件机械或单文件语义风险 / 3=跨面语义变更需契约、测试同步；
> tie-break：score 同 → severity 高者先 → impact 高者先。范围 = 全部 P1×4 + P2×19（P3×150 见 02-findings/INDEX.md，其中约 20 条文档类可一次提交批量关闭）。

| 排名 | score | 发现 | sev | impact | cost | 整改方向 | 依赖（必须先行） |
|---|---|---|---|---|---|---|---|
| 1 | 20.0 | F-004 | P1 | 2 | 1 | deploy 模板删 :33（ACCEPTED=2,2）与 :32（废弃 SERVER_API_VERSION）两行 + operations.md 措辞更正 | 无——独立可做；与 F-339 文档批合并更佳 |
| 2 | 18.0 | F-025 | P2 | 3 | 1 | sessions v4 limit 边界统一（501-1000 与 >1000 的 422 归一），对照 §4.1 裁决后单文件修正+测试 | 前置：A4/D04 §4.1 对照裁决已给出方向 |
| 3 | 15.0 | F-001 | P1 | 3 | 2 | IMMEDIATE 集改名 permission.replied/permission.v2.replied（对齐上游）+ 契约/文档回声勘误 + 幽灵名回归测试 | 建议先做 F-216（丢弃计数）以便验证修复生效 |
| 4 | 15.0 | F-006 | P1 | 3 | 2 | merged fanout 候选重试/预算预留策略修正（或恢复预留+降级标记），补默认参数组合测试 | 契约 §4a.5 语义内修复；测试补 32MiB×8MiB 组合 |
| 5 | 12.0 | F-007 | P2 | 2 | 1 | _stop_qp_sweep 包 try/except 与其余 13 个关停回调同款隔离 | 无 |
| 6 | 12.0 | F-008 | P2 | 2 | 1 | _ACCESS_LOG_RE 接受 access-legacy-*.jsonl.gz（或独立 prune 分支） | 无 |
| 7 | 12.0 | F-009 | P2 | 2 | 1 | snapshot prune 与 access-log 维护循环解耦（独立周期或禁用时仍清理） | 与 F-010 关停链调整同批 |
| 8 | 12.0 | F-011 | P2 | 2 | 1 | _remove_hub_after_grace 兜底 except + 失败后 _removal_task 清理 | 无 |
| 9 | 12.0 | F-015 | P2 | 2 | 1 | qp_last_activity 并入有界 LRU/逐出策略 | 与 F-273（sweep 逐出击穿）同修 |
| 10 | 12.0 | F-123 | P2 | 2 | 1 | INTERFACE_MAP 头部声明更新为 (3,4) 双版本 | 并入 F-346..F-353 文档批量提交 |
| 11 | 12.0 | F-137 | P2 | 2 | 1 | app 装配显式关 docs/openapi（FastAPI(docs_url=None 等)） | 安全与记账双收益 |
| 12 | 12.0 | F-216 | P2 | 2 | 1 | catch-all 丢弃帧 per-type 计数器 + 采样日志（可检测事件集漂移） | F-001 修复的观测前提 |
| 13 | 12.0 | F-339 | P2 | 2 | 1 | operations.md runbook 批量补齐（20+ env、503/degraded/订阅/重放/allowlist 场景、E-II 姿态运维） | 纯文档；含 19 条缺口清单 |
| 14 | 10.0 | F-251 | P1 | 3 | 3 | E-II 面收敛组合拳：回环绑定或 ACL 强制校验/启动告警 + 部署文档强制化（部署边界未验证） | 依赖 F-252（allowlist 覆盖）与 F-339（运维指引）先行 |
| 15 | 9.0 | F-201 | P2 | 3 | 2 | messages 列表/merged 200 尾部 gzip+sha256 移入 worker job | 与 F-271 同主题归并执行 |
| 16 | 9.0 | F-252 | P2 | 3 | 2 | directory allowlist 覆盖面扩展到全部 directory 敏感路由 | F-251 的缓解前提 |
| 17 | 9.0 | F-271 | P2 | 3 | 2 | （与 F-201 归并）ETag sha256 offload | 依赖 F-201 |
| 18 | 6.0 | F-017 | P2 | 3 | 3 | providers v3 面敏感字段裁剪评估（需契约修订——v3 冻结面 vs 安全豁免的正式裁决） | owner 裁决项：v3 逐字节冻结 vs 密钥暴露的权衡 |
| 19 | 6.0 | F-121 | P2 | 3 | 3 | 多工作目录客户端指引/客户端侧过滤方案成文（服务端补齐被 §17 non-goal 堵死） | CLIENT_CHANGES v4 章节先行（F-124） |
| 20 | 6.0 | F-010 | P2 | 2 | 2 | 关停链超时收敛或 TimeoutStopSec 上调对齐（30+5+10s 级 vs 15s） | 与 F-009 同批评估 |
| 21 | 6.0 | F-304 | P2 | 2 | 2 | questions/permissions 提取共享聚合框架（相似度 0.832） | 纯重构；测试锚点迁移 |
| 22 | 4.0 | F-301 | P2 | 2 | 3 | tokenstream/hub.py 五模块化拆分 | 建议在 F-201/F-271 修复后（避免 rebase） |
| 23 | 4.0 | F-302 | P2 | 2 | 3 | messages.py 三族端点拆包 | 同上 |

## 依赖关系汇总（必须先行）

- F-216 → F-001（丢弃计数是修复生效的观测前提）
- F-252 + F-339 → F-251（allowlist 覆盖与运维指引是 E-II 收敛组合的前提）
- F-201 → F-271（同主题归并执行）
- F-201/F-271 → F-301/F-302（性能 offload 先于大拆分，避免 rebase 冲突）
- F-124（P3，CLIENT_CHANGES v4 章节）→ F-121（多目录客户端指引载体）

## P3 快赢簇（未计分，随手批处理）

- 文档批量提交：F-123 + F-346..F-353 + F-124 + F-125/F-151（约 20 条机械字面更新，D14 评估一次提交可关闭）
- 死依赖/死代码清扫：F-018（respx）+ F-024 七项 + F-290/F-292 残链
