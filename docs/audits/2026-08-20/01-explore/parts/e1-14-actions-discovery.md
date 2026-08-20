# E1-14 精读卡片：actions / discovery / routes-actions

> 审计日期 2026-08-20。三个文件全文精读（非抽样），引用格式 `路径:行号`。
> 反查工具：rg（importer / 配置 / 契约文档 / 测试）。只读审计，未改动任何仓库文件。

---

### src/oc_slimapi/actions.py（975 行）

- **职责**：`/slimapi/actions` 的核心——配置驱动（TOML manifest）的通用管理动作框架：manifest 加载与 fail-closed 校验、action 目录（registry）、admission（全局 Semaphore + 单飞 + min_interval 节流）、子进程执行与统一清理（killpg/reap/审计）。模块头（:1-24）自述安全姿态：**risk-accepted** 明文面，缓解措施（默认空 manifest、并发帽、单飞+节流、owner-only-write、不可关闭的结构化审计、`shell=False`、argv 插值扫描）"是缓解不是授权"。

- **对外符号**（名字+行号+职责）：

  常量区（:43-100，注释声明 "wire-invariant; not env knobs"）：
  - `_NAME_RE` :49 — `^[a-zA-Z0-9_][a-zA-Z0-9_-]*\Z`（用 `\Z` 而非 `$`，防尾部 `\n` 混入名字门，:47-48 注释）。
  - `_MAX_NAME_LEN=64` :50；`_DEFAULT_TIMEOUT_S=30.0` :52；`_TIMEOUT_S_MIN/MAX=1.0/600.0` :53；`_DEFAULT_MIN_INTERVAL_EXEC=30.0` / `_DEFAULT_MIN_INTERVAL_QUERY=0.0` :54-55；`_DEFAULT_MAX_OUTPUT_BYTES=64KiB` :56；`_MAX_OUTPUT_BYTES_CAP=1MiB` :57；`_DESCRIPTION_MAX_LEN=256` :58。
  - `_READ_CHUNK=4096` :60；`_STDERR_LOG_CAP=64KiB`（**字节**帽，:61，:846-851 注释解释为何 chunk 计数帽会漏到 ~256MiB）；`_DRAIN_DEADLINE_S=5.0` :62（Bug C，rev-13）；`_CLEANUP_REAP_S=5.0` :63；`_ADMISSION_TIMEOUT_S=2.0` :64（Semaphore 获取预算 → ActionBusy）；`_EXEC_KINDS={"exec","query"}` :65；`_ALLOWED_FIELDS` :66-69；`_INTERPOLATION_MARKERS=("${","%(","$(")` :73（regression guard，非授权，:70-72）。
  - `_AUDIT_LOGGER`/`_APP_LOGGER` :75-76。
  - `_ACTION_ENV_ALLOWLIST` :86-89 — P2-2 子进程环境**白名单**（PATH/HOME/LANG/LC_ALL/LC_CTYPE/TMPDIR/XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS），fail-closed：`OC_SLIMAPI_*`（upstream URL、salt 等）绝不进动作环境（:78-85 rationale）。
  - `_build_action_env(source=None)` :92-100 — 从 `os.environ`（或给定 mapping）复制白名单键，返回新 dict。
  - `_ms(start)` :103-104 — monotonic 毫秒差。

  数据模型：
  - `ActionSpec` :112-124 — frozen dataclass：`name/kind/argv/description/timeout_s/min_interval_s/require_confirm(max_output_bytes)/cwd`；`require_confirm` exec-only、`max_output_bytes` query-only 由校验保证（:122-123 注释）。
  - `ActionResult` :127-138 — 调用结果（timeout/spawn 失败走异常不返回此对象，:129-130）。
  - `_DrainState` :141-153 — stdout drain 的共享累积器（`kept: bytearray` + `truncated: bool`）；rev-14：drain task 被 deadline 强杀后局部变量随 task 销毁，holder 保住部分输出（:143-150）。

  异常族（全部 `ActionError` 子类，routes 层经 `to_coded()` 映射）：
  - `ActionError` :162-185 — 基类；`status_code/code/headers` ClassVar；实例属性 `retry_after`/`timeout_s` 分别喂 `Retry-After` 头（:176-178）与 body `timeout_s` 字段（:179-182）；`to_coded()` :174-185 产出 `CodedHTTPException`。
  - `ActionsDisabled` :188-192 — 503 `actions_disabled`。
  - `ActionNotFound` :195-203 — 404 `action_not_found`。
  - `ActionConfirmRequired` :206-210 — 409 `action_confirm_required`。
  - `ActionThrottled` :213-221 — 429 `action_throttled`，构造参数 `retry_after`。
  - `ActionBusy` :224-229 — 503 `action_busy`，`retry_after=2`（类属性）。
  - `ActionTimeout` :232-240 — 504 `action_timeout`，构造参数 `timeout_s`。
  - `ActionUnavailable` :243-247 — 503 `action_unavailable`（OSError 全族 + spawn ValueError，见 :652-656）。

  Manifest 加载/校验：
  - `_ManifestError` :255-259 — 单条校验失败；永不逃出 `load_registry`。
  - `_load_manifest(path, logger)` :262-311 — **单次 `os.open`+`fstat`**（无 check-then-open TOCTOU，:267-269）：symlink 拒绝 :271-272 → 非 regular file 拒绝 :277-278 → 组/其他写位（`& 0o022`）拒绝 :280-283 → owner != euid 拒绝 :284-285 → `tomllib.load` :286-288 → 根必须只含 `actions` 表 :293-300 → `actions` 必须是表 :301-303；逐 action `_validate_action`，单条失败 WARNING + 仅丢该条（:306-310）。
  - `_validate_action(name, raw)` :314-410 — 非 table 拒 :315-316；未知字段拒 :317-319；名字正则+长度 :322-325；kind 枚举 :328-330；argv 非空字符串数组 :333-337、argv[0] 绝对路径 :338-339、插值 marker 扫描 :340-345、realpath 后 isfile :346-348 + `os.access X_OK` :349-350；description 长度+控制字符 :353-359；`timeout_s` ∈[1,600] :362-366；`min_interval_s >= 0`（**无上限**）:370-372；kind 互斥（exec 禁 `max_output_bytes` / query 禁 `require_confirm`）:375-378；query 的 `max_output_bytes` ∈(0, 1MiB] 且拒 bool :380-387；exec 的 `require_confirm` 必须 bool :389-394；`cwd` 字符串或缺失 :396-398。
  - `_as_number(raw, key, default)` :413-418 — int/float 皆可、bool 拒绝、转 float。
  - `load_registry(settings)` :421-458 — **best-effort、永不 raise**（:421-422）：`settings.actions_file` 未设 → disabled + INFO（:433-439）；`_ManifestError` → ERROR + disabled（:442-446）；`OSError/TOMLDecodeError` → ERROR + disabled（:447-452）；加载成功但 0 条有效 action → WARNING 空 catalog（:453-457）。镜像 app.py access-log 的 best-effort 模式（:427-429），broken manifest 永不炸 lifespan。

  `ActionRegistry` :466-975：
  - `__init__` :471-487 — `_actions` 拷贝入 dict；`_semaphore = asyncio.Semaphore(max_concurrent)` **仅 enabled 时创建**（:481，lazy 绑定 loop 的注释 :477-480）；`_in_flight: set[str]` :484；`_last_run: dict[str,float]` 内存态、重启即清（:485-487）。
  - `enabled` property :489-491。
  - `discover()` :493-503 — GET 目录：`[{name,kind,description,requireConfirm}]`，dict 保序（TOML 声明序），无排序无分页。
  - `invoke(name, confirmed)` :505-570 — **状态机入口**，顺序：disabled → `ActionsDisabled`（:509-510）；未知名 → `ActionNotFound`（:511-513）；**单飞标记在任何 await 之前完成 check-and-set**（:515-521，单线程 loop 下原子；冲突 → 审计 + `ActionThrottled(retry_after=2)` :517-520）；confirm 门（exec+require_confirm+未 confirm → 审计 + `ActionConfirmRequired`，:523-527）；min_interval 门（剩余 >0 → 审计 + `ActionThrottled(ceil(remaining))`，:528-535）；**服务级 admission**：`asyncio.wait_for(semaphore.acquire(), 2.0)`，超时 → 审计 + `ActionBusy`（:539-547），等位期间被 cancel → 审计 + re-raise（Bug E 修复，:548-556）；获信号量后**先写 `_last_run` 再执行**（:557-559，失败也计入节流窗），finally release（:560-561）；成功 → 审计 + 返回（:565-568）；最外层 finally `_in_flight.discard`（:569-570）。disabled 且无 semaphore 的分支 :562-564 标注 `pragma: no cover`。
  - `_audit(...)` :574-604 — 结构化审计 JSON（action/kind/exit_code/ok/duration_ms/throttled/timeout/confirm，sort_keys）固定 WARNING 级打到 `oc_slimapi.actions_audit` logger，与 `OC_SLIMAPI_LOG_LEVEL` 无关（:586-589；handler 在 logging_config.py:27,55-75 配置，`propagate=False`、幂等、stderr 流）。覆盖全部路径含 timeout/spawn-fail/断连/节流（:589）。
  - `_execute(spec, start, confirmed)` :608-740 — **rev-13/rev-14 统一生命周期**：spawn 包成独立 task + `asyncio.shield`（Bug F：spawn 中途被 cancel 时句柄可恢复、子进程不孤儿，:640-651）；spawn 抛 OSError/ValueError → 审计 + `ActionUnavailable`（:652-659）；stdout/stderr **并发 drain**（gather，防双管道互锁，:661-671）；`_wait_exit` 超 `timeout_s` → `outcome="timeout"` + `ActionTimeout`（:672-676）；进程退出后**立刻 killpg**（孙进程持管道写端不再阻塞 drain 到 EOF，Bug C，:677-683）；带 deadline 的 drain（:684-686）；except `CancelledError` → `outcome="cancelled"` re-raise（:690-696）；**finally 统一清理**：有句柄 → `_cleanup`；无句柄 → shield 等 spawn task 完成或直接取 result 恢复句柄再 cleanup（:697-731，`except (Exception, asyncio.CancelledError)` 兜底 recovered=None :716-718/:724-726）；双 cancel 窄竞态（Bug D，accepted）下无句柄可恢复且 cancelled → 仍补审计（:732-738）；最后 `_cancel_quietly` drain tasks（:739-740）。
  - `_build_result(...)` :742-774 — exec：`ok = exit_code==0`，`message=None|"non-zero exit"`（固定短串，stdout 已丢弃，:759-760）；query：非零退出 markdown=""（**部分输出也丢弃**）、`truncated = truncated and exit_code==0`（:762-774）。
  - `_spawn(spec)` :776-791 — staticmethod；`create_subprocess_exec(*argv, cwd, start_new_session=True, stdout/stderr=PIPE, env=_build_action_env())`——pgid==child pid 使 killpg 覆盖孙进程（:787 注释），P2-2 环境（:790）。
  - `_cancel_quietly(*tasks)` :793-797 — cancel + `gather(return_exceptions=True)`。
  - `_drain_stdout(proc, spec, state)` :799-840 — exec 全丢弃；query 累积到 cap 后**继续 drain-and-discard 到 EOF**（提前停会撑爆管道缓冲造成假超时，:806-809）；超 cap 截断置 `truncated`（:832-837）；`except Exception: return`（管道错误不破坏结果路径，:838-840）。
  - `_drain_stderr(proc, name)` :842-872 — 字节帽 64KiB 后丢弃、永不 raise；有输出则以 WARNING 进 journald（:865-872）。
  - `_drain_with_deadline(...)` :874-907 — `wait_for(gather(drain...), 5.0)`；超时 → 警告 + 强杀 drain + 返回 holder 中的部分输出并标 truncated=True（:894-907）。
  - `_wait_exit(proc, timeout_s)` :909-931 — **轮询 `proc.returncode`**（50ms 步长）而非 `Process.wait`：asyncio 的 wait 要等管道也断开（Bug C 根因），transport 在 SIGCHLD 即缓存 returncode（:911-922）；超时抛内建 `TimeoutError`。
  - `_killpg_quiet(proc)` :933-940 — `os.killpg(pid, SIGKILL)`，`ProcessLookupError` 吞掉。
  - `_cleanup(proc, spec, start, confirmed, outcome)` :942-975 — finally 中无条件调用：killpg → （returncode 为 None 时）`wait_for(proc.wait(), 5.0)`（ProcessLookupError/Timeout/CancelledError 均吞，:964-969）→ `outcome` 非 None（timeout/cancelled）时补失败审计（:970-975）。永不 raise（:961-963）。

