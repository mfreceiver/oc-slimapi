# D07 — A7 dbaux 深审（只读投影源基础设施）

> 审计专项报告 · Phase 2 / A7 · 2026-08-20
> 快照：`0b836e7`（BASELINE_HEAD = 0b836e78c5de62d0c73b8593bf62c6650043dedf，release: v4.4.0）。本文全部 `file:line` 证据属该快照。
> 输入：`src/oc_slimapi/dbaux/` 五文件全文亲读（`__init__.py` 105 行 / `path_resolution.py` 158 行 / `lifecycle.py` 768 行 / `cursor.py` 225 行 / `projection.py` 362 行）；`01-explore/upstream-notes.md`；`01-explore/state-machines.md` 卡 10/17；`01-explore/parts/e1-08-dbaux.md`（疑问点逐条复核——其中疑问 6a 的「自愈良性」结论**被本审计推翻**，见 §5.2）；`docs/specs/v4-contract.md` §4（含 §4.1-§4.6）；tests：`test_wal_staleness.py` / `test_dbaux_lifecycle.py` / `test_dbaux_metrics.py` / `test_sql_semantics.py` / `test_cursor_matrix.py` / `test_eqp_matrix.py` / `test_db_path_resolution.py` / `test_equivalence_anchor.py`（结构扫描 + 关键锚点亲读）。
> 上游真值源（只读对照）：`/home/mar/personal_projects/ocdroid/opencode-src/current/`（→ v1.18.18）`packages/core/src/database/database.ts`、`packages/core/src/database/schema.gen.ts`、`packages/opencode/src/session/session.ts`。
> 纪律：仓库零写入（无 pytest/pip/git 写、无真实 DB 连接；`urllib.parse.quote`/`posixpath.normpath` 行为以 python3 -c 纯函数验证）；除本报告与 02-findings 白名单外零写。

---

## 0. 审计范围与结论速览

| # | 审计项 | 结论 |
|---|---|---|
| 1 | 只读双保险 + 路径解析链 + URI 注入面 | **通过**（两道防线均在且为唯一 connect 点）；缺口 = explicit-env 相对路径未拒（F-241）+ `//` 前缀 URI authority 语义（F-248） |
| 2 | 单线程 executor 纪律 / 线程存活 / 关停归宿 | **主体通过**（max_workers=1、check_same_thread=True、连接操作全在 worker）；缺口 = stop 的 close submit 无界（F-236）+ 关停窗口异常逃逸 500（F-237）+ stop 后复用无守卫（F-245） |
| 3 | 断路器口径 + metrics 对照 + D10-1 | **实现与 §2.3-6 冻结口径一致**、metrics 字段一一对应；D10-1 **确认**（F-238）；D10-2/3 **确认**为设计内弱点（F-239） |
| 4 | SQL 全量逐条审计 | **3 条 SELECT + 2 条 PRAGMA 门读 + 1 条探针**，全参数化/全常量，无注入面；LIKE 转义与 §4.6 逐字对应；F-027 机制修正（单向 false-accept）、F-028 被上游 schema 真值关闭 |
| 5 | 活库读取一致性 | 上游 **WAL**（database.ts:27）+ busy_timeout 5000 同值；快照隔离=单查询级（契约自洽）；F-029 **确认**（WAL checkpoint 推 mtime → 周期 swap 清零熔断）；**新发现 inode 基线捕获双缺陷**（F-240，含对 e1-08 疑问 6a 的推翻） |
| 6 | 敏感信息泄面 | **wire 零泄**（health/metrics/503 hint 三面复核）；`snapshot()["path"]` 为未来泄面跟踪点（F-244） |

**发现账目**：更新 4 条（F-012 verified / F-027 verified+机制修正 / F-028 verified+降级 / F-029 verified）；新建 14 条（F-236..F-249）。

---

## 1. 只读双保险、路径解析链与 URI 注入面（任务 1）

### 1.1 双保险验证（✓ 两道防线都在，且覆盖所有连接路径）

唯一 `sqlite3.connect` 点（rg 全 src 验证，排除 `__pycache__`）：

```python
# src/oc_slimapi/dbaux/lifecycle.py:505-511
path = self._resolution.path  # type: ignore[union-attr]
uri = f"file:{quote(path, safe='/')}?mode=ro"          # :506 第一道：mode=ro URI
conn = sqlite3.connect(uri, uri=True)  # check_same_thread 默认 True   # :507
self._worker_thread_id = threading.get_ident()          # :508
conn.execute("PRAGMA query_only=ON")                    # :509 第二道：query_only
conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")  # :510（=5000，:302）
```

- **第一道** `mode=ro`（lifecycle.py:506-507）：SQLite 层拒绝任何写（含 DDL/PRAGMA 写域——AGENTS.md「SQLite 写域」硬规则的机制承载）。
- **第二道** `PRAGMA query_only=ON`（lifecycle.py:509）：连接级再次拦截（即使某路径意外绕过 mode=ro 仍拒写）。测试锁定：test_dbaux_lifecycle.py:596-608 `test_readonly_connection_never_writes`（INSERT → `query_cantinit` 禁用，重探恢复后写从未生效）。
- **覆盖面**：swap（:575 经 `_open_and_gate`）、reprobe（:657 同）、startup（:373 同）全部经 `_open_and_gate_sync`（:521-547）→ `_open_conn`（:499-511）——**每条连接都带双保险**，无第二建连点。
- `check_same_thread` 未显式传参 = 默认 `True` 保持（:507 注释 + 代码事实）；test_dbaux_lifecycle.py:290-306 断言 worker 外线程使用连接 → `sqlite3.ProgrammingError`。
- `immutable=1` 零出现（rg 验证；仅 docstring 否决记录 :11,:20,:502-503）；test_wal_staleness.py 全模块守护该弃用决策（case1/2 immutable 陈旧读、case3 :116-136 ro 经 -shm 读 WAL 全量）。
- **写域纪律符合性**：全 dbaux + 消费面无 DDL/DML；PRAGMA 仅 `query_only`/`busy_timeout`（连接配置，不改库文件）与 `table_info`（读）——lifecycle.py:103,105,509,510 全集。

