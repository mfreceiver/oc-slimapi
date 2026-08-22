# oc-slimapi v4 wire 契约（4.0.0 实施基线 + 2026-08-19 正式修订；修订二：POST 等效动作族——已发版 v4.3.0；修订三 [2026-08-20]：providers 投影 ModelEntry 恢复 optional limit——已发版 v4.4.0；修订四 [2026-08-21]：toolcard 投影族（patch files 归一化 + tool metadata.files/diffStats + compress title + outputBytes）——已发版 v4.9.0；修订五 [2026-08-22]：P1–P6 流量优化族（messages `?since=` 前向差分 + thin 路由 ETag + digest `messagesRevision` + `/slimapi/file/raw` 二进制直读 + readiness 第 11 ID）——目标 4.11.0）

> **状态**：**4.0.0 实施基线（2026-08-18，B3a+B3b 已落地）+ 2026-08-19 正式修订（owner 裁决：修订并入 v4——v4 尚无消费方，修订无破坏影响）**。B0 批冻结全部可观察语义（2026-08-17，rev-6 PASS-with-notes + S-B01 四项 owner 终裁全收敛）；wire 终态 = 4.0.0（`ACCEPTED_CLIENT_VERSIONS` (3,3)→(3,4)；现行窗 (4,4) v4-only——见 §0.1 版本窗收窄修订）。B3a 批（阶段 A selector 双版本——历史批次命名，交付于 4.0.0–4.7.0 双版本窗 / B1 dbaux 连接生命周期 / B2 投影 SQL / B3 cursor / B4 路由分叉降级矩阵 / B5 观测）已按本契约落地并全量测试通过；B3b 批（SSE id:/重放、§7 全量、能力键 `sseReplay`/`qpImmediateFull` 广告）**已落地**。S-B01 四项（§7.0）**已全部 owner 终裁（2026-08-17）**；其余章节（含 DB 设计 R1/R2/R3/R6，已凭真库实证冻结——见 design-v4-dbaux §0.2）均为冻结语义。
> **2026-08-19 正式修订范围**（各修订节带**当前状态注记**——现行已发布行为 → 修订后冻结目标，实现批次随后落地）：providers 安全投影（§12）/ session 单查 parity（§13）/ expand 闭环（§14）/ 表示层（§15）/ method 边界与修订 non-goals（§16-§17）/ readiness 门禁（§3.3）。修订仅作用 `?v=4` 视图，`?v=3` 零改动（v3 冻结）；无新 major、无版本窗变更（`ACCEPTED_CLIENT_VERSIONS` 仍 (3,4)——该窗口前提已被 2026-08-21 版本窗收窄修订覆盖，见 §0）。设计出处：owner 终裁（2026-08-19）。**D4-A 断链考证注记（2026-08-22）**：原文引用的 `docs/ocmar/plans/2026-08-19-v4-rebaseline.md` §4-§7 路径在本仓 git 全历史中不存在（`git log --all --follow` 与对象库全文检索均无命中，无重命名前路径可考）——修订内容的存续出处 = 本文件 §12-§17 与 `design-v4-selector.md` / `design-v4-dbaux.md` / `design-v4-sse-replay.md` / `design-v4-qp-payload.md` 设计文档。
> **自包含声明（2026-08-21 修订注记，F-126 契约自包含化）**：本文件为 v4 wire 契约的**自包含权威**——`?v=4` 视图的全部可观察语义（路由全集 §10.1、消息投影 §10.2、directory 消费 §5.1、SSE 帧名帧形 §7、ETag/Vary/304 与 judge 三态 §6、expand 端点 §14、资源上限、错误映射、gzip 族、指纹、token stream 帧形等）以**本文件**为规范源。历史演进：v4 于 4.0.0 由 v3 契约「全量继承 + 本文件逐条差异覆盖」演进而来（差异层 = 新增全局 sessions 面（DB 投影源）、SSE id:/重放、directory 于全局列表退役）；本修订将原继承基线条款（未提及语义逐字取自 v3 契约）**就地正文化**（被继承条款全文转录进 §1/§2/§5.1/§6/§10.1/§10.2/§14），**不再以 v3 契约为规范源**；历史/演进注记与「与 v3 不同」对比性、状态性说明按原样保留。
> **D4-A 修订注记（2026-08-22，纯文档——语义零改动）**：兑现上条自包含声明中「SSE 帧名帧形 §7 / token stream 帧形」的规范源地位——将此前仅存于已退役 `v2-contract.md`（≤2.x 历史契约）的三类载荷定义**正文化转录**进本文件：digest 载荷字段集（→ §7.5 首条）、q/p 直推包装帧与帧名枚举（→ §7.4 首条）、token 流业务帧形（→ §7.7 新增小节）；另将 §10.1 既有 thin 路由（todo/children/diff/questions/permissions）语义自 v2-contract §2 正文化（§10.1 thin 语义块）。每处转录均附「历史演进注记」（源节号 + 语义零改动 + 本注记标识）；源文 v2 表述与 v3/v4 现文的历史差异**不在本修订内裁决**，以演进注记如实标注。顺带刷新 §7.2/§7.4 漂移代码行号锚点至现行源码（`global_hub.py:739-744` 等；`frames.py:137-151` 复核未漂移；`tokenstream/hub.py:1896-1900` 已随模块拆分迁移至 `tokenstream/ingest.py:598`）。**Q4 追加（2026-08-22 第二批转录）**：token T3 资源信封（→ §7.7 末条）、agent / command / sessions-status thin 行（→ §10.1 thin 语义块）同批正文化，纪律同上；**Q5 追加（2026-08-22）**：「忽略未知键」总则正文化进 §1。
> **版本窗收窄修订（2026-08-21，本修订实施；owner 方向指令出处：`docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md` §1）**：版本窗自 4.8.0 起收窄为 **v4-only**（`ACCEPTED_CLIENT_VERSIONS (4,4)`）。原 (3,4) 永久双版本裁决被本节覆盖——`?v=3` 请求自 4.8.0 起答复 400 `unsupported_version` `supported:[4]`。§0.3/§9.4 相应修订。
> **裁决出处**：`docs/system-architecture-proposal-2026-08-17.md`（v2.2，权威基准，行号引用）；工程细化 `docs/refactor-plans/slimapi-refactor-plan.md`；设计文档 `design-v4-selector.md` / `design-v4-dbaux.md` / `design-v4-sse-replay.md` / `design-v4-qp-payload.md`。
> **消费者**：ocdroid（B5a 探测 / B5b 适配）与 oc-webui 可**仅凭本文件**完成 v4 对接开发。

---

## §0 版本原则与并存退役规则 [冻结]

1. **版本窗（4.8.0 起 v4-only）**：4.8.0 起 `ACCEPTED_CLIENT_VERSIONS = (4, 4)`（2026-08-21 版本窗收窄修订；owner 方向指令 `docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md` §1）。`?v=4` 是唯一合法入口。`?v=3`、无 `v`、其他合法值 → 400 `unsupported_version`，`supported:[4]`（端点存在、不静默 404）。历史：4.0.0–4.7.0 曾为 (3,4) 双版本窗口；`?v=3` 在此时期语义逐字节不变。
2. **major 与 wire 协议版本绑定**（release.md §1.1 铁律；owner 2026-08-21 重申：major 只跟协议大版本走）：仅当 wire `ACCEPTED_CLIENT_VERSIONS` bump（协议大版本升级，如 4 系→5 系）时发 major。版本窗**收窄**不 bump 协议大版本（4 系窗口内 (3,4)→(4,4)），按 owner 2026-08-21 裁定发 **minor**（4.8.0 收窄即先例；历史例：(3,3)→(3,4) 窗口扩大伴随协议 3→4 bump，故 4.0.0 为 major）。
3. **v3 退役（2026-08-21 版本窗收窄修订实施）**：4.8.0 起 v3 wire 版本已退役。`?v=3` 答复 400 `unsupported_version` `supported:[4]`。原 (3,4) 永久双版本裁决（2026-08-19 owner 终态裁决，CHANGELOG [4.1.0]）被本修订覆盖——退役形式为版本窗收窄至 v4-only（4.8.0 minor），不再经观测判据评估。`?v=3` 管线在本分支（v4 分支）上不再可达。
4. **消费者回退语义**：503 族 = 显式错误，客户端**不自动回退旧版本**（维持当前 wire 版本，按 Retry-After/手动重试处理）。v4-only 窗下 `available` 不再含 3，无版本回退路径。历史（4.0.0–4.7.0 双版本期）：v3 目录级浏览仅经**用户显式触发**的整体版本重协商（`available` 含 3 时覆写 selectedWireVersion=3，全端点一致），且是**功能降级非等价回退**——v4 的跨目录 parent/archived 过滤与全局 cursor 翻页在 v3 无对应语义，UX 按功能降级建模。
5. **2026-08-19 正式修订 [冻结目标]**：owner 裁决修订直接落在 `?v=4` 视图（v4 尚无消费方，无破坏影响；无新 major、无版本窗变更——该"无版本窗变更"前提已被 2026-08-21 版本窗收窄修订覆盖）。修订语义 = §3.3 readiness 门禁（**按 feature ID 独立门控**）+ §12-§17。**契约先行冻结纪律**：各修订节描述冻结目标语义并载**当前状态注记**（现行 4.0.0/4.1.0 已发布行为 → 修订后目标）；实现批次随后落地——各修订面语义**当且仅当对应 feature ID ∈ `satisfied`**（§3.3 门控）方可达，落地前 `capabilities["4"]` 不含对应扩展键、readiness 对应 feature 不 satisfied。`?v=3` 零改动（v3 冻结）。

## §1 头与参数总则 [冻结]

- `X-Slimapi-Version` 头：3.0.0 已删除，v4 维持——出现不解读、不报错。
- `?v=` selector：sidecar **保留参数**，dispatch 层消费剥离，**永不转发上游**（消费剥离仅发生在 `/slimapi/**` 路由）。词法冻结：合法值 = `^[1-9][0-9]*$`；`0`、`03`、`+3`、` 3`、`3.0`、空串 → 词法非法。支持集 `[4]`（v4-only 版本窗，§0.1）；多值**同值**宽容折叠、多值异值 → 400。历史（4.0.0–4.7.0 双版本窗）：支持集曾为 `[3,4]`。
- 其余保留参数（`directory` 等）按 §5 消费矩阵。
- **「忽略未知键」总则（2026-08-22 Q5 owner 裁决 [冻结]）**：sidecar 拥有的载荷（`/slimapi/health`、`/slimapi/ready` 及一般 envelope）可在 **wire 版本不 bump** 的前提下**加性增补可选字段**；客户端**必须（MUST）** 忽略未知键，**不得**因出现未知键报错或拒绝载荷（发现端点 `capabilities` 的未知键容忍忽略为该原则在 §3.1 的既有落点）。lifecycle degraded 时 `/slimapi/health` 附带的 `reason` 键即依此原则增补（P3-13 收编）。反向不成立：字段**删除**或语义变更不属加性增补，须走正式契约修订。

## §2 selector 状态表（v4-only）[冻结]

设计权威：`design-v4-selector.md`（实现锚点 selector.py 全量对照）。wire 可见状态机：

| 请求形态（`/slimapi/**`） | 判定 | 行为 |
|---|---|---|
| `v=4` | v4 | v4 语义（本契约）；directory 于 §5.2 退役集 → 400 `directory_retired_in_v4` |
| `v=3` / 无 `v` / `v` 词法合法但 ∉{4} | 不支持 | 400 `{"code":"unsupported_version","supported":[4]}` |
| `v` 词法非法 / 多值不同 | 畸形 | 400 `invalid_version_selector`（词法 = §1 `^[1-9][0-9]*$`） |
| `GET /slimapi/versions` | 豁免 | 无条件豁免 selector；非 GET → 405+`Allow: GET` 优先于一切（§8.3 总链 ①） |

- **request-scope wireVersion**：selector 将本次请求 wire 视图写入 scope state；路由/health/versions 同源读此值，禁止错配组合（S-B04）。v4-only 窗下唯一可写入值 = "4"（`?v=4` 经 selector 进入）；selector-less 直调 scope 亦解析为 4（`wire_view_from_scope` 恒返 4）。历史（4.0.0–4.7.0 双版本期）：可写 "3"|"4" 两值、v4 能力只能经 selector 显式进入、测试直调缺省 = v3 视图。
- **directory 消费集版本分叉**（历史 4.0.0–4.7.0 双版本期表述；v4-only 窗下即现行消费集定义）：v4 仅将 `^/slimapi/sessions$`（全局列表）移出消费集；`/sessions/status`、`/sessions/{sid}/**`、messages、读组、写组等**全部保留** §5.1 基线消费语义（v4 无新语义的路由不动）。
- 观测：`selectorResult` 枚举增 `v4`；`wireVersion` 增 "4"（§9.1）——历史 4.0.0 增量表述；v4-only 窗下 `selectorResult` 可产出值 = `v4|rejected|exempt|not_applicable`（`v3` 不再产出）、`wireVersion` 可产出 "4"|None。

## §3 发现端点与能力面 [冻结]

### §3.1 `GET /slimapi/versions`

```
{"current": 4, "available": [4],
 "capabilities": {
   "4": {
     "globalSessions": true,      # B3a 起
     "auxiliaryFilters": true,    # B3a 起
     "sseReplay": true,           # B3b 起已广告（同批落地）
     "qpImmediateFull": true      # B3b 起已广告（同批落地；语义由 design-v4-qp-payload.md 结论冻结）
   }}}
```

- **历史载荷形态（4.0.0–4.7.0 双版本期）**：`available` 曾为 `[3, 4]`，`capabilities` 曾含 `"3"` 面（既有形状不变：envelope/directoryQuery/versionHeaderOptional/writeRoutes/readRoutes/expand）——4.8.0 版本窗收窄后随 `?v=3` 管线一并移除（§0.1）。
- `current` 恒为最新主版本（=4，S-B04；「双版本期」限定为 4.0.0–4.7.0 历史表述，v4-only 窗下仍成立且与 `available` 唯一元素同值）。
- **能力键为静态键**（v2.2 行 140/254）：存在即广告，**不随 DB 抖动**——DB 熔断/降级不改变 capabilities，瞬态可用性经 503 + health `auxiliary` 字段（§3.2）+ metrics 表达。
- **广告时序（n1 冻结）**：`sseReplay`/`qpImmediateFull` 与实现**同批启用**——B3a 的 `capabilities["4"]` **不含**此二键；B3b 实现落地同期广告（**已执行，B3b-5**：两键随 4.0.0 发布面广告；本条为时序约束的历史记录）。消费者：键缺席 = 该能力不可用，不得预依赖。
- 消费者探测（B5a；历史条款——4.0.0–4.7.0 双版本期）：`capabilities["4"]` 不存在 → 继续 v=3；未知键容忍忽略。v4-only 窗（4.8.0 起）：`capabilities` 恒仅含 `"4"` 面、`?v=3`/无 `v` → 400（§0.1），该回退分支不可达；未知键容忍忽略仍适用。
- **expand 能力探测注记（2026-08-19 补载——如实描述已发布状态；2026-08-21 版本窗收窄后注记）**：messages 的 2 条 expand 路由（§10/§14）在 `?v=4` 下**可达**（selector 放行；端点行为零版本分叉，语义全文 = §14）。历史（4.0.0–4.7.0 双版本期）：`capabilities["4"]` 静态键面不含 `expand` 键，expand 能力广告仅存在于 `capabilities["3"].expand`，客户端探测读该键、不因使用 `?v=4` 而改读他键。v4-only 窗下 `capabilities["3"]` 面已随版本窗移除，expand 探测唯一口径 = `capabilities["4"].expand`（§14 修订扩展键，iff `messages.expand.v4 ∈ satisfied` 方广告，§3.3 双向不变量）。
- **修订扩展键（2026-08-19 修订冻结目标）**：`capabilities["4"]` 随实现批次**加性扩展**两键——`readiness`（§3.3 feature 就绪度门；修订二后全集 U = **十** ID）与 `expand`（§14：categories + fragmentMaxBytes，随 `messages.expand.v4` 进入 `satisfied` 加入）。扩键前本节静态四键即 `capabilities["4"]` 全部形状；`expand` 键出现前 expand 能力探测仍读 `capabilities["3"].expand`（上文注记——历史 4.0.0–4.7.0 行为；v4-only 窗下该键不存在，探测唯一口径 = `capabilities["4"].expand`）。

### §3.2 `GET /slimapi/health`（v4-only 单视图）

- v4-only 窗下恒 v4 视图：`schema.version=4`/`server.api_version=4`（同源同值，S-B04；scope wire 解析唯一可返值 = 4，selector-less 直调亦然）。历史（4.0.0–4.7.0 双版本期）：按请求 wireVersion 返回 v3/v4 对应双视图（v3 视图双双 =3）。
- 瞬态字段（历史称「v4 视图新增」——相对 4.0.0–4.7.0 v3 视图而言）：`auxiliary: {available: bool, mode: "db"|"http"}`（v2.2 行 140；available=false 时 mode="http"；根级）；`features.allowlist: {enabled: bool}`（机制是否启用，B4-4 落地，未配置=false；只报布值**不泄露清单内容**——嵌套于 `features.allowlist`，非根级；hub registry 可达时附 `droppedEvents: <int>`（SSE 帧丢弃计数，allowlist 非空清单过滤所致）；`/slimapi/ready` 不加该块）。
- `ready` 端点形状不变。**D3 裁决注记（2026-08-22 owner，消歧）**：ready 载荷中的 `server.api_version` / `schema.version` 恒等于版本窗 server 版本——当前 (4,4) v4-only 窗下恒为 `4`，与 `/slimapi/health`（本节首条）、`/slimapi/versions`（§3.1）三端点同源同值、一致呈现；`ready` 形状仍零分叉冻结。

### §3.3 `capabilities["4"]` 扩展：readiness 就绪度门（2026-08-19 修订冻结）

> **当前状态**：4.2.0 已实施本节（`readiness` 键已广告，九项全 `satisfied`、`ready:true`）。**修订二（owner 裁决 2026-08-19，已实施——实施批次落地：U 扩为十项全集且 `session.post-actions.v4` 已入 `satisfied`）**：现行服务端按十项全集发出（`required`=`satisfied` 十 ID、`ready:true`）；三条 POST 已激活为等效路由（§16.2），§16.1 的 405 拒绝面按声明式组合优先级让位（四位组合表第三行）。键形状与规范化规则不变。

`capabilities["4"].readiness` 形状：

```ts
type ReadinessGate = {
  ready: boolean        // 派生值，见下文公式
  required: string[]    // 服务端恒发十 ID 全集（修订二后）
  satisfied: string[]   // 已就绪子集 ⊆ 十 ID 全集
}
```

**feature ID 全集 U（冻结；修订二 9→10 加性扩展；修订五 [2026-08-22] 10→11 加性扩展）**：

1. `selector.v4`
2. `session.list.global.v4`
3. `session.single.projection.v4`
4. `messages.expand.v4`
5. `providers.redacted.v4`
6. `events.global.replay.v4`
7. `events.token.replay.v4`
8. `representation.vary.v4`
9. `method.boundary.v4`
10. `session.post-actions.v4`（修订二新增，排序位第 10——置于 `method.boundary.v4` 之后，二者构成 §16.3 的组合优先级 + 依赖蕴含对：`session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied`）
11. `sessions.details.v4`（修订五新增 [P3]，排序位第 11——纯加性、无新依赖蕴含对；**retroactive 正名**：对应面（§18 批量 session 详情）已于 4.10.0 发布生效，本 ID 于 4.11.0 补入 U 并随版本 satisfied——「就绪面先于 ID 发布」属历史基线例外（与例外②同性质）：ID 发布前后该面行为一致，无过渡态差异）

- **门控模型（owner 裁决 2026-08-19：按 feature ID 独立门控，冻结）**：每个 feature ID 的修订语义**当且仅当该 ID ∈ `satisfied` 时生效可见**——某 ID 未 satisfied → 该 ID 对应的修订面语义不可达（该面维持 4.0.0 已发布 v4 行为），**不影响其他 ID**。`ready` 仅为**聚合指示器**（下款公式），**不作为全局可达闸门**——`ready:false` 只表示「十项中至少一项未就绪」，不使已 satisfied 的单项语义失效。客户端按所关心的**具体 feature ID** 查 `satisfied`（如启用 providers 投影 → 查 `providers.redacted.v4 ∈ satisfied`），不依赖 `ready` 整体值。**两条例外性说明（修订二，冻结——均非对独立门控的违反）**：① **`method.boundary.v4` 的语义定义** = 「三条 POST 组合在 `session.post-actions.v4` 未激活时的 fallback 405」——其 satisfied 态的可见行为随 post-actions 激活而让位，这是**声明式组合优先级**（§16.3 四位组合表）而非对独立门控的违反（历史基线注记：4.2.0 时 `session.post-actions.v4` 尚不存在，该 405 即 boundary 的完整语义；门控模型其余条款对两 ID 各自其余语义面照常成立）。② **第 10 项未 satisfied 时三条 POST 的行为 = 4.2.0 coded 405**（`method_not_applicable`），非「4.0.0 已发布行为」——历史基线例外：该三组合在 4.0.0/4.1.0 为框架 404（`thin_route_not_found`），4.2.0 起（`method.boundary.v4 ∈ satisfied`）为 coded 405，post-actions 激活后为等效路由（版本演进表见 §16.0 操作表；本款为独立门控条款「未 satisfied → 维持 4.0.0 已发布行为」的显式例外）。**D5 例外注记（2026-08-22 owner 裁决——href 面；门控模型「未 satisfied → 维持 4.0.0 已发布行为」条款的显式例外，同例外①②性质）**：`messages.expand.v4` 未 satisfied 时，messages 投影 `expandRefs` href 仍**恒 `?v=4`**（按解析后 selector 生成，§14）——**不折回 4.0.0 历史 `?v=3` 硬编码**：v4-only 窗下 sidecar 自产 `?v=3` href 会被 selector 层 400 `unsupported_version` 拒绝，构成死链；§14 所载「历史 4.0.0–4.7.0：href 硬编码 `?v=3`」为历史行为记录，非未就绪回落目标。
- **`session.post-actions.v4` 语义段（修订二，owner 裁决 q2，冻结）**：门控对象 = §16 修订二的三条 POST 等效动作路由（`POST /slimapi/session/{sid}`、`POST /slimapi/session/{sid}/archive`、`POST /slimapi/session/{sid}/delete`，仅 `?v=4`）。**依赖蕴含（冻结）**：`session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied`（第 10 项依赖第 9 项）——违反该蕴含的发现端点载荷 = discovery contradiction（下款条件⑦）。蕴含动机：`method.boundary.v4` 的语义 = 三条 POST 组合在 post-actions 未激活时的 **fallback 405**（声明式组合优先级，见门控模型条款例外①）；post-actions 激活（∈ satisfied）→ 三条 POST 激活为等效路由（§16.2：POST≡PATCH / POST …/delete≡DELETE / archive 便捷加性），该 fallback 405 对这三条组合不再产生；post-actions 未激活（∉ satisfied）→ 三条 POST = §16.1 coded 405（4.2.0 现行为，门控模型条款例外②）。**为何新 feature ID 而非复用 `method.boundary.v4`（冻结理由）**：该 ID 在 4.2.0 已 satisfied 且语义冻结为「三条 POST → 405」；在同一 feature 下改行为违反 per-feature 门控不变量（satisfied 语义随版本漂移），故以第 10 项加性扩展承载激活期。
- `required ≡ U`：服务端必须以全集发出（**修订二后 U = 十项；修订五 [2026-08-22] 后 U = 十一项**；§16-§17 的 non-goals 边界仍编码进该全集——cascade 编排与 cross-session search 永久缺 ID（§17 修订二）；POST 等效动作族经 `session.post-actions.v4` 进入 U（修订二）；批量详情经 `sessions.details.v4` 进入 U（修订五）；无 project-status/Turn/semantic-expand ID）。**规范化规则（比较前双方适用）**：`f(A) = 去重 → UTF-8 字节序排序`；服务端必须以规范化形式发出两数组。**未知 ID（∉ U）拒绝**——不静默忽略；服务端不得发出 U 之外的值。数组元素必须均为 `string`（出现 `null`/非字符串元素 → 按载荷矛盾处理）。
- **`ready` 判定公式（冻结；聚合指示器语义）**：`ready ⇔ f(required) ⊆ f(satisfied)`——`ready` 是派生值，服务端按公式计算并冻结输出，**不允许独立翻转**；`ready:true ⇔ f(required) ⊆ f(satisfied)` 双向等价。`ready` 的用途 = 聚合视图（「修订面全部就绪」的单布尔摘要），单项可达性以上款门控模型为准。
- **与 selector 放行/版本窗的关系（与已发布行为的边界，冻结）**：`4 ∈ available` 与 `capabilities["4"]` 存在自 4.0.0 发布即成立（版本窗事实），**不随 readiness 变化**——readiness 仅门**修订面**（§12-§17 语义，按 feature ID），不门 selector 放行：`?v=4` 的 4.0.0 已发布语义（§2 状态表、§4 全局列表既有行为等）在 readiness 任何状态下持续可用；修订面中已 satisfied 的 feature 语义同样可用，未 satisfied 的单项面维持 4.0.0 已发布 v4 行为。
- **客户端 opt-in 公式（按 feature，冻结）**：对单个 feature `F ∈ U`：`optIn(F) = localV4RevisionEnabled && (4 in available) && (F ∈ capabilities["4"].readiness.satisfied)`——三条件合取缺一不可：本地显式 feature flag（默认关）+ 服务端版本窗事实 + 该单项就绪。`current`、静态四键、`available`、`ready` 聚合值单独出现均不构成任何单项 opt-in；`current == 4` 仅为信息性（不是 opt-in 条件、不是 readiness 信号）。
- **`expand` 键双向不变量（冻结）**：`expand` 键存在 **iff** `messages.expand.v4 ∈ satisfied`（§14）。四种组合穷尽：① `expand` 存在且 `messages.expand.v4 ∈ satisfied` = 唯一合法出现态；② `expand` 缺席且 `messages.expand.v4 ∉ satisfied` = 合法（feature 未就绪的过渡态）；③ `expand` 存在而 `messages.expand.v4 ∉ satisfied` = contradiction（未就绪却广告能力）；④ `expand` 缺席而 `messages.expand.v4 ∈ satisfied` = contradiction（就绪却不广告，能力探测断链）。另：`readiness` 键缺席而 `expand` 键存在 = contradiction（expand 能力必须在 readiness 框架内广告，此态下 `satisfied` 不可评估）。`expand` 键存在而形状非法（`categories` ≠ §14 十二项有序清单 / `fragmentMaxBytes` 非 number）→ contradiction（下款）。未知额外 key 仍按「消费方忽略未知字段」（§3.1 载荷约束）处理，不误判。
- **`discovery contradiction`（单一结局，客户端侧分类——不定义新服务端错误码，发现端点载荷本身仍 200）**。适用于 **`readiness` 或 `expand` 任一键已出现**的载荷（两键均缺席的静态四键现载荷合法）。任一命中 → 一律归为单一结局：① `ready` 与规范化子集判定不一致（任一方向：`ready:false` 而 `required ⊆ satisfied`，或 `ready:true` 而不满足包含）；② `required ≠ U`（规范化后）；③ 任一数组含未知 ID（∉ U）；④ 数组非 `string[]` 或形状不可解析；⑤ `expand` 键与 `messages.expand.v4 ∈ satisfied` 任一方向不一致（含 `readiness` 缺席而 `expand` 存在）；⑥ `expand` 键存在而形状非法；⑦ `session.post-actions.v4 ∈ satisfied` 而 `method.boundary.v4 ∉ satisfied`（违反修订二依赖蕴含，语义段冻结款）。**结局**：客户端不得使用修订面语义（维持 4.0.0 已发布 v4 行为），不得从 `current` 推断 readiness/能力，按运维渠道上报。**边界（不属于 contradiction）**：`satisfied` 含某 feature ID 而对应路由行为未升级，属**实现 bug**（服务端违约，按运维渠道修复），不属本清单载荷矛盾——contradiction 仅指 `/versions` 载荷自身形状/一致性的可判定违约。**修订二过渡注记（required 9→10）**：实施批次将 `required` 扩为十项后，仅识别旧九项全集的客户端会把第 10 项 `session.post-actions.v4` 按「未知 ID（③）」判 contradiction——v4 视图现无消费方（ocdroid/WebUI 均在 `?v=3`），无实际影响；客户端应随实施批次同步十项全集。反之，扩项前的 4.2.0 服务端载荷（required = 九项）按其发布时契约合法，不按本修订追溯判矛盾。**修订五过渡注记（required 10→11）**：同理——仅识别十项全集的客户端会把第 11 项 `sessions.details.v4` 按「未知 ID（③）」判 contradiction；v4 消费方应随 4.11.0 同步十一项全集；扩项前服务端载荷（required = 十项）按其发布时契约合法，不追溯判矛盾。

