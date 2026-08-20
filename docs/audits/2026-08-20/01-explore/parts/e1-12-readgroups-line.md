# E1-12 精读卡片：read_groups.py / permissions.py / questions.py

> 只读审计产物（2026-08-20）。引用格式 `src/oc_slimapi/...:行号`；三个文件均已全文精读（非抽样）。

---

### src/oc_slimapi/routes/read_groups.py（630 行）

- **职责**：v3-contract §10.a 读组（Batch C1）的 12 个 GET 路由：file/vcs/find/providers/session-single/active/global-health 七个附件读组 + B4 `session/{sid}/context`。thin controlled proxy（治理 only：selector `v`/`directory` 消费、body cap、ETag、gzip、结构化错误，无字节省流）+ 两处 v4 修订面分叉（§12 providers 安全投影、§13 session single canonical 投影）。模块 docstring（1-38）给出 12 路由→上游映射表与 opencode 上游锚点。

- **对外符号**（逐个）：
  - `router`（:90）— `APIRouter(prefix="/slimapi", tags=["read-groups"])`；被 `src/oc_slimapi/app.py:29,760` include。
  - `_resolve(request, directory)`（:93-112）— 读组 directory 解析：优先 selector stash（`resolve_route_directory(scope, None)`，:106），否则 `x-opencode-directory` header 兼容通道（空 header 视为缺席，:109-111）；两者均过 `validate_directory`（400 `invalid_directory`）。
  - `_project_session(raw)`（:115-120）— v3 session single 投影：orjson 解析，非 dict → `ValueError`（→503）；否则 `skeleton_session` 白名单重序列化。
  - `_authorized_file_directory(request, directory)`（:123-143）— rev-2 403 门（**仅 file 组 3 路由调用**）：allowlist `None` → 原样返回；否则 `candidate_canonical` 实时 realpath（相对/解析失败 → `None` → 统一 403 `directory_not_allowed`，:142），`match_allowlist` 边界对齐前缀匹配，通过后返回 **canonical** 形态（防 check-to-upstream symlink retarget，sub-3）。
  - `file_list`（:149-163）— GET `/slimapi/file`：`path` 必填（缺 → FastAPI 422）；`_resolve` + 403 门 → `read_passthrough_get("/file")`，selector 后 raw query 逐字嵌入上游 URL。
  - `file_content`（:166-175）— GET `/slimapi/file/content`，同上（`path` 必填）。
  - `file_status`（:178-186）— GET `/slimapi/file/status`，同上（无 `path`）。
  - `vcs_info`（:192-198）— GET `/slimapi/vcs`：`_resolve` 直接转发（**无 403 门**）。
  - `vcs_status`（:201-207）— GET `/slimapi/vcs/status`，同上。
  - `vcs_diff`（:210-221）— GET `/slimapi/vcs/diff`：声明 `mode`/`context`（仅类型/存在性校验面，值不消费），raw query 逐字转发，上游 400 逐字透传。
  - `find_file`（:227-241）— GET `/slimapi/find/file`：`query` 必填（缺 → 422）；`dirs`/`type`/`limit` 声明不消费。
  - `_handle_providers_v4(request, directory)`（:247-368）— §12 十二步流水线（求值序冻结 §12.5.2）：③ 无 permit 网络获取 + 状态映射（:289-320）；④ 解析前 source-body cap `read_with_cap`（:323-334）；⑤ cap 检查后才取 transform permit（:339-345）；⑥-⑪ 单 worker job `project_and_pack`（:341-345，严格校验/投影/四限额/canonical 序列化/body 限额/gzip+ETag 全在 worker）；⑫ 主上下文仅 `If-None-Match` 比对（:360-368）。全部错误盖 `Cache-Control: no-store`。
  - `_V4_PROVIDERS_FEATURE`（:379）/ `_V4_PROVIDERS_REVISION_ACTIVE()`（:382-384）— §3.3 门：`providers.redacted.v4 ∈ readiness_mod.SATISFIED`（调用时动态读，非 def-time 冻结；当前 `SATISFIED == REQUIRED` 全开，readiness.py:93）。
  - `_V4_SESSION_SINGLE_FEATURE`（:387）/ `_V4_SESSION_SINGLE_REVISION_ACTIVE()`（:390-392）— 同款门 `session.single.projection.v4`。
  - `config_providers`（:395-409）— GET `/slimapi/config/providers`：`wire_view_from_scope==4 && 门开` → `_handle_providers_v4`（:403-405）；否则（`?v=3` / selector-less / 门关）v3 受控透传 `read_passthrough_get`（零投影、全字段面，:406-409）。
  - `_session_single_sql()`（:425-434）+ `_SESSION_SINGLE_SQL`（:437）— dbaux 单行点查 SQL（session 投影列 + project LEFT JOIN 别名列；import 时物化为模块常量）。
  - `_project_native_session_single(raw)`（:440-452）— native 回退体：orjson 解析（非 dict → ValueError → 503）→ `native_session_to_record`（键 presence 三态载体）→ `canonical_session_skeleton_v4(record, fallback=True)`。
  - `_session_single_native_fallback(request, sid, directory)`（:455-517）— dbaux 不可用兜底：permit 先取（:469）→ 上游 `GET /session/{sid}`（:471-472）；5xx/网络 → 503；4xx 逐字（:481-488）；cap → 413；offload 投影 ValueError → 503（:504-506）；skeleton None → 503 `auxiliary_unavailable`（:511-513）；成功 200 + no-store（**v4 分支无 ETag**，批次 3A 未做）。
  - `_handle_session_single_v4(request, sid, directory)`（:520-552）— dbaux 可用 → 点查（:527）：空行 → 404 `session_not_found`（:535-537）；`rows_to_records` 空行（坏 JSON 列）→ 503 aux（:539-541）；投影 None（required 不可表示）→ 503 aux（:543-546）；`sqlite3.Error` → fail-closed 503（不回退、不泄细节，:531-533）；`AuxiliaryUnavailableError` 竞态 → 降级 native 回退（:528-530）。
  - `session_single`（:555-576）— GET `/slimapi/session/{sid}`：v4+门 → `_handle_session_single_v4`；否则 v3 `read_passthrough_get(project=_project_session)`（skeleton 白名单，drops `cost/tokens/location/subpath/repoPath/commit/branch/status/version`）。
  - `session_active`（:582-588）— GET `/slimapi/api/session/active` 宽容路由：**不消费 directory**（不解析、不剥离，query 原样进上游 URL），无投影。
  - `global_health`（:591-596）— GET `/slimapi/global/health`，同上。
  - `_strip_directory_query(request)`（:602-616）— B4 宽容剥离：`_strip_query_keys` 字节保原扫描去掉 `directory`（重复/编码保序）。
  - `session_context`（:619-630）— GET `/slimapi/session/{sid}/context`：strip directory 后 passthrough `/api/session/{sid}/context`，无投影。

