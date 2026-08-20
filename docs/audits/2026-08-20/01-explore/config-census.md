# E3 配置普查 — env 全集 × Settings 字段 × 代码读取点 × operations.md × deploy 四方对账

> 审计探索产物（只读），2026-08-20。基准：`src/oc_slimapi/config.py`（1158 行全文精读，行号与 `parts/e1-04-config.md` 精读卡片复核一致）、`src/oc_slimapi/versioning.py`、`src/oc_slimapi/app.py`、`docs/operations.md`、`deploy/oc-slimapi.service`、`tests/`。
> 证据格式 `路径:行号`。非法值行为缩写：**启动拒绝** = lifespan/`main()` `validate()` 抛 RuntimeError（fail-closed）；**导入崩溃** = `settings = Settings()`（config.py:1158）import 期抛裸 `int()/float()` ValueError（消息不含 env 名）；**静默 False** = 布尔旋钮非真值字符串静默解释为关闭、无告警；**warning+忽略** = 打 warning 不破启动；**无校验** = 照单全收。

## 0. 总量

| 类别 | 数量 |
|---|---|
| Settings 字段（config.py:356-671） | 71 |
| Settings 读取的 `OC_SLIMAPI_*` env（70 字段有 env 输入；`server_api_version` 已常量钉死无输入） | 70 |
| 非 Settings 生产 env（`OC_SLIMAPI_LOG_LEVEL`、`OC_SLIMAPI_OPENCODE_DB`） | 2 |
| **活跃生产 env 全集** | **72** |
| 历史/幽灵 env（src/ 零读取或已删字段，见 §5） | 8 |
| 测试专用 env（EQ_BINARY / EQ_WRITE_REAL_GOLDEN / REQUIRE_EQ007，仅 tests/） | 3 |
| 四方差异条目（§4） | 14 |

非 Settings 双入口：
- `OC_SLIMAPI_LOG_LEVEL`：`src/oc_slimapi/logging_config.py:41`（默认 INFO；非法值 warning 回退 INFO——logging_config.py:48 具名告警，**非崩溃**）；operations.md:583,643 记载。
- `OC_SLIMAPI_OPENCODE_DB`：`src/oc_slimapi/dbaux/path_resolution.py:28`（`ENV_EXPLICIT_DB`，显式配置最高优先，config.py:652-654 注释自述"DB 路径不是 Settings 字段"）；operations.md:389 记载（生产推荐）。

## 1. 四方对账主表（Settings 71 字段 ↔ 代码读取点 ↔ operations.md ↔ deploy）

> 代码读取点只列功能消费处（config.py 自身定义/validate 不重复列）；「ops」= operations.md 显式记载行号（— = 未记载；`泛指` = 仅 operations.md:124"调参见 develop.md §配置"）；「deploy」= deploy/oc-slimapi.service 的 Environment= 行（— = 未设置）。默认值/校验行号均属 config.py。

