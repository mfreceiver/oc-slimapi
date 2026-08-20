# E1 精读卡片 — write_groups.py / events.py / token_stream.py

> 只读审计产物（2026-08-20）。引用格式 `src/oc_slimapi/...:行号`；跨文件引用已实地核对。

---

## 1. `src/oc_slimapi/routes/write_groups.py`（583 行）

### 职责
§10.b 的 12 条受控写代理（Batch C2）+ B4 五条加性端点（#13–#17，directory 非消费集，转发到上游 v2 `/api/session/**`）+ §16 修订二三条 POST 等效动作（#18–#20，`?v=4` ∧ `session.post-actions.v4 ∈ readiness.SATISFIED` 门控）。全部 handler 收敛到唯一共享管线 `_write_passthrough`：sidecar 不改写成功语义，只加请求/响应上限、审计头与 `?v=`/`?directory=` 消费（模块 docstring :1-63）。

### 共享管线 `_write_passthrough`（:112-248）——20 个 handler 的统一五段
1. **body 校验**：自读 socket，超过 `config.max_message_bytes` → 413 `request_too_large`（:135-144）；`preset_body` 非 None 时跳过自读（archive 路由已按同一 loop 预读并查限，:145-149；archive 自读副本 :368-376，同码同序）。
2. **upOut 记账**：`if body: stash_up_out(request, len(body))`（:154-155；traffic.py:303-309）。
3. **directory**：`_resolve`（:98-109）从 selector stash（`resolve_route_directory`）取已校验值再 `validate_directory`，仅作 `X-Opencode-Directory` 头通道（`forward_upstream_headers`，upstream.py:117-137）；client 请求头形态的 directory 由 dispatch 层拒绝，本层不读（:100-105）。content-type 逐字节透传（:160-169），唯一 override 是 archive 合成体的冻结 `application/json`（:161-165）。
4. **上游转发**：`_raw_upstream_url`（_read_passthrough.py:103-116，scope query 剥 `v` 后逐字节拼上游 URL；B4 路由先经 `_strip_directory_query` 就地剥 `directory`，:504-514）；`build_request(..., content=bytes(body) or None)`（:171-176）→ **S2 turn-fence bump 触点**：`method == "POST" and is_turn_bumping_path(upstream_path)`（:182-186）时 `turn_registry.bump_turn(sid)`，**bump-before-send**——send 失败不回滚（强栅栏容忍洞，:177-181 注释）。正则 `^/session/[^/]+/(prompt_async|abort)/?$`（turn_registry.py:283-314），故 20 条路由中**仅 #4 prompt_async（:417-422）与 #5 abort（:425-429）bump**；POST 等效族以 PATCH/DELETE 转发（:339/:414）天然不 bump，B4 路径前缀 `/api/` 不匹配正则。
5. **响应变形**：`send(stream=True)`（:188-192，`httpx.RequestError` → `raise_upstream_unavailable`，upstream_errors.py:35）→ 5xx：cap 保护读错误体后折叠 503 `upstream_unavailable`（:196-203）→ 4xx：status+body 逐字节 verbatim + 冻结头集，**不加** no-store/Vary（:204-216）；oversized 错误体经 `_read_error_body` 降级 503（_read_passthrough.py:137-149）→ 2xx/3xx：`read_with_cap(max_response_bytes, on_read=stash_up_in)`（:217-222），超限 413 `response_too_large`（:223-228）→ 成功重编码：`compress_if_beneficial` + `Cache-Control: no-store` + 冻结响应头集（present-only，`default_content_type=None`，_read_passthrough.py:71-100）+ 单值 `Vary: Accept-Encoding` 覆盖（:235-248）。3xx 同按成功处理，不跟随重定向（:36-38, :232-234）。

