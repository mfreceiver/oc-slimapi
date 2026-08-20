# E7 测试普查（test-census.md）

> 审计对象：`tests/`（109 个 .py = 107 个 `test_*.py` + `conftest.py` + `v4_fixture.py`）。
> 计数方式：`wc -l` / `rg -c "def test_"`（**未运行 pytest**，收集数与实际对比由主审计另行执行）。
> 路由对齐基准：`docs/audits/2026-08-20/01-explore/route-census.md`（54 路由）。
> 证据基线：本文件全部 `路径:行号` 均为 rg/读文件实取。

## 0. 总量摘要

| 指标 | 值 |
|---|---|
| .py 文件数 | **109**（107 test + conftest + v4_fixture） |
| 总行数 | **66,277** |
| test 函数总数（`def test_`） | **2642**（与预期 ≈2642 一致） |
| 使用 `upstream_factory` 的测试文件 | 30 |
| import `v4_fixture` 的测试文件 | 8 |
| 直接驱动 `tests/golden/` 的测试文件 | **1**（test_equivalence_anchor.py；另 v4_fixture.py 自身） |
| v3+v4 双 wire 视图同文件锁定 | **12** 文件（其中 `?v=3`+`?v=4` 字面双 selector 10 文件） |
| readiness 门控（SATISFIED 开/关）双测 | **10** 文件 |
| 功能开关双测（coalesce/etag/fingerprint/ttl off 态回归） | ≥4 文件 |
| sleep 相关证据行 | 191（其中 `sleep(0)` 协作让步 25；sleep 注入/FakeClock 补丁面 45 行） |
| 真实时钟读取（time.time/monotonic/datetime.now） | 27 处 |
| random/urandom | 13 处 |
| respx 使用 | **0**（pyproject 声明了依赖但全仓零引用——见 §4） |
| httpx.MockTransport | 37 文件 63 处；ASGITransport 69 文件 |

---

## 1. 每测试文件一行总表（109 行）

缩写：**UF** = conftest `upstream_factory`；**V4F** = `tests/v4_fixture.py`；**自建** = 文件内自建 fixture/直接构造（多含直建 `httpx.MockTransport`）；**G** = `tests/golden/` 金样驱动。双态列：`v3+v4` = 同文件双 wire 视图；`gate` = readiness.SATISFIED 开关双测；`flag` = 功能开关 off 态回归；`单态` = 仅单版本/无版本面。路由列对齐 E2 census 54 路由（写法如 `GET sessions` = `GET /slimapi/sessions`）。

