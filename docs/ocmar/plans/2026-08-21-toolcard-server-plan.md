# oc-webui 工具卡提案 — 服务端实施方案 v2.1（供 owner 审阅）

> v2.1 修订记录（R2 增量复审整改，rev-sgpt 裁决 PASS WITH CONDITIONS「文字级整改后可直接派发」）：P1-N1 AGENTS.md 纳入写域（§5b）；P1-N2 title 求值顺序冻结（§2）；P1-N3 源 diffStats 优先级 + `_compute_diffstats` 异常安全（§4b）；P1-N4 `_valid_count` 收紧为 int-only（§4a）；P1-N5 状态机 hunk 行数耗尽语义（§4d）；P1-N6 extractor 同守 truncated（§4d）；P2-N1 ref 判定基于源值（§4b）。
> v2 修订记录：吸收 rev-sgpt 评审（2026-08-21，裁决 PASS WITH CONDITIONS，12 P1 + 3 P2）全部整改项。
> 关键前提修正（P1-1，编排者已亲验）：**v4.8.0 已正式发布**（tag `v4.8.0`、HEAD `f111a82` release commit、CHANGELOG 已定稿、新空 `[Unreleased]` 在 :1169）；「修订三」已被 providers limit（v4.4.0）占用；发版规则已改为「**major 只跟协议大版本走**，wire 不 bump 的破坏性变更发 minor」（docs/release.md:37-38/54-55，owner 2026-08-21 裁定）。v1 方案的「4.8.0 major + 修订三」锚点全部失效，v2 改锚 **4.9.0 minor + 修订四**。
> 依据：`/home/mar/personal_projects/oc-webui/TOOL_CARD_CONTRACT_PROPOSAL.md` + 生产 opencode DB 只读实证（23,827 patch / 22,963 edit / 1,722 apply_patch / 5,657 compress）+ 双客户端源码核验（ocdroid `Part.kt:139-176` / oc-webui `PartCards.vue:237-253`）。

## 0. 结论摘要

| 项 | 裁决 | 性质 | 载体 |
|---|---|---|---|
| P1-3 compress title 合成 | ✅ 实施（改案：不动 TOOL_INPUT_KEYS） | 加性 wire | 4.9.0 [Unreleased] |
| P2-4 outputBytes | ✅ 实施（口径=JSON wire 字节） | 加性 wire | 4.9.0 |
| P0-1a patch files 归一化 | ✅ 实施 | **wire 形状变更**（D1-r 例外） | 4.9.0 + 修订四 |
| P0-1b tool metadata.files 投影 | ✅ 实施（含 aggregate diffStats） | 加性 wire | 4.9.0 |
| B2 edit diffStats + 合成 files | ✅ 实施（含 extractor 增补） | 加性 wire | 4.9.0 |
| ETag REP_VERSION bump | ✅ 实施（P1-9） | 内部（触发全量 ETag 轮换） | 4.9.0 |
| P0-2 patch expand ref | ❌ 搁置 | — | 重启条件见 §6 |

## 1. 生产实证基础（ground truth，不变）

1. patch part 0/23,827 带 state；files 恒 string[] 绝对路径；长度分布 1 文件 19,884 / 2 文件 2,230 / 3 文件 789 / 4 文件 353 / 5–8 文件 416；cap 10 覆盖 ≥99.7%；单条 path ≤243 字符。
2. edit metadata = `{diagnostics, diff(正文，含 Index: 头), filediff(恒空[]), truncated}`，无 files 键；filediff 恒空 → 现行 diffStats 注入对 edit 从未生效。
3. apply_patch 是唯一带 metadata.files 的工具（1,722/1,722 completed）；条目键 `{filePath, relativePath, type, patch(~700B diff 正文，剔除), additions, deletions}`；其 metadata **常常只有 files 一个键**。
4. compress 100% 缺 title；`input.content[] = {topic(avg 28/max 110 字符，人类撰写), startId, endId, summary(avg 3,938/max 29,685)}`。**topic/summary 嵌套在 content[0]，非 input 顶层键**（P1-7 事实基础）。
5. 双客户端双形状兼容已实证（ocdroid `PartFilesSerializer` / oc-webui `toFiles`）。