| # | 字段 / env | 默认值 | 校验 / 非法值行为 | 代码读取点 | ops | deploy |
|---|---|---|---|---|---|---|
| 1 | `host` / `OC_SLIMAPI_HOST` | 127.0.0.1 | ∈{127.0.0.1,::1,localhost,0.0.0.0}（:773）→ 启动拒绝 | app.py:640,777（uvicorn bind） | :88,:331 | **:28 =0.0.0.0（≠默认）** |
| 2 | `port` / `OC_SLIMAPI_PORT` | 4097 | 1..65535（:786）→ 启动拒绝；畸形→导入崩溃 | app.py:641,778 | :89 | :29 =4097（=默认） |
| 3 | `upstream` / `OC_SLIMAPI_UPSTREAM` | http://127.0.0.1:4096 | loopback http + 无凭证/query/fragment（:779,:781）→ 启动拒绝 | upstream.py:42；app.py:642 | :90,:331 | :30（=默认） |
| 4 | `max_message_bytes` / `OC_SLIMAPI_MAX_MESSAGE_BYTES` | 32 MiB | ≤256 MiB（:845）→ 启动拒绝；**0/负通过**（无下界，e1-04 Q3） | messages.py:545,655,1228,1593；write_groups.py:138,370 | :91 | :31 =33554432（=默认） |
| 5 | `max_transforms` / `OC_SLIMAPI_MAX_TRANSFORMS` | 1 | ≥1（:837）+ RSS 乘积 ≤512 MiB（:878-892）→ 启动拒绝 | transform.py:208,210；app.py:321,643 | 泛指 | — |
| 6 | `transform_wait_seconds` / `OC_SLIMAPI_TRANSFORM_WAIT_SECONDS` | 2 | >0（:839）；**nan 通过**（无 isfinite） | transform.py:233；messages.py:1211,1579 | 泛指 | — |
| 7 | `max_response_bytes` / `OC_SLIMAPI_MAX_RESPONSE_BYTES` | 64 MiB | >0（:841）且 ≤256 MiB（:850）→ 启动拒绝 | transform.py:23；discovery.py:93；messages.py:827,833,1019；_read_passthrough.py:148 等 | :289,:292（RAW_FETCH 语境） | — |
| 8 | `max_expand_response_bytes` / `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` | 8 MiB | ∈[1 KiB,32 MiB]（:860）；畸形→**具名** RuntimeError（_int_env :197） | versions.py:130,148（`fragmentMaxBytes`） | — | — |
| 9-12 | `catalog_cache_*`（ttl/max_entries/max_bytes/max_entry_bytes） | 300/16/16 MiB/1 MiB | ttl≥0（:930）、entries≥1（:935）、bytes≥1 MiB（:937）、entry≥1 且 ≤bytes（:942,:948）→ 启动拒绝 | app.py:362-365（cache ctor） | :283-286 | — |
| 13 | `coalesce_enabled` / `OC_SLIMAPI_COALESCE_ENABLED` | true | 无 → 静默 False | messages.py:995；permissions.py:167；sessions.py:728,839；questions.py:145；app.py:384 | :287 | — |
| 14 | `raw_fetch_concurrency` / `OC_SLIMAPI_RAW_FETCH_CONCURRENCY` | 4 | ≥1（:907）→ 启动拒绝 | app.py:387 | :288 | — |
| 15 | `raw_fetch_max_bytes` / `OC_SLIMAPI_RAW_FETCH_MAX_BYTES` | 64 MiB | >0（:909）+ raw+transform ≤576 MiB（:911-923）→ 启动拒绝 | app.py:386 | :289,:292 | — |
| 16 | `etag_enabled` / `OC_SLIMAPI_ETAG_ENABLED` | true | 无 → 静默 False | etag.py:96、providers_projection.py:121（均 `getattr(...,True)`） | **—（未记载）** | — |
| 17 | `message_fingerprint_enabled` / `OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED` | true | 无 → 静默 False | messages.py:848,885,1058,1069,1083 | :290 | — |
| 18 | `smoke_session_id` / `OC_SLIMAPI_SMOKE_SESSION_ID` | None | 无校验 | app.py:138（smoke 探针） | :473 | — |
| 19 | `server_api_version`（**无 env 输入**，钉死常量 4，:436） | =SERVER_API_VERSION | :806,:827 自引用不变式（常量下恒真）；env `OC_SLIMAPI_SERVER_API_VERSION` 出现→warning+忽略（:796-804） | **src/ 零功能读取**（versions.py:61 用常量；health.py 不回显） | :92（弃用说明） | **:32 =2（已废弃 env，仅 warning）** |
| 20 | `accepted_client_versions` / `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | "3,4"（ACCEPTED_CLIENT_VERSIONS） | 语法畸形→导入期具名 RuntimeError（:170）；**≠(3,4)→启动拒绝**（:817-822 钉死） | health.py:40,49-50,133,138-139（`accepted_client_versions`/clientMin/Max 回显） | :92（弃用+钉死说明） | **:33 =2,2（≠钉死 → 必然启动拒绝，§3）** |
| 21-25 | SSE 控制面：`max_subscribers_per_directory`/`max_total_subscribers`/`sse_queue_items`/`sse_buffer_bytes`/`sse_max_frame_bytes` | 8/16/256/2 MiB/256 KiB | ≥1（:978）/≥per-dir（:980）/≥2（:984）/＞0（:995,:997）→ 启动拒绝 | app.py:479-483 → sse/registry.py:54-64,212-223 | **—（均未记载）** | — |
| 26-29 | token-stream 四旋钮（max_subscribers/queue_items/buffer_bytes/max_frame_bytes） | 8/64/512 KiB/1 MiB | ≥1（:1004）/≥2（:1006）/＞0（:1013,:1015）→ 启动拒绝 | app.py:539,563-566 → tokenstream hub/subscriber | **—（未记载）** | — |
| 30-32 | `token_stream_debug_*`（live_budget/part_max/live_parts） | None | 设了＞0（:1019,:1023）且 live_parts≤4096（:1027-1041）；畸形→**裸** ValueError（:180）；debug-only | hub.py:156-161（`apply_debug_budget_overrides` 运行时改写模块全局） | —（有意不载） | — |
| 33 | `shell_deny_list_enabled` / `OC_SLIMAPI_SHELL_DENY_LIST_ENABLED` | 1 | 无校验 | **仅 app.py:636,644 startup banner**——deny-list 本体已随 catch-all 反代退役（proxy.py:21-23），**功能零读取**（§6） | — | — |
| 34 | `directory_allowlist` / `OC_SLIMAPI_DIRECTORY_ALLOWLIST` | None（三态） | 条目绝对/无控制字符（:738-750）+ realpath 可解析（:758-766）→ 启动拒绝；None=不过滤，""=[]（/file 路由 reject-all、SSE hub 放行——语义不对称，e1-04 Q5） | health.py:91；sessions.py:365；read_groups.py:51,140-141；global_hub.py:22-23,528-530,572-585 | **—（未记载）** | — |
| 35-36 | `deployment_revision`(_file) | None | 无（best-effort；读文件失败→warning+None :700-707） | config.py:685-692（`read_deployment_revision`）；app.py:414；health.py:87 | :130（仅 REVISION） | — |
| 37 | `actions_file` / `OC_SLIMAPI_ACTIONS_FILE` | None（空串→None） | 无（actions.py 自校验 manifest） | actions.py:433 | :570,:587,:592,:630 | :60（**注释掉，未启用**） |
| 38 | `actions_max_concurrent` / `OC_SLIMAPI_ACTIONS_MAX_CONCURRENT` | 4 | ≥1（:1044）→ 启动拒绝 | actions.py:432 | :579 | — |
| 39 | `traffic_metrics_enabled` / `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | true | 无 → 静默 False | app.py:286,720（ledger 开关） | —（develop.md:26 有） | — |
| 40 | `access_log_enabled` / `OC_SLIMAPI_ACCESS_LOG_ENABLED` | true | 无 → 静默 False | app.py:237,245-246 | —（develop.md 有） | — |
| 41 | `access_log_path` / `OC_SLIMAPI_ACCESS_LOG_PATH`（**DEPRECATED**） | logs/access.jsonl | 无直接校验；仅当 ACCESS_LOG_DIR 未设且值≠默认时 parent 兜底（:711-734）+ app.py:210-215 warning | config.py:730（`effective_access_log_dir`）；app.py:209 | —（traffic-accounting.md:261 记载） | — |
| 42 | `access_log_dir` / `OC_SLIMAPI_ACCESS_LOG_DIR` | logs | 显式设置永远压过废弃 path（:727-734） | app.py:209（经 effective_access_log_dir）→237,271-275,584,677 | :103,:201 | **:40 =%S/oc-slimapi/logs（≠默认）** |
| 43 | `access_log_compress_on_startup` | true | 无 → 静默 False | app.py:272 | — | — |
| 44 | `access_log_retain_days` | 0（不删） | ≥0（:1054）→ 启动拒绝 | app.py:275,678 | :105,:232（默认 0/生产 3） | **:46 =3（≠默认，有记载）** |
| 45 | `access_log_maintenance_interval_s` | 3600 | ≥60（:1050）→ 启动拒绝 | app.py:679 | :231 | — |
| 46 | `traffic_snapshot_enabled` | true | 无 → 静默 False | app.py:720 | —（develop.md:29 有） | — |
| 47 | `traffic_snapshot_interval_s` | 300 | ≥1（:1056）→ 启动拒绝 | traffic_snapshot.py:42；app.py:293 | :191（"默认 300s"文字） | — |
| 48 | `traffic_snapshot_path` | logs/traffic-snapshot.jsonl | 无校验 | traffic_snapshot.py:43；app.py:294,668 | :104,:201 | **:41 =%S/...（≠默认）** |
| 49 | `traffic_snapshot_retain_days` | 0（不删） | ≥0（:1059）→ 启动拒绝 | app.py:673 | :233,:238（默认 0/生产 30） | **:45 =30（≠默认，有记载）** |
| 50-51 | `client_id_hash` / `client_id_salt` | true / None | 无 → 静默 False（**明文记 device id 隐私回退**） | middleware/traffic_accounting.py:311-313（getattr） | **—（未记载）** | — |
| 52-53 | `skeleton_inline_output_max_(message_)bytes` | 4 KiB / 16 KiB | >0（:955,:959）且 ≤16 MiB（:966,:970）→ 启动拒绝 | messages.py:846-847,1056-1057；etag.py:73-74；health.py:72 | —（develop.md:25 有其一） | — |
| 54-56 | `questions_*`（max_response/aggregate/fanout_concurrency） | 2 MiB/16 MiB/8 | :1064,:1068,:1072,:1077 → 启动拒绝 | questions.py:233-235；app.py:402 | :249-251 | — |
| 57-59 | `permissions_*`（max_response/fanout/aggregate） | 2 MiB/8/16 MiB | :1086,:1090,:1094,:1099 → 启动拒绝 | permissions.py:251-253；app.py:408 | :259-261 | — |
| 60-62 | `qp_sweep_enabled` / `interval_seconds` / `daily_budget` | true/1800.0/100 | 无 / >0（:1103，nan 通过）/ ≥0（:1130） | app.py:501,506-507,521-522 | **—（未记载）** | — |
| 63-65 | `merged_fanout` / `max_fulls_per_page` / `max_bytes` | 8/16/8 MiB | [1,16]（:1132）/[1,64]（:1136）/＞0 且 ≤128 MiB（:1140,:1144） | messages.py:473,650-651,683 | :267-269 | — |
| 66 | `transform_absorb_budget_seconds` | 2.5 | >0（:1148，nan 通过） | messages.py:1205,1573 | :275 | — |
| 67 | `state_dir` / `OC_SLIMAPI_STATE_DIR` | state | 非空（:1154）→ 启动拒绝 | app.py:583（TurnRegistry） | :106,:212,:216 | **:54 =%S/oc-slimapi（≠默认）** |
| 68 | `dbaux_probe_interval_s` | 30 | >0（:1109，nan 通过） | app.py:601 | **—（未记载）** | — |
| 69 | `replay_max_count` / `OC_SLIMAPI_REPLAY_COUNT`（env 无 MAX_ 前缀） | 2048 | ≥1（:1117）；畸形→具名 RuntimeError（_int_env） | app.py:428（ReplayLog ctor） | **—（未记载）** | — |
| 70 | `replay_max_bytes_kb` / `OC_SLIMAPI_REPLAY_BYTES_KB`（单位 **KiB**） | 65536（=64 MiB） | ≥1（:1119）；×1024 换算在 app.py:429；config 侧无字节上限（e1-04 Q7） | app.py:429 | — | — |
| 71 | `replay_ttl_s` / `OC_SLIMAPI_REPLAY_TTL_S` | 900 | **math.isfinite 且 >0**（:1121-1129，唯一带 nan/inf 防护的 float 旋钮） | app.py:430 | — | — |

