# oc-slimapi 批次三总计划（全量开展：Wave1-3 上 main + v4 分支 v3 移除）— rev2

> **For agentic workers:** 编排者调度；rev-cgpt 门控 ≥9.0 后分波派发 fixer-glm 泳道（波内并行、波间串行）；每波收尾由编排者执行（CHANGELOG/check.sh/发版）。checkbox 追踪。

**Goal:** 全量消化 follow-up-backlog：批次二残留机械项（B2-1/2/3/4/5/6）+ 批次三大重构（B3-1/2/3）+ v3 全面移除（owner 2026-08-21 指令：移除部分放 **`v4` 分支**，不发版不部署本机，待 ocdroid W4 上线后再 merge 及发布 major）。

**Architecture / 依赖排序（决策冻结）**：
- **Wave 1（main，四泳道并行）**：机械批——文档漂移批 / 死代码清扫+下界校验 / 关停超时对齐 / resync 值域防线。→ 发版 4.6.1（patch，零 wire 变更）。
- **Wave 2（main，单泳道）**：性能 offload（F-201/F-271/F-202 族）——必须在 Wave 3 拆分前（backlog 依赖声明）。→ 4.6.2（patch）。
- **Wave 3（main，三泳道）**：大拆分（tokenstream 五拆 / messages 三拆 / questions-permissions 共享框架）+ B12 等价性测试字面化起步。→ 4.7.0（minor，纯重构零 wire）。
- **v4 分支（Wave 3 完成后从 main HEAD 拉出）**：v3 全面移除（versioning 收窄 / v3 面拆除 / 契约修订 / [5.0.0] CHANGELOG 草案）——**不发版、不部署本机**；merge 门 = ocdroid W4 上线 + wireVersion v3 占比判据（评估文档 §6 Phase 2/3）。
- 理由：B3 拆分与 v3 移除都重组 tokenstream/messages 上帝文件；先拆分稳定文件结构，v4 分支的移除 diff 才干净、merge 冲突最小。ocdroid W4 尚在评审尾段，v4 分支晚开工不构成实际延误。

**基线**：`3573b97`（main HEAD）。
> rev2（2026-08-21）：按 rev-cgpt 门控 R1（FAIL 5.8）修订——B1 增补 backlog 全量覆盖矩阵（F-124/F-346..F-353/R-1..R-5 逐项归属或显式排除）；B2 W1-B 写域收窄为点名文件清单并显式排除 app.py + 派发前机器路径交集校验；B3 B12 完成前移为 v4 分支 v3 移除的**开工准入门**（非仅 merge 门）；B4 merge 观测判据对齐评估文档 §6（阈值/窗长 owner 届时裁定 + SSE active v3 == 0 + 匿名/直连面核查，废除计划自拟的「7 天 == 0」）；B5 分支 CHANGELOG 恒用 [Unreleased]、merge 时由编排者转 `## [5.0.0] - YYYY-MM-DD`（非 fixer 工作）；B6 分支纪律改「门通过前禁止 merge/release/deploy，通过后允许一次性合并」+ 部署 preflight 机械防线；N1-N6 一并落实（AST 扫描规格/respx 验收拆域/W2 字节等价矩阵/W1-C coordinator-only patch 协议/契约追加模式锚定/W3 等价性门）。

## Global Constraints（全波适用）

- fixer 禁改：`CHANGELOG.md`、git 操作；`docs/specs/v3-contract.md` 的修改**仅限本计划点名的漂移行/追加节**（owner v3 废弃方向 + 本计划授权；历史契约 v2-contract.md 恒不改）。
- 各泳道只跑定向 pytest；全量 check.sh 归编排者收尾。
- **契约追加模式（N5，W1-A 适用）**：v3-contract 只允许「在点名小节末尾追加注记行」——§8 错误体节末尾一条（405 `method_not_allowed`/WS 501 `websocket_not_supported`/actions `invalid_request_body` 三码补录，措辞模式照抄 [4.6.0] §9 追加条目：「[X.Y.Z] 追加：…」）；§2/§3/§11 的漂移行只加行内括注（(3,4) 双版本/2.0.0 快照标注），不重写冻结正文任何既有字符；v2-contract.md 恒零改动。收尾时编排者对契约 diff 逐 hunk 复核（只允许追加/括注两类 hunk 形态，出现删改行即打回）。
- 发现详情以 `docs/audits/2026-08-20/02-findings/F-*.md` 为准（泳道自行读取）；行号基于 0b836e7 快照，漂移时以符号定位。

