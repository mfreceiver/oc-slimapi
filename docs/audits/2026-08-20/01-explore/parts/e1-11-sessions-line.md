# E1-11 精读卡片：sessions 列表线（sessions.py / _read_passthrough.py / _catalog_common.py）

> 审计探索卡片，2026-08-20。行号以当前工作树为准；引用格式 `src/oc_slimapi/...:行号`。

---

### src/oc_slimapi/routes/sessions.py（883 行）

- **职责**：`/slimapi/sessions`（v3 上游省流列表 + v4 dbaux DB 投影 facade）与 `/slimapi/sessions/status`（上游状态 map + TurnRegistry merge）两个读端点；含 coalesce（raw-fetch lease）共享工厂、v3/v4 双面响应尾、v4 降级矩阵与 §13/§15 readiness 门控。

- **对外符号**（逐函数）：
  - `router` :43 — `APIRouter(prefix="/slimapi", tags=["sessions"])`；注册于 `app.py:760-761`。
  - `_canonical_sessions_query(limit, roots, start, search)` :55-65 — lease key 的确定性排序查询串；directory 是独立 key 分量（既是 query param 又是路由 header，:58-59 注释）。
  - `_fetch_sessions_raw(request, params, directory, *, cap)` :68-89 — 共享工厂体：**一次**上游 `GET /session`（stream=True）+ cap-read（`read_upstream_response`）；`httpx.RequestError` → `raise_upstream_unavailable`（:80-81）；finally `aclose`（:88-89）。
  - `_finalize_sessions_response(request, sessions, limit, accept_encoding)` :92-138 — v3 两路径（lease/direct）共用响应尾：`complete = len(sessions) < limit`（:115）→ `sessions_envelope_payload`（:118）→ `rep=None`（ETag 关）时无 ETag、`Vary: Accept-Encoding` 单值（:119-124）；否则 identity `orjson.dumps` → coding 派生 → `compute_etag` → `conditional_304`（弱比较）→ 200 带 `ETag`/`Vary`（:125-138）。`X-Complete` header 永不发出（§1 退役，:110-113）。
  - `_sessions_via_lease(...)` :141-198 — join-first lease 路径（`raw_fetch_registry.fetch_or_bypass`，key = `("sessions-list", id(upstream), directory, canonical_query)` :155-162）；budget 满 → 返回 `None` 由调用方走 direct（:163-164）；lease 内 `body is None` → 413 `response_too_large`（:167-171）；**调用方自己的 admission**（`async with pool` :184）内 parse + list/元素 guard（:186-192）+ offload `_project_sessions`（:193）；`TransformBusy` → `busy_response`（:194-195）；出 lease 后 `_finalize_sessions_response`（:198，X-Complete/ETag per-caller）。
  - `_fetch_status_raw(request, params, directory)` :201-219 — 一次上游 `GET /session/status`（**非流式** `upstream.get`，一次性缓冲 `response.content`）；`stash_up_in`（:214）→ `raise_for_status` → `raise_upstream_status`（:215-218）。
  - `_status_via_lease(request, registry, directory)` :222-260 — status 的 lease 路径；turn merge 刻意在 factory **外** per-caller（:225-228 注释：turn 状态随时间变化不能冻结在 factory）；lease 释放后 parse（bytes 不可变，:241-248）→ TurnRegistry merge（:250-256）→ `json_response`（:257-260）。
  - 常量：`_V4_ARCHIVED_STATES = ("omit","only","all")` :271；`_V4_PARENT_RESERVED = ("all","none","only")` :272（**定义未使用**，见疑问 11）；`_V4_LIMIT_MAX = 500` :273（v3 保持 1000）；`_AUX_RETRY_AFTER = "30"` :274；`_AUX_UNAVAILABLE_HINT` :276-279。
  - `_raw_query_keys(request)` :282-296 — post-selector-strip 原始 query key 集合；presence-based（空值也算），`keep_blank_values=True`，latin-1 解码（:291-294）。
  - `_aux_unavailable()` :299-306 — 503 `auxiliary_unavailable` 工厂（flat body，无 DB 细节泄露，:300；`Retry-After: 30`）。
  - `_fail_closed_503(request)` :309-317 — 503 fail-closed 统一出口；额外写 `request.state.slimapi_degraded_503 = True`（:316，R2 在 access log/snapshot/metrics 消费，`traffic.py:250` 注释呼应）；Class A 降级 200 不置位（那是 `slimapi_sessions_source="http"` 辖区，:312-315）。
  - `_http_session_to_v4(item)` :320-354 — 降级路径投影：上游 HTTP `SessionInfo` camelCase → `SessionSkeletonV4`；`project: null`（:353，HTTP 形态无 project join）；tokens 嵌套→拍平列名（:341-349）；time/summary/revert 子对象按键存在性挑选（:335-352）。
  - `_v4_allowlist_entries(request)` :357-368 — config `directory_allowlist` 三态归一：`None`/`[]` → `()`（无轴）；滤掉空串项（config noise）。
  - `_sessions_v4(...)` :371-568 — v4 列表主体（分段见下）。
  - `_V4_REPRESENTATION_FEATURE = "representation.vary.v4"` :571；`_V4_SESSION_SINGLE_FEATURE = "session.single.projection.v4"` :573。
  - `_v4_session_single_revision_active()` :576-585 — §3.3 门控：`session.single.projection.v4 ∈ readiness_mod.SATISFIED` 时 §13 修订面生效；**调用时动态读模块全局**（readiness 翻转无需改本文件，:580-582）。
  - `_v4_representation_revision_active()` :588-595 — 同款 §15 门控（`representation.vary.v4`）。
  - `_v4_json_response(payload, request)` :598-651 — v4 200/304 响应尾（DB 投影与 Class A 共用，调用点在判定**之后**、ETag 管线不短路，:601-603）：门控关 → 无 ETag 且 `del response.headers["Vary"]`（:618-623，4.0.0 冻结态）；门控开 → `Vary: Accept-Encoding` 恒在 + `Cache-Control: no-store` + ETag（`wire_view=4` 域隔离，:625-651）；`rep=None`（ETag env 关）→ 无 validator/无 304 但 Vary/no-store 仍发（:628-633）。
  - `_project_http_sessions_v4(payload)` :654-656 — worker：降级 items 投影（4.0.0 形态）。
  - `_project_http_sessions_v4_canonical(payload)` :659-675 — worker：§13 fallback → `native_session_to_record` 归一化后过唯一 canonical projector（`fallback=True`）；任一 item 不可表示 → `_aux_unavailable()` 503 整响应 fail-closed（:672-673）。
  - `sessions(...)` :678-798（`@router.get("/sessions")` :678）— GET `/slimapi/sessions` 路由（v3/v4 分叉点 :693）。
  - `_project_sessions(payload)` :801-803 — worker：`[skeleton_session(item) for item in payload]`（无副作用）。
  - `sessions_status(request, directory)` :806-880（`@router.get("/sessions/status")` :806）— GET `/slimapi/sessions/status`。