- **12 路由逐 handler（directory 消费 / 上游 / 投影 / 缓存 / 记账桶）**：

  | # | 路由 | directory | 上游调用 | 投影 | 缓存/条件 | 桶（traffic.py） |
  |---|---|---|---|---|---|---|
  | 1 | `/file`（:149） | 消费（stash/header→403 门→canonical 转发） | `GET /file` + raw query | 无（raw） | ETag(rep v3 域)+gzip+no-store | `file`（:145-146） |
  | 2 | `/file/content`（:166） | 同 #1 | `GET /file/content` | 无 | 同上 | `file` |
  | 3 | `/file/status`（:178） | 同 #1 | `GET /file/status` | 无 | 同上 | `file` |
  | 4 | `/vcs`（:192） | 消费（`_resolve`，**无 403 门**） | `GET /vcs` | 无 | 同上 | `vcs`（:147-148） |
  | 5 | `/vcs/status`（:201） | 同 #4 | `GET /vcs/status` | 无 | 同上 | `vcs` |
  | 6 | `/vcs/diff`（:210） | 同 #4 | `GET /vcs/diff` | 无 | 同上 | `vcs` |
  | 7 | `/find/file`（:227） | 同 #4 | `GET /find/file` | 无 | 同上 | `find`（:149-150） |
  | 8 | `/config/providers`（:395） | 消费（无 403 门） | v3：`GET /config/providers` raw 透传；v4：§12 流水线 | v3=**零投影全字段**；v4=安全投影（顶层恰 `{providers,default}`；provider 保 `id/name/source/models`；model 保 `id/name/providerID/status/variants/limit`——`limit` 为 4.4.0 修订三恢复，子键白名单 `{context,input,output}` 逐子键 int-else-omit，任何上游形态不产生错误，providers_projection.py:332-355；表示域指纹 `providers-projection-v2`，旧 v4 ETag 自然失效） | v4：ETag=worker canonical bytes、全错误 no-store；v3：rep v3 域 | `providers`（`/slimapi/config/**` 全落，:151-152） |
  | 9 | `/session/{sid}`（:555） | 消费（无 403 门） | v3：上游 `/session/{sid}`+投影；v4：dbaux 点查优先，native `/session/{sid}` 回退 | v3=`skeleton_session` 白名单；v4=`canonical_session_skeleton_v4`（与 v4 列表同 projector，§13.3） | v3 分支 ETag；v4 分支 no ETag、200 no-store | `session_single`（GET，:184-185） |
  | 10 | `/api/session/active`（:582） | **宽容**（不解析不剥离，字节进上游 URL） | `GET /api/session/active` 逐字 | 无 | ETag+gzip | `session_active`（:186-187） |
  | 11 | `/global/health`（:591） | 宽容，同 #10 | `GET /global/health` 逐字 | 无 | ETag+gzip | `global_health`（:188-189） |
  | 12 | `/session/{sid}/context`（:619） | **宽容剥离**（`_strip_directory_query`，:627） | `GET /api/session/{sid}/context` | 无 | ETag+gzip | `session_context`（:180-181） |

  v3/v4 providers 分叉点：`config_providers`（:403-405）`wire_view_from_scope(request.scope) == 4 && _V4_PROVIDERS_REVISION_ACTIVE()`；两分支都先 `_resolve`（:402）。v4 的 `limit` 恢复不在本文件——在 `project_and_pack` 内（`providers_projection.py:332-355`），本文件只透传 `rep_version`（:278, :344）与 worker 结果。