## 2. deploy `Environment=` 行全集（deploy/oc-slimapi.service）

| 行号 | env | 值 | 与默认关系 | 定性 |
|---|---|---|---|---|
| :28 | HOST | 0.0.0.0 | ≠默认（127.0.0.1） | 有意（Tailscale 直连；operations.md:88 同款） |
| :29 | PORT | 4097 | =默认 | 冗余但无害 |
| :30 | UPSTREAM | http://127.0.0.1:4096 | =默认 | 冗余 |
| :31 | MAX_MESSAGE_BYTES | 33554432 | =默认 32 MiB | 冗余 |
| :32 | SERVER_API_VERSION | **2** | env 已废弃 | warning+忽略（config.py:796-804），单独不破启动 |
| :33 | ACCEPTED_CLIENT_VERSIONS | **2,2** | **≠钉死 (3,4)** | **启动必然拒绝 → 无限 crash-loop（§3）** |
| :40 | ACCESS_LOG_DIR | %S/oc-slimapi/logs | ≠默认 | 有意（StateDirectory），operations.md:103 一致 |
| :41 | TRAFFIC_SNAPSHOT_PATH | %S/oc-slimapi/logs/... | ≠默认 | 有意，operations.md:104 一致 |
| :45 | TRAFFIC_SNAPSHOT_RETAIN_DAYS | 30 | ≠默认 0 | 有意，operations.md:233 一致 |
| :46 | ACCESS_LOG_RETAIN_DAYS | 3 | ≠默认 0 | 有意，operations.md:105,:232 一致 |
| :54 | STATE_DIR | %S/oc-slimapi | ≠默认 state | 有意，operations.md:106 一致 |
| :60 | ACTIONS_FILE | （注释） | 未启用 | opt-in 默认关，合规 |

