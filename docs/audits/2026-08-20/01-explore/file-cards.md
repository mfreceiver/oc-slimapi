# E1 文件级精读卡片（file-cards.md）

> 证据基线：BASELINE_HEAD=0b836e7（脏基线，工作区快照）。行数引用 01-explore/inventory.json。
> 覆盖：inventory `src_files` 全部 71 项 + `tracked_executables` 全部 10 项（9 文件 + measure_token_overhead.md 扫读并入 .py 卡）。

## >500 行全文必读集合（20 个，数据驱动生成自 inventory）

```
  2190  src/oc_slimapi/sse/tokenstream/hub.py
  1643  src/oc_slimapi/routes/messages.py
  1177  src/oc_slimapi/skeleton.py
  1158  src/oc_slimapi/config.py
  1090  src/oc_slimapi/sse/global_hub.py
   975  src/oc_slimapi/actions.py
   883  src/oc_slimapi/routes/sessions.py
   874  src/oc_slimapi/sse/tokenstream/subscriber.py
   845  src/oc_slimapi/traffic.py
   785  src/oc_slimapi/app.py
   770  src/oc_slimapi/singleflight.py
   768  src/oc_slimapi/dbaux/lifecycle.py
   739  src/oc_slimapi/selector.py
   726  src/oc_slimapi/access_log.py
   630  src/oc_slimapi/routes/read_groups.py
   598  src/oc_slimapi/sse/replay_log.py
   583  src/oc_slimapi/routes/write_groups.py
   541  src/oc_slimapi/traffic_snapshot.py
   526  src/oc_slimapi/routes/permissions.py
   504  src/oc_slimapi/routes/questions.py
```

## 卡片正文（按依赖自底向上：核心语义层 → 配置/上游 → 投影/并发/缓存 → 记账/观测 → dbaux → sse → routes → middleware → proxy → app → 资产）

<!-- ==== e1-16-core-semantics ==== -->
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

<!-- ==== e1-04-config ==== -->
# E1-04 精读卡片 — src/oc_slimapi/config.py

> 审计探索产物（只读精读），2026-08-20。全文 1158 行已逐行读取，无抽样。
> 引用格式 `src/oc_slimapi/config.py:行号`（省略前缀时均指本文件）。

### src/oc_slimapi/config.py（1158）

## 职责

环境变量唯一的配置入口：`@dataclass(frozen=True, slots=True) Settings` 在**导入期**读取全部 `OC_SLIMAPI_*` 环境变量并实例化为模块级单例 `settings`（:1158）；`Settings.validate()`（:736-1155）在 app lifespan 启动期做 fail-closed 校验（app.py:196 与 :771 两处调用）。唯一非 env 配置例外是只读 actions manifest 文件（`OC_SLIMAPI_ACTIONS_FILE`，模块 docstring :1）。文件还承载三类非 env 内容：

1. token-stream 代码级预算常量（`TOKEN_*`，:46-101，非生产 ops 旋钮，仅 DEBUG env 可越权覆盖其中 3 个）；
2. 校验上限常量（`_MAX_*` / `_MIN_EXPAND_RESPONSE_BYTES`，:106-128）+ 导入期 assert 不变量（:142-163）；
3. directory-allowlist 的 canonical 匹配函数族（`allowlist_roots` / `candidate_canonical` / `match_allowlist` / `directory_allowed`，:255-351，被 read_groups.py 403 门与 global_hub.py SSE 帧过滤共用）。

## 对外符号

### 模块级常量（代码级，非 env 可直接改）

| 常量 | 值 | 行号 | 说明 |
|---|---|---|---|
| `TOKEN_PART_MAX_BYTES` | 1 MiB | :46 | 单 part 累积上限 |
| `TOKEN_LIVE_PARTS_MAX` | 32 | :47 | 全局活跃 LivePart 数上限（C5） |
| `TOKEN_LIVEPARTS_MAX_BYTES` | 4 MiB | :48 | 全局 LivePart 字节上限（Stage E 拆分） |
| `TOKEN_PENDING_MAX_BYTES` | 4 MiB | :49 | 全局未 flush 字节上限 |
| `TOKEN_FLUSH_SECONDS` | 0.1 | :50 | 100 ms flush 窗口 |
| `TOKEN_FLUSH_BYTES` | 4096 | :51 | 4 KiB 提前 flush 阈值 |
| `TOKEN_ACC_IDLE_MS` | 60_000 | :52 | 孤儿 LivePart 60 s idle TTL |
| `TOKEN_HEARTBEAT_SECONDS` | 15 | :53 | SSE keepalive |
| `TOKEN_DISABLED_MAX` | 4096 | :59 | tombstone 有界 map 容量（同 revision cap） |
| `TOKEN_DISABLED_TTL_S` / `_MS` | 300 / 300000 | :60-61 | tombstone TTL |
| `TOKEN_RESYNC_QUEUE_CAP` | 64 | :67 | resink 队列上限 |
| `TOKEN_REMOVED_MESSAGES_MAX` | 1000 | :75 | removed 消息 replay FIFO 上限 |
| `TOKEN_REMOVED_MESSAGES_TTL_MS` | 24 h | :76 | removed TTL |
| `DEFAULT_TOKEN_MAX_FRAME_BYTES` | 1 MiB | :81 | hub 未接线时的缺省帧上限 |
| `TOKEN_HANDSHAKE_ITEMS` | 2048 | :100 | 握手 deque item 上限（assert 锁死下界） |
| `TOKEN_HANDSHAKE_BUFFER_BYTES` | 8 MiB | :101 | 握手 deque 字节上限 |
| `_MAX_MESSAGE_BYTES_CAP` / `_MAX_RESPONSE_BYTES_CAP` | 256 MiB | :106-107 | P1-35 sanity 上限 |
| `_MAX_TRANSFORM_TOTAL_BYTES` | 512 MiB | :112 | P1-30 RSS 上界 |
| `_MAX_RAW_PLUS_TRANSFORM_TOTAL_BYTES` | 576 MiB | :116-118 | raw-fetch + transform 聚合内存界 |
| `_MIN/_MAX_EXPAND_RESPONSE_BYTES` | 1 KiB / 32 MiB | :127-128 | expand 片段窗口 |
| `_ACCESS_LOG_DIR_DEFAULT` / `_ACCESS_LOG_PATH_DEFAULT` | "logs" / "logs/access.jsonl" | :134-135 | 区分"未设"与"显式设为默认值" |
| `_ALLOWLIST_ROOTS_CACHE` | `dict` | :252 | 模块级可变缓存（见状态节） |

导入期 assert（:142-146 `TOKEN_LIVE_PARTS_MAX <= TOKEN_DISABLED_MAX`；:153-160 `TOKEN_HANDSHAKE_ITEMS >= TOKEN_REMOVED_MESSAGES_MAX + 1 + TOKEN_LIVE_PARTS_MAX`；:161-163 握手字节 > 0）：改代码级常量破坏不变量 → **导入即 AssertionError**（env 不可触发）。

### 模块级函数

| 函数 | 行号 | 说明 |
|---|---|---|
| `_version_range(value)` | :166-171 | 解析 `"min,max"`；畸形 → `RuntimeError("OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS must be min,max")`（导入期） |
| `_opt_int_env(name)` | :174-180 | 可选 int env：未设/空白 → None；非整数 → **裸 ValueError**（导入期，不点名变量） |
| `_int_env(name, default)` | :183-197 | int env：未设 → default；畸形 → `RuntimeError(f"{name} must be an integer")`（**具名**，仅 3 个字段用） |
| `_directory_allowlist_env()` | :200-213 | 三态解析：env 未设 → None；`""` → `[]`；否则按 `:` 拆分，空白段保留为 `""`（留给 validate 拒绝），条目 `normpath(normalize_directory(...))` |
| `clear_allowlist_roots_cache()` | :255-265 | 配置（重）应用信号：清 `_ALLOWLIST_ROOTS_CACHE`；由 `Settings.validate()`（:757）与 `GlobalHub.set_directory_allowlist()` 调用 |
| `allowlist_roots(allowlist)` | :268-291 | 根 canonical 化（realpath），**按值缓存**；解析失败（OSError/ValueError）的条目跳过（fail-closed：不可解析即不授权） |
| `candidate_canonical(directory)` | :294-318 | 候选**实时** canonical 化（绝不缓存）；非 str/空/相对路径/解析失败 → None（调用方 fail-closed） |
| `match_allowlist(roots, canonical)` | :321-338 | 边界对齐前缀匹配（`canonical == root` / `root == "/"` / `canonical.startswith(root + "/")`）；POSIX 字节大小写敏感 |
| `directory_allowed(allowlist, directory)` | :341-351 | 便捷链：cached roots vs realtime candidate；三态门控留给调用方 |

### Settings 类 — 字段全清单（71 个）

> 行为分类缩写：**导入崩溃** = `settings = Settings()`（:1158）在 import 期解析 env，畸形数值 → 裸 `int()/float()` ValueError（**消息不含 env 名**）；**启动拒绝** = lifespan `validate()` 抛 `RuntimeError`（fail-closed）；**静默 False** = 布尔旋钮任意非真值字符串静默解释为关闭（feature 默认 true，垃圾值 → 功能关闭，无告警）；**无校验**。

| # | 字段 | env | 默认值 | 校验规则（validate 行号） | 非法值行为 | 定义行 |
|---|---|---|---|---|---|---|
| 1 | `host` | `OC_SLIMAPI_HOST` | `"127.0.0.1"` | ∈ {127.0.0.1, ::1, localhost, 0.0.0.0}（:773） | 启动拒绝（"must be loopback or 0.0.0.0"） | :356 |
| 2 | `port` | `OC_SLIMAPI_PORT` | `4097` | 1 ≤ port ≤ 65535，0 不支持（:786） | 数值畸形→导入崩溃；越界→启动拒绝 | :357 |
| 3 | `upstream` | `OC_SLIMAPI_UPSTREAM` | `http://127.0.0.1:4096`（rstrip "/"） | scheme==http 且 hostname∈loopback（:779）；无 user/pass/query/fragment（:781） | 启动拒绝（"must be fixed loopback HTTP" / "must not contain credentials..."） | :358 |
| 4 | `max_message_bytes` | `OC_SLIMAPI_MAX_MESSAGE_BYTES` | 32 MiB | 仅上限 ≤ 256 MiB（:845）；**无 >0 下界**（见疑问 Q3） | 畸形→导入崩溃；>256 MiB→启动拒绝；0/负数→**通过** | :359 |
| 5 | `max_transforms` | `OC_SLIMAPI_MAX_TRANSFORMS` | `1` | ≥1（:837）；×max(resp,expand) ≤ 512 MiB（:878-892） | 启动拒绝 | :363 |
| 6 | `transform_wait_seconds` | `OC_SLIMAPI_TRANSFORM_WAIT_SECONDS` | `2`（float） | >0（:839）；**无 isfinite**（见 Q4） | 畸形→导入崩溃；≤0→启动拒绝；nan→**通过** | :364 |
| 7 | `max_response_bytes` | `OC_SLIMAPI_MAX_RESPONSE_BYTES` | 64 MiB | >0（:841）且 ≤256 MiB（:850） | 启动拒绝 | :365 |
| 8 | `max_expand_response_bytes` | `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` | 8 MiB | ∈[1 KiB, 32 MiB]（:860） | 畸形→**具名** RuntimeError（_int_env :197）；越界→启动拒绝 | :372-374 |
| 9 | `catalog_cache_ttl_seconds` | `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS` | `300`（float） | ≥0（0=禁用缓存，:930）；无 isfinite | 畸形→导入崩溃；负→启动拒绝；nan→**通过** | :380-382 |
| 10 | `catalog_cache_max_entries` | `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRIES` | `16` | ≥1（:935） | 启动拒绝 | :383-385 |
| 11 | `catalog_cache_max_bytes` | `OC_SLIMAPI_CATALOG_CACHE_MAX_BYTES` | 16 MiB | ≥1 MiB（:937，**下界高**） | 启动拒绝 | :386-388 |
| 12 | `catalog_cache_max_entry_bytes` | `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRY_BYTES` | 1 MiB | ≥1（:942）且 ≤ max_bytes（:948） | 启动拒绝 | :389-391 |
| 13 | `coalesce_enabled` | `OC_SLIMAPI_COALESCE_ENABLED` | `true` | 无 | 静默 False（绕过合并注册表） | :400-402 |
| 14 | `raw_fetch_concurrency` | `OC_SLIMAPI_RAW_FETCH_CONCURRENCY` | `4` | ≥1（:907） | 启动拒绝 | :403-405 |
| 15 | `raw_fetch_max_bytes` | `OC_SLIMAPI_RAW_FETCH_MAX_BYTES` | 64 MiB | >0（:909）；raw+transform ≤576 MiB 聚合（:911-923） | 启动拒绝 | :406-408 |
| 16 | `etag_enabled` | `OC_SLIMAPI_ETAG_ENABLED` | `true` | 无 | 静默 False（ETag/304 全关，字节回退） | :414-416 |
| 17 | `message_fingerprint_enabled` | `OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED` | `true` | 无 | 静默 False（`contentFingerprint` 字段全省略） | :425-427 |
| 18 | `smoke_session_id` | `OC_SLIMAPI_SMOKE_SESSION_ID` | None | 无校验 | 任意字符串照单全收 | :428 |
| 19 | `server_api_version` | ~~`OC_SLIMAPI_SERVER_API_VERSION`~~（**已废弃**） | 常量 `SERVER_API_VERSION`=4（钉死，非 env） | ≥1（:806）且 ∈accepted 区间（:827）——常量下均为不变式 | env 存在→**warning+忽略**（:796-804），启动不破 | :436 |
| 20 | `accepted_client_versions` | `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `"3,4"`（来自 ACCEPTED_CLIENT_VERSIONS） | 语法解析导入期（:170 RuntimeError）；**必须严格等于 (3,4)**（:817-822，P1-13 fail-closed 钉死，不可加宽/收窄） | 畸形→导入崩溃（具名）；≠(3,4)→启动拒绝 | :437-442 |
| 21 | `max_subscribers_per_directory` | `OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY` | `8` | ≥1（:978） | 启动拒绝 | :447-449 |
| 22 | `max_total_subscribers` | `OC_SLIMAPI_MAX_TOTAL_SUBSCRIBERS` | `16` | ≥ per_directory（:980） | 启动拒绝 | :450 |
| 23 | `sse_queue_items` | `OC_SLIMAPI_SSE_QUEUE_ITEMS` | `256` | ≥2（:984，溢出终态路径需容纳 resync+STOP 两帧） | 启动拒绝 | :451 |
| 24 | `sse_buffer_bytes` | `OC_SLIMAPI_SSE_BUFFER_BYTES` | 2 MiB | >0（:995） | 启动拒绝 | :452 |
| 25 | `sse_max_frame_bytes` | `OC_SLIMAPI_SSE_MAX_FRAME_BYTES` | 256 KiB | >0（:997） | 启动拒绝 | :453 |
| 26 | `token_stream_max_subscribers` | `OC_SLIMAPI_TOKEN_STREAM_MAX_SUBSCRIBERS` | `8` | ≥1（:1004） | 启动拒绝 | :466-468 |
| 27 | `token_stream_queue_items` | `OC_SLIMAPI_TOKEN_STREAM_QUEUE_ITEMS` | `64` | ≥2（:1006） | 启动拒绝 | :469 |
| 28 | `token_stream_buffer_bytes` | `OC_SLIMAPI_TOKEN_STREAM_BUFFER_BYTES` | 512 KiB | >0（:1013） | 启动拒绝 | :470-472 |
| 29 | `token_stream_max_frame_bytes` | `OC_SLIMAPI_TOKEN_STREAM_MAX_FRAME_BYTES` | 1 MiB | >0（:1015） | 启动拒绝 | :473-475 |
| 30 | `token_stream_debug_live_budget_bytes` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_BUDGET_BYTES` | None | 设了须 >0（:1019）；DEBUG 专用 | 畸形→**裸** ValueError（导入）；≤0→启动拒绝 | :482-484 |
| 31 | `token_stream_debug_part_max_bytes` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_PART_MAX_BYTES` | None | 设了须 >0（:1023） | 同上 | :485-487 |
| 32 | `token_stream_debug_live_parts_max` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_PARTS_MAX` | None | 设了须 >0 且 ≤ TOKEN_DISABLED_MAX=4096（:1027-1041，防 revision 回退） | 同上 + 越上界启动拒绝 | :488-490 |
| 33 | `shell_deny_list_enabled` | `OC_SLIMAPI_SHELL_DENY_LIST_ENABLED` | `1`（true） | 无；ops break-glass，关闭≠安全隔离（:491-494 注释） | 静默 False（deny-list 关闭） | :495-497 |
| 34 | `directory_allowlist` | `OC_SLIMAPI_DIRECTORY_ALLOWLIST` | None（三态） | 条目：非空/绝对/无 `\0`/无控制字符/≤4096（:738-750）；realpath 可解析（:758-766） | None=**不过滤**（默认放行）；`""`→`[]`（/file 路由 reject-all，SSE hub 不过滤——见 Q5）；坏条目→启动拒绝 | :498 |
| 35 | `deployment_revision` | `OC_SLIMAPI_DEPLOYMENT_REVISION` | None | 无（best-effort） | 无 | :501 |
| 36 | `deployment_revision_file` | `OC_SLIMAPI_DEPLOYMENT_REVISION_FILE` | None | 无；读文件失败→warning+None（:700-707） | warning+忽略 | :502 |
| 37 | `actions_file` | `OC_SLIMAPI_ACTIONS_FILE` | None（空串→None） | 无（actions.py 自行处理） | 无 | :507 |
| 38 | `actions_max_concurrent` | `OC_SLIMAPI_ACTIONS_MAX_CONCURRENT` | `4` | ≥1（:1044） | 启动拒绝 | :509 |
| 39 | `traffic_metrics_enabled` | `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `true` | 无 | 静默 False | :516-518 |
| 40 | `access_log_enabled` | `OC_SLIMAPI_ACCESS_LOG_ENABLED` | `true` | 无 | 静默 False | :519-521 |
| 41 | `access_log_path` | `OC_SLIMAPI_ACCESS_LOG_PATH`（**DEPRECATED**） | `"logs/access.jsonl"` | 无直接校验；仅当 `OC_SLIMAPI_ACCESS_LOG_DIR` 未设且值≠默认时，其 parent 作为回退目录（:711-734）+ app.py:210-215 warning | warning+回退（不破启动） | :527 |
| 42 | `access_log_dir` | `OC_SLIMAPI_ACCESS_LOG_DIR` | `"logs"` | 显式设置永远压过废弃 path（:727-734） | 无 | :533 |
| 43 | `access_log_compress_on_startup` | `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | `true` | 无 | 静默 False | :534-536 |
| 44 | `access_log_retain_days` | `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0`（=不清理） | ≥0（:1054） | 启动拒绝 | :537 |
| 45 | `access_log_maintenance_interval_s` | `OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S` | `3600` | ≥60（:1050，防热循环） | 启动拒绝 | :538-540 |
| 46 | `traffic_snapshot_enabled` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `true` | 无 | 静默 False | :546-548 |
| 47 | `traffic_snapshot_interval_s` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S` | `300` | ≥1（:1056） | 启动拒绝 | :549-551 |
| 48 | `traffic_snapshot_path` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `"logs/traffic-snapshot.jsonl"` | 无 | 无 | :552-554 |
| 49 | `traffic_snapshot_retain_days` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` | `0`（=不清理；生产 systemd 设 30） | ≥0（:1059） | 启动拒绝 | :561-563 |
| 50 | `client_id_hash` | `OC_SLIMAPI_CLIENT_ID_HASH` | `true`（fail-closed 哈希） | 无 | 静默 False（明文记 device id——隐私回退） | :571-573 |
| 51 | `client_id_salt` | `OC_SLIMAPI_CLIENT_ID_SALT` | None | 无（有则 HMAC-SHA256，无则裸 SHA-256） | 无 | :574 |
| 52 | `skeleton_inline_output_max_bytes` | `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES` | 4 KiB | >0（:955）且 ≤16 MiB（:966） | 启动拒绝 | :579-581 |
| 53 | `skeleton_inline_output_max_message_bytes` | `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES` | 16 KiB | >0（:959）且 ≤16 MiB（:970） | 启动拒绝 | :582-584 |
| 54 | `questions_max_response_bytes` | `OC_SLIMAPI_QUESTIONS_MAX_RESPONSE_BYTES` | 2 MiB | >0（:1068） | 启动拒绝 | :590-592 |
| 55 | `questions_max_aggregate_bytes` | `OC_SLIMAPI_QUESTIONS_MAX_AGGREGATE_BYTES` | 16 MiB | ≥ per-dir（:1072）且 ≤128 MiB（:1077） | 启动拒绝 | :593-595 |
| 56 | `questions_fanout_concurrency` | `OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY` | `8` | ∈[1,16]（:1064） | 启动拒绝 | :596-598 |
| 57 | `permissions_max_response_bytes` | `OC_SLIMAPI_PERMISSIONS_MAX_RESPONSE_BYTES` | 2 MiB | >0（:1090） | 启动拒绝 | :611-613 |
| 58 | `permissions_fanout` | `OC_SLIMAPI_PERMISSIONS_FANOUT` | `8` | ∈[1,16]（:1086） | 启动拒绝 | :614-616 |
| 59 | `permissions_max_aggregate_bytes` | `OC_SLIMAPI_PERMISSIONS_MAX_AGGREGATE_BYTES` | 16 MiB | ≥ per-dir（:1094）且 ≤128 MiB（:1099） | 启动拒绝 | :617-619 |
| 60 | `qp_sweep_enabled` | `OC_SLIMAPI_QP_SWEEP_ENABLED` | `true` | 无 | 静默 False | :622-624 |
| 61 | `qp_sweep_interval_seconds` | `OC_SLIMAPI_QP_SWEEP_INTERVAL_SECONDS` | `1800.0`（float） | >0（:1103）；**无 isfinite** | 畸形→导入崩溃；≤0→启动拒绝；nan→**通过** | :625-627 |
| 62 | `qp_sweep_daily_budget` | `OC_SLIMAPI_QP_SWEEP_DAILY_BUDGET` | `100` | ≥0（:1130） | 启动拒绝 | :628-630 |
| 63 | `merged_fanout` | `OC_SLIMAPI_MERGED_FANOUT` | `8` | ∈[1,16]（:1132） | 启动拒绝 | :631-633 |
| 64 | `merged_max_fulls_per_page` | `OC_SLIMAPI_MERGED_MAX_FULLS_PER_PAGE` | `16` | ∈[1,64]（:1136） | 启动拒绝 | :634-636 |
| 65 | `merged_max_bytes` | `OC_SLIMAPI_MERGED_MAX_BYTES` | 8 MiB | >0（:1140）且 ≤128 MiB（:1144） | 启动拒绝 | :637-639 |
| 66 | `transform_absorb_budget_seconds` | `OC_SLIMAPI_TRANSFORM_ABSORB_BUDGET_SECONDS` | `2.5`（float） | >0（:1148）；**无 isfinite** | nan→**通过** | :640-642 |
| 67 | `state_dir` | `OC_SLIMAPI_STATE_DIR` | `"state"`（相对路径） | 非空/非纯空白（:1154） | 启动拒绝 | :648 |
| 68 | `dbaux_probe_interval_s` | `OC_SLIMAPI_DBAUX_PROBE_INTERVAL_S` | `30`（float） | >0（:1109）；**无 isfinite** | nan→**通过** | :658-660 |
| 69 | `replay_max_count` | `OC_SLIMAPI_REPLAY_COUNT`（env 名无 MAX_ 前缀） | `2048` | ≥1（:1117） | 畸形→**具名** RuntimeError；越界→启动拒绝 | :669 |
| 70 | `replay_max_bytes_kb` | `OC_SLIMAPI_REPLAY_BYTES_KB`（**单位 KiB**） | `65536`（=64 MiB） | ≥1（:1119）；×1024 换算在 app.py:429 | 畸形→具名 RuntimeError；<1→启动拒绝 | :670 |
| 71 | `replay_ttl_s` | `OC_SLIMAPI_REPLAY_TTL_S` | `900`（float，秒） | `math.isfinite` 且 >0（:1121，**唯一**带 nan/inf 防护的 float 旋钮） | 畸形→导入崩溃；nan/inf/≤0→启动拒绝（消息含 got {value!r}） | :671 |

### Settings 方法

| 方法 | 行号 | 说明 |
|---|---|---|
| `read_deployment_revision()` | :673-709 | env 优先（strip 后空白视为空，P1-40）；否则文件（`CREDENTIALS_DIRECTORY` 回退 :691-692）；未设/NotFound→静默 None；Permission/Unicode/OSError→warning+None（lazy import logging_config :703） |
| `effective_access_log_dir()` | :711-734 | 废弃优先级：仅当 `OC_SLIMAPI_ACCESS_LOG_DIR` **未出现在 os.environ** 且 `access_log_path != 默认` 时用废弃 path 的 parent；返回 `(dir, deprecated_used)` |
| `validate()` | :736-1155 | 全部 fail-closed 校验（见错误路径节）；:757 顺带 `clear_allowlist_roots_cache()` |

### 模块级单例

- `settings = Settings()`（:1158）——**导入期**求值：所有 env 读取/解析都发生在 import 时；此后进程内不再读 env（例外：`read_deployment_revision` 的 CREDENTIALS_DIRECTORY、`effective_access_log_dir` 的 `in os.environ` 探测在调用期）。

## 依赖（内部 imports）

- `.directory.normalize_directory`（:12；实现 directory.py:12-20，纯 rstrip("/")，根 "/" 保留）
- `.versioning.ACCEPTED_CLIENT_VERSIONS, SERVER_API_VERSION`（:13；versioning.py:38 `SERVER_API_VERSION = 4`，:44 `ACCEPTED_CLIENT_VERSIONS = (3, 4)`）
- lazy：`.logging_config.get_logger`（:703、:797——仅 warning 路径，避免导入环）
- 标准库：dataclasses（dataclass/field）、math（isfinite）、os、pathlib.Path、typing.Any、urllib.parse.urlsplit

## 被依赖（主要使用方）

| 使用方 | 用法 |
|---|---|
| `src/oc_slimapi/app.py:21` | `from .config import settings`；lifespan 两处 `settings.validate()`（:196、:771）；`app.state.config = settings`（:197）后几乎所有子系统 wiring：transform pool（:321-323）、catalog cache（:362-365）、coalesce（:384-387）、GlobalHub SSE 限额（:479-483）+ allowlist（:492）、token hub（:539-566）、`apply_debug_budget_overrides(settings)`（:307）、TurnRegistry state_dir（:583）、dbaux probe（:601）、ReplayLog（:428-430，`max_bytes=settings.replay_max_bytes_kb * 1024` 的 KiB→B 换算在此）、snapshot（:668-720）、smoke（:138） |
| `src/oc_slimapi/routes/read_groups.py:51,140-141` | `allowlist_roots/candidate_canonical/match_allowlist` —— /file 族 403 `directory_not_allowed` 门；None→原样透传，[]→`allowlist_roots([])=()` → 恒 False → reject-all |
| `src/oc_slimapi/sse/global_hub.py:22-23,534,572-585` | `clear_allowlist_roots_cache` / `directory_allowed` —— SSE 帧过滤；**`if not allowlist: return True`（:574）把 None 和 [] 都放行** |
| `src/oc_slimapi/upstream.py:8,40` | `Settings` 作类型标注（`create_client(config: Settings)`） |
| `src/oc_slimapi/routes/versions.py:130,148` | `settings.max_expand_response_bytes` → `fragmentMaxBytes` |
| `src/oc_slimapi/routes/health.py:40-50,72,87,133-139` | `accepted_client_versions`（clientMin/clientMax）、skeleton inline caps、deployment_revision |
| `src/oc_slimapi/actions.py:424-433` | `settings.actions_file`（None=功能关闭）、`settings.actions_max_concurrent` |
| `src/oc_slimapi/etag.py:45-46,73-96`、`providers_projection.py:121` | skeleton caps 进 ETag REP_VERSION；`etag_enabled`/`message_fingerprint_enabled` 开关（getattr 带默认 True） |
| `src/oc_slimapi/routes/messages.py:545-546,846-1083` | `max_message_bytes`（/full 413 界）、skeleton inline caps、fingerprint 开关 |
| `src/oc_slimapi/middleware/traffic_accounting.py:311-313` | `client_id_hash`（getattr 默认 True）、`client_id_salt` |
| `src/oc_slimapi/sse/tokenstream/hub.py:126` | `apply_debug_budget_overrides` 消费 3 个 DEBUG 字段，**运行时改写 hub 模块全局** TOKEN_LIVEPARTS_MAX_BYTES / TOKEN_PART_MAX_BYTES / TOKEN_LIVE_PARTS_MAX |
| tests | `tests/test_config.py` 专测 + 约 80 个测试文件 import config/settings/Settings |

## 状态 / 可变性

- `Settings` 为 `frozen + slots` dataclass：实例不可重绑字段；但 `directory_allowlist: list[str]` 是**可变 list 挂在 frozen 实例上**（:498）——冻结只防 `settings.directory_allowlist = x`，不防 list 原地变异（仓内无变异点，纯防御性事实）。
- **env 快照时点 = import 时**（:1158）。uvicorn reload / 测试 monkeypatch env 后必须重新 import 才生效；`validate()` 不再读数值 env（除 :727 的存在性探测与 :796 的废弃探测）。
- 模块级可变状态两处：`_ALLOWLIST_ROOTS_CACHE`（:252，按 allowlist 值缓存 canonical roots，`clear_allowlist_roots_cache` 清空，无淘汰——键空间=进程内出现过的 distinct allowlist 值）；hub.py 的 `TOKEN_*` 全局被 `apply_debug_budget_overrides` 就地改写（跨模块可变状态，仅 DEBUG env 设置时发生）。
- allowlist roots 的 ops 语义（:234-241）：root 自身被 symlink 重定向后**不影响**已缓存判定，直至配置重应用或进程重启——有意的运维语义。
- 导入期 assert（:142-163）意味着常量误编辑会让任何 import 该模块的进程（含 pytest 收集）直接崩。

## 错误路径（全部抛点）

### 导入期（`settings = Settings()` :1158 或模块 assert）

| 行号 | 异常 | 触发 / 消息关键词 |
|---|---|---|
| :142, :153, :161 | AssertionError | 代码级常量不变量被破坏（env 不可达） |
| :170 | RuntimeError | `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS must be min,max`（畸形 min,max） |
| :180 | ValueError（**裸**） | 3 个 DEBUG 字段非整数（`int()` 直抛，不点名） |
| :197 | RuntimeError（具名） | `{name} must be an integer` —— 仅 `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` / `OC_SLIMAPI_REPLAY_COUNT` / `OC_SLIMAPI_REPLAY_BYTES_KB` |
| :357, :359, :363-408, :447-539... | ValueError（**裸**） | 其余全部 `int(os.getenv(...))` / `float(...)` 字段：畸形数值 → 裸异常，**消息不含 env 名**（_int_env docstring :184-189 自认此诊断缺口） |

### lifespan `validate()`（全部 RuntimeError，fail-closed；行号=raise 处）

| 行号 | 消息关键词（env 名） |
|---|---|
| :747 | DIRECTORY_ALLOWLIST entries must be non-empty absolute...（坏条目） |
| :762 | DIRECTORY_ALLOWLIST ... canonically resolvable（realpath 失败） |
| :774 | HOST must be loopback or 0.0.0.0 |
| :779 | UPSTREAM must be fixed loopback HTTP |
| :781 | UPSTREAM must not contain credentials/query/fragment |
| :786 | PORT must be in [1, 65535] |
| :799 | （**warning 非 error**）SERVER_API_VERSION is deprecated and ignored |
| :806 | slimapi version configuration is invalid |
| :817 | ACCEPTED_CLIENT_VERSIONS must be (3, 4) — fail-closed to the pinned range |
| :827 | SERVER_API_VERSION ... must be within ... range（常量钉死后实际不可达，见 Q2） |
| :837/:839/:841 | MAX_TRANSFORMS >= 1 / TRANSFORM_WAIT_SECONDS > 0 / MAX_RESPONSE_BYTES > 0 |
| :845/:850 | MAX_MESSAGE_BYTES / MAX_RESPONSE_BYTES <= 256 MiB |
| :860 | MAX_EXPAND_RESPONSE_BYTES in [1 KiB, 32 MiB] |
| :882 | MAX_TRANSFORMS × max(...) exceeds 512 MiB — risk of OOM |
| :907/:909/:912 | RAW_FETCH_CONCURRENCY >= 1 / RAW_FETCH_MAX_BYTES > 0 / raw+transform exceeds 576 MiB |
| :930-:953 | CATALOG_CACHE_TTL_SECONDS >= 0 / MAX_ENTRIES >= 1 / MAX_BYTES >= 1 MiB / MAX_ENTRY_BYTES >= 1 且 <= MAX_BYTES |
| :955-:973 | SKELETON_INLINE_OUTPUT_MAX_BYTES(_MESSAGE_BYTES) > 0 且 <= 16 MiB |
| :978-:998 | MAX_SUBSCRIBERS_PER_DIRECTORY >= 1 / MAX_TOTAL >= PER_DIRECTORY / SSE_QUEUE_ITEMS >= 2 / SSE_BUFFER_BYTES > 0 / SSE_MAX_FRAME_BYTES > 0 |
| :1004-:1041 | TOKEN_STREAM_MAX_SUBSCRIBERS >= 1 / QUEUE_ITEMS >= 2 / BUFFER_BYTES > 0 / MAX_FRAME_BYTES > 0 / 3 个 DEBUG 字段 > 0 when set / DEBUG_LIVE_PARTS_MAX <= TOKEN_DISABLED_MAX |
| :1044 | ACTIONS_MAX_CONCURRENT >= 1 |
| :1050-:1060 | ACCESS_LOG_MAINTENANCE_INTERVAL_S >= 60 / ACCESS_LOG_RETAIN_DAYS >= 0 / TRAFFIC_SNAPSHOT_INTERVAL_S >= 1 / TRAFFIC_SNAPSHOT_RETAIN_DAYS >= 0 |
| :1064-:1080 | QUESTIONS_FANOUT_CONCURRENCY in [1,16] / QUESTIONS_MAX_RESPONSE_BYTES > 0 / QUESTIONS_MAX_AGGREGATE >= per-dir 且 <= 128 MiB |
| :1086-:1106 | PERMISSIONS_FANOUT in [1,16] / PERMISSIONS_MAX_RESPONSE_BYTES > 0 / PERMISSIONS_MAX_AGGREGATE 同型 / QP_SWEEP_INTERVAL_SECONDS > 0 |
| :1109 | DBAUX_PROBE_INTERVAL_S > 0 |
| :1117-:1129 | REPLAY_COUNT >= 1 / REPLAY_BYTES_KB >= 1 / REPLAY_TTL_S must be a finite number > 0 (got ...) |
| :1130-:1151 | QP_SWEEP_DAILY_BUDGET >= 0 / MERGED_FANOUT in [1,16] / MERGED_MAX_FULLS_PER_PAGE in [1,64] / MERGED_MAX_BYTES > 0 且 <= 128 MiB / TRANSFORM_ABSORB_BUDGET_SECONDS > 0 |
| :1154 | STATE_DIR must be non-empty |

非抛点降级：`read_deployment_revision` 读文件失败 → warning + None（:700-707）；`OC_SLIMAPI_SERVER_API_VERSION` 存在 → warning + 忽略（:796-804）；`OC_SLIMAPI_ACCESS_LOG_PATH` 废弃回退 → app.py:210-215 warning。

## 疑问点（可疑处，含行号）

1. **Q1 — AGENTS.md 与 versioning.py 钉值漂移**：仓库 AGENTS.md 称 `ACCEPTED_CLIENT_VERSIONS` 当前 `[3,3]`，实际 versioning.py:44 为 `(3, 4)`（4.0.0 双版本窗口）。config.py:817 的钉死校验用 `!= ACCEPTED_CLIENT_VERSIONS` 常量比较，逻辑正确；漂移在文档侧（AGENTS.md 未随 4.0.0 更新）。
2. **Q2 — OC_SLIMAPI_SERVER_API_VERSION 废弃语义**：:796-804 确为 **warning+忽略**（env 出现即告警，值不解读，启动永不破——与 docstring "settable without breaking startup" 一致）。但 :827-832 的区间校验错误消息仍点名 `OC_SLIMAPI_SERVER_API_VERSION`，而 `server_api_version` 已常量钉死为 4（:436），该分支在生产 env 下**不可达**（除非改 versioning.py 常量），错误消息具误导性（死错误路径 + 张冠李戴的 env 名）。
3. **Q3 — max_message_bytes 无下界**：:845 只查 `> 256 MiB` 上限，不查 `> 0`（对比 max_response_bytes :841 有 `<= 0` 检查）。`OC_SLIMAPI_MAX_MESSAGE_BYTES=0` 或负数可通过 validate；消费方 messages.py:545 `min(cap, config.max_message_bytes)` 会把每次 /full 上限压成 0/负 → 行为未定义（413 或 0 字节读）。疑似遗漏。
4. **Q4 — float 旋钮的 nan/inf 旁路（rev-gate MAJOR-1 只修了一处）**：:1121-1129 仅 `replay_ttl_s` 有 `math.isfinite` 防护（注释明说 nan 使 `age > ttl_s` 恒 False、TTL 驱逐静默失效）。但同型 `<= 0` 检查的 float 旋钮——`transform_wait_seconds`（:839）、`catalog_cache_ttl_seconds`（:930，nan 还会绕过 `< 0`）、`qp_sweep_interval_seconds`（:1103）、`dbaux_probe_interval_s`（:1109）、`transform_absorb_budget_seconds`（:1148）——`float("nan") <= 0` 均为 False，`OC_SLIMAPI_...=nan` 全部**通过校验**；inf 同理。是否算未修完的同族缺口值得后续核对（`float("inf")` 的 env 字符串 "inf"/"nan" 可被 float() 接受）。
5. **Q5 — directory allowlist 三态不对称**：默认 None = **完全放行**（不过滤，read_groups.py:139 原样返回、global_hub.py:574 放行）；`OC_SLIMAPI_DIRECTORY_ALLOWLIST=""` → `[]` 在 /file 路由 = reject-all（roots 为空 tuple 恒不匹配），但在 SSE hub（global_hub.py:574 `if not allowlist: return True`）= **不过滤**。同一 `[]` 值在两个执行点语义相反；config.py:346-349 docstring 明示这是有意设计，但运维设空串期望"全拒"时 SSE 帧仍外发——审计时建议按安全语义复核。另：`_directory_allowlist_env`（:209-211）把空白段保留为 `""` 条目交给 validate 拒绝（fail-closed，消息是通用的 entries 报错，不指明哪个段）。
6. **Q6 — 导入期裸 ValueError 诊断缺口**：:357-:671 间约 40 个字段用裸 `int(os.getenv(...))` / `float(...)`，畸形值在 **import 期**抛不含 env 名的 ValueError；_int_env（:183-197）的具名模式只覆盖 3 个字段（docstring 自认 "a bare ValueError at import time would not name the offending variable"）。排障时须靠 traceback 定位字段行。
7. **Q7 — REPLAY 三参数命名/单位陷阱**：env 名 `OC_SLIMAPI_REPLAY_COUNT` / `OC_SLIMAPI_REPLAY_BYTES_KB` / `OC_SLIMAPI_REPLAY_TTL_S`（:669-671）与字段名 `replay_max_count` / `replay_max_bytes_kb` / `replay_ttl_s` 不同形（env 无 MAX_ 前缀）；`replay_max_bytes_kb` 单位是 **KiB**，×1024 换算发生在 app.py:429 而非 config——运维直接写字节数会放大 1024 倍（仅受 :1119 `>= 1` 下界与上游 ReplayLog 自身约束，config 侧无字节上限校验，与 max_response_bytes 系列的 256 MiB sanity cap 风格不一致）。
8. **Q8 — 布尔旋钮静默 False**：13 个布尔字段（:400,414,425,495,516,519,534,546,571,622 等）接受任意字符串，非 {1,true,yes,on} 一律 False 且无告警——typo（如 "ture"）会**静默关闭** ETag、fingerprint、traffic metrics、client_id_hash（隐私回退：明文记录 device id）等默认开启的能力，无任何日志。
9. **Q9 — 冻结实例上的可变 list**：`directory_allowlist: list[str]`（:498）在 frozen dataclass 内是可变对象；仓内无变异点，但 `app.state.config` 全局共享该 list，任何下游原地 append 都会无声改变授权面（只读审计未发现实际变异者）。
10. **Q10 — `effective_access_log_dir` 依赖 env 存在性而非字段值**（:727）：判定"新 dir 是否显式设置"用 `"OC_SLIMAPI_ACCESS_LOG_DIR" in os.environ`，而字段值已在 import 时定格——若进程启动后有人 `os.environ` 补设/删除该键再调用此方法，判定与 `settings.access_log_dir` 值可能脱节（生产 lifespan 只调一次 :209，实际风险低）。
11. **Q11 — 废弃字段清单不完整披露**：代码内明确 DEPRECATED 的只有 `access_log_path`（:522-527，未用自轮转改造起）与 `OC_SLIMAPI_SERVER_API_VERSION`（:791-804，S-B04）。`server_api_version` 字段本身（:436）保留但已无 env 输入（仅作 health 展示与常量载体），`smoke_session_id`（:428）等无废弃标记。CHANGELOG 是否同步这两处废弃需在 E2/E3 阶段核对。
12. **Q12 — catalog_cache_max_bytes 下界 1 MiB 偏高**（:937）：想配 512 KiB 小缓存的部署会被启动拒绝（"must be >= 1 MiB"），与同组 `max_entry_bytes >= 1`（:942）的宽松风格不一致；属有意（注释自述）但反直觉。

<!-- ==== e1-03-skeleton-envelope-etag ==== -->
# E1-03 精读卡片：skeleton.py / envelope.py / etag.py

> 只读审计产物（2026-08-20）。引用格式 `src/oc_slimapi/...:行号`；全部行号基于当日工作区全文精读（非抽样）。

---

### src/oc_slimapi/skeleton.py（1177 行）

- **职责**：消息 / 会话 / 目录（command、agent）/ v4 DB 投影的纯投影函数库。把上游 opencode 的完整 JSON 消息树降为 ocdroid 消费的 thin skeleton（白名单 pick + thresholded inline + omitted/hasFull 标记 + expandRefs 生成），并承载 v3/v4 会话骨架的 canonical projector。模块自述为 "Pure v2-contract message/session projection functions"（:1，历史命名；实际现为 v3/v4 投影权威实现）。**不读全局 settings 单例**（:85-96 注释，P1-3 去单例化；限额经 `SkeletonLimits` 显式传入）。

- **对外符号**（逐顶层符号）：
  - `PLACEHOLDER_TEXT = "[内容已折叠，点开查看]"`（:12）：无可渲染 part 时注入的占位文本。
  - `PART_IDS = {"id","type","messageID","sessionID"}`（:13）：所有 part 类型共享的白名单基键。
  - `FINGERPRINT_VERSION = 1`（:41）：消息内容指纹版本；仅当规范化规则变化时 bump，不随包版本/REP_VERSION（:20-23）。
  - `FINGERPRINT_FIELD = "contentFingerprint"`（:42）。
  - `compute_message_fingerprint(message) -> str`（:45-59）：对投影后消息（排除自身 `contentFingerprint` 键）做 `orjson.dumps(OPT_SORT_KEYS)` + sha256 hex，输出 `v{N}:{hex}`，不截断（:52-59）。幂等安全（旧指纹被排除，:49-51）。
  - `recompute_fingerprint(message) -> None`（:62-64）：就地覆写消息指纹（merged 拼接点使用）。
  - `TOOL_KEYS = PART_IDS | {"tool","callID"}`（:65）：tool part 顶层白名单。
  - `TOOL_INPUT_KEYS = {"path","filePath","file_path","command","agent","description","subagent_type","todos"}`（:66-69）：`state.input` 白名单。
  - `TOOL_METADATA_KEYS = {"sessionId","sessionID","description","agent","diffStats"}`（:70）：`state.metadata` 白名单。
  - `FILE_URL_LIMIT = 8*1024`（:71）：file part url 内联长度上限。
  - `COMPACTION_PART_LIMIT = 64*1024`（:72）：单个 compaction part 原样保留的 JSON 字节上限。
  - `TEXT_INLINE_MAX_BYTES = 2048`（:81）：历史 3.1.x TextPart 上限；**3.2.0 起 text 恒逐字内联**，常量仅留作 expand 端点文档与测试基线（:77-80）。
  - `REASONING_INLINE_MAX_BYTES = 2048`（:82）：reasoning text 内联上限（UTF-8 字节；超限 → `text:null` + expandRef，绝不半截断，:74-76）。
  - `SkeletonLimits`（:97-114，frozen dataclass + slots）：per-call 投影开关——`field_bytes`（单字段 inline 上限）、`message_bytes`（单消息累计 inline 预算）、`fingerprint: bool = False`（B3 指纹开关，经 limits 线程穿越以不改 worker 签名，:107-110）。
  - `DEFAULT_SKELETON_LIMITS`（:119）：4 KiB / 16 KiB / fingerprint=False，供纯函数测试；生产由 routes 从 `request.app.state.config` 构造（:117-118）。
  - `SKELETON_INLINE_FIELDS = ("output","error")`（:145）：阈值内联候选。
  - `SKELETON_ALWAYS_OMIT_FIELDS = ("structured","result","raw","attachments")`（:146）：恒省略（attachments 有 expand 类别，其余 /full-only）。
  - `_pick(value, keys)`（:149-150）：白名单 deepcopy pick（缺键跳过）。
  - `_mark(part, omitted)`（:153-157）：非空 omitted → 置 `hasFull=True` + `omitted=sorted(set(...))`。
  - `_utf8_bytes_exceeds(text, limit)`（:160-164）：UTF-8 **wire 字节**阈值；非 str 永不超限。
  - `_expand_ref(category, message_id, part_id, sid, wire_view=3)`（:167-187）：构造单条 §5 expandRef；href = `/slimapi/messages/{sid}/expand/{category}/{mid}[/{partID}]?v={view}`，`v` 恒为第一个 query 键（:178-181，v4 §14 键序冻结）。
  - `_emit_expand_refs(part, refs, sid, wire_view=3)`（:190-214）：去重（按 (category, partID) 集合）+ 排序后挂 `expandRefs`；`sid` 缺或 part 无 `messageID` 或 refs 空 → 原样返回（reductions 仍生效，:205-209）；falsy partID 被过滤（M3，:212）。
  - `_field_byte_size(value)`（:217-231）：**唯一字节计量原语**——`len(orjson.dumps(value))`（含 JSON 引号/转义与嵌套），TypeError 回退 `str(value)` UTF-8 长度（:228-231）。
  - `_compute_diffstats(filediff)`（:234-280）：从 `state.metadata.filediff`（单 dict 或 list[dict]）算 `{additions,deletions,files}`；非识别形状 → None；缺/非数值子值按 0（:265-266,273-274）；list 的 `files=len(filediff)`（:270）。含 digest 对账 TODO 标注（:254-257，B.8 未实现）。
  - `_compute_diffstats_from_files(files)`（:283-309）：从 patch `files[]` 算 diffStats；纯 `string[]`（v1.18.16 形状）→ None，不造 0 值（rev-gpt R1-M1，:288-291,303-304）。
  - `_maybe_inline_state_field(thin_state, state, key, omitted, budget, limits)`（:312-341）：inline `state.output/error` 的判定核心——同时满足 per-field（`size <= field_bytes`，:331）与剩余预算（`budget["used"]+size <= message_bytes`，:332-335）才 deepcopy 内联并扣预算；否则记 `state.<key>` 进 omitted（全有或全无，:336-341）。budget 为 None → 仅 per-field 约束（:332-334）。
  - `_tool(part, *, budget=None, limits=DEFAULT, sid=None, wire_view=3)`（:344-416）：tool part 投影。state pick {status,title,time}；input/metadata 白名单 pick，非白名单键逐个记 omitted 并 collapse 为单条 `part_state_input_full`/`part_state_metadata_full` ref（:357-363,371-376）；output/error 走阈值 inline，被省略且原值非 `None/""` 时发 `part_state_output`/`part_state_error` ref（:380-385）；always-omit 四键（attachments 非空值时发 ref，:387-394）；diffStats 注入 `state.metadata.diffStats`（阈值之后注入故不可被省略，:395-411）；顶层未知键记 omitted（:413-415）。
  - `_patch(part, *, budget=None, limits=DEFAULT, sid=None)`（:419-478）：patch part 投影。保留 `hash`（P0 修复，:421-424）；`files` 为纯 string[] → 原样保留，含 dict 项 → 逐项 pick {path,additions,deletions,status}（:427-434）；metadata 只 pick `path`（:435-437）；output/error 与 tool **共享同一消息预算**（:450-454）；从 files[] 注入 diffStats，可无中生有 `state`/`metadata` 容器（:464-472）；**无 expand 类别**（:476-477，故无 wire_view 参数）。
  - `_file(part, *, sid=None, wire_view=3)`（:481-501）：file part 投影。url 仅当 str 且 http/https 前缀且 `len(url) <= FILE_URL_LIMIT` 才内联（:487-488）；否则 `url:null`+omitted+非空值发 `part_url` ref（:489-493）；`source` 恒省略+非空值发 `part_source` ref（:494-497）。
  - `skeleton_part(part, *, budget=None, limits=DEFAULT, sid=None, wire_view=3)`（:504-561）：**分派器**。入口先剥上游 `expandRefs`（sidecar-owned 键，:505-509）；`text` → 恒内联 text（3.2.0 决策，part_text 类别不再产出，:511-520）；`reasoning` → 超阈 `text:null`+`part_reasoning` ref（阈值在 _pick 前评估避免大文本被 deepcopy，n1，:521-534）；`tool`/`patch`/`file` → 分派；`step-start`/`step-finish` → 仅 PART_IDS + snapshot 非空时 `part_snapshot` ref（:541-549）；`compaction` → 整 part deepcopy，`len(orjson.dumps) <= 64KiB` 原样返回（无 _mark），超限 → `omitted:["*"]`+`compaction_full` ref（falsy id 无 ref，Lane-A，:550-560）；未知类型 → PART_IDS + 其余键 omitted（无键则 `["*"]`），无任何 ref（:561）。
  - `skeleton_message(message, *, limits=DEFAULT, fingerprint=False, sid=None, wire_view=3)`（:564-634）：**消息级投影**。`info` 非 dict → 归一 `{}`（P1-29 防御，:576-579）；剥 info 上游 expandRefs（:580-583）；`info.summary.diffs` 恒置 null，原值为非空 list 且 sid+真 messageID 时发 message-level `info_summary_diffs` ref（m1 类型感知：`""`/`False`/`{}` 不算；M3：`unknown` id 不发，:585-601）；`parts` 非 list → `[]`（:602-604）；per-message budget `{"used":0}` 按部件顺序共享（:605-609）；非 dict part 静默跳过（:614）；无任何可渲染 part → 追加 `thin_placeholder_{message_id}` 占位 text part（:616-624）；`fingerprint or limits.fingerprint` → 投影完成点注入指纹（:626-633）。
  - `skeleton_messages(messages, *, ...)`（:637-656）：列表映射；`wire_view` 透传决定所有 href 的 `?v=`（默认 3 保持历史字节，:646-649）。
  - `strip_diagnostics_message(message)`（:676-709）：**/full 诊断剥离（就地修改）**——仅剥每个 part 的 `state.metadata.diagnostics`（:704-708）；顶层 `metadata.diagnostics` 刻意不动（:680-684）；非 dict 输入原样返回（形状鲁棒，:697-698）。
  - `_is_renderable(part)`（:712-726）：占位判定——text/reasoning：truthy text 或有 expandRefs（:714-718）；tool：`tool`/`state.title`/`state.input`（:719-721）；patch：`files`/`metadata`/`state`（:722-723）；file：`filename`/`url`（:724-725）；**其余类型（step-start/step-finish/compaction/未知）恒 False**。
  - `SESSION_KEYS`（:729-731）+ `skeleton_session(session)`（:734-744）：v3 会话骨架——顶层白名单 + time/summary/revert 子对象白名单 pick。
  - `COMMAND_SKELETON_KEYS`（:766）/ `skeleton_command`（:769-779）/ `skeleton_commands`（:782-788）：command 目录白名单投影（丢 template 等 ~97.6% 字节）；非 dict 行静默跳过（:783-788）。
  - `AGENT_SKELETON_KEYS`（:791）/ `skeleton_agent`（:794-804）/ `skeleton_agents`（:807-809）：agent 目录白名单投影（丢 prompt/permission 等 ~95.8%）。
  - `project_rows_to_v4_skeletons(rows)`（:827-879）：**旧 4.0.0 形态** v4 会话列表投影（DB 行 → SessionSkeletonV4：平铺列 → 嵌套 wire 对象；join 缺行 → `project:null`，:868-877）。无 partial/degraded、无 required 校验；非 dict / 缺 id 行静默跳过（:834-840）。
  - `native_session_to_record(item)`（:909-974）：native SessionInfo（camelCase）→ DB 投影记录形状（snake_case 列 + p_* join 列）的**归一化器**（非投影，§13.3）。键 presence 三态载体：对象字段（model/summary/tokens/revert）仅 dict 或显式 None 落列，其余形状视为来源不可得（键缺席，:914）；summary 子值为 null 视为畸形成员不落列（:937-947）。
  - `_CANONICAL_REQUIRED_NON_NULL`（:977-979）：id/directory/title/time_created/time_updated。
  - `_canonical_number(value)`（:982-984）：JSON number（int/float 排除 bool）。
  - `_canonical_object_field(source, required, optional)`（:987-1014）：model/revert 共用的 nullable 对象子字段校验——required 缺席/类型错、optional 在场 null/类型错 → 整体 None；optional 缺席 → 不置键。
  - `canonical_session_skeleton_v4(record, *, fallback=False)`（:1017-1177）：**唯一 canonical projector**（§13.3 列表/单查/DB/native 四路径共用）。required 缺席/null/类型/约束违约（id/directory 非空串、title 可空串、time.created/updated 非负数）→ `None`（调用方转整响应 503 `auxiliary_unavailable`，:1035-1049）；nullable 三态（键缺席 → null+partial，在场 None → 业务 null，:1053-1060）；nullable_str/number 类型错 → null+partial（:1062-1078）；`project` 双形态：projectID null → project **缺席**，非空且三不变量（join 成功+id 匹配+worktree 非空串）不满足 → `project:null`+partial（:1080-1106）；model/revert 子字段畸形 → 整体 null+partial（:1111-1127,1159-1173）；summary 三元组：全 null → null、全数值 → 对象、混合 → null+partial（:1138-1152）；`degraded = partial or fallback`（:1175-1176）。

- **依赖**：仅标准库 + orjson——`hashlib`（:5）、`copy.deepcopy`（:6）、`dataclasses`（:7）、`typing.Any`（:8）、`orjson`（:10）。**零内部 imports**（不依赖 settings/gzip_util/路由层；:85-96 注释明确去单例化）。

- **被依赖**（rg 反查）：
  - `routes/messages.py:19-24`：`SkeletonLimits`、`recompute_fingerprint`、`skeleton_messages`、`strip_diagnostics_message`（每请求从 config 构造 limits，:845-849、:1055-1059、:1066-1070）。
  - `routes/sessions.py:31-36`：`canonical_session_skeleton_v4`、`native_session_to_record`、`project_rows_to_v4_skeletons`（门控切换 canonical vs 4.0.0 形态，:457-465；native 回退 :667-674）。
  - `routes/read_groups.py:73-77`：`canonical_session_skeleton_v4`、`native_session_to_record`、`skeleton_session`（单查 DB :542 / native fallback :450-452）。
  - `routes/children.py:28`：`skeleton_session`。
  - `routes/command.py:7` / `routes/agent.py:7`：`skeleton_commands` / `skeleton_agents`。
  - `transform.py:61-63`：`strip_diagnostics_message`（full/打包路径）。
  - `app.py:570`：注释提及避免经 skeleton.py 循环导入。
  - 测试：`tests/test_skeleton.py`、`tests/test_message_fingerprint.py`、`tests/test_b2_merged_text_compat.py`、`tests/test_expand_href_v4.py`、`tests/test_readiness_gating_integration.py` 等。

- **状态/可变性**：模块级常量均为不可变（str/set/tuple/frozen dataclass）；`set` 常量（PART_IDS 等）未 frozenset 但约定不改。**所有投影函数纯度靠约定**：`_pick`/`_mark`/`_maybe_inline_state_field`/`skeleton_message` 返回新对象（deepcopy 隔离），但 `recompute_fingerprint`（:62-64）与 `strip_diagnostics_message`（:676-709，docstring :687-690 明示）**就地修改输入**；`_tool`/`_patch` 里 budget dict 就地累加（:339）。`skeleton_part` 入口 L509 重建 part dict（浅拷贝）避免污染调用方。

- **错误路径**：模块**自身不构造任何 HTTP 错误码/异常**——纯函数，坏形状全部走防御性降级（info/parts 归一、非 dict 项跳过、diffStats → None、canonical → None）。唯一的异常传播路径：`orjson.dumps` 在 compaction 分支（:553）与 `_field_byte_size`（:229 有 TypeError 回退，但 compaction 分支**无回退**）对非 JSON 序列化对象会裸抛 TypeError——上游数据源自 HTTP JSON 解析时不可达。canonical 返回 None 后由调用方转 503 `auxiliary_unavailable`（read_groups.py:541-546、sessions.py:460-461）。

- **疑问点**（宁多勿漏）：
  1. **fingerprint 输入域含 expandRefs → v3/v4 视图与 sid 进入指纹域**（:597-601, :633）：`recompute_fingerprint(result)` 对含 `expandRefs`（href 内嵌 `{sid}` 与 `?v={wire_view}`，:182-183）的最终对象计算。同一消息在 `wire_view=3` vs `4` 下 `contentFingerprint` **不同**，而 `FINGERPRINT_VERSION` 前缀（`v1:`）不编码视图（:36-39 只提到 default vs mode=merged 不可比）。客户端若跨视图缓存比对指纹会假阴性。契约侧是否明示此 namespace？建议核对 v3/v4-contract 的 fingerprint 章节。
  2. **placeholder 参与指纹**（:616-624 在 :626-633 之前执行）：占位 part（id `thin_placeholder_{id}`、omitted ["parts"]）进入 hash 输入——同一上游消息因 sid/视图导致 refs 有无（`_emit_expand_refs` 无 sid 时丢 refs，:205-206）会连锁改变指纹。是否合意？
  3. **`_file` 的 `len(url) <= FILE_URL_LIMIT` 用字符数而非 UTF-8 字节**（:487）：与 `_utf8_bytes_exceeds`（:160-164）和 `_field_byte_size`（:217-231）的 wire 字节度量不一致；多字节 URL 字符数 ≤8192 但字节数可达 ~32KiB。
  4. **`_expand_ref` href 不做 URL 编码**（:182-183）：sid/message_id/part_id/category 直接 f-string 拼路径。对上游 id 形状（`msg_*`/`psrtp_*` 等 URL-safe token）是隐式假设；id 含 `/`、`?`、`#` 时 href 语义破坏。全模块未验证 id 字符集。
  5. **step-start snapshot ref 的空容器判断不一致**（:547）：`part.get("snapshot") not in (None, "")` 允许 `{}`/`[]` 通过并发 ref，与 `_file` source 的 `not in (None, {}, [], "")`（:496）及 attachments 的 `not in (None, [], {})`（:393）不一致；注释宣称 "non-null/non-empty"（:546）但空 dict/list 会产生指向空内容的 expand href。
  6. **`_compute_diffstats` list 分支 `files = len(filediff)` 计入非 dict 项**（:263-271）；`_compute_diffstats_from_files` 混合列表时 string 项同样计入 `len(files)`（:298-308）。统计口径与"仅 dict 项贡献数值"略不对称。
  7. **`_tool` L384 的 `value not in (None, "")`**：防 budget 挤掉空值时发无意义 ref；但 `0`/`False` 若被 budget 挤掉会发 ref（size 1/5 字节，几乎不可能触发）——行为正确但依赖隐式大小推理。
  8. **`_is_renderable` 对 step/compaction/未知类型恒 False**（:726）：只含 compaction part 的消息（压缩摘要消息常见形态）必追加 placeholder——设计使然（§4.3），但意味着 compaction-only 消息的指纹/字节都含 placeholder。
  9. **compaction 超限路径 `omitted:["*"]` 有 ref，未知类型 `["*"]` 无 ref**（:560 vs :561）：同为 `["*"]` 语义但 expand 能力不同，客户端需区分处理。
  10. **`canonical_session_skeleton_v4` 的 `p_name` 非 str 静默降级**（:1098-1101）：project 对象发 `{id, worktree}` 无 name 键且**不置 partial**——与其它 nullable 字段"类型错 → null+partial"（:1062-1069）语义不一致。
  11. **`project_rows_to_v4_skeletons` 与 canonical projector 并存**（:827-879 vs :1017-1177）：旧形态无 required 校验（缺 id 行静默跳过 :839-840）、无 partial/degraded、`directory` 可为 null（无 §13.2a 校验）。sessions.py 门控（:457-465）切换两形态——门控关闭期间两套投影语义分叉是审计面。
  12. **`_canonical_object_field` 的 bool 检查对 str kind 冗余**（:1003,1011）：`isinstance(value, bool)` 在 kind=str 时永 False；仅当未来 kind 用数值才有意义。无害但易误读。
  13. **`native_session_to_record` 的 time 非 dict → 整条记录 created 不可得 → canonical None → 整响应 503**（:929-935 + :1046-1049）：一条坏 time 形状使整个列表请求 fail-closed（sessions.py:460-461）。是否符合 §13.2c "禁混入 items" 的期望范围（单行失败 → 整列表 503）值得与契约对账。
  14. **`_pick` 逐值 deepcopy 性能**（:150）：大 `todos`（TOOL_INPUT_KEYS 含 todos，:68）或大 files 逐值深拷贝；`_field_byte_size` 先 dumps 测量再 deepcopy 内联——大 output 字段被拒绝时无 deepcopy（好），被接受时 dumps+deepcopy 双份成本。
  15. **`skeleton_part` text 分支向 `_emit_expand_refs` 传空 refs**（:520）：等价 no-op（:206 `not refs` 直接返回），仅为保持调用形状一致——阅读噪音。
  16. **TOOL_METADATA_KEYS 同时含 `sessionId` 与 `sessionID`**（:70）：两个大小写变体都在白名单，上游只用其一——防御性冗余，无歧义但值得知道来源。
  17. **fingerprint 双开关 `fingerprint or limits.fingerprint`**（:626）：两个入口都可开，语义重复；routes 走 limits.fingerprint（messages.py:848），直传参数为测试遗留——若两者将来分歧（如直传 False 但 limits True）无文档化优先级。

---

### src/oc_slimapi/envelope.py（73 行）

- **职责**：v3/v4 信封（envelope）构造助手（v3-contract §4）。v3 消息信封把 v2 bare-array 打包字节**逐字拼接**（不重解析）；v3 会话信封为 dict payload；v4 会话信封带 nextCursor/degraded 修订面。模块 docstring 明示：错误永不入信封（§4.4）、304 无 body（§6.4）是路由的结构属性而非本模块属性（:11-13）。

- **对外符号**（逐顶层符号）：
  - `messages_envelope_bytes(items_bytes, next_cursor) -> bytes`（:21-29）：拼 `{"items":<v2 array 原字节>,"nextCursor":<str|null>}`。cursor 用 `orjson.dumps` 序列化（非 None 时，:28），键序冻结 (items, nextCursor)。docstring :26 指出此字节同时是 v3 messages 路由的 **canonical ETag 输入**（§6.3）。
  - `sessions_envelope_payload(sessions, complete) -> dict`（:32-43）：`{"items":[...],"complete":<bool>}`；complete 继承 v2 `X-Complete` 语义（非权威声明，:38-40）；由路由常规 `json_response` 序列化故 gzip/Vary 与 v2 路径一致（:40-42）。
  - `sessions_envelope_v4(items, next_cursor, complete, *, degraded=False, degraded_required=False) -> dict`（:46-73）：v4 会话信封（v4-contract §4.1 + §13.1 修订面）。键序冻结 `(items, nextCursor, complete[, degraded])`（:56）；`degraded_required=False`（4.0.0 已发布形态）时 `degraded` 键仅在 Class A fallback 且恒 true；`degraded_required=True`（§13.1 修订）时恒发布尔（含 false）（:57-64,71-72）。

- **依赖**：`orjson`（:18）。零内部 imports。

- **被依赖**：
  - `routes/messages.py:15`：`messages_envelope_bytes`（:892 v3 列表、:1089 merged 尾部——identity body 组装点，随后进 ETag 计算）。
  - `routes/sessions.py:25`：`sessions_envelope_payload`（:118，v3 列表）、`sessions_envelope_v4`（:480 canonical 路径、:562 native fallback 路径）。
  - 测试：`tests/test_v3_envelope.py`、`tests/test_etag.py:28`（作为 ETag 输入）、`tests/test_session_single_v4.py:10`（§13.4 聚合公式对账）。

- **状态/可变性**：全部纯函数；返回新建 bytes/dict，不改参数。无模块级可变状态。

- **错误路径**：无。模块不抛异常、不构造错误码。`messages_envelope_bytes` 假定 `items_bytes` 是合法 JSON array 字节——**无校验**，调用方（messages.py 打包管线）负责。

- **疑问点**：
  1. **`messages_envelope_bytes` 对 items_bytes 零校验**（:29）：若上游管线传入非法字节（截断的 array 等）会产出整体非法 JSON 且 ETag 照算。调用链（messages.py `_project_list_sorted_and_pack` → orjson.dumps 产物）当前保证合法性，属隐式契约。
  2. **dict 键序依赖 orjson 默认不排序**（:43, :66-72）：wire 键序 = 构造序；若未来序列化层加 `OPT_SORT_KEYS`，`complete` 会排在 `degraded`/`items` 前破坏 §4.1 冻结键序——键序正确性依赖序列化点的配合，本模块无法独立保证。
  3. **`sessions_envelope_v4` 无法表达「degraded_required=False 且显式 degraded=false」**（:71-72）：`degraded=False, degraded_required=False` → 无键；该组合留空是 4.0.0 形态定义，但参数矩阵 (degraded, degraded_required) 四格中 (True, False)/(False, True)/(True, True) 语义可叠加、(False, False) 特殊——`degraded=True and degraded_required=True` 时值仍取 `bool(degraded)`，两开关无冲突检测。
  4. **`next_cursor` 序列化用 orjson.dumps(str)**（:28）：对含控制字符/非 ASCII 的 cursor 会产 `\uXXXX` 转义——与 v2 `X-Next-Cursor` 头的原始字符串形式字节不同。cursor 域（上游 cursor token）若含非 ASCII，头与信封内表示不同形；实际 cursor 通常 URL-safe，属隐式假设。

---

### src/oc_slimapi/etag.py（294 行）

- **职责**：Traffic plan Batch 2 / B1——ETag/304 条件请求支持（权威 `docs/ocmar/plans/2026-08-16-traffic-optimization-plan.md` §4）。validator 方案（:9-16）：identity → STRONG `"<sha256hex(REP_VERSION \0 b"identity" \0 identity)>"`；gzip → WEAK `W/"<sha256hex(REP_VERSION \0 b"gzip" \0 identity)>"`——**hash 输入恒为 identity 字节 + coding id，绝不含压缩字节**；跨 coding 复用 fail-closed（保守 200）。管线恒全量跑（ETag 不短路上游抓取/投影），命中只省下行 body。

- **对外符号**（逐顶层符号）：
  - `_ETAG_SCHEME_VERSION = b"etag-v1"`（:29，私有）：validator 派生方案自身版本。
  - `SKELETON_REPRESENTATION_VERSION = b"skeleton-v1"`（:39，公开常量）：投影语义版本——投影语义/gzip 实现/MIN_GZIP_BYTES/益处门变化时必须 bump（rev-5 C1，:31-38）；与 config 指纹合成 REP_VERSION，翻转即轮换全部 validator。
  - `_ConfigLike`（:42-46，私有 Protocol）：Settings 影响响应表示的子集——两个 inline 上限。
  - `representation_version(config, *, wire_view=3) -> bytes`（:49-88）：REP_VERSION = `b"\x00".join([scheme, skeleton-v1, str(field_cap), str(message_cap), fingerprint=on/off, wire=v{view}])`。`message_fingerprint_enabled` 经 `getattr(..., True)` 读取（:81-83）以便翻转时轮换 ETag 且纯函数测试的 ad-hoc config 可用；wire-view 域标记（v3-contract §6.1，:62-69）使不同 wire 域的 validator 永不交叉 304，无 v2 特例分支（M3-5 终态，:66-68）。
  - `response_rep_version(config, *, wire_view=3) -> bytes | None`（:91-98）：响应发射用 REP_VERSION；`config is None` 或 `etag_enabled=False` → None（字节等价 legacy 行为）。
  - `compute_etag(identity_body, coding, rep_version) -> str`（:101-113）：`sha256(rep_version + \0 + coding + \0 + identity_body)` 全 hex 不截断；gzip → `W/"..."`，否则 `"..."`。
  - `if_none_match_matches(header_value, etag) -> bool`（:116-148）：RFC 9110 If-None-Match **弱比较** + `*`；逗号分割（引号外，:133-142）；空 header 永不匹配。
  - `_opaque_tag(candidate)`（:151-168，私有）：剥大小写敏感的 `W/` 前缀；`w/` 小写视为 malformed 跳过（:159-162）；inner 空/含引号 → None。
  - `merged_vary(current) -> str`（:171-176）：终态 §6.2——**恒返回 `"Accept-Encoding"`**，忽略入参（`X-Opencode-Directory` 维度随头通道退役）；签名保留兼容。
  - `judge_conditional(identity_body, if_none_match, rep_version, *, accept_encoding, min_gzip_bytes=MIN_GZIP_BYTES) -> str | None`（:179-243）：rev-5 coding-specific 304 判定（benefit-gated 压缩路由用——messages 列表/merged 尾部）。返回 None（200）/ `"*"`（调用方压缩一次后 echo 实际 coding tag）/ tag 串（304 echo，零压缩达到判定）。规则：不接受 gzip 或 body < min → 单候选 identity STRONG 精确判定（gzip tag 不可能匹配 → 保守 200，C5，:230-237）；接受 gzip 且 ≥min → 服务 coding 不可静态知（益处门"压缩不更小"回退需真压缩，304 绝不压缩）→ 单候选 gzip WEAK tag（:238-243）：INM 弱匹配 gzip tag → 304 echo（echo 可靠性论证 :212-218）；INM 含 identity tag → 恒 200（:219-222）。
  - `not_modified_response(etag_value, vary_value, aux=None) -> Response`（:246-263）：pre-compression 单候选路径的 304 构造——ETag + Vary + `Cache-Control: no-store` + aux 头，无 body 无 Content-Encoding。
  - `conditional_304(final_headers, if_none_match, aux=None)`（:266-294）：从 final_headers 读 ETag（缺失→None 即 ETag 关闭）、不匹配→None；匹配则 304 头集与 200 相同的 ETag/Vary（默认 Accept-Encoding）+ no-store + aux（如 X-Next-Cursor/X-Complete——由本次管线运行计算，:276-281）。

- **依赖**：`hashlib`（:20）、`typing.Protocol`（:21）、`starlette.responses.Response`（:23）、内部 `.gzip_util`（`MIN_GZIP_BYTES`=64、`accepts_gzip`，:25）。`accepts_gzip(None)` 返回 False（gzip_util.py:28,40）。

- **被依赖**（rg 反查，均 `from .. import etag as etag_mod` 形式）：
  - `routes/messages.py:14`：`response_rep_version`/`merged_vary`/`judge_conditional`/`not_modified_response`/`compute_etag`（:902-938 列表、:1098-1134 merged）。
  - `routes/_read_passthrough.py:58`：`response_rep_version(wire_view=3)`/`merged_vary`/`judge_conditional`/`not_modified_response`/`compute_etag`（:186-273）。
  - `routes/sessions.py:27`：`response_rep_version`/`merged_vary`/`compute_etag`/`conditional_304`（:116-129 v3、:625-641 v4）。
  - `routes/_catalog_common.py:30`：`compute_etag`/`merged_vary`/`if_none_match_matches`/`response_rep_version`/`conditional_304`（:170-395）。
  - `routes/read_groups.py:49`：`if_none_match_matches`/`not_modified_response`（:362-363）。
  - `providers_projection.py:49`：`compute_etag`（:432；:115 注释自述镜像 `response_rep_version` 门控但自建 rep）。
  - `transform.py:59`：`merged_vary as etag_merged_vary`（:100 Vary 折叠）。
  - 测试：`tests/test_etag.py`、`tests/test_v3_etag_domain.py:25`、`tests/test_message_fingerprint.py:27`。

- **状态/可变性**：全部纯函数/常量；无模块级可变状态。`Response` 对象每次新建。

- **错误路径**：无异常、无错误码构造。所有"失败"表达为返回值：`response_rep_version → None`（ETag 关闭）、`judge_conditional → None`（保守 200）、`conditional_304 → None`（不发 304）。设计立场：**绝不因条件请求逻辑把 200 变 5xx**。

- **疑问点**（含 canonical hash 输入域与 wire v3/v4 差异专项）：
  1. **REP_VERSION 指纹域完备性靠人工纪律**（:70-88）：域 = scheme + skeleton 版本 + 两个 inline 上限 + fingerprint 开关 + wire 标记。**未包含**：`max_response_bytes`（外层 body cap——若截断会改变 body 则应入域；需确认截断是否产生不同 body 还是错误响应）、transform 池相关影响字节的配置。新增任何改变投影字节的配置若忘加 `fingerprint_parts` → stale 304 风险。类注释 :36-38 把 gzip 实现/MIN_GZIP_BYTES/益处门变化归入"应 bump SKELETON_REPRESENTATION_VERSION"——纯注释纪律，无机制保证。
  2. **gzip tag 与压缩实现天然无关 vs 注释要求 bump 的微妙矛盾**（:108-113 vs :35-38）：`compute_etag` 的 hash 输入不含压缩字节，故 zlib 升级不改变 tag 值；注释要求 bump 的真实原因是益处门可能翻转实际服务的 coding（identity↔gzip）。bump `SKELETON_REPRESENTATION_VERSION` 会同时轮换 identity tag（本不需要轮换）——过度轮换但安全方向。
  3. **wire v3/v4 域隔离依赖调用方传对 view**（:87）：`representation_version` 接受任意 int；`_read_passthrough.py:186` 硬编码 `wire_view=3`，messages/sessions 从请求选择器取。若某路由忘传（默认 3）而该路由实际发 v4 字节 → v4 响应带 v3 域 validator，v3 客户端可能假 304。反查确认当前 v4 路径（sessions.py:626）有传 view，但这是每路由的隐式义务。
  4. **`judge_conditional` 的 min-gate 用 identity 长度预判服务 coding**（:231-232）：与 `gzip_util.compress_if_beneficial` 的实际门（MIN_GZIP_BYTES=64 + 压缩得益比较）的一致性依赖两处实现同步；若益处门的 min 常数与 `min_gzip_bytes` 默认值漂移，规则 1/2 的分支选择会错（后果只是保守 200 或需要 echo 校验，仍安全）。
  5. **`"*"` 返回约定**（:229,196-198,223-224）：`If-None-Match: *` 命中返回字面 `"*"` 让调用方压缩一次并 echo 实际 coding——协议经字符串多态（None/"*"/tag 三态）表达，调用方（messages.py:913-931）必须正确区分；类型上三者都是 str|None，无类型防护。
  6. **`conditional_304` 与 `judge_conditional` 的保守性差异**（:285 vs :241-243）：`conditional_304` 用同一弱比较函数，但其调用方（sessions/_catalog 单候选路径）已在头里放了**即将服务**的 coding 的 tag，故 INM 持另一 coding tag 自然不匹配。两套 304 判定路径（judge_conditional 双候选 vs conditional_304 头回读）语义等价性依赖"final_headers 的 ETag 已是实际服务 coding 的 tag"这一时序约定，无断言保护。
  7. **`if_none_match_matches` 不处理列表内 `*`**（:128-129 仅整头 `*`）：`"etag1, *"` 按字面候选解析，`*` 候选被 `_opaque_tag` 拒绝（非引号包裹）→ 不匹配。RFC 9110 要求列表中含 `*` 即匹配——边角偏离（实际客户端极少发混合 `*` 列表）。
  8. **`_opaque_tag` 接受任意非空 inner**（:165-167）：不校验 hex/长度——对未知格式 tag 自然不匹配（安全），但意味着本实现永不因格式收紧而拒绝合法自产 tag；反之若未来 tag 格式变化需 bump scheme 版本（已有机制）。
  9. **`merged_vary` 丢弃入参**（:171-176）：所有调用点传 `"Accept-Encoding"` 或既有 Vary 值均被折叠为单值——若上游响应带 `Vary: X-Custom`，经此函数后维度丢失。当前架构（sidecar 全权决定缓存维度）下属设计决策，但列此备查。
  10. **`representation_version` 的 `getattr(config, "message_fingerprint_enabled", True)` 默认 True**（:82-83）：与 config.py:423-426（env 默认 "true"）一致；但若未来 Settings 改名该键，getattr 静默回 True——指纹域组件静默消失而非报错，ETag 域变化不可见（此处幸运地朝"仍含 fingerprint 组件"方向，但开关语义可能失真）。
  11. **`judge_conditional` 规则 2 的 echo 可靠性论证依赖"合法交换史"**（:212-218）：手造 gzip tag 对不可压缩 body 的情形被论证为不可能从合法交换产生。论证成立前提是本服务是唯一 ETag 来源且 REP_VERSION 域无碰撞——跨 REP_VERSION 轮换后旧 gzip tag 携带旧域 hash，hash 不匹配自然 200，论证仍闭合。
  12. **`not_modified_response`/`conditional_304` 的 aux 头无过滤**（:261-262,292-293）：`headers.update(aux)` 直接合并——若 aux 含 `Content-Encoding`/`Content-Length` 等会泄漏进 304。当前调用方只传 X-Next-Cursor/X-Complete 类，属调用方纪律。

---

## 附：三文件关系速览

- `skeleton.py`（纯投影）→ 产物经打包 → `envelope.py`（v3/v4 信封字节）→ 该 identity 字节进 `etag.py` `compute_etag`（messages 路由：envelope 字节即 canonical ETag 输入，envelope.py:26）。
- REP_VERSION（etag.py:49-88）覆盖 skeleton 的 inline 上限与 fingerprint 开关——**skeleton 投影语义变化必须同步 bump `SKELETON_REPRESENTATION_VERSION` 或改动指纹域**，否则 stale 304；反向，skeleton 的 expandRefs `?v=`（wire v3/v4）既影响 envelope 前的 body 字节（进 ETag），也影响消息级 `contentFingerprint`（skeleton.py 疑问点 1）。
- 三模块互不 import（envelope/etag 零内部依赖除 gzip_util；skeleton 零内部依赖）——依赖方向全部由路由层编织。

<!-- ==== e1-07-singleflight-transform-cache ==== -->
# E1-07 精读卡片：singleflight.py / transform.py / catalog_cache.py

审计基线：pyproject `version = "4.4.0"`，`requires-python = ">=3.11"`。引用行号均为当前工作树。

---

### src/oc_slimapi/singleflight.py（770 行）

- **职责**：per-key single-flight 去重共享上游 GET——单一 `SingleFlight` 类承载两个 profile（B6-1 合并产物）：plain（`max_bytes=None`，join-or-lead `fetch()`，完成后结果保留 ~1s grace 供 admission 序列化的同 key 请求合流；调用方 = 进程级 `fulls` 注册表（direct /full + merged fan-out）与 catalog-cache 刷新防踩踏）与 leased（`max_bytes` 必填，`fetch_or_bypass()` 字节预算 admission + Lease 纪律；调用方 = per-app `raw_fetch_registry`（列表路由上游 GET））。只共享 raw fetch，transform 不在本模块视野内（L2-CD-1 oracle §C-2，`singleflight.py:31-33`）。
- **对外符号（逐类逐方法）**：
  - 模块常量：`_DEFAULT_RESULT_GRACE_SECONDS=1.0`（97）、`_MAX_RETAINED_ENTRIES=64`（103）、`_MAX_RETAINED_BYTES=32MiB`（104）；所有权状态 `IN_FLIGHT/GRACE/RETAINED/FAILED`（108-111）；层常量 `ACTIVE/RETIRED`（114-115）；`_REJOIN` 哨兵（120，flight 死亡后调用方须重入串行点）；`FactoryT`（91）。
  - `FetchFailed`（123-137）：失败 RESULT 信封（`__slots__=("exc",)`）；leader 用 `set_result(FetchFailed(exc))` 而非 `set_exception`，零 waiter 失败不触发 "Future exception was never retrieved" 告警（124-131）。
  - `_Entry`（140-179）：一代 flight。`__init__`（157）——`future` 在构造时用 `get_running_loop().create_future()`（160）；`caller_refs`（leased 引用计数，plain 恒 0）；`accounted`（预算已入账标志，dual-refund 规则，166-167）；`expires_at`（None=在飞，monotonic deadline=grace，168）；`timer`（grace 到期 `TimerHandle`，shutdown 可取消，rev-9，171-172）；`size`（plain 完成后按 `len(result)` 计，175）；`in_flight` property（177-179，`expires_at is None`）。
  - `_current_task_cancelling()`（182-186）：3.11+ `task.cancelling() > 0`，区分"我自己被取消"与"flight 死了"。
  - `Lease`（189-240）：leased 调用方句柄。`__init__`（215）、`__aenter__`（221）、`__aexit__`（224，调 `_release`）、`_release`（228-240）：幂等守卫 + **先切断 `self.body=None; self._entry=None` 再递减 caller_refs**（237-240；终审 rev-1 修复：已释放 Lease 不得跨后续 await 持有共享 raw body，防 zombie generation）。
  - `full_fetch_key(scope, sid, mid, directory)`（243-254）：`("full", id(scope), sid, mid, directory)`；scope=app 的 TransformPool，`directory` 必须入 key。
  - `SingleFlight`（257-761）：
    - `__init__`（273-337）：profile 由 `max_bytes is not None` 决定；跨 profile kwargs 即早 `TypeError`（295-308）；plain 注入 retention 上限（330-337）；leased 建 `asyncio.Semaphore(network_concurrency)`（321-324）。
    - `leased_bytes` property（343-347）：当前预算占用。
    - `in_flight(key)`（349-352）：方法名与 `_Entry.in_flight` property 同名异构（方法 vs 属性）。
    - `snapshot()`（354-366）：`{key: [(layer, seq, caller_refs, state), ...]}` 双层账本视图。
    - `fetch(key, factory)`（372-391）【plain】：`while True` 循环——`_expire_if_due` → 无 entry 则登记新 `_Entry` + `_evict_over_budget` + `_lead`（384-388）；有则 `_join`，`_REJOIN` 则重入循环（389-391）。永不 bypass。
    - `fetch_or_bypass(key, factory, reserve_bytes)`（393-428）【leased】：同步串行点（上方无 await）——无 entry：`_try_reserve` 失败返回 `None`（bypass，418）；成功则 `_new_entry` 入账 + **leader ref 在 factory 之前登记**（421）+ `_lead` 结果直接包 `Lease`（422）。有 entry（在飞或 grace）：**waiter ref 在 await 之前 +1**（424）→ `_join` → `_REJOIN` 则 `continue` 重入串行点（426-427）。
    - `_join(entry)`（430-455）：`await asyncio.shield(entry.future)`（438）+ 三分支取消机：`CancelledError` → 先 `_release_caller`（440）→ `_current_task_cancelling()` 为真 → 分支③ 原样 `raise`（441-446，自身取消，共享 future 不受影响）；否则分支② fall-out → 返回 `_REJOIN`（447-450，旧 ref 已释放，重入串行点 re-join/re-reserve/re-lead）。`FetchFailed` → 分支① `_release_caller` 后 `raise result.exc`（451-454，所有 waiter 同一异常实例）。plain 下 `_release_caller` 为 no-op，两 profile 分支结构行为等价（434-436）。
    - `_lead(entry, factory)`（457-481）：factory（leased 且有 `_network_sem` 时在信号量内执行，462-464）；`CancelledError` → 分支② `_fail(entry)` 后 `raise`（467-471）；其他异常 → 分支① `_fail(entry, exc)` 后 `raise`（472-476）；成功 → **先发布 `set_result` 给 waiter，再做所有权转换**（478-480）。
    - `_convert_success(entry, result)`（487-534）：plain——二次身份校验（`_entries.get(key) is entry`，防 shutdown/替换，503）后设 `expires_at`、按 `len(result)` 计 `size` 入 `_retained_bytes`、`_evict_over_budget`、再身份校验后挂 `call_later` 主动到期回调（504-520；新完成 entry 可能被自身逐出，故第二次校验防无谓回调）。leased——`ACTIVE+IN_FLIGHT` 原地转 `GRACE` + 到期 timer（522-529）；`RETIRED+IN_FLIGHT`（shutdown 分离后的迟到成功）→ `RETAINED`，无 grace/timer/重入账（530-533）。
    - `_fail(entry, exc=None)`（535-554）：plain——`_drop` 后 `_fail_future`（542-545）。leased 顺序敏感：**leader ref 先释放 → 摘除 active 注册 → 转 `RETIRED/FAILED` 墓碑 → 立即 `_refund`（先于 waiter 唤醒，使 waiter re-lead 能拿到释放的字节）→ `_fail_future` → 无子嗣即 `_reap`**（546-554）。
    - `_fail_future(entry, exc)`（556-566）：future 已 done 则 no-op；`exc is None` → `future.cancel()`（分支②）；否则 `set_result(FetchFailed(exc))`（分支①）。
    - `_expire_if_due(key)`（568-575）：串行点前的惰性 grace 到期检查。
    - `_expire_grace_entry(key, entry)`（577-591）：timer 回调与惰性尾部共用；身份校验先行（582）；plain 到期即 `_drop`（587-588）；leased 仅处理 `GRACE+ACTIVE`（590-591）。
    - `_expire_grace(entry)`（593-601）：leased grace → 取消 timer → `_drop_grace`。
    - `_drop_grace(entry)`（603-611）：active/grace → retired/retained；无 caller 立即 `_reap`。
    - `_release_caller(entry)`（613-625）：plain 直接 return（614-615）；leased 递减；归零后按状态定命——`RETAINED`/`FAILED` → `_reap`（621-624），`GRACE` 留 body 给迟到者，`IN_FLIGHT`（含 detached）预算继续持有（625-626）。
    - `_reap(entry)`（628-633）：删墓碑 + 条件 refund + 防御性摘 active。
    - `_refund(entry)`（635-638）：`accounted` 双refund守卫。
    - `_detach_from_active(entry)`（640-642）：身份校验后摘除。
    - `_drop(key)`（648-660）：plain 按 key 删除 + 取消 timer + 归还 retained bytes。
    - `_evict_over_budget()`（662-677）：plain 双上限（条数/字节）强制；**只逐出 oldest COMPLETED（`expires_at is not None`），永不逐在飞**；两个串行点调用（插入后 387、完成后 509）。
    - `_try_reserve(needed)`（683-696）：单 flight 超总预算 → False（684-685）；放不下则按插入序逐出**零 caller 的 GRACE** entry（CD-1 纪律）再判（690-696）。
    - `_new_entry(key, reserve_bytes)`（698-703）：seq 自增 + `accounted=True` + 入账。
    - `shutdown()`（709-761）：注册表收敛后**保持可用**（CD-1）。plain：逐 entry 取消 timer + `_drop`，单 key 清理失败被隔离（try/except + 从 entry 自身字段强制退还，730-747）；retained ledger 清零。leased：逐 active entry 原子转换——in-flight → retired/detached（仍计数，future 不动，756-757）；grace → retained，无 caller 立即 reap（758-761）。**在飞 future 永不 cancel**，迟到完成路径靠注册身份复检兜底。
  - `LeasedSingleFlight = SingleFlight`（766）：B6-1 兼容别名。
  - `fulls = SingleFlight()`（770）：进程级 plain 注册表（direct /full + merged fan-out 跨形态去重）。
- **依赖 / 被依赖**：仅依赖 stdlib（`asyncio`、`time`）。被依赖（rg 反查）：`src/oc_slimapi/app.py:32`（import `LeasedSingleFlight, fulls`；385 建 `raw_fetch_registry`，348-355/390-397 lifespan shutdown）；`src/oc_slimapi/routes/messages.py:18`（`full_fetch_key, fulls`；566 direct /full 共享 GET；994-1001 列表 join-first lease 路径）；`src/oc_slimapi/routes/sessions.py:727,838`、`routes/questions.py:144`、`routes/permissions.py:166`（lease 消费方）；`src/oc_slimapi/catalog_cache.py:32`（刷新防踩踏）；`src/oc_slimapi/config.py:393`（文档引用）。测试：`tests/test_leased_singleflight.py`（协议全锁）、`tests/test_full_absorb.py:305-527`（plain 单元）、`tests/test_messages_merged.py`、`tests/test_messages_coalesce.py`、`tests/test_sessions_coalesce.py`、`tests/test_questions_coalesce.py`。
- **状态 / 可变性**：单线程 asyncio 专用（docstring 270 声明 Not thread-safe）。核心可变状态：`_entries`（joinable 层，plain 平铺视图 = leased ACTIVE 层）、`_retired`（leased 墓碑 `(key, seq)→entry`）、`_leased_bytes`（预算 ledger，不变式 = Σ reserve of {in-flight(含 detached), grace, retained}，64-69）、`_retained_bytes`（plain retained 计量）、`_seq`（代序号）、`_network_sem`（仅 leader factory）。锁语义完全靠"串行点"（无 await 的同步段）而非 mutex。
- **错误路径**：lead 常规异常 → `FetchFailed` RESULT 信封 → 全部 waiter `raise result.exc`（**同一异常实例**，451-454），entry 丢弃，绝不负缓存（下一个请求重试）；lead 被取消 → `future.cancel()`（未包裹，564）→ 存活 waiter 走分支② `_REJOIN` → 重入串行点 re-lead（立即 refund 使 re-lead 可预约）；waiter 自身取消（含 registered-ref→await 窗口）→ 分支③ 自身 ref exactly-once 释放 + `CancelledError` 传播，共享 future 与他人不受影响。路由层后果：joined caller 与 leader 一起等 httpx 超时 → 503 `upstream_unavailable`（CHANGELOG 1.5.0 行为披露②，`CHANGELOG.md:284`）。
- **疑问点（16）**：
  1. **[中] plain `_fail` 按 key 删除不做身份校验**（542-545 `self._drop(entry.key)`）：若 leader 的 factory 运行期间 key 的注册 entry 被替换（如 shutdown 清空后新 caller 重建同 key flight），旧 leader 失败会把**新 entry** 从注册表摘掉——新 flight 的后续 joiner 无法发现它（去重瞬时丢失、多发一次上游 GET），且 `_evict_over_budget` 的"在飞不逐出"不变式被绕过。与 `_convert_success`（503/510）和 `_expire_grace_entry`（582）的身份校验纪律不一致。无内存泄漏（新 leader 的 `_lead`/`_convert_success` 有身份兜底），但属防御缺口。
  2. **[低] 混合时钟**：plain grace timer 用 `call_later(max(0.0, entry.expires_at - self._clock()))`（517-519）把**注入 clock** 的差值喂给 **loop time**；leased timer 却直接 `call_later(self._grace, ...)`（527-528）完全绕开注入 clock；`_expire_grace_entry` plain 分支又用 `self._clock()` 复检 `expires_at <= clock()`（587）。测试注入快进 clock 时，timer 回调触发瞬间 `clock()` 可能尚未越过 `expires_at` → `_drop` 被跳过且无后续 timer → entry 滞留（直到同 key 下次 fetch 惰性触发或其他 key churn 引发逐出）。生产 monotonic 时钟下窗口极小。
  3. **[低] `self._leased = max_bytes is not None` 赋值两次**（293 与 309）：冗余，纯代码卫生。
  4. **[低] `id(scope)` 键复用**：`full_fetch_key` 用 `id(TransformPool)`（254）。app 销毁后 pool 对象被 GC、新 app 的 pool 恰好复用同一 id 时，可与旧 app 尚在 grace（≤1s）的 entry 撞 key 共享 body。shutdown() 正常清空 + 1s grace 使窗口极小，但测试中快速建/拆 app 且未 shutdown 时可复现。
  5. **[信息] 分支②/③ 竞态合流**：waiter 自身取消与 flight 死亡同时发生时 `_current_task_cancelling()` 判真 → 走分支③ `raise`（441-446），此时该 caller 不再 re-lead——一次取消同时命中两种语义时保守取"自身取消"，可接受但未在 docstring 明示。
  6. **[低] leased `_try_reserve` 逐出只看插入序**（690-695）：`dict` 插入序 ≈ oldest first，但没有按 reserve 大小或等待时长的公平性——大数据集 flight 先到先占，小 reserve 请求在零-caller GRACE 耗尽后直接 bypass（返回 None 直取，行为正确，无饥饿错误，但去重覆盖率对小请求不公平）。
  7. **[信息] reserve 不按实际 body 调整**（26-28 明示 deliberate）：messages lease 路径 `reserve_bytes=config.max_response_bytes`（`routes/messages.py:833`），默认 64 MiB（`config.py:365`）= `raw_fetch_max_bytes` 全部预算（`config.py:404-406`）→ **默认配置下同时只容 1 个 leased flight**，第二个并发不同 key 直接 bypass 直取（与 CHANGELOG.md:283 披露一致）。预算利用率与去重率在此默认下最低。
  8. **[信息] 共享 raw bytes 免拷贝**：joiner 与 leader 拿到**同一 body 对象**（`Lease.body`，428）。正确性依赖所有消费方只读（messages/questions/permissions 均 `orjson.loads` 后 `del` 局部引用，`routes/questions.py:186-198`）；任何路由就地修改共享 body 会污染并发 joiner。当前无违例，属隐式契约。
  9. **[信息] exactly-once 释放回归**：`Lease._release` 幂等守卫（229-231）+ 引用切断（237-238）+ `_fail` 先释放 leader ref（546）+ waiter 在 `_join` 两分支各自释放（440/453）+ `_release_caller` plain no-op（614-615）——leased 引用计数在三分支下均恰好一次；`accounted` 守卫（635-638）保证 refund 恰好一次。`tests/test_leased_singleflight.py:724-774` 锁定引用切断。**回归完整**，未见双释放/泄漏路径。
  10. **[信息] 取消→503 误映射检查**：分支③ 的 `CancelledError` 原样 `raise`（446），`fetch_or_bypass`/`fetch` 均不吞——路由层客户端断连时 uvicorn 取消 handler 任务，传播为连接中止而非 503；`TransformPool.acquire` 的 `TimeoutError` 才映射 `TransformBusy`（`transform.py:242-243`）。未发现取消被误映射为 503 的路径。
  11. **[信息] grace 窗口竞态（join 一致性）**：grace entry 被 `_expire_grace`/`_drop_grace` 转 retired 的瞬间，已通过 424 行 `caller_refs += 1` 且正在 `await self._join(entry)` 的 straggler 不受影响——shield 等的是 future（已完成），retained 状态下 `_release_caller` 归零才 reap（621-622）。竞态闭合。
  12. **[低] `_evict_over_budget` 的条数上限把在飞 entry 计入**（669）：>64 个并发不同 key 在飞时循环因"无 completed 可逐"而 break——上限对在飞不成立，docstring 101-102 已声明由调用方 admission 兜底（plain 调用方是 `fulls`，其 caller 在 transform pool 内，`max_transforms=1` 默认下在飞数实际 ≤1+merged fan-out；但 merged fan-out 的 per-mid fetch 在 admission 释放后执行（oracle §C-2，`routes/messages.py:1042-1045`），fan-out 宽度 `merged_fanout`（≤16，`config.py:1081`）+ 并发 direct /full 理论上可超 64——仅损失 grace 保留，无内存上界破坏（retained bytes 上界仍被强制）。
  13. **[信息] `shutdown()` 后迟到的 leased `_fail`**：detached in-flight leader 失败 → `_fail` 走 leased 分支：`_detach_from_active` 身份 no-op、墓碑重复写入同 `(key,seq)`（550，幂等覆盖）、refund 恰一次——账本闭合。
  14. **[低] `_release_caller` 对 `caller_refs` 已为 0 的 entry 调用时静默跳过递减**（616-617）：不视为 bug（所有路径配对），但守卫写法意味着"多释放一次"不会炸、只会漏检——审计上弱失败模式。
  15. **[信息] `_REJOIN` 无限循环风险**：分支② 后 `while True` 重入；若极端交错下每次 re-join 都遇上被取消的 flight，理论上活锁（每次都有新 leader 被取消）。实际受上游 httpx 超时与调用方取消约束，未见现实路径，记录备查。
  16. **[信息] `snapshot()` 不做 plain/leased 区分**：plain 调用 `snapshot()` 返回 state 恒 `IN_FLIGHT` 的伪账本（145-149 声明 plain 语义下 state 恒 IN_FLIGHT）——仅 ops 观测，无消费方依赖（rg 未见 src 内调用，仅测试）。

---

### src/oc_slimapi/transform.py（326 行）

- **职责**：有界 transform 池——`asyncio.Semaphore(max_transforms)` admission（**先于上游 GET 获取**，约束内存：任意时刻至多 `max_transforms` 份 body 被缓冲，31-44 RSS 模型）+ 同尺寸 `ThreadPoolExecutor` 承载 CPU 工作（orjson parse → 投影 → dumps → gzip），保证事件循环对 SSE 心跳空闲（1-29）。附带模块级工具：`read_with_cap` cap-read、`strip_diagnostics_and_pack` /full worker 入口、`_pack_json` 序列化+gzip+Vary。
- **对外符号（逐类逐方法）**：
  - `TransformConfig`（66-73，frozen dataclass）：`max_transforms` / `transform_wait_seconds` / `max_response_bytes` 三旋钮快照。
  - `TransformBusy`（75-76）：admission 超时异常，路由映射 503 `transform_busy`。
  - `_pack_json(value, accept_encoding, *, merge_directory_vary=False)`（79-101）：dumps → `compress_if_beneficial` → 恒带 `Vary: Accept-Encoding`；`merge_directory_vary=True`（v3 契约 §6.2 gate B1）追加 `X-Opencode-Directory` 维度。纯 CPU，worker 安全。
  - `strip_diagnostics_and_pack(body, *, accept_encoding, merge_directory_vary=False)`（104-140）：/full worker 入口——`orjson.loads`（128）→ 非 dict `raise ValueError("upstream single-message body is not a dict")`（129-135，防坏上游 200 以 200 透出）→ `strip_diagnostics_message` 原地轻剥离（无 deepcopy，116-118）→ `_pack_json`。空/坏 JSON 抛 `orjson.JSONDecodeError`，路由映射 503。
  - `read_with_cap(response, max_bytes, *, chunk_size=64KiB, on_read=None)`（143-192）：流式 cap-read——`max_bytes<=0` 短路 `(None, 0)`（180-181）；逐 chunk 累计后**先 `on_read(len(chunk))` 再判 cap**（185-190，三条出口路径的字节归因统一，P0-9）；越限返回 `(None, total)`（未缓冲整个超限 body，≤ max_bytes+chunk）；中途异常 chunk 已归因后原样传播（169-173）。
  - `TransformPool`（195-326）：
    - `__init__(config)`（206-214）：semaphore + `ThreadPoolExecutor(max_workers=max_transforms, thread_name_prefix="oc-slimapi-transform")` + `_active/_waiting` 计数器。
    - `config` property（216-218）。
    - `acquire(timeout=None)`（220-246）：`wait_for(semaphore.acquire(), timeout=timeout if not None else transform_wait_seconds)`（L2-CD-1 预算收窄：重试者传剩余墙钟预算，防 N× 全额等待，221-231）；`TimeoutError → TransformBusy`（242-243）；`_waiting` 在 finally 归还（235/244-245）；成功后 `_active += 1`（246）。成功 acquire 必须配对恰一次 `release`。
    - `release()`（248-251）：`_active -= 1` + `semaphore.release()`。
    - `__aenter__`（253-255）→ `acquire()`；`__aexit__`（257-259）→ 复制 release 逻辑（未复用 `self.release()`）。
    - `offload(func, *args, **kwargs)`（261-274）：`loop.run_in_executor(self._executor, ...)`，kwargs 用 `functools.partial` 包装（270-273）；executor 与 admission 同尺寸 → 排队天然有界（调用方持 slot 期间 await offload，264-267）。
    - `snapshot_metrics()`（276-289）：`{"active", "waiting"}`（P2-3，供 `sse/registry.py:373` 读取，不摸私有信号量字段）。
    - `shutdown(wait_seconds=10.0)`（291-326，P1-41）：`executor.shutdown(wait=False, cancel_futures=True)` 立即取消 pending（310）→ 守护线程内 `shutdown(wait=True)` 有界 drain（313-325）→ `done.wait(timeout)` 超时即返回，不阻塞事件循环/进程退出（302-304）。幂等。
- **依赖 / 被依赖**：依赖 `orjson`、`.etag.merged_vary`、`.gzip_util.compress_if_beneficial`、`.skeleton.strip_diagnostics_message`、stdlib（`asyncio/functools/threading/concurrent.futures/dataclasses`）。被依赖：`app.py:40,320-324`（lifespan 构造 + 326-337 有界 drain）；`discovery.py:40`；`routes/messages.py:26-28`（TransformBusy/read_with_cap/strip_diagnostics_and_pack）；`routes/_read_passthrough.py:62`（TransformPool 类型 + admission 上下文）；`routes/{directories,diff,todo,agent,command,sessions}.py`（TransformBusy + read_with_cap）；`routes/{permissions,questions,write_groups}.py`（read_with_cap）；`sse/registry.py:71,87`（snapshot_metrics 接线）。测试：`tests/test_transform.py` 及全部路由测试。
- **状态 / 可变性**：`_semaphore`（admission 许可）、`_executor`（线程池——**唯一跨线程边界**：worker 内运行 `_pack_json`/`strip_diagnostics_and_pack`/投影，输入输出经 `run_in_executor` 拷贝语义传递，无共享可变状态）、`_active/_waiting` 纯 int 计数（仅事件循环线程读写，`_active` 在 `__aexit__`/`release` 由循环线程改）。
- **错误路径**：admission 超时 → `TransformBusy` → 各路由 503 `transform_busy`（+Retry-After）；worker 内 `orjson.JSONDecodeError`/`ValueError` 等 → 调用方 catch 映射 503 `upstream_unavailable`（如 `routes/messages.py:1073-1074,1246-1248`）；cap 超限 → `read_with_cap` 返回 `(None, total)` → 路由 413 `response_too_large`/`message_too_large`；admission 后上游异常 → `async with pool` 退出时释放 slot（26）。
- **疑问点（8）**：
  1. **[中] `wait_for(semaphore.acquire())` 取消竞态可泄漏许可**（238-241）：任务在 `semaphore.acquire()` 内部完成的同一瞬间被取消时，`asyncio.wait_for` 在 3.11 仍存在"结果丢失"边缘（bpo-42130 语义 3.12 才收口）——许可已扣但 `TimeoutError`/`CancelledError` 抛出，`_active` 不加、`release` 永不发生 → 池容量永久 -1。触发面 = 请求恰在 admission 排队获得许可的瞬间断连。3.11 基线下值得复核（3.12+ 无此问题；Python 3.11.x 的 wait_for 修复情况需按小版本确认）。
  2. **[低] `acquire` 成功与 `_active += 1` 之间被取消**：`wait_for` 返回后到 246 行之间无 await，事件循环不会插入取消——安全；但若未来有人在中间插 await 则 `_active` 计数漂移 + 许可泄漏。当前代码安全，属脆弱性备注。
  3. **[信息] admission 先于上游 GET 的顺序保证**：这是**调用方纪律**而非本模块强制——模块只在 docstring（15-17,197-203）约定 `async with pool:` 内发 GET。逐路由抽查均遵守（`routes/messages.py:1005-1012`（直取路径注释明示）、`_catalog_common.py:432`、`_read_passthrough.py` admission 上下文）；例外是**join-first lease 路径**（`routes/messages.py:830-862`：先 `fetch_or_bypass` 拿共享 body，**后**进自己的 admission）——这是 1.5.0 设计变更（join-first），内存上界由 leased 注册表的 `reserve_bytes` 预算（`raw_fetch_max_bytes`）接管，但两条路径的缓冲上界模型不同（admission-first：max_transforms×body；join-first：raw_fetch 预算 + joiner 各自的 admission×body）。审计 E2 阶段建议对 lease 路径的并发内存峰值单独建模。
  4. **[信息] absorb 预算/池满公平性**（`routes/messages.py:1195-1213` 消费本模块）：while 循环每次 `pool.acquire(min(transform_wait_seconds, remaining))`，`TransformBusy` 则 continue 收窄重试——总等待 ≤ `transform_absorb_budget_seconds`，且"503 transform_busy 永不伴随已发出的上游请求"不变式保持（1210-1212 注释）。但 `asyncio.Semaphore` 唤醒为 FIFO，`max_transforms=1` 默认下 absorb 重试者与新鲜请求同队竞争，无优先级——大预算 absorb 者可能反复排队失败直至预算耗尽（正确但可能"等了很久仍 503"）。
  5. **[信息] `max_transforms=1` 默认吞吐**：默认配置（`config.py:363`）下全 sidecar 串行变换——每请求至少 1 次 offload（列表=parse+project+pack；/full=strip+pack），worker 内 gzip level 6 大 body 可达数百 ms，1 并发 + `transform_wait_seconds=2s` 等待上限意味着并发客户端极易 503 `transform_busy`。这是刻意的内存保守（docstring 41-44），且 config.validate 以 `_MAX_TRANSFORM_TOTAL_BYTES`（512 MiB）约束上调幅度；审计应关注默认吞吐是否满足 ocdroid 单客户端 + 偶发并发场景（当前看是够的，SSE 心跳不受影响）。
  6. **[低] `__aexit__` 不复用 `release()`**（257-259）：三行重复，若未来 `release` 增加逻辑（如 finalizer/审计）会分叉。
  7. **[信息] `read_with_cap` 的 `on_read` 回调在事件循环线程同步执行**（187-188）：回调若做重活会阻塞循环——现有调用方均为 `stash_up_in`（计数器累加），安全。
  8. **[信息] `shutdown(cancel_futures=True)` 对 awaiting offload 的路由**：pending future 被取消 → `run_in_executor` 的 awaiter 收 `CancelledError`——与请求取消同形，路由无专门处理；仅发生在 lifespan teardown（此时请求应已排空），可接受。

---

### src/oc_slimapi/catalog_cache.py（181 行）

- **职责**：catalog 路由（`/slimapi/agent`、`/slimapi/command`）的 TTL body 缓存——仅缓存成功上游 body（200 + 可解析 JSON list + 双预算内），TTL 窗口内重复 GET 不打上游；gzip/身份编码不入缓存（缓存 raw body，压缩按请求协商，1-7）。
- **对外符号（逐类逐方法）**：
  - `FactoryT`（34）：`Callable[[], Awaitable[bytes | None]]`（None = cap 超限）。
  - `CatalogCache`（37-181）：
    - `__init__(*, ttl_seconds, max_entries, max_bytes, max_entry_bytes, clock=time.monotonic, refresh_singleflight=None)`（52-71）：三预算（条数/总字节/单条字节）+ 可注入 clock 与外部 SingleFlight；默认自建 plain `SingleFlight()` 做刷新防踩踏。
    - `retained_bytes` property（77-79）/ `entry_count` property（81-83）：ops/测试观测。
    - `lookup(key)`（89-104）：同步新鲜度检查——TTL≤0 直接 None（95-96）；过期条目在此惰性删除（串行点无 await，101-103）；命中返回**同一 bytes 对象**（免拷贝，消费方只读）。
    - `refresh(key, factory)`（106-135）：**须在 transform admission 内调用**（docstring 44-45；调用方 `_catalog_common.py:432` 遵守）。TTL≤0 → `await factory(), None`（禁用=逐请求直取，无 cache 标签，117）。`_fetch_and_store`（119-133）：factory None（cap）→ 不缓存（122）；超 `max_entry_bytes` → 旁路不入账（124）；坏 JSON / 非 list → 不缓存（126-130）；合格则 `_store`（store+evict 同串行点，131）。经内部 `_sf.fetch(("catalog-refresh", key), ...)` 去重（134），straggler 在 1s grace 内加入刚完成的刷新；返回 `(body, "miss")`。
    - `_store(key, body)`（141-147）：先 `_drop` 旧条目再追加新条目（replace-in-place 不双计，143-145）→ `_evict_over_budget`。
    - `_evict_over_budget()`（149-162）：双上限强制，oldest（dict 插入序=fetch 序）先逐；新插入条目最新故不会被逐（validate 保证单条 fits 双预算，152-153）。
    - `_drop(key)`（164-167）：删除 + 归还字节。
    - `shutdown()`（173-181）：`_sf.shutdown()` + 清空 entries + ledger 清零；CD-1 语义——缓存此后仍可用（下次访问重新填充）。由 app lifespan teardown 调（`app.py:368-375`）。
- **依赖 / 被依赖**：依赖 `time`、`orjson`、`.singleflight.SingleFlight`。被依赖：`app.py:20,361-366`（构造，旋钮来自 `config.py:380-391`：TTL 默认 300s / 16 条 / 16 MiB / 1 MiB 单条）；`routes/_catalog_common.py:241,263,369`（`_handle_catalog_cached` 唯一消费链——hit 路径免上游 GET 只做 admission+offload（405-412）；miss 路径 admission-first + `cache.refresh` + offload（432-445））；`routes/agent.py:49`、`routes/command.py:46`（注入 cache）。测试：`tests/test_catalog_cache.py`、`tests/test_etag.py:32,833+`、`tests/test_vary_directory_unconditional.py`。
- **状态 / 可变性**：`_entries`（`dict[key, (body, fetched_at)]`，插入序=逐出序）、`_retained_bytes`、`_sf`（内部 plain SingleFlight）。全部仅事件循环线程触达；所有变更（lookup 惰性过期、_store、_evict、shutdown）均为无 await 串行点 → **三预算原子性成立**（store+evict 之间无 await，149-162；插入前先 drop 旧条目，144）。
- **错误路径**：factory 异常 → 经 SingleFlight 分支① 信封 → 所有 joiner 同实例 re-raise → 路由按 discovery 异常映射（4xx/5xx/网络 → 502/503），**绝不负缓存**（模块 docstring 11-13）；cap 超限（None）与坏 JSON/非 list 均不入缓存（122-130）；TTL=0 全禁用（字节等价旧行为，`_catalog_common.py:435-438` 的 None 标签省略 access log `cache` 字段）。
- **疑问点（5）**：
  1. **[信息] 并发 miss 风暴 per-key 去重：有**——同 key 并发 refresh 经内部 `_sf.fetch` 合并（134，leader fetch + 1s grace straggler 加入）；且 miss 路径整体在 transform admission 内（`_catalog_common.py:432`），`max_transforms=1` 默认下同 key 并发请求被 admission 串行化后，后到者 `lookup` 已命中 leader 存入的条目 → 实际上双重防踩踏（admission 串行 + singleflight）。**不同 key** 并发 miss 各自刷新（无跨 key 合并，符合语义）。
  2. **[低] `refresh` 恒返回 `"miss"` 标签**（135）：grace 内加入 leader flight 的 straggler 也记 `miss`——access log 的 `cache: hit|miss` 语义中"合流命中"被计为 miss，省流审计上轻微低估去重收益（CHANGELOG.md:285 只承诺 hit/miss 二值）。
  3. **[信息] cap 超限结果（None）经 grace 共享**：leader `None` 在 1s grace 内被 straggler 直接复用（同为 413 `response_too_large`，同 body 同判定）——一致；但 None 不是 FetchFailed，属"正常结果"负语义，无负缓存问题（None 不 `_store`）。
  4. **[低] `_evict_over_budget` 无 while 内进展保证的注释依赖外部 validate**：若绕过 `Settings.validate` 直接构造（测试可以）且 `max_entry_bytes > max_bytes`，新条目插入后循环会连续逐出包括最新条目在内的一切直至空 dict（`for...else break` 兜底终止，154-162）——不死循环，但"新条目不被逐"的注释假设（152-153）仅在 validate 成立时为真。
  5. **[信息] shutdown 时序**：`shutdown()` 先 `_sf.shutdown()` 再清 entries（179-181）——若此刻仍有 in-flight refresh leader，其 `_fetch_and_store` 迟到完成时 `_store` 会把条目写回**已 shutdown 的缓存**（单飞 late-completion 只对注册表身份兜底，`_store` 无 shutdown 标志检查）→ shutdown 后缓存非空。CD-1"保持可用"语义下无害（条目 TTL 正常过期），但"shutdown 清空"不变式可被迟到写入打破；调用方 `_catalog_common.py` 的 offload 仍会正确消费该 body。与 app lifespan LIFO（upstream 后关）共同决定 leader 大概率在 shutdown 前完成，窗口极小。

---

## 汇总

| 文件 | 行数 | 疑问点数 | 高优先级 |
|---|---|---|---|
| src/oc_slimapi/singleflight.py | 770 | 16 | plain `_fail` 无身份校验（Q1）、混合时钟（Q2） |
| src/oc_slimapi/transform.py | 326 | 8 | 3.11 `wait_for`+`Semaphore.acquire` 取消竞态许可泄漏（Q1） |
| src/oc_slimapi/catalog_cache.py | 181 | 5 | 无中危；shutdown 迟到写回（Q5）为信息级 |

<!-- ==== e1-19-providers-projection ==== -->
### src/oc_slimapi/providers_projection.py（433）
- 职责：v4-contract §12 providers 白名单投影的纯逻辑模块（decode→validate→project→count→serialize→cap→gzip→ETag ⑥-⑪ 全链一个 worker 作业）。
- 对外符号：
  - `MAX_PROVIDERS=256` / `MAX_MODELS_PER_PROVIDER=1024` / `MAX_VARIANTS_PER_MODEL=64` / `MAX_PROJECTED_BODY_BYTES=8_388_608`（:54-57，§12.4 冻结 wire 常量，禁 env 覆盖）
  - `PROVIDERS_REPRESENTATION_VERSION=b"providers-projection-v2"`（:66，修订三 2026-08-20 恢复 `limit` 子对象导致指纹 bump）
  - `_ORJSON_INT_MIN=-(2**63)` / `_ORJSON_INT_MAX=2**64-1`（:80-81，limit int 值域 = orjson 可序列化范围，超界走省略路径）
  - `ProviderUpstreamMalformed(ValueError)`（:86，§12.5.3 → 502 `provider_upstream_malformed`）
  - `ProviderProjectionLimit(Exception)`（:97，§12.5.3 → 413 `provider_projection_limit`；`limit`/`limit_value` 属性）
  - `providers_rep_version(config) -> bytes|None`（:111，§12.6 指纹 = etag-v1 \0 providers-projection-v2 \0 四常量 \0 wire=v4；etag_enabled=false → None；骨架 config 字段不参与）
  - `_reject_duplicate_members` / `_loads_strict`（:137/:149，stdlib json 严格解码，orjson 会静默吞重复键故走 stdlib）
  - `_ensure_utf8` / `_require_str` / `_validate`（:161/:180/:187，⑦ 全量校验：顶层恰两键、逐串 UTF-8 可编码（lone surrogate → malformed）、models key==Model.id、嵌套 providerID 一致、provider id 全局唯一、default 三元组）
  - `_utf8_key` / `_project`（:288/:295，§12.2 UTF-8 字节序排序 + §12.4 first-triggered-wins 计数 tripwire（不截断）；optional 键 string-else-omit；`variants` 只发排序键数组；修订三 `limit` 子键白名单 {context,input,output} 逐子键 int-else-omit、bool 排除、零存活子键 → 整键省略）
  - `project_and_pack(body, *, accept_encoding, rep_version)`（:376，单一 worker 作业 ⑥-⑪；返回 (encoded, headers)，headers 含 Vary: Accept-Encoding 恒发 + ETag（strong identity/weak gzip，恒 hash canonical identity 字节）；⑫ If-None-Match 判断留在调用方主上下文）
- 依赖：`etag`（compute_etag）、`gzip_util`（compress_if_beneficial）、orjson/json/gzip。
- 被依赖：`routes/read_groups.py`（:62 import，:353-355 映射 413）。
- 状态/可变性：无（纯函数模块）。
- 错误路径：`ValueError` 兜底归一为 ProviderUpstreamMalformed（:421-426，orjson JSONEncodeError/UnicodeEncodeError 防泄漏 500）；ProviderProjectionLimit 原样上抛。
- 疑问点：
  1. `provider_projection_limit` 为 wire 码但 inventory 正则（code= 单行模式）未捕获（多行构造 read_groups.py:353-355）——E2/A4 全量对账须以 rg 为准修正（34 → ≥35）。
  2. `_loads_strict` 用 stdlib json（重复键拒绝）→ 大 body 解码性能低于 orjson，但 8MiB cap 在 ⑩（投影后）而非解码前——解码前的上游 body 上限由路由 read cap 承担（E5 场景 6 核对）。
  3. `_validate` 对 `models.values()` 遍历两次校验（:227-243 类型 + :249-257 关系），无复杂度问题但 O(2N)。
  4. `providers_rep_version` 不含 Accept-Encoding 协商状态（coding 区分由 compute_etag 的 actual 参数承载）——与 etag.py 一致，无问题，记录以免误判。

### src/oc_slimapi/sse/__init__.py（1）
- 职责：包 docstring（"Curated SSE bridge package."），无再导出。
- 对外符号：无。依赖：无。被依赖：包标记。状态：无。错误路径：无。
- 疑问点：无。

### src/oc_slimapi/sse/tokenstream/__init__.py（8）
- 职责：tokenstream 包聚合门面——从 frames/models/hub/subscriber 再导出 16 符号（含 `_connected_frame` 等 8 个私有名）。
- 对外符号：`STOP`、`sse_frame`、`PartKey`、`DeltaAccumulator`、`LivePart`、`_TokenMetrics`、`TokenStreamHub`、`TokenSubscriber`、`TokenSubscriberCapacityError`、`TokenStreamRegistry` 及 6 个 `_xxx_frame`/`_now_ms` 私有再导出。
- 依赖：.frames/.models/.hub/.subscriber。被依赖：`sse/token_hub.py` shim 经包路径转发；生产代码直接 import 点见 E1-05 卡（app.py:36、routes/token_stream.py:61、global_hub.py:55）。
- 状态：无。错误路径：无。
- 疑问点：私有符号（`_frame` 系列、`_now_ms`、`_TokenMetrics`）经 `__init__` 公开化——与 frames.py 卡片「三处双实现漂移」疑问叠加，扩大了 shim 面积。

<!-- ==== e1-13-traffic-observability ==== -->
# E1-13 流量观测链精读卡片（traffic / access_log / snapshot / middleware / request_id / metrics / sse_observability）

> 审计探索卡片，只读产物。引用格式 `src/...:行号`。全部七文件已全文精读（行数经 wc -l 核对）。

---

## src/oc_slimapi/traffic.py（845 行）

### 职责
全链路双向字节账本（TrafficLedger）+ 路径→逻辑 bucket 分类（bucketize）+ expand category 白名单归一 + v3 §9.2 观测矩阵 / SSE 生命周期存量 + v4 sessions degraded 每响应计数器（独立于 ledger 开关）。单 uvicorn worker / 单事件循环模型，`threading.Lock` 仅作诚实防护（docstring :5-8）。

### 对外符号
- `_LATENCY_SAMPLES`（:40）— 每 bucket 延迟样本 deque 上限（1024，最旧逐出）。
- `EXPAND_CATEGORIES`（:50-63）— 12 个 expand category 冻结表（单一事实源：versions 路由 capability 广告 + ledger 白名单）。
- `EXPAND_CATEGORIES_SET`（:64）、`_EXPAND_INVALID_CATEGORY = "invalid"`（:68）。
- `_normalize_expand_category(category)`（:71-81）— 白名单外 category 一律折叠到 `invalid`，防 `_expand` dict 无界增长（rev-gpt R1 M2）。
- `SSE_BUCKETS = {events_sse, token_stream_sse}`（:88）。
- `bucketize(method, path)`（:91-192）— 路径→bucket 映射（前缀序，specific 在前）。
- `_EXPAND_SEGMENT = "expand/"`（:200）、`_expand_tail(path)`（:203-222）— 段严格的 expand 路径尾部提取（空 sid / 裸 `/expand` 不匹配）。
- `expand_category_from_path(path)`（:225-240）— 提取原始 category 段（不白名单化，可返回空串/伪造值）。
- `_UP_IN_KEY/_UP_OUT_KEY/_CACHE_KEY`（:245-247）— scope state stash 键。
- `SESSIONS_SOURCE_STATE_KEY`（:258）、`DEGRADED_503_STATE_KEY`（:259）、`SESSIONS_SOURCE_VALUES = {"db","http"}`（:262）— v4 degraded 状态标记键拼写单一事实源。
- `stash_cache(request, state_value)`（:265-281）— catalog cache hit/miss stash（None 为 no-op）。
- `stash_up_in(request, n)`（:284-300）/ `stash_up_out(request, n)`（:303-313）— handler 累积 upstream 字节到 scope state。
- `_read_state_int(scope, key)`（:316-324）— 读 stash int（bool 拒绝、非正归零）。
- `read_sessions_source(scope)`（:327-337）/ `read_degraded_503(scope)`（:340-345）— 校验式读取 degraded 标记（垃圾值忽略）。
- `SessionsDegradedCounters`（:348-391）— per-response degraded 计数（`record_degraded_200` :378、`record_fail_closed_503` :382、`snapshot` :386），挂 `app.state.sessions_degraded`，刻意不随 traffic 开关关停。
- `SESSIONS_DEGRADED_STATE_ATTR`（:395）、`ensure_sessions_degraded_counters(state)`（:398-417）— 同步 get-then-set 惰性挂载（setattr 失败返回临时实例）。
- `TrafficLedger`（:420-845）：
  - `__init__`（:440-449）：一把 `threading.Lock` + 8 个累积结构（`_buckets/_sse/_latencies/_v3_matrix/_v3_sse/_expand/_v4_degraded`）。
  - `enabled` property（:451-453）。
  - `record_downstream`（:457-490）— 下行 HTTP：requests/downIn/downOut/errors4xx/errors5xx + 有界延迟样本。
  - `record_upstream`（:494-518）— 上行 HTTP：upOut/upIn（`method`/`status` 保留未用 :508-511）。
  - `record_sse_upstream`（:522-541）— 共享 `/global/event` 上行字节（只喂 `events_sse`，防双计）。
  - `record_sse_downstream`（:545-556）— 每帧下行字节 + framesEmitted。
  - `_new_bucket`（:558-568）/ `_new_sse`（:570-572）。
  - `_v3_status_class`（:576-580）— status→"Nxx"（非 int / bool → "none"；int 0 → "0xx"）。
  - `record_selector_request`（:582-611）— v3 §9.2 扁平键 `selectorResult|wireVersion|directoryForm|recordType|statusClass|bucket` 计数（自启动累计）。
  - `record_sse_lifecycle`（:613-635）— SSE open/close 存量（active 钳 0、孤儿 close 计 orphanCloses）。
  - `record_expand`（:640-672）— `category|status` 计数 + bytes（category 先经 :71 白名单化）。
  - `record_sessions_degraded`（:676-705）— v4 扁平键 `degraded|kind|statusClass|bucket`。
  - `snapshot`（:709-845）— `/slimapi/metrics` 的 `traffic` 块（buckets 合并 SSE、totals、ratios（upIn>0 才有）、latencyMs p50/p90/p99/count、`v3.matrix/sseLifecycle/sseActive`、`expand`、`v4.degradedMatrix`（稀疏，首条 degraded 后才出现 :843-844））。

### 依赖 / 被依赖
- 依赖：仅 stdlib（threading/collections/typing）。零仓库内 import（无循环）。
- 被依赖（rg 反查）：`middleware/traffic_accounting.py:61`、`app.py:38`（TrafficLedger 实例化）、`routes/versions.py:54`（EXPAND_CATEGORIES 广告）、`routes/messages.py:1271-1272`（文件中部 import 冻结表）、`routes/metrics.py:15`、`discovery.py:39` / `routes/_read_passthrough.py:61` / `permissions.py:16` / `health.py:9` / `_catalog_common.py:33` / `read_groups.py:78` / `questions.py:16` / `write_groups.py:83` / `sessions.py:37`（stash_up_* / stash_cache）、`sse/global_hub.py:52` / `sse/registry.py:29`（TYPE_CHECKING）、`sse_observability.py:29`（TYPE_CHECKING）；测试 10+ 文件。

### 状态 / 可变性
- 单把 `threading.Lock`（:441）守全部可变结构；无后台 task、无文件句柄。
- 除 `_latencies`（deque maxlen=1024，:40/:490）外全部单调递增、无上界重置（重启即失——由 traffic_snapshot 落盘补偿）。
- 键空间有界性：`_buckets` 由 bucketize 固定集界定；`_expand` 由 (12+1)×观测 status 界定；`_v3_sse` 靠调用方归一到 5 dim；`_v3_matrix` 界定依赖调用方值域（见疑点 5）。

### 错误路径
- 无 IO。`enabled=False` 时所有 `record_*` no-op、`snapshot()` 返回 `{"enabled": False}`（:762-763）。
- `ensure_sessions_degraded_counters` setattr 失败返回临时实例、绝不 raise（:410-416）。

### 疑问点（14）
1. **`passthrough` 桶名与现实不符（:192）**：3.0.0 反代已关闭，非 `/slimapi` 请求由 `proxy.py:44-51` 统一 404 `thin_route_not_found`；bucket 仍叫 "passthrough"，且 selector 对非 /slimapi 请求 stash `not_applicable`（`selector.py:72,104`）→ v3 矩阵出现 `not_applicable|…|passthrough` 键。`docs/manual/traffic-accounting.md` "按 bucket==passthrough 找未省流请求" 的运维口径已失真（现在全是被拒 404，非真实过境流量）。
2. **`sweep` 桶（:109-110）疑似死桶**：B1b shadow 预留路径 `/slimapi/_shadow/sweep`，而 `routes/metrics.py:40-41` 明言 "no HTTP sweep is issued"——无路由命中，桶永不出现。
3. **`/slimapi/config/` 仅匹配带尾斜杠前缀（:151）**：裸 `/slimapi/config`（无尾斜杠）落入 `other`（:190）而非 `providers`——需对照路由注册形态确认归桶口径一致。
4. **前缀无尾斜杠约束的过匹配**：`startswith("/slimapi/sessions")`（:141）、`startswith("/slimapi/messages")`（:124）会把 `/slimapi/sessionsfoo` 等归入 sessions/messages（此类路径最终 404，影响仅是 404 计入该桶 errors4xx）。
5. **`record_selector_request` 无值域白名单（:602-609）**：selector_result/wire_version/directory_form 由 middleware 透传 selector state 原值（`traffic_accounting.py:284-288`）；若 selector 未来引入新 result 值，矩阵键集静默扩张——与 `_expand` 的白名单防御（:71-81）不对称。
6. **`record_sse_lifecycle` 不校验 `result`（:613-626）**：键可为任意字符串；当前唯一调用方 `sse_observability._dims`（:59-60）已归一到 `SSE_RESULT_DIMS`，但 ledger 侧零防御（对比 :71）。
7. **status=0 产生 "0xx" 键且无错误计数**：middleware `status_code` 初值 0（`traffic_accounting.py:181`），app 未发 `http.response.start` 即正常返回时 status=0 入账；`_v3_status_class`（:577-580）对 int 0 输出 "0xx"；`:486-489` 的 4xx/5xx 判定均不命中 → 该行零错误分类。
8. **分位数口径**：`samples[min(n-1, int(n*0.50))]`（:805-807）为 floor 索引（n=100 时 p99 取第 98 位样本），与常见 nearest-rank ceil 口径略有偏差——分析侧需知。
9. **latency 语义异质**：deque 只保最近 1024 样本（:40），latencyMs 是"近期"分位而非全程；SSE 桶的 duration_ms 是整连接生命周期（连接关闭才记账）→ 与 HTTP 桶延迟不同质，混读易误。
10. **snapshot 持锁做 O(n log n)**：`list(...)` 拷贝 + `sort()`（:800-803）在 `self._lock` 内执行；`/slimapi/metrics` 高频拉取与所有 record_* 争锁（量级小但热点在锁内）。
11. **`record_upstream` 的 method/status 保留未用（:508-511）**：签名宽于用途，易误导调用者以为有 per-method 拆分。
12. **`ensure_sessions_degraded_counters` 临时实例丢计数（:410-416）**：只读 state 对象部署下 sessionsDegraded 恒 0（best-effort 已注释声明，但无可观测信号提示降级发生）。
13. **`snapshot()` SSE 口径差已文档化（:750-757）**：活跃 SSE 期间 `downOut>0` 而 `requests==0`——契约已知项，非 bug，审计下游消费者需容忍。
14. **`v4` 块稀疏出现（:843-844）**：`set(traffic)` 精确形状消费者在首条 degraded 前后看到不同键集——zero-knowledge additive 约定，消费端需容错。

---

## src/oc_slimapi/access_log.py（726 行）

### 职责
结构化 JSONL 访问日志：`DailyAccessHandler` 按天写 `access-YYYY-MM-DD.jsonl`（每行一请求/SSE 生命周期标记），独立函数做 gzip 压缩 / 按保留天数清理 / 旧格式迁移，async 维护循环周期执行；全程 best-effort（失败 warning、绝不 raise，:19-23）。

### 对外符号
- `_LOGGER_NAME`/`_setup_lock`（:43-44）；`_MAINT_LOCK`（:56）— 跨线程序列化 compress/prune/migrate（整函数粒度）。
- `_active_handler_ref`（:63）— 当前已装 handler 引用（P1-25：维护期避免 unlink 活 handler 持有的 .jsonl）。
- `_get_maint_log()`（:71-75）— 维护日志（独立于 access logger，防诊断 warning 污染 jq 解析的 jsonl，:65-68）。
- `_ACCESS_LOG_RE`（:79）— 严格日文件名正则 `^access-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$`。
- `get_access_logger()`（:87-89）。
- `hash_client_id(raw, salt=None)`（:92-103）— sha256/hmac-sha256 前 16 hex。
- `DailyAccessHandler(logging.Handler)`（:111-203）：`__init__` :123、`__del__` :129、`_ensure_dir` :137、`_open_file` :140（append 模式）、`_close_current_fh` :144、`current_path` property :155-169（P1-25 读口径）、`emit` :173-198（按 `record.created` 定日期跨零点换文件；单调用行写 :195 + flush :196）、`close` :200。
- `setup_access_log(*, enabled, dir)`（:211-259）— 幂等安装/清理旧 handler；失败降级 `logger.disabled=True` 绝不 raise。
- `write_access_log(...)`（:267-364）— 每请求行（固定键集 + 稀疏 `cache`/`sessionsSource`/`degraded503` 尾字段，:350-363）。
- `write_sse_lifecycle_log(...)`（:367-405）— sse_open/sse_close 行（无字节/时长字段）。
- `_unique_tmp_path(base, suffix)`（:413-425）— `.{suffix}.{pid}.{uuid8}` 唯一临时名。
- `_cleanup_leftover_tmp(dir)`（:428-446）— 删孤儿 tmp（含 PID 域变体与 legacy 变体）。
- `compress_old_access_logs(dir, today)`（:449-544）— 压缩 < today 的 .jsonl：严格命名校验、已存在 .gz 跳过、活 handler 文件跳过（P1-25 :509-515）、unique tmp + `os.replace` 原子提交、源删除失败保 .gz。
- `prune_old_access_logs(dir, retain_days, today)`（:547-583）— 删除 `file_date < today - retain_days` 的 .jsonl/.jsonl.gz（边界日保留；retain<=0 no-op）。
- `migrate_legacy_access_log(dir)`（:586-625）— 旧 `access.jsonl(.N)` → `access-legacy-{mtime:%Y%m%d}-{N}.jsonl.gz`。
- `_migrate_one(path, label, log)`（:628-660）— 单文件 gzip+替换+删源（BaseException 清理 tmp）。
- `run_access_log_maintenance_loop(*, dir, retain_days, interval_s, stop_event, extra_prune)`（:668-726）— 循环 compress→prune→extra_prune（均 `asyncio.to_thread`），单失败不杀循环；不负责 cancel 时 join 线程（:691-700 契约注释）。

### 依赖 / 被依赖
- 依赖：stdlib + `.logging_config.get_logger`。
- 被依赖：`middleware/traffic_accounting.py:58`（get_access_logger/hash_client_id/write_access_log）、`sse_observability.py:23`（get_access_logger/write_sse_lifecycle_log）、`app.py:14-17`（setup/migrate/maintenance loop）；测试 `tests/test_access_log.py` 等。

### 状态 / 可变性
- 模块级单例：`_active_handler_ref`（:63，仅 setup_access_log 在 `_setup_lock` 下写）、`_MAINT_LOG`（:67 惰性）。
- `_MAINT_LOCK`（:56）进程内锁——**跨进程无效**（见疑点 4）。
- handler 持一个 append 模式文件句柄，跨零点首条 emit 时切换（:186-191）；每行 flush（:196）、无 fsync。
- 维护循环在 app.py 作为 asyncio task 运行（`app.py:680-683`），shutdown 经 stop_event + drain 超时 cancel（`app.py:693-718`）。

### 错误路径
- emit 异常 → `handleError`（:198，logging 默认行为：`raiseExceptions=True` 时写 stderr）。
- setup 失败 → warning + `logger.disabled=True`（:252-258）；app.py:243-251 以实际安装结果 gate 维护循环（P1-39）。
- compress/prune/migrate 单文件失败 → warning 继续；循环内三段独立 try（:711-726）。

### 疑问点（12）
1. **【实证】legacy 归档永不清理**：`prune_old_access_logs` 的 glob `"access-*.jsonl.gz"`（:566）会命中 `access-legacy-20260701-1.jsonl.gz`，但 `_ACCESS_LOG_RE`（:79）要求 `access-` 后紧跟 `\d{4}-\d{2}-\d{2}`，legacy 名不匹配 → `continue`（:568-569）。RETAIN_DAYS 永远触不到 legacy 归档，一旦迁移产生便永久留存（已用 Python re 实测：legacy→False，daily→True）。
2. **每行 write+flush 两次系统调用、无 fsync（:195-196）**：进程硬崩丢页缓存尾部行；且与 traffic_snapshot P1-27 的"单 write 调用 POSIX 原子性"口径不同源（此处 write(msg+"\n") 已是单 write 调用 :195，但紧随 flush，注释自认"缩小而非消除半行窗口"）。
3. **行内 ts 与文件名日期双时钟**：文件名日期取 `record.created`（:186），行内 `ts` 取 `write_access_log` 调用时的 `datetime.now()`（:335）——logger 无队列时几乎同刻，但两采样点独立，理论上可分属两日（与 snapshot P1-26 单采样点修复对照，此处未做同等处理）。
4. **`_MAINT_LOCK` 进程内锁 + tmp 清理无年龄判断（:428-446）**：多 sidecar 实例共享同一 logs 目录时，B 进程的 `_cleanup_leftover_tmp` 可误删 A 进程 in-flight 的 unique tmp（→ A 的 os.replace 失败降级 warning）。单 worker 假设散见注释但代码处无断言/无锁文件。
5. **损坏 .gz 无自愈**：已存在 .gz 即跳过（:500-501，注释自认 "a damaged .gz … is not re-compressed"），且源 .jsonl 已删 → 该日数据可用性依赖人工干预。
6. **压缩成功但源删除失败（:526-532）→ .jsonl/.gz 双存**：下次 tick 因 .gz 已存在而跳过（:500-501），源文件不会被重试删除；仅当 retain_days>0 时由 prune 兜底（:566-575 会清 .jsonl）；retain_days=0（默认）下永久双存。
7. **`prune` 边界多留一天（:562,575）**：`deadline = today - retain_days`、`file_date < deadline` 才删 → retain_days=3 实际保留 4 个日历日文件（today-3 边界保留）。与直觉"保留 3 天"差一天，文档口径需核对。
8. **`handleError` 走 logging 默认（:198）**：`logging.raiseExceptions=True`（开发默认）时向 stderr 打 traceback——生产噪音渠道，未接 get_logger 体系。
9. **recordType 过滤陷阱（任务点名）**：`write_sse_lifecycle_log` 的 sse 行同样带 bucket/status（:392-404），聚合侧 `aggregate_v3_observability` 将其计入 counts 矩阵（键含 recordType 维）——消费 jq 若忘按 `recordType=="request"` 过滤，SSE 桶"请求数"被 open/close 行放大约 3 倍。
10. **`hash_client_id` 16 hex = 64 bit（:102-103）**：生日碰撞 ~2^32 量级；单部署设备数下够用，但无 salt 时跨部署可链接（sha256 无密钥）——隐私声明依赖运维配 salt。
11. **shutdown 不 join in-flight gzip 线程（:691-700）**：app.py drain 超时后 cancel（`app.py:703-718`），进程退出时后台 gzip 线程可能被硬杀留下 unique tmp——依赖下次启动 `_cleanup_leftover_tmp`（:428）兜底，冷启动前目录残留。
12. **`write_access_log` 的 `status` 形参未做值域检查（:339）**：middleware 传 0 时行内 `"status": 0`（联动 traffic_accounting 疑点 8），jq 按状态类过滤时 0 行成黑洞。

---

## src/oc_slimapi/traffic_snapshot.py（541 行）

### 职责
两块：(a) `prune_old_snapshots` 按天清理旧快照文件；(b) v3 §9.2 纯分析函数 `aggregate_v3_observability`（access log 行 → 跨日矩阵/SSE 配对序列）；(c) `TrafficSnapshotter` 后台循环把 `TrafficLedger.snapshot()` 全量落盘为每日 `traffic-snapshot-YYYY-MM-DD.jsonl`（SSE 上游字节成本的唯一持久载体，重启即失的补偿）。

### 对外符号
- `_snapshot_file_re(stem)`（:75-76）— 快照文件名正则。
- `prune_old_snapshots(directory, stem, retain_days, today)`（:79-101）— 删 `file_date < today - retain_days`（retain<=0 no-op）。
- `_SSE_DIMS`（:111）— `("v2","v3","v4","absent","not_applicable")`，与 `selector.SSE_RESULT_DIMS` 手工双拷贝（注释自认 grep-verified）。
- `_DEGRADED_KINDS`（:117）、`_DEGRADED_SEED_KEYS`（:122-125）— 每日 degraded 图固定种子键。
- `_v3_row_key(row)`（:128-139）— 行→扁平矩阵键（`str(x or "null")` 空值折叠）。
- `_degraded_row_key(row)`（:142-164）— 稀疏标记 → `degraded|kind|statusClass|bucket`（无标记返回 None）。
- `aggregate_v3_observability(records)`（:167-286）— 输出 `counts/countsByDate/sseActive(窗口期初存量)/sseOpens/sseMatchedCloses(按 lifecycleId §11.8 配对)/sseOrphanCloses/sseLive/degradedCounts(degradedCountsByDate)`。
- `TrafficSnapshotter`（:289-541）：`__init__` :318-335（dir+stem 模板、bootTs/runId/pid/start_monotonic）、`start` :341-372（首帧同步写，失败永久 inactive）、`stop` :374-400（cancel 后必写终帧）、`active` property :402-405、`_loop` :411-428（逐迭代兜异常）、`_path_repr` :430-432、`_write_once` :434-541（单时钟采样点 P1-26 :452/501；mkdir best-effort :502-510；单 write 调用 P1-27 :532-533；明确不 fsync :524-531）。

### 依赖 / 被依赖
- 依赖：stdlib only（`ledger` 参数 duck-typed 防循环，:321）。
- 被依赖：`app.py:39`（TrafficSnapshotter + prune_old_snapshots）、`app.py:291-299`（实例化/stop 回调）、`app.py:668-674`（prune 经 functools.partial 挂为 access-log 循环 extra_prune）、`app.py:720-722`（start，双开关 gate）；`aggregate_v3_observability` 无 src 内调用方（纯分析时工具，tests + manual 使用）。

### 状态 / 可变性
- snapshotter 持一个 asyncio Task（:335）；每帧 open→write→close，无常驻句柄（:513-534）。
- 累积器上界：ledger 侧见 traffic.py 卡片；快照**文件**每 interval（默认 300s，config:552-554）追加一行全量 ledger JSON，无单文件大小 cap、无压缩（对比 access log 的 gzip 链）——文件体积 = 帧数 × 全量键集大小，仅按天 rotate + retain prune 约束。
- `aggregate_*` 为纯函数（局部状态）。

### 错误路径
- 首帧失败 → 永久 inactive + warning（:359-369）；循环迭代失败 → warning 继续（:424-428）；终帧 best-effort 忽略返回值（:399-400）；mkdir/open 失败 warning 返回 False（:502-541）。
- prune 单文件 unlink 失败 → warning（:97-100）。

### 疑问点（11）
1. **快照文件永不压缩**：prune 只删不压（:79-101），无 compress 对应物——与 access log 的 gzip 生命周期不对称；300s 全量帧（含全 v3 矩阵键）长期裸存，磁盘占用可观（生产 retain=30 天时 30 × 288 帧全量 JSON）。
2. **RETAIN_DAYS 清理链耦合缺陷（任务点名）**：snapshot prune **只**经 extra_prune 挂在 access-log 维护循环上（`app.py:668-683`），而该循环整体 gated on `access_log_active`（`app.py:243` 安装实际结果）——`OC_SLIMAPI_ACCESS_LOG_ENABLED=false`（或目录安装失败）+ snapshot enabled 时，snapshotter 照常每 300s 写文件（`app.py:720-722` 独立启动）但**清理永不运行** → 快照目录无界增长。无任何告警暴露此状态。
3. **`_SSE_DIMS` 双拷贝漂移风险（:111）**：与 `selector.SSE_RESULT_DIMS`（selector.py:109）靠注释纪律同步（"the only two copies; grep-verified"），无 import 复用。
4. **聚合假设 append 序（:168-170）**：`day_start_stock` 在每日首行处理前冻结（:228-231）——若输入非严格时间序（gz 解压拼接顺序错乱），期初存量取错；纯约定无排序防御。
5. **缺 ts 行落 "unknown" 伪日期（:224）**：`str(row.get("ts",""))[:10] or "unknown"`——聚合输出出现 "unknown" 日期键并占 day_order 一位。
6. **counts 不过滤 recordType（:239-241）**：request/sse_open/sse_close 全计入矩阵（键第 4 段可区分）——消费侧忘过滤即三倍计数（联动 access_log 疑点 9）。
7. **lifecycleId 配对跨日但 open_ids 无窗口回收**：`open_ids[dim]`（:213）跨整个聚合窗口累积，未关闭的 open（活跃连接或进程崩溃遗留）永不回收——窗口末 `sseLive` 含全部悬挂 open（by design :194）；巨窗聚合时 set 大小 = 活跃+悬挂连接数，无上界告警。
8. **终帧与循环末帧可能同秒重复**：stop 先 cancel（sleep 处生效）再写终帧（:389-400）——相邻两帧可能零 delta 重复；分析侧 delta 推导需容忍（设计已知，未在输出标记 final 帧）。
9. **首帧失败无自愈（:359-369）**：瞬时磁盘满也永久 inactive 直至重启；snapshotter 的 active 状态不经 `/slimapi/metrics` 暴露（仅日志可见）——运维盲区。
10. **prune glob 未转义 stem（:85）**：regex 侧 `re.escape`（:76）已防，glob 侧 `f"{stem}-*.jsonl*"` 未转义——stem 含 glob 元字符（`[`/`*`）时行为漂移（现实 stem 为固定配置值，低风险）。
11. **`_write_once` 的 enabled=False 早退 return True（:463-464）**：start 已在 :356 检查且 enabled 不可变——防御性死分支，无害但暗示对 ledger 状态的不信任未消除。

---

## src/oc_slimapi/middleware/traffic_accounting.py（435 行）

### 职责
纯 ASGI 记账中间件：包 receive/send 计下行线字节（downIn/downOut，wire 口径），请求结束时读 handler stash 的 upstream 字节并入账，写一行 access log，喂 v3 矩阵 / expand / v4 degraded；SSE 真流（200+text/event-stream）downOut 交由 per-frame 计数器防双计。

### 对外符号
- `_ledger_from_scope(scope)`（:80-88）/ `_config_from_scope(scope)`（:91-99）— app.state best-effort 查找。
- `_CLIENT_IDENT_HEADERS`（:103-107）— X-Client-Name/Version/Id 头名→槽位。
- `_read_client_headers(scope)`（:110-144）— 校验（≤128 UTF-8 字节、无控制字符、非空白；重复头首个有效值胜）。
- `TrafficAccountingMiddleware`（:147-246）：`__init__` :152-159、`__call__` :161-246（非 http 直通 :162-165；client 头 stash :173-177；`counted_receive` :185-193 / `counted_send` :195-212（status+content-type 捕获）；BaseException 路径先记账再 raise :216-233）。
- `_record(...)`（:249-435）— 请求终点记账：access log 写入（:297-344）→ ledger 记账（record_downstream/selector/expand/degraded/upstream :346-419）→ app.state degraded 计数器（:425-435，独立于 ledger 开关）。

### 依赖 / 被依赖
- 依赖：`access_log`（get_access_logger/hash_client_id/write_access_log）、`logging_config`、`selector`（SELECTOR_STATE_KEY/DIRECTORY_FORM_STATE_KEY）、`traffic`（bucketize/SSE_BUCKETS/stash 键/read_* 等）、`request_id`（REQUEST_ID_KEY）。
- 被依赖：`app.py:26,753`（add_middleware）；测试 test_traffic_integration/test_expand_config 等。

### 状态 / 可变性
- 无实例级可变状态（`__slots__ = ("app","logger")`，:150）；计数全部委托 ledger / logging。
- 作用域内闭包计数器 `down_in/status_code/down_out/content_type`（:179-182）随连接生命周期。

### 错误路径
- `_record` 三段独立 try/except（access log :343-344；ledger :418-419；degraded 计数器 :434-435），全部 warning 吞异常——记账绝不破坏请求。
- 中间件异常路径记账 `status_code or 500`（:225）。

### 疑问点（13）
1. **404/405 覆盖、501 不覆盖（任务点名）**：HTTP catch-all 404（`proxy.py:44-51`）走正常路由栈 → 记账（bucket=passthrough、errors4xx）；405（FastAPI 路由层异常经 ExceptionMiddleware 转 response，过 counted_send）覆盖；**WS 501 stub（`proxy.py:34-38`）scope type="websocket" → `__call__` :162-165 直接放行，无 access log 行、无 ledger 计数**——websocket 类型对记账全盲。
2. **未处理异常 500 的响应字节丢失**：Starlette ServerErrorMiddleware 在本中间件**之外**生成 500 响应，绕过 counted_send/send_with_rid → 该响应体不计 downOut、无 X-Request-ID 回显头；except 路径（:216-233）行内 down_out 仅为异常前已发字节（通常 0）。注释 :217-218 "disconnects / 500s still count" 对行成立、对字节不成立。
3. **"Outermost" 文档漂移（:148）**：类 docstring 自称 outermost，但 `app.py:755` RequestIdMiddleware 后加（Starlette last-added=outermost）→ 真实序 RequestId > TrafficAccounting > Selector；`app.py:750-752` 注释同病。
4. **SSE 桶 upstream stash 一律忽略（:410-417）**：`if not is_sse and (up_in>0 or up_out>0)`——SSE 路径上的**非流式**错误响应（400/503，is_sse 按 bucket 恒真）若 handler 曾 stash up_in（如已向上游发请求），字节只进 access log 行、不进 ledger（events_sse.upIn 恒由 hub 独供）。是否所有 SSE 错误路径零 stash 需 routes/events.py、token_stream.py 佐证（本次范围外）。
5. **early-reject body 不计 downIn（:25-34 文档化）**：version gate 400 等未 consume 的请求体字节不入账——wire 真实口径的代价，已知约定。
6. **SSE 双计防护依赖 content-type 约定（:354-368）**：`is_real_sse_stream` 需 status==200 且 content-type 含 text/event-stream；注释自认未来 SSE 变体漏设头即静默双计 downOut——无断言/无测试外的防线。
7. **status=0 入账（:181,239）**：app 未发 response.start 即正常返回（理论路径）→ 行/矩阵出现 status 0 / "0xx"（联动 traffic.py 疑点 7）；异常路径有 `or 500` 兜底、正常路径无。
8. **`_record` ledger 段异常标签误导（:419）**：统一 log "record_upstream failed"，但该段含 downstream/selector/expand/degraded 全部 record 调用——排障时日志指向错误。
9. **access log 行先于 ledger 写（:319 → :346）**：ledger 记账失败时行已落盘——行与账本可短暂不一致（各自 best-effort，无补偿）。
10. **client 头与 request_id 的重复头策略不一致（:127-128 vs request_id.py:45-59）**：client 头"首个**有效**值胜"（先出现的无效值不阻塞后续重复头）；request_id"首个匹配头即定，无效则整头弃用"（不尝试第二个有效值）。
11. **client_id 明文模式（:314-317）**：`client_id_hash=false` 时设备 id 明文入日志（运维开关的隐私权衡；fail-closed 默认 hash :310-313 正确）。
12. **`traffic_client_*` state 键信任边界（:174-177 写、:300-303 读）**：中间件写后 handler 理论可覆盖 state 值（当前无此用例）；读取无二次校验。
13. **content-type 取首头（:203-207 break）**：畸形多 content-type 请求按首头判 SSE——与 RFC 单头假设一致，无实质风险（记录在案）。

---

## src/oc_slimapi/middleware/request_id.py（111 行）

### 职责
纯 ASGI X-Request-ID 注入/提取：入站头（可打印 ASCII、≤128 字符）采用否则生成 uuid4.hex；存 `scope["state"]["request_id"]`；HTTP 响应头回注（过滤内层同名头）；WebSocket 仅存 state。

### 对外符号
- `REQUEST_ID_KEY = "request_id"`（:23）。
- `_find_request_id(scope)`（:29-60）— 入站头校验（strip、非空、≤128、全字节 0x20-0x7e；P1-15 非 ASCII 拒绝防 httpx build 时异常 500）。
- `RequestIdMiddleware`（:63-111）：`__slots__=("app",)` :73、`__init__` :75-76、`__call__` :78-111（非 http/ws 直通 :79-81；ws 分支仅存 state :109-111；`send_with_rid` :94-106 过滤+追加单头）。

### 依赖 / 被依赖
- 依赖：stdlib（uuid）。
- 被依赖：`app.py:25,755`（最外层中间件）、`upstream.py:9,140`（读 REQUEST_ID_KEY 转发上游）、`sse_observability.py:72`（函数内 import 读 rid）；`traffic_accounting.py:72`（import 常量，access log 行 requestId 字段）；测试 test_request_id/test_command_routes。

### 状态 / 可变性
- 无可变状态（纯转发 + state 注入）。

### 错误路径
- 无显式 try/except：入站头解析全部防御式返回 None → 生成新 id；无 raise 路径。

### 疑问点（6）
1. **客户端可注入 request_id（:84-86 直接采用）**：rid 进 access log（`traffic_accounting.py:330`）且被 proxy 转发上游（upstream.py:140）——排障关联性可被外部污染（固定 rid 混淆归因）；服务端未区分"内生成 vs 外来"（无前缀命名空间）。
2. **重复 X-Request-ID 头只看第一个（:45-59）**：首个无效（超长/非 ASCII）→ 弃用整头重新生成，不尝试后续重复头的有效值——与 `_read_client_headers` 的 lenient 策略相反（见 card 4 疑点 10）。
3. **内层 X-Request-ID 被无条件替换（:97-105）**：handler 显式设置的不同 rid 会被覆盖（当前仓库无此用例；upstream 回显场景两值相同无实害）。
4. **WS 无 rid 回显（:109-111）**：WS 501 响应无 X-Request-ID 头，且 traffic middleware 对 ws 不记账（联动 card 4 疑点 1）——WS 探测请求在两个观测面都不可见。
5. **`rid.encode("utf-8")`（:104）**：校验已限 ASCII，等价 ascii 编码——防御冗余无害。
6. **docstring 顺序声明（:8-12）与实际栈序一致但表述反直觉**："registered *after* the traffic-accounting middleware" 才能使其更外层——读者易误解为内层；与 card 4 疑点 3 的文档漂移同源。

---

## src/oc_slimapi/routes/metrics.py（111 行）

### 职责
`GET /slimapi/metrics`（T3 观测端点）：聚合 hub registry 快照 + 可选附加块（tokenStream/traffic/sweep/dbaux/sessionsDegraded/replay），gzip 协商响应。自身零业务逻辑，纯拼装。

### 对外符号
- `router = APIRouter(prefix="/slimapi", tags=["metrics"])`（:17）。
- `metrics(request)`（:20-111）— 唯一 handler：
  - `hubs.snapshot_metrics()`（:22）→ `{sse:{subscribers,hubs,clients},skeleton}` + `batch=None`（:23）。
  - `tokenStream`（:29-31，有 token_registry 才有）。
  - `traffic`（:36-38，有 ledger 才有，直接内联 `ledger.snapshot()` 全量）。
  - `sweep`（:42-44，qp_sweep 存在且 enabled 才有）。
  - `dbaux`（:51-69，available/mode/reason/generation/source/latency{p50_ms,p99_ms,samples,total}/breaker_open/counters；注释 :48-50 声明不回显 DB 路径）。
  - `sessionsDegraded`（:80-84，计数器已挂载才有，`{degraded_200,fail_closed_503}`）。
  - `replay`（:97-107，epoch + domains/frames/bytes/barriers + 其余计数器；注释 :94-96 声明不泄帧载荷/目录路径）。
  - `json_response(..., accept_encoding=...)`（:108-111）。

### 依赖 / 被依赖
- 依赖：`gzip_util.json_response`、`traffic.SESSIONS_DEGRADED_STATE_ATTR`、app.state（hubs/token_registry/traffic_ledger/qp_sweep/dbaux/replay_log）。
- 被依赖：`app.py:29`（import）+ `app.py:760`（include_router 元组）；INTERFACE_MAP 有记录（check_routes_doc 校验对象）。

### 状态 / 可变性
- 无自有状态；每次调用现拉各源快照（`ledger.snapshot()` 在 ledger 锁内做拷贝+排序，见 traffic.py 疑点 10）。

### 错误路径
- `request.app.state.hubs`（:22）无 getattr 容错——未挂 hubs 的 app 直接 AttributeError→500（生产恒挂；对比后续块全部 getattr 容错）。
- 其余块缺席即省略（zero-knowledge additive 约定）。

### 疑问点（9）
1. **敏感信息审查（任务点名）**：暴露字段全集中无 query string、无目录名、无文件路径——`subscriberId` 是随机 token（`sse/hub_types.py:240` `"sub_" + secrets.token_hex(4)`）；replay `domains` 是计数（:100-103）；traffic buckets 仅桶名。**待查项**：`dbaux.reason`（:57-58）的具体取值本文件不可见（注释 :48-50 声称不回显 DB 路径、`source` 仅通道标签）——需 dbaux.snapshot 卡片佐证 reason 字符串不含路径/错误原文。
2. **`/slimapi/metrics.traffic` 命名口径（文档 vs wire）**：AGENTS.md 与 traffic.py 注释（:120,:128,:144）以 `/slimapi/metrics.traffic` 指称 traffic 块，但**无此路由**（rg 全仓无注册）——实际是 `/slimapi/metrics` 响应内的 `traffic` 子块；文档写法易被读成独立端点。
3. **metrics 端点受版本选择器管辖**（docstring :7-9）：`GET /slimapi/metrics` 需带 `?v=3`，否则 400 version_required（唯一豁免是 /slimapi/versions）——监控探针/告警抓取必须带版本参数，运维便利性折损且易踩坑。
4. **`state.hubs` 无防御（:22）**：与后续块的 getattr 容错风格不一致（测试 app 面）。
5. **`sessionsDegraded` 首请求前缺席（:80-84 + 注释 :74-79）**：刚启动进程的 metrics 响应无此块，首个过栈请求后才出现——监控需容忍字段缺席（且 handler 先于中间件记账执行，首次 GET 自身不触发挂载）。
6. **`getattr(qp_sweep, "enabled", True)`（:43）默认 True**：无 enabled 属性的 sweep 对象会被当作启用调用 metrics()——与注释 "test apps intentionally omit"（:41）意图相悖的兜底方向（缺属性应默认 False 更保守）。
7. **`batch` 恒 null（:23）**：死键为兼容保留——契约形状冻结项，无消费逻辑。
8. **`hubs_snapshot["sse"]["tokenStream"]`（:31）假定 "sse" 键存在**：registry.snapshot_metrics 恒返回该键（registry.py:359-367）成立——隐式耦合无断言。
9. **无 rate-limit/缓存控制**：每次拉取全量 ledger 快照（含锁内排序）——loopback+stunnel 部署模型下可接受；高频拉取与记账争锁（联动 traffic.py 疑点 10）。

---

## src/oc_slimapi/sse_observability.py（130 行）

### 职责
SSE 生命周期观测：每条 SSE 连接写 `sse_open`/`sse_close` 两行 access log（共享进程单调 lifecycleId 配对）并同步 bump ledger 的 sseActive 存量；全部 best-effort 绝不断流。

### 对外符号
- `_lifecycle_lock`/`_lifecycle_counter`（:31-32）。
- `next_lifecycle_id()`（:35-38）— 进程单调 id（自 1 起，锁内 next）。
- `_access_logger()`（:41-43）— 测试注入点。
- `_dims(scope)`（:46-60）— (selectorResult, wireVersion, directoryForm, sseActive-dim)；缺 selector state → "absent" 维；None scope → 全 null + absent。
- `_ledger_from_scope(scope)`（:63-68）、`_request_id(scope)`（:71-80，函数内 import REQUEST_ID_KEY）。
- `_emit(scope, *, bucket, record_type, lifecycle_id, status)`（:83-114）— 写 lifecycle 行（异常 pass）+ ledger.record_sse_lifecycle（异常 pass）。
- `sse_open(scope, *, bucket)`（:117-125）— open 行（status 恒 200）返回 lifecycle id。
- `sse_close(scope, *, bucket, lifecycle_id)`（:128-130）— close 行（status None）。

### 依赖 / 被依赖
- 依赖：`access_log`（get_access_logger/write_sse_lifecycle_log）、`selector`（SELECTOR_STATE_KEY/DIRECTORY_FORM_STATE_KEY/SSE_RESULT_DIMS）、`traffic`（TYPE_CHECKING）、`middleware.request_id`（函数内）。
- 被依赖：`routes/events.py:16,182,242`（bucket="events_sse"）、`routes/token_stream.py:67,252,307`（bucket="token_stream_sse"）。

### 状态 / 可变性
- 进程级 `_lifecycle_counter`（itertools.count）+ 锁——重启归零（跨重启 orphan 由聚合侧容忍，traffic_snapshot.py:188-192）。

### 错误路径
- `_emit` 两段各自 `except Exception: pass`（:107-108,:113-114）——观测丢失**无任何日志**（连 warning 都没有；与 access_log 模块 "warning + 继续" 的姿态不同）。

### 疑问点（8）
1. **模块 docstring dim 列表漏 "v4"（:12-13）**：写 "v2/v3/absent/not_applicable"，而 `SSE_RESULT_DIMS`（selector.py:109）与 `traffic_snapshot._SSE_DIMS` 均含 v4——文档漂移。
2. **观测丢失零可见性（:107-108,:113-114）**：lifecycle 行/ledger bump 失败静默——"never break the stream" 的代价是 SSE 观测链路自身无健康信号（对比 middleware `_record` 至少 warning）。
3. **sse_open 恒记 status=200（:124）**：调用点在 generator 内流真正建立后，但若 StreamingResponse 实际未发出任何字节（客户端即刻断开），open 行的 200 是断言而非观测事实（close 行 status=None 部分补偿）。
4. **open 后进程崩溃 → 永久悬挂 open**：ledger sseActive 与聚合 sseLive 均高估直至重启（孤儿 close 机制只处理"多 close"，不处理"少 close"）；无对账/心跳校正。
5. **`_dims` 的 None scope 分支（:50-51,117-122）为测试专用**：生产 scope 恒在——测试路径进了生产函数签名（`scope | None`），调用点均 `getattr(request,"scope",None)`（events.py:182 等）防御。
6. **`_request_id` 函数内 import（:72）过度防御**：`middleware.request_id` 不依赖本模块，无真实循环——每次调用多一次 import 查表（开销可忽略，样式噪音）。
7. **`not_applicable` dim 的现实来源存疑**：SSE 端点都在 `/slimapi/**` 下（selector 必然给出 v2/v3/v4/absent 之一）；`not_applicable` 只对非 /slimapi 请求 stash（selector.py:72）——SSE 生命周期行的该 dim 理论上不可达（死维度，与 traffic.py 疑点 1 的 passthrough 残余同源）。
8. **lifecycle 行不含字节字段（by design，access_log.py:385-388）**：SSE 全量观测 = lifecycle 行（open/close 配对）+ request 行（连接级字节，关闭时）+ ledger per-frame 计数三处拼合——排障需按 lifecycleId/requestId join，无单一视图。

---

## 附：跨文件链路要点（RETAIN_DAYS 清理链 / 写盘链 / 记账覆盖面）

- **RETAIN_DAYS 链**：`config.py:537` `access_log_retain_days` 默认 0（生产 systemd 设 3）→ 启动一次性 migrate/compress/prune（`app.py:268-277`）+ 维护循环每小时 compress→prune→extra_prune（`app.py:676-683`，interval 默认 3600s `config.py:540-541`）→ `access_log.py:715-726`。snapshot retain（`config.py:561-562` 默认 0，生产 30）仅经 extra_prune 挂靠该循环（`app.py:668-674`）——**access log 关闭/安装失败时 snapshot 清理随之停摆**（见 traffic_snapshot 疑点 2）。
- **access log 写盘链**：handler 单句柄 append + 每行 flush 无 fsync（access_log.py:195-196）→ 跨零点按 record.created 换文件（:186-191）→ 维护期 gzip（unique tmp + os.replace 原子提交 :517-523，活文件跳过 :509-515）→ prune 双格式（:566-575）。legacy 归档（`access-legacy-*`）例外：**regex 不匹配 → 永不 prune**（access_log 疑点 1）。
- **middleware 记账覆盖面**：HTTP 200/4xx/5xx/404(catch-all)/405(路由层) 全覆盖（含异常路径 500 记行）；盲区 = websocket 类型（WS 501 stub 不记账）、ServerErrorMiddleware 生成的 500 响应字节、early-reject 请求体 downIn。
- **request_id 传播**：入站头/生成（request_id.py:84-89）→ state → traffic_accounting 读入 access log（:299,:330）→ proxy 转发上游（upstream.py:140）→ 响应头回注（request_id.py:94-106，替换内层同名头）。

<!-- ==== e1-14-actions-discovery ==== -->
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

<!-- ==== e1-08-dbaux ==== -->
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

<!-- ==== e1-05-globalhub-hubtypes ==== -->
# E1-05 精读卡片：SSE hub 四文件（global_hub / hub_types / hub / token_hub）

> 生成：2026-08-20 审计探索（只读）。引用格式 `路径:行号`。四文件均已全文精读（非抽样）。

---

### src/oc_slimapi/sse/global_hub.py（1090 行）

#### 职责
进程唯一（process-wide）的上游 `/global/event` SSE 订阅者：单条连接消费上游 GlobalBus 事件流，将其分类（IMMEDIATE 直推 / digest 防抖合并 / token 流路由 / 丢弃），策展后扇出给控制面订阅者（`Subscriber`），并维护 sticky lastError、deleted tombstone、retired-message gate、replay 日志写入、token-hub 镜像路由、T3 观测计数。任务组（run/flush/heartbeat）由 supervisor done_callback 自愈。

#### 对外符号（完整）

模块级：
- `_LAST_UPDATED_AT_BY_SID_MAX = 10_000`（src/oc_slimapi/sse/global_hub.py:60）— 三个 sid 表（`_last_updated_at_by_sid` / `sticky_last_error` / `deleted_tombstones`）共用的 FIFO/LRU 上限。
- `logger`（src/oc_slimapi/sse/global_hub.py:57）— `logging_config.get_logger(__name__)`。

类 `GlobalHub`（src/oc_slimapi/sse/global_hub.py:63）：
- `__init__`（:66）— 构造：client/订阅参数/traffic_ledger/allowlist/replay_log/turn_registry 注入位 + 全部可变状态容器初始化（见「状态/可变性」）。
- `_bump_updated_at`（:168）— `entry.updated_at = max(now, max(entry_prev, session_prev)+1)`，按 sid 跨防抖窗保证严格单调（LRU `move_to_end`）。
- `ensure_upstream`（:194）— 幂等启动 run/flush/heartbeat 任务组；取消已武装的 grace-stop；`task.done()` 时经 `_spawn_group` 重建。
- `_spawn_group`（:219）— INV-1 原子任务组创建：先取消残存兄弟任务，再以局部变量建 run/flush/heartbeat 三个 task 并挂 done_callback，最后赋给 `self.*`。
- `_make_group_done_callback`（:260）— 组成员 supervisor：闭包持有本组 task 引用；cancelled→no-op；正常退出（仅 run）→取消兄弟；异常死亡→取消兄弟并（若有消费者）强制 `_spawn_group` 重建；staleness 守卫 `self.task is run_task`。
- `subscribe`（:329）— 准入一个 `Subscriber`；`welcome=True` 先投连接局部 `server.connected` 帧（v4 路由传 False 抑制）；随后 `ensure_upstream()`。
- `unsubscribe`（:349）— 移除订阅者；最后一个离开且无 stop_task 时武装 `stop_after_grace`（生产 detach 走 `HubRegistry.unsubscribe`）。
- `has_consumers`（:362）— `bool(self.subscribers) or (_token_hub.subscriber_count > 0)`：控制面 + token 两个账本合并判活（刻意为方法非属性）。
- `_notify_upstream_loss`（:383）— 上游失联规范化钩子：`resync_all()` + replay `write_barrier()`（best-effort）+ `token_hub.on_upstream_reconnect()`；per-epoch 只应触发一次（守卫在 run() 侧）。
- `stop_after_grace`（:419）— 30s 宽限后若仍无消费者则 cancel 三个任务。
- `flush_loop`（:427）— 每 `DEBOUNCE_SECONDS`(0.25s) 调 `flush()`。
- `flush_sid`（:432）— 只冲刷单个 sid 的 pending digest（G1-A `session.error` 与 busy-清-sticky 立即路径）；sticky 合并 + `changed=[sid]` + `_emit_directory_frame`。
- `flush`（:452）— 批量冲刷：先机会式 prune 四张表，再 `snapshot, self.pending = self.pending, {}` 逐 sid 合并 sticky、置 `changed`、发 `session.digest`。
- `heartbeat_loop`（:481）— 每 10s 向所有订阅者投 `server.heartbeat` 并累计 `emitted_frames_total`。
- `set_token_hub`（:490）— 注入 TokenStreamHub（publish 路由 part/delta 依赖）。
- `set_directory_observer`（:500）— 注入可选同步 directory 观察者（B1b shadow scheduler）。
- `_observe_directory`（:506）— 非空 str directory 时调观察者；异常仅 debug log，绝不影响 ingest。
- `set_turn_registry`（:516）— 注入 TurnRegistry（digest 的 `turnIncarnation`/`turn` 快照盖章）。
- `set_directory_allowlist`（:528）— 设置进程级 directory 过滤并 `clear_allowlist_roots_cache()`（config 变更信号，重解析根）。
- `set_replay_log`（:536）— 注入 ReplayLog（B3b-2 v4 replay；None = v3-only 栈）。
- `_replay_publish`（:547）— 向 GLOBAL domain append 一帧，返回 `id:` 行；append 失败降级为 None（id-less 扇出 + warning）。
- `_directory_allowed`（:572）— allowlist 为空→True；否则委托 `config.directory_allowed`（相对路径/非 str fail-closed）。
- `_emit_directory_frame`（:584）— allowlist 闸门（不过→`allowlist_dropped_events++` 丢弃）；过→replay 记录；wire_v4 订阅者收 `id_line+frame`，v3 收原字节；`emitted_frames_total += len(subscribers)`。
- `_prune_retired_messages`（:601）— retired-message gate 的 TTL(24h)+FIFO(1000) 修剪，与 token hub 语义对齐。
- `_prune_last_updated_at`（:629）— `_last_updated_at_by_sid` LRU 上限 10k。
- `_prune_sticky_last_error`（:644）— sticky lastError FIFO 上限 10k。
- `_prune_deleted_tombstones`（:651）— deleted tombstone FIFO 上限 10k。
- `publish`（:658）— 上游帧总分类器（详见下）。
- `resync_all`（:977）— 清 tombstone / retired gate / updated_at 表；向全部订阅者投 `resync{reconnect_no_replay}`。
- `run`（:993）— 上游连接主循环：指数退避(1→30s)重连；连接成功且 ever_connected→重连计数+（未通知过则）loss 通知；aiter_lines 组帧（`data:` 前缀 + 空行分隔）→ `orjson.loads` → `publish`；EOF 与异常路径等价处理（通知+退避）。

`publish()` 内部分类（全集）：
1. `IMMEDIATE`（question.asked/question.v2.asked/permission.asked/permission.resolved/permission.v2.asked/permission.v2.resolved，src/oc_slimapi/sse/hub_types.py:73）→ 原样直推帧（含 `qp_last_activity[directory]` 记录）（:671-685）。
2. `SESSION_EVENTS`（session.status/session.updated/session.deleted）（:688-782）：
   - `session.status`：`normalize_session_status` 归一（:702）→ 填 `entry.status`；TurnRegistry 快照 ingest 时盖章（:713）；busy 且 sticky 存在 → pop sticky + `entry.last_error=None` + `flush_sid` 立即清帧（:720-723）。
   - `session.deleted`：`entry.deleted=True`、写 tombstone、pop sticky、`last_error=_UNSET`、清该 sid 的 retired gate 与 updated_at 高水位（:724-743）。
   - `session.updated`：仅透传 `info.time.archived`（int 非 bool，含 0；缺失不清）（:744-762）。
   - token hub 镜像分支（session.status/deleted → `on_session_status`/`on_session_deleted`）（:768-781）。
3. `MESSAGE_EVENTS`（message.updated/message.appended）（:784-800）：提取 sid + messageID → `_bump_updated_at`（updatedAt=sidecar 墙钟，非上游时间戳）。
4. `session.error`（:806-858）：name 非 str 强转 None；`ABORT_NAME` 静默丢弃；`_sanitize_error_message` 脱敏；有 sid（仅取 `props.sessionID`）→ tombstone/同窗 deleted 守卫 → 写 entry+sticky → `flush_sid` 立即；无 sid → 直接 `session.error` 帧。
5. token 族（message.part.delta / message.part.updated / message.part.removed / message.removed）（:882-973）：part.updated 校验 `part.{sessionID,messageID,id}` → retired gate 拦截 → `token_hub.on_part_updated`；part.removed（flat `{sessionID,messageID,partID}`）→ `on_part_removed`；message.removed（flat）→ 写 retired gate + `on_message_removed`；delta / 畸形 part → `on_part_delta` / `on_part_updated` 兜底。
6. 其余一切（text delta、tool.*、未知类型）→ 静默丢弃（:975 注释，无计数）。

#### 依赖 / 被依赖
- 依赖（import）：`..config`（TOKEN_REMOVED_MESSAGES_MAX/TTL、clear_allowlist_roots_cache、directory_allowed，src/oc_slimapi/config.py:75-76,255,341）；`..logging_config.get_logger`；`.hub_types`（21 个符号）；`.replay_log.GLOBAL_DOMAIN`；`.replay_wire.sse_id_line`；TYPE_CHECKING：`..traffic.TrafficLedger`、`..turn_registry.TurnRegistry`、`.replay_log.ReplayLog`、`.token_hub.TokenStreamHub`（注意：**经 shim** `.token_hub` 而非直连 `.tokenstream`，src/oc_slimapi/sse/global_hub.py:55）；运行时三方：httpx、orjson、asyncio。
- 被依赖（生产）：`sse/registry.py:13`（HubRegistry 持有唯一 GlobalHub）；`sse/hub.py:17`（shim re-export）；`app.py:490-491,505,511,588`（set_replay_log / set_directory_allowlist / qp_last_activity / set_directory_observer / set_turn_registry）；`routes/health.py:100`（读 allowlist_dropped_events）；`sse/registry.py:347-349`（读三个计数器）。
- 被依赖（测试）：test_batch3_lifecycle / test_sse_replay_wire / test_b4_allowlist / test_turn_registry / test_b1b_sweep_shadow / test_message_fingerprint / test_hub（monkeypatch `global_hub._now_ms`、`GRACE_SECONDS`、`asyncio.sleep`）/ test_events_tokens / test_globalhub_retired_gate / test_session_status_object_format 等。

#### 状态 / 可变性
- 任务组：`task`/`flush_task`/`heartbeat_task`（run/flush/heartbeat）+ `stop_task`（grace 定时器）；INV-1 原子组 + supervisor 自愈；**全程无锁**（单事件循环内联假设，`Subscriber.put` 注释 src/oc_slimapi/sse/hub_types.py:298 亦确认）。
- sid 表：`pending: dict[str, DigestFields]`（防抖窗累积，flush 时整体换出）；`_last_updated_at_by_sid`（LRU 10k）；`sticky_last_error`（FIFO 10k）；`deleted_tombstones`（FIFO 10k，resync_all 清空）；`_retired_messages`（(sid,mid)→ts，TTL 24h + FIFO 1000，session.deleted / resync_all 清）。
- `qp_last_activity: dict[str, float]`（:109,:678）— **唯一无上限的 map**（键=出现过 q/p 事件的 directory；QpSweepShadow 只读写从不删键，src/oc_slimapi/qp_sweep.py:55-183）。
- 计数器：`upstream_events_total` / `emitted_frames_total` / `reconnects_total` / `allowlist_dropped_events`（:146-149）；`ever_connected`、`_upstream_loss_notified`（per-epoch 失联守卫）。
- 注入位（可后置替换）：`_token_hub` / `_turn_registry` / `_replay` / `_directory_observer` / `directory_allowlist` / `_traffic_ledger`。

#### 错误路径
- run()：`raise_for_status` 失败 / 流异常 → except Exception → （首失联时）`_notify_upstream_loss` + warning + 退避重连（:1075-1090）；EOF（正常结束）等价处理（:1056-1072）；client=None 防御性 sleep 退避防热循环（:997-1001）。
- 帧解码：`orjson.JSONDecodeError` → debug log 丢帧（:1053-1054）。
- publish 内 session.error name 强转 str 防 TypeError 逃逸（:810-813）。
- replay：append 失败 → warning + id-less 降级（:567-569）；barrier 写失败 → warning 降级（:411-415）。
- 观察者 / 流量账本失败 → warning/debug，不影响主路径（:512-514,:1046-1047）。
- 下游背压：`Subscriber.put` 溢出 → 强制断连（v3: resync+STOP；v4: 仅 STOP）。

#### 疑问点（宁多勿漏）
1. **死 import**：`import logging`（:11）全文件未使用（logger 来自 logging_config）。
2. **publish 异常逃逸面**：`orjson.loads` 产出非 dict JSON（如 `[1,2]`）时 `global_event.get` AttributeError 逃出 publish → 被 run() except 捕获 → **整条连接按上游失联处理**（重连 + resync 扇出）；JSONDecodeError 有守卫（:1053）但非 dict 形状无守卫（:1052 vs :662-663）。毒帧可造成反复重连噪音。
3. **未知类型零观测**：catch-all 丢弃（:975）无 per-type 计数 / 日志；上游帧分类全集是否与 opencode v1.18.18 GlobalBus 实际发出的类型集对齐（instance.idle / authorization.updated / integration.* / config.* 等是否需要透传或至少计数）无法从本文件验证 —— 需对照 `opencode-src/current` 源码（AGENTS.md 表列路径）。
4. **allowlist 语义**：空 list 与 None 等价=禁用（:574-575）；被拒帧静默丢（仅计数 :586），**不进 replay**（`_replay_publish` 在闸门之后，:592）—— 符合"从未发布"语义，但 v4 客户端对被拒 sid 的 Last-Event-ID 补帧会直接跳过该帧，客户端无从感知过滤发生。
5. **`emitted_frames_total` 语义偏差**：按 `len(self.subscribers)` 累加（:488,:598-599），忽略 `Subscriber.put` 的 bool 返回（v6 §3.5 已提供）——closed/溢出丢弃的帧也被计入"emitted"。
6. **IMMEDIATE 帧含 `"directory": None`**（:679-683）：directory 缺失时 q/p 帧仍带 null 键；与 digest 的省略式（`to_payload` 仅非 None 才带键，src/oc_slimapi/sse/hub_types.py:183-184）不一致 —— 是否契约冻结形状需对照 v3-contract §7。
7. **q/p 丢失窗口**：(a) hub 任务组退出期间（无消费者）上游事件完全不被 ingest，永久丢失，靠重连路径 `resync_all` 通知客户端冷同步；(b) 上游断连到重连之间的事件丢失，v4 靠 ReplayLog 补（GLOBAL domain 记录含 IMMEDIATE 帧，:90-93 注释），但 v3 无补；(c) SSE 组帧要求空行终结（:1050），**流 EOF 时无空行收尾的最后一个 data 块丢失**（`data_lines` 残留不 publish）。
8. **read=None 无读超时**（:1002）：上游半开连接（TCP 未死但无数据）不会被检测，恢复仅靠 EOF/异常；heartbeat 是下游方向，掩盖不了上游僵死。
9. **sticky 表 FIFO 逐出的隐性语义**（:648-649）：sticky 被逐出后 digest 不再合并 lastError（wire 效果=字段消失），与显式 `lastError: null` 清除（:722）在客户端可区分吗？契约是否区分"省略"与"null"？
10. **G1 busy 清 sticky 的精确条件**（:720-723）：仅 `normalized == "busy"` 精确匹配（大小写敏感）；`normalize_session_status` 不做枚举校验（任意 str 直通 digest.status，src/oc_slimapi/sse/hub_types.py:398-399），所以 `"Busy"` 会进 digest 但不触发清除——上游枚举域需对照 opencode 源码确认。
11. **busy-clear 后同窗孤儿对象**：`flush_sid` pop 掉 pending entry 后，同一次 publish 调用内后续分支不再写 `entry`（当前安全，:723 后直接进 mirror 分支）；但 `session.error` 路径 :846 同理——若未来在 flush_sid 之后追加对 `entry` 的写即成孤儿写（写进已发出的对象），结构性脆弱点。
12. **session.updated 不 bump updatedAt**（:744-762 只透传 archived）：纯 session.updated 窗口产出的 digest 无 `updatedAt` 字段；客户端 `(updatedAt, messageID)` 排序对无 updatedAt 帧的行为需对照契约 §5。
13. **session.error 的 sid 仅取 `props.sessionID`**（:804-805,:823）：若上游把 sid 放 info 内（其他 session.* 支持 info.id 兜底，src/oc_slimapi/sse/hub_types.py:349-373），会误走 G1-B 无 sid 全局帧路径——上游 session.error 实际形状需对照 schema。
14. **token mirror 分支读外层变量 `status`**（:778）：仅在 `event_type=="session.status"` 分支定义（:702）；当前控制流保证已定义，但重排即 NameError（无局部静态保障）。
15. **`_retired_messages` 逐出不对称**：gate FIFO 1000 逐出后，迟到的 part.updated 可为已删消息重建 token-hub 状态（与 token hub 自身 `_retired_messages` 语义对齐，:606-618 承认此权衡）；跨 24h TTL 的迟到帧同理。
16. **tombstone FIFO 10k 逐出后**（:655-656），极高 churn 下迟到 session.error 可复活已删会话的 sticky（P1-21 已知权衡，注释 :138-143 仅论证 resync 清空路径）。
17. **stop_after_grace 不清 task 引用**（:419-425）：cancel 后 `self.task` 仍指 done task；`ensure_upstream` 靠 `task.done()` 判断可重建（正确），但 `self.stop_task` 触发后残留 done 引用使 `unsubscribe` 的 `not self.stop_task` 守卫不再武装第二次 grace（需订阅→退订→再退订序列才复现，边缘）。
18. **heartbeat 也计入 emitted_frames_total**（:487-488）：控制帧与业务帧混在同一计数器，metrics 语义需对照契约 §6。
19. **`qp_last_activity` 无 prune**（见上）；键空间=上游可控的 directory 字符串（任意非空 str 即入键，:672-678），恶意/异常上游可撑大该 dict。
20. `_observe_directory` 双重观察（publish 入口 :663 + flush/flush_sid :449,:478）——幂等无害但重复调用。

---

### src/oc_slimapi/sse/hub_types.py（419 行）

#### 职责
SSE hub 基础层：哨兵（STOP/_UNSET）、错误脱敏、T3 默认值、事件分类集合（IMMEDIATE/SESSION_EVENTS/MESSAGE_EVENTS）、时序常量、帧构造助手、`DigestFields`（防抖窗聚合）、`Subscriber`（T3 带守卫出站队列）、sid 提取、status 归一化、`SubscriberCapacityError`。叶子模块（hub→registry 单向，无反向依赖）；被 global_hub / registry / tokenstream 共享以避免实现重复。

#### 对外符号（完整）

模块级常量 / 哨兵：
- `STOP = object()`（src/oc_slimapi/sse/hub_types.py:30）— 控制面 SSE 生成器终结哨兵（注意与 tokenstream 自有 STOP 是**两个不同对象**，src/oc_slimapi/sse/tokenstream/frames.py:19）。
- `_UNSET = object()`（:32）— `DigestFields.last_error` 三态哨兵（_UNSET=省略 / None=显式清 / dict=对象）。
- `ABORT_NAME = "MessageAbortedError"`（:33）— 静默丢弃的 abort 错误名。
- `_UNIX_PATH_RE`（:35）/`_WIN_PATH_RE`（:36）— 绝对路径脱敏正则。
- `_STACK_FRAME_RE`（:37）— Python 风格栈帧剥离正则。
- `_SECRET_RE`（:41-44）— 键值式 secret 脱敏正则（access_token/token/key/bearer/password/authorization 等）。
- `DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY = 8`（:66）/ `DEFAULT_MAX_TOTAL_SUBSCRIBERS = 16`（:67）— T3 准入默认上限（生产由 Settings 覆盖）。
- `DEFAULT_SSE_QUEUE_ITEMS = 256`（:68）/ `DEFAULT_SSE_BUFFER_BYTES = 2MiB`（:69）/ `DEFAULT_SSE_MAX_FRAME_BYTES = 256KiB`（:70）— Subscriber 队列三默认。
- `IMMEDIATE`（:73-77）— 免防抖直推的 q/p 六类型 frozenset。
- `SESSION_EVENTS`（:80-82）— session.status/updated/deleted。
- `MESSAGE_EVENTS`（:88-90）— message.updated/appended（appended 仅为 wire 兼容保留）。
- `DEBOUNCE_SECONDS = 0.25`（:92）/ `HEARTBEAT_SECONDS = 10.0`（:93）/ `GRACE_SECONDS = 30.0`（:94）。
- `TOKEN_FRAME_TYPE = "token"`（:102）— L2-A 策划流 token 帧类型（仅 tokenstream 消费，放在本模块是共享叶子化选择）。

函数：
- `_sanitize_error_message(message, fallback_name)`（:47-61）— G1 脱敏：首行→Win 路径→Unix 路径→栈帧→secret→截 512；空/非 str 回退 name 或 "(no detail)"。
- `sse_frame(payload, event=None)`（:105-107）— 组 `event:`+`data: <json>\n\n` 帧字节。
- `_now_ms()`（:110-111）— epoch 毫秒。
- `_upstream_line_bytes(line)`（:114-133）— 流量计量：行字节数+1（补被 strip 的 LF）；空行=1；CRLF 会每行少计 1 字节（文档明示保守偏差）。
- `_extract_session_id(payload, props)`（:349-373）— sid 解析序：`props.sessionID`→`props.info.sessionID`→（仅 session.*）`props.info.id`；**刻意不回退 `payload.id`**（那是事件 id，误用会挂错 digest/sticky）。
- `normalize_session_status(value)`（:376-404）— str→原样；dict 且 `type` 为 str→取 type；其余（dict 无 str type / 非 dict 非 str）→None（该事件的 status 被忽略）。global_hub（digest 填充/G1 清除/token 镜像）与 tokenstream/hub.py:1048 共用同一实现。

数据类：
- `DigestFields`（:136-210）— 防抖窗内每 sid 聚合态。字段：`directory`(:154)、`status`(:155)、`message_id`(:156)、`updated_at: Any`(:157)（实际恒 int，见疑问 6）、`archived: int|None`(:158)、`deleted`(:159)、`last_error: Any=_UNSET`(:160)、`turn_incarnation`(:170)/`turn`(:171)（成对出现或成对省略）、`changed: list[str]|None`(:179)。方法：`to_payload(session_id)`（:181-210）— 非None条件拼装 digest payload；turn 两字段平铺在根级（ocdroid 平根解析约束，:199-205）；`changed` 同其他可选字段条件包含。
- `Subscriber`（:213-346，`eq=False`）— 单客户端出站队列 + T3 三守卫。字段：`queue_items/buffer_bytes/max_frame_bytes`(:235-237)、`id`(:240,"sub_"+hex4)、`queued_bytes`(:241)、`closed`(:242)、`dropped_frames`(:243)、`forced_disconnects`(:244)、`wire_v4`(:255,由 /events 路由在 subscribe 返回后立即置位)、`queue`(:258,post_init 建 maxsize 队列)。方法：
  - `__post_init__`（:260）— 建 asyncio.Queue。
  - `put(frame)`（:264-325）— 三守卫序：closed 静默丢；STOP 哨兵尽力入队（满→False）；超 max_frame_bytes→dropped++ 丢；队满或字节超限→**立即断连**（closed=True、forced_disconnects++、清队、v3 投 resync{subscriber_backpressure}+STOP / v4 仅 STOP，:316-320）；成功入队记账返回 True。
  - `ack(frame)`（:327-338）— 消费侧对称减 `queued_bytes`，STOP 不记账，floor 0。
  - `_clear_queue`（:340-346）— 清空队列并归零字节账。
- `SubscriberCapacityError(Exception)`（:407-419）— T3 准入超限异常；`__init__(code, *, limit, current)`（:415）；code ∈ {sse_subscriber_limit_directory, sse_subscriber_limit_total}（由 registry 抛出，不在本四文件内）。

#### 依赖 / 被依赖
- 依赖：`oc_slimapi.logging_config.get_logger`（:23，`logger` 定义 :27 但**本文件无直接使用**）；`.replay_wire.V4_RESYNC_REASONS`（:25，用于 put 的 v4 分支 :317）；三方 asyncio/dataclasses/contextlib/re/secrets/time/orjson。
- 被依赖（生产）：`sse/global_hub.py:26`（21 符号）、`sse/hub.py:18`（24 符号 shim）、`sse/registry.py:14`（10 符号）、`sse/tokenstream/hub.py:84`（TOKEN_FRAME_TYPE、normalize_session_status）。
- 被依赖（测试）：test_batch3_lifecycle:19（Subscriber）、test_b4_allowlist:20、test_turn_registry:36（DigestFields）、test_sse_replay_wire:1543（注释引用）。

#### 状态 / 可变性
- 模块级全部为不可变常量/哨兵/正则（编译期）+ 两个 object 哨兵；无模块级可变状态。
- `DigestFields` 可变 dataclass（全局 hub 在 publish/flush 中反复改写）；`Subscriber` 可变（队列 + 计数 + closed/wire_v4 标志）；`wire_v4` 设计为 subscribe() 返回后、无 await 间隙由路由置位（:250-255）以杜绝竞态。
- 无锁（单 loop 内联假设）。

#### 错误路径
- `Subscriber.put` 的全部失败出口：closed 丢 / STOP 入队满 / 超大帧丢 / 溢出强制断连（v4 STOP-only 降级基于 `reason not in V4_RESYNC_REASONS`，:317）。
- `_sanitize_error_message` 对 None/非 str 输入全兜底；脱敏链每步防御性（无正则异常面，模式预编译）。
- `_extract_session_id` / `normalize_session_status` 全 isinstance 守卫，无抛出面。

#### 疑问点
1. **两个 STOP 哨兵**：`hub_types.STOP`（:30）与 `tokenstream/frames.py:19 STOP` 是不同 object；`sse/token_hub.py` shim re-export 的是 tokenstream 的 STOP。events.py（:7 从 hub shim 导入）与 token_stream 路由各自比较自己的哨兵——跨流误比较会永不命中，需在 E2 路由层复核。
2. **`_sanitize_error_message` 覆盖面**：(a) JS 风格栈帧 `at foo (file:1:2)` 不被 `_STACK_FRAME_RE`（仅 `at \S+?:\d+`，:37）剥离（`\S+` 无法跨空格括号）——上游若是 JS 错误消息则路径/file:line 残留（路径部分会被 <path> 替换，但函数名+行号形状残留）；(b) `_SECRET_RE` 值字符类 `[A-Za-z0-9._\-/=+]+`（:43）不含 `~ : ; , !` 等，含这些字符的 secret 值只部分脱敏；(c) 截断 `[:512]`（:60）可能把 `<redacted>` 字面量切成 `<reda` 尾巴。
3. **过度脱敏**：`_UNIX_PATH_RE`（:35）会把消息里任何 `a/b.c` 形状的相对路径片段（如 "src/foo.py"）也替换为 `<path>`——审计消息可读性受损是否为接受的权衡（impl-spec §7 硬约束 4 只说 strip abs paths）。
4. **`normalize_session_status` 无枚举校验**（:398-399 任意 str 直通）：digest.status 的值域=上游值域未冻结；`"busy"` 语义清 sticky 依赖精确匹配（global_hub:720）。对象信封只读 `type` 键，信封内其他字段（若有 reason 等）被丢弃——上游 2026-08-19 实测形状之外的第三种形状（如 `{"status":"busy"}`）会归一为 None 被整体忽略。
5. **`IMMEDIATE` 六类型是否完整**（:73-77）：question.v2 / permission.v2 双轨并存说明上游在迁移期；若上游再加 v3 后缀类型，本表不感知即静默丢弃（与 global_hub 疑问 3 同源，无漂移检测）。
6. **`DigestFields.updated_at: Any`（:157）**：实际只被赋 int（`_bump_updated_at`）或保持 None；类型标 Any 过宽，`to_payload` 的 `updatedAt` 无类型保障。
7. **`queue: asyncio.Queue = field(default=None)`（:258）**：注解非 Optional 却默认 None（post-init 惰性建），类型不严谨。
8. **`put` 的 QueueFull 竞争注释（:297-300）**：先查 `qsize()<queue_items` 再 put_nowait，满时落入溢出路径=立即断连——若真有并发生产者（注释称实际没有），一次偶发满员即断连过激。
9. **v4 STOP-only 路径 STOP 丢失**（:318-319）：断连分支里 queue 已满时 `put_nowait(STOP)` 被 suppress → 订阅者 closed 但 SSE 生成器收不到 STOP，连接是否靠生成器侧 closed/断线检测收尾需在 routes/events.py 复核（E2 范围）。
10. **`ack` 不配对风险**（:327-338）：只减不增、floor 0；若生成器取帧后不 ack（如异常路径），`queued_bytes` 虚高导致提前触发背压断连——生成器是否全路径 ack 需 E2 复核。
11. **`_extract_session_id` 的 `info.id` 兜底仅限 `session.*`**（:366-372）：message.* 事件若上游只带 `info.id`（消息 id）而无 sessionID，会返回 None → 整事件丢弃（global_hub:786-787）——上游 message.updated 是否恒带 sessionID 需对照 message-v2.ts。
12. **模块内 `logger` 死变量**（:27）：定义后本文件无任何调用。
13. `TOKEN_FRAME_TYPE`（:102）与 hub 控制面无关，放在 hub_types 仅因叶子共享；归属略错位（风格问题）。
14. `Subscriber` 无显式 `close()`/终结协议：closed 置位后队列残留（溢出路径已清，但 STOP 正常终结路径不清）——`queued_bytes` 残值无回收方。

---

### src/oc_slimapi/sse/hub.py（42 行）

#### 职责
**纯 re-export 兼容 shim**（非实现）：原单体 hub 拆分为 hub_types / global_hub / registry 三模块后，本模块保持 `from oc_slimapi.sse.hub import X` 旧导入路径不变。自身零逻辑（仅 import 三方 26 个符号）。

#### 对外符号（完整）
- 来自 `.global_hub`（:17）：`GlobalHub`、`_LAST_UPDATED_AT_BY_SID_MAX`（下划线私有符号被 re-export）。
- 来自 `.hub_types`（:18-41）：`ABORT_NAME`、`DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY`、`DEFAULT_MAX_TOTAL_SUBSCRIBERS`、`DEFAULT_SSE_BUFFER_BYTES`、`DEFAULT_SSE_MAX_FRAME_BYTES`、`DEFAULT_SSE_QUEUE_ITEMS`、`DEBOUNCE_SECONDS`、`DigestFields`、`GRACE_SECONDS`、`HEARTBEAT_SECONDS`、`IMMEDIATE`、`MESSAGE_EVENTS`、`SESSION_EVENTS`、`STOP`、`Subscriber`、`SubscriberCapacityError`、`_UNSET`、`_extract_session_id`、`_now_ms`、`_sanitize_error_message`、`_upstream_line_bytes`、`sse_frame`（22 个）。
- 来自 `.registry`（:42）：`HubRegistry`。

#### 依赖 / 被依赖
- 依赖：global_hub / hub_types / registry 三实现模块。
- 被依赖（生产，rg 实证）：`routes/events.py:7`（STOP、SubscriberCapacityError、sse_frame）；`app.py:33`（HubRegistry）。即 **shim 有真实生产使用者，非死代码**。
- 被依赖（测试，大量）：test_hub.py:28（及 954-1007 的 `_sanitize_error_message`、1428/1520 的 `DigestFields`）、test_token_hub_lifecycle:24、test_dbaux_metrics:18、test_token_stream_route:40、test_metrics_replay_block:30、test_globalhub_retired_gate:20、test_token_hub:31/669/683/694、test_traffic_integration:50、test_turn_registry:35、test_b1a_digest_changed:34、test_messages_routes:34、test_sse_replay_wire:58、test_upstream_error_boundary:38、test_command_routes:21、test_access_log_v3_fields:291、test_traffic_sse:29、test_agent_routes:28、test_hub_behavior_lock:55、test_events_tokens:43、test_v3_sse_meta:43、test_sse_logging:16、test_traffic_upin_gaps:35、test_metrics:23、test_sessions_coalesce:32、test_etag:41、test_messages_coalesce:41、test_session_status_object_format:33 等（≈25 个测试文件仍走 shim）。

#### 状态 / 可变性
- 无状态（纯转发；`from __future__ import annotations` + 三组 import）。

#### 错误路径
- 无自身错误路径；符号缺失会在 import 期 AttributeError（re-export 名单与实现模块 drift 时测试即崩，属可接受的显性失败）。

#### 疑问点
1. **结论：纯 shim，且有生产使用者**（routes/events.py、app.py）——不可删；但生产侧仅用 4 个符号（STOP/SubscriberCapacityError/sse_frame/HubRegistry），其余 22 个 re-export 主要服务测试兼容。
2. `_LAST_UPDATED_AT_BY_SID_MAX` 被 re-export（:17）但 rg 显示**无任何外部代码经 shim 使用它**（tests 直接从 global_hub 导入，test_batch3_lifecycle:1138）——疑似多余行。
3. 私有下划线符号（`_UNSET`/`_now_ms`/`_sanitize_error_message`/`_upstream_line_bytes`/`_extract_session_id`/`_LAST_UPDATED_AT_BY_SID_MAX`）经 shim 固化为事实公共 API（测试大量依赖），未来收窄面困难。
4. `sse/tokenstream/frames.py:25-36` 为避 import 环路**复制**了 `sse_frame`/`_now_ms` 而非复用 hub_types——本可 import hub_types（hub_types 是叶子、无环），复制理由（"hub.py re-export 会成环"）针对的是 hub.py 而非 hub_types.py，注释与实际可选方案有出入（需要 E 组其他卡片确认 frames.py 为何不复用 hub_types）。

---

### src/oc_slimapi/sse/token_hub.py（23 行）

#### 职责
**纯 re-export 兼容 shim**：token 流实现已物理迁移至 `oc_slimapi/sse/tokenstream/` 包（hub.py/subscriber.py/frames.py），本模块保持 `from oc_slimapi.sse.token_hub import ...` 旧路径可用。自身零逻辑。

#### 对外符号（完整）
来自 `.tokenstream`（src/oc_slimapi/sse/token_hub.py:5-23，共 18 个）：`STOP`（tokenstream 自有哨兵，≠hub_types.STOP）、`DeltaAccumulator`、`LivePart`、`PartKey`、`TokenStreamHub`、`TokenStreamRegistry`、`TokenSubscriber`、`TokenSubscriberCapacityError`、`_TokenMetrics`、`_connected_frame`、`_delta_frame`、`_heartbeat_frame`、`_now_ms`、`_resync_frame`、`_snapshot_frame`、`_truncated_frame`、`sse_frame`（均为 `# noqa: F401` 转发）。

#### 依赖 / 被依赖
- 依赖：`.tokenstream` 包（真实实现，`tokenstream/__init__.py:7` 导出 TokenStreamHub）。
- 被依赖（生产，rg 实证）：`app.py:36`（TokenStreamHub、TokenStreamRegistry——lifespan 构造）；`routes/token_stream.py:61`（大量符号）；`sse/global_hub.py:55`（TYPE_CHECKING 下 `from .token_hub import TokenStreamHub`——**类型引用也走 shim**）。即 **shim 有真实生产使用者，非死代码**。
- 被依赖（测试）：test_token_hub_lifecycle:25（含 `_now_ms`）、test_token_hub_flush:49、test_v3_sse_meta:44（STOP as TOKEN_STOP、sse_frame——与 hub 侧 STOP 并排导入证实双哨兵）、test_token_stream_route:41、test_events_tokens:44、test_sse_replay_wire:77、test_token_hub:32。
- 对照：`sse/registry.py` 的 TYPE_CHECKING **直连** `from .tokenstream import TokenStreamHub`（不走 shim）——同一语义两条导入路径并存。

#### 状态 / 可变性
- 无状态（纯转发）。

#### 错误路径
- 无自身错误路径；实现符号 drift 在 import 期显性失败。

#### 疑问点
1. **结论：纯 shim，且有生产使用者**（app.py、routes/token_stream.py、global_hub.py 的类型引用）——不可删。
2. **双哨兵风险再确认**：本 shim 的 `STOP`/`sse_frame`/`_now_ms` 来自 tokenstream/frames.py 的**复制实现**（frames.py:19,:25-36），与 hub_types 同名符号是不同对象/不同函数对象——任何跨两套体系的比较或单例假设都是隐患（test_v3_sse_meta:43-44 同时导入两者佐证已知此分叉）。
3. **导入路径不一致**：global_hub 类型引用走 shim（:55），registry 类型引用直连 tokenstream——建议统一（风格/漂移面）。
4. 私有符号（`_TokenMetrics`、`_*_frame`、`_now_ms` 等 9 个下划线符号）经 shim 固化为测试可达 API。
5. shim 存在使 `token_hub`（旧名）与 `tokenstream`（新名）双名并存；若无退役计划，长期漂移风险=两套入口任一改名即断（可接受但应有 retire 时间表——未见文档）。

---

## 汇总备注（跨文件）
- 四文件零锁设计的前提=「publish/flush/heartbeat 全部内联同一事件循环」（hub_types.py:297-299 注释自证）；任何未来把 publish 移到线程/其他 loop 的改动都会引入数据竞争。
- 帧分类全集（IMMEDIATE 6 + SESSION 3 + MESSAGE 2 + session.error + token 4 = 16 类型 + catch-all 丢弃）是本 sidecar 策展边界的单一事实源；与 `docs/specs/v3-contract.md` §7 的一致性、以及与 opencode v1.18.18 GlobalBus 实际发射集的对齐，是本次审计应在校验阶段完成的外部对照项（本卡片仅记录代码事实）。

<!-- ==== e1-01-tokenstream-hub ==== -->
# E1 精读卡片 — src/oc_slimapi/sse/tokenstream/hub.py

- 文件：`src/oc_slimapi/sse/tokenstream/hub.py`（2190 行，全文精读，无抽样）
- 职责（一句）：**part 生命周期门控的 token 累积器 + 100ms flush 引擎**——把上游 `message.part.updated/delta/removed` 与 `session.status/deleted` 事件累积进 `LivePart`/`DeltaAccumulator` 双账本，按 TICK/字节阈值 flush 成 delta/snapshot/resync/heartbeat 帧 fanout 给 per-session 订阅者与 `/slimapi/events?tokens=1` tap，并执行 LIVE/PENDING 双内存预算、LRU 逐出、tombstone 回放与 v4 ReplayLog 发布。
- 对外符号数：顶层 9 个（1 类 + 8 模块级）；`TokenStreamHub` 类内 59 个方法/属性。
- 疑问点数：24（见文末）。

---

## 1. 对外符号（逐类逐方法完整清单）

### 模块级（8 + logger）

| 符号 | 行号 | 职责 |
|---|---|---|
| `logger` | :111 | 模块 logger（`get_logger(__name__)`），全文件仅 3 个日志点（:469 critical、:1392/:1422 warning） |
| `_SESSION_STATUS_MAX` | :116 | P1-21：`_session_status`/`_busy_sids` 的 FIFO cap（10_000） |
| `_TTL_TICK_INTERVAL` | :121 | flush tick ↔ 60s TTL sweep 换算（import 时计算，floor 1） |
| `_HEARTBEAT_TICK_INTERVAL` | :123 | flush tick ↔ 15s heartbeat 换算（import 时计算，floor 1） |
| `apply_debug_budget_overrides(settings)` | :126-161 | **Debug/联调 break-glass**：用 `OC_SLIMAPI_TOKEN_STREAM_DEBUG_*` env 覆盖模块级全局 `TOKEN_LIVEPARTS_MAX_BYTES`/`TOKEN_PART_MAX_BYTES`/`TOKEN_LIVE_PARTS_MAX`；app lifespan 启动时调用一次（app.py:307） |
| `_V4_INELIGIBLE_FRAME_PREFIX` | :171 | `b"event: message.part.snapshot\n"`——snapshot 族帧前缀（v4 wire 永不发送） |
| `_v4_frame_eligible(frame)` | :174-190 | rev-gate R2 BLOCKER-1：帧是否可进 v4 wire/ReplayLog（snapshot 族 → False） |
| `_events_token_frame(key, text)` | :193-211 | L2-A curated-events 精简 token 帧：`{type:"token", sessionID, messageID, partID, delta}`，无 `event:` 名、无 revision、无 directory |

### class TokenStreamHub（:214）

类 docstring（:215-246）枚举 9 个核心容器。构造参数（:248-253）：`max_frame_bytes=DEFAULT_TOKEN_MAX_FRAME_BYTES`（1 MiB）、`replay_log: ReplayLog | None = None`。

#### 属性 / 只读（7）

| 方法/属性 | 行号 | 职责 |
|---|---|---|
| `subscriber_count` (property) | :360-375 | 所有 sid 的 token 订阅者总数（`sum(len(subs))`）；`GlobalHub.has_consumers` 的存活判据之一 |
| `has_consumers()` | :377-395 | 统一存活谓词：`subscriber_count > 0 or len(events_tap) > 0`（events-token-only 也保活 flush loop） |
| `orphan_deltas` (property) | :397-400 | 累计孤儿 delta 计数（C3） |
| `flushed_frames_total` (property) | :402-404 | 已 fanout 帧计数 |
| `dropped_frames_total` (property) | :406-408 | 丢帧计数（**本文件从不递增**，只有 subscriber.py:405/421/441 递增共享 `_metrics`） |
| `truncated_snapshots_total` (property) | :410-412 | truncated 帧计数 |
| `token_memory_limit_total` (property) | :414-416 | `resync{token_memory_limit}` 次数 |

#### 后台 flush 生命周期（5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `start()` | :421-441 | 幂等启动 `flush_loop` task + 挂 `_on_flush_done` watchdog |
| `_on_flush_done(task)` | :443-476 | INV-1 watchdog：exception 死亡且有消费者 → CRITICAL 日志 + `start()` 重建；cancelled/normal/stale-task → no-op |
| `stop()` | :478-482 | 幂等 cancel flush task |
| `flush_loop()` | :484-517 | `while True: sleep(TOKEN_FLUSH_SECONDS) → flush()`；每 `_TTL_TICK_INTERVAL` tick 一次 `ttl_sweep`，每 `_HEARTBEAT_TICK_INTERVAL` tick 一次 `_fanout_heartbeat`；仅 CancelledError 重抛 |
| `flush()` | :519-579 | 排序 drain 全部 `_pending` → `_fanout_frame`（每帧消费独立 revision）+ events_tap 直推 + `_drain_pending_session_resyncs`；记 `flush_duration_ms_total`/`flush_ticks_total` |

#### flush / 握手辅助（1）

| 方法 | 行号 | 职责 |
|---|---|---|
| `flush_sid(sid)` | :581-614 | 仅 drain 一个 sid 的 pending（§5.5 握手第 3 步：老订阅者收残差、新订阅者不收，C2 防双发） |

#### Ingest（GlobalHub.publish 调用，5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `on_part_updated(props, part_revision=None)` | :619-715 | `message.part.updated`：text-start 创建 LivePart（与订阅者解耦，B1）/ text-end → `finish_part`；非 text 记 `_nontext_parts`；`part_revision` 参数被忽略（签名兼容）；deleted-sid / retired-message / malformed / disabled 门全部先于 revision 消费 |
| `on_part_delta(props)` | :739-829 | `message.part.delta`：field==text 门 + 五重 gate → `_reserve` 预算 → 同时 append 到 LivePart 与 `_pending` → 超 `TOKEN_FLUSH_BYTES`(4KiB) 立即早 flush → `_check_pending_budget` |
| `on_message_removed(sid, mid)` | :835-877 | `_retire_message`（原子清态+gate）→ live fanout（tombstone 进 ReplayLog）→ 记 `_removed_messages`（move_to_end 防 FIFO 逐出新鲜项，MAJOR 6） |
| `on_part_removed(sid, mid, pid)` | :879-907 | 幂等 `drop_part`（退役单 part；message-level 已退役则 no-op） |
| `on_session_status(sid, status)` | :1019-1070 | 归一化 busy/idle（兼容 string 与 `{"type":...}` 信封）；idle → `_retire_session` + **无条件写 replay barrier**（R4）+ `_enqueue_session_resync("session_idle")` |

#### 退役 / 清理（3）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_retire_message(sid, mid)` | :909-945 | 清 `(sid, mid, *)` 的 5 类结构 + 字节表 floor-0 + 写 `_retired_messages` gate |
| `_retire_session(sid)` | :1134-1165 | 清一个 sid 的 5 类 part 态结构（不动 `_session_status`/`_busy_sids`） |
| `ttl_sweep(now_ms=None)` | :1167-1206 | 60s tick：仅对 `_session_status==idle` 且超 `TOKEN_ACC_IDLE_MS`(60s) 的 LivePart 静默退役（busy-guard NB#4）+ prune `_removed_messages` |

#### 终态（1）

| 方法 | 行号 | 职责 |
|---|---|---|
| `finish_part(key, final_text)` | :950-1014 | 同步 drain 残差 pending → delta 帧（先）→ `snapshot{done:true}` 无 text 终态标记（后，Lever 1）→ `drop_part`；LivePart 已不存在则抑制标记 |

#### 会话删除 / 上游重连（2）

| 方法 | 行号 | 职责 |
|---|---|---|
| `on_session_deleted(sid)` | :1072-1132 | `_retire_session` + 清 status/busy + 写 barrier("session_deleted") + 清 `_retired_messages` 本 sid 项 + `_remember_deleted_sid` gate + **逐个 `sub.terminate("session_deleted")`**（INV-4，resync→STOP） |
| `on_upstream_reconnect()` | :2124-2190 | 全量清态（8 类）+ **保留 `_part_revisions`（防 ocdroid 严格 `>` 水位回退）与 `_removed_messages`**；对每个有订阅者的 sid fan `resync{reconnect_no_replay}` |

#### 订阅者 fanout 记账（3）

| 方法 | 行号 | 职责 |
|---|---|---|
| `attach_subscriber(sid, sub, wire_v4=False)` | :1211-1338 | §5.5 握手：v4 = 无 prefill 直入 fanout（`sub.wire_v4=True` + closed 检查）；v3 = `begin_handshake` 括号内 connected 帧 → tombstone 回放（TTL 过滤）→ `flush_sid` → 每 LivePart 快照/截断 → `end_handshake` 后 closed 再查 → 入 `_subs_by_sid` |
| `detach_subscriber(sid, sub)` | :1340-1353 | 幂等移出 fanout set；**不**退役 LivePart（B1 解耦） |
| `has_subscriber(sid, sub)` | :1355-1366 | 身份制成员检查（NB-D1，unsubscribe 防重复扣减） |

#### Fanout 辅助（10）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_replay_publish_token(sid, frame, kind)` | :1371-1394 | B3b-2 咽喉：帧 append 进 sid 的 token domain（published 语义，零订阅也记）→ 返回 `id:` 行；log 异常降级返回 None |
| `_write_replay_barrier(sid, why)` | :1396-1424 | R4：idle/evict/delete 三处**无条件**写 barrier，使 cursor≤watermark 的重连必走 `resync{reconnect_no_replay}`；异常吞掉 + warning |
| `_deliver_logged(sid, frame, id_line)` | :1426-1438 | v4 sub 收 `id_line+frame`，v3 收裸 frame；返回投递 sub 数 |
| `_deliver_v3_only(sid, frame)` | :1440-1459 | snapshot 族帧只投 v3 sub（v4 一律不收） |
| `_fanout_frame(key, frame)` | :1461-1481 | 帧分发总入口：eligible → 先 log 再 `_deliver_logged`；ineligible → `_deliver_v3_only`；计 `flushed_frames_total` |
| `_fanout_message_removed(sid, mid)` | :1483-1500 | tombstone live fanout（`FRAME_KIND_TOMBSTONE` 进 log，REPLAY-012） |
| `_fanout_resync(sid, reason)` | :1502-1527 | R3：v4 sub 遇非冻结 reason（`token_memory_limit`/`session_idle` 等）→ `sub.terminate(reason)`（只断不发帧）；v3 照发 `resync{reason}` |
| `_fanout_heartbeat()` | :1529-1536 | 15s 心跳到全部 token 订阅者（v3+v4 都收，不进 log） |
| `_emit_snapshot_or_truncated(sub, key, text, done)` | :1538-1595 | 单 sub 快照 + C6 帧上限检查；超限 → `_truncate_part_for_all`（fanout + drop）+ 非 fanout 内 sub 直投 truncated；v4 sub 只做探针（超限即 truncate，不投帧） |
| `_emit_snapshot_or_truncated_nodrop(sub, key, text, done)` | :1597-1648 | MB-P-S1：eviction 后对 **skip_key（当前 key）** 的重快照——超限只对**该 sub** 投 truncated、**绝不 drop_part**（保 O1：调用方持有的 `live` 引用不失效）；v4 直接 return |

#### 截断 / 内存预算（5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_truncate_part_for_all(key, done)` | :1650-1698 | C6 backstop：幂等（`_is_disabled` 先查）→ 消费 revision → `drop_part` → `_deliver_v3_only(truncated)`；返回捕获的 revision 供直投 |
| `_reserve(live, n, key)` | :1703-1749 | LIVE 预算：per-part `TOKEN_PART_MAX_BYTES`(1MiB) 超限 → truncate+False；全局 `TOKEN_LIVEPARTS_MAX_BYTES`(4MiB) 超限 → while 循环 LRU 逐出**其他** key（绝不逐当前 key） |
| `_evict_part_for_memory(key, skip_key=None)` | :1751-1821 | LRU 逐出：`drop_part` → `flush_sid`（I1 防双发）→ 写 barrier("token_memory_limit") → `resync{token_memory_limit}` → 对该 sid 剩余 LivePart 重快照（skip_key 走 nodrop 路径） |
| `_check_pending_budget(current_key)` | :1823-1862 | Stage E：`_total_pending_bytes > TOKEN_PENDING_MAX_BYTES`(4MiB) → 强制 `flush()`；无订阅者（**全局**计数）/仍超 → LRU 逐出最老 LivePart + resync |
| `_start_part(key, seed="")` | :1867-1917 | 建 LivePart；count cap（`TOKEN_LIVE_PARTS_MAX`=32）先逐出；seed 超 per-part cap → 立即 truncate；NB-C1：seed 入账后 while 逐出其他 key 直至 ≤ 全局字节 cap |

#### Part 生命周期 / tombstone 记账（14）

| 方法 | 行号 | 职责 |
|---|---|---|
| `drop_part(key)` | :1919-1946 | 幂等退役：pop pending/live（字节 floor-0）→ 清 revision → 首次调用记 `_disabled_parts` 返回 True，后续 False；**从未见过的 key 也合法并标记 disabled** |
| `_remember_disabled(key)` | :1951-1966 | 有界 tombstone（cap `TOKEN_DISABLED_MAX`=4096 + TTL；重记不刷新 TTL） |
| `_remember_nontext(key)` | :1968-1979 | 同上有界非 text part 记录 |
| `_discard_nontext(key)` | :1981-1984 | 删单条 nontext tombstone |
| `_is_disabled(key)` | :1986-1987 | disabled gate 查询 |
| `_is_nontext(key)` | :1989-1990 | nontext gate 查询 |
| `_prune_bounded(store, now_ms)` | :1992-2016 | TTL 前向扫描 + FIFO cap（O(cap)） |
| `_remember_deleted_sid(sid)` | :2018-2030 | P1-22 有界 deleted-sid gate（cap/TTL 对齐 removed-messages 常量） |
| `_is_deleted_sid(sid)` | :2032-2040 | gate 查询 + 惰性 TTL 过期 |
| `_prune_deleted_sids(now_ms)` | :2042-2049 | gate 的 FIFO cap + TTL |
| `_prune_session_status()` | :2051-2054 | `_session_status` FIFO cap（10k） |
| `_prune_busy_sids()` | :2056-2059 | `_busy_sids` FIFO cap（10k） |
| `_prune_removed_messages(now_ms)` | :2061-2087 | removed 队列 TTL+cap；**逐出项同步 discard `_retired_messages` gate**（生命周期耦合） |
| `_next_part_revision(key)` | :717-737 | revision 唯一递增点（-1 起步首帧 0）；move_to_end + LRU cap（`TOKEN_DISABLED_MAX`） |

#### Pending resync 队列（2）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_enqueue_session_resync(sid, reason)` | :2092-2103 | 有界 `(sid, reason)` 队列（cap 64，溢出丢最老，NB-B2） |
| `_drain_pending_session_resyncs()` | :2105-2118 | flush 内快照+清空后逐条 `_fanout_resync` |

（注：`_next_part_revision` 位于 ingest 区，为凑表格归入 tombstone/revision 组；行号为准。）

---

## 2. 依赖（内部 imports）

- `...config`（:67-82）：13 个 `TOKEN_*` 预算/节奏常量 + `DEFAULT_TOKEN_MAX_FRAME_BYTES`；其中 3 个（`TOKEN_LIVEPARTS_MAX_BYTES`/`TOKEN_PART_MAX_BYTES`/`TOKEN_LIVE_PARTS_MAX`）会被 `apply_debug_budget_overrides` 运行时改写（模块全局，测试 monkeypatch 同名）。
- `...logging_config.get_logger`（:83）。
- `..hub_types`（:84）：`TOKEN_FRAME_TYPE`、`normalize_session_status`。
- `..replay_log`（:85-90）：`ReplayLog`、`token_domain`、`FRAME_KIND_BUSINESS/TOMBSTONE`。
- `..replay_wire`（:91）：`V4_RESYNC_REASONS`（frozen 4 元素：epoch_changed/replay_expired/replay_gap/reconnect_no_replay，replay_wire.py:72-77）、`sse_id_line`。
- `.frames`（:92-104）：`STOP`、`sse_frame`、`_connected/_delta/_heartbeat/_message_removed/_now_ms/_resync/_snapshot/_truncated_frame`、`PartKey`。
- `.models`（:105）：`DeltaAccumulator`、`LivePart`、`_TokenMetrics`（models.py 定义，含 `dropped_frames_total` 等 9 计数器）。
- `TYPE_CHECKING`：`..hub.HubRegistry`（:107-108，仅类型）。
- 标准库：`asyncio`、`time`、`collections.OrderedDict`、`typing`。**无锁、无 executor、无线程**——全部状态假设单事件循环线程内同步访问（所有 ingest/flush 路径无 await 窗口）。

## 3. 被依赖（rg 反查 `tokenstream.hub` / `TokenStreamHub`，结论）

生产代码（7 处）：

| 使用方 | 位置 | 用法 |
|---|---|---|
| `sse/tokenstream/__init__.py` | :7 | re-export `TokenStreamHub` |
| `sse/token_hub.py` | :10-26 | 兼容 shim re-export（旧 import 路径） |
| `app.py` | :36-37, :307, :538-541, stack.callback | lifespan 构造唯一实例（`max_frame_bytes=settings.token_stream_max_frame_bytes`、`replay_log=app.state.replay_log`）、调 `apply_debug_budget_overrides(settings)`、shutdown 时 `stop()`（NB-C4 LIFO 在 hubs.close 前） |
| `sse/global_hub.py` | :55, :117, :490-491, :779/:781, :911/:935/:963/:970/:972, :417 | `set_token_hub` 注入；`publish()` 路由 session.status/deleted、part.updated/removed、message.removed、part.delta；`has_consumers` 读 `subscriber_count`；`_notify_upstream_loss` 调 `on_upstream_reconnect` |
| `sse/registry.py` | :31, :75, :94-95, :323 | `HubRegistry` 持 `_token_hub`；grace 移除路径调 `on_upstream_reconnect`（清理后再判断） |
| `sse/tokenstream/subscriber.py` | :603/:619（events_tap append/remove）、:606/:621/:701/:782/:832（start/stop）、:705（attach_subscriber）、:772/:821（has_subscriber）、:773/:823（detach_subscriber）、:681（`self.token_hub._metrics` 私有穿透共享计数器） | `TokenStreamRegistry` 是 hub 的 HTTP 层编排者；`TokenSubscriber` 被 hub 鸭子类型消费（`put`/`terminate`/`begin/end_handshake`/`closed`/`wire_v4`） |
| `routes/events.py` | :158-162 | `?tokens=1` → `token_registry.attach_events_subscriber(subscriber)`（间接进 `events_tap`）；`routes/token_stream.py:138` 经 `token_registry` 间接 |

测试（9 个文件，重度依赖）：`tests/test_token_hub.py`、`test_token_hub_flush.py`（大量 `monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_*")` 改模块全局）、`test_token_hub_lifecycle.py`、`test_token_stream_route.py`、`test_sse_replay_wire.py`（含 `tokenstream_hub_module` 直接 import + `_SESSION_STATUS_MAX`）、`test_events_tokens.py`、`test_batch3_lifecycle.py`、`test_session_status_object_format.py`、`test_token_subscriber_overflow.py`。

**结论**：hub 是 token 流域的唯一权威累积器，进程级单例（app.state.token_hub），上游入口 = `GlobalHub.publish`，下游出口 = `TokenStreamRegistry`/`TokenSubscriber` + events_tap；测试与模块全局 cap 的耦合是刻意的（`apply_debug_budget_overrides` docstring :139-141 说明兼容性）。

## 4. 状态 / 可变性（长生命周期对象逐项）

| 状态 | 行号 | 结构 / 预算 |
|---|---|---|
| `live_parts` | :254 | `dict[PartKey, LivePart]`——LIVE 权威文本；受 count cap 32 + 全局 4MiB + per-part 1MiB |
| `_replay` | :271 | 进程级 `ReplayLog`（只 append/barrier，异常全吞） |
| `_nontext_parts` | :273 | `OrderedDict[PartKey, int]`，cap 4096 + TTL |
| `_disabled_parts` | :274 | 同上 |
| `_pending` | :275 | `dict[PartKey, DeltaAccumulator]`——flush 前影子；全局 4MiB |
| `_total_live_bytes` | :276 | LIVE 字节表（floor-0 递减） |
| `_total_pending_bytes` | :277 | PENDING 字节表（与 LIVE 独立，同字节双计是设计 :50-54） |
| `_metrics` | :278 | `_TokenMetrics`（**与 subscriber.py 共享可变对象**，subscriber.py:681 私有穿透） |
| `_session_status` | :282 | `OrderedDict[str, str]`，cap 10k（P1-21） |
| `_busy_sids` | :283 | `OrderedDict[str, None]`，cap 10k；**生产无读者**（见疑问点 2） |
| `_pending_session_resinks` | :285 | `list[(sid, reason)]`，cap 64 丢最老 |
| `_subs_by_sid` | :288 | `dict[str, set[Any]]` 订阅者 fanout 账本 |
| `events_tap` | :298 | **公有可变 `list`**（registry append/remove bound `sub.put`） |
| `_max_frame_bytes` | :300 | 帧上限（来自 settings，INV-5） |
| `_flush_task` | :302 | 唯一后台 task（watchdog 重建） |
| `_part_revisions` | :325 | `OrderedDict[PartKey, int]` LRU cap 4096；reconnect 保留（:2170-2173） |
| `_removed_messages` | :335 | `OrderedDict[(sid, mid), int]` cap 1000 + 24h TTL；reconnect 保留 |
| `_retired_messages` | :345 | `set[(sid, mid)]`——与 removed 队列生命周期耦合（:2071-2087），但 reconnect wholesale 清空（:2180） |
| `_deleted_sids` | :355 | `OrderedDict[str, int]` cap 1000 + 24h TTL（对齐 removed 常量） |

锁：无（单事件循环假设）。executor：无。queue：订阅者队列在 subscriber.py 侧（T3 背压），hub 侧仅 `_pending_session_resinks` 简单 list。

## 5. 错误路径

- **异常抛出**：本文件**不主动抛**任何异常；ingest 全部静默早退（malformed props → return，:665-671、:766-776 等）。
- **异常捕获/降级**（仅 3 处，全在 ReplayLog 周边）：
  - `_replay_publish_token` :1389-1394 `except Exception` → `logger.warning("replay log append failed for sid %r")` → 返回 None（帧照发、无 id 行）。
  - `_write_replay_barrier` :1419-1424 `except Exception` → `logger.warning("replay barrier write failed ...")` → 吞掉。
  - `flush_loop` :516-517 仅重抛 `CancelledError`；**其他异常杀掉 task** → `_on_flush_done` :469-476 `logger.critical("token flush_loop died unexpectedly; ...")` + `has_consumers()` 时重建。
- **错误码/resync reason 产出**：`token_memory_limit`（:1802，v4 → terminate）、`session_idle`（:1070，v4 → terminate）、`reconnect_no_replay`（:2190，v4 冻结 reason，正常帧）、`session_deleted`（:1132 经 terminate）；`subscriber_backpressure` 在 subscriber.py 侧触发，hub 的 events_tap 直推路径复用同一 `put` 守卫（A-C4）。
- **warning 日志点**：:1392、:1422-1423（仅这两处 warning）；critical：:469-473。
- **静默丢弃（设计内，C3/C4）**：orphan delta 只计数（:787）；nontext/disabled/retired/deleted-sid/ended-late delta 全静默。

## 6. 疑问点（draft 种子，24 条，宁多勿漏）

1. **TODO :663（on_part_updated）**：`# TODO(§13.2): confirm live wire key casing for properties.part.` —— `props.get("part")` → `part.get("sessionID"/"messageID"/"id")` 的大小写是**未与 live wire 核对的假设**。若实际 wire 携带 `sessionId` 等不同 casing，:665-671 直接 return——**无声无计数无日志**（连 `orphan_deltas` 都不加），text-start 整个丢失后所有 delta 变 orphan。影响：整段流静默消失，仅能靠 orphan_deltas 涨数间接发现 text-start 没建出来。
2. **TODO :760（on_part_delta）**：同族 casing 假设（`sessionID`/`messageID`/`partID`/`field`/`delta`）。:761 `field != "text"` 与 :763-766 任一不匹配都是静默 return。与 :663 同为「上游 schema 漂移 → 全静默」风险点。
3. **`_busy_sids` 生产端只写不读**（:283、:1056-1058、:1061、:1113、:2176；docstring :240 称 "O(1) busy lookup mirror"）。`ttl_sweep` 实际读的是 `_session_status.get(sid)`（:1190）。rg 反查：生产代码零读者，仅测试断言读（test_token_hub_lifecycle.py:533 等）。死状态 + 每次状态事件多付一次 prune。
4. **tombstone 回放排序成本**（:1315-1317）：v3 握手对**全局** `_removed_messages`（最多 1000 项）按 timestamp 全排序再按 sid 过滤——每次 attach O(N log N)，且过滤在排序后。可先按 sid 过滤再排序。
5. **`_check_pending_budget` 的 had_subs 是全局计数**（:1853）：sid B 无订阅者触发 pending 溢出、但 sid A 有订阅者时 → `had_subs=True` → 只 force-flush（B 的 delta 静默丢）**不逐出**；B 的 LivePart 继续增长，仅靠 LIVE 预算兜底，且每条 delta 反复触发全量 flush（:1855）。docstring :1848-1849 说的是 "NO subscribers"，实现是全局口径——语义偏差。
6. **v4 订阅者 oversized part 零信号**（:1562-1576 + :1691-1696）：v4 探针超限 → `_truncate_part_for_all` → truncated 帧只投 v3（`_deliver_v3_only`）。v4 客户端对该 part **什么都收不到**（无 snapshot、无 truncated、无 resync、done 也被抑制），只能靠 HTTP 全量拉取自愈——契约上成立（v4 状态对齐=HTTP），但流侧无任何可观测信号。
7. **v4 非冻结 reason 一律 terminate**（:1520-1524）：`session_idle` 也走 `sub.terminate`——v4 sub 挂在一个 idle→busy→idle 波动的会话上会被**反复断连**而非收 resync。R3 语义如此，但对会话生命周期短的 v4 客户端体验是断连风暴隐患。
8. **`_on_flush_done` 重建无退避**（:469-476）：若 `flush()` 确定性抛异常（如未来某 tap/序列化路径引入异常），watchdog 以 ≤10Hz 频率 CRITICAL 刷屏 + 无限重建，无 retry budget/backoff。当前 `Subscriber.put` 不抛（返回 bool），但该不变量靠约定不靠类型。
9. **`_next_part_revision` LRU cap 逐出可致 revision 回退**（:730-737）：条目被 4096-cap 逐出后同 key 重新从 0 计数 → 严格 `>` 客户端丢后续帧。防线是 config.py:142-144 的 `assert TOKEN_LIVE_PARTS_MAX(32) <= TOKEN_DISABLED_MAX(4096)` + 活跃 part 的 LRU 热度——**隐式跨模块不变量**，`apply_debug_budget_overrides` 只校验 live_parts_max 一项（config.py:1033-1038）。
10. **进程重启 revision 归零**（:2149-2155 KNOWN LIMITATION）：文档已承认，跨仓协议问题。审计时确认 ocdroid 侧无「sidecar 冷启动重置水位」信号即可。
11. **`on_upstream_reconnect` 的 gate/queue 解耦**（:2180 清 `_retired_messages` vs :2184-2185 保留 `_removed_messages`）：违反 :341-344 声明的「gate 生命周期与 replay 队列耦合」不变式。理由是新 epoch 无迟到事件（GlobalBus 无 replay）——前提成立则无害，但这是**依赖上游行为假设的非对称**。
12. **`ttl_sweep` 退役不写 barrier、不发任何帧**（:1192-1202）：与 `_evict_part_for_memory`（:1801 写 barrier + resync）不一致。可达路径：idle 后又有迟到 text-start（idle 转换时 `_retire_session` 已清过一轮，:1062）→ 新 LivePart 累积 → 60s 静默 → TTL 退役。此时 v4 客户端 cursor==last_seq 仍判 up-to-date 挂在死 part 上（idle barrier 的 watermark 早于该 part 的帧）。极边缘但与 R4 rationale 相悖。
13. **`finish_part` 对孤儿 text-end 也会 disable key**（:983-1014 → `drop_part` :1919-1946 docstring「从未见过的 key 也标记 disabled」）：sidecar 重启错过 text-start 的 part，其 text-end 会把 key 永久（6h TTL 内）拉黑；若上游对同 pid 重发 text-start（文档 :1939-1941 称 session 内 ID 不复用）会被 `_is_disabled` 静默吞。
14. **`_emit_snapshot_or_truncated` v4 探针缺 revision 字段**（:1573，注释自认 ~10 字节偏差）：v3/v4 截断边界不一致——理论上一帧「v3 判不超、v4 探针判超」的窗口存在，导致 v4 路径多 truncate 一个本可不截的 part。
15. **revision 在尺寸检查前消费**（:1577-1581）：oversized snapshot 的 revision 被浪费（docstring :1553-1560 自认）。客户端严格 `>` 仍接受后续帧，只是序列有洞——可观测性小噪声。
16. **`flushed_frames_total` 计的是投递尝试数**（:1481、:1437-1438 返回 `len(subs)`）：`sub.put` 返回 False（closed/溢出）也计入。指标名与语义不符（delivered vs attempted）。
17. **`dropped_frames_total` 在 hub.py 零递增**（属性 :406-408；递增全在 subscriber.py:405/421/441）：靠 `subscriber.py:681` 的 `self.token_hub._metrics` **私有属性穿透**共享同一 `_TokenMetrics` 实例——封装脆弱点，两文件必须同实例否则指标分裂。
18. **`apply_debug_budget_overrides` 改模块全局**（:155-161）：生产误设 `OC_SLIMAPI_TOKEN_STREAM_DEBUG_*` 即改变所有 hub 实例预算；validate 只挡 live_parts_max 一项（config.py:1033-1038），`live_budget_bytes < TOKEN_PART_MAX_BYTES` 的误配会触发 :1741-1745 自认「不可达」的防御分支（当前 key 也被 truncate）。
19. **`_start_part` 先入账后逐出**（:1894-1917）：seed 字节先加进 `_total_live_bytes`（:1907）再 while 逐出（:1912）——与 `_reserve` 的「先查后加」模式相反，瞬时可超 cap；单线程内无害，但若未来并发化是隐患。且 count-cap 逐出（:1891-1893）在加 seed 之前，两轮逐出条件不对称（`>=` vs `>`）。
20. **import 时换算的 tick 间隔**（:121、:123）：`_TTL_TICK_INTERVAL`/`_HEARTBEAT_TICK_INTERVAL` 在 import 时由常量算出，事后 monkeypatch `TOKEN_FLUSH_SECONDS` 不生效（测试只能直接 patch 间隔，test_token_hub_flush.py:403/:438 即如此）——行为耦合点，非 bug。
21. **`events_tap` 是公有可变 list**（:298）：类型 `list[Any]`（装 bound `sub.put`），registry 双账本（`registry.events_tokens` set + `hub.events_tap` list）靠 `attach/detach_events_subscriber` 对称维护（subscriber.py:601-621）——docstring :387-390 自称「no parallel counter to drift」，但两容器本身**就是**并行账本，detach 的 `suppress(ValueError)`（subscriber.py:619）掩盖不对称。
22. **`on_part_updated` 重复 text-start 忽略 seed**（:706-710）：`key in live_parts` 时不 append seed。若上游在重复 text-start 里携带了更长累积文本（非重复而是补偿），多出的部分静默丢失，依赖后续 delta 补齐——依赖「text-start 幂等且文本单调」的上游假设。
23. **handshake tombstone 回放无 id/无 log**（:1322 `sub.put` 直投）：v3 客户端重连会再次收到同一批 tombstone（幂等性交给客户端）；同时该路径在 `begin_handshake` 括号内绕过 T3 溢出守卫（CRITICAL 3 设计），超大批量 tombstone（理论上限 = 1000 + 32 snapshot，config.py:87-88 注释自算）可无上限压入握手缓冲——溢出守卫被显式绕过后仅靠 `sub.closed` 事后检查兜底（:1335-1336）。
24. **命名/杂项**：`_pending_session_resinks` 拼写（resinks vs resyncs，:285 等）；`flush()` 每 tick 两次全量列表推导（:542 sorted + :572 全量扫描空 acc，O(N)·10Hz，N 受 4MiB 预算约束尚可）；`attach_subscriber` v4 分支首帧最多延迟 100ms（:1289-1304 无 flush_sid，留给下个 tick，docstring 自述）。

### 任务点名专题小结

- **两处 TODO（:663/:760）**：见疑问点 1/2——同一根因（live wire key casing 未核实），影响是「静默丢流」，建议 draft 阶段对照 opencode `message-v2.ts`/event payload 实测核对。
- **tombstone 回放**：v3 握手 :1313-1322（全局排序成本 #4）；v4 改由 ReplayLog 承担（:1498）；gate 与队列耦合破裂于 reconnect（#11）。
- **预算/flush 窗口**：LIVE/PENDING 双 4MiB 独立账本（:276-277）；4KiB 早 flush（:811-823）在 `_check_pending_budget`（:829）之前；had_subs 全局口径偏差（#5）；`_start_part` 先加后逐（#19）；debug override（#18）。
- **空闲逐出**：`ttl_sweep` busy-guard（:1190 只认已知 idle）+ 静默退役无 barrier（#12）；`_reserve`/`_start_part`/`_check_pending_budget` 三处 LRU 逐出共用 `_evict_part_for_memory`，skip_key/nodrop 保 O1（:1789-1821）。
- **events-token 保活双账本**：`events_tap`（hub :298）+ `events_tokens`（registry subscriber.py:581）并行容器（#21）；`has_consumers`（:395）统一判活修复了 events-only 死循环问题（docstring :390-393）。
- **背压溢出断连路径**：hub 侧全部经 `sub.put`（返回 bool 被忽略，#16）；溢出 → subscriber 内部 terminate(resync+STOP)+closed（subscriber.py:386-441）；握手期被 `begin/end_handshake` 绕过（#23）；v4 非冻结 reason 走 terminate 不走帧（#7）。

<!-- ==== e1-10-tokenstream-rest ==== -->
# E1-10 精读卡片 — tokenstream subscriber / sse registry / frames / models

> 审计探索产物（2026-08-20）。只读精读，引用格式 `src/oc_slimapi/...:行号`。四个文件均全文精读（未抽样）。

---

### src/oc_slimapi/sse/tokenstream/subscriber.py（874 行）

- **职责**：token stream 的消费者侧三件套：(1) `_SubscriberQueue` — 把 handshake 预填充（有界 deque）与 runtime T3 背压（有界 asyncio.Queue）物理分离（rev-ogpt CRITICAL 3）；(2) `TokenSubscriber` — 单 session 出站队列，T3 三段守卫（closed → oversized-drop → overflow-disconnect）（design §5.5/§5.6/§16-D）；(3) `TokenStreamRegistry` — token 订阅独立准入账本（不占 `MAX_TOTAL_SUBSCRIBERS`）+ flush loop 首挂/尾卸生命周期 + events-token tap 账本（L2-A）。

- **对外符号**（逐个）：
  - `_SubscriberQueue`（:58）
    - `__slots__`（:78-86）：`_runtime/_handshake/_handshake_max_items/_handshake_max_bytes/runtime_bytes/handshake_bytes/last_get_handshake`。
    - `__init__(*, runtime_max_items, handshake_max_items, handshake_max_bytes)`（:88）：runtime `asyncio.Queue(maxsize=…)`（:99，" defence-in-depth"，正常路径 caller 先检 `qsize()`，QueueFull 永不触发）；handshake `deque()`（:100）；双字节账本（:104-105）；`last_get_handshake` 单槽切换（:111，get→ack 路由用）。
    - `put_handshake(frame) -> bool`（:116）：**fail-on-overflow**（非 drop-oldest）——条目数或字节数超帽即返回 False（:133-137），落地则 append + `handshake_bytes += size`（:138-139）。
    - `put_runtime(frame)`（:145）：`put_nowait`，非 STOP 计入 `runtime_bytes`（:155-157）；不复查上限（caller 责任）。
    - `clear_runtime()`（:159）：清空 runtime 队列 + `runtime_bytes = 0`（:167-172）；**不动 handshake**。
    - `put_runtime_terminal(frame)`（:174）：终态帧（resync/STOP）入队但不计账（"backlog cleared, terminal pair sealed outside the budget"）。
    - `ack_runtime(frame)`（:186）/ `ack_handshake(frame)`（:192）：floor-0 字节减记，STOP no-op。
    - `qsize()`（:201）：**仅 runtime 深度**（handshake 有意不计入 T3 item 帽，:202-209）；`handshake_qsize()`（:212）诊断面；`empty()`（:216）双侧。
    - `get()` async（:219）/ `get_nowait()`（:227）：handshake 先排干（"handshake drains first"），并置 `last_get_handshake`。
  - `@dataclass(eq=False) TokenSubscriber`（:235）
    - 字段：`session_id`（:289）、`metrics: _TokenMetrics`（:290）、`queue_items=64`（:291）、`buffer_bytes=512KiB`（:292）、`max_frame_bytes=DEFAULT_TOKEN_MAX_FRAME_BYTES`（:293, 1MiB）、`handshake_items=TOKEN_HANDSHAKE_ITEMS`（:302, 2048）、`handshake_buffer_bytes=TOKEN_HANDSHAKE_BUFFER_BYTES`（:303, 8MiB）、`id="tok_"+hex4`（:305）、`closed`（:306）、`_handshake_overflow`（:310, MINOR 1 错误码区分）、`dropped_frames`（:311）、`forced_disconnects`（:312）、`wire_v4`（:323, B3b-2）、`queue`（:325）、`_in_handshake`（:331）。
    - `__post_init__`（:333）：惰性建 `_SubscriberQueue`。
    - `queued_bytes` property（:341）：`runtime_bytes + handshake_bytes`（与控制面 Subscriber 可变字段同名 duck-type 兼容；T3 字节检查直读 `queue.runtime_bytes`，handshake 字节不计入 runtime 预算）。
    - `begin_handshake()`（:354）/ `end_handshake()`（:358）：切 `_in_handshake`。
    - `put(frame) -> bool`（:362）：T3 路由 — `closed` → 静默丢弃（:386-390）；`STOP` → 恒走 runtime（:391-396）；oversized（`len > max_frame_bytes`）→ drop + `dropped_frames+1` + `metrics.dropped_frames_total+1`，**不闭连接**（:397-406）；handshake 模式 → `put_handshake` 失败则 `closed=True` + `_handshake_overflow=True` + 双计数（:407-423）；runtime 守卫 `qsize() < queue_items and runtime_bytes + size <= buffer_bytes`（:428-431）；溢出 → `closed=True` + `forced_disconnects+1` + `metrics.dropped_frames_total+1` + `clear_runtime()`（:439-442），v4 且 reason 不在 `V4_RESYNC_REASONS` → 仅 STOP（:450-452），v3 → `_resync_frame(sid,"subscriber_backpressure")` + STOP（:453-457）。
    - `terminate(reason)`（:460）：INV-4 服务端终止（session.deleted）——closed + clear_runtime +（reason 在 v4 域内或 v3 才发）resync + STOP（:487-493）；**不** bump forced_disconnects/dropped；不摘除 fanout（留给 generator finally → unsubscribe）。
    - `ack(frame)`（:495）：按 `last_get_handshake` 路由到对应账本；STOP no-op（:507-512）。
  - `TokenSubscriberCapacityError(Exception)`（:515）：`code`（`sse_token_subscriber_limit` / `sse_token_handshake_overflow`）、`limit/current/buffer_bytes`（:528-533）。
  - `TokenStreamRegistry`（:536）
    - `__init__(token_hub, hub_registry, *, max_subscribers, queue_items, buffer_bytes, max_frame_bytes)`（:554）：`total_subscribers/rejected_total`（:570-571）、`events_tokens: set[Any]`（:582, L2-A）。
    - `attach_events_subscriber(sub)`（:584）：幂等集合去重（:600-601）+ `token_hub.events_tap.append(sub.put)`（:603）+ `token_hub.start()`（:606）。
    - `detach_events_subscriber(sub)`（:608）：discard + `events_tap.remove(sub.put)`（suppres ValueError, :617-619）+ 双账本皆空才 `th.stop()`（:620-621）+ `maybe_arm_grace_if_idle()`（:622-623）。
    - `subscribe(sid, wire_v4=False) -> TokenSubscriber`（:625）：容量检查（:668-674）→ 早建 sub（无副作用, :679-685）→ 先盖 `wire_v4` 章（:689）→ try：`hub_registry.get_global()` + `cancel_pending_removal()` + `ensure_upstream()`（:696-699）+ `token_hub.start()`（:701）+ `attach_subscriber`（:705）；`except asyncio.CancelledError: raise`（:706-707）；`except Exception:` rollback + `rejected_total+1` + raise（:708-711）；post-attach `sub.closed` 复查（:725）→ `_rollback_failed_attach` + 抛 `TokenSubscriberCapacityError`（MINOR 1 按 `_handshake_overflow` 选码, :732-742）；成功才 `total_subscribers += 1`（:743）。
    - `_rollback_failed_attach(sid, sub)`（:746）：防御性 detach（:772-773）+ 双账本空才 `th.stop()`（:781-782）+ 对称重挂 grace（:786-787）。
    - `unsubscribe(sub)`（:789）：**成员守卫真幂等**（NB-D1, :821-822 `if not th.has_subscriber(...): return`）→ detach + 减记 + floor 0（:823-827）→ 双账本空才 stop（:831-832）→ `maybe_arm_grace_if_idle()`（:835-836）。
    - `snapshot_token_metrics()`（:838）：读 `th._metrics/th._pending/_subs_by_sid` 私有；`maxSubscriberQueueDepth` 只算 runtime 深度、只算 fanout 内 sub（:852-857）；输出 `sse.tokenStream.*` 14 键（:858-874）。

- **依赖 / 被依赖**（rg 反查）：
  - 依赖：`config`（TOKEN_HANDSHAKE_ITEMS=2048/BUFFER_BYTES=8MiB/DEFAULT_TOKEN_MAX_FRAME_BYTES=1MiB, config.py:81/100-101 + 静态断言 :153-163）；`.frames`（STOP, _resync_frame）；`..replay_wire.V4_RESYNC_REASONS`（replay_wire.py:72-77，四值冻结域）；`.models._TokenMetrics`；`..hub.HubRegistry`（TYPE_CHECKING, :23-24）。运行期通过 `token_hub`（TokenStreamHub）与 `hub_registry`（HubRegistry）协作：`attach_subscriber/has_subscriber/detach_subscriber/start/stop/events_tap`（tokenstream/hub.py:1211/1355/1340/421/478/298），`get_global/cancel_pending_removal/maybe_arm_grace_if_idle`（registry.py:143/146/161）。
  - 被依赖：`sse/tokenstream/__init__.py:8` 再导出；`sse/token_hub.py:11-13` 兼容 shim；`routes/token_stream.py:63,172-182`（subscribe + 503 映射）；`routes/events.py:162,237`（attach/detach_events_subscriber）；`routes/metrics.py:31`（snapshot_token_metrics）；`app.py:560-567`（构造，参数来自 settings.token_stream_*）；测试 `tests/test_token_subscriber_overflow.py`（874 行级专项）、`test_token_stream_route.py`、`test_events_tokens.py`、`test_batch3_lifecycle.py`、`test_sse_replay_wire.py`、`test_token_hub_flush.py`。

- **状态 / 可变性**：
  - 每 sub：handshake deque（一次性，attach 同步段内填充）+ runtime bounded Queue（64 item / 512KiB 默认）+ 双字节账本 + `last_get_handshake` 单槽 + `closed/_in_handshake/_handshake_overflow/wire_v4` 布尔 + `dropped_frames/forced_disconnects` 计数。全部单事件循环内同步访问，无锁（admission 关键段无 await，:637-645 声明）。
  - Registry：`total_subscribers/rejected_total` int、`events_tokens` set（identity 去重，控制面 `Subscriber` 为 `@dataclass(eq=False)` hub_types.py:213 可哈希）。本模块**不持有 task**（flush task 在 TokenStreamHub `_flush_task`，tokenstream/hub.py:302/439）。
  - 消费者（routes/token_stream.py:293-302）`await queue.get()` → STOP 则 break → `finally: registry.unsubscribe(subscriber)`（:303-307）。慢客户端背压链路：ASGI send 阻塞 → generator 停在 yield → runtime 队列涨 → put 溢出 → 立即 clear_runtime + resync+STOP → generator 排干 handshake 后取 STOP 断开。**公平性**：put 全为 put_nowait（永不阻塞 flush loop），单慢 sub 只影响自己（每 sub 独立队列），无队头阻塞；heartbeat 每 15s（tokenstream/hub.py:1529-1536）保证 get() 不会永久饿死。

- **错误路径**：容量满 → `sse_token_subscriber_limit`（503+Retry-After:5, routes/token_stream.py:177-181）；handshake 溢出 → `sse_token_handshake_overflow`（含 bufferBytes）；attach 任意异常 → rollback 后原样 raise（INV-3）；oversized 帧 → 丢弃不闭连（C6 由 hub 出 truncated 替代帧）；runtime 溢出 → v3 `resync{subscriber_backpressure, sessionID}`+STOP / v4 STOP-only（rev-gate R3 BLOCKER-1）；`session.deleted` → `terminate("session_deleted")`（hub.py:1131-1132 调用）。

- **疑问点（13）**：
  1. **:397-406 vs :651-653/:729-736 文档失真**：`put()` 的 oversized 分支只 drop 不置 `closed`，但 `subscribe()` docstring（:651-653）与 MINOR 1 注释（:729-731 "handshake buffer / oversized-frame guard"）声称 oversized 守卫可导致 attach 带 `closed=True` 回来并映射 `sse_token_handshake_overflow`。按现行代码 oversized 永不闭 sub；若未来某路径在 handshake 期闭 sub 而未置 `_handshake_overflow`，错误码会回落到语义错误的 `sse_token_subscriber_limit`（容量码，实际失败是帧尺寸）。
  2. **:440-441 指标口径**：runtime 溢出 `metrics.dropped_frames_total += 1` 且 `dropped_frames`（per-sub）不加，但 `clear_runtime()` 实际丢弃可达 64 帧/512KiB —— 指标计的是"断连事件数"而非"丢帧数"；与 oversized 路径（:404 同时 bump per-sub）不一致；`snapshot_token_metrics` 未暴露 per-client 计数，仅总量。
  3. **:449-457 v4 溢出可观测性**：v4 断连只有 STOP（无 resync reason），客户端无法区分 backpressure / 服务端 close / `session_deleted`（:489-493 后者在 v4 同样静默）。设计上以"断连即信号 + Last-Event-ID 重连"恢复，但对排障仅剩 metrics。
  4. **:303 + config.py:90-94 握手字节帽可行性**：8MiB 帽 vs 32×近1MiB snapshot + JSON 转义放大（config 注释自认 "may be insufficient"）→ 合法大状态也可能 503 重试循环（客户端 Retry-After:5 无限重试同一 sid）。确认产品接受度 / 是否需要按 sid 降级策略。
  5. **:694-711 CancelledError 路径无 rollback**：`except asyncio.CancelledError: raise`（:706-707）跳过 `_rollback_failed_attach` —— 若 `ensure_upstream/start/attach` 段内出现 await 点（当前全同步，但 `ensure_upstream` 内部或未来改动引入 await）后被 cancel，已做的副作用（grace 取消、flush 启动）不回滚。当前无 await 时 CancelledError 只能来自更外层，仍会泄漏已 start 的 flush loop（由 `_rollback_failed_attach` 覆盖的路径不走）。
  6. **:19 vs hub_types.py:30 双 STOP 哨兵**：tokenstream `STOP` 与控制面 `STOP` 是不同 `object()`；若误把控制面 STOP 喂给 `TokenSubscriber.put`，`frame is STOP` 为 False → `len(frame)` 对 `object()` 抛 TypeError。当前无跨用（rg 验证），但无类型防呆。
  7. **:109-111 `last_get_handshake` 单槽假设**：仅当"单消费者且 get→ack 严格成对"成立才正确；route generator 满足，但任何 `get()` 后不 `ack()` 或乱序 ack 会使账本减记路由错侧（floor-0 防负数，不防漂高）。
  8. **:227-232 `get_nowait()` 空队抛 QueueEmpty**：runtime 侧直接透传 `get_nowait`；当前消费面只用 async `get()`（routes/token_stream.py:294），`empty()`（:216）有但无人用 —— 若未来用 get_nowait 轮询需自catch。
  9. **:710 `rejected_total` 语义混合**：容量拒绝（:669）、attach 异常（:710）、handshake 溢出（:727）共用一个计数器，metrics 无法区分原因；且 handshake 溢出时 `limit/max_subscribers` 字段报的是与失败无关的容量帽（:739-740）。
  10. **:582-606 events_tokens 泄漏面**：强引用控制面 Subscriber + `events_tap` 里的 bound method；清理完全依赖 events 路由 generator finally（routes/events.py:237）。若 generator 从未启动（Starlette 取消响应体前不再迭代），`events_tokens`/`events_tap` 永不清 → flush loop 永不停（100ms 空转）+ GlobalHub grace 永不挂。依赖框架 aclose 语义，本仓无兜底超时。
  11. **:852-857 深度规口径**：`maxSubscriberQueueDepth` 只看 `_subs_by_sid`（per-session sub）的 runtime 深度；events-token 消费者（走控制面自己的 queue）与其 flush 压力不在该 gauge 内，而 `has_consumers()`（tokenstream/hub.py:395）却覆盖 taps —— 观测谓词与存活谓词不对称。
  12. **:838-874 层级穿透**：registry 直读 `th._metrics/_pending` 私有属性（:849-853,862-867），hub 已有同名 public property（flushed_frames_total 等, tokenstream/hub.py:402-416）—— 混用公私接口，形状已冻结但实现耦合。
  13. **:325 `queue: _SubscriberQueue = field(default=None)`**：注解非 Optional 却默认 None；且 `queued_bytes` property（:341）与控制面 Subscriber 的可变字段（hub_types.py:241）同名 —— duck-type 读 OK，任何 `sub.queued_bytes += x` 写法（控制面测试风格）会 AttributeError。测试通过 monkeypatch `_SubscriberQueue.__init__` 缩帽（test_token_stream_route.py:999-1011），说明该构造缝是被依赖的测试面。

---

### src/oc_slimapi/sse/registry.py（408 行）

- **职责**：`HubRegistry` — 进程唯一 `GlobalHub` 的持有者 + 控制面（curated SSE）T3 准入（per-directory/total 双帽）+ 空闲 grace 拆除编排（`GRACE_SECONDS=30`, hub_types.py:94）+ 对 token hub / turn registry / replay log / TransformPool 的接线板（set_* 转发到惰性创建的 hub）。

- **对外符号**：
  - `HubRegistry.__init__(client, *, max_subscribers_per_directory, max_total_subscribers, queue_items, buffer_bytes, max_frame_bytes, traffic_ledger=None)`（:50-84）：`_global`、双帽、`total_subscribers/rejected_total`（:69-70）、`_transforms`（:71）、`_token_hub`（:75）、`_turn_registry`（:79）、`_replay_log`（:83）、`_removal_task`（:84）。
  - `set_transforms(pool)`（:86）：仅 metrics 引用。
  - `set_token_hub(token_hub|None)`（:94）：写入自身 + 活跃 hub 的 `_token_hub`（:100-102 私有直写）。
  - `set_turn_registry(registry|None)`（:104）：同型转发（:111-113）。
  - `set_replay_log(replay_log|None)`（:115）：经 hub 的 `set_replay_log` 转发（:126-127）。
  - `get(directory=None) -> GlobalHub`（:129）：惰性建 hub 并转发全部接线（:130-140）；**directory 被忽略**（:37-40 兼容声明）。
  - `get_global()`（:143）：`get(None)`。
  - `cancel_pending_removal()`（:146）：cancel `_removal_task` + 置 None，幂等（NB-B1 —— token subscribe 在 grace 窗口内到达时不被拆）。
  - `maybe_arm_grace_if_idle()`（:161）：hub 存在 && `hub.has_consumers()` 为 False && 未在挂 → `create_task(_remove_hub_after_grace)`（:185）。`has_consumers` 跨两账本（global_hub.py:362-381：控制面 subscribers ∪ `_token_hub.subscriber_count > 0`）。
  - `subscribe(wire_v4=False) -> Subscriber`（:187）：单同步关键段——per-directory 帽（:212-218, `sse_subscriber_limit_directory`）→ total 帽（:219-225, `sse_subscriber_limit_total`）→ `hub.subscribe(welcome=not wire_v4)`（:226, v4 抑制 server.connected）→ 盖 `wire_v4`（:227）→ 增记（:228）。**有意不 cancel `_removal_task`**（:203-207 注释：保持同步性，靠 grace task 醒后 `has_consumers()` 复查自 abort）。
  - `unsubscribe(subscriber)`（:232）：幂等（:240-241 成员守卫）→ discard + 减记 + floor 0（:242-247）→ `maybe_arm_grace_if_idle()`（:256, NB-D3 双账本谓词）。
  - `_remove_hub_after_grace(hub)` async（:258）：sleep 30s（:284）→ 复查 `hub is self._global and not hub.has_consumers()`（:287）→ cancel hub 4 task（:295-300）→ `gather(..., return_exceptions=True)`（:303-305, INV-2 严格串行 epoch）→ 复查（:314）→ 同步段：`token_hub.on_upstream_reconnect()`（:322-323, 清旧 epoch ingest 态；`_part_revisions/_removed_messages` 保留）→ `self._global = None; _removal_task = None`（:324-325）。
  - `snapshot_metrics()`（:327）：冻结形状 `sse.subscribers{current,limit,rejectedTotal}/hubs[...]/clients[...]` + `skeleton`（:344-370）。
  - `_snapshot_skeleton()`（:372）：TransformPool 公共 API；`cacheEnabled` 硬编码 False。
  - `close()` async（:389）：hub 4 task + `_removal_task` 一并 cancel + gather（:396-406）→ `_global = None`、`total_subscribers = 0`（:407-408）。

- **依赖 / 被依赖**（rg 反查）：
  - 依赖：`global_hub.GlobalHub`、`hub_types`（常量/Subscriber/SubscriberCapacityError/STOP/GRACE_SECONDS）；TYPE_CHECKING httpx/TrafficLedger/TurnRegistry/TokenStreamHub。
  - 被依赖：`app.py`（构造 `app.state.hubs`、lifespan 内 `set_token_hub/set_turn_registry/set_replay_log/set_transforms`、shutdown `await app.state.hubs.close()` app.py:497）；`routes/events.py:245`（unsubscribe）与 subscribe 入口；`routes/metrics.py:22`（snapshot_metrics）；`tokenstream/subscriber.py`（反向：token registry 持 hub_registry 引用，调 get_global/cancel_pending_removal/maybe_arm_grace_if_idle）；`sse/hub.py` 再导出；测试十余个文件（test_hub*.py、test_batch3_lifecycle.py、test_sse_replay_wire.py 等）。

- **状态 / 可变性**：
  - `_global`：单例强引用；grace 拆除与 close 置 None（GC 释放 hub + task 句柄，:260-264 注释）。
  - `_removal_task`：本文件唯一 `asyncio.create_task`（:185）；被 `cancel_pending_removal`（:157-159）与 `close`（:400-402）cancel+置 None。
  - 准入关键段（:209-229）无 await —— 协作调度下无 over-admit；`unsubscribe` 幂等防负数。
  - 全库 `create_task/ensure_future` 清点（本卡范围内逐点）：registry.py:185（如上，异常路径见疑问 1）；global_hub.py:238-240（run/flush/heartbeat，hub 自持）、:360（stop_after_grace → `hub.stop_task`）；tokenstream/hub.py:439（flush loop + done_callback 看门狗 :443-476，异常死亡自动重建）。范围外：traffic_snapshot.py:372、actions.py:648/666/669、qp_sweep.py:225、routes/permissions.py:461、routes/questions.py:439、app.py:448/675、dbaux/lifecycle.py:378（各文件自管）。

- **错误路径**：`subscribe` 双帽 raise `SubscriberCapacityError`（events 路由转 503+Retry-After）；`unsubscribe` hub 缺失/非成员 no-op；`_remove_hub_after_grace` sleep 期被 cancel → 直接 return（:285-286）；gather 期被 cancel → return 不置空（:306-311，依赖 canceller 已置 None）；`close` gather 吞异常（teardown 可接受）。

- **疑问点（9）**：
  1. **:283-325 `_remove_hub_after_grace` 无 `except Exception` 兜底**：sleep 后的拆除体（尤其 :322-323 `on_upstream_reconnect()`）若抛异常，task 带 exception 死亡 → "Task exception was never retrieved" 警告 + `_removal_task` 残留非 None → `maybe_arm_grace_if_idle` 的 `if self._removal_task is not None: return`（:183-184）**永久失效**，且 `_global` 未置空（连接泄漏恰是它要修的 B-D1）。CancelledError 有处理，普通异常没有。
  2. **:203-207 靠复查而非取消的窗口**：`subscribe` 不 cancel grace task；若 grace task 恰在 :303 `await gather` 期间被新订阅"复活"——复查 :314 会 abandon，正确；但若订阅发生在 :287 复查之后、cancel 循环（:295-300, 同步不可插入）之前——不可能。唯一漏洞是 hub task 被 cancel 后 `hub.subscribe→ensure_upstream` 是否能复活已 cancel 的 task（属 global_hub 卡范围，此处挂链接待 E-05 核对）。
  3. **:212-218 "directory" 帽名存实亡**：单一全局 hub（:37-40 自认 directory ignored），`max_subscribers_per_directory` 实为第二个全局帽，错误码 `sse_subscriber_limit_directory` 对客户端传达错误语义；两帽作用于同一集合，前者恒 ≤ 后者时后者永不触发（默认值需核对 hub_types）。
  4. **:226 `hub.subscribe` 半途异常的账本漂移**：若 GlobalHub.subscribe 内部先 `subscribers.add` 后抛（未读其实现，E-05 核对），`total_subscribers` 不增而 hub 集合已有成员 → admission 永久少记一个；本文件假设 hub.subscribe 原子。
  5. **:296 hub 4 task 清单硬编码**：`(hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)` 与 `close()`（:397）重复列举 —— GlobalHub 新增第 5 个 task 时两处都要改，易漏（无单一 source of truth）。
  6. **:389-408 `close()` 不清 token 侧账本**：`total_subscribers=0` 只救控制面；`TokenStreamRegistry.total_subscribers`、`events_tokens`、token hub 状态不归它管（app.py:543-551 用 ExitStack LIFO 先 `token_hub.stop()` 再 `hubs.close()`），但 token registry 的 `total_subscribers` 若此刻 >0（生成器 finally 未跑完）则残留 —— 进程关闭场景无害，测试复用 app 时可能。
  7. **:322-323 命名误导**：grace 拆除调用 `on_upstream_reconnect()`（实则"epoch 重置/清态"语义）；虽注释明确，函数名暗示的"重连 fanout resync"行为在 `has_consumers()==False` 下是 no-op —— 未来读者易误解其在有消费者时的副作用。
  8. **:185 task 未命名**：`asyncio.create_task(self._remove_hub_after_grace(hub))` 无 `name=`，与 qp_sweep（name="qp-sweep-shadow"）风格不一，排障时难从 task 列表辨认。
  9. **:341-358 `snapshot_metrics` 与 token 块拼装层级**：本文件产出控制面形状，metrics 路由再补 `sse.tokenStream`（metrics.py:31）——`clients[]` 只含控制面 sub，token sub 的 per-client 计数（dropped/forced_disconnects）无处暴露（对照 tokenstream snapshot 只有聚合值）。

---

### src/oc_slimapi/sse/tokenstream/frames.py（152 行）

- **职责**：token stream 的 wire 帧构造器（design §5.6）——snapshot / delta / truncated / resync / server.connected / server.heartbeat / message.removed 七种帧 + `STOP` 哨兵 + `PartKey` 类型别名 + SSE 序列化底座 `sse_frame`。

- **对外符号**：
  - `PartKey = tuple[str, str, str]`（:13）：(sessionID, messageID, partID)。
  - `STOP = object()`（:19）：runtime 终态哨兵（"kept local to avoid a runtime import cycle"）。
  - `_now_ms()`（:22）：epoch 毫秒（防 import 环有意复制自 hub）。
  - `sse_frame(payload, event=None) -> bytes`（:33）：`event: <name>\n`（可选）+ `data: <orjson>\n\n`（:42-43）。
  - `_snapshot_frame(key, text, done, part_revision=None)`（:53）：payload 固定序 `sessionID/messageID/partID/done`（:57-62）+ 可选 `text`（:63-64）+ 可选 `partEventRevision`（:65-81, rev-ogpt CRITICAL 1 Option B **per-frame** 严格递增）；event `message.part.snapshot`。
  - `_delta_frame(key, text, part_revision=None)`（:85）：`sessionID/messageID/partID/text` + 可选 revision（:88-99）；event `message.part.delta`。
  - `_truncated_frame(key, done, part_revision=None)`（:103）：`…/truncated:true/done` + 可选 revision（:107-121）；event 复用 `message.part.snapshot`（:122）。
  - `_resync_frame(sid, reason)`（:125）：`{"reason": reason, "sessionID": sid}`（**reason 在前**）；event `resync`。
  - `_connected_frame(sid)`（:129）：`{"sessionID"}`；event `server.connected`。
  - `_heartbeat_frame()`（:133）：`{}`；event `server.heartbeat`。
  - `_message_removed_frame(sid, mid)`（:137）：`{"sessionID","messageID"}`（message 级、无 partID）；event `message.removed`（:150-152）。

- **依赖 / 被依赖**：
  - 依赖：仅 `time` + `orjson`（:7,10）—— 零内部依赖（除注释引用 hub）。
  - 被依赖：`tokenstream/subscriber.py:19`（STOP, _resync_frame）；`tokenstream/hub.py:92-104`（全部 builder + PartKey）；`tokenstream/models.py:10`（_now_ms）；`tokenstream/__init__.py:2-5` 再导出；`sse/token_hub.py` shim；`routes/token_stream.py:61-66`（经 token_hub 取 STOP/_resync_frame/sse_frame）。

- **状态 / 可变性**：全模块无状态（纯函数 + 模块级常量 STOP）；orjson 序列化确定性依赖调用点 payload dict 构造序（本文件全部字面构造，快照测试可字节稳定）。

- **错误路径**：无（纯构造）；异常面只在调用方（序列化对象不可 JSON 时 orjson 抛 TypeError —— 本文件入参均为 str/bool/int，不会）。

- **序列化形状 vs 上游（opencode）差异**：
  - `message.part.snapshot` / `message.part.delta` 是 **sidecar 自造**事件名 —— 上游 `/global/event` 的 part 流事件是 `message.part.updated`（载荷含完整 part 结构，见 ocdroid 仓 `opencode-src/current` session/message 协议）；sidecar 把它重投影为省流 delta/snapshot 帧，非上游原样转发。
  - `message.removed` 事件名与上游一致，但载荷裁到最小 flat `{sessionID, messageID}`（:144-145 "mirrors the upstream flat-props shape" —— 上游还带更多字段）。
  - `server.connected` / `server.heartbeat` / `resync` 与控制面 curated SSE 同名同构；token 的 resync 多一个 `sessionID` 键（§16-D；控制面是 `{"reason"}` 单键，hub_types.py:321）。
  - 本模块**从不**产 `id:` 行 —— v4 的 `id: t:<sid>:<epoch>:<seq>` 前缀由 hub 层（`_deliver_logged`, tokenstream/hub.py:1437 / replay_wire.sse_id_line）在投递时包裹；故 frames.py 输出是版本无关字节。

- **疑问点（7）**：
  1. **:33-43 vs hub_types.py:105-107 `sse_frame` 双实现**：注释称 "Both copies share orjson so … byte-identical"，但无共享断言测试钉死两份实现（一改一漏即漂移，如未来 multi-line data / CRLF / retry 字段）。rg 未见专门的双实现字节一致性测试。
  2. **:19 双 STOP 哨兵**（同 subscriber 卡疑问 6）：与 hub_types.py:30 的 STOP 互不识别；`TokenSubscriber.put(控制面STOP)` 会 `len(object())` TypeError。
  3. **:65-81 `partEventRevision` 省略语义**：revision 未知（冷启/重连后）整键省略 → 同一 part 的投递历史可能"有无 revision 混排"，客户端必须把缺省当"未知"而非 0；契约侧是否明确该三态（有/无/回退）待与 v3/v4 契约 §7 对表。
  4. **:103-122 truncated 复用 `message.part.snapshot` 事件名**：按事件名分派的客户端若不检查 payload `truncated:true` 会把截断帧当全量快照（text 缺失）解析 —— 客户端规约是否强制按 (event,payload) 联合判别？
  5. **:125-126 resync 键序 `reason` 前 `sessionID` 后**：与 v3-contract §16-D 冻结形状是否逐字节对表过（快照测试有，但契约文档为准）——审计层面需交叉核对（E 卡契约对照阶段）。
  6. **:22-30 `_now_ms` 墙钟**：`time.time()` 非单调 —— 下游 `LivePart.last_delta_ms` 的 LRU 逐出序与 60s TTL 在 NTP 回拨时会失真（详见 models 卡）。
  7. **:137-152 `_message_removed_frame` 载荷最小化**：上游 message.removed 载荷更富（如 reason/timestamp）；sidecar 裁剪后 ocdroid 只能靠 `sessionID+messageID` 盲删本地态 —— CLIENT_CHANGES 是否已冻结该最小形状（是则无碍，记录在案）。

---

### src/oc_slimapi/sse/tokenstream/models.py（96 行）

- **职责**：token 累加器的数据模型（design §5.3/§5.4）：`LivePart`（在飞 text part 权威副本）、`DeltaAccumulator`（每 key flush 窗口影子缓冲）、`_TokenMetrics`（`sse.tokenStream.*` 计数器）。

- **对外符号**：
  - `@dataclass LivePart`（:13-30）：`chunks: list[str]`（:27, O(1) append, join-on-demand）、`byte_count: int`（:28, UTF-8 字节, Stage C `_reserve` 预算单位）、`ended: bool`（:29）、`last_delta_ms: int`（:30, 默认 `_now_ms()`；Stage-B TTL retiree + Stage-C LRU 逐出键）。
  - `@dataclass DeltaAccumulator`（:33-44）：`chunks`（:43）、`byte_count`（:44）。
    - `append(text)`（:46）：空串 no-op；append + `len(text.encode("utf-8"))` 计数（:48-51）。
    - `drain() -> str`（:53）：join + clear + `byte_count=0`（:59-65），跨窗口复用。
  - `@dataclass _TokenMetrics`（:68-96）：`orphan_deltas`（:87）、`flushed_frames_total`（:88）、`dropped_frames_total`（:89）、`truncated_snapshots_total`（:90）、`token_memory_limit_total`（:91）、S-3a 增补 `gzip_raw_bytes_total/gzip_compressed_bytes_total/flush_duration_ms_total(float)/flush_ticks_total`（:93-96）。`maxSubscriberQueueDepth` 特意不存（snapshot 时活算，:81-84）。

- **依赖 / 被依赖**：
  - 依赖：`.frames._now_ms`（:10）。
  - 被依赖：`tokenstream/hub.py:105`（LivePart/DeltaAccumulator/_TokenMetrics 全量导入 —— 状态容器与计数器宿主）；`tokenstream/subscriber.py:21`（`_TokenMetrics` 注解）；`tokenstream/__init__.py:6`、`sse/token_hub.py` shim；`routes/token_stream.py:224-225`（gzip 计数直写 `subscriber.metrics.gzip_*`）；测试 test_token_hub.py / test_token_subscriber_overflow.py（直接构造 _TokenMetrics/tight_sub）/ test_token_hub_flush.py / test_batch3_lifecycle.py。

- **状态 / 可变性**：纯可变 dataclass，无锁（单事件循环所有者 = TokenStreamHub）；不变式（byte_count == sum(UTF-8(chunks))、`_total_live_bytes/_total_pending_bytes` 聚合）全靠 hub 维护，模型自身不校验；无 `__slots__`（对象数受 TOKEN_LIVE_PARTS_MAX=32 / pending 聚合帽约束，开销可忽略）。

- **错误路径**：无（`drain` 空态返回 ""，`append` 空串 no-op）；所有预算/溢出决策在 hub（`_reserve`/`_evict_part_for_memory`/`_check_pending_budget`），模型层无失败面。

- **疑问点（6）**：
  1. **:30 `last_delta_ms` 墙钟做 LRU 键**：`_now_ms()` 基于 `time.time()`（frames.py:30）—— NTP 回拨会使 LRU 逐出（hub `_evict_part_for_memory`, hub.py:1754 "oldest by last_delta_ms"）与 60s TTL 判定失真；`time.monotonic()` 更稳（换 API 属实现自由，wire 不冻结）。
  2. **:87-91 docstring 口径偏窄**：`dropped_frames_total` 注释写 "oversized non-snapshot frames dropped"，但实际写入点含 oversized（subscriber.py:405）、handshake 溢出（:421）、runtime 溢出断连（:441）三类（subscriber.py:249-254 自称 single authoritative write site）—— models 注释与真实语义漂移。
  3. **:29 `ended` 的读者未在本卡范围核实**：置位点在 hub text-end 路径；读取点（若仅诊断/断言）不影响 wire —— 留给 E-01（hub 精读卡）交叉核对，此处挂账。
  4. **:95 `flush_duration_ms_total: float`**：名称带 Ms 但类型是 float 累加毫秒；snapshot 原样透出（subscriber.py:871）—— 消费方（metrics JSON）数值单位仅靠命名约定，文档未在 metrics 手册标注单位（待对 docs/manual）。
  5. **:59-61 `drain` 空分支冗余置零**：`if not self.chunks: self.byte_count = 0` —— byte_count 与 chunks 同生同灭，正常态不可能 chunks 空而 byte_count>0；防御无害但说明不变式无断言（一处 `assert` 可锁死模型契约，现为口头约定）。
  6. **:68 命名下划线却跨模块公开**：`_TokenMetrics` 私有名却被 registry/路由/测试广泛导入（metrics 直写点在 routes/token_stream.py:224-225 —— 绕过 hub 直接改计数器），"私有"约定名存实亡；S-3a 增补字段直写路由层若异常无防护（在 SSE 生成器内 +1，永不抛，可接受）。

---

## 附：跨卡交叉点（供后续卡片汇拢）

- 断连清理全链：客户端断 → ASGI send 抛/Cancelled → route generator finally（token_stream.py:303-307 / events.py:235-245）→ `unsubscribe`/`detach_events_subscriber` → 双账本空 → `token_hub.stop()` + `maybe_arm_grace_if_idle()` → 30s 后 `_remove_hub_after_grace` 拆 hub。溢出断连额外路径：`put` 溢出 → resync+STOP → generator break → 同一 finally。`session.deleted` → `terminate` → STOP → 同一 finally。三路汇聚于 unsubscribe 成员守卫，闭环成立（依赖 generator finally 必达 —— Starlette aclose 语义为外部前提）。
- 背压公平结论：per-sub 有界队列 + put_nowait + 溢出即断，flush loop 永不因单慢客户端阻塞；无跨 sub 优先级/加权 —— 帧序公平仅由 sorted-key 遍历（hub flush, hub.py:542）保证。
- 双实现/双哨兵（sse_frame ×2、STOP ×2、_now_ms ×2）是本组的结构性腐化风险点，建议审计结论单列。

<!-- ==== e1-09-replay ==== -->
# E1-09 精读卡片：SSE replay 数据层 + wire 层

> 审计基线：2026-08-20 工作树。引用格式 `src/oc_slimapi/...:行号`。两文件全文精读（非抽样）。

---

### src/oc_slimapi/sse/replay_log.py（598 行）

- **职责**：v4 SSE replay 的**纯数据结构层**（B3b-1；design-v4-sse-replay §3.4）。有界环形重放日志：全局域 `"g"` + 每订阅 sid 惰性创建的 `"t:<sid>"` 域；per-domain 单调 seq（从 1 起，tombstone 同样占 seq 保无洞）；进程级 epoch（16-hex 随机 boot nonce，仅相等比较）；跨上游丢失的 per-domain barrier 低水位（`seq <= watermark` → `reconnect_no_replay`，禁跨 barrier 补帧；barrier 是元数据，不受 count/bytes/TTL 驱逐）。分类 ③ epoch / ④ barrier→window→gap 在 `replay()` 内按冻结短路序实现；①②语法/端点匹配不在本层（docstring `replay_log.py:18-25`）。asyncio 单线程无锁模型（`replay_log.py:33-35`）。

- **对外符号**（`__all__` `replay_log.py:50-69`）：
  - 模块级：
    - `GLOBAL_DOMAIN = "g"`（78）— 全局域 key；token 域恒 `"t:"` 前缀故不可能与之相撞（76-78 注释）。
    - `FRAME_KIND_BUSINESS / FRAME_KIND_TOMBSTONE`（83-84）— 帧种类；tombstone 仍带 `id:` 并占 seq。
    - `RESYNC_EPOCH_CHANGED / RESYNC_REPLAY_EXPIRED / RESYNC_REPLAY_GAP / RESYNC_RECONNECT_NO_REPLAY`（88-91）— log 层可裁决的四个冻结 resync reason（§7.2）。
    - `DEFAULT_REPLAY_MAX_COUNT=2048 / DEFAULT_REPLAY_MAX_BYTES=64MiB / DEFAULT_REPLAY_TTL_S=900.0`（93-95）— 三维默认界。
    - `new_epoch()`（101-108）— `secrets.token_hex(8)` 产 16 位小写 hex 进程 nonce。
    - `token_domain(sid)`（111-113）— `f"t:{sid}"`；对 sid 无任何校验。
    - `_default_size_of(payload)`（116-129）— bytes bound 计费：bytes/str 取 len，可 JSON 序列化取序列化长，其余计 0；可经 ctor 注入。
  - dataclass（均 frozen+slots）：
    - `ReplayEntry`（136-158）— 一条保留帧：`domain/seq/payload/kind/appended_at/size/order`（order=进程级 append 计数，用于跨域 bytes 驱逐排序）；property `is_tombstone`（156-158）。
    - `ReplayFrames`（161-170）— 成功结果：严格递增**连续**的 entries 元组；空元组=已追平（非 resync）。
    - `ReplayResync`（173-182）— 服务端决定 resync；reason ∈ 四冻结值。
    - `ReplayIgnoreReset`（185-197）— 忽略游标按首连处理（future cursor）；携带 `seq`。
    - `ReplayOutcome`（198）— 上述三者 Union。
  - `_DomainState`（205-231，内部）— 单域状态：`entries(deque)/next_seq/last_seq/bytes/barrier_watermark/last_touch`；property `window_start`（227-230）。
  - `ReplayLog`（237-598）：
    - `__init__`（260-302）— 校验 epoch 格式/max_count≥1/max_bytes≥1/ttl_s 有限且>0（nan/inf fail-closed，281-289 rev-gate MAJOR-1）；初始化 outcome Counter、可注入 clock/size_of。
    - `has_domain`（306）/`domain_keys`（309）/`domain_count`（312）/`frame_count`（315）/`domain_frame_count`（318）— 只读盘点。
    - `last_seq(domain)`（322-325）— 域最大已发布 seq（0=未创建）。
    - `window_start(domain)`（327-331）— 窗口下界（最老保留 seq；None=空/未知域）。
    - `barrier_watermark(domain)`（333-336）— 当前 barrier 水位或 None。
    - `metrics_snapshot()`（338-347）— 展平 outcome 计数 + domains/frames/bytes/barriers 给 /slimapi/metrics。
    - `append(domain, payload, *, kind)`（351-395）— 发布一帧：close 后 RuntimeError；惰性建域；先 TTL 驱逐头部分配 seq（tombstone 同占 seq，REPLAY-012），再 count/bytes 驱逐；**记录 published 而非 delivered**（360-363：背压溢出帧仍入日志）。
    - `replay(domain, after_seq, epoch)`（399-468）— 分类重连游标，冻结短路序：③ epoch 不等→`epoch_changed`（423-425，dominates）→ ④a `after_seq<=watermark`→`reconnect_no_replay`（434-436，`<=` 含水位帧本身，rev-5 勘误）→ ④b future cursor→`ReplayIgnoreReset`（440-442）→ ④c 窗口空/expired（448-458；`after_seq==last` 为 up_to_date 空帧）→ ④d 连续性防御扫描→`replay_gap`（459-466，设计上不可达）→ 否则返回连续 `ReplayFrames`（467-468）。每个出口都记 outcome 计数。
    - `write_barrier(domain=None)`（472-493）— 写上游丢失低水位：None=全域（含离线 token 域）；watermark=写时 last_seq，单调不降（491）；barrier 后新建的域无水位。
    - `recycle_domain(domain)`（495-511）— 清帧清 bytes，**保留 seq 计数与 barrier**（REPLAY-018 fail-safe：回收域旧 cursor 永不退化为首连语义）；返回域是否存在。
    - `sweep(now=None)`（513-539）— 全域 TTL 头部驱逐 + barrier GC（条件 `entries[0].seq > watermark+1`，537，rev-gate R5 off-by-one 修正：窗口下界越过 W+1 后 cursor W 自带 replay_expired，barrier 才真冗余；空窗口保 barrier——cursor==watermark==last 必须仍被拦截）。
    - `closed` property（541-544）/ `close()`（546-555）— 幂等关停；置 _closed + 清域；append 此后 fail loud。
    - `_ttl_evict_head`（559-566）— 严格大于 ttl_s 才驱逐（恰 ttl_s 年龄仍可 replay）。
    - `_evict_for_count`（568-570）— 本域 count ring（保最新 ≥1 帧）。
    - `_evict_for_bytes`（572-592）— 进程级 bytes：删全局最老（min head order）直到达标或只剩 1 帧（单帧超预算仍保留——不丢刚接受的帧）。
    - `_drop_head`（594-598）— popleft + 双记账（域 bytes/总 bytes）扣减。

- **依赖**：标准库 `math/re/secrets/time/collections.(Counter,deque)/dataclasses/typing` + 第三方 `orjson`（仅 `_default_size_of` 序列化计费）。无本仓内部依赖。

- **被依赖**（rg 反查）：
  - `src/oc_slimapi/app.py:34`（import `ReplayLog, new_epoch`）；`app.py:425-455` lifespan 构造（settings.replay_max_count / replay_max_bytes_kb*1024 / replay_ttl_s）+ sweep 任务；`app.py:490` `hubs.set_replay_log`；`app.py:540` `TokenStreamHub(replay_log=...)`。
  - `src/oc_slimapi/sse/global_hub.py:48-54,76-96,536-545`（持有/注入）；`global_hub.py:413` 上游首次确认丢失时 `write_barrier()`（全域）；`global_hub.py:552-570` `_replay_publish` append(GLOBAL_DOMAIN) + `sse_id_line`。
  - `src/oc_slimapi/sse/tokenstream/hub.py:85-91,252-271`；`hub.py:1375-1394` `_replay_publish_token`（在 no-subscriber 早退**之前** append，REPLAY-007）；`hub.py:1405-1424` `_write_replay_barrier(sid)`（idle retire / memory eviction / session deletion 三处状态失效源无条件单域写 barrier）。
  - `src/oc_slimapi/sse/registry.py:80-138`（replay_log 经 HubRegistry 转发）。
  - `src/oc_slimapi/routes/events.py:8-15,103-206`（classify/meta/帧补发消费）；`routes/token_stream.py:53-60,144-277`；`routes/metrics.py:97-102`（metrics_snapshot + epoch）；`src/oc_slimapi/sse/replay_wire.py:38-48`（见下卡）。
  - 配置：`src/oc_slimapi/config.py:662-671`（OC_SLIMAPI_REPLAY_COUNT/BYTES_KB/TTL_S）+ `config.py:1113-1128` fail-closed 校验。
  - 测试：`tests/test_replay_log.py`（数据层专测）、`tests/test_sse_replay_wire.py`、`tests/test_metrics_replay_block.py`。

- **状态/可变性**：单例（app.state.replay_log，进程生命周期）。可变状态全在 `ReplayLog`：`_domains: dict[str,_DomainState]`（域壳进程内**永不删除**，253-257——删除会使 next_seq 回归造成 ID 回退；真正 GC=进程重启换 epoch）、`total_bytes`（进程级 bytes 记账，**可长期超 max_bytes**，见疑点 7）、`_order`（跨域驱逐排序计数）、`_closed`。`_DomainState.entries` 只从头部弹出（head-only 驱逐不变式 → 窗口恒连续，205-212 docstring）。`replay_outcomes_total: Counter` 只增。`clock` 默认 `time.monotonic`（TTL 与壁钟无关，重启自然换 epoch 故无需持久化时间）。barrier_watermark 只在 `write_barrier` 单调上调、`sweep` 中可清 None；**不受任何帧驱逐影响**（元数据）。

- **错误路径**：
  - ctor `ValueError`：epoch 非 16-hex 小写（272-276）、max_count<1（277）、max_bytes<1（279）、ttl_s≤0 或非有限（281-289，nan 绕过 `<=0` 的坑已堵）。
  - `append`：`RuntimeError`（closed，365-366）；`ValueError`（domain 非非空 str，367-368）。生产调用方均 try/except 降级为 id-less fanout + warning（global_hub.py:565-569 / tokenstream hub.py:1390-1394）。
  - `replay`：`ValueError`（after_seq 非非负 int / 是 bool，421-422）。其余一切"错误"以返回值表达（ReplayResync×4 / ReplayIgnoreReset）。
  - `close` 幂等；close 后 replay/sweep/write_barrier **不报错**（见疑点 2）。

- **疑问点**（20 条）：
  1. **epoch 唯一性（boot nonce 冲突）**（101-108,270-276）：epoch=64-bit 随机；两进程碰撞（单次 ~2⁻⁶⁴，生日界 ~2³² 次重启）时 ③ 相等检查失效——旧 cursor 落入窗口判定：seq 超前→`ReplayIgnoreReset` **静默首连**（185-196），seq 恰在窗内→跨代际补发新进程帧。无 pid/启动时间复合校验。设计接受（§7.1 frozen），残余风险记录。
  2. **close() 后 replay() 不设防**（546-555 vs 399-468）：`_closed` 只挡 append（365）；close 清域后 in-flight replay 得到「空域」语义（epoch 未变：cursor>0→IgnoreReset，==0→up_to_date 空帧）而非显式错误——与 append 的 fail-loud 不对称；shutdown 竞态下新分类结果误导。
  3. **barrier GC 只在 sweep**（513-539,尤其 537）：count/bytes 驱逐已使 `entries[0].seq > watermark+1` 后、下次 sweep 前的窗口内，cursor≤watermark 报 `reconnect_no_replay` 而非 `replay_expired`——reason 随 sweep 时序漂移（客户端动作等价：都 HTTP 全量对齐，wire 无害；语义/统计口径有差异）。
  4. **空窗口 barrier 永久保留**（536 条件 `state.entries and ...`；526-529 注释自陈）：空窗 + cursor<last 时 replay_expired 本就自带拦截，barrier 冗余却保留（直到有新帧把下界推过 W+2 或进程重启）；叠加 recycle_domain 保 barrier（495-511）→ barrier 生命周期远超必要性。保守正确，无误但需知晓。
  5. **write_barrier 指定域不存在时静默 no-op**（488-489 `targets=[]`）：per-sid invalidation（tokenstream/hub.py:1420）若早于该域首帧，barrier 落空；此后 cursor=0 重连不受拦截——首连语义本无旧状态可失效，语义无害，但 hub 侧调用顺序无保证、本层不设防。
  6. **read path 有副作用**（428-429）：replay() 先做该域 TTL 驱逐再分类——重连读取本身会推进过期驱逐，「判定结果依赖调用时刻 TTL 状态」；测试必须注入 clock 才确定。
  7. **单帧超预算长期突破 bytes bound**（572-592 `frames > 1`，577 先查 `<=`）：只剩 1 帧时即使超预算也停——`total_bytes` 可长期 > max_bytes，`metrics_snapshot()["bytes"]`（343）会显示超支；属设计裕度（"never drops the frame it just accepted"），运维告警阈值需知晓。另：bytes 驱逐可删**别的域**的头（跨域副作用，572-592）——全局内存压力下 token 域补发窗口被「全局最老帧」驱逐侵蚀，跨域影响面值得审计备案。
  8. **_evict_for_bytes 复杂度**（580-592）：每删一帧全量扫所有域 head 取 min order，O(域数×驱逐数)；巨帧 append 触发批量驱逐时最坏 O(N²)。无堆结构。性能疑点非正确性。
  9. **_default_size_of 0 计费漏洞**（124-129）：非 bytes/str 且不可 JSON 序列化 payload 计 0——bytes 维度对其无约束（count/TTL 兜底）。生产 payload 均为 bytes 帧（两 hub 调用点），len() 精确；仅非常规调用方受影响。
  10. **ReplayIgnoreReset 静默性**（185-196,437-442）：future cursor（含「域从未创建 + cursor>0」路径，437-438 注释）不回 resync 不补帧——客户端对齐完全依赖其主动采纳 meta.seqBase（routes 在 handler 冻结 seqBase）；若客户端忽略游标倒挂，服务端无再纠正信号。契约义务全在客户端侧。
  11. **未创建域 cursor=0 记为 up_to_date**（448-451）：state=None → last=0、entries=()，after_seq==0 与「已创建且恰好追平」不可区分；metrics 词表无 unknown_domain 维度（routes/metrics.py:97-102 直接展平），统计口径盲区。
  12. **outcome 计数键无常量冻结**（297; 424/435/441/450/452/457/465/467）：`RESYNC_*` 只覆盖 resync reason；`"ignore_reset"/"up_to_date"/"replayed"` 等为裸字符串，仅 tests/test_metrics_replay_block.py:41 词表冻结——键名漂移风险。
  13. **last_touch 写而不读**（216,225,392,430,493,510）：全仓 rg 无任何读取点——死状态（或预留「按空闲回收域」用，现回收策略在 replay_wire sweep 以帧数==0 为准，见下卡疑点 6）。
  14. **并发正确性依赖 routes 顺序不变量**（33-35 无锁声明成立的前提）：本模块方法内无 await，单 loop 下 append/replay/sweep 各自原子 ✓；但「不重不漏」还需 handler 中 **classify(T0) 先于 subscribe(T1)**（events.py:110-114 / token_stream.py:151-155 注释）：replay 覆盖 ≤last@T0，queue 覆盖 attach 后发布帧。该不变量不在本模块强制——若未来把 classify 挪进 generator 首帧期即出现 gap/dup。API 无防御。
  15. **环形覆盖 × 在途重连（任务点名）**（443-447）：`replay()` 返回前 `tuple(...)` 即时快照拷贝 deque——此后 popleft 驱逐（count/bytes/TTL/sweep）不影响已返回 entries（frozen dataclass + payload 引用保活）→ 在途重连可安全逐帧 yield 已被覆盖帧。正确性关键一行，成立 ✓（payload 生命周期由 hub 侧保证不再复用 buffer，本层只持引用）。
  16. **「背压溢出帧仍入日志」×「窗口无空洞补发」组合（任务点名）**（360-363; tokenstream/hub.py:1378-1394 在 no-subscriber 早退前 append）：背压/离线期发布的帧照样占 seq 可补发；配合 v4 silent-STOP（域外 reason 不发 resync，hub_types.py:311-317 / subscriber.py:444-460），客户端恢复路径唯一 = Last-Event-ID 重连 → 本层窗口判定。组合闭环前提 = 窗口未过期且无 barrier；若溢出期间该 sid 恰逢 idle/evict/deleted（tokenstream/hub.py:1420 写 barrier），`after_seq<=watermark` → `reconnect_no_replay`（434 先于窗口判定）——补发让位于屏障，组合语义一致（屏障优先）✓。
  17. **resync 四值短路序自洽性（任务点名）**（399-468）：③ epoch（423 dominates）→④a barrier（434）→④b future（440）→④c expired（448-458）→④d gap（459-466 防御）。两处顺序自洽验证：barrier 先于 future——watermark≤last 恒成立（491 取 last_seq 且 last 只增），`after_seq<=watermark≤last` 必非 future ✓；`entries[0].seq != after_seq+1`→expired（454-458）先于 gap 扫描，gap 分支靠 head-only 驱逐不变式保持不可达，若破则 fail as `replay_gap` 不静默服务带洞 replay ✓。
  18. **`after_seq <= watermark` 边界**（434，rev-5 勘误 `<=` 而非 `<`）：水位帧自身（seq==watermark）在丢失前已发布——该帧是否真送达过客户端不可知，按「已拦截至水位」处理 ✓ 冻结语义。
  19. **epoch 参数宽容**（399,423）：replay() 对传入 epoch 只做相等比较、不校验 16-hex 语法（parse 层已过滤）；`None` epoch 会命中 `epoch_changed` 计数——公开 API 语义上可接受，未来直调方需知。
  20. **recycle_domain 实际语义（任务点名「被覆盖帧×在途重连」邻接）**（495-511）：唯一生产调用方是 replay_wire sweep（对帧数已 0 的域）→ `while state.entries` 体不执行，效果仅刷新 last_touch（无读者，疑点 13）+返回 True——在当前 wiring 下近乎 no-op；域壳本就永不删除（253-257）。设计文档称「回收策略」，实现是幂等清空 + 保壳保 seq 保 barrier。

---

### src/oc_slimapi/sse/replay_wire.py（282 行）

- **职责**：v4 SSE replay 的 **wire 层**（B3b-2；design-v4-sse-replay §4 / v4-contract §7）：`id:` 行生成（`g:<epoch>:<seq>` / `t:<sid>:<epoch>:<seq>`——统一为 `<domain>:<epoch>:<seq>`）；Last-Event-ID 解析 + 分类 ①语法/②端点-sid 匹配（③④委托 `ReplayLog.replay` 保持冻结短路序）；v4 `slimapi.meta` 加性扩展字段（capabilities/epoch/seqBase，meta 帧本身不带 id）；周期维护循环 `replay_sweep_loop`（TTL GC + barrier GC + 空域 recycle，app.py wiring）；冻结四值 resync reason 域 `V4_RESYNC_REASONS`（生产 allowlist，非测试 oracle）。①②违规一律 **ignore+reset**（静默首连，不发 resync——客户端协议违规不是服务端状态变化，20-29 docstring）。

- **对外符号**（`__all__` `replay_wire.py:50-59`）：
  - `V4_RESYNC_REASONS: frozenset`（61-77）— rev-gate R3 BLOCKER-1 冻结 v4 `resync.reason` 值域（四值，从 replay_log 导入同源）；域外 legacy reason（subscriber_backpressure/token_memory_limit/session_idle/session_deleted…）在 v4 走静默 STOP 路线（断连本身是信号）。
  - `_EPOCH_RE`（82）/`_SEQ_RE`（83）— §7.1 语法：epoch 恰 16 小写 hex；seq 十进制（容忍前导零——值域是整数，79-81 注释）。
  - `_GLOBAL_SEGMENTS = 3`（89）— global id 恰 3 冒号段；token ≥4 段，sid 取 label 与尾部 epoch/seq 对之间的一切（rsplit 语义，86-88 注释：sid 含冒号仍可 round-trip）。
  - `META_CAPABILITY_KEYS: dict`（91-96）— v4 meta 帧能力广告 `{"sseReplay": True}`；常量 dict 单源供 meta 帧与 versions 端点共用防漂移。
  - `DEFAULT_SWEEP_INTERVAL_S = 60.0`（98-101）— 维护节拍（远低于 15min TTL 使空闲域收敛）；**不在 `__all__`**。
  - `sse_id_line(domain, epoch, seq) -> bytes`（104-113）— 生成 `id: {domain}:{epoch}:{seq}\n`（含尾换行）ASCII 编码；domain 即 log 域 key（"g" / "t:<sid>"）恰为 wire id 前缀段。
  - `frame_with_id(frame, domain, epoch, seq)`（116-123）— 已序列化 SSE 帧块前缀加 id 行；不重序列化帧本体（字节同一性）。
  - `parse_last_event_id(header, *, token_sid=None) -> (epoch,seq)|None`（126-166）— 分类 ①②：global 端点（token_sid=None）只收恰 3 段 `g:` 开头（151；`t:` id 到 /events 是跨端点违规，不管后续）；token 端点只收 ≥4 段 `t:` 且重组 sid == 路径 sid（157-162）；epoch/seq 正则终检（164）。**任何违规→None**（调用方按 ignore+reset 处理）。
  - `classify_reconnect(header, replay, *, domain, token_sid=None)`（169-209）— 完整 ①②③④：无头/①②违规→None（首连语义）；③④委托 `replay.replay()`（短路序与 outcome 计数单点保持，196-198 注释）。
  - `meta_v4_extension(epoch, seq_base) -> dict`（212-226）— B3b-4 加性三键 `capabilities/epoch/seqBase`；seqBase=连接时该域最大已发布 seq（首连后首个 id 帧= seqBase+1）；meta 帧自身无 id（§7.0 终裁②）。
  - `replay_sweep_loop(replay, *, interval_s, stop_event)`（229-282）— 周期维护协程：每 tick `sweep()`（TTL+barrier GC）→ 对帧数 0 的非全局域 `recycle_domain`（保 seq 壳/barrier）；best-effort（异常 warning 继续；RuntimeError=closed 竞态静默退出）；stop_event 唤醒即返回。

- **依赖**：标准库 `asyncio/re/typing`；内部 `from .replay_log import GLOBAL_DOMAIN, ReplayOutcome, RESYNC_*×4`（38-45）+ TYPE_CHECKING `ReplayLog`（47-48）；函数内延迟 `from ..logging_config import get_logger`（255，避启动成本/循环依赖）。

- **被依赖**（rg 反查）：
  - `src/oc_slimapi/app.py:35,447-455`（replay_sweep_loop lifespan wiring，`interval_s=_REPLAY_SWEEP_INTERVAL_S` + stop_event；LIFO 保证 sweep 先于 replay_log.close 停）。
  - `src/oc_slimapi/routes/events.py:15,115-118`（classify_reconnect 全局域）；`events.py:151-153`（meta_v4_extension）；`events.py:201-203`（frame_with_id 补发）。
  - `src/oc_slimapi/routes/token_stream.py:60,155-158`（classify + token_sid）；`token_stream.py:191-195`（meta seqBase=last_seq(token_domain(sid))）；`token_stream.py:275-276`（frame_with_id）。
  - `src/oc_slimapi/sse/global_hub.py:49,570`（sse_id_line 全局域 id 行）。
  - `src/oc_slimapi/sse/tokenstream/hub.py:91,1394`（sse_id_line token 域）；`hub.py:85-91`（imports）。
  - `src/oc_slimapi/sse/hub_types.py:25,311-317` 与 `src/oc_slimapi/sse/tokenstream/subscriber.py:20,444-489`（V4_RESYNC_REASONS 门控：v4 域外 reason → 仅 STOP 不发 resync）。
  - `src/oc_slimapi/routes/versions.py:53,87-95`（`**META_CAPABILITY_KEYS` 把 sseReplay 广告进 /slimapi/versions）。
  - 测试：`tests/test_sse_replay_wire.py`（导入同批常量做 oracle）。

- **状态/可变性**：**本模块完全无状态**——全部纯函数（sse_id_line/frame_with_id/parse/classify/meta_v4_extension）+ 一个协程（loop 状态仅局部）。所有可变状态在委托的 `ReplayLog`（见上卡）。`META_CAPABILITY_KEYS` 是模块级**可变 dict**（疑点 9）；`DEFAULT_SWEEP_INTERVAL_S` 与 app.py:91 `_REPLAY_SWEEP_INTERVAL_S` 双定义（疑点 10）。

- **错误路径**：
  - `parse_last_event_id`：一切①②违规→`None`（无异常；除疑点 2 的 int() 超长串边界）。
  - `classify_reconnect`：无头→None；①②→None；③④异常透传 replay 的 `ValueError`（after_seq 非法——parse 产物恒为非负 int，实际不可达）。
  - `sse_id_line`：`.encode("ascii")` 对非 ASCII sid 抛 `UnicodeEncodeError`（113）——生产两调用点均在 hub 的 try/except 内降级为 id-less fanout + warning（global_hub.py:565-569 / tokenstream hub.py:1390-1394）。
  - `replay_sweep_loop`：`asyncio.TimeoutError`→继续下一 tick（264-265）；`RuntimeError`（closed 竞态）→静默 return（278-280）；其他 `Exception`→`logger.warning` 继续（281-282）；closed 先查双保险（269-271）。

- **疑问点**（14 条）：
  1. **id 生成/解析对称性总检（任务点名）**（104-113 vs 145-166）：global 严格对称（生成恰 3 段 / 解析恰 3 段+`g` 首段，151）✓；token 生成 `t:<sid>:<epoch>:<seq>`，解析 ≥4 段+`t` 首段+`sid=":".join(parts[1:-2])`（157-159）——rsplit 固定尾部两段定界，**sid 含任意冒号 round-trip 无歧义**（测试 `test_sse_replay_wire.py:499`：`t:a:b:<epoch>:5` ↔ sid="a:b"）✓。**单向宽容不对称（方向安全）**：解析容忍 seq 前导零（83+166 `int()`）与 cursor=0，生成端永不产这两种形式（seq≥1 无前导零）——不会构成 round-trip 冲突。拒绝面完备：大写 hex、非数字 seq、段数错、错 label、跨端点、跨 sid 全→None ✓。
  2. **`int(seq_text)` 超长数字串抛 ValueError**（166）：`_SEQ_RE` 限定纯数字但**不限长度**；Python ≥3.11（pyproject `requires-python = ">=3.11"`）默认 int 最大 4300 位——`int("9"*5000)` 抛 `ValueError`，parse 未 catch → classify 冒泡（209）→ route handler 无此异常分支 → 潜在 500。畸形 header 本应 ignore+reset（139-141 语义），此处漏防（唯一触发面：恶意/损坏的超长 Last-Event-ID）。
  3. **空 sid 与非 ASCII sid 不设防**（159-162 / 113）：`token_domain("")="t:"`（replay_log.py:111-113 无校验）→ id `t::<epoch>:<seq>` 在 token_sid="" 时被接受（依赖路由层路径参数 sid 非空）；`sse_id_line` 的 ascii 编码对非 ASCII sid 抛错，hub 侧降级后 **v4 wire 出现无 id 业务帧**（§7.1 一致性破口，与 global_hub.py:553-554 "degrades to id-less fan-out" 有意降级同源）——降级可见性仅一条 warning。
  4. **classify_reconnect 的 domain 与 token_sid 无耦合校验**（169-209）：domain="g" 配 token_sid="s1" 会按 token 语法 parse 而查 "g" 域；消费侧 frame_with_id 也由 routes 重传 domain（events.py:201-203 / token_stream.py:275-276）而不用 `entry.domain`（ReplayEntry 自带域字段）——两处一致性纯靠调用约定，type-level 不防错配。
  5. **barrier 水位原子性/并发重连竞态（任务点名）**：本模块纯函数无状态，分类原子性= `ReplayLog.replay` 单 loop 原子（见上卡疑点 14/15）+ routes 的 classify(T0)→subscribe(T1) 顺序不变量 + meta seqBase 同在 handler 冻结（events.py:149-155 注释：防 T0/T1 间发布帧使 seqBase 超前于 replay plan）→ 客户端序列 meta→replay(≤last@T0)→queue(>last@T0) 严格递增，无重无漏 ✓。竞态残余面：**ReplayIgnoreReset 分支** seqBase=last ≪ 客户端 cursor，靠客户端主动倒退对齐（契约义务，服务端无强制）；若客户端不采纳将永久超前。
  6. **sweep 的 recycle 实效是 no-op**（273-277）：对 `domain_frame_count==0` 的域调 `recycle_domain`——帧已空（while 不执行）、域壳永不删、seq/barrier 本就保留 → 实际效果仅刷新 last_touch（无读者，上卡疑点 13）。「expired-domain recycle policy」在实现层是文档性行为；GLOBAL_DOMAIN 跳过（274-275）正确（共享序列）。
  7. **sweep loop 时序**（258-267）：先 sleep 后 sweep——首 tick 延迟一个 interval（60s 内过期帧不清理，可接受）；stop_event 唤醒后**不做最后一次 sweep** 即 return（266-267）——残留帧由 close() 清（app.py:433-441 LIFO 顺序保证）✓；`except asyncio.TimeoutError` 在 3.11+ 与内建 TimeoutError 合一，写法可达 ✓。
  8. **V4_RESYNC_REASONS 门控覆盖面**（61-77）：hub 侧两处门控（hub_types.py:311-317 / subscriber.py:444-489）把域外 reason 转 silent-STOP；但 **route 层直接 yield resync**（events.py:196-198 / token_stream.py:270-272）不经门控——安全仅因 replay_plan 的 reason 来自 log 层封闭四值 + v3 fallback 亦为 `RESYNC_RECONNECT_NO_REPLAY`（events.py:124-126 / token_stream.py:164-167）；无运行时 assert，log 层若加第五 reason 会未经门控直接上 wire——值域封闭性靠字面量纪律。
  9. **META_CAPABILITY_KEYS 可变性**（96）：普通 dict 而非 MappingProxy/frozenset——注释（91-95）自称为防漂移单源，防的是「两处字面量」漂移（versions.py:95 与 meta 共用 ✓），不防运行时篡改；低风险。
  10. **sweep 间隔双定义**（101 vs app.py:91 `_REPLAY_SWEEP_INTERVAL_S = 60.0`）：app.py 显式传参不用本模块默认值——两处 60.0 各自维护；且 `DEFAULT_SWEEP_INTERVAL_S` 不在 `__all__`（50-59），星号导入不可见。当前值一致，属 drift 隐患。
  11. **无头/空串双检冗余**（200-201 vs 143）：classify 与 parse 各查一次 `if not header`——空 Last-Event-ID 头等价无头（first-connect），测试 492-493 冻结；冗余无害。
  12. **meta 字段顺序契约靠调用方**（212-226）：docstring 承诺顺序 `subscriberId, tokens, capabilities, epoch, seqBase`——本函数只产后三键，顺序由 routes 的 `dict.update`（py3.7+ 有序 dict）保证 ✓；「首帧恰为 seqBase+1」承诺依赖 handler 冻结时序（疑点 5）。
  13. **sweep loop 持续性异常无退避**（268-282）：非 RuntimeError 持续抛（如注入 clock 故障）→ 每 60s 一条 warning，无退避/升级/自终止——故障被 best-effort 吞掉。
  14. **token_sid 参数语义重载**（127,146-163）：None=global 端点、非 None=token 端点——用参数存在性区分端点而非显式枚举；错配调用 `token_sid=None` + domain="t:x" 会按 global 语法解析 token id → 段数 4≠3 拒绝（碰巧 fail-safe）✓，反向错配见疑点 4。

---

### 附：两文件组合语义小结（审计关注项交叉确认）

- **短路序全链**：①语法+②端点/sid（replay_wire `parse_last_event_id`）→③epoch（replay_log:423，dominates）→④a barrier（434）→④b future（440）→④c expired（448-458）→④d gap（459-466 防御不可达）。顺序两处自洽验证通过（barrier≤last 恒成立故先于 future 无冲突；expired 先于 gap 且 gap 靠 head-only 不变式兜底）。
- **barrier 双写路径**：进程级上游丢失 `global_hub.py:413 write_barrier()`（全域含离线 token 域）+ per-sid 状态失效 `tokenstream/hub.py:1420 write_barrier(token_domain(sid))`；水位都取 last_seq 且单调（replay_log:491）。`token_hub.on_upstream_reconnect`（hub.py:2124-2185）清 accumulator 状态但**不**写 barrier（全域 barrier 已由 global_hub 先写）。
- **背压溢出帧入日志 × 窗口补发 × barrier** 三者组合：溢出帧占 seq 可补发（published 语义）；若溢出期间发生 invalidation（barrier 落位），cursor≤watermark 一律 `reconnect_no_replay` → 客户端 HTTP 全量对齐（v4-contract §7.2 冻结恢复路径）——屏障优先于补发，语义闭环一致。
- **在途重连 vs 环形覆盖**：`replay()` 的 tuple 快照（replay_log:444）+ frozen entry + handler 冻结 plan/meta（routes T0<T1）→ 已覆盖帧可安全补发，无「撤回已承诺帧」路径。

<!-- ==== e1-02-messages ==== -->
# E1-02 精读卡片 — src/oc_slimapi/routes/messages.py（1643 行）

> 只读审计产物（2026-08-20）。全部行号基于当前 worktree；引用格式 `src/oc_slimapi/routes/messages.py:行号`（下文省略为 `:行号`）。
> 交叉验证文件：`src/oc_slimapi/{transform,singleflight,skeleton,etag,envelope,gzip_util,errors,directory,selector,readiness,upstream_errors,traffic}.py`、`src/oc_slimapi/routes/_catalog_common.py`、`src/oc_slimapi/app.py`、`src/oc_slimapi/config.py`、`tests/test_messages_merged.py`。

## 职责（一句）

`/slimapi/messages/{sid}` 前缀下的四个消息消费路由：skeleton 列表投影（含 `mode=merged` 服务端合并展开、上游 list GET 单飞合并、ETag/304）、单消息 `/full/{mid}` 全量投影（strip LSP diagnostics、与 merged fan-out 同 key 单飞）、以及两个 design-expand 碎片展开端点（12 类白名单 category，与 `/full` 共享单飞 GET）。

## 对部符号清单（名字 + 行号 + 职责）

### 模块级对象

| 符号 | 行号 | 职责 |
|---|---|---|
| `router` | :40 | `APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])`，四个 GET 路由的挂载点 |
| `TRANSFORM_RETRY_AFTER_SECONDS = 2` | :44 | admission 超时 503 的固定 `Retry-After`（body 字段 + 头双写，见 :278-284） |
| `_V4_EXPAND_FEATURE = "messages.expand.v4"` | :55 | §3.3 readiness 门控 feature id（expandRefs href 是否用 `?v=4`） |
| `_REL_PARAM_RE` | :168 | RFC 5988 `rel=` link-param 的编译正则（IGNORECASE，防 `title="rel=next"` 误匹配） |
| `_PLACEHOLDER_PART_ID_PREFIX = "thin_placeholder_"` | :361 | skeleton 塌缩占位 part 的 id 前缀（对 `skeleton.py:618` 的字符串复制，见疑问点 10） |
| `_DEGRADED = object()` | :365 | merged fan-out 单项失败/超预算的哨兵（该消息保留 skeleton 投影） |
| `_EXPAND_CATEGORIES` / `_EXPAND_CATEGORIES_SET` | :1271-1272 | **mid-file import**，从 `traffic.py` 引入 12 类冻结 category（单一事实源） |
| `_EXPAND_MESSAGE_LEVEL_CATEGORIES` | :1275 | `frozenset({"info_summary_diffs"})` — 唯一 message 级 category |
| `_EXPAND_APPLICABLE_TYPES` | :1278-1290 | part 级 category → 适用 part.type 元组（如 `part_state_output` → `("tool",)`、`part_snapshot` → `("step-start","step-finish")`） |
| `_EXPAND_EXTRACTORS` | :1471-1484 | category → extractor 函数表（12 项，与 `_EXPAND_APPLICABLE_TYPES` 键一一对应） |

### 类

| 符号 | 行号 | 职责 |
|---|---|---|
| `_CapExceeded(Exception)` | :368-387 | 内部哨兵异常：单飞 factory 内 read-cap 截断（`None` 翻译而来），携带 `cap`；使 flight entry 被 drop 而非以 `None` 结果 grace 保留，joiner（更大 cap）可 re-lead 重试 |

### 函数（非路由）

| 符号 | 行号 | 职责 |
|---|---|---|
| `_expand_wire_view(scope)` | :58-64 | §3.3 门控下的 href view：selector view==4 且 `messages.expand.v4 ∈ readiness.SATISFIED` → 4，否则折回 3（动态读模块全局，支持 monkeypatch） |
| `_created_sort_key(msg)` | :77-92 | 排序键 `info.time.created` ASC；畸形行默认 0（排最前，见疑问点 15） |
| `_parse_sort_project(body, *, limits, sid, wire_view)` | :95-133 | worker 入口：orjson 解析 + 非列表守卫（ValueError→503）+ 按 created ASC 排序 + skeleton 投影（不序列化）；merged 路径需要 dict 中间态 |
| `_project_list_sorted_and_pack(...)` | :136-165 | worker 入口：`_parse_sort_project` + `orjson.dumps` → identity bytes（pre-gzip；`accept_encoding` 仅为调用点对称保留） |
| `_link_rel_tokens(attrs)` | :179-190 | Link 属性串里 `rel` 值 → 小写 token 列表（RFC 5988 §3 多 token / 大小写不敏感） |
| `_extract_before_verbatim(query)` | :193-218 | 从原始 query 切出 `before=` 值，**绝不 percent-decode / `+`→space**（保护上游 opaque base64url cursor 往返）；`?before` 无值 → None（fail-safe） |
| `_parse_link_next_cursor(link_header)` | :221-265 | RFC 5988 Link 解析：取 `rel~next` URL query 中的 `before` cursor 原样返回（我们自己的 `nextCursor` 通道；见疑问点 8 的逗号切分） |
| `_busy_response(accept_encoding)` | :268-284 | 503 `transform_busy` + `Retry-After: 2`（body 含 `retry_after` 字段 :280-282，头 :283 后置；经 `error_response` 走 gzip 协商 + `Vary: Accept-Encoding`） |
| `_resolve_messages_directory(request, directory)` | :287-316 | directory 解析：selector stash 替换（:309）→ None 直通 → query/header 冲突 400 `directory_not_allowed`（:314-315）→ `validate_directory` 归一化（400 `invalid_directory` 在 directory.py:23）；**async 但无 await**（疑问点 14） |
| `_stream_upstream(request, path, params, directory)` | :319-334 | 构造并发出流式上游 GET（`forward_directory_headers`）；初始 `httpx.RequestError` → `raise_upstream_unavailable`（503）；调用者必须 `aclose()` |
| `_placeholder_pairs(projected)` | :390-422 | 页序收集携带 `thin_placeholder_{mid}` part 的 (index, mid) — merged 高优先队列；mid 优先取占位 part 的 `messageID`（skeleton.py:618-625 写入），回退 `info.id`；无可用 id 跳过 |
| `_expand_ref_pairs(projected)` | :425-451 | 页序收集**任一 part 携带 part 级 `expandRefs`** 的 (index, mid)；message 级 `info.expandRefs`（diffs 引用，~105KB）永不入候选集（§4.3.1） |
| `_merged_candidate_pairs(projected, config)` | :454-481 | 合并候选：placeholder 先占 `merged_max_fulls_per_page`（默认 16）个槽（页序），ref 候选只填剩余槽、按 mid 去重（placeholder 身份优先） |
| `_dedicated_full_get(request, sid, mid, directory, cap)` | :484-514 | 单消息 ONE 专用流式 GET + `read_upstream_response` cap-read（`sid=sid` → 404 映射 `session_not_found`，见疑问点 2）；返回 body 或 None（截断）；`finally: aclose()` |
| `_fetch_full_shared(request, pool, sid, mid, directory, *, cap)` | :517-588 | 经 `singleflight.fulls`（key=`("full", id(pool), sid, mid, directory)`）共享 GET；`cap=None`（direct /full）→ 全量 `max_message_bytes`；截断 raise `_CapExceeded` drop entry；join 小 cap 截断 → 最多 3 次尝试 re-lead；direct 耗尽后专用 GET fallback（:584-587），merged 显式无 fallback（:588 返回 None → 降级） |
| `_merge_fulls(...)` | :591-693 | merged phase B+C：预算预留模型 fan-out（`remaining` 可变单元 :651，reserve=min(max_message_bytes, remaining) :655-658，refund :666-671，逐项 CodedHTTPException→`_DEGRADED`）+ `asyncio.gather`（无 return_exceptions，疑问点 3）+ windfall 累计检查 :678-686 + phase C `async with pool` 下单次 offload splice :688-693 |
| `_merge_fulls_and_pack(projected, fetched, *, accept_encoding, fingerprint)` | :696-738 | worker 入口（phase C）：逐项 parse full → `strip_diagnostics_message` → 替换 `parts`（保留 LIST 的 `info`）；坏 JSON/非 dict/非 list parts 逐项降级；成功 splice 后 `recompute_fingerprint`（fingerprint 开启时）；`orjson.dumps` → identity bytes |
| `_canonical_list_query(limit, before, mode)` | :752-766 | 排序键序拼 query 串（coalescing key 用）；`mode` 参与 key（merged/默认页互不共享 flight，即使 ignored 值也分 key） |
| `_messages_list_key(request, sid, directory, limit, before, mode)` | :769-781 | list flight key：`("messages-list", id(upstream_client), sid, directory, canonical_query)` — 嵌 upstream client 身份防跨 app 共享 |
| `_fetch_list_raw(request, sid, params, directory, *, cap)` | :784-805 | 单飞 factory body：ONE list GET + cap-read + Link→cursor 捕获（aclose 前）；返回 `(body, next_cursor)` 给所有 joiner |
| `_messages_via_lease(...)` | :808-945 | join-first 租约路径（coalesce 开启时优先走）：`registry.fetch_or_bypass`（预算满返回 None → 调用方走直连）→ 自有 pool admission + offload 投影 → merged 则 `_merge_fulls` → envelope → ETag/Vary/gzip 尾部（与直连尾部逐行同构，:887-943） |
| `_expand_shape_error()` | :1293-1295 | raise `CodedHTTPException(502, code="upstream_invalid_shape")` — parsed-but-malformed 上游体 |
| `_expand_locate_part(message, part_id)` | :1298-1329 | 在 parts 中定位 part_id：parts 非 list/元素非 dict/id 不可用/重复 id → 502；找到列表尾仍无 → 404 `expand_target_not_found` reason=`part_missing` |
| `_expand_str_field(obj, field)` | :1332-1340 | 嵌套字符串字段：missing/null → None；非字符串 → 502 |
| `_extract_info_summary_diffs` | :1343-1362 | `info.summary.diffs` → `{"diffs": FileDiff[]|null}`（message 级；逐层 null 容忍、逐层类型校验） |
| `_extract_part_text` / `_extract_part_reasoning` | :1365-1372 | `part.text` → `{"text": str|null}` |
| `_expand_state(part)` | :1375-1383 | tool state 访问器：missing/null → None；非 dict → 502 |
| `_extract_part_state_output` / `_extract_part_state_error` | :1386-1395 | `state.output` / `state.error` → `{"output"|"error": str|null}` |
| `_extract_part_state_input_full` | :1398-1408 | `state.input` → `{"input": object|null}`；非 dict → 502 |
| `_extract_part_state_metadata_full` | :1411-1424 | `state.metadata` → `{"metadata": object|null}`，**剔除 `diagnostics` 键**（与 /full 同一 strip 语义） |
| `_extract_part_state_attachments` | :1427-1442 | `state.attachments` → `{"attachments": object[]|null}`；非 list 或元素非 dict → 502 |
| `_extract_part_url` / `_extract_part_source` / `_extract_part_snapshot` | :1445-1462 | `part.url`(str) / `part.source`(dict) / `part.snapshot`(str) → 单键 dict |
| `_extract_compaction_full` | :1465-1468 | 完整 compaction part **减去 sidecar 注入的 `expandRefs`**（白名单式构建） |
| `_expand_fragment_worker(body, *, category, mid, part_id, limit, accept_encoding)` | :1487-1528 | worker 入口（§3.1 步骤 4d-7）：parse（失败→ValueError→503）→ 定位 part → type 适配校验（400）→ extractor → envelope `{category, messageID, data[, partID]}` → 序列化后按 `limit` 检查（413）→ `compress_if_beneficial` |
| `_expand_fragment(request, sid, category, mid, part_id, directory)` | :1531-1613 | 两个 expand 路由的共享实现（§3.1 严格求值序，见下文路由段） |

### 四个路由 handler

#### 1) `messages` — `GET /slimapi/messages/{sid}`（`@router.get("")` :948，handler :949-1141）

逐段：

- **入参** :950-955：`sid` path；`limit: int = Query(40, ge=1, le=200)`（越界 → FastAPI 原生 422，非 coded body，疑问点 18）；`before`（opaque cursor，原样转发 :981-987，注释论证 base64url 字符集在 FastAPI percent-decode 下安全）；`directory`、`mode`（仅字面 `"merged"` 生效 :990，其余值静默忽略，永不 400）。
- **directory 解析** :979 → `_resolve_messages_directory`。
- **coalescing 前置** :994-1001：`raw_fetch_registry` 存在且 `coalesce_enabled` → `_messages_via_lease`；返回 None（预算满/禁用/旧实例）→ 落入下方直连路径。
- **直连 admission** :1009 `async with pool`（**GET 前先 admission** — 关键防 OOM 修复，注释 :1005-1008）。
- **上游 GET** :1010-1012 `_stream_upstream` → `GET /session/{sid}/message?limit=&before=`。
- **cap-read** :1017-1022 `read_upstream_response(cap=max_response_bytes)`；`None` → 413 `response_too_large`（:1024-1028，early return 于 `finally aclose` :1075-1076 保护内，仍持 admission 至 `async with` 退出）。
- **cursor 捕获** :1035 `_parse_link_next_cursor(response.headers.get("Link"))`；上游 Link 头**不**外泄（v3 契约 cursor 通道 = envelope `nextCursor`）。
- **wire_view** :1050 `_expand_wire_view(request.scope)`（事件循环上读 selector stash）。
- **投影 offload** :1051-1072：merged → `_parse_sort_project`（仅 dict，不 pack）；默认 → `_project_list_sorted_and_pack`（直接 identity bytes）；`(orjson.JSONDecodeError, ValueError, TypeError, AttributeError)` → 503 `upstream_unavailable`（:1073-1074）。
- **merged B+C** :1077-1084 `_merge_fulls`（phase A 的 admission 已释放；phase B 无 slot fan-out；phase C 内部再 admission :688）。
- **envelope** :1089 `messages_envelope_bytes(identity, next_cursor)` → `{"items":<bare array bytes>,"nextCursor":<str|null>}`（envelope.py:21-28，字节拼接不重序列化；envelope bytes = ETag 输入）。
- **ETag/Vary/gzip 尾部** :1090-1139：
  - `Cache-Control: no-store` :1090；
  - `rep_version = etag_mod.response_rep_version(config, wire_view=3)` :1098-1099 — **硬编码 3**（疑问点 5）；
  - `vary_value = etag_mod.merged_vary("Accept-Encoding")` :1102（实际恒返回 `"Accept-Encoding"`，directory 维度已退役 — 注释 :1100-1101 与实现错位，疑问点 7）；
  - `judge_conditional`（pre-compression 单候选判定）:1107-1112；`"*"` → 压缩一次 + 回显实际 coding 的 tag :1113-1122；命中 → 304 `not_modified_response`（ETag+Vary+no-store，无 body、无 aux）:1123-1125；
  - miss → `compress_if_beneficial` :1126-1128（gzip 协商 + MIN_GZIP_BYTES + 实际收益三重门，gzip_util.py:75-108）；
  - 200：`final_headers = c_headers` + `Vary` 覆写 :1129-1130 + 条件 ETag（按实际 coding 强/弱 tag）:1131-1135 → `Response(..., media_type="application/json")` :1136-1139。
- **busy** :1140-1141 `except TransformBusy → _busy_response`。

租约路径 `_messages_via_lease`（:808-945）逐段：accept-encoding 读取 :823 → `_factory`（cap=config.max_response_bytes）:825-828 → `fetch_or_bypass(reserve_bytes=max_response_bytes)` :830-834 → None→直连 :835-836 → `async with lease`（**lease 持有跨越 merged fan-out 与 admission 等待**，疑问点 9b）:837 → 共享 body None → 413 :838-844 → 自有 admission + offload :862-877（异常面同直连）→ merged `_merge_fulls` :878-886 → envelope :892 → 与直连完全同构的 ETag/Vary/gzip 尾部 :893-943（rep_version pin 同样在 :902-903）→ TransformBusy :944-945。注意：租约路径上游 GET 在 admission **之外**（factory 内），与直连「admission-before-GET」不变量不同（plan §3.x 设计，非 bug）。

#### 2) `message` — `GET /slimapi/messages/{sid}/full/{mid}`（`@router.get("/full/{mid}")` :1144，handler :1145-1249）

- **入参** :1146-1147：`sid`、`mid` path、`directory` query；`?mode=` / `?known.*` 静默忽略（lite-v2 §2 降级为纯 on-demand expand，无 304 短路、无 `X-Message-Event-Seq`）。
- **directory** :1181；config/pool/accept_encoding :1182-1184。
- **admission absorb 循环** :1205-1214：`deadline = now + transform_absorb_budget_seconds`（默认 2.5s）；每轮 `pool.acquire(min(transform_wait_seconds, remaining))`（默认单轮 2s）；耗尽 → `raise TransformBusy()`（不变量：503 `transform_busy` 从未发出上游请求）。
- **单飞共享 GET** :1224 `_fetch_full_shared(cap=None)` — key=`("full", id(pool), sid, mid, directory)`，与 merged fan-out、expand 路由同 key 去重（singleflight.py:243-254；`fulls = SingleFlight()` plain profile，result grace 默认 1.0s）。
- **None → 413 `message_too_large`**（limitBytes=max_message_bytes）:1225-1230。
- **offload** :1235-1239 `strip_diagnostics_and_pack(body, accept_encoding, merge_directory_vary=True)`（worker：strip `state.metadata.diagnostics` + pack + gzip + Vary）；`(JSONDecodeError, ValueError, TypeError, AttributeError)` → 503 :1240-1241。
- **release** :1242-1243（`finally`，与 acquire 严格配对）。
- **响应** :1244-1247：200 + `Cache-Control: no-store` + extra（Vary / Content-Encoding；**无 ETag / 无 304** — lite-v2 冻结行为）。
- **busy** :1248-1249。

#### 3) `expand_message_fragment` — `GET /slimapi/messages/{sid}/expand/{category}/{mid}`（:1616-1628）

薄壳：调 `_expand_fragment(..., part_id=None, ...)`。仅 `info_summary_diffs` 为 message 级；其余 category 在此 400（expectedLevel=part）。

#### 4) `expand_part_fragment` — `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`（:1631-1643）

薄壳：调 `_expand_fragment(..., part_id=partID, ...)`。`info_summary_diffs` + partID → 400 expectedLevel=message；未知 partID → 404。

#### `_expand_fragment` 共享实现（§3.1 严格求值序，:1531-1613）

1. **category 白名单** :1539-1544（plain str 手工白名单，故意不用 FastAPI Enum 避免 422）→ 400 `invalid_expand_category` + `validCategories`（12 类全表广播）。
2. **level 匹配** :1548-1560 → 400 `expand_category_mismatch`（expectedLevel=message/part）——**在 admission 之前**。
3. **directory** :1562；`fragment_limit = config.max_expand_response_bytes`（默认 8MiB，getattr 回退）:1567-1569。
4. **admission absorb 循环** :1573-1582（同 /full；「pool 满 503 先于一切 part 级 40x」）。
5. **共享单飞 GET** :1586（与 /full 同 key、cap=None 全量）→ None → 413 `expand_source_too_large` :1587-1595（cap-read 先于 JSON decode，oversize+malformed 仍 413）。
6. **offload worker** :1598-1602 `_expand_fragment_worker`（定位 404/502、type 400、序列化后 413、gzip 都在 worker 内）；route 捕 `(orjson.JSONDecodeError, ValueError)` → 503 :1603-1605（捕获面窄于其他路由，疑问点 12）。
7. **响应** :1608-1611（200 + no-store + extra）。
8. **busy** :1612-1613。

## 依赖（内部 imports）

| 来源 | 符号 | 行号 |
|---|---|---|
| `..errors` | `CodedHTTPException` | :13 |
| `..etag` | `etag_mod`（`response_rep_version`/`compute_etag`/`merged_vary`/`judge_conditional`/`not_modified_response`） | :14 |
| `..envelope` | `messages_envelope_bytes` | :15 |
| `..gzip_util` | `compress_if_beneficial`, `error_response` | :16 |
| `..selector` | `resolve_route_directory`, `wire_view_from_scope` | :17 |
| `..singleflight` | `full_fetch_key`, `fulls` | :18 |
| `..skeleton` | `SkeletonLimits`, `recompute_fingerprint`, `skeleton_messages`, `strip_diagnostics_message` | :19-24 |
| `..transform` | `TransformBusy`, `read_with_cap`, `strip_diagnostics_and_pack` | :25-29 |
| `..upstream` | `forward_directory_headers` | :30-32 |
| `..upstream_errors` | `raise_upstream_unavailable` | :33-35 |
| `._catalog_common` | `read_upstream_response` | :36 |
| `..directory` | `validate_directory` | :37 |
| `..readiness` | `readiness_mod`（动态读 `SATISFIED`） | :38 |
| `..traffic` | `EXPAND_CATEGORIES`, `EXPAND_CATEGORIES_SET`（**mid-file**） | :1271-1272 |

外部：`orjson`, `httpx`, `fastapi`(APIRouter/Query/Request), `starlette.responses.Response`, `asyncio`, `re`, `time`, `urllib.parse.urlparse`。

## 被依赖（rg 反查）

- **生产**：`src/oc_slimapi/app.py:29`（import `messages`）、`src/oc_slimapi/app.py:760-761`（`include_router`，位于 health/versions/actions/agent/command/sessions/children/todo/diff 之后、events 等之前）。
- **注释引用**：`src/oc_slimapi/skeleton.py:118`（SkeletonLimits 的 route 侧说明反向指向本文件）。
- **测试**（直接 import）：`tests/test_messages_routes.py:33`（并直接导入私有 `_parse_link_next_cursor` :1009/:1023/:1036/:1054、`msgs_mod` :308）；`tests/test_messages_merged.py:44`；`tests/test_errors.py:9`；`tests/test_upstream_error_boundary.py:37`；`tests/test_v3_envelope.py:25`；`tests/test_sessions_coalesce.py:30`；`tests/test_b2_merged_text_compat.py:28`。间接：`tests/test_readiness_gating_integration.py`（`messages.expand.v4` 门控）。

## 状态 / 可变性

- **模块级长生命周期对象**：`router` :40（注册后只读）；`TRANSFORM_RETRY_AFTER_SECONDS` :44；`_V4_EXPAND_FEATURE` :55；`_REL_PARAM_RE` :168（编译正则，不可变）；`_PLACEHOLDER_PART_ID_PREFIX` :361；`_DEGRADED` 哨兵对象 :365；`_EXPAND_MESSAGE_LEVEL_CATEGORIES` frozenset :1275；`_EXPAND_APPLICABLE_TYPES` dict :1278-1290 与 `_EXPAND_EXTRACTORS` dict :1471-1484（**类型上是可变 dict，但构造后仅读**）；`_EXPAND_CATEGORIES`（traffic.py 导入的 **list**，可变类型仅读）。
- **本模块无锁、无 task、无线程**；所有 CPU 工作经 `pool.offload` 进入 `TransformPool` 的 `ThreadPoolExecutor`（worker 函数 `_parse_sort_project`/`_project_list_sorted_and_pack`/`_merge_fulls_and_pack`/`_expand_fragment_worker` + transform.py 的 `strip_diagnostics_and_pack` 必须线程安全 — 均为纯函数）。
- **引用（非拥有）的进程级共享可变状态**：
  - `singleflight.fulls`（:18；plain `SingleFlight()`，completed 结果 grace 保留 1.0s，singleflight.py:97/:278/:770）— /full、expand、merged fan-out 三方共键；
  - `app.state.transforms`（`TransformPool`：信号量 + executor；`acquire/release` 手动配对路径 :1211/:1243、:1579/:1607）；
  - `app.state.raw_fetch_registry`（`LeasedSingleFlight(max_bytes=raw_fetch_max_bytes=64MiB 默认, network_concurrency=4 默认)`，app.py:385-388）；
  - `app.state.upstream`（httpx AsyncClient）；
  - `readiness_mod.SATISFIED` — **调用时动态读**（:62），测试可 monkeypatch 翻转门控；
  - `_merge_fulls` 的 `remaining` 可变单元（:651）只在事件循环串行点触达（reserve :658 / refund :671，无 await 穿插）。
- 模块导入即完成路由注册副作用（`@router.get` 装饰器）；mid-file imports（:1271-1272）在 import 期执行，无循环依赖（traffic.py 不反向 import routes）。

## 错误路径（全部构造点）

### 本文件直接构造

| 位置 | 状态码 / code | 触发 |
|---|---|---|
| :278-284 `_busy_response`（调用点 :945, :1141, :1249, :1613） | 503 `transform_busy`（+ body `retry_after` + 头 `Retry-After: 2`） | admission absorb 预算耗尽 / `TransformBusy` |
| :315 | 400 `directory_not_allowed` | query 与 `X-Opencode-Directory` 头冲突（rstrip("/") 归一比较） |
| :840-844（lease）/:1024-1028（direct） | 413 `response_too_large`（limit=max_response_bytes） | list body 超 cap（共享 flight 截断同样 413） |
| :1226-1230 | 413 `message_too_large`（limitBytes=max_message_bytes） | /full body 超全量 cap（含 direct 专用 GET fallback 后的真 413） |
| :1295 `_expand_shape_error`（worker 内多点调用：:1308/:1313/:1319/:1321/:1339/:1349/:1354/:1361/:1382/:1407/:1421/:1439/:1441/:1456） | 502 `upstream_invalid_shape` | parsed-but-malformed（parts 非 list、part 无 id、重复 id、嵌套字段类型不符等） |
| :1326-1328 | 404 `expand_target_not_found`（reason=`part_missing`） | partID 不在消息 parts |
| :1510-1513（worker 内） | 400 `expand_category_mismatch`（expectedTypes） | part.type 不适用该 category |
| :1525-1527（worker 内） | 413 `expand_fragment_too_large`（limitBytes=max_expand_response_bytes） | 序列化后 fragment 超 cap（gzip 前 identity 判定） |
| :1540-1544 | 400 `invalid_expand_category`（validCategories=12 类全表） | category 不在白名单 |
| :1549-1554 / :1556-1560 | 400 `expand_category_mismatch`（expectedLevel=message/part） | level/category 不匹配 |
| :1591-1595 | 413 `expand_source_too_large`（limitBytes=max_message_bytes） | expand 源 body 超全量 cap |

### 经由依赖模块的构造（本文件调用点）

| 调用点 | 来源 | 结果 |
|---|---|---|
| :334, :505 | `raise_upstream_unavailable`（httpx.RequestError） | 503 `upstream_unavailable` |
| :877, :1074, :1241, :1605 | `raise_upstream_unavailable`（JSONDecodeError/ValueError/TypeError/AttributeError；:1605 仅前两种） | 503 `upstream_unavailable`（上游坏 JSON / 非法形状） |
| :507-512, :796-801（经 `read_upstream_response` → `raise_upstream_status_code`，`sid` 传入） | upstream_errors.py:63-77 | **404+sid → 404 `session_not_found`**；其他 4xx → 502 `upstream_http_N`；5xx → 503 `upstream_unavailable`；mid-stream RequestError → 503 |
| :316（`validate_directory`，directory.py:23-53） | 400 `invalid_directory` | `..`/`.` 段、NUL、控制字符、>4096 |

### 非 coded 错误面

- `limit` 越界（:952 `Query(ge=1, le=200)`）→ **FastAPI 原生 422**（`{"detail":[...]}` 形状，非 `{"code":...}`）。**本文件不存在 `invalid_cursor` / `request_too_large` 构造点**（`before` 完全透传不校验）。
- `_CapExceeded`（:562 raise）与 `TransformBusy`（:1209, :1577 raise；:944, :1140, :1248, :1612 catch）为内部异常，不直接上 wire。

## 疑问点（draft 种子，宁多勿漏；按可疑度排序）

1. **merged 预算预留粒度 × 默认配置 → fan-out 实际退化为每页最多 1 条**（:649-672，config.py 默认 `max_message_bytes=32MiB` > `merged_max_bytes=8MiB`）。首个候选一次性 reserve `min(32MiB, 8MiB)=8MiB`（:655-658，第一个真 await 之前完成），`remaining` 归零；同批 gather 顺序调度的其余任务 `cap<=0` → `_DEGRADED`（:656-657）**且永不重试**（refund :666-671 只惠及尚未启动的任务）。`gather` 按创建序跑到首个挂起点是确定性语义 → 默认配置下每页恰好 inline 第一条 placeholder，其余 15 条（`merged_max_fulls_per_page=16`）静默降级，与 handler docstring（:966-970）和 `_merged_candidate_pairs` 语义不符。测试 `tests/test_messages_merged.py:250-258` 明确 pin `max_message_bytes=256KiB`（"reservations cannot exhaust merged_max_bytes before all 16 items start"）恰好绕开生产默认参数组合；而 :498/:539/:593 的预算测试也全用 `max_message_bytes=8000 < merged_max_bytes=10000`。**默认配置组合疑似无人测试覆盖**。
2. **/full 与 expand 的上游 404 映射为 `session_not_found`**（:507-512 `_dedicated_full_get` 以 `sid=sid` 调 `read_upstream_response` → `raise_upstream_status_code` 404+sid 无条件映射 `session_not_found`，upstream_errors.py:75-76）。消息不存在而 session 存在时（上游 `GET /session/{sid}/message/{mid}` 404），客户端收到 404 `session_not_found` — 语义错位（可能误导 ocdroid 清 session 缓存）；是否契约冻结需查 v3-contract §错误表。
3. **`_merge_fulls` 的 `asyncio.gather` 无 `return_exceptions`**（:674-676）：`_fetch_one` 只捕 `CodedHTTPException`（:664-665）；`singleflight` 注册表 shutdown 竞态、`RuntimeError`、`CancelledError` 等会逃出 gather → 整页 500/中断，与 :641-642 "merging must never fail the page" 的强声明不完全一致（该声明仅对结构化上游错误成立）。
4. **windfall 峰值内存自认超标**（docstring :619-637 层 2/层 3）：单飞 key 不含 cap（:567），merged 小 cap waiter 可 join direct-led 32MiB flight（或其 grace 结果），`gather` 结果持有共享 body 直到 :678-686 splice 排除 → 页面瞬态可持有 ~`max_message_bytes`（32MiB），远超 `merged_max_bytes + fanout×chunk ≈ 8.5MiB` 公式。已文档化，但 systemd `MemoryMax=384M` 下的实测峰值值得 draft 阶段核查（与 `max_transforms=1` 并发放大）。
5. **`response_rep_version(config, wire_view=3)` 硬编码 3**（:902-903, :1098-1099）与 `_expand_wire_view` 可返回 4（:854, :1050）不对称：REP_VERSION salt 固定 `wire=v3`（etag.py:85-88），而 identity bytes 可能含 `?v=4` href。ETag 正确性不受影响（bytes 变则 tag 变），但跨 view 的 rep-version 语义不一致 — 是有意（保持 v3 ETag 基线稳定）还是遗漏，需对照 v4-contract §14。
6. **merged 页的 ETag 是「本次获取结果指纹」而非「内容指纹」**（:683-686 windfall 跳过 + 单飞 grace 1s 复用 + 预算时序）：同一页面内容在不同并发环境下（是否恰有 direct /full 同 key flight）产生不同 identity/ETag。方向上是保守正确的（内容变→200），仅损失 304 命中率；但契约若声称 messages ETag 表征内容，merged 模式下该声明需限定。
7. **Vary 注释与实现错位**：:904-907 / :1100-1102 注释称 "directory Vary dimension is unconditional"，而 `etag.merged_vary`（etag.py:171-177）恒返回 `"Accept-Encoding"`、directory 维度已随 §5.7 退役。注释中 "unconditional" 实指「Vary 头的发射不依赖 ETag 开关」，易误导审计者/后续维护者以为 Vary 含 directory。
8. **`_parse_link_next_cursor` 按 `,` 切分 Link**（:246）：RFC 5988 允许引号串内逗号；注释 :240-242 自认依赖 "opencode cursors 不含逗号"。上游（或中间层）异常 Link 可错切 → cursor 解析失败 → None → fail-safe（无 nextCursor），影响面小但属于对上游形态的未防御假设。
9. **merged phase B 的并发面**：(a) fan-out 无 admission（:653-676，默认 `merged_fanout=8`）→ 同时最多 8 个上游 full GET + 直连 /full/expand 并发，是否受 `app.state.upstream` httpx client 连接上限约束需在 app.py/上游客户端卡片核对；(b) **lease 路径下 merged 请求在 fan-out + phase C admission 等待期间一直持有 list lease**（:837 `async with lease` 包住 :882 `_merge_fulls`）→ 拉长 `raw_fetch_max_bytes`（默认 64MiB，仅容 1 个 flight）的占用窗口，降低其他请求 coalescing 可用性。
10. **`thin_placeholder_` 前缀跨模块字符串复制**（:358-361 注释自认 skeleton.py 在当时写域外）：skeleton.py:618 构造 `f"thin_placeholder_{message_id}"`，本文件 :361 复制前缀做识别 — 隐式契约无共享常量；skeleton 侧改名则 merged 候选集静默为空（列表仍 200，仅 merged 功能失效）。
11. **expand 400 的双时序**：level mismatch 400 在 admission 前（:1548-1560），type mismatch 400 在 admission + 上游 GET 之后（worker :1509-1513）— 同码不同成本；契约 §3.1 step 6 如此冻结（"pool-full 503 precedes every part-level 40x" 注释 :1571-1572），但意味着 type mismatch 每次消耗一次上游 GET + transform slot（可否在 list/skeleton 元数据预判为 draft 议题）。
12. **expand offload 异常捕获面窄**（:1603 仅 `(orjson.JSONDecodeError, ValueError)`，对比 :876/:1073/:1240 的 4 种）：extractors 均 isinstance 防御、`orjson.dumps` 对 NaN/Infinity 输出 null（已实测 orjson 3.11.9），实际暴露面小，但任何意外 `TypeError` 会以裸 500 上 wire 而非 503。
13. **mid-file imports**（:1271-1272）：`from ..traffic import ...` 位于文件中部 expand 段落（有意将冻结表贴着唯一消费者），非顶部 — 风格异常，工具链（import 排序/lint）与读者易漏；无循环依赖（traffic 不 import routes）。
14. **`_resolve_messages_directory` 为 async 但无 await**（:287-316，纯历史形态）；且 header-only directory（只发 `X-Opencode-Directory` 不发 query）被静默忽略不转发（:294-296 自认 "unchanged behaviour"）— 只发头的客户端会静默失去 directory 语义。
15. **`_created_sort_key` 畸形行默认 0 排最前**（:77-92）：缺 `info.time.created` 的上游行会跃居所有正常消息之前，改变可见顺序（文档化的防御性排序的副作用）；0 亦为合法 epoch-ms 值，不可区分。
16. **直连 413 `response_too_large` 不携带 cursor**（:1024-1028 early return）：超大列表页既拿不到 body 也拿不到 `nextCursor`，客户端无法降 limit 重试翻页 — 契约是否提供 recovery 路径可查。
17. **`_fetch_full_shared` 的 re-lead 逻辑与 singleflight entry-drop 语义强耦合**（:552-588）：正确性依赖「factory raise → entry 被 drop（不 grace 保留 None）」与「joiner 收到同一异常」两点（`_CapExceeded` docstring :370-380 声明），需在 singleflight 卡片交叉验证 `SingleFlight.fetch` 的异常传播路径；3 次上限与 direct fallback（:584-587）的「worst case 多一次 GET」已被注释接受。
18. **`limit` 校验错误为 FastAPI 原生 422**（:952）：非 `{"code":...}` 形状，与契约 §11 错误体约定的一致性（以及 ocdroid 是否依赖 coded body）值得核对；另 `_canonical_list_query` 让被忽略的 `mode` 值（如 `mode=full`）参与 flight key（:761-766，A2-C2 保守分 key）— 有意但降低命中率。

## 附：与测试的关键对齐点（供 draft 复核）

- `tests/test_messages_merged.py:140-142` 测试基线 settings 与生产默认一致（32MiB/8/16/8MiB），但具体预算测试全部 override（见疑问点 1）。
- `tests/test_messages_merged.py:750-800`：merged 小 cap flight + direct join 的 re-lead/降级语义端到端锁定（`full_calls==2`、direct 200 非 413）。
- `tests/test_readiness_gating_integration.py:256-266`：`messages.expand.v4` 门控开/关 → href `?v=` 折回行为。

<!-- ==== e1-11-sessions-line ==== -->
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

<!-- ==== e1-12-readgroups-line ==== -->
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

<!-- ==== e1-15-write-events-stream ==== -->
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

<!-- ==== e1-17-small-routes-misc ==== -->
# E1-17 精读卡片 —— 小路由与杂项模块（十四文件）

> 审计日期 2026-08-20。引用格式 `src/oc_slimapi/...:行号`。全部文件已全文精读（非抽样）；交叉依赖符号经 grep/定点阅读核实。

---

### src/oc_slimapi/routes/directories.py（202 行）

- **职责**：`GET /slimapi/directories` —— 全局工作目录目录（ocdroid 项目切换器）。发现源为上游 `GET /experimental/session?roots=true&archived=true`（跨 workdir 的 GLOBAL 顶层 session 列表），按 `normalize_directory` 聚合为每 workdir 一行。无任何 query 参数；经 `?v=3` selector 准入。
- **对外符号**：
  - `router`（src/oc_slimapi/routes/directories.py:14）— `APIRouter(prefix="/slimapi", tags=["directories"])`。
  - `directories(request)`（:17-108）— 路由 handler：TransformPool admission（`async with request.app.state.transforms as pool`，:77）→ `fetch_global_root_sessions`（:80-82）→ 严格 schema 守卫（:90-95）→ `pool.offload(_aggregate_and_pack, ...)`（:99-102）→ 200 响应 + `Cache-Control: no-store`（:105-108）。
  - `_num(v)`（:111-115）— 数值抽取；非数（含 bool）→ 0（排序垫底）。
  - `_session_rank(s)`（:118-130）— winner 排序键 `(time.updated, time.created, id)`；`id` 缺失/非 str 强制 `""` 防异构比较 TypeError。
  - `_aggregate_and_pack(sessions, discovery_complete, *, accept_encoding)`（:133-202）— worker 侧聚合 + orjson + 可选 gzip（`compress_if_beneficial`）。聚合规则：group key = `normalize_directory(s["directory"])`；`activeRootSessionCount` = `time.archived` 非数值者；`archivedRootSessionCount` = 数值者（排除 bool，:176）；winner = `max` by rank；`title` 非非空 str → None（:184-186）；输出按 `lastUpdated` DESC、`directory` ASC 排序（:198）。
- **依赖 / 被依赖**：依赖 `directory.normalize_directory`、`discovery._DISCOVERY_LIMIT`（=10_000，src/oc_slimapi/discovery.py:52）与 `discovery.fetch_global_root_sessions`、`gzip_util.compress_if_beneficial`、`transform.TransformBusy`、`upstream_errors.raise_upstream_unavailable`、`_catalog_common.busy_response`。被挂载于 `src/oc_slimapi/app.py:760`（router 列表第 14 位）。
- **状态 / 可变性**：模块自身无状态（router + 纯函数）。无缓存、无 ETag。admission 覆盖 fetch→guard→aggregate 全程（docstring :38-41 "review blocker"）。
- **错误路径**：session 非 dict 或 `directory` 非非空 str → `raise_upstream_unavailable()`（:92、:95）→ 503 `upstream_unavailable`（无 envelope）；`TransformBusy` → `busy_response`（:103-104）→ 503 `transform_busy` + `Retry-After: 2`（`_catalog_common.py:44-52`）；发现阶段自身失败在 discovery 内 → 同样 503。
- **疑问点**（7）：
  1. :8 跨模块导入私有下划线符号 `_DISCOVERY_LIMIT`（风格 / 封装边界）。
  2. docstring :34-35 残留历史陈述 "no `X-Slimapi-Version` bump (still 2)"——该头已于 3.0.0 删除、现为 v3-only + v4 双窗，文档滞后于契约（AGENTS.md 明示冲突以契约为准）。
  3. handler 签名（:18）不接受任何 query 参数，客户端多传的 `?directory=` 等被 FastAPI 静默忽略——与 docstring "No query parameters"（:29-31）是"忽略"而非"拒绝"，契约未写明多余参数语义。
  4. winner 的 `time.updated` 缺失/非数值时 `lastUpdated=0`（:183 经 `_num`）——客户端会把该目录排在 1970 位置；0 占位是否为契约冻结值待核（v3-contract 相应条目）。
  5. `archivedOnly`（:194）= `activeRootSessionCount == 0`——若 group 全部 archived，winner 仍可能带有效 title/lastUpdated，字段间一致性依赖上游。
  6. `discoveryComplete` 判定在 discovery 层（`len(sessions) < limit`，discovery.py:177）而非本文件；docstring :64-65 说 "filled exactly at `_DISCOVERY_LIMIT`" 与实现（`<` 才 true）一致，但语义边界（恰好 == limit 时报 false）是保守取舍，审计时注意它与"真实截断"可能不等价。
  7. :198 排序 key `-it["lastUpdated"]`——`lastUpdated` 为 int|float 混合（`_num` 可返回 float），负号取反对 float 无碍，但混排时 0（缺时间占位）与真实 ms 时间戳差距悬殊，目录顺序对缺失时间数据敏感。

---

### src/oc_slimapi/routes/diff.py（110 行）

- **职责**：T18（2026-08-16）read-only diff thin route：`GET /slimapi/sessions/{sid}/diff` → 上游 `GET /session/{sid}/diff[?messageID=…]`。近恒等投影（上游 `Snapshot.FileDiff` 五字段全保留，`patch` 是路由本体）；gzip + cap + admission 是省流杠杆。
- **对外符号**：
  - `router`（:44）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_diff(items)`（:47-60）— 逐项 dict 守卫后原样返回（near-identity）；非 dict → `ValueError`。
  - `session_diff(request, sid, directory=None, messageID=None)`（:63-110）— 路由 handler：`resolve_route_directory`（:88）→ `validate_directory`（:90）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES, upstream_params={"messageID": …} iff 非None)`（:95-108）。
- **依赖 / 被依赖**：依赖 `_catalog_common.handle_catalog_request/busy_response`、`selector.resolve_route_directory`、`directory.validate_directory`、`gzip_util.MIN_GZIP_BYTES`（=64，src/oc_slimapi/gzip_util.py:25）、`transform.TransformBusy/read_with_cap`。挂载于 app.py:760（以 `diff_routes` 别名，app.py:30）。
- **状态 / 可变性**：无状态——无缓存、`enable_etag=False`（无 ETag / 304，`If-None-Match` 被忽略恒 200）。
- **错误路径**：本文件构造点仅 `TransformBusy` → `busy_response`（:109-110，503 `transform_busy`）。`ValueError`（非 dict item / 非列表 / JSON 解码失败）由共享 handler 映射 503 `upstream_unavailable`（`_catalog_common.py:312-313`）；上游 4xx → 502 `upstream_http_N`、带 `sid` 的 404 → 404 `session_not_found`、超 cap → 413 `response_too_large`（`_catalog_common.py:296-299`）。
- **疑问点**（5）：
  1. `messageID` 空串（`?messageID=`）满足 `is not None`（:91-93）会被原样转发为空值 query——上游按"提供了 messageID"处理 → 200 `[]`；空串语义是否契约冻结待核。
  2. `read_timeout=None`（:102）——无上游读超时；`patch` 本体可能很大，上游慢读时该请求可无限期占住 admission（与 command.py 的 300s 形成对比）。
  3. docstring :8-9、:14-18 锚定 v1.18.16 上游源码路径，而 AGENTS.md 声明当前对齐 v1.18.18——锚点未随 repoint 更新（对照时需重新核验 summary.ts 行号）。
  4. `directory` 经 stash 解析后又 `validate_directory`（:88-90）——注释称幂等纯函数重复校验（selector.py:410-413 确认 stash 已验证），双保险但也是重复。
  5. `upstream_params` 仅 messageID 一个键；其余未知 query 参数不透传（静默丢弃）——与 passthrough 路由行为差异是否已被 ocdroid 文档化。

---

### src/oc_slimapi/routes/todo.py（84 行）

- **职责**：T17 / traffic plan Batch 3（C2a）read-only todo thin route：`GET /slimapi/sessions/{sid}/todo` → 上游 `GET /session/{sid}/todo`。近恒等投影（`Todo.Info` 三字段全保留）。
- **对外符号**：
  - `router`（:29）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_todo(items)`（:32-44）— 逐项 dict 守卫（rev-6 B2）后原样返回；非 dict → `ValueError`。
  - `session_todo(request, sid, directory=None)`（:47-84）— handler：stash 解析 directory（:66-68）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES)`（:70-82）。
- **依赖 / 被依赖**：同 diff.py（无 `upstream_params`）。挂载于 app.py:760。
- **状态 / 可变性**：无状态（无缓存、无 ETag）。
- **错误路径**：`TransformBusy` → `busy_response`（:83-84）；`ValueError`/JSON 失败 → 503 `upstream_unavailable`；上游 4xx → 502 `upstream_http_N`；带 sid 404 → 404 `session_not_found`；超 cap → 413 `response_too_large`（均在 `_catalog_common`）。
- **疑问点**（4）：
  1. `read_timeout=None`（:77）——同 diff.py 疑问 2，无读超时占 admission。
  2. 设计文档引用 `docs/specs/traffic-route-todo-2026-08-10.md`（:5）——审计下游应确认该文档仍在仓且与实现一致。
  3. `_project_todo` 对 dict item 的内部字段零校验（如 `status` 非 str 原样透传）——近恒等是设计决定，但畸形字段直通客户端。
  4. 空 `[]` 响应走 identity（`min_gzip_bytes=64` gate，:81）——`Vary` 头仍合并 directory（`merge_directory_vary=True`），identity + Vary 组合对中间缓存正确但需契约确认。

---

### src/oc_slimapi/routes/agent.py（52 行）

- **职责**：`GET /slimapi/agent` —— agent 目录骨架投影：白名单 `name/description/mode/hidden/native`（`AGENT_SKELETON_KEYS`，src/oc_slimapi/skeleton.py:790），丢 `prompt`（~34.7%）与 `permission`（~61.2%），实测 ~95.8% raw 省流。
- **对外符号**：
  - `router`（:14）— `prefix="/slimapi"`，tags=["catalog"]。
  - `agent(request, directory=None)`（:17-52）— handler：stash 解析 + validate directory（:37-39）→ `handle_catalog_request(upstream_path="/agent", project_fn=skeleton_agents, cache=getattr(app.state, "catalog_cache", None), merge_directory_vary=True, read_timeout=None)`（:41-50）。
- **依赖 / 被依赖**：依赖 `skeleton.skeleton_agents`（含非 dict item 静默跳过，skeleton.py:807-809）、`_catalog_common`、`selector`、`directory`、`transform`。挂载于 app.py:760（第 4 位）。
- **状态 / 可变性**：路由自身无状态；经 `catalog_cache` 走 TTL body 缓存 + **默认 `enable_etag=True`**（`_catalog_common.py:230`，未传 → True）——与 todo/diff/children 不同，agent/command 带 ETag/304。
- **错误路径**：`TransformBusy` → `busy_response`（:51-52，503 `transform_busy`）；非列表/JSON 失败 → 503 `upstream_unavailable`；4xx → 502 `upstream_http_N`（catalog 路由不传 `sid`，404 不映射 `session_not_found`）；超 cap → 413；缓存工厂错误同 503（`_catalog_common.py:430-431`）。
- **疑问点**（4）：
  1. 未传 `min_gzip_bytes`（默认 None → 无 tiny-body gate）——与 todo/children 的 64B gate 行为分叉（rev-6 C2 只覆盖 Batch 3），小目录无 gate 损益极小但属有意差异，审计应确认契约记录。
  2. `read_timeout=None`（:48）——无读超时；agent 目录 ~250KB，慢上游时占 admission。
  3. `skeleton_agents` 静默跳过非 dict item（skeleton.py:809）——与 todo/diff/children 的"非 dict → 503"守卫方向相反（容错 vs fail-fast），同一 `handle_catalog_request` 链上两种畸形策略并存。
  4. `directory` 被接受并转发但上游全局目录忽略之（docstring :30-32 自认"无害"）——多余 variance 进入 `Vary` 合并（`merge_directory_vary=True`），缓存键被无语义参数放大。

---

### src/oc_slimapi/routes/children.py（90 行）

- **职责**：T17 children thin route：`GET /slimapi/sessions/{sid}/children` → 上游 `GET /session/{sid}/children`。每个 child 是 `Session.Info`，逐个经 `skeleton_session()` 投影（与 sessions 列表路由同语义）；STATELESS re-add（v1 缓存一致性机制保持删除）。
- **对外符号**：
  - `router`（:35）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_children(items)`（:38-52）— 逐项 dict 守卫后 `[skeleton_session(item) for item in items]`（SESSION_KEYS 白名单 + time/summary/revert 嵌套 pick，skeleton.py:729-743）。
  - `session_children(request, sid, directory=None)`（:55-90）— handler：stash 解析 + validate（:72-74）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES)`（:76-88）。
- **依赖 / 被依赖**：依赖 `skeleton.skeleton_session`；其余同 todo.py。挂载于 app.py:760。
- **状态 / 可变性**：无状态（docstring :13-16 明确：无 `X-Children-Version`、无 digest 字段、无 cache/single-flight/SSE 失效）。
- **错误路径**：同 todo.py（`ValueError` → 503 `upstream_unavailable`；404+sid → 404 `session_not_found`；TransformBusy → 503；413）。
- **疑问点**（4）：
  1. `read_timeout=None`（:83）——同前，无读超时。
  2. 非守卫覆盖的 `skeleton_session` 内部畸形（`time` 非 dict → 跳过嵌套 pick 而非报错，skeleton.py:739-741）——静默降级为缺键。
  3. 投影丢弃 `parentID` 之外的重组信息：child 自身 `parentID` 保留（SESSION_KEYS 含 parentID），但列表顺序由上游决定——顺序稳定性未在文件内声明。
  4. 设计文档 `docs/specs/traffic-route-children-2026-08-10.md`（:5）与 §6.2 guardrail 的引用需下游审计核对。

---

### src/oc_slimapi/routes/command.py（49 行）

- **职责**：`GET /slimapi/command` —— command 目录骨架投影：白名单 `name/description/agent/hints`（`COMMAND_SKELETON_KEYS`，skeleton.py:768），丢 `template`（~97.7%），实测 ~97.6% raw 省流。
- **对外符号**：
  - `router`（:14）— `prefix="/slimapi"`。
  - `command(request, directory=None)`（:17-49）— handler：stash 解析 + validate（:34-36）→ `handle_catalog_request(upstream_path="/command", project_fn=skeleton_commands, read_timeout=300.0, cache=..., merge_directory_vary=True)`（:38-47）。
- **依赖 / 被依赖**：同 agent.py（`skeleton_commands`，非 dict item 静默跳过，skeleton.py:782-787）。挂载于 app.py:760（第 5 位）。
- **状态 / 可变性**：经 `catalog_cache` TTL 缓存 + 默认 `enable_etag=True`；无路由自身状态。
- **错误路径**：同 agent.py（TransformBusy → 503 `transform_busy`；JSON/非列表 → 503 `upstream_unavailable`；4xx → 502 `upstream_http_N`；413 `response_too_large`）。
- **疑问点**（4）：
  1. `read_timeout=300.0`（:45）——**唯一带读超时的 catalog 路由**（agent=None、todo/diff/children=None）；同族内不一致的理由未在文件内记录。
  2. 未传 `min_gzip_bytes`（同 agent.py 疑问 1）。
  3. 300s 超时同时充当 connect/read/write 的 read/write 值（`stream_upstream` 中 connect 固定 5.0，`_catalog_common.py:84-90`）——write 也 300s 对 GET 无意义但无害。
  4. `directory` 同样被全局目录忽略（docstring :26-28）——与 agent.py 疑问 4 相同的 Vary 放大。

---

### src/oc_slimapi/routes/health.py（141 行）

- **职责**：两个诊断端点。`GET /slimapi/health`（进程/契约自描述）与 `GET /slimapi/ready`（上游连通 readiness 探针）。
- **对外符号**：
  - `router`（:12）— `prefix="/slimapi"`。
  - `READY_VIEW = 3`（:19）— /ready 冻结到终态 v3 形（契约 §12：零 v4 差异）。
  - `health(request)`（:22-102）— `/slimapi/health`：`view = wire_view_from_scope(request.scope)`（:30，selector stash；无 selector 直调默认 3）。响应：`slimapi_contract=view`、`sidecar.{ok,version}`、`server.{api_version=view, accepted_client_versions}`、`schema.{degraded, version=view, clientMin, clientMax}`、`features = {**FEATURES, tokenStream: True, thresholdedSkeleton: True, skeletonInlineOutputMaxBytes, allowlist:{enabled[, droppedEvents]}}`；`view>=4` 时追加 `auxiliary`（dbaux.status().auxiliary_view()，无 dbaux → `{"available": False, "mode": "http"}`，:81-85）；`deploymentRevision` 可选（:87-89）。
  - `ready(request)`（:105-141）— `/slimapi/ready`：转发 `X-Request-ID`（:118-121）GET 上游 `/global/health`（timeout 5.0，:115-117）；`stash_up_in` 记账（:125）；`ok = status_code < 300`（:126）；任何异常 → `ok=False`（:127-128）；响应 `upstream.{ok, latencyMs}` + 冻结 v3 的 `server`/`schema` 块；HTTP 200 iff ok else 503（:141）。
- **/health 与 /ready 的差异**（重点）：/health 是**双视图**（?v=4 → v4 face 带 `auxiliary`；?v=3 → 字节等同 v3 终态），不触上游、恒 200；/ready **不 fork 版本**（`READY_VIEW=3` 硬编码，:109），触上游 ping、按上游状态返 200/503，且**不含** `slimapi_contract`/`features`/`auxiliary`。**readiness 聚合**：/ready 的聚合面只有上游 ping（<300 即 ok）+ `schema.degraded` 透传；v4 契约 §3.3 的九/十-ID readiness gate **不在 /ready**，而在 `GET /slimapi/versions` 的 `capabilities["4"].readiness`（src/oc_slimapi/readiness.py，经 versions.py:124 暴露）——两处 "readiness" 语义不同，易混淆。
- **依赖 / 被依赖**：依赖 `__version__`、`features.FEATURES`（静态全 True dict，src/oc_slimapi/features.py:15）、`gzip_util.json_response`、`selector.wire_view_from_scope`、`traffic.stash_up_in`、`upstream.forward_upstream_headers/request_id_from_scope`、app.state（config/schema_degraded/deployment_revision/hubs/dbaux）。两路由被 `traffic.bucketize` 归入 `health` 桶（src/oc_slimapi/traffic.py:103-104）。挂载于 app.py:760（第 1 位）。
- **状态 / 可变性**：无自身状态；全部读 app.state / config。`accepted_client_versions` 经 `list()` 拷贝（:40、:133）防外泄可变引用。
- **错误路径**：本文件无 CodedHTTPException 构造；/ready 上游失败 → 结构化 503（json body `upstream.ok=false`，非错误码 envelope）；/health 恒 200。`hubs.get_global()` 异常被裸 `except Exception` 吞为 `hub=None`（:96-98）→ 省略 `droppedEvents`。
- **疑问点**（8）：
  1. /health 受 selector 管控（需 `?v=3`/`?v=4`，无 v → 400 `unsupported_version`），/versions 无条件豁免——监控探针若不带 `?v=`，health 会 400 而 versions 200；部署侧（systemd/smoke）是否已适配待核（docs/operations.md）。
  2. `ok = response.status_code < 300`（:126）——3xx 重定向也算 ready；上游若被反代改写返回 301 仍报 ok。
  3. /health 与 /ready 响应**均未显式设 `Cache-Control: no-store`**（:102、:141 无 headers 参数）——对比 directories/versions 都设了 no-store；诊断端点可被缓存是否符合意图待核。
  4. :96-98 裸 `except Exception` 吞 `hubs.get_global()` 异常——无日志，静默丢 `droppedEvents`。
  5. `schema.clientMin/clientMax`（:49-50、:138-139）直接取 `accepted_client_versions[0]/[1]`——若 tuple 长度非 2 会 IndexError；config `_version_range` 保证恰 2 元（config.py:167-172），防线在别处。
  6. `features` 字面量键（`tokenStream`/`thresholdedSkeleton`/`skeletonInlineOutputMaxBytes`，:70-72）排在 `**FEATURES` 之后——若 FEATURES 未来加入同名键会被字面量覆盖（当前无冲突，顺序敏感是隐患）。
  7. /health 的 `auxiliary` 占位 `{"available": False, "mode": "http"}`（:85）与 dbaux 真实 reason 字段（dbaux/lifecycle.py:278-285 有条件带 `reason`）形状不对称——测试 app 与生产 wire 形状在此处分叉。
  8. /ready 的 `latencyMs` 含 `stash_up_in` 记账耗时（:125-130 之间）——测量值略大于纯上游 RTT；无关正确性，仅精度语义。

---

### src/oc_slimapi/routes/versions.py（162 行）

- **职责**：`GET /slimapi/versions` —— v4-contract §3 版本发现端点。无参数、无条件豁免 selector（selector.py:546-548）、非 GET → 405 + `Allow: GET`（selector.py:534-540，优先于一切）。
- **对外符号**：
  - `router`（:57）— `prefix="/slimapi"`。
  - `CURRENT_VERSION = SERVER_API_VERSION`（:61）— 常量 4（versioning.py:38）。
  - `AVAILABLE_VERSIONS: list[int]`（:62-64）— `list(range(3, 5)) == [3, 4]`，导入时固定。
  - `CAPABILITIES: dict[str, dict]`（:67-98）— `"3"` face：envelope/directoryQuery/versionHeaderOptional/writeRoutes/readRoutes（终态冻结）；`"4"` face 四静态键：`globalSessions/auxiliaryFilters/sseReplay(经 **META_CAPABILITY_KEYS)/qpImmediateFull`。
  - `EXPAND_FEATURE_ID = "messages.expand.v4"`（:101）。
  - `_capabilities4(satisfied=None)`（:104-132）— 组装 `"4"` face：`satisfied=None` → **运行时模块全局查找** `readiness_mod.SATISFIED`（:120-121，flip batch 零编辑传播）；`caps4["readiness"] = readiness_payload(sat)`（:124，恒 advertised）；`EXPAND_FEATURE_ID ∈ sat` → `caps4["expand"] = {categories: EXPAND_CATEGORIES, fragmentMaxBytes: settings.max_expand_response_bytes}`（:127-131，iff 双侧不变量）。
  - `versions(request)`（:135-162）— handler：`capabilities = {"3": {**CAPABILITIES["3"], "expand": {...live fragmentMaxBytes...}}, "4": _capabilities4()}`（:143-152）→ `json_response({current, available, capabilities, sidecarVersion}, headers={"Cache-Control": "no-store"})`（:153-162）。**无 ETag**（docstring :38-39 "discovery must always be revalidated"）；`Vary: Accept-Encoding` 经 json_response 协商族。
- **响应形状（重点——capabilities["4"] 静态性）**：`{"current": 4, "available": [3, 4], "capabilities": {"3": {envelope, directoryQuery, versionHeaderOptional, writeRoutes, readRoutes, expand{categories, fragmentMaxBytes}}, "4": {globalSessions, auxiliaryFilters, sseReplay, qpImmediateFull, readiness{...}, expand?{...}}}, "sidecarVersion": "__version__"}`。静态性边界：`"4"` 的**前四个键**冻结字面（不随 runtime/DB/replay-log 配置变化）；`readiness`/`expand` 随**代码版本**（readiness.SATISFied 的 flip batch）变化——当前 `SATISFIED = frozenset(REQUIRED)` 全集（readiness.py:93，2026-08-19 修订二 close-out）→ `ready=True`、`expand` 出现；`fragmentMaxBytes` 每请求从 Settings 读（env 可调，config.py:372 + 校验 config.py:860-866）——严格说是 **runtime-config 派生**，与 docstring :20-21 "never vary with runtime state" 存在措辞张力（注释 :137-140 已自认 live）。
- **依赖 / 被依赖**：依赖 `versioning.ACCEPTED_CLIENT_VERSIONS/SERVER_API_VERSION`、`readiness` 模块（REQUIRED/SATISFIED/readiness_payload，含未知 ID RuntimeError 守卫与依赖蕴含 ⑦ 守卫）、`config.settings`、`gzip_util.json_response`、`sse.replay_wire.META_CAPABILITY_KEYS`（= `{"sseReplay": True}`，replay_wire.py:96——meta 帧与 versions 通道同源不漂移）、`traffic.EXPAND_CATEGORIES`（12 类冻结表，traffic.py:50-63）、`__version__`。挂载于 app.py:760（第 2 位）。
- **状态 / 可变性**：模块级 `CURRENT_VERSION/AVAILABLE_VERSIONS/CAPABILITIES` 导入时冻结；请求期动态部分仅 readiness 集合与 settings 读取。无缓存。
- **错误路径**：本文件无错误构造点（端点永不因未知输入拒绝——根本不接受参数）；`readiness_payload` 内部对未知 ID 抛 RuntimeError（readiness.py:116-127）→ 会变成未捕获 500，但 SATISFIED 为字面常量，结构上不可达。
- **疑问点**（7）：
  1. `AVAILABLE_VERSIONS/CURRENT_VERSION` 源自 versioning **常量**，而 /health 的 `accepted_client_versions/clientMin/clientMax` 源自 **config**（可被 env 解析）——双源；被 config.py:817-822 的 fail-closed 校验（必须恰等 `(3,4)` 否则启动 RuntimeError）钉死对齐，当前无分歧路径，但一致性靠校验维持而非单一来源。
  2. `_capabilities4(satisfied)` 的参数是测试后门（docstring :118 "tests pass explicit sets"）——生产恒 None；若未来被误用于注入非全集集合，模块级守卫只在 readiness.py 侧，本文件无二次校验。
  3. `"3"` face 的 `expand` 无条件存在且 `fragmentMaxBytes` 为 live 配置（:147-149）；`"4"` face 的 `expand` 受 iff 门控（:126-131）——两 face 的 expand 存在性规则不同（3 无条件 / 4 iff），契约 §0.5 v3 冻结与 §14 的组合结果，审计需确认客户端不假设对称。
  4. `CAPABILITIES` 为可变模块级 dict 且被 `_capabilities4` 以 `dict(CAPABILITIES["4"])` 浅拷贝（:123）——外层防改，但内层值（列表/bool）仍共享引用；当前全为不可变标量，无实际风险。
  5. `sidecarVersion` 在未安装 checkout 上为 `"0.0.0+unknown"`（`__init__.py:9`）——发现端点会广播占位版本；运维应保证生产经 pip 安装。
  6. docstring :8 "Non-GET → 405 + Allow: GET (enforced by the selector middleware)"——该行为不在本文件，审计断言依赖 selector.py:534-540 的实现顺序（①versions 405 优先于版本 400），跨文件耦合。
  7. `AVAILABLE_VERSIONS` 是 `list`（可变类型作模块常量，:62）——无 `Final`/tuple 保护，风格上可被意外 mutate。

---

### src/oc_slimapi/qp_sweep.py（250 行）

- **职责**：B1b 阶段 1 **shadow-only** q/p sweep 调度器：模块内零上游客户端、零 HTTP 操作；每次 touch 只记录"未来真实 sweep 会做什么"的 marker，使调度节奏与预算可在生产安全观察。
- **对外符号**：
  - `_EVICTION_AFTER = 30*86400`（:20）、`_MAX_SLEEP_SECONDS = 30.0`（:21）。
  - `QpSweepShadow`（:24-250）：
    - `ESTIMATED_DIRECTORY_BYTES = 2*1024`（:34）— shadow 估算每目录 sweep 字节成本。
    - `__init__(*, activity=None, directories=(), directory_source=None, interval_seconds=1800.0, daily_budget=100, enabled=True, now=time.time, jitter=None, eviction_after_seconds=30d)`（:36-80）— 参数校验：interval<=0 / budget<0 / eviction<=0 → `ValueError`（:49-54）；`markers = deque(maxlen=256)`（:73）。
    - `activity` property（:82-84）、`task` property（:86-88）、`running` property（:90-92）。
    - `observe_directory(directory, *, now=None)`（:94-107）— 非 str/空串静默返回；新目录 `_next_run[dir]=timestamp`（下次扫描即评估）；运行中 set `_wake_event`。
    - `record_activity(directory, *, now=None)`（:109-114）— 写 `_activity[dir]` + observe。
    - `record_request_activity(directories, *, now=None)`（:116-122）— 一次聚合 q/p 请求对其全部目录记 activity（questions.py:218-220、permissions.py:234-236 调用）。
    - `_ingest_directory_source()`（:124-131）— 遍历 `_activity` 以旧时间戳 observe；再拉 `directory_source()`（item 取 `getattr(item, "directory", None)`）。
    - `next_delay()`（:133-135）— `interval * clamp(jitter(), 0.8, 1.2)`（默认 jitter uniform(0.8,1.2)，:68）。
    - `_utc_day`（:137-139）— UTC 日 ISO 串。
    - `_reset_budget_if_needed`（:141-145）— 跨 UTC 日清零 `_budget_used`。
    - `_due_directories`（:147-153）— `next_run <= now`，按字典序排序遍历。
    - `_evict_stale_directories`（:155-164）— `now - seen_at >= 30d` 的目录从三表移除。
    - `run_once(*, now=None)`（:173-209）— **shadow 决策核心**：ingest → evict → reset budget → 对每个 due 目录：`_triggers_total+=1`；`elapsed = now - last_activity`（activity 优先、seen_at 兜底，:182）；`elapsed < interval*3` → `skip`；`_budget_used >= daily_budget` → `budget_exhausted`；否则 `cold`（`would_sweep=True`、`_budget_used+=1`、`_est_bytes_total += 2KB`）；marker 入 deque；**每目录重排 `next_run = now + next_delay()`（含 skip/exhausted）**（:208）。
    - `_run()`（:211-220）— 主循环：sleep 至最近 deadline（cap 30s；`_wake_event` 可提前唤醒）→ `run_once()`。
    - `start()`（:222-226）— `enabled=False` 或已运行 → 返回现 task；否则 `asyncio.create_task(_run(), name="qp-sweep-shadow")`。
    - `stop()`（:228-237）— cancel + await，吞 CancelledError。
    - `metrics()`（:239-247）— 六键：`triggers_total/cold_hits/skips/budget_exhausted/est_bytes_total/known_directories`。
    - `snapshot()`（:249-250）— metrics + `markers` 列表。
- **阶段 1 shadow 逻辑与 metrics 暴露（重点）**：无真实 sweep——`would_sweep` 只是 marker 布尔；"cold" 判定 = 该目录 3×interval 无 q/p 活动（:184 `elapsed < self.interval_seconds * 3` 硬编码系数 3）；预算按 UTC 日重置。**metrics 暴露**：`GET /slimapi/metrics` 的 `hubs["sweep"]` 块 = `qp_sweep.metrics()` 六计数键（src/oc_slimapi/routes/metrics.py:42-44，`app.state.qp_sweep` 存在且 `enabled` 时）；**含 markers 的 `snapshot()` 在生产代码无调用方**（仅 tests/test_b1b_sweep_shadow.py）——marker 明细实际不可经 wire 观察。
- **依赖 / 被依赖**：仅标准库（asyncio/random/time/collections/datetime）。被装配于 `src/oc_slimapi/app.py:501-525`（`qp_sweep_enabled` 时：`activity=global_hub.qp_last_activity` **共享可变 dict 引用**、`interval/budget` 来自 settings `OC_SLIMAPI_QP_SWEEP_*`（config.py:622-630）、`global_hub.set_directory_observer(qp_sweep.observe_directory)` 同步观察每个有效事件目录、lifespan stop 回调；disabled → `app.state.qp_sweep = None`）；消费方 questions.py:218、permissions.py:234、metrics.py:42-44。预留桶 `/slimapi/_shadow/sweep`（traffic.py:106-108）无实际路由。
- **状态 / 可变性**：全部可变状态集中于实例（`_known_dirs/_seen_at/_next_run` 三表 + 计数器 + deque + `_budget_day/_budget_used` + `_task/_wake_event`）；`_activity` 与 GlobalHub 共享引用（外部可写）。单事件循环假设下无锁（方法全同步）。
- **错误路径**：构造期 `ValueError`（:49-54）；运行期无异常构造点——`observe_directory` 对非法输入静默 return（:95-96）、`_ingest_directory_source` 对非 str 对象 `getattr(..., None)` 后同样静默（:130-131）。
- **疑问点**（10）：
  1. `_ingest_directory_source`（:125-126）用 activity dict 中**旧时间戳**作 observe 的 now——若 hub 重启后 `qp_last_activity` 残留旧值，目录 observe 即刻携带旧 `seen_at`，`run_once` 开头的 `_evict_stale_directories`（:177）可能**当轮即逐出**刚 observe 的目录（30d 活动旧值），行为正确但微妙，值得测试覆盖（tests 已有部分）。
  2. `directory_source` 分支（:127-131）在生产装配（app.py:504-508）未传入——当前为死代码路径；保留 API 但无调用方。
  3. 预算按 **UTC 日**重置（:138-139）而注释/命名说 "daily"——本地时区部署的运维对账需注意日界限。
  4. "cold" 阈值 `interval*3`（:184）魔法数硬编码，未配置化也未命名常量。
  5. `_run()` 主循环（:211-220）**无 try/except**——`run_once` 若抛未预期异常（如 jitter 回调抛错），task 静默死亡且无日志、无重启；当前纯内存逻辑难触发，但调度器无自愈/可观测死亡是结构缺口。
  6. `stop()` 后 `_task=None`（:230），`start()` 可再启——生产只用一次；重复 start/stop 循环下的 `_wake_event` 残留 set 状态未清理（`_run` 开头 clear 在 delay>0 时才发生）。
  7. `next_delay` 对 jitter 结果再 clamp（:134）与默认 uniform(0.8,1.2) 双重保险——若注入自定义 jitter 越界会被静默截断，测试语义可能失真。
  8. `metrics()` 的 `est_bytes_total` 以每目录恒 2KB 估算累加（:197）——粗估模型，无 README 级别说明（仅类常量名自述）。
  9. markers 观测断层：deque 保存 256 条 marker 但生产唯一出口 `metrics()` 只暴露聚合计数（metrics.py:44）——`snapshot()` 的 markers 无 wire 出口（traffic.py:106 的 `_shadow/sweep` 桶是预留无路由）；"观察 cadence" 的阶段 1 目标实际只能看到计数，看不到 per-directory 节奏。
  10. `record_request_activity`（:116-122）由 q/p 请求路径同步调用（questions/permissions handler 内）——每次遍历全部目录写共享 dict；目录数大时（上限受 discovery 10k 影响）在请求热路径上的同步开销未设上限。

---

### src/oc_slimapi/turn_registry.py（314 行）

- **职责**：turn token 强 fence——给转发的 `session.digest` SSE 事件盖 `turnIncarnation` + `turn` 两个平顶层字段，供 ocdroid 做因果栅栏。冻结决策：O2 策略 A（`persisted_last+1`，单进程免文件锁）、O3 单实例（scope key = sid alone）、O4 turn 不持久化（重启清零，靠 incarnation 抬升）、S2 commit 点 = bump-before-send（发送失败留洞——契约 §4.2 的批准放宽）。
- **对外符号**：
  - `_FALLBACK_INCARNATION = 1`（:49）；`_TURNS_MAX = 10_000`（:61，LRU 上限）；`_INCARNATION_FILENAME = "incarnation"`（:64）。
  - `IncarnationStore`（:67-200）：
    - `__init__(state_dir, legacy_state_dir=None)`（:90-98）— 新路径优先，legacy（旧 access_log 目录）仅在新路径 missing/corrupt 时回读。
    - `_read_path(path)`（:100-128）— missing/None 路径 → `(False, 0)` 静默；OSError/空串/非整数/负数 → `(False, 0)` + warning（各 :112-127）。
    - `load_or_bump()`（:130-149）— 读新 → 不 valid 再读 legacy → `base+1`；**只写新路径**；写失败仍返回内存值 + warning（:143-148）。
    - `_write_persisted(inc)`（:151-200）— 原子写：mkdir → 写兄弟 `.tmp` → `flush` + **`os.fsync`** → `os.replace`（POSIX 原子）；失败清理 tmp、warning、返回 False，**永不 raise**（防 restart-incarnation-reuse：半写文件会被解析为 corrupt → 回退 0 → 复用 incarnation 1）。
  - `TurnRegistry`（:203-277）：
    - `__init__(incarnation)`（:231-233）— `incarnation` 进程期冻结；`_turns: OrderedDict[str, int]`。
    - `bump_turn(sid)`（:235-263）— `+1`、`move_to_end`（LRU 刷新）；`len > 10_000` 时 `popitem(last=False)` 逐出 + observability warning（:254-262，B7/P1-23：行为不变仅加日志——新 incarnation 疗法被否决因 incarnation 是进程级冻结）。
    - `snapshot(sid)`（:265-277）— 恒返 `(incarnation, turn)` 二元组；未观察 sid → `(incarnation, 0)`；调用方（GlobalHub.publish）在 DigestFields 上冻结值，后续 bump 不回溯（契约 §7.4/V10）。
  - 路径分类器（自退役 catch-all 迁入，:280-314）：
    - `_SESSION_SID_RE`（:283）、`_TURN_BUMPING_SUFFIX_RE`（:284-285，`/session/{sid}/(prompt_async|abort)/?`，容忍尾斜杠）。
    - `extract_sid_from_path(norm_path)`（:288-299）— `/session/{sid}/…` 首段；非 session 路径 / 空 sid → None；不硬编码上游 id 格式。
    - `is_turn_bumping_path(norm_path)`（:302-314）— 仅路径匹配；**POST 检查留给调用方**；sync `/session/{sid}/prompt` 不再识别（M3-3：从未收集、catch-all 已关）。
- **盘上计数与 sweep（重点）**：**盘上计数** = incarnation 单行整数文件（state_dir/incarnation，app.py:582-584 注入 `settings.state_dir` 与 legacy access_log 目录），启动一次 read→+1→原子写回；turn 本身**不落盘**（O4）。**"sweep"** 对应 `_turns` 的 LRU 逐出（`_TURNS_MAX=10_000`，:254-262）——已知上界权衡：同 incarnation 内被逐出 sid 再 bump 会从 1 重来（lex-LOWER 回归，客户端 fence 视为 stale 直到爬回原值）；披露为"实际不可达（需单进程 >10_000 个 distinct bumped sids）"。
- **依赖 / 被依赖**：依赖 `logging_config.get_logger`（:41-43）。被依赖：`app.py:580-588`（lifespan 构造 + `hubs.set_turn_registry`）；`routes/write_groups.py:182-186`（`POST` + `is_turn_bumping_path` → `extract_sid_from_path` → `bump_turn(sid)`，**send 之前**）；`sse/global_hub.py:712-713`（publish 时 `snapshot(session_id)` 盖章）；`routes/sessions.py:250-254、870-874`（status 路径 per-caller merge turn 字段）；`sse/registry.py:104-137`（set_turn_registry 传播至 live hub）；`proxy.py:19-20` 仅注释提及。
- **状态 / 可变性**：`IncarnationStore` 持两路径（不可变）；`TurnRegistry.incarnation` 冻结、`_turns` 可变 OrderedDict。全部同步纯 dict 操作，单事件循环免锁（契约 §7.2）。
- **错误路径**：无 HTTP 错误码构造点；全部 I/O 失败降级为 warning + 内存值（`_read_path`/`load_or_bump`/`_write_persisted`）。`validate` 类 RuntimeError 不在本文件（在 readiness.py）。
- **疑问点**（8）：
  1. incarnation 回退窗口（:137-139 + :143-148 组合）：上次进程成功持久化 inc=N 后**新路径文件损坏** → 回读 legacy M（可能 < N）→ 新 inc = M+1 ≤ N——同 fence 值复用。需要"新文件在成功写入后被外部损坏"才触发，概率极低，但 docstring 只披露了"写失败"路径未点名这条"损坏后 legacy 回退"链。
  2. legacy 文件永不删除（:136 注释）——迁移完成后成为永久回退隐患（与疑问 1 同链）。
  3. `load_or_bump` 写失败时返回内存 inc（:143-148）——若随后进程长期运行并再次重启，文件仍 stale，两次重启可能拿到相同 inc（注释已自认 "restart may re-read a stale value"）。
  4. `bump_turn` 留洞语义（S2 放宽）——`send` 抛错时 turn 已前进；审计下游应确认 ocdroid 侧 lex 比较确实容忍洞（docstring :24-26 声称已批准）。
  5. LRU 逐出回归（:254-262）只在**逐出发生时**告警，被逐 sid 后续 bump 回 1 时**无第二次告警**——实际回归发生点不可见。
  6. `is_turn_bumping_path` 不校验 method（:311-313 自述），`write_groups.py:182` 负责 `POST` 检查——若未来新增调用方漏检 method，GET 也会 bump；契约防线在调用方而非分类器。
  7. `extract_sid_from_path` 对 URL 编码 sid 不解码（正则原始段）——与 write_groups 侧 norm_path 的构造方式耦合（须确认上游路径未 percent-encode sid；一般 zod/路由下不会，但未见断言）。
  8. `IncarnationStore` 无防重入（`load_or_bump` 假设 startup 期无并发，:77-78）——若未来 lifespan 变并行或被测试重复调用，read→bump→write 可能交错；当前 app.py 只调一次（:582-586）。

---

### src/oc_slimapi/logging_config.py（146 行）

- **职责**：`oc_slimapi` 根 logger 的 stderr StreamHandler 配置（`OC_SLIMAPI_LOG_LEVEL` env 驱动、幂等、uvicorn 热重载安全）+ 独立的 `oc_slimapi.actions_audit` WARNING 级审计 logger + `get_logger` 前缀工厂。不触碰 `oc_slimapi.access` logger（由 access_log 的 DailyAccessHandler 管理）。
- **对外符号**：
  - `_ROOT_LOGGER_NAME = "oc_slimapi"`（:26）、`_AUDIT_LOGGER_NAME`（:27）、`_setup_lock`（:28，threading.Lock）。
  - `_resolve_log_level()`（:31-52）— 读 env（默认 "INFO"）大写化后查 `logging._nameToLevel`（stdlib 私有表，:44）；无效值 → warning + 回退 INFO。
  - `setup_actions_audit_logging()`（:55-85）— audit logger：`setLevel(WARNING)` + `propagate=False`（:67-68，独立于根级）；锁内幂等检查（已有 stderr StreamHandler 则 return，:71-75）；固定 WARNING handler + 统一 formatter。
  - `setup_logging()`（:88-123）— 根 logger setLevel(env)；锁内幂等检查（:104-110）；DEBUG 级 handler 让 logger 级做门（:113）；末尾引导 audit logger（:123）。
  - `get_logger(name)`（:126-137）— 已带 `oc_slimapi` 前缀则原样，否则加前缀。
  - `redact(secret)`（:140-146）— 恒返 `"<redacted>"`；**测试专用，无生产调用方**（:143-145 自认，tests/test_logging_config.py:19 钉契约）。
- **依赖 / 被依赖**：仅标准库。被依赖极广：`app.py:24`（setup_logging 于 :195 lifespan 启动）、`turn_registry.py:41-43`、`sse/replay_wire.py:255-257`、`sse/tokenstream/hub.py:83-111`、`dbaux/projection.py:51-59`；测试 tests/test_logging_config.py、tests/test_sse_logging.py:15。
- **状态 / 可变性**：模块级 `_setup_lock`；logger/handler 状态由 logging 全局管理；幂等检查以 `h.stream is sys.stderr` 身份判定（:72-74、:107-109）。
- **错误路径**：无异常路径——env 无效降级 INFO（:46-52）；audit 与主 setup 均不 raise。
- **疑问点**（5）：
  1. `logging._nameToLevel`（:44）是 stdlib **私有** API——注释自认知，但 CPython 升级无兼容承诺（虽长期稳定）。
  2. `setup_actions_audit_logging` 的 `setLevel/propagate`（:67-68）在锁外、幂等检查在锁内——微小竞态窗口（值恒定无实际危害，仅次序不严格）。
  3. `NOTSET` 被列为合法值（docstring :39）——根 logger 设 NOTSET（=0）等于全量放行 DEBUG，与运维对 "NOTSET=继承" 的直觉相反（根上无继承语义）。
  4. `_resolve_log_level` 的回退 warning 在 handler 装配**之前**发出（:47-51）——经 lastResort 输出 stderr，实际可见，但绕过统一 formatter。
  5. 幂等判定用 `h.stream is sys.stderr` 身份比较——若 uvicorn/reload 或测试替换 `sys.stderr` 对象，旧 handler 不再匹配 → 可能叠加第二个 handler；现实中低风险（测试已覆盖幂等主线）。

---

### src/oc_slimapi/__init__.py（9 行）

- **职责**：包版本单一来源：`__version__ = importlib.metadata.version("oc-slimapi")`（dist-info，release.sh bump 自动传播）；未安装 checkout 回退 `"0.0.0+unknown"`（`PackageNotFoundError`，:8-9）。
- **对外符号**：`__version__`（:7/:9）——唯一符号。
- **依赖 / 被依赖**：仅标准库 importlib.metadata。被 `routes/health.py:5`（sidecar.version）、`routes/versions.py:49`（sidecarVersion）、app.py 等消费。
- **状态 / 可变性**：导入期一次性求值，不可变字符串。
- **错误路径**：无（PackageNotFoundError 已捕获）。
- **疑问点**（2）：
  1. 回退值 `"0.0.0+unknown"` 会原样进入 `/slimapi/health.sidecar.version` 与 `/slimapi/versions.sidecarVersion`——裸源码运行时客户端/运维看到占位版本，无标记区分"未安装"与"真实 0.0.0"（PEP 440 本地版本段 `+unknown` 略有区分，但依赖消费方解读）。
  2. 模块导入即触发 dist-info 查询（每次进程一次，开销可忽略）——无缓存问题，仅记录。

---

### src/oc_slimapi/middleware/__init__.py（1 行）

- **职责**：ASGI middleware 包标记（docstring："oc-slimapi ASGI middleware."）。
- **对外符号**：无——包内 `request_id.py`、`traffic_accounting.py` 由调用方按子模块路径直接导入，不经 `__init__` 再导出。
- **依赖 / 被依赖**：无依赖；被依赖：app.py 装配 middleware 时直接 import 子模块（`middleware/request_id.py`、`middleware/traffic_accounting.py`）。
- **状态 / 可变性**：无。
- **错误路径**：无。
- **疑问点**（1）：
  1. `__init__` 不导出子模块公共符号——新调用方需知道完整子模块路径；纯风格，无再导出约定（`selector.py` 位于包根而非 middleware/ 下，命名归属略有不一致——selector 事实上的 ASGI middleware 却不在 middleware 包内）。

---

### src/oc_slimapi/routes/__init__.py（1 行）

- **职责**：thin API 路由包标记（docstring："Thin API route package."）。
- **对外符号**：无——app.py:29-30 直接 `from .routes import actions, agent, children, command, directories, events, health, messages, metrics, permissions, questions, read_groups, sessions, todo, token_stream, versions, write_groups` + `from .routes import diff as diff_routes`。
- **依赖 / 被依赖**：无依赖；被 app.py:760 的挂载循环消费（18 个 router 按固定顺序注册：health → versions → actions → agent → command → sessions → children → todo → diff → messages → events → metrics → questions → permissions → directories → token_stream → read_groups → write_groups）。
- **状态 / 可变性**：无。
- **错误路径**：无。
- **疑问点**（1）：
  1. router 挂载顺序散落在 app.py:760 一处元组里（本包 `__init__` 不提供 `ROUTERS` 聚合）——顺序敏感（如 token_stream 须在 catch-all 之前，INTERFACE_MAP.md:13 说明）但无本地单一常量可查；新增路由改 app.py 元组 + INTERFACE_MAP 双处，漂移由 check_routes_doc.py 拦截路由↔文档一致性，但**顺序**本身无守卫。

---

## 汇总

| 文件 | 行数 | 疑问点数 |
|---|---|---|
| routes/directories.py | 202 | 7 |
| routes/diff.py | 110 | 5 |
| routes/todo.py | 84 | 4 |
| routes/agent.py | 52 | 4 |
| routes/children.py | 90 | 4 |
| routes/command.py | 49 | 4 |
| routes/health.py | 141 | 8 |
| routes/versions.py | 162 | 7 |
| qp_sweep.py | 250 | 10 |
| turn_registry.py | 314 | 8 |
| logging_config.py | 146 | 5 |
| __init__.py | 9 | 2 |
| middleware/__init__.py | 1 | 1 |
| routes/__init__.py | 1 | 1 |

疑问点合计：70。

<!-- ==== e1-06-app ==== -->
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

<!-- ==== e1-18-assets ==== -->
# E1-18 资产文件精读卡片（scripts / deploy / pyproject）

> 审计探索卡片，2026-08-20。只读精读产物；引用格式 `路径:行号`。
> 交叉核对源：`src/oc_slimapi/config.py`（Settings 默认值）、`src/oc_slimapi/versioning.py`、`tests/v4_fixture.py`、`tests/test_eqp_matrix.py`、`docs/operations.md`。

---

### scripts/check.sh（37 行）

- **职责**：改动校验质量门禁（AGENTS.md 规定的必做入口）；pytest + 路由↔INTERFACE_MAP 一致性 + compileall。

- **关键行为（检查项清单，按执行序）**：
  1. `set -euo pipefail`（`scripts/check.sh:9`）；`ROOT` 定位 + cd（10-11）。
  2. venv 存在性：`.venv/bin/python` 不存在 → 报错提示 `python -m venv .venv && pip install -e '.[test]'` 并 `exit 1`（13-17）。
  3. `pytest tests/ -q`（21-22）。
  4. 路由↔文档一致性：`python scripts/check_routes_doc.py`（24-25）。
  5. `python -m compileall -q src`（27-28）。
  6. `MODE` 参数校验：`--full|default|""` 合法（`--full` 为兼容别名、行为等价默认，7、30-32）；其他值 → 用法提示 + `exit 1`（33-34）。
  7. 全过 → `✅ check.sh 通过`（37）。

- **失败处理**：任一步非零由 `set -e` 立即中止（退出码透传）；venv 缺失显式 `exit 1`（14-17）。

- **疑问点**：
  1. `MODE` 校验放在三步检查**之后**（30-35）：传错参数仍会完整跑完 pytest + 对账 + compileall 才报 usage——参数校验应前置（浪费但不误放行）。
  2. `compileall src` 会在 `src/` 下产生 `__pycache__` 副产物，无清理步骤（27-28；仓内已有 `scripts/__pycache__` 同类残留）。
  3. `--full` 与默认完全等价（7）：`docs/release.md:16` 的门禁描述（compile + unit）与实际三项一致，无差异化 full 模式——`--full` 仅为兼容保留，是否还需要保留可议。
  4. 不运行 `scripts/measure_token_overhead.py` 与 `scripts/eqp_matrix.py` 的 CLI 模式（后者仅经 pytest 间接复用 draft 矩阵函数）；测量报告数字（见 5b 卡）无 CI 防漂移。

---

### scripts/release.sh（96 行）

- **职责**：发版唯一入口（semver 推算 + CHANGELOG 门禁 + pyproject 版本写回 + release commit + annotated tag；不自动 push）。规范权威 `docs/release.md`（2-3）。

- **关键行为（步骤序列）**：
  1. 参数校验：`TYPE` 缺失即用法错误（22）；必须 `^(patch|minor|major)$`（23）。
  2. git 前置：当前分支必须 `main`（26-27）；工作区干净检查 `git diff --quiet HEAD` + `git diff --cached --quiet`，脏则打印 `git status --short` 并 `exit 1`（29-33）——**只查已跟踪改动，untracked 文件不拦**。
  3. 质量门禁：`./scripts/check.sh`（35-37），失败即中止（`set -e`）。
  4. 版本解析：`sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1`（42-43）；解析失败 `exit 1`（43）。
  5. 版本推算：`IFS='.' read MAJOR MINOR PATCH` + case 递增（44-50）。
  6. **CHANGELOG 门禁（失败关闭）**：`grep -qE "^## \[${VERSION}\]" CHANGELOG.md` 不命中 → 提示先把 `[Unreleased]` 整理进 `## [X.Y.Z] - YYYY-MM-DD` 并 `exit 1`（54-59）——**发生在写 pyproject 之前**，即 changelog 校验缺失时发版中止、仓库零改动。
  7. 版本写回：Darwin/Linux 两分支 `sed -i "0,/^version = .../..."` 只替换第一个 `version = "..."` 行（61-67）；写回后 `grep -q "^version = \"${VERSION}\""` 复核，失败 `exit 1`（68）。
  8. commit：`git add pyproject.toml CHANGELOG.md` + `git commit -m "release: v${VERSION}"`（71-73）；注释明确"只收版本+changelog"，契约等其余改动由调用方事先 commit（72）。
  9. annotated tag：`mktemp` + `trap rm EXIT`（76-77）；awk 提取 CHANGELOG `## [VERSION]` 节（含标题行自身，到下一 `## [` 前或文件尾）（78-83）；提取为空 → `exit 1`（84）；`git tag -a "$TAG" -F "$NOTE_FILE"`（85）。
  10. 人工 push：只打印 `git push origin main && git push origin $TAG` 与 Gitea Release 建议，**不自动 push**（88-96）。

- **失败处理**：`set -euo pipefail`（18）兜底；全部关键失败点（参数/分支/脏树/门禁/解析/CHANGELOG/写回/tag note）均显式 `exit 1`，且 CHANGELOG 门禁先于任何仓库写操作。

- **危险操作防误触**：不自动 push（16、88-96）；必须 main 分支（27）；必须干净树（29-33）；commit 范围限 pyproject+CHANGELOG（71）；tag 已存在时 `git tag -a` 自身失败（未显式预检）。

- **版本一致性检查**：CHANGELOG `## [X.Y.Z]` 节存在性（55）+ pyproject 写回复核（68）。**不检查**：`[Unreleased]` 节是否已清空、CHANGELOG 日期格式（仅注释建议，57）、pyproject 与代码内常量（代码无版本常量，双轨设计下无需）。

- **疑问点**：
  1. 脏树检查不覆盖 untracked 文件（29-33）：untracked 改动不会进 release commit（add 限定两文件），风险有限，但"工作区干净"语义与 `git status` 直觉不完全一致。
  2. 版本解析用 `head -1` 匹配任意 `^version = "..."` 行（42）：当前 pyproject 中 `[project] version` 是唯一首个匹配；未来若其他段引入顶层 `version =` 行会解析错位（与 sed 首个替换策略 62-66 一致，故不会错位写回，仅解析源可能漂）。
  3. `PATCH=$((PATCH + 1))`（46-48）对带后缀版本（如 `4.4.0rc1`）会算术失败——当前纯 semver 无影响，无前置格式校验。
  4. 未预检 tag 是否已存在（85）：重复发版同版本会在 `git tag -a` 处失败，报错来自 git 而非脚本自身的友好提示。
  5. tag note 含 `## [VERSION]` markdown 标题行（79-83 的 `p {print}` 在置 p=1 后连标题行一并打印）——tag message 首行是节标题而非摘要，风格问题。

---

### scripts/check_routes_doc.py（322 行）

- **职责**：路由 ↔ `docs/specs/INTERFACE_MAP.md` 一致性校验（防漂移 enforced gate）：`src/oc_slimapi/routes/*.py` 每条 `/slimapi/**` 路由必须在文档**表行**中有记录且 HTTP method 一致（2-6）。

- **对账逻辑**：
  - **代码侧（查什么）**：`ast` 遍历 `routes/*.py`（162-180），收集 `@router.<attr>(path)` 装饰器——`get/post/put/patch/delete/head/options`（`_METHOD_ATTRS`，57）+ `api_route(methods=[...])`（102-129、141-150）；多行装饰器天然覆盖（153-160）；router 前缀取自 `APIRouter(prefix="...")` 正则（53、95-99，app.include_router 无额外前缀的约定，52）；文件级 `SyntaxError` 静默跳过（169-170），无 router 定义的模块（如 `__init__.py`）跳过（165-166）。
  - **文档侧**：只解析以 `|` 开头的物理表行，从首单元格 `**<METHOD> `<path>`**` 提取 `(method, path)`（60-62、188-204）；prose/历史段/删除区的路径提及**不**满足校验（防 P1-16 rev-2 (a)：删路由后文档别处残留路径字符串，15-18）。
  - **校验 1 存在性**：代码声明的 `(method, prefix+path)` 不在文档表行 → missing（261-264）。
  - **校验 2 method 一致**：path 在但 method 不同（如 GET 改 POST）→ method_mismatch（265-266）。
  - **校验 3 语义（`SEMANTIC_CHECKS` 白名单，7 条，71-87）**——该路由的文档表行（路径边界正则 `(?![\w/])` 防前缀假匹配，207-209；多行命中则 join 后整体验证，212-233）须包含关键词子串：
    1. `/slimapi/sessions` → `upstream_http_`、`upstream_unavailable`（72）
    2. `/slimapi/messages/{sid}` → `session_not_found`、`upstream_http_`、`upstream_unavailable`、`transform_busy`（73-75）
    3. `/slimapi/messages/{sid}/full/{mid}` → 同上 4 关键词（76-78）
    4. `/slimapi/messages/{sid}/expand/{category}/{mid}` → `expand`、`EXPAND_CATEGORIES`、`12`（79-81）
    5. `/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}` → 同上 3 关键词（82-84）
    6. `/slimapi/command` → `upstream_http_`、`upstream_unavailable`、`transform_busy`（85）
    7. `/slimapi/agent` → 同 command 3 关键词（86）
    白名单刻意保守（25-29）；expand 两路由守卫实际为后两关键词（路径本身恒含 `expand`，68-70）。无匹配表行时返回空（存在性校验兜底，229-231）。
  - **不查什么**：① 反向漂移（文档多列路由、代码已删）不报错；② 非 `routes/*.py` 的动态注册（`app.add_api_route` 等）看不到——已知局限，当前所有 `/slimapi` 路由均静态声明，改运行时遍历会引入 import 副作用，收益不抵风险（34-38）；③ SEMANTIC_CHECKS 之外路由的语义；④ path 参数命名差异（`{sid}` 按字面匹配）。
  - `validate()` 为纯函数无 I/O，便于单测（237-247）——确有 `tests/test_check_routes_doc.py` 覆盖（实测 grep 确认存在）。

- **失败处理**：退出码 0=通过 / 1=缺失或 method 不一致或语义不符（分类打印明细 + 修复指引，286-312）/ 2=文档文件缺失（279-281）。

- **疑问点**：
  1. `scripts/check_routes_doc.py:311` 的语义失败修复提示让对齐 **`docs/specs/v2-contract.md` §7**——v2 语义 3.0.0 已退役（AGENTS.md），权威是 v3-contract；提示文本陈旧。
  2. 关键词 `"12"` 为纯子串匹配（80-84）：文档行内任意 `12` 数字（如字节数 12288、年份）即可命中，实际守卫只有 `EXPAND_CATEGORIES`；若想锁类目计数，正则应更严。
  3. 文档侧 `_DOC_METHOD_PATH_RE.finditer` 扫整行（201-203）：一条长表行内若并列出现多个 `**METHOD `path`**` 标题（如对照说明），全部计入 `by_path`，可能放宽 method_mismatches 判定（代码 GET、同行含 GET+POST 标题即通过）——边界情况，当前文档形状未触发。
  4. `_PREFIX_RE` 只认双引号字面量 `prefix="..."`（53）：若某 router 改用单引号/f-string 前缀会静默漏采（该文件路由整体漏检，而非误报）。
  5. `SyntaxError` 静默 `continue`（169-170）：routes 文件语法损坏时该文件路由全部漏检——但 compileall/pytest 会另行失败，实际有兜底；仍属静默吞错路径。

---

### scripts/eqp_matrix.py（564 行）

- **职责**：B0-6(b) EQP 全过滤矩阵 + 真库 P99 实证脚本（`docs/system-architecture-proposal-2026-08-17.md` §3.1 / `design-v4-dbaux.md` 配套）：实证 v4 `/slimapi/sessions` DB 投影源在「sidecar 零索引」前提下 48 组合的 planner 特征 + 真库无索引直跑基线（1-28）。

- **关键行为**：
  - **48 组合** = archived(omit/only/all) × parent(all/none/only/s0000) × cursor(y/n) × search(y/n)（86-98）。
  - 投影 SQL 模板 `SQL_TMPL`（64-76）：`LEFT JOIN project p ON s.project_id=p.id`；keyset 复合谓词 `(s.time_updated, s.id) < (:cursor_t, :cursor_i)`（SQLite ≥3.15，123-124）；`LIMIT :limit + 1`；search 为 `:search IS NULL OR s.title LIKE :search ESCAPE '\'`（72）。
  - join 列冻结 `worktree`：`build_sql` 对其他值直接 `ValueError`（132-134，v2.2 行 74 directory 列真库不存在，rev-1 关闭）。
  - 列名以真库 PRAGMA 为准（16-20、57-59）：tokens_input/output（非 v2.2 模板的 tokens_in/out）；project join 列契约冻结 `{id,name,worktree}`（59）。
  - **草稿库模式**：`mkdtemp` 建 /tmp 临时 WAL 库（194、231 复刻上游 `database.ts:27`）；确定性数据分布——每 5 行 1 根会话、title=`grp{i%4}-{i:04d}`（search 命中 25%）、每 10 行 3 行归档（30%）、38 个 project 行、`time_created=time_updated=base+i` 单调（197-227）；keyset 锚点取排序中位行（254-256）。
  - **逐组合断言**：EXPLAIN QUERY PLAN 结构特征 = `SCAN session` + `USE TEMP B-TREE FOR ORDER BY`（parse_eqp 只断言结构不断言全文案，S-B08，309-329）+ 行数与前 K id 与 Python 镜像 oracle `expected_window`（277-302）精确匹配（384-391）。
  - **真库模式**：URI `mode=ro`（429，只读铁律）+ `PRAGMA query_only=ON`（431，防御层）+ `busy_timeout=5000`（432，对齐上游 `database.ts:29`）；schema 兼容门用 `PRAGMA table_info` 对照 `SESSION_PROJECTION_COLS`/`PROJECT_JOIN_COLS`（435-448，记录不写）；每组合 warmup 3 + `--reps`（默认 30）采样计时，输出 per-combo 与聚合 P50/P99/max（450-505）。
  - CLI：`--rows`（默认 1000，须 ≥10 否则 `sys.exit`，518、530-531）、`--limit`（默认 100，SQL 内 LIMIT+1）、`--out`（默认 /tmp/eqp.json，557-559 写报告）、`--seed`（**预留未用**，521、193）、`--keep`（保留临时库调试，522、268-270 rmtree 清理）、`--join-col`（仅 worktree，523-524）、`--real-db [PATH]`（默认 `~/.local/share/opencode/opencode.db`，525-526）。

- **被测试引用方式**：
  - `tests/v4_fixture.py:50-55`：`importlib.util.spec_from_file_location("eqp_matrix_under_test", scripts/eqp_matrix.py)` 按**文件路径**装载（scripts/ 非包）。
  - `tests/test_eqp_matrix.py:15-17`：用其 `build_draft_db / all_combos / parse_eqp / expected_window / cleanup_draft` 作为 oracle，被测对象是**真实组装器** `oc_slimapi.dbaux.build_sessions_query`（54-87：draft DB 断言 SCAN + TEMP B-TREE + 行集精确匹配 + `rows_to_records` 管道不改行集）。
  - `tests/test_eqp_matrix.py:28-38`：绕过冻结缺陷——eqp_matrix 草稿库 `model` 列写纯文本 `"model-x"`（218）而生产 `rows_to_records` 按 §8 要求 JSON，测试侧 UPDATE 归一（"scripts/eqp_matrix.py B0 冻结不改"，30）。
  - `tests/v4_fixture.py:211-226` 复用其 DDL 构造 fixture；`tests/test_session_single_v4.py:408` 引用其 PRAGMA 对齐结论（真库五列 `INTEGER NOT NULL`）。

- **失败处理**：draft 断言失败 → 打印 FAILURES JSON + `sys.exit("exit 1: ...")`（418-419、553-554）；真库路径不存在 → `sys.exit`（427-428）；`--rows<10` → `sys.exit`（530-531）；**真库 schema gate 不通过不改变退出码**（仅 `gate_passes=False` 记录；544-545 只在通过时打印，失败分支无输出无告警，仍 exit 0）。

- **疑问点**：
  1. 真库模式 schema gate 失败静默通过（544-545 无 else 分支）：采集到不兼容 schema 的库也 exit 0——作为"数据采集不断言"（27）是设计选择，但 gate 失败连 WARNING 都没有。
  2. `expected_window` 的 search 匹配用 `r["title"].startswith("grp1-")`（292），SQL 用 `LIKE '%grp1%'`（72、129）——仅因生成 title 形状（`grp{i%4}-` 前缀）二者等价；若改数据分布，oracle 与 SQL 语义会分叉（隐性耦合）。
  3. 真库锚点查询用 f-string 拼 `OFFSET {max(session_rows // 2 - 1, 0)}`（457-459）：值来自 `count(*)` 无注入面，但与参数化风格不一致。
  4. `build_draft_db(rows, seed)` 的 `seed` 参数从未使用（"rng seed（预留）"，521；docstring "seed 仅作将来扩展"，193）。
  5. `SESSION_PROJECTION_COLS`（50-56）比 `SQL_TMPL` 实际 SELECT 的列宽（schema 门含 summary_additions/deletions/files、tokens_reasoning/cache_read/cache_write、time_compacting 等，SELECT 只取子集，64-69）——门比投影宽是有意（v2.2 行 146 全投影列版）还是历史遗留，值得确认。
  6. 草稿库 `parent` 组合 `s0000` 硬编码假定 rows>5（`s{i - (i%5):04d}` 生成的根 id 含 s0000，200）；`--rows>=10` 下 s0000 恒存在，约束自洽。
  7. draft 模式 `failures` 打印剔除 `first_ids`（393）便于阅读，但 `results` 内仍保留——非问题，仅记录。

---

### scripts/measure_token_overhead.py（416 行）+ scripts/measure_token_overhead.md（85 行，扫读）

- **职责**：token-stream SSE overhead 自包含测量 harness（`docs/specs/design-token-stream.md` §11 方法论落地），验证两个用户批准 lever 后的 ≤1.2x 目标；.md 是其结果报告快照。

- **关键行为（.py）**：
  - **自包含**：不 import 未建/在建的 token_hub，自行编码 §5.6 wire 帧，声称镜像 `src/oc_slimapi/sse/hub.py:110` 的 `sse_frame()`（10-12、58-61）。
  - **Lever 1（终帧 MARKER）**：终帧 `message.part.snapshot{done:true}` 只含 `{sessionID,messageID,partID,done}`，无 `text`、无全文重发（15-20、143-148）——消除原设计结构性 ≥2x 下限；最终文本以 `/since` 为准（§5.7）。
  - **Lever 2（gzip 默认）**：gzip 压缩后 wire 为 PRIMARY 指标（`wire_bytes`），`wire_bytes_nogzip` 仅参考列（21-24、82-83、151）；gzip = level 6 + 每帧 `Z_SYNC_FLUSH` 流式建模（41-43、96-104）。
  - **建模假设**（26-44，声明为参数非验收标准）：TOKENS_PER_SECOND=30（保守悲观速率，72-73）、TOKEN_FLUSH_MS=100（70）、TOKEN_FLUSH_BYTES=4096（71）；subscribe 建模在 text-start（空快照，113-116）；不测 1MiB 截断路径（38-40）。
  - **批处理**：pending 累积，`pending_bytes >= 4096 OR (t_ms - window_start_ms) >= 100ms` 即 flush 为一个 delta 帧；window 仅在 flush 时推进（119-141）；结束 drain residual → delta 帧 → 终 MARKER（143-148）。
  - **12 条 trace**（≥10 要求，304-319）覆盖 §11 a-e 类：short-text-en×2 / long prose+code / cjk 中文+日混 / reasoning dense+mixed（1-3 字符最坏 framing 场景，234-237）/ tool-input json+command / mixed-assistant / emoji-unicode；全部 seeded 确定性（160-161）。
  - 输出 markdown 表 + 汇总 + 目标判定（327-412）；`main()` 恒 `return 0`（412）——测量 harness 非门禁，MISSES 也不失败退出。

- **关键行为（.md，扫读）**：
  - 判定 **NO**：median `overhead_x_gzip` = **1.47x** 未达 ≤1.2x（原设计 batched median ~12.05x，降 8.2×）；4/12 trace 单独 ≤1.2x、6/12 ≤1.5x；`overhead_x_nogzip` median 11.04x（lever 1 单独的效果）（49-58）。
  - 剩余 gap 归因：短消息（固定 gzip 流成本不摊销）+ 低冗余内容（CJK/代码/单字符 reasoning 抗压缩）（62-65）。
  - Open questions（71-79）：建议重锚目标为 "~1.5x median gzip / ≤2.5x worst-case"、flush 窗口/速率敏感性、gzip flush 节奏与真实 Stage-D 编码器核对、level 9、marker 形状已确认够用。
  - 引用 `CHANGELOG [0.1.0]` "SSE 永不 gzip" 的首个例外（10）。

- **失败处理**：无失败路径（纯计算脚本）；仅 orjson ImportError 依赖缺失（56，主依赖必有）。**check.sh/测试均不运行它**（grep 证实 tests/ 无引用），数字防漂移无 gate。

- **疑问点**：
  1. `scripts/measure_token_overhead.py:11-12` 引用 `hub.py:110` **已陈旧**：实测 `src/oc_slimapi/sse/hub.py` 现仅 42 行、无 `sse_frame`；真实实现在 `src/oc_slimapi/sse/hub_types.py:105-108` 与 `src/oc_slimapi/sse/tokenstream/frames.py:33-42`（两处复制、逐字节同逻辑）。镜像逻辑本身与当前实现一致（已核对），仅路径行号漂移。
  2. `gen_cjk_chinese` 死代码：`sep = "" if rng.random() < 0.15 else ""`（215）两分支相同——本意（15% 概率加分隔符？）未实现。
  3. overhead 定义 = wire / **纯 UTF-8 全文字节**（109-110）：raw 侧不计上游 delta 事件自身的 SSE 框架字节（上游也是 `event:/data:` 帧到达 sidecar）。这是 §11 方法论选择（docstring 已声明 26-44），但意味着 1.47x 高估了"sidecar 额外"开销的比例——审计值得记录口径。
  4. .md 表为某时点快照：脚本若改（常数/生成器），无机制强制 .md 同步；反之亦然。
  5. 时间窗模型 `window_start_ms` 仅 flush 时推进（139-141）：与 tokenstream hub 真实 flush 实现的一致性未在本卡核实（属 e1-01/e1-10 卡范围）；若实现是固定节拍窗而非"自上次 flush 起"，测量模型会偏。
  6. `TOKENS_PER_SECOND=30` 单一速率点：.md open question 2 已自认（60 tok/s / 250ms 窗未量化），列此备查。

---

### deploy/oc-slimapi.service（73 行）

- **职责**：systemd **user** service 单元模板（部署到 `~/.config/systemd/user/oc-slimapi.service`，`systemctl --user`；部署步骤 `docs/operations.md` §3）。

- **关键行为**：
  - 刻意**不含** `ProtectSystem/ProtectHome/ProtectKernel*/NoNewPrivileges` 等 sandbox——user manager 无权设置，会 `status=218/CAPABILITIES` 失败（4-7）；进程隔离靠 stunnel mTLS（:14097/:14096）+ Tailscale ACL。
  - `Type=simple`（16）；`WorkingDirectory` / `ExecStart=.venv/bin/python -m oc_slimapi.app` / `Documentation=` 均为本机绝对路径（12、17-18）。
  - `Restart=on-failure` + `RestartSec=5`（19-20）；`TimeoutStopSec=15`（P0-1：高于 uvicorn 5s graceful，低于 90s SIGKILL 默认，给 SSE 排空机会，21-24）。
  - `StateDirectory=oc-slimapi`（39，systemd 自动建 `~/.local/state/oc-slimapi`）。
  - `StandardOutput/StandardError=journal` + `SyslogIdentifier=oc-slimapi`（62-64）。
  - `MemoryMax=384M`（70，cgroup OOM 保护；注释算术 = max_transforms=1 + max_response_bytes=64MiB + 16MiB inline cap + Python/Baseline RSS，66-69）。
  - `WantedBy=default.target`（73）。

- **逐行 Environment= 清单（与 config.py 默认对照）**：
  | 行 | Environment= | config.py 默认 | 判定 |
  |---|---|---|---|
  | L28 | `OC_SLIMAPI_HOST=0.0.0.0` | `127.0.0.1`（config.py:356） | **刻意覆盖**（Tailscale 直连；validate 允许 0.0.0.0，config.py:773；:4097 明文，远程暴露靠 ACL/防火墙，26-27 注释自认风险） |
  | L29 | `OC_SLIMAPI_PORT=4097` | `4097`（config.py:357） | 一致（冗余声明） |
  | L30 | `OC_SLIMAPI_UPSTREAM=http://127.0.0.1:4096` | 同值（config.py:358） | 一致（冗余声明） |
  | L31 | `OC_SLIMAPI_MAX_MESSAGE_BYTES=33554432` | `33554432`/32MiB（config.py:359） | 一致（冗余声明） |
  | L32 | `OC_SLIMAPI_SERVER_API_VERSION=2` | 常量钉死 `SERVER_API_VERSION=4`（versioning.py:38、config.py:436） | **已废弃残留**：4.0.0 起该 env 不再影响任何视图，设置仅产生启动 warning 并被忽略（config.py:796-804；CHANGELOG.md:122）。不致命但陈旧 |
  | L33 | `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2` | 钉死 `(3,4)`（versioning.py:44） | **致命残留（P0）**：`Settings.validate()` fail-closed——解析得 `(2,2) ≠ (3,4)` 直接 `RuntimeError`（config.py:817-822）。**按此模板部署启动即崩**。docs/operations.md:92-94 声称"生产 unit 已同款清理，模板不再示例"，与仓库模板事实**直接矛盾**（模板 32-33 行仍在） |
  | L34 | `PYTHONUNBUFFERED=1` | —（Python stdio，非应用配置） | 正常 |
  | L40 | `OC_SLIMAPI_ACCESS_LOG_DIR=%S/oc-slimapi/logs` | `logs`（config.py:533） | 刻意生产覆盖（StateDirectory；%S=~/.local/state） |
  | L41 | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH=%S/oc-slimapi/logs/traffic-snapshot.jsonl` | `logs/traffic-snapshot.jsonl`（config.py:552-554） | 刻意生产覆盖 |
  | L45 | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30` | `0`（config.py:561-563，0=永不清理） | 刻意生产覆盖（注释 42-44：复用 access-log 维护循环） |
  | L46 | `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS=3` | `0`（config.py:537） | 刻意生产覆盖（AGENTS.md：生产 RETAIN_DAYS=3） |
  | L54 | `OC_SLIMAPI_STATE_DIR=%S/oc-slimapi` | `state`（config.py:648） | 刻意生产覆盖（T9/P1-4：incarnation 状态独立目录，含单调迁移语义，47-53 注释） |
  | L60 | `#Environment=OC_SLIMAPI_ACTIONS_FILE=%h/.config/oc-slimapi/actions.toml`（**注释态**） | `None`（config.py:507，opt-in 默认关） | 未启用；启用路径见 actions.manifest.example.toml 卡 |

- **失败处理**：`Restart=on-failure` 5s 重启（19-20）；`MemoryMax` cgroup OOM kill 兜底 runaway 上游体（66-70）；启动期配置错误（如 L33）→ 进程崩溃 → 反复重启（on-failure 无次数上限，systemd 默认 burst 限速）。

- **疑问点**：
  1. **P0：L33 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2` 与 fail-closed 钉死 (3,4) 冲突 → 启动 `RuntimeError`**（config.py:817-822 + versioning.py:44）。模板严重过时；且 operations.md:92-94 声称模板已清理，仓库事实不符——要么模板漏改，要么 operations.md 描述了未落库的生产改动。
  2. L32 `OC_SLIMAPI_SERVER_API_VERSION=2` 废弃残留（仅 warning 不致命，config.py:796-804），应与 L33 一并删除。
  3. `Documentation=file:///home/mar/...`（12）、`WorkingDirectory`（17）、`ExecStart` 绝对 .venv 路径（18）——机器特定硬编码，模板可移植性依赖人工改写（头部注释未提醒改这三行，仅提醒 env）。
  4. `MemoryMax=384M` 算术注释（66-69）只算了 transform/inline 口径：v4 新增的 token-stream worst case ~76MiB（config.py:457-463）、replay log 默认 64MiB（config.py:667-671）、raw-fetch 64MiB（config.py:406-408）等预算叠加后是否仍 fit 384M，注释未重算——需要审计复核（RSS 峰值是活跃叠加而非简单求和，但裕量论证已过时）。
  5. `HOST=0.0.0.0` 明文暴露面为已声明风险（26-27），与 stunnel 卡的 mTLS 定位并存——审计确认这是 Tailscale ACL 前提下的风险接受，非疏漏。

---

### deploy/stunnel.conf（29 行）

- **职责**：stunnel 服务端 mTLS 双入口配置模板（ocdroid → 14096/14097 的 TLS 终结）。

- **关键行为（mTLS 配置要点）**：
  - `foreground = no`、`client = no`（服务端模式）（1-2）。
  - 两个节，**同一套 mTLS 姿态**（4 注释）：
    - `[opencode-direct]`：`accept = 127.0.0.1:14096` → `connect = 127.0.0.1:4096`（opencode 直连回退入口）（5-7）。
    - `[opencode-thin]`：`accept = 127.0.0.1:14097` → `connect = 127.0.0.1:4097`（sidecar 入口）（18-20）。
  - 每节：`cert/key = /etc/stunnel/server-{cert,key}.pem` + `CAfile = /etc/stunnel/ca-cert.pem` + `verifyChain = yes`（8-11、21-24）——客户端证书须对 CA 链验证通过（mTLS 服务端侧强制）。
  - `TIMEOUTidle = 43200`（12h 空闲超时）（12、25）。
  - `socket = l:TCP_NODELAY=1` / `r:TCP_NODELAY=1` / `l:SO_KEEPALIVE=1` / `r:SO_KEEPALIVE=1`（本地+远端双侧，SSE 低延迟与死连检测）（13-16、26-29）。

- **失败处理**：本文件无日志/输出配置段——stunnel 自身失败（证书缺失等）落在 stunitunnel 系统日志/服务管理器，不在 sidecar 可观测面。

- **疑问点**：
  1. `/etc/stunnel/*.pem` 为占位路径（4 注释要求替换），仓内无部署校验/一致性检查（是否与 ocdroid 侧客户端证书配套无从在本仓验证）。
  2. 未显式钉 TLS 最低版本/密码套件（依赖系统 stunnel 默认）——audit 视角值得记录。
  3. `verifyChain=yes` 只验链不钉身份（无 `verifyPeer`/`checkHost`）：CA 签发的**任意**客户端证书皆可入（单客户端自建 CA 场景够用；若 CA 复用则面变宽）。
  4. `TIMEOUTidle=43200`（12h）与 sidecar `TOKEN_HEARTBEAT_SECONDS=15`（config.py:53）配合绰绰有余——无疑问，仅记录心跳设计前提。

---

### deploy/actions.manifest.example.toml（55 行）

- **职责**：`/slimapi/actions` manifest 参考示例（与本机真实部署一致的 4 动作；wire 契约引用 v2-contract §2、ops 见 operations.md §11）（1-4）。

- **关键行为**：
  - **启用路径**：copy 到机器本地路径（如 `~/.config/oc-slimapi/actions.toml`）→ `chmod 0600` → systemd unit 设 `OC_SLIMAPI_ACTIONS_FILE` 指向它（6-8；service 模板 L60 注释行即此入口，默认注释态=特性关闭）；按主机调整 argv[0] 路径（8）。
  - **启动校验**（`src/oc_slimapi/actions.py::_load_manifest`，10-13）：regular file（拒 symlink）、owner-only-write（无 group/other 写位）、属 runtime user；**坏文件 → 特性整体禁用（fail-closed）；坏动作 → 仅丢弃该动作 + WARNING**。
  - **4 个示例动作**：
    - `[actions.plan_limit]`：kind=query（echo stdout as markdown），`argv=[/home/mar/.config/opencode/scripts/plan_limit.py]`，timeout_s=90，max_output_bytes=65536（23-28）。
    - `[actions.list_model]`：query，15s，16384（30-35）。
    - `[actions.list_agent]`：query，15s，32768（37-42）。
    - `[actions.restart]`：kind=exec，`argv=[/usr/bin/systemctl, --user, restart, opencode-web]`，timeout_s=30，`min_interval_s=60`，`require_confirm=true`（49-55）。
  - `require_confirm` 语义：POST 不带 `{"confirm":true}` → 409 `action_confirm_required`，动作**不执行**；`min_interval_s` 为内存态节流，sidecar 重启即重置（44-47）。
  - 安全姿态注释（15-18）：明文 :4097 可达 = 风险接受面（与既有 catch-all → opencode 控制端点同级）；缓解（非授权）：spawn 并发 cap、single-flight + min_interval 节流、`shell=False`、WARNING 级结构化审计到 journald。

- **失败处理**：文件级 fail-closed / 动作级降级（10-13）；运行期由 timeout_s / max_output_bytes / 并发 cap / min_interval 约束。

- **疑问点**：
  1. 头注释 wire 契约引用 **`docs/specs/v2-contract.md` §2**（3，另见 service L59 同引）——v2 语义 3.0.0 已退役；actions 在 v3-contract 中属"不在消费集/收编全集"清单（v3-contract.md:157、200）。引用陈旧（与 check_routes_doc.py:311 同类问题）。
  2. argv[0] 全为本机绝对路径 `/home/mar/.config/opencode/scripts/*.py`（25、31、39）——脚本本体在仓外，本仓审计无法核其内容/安全性（审计边界外，需另核）。
  3. `restart` 动作重启的是 `opencode-web` 而非 opencode 本体（51）——与本 sidecar 无直接关系，属主机运维动作；无疑问，记录用途。
  4. 示例未展示 exec 类无 `require_confirm` 的用法，也无 query 类以外的输出语义差异说明——完整性小缺口。

---

### pyproject.toml（32 行）

- **职责**：包定义（名称/版本/依赖/入口）+ 构建系统 + pytest 配置。

- **关键行为**：
  - **build backend**：`setuptools>=75` + `setuptools.build_meta`（1-3）。
  - **包版本**：`version = "4.4.0"`（7）——与 `git describe` 输出 `v4.4.0` 一致（HEAD 即当前 tag，发版闭环无漂移）。
  - **Python 窗口**：`requires-python = ">=3.11"`（9），无上界。
  - **运行依赖（4 个，全带下/上界窗口）**：`fastapi>=0.115,<1`、`httpx>=0.28,<1`、`orjson>=3.10,<4`、`uvicorn>=0.34,<1`（10-15）。
  - **test extra**：`pytest>=8,<9`、`pytest-asyncio>=0.25,<1`、`respx>=0.22,<1`（18-22）——respx 为 httpx mock 层（测试对上游的模拟）。
  - **入口脚本**：`oc-slimapi = "oc_slimapi.app:main"`（24-25）——生产 ExecStart 用 `-m oc_slimapi.app`（service:18），两者并存。
  - **包发现**：`[tool.setuptools.packages.find] where = ["src"]`（27-28，src-layout）。
  - **pytest**：`asyncio_mode = "auto"` + `testpaths = ["tests"]`（30-32）。

- **失败处理**：构建/安装失败由 pip/setuptools 常规报错；`check.sh` 的 venv 提示（check.sh:15）即依赖缺失的第一道引导。

- **疑问点**：
  1. 无锁文件（仓内无 requirements/uv.lock/poetry.lock）：依赖窗口跨度大（如 fastapi 0.115→0.x 任意），不同时间装机解析结果可漂——复现性依赖本机 .venv 惯例。
  2. 无 lint/type 工具配置（ruff/mypy 缺位），`check.sh` 亦无 lint 项——代码质量门禁仅 pytest+对账+compileall。
  3. `requires-python` 无上界（3.11+ 全开）：依赖（fastapi/uvicorn 等）自身窗口间接约束，无显式声明。
  4. 版本写回唯一通道是 release.sh 的 sed（release.sh:61-67）——`[project] version` 是文件中唯一 `^version =` 行（已核实），当前解析/写回自洽；此为 release.sh 疑问点 2 的镜像面。

---

## 汇总：跨文件一致性发现（供后续审计阶段合并）

1. **P0 — deploy/oc-slimapi.service:33**：`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2` 与 `versioning.py:44` 钉死 `(3,4)` + `config.py:817-822` fail-closed 冲突 → 按模板部署**启动即 RuntimeError**；`docs/operations.md:92-94` 声称模板已清理，与仓库事实矛盾。L32 `OC_SLIMAPI_SERVER_API_VERSION=2` 为废弃残留（仅 warning）。
2. **陈旧 v2 契约引用三处**：`scripts/check_routes_doc.py:311`（错误修复提示）、`deploy/actions.manifest.example.toml:3`、`deploy/oc-slimapi.service:59`——均指向已退役的 v2-contract，权威应为 v3-contract。
3. **陈旧代码路径引用**：`scripts/measure_token_overhead.py:11-12` 的 `hub.py:110` 已不存在（实现在 `hub_types.py:105` / `tokenstream/frames.py:33`，逻辑镜像仍逐字节一致）。
4. 测量/报告资产（measure_token_overhead .py/.md）与 CI 无联动，数字快照无防漂移 gate。

