# oc-slimapi v4 wire 契约（B0 起草稿 —— 随 4.0.0 定稿）

> **状态**：**B0 规范先行批次起草稿（2026-08-17，rev-1 评审修复后）**。wire 终态目标 = 4.0.0（`ACCEPTED_CLIENT_VERSIONS` (3,3)→(3,4)）；本文在 B0 批冻结全部可观察语义，实现随 B3a/B3b 分批落地（同属 4.0.0 一个 major）。S-B01 ②③④（§7 内标注）为**设计提案待 owner 终裁**；其余章节（含 DB 设计 R1/R2/R3/R6，已凭真库实证冻结——见 design-v4-dbaux §0.2）均为冻结语义。
> **继承基线**：v3 契约（`docs/specs/v3-contract.md`）全量继承 + 本文件逐条差异覆盖——**凡未提及语义逐字沿用 v3**（投影、SSE 帧名帧形、资源上限、错误映射、gzip 族、指纹、catalog、token stream 帧形等）。v4 = v3 的**严格超集面**上的差异层：新增全局 sessions 面（DB 投影源）、SSE id:/重放、directory 于全局列表退役。
> **裁决出处**：`docs/system-architecture-proposal-2026-08-17.md`（v2.2，权威基准，行号引用）；工程细化 `docs/refactor-plans/slimapi-refactor-plan.md`；设计文档 `design-v4-selector.md` / `design-v4-dbaux.md` / `design-v4-sse-replay.md` / `design-v4-qp-payload.md`。
> **消费者**：ocdroid（B5a 探测 / B5b 适配）与 oc-webui 可**仅凭本文件**完成 v4 对接开发。

---

## §0 版本原则与并存退役规则 [冻结]

1. **双版本期**：4.0.0 起 `ACCEPTED_CLIENT_VERSIONS = (3, 4)`（v2.2 行 245）。`?v=3` 语义**逐字节不变**（v3 管线原样）；`?v=4` 启用本契约差异面。无 `v` / `v=3` 以外旧版本 → 400 `unsupported_version`，`supported:[3,4]`（端点存在不 404，沿袭 v3 §2 退役后语义）。
2. **major 与 wire 协议版本绑定**（release.md §1.1 铁律）：(3,3)→(3,4) 与 (3,4)→(4,4) 均为 major 发版。
3. **v3 退役（5.0.0，(4,4)）判据**（v2.2 行 248）：access log `wireVersion` 维度 v3 流量归零 + SSE active 无 v3 连接（观察期口径见 §9.4）→ 删 v3 管线，`supported:[4]`。收窄即 major，写入本节。
4. **消费者回退语义**：503 族 = 显式错误，客户端**不自动回退 v3**（维持当前 wire 版本，按 Retry-After/手动重试处理）。v3 目录级浏览仅经**用户显式触发**的整体版本重协商（`available` 含 3 时覆写 selectedWireVersion=3，全端点一致），且是**功能降级非等价回退**——v4 的跨目录 parent/archived 过滤与全局 cursor 翻页在 v3 无对应语义，UX 按功能降级建模。

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
     "sseReplay": true,           # B3b 起（B3a 期缺席）
     "qpImmediateFull": true      # B3b 起（B3a 期缺席；语义由 design-v4-qp-payload.md 结论冻结）
   }}}