- **依赖**：`config`（`allowlist_roots/candidate_canonical/match_allowlist`，:51）、`directory.validate_directory`（:52）、`dbaux`（:53-58）、`errors.CodedHTTPException`（:59）、`gzip_util`（`error_response/json_response`，:60）、`providers_projection`（:61-66）、`selector`（:67-72）、`skeleton`（:73-77）、`traffic.stash_up_in`（:78）、`transform`（`TransformBusy/read_with_cap`，:79）、`upstream_errors`（:80）、`_catalog_common`（`busy_response/stream_upstream`，:81）、`_read_passthrough`（:82-87）、`sessions._aux_unavailable`（:88）、`etag`/`readiness`（:49-50）。
- **被依赖**（rg 反查）：`app.py:29,760`（router include）；`write_groups.py:88` 共享 `_read_passthrough`；测试 `tests/test_read_groups.py`（:29,111,921-923 monkeypatch `_project_session` spy）、`tests/test_b4_new_routes.py:38,87`、`tests/test_b4_allowlist.py:17,54`、`tests/test_providers_projection_v4.py:50,218,999`、`tests/test_session_single_v4.py:43,180`、`tests/test_readiness_gating_integration.py:33,172`、`tests/test_terminal_matrix.py:39,98`。
- **状态/可变性**：模块级只读常量 `_SESSION_SINGLE_SQL`（:437，import 时物化）；`_V4_*_REVISION_ACTIVE` 每调用动态读 `readiness_mod.SATISFIED`（frozenset，运行期静态、测试可 monkeypatch）；无 per-request 可变路由状态；`_strip_directory_query` 就地改写 `request.scope["query_string"]`（:613-616，请求内、幂等）。
- **错误路径构造点**（逐点）：
  - `invalid_directory` 400 — 经 `_resolve` 的 `validate_directory`（:108/:111；实现在 directory.py:37/41/46/50：`..`/`.` 段、NUL、控制字符、>4096）。
  - `directory_not_allowed` 403 — `_authorized_file_directory`（:142）。
  - `_handle_providers_v4`：503 `upstream_unavailable`（:292-294 网络；:310-313 上游 5xx；:306-309 drain 失败/超 cap 重抛补 no-store；:327-330 读体网络错）；502 `provider_upstream_malformed`（:314-317 非 200 的 2xx 含 204；:351-352 worker 校验失败）；502 `upstream_http_{status}`（:318-320 3xx/4xx）；413 `response_too_large` + `limitBytes`（:331-334）；503 `transform_busy`（:346-350，busy body 补 no-store）；413 `provider_projection_limit` + `limit`/`limitValue`（:353-356；四限额 256/1024/64/8MiB，providers_projection.py:54-57）。
  - `_session_single_native_fallback`：503 `upstream_unavailable`（:473-474 网络；:477-480 5xx；:493-494 读体错；:504-506 malformed）；上游 4xx 逐字（:481-488）；413 `response_too_large` + `limit`（:495-500）；503 `transform_busy`（:509-510）；503 `auxiliary_unavailable`（:511-513，`Retry-After` 由 sessions.py 助手带）。
  - `_handle_session_single_v4`：404 `session_not_found` + `sessionID`（:535-537）；503 `auxiliary_unavailable`（:533 sqlite3.Error fail-closed；:541 坏 JSON 列行；:546 required 不可表示）。
  - v3 共享链错误（`_read_passthrough.read_passthrough_get`，:194-243）：503 `upstream_unavailable`（网络/5xx/投影 ValueError）、上游 4xx status+body 逐字（`_read_passthrough.py:207-218`）、413 `response_too_large` + `limit`（:225-230）、503 `transform_busy`（投影路由 only，:242-243）。