| # | 文件 | 行数 | test数 | 覆盖路由（→census）/ 模块 | fixture | 双态锁定 | 金样 |
|---|---|---|---|---|---|---|---|
| 1 | test_access_log.py | 1142 | 50 | 模块 `access_log`（落盘/轮转/压缩/迁移，非路由） | 自建（autouse 状态复位） | 单态 | — |
| 2 | test_access_log_v3_fields.py | 378 | 16 | GET agent/events/health/messages/sessions/versions 的 access-log §9.1 字段 | 自建 | v3 单态（wireVersion 维度） | — |
| 3 | test_actions.py | 1018 | 55 | GET+POST actions 核心执行器（无路由层，spawn 真实短命子进程） | 自建（真实 subprocess） | 单态 | — |
| 4 | test_actions_routes.py | 607 | 24 | GET actions + POST actions/{name} | 自建 | 单态 | — |
| 5 | test_agent_routes.py | 702 | 18 | GET agent（skeleton 目录投影） | UF | 单态 | — |
| 6 | test_app_main.py | 21 | 1 | 模块 app.main（shutdown timeout 透传） | 自建 | — | — |
| 7 | test_b1a_digest_changed.py | 280 | 7 | 模块 sse hub `session.digest.changed` | 自建 | 单态 | — |
| 8 | test_b1b_sweep_shadow.py | 466 | 32 | `/slimapi/_shadow/sweep` + GET metrics（shadow 面，**54 census 之外**的内测路由） | 自建 | 单态 | — |
| 9 | test_b2_merged_text_compat.py | 285 | 6 | GET messages + expand×2（TextPart 恒内联，双投影模式） | UF | flag（skeleton+merged 双投影） | — |
| 10 | test_b4_allowlist.py | 564 | 23 | GET file / file/content / file/status + health（allowlist fail-closed） | 自建（MockTransport） | 单态 | — |
| 11 | test_b4_new_routes.py | 415 | 19 | POST agent/model/context/revert/stage/clear/commit（7 新收编路由） | 自建 | 单态 | — |
| 12 | test_batch3_lifecycle.py | 1157 | 40 | 模块：SSE/Token 生命周期状态机（无路由） | 自建（含 sleep patch 面） | 单态 | — |
| 13 | test_catalog_cache.py | 722 | 34 | GET agent + command（TTL catalog cache C1..C6） | 自建（MockTransport） | flag（ttl=0 关态） | — |
| 14 | test_check_routes_doc.py | 283 | 18 | 模块 `scripts/check_routes_doc.py`（路由↔文档对账工具） | 自建 | — | — |
| 15 | test_children_routes.py | 424 | 18 | GET sessions/{sid}/children | UF | 单态 | — |
| 16 | test_command_routes.py | 611 | 18 | GET command（skeleton 目录） | UF | 单态 | — |
| 17 | test_config.py | 582 | 66 | 模块 config.Settings.validate（bind-host/upstream 守卫） | 自建 | — | — |
| 18 | test_cursor_matrix.py | 378 | 36 | 模块 v4 cursor 编解码/指纹/边界矩阵（§11.4） | 自建 | v4 单态 | — |
| 19 | test_dbaux_lifecycle.py | 754 | 34 | 模块 dbaux 连接生命周期 + GET health auxiliary 三态（真形状 schema 库自建） | 自建（FakeClock 注入） | 单态 | — |
| 20 | test_dbaux_metrics.py | 250 | 9 | GET metrics（dbaux 观测块三态） | V4F | 单态 | — |
| 21 | test_db_path_resolution.py | 172 | 14 | 模块 dbaux.path_resolution（§3.4 11 case） | 自建（env/home 全注入） | — | — |
| 22 | test_degraded_observability.py | 728 | 20 | GET sessions + metrics + health（降级观测全链路 BLOCKER-4） | V4F | v3+v4 + gate | — |
| 23 | test_diff_routes.py | 506 | 21 | GET sessions/{sid}/diff | UF | 单态 | — |
| 24 | test_directories_routes.py | 538 | 24 | GET directories | UF | 单态 | — |
| 25 | test_directory.py | 85 | 17 | 模块 directory 校验（S5） | 自建 | — | — |
| 26 | test_eqp_matrix.py | 87 | 1 | 模块：SQL EQP 48 组合 planner 断言（复用 scripts/eqp_matrix.py + B2 组装器） | V4F（+eqp 装载） | 单态 | — |
| 27 | test_equivalence_anchor.py | 1444 | 28 | dbaux 投影 vs 上游 `/experimental/session` 等价锚定 EQ-001..008（sessions 路由语义层） | V4F + 真实二进制 | 双权威源（golden+真进程） | **G×2**（mirror+real golden） |
| 28 | test_errors.py | 79 | 2 | 错误信封注册 + GET messages 边界 | UF | — | — |
| 29 | test_etag.py | 1241 | 38 | GET sessions/messages/agent/command ETag/304（B1-C1..C7） | UF | 单态 | — |
| 30 | test_events_tokens.py | 674 | 12 | GET events?tokens=1（L2-A token 帧） | 自建 | 单态 | — |
| 31 | test_expand_config.py | 413 | 24 | GET versions capabilities + Settings expand 面 | 自建 | 单态 | — |
| 32 | test_expand_href_v4.py | 423 | 11 | GET messages + expand×N（§14 href per wire view，v3 字节回归） | UF | **v3+v4** | — |
| 33 | test_expand_routes.py | 1356 | 57 | GET messages/{sid}/expand/{category}/{mid}[/partID] + full/{mid} + selector 面 | UF | 单态 | — |
| 34 | test_full_absorb.py | 563 | 14 | GET messages/{sid}/full/{mid}（single-flight + absorb 预算） | UF | 单态 | — |
| 35 | test_globalhub_retired_gate.py | 325 | 9 | 模块 sse.global_hub `_retired_messages` 门 | 自建 | 单态 | — |
| 36 | test_gzip_negotiation.py | 170 | 25 | 模块 gzip 协商 + json_response + 压缩门槛 | 自建 | — | — |
| 37 | test_health.py | 499 | 16 | GET health + ready（§9 gzip 清理） | UF | 单态 | — |
| 38 | test_health_dual_view.py | 121 | 6 | GET health/ready（v3 单视图收口终态） | 自建 | v3 单态（收口锁定） | — |
| 39 | test_health_features.py | 19 | 1 | GET health features 广告（app.state 预填） | 自建 | — | — |
| 40 | test_hub.py | 1582 | 67 | 模块 sse hub（策展 digest/q/p 直推契约） | 自建 | 单态 | — |
| 41 | test_hub_behavior_lock.py | 1841 | 119 | 模块 sse hub 行为锁（拆分前基线，自含 fixture 不碰 conftest） | 自建 | 单态 | — |
| 42 | test_leased_singleflight.py | 802 | 23 | 模块 LeasedSingleFlight（A2 九类释放） | 自建 | 单态 | — |
| 43 | test_lifespan.py | 304 | 9 | 模块 app lifespan 事务性启动回滚（P0-1） | 自建 | — | — |
| 44 | test_logging_config.py | 133 | 6 | 模块 logging_config | 自建 | — | — |
| 45 | test_message_fingerprint.py | 594 | 28 | GET messages/{sid} 内容指纹（B3-C3..C5） | UF | **flag**（fingerprint off 态字节回归） | 设计文档固定 golden vector（非 tests/golden） |
| 46 | test_messages_coalesce.py | 647 | 16 | GET messages/{sid} join-first 合流（A2-C1..C6） | UF | **flag**（coalesce_enabled=false 旁路） | — |
| 47 | test_messages_merged.py | 922 | 13 | GET messages?mode=merged + full/{mid}（CD2-C1..C5） | UF | 单态 | — |
| 48 | test_messages_routes.py | 1583 | 46 | GET messages + full + since + POST question reply/reject + permissions + children + sessions/status 多路由边界 | UF | 单态 | — |
| 49 | test_method_boundary_v4.py | 530 | 16 | POST session/{sid}、archive、delete 的 405/Allow（§16） | UF | **v3+v4 + gate** | — |
| 50 | test_metrics.py | 194 | 5 | GET metrics（Lane-H/T3 wire 面） | 自建 | 单态 | — |
| 51 | test_metrics_replay_block.py | 196 | 5 | GET metrics replay 观测块（B3b-5） | 自建 | 单态 | — |
| 52 | test_permissions.py | 1026 | 24 | GET permissions（跨目录聚合） | UF | 单态 | — |
| 53 | test_post_actions_v4.py | 650 | 29 | POST session/{sid}≡PATCH、delete≡DELETE、archive 合成（§16 修订二） | UF | **v3+v4 + gate** | — |
| 54 | test_providers_projection_v4.py | 1184 | 51 | GET config/providers（§12 安全投影 TDD） | 自建（MockTransport） | **v3+v4** | — |
| 55 | test_proxy.py | 194 | 8 | catch-all 404 thin_route_not_found + WS 501（§8.2 终态） | 自建（install_proxy） | 单态（终态） | — |
| 56 | test_proxy_sse_observability.py | 123 | 2 | /event、/global/event 关闭后 SSE 观测终态 | 自建 | 单态（终态） | — |
| 57 | test_questions_coalesce.py | 705 | 14 | GET questions + permissions 两级合流（A4） | UF | 单态 | — |
| 58 | test_questions_routes.py | 1536 | 37 | GET questions（跨目录聚合路由） | UF | 单态 | — |
| 59 | test_read_groups.py | 1025 | 59 | 7 读组 13 路由：file×3、vcs×3、find/file、config/providers、session/{sid}、api/session/active、global/health | 自建（MockTransport） | 单态 | — |
| 60 | test_readiness_gating_integration.py | 373 | 6 | 跨批次 §3.3 门控接线（providers/messages/sessions/session single/method boundary/versions） | 自建 | **v3+v4 + gate** | — |
| 61 | test_replay_log.py | 757 | 51 | 模块 ReplayLog（B3b-1 日志层 REPLAY-001..018 之日志半） | 自建（FakeClock） | 单态 | — |
| 62 | test_request_id.py | 341 | 12 | 模块 RequestIdMiddleware（纯 ASGI） | 自建 | — | — |
| 63 | test_selector.py | 317 | 23 | 模块 SlimapiSelectorMiddleware 端到端（health/versions 承载） | 自建 | **v3+v4**（双版本窗口） | — |
| 64 | test_selector_query_strip.py | 139 | 9 | selector `?v` 剥离语义（health/versions 承载） | 自建 | v3+v4 | — |
| 65 | test_session_single_v4.py | 1238 | 50 | GET session/{sid} + sessions canonical projector（§13） | V4F | **gate**（SATISFIED patch 双态） | — |
| 66 | test_session_status_object_format.py | 395 | 15 | 模块 global_hub `session.status` 对象信封（Bug A 回归） | 自建 | 单态 | — |
| 67 | test_sessions_coalesce.py | 572 | 12 | GET sessions + sessions/status 合流（A3） | UF | 单态 | — |
| 68 | test_sessions_routes.py | 774 | 29 | GET sessions + sessions/status | UF | 单态 | — |
| 69 | test_sessions_v4_matrix.py | 1103 | 36 | GET sessions（144 等价类降级矩阵 §4.2/§11.3） | V4F | **v3+v4 + gate** | — |
| 70 | test_sessions_v4_representation.py | 503 | 10 | GET sessions（§15 Vary+ETag/304） | V4F | **gate**（SATISFIED 排除/纳入双态） | — |
| 71 | test_skeleton.py | 839 | 50 | 模块 skeleton 投影（纯函数） | 自建 | 单态 | `tests/fixtures/msg40.json`（in-tree 443KB 真实形状数据） |
| 72 | test_skeleton_expand.py | 544 | 36 | 模块 skeleton expand（Lane A §4/§5） | 自建 | 单态 | — |
| 73 | test_smoke.py | 199 | 10 | 模块 app.smoke（schema 探测分支） | 自建（MockTransport） | — | — |
| 74 | test_sql_semantics.py | 273 | 19 | dbaux SQL 语义 19 case（§9.5，对照镜像 oracle） | V4F（+sqlite） | 单态 | — |
| 75 | test_sse_logging.py | 138 | 4 | 模块 SSE hub 诊断日志 | 自建 | 单态 | — |
| 76 | test_sse_replay_wire.py | 2586 | 73 | GET events + sessions/{sid}/stream（B3b-2 wire 层 id:/replay + v3 字节锚） | 自建 | **v3+v4**（v3 无 id 字节锚定） | — |
| 77 | test_terminal_matrix.py | 506 | 24 | selector 终态矩阵（12 路由承载：agent/events/file/global health/health/messages/sessions/session/command/stream/vcs/versions） | 自建（MockTransport） | v3+v4 窗口 [3,4] | — |
| 78 | test_todo_routes.py | 422 | 18 | GET sessions/{sid}/todo | UF | 单态 | — |
| 79 | test_token_hub.py | 999 | 63 | 模块 tokenstream accumulator Stage-A（数据结构/ingest） | 自建 | 单态 | — |
| 80 | test_token_hub_flush.py | 1653 | 81 | 模块 flush 引擎 Stage-C（100ms 节拍/内存预算） | 自建（sleep patch） | 单态 | — |
| 81 | test_token_hub_lifecycle.py | 981 | 52 | 模块 accumulator Stage-B（生命周期/tombstone） | 自建（sleep patch） | 单态 | — |
| 82 | test_token_stream_route.py | 1484 | 49 | GET sessions/{sid}/stream Stage-D + events/health/metrics 承载 | 自建 | 单态 | — |
| 83 | test_token_subscriber_overflow.py | 873 | 37 | 模块 TokenSubscriber T3 三段防线（隔离无 hub） | 自建 | 单态 | — |
| 84 | test_traffic_integration.py | 441 | 6 | GET messages + metrics（traffic 端到端 additivity） | UF | 单态 | — |
| 85 | test_traffic_latency.py | 109 | 5 | 模块 TrafficLedger 百分位/错误计数（M5） | 自建 | — | — |
| 86 | test_traffic_ledger.py | 651 | 56 | 模块 TrafficLedger + bucketize + stash | 自建 | 单态 | — |
| 87 | test_traffic_middleware.py | 972 | 25 | 模块 TrafficAccountingMiddleware（纯 ASGI 包裹） | 自建 | 单态 | — |
| 88 | test_traffic_snapshot.py | 1199 | 43 | 模块 TrafficSnapshotter（周期快照循环） | 自建（datetime 计数补丁） | 单态 | — |
| 89 | test_traffic_snapshot_v3.py | 333 | 20 | 模块 snapshot §9.2 聚合矩阵 + sseActive 结转 | 自建 | v3 单态 | — |
| 90 | test_traffic_sse.py | 621 | 4 | GET events SSE 省流端到端（upIn>downOut 实证） | UF | 单态 | — |
| 91 | test_traffic_upin_gaps.py | 352 | 6 | GET ready/sessions/messages/session command（upIn/upOut 缺口修复） | UF | 单态 | — |
| 92 | test_transform.py | 482 | 24 | 模块 transform pool / cap reader / 线程池隔离 | 自建 | 单态 | — |
| 93 | test_turn_registry.py | 781 | 34 | 模块 turn_registry + POST prompt_async/abort 戳记（承载 2 写路由） | UF | 单态 | — |
| 94 | test_upstream.py | 208 | 17 | 模块 upstream strip_hop_by_hop（M1） | 自建 | — | — |
| 95 | test_upstream_error_boundary.py | 161 | 2 | GET sessions/status + messages + full 上游错误边界（§7 TDD） | 自建 | 单态 | — |
| 96 | test_v3_directory.py | 720 | 26 | directory 查询矩阵（§5；15 路由承载：agent/command/directories/events/health/messages/questions/permissions/sessions×5/versions） | 自建 | v3 单态 | — |
| 97 | test_v3_envelope.py | 329 | 11 | GET messages/sessions/sessions/status 信封（§4） | 自建 | v3 单态 | — |
| 98 | test_v3_etag_domain.py | 252 | 8 | GET sessions/messages ETag 域隔离（§6） | 自建 | v3 单态 | — |
| 99 | test_v3_rawbody_regression.py | 263 | 4 | GET sessions/health/versions 逐字节基线（rev gate MINOR-1） | 自建（`PYTHONHASHSEED=0` 子进程取现场，tests/test_v3_rawbody_regression.py:184-187） | v3 单态 | 文件内嵌字节基线（`--capture` 再生成） |
| 100 | test_v3_sse_meta.py | 486 | 15 | GET events + sessions/{sid}/stream `slimapi.meta` 首帧（§7.2） | 自建 | v3 单态 | — |
| 101 | test_v4_dual_window.py | 590 | 31 | selector 双版本窗口 (3,4) 矩阵（**~35 路由全扫**：A1 版本钉死/A2 selector/A3+ 逐路由 v4 面） | 自建 | **v3+v4 + gate** | — |
| 102 | test_v4_observability.py | 323 | 12 | 观测维度 v4 值集扩面（events/health/sessions 承载，A4） | 自建 | **v3+v4** | — |
| 103 | test_vary_directory_unconditional.py | 341 | 11 | GET messages full + agent/command/sessions/children/diff/todo Vary 矩阵（B1+C3） | 自建 | **flag**（`OC_SLIMAPI_ETAG_ENABLED=false` 降级态） | — |
| 104 | test_versions_readiness.py | 595 | 35 | GET versions readiness 门（§3.3 十 feature 全矩阵） | 自建 | **v3+v4 + gate** | — |
| 105 | test_versions_route.py | 172 | 9 | GET versions 发现端点（§3 终态） | 自建 | 单态 | — |
| 106 | test_wal_staleness.py | 167 | 1 | dbaux WAL 陈旧读守护（immutable=1 弃用裁决，B0-6a；真实 WAL 库 + 持开 writer） | 自建（真实 sqlite WAL） | 单态 | — |
| 107 | test_write_groups.py | 557 | 27 | §10.b 12 写端点（POST session/PATCH/DELETE/abort/command/fork/permissions/prompt_async/revert/summarize + question reply/reject） | 自建 | 单态 | — |
| 108 | conftest.py | 26 | 0 | 共享 fixture `upstream_factory`（见 §7） | — | — | — |
| 109 | v4_fixture.py | 884 | 0 | 测试基建：七维度数据集/镜像 oracle/golden 生成与校验（见 §6/§7） | — | — | G（生成器+校验器本体） |

