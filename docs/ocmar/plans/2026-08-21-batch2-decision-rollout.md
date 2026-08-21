# oc-slimapi 批次二实施计划（裁决落地批：R-1a/R-1b/R-3/R-4/R-5/R-6）— rev4

> **For agentic workers:** 本计划由编排者调度执行：rev-cgpt 门控通过后，Lane A–D 并行派发 fixer-glm（写域互斥），完成后编排者收尾（CHANGELOG + 契约修订 + check.sh + 发版 + 本机服务更新）。步骤用 checkbox 追踪。

**Goal:** 落地 owner 2026-08-21 六项裁决：R-1a host 默认回环（本机无 TS 直连消费方）、R-1b allowlist 全局门影响评估（只评估不改码）、R-3 CLIENT_CHANGES v4 章节（现做）、R-4 `question.*` 决议族纳入 IMMEDIATE 直推（修复 webui 提问卡片不消失）、R-5 丢弃计数暴露 `/slimapi/metrics`、R-6 v3 退役全面重估（新政策方向：owner 正推进 v3 全面废弃，取代 2026-08-18「(3,4) 永久双版本」冻结）。

**Architecture:** 四条写域互斥泳道。A=SSE 代码（R-4+R-5），B=部署/运维（R-1a），C=客户端文档（R-3），D=评估文档（R-1b+R-6）。契约（v3-contract §9.2 等）与 CHANGELOG 只由编排者收尾修改；本机 systemd unit 的生产变更由编排者在发版后执行（不进泳道）。

**Tech Stack:** Python 3.11 / FastAPI / pytest（asyncio auto）。

## Global Constraints

- **基线**：commit `d1b0dcd`（v4.5.0，main HEAD，已 push）。
- **校验**：收尾阶段编排者跑 `./scripts/check.sh` 全量；各泳道只跑自己的定向 pytest。
- **fixer 禁改**：`CHANGELOG.md`、`docs/specs/**`（Lane C 的 CLIENT_CHANGES.md 例外——它是客户端对接文档非 wire 契约，明确划入 Lane C 写域）、`pyproject.toml`、git 任何操作。
- **写域白名单**：
  - Lane A：`src/oc_slimapi/sse/hub_types.py`、`src/oc_slimapi/sse/registry.py`、`tests/test_hub_behavior_lock.py`、`tests/test_metrics_dropped_events.py`（新建）、**`tests/test_hub.py`、`tests/test_global_hub_dropped_events.py`（B1：两者含 hubs[] 精确键集断言 / metrics shape unchanged 注释，R-5 加性键后必须同步更新——断言与注释一并改，注释注明「2026-08-21 R-5 裁决加性，取代 4.5.0 内部观测决定」）**
  - Lane B：`deploy/oc-slimapi.service`、`docs/operations.md`
  - Lane C：`docs/specs/CLIENT_CHANGES.md`
  - Lane D：`docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`（新建）、`docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md`（新建）
- **行为红线**：`/slimapi/metrics` 既有键**零改动**（R-5 纯加性）；IMMEDIATE 集扩张只新增成员不移除；`?v=3`/`?v=4` 一切既有响应形状不变。
- **门禁**：rev-cgpt ≥9.0 PASS 后派发。
> rev3（2026-08-21）：按 R2 评审修订——B3 六处清单扩为**11 行逐行改法**（grep 实测 operations.md 0.0.0.0 共 11 行：:24/:88/:326/:331/:456/:492/:495/:500/:502/:505/:507 中 :331 原判「保持不变」有误，改为带 opt-in 定语的事实表述；G-ACL 节 :492 标题/:495/:500-:502 图示/:505/:507 逐行规定）；NI-1 A-2 补快照独立性断言（返回对象与内部表解耦）。
> rev4（2026-08-21）：R3 PASS 9.1 后两条非阻塞修正——「十行」计数勘误为 11 行；快照独立性断言精确到嵌套字段身份 `snap1["sse"]["hubs"][0]["droppedEventsByType"] is not hub.upstream_dropped_events_total`（避免外层 snap 身份断言误写为无效检查）。
> rev2（2026-08-21）：按 rev-cgpt R1 评审修订——B1 Lane A 写域补入两个严格断言测试文件并加同步更新步骤；B2 契约锚点改 v3-contract §9 + v4-contract §9 双侧确定式加性句（§9.2 实为 snapshot 聚合矩阵，原锚点错误）；B3 Lane B 改为 operations.md 全量 0.0.0.0 站点清单（6 处）；B4 R-6 政策声明改「待转化的 owner 方向」措辞（4.1.0 冻结在正式 major 前仍生效）；N1-N4 一并落实。

