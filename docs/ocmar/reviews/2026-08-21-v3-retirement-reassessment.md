# R-6：v3 退役全面重估（2026-08-21）

> 产出：批次二 Lane D（fixer-glm #4），依据 `docs/ocmar/plans/2026-08-21-batch2-decision-rollout.md` rev4 Task D-2（plan:126-130）。基线 = v4.5.0（git tag，HEAD d1b0dcd）。
> 性质：**只读分析文档**——在 owner 2026-08-21 新方向下重估 2026-08-20 审计交付物 `docs/audits/2026-08-20/04-final/v3-retirement-plan.md`（快照 0b836e7 = v4.4.0）的立场与路线。不伴随任何代码/契约/版本窗改动。
> 锚点规则：全部事实断言附 `file:line`。亲验 = 本文档写作时点对 v4.5.0 工作区实读；审计锚点 = 复用 0b836e7 快照证据。归属声明见 §9。

---

## 1. 政策基线声明（措辞冻结——照抄计划 rev4 B4 条目）

**owner 方向（2026-08-21，规划基线）**，计划 Goal 原文（plan:5，亲验）：

> 「R-6 v3 退役全面重估（新政策方向：owner 正推进 v3 全面废弃，取代 2026-08-18「(3,4) 永久双版本」冻结）。」

**本文件的分层表述（措辞照抄计划 rev4 D-2 §1 内容要求，plan:130，亲验）**：

> 「① **政策基线声明（B4 措辞冻结）**：owner 2026-08-21 明示新方向『正在推进 v3 全面废弃』——本文件将其记录为**待转化的 owner 方向（规划基线）**，尚未构成 wire 契约变更：v4-contract §0.3/§9.4 与 CHANGELOG [4.1.0] 的『(3,4) 永久双版本』冻结记录**在正式 major 发版修订前仍然生效**，Phase 3（窗口收窄 major）才触发契约正式修订与 CHANGELOG 记录；本节显式引用 owner 原话语境，方向本身不可推翻、生效节奏按上述分层表述。」

据此，本文件的全部分析遵守两条纪律：

1. **方向不可推翻**：全文不包含「建议维持永久双版本 / 质疑废弃方向」类表述——这与 2026-08-20 审计计划文档（v3-retirement-plan.md:5 §0.3 口径约束声明「本文全文不包含『建议淘汰 v3 / 建议重启裁决』类表述」、:29 §1.4「维持现状」唯一政策输出）的立场**相反**：该文档系旧方向（永久双版本）下的合规产物，其政策模块已被 owner 2026-08-21 方向取代；但其**素材模块**（§2 迁移 checklist、§3 成本模型 B1-B12、§4 维持成本量化）不依赖政策立场，本文继续引用。
2. **生效节奏分层**：现行**生效**规则仍是——v4-contract §0.3（v4-contract.md:15，亲验）：「协议封顶 4 系、**(3,4) 永久双版本窗口**、原预定 major 退役发版已取消（见 CHANGELOG [4.1.0]）。若未来评估 v3 退役，判据为**纯观测性**……**退役形式与版本号另行 owner 裁决，本契约不预设任何未来版本窗**。若裁决发生，收窄即 major，写入本节。」；§9.4（v4-contract.md:329-331，亲验）同义；CHANGELOG [4.1.0]（CHANGELOG.md:134-140，亲验）：「owner 终态裁决 2026-08-18——协议封顶 4 系，(3,4) 永久双版本，5.0.0 与 B6-2 取消」。**在 owner 走完 §6 Phase 3（窗口收窄 major 发版）前，wire 层不发生任何 v3 收窄**：`ACCEPTED_CLIENT_VERSIONS = (3, 4)`（versioning.py:44，亲验）不变，`?v=3` 语义逐字节不变（v4-contract §0.1，v4-contract.md:13，亲验）。owner 新方向的**首个 wire 触点 = Phase 3 的 major 发版**（§5）。

**一个术语澄清（避免误读）**：owner 方向是「v3 全面废弃」，含义 = wire 版本窗最终收窄到 v4-only + v3 代码/契约面拆除；它**不改变** v4 契约差异面本身（v4 已冻结且 oc-webui 生产在用）。