### 对外符号（20 路由逐 handler）
| # | handler / 路由 | 行号 | 五段差异点 |
|---|---|---|---|
| — | `router = APIRouter(prefix="/slimapi", tags=["write-groups"])` | :95 | — |
| — | `_resolve(request)` | :98-109 | 消费集 directory → 头；非消费集（B4）返回 None |
| — | `_write_passthrough(...)` | :112-248 | 共享管线（上节） |
| 1 | `create_session` — POST `/slimapi/session` | :256-259 | 标准管线 |
| 2 | `update_session` — PATCH `/slimapi/session/{id}` | :262-271 | 双 payload 形状不区分，verbatim |
| 3 | `delete_session` — DELETE `/slimapi/session/{id}` | :274-278 | 标准管线（空体转发为空） |
| 18 | `post_update_session` — POST `/slimapi/session/{id}` | :325-339 | 非 admitted → 404（:336-337）；admitted → 以 `method="PATCH"` 进管线（:338-339） |
| 20 | `post_archive_session` — POST `.../archive` | :342-395 | 非 admitted → 404（:358-359）；自读实体（:368-376）→ **octet 判据**：`len(body)>0` 一律不解析透传（:378-385，CT verbatim）；空实体 → 合成 `{"time":{"archived":<ms>}}`，`<ms>=int(time.time()*1000)` 判空后立即读（:387-391），`content_type_override="application/json"`（:392-395） |
| 19 | `post_delete_session` — POST `.../delete` | :398-414 | 非 admitted → 404（:411-412）；admitted → `method="DELETE"` 进管线，无 ignore-body 分支（:413-414） |
| 4 | `prompt_async` — POST `.../prompt_async` | :417-422 | **S2 bump 触点** |
| 5 | `abort_session` — POST `.../abort` | :425-429 | **S2 bump 触点** |
| 6 | `summarize_session` | :432-437 | 标准管线 |
| 7 | `fork_session` | :440-445 | `messageID` 是 body 字段非 query（:442-443） |
| 8 | `revert_session` — POST `.../revert` | :448-452 | legacy 单步；与 #15-17 路径不互截（:552） |
| 9 | `respond_permission` — POST `.../permissions/{pid}` | :455-462 | 标准管线 |
| 10 | `reply_question` — POST `/slimapi/question/{rid}/reply` | :465-470 | 标准管线 |
| 11 | `reject_question` — POST `/slimapi/question/{rid}/reject` | :473-478 | 空体转发 |
| 12 | `session_command` — POST `.../command` | :481-486 | 标准管线 |
| — | `_POST_ACTIONS_FEATURE = "session.post-actions.v4"` | :300 | 与 selector.py:245 重复字面量（见疑问 4） |
| — | `_post_actions_admitted(scope)` | :303-311 | `wire_view_from_scope(scope) >= 4 ∧ feature ∈ readiness_mod.SATISFIED`（请求时动态读模块属性） |
| — | `_pre_revision_404(request)` | :314-322 | 404 `thin_route_not_found`，gzip 协商 + `Vary`，无 Allow/no-store（error_response → gzip_util.py:110-148） |
| — | `_strip_directory_query(request)` | :504-514 | B4 非消费集宽容：就地 mutate `scope["query_string"]` 剥 `directory`（`_strip_query_keys` 字节保真，selector.py:478） |
| 13 | `session_agent` — POST `.../agent` | :517-528 | `_strip_directory_query` → 上游 `/api/session/{sid}/agent` |
| 14 | `session_model` | :531-542 | 同上 → `/api/session/{sid}/model` |
| 15 | `revert_stage` | :545-557 | 同上 → `/api/session/{sid}/revert/stage` |
| 16 | `revert_clear` | :560-570 | 同上 → `/api/session/{sid}/revert/clear` |
| 17 | `revert_commit` | :573-583 | 同上 → `/api/session/{sid}/revert/commit` |

### 依赖 / 被依赖
依赖：`readiness`（:74，SATISFIED frozenset，readiness.py:93 全集=REQUIRED，含 post-actions 已点亮）、`directory.validate_directory`（directory.py:23-48）、`gzip_util.compress_if_beneficial/error_response`、`selector`（DIRECTORY_QUERY_PARAM/_strip_query_keys/resolve_route_directory/wire_view_from_scope）、`traffic.stash_up_in/out`、`transform.read_with_cap`（transform.py:143-155）、`turn_registry`（:283-314）、`upstream.forward_upstream_headers/request_id_from_scope`、`upstream_errors.raise_upstream_unavailable`、`routes._read_passthrough`（:71-77 头集 / :103-116 / :137-149 / :80-100）。被依赖：app.py:29 导入、app.py:760 注册（read_groups 之后、catch-all 之前）。

### 状态 / 可变性
handler 自身无状态；两类就地变更：selector 已 mutate scope（剥 `v`、消费集剥 `directory`+stash）、B4 路由再 mutate `scope["query_string"]`（:511-514）。`readiness_mod.SATISFIED` 为进程级 frozenset，flip batch 重赋值、请求时动态读（:310-311）。`config.max_message_bytes` / `max_response_bytes` 每请求读（:130/:138/:219）。

