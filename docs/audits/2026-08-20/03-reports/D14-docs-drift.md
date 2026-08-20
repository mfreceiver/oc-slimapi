# D14 — A14 文档漂移审计（docs 全量 + AGENTS/CHANGELOG/README）

> 审计专项报告 · Phase 2 / A14 · 2026-08-20
> 快照：`0b836e7`（release: v4.4.0）。本文全部 `file:line` 属该快照、均为本轮实读/rg 实取。
> 输入：docs/ 全部 + AGENTS.md + CHANGELOG.md + README.md + `01-explore/docs-notes.md`（D/S 清单底稿）+ `scripts/check_routes_doc.py`。
> 纪律：仓库零改动（本审计目录除外）；D04（A4 契约质量）与 D02 已裁决条目**不重复立 findings**，只引用；设计文档与契约冲突按方案 §0.2/§164 只记漂移不立 findings。
> 严重度刻度：P0-P3（docs 行默认 P3，见 D04 §7 同款口径；P1 保留给已裁决的 F-004 部署面矛盾）。

---

## 0. check_routes_doc.py 保障范围裁定（A14 依赖结论）

`scripts/check_routes_doc.py` 是唯一自动化文档 gate，其保障面（脚本自述 + 代码核实）：

| 保障 | 机制 | 证据 |
|---|---|---|
| 路由存在性 + method 一致 | AST 收集 `routes/*.py` 全部 `@router.<method>`（54 条），逐条要求出现在 INTERFACE_MAP **表行**首单元格 `**<METHOD> \`<path>\`**` | check_routes_doc.py:10-20,153-180,241-275 |
| 语义关键词（7 路由白名单） | sessions / messages×3 / command / agent 行须含指定错误码子串；expand 两路由须含 `EXPAND_CATEGORIES` + `12` | check_routes_doc.py:71-87 |

**保障范围外（= 本报告管辖的漂移面）**：描述列内容的正确性（版本窗字面、Vary 值、头要求、非白名单路由的错误码）、文件头横幅与 prose、INTERFACE_MAP 以外的一切文档（AGENTS/README/operations/develop/CLIENT_CHANGES/release/traffic-accounting/specs 设计稿）、env 表与命令示例、CHANGELOG↔实现↔测试三向一致性、死链。静态扫描盲区（动态注册路由）脚本自述不适用（当前全部静态声明，check_routes_doc.py:34-38）。

结论：**现存全部漂移都在 gate 盲区内**——这正是 D14 各条的定性基础。

---

## 1. docs-notes D1-D22 逐条裁决

> 底稿实有 **19 条**（D1-D18 + D22；D19-D21 编号不存在于 docs-notes.md——该文件 §7「共 22 条」为底稿自身计数笔误，state-machines.md 的 D19/D20 是另一套编号体系，不属本文管辖）。裁决前全部锚点经本轮 rg/sed 复核。

