# D13 — A13 可观测性与运维审计（metrics 完备性 / access log 实用性 / runbook / deploy 对账 / 告警建议）

> Phase 2 专项报告，2026-08-20。快照 `0b836e7`（v4.4.0）。只读审计：未运行 pytest/pip/systemd，未改动仓库源码。
> 证据格式 `路径:行号`。严重度 P0–P3。输入：`src/oc_slimapi/{traffic.py,access_log.py,traffic_snapshot.py,routes/metrics.py,sse_observability.py,middleware/}`、`docs/operations.md`、`docs/manual/traffic-accounting.md`、`deploy/oc-slimapi.service`、`01-explore/{parts/e1-13,config-census.md,docs-notes.md,route-census.csv}`。
> 关联报告：D07（dbaux）、D08（安全/E-II）、D09（性能/内存/关停链——A9 已测内存上界与 30s 维护排水）、E6（docs-notes §4 deploy 三方对账底表）。

---

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| metrics 块完备性 | 7 块（hubs/skeleton/traffic/sweep/dbaux/sessionsDegraded/replay）形状齐全、加性约定一致；但**全部块均无错误码维度**——503/502/413 族内部多码不可区分（F-331） |
| metrics↔路由覆盖 | 54 条路由（E2 全量）逐行核对：状态类+字节归因全覆盖；码级归因 0 条路由可达；专项块补强 6 族（sessions v4/expand/SSE×2/actions(journald)/questions-permissions 无) |
| 静默路径 | **12 条**（§1.3），其中 3 条已有主辖发现（F-216/F-022/F-023），本次新增 7 条立 F-331/F-333/F-337/F-338/F-340/F-341 |
| access log 实用性 | 字段全集与 traffic-accounting.md §5.1 口径一致；`recordType` 陷阱文档显眼度中等（§2.2）；RETAIN 3×30 窗错配使 §9.4 对账公式在 >4 天窗口系统性失真（F-336） |
| runbook | **19 条缺口**（§3.3）；E-II 姿态 §11 已成文（正例）但小节编号漂移 + 外部证据未回填；DIRECTORY_ALLOWLIST/REPLAY_*/SSE 旋钮等 20+ env 零 ops 记载（汇总 F-339，P2） |
| deploy 对账 | 12 条 Environment 逐条裁决（§4.1）：3 有意覆盖一致、4 冗余默认（1 条锁旧值风险）、2 残留（:33 startup-fatal＝F-004 P1；:32 弃用警告＝F-005 P3）、3 StateDirectory 族一致；外加 4 条指令级裁决（TimeoutStopSec 与 F-010 冲突、Restart 族放大 F-004、MemoryMax 对照 A9 上界、无 StartLimit/OnFailure＝F-344） |
| 告警建议 | 18 项 advisory 阈值表（§5），全部可由 `GET /slimapi/metrics?v=3` + journald 关键词实现，无需新代码（除 2 项标注需加观测位） |

---

## 1. metrics 完备性（任务 1）

### 1.1 `GET /slimapi/metrics` 块清单（routes/metrics.py:20-111）

| 块 | 行号 | 内容 | 出现条件 | 备注 |
|---|---|---|---|---|
| `sse` | :22-23 | `{subscribers:{current,limit,rejectedTotal}, hubs:[…], clients:[…], batch:null}` | 恒（`state.hubs` 无 getattr 容错 :22——未挂即 AttributeError 500，测试 app 面） | `batch` 死键（契约形状冻结） |
| `sse.tokenStream` | :29-31 | current/limit/rejectedTotal/pendingAccumulators/flushed/dropped/truncated/orphan/tokenMemoryLimitTotal + gzip 双计数 + flush 计数 + maxSubscriberQueueDepth（tokenstream/subscriber.py:838-873） | 挂 token_registry 才有 | |
| `skeleton` | :22（hubs 快照内） | activeTransforms/waitingTransforms/cacheEnabled（sse/registry.py:327-338） | 恒 | transform 饱和唯一观测位 |
| `traffic` | :36-38 | `ledger.snapshot()` 全量（buckets/totals/ratios/latencyMs/v3.matrix/sseLifecycle/sseActive/expand/v4.degradedMatrix，traffic.py:709-845） | 挂 ledger 才有 | `enabled=false` 时仅 `{enabled:false}`（traffic.py:762-763） |
| `sweep` | :42-44 | triggers_total/cold_hits/skips/budget_exhausted/est_bytes_total/known_directories（qp_sweep.py:239-247） | 挂 qp_sweep 且 `getattr(enabled, True)` | **三文档零记载**（grep operations.md/traffic-accounting.md/develop.md 无 "sweep"）；`getattr(qp_sweep,"enabled",True)` 默认 True 方向与注释意图相反（metrics.py:41-43，缺属性应默认 False 更保守）→ F-333 |
| `dbaux` | :51-69 | available/mode/reason/generation/source/latency{p50,p99,samples,total}/breaker_open/counters | 挂 dbaux 才有 | D07 已逐字段对照（不泄 DB 路径） |
| `sessionsDegraded` | :80-84 | `{degraded_200, fail_closed_503}`（traffic.py:348-391） | 中间件首请求挂载后 | 首请求前缺席（:74-79 注释自认）；挂载失败丢计数无信号（traffic.py:410-416）→ F-340 |
| `replay` | :97-107 | epoch + domains/frames/bytes/barriers + outcome/resync 计数（sse/replay_log.py:338-347） | 挂 replay_log 才有 | |

