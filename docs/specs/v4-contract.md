# oc-slimapi v4 wire 契约（4.0.0 实施基线 + 2026-08-19 正式修订；修订二：POST 等效动作族——已实施，待发版）

> **状态**：**4.0.0 实施基线（2026-08-18，B3a+B3b 已落地）+ 2026-08-19 正式修订（owner 裁决：修订并入 v4——v4 尚无消费方，修订无破坏影响）**。B0 批冻结全部可观察语义（2026-08-17，rev-6 PASS-with-notes + S-B01 四项 owner 终裁全收敛）；wire 终态 = 4.0.0（`ACCEPTED_CLIENT_VERSIONS` (3,3)→(3,4)）。B3a 批（阶段 A selector 双版本 / B1 dbaux 连接生命周期 / B2 投影 SQL / B3 cursor / B4 路由分叉降级矩阵 / B5 观测）已按本契约落地并全量测试通过；B3b 批（SSE id:/重放、§7 全量、能力键 `sseReplay`/`qpImmediateFull` 广告）**已落地**。S-B01 四项（§7.0）**已全部 owner 终裁（2026-08-17）**；其余章节（含 DB 设计 R1/R2/R3/R6，已凭真库实证冻结——见 design-v4-dbaux §0.2）均为冻结语义。
> **2026-08-19 正式修订范围**（各修订节带**当前状态注记**——现行已发布行为 → 修订后冻结目标，实现批次随后落地）：providers 安全投影（§12）/ session 单查 parity（§13）/ expand 闭环（§14）/ 表示层（§15）/ method 边界与修订 non-goals（§16-§17）/ readiness 门禁（§3.3）。修订仅作用 `?v=4` 视图，`?v=3` 零改动（v3 冻结）；无新 major、无版本窗变更（`ACCEPTED_CLIENT_VERSIONS` 仍 (3,4)）。设计出处：`docs/ocmar/plans/2026-08-19-v4-rebaseline.md` §4-§7（owner 终裁 2026-08-19）。
> **继承基线**：v3 契约（`docs/specs/v3-contract.md`）全量继承 + 本文件逐条差异覆盖——**凡未提及语义逐字沿用 v3**（投影、SSE 帧名帧形、资源上限、错误映射、gzip 族、指纹、catalog、token stream 帧形等）。v4 = v3 的**严格超集面**上的差异层：新增全局 sessions 面（DB 投影源）、SSE id:/重放、directory 于全局列表退役。
> **裁决出处**：`docs/system-architecture-proposal-2026-08-17.md`（v2.2，权威基准，行号引用）；工程细化 `docs/refactor-plans/slimapi-refactor-plan.md`；设计文档 `design-v4-selector.md` / `design-v4-dbaux.md` / `design-v4-sse-replay.md` / `design-v4-qp-payload.md`。
> **消费者**：ocdroid（B5a 探测 / B5b 适配）与 oc-webui 可**仅凭本文件**完成 v4 对接开发。

---

## §0 版本原则与并存退役规则 [冻结]

1. **双版本期**：4.0.0 起 `ACCEPTED_CLIENT_VERSIONS = (3, 4)`（v2.2 行 245）。`?v=3` 语义**逐字节不变**（v3 管线原样）；`?v=4` 启用本契约差异面。无 `v` / `v=3` 以外旧版本 → 400 `unsupported_version`，`supported:[3,4]`（端点存在不 404，沿袭 v3 §2 退役后语义）。
2. **major 与 wire 协议版本绑定**（release.md §1.1 铁律）：版本窗任何变更均为 major 发版（历史例：(3,3)→(3,4) 即 4.0.0 major）。
3. **v3 退役判据（观测性判据，2026-08-19 owner 终态裁决改写）**：协议封顶 4 系、**(3,4) 永久双版本窗口**、原预定 major 退役发版已取消（见 CHANGELOG [4.1.0]）。若未来评估 v3 退役，判据为**纯观测性**：access log `wireVersion` 维度 v3 流量占比持续低于阈值 + SSE active 无 v3 连接（连续观察窗）方启动评估；**退役形式与版本号另行 owner 裁决，本契约不预设任何未来版本窗**。若裁决发生，收窄即 major，写入本节。
4. **消费者回退语义**：503 族 = 显式错误，客户端**不自动回退 v3**（维持当前 wire 版本，按 Retry-After/手动重试处理）。v3 目录级浏览仅经**用户显式触发**的整体版本重协商（`available` 含 3 时覆写 selectedWireVersion=3，全端点一致），且是**功能降级非等价回退**——v4 的跨目录 parent/archived 过滤与全局 cursor 翻页在 v3 无对应语义，UX 按功能降级建模。
5. **2026-08-19 正式修订 [冻结目标]**：owner 裁决修订直接落在 `?v=4` 视图（v4 尚无消费方，无破坏影响；无新 major、无版本窗变更）。修订语义 = §3.3 readiness 门禁（**按 feature ID 独立门控**）+ §12-§17。**契约先行冻结纪律**：各修订节描述冻结目标语义并载**当前状态注记**（现行 4.0.0/4.1.0 已发布行为 → 修订后目标）；实现批次随后落地——各修订面语义**当且仅当对应 feature ID ∈ `satisfied`**（§3.3 门控）方可达，落地前 `capabilities["4"]` 不含对应扩展键、readiness 对应 feature 不 satisfied。`?v=3` 零改动（v3 冻结）。

## §1 头与参数总则 [冻结]

- `X-Slimapi-Version` 头：3.0.0 已删除，v4 维持——出现不解读、不报错。
- `?v=` selector：v3 §2 词法与消费规则不变（sidecar 保留参数，dispatch 层消费剥离，永不转发上游）；支持集扩为 `[3,4]`；多值同值宽容折叠不变。
- 其余保留参数（`directory` 等）按 §5 消费矩阵。

## §2 selector 双版本状态表 [冻结]

设计权威：`design-v4-selector.md`（实现锚点 selector.py 全量对照）。wire 可见状态机：

| 请求形态（`/slimapi/**`） | 判定 | 行为 |
|---|---|---|
| `v=3` | v3 | v3 管线逐字节不变（含 directory 消费 §5） |
| `v=4` | v4 | v4 语义（本契约差异面）；directory 于 §5.2 退役集 → 400 `directory_retired_in_v4` |
| 无 `v` / `v` 词法合法但 ∉{3,4} | 不支持 | 400 `{"code":"unsupported_version","supported":[3,4]}` |
| `v` 词法非法 / 多值不同 | 畸形 | 400 `invalid_version_selector`（v3 §2 词法不变） |
| `GET /slimapi/versions` | 豁免 | 无条件豁免 selector；非 GET → 405+`Allow: GET` 优先于一切（v3 §8.3 ①不变） |

- **request-scope wireVersion**：selector 将本次请求 wire 视图（"3"|"4"）写入 scope state；路由/health/versions 同源读此值，禁止错配组合（S-B04）。v4 能力只能经 selector 显式进入（测试直调缺省 = v3 视图）。
- **directory 消费集版本分叉**：v4 仅将 `^/slimapi/sessions$`（全局列表）移出消费集；`/sessions/status`、`/sessions/{sid}/**`、messages、读组、写组等**全部保留** v3 消费语义（v4 无新语义的路由不动）。
- 观测：`selectorResult` 枚举增 `v4`；`wireVersion` 增 "4"（§9.1）。

