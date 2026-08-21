# R-1b：directory allowlist 全局门影响评估（2026-08-21）

> 产出：批次二 Lane D（fixer-glm #4），依据 `docs/ocmar/plans/2026-08-21-batch2-decision-rollout.md` rev4 Task D-1（plan:120-124）。基线 = v4.5.0（git tag，HEAD d1b0dcd）。
> 性质：**只读分析文档**——评估「allowlist 403 门从 `/slimapi/file/**` 扩面到全局（消费集全路由）」的影响，不伴随任何代码/契约改动。owner 裁决原文（plan:5）：「R-1b allowlist 全局门影响评估（只评估不改码）」。
> 锚点规则：全部事实断言附 `file:line`。**亲验** = 本文档写作时点对当前工作区实读复核（v4.5.0，d1b0dcd）；**审计锚点** = 复用 2026-08-20 审计（快照 0b836e7 = v4.4.0）证据，未逐条重验。逐条归属见 §9 锚点核验声明。

---

## 1. 现状覆盖图（gate 挂载点穷举 + 未覆盖清单）

### 1.1 机制入口（env 三态）

`OC_SLIMAPI_DIRECTORY_ALLOWLIST` 三态解析在 `src/oc_slimapi/config.py:200-213`（`_directory_allowlist_env`，亲验）：键缺失 → `None`（机制禁用，一切行为零变化）；显式空串 → `[]`；非空 → 冒号分隔、逐项 `normalize_directory` + `normpath`。canonical 匹配语义（realpath 双边、候选实时解析不缓存、根按配置值缓存、重应用配置即失效）冻结于 `config.py:216-246` 模块注释（亲验；实现在 `config.py:252` `_ALLOWLIST_ROOTS_CACHE` 与 `config.py:268` `allowlist_roots`）。

### 1.2 gate 挂载点穷举（当前仅三处半）

| # | 挂载点 | 位置 | 语义 | 核验 |
|---|---|---|---|---|
| G1 | `/slimapi/file`、`/slimapi/file/content`、`/slimapi/file/status` 三端点 403 门 | `src/oc_slimapi/routes/read_groups.py:123-143`（`_authorized_file_directory`），调用点 :159、:171、:182 | 不在白名单子树 / 无法判定（缺 directory、相对路径、realpath 失败）→ 403 `{"code":"directory_not_allowed"}`；通过则向 upstream 转发 **canonical（realpath）directory**（TOCTOU 绑定） | 亲验（函数体 + 三调用点） |
| G2 | GlobalHub SSE 帧过滤（digest + q/p IMMEDIATE 直推） | `src/oc_slimapi/sse/global_hub.py:595-605`（`_directory_allowed`）、:608-609（丢帧 + `allowlist_dropped_events` 计数，计数器 :162）；配置热应用 :551-557（`set_directory_allowlist` + `clear_allowlist_roots_cache`） | **仅非空清单**过滤：帧 directory canonical 化后不在子树 / 无 directory / 相对 / 无法判定 → 丢帧 + 计数；显式空清单 → SSE **不过滤** | 亲验 |
| G3 | v4 全局 sessions 列表 DB 谓词 + 降级 fail-closed | DB 子树谓词 `src/oc_slimapi/dbaux/projection.py:17-21`（`s.directory = ? OR substr(...) = ?`，BINARY 前缀、弃 LIKE）；DB 不可用且 allowlist 非空 → 503 fail-closed `src/oc_slimapi/routes/sessions.py:489-491`；cursor 指纹含 `allowlist_rev` `src/oc_slimapi/dbaux/cursor.py:100-113`（:100-101、:131、:144） | v4-only：全局列表在 SQL 层过滤非白名单目录；「白名单 ⊆ 结果集」不可由上游保证时宁可 503（ora B-2 选②，sessions.py:488 注释） | 亲验（projection.py:14-30、sessions.py:485-495、cursor.py grep） |
| G3a | health 回演（只读观测，非执行门） | `src/oc_slimapi/routes/health.py:90-101` | `features.allowlist.enabled` + 可达时 `droppedEvents`（不泄露清单内容） | 审计锚点（F-252 证据节；未重验） |