### 1.2 metrics↔路由覆盖表（E2 route-census 54 条逐行）

图例：**状态类**＝bucket 级 requests/errors4xx/errors5xx（traffic.py:483-489）+ v3 矩阵 statusClass（:582-611）+ access log 行（path+status）；**码级**＝错误 code 可区分；**专项块**＝该路由失败另有专属观测位。所有路由共同缺口：access log 无 `code` 字段（access_log.py:333-364）、矩阵只到 statusClass（traffic.py:576-580）——**`upstream_http_<N>`（502）无独立计数**，与 `upstream_invalid_shape`/`provider_upstream_malformed` 同为 502 不可分；413 族（request/response/message_too_large、provider_projection_limit、expand 族 413）同为 413 不可分；503 族（upstream_unavailable/transform_busy/auxiliary_unavailable/action_unavailable）同为 503 不可分 → F-331。

| # | 路由 | 桶 | 状态类 | 码级 | 专项块 / 注记 |
|---|---|---|---|---|---|
| 1 | DELETE /slimapi/session/{id} | write_session | ✔ | ✘ | 413 双义（request/response_too_large） |
| 2 | GET /slimapi/actions | other | ✔ | ✘ | enabled 状态在响应体；metrics 无 actions 块 |
| 3 | GET /slimapi/agent | agent | ✔ | ✘ | cache hit/miss 稀疏字段 |
| 4 | GET /slimapi/api/session/active | session_active | ✔ | ✘ | |
| 5 | GET /slimapi/command | command | ✔ | ✘ | cache hit/miss 稀疏字段 |
| 6 | GET /slimapi/config/providers | providers | ✔ | ✘ | 裸 `/slimapi/config`（无尾斜杠）落 other（traffic.py:151）→ F-343 |
| 7 | GET /slimapi/directories | directories | ✔ | ✘ | |
| 8 | GET /slimapi/events | events_sse | ✔ | ✔* | SSE 族：sseLifecycle/sseActive + hubs 块 + sse_open/close 行；400 sse_subscriber_limit_* 与 invalid_tokens 同 4xx 不可分 |
| 9 | GET /slimapi/file | file | ✔ | ✘ | 403 directory_not_allowed 与 413/503 可按状态粗分 |
| 10 | GET /slimapi/file/content | file | ✔ | ✘ | 同上 |
| 11 | GET /slimapi/file/status | file | ✔ | ✘ | 同上 |
| 12 | GET /slimapi/find/file | find | ✔ | ✘ | |
| 13 | GET /slimapi/global/health | global_health | ✔ | ✘ | |
| 14 | GET /slimapi/health | health | ✔ | ✘ | 自描述；schema.degraded 在本响应体 |
| 15 | GET /slimapi/messages/{sid} | messages | ✔ | ✘ | |
| 16 | GET …/expand/{category}/{mid} | messages.expand | ✔ | 部分 | **expand 块 category×status**（traffic.py:640-672）——唯一带 per-category 失败面的路由；错误码仍不可分（expand_* 族同 4xx/5xx 内多码） |
| 17 | GET …/expand/…/{partID} | messages.expand | ✔ | 部分 | 同上 |
| 18 | GET /slimapi/messages/{sid}/full/{mid} | messages | ✔ | ✘ | 404 session vs message_not_found 语义混（F-021，A10 主辖）——观测面同为 404 不可分 |
| 19 | GET /slimapi/metrics | metrics | ✔ | ✘ | 自身受 selector 管辖需 `?v=`（metrics.py:7-9）→ F-334 |
| 20 | GET /slimapi/permissions | other | ✔ | ✘ | **200 载错误**：envelope errors[]/truncated 部分目录失败时 status=200，观测面不可见 → F-331 证据 |
| 21 | GET /slimapi/questions | questions | ✔ | ✘ | 同上 200 载错误 |
| 22 | GET /slimapi/ready | health | ✔ | ✘ | 503 无 body 细节（设计） |
| 23 | GET /slimapi/session/{sid} | session_single | ✔ | 部分 | v4 单查 degraded 标记缺失（F-022 扩展：read_groups.py:513,533,541,546 直接 `_aux_unavailable()` 无 `slimapi_degraded_503` 置位） |
| 24 | GET /slimapi/session/{sid}/context | session_context | ✔ | ✘ | |
| 25 | GET /slimapi/sessions | sessions | ✔ | ✔* | sessionsDegraded 块 + sessionsSource/degraded503 稀疏字段 + v4.degradedMatrix（traffic.py:676-705）；例外：Class A HTTP fallback 内部 503（sessions.py:673 经 worker offload）漏标记 → F-022 |
| 26 | GET /slimapi/sessions/status | sessions | ✔ | ✘ | |
| 27 | GET …/children | sessions | ✔ | ✘ | |
| 28 | GET …/diff | sessions | ✔ | ✘ | |
| 29 | GET …/stream | token_stream_sse | ✔ | ✔* | tokenStream 块 + sseLifecycle；handshake overflow/subscriber limit 同 4xx 不可分 |
| 30 | GET …/todo | sessions | ✔ | ✘ | |
| 31-33 | GET /slimapi/vcs[/diff|/status] | vcs | ✔ | ✘ | |
| 34 | GET /slimapi/versions | other | ✔ | ✘ | 唯一 selector 豁免 |
| 35 | PATCH /slimapi/session/{id} | write_session | ✔ | ✘ | |
| 36 | POST /slimapi/actions/{name} | other | ✔ | ✘ | **journald WARNING 审计**（operations.md:643）有日志无指标；action_busy/throttled/timeout 在 ledger 仅 other 桶 4xx/5xx |
| 37-38 | POST /slimapi/question/{rid}/[reject\|reply] | write_question | ✔ | ✘ | |
| 39 | POST /slimapi/session | write_session | ✔ | ✘ | |
| 40 | POST /slimapi/session/{id}（v4 等效） | write_session | ✔ | ✘ | 405/404 过渡态按状态可分 |
| 41-54 | POST …/{abort,agent,archive,command,delete,fork,model,permissions/{pid},prompt_async,revert,revert/clear,revert/commit,revert/stage,summarize} | write_session | ✔ | ✘ | 413 双义同 #1 |

