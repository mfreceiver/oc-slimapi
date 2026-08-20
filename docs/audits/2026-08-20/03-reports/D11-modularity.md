# D11 — A11 模块化与可维护性审计（数据驱动）

- 快照：BASELINE_HEAD = `0b836e7`（`0b836e78c5de62d0c73b8593bf62c6650043dedf`）；全部 `file:line` 相对该快照。
- 方法：**只读、数据驱动**。全部结论基于五类可复现度量：行数/顶层符号数（inventory.json，71 个 src 文件）、内部 import 扇入/扇出（AST 解析 247 条内部边，/tmp/dep_graph.py 同逻辑脚本）、可变状态（01-explore/parts/e1-*.md 卡片）、变更频率（`git log --oneline --since=2026-08-01 --name-only` 聚合，203 commits/19 天）、缺陷史（CHANGELOG.md 0.9.0–4.4.0 Fixed/Security 全量提取 51 条）。无固定白名单；上帝文件候选完全由入围规则计算产出。
- 输入：`01-explore/inventory.json`、`01-explore/parts/e1-*.md`（19 张卡片）、`01-explore/config-census.md`、git log（只读子命令）。
- 新建发现：**F-301 … F-315（15 个）**：拆分类 F-301/302/303/304/308/313/315，耦合类 F-305/309，确认类（正向/存档）F-306/310/311/312/314，收尾欠账 F-307。
- 严重度分布：P2 × 3（F-301/F-302/F-304，均为可维护性风险）；P3 × 12（含 5 个正向确认/存档项 F-306/310/311/312/314）。无 P0/P1——本维度无可运行时缺陷。

---

## 1. 执行摘要

| 维度 | 结论 |
|---|---|
| 分层单向性（util→核心→sse/dbaux→routes→app） | **通过**：0 反向依赖、0 环（247 边 Tarjan）；2 条边界注记（F-306） |
| 上帝文件候选（数据驱动入围） | **25 个**入围（占 71 文件 35%）：拆分建议 **8 个文件**（7 项建议），保持论证 **17 个** |
| 最大风险 | `sse/tokenstream/hub.py`（2190 行，三指标全中）；`routes/messages.py`（1643 行 + 缺陷史 5 轮）；questions/permissions 复制度 0.832（F-304） |
| 耦合面 | config.Settings 71 字段扁平但消费窄（拆分可行性高，F-303）；hub_types.py 聚合健康（F-314）；`app.state` 25 键/21 文件隐式契约（F-305） |
| 热点×缺陷 | 缺陷密度与「逻辑族×外部假设数」正相关、与纯 churn 无关；proxy.py 缩容→缺陷止血是拆分效度的历史实证（F-312） |

---

## 2. 任务 1：分层违规清查（→ F-306）

### 2.1 反向依赖指定清查

```
rg -n "from oc_slimapi.routes|from ..routes|from ..app|from oc_slimapi.app" \
   src/oc_slimapi/{sse,dbaux} src/oc_slimapi/*.py   # 排除 app.py 自身
→ 0 条命中
```

根模块（非 routes/app）import routes/app：0 条。`oc_slimapi.app` 扇入 0（无人 import 组合根）。

### 2.2 全图环检测

