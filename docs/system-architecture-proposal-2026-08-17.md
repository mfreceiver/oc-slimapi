# oc-slimapi 体系架构重塑方案（2026-08-17，v2.2 修订版）

> **v2.2 修订**：并入三方 R2 复审（oracle APPROVE WITH CONCERNS / rev-sgpt REJECTED / deepest SOUND WITH GAPS）全部发现。核心修正：① DB 辅助连接策略反转——`mode=ro` 主路径（WAL-aware），`immutable=1` **完全弃用**（live WAL 下静默陈旧读，三方独立实证）；② 数据源模型裁决——DB = v4 sessions **投影源**（非"过滤辅助"），HTTP = schema 权威 + 降级路径；③ 降级矩阵冻结——HTTP fallback 仅服务可等价表达的过滤组合，其余显式 503，禁止 `degraded:true` 掩盖语义削弱；④ D-ix 移出 sidecar（永不写上游 DB，索引 = 运维动作）；⑤ q/p 60s 定时兜底废除（285× 流量放大实证）→ resync 驱动 + 低频 jitter sweep；⑥ B5 拆 B5a/B5b、B3 拆 B3a/B3b；⑦ 组件账目 15→15 修正。
> **v2 修订**（历史）：并入三方 R1 评审全部发现——status 矛盾消除、`slimapi.meta` 事实纠正（已实现）、SSE id: 移 v4-only、q/p「删除」改「降频」、双版本 selector 前置 B0、发版路线对齐 release.sh、/global/event 事件源实证纠正。
> 证据链：`slimapi-complexity-map-2026-08-17.md` + `ocdroid-needs-audit-2026-08-17.md` + `upstream-global-model-2026-08-17.md`（其 /global/event 论断已被实证推翻，见 §1.1）+ `native-web-benchmark-2026-08-17.md` + `webui-needs-audit-2026-08-17.md` + 三方 R2 评审（WAL 陈旧读草稿库复现 / EQP 索引矩阵 / 降级语义审查，2026-08-17）
> 目标（owner 原话）：（1）不逊于原生 web 的接口和能力（2）更现代便捷的方式重塑 API（3）针对移动终端、互联网传输优化；三件套形成以 slimapi 为基本能力的体系；减轻 slimapi 复杂性；解放下游改造代价。

---

## 1. 现状诊断

### 1.1 根本矛盾：存储全局 vs 服务 per-directory（含事实修订）

上游 v1.18.18 真相：所有 project 的 session/message/事件在**同一个 SQLite**；服务层 InstanceContext 强绑定 directory。唯一全局后门：`GET /experimental/session`（全局列表 + archived 过滤 + cursor 分页）。

**事实修订（实证 2026-08-17）**：`/global/event`（GlobalBus）**携带全部已启动（booted）实例的完整 EventV2 事件流**——`event-v2-bridge.ts:35-46` 将每实例 EventV2 全量 republish 到 GlobalBus（含 message.*/question.*/permission.*，带 directory 标签）；实例级 `/event` 只是同流的 directory 过滤视图（`handlers/event.ts:40-44`）。推论：q/p 事件驱动发现**零新增上游连接**；限制：**未 boot 的冷目录不发事件**（InstanceStore LRU 惰性加载）——冷目录 pending 仍需权威兜底（见 §3.2a 兜底重设计）。

**上游 DB 运行事实（R2 实证）**：上游以 **WAL 模式**运行（`database.ts:27` `PRAGMA journal_mode=WAL` + `synchronous=NORMAL`；仅启动时 `wal_checkpoint(PASSIVE)`）——live `-wal` 常态存在未 checkpoint 提交（真库探测：主库 5.4GB，-wal 19.9MB，-shm 32KB，opencode serve 常驻）。此事实直接决定 §3.1 连接策略。

### 1.2 消费者补偿（不变）

ocdroid：WorkdirPrefs 单目录耦合、客户端 archived 过滤、C1/C2/C3 违规（图片直连/健康探测绕过）、404-sticky、三形状 SSE 解析。webui：收藏目录 N×2 扇出、digest 整表重拉、q/p 帧当变更信号。

**status 事实澄清（rev-1 实证）**：上游 `/session/status` 返回**全局内存 map，directory 不影响结果**（sessions.py:350-368 现转发亦然）。30.6K 次/4 日的 status 请求放大是**消费者调用策略问题**，非上游缺全局能力——sidecar 无须任何工作，消费者改单次全局调用即可。

### 1.3 slimapi 复杂度基线

15 个有状态组件（10 essential + 5 compensation）；两大补偿块：双 SingleFlight 实现（~770 行）、q/p 跨目录扇出（~400 行 + 2 semaphores）。测试基线：78 测试文件 / **2,199 个 collected tests**（deepest 实证复核）。

### 1.4 与原生 web 差距（不变）

真实缺口：运行中 agent/model 切换、context 用量、revert 三段式。明确不做：PTY/OAuth/Project copy。

---

## 2. 体系架构：全局门面（Global Facade）