> 行数口径：54 行＝E2 route-census.csv 全量数据行（1 表头 + 54 路由）。码级归因列全表 ✘（2 个 ✔* 为 SSE/degraded 族的**部分**码级区分，靠稀疏字段/专属块而非通用错误码维度）。

### 1.3 静默路径清单（12 条）

| # | 路径 | 证据 | 归属 |
|---|---|---|---|
| 1 | catch-all 丢弃 76 型上游事件零计数 | global_hub.py:975 | **F-216**（A6 已立，P2） |
| 2 | sessions v4 fallback/单查 503 漏 degraded 观测位 | sessions.py:673；read_groups.py:513,533,541,546 | **F-022**（本次 verified 扩展） |
| 3 | WS 501 stub 零记账 | proxy.py:34-38；traffic_accounting.py:162-165 | **F-023**（A8 补证据） |
| 4 | ServerErrorMiddleware 500 响应字节绕过计数 | starlette applications.py:69-71 栈序 | **F-023** |
| 5 | sweep 桶死桶 + sweep 块零文档/零消费方 | traffic.py:109-110；metrics.py:42-44；三文档 grep 无 | **F-333**（新） |
| 6 | SSE 路径非流式错误响应的 stash 上游字节不进 ledger | traffic_accounting.py:410-417 | 新（并入 F-331 证据面，卡片 e1-13 疑点 4） |
| 7 | questions/permissions 200 载 envelope errors[]/truncated 降级不可观测 | 响应 200 + ledger 只记 2xx | **F-331**（新） |
| 8 | sse_observability 双 `except Exception: pass` 零日志 | sse_observability.py:107-108,113-114 | **F-338**（新） |
| 9 | snapshotter inactive / access-log handler disabled 状态无 metrics 位 | traffic_snapshot.py:359-369；app.py:245-250 | **F-337**（新） |
| 10 | sessionsDegraded 计数器 setattr 失败临时实例丢计数无信号 | traffic.py:410-416 | **F-340**（新） |
| 11 | status=0/"0xx" 行零错误分类（正常路径无 or-500 兜底） | traffic_accounting.py:181,239；traffic.py:486-489,576-580 | **F-341**（新） |
| 12 | metrics 无错误码维度（503/502/413 族内部不可分） | traffic.py:576-611；access_log.py:333-364 | **F-331**（新） |