## 2. P1-3 · compress title 合成（改案）

**v1 → v2 变更（P1-7 整改）**：**取消 `TOOL_INPUT_KEYS` 增补 topic/summary**——生产 compress 的 topic/summary 嵌套在 `input.content[0]`，`_pick` 顶层白名单根本读不到；而全局加白名单会让**所有非 compress 工具**的顶层同名字段开始上线（不可接受的扩面）。title 改为 compress-only 直读：

- `_tool()` 内，`part.get("tool") == "compress"` 且 thin_state 无 title（缺席/None/空串）时，**求值顺序冻结（P1-N2）**：
  1. `source_input = state.get("input")` 必须 `isinstance(dict)`——否则放弃合成（复用 `_tool` 既有防护，绝不对其 `.get()`）；
  2. `c = source_input.get("content")` 必须是非空 list——否则放弃；
  3. `e = c[0]` 必须 `isinstance(dict)`——否则放弃（不尝试 c[1]）；
  4. 此后方可求值：`title = _clip(e.get("topic"), 160) ?? _clip(e.get("summary"), 160) ?? f"压缩 {len(c)} 段"`。
  任一前置条件不满足 → 保持无 title（无兜底文案——段数 fallback 仅在 1-3 全过后 topic/summary 均缺失时使用）。
- `_clip(s, n)` 语义冻结：仅 str；先 `strip()`，空白串视为缺失；按**字符**截断 n，**不附省略号**；非 str 返回 None。
- `content` 键不进白名单（维持 omitted + `part_state_input_full` ref）→ WebUI 全量 input 展开路径零变化。
- 与提案 P1-3 的偏差记录：提案的「TOOL_INPUT_KEYS 增加 summary 投影」因嵌套形状不可达而取消；折叠卡 title 需求由本合成满足，展开全量路径已有。
- **merged 语义（P1-8）**：合成 title 仅保证 skeleton 视图；`mode=merged` 成功 splice 后 parts 被上游 full 原样替换，title 回到上游原状（即维持今日行为，无回归）——契约与 CLIENT_CHANGES 明示此限。

## 3. P2-4 · outputBytes（含 P2-1 口径冻结）

- 位置：`_maybe_inline_state_field` omit 分支，**仅 key == "output"**：`thin_state["outputBytes"] = size`（`_field_byte_size` 已算值）。
- **口径（P2-1，契约明文）**：`outputBytes` = **被省略字段值的 JSON wire 字节数**（`orjson.dumps(value)` 长度，含 JSON 引号/转义/嵌套结构——与本仓 skeleton 阈值同一记账原语 `_field_byte_size`，非裸 UTF-8 文本长度）。契约写明，客户端按提示值理解。
- 条件：output 在场、非 None/空串、因 per-field cap 或 per-message budget 省略。error 不附 errorBytes。
- 合成提示键：不进 omitted/hasFull，不参与 `_is_renderable`，merged/full 视图自然无此键（上游原样）。

## 4. P0-1 · files 规范化投影

### 4a. patch 侧（wire 形状变更）