**核心决策不变：不改上游，slimapi 升级全局门面。** 全局读（零扇出）+ 定向写（directory 路由，上游模型决定）+ 增量优先（cursor/seq/id:）。

**备选排除记录**：纯客户端聚合——已证伪（两消费者各自补偿失败正是本方案动机）；上游改造——排除理由：上游发版节奏不受本体系控制，且现全局后门已够用；仅当未来上游原生提供全局 pending/stable cursor 时可回收 sidecar 对应状态（记入 §9 观察）。

---

## 3. API 重塑方案

### 3.1 全局会话目录（v4 重塑；owner 已定：keyset + DB 辅助；v2.2 数据源模型裁决）

**`GET /slimapi/sessions`（v=4）**：sidecar 自有查询面——**数据源模型（R2 裁决，二选一定案）**：

> **DB = v4 sessions 投影源（常态路径）**；上游 HTTP `/experimental/session` = **schema 权威 + 降级路径**。
> 理由：上游 global list **无 arbitrary-ID 批量取回接口**（session.ts:557-575 仅 directory/roots/start/cursor/search/limit），"HTTP 主源 + DB 键窗口再取内容"的双源 hydration 不可执行（rev-1 B2）；DB 直读组装是唯一无放大路径，故 DB 升格为投影源。HTTP 保持 schema 权威地位：sidecar 逐版本契约测试锚定 DB 投影与 HTTP 投影的等价性（见护栏 2′）。

```
GET /slimapi/sessions?v=4
    &archived=omit|only|all     # 三态过滤（默认 omit）
    &parent=all|none|only|<sid> # 子会话过滤；省略 = all（全量，v4 显式冻结）
    &search=<title-substring>   # 标题子串（DB LIKE；降级透传上游）
    &cursor=<opaque>            # keyset best-effort（决策 1）
    &limit=1..500
    → 200 {items: SessionSkeletonV4[], nextCursor: string|null,
           complete: bool, degraded?: true}
```

- `degraded?: true` **进入正式成功响应 schema**（rev-1 M4）：仅当走 HTTP 降级路径时出现；语义冻结 = 数据源降级 + 排序/complete 强度弱化，**过滤结果语义与常态等价**（不等价一律 503，见降级矩阵）
- **parent 省略默认 = all**（dee 消费者风险）：`roots=true` 消费者迁移为显式 `parent=none`；默认 all 保证不带参数的全局列表语义直觉
- **v4 sessions 豁免 ocdroid DirectoryHeaderInterceptor 注入**（B5a 适配清单）：拦截器向 `/slimapi/**` 自动注入 directory query → v4 sessions 会 400 `directory_retired_in_v4`，豁免规则先行

#### 投影查询（一条 SQL 组装，同一只读 snapshot）

```sql
SELECT s.id, s.parent_id, s.time_archived, s.time_updated, s.directory, s.title,
       s.agent, s.model, s.version, s.summary_diffs, s.tokens_in, s.tokens_out,
       s.time_created, s.revert, s.permission, s.metadata,
       p.directory AS project_directory, p.worktree AS project_worktree
FROM session s LEFT JOIN project p ON s.project_id = p.id
WHERE (archived 三态谓词) AND (parent 四态谓词)
  AND (?search IS NULL OR s.title LIKE ? ESCAPE '\')
  AND (allowlist 非空时：s.directory IN allowlist 子树谓词)
  AND (cursor keyset: (s.time_updated, s.id) < (:t, :i))
ORDER BY s.time_updated DESC, s.id DESC
LIMIT :limit + 1                  -- complete 由同查询窗口判定（同 snapshot，rev-1 M9）
```

- **组装容忍语义**（ora R2-m2 / dee N-6）：project join 缺行 → `project=null`（对齐上游 GlobalInfo.project 容忍）；JSON 列（summary_diffs/metadata 等）解析失败 → 该会话 skip + warning log，不 500；键集内行缺失即跳过
- **allowlist 进 SQL 谓词 + cursor 指纹**（dee N-9）：非空 allowlist 是结果全集的一部分，中途变更 → cursor 指纹不匹配 → 400 `invalid_cursor`（重开首屏，行为可预期）
- **search 语义冻结**（rev-1 M8 / dee N-8）：DB 路径 = `LIKE ? ESCAPE '\'` + 规范化 hash 进 cursor 指纹；降级路径 = 透传上游（上游原生支持）
- **legacy 空 directory**（ora 护栏增补）：过滤 SQL 复刻上游 `directoryColumn` 空串规范化语义（path.ts:41-52），否则 DB 路径与 HTTP 路径对同一行集分叉
- **keyset 下推**：`WHERE (time_updated, id) < (t, i) AND <filters>` 严格复合谓词；排序 tie-break `(time_updated DESC, id DESC)` 契约冻结

#### 连接与生命周期（R2 Blocker 修正：mode=ro 主路径，immutable 弃用）

> **实证依据**（三方独立复现）：`immutable=1` 不读 live `-wal` 内容——草稿库测试中 WAL 内已 commit 的行不可见（count=1 vs 2），表建于 WAL 时甚至 `no such table`；真库探测 max(time_updated) 滞后 7.6s。陈旧读**不产生任何错误**（连接/查询均成功），探测链无法检测——这是静默数据过期，非可用性降级。