- **依赖**：stdlib（asyncio/json/logging/math/os/re/signal/stat/time/tomllib）+ `.errors.CodedHTTPException`（:41）。
- **被依赖**（rg 反查）：`app.py:19`（`load_registry as actions_load_registry`）与 `app.py:421-424`（lifespan 内 `app.state.actions_registry = actions_load_registry(settings)`，注释强调 best-effort 不炸 lifespan、Semaphore lazy 绑 loop）；`routes/actions.py:33`（`ActionError, ActionResult`）；`logging_config.py:27,55`（audit logger 名与固定 WARNING stderr handler）；测试 `tests/test_actions.py`（含 :884-897 对 `_spawn` 的 monkeypatch 验证 shield 行为）、`tests/test_actions_routes.py:34-38`。配置来源：`config.py:507`（`actions_file: str|None = os.getenv("OC_SLIMAPI_ACTIONS_FILE") or None`）、`config.py:509`（`actions_max_concurrent` env `OC_SLIMAPI_ACTIONS_MAX_CONCURRENT` 默认 4）、`config.py:1044-1045`（启动校验 `>= 1`，无上限）。

- **状态/可溶性**：
  - 运行态表：`_actions`（加载后不可变，**无 reload 机制**——改 manifest 必须重启 sidecar）；`_in_flight`（单飞标记，invoke finally 必清）；`_last_run`（节流时间戳，**仅内存**，重启清零，:485-487 注释自认并指向 operations.md）；`_semaphore`（进程生命周期内常驻）。
  - 锁：无 threading 锁——单线程事件循环 + "标记先于首个 await" 约定（:515-521 注释）。
  - task：无常驻 task；每次调用临时创建 spawn_task/stdout_task/stderr_task，`_execute` finally 保证 cancel（:739-740）；子进程经 killpg+reap 收口（孙进程经 setsid 逃逸组时仅 5s drain 兜底，进程本身可能残留——:884-886 注释承认）。

