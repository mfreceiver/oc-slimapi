# E1-16 核心语义文件精读卡片（10 文件全文精读）

审计基线：2026-08-20 工作区快照。引用格式 `src/oc_slimapi/...:行号`。全部文件已逐行读完（非抽样）。

---

### src/oc_slimapi/selector.py（739 行）

- **职责**：pure-ASGI 的 `/slimapi/**` wire 版本选择器（v4-contract §2，双版本窗口 3/4）：解析 `?v=`、执行 §8.3 冻结优先链（versions 405 → version 400s → method 405 → directory 400s）、消费 `v`/`directory` 参数（字节保真剥离）、§16.1 过渡期三组合 POST 405 门、向 `scope["state"]` 记录 §9.1 观测维度。非 `/slimapi` 请求零接触放行。

- **对外符号**（逐个）：
  - `SELECTOR_STATE_KEY = "slimapi_selector"`（92）：scope-state 键，traffic-accounting 中间件在请求结束时读取。
  - `DIRECTORY_FORM_STATE_KEY = "slimapi_directory_form"`（93）：directoryForm 维度键。
  - `SELECTOR_ABSENT/V2/V3/V4/REJECTED/EXEMPT/NOT_APPLICABLE`（98-104）：§9.1 selectorResult 枚举（冻结；`absent`/`v2` 按构造不再产生，注释 95-97 自认）。
  - `SSE_RESULT_DIMS = ("v2","v3","v4","absent","not_applicable")`（109）：sseActive 维度表（含两个死值）。
  - `VERSION_QUERY_PARAM = "v"`（111）、`VERSIONS_PATH = "/slimapi/versions"`（112）。
  - `DIRECTORY_QUERY_PARAM = "directory"`（117）、`DIRECTORY_HEADER_NAME = "x-opencode-directory"`（118）。
  - `V3_DIRECTORY_STATE_KEY = "slimapi_v3_directory"`（124）：**仅**在 §5.3 consuming 非 stream 路由成功消费 `?directory=` 后写入（= 已校验 resolved 值）。
  - `_SELECTOR_LEXICAL_RE = ^[1-9][0-9]*$`（127）：ASCII 数字、无前导零。
  - `SUPPORTED_WIRE_VERSIONS`（135-137）：由 `ACCEPTED_CLIENT_VERSIONS` 派生的升序元组 `(3,4)`——单一真源在 versioning。
  - `_DIRECTORY_CONSUMING_PATTERNS`（143-188）：§5.3 消费集，27 条 regex（messages 三形态 + expand 两条、sessions/status/todo/children/diff/stream、agent、command、§10.a 读组 9 条、§10.b 写组 3 条）。stream 显式列入（注释 139-142 说明：其错误语义在消费集、仅 happy case no-op）。
  - `_DIRECTORY_V4_RETIRED_PATTERNS`（197-199）：v4 退役表，**只列** `^/slimapi/sessions$`（集合差而非重定义，防漂移，注释 191-196）。
  - `_DIRECTORY_RETIRED_IN_V4_BODY`（205-212）：统一 `directory_retired_in_v4` 错误体（code+hint，无 directory 回显）。
  - `_V4_METHOD_BOUNDARY_FEATURE`/`_V4_POST_ACTIONS_FEATURE`（244-245）：§16 双条件字符串 ID。
  - `_METHOD_BOUNDARY_POST_PATTERNS`（247-254）：三组合：`POST /session/{sid}`（allow=GET,PATCH,DELETE）、`POST /session/{sid}/archive`、`POST /session/{sid}/delete`（allow=空）。
  - `_v4_method_boundary_405_live()`（257-266）：§16.1 合取 `method.boundary.v4 ∈ SATISFIED ∧ session.post-actions.v4 ∉ SATISFIED`，**调用时动态读** `readiness_mod.SATISFIED`。
  - `_method_boundary_allow()`（269-280）：method≠POST 返回 None；匹配三组合返回冻结 allow 元组。
  - `_normalize_path()`（283-284）：多斜杠折叠（仅用于 selector 决策，路由仍见原始 path，P1-14）。
  - `_is_directory_consuming()`（287-288）、`_is_v4_directory_retired()`（291-295）、`_directory_consuming_for()`（298-308，v4 = v3 集 − 退役表）。
  - `_has_query_key()`（311-322）：query 带 key 且**值非空**（keep_blank_values）。
  - `_has_directory_header()`（325-333）：头**存在即真**（空/空白值也算，M3-1/§5.7）。
  - `_directory_form()`（336-349）：directoryForm = both|query|header|absent（非 consuming 路由 None）。
  - `_stash()`（352-356）：写 `scope["state"]`。
  - `selector_info_from_scope()`（359-365）：读 stash（未跑返回 `{}`）。
  - `wire_view_from_scope()`（368-388）：S-B04——本请求 wire 视图，仅 stash wire=="4" 返回 4，**其余一律默认 3**（含 selector-less 测试栈）。
  - `_has_directory_query_pair()`（391-403）：**key 存在即真**（空值也算）——v4 退役判定用（与 `_has_query_key` 的非空要求刻意不同，391-398 注释自证）。
  - `resolve_route_directory()`（406-421）：路由取最终 directory——有 stash 用 stash（已被 selector 校验+剥离），否则用 FastAPI 绑定值原样。
  - `_collect_v_values()`（424-432）：全部 `v` 值（含空）。
  - `_collect_directory_values()`（435-445）：全部 `directory` 值（空值保留）。
  - `_directory_header_value()`（448-458）：第一个 X-Opencode-Directory 原始值（空值也返回）。
  - `_is_stream_path()`（461-462）：`endswith("/stream")`。
  - `_segment_key_in()`（465-475）：raw 段 key 经 `unquote_plus` 解码后比对——`%76=3` 按 `v=3` 判定并同样被消费。
  - `_strip_query_keys()`（478-494）：按 `&` 分段、逐段保留幸存者**原字节**（不 urlencode 重建）。
  - `_strip_v_segments()`（497-499）：剥离全部 `v` 对。
  - `SlimapiSelectorMiddleware`（502-732）：`__call__`（510-620）主链；`_reject_version()`（622-634）400 `unsupported_version`+`supported:[3,4]`；`_consume_directory()`（636-717）目录消费/校验梯；`_forward()`（719-732）剥离 `v` 后放行。
  - `_header()`（735-739）：取第一个匹配头（不合并多值）。