非 `OC_SLIMAPI_` 行：`PYTHONUNBUFFERED=1`（:34）。

## 3. deploy 残留 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2` 启动行为推演（§任务5）

**结论：生产若用此 unit 文件原样启动，服务永远起不来，进入 systemd 无限重启循环。**

推演链（每步行号证据）：

1. unit `ExecStart=.venv/bin/python -m oc_slimapi.app`（deploy/oc-slimapi.service:18）→ 入口 `main()`（src/oc_slimapi/app.py:765）。
2. `main()` 在 uvicorn 起动前先 `settings.validate()`（app.py:771）——早于 lifespan 的第二处 validate（app.py:196）。
3. import 期（config.py:1158）`_version_range("2,2")`（config.py:166-171）语法合法 → 解析为 `(2, 2)`，**不触发** :170 的畸形 RuntimeError。
4. `validate()` 内按序：host=0.0.0.0 合法（:773）；`OC_SLIMAPI_SERVER_API_VERSION=2` 存在 → **仅 warning**"deprecated and ignored"（:796-804，测试 tests/test_v4_dual_window.py:151-157 证明越界值 9 也只 warning 不 raise）；区间自洽检查通过（:806，2≤2）；随后钉死检查 :817 `self.accepted_client_versions != ACCEPTED_CLIENT_VERSIONS`（versioning.py:44 `(3, 4)`）→ `(2,2) != (3,4)` → **RuntimeError**（:818-822，消息"must be (3, 4) — the production version gate is fail-closed to the pinned range and cannot be widened via env (got (2, 2))"）。
5. `main()` 捕获 RuntimeError → error 日志 `configuration error: ...` → `SystemExit(1)`（app.py:772-775），退出码 1。
6. systemd：`Restart=on-failure`（deploy:19）+ `RestartSec=5`（deploy:20）→ 每 ~5 秒重启一次、每次同样失败。unit 无 `StartLimitIntervalSec`/`StartLimitBurst` 覆盖；systemd 默认 burst=5 次/10s 窗口，5s 间隔下 10s 窗口内仅 ~2 次失败，**达不到 start-limit 门槛 → 无限 crash-loop**（非 failed 终态），journald 每轮一条 `configuration error`。

