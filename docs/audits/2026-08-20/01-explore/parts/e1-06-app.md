# E1-06 · src/oc_slimapi/app.py（785 行）

> 全文精读卡片（审计基线日期 2026-08-20）。引用格式 `src/oc_slimapi/app.py:行号`；跨文件引用仅标注存在的事实（符号定义处行号），不展开。

## 职责

FastAPI 应用装配点 + 唯一 lifespan 所有权者：完成配置校验、日志/access-log 初始化、全部 `app.state.*` 资源的事务化创建与 LIFO 关停（`AsyncExitStack`，P0-1）、启动 smoke 校验、startup banner、路由器挂载（18 个 router + 反代 catch-all）与中间件栈（RequestId / TrafficAccounting / SlimapiSelector）装配，并提供 `main()` 入口（uvicorn 单 worker，graceful shutdown 5s）。

## 对外符号（模块级）

### 常量（smoke 状态机）
| 符号 | 行号 | 含义 |
|---|---|---|
| `SMOKE_NOT_RUN` | 51 | smoke 未执行（无可用 session 等） |
| `SMOKE_UPSTREAM_UNAVAILABLE` | 52 | 上游不可达 / 非 2xx（非 schema 回归） |
| `SMOKE_INVALID_SCHEMA` | 53 | 上游有响应但消息形状不匹配 |
| `SMOKE_VALID` | 54 | schema 校验通过 |

### 私有常量（超时/节拍）
| 符号 | 行号 | 值 | 用途 |
|---|---|---|---|
| `_SMOKE_TIMEOUT` | 61 | 5.0s | smoke + 健康探测读超时（P1-37，防 30s 默认值拖死 systemd readiness） |
| `_MAINT_DRAIN_TIMEOUT` | 70 | 30.0s | access-log 维护任务关停排空上限（P1-38），超时 force cancel |
| `_TRANSFORM_DRAIN_TIMEOUT` | 79 | 10.0s | TransformPool 关停排空上限（P1-41） |
| `_DBAUX_DRAIN_TIMEOUT` | 85 | 5.0s | dbaux 单 worker executor 排空上限（B3a-B1） |
| `_REPLAY_SWEEP_INTERVAL_S` | 91 | 60.0s | replay-log 周期清扫节拍（B3b-2；TTL 15min 是 wire 窗口，此处纯记账） |
| `_GRACEFUL_SHUTDOWN_TIMEOUT` | 97 | 5.0s | uvicorn `timeout_graceful_shutdown`（P0-1；systemd TimeoutStopSec=15 在其上） |

### 函数 / 对象
| 符号 | 行号 | 职责 |
|---|---|---|
| `_log_maint_task_exception(task)` | 100 | 消费并记录维护任务未观察异常（标记 `task.exception()` 已读，防 GC 期噪音） |
| `_log_directory_allowlist(settings)` | 117 | allowlist 启用但为空 → WARNING（`/slimapi/file/**` 将 403）；非空 → INFO 条目数 |
| `smoke(app)` | 129 | 启动期上游消息 schema 冒烟校验；写 `app.state.smoke_status` / `app.state.schema_degraded` |
| `lifespan(app)` | 189-190 | `@asynccontextmanager`；全部资源的创建+注册+启动序列与 LIFO 关停（见下节） |
| `app` | 734 | FastAPI 单例（`title="oc-slimapi"`，`version=__version__`，`lifespan=lifespan`）；import 时即装配错误处理器/中间件/路由 |
| `main()` | 765 | CLI 入口：先 `setup_logging`+`settings.validate()`（RuntimeError → `SystemExit(1)`），再 `uvicorn.run("oc_slimapi.app:app", workers=1, timeout_graceful_shutdown=5.0)` |
| `if __name__ == "__main__"` | 784-785 | `python -m oc_slimapi.app` 入口 |

---

## lifespan 启动/关停序列（核心）