### 错误路径（构造点逐点）
- **413 `request_too_large`**：write_groups.py:139-143（管线自读）与 :370-375（archive 自读，同 loop 同序）；任一上游调用前。
- **503 `upstream_unavailable`**：:191-192（send `RequestError`）与 :202-203（5xx 折叠），均经 upstream_errors.py:35（NoReturn）；4xx 错误体超限的降级 503 在 _read_passthrough.py:137-149。
- **413 `response_too_large`**：:223-228。
- **404 `thin_route_not_found`**：:319-322 构造；:337 / :359 / :412 三处触发（v3 基线 + 门控关防御穿透；与 proxy.py catch-all 的 4.0.0 基线逐字节一致，:314-318）。
- **405 `method_not_applicable`**（不在本模块，selector 层）：selector.py:594-607 构造（body `{code, method, allow}` + `Allow` 头 + no-store）；生效条件 selector.py:257-266（`method.boundary.v4 ∈ SATISFIED ∧ session.post-actions.v4 ∉ SATISFIED`）；组合表 selector.py:248-252（`POST /slimapi/session/{sid}` → Allow `GET, PATCH, DELETE`；`.../archive`、`.../delete` → 空 Allow）；仅 `?v=4`，v3 永不被 selector 拦（write_groups.py:281-298 注释）。
- **400 `invalid_directory`**：directory.py:23-48（`_resolve` :108 调用；消费集值已由 selector 先验）。
- selector 层 400 族（本模块消费集路由会被先行拦截）：`invalid_directory_selector`（selector.py:681）、`directory_conflict`（selector.py:686-690）、`directory_header_retired`（selector.py:703-704）、`directory_retired_in_v4`（selector.py:641-646，body :200-206）。
- **501 `websocket_not_supported`**：proxy.py:35-37（任务清单要求定位；与本模块无直接关系）。

### 疑问点（12）
1. **:310 `>= 4`** 而全仓其余处均 `== 4`（events.py:37、token_stream.py:81）——为 v5 预留还是笔误？当前值域 {3,4} 下等价。
2. **archive 合成体计入 upOut**：:149 `body=bytearray(preset_body)` 非空 → :154-155 `stash_up_out`——sidecar 合成字节被记为"upstream-request bytes"，与客户端实发 0 字节不符（:151-155 注释自辩为"buffered body about to send"口径）；traffic 审计口径需确认。
3. **POST 等效族双门答案分裂**：selector 过渡态答 405+Allow（selector.py:594-607），handler 防御门答 404 无 Allow（:319-322）——若未来 flip 回退（SATISFIED 重赋值）出现门控关穿透，同一 URL 两代答案漂移；且 `session.post-actions.v4` 字面量在 :300 与 selector.py:245 双处定义、无单一事实源。
4. **v3 method 发现性为零**：v3 下 `POST /slimapi/session/{sid}`、`.../archive`、`.../delete` 一律 404 `thin_route_not_found`（:336-337/:358-359/:411-412），无 Allow、无 hint——契约冻结如此（:296-298），但对 v3 客户端不可区分"路由不存在"与"方法不适用"。
5. **archive octet 判据的 chunked 边界**：判据是"读完 socket 后 len==0"（:368-376, :378）；`Content-Length: 0` 与空 chunked 流等价处理；若客户端发 trailer-only chunked 或连接半途断开，`request.stream()` 异常未被本路由捕获（FastAPI/ASGI 层兜底）——确认可接受。
6. **双份 body 内存峰值**：archive 路由 :376 `body`（bytearray）→ :382-384 `bytes(body)` 拷贝 → 管线 :149 `bytearray(preset_body)` 再拷贝——max_message_bytes 级实体存在至多 3 份瞬时副本。
7. **:182 bump 条件只看 `(method, path)`**：不看 admission/版本——v3/v4 的 prompt_async/abort 均 bump（正确）；但 bump 位于 `_resolve`/headers 之后（:157-186），若 `_resolve` 抛 `invalid_directory` 则不 bump（顺序正确）；若 `build_request` 抛（:171-176）也不 bump。需确认契约对"目录非法的 prompt_async 不进位"的预期。
8. **B4 多值异值 directory 静默全剥**（:504-514 + selector 非消费集不校验）：`?directory=/a&directory=/b` 在 B4 路由被宽容丢弃，与消费集同形输入的 400 `invalid_directory_selector` 严格性形成反差（:494-500 注释自证为设计）。
9. **3xx 成功化处理**（:36-38, :232-248）：redirect 实体重编码 + no-store + Vary 覆盖、`Location` 保留；`follow_redirects=False` 依赖 upstream client 配置（docstring :37-38 声明）——本文件外，需在 upstream.py 复核。
10. **4xx verbatim 无 no-store/Vary**（:204-216）：与成功分支（:236-247）缓存头不对称——经 stunnel/中间代理时的缓存语义差异点。
11. **`_resolve` 对 B4 恒 None 的隐式耦合**：B4 不 bump directory 依赖 selector `_DIRECTORY_CONSUMING_PATTERNS` 不收录这些路径（:496-499 注释）——无断言保护；若 selector 消费集扩张，B4 将开始向 `/api/` 上游发 `X-Opencode-Directory`。
12. **archive `<ms>` 时钟源**（:389）：sidecar wall-clock，与上游实际落库的 `time.archived` 可能偏差（docstring :352-356 冻结为 sidecar 口径、与 digest `updatedAt` 同源）——契约 §16.2-c 一致性已冻结，审计确认无实现漂移即可。