- **GET /slimapi/sessions v3 面逐段**（:693 起为 direct path；lease 面见上）：
  1. :693-697 `wire_view_from_scope(request.scope) >= 4` → 转 `_sessions_v4`（v4 分叉点；`directory` 在 v4 由 selector 退役，pre-route 拦截，:690-692 注释）。
  2. :700-705 v3 × v4-only 参数 presence 检查：`_raw_query_keys & {"archived","parent","cursor"}` 非空 → 422 `param_version_mismatch`（hint `["archived", ...] are v4-only parameters`）。
  3. :708-712 `resolve_route_directory`（scope stash 恢复被 selector 剥掉的 `?directory=`）→ `validate_directory`（不再 gate，规范化后转发，上游决定可否服务）。
  4. :713-721 params 组装：`limit`/`roots`/`start`/`search`；directory **仅**以 `X-Opencode-Directory` header 走上游（:746 `forward_directory_headers`；§5.2 terminal，:714-717 注释）。
  5. :726-736 lease 尝试（`registry is not None and config.coalesce_enabled`）；`leased is not None` → 直接返回；budget 满 → 落 direct。
  6. :737-793 direct：`async with request.app.state.transforms as pool`（admission BEFORE 上游 GET，:738）→ stream GET `/session`（:743-751；`RequestError` → 503）→ `read_upstream_response`（:757-761；4xx → 502 `upstream_http_N`，无 sid → 404 也是 `upstream_http_404`，:753-756 注释）→ `body is None` → 413（:762-766）→ `orjson.loads` + 非列表 guard（:771-778，v6 §1.1：防 dict/string 被静默迭代成空 200）+ 标量元素 guard（:779-783）→ offload `_project_sessions`（:786-789）→ finally `aclose`（:790-791）；`TransformBusy` → `busy_response`（:792-793）。
  7. :796-798 `_finalize_sessions_response`（envelope + ETag/304 per-caller）。