结构：单个 `AsyncExitStack`（235），每个资源创建后**立即**注册清理回调；正常关停、启动失败（yield 前异常）、取消三条路径都走同一 LIFO 回滚（216-234, 728-731）。各回调独立 try/except 隔离（**例外见疑问点 1**）。

### app.state.* 资源一览（创建顺序）

| # | 属性 | 创建点 | 关停点（回调） | LIFO 执行序 | 超时 | 备注 |
|---|---|---|---|---|---|---|
| 1 | `config` | 197 | 无（静态） | — | — | 即模块级 `settings` 的引用 |
| 2 | `traffic_ledger` | 286 | 无显式 close | — | — | 内存台账；终值靠 snapshotter 落盘 |
| 3 | `traffic_snapshotter` | 291-295 | `_stop_snapshotter`@304（写最终快照） | 第 13 位 | 无超时参数 | `start()` 仅在 720-726（双开关，见疑问点 12）；`stop` 无条件注册 |
| 4 | `upstream` | 308 | `_aclose_upstream`@315 | 第 12 位 | httpx 自身 | `create_client(settings)` |
| 5 | `transforms` | 320-324 | `_shutdown_transforms`@337（sync callback） | 第 11 位 | 10s（`_TRANSFORM_DRAIN_TIMEOUT`） | 准入信号量 + 有界 executor |
| — | （进程级 `fulls`，非 app.state） | import@32 | `_shutdown_fulls`@355（sync） | 第 10 位 | 无 | 进程级 singleflight，收敛 grace 定时器与驻留 body |
| 6 | `catalog_cache` | 361-366 | `_shutdown_catalog_cache`@375（sync） | 第 9 位 | 无 | agent/command 目录缓存 |
| 7 | `raw_fetch_registry`（条件） | 385-388 | `_shutdown_raw_fetch_registry`@397（sync） | 第 8 位 | 无 | 仅 `settings.coalesce_enabled`；否则属性不存在（路由走直连路径） |
| 8 | `schema_degraded`（标量） | 398（初值 False；smoke 改写） | — | — | — | 仅 `invalid_schema` 时 True |
| 9 | `questions_semaphore` | 401-403 | 无需 | — | — | `/question` fan-out 全局并发帽 |
| 10 | `permissions_semaphore` | 407-409 | 无需 | — | — | `/permission` fan-out 并发帽 |
| 11 | `smoke_status`（标量） | 411（`SMOKE_NOT_RUN`） | — | — | — | smoke()@620 改写 |
| 12 | `deployment_revision`（标量） | 414（失败→None@417） | — | — | — | env/文件读取 best-effort |
| 13 | `actions_registry` | 424 | 无显式关停 | — | — | `load_registry` 声明不抛 |
| 14 | `replay_epoch`（标量） | 425 | — | — | — | `new_epoch()` |
| 15 | `replay_log` | 426-431 | `_close_replay_log`@441（sync） | 第 7 位 | 无 | 释放 retained frames（无后台任务/文件句柄） |
| 16 | `_replay_sweep_task` | 448-455 | `_stop_replay_sweep`@470 | 第 6 位 | **无 drain**（set event 后立即 cancel@461） | 周期 60s（`_REPLAY_SWEEP_INTERVAL_S`） |
| 17 | `hubs` | 477-485 | `_close_hubs`@500 | 第 5 位 | 由 HubRegistry 内部决定 | 构造后立即 `set_replay_log`@490、`get_global().set_directory_allowlist`@491-493 |
| 18 | `qp_sweep`（条件） | 504-512（else `None`@525） | `_stop_qp_sweep`@517 | 第 4 位 | 无 | 仅 `qp_sweep_enabled`；**唯一未 try/except 隔离的回调**（疑问点 1） |
| 19 | `token_hub` | 538-541 | `_stop_token_hub`@551（sync） | 第 3 位（早于 hubs.close，NB-C4） | 无参数 | 注册于 hubs 之后 → LIFO 先停（548-550 注释） |
| 20 | `token_registry` | 560-567 | **无 lifespan 回调** | — | — | flush loop 靠 first-attach/last-detach 自停（疑问点 3） |
| 21 | `turn_registry` | 587（`IncarnationStore`@582-585，`load_or_bump`@586 启动即写盘） | 无（incarnation 已持久化） | — | — | state_dir 有 fallback=access_log_dir |
| 22 | `dbaux` | 599-602（`await start()`@603） | `_stop_dbaux`@615 | 第 2 位 | 5s（`_DBAUX_DRAIN_TIMEOUT`） | 只读投影源；失败降级不崩 |
| 23 | `_access_log_stop_event`（条件） | 656-657 | 消费于 `_stop_maintenance`@689 | 第 1 位 | — | 仅 `access_log_active` |
| 24 | `_access_log_maintenance_task`（条件） | 675-684 | `_stop_maintenance`@719 | 第 1 位（最先执行） | 30s（`_MAINT_DRAIN_TIMEOUT`）后 force cancel | `extra_prune` 捎带快照剪枝（668-674） |

