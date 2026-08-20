# E1-08 dbaux 精读卡片（只读审计 · 2026-08-20）

范围：`src/oc_slimapi/dbaux/` 五文件全文精读（禁止抽样）。引用格式 `src/oc_slimapi/...:行号`。

---

### src/oc_slimapi/dbaux/lifecycle.py（768 行）

- **职责**：v4 sessions DB 投影源的连接生命周期——路径打开（ro + query_only 双保险）、专属单 worker 线程亲和、短事务查询通道、schema 门、P99 熔断、inode/mtime 校验 swap、错误分类重探、状态/计数快照。设计权威 design-v4-dbaux §1/§2/§4/§6（模块 docstring :1-27）。

- **对外符号**：
  - `SESSION_PROJECTION_COLUMNS` :65 — session 表 24 投影列冻结清单（真库列名 tokens_input/output 等）。
  - `PROJECT_JOIN_COLUMNS` :94 — project join 列 `(id, name, worktree)`。
  - `schema_gate_missing(conn)` :97 — `PRAGMA table_info` 只读比对两表投影列，返回缺失列清单（[] = 通过）。
  - `classify_sqlite_error(exc)` :114 — 错误文本匹配分类到 {schema, io, cantinit, busy, programming, other}。
  - `LatencyBreaker` :147 — P99 熔断护栏（纯内存，可注入 clock）：
    - `__init__` :159 — window_s=60 / min_samples=10 / trip=20ms / recover=10ms。
    - `open` property :177 — 熔断态布尔。
    - `_prune` :181 — 滑窗惰性剪枝（重绑 `_samples`）。
    - `_pctl` :186 — 最近邻秩百分位（`ceil(pct*n)-1`，:191）。
    - `_p99` :194。
    - `record` :197 — 计样本；关闭态且过 warmup（`_total <= min_samples` :206）且窗内样本 ≥10（:208）时判 P99≥20ms → `trip`。
    - `trip` :214 — open=True 并清空窗口（恢复需新鲜证据）。
    - `note_probe` :219 — 半开探针结果计入；P99<10ms → 闭合返回 True。
    - `reset` :232 — swap/重开成功后全清零（warmup 重起算）。
    - `snapshot` :238 — {open, samples, total, p50_ms, p99_ms}。
  - `AuxiliaryUnavailableError(RuntimeError)` :254 — `reason` 属性 :262；query 被拒信号。
  - `DbAuxStatus` :270 — frozen dataclass（available/mode/reason/generation）；`auxiliary_view()` :278 — health `auxiliary` 字段（仅 available/mode/reason，reason 只在不可用时出现）。
  - `DbAuxiliarySource` :286 — 主体：
    - `BUSY_TIMEOUT_MS = 5000` :302 — 对齐上游 database.ts:29。
    - `__init__` :304 — 建 `ThreadPoolExecutor(max_workers=1)` :325；初始化 state=disabled/counters/reprobe 允许位 :349-352。
    - `start()` :362 — 启动探测 + 启动周期 task（幂等：`_task is not None` 直接返回 status）。
    - `stop(drain_seconds=5.0)` :383 — 停周期 task（有界 drain）→ worker 内 close → 有界 executor shutdown → `_disable("stopped")`。
    - `_bounded_executor_shutdown` :407 — cancel pending + 守护线程 bounded wait。
    - `query(sql, params)` :429 — 唯一合法查询通道：status 门 → submit `_run_query` → 错误分类（busy 原样上抛 :453，其余禁用+`AuxiliaryUnavailableError` :459-460）→ `_check_breaker_state` :462。
    - `_run_query` :465 — worker 内：BEGIN :473 →（测试钩子 `_in_txn_pause` :474）→ execute/fetchall :475-476 → COMMIT :477；finally 强制 ROLLBACK（`conn.in_transaction` 时）:482-486 + 游标 close :487 + `breaker.record` :489。
    - `_submit` :495 — `loop.run_in_executor(self._executor, ...)`。
    - `_open_conn()` :499 — **:506 `file:{quote(path, safe='/')}?mode=ro` URI + :507 `sqlite3.connect(uri, uri=True)`（check_same_thread 默认 True 保持）+ :509 `PRAGMA query_only=ON` + :510 `PRAGMA busy_timeout`**；:508 记 `_worker_thread_id`。
    - `_close_conn` :513 — worker 内置 None + close（吞 sqlite3.Error）。
    - `_open_and_gate_sync` :521 — 关旧 → 开新 → schema 门；门异常时 finally 关局部 conn（MAJOR-1 所有权纪律 :538-543）；成功才转移 `self._conn`、`_generation += 1` :544-545、stat 记录 `_inode` :546-547。
    - `_open_and_gate(why)` :549 — async 封装；失败 → `_disable("gate_failed"|"open_failed")` + `_reason_detail=str(exc)` + warning :552-558；成功 → available + `breaker.reset()` + `_next_probe_at=None` :560-564。
    - `swap()` :566 — 提交 `_open_and_gate` 到同一 worker FIFO；成功 `swaps += 1`；DisabledResolution 直接返回当前 generation :573-574。
    - `_periodic(stop)` :584 — `wait_for(stop.wait(), interval)` 循环调 `tick`；单次异常 warning 不退出 :594-595。
    - `tick()` :597 — ①available 时 inode/mtime 对比 :602-609（变化 → swap + return）②circuit_open 时到点 `probe()` :611-614 ③disabled 且允许时 `reprobe()` :616-617。
    - `probe()` :619 — 半开探针（probes+1，`_next_probe_at` 先置 :622）；失败保持 open（info log）:626-628；成功 `note_probe` → 恢复 available :630-634。
    - `_probe_sync` :636 — worker 内 BEGIN/SELECT 1/COMMIT + finally ROLLBACK。
    - `reprobe()` :652 — 禁用重探（`_reprobe_allowed` False 提前返回）；成功 info log。
    - `_disable(reason)` :667 — 置 disabled；"stopped" 不计 disables 计数 :669-670。
    - `trip_breaker()` :675 — 状态联动到 circuit_open（非 circuit_open 时 trips+1）+ 探针排期。
    - `_check_breaker_state` :683 — breaker.open 且 state==available → trip_breaker + warning。
    - `status()` :691 — 先 `_check_breaker_state` 再产 `DbAuxStatus`。
    - `snapshot()` :707 — {available/mode/reason/generation/breaker/counters/source/**path**}。
    - `_log_startup` :729 — 启动 log：disabled 打 reason+detail；resolved 打 path/source/warning/gate 结果（gate fail 串含 `_reason_detail` :737-742）。
    - `connection` property :756 — 测试专用（断言 worker 外使用 → ProgrammingError）。
    - `generation` property :762 / `breaker` property :766。

- **依赖**：`sqlite3`、`asyncio`、`threading`、`concurrent.futures.ThreadPoolExecutor`、`urllib.parse.quote`、`..logging_config.get_logger` :40、`.path_resolution`（`DisabledResolution`/`ResolvedPath`/`stat_inode_marker`）:41-45。
- **被依赖**（rg 反查）：`src/oc_slimapi/app.py:22,598-603`（创建+start；:605-615 stop 回调 drain `_DBAUX_DRAIN_TIMEOUT`）；`src/oc_slimapi/routes/sessions.py`（经 `fetch_sessions_page` 间接 + `dbaux.status().available` :431）；`src/oc_slimapi/routes/read_groups.py:527`（直调 `dbaux.query(_SESSION_SINGLE_SQL, (sid,))`）；`src/oc_slimapi/routes/health.py:83`（`status().auxiliary_view()`）；`src/oc_slimapi/routes/metrics.py:51-69`（`snapshot()`，显式丢弃 path :48-50）；测试 `tests/test_dbaux_lifecycle.py`、`test_dbaux_metrics.py`、`test_equivalence_anchor.py:31` 等。

- **状态/可变性**：
  - `_executor` :325 — 单 worker 线程池，构造后不变；stop 后 shutdown（实例不可复用，:404-405 注释「重启用新实例」但无 `_closed` 防御标志）。
  - `_conn` :330 — 仅 worker 内读写（:328-329 注释）；`connection` property 是测试后门。
  - `_state` ∈ {disabled, available, circuit_open} :333；`_reason`/`_reason_detail` :334-335。
  - `_generation` :331 — 仅 `_open_and_gate_sync` 成功路径 +1 :545。
  - `_inode` :332 — `(st_ino, st_mtime_ns)` 或 None :546-547。
  - `_counters` {queries,probes,trips,swaps,disables} :341-347。
  - `_reprobe_allowed` :349-352 — explicit-memory/upstream-memory 永久禁重探。
  - `_task`/`_stop_event`/`_next_probe_at` :353-355；`_worker_thread_id` :356（只写不读，见疑问 12）。
  - breaker 跨线程：`record` 在 worker（:489），`note_probe`/`reset`/`snapshot` 在事件循环——无锁（疑问 7）。
  - 状态转移表（实现归纳）：start → available | disabled(open_failed|gate_failed|explicit-memory|upstream-memory)；available --查询错(schema/io/cantinit/programming)--> disabled(query_*)；available --P99≥20ms--> circuit_open（worker record trip → loop `_check_breaker_state`）；circuit_open --探针 P99<10ms--> available，失败停留；disabled(非 memory) --reprobe 成功--> available；available --inode/mtime 变--> swap（available+gen+1 或 disabled）；任意 --stop--> disabled(stopped)。busy 不禁用不熔断态（仅计样本）。

- **错误路径**：
  - `AuxiliaryUnavailableError` 构造点：:442（status 不可用拒绝，reason=state reason 或 "disabled"）、:460（`f"query_{kind}"`）。B4 消费映射 503 `auxiliary_unavailable`：sessions.py:299-306/443-450、read_groups.py:88,513,533,541,546。
  - busy 原样 `raise` :453（sqlite3.Error 上抛，sessions.py:445-450 统一转 503）。
  - 异常吞噬点：:392-397（stop drain best-effort，`Exception`/`BaseException`）；:400-401（close 失败 warning）；:485-486 与 :648-649（ROLLBACK 失败吞 sqlite3.Error——finally 兜底，刻意）；:518-519、:540-542（close 吞）；:552-558（open/gate 失败 catch-all → 禁用 + warning，**不重抛**）；:594-595（tick 失败 warning，周期任务存活）；:626-628（探针失败 info log 保持熔断）。
  - reason 泄面：`_reason_detail` :555 = str(exc)（可能含列名/路径片段）——只进 log（:557-558、:740），不进 wire；wire 侧 health/metrics reason 为粗粒度标签 ✓。

- **疑问点（16）**：
  1. **只读双保险验证：两道防线都在** ✓。第一道 :506-507（`file:...?mode=ro` URI + `sqlite3.connect(uri, uri=True)`）；第二道 :509 `PRAGMA query_only=ON`。`_open_conn` 是全仓唯一 `sqlite3.connect` 点（rg 验证），swap/reprobe/probe 全走它 → 每条连接都带双保险；`check_same_thread` 未显式传参 = 默认 True 保持 ✓。`immutable=1` 确无出现（仅 docstring 否决记录 :11,:20,:502-503）。
  2. **file: URI 转义**：:506 `quote(path, safe='/')` 把 `?`→%3F、`#`→%23、空格→%20、`%`→%25 等（`?`/`#` 不在 unreserved 也不在 safe）——SQLite URI 解码还原为文件名字符，不会截断 query 段 ✓。但 **explicit-env 相对路径未拒绝**：path_resolution.py:96-100 对 `OC_SLIMAPI_OPENCODE_DB` 非 memory 值直接 normpath 使用（不检查 isabs，不像 upstream-env 相对路径有挂数据目录语义 :116-119）→ 相对路径产出 `file:相对路径?mode=ro`，相对进程 cwd 打开且不报错——语义歧义/错库风险（对比 upstream 分支的显式处理）。
  3. **stop() 的 close 提交无超时**：:399 `await self._submit(self._close_conn)` 无界等待——若 worker 卡在长查询（busy_timeout 5s + fetchall）或磁盘 hang，stop 永不返回；:402 的有界 shutdown 排在其后才执行。周期任务的 drain 有 timeout :391，close 这步没有。「超时也返回」的保证（:409 docstring）只覆盖 `_bounded_executor_shutdown` 阶段。
  4. **shutdown 竞态可产生 500**：query :440-441 status 检查通过后 submit；若并发 `stop` 的 `_close_conn` 先入 FIFO（stop 先提交），`_run_query` :467 `assert self._conn is not None` 失败 → AssertionError 不属于 `sqlite3.Error`，:448 不捕 → 逃逸到 sessions.py:443-450（只捕 AuxiliaryUnavailableError/sqlite3.Error）→ FastAPI 500。窗口极窄；且 Python `-O` 下 assert 剥除 → `None.cursor()` AttributeError 同样逃逸。
  5. **not_found/path_ambiguous 的 reprobe 必然 AttributeError**：`DisabledResolution` 无 `path` 属性；:616-617 reprobe → `_open_and_gate` → worker `_open_conn` :505 `self._resolution.path` 抛 AttributeError → :552 捕获 → reason 从 not_found/path_ambiguous **漂移为 open_failed**，且每 30s 重复 warning 一次；`resolve_db_path` 仅 app.py:598 启动调用一次、无候选重新发现 → 这两类禁用实际不可能经重探自愈（与 :615「冷启动竞态自愈」注释的适用范围不符——自愈只对「路径存在但暂不可开」有效）。测试只覆盖 explicit-memory 的 reprobe False（tests/test_dbaux_lifecycle.py:268-281）。
  6. **inode 探测竞态/盲区**：(a) `_open_and_gate_sync` :531 open 与 :546 stat 之间换库 → 记录的是新文件 marker 而连接持旧库 → 下个 tick 再 swap 一次（自愈，良性）。(b) 若 :546 stat 失败（open 成功后文件即被删）→ `self._inode = None`，而 tick :604 要求 `marker is not None and self._inode is not None` 双非空 → 该连接存续期内 swap 检测静默失效。(c) tick 的 stat 在事件循环线程同步执行 :603（非 worker）——stat 快，可接受，但与「连接操作全在 worker」叙事不一致（stat 不触连接，纪律上允许）。
  7. **LatencyBreaker 跨线程无锁**：`record` 仅 worker 调用（:489），`note_probe`/`reset`/`snapshot`（含 `_prune` 重绑 `_samples`）在事件循环——并发窗口内 `_prune` 重绑可丢 worker 刚 append 的样本、`_total += 1` 与 `reset()` 竞态。GIL 下无崩溃，但 metrics 的 p50/p99/samples 可有瞬时失真。类未声明线程安全契约。
  8. **mtime 纳入 swap 判据的运维后果**：marker = `(st_ino, st_mtime_ns)`（path_resolution.py:145），tick :604 tuple 不等即 swap——**主 .db 文件 mtime 因上游正常 checkpoint 也会变化** → 活跃上游下可能每 30s 周期 swap 一次：generation 递增、`breaker.reset()` :563（warmup 重起算、样本清空）→ **P99 熔断护栏在写频繁期被反复清零，形同虚设**。实现与设计冻结一致（design-v4-dbaux.md:222「对比 st_ino/st_mtime_ns；变化 → swap」，:225 只豁免 -wal/-shm 的 inode），但设计动机是「替换文件」场景，mtime 维度把正常写也拉进 swap 面。测试只覆盖 rename 换 inode（tests/test_dbaux_lifecycle.py:400,406），mtime-only 变化路径未测。
  9. **单探针即可恢复熔断**：`note_probe` :225-229 对窗口内仅 1 个探针样本即算 P99 → 一次 <10ms 探针即闭合；hysteresis（20/10ms 双阈值）只作用于延迟分布，不要求最小探针次数。恢复后被真实流量立刻再 trip → 30s 周期震荡可能。
  10. **classify 文本匹配脆弱**：:128-139 按英文错误文本子串分类（"unable to open database file"/"readonly"/"shm" → cantinit 在 "disk i/o" 之前；"database is locked" → busy）。Python sqlite3 不暴露 extended error code，文本匹配是现实选择，但依赖 SQLite 文案稳定性；`SQLITE_READONLY` 系（含 query_only 拦截写的 "attempt to write a readonly database"）也归 cantinit → 禁用+重探（对投影 SQL 而言合理，因为正常路径不应有写）。
  11. **schema 门 MAJOR-1 验证** ✓：:530-543 门异常（含 PRAGMA/IO 错）时局部 conn 在 finally 关闭，绝不泄漏 fd；只有门全过才 `self._conn = conn` + generation+1 :544-545。fd 泄漏回归测试在 tests/test_dbaux_lifecycle.py:240-261。
  12. **`_worker_thread_id` :508 只写不读**（rg 全仓无消费者）——死代码或预留观测位。
  13. **lifecycle `__all__` :47-55 缺 `DbAuxStatus`**——`__init__.py:38` 显式 import 不受影响，但 `from .lifecycle import *` 拿不到；与 `__init__` 导出面漂移。
  14. **stop 后 start 不可复用未防御**：stop :388 置 `_task=None` → 再 start :368 幂等检查通过、重建周期 task，但 executor 已 shutdown（:410）→ 首次 `_submit` RuntimeError。:403-405 注释声明「重启用新实例」，代码无守卫。
  15. **snapshot() 含明文 `path`** :722-726——当前唯一消费者 metrics.py:53-69 显式丢弃（metrics.py:48-50 注释「resolved DB path is deliberately NOT echoed」）✓；但 snapshot 是公开方法，未来消费方易无意把 path 带上 wire（审计跟踪点）。health `auxiliary_view` :278-283 无 path ✓。
  16. **circuit_open 期间不做 inode 校验**：tick :611-614 该分支直接 return（①在 :609 也 return）——熔断期间换库，探针 SELECT 1 打在旧 fd 成功 → 恢复 available，下一个 tick 才检测 swap → 多一个周期读旧库（探针 `_probe_sync` :641 只验连接活性，不验 schema/表可达，恢复后首个真实查询才发现 schema 漂移）。

---

### src/oc_slimapi/dbaux/projection.py（362 行）

- **职责**：v4 sessions 投影 SQL 组装（全参数化）+ 行组装容忍（§8）+ `LIMIT ?+1` 同窗口 complete 判定 + 经 `DbAuxiliarySource.query` 的执行入口；search 规范化/LIKE 转义/通配判定的唯一实现源。

- **对外符号**：
  - `PROJECT_ALIASED_COLUMNS` :62 — `p.id AS p_id, p.name AS p_name, p.worktree AS p_worktree`。
  - `ROW_KEYS` :69 — 行→dict 键序（24 session 列 + 3 join 列，与 SELECT 列序严格一致）。
  - `JSON_COLUMNS` :75 — ("summary_diffs","revert","permission","metadata","model")——model 为 json 列（R5 BLOCKER-1 实证注释 :72-76）。
  - `ARCHIVED_STATES` :79 — ("omit","only","all")。
  - `PARENT_RESERVED_STATES` :82 — ("all","none","only")；**rg 全仓无消费者**（仅定义+re-export）。
  - `normalized_search(raw)` :90 — None 透传 / 非 str TypeError :100 / `strip()`。
  - `escape_like(value)` :104 — `\`→`\\`（最先）、`%`→`\%`、`_`→`\_`。
  - `has_wildcard(normalized)` :113 — 含 `%`/`_`/`\` 任一 → True（DB 不可用时降级拒绝依据）。
  - `SessionsQuery` :127 — frozen (sql, params)。
  - `build_sessions_query(...)` :135 — 组装四谓词 + keyset 下推 + `LIMIT ? + 1`；archived/parent/limit/allowlist 域校验 :164-169, :204-208, :219-225。
  - `rows_to_records(rows)` :248 — zip ROW_KEYS → dict；缺 id 跳行 :260-262；JSON 列 orjson 解析失败或 model 非对象形状跳行 + warning（带 sid）:263-292。
  - `SessionsPage` :297 — frozen (records, complete, anchor)。
  - `_window_anchor(rows, limit)` :315 — 倒序扫描**原始行**前 limit 行，取最后一个 (int time_updated, 非 str 空 id) 锚点。
  - `fetch_sessions_page(source, ...)` :332 — build + `source.query` + complete/anchor 组装。

- **依赖**：`orjson`、`..logging_config`、`.cursor`（`allowlist_rev`/`search_hash` re-export :52）、`.lifecycle`（`DbAuxiliarySource`/两列清单）。
- **被依赖**：`src/oc_slimapi/routes/sessions.py:11-22`（fetch_sessions_page/has_wildcard/normalized_search 等）；`src/oc_slimapi/routes/read_groups.py:53-57`（`rows_to_records` + 两个列清单——自拼 `_SESSION_SINGLE_SQL` :425-437）；`src/oc_slimapi/skeleton.py:819-831`（消费 rows_to_records 记录契约）；测试 test_sql_semantics / test_eqp_matrix:15 / test_equivalence_anchor:31,217 等。

- **状态/可变性**：全模块纯函数 + frozen dataclass，无连接/锁/可变全局；`_LOGGER` 模块级。SQL 文本每次现拼（无缓存）——EQP 形状稳定性靠谓词轴恒在（:150-152 search `? IS NULL` 恒真形）。

- **错误路径**：`ValueError`（组装域校验 :165-169, :205-208, :220-225——fail-closed，属调用方错误）；`TypeError`（normalized_search :100）；跳行不抛（warning :261, :287-291）；`AuxiliaryUnavailableError`/sqlite3.Error 从 `source.query` 透传 :356（docstring :344-346 声明 B4 统一映射 503）。

- **疑问点（10）**：
  1. **SQL 参数化验证** ✓：用户值全部 `?` 绑定——parent sid :186-187、search pattern :195-197（同一 pattern 绑两 ?）、allowlist item/prefix_len/prefix :214、cursor 锚点三绑 :227、limit :241；拼接进 SQL 文本的只有冻结谓词片段与列名常量。无字符串拼接注入面。
  2. **LIKE 转义封闭性**：`escape_like` :110 替换顺序正确（`\` 最先防二次转义）；SQL 侧 `ESCAPE '\\'`（Python 源 `"\\'"` → SQL 字面 `'\'`）:192,:196；转义后 `%`/`_` 失去通配 → 字面子串语义 ✓。SQLite `LIKE` 默认 ASCII 大小写不敏感（与上游 `like()` 等价性锚点 :16-17）——`case_sensitive_like` 是 per-connection 非持久 PRAGMA，sidecar 自有连接恒默认 ✓。BINARY 语义的 allowlist `=`/`substr` 不受影响。
  3. **search 轴恒真形的第一参数绑 pattern 本身** :196-197（非标志位）——None 时 (None,None)：`? IS NULL` 短路 ✓；正确但可读性差，且依赖 SQLite 对 `? IS NULL` 的短路求值（不短路也只是冗余 LIKE NULL 比较，语义仍安全）。
  4. **allowlist 谓词与指纹的 strip 不一致**：SQL 分支用**原样** item（:204 只拒非 str/空串，不 strip），而 cursor.py `allowlist_rev` :109 对项 strip 后去重——配置含 `"/a "`（尾空白）时：SQL 绑 `"/a "`、`"/a /"`（匹配不到行），指纹却按 `"/a"` 计算 = 与干净配置 `"/a"` 同指纹。后果：运维把 `"/a "` 修成 `"/a"` 时指纹不变而行为变（翻页跨配置变更的漏检面）；B4 入口 `_v4_allowlist_entries`（sessions.py:357-368）也只滤空串不 strip。低概率配置错误场景，但规范化双轨值得统一。
  5. **complete 判定与 anchor 的窗口纪律** ✓：`complete = len(rows) <= limit` :357（原始行集口径，容忍跳行不放大 complete）；`records = rows_to_records(rows[:limit])` :359；`_window_anchor` 只扫 `rows[:limit]` :323（不含第 limit+1 行 → 下一页重见它，无跳行）。窗口满且全为坏行 → records=[] 但 anchor 非 None → complete=false + nextCursor 仍发（BLOCKER-3，分页不死锁）✓。
  6. **全窗无可锚行 + incomplete 的矛盾态**：若窗口内所有行 `time_updated` 非 int（REAL/NULL）→ `_window_anchor` :326-328 返回 None → sessions.py:469 `not complete and anchor is not None` 不成立 → 无 nextCursor 但 complete:false——客户端视角「还有更多但拿不到游标」。上游 schema time_updated INTEGER + schema 门只查列名不查类型/NOT NULL（lifecycle :97-107）→ 理论漂移面。
  7. **NULL time_updated 与 keyset 谓词不兼容**（理论）：谓词 `s.time_updated < ? OR (= AND id < ?)` :226 对 NULL 行恒假 + `ORDER BY ... DESC` 中 NULL 排最后 → NULL-t 行只可能出现在首页窗口内，cursor 翻页永远到不了（同上，依赖上游 NOT NULL 假定，门不校验）。
  8. **ROW_KEYS 与 read_groups 的 SELECT 复制**：read_groups.py:425-437 手写 `_SESSION_SINGLE_SQL` 复制同一列形（注释自认「与 build_sessions_query 同一 SELECT 形状」）——列序双份维护，projection 增列时 read_groups 不同步则 zip 错位（rows_to_records zip :258 静默截断/错位——SELECT 显式列使行宽=键数，错位只会在「两处列序漂移」时发生）。
  9. **model 形状门** :278-284：合法 JSON 非 dict（'[]'/'"s"'/'123'）跳行 ✓，JSON null → None 允许（契约 object|null）；其余 JSON 列允许多形不加门 :277-283 注释明确。orjson 解析失败统一跳行不 500 ✓。
  10. **limit 上界缺位**：:168 只校验 `limit >= 1`（bool 显式拒）；上界 1..500 属 B4（sessions.py:389-393 `_V4_LIMIT_MAX` → 422）。直接调用方（测试/未来泳道）绕过 B4 时无上界——`LIMIT ?+1` 大值 + fetchall 全量物化内存。内部 API 风险低，但 `build_sessions_query` 的「域校验属路由层」分工（:157-158）意味着本函数不能独立安全使用。

---

### src/oc_slimapi/dbaux/cursor.py（225 行）

- **职责**：v4 sessions keyset 翻页 cursor 的编解码 + 过滤上下文指纹（§4.5）。纯函数：`base64url(JSON {t,i,f})` 无 padding；`search_hash`/`allowlist_rev` 的 canonical 实现（projection re-export 防第二实现漂移）。

- **对外符号**：
  - `ARCHIVED_DEFAULT` :43 / `PARENT_DEFAULT` :44 — "omit"/"all"。
  - `_HASH_HEX_LEN = 16` :47 / `_EMPTY_SENTINEL = ""` :49。
  - `_B64URL_RE` :53 / `_CURSOR_KEYS` :54 / `_FINGERPRINT_KEYS` :55。
  - `InvalidCursorError(ValueError)` :58 — `reason` 粗粒度标签（charset/decode/json/shape/type/empty_anchor）进日志不进 wire :62-63。
  - `CursorFingerprint(TypedDict)` :70 — {archived, parent, search_hash, allowlist_rev}。
  - `CursorPayload` :79 — frozen (t: int, i: str, f)。
  - `search_hash(normalized_search)` :88 — None → `""` 哨兵 :95-96；否则 sha256 utf-8 截 16 hex :97。
  - `allowlist_rev(entries)` :100 — 逐项 strip 去空项 → set → sorted → canonical JSON → sha256 截 16 hex；空 → `""`。
  - `normalize_archived` :116 / `normalize_parent` :121 — None/"" → 默认值。
  - `build_fingerprint(...)` :126 — 归一化集中地：search 在此 trim :139 后 hash。
  - `fingerprint_mismatch(payload_f, current_f)` :148 — 非 Mapping 或 dict 不等 → True。
  - `encode_cursor(t, i, fingerprint)` :158 — JSON（键序 t,i,f 字面量序 + compact + ensure_ascii）→ base64url 无 padding。
  - `decode_cursor(raw)` :165 — 语法校验链（见疑问 1-4）。

- **依赖**：`hashlib`/`json`/`re`/`base64`/`binascii`——无仓库内依赖、无 IO。
- **被依赖**：`src/oc_slimapi/dbaux/projection.py:52`（re-export search_hash/allowlist_rev）；`src/oc_slimapi/routes/sessions.py:18-22`（decode/build_fingerprint/fingerprint_mismatch/encode_cursor）；测试 test_cursor_matrix.py 等。

- **状态/可变性**：全纯函数；常量全 frozen/compiled regex。

- **错误路径**：`InvalidCursorError` 构造点 :184(charset), :188(decode), :192(json), :194/:197(shape), :200/:202(type), :207(empty_anchor)——全部 `raise ... from None` 清链。B4 映射 400 `invalid_cursor`：sessions.py:413-427（两处：语法 + 指纹不匹配），且 §8.3 优先于 503（sessions.py:409 在 :430 dbaux 状态检查之前）✓。无吞异常点。

- **疑问点（9）**：
  1. **字符集预检必要且已做** ✓：:53 `_B64URL_RE = [A-Za-z0-9_-]+` + :183 `fullmatch`——`urlsafe_b64decode` validate=False 会静默丢非字母表字符（:52 注释自认），预检挡住 `+`/`/`/`=` 及任意垃圾 → charset。padding `=` 被拒（charset 域）✓。
  2. **补齐与长度校验**：:186 `raw + "=" * (-len(raw) % 4)`——len%4==1 时 binascii.Error → "decode" :187-188 ✓；其余长度恒可解。
  3. **结构与类型校验封闭**：顶层 dict + 键集严格 `== {t,i,f}` :193；f 子键集严格四键 :196；t int 且显式拒 bool :199（JSON true → Python True 是 int 子类）；i 与 f 值全 str :201；i 空串拒 :203-207（BLOCKER-2，DB 可用时曾逃逸为 500 的路径已前置到 400）。**t 无值域**（负数/超大 int 收）——谓词参数化无注入面，伪造锚点=客户端自由翻页 ✓ 非漏洞。
  4. **无长度上限（DoS 面）**：docstring :177-179 明示「超长但合法的 cursor 正常解码」（不过度防御）——decode 在事件循环内 b64decode + json.loads；sessions.py:414 前也无长度截断 → 超大 `?cursor=` 参数可造成 CPU/内存压力。部署形态（stunnel mTLS + ocdroid 唯一客户端）缓解，但 sidecar 监听 loopback + stunnel，恶意面取决于 stunnel 配置——审计记录。
  5. **encode 确定性** ✓：:160-162 dict 字面量序（t,i,f）+ `separators=(",", ":")` + ensure_ascii + rstrip("=") → 同输入逐字节相同；i 含非 ASCII/控制字符时 ensure_ascii 转义仍确定。
  6. **哨兵与缺席/空串不等价** ✓：`search_hash(None)` → `""`，`search_hash("")` → sha256("")[:16] ≠ ""（hex 摘要不可能为空串）:95-97；`build_fingerprint` :139 对 raw 先 trim——`?search=`（显式空串）trim 后 "" 走 hash，与缺席（None）区分 ✓。
  7. **allowlist_rev 的定界注入免疫** ✓：:112 canonical JSON（separators 紧凑 + 引号定界）——项含 `\n`/`,` 不会跨项碰撞；sorted + set 确定性 ✓。但与 SQL 谓词侧的 strip 不一致问题见 projection 疑问 4（指纹 strip、谓词不 strip）。
  8. **tie-break 封闭性**：排序 `(time_updated DESC, id DESC)`（projection :238）+ 下推谓词 `(t < ? OR (t = ? AND id < ?))`（projection :226，OR 展开避免依赖 SQLite ≥3.15 行值）——id 唯一（上游主键假定）时严格全序，同快照内翻页无重无漏；**id 重复或 time_updated NULL/非整数时封闭性破口**（见 projection 疑问 6/7）；跨快照并发更新契约明示不承诺零重复零遗漏 :7-8。fingerprint_mismatch :153-155 非 Mapping fail-closed → 400 ✓。
  9. **normalize_* 双实现**：normalize_archived/parent :116-123 与 sessions.py:404-405 `archived or "omit"`/`parent or "all"` 语义相同但两处实现——路由侧 `or` 把任意 falsy（只有 ""）折默认，与 cursor.py 的 None/"" 判定一致 ✓，当前无漂移；`build_fingerprint` :139 的 trim 与 `normalized_search`（projection :90-101）同语义第三处实现（后者多 TypeError 防护）——三处 strip 逻辑建议收敛。

---

### src/oc_slimapi/dbaux/path_resolution.py（158 行）

- **职责**：DB 路径解析（design §3）：explicit env → OPENCODE_DB → 候选发现，fail-closed；`stat_inode_marker` 供 lifecycle §4.1。纯函数（glob/stat 属读取性探测）。

- **对外符号**：
  - `ENV_EXPLICIT_DB` :28 / `ENV_UPSTREAM_DB` :30 / `ENV_XDG_DATA_HOME` :32 — 三个 env 名常量。
  - `_MEMORY` :34 / `_CANDIDATE_GLOB = "opencode*.db"` :35。
  - `SINGLE_CANDIDATE_WARNING` :38 — 中文 warning 常量（测试断言锚点）。
  - `ResolvedPath` :41 — frozen (path, source, warning=None)；source ∈ explicit-env|upstream-env|upstream-env-relative|candidate-discovery。
  - `DisabledResolution` :51 — frozen (reason, detail)；reason ∈ explicit-memory|upstream-memory|path_ambiguous|not_found。
  - `_clean` :62 — strip。
  - `_expanduser` :67 — 注入 home 时仅替换首个 `~`；否则 `os.path.expanduser`。
  - `_data_dir` :75 — XDG_DATA_HOME 或 ~/.local/share + "/opencode"。
  - `resolve_db_path(env, home)` :82 — 三级解析（§3.3 伪代码落地）。
  - `stat_inode_marker(path)` :136 — `(st_ino, st_mtime_ns)`；OSError → None。

- **依赖**：`glob`/`os`/`pathlib.Path`（仅 re-export）/`dataclasses`。无仓库内依赖。
- **被依赖**：`lifecycle.py:41-45`（三符号）；`app.py:598`（启动唯一调用点）；测试 test_db_path_resolution.py。

- **状态/可变性**：纯函数；无状态。

- **错误路径**：本模块不抛（fail-closed 都折成 DisabledResolution）；`stat_inode_marker` stat 失败 → None（不抛）:144。detail 字段（:132 候选列表、:133 data_dir）含本机路径——消费方 lifecycle `_log_startup` :732-734 打进日志（log 允许），DisabledResolution 的 reason 字符串本身不含路径 → wire health reason 无泄面 ✓。

- **疑问点（8）**：
  1. **explicit-env 相对路径不拒绝**（同 lifecycle 疑问 2 的根因）：:96-100 对 `OC_SLIMAPI_OPENCODE_DB` 非 memory 值直接 `_expanduser + normpath`——相对路径原样通过（upstream-env 相对有挂数据目录 :116-119，explicit 没有）→ lifecycle :506 生成相对 `file:` URI 相对 cwd 打开。建议 isabs 校验或折 not_found。
  2. **`_expanduser` 注入语义分歧**：:68-69 home 非 None 且 p 以 `~` 开头 → `p.replace("~", home, 1)`——`"~bob/x.db"` → `home + "bob/x.db"`（缺分隔符，错误路径）；生产路径（home=None）走 `os.path.expanduser("~bob")` = 用户目录查找。测试注入与生产语义不一致，可能掩盖行为差异。
  3. **XDG_DATA_HOME 相对值未校验**：:77-79 非空即用作 base——XDG 规范要求绝对路径；相对值 → data_dir 相对 → glob 相对 cwd。低危（自配 env），但与「不猜测」精神不符。
  4. **候选 glob 不滤目录**：:124 `glob.glob(...opencode*.db)`——名为 `opencode-x.db` 的**目录**也计入候选；恰一个目录候选 → ResolvedPath → 打开失败 → lifecycle open_failed 禁用 + 无效 30s 重探循环（重探还叠加 lifecycle 疑问 5 的 AttributeError 路径？否——此处 resolution 是 ResolvedPath 有 path，重探会反复尝试 open 该目录失败 → open_failed，行为正确但永不自愈）。symlink 候选被收入（open 跟随）——运维面可接受。
  5. **`glob.escape(data_dir)` 已做** ✓ :124——data_dir 含 `[]*?` 时不会被解释为模式；`_CANDIDATE_GLOB` 的 `*` 不跨 `/` ✓；`sorted()` 确定性 ✓。
  6. **mtime_ns 纳入 marker 的下游后果**：:145 `(st_ino, st_mtime_ns)`——inode 复用（回收）兜底 + 换库检测，但 mtime 维度把上游正常 checkpoint 也触发 swap（详见 lifecycle 疑问 8；设计冻结 design-v4-dbaux.md:222）。若意图只是「inode 复用兜底」，应在 ino 相同才比 mtime；当前 tuple 不等即换。
  7. **`__all__` 导出 `Path`** :157——pathlib re-export，无消费者，误导性导出面（读代码者会以为本模块处理 Path 对象，实际全 str）。
  8. **`OPENCODE_DISABLE_CHANNEL_DB` 注释声明**：:121-123 声称该上游开关情形「由候选枚举自然覆盖」（按盘上文件事实判定）——正确性依赖上游建库名始终匹配 `opencode*.db`；上游改名（如带 channel 后缀变更）时 not_found 静默降级 HTTP 而无告警（只有启动 log）——版本升级对齐时的漂移观察点。

---

### src/oc_slimapi/dbaux/__init__.py（105 行）

- **职责**：dbaux 包门面——纯 re-export 汇总四个子模块的公开面（docstring :1-20 概述各泳道 + 双保险重申）；无任何逻辑。

- **对外符号**：`from .cursor import ...` :21-35（13 个）；`from .lifecycle import ...` :36-45（8 个，含 `DbAuxStatus` :38）；`from .path_resolution import ...` :46-51（4 个）；`from .projection import ...` :52-65（13 个）；`__all__` :67-105（38 项，字母序）。

- **依赖 / 被依赖**：依赖 = 四子模块；被依赖 = `app.py:22`、`routes/sessions.py:11-22`、`routes/read_groups.py:53-57`、`routes/health.py`（经 status 视图间接）、测试多处（test_eqp_matrix.py:15、test_equivalence_anchor.py:31 等）。`path_resolution.ENV_*`/`SINGLE_CANDIDATE_WARNING` 未入 `__init__` 导出面（外部用 `oc_slimapi.dbaux.path_resolution` 全路径访问，如 config.py:650-655 注释所示）——一致性可接受。

- **状态/可变性**：无状态；import 即拉起 orjson/sqlite3 子系统（模块级副作用仅 logger 构造 projection.py:59）。

- **错误路径**：无（纯 re-export；子模块异常面向上透传）。

- **疑问点（3）**：
  1. **导出面与子模块 `__all__` 不同步**：`DbAuxStatus` 在 `__init__` 导出（:38,:73）但 lifecycle 自身 `__all__`（lifecycle.py:47-55）缺它——`from .lifecycle import *` 与 `from . import DbAuxStatus` 行为分叉；反向：projection 无 `__all__`（依赖 `__init__` 显式清单）——三种导出策略并存（cursor 有 `__all__`、lifecycle 有但不全、projection 没有），漂移温床。
  2. **`PARENT_RESERVED_STATES` 死导出**：:55,:80 re-export 但全仓无消费者（rg 验证）——或删或补文档锚点用途。
  3. **`stat_inode_marker`/`resolve_db_path` 从 `__init__` 导出（:50,:100-101）与 `lifecycle` 内直接 `from .path_resolution import`（lifecycle.py:41-45）并存**——两条取用路径（包门面 vs 子模块直连）无一致性检查；read_groups 走包门面、lifecycle 走子模块直连，重构（如改 marker 结构）需同时顾及两条 import 链，建议统一走包门面或全直连。

---

## 附：横向核对结论（审计关注项速览）

| 关注项 | 结论 |
|---|---|
| 只读双保险 | ✓ 两道都在且唯一 connect 点覆盖所有路径（lifecycle.py:506-509；swap/reprobe/probe 均复用 `_open_conn`） |
| file: URI 转义 | ✓ `quote(path, safe='/')` 处置 `?`/`#`/空格/`%`；缺口 = explicit-env **相对路径**未拒（path_resolution.py:96-100 → 相对 URI 按 cwd 打开） |
| 单线程 executor 纪律 | ✓ 建连/查询/rollback/重开/关闭全在 max_workers=1 worker（lifecycle.py:325,:495-497）；check_same_thread 默认 True；缺口 = stop 的 close await 无超时（:399）+ stop/query FIFO 竞态可 500（:467 assert） |
| 断路器 | 滑窗 60s/≥10 样本/warmup 10 次/P99≥20ms trip、半开 30s、P99<10ms 恢复均实现；弱点 = 单探针即可闭合（:225-229）、跨线程无锁（:489 vs :630）、**swap reset 被上游正常写（mtime 变化）反复清零**（:145 marker 含 mtime_ns + :604 tuple 比较） |
| inode 探测竞态 | open→stat 间换库自愈（良性）；stat 失败 → `_inode=None` 后 swap 检测静默失效（:546-547,:604）；stat 在事件循环线程执行（:603） |
| SQL 参数化 / LIKE 转义 | ✓ 全参数化；escape_like 顺序正确 + ESCAPE '\'；缺口 = allowlist 谓词不 strip 而指纹 strip（projection.py:204-214 vs cursor.py:109） |
| cursor 编解码 | 校验链封闭（charset/decode/json/shape/type/empty_anchor）；tie-break 依赖 id 唯一 + time_updated int NOT NULL（schema 门不校验类型）；无长度上限 |
| DB 路径泄入 | wire 无泄（health auxiliary_view 粗粒度、metrics 显式弃 path）；log 有（启动 log :744 打 path 属允许域）；`snapshot()["path"]` 是未来泄面 |
