# oc-slimapi 架构与质量审查报告 — 2026-08-09

> 本报告是**只读审查产出**：仅记录架构、代码质量、可维护性、可靠性、安全、功能审查结论，以及后续低能力 agent 可执行批次计划的输入。**本报告不包含任何代码修改**；所有修复方向由
> `docs/implementation-batches-2026-08-09.md` 承接。

## 状态与基线

| 项 | 值 |
|---|---|
| 审查类型 | 架构 / 质量 / 可维护性 / 可靠性 / 安全 / 功能 综合评审（只读） |
| 评审基准 commit（审查对象） | `e236ed35988f245d4e07d3eb79f1f6e81c5ced28`（release v1.3.1） |
| 当前 HEAD（评审时点） | `216ff0bda8c6e7f81ebf7e7565ecae3d837daebf`（分支 `bundle-slimapi-actions`） |
| 相对评审 commit 的增量 | 仅 `CHANGELOG.md`(+6)、`deploy/actions.manifest.example.toml`(+55)、`deploy/oc-slimapi.service`(+6)、`docs/operations.md`(+47/-8)——**无核心实现变化**（`git diff --stat` 核实） |
| 工作树状态 | 审查与当前 HEAD 差异核验时干净；本报告落盘后新增两份未跟踪 docs 文件（本报告与 `docs/implementation-batches-2026-08-09.md`） |
| 验证证据 | 契约核验 agent 实跑全量 `./scripts/check.sh`：**1386 passed**；route↔INTERFACE_MAP 一致性 gate 通过（`scripts/check_routes_doc.py`） |
| Wire API 版本 | 仍为 2（`X-Slimapi-Version: 2`；`versioning.py` 中 `ACCEPTED_CLIENT_VERSIONS == (2, 2)`，config.validate 强制 fail-closed） |
| Wire 权威 | `docs/specs/v2-contract.md`（唯一 wire 基准） |

## 执行摘要与评分表

oc-slimapi 是一个架构清晰、防守密集的省流 sidecar：事务化启动（`AsyncExitStack` P0-1）、逐组件 best-effort 降级、T3 资源护栏、错误码/契约纪律（route↔INTERFACE_MAP 防漂移 gate）都是高质量工程。wire 面极稳（v2 长期未动、加性演进）。主要短板集中在**运维边界**（优雅停机缺超时、日志/快照生命周期）、**资源预算的病态上界**（questions 聚合峰值）、以及**可维护性**（配置双轨、私有字段访问、重复的 busy_response 实现、无 CI/coverage 门禁）。

| 维度 | 评分 | 一句话依据 |
|---|---|---|
| Wire 契约 | **8.8** | 唯一权威 + 版本双轨纪律 + 加性演进 + route↔INTERFACE_MAP gate；少量文档措辞漂移 |
| 代码质量 | **7.0** | 单一职责、注释密度高、错误路径覆盖全；模块级可变全局（skeleton 读 settings、questions 模块级 semaphore）与私有字段访问拖分 |
| 可维护性 | **6.5** | 配置双轨（skeleton）、重复 busy_response、app.py lifespan 手工接线、无 test builder/fake clock 基建 |
| 可靠性 | **7.0** | best-effort 收敛优秀；但优雅停机无超时（P0）、questions 聚合峰值上界未约束（P1）、快照无 retention（P2） |
| 安全 | **7.5** | 版本门禁 fail-closed、shell deny-list、actions manifest 校验严格；actions 子进程继承完整环境（P2）是主要缺口 |
| 测试 | **8.5** | 1386 测试、测试数量与覆盖深入；但 app builder / settings fixture 在各路由测试模块中重复（`conftest.py` 仅提供 `upstream_factory`，`_settings`/`_build_app` 逐文件复制）；缺少 subprocess 级 shutdown 集成测试与 questions 峰值约束测试 |
| 运维 | **6.5** | 文档完善但 incarnation 无运维说明、shutdown 语义未文档化、无 CI/coverage、快照无限增长 |
| 功能完整性 | **8.0** | 读路径覆盖充分（sessions/messages/events/questions/directories/command/agent/actions/token-stream）；children/diff/file/providers 未入 |
| 产品价值兑现 | **5.5** | 省流（skeleton 投影、digest 策展、catalog 白名单）已落地；但多项读路径由 traffic 证据驱动尚未排序，价值全量兑现仍未完成 |
| **综合** | **约 7.3** | |