汇总：**24 个 app.state 属性**（含 4 个标量 `schema_degraded`/`smoke_status`/`deployment_revision`/`replay_epoch`；4 个条件属性 `raw_fetch_registry`/`qp_sweep`/`_access_log_stop_event`/`_access_log_maintenance_task`）+ 1 个进程级 `fulls`；**14 个关停回调**。

### 完整 LIFO 关停序列（正常 shutdown）

```
C14 _stop_maintenance(719)      # 30s drain → force cancel；先停，防与 ledger/hub 清理竞态
C13 _stop_dbaux(615)            # 5s drain
C12 _stop_token_hub(551)        # sync；flush loop 排空（须在 hubs 仍一致时）
C11 _stop_qp_sweep(517)         # 无隔离 try/except！
C10 _close_hubs(500)            # GlobalHub/订阅者收敛
C9  _stop_replay_sweep(470)     # set event + 立即 cancel（无 drain）
C8  _close_replay_log(441)      # 在 hubs 关闭之后 → hub 清理期追加的帧仍能落 log
C7  _shutdown_raw_fetch_registry(397)
C6  _shutdown_catalog_cache(375)
C5  _shutdown_fulls(355)        # 须在 upstream 关闭前（在途 fetch 可能仍在等 GET）
C4  _shutdown_transforms(337)   # 10s drain
C3  _aclose_upstream(315)       # 所有 fetch 层 registry 之后
C2  _stop_snapshotter(304)      # 最终快照（台账终值落盘）
C1  _close_access_log_handlers(265)  # flush + 释放文件句柄，最后执行
```

关键跨组件顺序约束均满足：token_hub.stop 先于 hubs.close（NB-C4，548-550）；replay sweep 停先于 replay_log.close（442-446）；maintenance 最先（687-688 注释）；fetch 层 registry（fulls/catalog/raw_fetch）全部先于 upstream.aclose（342-347, 359-360, 383 注释）。

**注意：225-233 的注册顺序注释已过期**（只列 7 个回调，实际 14 个；见疑问点 2）。

### 在途请求与 SSE 订阅者归宿

- uvicorn（`main()`@765，`workers=1`@779，`timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT=5.0`@780）：SIGTERM 后停止接受新连接 → 等在途连接最多 **5s** → 强制关闭；**lifespan 关停（上述 C14-C1）在连接排空之后才运行**。
- SSE 订阅者（`/slimapi/**/events`、token stream）：5s 内未自然结束的连接被强制断开 → StreamingResponse 生成器收到取消/finally → 各自 unsubscribe（HubRegistry / TokenStreamRegistry）→ 随后 C12/C10 收敛 hub 侧；上游共享 `/global/event` 连接由 `hubs.close()` 收敛。
- 在途普通请求：5s 内完成的正常完成并计入台账；超时被取消，清理走请求内 finally。
- systemd TimeoutStopSec=15（96 注释）：lifespan 最坏路径（30+5+10+…s）可超 15s → SIGKILL 截断尾部回调（疑问点 4）。