## §4 `GET /slimapi/sessions`（v4 全局会话目录）[冻结]

数据源模型（v2.2 §3.1 裁决）：**DB 投影源为常态路径**（上游 SQLite `session` LEFT JOIN `project` 只读投影）；上游 HTTP `/experimental/session` = **schema 权威 + 降级路径**（等价性锚定见 §11.8）。

### §4.1 参数矩阵

```
GET /slimapi/sessions?v=4
    &archived=omit|only|all     # 三态，默认 omit
    &parent=all|none|only|<sid> # 四态；省略 = all（显式冻结，v2.2 行 65）
    &search=<title-substring>   # 标题字面子串（§4.6）
    &cursor=<opaque>            # keyset best-effort（§4.5）
    &limit=1..500               # v4 域（历史 4.0.0–4.7.0 v3 视图保持 1000）
    → 200 {items: SessionSkeletonV4[], nextCursor: string|null,
           complete: bool, degraded?: true}
```

**limit 域外归宿（2026-08-21 审计 F-025 澄清，行为零改动——冻结现状）**：`limit=501..1000`（FastAPI 声明域内、v4 域外）→ 422 coded body `{"code":"param_version_mismatch","hint":"v4 limit domain is 1..500"}`；`limit>1000 / ≤0 / 非整数`（FastAPI 声明域外）→ 框架 422 `{"detail":[...]}`（**只冻结**状态码 + `detail` 为数组存在 + 无 `code` 字段；框架文案不冻结）。`archived` 非三态值 / `parent=""` → 同 coded 422 `param_version_mismatch`。

| 参数 | v3 请求（4.0.0–4.7.0 历史行为；v4-only 窗下 `?v=3` 已 400 不达路由） | v4 请求 |
|---|---|---|
| `archived` / `parent` / `cursor` | **422**（未知参数显式拒绝，不依赖框架默认忽略） | 本表语义 |
| `roots` / `start` | 现状语义（roots 由 `parent=none` 精确承接，v2.2 行 135） | **422**（参数版本不匹配，S-B04） |
| `directory`（任何形式） | 现状消费 | **400 `directory_retired_in_v4`**（§5） |
| `search` / `limit` | 现状 | v4 语义（limit 上限 500） |

- `parent=only` 谓词 = `parent_id IS NOT NULL`（v2.2 未冻结，B0 **实证冻结**：真库 parent_id NULL 86 / NOT NULL 321 / 空串 0——无空串哨兵歧义，design-v4-dbaux §0.2 R6）。
- **SessionSkeletonV4**：既有 skeleton 列投影（`skeleton.py::SESSION_KEYS` 白名单：顶层 `id/directory/parentID/projectID/title/agent/model` + 嵌套 `time{created,updated,archived}`、`summary{additions,deletions,files}`、`revert{messageID,partID}`——已含 directory）+ `project` 对象（`{id, name, worktree}`——三列均已进 DB 投影 SELECT、schema 门与等价性 golden；join 缺行 → null）+ v4-only 字段。列名以真库实证为准（`tokens_input/tokens_output`，v2.2 行 72 模板 `tokens_in/out` 为撰写笔误——**B0 实证冻结**，design-v4-dbaux §0.2 R2）。
- **排序冻结**：`(time_updated DESC, id DESC)` 复合排序（上游 session.ts:261-299 事实同构；行号按对齐版本 v1.18.18 勘误，原引 :571-572 为撰写时行号漂移）。
- **complete**：同查询 `LIMIT :limit+1` 窗口判定（同一只读 snapshot；返回 =limit+1 行 → `complete:false`）。
- **零缓存**：一条 SQL 一次组装（v2.2 行 139）。

### §4.2 降级矩阵（72 格生成规则 + 逐格语义，B0-6d 冻结）

维度：需求态 req（12 格 = archived×parent）× DB 态（avail/disabled/tripped）× allowlist 态（empty/nonempty）× cursor 轴（正交硬闸）。生成规则（design-v4-dbaux §7 同源）：

```
result(req, db, al, cursor, search):
  db == avail            → 200；全过滤入 SQL 谓词（archived×parent×search×cursor
                            keyset×allowlist 子树谓词）；allowlist 态不影响状态码
  db ∈ {disabled,tripped}:            # 全降级上游 HTTP
    al == nonempty         → 503 auxiliary_unavailable（fail-closed，ora B-2 选②：
                              不做首N行后置过滤/循环翻页凑行——真子集风险+撕裂单快照）
    al == empty:
      search 含通配字符    → 503 auxiliary_unavailable（%/_/\ 无法等价表达，过滤语义
      (%/_/\\)               永不降级——上游原生 LIKE 通义 vs DB 字面转义）
      cursor               → 503 auxiliary_unavailable（上游单键 cursor 无法兑现
                              (t,i) keyset 指纹，行 120）
      req ∈ Class A        → 200 + degraded:true（archived∈{omit,all} × parent∈{all,none}；
                              parent=none→roots=true 透传；search 纯字面子串透传等价）
      req ∈ Class B        → 503 auxiliary_unavailable（archived=only / parent=only|<sid>，
                              上游无法表达，行 118-119）
```

- **坐标系（冻结）**：72 格 = **行为等价类** (req 12 × db 3 × al 2)；**cursor 不进坐标系**，为正交叠加轴（任何格叠加 cursor：db-avail → 仍 200 keyset 下推；db-不可用 → 503）。测试落地 = 72 等价类 × cursor 2 态 = 144 case（§11.3）。
- 逐格计数（cursor 缺席基线）：Class A = 4 req；Class B = 8 req。DB avail 24 格全 200（仅 SQL 谓词差异）；DB 不可用 × allowlist 空 24 格（4 req × 2 db 态 + 8 req × 2 db 态）= **8 格 200+degraded + 16 格 503**；DB 不可用 × allowlist 非空 24 格 = 全 503。
- **search 等价性轴（冻结）**：DB 不可用时，search 含 `%`/`_`/`\` 任一字符 → **503**（上游原生通配语义无法等价表达——过滤语义永不降级，v2.2 行 57「降级透传上游」按此收窄）；纯字面子串 → 按 Class A/B 规则（上游 `LIKE '%…%'` 对无通配字符输入与字面子串等价）。
- **`degraded:true` 语义冻结**（行 64/123）：只表数据源降级 + 排序/complete 强度弱化（排序退化上游单键 time_updated、tie-break 弱；complete 退 best-effort；cursor 翻页强度退化）；**过滤语义永不降级**——可等价表达 → 200+degraded，不可表达 → 503。allowlist 维度上「过滤语义」= 白名单 ⊆ 结果集（放行不失、禁止不漏）。
- **503 统一附 `Retry-After: 30`**（秒，与熔断恢复探针同量级）；错误体**不泄露 DB 路径/schema 细节/白名单内容**（行 122；统一体见 §8）。
- search 降级注记：上游原生 search 不做 `%`/`_` 转义（通配语义）——**v2.2 行 57「降级透传上游」按 search 等价性轴收窄**：仅纯字面子串（无 `%`/`_`/`\`）可透传（等价），含通配字符一律 503（过滤语义永不降级）。

### §4.3 错误族

| 错误 | 码 | 触发 |
|---|---|---|
| `invalid_cursor` | 400 | cursor 语法非法 / 指纹不匹配（§4.5）；**优先于** 503（§8.3） |
| `auxiliary_unavailable` | 503 | 降级矩阵（§4.2）；附 Retry-After |
| `directory_retired_in_v4` | 400 | §5.2 |
| 参数版本不匹配（code `param_version_mismatch`，coded body） | 422 | v4 收 roots/start；v3 收 archived/parent/cursor（4.0.0–4.7.0 历史行为）；**v4 limit 域外 501..1000、archived 非三态、parent 空串**（2026-08-21 审计 F-025 补全；limit>1000/≤0/非 int 为框架 422 `detail` 形状——见 §4.1 域外归宿句） |

### §4.4 ETag

v4 sessions **无 ETag/Vary/304**（v2.2 行 254 §6）；v3 全表面 ETag 原样（4.0.0–4.7.0 历史行为——v4-only 窗下 `?v=3` 已 400 不可达）。ETag validator 版本隔离：v3/v4 validator 互不匹配（v4 其他路由若产 ETag，前缀隔离）。**（2026-08-19 修订目标见 §15：v4 sessions 列表增 ETag + 全 v4 路由 `Vary: Accept-Encoding` 修正——当 `representation.vary.v4` 进入 `satisfied`（§3.3 门控）时生效；生效前本条发布态继续成立。）**

### §4.5 cursor（无状态 keyset best-effort，决策 1 定案）

- cursor = base64url(JSON `{t: time_updated, i: id, f: {archived, parent, search-hash, allowlist-rev}}`)——复合键 + 过滤上下文指纹（v2.2 行 127）。
- 承诺：确定性排序（§4.1 冻结）；**不承诺**并发更新零重复零遗漏（跨边界重见为预期行为，契约明示）。
- 指纹不匹配当前请求参数 → 400 `invalid_cursor`（提示重开首屏）。
- keyset 下推 SQL（`(time_updated, id) < (t, i)` 复合谓词）；降级路径遇 cursor 一律 503（§4.2）。
- search-hash = hash(normalized_search)（**normalized_search = trim(raw) 为四个消费点的唯一输入源**：SQL pattern 构造 / has_wildcard 判定 / 指纹 hash / **HTTP 降级上游 query**——降级路径同样传 normalized，禁 DB 查 trim 值而上游收 raw；hash 输入 = trim 后、LIKE 转义**前**的串，sha256 截断 8-16 hex）；allowlist-rev = 非空 allowlist 集合修订版本——**同一输入两次执行 hash 相同**（确定性断言，§11.6）。

### §4.6 search / allowlist SQL 语义（B0-6e 冻结，design-v4-dbaux §9 同源）

- search：输入先规范化 `normalized_search = trim(raw)`（唯一输入源，§4.5）；DB 路径 `title LIKE :pattern ESCAPE '\'`，pattern = `%` + LIKE 字面转义（normalized_search）+ `%`——**字面子串匹配**；规范化 hash 进指纹。**降级（DB 不可用）**：has_wildcard(normalized_search) 含 `%`/`_`/`\` 任一字符 → 503（不可等价表达，§4.2）；纯字面子串 → 上游透传等价（**传 normalized_search**，上游对无通配字符输入同字面子串）。
- allowlist 子树谓词（非空时，**二进制前缀，弃 LIKE**——实测 SQLite LIKE 对 ASCII 大小写不敏感、`=` 二进制敏感，LIKE 通配规则不可用于安全边界）：
  ```sql
  (s.directory = :d_raw
   OR substr(s.directory, 1, :prefix_len) = :prefix)
  -- :prefix = :d_raw || '/'（独立绑定，不做 LIKE 转义）；:prefix_len = length(:d_raw)+1
  ```
  `/foo` 匹配 `/foo` 与 `/foo/**`，**不含** `/foobar`、**大小写敏感**（`/Foo` 不匹配）；**根目录特例**：allowlist 项 = `/` → 匹配所有非空绝对路径 directory（单独定义，不与 `//` 前缀混算）。比较 = 存储值 vs 规范化（absolute 非 realpath，与上游 `directoryColumn` 一致）。空 directory 行在 allowlist 非空查询中排除（§4.1 既有语义）。
- legacy 空 directory：空串按字面空串参与谓词（复刻上游 `database/path.ts:43-59` 空"value 保留"语义）；allowlist 非空时空 directory 行天然排除。
- `/slimapi/directories` 保持现形态（/experimental/session 发现 + allowlist 过滤叠加），**不**升 DB 投影（范围冻结，v2.2 行 145/183）。

## §5 directory 消费矩阵 [冻结]

### §5.1 基线 directory 消费语义（2026-08-21 正文化转录 [冻结]；`?v=3` 视图全量适用——4.0.0–4.7.0 历史表述，v4-only 窗下 `?v=3` 已 400；v4 视图经 §5.2 对非退役路由引用同一语义）

> 历史演进注记：本节语义 4.0.0 起原以「v3 契约 §5 逐字引用」的继承基线条款承载；F-126 自包含化后就地正文化，语义零改动。

1. **canonical：`?directory=` query**（单值）；消费集内由 sidecar 消费并转上游 `X-Opencode-Directory` 头（wire 等价）。
2. **消费剥离规则**：`v` 在 `/slimapi/**` 路由上**无条件消费剥离**（任何视图均剥离、永不转发上游，§1）；`directory` 的消费/转换**仅限消费集内**。
3. **消费集**：`messages/{sid}`（含 **expand 两路由，§14——同样消费 `?directory=`**）、`sessions`（列表+status）、`todo`/`children`/`diff`、`agent`/`command`、**§10.1 全部收编路由（按各自 directory 列——以上游组声明为准：file=FileQuery、file/status=WorkspaceRoutingQuery、vcs=WorkspaceRoutingQuery、find=FindFileQuery、providers=WorkspaceRoutingQuery、session 单查=WorkspaceRoutingQuery 等）**。
4. **多值规则**：`?directory=` **多值异值** → 400 `{"code":"invalid_directory_selector"}`。
5. **双现/头退役规则（仅消费集）**：query 与 `X-Opencode-Directory` 头同时出现——归一化后同值 → 正常；不同值 → 400 `{"code":"directory_conflict","queryDirectory":<str>,"headerDirectory":<str>}`；**消费集内 directory 头出现（头退役终态）** → 400 `directory_header_retired`（提示改用 `?directory=`）。
6. **stream 例外**：query-only directory 接受（no-op，不报错）；query 与头同时存在且归一化后不同值 → 400 `directory_not_allowed`；多值异值前置 400 `invalid_directory_selector`（消费集统一规则），单值化后按前述规则判定。
7. **不在消费集（宽容忽略）**：`questions`/`permissions`（跨目录自发现聚合）、`events`、`health`/`versions`/`ready`/`metrics`/`actions`/`directories`。

### §5.2 v4

| 路由 | v4 × directory |
|---|---|
| `GET /slimapi/sessions`（全局列表） | **整体退役**：query（单值/多值）、header（任何形式）、query+header 混合 → 一律 **400 `directory_retired_in_v4`**（selector 层拦截，先于路由；不泄露目录存在性） |
| `/slimapi/sessions/status`、`/sessions/{sid}/todo|children|diff|stream`、messages×5、agent、command、读组×10（**修订五 [P5]：读组×11**——`/slimapi/file/raw` 入组，4.11.0，§19）、写组×5 | **§5.1 基线消费语义原样**（query 单值消费剥离；header 退役 400；多值 400） |

- **allowlist 作用域全覆盖**（v2.2 行 186，B4-4 落地）：非空时全局 sessions 列表（DB SQL 谓词）、directories 列表、digest/q/p 帧、事件流均过滤非白名单目录；`/slimapi/file/**` fail-closed（空 → 403 `directory_not_allowed`）。allowlist 三态（未配置/显式空/非空）语义见 B4-4（P1 3.3.0 起）。
- **修订二加注（`session.post-actions.v4 ∈ satisfied` 时生效，§16）**：三条 POST 等效动作路由的 directory 消费 ≡ 各自等效目标路由——`POST /slimapi/session/{sid}` 与 PATCH 同路径（消费集既有行，query 单值消费剥离 → header；多值/header-only 违约梯子 §5.1 适用）；`POST …/archive`、`POST …/delete` 的等效上游目标为消费集路径 `/session/{sid}`（上游写端点声明 WorkspaceRoutingQuery），同样按「query 单值消费剥离 → header、§5.1 违约梯子」消费，v4 无 retirement 差异（非全局列表路由）。门控未激活（过渡态）期间此三组合在 selector 层 405 先行（§8.3 插列），不触达消费判定。

## §6 ETag / Vary / 304 [冻结]

> 历史演进注记：本节基线语义 4.0.0 起原整节指回 v3 契约 §6；F-126 自包含化后就地正文化，语义零改动。§4.4 已含 v4 差异（发布态 v4 sessions 列表无 ETag，修订目标见 §15）。