## 架构地图

```text
ocdroid ──(stunnel mTLS 14097)──▶ oc-slimapi :4097 ──(loopback HTTP)──▶ opencode :4096
                                     │ loopback 明文（Tailscale/防火墙隔离）
```

| 组件 | 角色 | 关键文件 |
|---|---|---|
| **Composition root** | lifespan 事务化接线（AsyncExitStack P0-1）、smoke 探测、startup banner、路由注册、`main()` | `src/oc_slimapi/app.py`（lifespan L150-484；路由注册 L505-507；`main()` L510-520） |
| **Thin routes** | `/slimapi/**` 骨架/聚合/管理端点，共享 admission→cap-read→project→gzip 链 | `src/oc_slimapi/routes/`：`sessions.py`、`messages.py`、`events.py`、`questions.py`、`directories.py`、`agent.py`、`command.py`、`actions.py`、`health.py`、`token_stream.py`、`metrics.py`；共享链在 `_catalog_common.py` |
| **Proxy** | catch-all 反代：路径归一、shell/PTY deny-list、turn fence bump、原始 query 透传、SSE/command 超时分类、字节计数 | `src/oc_slimapi/proxy.py`（`install_proxy` L106-281） |
| **GlobalHub** | 控制面 SSE：上游 `/global/event` 全量流 → `session.digest` 策展 + q/p 直推、T3 订阅预算、turn 戳 | `src/oc_slimapi/sse/hub.py`（HubRegistry）、`sse/global_hub.py`（GlobalHub） |
| **TokenStreamHub** | token-stream 累加器/订阅/flush/回放（独立账本），wire 见 design-token-stream §3.x | `src/oc_slimapi/sse/token_hub.py`（顶层接线）、`sse/tokenstream/hub.py`（核心，L155）、`sse/tokenstream/{frames,models,subscriber}.py` |
| **Actions** | manifest 驱动 exec/query 管理动作、admission/single-flight/节流/审计 | `src/oc_slimapi/actions.py`（L442 ActionRegistry）、`routes/actions.py` |
| **Traffic / log / snapshot** | 双向字节账本、按天 access log + 压缩/prune 维护循环、累计快照 | `src/oc_slimapi/traffic.py`（TrafficLedger）、`access_log.py`（DailyAccessHandler L110 + 维护循环）、`traffic_snapshot.py`（TrafficSnapshotter）、`middleware/traffic_accounting.py`、`middleware/request_id.py` |
| **Turn registry** | S2/S5 turn fence：incarnation 持久化 + per-sid turn 计数（LRU 10k） | `src/oc_slimapi/turn_registry.py`（IncarnationStore + TurnRegistry） |
| **Transform** | admission semaphore + 有界 worker 池（offload parse/project/serialize/gzip） | `src/oc_slimapi/transform.py`（TransformPool L178） |
| **支撑层** | 配置、版本门禁、错误码、gzip、上游客户端 | `config.py`、`versioning.py`、`errors.py`、`gzip_util.py`、`upstream.py`、`upstream_errors.py`、`discovery.py`、`directory.py` |

## 已验证问题（严格分层）

> 每项均经源码证据核实。wire 分类仅用于说明本项是否影响 wire；**全部问题均可非破坏性修复，不 bump `X-Slimapi-Version`**。

### P0（阻断生产可靠性）

#### P0-1 uvicorn 未设 graceful shutdown timeout，活跃 SSE 时 restart 可被 systemd 90s SIGKILL