### startup smoke test 内容（`smoke()`@129-186，调用点 620）

1. `settings.smoke_session_id` 为空 → `GET /session?limit=1`（timeout 5s，143）取首个会话 id；异常 → `upstream_unavailable` + `schema_degraded=False`，return（147-151）；无会话 → `not_run`，return（152-155）。
2. `GET /session/{sid}/message?limit=1`（timeout 5s，157-159）；异常 → `upstream_unavailable`（161-165）。
3. `status_code >= 300` → `upstream_unavailable`（169-176）——注释明确 404（session 消失）/5xx 属可用性问题，**不算 schema 回归**。
4. 形状校验（177-180）：payload 是 list；非空时 `payload[0]["info"]["id"]` 为 str，且 `payload[0]["parts"]` 每个 `part["type"]` 为 str（浅校验）。
5. 通过 → `SMOKE_VALID`（182）；否则 `SMOKE_INVALID_SCHEMA` + `schema_degraded=True`（185-186）——`schema_degraded` 仅此分支为 True（44-50 注释）。
6. smoke 之后另有 best-effort `GET /global/health`（timeout 5s，626-629），**异常全吞、结果不落任何状态**（疑问点 8）。
7. startup banner（633-648）：version/host/port/upstream/max_transforms/shell_deny_list_enabled/token_stream_max_subscribers/traffic_ledger_enabled/access_log_dir（不打印 secrets）。
8. 之后才启动后台任务：access-log 维护循环（655-719，含快照剪枝 piggyback）与 traffic snapshotter（720-726）。

---

## 路由挂载顺序

### include_router（760-761，按序）

`health → versions → actions → agent → command → sessions → children → todo → diff → messages → events → metrics → questions → permissions → directories → token_stream → read_groups → write_groups`，最后 `install_proxy(app)`@762（catch-all 反代；3.0.0 起已关闭，非 `/slimapi` 路径经它回答 404，744-746 注释）。

### 中间件栈（add_middleware 后加者在外层）

| 挂载点 | 行号 | 层级 |
|---|---|---|
| `SlimapiSelectorMiddleware` | 747 | 最内（紧贴路由；`?v=` 选择器，v3/v4 双窗） |
| `TrafficAccountingMiddleware` | 753 | 中层（pure-ASGI，包住 selector 的 400 与 catch-all） |
| `RequestIdMiddleware` | 755 | 最外（包住一切） |

`register_error_handlers(app)`@735（来自 `.errors`）在任何中间件之前注册（异常处理器位于最内层 ExceptionMiddleware）。

### 405/400/404 优先链的形成

- **GET `/slimapi/versions` 无条件豁免**（742-744 注释；versions router 第 2 位挂载@760）；非 GET 到该路径 → 405 + `Allow: GET`（"first priority"，实现在 selector/versions 路由侧，本文件只落注释）。
- 其余 `/slimapi/**` 版本形式不合法 → 400（selector middleware@747，在路由分发之前、traffic accounting 之内）。
- 非 `/slimapi` catch-all → `install_proxy`@762 的已关闭反代 → 404。
- 挂载顺序对路径遮蔽的影响：token_stream 第 16 位挂载、在 `install_proxy` 之前（756-759 注释自证 `/slimapi/sessions/{sid}/stream` 不被 `/{sid}/status`、`/{sid}/children` 遮蔽；与第 6 位的 sessions router 的关系见疑问点 19）。

---

## 依赖 / 被依赖

### 依赖（app.py 导入的仓内模块）