---

## Lane A（fixer-glm #1）：R-4 question 决议族直推 + R-5 metrics 暴露

### Task A-1：R-4 `question.replied/rejected/v2.*` 纳入 IMMEDIATE

**Files:** `src/oc_slimapi/sse/hub_types.py`（IMMEDIATE 集，现 :80-84）、`tests/test_hub_behavior_lock.py`

**语义依据**：上游 v1.18.18 真实发布 `question.replied`/`question.rejected`（schema/v1）与 `question.v2.replied`/`question.v2.rejected`（审计 F-001 A6 复核 + F-216 的 76 型丢弃清单实证成员）；现全部 catch-all 静默丢弃——与 permission.replied 同构缺陷：`question.asked` 秒推、任何客户端答复后**其他客户端的提问卡片永不消失**（owner 确认 webui 实际痛点）。4.5.0 已修 permission 双成员，本任务补齐 question 四成员。SSE 客户端按 `data.type` 分发，新帧类型对未适配消费方为忽略型加性；契约 §7.2 未逐成员枚举 IMMEDIATE 集（F-001 复核 (b) 条结论），CHANGELOG 记录即可。

- [ ] **Step 1**：IMMEDIATE 集追加 `"question.replied", "question.rejected", "question.v2.replied", "question.v2.rejected"`（置于 `question.asked`/`question.v2.asked` 之后，保持族内聚；注释注明 R-4/2026-08-21 裁决 + 上游真名来源）。
- [ ] **Step 2：回归测试**（`tests/test_hub_behavior_lock.py`，镜像既有 `test_permission_replied_upstream_name_forwarded` 模式）：

```python
async def test_question_resolution_family_forwarded(...):
    """R-4：question.replied/rejected/v2.replied/v2.rejected 四型必须 IMMEDIATE 直推
    （真实上游名——答复后其他客户端卡片可消失）。"""
    # 逐型注入 → 断言订阅者收到原帧；并断言 hub.qp_last_activity[directory] 被刷新
    # （N1：锁 IMMEDIATE 分支 startswith("question.") 联动不漂移）；
    # 反向：注入拼写错误名（如 "question.resolved"）→ 不产生直推帧（落 catch-all 计数）。
```

- [ ] **Step 3**：核对 `qp_last_activity` 联动无需改（IMMEDIATE 分支的 q/p startswith 门自动覆盖新成员，审计 F-001 A6 第 3 点机制）——在报告中确认即可。
- [ ] **Step 4**：`.venv/bin/python -m pytest tests/test_hub_behavior_lock.py -q` 绿。

**Acceptance：** A-1-C1 四型直推断言通过；A-1-C2 拼写错误名不直推；A-1-C3 定向 pytest 绿。

### Task A-2：R-5 droppedEventsByType 暴露到 metrics

**Files:** `src/oc_slimapi/sse/registry.py`（`snapshot_metrics` hubs[] 条目，现 :340-355）、`tests/test_metrics_dropped_events.py`（新建）

**设计（冻结，评审可否决）**：hubs[] 每条目新增一个键 `"droppedEventsByType": <dict[str,int]>`——直接暴露 4.5.0 落地的 `upstream_dropped_events_total` 有界表（基数 ≤257 含 `__other__`，最坏 ~8KB payload，ops 端点可接受）。选 per-type 而非聚合总数：F-216 的核心价值是**事件集漂移检测**（新类型出现可图表化告警），聚合总数无法区分漂移与噪声；60s 采样日志保留作低成本旁证。既有键（`upstreamEventsTotal`/`emittedFramesTotal` 等）零改动。