- **疑问点（12）**：
  1. :93-112 `_resolve` 的 `directory` 形参是**死参**——:106 恒传 `None` 给 `resolve_route_directory`，各 handler 声明的 `directory` 形参（:151/:168/:180/…）从不被消费。生产栈 selector 必在（stash 命中）无影响；但 selector-less 测试栈下 `?directory=` 被 FastAPI 绑定后**静默丢失**，只剩 header 通道。是否有意？
  2. :123-143 403 allowlist 门只覆盖 file 组 3 路由（:159/:171/:182）；vcs/find/providers/session_single（:197/:206/:220/:240/:574）**不设防**——配置 `directory_allowlist` 后仍可经 `/slimapi/vcs?directory=…`、`/slimapi/find/file?…&directory=…`、`/slimapi/vcs/diff` 读取未授权工作区（vcs diff 同样回传文件内容）。是 B4 设计只封文件组还是遗漏？
  3. :582-596 宽容路由 active/health **不剥离** `?directory=`——directory 字节原样进上游 URL（`_read_passthrough.py:103-116`）；"directory tolerant" 完全依赖上游忽略未知 query。与 B4 context 的显式剥离策略（:602-616）不一致；若上游 workspace-routing 中间件读 query directory，行为可能漂移。
  4. :471-472/:573/:629 `sid` 未做词法校验直接 f-string 拼进上游 URL（`f"/session/{sid}"` 等）——FastAPI 单段匹配排除裸 `/`，但 `%2F`/`?`/`#` 等编码字节经 httpx `build_request` 的再编码行为与上游路径解析面需核实（潜在 path 参数穿越/改道）。
  5. :210-232 类型化声明（`context: int`、`limit: int`）使 `?limit=abc` 在 **sidecar 侧 422**，而 docstring（:213-217/:233-237）宣称 raw query verbatim、校验面属上游——类型化形参引入了 sidecar 侧 422 面，与 §5.2 逐字转发纯度有偏差（值未转发，仅校验存在与类型）。
  6. :331-334 v4 providers 413 用字段名 `limitBytes`，而 :495-500（native 回退）与 v3 共享链（`_read_passthrough.py:229`）同码 `response_too_large` 用 `limit`——同名错误两种字段名并存（各自契约冻结，客户端/审计需注意）。
  7. :314-317 v4 把非 200 的 2xx（含 204）判 502 `provider_upstream_malformed`，而 v3 链是成功状态逐字透传（`_read_passthrough.py:274-277`）——同一上游 204 在 v3/v4 两面语义完全相反；需确认上游 `/config/providers` 实际可能的状态域。
  8. :520-552 v4 session single 的 dbaux 点查**完全不使用 directory**（也不设 403 门），native 回退却转发 directory——同一路由两数据源 directory 语义不对称；等价性依赖"sid 全局唯一"这一上游不变量。
  9. :437 `_SESSION_SINGLE_SQL` import 时物化，列名与上游 SQLite schema 的同步漂移仅表现为 `sqlite3.Error → 503 fail-closed`（:531-533，无细节观测）——排障盲区。
  10. :382-392 门控回落无信号：若 `SATISFIED` 收缩，`?v=4` 请求**静默回落 v3 行为**（providers 透传而非投影、session single 走 v3 skeleton），客户端只能从响应字段面/ETag 域差异间接分辨。
  11. :109-111 header 兼容通道 `x-opencode-directory` 与 selector 的 header 处理（selector.py:331/:456）并存——双通道（selector 消费 + 路由兜底直读）是否存在重复消费或绕过 selector 验证的面（本路由会再 `validate_directory`，但 selector 的多值/双通道错误语义不会触发）？
  12. :23 模块 docstring 上游锚点写 "opencode v1.18.16"，AGENTS.md 当前对齐版本为 v1.18.18——文档漂移（不影响行为，审计溯源需注意）。