---

## 2. `src/oc_slimapi/routes/events.py`（253 行）

### 职责
`GET /slimapi/events`——进程级策展 SSE：单 `/global/event` 上游订阅、全 directory 全 session 广播、客户端本地过滤（:42-47）；v3/v4 双面握手（`slimapi.meta` 首帧 + v4 `capabilities/epoch/seqBase` 扩展）；`Last-Event-ID` v3 blanket resync / v4 四级分类 replay；T3 准入（per-directory + total caps）；L2-A `?tokens=1`（v3 opt-in，v4 退役）。

### 对外符号
- `TOKENS_STREAM_RETIRED_IN_V4`（:21-24）：v4 冻结退役错误体 `{code, hint}`（dict 常量）。
- `_request_wire_v4(request)`（:27-37）：scope 缺省（selector-less 测试栈/mock 无 `.scope`）→ False（v3 视图）；等价 `selector.wire_view_from_scope`（selector.py:368-392，stash `wire=="4"` 才 4，默认 3）。
- `events(request, tokens=None)`（:40-253）主 handler：
  - :88-89 `tokens` 非 None 且 ≠ 字面 `"1"` → 400 `invalid_tokens`（CodedHTTPException）。
  - :91-99 v4 ∧ `tokens=="1"` → 400 `tokens_stream_retired_in_v4` + hint（**流开启前**，无 SSE 字节、不占订阅槽）。
  - :103-107 从 `app.state` 取 `replay_log`/`replay_epoch`（epoch 缺省回退 `log.epoch`）+ `Last-Event-ID` 头。
  - :114-124 replay 分类（**先于 subscribe**）：v4∧有 log → `classify_reconnect(last_event_id, log, domain="g")`；v4∧无 log∧有 cursor → 兜底 `ReplayResync("reconnect_no_replay")`；first-connect/①②违例 → None。
  - :126-139 `hubs.subscribe(wire_v4=v4)`（registry.py:187-233：T3 双 cap 单无 await 临界区检查+接纳；`wire_v4` 抑制连接本地 `server.connected` welcome 帧并 stamp subscriber 供 fanout 打 id）；`SubscriberCapacityError` → 503 `{code, limit, current}` + `Retry-After: 5`。
  - :141-155 **meta 冻结于 handler 时刻**（非惰性到 generator）：`{subscriberId, tokens}` +（v4∧log∧epoch）`meta_v4_extension(epoch, last_seq("g"))`（replay_wire.py:212-226：`capabilities/epoch/seqBase`，meta 自身无 id）。
  - :160-162 `tokens=="1"` ∧ `token_registry` 存在 → `attach_events_subscriber(subscriber)`（tokenstream/subscriber.py:584）。
  - :165-174 `traffic_ledger` 拉取 + `_accounted`（`record_sse_downstream(bucket="events_sse")`，吞一切异常）。
  - :176-242 `generate()`：`sse_open` → **meta 首帧**（:188-189）→ replay 块：`ReplayResync` → `resync{reason}` 帧（无 id，:195-198）；`ReplayFrames` → 逐帧 `frame_with_id`（id 前缀纯加性，replay_wire.py:104-123，:199-205）；`ReplayIgnoreReset` → 无（:206）→ **v3 分支**：任何 `Last-Event-ID` → `resync{reconnect_no_replay}`（:207-213，v4 永不入此支）→ 主循环 `queue.get()`：STOP→break；`subscriber.ack(item)`（hub_types.py:327-338）→ `_accounted` → yield（:214-231）→ **finally**：token ledger 先 detach（:236-237）→ `hubs.unsubscribe(subscriber)`（经 registry 才减 `total_subscribers`，否则计数泄漏致永久 503，:238-241）→ `sse_close`（:242）。
  - :244-253 headers `no-cache, no-transform` + `X-Accel-Buffering: no` → `StreamingResponse`。