- **错误路径（action_* 构造点逐点）**：`ActionsDisabled` :510；`ActionNotFound` :513；`ActionThrottled` :520（单飞，retry_after=2）与 :535（min_interval，retry_after=ceil(remaining)）；`ActionConfirmRequired` :527；`ActionBusy` :547；`ActionUnavailable` :659；`ActionTimeout` :676。HTTP 映射统一在 `to_coded()` :174-185（Retry-After :178、timeout_s :182）。**TOML 解析失败行为**：文件级（symlink/权限/owner/根形状/TOMLDecodeError/OSError）→ ERROR 日志 + 整体 disabled（:442-452）；条目级 → WARNING + 仅丢该条（:306-310）；lifespan 永不炸（app.py:421-424）。

- **疑问点**（12 条，宁多勿漏）：
  1. **manifest 死配置/启用面**：`config.py:507` 默认 None → 功能默认关；`deploy/oc-slimapi.service:60` 的 `#Environment=OC_SLIMAPI_ACTIONS_FILE=%h/.config/oc-slimapi/actions.toml` **默认注释**——生产启用 = 手工取消注释 + 拷贝 `deploy/actions.manifest.example.toml`（:1-16 自述 "copy to a machine-local path, chmod 0600"）+ 改 argv[0]（example 指向 `/home/mar/.config/opencode/scripts/*.py` 机器本地路径）。仓库内无任何代码/脚本自动启用；operations.md §11（:570-630）为唯一操作手册。审计应确认生产机该 env 是否实际设置（本仓只读无法验证运行态）。
  2. **manifest 注入面（TOML 路径 env）**：能控制服务环境变量或放置 owner=mar、0600 文件者即能声明任意动作——owner/writabit 校验（:280-285）只覆盖 **manifest 文件本身**；argv[0] 只做 realpath+isfile+X_OK（:346-350），**不检查目标文件/所在目录的写位**（如 `~/.config/opencode/scripts/plan_limit.py` 被同组可写目录下的替换即接管动作）。校验（加载时）与执行（调用时）之间对 argv[0] 存在 TOCTOU：spawn 仍用原路径（:784-785），realpath 仅用于校验。
  3. **confirm 流程安全**：confirm 是无状态布尔（routes/actions.py:144），无 challenge/nonce/时效——任何能达明文 :4097（或 stunnel mTLS 14097 后）的客户端重放 `{"confirm":true}` 即可执行 `restart` 类 exec 动作（example :40-47 即 systemctl --user restart）。模块头 :12-19 明示 risk-accepted、与 catch-all → `/global/upgrade` 等明文控制端点同级；但 mTLS 之外的明文 :4097 监听面使该声明成立的前提是 loopback-only 绑定（在 app/config 卡核对）。另 query 恒免 confirm（:524 仅 exec 判定；:377-378 禁 query 声明 require_confirm）。
  4. **节流按"尝试"而非"成功"计**：`_last_run` 在 spawn 之前写入（:558），spawn 失败（ENOENT 等）与超时同样烧掉 min_interval 窗口——`min_interval_s=60` 的坏动作每分钟最多报错一次。契约（v2-contract.md:241）只说 "min_interval 防同动作频繁调用"，未澄清该语义。
  5. **`min_interval_s` 无上限**（:370-372 仅 `>=0`）：极端 manifest 可产出巨大 `Retry-After`（:535 `ceil(remaining)`，int 秒直出 :178）。
  6. **audit 的 duration_ms 口径**：各失败点用 `_ms(start)`（:518/:526/:533/...），start 取 invoke 入口（:508）——失败审计的 duration 含 semaphore 等待与判定耗时；成功路径同样从 invoke start 起算（`_build_result` :750）。口径一致但与 `timeout_s`（纯执行预算）不同义，读审计时易误读。
  7. **失败 query 的部分输出全丢**：`_build_result` :768-771 非零退出 markdown=""、:772 truncated 强制 False——超长被截的失败 query 既无 stdout 线索也无 stderr 回传（stderr 仅 journald，:865-872），客户端只能看到 exit_code。
  8. **双审计核对（未发现重复）**：spawn OSError → :657 审计一次，finally 中 spawn_task.result() 重抛 OSError 被 :725 捕获 → recovered=None → outcome≠"cancelled" 不补审（:732）；timeout/cancelled → 仅 `_cleanup` :970-975 一次；成功 → 仅 :565 一次；semaphore 等位 cancel → 仅 :554 一次。逻辑上单次，但该不变量完全靠 outcome/proc 双变量编排，无断言保护——后续改动易破。
  9. **`_wait_exit` 50ms 轮询**（:931）：超时精度 ±50ms；`timeout_s` 最小 1.0s（:53）下无实际风险，仅备注。
  10. **exec 丢弃全部 stdout**（:815-820）：exec 信封只有固定 message（:760），排障仅剩 stderr journald 与 exit_code；契约如此设计（v2 §2），运维侧需知晓。
  11. **环境白名单的运维耦合**：`_build_action_env`（:92-100）不透传 `OC_SLIMAPI_*`（P2-2，正确），但 example 的 `systemctl --user restart` 依赖 `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` 在 sidecar 服务环境中存在（:83-85 注释自认）——systemd user 服务缺这些 env 时动作静默失败（以 stderr/exit_code 形式）。
  12. **`_INTERPOLATION_MARKERS` 仅 3 个标记**（:73）：`${`、`%(`、`$(`；若未来有人改 `shell=True`，`;`、反引号、`|` 等不在守卫内——注释（:70-72）明说这是 regression guard 而非授权边界，可接受但审计记录在案。