### 1.2 路径解析链三级（path_resolution.py:82-133，逐级）

1. **`OC_SLIMAPI_OPENCODE_DB` 显式配置**（:95-100，最高优先）：`":memory:"` → `DisabledResolution("explicit-memory")`（:97-98）；否则 `_expanduser` + `normpath` → `ResolvedPath(source="explicit-env")`。**缺口**：不检查 `os.path.isabs`（对比 upstream-env 相对路径 :116-119 有挂数据目录语义）→ 相对路径原样通过 → **F-241**。
2. **`OPENCODE_DB` 上游 env**（:105-119）：`":memory:"` → `upstream-memory`；绝对路径或 `~` 前缀 → `upstream-env`（:110-114）；相对 → 挂数据目录 → `upstream-env-relative`（:116-119，复刻上游 database.ts:44-46 的 join 语义）。
3. **channel 候选发现**（:121-133，fail-closed R3 冻结）：`sorted(glob.glob(join(glob.escape(data_dir), "opencode*.db")))`（:124，`glob.escape` 已做 ✓、`sorted` 确定性 ✓）；恰一个 → `candidate-discovery` + 中文 warning（:125-130，`SINGLE_CANDIDATE_WARNING` :38 为测试断言锚点）；多候选 → `path_ambiguous`（detail=候选列表）；零候选 → `not_found`（detail=data_dir）。
   - 数据目录 = `XDG_DATA_HOME` 或 `~/.local/share` + `/opencode`（:75-79，复刻上游 global.ts:10-11）；XDG 相对值未校验绝对性（并入 F-241）。
   - **缺口**：glob 不滤目录——名为 `opencode-x.db` 的**目录**计入候选（F-242）。
- 解析结果一次性消费：app.py:598 启动唯一调用点；进程存活期内不重解析（F-012 的自愈不可能性根因之一）。
- `stat_inode_marker`（:136-145）：`(st_ino, st_mtime_ns)`，stat 失败 → None（不抛）；`-wal`/`-shm` 不参与。

### 1.3 URI 构造注入面（quote 逐点验证）

`quote(path, safe='/')`（lifecycle.py:506，`urllib.parse` 真值验证）：

| 路径字符 | 编码结果 | 判定 |
|---|---|---|
| `?` | `%3F` | ✓ 不截断 query 段 |
| `#` | `%23` | ✓ 不截断 fragment |
| 空格 | `%20` | ✓ |
| `%` | `%25` | ✓ 防二次解码歧义 |
| `\` | `%5C` | ✓ |
| `/` | `/`（safe） | ✓ 路径语义保留 |

SQLite URI 解码将 `%XX` 还原为文件名字符——注入面**封闭**。两个残留缺口：

- **相对路径**（F-241）：`rel/x.db` → `file:rel/x.db?mode=ro`，SQLite 接受相对 URI、按进程 cwd 解析——错库/语义歧义（生产进程 cwd ≠ 配置者预期）。
- **`//` 前缀**（F-248）：`posixpath.normpath('//srv/share/x.db')` = `//srv/share/x.db`（POSIX 保留双前导斜杠）→ `file://srv/share/x.db?mode=ro` → SQLite URI 的 authority 段语义在 unix 上被跳过 → 实开 `/share/x.db`（静默丢首段）。仅 explicit-env 手配 UNC 风格路径可达，P4。

---

## 2. 单线程 executor 纪律、线程存活与关停归宿（任务 2）

### 2.1 纪律验证（✓）

- `ThreadPoolExecutor(max_workers=1, thread_name_prefix="oc-slimapi-dbaux")`（lifecycle.py:325-327）；:323-324 注释明言共享多 worker 池 = 线程错误。
- **全部连接操作在 worker 内**：建连（`_open_conn` :499，经 `_open_and_gate_sync` :531）、查询（`_run_query` :465）、rollback（:484,:647）、重开（swap/reprobe 共用 `_open_and_gate_sync`）、关闭（`_close_conn` :513，经 stop :399 提交）——均经 `_submit`（:495-497，`loop.run_in_executor(self._executor, ...)`）。
- 事件循环侧仅 async 封装；`check_same_thread=True` 使 worker 外使用连接即 `ProgrammingError`（期望安全性质，test_dbaux_lifecycle.py:290-306 断言）。
- FIFO 锁交接：swap 与在途查询同 worker 串行——test_dbaux_lifecycle.py:369-392 `test_swap_during_active_query_no_deadlock`（`_in_txn_pause` 钩子 :473-474 模拟慢查询中 swap 入队，查询先完成再 swap，无死锁）。

### 2.2 线程存活 / 回收

- worker 线程惰性创建（首次 submit），进程存活期内不回收（`concurrent.futures` 无 idle 超时）；仅 stop 关停。
- stop 的 drain watcher（:419-422）为一次性 daemon 线程，`done.wait(timeout=wait_seconds)`（:423）有界返回。
- `_worker_thread_id`（:508）只写不读（rg 全仓无消费者）——死代码，入 F-246 卫生合集。

### 2.3 关停时在途查询归宿（F-236 / F-237 / F-245）