测试证据：tests/test_config.py:125-137（1,2 / 1,1 拒绝）、:140-145（**同为非钉死区间的 (2,3) 拒绝**，与 (2,2) 同一行 :817 逻辑）、:148-150（(3,4) 唯一接受）、:129/:136/:144 match `"must be \(3, 4\)"` 锁定错误消息。CHANGELOG.md:122（4.0.0）："`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` fail-closed 钉死 (3,4) 语义不变"；CHANGELOG.md:1055 显示 3.0.0 升级时已要求生产 unit 改为 `3,3`——deploy 模板仍停留在两代之前的 `2,2`。

附带说明：operations.md:92-94 明确声明"生产 unit 已同款清理，模板不再示例"，但 deploy/oc-slimapi.service:32-33 两行仍在——**operations.md 的声明与 deploy 实际文件不符**（文档声称的清理未落到模板）。

## 4. 差异清单（14 条）

| # | 差异 | 证据 | 定性 |
|---|---|---|---|
| D1 | **deploy:33 设置 `ACCEPTED_CLIENT_VERSIONS=2,2` ≠ 钉死 (3,4)** | config.py:817-822；versioning.py:44 | **致命**：启动拒绝 + crash-loop（§3） |
| D2 | **deploy:32 设置已废弃 `SERVER_API_VERSION=2`** | config.py:796-804 | 无害（warning+忽略），但 operations.md:92 声称已清理 → 模板滞后 |
| D3 | operations.md:92-94 声称"生产 unit 已同款清理"与 deploy:32-33 实际残留矛盾 | 上述两行 | 文档↔deploy 漂移 |
| D4 | deploy:28 `HOST=0.0.0.0` ≠ 默认 127.0.0.1 | config.py:356,:773 | 有意且 operations.md:88 记载（非缺陷，登记为差异） |
| D5 | deploy:40,41,45,46,54 五项 ≠ 默认（state-dir 族） | §2 表 | 有意且 operations.md:103-106,:233 一致记载（非缺陷） |
| D6 | deploy:29-31 三项 = 默认值（冗余设置） | §2 表 | 无害冗余；若默认值漂移会静默锁旧值 |
| D7 | operations.md **未记载** `OC_SLIMAPI_ETAG_ENABLED`（回退开关存在却无 ops 文档） | §1 #16 | 文档缺口 |
| D8 | operations.md 未记载 SSE 控制面 5 旋钮（#21-25）与 token-stream 4 旋钮（#26-29） | §1 | 文档缺口（仅 operations.md:124 泛指 develop.md） |
| D9 | operations.md 未记载 `DIRECTORY_ALLOWLIST`（含 `""`=[] 的 reject-all/SSE 不对称语义） | §1 #34；e1-04 Q5 | 文档缺口（安全相关语义无 ops 记载） |
| D10 | operations.md 未记载 `CLIENT_ID_HASH`/`CLIENT_ID_SALT`（静默 False = 明文 device id 隐私回退） | §1 #50-51 | 文档缺口 + 隐私回退无告警 |
| D11 | operations.md 未记载 `REPLAY_*` 三参数（env 名无 MAX_ 前缀 + BYTES_KB 单位陷阱）与 `DBAUX_PROBE_INTERVAL_S`、`QP_SWEEP_*` | §1 #60-62,68-71 | 文档缺口 |
| D12 | operations.md 未记载 `SHELL_DENY_LIST_ENABLED` 已是**无功能开关** | §6 | 文档缺口（ops 以为有 break-glass 作用） |
| D13 | develop.md:23-24 表仍写 `SERVER_API_VERSION=3`（默认值列）与 `ACCEPTED_CLIENT_VERSIONS=3,3`——前者已废弃、后者钉死值实为 3,4 | docs/develop.md:23-24；versioning.py:38,44 | 文档漂移（同 AGENTS.md"[3,3]"，e1-04 Q1） |
| D14 | CHANGELOG.md:566 与 docs/manual/traffic-accounting.md:261 称 `OC_SLIMAPI_ACCESS_LOG_MAX_BYTES`/`_BACKUPS`"保留兼容不删字段"，实际 Settings 已无这两字段（config.py 全文无），env 设置被**完全静默忽略** | config.py:354-671 无此二字段 | 文档过时（行为上兼容：不破启动） |