- `_patch()` files 统一归一化对象数组：string → `{"path": s}`；dict → 现行 `_pick(item, {path, additions, deletions, status})`（legacy 分支保留）；非 str/dict 条目跳过。cap 10 条，超限附顶层 `filesTotal`。
- **filesTotal 口径（P2-2）**：`filesTotal = len(源数组)`（**源计数**，含无效条目——契约写明它是 source count，不是可展示文件数；compact 列表只含有效映射条目）。
- **diffStats 防伪造守卫改写（P1-5/N4 整改）**：守卫与求和使用**同一个严格数值校验器** `_valid_count(v) = isinstance(v, int) and not isinstance(v, bool) and v >= 0`（**int-only**：拒 float（含 `1.0`/`inf`/`nan`——`int(inf)` 会 OverflowError、小数截断与 int 字段语义不符）、拒 bool、拒负数；JSON 大整数 Python int 原生支持）。守卫条件：**至少一条目携带 ≥1 个 `_valid_count` 的 additions/deletions** 才注入；求和：逐条目 `_valid_count` 值计入、非法值计 0；`files = 有效映射条目数`（非源数组长度）。归一化 string 派生条目（仅 {path}）永不注入——R1-M1 语义保持。异常输入一律降级为跳过/不注入，绝不让消息列表 500。
- 兼容性：ocdroid/WebUI 双形状解析已实证零必改。

### 4b. tool 侧（加性）

`TOOL_METADATA_KEYS` 增加 `"files"`，投影为专用 compact 映射（非 verbatim）：

```text
条目 → {path: relativePath ?? filePath, additions?: int, deletions?: int, status?: type}
剔除：patch（diff 正文）、filePath/relativePath/type 原键；非 dict 条目跳过
cap 10；filesTotal = len(源数组)（源计数口径，同 4a）
```

- **aggregate diffStats（P1-4/N3 整改）**：对 compact 映射后的有效条目用 `_compute_diffstats_from_files`（严格校验器版）合成 `metadata.diffStats`。**注入优先级链（全链冻结，含源值冲突）**：⓪ 源 `metadata.diffStats` 在场且为合法形状（dict 且三子键 `_valid_count`）→ **保留源值，跳过一切派生**（源值是上游/既有消费者已认可的权威，派生永不覆盖）；① `metadata.filediff` 结构化有效（`_compute_diffstats` 安全会话下返回非 None）→ 注入；② 否则 `metadata.files` 有效（≥1 `_valid_count` 数值）→ `_compute_diffstats_from_files`；③ 否则 tool==edit 且 `metadata.diff` 可解析（§4d）→ 解析器统计；④ 均不可得 → 不注入。**`_compute_diffstats` 异常安全化（§5a）**：现行实现 `int(value)`(:264-275) 对字符串垃圾/list/`inf` 可抛异常——改写为逐条目 `_valid_count` 校验（同 4a 单一校验器），非法值计 0、非 dict 条目跳过，**永不抛异常**；非空但全部条目畸形 → 返回 None（落 ②/③ 兜底）。
- **ref 保活（P1-2 整改 + P2-N1 源值判定）**：`part_state_metadata_full` ref 触发条件 = 既有条件（存在白名单外键被省略）**OR 源 `metadata.files` 为非空 list**（compact 投影恒有损：剔 patch/filePath/relativePath/type——即使 1 个文件也是有损投影，ref 必须在场）。**判定基于源值而非映射结果**：源 files 非空但全部条目畸形 → 映射结果为空列表，ref 仍必须在场（源信息被丢弃是更强的展开理由）。即：apply_patch 任何非空 metadata.files → ref 恒在。测试覆盖 1/10/11 文件三档 + 全畸形条目档。
- ≤10 条时 compact 全量映射仍在（含 additions/deletions/status），ref 同时在场（可取回 patch 正文等被剔字段）——「全量已在折叠卡」表述作废，以「compact 有损 + ref 恒活」为准。

### 4d. B2 · edit diffStats + 合成 files（P1-3/P1-6/P2-3 整改）

**解析器** `_files_from_diff_text(text) -> list[dict] | None`（skeleton.py，紧邻 `_compute_diffstats`）——**小型状态机**，单次线性扫描：