---

## 2. 硬阻塞盘点（ocdroid v3 全量锁定的迁移工程量框架）

v3-retirement-plan.md:155-159（§6.1，亲验）列三阻塞；按新方向重新定性——**不再是「是否做」的阻塞，而是「何时能做完」的工程排期项**：

### 2.1 阻塞①：ocdroid v3 全量锁定（工程量主项）

- 证据（审计锚点，v3-retirement-plan.md:157）：ocdroid 全量流量 `?v=3`（CHANGELOG [3.3.1]「现行消费方（ocdroid/WebUI）均使用 wire v3」；[4.2.0]「ocdroid/WebUI 均在 ?v=3」——同窗口 oc-webui 已迁 v4、ocdroid 未迁）。
- **【进展注记 2026-08-21 10:30，ocdroid 侧会话确认】阻塞①实质解除在望**：① 观测到的 v3 流量（4h 窗口 7,378 条，大头 `/slimapi/sessions/status` 轮询 61%）归属**已发布存量客户端 3.2.0**，非当前开发线；② 迁移树已完成 v4 全量迁移（仅 v4 + 硬门禁 + V3 fallback 全删），`sessions/status` 经统一拦截器自动 `?v=4`（同端点无字段差）、列表已按客户端补偿模式迁移（`?v=4&parent=none&archived=omit` + 本地 directory 分桶）、SSE 281 路全 v4 与迁移树一致；③ **无契约级阻碍**（per-directory 客户端过滤、providers limit 子键已处理）；④ 排期：W4 发版前评审收敛尾段（r9 评审→模拟器 v4 冒烟→终审→release.sh），预期近端发版，存量 v3 流量随用户升级清零。Phase 1（§6）准入条件已在满足路径上。
- **【观测注记 2026-08-21 12:54，本仓编排者生产 access log 实测（§4.4 观测通道）】v3 占比三日收敛 + SSE v3 归零**：① `recordType=="request"` 口径 wireVersion v3 占比：08-19 = 99.7%（84,494/84,754，当日 SSE 尚有 442 路 v3）→ 08-20 = 69.5%（39,741/57,208，SSE v3=0）→ 08-21（至 12:48）= **49.1%**（13,663/27,841；v4 14,019 已反超，SSE 558 路全 v4）；② 08-21 v3 流量 **100% 归属存量客户端 ocdroid 3.2.0-9e067e15**（13,663 条无一例外，无其它 client/版本混入），Top = `GET /slimapi/sessions/status` 轮询 48.8%（6,672）+ `GET /slimapi/sessions` 11.2%（1,535）+ `GET /slimapi/session/{sid}` 单条（~6%）；③ 判读：ocdroid v4.1.0（2026-08-21 04:19Z 发版）后的增量流量已全 v4，**merge 门②的「SSE active v3 == 0」分量连续两日满足**；剩余 v3 全部随 3.2.0 存量用户升级自然清零，占比阈值与观察窗长仍待 owner 书面裁定（§6 Phase 2）。数据源：`~/.local/state/oc-slimapi/logs/access-2026-08-{19,20,21}.jsonl(.gz)`（RETAIN_DAYS=3 窗口内）。
- **迁移工程量框架** = 该文档 §2 十六项 checklist（A1-A16，v3-retirement-plan.md:41-58，亲验）。核心四项（:62）：
  - **A2** sessions 参数轴换轨（directory/roots/start → archived/parent/search/cursor；`start` 时间水位无 v4 等价，增量列表逻辑重写）——**高**风险；
  - **A3** directory 服务端过滤缺口（唯一无服务端等价物项，F-121；全局拉取 + `item.directory` 客户端过滤，v3-retirement-plan.md:64-75）——**高**风险；
  - **A5/A6** 降级矩阵 + 503 处理（72 等价类、`auxiliary_unavailable` + Retry-After:30）；
  - **A11-A14** SSE 重连器重写（(epoch,seq) 状态机、冷启动改 HTTP 对齐、legacy resync → 服务端静默断连语义）。
  - 其余 30+ 路由 v4 = v3 语义原样零必改点（:60）。