1. **主路径 `mode=ro`**：普通只读连接，经 `-shm` 正常读 WAL 内容，与 live writer 共存（sidecar 与 opencode 同机同用户，shm 访问成立）；`PRAGMA query_only=ON` 作防御层
2. **短生命周期只读事务**：每查询一个 `BEGIN … COMMIT` snapshot——请求内一致（含 complete 判定），不长期持读锁（不阻滞 WAL 回收）
3. **`immutable=1` 完全弃用**：不作为主路径、不作为降级档（live WAL 下静默过期 + 冷文件场景也可被 ro 覆盖——ro 仅在无 shm 且目录不可写时失败，本部署不成立；若未来部署形态变化 → 直接禁用辅助源走 HTTP）
4. **连接探测**：启动时 ro 打开 + schema 门（见护栏 3′）探测；失败 → 禁用辅助源（全降级 HTTP），**不试 immutable**
5. **DB 路径解析**（dee N-10 / ora 护栏）：`OC_SLIMAPI_OPENCODE_DB` 显式配置（生产推荐）+ 默认复刻上游解析（`OPENCODE_DB` env → InstallationChannel 分库：latest/beta/prod → `opencode.db`，否则 `opencode-<channel>.db`；`:memory:` → 禁用辅助）+ **启动 log 实际解析路径**
6. **inode/mtime 定期校验**：备份恢复 / channel 切换换 DB 文件 → sidecar 持旧 fd 读已删 inode → 校验发现即重开重探（挂熔断器周期）
7. **错误触发的 schema 重探**（非仅启动一次）：查询错误分类（`SQLITE_SCHEMA` / `no such table/column` / I/O / WAL-SHM 不可达）→ 熔断禁用 → 周期重探恢复

#### 索引策略（R2 修正：D-ix 移出 sidecar；无索引直跑 + 熔断）

**EQP 实证（dee-2，1,000 行草稿库全矩阵）**：v2.1 拟议复合索引 `(time_archived, parent_id, time_updated DESC, id DESC)` 仅在 `archived=omit` + `parent` 等值约束时 covering 且免排序；`archived=only|all`、parent-any、keyset 谓词状态全部走旧索引/全扫 + **临时排序**——该索引是 filter-shaped 非 sort-shaped，「keyset 排序真正成立」的 v2.1 表述撤回。keyset 排序正确性来自 SQL `ORDER BY`（恒成立），索引只影响性能。

- **首期无索引直跑**：真库 384 行全表扫温测 ~0.015ms，`P99 < 20ms` 熔断护栏兜底（超限 → 熔断降级 HTTP + 告警）
- **sidecar 永不写上游 DB（含 DDL）**：D-ix 从 sidecar 启动行为中**移除**（v2.1 决策 7 的「sidecar 幂等建索引」撤回）——与「不写 SQLite」护栏的自相矛盾（ora R2-M2 / dee N-5 / rev-1 M1 三方一致）以此消除
- **索引 = 运维手册动作**：仅当生产 EQP + P99 数据证明必要时，由**运维显式执行**（`docs/operations.md` 记录程序）；sort-shaped 候选 = 独立 `(time_updated DESC, id DESC)` 索引（服务 keyset 排序），非 v2.1 复合索引；`CREATE INDEX IF NOT EXISTS` 不验证列定义——运维程序必须含 `PRAGMA index_xinfo` 定义校验（防同名异构误判）
- **AGENTS.md 措辞（配套修订）**：「禁止写入/修改上游 opencode SQLite 业务数据；sidecar 代码路径零 DDL/DML/PRAGMA 写；索引建立属显式运维动作（含定义校验），不在 sidecar 内」——wire contract 只冻结可观察语义（参数/错误/降级/degraded），**不冻结 SQLite 这一实现手段**（rev-1：实现手段进 AGENTS/架构 ADR/operations/schema 兼容测试，四处同步）

#### 降级矩阵（R2 Blocker 修正：禁止静默削弱过滤语义）

DB 辅助禁用/熔断时（全降级 HTTP `/experimental/session`）：

| 请求状态 | 降级行为 |
|---|---|
| `archived=omit\|all` + `parent ∈ {all, none}` + **无 cursor** + search 任意 | **200 + `degraded:true`**——上游原生等价（parent=none → `roots=true`；parent=all → 不过滤；search 原生）；排序退化上游单键 `time_updated`（tie-break 弱）、complete 退 best-effort——均在 degraded 语义内披露 |
| `archived=only` | **503 `auxiliary_unavailable`**（上游二态无法表达 only） |
| `parent=only` / `parent=<sid>` | **503 `auxiliary_unavailable`**（上游无此过滤） |
| 带 `cursor`（任何过滤组合） | **503 `auxiliary_unavailable`**（上游单键 cursor 无法兑现 `(t,i)` keyset 指纹语义） |

- 503 附 `Retry-After`；错误体不泄露 DB 路径/schema 细节（rev-1 minor 采纳）
- 原则（rev-1 B3）：`degraded:true` 只表数据源降级与强度弱化；**过滤语义永不降级**——可等价表达 → 200+degraded，不可表达 → 503