## §3 发现端点与能力面 [冻结]

### §3.1 `GET /slimapi/versions`

```
{"current": 4, "available": [3, 4],
 "capabilities": {
   "3": {…v3 既有形状不变…},
   "4": {
     "globalSessions": true,      # B3a 起
     "auxiliaryFilters": true,    # B3a 起
     "sseReplay": true,           # B3b 起已广告（同批落地）
     "qpImmediateFull": true      # B3b 起已广告（同批落地；语义由 design-v4-qp-payload.md 结论冻结）
   }}}
```

- `current` 双版本期恒为最新主版本（=4，S-B04）。
- **能力键为静态键**（v2.2 行 140/254）：存在即广告，**不随 DB 抖动**——DB 熔断/降级不改变 capabilities，瞬态可用性经 503 + health `auxiliary` 字段（§3.2）+ metrics 表达。
- **广告时序（n1 冻结）**：`sseReplay`/`qpImmediateFull` 与实现**同批启用**——B3a 的 `capabilities["4"]` **不含**此二键；B3b 实现落地同期广告（**已执行，B3b-5**：两键随 4.0.0 发布面广告；本条为时序约束的历史记录）。消费者：键缺席 = 该能力不可用，不得预依赖。
- 消费者探测（B5a）：`capabilities["4"]` 不存在 → 继续 v=3；未知键容忍忽略。
- **expand 能力探测注记（2026-08-19 补载——如实描述已发布状态）**：messages 的 2 条 expand 路由（§10）在 `?v=4` 下**可达且行为与 v3 逐字节相同**（selector 放行 + 行为继承 v3，无 v4 分叉）；`capabilities["4"]` 为静态键面，**不含 `expand` 键**——expand 能力广告仅存在于 `capabilities["3"].expand`（12 类目表 + `fragmentMaxBytes`）。客户端探测 expand 可用性应读 `capabilities["3"].expand`，不因使用 `?v=4` 而改读他键。
- **修订扩展键（2026-08-19 修订冻结目标）**：`capabilities["4"]` 随实现批次**加性扩展**两键——`readiness`（§3.3 feature 就绪度门；修订二后全集 U = **十** ID）与 `expand`（§14：categories + fragmentMaxBytes，随 `messages.expand.v4` 进入 `satisfied` 加入）。扩键前本节静态四键即 `capabilities["4"]` 全部形状；`expand` 键出现前 expand 能力探测仍读 `capabilities["3"].expand`（上文注记）。

### §3.2 `GET /slimapi/health` 双视图

- 按请求 wireVersion 返回对应视图：v3 视图 `schema.version=3`/`server.api_version=3`；v4 视图双双 =4（同源同值，S-B04）。
- v4 视图新增瞬态字段：`auxiliary: {available: bool, mode: "db"|"http"}`（v2.2 行 140；available=false 时 mode="http"）；`allowlist: {enabled: bool}`（机制是否启用，B4-4 落地，未配置=false；不泄露清单内容）。
- `ready` 端点形状不变。

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

**feature ID 全集 U（冻结；修订二 9→10 加性扩展）**：

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

- **门控模型（owner 裁决 2026-08-19：按 feature ID 独立门控，冻结）**：每个 feature ID 的修订语义**当且仅当该 ID ∈ `satisfied` 时生效可见**——某 ID 未 satisfied → 该 ID 对应的修订面语义不可达（该面维持 4.0.0 已发布 v4 行为），**不影响其他 ID**。`ready` 仅为**聚合指示器**（下款公式），**不作为全局可达闸门**——`ready:false` 只表示「十项中至少一项未就绪」，不使已 satisfied 的单项语义失效。客户端按所关心的**具体 feature ID** 查 `satisfied`（如启用 providers 投影 → 查 `providers.redacted.v4 ∈ satisfied`），不依赖 `ready` 整体值。**两条例外性说明（修订二，冻结——均非对独立门控的违反）**：① **`method.boundary.v4` 的语义定义** = 「三条 POST 组合在 `session.post-actions.v4` 未激活时的 fallback 405」——其 satisfied 态的可见行为随 post-actions 激活而让位，这是**声明式组合优先级**（§16.3 四位组合表）而非对独立门控的违反（历史基线注记：4.2.0 时 `session.post-actions.v4` 尚不存在，该 405 即 boundary 的完整语义；门控模型其余条款对两 ID 各自其余语义面照常成立）。② **第 10 项未 satisfied 时三条 POST 的行为 = 4.2.0 coded 405**（`method_not_applicable`），非「4.0.0 已发布行为」——历史基线例外：该三组合在 4.0.0/4.1.0 为框架 404（`thin_route_not_found`），4.2.0 起（`method.boundary.v4 ∈ satisfied`）为 coded 405，post-actions 激活后为等效路由（版本演进表见 §16.0 操作表；本款为独立门控条款「未 satisfied → 维持 4.0.0 已发布行为」的显式例外）。
- **`session.post-actions.v4` 语义段（修订二，owner 裁决 q2，冻结）**：门控对象 = §16 修订二的三条 POST 等效动作路由（`POST /slimapi/session/{sid}`、`POST /slimapi/session/{sid}/archive`、`POST /slimapi/session/{sid}/delete`，仅 `?v=4`）。**依赖蕴含（冻结）**：`session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied`（第 10 项依赖第 9 项）——违反该蕴含的发现端点载荷 = discovery contradiction（下款条件⑦）。蕴含动机：`method.boundary.v4` 的语义 = 三条 POST 组合在 post-actions 未激活时的 **fallback 405**（声明式组合优先级，见门控模型条款例外①）；post-actions 激活（∈ satisfied）→ 三条 POST 激活为等效路由（§16.2：POST≡PATCH / POST …/delete≡DELETE / archive 便捷加性），该 fallback 405 对这三条组合不再产生；post-actions 未激活（∉ satisfied）→ 三条 POST = §16.1 coded 405（4.2.0 现行为，门控模型条款例外②）。**为何新 feature ID 而非复用 `method.boundary.v4`（冻结理由）**：该 ID 在 4.2.0 已 satisfied 且语义冻结为「三条 POST → 405」；在同一 feature 下改行为违反 per-feature 门控不变量（satisfied 语义随版本漂移），故以第 10 项加性扩展承载激活期。
- `required ≡ U`：服务端必须以全集发出（**修订二后 U = 十项**；§16-§17 的 non-goals 边界仍编码进该全集——cascade 编排与 cross-session search 永久缺 ID（§17 修订二）；POST 等效动作族经 `session.post-actions.v4` 进入 U（修订二）；无 project-status/Turn/semantic-expand ID）。**规范化规则（比较前双方适用）**：`f(A) = 去重 → UTF-8 字节序排序`；服务端必须以规范化形式发出两数组。**未知 ID（∉ U）拒绝**——不静默忽略；服务端不得发出 U 之外的值。数组元素必须均为 `string`（出现 `null`/非字符串元素 → 按载荷矛盾处理）。
- **`ready` 判定公式（冻结；聚合指示器语义）**：`ready ⇔ f(required) ⊆ f(satisfied)`——`ready` 是派生值，服务端按公式计算并冻结输出，**不允许独立翻转**；`ready:true ⇔ f(required) ⊆ f(satisfied)` 双向等价。`ready` 的用途 = 聚合视图（「修订面全部就绪」的单布尔摘要），单项可达性以上款门控模型为准。
- **与 selector 放行/版本窗的关系（与已发布行为的边界，冻结）**：`4 ∈ available` 与 `capabilities["4"]` 存在自 4.0.0 发布即成立（版本窗事实），**不随 readiness 变化**——readiness 仅门**修订面**（§12-§17 语义，按 feature ID），不门 selector 放行：`?v=4` 的 4.0.0 已发布语义（§2 状态表、§4 全局列表既有行为等）在 readiness 任何状态下持续可用；修订面中已 satisfied 的 feature 语义同样可用，未 satisfied 的单项面维持 4.0.0 已发布 v4 行为。
- **客户端 opt-in 公式（按 feature，冻结）**：对单个 feature `F ∈ U`：`optIn(F) = localV4RevisionEnabled && (4 in available) && (F ∈ capabilities["4"].readiness.satisfied)`——三条件合取缺一不可：本地显式 feature flag（默认关）+ 服务端版本窗事实 + 该单项就绪。`current`、静态四键、`available`、`ready` 聚合值单独出现均不构成任何单项 opt-in；`current == 4` 仅为信息性（不是 opt-in 条件、不是 readiness 信号）。
- **`expand` 键双向不变量（冻结）**：`expand` 键存在 **iff** `messages.expand.v4 ∈ satisfied`（§14）。四种组合穷尽：① `expand` 存在且 `messages.expand.v4 ∈ satisfied` = 唯一合法出现态；② `expand` 缺席且 `messages.expand.v4 ∉ satisfied` = 合法（feature 未就绪的过渡态）；③ `expand` 存在而 `messages.expand.v4 ∉ satisfied` = contradiction（未就绪却广告能力）；④ `expand` 缺席而 `messages.expand.v4 ∈ satisfied` = contradiction（就绪却不广告，能力探测断链）。另：`readiness` 键缺席而 `expand` 键存在 = contradiction（expand 能力必须在 readiness 框架内广告，此态下 `satisfied` 不可评估）。`expand` 键存在而形状非法（`categories` ≠ §14 十二项有序清单 / `fragmentMaxBytes` 非 number）→ contradiction（下款）。未知额外 key 仍按「消费方忽略未知字段」（§3.1 载荷约束）处理，不误判。
- **`discovery contradiction`（单一结局，客户端侧分类——不定义新服务端错误码，发现端点载荷本身仍 200）**。适用于 **`readiness` 或 `expand` 任一键已出现**的载荷（两键均缺席的静态四键现载荷合法）。任一命中 → 一律归为单一结局：① `ready` 与规范化子集判定不一致（任一方向：`ready:false` 而 `required ⊆ satisfied`，或 `ready:true` 而不满足包含）；② `required ≠ U`（规范化后）；③ 任一数组含未知 ID（∉ U）；④ 数组非 `string[]` 或形状不可解析；⑤ `expand` 键与 `messages.expand.v4 ∈ satisfied` 任一方向不一致（含 `readiness` 缺席而 `expand` 存在）；⑥ `expand` 键存在而形状非法；⑦ `session.post-actions.v4 ∈ satisfied` 而 `method.boundary.v4 ∉ satisfied`（违反修订二依赖蕴含，语义段冻结款）。**结局**：客户端不得使用修订面语义（维持 4.0.0 已发布 v4 行为），不得从 `current` 推断 readiness/能力，按运维渠道上报。**边界（不属于 contradiction）**：`satisfied` 含某 feature ID 而对应路由行为未升级，属**实现 bug**（服务端违约，按运维渠道修复），不属本清单载荷矛盾——contradiction 仅指 `/versions` 载荷自身形状/一致性的可判定违约。**修订二过渡注记（required 9→10）**：实施批次将 `required` 扩为十项后，仅识别旧九项全集的客户端会把第 10 项 `session.post-actions.v4` 按「未知 ID（③）」判 contradiction——v4 视图现无消费方（ocdroid/WebUI 均在 `?v=3`），无实际影响；客户端应随实施批次同步十项全集。反之，扩项前的 4.2.0 服务端载荷（required = 九项）按其发布时契约合法，不按本修订追溯判矛盾。