- **`?v=` 解析与 405/400 优先链（§8.3，冻结顺序）**：
  1. 非 `/slimapi`（`_is_slimapi_path`，versioning）→ 零接触，仅记 `not_applicable`（516-523）。
  2. **① versions 405**：折叠 path == `/slimapi/versions` 且非 GET → 405 `method_not_allowed` + `Allow: GET`（533-545）；GET → 无条件豁免 `exempt`，直接 `_forward`（546-549）。
  3. **② version 400s**：无任何 `v` → 400 `unsupported_version`（551-556 → 622-634；「无 selector = 已退役版本请求，绝不 404」）；任一值 lexical 非法 **或** 去重后多值（`?v=3&v=4`）→ 400 `invalid_version_selector`（558-565）；同值重复折叠合法（`len(set(values))!=1` 判定，558）；lexical 合法但 ∉{3,4} → 400 `unsupported_version`（567-572）。
  4. **method 405（§16.1）**：`wire=="4" ∧ _v4_method_boundary_405_live() ∧ (method,path) ∈ 三组合` → 405 `method_not_applicable` + 冻结 Allow + `Cache-Control: no-store`（592-609）；此时 stash 保持 `selectorResult=v4`（非 rejected，588-590 注释自证）。
  5. **③ directory 400s**：`_consume_directory`（611-619）：v4 退役路由（`/slimapi/sessions`）**任何** directory 输入（query key 存在即可/头存在即可）→ 统一 400 `directory_retired_in_v4`（668-673）；tolerant 路由放行（674-677）；v3 梯子：多值 distinct(normalized) → `invalid_directory_selector`（679-681）→ query 单值 validate 失败透传 `exc.code` 即 `invalid_directory`（682-688）→ dual-present normalized-different → `directory_conflict`（frozen `queryDirectory`/`headerDirectory` 字段，689-695）→ dual-present same / header-only → `directory_header_retired`（696-698, 702-704）→ query-only 单值：stream 路由 no-op（699-701），否则 stash+strip（707-716）。
  6. **④ forward**：剥离全部 `v` 段后进路由（620, 729-731）。
  - 豁免清单（不做版本校验者）：仅 **GET `/slimapi/versions`**（含斜杠折叠变体）与**全部非 `/slimapi` 路径**；其余一切 `/slimapi/**`（含 `/slimapi/health`）都必须带合法 `?v=`，否则 400 `unsupported_version`。
  - wire 版本分支返回：`_stash(scope, SELECTOR_V3 if wire=="3" else SELECTOR_V4, wire)`（579）——v3/v4 对称入 stash。
  - selectorResult 观测：`_directory_form` 在拒绝前先算并写入（525-530）；`not_applicable` 请求也写 `directoryForm=None`（519-521）。