#### cursor（决策 1 定案：无状态 keyset best-effort）

- cursor = base64url(JSON `{t: time_updated, i: id, f: {archived, parent, search-hash, allowlist-rev}}`)——复合键 + 过滤上下文指纹（allowlist 修订入指纹，dee N-9）
- 承诺：确定性排序（`(time_updated DESC, id DESC)` 冻结）；**不承诺**并发更新零重复零遗漏（跨边界重见为预期行为，契约明示）
- 指纹不匹配当前请求参数 → 400 `invalid_cursor`（提示重开首屏）
- keyset 下推 SQL（常态路径）；降级路径遇 cursor 一律 503（见矩阵）

#### 其余参数矩阵

- **零 directory 参数**；每项含 `directory`/`project`（**新 v4 SessionSkeletonV4 投影**——现 `SESSION_KEYS` 已含 directory（skeleton.py:704-719，dee N-12 纠正），新投影为 `project` 对象与 v4-only 字段而设，非 directory）
- `roots`/`start` **退役**：`roots` 语义由 `parent=none` 精确承接；时间过滤由 cursor 承载
- v4 收到 `directory` → 400 `directory_retired_in_v4`；v3 收到 `archived`/`parent`/`cursor` → 422（未知参数）
- `complete` = 同查询 `LIMIT+1` 窗口判定（同一只读 snapshot，rev-1 M9）；降级路径 = 上游 best-effort（degraded 披露）；limit 500 为 v4 域（v3 保持 1000）
- **status 全局化删除**（决策 2）：上游 `/session/status` 本就全局——消费者改单次全局调用（B5a），digest 变更后按 §3.2 定位精拉
- 首期**无 sidecar 行缓存**：一条 SQL 一次组装，无缓存失效问题
- **capabilities 边界**（rev-1 M5）：`auxiliaryFilters` = **静态能力键**（v4 存在即广告，不随 DB 抖动）；瞬态可用性 = 503 + `/slimapi/health` 扩展字段（`auxiliary: {available: bool, mode: "db"|"http"}`）+ metrics（降级计数/查询延迟）——`/versions` 能力面恒定

#### DB 投影源的边界与护栏（v2.2 修订，配套修订 AGENTS.md）

1. **只读不变性**：`mode=ro` + `query_only=ON`；sidecar 代码路径**零写入**（含 DDL——索引属运维动作，见上）
2. **辅助范围冻结**：仅限 v4 sessions 投影（`session` 表 + `project` 表 LEFT JOIN 的冻结列集）——**不得**扩展到 message/part/事件溯源（那些走既有 HTTP 收编路径；防 sidecar 变第二查询引擎）
3. **schema 兼容门（全投影列版）**：启动探测 `session` 表 + 全部投影列（id/parent_id/project_id/time_archived/time_updated/directory/title/agent/model/version/summary_*/tokens_*/time_*/revert/permission/metadata）+ `project` 表 join 列齐备，否则禁用辅助降级 HTTP；**运行中错误分类触发重探**（见连接生命周期 §5/§7）；上游版本升级 schema 变更 → 契约测试矩阵覆盖
4. **性能护栏**：辅助查询 P99 < 20ms；超限熔断降级 + 周期重探恢复
5. **等价性锚定**：sidecar 契约测试逐版本锚定 DB 投影 ≡ HTTP 投影（同一行集同字段语义）——HTTP 的 schema 权威地位由此兑现，上游演进时等价性测试失败 = 禁用辅助的信号

### 3.2 增量事件：digest 定位（v3 加性）+ SSE id:（v4-only）

- **digest 帧增强（v3 安全加性，B1a）**：`session.digest` 增可忽略字段 `changed: [sid…]`——**仅此一项新增**（v2.1 所列 `directory` 字段已存在：hub_types.py:171-195 / v2-contract.md:276，dee N-11 纠正，不得重复新增）；v3 契约 §7 正式修订（帧形加字段，旧客户端忽略）；消费端从整表重拉 → 定向精拉
- **SSE `id:` / `Last-Event-ID` 重放（v4-only，rev-1 B2 裁决）**：v3 契约 §7 冻结「帧名帧形零变化 + Last-Event-ID 无重放 API」。v4 需定义完整重放协议：进程 epoch + 单调 seq、ID 作用域（全局流 vs token 流独立）、哪些帧分配 ID（业务帧/digest/meta 有；heartbeat 无）、有界重放日志（count/bytes/TTL）、expired/future/gap 处理（gap → resync+snapshot）、与 meta-first/背压/上游重连的顺序。现 GlobalHub pending（250ms debounce）与 tombstone 队列**不是** replay log——v4 新建有界环形日志组件
- **`slimapi.meta`**：**已实现**（token_stream.py:182-200 + events.py:75-91 双端点，dee 复核）——v4 仅 additive 扩展（capabilities 摘要 + epoch/seq 基线字段），v3 形状不动
- **事件源（实证）**：q/p 事件驱动发现复用现有单条 `/global/event` 连接（GlobalBus 携带全部 booted 实例 EventV2 流）——零新增上游连接；冷目录不发事件 → 兜底见 §3.2a