- **GET /slimapi/sessions v4 面（`_sessions_v4`）逐段**：
  1. ④ 参数版本不匹配（§8.3 先于 invalid_cursor/503）：`roots`/`start` **presence**（任何值含非法值）→ 422（:384-388）；`limit > 500` → 422（:389-393）；`archived` 非法值 → 422（:394-398）；`parent` 空串 → 422（:399-402）。
  2. :404-407 `archived_state = archived or "omit"`、`parent_state = parent or "all"`、`normalized_search(search)`、`_v4_allowlist_entries`。
  3. ⑤ invalid_cursor 400 优先于 503（纯内存校验）：`build_fingerprint(archived, parent, search, allowlist)`（:410-412）→ `decode_cursor` 抛 `InvalidCursorError` → 400 `invalid_cursor`（malformed，:413-419）→ `fingerprint_mismatch` → 400 `invalid_cursor`（filter context 不匹配，:420-427）。
  4. ⑥ DB 路径：`dbaux.status().available`（:430-431）→ `fetch_sessions_page(dbaux, archived=, parent=, search=, cursor=(t,i), limit=, allowlist=)`（:433-442）；`AuxiliaryUnavailableError` → `pass` 落入降级矩阵（status/query 间竞态，:443-444）；`sqlite3.Error` → `_fail_closed_503`（rev gate BLOCKER-1：busy 族查询期异常统一转 503，不泄 SQLite 细节，:445-450）；成功 → §13 门控开时 items 逐条 `canonical_session_skeleton_v4`，任一 `None` → `_fail_closed_503`（不可表示项不混入，:452-462），门控关时 `project_rows_to_v4_skeletons`（4.0.0 逐字节，:463-465）→ `nextCursor` 用**原始窗口锚点**（`page.anchor`，items 可空仍可前进；仅 `not page.complete and page.anchor is not None` 时编码，:468-472，BLOCKER-3）→ `request.state.slimapi_sessions_source = "db"`（:473）→ envelope `degraded = any(item["degraded"])`（门控开才求值；门控关稀疏形态短路，:479-487）。
  5. **降级矩阵**（DB unavailable 或竞态落入，:488-502，按序全 fail-closed 503）：`allowlist` 非空 → 503（白名单 ⊆ 结果集不可由上游保证，:489-491）；`has_wildcard(normalized)` → 503（`%`/`_`/`\` 过滤语义永不降级，:492-494）；`cursor_payload is not None` → 503（上游单键 cursor 无法兑现 (t,i) keyset，:495-497）；`class_a = archived_state ∈ ("omit","all") and parent_state ∈ ("all","none")`（:498-500）；非 class_a → 503（:501-502）。
  6. **Class A 200 + degraded:true**（HTTP 降级，v3 调用形态复制，:504-568）：`params = {"limit": limit}`，`parent=="none"` → `params["roots"]="true"`（§4.2 透传，:505-507），`normalized is not None` → `params["search"]=normalized`（:508-509）；pool admission（:512）→ stream GET `/session`（`forward_directory_headers(None)`——v4 global facade 恒无 directory，:514-520）→ cap-read（:524-528）→ 413（:529-533）→ parse + list/元素 guard（:534-541）→ §13 开：offload `_project_http_sessions_v4_canonical`，否则 `_project_http_sessions_v4`（:542-551）→ finally `aclose`（:552-553）；`TransformBusy` → `busy_response`（:554-555）；`complete = len(items) < limit`（best-effort，上游无 LIMIT+1 窗口，:556-558）；`slimapi_sessions_source = "http"`（:560）；`_v4_json_response(envelope degraded=True, nextCursor=None)`（降级页无法 (t,i) keyset 续读，cursor → 503，:561-568）。

- **GET /slimapi/sessions/status（:806-880）**：docstring 明示 directory 可选、上游 handler 无参、恒返回全局 map（:817-828）；:832-834 stash 恢复 + `validate_directory`；:838-843 lease 尝试（`coalesce_enabled`）；:844-856 direct：`upstream.get("/session/status", params={}, headers=...)` → `RequestError` → 503（:850-851）→ `stash_up_in`（:852）→ `raise_for_status` → `raise_upstream_status`（:853-856）；:857-864 `response.json()` 异常 → 503，非 dict → 503（:861-864）；:870-876 TurnRegistry merge（每 sid 写 `turnIncarnation`/`turn`；registry 缺席则两字段全省略 = ocdroid Tier-2 降级；非 dict entry 原样透传）；:877-880 `json_response`（**无 ETag/无 Cache-Control 控制**）。

- **依赖**（import，:10-41）：`dbaux`（`AuxiliaryUnavailableError`/`build_sessions_query`/`fetch_sessions_page`/`has_wildcard`/`normalized_search`）、`dbaux.cursor`（`InvalidCursorError`/`build_fingerprint`/`decode_cursor`/`encode_cursor`/`fingerprint_mismatch`）、`directory.validate_directory`、`envelope.sessions_envelope_payload/v4`、`errors.CodedHTTPException`、`etag`、`readiness`、`gzip_util.accepts_gzip/json_response`、`selector.resolve_route_directory/wire_view_from_scope`、`skeleton.canonical_session_skeleton_v4/native_session_to_record/project_rows_to_v4_skeletons/skeleton_session`、`traffic.stash_up_in`、`transform.TransformBusy/read_with_cap`、`upstream.forward_directory_headers`、`upstream_errors.raise_upstream_status/raise_upstream_unavailable`、`_catalog_common.busy_response/read_upstream_response`。
- **被依赖**（rg 反查）：`app.py:29,760-761`（router 注册）；`routes/read_groups.py:88` `from .sessions import _aux_unavailable`（跨模块取私有工厂）；`traffic.py:250` 注释引用 `_sessions_v4` 写 `slimapi_degraded_503`；测试 `tests/test_sessions_routes.py:10`、`tests/test_sessions_v4_matrix.py`、`tests/test_degraded_observability.py`、`tests/test_upstream_error_boundary.py:6`。

- **状态/可变性**：模块级仅常量 + `router`；无跨请求可变状态。请求级状态写点：`request.state.slimapi_degraded_503`（:316）、`request.state.slimapi_sessions_source`（:473 db / :560 http）；`_status_via_lease`/`sessions_status` 对**每调用方自有** payload dict 做 merge（:252-256/:872-876），共享的只是不可变 bytes（:242 注释）。readiness 门控为请求时动态读模块全局（:585/:595）。

- **错误路径**（构造点逐点）：
  - `response_too_large` 413：:168-171（lease body None）、:529-533（v4 Class A cap 超）、:762-766（v3 direct cap 超）。
  - `upstream_unavailable` 503：:81/:188/:190-192（lease 路径 RequestError/parse/非列表）、:213/:246/:248（status RequestError/parse/非 dict）、:450 之前的 :522/:537/:541（v4 Class A RequestError/parse/非列表）、:751/:770/:778/:783（v3 direct 同族）、:851/:860/:864（status direct 同族）。
  - `upstream_http_N` 502 族：经 `raise_upstream_status`（:218、:856）与 `read_upstream_response` 内 `raise_upstream_status_code`（v3 列表无 sid → 404 报 `upstream_http_404`，:753-756 注释）。
  - `transform_busy` 503：经 `busy_response`（:195、:555、:793）。
  - `param_version_mismatch` 422：:385-388（roots/start presence）、:390-393（limit>500）、:395-398（archived 非法）、:400-402（parent 空串）、:702-705（v3 × archived/parent/cursor presence）。
  - `invalid_cursor` 400：:416-419（malformed）、:423-427（fingerprint mismatch）。
  - `auxiliary_unavailable` 503：`_aux_unavailable` :301-306；触发点 = `_fail_closed_503` :317 的调用处：:450（sqlite3.Error）、:461（canonical 不可表示 item）、:491（allowlist）、:494（wildcard）、:497（cursor）、:502（非 class_a）；另有 :673（canonical fallback worker 内直接 `_aux_unavailable`，**不置** `slimapi_degraded_503` 观测位——与 `_fail_closed_503` 的差异，见疑问 12）。
  - `directory_*` 族：本文件**不构造**——v3 多值/冲突/header 退役与 v4 `directory_retired_in_v4` 均在 selector 层（`selector.py:206/:291`）；本文件只调 `validate_directory`（:712/:834）。

- **疑问点**（12）：
  1. :12 `build_sessions_query` 导入后**文件内零使用**（rg 全仓仅 tests 从 `dbaux` 直接导入）——dead import，确认后可清理。
  2. limit/offset…边界双轨：:683 `limit: int = Query(100, ge=1, le=1000)` 为 v3/v4 共用签名。v4 时 `limit ∈ (500, 1000]` 由处理器判 422 `param_version_mismatch`（:389-393），但 `limit > 1000` / `limit < 1`（以及 :684 `start < 0`）会先撞 **FastAPI 内建 422**（非 `CodedHTTPException` 形状）——v4 域 422 有两种 body 形状（coded vs FastAPI validation error），契约是否冻结了后者？
  3. v4 envelope ETag 依赖双门控叠加：`_v4_json_response` 需 `representation.vary.v4 ∈ SATISFIED` **且** `response_rep_version(...) is not None`（:618/:626-628）；v3 面（`_finalize_sessions_response` :116）无 readiness 门控只看 env。v3/v4 ETag 启用条件不对称是否契约明示（v4-contract §4.4/§15）？
  4. §13 门控 mid-request 双读：:452 与 :474（DB 路径）、:542 与 :565（Class A 路径）各自独立调 `_v4_session_single_revision_active()`；readiness 在两次读之间翻转 → 同一响应内 item 形态与 `degraded_required` 不一致（纯理论 race，动态读法固有）。
  5. v4 Class A `complete = len(items) < limit`（:558）best-effort：恰好返回 limit 条且实为末页时 `complete:false` 且 `nextCursor` 恒 null（:563）——客户端拿什么信号停页？（契约 §4.2 已声明 best-effort，但空页终止协议值得确认。）
  6. v3 同族问题：`_finalize_sessions_response` :115 `complete = len(sessions) < limit`——上游 `/session` 是否有 LIMIT+1 语义？若无，v3 也有"末页 complete:false"多发一页的问题（v3 可继续 start 递增，拿到空列表后 complete:true 终止，多一次 RTT）。
  7. `_fetch_status_raw` :207-211 用**非流式** `upstream.get` 一次性缓冲，无 `read_with_cap`/413 保护（对比列表路径 stream+cap）；status map 上游无界时 sidecar RSS 暴露。是否有意（体量假设）？
  8. `_status_via_lease` :244 与 `sessions_status` :858 的 JSON parse 均在事件循环（无 offload）——与列表路径 offload 纪律不一致，同为体量假设问题。
  9. v4 分支 `directory` 参数（:681 签名保留）在 `_sessions_v4` 内完全不消费；selector `_DIRECTORY_V4_RETIRED_PATTERNS` 只含 `^/slimapi/sessions$`（`selector.py:194-197`）——即 **`/slimapi/sessions/status` 在 v4 面仍消费 directory**（不在退役表，继承 v3 语义）。v4 "global facade" 哲学下 status 保留 directory 输入是否契约有意的例外？
  10. `_sessions_via_lease` :184-193：`sessions = await pool.offload(...)` 赋值发生在 `async with lease` + `async with pool` 双层内，`TransformBusy` 在 :194 捕获时 lease 上下文仍会释放 caller ref（:174-177 注释自证）——异常安全性依赖 `RawFetchRegistry` lease 的 `__aexit__` 实现（本文件不可见，审计 E1 其他卡片覆盖）。
  11. `_V4_PARENT_RESERVED` :272 **定义未使用**——`parent` 未在路由层校验保留词（任意非空串均放行，:399-402 只拒空串）；四态语义（all/none/only/字面 sid）完全下沉 `dbaux/projection.py:180-187`。路由层无校验 + 常量闲置，读起来像漏了校验，实际是下沉设计——建议确认契约 §4.1 是否要求路由层显式拒非保留词。
  12. :673 `_project_http_sessions_v4_canonical` 内不可表示 item 抛 `_aux_unavailable()`（非 `_fail_closed_503`）→ **不写** `request.state.slimapi_degraded_503`，而 DB 路径同场景 :461 走 `_fail_closed_503`（写观测位）。同为 §13.2c fail-closed，R2 观测口径不一致（http 降级路径已有 `slimapi_sessions_source="http"` 可区分，但 503 泳道缺失）——确认是否故意。

---

### src/oc_slimapi/routes/_read_passthrough.py（277 行）

- **职责**：§10.a 读组（file/vcs/find/providers/session-single/active/global-health）共享的 controlled-proxy GET 管线：verbatim raw-query 转发 → streaming 上游 GET → 两层冻结错误映射（5xx/网络 → 503 `upstream_unavailable`；4xx → status+body verbatim）→ 成功状态 verbatim 透传 → （投影路由）gated+pooled+offloaded 投影 → cap-read → ETag/304 → 冻结 header 透传集 + gzip 重编码。

- **对外符号**：
  - `_PASSTHROUGH_UPSTREAM_HEADERS` :71-77 — 冻结透传 header 集（content-type/location/retry-after/x-request-id/last-request-id；**永不** Content-Encoding，:66-70 注释：httpx 已解码实体，sidecar 在自有 gzip 门下重编码）。
  - `_upstream_passthrough_headers(response, *, default_content_type="application/json")` :80-100 — 收集上游响应上实际存在的冻结集 header；`default_content_type` 仅 C1 读组保留 `application/json` 默认（§10.b 写路由传 `None` 走 strictly-present-only，:85-91）。
  - `_raw_upstream_url(request, upstream_path)` :103-116 — §5.2 verbatim-query 上游 URL：取 post-selector raw query bytes → `_strip_v_segments`（幂等，兜 selector-less 栈，:108-110）→ `f"{upstream_path}?{raw_qs.decode('latin-1')}"`；未知参数/重复/百分号编码/`+` 字节级保留。
  - `_maybe_pool(transforms)` :119-134 — admission 包装 asynccontextmanager：投影路由 pooled（`TransformBusy` 上抛 → 调用方 handler 转 `transform_busy`）；raw 路由 `transforms=None` 完全不碰池（§10.a admission 冻结：纯 raw 受控代理**不占** transform 池）。
  - `_read_error_body(request, response)` :137-154 — cap 保护的错误体读取（§10.a:141 冻结）：`read_with_cap` + `stash_up_in`；`RequestError` → 503；超 cap（`err is None`）→ verbatim 职责降级为 503（资源保护优先，:152-153）。
  - `read_passthrough_get(request, *, upstream_path, directory=None, project=None)` :157-277 — 管线主体（阶段分解见下）。

- **共享管线各阶段**（`read_passthrough_get` 内）：
  1. **校验/准备** :183-192：`rep_version = etag_mod.response_rep_version(config, wire_view=3)`（:186）；`vary = merged_vary("Accept-Encoding")`（§6.2 terminal 单值，:187-188）；`upstream_url = _raw_upstream_url(...)`（:189）；投影路由才取 `transforms`（:191-192）。
  2. **上游**（admission 内，`_maybe_pool` :195）：`stream_upstream`（:197-198，来自 `_catalog_common`；`RequestError` → 503，:199-200）。
  3. **状态分派** :202-239：`status >= 500` → `_read_error_body` 排空后 503（:203-206，错误体只用于记账不透传）；`status >= 400` → cap-read 错误体 + `Response(err, status_code=status, headers=_upstream_passthrough_headers(response))` **verbatim 透传**（:207-218；无 ETag/Vary/Cache-Control 添加）；成功 → `read_with_cap` + `stash_up_in`（:219-224）→ `body is None` → 413 `error_response("response_too_large", ...)`（:225-230）。
  4. **变形** :231-239：`project is not None and pool is not None and 200 <= status < 300 and body`（非空）才 `pool.offload(project, body)`；`ValueError`（坏 JSON/非对象）→ 503（2xx-but-malformed = upstream breach，:236-239）；204/3xx/空体 verbatim 不投影（B5 门控）。
  5. **缓存/条件请求** :245-257：`rep_version is not None and body` 时 `etag_mod.judge_conditional(body, if_none_match, rep_version, accept_encoding)`（:249-250）；verdict `"*"`（`If-None-Match: *`）→ 重算 coding 派生 ETag 后 304（:251-255）；verdict 命中 → 304（:256-257）。**管线从不短路**——条件请求在上游 GET 之后 fresh 计算。
  6. **记账**：`on_read=lambda n: stash_up_in(request, n)`（:222；错误体读取同样 stash，:149）。
  7. **响应组装** :259-277：`compress_if_beneficial` → headers = `Cache-Control: no-store` + 冻结透传集 + coding + `Vary` 覆写（:259-270）；非空 body 加 ETag（coding 派生 actual，:271-273）；**成功状态 verbatim 透传**（201/202/204/206/3xx，不 follow 重定向——upstream client `follow_redirects=False`，:274-276）。

- **依赖**：`contextlib.asynccontextmanager`、`httpx`、`etag`、`gzip_util.compress_if_beneficial/error_response`、`selector._strip_v_segments`（**私有跨模块引用**）、`traffic.stash_up_in`、`transform.TransformBusy/TransformPool/read_with_cap`、`upstream_errors.raise_upstream_unavailable`、`_catalog_common.busy_response/stream_upstream`。
- **被依赖**：`routes/read_groups.py:82-87`（`_raw_upstream_url`/`_read_error_body`/`_upstream_passthrough_headers`/`read_passthrough_get`——12 个调用点 :160-:628）；`routes/write_groups.py:88-93`（复用 `_PASSTHROUGH_UPSTREAM_HEADERS`/`_raw_upstream_url`/`_read_error_body`/`_upstream_passthrough_headers`，不调 `read_passthrough_get`）；`tests/test_b4_new_routes.py:20`。

- **状态/可变性**：模块级仅常量（header 集）；无请求间状态；一切按请求局部。

- **错误路径**：
  - `upstream_unavailable` 503：:151（错误体读 RequestError）、:153（错误体超 cap）、:200（上游 GET RequestError）、:205-206（5xx 排空后）、:224（成功体读 RequestError）、:238-239（投影 ValueError）。
  - `response_too_large` 413：:226-230（成功体超 cap，`error_response` 直构）。
  - `transform_busy` 503：:242-243（`busy_response`，仅投影路由）。
  - 4xx verbatim：:214-218（原状态码 + 原始 body + 冻结 header 集，非 coded 错误）。

- **疑问点**（6）：
  1. :60 `from ..selector import _strip_v_segments` — 引用 selector **私有**符号；selector 重构（改名/移动）会静默破坏 verbatim 兜底。
  2. :251-255 `verdict == "*"` 分支先 `compress_if_beneficial` 再丢弃 `encoded` 只为判 actual coding——304 前做了一次无用压缩（CPU 浪费；可先由 `accepts_gzip` 判 coding）。行为正确但费算。
  3. 4xx verbatim 分支（:214-218）不带 `Vary`/`Cache-Control`——若客户端缓存对 4xx 存 body（RFC 允许带 Cache-Control），无 Vary 的 gzip 重编码缺失（此分支不重编码、原样 bytes，无 Content-Encoding header）——确认上游 4xx 体是否可能 gzip 编码到达（httpx `stream_upstream` 走 `send(stream=True)` 后 `aread()`，httpx 会自动解压？`read_with_cap`/`aread` 的解码语义需在 transform.py 核对，E1 对应卡片覆盖）。
  4. :233 投影条件含 `pool is not None`——`project is not None` 时 transforms 理论上恒非 None（:191-192 同条件推导），双重检查冗余（防御式，无行为差异）。
  5. :245/:271 两处 `rep_version is not None and body` —— 空 body 2xx（如真 200 空体）无 ETag 也无 304 判定，但 :259 `compress_if_beneficial` 对空体的行为（MIN_GZIP_BYTES 门）决定 wire 形状；与 :247-248 注释一致，无问题，仅记录。
  6. `_upstream_passthrough_headers` 的 `default_content_type` 默认 `"application/json"`：4xx verbatim 分支（:217）也走该默认——上游 4xx **无** Content-Type 时 sidecar 会**补** `application/json`，而 verbatim 语义（"客户端看原始错误"）下补默认值是否算变形？（§10.a 冻结集语义 vs C2 gate follow-up 注释只谈了读组 2xx。）

---

### src/oc_slimapi/routes/_catalog_common.py（439 行）

- **职责**：catalog 族（agent/command/diff/todo/children + messages/directories/read_groups/sessions 复用局部）共享的 admission → stream 上游 → cap-read → 错误映射 → offload project+pack → Response 管线；P2-B2 从 agent.py/command.py ~95% 重复代码合并；含 TTL body 缓存路径（Batch 1/A1）与 ETag 预压缩判定（Batch 2/B1）、rev-6 B1/C2 的 `merge_directory_vary`/`min_gzip_bytes` 旋钮。

- **对外符号**：
  - `TRANSFORM_RETRY_AFTER_SECONDS = 2` :41 — 与测试共享的 Retry-After 常量。
  - `busy_response(accept_encoding=None)` :44-52 — 503 `transform_busy` + `Retry-After: 2`（`error_response(retry_after=...)` 与 :51 手动 set 双写，冗余无害）。
  - `stream_upstream(request, upstream_path, directory, read_timeout=None, upstream_params=None)` :55-94 — 构建并发送 streaming GET（cap-read 前提）；转发 `X-Request-ID` + directory header（P0-6 关联 access log 与 opencode 日志，:68-70）；`read_timeout` → httpx timeout 扩展（connect 5.0 / read+write read_timeout / pool 5.0，:84-90）；`upstream_params` verbatim 转发（T18，diff 的 messageID）；`RequestError` → 503（:93-94）。**调用方必须 finally `aclose`**（:65）。
  - `read_upstream_response(request, response, *, cap, read_with_cap, sid=None)` :97-143 — 排空错误体或 cap-read 成功体（不关 response，调用方持有）；`>= 400` → `aread` + stash + `raise_upstream_status_code(status, sid=sid)`（`sid` 切换 404 → `session_not_found` 映射，:117-119）；成功 → `read_with_cap` + `on_read=stash_up_in`（mid-stream RequestError 不丢已读字节，P0-9，:120-121）；任何 `RequestError` → 503（:141-142）；返回 `None` 表示超 cap（调用方自定 413 形状）。`read_with_cap` 作参数以保测试 monkey-patch 通路（:126-130）。
  - `make_project_and_pack(project_fn, body, *, err_label, accept_encoding, rep_version=None, if_none_match=None, merge_directory_vary=False, min_gzip_bytes=None)` :146-213 — worker 线程入口：parse → 非列表 `ValueError`（:191-192，路由映射 503）→ `project_fn` → identity dumps → gzip 协商（`accepts_gzip`，RFC 7231 `gzip;q=0` 尊重）→ `min_gzip_bytes` 小体门（:198-199）→ `rep_version` 非空时**压缩前**算 ETag + `If-None-Match` 命中返回 `(None, headers)`（304 零压缩零传输，:200-207）→ gzip level 6（:210-212）。catalog 列表**不排序**（非时序，上游序保留，:159-160）。
  - `handle_catalog_request(...)` :216-325 — 共享 handler（参数：`upstream_path`/`directory`/`project_fn`/`read_with_cap`/`err_label`/`read_timeout`/`cache`/`sid`/`enable_etag`/`merge_directory_vary`/`min_gzip_bytes`/`upstream_params`）；`cache` 非空 → `_handle_catalog_cached`（:262-273）；否则：`rep_version`（仅 `enable_etag` 时计算，wire_view 来自 scope，:278-283）→ `async with pool`（admission-first，:284）→ `stream_upstream`（:285-288）→ `read_upstream_response`（:290-295，`sid` 传递）→ `body is None` → 413（:296-301）→ offload `make_project_and_pack`（:302-311；`JSONDecodeError/ValueError` → 503，:312-313）→ finally `aclose`（:314-315）→ `encoded is None` → `conditional_304`（:316-321）→ 200 + `Cache-Control: no-store` + extra（:322-325）。
  - `_offload_catalog_body(request, pool, project_fn, body, err_label, rep_version=None, merge_directory_vary=False)` :328-366 — 仅缓存路径共享的 offload+组装（与 uncached 内联形逐字节对齐，:337-338；坏 JSON/非列表仍查 503 保 parity，:339-341）；`rep_version` 在**投影后**的 body 上取 validator（缓存原始体按 config 重投影后哈希，:342-344）。
  - `_handle_catalog_cached(...)` :369-439 — 缓存链：fresh hit → 跳过上游 GET，只 admission + offload（:399-407，`stash_cache("hit")`）；miss → admission 内 `cache.refresh(key, _fetch_body)`（single-flight 去重，:422-423）；`ttl=0` → refresh 返回 state `None` 且不报 cache 字段（:424-428）；`body is None` → 413（:429-434）；仅成功 200 体入缓存（factory 错误/cap 溢出/坏 JSON 旁路，:387-390 注释，实现在 CatalogCache）。

- **依赖**：`gzip`（level 6 手压，**不经** `gzip_util.compress_if_beneficial` 的 MIN_GZIP 门——门在 :198-199 自实现）、`orjson`、`httpx`、`etag`、`gzip_util.accepts_gzip/error_response`、`selector.wire_view_from_scope`、`traffic.stash_cache/stash_up_in`、`upstream.forward_upstream_headers/request_id_from_scope`、`upstream_errors.raise_upstream_status_code/raise_upstream_unavailable`。
- **被依赖**（rg 反查）：`routes/agent.py:9,41`、`routes/command.py:9,38`、`routes/diff.py:39,95`、`routes/todo.py:24,70`、`routes/children.py:30,76`（以上 `handle_catalog_request` + `busy_response`）；`routes/messages.py:36`（`read_upstream_response`）；`routes/directories.py:12`（`busy_response`）；`routes/read_groups.py:81`（`busy_response/stream_upstream`）；`routes/_read_passthrough.py:64`（`busy_response/stream_upstream`）；`routes/sessions.py:41`（`busy_response/read_upstream_response`）；测试 monkey-patch 点：`tests/test_diff_routes.py:441`、`tests/test_children_routes.py:364`、`tests/test_todo_routes.py:356`、`tests/test_catalog_cache.py:595`（patch `_catalog_common.read_upstream_response/stash_cache`）。

- **状态/可变性**：模块级仅常量；无请求间状态。缓存可变性在 `CatalogCache`（`cache.refresh` 单飞）；`_handle_catalog_cached` 的 `_fetch_body` 闭包按请求构造。

- **错误路径**：
  - `upstream_unavailable` 503：`stream_upstream` :93-94（RequestError）；`read_upstream_response` :141-142（drain/read RequestError）；`handle_catalog_request` :312-313（offload `JSONDecodeError/ValueError`）；`_offload_catalog_body` :355-356（同族）。
  - `upstream_http_N` 502 族 / `session_not_found`：`read_upstream_response` :136 `raise_upstream_status_code(response.status_code, sid=sid)`——`sid=None`（catalog 族）404 → 502 `upstream_http_404`；`sid` 非空（todo/children/diff）404 → `session_not_found`（:116-119/:247-250 注释）。
  - `response_too_large` 413：`handle_catalog_request` :296-301、`_handle_catalog_cached` :429-434。
  - `transform_busy` 503：本模块**不构造**——admission `async with pool` 的 `TransformBusy` 上抛给调用方（agent/command/todo/children/diff 的 `except TransformBusy: return busy_response(...)`；read_passthrough/sessions 内部自捕）。
  - `directory_*`：不在此层——路由层 `validate_directory` 先行（:237-239 注释），selector 层管 ladder。

- **疑问点**（7）：
  1. **缓存路径参数缺口**：`handle_catalog_request` :262-273 转 `_handle_catalog_cached` 时**丢弃** `sid`/`enable_etag`/`min_gzip_bytes`/`upstream_params`——缓存路径 :395-397 无条件算 `rep_version`（不看 `enable_etag`）。当前唯一传 cache 的调用方是 agent.py:44（enable_etag 默认 True、无 sid），缺口不可达；但若未来给 todo/children（enable_etag=False）接缓存，会**静默**发 ETag 且丢 404→session_not_found 映射。建议在签名或断言上设防。
  2. :198-199 `min_gzip_bytes` 门在 `rep_version` 判定**之前**作用于 `gzip_wanted`：文档（:185-188）称"不与 rep_version 组合"仅是调用方约定——代码上组合时 :203 coding 与 :210-212 实际编码仍一致（exactness 不破），docstring 的"would break that exactness"表述与代码不完全相符（是约定禁用而非机制禁用）。
  3. :211 `gzip.compress(encoded, compresslevel=6)` 直接用 stdlib gzip，绕过 `gzip_util.compress_if_beneficial`——与 `_read_passthrough`/`gzip_util.json_response` 的压缩策略（含 MIN_GZIP_BYTES=64 全局门）分叉；此处小体门由 `min_gzip_bytes` 参数自理，agent/command 不传则**无小体门**（空 `[]` envelope 也会压）。确认是否有意。
  4. `make_project_and_pack` 304 路径（:206-207）返回的 headers 含 `ETag` + merged `Vary` 但**无** `Cache-Control`——304 的 `Cache-Control: no-store` 由 `etag_mod.conditional_304`（:319-321/:360-362 调用）内部补还是缺失？（本文件不可见，etag.py 卡片覆盖；若缺失则 200 与 304 缓存语义不一致。）
  5. `_handle_catalog_cached` fresh-hit 路径 :399-407：hit 时**不发生任何上游请求**，但 `rep_version` 仍含 wire-view 标记、ETag 对缓存 body 投影后判定——若 config 的 project 白名单在缓存 TTL 内变更，缓存 body 重投影正确（好），但 `If-None-Match` 命中返回 304 的 body 语义 = **旧 config 投影**（缓存原始体未失效）——config 变更是否使缓存键失效？（CatalogCache 键 = `(upstream_path, directory)` :398，不含 config 维度。）
  6. `read_upstream_response` :134-136：`>= 400` 时先 `aread()` 排空再 `raise_upstream_status_code`——排空体仅 stash 记账后丢弃；对比 `_read_passthrough._read_error_body` 会把 4xx 体**透传**。两模块对 4xx 的处理哲学不同（catalog 族把上游 4xx 转 coded 502/404，read 组 verbatim 透传）——这是两套契约面（§骨架路由 vs §10.a），仅记录边界。
  7. `busy_response` :46-51：`error_response(..., retry_after=...)` 已设 `Retry-After`，:51 再手动覆写同值——双写冗余（若 `error_response` 的 retry_after 参数语义变化，:51 会静默覆盖）。

---

## 汇总

| 文件 | 行数 | 疑问点数 |
|---|---|---|
| src/oc_slimapi/routes/sessions.py | 883 | 12 |
| src/oc_slimapi/routes/_read_passthrough.py | 277 | 6 |
| src/oc_slimapi/routes/_catalog_common.py | 439 | 7 |