- [ ] **Step 1**：`snapshot_metrics()` hubs 条目追加 `"droppedEventsByType": dict(hub.upstream_dropped_events_total)`（浅拷贝防并发迭代；docstring 的 "Shape (strict)" 列表同步补该键 + 基数界说明）。
- [ ] **Step 2：测试**（新建）：
  - 注入未知类型 ×N → `GET /slimapi/metrics`（沿既有 metrics 测试的 client 构造）hubs[0].droppedEventsByType 计数正确；
  - 既有键存在性断言（形状加性——旧键全在）；
  - 空表时键存在且为 `{}`（恒发布，非 optional）；
  - **快照独立性（NI-1）**：`snap1 = registry.snapshot_metrics()` 后再注入一帧未知事件（内部表变化）→ `snap1` 中已返回的 dict 内容不变；并断言嵌套字段身份 `snap1["sse"]["hubs"][0]["droppedEventsByType"] is not hub.upstream_dropped_events_total`（浅拷贝解耦——防未来改回共享引用；注意断言的是嵌套字段而非外层 snap 对象）。
- [ ] **Step 3（B1：既有严格断言迁移）**——`tests/test_hub.py:857-889`、`tests/test_hub_behavior_lock.py:1534-1565`、`tests/test_global_hub_dropped_events.py:57-68` 的 hubs[] 精确键集断言同步加 `droppedEventsByType`；「metrics shape unchanged」类注释改为「shape 加性演进：droppedEventsByType（2026-08-21 R-5 裁决，取代 4.5.0 内部-only 决定）」。
- [ ] **Step 4**：`.venv/bin/python -m pytest tests/test_metrics_dropped_events.py tests/test_hub.py tests/test_hub_behavior_lock.py tests/test_global_hub_dropped_events.py -q` + `ls tests/test_metrics*` 全部绿。

**Acceptance：** A-2-C1 per-type 计数上 wire；A-2-C2 既有键零改动；A-2-C3 恒发布（空表 `{}`）；A-2-C4 定向 pytest 绿。

---

## Lane B（fixer-glm #2）：R-1a host 回环默认

**Files:** `deploy/oc-slimapi.service:26-28`、`docs/operations.md`（E-II 姿态节）

- [ ] **Step 1**：deploy 模板 `:28` `Environment=OC_SLIMAPI_HOST=0.0.0.0` → `Environment=OC_SLIMAPI_HOST=127.0.0.1`；`:26` 注释改写：回环为默认安全姿态（E-I/E-III 经 stunnel 均可工作）；`0.0.0.0` 降级为「显式 opt-in，须自担网络层隔离（Tailscale ACL/防火墙），当前部署不使用」（owner 2026-08-21：无 TS 直连消费方）。
- [ ] **Step 2（B3 rev3：operations.md 11 行逐行改法——grep `0\.0\.0\.0` 全部命中点，一处不落）**：
  - **:24** 入口列表条目 → 「**`0.0.0.0:4097`**（opt-in，默认关闭；2026-08-21 起默认回环）：显式开启后允许通过 Tailscale 地址直接访问……」；
  - **:88** systemd 示例 → `Environment=OC_SLIMAPI_HOST=127.0.0.1   # 默认回环；0.0.0.0 为 opt-in（须自担网络层隔离，见 §11）`；
  - **:326** 日志示例 → `Uvicorn running on http://127.0.0.1:4097`（:329 既有回环对照行保持）；
  - **:331**（rev3 改法——原「保持不变」判定有误，该行裸写 0.0.0.0 过 grep 验收）→ 「启动失败常见原因：upstream 不可达、`OC_SLIMAPI_HOST` 非 loopback 且非 `0.0.0.0`（validate 白名单事实——`0.0.0.0` 为 opt-in 选项，非默认）、`OC_SLIMAPI_UPSTREAM` 非 loopback HTTP。」；
  - **:456** 拓扑表行尾追加「（opt-in 非默认；2026-08-21 起默认回环）」；
  - **:492** 节标题 → 「## 11. G-ACL 部署姿态与边界验证（历史 0.0.0.0:4097 + 14097 mTLS 隧道；2026-08-21 起默认回环，本节为 opt-in 部署 runbook）」；
  - **:495** 部署姿态声明 → 「**历史部署姿态（2026-08-20 前 steady-state）**：`0.0.0.0:4097` 明文监听 + `:14097` mTLS 隧道……；**2026-08-21 起（R-1a 裁决）默认部署为回环 `127.0.0.1`，直连入口默认关闭**，本节保留为 opt-in 部署的边界验证 runbook。」；
  - **:498（10.1 小节标题）** → 「### 10.1 opt-in 部署拓扑（历史稳态同构）」，并在 **:500 图示前**插入引用行：「> 下图为 opt-in（`0.0.0.0`）部署的历史稳态拓扑；默认部署中 sidecar 绑定 `127.0.0.1`，stunnel（:14097）转发目标 `127.0.0.1:4097` 不变。」（:500/:502 ASCII 图示本身保留为历史记录，靠前置声明归类）；
  - **:505** 「**用户接受的稳态**」→「**opt-in 部署的稳态（历史）**」；
  - **:507** 「这就是使 `0.0.0.0` 可接受的安全约束」→「这就是使 opt-in `0.0.0.0` 部署可接受的安全约束」（加 opt-in 定语）。
  config.py:767-773 validate 白名单零改动（`0.0.0.0` 仍合法可选）。