AST 解析 71 模块全部 import（含相对 import 逐级解析）：247 条内部边，Tarjan SCC **0 个环**。分层指派（L0 util：gzip_util/logging_config/upstream/upstream_errors/errors/directory/versioning/features/envelope；L1 核心：其余根模块；L2：sse/*、dbaux/*；L3：routes/*；L4：app）逐边验证：

| 违规类别 | 计数 |
|---|---|
| 核心(L1) → sse/dbaux/routes/app | 0 |
| sse/dbaux(L2) → routes/app | 0 |
| routes(L3) → app | 0 |
| 环 | 0 |
| 边界注记 | 2（upstream.py:8→config、upstream.py:9→middleware.request_id——upstream 实为 L1 核心 HTTP 工厂，建议文档化定位而非改代码；middleware 不在五层栈定义内，现状与核心件互不反向） |

结论：分层纪律成立，为 §4 全部拆分建议提供「无循环导入阻力」的可行性背书。建议把五层规则写入架构文档并在 check.sh 加 AST 单向性断言（F-306）。

附带发现 F-307：两代兼容 shim（`sse/hub.py` 42 行、`sse/token_hub.py` 23 行）仍被 **src 主路径**消费（sse.hub 扇入 4：app/routes.events/tokenstream.hub/tokenstream.subscriber；token_hub 扇入 3：app/routes.token_stream/global_hub）——历史物理拆分只迁移了一半，tokenstream 包内部甚至反向从 shim 取符号。

---

## 3. 任务 2：上帝文件候选（数据驱动）

### 3.1 度量定义与入围规则

- 行数（inventory）；顶层符号数（inventory）；内部扇出/扇入（AST import 边）；可变状态（e1 卡片，定性+计数）；变更频率（git log since 2026-08-01，文件级聚合）。
- 入围（满足任一）：①行数>500——命中 **20 个**；②内部扇出 Top10；③变更频率 Top10（并列展开至 12）。并集 = **25 个文件**（无预设立场；候选池确实含 tokenstream/hub.py、messages.py、config.py、global_hub.py、app.py，也含计算带入的 token_stream.py/_read_passthrough.py/events.py/proxy.py）。

### 3.2 入围文件指标总表（25 个，按行数降序）

`L`=行数，`sym`=顶层符号，`fo/fi`=内部扇出/扇入，`chg`=2026-08-01 以来提交数，`测试锚`=tests/ 引用该模块的文件数。判定列：**拆**=有具体目标文件集；**保**=保持论证成立。

| # | 文件 | L | sym | fo | fi | chg | 测试锚 | 入围依据 | 判定 | 发现 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | sse/tokenstream/hub.py | 2190 | 8 | 8 | 2 | 11 | 33 | 行数+扇出+频率 | **拆** | F-301 |
| 2 | routes/messages.py | 1643 | 51 | 14 | 0 | 17 | 17 | 行数+扇出+频率 | **拆** | F-302 |
| 3 | skeleton.py | 1177 | 48 | 0 | 7 | 13 | 9 | 行数+频率 | 保 | F-310 |
| 4 | config.py | 1158 | 35 | 3 | 7 | 22 | 82 | 行数+频率 | **拆**（局部） | F-303 |
| 5 | sse/global_hub.py | 1090 | 2 | 8 | 2 | 11 | 10 | 行数+扇出+频率 | **拆**（方法级） | F-308 |
| 6 | actions.py | 975 | 38 | 1 | 2 | 2 | 2 | 行数 | 保 | §4.6 |
| 7 | routes/sessions.py | 883 | 27 | 14 | 1 | 15 | 18 | 行数+扇出+频率 | 保（至 v3 退役） | F-311 |
| 8 | sse/tokenstream/subscriber.py | 874 | 4 | 5 | 1 | 6 | 4 | 行数 | 保 | §4.6 |
| 9 | traffic.py | 845 | 14 | 0 | 17 | 11 | 20 | 行数+频率 | 保 | §4.6 |
| 10 | app.py | 785 | 15 | 24 | 0 | 27 | 14 | 行数+扇出+频率 | 保（函数级重构） | F-309 |
| 11 | singleflight.py | 770 | 16 | 0 | 3 | 2 | 7 | 行数 | 保（先例） | §4.6 |
| 12 | dbaux/lifecycle.py | 768 | 6 | 2 | 2 | 2 | 8 | 行数 | **拆**（低优先） | F-315 |
| 13 | selector.py | 739 | 42 | 5 | 17 | 7 | 37 | 行数 | 保 | §4.6 |
| 14 | access_log.py | 726 | 19 | 1 | 3 | 8 | 4 | 行数+频率 | 保 | §4.6 |
| 15 | routes/read_groups.py | 630 | 26 | 15 | 0 | 6 | 5 | 行数+扇出 | **拆** | F-313 |
| 16 | sse/replay_log.py | 598 | 20 | 0 | 6 | 3 | 3 | 行数 | 保 | §4.6 |
| 17 | routes/write_groups.py | 583 | 26 | 10 | 0 | 5 | 6 | 行数+扇出 | 保 | §4.6 |
| 18 | traffic_snapshot.py | 541 | 6 | 0 | 1 | 7 | 4 | 行数 | 保 | §4.6 |
| 19 | routes/permissions.py | 526 | 8 | 6 | 0 | 3 | 2 | 行数 | **拆**（与 #20 合并处理） | F-304 |
| 20 | routes/questions.py | 504 | 7 | 6 | 0 | 11 | 2 | 行数+频率 | **拆**（与 #19 合并处理） | F-304 |
| 21 | transform.py | 326 | 6 | 3 | 15 | 9 | 42 | 频率 | 保 | §4.6 |
| 22 | routes/token_stream.py | 324 | 4 | 8 | 0 | 5 | 4 | 扇出 | 保 | §4.6 |
| 23 | routes/_read_passthrough.py | 277 | 6 | 7 | 2 | 2 | 0（经路由间接） | 扇出 | 保 | §4.6 |
| 24 | routes/events.py | 253 | 3 | 7 | 0 | 5 | 11 | 扇出 | 保 | §4.6 |
| 25 | proxy.py | 51 | 1 | 1 | 1 | 9 | 36 | 频率 | 保（残量） | §4.6 |

**合计：拆分建议覆盖 8 个文件（7 项建议，questions+permissions 共享 1 项）；保持论证 17 个。**

### 3.3 拆分建议汇总（目标文件集 + 迁移风险 + 收益）

| 建议 | 源文件 → 目标集（行数估计） | 迁移风险（测试锚点 rg 实测） | 收益 |
|---|---|---|---|
| S1（F-301） | tokenstream/hub.py 2190 → `budgets.py` ~400 + `flush_engine.py` ~200 + `ingest.py` ~550 + `fanout.py` ~430 + hub 壳 ~350；shim 保 `TokenStreamHub` 公共面 | 低：33 测试文件/936 引用全走公共类；仓库已有同型拆分先例（sse/hub.py:1-13 自述 + token_hub→tokenstream 包化） | 五族职责独立评审；预算族（0.6.0 O1 缺陷位）可单独演进 |
| S2（F-302） | routes/messages.py 1643 → `messages/_list.py` ~600 + `_full_merge.py` ~400 + `_expand.py` ~380 + `__init__.py` re-export router | 低：17 测试文件/105 引用锚路由函数与少数 helper，shim 保 import 路径 | 三端点族解耦；缺陷史 5 轮的三族交叉定位成本消除 |
| S3（F-303） | config.py 1158 → `tokenstream/budgets.py`（TOKEN_* :46-101 + apply_debug_budget_overrides）+ `directory_allowlist.py`（:200-351 + 缓存）+ `config_validate.py`（:736-1155）；Settings 本体保持扁平 | 低：三块消费面 rg 可枚举（TOKEN_* 2 处、allowlist 3 处、validate 2+2 处）；82 测试文件的 settings 用法不动 | 变更磁铁（频率 22）分流三域；跨模块可变改写收拢属主 |
| S4（F-304） | questions.py 504 + permissions.py 526 → 共享 `routes/_aggregate_fanout.py` ~250 + 两路由各 ~120 | 低：4 测试文件锚路由函数；共享预算器获直接单测（当前仅间接覆盖） | 归一化相似度 0.832 的复制粘贴消除；修复单边遗漏风险（chg 11 vs 3 已漂移）解除 |
| S5（F-308） | global_hub.py：文件级不拆，publish() :658-977（~320 行 if/elif 八事件族）拆 per-event 处理器方法 + 分派骨架 | 极低：10 测试文件/27 引用全走 public 方法 | 事件族演进局部化（3.3.1 跨三文件修复的根因缓解） |
| S6（F-313） | read_groups.py 630 → `routes/files_vcs.py` ~270 + `routes/providers.py` ~165 + `routes/session_single.py` ~240 | 低：5 测试文件/16 引用按端点锚；INTERFACE_MAP 按端点登记零契约成本 | v4 活跃修订面（providers/session-single）与稳定透传族分离 |
| S7（F-315） | dbaux/lifecycle.py 768 → `dbaux/errors.py` ~50 + `dbaux/breaker.py` ~105 + lifecycle 留 DbAuxiliarySource ~600 | 低：8 测试文件走 public 符号；__init__ re-export 不变 | 熔断器原语可复用；低优先（低 churn 无缺陷） |

### 3.4 保持论证要点（17 个，先例参照 singleflight.py / hub_types.py）

判据通用式：**行数大 ≠ 上帝文件**。当「单一职责域 + 状态机不变式需要同屏 + 低逻辑族数 + 修改同类集中」时保持优于拆分。

- **skeleton.py（F-310 存档）**：扇出 0、可变状态 0（e1-03）、纯函数按投影对象分节、修改全为加性投影字段——认知负荷低。
- **singleflight.py（先例正面引用）**：770 行单状态机，4.1.0 B6-1 **有意**合并双实现（CHANGELOG「纯内部重构，ocdroid 零感知」）；拆开会把 `_entries/_retired/_leased_bytes` 不变式（e1-07 卡 :45：Σ reserve 不变式 docstring :64-69）从串行点撕裂到多文件——保持。
- **hub_types.py（先例正面引用，F-314）**：419 行类型+常量聚合，扇入 4 全在 sse 域、无模块可变状态——域类型聚合点健康。
- **actions.py**：975 行自成域（manifest 加载/校验/子进程执行/节流），扇入 2、频率 2、缺陷已收敛（1.3.0 一轮修完）——低风险大文件。
- **routes/sessions.py（F-311）**：v3/v4 双投影过渡态；v3 退役即自然减半，先拆=负收益。
- **subscriber.py**：已是 tokenstream 包拆分产物（队列+订阅者+注册表同生命周期）。
- **traffic.py**：845 行但「流量记账原语」单域（ledger/bucketize/stash/degraded 四件套被 17 文件共享）——共享 API 面稳定即不宜动。
- **app.py（F-309）**：组合根集中是优点（扇出 24 全仓唯一合法）；建议仅函数级 stage 化。
- **selector.py**：42 符号多但全是「请求上下文解释」（版本选择器+directory 形态）一族；扇入 17 共享面要求稳定。
- **access_log.py / traffic_snapshot.py / replay_log.py**：各为单一数据结构/IO 生命周期的同域聚合。
- **write_groups.py**：25 端点但单一 `_write_passthrough` 模式实例化，加端点=加 6 行函数。
- **transform.py / token_stream.py / _read_passthrough.py / events.py / proxy.py**：小文件（≤326 行）入围纯因扇出/频率接线属性——非面积问题，保持。

---

## 4. 任务 3：耦合度量

### 4.1 config.py Settings 扇出与拆分组可行性（→ F-303）

71 个注解字段单 dataclass（:355-735）。域分组实测：deployment/net 18、traffic/obs 11、transform 10、sse_hub 8、tokenstream 7、skeleton/expand 6、catalog 4、questions/permissions 4、actions 2、dbaux 1。

消费侧（rg `config.<field>` 全量）：**字段消费窄**——各域字段仅属主模块+app.py 接线读取（如 replay_ttl_s 仅 config+app；token_stream_debug_* 仅 config+tokenstream/hub）；唯一跨切字段 `max_response_bytes`（32 处，所有读体量上限）。⇒ Settings **本体保持扁平**（env 单命名空间是 ops 面 + 82 测试文件用法），拆分收益在文件级三块剥离（TOKEN_*/allowlist 族/validate，见 S3），非字段分组化。