---

### src/oc_slimapi/routes/permissions.py（526 行）

- **职责**：`GET /slimapi/permissions` —— 跨目录聚合 pending permission 卡片（ocdroid slim 模式冷启动恢复路径）。上游 `GET /permission` 是 per-Location（`X-Opencode-Directory` 路由，无 header 回落 `process.cwd()`），本端点两阶段 fan-out：`GET /experimental/session?roots=true&archived=true&limit=10000` 发现 distinct workdir → 对每 dir `GET /permission`（带 directory header）合并为单一 envelope。结构逐行镜像 questions.py（共享 sliding-window scheduler 形状）；差异点：per-dir 响应做 `PermissionV1.Request` **7 字段白名单投影**（:79-81，:417-422），questions 是 verbatim。

- **对外符号**：
  - `router`（:25）— `APIRouter(prefix="/slimapi", tags=["permissions"])`；`app.py:29,760` include。
  - `_DirFetchFailure`（:28-39）— per-dir 上游失败异常（`__slots__=("code",)`），在共享 flight factory 内抛出使 flight 失败（预算即时退款、无负缓存），joiner 各自隔离进 `errors[]`。
  - `_MAX_AGGREGATE_ITEMS = 10_000`（:46）— P1-28 聚合 item 上界（第二层 cap，模块常量，**无 env knob**）。
  - `_directories_from_sessions(sessions_payload)`（:49-65）— 从 session 真实 `directory` 字段派生 distinct workdir（首见保序 `dict.fromkeys`；跳过非 dict/非 str/空值）；返回 caller-owned 列表。
  - `permissions(request)`（:84-289）— 主 handler：Step1 发现（coalesce LEVEL 1：固定 key `("discovery", id(client), _DISCOVERY_LIMIT)` 共享 flight，joiner 在 lease 内 parse + 派生目录串后 `del` 图与 raw body，:186-223；预算满 bypass → 直连 :216-223；非 coalesce 直连 :224-229）；`qp_sweep` shadow 活动上报（:234-236）；Step3 fan-out（:249-256，`permissions_fanout`/`permissions_max_response_bytes`/`permissions_max_aggregate_bytes` + `item_cap`）；Step4 authoritative 规则（:265-269：`not errors and discovery_complete and not truncated` → None，否则 succeeded 列表）；envelope + truncated（:270-277）；序列化 offload 到 transform executor（**无 admission**，:282-286）。
  - `_pack_permissions_envelope(envelope, *, accept_encoding)`（:292-303）— worker 入口：`orjson.dumps` + `compress_if_beneficial`。
  - `_fetch_permissions_for_dir(upstream_client, request, directory, *, cap, registry=None)`（:306-423）— 单 dir 拉取：`_raw()` 在 `app.state.permissions_semaphore` 内流式 GET `/permission`（:346-384，cap 保护读 + traffic stash + finally `aclose`）；共享 flight key `("permission-dir", id(client), directory)`（:390-394），bypass → 直连；解析/白名单投影/`directory` 盖章在 caller 侧（共享仅 raw body）；失败统一返回 `([], code, 0)`，不抛（CancelledError 除外）。
  - `_collect_with_byte_budget(...)`（:426-526）— 滑窗调度器（与 questions.py 逐字节同构）：至多 `concurrency` in-flight、严格 index 序消费、aggregate raw bytes + item 计量、预算触发 → `truncated=True` + 取消未消费 task + `gather(return_exceptions=True)` + break（:498-506）；`except Exception` 折叠为 per-dir `upstream_unavailable`（:482-489）；finally 再取消/await 未消费 task（:518-524）。