1. **validator 规则**：按实际 coding 派生——identity → 强 validator `"<sha256hex>"`；gzip → 弱 validator `W/"<sha256hex>"`（canonical 输入恒为 identity body 字节；hash 输入 = `REP_VERSION + NUL + coding + NUL + canonical identity body`，全量 hex 不截断——§12.6 同口径）。`REP_VERSION` 输入含 wire 版本标记 + 投影/配置指纹——**跨视图 validator 互不匹配**（交叉 `If-None-Match` → 保守 200）；投影/配置变化 → 全部 validator 轮换 → 构造上不可能误 304。
2. **Vary**：全部 `/slimapi` ETag 路由恒单值 `Vary: Accept-Encoding`（directory-消费与不消费路由一致；`X-Opencode-Directory` 头维度已随 §1 头退役移除，不出现在任何 Vary 值中）。`?v=`/`?directory=` 属 URI 维度不加 Vary。
3. **`If-None-Match`/`*`/judge 三态（冻结）**：RFC 9110 **弱比较**（忽略 `W/` 前缀按 opaque tag 比较）+ `*`。三态结局：① 无 `If-None-Match` / 无命中 → **200**（正常管线）；② `If-None-Match: *`（资源存在）→ **304**（回显实际将服务的 coding 的 validator——benefit-gated 路由须真实压缩一次以确定 coding，回显不误标）；③ 弱比较命中 validator → **304**、精确回显该 validator（零压缩达判定）。保守 200 边界：请求可接受 gzip 且 body 过 gzip 受益门时，服务 coding 不可静态判定——identity 强 validator 命中亦**保守 200**（绝不冒 304 回显错 coding 的风险）；反之 identity-only 请求携带 gzip validator 不匹配 → 200。**管线照常执行不短路**——上游 GET/投影/admission 全部照跑，命中仅省**下行传输体**（非缓存授权）。
4. **ETag 路由全集**：既有 envelope/catalog 四端点（`sessions` 列表、`messages/{sid}` 列表（`mode=merged` 同样适用——以最终 splice 后 body 为准）、`agent`、`command`）+ §10.1 收编读组全部 GET 路由（file/vcs/find/providers/session 单查/active/global health/context）；todo/children/diff thin 路由**[修订五] 已入全集**（4.11.0 起参与 §6 全规则 304 判定——唯一行为变更 = 携带命中 validator 的 `If-None-Match` 重放可能 **304**（历史 ≤4.10.x：不在全集、`If-None-Match` 忽略、恒 200）；头集/Vary/`Cache-Control: no-store` 语义不变，validator 规则同 §6.1 全量口径）；**expand 两路由（§14）不在全集**（成功恒 200、无 304/`If-None-Match` 判定）；§10.1 写路由不启用。envelope 路由 canonical 输入 = envelope body。上游自身 ETag 头不透传（sidecar 生成域）。
5. **304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`（sessions/messages envelope 自含 `nextCursor`/`complete`，304 不复制分页信息）。
6. **边界**：4xx/5xx 不带 `ETag`、不参与 304；`OC_SLIMAPI_ETAG_ENABLED=false` → 不输出 `ETag`、不判 304（`Vary` 仍发——表示可变性与 ETag 正交，§15）。

## §7 SSE id: / 重放（v4-only）[四项已全部 owner 终裁，2026-08-17]

> 设计权威：`design-v4-sse-replay.md`（协议矩阵用例表 + 状态机全文）。本节为 wire 可见语义，**已随 B3b 批落地（B3b-2，全量测试通过）**。**v3 SSE 帧名帧形零变化**（v2.2 行 153 冻结）——id:/重放仅 v4 生效；v3 客户端无感知（历史 4.0.0–4.7.0 表述——v4-only 窗下 `?v=3` 已 400，无 v3 SSE 客户端）。能力键 `sseReplay`/`qpImmediateFull` 已随 B3b 落地并广告（§3.1）。

### §7.0 四项协议裁决记录（S-B01，B0 出门 gate）

| # | 议题 | 状态 |
|---|---|---|
| ① | tokens=1 统一流 | **已裁决（owner，2026-08-17）**：v4 禁止复用——`/events?tokens=1` → **400**，token 流必须走独立 `/slimapi/sessions/{sid}/stream`。理由：单 Last-Event-ID 无法恢复双序列（meta-first 与重放顺序结构性矛盾：重连新 meta 分配新 seq 后发旧 replay 帧 = 线上 ID 倒退）；webui/ocdroid 本就分离两连接，成本最低 |
| ② | meta 重连语义 | **已裁决（owner，2026-08-17）——按 design-v4-sse-replay §2.2 提案原文冻结**：meta 帧**不带 `id:`**（连接级协商帧，不参与序列）；epoch **不随重连更换**（仅进程重启换——重连同进程内历史帧与日志窗口仍有效，换 epoch 会浪费窗口内可补帧）；线序严格 = meta（无 ID）→ replay 帧 → 新帧，全程 `(epoch,seq)` 单调不减 |
| ③ | token ID 作用域 | **已裁决（owner，2026-08-17）——按 design-v4-sse-replay §2.3 提案原文冻结**：**token 流 = per-sid** 独立序列（端点天然绑定 sid，域键 = sid）；**全局流 `/events` = 该全局输出流自身的单一序列**（全实例策展帧共序——`/events` 是单连接全实例流，无 directory 绑定，per-directory 域会产生跨 directory 重复 ID / 单连接 seq 不单调 / Last-Event-ID 无域信息不可恢复，不可实现）。否决全局跨端点统一序列（token 流独立端点独立域）与每连接序列（重连后 Last-Event-ID 失效）；单一/per-sid 域下 seq 空洞唯一来源 = 日志逐出 → gap 判定干净 |
| ④ | 两端点逐帧状态机 | **已裁决（owner，2026-08-17）——按 design-v4-sse-replay §2.4 提案原文冻结（含上游断连 barrier 机制、REPLAY-001~018 协议矩阵）**：CONNECTING → ESTABLISHED/REPLAYING/RESYNCED 转移逐帧表（8 场景 × 2 端点），4 条通用不变量：meta 恒首帧；带 `id:` 帧按 (epoch,seq) 严格单调不减；无 `id:` 帧（meta/resync/heartbeat）不参与序列；replay 序列内不插新帧 |

（②③④完整论证与状态机表格见 `design-v4-sse-replay.md` §2/§3——owner 2026-08-17 终裁以该文档 §2.2-2.4 提案原文为冻结基线；本节为 wire 摘要。resync reason 值域**冻结** = `epoch_changed` / `replay_expired` / `replay_gap` / `reconnect_no_replay`。）

### §7.1 id: 语法与序列（已裁决，owner 2026-08-17）

- ID 语法（域标签编入，冻结）：`id: g:<epoch>:<seq>`（全局流 `/events`）；`id: t:<sid>:<epoch>:<seq>`（token 流 `/sessions/{sid}/stream`）。epoch = 进程代（**随机 boot nonce 16 hex，非墙钟、无序、不比较大小**——墙钟可回拨/碰撞；**重启必换**、不随 SSE 重连更换），seq = 单调递增。
- **ID 域独立 + 机械判定**：全局流 = 全实例策展帧**单一序列**；token 流 = per-sid 独立序列（域键 = sid 编入 ID）。服务端按域标签判定：前缀非 `g`/`t` → 格式非法；`g:` 到 `/stream` 或 `t:` 到 `/events` → 跨端点域；`t:<sid>` 与路径 sid 不符 → 跨 sid 域——三者一律忽略 + 重置（按首连；跨域混用属客户端协议违约，不报错不 resync）。
- 帧分类：业务帧 / digest 分配 id；meta / resync / heartbeat **无 id**（不参与序列）。
- **ID 无倒退不变式**：任一连接上线后带 `id:` 帧严格单调不减（§7.0② 线序保障）。

### §7.2 重放语义（已裁决，owner 2026-08-17）

- 有界重放日志（新组件，count/bytes/TTL 三维上限，环形覆盖）——现 GlobalHub pending（250ms debounce）与 tombstone 队列**不是** replay log；与既有 token 域重放队列（cap 1000/TTL 24h）并存不混用。
- `Last-Event-ID` 重连：缺口在日志窗口内 → 补发 replay 帧；ID 过期（早于窗口）→ 发 resync 提示帧（客户端全量对齐）；**epoch 归类（冻结，四类拆分）**：旧 epoch（格式合法、epoch ≠ 当前——随机 nonce 无序不比较大小，即进程重启前世界）→ `resync{reason:"epoch_changed"}`；future（同 epoch 且 seq > 已发布 max）→ 忽略 + 重置（按首连）；格式非法 / 跨端点域 / 跨 sid 域 → 忽略 + 重置。
- **Last-Event-ID 分类优先级（冻结，严格短路序）**：①完整语法校验（域标签 + epoch 16hex + seq 十进制）→ ②端点标签与路径 sid 校验（`g:` 只属 `/events`；`t:` 只属 `/stream` 且 sid 匹配路径）→ ③epoch 比对（仅对通过 ② 的正确域 ID）→ ④seq/窗口比对（仅同 epoch）。组合输入按最先命中者短路（如 `t:<sid>:<旧epoch>:5` 到 `/events` = 跨端点域 → 忽略重置，不触发 epoch_changed）。
- **上游断连恢复（触发条件冻结）**：**首次确认上游 loss 即触发**（EOF/异常路径为主，`_upstream_loss_notified` 防重；成功重连仅作未通知时兜底——现行为延续，`global_hub.py:1091-1096/1135-1137/1149-1155`）→ 对全部存量订阅者 fanout `resync{reason:"reconnect_no_replay"}`（无 id）→ 恢复后新帧（seq 继续单调不重置；**epoch 不变**）；token 域另清空该 sid pending live 缓冲（`tokenstream/ingest.py:598` `on_upstream_reconnect` 锚点；原 `tokenstream/hub.py:1896-1900` 已随 tokenstream 模块拆分迁移——D4-A 行号刷新）。**持久 barrier（S-B01④已裁决冻结，owner 2026-08-17；low-watermark 数据结构为实现细节）**：上游 loss 时写 low-watermark barrier（水位 = 该域已发布 max seq）——**写入范围 = 全局域 + 当前 epoch 内全部已创建 per-sid 域（不限在线订阅者）**；后续任何 `Last-Event-ID` seq **≤** barrier 水位的重连（含断连期间离线的客户端）一律 `resync{reconnect_no_replay}`（水位本身对应的帧亦发布于缺口前），seq > 水位 → 走完整第④级分类（**future**（同 epoch 且 seq > 已发布 max）→ 忽略 + 重置按首连；否则窗口内 replay / `replay_expired` / `replay_gap`，§7.2 上文分类）；**禁止跨 barrier 补帧**（barrier 前后存在 sidecar 未观察到的上游事件缺口，窗口内连续不构成补帧依据）。barrier 不受 count/bytes/TTL 逐出（仅窗口下界严格越过后可删；域回收保留失效水位或 fail-safe resync；进程重启归 `epoch_changed` 拦截）；客户端 HTTP 全量对齐。
- gap 处理：区分「日志逐出」（→ resync）vs 合法缺席（单一/per-sid 域下不存在跨域合法空洞）。**snapshot 不是服务端帧**——resync 后客户端自行 HTTP 全量对齐（全局域如 `/slimapi/sessions` 首屏、token 域重拉消息投影），服务端只发 meta → resync → 新帧。逐出-发布并发的边界 gap 误判风险为实现期待验证项（design-v4-sse-replay.md §5 待裁决 5，可降级防御分支，不影响 wire 语义）。
- 背压：溢出帧**入**重放日志（日志记录「已发布帧」而非「已送达帧」）；订阅端溢出断连 → 重连走 Last-Event-ID 重放。
- **resync 帧 reason 值域（v4 冻结，加性扩展）**：`epoch_changed` | `replay_expired` | `replay_gap` | `reconnect_no_replay`（既有）；token 流 tombstone（消息已撤销）在 replay 时**照常消耗其 seq 并以 `message.removed` 轻量撤销帧回放**（既有帧形 `tokenstream/frames.py:137-151` = `event: message.removed` + `{sessionID, messageID}`；保留 `id:`，维持 ID 序列无空洞）。
- meta 恒首帧（meta-first 不变）；v4 meta additive 扩展：capabilities 摘要 + epoch/seq 基线字段（v3 形状不动，B3b-4；**完整字段集与字段序见 §7.5**）。

### §7.3 tokens=1（已裁决终态）

- `/events?tokens=1`（v4）→ 400 `{"code":"tokens_stream_retired_in_v4","hint":"token 流请使用 /slimapi/sessions/{sid}/stream"}`；v3 请求该参数语义不变。（**一致性注记，B3b-5**：已核对实现错误体与本条逐字一致——`routes/events.py::TOKENS_STREAM_RETIRED_IN_V4`。）
- token 流端点 `/slimapi/sessions/{sid}/stream`：v4 起分配独立 id:（§7.1）；directory 消费保留（§5.2）。

### §7.4 q/p 帧载荷（`qpImmediateFull` 语义）

- **q/p 直推包装帧与帧名枚举（基线帧形；2026-08-22 D4-A 正文化转录 [冻结]）**：**完整枚举 6 个帧名** = `question.asked`、`question.v2.asked`、`permission.asked`、`permission.resolved`、`permission.v2.asked`、`permission.v2.resolved`——命中即**立即直推**（不走 digest debounce）；包装帧 data payload = `{directory, type, properties}`。事件名作为 data payload 的 **`type` 字段值**下发（**无 SSE `event:` 字段**），取上游 opencode 事件名 **verbatim**——sidecar 不重命名/不映射（`global_hub.py:739-744` 直通路径）。**客户端必须同时处理两种形式**（不可只订阅其中一种）：legacy（`type=="question.asked"` / `type=="permission.asked"` / `type=="permission.resolved"`）与 v2 namespaced（`type=="question.v2.asked"` / `type=="permission.v2.asked"` / `type=="permission.v2.resolved"`）；当前上游 opencode（v1.18.x）主要下发 namespaced 形式，legacy 形式仍在 sidecar 识别集合（二者均触发立即直推），客户端按 `type` 字段值分发。SSE 直推这些事件仅作观察信号；客户端应答 q/p 走 §10.1 写路由 #9-11（respondPermission / replyQuestion / rejectQuestion）。**历史演进注记**：本条转录自 v2-contract §3「q/p 阻塞信号（完整枚举 6 个帧名）」条，语义零改动（2026-08-22 D4-A）；v2 原文「应答走 catch-all + `X-Opencode-Directory`」指引随 3.0.0 catch-all 关闭失效，现行应答路径以 §10.1 写路由表为准。
- 逐字段核对结论（`design-v4-qp-payload.md`，B0-4 产出）：**已完整**——sidecar `properties` = 上游 event.data 原样透传（`event-v2-bridge.ts:39-44` 构造 → `global_hub.py:739-744` 零裁剪 → 上游 `core/question.ts:93-110`、`permission.ts:164-174` 发布完整 Request）；`question.asked`（10 字段）与 `permission.asked`（10 字段）逐字段比对**无缺失、无改名、无裁剪**。EventV2 envelope 字段（evt_ id/metadata/durable/location）按既有投影语义本就不进 properties，不属于缺失。
- **`qpImmediateFull` 语义冻结 = 现状已成立**：B1b 零 wire 变更，webui/ocdroid 直投为纯客户端改动；不触发 B3b-3 补全路径（该任务留空）。两套字段表（上游完整直投字段集 / 最小可渲染字段集）以 design-v4-qp-payload.md §2/§3 为权威，随实现批同步引用。
- digest 帧跨版本注记：B1a（P1 3.3.0）起 `session.digest` 增可忽略字段 `changed:[sid…]`（**最小语义已裁决**：changed = [本帧 sid]——digest 为 per-sid 逐帧产出，帧出现即 changed；形状保留列表为未来聚合留形），v4 帧形沿用。

### §7.5 SSE 跨视图同步语义（2026-08-19 补载 [冻结]）

以下 SSE 语义 v3/v4 两视图一致（逐条现状载明，本节即权威全文）；v4 附加差异单独标注：

- **digest 帧载荷字段集（基线帧形；2026-08-22 D4-A 正文化转录 [冻结]）**：`event: session.digest` data payload 字段集 = `{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?, turnIncarnation?, turn?}`——可选字段**仅发有变化的**（digest 为 per-sid 逐帧产出；debounce 250ms/session）。**`updatedAt` = sidecar wall-clock**（epoch-ms，sidecar 收到事件时；非上游 message `info.time.updated`——该字段 v1.18.x message 级不可靠），跨窗口严格单调不保证（时钟回拨/同毫秒批量）→ 客户端 watermark 必须用 **`(updatedAt, messageID)` 二元组字典序**（`MessageID = msg_+…` 单调递增、字典序可排）：先 strict 比 `updatedAt`（时间相等/回退→不删既有消息，幂等），相等再 strict 比 `messageID`。**字段来源**：`status` ← `session.status`（归一化恒字符串，见下条）；`messageID`/`updatedAt` ← `message.updated` / `message.appended`（该 sid 最新）；`archived` ← `session.updated` 的 `time.archived`；`deleted` ← `session.deleted`（`deleted=true` 时强制省略 `lastError`，见下条）；`turnIncarnation`/`turn` 为 flat 顶层配对字段（两字段必须同时出现或同时缺失；bump-before-send、per-sid 单调；跨项目 SSOT = ocdroid `docs/2026-07-31-oc-slimapi-turn-token-contract.md`）。**abort 静默丢弃**（`error.name=="MessageAbortedError"`：不写 lastError、不发 G1-B 帧）。**历史演进注记**：本条转录自 v2-contract §3 `session.digest`（G1-A）帧定义与 §5 watermark tie-break 条，语义零改动（2026-08-22 D4-A）；加性演进不在此重复——`changed:[sid…]`（§7.4 末条）、`lastError` provider 结构化字段与脱敏管线（§7.6）、status 归一化（下条）。两视图一致。
- **digest `messagesRevision`（修订五 [P4] 加性，4.11.0）**：digest 条目窗口含 message 域事件（`message.updated`/`message.appended`/`message.removed`）时，帧携带 `messagesRevision: <int>`——进程级全局单调修订号（relevant 事件 bump；多事件 debounce 窗口 flush 携带**窗口末值**）。**仅 message 域 digest 携带**（session-only digest 省略该键，条件发射同 §7.5 可选字段总则）；生命周期 = **进程**（重启清零，客户端**不得跨进程比较**；进程内 SSE 重连/upstream resync 可比较——resync 不清零）。`message.removed` 分支 bump 处语义序最后（retired gate → prune → token hub → bump）。用途 = **变化信号**（触发客户端对账：`/messages?since=` 差分或 If-None-Match 精拉），非序号承诺、不承载 per-sid 语义——与既有 `(updatedAt, messageID)` 双水位正交；与 §10.3 since 通道的对账兜底关系见 §10.3 末条。
- **digest `status` 恒字符串**：上游 `session.status` 的 status 字段可能以字符串（`"busy"`）或对象信封（`{"type":"busy"}`）两种 wire 形态到达；sidecar 统一归一化——digest `status` 字段**恒为字符串**（busy/idle 等上游状态值原样）；信封无效（缺 `type` / `type` 非字符串）时该次状态更新被忽略（digest 其余字段不受影响）。两视图一致。
- **digest `lastError` sticky 清除语义**：`session.error` 携带 sessionID → 该 sid digest 记 `lastError:{name, message, at}`（`name` 截断 128 字符）并立即定向 flush + 记入 sticky 存储；该 sid **下一次 `session.status=busy`** → sticky 弹出 + digest **显式 `lastError:null` 清除帧** + 定向 flush（busy 判定对字符串/对象信封两形态一致）；`session.deleted` → sticky 弹出 + 字段省略；后续 flush 在本窗口未自行设置/清除时合并 sticky 值（贴回语义，直到 busy 清除帧）；**sticky 仅在同一 sidecar 进程生命周期内成立**（进程内内存态、无持久化，重启即丢、不复活不重贴；重启后新 `session.error` 才重新记录）；FIFO 容量上限 10,000 sid，逐出后不再贴回。两视图一致。`lastError` 对象另含**可选结构化 provider 字段**（§7.6，2026-08-21 加性——`{name,message,at}` 基线三键与本条三态语义零变化）。
- **SSE 恒 identity**：两端点 SSE 流不做 gzip/content-encoding，响应**无 `Vary` 头**（响应头 = `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no`；SSE 路径不参与 `Accept-Encoding` 内容协商）。两视图一致。
- **digest 水位定位与 catch-up 盲区（after 游标等效方案裁决，2026-08-19 冻结）**：上游 `MessageV2.page()` 仅 `before` 向后 keyset（v1.18.18 实证），v4 messages 亦无 after 游标；增量 catch-up 等效方案 = **digest 触发 + 条件重拉**（水位仅当触发器不当过滤器；两盲区：`message.removed` 不进 digest、SSE 断连窗口无补偿；双轨消费 = digest 触发 If-None-Match 精拉 + 低频周期 304 对账兜底，重启/epoch 变化视为全失效）。v4 重放（§7.2 `id:`/Last-Event-ID）可缩小断连盲区但不消除（逐出/barrier 仍 resync），周期对账在两视图均为必选。两视图一致。
- **v4 附加——meta 首帧字段集（v3 形状不动）**：v4 视图首帧 `event: slimapi.meta` data 字段序 = `subscriberId, tokens, capabilities, epoch, seqBase`；v4 追加三键：`capabilities: {"sseReplay": true}`（**恒此一键**——`qpImmediateFull` 仅广告于 `GET /slimapi/versions` 的 `capabilities["4"]`，不入 meta 帧）、`epoch`（进程代，16 hex 字符串）、`seqBase`（连接建立时该域已发布最大 seq，整数；首连后首个带 `id:` 帧恰为 `seqBase + 1`）。meta 帧自身**无 `id:`**（§7.0②）。
- **v4 附加——welcome 帧抑制**：v4 连接不产出连接本地 `server.connected` 首帧（v3 照旧产出）；v4 线上首帧恒为 `slimapi.meta`。

### §7.6 lastError 结构化 provider 字段（2026-08-21 加性修订 [冻结]）

digest `lastError` 对象（G1-A，有 sid）与 session-less `event: session.error` **直推帧**（G1-B，帧形 `{directory?, name, message, at, code, provider?, model?, retryAfter?, quotaResetAt?}`）**同步增补**下列字段（camelCase）；既有基线键 `{name, message, at}` 不变，§7.5 sticky 三态语义（记录 / `busy` 显式 `null` 清除 / `session.deleted` 弹出省略）**零变化**——增补字段位于错误对象内部，清除帧与字段省略行为不受影响。

**字段表**：

| 字段 | 类型 | 何时出现 | 约束 |
|---|---|---|---|
| `code` | string | **恒有**（增补生效后的错误对象） | 枚举见下表；无法分类 → `provider_error` 兜底 |
| `provider?` | string | 仅可推导时 | ≤64 字符，字符集 `[A-Za-z0-9._\-/:]`；不满足 → 字段缺省（不报错） |
| `model?` | string | 仅可推导时 | 同 `provider` 约束 |
| `retryAfter?` | int（秒） | 仅可推导时（**来源一**：上游结构化字段；**来源二**：`message` 文本提取 regex v3 `(?i)(?:retry\|try again)\s+(?:in\|after)\s+(\d+(?:\.\d+)?)(?:[ \t]*(?:seconds?\|secs?\|s)(?![A-Za-z0-9])\|(?![A-Za-z0-9.])(?![ \t]*[A-Za-z]))`——两分支：① 单位分支：同行空白 `[ \t]*` + 完整单位词（seconds?/secs?/s），后卫仅拒字母数字（句号/逗号/续句合法）；② 无单位分支：拒紧邻字母数字点（`30ms`/`1e3s`/`30.5.5s`）+ 拒**同行**空格后字母（`30 ms`/`30 minutes`；不跨行）。示例：`30 seconds.`→30、`30 seconds before retrying`→30、`30\nNext line`→30、`30, please wait`→30；`30 ms`/`30 minutes` 拒；`\|` 为正则 alternation 的表格转义） | 任何来源的值一律**向上取整（ceil）后 clamp 1..86400**；纯整数部分 >9 位或总长 >15 的天文数字直接 clamp 86400（不做浮点转换）；非数字结构化值丢弃（字段缺省） |
| `quotaResetAt?` | number（int/float）或 ISO-8601 字符串 | **仅上游结构化数据提供时** | 仅接受 number 或 ≤64 字符且可被 ISO-8601 解析的字符串；int 值仅在 **[-2^63, 2^63-1]**（64-bit 有符号整数范围）内原样输出，超范围 → 字段安全缺省（丢弃）；float 仅要求有限（isfinite）；其余类型（含对象/数组/超长/非 ISO 文本/NaN/Inf）**安全降级为缺省——绝不原样透传任意上游值**（sidecar 不从文本推导） |

（`name` 截断 128 字符、`message` 沿用既有脱敏管线——增补字段不改变两者既有约束。）

**code 枚举与客户端展示建议**：

| code | 语义 | 客户端展示建议 |
|---|---|---|
| `provider_rate_limited` | 限速 | 可配 `retryAfter` 倒计时，到点前禁用重发 |
| `provider_quota_exceeded` | 配额耗尽 | 可用 `quotaResetAt`（如有）提示恢复时间；勿自动重试 |
| `provider_model_overloaded` | 模型过载 | 提示稍后重试（可短退避，`retryAfter` 如有可用） |
| `provider_context_length_exceeded` | 上下文超长 | 提示精简对话/开新会话，勿原样重试 |
| `provider_unauthorized` | 认证/凭据失败 | 提示检查 provider 凭据，勿自动重试 |
| `provider_model_not_found` | 模型不存在 | 提示更换模型 |
| `provider_error` | 无法分类兜底 | 回退展示脱敏 `message` |

- **分类依据与优先级（两级，冻结）**：
  1. **结构化信号（优先）**，来源 `error.data` 白名单：
     - `code`（string）：值**等于**七枚举成员之一 → 直接采用；其他值忽略。
     - `type`（string，小写匹配）白名单映射：`rate_limit_error`→`provider_rate_limited`、`overloaded_error`→`provider_model_overloaded`、`authentication_error`→`provider_unauthorized`、`insufficient_quota`→`provider_quota_exceeded`；未列 type 忽略。
     - `status`（整数）：401→`provider_unauthorized`、429→`provider_rate_limited`、402→`provider_quota_exceeded`；其他值忽略。
     - 结构化内部优先级：`code` 直用 > `type` 映射 > `status` 映射；全部未命中 → 落文本分类。
  2. **文本模式（兜底）**：`name` + 原始 `message`（pre-sanitize）小写子串匹配；多类命中按严格短路序取首个：**unauthorized → model_not_found → context_length → quota → rate_limited → overloaded → 兜底**；`401`/`429` 以数字边界匹配（前后非字母数字，防 `1401` 类假阳性）。
- **安全白名单承诺**：sidecar 只输出上表白名单字段 + **已脱敏** `message`（沿用既有脱敏管线）；**绝不透传**上游原始响应体 / 内部堆栈；`provider`/`model` 输出承诺 = **白名单 + 字符集 + 长度 + 凭据形态多重防线，覆盖常见凭据形态**（非对任意未知形态的绝对"绝不泄露"保证）——字符集与长度校验之外另设两道凭据形态防线：① 已知凭据前缀黑名单（`sk-` / `sk_` / `pk-` / `rk-` / `ghp_` / `gho_` / `ghu_` / `ghs_` / `github_pat_` / `xoxb-` / `xoxp-` / `xoxa-` / `AKIA` / `AGPA` / `AIDA` / `AIza` / `eyJ`）；② 高熵启发式（连续字母数字段 ≥32 丢弃）。任一校验/防线不满足 → 字段缺省（不报错）。
- **向后兼容（加性）**：老客户端忽略未知键即可，零必改点；不 bump wire 版本。未知 `code` 值（未来扩展）客户端应回退按 `message` 展示。
- **写路径不变**：provider 错误经 SSE `session.error` 送达；`prompt_async` 等写路由（§10.1 基线）**立返 202**，4xx verbatim / 5xx→503 冻结职责不受本修订影响。

### §7.7 token 流业务帧形（`GET /slimapi/sessions/{sid}/stream`；2026-08-22 D4-A 正文化转录 [冻结]）

> 历史演进注记：本小节转录自 v2-contract §3.x.2「Wire 帧」（P1 范围/opt-in 语义转录自 §3.x.1；端点现行态见 §7.3 末条），语义零改动（2026-08-22 D4-A）。v2 原文「不发 SSE `id:` 字段；`Last-Event-ID` 仅触发首帧 resync、值忽略」为 v2/v3 视图行为——v4 视图按 §7.1/§7.2 分配独立 id: 并支持重放，两代语义以 §7.0-§7.2 冻结条款为准，本节照录 v2 基线帧形不改写。

帧清单（6 类）：

```
# 1) 订阅首帧：活跃 part 累计全文锚点
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<累计全文>","done":false}

# 2) 批式增量（100ms / 4KiB flush）
event: message.part.delta
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<本窗拼接>"}

# 3) 终态 marker（杠杆1：去终态全文——仅完成标记，无 text；权威全文走 /messages/{sid}）
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","done":true}

# 4) 大 part 超 1MiB（done:false 或 done:true 均可能）——不静默 drop
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","truncated":true,"done":false|true}

# 5) resync（背压/重连/超大/内存上限/生成结束清理；token resync 恒带 sessionID）
event: resync
data: {"reason":"subscriber_backpressure|reconnect_no_replay|token_memory_limit|session_idle|session_deleted","sessionID":"…"}