- **消费字段差集已由本批 Lane C 产出**（plan:96-115，R-3 CLIENT_CHANGES v4 迁移章节四小节，亲验计划文本；小节 2 = providers/sessions 字段差集对照表、小节 3 = per-directory 补偿模式）——即 v3-retirement-plan §5-P1/P2 的落地载体。**注**：本文写作时点 git 工作区尚未见 CLIENT_CHANGES.md 改动（Lane C 与 Lane D 同批并行），完成状态以编排者收尾报告为准。
- **生产实证坑**（oc-webui 先例，v3-retirement-plan.md:77-81 亲验）：v4 providers 投影曾剥 `limit` 字段致上下文百分比失分母（CHANGELOG [4.4.0] 修复）——教训「迁移前消费字段差集全量 diff」已被 Lane C 小节 2 制度化。
- **直连退役协同**：CLIENT_CHANGES.md:477-479（亲验）——`:14096` 仍是 ocdroid 回退路径（`slim=false`/Manual 连接源），C1/C3 前置完成才退役直连。ocdroid 迁 v4 完成是该前置的组成部分（回退路径不再被使用）。

### 2.2 阻塞②：政策转化（Phase 3 前无 wire 变更空间）

见 §1：现行生效记录 = (3,4) 永久双版本；新方向须经 major 发版才转化为 wire 事实。这不是日程阻塞而是**程序约束**（版本窗变更 = major，v4-contract §0.2 v4-contract.md:14 亲验：「版本窗任何变更均为 major 发版」）。

### 2.3 阻塞③：v3 冻结回归基线（质量基础设施）

v3-retirement-plan.md:159（亲验）：v4 正确性大量以「与 v3 逐字节相同」断言（106 v3 单态函数 + 12 双视图文件 + 294 处 `v=3` 字面 ≈ >15% 断言面，总测试基数 2642 函数）。**任何 v3 面收窄/拆除前必须先完成 B12 字面化**（§3）——该前置本身即大型机械工程。

---

## 3. 等价性测试解耦（B12 字面化：路径与工作量级）

**B12 = 契约/文档继承链字面化**（v3-retirement-plan.md:105，亲验行）：v3-contract.md 整份 288 行 25 节废止 + v4-contract.md:5 继承基线条款（「凡未提及语义逐字沿用 v3」，F-126:10 亲验）+ 67 行 v3 字面 + 13 处显式沿用/继承表述（F-126:9 亲验：`rg -c "v3" docs/specs/v4-contract.md`=67、`rg -c "沿用 v3|继承 v3|v3 原样|沿袭 v3|同 v3"`=13）+ INTERFACE_MAP.md 41 处 v3 引用 + AGENTS.md「版本双轨」硬规则。

**量化基线**（F-126:12 亲验 + v3-retirement-plan.md:129 亲验；两源文件计数口径有差，并列载明）：

| 维度 | 数字 | 出处 |
|---|---|---|
| v4-contract v3 字面行 | 67 行 | F-126:9（Phase 3 复核 rg 重放一致，F-126:22） |
| v4-contract 显式继承表述 | 13 处 | F-126:9 |
| 零差异路由注载 | 47 条路由「零 v4 差异」 | F-126:11 |
| 双 wire 视图同文件测试 | 12 文件（其中 10 文件 `?v=3`+`?v=4` 字面双 selector） | F-126:12 |
| v3 单态测试文件/函数 | 8 文件 / 106 函数（test_v3_directory 26、test_traffic_snapshot_v3 20、test_access_log_v3_fields 16、test_v3_sse_meta 15、test_v3_envelope 11、test_v3_etag_domain 8、test_health_dual_view 6、test_v3_rawbody_regression 4） | v3-retirement-plan.md:129 |
| `v=3` 字面断言 | 294 处（F-126:12 记 47 文件；D02 §4 记 32 文件、 broader 口径 51 文件 568 行） | F-126:12 / v3-retirement-plan.md:129（口径差注记） |
| v3 字节回归锚 | 3 处：test_sse_replay_wire.py（v3 无 id 字节锚）、test_expand_href_v4.py（v3 href 字节回归）、test_v3_rawbody_regression.py（PYTHONHASHSEED=0 子进程基线） | F-126:12 |