- **fanout 并发与聚合预算**：窗口 = `config.permissions_fanout`（默认 8，env `OC_SLIMAPI_PERMISSIONS_FANOUT`，config.py:614-616；1-16 校验 :1086）；跨请求全局并发 = `app.state.permissions_semaphore`（app.py:407-408）；per-dir cap = `permissions_max_response_bytes`（默认 2MiB，config.py:611-613）；聚合字节上界 = `permissions_max_aggregate_bytes`（默认 16MiB，config.py:617-619，须 ≥ per-dir 且 ≤128MiB）；item 上界 = `_MAX_AGGREGATE_ITEMS`=10,000（硬编码）。预算按 **raw body 字节**（`read_with_cap` 的 `total`）记账，非投影后字节。

- **依赖**：`discovery`（`_DISCOVERY_LIMIT`/两个 fetch，:10-14）、`gzip_util.compress_if_beneficial`（:15）、`traffic.stash_up_in`（:16）、`transform.read_with_cap`（:17）、`upstream.forward_directory_headers`（:18）、`upstream_errors`（:19-23）。app.state：`upstream`/`config`/`raw_fetch_registry`/`qp_sweep`/`permissions_semaphore`/`transforms`。
- **被依赖**（rg 反查）：`app.py:29,760`；测试 `tests/test_permissions.py:30`、`tests/test_questions_coalesce.py:33`（与 questions 合测 coalesce）。
- **状态/可变性**：无模块可变状态；`_MAX_AGGREGATE_ITEMS` 常量；per-request 局部 `tasks` dict；共享 flight 生命周期由 registry 管理（joiner 在 lease 内 `del` 引用，:210-215）。
- **错误路径构造点**：
  - 发现阶段（经 `discovery.fetch_global_root_sessions[_raw]`，discovery.py:113-200）：网络/4xx/5xx/坏 JSON/非 list/超 cap → 一律 503 `upstream_unavailable`（total failure，无 envelope；joiner 侧防御性重校验 :201-206）。
  - `_raw()`：send `httpx.RequestError` → `_DirFetchFailure(UPSTREAM_UNAVAILABLE)`（:356-357）；`status>=400` → cap 保护 drain（**返回值被忽略**，:365-368）→ `_DirFetchFailure(upstream_error_code_for_status(status))`（:369-370；5xx→`upstream_unavailable`、4xx→`upstream_http_N`）；读体网络错（:381-382）/ body None 超 cap（:378-379）→ `_DirFetchFailure(UPSTREAM_UNAVAILABLE)`。
  - 解析：坏 JSON（:412-414）/ 非 list（:415-416）→ `([], UPSTREAM_UNAVAILABLE, 0)`。
  - `_collect_with_byte_budget`：worker 任意异常 → per-dir `errors[] {directory, code:"upstream_unavailable"}`（:486-489）；per-dir 失败 → `errors[]`（:492-497）；预算触发 → truncated（不进 errors）。
  - total failure 503 `upstream_unavailable`（文档 :146-147；由 discovery 助手 raise）。
- **疑问点（10）**：
  1. :149 与 :164 `upstream_client = request.app.state.upstream` **重复赋值**（:164 覆盖 :149）——questions.py 复制残留，无行为影响，纯代码卫生。
  2. :105 docstring "no `X-Slimapi-Version` bump (still 2)" **过时**——该 header 已于 3.0.0 删除，当前 wire 版本 (3,4)；误导审计与维护者。
  3. **记账桶缺失**：`/slimapi/permissions` 在 `traffic.py bucketize`（:91-192）无专属分支 → 落 `other` 桶（:190），而 questions 有专属 `questions` 桶（:135-136）——`/slimapi/metrics.traffic` 无法单独观测 permissions 流量，与 questions 不对称。
  4. :482-489 `except Exception as exc: outcome = exc` 把 worker **任意异常**（含编程 bug、offload 基础设施错误）折叠为该 dir 的 `upstream_unavailable`——上游故障与 sidecar 故障不可区分、无日志。
  5. :498-506 预算触发分支 `await asyncio.gather(*tasks.values(), ...)` gather 全部 tasks（含已完成），与 finally 清理（:518-524）重复；且当前目录已完整拉回的 body 被整体丢弃（无部分并入），丢失粒度 = 整目录（设计声称如此，docstring :449）。
  6. :417-422 白名单投影丢弃未知字段与非 dict 元素均**无观测信号**（不进 `errors[]`、不打日志）——上游 schema 加字段时客户端静默不可见，直到白名单更新。
  7. :46 `_MAX_AGGREGATE_ITEMS` 硬编码（与三个 env 化 knob 不对称，ops 不可调）；且 docstring :44-45/:279-281 称聚合 "memory-bounded"——预算记的是 raw 传输字节，10k items × Python dict 驻留（对象膨胀）实际内存上界显著大于 16MiB，声称不严格。
  8. :365-368 错误体 drain 的 `read_with_cap` 返回值被忽略（超 cap 静默截断 drain）——与 read_groups `_read_error_body` 的超 cap → 503 策略不同（这里 per-dir 降级为连接不复用，无错误），是有意省略还是遗漏值得确认。
  9. :344 `config = request.app.state.config` 仅被 :393 `reserve_bytes` 使用——非 coalesce 直连路径也要求该属性存在（测试直挂 router 需注意；questions.py 同构但无此重复赋值问题）。
  10. :67-74 上游形状注释锚定 "opencode v1.18.16"（AGENTS.md 当前对齐 v1.18.18）——同 read_groups 的文档漂移问题。