### 1.1 路由覆盖对齐结论（对 E2 census 54 路由）

- **54/54 路由均有测试命中**（专测文件或矩阵承载文件），无零测试路由。覆盖最广的三个文件：`test_v4_dual_window.py`（~35 路由 v3/v4 全扫）、`test_v3_directory.py`（15 路由 directory 矩阵承载）、`test_read_groups.py`（13 读组路由专测）。
- POST question reply/reject 的专测在 `test_messages_routes.py:46`（文件头注明多路由边界）与 `test_v4_dual_window.py`，`test_write_groups.py` 覆盖其余写组。
- `/slimapi/_shadow/sweep`（test_b1b_sweep_shadow.py:50）**不在 54 census 内**——内测/运维 shadow 路由，属 census 口径外发现（与 E2 的「实现 54」声明一致，非矛盾，但文档层 INTERFACE_MAP 是否记载数值得 A 组复核）。
- 版本面：`POST session/{sid}`、`/archive`、`/delete` 为 v4-only 路由（census 版本面列 v4），测试集中在 test_post_actions_v4 / test_method_boundary_v4（均做 v3 侧 404/thin_route_not_found 对照，即双态）。

---

## 2. 时间敏感清单

计数（rg 全量）：`sleep` 相关证据行 **191**（36 文件）；其中 `sleep(0)` 协作让步 25 行；sleep 注入/FakeClock 补丁面 45 行；真实时钟读取 27 处；`os.urandom`/`random` 13 处。分类：**[真延迟]**（真实等待墙钟）/ **[让步]**（`sleep(0)` 纯调度让出，无墙钟依赖）/ **[可控注入]**（patch sleep/FakeClock，时间被虚拟化）/ **[真实时钟]**（读 `time.time`/`monotonic`/`datetime.now` 参与断言，有环境敏感性）。