- 状态：`idle`（未进文件段）→ `in_file`（见 `Index: ` 或配对 `+++ `/`--- ` 头）→ `in_hunk`（见 `@@ `）→ 下一文件段回归 `in_file`。
- **hunk 退出双机制（P1-N5）**：① `@@` 头解析 old/new 行数计数（`@@ -l,s +l,s @@`），hunk 体内行数耗尽即退回 `in_file`（耗尽前遇到的 `+++ `/`--- ` 按 hunk 正文前缀计数——git 语义：hunk 行数内一切行是正文）；② 计数未耗尽但遇 `Index: ` / 配对头 / `diff --git` 边界 → 视为畸形/截断，当前文件段按已见行收尾、新文件段正常开始（宁少计不误归属）。
- 文件段识别：`Index: <path>` 优先；否则配对头 `+++ b/<path>`（`--- a/` 配对校验；剥 `a/`/`b/` 前缀）；`+++ /dev/null`（删除文件：路径取 `--- a/<path>`）、`--- /dev/null`（新增文件：路径取 `+++ b/<path>`）。**孤立 `Index:` 不构成有效文件段（P1-N5）**：零 hunk 文件段必须具备成对 `---`/`+++` 头（rename-only 同理——git 对 rename 也发 `---`/`+++` 对）；仅有孤立 `Index:` 行的日志/截断文本 → 整体 None。
- **计数仅在 `in_hunk` 状态**：行首 `+`/`-` 各计 additions/deletions；`@@` 头行、`+++`/`---` 头行不计；hunk **正文内**恰以 `+`/`-` 开头的行按状态机语义正确归属；`\ No newline` 忽略。
- 零 hunk 文件段（成对头在场、无 `@@`，rename-only 等）：计入 files（path 在场、±0）。
- 返回 None（**不伪造**，R1-M1 哲学）：text 非 str / 无任何有效文件段（成对 `---`/`+++` 头或 `Index:`+成对头均无）。**多文件支持声明（P1-N5）**：无 `Index:` 的多文件 diff 依赖 hunk 行数耗尽 + `diff --git`/配对头边界转移——生产 edit 的 diff 恒带 `Index:` 头（上游 git diff --no-index 格式），无 Index 多文件为防御性支持，边界模糊处按「宁少计不误归属」降级。
- **截断降级（P2-3）**：`metadata.truncated == true` → **跳过合成**（partial diff 的 files/统计会误导，宁缺毋假）。
- 解析器 O(n) 单遍，无重复切片；不做缓存（D-gate 复核 P95/P99/max，超标再议）。

**注入**（`_tool()`，filediff 恒优先，链见 4b）：

- `metadata.diffStats`：解析器统计聚合。
- `metadata.files`：**合成投影**（`{path, additions, deletions}`，与 4b 同形无 status——diff 文本无类型信息），走 4b 同一 cap 10 + filesTotal + **ref 保活**管线（edit 的 `metadata.diff` 本就非白名单 omitted → `part_state_metadata_full` ref 恒在场，合成投影不改变这一点）。
- 注入时机同既有 diffStats：thresholding 后、永不 omit；`state.metadata.diff` 本身维持 omitted。

**extractor 增补（P1-3 + P1-N6 整改——第 11 条可达性 + eligibility 同守）**：`_expand.py::_extract_part_state_metadata_full` 增补：`part.tool == "edit"` 且源 metadata 无 `files` 键且 **`metadata.truncated` 非 true**（与 skeleton 注入**同一 eligibility 判定**——截断的 diff 不合成 files，extractor 只返回原始 metadata 去 diagnostics）且 `metadata.diff` 解析成功 → 返回的 metadata 对象附加 `"files": <完整解析列表>`（无 cap）。expand 返回形状为加性变更（edit part 的 metadata 展开多一个合成键），纳入修订四 §14 条款。测试必须**真实调用 expand 路由**：断言第 11 条路径可达 + **truncated edit 展开不含合成 files**。

### 4c. D1-r · 发版与契约载体（P1-1 整改后重录）