---

### src/oc_slimapi/discovery.py（192 行）

- **职责**：全局根 session 发现助手——`GET /experimental/session?roots=true&archived=true&limit=N`（opencode GLOBAL 顶层 session，跨全部 workdir 实例）的取数 + cap 读 + JSON 解析 + 顶层 list 守卫，返回 `(sessions, discovery_complete)`。**不**校验个体 session 形状（caller 负责：questions 宽松跳过 / directories 严格 503，:10-18）。两种公开形态（B1 fix 2026-08-16，:24-31）：解析后 list 形（`fetch_global_root_sessions`）与 **capped raw bytes + complete 标志**（`fetch_global_root_sessions_raw`，coalesce 共享飞行的值——展开图绝不跨 lease 共享）。

- **对外符号**：
  - `_DISCOVERY_LIMIT = 10_000` :52 — 发现调用页大小安全帽。`roots=true` 只回顶层 session（parentID==null），数量 ≈ workdir 数，实践中永远到不了 10000；**页恰好填满 → discovery 标记 incomplete**，客户端降级（questions: authoritativeDirectories→部分替换；directories: discoveryComplete=false），:43-51 注释。导出供 caller 作 `limit` 传参与测试 monkeypatch。
  - `_fetch_discovery_body(upstream_client, request, *, limit)` :55-103 — 私有共享取数：send（stream=True）:69-79；初始 `httpx.RequestError` → `raise_upstream_unavailable(exc)` :80-81；**status>=400（4xx 也）→ 读错误 body 记账后 503 `upstream_unavailable`**（不映射 `upstream_http_N`——发现是内部派生调用，泄漏上游状态会误导客户端以为某 directory 失败，:84-91）；成功路径 `read_with_cap(config.max_response_bytes, on_read=stash_up_in)` :92-95，cap 超（body None）→ 503 :96-97；中途 `RequestError` → 503 :99-101；finally `aclose` :102-103。`config = request.app.state.config`（:67；app.py:197 装配；`max_response_bytes` 默认 64MiB，config.py:365）。
  - `_validate_discovery_list(parsed)` :106-110 — 顶层必须 JSON list，否则 503。
  - `fetch_global_root_sessions_raw(...)` :113-143 — raw 形态：取 body → leader 侧瞬时 `orjson.loads` 仅做 list 形状校验与 `complete = len < limit` 计算 → **`del sessions`**（:142，展开图不入 lease）→ 返回 `(body_bytes, complete)`。错误映射与 list 形完全一致（坏 JSON/非 list → 503，**飞行失败则无 joiner 见到未校验 body**，:129-132）。
  - `fetch_global_root_sessions(...)` :146-192 — list 形态：同取数 + `orjson.loads`（in-loop，与 sessions.py 同模式，:20-22 / :187-190 论证）+ list 守卫 → `(sessions_payload, len < limit)`。docstring :152-177 详述参数语义（roots ⇒ 顶层；archived ⇒ 超集，保护仅有归档 session 的 workdir 不被丢）与四类错误映射。不校验个体形状（:179-180）。