`__version__`；`access_log`（setup/migrate/compress/prune/loop/get_logger）；`actions.load_registry`；`catalog_cache.CatalogCache`；`config.settings`；`dbaux.DbAuxiliarySource/resolve_db_path`；`errors.register_error_handlers`；`logging_config`；`middleware.request_id.RequestIdMiddleware`；`middleware.traffic_accounting.TrafficAccountingMiddleware`；`proxy.install_proxy`；`qp_sweep.QpSweepShadow`；`routes`（18 个 + `diff` 别名@30）；`selector.SlimapiSelectorMiddleware`；`singleflight.LeasedSingleFlight/fulls`；`sse.hub.HubRegistry`；`sse.replay_log.ReplayLog/new_epoch`；`sse.replay_wire.replay_sweep_loop`；`sse.token_hub.TokenStreamHub/TokenStreamRegistry`；`sse.tokenstream.hub.apply_debug_budget_overrides`；`traffic.TrafficLedger`；`traffic_snapshot.TrafficSnapshotter/prune_old_snapshots`；`transform.TransformConfig/TransformPool`；`upstream.create_client`；`turn_registry`（**函数内延迟导入**@580）。外部：FastAPI、uvicorn、httpx（间接）。

### 被依赖

- 运行入口：`uvicorn.run("oc_slimapi.app:app")`@776；`python -m oc_slimapi.app`（784-785）。
- 测试：`tests/test_smoke.py:19`（`smoke`）、`tests/test_lifespan.py:22`（`lifespan`、`_log_maint_task_exception`；237 直接读 `_access_log_maintenance_task`）、`tests/test_health_features.py:3`（`app`）、`tests/test_b4_allowlist.py:15`（`_log_directory_allowlist`）、`tests/test_replay_log.py:706/732/747`（`lifespan`）。
- `app.state` 消费者遍布 23 个文件（routes/sessions 25 处、routes/messages 15、routes/health 14、routes/metrics 9 等）——lifespan 属性契约面很宽。

## 状态 / 可变性

- **import 时副作用**：734-762 在模块导入即构建 `app`、注册异常处理器/三层中间件/18 个 router + proxy——任何 import（含测试）都会拉起全部路由模块。
- **运行期可变**：`traffic_ledger`（中间件/SSE 生成器/GlobalHub.run 三方写，281-285）；`replay_log`（hub/token hub 追加）；`hubs`（惰性 per-directory hub）；信号量运行期获取；`fulls`（进程级、模块作用域，lifespan 外仍存在，shutdown@355 收敛）。
- **启动后只读/一次性**：`smoke_status`/`schema_degraded` 仅 lifespan 期写（无运行期 re-smoke，疑问点 11）；`config`/`deployment_revision`/`replay_epoch`/`turn_registry`（incarnation 启动落盘一次）。
- **条件性属性**：`raw_fetch_registry`（384，禁用时**属性不存在**）、`qp_sweep=None`（525，属性存在）、`_access_log_*`（access_log_active=False 时不存在，tests/test_lifespan.py:278/304 以 `not hasattr` 断言）。

## 错误路径（启动失败 vs 降级运行）

**阻止启动（异常传播 → AsyncExitStack LIFO 回滚已建资源；P0-1 事务性）**
- `settings.validate()`@196 抛错：`main()` 捕 `RuntimeError` → `SystemExit(1)`（772-774，P1-35 友好报错）；uvicorn 直接加载时以 lifespan 异常失败。
- 未被本地 try/except 包裹的构造/启动调用若抛错同样阻断：`create_client`@308、`QpSweepShadow.start()`@512、`await dbaux.start()`@603（依赖其内部不抛的隐式契约，疑问点 15）、`IncarnationStore.load_or_bump`@586（依赖内部降级）、`new_epoch`/`ReplayLog`/`HubRegistry`/`TokenStreamHub` 等构造。

**降级运行（warning + 功能失效，不崩 lifespan）**
- access-log handler 安装失败（`logger.disabled`）→ 维护循环抑制（239-250）。
- 启动期 access-log migrate/compress/prune 失败 → warning（277-280）。
- `deployment_revision` 读取失败 → None（413-417）。
- actions manifest 缺失/无效 → 功能禁用（418-424，`load_registry` 不抛）。
- `smoke()` 全路径内部吞异常（147/161），落 `SMOKE_*` 状态。
- `/global/health` 探测失败 → 全吞（626-629）。
- snapshotter `start()` 失败 → warning（720-726）。
- dbaux 解析/打开失败 → 只读辅助禁用 + 周期重探（589-597 注释；实现在 dbaux/lifecycle.py:362）。
- 每个关停回调独立 try/except（`_stop_qp_sweep` 除外——疑问点 1）。