- 正确性骨架：:116 classify 与 :132 subscribe 之间**无 await**（同 tick）——replay 窗口（`ReplayFrames.entries` 在 classify 时同步物化为 tuple，replay_log.py:162-170, :454-469）与 attach 后 queue 无缝衔接，不重不漏（:109-113 注释）。

### 依赖 / 被依赖
依赖：`errors.CodedHTTPException`、`gzip_util.json_response`（gzip_util.py:110-123）、`selector.wire_view_from_scope`、`sse.hub`（re-export hub，实体在 hub_types.py：`STOP` :30 / `SubscriberCapacityError` :407 / `sse_frame` :105）、`sse.replay_log`（`GLOBAL_DOMAIN="g"` :78 / 冻结 reason 域 :88-91 / `ReplayFrames` :162 / `ReplayResync` :174）、`sse.replay_wire`（`classify_reconnect` :169-209 / `frame_with_id` :116 / `meta_v4_extension` :212）、`sse_observability.sse_open/sse_close`。被依赖：app.py:29/760 注册；`token_registry.attach/detach_events_subscriber`（tokenstream/subscriber.py:584/608）；`HubRegistry`（app.py:477-490，replay_log 注入）。

### 状态 / 可变性
无模块级可变状态（`TOKENS_STREAM_RETIRED_IN_V4` 是可变 dict 但按冻结约定不改）。每连接闭包态：`meta`（bytes，handler 时冻结）、`replay_plan`、`traffic_ledger`、`token_registry`。订阅侧运行态在 hub：`subscriber.wire_v4`、queue 的 `closed/forced_disconnects`（hub_types.py:242-255, :264-325）。分类顺序敏感：**classify(T0) → subscribe(T1) → meta seqBase(T2)** 全在同一同步块（:114-155 无 await）。

### 错误路径（构造点逐点）
- **400 `invalid_tokens`**：events.py:88-89（仅字面 `"1"` 合法）。
- **400 `tokens_stream_retired_in_v4`**（code+hint）：events.py:92-99，常量 :21-24；先于流开启。
- **503 容量族**（本文件仅映射，raise 构造点在 registry.py:213-228）：`sse_subscriber_limit_directory`（per-directory cap）/ `sse_subscriber_limit_total`；body `{code, limit, current}` + `Retry-After: 5`（:133-139）。
- **运行期连接终结（非 HTTP 错误）**：queue 溢出 → `SubscriberQueue.put` 自产 `resync{subscriber_backpressure}` + STOP 并丢弃既有队列（hub_types.py:304-325）；**v4 抑制该 resync 只 STOP**（`subscriber_backpressure ∉ V4_RESYNC_REASONS`，hub_types.py:310-320；冻结域 replay_wire.py:60-77 恰为 4 值）——"非冻结 reason 终结连接"的实现点即此：v3 = resync+STOP 终结，v4 = STOP-only 终结（断连本身是信号，恢复靠 Last-Event-ID 重连）。
- `_accounted` best-effort 吞异常：:173-174。