---

## Wave 1（四泳道并行）

### W1-A 文档漂移批（纯 docs）

**写域**：`docs/operations.md`、`docs/develop.md`、`docs/specs/INTERFACE_MAP.md`、`docs/specs/v3-contract.md`（仅点名行）、`docs/specs/CLIENT_CHANGES.md`、`docs/manual/traffic-accounting.md`。

- [ ] **F-339 runbook 增补**（finding 内 19 条缺口清单逐项）：operations.md §5.5 扩完整 env 表（①-⑩ 全部零记载 env：DIRECTORY_ALLOWLIST 三态语义/SSE 五旋钮/token 四旋钮/REPLAY 三参数/QP_SWEEP/DBAUX_PROBE/CLIENT_ID_HASH/ETAG_ENABLED/观测关闭后果/COMPRESS_ON_STARTUP）+ 新「allowlist 运维」节（含 F-252 裁决前现状边界声明：仅 file 族+SSE 帧+events 过滤）+ §7 扩 503 场景矩阵（⑪allowlist×dbaux down ⑫search×db-down）+ §9 扩 SSE/replay/transform_busy 三行 + metrics 探针带参提示（⑯）+ crash-loop 判据（⑰）+ 观测面自检关键词（⑱）+ §11 小节编号漂移修正（⑲）。
- [ ] **F-123**：INTERFACE_MAP 头部「v3-only 终态/supported:[3]/v=4 不支持」→ (3,4) 双版本表述。
- [ ] **F-125/F-151**：v3-contract §2 表/§3「available:[3]」行 + §3a 冻结文本 → 加 (3,4) 双版本注记（沿头部 2026-08-19 注记风格；不重写冻结正文，追加勘误注记行）。
- [ ] **F-156**：v3-contract §11 测试矩阵节首加「2.0.0 门控快照，未随版本演进出注」标注 + 11.11 已删对象/11.16 未落地两条勘误注记。
- [ ] **F-020**：develop.md 钉值 (3,3)→(3,4)、check_routes_doc L311 修复提示改指 v3-contract、measure_token_overhead 锚点更新（sse_frame 迁移后位置）。
- [ ] **F-139**：traffic-accounting.md passthrough 教学口径更新 + sse_observability dim 列表补 v4。
- [ ] **F-157 剩余**：CLIENT_CHANGES.md 权威指针指 v2 契约的行改指 v3/v4 + 已删除头/信封头/token gzip 例外/truncated 语义五族时效修正。
- [ ] **F-152/F-153/F-154/F-155 契约归宿句**（文档面；行为零改动）：v3-contract §8 错误体节追加注记行——405 `method_not_allowed`（非 GET /slimapi/versions）、WS 501 `websocket_not_supported`、actions 第 8 码 `invalid_request_body`（422）三码补录；v4-contract §4.3 或 §8 对应补同句；F-155：v4-contract §3.2 `allowlist` 字段位置措辞修正（明示嵌套于 `features.allowlist`）。
- [ ] 验证：`./scripts/check.sh` 的路由↔文档一致性（编排者收尾跑）；本泳道 grep 抽查（(3,3) 零残留等）。

### W1-B 死代码清扫 + 下界校验

**写域**：`pyproject.toml`（仅删 respx 一行，本计划显式解禁此一处）、**仅限下列点名文件**：`src/oc_slimapi/tokenstream/hub.py`、`src/oc_slimapi/tokenstream/frames.py`、`src/oc_slimapi/sse/hub_types.py`、`src/oc_slimapi/sse/global_hub.py`、`src/oc_slimapi/sse/replay_log.py`、`src/oc_slimapi/qp_sweep.py`、`src/oc_slimapi/upstream.py`、`src/oc_slimapi/config.py`、`src/oc_slimapi/directories.py`（build_sessions_query dead import 所在，以 rg 定位为准增删但**显式排除 `src/oc_slimapi/app.py`**——归 W1-C）+ 对应 tests。派发前编排者对四泳道写域做机器路径交集校验（脚本比对白名单，交集非空即拒绝派发）。