已知**非**静默的口径边界（文档化，不计入）：early-reject 请求体 downIn 不计（traffic_accounting.py:25-34）；SSE 活跃期间 requests==0/downOut>0（traffic.py:750-757）；coalescing 共享 fetch 不入桶（traffic-accounting.md §7）。

---

## 2. access log 实用性（任务 2）

### 2.1 字段全集 vs traffic-accounting.md §5.1 口径

实现（access_log.py:333-364）：固定 14 键 `ts/method/path/bucket/status/durationMs/downIn/downOut/upIn/upOut/requestId/client/clientVer/clientId` + 恒写 5 键 `wireVersion/selectorResult/directoryForm/recordType/lifecycleId`（:352-356）+ 稀疏 4 键 `cache`（:350-351）/`sessionsSource`（:360-361）/`degraded503`（:362-363，仅 true 永不 false）。手册 §5.1 字段表（traffic-accounting.md:159-175）**逐字段一致**，含稀疏语义与 jq 用法。✅ 无漂移。`status` 形参无值域检查（:339）——status=0 行是 jq 状态类过滤黑洞（F-341 关联）。

### 2.2 `recordType` 过滤陷阱的文档显眼度

- 手册有 **3 处**提示：§5 字段表 `recordType` 行（:172「统计请求数/字节时必须过滤」）＋ §5 常用分析 blockquote（:192，含旧文件容错写法）＋ §5.1 生命周期行说明（:179）。显眼度**中等偏上**。
- **缺口 A**：§9.4 离线对账节（:363-365）描述 matrix/sseActive 公式时**未重申**过滤——而聚合函数 `aggregate_v3_observability` 自身不过滤 recordType（traffic_snapshot.py:239-241，counts 把 sse_open/close 行计入矩阵，键第 4 段才可区分），照抄「跨日 carry-in 公式」的运维若直接用该函数对账，SSE 桶计数放大约 3×。→ **F-335**。
- **缺口 B**：operations.md（面向值班排障的第一入口）零提及 recordType——只在 traffic-accounting.md。跨文档发现成本。

### 2.3 RETAIN_DAYS=3 × snapshot retain 30 的对账价值（时间窗错配）

- 事实：生产 access log 保留 3 天（deploy:46），snapshot 保留 30 天（deploy:45）；且 prune 边界是 `file_date < today - retain_days`（access_log.py:562,575；traffic_snapshot.py:79-101 同式）→ 实际各多留 1 个日历日（4 天 / 31 天）。
- **对账价值结论**：snapshot 的 v3 节与 access log 的对账只在 **≤4 天窗口**内双向可证（access log 是矩阵的明细源）；4–30 天窗口内只剩 snapshot 单侧——cumulative 帧可看趋势，但 §9.4 的跨日 carry-in 公式（`sseActive[D+1] = sseActive[D] + opens − matched_closes`）在窗口起点**没有对应 open 行**时：孤儿 close 只补计数不冲减（by design），期初存量只能取 0 或首帧 sseActive——长窗对账 sseLive/sseActive 系统性失真，手册未提示该窗上限。→ **F-336**（含「保留 3 天」实为 4 天的口径差）。
- 附：legacy 归档不受 retain（F-008）；snapshot 文件永不压缩（e1-13 疑点 1，磁盘口径见 operations.md §5.4 估算 ~9MB/30 天，可接受）。

---

## 3. runbook 审计（任务 3）

### 3.1 已覆盖（正例）