### 2.1 真延迟（重点：>0.5s 的项加粗）

| 证据 | 类别 | 说明 |
|---|---|---|
| test_actions.py:949 | [真延迟] | **4.0s** 等逃逸孙进程自毁 |
| test_full_absorb.py:241 | [真延迟] | **3.5s** 超出 2.5s absorb 预算 |
| test_transform.py:385 | [真延迟] | executor 内 `time.sleep(3.0)` 占满线程池 |
| test_full_absorb.py:164 | [真延迟] | **2.2s** 占用窗（2.0s<t<2.5s 预算） |
| test_catalog_cache.py:181,371,526 | [真延迟] | **1.2s×3** TTL 过期 + singleflight 宽限 |
| test_etag.py:485 | [真延迟] | **1.2s** TTL 过期 |
| test_message_fingerprint.py:412 | [真延迟] | **1.2s** |
| test_expand_routes.py:1025 | [真延迟] | **1.1s** `_DEFAULT_RESULT_GRACE_SECONDS==1.0` |
| test_hub_behavior_lock.py:424 | [真延迟] | **0.5s** |
| test_messages_routes.py:318 | [真延迟] | **0.5s** worker 线程内 `time.sleep`（配 :336 0.02s 让步 + :338-340 monotonic 断言） |
| test_actions.py:397-398,852-853,402,666,821,894,903 等 | [真延迟] | 子进程 argv `time.sleep(5)/(2)` + 0.05-0.3s 窗口 + :796-797,836-837 monotonic 轮询 deadline 4.0s |
| test_actions_routes.py:376,405 | [真延迟] | 子进程 argv `time.sleep(5)` |
| test_batch3_lifecycle.py:316,348,379 | [真延迟] | `k*TOKEN_FLUSH_SECONDS+0.05`；:502,545 handler 内 **10.0s**（被取消路径，实际不等待全程）；:105,146,180,212,239,274,505,547 0.02-0.05s 窗口；:70 `sleep(0)` |
| test_leased_singleflight.py | [真延迟] | 0.12s/0.15s 窗口 ×17 处（:61,94,160,204,251,324,401,502,589,606,660,717,767,800 及 :270,276 细分）；:32,780 sleep(0) |
| test_traffic_snapshot.py | [真延迟] | 0.05-0.15s ×11 处（:155,166,190,280,376,485,527,531,701,767,838） |
| test_sessions_coalesce.py | [真延迟] | :459 `time.sleep(0.15)`、:545 `time.sleep(0.08)`（线程内持锁）+ :148,232,240,366,413,448,502 0.02-0.05s |
| test_messages_coalesce.py | [真延迟] | :474 `time.sleep(0.08)`、:625 `time.sleep(0.15)` + :323,362,485,518 0.001-0.05s |
| test_traffic_sse.py:279,295,427,439 | [真延迟] | 0.05s 窗口 ×4 |
| test_access_log.py:620,648,763,779,1133 | [真延迟] | 0.02-0.15s 落盘/维护循环节拍 |
| test_b1b_sweep_shadow.py:50,252,287,292,312,316 | [真延迟] | 0.018-0.34s sweep 节拍 |
| test_sse_replay_wire.py:788,2431,2454,2456 | [真延迟] | 0.03-0.09s |
| test_messages_merged.py:420,533,688,774,907 | [真延迟] | 0.05-0.3s join 窗口 |
| test_children_routes.py:361 / test_todo_routes.py:353 / test_diff_routes.py:438 | [真延迟] | 0.15s 各一 |
| test_token_hub_flush.py:384 | [真延迟] | `TOKEN_FLUSH_SECONDS*3+0.05` 真等 flush 周期 |
| test_events_tokens.py:602 / test_permissions.py:828 / test_dbaux_lifecycle.py:383 / test_globalhub_retired_gate.py:58 / test_lifespan.py:213,233 / test_transform.py:354,359,437,473 / test_questions_coalesce.py:273,661 / test_expand_routes.py:935 / test_questions_routes.py:1282 / test_traffic_snapshot.py:（上列） | [真延迟] | 0.05-0.25s 竞态窗口/节拍 |