### 1.3 未设门的 directory 消费路由清单（照抄 F-252 证据节，逐条复核）

以下路由均**消费/转发** directory（v3-contract §5.3 消费集，v3-contract.md:155；§10.a/§10.b 路由表 v3-contract.md:222-258），但**不经过任何 allowlist 判定**——upstream 按该 directory 自路由（WorkspaceRoutingQuery 族）或按 sid 定位后仍读对应 workdir 数据：

| 路由族 | 位置 | 核验 |
|---|---|---|
| `/slimapi/vcs`、`/vcs/status`、`/vcs/diff` | `src/oc_slimapi/routes/read_groups.py:192-221` | 亲验（转发链无 gate 调用） |
| `/slimapi/find/file` | `read_groups.py:227-241` | 亲验 |
| `/slimapi/config/providers` | `read_groups.py:395-409` | 亲验（grep 无 `_authorized_file_directory`） |
| `/slimapi/session/{sid}`（单查投影） | `read_groups.py:555-576` | 亲验 |
| messages 族（列表/full/expand/context） | `src/oc_slimapi/routes/messages.py`（全文件无 allowlist 引用） | 亲验（grep） |
| `/slimapi/session/{sid}/todo`、`/children`、`/diff` | `todo.py:72`、`children.py:78`、`diff.py:97` | 审计锚点（F-252；未逐行重验） |
| 写族 17 端点（#1-12 消费 directory） | `src/oc_slimapi/routes/write_groups.py:262-583`（v3-contract §10.b :238-258） | 审计锚点 + 亲验 grep（无 allowlist 引用） |
| token stream `/slimapi/sessions/{sid}/stream` | `src/oc_slimapi/routes/token_stream.py:88-119`（directory 仅参与冲突判定，不授权） | 审计锚点（F-252） |
| `/slimapi/questions`、`/permissions` 跨目录聚合 | 两阶段 fan-out 逐 dir `GET /question`（`docs/specs/CLIENT_CHANGES.md:92`）；`questions.py`/`permissions.py` 无 allowlist 引用 | 亲验（grep；fan-out 机制引 CLIENT_CHANGES） |

### 1.4 覆盖图之外的新发现（本次亲验补充，F-252 未列）

1. **`/slimapi/directories` 无 allowlist 过滤——与 v4-contract §5.2 声明存在实现漂移**。v4-contract.md:214（亲验）声明「allowlist 作用域全覆盖……**directories 列表**、digest/q/p 帧、事件流均过滤非白名单目录」；但 `src/oc_slimapi/routes/directories.py` 全文（202 行，亲验 grep）**零** allowlist 引用——全局发现源 `GET /experimental/session?roots=true&archived=true` 聚合（directories.py docstring，亲验）不设子树谓词。即：启用 allowlist 后，「项目切换器」仍会列出（并暴露 title/lastUpdated/session 计数等聚合信息）白名单外的 workdir。**这是契约声明与实现的实证漂移**，无论是否采纳全局门都应单独处置（修正契约措辞，或补实现）。
2. **空清单语义不对称**（审计 config-census §1 #34 已记录）：`[]` 时 file 族 reject-all（403）而 SSE 不过滤。该不对称是 §5.7a.1-2 的冻结语义（v3-contract.md:163-165），全局门设计若沿用「三态」必须显式继承或修订此点。

### 1.5 威胁定性（为什么这些缺口是缺口）

Sidecar 对 upstream 而言是**持有全目录读写的可信代理**：任何经 selector 放行的请求，upstream 都按其转发的 `X-Opencode-Directory`（或隐式 CWD）执行。allowlist 的安全意图（B4-4）= 把「经 sidecar 可触达的文件系统面」收窄到清单子树。当前仅 file 族 + SSE 帧 + v4 列表落门，意味着**同等数据面经 vcs diff（读任意 workdir 的 git 状态/diff 正文）、find/file（文件名枚举）、messages（会话正文）、providers（按目录配置）完全可达**——门只封了正门，侧门全开。F-252 结论「覆盖面不完整」的实质即此。

---

## 2. 提升方案（统一前置门的位点候选 + 码面）

### 2.1 候选 A：selector 层统一前置门（推荐位点半绪）