### 4.2 hub_types.py 公共类型聚合点（→ F-314）

扇入 4 全 sse 域、无模块可变状态、内容为类型+协议常量+帧 helper——**不是**全局上帝接口。3.3.1 的 normalize_session_status 落位于此属正确共享（digest 与 token hub 双消费方同域）。保持；tokenstream 专属类型未来入 `tokenstream/types.py` 勿回流。

### 4.3 app.state 服务定位器（→ F-305）

全量清点（rg）：**25 键 / 21 个读取文件 / ~178 次访问**。高频四键 config(38)/upstream(30)/transforms(19)/hubs(12) 占 56%。3 个生命周期句柄键（_replay_sweep_task/_access_log_stop_event/_access_log_maintenance_task，app.py:455/:657/:684）与业务键同容器混放，仅下划线约定区分。键集无静态定义（拼写错=运行期 AttributeError→500）。

**DI 化可行性：中高**。建议 typed `AppContainer` dataclass + lifespan 构造整体挂载；SSE 后台任务本就持有 app 引用，容器化不改变生命周期；路由侧可渐进迁移（高频四键先行），无需一次性切换。

---

## 5. 任务 4：变更热点 × 缺陷关联（→ F-312）

数据：频率 Top（app 27/config 22/messages 17/sessions 15/skeleton 13/traffic·tokenstream-hub·global_hub·questions 11）× CHANGELOG 0.9.0–4.4.0 Fixed/Security 51 条归因。