```

- `current` 双版本期恒为最新主版本（=4，S-B04）。
- **能力键为静态键**（v2.2 行 140/254）：存在即广告，**不随 DB 抖动**——DB 熔断/降级不改变 capabilities，瞬态可用性经 503 + health `auxiliary` 字段（§3.2）+ metrics 表达。
- **广告时序（n1 冻结）**：`sseReplay`/`qpImmediateFull` 与实现**同批启用**——B3a 的 `capabilities["4"]` **不含**此二键；B3b 实现落地同期广告。消费者：键缺席 = 该能力不可用，不得预依赖。
- 消费者探测（B5a）：`capabilities["4"]` 不存在 → 继续 v=3；未知键容忍忽略。

### §3.2 `GET /slimapi/health` 双视图

- 按请求 wireVersion 返回对应视图：v3 视图 `schema.version=3`/`server.api_version=3`；v4 视图双双 =4（同源同值，S-B04）。
- v4 视图新增瞬态字段：`auxiliary: {available: bool, mode: "db"|"http"}`（v2.2 行 140；available=false 时 mode="http"）；`allowlist: {enabled: bool}`（机制是否启用，B4-4 落地，未配置=false；不泄露清单内容）。
- `ready` 端点形状不变。

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
- **排序冻结**：`(time_updated DESC, id DESC)` 复合排序（上游 session.ts:571-572 事实同构）。
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

v4 sessions **无 ETag/Vary/304**（v2.2 行 254 §6）；v3 全表面 ETag 原样。ETag validator 版本隔离：v3/v4 validator 互不匹配（v4 其他路由若产 ETag，前缀隔离）。

### §4.5 cursor（无状态 keyset best-effort，决策 1 定案）

- cursor = base64url(JSON `{t: time_updated, i: id, f: {archived, parent, search-hash, allowlist-rev}}`)——复合键 + 过滤上下文指纹（v2.2 行 127）。
- 承诺：确定性排序（§4.1 冻结）；**不承诺**并发更新零重复零遗漏（跨边界重见为预期行为，契约明示）。
- 指纹不匹配当前请求参数 → 400 `invalid_cursor`（提示重开首屏）。
- keyset 下推 SQL（`(time_updated, id) < (t, i)` 复合谓词）；降级路径遇 cursor 一律 503（§4.2）。
- search-hash = search 规范化形式（trim+LIKE 转义后）的稳定 hash；allowlist-rev = 非空 allowlist 集合修订版本——**同一输入两次执行 hash 相同**（确定性断言，§11.6）。

### §4.6 search / allowlist SQL 语义（B0-6e 冻结，design-v4-dbaux §9 同源）

- search：DB 路径 `title LIKE :pattern ESCAPE '\'`，pattern = `%` + 用户串（`%`/`_`/`\` 以 `\` 前缀字面转义）+ `%`——**字面子串匹配**；规范化 hash 进指纹。**降级（DB 不可用）**：search 含 `%`/`_`/`\` 任一字符 → 503（不可等价表达，§4.2）；纯字面子串 → 上游透传等价（上游对无通配字符输入同字面子串）。
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

## §6 ETag / Vary / 304 [冻结]

v3 原样（§4.4 已含 v4 差异：v4 sessions 无 ETag）。

## §7 SSE id: / 重放（v4-only）[①已裁决；②③④设计提案待 owner 终裁]

> 设计权威：`design-v4-sse-replay.md`（协议矩阵用例表 + 状态机全文）。本节为 wire 可见语义。**v3 SSE 帧名帧形零变化**（v2.2 行 153 冻结）——id:/重放仅 v4 生效；v3 客户端无感知。能力键 `sseReplay` 随 B3b 广告（§3.1 时序）。

### §7.0 四项协议裁决记录（S-B01，B0 出门 gate）

| # | 议题 | 状态 |
|---|---|---|
| ① | tokens=1 统一流 | **已裁决（owner，2026-08-17）**：v4 禁止复用——`/events?tokens=1` → **400**，token 流必须走独立 `/slimapi/sessions/{sid}/stream`。理由：单 Last-Event-ID 无法恢复双序列（meta-first 与重放顺序结构性矛盾：重连新 meta 分配新 seq 后发旧 replay 帧 = 线上 ID 倒退）；webui/ocdroid 本就分离两连接，成本最低 |
| ② | meta 重连语义 | **设计提案待 owner 终裁**：meta 帧**不带 `id:`**（连接级协商帧，不参与序列）；epoch **不随重连更换**（仅进程重启换——重连同进程内历史帧与日志窗口仍有效，换 epoch 会浪费窗口内可补帧）；线序严格 = meta（无 ID）→ replay 帧 → 新帧，全程 `(epoch,seq)` 单调不减 |
| ③ | token ID 作用域 | **设计提案待 owner 终裁**：**token 流 = per-sid** 独立序列（端点天然绑定 sid，域键 = sid）；**全局流 `/events` = 该全局输出流自身的单一序列**（全实例策展帧共序——`/events` 是单连接全实例流，无 directory 绑定，per-directory 域会产生跨 directory 重复 ID / 单连接 seq 不单调 / Last-Event-ID 无域信息不可恢复，不可实现）。否决全局跨端点统一序列（token 流独立端点独立域）与每连接序列（重连后 Last-Event-ID 失效）；单一/per-sid 域下 seq 空洞唯一来源 = 日志逐出 → gap 判定干净 |
| ④ | 两端点逐帧状态机 | **设计提案待 owner 终裁**：CONNECTING → ESTABLISHED/REPLAYING/RESYNCED 转移逐帧表（8 场景 × 2 端点），4 条通用不变量：meta 恒首帧；带 `id:` 帧按 (epoch,seq) 严格单调不减；无 `id:` 帧（meta/resync/heartbeat）不参与序列；replay 序列内不插新帧 |