**位点**：`src/oc_slimapi/selector.py` 的 directory 消费梯子成功路径末端。现状链（亲验 selector.py:560-699）：admit → `_stash(wire)` → v4 method-405 → `error = self._consume_directory(scope, normalized, int(wire))`（:636 起）→ `None` 则 `await self._forward(...)`。`_consume_directory` 的 case 4（query 单值）已 `validate_directory` 并 stash 到 `V3_DIRECTORY_STATE_KEY`（selector.py:124、:406-422 `resolve_route_directory`）——**全局门插在「stash 成功后、`_forward` 前」**，对「本次请求解析出了 directory」的一切消费集路由统一判定。

**结构优势**（全部亲验锚点）：
- selector 已是**唯一** directory 消费点（消费集表 selector.py:139-190；路由经 `resolve_route_directory` 读 stash，不再自解析 query）——单点收口天然成立；
- 错误优先级链（v3-contract §8.3 :201）无需重排：门位于 ②selector 400 与 ③directory 400 **之后**、路由匹配 ④ 之前，属新增一级「directory 授权 403」；
- v4 退役路由（全局 sessions 列表）已在更早处分流（selector.py:668-673 `directory_retired_in_v4` 先于一切），互不干扰。

**设计难点（必须显式裁决的三点）**：
1. **无 directory 请求不可判定**：门只能评估「带 `?directory=` 的请求」。不带 directory 的请求（隐式 = upstream CWD）无法在 selector 层 canonical 化。若沿用 §5.7a.2 的 fail-closed（「缺 directory → 403」）会击穿合法消费形态——ocdroid 对 catalog 类端点的 directory 本就是可选（CLIENT_CHANGES.md:112-113「`directory?`（可选）」）。**建议语义**：门只对「显式 directory」判定（未携带 = 维持现状放行），把「隐式 CWD 是否收严」留为独立后续裁决。
2. **授权通过后转发 canonical directory 的扩面**：file 族现状是授权通过后向 upstream 转发 realpath 形式（read_groups.py:123-143 尾段，亲验）。若全局门沿用，启用 allowlist 的部署下**全部消费集路由**的 `X-Opencode-Directory` 都变成 realpath 值——这是启用态下的 wire 可见变化（值可能不同于原字面），需写入契约修订。
3. **实时 realpath 成本**：候选 directory 每次实时解析不缓存（config.py:216-246 冻结语义）。全局门把它从「file 三端点」扩到全部消费集请求——高 directory 流量部署有可测成本（每请求一次 `realpath`）。file 族现状已承受同成本，量级评估归 §6 测试面与压测。

**码面**：403 复用冻结错误体 `{"code":"directory_not_allowed"}`（v3-contract §5.7a.2 :164；统一体、不泄露存在性、不区分「存在但禁」与「不存在」）。**不建议**新造 400 家族：400 已被 §8.3 ③ 目录梯子三码占据（`invalid_directory_selector`/`directory_conflict`/`directory_header_retired`，v3-contract.md:201），且授权拒绝语义上不是「请求畸形」。

### 2.2 候选 B：路由层共享 helper 推广

把 `_authorized_file_directory` 从 read_groups.py 提为公共 helper，在 §1.3 清单的 13+ 调用点逐路由挂载。**不推荐**：机械散点，恰好回归 F-252 批评的「门挂在路由内、覆盖靠枚举」模式；每新增 directory 消费路由都要记得挂门（回归风险），且测试面 ×路由数（§6）。

### 2.3 候选 C：仅观测收口（最保守前置）

不动 403 面：在 access log 增加「directory 授权判定命中/未命中」维度（access log 现有 `directoryForm` 维度旁，v3-contract §9.1 :205），先量化「启用 allowlist 的部署里有多少请求携带白名单外 directory」再定门。属 §9 加性观测修订（与 R-5 丢弃计数暴露同模式——`/slimapi/metrics` 加性需 §9.2 契约小修，参照本批 Lane A 的先例路径 plan:37-71）。

---

## 3. 各消费方影响

### 3.1 ocdroid（wire v3，多 workdir 主消费方）