# 6) server.connected{sessionID} / server.heartbeat{}（15s）
#    └ Q2 校正注记（2026-08-22 owner 裁决，按代码事实）：v4-only 面**抑制
#      server.connected**——v4 握手 = no-prefill join，连接本地帧仅有
#      slimapi.meta（首帧恒为它，§7.2 终态；tests/test_token_stream_route.py:861-888
#      白盒钉死：无 server-originated message.part.snapshot，状态对齐 = resync 后
#      客户端 HTTP full fetch）。本行 server.connected 描述保留为历史（v2/v3）
#      形态；heartbeat 不受影响。
```

- **`message.removed` 帧（token-stream 保留，非控制面）**：token hub 收到上游 `message.removed` 后：(1) 清理该 message 的累加器/修订状态；(2) 向当前订阅者 fan-out `message.removed{sessionID,messageID}` 帧；(3) 记入有界回放队列（cap 1000 / TTL 24h）。客户端在**握手期**可能收到回放的 `message.removed` 帧（`server.connected` → tombstones 回放 → snapshot → fanout），运行时也可能收到实时 fan-out 帧；收到后应丢弃该 message 的 live 渲染态。控制面 `session.digest` 的 `deleted=true` 是独立信号，二者共存、不替代。队列不受 `resync_all` / `on_upstream_reconnect` 影响。**Q2 校正注记（2026-08-22 owner 裁决）**：上述「握手期回放」（含 server.connected 与 tombstone/snapshot 预填）为 v2/v3 历史形态——v4-only 面握手 = no-prefill join，无握手期回放（tests/test_token_stream_route.py:861-888 白盒钉死）；**运行时 fan-out 路径不变**，`message.removed` 帧在 v4 下经运行时 fan-out 及重放域（§7.2 tombstone 条，帧形 `frames.py:137-151` 现行）照常到达。
- **终态顺序不变式（wire 强约束）**：对同一 `(sid,mid,pid)`，所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不许再发 delta。
- **杠杆1（决定性）**：终态 `snapshot{done:true}` 是**仅完成 marker，不带 text**——权威全文走 `/messages/{sid}`（持久化真值），幂等覆盖且凌驾所有 token 帧；客户端可接受 digest 完成先于/晚于 token 终态帧。
- **resync reasons**（token 流均带 `sessionID`）：`reconnect_no_replay`（上游重连）/ `subscriber_backpressure`（订阅者 T3 溢出）/ `token_memory_limit`（全局累加器上限）/ `session_idle`（生成结束清理）/ `session_deleted`（会话被删除）。**单 part >1MiB 不走 resync**，走 `snapshot{truncated:true}`——客户端清该 part 渲染态、停 append、走 `/messages/{sid}`。
- **`session_deleted` 服务端终止**：上游 `session.deleted` 到达时，sidecar 对该 sid 的所有 token 订阅者**同步**发 `resync{session_deleted,sessionID}` 后立即终止连接（发 STOP 关闭流）；客户端收到该 resync + 连接关闭后应视为该 session 的 token stream 已结束，如需重建须重新订阅。
- **truncated 处理**：收 `snapshot{truncated:true}`（`done:false` 或 `done:true` 均可能）→ 清该 part 渲染态、停 append、走 `/messages/{sid}` 重拉权威。
- **reasoning/tool part**（`part.type!="text"`）的 delta **静默 drop+计数**，不 resync；field≠"text" 的 delta 丢弃。
- **per-frame `partEventRevision`**：token stream 帧的 `partEventRevision` 由 token hub **per-frame** 维护（每帧唯一递增）；客户端按 strict `>` 去重（消费端算法见 CLIENT_CHANGES.md「partEventRevision 必须 strict `>` 去重」节）。
- **P1 范围与 opt-in**：仅 text part（reasoning / tool-input 延后 P2+）；不做二进制流。客户端前台/动画层才连；切背景/换 session 应断开（同时最多 1 条前台 stream）；连接独立于控制面 `/events`，两条连接互不替代。批式增量 = token-stream flush loop（100ms / 4KiB 窗口）对每个 `(sessionID, messageID, partID)` 完成窗口拼接；渲染须对任意 batch 稳健（不按 token 计数、不假定帧间隔）。背压复用既有 subscriber T3 守卫（`resync{subscriber_backpressure}` + 断连）。
- **token T3 资源信封（独立账本；2026-08-22 Q4 正文化转录 [冻结]）**：token 订阅**独立账本**，与控制面 SSE T3 隔离——不占控制面 subscriber 上限，避免 token 高吞吐挤掉 q/p 或误触控制面背压。**admission 常量**：`token_stream_max_subscribers=8`、`token_stream_queue_items=64`、`token_stream_buffer_bytes=512KiB/sub`、`token_stream_max_frame_bytes=1MiB`。**内存预算 = Option B（拆 4+4，不双计）**：`TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live LivePart.chunks 累计）、`TOKEN_PENDING_MAX_BYTES=4MiB`（pending DeltaAccumulator 累计，与 live **不双计**——同一 delta chunk 不在两池同时占额度）、单 part 上限 `TOKEN_PART_MAX_BYTES=1MiB`、全局活跃 part 数 `TOKEN_LIVE_PARTS_MAX=32`；worst-case ≈ `8 × 512KiB + 4MiB + 4MiB = 12MiB`（runtime 正常态）。`_reserve` 处理 delta 超剩余预算 → 退役最旧 part（按 `last_delta_ms`）+ `resync{token_memory_limit,sessionID}`。**admission 溢出** → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。**历史（v2/v3）形态如实标注**：① `token_stream_handshake_buffer_bytes=8MiB/sub` 握手暂存与 503 `sse_token_handshake_overflow`（items 超 2048 / bytes 超 8MiB）——v4 no-prefill join 不 bracket 握手，该失败模式在 v4-only 面**结构性不可达**（tests/test_token_stream_route.py:972-976 移除注记；常量与错误码保留定义）；② v2「token stream 默认 gzip（杠杆2，首个 SSE gzip 例外）」已被 v3 起恒 identity 取代——现行 = §7.5「SSE 恒 identity」两端点无例外。**历史演进注记**：本条转录自 v2-contract §6.x「Token stream T3 信封」，语义零改动（2026-08-22 Q4）；v2 worst-case 原文含 handshake buffer 项（`8 × (512KiB + 8MiB) + 4 + 4 = 76MiB`），v4 无握手暂存故上文本按 runtime 形态载 12MiB，76MiB 为 v2/v3 历史口径。

## §8 错误族与优先级 [冻结]

### §8.1 新增错误码

| code | 码 | 场景 |
|---|---|---|
| `directory_retired_in_v4` | 400 | §5.2；统一错误体 + hint，不泄露目录存在性 |
| `tokens_stream_retired_in_v4` | 400 | §7.3 |
| `invalid_cursor` | 400 | §4.5 |
| `auxiliary_unavailable` | 503 | §4.2；附 `Retry-After: 30`；错误体不含 DB 路径/schema/白名单内容 |
| `raw_decode_failed` | 502 | §19（修订五）；`/slimapi/file/raw` 上游 200 但 `LegacyContent` 信封畸形（判别失败/缺字段/类型错/严格 base64 解码失败）——house error renderer，no-store，无上游细节泄漏 |
| 参数版本不匹配（`v3 收 v4 参数 / v4 收 v3 参数`；code 字面量 `param_version_mismatch`——含 v4 limit 域外 501..1000 / archived 非三态 / parent 空串，2026-08-21 F-025 命名） | 422 | §4.1 |

### §8.2 403 vs 400 族

allowlist 403 族（`directory_not_allowed`，B4-4）与版本/directory 400 族命名区分（v2.2 行 188）；403 不泄露目录存在性（统一错误体）。

### §8.3 跨版本错误优先级真值表（S-B04 冻结；design-v4-selector §3 同源）

总链：**①405 versions 非 GET → ②selector version 族 400 → ③selector directory 族 400（v4 sessions = directory_retired_in_v4 整体替换）→ ④路由 422 参数版本不匹配 → ⑤路由 400 invalid_cursor → ⑥路由 503 auxiliary_unavailable → ⑦404/其余**。

| 组合 | 裁决 |
|---|---|
| malformed cursor vs auxiliary unavailable | 400 `invalid_cursor` 优先（语法校验先于降级判定） |
| 指纹不匹配 vs 熔断 | 400 优先（指纹校验在查询前、纯内存计算） |
| directory_retired_in_v4 vs roots/start 参数错误 | 400 directory 族优先（selector 层先于路由层） |
| repeated v（多值同值）vs 路由错误 | 折叠后正常路由，不因重复 400 |

### §8.4 2026-08-19 修订新增错误码 [冻结目标——当对应 feature ID 进入 `satisfied`（§3.3 门控）时生效；生效范围见 §12.5/§16]

| code | 码 | 场景 |
|---|---|---|
| `method_not_applicable` | 405 | §16：三条 deferred method-path 组合误用（**仅修订后 `?v=4` 视图**）；附 `method`/`allow`；`Allow` 头 + 不转发上游 + `Cache-Control: no-store`。**修订二适用面收窄**：仅 `session.post-actions.v4 ∉ satisfied` 的过渡期命中这三条组合（4.2.0 现行为）；该 ID 激活后**无命中面**（三条 POST 成为等效路由），错误码**保留定义**（历史过渡态 + 防御性保留，不删除） |
| `provider_upstream_malformed` | 502 | §12.5：上游 provider 数据形状违约全类目；错误体零上游细节 |
| `provider_projection_limit` | 413 | §12.4：四项投影限额任一超限；附 `limit`/`limitValue` |

**复用既有码**（不重定义、不改语义）：`response_too_large`（providers 源 body 超限，§12.5.2 ④）、`upstream_http_<N>`（providers 3xx/4xx 转换路由映射）、`upstream_unavailable`、`transform_busy`、`auxiliary_unavailable`（§13 整响应失败复用——扩展触发面不改 status/body/`Retry-After` 形状）、`invalid_cursor`、`session_not_found`、`thin_route_not_found`、expand 错误族（§14.3）。`discovery contradiction`（§3.3）为客户端分类结局，非服务端错误码。**优先级插列**：修订生效后 §8.3 总链在「② selector version 族 400」与「③ selector directory 族 400」之间插列 method 405（§16——判定不依赖 query 参数）。

**[4.6.1] 追加：405 `method_not_allowed`（非 GET `/slimapi/versions`）、WS 501 `websocket_not_supported`、actions `invalid_request_body`（422 POST body 畸形）三码补录（历史实现现状固化）**——v3-contract §8 末尾镜像同句。

## §9 观测 [冻结]

### §9.1 维度扩展

- access log / traffic snapshot：`selectorResult` 可产出值 = `v4|rejected|exempt|not_applicable`（历史增量表述：B3a 增 `v4`；`v3` 生产者随 2026-08-21 版本窗收窄移除，枚举值保留可解读）；`wireVersion` 可产出 "4"|None（历史增量表述：增 "4"）；SSE active 维度同步扩。
- DB 辅助指标（B3a-B5）：查询延迟（P50/P99）、降级计数、熔断计数、重探事件、inode swap 事件。
- replay 指标（B3b）：hit/miss/gap/resync 计数。
- **[4.6.0] 追加：`/slimapi/metrics` hubs[] 条目加性键 `droppedEventsByType`**——per-type 上游事件丢弃计数（有界，类型基数 ≤257 含 `__other__` 兜底桶；快照为浅拷贝、空表恒发布 `{}`）；既有键零改动（2026-08-21 owner R-5 裁决；沿 v3-contract §9 同句，两视图共享该 metrics 形状）。

### §9.2 bucket

v4 sessions 归入 sessions 桶既有记账；降级路径请求带 degraded 标记维度（可区分 DB/HTTP 源）。

### §9.3 运维信号

`/slimapi/health` `auxiliary.available=false` = DB 辅助禁用/熔断（runbook 见 operations.md §7：升级 opencode 后第一步观察）。

### §9.4 v3 退役（P4，2026-08-21 版本窗收窄修订）

**4.8.0 起 v3 wire 版本已退役**：`ACCEPTED_CLIENT_VERSIONS = (4,4)`，`?v=3` 答复 400 `unsupported_version` `supported:[4]`。原 (3,4) 永久双版本裁决（2026-08-19 owner 终态裁决）被本修订覆盖——退役通过版本窗收窄（4.8.0 minor；major 仅跟协议大版本 bump，owner 2026-08-21 裁定）直接实施，不经观测判据评估。历史观测数据（08-19 99.7% → 08-20 69.5% → 08-21 49.1%，SSE active v3=0 连续两日）为 merge 门佐证，非退役触发条件。

## §10 路由全集逐条（v4 差异列）[冻结]

**51 条** /slimapi 路由（read **26** + write **17** + SSE 2 + 发现/运维 **6**；**计数方法 = 路由 × 方法表行**，与 `scripts/check_routes_doc.py` 的路由↔INTERFACE_MAP 一致性校验同口径——2026-08-19 修订，取代原「45 条（read 23 + write 12 + 发现/运维 8）」旧计数）。**已发布（4.0.0/4.1.0）v4 差异仅下列 4 条**，其余 **47** 条 v4 无版本分叉（经 selector 分派；历史 4.0.0–4.7.0 双版本期两视图行为一致，v4-only 窗下唯一可达视图 = v4——路由/方法/directory/ETag/统一行为语义全文见 §10.1 基线路由表与 §10.2 消息投影基线）；**2026-08-19 正式修订（冻结目标）追加差异面见本节末修订块与 §12-§17**；**修订二（owner 裁决 2026-08-19，已实施——write 组三条 POST 等效动作路由已激活，`session.post-actions.v4 ∈ satisfied`）**：

| 路由 | v4 差异 |
|---|---|
| `GET /slimapi/sessions` | §4 全量（DB 投影源/参数矩阵/降级矩阵/cursor/无 ETag）；directory → 400 |
| `GET /slimapi/events` | §7：v4 分配 id:/重放；`tokens=1` → 400；directory 消费不变（events 非消费集路由，目录帧过滤随 allowlist） |
| `GET /slimapi/sessions/{sid}/stream` | §7：v4 分配独立 id:（token 流）；directory 消费保留 |
| `GET /slimapi/versions` | §3.1 v4-only 载荷（`available`:[4]、capabilities 仅 "4" 面；历史 4.0.0–4.7.0 为双版本载荷） |

`GET /slimapi/health` 视图差异（§3.2，v4-only 单视图；历史 4.0.0–4.7.0 为双视图）为响应差异，路由行为不变。messages（4 条 + 2 expand）、sessions/status、todo/children/diff、directories、agent、command、file/vcs/find/config/session-single/context（读组）、active、global/health、metrics、ready、actions（2）、write 17 条：**零 v4 差异**。其中两处显式注载（2026-08-19 补载）：

- **`GET /slimapi/session/{sid}` 单查**：`?v=4` 下**不升级 v4 骨架形状**——恒返回既有 skeleton 投影（§4.1 `SESSION_KEYS` 白名单列集；SessionSkeletonV4 仅用于 `GET /slimapi/sessions?v=4` 列表项；单查无 v4 分叉）。v4 客户端取单会话 v4 骨架走 `/slimapi/sessions?v=4` 列表。
- **`GET /slimapi/sessions/status`**：零 v4 分叉（无版本分支代码路径），directory 消费 = §5.1 基线（§5.2 表行：query 单值消费剥离、头出现 400、多值 400）；上游 `/session/status` 数据与 directory 无关（恒全局 map），`directory` 仅作 workspace 路由通道。
- 2 条 expand 路由的可达性与能力探测口径见 §3.1 注记（`capabilities["4"]` 无 `expand` 键）。

### §10.1 基线路由表（47 条零差异路由的权威语义；2026-08-21 正文化转录 [冻结]）

> 历史演进注记：本小节语义 4.0.0 起原经继承基线条款指向 v3 契约 §10/§4；F-126 自包含化后就地正文化，语义零改动。

**统一行为（受控代理，冻结）**：路径 = legacy 路径加 `/slimapi` 前缀；sidecar 不改写成功语义，叠加保护 + 审计 + `?v=`/`?directory=` 消费（§5.1）。**错误两级制**：成功（2xx）状态码+body 逐字透传；**4xx 状态码+body 逐字透传**（客户端校验错误原样到达）；**上游 5xx/网络错误 → 503 `upstream_unavailable`**（legacy 直连会收到上游原始 5xx 码，此为已知迁移点）。**admission（冻结）**：请求超限 → 413（既有 `max_request_bytes` 语义）；响应超限 → 413 `response_too_large`（读 cap = `max_response_bytes`，`read_with_cap` 口径——严格大于才超限、恰等于上限合法）；**纯 raw 受控代理不占 transform 池**（无投影变换，仅流式透传+上限检查，不产生 `transform_busy`）。**上游响应头透传集合冻结** = `Content-Type`、`Location`（上游 3xx 重定向：状态码 + body 均逐字透传，sidecar 不跟随不重写）、`Retry-After`、上游 `X-Request-ID`/`Last-Request-ID` 追踪头；其余上游自定义头不透传。**content-coding 规则**：上游 `Content-Encoding` 不透传——上游响应经解码后取实体字节，admission 按实体字节计，sidecar 按自身 gzip 族重新编码并生成自己的 `Content-Encoding`/`ETag`（「body 逐字透传」均指实体字节）。**错误 body 读取上限**：错误路径 body 读取同样受 `max_response_bytes` 保护；超限时无法逐字透传，降级 503 `upstream_unavailable`（资源保护优先于逐字义务）。**投影路由执行域（session 单查）**：仅当上游响应为 2xx 且 body 为合法 JSON object 时投影；其余一切状态（含 204 空 body、3xx 非 JSON）逐字透传不投影；投影属转换工作，经转换池 offload 执行（事件循环不承载 JSON 解析/序列化）。

**envelope 与分页（messages/sessions 列表；status 不 envelope 化）**：`GET /slimapi/messages/{sid}` → `{"items": [...], "nextCursor": <string|null>}`（游标不回退；`nextCursor` 为 opaque base64url，解析自上游 `Link: <...?before=CURSOR>; rel="next"`，客户端以 `?before=` 向旧方向翻页 drain；列表按 `time.created` 升序；`mode=merged` 同样适用）；`GET /slimapi/sessions`（`?v=3` 视图——4.0.0–4.7.0 历史行为，v4-only 窗下 `?v=3` 已 400）→ `{"items": [...], "complete": <bool>}`（complete 非权威性强制语言沿用，无 `nextCursor`；v4 视图 envelope 见 §4.1）；`GET /slimapi/sessions/status` 不 envelope 化（map，无分页）；错误响应不 envelope；304 无 body（§6）。

**读组（9 组；directory 列 = §5.1 消费语义，ETag 列 = §6 全集归属）**：

| 组 | 路由 | 上游 legacy | 方法 | directory | ETag |
|---|---|---|---|---|---|
| file | `/slimapi/file`、`/slimapi/file/content`、`/slimapi/file/status` | `/file*` | GET | 消费（`/file`、`/file/content`=FileQuery 族；`/file/status`=WorkspaceRoutingQuery） | 启用 |
| vcs | `/slimapi/vcs`、`/slimapi/vcs/status`、`/slimapi/vcs/diff` | `/vcs*` | GET | 消费（WorkspaceRoutingQuery） | 启用 |
| find | `/slimapi/find/file` | `/find/file` | GET | 消费（FindFileQuery） | 启用 |
| providers | `/slimapi/config/providers` | `/config/providers` | GET | 消费（WorkspaceRoutingQuery） | 启用 |
| session 单查 | `/slimapi/session/{id}` | `/session/{id}` | GET | 消费 | 启用 |
| active | `/slimapi/api/session/active` | `/api/session/active` | GET | 不消费 | 启用 |
| global health | `/slimapi/global/health` | `/global/health` | GET | 不消费 | 启用 |
| messages.expand | `GET /slimapi/messages/{sid}/expand/{category}/{mid}`、`GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`（§14；**转换端点**，非 raw 受控代理） | `/session/{sid}/message/{mid}`（singleflight 共享 GET） | GET | 消费（§5.1） | 不启用（恒 200） |
| session.context | `GET /slimapi/session/{sid}/context` | `/api/session/{sid}/context`（上游 v2 session 组） | GET | 不消费（按 sid 自路由；`?directory=` 宽容剥离不转发不报错） | 启用 |

（既有 thin 不重复列：sessions/messages/status/todo/children/diff/permission/question/agent/command/directories——todo/children/diff 无 ETag 为 **≤4.10.x 历史语义**（`If-None-Match` 忽略、恒 200）；**修订五 [2026-08-22] 起三路由入 §6.4 ETag 全集**，现行语义以 §6.4 为准；messages.expand 组不适用「错误两级制」与「上游响应头透传」（响应头由 sidecar 全权拥有 §14.1、错误映射 §14.3、占用 transform 池——池满 503 `transform_busy` + `Retry-After`）。）

**既有 thin 路由语义（todo / children / diff / questions / permissions / agent / command / sessions-status；2026-08-22 D4-A 正文化转录 [冻结]；agent / command / sessions-status 三行同日 Q4 追加）**：

> 历史演进注记：本块转录自 v2-contract §2 端点表对应行（todo / children / diff / questions / permissions 行——D4-A；agent / command / sessions/status 行——Q4 追加）及「`/slimapi/questions` envelope」节，语义零改动（2026-08-22 D4-A + Q4）。v2 行文中的历史表述随代际演进失效处如实标注：`X-Slimapi-Version: 2` 未 bump 表述随 3.0.0 版本头退役失效（§1）；「应答走 catch-all + `X-Opencode-Directory`」随 3.0.0 catch-all 关闭失效（应答 = 本节写路由 #9-11）；v2 行文 Vary 值（`Accept-Encoding, X-Opencode-Directory`）随头维度退役失效，表示层以 §6/§15 现行规则为准；directory 消费按 §5.1 基线（v2「仅 `X-Opencode-Directory` header 转发」已失效）；v2「加性：旧 sidecar 无此路由→catch-all 404 回退」句为版本史注记，未转录。