| 文件 | chg | 缺陷轮次 | 关联条目 | 模式 |
|---|---|---|---|---|
| routes/questions.py | 11 | **4** | 1.1.1/1.1.3（发现机制两轮根治）/1.1.4/1.5.0-B1 | 同一逻辑修三遍——外部语义假设反复证伪 |
| proxy.py | 9 | **6**（历史） | 1.1.2 + 1.1.6×4（P0-5/P0-7/P1-10/P1-11/P1-12） | 缩容（3.0.0 catch-all 关闭→51 行）后 **0 缺陷**——面积=缺陷面的实证 |
| routes/messages.py | 17 | 5 | 0.4.0-G6/1.1.2/1.1.5/1.1.6-P1-24/1.3.1 | 三族职责交叉定位（佐证 S2） |
| SSE 三文件族 | 11/11/6 | 跨文件 1 轮 + 各自史 | 3.3.1（横跨 global_hub+hub_types+tokenstream/hub）/0.4.0/0.11.0/0.6.0/1.1.6-INV-4 | 事件归一化知识分散（佐证 S1/S5） |
| routes/sessions.py | 15 | 3 | 0.4.0/1.1.2/1.4.0 | v3/v4 双路径双份修复成本（佐证 F-311） |
| app.py / config.py | 27/22 | **0/1** | （1.1.6 批次 4 配置硬化） | 高 churn 低缺陷——组合根/配置单职责清晰 |