- **版本**：`[Unreleased]`（CHANGELOG:1169）目标 **4.9.0 minor**——依据现行规约「major 只跟 wire 协议大版本走；wire 不 bump 的破坏性变更发 minor」（docs/release.md:54-55，4.8.0 先例）。
- **契约修订**：v4-contract **修订四**（修订三已被 providers limit 占用），标题日期 2026-08-21。
- **例外记录**：patch files 归一化是 wire 形状变更而 wire 版本维持 4——记录为 **owner 批准的例外**，依据 = 双客户端双形状兼容已实证（§1.5）+ 未知严格 string[] 消费方风险由 owner 知情接受。此例外记入修订四修订头 + CHANGELOG Changed 条目（显式写客户端必改点：「严格按 string[] 解析 files 的消费方需改；ocdroid/WebUI 已兼容零必改」）。
- **不改**已发布的 4.8.0 条目；不改 AGENTS.md / release.md 规则文本（规则已自洽：破坏不 bump wire → minor）。

## 5. 代码 / 契约 / 文档变更清单

### 5a. 代码（fixer-glm-code，单写者）

| 文件 | 变更 |
|---|---|
| `src/oc_slimapi/skeleton.py` | `_clip` helper；title 合成（`_tool`）；outputBytes（`_maybe_inline_state_field`）；`_valid_count` 严格校验器；`_compute_diffstats` **异常安全化改写**（`_valid_count` 化，永不抛异常，P1-N3）；`_compute_diffstats_from_files` 守卫+求和改写（P1-5/N4）；`_files_from_diff_text` 状态机解析器；tool metadata.files compact 映射 + aggregate diffStats 优先级链（含源 diffStats 保留）+ ref 保活（源值判定）；patch files 归一化 + cap/filesTotal（`_patch`）；B2 合成注入（`_tool`） |
| `src/oc_slimapi/etag.py` | `SKELETON_REPRESENTATION_VERSION` `skeleton-v1` → `skeleton-v2`（P1-9） |
| `src/oc_slimapi/routes/messages/_expand.py` | `_extract_part_state_metadata_full` edit 合成 files 增补（§4d） |

### 5b. 契约 / 文档（fixer-glm-docs，与代码并行）

| 文件 | 变更（P1-10 全清单） |
|---|---|
| `docs/specs/v4-contract.md` | **修订四**修订头（例外记录 + D1-r 裁决）；**§10.2 skeleton 通用投影段**（tool 投影：metadata files/filesTotal compact 映射口径 + 有损投影 ref 恒活、diffStats 注入优先级链、title 合成规则、outputBytes 口径=JSON wire 字节）；**§10.2 PatchPart 条款**（:468 verbatim → 归一化 + cap/filesTotal + 源计数口径 + 防伪造守卫）；**§14**（:776 「无 part_files_full」理由句更新 + `part_state_metadata_full` 对 edit 的合成 files 返回语义）；**merged 行为注记**（派生字段仅 skeleton 视图保证，merged/full 返回上游原状）；**ETag 注记**（skeleton-v2 轮换） |
| `docs/specs/CLIENT_CHANGES.md` | :26 patch bullet 改写（归一化形状 + filesTotal + filesTotal=源计数口径）；增补 title 合成 / outputBytes / metadata.files 投影 / edit 合成 files / merged 限界 五条客户端可观测行为 |
| `docs/specs/INTERFACE_MAP.md` | :111 `_patch()` 行 + `_tool()` 行（就近）改写：归一化 / compact 映射 / 解析器 / extractor 增补 / ETag v2 |
| `CHANGELOG.md` | `[Unreleased]`（:1169）增 **Changed**（patch files 归一化——含例外记录与必改点声明）+ **Added**（title 合成 / outputBytes / metadata.files+filesTotal / edit diffStats+合成 files+extractor / ETag v2 轮换说明）；目标 4.9.0 |
| `docs/specs/design-expand.md` | PatchPart string[] verbatim / 无 files ref 的现行态口吻段落（:100-112/:201-203 邻域）加**历史注记**：已被修订四取代，现行以 v4-contract 为准 |
| `AGENTS.md`（P1-N1） | :64 硬规则行「破坏性变更走 major 发版 + 契约修订」补 owner 例外口径：**wire 版本不变的破坏性 wire 形状变更，经 owner 批准可发 minor**，须在权威契约修订头 + CHANGELOG Changed 显式记录例外与客户端必改点（与 docs/release.md:37-38/54-55 2026-08-21 裁定对齐——消除规则文本互斥） |