- **依赖**：`readiness`（动态读 SATISFIED，85/264-266）、`directory.normalize_directory/validate_directory`（86）、`errors.CodedHTTPException`（87，仅用于捕获 validate 异常提取 code）、`gzip_util.json_response`（88）、`versioning.ACCEPTED_CLIENT_VERSIONS/_is_slimapi_path`（89）、starlette types。
- **被依赖**：`app.py:31,747`（`add_middleware(SlimapiSelectorMiddleware)`，位于 TrafficAccounting 之内、路由之外）；`access_log.py` / `middleware/traffic_accounting.py` / `sse_observability.py` 读 SELECTOR_STATE_KEY/DIRECTORY_FORM_STATE_KEY/SSE_RESULT_DIMS；routes 经 `wire_view_from_scope`/`resolve_route_directory` 读视图（sessions/messages/read_groups/write_groups/versions 等）。
- **状态/可变性**：模块级常量除派生元组外不可变；无实例状态（middleware 无 per-request 存储，全部走 scope state）；行为随 `readiness_mod.SATISFIED` 模块全局在调用时浮动（flip 即改 wire 行为，设计使然）。
- **错误路径构造点**：`method_not_allowed`（540）、`invalid_version_selector`（561）、`method_not_applicable`（597-601）、`unsupported_version`（629，经 `_reject_version`）、`invalid_directory_selector`（681）、`directory_conflict`（691-695）、`directory_header_retired`（698、704）、`directory_retired_in_v4`（常量 205-212，返回点 672）、`invalid_directory`（不在本文件构造——由 directory.validate_directory 抛出、687-688 透传 code）。`param_version_mismatch` 不在本文件（位于 `routes/sessions.py:386-401,703` 的 422）。`thin_route_not_found` 不在本文件（proxy.py:48）。
- **疑问点（12）**：
  1. `?directory=`（空值）单独出现：`_collect_directory_values` 保空值 → `validate_directory("")` 经 normalize 变 `"/"`（directory.py:20）→ **静默消费为根目录**而非 400/no-op；而 directoryForm 判定要求值非空（311-322）→ 空 directory 的观测记 `absent` 但语义上被消费为 `/`。观测与行为不一致，是否有意？
  2. 双版本表维护面：`SUPPORTED_WIRE_VERSIONS` 与 routes/versions.py:63 均派生自 versioning 单源（好），但 `SSE_RESULT_DIMS`（109）含 `"v2"`/`"absent"` 死枚举值，5.0.0 收敛 (4,4) 时 v3 分支（579）与 §5.1 梯子是需**删除**的全量代码路径（非翻转），退役清单应提前立账。
  3. `method_not_applicable` 405 时 stash 保持 `selectorResult=v4`（588-590）——access log 将记 v4+405；与 `rejected` 的区分是否被 traffic-accounting 正确消费（本文件外，需核对）。
  4. `_is_stream_path`（461-462）用 `endswith("/stream")` 而非精确 regex——当前被消费集限定，但未来任何新增以 `/stream` 结尾的 consuming 路由会**意外继承** §5.6 no-op 语义（脆弱耦合）。
  5. `_header`（735-739）对重复 `accept-encoding` 取第一个、不合并——多值 AE 场景协商结果依赖此顺序行为，无测试锚定的风险。
  6. `?v=3&v=03`："03" lexical 失败 → `invalid_version_selector`（而非 unsupported_version）——两码边界完全由 lexical regex 先后决定（558 在 567 之前），回归测试需显式覆盖此格。
  7. `/slimapi//versions` GET：`_normalize_path` 命中豁免（533）放行，但路由层见原始 path 不匹配 → catch-all 404 `thin_route_not_found`——豁免面与路由面的 path 归一化差异（P1-14 自认）产生「selector 放行、路由 404」组合，客户端排障时可能困惑。
  8. `_directory_form` 用 v3 全集判定（339）——v4 retired 路由 `/slimapi/sessions` 带 directory 也记 query/header/both，随后请求被 `directory_retired_in_v4` 拒；日志维度与错误码并存的解读需文档化。
  9. 多值同规范化折叠（`?directory=/a&directory=/a/`，679-684）取 `values[0]` 做 validate+stash（resolved 是规范化值，无歧义）——仅确认行为与契约「distinct(normalised) 才 400」一致。
  10. `_has_query_key`/`_collect_v_values`/`_collect_directory_values` 的 `try/except Exception`（319-321, 429-431, 442-444）：latin-1 decode 不会失败，死守卫（无害但属噪音）。
  11. `X-Slimapi-Version` 核对结论：本文件全文不读任何版本请求头（仅 `?v=`；568-570 注释自证「header is not read」）——与 3.0.0 删除声明一致，无残留解读路径。✓
  12. `_consume_directory` 中 stash 写在 strip 之前、且仅成功路径 strip（707-716 注释自证「400 请求 query 字节无关」）——正确；但 stash/strip 之间无原子性保护（同 scope 顺序执行，无并发面）——确认无问题。

---

### src/oc_slimapi/versioning.py（44 行）

- **职责**：wire 版本常量钉扎（v3-contract §0 / v4-contract §0）：当前最新 major、客户端可接受窗口、`/slimapi` 路径判定 helper。

- **对外符号**：
  - `_SLASH_RE`（25）/`_SLIMAPI_PATH`（26）：内部用。
  - `_is_slimapi_path(path)`（29-31）：折叠多斜杠后 == `/slimapi` 或 startswith `/slimapi/`——`/slimapi//x` 判定为 slimapi 域。
  - `SERVER_API_VERSION = 4`（38）：当前（最新 major）wire 版本；喂 `/slimapi/versions` 的 `current` 与 config 的常量钉扎 `Settings.server_api_version`。
  - `ACCEPTED_CLIENT_VERSIONS: tuple[int,int] = (3, 4)`（44）：inclusive (min,max) 窗口；4.0.0 由 (3,3)→(3,4)；5.0.0 将收敛 (4,4)。fail-closed：config.validate 强制 env 不可改（config.py:817-819）。

- **依赖**：无内部依赖（仅 re）。
- **被依赖**：selector.py:89（`ACCEPTED_CLIENT_VERSIONS` 派生 SUPPORTED、`_is_slimapi_path`）；config.py:13（常量钉扎 + fail-closed 校验 436,439-440,791-830）；routes/versions.py:55,61,63（`current`/`supported` 派生）。
- **状态/可变性**：全常量，不可变。
- **错误路径**：本文件不构造错误（版本越界的 400 在 selector；env 篡改的 RuntimeError 在 config.py:817-819）。
- **疑问点（2）**：
  1. 双版本表维护面：`SERVER_API_VERSION=4` 与 `ACCEPTED_CLIENT_VERSIONS=(3,4)` 需**成对同步**（5.0.0 时两处都要改）；config.py:829-830 只断言 `server_api_version ∈ accepted range`，**没有断言 `SERVER_API_VERSION == max(ACCEPTED)`**——若未来只改窗口忘改 current，`/versions` 的 `current` 将静默偏低且无报警。
  2. `_is_slimapi_path`（29-31）把 `/slimapi//x` 判入 slimapi 域（selector 会做版本校验/消费），而路由层匹配原始 path → catch-all 404——「已过 selector 又 404」的路径形态（`//` 变体）是刻意宽容还是遗漏（与 selector 疑问 7 同源，归口此处）。