结论：**缺陷密度 ∝ 文件内逻辑族数 × 外部语义假设数，与纯 churn 无关**。优先级排序：S2（messages 拆分）与 S4（questions/permissions 去重）优先于 S1（tokenstream/hub 体积最大但近期修复密度低）。

---

## 6. 结论与行动清单（优先级序）

| 序 | 行动 | 依据 | 量级 |
|---|---|---|---|
| 1 | S4：questions/permissions 提取共享聚合框架 | F-304(P2) + F-312 缺陷活跃 | ~250 行新模块 |
| 2 | S2：messages.py 拆三族包 + shim | F-302(P2) | 3 新文件 |
| 3 | S3：config.py 三块剥离（budgets/allowlist/validate） | F-303(P3) | 3 新文件 |
| 4 | S5：global_hub publish() 事件分派化 | F-308(P3) | 函数级 |
| 5 | S6：read_groups.py 按资源域拆三 | F-313(P3) | 3 新文件 |
| 6 | S1：tokenstream/hub.py 五模块化 | F-301(P2，体积大但修复密度低，排后) | 5 新文件 |
| 7 | shim 退役收尾 + 分层规则入文档 + check.sh AST 断言 | F-306/F-307 | 文档+脚本 |
| 8 | AppContainer 渐进引入 | F-305(P3) | 可与 F-309 stage 化合并 |
| — | 明确不做：skeleton/sessions/selector/singleflight 等保持项 | F-310/311/§3.4 | — |

全部行动均为内部重构，wire 契约零变化（ocdroid 零感知），每项均有 shim 先例与测试锚点数据支撑迁移风险为低。
