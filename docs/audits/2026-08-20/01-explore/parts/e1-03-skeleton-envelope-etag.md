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