### 3.2a q/p pending 降频（v2.2：60s 定时兜底废除 → resync 驱动）

上游无全局 pending 端点——事件只能覆盖 sidecar 在线期间观察到的变化；冷启动、断线缺帧、未 boot 冷目录不可恢复。v2.1 的「60s 低频权威兜底」**废除**（dee N-4 实证否决：60s 定时 = 1,440×N 次/日，N=30 目录 → 43,200 次/日 vs 实测基线 151 次/日 = **285× 放大**——兜底反而主导流量台账）。替代设计：

1. **发现 = 事件驱动**（/global/event q/p asked 帧，常态零轮询）
2. **权威对账 = resync 驱动**：SSE 重连 / 客户端 resync 请求 / digest 变更涉及 q/p 时触发对账（按需，非定时）
3. **低频兜底 sweep**：默认 **30min ± jitter**（每目录错峰），仅覆盖「冷目录从未 boot 且长期无事件」的长尾；**每 sweep 预算护栏**（并发上限沿用 semaphore）+ access-log 观测证明 sweep 流量 << 变更前基线（B1b exit criteria）
4. **载荷直投评估前置**（B1b 先行）：逐字段核对 GlobalHub 转发的 properties 是否已是完整对象（上游 question.asked/permission.asked payload）；已完整 → 零 wire 变更纯客户端改动；缺字段 → 移 B3 v4-only
5. **聚合路由不变**：`/slimapi/questions|permissions` 全局聚合语义保留（权威校准 + resync 后全量对账）
6. **净效果**：questions 桶 8.3MB/日上游 fan-out → 事件驱动近零 + 30min sweep 底噪（<50 次/日量级）；fan-out/semaphore 代码保留（§5 账目）

### 3.3 消息面（v2.2 增补：两模式裁剪原则定案）

- **两模式裁剪原则（owner 决策 2026-08-17，裁决 [3.2.0] 契约冲突）**：**正文（TextPart.text）是 chat 核心内容，永不截取**（与已发布 [3.2.0] 契约决策「TextPart.text 永远全量内联、不折叠、无阈值」一致）。裁剪对象 = **需展开才显示的折叠内容**（tool state、reasoning、diff/patch、attachments 等）；裁剪仅两种模式：**模式 1 = 默认完全不加载**（omitted + expandRefs，展开时经 expand 端点拉取）；**模式 2 = 未展开状态提供缩略信息**（如编辑行数——即 diffStats / 预览摘要类）。B2 的「merged 400 码点截断」**废止**；merged 恢复范围对齐两模式（正文全量内联、折叠内容保持模式 1/2）——B2 按此重设计（细化进 S 方案修复轮）
- **`GET /slimapi/session/{sid}/context`** 收编（B4 加性）：token 用量感知

### 3.4 能力缺口补齐（B4 加性，v=3 即可用）

| 新路由 | 上游源 | 价值 |
|---|---|---|
| `POST /slimapi/session/{sid}/agent` + `/model` | v2 `/api/session/:id/agent|model` | 运行中切换 |
| `GET /slimapi/session/{sid}/context` | v2 context | 用量感知 |
| `POST .../revert/stage|clear|commit` | v2 三段式 | 预览-确认回滚 |

### 3.5 目录发现收敛 + directory 白名单（fail-closed）

`GET /slimapi/directories` 保持。**`OC_SLIMAPI_DIRECTORY_ALLOWLIST`**：
- **`/slimapi/file/**` fail-closed：allowlist 为空 → 403 `directory_not_allowed` + 启动 warning 日志**
- 其余端点：空 = 不限制（现状兼容）；非空 = 白名单目录子树过滤（resolve 后前缀匹配，防 `..`/symlink/大小写绕过）
- **全局面过滤语义**：非空时全局 sessions/directories 列表（**含 §3.1 DB 投影 SQL 谓词**）、digest/q/p 帧、事件流均过滤非白名单目录；allowlist 修订进 cursor 指纹（§3.1）
- 消费者需可区分「空因过滤」vs「空因无会话」（B5a 适配：health 广播 allowlist 非空状态，不泄露清单内容）
- 403 不泄露目录存在性（统一错误体）；与 400 族错误码命名区分

---

## 4. 移动/互联网传输优化

| 优化 | 手段 | 现状基线 |
|---|---|---|
| 字节 | skeleton/expand + 折叠内容两模式裁剪（§3.3；正文永不截断） | 综合省流比 2.43%；messages 桶占上游 66% |
| 请求数 | 全局列表（§3.1）+ digest 定位（§3.2）+ q/p 事件驱动 + 30min sweep（§3.2a）+ status 单次全局调用 | sessions 40.5K + status 30.6K 次/4日；q/p 收益归因 = 事件驱动 + 低频 sweep（非 60s 定时） |
| 恢复成本 | SSE id: 重放（v4） | 断线全量恢复 → O(缺口) |
| 首屏 | 折叠内容两模式裁剪（正文全量内联，渲染层摘要展示）+ cursor 分页 | 首页 2×110KB → 渲染层摘要后等效 ~10KB 量级（wire 字节由折叠裁剪与 expand 按需加载承担） |
| 压缩 | gzip 保持；**zstd 不做**（owner 决策 5） | gzip 现状 |
| 弱网 | SSE 心跳看门狗（ocdroid 已有）+ Retry-After（503 族统一） | 已有 |