- [ ] **F-018**：pyproject 删 `respx`；验收拆域（N2）——① `rg -l 'import respx|from respx' src/ tests/ scripts/` 零命中；② pyproject/pylock 若存在 respx 声明零命中；③ docs/audits/CHANGELOG 等历史文档命中**仅为审计提示，不作完成条件**。
- [ ] **F-024 七项**：`_busy_sids`（tokenstream）、`last_touch`（replay_log）、`recycle` 近 no-op、`directory_source`（qp_sweep 死参）、`strip_hop_by_hop`（upstream.py，删除前 rg 确认零消费者——若 golden/测试引用则连测试一起清）、`build_sessions_query` dead import、`_V4_PARENT_RESERVED`。逐项删除 + 既有测试绿；若某项删除会改行为（非纯死码）则记录跳过并说明。
- [ ] **F-138 死符号群**：`SELECTOR_V2`/`SELECTOR_ABSENT` 常量、hub shim `_LAST_UPDATED_AT_BY_SID_MAX` 死 re-export、global_hub 死 `import logging`、hub_types 死 `logger`。
- [ ] **F-030**：tokenstream/hub.py:663/:760 两处 TODO 解除（properties part/fields key casing 按上游 camelCase 定论改注释为已决）。
- [ ] **B2-6**：config.py `max_message_bytes` 下界校验（≥1，validate() 抛 RuntimeError 同族风格）+ test_config 用例（0/-1 拒、1 过）。
- [ ] 定向：受影响模块既有测试 + test_config.py 全绿。

### W1-C 关停超时对齐（F-010/F-214）

**写域**：`deploy/oc-slimapi.service`、`src/oc_slimapi/app.py`（仅排水预算处）、tests。

- [ ] 方案（冻结）：deploy `TimeoutStopSec=15` → `60`（覆盖 uvicorn 5s + 维护排水 30s + dbaux 5s 最坏链 + 余量）；app.py 若存在可参数化排水预算（`OC_SLIMAPI_DRAIN_BUDGET_S`?）仅评估——若引入须 env 表同步（operations.md 归编排者收尾统一并入 W1-A 产出后状态）。**默认只改 deploy + 注释说明最坏链算术**。
- [ ] 定向：test_app_main.py 绿；operations.md 的 TimeoutStopSec 文案 = **coordinator-only patch（N4）**：W1-C 只在报告中给出文案草稿（锚点 = §2 部署节 systemd 参数表 + §6 关停排障节），编排者 Wave 1 收尾时以独立小 commit 合入（必须出现：TimeoutStopSec=60、最坏链算术 5s+30s+5s+余量、与 F-010/F-214 的关联注记），避免与 W1-A 的 operations.md 写域并行冲突。

### W1-D resync 值域运行时防线（F-122/P5）

**写域**：`src/oc_slimapi/sse/`（常量收口处）、`tests/`（新增防线测试）。

- [ ] 现状：route 层直发 resync 帧不经 `V4_RESYNC_REASONS` 门控。防线：将 reason 常量集收敛为单一模块级冻结 frozenset（若已存在则统一 import 源），新增测试断言**全仓 `reason=` 直发点的值 ∈ 该集**（**N1 扫描规格冻结**：基于 `ast` 解析 `src/oc_slimapi/` 全部 .py——枚举所有关键字 `reason=` 的调用点与 `sse_frame({...}, event="resync")` 构造点，断言其实参 ∈ 冻结 frozenset 的字面量/常量成员引用（允许 `REASON_X` 常量名传播，禁止其他自由变量）；扫描文件清单 = src/ 全树（tests 不扫）；测试含负向用例——临时注入未知字面量 `"canary_new_reason"` 后断言扫描器变红）。
- [ ] 定向：新测试 + sse 既有测试绿。

**Wave 1 收尾（编排者）**：写域复核 → W1-C 的 operations 文案补入 → CHANGELOG [4.6.1]（Docs/Fixed 内部）→ check.sh → release patch → push → 本机服务更新（host 已回环，仅版本刷新）。

---

## Wave 2：性能 offload（单泳道，F-201/F-271/F-202 族）

**写域**：`src/oc_slimapi/routes/messages.py`、`src/oc_slimapi/upstream.py`/read 路径所在、`src/oc_slimapi/gzip_util.py`、`src/oc_slimapi/sse/registry.py`（若 skeleton 哈希涉及）、tests。