- **依赖**：`httpx`、`orjson`、`fastapi.Request`、`.traffic.stash_up_in`（traffic.py:284）、`.transform.read_with_cap`（transform.py:143）、`.upstream_errors.raise_upstream_unavailable`（upstream_errors.py:35，NoReturn）。
- **被依赖**（rg）：`routes/directories.py:8,80-81`（list 形，严格消费）；`routes/questions.py:10-13,172-211`（raw 形作 coalesce 值 + list 形作非合并路径）；`routes/permissions.py:10-13,187-226`（同 questions 模式）；测试 `tests/test_directories_routes.py:494`、`tests/test_questions_routes.py:832,860,866`（monkeypatch 路由模块级 `_DISCOVERY_LIMIT`）、`tests/test_questions_coalesce.py:617,632`（包装 raw 形计数）。
- **状态/可溶性**：**全无状态纯函数**（async）；无锁、无 task、无缓存——coalesce/lease 逻辑在 questions/permissions 侧，本模块只承诺不把展开图交出去（:127-128）。
- **错误路径**：全部收敛到单一出口 `raise_upstream_unavailable`（503 `upstream_unavailable`）——send 网络错 :81、上游 >=400 :91、cap 超 :97、中途读错 :101、坏 JSON :139/:190、非 list :109。本文件**无** action_* 码；不产生 422/413。
- **疑问点**（8 条）：
  1. **错误分支 `aread()` 无 cap**（:89）：上游 >=400 时 `await response.aread()` 全量读入内存（仅为 `stash_up_in` 记账字节数）——成功路径用 `read_with_cap`，错误路径未用；恶意/异常上游回超大 4xx body 会全量进 RSS。loopback 信任域内低危，但与模块"cap-read"的整体姿态不一致，值得记为加固点。
  2. **503 不带上游状态细节**：:91 `raise_upstream_unavailable()` 无 exc 链、无日志记录上游 status/err body 摘要——排障时 journald 只见 503，无法区分"上游 404（实验端点不存在/版本不支持）"与"上游 500"。设计动机（不泄漏给客户端）正确，但服务端日志侧也一并丢了信息。
  3. **`_DISCOVERY_LIMIT` 语义边界**：`complete = len(sessions) < limit`（:141/:192）——恰好等于 limit → incomplete（保守，正确）；若上游**忽略** limit 参数返回超量，同样标 incomplete，但此时数据可能已超 10000 条被静默当作"可能截断"（客户端降级为部分覆盖）。依赖上游 `/experimental/session` 确实尊重 `limit`（上游行为卡核对，AGENTS.md 要求不凭记忆断言上游语义）。
  4. **模块 docstring 消费方清单不全**：:3-6 只列 questions/directories，permissions.py 同为主要消费者（:10-13）；B1 段落（:28-30）有提，首段遗漏——轻微文档漂移。
  5. **类型注解 `list[dict]` 是承诺非保证**（:151）：`_validate_discovery_list` 只验顶层 list，元素可为任意 JSON 值；questions 侧宽松跳过、directories 侧严格 503，permissions 侧策略需在对应卡核对（本卡不越界）。
  6. **默认参数 def 时绑定**：两个公开函数 `limit: int = _DISCOVERY_LIMIT`（:116/:149）在定义期求值——运行期改模块 `_DISCOVERY_LIMIT` 不影响默认值；实际 caller 全部显式传 `limit=_DISCOVERY_LIMIT`（路由模块级名字，可被测试 monkeypatch，tests 已如此用）。若未来新增 caller 依赖默认值，monkeypatch 语义会悄悄失效。
  7. **in-loop `orjson.loads`**（:137/:188）：接近 64MiB cap 的大 body 会在事件循环内解析，造成停顿；与 sessions.py L76 同模式且有书面 rationale（:20-22），属已接受取舍，记录在案。
  8. **无 per-call timeout**：本模块不给 `send` 传 timeout，完全依赖共享 `upstream_client` 的全局超时配置——若上游挂起，发现调用（及依赖它的 questions/permissions/directories 请求）受上层超时约束；需在 app.py/httpx client 卡确认确有全局 timeout。