- **`GET /slimapi/sessions/{sid}/todo`**：透传上游 `GET /session/{sid}/todo`，近恒等投影（上游 `Todo.Info` `{content,status,priority}` schema 已最小——无白名单杠杆，路由价值 = gzip + cap + admission + 结构化错误）。转换池 admission 先于 upstream GET + 流式 `read_with_cap`（超 `max_response_bytes`→413 `response_too_large`）+ worker gzip（**空 `[]` 跳过 gzip**——<64B 受益门）；**无 ETag/304 为 ≤4.10.x 历史语义**（响应无 `ETag` 头，`If-None-Match` 被忽略、恒 200）——**修订五 [2026-08-22] 起入 §6.4 ETag 全集**（命中 validator 可能 304），现行以 §6.4 为准。
- **`GET /slimapi/sessions/{sid}/children`**：透传上游 `GET /session/{sid}/children`，每项经既有 `skeleton_session()` 投影（与 sessions 列表**逐字相同** keep/drop：丢 `cost`/`tokens`/`location`/`subpath`）。**状态量护栏**：无 `X-Children-Version` 头、无 `childrenVersion` digest 字段、无 `childrenIDs[]`/`childrenComplete` list hints、无 cache/single-flight/SSE 失效——纯读（v1 状态机保持删除）。admission/cap-read/gzip（空 `[]` 跳过）/无 ETag 同 todo（**无 ETag 为 ≤4.10.x 历史语义，修订五起入 §6.4 全集——同 todo 注记**）。
- **`GET /slimapi/sessions/{sid}/diff`**：透传上游 `GET /session/{sid}/diff`，近恒等投影（上游 `Snapshot.FileDiff` `{file?,patch?,additions,deletions,status?}` schema 已最小——`patch` 大字段保留，gzip 是省流杠杆）；**`messageID` 可选同名 query 原样透传上游**（缺省不发——上游语义 = 返回 `[]`：缺省/未知消息/非 user 角色均答 200 `[]`，空结果为正常 body 非错误）。admission/cap-read/gzip（空 `[]` 跳过）/无 ETag 同 todo（**无 ETag 为 ≤4.10.x 历史语义，修订五起入 §6.4 全集——同 todo 注记**）。
- **错误映射（todo/children/diff 共用）**：上游 404 → 404 `session_not_found`（带 `sessionID`）；其他 4xx → 502 `upstream_http_N`；5xx/网络/坏 JSON/非 list/逐项非 dict → 503 `upstream_unavailable`；池满 → 503 `transform_busy` + `Retry-After: 2`。
- **`GET /slimapi/command`（catalog skeleton；Q4 转录）**：透传上游 `GET /command`，白名单投影每项 `{name,description,agent?,hints?}`（丢 `template`(~97.7% 字节)/`source`/`model`/`subtask`；raw 省 ~97.6%）；command catalog 全局，上游忽略 directory。转换池 admission 先于 upstream GET + 流式 `read_with_cap`（超 `max_response_bytes`→413）+ worker gzip。上游 4xx → 502 `upstream_http_N`；5xx/网络/坏 JSON/非 list → 503 `upstream_unavailable`；池满 → 503 `transform_busy` + `Retry-After: 2`；参数错误 422。catalog 无 `hasFull`/`omitted`。
- **`GET /slimapi/agent`（catalog skeleton；Q4 转录）**：透传上游 `GET /agent`，白名单投影每项 `{name,description,mode,hidden?,native?}`（丢 `prompt`(~34.7%)/`permission`(~61.2%，`Permission.Ruleset` 规则集——**非** pending permission card)/`topP`/`temperature`/`color`/`variant`/`options`/`steps`/`model`；raw 省 ~95.8%）；catalog 全局。转换池 admission + 流式 `read_with_cap` + worker gzip（同 command）；错误映射同 command；catalog 无 `hasFull`/`omitted`。
- **`GET /slimapi/sessions/status`（只读 status 投影；Q4 转录）**：透传上游 `GET /session/status`（`Record<SessionID, {type:"busy"|"idle"|"retry"}>`）+ sidecar merge 每条目 flat 顶层 `turnIncarnation`/`turn`（源自 `TurnRegistry.snapshot`，与 digest SSE turn 字段同源——§7.5 digest 字段集条；未观测 sid → `(inc,0)`；turn_registry 未装配时两字段配对缺省）。端点只读、不写、不缓存——同内存只读投影，不引入新状态机。`directory` 可选（不传 → 200 全局 map + 不转发 directory；传 → normalize+透传——上游 handler 零参数，directory 对上游是 no-op，恒返全量 map）。上游非 dict body → 503 `upstream_unavailable`；上游 4xx → 502 `upstream_http_N`；5xx/网络 → 503。v4 差异注记见 §10 既有载（零 v4 分叉；directory 消费 = §5.1 基线）。
- **`GET /slimapi/questions`（跨目录 pending question 聚合；无参数，sidecar 自发现目录）**：修复 slim-mode 冷启动看不到非-`process.cwd()` 目录 pending question 的 bug（上游 `GET /question` per-Location）。两阶段 fan-out：(1) `GET /experimental/session?roots=true&archived=true`（opencode 全局顶层 session 列表 + 含已归档 session，每个 session 携带真实 `directory` 字段，覆盖 git repo + 非-git 目录 + git worktree 子目录 + archived-only workdir）发现 distinct directory 集合；(2) 并发对每个 dir `GET /question`（带 `X-Opencode-Directory`）合并。返回 envelope `{items, errors, authoritativeDirectories, discoveryComplete}`（语义见下）；每条 item = 上游 entry 原样 + `directory` 字段。发现失败 → 整体 503 `upstream_unavailable`（无 envelope）；per-dir 失败 isolated 进 `errors[]`（不中断整体）。
- **`GET /slimapi/permissions`（跨目录 pending permission 聚合；无参数、无 body）**：两阶段 fan-out **镜像 questions**（同一发现调用 + per-dir `GET /permission` 带 `X-Opencode-Directory: <dir>`）。上游返**裸数组** `PermissionV1.Request[]`；sidecar 白名单投影（保留 7 字段 `id`/`sessionID`/`permission`/`patterns`/`metadata`/`always`/`tool?`，丢弃未知）+ 逐条 stamp `directory`。每 dir **独立隔离**（网络/5xx/非 list → `errors[]` `upstream_unavailable`；4xx 含 unlikely 404 → `errors[]` `upstream_http_N`；单 dir 失败不中断整体）。内部预算三 knob（`permissions_max_response_bytes`/`permissions_fanout`/`permissions_max_aggregate_bytes`，ops 面）触发 → `truncated:true` + `authoritativeDirectories` 降级。envelope 同 questions。健康公告 `features.permissionEvents`。
- **q/p 聚合 envelope 语义（questions/permissions 共用；转录自 v2-contract §2「`/slimapi/questions` envelope」节）**：
  - `items` = **所有已发现** directory 的合并 entry（上游 entry 原样 + 追加 `directory` 字段）。
  - `errors` = per-directory 失败，每条 `{directory, code, message?}`（code：网络/5xx/非 list body → `upstream_unavailable`；4xx → `upstream_http_N`）；**单个 directory 失败绝不中断整体请求**（isolated）。
  - `authoritativeDirectories`（客户端契约依赖）：**`null`** ⇔ `errors` 为空 **且** `discoveryComplete == true` → 全成功 + 发现完整、**全局权威**——客户端 **replace-all**（不在本次 `items` 中的旧 pending 应视为已不在）；**目录数组**（仅成功 directory，first-seen 保序）当 `errors` 非空或发现截断 → **partial**——客户端**仅可**对数组所列 directory 做 replace，**不得**对未列出 directory 做 replace（其既有 pending 必须保留——sidecar 未取得其当前集合，错误 replace-all 会丢失 pending）。
  - `discoveryComplete`：`true` 除非发现页正好填满 `_DISCOVERY_LIMIT`(=10000)（可能截断）；`roots=true` 只返顶层 session（数量 ≈ distinct workdir 数），实际恒 `true`。
  - `truncated`（absent-aware 加性诊断）：`true` 当聚合 `items` 数超 `_MAX_AGGREGATE_ITEMS`(=10000)——后续 directory 不再 extend，`authoritativeDirectories` 同步降级为 succeeded list（与发现截断同语义）；缺省 `false`。
  - 边界：无任何 session（`/experimental/session` 返 `[]`）→ `{items:[], errors:[], authoritativeDirectories:null, discoveryComplete:true}`（**权威空**，replace-all 安全）；发现调用 total failure（网络/5xx/4xx/坏 JSON/非 list）→ HTTP 503 `{"code":"upstream_unavailable"}`（**无 envelope**；客户端保留既有状态并重试）；不读 opencode SQLite（上游 `/question` 是 legacy `:4096` server 挂载路径 per-Location；发现调用 `/experimental/session` 是 opencode v2 全局端点）。

**写路由（17 端点；#1-12 directory 消费——上游 session/question 组均声明 WorkspaceRoutingQuery；#13-17 不消费——上游 v2 session 组按 sid 自路由，`?directory=` 宽容剥离不转发不报错）**：

| # | 路由 | 上游 | 方法 | 备注 |
|---|---|---|---|---|
| 1 | `/slimapi/session` | `/session` | POST | createSession |
| 2 | `/slimapi/session/{id}` | `/session/{id}` | PATCH | **双 shape 透传**：title/metadata/permission（UpdatePayload）与 time.archived——上游校验，sidecar 不区分 |
| 3 | `/slimapi/session/{id}` | `/session/{id}` | DELETE | deleteSession |
| 4 | `/slimapi/session/{id}/prompt_async` | 同名 | POST | PromptPayload 透传 |
| 5 | `/slimapi/session/{id}/abort` | 同名 | POST | abortSession |
| 6 | `/slimapi/session/{id}/summarize` | 同名 | POST | SummarizePayload 透传 |
| 7 | `/slimapi/session/{id}/fork` | 同名 | POST | ForkPayload；**`messageID` 为可选 body JSON 字段**，非 query |
| 8 | `/slimapi/session/{id}/revert` | 同名 | POST | RevertPayload（messageId+partId body） |
| 9 | `/slimapi/session/{id}/permissions/{permissionId}` | 同名 | POST | respondPermission |
| 10 | `/slimapi/question/{requestId}/reply` | 同名 | POST | replyQuestion |
| 11 | `/slimapi/question/{requestId}/reject` | 同名 | POST | rejectQuestion |
| 12 | `/slimapi/session/{id}/command` | 同名 | POST | CommandPayload 透传 |
| 13 | `/slimapi/session/{id}/agent` | `/api/session/{id}/agent` | POST | switchAgent；body `{"agent":"<id>"}` 透传；成功 **204** 无体（错误两级制照旧） |
| 14 | `/slimapi/session/{id}/model` | `/api/session/{id}/model` | POST | switchModel；body `{"model":"<provider/model>"}` 透传；成功 204 |
| 15 | `/slimapi/session/{id}/revert/stage` | `/api/session/{id}/revert/stage` | POST | revert 三段式之 stage；body `{"messageID":…,"files"?:bool}` 透传；成功 **200** `{"data":…}` 逐字（无投影）；与 #8 单步 `/revert` **加性并存**，互不替代 |
| 16 | `/slimapi/session/{id}/revert/clear` | `/api/session/{id}/revert/clear` | POST | 无 payload；成功 204 |
| 17 | `/slimapi/session/{id}/revert/commit` | `/api/session/{id}/revert/commit` | POST | 无 payload；成功 204 |

（修订二激活后 write 组另有三条 POST 等效动作路由（§16.2），见本节末修订块——计数 51→54。）

### §10.2 消息投影基线（`GET /slimapi/messages/{sid}` 骨架缩减与 expandRefs；2026-08-21 正文化转录 [冻结]；修订四 [2026-08-21]：toolcard 投影族——已发版 v4.9.0；修订五 [2026-08-22]：加性 `?since=` 前向差分——**见 §10.3**（本节投影基线语义零改动）——目标 4.11.0）

> 历史演进注记：本小节语义 4.0.0 起原经继承基线条款指向 v3 契约 §4a；F-126 自包含化后就地正文化，语义零改动。

> **修订四 [2026-08-21]**（owner 批准的正式契约修订；随 4.9.0 minor 发布）：**范围** = §10.2 toolcard 投影族五项——① PatchPart `files` 归一化（wire 形状变更，下文第 2 条）；② tool `state.metadata` 增补 `files` compact 投影（第 1 条修订款）；③ metadata aggregate `diffStats` 注入优先级链（第 1 条修订款）；④ compress title 合成（第 1 条修订款）+ `state.output` 省略附 `outputBytes`（第 1 条修订款）+ edit `metadata.diff` 合成注入（第 1 条修订款）；⑤ `part_state_metadata_full` ref 触发条件增补（第 3 条修订款）与 §14.2 expand 加性返回（edit 合成 `files`）。**owner 例外声明（wire 版本不变的破坏性 wire 形状变更）**：PatchPart `files` 归一化改变既有 wire 形状（`string[]` → 对象数组）而 wire 版本维持 4、不 bump 协议大版本——经 owner 批准随 4.9.0 **minor** 发布（D1-r 裁定，`docs/ocmar/plans/2026-08-21-toolcard-server-plan.md` §4c：依据 = 发版规约「major 只跟 wire 协议大版本走；wire 不 bump 的破坏性变更发 minor」（`docs/release.md:37-38/54-55`，4.8.0 收窄先例）+ 双客户端双形状兼容已实证（ocdroid `Part.kt` PartFilesSerializer / oc-webui `PartCards.vue` toFiles）+ 未知严格 `string[]` 消费方风险由 owner 知情接受）。客户端必改点：**严格按 `string[]` 解析 `files` 的消费方需改**（按对象数组/双形状解析）；ocdroid/WebUI 已兼容零必改。例外同步记入 CHANGELOG [4.9.0] Changed。**修订边界**：无新 readiness feature ID（§3.3 全集不变——本修订不经门控、直接修订 §10.2/§14 基线语义）；无新错误码；expand 12 类目有序清单不变（仍无 `part_files_full`，理由更新见 §14.2）；`mode=merged`/`/full` 行为零回归（派生字段仅 skeleton 视图，第 5 条修订款）；表示域投影指纹 bump（skeleton REP_VERSION `skeleton-v1` → `v2`）——同输入下新旧 v4 validator 必然不同，升级后旧 v4 ETag 全部自然失效重拉（4.8.0 后第二次全量轮换，预期内一次性重拉）。

范围：messages 缺省 skeleton 与 `mode=merged` 投影。原则：skeleton 内字段**整字段保留或整字段省略，不做部分截断**；多字节按 UTF-8 编码字节计数。省略字段（除 /full-only 清单）必携带 `expandRefs`。

1. **阈值与省略规则**：
   - `info.summary.diffs`：**总是省略** → `null` + 消息级 `info.expandRefs`（`category:"info_summary_diffs"`）；summary 其余 key 保留；仅省略时刻非 null/非空 list 才生成 ref。
   - `TextPart.text`：**永远全量内联，不折叠、无阈值**（对话正文无论字节数一律呈现；skeleton 与 merged 两模式经断言测试矩阵锁定全量内联——投影层零 text 截断分支、零 `truncated` 语义）。折叠内容按 omitted+expandRefs 模式 1 / 缩略信息模式 2；`part_text` 类目端点保留服务历史响应与降级场景（§14.2）。
   - `ReasoningPart.text`：UTF-8 编码字节 > **2048** → 整字段 `null` + `omitted:["text"]` + `hasFull:true` + part 级 `expandRefs`（`part_reasoning`）；≤ 2048 原样内联。
   - `ToolPart.state.output/error`：阈值 4 KB/字段、16 KB/消息，省略时新增 `expandRefs`（`part_state_output` / `part_state_error`）。
   - `tool state.input`/`metadata`/`attachments`（`object|null` / `object|null` / `object[]|null`）、`file.url`/`source`、`step-start`/`step-finish` 的 `snapshot`、compaction 整体超限：按 §14.2 映射生成 `expandRefs`。
   - **[修订四] `state.output` 省略附 `outputBytes`**：`output` 在场、非 null/空串、且因 per-field cap（4 KB）或 per-message budget（16 KB）省略时，`state` 附加整数键 `outputBytes` = 被省略字段值的 **JSON wire 字节数**（canonical 序列化长度，含 JSON 引号/转义/嵌套结构——与阈值判定同一记账原语，非裸 UTF-8 文本长度）。`error` 省略**不**附 `errorBytes`。合成提示键：不进 `omitted`/`hasFull`、不参与可渲染判定（第 4 条）；`mode=merged` 成功 splice 与 `/full` 视图自然无此键（上游原状）。
   - **[修订四] tool `state.metadata` 增补 `files` compact 投影**：metadata 投影键集增 `files`（源 `state.metadata.files`，如 apply_patch），投影为专用 compact 映射（**非 verbatim**）：源条目（object）→ `{path: relativePath ?? filePath, additions?, deletions?, status?: type}`（`additions`/`deletions` 仅经 `_valid_count` 存活——`isinstance(int)` 且非 bool 且 ≥0；`status` 取源 `type` 键）；**剔除** `patch`（diff 正文）与 `filePath`/`relativePath`/`type` 原键；非 object 条目跳过。**cap 10 条**（同 PatchPart，第 2 条）：有效映射条目超 10 → compact 列表截为前 10，附 `metadata.filesTotal` = **源数组长度（源计数，含无效条目——非可展示文件数）**。compact 投影恒为有损投影 → `part_state_metadata_full` ref 触发条件增补（第 3 条修订款）。
   - **[修订四] metadata aggregate `diffStats` 注入优先级链（冻结）**：tool part `state.metadata` 注入 `diffStats`（`{additions, deletions, files}`，thresholding 后注入、永不 omit）按全链判定、先命中先停：⓪ 源 `metadata.diffStats` 在场且合法（object 且三子键均 `_valid_count`）→ **保留源值、跳过一切派生**（源值权威，派生永不覆盖）；① `metadata.filediff` 结构化有效 → 由 filediff 合成（异常安全化：逐条目 `_valid_count`、非法值计 0、非 object 条目跳过、**永不抛异常**；非空但全畸形 → 视为无效落兜底）；② `metadata.files` 有效（≥1 条目携带 `_valid_count` 的 additions/deletions）→ 由 files 合成；③ `tool == "edit"` 且 `metadata.diff` 可解析（下条 B2）→ 解析器统计聚合；④ 均不可得 → 不注入。`files` 计数 = 有效映射条目数（非源数组长度）。
   - **[修订四] edit `metadata.diff` 合成注入（B2）**：`tool == "edit"` 且 `metadata.diff` 为可解析 unified diff 文本且 `metadata.truncated` 非 true → ① 注入 `metadata.diffStats`（解析器统计，仅当上述链 ⓪①② 均未命中）；② 注入 `metadata.files` **合成投影**（`{path, additions, deletions}`——无 `status`，diff 文本无类型信息），走上一款 compact 投影同一 cap 10 + `filesTotal` + ref 保活管线。解析器 = 单遍线性状态机（文件段识别：`Index: <path>` 优先、否则配对 `+++ `/`--- ` 头（剥 `a/`/`b/` 前缀）；`+++ /dev/null` 删除文件（路径取 `--- a/<path>`）、`--- /dev/null` 新增文件（路径取 `+++ b/<path>`）；**孤立 `Index:` 不构成有效文件段**（零 hunk 文件段须具备成对头——rename-only 同理）；hunk 退出双机制 = 行数耗尽（`@@ -l,s +l,s @@` 计数）+ 边界转移（遇 `Index:`/配对头/`diff --git` 按已见行收尾，宁少计不误归属）；计数仅 in_hunk 状态行首 `+`/`-`；`\ No newline` 忽略；零 hunk 文件段（成对头在场）计入 ±0；无任何有效文件段 → 整体不注入、**不伪造**）。`state.metadata.diff` 本身维持省略（白名单外，omitted 不变）。
   - **[修订四] compress title 合成**：`tool == "compress"` 且投影 `state` 无 `title`（缺席/null/空串）时合成折叠卡标题。求值顺序（冻结）：① `state.input` 为 object，否则放弃（不对非 object 求 `.get()`）；② `input.content` 为非空数组，否则放弃；③ `content[0]` 为 object，否则放弃（不尝试后续元素）；④ 此后 `title = clip(topic, 160) ?? clip(summary, 160) ?? "压缩 {len(content)} 段"`。`clip(s, n)`：仅 str；先 strip、空白串视为缺失；按**字符**截断 n、不附省略号；非 str 视为缺失。任一前置不满足 → 保持无 title（无兜底文案——段数 fallback 仅在 ①-③ 全过后 topic/summary 均缺失时使用）。`content` 键**不进** input 投影白名单（维持 omitted + `part_state_input_full` ref——展开全量路径零变化）。
   - **/full-only**（省略但不生成 refs，显式穷举）：`state.structured/result/raw`、tool input 非白名单单个 key、step-finish `reason/cost/tokens`、reasoning `metadata/time`、text `synthetic/ignored/time`、未知上游字段、`omitted:["*"]`（compaction 超限除外）、`thin_placeholder` 的 `omitted:["parts"]`。
2. **PatchPart**（**[修订四] 归一化投影——wire 形状变更，owner 例外见修订头**）：`files` 投影为**归一化对象数组**——源 `string` 条目 → `{"path": <s>}`；源 object 条目（legacy 形态）→ 既有四键投影 `{path, additions, deletions, status}`；非 string/object 条目跳过。**cap 10 条**：有效映射条目超 10 → compact 列表截为前 10，附 part 顶层整数 `filesTotal` = **源数组长度（源计数，含无效条目——非可展示文件数）**；未超限不附。`hash` 照旧保留。不生成 refs（`files` 无 expand 类目——P0-2 patch expand ref 搁置：生产 patch part 0/23,827 带 state；重启条件 = 上游为 PatchPart 引入 state 或生产出现带 state 的 patch part）。**part 级 `diffStats` 防伪造守卫（冻结）**：仅当源条目中**至少一条携带 ≥1 个 `_valid_count` 的 `additions`/`deletions`** 才注入（校验器 = `isinstance(int)` 且非 bool 且 ≥0——int-only，拒 float（含 `1.0`/`inf`/`nan`）/bool/负数；JSON 大整数原生支持）；注入时求和逐条目 `_valid_count` 值计入、非法值计 0，`files` 计数 = 有效映射条目数（非源数组长度）；**归一化 string 派生条目（仅 `{"path"}`）永不注入**（无计数来源，不伪造）。异常输入一律降级为跳过/不注入，绝不让消息列表 5xx。历史（≤4.8.0）：`files` 为上游 `string[]` **原样保留 verbatim**（+ 保留 `hash`）——不省略、不生成 refs、不做 dict 数组假设；该形状已被本修订取代（owner 例外，随 4.9.0 minor 发布）。
3. **`expandRefs` 为 sidecar 拥有键**：上游同名键**一律剥离/确定性替换**（skeleton 深拷贝时丢弃上游值后按本映射重建），永不透传、永不进 `omitted`。元素形状 `{category, messageID, partID?, href}`（数组）；去重：每 `(category, messageID, partID?)` 至多 1 条（tool input 多 key 省略 → 仅 1 条 `part_state_input_full`）；排序：category 字典序 + 同 category 内 partID 字典序（确定性）；`href` 含 selector 值（§14），directory 由客户端追加；空/null 上游字段与未知字段不生成。基数上界：每 part 理论 ≤ 5（tool part 可同时省略 input/metadata/attachments/output/error）。**[修订四] `part_state_metadata_full` 触发条件增补**：既有条件（metadata 白名单外键被省略）**OR 源 `state.metadata.files` 为非空数组**（compact 投影恒有损——剔 `patch`/`filePath`/`relativePath`/`type`，即使 1 个文件 ref 也必须在场）；**判定基于源值而非映射结果**——源 files 非空但全部条目畸形 → 映射结果为空列表，ref 仍在场（源信息被丢弃是更强的展开理由）。
4. **可渲染性**：skeleton 模式 `text:null` 但携带 `expandRefs` 的 part **计为可渲染**（part 骨架 + 展开入口，非整页 placeholder）；消息级省略（diffs）不参与可渲染判定；`thin_placeholder` 语义不变（无任何可渲染 part 时注入）。
5. **merged 语义（best-effort，显式不承诺 null-free）**：候选集 = 占位消息 ∪ 任一 part 携带 `expandRefs` 的消息（消息级 `info.expandRefs` **不进入候选**——diffs 永不 batch 恢复）；**placeholder-first** 优先级（占位消息按页面顺序优先占用预算，行为不被 ref 候选挤占）；ref 候选仅在剩余预算内按页面顺序 best-effort 还原；**交集去重**：同一消息同时属于两类时按 mid 去重——占位身份优先、只占 1 个 slot、只发起 1 次 full fetch；还原范围与现有 merge 相同（仅替换 `parts`），`info.summary.diffs` 在 merged 输出**恒 `null` + expandRefs**；预算耗尽/源超限/上游失败/畸形 body → 该项保留 skeleton（含 `null` text + expandRefs），客户端有展开入口兜底。**[修订四] 派生字段限界**：修订四全部派生字段（compress 合成 title、`outputBytes`、metadata `files` compact 投影、`diffStats`/合成注入）仅 **skeleton 视图**保证——`mode=merged` 成功 splice 后 parts 被上游 full 投影原样替换，派生字段回到上游原状（维持既有行为，无回归）；预算耗尽/失败保留 skeleton 的消息其派生字段在场。
6. **`contentFingerprint`**：每条消息 skeleton 加性字段 `contentFingerprint: string`，格式 `"<vN>:<sha256hex>"`（`vN` = 指纹方案版本前缀；SHA-256 全量 hex 不截断）。生成位置：缺省列表 = skeleton 投影完成时；`mode=merged` = placeholder splice 完成后**重算覆盖**（full 抓取失败/预算不足/坏响应等降级路径**不重算**，保留 skeleton 期指纹）。规范化输入 = 排除 `contentFingerprint` 自身后的消息投影 dict，`sort_keys` 序列化（parts 保持上游序）后取 SHA-256——指纹只覆盖 sidecar 投影后的表示，不含被丢弃字段。终态语义：同输入（同投影、同规范化、同 `vN`）恒同指纹（确定性，跨进程重启成立）；**不提供单调性**（不是序号/revision，客户端不得据此排序或版本比较）；**跨表示模式不可比较**（缺省与 merged 对同一消息产生不同指纹，`vN` 前缀不区分模式——仅在同一表示模式内比较）；指纹是 `(updatedAt, messageId)` 双水位去重的补充证据，digest 帧不携带指纹。`OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED=false` → 省略该字段；该开关状态参与 validator 版本输入（关闭 → 全部 ETag validator 轮换）。**[修订四]**：修订四派生字段自然进入指纹规范化输入（投影后表示的一部分，无 `vN` 改动）；skeleton 表示域 validator 随 REP_VERSION `skeleton-v1` → `skeleton-v2` 轮换（修订头）——升级后旧 v4 ETag/304 缓存全部自然失效重拉。

**2026-08-19 正式修订追加差异面 [冻结目标——逐项当对应 feature ID 进入 `satisfied`（§3.3 门控）时生效；生效前上述发布态注载继续成立。下表各行「`?v=3` 恒不变」条款 = 4.0.0–4.7.0 历史 v3 面语义基线——v4-only 窗（§0.1）下 `?v=3` 已 400，不构成现行可达行为]**：