- **分类**：可靠性 / 运维（P0）
- **证据**：`src/oc_slimapi/app.py:520` `uvicorn.run("oc_slimapi.app:app", host=settings.host, port=settings.port, workers=1)`——未传 `timeout_graceful_shutdown`（uvicorn 默认 `None` = 无限期等待存量连接关闭）；`deploy/oc-slimapi.service` 未设 `TimeoutStopSec`（systemd 默认 90s 后 SIGKILL）；`src/oc_slimapi/traffic_snapshot.py:154-180` `TrafficSnapshotter.stop()` 在 lifespan teardown 中写最终快照帧。
- **触发**：`systemctl --user restart|stop oc-slimapi`（或 hot reload）时存在活跃 SSE 连接（`/slimapi/events` 或 token stream）。
- **影响**：uvicorn 无限等待 SSE 连接关闭 → systemd 90s 后 SIGKILL。最终快照帧、access-log handler close、transform drain（`_TRANSFORM_DRAIN_TIMEOUT=10s` 只在关闭路径生效）可能被截断；优雅停机语义不可达。
- **修复方向**：`main()` 提取常量（如 `_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0`）并传 `timeout_graceful_shutdown=`；unit 加 `TimeoutStopSec=15`；文档化 shutdown/restart 语义；subprocess 级集成测试（见批次计划 Task 1/2）。
- **wire 分类**：非 wire（运维行为修复）。

### P1（需限期修复）

#### P1-1 questions 聚合内存峰值无字节预算（16×64MiB 病态上界）

- **分类**：可靠性 / 资源安全（P1）
- **证据**：`routes/questions.py:28-29` 模块级 `_FANOUT_CONCURRENCY = 16` + `_fanout_sem`；L157-163 `asyncio.gather` **一次性并发发起全部目录**请求（先全读）；L186-190 聚合 items 预算（`_MAX_AGGREGATE_ITEMS=10000`）在 **gather 完成之后**才检查。per-dir 读上限为 `config.max_response_bytes`（默认 64 MiB，`config.py:175`）。
- **触发**：目录数量大且各 `/question` 响应大（病态：16 并发 × 64 MiB = 1 GiB 峰值；`read_with_cap` 只防单 body 超限，不防并发聚合）。
- **影响**：单请求 RSS 峰值可达病态上界，突破 `MemoryMax=384M`（`deploy/oc-slimapi.service:54`）窗口；聚合 budget 生效前内存已爆。
- **修复方向**：配置化字节预算（per-dir cap + aggregate cap + fanout concurrency）+ sliding window worker scheduler（见批次计划 Task 5）。
- **wire 分类**：非 wire（沿用既有 `truncated` 字段）。

#### P1-2 access log 同步 write + flush 在事件循环上阻塞

- **分类**：可靠性 / 性能（P1）
- **证据**：`access_log.py:194-196` `DailyAccessHandler.emit` 执行 `self._current_fh.write(msg)` → `write("\n")` → `flush()`；`middleware/traffic_accounting.py:291` 在请求结束回调（事件循环线程）同步调用 `write_access_log`。
- **触发**：磁盘抖动/高 QPS 时每次请求同步 flush。
- **影响**：事件循环被文件 I/O 阻塞，SSE 心跳与其它轻量请求被拖慢。
- **修复方向**：先合成单次 `write(msg+"\n")`（Task 6，原子行）；bounded async writer + 冻结丢弃策略为设计门禁（Task 7，不在本批次实施）。
- **wire 分类**：非 wire。

#### P1-3 skeleton 配置双轨（模块级全局 settings vs request.app.state.config）

- **分类**：可维护性 / 可测试性（P1）
- **证据**：`skeleton.py:10` `from .config import settings`；`skeleton.py:168-171` `_maybe_inline_state_field` 直接读全局 `settings.skeleton_inline_output_max_bytes` / `_max_message_bytes`；而路由侧（`routes/health.py:56`）从 `request.app.state.config` 读同一字段——两条来源可能不同步，纯函数测试无法注入。
- **触发**：任何需要不同 caps 的 app 实例/测试场景。
- **影响**：跨 app 泄漏、测试无法独立控制阈值；后续加 config 字段需双处维护。
- **修复方向**：引入不可变 `SkeletonLimits(field_bytes, message_bytes)` 显式注入（见批次计划 Task 8）。
- **wire 分类**：非 wire（默认值不变）。

#### P1-4 incarnation 状态与 logs 共目录