## 5. 废弃 / 幽灵 env 清单（src/ 零读取）

| env | 状态 | 证据 | 设置时的实际行为 |
|---|---|---|---|
| `OC_SLIMAPI_SERVER_API_VERSION` | **官方废弃**（S-B04，4.0.0） | config.py:796-804；CHANGELOG:122 | warning+忽略，启动不破 |
| `OC_SLIMAPI_ACCESS_LOG_PATH` | **官方废弃**（daily rotation 起） | config.py:522-527,:711-734；app.py:210-215 | DIR 未设且值≠默认时 parent 兜底 + warning |
| `OC_SLIMAPI_ACCESS_LOG_MAX_BYTES` | 字段已删 | config.py 无字段；CHANGELOG:566 残留记载 | 完全静默忽略 |
| `OC_SLIMAPI_ACCESS_LOG_BACKUPS` | 字段已删 | 同上 | 完全静默忽略 |
| `OC_SLIMAPI_V3_SELECTOR_ENABLED` | 已删除（3.0.0） | CHANGELOG:1058；src/ 零读取 | 完全静默忽略 |
| `OC_SLIMAPI_PROXY_ALLOWLIST`(`_ENABLED`) | **从未实现**（规划稿） | docs/ocmar/plans/2026-08-16-single-entry-roadmap.md:52,55 | 无对应代码 |
| `OC_SLIMAPI_ACCESS_LOG_ASYNC_WRITER` | 从未实现（设计稿 flag 建议） | docs/specs/access-log-writer-design.md:405 | 无对应代码 |