### 2.2 可控注入（时间虚拟化——不是延迟问题）

| 证据 | 机制 |
|---|---|
| test_batch3_lifecycle.py:1036-1100 | `counting_sleep`/`fast_sleep` 替换 `gh_mod.asyncio.sleep`（手动 set/restore） |
| test_token_hub_lifecycle.py:258-412（×4 处） | `monkeypatch.setattr("oc_slimapi.sse.global_hub.asyncio.sleep", _fast)`；:268 注释明确用真实挂起替代被 patch 的 sleep |
| test_token_hub_flush.py:406-446（×2 处） | 同上，patch `tokenstream.hub.asyncio.sleep` |
| test_dbaux_lifecycle.py:453-510 | 自建 `FakeClock`（dbaux P99/熔断时间全虚拟化） |
| test_replay_log.py:44-86 | 自建 `FakeClock` 注入 ReplayLog（TTL/环形逐出免等待） |
| test_post_actions_v4.py:431 | `monkeypatch.setattr(write_groups, "time", _FrozenClock())`（墙钟 epoch-ms 冻结） |
| test_traffic_snapshot.py:720-743 | `_CountingDateTime` 包裹 `datetime.now` 并计数（P1-26：每帧恰好一次调用） |

### 2.3 真实时钟读取（危险/敏感项）