| 场景 | operations.md | 评价 |
|---|---|---|
| E-II 部署姿态（0.0.0.0+ACL） | §11（:492-556） | **已成文**：稳态拓扑、边界验证负向探针（nmap/curl 命令）、cert 复用说明。缺陷：小节编号漂移（§11 内用 10.1-10.4 标题）；负向探针结果指向外部报告 `docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md` §3 由 ops 回填——仓库内无回填核验机制 |
| dbaux 熔断/恢复/升级后 runbook | §7.1-7.4（:377-445） | 动作完备（路径解析顺序、索引运维程序含 PRAGMA index_xinfo 校验、P99>20ms 熔断、周期重探、`auxiliary.available` 观察、升级后第一步） |
| 升级三步（pull+reinstall+restart） | §2/§4（:47-54,166-177） | 含 health.version 滞后踩坑说明 |
| shutdown 语义 | §4（:164） | 5s 宽限/15s 上限记载，但未计入 30s 维护排水（→F-010，D09 :160 同判） |
| journald 查询/启动样本 | §5.6（:294-331） | 完备 |
| actions 管理 | §12（:560-645） | manifest/权限/审计/限频记载较全 |
| incarnation 迁移 | §5.2.1（:206-223） | 完备 |
| Fan-out/内存预算 knob | §5.5（:241-292） | questions/permissions/merged/absorb/catalog 8 knob 记载（与其余 20+ 零记载形成对照） |

### 3.2 逐场景缺失判定

| 场景族 | operations.md 现状 | 判定 |
|---|---|---|
| 每个 env 的记载 | 72 生产 env 中 operations.md 显式记载约 30（含 §5.5 族）；ETAG/SSE 5/token-stream 4/DIRECTORY_ALLOWLIST/CLIENT_ID_×2/REPLAY_×3/QP_SWEEP_×3/DBAUX_PROBE/COMPRESS_ON_STARTUP 等 20+ 零记载（config-census §1 ops 列、§8 结论 3） | 缺口（F-339 汇总） |
| 503/degraded 场景 | dbaux 熔断有 §7.3；**allowlist 非空 × dbaux 不可用 → 全 503 fail-closed**（v4 §4.2 72 格矩阵最严列）无条目；**search 通配 × db-down → 503** 无条目；transform_busy 持续（池饱和）无排障条目（metrics skeleton 块用法零说明） | 缺口 ×3 |
| 断路器恢复 | §7.3「周期重探（成功→解除）」有；但恢复后 ≤30s 旧库窗口（F-238）、单快探针弱证据振荡（F-239）未提示 | 部分（D07 主辖细节） |
| replay 调优 | **零**：REPLAY_* env、replay 块指标、resync 四因由（epoch_changed/replay_expired/replay_gap/reconnect_no_replay）客户端重连风暴排障全无 | 缺口 |
| allowlist（directory）运维 | **零**：三态语义（None/""/非空，""= /file reject-all 而 SSE hub 放行的不对称）、health `features.allowlist.droppedEvents` 观察、清单变更生效方式（重启）全无——operations.md 唯二 "allowlist" 出现在 :582/:642 均指 action 子进程 env | 缺口（安全相关，D8/F-252 交叉） |
| SSE 订阅饱和 | 旋钮零记载 + `sse.subscribers.{current,limit,rejectedTotal}` 用法零说明 | 缺口 |
| questions/permissions 降级观察 | envelope truncated/errors[]（200 载错误）观察口径零记载 | 缺口（F-331 交叉） |
| metrics 探针 | §9（:475）「查 /slimapi/metrics 的订阅者计数」**不带 `?v=`** —— 照抄即 400 `unsupported_version` | 缺口（F-334） |
| crash-loop 判据 | 无（journal `configuration error` 判据、`systemctl status` activating(auto-restart) 形态、StartLimit 行为） | 缺口（F-344，F-004 展开） |
| 观测面自身健康 | access-log 写失败/disabled、snapshot inactive 的检测手段零记载（仅 journald warning） | 缺口（F-337） |
| health 期望示例 | §6.2（:353-361）`api_version:3/[3,3]/version 1.1.1`、无 auxiliary/allowlist 键——三处滞后（docs-notes D5/D7，A14 交叉） | 缺口（归属 A14） |

### 3.3 runbook 缺口清单（19 条，F-339 汇总载体）