`stop(drain_seconds=5.0)`（:383-405）四段：

1. :386-397 周期任务停（`wait_for` 有界 drain_seconds，超时 cancel）✓；
2. **:399 `await self._submit(self._close_conn)` —— 无界**：close 提交进同一 worker FIFO，排在任何在途查询之后。在途查询最坏 = busy_timeout 5s（锁等待）+ fetchall 物化时间（大结果集无上界）→ **stop 阻塞事件循环超出 5s drain 预算**（app.py:85 `_DBAUX_DRAIN_TIMEOUT=5.0` 只约束段 1/3/4，不约束段 2）→ **F-236（P3）**。与 F-010 关停链预算（systemd TimeoutStopSec=15）叠加。
3. :402 `_bounded_executor_shutdown`（:407-423）：`shutdown(wait=False, cancel_futures=True)` 取消 pending + 守护线程有界等待——本身正确；但 cancel 只作用于 pending，**运行中的查询不可取消**，watcher daemon 线程可能滞留（进程退出无害，记录）。
4. :405 `_disable("stopped")`（终态，:403-404 注释「重启用新实例」）。

**关停窗口异常逃逸（F-237，P3）**：`_disable("stopped")` 在段 2 的 await 之后才执行。窗口内（close 已入队/已执行、executor 已 shutdown、state 仍 "available"）并发 `query()`：
- (a) status 门通过（:440-441）→ `_submit` 对已 shutdown executor → `RuntimeError("cannot schedule new futures after shutdown")`；或
- (b) `_run_query` 排在 close 后执行 → `assert self._conn is not None`（:467）`AssertionError`（`-O` 下 assert 剥除 → `None.cursor()` 的 `AttributeError`）。
两者均非 `sqlite3.Error`，query 的 except（:448）不捕 → sessions.py:443-450 只捕 `AuxiliaryUnavailableError`/`sqlite3.Error` → **FastAPI 500**。窗口亚秒级但真实可达（uvicorn 关停宽限 5s 内仍在收请求）。

**stop 后 start 复用无守卫（F-245，P4）**：stop 置 `_task=None`（:388）→ 再 start 通过幂等检查（:368-369）重建周期 task，但 executor 已 shutdown（:410）→ 首次 `_submit` 即 RuntimeError。生产 app 不重启实例（lifespan 一次性），无触发面；无 `_closed` 防御标志。

---

## 3. 断路器：口径、metrics 对照、在途语义与 D10 系列（任务 3）

### 3.1 实现口径 vs §2.3-6 冻结（✓ 一致）

`LatencyBreaker`（lifecycle.py:147-247）：

| 冻结口径 | 实现 | 证据 |
|---|---|---|
| 滑动窗口 60s | `_window_s=60.0`，`_prune` 惰性剪枝（重绑 `_samples`） | :159-168, :181-184 |
| ≥10 样本才计 P99 | `len(self._samples) < self._min_samples: return` | :208-209 |
| 前 10 次 warmup 豁免 | `if self._total <= self._min_samples: return` | :205-207 |
| P99 ≥ 20ms → open | `_p99() >= self._trip_ms → self.trip()`；trip=置 open+清窗 | :210-212, :214-217 |
| 半开探针 30s 周期 | `probe_interval_s=30.0`（:315）；`_next_probe_at` 排期（:622,:681） | tick :611-614 |
| P99 < 10ms 恢复（hysteresis） | `note_probe`：`p99 < self._recover_ms → open=False` | :219-230 |
| 最近邻秩百分位 | `rank = max(0, -(-n*pct//100) - 1)`（ceil 取秩） | :186-192 |
| swap/重开成功清零 | `reset()`（samples/total/open 全清，warmup 重起算） | :232-236, :563 |

测试锚定：test_dbaux_lifecycle.py:464-506（min_samples/window slide/hysteresis/probe failure 四组假时钟）+ :520-557（端到端 trip→probe 恢复、swap reset warmup）。

### 3.2 源级状态转移表（实测归纳，含事件行号）

| 态 | 事件 | 次态 | 证据/守卫 |
|---|---|---|---|
| disabled(reason∈startup 族) | tick ③ reprobe 成功 | available | :616-617, :652-661；`_reprobe_allowed`（:349-352，memory 族永久禁） |
| disabled(**not_found/path_ambiguous**) | tick ③ reprobe | disabled(**open_failed**，reason 漂移) | **F-012**：`_open_conn` :505 对 `DisabledResolution` 取 `.path` → AttributeError → :552 捕获 → open_failed；30s 循环 warning；不可自愈（解析不重跑，app.py:598） |
| available | query 错误 classified∈{schema,io,cantinit,programming} | disabled(query_*) | :448-460（programming=error log :457，余 warning） |
| available | query busy | available（不禁不熔断态） | :450-453 原样 raise；延迟已计样本（:489）→ P99 路径自洽 |
| available | P99≥20ms（worker record 内 trip） | circuit_open | :210-212 → 查询返回前/后由 `_check_breaker_state`（:462,:683-689）联动；`_next_probe_at=now+30s`（:681）；trips 计数（:677-679） |
| circuit_open | tick ② 到期 probe 成功 ∧ P99<10ms | available | :611-614, :619-634（`note_probe` :630） |
| circuit_open | probe 失败（异常）或 P99≥10ms | circuit_open（重排 30s） | :626-628（info log）; :622 |
| available | tick ① inode/mtime 变 | swap → available(gen+1) 或 disabled(gate/open_failed) | :602-609 → :566-578 |
| circuit_open | tick ① inode/mtime 变 | **circuit_open（检查被跳过）** | :602 守卫 `state=="available"` + :611-614 早 return → **F-238（D10-1 确认）** |
| 任意 | stop | disabled(stopped)（终态） | :383-405；"stopped" 不计 disables（:669-670） |