- **分类**：可靠性 / 运维（P1）
- **证据**：`app.py:387` `IncarnationStore(state_dir=access_log_dir)`；生产 `access_log_dir = %S/oc-slimapi/logs`（`deploy/oc-slimapi.service:36`）；`turn_registry.py:87` 状态文件名为 `incarnation`。
- **触发**：运维清理/迁移 logs 目录（access log 有 RETAIN_DAYS prune、compress、legacy migrate 一整套生命周期）。
- **影响**：状态文件（causal fence 持久化）被放在"可被维护循环触碰的日志目录"内；logs 被重建则 incarnation 重置（fence 弱化）；`docs/operations.md` 全文无 incarnation 字样（零运维说明）。
- **修复方向**：独立 `state_dir`（`OC_SLIMAPI_STATE_DIR`）+ legacy 原子迁移（见批次计划 Task 9）。
- **wire 分类**：非 wire（运维状态路径）。

#### P1-5 精确 `/slimapi`（无尾斜杠）绕过 thin-route 404 落到 catch-all

- **分类**：一致性 / 行为收敛（P1）
- **证据**：`proxy.py:128` `if norm_path.startswith("/slimapi/"):`——精确路径 `/slimapi`（归一后无尾斜杠）不满足 `startswith("/slimapi/")`，被反代到上游 opencode。
- **触发**：客户端（或误配置）请求精确 `/slimapi`。
- **影响**：未知 `/slimapi/**` 统一返回 404 `thin_route_not_found`，但精确根被透传上游（行为不一致、无统一错误码）。
- **修复方向**：条件改为 `norm_path == "/slimapi" or norm_path.startswith("/slimapi/")`（见批次计划 Task 3）。
- **wire 分类**：非 wire（错误路径一致化）。

#### P1-6 sessions 路由 transform_busy 缺 Retry-After（与 catalog 路由不一致）

- **分类**：一致性 / 客户端可重试性（P1）
- **证据**：`routes/sessions.py:100-101` `except TransformBusy: raise CodedHTTPException(503, code="transform_busy")`——无 body `retry_after` 无 `Retry-After` 头；对照 `routes/agent.py:47`、`routes/command.py:44`、`routes/directories.py:104` 均使用 `_catalog_common.busy_response`（body `retry_after=2` + header `Retry-After: 2`，`_catalog_common.py:42-50`）。另外 `routes/messages.py:190-206` 存在 `_busy_response` 与 `_catalog_common.busy_response` 的重复实现。
- **触发**：转换池饱和时 `GET /slimapi/sessions` 命中。
- **影响**：ocdroid 既有 `retryAfterHeaderToMs` + Retry-After 重试范式对该端点失效；同语义错误在不同 thin 路由表现不一。
- **修复方向**：sessions 复用 `_catalog_common.busy_response`（见批次计划 Task 4）；`messages._busy_response` 收敛为单一来源（见 Task 15）。
- **wire 分类**：加性（新增可选 body 字段 + 头，客户端可忽略），不 bump。

### P2（可后置）

#### P2-1 traffic snapshot 无 retention（每日文件无限增长）

- **证据**：`traffic_snapshot.py:191-207` `_loop` 只写不删；`config.py` 无 `traffic_snapshot_retain_days`。默认 300s tick → 一天 288 帧；access log 有 RETAIN_DAYS 而 snapshot 没有。
- **影响**：长期运行磁盘无限增长。
- **修复方向**：`traffic_snapshot_retain_days`（默认 0 = 不删）+ 严格匹配 `traffic-snapshot-YYYY-MM-DD.jsonl` 的 prune helper，接入维护循环（见批次计划 Task 10）。

#### P2-2 actions 子进程继承完整 sidecar 环境

- **证据**：`actions.py:760-766` `_spawn` 调 `asyncio.create_subprocess_exec(*spec.argv, cwd=..., start_new_session=True, stdout=PIPE, stderr=PIPE)`——未传 `env`，子进程继承完整环境（含 `OC_SLIMAPI_*` 配置/密钥类变量、访问日志路径等）。
- **影响**：action 子进程（已属 risk-accepted 面）额外获得 sidecar 环境信息。
- **修复方向**：`_build_action_env()` 固定 allowlist 复制（`PATH/HOME/LANG/LC_ALL/LC_CTYPE/TMPDIR/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS`；`/usr/bin/systemctl --user` 依赖 DBUS/XDG），fail-closed（见批次计划 Task 11）。

#### P2-3 TransformPool 读取私有 Semaphore 字段