测试专用（非生产面）：`OC_SLIMAPI_EQ_BINARY` / `OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN` / `OC_SLIMAPI_REQUIRE_EQ007`（tests/test_equivalence_anchor.py:357,1050,389）。

## 6. 「Settings 字段但代码零（功能）读取」清单

| 字段 | 证据 | 定性 |
|---|---|---|
| `shell_deny_list_enabled` | proxy.py:16-23 明示 Shell/PTY deny-list 随 catch-all 反代退役（"all unreachable by construction"）；src/ 唯一读取 = app.py:636,644 startup banner；tests/ 零提及 | **幽灵开关**：env 设置仅改变 banner 日志，无任何路由行为；config.py:491-494 注释仍描述其"ops break-glass"作用，已失实 |
| `server_api_version` | src/ 功能零读取：versions.py:61 用常量 `SERVER_API_VERSION`，health.py 只回显 accepted_client_versions；仅 config.py:806,:827 自引用校验（常量钉死下恒真，:827 分支生产不可达——e1-04 Q2） | 常量载体字段；保留无害，但 :827-832 错误消息仍点名已废弃 env 名，具误导性 |

其余 69 字段均有 ≥1 个功能读取点（§1 表）。

## 7. fail-closed 语义专节 + 测试锁定表

### 7.1 ACCEPTED_CLIENT_VERSIONS 钉死 (3,4)