---

### src/oc_slimapi/features.py（20 行）

- **职责**：`/slimapi/health` 的静态能力广播（四键全 True，随发布列车走，**非**灰度 flag）。

- **对外符号**：`FEATURES: dict[str,bool]`（15-20）——`tokenCoalesce/permissionEvents/serverMerge/transformAbsorb` 四键全 True。docstring（10-12）注明 `tokenStream`/`thresholdedSkeleton` 不在此，由 `routes/health.py` 自身提供。

- **依赖**：无。
- **被依赖**：`routes/health.py:6,69`（`**FEATURES` 合入 health 的 features 响应对象）。
- **状态/可变性**：静态 dict（模块级可变类型但无写入方）。
- **错误路径**：无。
- **疑问点（2）**：
  1. 与 readiness.py 的十 ID 体系是**两套并行的 feature 表面**（health `features` vs versions `capabilities["4"].readiness`），命名域不重叠（camelCase vs dotted-ID）——是否有合并计划/漂移风险（新增能力该进哪张表无成文规则）。
  2. features 对象由两处拼装（本模块四键 + health.py 自持两键）——形状归口在 routes/health.py:69，键集合变更需同步契约；无单一枚举权威。

---

### src/oc_slimapi/readiness.py（187 行）

- **职责**：v4-contract §3.3 十 feature readiness 门：`SATISFIED ⊆ REQUIRED` 宇宙、依赖蕴含（⑦）、`ready ⇔ f(REQUIRED) ⊆ f(SATISFIED)` 派生公式、payload 构造。**无任何运行时状态进入集合**（随代码版本变化，docstring 21-24 自证）。

- **对外符号**：
  - `REQUIRED`（58-69）：十 ID 宇宙（冻结枚举序）：`selector.v4`、`session.list.global.v4`、`session.single.projection.v4`、`messages.expand.v4`、`providers.redacted.v4`、`events.global.replay.v4`、`events.token.replay.v4`、`representation.vary.v4`、`method.boundary.v4`、`session.post-actions.v4`（第 10 个为修订二追加，紧随 method.boundary）。
  - `REQUIRED_SET`（71）：frozenset。
  - `_POST_ACTIONS_FEATURE`（75）/`_IMPLICATION_PAIR`（76）：⑦ 蕴含对（post-actions ⇒ method.boundary）。
  - `SATISFIED = frozenset(REQUIRED)`（93）：**当前 = 全集**（修订二 activation close-out 2026-08-19 已点亮第 10 个 ID；89-92 注释自证）。
  - `normalize(ids)`（96-103）：f() = 去重 → UTF-8 字节序排序。
  - `validate(satisfied)`（106-123）：非字符串或 ∉U → RuntimeError（列出 normalized offenders）。
  - `validate_dependencies(satisfied)`（126-144）：违反 ⑦ → RuntimeError。
  - `ready(required=None, satisfied=None)`（147-160）：默认**调用时**解析模块全局（flip 不冻结）；`set(normalize(required)) <= set(normalize(satisfied))`。
  - `readiness_payload(satisfied=None)`（163-179）：emit 前先 validate+validate_dependencies；固定键序 `{ready, required, satisfied}`，数组均 normalized。
  - import 期守卫（186-187）：对 SATISFIED 跑两校验。

- **十个 feature 判定逻辑逐项（satisfied 条件）**：本文件**没有**逐 feature 条件逻辑——SATISFied 是静态全集（93），十 ID 全部 satisfied（`ready()` 恒 True）。逐项门控消费点在别处（均动态读 `readiness_mod.SATISFIED`）：
  1. `selector.v4` — 无条件（selector 本体即实现，未见门控消费点）。
  2. `session.list.global.v4` — 未见运行时门控（行为直接落地）。
  3. `session.single.projection.v4` — `routes/sessions.py:585`、`routes/read_groups.py:392`。
  4. `messages.expand.v4` — `routes/messages.py:62`、`routes/versions.py:101-108`（capabilities emit 条件）。
  5. `providers.redacted.v4` — `routes/read_groups.py:384`。
  6. `events.global.replay.v4` — 未见运行时门控。
  7. `events.token.replay.v4` — 未见运行时门控。
  8. `representation.vary.v4` — `routes/sessions.py:595`。
  9. `method.boundary.v4` — `selector.py:265`（合取前半）+ write_groups 双条件门。
  10. `session.post-actions.v4` — `selector.py:266`（合取后半，not-in 判定）+ `routes/write_groups.py:311`。