1. `OC_SLIMAPI_DIRECTORY_ALLOWLIST` 三态语义 + 运维动作（安全相关，最高优先）。
2. SSE 控制面 5 旋钮（max_subscribers_per_directory/max_total/queue_items/buffer_bytes/max_frame_bytes）。
3. token-stream 4 旋钮。
4. `REPLAY_COUNT/BYTES_KB/TTL_S`（env 名无 MAX_ 前缀 + KiB 单位陷阱，config-census #69-70）＋ replay resync 排障节。
5. `QP_SWEEP_ENABLED/INTERVAL_SECONDS/DAILY_BUDGET` + sweep 块指标说明。
6. `DBAUX_PROBE_INTERVAL_S`。
7. `CLIENT_ID_HASH/CLIENT_ID_SALT`（隐私回退语义）。
8. `OC_SLIMAPI_ETAG_ENABLED`（含关闭轮换全部 ETag 的副作用，develop.md 有半句）。
9. `TRAFFIC_METRICS_ENABLED`/`ACCESS_LOG_ENABLED`/`TRAFFIC_SNAPSHOT_ENABLED` 关闭后果（含 F-009 清理停摆链）。
10. `ACCESS_LOG_COMPRESS_ON_STARTUP`。
11. allowlist×db-down 全 503 场景动作条目。
12. search 通配×db-down 503 场景。
13. transform_busy 持续 503 排障（skeleton 块读法）。
14. SSE 订阅上限 400 排障与调参。
15. questions/permissions 200 载错误观察口径。
16. metrics/health 探针统一 `?v=3` 提示（§9 :475 修正）。
17. crash-loop journal/systemd 判据 + unit 加固（StartLimitBurst/OnFailure）指引。
18. 观测面自身健康检测（snapshot inactive / access-log disabled / write_access_log failed 关键词）。
19. E-II §11 小节编号修正 + 边界验证结果回填机制。

---

## 4. deploy/oc-slimapi.service 对账裁决（任务 4）

### 4.1 12 条 Environment 逐条裁决（E6 底表 → A13 定级）

| # | 行 | env=值 | 裁决 | 级别 | 归属 |
|---|---|---|---|---|---|
| 1 | :28 | HOST=0.0.0.0 | **有意覆盖**，operations.md §1/§11 一致（E-II 姿态载体；边界依赖已 §11 成文） | 记录性 | — |
| 2 | :29 | PORT=4097 | 冗余（=默认 config.py:357） | 无害 | — |
| 3 | :30 | UPSTREAM=…4096 | 冗余（=默认） | 无害 | — |
| 4 | :31 | MAX_MESSAGE_BYTES=33554432 | 冗余（=默认 32MiB）；**锁旧值风险**：上游默认漂移时模板静默钉死旧值（config-census D6） | 无害+风险注记 | — |
| 5 | :32 | SERVER_API_VERSION=2 | **残留**：env 已废弃，每次启动一条 deprecation warning（config.py:796-804）；operations.md:92-94 声称已清理＝不实陈述 | P3 | **F-005**（已立） |
| 6 | :33 | ACCEPTED_CLIENT_VERSIONS=2,2 | **残留·startup-fatal**：≠钉死 (3,4) → `validate()` RuntimeError（config.py:817-822）→ 照模板部署即 crash-loop | **P1** | **F-004**（已立 verified） |
| 7 | :34 | PYTHONUNBUFFERED=1 | 非 OC_SLIMAPI_ 前缀，journald 实时输出，合规 | 无害 | — |
| 8 | :40 | ACCESS_LOG_DIR=%S/… | 有意覆盖（StateDirectory），operations.md:103 一致 | 无害 | — |
| 9 | :41 | TRAFFIC_SNAPSHOT_PATH=%S/… | 有意覆盖，operations.md:104 一致 | 无害 | — |
| 10 | :45 | TRAFFIC_SNAPSHOT_RETAIN_DAYS=30 | 有意覆盖（默认 0）；operations.md §3.2 内嵌示例**缺此行**（docs-notes D6）但 §5.3/§5.4 文字有值 | 低危漂移 | — |
| 11 | :46 | ACCESS_LOG_RETAIN_DAYS=3 | 有意覆盖（默认 0），operations.md:105 一致；「3 天」实存 4 个日历日（F-336 口径注记） | 无害 | — |
| 12 | :54 | STATE_DIR=%S/oc-slimapi | 有意覆盖，operations.md:106 + §5.2.1 一致 | 无害 | — |

另 :60 注释态 `ACTIONS_FILE`——opt-in 默认关，与 operations.md §12.2/12.3 一致，合规。