---

## 5. slimapi 复杂度削减（v2.2 账目修正）

| 削减项 | v1 说法 | v2.2 修正 |
|---|---|---|
| q/p fan-out 删除 | ~200 行 + 1 semaphore | **撤回**——事件驱动 + resync 对账 + 30min sweep（§3.2a）；删除 await 上游原生全局 pending 端点（§9） |
| LeasedSingleFlight+SingleFlight 合并 | ~300 行净减 | 保留主张（纯内部重构，wire 无关，B6） |
| 404-sticky 退役 | sidecar+ocdroid 双减 | 保留主张（v4 capabilities 探测替代，B6，生产流量证明前提） |
| 三形状 SSE 解析退役 | ocdroid ~40 行 | 保留主张（ocdroid 侧 v4 后清理） |
| 新增：SSE 重放日志 | 未列 | **+1 有状态组件**（v4 有界环形日志） |
| 新增：v4 SessionSkeletonV4 投影 | 未列 | 纯投影函数 + DB 只读查询面（无新状态组件；连接短事务无池化状态） |

**净账目**（v2.2 R2 修正，phase-aware）：终态 = 15 **+1**（B1b sweep 调度器，P1）**+1**（SSE replay 日志，B3b）**+1**（DB 辅助生命周期：熔断器+重探循环+inode 观察者，B3a）**−1**（singleflight 合并，B6）= **17**——初稿「15→15」漏记 sweep 与 DB-aux 两项（oracle/rev-sgpt S-lane R1 双独立指出）；若 owner 裁决「DB-aux 熔断/重探/sweep 调度为所属组件内部属性、不独立计账」，口径与数字随之调整（记入 S 方案 §8.1 待裁决）。相位：现在 15 → P1 16 → P3 18 → B6 后 17。测试基线：2,199 → 预计净增（v4 测试面新增 > sticky/fan-out 高频面删除；**不承诺具体数**，实测复核）。定性主张保留：selector 双版本化后协议分派复杂度下降、消费者侧补偿代码确定退役。

---

## 6. 消费者改造建议

### ocdroid（B5a 先行 / B5b 跟进）
1. **B5a（P2，先于 sidecar 4.0.0）**：识别 `capabilities["4"]`（不存在 → 继续 v=3）；未知能力容忍；**DirectoryHeaderInterceptor 豁免 `/slimapi/sessions` directory 注入**（v4 400 防护）；status 改单次全局调用（即刻可做，不依赖 v4）
2. **B5b（B3 后）**：会话列表全局拉取（`parent=none` 替代 `roots=true`；默认 all 语义知悉）+ WorkdirGroups 本地分组；翻页 cursor-aware（低频 workdir 首页可能不在第一屏——组内空 ≠ 无会话，见 §3.1 parent 默认）
3. C1-C3 修复：图片经 slimapi（复用 `/file/content` 反代）、健康探测改 /slimapi/health+/ready
4. 404-sticky + 三形状解析随 v4 退役（生产流量证明后）
5. context 用量接入；agent/model 切换接入

### oc-webui（B5a/B5b 同构）
1. **B5a**：q/p 帧载荷直投（若 §3.2a 核对为已完整，纯客户端改动即刻可做）；merged 截断零改动受益
2. **B5b**：收藏扇出 → 全局列表一次拉取 + 客户端分组（**翻页完整性**：全局 limit ≤500 不保证含全部收藏根会话——分组逻辑 cursor-aware，dee 消费者风险）；M6 与本方案合并规划
3. /file 通路三前置：allowlist（fail-closed）→ serve mount → renderer 解禁

---

## 7. 版本策略与发布路线

**版本铁律不变**：major 与 wire 协议版本绑定（release.md §1.1）。

```
P0  规范先行（B0 批次，见 §8）——v4-contract delta 全章 + 双版本 selector 设计 + DB 投影源设计定稿
P1  加性 v3 minor（3.3.0；3.2.0 已被 [3.2.0] text 全量内联发版占用，顺延——S 方案核准修订）：digest 定位字段（B1a）+ B1b 核对阶段 1（shadow）+ B2 兼容验证（merged 截断已按 owner 裁决废止）+ context/agent/model/revert-stage 路由（B4）
    → 常规 release.sh minor；ocdroid/webui 渐进采纳（零 breaking）
P2  消费者兼容版发布（= B5a）：识别 capabilities["4"] 的客户端（探测→v3 回退+拦截器豁免）
P3  sidecar 4.0.0 = wire (3,4) 双版本（release.sh major 一次到位）：
    B3a selector 双版本结构性改造 + v4 sessions DB 投影源
    B3b SSE id:/replay + q/p 帧补全（若需）——B3a/B3b 各自独立 rev gate，同一 major 内分批落地
P4  按指标退役：access log v3 流量归零 + SSE active 无 v3 连接 → sidecar 5.0.0 = (4,4) 删 v3
    （(3,4)→(4,4) accepted-range 收窄 = major，写入契约 §0；对齐 v2→v3 退役先例）
```