### 5c. 测试矩阵（P1-12 全清单，fixer-glm-code）

**新增/改写**（`test_skeleton.py` 为主，`test_skeleton_expand.py` 改写 :38/:389 两个测试）：

1. title：topic 优先 / 仅 summary / content 空 / 放弃条件三档（**input 非 dict / content 非 list / content[0] 非 dict——P1-N2**）/ 已有 title 不覆盖 / 非 compress 零合成 / **非 compress 工具带顶层 topic/summary 反例**（P1-7——白名单未动，断言不出现）/ clip 语义（空白=缺失、无省略号、字符截断）。
2. outputBytes：超 cap / 预算耗尽 → 在场且值 = `len(orjson.dumps(output))`；小 output 内联 → 缺席；**多字节字符串**（UTF-8 与 JSON 转义口径）；**跨 part message-budget** 场景；error 省略无 errorBytes。
3. patch 归一化：string[]→{path}[] / cap 10 边界（10 无 filesTotal / 11 有）/ filesTotal 含无效条目（源计数）/ **R1-M1 新守卫**（归一化 {path} 条目不伪造 diffStats）/ **P1-5/N4 混合非法值**（bool/负数/**+inf/nan/正小数**——不抛异常、非法计 0、守卫正确放行/拒绝）/ legacy dict 条目照旧。
4. tool metadata.files：apply_patch 形状映射（type→status、剔 patch/filePath 等）/ 1、10、11 文件三档 ref 均在场（P1-2）/ **全畸形条目档：源 files 非空但映射空 → ref 仍在场**（P2-N1）/ aggregate diffStats 断言（P1-4）/ 优先级链（**⓪ 源 diffStats 保留不覆盖——P1-N3** / filediff 有效 > files > diff 解析 / **非空畸形 filediff 无异常落兜底——P1-N3，含字符串垃圾/`inf` 条目**）。
5. B2 解析器：生产格式样本 / 无头文本→None / 多文件计数 / `\ No newline` / `+++ /dev/null` 删除文件 / `--- /dev/null` 新增 / 截断文本 / hunk 内 header-like 正文行 / **孤立 `Index:` 行 → 整体 None（P1-N5）** / **无 Index 两文件 diff（hunk 行数耗尽 + `diff --git` 边界转移——P1-N5）** / **truncated=true 跳过合成** / 空 diff→None。
6. B2 注入+expand：合成 files+diffStats / 11 文件 → filesTotal=11 + ref 在场 + **expand 路由真实调用断言第 11 条可达**（P1-3）/ **truncated edit expand 不含合成 files（P1-N6）** / 非 diff 文本零注入。
7. **ETag 轮换**（P1-9）：路由级测试——旧 representation ETag 对新投影返回 200 + 新 body/ETag。
8. **merged 回归**（P1-8）：成功 splice（parts 替换为上游原状、无合成字段——今日行为）/ 预算跳过（skeleton 保留、合成字段在场）/ full 拉取失败（同前）。
9. **幂等性**：投影不修改输入对象（deepcopy 纪律）；同一输入重复投影结果全等；历史消息重投影无需 backfill。
10. `_is_renderable` 不因 outputBytes/合成字段改变判定；`part_state_metadata_full` 适用类型不变（12 category 冻结面不动）。
11. fingerprint：派生字段自然进入指纹（普通断言，无 FINGERPRINT_VERSION 改动）。

**不动**：`test_expand_routes.py:527`（patch state 类目 400——P0-2 已搁置）；金样（无 patch part，已验证）；fingerprint normalization golden。

## 6. P0-2 搁置记录（不变）

patch state 省略生产 0/23,827 永不触发。重启条件：① 上游给 PatchPart 加 state/output；② 生产 DB 出现带 state 的 patch part。

## 7. 实施切片（rev 增量复审通过后派发）

| 切片 | 内容 | 写域 | 依赖 |
|---|---|---|---|
| **fixer-glm-code**（单写者，P1-11） | §5a 全部代码 + §5c 全部测试 | `src/oc_slimapi/skeleton.py`、`src/oc_slimapi/etag.py`、`src/oc_slimapi/routes/messages/_expand.py`、`tests/test_skeleton.py`、`tests/test_skeleton_expand.py`、（ETag/merged 路由测试就近新建/增补） | 方案 v2 复审 PASS |
| **fixer-glm-docs**（并行） | §5b 全部文档 | `docs/specs/v4-contract.md`、`CLIENT_CHANGES.md`、`INTERFACE_MAP.md`、`CHANGELOG.md`、`design-expand.md` | 同上（按本方案规格撰写，不等代码） |
| **D-gate**（编排者） | `./scripts/check.sh` 三项全绿；DB 复核（>10 files patch 计数 / edit metadata.diff P95/P99/max 尺寸——P2-3）；rev-sgpt 整改条件逐项核对； Implementation↔契约一致性抽查 | 只读 + 门禁 | 两 fixer 合入 |

P1-11 落实：skeleton.py **单写者**（v1 的 fixer-1/fixer-2 同文件拆分作废）；代码与文档写域互斥并行；共享 helper（`_valid_count`/`_clip`/cap/filesTotal/ref 管线）唯一 owner = fixer-glm-code。

## 8. 风险与回滚

- **ETag 全量轮换**：skeleton-v2 bump 主动触发（预期内一次性重拉，4.8.0 后第二次）。
- **traffic 基线漂移**：patch 卡 +~15B/文件包装、compress title +~90B、apply_patch compact files（剔 patch 正文后净省）；净收益为正。
- **B2 解析成本**：D-gate 复核 P95/P99/max；解析器 O(n) 单遍；truncated 跳过；超标再议缓存。
- **merged 限界**：派生字段仅 skeleton 视图（§2/§3 已记）；今日 merged 行为无回归。
- 回滚：各项独立 revert；patch 侧回滚同步回滚修订四（契约-实现不分离）。

## 附录 · Backlog（本轮不做）

1. 快照 repoint：`opencode-src/current → v1.18.18` 落后运行时（edit filediff 恒空 / metadata.diff 在场 / apply_patch / compress 均为快照外形状）。
2. P0-2 patch expand ref（见 §6）。

## 附 · rev-sgpt 评审发现 → 方案整改映射

**R1（v1→v2）**：P1-1→§0/§4c/§5b（4.9.0+修订四+例外记录）；P1-2→§4b ref 条件；P1-3→§4d extractor+§5c.6；P1-4→§4b 优先级链；P1-5→§4a `_valid_count`；P1-6→§4d 状态机；P1-7→§2 改案（取消白名单增补）；P1-8→§2/§3 merged 限界+§5c.8；P1-9→§5a etag+§5c.7；P1-10→§5b 全清单；P1-11→§7 单写者；P1-12→§5c 11 项；P2-1→§3 口径；P2-2→§4a/§4b filesTotal；P2-3→§4d truncated+§8。

**R2（v2→v2.1，增量复审六项，裁决「文字级整改后可直接派发」）**：P1-N1→§5b AGENTS.md 写域；P1-N2→§2 求值顺序冻结；P1-N3→§4b ⓪源 diffStats 保留 + `_compute_diffstats` 异常安全化；P1-N4→§4a int-only 校验器；P1-N5→§4d hunk 行数耗尽双机制 + 孤立 Index 判据；P1-N6→§4d extractor 同守 truncated；P2-N1→§4b ref 源值判定。