## §4 `GET /slimapi/sessions`（v4 全局会话目录）[冻结]

数据源模型（v2.2 §3.1 裁决）：**DB 投影源为常态路径**（上游 SQLite `session` LEFT JOIN `project` 只读投影）；上游 HTTP `/experimental/session` = **schema 权威 + 降级路径**（等价性锚定见 §11.8）。

### §4.1 参数矩阵

```
GET /slimapi/sessions?v=4
    &archived=omit|only|all     # 三态，默认 omit
    &parent=all|none|only|<sid> # 四态；省略 = all（显式冻结，v2.2 行 65）
    &search=<title-substring>   # 标题字面子串（§4.6）
    &cursor=<opaque>            # keyset best-effort（§4.5）
    &limit=1..500               # v4 域（v3 保持 1000）
    → 200 {items: SessionSkeletonV4[], nextCursor: string|null,
           complete: bool, degraded?: true}
```

| 参数 | v3 请求 | v4 请求 |
|---|---|---|
| `archived` / `parent` / `cursor` | **422**（未知参数显式拒绝，不依赖框架默认忽略） | 本表语义 |
| `roots` / `start` | 现状语义（roots 由 `parent=none` 精确承接，v2.2 行 135） | **422**（参数版本不匹配，S-B04） |
| `directory`（任何形式） | 现状消费 | **400 `directory_retired_in_v4`**（§5） |
| `search` / `limit` | 现状 | v4 语义（limit 上限 500） |

- `parent=only` 谓词 = `parent_id IS NOT NULL`（v2.2 未冻结，B0 **实证冻结**：真库 parent_id NULL 86 / NOT NULL 321 / 空串 0——无空串哨兵歧义，design-v4-dbaux §0.2 R6）。
- **SessionSkeletonV4**：v3 `SESSION_KEYS` 投影（已含 directory）+ `project` 对象（`{id, name, worktree}`——三列均已进 DB 投影 SELECT、schema 门与等价性 golden；join 缺行 → null）+ v4-only 字段。列名以真库实证为准（`tokens_input/tokens_output`，v2.2 行 72 模板 `tokens_in/out` 为撰写笔误——**B0 实证冻结**，design-v4-dbaux §0.2 R2）。
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
| 参数版本不匹配 | 422 | v4 收 roots/start；v3 收 archived/parent/cursor |

### §4.4 ETag

v4 sessions **无 ETag/Vary/304**（v2.2 行 254 §6）；v3 全表面 ETag 原样。ETag validator 版本隔离：v3/v4 validator 互不匹配（v4 其他路由若产 ETag，前缀隔离）。**（2026-08-19 修订目标见 §15：v4 sessions 列表增 ETag + 全 v4 路由 `Vary: Accept-Encoding` 修正——当 `representation.vary.v4` 进入 `satisfied`（§3.3 门控）时生效；生效前本条发布态继续成立。）**

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

### §5.1 v3（不变）

v3 §5 全部消费/容忍/错误语义逐字沿用。

### §5.2 v4

| 路由 | v4 × directory |
|---|---|
| `GET /slimapi/sessions`（全局列表） | **整体退役**：query（单值/多值）、header（任何形式）、query+header 混合 → 一律 **400 `directory_retired_in_v4`**（selector 层拦截，先于路由；不泄露目录存在性） |
| `/slimapi/sessions/status`、`/sessions/{sid}/todo|children|diff|stream`、messages×5、agent、command、读组×10、写组×5 | **v3 消费语义原样**（query 单值消费剥离；header 退役 400；多值 400） |