---

### src/oc_slimapi/routes/actions.py（162 行）

- **职责**：`/slimapi/actions` 的两个 sidecar 本地路由（无上游调用、不走 catch-all 反代，:3-4）：`GET /slimapi/actions` 目录发现；`POST /slimapi/actions/{name}` 按 manifest 白名单键调用。body 手工读取（空 body/`{}` → `{}`；非对象/坏 JSON/非布尔 confirm → 422；>1KiB → 413，admission 前拒绝）；7 个 action 错误经 `to_coded()` 映射并补 `Cache-Control: no-store`。两端点 gzip 协商 + 全响应 no-store（契约 §5，:21-24, :39-41）。

- **对外符号**：
  - `router = APIRouter(prefix="/slimapi", tags=["actions"])` :37。
  - `_NO_STORE = "no-store"` :41。
  - `_BODY_CAP_BYTES = 1024` :43-46 — POST body 硬帽（body 恒为空或 ~17 字节的 `{"confirm":true}`；防明文内存 DoS）。
  - `_request_too_large()` :49-54 — 413 `request_too_large`（带 no-store 头）。
  - `_read_body(request)` :57-108 — Content-Length 声明 >1024 → 不读一字节直接 413（:72-79；非数字 CL → 走流式帽 :76-77）；`request.stream()` 逐 chunk **先查帽再 append**（rev-14：单个超大 chunk 不落缓冲，:80-88）；空/全空白 → `{}`（:89-90）；`orjson.loads` 失败 → 422 `invalid_request_body`（:91-97）；非 dict → 422（:98-102）；`confirm` 存在但非 bool → 422（:103-107，fail-closed，`{"confirm":null}` 也拒）。
  - `_envelope(result)` :111-127 — 200 信封：公共 `{kind, ok, exit_code, duration_ms, message}`；query 追加 `markdown`/`truncated`（:124-126）。
  - `list_actions(request)`（`GET /actions`）:130-137 — `app.state.actions_registry`（:132）→ `{"enabled": registry.enabled, "actions": registry.discover()}` + gzip + no-store；disabled 时 200 + `enabled:false, actions:[]`。
  - `invoke_action(request, name)`（`POST /actions/{name}`）:140-162 — 先 `_read_body`（413/422 先于一切 admission，:143）→ `confirmed = bool(payload.get("confirm", False))`（:144）→ `registry.invoke`，`except ActionError` → `to_coded()` + 补 no-store + `raise ... from exc`（:145-157，注释列出 7 码全映射）；成功 → `_envelope` + gzip + no-store（:158-162）。

