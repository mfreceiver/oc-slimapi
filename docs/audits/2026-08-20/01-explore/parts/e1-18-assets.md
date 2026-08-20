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