### 疑问点（10）
1. **错误优先级**：v4 + `tokens=0` 命中 :88 `invalid_tokens`（先）而非 :92 退役错误——"值非法"先于"参数退役"；契约 §7.3 是否冻结此顺序需对照。
2. **meta `tokens` 可谎报**：:149 `"tokens": tokens == "1"` 在 `token_registry is None`（最小栈）时仍 true，但 :161-162 不会 attach——meta 声称的能力与实际不符（最小栈边缘）。
3. **无 log 兜底死循环风险**：:119-124 v4∧无 log∧有 cursor → resync；客户端重连仍同一兜底，直到 log 就绪——生产 app.py:425-427 恒建 log，仅测试栈受影响。
4. **v3/v3 游标混用**：v3 面对任何 `Last-Event-ID`（含 v4 形 `g:…:…`）→ blanket resync（:207-213）；v4 ①②违例静默 ignore+reset 无 resync（:206 + classify None）——代际语义差异已冻结，但 v3 客户端误发 v4 id 与发垃圾值不可区分。
5. **finally 无异常保护**：:236-241 detach/unsubscribe 顺序正确（token ledger 先、控制槽后），但 unsubscribe 若抛异常 `sse_close`(:242) 被跳过——依赖 `HubRegistry.unsubscribe` 幂等不抛的健壮性约定（registry.py:236+）。
6. **记账不回滚口径**：:188/:230 `_accounted` 在 yield 前计——ASGI send 失败（客户端断连）时帧已计数（:222-229 注释自辩为全局一致口径，无 send-failure rollback）。
7. **心跳路径不可见**：本 handler 无独立 keepalive，心跳依赖 hub 周期入队（`HEARTBEAT_SECONDS`，hub_types）；若 hub 上游断线重连窗口内（global_hub.py:1024-1086）是否仍有心跳入队需在 hub 侧核对。
8. **被拒/无 selector 请求默认 v3**：`_request_wire_v4` 对 `SELECTOR_REJECTED` stash 返回 False（selector.py:386-391 只认 `wire=="4"`）——被拒请求到不了路由，仅 selector-less 栈走默认；docstring :29-33 已自证，无风险但值得记录。
9. **双源 epoch**：:104-106 `app.state.replay_epoch` 与 `replay_log.epoch` 回退链——app.py:425-427 两者同源恒等，双读冗余（若未来只重建 log 不重建 state.epoch 会静默用旧 epoch）。
10. **meta 帧也无 id 但计入 `events_sse` downOut**（:188）——与业务帧同桶；traffic-accounting 手册口径应说明 meta/resync/heartbeat 均入同桶（本文件行为如此）。

---

## 3. `src/oc_slimapi/routes/token_stream.py`（324 行）

### 职责
`GET /slimapi/sessions/{sid}/stream`——per-session token 流 SSE：in-flight text-part delta + handshake（v3 预填 `server.connected`→snapshot）/ snapshot done 标记 / truncated / `message.removed` tombstone / terminal / resync / heartbeat（docstring :1-40, 模块级 `t:<sid>:<epoch>:<seq>` id）；v3 无 id（Last-Event-ID 值忽略→leading resync）、v4 id + 四级分类 replay + **no-prefill handshake**；流恒 identity（无 gzip、无 Vary）；独立 token 预算准入（不占 `MAX_TOTAL_SUBSCRIBERS`）。

### 对外符号
- `_request_wire_v4(request)`（:72-81）：同 events 版（scope 缺省 → v3）。
- `_accepts_gzip(request)`（:84-85）：**死代码**——v2 gzip 杠杆 3.0.0 退役后全文件无调用（:203 `use_gzip = False` 恒定；`accepts_gzip` import :51 仅被它使用）。
- `_resolve_directory_conflict(request, directory)`（:88-121）：NB-D7 结构守卫（admission 前，:137 调用）：多值异值（归一化 `value.rstrip("/") or "/"`）→ 400 `invalid_directory_selector`（:112-113）；query+header 归一化异 → 400 `directory_not_allowed`（:116-119）；空 header 视为缺席；末尾 `validate_directory(directory)` 仅"parity"（结果未用，:120-121）。directory 对 fanout 是 **NO-OP**（accumulator 按 sid 键，单用户 T3 下 sid 全局唯一，:91-96/:131-134）。
- `token_stream(request, sid, directory=None)`（:124-324）主 handler：
  - :137 directory 守卫（先于准入）。
  - :138-147 `token_registry` + replay wiring（缺省降级 v3 形）。
  - :153-164 replay 分类（先于 subscribe）：`classify_reconnect(..., domain=token_domain(sid), token_sid=sid)`（②含跨 sid 校验，replay_wire.py:126-166 token 语法 `t:<sid>:<epoch>:<seq>`、sid 段为 `":".join(parts[1:-2])`）；v4∧无 log∧有 cursor → 兜底 resync。
  - :166-182 `registry.subscribe(sid, wire_v4=v4)`（subscriber.py:625-700：cap 检查→构造→ensure_upstream→flush loop 启动→attach（v4 分支 no-prefill：无 `server.connected`/无历史 tombstone/无 live-part snapshot）→closed 回滚→ledger 递增，全程同步无 await）；`TokenSubscriberCapacityError` → 503 `{code, limit, current(, bufferBytes)}` + `Retry-After: 5`（code ∈ {`sse_token_subscriber_limit`, `sse_token_handshake_overflow`}，subscriber.py:515-533）。
  - :184-197 **meta 冻结于 handler 时刻**：`{subscriberId, tokens: True}`（`tokens` 恒真——token 流必带 token）+（v4）`seqBase = replay_log.last_seq(token_domain(sid))`。
  - :203 `use_gzip = False`（v3-contract §7.2 冻结：**SSE 流恒 identity**，v4 同；:198-202 注释）。
  - :208-307 `generate()`：per-connection `zlib.compressobj(6, DEFLATED, MAX_WBITS|16)`（仅 `use_gzip` 时创建——现状恒 None，:213-217）；`encode()`（Z_SYNC_FLUSH 逐事件块 + gzip metrics 计数 :219-226——死路径）；`_accounted(bucket="token_stream_sse")`（计**线上后**字节，:228-245）；`sse_open`（:252）→ meta 首帧（:253-262，无 id）→ replay 块（resync 带 sessionID `_resync_frame(sid, reason)` :269-272，frames.py:125-126；`ReplayFrames` 逐帧 `frame_with_id` :273-280；ignore-reset 无 :281）→ **v3 分支**：任何 Last-Event-ID（值忽略）→ leading `resync{reconnect_no_replay, sessionID}`，插在 subscribe() 已同步预填的 handshake 队列（server.connected→snapshot）之前（:282-292）→ 主循环（:293-302）→ finally：`registry.unsubscribe`（last-detach 停 flush loop，NB-C4；幂等，subscriber.py:789）+ `sse_close`（:303-307）。
  - :309-324 headers（`no-cache, no-transform` + `X-Accel-Buffering: no`；**无 Vary、无 Content-Encoding、无 X-Slimapi-Subscriber-ID**——identity 表示不依赖 AE，:313-318）。