### 4.2 指令级裁决（4 条）

| 指令 | 裁决 | 归属 |
|---|---|---|
| `TimeoutStopSec=15`（:24） | **与关停链冲突**：LIFO 清链最坏 = uvicorn 宽限 5s（app.py:97,:780）→ 维护排水 30s（app.py:70 `_MAINT_DRAIN_TIMEOUT`，:695-709）→ dbaux 排水 5s（app.py:85,:610-612，F-236 可再超）→ … → snapshotter 终帧（app.py:304，**最后执行**）。≥40s > 15s → SIGKILL 截断终帧/中间清理；deploy:21-23 注释只对照了 uvicorn 5s，未计 30s 维护排水（D09 :160 同判） | **F-010**（本次 verified） |
| `Restart=on-failure` + `RestartSec=5`（:19-20） | **crash-loop 放大器**：任何启动期致命错（不止 F-004）以 ~2 次/10s 重启，达不到 systemd 默认 start-limit 门槛（burst 5/10s）→ **无限循环**；无 `StartLimitBurst/IntervalSec` 覆盖、无 `OnFailure=` 告警钩子、无 WatchdogSec | **F-344**（新） |
| `MemoryMax=384M`（:70） | 对照 A9 内存上界表（D09 §1）：结构化上界合计 ≈356 MiB + 基线 RSS（unknown，禁实测）——**理论上界已贴近/可能越出 384M**，但需 replay 全满+双类订阅满配+三类缓存同满+64MiB 在制同时达峰（单用户部署不可达）；最大单结构 76 MiB，对单一结构失控余量充足。维持现状合理，无运维调整指引属 runbook 缺口 #19 外事项（记 D13 口径） | 引用 D09（不立新发现） |
| `StateDirectory=oc-slimapi`（:39） | 合规（user service 标准做法，operations.md §5.2 一致） | — |

### 4.3 F-004 运维后果展开（crash-loop 判据）

- **行为链**：unit ExecStart → `main()` 先 `settings.validate()`（app.py:771）→ `(2,2) != (3,4)` → RuntimeError「must be (3, 4) … (got (2, 2))」（config.py:817-822）→ error 日志 `configuration error: …` → `SystemExit(1)`（app.py:772-775）→ systemd Restart=on-failure + RestartSec=5 → 每 ~5s 一轮，**永不达 start-limit**（10s 窗口内仅 ~2 次 < burst 5）。
- **journal 判据**：`journalctl --user -u oc-slimapi` 周期性出现 `configuration error: accepted client versions must be (3, 4)`；`systemctl --user status oc-slimapi` 恒 `activating (auto-restart)`；`curl :4097/slimapi/health` 连接拒绝。
- **告警面**：无——unit 无 OnFailure/WatchdogSec，metrics 端点不可达是唯一外部信号（探针需带 `?v=3`，F-334 叠加：不带参数的探针在服务正常时也 400，探针设计必须区分「连接拒绝=宕机」vs「400=探针写错」）。
- **修复**（F-004 建议方向已载）：删 :32-33；同步 operations.md:92-94 措辞；release checklist 加 deploy↔config 对账项（F-344 补 unit 加固）。

---

## 5. 告警建议（advisory，任务 5）

> 全部基于现有 `GET /slimapi/metrics?v=3` 字段 + journald 关键词，标 ▲ 的两项需先补观测位（对应发现）。