- **directory 发送形态**：per-workspace——sessions 列表带 `?directory=`（审计锚点：v3-retirement-plan §2.2 A3 引 sessions.py:706-717）；expand `href` 不含 directory、由客户端按需追加（CLIENT_CHANGES.md:25，亲验）；catalog 类（agent/command）directory 可选（:112-113）；目录发现走 `/slimapi/directories`（:134-138，无 query、不传 directory）。ocdroid 是「目录切换器」型多 workdir 客户端（F-121 三方一致取证，v3-retirement-plan.md:68-75）。
- **全局门影响**：清单 ⊇ ocdroid 实际 workdir 集 → 零感知（放行 + canonical 转发对客户端不可见，upstream 侧 realpath 等价）；清单缺项 → 受影响面从现状「仅 file 族 403」扩为**一切带该 directory 的读写请求 403**（含 prompt_async 等写路径）——破坏面按缺项目录的流量占比线性放大。**这是全局门最大的消费者风险点**：v3 无 per-request 降级通道（403 = 显式错误，客户端只能换目录或改部署）。
- **单目录部署形态**：allowlist 单条目下全局门增量影响集中在 vcs/find/providers/messages 读写族；file 族行为不变（已有门）。

### 3.2 oc-webui（wire v4）

- 全局 sessions 列表已被 G3 覆盖（DB 谓词 + 503 fail-closed，§1.2）；该路由 v4 下 directory 已整体退役（v4-contract §5.2 :211，亲验）。
- 其余 v4 路由 directory 消费语义 = v3 原样（v4-contract.md:212，亲验）→ 全局门对 v4 的增量影响面与 v3 相同（§1.3 清单）。v4 无额外豁免或加重。

### 3.3 匿名消费方 / 直连

直连 `:14096 → opencode :4096` 不经 sidecar，allowlist 天然不适用（CLIENT_CHANGES.md:477-479 直连边界，亲验）——allowlist 只是 sidecar 侧收窄，不是主机边界控制；这一定位影响 §7 的必要性评估。

---

## 4. 契约影响面

### 4.1 v3 §5.7a 扩面 = 修订冻结条款（不可静默）

v3-contract.md:164（§5.7a.2，亲验）明文：门只作用于 `/slimapi/file/**`，「**其余端点不 gate（本批范围；全局面过滤为 v4 §3.5 后续批次）**」。扩面到消费集 = 修改 [冻结] 节条款 → 按 AGENTS.md「契约权威」硬规则必须走正式契约修订（禁止静默偏离），且修订须同步：
- §5.7a.1-3 三态语义重述（空清单是否沿用「file 403 + SSE 不过滤」不对称，或统一为全局 reject-all——**必须显式裁决**，见 §1.4-2）；
- §8.3 优先级链插级（403 `directory_not_allowed` 位于 ③ directory 400 后、④ 404 前）；
- §10.a/§10.b 表格 directory 列加「allowlist 启用时经全局门」注记；
- canonical 转发扩面条款（§2.1 难点 2）。

### 4.2 v4 侧

v4-contract §5.2 :214（亲验）已声明「allowlist 作用域全覆盖」为目标态——**全局门在 v4 侧主要是「实现追上声明」**：除 §1.4-1 的 directories 列表漂移需处置（改契约或补实现）外，v4 契约文字基本前瞻就位。v4-contract §5.1「v3 全部消费/容忍/错误语义逐字沿用」（:203-205，亲验）意味着 v3 §5.7a 修订自动传导 v4——**修订必须双侧同步审阅**。

### 4.3 CHANGELOG / 发版轨别（两轨，交 owner 裁决）

| 轨别 | 论据 | 适用前提 |
|---|---|---|
| **minor + 加性修订**（先例轨） | 3.3.0 将整套 allowlist 以 minor 加性发布，定性依据 = 「部署配置面——env 不开启即无任何行为变化」（v3-contract.md:161 冻结表述，亲验）。全局门保持同一性质：**未配置 = 零变化**，仅显式启用新语义的部署可见新 403 | owner 认可「启用态行为演进属配置面」的 3.3.0 先例延伸 |
| **major**（严格轨） | 对「已启用 allowlist 的部署」，原本 200 的 vcs/messages/写族请求将变 403——是 v3 [冻结] 面上的 wire 行为变更；严格读法下破坏性变更走 major（AGENTS.md 发版规则） | owner 采「已启用部署的兼容性优先」 |