| 证据 | 风险 |
|---|---|
| test_globalhub_retired_gate.py:325 | `int(time.time()*1000)` 构造时间戳参与语义（对时钟敏感，好在仅单调性/置性断言） |
| test_b1b_sweep_shadow.py:149-151 | `before <= qp_last_activity <= after` 时间戳夹逼断言（时钟回拨/NTP 环境下可脆弱） |
| test_post_actions_v4.py:406-409,604-608 | `before_ms/after_ms` 用 `time.time()*1000` 夹逼 wall-clock 戳（容忍型，低危） |
| test_actions.py:796-797,836-837；test_equivalence_anchor.py:723-725 | monotonic 轮询 deadline（4s/40s 上限轮询，非断言本体） |
| test_transform.py:358-390；test_full_absorb.py:180-197；test_messages_routes.py:338-340 | monotonic 计时参与**行为断言**（事件循环自由度/占用时长/health 阻塞时长——机器高负载时可能假失败） |
| test_traffic_snapshot.py:514 | 注释确认生产 `datetime.now().astimezone()`（测试侧已被 _CountingDateTime 隔离） |
| test_equivalence_anchor.py:1048 | golden builder `generated_at=datetime.now(timezone.utc)`（真值入 golden 头；CI 校验仅验 ISO+tz 格式，不比对具体时刻——设计自洽） |

### 2.4 random

| 证据 | 分类 |
|---|---|
| test_etag.py:1034,1036,1099,1101,1214,1216 | `os.urandom(600)` 循环再生成直至 gzip 确证膨胀——结果确定性，输入随机（安全用法） |
| test_gzip_negotiation.py:137 | `os.urandom(512)` 不可压缩体（安全用法） |
| test_equivalence_anchor.py:399-403 | `random.shuffle(candidates)` 打乱注入顺序（鲁棒性用法，不进断言值） |
| tests/v4_fixture.py:86-87 | **刻意规避** `hash()`（跨进程随机化），改 sha256 派生——golden 确定性正面案例 |

### 2.5 顺带发现（时间面）

- `test_v3_rawbody_regression.py:184-187`：sessions 键序跨进程随 `PYTHONHASHSEED` 漂移，测试以固定 seed 子进程取现场——**已知非确定性被显式围栏**，基线锚定与生产跨进程字节稳定性是两回事（A 组可关注 `skeleton._pick` 对 set 迭代的键序问题是否应修）。
- 最重串行真延迟粗估 >30s（4.0+3.5+3.0+2.2+1.2×5+1.1+0.5×3+大量 0.1-0.2s 窗口），集中在 test_actions / test_full_absorb / test_transform / test_catalog_cache / test_leased_singleflight。

---

## 3. 真实资源清单

### 3.1 真实 sqlite（均建于 tmp_path 临时目录，无生产库写入）

| 文件 | 用法 | 备注 |
|---|---|---|
| test_wal_staleness.py | 真实 WAL 模式库：主库 commit 1 行 + `-wal` 内 commit 第 2 行 + WAL 期新表，**writer 连接持开**（fixture teardown 统一关闭，:33-40 注释） | 唯一验证 WAL 语义本身的测试；1 test/167 行 |
| test_eqp_matrix.py | 经 V4F 装载 `scripts/eqp_matrix.py` 的 DDL/行集 oracle，48 组合 EQP | draft DB 无索引 → SCAN+TEMP B-TREE 断言（文件头声明真库 SEARCH 差异属上游索引面） |
| test_sql_semantics.py | 19 case 逐条对照 `v4_fixture.mirror_page` | sqlite3 直查 |
| test_sessions_v4_matrix.py | fixture DB 上真实 `DbAuxiliarySource`（avail）+ `_StubAux`/`_BusyAux`（disabled/tripped/busy） | busy 注入 = 查询期 sqlite 异常 → 503 不泄细节 |
| test_session_single_v4.py | V4F fixture DB + 真实 DbAuxiliarySource 投影 | |
| test_dbaux_lifecycle.py | 自建**真形状 schema 库**（§6.1 全投影列 + project join 列，:40） | inode swap/P99 熔断用 |
| test_equivalence_anchor.py | **真实 opencode 实例的 DB**（隔离 ephemeral 实例，`_real_source(real_upstream.db_path)`）+ fixture DB | 唯一接触真实上游产物 DB 的文件 |
| v4_fixture.py:214-261 `build_fixture_db` | 以上多个文件共用的建库器（DDL 来自 eqp_matrix B0 冻结；`column_rename` 做 EQ-008 schema 漂移哨兵） | |

### 3.2 真实进程 / 二进制

| 文件 | 用法 | 条件 |
|---|---|---|
| test_equivalence_anchor.py | 权威源①：`/home/mar/.opencode/bin/opencode`（v1.18.18 发布二进制）spawn 隔离实例 + `httpx.Client` 真 HTTP 对照（:684-699 版本断言） | 二进制缺席才 skip；golden 校验（`load_real_golden`）**永不 skip**（v4_fixture.py:767-779） |
| test_actions.py / test_actions_routes.py | spawn `python -c "time.sleep(5)"` 等真实子进程 + `pgrep -f` 验证进程组清理（test_actions.py:411,429,454,677,771,798,803,838,843,916,950） | 依赖 `pgrep` 可用（procps） |
| test_v3_rawbody_regression.py:186-187 | `subprocess.run` 以 `PYTHONHASHSEED=0` 取字节现场 | |

### 3.3 真实文件系统