---

## 疑问点（宁多勿漏）

1. **`_stop_qp_sweep`（514-517）是唯一没有 try/except 隔离的关停回调**，直接违反 221-223 "每个回调独立隔离，一个失败不跳过其余"的声明：若 `qp_sweep.stop()`（qp_sweep.py:228）抛错，`AsyncExitStack.__aexit__` 会中止并**跳过 C10-C1 全部剩余回调**（hubs.close、upstream.aclose、最终快照、access-log 句柄 flush 全丢）。高风险关停正确性问题。
2. **225-233 注册顺序注释过期**：只列 7 个回调（access-log handler → snapshotter → upstream → transforms → hubs → token_hub → maintenance），实际 14 个（缺 fulls/catalog_cache/raw_fetch_registry/replay_log/replay_sweep/qp_sweep/dbaux）——对关停序的文档性理解会被误导。
3. **`token_registry`（560-567）无 lifespan 关停回调**：flush loop 靠 first-attach/last-detach 自停（553-559 注释）——需确认 uvicorn 强断 SSE 后各 unsubscribe finally 一定先于 `_stop_token_hub`（551）/`_close_hubs`（500）执行；若顺序不保，flush loop 可能在 hub 已 stop 后仍持引用运行。
4. **关停超时预算与 systemd 冲突**：最坏 lifespan 排空 = 30s（maint，70）+ 5s（dbaux，85）+ 10s（transforms，79）+ 其余 > systemd TimeoutStopSec=15（96 注释）→ SIGKILL 截断尾部回调（C2 最终快照、C1 access-log flush 丢失）。`_TRANSFORM_DRAIN_TIMEOUT=10s` 也已接近 15s 上限；uvicorn 连接排空 5s（97/780）与资源排空超时分属两层、无统一预算。
5. **多个 sync `stack.callback`（337/355/375/397/441/551/265）若内部同步阻塞会冻结事件循环**：尤其 `_shutdown_transforms` 语义是"等 in-flight worker"（326-334）；transform.py:291 的实现若同步 join 则关停期事件循环停摆（76-78 注释称 daemon drain 线程后台接管，需确认 `shutdown()` 本身不阻塞）。
6. **`_stop_replay_sweep`（457-470）无 drain**：set stop_event 后无条件立即 `cancel()`（461），与 `_stop_maintenance` 的优雅排空模式不一致；若 sweep 正处于 replay_log GC 中段被取消，正确性完全依赖 replay_log 内部操作原子性。
7. **启动延迟串行叠加**：`await smoke(app)`@620 最坏两个 5s 窗口（143/158）+ `/global/health` 5s（627）→ 上游不可达时启动静默最多 ~15s，56-60 注释只论证了单窗口；banner（633）在此之后才打印。
8. **`/global/health` 探测（626-629）结果完全丢弃**、不落任何状态——其可观测价值（连接池预热？）无落点；若想做健康标记则未实现。
9. **smoke 分类失真**：141-146 假定 `/session` 返回裸 list（legacy 形状）；若上游改形为 dict 包裹，`sessions[0]` 的 TypeError 被 147 的宽 except 捕获归为 `upstream_unavailable` 而非 `invalid_schema`——schema 回归检测在入口处就失效。
10. **3xx 归类**（169，`>= 300`）：重定向也计 `upstream_unavailable`；实际行为取决于 `create_client` 是否 `follow_redirects`（upstream.py 待核）。
11. **`smoke_status`/`schema_degraded` 是启动时点快照**（411/398 只在 lifespan 写一次），运行期 schema 漂移无重探机制——health/ready 诊断长期陈旧。
12. **snapshotter start/stop 条件不对称**：`start()` 需 `traffic_snapshot_enabled and traffic_metrics_enabled`（720），`_stop_snapshotter` 却无条件注册（304）——依赖未 start 状态下 `stop()`（traffic_snapshot.py:374）幂等。
13. **快照剪枝 piggyback 的死区**：剪枝绑定在 access-log 维护循环（655-719）；`access_log_active=False`（245）时快照**永不剪枝** → `traffic_snapshot_enabled=true` + `access_log_enabled=false` 组合下 `traffic_snapshot_retain_days` 失效、磁盘无限增长。
14. **668-674 注释承认历史 bug**（`functools.partial` 关键字绑定撞 `today` 位置参数的 `TypeError` 被循环 `except Exception` 吞掉）——同类绑定错误今后仍会被维护循环静默吞掉，无告警面。
15. **`await dbaux.start()`@603 无本地 try/except**，依赖 `DbAuxiliarySource.start`（dbaux/lifecycle.py:362）不抛的隐式契约（589-597 注释声称）；`load_or_bump`@586 同理（corrupt/unwritable → fallback incarnation）——两者均为"注释级契约"。
16. **586 启动即写 incarnation 文件**（state_dir，fallback access_log_dir@584）：lifespan 中唯一非日志/快照类写盘动作（非 SQLite，不违硬规则，但审计应知悉）。
17. **无 CORS / 无 GZip 中间件、FastAPI 默认开启 `/docs` + `/openapi.json`**（734，未设 `docs_url=None`）：loopback + stunnel mTLS 部署下或可接受，但属显式未决策项（755 之后再无 `add_middleware`）。
18. **异常处理器覆盖面**：`register_error_handlers`@735 的处理器挂在最内层 ExceptionMiddleware；三个 pure-ASGI 中间件（747/753/755）自身抛错不会经过自定义处理器 → 原生 500 响应。
19. **路由遮蔽自证未验证**：sessions router 第 6 位挂载、token_stream 第 16 位（760）；756-759 注释声称 `/slimapi/sessions/{sid}/stream` 不被遮蔽——需在 routes/sessions 卡片核实无 `{sid}/{rest}` 类通配路径。
20. **中间件 contextvar 传播假设**（753-755）：RequestId 最外、TrafficAccounting 次外——traffic 记录若要携带 request-id，依赖外层 set / 内层 get 的 contextvar 传播成立（pure-ASGI 无 BaseHTTPMiddleware 的 task 拷贝问题，理论成立，确认实现）。
21. **`workers=1` 硬编码（779）无防呆**：`fulls`（32，进程级 registry）、lifespan 任务、replay epoch 均假设单进程；改 `workers>1` 会得到每 worker 一套台账/hub/epoch 且无报警。
22. **`app.state.config`（197）与模块级 `settings` 双源并存**：routes 两种取用方式混用（grep 可见），漂移风险低但契约面不统一。
23. **下划线属性挂公共 state**（455/657/684：`_replay_sweep_task`/`_access_log_stop_event`/`_access_log_maintenance_task`）：命名约定不一致，且测试直接伸手（tests/test_lifespan.py:237）。
24. **闭包变量 shadow**：254-256 `_close_access_log_handlers` 内重新绑定 `access_logger`，遮蔽外层 236 的同名变量——无害但易误读。
25. **`main()` 与 lifespan 重复执行 `setup_logging`+`validate`**（195-196 vs 769-771）：双路径无害，但 systemd 经 uvicorn CLI 直接加载时 config 错误以 traceback 而非 772-774 的友好信息呈现。
26. **491 `hubs.get_global()` 在启动期即急切实例化 GlobalHub**：其上游 `/global/event` 连接是即建还是首订阅才建，影响"启动即占上游连接"与否（归属 hub 卡片核实）。
27. **720-726 `start()` 成功后 snapshotter 循环内异常**是否被内部吞掉（traffic_snapshot.py 范畴）——本文件只兜了 start 失败。
28. 注释 typo：267 "non-toay"（应为 non-today）——纯文案。