- **allowlist 作用域全覆盖**（v2.2 行 186，B4-4 落地）：非空时全局 sessions 列表（DB SQL 谓词）、directories 列表、digest/q/p 帧、事件流均过滤非白名单目录；`/slimapi/file/**` fail-closed（空 → 403 `directory_not_allowed`）。allowlist 三态（未配置/显式空/非空）语义见 B4-4（P1 3.3.0 起）。
- **修订二加注（`session.post-actions.v4 ∈ satisfied` 时生效，§16）**：三条 POST 等效动作路由的 directory 消费 ≡ 各自等效目标路由——`POST /slimapi/session/{sid}` 与 PATCH 同路径（消费集既有行，query 单值消费剥离 → header；多值/header-only 违约梯子 §5.1 适用）；`POST …/archive`、`POST …/delete` 的等效上游目标为消费集路径 `/session/{sid}`（上游写端点声明 WorkspaceRoutingQuery），同样按「query 单值消费剥离 → header、§5.1 违约梯子」消费，v4 无 retirement 差异（非全局列表路由）。门控未激活（过渡态）期间此三组合在 selector 层 405 先行（§8.3 插列），不触达消费判定。

## §6 ETag / Vary / 304 [冻结]

v3 原样（§4.4 已含 v4 差异：v4 sessions 无 ETag）。

## §7 SSE id: / 重放（v4-only）[四项已全部 owner 终裁，2026-08-17]

> 设计权威：`design-v4-sse-replay.md`（协议矩阵用例表 + 状态机全文）。本节为 wire 可见语义，**已随 B3b 批落地（B3b-2，全量测试通过）**。**v3 SSE 帧名帧形零变化**（v2.2 行 153 冻结）——id:/重放仅 v4 生效；v3 客户端无感知。能力键 `sseReplay`/`qpImmediateFull` 已随 B3b 落地并广告（§3.1）。

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
- **上游断连恢复（触发条件冻结）**：**首次确认上游 loss 即触发**（EOF/异常路径为主，`_upstream_loss_notified` 防重；成功重连仅作未通知时兜底——v3 现行为延续，`global_hub.py:894-904/913-922/847-863`）→ 对全部存量订阅者 fanout `resync{reason:"reconnect_no_replay"}`（无 id）→ 恢复后新帧（seq 继续单调不重置；**epoch 不变**）；token 域另清空该 sid pending live 缓冲（`tokenstream/hub.py:1896-1900` 锚点）。**持久 barrier（S-B01④已裁决冻结，owner 2026-08-17；low-watermark 数据结构为实现细节）**：上游 loss 时写 low-watermark barrier（水位 = 该域已发布 max seq）——**写入范围 = 全局域 + 当前 epoch 内全部已创建 per-sid 域（不限在线订阅者）**；后续任何 `Last-Event-ID` seq **≤** barrier 水位的重连（含断连期间离线的客户端）一律 `resync{reconnect_no_replay}`（水位本身对应的帧亦发布于缺口前），seq > 水位 → 走完整第④级分类（**future**（同 epoch 且 seq > 已发布 max）→ 忽略 + 重置按首连；否则窗口内 replay / `replay_expired` / `replay_gap`，§7.2 上文分类）；**禁止跨 barrier 补帧**（barrier 前后存在 sidecar 未观察到的上游事件缺口，窗口内连续不构成补帧依据）。barrier 不受 count/bytes/TTL 逐出（仅窗口下界严格越过后可删；域回收保留失效水位或 fail-safe resync；进程重启归 `epoch_changed` 拦截）；客户端 HTTP 全量对齐。
- gap 处理：区分「日志逐出」（→ resync）vs 合法缺席（单一/per-sid 域下不存在跨域合法空洞）。**snapshot 不是服务端帧**——resync 后客户端自行 HTTP 全量对齐（全局域如 `/slimapi/sessions` 首屏、token 域重拉消息投影），服务端只发 meta → resync → 新帧。逐出-发布并发的边界 gap 误判风险为实现期待验证项（design-v4-sse-replay.md §5 待裁决 5，可降级防御分支，不影响 wire 语义）。
- 背压：溢出帧**入**重放日志（日志记录「已发布帧」而非「已送达帧」）；订阅端溢出断连 → 重连走 Last-Event-ID 重放。
- **resync 帧 reason 值域（v4 冻结，加性扩展）**：`epoch_changed` | `replay_expired` | `replay_gap` | `reconnect_no_replay`（既有）；token 流 tombstone（消息已撤销）在 replay 时**照常消耗其 seq 并以 `message.removed` 轻量撤销帧回放**（既有帧形 `tokenstream/frames.py:137-151` = `event: message.removed` + `{sessionID, messageID}`；保留 `id:`，维持 ID 序列无空洞）。
- meta 恒首帧（meta-first 不变）；v4 meta additive 扩展：capabilities 摘要 + epoch/seq 基线字段（v3 形状不动，B3b-4；**完整字段集与字段序见 §7.5**）。

### §7.3 tokens=1（已裁决终态）

- `/events?tokens=1`（v4）→ 400 `{"code":"tokens_stream_retired_in_v4","hint":"token 流请使用 /slimapi/sessions/{sid}/stream"}`；v3 请求该参数语义不变。（**一致性注记，B3b-5**：已核对实现错误体与本条逐字一致——`routes/events.py::TOKENS_STREAM_RETIRED_IN_V4`。）
- token 流端点 `/slimapi/sessions/{sid}/stream`：v4 起分配独立 id:（§7.1）；directory 消费保留（§5.2）。

### §7.4 q/p 帧载荷（`qpImmediateFull` 语义）

- 逐字段核对结论（`design-v4-qp-payload.md`，B0-4 产出）：**已完整**——sidecar `properties` = 上游 event.data 原样透传（`event-v2-bridge.ts:39-44` 构造 → `global_hub.py:522,529` 零裁剪 → 上游 `core/question.ts:93-110`、`permission.ts:164-174` 发布完整 Request）；`question.asked`（10 字段）与 `permission.asked`（10 字段）逐字段比对**无缺失、无改名、无裁剪**。EventV2 envelope 字段（evt_ id/metadata/durable/location）v3 契约本就不进 properties，不属于缺失。
- **`qpImmediateFull` 语义冻结 = 现状已成立**：B1b 零 wire 变更，webui/ocdroid 直投为纯客户端改动；不触发 B3b-3 补全路径（该任务留空）。两套字段表（上游完整直投字段集 / 最小可渲染字段集）以 design-v4-qp-payload.md §2/§3 为权威，随实现批同步引用。
- digest 帧跨版本注记：B1a（P1 3.3.0）起 `session.digest` 增可忽略字段 `changed:[sid…]`（**最小语义已裁决**：changed = [本帧 sid]——digest 为 per-sid 逐帧产出，帧出现即 changed；形状保留列表为未来聚合留形），v4 帧形沿用。

### §7.5 与 v3 §7 的同步语义（2026-08-19 补载 [冻结]）

以下语义 v3/v4 两视图一致（继承 v3 §7，逐条现状载明）；v4 附加差异单独标注：