- **依赖**：`fastapi.APIRouter/Request`、`orjson`、`..actions.ActionError/ActionResult`、`..errors.CodedHTTPException`、`..gzip_util.json_response`（gzip_util.py:110-122：orjson 序列化 + `Vary: Accept-Encoding` + level 6 gzip）。
- **被依赖**：`app.py:29`（import）+ `app.py:761`（router 注册元组第 4 位，先于 `install_proxy` catch-all）；`tests/test_actions_routes.py:38,71-77`（测试自建 app 装配 `app.state.actions_registry`）；INTERFACE_MAP.md:74-75 有两条端点记录（check_routes_doc.py 防漂移对象）。
- **状态/可溶性**：**无状态**——所有状态在 `ActionRegistry`（app.state），handler 零本地状态、无锁无 task。
- **错误路径（构造点逐点）**：413 `request_too_large`——:79（声明 CL 超帽）与 :87（chunked 实读超帽），构造于 :49-54；422 `invalid_request_body`——:94-97（坏 JSON）、:99-102（非对象）、:104-107（非布尔 confirm）；action_* 7 码——统一 :153 `exc.to_coded()`（Retry-After/timeout_s 由 actions.py:174-185 注入），no-store 补章 :154-156。GET 端点无错误分支（disabled 也是 200）。
- **疑问点**（7 条）：
  1. **`invalid_request_body` 未见于任何契约码表**：rg 全仓，该 code 名仅出现在本文件（:63,:95,:100,:105）与审计 inventory；v2-contract.md:207 只写 "malformed body → **422**" 未给 code 名，§7 码表（v2-contract.md:239-245,491）含 7 个 action 码与 `request_too_large` 但无 `invalid_request_body`；v3-contract.md 中亦无。实现有码名、契约只锁状态码——客户端若按码名分支会踩空。属文档漂移或"422 码名未冻结"，建议核对是否要在契约补记。
  2. **错误优先级：413/422 先于 `actions_disabled`**：`_read_body`（:143）在 `invoke`（:146）之前——manifest 未配置时，malformed/超大 body 得 422/413 而非 503 `actions_disabled`。契约未明示该优先级；实现合理（DoS 守卫最先）但值得在契约澄清。
  3. **`name` 无路由级格式校验**：path 参数纯 str（:141），无 `_NAME_RE`/长度预检——未知/超长/URL 编码名一律落到 registry 字典查找 → 404 `action_not_found`（actions.py:513）。无注入面（不拼路径不 eval，v2-contract.md:209 亦如此声明）；但 `/slimapi/actions/`（空 name）由 Starlette 路由层处理不进本 handler，行为（307 重定向 vs 404）取决于路由器配置而非契约。
  4. **confirm 布尔语义**：`bool(payload.get("confirm", False))`（:144）——`{"confirm":false}` 显式 false 与缺失等价（409），`require_confirm=false` 的 exec 收到 confirm 被忽略正常执行（v2-contract.md:206 明示）；无 replay 防护（同疑问卡 actions.py 第 3 条）。
  5. **Content-Length 负数/重复头**：`int("-5")` 合法且 `>1024` 为 False → 落入流式帽（:76-79），无害；重复 CL 头的取值行为取决于 Starlette `headers.get`（本卡未展开验证），流式帽兜底。
  6. **413/422 已带 no-store，422 无 Vary**：`_request_too_large`（:52-53）与三个 422 构造点均 pin `Cache-Control: no-store` 但不含 `Vary: Accept-Encoding`（错误响应无 gzip 协商，天然无 vary 需求）——与契约 §5 "every response carries no-store" 一致（INTERFACE_MAP.md:75 备注同）；GET 成功响应的 `Vary` 由 `json_response` 统一加（gzip_util.py:119）。
  7. **`?v=3` 版本门不在本层**：模块 docstring（:22-24）声明版本选择器已覆盖所有 `/slimapi/**`——实际 gate 在全局中间件/入口（app 卡核对）；handler 自身不检查 `v`，若版本门存在绕过路径，本路由无二次防线（GET 尤其无任何参数校验）。