（②③④完整论证与状态机表格见 `design-v4-sse-replay.md` §2/§3；本节为 wire 摘要。）

### §7.1 id: 语法与序列（提案口径）

- `id: <epoch>:<seq>`（冒号分隔，纯十进制）——epoch = 进程代（unixtime_ms，**重启必换**；不随 SSE 重连更换），seq = 单调递增。
- **ID 域独立**：全局流 `/events` = 全实例策展帧**单一序列**（一个 epoch 一个 seq 计数器）；token 流 = per-sid 独立序列（域键 = sid）。同 epoch 下两域 seq 空间不相交；跨端点 `Last-Event-ID` 视为无效（忽略重置按首连处理）。
- 帧分类：业务帧 / digest 分配 id；meta / resync / heartbeat **无 id**（不参与序列）。
- **ID 无倒退不变式**：任一连接上线后带 `id:` 帧严格单调不减（§7.0② 线序保障）。

### §7.2 重放语义（提案口径）

- 有界重放日志（新组件，count/bytes/TTL 三维上限，环形覆盖）——现 GlobalHub pending（250ms debounce）与 tombstone 队列**不是** replay log；与既有 token 域重放队列（cap 1000/TTL 24h）并存不混用。
- `Last-Event-ID` 重连：缺口在日志窗口内 → 补发 replay 帧；ID 过期（早于窗口）→ 发 resync 提示帧（客户端全量对齐）；**epoch 归类（冻结，四类拆分）**：旧合法 epoch（格式合法、epoch < 当前，即进程重启）→ `resync{reason:"epoch_changed"}`；future（epoch > 当前 或 seq > 已发布 max）→ 忽略 + 重置（按首连）；格式非法 / 跨端点域 → 忽略 + 重置。
- gap 处理：区分「日志逐出」（→ resync）vs 合法缺席（单一/per-sid 域下不存在跨域合法空洞）。**snapshot 不是服务端帧**——resync 后客户端自行 HTTP 全量对齐（全局域如 `/slimapi/sessions` 首屏、token 域重拉消息投影），服务端只发 meta → resync → 新帧。逐出-发布并发的边界 gap 误判风险为实现期待验证项（design-v4-sse-replay.md §5 待裁决 5，可降级防御分支，不影响 wire 语义）。
- 背压：溢出帧**入**重放日志（日志记录「已发布帧」而非「已送达帧」）；订阅端溢出断连 → 重连走 Last-Event-ID 重放。
- **resync 帧 reason 值域（v4 冻结，加性扩展）**：`epoch_changed` | `replay_expired` | `replay_gap` | `reconnect_no_replay`（既有）；token 流 tombstone（消息已撤销）在 replay 时**照常消耗其 seq 并以 `message.removed` 轻量撤销帧回放**（既有帧形 `tokenstream/frames.py:137-151` = `event: message.removed` + `{sessionID, messageID}`；保留 `id:`，维持 ID 序列无空洞）。
- meta 恒首帧（meta-first 不变）；v4 meta additive 扩展：capabilities 摘要 + epoch/seq 基线字段（v3 形状不动，B3b-4）。

### §7.3 tokens=1（已裁决终态）