- **digest `status` 恒字符串**：上游 `session.status` 的 status 字段可能以字符串（`"busy"`）或对象信封（`{"type":"busy"}`）两种 wire 形态到达；sidecar 统一归一化——digest `status` 字段**恒为字符串**（busy/idle 等上游状态值原样）；信封无效（缺 `type` / `type` 非字符串）时该次状态更新被忽略（digest 其余字段不受影响）。两视图一致。
- **digest `lastError` sticky 清除语义**：`session.error` 携带 sessionID → 该 sid digest 记 `lastError:{name, message, at}`（`name` 截断 128 字符）并立即定向 flush + 记入 sticky 存储；该 sid **下一次 `session.status=busy`** → sticky 弹出 + digest **显式 `lastError:null` 清除帧** + 定向 flush（busy 判定对字符串/对象信封两形态一致）；`session.deleted` → sticky 弹出 + 字段省略；后续 flush 在本窗口未自行设置/清除时合并 sticky 值（贴回语义，直到 busy 清除帧）；**sticky 仅在同一 sidecar 进程生命周期内成立**（进程内内存态、无持久化，重启即丢、不复活不重贴；重启后新 `session.error` 才重新记录）；FIFO 容量上限 10,000 sid，逐出后不再贴回。两视图一致（全文语义同 v3 §7.5）。
- **SSE 恒 identity**：两端点 SSE 流不做 gzip/content-encoding，响应**无 `Vary` 头**（响应头 = `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no`；SSE 路径不参与 `Accept-Encoding` 内容协商）。两视图一致。
- **digest 水位定位与 catch-up 盲区（after 游标等效方案裁决，2026-08-19 冻结）**：上游 `MessageV2.page()` 仅 `before` 向后 keyset（v1.18.18 实证），v4 messages 亦无 after 游标；增量 catch-up 等效方案 = **digest 触发 + 条件重拉**（水位仅当触发器不当过滤器；两盲区：`message.removed` 不进 digest、SSE 断连窗口无补偿；双轨消费 = digest 触发 If-None-Match 精拉 + 低频周期 304 对账兜底，重启/epoch 变化视为全失效）。全文语义同 v3 §7.8；v4 重放（§7.2 `id:`/Last-Event-ID）可缩小断连盲区但不消除（逐出/barrier 仍 resync），周期对账在两视图均为必选。两视图一致。
- **v4 附加——meta 首帧字段集（v3 形状不动）**：v4 视图首帧 `event: slimapi.meta` data 字段序 = `subscriberId, tokens, capabilities, epoch, seqBase`；v4 追加三键：`capabilities: {"sseReplay": true}`（**恒此一键**——`qpImmediateFull` 仅广告于 `GET /slimapi/versions` 的 `capabilities["4"]`，不入 meta 帧）、`epoch`（进程代，16 hex 字符串）、`seqBase`（连接建立时该域已发布最大 seq，整数；首连后首个带 `id:` 帧恰为 `seqBase + 1`）。meta 帧自身**无 `id:`**（§7.0②）。
- **v4 附加——welcome 帧抑制**：v4 连接不产出连接本地 `server.connected` 首帧（v3 照旧产出）；v4 线上首帧恒为 `slimapi.meta`。

## §8 错误族与优先级 [冻结]

### §8.1 新增错误码

| code | 码 | 场景 |
|---|---|---|
| `directory_retired_in_v4` | 400 | §5.2；统一错误体 + hint，不泄露目录存在性 |
| `tokens_stream_retired_in_v4` | 400 | §7.3 |
| `invalid_cursor` | 400 | §4.5 |
| `auxiliary_unavailable` | 503 | §4.2；附 `Retry-After: 30`；错误体不含 DB 路径/schema/白名单内容 |
| 参数版本不匹配（`v3 收 v4 参数 / v4 收 v3 参数`） | 422 | §4.1 |

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

**复用既有码**（不重定义、不改语义）：`response_too_large`（providers 源 body 超限，§12.5.2 ④）、`upstream_http_<N>`（providers 3xx/4xx 转换路由映射）、`upstream_unavailable`、`transform_busy`、`auxiliary_unavailable`（§13 整响应失败复用——扩展触发面不改 status/body/`Retry-After` 形状）、`invalid_cursor`、`session_not_found`、`thin_route_not_found`、v3 §4b expand 错误族。`discovery contradiction`（§3.3）为客户端分类结局，非服务端错误码。**优先级插列**：修订生效后 §8.3 总链在「② selector version 族 400」与「③ selector directory 族 400」之间插列 method 405（§16——判定不依赖 query 参数）。

## §9 观测 [冻结]

### §9.1 维度扩展

- access log / traffic snapshot：`selectorResult` 增 `v4`；`wireVersion` 增 "4"；SSE active 维度同步扩。
- DB 辅助指标（B3a-B5）：查询延迟（P50/P99）、降级计数、熔断计数、重探事件、inode swap 事件。
- replay 指标（B3b）：hit/miss/gap/resync 计数。

### §9.2 bucket

v4 sessions 归入 sessions 桶既有记账；降级路径请求带 degraded 标记维度（可区分 DB/HTTP 源）。

### §9.3 运维信号

`/slimapi/health` `auxiliary.available=false` = DB 辅助禁用/熔断（runbook 见 operations.md §7：升级 opencode 后第一步观察）。

### §9.4 v3 退役判据（P4）

**观测性判据（2026-08-19 owner 终态裁决）**：协议封顶 4 系、(3,4) 永久双版本、原预定 major 退役发版已取消（CHANGELOG [4.1.0]）——当前**无预定退役版本**。评估触发条件 = `wireVersion` 维度 v3 流量占比持续低于阈值 + SSE active 无 v3 连接（连续观察窗）；是否退役、退役形式与版本号届时另行 owner 裁决（§0.3）。

## §10 路由全集逐条（v4 差异列）[冻结]

**51 条** /slimapi 路由（read **26** + write **17** + SSE 2 + 发现/运维 **6**；**计数方法 = 路由 × 方法表行**，与 `scripts/check_routes_doc.py` 的路由↔INTERFACE_MAP 一致性校验同口径——2026-08-19 修订，取代原「45 条（read 23 + write 12 + 发现/运维 8）」旧计数）。**已发布（4.0.0/4.1.0）v4 差异仅下列 4 条**，其余 **47** 条 v4 = v3 语义原样（经 selector 分派）；**2026-08-19 正式修订（冻结目标）追加差异面见本节末修订块与 §12-§17**；**修订二（owner 裁决 2026-08-19，已实施——write 组三条 POST 等效动作路由已激活，`session.post-actions.v4 ∈ satisfied`）**：

| 路由 | v4 差异 |
|---|---|
| `GET /slimapi/sessions` | §4 全量（DB 投影源/参数矩阵/降级矩阵/cursor/无 ETag）；directory → 400 |
| `GET /slimapi/events` | §7：v4 分配 id:/重放；`tokens=1` → 400；directory 消费不变（events 非消费集路由，目录帧过滤随 allowlist） |
| `GET /slimapi/sessions/{sid}/stream` | §7：v4 分配独立 id:（token 流）；directory 消费保留 |
| `GET /slimapi/versions` | §3.1 双版本载荷 |

`GET /slimapi/health` 双视图（§3.2）为响应差异，路由行为不变。messages（4 条 + 2 expand）、sessions/status、todo/children/diff、directories、agent、command、file/vcs/find/config/session-single/context（读组）、active、global/health、metrics、ready、actions（2）、write 17 条：**零 v4 差异**。其中两处显式注载（2026-08-19 补载）：