### v4-contract 修订清单（rev-1 11 章 + R2 增补）

§0/1 版本原则与 v3/v4 并存退役规则｜§2 selector 双版本状态表（supported:[3,4]、request-scope wireVersion、跨版本错误优先级）｜§3 versions/health 双视图 + capabilities["4"] 能力键（globalSessions/sseReplay/qpImmediateFull/auxiliaryFilters——**静态能力，不随瞬态抖动**）+ health 瞬态字段（auxiliary available/mode）｜§4 sessions 参数矩阵（archived 三态/parent 四态含省略=all）/排序/complete（LIMIT+1 同 snapshot）/cursor 绑定（f 含 search-hash+allowlist-rev）/错误族（invalid_cursor + 503 auxiliary_unavailable **完整降级矩阵表** + Retry-After + degraded 响应 schema）｜§5 directory 消费矩阵（v3 继续消费；v4 sessions 拒绝；allowlist 作用域全覆盖含 DB 谓词）｜§6 ETag（v3 原样；v4 sessions 无 ETag；validator 版本隔离）｜§7 SSE id: 语法/epoch/seq/作用域/重放顺序/gap 处理 + meta v4 扩展 + q/p 精确 JSON schema｜§8 错误族（403 vs 400 族优先级；503 Retry-After 规范）｜§9 观测（selectorResult=v4/wireVersion 维度、replay hit/miss/gap、**DB 辅助查询延迟/降级/熔断计数**、v3 退役判据）｜§10 路由全集逐条（版本/directory/allowlist/ETag/错误/上游源）｜§11 测试矩阵（跨版本/重启/过期/背压/冷启动/**DB schema 变更兼容/运行中迁移/ro-vs-immutable WAL 测试/EQP 全矩阵/降级矩阵逐格/等价性锚定**）

**B0 同步修订**：`docs/release.md:42-47` 陈旧 v2/X-Slimapi-Version 描述（rev-1 M7）+ AGENTS.md「不写 SQLite」措辞（§3.1 护栏 5）+ `docs/operations.md`（DB 路径解析/索引运维程序/熔断排障）——wire contract 只冻可观察语义，实现边界进治理文档。

---

## 8. 实施批次（v2.2：B0 硬出口门槛 + B3a/B3b 拆分 + B5a/B5b 拆分）

| 批次 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **B0** | **规范先行 + 设计实证**：v4-contract delta 全章（§7 清单）+ 双版本 selector 结构设计 + SSE 重放协议设计 + q/p 载荷核对 + **DB 投影源设计评审** + release.md/AGENTS.md/operations.md 同步修订。**硬出口门槛**：(a) ro-vs-immutable WAL 陈旧读测试（草稿库复现进 CI）(b) EQP 全过滤矩阵（archived 3×parent 4×cursor 2×search 2）+ 真库 P99 数据 (c) DB 路径解析设计（env+channel 复刻）(d) 降级矩阵逐格冻结（200+degraded / 503 边界）(e) search/complete/allowlist SQL 语义冻结 (f) AGENTS 不写措辞 + DDL 运维程序定稿 (g) 等价性锚定测试设计 | 无 | 低——纯设计+实证 |
| B1a | digest 定位字段（仅 `changed:[sid…]`，v3 契约修订 + minor） | B0 | 低 |
| B1b | q/p 事件驱动 + resync 对账 + 30min jitter sweep（载荷核对若「已完整」→ 零 wire 变更；否则 v4 部分移 B3b）；**exit criteria：sweep 流量 < 变更前基线（access-log 证明）** | B0 | 中 |
| B2 | ~~merged 截断 cap~~ **已按 owner 裁决废止**（正文永不截断，§3.3）→ 改为：merged 恢复范围对齐两模式裁剪的兼容验证 + 契约注记 | B0 | 低 |
| B4 | 加性路由 context/agent/model/revert-stage + directory 白名单 fail-closed | B0 | 低 |
| **B3a** | wire v4 第一刀：selector 双版本结构性改造 + v4 sessions **DB 投影源**（mode=ro/短事务/schema 门/降级矩阵/熔断/inode 校验）——独立 rev gate | B0 + P2（B5a 就绪） | 高——selector+DB 双重 |
| **B3b** | wire v4 第二刀：SSE id:/重放日志 + q/p 帧补全（若需）——独立 rev gate | B3a | 高——重放协议 |
| B5a | **消费者兼容版（= P2，先于 B3 发布）**：capabilities["4"] 探测 + 未知容忍 + v3 回退 + ocdroid 拦截器豁免 + status 单次全局调用 + webui q/p 直投（若零 wire 变更） | B0 | 低 |
| B5b | 消费者 v4 适配：全局列表 + cursor-aware 分组/收藏 + C1-C3 + /file | B3a/B3b | 中 |
| B6 | v4 稳定后清理：singleflight 合并 + sticky/三形状退役（生产流量证明前提） | B5b | 低 |

（B1a/B1b/B2/B4/B5a-客户端部分 并入 P1-P2 发布；B3a+B3b = P3 major；顺序编号保留便于引用。）

---

## 9. 决策记录与观察项

### 已定决策（owner 2026-08-17 + R2 修订）

| # | 决策点 | 决策 |
|---|---|---|
| 1 | v4 cursor | **(a) 无状态 keyset best-effort**；keyset 下推 SQL；指纹含 search-hash + allowlist-rev |
| 2 | sessions/status | **消费端单次全局调用**（上游本就全局）；sidecar 零新增 |
| 3 | q/p 优化 | 事件驱动 + resync 对账 + 30min jitter sweep（**60s 定时兜底废除**——285× 放大实证）；范围 = 降频+直投评估 |
| 4 | PTY / OAuth | 确认不做 |
| 5 | zstd | 不做 |
| 6 | /file + allowlist | **fail-closed**（空 → 403） |
| 7 | archived / 子会话过滤 | **提供**，经 **DB 投影源**实现（v2.2 升格：DB 为 v4 sessions 投影源，mode=ro 连接，**immutable 弃用**）；**索引移出 sidecar**（sidecar 零写入，索引 = 运维动作） |
| 8 | v4 parent 缺省 | **= all**（显式冻结；`parent=none` 承接 `roots=true`） |
| 9 | degraded 语义 | `degraded:true` 仅表数据源降级+强度弱化；**过滤语义永不降级**（可等价 → 200+degraded；不可等价 → 503） |

### 观察项（上游演进触发回收）

- 上游若原生提供全局 pending 端点 → 回收 sidecar q/p fan-out sweep（§3.2a）
- 上游若提供稳定 cursor（snapshot 型）→ 回收 sidecar cursor 自造逻辑
- 上游若提供 archived/parent 过滤 → 回收 DB 投影源（§3.1 降级为纯 HTTP 透传，等价性锚定测试随之退役）
- `upstream-global-model-2026-08-17.md` 的 /global/event 论断已实证有误——仅作历史参考，以本方案 §1.1 为准

---

## 附：三件套体系分工（终态，不变）

| 组件 | 职责 | 不做什么 |
|---|---|---|
| **oc-slimapi** | 全局门面：省流投影、策展 SSE（digest/token stream/id:重放）、全局目录（DB 投影源+降级矩阵）、增量原语、T3 准入、流量记账、directory 白名单 | 不做 UI、PTY/OAuth、不改上游、不写上游 DB（零 DDL/DML） |
| **ocdroid** | 移动端体验：本地分组/过滤、mTLS、离线缓存 | 不做扇出、不做能力探测补偿（v4 起） |
| **oc-webui** | web 端体验：收藏分组、文件树（/file 通路）、渲染 | 同上 |

---

## 附 B0：owner 三项裁决写回（2026-08-17 omni-orch 会话，B0 规范先行批次）

> 本附录为 B0 批落盘记录，**不改动上文行号**（正文行号引用持续有效）。三项裁决的工程落点见 `docs/refactor-plans/slimapi-refactor-plan.md` §8.1/§8.2。

1. **S-B01 ① tokens=1 统一流（对行 153 SSE 重放协议）**：**裁决 = v4 禁止复用**——`/events?tokens=1` 请求在 v4 返回 400，token 流必须走独立 `/sessions/{sid}/stream`。理由：单 Last-Event-ID 无法恢复双序列（meta-first 与重放顺序结构性矛盾：重连新 meta 分配新 seq 后发旧 replay 帧 = 线上 ID 倒退）；webui/ocdroid 本就分离两连接，成本最低。已写入 v4-contract §7.0/§7.3 与 design-v4-sse-replay.md。S-B01 ②③④（meta 重连语义 / token ID 作用域 / 逐帧状态机）由 B0-3 产出**设计提案**随 B0 汇报上报 owner 终裁——四项全部收敛记录是 B0 出门 gate。
2. **B1a digest `changed` 触发语义（对行 152）**：**裁决 = 最小语义**——`changed:[本帧sid]`（digest 为 per-sid 逐帧产出，帧出现即 changed，覆盖全部触发 digest 的事件：message.*/status/archived/deleted/updatedAt）；形状保留 `[sid…]` 列表为未来聚合留形；sidecar 零新增状态。
3. **B1b 阶段 2 exit criteria 口径（对行 159/163/166 的 S-B06 修法确认）**：**裁决 = B1b-5 现稿确认**——稳态窗连续 7 日；公式 = 真实 sweep 请求 + 事件驱动 + 客户端 q/p 相关请求合计 < 阶段 1 同期（7 日）实际基线（shadow 模拟计数不入基线）；载体 = traffic snapshot（保留期配置 >7 天）为主、access log（RETAIN_DAYS=3）短窗辅助；cold-set 长尾可达性作并列判据。行 166「<50 次/日量级」维持为预期描述、非验收口径。