| 路由 / 面 | 修订差异（仅 `?v=4`） |
|---|---|
| `GET /slimapi/config/providers` | §12 安全投影：白名单 schema + 嵌套递归丢弃 + 四限额 + 三带错误面 + ETag/canonical 口径；`?v=3` 恒透传不变 |
| `GET /slimapi/session/{sid}` | §13 单查 parity：升级为与列表同源 canonical SessionSkeleton 形状（dbaux 点查优先 + whole-response native fallback）；`?v=3` 恒 v3 skeleton |
| messages 投影 expandRefs href + 2 条 expand 路由能力 | §14：href 按 wire 视图生成（`?v=4` 响应 → `?v=4`，`?v=3` → `?v=3`）；`capabilities["4"]` 增 `expand` 键 |
| `GET /slimapi/versions` | §3.3：`capabilities["4"]` 增 `readiness`（+随批 `expand`）扩展键 |
| v4 表示层 | §15：v4 sessions 列表增 ETag（identity 强 / gzip 弱 `W/`）；全 v4 路由 `Vary: Accept-Encoding` 修正（现行 `_v4_json_response` 删 Vary 为已知 bug） |
| 三条 deferred method-path 组合 | §16：`?v=4` 精确 405 `method_not_applicable`（`Allow` 头 + coded body + 不转发 + no-store）——**修订二后为过渡态行为**（见下行，`session.post-actions.v4` 激活后 405 面让位） |
| `POST /slimapi/session/{sid}`（新） | §16 修订二：`session.post-actions.v4 ∈ satisfied` 时激活为 **PATCH 等效路由**（同一 PatchPayload 透传、逐字节等效受控写管线）；`?v=3` → 404 `thin_route_not_found`（4.0.0–4.7.0 历史，v4-only 窗下不可达） |
| `POST /slimapi/session/{sid}/archive`（新） | §16 修订二：同门控激活；body 可选（合法 PATCH body 透传 / 缺省 sidecar 合成 `{"time":{"archived":<now epoch-ms>}}` 走 PATCH 等效管线）；`?v=3` → 404（4.0.0–4.7.0 历史，v4-only 窗下不可达） |
| `POST /slimapi/session/{sid}/delete`（新） | §16 修订二：同门控激活；**DELETE 等效路由**（请求实体处理与 DELETE 完全相同并原样转发——读取实体、同 cap 413、Content-Type 透传、body 逐字节转发，无忽略分支，§16.2-b；上游递归删子+吞错语义如实继承，非幂等可接受——owner q1）；`?v=3` → 404（4.0.0–4.7.0 历史，v4-only 窗下不可达） |

其余路由维持零 v4 差异；**计数方法（路由 × 方法表行）不变——4.2.0 已实现 51 条，修订二实施后 54 条（write 20），修订五实施后 55 条（`GET /slimapi/file/raw` 加性，§19）**。

### §10.3 messages `?since=` 前向差分（修订五 [P1]；4.11.0）

> **范围**：`GET /slimapi/messages/{sid}` 列表端点加性参数 `since`（`mode=skeleton|merged` 均适用；`?v=4` only）。设计稿 `docs/specs/plan-4.11.0-p1-p6.md` §3（六轮评审 9.4 PASS + v6.1 编排者裁定）。

**envelope 形状（加性键序冻结）**：`{"items":[…],"nextCursor":<string|null>[,"removed":[mid…]][,"nextSince":<string>]}`——`removed`/`nextSince` 仅 `since` 请求且非 reset 时按条件出现（reset 响应 = 全量 `items` + 新 `nextSince`，无 `removed`；无 `since` 请求的响应形状与 4.10.x 逐字节一致，仅可能多 `nextSince` 键；带 `before` 的响应**恒无** `nextSince`/`removed`）。差分响应 `items` = **changed 投影数组**（新抓取窗口内 fingerprint 变化/新增的消息，保持窗口序）。

**token（`nextSince` 值）**：`base64url({v:1, epoch, sid, cq_hash, gen})`——`epoch` = 进程启动随机 nonce（**token 属进程域，跨进程/重启比较无意义**）；`cq_hash` = 规范化查询指纹 `v1:{limit 归一化（omitted≡默认 40 同值）}:{directory 归一化（effective 验证后值；None→""）}:{mode 归一化（非 "merged"→"baseline"；"merged"→"merged"）}`；`gen` = 进程级单调计数（首次安装与内容不同的成功替换递增；byte-identical 复用不递增）。