- **`GET /slimapi/session/{sid}` 单查**：`?v=4` 下**不升级 v4 骨架形状**——恒返回 v3 skeleton 投影（SessionSkeletonV4 仅用于 `GET /slimapi/sessions?v=4` 列表项；单查无 v4 分叉）。v4 客户端取单会话 v4 骨架走 `/slimapi/sessions?v=4` 列表。
- **`GET /slimapi/sessions/status`**：零 v4 分叉（无版本分支代码路径），directory 消费继承 v3（§5.2 表行：query 单值消费剥离、头出现 400、多值 400）；上游 `/session/status` 数据与 directory 无关（恒全局 map），`directory` 仅作 workspace 路由通道。
- 2 条 expand 路由的可达性与能力探测口径见 §3.1 注记（`capabilities["4"]` 无 `expand` 键）。

**2026-08-19 正式修订追加差异面 [冻结目标——逐项当对应 feature ID 进入 `satisfied`（§3.3 门控）时生效；生效前上述发布态注载继续成立]**：

| 路由 / 面 | 修订差异（仅 `?v=4`） |
|---|---|
| `GET /slimapi/config/providers` | §12 安全投影：白名单 schema + 嵌套递归丢弃 + 四限额 + 三带错误面 + ETag/canonical 口径；`?v=3` 恒透传不变 |
| `GET /slimapi/session/{sid}` | §13 单查 parity：升级为与列表同源 canonical SessionSkeleton 形状（dbaux 点查优先 + whole-response native fallback）；`?v=3` 恒 v3 skeleton |
| messages 投影 expandRefs href + 2 条 expand 路由能力 | §14：href 按 wire 视图生成（`?v=4` 响应 → `?v=4`，`?v=3` → `?v=3`）；`capabilities["4"]` 增 `expand` 键 |
| `GET /slimapi/versions` | §3.3：`capabilities["4"]` 增 `readiness`（+随批 `expand`）扩展键 |
| v4 表示层 | §15：v4 sessions 列表增 ETag（identity 强 / gzip 弱 `W/`）；全 v4 路由 `Vary: Accept-Encoding` 修正（现行 `_v4_json_response` 删 Vary 为已知 bug） |
| 三条 deferred method-path 组合 | §16：`?v=4` 精确 405 `method_not_applicable`（`Allow` 头 + coded body + 不转发 + no-store）——**修订二后为过渡态行为**（见下行，`session.post-actions.v4` 激活后 405 面让位） |
| `POST /slimapi/session/{sid}`（新） | §16 修订二：`session.post-actions.v4 ∈ satisfied` 时激活为 **PATCH 等效路由**（同一 PatchPayload 透传、逐字节等效受控写管线）；`?v=3` → 404 `thin_route_not_found` 现状不变 |
| `POST /slimapi/session/{sid}/archive`（新） | §16 修订二：同门控激活；body 可选（合法 PATCH body 透传 / 缺省 sidecar 合成 `{"time":{"archived":<now epoch-ms>}}` 走 PATCH 等效管线）；`?v=3` → 404 现状不变 |
| `POST /slimapi/session/{sid}/delete`（新） | §16 修订二：同门控激活；**DELETE 等效路由**（请求实体处理与 DELETE 完全相同并原样转发——读取实体、同 cap 413、Content-Type 透传、body 逐字节转发，无忽略分支，§16.2-b；上游递归删子+吞错语义如实继承，非幂等可接受——owner q1）；`?v=3` → 404 现状不变 |

其余路由维持零 v4 差异；**计数方法（路由 × 方法表行）不变——4.2.0 已实现 51 条，修订二实施后 54 条（write 20）**。

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

## §12 Provider 安全投影（`GET /slimapi/config/providers?v=4`）[2026-08-19 修订冻结]