---

### src/oc_slimapi/routes/questions.py（504 行）

- **职责**：`GET /slimapi/questions` —— 跨目录聚合 pending question（修复 slim 模式冷启动：pending question 在 `workdir ≠ process.cwd()` 时不可见）。与 permissions.py 同构：两阶段 fan-out（`/experimental/session?roots=true&archived=true` 发现 → 每 dir `GET /question` 合并）。差异点：per-dir 条目 **verbatim** 合并（`{**entry, "directory": dir}`，:399-402），无白名单投影；fanout knob 名为 `questions_fanout_concurrency` / `questions_semaphore`；共享 flight key 前缀 `("question-dir", ...)`。

- **对外符号**：
  - `router`（:25）— `APIRouter(prefix="/slimapi", tags=["questions"])`；`app.py:29,760` include。
  - `_DirFetchFailure`（:28-39）— 与 permissions.py 同款（独立类，非共享导入）。
  - `_MAX_AGGREGATE_ITEMS = 10_000`（:47）— P1-28/T5-C10 聚合 item 上界。
  - `_DISCOVERY_LIMIT`（:10 导入，:49-52 注释：按名引用使测试可 monkeypatch 本模块绑定）。
  - `_directories_from_sessions(sessions_payload)`（:55-75）— 与 permissions.py 逐字节同逻辑的 distinct 目录派生。
  - `questions(request)`（:78-272）— 主 handler：Step1 发现 + coalesce LEVEL 1（固定 key `("discovery", id(client), _DISCOVERY_LIMIT)`，与 permissions **共享同一 key**，:170-208；joiner lease 内 parse + `del` 图，:181-200；bypass :201-208；直连 :209-214）；`qp_sweep` 上报（:218-220）；Step3 fan-out（:231-238：`questions_fanout_concurrency`/`questions_max_response_bytes`/`questions_max_aggregate_bytes`/item_cap）；Step4 authoritative（:247-251）+ envelope（:252-259）；offload 序列化（无 admission，:265-269）+ Response（:270-272）。
  - `_pack_questions_envelope(envelope, *, accept_encoding)`（:275-286）— worker 入口：dumps + `compress_if_beneficial`（小/不可压跳过 gzip）。
  - `_fetch_questions_for_dir(upstream_client, request, directory, *, cap, registry=None)`（:289-403）— 单 dir：`_raw()` 在 `questions_semaphore` 内流式 GET `/question`（:328-366）；共享 flight `("question-dir", id(client), directory)`（:372-376）；解析（:393-398）→ verbatim 盖章（:399-402）。
  - `_collect_with_byte_budget(...)`（:406-504）— 滑窗调度器（permissions.py 的镜像源）。

- **fanout 并发与聚合预算**：窗口 = `questions_fanout_concurrency`（默认 8，env `OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY`，config.py:596-598；1-16 校验 :1064）；全局信号量 `app.state.questions_semaphore`（app.py:401-402）；per-dir cap = `questions_max_response_bytes`（默认 2MiB，config.py:590-592）；聚合字节上界 = `questions_max_aggregate_bytes`（默认 16MiB，:593-595；须 ≥ per-dir、≤128MiB，:1072-1077）；item 上界 = 10,000（硬编码）。