- **证据**：`transform.py:240-248` `snapshot_metrics()` 读 `self._semaphore._value` / `self._semaphore._waiters`。
- **影响**：依赖 asyncio 私有实现细节，Python 版本演进有碎裂风险。
- **修复方向**：内部维护 `_active` / `_waiting` 计数器（见批次计划 Task 12），不改变 admission 语义。

#### P2-4 GlobalHub.publish 单体过大（可维护性）

- **证据**：`sse/global_hub.py` publish 路径承担 digest 策展、turn 戳、traffic 计数、upstream 保证等多职责（`GlobalHub` L56 起，方法群密集）。
- **影响**：评审/单测/行为审计成本高。
- **修复方向**：拆分只进 Task 15（独立后续计划入口），不得与修复任务混做。

#### P2-5 config/app.state/test 基建零散（可维护性）

- **证据**：`app.py` lifespan 手工逐项接线 `app.state.*`（upstream/transforms/hubs/token_registry/turn_registry/actions_registry/…）；测试层用 `conftest.py upstream_factory` 复制 app 状态，无统一 test builder / fake clock。
- **影响**：加组件需同步改 lifespan 与测试夹具；时间相关测试无注入时钟。
- **修复方向**：Task 15 定义 app services 组装 + test builder/fake clock 的后续计划入口。

#### P2-6 observability 粒度不足

- **证据**：`/slimapi/metrics` 有 traffic buckets；transform 仅 `activeTransforms`/`waitingTransforms` 快照；无等待时长/超时次数等直方图/计数器。
- **影响**：池饱和、SIGTERM 行为等排障靠日志拼图。
- **修复方向**：Task 12 计数器 + Task 15 观测面规划；不在本批次扩张。

## 文档漂移清单（已核实）

> 均为描述/措辞层漂移；wire version 不变。修复由批次计划 Task 13 承接。

| # | 漂移项 | 证据 | 应修方向 |
|---|---|---|---|
| D1 | **legacy-only 文字残留** | `docs/specs/v2-contract.md:31,126,262-264,393-398` 多处「仅 legacy `/session` API」措辞与已删除 v1 端点叙述（v1-contract.md 文件已删）；INTERFACE_MAP 与 design-v2 亦含 v1 时代残留 | 对照当前路由面清理文字，明确 legacy 仅指 opencode 上游 API，非 slimapi 端点 |
| D2 | **health `slimapi_contract` 描述** | `routes/health.py:23` 硬编码 `"slimapi_contract": 2`（不随 `SERVER_API_VERSION` 派生）；`docs/operations.md:277,285` 与 INTERFACE_MAP:54 描述为「当前 wire 契约版本（v2）」 | 明确该字段是静态常量（bump 需同步改代码+文档），说明与 `server.api_version` 的关系 |
| D3 | **`directory_not_allowed` 适用范围** | `v2-contract.md:449` 称「v2 无独立生产路径」与 `:452` messages-only 自相矛盾；`INTERFACE_MAP.md:89` 泛化表述；实现同时存在于 `routes/messages.py:230` 与 `routes/token_stream.py:67`（均为 query vs header 冲突时的结构性守卫） | 统一为 messages 与 token-stream 的 query/header conflict 结构性守卫 |
| D4 | **CLIENT_CHANGES truncated/session_deleted 措辞** | `CLIENT_CHANGES.md:277,289` token-stream `truncated`/`session_deleted` 描述需对照 `sse/tokenstream/hub.py:922-974` 实际发射的 `resync{session_deleted}→STOP`、`snapshot{truncated:true}` 校准 | 逐条对齐 hub.py 发射语义与 resync 表 |
| D5 | **token gzip 措辞** | `CLIENT_CHANGES.md:70` 「建议 `Accept-Encoding: gzip`」与实现措辞歧义；design-token-stream 中 gzip 字样 | 明确三层语义：控制面 `/slimapi/events` **不** gzip；token stream `/slimapi/sessions/{sid}/stream` 可按 `Accept-Encoding` **gzip**；普通 JSON/catalog 按内容协商且仅 beneficial 时压缩（不得写成「SSE 都不 gzip」） |
| D6 | **access log `ts` 语义未文档化** | `access_log.py:297` `ts = datetime.now()`（请求**结束**时刻）；`docs/operations.md:184`、`docs/manual/traffic-accounting.md:146` 仅写「请求元数据」 | 明示 `ts` 为请求完成落盘时刻（end），时长分析用 `durationMs` |
| D7 | **incarnation 运维说明缺失** | `docs/operations.md` 全文无 `incarnation`；`turn_registry.py` 注释声明复用 access_log dir 为 state dir | 补状态文件路径/生命周期说明（并随 Task 9 状态目录迁移更新） |