> **当前状态**：现行该路由 `?v=3` / `?v=4` 行为相同（legacy provider map 受控代理 + 既有 ETag，§10 发布态「零 v4 差异」读组）。本节为 `?v=4` 冻结目标——当 feature `providers.redacted.v4` 进入 `satisfied`（§3.3 门控）时生效；`?v=3` 恒透传不变（v3 冻结）。

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
}
```

- **顶层恰好两 key**：`providers` + `default`；**多余/缺失顶层 key = malformed**（不猜测、不部分转换）。
- **嵌套规则（冻结）**："Unknown provider/model fields are discarded recursively"——provider/model 内未知字段**递归丢弃**（丢弃不报错）；仅顶层受 exact-two-key 约束。
- **确定性丢弃清单**（上游存在但不进投影）：`Info.env`/`Info.key`/`Info.options`；model 的 `api`/`capabilities`/`cost`/`limit`/`options`/`headers`/`release_date`；variant 内除 map key 外一切（`name`/`status` 等不进 wire）。
- **optional 字段策略（冻结）**：`source`/`status` 为 absent / null / 非 string → **省略该字段**（不报错、不猜测、无发明枚举）——任何响应绝无 `"source": null`/`"status": null`；`variants` **absent → 省略键**，**存在则必须为 map（object）**，非 map = malformed（§12.5.3）。**字段策略维度的 malformed 全部来源 = required 字段（provider `id`/`name`/`models`，model `id`/`name`/`providerID`）缺失或错型，加上唯一一条 optional-key 错误路径：`variants` 存在但非 map**——除上述来源外，`source`/`status` 的任何上游形态都不产生错误（一律省略该键）；同一输入在合规实现间**唯一结果**。
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
② directory 消费/校验（400 族——providers 路由的 directory 规则出处为 v3 §10.a C1 消费集
   （providers=WorkspaceRoutingQuery）与 §5 directory 消费矩阵）
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

**offload 边界（冻结）**：⑥-⑪ 的**全部 CPU 工作**（JSON 解码、重复 member 检查、schema 校验、投影、限额、canonical 序列化、body 限额、**gzip 协商判定与压缩**）**只在该 transform worker job 内执行，绝不在 event loop 上执行**（v3 §10.a 既有纪律延续；不存在「8 MiB body 在 event loop 压缩」的合法路径）。③-④ 为异步上游 I/O + 流式字节计数，留在主上下文——**网络等待不占 transform 配额**。⑤ = **transform permit 获取**：permit 语义 = worker/CPU 配额（门的是 ⑥-⑪ 的 CPU 工作，不是上游 I/O）；获取时机 = body cap 检查通过之后、worker job 提交之前——**permit 持有期 = worker job 生命周期**（job 结束即释放），网络等待期不持有。因此 **503 `transform_busy` 仅可能发生在 ⑤ 提交点**（数据级错误 ③/④ 先于 permit 判定；这与 v3 §4b.3 ③ 「准入先于上游工作」的语义差异是本路由的显式设计——上游 I/O 不消耗 CPU 配额，先做完 I/O 与 cap 检查再竞争 permit）。⑫ 回主上下文仅做 conditional 判定与响应发射。

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
- **REP_VERSION 域隔离**：含 wire-view 标记 + 投影/配置指纹——v3 validator 与修订后 v4 validator 互不匹配；修订切换（透传 → 投影）本身经 REP_VERSION 投影版本轮换全部 validator（v2 §6.2 机制继承），构造上不可能误 304；上游自身 ETag 不透传（sidecar 生成域，v3 §6.1 不变）。
- **`Vary: Accept-Encoding` 强制**（§15）；`If-None-Match`/`*`/judge 三态沿用 v3 §6 冻结行为；200/304 均 `Cache-Control: no-store`（ETag 仅省下行传输字节，管线照跑，非缓存授权）。
- **ETag 开关**：继承 `OC_SLIMAPI_ETAG_ENABLED`（缺省开）。关闭 → 本路由无 `ETag`、无 304 判定；**`Vary: Accept-Encoding` 仍发**——表示可变性与 ETag 正交。readiness 不受该开关影响：`representation.vary.v4` 恒可满足（§3.3）。

## §13 Session 单查 parity（`GET /slimapi/session/{sid}?v=4`）[2026-08-19 修订冻结]

> **当前状态**：现行 `?v=4` 单查恒返回 v3 skeleton 投影（§10 发布态注载——单查无 v4 分叉）。本节为冻结目标——当 feature `session.single.projection.v4` 进入 `satisfied`（§3.3 门控）时生效：单查升级为与 v4 列表同源 canonical 形状，列表 item 形态与本节对象同批统一；`?v=3` 恒 v3 skeleton（v3 冻结）。

单查与全局列表 `GET /slimapi/sessions?v=4` 共用**同一 canonical 对象**；列表请求参数矩阵（`archived`/`parent`/`search`/`cursor`/`limit` 1..500）、cursor 语义、降级判定（何时允许 fallback、何时 503 `auxiliary_unavailable`，含 `Retry-After: 30`）**继承 §4 冻结**——本节在其上统一 item 投影并叠加标记语义。单查 `directory` 消费语义沿 v3（单查为 v3 消费集路由：query 单值消费剥离、头退役 400、多值 400）；未命中 → 404 `session_not_found`（既有）；native 4xx 逐字 / 5xx/网络 → 503 `upstream_unavailable`（继承）。

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

修订形态 = §4.1 发布态字段集（v3 `SESSION_KEYS` 投影 + `project` 对象 + **tokens 五列平铺 v4-only 字段**，§4.1 R2 真库列名实证：`tokens_input/tokens_output/tokens_reasoning/tokens_cache_read/tokens_cache_write`）+ **`partial`/`degraded` 两个 item 标记** + envelope `degraded` 改 **required 布尔**。单查响应 = **裸 `SessionSkeletonV4` 对象**（无 envelope；响应级 degraded 即 item 自身 degraded，§13.4 公式平凡成立）。无 effectiveStatus / subagentList / Turn / cost 聚合 / exact-merged / generic fragment 字段（§17）。

### §13.2 字段真值表（requiredness / null / absent + fallback 可表示性，冻结）

| 字段 | requiredness | null / absent 语义 | dbaux 来源 | native fallback 来源 / 行为 |
|---|---|---|---|---|
| `id` | required | 永不 null | session 行 | native session id；不可得 → **整响应失败**（§13.2a） |
| `directory` | required | 全局列表强制**非空字符串**；单查继承 v3 directory 消费（显式传入仍经 v3 校验，非法值 → 400 `invalid_directory` 族） | session 行 | native directory（同值，禁跨源合并）；不可得 → **整响应失败** |
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

## §14 expand href 与能力闭环 [2026-08-19 修订冻结]

> **当前状态**：现行 expandRefs href 硬编码 `?v=3`（`skeleton.py` `_expand_ref`）——`?v=4` 响应亦发 `v=3` href（§3.1/§10 发布态注载）；`capabilities["4"]` 无 `expand` 键。本节为冻结目标——当 feature `messages.expand.v4` 进入 `satisfied`（§3.3 门控）时生效。

- **12 类目有序清单（原样照抄 v3 §3/§4b.2，冻结）**：`info_summary_diffs` → `part_text` → `part_reasoning` → `part_state_output` → `part_state_error` → `part_state_input_full` → `part_state_metadata_full` → `part_state_attachments` → `part_url` → `part_source` → `part_snapshot` → `compaction_full`（单一事实源 `src/oc_slimapi/traffic.py::EXPAND_CATEGORIES` 表序延续；versions 广告与流量记账同源）。
- **`fragmentMaxBytes` = `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 运行时值**（缺省 **8388608**，界 **1024..33554432** 含边界；非法值启动 RuntimeError——config.py 既有冻结）。capability 广告 `capabilities["4"].expand = {categories, fragmentMaxBytes}` 与 v3 §3 形状同构；**仅当全部 12 类目在 v4 视图闭环（href/响应/错误）才广告**（§3.3 批次闭合）。
- **href canonical 形态（冻结）**：`GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=<selector>[&directory=...]`（part 级含 `/{partID}`）。query 键序：**`v` 第一、客户端追加 `directory` 第二、无其他 key**；`v` 值来自**解析后 selector**——`?v=3` 请求的响应 → `v=3`（v3 冻结，修订不触碰）；`?v=4` 请求的响应 → `v=4`（本节修订）；经既有 query 编码**恰编码一次**。
- 端点求值序（含 transform 准入先于 part 级错误）、错误码族、响应 envelope（`{"category","messageID"[,"partID"],"data"}`）、`Cache-Control: no-store` / `Vary: Accept-Encoding` / 无 ETag——全部继承 v3 §4b 冻结语义，本修订不另立。

## §15 表示层：Vary 规则与 v4 ETag [2026-08-19 修订冻结]

> **当前状态**：现行 v4 sessions 列表 200 响应**无 ETag 且显式删除 `Vary`**（`sessions.py` `_v4_json_response`——`json_response` 基线为 gzip 协商统一盖 `Vary: Accept-Encoding`，该 helper 将其删除；**已知 bug/风险项**，§4.4 发布契约载「v4 sessions 无 ETag/Vary/304」）。本节为冻结目标——当 feature `representation.vary.v4` 进入 `satisfied`（§3.3 门控）时生效。

- **`Vary: Accept-Encoding` 直接规则（冻结）**：凡可随 `Accept-Encoding` 变化的 v4 表示（200/304）**必带** `Vary: Accept-Encoding`。修订修正上述删除 bug——全 v4 路由正确发 Vary（v4 sessions 列表含 body gzip 协商，无 Vary 声明即缓存不正确）。SSE 帧化为**显式例外**（§7.5：SSE 恒 identity、无 Vary）。
- **v4 sessions 列表 ETag（新增）**：canonical 口径与 §12.6 同规则——canonical identity bytes 即 wire body；identity 强 validator `"<sha256hex>"` / gzip 弱 `W/"<sha256hex>"`；hash 输入 = `REP_VERSION + NUL + coding + NUL + canonical identity body`。`If-None-Match`/`*`/judge 三态沿用 v3 §6；304 头集合 = `ETag` + `Vary` + `Cache-Control: no-store`（sessions envelope 自含 `nextCursor`/`complete`，无 aux 头）；200/304 均 `no-store`。
- **`merged_vary` 说明（如实）**：现行 `merged_vary`（`etag.py`）将任意输入折叠为单值 `"Accept-Encoding"`——v4 修订表示层**恰好只需该单值形态**（directory Vary 值已随 §1 头退役移除，v4 无其他 Vary token），无需扩展 helper；如未来出现多 token 合并需求，须先扩展该 helper 并保持 v3/v4 既有输出逐字节不变。
- **域隔离（冻结）**：缓存键 / singleflight 键 / ETag REP_VERSION 均含 wire-view 标记——v3 与修订后 v4 validator 互不匹配，跨视图 `If-None-Match` 保守 200；修订切换（无 ETag → 有 ETag）经 REP_VERSION 投影版本轮换，不可能误 304。
- v4 sessions 以外的 v4 路由：ETag/Vary 语义与 v3 发布态一致（§6「v3 原样」延续）；providers 路由修订后按 §12.6 口径。