- **依赖**：同 permissions.py（discovery/gzip_util/traffic/transform/upstream/upstream_errors）；app.state 额外 `questions_semaphore`。
- **被依赖**（rg 反查）：`app.py:29,760`；测试 `tests/test_questions_routes.py:23`、`tests/test_questions_coalesce.py:33`。
- **状态/可变性**：无模块可变状态；`_DISCOVERY_LIMIT` 为 import 绑定（monkeypatch 面）；per-request `tasks` dict。
- **错误路径构造点**：
  - 发现：网络/4xx/5xx/坏 JSON/非 list/超 cap → 503 `upstream_unavailable`（total failure，无 envelope；discovery.py:165-176 映射；joiner 防御重校验 :187-191）。
  - `_raw()`：send 网络错 → `_DirFetchFailure(UPSTREAM_UNAVAILABLE)`（:338-339）；`status>=400` → cap 保护 drain（返回值忽略，:347-350）→ `_DirFetchFailure(upstream_error_code_for_status(status))`（:351-352）；读体错（:363-364）/超 cap body None（:360-361）→ `UPSTREAM_UNAVAILABLE`。
  - 解析：坏 JSON（:394-396）/非 list（:397-398）→ `([], UPSTREAM_UNAVAILABLE, 0)`。
  - 调度器：任意 worker 异常 → per-dir `upstream_unavailable`（:463-467）；per-dir 失败 → `errors[]`（:470-475）；预算触发 → `truncated:true`（:476-484，不进 errors）。
  - total failure 503（docstring :139-140）。
- **疑问点（9）**：
  1. :90 docstring "no `X-Slimapi-Version` bump (still 2)" 过时（同 permissions）。
  2. :399-402 `{**entry, "directory": directory}` verbatim 合并——上游 entry 若自带 `directory` 键会被**覆盖**；且 docstring :91-93 承诺字段序（id/sessionID/questions/tool/directory）实际依赖上游输出序，orjson 保序成立但无强制。
  3. :460-467 `except Exception` 折叠任意 worker 异常为 `upstream_unavailable`（同 permissions 疑问 4）。
  4. :47 item 上界硬编码 10,000；且 **per-dir 无 item 数限制**（仅 2MiB byte cap）——单目录一条小 entry 海量数组可一次性冲爆 item_cap（防护正确触发，但说明第一层 cap 粒度不齐）。
  5. :394 `orjson.loads(body)` 在**事件循环内**解析（≤2MiB per dir；docstring :260-264 只强调序列化 offload）——解析 CPU 不 offload，与"不阻塞事件循环"的动机不完全一致（短暂阻塞，量级小）。
  6. :347-350 错误体 drain 返回值忽略（同 permissions 疑问 8）。
  7. :476-484 预算触发 gather 全部 tasks 与 finally（:496-502）重复；当前目录整丢（同 permissions 疑问 5）。
  8. :49-52 注释宣称 monkeypatch `questions._DISCOVERY_LIMIT` 可测截断路径——该绑定同时进入 flight key（:177），monkeypatch 后 key 随之变（正确），但 **permissions.py 无对应注释却共享同一 key 元组**（`("discovery", id(client), _DISCOVERY_LIMIT)`）——两个模块各自 import 的 `_DISCOVERY_LIMIT` 若被单独 monkeypatch，同 key 假设破裂、coalesce 跨端点共享失效（仅测试面风险）。
  9. :88 上游锚点 "opencode v1.18.16"（AGENTS.md 当前 v1.18.18）——文档漂移（同前两文件）。

---

## 汇总

| 文件 | 行数 | 路由数 | 疑问点数 |
|---|---|---|---|
| src/oc_slimapi/routes/read_groups.py | 630 | 12 | 12 |
| src/oc_slimapi/routes/permissions.py | 526 | 1 | 10 |
| src/oc_slimapi/routes/questions.py | 504 | 1 | 9 |

跨文件共性主题：① permissions/questions 双胞胎代码（`_DirFetchFailure`/`_directories_from_sessions`/`_collect_with_byte_budget` 三处近似复制，未抽共享模块，漂移风险已现——permissions 多一处重复赋值）；② `except Exception` 折叠掩盖 sidecar 自身故障；③ 记账不对称（permissions 无专属 traffic 桶）；④ docstring 版本陈述过时（X-Slimapi-Version、opencode v1.18.16）；⑤ read_groups 403 门覆盖面（仅 file 组）与宽容路由 directory 字节转发策略一致性。