- **依赖**：无（仅 typing）。
- **被依赖**：selector.py:85/264-266；routes/versions.py:50/121-124（payload emit）；routes/sessions.py:585,595；routes/messages.py:62；routes/read_groups.py:384,392；routes/write_groups.py:74,311。
- **状态/可变性**：`SATISFIED` 是模块全局 frozenset，flip batch **整体重赋值**（文档规定唯一改法，46-48 注释：所有消费者调用时读，无 def-time 冻结）。
- **错误路径**：`validate`/`validate_dependencies` 的 RuntimeError（120-123, 139-144）——import 期与 emit 期双重，构造点即此；无 HTTP 错误。
- **疑问点（5）**：
  1. SATISFIED=全集 → `ready()` 恒 True、selector §16 合取恒 False（405 face 已熄灭、§16.2 等效管线激活）——过渡机制（⑦ 守卫、四格表）当前全部 inert，只剩防未来 flip 出错的护栏。审计需确认这是 4.2.x close-out 终态而非漏翻转（89-92 注释声称已 close-out，可信但应对照 CHANGELOG）。
  2. 十 ID 中至少 4 个（selector.v4 / session.list.global.v4 / events.global.replay.v4 / events.token.replay.v4）**无运行时门控消费者**——对它们翻转 SATISFIED 零行为差异；readiness 表是纯声明面，与实际门控面不对齐（新增修订面时「表已列但无门」会造成假阳性 ready 语义）。
  3. wire 数组序 = UTF-8 字节排序（177-178），≠ REQUIRED 枚举序（58-69）——消费方（ocdroid）若假设枚举序会错；契约已冻结 normalized 形式，仅提示。
  4. `ready()` 允许注入任意 required/satisfied（147-150）——纯内部 API，无外部输入入口，安全；但无访问修饰区分「测试用」与「生产用」签名。
  5. `validate()` 对非 str 元素先 repr 再 `normalize(map(str,...))`（116-122）——消息构造对混合类型稳健；仅确认。

---

### src/oc_slimapi/directory.py（52 行）

- **职责**：directory 字符串归一化 + 语法校验（core helper，置于包根避免 core→routes 反向依赖）。

- **对外符号**：
  - `normalize_directory(directory)`（12-20）：仅 `rstrip("/") or "/""`（保根 `/`）；**纯函数、无 allowlist**（docstring 13-18：不再 gate，任何 directory 都转发上游、由上游决定可服务性；归一化仅为跨端点/跨调用一致性）。
  - `validate_directory(directory)`（23-52）：normalize 后依次拒绝——精确段 `..`/`.`（36）、NUL（40）、控制字符 ord<0x20 或 ==0x7f（44-46）、长度 >4096（49-50）；全部抛 `CodedHTTPException(400, code="invalid_directory")`；通过则返回**规范化值**。

- **依赖**：`errors.CodedHTTPException`（9）。
- **被依赖**：selector.py:86（消费梯里 validate+stash）；config.py:12（normpath/realpath 组合归一，212/285/312/760——运行时目录配置校验在 config 层另有一套）；routes 层 13 处直接调用（children/diff/messages/token_stream/todo/write_groups/read_groups/agent/command/sessions）；directories.py:7（分组键归一）。
- **状态/可变性**：纯函数，无状态。
- **错误路径**：`invalid_directory` 构造点 36-37/40-41/45-46/49-50（四个 raise，同 code 无 fields 差异——不区分拒绝原因）。
- **疑问点（4）**：
  1. **canonicalization 极简**：内部双斜杠（`/a//b`）、大小写、尾部空白均不归一——selector 的 `directory_conflict` 比较用 `normalize_directory`（selector.py:690），两个语义相同但形式不同的 query/header 会判 **conflict** 而非 same；上游 opencode 是否进一步 canonicalize 决定这是否产生 false conflict（需对照上游 source）。
  2. `..`/`.` 是**精确段**匹配（36）——`...`、`..foo` 段放行（合法路径名，应放行）；无 Windows 盘符/反斜杠处理（Linux loopback 定位下可接受，记录在案）。
  3. 长度上限按 normalize 后 **code points** 计（49）而非字节——多字节目录名实际字节上限可达 ~16KB；与上游限制是否对齐未知。
  4. 空/空白输入：`normalize_directory("")→"/"`（20）——`?directory=` 静默变根目录（与 selector 疑问 1 同源，归口此处）；`"  "`（空白）不含控制字符、通过校验原样转发——上游如何解释未知。

---

### src/oc_slimapi/errors.py（62 行）

- **职责**：结构化 thin-route 错误：`CodedHTTPException`（body=`{"code":...,**fields}`，contract §11）+ 注册到 FastAPI 的 handler。

- **对外符号**：
  - `CodedHTTPException(HTTPException)`（20-41）：`code`/`fields` 属性；`detail=code` 仅为日志可读（23-25）；`self.headers` 在 `super().__init__` **之后**赋值（38-41 注释：starlette 的 `__init__` 会用自家默认 None headers 覆盖——依赖 starlette 实现细节的顺序 hack）。
  - `coded_exception_handler`（44-52）：`json_response({"code":...,**fields})` + gzip 协商；`exc.headers` 存在则 update 到响应头（50-51）。
  - `register_error_handlers(app)`（55-62）：注册 handler；生产 app.py:735 与每个测试 `_build_app` 都必须调用（docstring 57-60：测试绕过模块级 app 构建，不接则渲染回 `{"detail":...}`）。

- **依赖**：`gzip_util.json_response`（17）；fastapi/starlette。
- **被依赖**：upstream_errors.py（全部 raiser）、directory.py、routes 全域（13+ 模块 raise 点）；app.py:23,735。
- **状态/可变性**：异常对象 per-raise；handler 无状态。
- **错误路径**：本文件是错误渲染中枢；构造点在各 raise 方（见各卡）。
- **疑问点（4）**：
  1. **错误体形状统一性（并存三路）**：① 本 handler（异常驱动）；② selector.py 手写 `json_response` 体（561,597-601,627-631 等——不经异常类）；③ proxy.py `error_response`（47-51）。三路都收敛到 `{"code":...}` 但**无共享构造器、错误码无常量表**（字符串字面量散落各文件）——新增错误码拼写无编译期保护，drift 风险。
  2. `coded_exception_handler` 的 `response.headers.update(exc.headers)`（51）在 json_response 已写 `Vary` 之后——`exc.headers` 若含 `Vary` 会整体覆盖（丢 Accept-Encoding 维度）；当前无调用点传 Vary，属潜在。
  3. headers 顺序 hack（38-41）依赖 starlette `HTTPException.__init__` 实现细节——升级 starlette/fastapi 需回归此点（有注释但无测试锚定提示）。
  4. 未覆盖的默认错误面：RequestValidationError / 方法-路由不匹配的默认 405 仍走 FastAPI 默认形状（非 `{"code":...}`）——错误体统一性存在**结构性例外**（见 proxy 疑问 2）。