- [ ] **Step 3**：无代码改动 → 无新测试；`grep -n "0.0.0.0" deploy/ docs/operations.md` 仅剩 opt-in 说明性提及。
- [ ] **Step 4**：`.venv/bin/python -m pytest tests/test_config.py -q` 绿（回归确认 validate 语义未动）。

**Acceptance：** B-C1 模板/文档一致（默认 127.0.0.1）——`grep -n "0.0.0.0" docs/operations.md deploy/oc-slimapi.service` **每一命中行**归类为 {opt-in 声明（含 :331/:507 定语）/ 历史标注 / runbook 历史图示（:500/:502，靠 :498 前置声明覆盖）/ validate 白名单事实}之一，全文无「现行默认/用户当前接受」类表述（:24/:88/:326/:456 已改为回环默认或 opt-in 标注）；B-C2 config validate 白名单零改动；B-C3 定向 pytest 绿。

**编排者收尾专属（不进泳道）**：发版后改本机 `~/.config/systemd/user/oc-slimapi.service` 同款 env → `daemon-reload` + `restart` → 验证四件套（N3）：① `ss -tlnp | grep 4097` 仅 `127.0.0.1:4097` LISTEN 且**监听 PID == `systemctl --user show oc-slimapi -p MainPID`**（防误认旧进程）；② `curl 127.0.0.1:4097/slimapi/health?v=4` 正常；③ 经非回环地址（本机 LAN IP）curl :4097 **连接拒绝**；④ journal 无 error。

---

## Lane C（fixer-glm #3）：R-3 CLIENT_CHANGES v4 迁移章节

**Files:** `docs/specs/CLIENT_CHANGES.md`（新增顶层章节，置于文件头部 `## 模型` 之前）

**内容骨架（冻结，素材全部在库）**：
- [ ] **Step 1**：新章节 `## v4 迁移指南（wire v3 → v4，2026-08-21）`，含四小节：
  1. **迁移总入口**：整合 CHANGELOG [4.0.0]–[4.5.0] 全部消费者行动项（`?v=` selector、readiness 门控 opt-in、POST 等效动作族、providers limit 恢复、批次一的 F-001/F-137 等）为单一 checklist；素材源 = CHANGELOG 各版「消费方提示」段。
  2. **字段差集对照表**：providers `?v=3`→`?v=4` 逐字段去向表（v3 透传的 `env`/`key`/`options`/`api`/`cost`/`capabilities`/`headers`/`release_date` 在 v4 的确定性丢弃——素材源 v4-contract §12.1 丢弃清单 + 审计 D01 G5）；sessions §13.1 形状差异表（素材源 v4-contract §13.1）。
  3. **per-directory 列表的客户端补偿模式**（F-121 落地指引）：v4 无服务端 directory 过滤（§17 永久 non-goal + selector 400 `directory_retired_in_v4`）；标准模式 = `/slimapi/directories` 发现 → 全局列表分页拉取 → 按 `SessionSkeletonV4.directory` 客户端过滤；含翻页预算建议（目录数 × 每目录会话量估算 `limit` 与终止条件 `nextCursor==null`）。素材源 = v3-retirement-plan §2 checklist + F-121 finding。
  4. **SSE 新帧类型消费指引**：`question.replied/rejected`、`permission.replied`（含 v2）直推帧语义（收到即关闭对应提问/权限卡片）——R-4 落地后 webui/ocdroid 的消费模式。**兼容性依据（N2，须在文中引证）**：q/p 直推帧为原帧转发（data JSON 含上游 `type` 字段），消费方按 `data.type` 分发——本仓既有先例 = INTERFACE_MAP `/slimapi/events` 行 token 帧消费约定「客户端按 `data.type` 分发」；未适配类型按 SSE 惯例忽略（客户端分发器均为非穷尽 switch）。该依据落 C-C1 锚点清单。