- **tmp_path 之外零写入**：20 个文件用 `tmp_path`，所有落盘（access log、snapshot、sqlite、replay）均在临时目录。
- 读取 in-tree 数据：`tests/fixtures/msg40.json`（443KB，仅 test_skeleton.py 用）。
- **孤儿 fixture**：`tests/fixtures/g_f1/equal_ts_page1.json` + README——grep 全 tests/*.py 零引用（README 自述用于 G-F1 cursor 测试，`loop_scenario_pages.json` 自标注 Not yet used 且文件不存在）。属死数据，可清理项。
- 真实 home 路径仅出现在**注入/注释**（test_db_path_resolution.py:125,149 以参数注入 home；无 `~/.local/share/opencode` 真库依赖，文件头 :4 明示）。

---

## 4. Mock 边界

| 机制 | 规模 | 证据 |
|---|---|---|
| **respx** | **0 文件 0 处** | `rg respx tests/` 空。**但 pyproject.toml:21 声明 `respx>=0.22,<1` test 依赖且 src/scripts 亦零引用——死依赖，可从 test extras 移除** |
| `httpx.MockTransport` | 37 文件 63 处 | 直建 27 文件 + 经 conftest `upstream_factory` 间接 30 文件（部分重叠，如 test_etag.py 两种都有） |
| `httpx.ASGITransport` | 69 文件 | 路由层集成测试主流（app 内进程驱动） |
| conftest `upstream_factory` | 30 测试文件 | 唯一共享 fixture（MockTransport 包装 + 统一 aclose，conftest.py:7-26） |
| 真实 HTTP client | test_equivalence_anchor.py | 真 opencode 实例（唯一出网/出进程面） |

结论：上游 mock 全部走 httpx 原生 MockTransport（handler 按 handler(request)->Response 编写），无 respx 抽象层；ASGI 层一律 ASGITransport 不起端口。mock 边界一致性良好，唯一问题是 respx 死依赖。

---

## 5. 最大 15 / 最小 15（按行数）

**最大 15**（合计 22,665 行，占 tests 总量 34%）：

| # | 文件 | 行数 | test数 |
|---|---|---|---|
| 1 | test_sse_replay_wire.py | 2586 | 73 |
| 2 | test_hub_behavior_lock.py | 1841 | 119 |
| 3 | test_token_hub_flush.py | 1653 | 81 |
| 4 | test_messages_routes.py | 1583 | 46 |
| 5 | test_hub.py | 1582 | 67 |
| 6 | test_questions_routes.py | 1536 | 37 |
| 7 | test_token_stream_route.py | 1484 | 49 |
| 8 | test_equivalence_anchor.py | 1444 | 28 |
| 9 | test_expand_routes.py | 1356 | 57 |
| 10 | test_etag.py | 1241 | 38 |
| 11 | test_session_single_v4.py | 1238 | 50 |
| 12 | test_traffic_snapshot.py | 1199 | 43 |
| 13 | test_providers_projection_v4.py | 1184 | 51 |
| 14 | test_batch3_lifecycle.py | 1157 | 40 |
| 15 | test_access_log.py | 1142 | 50 |

**最小 15（test_*.py；conftest 26 行 / v4_fixture 884 行为支持件不计入）**：

| # | 文件 | 行数 | test数 |
|---|---|---|---|
| 1 | test_health_features.py | 19 | 1 |
| 2 | test_app_main.py | 21 | 1 |
| 3 | test_errors.py | 79 | 2 |
| 4 | test_directory.py | 85 | 17 |
| 5 | test_eqp_matrix.py | 87 | 1 |
| 6 | test_traffic_latency.py | 109 | 5 |
| 7 | test_health_dual_view.py | 121 | 6 |
| 8 | test_proxy_sse_observability.py | 123 | 2 |
| 9 | test_logging_config.py | 133 | 6 |
| 10 | test_sse_logging.py | 138 | 4 |
| 11 | test_selector_query_strip.py | 139 | 9 |
| 12 | test_upstream_error_boundary.py | 161 | 2 |
| 13 | test_wal_staleness.py | 167 | 1 |
| 14 | test_gzip_negotiation.py | 170 | 5 |
| 15 | test_versions_route.py / test_db_path_resolution.py | 172 | 9 / 14 |

密度两极：test_hub_behavior_lock.py 15.5 行/测试（行为锁叙事重）；test_directory.py 5.0 行/测试（纯函数矩阵）。

---

## 6. tests/golden/ 结构

```
tests/golden/
├── sessions-global-v1.18.18.json        21,485B  生成器 mirror-oracle-v1（镜像 oracle 全量投影）
└── sessions-global-real-v1.18.18.json   31,219B  生成器 real-upstream-http-1.18.18（真实 handler）
```

- **金样数量：2**（+1 个 in-tree 数据 fixture `tests/fixtures/msg40.json`、1 个孤儿 `fixtures/g_f1/`）。
- **消费方**：仅 `test_equivalence_anchor.py`（EQ-001..006 以 real golden 为期望对生产 `fetch_sessions_page` 断言；mirror oracle 降为辅助，v4_fixture.py:782-865 `build_db_from_real_golden` 从 real golden 逆映射重建确定性 DB）。
- **再生成机制**：
  - mirror golden：`.venv/bin/python tests/v4_fixture.py --write-golden`（v4_fixture.py:868-880 `main`；golden 头内嵌 `regenerate_hint` 同串）。
  - real golden：`OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN=1 pytest tests/test_equivalence_anchor.py -k eq007_real_golden`（需真实 opencode 1.18.18 二进制，v4_fixture.py:513-517）。
- **漂移检测（三层）**：
  1. `validate_golden`（v4_fixture.py:450-482）：version/generator/dataset fingerprint（sha256[:16] canonical 序列化）/dataset_digest/upstream_locked/regenerate_hint **逐键强制** + sessions 载荷与镜像 oracle 全量相等 + `response_fingerprint`（载荷 canonical sha256[:16]）——数据集与投影管线两层指纹交叉定位漂移层（:400-409 注释）。
  2. `validate_real_golden_ci`（v4_fixture.py:642-764）：**顶层键集冻结**（`REAL_GOLDEN_TOP_LEVEL_KEYS` frozenset，:593-598，生成器增删键即失败）+ dataset_manifest **全字典相等**（非只比 digest）+ `generated_at` 必须带时区 ISO + query 端点冻结 + server-assigned 字段清单 + 注入清单桥接的逐字段全量比对（title/archived 置性+值/parent 链/tokens 五列/summary/revert/metadata/agent/model/permission）+ 排序单调性。
  3. 装载即校验：`load_golden`/`load_real_golden`（:485-489,767-779）assert 失败即红——real golden 校验**无条件执行**（二进制缺席不 skip 校验）。
- 确定性保障：`FIXED_NOW_MS` 常数代替真实 now（:38-40）、sha256 派生代替 `hash()`（:86-87）、ALIGNED_VERSION 常数钉 v1.18.18（:33）。

## 7. 核心 fixture 精读

### 7.1 tests/conftest.py（26 行）

- 唯一 fixture `upstream_factory`（conftest.py:7-26）：工厂闭包 `_make(handler, *, base_url="http://127.0.0.1:4096")` → `httpx.AsyncClient(transport=httpx.MockTransport(handler))`，登记 clients，yield 后统一 `aclose`。
- **开关面**：仅 2 个——per-test handler 函数 + 可覆写 base_url。无 env / 时钟 / feature 开关（极薄设计；所有路由级 knob 由各测试文件自建 app 实现「绕过模块级 lifespan 以免动 env」的模式，如 test_agent_routes.py:63-65 注释）。
- 注释声明镜像 `oc_slimapi.upstream.create_client`（conftest.py:12）。

### 7.2 tests/v4_fixture.py（884 行）——B3a-B2 测试共享基建

- **数据集**（:68-186）：七维度 26 行 session + 2 project（tie-break / archived×父子 / allowlist 多子树 / legacy 空目录与 `${owner}-${host}` / 极端时间戳 0 与 FIXED_NOW_MS / join 缺行 / 坏 JSON 跳行含 model 列 / search 字面与大小写折叠）。
- **开关面**（提供的环境/注入旋钮）：
  - `mirror_page(archived=, parent=, search=, cursor=, limit=, allowlist=, session_rows=, project_rows=)`（:341-393）——v4 sessions 全管线镜像（谓词→排序→keyset→LIMIT+1→§8 容忍，次序与 SQL 严格一致）；
  - `build_fixture_db(db_path, session_rows=, project_rows=, column_rename=)`（:214-261）——列改名 = EQ-008 schema 漂移哨兵；
  - `load_eqp_matrix()`（:50-61）——importlib 按路径装载 scripts/eqp_matrix.py（scripts 非包）。
- **隔离铁律**（S-B03，:1-31 文件头）：oracle 是独立重写的谓词/排序/翻页（不 import 生产投影谓词）、JSON 解析用 stdlib `json` 而非生产 `orjson`、仅引用 `ROW_KEYS` 常量做键集对齐。
- **golden 生成/校验**：`build_golden_document` / `validate_golden` / `load_golden` / `build_real_golden_document` / `validate_real_golden_ci` / `load_real_golden` / `build_db_from_real_golden`（详见 §6）。
- 消费方 8 个测试文件：test_equivalence_anchor、test_eqp_matrix、test_session_single_v4、test_sql_semantics、test_dbaux_metrics、test_sessions_v4_representation、test_degraded_observability、test_sessions_v4_matrix。

---

## 8. 双态锁定统计（口径与名单）

| 口径 | 数量 | 名单 |
|---|---|---|
| v3+v4 双 wire 视图同文件（`?v=`/`"v3"`/`wireVersion` 任一形态双断言） | **12** | test_degraded_observability, test_expand_href_v4, test_method_boundary_v4, test_post_actions_v4, test_providers_projection_v4, test_readiness_gating_integration, test_selector, test_sessions_v4_matrix, test_sse_replay_wire, test_v4_dual_window, test_v4_observability, test_versions_readiness |
| 其中 `?v=3` 与 `?v=4` 字面双 selector | 10 | 上表减 test_degraded_observability、test_v4_observability（这两者以 wireVersion/观测维度值断言双态） |
| readiness.SATISFIED 门控开关双测（排除 ID→4.0.0 回退 vs 全集→修订生效） | **10** | test_session_single_v4, test_versions_route, test_versions_readiness, test_method_boundary_v4, test_sessions_v4_representation, test_degraded_observability, test_post_actions_v4, test_v4_dual_window, test_sessions_v4_matrix, test_readiness_gating_integration |
| 功能开关 off 态回归 | ≥4 | test_messages_coalesce（coalesce_enabled=false）、test_message_fingerprint（fingerprint off 字节回归）、test_catalog_cache（ttl=0）、test_vary_directory_unconditional（ETAG_ENABLED=false） |
| 并集（任一双态形态） | **19 文件** | 12 ∪ 10 去重叠 7 = 15；4 个 flag 文件均不在前 15 内，故 15+4 = 19 |

双态覆盖结论：v4 修订面（§12-§16）全部有「修订生效态 + 4.0.0 回退态」成对锁定；v3 侧以「字节回归」锚定（test_expand_href_v4、test_sse_replay_wire、test_v3_rawbody_regression）。代表性双态锚：test_readiness_gating_integration.py:44 起（§3.3 五 feature 逐一 monkeypatch 双向）。

## 9. 顺带发现（供主审计汇总）

1. **respx 死依赖**：pyproject.toml:21 声明、全仓零引用（§4）。
2. **孤儿 fixture**：`tests/fixtures/g_f1/equal_ts_page1.json` 零引用（§3.3）。
3. `/slimapi/_shadow/sweep` 内测路由不在 54 census（§1.1）。
4. 键序跨进程非确定性已被 `PYTHONHASHSEED=0` 围栏但未根治（§2.5，skeleton `_pick` set 迭代）。
5. 最重真延迟测试串行 >30s（§2.5），集中在并发/生命周期类文件。