- [ ] F-201/F-271：messages 列表/merged 200 尾部 gzip(level-6) + ETag sha256 移入 worker job（pool.offload 既有基建）；保持 wire 输出逐字节不变（ETag/gzip 产物一致）。
- [ ] F-202 read_passthrough 尾部 sha256 判定 + gzip 同题处理（评估纳入或记录理由）。
- [ ] F-203/F-204/F-205/F-206 同族逐项评估：envelope 尾双跑/write 回显/access-log emit/snapshotter 写盘——按「事件循环纪律」统一处置或逐项记录豁免理由（量级小实测无害的可豁免但须注记）。
- [ ] **基准（N3 规格冻结）**：① 基线捕获 = offload 前先跑「录制用例」把代表性响应（列表 200 尾 identity/gzip 两态 × Accept-Encoding 变体、merged 200、ETag 命中 304 / 未命中 200、空 items / 单条 / 满 16 条边界、422/503 错误体）的 status+headers+body sha256 落 golden 文件；② offload 后同输入回放逐项 hash 相等；③ 事件循环退出证明 = pool.offload 调用点 mock 计数（offload 前 0 次、后 ≥1 次/请求）。

**Wave 2 收尾**：CHANGELOG [4.6.2]（Fixed 性能内部）→ check.sh → release patch → push。

## Wave 3：大拆分 + B12 起步（三泳道，offload 落地后）

- **W3-1 tokenstream/hub.py（2190 行）五模块化**（F-301）：按审计 e1-01 卡片的五职责切分（连接/订阅者账本/flush loop/背压/replay 桥）；纯移动零行为变更；测试锚点随迁。
- **W3-2 messages.py（1643 行）三族拆包**（F-302）：列表/merged/single-flight 三族 → 子模块；路由注册表不变。
- **W3-3 questions/permissions 共享聚合框架**（F-304，相似度 0.832）：提取共享实现，两路由变薄壳。
- **B12（升级为 v4 分支开工准入门，B3）**：Wave 3 内**完成**（非起步）——① 12 个双视图测试文件 v4 自包含 golden 化改造（等价断言不再引用 v3 路径/fixture，对照系字面化）；② 106 个 v3 单态函数字面化改造；③ 完成判据 = 全量 check.sh 绿 + `rg -l 'v=3|"3"' tests/` 仅剩显式 v3 契约锁定用例清单（白名单文档化）。**B12 未完成前，v4 分支不得开始任何 v3 面移除**（评估文档 §3 硬前置）；工程量大时允许 W3 泳道扩编或独立 W3-B12 泳道。
- **等价性门（N6，每泳道必备）**：拆分前先落「接口矩阵快照」——路由表（method/path/错误码族）/ SSE 帧形 golden / ETag-Vary 头快照，拆分后逐项 diff 为空才准入收尾；F-304 共享框架额外要求 questions/permissions 两路由的响应体与错误路径 golden 对比；测试锚点随迁清单入报告。
- 收尾：CHANGELOG [4.7.0]（Refactor）→ check.sh（全量测试是拆分的主验收）→ release minor → push。

## v4 分支：v3 全面移除（Wave 3 后从 main HEAD 拉出；不发版不部署）

> **W3 收尾注记（2026-08-21，B12 落地后）**：测试侧 B12 已完成（双视图 golden 化 + 106 函数三分处置 + 白名单 `docs/ocmar/plans/2026-08-21-batch3-b12-whitelist.md`）——v4 分支**开工门（B12）要件达成**。契约侧 B12（F-126：v4-contract 自包含化，67+13 处 v3 引用正文化）未入 Wave 3，按门控 rev3 裁定 = **v4 分支上的第一个动作**，先于任何 v3 面移除（满足「唯一必须在任何 v3 面拆除之前完成」的顺序纪律）。