**本评估推荐 minor + 契约修订**（§8），理由：变更不触 selector/版本窗（ACCEPTED_CLIENT_VERSIONS 不动）；3.3.0 先例直接可引；且全局门预期默认关闭部署（§5）。CHANGELOG 须在「Fixed/Changed（行为）」明示「仅启用 allowlist 的部署可见」。

---

## 5. 默认值与迁移

- **默认必须维持 `None`（未配置 = 机制禁用）**。config.py:200-213 三态（亲验）+ §5.7a.1「未配置=零变化」是冻结语义；把默认改为 `[]`（显式空）会使**一切未配置部署的 file 族立即全 403**——直接破坏 §5.7a.1 并构成事实 major。**默认收严不可行，无分歧空间**。
- **迁移路径（若采纳全局门）**：
  1. 契约修订 + 实现 + 测试同批（§6）；
  2. `docs/operations.md` G-ACL 节（:492 起，Lane B 本批正在改写该节为 opt-in runbook，plan:84-85）补「全局门语义 + 升级注意」段；
  3. deploy 模板 env 注释同步（模板 env 集 ⊆ config.py env 集对账项，[4.5.0] release checklist 已固化，CHANGELOG.md:52 亲验）；
  4. 已启用部署的升级指引：清单若遗漏某常用 workdir，升级后该目录全路由 403——operations.md 须给出「先观测后启用」的runbook 顺序（与候选 C 衔接）。
- **建议的渐进顺序**：候选 C（观测维度）先行 → 部署侧校准清单 → 候选 A（403 门）发布。两步可拆两个 minor。

---

## 6. 测试面估算

现状基线（亲验 `grep -c "def test_"`）：`tests/test_b4_allowlist.py` 23 函数（file 门 + SSE 过滤 + health 广播 + cursor rev）；`tests/test_v3_directory.py` 26 函数（消费梯子）。

| 方案 | 新增用例估算 | 回归面 |
|---|---|---|
| 候选 A（selector 层） | selector 单元矩阵 ≈15-20（wire v3/v4 × 消费/非消费/退役路由 × {None, [], 命中, 未命中, 相对路径, realpath-symlink}）+ 每路由族 403 透传 smoke 1-2 条 ≈ **30-40 条**；§8.3 插级后 v3-contract §11.1 selector 全状态表补 403 行 | selector 梯子既有 26 函数全量回归（语义不变则应零改动通过——这是单点收口的回归优势） |
| 候选 B（路由层） | 13+ 调用点 × 5 状态 ≈ **80-120 条**，且每新增 directory 消费路由须记得挂门 | 全部路由族测试文件 |
| 候选 C（观测） | ≈5-8 条（维度存在性 + 三态） | access log 既有测试 |

附：全局门引入后 `test_b4_allowlist.py` 的 file 族 23 函数应零改动通过（file 族门被 selector 门包含/或保留双层——设计决策：**保留 file 族现门**作为路由内纵深，selector 门为前置层，两层同语义不冲突）。

---

## 7. 与 R-1a 回环化的叠加关系（威胁模型重估）

- **R-1a 现状**：本批 Lane B 落地中（plan:73-95）——deploy 模板 `OC_SLIMAPI_HOST` 默认 `0.0.0.0` → `127.0.0.1`，`0.0.0.0` 降级为显式 opt-in。R-1a 针对的实证暴露 = F-251（P1）：`deploy/oc-slimapi.service:28` 明文 `0.0.0.0:4097` LISTEN + 零认证中间件（app.py:747-755，审计锚点）。
- **回环后威胁模型变化**：网络侧攻击者必须先破 stunnel mTLS（:14097）或本机权限。allowlist 全局门的防御对象随之从「网络边界 ACL」降格为**纵深防御层**：①防持有合法客户端证书的调用方（或被攻陷的客户端）越权触达白名单外 workdir；②防客户端 bug 全盘扫荡（vcs/find 枚举面）；③多租户共享 sidecar 形态下的租户隔离。
- **必要性重估结论**：本仓实际部署形态 = 单用户（ocdroid + oc-webui）自托管，回环默认落地后全局门的**边际收益显著下降**（最大暴露面已被 R-1a 消除）；其价值主要在「opt-in `0.0.0.0` 部署」（Lane B 保留的显式选项）与未来多消费方形态。**因此不构成紧急项**——支持 §8 推荐的「先观测、后立法」节奏。若 owner 维持 `0.0.0.0` opt-in 部署长期存在，全局门优先级相应上调。