### 3.3 metrics `dbaux` 块一一对照（✓）

breaker.snapshot()（lifecycle.py:238-247）→ metrics.py:51-69 映射：

| 源字段 | metrics 字段 | 备注 |
|---|---|---|
| `available/mode/reason/generation` | 同名直传 | 状态三态形状 |
| `source` | `source` | 解析通道标签（无路径） |
| `breaker.open` | `breaker_open` | |
| `breaker.{p50_ms,p99_ms,samples,total}` | `latency.{…}` 四键 | |
| `counters{queries,probes,trips,swaps,disables}` | `counters` | :341-347 定义；probes=半开+禁用重探双计（:621,:656）；swaps 仅成功换手（:576-577）；disables 不含 stop（:669-670） |

- **path 显式不进 metrics**（metrics.py:48-50 注释 + 代码只取白名单键）；test_dbaux_metrics.py:112-115 断言 `"path" not in block` 且响应文本无 `.db`。
- 契约 §9.1 五维度（查询延迟 P50/P99 ✓、降级计数 ✓（counters.disables + 路由层 sessionsDegraded 每响应计数——metrics.py:70-84 分工注释明确）、熔断计数 ✓、重探事件 ✓、inode swap 事件 ✓）全落地。
- `snapshot()`（lifecycle.py:707-727）**本身含明文 `path`**（:722-726）——当前唯一消费方丢弃，公开 API 为未来泄面（**F-244**）。

### 3.4 断路器打开瞬间的在途请求语义

- trip 发生在 worker 内 `_run_query` 的 finally `breaker.record()`（:489）——**触发 trip 的那个查询已 fetchall 完毕、正常返回行**（延迟记录在数据取得之后）。
- 已通过 status 门（:440-441）并在 FIFO 排队/执行中的查询**继续执行完**（admitted-before-trip 语义）；worker 串行保证无并发连接争用。
- trip 后首个 `status()`（query :440 / health :83 / metrics :53）经 `_check_breaker_state`（:691-692 惰性联动）翻转为 circuit_open → 后续请求 `AuxiliaryUnavailableError("circuit_open")`（:442）→ 路由层 §4.2 降级矩阵（503+Retry-After:30 或 Class A 200+degraded）。
- 副作用注记（D10-5，低危）：`status()` 含状态联动副作用——health 高频轮询会「见证」trip 而非触发 trip（trip 源恒为 record）。

### 3.5 D10-1 / D10-2 / D10-3 复核

- **D10-1 确认（F-238，P3）**：tick 的 ①inode 校验守卫 `self._state == "available"`（:602）+ ②circuit_open 分支早 return（:611-614）→ **熔断期间换库检测被跳过**。放大因素：`_probe_sync` 的 `SELECT 1`（:641）**不触任何表页**——旧连接（fd 指向已被 replace/delete 的旧文件）恒探针成功 → 恢复 available → **恢复后到下一个 tick（≤30s）之间所有真实查询读旧库**；若旧文件被删除而非替换，旧 fd 继续可读（陈旧无界，直到该窗口内某查询碰巧 schema 错误禁用）。契约不承诺新鲜度（§4.5），但 swap 机制的设计目的在该窗口失效。
- **D10-2/D10-3 确认（F-239，P3 risk，设计内）**：`trip()` 清窗（:216-217）后 `note_probe` 无最小样本数要求（:225-229）——**首个 <10ms 探针即闭合**（P99=该单样本）；且 SELECT 1 不测投影延迟分布 → 恢复证据系统性弱于 trip 证据。振荡周期 ≈ 30s 探针间隔 + 10 个慢查询。实现与 §2.3-6 冻结措辞（「半开探针样本计入后 P99<10ms 恢复」）**逐字一致**——弱点属设计口径而非实现偏离。测试 test_breaker_hysteresis_probe_recovery（:489-506）反向锚定：单个 15ms 探针把窗口 P99 钉在 15ms 达 60s（最近邻秩在 n<100 时≈max）。

---

## 4. SQL 全量逐条审计（任务 4）

### 4.1 清单（生产路径全集，rg 验证无遗漏）

| # | 语句 | 位置 | 参数化 | 判定 |
|---|---|---|---|---|
| S1 | 投影主查询 `SELECT {24 列}, p.id AS p_id, … FROM session s LEFT JOIN project p … WHERE … ORDER BY s.time_updated DESC, s.id DESC LIMIT ? + 1` | projection.py:229-241 | 全 `?`（parent sid :186-187 / search pattern ×2 :196-197 / allowlist item+prefix_len+prefix :214 / cursor 锚 ×3 :227 / limit :241） | ✓ |
| S2 | 单行点查 `SELECT … FROM session s LEFT JOIN project p … WHERE s.id = ?` | read_groups.py:425-434 | sid 单 `?`（:527 绑定） | ✓ |
| S3 | 半开探针 `SELECT 1` | lifecycle.py:641 | 无参常量 | ✓ |
| P1/P2 | 门读 `PRAGMA table_info(session)` / `PRAGMA table_info(project)` | lifecycle.py:103,105 | 表名冻结常量 | ✓ |
| P3/P4 | 连接配置 `PRAGMA query_only=ON` / `PRAGMA busy_timeout=5000` | lifecycle.py:509-510 | 类常量 int（:302） | ✓（非写域） |
| T1-T3 | `BEGIN`（deferred）/ `COMMIT` / finally `ROLLBACK` | lifecycle.py:472,477,484; :640,643,647 | 常量 | ✓ |