- [ ] `git branch v4`（Wave 3 完成后）；**分支纪律（B6 措辞冻结）：merge gate 通过前，禁止 merge 回 main / 运行 release.sh / 本机部署；gate 通过后允许一次性合并**。机械防线：① 分支内 `scripts/` 加 preflight 检查——`git branch --show-current == v4` 时 release/deploy 类脚本直接非零退出并打印「v4 branch: merge gate not passed」；② docs/operations.md 部署节加规则「本机部署仅允许 main 分支 HEAD + 已发版 tag（`git describe --exact-match` 校验），v4 分支拒绝」；③ 编排者每次会话接触本仓库先 `git branch --show-current` 确认所在分支。
- [ ] **版本窗收窄**：versioning.py `ACCEPTED_CLIENT_VERSIONS (3,4)→(4,)`、`SERVER_API_VERSION=4` 不变；validate/测试同步；`?v=3` → 400 `unsupported_version supported:[4]`。
- [ ] **v3 面拆除**：v3-only 分支逻辑（selector/路由/投影）、providers v3 passthrough、双视图测试的 v3 侧、v3 ETag 域；`/slimapi/versions`、health `accepted_client_versions` 输出同步。
- [ ] **契约修订**：v4-contract §0.3/§9.4「(3,4) 永久双版本」→「5.0.0 起仅 v4」正式修订（major 记录）；v3-contract.md 头部加退役章（历史契约存档地位不变）；INTERFACE_MAP 全局头改 v4-only。
- [ ] **CHANGELOG 约定（B5）**：分支内恒用 `## [Unreleased]` 节累积（无 -draft 后缀——release.sh 的 `^## \[VERSION\]` 校验不认变体）；merge 通过后、`release.sh major` 前，由**编排者**（非 fixer）将其转换为精确的 `## [5.0.0] - YYYY-MM-DD` 标题，转换本身作为 merge checklist 一项。
- [ ] **merge 门（B4 对齐评估文档 §6，冻结）**：① ocdroid W4 已发版；② **观测判据（口径按评估文档 Phase 2 原文，阈值与窗长 owner 届时书面裁定）**：`recordType=="request"` 过滤下 wireVersion v3 占比持续低于 owner 阈值 **且** sseActive v3 连接 == 0（access log `sse_open` 维度）；③ 非 ocdroid 残留流量核查（匿名消费方 + opencode :14096 直连面 —— 直连退役状态确认，评估文档 §7 风险 5）；④ B12 完成（已前移为开工门，此处复核）；⑤ owner 终审。全过 → 一次性 merge → main → CHANGELOG 标题转换 → `release.sh major`（5.0.0）→ 本机部署。观测数据源与查询样例 = docs/manual/traffic-accounting.md（P4 已手册化）。
- [ ] 分支上全量 check.sh 绿为准入；分支存活期定期 rebase main（若 main 有新提交）。

## Backlog 全量覆盖矩阵（B1）

| backlog 项 | 归属 | 备注 |
|---|---|---|
| B2-1（F-339/123/125/151/157/020/139 + F-346..F-353 + F-124 + F-155/152/153/154/156） | **W1-A**（扩容） | F-346..F-353 八条 docs 漂移（README/AGENTS/operations 示例/INTERFACE_MAP 行/traffic-accounting 口径/release.md 指针/设计稿横幅回写/死链标注）与 F-124（CLIENT_CHANGES 头部补 v4 迁移指引指针——主体已在 4.6.0 落地，此处补指针行）全部并入 W1-A 清单 |
| B2-2（F-201/271 + F-202..206） | Wave 2 | |
| B2-3（F-010/214） | W1-C | |
| B2-4（F-152/153/154/155/156） | W1-A | 契约归宿句（行为零改动） |
| B2-5（F-018/024/138/030） | W1-B | |
| B2-6（max_message_bytes 下界） | W1-B | |
| B3-1/2/3（F-301/302/304） | Wave 3 | |
| R-1（E-II 收敛） | **已完成**（4.6.0 R-1a + 评估文档 R-1b） | 后续动作待 owner 读评估后指令 |
| R-2（providers v3 敏感面） | **显式排除本计划** | 随 v3 面拆除自然消解（v4 分支）；不阻塞任何 wave/merge 门 |
| R-3（客户端指引） | **已完成**（4.6.0 CLIENT_CHANGES） | |
| R-4/R-5 | **已完成**（4.6.0） | |
| R-6（v3 退役推进） | **本计划 v4 分支即其 Phase 2-3 载体** | 阈值/窗长 owner 届时裁定（merge 门②） |

## Criterion Ownership Matrix（摘要）

| 波 | 覆盖 | Owner | 关键验收 |
|---|---|---|---|
| W1-A | F-339/123/125/151/156/157/020/139/152/153/154/155 | W1-A | grep 抽查 + check.sh 一致性 |
| W1-B | F-018/024/138/030 + B2-6 | W1-B | rg 零命中 + test_config |
| W1-C | F-010/214 | W1-C | deploy 算术注释 + test_app_main |
| W1-D | F-122/P5 | W1-D | 源码级扫描断言测试 |
| W2 | F-201/271/202(+203-206 评估) | W2 | 响应字节一致断言 |
| W3 | F-301/302/304 + B12 起步 | W3×3 | 全量 check.sh |
| v4 分支 | v3 移除全量 | 编排者督战 | 分支 check.sh + 开工门（B12 完成）+ merge 五门 |