**机械化路径（四步，顺序敏感）**：

1. **v4-contract 自包含化**：67+13 处引用正文化——47 路由零差异行展开为 v4 字面语义、§7.5/§8.4/§14 继承段落（F-126:11）正文化、:5 继承基线条款改自包含声明。性质 = 纯文档重写，机械但量大；**B12 全部子项中唯一必须在任何 v3 面拆除（B1-B11）之前完成者**（v3-retirement-plan.md:116：「B12 必须先行——否则 v4 面失去规范锚点与测试承载」）。
2. **12 双视图文件 v3 半区改写**：断言保留、基准从「v4 ≡ v3」改为 v4 字面 golden。
3. **106 v3 单态函数三分处置**：①消费梯子类（如 test_v3_directory 26 函数——`invalid_directory_selector`/`directory_conflict`/`directory_header_retired` 对 v4 同样生效，v4-contract §5.1 逐字沿用）→ selector 值 `3`→`4` 改写后保留；②纯 v3-only 行为类（envelope X-Next-Cursor、SSE 握手预填、blanket resync、ETag wire=v3 validator 域）→ **保留至 Phase 4** 随代码拆除（它们是「v3 行为不变」的守护网，拆除前恰恰需要）；③观测维度类（wireVersion "3"、selectorResult v3）→ 随 Phase 3 收窄改写。
4. **294 处 `v=3` 字面清理**：机械替换级，但须逐处确认非 v3-specific 语义（`_expand_wire_view` 生成的 href `?v=3` vs `?v=4` 是**行为差异**非字面——messages.py:58-66 审计锚点）。

**工作量级估算**（框架引 v3-retirement-plan.md:107-116 §3.2 分组）：B12（契约+测试字面化）= 数天级纯机械工程（67+13 契约引用 + 12 文件半区 + 106 函数分类 + 294 字面 + INTERFACE_MAP 41 处）；B1-B10 src 拆除 = 400-600 行 ≈ src 26,452 行的 2%（v3-retirement-plan.md:128）。**结论：等价性解耦不是规模问题而是顺序纪律问题**——B12 先行原则（:116）在 Phase 4 排程中不可让位。

---

## 4. 五项机械准备 P1-P5 的现状映射

源清单 = v3-retirement-plan.md:137-149（§5，亲验）。逐项映射（基线 v4.5.0 工作区 + 本批计划）：

| # | 事项 | 现状（2026-08-21） | 剩余工作 |
|---|---|---|---|
| P1 | CLIENT_CHANGES.md 增补 v4 迁移章节 | **本批 Lane C 落地中**（plan:96-115 R-3：四小节 = 迁移总入口/字段差集表/per-directory 补偿/SSE 新帧消费；写作时点 git 工作区未见改动，同批并行） | Lane C 交付即完成；后续随 CHANGELOG 演进维护 |
| P2 | 消费字段差集对照表 | **并入 Lane C 小节 2**（plan:105-106，素材源 v4-contract §12.1 丢弃清单 + §13.1 形状差异） | 同上 |
| P3 | 文档漂移修正（INTERFACE_MAP 全局头「v3-only 终态/supported:[3]」+ v3-contract §2/§3 `available:[3]` 行；F-123/F-125） | **未落地**（v4.5.0 CHANGELOG [4.5.0] Docs 段仅 v4-contract §4.1/§4.3/§8.1，CHANGELOG.md:54-56 亲验） | 独立小批即可；**新方向下双重价值**：短期修正消费方误读，长期被 Phase 4 契约整份重写吸收——建议尽早做（消费方现阶段仍在读） |
| P4 | wireVersion v3 占比查询样例入 traffic-accounting.md | **未落地** | **优先级上调**：它就是 Phase 2 观测判据（§6）的手册化数据面；纯文档（维度已在 access log，v3-contract §9.1 :205），仅缺样例 |
| P5 | resync reason 值域运行时防线（F-122） | **未落地** | Phase 4 前始终有效（v3/v4 双语义维持期的结构性保险） |