**token 解析分类（v6.1 编排者裁定，冻结）**：
- **400 `invalid_params`**（真错误组）：语法损坏/非 JSON 对象/版本不支持/`sid` 失配/token 超长（>512B）/**`since`+`before` 同现/since 或 before 重复多值**。
- **reset**（安全全量组；语义 = 返回全量投影 + 按 CAS 规则签发新 `nextSince`，**非幂等、重试安全**）：格式合法但 `gen` 过期/`epoch` 失配（含跨进程）/cache miss/LRU 逐出/oversized bypass 后旧 token/**`cq_hash` 失配（limit/directory/mode 查询轴变化）**。判定基准 = 实现分类器（`since_cache.py::TokenKind`）；**无显式 reset 标记键**——客户端以「`items` 为全量（无基线差分）+ 新 `nextSince`」隐式识别，与 400 的区分由状态码承载。

**差分算法（冻结）**：
```
window_exhausted = (before 缺席) and (next_cursor is None)   # nextCursor 权威：上游 Link 解析结果
changed = [i for i in fresh if mid ∉ baseline.fingerprints or 指纹(mid) 变化]
removed = [mid for mid in baseline.fingerprints if mid ∉ fresh_mids
           and (window_exhausted or boundary_newer(mid, fresh_oldest))]
boundary_newer(mid, oldest): oldest is None → False（非穷尽空投影不推断任何 removal）
                            (mid.time_created, mid.id) > (oldest.time_created, oldest.id) 严格比较
                            == 或 < → False（同时间戳并列防御性不报——宁可漏报 removed 走全量对账，不误报）
```
指纹 = SHA-256（消息 canonical 投影字节）；**穷尽权威 = `nextCursor is None`**（截断窗口滚出与新删除并存不可区分 → 保守不报，客户端走全量对账）。**盲区声明**：本通道为 best-effort 前向差分，漏报（保守分支）由 `digest messagesRevision`（§7.5）+ 周期性全量 If-None-Match 对账兜底（P4 对账兜底）；**removed 不得出现假阳性**（契约级不变量）。

**CAS 并发语义（冻结）**：并发携带相同 token 的差分请求，完成时按三分支收敛——①基线不存在（首见）→ 安装新 gen + 签发；②CAS 成功（`current_gen == observed_gen`）→ fresh 与缓存 byte-identical → 复用 gen（稳定，不轮换 validator 域）/ 不同 → 替换 + 新 gen；③**CAS loser（并发竞争降级）**→ byte-identical → 复用当前 gen + 照常签发 / **differing → 丢弃写入 + `nextSince` omit（键缺席）**——客户端见 `nextSince` 缺席即「并发竞争降级」，下次重试（旧 token 已失效将走 reset）或直接全量。「读取 current→比较→替换/复用」为无 await 同步原子操作（单事件循环安全序）。

**缓存资源域（实现边界，可观察承诺冻结）**：进程内 LRU（key=(sid, cq_hash)，字节预算默认 64MiB/256 entries/单项 1MiB，env 可调）；超预算逐出/bypass 后旧 token → reset。**token 不跨进程存活**——客户端在任何 503/重启后应预期 reset。singleflight 键**不含 since**（全量与差分请求共享上游拉取）。

**ETag 交互**：差分 envelope 与全量 envelope 各自按 §6 全规则计算 validator（canonical 输入 = **最终 envelope body**——差分响应与全量响应 validator 天然不同域，不混用）；304 语义不变。

## §11 测试矩阵（B0 冻结用例面；落地批次标注）

| # | 面 | 内容 | 落地 |
|---|---|---|---|
| 11.1 | 跨版本 | §2 状态表 × §8.3 真值表逐组合；v3 全回归逐字节不变 | B3a-A |
| 11.2 | selector 分叉 | v4 sessions × directory 四形态 → 单一错误码；v4 非 sessions-list × directory → 正常消费 | B3a-A |
| 11.3 | 降级矩阵 | **72 等价类 × cursor 2 态 = 144 case 逐格**（状态码/degraded/Retry-After/错误体负向断言：不含 DB 路径/schema 字样/白名单内容）；search 等价轴（含通配字符 → 503）入格 | B3a-B4 |
| 11.4 | cursor | 编解码/指纹矩阵（参数变更→400）/边界/畸形/确定性（同输入两次 hash 相同） | B3a-B3 |
| 11.5 | SQL 语义 | search 转义矩阵 4 + 降级等价轴 2（含通配字符×db不可用→503 / 纯子串×ClassA→200+degraded）/ allowlist 二进制前缀边界 3 + 3（大小写差异不匹配 / 根 `/` 全匹配 / 路径段含 `%`/`_` 字面）/ complete 边界 2 / legacy 空 directory 2 / 键集下界 1 / 指纹确定性 2（~19 case） | B3a-B2 |
| 11.6 | DB 生命周期 | schema 门/熔断（P99 滑动窗+最小样本+warmup+hysteresis）/inode swap/路径解析 ~10 case/并发阻断（线程亲和 R4 断言：worker 外访问被 check_same_thread 拒绝=期望性质） | B3a-B1 |
| 11.7 | WAL 陈旧读 | ro-vs-immutable 3 case（已进 CI：`tests/test_wal_staleness.py`） | **B0 已落地** |
| 11.8 | 等价性锚定 | DB 投影 ≡ 权威源（真实 opencode 进程 / 版本标记 golden，S-B03 禁 mock 自证）× {行集/字段语义/排序/complete} | B3a-B2（设计定稿见 design-v4-dbaux §10 / design-v4-equivalence-anchor） |
| 11.9 | EQP 全矩阵 | 48 组合 planner 特征断言（SCAN/SEARCH、TEMP B-TREE、行数；非全文案） | B3a-B2（脚本 `scripts/eqp_matrix.py` **B0 已落地**） |
| 11.10 | SSE 重放 | 重放/缺口/过期/重启 epoch/背压/重连/tokens=1 400/ID 无倒退断言/**上游断连 barrier（边界三连+token 离线变体）/组合输入优先级**（协议矩阵用例表 18 条 REPLAY 见 design-v4-sse-replay.md §4） | B3b |
| 11.11 | DB schema 变更兼容 / 运行中迁移 | 上游升版列变更 → 门失败降级；运行中 inode swap | B3a-B1/B6 |
| 11.12 | 冷启动 | P99 warmup 豁免；首查延迟 | B3a-B1 |

## §12 Provider 安全投影（`GET /slimapi/config/providers?v=4`）[2026-08-19 修订冻结；修订三 [2026-08-20]：ModelEntry 恢复 optional `limit`——已发版 v4.4.0]

> **当前状态**：现行该路由（v4-only 唯一可达视图 `?v=4`）= legacy provider map 受控代理 + 既有 ETag（§10 发布态「零 v4 差异」读组；历史 4.0.0–4.7.0 双版本期 `?v=3`/`?v=4` 行为相同）。本节为 `?v=4` 冻结目标——当 feature `providers.redacted.v4` 进入 `satisfied`（§3.3 门控）时生效；`?v=3` 恒透传不变（v3 冻结——4.0.0–4.7.0 历史语义，v4-only 窗下 `?v=3` 已 400）。

> **修订三 [2026-08-20]**（owner 批准的正式契约修订；消费方 oc-webui 已确认嵌套形状）：§12.1 ModelEntry 恢复 optional `limit`（子键白名单恰好 `{context, input, output}`，逐子键 int-else-omit）。**动因**：`limit` 是模型规格参数（上下文窗口 / input / output 上限）**非敏感信息**——上游 schema（opencode v1.18.18 `packages/schema/src/model.ts:81-85`）本就携带；v3 raw 透传含 limit，v4 投影丢失使 oc-webui 上下文百分比失去分母。**纯加性 schema 演进**：providers.redacted.v4 面内（无新 readiness feature ID，§3.3 全集不变）、无新 malformed 错误路径（§12.5.3 错误表零增量）、§12.4 四项限额不变（limit 增量约 48B/model 由既有 `projected_body_bytes` 自然覆盖）；表示域投影指纹 bump（§12.6 REP_VERSION `providers-projection-v1` → `v2`），升级后旧 v4 ETag 自然失效重拉，v3 校验器域不受影响。

同路径投影（无 `/safe` 新路径、无负向黑名单）。数据源 = 上游 `ConfigProvidersResult` `{providers: Info[], default: DefaultModelIDs}`（native HTTP）。

### §12.1 wire schema 与字段策略

```ts
type ProvidersProjection = {
  providers: ProviderEntry[]        // 顶层恰好两 key（providers + default）
  default: Record<string, string>   // providerID → modelID
}
type ProviderEntry = {
  id: string                        // required
  name: string                      // required
  source?: string                   // optional：上游值为 string 时逐字透传；absent/null/非 string → 省略键（不报错）
  models: ModelEntry[]              // required
}
type ModelEntry = {
  id: string                        // required；必须 == 上游 Info.models 的 map key
  name: string                      // required
  providerID: string                // required；必须 == 所属 provider.id
  status?: string                   // optional：上游值为 string 时逐字透传；absent/null/非 string → 省略键（不报错）
  variants?: string[]               // optional：上游存在 variants 时必须为 map（否则 malformed）；值为 map key 的排序数组；空 map → []
  // [2026-08-20 修订三] optional。上游 schema（opencode v1.18.18
  // packages/schema/src/model.ts:81-85）为 limit: {context: Int, input?: Int,
  // output: Int}——context/output 在上游为必填。但那是上游的校验语义；投影层
  // 三子键一律 optional：投影是「省略策略」不是「校验策略」——上游子键缺席/
  // 错型一律静默省略该子键，绝不报错、绝不补默认值。模型规格参数非敏感信息。
  limit?: {
    context?: number                // 子键白名单恰好 {context, input, output}；逐子键独立 int-else-omit（bool 非法）
    input?: number
    output?: number
  }
}
```

- **顶层恰好两 key**：`providers` + `default`；**多余/缺失顶层 key = malformed**（不猜测、不部分转换）。
- **嵌套规则（冻结）**："Unknown provider/model fields are discarded recursively"——provider/model 内未知字段**递归丢弃**（丢弃不报错）；仅顶层受 exact-two-key 约束。
- **确定性丢弃清单**（上游存在但不进投影）：`Info.env`/`Info.key`/`Info.options`；model 的 `api`/`capabilities`/`cost`/`options`/`headers`/`release_date`（`limit` 曾列于此，修订三恢复进投影——见下条）；variant 内除 map key 外一切（`name`/`status` 等不进 wire）。
- **optional 字段策略（冻结）**：`source`/`status` 为 absent / null / 非 string → **省略该字段**（不报错、不猜测、无发明枚举）——任何响应绝无 `"source": null`/`"status": null`；`variants` **absent → 省略键**，**存在则必须为 map（object）**，非 map = malformed（§12.5.3）。**字段策略维度的 malformed 全部来源 = required 字段（provider `id`/`name`/`models`，model `id`/`name`/`providerID`）缺失或错型，加上唯一一条 optional-key 错误路径：`variants` 存在但非 map**——除上述来源外，`source`/`status` 的任何上游形态都不产生错误（一律省略该键）；同一输入在合规实现间**唯一结果**。
- **`limit` optional 省略策略（修订三 [2026-08-20]，冻结）**：上游 `limit` 键 absent / null / 非 object（str/num/array/bool）→ **省略整键，不报错**（任何响应绝无 `"limit": null`）；子键白名单**恰好 {`context`, `input`, `output`}**，逐子键独立 **int-else-omit**——子键存在且值为 int（**显式排除 bool**）→ 逐字透传该子键；缺席/null/非 int（含 float/bool/str）→ 省略该子键；逐子键投影后**零子键存活 → 省略整个 limit 键**（任何响应绝无 `"limit": {}`）；未知子键（如 `limit.reasoning`）丢弃不报错（与「递归丢弃未知字段」一致）。**「int」判定 = 冻结 canonical 算法（orjson）可序列化整数范围 `[-2^63, 2^64-1]`（2026-08-20 实证边界：`2^64`/`-(2^63)-1` 抛 orjson 超界错误；实现以模块内实测边界常量为准）——超界整数值 → 按非 int 处理省略该子键，仍零错误路径，绝不落入 `provider_upstream_malformed`**。`limit` 的**任何上游形态都不产生错误**——上款「字段策略维度的 malformed 全部来源」穷尽句**不因修订三增加任何来源**（§12.5.3 错误表零增量）。
- **`variants` absent ⇔ 上游无 variants map**；存在时 = 该 map 全部 key 的排序数组（含空 map → `[]`）。
- `Info.models` 的 **map key == 发出的 `Model.id`**，不一致 = malformed；发出的每个 model `providerID` == 容器 provider 的 `id`，违反 = malformed。

### §12.2 排序与唯一性

| 层级 | 排序 / 唯一性 |
|---|---|
| `providers` 数组 | 按 `id` **UTF-8 字节序**升序；`id` 全局唯一，重复 = malformed |
| `models` 数组 | 按上游 `Info.models` **map key 序**（即 `Model.id` 字节序）；`id` 在 provider 内唯一 |
| `variants` 数组 | 按 map key **字节序**升序；variant ID 在 model 内唯一 |
| `default` map | key 按 **UTF-8 字节序**（canonical 序列化统一稳定键序——`orjson.dumps(OPT_SORT_KEYS)`，与 providers/models/variants 同规则；§12.6） |

排序 + 确定性键集使投影体成为**规范化 body**（同一上游输入 → 逐字节相同输出）。

### §12.3 `default` 解析校验（逐 key 三重校验，任一失败 = malformed）

1. key 命中已发出的 provider（`providers[].id` 存在）；
2. value 命中该 provider `models[]` 内的 `Model.id`（model 必须经**该 provider** 的 `Info.models` 解析）；
3. 该 model 的 `providerID` == 此 key（跨层一致）。

无全局默认 provider/variant/name 语义；value 为 `null` 一律非法（map 值必须为 string）。

### §12.4 四项数值限额（wire 常量，冻结）

| # | 限额 | 值 | rationale（锚点） |
|---|---|---|---|
| 1 | provider 数上限 | **256** | 上游现实量级：models.dev 全量目录 ≈ 低百量级 provider；256 = 2^8 头寸，沿用本仓 config.py 的 2 的幂上限惯例 |
| 2 | 单 provider model 数上限 | **1024** | models.dev 最大 provider 家族（google/openai 含别名与分代）为 O(10²)；1024 ≈ 10× 余量的 2 的幂 |
| 3 | 单 model variant 数上限 | **64** | 上游 `Model.variants` 实测 ≤ 个位数分层（default/thinking/mini 等）；64 = 8× 余量 |
| 4 | 投影后 body 字节上限 | **8388608**（8 MiB） | 对齐 `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 缺省 8 MiB（config.py 唯一既有 wire 可见单响应片段上限的量级）；口径 = redacted canonical **identity** 序列化字节（gzip 前） |

- 四项为**固定 wire 常量**（无 env 覆写——广告值与实际行为不得漂移），是相互独立的 fail-closed tripwire：**最先生效者触发错误，无静默截断**（不砍数组、不丢 provider、不留 hint 截断标记）。计数上限的算术最坏组合（256×1024）可超过 body 上限——不矛盾：四项独立判定，先触发者生效。
- **与源上限判然两事**：源 body 超限走既有 admission 语义（`max_response_bytes` 缺省 64 MiB，解析**前**检查 → 既有 413 `response_too_large`）；投影限额在投影后检查（§12.5.2 ⑧/⑩）。

### §12.5 错误契约（状态映射 + 完整求值序 + 逐类目，冻结）

#### §12.5.1 上游 HTTP 状态映射

| 上游状态 | 处理 |
|---|---|
| `200` | 进入解析（§12.5.2 ⑥） |
| 非 200 的 2xx（含 204 无 body） | **502 `provider_upstream_malformed`**——非 `ConfigProvidersResult` 形态，确定性形状违约 |
| 3xx | **502 `upstream_http_<N>`**（httpx `follow_redirects=False` 既有行为：不跟随、作为终态到达；转换路由不透传不跟随） |
| 4xx（drain 成功） | **502 `upstream_http_<N>`**（controlled-proxy 转换路由惯例——**明确不逐字透传**：本路由为转换端点，sidecar 拥有错误面，区别于 raw 代理路由） |
| 5xx / 网络/发送失败 / 4xx drain 失败 / **错误 body 读取超 cap** | **503 `upstream_unavailable`**（资源保护优先于状态映射）。错误 body 读取 cap = 既有 `max_response_bytes`（`OC_SLIMAPI_MAX_RESPONSE_BYTES` 运行时值，与源 body cap **同键同名继承**，无独立错误体上限）；边界口径 **`>`**（严格大于才超限，恰等于上限合法），与既有 `read_with_cap` 一致 |

#### §12.5.2 完整求值序（可执行结构 + offload 边界；复合失败按此序产生唯一结果）

```
[main context — 事件循环]
① selector（400 族，§2）
② directory 消费/校验（400 族——providers 路由为 §10.1 读组消费集成员
   （providers=WorkspaceRoutingQuery），按 §5.1 基线消费矩阵）
③ 上游 fetch + HTTP 状态映射（§12.5.1）——异步网络 I/O
                                            → 502 upstream_http_<N> / 502 provider_upstream_malformed /
                                               503 upstream_unavailable
④ 源 body 字节 cap（解析前，read_with_cap 口径）
                                            → 413 response_too_large
⑤ transform permit 获取（permit 语义 = worker/CPU 配额占位；获取时机 = body cap
   检查通过之后、worker job 提交之前——上游网络等待期不持有 permit）
                                            → 503 transform_busy（Retry-After；仅可能发生在本提交点）
═══ ⑥-⑪ = 一个 worker job，整体提交 transform executor 依序执行 ═══
⑥ JSON 解码（重复 JSON member name 一律拒绝）
                                            → 502 provider_upstream_malformed
⑦ schema/关系校验（provider 序遍历：顶层恰好两 key → 数组条目/类型 → required →
   Info.models map-key == Model.id → 嵌套 providerID 一致 → default 逐 key 三重校验 §12.3）
                                            → 502 provider_upstream_malformed
⑧ 投影 + 计数限额（providers → 每 provider models → 每 model variants，同序遍历；
   先触发者生效并报该 limit）                → 413 provider_projection_limit
⑨ canonical 序列化（§12.6 口径）
⑩ 投影后 body 字节限额                      → 413 provider_projection_limit（limit="projected_body_bytes"）
⑪ gzip 协商判定 + 压缩（Accept-Encoding 解析与 gzip 压缩均在本 worker job 内：
   协商接受 gzip → 对 canonical identity bytes 压缩产出 gzip 表示（含 §12.6 weak
   validator）；协商拒绝 → 仅产出强 validator。压缩为 CPU 工作，绝不回 event loop）
═══ worker job 结束，permit 释放 ═══
[回主上下文]
⑫ conditional 判定（If-None-Match/304，基于 worker 产出的 validator）+ 响应发射
   （identity/gzip 字节与 validator 均由 worker 产出——主上下文零序列化、零压缩）
```

**offload 边界（冻结）**：⑥-⑪ 的**全部 CPU 工作**（JSON 解码、重复 member 检查、schema 校验、投影、限额、canonical 序列化、body 限额、**gzip 协商判定与压缩**）**只在该 transform worker job 内执行，绝不在 event loop 上执行**（既有受控代理 offload 纪律延续，§10.1；不存在「8 MiB body 在 event loop 压缩」的合法路径）。③-④ 为异步上游 I/O + 流式字节计数，留在主上下文——**网络等待不占 transform 配额**。⑤ = **transform permit 获取**：permit 语义 = worker/CPU 配额（门的是 ⑥-⑪ 的 CPU 工作，不是上游 I/O）；获取时机 = body cap 检查通过之后、worker job 提交之前——**permit 持有期 = worker job 生命周期**（job 结束即释放），网络等待期不持有。因此 **503 `transform_busy` 仅可能发生在 ⑤ 提交点**（数据级错误 ③/④ 先于 permit 判定；这与 §14.3 求值序 ③「准入先于上游工作」的语义差异是本路由的显式设计——上游 I/O 不消耗 CPU 配额，先做完 I/O 与 cap 检查再竞争 permit）。⑫ 回主上下文仅做 conditional 判定与响应发射。

#### §12.5.3 错误表（逐类目）

| 类目 | 求值步 | HTTP | code（snake_case） | body | cache |
|---|---|---|---|---|---|
| 源 body 超 `max_response_bytes`（解析前） | ④ | 413 | `response_too_large`（**复用**既有） | `{"code":"response_too_large","limitBytes":<max_response_bytes>}` | `no-store` |
| JSON 解码失败 / **重复 JSON member name**（fail-closed，不依赖解析器 last-wins 任意性）/ 顶层非恰好两 key 对象 / 数组含非对象条目或混合 / **required 缺失或错型** / `variants` 存在但非 map / 重复 provider ID / map-key ≠ `Model.id` / 嵌套 `providerID` ≠ 容器 / default 逐 key 校验失败（§12.3）/ 非 200 2xx（含 204） | ③/⑥/⑦ | **502** | `provider_upstream_malformed`（**新**） | `{"code":"provider_upstream_malformed"}` | `no-store` |
| 上游 3xx / 4xx（drain 成功；httpx 不跟随重定向） | ③ | 502 | `upstream_http_<N>`（**复用**，N=上游状态） | `{"code":"upstream_http_<N>"}` | `no-store` |
| 上游 5xx / 网络/发送失败 / 4xx drain 失败 / 错误 body 读取超 cap（cap = `max_response_bytes`，口径 `>`） | ③ | 503 | `upstream_unavailable`（**复用**） | 既有体 | `no-store` |
| transform permit 耗尽（worker/CPU 配额；获取于 body cap 检查后、worker job 提交前——§12.5.2 ⑤） | ⑤ | 503 | `transform_busy`（**复用**） | 既有体 + `Retry-After` | `no-store` |
| 四项投影限额任一超限（§12.4；⑧ 计数三项 / ⑩ 字节一项） | ⑧/⑩ | **413** | `provider_projection_limit`（**新**） | `{"code":"provider_projection_limit","limit":"providers"\|"models_per_provider"\|"variants_per_model"\|"projected_body_bytes","limitValue":<N>}` | `no-store` |

**三带语义明确区分（冻结）**：**502 带** = 上游数据形状违约或不可转换的非 2xx 终态——确定性、归因上游、客户端不可纠正（区别于 503 瞬态可重试）；**413 带** = 超限——确定性，源限复用既有 admission 语义、投影限为本路由新码；**503 带** = 瞬态/客户端无关——`Retry-After` 语义沿用既有码，上游错误 body 读取超 cap 亦归此档（资源保护优先于逐字义务）。**422 不适用于本路由**：GET 无客户端可控载荷可触发语义错误；selector/directory 层既有 400 族先行（§8.3/§8.4 优先级链）；显式不引入新 422 码，避免与「参数版本不匹配」422 语义（§4.1）混淆。

**错误体纪律**：502 体**不携带**上游内容、字段路径或枚举细节（不泄露，对齐 `auxiliary_unavailable` 纪律）；`limit`/`limitValue` 为公开 wire 常量可安全回显。全部错误响应 `Cache-Control: no-store`。

### §12.6 ETag / Vary / 缓存

- **canonical body（canonical 字节冻结）**：canonical 字节 = `orjson.dumps(value, option=orjson.OPT_SORT_KEYS)` 的逐字节输出——UTF-8 直出非 ASCII、不转义 `/`、控制字符转义按 orjson 实测形态（`\n`/`\t`/`\b`/`\f`/`\r` 五个短转义；其余 C0 控制字符 `\uXXXX`；DEL `0x7f` 与 U+2028/U+2029 直出不转义）、紧凑分隔符；**orjson 语义即冻结算法**（转义形态随 orjson 实际输出冻结，不另行规定）；一切 object 含 `default` map 按 key UTF-8 字节序；数组序按 §12.2。**canonical 序列化产生的字节就是 wire body**——ETag 哈希对象 = 实际发送的 canonical identity bytes，不存在另一份重排副本（同一字节双重身份：传输体与哈希输入）。ETag 输入**永不**是上游原始字节；同一上游输入 → 同一 canonical 字节 → 同一 validator。
- **validator 规则（`src/oc_slimapi/etag.py` 口径）**：identity → 强 validator `"<sha256hex>"`；gzip → 弱 validator `W/"<sha256hex>"`；hash 输入 = `REP_VERSION + NUL + coding + NUL + canonical identity body`（全量 hex，不截断）。
- **REP_VERSION 域隔离**：含 wire-view 标记 + 投影/配置指纹——v3 validator 与修订后 v4 validator 互不匹配；修订切换（透传 → 投影）本身经 REP_VERSION 投影版本轮换全部 validator（v2 §6.2 机制继承），构造上不可能误 304；上游自身 ETag 不透传（sidecar 生成域，§6 第 4 条基线）。**修订三注记 [2026-08-20]**：投影字段集演进（ModelEntry 恢复 optional `limit`）同样经指纹 bump 轮换（实现值 `providers-projection-v1` → `v2`）——同输入下新旧 v4 validator 必然不同，旧 ETag 全部自然失效重拉。
- **`Vary: Accept-Encoding` 强制**（§15）；`If-None-Match`/`*`/judge 三态 = §6 第 3 条冻结行为；200/304 均 `Cache-Control: no-store`（ETag 仅省下行传输字节，管线照跑，非缓存授权）。
- **ETag 开关**：继承 `OC_SLIMAPI_ETAG_ENABLED`（缺省开）。关闭 → 本路由无 `ETag`、无 304 判定；**`Vary: Accept-Encoding` 仍发**——表示可变性与 ETag 正交。readiness 不受该开关影响：`representation.vary.v4` 恒可满足（§3.3）。

## §13 Session 单查 parity（`GET /slimapi/session/{sid}?v=4`）[2026-08-19 修订冻结]

> **当前状态**：现行 `?v=4` 单查恒返回既有 skeleton 投影（§4.1 `SESSION_KEYS` 列集；§10 发布态注载——单查无 v4 分叉）。本节为冻结目标——当 feature `session.single.projection.v4` 进入 `satisfied`（§3.3 门控）时生效：单查升级为与 v4 列表同源 canonical 形状，列表 item 形态与本节对象同批统一；`?v=3` 恒 v3 skeleton（v3 冻结——4.0.0–4.7.0 历史语义，v4-only 窗下 `?v=3` 已 400）。

单查与全局列表 `GET /slimapi/sessions?v=4` 共用**同一 canonical 对象**；列表请求参数矩阵（`archived`/`parent`/`search`/`cursor`/`limit` 1..500）、cursor 语义、降级判定（何时允许 fallback、何时 503 `auxiliary_unavailable`，含 `Retry-After: 30`）**继承 §4 冻结**——本节在其上统一 item 投影并叠加标记语义。单查 `directory` 消费语义 = §5.1 基线（单查为消费集路由：query 单值消费剥离、头退役 400、多值 400）；未命中 → 404 `session_not_found`（既有）；native 4xx 逐字 / 5xx/网络 → 503 `upstream_unavailable`（继承）。

### §13.1 canonical 形状（list item 与 single 共用）

```ts
type SessionSkeletonV4 = {
  id: string
  directory: string
  parentID: string | null
  projectID: string | null
  project?: { id: string; name?: string; worktree: string } | null
  title: string
  agent: string | null
  model: { id: string; providerID: string; variant?: string } | null
  time: { created: number; updated: number; archived: number | null }
  summary: { additions: number; deletions: number; files: number } | null
  tokens_input: number | null
  tokens_output: number | null
  tokens_reasoning: number | null
  tokens_cache_read: number | null
  tokens_cache_write: number | null
  revert: { messageID: string; partID?: string } | null
  partial: boolean
  degraded: boolean
}
type SessionsV4 = {
  items: SessionSkeletonV4[]
  nextCursor: string | null
  complete: boolean
  degraded: boolean        // required 布尔（区别于 §4.1 发布态 degraded?: true 可选形态）
}
```

修订形态 = §4.1 发布态字段集（§4.1 `SESSION_KEYS` 白名单列投影 + `project` 对象 + **tokens 五列平铺 v4-only 字段**，§4.1 R2 真库列名实证：`tokens_input/tokens_output/tokens_reasoning/tokens_cache_read/tokens_cache_write`）+ **`partial`/`degraded` 两个 item 标记** + envelope `degraded` 改 **required 布尔**。单查响应 = **裸 `SessionSkeletonV4` 对象**（无 envelope；响应级 degraded 即 item 自身 degraded，§13.4 公式平凡成立）。无 effectiveStatus / subagentList / Turn / cost 聚合 / exact-merged / generic fragment 字段（§17）。

### §13.2 字段真值表（requiredness / null / absent + fallback 可表示性，冻结）

| 字段 | requiredness | null / absent 语义 | dbaux 来源 | native fallback 来源 / 行为 |
|---|---|---|---|---|
| `id` | required | 永不 null | session 行 | native session id；不可得 → **整响应失败**（§13.2a） |
| `directory` | required | 全局列表强制**非空字符串**；单查按 §5.1 基线 directory 消费（显式传入经基线校验，非法值 → 400 `invalid_directory` 族） | session 行 | native directory（同值，禁跨源合并）；不可得 → **整响应失败** |
| `parentID` | required | `null` = 根会话（业务合法 null） | session 行 | 不可得 → `null` + `partial:true, degraded:true`（可 null 字段，§13.2b） |
| `projectID` | required | `null` = 上游确无 project（**≠** join 失败） | session 行 | 不可得 → `null` + partial/degraded |
| `project` | optional | **absent ⇔ `projectID == null`**；`null` 仅当非空 `projectID` 的 join 不可用/无效（必伴 `partial:true, degraded:true`）；对象形 `{id, name?, worktree}`，`name` 可 absent | project join | 同 §13.5 三不变量；join 不可用/无效 → `null` + partial/degraded |
| `title` | required | 可为空串（不可 null） | session 行 | 不可得 → **整响应失败** |
| `agent` | required | 业务合法 null | session 行 | **三态（§13.2b）**：上游确无值 → `null` 不 partial；来源不可得 → `null` + partial/degraded |
| `model` | required | 业务合法 null；`variant` absent 不置 null | session 行 | 三态同 `agent`（variant 键随 model 对象整体判定） |
| `time` | required | `created`/`updated` 非负数；`archived` nullable；子键无 absent 形态 | session 行 | `created`/`updated` 不可得 → **整响应失败**；`archived` 不可得 → `null` + partial/degraded |
| `summary` | required | 业务合法 null；对象时三子键均为数值 | session 行 | 三态同 `agent` |
| `tokens_input` / `tokens_output` / `tokens_reasoning` / `tokens_cache_read` / `tokens_cache_write`（五字段一组，§4.1 R2 真库列名平铺） | required | `null` = 无计量（业务合法 null，与 §4.1 发布态一致：DB 行该列 NULL / LEFT JOIN 缺行 → `null`） | session 行五平铺列 | **三态（§13.2b）**：上游确无计量 → `null` 不 partial；来源不可得 → `null` + partial/degraded。native fallback 映射：上游嵌套 `tokens.{input,output,reasoning}` + `tokens.cache.{read,write}` → 五平铺键（§4.1 `_http_session_to_v4` 同构） |
| `revert` | required | 业务合法 null；`partID` absent 不置 null | session 行 | 三态同 `agent` |
| `partial` / `degraded` | required | §13.4 公式；业务合法 null 不触发 partial | 派生 | 派生（fallback 态下 item `degraded` 恒 true） |

**§13.2a 整响应失败规则（required 且不可 null 字段）**：`id`/`directory`/`title`/`time.created`/`time.updated` 在 fallback 源无法获得 → **整响应失败**：503 `auxiliary_unavailable`（**复用既有码**，附 `Retry-After: 30` 同族）——不发明占位值（空串/`0`/伪造 id 均禁止）、不砍字段发残 item、不造新码。

**§13.2b 可 null 字段三态**：① 上游确无值（业务合法 null）→ `null`，不 partial；② 来源不可得（字段缺失/读取失败）→ `null` + `partial:true, degraded:true`——**仅当字段类型允许 null**；③ 正常有值 → 投影值。不可 null 字段无三态，只有 §13.2a 一条路。

**§13.2c 单 item 失败边界**：**无「不可表示或投影失败」的 item 混入 `items` 数组**——该类 item → 整响应失败（503 `auxiliary_unavailable` 同 §13.2a），与 whole-response fallback 原则一致。§13.2b、§13.5 明确允许的**可表示 partial item 正常进入 `items` 数组**（nullable 字段来源不可得的 `null`+标记、project join 失败的 `project:null`+标记）并参与 §13.4 envelope degraded 聚合（partial item 本身不是失败）。**整响应失败仅指两处**：§13.2a 的 required-不可-null 字段不可得，与本条的不可表示/投影异常。

### §13.3 来源策略（dbaux-primary）

- **list 与 single 均 dbaux-primary、同一 snapshot boundary、同一 projector**：单查 = dbaux 点查优先（只读 SQLite snapshot 内组装整个响应，列裁剪 + join）；列表的形状与组装 = §4.1（参数矩阵/单条 SQL 零缓存组装/排序），常态路径与降级判定 = §4.2（DB 投影源常态 + 72 格降级矩阵）。**同一 projector 不变量（就地冻结）**：list item 与 single 响应体共用**同一 canonical projector 代码路径**，同一输入行/对象产出逐字段同值——不存在单查独有的第二投影实现（分裂投影 = 实现违约）。
- **dbaux 不可用 → 整响应切换 native HTTP fallback 并 `degraded:true`**（冻结措辞）："selected whole-response native fallback; no per-field or per-item cross-source mixing"——**禁止**逐字段/逐条目跨源拼接；fallback 路径的 items 仍经**同一 projector** 投影为 canonical 形状（native 权威 schema → 同一 keep/drop 规则）。
- fallback 是否被允许（vs 503 `auxiliary_unavailable`）由 §4.2 降级矩阵（72 等价类 + cursor/search 正交轴）冻结判定；本节不放宽不收窄。project join 在整响应决策内同规则处理（§13.5）。

### §13.4 `partial` / `degraded` / `complete`（显式公式）

```
envelope.degraded == (任一 item.degraded == true) ∨ (本响应采用 native fallback)
```

- **marker 语义**：业务合法 null（`agent`/`model`/`summary`/`revert` 的上游 null）**不触发** `partial`；仅来源缺字段/失败（含 project join 失败）触发。`partial ⇒ degraded`（单方向蕴含；degraded 可单独成立——如纯 fallback 无字段缺失）。
- **`complete` 仅表分页完备性**：不因 `degraded:true` 变 `false`（两者正交；fallback 下 complete 退 best-effort 强度的 §4.2 语义继承）。
- envelope `degraded` 为 **required 布尔**：list 响应恒携带（含 `false`）；§4.1 发布态 `degraded?: true` 省略形态随修订废止（该废止随 `session.single.projection.v4` 进入 `satisfied` 生效——生效前列表 envelope 维持发布态）。

### §13.5 project join 不变量

`project` 对象**当且仅当**同时满足以下三条才非 null 发出：

1. `project.id == projectID`（join 行 ID 与 session 行 `projectID` 一致）；
2. `worktree` 为**非空字符串**；
3. join 本身成功（dbaux 可用且取到行）。

任一不满足——**缺失 / ID mismatch / join 失败** → 该 item `project: null, partial: true, degraded: true`。**"无 project"（`projectID:null` → `project` absent）与 "join 不可用"（`project:null` + 标记）是两种不同 wire 形态**，不得混同。

## §14 expand 端点全文语义与能力闭环 [2026-08-19 修订冻结]

> **当前状态（2026-08-21 版本窗收窄更新）**：feature `messages.expand.v4` 已 `satisfied`（§3.3 门控全集点亮）——**本节语义已生效**。href 按解析后 selector 生成（`skeleton.py` `_expand_ref` 收 `wire_view`；v4-only 窗下恒 `?v=4`）；`capabilities["4"].expand` 已广告（§3.1）。历史状态（4.0.0–4.7.0）：href 硬编码 `?v=3`、`capabilities["4"]` 无 `expand` 键——该形态随 4.8.0 版本窗收窄一并移除。

- **12 类目有序清单（冻结）**：`info_summary_diffs` → `part_text` → `part_reasoning` → `part_state_output` → `part_state_error` → `part_state_input_full` → `part_state_metadata_full` → `part_state_attachments` → `part_url` → `part_source` → `part_snapshot` → `compaction_full`（单一事实源 `src/oc_slimapi/traffic.py::EXPAND_CATEGORIES` 表序延续；versions 广告与流量记账同源；类目级别/适用 part 类型/返回 `data` 形状见 §14.2）。
- **`fragmentMaxBytes` = `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 运行时值**（缺省 **8388608**，界 **1024..33554432** 含边界；非法值启动 RuntimeError——config.py 既有冻结）。capability 广告 `capabilities["4"].expand = {categories, fragmentMaxBytes}` 与历史键 `capabilities["3"].expand`（§3.1——4.0.0–4.7.0 形状锚点，v4-only 窗下该键已随版本窗移除）形状同构（categories = 12 类目有序数组 + fragmentMaxBytes = 数值）；**仅当全部 12 类目在 v4 视图闭环（href/响应/错误）才广告**（§3.3 批次闭合）。
- **href canonical 形态（冻结）**：`GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=<selector>[&directory=...]`（part 级含 `/{partID}`）。query 键序：**`v` 第一、客户端追加 `directory` 第二、无其他 key**；`v` 值来自**解析后 selector**——`?v=4` 请求的响应 → `v=4`（本节修订）；历史规则（4.0.0–4.7.0）：`?v=3` 请求的响应 → `v=3`（v3 冻结，修订不触碰；v4-only 窗下 `?v=3` 已 400）；经既有 query 编码**恰编码一次**。
- 端点求值序、错误码族、响应 envelope、响应头基线语义（两视图一致）全文 = §14.1-§14.5（2026-08-21 正文化转录 [冻结]，本修订不另立）。

### §14.1 路由、selector 与响应头

`GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=<selector>[&directory=...]`（消息级，仅 `info_summary_diffs` 合法）与 `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v=<selector>[&directory=...]`（part 级，其余 11 类目）。两路由均挂 messages 面、require selector、消费 `?directory=`（§5.1 消费集）；`category` 以 str 接收 + 手工白名单校验（不用 Enum 路径参数）→ 400 `invalid_expand_category`。响应**无 ETag/304**（成功恒 200）、`Cache-Control: no-store`、`Vary: Accept-Encoding`、无自定义辅助头。

### §14.2 类目表与映射要点

| category | 级别 | 适用 part 类型 | 返回 `data` |
|---|---|---|---|
| `info_summary_diffs` | 消息级 | — | `{diffs: <FileDiff[]> \| null}`（`info.summary.diffs`） |
| `part_text` | part | text | `{text: string \| null}`（`part.text`；现行 skeleton 不再产生 `part_text` ref——text 永远内联，端点保留服务历史响应与降级场景） |
| `part_reasoning` | part | reasoning | `{text: string \| null}`（`part.text`） |
| `part_state_output` | part | tool | `{output: string \| null}`（`state.output`） |
| `part_state_error` | part | tool | `{error: string \| null}`（`state.error`） |
| `part_state_input_full` | part | tool | `{input: object \| null}`（`state.input`） |
| `part_state_metadata_full` | part | tool | `{metadata: object \| null}`（**剥离 `diagnostics`**；**[修订四]** `tool=="edit"` 且源 metadata 无 `files` 键且 `truncated` 非 true 且 `diff` 解析成功 → 返回的 metadata **附加合成键 `"files"` = 完整解析列表（无 cap）**——与 skeleton cap 10 投影互补，cap 外第 11+ 条经本端点可达；不满足则返回原始 metadata 去 `diagnostics`） |
| `part_state_attachments` | part | tool | `{attachments: object[] \| null}`（`state.attachments`） |
| `part_url` | part | file | `{url: string \| null}`（`part.url`） |
| `part_source` | part | file | `{source: object \| null}`（`part.source`） |
| `part_snapshot` | part | step-start / step-finish | `{snapshot: string \| null}`（`part.snapshot`，均 optional） |
| `compaction_full` | part | compaction | 完整 compaction part（**剥离 `expandRefs`**） |

**映射要点**：state 类 category 全部 **tool-only**（PatchPart 无 state——跨类型请求 → 400 `expand_category_mismatch`；PatchPart `files` 自修订四起为归一化对象数组投影（§10.2 第 2 条），仍无 state、state 类目适用集不变）；`part_snapshot` 覆盖 step-start 与 step-finish 两类型；**无 `part_files_full`（维持）**——修订四后 `files` 已是 compact 对象数组投影（归一化 + cap 10 + `filesTotal`，§10.2），仍不设整字段展开类目（P0-2 patch expand ref 搁置：生产 patch part 0/23,827 带 state，永不触发；重启条件 = 上游为 PatchPart 引入 state 或生产出现带 state 的 patch part）；extractor 全部白名单构造，不暴露 category 外字段（唯一例外 = `part_state_metadata_full` 的 edit 合成 `files` 增补，见上表行——§10.2 修订四）。

### §14.3 冻结求值顺序链与错误族

路由内求值序（独立于 §8.3 middleware 链——selector/directory 400 仍先于路由内错误）：

```
① category ∈ 白名单（str 校验，12 项）        → 400 invalid_expand_category（附有效类目表序）
② 级别匹配（part 级缺 partID / 消息级 category 携带 partID）
                                               → 400 expand_category_mismatch（附 expectedLevel）
③ transform pool 准入（镜像 /full 吸收语义）  → 503 transform_busy（Retry-After）——先于一切 part 级错误
④ 共享 single-flight GET（与 direct /full、merged fan-out 同键去重；键含 scope+sid+mid+directory）：
   a. 发送/网络失败                          → 503 upstream_unavailable
   b. 上游错误状态（≥400）：drain 失败        → 503 upstream_unavailable；drain 成功按状态映射——
      sid 级 404                             → 404 session_not_found（带 sessionID）
      5xx                                   → 503 upstream_unavailable
      其余 4xx                              → 502 upstream_http_N
   c. 上游 2xx cap-read 网络失败              → 503 upstream_unavailable
      源 body 超 max_message_bytes          → 413 expand_source_too_large（limitBytes）——先于 JSON 解码（超限且畸形 body 仍 413）
   d. JSON 解码失败 / 顶层非 dict body        → 503 upstream_unavailable
⑤ parts 定位/形状（parts 缺失/null/标量/元素非对象/重复 partID）
                                               → 502 upstream_invalid_shape；partID 未命中 → 404 expand_target_not_found（附 reason:"part_missing"）
⑥ part.type ∈ category 适用集                 → 400 expand_category_mismatch（附 expectedTypes）
⑦ 提取（白名单构造）+ 嵌套类型校验：字段存在但类型与冻结 schema 不符 → 502 upstream_invalid_shape；
      缺失或 JSON null                          → 200 + data 对应键 null
⑧ 片段字节 cap（序列化后、gzip 前）超 max_expand_response_bytes
                                               → 413 expand_fragment_too_large（limitBytes）
```

**错误码唯一命名**：全文仅存在 `expand_target_not_found`（附 `reason:"part_missing"` 字段）与 `expand_category_mismatch` 两码，无独立 `part_missing`/`category_mismatch` 码。要点：503/413(源)/502 **可能先于** 404(part)/400(类型) 出现——「准入在前、先取后析」既有管线（与 /full 同构）的固有序，如实冻结为契约。

### §14.4 响应 envelope（200）

消息级 `{"category": <str>, "messageID": <mid>, "data": {...}}`；part 级 `{"category", "messageID", "partID", "data": {...}}`——`data` 形状按 §14.2 表对应键，缺失或显式 null 均 `data.<key> = null`。无 `contentFingerprint`（片段端点不适用）。成功响应携带 `messageID`/`partID` 供对账；**读当前态**（非 skeleton 快照）：当前态字段值；part 已删 → 404（`reason:"part_missing"`）；part 类型已变 → 400——客户端据此刷新 skeleton。

### §14.5 配置与观测

`OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES`（默认 **8 MiB**，界 **1 KiB–32 MiB** 含边界，非法值启动 `RuntimeError`）；全局内存信封按 `max(max_response_bytes, max_expand_response_bytes) × max_transforms` 计账（expand worker 同时持有原始 full-message bytes、解析对象、片段序列化 bytes、可选 gzip bytes），expand cap 配置导致信封超限的组合 **startup 拒绝**。观测：traffic ledger/snapshot/metrics `expand` 块——按 category × status 聚合（12 类目白名单），伪造/非法 category 折叠进固定 `invalid` 桶（防基数 DoS）；`traffic-snapshot-YYYY-MM-DD.jsonl` 持久化含 expand。

## §15 表示层：Vary 规则与 v4 ETag [2026-08-19 修订冻结]

> **当前状态**：现行 v4 sessions 列表 200 响应**无 ETag 且显式删除 `Vary`**（`sessions.py` `_v4_json_response`——`json_response` 基线为 gzip 协商统一盖 `Vary: Accept-Encoding`，该 helper 将其删除；**已知 bug/风险项**，§4.4 发布契约载「v4 sessions 无 ETag/Vary/304」）。本节为冻结目标——当 feature `representation.vary.v4` 进入 `satisfied`（§3.3 门控）时生效。

- **`Vary: Accept-Encoding` 直接规则（冻结）**：凡可随 `Accept-Encoding` 变化的 v4 表示（200/304）**必带** `Vary: Accept-Encoding`。修订修正上述删除 bug——全 v4 路由正确发 Vary（v4 sessions 列表含 body gzip 协商，无 Vary 声明即缓存不正确）。SSE 帧化为**显式例外**（§7.5：SSE 恒 identity、无 Vary）。
- **v4 sessions 列表 ETag（新增）**：canonical 口径与 §12.6 同规则——canonical identity bytes 即 wire body；identity 强 validator `"<sha256hex>"` / gzip 弱 `W/"<sha256hex>"`；hash 输入 = `REP_VERSION + NUL + coding + NUL + canonical identity body`。`If-None-Match`/`*`/judge 三态 = §6 第 3 条；304 头集合 = `ETag` + `Vary` + `Cache-Control: no-store`（sessions envelope 自含 `nextCursor`/`complete`，无 aux 头）；200/304 均 `no-store`。
- **`merged_vary` 说明（如实）**：现行 `merged_vary`（`etag.py`）将任意输入折叠为单值 `"Accept-Encoding"`——v4 修订表示层**恰好只需该单值形态**（directory Vary 值已随 §1 头退役移除，v4 无其他 Vary token），无需扩展 helper；如未来出现多 token 合并需求，须先扩展该 helper 并保持 v3/v4 既有输出逐字节不变。
- **域隔离（冻结）**：缓存键 / singleflight 键 / ETag REP_VERSION 均含 wire-view 标记——v3 与修订后 v4 validator 互不匹配，跨视图 `If-None-Match` 保守 200；修订切换（无 ETag → 有 ETag）经 REP_VERSION 投影版本轮换，不可能误 304。
- v4 sessions 以外的 v4 路由：ETag/Vary 语义 = §6 基线发布态（无版本分叉）；providers 路由修订后按 §12.6 口径。

## §16 POST 等效动作族 + method 边界 [2026-08-19 修订冻结；**修订二：POST 等效动作族——已发版 v4.3.0**]

> **当前状态**：修订一（feature `method.boundary.v4`，v4.2.0 已 `satisfied`）已落地——三条 POST 组合在 `?v=4` 下返回精确 405 `method_not_applicable`（§16.1，现行为）。**修订二**（owner 裁决 2026-08-19，新 feature `session.post-actions.v4`，§3.3 第 10 ID）为本节冻结目标：该 ID 进入 `satisfied` 时三条 POST 激活为等效路由、§16.1 的 405 拒绝面按**声明式组合优先级**让位（§16.2/§16.3 四位组合表；依赖蕴含 `session.post-actions.v4 ⇒ method.boundary.v4` 见 §3.3）。**全部仅 `?v=4` 视图；`?v=3` 冻结零改动**（三组合在 v3 → 404 `thin_route_not_found`——4.0.0–4.7.0 历史行为，任何阶段不变；v4-only 窗下 `?v=3` 已 400，不达路由）。**加性并存，非替代**：PATCH/DELETE 在 v3/v4 均继续可用，v4 继承不退役。

| 操作 | V3（4.0.0–4.7.0 历史；v4-only 窗下 `?v=3` 已 400 不达路由） | V4（`session.post-actions.v4 ∉ satisfied`，v4.2.0 现行为） | V4（`∈ satisfied`，修订二冻结目标） |
|---|---|---|---|
| 更新 session（title/metadata/permission / `time.archived`，双 shape） | PATCH（发布语义） | **PATCH 继承**（applicability 行显式声明，非路由 fallthrough） | PATCH 继承（不退役） |
| 删除 session | DELETE（发布语义） | **DELETE 继承** | DELETE 继承（不退役） |
| `POST /slimapi/session/{sid}` | 404 `thin_route_not_found`（现状） | 405 `method_not_applicable`（§16.1 过渡态） | **≡ PATCH 等效路由**（§16.2-a） |
| `POST /slimapi/session/{sid}/archive` | 404（现状） | 405（§16.1 过渡态，空 `Allow`） | **便捷 archive**（可选 body，§16.2-c） |
| `POST /slimapi/session/{sid}/delete` | 404（现状） | 405（§16.1 过渡态，空 `Allow`） | **≡ DELETE 等效路由**（§16.2-b） |

### §16.1 过渡态 405（`method.boundary.v4`；修订二激活前现行为，冻结值不回收）

**method-not-applicable 精确响应（范围收窄：仅当 selector 已成功选择 `?v=4` 且 `method.boundary.v4 ∈ satisfied ∧ session.post-actions.v4 ∉ satisfied`（两位合取，与 §16.3 四位组合表第二行同口径——boundary 亦未 satisfied 时为框架 404 历史态，不发本 405），method-path 为上述三条组合之一时返回）**——V3 对同 method/path 保持已发布行为（404 `thin_route_not_found`，不引入本 code；4.0.0–4.7.0 历史行为——v4-only 窗下 `?v=3` 已 400 不达路由）：

- **HTTP 405**；
- **`Allow` 头**（字面冻结，逗号+空格分隔）：`POST /slimapi/session/{sid}` → `Allow: GET, PATCH, DELETE`；`POST /slimapi/session/{sid}/archive`、`POST /slimapi/session/{sid}/delete` → **空 `Allow:`**（RFC 9110 §10.2.1：空值 = 资源不支持任何方法）；
- **coded error body**（统一错误体惯例，§8.1）：`{"code":"method_not_applicable","method":"<METHOD>","allow":["GET","PATCH","DELETE"]}`（`allow` 数组与 `Allow` 头一致；archive/delete 为 `[]`）；
- **不转发上游**（零上游 IO）+ **`Cache-Control: no-store`**；**selector 扩展不得自然转发**——v4 视图下这三条组合在过渡态永不透传上游；
- **优先级**：§8.3 链在「② selector version 族 400」与「③ selector directory 族 400」之间插列本节 405（判定不依赖 query 参数，故先于 directory 消费；§8.4）；
- **适用范围精确限定**：本 405 仅限三条组合且 selector 已选 v4 且门控未激活。其他已收编 path 的未注册方法**继承既有路由行为**（框架 405/404 现状——不发本 code、不发 `allow` 数组）；完全未收编 path → 既有 404 `thin_route_not_found`（catch-all 关闭终态：未收编路径显式 404，任何版本不变）；
- V3/V4 的 PATCH/DELETE controlled-write 语义（JSON body 透传、上游 4xx 逐字、5xx/网络 → 受控 503、no-store）两视图**逐字不变**（「两视图」为 4.0.0–4.7.0 历史表述——v4-only 窗下唯一可达视图 = v4，语义对该视图逐字成立）。

### §16.2 POST 等效动作族（`session.post-actions.v4 ∈ satisfied` 时激活；桥式 **100% 等效优先**——sidecar 不新增语义）

- **a. `POST /slimapi/session/{sid}` ≡ `PATCH /slimapi/session/{sid}`**：body = 同一 PatchPayload **透传**；响应/错误/求值序/directory 消费/请求 cap ≡ PATCH 在**同 selector 视图**下的现行受控写管线行为（**逐字节等效**；上游 4xx 逐字、5xx/网络 → 受控 503、`no-store`、gzip 协商头随基线管线）。
- **b. `POST /slimapi/session/{sid}/delete` ≡ `DELETE /slimapi/session/{sid}`**：请求实体处理与 DELETE **完全相同并原样转发**——读取请求实体、同一 `max_message_bytes` cap（超限 → 413 同码同序）、`Content-Type` 透传、body **逐字节转发上游**；无「忽略 body」分支。求值序 = 受控写管线原序。**上游语义如实继承并载明**（owner 裁决 q1）：上游递归删子 + 吞错语义原样——**非幂等可接受**（重复 delete 返回上游行为，如 404）；部分子会话删除失败**不可见**（上游已知语义）；sidecar **不聚合、不重试、不自建编排层**。等效上游调用形态 = 受控写管线对 `DELETE /session/{sid}` 的现行转发。
- **c. `POST /slimapi/session/{sid}/archive`（便捷加性）**——三项精确冻结：
  - **缺省判据（octet 层，冻结）**：请求实体长度 = 0 → 缺省（走下述合成）；非空实体（含空 JSON `{}`、仅空白字节）→ **一律不解析、逐字节透传**上游验证（与 PATCH 同为「sidecar 不预解析」）。`Content-Type` 是否存在/取值**不影响判据**（随实体一并透传）。
  - **合成体（冻结）**：仅当缺省时——`Content-Type: application/json`；JSON 字节 = 恰 `{"time":{"archived":<ms>}}`（无空格紧凑形，`<ms>` 为十进制毫秒整数）；`<ms>` = 合成点（求值序中 body 判空后立即）的 sidecar wall-clock epoch-ms（`time.time()*1000` 取整——与 digest `updatedAt` 同源口径，不读上游）。
  - **错误映射（与受控写管线零偏差）**：上游 4xx（含拒绝 `null` archived 的取消归档请求）**原样透传**；上游 5xx/网络故障 → 既有受控写管线统一 503。等效管线**不引入新错误码**（无 502 分支）。
  - 合成或透传后均走 a 款 PATCH 等效管线。
- **directory 消费**：三条 POST ≡ 各自等效目标路由（§5.2 修订二加注）。
- **无 SSE / 缓存新语义**：无**新增** SSE 事件类型、无 sidecar **合成**额外事件；等效上游动作自然触发的既有事件（`session.updated`/`session.deleted` 等）按现行管线照常传播。不参与 ETag（受控写管线 `no-store` 继承）。

### §16.3 组合优先级与蕴含依赖（修订二；与 §3.3 门控模型例外①②同口径）

- **依赖蕴含（冻结，同 §3.3 语义段）**：`session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied`——违反即 discovery contradiction（§3.3 条件⑦），服务端不得发出该载荷。
- **四位组合行为表（穷尽；行为列 = 三条 POST 组合在 `?v=4` 下的命中面）**：

| `method.boundary.v4` | `session.post-actions.v4` | 三条 POST 组合（`?v=4`）行为 | 状态 |
|---|---|---|---|
| ∉ satisfied | ∉ satisfied | 框架 404 `thin_route_not_found`（4.0.0/4.1.0 历史态） | 合法（boundary 修订未落地时） |
| ∈ satisfied | ∉ satisfied | §16.1 coded 405 `method_not_applicable` | 合法（**4.2.0 现行为/过渡态**） |
| ∈ satisfied | ∈ satisfied | §16.2 等效路由（fallback 405 对这三条组合不再产生） | 合法（修订二激活态） |
| ∉ satisfied | ∈ satisfied | ——（不可达） | **contradiction（条件⑦）** |

- **声明式组合优先级（冻结；非对独立门控的违反，§3.3 门控模型例外①）**：`method.boundary.v4` 的语义定义 = 三条 POST 组合在 post-actions 未激活时的 **fallback 405**；post-actions 激活后该 fallback 对这三条组合不再产生（`method_not_applicable` 无命中面——错误码保留定义，§8.4 注记），boundary **其余 method-path 维度**（其他已收编 path 的未注册方法 → 框架 405/404）**继续现行**。历史基线注记：4.2.0 时 post-actions 尚不存在，该 405 即 boundary 的完整语义。
- **Allow 字面调整**：§16.1 冻结的 `Allow` 字面（含空 `Allow:`）为**过渡窗口行为**（= 四位组合表第二行），激活后随 fallback 405 一并消失——不再有任何路由为这三条组合发 `Allow` 头；sidecar 亦**不新增**其他任何 method-path 组合的 `Allow` 发射面（框架 405/404 现状继承）。
- **过渡安全**：门控翻转只改变这三条组合的 v4 命中面（405 → 等效路由）；v3 视图（历史 4.0.0–4.7.0——v4-only 窗下不可达）与 PATCH/DELETE 主路径在整个翻转前后逐字节不变。

## §17 修订 non-goals（明确不做项）[2026-08-19 修订冻结；**修订二：non-goals 收紧**]

> **当前状态注记**：本节所列能力在现行已发布实现（4.0.0/4.1.0/4.2.0）中**均不存在**，修订后**仍为 non-goals**（不是待实施 feature）。它们不进入 §3.3 readiness ID 集合的原因：本节是**能力边界声明**（sidecar 明确不提供面），非待点亮 feature——边界本身已编码进 `required ≡ U` 全集（缺 ID 即不做）。**修订二（owner 裁决 2026-08-19）收紧**：cascade 编排层与 cross-session search 为**永久 non-goal**（无对应 feature ID、无 deferred 候选资格，再启用须推翻本节正式修订）；原「POST-only update」deferred 候选已被修订二 q2 激活为 §16.2 POST 等效动作族（经 `session.post-actions.v4` 门控），自本节移除。

- **project status / effectiveStatus / subagentList** 聚合字段——骨架不做会话状态推断；
- **独立 Turn 资源**——turn 语义维持 `/slimapi/sessions/status` 的 `turnIncarnation`/`turn` 合并字段现状；
- **exact merged**——merged 模式 best-effort 语义不变（§10.2 第 5 条基线冻结延续）；
- **512B preview / generic fragment**——expand 类目维持 12 项冻结清单（§14），不加预览/通用片段类目；
- **cascade 编排层**（owner 裁决 q1，永久）——sidecar **不自建**级联编排/子删除聚合/重试/部分失败可见性：delete 语义 = `DELETE`（及 §16.2-b 等效 POST）**沿用上游递归删子 + 吞错现状**（非幂等可接受）；archive = 单会话 PATCH 等效（§16.2-c，**不级联**）；
- **cross-session search**（owner 裁决 q3，永久）——非必要能力；§4.6 search 维持 per-list 字面子串语义，不做跨会话检索。

---

## §18 POST /slimapi/sessions/details（批量 session 详情）[2026-08-22 新增；4.10.0]

> **动机**：oc-webui 逐 session 轮询 `GET /slimapi/session/{sid}`（12h 观测 6.2k 次）；一次 POST 拿多个 session 的 skeleton 详情。纯加性（v4-only 窗口内的 minor），客户端必改点：**无**。

### 18.1 请求

`POST /slimapi/sessions/details?v=4`，body：`{"sids": ["ses_...", ...]}`。

- `sids` **缺失** / body **非 JSON 对象** / `sids` **非字符串数组** / **空数组** / **含非字符串元素** / **含非法字符的 sid** / malformed JSON / 超请求体 raw cap（256 KiB）→ 400 `invalid_body`；
- **sid 字符集白名单**（rev 8.8 整改 MAJOR-1 引入；rev 8.9 MAJOR-2 裁定依据修正）：每个 sid 必须匹配 `^[A-Za-z0-9_-]{1,128}$`，不匹配 → 400 `invalid_body`（hint 说明字符集约束）。**这是 sidecar 显式产品裁定，不是上游 schema 推论**——上游 `packages/schema/src/session-id.ts` 实际接受任意 `ses` 前缀字符串（自定义/历史/迁移 ID 可宽于本模式）；本端点有意收窄到生成器产物域（`ses_` + 26 位：12 小写 hex + 14 base62，`schema/identifier.ts`；fixture / DB 内 sid 同落该模式），收窄理由：① 输入域严格窄于 §13 单查——单查 sid 是**路径参数**，Starlette `{sid}` 仅结构性排除 `/`（点号/冒号/Unicode/超长值仍可达单查，`?`/`#` 不达路由），本白名单把批量 body 输入收窄到生成器产物域（比单查 de facto 域更严，非等价）；② 拒绝 `/`、`?`、`#`（拼上游 URL 时会被重解释为路径/query/fragment）与控制字符（上游 `build_request` 抛 `InvalidURL` → 裸 500 面）；③ 已知消费方（ocdroid / oc-webui）只出现生成器形态。若未来需要 schema 完整域，属加性契约修订（放宽白名单 + URL 编码处理），可逆。非法输入在**入口**拒绝 → dbaux / native 两路径语义天然一致；
- `sids` **去重后 > 50** → 400 `too_many_sids`（去重先于计数：60 个原始 sid 含 10 重复 = 50 唯一 → 合法）；
- **重复 sid 静默去重**；响应 `sessions` item 顺序 = 请求（首现）顺序，`missing` 同序；
- `?directory=`：**tolerant-ignore**（读后丢弃，不校验不转发——B4 惯例，同 §13.4）；`X-Opencode-Directory` 头同样忽略，**无冲突检查**；
- `?v=` selector 语义同全局面（v4-only 窗口，缺 `?v=4` → 400 `unsupported_version`）。

### 18.2 响应 200

```json
{"sessions": [<SessionSkeletonV4>, ...], "missing": ["ses_...", ...]}
```

- `sessions`：查到的 sid 的 **§13 同一 canonical projector**（`canonical_session_skeleton_v4`）产出项，按请求顺序重排（`dict` by sid 映射）；
- `missing`：未查到的 sid（按请求顺序）；**全 404 合法**（`sessions: []`）；
- 头：`Cache-Control: no-store`；gzip 协商 + `Vary: Accept-Encoding` 走 `json_response` 全局惯例。

### 18.3 两级路径（同 §13.4 哲学）

| # | 条件 | 行为 |
|---|---|---|
| A | dbaux available | **点查优先**：`WHERE s.id IN (?,?,...)` 动态占位符（参数化，无注入），SQL 形状同 §13 `_SESSION_SINGLE_SQL`（SELECT/JOIN 不变）；**不施加 allowlist**（点查先例：sid 已知即放行，allowlist 只管 §4 列表发现面）。逐 record 过 canonical projector（`degraded:false`） |
| B | dbaux 不可用 / 竞态禁用（`AuxiliaryUnavailableError`） | **native fan-out 回退**：对每个 sid 上游 `GET /session/{sid}`（不带 directory，有界并发 8），**单次** transform-pool offload 解析+投影全批成功响应；item 恒 `degraded:true`（`fallback=True`，native 来源不可全信）。池满 → 503 `transform_busy` + `Retry-After: 2`（admission 先于 fan-out，零上游 IO）。**受控收束**（rev 8.8 整改，MAJOR-2）：任一 fan-out 任务异常时先同步 cancel 全部 sibling 任务并 await 收尾完毕才向上传播——无孤儿上游 IO |

### 18.4 降级矩阵

| dbaux | native 上游 | 结果 |
|---|---|---|
| 可用，全部可表示 | ——（零上游 IO） | canonical items（`degraded:false`） |
| 可用，任一 record 不可表示（projector None / 行被 `rows_to_records` 跳过） | —— | **503 fail-closed**（不得把存在的 session 误报进 `missing`） |
| 可用，`sqlite3.Error` | —— | **503 fail-closed**（§4 BLOCKER-1 同规：不回退 native） |
| 不可用/竞态 | 全部 200 | degraded items（恒 `degraded:true`）+ per-sid 404 → `missing` |
| 不可用/竞态 | 任一 非-404 4xx / 5xx / 网络错 / body 超 cap / malformed | **503 fail-closed**（不混装部分数据） |
| 不可用/竞态 | 404 + body 超 cap | **503 fail-closed**（rev 8.8 整改，MAJOR-3：cap 超限优先于状态码语义——404 超体绝不误判 `missing`；404 + 正常体仍 → `missing`） |

### 18.5 错误码表

| 状态 | `code` | 触发 |
|---|---|---|
| 400 | `invalid_body` | §18.1 全部 malformed 输入族（新 code，加性） |
| 400 | `too_many_sids` | 去重后 >50（新 code，加性） |
| 400 | `unsupported_version` | `?v=` selector 全局面（非本端点新增） |
| 503 | `upstream_unavailable` | native 路径：非-404 4xx / 5xx / 网络错（**含连接建立后流式读取期 mid-stream 网络错**——`read_with_cap` 契约下原样上抛，路由层映射，rev 8.9 整改）/ body 超 cap（**含 404 超体**，cap 优先于状态码语义）/ malformed |
| 503 | `auxiliary_unavailable` | dbaux 不可表示（§13.2a/§13.2c 同判）/ `sqlite3.Error` / native item 不可表示（§13.2a） |
| 503 | `transform_busy` | 池满（`Retry-After: 2`） |

### 18.6 与 §13 单查的关系及 4xx 透传差异

- **同一 canonical projector**：本端点是 §13 单查（`GET /slimapi/session/{sid}`）的**读扇出聚合**，item 形状 = §13.2 `SessionSkeletonV4` 逐字段一致，无新字段。
- **4xx 处理差异（显式冻结）**：§13 单查对上游非-404 4xx **逐字透传**；本端点对任一非-404 4xx **整响应 503 `upstream_unavailable`**。理由：批量响应无 per-sid 透传面（混装「部分 200 + 部分 4xx 原文」会破坏 `{sessions, missing}` 二分结构且客户端无法归因）；fail-closed 与 §10 降级哲学一致（宁可整批重试，不混装部分数据）。

### 18.7 与 §17 non-goal 的边界声明

§17 冻结的 **cascade 编排层** non-goal 针对**写侧**（delete/archive 的级联编排、部分失败可见性）；本端点是**读侧扇出聚合**（多 sid 只读点查/fan-out，无编排状态、无部分失败语义——任一失败整响应 503）。先例：`/slimapi/sessions/status`（§3.y）亦是读聚合。二者不相交，§17 不因本节松动。

---

## §19 `GET /slimapi/file/raw`（裸二进制文件直读；修订五 [P5]；4.11.0）

> **动机**：收编 ocdroid 侧 `HttpImageHolder` 直连 opencode 的图片拉取——binary 经 sidecar 转发后**总字节 ≈ 4/3 ×**（上游 LegacyContent base64 信封），本端点解码为裸 identity bytes 下发，loopback 段显著省流。设计稿 `docs/specs/plan-4.11.0-p1-p6.md` §4。

**请求**：`GET /slimapi/file/raw?path=<str 必填>&directory=<str?>`（`?v=4` selector 必填；directory 消费同 `/slimapi/file` 组 §5.1 基线语义 + allowlist 三态 fail-closed 同组）。

**上游**：`GET /file/content?path=`（directory 作 `X-Opencode-Directory` header）；上游响应 = `LegacyContent` JSON 信封。

**响应（按信封 `type` 分派，冻结）**：
- **`type=binary`**：严格 base64（`validate=True`）解码 `content` → 裸 identity bytes。`Content-Type` = 上游 `mimeType` 经白名单验证（`[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+`；非法/缺失 → `application/octet-stream` 回退）。**跳过 gzip**（对已压缩图片格式无收益且双重压缩浪费 CPU；客户端收 `Accept-Encoding: gzip` 请求仍获 identity）。**强 ETag**（identity bytes 全量 SHA-256，§6.1 口径）+ `Vary: Accept-Encoding` + `Cache-Control: no-store`；`If-None-Match` 弱比较命中 → 304（§6.3 三态全规则适用）。
- **`type=text`**：正文 `text/plain; charset=utf-8`；常规 gzip 协商（受益门适用）→ gzip 200 携带 `W/` 弱 validator；ETag/304 同 §6 全规则。

**错误族（求值序：transform admission 先于上游 GET；读取 cap = `min(max_response_bytes, file_raw_max_envelope_bytes)`（默认 32MiB））**：
- 上游 200 但信封畸形（判别失败/缺字段/类型错/严格 b64 失败）→ **502 `raw_decode_failed`**（§8.1，house error renderer，no-store，无上游细节泄漏）；
- 上游 4xx → verbatim 透传；上游 5xx/网络 → 503 `upstream_unavailable`；
- 信封超 cap → 413 `response_too_large`；
- transform 池满（admission）→ 503 `transform_busy` + `Retry-After`；
- `?v=` 缺失/错误 → selector 层 400（§2 状态表）；path 缺失 → 400 `invalid_params`。

**资源模型（实现边界；[修订五复审 MAJOR-3 收窄声明，2026-08-22]）**：admission-before-buffering——permit 先于上游 GET，严格许可存续保证**变换段在途工作项（运行+排队）≤ W**，变换段峰值内存 =(A_AMP+1)×W×effective_cap（A_AMP=4 注记系数；config 启动校验验证的即此**变换段**预算）。**边界声明**：transform future 终结即释放 permit，随后 body 所有权转入 ASGI 发送段——已完成响应体在发送期的驻留**不**由 W 约束，与本仓其余全部 `/slimapi` 路由同性质（依赖服务器标准发送背压）；`(A_AMP+1)×W×effective_cap` 是**变换段硬上界**，**不是**进程全量内存上界，不得作此宣称。Content-Length 由框架按实际 body 自动设置（不手工计算——解码前长度≠解码后长度）。

**non-goals**：Range 请求、immutable hash URL、跨请求缓存（no-store 恒定）——设计稿 §4 明示不做。

---

## 附：与设计文档的对应

| 契约节 | 设计权威 |
|---|---|
| §2/§8.3 | design-v4-selector.md |
| §4 全量 | design-v4-dbaux.md（连接/降级/SQL/cursor/等价性） |
| §7 | design-v4-sse-replay.md + design-v4-qp-payload.md |
| §3 能力键时序 | refactor-plan §4.1（n1 冻结） |

*（完）B0-1 产出。定稿条件已执行（2026-08-18）：S-B01 ②③④ owner 终裁收敛，状态行更新为「4.0.0 实施基线（B3a 已落地）」→ B3b 批落地后更新为「B3a+B3b 已落地」（B3b-5，本行）；本文件随 4.0.0 发版定稿。*