**SELECT 共 3 条（S1/S2/S3）+ 2 条 PRAGMA 门读**；拼接进 SQL 文本的仅冻结谓词片段与列名常量（`SESSION_PROJECTION_COLUMNS` :65-90 / `PROJECT_JOIN_COLUMNS` :94 / 谓词模板串）——**无用户输入拼接面，无注入**。S2 与 S1 的列序双份维护（read_groups.py:426-428 注释自认）——漂移温床记录（zip 错位仅在两处列序漂移时发生，SELECT 显式列使行宽恒等）。

### 4.2 search LIKE 转义 vs §4.6（✓ 逐字对应）

- `escape_like`（projection.py:104-110）：`\`→`\\`（**最先**，防二次转义）、`%`→`\%`、`_`→`\_`；测试 test_sql_semantics.py:221（`escape_like("a%b_c\d") == "a\%b\_c\\d"`）。
- SQL 侧 `(? IS NULL OR s.title LIKE ? ESCAPE '\')`（projection.py:192,196——Python 源 `"\\'"` → SQL 字面 `ESCAPE '\'`）；pattern = `'%' + escape_like(norm) + '%'`（:195）。
- 契约 §4.6：pattern 形状、字面子串语义、`ESCAPE '\'`、trim-后-转义-前 hash——实现一一对应（§4.5 四消费点同源：SQL pattern :195 / has_wildcard :113-120 / 指纹 cursor.py:88-97 / 降级透传 sessions.py:508-509 传 normalized）。
- search 轴恒在（None 时 `? IS NULL` 恒真形 + 双 None 绑定 :191-193）——SQL 形状稳定（EQP 特征不随参数缺席漂移，test_eqp_matrix.py 48 组合断言 SCAN+TEMP B-TREE 恒定）。
- 大小写：SQLite LIKE 默认 ASCII 折叠 = 上游 drizzle `like()`（session.ts:563）同源——等价性锚点成立（test_search_plain_substring 双向命中断言）；`case_sensitive_like` 是 per-connection 非持久 PRAGMA，sidecar 自有连接恒默认 ✓。
- allowlist 谓词（projection.py:199-215）vs §4.6：`(s.directory = ? OR substr(s.directory, 1, ?) = ?)`、prefix=`item+'/'` 独立绑定、prefix_len=len+1、根 `/` 特例 `substr(…,1,1)='/'`（:211）、多顶 OR 并集、二进制比较大小写敏感——逐条对应；test_sql_semantics.py:84-108,227-253（兄弟前缀排除 / 深子树 / 并集 / 大小写 / 根特例 / 字面 %_ 段）。

### 4.3 archived/parent 过滤 vs §4.1 参数矩阵（✓）

| 参数 | §4.1 | 实现（projection.py） |
|---|---|---|
| archived=omit（默认） | time_archived IS NULL | :175-176 |
| archived=only | IS NOT NULL | :177-178 |
| archived=all | 无谓词 | （:174 分支不落） |
| parent=all（默认） | 无谓词 | :185 分支不落 |
| parent=none | parent_id IS NULL | :181-182 |
| parent=only | parent_id IS NOT NULL（R6 冻结） | :183-184 |
| parent=\<sid\> | `parent_id = ?` 字面绑定 | :185-187 |

路由侧域校验（sessions.py:389-402）+ 组装器下界防御（projection.py:164-169）双层。上游 schema 真值：`parent_id` text **nullable**、`time_archived` integer nullable（schema.gen.ts:186,211）——三态谓词语义与真值相容。

### 4.4 排序与 cursor {t,i,f} keyset 一致性（✓ 主路径封闭；F-028 边缘确认）

- 排序冻结 `(time_updated DESC, id DESC)`（projection.py:238）= 上游 HTTP `/session` 列表 `orderBy(desc(time_updated), desc(id))`（opencode/src/session/session.ts:574）——契约 §4.1「事实同构」成立（注意：core 的 V2Session.list 排序键是 time_created，session.ts(core):272，但该路径**不服务** HTTP /session——本审计已沿 handler import 链定论 handlers/session.ts:8 → `@/session/session`）。
- keyset 下推 `(t < ? OR (t = ? AND id < ?))`（projection.py:226）= §4.5 复合谓词 OR 展开形（不依赖 ≥3.15 行值）。
- 封闭性条件 = t 非空 int ∧ id 唯一。**上游真值关闭了破口**：`id text PRIMARY KEY`（schema.gen.ts:183）+ `time_updated integer NOT NULL`（:209，drizzle `$onUpdate(() => Date.now())` 恒 int）——F-028 在当前对齐版本**不可达**。
- 残余：schema 门只校验**列名**存在（lifecycle.py:97-107 集合成员判定），不校验类型/NOT NULL/PK——上游演进改型（或手改库）时 F-028 路径复活：(a) NULL-t 行 DESC 排尾且谓词恒假 → 首页后永不可达（静默丢行）；(b) REAL/TEXT-t 行可返回但不可为锚（`_window_anchor` :326-328 要求 `isinstance(t, int)`）→ 窗口尾部全为非 int-t 行时 anchor=None + complete:false → sessions.py:469 不发 nextCursor 的**死端页**（「还有更多但拿不到游标」）。F-028 据此更新为 verified/理论性、建议降 P4。
- anchor 窗口纪律（BLOCKER-3）✓：`_window_anchor` 只扫 `rows[:limit]`（:323，不含第 limit+1 行）；坏行不丢锚点（分页不死锁）；complete 用原始行集口径（:357，容忍跳行不放大 complete）。

### 4.5 schema 门对上游演进的容忍度

- **加列**：容忍（门 = 必需列 ⊆ 实有列，:103-106 集合判定）——当前上游 session 表 29 列含 sidecar 未投影的 workspace_id/slug/path/cost/share_url（schema.gen.ts:182-213），门通过 ✓。
- **删列/改名**（任一 24+3 列）→ gate_failed 禁用 → 全降级 HTTP + 30s 重探循环（ResolvedPath 有 path，重探语义正确，恢复条件 = 上游回滚或 sidecar 更新）——fail-closed 方向正确；副作用 = 升级对齐期 30s 一次 warning + ocdroid 视角全量 degraded。
- **改型不改名**：门**不发现**（4.4 残余）。

---

## 5. 活库读取一致性（任务 5）

### 5.1 上游 journal 模式定论（WAL）与 busy_timeout

上游每库打开即设（`packages/core/src/database/database.ts:27-32`）：

```ts
yield* db.run("PRAGMA journal_mode = WAL")      // :27 —— WAL 定论
yield* db.run("PRAGMA synchronous = NORMAL")    // :28
yield* db.run("PRAGMA busy_timeout = 5000")     // :29 —— sidecar BUSY_TIMEOUT_MS=5000 同值（lifecycle.py:302）
yield* db.run("PRAGMA cache_size = -64000")     // :30
yield* db.run("PRAGMA foreign_keys = ON")       // :31
yield* db.run("PRAGMA wal_checkpoint(PASSIVE)") // :32 —— 启动即 checkpoint
```

- WAL 为库文件头持久属性 → sidecar ro 连接（无 journal PRAGMA）读同一 WAL 库；ro 经 `-shm` 读到 `-wal` 内已 commit 数据（test_wal_staleness.py:116-136 实证锚定）；**未设 `wal_autocheckpoint=0`** → SQLite 默认 1000 页自动 checkpoint 生效（F-029 机制前提）。
- busy 行为：WAL 下读写不互斥，读侧 BUSY 罕见；发生时 sidecar busy_timeout 5s 等待 → 仍 busy 则 classify=busy（:138-139）→ 原样 raise（:453）→ 路由 503 fail-closed；延迟计入 P99（:489）→ 超限熔断路径自洽。与 §4.2「busy 不禁用」一致。
- `-shm` 不可达场景（上游进程退出且清理后 sidecar 首开/重开）：ro 打开 WAL 库无 -shm → "unable to open database file" → cantinit（:132-135）→ 禁用+30s 重探，上游回归后自愈——正确且**不试 immutable**（§1.3 弃用纪律，lifecycle.py:20,:502-503）。

### 5.2 inode 探测 × 文件替换竞态（F-029 复核确认 + F-240 新发现）

- **F-029 确认（P3 risk）**：marker=`(st_ino, st_mtime_ns)`（path_resolution.py:145）；tick :604 **tuple 不等即 swap**。WAL 模式下主 .db 文件 mtime **只在 checkpoint 时推进**（写先进 -wal；自动 checkpoint 每 1000 页 + 上游启动 PASSIVE）→ 写频繁期每 30s tick 大概率命中 mtime 漂移 → swap → `breaker.reset()`（:563，清样本+warmup 重起算）+ generation+1 + swaps 计数膨胀。**P99 熔断护栏在写频繁期被反复清零，形同虚设**。实现与设计冻结（design-v4-dbaux「对比 st_ino/st_mtime_ns；变化 → swap」）一致——设计动机是「替换文件」，mtime 维度过敏感。测试只覆盖 rename 换 inode（test_dbaux_lifecycle.py:399-414；test_dbaux_metrics.py:182-197），mtime-only 路径未测。
- **F-240 新发现（P3 defect，推翻 e1-08 疑问 6a 的「自愈良性」结论）——inode 基线捕获双缺陷**（同一代码位点 lifecycle.py:546-547 + 守卫 :604）：
  - (a) **stat 失败分支**：open 成功后文件即被删 → `marker=None` → `self._inode=None`；tick 守卫 `marker is not None and self._inode is not None`（:604）双非空才比较 → **该连接存续期内 swap 检测永久静默失效**（陈旧读无界，仅 query 错误禁用或重启可破）。
  - (b) **open→stat 间隙换库**：t1 open 拿到旧文件 fd；t2 `os.replace` 新文件；t3 stat 记录的是**新文件** marker → `_inode`=新值 → 此后每 tick `marker == _inode` → **永不触发 swap**，连接终生读旧（已 unlinked）文件。间隙宽度 = schema 门查询时长（毫秒级），但 reprobe/swap 与上游重启/恢复操作并发时可命中。e1-08 疑问 6a 判「下个 tick 再 swap 一次（自愈）」是**方向性错误**（记新 marker 恰好掩盖旧连接）。
  - 修复方向（非实现）：stat 应在 open **前**取基线，或 open 后 stat 失败/比对不一致时立即重 stat 校验；至少 (a) 分支应对 `_inode=None` 走禁用重探而非静默。

### 5.3 快照隔离与 v4 翻页一致性（定级：契约自洽，无缺陷）

- **单查询内**：`BEGIN`（deferred，:472）→ 首个读语句取读快照 → fetchall → COMMIT——投影 + LIMIT+1 complete 判定 + anchor 同快照（fetch_sessions_page projection.py:356-361 一次物化）✓ §4.1「同一只读 snapshot」承诺兑现。
- **跨页**：每页独立事务 = 独立快照。跨页写入的行漂移：新行/被更新行（t 增大）→ 落锚点之上，进行中的翻页看不到（客户端刷新首页才见）；锚点之下被更新 → 同理不可见；删除 → 消失；DESC + t 单调增（Date.now）下重复见行实际不可达（钟回拨除外）。**全部在契约 §4.5 明示不承诺范围内**（「不承诺并发更新零重复零遗漏，跨边界重见为预期行为」）。
- **f 指纹的能力边界验证**：f={archived,parent,search_hash,allowlist_rev}（cursor.py:70-76）防**过滤上下文漂移**（改参翻旧页 → 400 invalid_cursor，sessions.py:420-427）——**防不住时间旅行**（同参跨页数据漂移，设计如此）。已验证并定级：**非缺陷，契约明示**；f 的 allowlist 维度存在单向粗化（F-027，见下）。
- **F-027 机制修正（verified）**：指纹侧 `allowlist_rev` 对项 strip+去空+去重（cursor.py:109-113；test_cursor_matrix.py:299-304 **有意锚定**该归一），SQL 谓词侧用**原样**项（projection.py:200-214 只拒非 str/空串；sessions.py:365-368 同不 strip）。后果是**单向 false-accept**：`"/a "`（尾空白）与 `"/a"` 同指纹但 SQL 行为不同（前者绑 `"/a "`/`"/a /"` 匹配不到行）→ (i) 含尾空白配置静默零结果（fail-closed 方向，不泄行）；(ii) 运维修正空白后指纹不变 → 旧 cursor 被继续接受（stale-cursor false-accept）。原 draft 标题中「可产生 400」不成立（不同指纹必对应不同 strip 后集合 → SQL 亦不同 → 400 是正确行为）。定级维持 P3（误配置触发面）。

---

## 6. 敏感信息泄面（任务 6）——逐 except 分支验证

| 路径 | 内容 | 去向 | 判定 |
|---|---|---|---|
| `AuxiliaryUnavailableError.__init__`（lifecycle.py:260-262） | `f"db auxiliary unavailable: {reason}"`——reason 恒粗粒度标签（circuit_open/query_schema/query_io/query_cantinit/query_programming/stopped/open_failed/gate_failed/not_found/…） | 异常消息**不进 wire**（sessions.py:443-444 捕获后弃消息换统一 hint；read_groups.py:528-530 同） | ✓ |
| query 错误分支（lifecycle.py:456-460） | `log("dbaux query error classified=%s", kind, exc_info=exc)`——完整 exc（可含列名/锁细节） | 仅 journald 日志域 | ✓（§7.4/§4.2 允许） |
| open/gate 失败（lifecycle.py:552-558） | `_reason_detail = str(exc)`（gate_failed 时含缺失列名清单 :535-536） | 仅日志（:557-558 warning + 启动 log :737-742）；sqlite 异常文本不含路径（实测文案族） | ✓ |
| 503 统一体（sessions.py:276-306） | hint=`"session projection is temporarily served from a degraded source; retry shortly"` + `Retry-After: 30`——无 DB 路径/schema/白名单内容 | wire | ✓ §4.2「错误体不泄露」逐项满足 |
| busy 分支（:450-453 → sessions.py:445-450） | sqlite3.Error 原样上抛 → 路由 `raise _fail_closed_503(request) from None`——**from None 切断异常链** | wire 仅统一 503 体 | ✓（BLOCKER-1 规范） |
| health `auxiliary_view`（lifecycle.py:278-283） | `{available, mode[, reason]}`——reason 仅不可用时出现 | wire | ✓ 无路径 |
| metrics dbaux 块（metrics.py:51-69） | 白名单键；path 显式丢弃（:48-50） | wire | ✓（test_dbaux_metrics.py:112-115 断言） |
| `snapshot()`（lifecycle.py:707-727） | **含明文 `path`**（:722-726） | 公开 API；当前唯一消费方不透出 | **F-244 跟踪点**（未来消费方易带上网） |
| 启动 log（lifecycle.py:729-752） | path/source/warning/gate 结果 | 日志域 | ✓（运维需要；logs 属允许域） |

**结论：wire 三面（health / metrics / 503 错误体）零 DB 内部信息泄露**；日志域含路径与 gate 细节属设计允许。cursor 错误 `InvalidCursorError.reason`（cursor.py:62-63 粗粒度标签）也仅进日志不进 wire（sessions.py:413-419 换统一 hint）✓。

---

## 7. 发现清单（本专项产出）

### 7.1 主辖更新（4 条）

| 编号 | 终态 | 要点 |
|---|---|---|
| F-012 | **verified**，建议 P2→**P3** | not_found/path_ambiguous 重探必 AttributeError（lifecycle.py:505 `.path` 于 DisabledResolution）→ reason 漂移 open_failed + 30s warning 循环，不可自愈（resolve_db_path 仅 app.py:598 一次性）。无 wire/功能影响（降级 HTTP 正确），但 health reason 误导运维（「临时不可开」假象）。测试只覆盖 explicit-memory 的 reprobe=False（test_dbaux_lifecycle.py:267-283） |
| F-027 | **verified**，P3，机制修正 | 单向 false-accept（指纹 strip 粗于谓词不 strip）：`"/a "` 与 `"/a"` 同指纹异行为；draft 标题「可产生 400」不成立。触发面 = 误配置 |
| F-028 | **verified**，建议 P2→**P4** | 上游真值关闭：`id text PRIMARY KEY` + `time_updated integer NOT NULL`（schema.gen.ts:183,209）→ NULL/非 int t 不可达。残余 = schema 门不校验类型（演进漂移防御缺口）：NULL-t 首页后不可达 / 非 int-t 死端页（anchor None + complete:false） |
| F-029 | **verified**，P3 | WAL 自动 checkpoint（默认 1000 页，上游未禁）推主库 mtime → tick tuple 比较触发 swap → `breaker.reset()` 反复清零熔断统计；写频繁期护栏失效 |

### 7.2 新建（F-236..F-249，14 条）

| 编号 | 级/类 | 标题 |
|---|---|---|
| F-236 | P3 defect | stop() 的 close submit 无界等待（lifecycle.py:399）——在途查询（busy 5s + fetchall）可把 stop 拖出 5s drain 预算 |
| F-237 | P3 defect | 关停窗口非 sqlite 异常逃逸 → 500：close 后 `_run_query` assert None（:467；-O 下 AttributeError）或 executor 已 shutdown 的 RuntimeError——query :448 只捕 sqlite3.Error，sessions.py:443-450 同 |
| F-238 | P3 defect | D10-1 确认：circuit_open 期间 inode/mtime 校验被早 return 跳过（:602,:611-614）+ SELECT 1 探针不触表 → 恢复后 ≤30s 读旧库窗口（删库场景陈旧无界） |
| F-239 | P3 risk | D10-2/3 确认：单快探针即闭合熔断（note_probe :225-229 无最小样本）+ SELECT 1 与投影延迟不同源 → 恢复证据弱、可振荡（设计冻结口径内） |
| F-240 | P3 defect | inode 基线捕获双缺陷：stat 失败 → `_inode=None` 永久盲（:546-547,:604）；open→stat 间隙换库记新 marker 掩盖旧连接（推翻 e1-08 疑问 6a「自愈」结论） |
| F-241 | P3 defect | explicit-env 相对路径未拒（path_resolution.py:96-100 无 isabs）→ `file:rel?mode=ro` 按进程 cwd 打开错库；连带 XDG_DATA_HOME 相对值未校验 |
| F-242 | P4 defect | 候选 glob 不滤目录（:124）——`opencode*.db` 目录计入候选 → open_failed 30s 无效重探循环 |
| F-243 | P4 risk | LatencyBreaker 跨线程无锁：worker `record`（:489）vs 事件循环 `note_probe`/`reset`/`snapshot`（:630,:563,:238）——GIL 下无崩溃，metrics p50/p99/samples 可瞬时失真 |
| F-244 | P4 risk | `snapshot()` 公开返回明文 DB path（:722-726）——当前 metrics 丢弃；未来消费方带上网的泄面跟踪点 |
| F-245 | P4 quality | stop 后 start 不可复用无守卫（executor 已 shutdown → 首次 submit RuntimeError；:403-405 注释声明但代码无 `_closed` 标志） |
| F-246 | P4 quality | dbaux 卫生合集：`_worker_thread_id` 只写不读（:508）；lifecycle `__all__` 缺 `DbAuxStatus`（:47-55）；`PARENT_RESERVED_STATES` 全仓零消费者（projection.py:82）；path_resolution `__all__` 导出无人消费的 `Path`（:157）；导出面三策略并存（有/缺/无 `__all__`） |
| F-247 | P4 risk | cursor 解码无长度上限（cursor.py:165-208 「不过度防御」明示；10KB 合法 cursor 测试锚定 :373-378）——事件循环内 b64decode+json.loads 的 CPU/内存面；stunnel mTLS 单客户端部署缓解 |
| F-248 | P4 defect | `//` 前缀路径经 `file://authority` URI 语义丢首段（quote safe='/' 保留双斜杠 + normpath 保留 `//`；unix 上 SQLite 跳过 authority）——UNC 风格手配路径静默错库 |
| F-249 | P4 defect | `_expanduser` 注入 home 语义分歧（path_resolution.py:68-69）：`~bob/x` + 注入 home → `home+"bob/x"`（缺分隔符）≠ 生产 `os.path.expanduser` 的用户目录查找——测试语义与生产分叉 |

### 7.3 报告内记录不计编号的观察

- `status()` 的惰性 trip 联动副作用（D10-5）：health 轮询「见证」而非触发，低危。
- `_probe_sync` 探针延迟含 executor 排队延迟（:623-629 墙钟包裹 await submit）——证据进一步弱化的次因（并入 F-239 叙述）。
- `OPENCODE_DISABLE_CHANNEL_DB` 注释声明（path_resolution.py:121-123）依赖上游建库名恒匹配 `opencode*.db`——上游命名变更时 not_found 静默降级（升级对齐观察点）。
- read_groups `_SESSION_SINGLE_SQL` 与 projection SELECT 形状双份维护（read_groups.py:425-437）——列序漂移温床（重构跟踪点）。
- core `V2Session.list` 排序键为 time_created（core/session.ts:272）但**不服务** HTTP /session——已定论，消除契约 §4.1「事实同构」表述的潜在歧义。

---

## 8. 完成判据对照

- 任务 1 只读双保险/路径链/注入面：✔（§1，quote 逐点实证）。
- 任务 2 executor 纪律/线程/关停归宿：✔（§2，F-236/237/245）。
- 任务 3 断路器口径/metrics 一一对照/在途语义/D10-1：✔（§3，转移表 + D10-1/2/3 全复核）。
- 任务 4 SQL 全量逐条 + LIKE 转义 + 参数矩阵 + keyset 封闭性 + schema 门容忍度：✔（§4，SELECT 3 条 + PRAGMA 2 条清单）。
- 任务 5 WAL 定论/busy_timeout/inode 竞态/快照隔离定级：✔（§5，F-029 确认 + F-240 新发现 + f 指纹边界定级）。
- 任务 6 敏感信息逐 except 分支：✔（§6 全表）。