### 依赖 / 被依赖
依赖：`directory.validate_directory`、`errors.CodedHTTPException`、`gzip_util.accepts_gzip/json_response`、`selector.wire_view_from_scope`、`sse.replay_log`（`token_domain(sid)` :111 / `ReplayFrames`/`ReplayResync`/`RESYNC_RECONNECT_NO_REPLAY`）、`sse.replay_wire`（classify/frame_with_id/meta_v4_extension）、`sse.token_hub`（兼容 shim → `sse.tokenstream`：`STOP`（tokenstream 自有哨兵，非 hub_types.STOP，tokenstream/hub.py:92-94）/ `TokenSubscriberCapacityError` / `_resync_frame` / `sse_frame`，frames.py:33/125）、`sse_observability`。被依赖：app.py:760 注册（:756 注释：先于其余 /slimapi router）；`token_registry` 由 app.py:555-566 构建（`token_stream_max_subscribers/queue_items/buffer_bytes/max_frame_bytes`）。

### 状态 / 可变性
每连接闭包：`meta_frame`（handler 冻结）、`replay_plan`、`use_gzip`（恒 False）、`traffic_ledger`、`compressor`（恒 None）——gzip 相关为死状态。订阅侧运行态：`TokenSubscriber`（handshake/runtime 双 ledger queue、`wire_v4`、metrics 含死 gzip 计数器）与进程级 flush loop 生命周期（first-attach 启 / last-detach 停，NB-C4，:37-39）。STOP 哨兵与 hub 族是**两个不同 object**（hub_types.py:30 vs tokenstream frames），不可跨族混用。

### 错误路径（构造点逐点）
- **400 `invalid_directory_selector`**：token_stream.py:112-113（多值异值）。
- **400 `directory_not_allowed`**：token_stream.py:116-119（query+header 归一化异值）——注意与 selector 层同形输入的 `directory_conflict`（selector.py:686-690）**代码名不一致**（见疑问 2）。
- **400 `invalid_directory`**：directory.py:23-48 经 :121 触发（query-only 非法值；全栈下 selector 对 stream 的 case-4 no-op 前也 validate——同码，selector.py:677-701）。
- **503 token 预算族**：:173-182 映射；raise 构造点 subscriber.py:515-533 + :663-668（`sse_token_subscriber_limit`）；handshake 溢出 `sse_token_handshake_overflow` 带 `bufferBytes`（:174-176）。
- **运行期终结（非 HTTP 错误）**：`session_deleted` → `resync{session_deleted}` → STOP terminate（tokenstream/hub.py:1072-1089, :1121 写 replay barrier）；v4 下 `session_deleted`/`token_memory_limit`/`session_idle` 等**非冻结 reason 走 STOP-only**（∉ `V4_RESYNC_REASONS`，replay_wire.py:60-77）；queue 溢出同 events 语义（subscriber put 家族）。**v4 永不发 snapshot 帧**（resync 后客户端 HTTP 全量拉取，:263-268 注释；但 `ReplayFrames` 窗口内可含 snapshot-done 标记帧——docstring :18-19，两类"snapshot"需区分）。
- `_accounted` 吞异常：:244-245。