---

## 8. 结论：可选路径与推荐

| 路径 | 内容 | 契约/发版 | 适合场景 |
|---|---|---|---|
| **路径一（推荐）** | **观测先行 + 漂移修正**：①处置 §1.4-1 directories 列表契约-实现漂移（独立小批：改 v4 §5.2 措辞或补过滤实现，owner 二选一）；②按候选 C 增观测维度（§9 加性小修）；③数据面就绪后 owner 再裁决是否上候选 A | 各自 minor 级加性 | R-1a 已收最大面（§7）、无流量证据前 403 扩面破坏面不可校准（§3.1） |
| 路径二 | **selector 层全局门一步到位**（候选 A 全量）：403 `directory_not_allowed`、仅显式 directory 判定、canonical 转发扩面、§8.3 插级 | v3 §5.7a 修订（4.1 清单）+ v4 双侧同步；推荐 minor（先例轨，§4.3），CHANGELOG 明示启用态变化 | owner 判定纵深防御优先 / 存在长期 `0.0.0.0` opt-in 部署 |
| 路径三 | 路由层逐挂载（候选 B） | 同路径二 | **不推荐**：散点模式回归 F-252 批评对象，测试面 2-3×（§6） |

**推荐 = 路径一**。核心理由：①R-1a 回环默认已消除 F-251 实证的最大暴露面，全局门紧迫性下降（§7）；②当前唯一**实证**缺口是 directories 列表漂移（§1.4-1）——它是文档/小实现级修复，不需要全局门；③ocdroid 多 workdir 形态下 403 扩面的破坏面（§3.1）缺乏「白名单外 directory 真实流量占比」数据支撑，先观测（候选 C）可把路径二的决策成本降到最低。**本评估不改任何代码/契约**（R-1b 裁决原文约束，plan:5），路径选择权在 owner。

---

## 9. 锚点核验声明（D-C2）

- **亲验**（当前工作区 v4.5.0/d1b0dcd 实读）：config.py:200-279（三态/canonical/缓存）；read_groups.py:51、:123-143、:159/:171/:182、:192-241、:395-409、:555-576（grep）；global_hub.py:88/:97/:162/:551-557/:595-609；dbaux/projection.py:14-30；routes/sessions.py:485-495；dbaux/cursor.py（grep :100-101/:131/:144）；selector.py:27-124、:135-210、:560-699；routes/directories.py 全文（grep 零 allowlist）；routes/questions.py、permissions.py、messages.py、write_groups.py（grep 零 allowlist 引用）；v3-contract.md:149-268（§5/§5.7a/§8.3/§10）；v4-contract.md:11-40、:201-240（§0/§2/§5）；CLIENT_CHANGES.md:25/:92/:112-113/:134-138/:477-479；plan:5/:73-124；CHANGELOG.md [4.5.0]/[4.1.0] 段；tests 计数（test_b4_allowlist.py=23、test_v3_directory.py=26）；git HEAD=d1b0dcd。
- **审计锚点复用**（快照 0b836e7=v4.4.0，未逐条重验）：F-252 证据节中 health.py:90-101、todo.py:72、children.py:78、diff.py:97、token_stream.py:88-119、write_groups.py:262-583 行号；F-251 的 deploy/oc-slimapi.service:28、config.py:356/:498、app.py:747-755；config-census §1 #34；v3-retirement-plan §2.2 A3 引 sessions.py:706-717。快照间行号漂移风险：v4.4.0→v4.5.0 变更为审计整改批（CHANGELOG [4.5.0]，无 routes/sse 结构性重排），漂移预期为零星 ±行级。
- **无悬置项**（D-C4）：全部结论落 §8 三路径 + owner 决策点枚举（发版轨别 §4.3、空清单语义 §1.4-2、隐式 directory §2.1-1），未留「待研究」。