## 功能机会（按优先级）

1. **优先完成/核实 thin route 迁移**：ocdroid 对接状态需以其仓库当前代码/文档核验（本报告为静态审查，不假定其已完整消费）；本轮静态调查显示迁移**尚未完全兑现**，因此优先完成/核实 thin route 迁移（sessions/messages/events/metrics/questions 为候选）。
2. **再依据生产 traffic log 排序**：对 3 天 access log 中 `bucket=="passthrough"` 聚合 method+path（`docs/manual/traffic-accounting.md`），按 requests/upIn/downOut 排序，候选只读路径：**children / diff / file / providers**（见批次计划 Task 16 证据 + Task 17 设计）。
3. **明确的"不做"清单**：`start` 已等同 updatedSince（sessions 语义已定，**不建议**消息 after 伪实现）；**不做**跨 session 全文搜索；**不做** sidecar 本地 DB/cache（单用户、状态收敛已够）；**不做** PTY/auth/actor/multi-worker（T3 单实例模型是设计约束）。

## 已证伪或降级列表（勿再修）

> 以下为先前 explorer/评审提出的项，已被 fixer-ds 以源码证据证伪或降级为非问题。**后续 agent 不得将其重新写成缺陷**，除非上游 opencode 行为发生变化。

| 原提法 | 结论 | 依据 |
|---|---|---|
| retired-message TTL gate | **证伪**（设计如此） | tokenstream 墓碑/退休机制是有意设计（`hub.py` TTL/prune），非泄漏 |
| part revision reconnect | **证伪**（符合契约） | partEventRevision strict `>` 去重为契约 §3.x.2 规定行为 |
| session.error 需携带 sid | **证伪**（契约如此） | 无 sid 的 `session.error` 全局 toast 是 v2-contract §3 既定语义 |
| manifest TOCTOU | **证伪**（已收敛） | 代码虽有 path 检查流程，但最终对**已打开 fd** 做 `fstat` owner/mode/regular-file 校验，读取与检查钉在同一 inode（`actions.py:238-267`）；不是「无 check-then-open」 |
| proxy double-close | **证伪**（幂等闭合） | `proxy.py:265-280` finally aclose + BackgroundTask aclose 幂等（httpx aclose reentrant），注释明确 |
| raw query latin-1 解码 | **证伪**（byte 保真） | `proxy.py:196-199` 用 scope 原始 bytes 以 latin-1 无损重编码，P0-7 注释给出依据 |
| 当前 SSE traffic double-count | **证伪**（单点计数） | SSE upstream 字节在 `GlobalHub.run` 单一消费点计数一次（`app.py:318-319` 注释），access log 的 `events_sse` 桶为连接粒度 |
| module fanout semaphore starvation | **降级**（纳入 Task 5 统一修复） | 模块级 `_fanout_sem` 不会饿死；真正问题是无字节预算（P1-1），随 Task 5 移除模块级 semaphore |

## 最终结论

1. **wire 与契约纪律是本仓库最强资产**：v2 长期稳定、加性演进、route↔INTERFACE_MAP 防漂移 gate 有效，综合评分主要由运维/资源边界/可维护性拖低。
2. **必须优先处理 P0-1（优雅停机超时）**：它是唯一能在真实部署中造成数据丢失（最终快照/日志截断）+ 最多约 90 秒（取决于 systemd `TimeoutStopSec`）不可控停机的问题。
3. **P1 六项均为有明确修法的小步改动**，且全部非破坏性；P1-1（questions 预算）需先设计后实现（高风险），P1-6 需顺带收敛重复的 busy_response。
4. **P2 与维护性项（Task 15）不得与修复混做**；Traffic 证据（Task 16）与产品路由设计（Task 17）严格串行且需用户批准。
5. **本报告不实施任何修复**；执行细节、批次门禁、验收标准见 `docs/implementation-batches-2026-08-09.md`，且必须从 Task 0 开始。