| # | 位置 | 漂移内容（复核后） | 定级 | 处置 |
|---|---|---|---|---|
| D1 | AGENTS.md:78（另 :21/:22/:116 同族） | 「`ACCEPTED_CLIENT_VERSIONS`，当前 `[3,3]`」vs versioning.py:44 `(3,4)`、:38 `SERVER_API_VERSION=4`；同文件 :21「v3-only 选择器（`?v=3`）」、:22/:116「v3-contract 唯一 wire 基准，v3-only 终态」均未随 4.0.0 双版本窗口更新（:121-122 索引表又并列列出 v4-contract——文内自相矛盾） | **P3** | 新立 **F-347** |
| D2 | AGENTS.md:73 vs :61 | 硬规则行注「check.sh（当前 = `pytest tests/`）」；实际 check.sh = pytest + check_routes_doc.py + `compileall src`（scripts/check.sh:18-27）；:61 流程表描述亦不含 compileall。文内两处描述均与脚本不符 | **P3** | 并入 **F-347**（同文件未跟进族） |
| D3 | AGENTS.md:20 | 「当前**不读** opencode SQLite（v4 起经只读投影源 mode=ro 读…）」自相矛盾句式：4.0.0 已发版且 operations.md §7 声明生产已部署 dbaux，读 SQLite 是现状而非「v4 起」的将来 | **P3** | 并入 **F-347** |
| D4 | operations.md:92-94 vs deploy/oc-slimapi.service:32-33 | 「生产 unit 已同款清理，模板不再示例」为不实陈述：权威模板 :32/:33 仍逐行示例 `OC_SLIMAPI_SERVER_API_VERSION=2` / `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`（:33 与钉死 (3,4) 冲突 = startup-fatal） | **P1** | **已裁决，不重复立**：F-004（P1 crash-loop，D04 §7 终判）+ F-005（P3 warning 面）；deploy 三方对账终裁归 A13 |
| D5 | operations.md:353-361（另 :458） | health 期望响应示例 `api_version:3` / `[3,3]` / `sidecar.version 1.1.1` / features 无 `allowlist` 键（[3.3.0] 起存在、v4 视图另有 `auxiliary`）；:458「supported:[3]」应为 [3,4] | **P3** | 新立 **F-348** |
| D6 | operations.md §3.2 内嵌 unit 示例（:75-120） | 缺 `TimeoutStopSec=15` 与 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30` 两行（deploy 模板 :24/:45 均有；§4:164/§5.3:239 文字引用了这两个值）——示例/文字/模板三处不同步，有「以 deploy 为准」兜底句（:122） | **P3**（低） | 并入 **F-348** |
| D7 | operations.md:458 | 排障/接入表「无 `v`/`v=2` → 400 supported:[3]」未提 `v=4` 现为合法（与 D5 同族） | **P3** | 并入 **F-348** |
| D8 | develop.md:23-24 | 配置表把两已弃用 env 列为常规 knob：`OC_SLIMAPI_SERVER_API_VERSION`（默认 `3`）/ `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS`（默认 `3,3`）——实际钉 4/(3,4)，且前者设置仅 warning+忽略（config.py:796-804）、后者设非 (3,4) 值启动 RuntimeError（config.py:817-822）；表内未标弃用（operations.md:92-94 已标） | **P3** | 更新 **F-020**（其位置清单首项即此） |
| D9 | CLIENT_CHANGES.md:1-5 + :86/:136/:356/:361/:438 | 头部「Aligned with v2-contract.md (lite-v2)」+ 多处「wire 以 v2-contract §… 为准」「未 bump 仍 2」——v2 契约已两个 major 周期前退役 | **P3** | **已裁决**：F-157（A4 五族滞后，P3 维持） |
| D10 | CLIENT_CHANGES.md:112/:344/:361/:486 | 「须带 `X-Slimapi-Version: 2`」四处——头已删（v3 §1），唯一通道 `?v=` selector | **P3** | **已裁决**：F-157 |
| D11 | CLIENT_CHANGES.md:57/:75-81（另 :237） | 「翻页用 X-Next-Cursor」+「X-Complete」整节——两头已移除，envelope 替代，未标废止 | **P3** | **已裁决**：F-157 |
| D12 | CLIENT_CHANGES.md:366 | token stream「允许 gzip（首个 SSE gzip 例外）」三层语义——v3 终态 SSE 恒 identity（v3 §7.7；INTERFACE_MAP §3.1 同注） | **P3** | **已裁决**：F-157 |
| D13 | CLIENT_CHANGES.md:380/:413-414 vs :443 | 「truncated 帧携带 partEventRevision」「strict > 去重（契约 §3.x.2= v2）」——**本轮实现复核**：现行 per-session 流 truncated 帧确带 partEventRevision（sse/tokenstream/hub.py:1668 注释 + 实现；hub.py:536/646/754 strict-> 语义锁定），行为描述**准确**，漂移仅在权威指针挂已退役 v2 契约 | **P3** | **已裁决**：F-157（指针族）；实现复核结论录入本表 |
| D14 | INTERFACE_MAP.md:7/:14（banner+selector 段）+ :95-97（versions/health/ready 行） | 「M3 v3-only 终态（2026-08-16）/ supported:[3] / current=3 available=[3] / accepted [3,3] / health 恒 3 单视图」——与 4.0.0 (3,4) 窗口冲突（v4 §3.1/§3.2：current=4/available=[3,4]/health 双视图；versions 行内已补 v4 注记但「current=3、available=[3]」主值未改） | **P3** | 新立 **F-349** |
| D15 | INTERFACE_MAP.md:98（metrics 行） | 「须带 `X-Slimapi-Version:int`…缺/坏/越界版本头→门闩 400」——头已删，metrics 与其他 /slimapi 路由同走 `?v=` selector | **P3** | 并入 **F-349** |
| D16 | INTERFACE_MAP.md 九处（:25/:26/:31/:32/:33/:34/:35/:39/:51） | 行文仍写双值 Vary「`Accept-Encoding, X-Opencode-Directory`」——实现恒单值（etag.py:171-176 `merged_vary` 无条件返回 `Accept-Encoding`；test_vary_directory_unconditional.py 11 用例锁定）；与自身 :7 横幅「Vary 全路由收缩为单值」**文内自相矛盾**。注：banner 有「v2 时代注记以终态声明为准」兜底句，但其中 B1（2026-08-16，v3 时代）注记与 todo/children/diff 返回列主值不属于「v2 时代注记」，兜底不覆盖 | **P3** | 并入 **F-349** |
| D17 | INTERFACE_MAP.md:99（catch-all 行） | 收编面「读 7 组 + 写 12 端点」——v3 §10 现行读 9 组 + 写 17 端点（v3-contract.md:214；3.0.0 时点口径未加历史注） | **P3** | 并入 **F-349** |
| D18 | traffic-accounting.md:34（头部横幅+§2） | 「缺 v / v=2 / 不支持值 → 400 `unsupported_version supported:[3]`」——与 (3,4) 窗口冲突，且与自身 §5.1 字段表（:169-170 已载 wireVersion "4"/selectorResult v4）**文内不一致** | **P3** | 新立 **F-350** |
| D22 | release.md:45/:47/:63/:197 | §1.2 与 §7 文件职责表把 wire 契约权威仅指向 v3-contract（「接受区间见 versioning.py 与 v3-contract §1/§2」）——未列 v4-contract.md；与 AGENTS.md 索引（v3+v4 并列）不同步；§1.3 双版本期文字已有但职责表未跟上 | **P3**（低） | 新立 **F-351** |

---

## 2. specs 扫读条目 S1-S12 裁决

| # | 文档 | 裁决 | 定级 | 处置 |
|---|---|---|---|---|
| S1 | design-v2.md:1-13 | 「aligned with v2-contract.md」+ 硬约束「禁 SQLite」「版本走必填请求头 `X-Slimapi-Version`」均过时；AGENTS.md:113 仍将其列为「当前态设计（接口/骨架/部署）」入口——入口定位误导（历史 rationale 仍有效） | P3 | 只记漂移（方案 §164：设计文档低于契约不立 findings）；入口定位问题在 F-347 提及 |
| S2 | design-token-stream.md:3 | 自我定位「历史设计稿」充分，但头部「当前 wire 契约以 **v2-contract §3.x** 为准」指针未改（AGENTS 索引行已代为更正为 v3 §7） | P3 | 只记漂移 |
| S3 | v2-contract.md:3 | 文件头仍自称「**This is the AUTHORITATIVE wire contract**」且全文无退役横幅——与 AGENTS/v3 §0「≤2.x 历史契约」定性冲突，读者从文件本身无从得知已退役 | P3 | **已裁决**：F-019（A4 终判 P3 维持；A14 复核见其文件） |
| S4 | design-expand.md:5 | 上游对齐基准 v1.18.16 vs AGENTS 声明 current 已 repoint v1.18.18——快照对齐滞后（v3 §4b 亦注 v1.18.16 基准并要求 repoint 时复核） | P3（低） | 只记漂移 |
| S5 | design-message-watermark.md:3 | 「v2-contract §消息列表加性节为 wire 摘要」——现行 wire 载体为 v3 §4a.6（2026-08-19 就地载明），指针滞后 | P3（低） | 只记漂移 |
| S6 | design-v4-selector.md | 无冲突（自声明契约优先级正确） | NONE | — |
| S7 | design-v4-dbaux.md | 无冲突（上游对齐 v1.18.18 与 AGENTS 一致） | NONE | — |
| S8 | design-v4-sse-replay.md | 无冲突（「现行 wire 以 v3-contract §7 为准」表述正确） | NONE | — |
| S9 | design-v4-qp-payload.md | 无冲突 | NONE | — |
| S10 | access-log-writer-design.md | 无冲突（DESIGN ONLY 自声明充分） | NONE | — |
| S11 | chat-toolcard-slimapi-plan.md:3 | 「阶段 A：方案完善，不开工实现」横幅**仍准确**（skeleton.py:256 注释「本轮不实现」佐证未实施）——无状态漂移；其引用的 `chat-toolcard-investigation.md` 不在本仓（见 §4 死链） | NONE | 死链归 F-353 |
| S12 | traffic-route-todo-2026-08-10.md:8 / traffic-route-children-2026-08-10.md:8 | 头部「**PROPOSAL — NOT IMPLEMENTED**. No code, no contract, no INTERFACE_MAP, no CHANGELOG」——todo/children 路由**已实现并收编**（INTERFACE_MAP §1 :31-32 表行、v3 §10 消费集、CHANGELOG 2026-08-16 T17/T18）；设计稿落地后未回写状态横幅 | **P3** | 新立 **F-352** |

---

## 3. 底稿未覆盖、本轮新增漂移面

### 3.1 README.md（v2 时代全文未随 3.0.0/4.0.0 更新）→ **F-346**

| 位置 | 漂移 |
|---|---|
| README.md:3 | 「（不读 SQLite）」——v4 起 dbaux 只读 SQLite 生产启用（与 AGENTS D3 同族） |
| README.md:5 | 「权威 wire 契约为 `docs/specs/v2-contract.md`」——已退役两个 major 周期；应为 v3/v4 |
| README.md:29 | 快速开始验证命令 `curl --fail -H 'X-Slimapi-Version: 2' …/slimapi/health`——头已删且无 `?v=3`，**照抄即 400**（quick-start 实际不可用） |
| README.md:35-38 | 「范围（v2 / wire `X-Slimapi-Version: 2`）」整节 + 「其他 HTTP path：流式反代（catch-all）」——头已删、catch-all 已关（404 thin_route_not_found） |
| README.md:41 | token stream「默认 gzip（lever2…）」——v3 终态 SSE 恒 identity |
| README.md:56-57 | 文档表「v2-contract.md = Wire 契约权威」「design-v2.md = 当前态设计」——未列 v3/v4-contract |

定性：仓库门面文档（GitHub 落地页）整体停留在 v2 时代。严重度与 F-157（CLIENT_CHANGES，A4 裁 P3）同刻度取 **P3**——纯文档缺陷非运行时，但可见度全仓最高、quick-start 命令直接失败。

### 3.2 死链扫描 → **F-353**

`rg -o "docs/specs/[a-z0-9-]*\.md" docs/ *.md scripts/` 全集 18 个目标，**15 个存在、3 个不存在**：

| 死链目标 | 引用数 | 引用位置 | 定性 |
|---|---|---|---|
| `docs/specs/v1-contract.md` | 16 | CHANGELOG.md ×8（历史版本节）+ docs/ocmar/{plans,reports,specs} ×8（冻结归档） | v2-contract.md:3 明言「v1-contract.md 已废弃并移除（文件已删除）」——历史记载指向已知删除文件，可辩护；但 CHANGELOG 内 8 处无一处标注「已删除」 |
| `docs/specs/v1-impl-spec.md` | 9 | CHANGELOG.md ×1 + docs/ocmar/* ×8 | 同上（无「已删除」标注） |
| `docs/specs/chat-toolcard-investigation.md` | 2+1 | chat-toolcard-slimapi-plan.md:6/:431 + **src/oc_slimapi/skeleton.py:256（代码注释）** | 计划文档 :6 括注「（ocdroid）」暗示跨仓文件，但 skeleton.py:256 引用 `docs/specs/chat-toolcard-investigation.md §B.8` 无仓限定——本仓 docs/specs 下不存在，跨仓歧义 |

现存文档间的 specs 交叉引用（v2/v3/v4-contract、design-*、traffic-route-* 等）**零死链**。

---

## 4. CHANGELOG 4.2.0–4.4.0 三向核对（全量，C14 验收项）

> 方法：三个版本节（CHANGELOG.md:29-103）全部 Added/Changed/Removed 条目逐条提取（20 条），rg 核对实现锚点与测试锚点存在性。**结果：20/20 全对**——无幽灵条目（CHANGELOG 声称而实现不存在）、无漏报（实现存在而 CHANGELOG 未记的 4.2.0+ 行为变更由 D01/D03 辖，此处不重复）。

| # | 版本 | 条目摘要 | 实现锚点 | 测试锚点 | 判定 |
|---|---|---|---|---|---|
| 1 | 4.4.0 Added | ModelEntry 恢复 optional `limit`，子键恰 {context,input,output}，int-else-omit、bool 排除、orjson 边界 [-2^63,2^64-1] | providers_projection.py:73-81（_ORJSON_INT_MAX）、:344-356 | test_providers_projection_v4.py:336/352/374/400/415/429/463/502/516（limit 族 9 测试） | PASS |
| 2 | 4.4.0 Added | 省略策略零错误路径（absent/null/非 object→整键省略；绝无 `"limit": null`/`{}`） | providers_projection.py:23-25,73-80 | test_v4_limit_absent_null_non_object_omitted_never_malformed:352、test_v4_optional_keys_all_omission_forms:294 | PASS |
| 3 | 4.4.0 Added | 投影指纹 bump `providers-projection-v1`→`v2`（旧 v4 ETag 自然失效） | providers_projection.py:66 | test_v4_limit_revision3_bumps_representation_fingerprint:447 | PASS |
| 4 | 4.4.0 Added | 限额与 readiness 不变（四限额 256/1024/64/8MiB；10/10 ready） | providers_projection.py:54-57；readiness.py:58-69（十 ID） | test_v4_limits_are_frozen_wire_constants；test_versions_readiness.py(35) | PASS |
| 5 | 4.3.0 Added | readiness U 9→10（`session.post-actions.v4`）+ 蕴含⑦ | readiness.py:58-69（REQUIRED 十项）、:126-144（validate_dependencies） | test_versions_readiness.py:263-268 | PASS |
| 6 | 4.3.0 Added | `POST /slimapi/session/{sid}` ≡ PATCH 逐字节等效 | write_groups.py:326（post_update_session→_write_passthrough PATCH） | test_post_actions_v4.py(29) | PASS |
| 7 | 4.3.0 Added | `POST …/delete` ≡ DELETE（实体处理相同、非幂等可接受） | write_groups.py:399 | test_post_actions_v4.py | PASS |
| 8 | 4.3.0 Added | `POST …/archive` 便捷（octet 缺省判据、`{"time":{"archived":<ms>}}` 紧凑合成） | write_groups.py:343,389-391 | test_post_actions_v4.py | PASS |
| 9 | 4.3.0 Added | 并存非替代：PATCH/DELETE 不退役；`?v=3` 恒 404 | write_groups.py + selector | test_post_actions_v4.py:546（test_v3_returns_thin_route_not_found） | PASS |
| 10 | 4.3.0 Added | 过渡态 405 让位（激活后 coded 405 面消失；transitional fixture 永久锁定 4.2.0 行为） | selector.py:216-249 | test_post_actions_v4.py:173（transitional_gates）/:561；test_method_boundary_v4.py(16) | PASS |
| 11 | 4.3.0 Removed | cascade 编排层 + cross-session search 升永久 non-goal；「POST-only update」deferred 移除 | v4-contract.md:693-700（§17 修订二）；rg 负向：src 无实现（selector.py:251 仅注释） | 负向由 readiness 全集（无 ID）+ test_versions_readiness 锁定 | PASS |
| 12 | 4.3.0 观测 | 三条 POST 入 INTERFACE_MAP（54 条，write 20）；bucket 继承 `write_session` | write_groups.py 20 装饰器；traffic.py:158,163,179 | test_check_routes_doc.py gate（本轮独立复算：doc/code 各 54 条 method+path 集合双向差为空） | PASS |
| 13 | 4.2.0 Added | readiness 门禁（九 ID、f(A) 规范化、ready 公式、按 feature 独立门控、expand iff） | readiness.py:58-187；versions.py:120-133 | test_versions_readiness.py(35)、test_readiness_gating_integration.py(6) | PASS |
| 14 | 4.2.0 Added | providers 安全投影 §12（白名单 schema、四限额、十二步求值序、canonical=ETag 输入、502/413 错误族） | providers_projection.py:54-66,98-108,300-356；read_groups.py:282-355 | test_providers_projection_v4.py(51) | PASS |
| 15 | 4.2.0 Added | session 单查 parity §13（唯一 canonical projector、presence 语义、degraded 公式） | sessions.py:452-479,659-675 | test_session_single_v4.py(50) | PASS |
| 16 | 4.2.0 Added | expand 闭环 §14（href 按视图生成、`capabilities["4"].expand` 首次广告） | skeleton.py:167-187（_expand_ref wire_view）；versions.py:125-133 | test_expand_href_v4.py(11) | PASS |
| 17 | 4.2.0 Added | 表示层 §15（Vary 修复 4.0.0 bug、ETag identity 强/gzip 弱、304、域隔离、关态仍发 Vary） | sessions.py:598-630 | test_sessions_v4_representation.py(10) | PASS |
| 18 | 4.2.0 Added | method 边界 §16（三条 POST v4→405 coded、Allow 字面、优先级插列） | selector.py:244-280,581-609 | test_method_boundary_v4.py(16) | PASS |
| 19 | 4.2.0 Added | 门控关态逐字节回退 4.0.0 行为（双态测试锁定）；`?v=3` 零改动 | 各路由门控分支 + selector | test_sessions_v4_matrix.py:488（gate_off_no_etag_vary_304）/:48（single revision gate off）/:547；test_v4_dual_window / test_v3_rawbody_regression | PASS |
| 20 | 4.2.0 观测 | providers v4 投影入既有 `providers` 桶（wireVersion="4" 维度） | traffic.py:152；access_log.py:309,352,400 | test_v4_observability / test_access_log_v3_fields | PASS |

辅助复核：4.3.0「54 条，write 20」独立复算——doc 表行与代码装饰器各 54 条 (method,path)，双向差集为空；write_groups.py 20 装饰器 = POST 18 + PATCH 1 + DELETE 1（doc 全仓 POST 19 含 actions.py 1 条，与「write 20」口径不同但各自正确）。

---

## 5. 漂移条目总表（裁决后全集）

> 编号沿用底稿 D/S；R=本轮新增。定级列 P1 一条为 D04 已裁决项的引用（不重复立 findings）。

| 编号 | 位置 | 漂移内容 | 定级 | findings 归属 | 建议动作 |
|---|---|---|---|---|---|
| D1 | AGENTS.md:78,21,22,116 | 版本窗 [3,3]/v3-only 表述未随 4.0.0 更新 | P3 | F-347 | 改 `(3,4)`；v3-only 改「(3,4) 双版本窗口，v4 差异见 v4-contract」 |
| D2 | AGENTS.md:73（vs :61） | check.sh 描述 =「pytest tests/」失实 | P3 | F-347 | 两处统一为三项（pytest + gate + compileall） |
| D3 | AGENTS.md:20 | 「当前不读 SQLite」自相矛盾句 | P3 | F-347 | 改「经只读投影源 mode=ro 读（4.0.0 起），绝无写入」 |
| D4 | operations.md:92-94 ↔ deploy:32-33 | 「模板不再示例」不实 + startup-fatal 残留 | **P1** | F-004/F-005（已裁决） | 删 deploy:32-33；A13 辖 |
| D5 | operations.md:353-361,458 | health 示例 [3,3]/1.1.1/无 allowlist；supported:[3] | P3 | F-348 | 示例改 (3,4)+4.4.0+allowlist 键；[3]→[3,4] |
| D6 | operations.md:75-120 | §3.2 示例缺 TimeoutStopSec/RETAIN_DAYS 两行 | P3 | F-348 | 补两行或示例直接指向 deploy 模板 |
| D7 | operations.md:458 | 未提 v=4 合法 | P3 | F-348 | 同 D5 |
| D8 | develop.md:23-24 | 弃用 env 列常规 knob 且默认值过时 | P3 | F-020（更新） | 表行标「已弃用（4.0.0）」并注明 warning/RuntimeError 行为 |
| D9-D13 | CLIENT_CHANGES.md 多处 | v2 权威指针/已删头/gzip 语义五族 | P3 | F-157（已裁决） | 见 F-157；D13 行为已实现复核无误 |
| D14 | INTERFACE_MAP.md:7,14,95-97 | v3-only 终态/current=3/[3,3] 主值未更新 | P3 | F-349 | banner+三行主值改 (3,4) 口径 |
| D15 | INTERFACE_MAP.md:98 | metrics 行要求已删头 | P3 | F-349 | 改 `?v=` selector 口径 |
| D16 | INTERFACE_MAP.md 九处 | 双值 Vary vs 实现单值（文内自相矛盾） | P3 | F-349 | 九处统一单值 `Accept-Encoding` |
| D17 | INTERFACE_MAP.md:99 | 读 7 组+写 12 端点（现行 9+17） | P3 | F-349 | 计数更新或加历史注 |
| D18 | traffic-accounting.md:34 | supported:[3] 且与自身 §5.1 字段表不一致 | P3 | F-350 | [3]→[3,4] |
| D22 | release.md:45,47,63,197 | 文件职责表漏 v4-contract | P3 | F-351 | 职责表并列 v3/v4 |
| S1 | design-v2.md:1-13 + AGENTS 索引 | 「当前态设计」入口挂 v2 时代文档 | P3 | 记录（F-347 提及索引行） | design-v2 头部加历史横幅；AGENTS 索引改「历史设计」 |
| S2 | design-token-stream.md:3 | wire 指针挂 v2 | P3 | 记录 | 指针改 v3 §7 |
| S3 | v2-contract.md:3 | 无退役横幅仍自称 AUTHORITATIVE | P3 | F-019（已裁决，A14 复核） | 文件头加「≤2.x 历史契约（3.0.0 退役）」横幅 |
| S4 | design-expand.md:5 | 基准 v1.18.16 vs current v1.18.18 | P3 | 记录 | 加「基准为快照时点」注或随 repoint 复核 |
| S5 | design-message-watermark.md:3 | wire 摘要指针挂 v2 | P3 | 记录 | 指针改 v3 §4a.6 |
| S12 | traffic-route-{todo,children}:8 | 「NOT IMPLEMENTED」横幅 vs 已实现 | P3 | F-352 | 横幅改「已实施（2026-08-16 T17/T18）」+ 指针 |
| R1 | README.md:3,5,29,35-41,56-57 | v2 时代全文（quick-start 命令照抄即 400） | P3 | F-346 | 按 v3/v4 现态重写范围节+命令+文档表 |
| R2 | 死链 ×3（v1-contract/v1-impl-spec/chat-toolcard-investigation） | 引用不存在的 specs 文件（含 skeleton.py:256 代码注释 1 处） | P3 | F-353 | CHANGELOG 历史节加「已删除」通注；skeleton.py:256 引用补仓限定 |

**分桶统计**：漂移条目共 **27** 条 = P1 ×1（D4，引用 F-004）+ P2 ×0 + **P3 ×26**；裁决 NONE ×6（S6-S11）。

---

## 6. 文档健康度总评

**总评：中等偏弱——权威链核心健康、外围文档系统性滞后。**

1. **强项**：两份现行契约（v3/v4-contract）与 CHANGELOG 4.2.0-4.4.0 的三向一致性**全对（20/20）**，实现/测试锚点齐全、零幽灵条目；specs 交叉引用死链率为 3/18 且全部集中在历史文件引用；check_routes_doc.py 对路由存在性/method 的防漂移 gate 有效（本轮独立复算 54 条双向差为空）。v4 系文档（design-v4-* 四份 + v4-contract）状态自声明纪律好。
2. **弱项（系统性）**：4.0.0 双版本窗口是**单点事件、多文档未跟进**——[3,3]/[3]/v3-only 字面残留在 AGENTS/operations/develop/INTERFACE_MAP/traffic-accounting/release 六处（D1/D5/D7/D8/D14/D18/D22），其中三处（D16/D18/INTERFACE_MAP banner vs 行文）是**文内自相矛盾**；3.0.0 头退役/catch-all 关闭是第二个单点事件，README 与 CLIENT_CHANGES 整体停留 v2 时代（R1/D9-D13）。
3. **风险画像**：全部漂移均为 P3 文档缺陷（唯一 P1 是 D04 已裁决的 deploy 模板矛盾），无运行时影响；但 README quick-start 命令照抄即失败、v2-contract 自称权威无退役横幅（F-019）两项对新读者/新消费方的误导面最大。
4. **修复成本**：26 条 P3 中约 20 条为机械字面更新（版本窗/计数/指针），可一次「文档收编型」提交关闭（无 wire 变更、无需发版协调）；结构性收编仅 F-019（actions 规范从 v2 §2 迁入现行契约）一项。

---

*（完）A14 · D14 · 快照 0b836e7 · 2026-08-20*