### 疑问点（11）
1. **整条 gzip 路径为死代码**：:51 `accepts_gzip` import、:84-85 `_accepts_gzip`、:203 恒 False、:213-226 compressor/encode gzip 分支与 `subscriber.metrics.gzip_*` 计数——自 3.0.0（v2 杠杆退役）起 v3/v4 均 identity；保留意图（v2 墓碑 / 未来复用）还是应删，审计标记。
2. **directory 冲突代码名双层不一致**：同形输入（query+header 异值）全栈下 selector 先答 `directory_conflict`（selector.py:686-690），selector-less 栈（直调路由测试）走 :116-119 答 `directory_not_allowed`——契约应指明哪个是冻结答案；messages.py:315 存在同款双轨。
3. **:112 归一化与 selector `normalize_directory` 的一致性**：本文件用 `value.rstrip("/") or "/"`（空值→"/"），selector 用 `directory.normalize_directory`（directory.py:33）——两套归一化的逐字节等价性需核对（`?directory=/a&directory=/a/` 是否恒同值）。
4. **双写 directory 校验**：:121 `validate_directory` 结果丢弃（"parity"），全栈下 selector 已对 stream case-4 前置 validate（selector.py:677-701）——防御性重复；selector-less 栈下此处是唯一防线。
5. **meta `"tokens": True` 恒真**（:190）：与 events 的 `tokens` 字段同形不同义（events=opt-in 标志 / stream=恒真）——客户端不得据 stream meta 判断 events 订阅态。
6. **v3 leading resync 依赖同步预填**（:282-292）：resync 帧须排在 subscribe() 同步入队的 handshake（server.connected→snapshot）之前——正确性依赖 `subscribe()` 无 await 的临界区纪律（subscriber.py:644-660 docstring），任何在 subscribe 内引入 await 的改动都会破坏该顺序。
7. **tombstone 双通道**：v3 历史 tombstone 在 handshake 预填；v4 不预填，靠 replay 窗口内的 `FRAME_KIND_TOMBSTONE` 帧（replay_log.py:85-87）或 `message.removed` fanout——replay 窗口 TTL 过期后重连的 v4 客户端 tombstone 补偿是否完备（`replay_expired` resync 后客户端全量拉取兜底）需结合 design-v4-sse-replay 核对。
8. **独立预算不对称披露**：token 503 的 `limit/current` 是独立 ledger（subscriber.py:537-550，最坏 ~76MiB 口径），与 events 503 字段同构但池不同；CLIENT_CHANGES §7「同时最多 1 条前台 stream」仅客户端建议、服务端只强制 `token_stream_max_subscribers`——运维排障时两池数字不可互相印证。
9. **STOP 双哨兵**：tokenstream STOP（frames）与 hub STOP（hub_types.py:30）为不同 object——token_stream.py:61 从 `..sse.token_hub` 导入的是 tokenstream 版（shim token_hub.py:8-23）；跨族误用（如 events 订阅者收到 tokenstream STOP）会静默 break 而非类型错误——现状隔离正确，仅作审计记录。
10. **finally 无异常保护**（:303-307）：`registry.unsubscribe` 若抛异常 `sse_close` 跳过——同 events 疑问 5，依赖幂等约定。
11. **:206 traffic_ledger 拉取位置**：在 meta 冻结之后、generate 定义之前（handler 体）——若 ledger 运行中被禁用/置 None，已建连接的闭包引用不受影响（一致性 OK）；仅当 handler 重入时才见新值——无问题，记录口径。

---

## 附：三文件共性结论（供 E2/E3 汇总）
- v4「非冻结 reason 终结连接」的统一实现：冻结 reason 域 4 值（replay_wire.py:72-77）；域外 reason v3 发 resync+STOP、v4 只 STOP（hub_types.py:310-320；tokenstream subscriber 同策略）。
- v4「不发 snapshot」在两个 SSE handler 均成立（events.py:190-206 / token_stream.py:263-268），恢复路径=Last-Event-ID 重连 + ReplayLog replay 或 HTTP 全量。
- 客户端断连清理均走 generator `finally` → registry unsubscribe（events.py:232-242 / token_stream.py:303-307），registry 层负责计数与 flush-loop/上游生命周期回收。
- replay 无缝衔接依赖「classify→subscribe 无 await」+「ReplayFrames.entries classify 时物化 tuple」（replay_log.py:162-170, :454-469）——两个 SSE handler 的 :109-113/:149-152 注释即此论证。