- `/events?tokens=1`（v4）→ 400 `{"code":"tokens_stream_retired_in_v4","hint":"token 流请使用 /slimapi/sessions/{sid}/stream"}`；v3 请求该参数语义不变。
- token 流端点 `/slimapi/sessions/{sid}/stream`：v4 起分配独立 id:（§7.1）；directory 消费保留（§5.2）。

### §7.4 q/p 帧载荷（`qpImmediateFull` 语义）

- 逐字段核对结论（`design-v4-qp-payload.md`，B0-4 产出）：**已完整**——sidecar `properties` = 上游 event.data 原样透传（`event-v2-bridge.ts:39-44` 构造 → `global_hub.py:522,529` 零裁剪 → 上游 `core/question.ts:93-110`、`permission.ts:164-174` 发布完整 Request）；`question.asked`（10 字段）与 `permission.asked`（10 字段）逐字段比对**无缺失、无改名、无裁剪**。EventV2 envelope 字段（evt_ id/metadata/durable/location）v3 契约本就不进 properties，不属于缺失。
- **`qpImmediateFull` 语义冻结 = 现状已成立**：B1b 零 wire 变更，webui/ocdroid 直投为纯客户端改动；不触发 B3b-3 补全路径（该任务留空）。两套字段表（上游完整直投字段集 / 最小可渲染字段集）以 design-v4-qp-payload.md §2/§3 为权威，随实现批同步引用。
- digest 帧跨版本注记：B1a（P1 3.3.0）起 `session.digest` 增可忽略字段 `changed:[sid…]`（**最小语义已裁决**：changed = [本帧 sid]——digest 为 per-sid 逐帧产出，帧出现即 changed；形状保留列表为未来聚合留形），v4 帧形沿用。

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

`wireVersion` 维度 v3 流量归零 + SSE active 无 v3 连接（连续观察窗）→ 5.0.0 (4,4)（§0.3）。

## §10 路由全集逐条（v4 差异列）[冻结]

45 条 /slimapi 路由（read 23 + write 12 + SSE 2 + 发现/运维 8）。**v4 差异仅下列 4 条**，其余 41 条 v4 = v3 语义原样（经 selector 分派）：

| 路由 | v4 差异 |
|---|---|
| `GET /slimapi/sessions` | §4 全量（DB 投影源/参数矩阵/降级矩阵/cursor/无 ETag）；directory → 400 |
| `GET /slimapi/events` | §7：v4 分配 id:/重放；`tokens=1` → 400；directory 消费不变（events 非消费集路由，目录帧过滤随 allowlist） |
| `GET /slimapi/sessions/{sid}/stream` | §7：v4 分配独立 id:（token 流）；directory 消费保留 |
| `GET /slimapi/versions` | §3.1 双版本载荷 |

`GET /slimapi/health` 双视图（§3.2）为响应差异，路由行为不变。messages（4 条 + 2 expand）、sessions/status、todo/children/diff、directories、agent、command、file/vcs/find/config/session-single（读组 10）、active、global/health、metrics、ready、actions（2）、write 12 条：**零 v4 差异**。

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
| 11.10 | SSE 重放 | 重放/缺口/过期/重启 epoch/背压/重连/tokens=1 400/ID 无倒退断言（协议矩阵用例表见 design-v4-sse-replay.md） | B3b |
| 11.11 | DB schema 变更兼容 / 运行中迁移 | 上游升版列变更 → 门失败降级；运行中 inode swap | B3a-B1/B6 |
| 11.12 | 冷启动 | P99 warmup 豁免；首查延迟 | B3a-B1 |

---

## 附：与设计文档的对应

| 契约节 | 设计权威 |
|---|---|
| §2/§8.3 | design-v4-selector.md |
| §4 全量 | design-v4-dbaux.md（连接/降级/SQL/cursor/等价性） |
| §7 | design-v4-sse-replay.md + design-v4-qp-payload.md |
| §3 能力键时序 | refactor-plan §4.1（n1 冻结） |

*（完）B0-1 产出。定稿条件：S-B01 ②③④ owner 终裁后 §7 转冻结、状态行更新为「4.0.0 实施基线」。*