## §16 POST 等效动作族 + method 边界 [2026-08-19 修订冻结；**修订二：POST 等效动作族——已实施，待发版**]

> **当前状态**：修订一（feature `method.boundary.v4`，v4.2.0 已 `satisfied`）已落地——三条 POST 组合在 `?v=4` 下返回精确 405 `method_not_applicable`（§16.1，现行为）。**修订二**（owner 裁决 2026-08-19，新 feature `session.post-actions.v4`，§3.3 第 10 ID）为本节冻结目标：该 ID 进入 `satisfied` 时三条 POST 激活为等效路由、§16.1 的 405 拒绝面按**声明式组合优先级**让位（§16.2/§16.3 四位组合表；依赖蕴含 `session.post-actions.v4 ⇒ method.boundary.v4` 见 §3.3）。**全部仅 `?v=4` 视图；`?v=3` 冻结零改动**（三组合在 v3 → 404 `thin_route_not_found` 现状，任何阶段不变）。**加性并存，非替代**：PATCH/DELETE 在 v3/v4 均继续可用，v4 继承不退役。

| 操作 | V3 | V4（`session.post-actions.v4 ∉ satisfied`，v4.2.0 现行为） | V4（`∈ satisfied`，修订二冻结目标） |
|---|---|---|---|
| 更新 session（title/metadata/permission / `time.archived`，双 shape） | PATCH（发布语义） | **PATCH 继承**（applicability 行显式声明，非路由 fallthrough） | PATCH 继承（不退役） |
| 删除 session | DELETE（发布语义） | **DELETE 继承** | DELETE 继承（不退役） |
| `POST /slimapi/session/{sid}` | 404 `thin_route_not_found`（现状） | 405 `method_not_applicable`（§16.1 过渡态） | **≡ PATCH 等效路由**（§16.2-a） |
| `POST /slimapi/session/{sid}/archive` | 404（现状） | 405（§16.1 过渡态，空 `Allow`） | **便捷 archive**（可选 body，§16.2-c） |
| `POST /slimapi/session/{sid}/delete` | 404（现状） | 405（§16.1 过渡态，空 `Allow`） | **≡ DELETE 等效路由**（§16.2-b） |

### §16.1 过渡态 405（`method.boundary.v4`；修订二激活前现行为，冻结值不回收）

**method-not-applicable 精确响应（范围收窄：仅当 selector 已成功选择 `?v=4` 且 `method.boundary.v4 ∈ satisfied ∧ session.post-actions.v4 ∉ satisfied`（两位合取，与 §16.3 四位组合表第二行同口径——boundary 亦未 satisfied 时为框架 404 历史态，不发本 405），method-path 为上述三条组合之一时返回）**——V3 对同 method/path 保持已发布行为（404 `thin_route_not_found` 现状，不引入本 code）：

- **HTTP 405**；
- **`Allow` 头**（字面冻结，逗号+空格分隔）：`POST /slimapi/session/{sid}` → `Allow: GET, PATCH, DELETE`；`POST /slimapi/session/{sid}/archive`、`POST /slimapi/session/{sid}/delete` → **空 `Allow:`**（RFC 9110 §10.2.1：空值 = 资源不支持任何方法）；
- **coded error body**（v3 envelope 惯例）：`{"code":"method_not_applicable","method":"<METHOD>","allow":["GET","PATCH","DELETE"]}`（`allow` 数组与 `Allow` 头一致；archive/delete 为 `[]`）；
- **不转发上游**（零上游 IO）+ **`Cache-Control: no-store`**；**selector 扩展不得自然转发**——v4 视图下这三条组合在过渡态永不透传上游；
- **优先级**：§8.3 链在「② selector version 族 400」与「③ selector directory 族 400」之间插列本节 405（判定不依赖 query 参数，故先于 directory 消费；§8.4）；
- **适用范围精确限定**：本 405 仅限三条组合且 selector 已选 v4 且门控未激活。其他已收编 path 的未注册方法**继承既有路由行为**（框架 405/404 现状——不发本 code、不发 `allow` 数组）；完全未收编 path → 既有 404 `thin_route_not_found`（v3 §8.2）任何版本不变；
- V3/V4 的 PATCH/DELETE controlled-write 语义（JSON body 透传、上游 4xx 逐字、5xx/网络 → 受控 503、no-store）两视图**逐字不变**。

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
- **过渡安全**：门控翻转只改变这三条组合的 v4 命中面（405 → 等效路由）；v3 视图与 PATCH/DELETE 主路径在整个翻转前后逐字节不变。

## §17 修订 non-goals（明确不做项）[2026-08-19 修订冻结；**修订二：non-goals 收紧**]

> **当前状态注记**：本节所列能力在现行已发布实现（4.0.0/4.1.0/4.2.0）中**均不存在**，修订后**仍为 non-goals**（不是待实施 feature）。它们不进入 §3.3 readiness ID 集合的原因：本节是**能力边界声明**（sidecar 明确不提供面），非待点亮 feature——边界本身已编码进 `required ≡ U` 全集（缺 ID 即不做）。**修订二（owner 裁决 2026-08-19）收紧**：cascade 编排层与 cross-session search 为**永久 non-goal**（无对应 feature ID、无 deferred 候选资格，再启用须推翻本节正式修订）；原「POST-only update」deferred 候选已被修订二 q2 激活为 §16.2 POST 等效动作族（经 `session.post-actions.v4` 门控），自本节移除。

- **project status / effectiveStatus / subagentList** 聚合字段——骨架不做会话状态推断；
- **独立 Turn 资源**——turn 语义维持 `/slimapi/sessions/status` 的 `turnIncarnation`/`turn` 合并字段现状；
- **exact merged**——merged 模式 best-effort 语义不变（v3 §4a.5 冻结延续）；
- **512B preview / generic fragment**——expand 类目维持 12 项冻结清单（§14），不加预览/通用片段类目；
- **cascade 编排层**（owner 裁决 q1，永久）——sidecar **不自建**级联编排/子删除聚合/重试/部分失败可见性：delete 语义 = `DELETE`（及 §16.2-b 等效 POST）**沿用上游递归删子 + 吞错现状**（非幂等可接受）；archive = 单会话 PATCH 等效（§16.2-c，**不级联**）；
- **cross-session search**（owner 裁决 q3，永久）——非必要能力；§4.6 search 维持 per-list 字面子串语义，不做跨会话检索。

---

## 附：与设计文档的对应

| 契约节 | 设计权威 |
|---|---|
| §2/§8.3 | design-v4-selector.md |
| §4 全量 | design-v4-dbaux.md（连接/降级/SQL/cursor/等价性） |
| §7 | design-v4-sse-replay.md + design-v4-qp-payload.md |
| §3 能力键时序 | refactor-plan §4.1（n1 冻结） |

*（完）B0-1 产出。定稿条件已执行（2026-08-18）：S-B01 ②③④ owner 终裁收敛，状态行更新为「4.0.0 实施基线（B3a 已落地）」→ B3b 批落地后更新为「B3a+B3b 已落地」（B3b-5，本行）；本文件随 4.0.0 发版定稿。*