- [ ] **Step 2**：章节头部标注 wire 版本双轨现状（(3,4)，ocdroid 在 v3、oc-webui 在 v4）。
- [ ] **Step 3**：无测试；`grep -n "v4 迁移指南" docs/specs/CLIENT_CHANGES.md` 命中；文件既有章节零改动。

**Acceptance：** C-C1 四小节齐备且**每小节附锚点清单表**（N4：小节 1 = CHANGELOG 各版本号列表；小节 2 = v4-contract §12.1/§13.1 具体行；小节 3 = F-121 finding + v3-retirement-plan §2；小节 4 = INTERFACE_MAP `/slimapi/events` 行 + 本批 R-4 裁决——逐项列 file:line，防 v3/v4 行为混写）；C-C2 既有章节零改动；C-C3 check.sh 路由↔文档一致性不受影响（CLIENT_CHANGES 不在路由表校验域）。

---

## Lane D（fixer-glm #4）：R-1b allowlist 影响评估 + R-6 v3 退役重估（两份分析文档）

**Files:** `docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`、`docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md`（均新建；只读全仓素材）

### Task D-1：R-1b allowlist 全局门影响评估

**素材源**：审计 F-252/F-251（02-findings/）、config.py:216-246 allowlist 语义、v3-contract §10.a/§5.7a、v4-contract §5.2。

- [ ] 内容要求（八节）：① 现状覆盖图（gate 挂载点穷举：file 三路由 + SSE 帧过滤 + v4 sessions 降级矩阵；未覆盖的 directory 消费路由清单——照抄 F-252 证据节并逐条 file:line 复核）；② 提升方案（selector 层统一前置门的实现位点候选与 403/400 码面）；③ 各消费方影响（ocdroid v3 现发什么 directory、oc-webui v4、单目录 vs 多目录部署形态）；④ 契约影响面（v3 §10.a 扩面是否破坏冻结、v4 §5.2、CHANGELOG/发版轨别）；⑤ 默认值与迁移（默认放行保持现状 vs 默认收严——破坏性评估）；⑥ 测试面估算；⑦ 与 R-1a 回环化的叠加关系（回环后威胁模型变化，全局门必要性重估）；⑧ 结论：给 owner 的 2–3 个可选路径 + 推荐。

### Task D-2：R-6 v3 退役全面重估（新政策方向）

**素材源**：`docs/audits/2026-08-20/04-final/v3-retirement-plan.md` 全文（口径 b 成本模型、B1-B12 拆除顺序、§5 五项机械准备 P1-P5、§6 三阻塞项）、审计 F-126（等价性耦合量化：106 函数/12 文件/294 字面）。

- [ ] 内容要求（八节）：① **政策基线声明（B4 措辞冻结）**：owner 2026-08-21 明示新方向「正在推进 v3 全面废弃」——本文件将其记录为**待转化的 owner 方向（规划基线）**，尚未构成 wire 契约变更：v4-contract §0.3/§9.4 与 CHANGELOG [4.1.0] 的「(3,4) 永久双版本」冻结记录**在正式 major 发版修订前仍然生效**，Phase 3（窗口收窄 major）才触发契约正式修订与 CHANGELOG 记录；本节显式引用 owner 原话语境，方向本身不可推翻、生效节奏按上述分层表述；② 硬阻塞盘点（ocdroid v3 全量锁定的迁移工程量评估框架——消费字段差集 Lane C 已产出、SSE v3 依赖面、直连退役状态）；③ 等价性测试解耦（B12 字面化：106/12/294 的机械化路径与工作量级估算）；④ 五项机械准备 P1-P5 的现状映射（P1/P2 已由本批 Lane C 落地、P3 文档漂移、P4 观测样例、P5 resync 值域防线——各自剩余工作）；⑤ 版本窗收窄机制（`ACCEPTED_CLIENT_VERSIONS` 钉死的变更路径 = major 发版；selector 400 行为的阶段性收紧选项）；⑥ 分阶段路线图（Phase 0 机械准备 → Phase 1 ocdroid 迁移 → Phase 2 观测判据（wireVersion v3 占比）→ Phase 3 窗口收窄 major → Phase 4 v3 面拆除；每阶段准入/退出判据）；⑦ 风险表（ocdroid 迁移延期、双视图测试债、wire 破坏面）；⑧ 立即可启动项清单（不依赖 ocdroid 进度的部分）。
- [ ] **Step 2**：两文档互不依赖可并行撰写；每节素材锚点必须 file:line 可溯（复用审计已有锚点，抽查复核）。