| # | 信号 | 源 | 建议阈值 / 动作 | 依据 |
|---|---|---|---|---|
| 1 | dbaux 熔断开启 | `metrics.dbaux.breaker_open == true` | 持续 >2 探针周期（60s）→ 页面；恢复后自动清除 | §7.3 P99>20ms 熔断 |
| 2 | dbaux 延迟预警 | `metrics.dbaux.latency.p99_ms` | >15ms 预警（熔断线 20ms 前 75%） | §7.2 |
| 3 | sessions fail-closed 503 | `metrics.sessionsDegraded.fail_closed_503` 增速 | >0/min 即告警（任何 fail-closed 都该看） | v4 §4.2 |
| 4 | sessions 常态化降级 | `sessionsDegraded.degraded_200` / traffic.buckets.sessions.requests | 占比 >50% 持续 1h → 提醒（dbaux 长期不可用） | v4 §9.1 |
| 5 | 上游断连 barrier | `metrics.replay.barriers > 0` 且递增 | 出现即告警（上游 /global/event 断连，订阅者将收 resync） | v4 §7.2 |
| 6 | replay 退化 | replay counters `replay_expired`/`replay_gap` 增速 | >0/min → 提醒（客户端重连风暴/日志逐出过快） | v4 §7.2 |
| 7 | sweep 预算耗尽 | `metrics.sweep.budget_exhausted` 递增 | 连续 3 天增长 → 提醒（QP 影子扫描饿死） | qp_sweep.py:244 |
| 8 | sweep 全跳过 | `metrics.sweep.skips` / `triggers_total` | 比值 ≈1 持续 1 天 → 检查 interval/budget 配置 | F-273 交叉 |
| 9 | SSE 订阅饱和 | `metrics.sse.subscribers.{current,limit}`（默认 8/16） | current/limit > 0.8 预警；`rejectedTotal` 递增 = 已拒即告警 | config #21-25 |
| 10 | token 流饱和 | `metrics.sse.tokenStream.{current,limit}` + `maxSubscriberQueueDepth` | 同上；queueDepth 持续 > `sse_queue_items/2`（128）→ 背压预警 | config #26-29 |
| 11 | 订阅者丢帧/强断 | `metrics.sse.clients[].droppedFramesTotal` / `forcedDisconnectsTotal` | 任一递增即提醒（背压/协议断连） | registry.py:327-338 |
| 12 | 上游 SSE 不稳 | `metrics.sse.hubs[].reconnectsTotal` 增速 | >1/10min → 提醒 | F-275 交叉 |
| 13 | transform 饱和 | `metrics.skeleton.{activeTransforms,waitingTransforms}` | active==max_transforms 且 waiting>0 持续 30s → 503 前兆 | §5.5 absorb 语境 |
| 14 | ▲ 观测面写失败 | journald `write_access_log failed` / `traffic snapshotter start failed` / `access log maintenance did not drain` | 出现即提醒（当前唯一信号是 journald——建议提升为 metrics 健康块，F-337） | app.py:246-250,704-708 |
| 15 | crash-loop | journald `configuration error` + systemd `activating (auto-restart)` | 出现即紧急（F-004/F-344；建议 unit 加 OnFailure） | §4.3 |
| 16 | actions 失败率 | journald `-p warning \| rg action`（audit 行） | action_timeout/action_busy 频发 → 检查 manifest timeout/并发 | §12.3 |
| 17 | 磁盘 | logs 目录体积 + `access-legacy-*.jsonl.gz` 存在 | 目录 >500MB 或 legacy 文件出现 → 手动清理（F-008：永不清除） | §5.4 |
| 18 | ▲ catch-all 丢弃漂移 | 无（F-216：零观测） | 建议 `upstream_dropped_events_total{type}` 低基数表后，未知类型出现即告警 | F-216 建议方向 |

---

## 6. 发现索引（A13 产出）

- 主辖更新（5）：F-008（verified，补实证）、F-009（verified，补双触发路径）、F-010（verified，补关停链常量与 LIFO 序）、F-022（verified，扩展单查族 4 个 raise 点）、F-023（维持 P3，A13 补 runbook 侧口径）。
- 新建（15）：F-331（观测无错误码维度/200 载错误，P3）、F-332（passthrough 桶语义失真，P3）、F-333（sweep 双盲+enabled 兜底反向，P3）、F-334（metrics 探针 `?v=` 陷阱，P3）、F-335（recordType 陷阱文档缺口+聚合函数不过滤，P3）、F-336（retain 口径双偏差+对账窗错配，P3）、F-337（观测面自身健康无观测位，P3）、F-338（sse_observability 静默吞异常，P3）、F-339（runbook 缺口汇总 19 条，P2）、F-340（sessionsDegraded 丢计数无信号，P3）、F-341（status=0 记账黑洞，P3）、F-342（clientId 无 salt 可链接+明文回退零告警，P3）、F-343（providers 桶裸路径归桶陷阱，P3）、F-344（unit 无 start-limit/OnFailure 加固，P3）、F-345（记账三段 best-effort 无对账+异常标签误导，P3）。
- 交叉引用不重复立项：F-004/F-005（deploy 残留）、F-216（catch-all）、F-236/F-238/F-239（dbaux 关停/熔断细节，D07）、D09 内存上界（MemoryMax 对照）、D5/D6/D7（docs 滞后，A14 主辖）。
- INDEX.md 未同步（写入白名单限制）；由协调方或 Phase 3 归档时合并。