---

### src/oc_slimapi/proxy.py（51 行）

- **职责**：终端 catch-all 边界（contract §8.2 3.0.0）：v2 时代透明反代已退役——一切未收编路径（`/slimapi/**` 路由 miss + 全部非 slimapi 路径，含 `/event`、`/global/event`）→ 404 `thin_route_not_found`；WebSocket → 501 stub。

- **对外符号**：
  - `install_proxy(app)`（31-51）：注册两个路由。
  - `websocket_not_supported`（34-38）：`@app.websocket("/{path:path}")`——先 `accept()`，`send_json({"code":"websocket_not_supported","status":501})`，`close(1011)`。
  - `catch_all_closed`（40-51）：`@app.api_route("/{path:path}", methods=[GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS])`——返回 `error_response("thin_route_not_found", 404, ...)`（gzip 协商，经 gzip_util 一路）；`path` 参数接收未用。

- **依赖**：`gzip_util.error_response`（28）。
- **被依赖**：`app.py:27,762`（在全部 router include 之后安装，顺序注释 757-758 自证「route must precede the proxy」）。
- **状态/可变性**：无状态（闭包仅捕获 app）。
- **错误路径**：`thin_route_not_found` 构造点 48（404）；`websocket_not_supported` 构造点 37（JSON 体声称 501，实际 WS close code 1011）。§8.3 优先链声明（9-14）：selector 的 405/400 先于此边界 fire，本 handler 只表达第 ④ 类 route-miss。
- **docstring 声称的职责转移核对**（16-24）：**属实**——turn-fence S2 bump 现于 `routes/write_groups.py:112` `_write_passthrough`（bump-before-send 于 182-184，经 `turn_registry.extract_sid_from_path`/`is_turn_bumping_path`，import 于 write_groups.py:85）；本文件确无转发路径（不 import upstream，零上游 IO）；shell/PTY deny list、raw-query 转发、上游字节计数、catch-all SSE 观测在本文件不可达（「moved or deleted」——moved 部分已证实，deleted 部分本卡无法穷尽，留 routes 卡核对）。
- **疑问点（5）**：
  1. WS stub 的呈现层级：`accept()` 后才送 501 JSON、close(1011)（36-38）——客户端视角是握手成功(101)后异常关闭，**不是 HTTP 层 501**；`"status":501` 仅是 JSON 字段。ocdroid 侧如何消费此形（期待 1011?）需对照客户端。
  2. catch-all methods 列表不含 TRACE/CONNECT（42）——未列方法命中 Starlette 默认 405（非 coded 形状），错误体统一性的结构性例外（与 errors.py 疑问 4 同源）。
  3. 404 体无 path 回显、无 `Cache-Control: no-store`——而 selector 的 405 有（selector.py:605）；同类终端边界响应头不齐（无缓存语义危害，loopback，但一致性问题在）。
  4. `error_response` 路径设置 `Vary: Accept-Encoding`（经 json_response 119）——404 恒定 body 上的 Vary 无害但冗余；仅记录。
  5. `path` 形参未用（44）——纯占位；若未来想回显/日志 path，此处是挂点。

---

### src/oc_slimapi/upstream.py（149 行）

- **职责**：共享 httpx.AsyncClient 工厂 + 安全头转发 helpers（原为反代核心，反代退役后仅剩 client 工厂与注入头构造）。

- **对外符号**：
  - `HOP_BY_HOP`（11-25）：RFC 7230 §6.1 集合 + `host` + `proxy-connection`；**不含** `content-length`（P1-11 注释 14-20：历史上误剥，非 hop-by-hop，已纠正）；`transfer-encoding` 仍剥。
  - `DIRECTORY_HEADER = "X-Opencode-Directory"`（26）、`REQUEST_ID_HEADER = "X-Request-ID"`（27）。
  - `FORBIDDEN_PREFIXES = {"x-forwarded-","x-real-"}`（31-34）、`FORBIDDEN_EXACT = {"x-real-ip"}`（35-37）：客户端→上游不转发（防伪造）。
  - `create_client(config)`（40-46）：`base_url=config.upstream`；**硬编码** `timeout=Timeout(connect=5.0, read=30.0, write=300.0, pool=5.0)`、`limits=Limits(max_connections=32, max_keepalive_connections=16)`、`follow_redirects=False`。
  - `strip_hop_by_hop(headers)`（49-111）：multi_items 保重复头；Connection-token 追加封锁；剥 hop-by-hop/伪造前缀/cookie；重复头 comma-merge（103-110，Set-Cookie caveat 注释 96-102）。
  - `forward_directory_headers(directory)`（113-114）：`{X-Opencode-Directory: d}` or `{}`。
  - `forward_upstream_headers(directory, request_id)`（117-136）：directory + X-Request-ID 合并（§7 可观测：每个 sidecar→opencode 请求必须带 X-Request-ID）。
  - `request_id_from_scope(scope)`（139-149）：读 RequestIdMiddleware 的 stash；middleware 未跑返回 None（安全省略）。