**Acceptance：** D-C1 两文档八节齐备；D-C2 全部事实断言有 file:line 锚点（审计锚点复用 + 抽样复核声明）；D-C3 D-2 §1 显式声明政策基线变更且不可推翻；D-C4 结论/路线图可执行（无「待研究」悬置项）。

---

## 收尾阶段（编排者）

- [ ] **S1**：写域复核（`git diff --stat` 对照白名单）。
- [ ] **S2 CHANGELOG `[Unreleased]`**：
  - **Added（wire，SSE 加性）**：q/p IMMEDIATE 集新增 `question.replied`/`question.rejected`/`question.v2.replied`/`question.v2.rejected` 直推（R-4/裁决 2026-08-21）——任何客户端答复 question 后其余客户端实时收到决议帧（修复 webui 提问卡片不消失：此前帧被 catch-all 静默丢弃，非 webui 消费缺陷）；未适配客户端按未知类型忽略，零迁移。
  - **Added（观测，metrics 加性）**：`/slimapi/metrics` hubs[] 新增 `droppedEventsByType`（per-type 有界丢弃计数，基数 ≤257 含 `__other__`）——事件集漂移可图表化检测（R-5）；既有键零改动。
  - **Changed（部署）**：deploy 模板 `OC_SLIMAPI_HOST` 默认 `0.0.0.0`→`127.0.0.1`（R-1a；owner 确认无 TS 直连消费方，E-II 明文面收敛；`0.0.0.0` 保留为带风险声明的显式 opt-in）。
- [ ] **S3 契约修订（B2 勘误：/slimapi/metrics hubs[] 形状的权威记载不在 §9.2——§9.2 是 snapshot 聚合矩阵）**：**v3-contract §9（观测）新增编号条目**，沿 §8 既有「[X.Y.Z] 追加」加性模式：「[4.6.0] 追加：`/slimapi/metrics` hubs[] 条目加性键 `droppedEventsByType`（per-type 有界丢弃计数，基数 ≤257 含 `__other__`；既有键零改动；空表恒发布 `{}`）」；**v4-contract §9 镜像同句**（确定式，非条件式——两契约观测节均记载）；owner 授权依据 = R-5 裁决。两契约其余内容零改动。
- [ ] **S4**：`./scripts/check.sh` 全绿。
- [ ] **S5**：`./scripts/release.sh minor`（预期 v4.6.0）+ push + **本机服务更新**（unit env `OC_SLIMAPI_HOST=127.0.0.1` + daemon-reload + restart + `ss -tlnp` 仅 127.0.0.1:4097 + health/`question` 冒烟）+ 向 owner 汇报 R-1b/R-6 两份评估的结论摘要。

## Criterion Ownership Matrix

| Criterion | 裁决 | Owner | 验证 |
|---|---|---|---|
| A-1-C1..C3 | R-4 | Lane A | 定向 pytest 绿 |
| A-2-C1..C4 | R-5 | Lane A | metrics 测试绿 + 既有键断言 |
| B-C1..C3 | R-1a | Lane B | grep + test_config 绿 |
| C-C1..C3 | R-3 | Lane C | 章节结构 + 溯源抽查 |
| D-C1..C4 | R-1b/R-6 | Lane D | 八节齐备 + 锚点抽查 |
| S1..S5 | 全部 | 编排者 | check.sh + 发版 + 本机服务实证回环 |