- 常量：versioning.py:44 `ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (3, 4)`；:38 `SERVER_API_VERSION = 4`。
- 语法层（导入期）：config.py:166-171 `_version_range`，畸形（无逗号/非整数）→ RuntimeError"must be min,max"（:170）——**具名 fail-fast**。
- 值层（启动期）：config.py:817-822 `!= ACCEPTED_CLIENT_VERSIONS` → RuntimeError，错误消息明言"cannot be widened via env"。区间 sanity（:806）先行：`minimum>maximum` 等先抛"slimapi version configuration is invalid"。
- 调用点：app.py:196（lifespan）与 app.py:771（`main()`，SystemExit(1) 于 :775）。
- 锁定测试（tests/test_config.py）：:111-115 `test_validate_rejects_inverted_version_range`；:125-130 `..._non_pinned_version_range_1_2`；:133-137 `..._1_1`；:140-145 `..._retired_v2_v3_range`（(2,3) 拒绝）；:148-150 `test_validate_accepts_pinned_range`；(3,4) 是唯一通过值。tests/test_v4_dual_window.py:131-133 `test_pinned_constants_dual_window`（常量=4/(3,4)）。

### 7.2 OC_SLIMAPI_SERVER_API_VERSION 废弃「warning+忽略」

- 实现：config.py:796-804 —— env 存在（值不解读）→ `get_logger("config").warning("...deprecated and ignored...pinned to %d")`，validate 继续；:436 `server_api_version: int = SERVER_API_VERSION`（无 env 输入）。
- 锁定测试（tests/test_v4_dual_window.py）：:136-148 `test_server_api_version_env_is_ignored_with_warning`（in-range 值 3：warning + `server_api_version == 4` + 不 raise）；:151-157 `..._out_of_range_still_ignored`（值 9：仅 warning）；:159-163 `..._constant_without_env`。

### 7.3 REPLAY 三参数校验

- 定义：config.py:669-671（`_int_env` ×2 + `float` ×1）；校验 :1117-1129（count≥1、bytes_kb≥1、ttl `math.isfinite` 且 >0——nan 使 `age > ttl_s` 恒 False 静默禁用 TTL 驱逐，rev-gate MAJOR-1 修死）。
- 锁定测试（tests/test_replay_log.py）：:568-578 `test_config_replay_defaults`（2048/65536/900.0）；:581-597 `test_config_replay_env_override`（8/16/30.0）；:600-608 `test_config_replay_env_malformed_int_fails`（"abc" → 具名 RuntimeError）；:611-626 `test_config_replay_fail_closed_validation`（0/-1/0/0/-5.0 参数化全拒）；:635-637 `test_replay_log_ctor_rejects_non_finite_ttl`（ctor 第二层）；:641-645 `test_settings_validation_rejects_non_finite_ttl`；:653-655 `test_settings_env_non_finite_ttl_fails_closed`（env 注入 "nan"/"inf"/"-inf"/"+inf"）。

### 7.4 其余 fail-closed 家族（摘要，测试同文件锁定）

- ACCESS_LOG_PATH 废弃回退 4 测试：tests/test_config.py:498-541（默认/显式 dir 压过/兜底/默认 path 不兜底）。
- 布尔旋钮（13 个）**无锁定"非法字符串告警"测试**——设计上静默 False（e1-04 Q8），属审计关注点而非测试缺口声明。
- 导入期裸 ValueError 家族（~40 字段）仅 `_int_env` 3 字段具名（config.py:183-197 docstring 自认诊断缺口，e1-04 Q6）。

## 8. 结论摘要

1. **deploy 模板是最危险的一份文件**：:33 的 `2,2` 使任何照抄它的生产部署直接进入无限 crash-loop（§3），而 operations.md:92-94 已声称清理——模板滞后两代未同步。
2. fail-closed 三支柱（版本钉死 / 废弃 warning / REPLAY 有限值校验）实现与测试锁定完整成套（§7），钉死语义无可绕过 env 后门。
3. 主要文档缺口：operations.md 对 ETAG/SSE/token-stream/DIRECTORY_ALLOWLIST/CLIENT_ID/REPLAY/QP_SWEEP/DBAUX 等 20+ 旋钮零记载，仅 operations.md:124 泛指 develop.md；develop.md:23-24 又自带过时钉值（D13），形成"ops 文档→次级文档→过时值"的链式漂移。