**附加发现（本次亲验，非 P1-P5 原项）**：`versioning.py:41-43` 注释仍写「5.0.0 will collapse this to (4, 4) once v3 traffic retires (§0.3)」——引用已被 [4.1.0] 取消的 5.0.0，且与 §0.3 现文「不预设任何未来版本窗」相悖（注释漂移）。在新方向下该注释重新变得前瞻正确，但引用失真——列为 Phase 0 清理候选（注释级，零 wire 影响）。

---

## 5. 版本窗收窄机制

- **钉死现状**：`ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (3, 4)`（versioning.py:44，亲验）；env `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` 覆盖 = 启动 RuntimeError（fail-closed；[4.5.0] 整改实证：deploy 模板残留 `=2,2` 即 crash-loop，CHANGELOG.md:52 亲验）。**唯一变更路径 = 改常量 = wire 行为变更 = major 发版**（v4-contract §0.2 :14；AGENTS.md 版本双轨硬规则）。
- **收窄后的 wire 形态**：(3,4)→(4,4) 时无 `v` / `v=3` → 400 `{"code":"unsupported_version","supported":[4]}`——selector 拒绝路径读 `SUPPORTED_WIRE_VERSIONS` 常量（selector.py:567 判定、:625-631 `_reject_version` 响应体，亲验），端点存在不 404（v4-contract §0.1 :13）。SSE 同断（开流前 400，v3-contract §7.4 :180 亲验）。
- **major 前的阶段性收紧选项（合规枚举）**：
  - **选项 a（零 wire，推荐默认）**：纯观测——wireVersion v3 占比视图 + 告警（数据面 = P4 手册化）。
  - **选项 b（加性 minor，可选）**：发现端点软提示——`capabilities["3"]` 增加 `deprecated: true` 类可忽略字段（旧客户端忽略未知字段，零必改点；需 v3-contract §3 + v4-contract §3.1 加性小修）。给 ocdroid 迁移提供机器可读的推进信号。
  - **选项 c（不可行，明示排除）**：对 `?v=3` 返回 429/503 类软拒绝——**违反** v4-contract §0.1「`?v=3` 语义逐字节不变」的冻结承诺（:13），Phase 3 前任何此类收紧都构成未经 major 的破坏性变更。列出仅为封堵该思路。
- **Phase 3 的契约动作清单**（届时执行，本文仅预列）：versioning.py 常量改 (4,4)；v4-contract §0.3/§9.4 修订（写入实际裁决：版本号、日期、判据满足证据）；v3-contract.md 加终态状态注记（历史契约归档）；CHANGELOG major 条目（含 ocdroid 必改点：一切请求 `?v=3`→`?v=4`）；§1 分层表述的兑现点即此。

---

## 6. 分阶段路线图（每阶段准入/退出判据）

```text
Phase 0 机械准备 ──▶ Phase 1 ocdroid 迁移 ──▶ Phase 2 观测判据 ──▶ Phase 3 窗口收窄 major ──▶ Phase 4 v3 面拆除
```