- **依赖**：config.Settings（类型）、middleware.request_id.REQUEST_ID_KEY。
- **被依赖**：`app.py:41,308`（lifespan 里 `create_client`，`aclose` 于 312）；routes/health.py、routes/_catalog_common.py、routes/write_groups.py（`forward_upstream_headers`/`request_id_from_scope`）；**`strip_hop_by_hop` 生产路径零消费者**（仅 tests/test_upstream.py 六个用例引用）。
- **状态/可变性**：client 是 app.state 上的长生命周期对象（lifespan 管理）；helpers 纯函数。
- **错误路径**：本文件无错误构造（网络错误映射在 upstream_errors/routes）。
- **疑问点（5）**：
  1. **超时/limits 全硬编码**（43-44）：Settings 无对应字段、运维不可调——read=30s 对慢上游操作（大 skeleton、冷启动）的余量；write=300s 与 pool=5s 的组合在 32 连接上限下的排队行为；均无配置出口。follow_redirects=False → 上游 3xx 以 `upstream_http_3xx` 502 浮出（见 upstream_errors 疑问 1）。
  2. **`strip_hop_by_hop` 是生产 dead code**：反代退役后无任何 src 调用点（仅测试引用）——连同 HOP_BY_HOP/FORBIDDEN_* 常量与 comma-merge 逻辑（含 Set-Cookie 合并缺陷 caveat）一起成遗留。是预留还是应删？若删，tests/test_upstream.py 随之处理。
  3. 同名头方向性语义：客户端**入站** X-Opencode-Directory 在 consuming 集被 selector 判 retired 400（selector.py:325-333），而 sidecar **出站**自注入同名头（113-114）——「入禁出注」正确但同名易混淆，部署文档需明示（stunnel/中间层若回注入站头会触发 400）。
  4. `request_id_from_scope` 依赖 RequestIdMiddleware 为最外层（app.py:755 满足）；direct-route 测试栈无该中间件时静默省略 X-Request-ID（docstring 143-145 自认）——§7「必须携带」在测试栈是软约束。
  5. cookie 永不转发（90-91，注释「opencode does not rely on cookies」）——上游若未来引入 cookie 会话会**静默断**，无监控面。

---

### src/oc_slimapi/upstream_errors.py（105 行）

- **职责**：上游错误→结构化 CodedHTTPException 的单一映射源（contract §7），三个 raiser + 一个 envelope 码串函数。

- **对外符号**：
  - `UPSTREAM_UNAVAILABLE = "upstream_unavailable"`（32）：§7 码串单源（questions fan-out 等 envelope 场景引用）。
  - `raise_upstream_unavailable(exc=None)`（35-46）：503；覆盖网络失败/中途读失败/JSON 解码失败/非 list 体/5xx-after-drain；`exc` 提供则 `from exc` 链。
  - `upstream_error_code_for_status(status)`（49-60）：5xx→`upstream_unavailable`；**其余（含 4xx 与 3xx）**→`upstream_http_N`——envelope 场景（questions fan-out）用。
  - `raise_upstream_status_code(status, *, sid=None)`（63-84）：drain 后调用的非链式变体——404+sid→404 `session_not_found`（field `sessionID=sid`）；<500→502 `upstream_http_N`；≥500→503。
  - `raise_upstream_status(exc, *, sid=None)`（87-105）：`raise_for_status()` 异常驱动变体，同映射 + `from exc` 链（buffered `upstream.get()` 路由用，如 GET /slimapi/sessions/status）。

- **依赖**：errors.CodedHTTPException（28）、httpx（类型）。
- **被依赖**：discovery、_catalog_common、directories、messages、permissions、questions、read_groups、_read_passthrough、sessions、write_groups（10 模块，20+ raise 点）。
- **状态/可变性**：纯函数。
- **错误路径**（全部构造点）：503 `upstream_unavailable`（45-46, 84）；404 `session_not_found`（81, 102）；502 `upstream_http_N`（83, 104）；envelope 码串（59-60）。
- **疑问点（5）**：
  1. **3xx 落入 `<500` 格**（82-83）：follow_redirects=False（upstream.py:45）下上游任何重定向都成 502 `upstream_http_301/302/307…`——「上游错误」码形实为**路由/配置期望破裂**；契约 §7 是否冻结此格值得核对（码语义对客户端有误导性）。
  2. 404+sid 的**信任假设**（80-81）：把上游 404 译码为 `session_not_found`——若 404 实因路由拼接 bug（path 错），会误报会话不存在；sid 判定隐含「路由永远拼对」。
  3. **同一映射三份实现**：`raise_upstream_status_code`（63-84）/`raise_upstream_status`（87-105）/`upstream_error_code_for_status`（49-60）——4xx/5xx 分界与 404-sid 特判在两处重复（仅 chaining 差异）；改映射需同步三处，drift 面积小但存在。
  4. `upstream_http_N` 的 N 无白名单——任意状态码直接拼接进 wire 码串；客户端按字符串前缀解析，新增码无协商。
  5. `raise_upstream_unavailable(exc=None)` 不链时 503 丢失根因 traceback（44-46）——调用方责任选择；审计 routes 时应抽查各调用点是否一致传 exc。

---

### src/oc_slimapi/gzip_util.py（144 行）

- **职责**：显式 gzip 助手（SSE/streaming 不经此）；`accepts_gzip` 是「是否 gzip 此 JSON body」的唯一协商权威；两条压缩路径 + JSON/error 响应构造。