| Phase | 内容 | 准入判据 | 退出判据 |
|---|---|---|---|
| **0 机械准备**（立即可启动，§8） | P3/P4/P5 落地；B12 启动（v4-contract 自包含化 → 12 双视图半区 → 106 函数三分处置）；versioning.py:41-43 注释清理；可选：capabilities["3"] deprecated 软提示（§5 选项 b，加性 minor） | 无（全部项不依赖 ocdroid） | **B12 完成**：v4-contract 自包含 + 测试 golden 化（v4 正确性不再依赖 v3 对照系） |
| **1 ocdroid 迁移**（ocdroid 仓库侧工程，本仓无代码动作） | 按 Lane C 指南执行 A1-A16（核心 A2/A3/A5/A11-A14）；生产灰度切 `?v=4` | Lane C 指南就绪（本批）；oc-webui 先例坑已文档化（§2.1） | ocdroid 全量 `?v=4` + 生产 smoke 全绿 + 直连退役 C1/C3 前置完成（CLIENT_CHANGES.md:477-479）；access log ocdroid 通道 `selectorResult==v3` 归零 |
| **2 观测判据** | 连续观察窗记录 `wireVersion` v3 占比 + SSE active v3 连接（判据原文 = v4-contract §0.3/§9.4 :15/:331：占比持续低于阈值 + SSE active 无 v3 连接；**阈值与窗长 owner 届时裁定**） | Phase 1 退出 + P4 手册化就绪 | owner 书面确认判据满足（含非 ocdroid 残留流量核查——见 §7 风险 5） |
| **3 窗口收窄 major** | §5 Phase 3 动作清单：常量 (4,4) + 契约修订 + CHANGELOG；**首个 wire 触点** | Phase 2 退出 + owner 裁决具体版本号（版本窗变更必为 major；具体号 owner 定） | 发版 + 生产部署 + `?v=3` 全量 400 验证 + 无回滚诉求观察期 |
| **4 v3 面拆除** | B1-B11 src 拆除（12 路径 400-600 行，v3-retirement-plan.md:94-105）+ v3-contract 288 行废止归档 + INTERFACE_MAP 41 处重写 + 106 函数中 v3-only 类随拆 + 294 字面清零 | Phase 3 后稳定观察期 | check.sh 全绿 + 全仓 v3 字面清零 + AGENTS.md 版本双轨规则改写 |

**关键性质**：Phase 0-2 期间 **zero wire change**（全部动作 = 文档/测试/观测），任意时点可暂停回退，与「(3,4) 冻结记录在 Phase 3 前生效」（§1）自洽。Phase 3 是单向门（major 不可回退到 (3,4) 而不再发一次 major）——故其准入判据最严格。

---

## 7. 风险表

| # | 风险 | 影响面 | 缓解 |
|---|---|---|---|
| 1 | **ocdroid 迁移延期**（最大日程风险：A2/A3/A11-A14 四核心项 + SSE 重连器为新实现） | Phase 2-4 全部后移 | Phase 0 与其完全并行；迁移指南（Lane C）+ oc-webui 实证坑文档化（§2.1）；A3 客户端补偿模式已制度化成文 |
| 2 | **双视图测试债累积** | Phase 0-3 期间 106 函数/294 字面/12 双视图的维护成本持续（>15% 断言面） | B12 尽早启动（Phase 0 内完成是理想态）；三分处置避免误删 v3 守护网（§3 步骤 3-②） |
| 3 | **Phase 3 wire 破坏面** | ocdroid 若仍有 v3 残留流量（旧构建/缓存）→ 全量 400 unsupported_version | Phase 2 判据门 + §5 选项 b 软提示提前铺垫 + unsupported_version 响应含 `supported:[4]` 自解释（selector.py:625-631）；单一部署无灰度可能 → 观察窗取长 |
| 4 | **方向-冻结窗口期的表述混乱** | 消费方读 v4-contract §0.3 见「永久双版本」、读本文件/计划见「推进废弃」——表象矛盾 | 本文件 §1 分层表述为权威口径；可选 Phase 0 动作：v4-contract §0.3 加「owner 2026-08-21 方向注记（规划性，非契约变更）」——**加性状态注记，需 owner 批准**，性质同 v3-contract 头部既有状态注记先例 |
| 5 | **匿名/残留 v3 流量不可见** | Phase 2 判据若只看 ocdroid 通道，漏计匿名消费方（access log 无消费方身份维度；直连 :14096 不经 sidecar 更不可见） | 判据设计按「sidecar 全量 wireVersion 维度」而非按消费方通道；直连侧流量由 opencode 侧观测另行覆盖（超出本仓域，判据文档须注明边界） |
| 6 | **B12 字面化期间 v3 语义意外回归** | 294 字面改写触动 3 处字节锚（PYTHONHASHSEED=0 基线等） | 字节锚文件最后动；每步 check.sh（pytest 全量）门禁（AGENTS.md 硬规则） |

---

## 8. 立即可启动项清单（不依赖 ocdroid 进度）

按依赖序（全部属 Phase 0；均为文档/测试/注释级，zero wire change）：

1. **P4：wireVersion v3 占比查询样例补入 `docs/manual/traffic-accounting.md`**——Phase 2 判据的数据面手册化（维度已在 access log，v3-contract §9.1 :205；仅缺样例，v3-retirement-plan.md:146）。**首推第一项**：它是后续一切判据的地基。
2. **P3：文档漂移修正**——INTERFACE_MAP 全局头「v3-only 终态/supported:[3]」与 v3-contract §2/§3 `available:[3]` 行改为双版本表述（v3-retirement-plan.md:145；F-123/F-125）。
3. **P5：resync reason 值域运行时防线测试**（v3-retirement-plan.md:147；F-122：route 层直发不经 `V4_RESYNC_REASONS` 门控的封闭性缺口）。
4. **versioning.py:41-43 注释清理**（§4 附加发现）：移除失真的 5.0.0 前瞻表述，零 wire 影响。
5. **B12 试点**：选 12 双视图文件之一做 v4 golden 化改造——验证流程、校准 §3 工作量估算（数天级结论的实证化）。
6. **F-126 可选预备**：v4-contract 引用的 v3 条款编号反向索引附录（F-126:26 建议；降低 B12 正文化检索成本）。
7. **（需 owner 批准）v4-contract §0.3 方向注记**：§7 风险 4 的缓解——规划性状态注记，非契约变更。

依赖关系：1-6 相互独立可并行；7 独立。**全部不触及 ACCEPTED_CLIENT_VERSIONS、selector 行为、任何 `/slimapi/**` wire 语义**——与 §1「Phase 3 前冻结记录生效」完全自洽。

---

## 9. 锚点核验声明（D-C2）

- **亲验**（v4.5.0/d1b0dcd 工作区实读）：versioning.py:35-44（常量 + :41-43 注释漂移）；selector.py:560-631（unsupported_version 判定/响应）、:636-699（消费梯子）；v4-contract.md:11-40（§0.1-§0.3/§2）、:201-240（§5/§5.2）、:313-331（§9.1-§9.4）；v3-contract.md:149-268（§5/§8.3/§10）；CHANGELOG.md:29-58（[4.5.0] 全段含 Docs 段 :54-56）、:123-152（[4.1.0] 全段含 owner 终态裁决记录 :139-140）；v3-retirement-plan.md 全文 190 行（§2 checklist :41-62、§2.2 :64-75、§2.3 :77-81、§3 B1-B12 :90-118、§4 :122-133、§5 :137-149、§6 :153-172）；F-126 全文 25 行（:9 67/13 计数、:12 12 双视图/106/294(47 文件)/3 字节锚）；plan:5/:25/:96-115/:126-130；CLIENT_CHANGES.md:477-479；git log HEAD=d1b0dcd（v4.5.0）。
- **审计锚点复用**（0b836e7=v4.4.0，未逐条重验）：sessions.py:706-717（A3 机制）、messages.py:58-66（href wire view）、CHANGELOG [3.3.1]/[4.0.0]/[4.2.0]/[4.4.0] 条目原文（经 v3-retirement-plan.md:157/:79 转引）、B1-B11 各 file:line（v3-retirement-plan.md:94-105——Phase 4 执行前须按届时 HEAD 重新对行号）。快照漂移风险：v4.4.0→v4.5.0 为审计整改批（无 selector/sessions/messages 结构性重排），预期零星 ±行级。
- **D-C3 合规**：§1 显式声明政策基线变更（owner 2026-08-21 方向）**不可推翻**且分层表述（Phase 3 前冻结记录生效）——措辞照抄计划 rev4 B4/D-2 §1 条目（plan:25/:130）。
- **D-C4 合规（无悬置项）**：全部开放问题均已落为具名决策点——Phase 2 阈值与窗长（owner 届时裁定，§6）、Phase 3 版本号（owner 裁决，§5）、软提示选项 b 与方向注记（owner 批准，§7-4/§8-7）；无「待研究」类悬置。