- **对外符号**：
  - `MIN_GZIP_BYTES = 64`（25）：P1-31 最小体阈值（gzip 帧头开销 ~18B+deflate 框架，小体压了更大；注释 22-24 举例 version gate 400 体 ~44B、短错误码 ~31B 应跳过）。
  - `accepts_gzip(accept_encoding)`（28-72）：RFC 7231 §5.3.4 q 值解析——`gzip;q=0` 显式拒绝（P3-1 修复，34-36）；`*;q=N` 通配仅在无显式 gzip 时生效（显式 gzip;q=0 覆盖通配，37-38）；`x-gzip` 同义（39-40）；空/None→False（41-42）；畸形 q 忽略保默认 1.0（43-44, 59-63）。
  - `compress_if_beneficial(body, accept_encoding)`（75-107）：**三门**——协商、>MIN_GZIP_BYTES、压缩结果严格更小（incompressible 保护，93-96）；返回 `(payload, headers)`，headers 恒含 `Vary: Accept-Encoding`，实际压缩才加 `Content-Encoding: gzip`。
  - `json_response(value, *, accept_encoding=None, status_code=200, headers=None)`（110-123）：orjson 序列化；**无阈值无收益比较**——`accepts_gzip` 通过即压（121-122）；无条件覆写 `Vary`（119）。
  - `error_response(code, status_code, *, accept_encoding=None, **fields)`（126-144）：`{"code":...,**fields}` 经 `json_response` 同路。

- **依赖**：orjson、gzip、starlette Response。
- **被依赖**：极广——errors.py、proxy.py、selector.py、envelope.py、transform.py、providers_projection.py、routes 14 模块（messages 16 处、sessions 14 处最多）；`compress_if_beneficial` 用于 providers_projection、write_groups、_read_passthrough、transform、questions、permissions、directories、messages。
- **状态/可变性**：纯函数；`compresslevel=6` 两处硬编码（103, 121）。
- **错误路径**：无异常路径（解析 best-effort 全吞）。
- **疑问点（7）**：
  1. **`json_response` 绕过 P1-31 三门**（110-123）：无 MIN_GZIP_BYTES、无收益比较——`accepts_gzip` 即压。而 MIN_GZIP_BYTES 注释（22-24）声称 version gate 400 体（~44B）与短错误码（~31B）是「returned raw」的典型——**但 selector 的 400/405 体恰恰走 json_response**（selector.py:539,560,596,627 → error_response/json_response），小体仍会被压缩且净膨胀（44B gzip 后 ~60B+）。注释描述与实现路径**矛盾**：三门只在 compress_if_beneficial 一路生效。
  2. **两套压缩路径并存**（核心统一性问题）：`compress_if_beneficial`（三门，路由 envelope 用）vs `json_response`（一门，错误体/小 JSON 用）；`error_response` docstring（136-139）声称「same path as json_response — no duplicated gzip logic」，实际是**复用了较弱的那条**——错误体与数据体的 gzip 策略不对称（错误体可能膨胀压）。
  3. `json_response` 无条件覆写调用方 `Vary`（119：`output_headers["Vary"] = "Accept-Encoding"`）——若调用方需叠加其他 Vary 维度会被 clobber；`compress_if_beneficial` 同样只支持单一 Vary（98）。均不支持 Vary 追加。
  4. `accepts_gzip` 重复 q 参数取**最后一个**（57-63 循环覆盖，如 `gzip;q=0;q=1`→1.0）；RFC 7231 规定首个为准——best-effort 自认（43-44），记录偏差。
  5. `p[:2].lower()=="q="` 匹配（59）——参数名仅 `q`（大小写不敏感）生效；`Q=0.5` 命中而 `q =0.5`（=前空格）不命中——tokens 已 strip，正常形不踩坑。
  6. `compresslevel=6` 两处硬编码（103,121）——无配置出口，CPU/压缩率权衡固定。
  7. `error_response` docstring 措辞「When truthy and containing gzip」（137）暗示子串匹配——实际走 `accepts_gzip` 全语义（q=0 拒绝）；文档小误，建议修词。

---

## 汇总

| 文件 | 行数 | 一句话 | 疑问点数 |
|---|---|---|---|
| selector.py | 739 | §8.3 冻结优先链 + 双版本窗口 + directory 消费 + §16 405 门的唯一裁决层，观测维度经 scope state 外放 | 12 |
| versioning.py | 44 | 双版本窗口 (3,4) 单源钉扎 + `/slimapi` 域判定 | 2 |
| features.py | 20 | health 静态四能力广播（全 True，非 flag） | 2 |
| readiness.py | 187 | 十 ID readiness 宇宙 + ⑦ 蕴含守卫 + ready 派生；当前 SATISFIED=全集 | 5 |
| directory.py | 52 | 极简 canonicalization（rstrip 尾斜杠）+ 四类语法拒绝 | 4 |
| errors.py | 62 | CodedHTTPException + handler；错误体形状三路并存的归口之一 | 4 |
| proxy.py | 51 | 终端 404 `thin_route_not_found` + WS 501 stub；职责转移声明核实属实 | 5 |
| upstream.py | 149 | httpx client 工厂（超时/limits 硬编码）+ 注入头 helpers；strip_hop_by_hop 成生产 dead code | 5 |
| upstream_errors.py | 105 | 上游→coded 错误映射三实现 + envelope 码串；3xx 落 502 格 | 5 |
| gzip_util.py | 144 | gzip 协商权威 + 两条不对称压缩路径（json_response 绕过三门） | 7 |

疑问点合计 51。
