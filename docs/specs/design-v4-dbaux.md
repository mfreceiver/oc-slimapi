# 设计方案：v4 sessions DB 投影源（B0-5 工程化 + B0-6b/c/e 实证）

> **状态**：B0 批设计冻结版（2026-08-17）。v4 wire 契约以 `docs/specs/v4-contract.md` §4 为准（由编排者在 B0 批同步定稿）；本文档 = §3.1 数据源裁决的**工程化实现设计**（连接生命周期 / 所有权模型 / 路径解析 / 熔断重探 / 索引策略 / schema 门 / 降级矩阵 / SQL 语义冻结）+ B0-6(b)(c)(e) 三项硬门槛的实证记录。
> **引用基准**：`docs/system-architecture-proposal-2026-08-17.md` v2.2 §3.1（行 46-149，本文档所有「行 n」均指该文件行号）；`docs/refactor-plans/slimapi-refactor-plan.md` §2.1 B0-5 / B0-6 表、§5.3（降级矩阵）。
> **实现落地**：B3a 阶段 B（refactor-plan 行 236-241，B3a-B1 连接生命周期 + B3a-B2 投影 SQL 组装）；`src/oc_slimapi/dbaux/` 新建模块。
> **上游源码核对对齐版本**：`opencode-src/current` → v1.18.18（warehouse package.json 直测；真库所有采样标注「2026-08-17，对齐 v1.18.18」）。
> **实证脚本**：`scripts/eqp_matrix.py`（B0-6b，48 组合 EQP + 真库 P99）。

---

## 0. 裁决速览与待裁决清单

### 0.1 引用映射（v2.2 行号 → 本文档章节）

| v2.2 行 | 裁决内容 | 本文档 |
|---|---|---|
| 90-96 | mode=ro 主路径 / 短事务 / immutable 弃用 | §1 |
| 97 | 启动探测失败 → 全降级 | §1.4 |
| 98 | DB 路径解析（env + channel 复刻 + 启动 log） | §3 |
| 99 | inode/mtime 定期校验 | §4.1 |
| 100 | 错误分类 schema 重探 | §4.2 |
| 102-110 | 索引策略（无索引直跑 + 运维 DDL） | §5 |
| 111-124 | 降级矩阵 + degraded 语义 | §7 |
| 81, 84-88 | SQL 语义（组装容忍 / search / allowlist / keyset / legacy 空目录） | §8, §9 |
| 146 | schema 兼容门（全投影列版） | §6 |
| 147 | P99 < 20ms 性能护栏 | §2.7, §5.3 |

### 0.2 裁决记录（rev-1 结算：四项关闭转冻结/实证，两项保留为记录注）

> 纪律（工单）：发现 v2.2 矛盾/缺口 → 记「待裁决」并汇报。rev-1 评审实证后已结算：**R1/R2/R3/R6 关闭**（冻结或转为实证记录），仅留 R4/R5 两条非决策性记录注。

**已关闭（rev-1 冻结/实证确认）**：

| # | 裁决项 | rev-1 结论（冻结/关闭依据） |
|---|---|---|
| R1 | project join 列 | **关闭（实证）**：真库 `project` 表**含 `name`、无 `directory`**（直测 2026-08-17：12 列 = id/worktree/vcs/name/icon_url/icon_color/time_created/time_updated/time_initialized/sandboxes/commands/icon_url_override——评审记为 13 列为计数口径/live 差异，不影响门定义）；契约冻结投影链 = `project={id, name, worktree}`（上游 `session.ts:582` 同）。`p.directory` 模板废弃 |
| R2 | tokens 列名 | **关闭（冻结）**：投影 SQL 与 schema 门用真库列名 `tokens_input/tokens_output`（行 146 `tokens_*` 通配覆盖）；模板内不出现 `tokens_in/out`（v2.2 行 72 笔误） |
| R3 | channel 复刻 | **关闭（冻结为 fail-closed）**：因 channel 为编译期常量不可观测，**不做 channel 猜测**——§3 改单候选探测（唯一候选 → 采用 + warning；多候选/零候选 → 禁用辅助，reason=path_ambiguous\|not_found）。含 `OPENCODE_DISABLE_CHANNEL_DB` 分支复刻（§3.3） |
| R6 | parent=only 谓词 | **关闭（冻结，真库实证）**：`parent=only` → `parent_id IS NOT NULL`。依据：真库 parent_id 分布 NULL 86 / NOT NULL 321 / **空串 0**（2026-08-17 直测）→ 无空串哨兵歧义；上游亦无指向空串 parent 的语义 |

**保留记录注（非决策项）**：

| # | 项 | 说明 |
|---|---|---|
| R4 | path.ts 行号/路径漂移 | v2.2 行 87 引 `path.ts:41-52`；v1.18.18 对齐版 `directoryColumn` 在 `packages/core/src/database/path.ts:43-59`——语义一致，引用以对齐全为准（§9.4） |
| R5 | 真库行数漂移 | v2.2 行 106 记 384；实测持续漂移 406 → 407 → 408（live 库，归档操作中）——基线以实测为准（§5.2，2026-08-17 rev-1 复测 407/408） |

---

## 1. 连接生命周期（v2.2 行 90-101）

### 1.1 主路径 `mode=ro` + `PRAGMA query_only=ON` 防御层（行 94）

- 连接一律经 URI **`file:{abs_path}?mode=ro`** 打开（`sqlite3.connect(uri, uri=True)`），普通只读连接——经 `-shm` 正常读 live WAL 内容，与 opencode 常驻 writer 共存（sidecar 与 opencode 同机同用户，shm 访问成立，行 94 论证；部署形态变化 → 见 §1.4 失败处置）。
- 打开后立即 `PRAGMA query_only=ON` 作**防御层**：即使 `mode=ro` 被某种方式绕过/配置失误，任何写语句在执行期被 SQLite 拒绝（错误码 `SQLITE_READONLY`），与「sidecar 代码路径零 DDL/DML/PRAGMA 写」（行 109，AGENTS.md 措辞）形成双层保障。
- 真库核对（2026-08-17 实测）：`journal_mode=wal`（复刻 database.ts:27 实证），`mode=ro` 打开 + `query_only=ON` 下 48 组合投影 SQL 全数可执行（§5.2），WAL 内容经 shm 正常可见。

### 1.2 短生命周期只读事务（行 95）

- 每查询一组显式 `BEGIN … COMMIT`（deferred 只读事务，snapshot 语义）：
  - 请求内一致：单次 v4 sessions 请求的**全部查询**（投影 + complete 判定 LIMIT+1）在同一事务快照内执行——行 81「complete 由同查询窗口判定（同 snapshot）」由事务边界保证；
  - 不长期持读锁：快照在 COMMIT 即释放，不阻滞上游 WAL 回收（行 95 论证）；
  - 异常路径强制 `ROLLBACK` + 游标/语句关闭（`try/finally` 收尾）——防止 `cannot start a transaction within a transaction` 与连接脏状态（§2.3 冻结）。
- 事务原子单元 = 一条投影 SQL（组装容忍见 §8；**不做**多语句/跨请求长事务）。

### 1.3 `immutable=1` 完全弃用（行 96）

- **不作主路径、不作降级档**。实证依据（v2.2 行 92 三方独立复现 + B0-6a 进 CI 的 `tests/test_wal_staleness.py`）：
  - `immutable=1` **不读 live `-wal`**：WAL 内已 commit 的行不可见（草稿库 count=1 vs 2）；
  - 表建于 WAL 时 `immutable=1` 甚至报 `no such table`；
  - 陈旧读**不产生任何错误**（连接/查询均成功）→ 探测链无法检测 = **静默数据过期**，非可用性降级（行 92 结论）。
- 冷文件场景（无 `-wal`/`-shm`）`mode=ro` 若无 shm 且目录不可写会失败——但本部署（同机同用户、上游常驻 WAL）不成立；**若未来部署形态变化 → 直接禁用辅助源走 HTTP（行 96），不试 immutable**。

### 1.4 启动探测 + 失败降级（行 97）

- 启动（lifespan 装配，`app.py` TransformPool 装配段同区，app.py:292 先例选型见 §2.2）：
  1. 路径解析（§3）→ `mode=ro` 打开 → `PRAGMA query_only=ON`；
  2. **schema 门探测**（§6 全投影列版：session 表 + 投影列 + project 表 join 列）跑一遍只读校验查询；
  3. 任一失败 → **禁用辅助源 = 全降级 HTTP**（`auxiliary: {available:false, mode:"http"}`，行 140），**不试 immutable**；
  4. 启动 log 记：解析路径 / 门探测结果 / 辅助源状态（available|disabled；熔断为运行中态）。
- 禁用后的重探：**定期重探**（周期与熔断器共用，§4.2）——启动失败多为上游尚未就绪/路径错配，周期重探允许冷启动竞态自愈（不重启进程）。

---

## 2. S-B02 连接所有权与并发（engineered core）

> 源头：refactor-plan B0-5（行 120 内 S-B02 段）——「connection ownership and concurrency 独立小节，B0 设计必含」；R3 修复的线程亲和性冻结。**本节是 B3a-B1 并发模块的实现基准**。

### 2.1 并发执行模型选型论证

候选二选一（refactor-plan 行 120）：

- **方案 ① 每查询新建短连接**：连接即事务边界、无共享状态、天然隔离；代价 = 每次 open/close 开销 + fd 抖动（T3/高并发下有 fd 压力），且**多连接 = 多份 schema 门/熔断/inode generation 状态**——换代的 swap/重探/熔断无法围绕单一状态机串行化（R3 补强否决）。
- **方案 ② 单连接 + 串行化短事务 + executor offload**：共享连接生命周期状态机；单连接 = schema 门/熔断器/inode generation 的**单一状态机**（swap/重探/熔断全部围绕同一连接对象是串行化前提，行 120「R3 补强」）。

**冻结选型 = 方案 ② 的线程亲和实现（方案 1，下文）**——与 v2.2/refactor-plan「推荐单连接」（行 120）一致，且单 fd 语义吻合 `mode=ro`（行 94）。

### 2.2 线程亲和冻结：方案 1 专属 `ThreadPoolExecutor(max_workers=1)`（选型定案）

> R3 修复：`check_same_thread` 默认 True 下，连接在 event-loop 线程创建、查询在 executor worker 执行 = **立即线程错误**。方案 ② 下必须显式冻结线程归属，二选一：

| 维度 | **方案 1（冻结 ✓）**：专属 `ThreadPoolExecutor(max_workers=1)` 线程亲和 | 方案 2（否决 ✗）：`check_same_thread=False` + async lock |
|---|---|---|
| 线程归属 | **恒定**：connection 的建立/查询/rollback/重开/关闭**全部**在该专属 worker 内执行；event loop 侧仅经 async 封装等待结果 | 任意线程可访问（连接为多线程共享对象） |
| 锁需求 | **天然免锁**：worker 单线程串行 + 单连接 = 无需 `asyncio.Lock` 保护连接本身；队列本身即串行化边界 | 所有连接访问必须经**同一** async lock（并发访问防护） |
| check_same_thread | **默认 True 保持**：从不触碰（线程归属恒定 = 永不跨线程访问） | 显式 False（默认行为被破坏，需测试覆盖换线程语义） |
| 换代语义（swap/重探/熔断） | 单一 worker 内 FIFO 串行：swap 任务排在活跃查询后，**锁交接天然由队列保证**；generation 单一计数 | 换代需 lock 内完成，且旧引用可能在 lock 外的旧线程池残留——多线程/换代测试必做，风险高 |
| 风险 | 低：与 TransformPool 现有 offload 先例（app.py:292 `TransformPool` + `pool.offload(...)`）同构；worker 串行 = 查询天然排队（sidecar T3 并发上限可控） | 高：换线程语义 + 换代竞态需全量并发测试；check_same_thread=False 打开跨线程访问面 |
| TransformPool 关系 | **复用此池或独立小池，但 `max_workers` 必须固定 1**——不可用共享多 worker 池跑 DB 查询（多 worker 会并发访问同一连接 = 线程错误） | 复用任意池 + lock（池本身可多 worker） |

**冻结定案：方案 1。** asyncio 侧封装形态：`async def query(...) -> await loop.run_in_executor(db_worker, _sync_query, ...)` 或自建 `run_in_executor` 包装（模块内 `db_worker` 为 `ThreadPoolExecutor(max_workers=1)`，lifespan 装配/关闭，关闭时 drain 有界超时——对齐 `app.py` `_shutdown_transforms` 先例，app.py:298-309）。

### 2.3 冻结项（本小节强制实现清单）

1. **连接 swap generation + 锁语义**：inode 变化（§4.1）→ 提交 swap 任务到专属 worker：**等待活跃查询完成或失败后锁交接**（FIFO 队列天然实现：swap 排在本批查询后），swap 内执行旧连接 close → 新连接建立（同一 worker 内再次申报 `PRAGMA query_only=ON` + schema 门 + `BEGIN` 可用）→ generation 计数 +1；**禁止查询持锁跨 swap**——查询任务不得越过 swap 边界（worker 串行保证，无需显式锁；若实现引入锁，锁生命周期必须短于单查询）。
2. **查询异常强制 `ROLLBACK`/`finally` 收尾**：每查询 `BEGIN → execute → COMMIT`，`except/finally` 中 `ROLLBACK`（防 `cannot start a transaction within a transaction` 脏状态），游标 `close()`；异常上行给 async 封装（按 §4.2 错误分类）。
3. **`PRAGMA busy_timeout = 5000`**：与上游 `database.ts:29` 同值（上游 server 自身以 5000ms 等待 writer 锁）——sidecar 读端与 live writer 冲突时等待而非立即 `SQLITE_BUSY`；理由：短事务（§1.2）不长期持锁，busy 冲突窗口极小，5s 为上限防护而非常态；**不冲突 P99 < 20ms 护栏**（护栏以实测查询延迟计，busy 等待计入 → 超限熔断，语义自洽）。
4. **重探（schema 重探/inode 重开）与活跃查询的串行化边界**：重探 = 独占连接操作，提交到同一 worker、**等待锁队列清空**后执行（FIFO）；重探期间新查询排队（或按熔断状态直接拒绝，§2.7）。
5. **同步 sqlite3 调用绝不直接跑在 event loop 线程**：所有连接访问统一经专属 worker offload（铁律，含 schema 门、PRAGMA、计数查询）；event loop 侧只见 async 封装。
6. **P99 熔断样本口径（冻结）**：
   - **滑动窗口 60s**（查询延迟样本环形缓冲）；
   - **最小样本 ≥10 次才计 P99**（样本不足 = 不判熔断；冷启动 **前 M 次 warmup 豁免**，M=首次 10 次查询计入样本但仅走「不足 10 次不判」路径——与 oracle n3 联动：冷启动 P99 噪声不误熔断）；
   - **熔断阈值**：P99 ≥ 20ms（行 106,147 护栏）；
   - **恢复探针**：熔断后进入半开（half-open），周期（如 30s）单次探针查询，成功（且 P99 样本回落）→ 关闭熔断恢复；失败 → 保持熔断；
   - **hysteresis**：恢复阈值 < 熔断阈值（如 **恢复 P99 < 10ms** 才闭合，防抖——避免 20ms 临界抖动反复开关）；
   - 熔断状态进 health `auxiliary: {available:false, mode:"http", reason:"circuit_open"}`（行 140）+ metrics（降级计数/查询延迟，行 254 §9）。
7. **B3a-B1 并发阻断测试的线程亲和用例（R4 断言语义，冻结）**：
   - ① worker 外线程直接访问连接/游标 → **断言被 `check_same_thread` 拒绝（抛 `ProgrammingError`）**——方案 1 下这是**期望安全性质**（证明线程归属恒定、并发访问被底层禁止）；
   - ② 经专属 worker 封装的 async 调用 → 成功（断言语义唯一合法通道可用）；
   - ③ 换代后旧连接引用失效：swap 后旧连接对象调用 → 抛错/拒绝（generation 不匹配断言）。

---

## 3. DB 路径解析（B0-6(c)，v2.2 行 98）

### 3.1 解析优先级（冻结，rev-1 fail-closed 修订）

1. **`OC_SLIMAPI_OPENCODE_DB` 显式配置（生产推荐，最高优先）**：sidecar 自有 env；`":memory:"` → 禁用辅助；
2. **`OPENCODE_DB` 上游 env（可观测、无歧义）**：按上游语义复刻——`:memory:` → 禁用；绝对路径 → 直接用；相对路径 → 挂数据目录（`database.ts:44-46`）；
3. **channel 候选发现（不猜测，R3 冻结）**：上述两者均未配置时，枚举数据目录下 `opencode*.db` 候选：**恰一个 → 采用 + warning log**（注明「channel 未观测，单候选采用」）；**多候选并存 / 零候选 → 禁用辅助源**（fail-closed，`auxiliary.available=false`，reason=`path_ambiguous`|`not_found`）——不猜 channel、不猜默认库名（channel 为编译期常量不可观测，猜错 = 稳定选错文件永久降级，rev-1 否决猜测方案）；
4. 启动 log 记录实际解析路径（含来源：explicit env / OPENCODE_DB / candidate-discovery），便于运维核对（行 98）。

### 3.2 上游解析核对（源码实证）

`packages/core/src/database/database.ts:43-55`（v1.18.18 对齐版，package.json 直测）：

```
43  export function path() {
44    if (Flag.OPENCODE_DB) {                                    # flag.ts:47: OPENCODE_DB = process.env["OPENCODE_DB"]
45      if (Flag.OPENCODE_DB === ":memory:" || isAbsolute(Flag.OPENCODE_DB)) return Flag.OPENCODE_DB
46      return join(Global.Path.data, Flag.OPENCODE_DB)          # 相对路径 → 挂在 data 目录下
47    }
48    if (
49      ["latest", "beta", "prod"].includes(InstallationChannel) ||
50      process.env.OPENCODE_DISABLE_CHANNEL_DB === "1" ||
51      process.env.OPENCODE_DISABLE_CHANNEL_DB === "true"
52    )
53      return join(Global.Path.data, "opencode.db")
54    return join(Global.Path.data, `opencode-${InstallationChannel.replace(/[^a-zA-Z0-9._-]/g, "-")}.db`)
55  }
```

配套事实：

- **数据目录** `Global.Path.data` = `path.join(xdgData, "opencode")`（`packages/core/src/global.ts:10-11`），XDG 默认 → `~/.local/share/opencode`（真库实测即 `~/.local/share/opencode/opencode.db`，2026-08-17 5.6GB + `-wal` 19.9MB + `-shm` 32KB，行 18 实证同源）。
- **`InstallationChannel`** 为**编译期常量**（`packages/core/src/installation/version.ts:1-7`：`declare global const OPENCODE_CHANNEL`，默认 `"local"`）——运行时无 env 注入；官方发布二进制 channel 由构建注入（典型 latest）。→ **sidecar 不可观测该注入值 → R3 冻结为候选发现（§3.1-3）：不做任何 channel 猜测**。

### 3.3 解析逻辑伪代码（定稿，直接落 B3a-B1 实现）

```
def resolve_db_path() -> ResolvedPath | Disabled:
    # 1. sidecar 显式配置（生产推荐，R3 后最高优先）
    if OC_SLIMAPI_OPENCODE_DB is set:
        p = expanduser(OC_SLIMAPI_OPENCODE_DB)      # ~ 展开
        if p == ":memory:": return Disabled(reason="explicit-memory")
        return Resolved(path=normpath(p), source="OC_SLIMAPI_OPENCODE_DB")
    data_dir = XDG_DATA_HOME or "~/.local/share"  + "/opencode"   # global.ts:11 复刻
    # 2. 上游 OPENCODE_DB env（flag.ts:47，可观测，无 channel 猜测）
    if OPENCODE_DB set:
        raw = OPENCODE_DB
        if raw == ":memory:": return Disabled(reason="upstream-memory")   # 行 98
        if isabs(raw) or raw.startswith("~"):       # isAbsolute 复刻（POSIX 语义）
            return Resolved(path=normpath(expanduser(raw)), source="OPENCODE_DB")
        return Resolved(path=normpath(join(data_dir, raw)), source="OPENCODE_DB-relative")  # database.ts:46
    # 3. channel 候选发现（fail-closed，R3 冻结；含 OPENCODE_DISABLE_CHANNEL_DB 情形
    #    由候选枚举自然覆盖——该开关只影响上游建库名，sidecar 按盘上文件事实判定）
    candidates = sorted(glob(join(data_dir, "opencode*.db")))   # 含 opencode.db / opencode-local.db / opencode-<ch>.db
    if len(candidates) == 1:
        return Resolved(path=normpath(candidates[0]), source="candidate-discovery",
                        warning="channel 未观测（编译期常量），单候选采用")
    if len(candidates) > 1:
        return Disabled(reason="path_ambiguous", detail=candidates)  # fail-closed
    return Disabled(reason="not_found", detail=data_dir)
```

要点：`~` 展开（显式配置路径）、相对/绝对规范化（`normpath`）、尾斜杠归一、空白 trim；解析结果（含 source / reason / warning）进启动 log（§1.4）；`OPENCODE_DISABLE_CHANNEL_DB` / `OPENCODE_CHANNEL` 的枚举复刻见 §3.2 源码块（决策路径已由候选发现替代，不再作为侧car 猜测依据）。

### 3.4 单元测试用例表（~11 case，B3a-B1 落地 `tests/test_db_path_resolution.py`）

| # | case | 输入 | 期望 |
|---|---|---|---|
| 1 | 显式 env 优先 | `OC_SLIMAPI_OPENCODE_DB=/x/y.db` + `OPENCODE_DB=/z.db` + data 目录多候选 | `/x/y.db`（source=explicit） |
| 2 | OPENCODE_DB 继承（绝对） | 仅 `OPENCODE_DB=/z.db` | `/z.db`（source=OPENCODE_DB） |
| 3 | OPENCODE_DB 继承（相对） | 仅 `OPENCODE_DB=rel/db.db` | `<data>/rel/db.db`（join+normpath，source=OPENCODE_DB-relative） |
| 4 | 单候选发现（含 channel 名） | 无 env db，data 目录仅 `opencode.db`（或仅 `opencode-local.db`） | 采用该文件 + warning「channel 未观测，单候选采用」 |
| 5 | 多候选 fail-closed | 无 env db，data 目录同时存在 `opencode.db` 与 `opencode-local.db` | Disabled(reason=`path_ambiguous`)，不猜测 |
| 6 | 零候选 fail-closed | 无 env db，data 目录无任何 `opencode*.db` / 目录不存在 | Disabled(reason=`not_found`) |
| 7 | `:memory:` 禁用 | `OC_SLIMAPI_OPENCODE_DB=:memory:`（或 `OPENCODE_DB=:memory:`） | Disabled（不打开） |
| 8 | 相对/绝对规范化 | `OPENCODE_DB=./a.db` → `<data>/a.db`（join+normpath） | normpath 后无 `.`/`..` |
| 9 | `~` 展开 | `OC_SLIMAPI_OPENCODE_DB=~/db.db` | `<home>/db.db` 绝对路径 |
| 10 | 尾斜杠 / 空白 | `OC_SLIMAPI_OPENCODE_DB=/x/y/`（尾斜杠）或 `" /x/y.db "` | `normpath(/x/y)` + strip 后 `/x/y.db` |
| 11 | 双 env 冲突 | `OC_SLIMAPI_OPENCODE_DB` 与 `OPENCODE_DB` 同时存在且不同 | 显式 env 胜出（case 1 语义）；优先级冻结不告警不合并 |

**runtime 步骤**（进 B3a-B1 阻断测试，非 B0）：**冷启动**——启动 log 断言实际解析路径 + 辅助源状态；**运行中 inode swap**——替换 DB 文件（备份恢复/channel 切换模拟）后观察重开重探日志、期间查询不挂死（§4.1 场景）。

---

## 4. inode/mtime 校验 + 错误分类重探（行 99-100）

### 4.1 inode/mtime 定期校验（行 99）

- **机制**：周期任务（与熔断恢复探针同调度器，如 30s，对齐 §2.7 恢复探针节奏）`stat(解析路径)` 对比 `st_ino`/`st_mtime_ns`；变化 → 提交 **swap**（§2.3-1）：关闭旧连接（持旧 fd 读已删 inode 的隐患消除）→ 重开 → schema 门重探 → 更新 generation。
- **场景**：备份恢复 / channel 切换换 DB 文件 / 运维手动替换 → sidecar 持旧 fd 读已删 inode（内容陈旧但无错误——不可见静默过期类风险，需主动校验，行 99）。
- **挂熔断器周期**：swap 期间查询排队等待（专属 worker FIFO，§2.2）；swap 失败（新文件不可打开/门不过）→ 熔断禁用 + 周期重试（回到 §1.4 禁用态语义）。
- 补充观察：`-wal`/`-shm` 文件**不参与** inode 校验（由主文件 inode 变化代表换库；wal 增删属上游正常运行）。

### 4.2 错误分类 → 处置表（行 100；非仅启动一次）

| 错误观察（查询/探测抛错） | 判定 | 处置 |
|---|---|---|
| `SQLITE_SCHEMA` / schema 变更（列缺/表缺；上游升级） | schema 门失效 | 熔断禁用 → **周期重探**（按 §2.3-4 串行化）→ 门过则恢复；门不过保持禁用（全降级 HTTP） |
| `no such table: session` / `no such column: ...`（查询期出现 = 运行中 schema 变更） | schema 门失效（同上） | 同上（与 `SQLITE_SCHEMA` 合并路径） |
| I/O 错误（disk 错误码类，如 `SQLITE_IOERR*`） | 数据源物理不可用 | 熔断禁用 → 周期重探（同 §2.3-4）；持续失败保持禁用 |
| WAL-SHM 不可达（`SQLITE_READONLY_CANTINIT` / `unable to open database file`（shm 路径）） | 只读路径失效（无 shm 且目录不可写场景，行 96 例外路径） | 熔断禁用 → 周期重探；**不试 immutable**（行 96 铁律）——重探周期性允许形态恢复（如目录权限恢复）自愈 |
| `SQLITE_BUSY` 超 busy_timeout（5s） | writer 长持有锁 | 计入查询延迟样本 → P99 超限熔断路径（§2.7）；短事务设计下罕见 |
| `ProgrammingError`（check_same_thread 违背，线程亲和被破坏） | 实现缺陷信号（非用户可恢复） | 熔断 + error log（B3a-B1 线程亲和用例断言的方向：worker 外访问必须被拒） |

- 分类入口统一在查询封装层（async 包装内捕获 `sqlite3.Error` 子类 + 消息匹配），输出到 metrics（行 254 §9：DB 查询延迟/降级/熔断计数）。
- **错误体不泄露 DB 路径/schema 细节**（行 122）：分类信息只进日志/metrics，HTTP 侧一律 503 `auxiliary_unavailable` 统一错误体（§7.3）。

---

## 5. 索引策略（行 102-110）+ B0-6(b) EQP/真库实证

### 5.1 首期无索引直跑（冻结）

- **sidecar 永不写上游 DB（含 DDL）**（行 107）：D-ix 移出 sidecar（v2.1「sidecar 幂等建索引」撤回；AGENTS.md 措辞行 109 同步）。**索引 = 运维手册动作**：仅当生产 EQP + P99 数据证明必要时，运维显式执行（`docs/operations.md` 记录程序）；候选 = **sort-shaped 独立 `(time_updated DESC, id DESC)` 索引**（服务 keyset 排序，非 v2.1 filter-shaped 复合索引，行 108）；`CREATE INDEX IF NOT EXISTS` 不验证列定义 → 运维程序必须含 **`PRAGMA index_xinfo` 定义校验**（防同名异构误判，行 108）。
- 排序正确性不依赖索引：`ORDER BY (time_updated DESC, id DESC)` 恒成立（行 104：keyset 排序正确性来自 SQL，索引仅性能）。

### 5.2 B0-6(b) 实证数据（`scripts/eqp_matrix.py`，2026-08-17 rev-1 实测，对齐 v1.18.18）

**草稿库 48 组合全矩阵**（`--rows 1000 --limit 100`，SQL `LIMIT 101`；无任何用户索引，仅 PK autoindex）：

| 断言 | 结果 |
|---|---|
| 组合数 | 48（archived 3 × parent 4 × cursor 2 × search 2）|
| 行集精确匹配（rowcount + 排序后前 K id） | **48/48 PASS**（Python 内镜像谓词的 oracle 比对） |
| planner：`SCAN session` | **48/48**（零索引前提，纯全表扫） |
| planner：`SEARCH session USING <index>` | 0/48 |
| planner：`USE TEMP B-TREE FOR ORDER BY` | **48/48**（无 `time_updated` 索引覆盖排序 → 排序由临时 b-tree 承担，与行 104 结论一致） |

**真库采样**（`~/.local/share/opencode/opencode.db`，`file:...?mode=ro` + `query_only=ON` + `busy_timeout=5000`，48 组合 × 50 次 = 2400 样本；采样 2026-08-17 rev-1 复测，对齐 v1.18.18；投影 SQL 含契约冻结 `p.name`）：

| 指标 | 实测 | 对照 |
|---|---|---|
| session 行数 | **407**（复测窗口 408——live 库持续漂移，v2.2 行 106 记 384，R5 记录注）|
| P50 | **0.0283 ms** | 行 106「~0.015ms 温测」同量级（含 Python sqlite3 调用开销） |
| **P99** | **1.152 ms** | 幅距 **>17× 裕量**（护栏 20ms，行 106/147） |
| mean / max | 0.149 ms / 2.01 ms | — |
| planner：SCAN session | 24/48（parent=all / parent=only） |
| planner：SEARCH session（命中上游自带索引） | 24/48（parent=none / parent=<sid>）——`parent_id IS NULL` 与 `parent_id = ?` 均命中 **`session_parent_idx`**（上游自身索引，非 sidecar 所建；真库 `PRAGMA index_list(session)` = workspace/parent/project 三索引 + PK autoindex，**无 time_updated 索引**） |
| planner：`USE TEMP B-TREE FOR ORDER BY` | 48/48（无 time_updated 覆盖 → 排序恒由临时 b-tree 承担） |
| schema 门（投影列 + project join 列 id/name/worktree） | **通过**（§6 核对记录） |

**结论（冻结）**：

1. **无索引直跑成立**：~407 行真库 P99 ≈ 1.15ms（最坏组合 ≈ 2.0ms），远低 20ms 护栏 → **首期无索引直跑**，DDL 程序保持运维态（行 106）；
2. **e2e 性能走势**：全表扫为 O(N)，行数增长（如 5k+）时 EQP 特征不变但延迟线性上升 → P99 < 20ms 熔断护栏（§2.7）兜底，超限自动降级 + 运维按 §5.1 建 sort-shaped 索引；
3. 真库 EQP 特征与草稿库差异仅来自**上游自有索引**（session_parent_idx），非 sidecar 行为——sidecar 零索引假设与真实运行一致（只读复用上游既有索引）。

### 5.3 P99 < 20ms 护栏（行 106,147）

- 护栏 = 每查询延迟采样（§2.7 口径：60s 滑窗 + ≥10 样本 + warmup 豁免）P99 ≥ 20ms → 熔断 → 全降级矩阵（§7）+ 告警；恢复走半开探针 + hysteresis。
- 护栏与降级矩阵联动：熔断期间 wire 行为 = §7「DB 熔断/禁用」列（allowlist 空可等价格除外——200+degraded）。

---

## 6. schema 兼容门（全投影列版，行 146）+ 真库核对记录

### 6.1 门定义（冻结）

启动探测（§1.4）+ 错误重探（§4.2）共用：

1. `session` 表存在，且**全部投影列**存在（缺任一 → 禁用辅助降级 HTTP）：
   `id, parent_id, project_id, time_archived, time_updated, directory, title, agent, model, version, summary_*（additions/deletions/files/diffs）, tokens_*（input/output/reasoning/cache_read/cache_write）, time_*（created/updated/compacting/archived）, revert, permission, metadata`（行 146 通配展开 = 真库实测列名；R2 已实证关闭：模板 tokens_in/out 为撰写笔误，以真库列名 tokens_input/output 为准）；
2. `project` 表存在且 join 列齐备：`id` + `name` + `worktree`（**契约冻结 `project={id,name,worktree}`**；v2.2 行 74 的 `directory` 列真库不存在——已实证，R1 关闭；门以实际投影读取的列为准）；
3. 门校验方式：`PRAGMA table_info(session)` / `PRAGMA table_info(project)` 只读比对（不做任何写入/DDL 尝试）。

运行中错误分类触发重探（§4.2）；上游版本升级 schema 变更 → 等价性锚定测试矩阵覆盖（§10，行 148）。

### 6.2 真库投影列核对记录（B0 实证，2026-08-17，`file:...?mode=ro`）

`session` 表（真库 29 列）与投影列逐一对齐：

| 投影列（行 146） | 真库列（PRAGMA table_info(session)） | 状态 |
|---|---|---|
| id | `id TEXT PK` | ✓ |
| parent_id | `parent_id TEXT` | ✓ |
| project_id | `project_id TEXT NOT NULL` | ✓ |
| time_archived | `time_archived INTEGER` | ✓ |
| time_updated | `time_updated INTEGER NOT NULL` | ✓ |
| directory | `directory TEXT NOT NULL` | ✓ |
| title | `title TEXT NOT NULL` | ✓ |
| agent / model / version | `agent TEXT` / `model TEXT` / `version TEXT NOT NULL` | ✓ |
| summary_\* | `summary_additions/summary_deletions/summary_files/summary_diffs`（INTEGER×3 + TEXT） | ✓ |
| tokens_\*（行 72 模板 tokens_in/out；R2 已实证关闭） | `tokens_input/tokens_output/tokens_reasoning/tokens_cache_read/tokens_cache_write`（INTEGER NOT NULL） | ✓（真库列名实证冻结） |
| time_\* | `time_created/time_updated/time_compacting/time_archived` | ✓ |
| revert / permission / metadata | `revert TEXT` / `permission TEXT` / `metadata TEXT` | ✓ |

`project` 表（真库 12 列，含 `name`、无 `directory`；2026-08-17 直测，对齐 v1.18.18）join 列：

| join 列 | 真库列 | 状态 |
|---|---|---|
| id | `id TEXT PK` | ✓ |
| name | `name TEXT` | ✓（契约冻结投影链含 name，上游 projective upgrade 同 `session.ts:582`） |
| worktree | `worktree TEXT NOT NULL` | ✓ |
| ~~directory~~（行 74 模板） | **不存在**（12 列 = id/worktree/vcs/name/icon_url/icon_color/time_created/time_updated/time_initialized/sandboxes/commands/icon_url_override） | 实证记录（R1 关闭） |

**parent_id 分布（2026-08-17 rev-1 直测，R6 冻结依据）**：NULL **86** / NOT NULL **321** / 空串 **0**（合计 407 ≈ 现 407-408 live 行）→ `parent=only` = `parent_id IS NOT NULL` 无空串哨兵歧义。**LIKE/`=` 大小写实测**：`'Foo' LIKE 'foo'` = 1（ASCII 大小写**不敏感**）、`'Foo' = 'foo'` = 0（二进制**敏感**）→ §9.3 谓词必须回避 LIKE 通配规则（rev-1 冻结，Fix 3）。

**核对结论**：门在真库（2026-08-17 直测，对齐 v1.18.18）**通过**（eqp_matrix `--real-db` gate 输出 `gate_passes: True`；session 29 列 / project join 三列 id+name+worktree 无缺失，`missing=[]`）；v2.2 行 74 模板的 `p.directory` 与真库冲突 → R1 关闭（实证记录），本文档 SQL 模板以 `p.id/p.name/p.worktree` 为准（§5.2、§9 已用）。

---

## 7. 降级矩阵 ≈72 格生成规则冻结（行 111-124 + ora B-2）——B0-6(d)

> 输入：refactor-plan §5.3 12 格行为表（4 需求行 × allowlist 2 态）+ B0-6(d) 口径（12 格 × DB 三态 × allowlist 两态 ≈ 72）。**本节 = 生成规则（formula），逐格语义由编排者同步进 v4-contract §4；本节冻结的是规则本身。**

### 7.1 维度与符号（rev-1 坐标系修正）

- **坐标系（rev-1 冻结）**：**72 格 = 行为等价类坐标系 `(req 12 × db 3 × al 2)`**；`cursor` 与 `search` **不进坐标系**——二者是**正交叠加轴**：任何格叠加 cursor/search 时行为按 §7.2 formula 对应行改写（cursor：db-avail → 仍 200 + keyset 下推；db-不可用 → 503；search：见下）。
- **需求态** `req` ∈ 12 格 = `archived(3) × parent(4)`：
  - Class A（可等价表达组，**4 格**）：`archived ∈ {omit, all}(2) × parent ∈ {all, none}(2)`
  - Class B（不可表达组，**8 格** = 12 − 4）：`archived=only × parent 任意`（4 格）∪ `parent ∈ {only, <sid>} × archived ∈ {omit, all}`（2×2=4 格；`parent=only` 谓词冻结见 §6.2/R6）
- **cursor** 正交轴：db-avail → 200（keyset 下推）；db-不可用 → **503**（行 120；上游单键 cursor 无法兑现 `(t,i)` keyset 指纹，session.ts:562 实证）。
- **search** 正交轴（rev-1 收紧，Fix 2）：db-avail → 入 SQL（字面转义子串，§9.1）；db-不可用 → **search 含 `%`/`_`/`\` 任一字符 → 503**（不可等价——上游 `LIKE '%…%'` 无 ESCAPE，`%`/`_` 为通配（session.ts:563 实证），`\` 保守加宽，§9.1）；纯字面子串（无三字符）→ 透传上游等价 → 按 Class A/B 规则走。
- **DB 态** `db ∈ {avail, disabled, tripped}`：avail = 连接可用 + 门过 + 未熔断；disabled/tripped 对 **wire 行为同构**（都是「辅助源不可用」），仅恢复机制不同（§4.1/§4.2；行 97「禁用」、§2.7「熔断」）——**同一坐标系内各占一半格数，行为一致**。
- **allowlist 态** `al ∈ {empty, nonempty}`（S-B05 三态中「未配置机制」= empty：env 未配置 = 机制未启用、无过滤义务，§5.3 例外；非空 = 白名单过滤义务在身）。

### 7.2 生成规则（formula，冻结）

```
result(req, db, al, cursor, search):
  if db == avail:
      → 200，全过滤入 SQL 谓词（archived × parent × search(字面转义) × cursor(keyset)
        × allowlist 子树谓词 + 指纹）
        （allowlist 维度不影响状态码，只影响 SQL 谓词与 cursor 指纹，行 78,85）
  else:  # disabled | tripped —— wire 行为同构（§7.1 db 态），全降级 HTTP /experimental/session（行 113）
      if al == nonempty:
          → 503 auxiliary_unavailable（fail-closed，ora B-2 选②）
             ——不做「首 N 行后置过滤/内部循环翻页凑行」（真子集风险 + 撕裂单快照原子性，§5.3 论证 a/b/c）
      elif has_wildcard(search):   # 含 %/_/\ 任一（§9.1 判定，rev-1 收紧）
          → 503 auxiliary_unavailable（search 不可等价表达，与 Class B 同类）
      elif cursor:                 → 503 auxiliary_unavailable（正交硬闸，行 120）
      elif req ∈ Class A:          → 200 + degraded:true
          （parent=none → roots=true；parent=all → 不过滤；search 纯字面透传；
            排序退化上游单键 time_updated（tie-break 弱）、complete 退 best-effort——degraded 披露，行 117）
      else:                        → 503 auxiliary_unavailable（Class B：archived=only / parent=only|<sid>，行 118-119）
```

### 7.3 逐格语义（72 格展开，紧凑矩阵）——cursor/search 缺席基线

**坐标系格子（`cursor 缺席` + `search 任意` 为基线；search 轴对基线的改写见下表注）**：

| 坐标系格（12 req × 3 db × 2 al = 72） | 格数 | 行为 |
|---|---|---|
| db=avail × al=empty：12 req | 12 | **全 200**，SQL 全过滤，`degraded` 缺席 |
| db=avail × al=nonempty：12 req | 12 | **全 200**，SQL 全过滤 + allowlist 子树谓词（§9.3），cursor 指纹含 allowlist-rev；**allowlist 维度不影响状态码** |
| db=disabled × al=empty | 12 | Class A 4 格 → **200 + `degraded:true`**（上游等价，行 117）；Class B 8 格 → **503**（行 118-119） |
| db=tripped × al=empty | 12 | **同上（wire 行为同构）**：Class A 4 格 200+degraded；Class B 8 格 503 |
| db=disabled × al=nonempty | 12 | **全 503 `auxiliary_unavailable`**（fail-closed，ora B-2 选②） |
| db=tripped × al=nonempty | 12 | **全 503**（同上） |

**计数核对**：72 = 24（db-avail）+ 48（db-不可用 = disabled/tripped × al 两态 = 4 组 × 12 req）。db-不可用 × al-empty 24 格 = **Class A 8 格（4 req × 2 db 态）200+degraded + Class B 16 格（8 req × 2 db 态）503**；db-不可用 × al-nonempty 24 格全 503。

**正交叠加轴**（不增加坐标系格数，改写部分格的行为）：

| 叠加轴 | db-avail | db-不可用 |
|---|---|---|
| 叠加 `cursor`（任何 req） | 仍 200（keyset 下推，SQL 谓词 + 指纹） | → 503（硬闸，行 120） |
| 叠加 `search 含 %/_/\` | 仍 200（字面转义后入 SQL，§9.1） | → 503（不可等价，rev-1 收紧）——Class A 格亦 503 |
| 叠加 `search 纯字面` | 仍 200 | 不改变基线行为（透传等价：Class A → 200+degraded；Class B → 503） |

**测试落地口径（rev-1 冻结）**：72 等价类基线用例 + `cursor` 两态**正交参数化** = **144 参数化组合**（72 基线语义单表 + cursor 叠加断言；与 v4-contract §11.3 对齐由编排者执行）。

### 7.4 冻结语义

- **503 统一附 `Retry-After`**（行 122；建议 30s，与熔断恢复探针节奏同量级）；错误体**不泄露 DB 路径/schema 细节**（行 122）+ 不泄露白名单内容（§5.3 断言）——统一错误体 `auxiliary_unavailable`（?v=4 结构见 v4-contract §8，编排者落）。
- **`degraded:true` 语义冻结**（行 123/64）：只表**数据源降级 + 排序/complete 强度弱化**；**过滤语义永不降级**——可等价表达 → 200+degraded，不可表达 → 503（行 123）；allowlist 维度上「过滤语义」= 白名单 ⊆ 结果集（放行不失、禁止不漏，§5.3 边界原则）。
- **瞬态能力面恒定**：`auxiliaryFilters` 为静态能力键（v4 存在即广告，行 140）；瞬态可用性经 503 + `/slimapi/health` `auxiliary: {available, mode, reason?}` + metrics（降级计数/查询延迟；行 254 §9）。
- 逐格语义表（含 allowlist 维度版本）**同步进 v4-contract §4**——由编排者在 B0 批契约定稿时完成，本节仅冻结生成规则与上表语义。

---

## 8. 组装容忍语义（行 84）

| 场景 | 行为（冻结） | 依据/实证 |
|---|---|---|
| project join 缺行（`LEFT JOIN` 无匹配；project 行被删/迁移中） | `project=null`（不 500、不丢 session 行） | 行 84；上游 `session.ts:595`：`projects.get(row.project_id) ?? null` 实证同语义 |
| JSON 列解析失败（`summary_diffs`/`metadata` 等非法 JSON） | **跳过该会话行 + warning log**（不 500） | 行 84 |
| 键集内行缺失（如投影要求的子键不存在） | 该键缺失即跳过（保持行级容忍） | 行 84 |

- 容忍语义只作用于**组装层**（行级后处理）；SQL 谓词层（§9）无此自由度（谓词决定行集，行集内再容忍）。
- warning log 走既有 logger（`get_logger`），带 sid 便于定位。

---

## 9. SQL 语义冻结（B0-6(e)，行 81, 85, 86, 87）——四条逐条

> 落地：B3a-B2 投影 SQL 组装；测试模块 `tests/test_sql_semantics.py`（B3a 落地，B0 固用例表）。

### 9.1 search = `LIKE ? ESCAPE '\'` + 规范化 hash 进 cursor 指纹（行 86）

- **规范化同源（rev-2 冻结）**：`normalized_search = trim(raw)` 为**唯一输入源**——SQL pattern 构造、`has_wildcard` 判定、cursor 指纹 hash **三者全部读取 normalized_search**（禁一处用 raw 一处用 trim——否则 `" foo "` 与 `"foo"` 两请求同 hash，后续页在错误过滤上下文续走）。指纹 = normalized_search 经转义后的串 hash（sha256 截断 8-16 hex）。
- **谓词**：`(:search IS NULL OR s.title LIKE :search ESCAPE '\')`；`:search` = `%` + LIKE 字面转义（normalized_search）+ `%`。
- **字面转义（冻结）**：用户输入中的 `%`、`_`、`\` 在构造 pattern 时以 `\` 前缀转义（`\%%`、`\_`、`\\`）；`ESCAPE '\'` 使 `%`/`_` 不再具备通配语义——**search 语义 = 字面子串匹配**（行 57「标题子串」契约兑现）。
- **游标指纹**：search 的规范化形式（normalized_search = trim(raw)，§「规范化同源」）hash 进 cursor 指纹 `f.search_hash`（行 127）；**同一输入两次执行 → hash 相同（规范化算法确定性 = 测试断言）**；指纹不匹配 → 400 `invalid_cursor`（行 129）。
- 降级路径（rev-1 冻结，行 86 收紧）：DB 不可用（disabled/tripped）时——**search 含 `%`/`_`/`\` 任一字符 → 503 `auxiliary_unavailable`**：不可等价表达——上游降级透传为 `LIKE '%…%'` 且**无 ESCAPE**（session.ts:563 实证），`%`/`_` 在 SQLite LIKE 中为**通配符**，会放大行集（如 `search="100%"` 会匹配含 `100%` 之外的行），与 DB 侧字面转义子串行集不同 → 违反「过滤语义永不降级」（行 123）；`\` 在上游无 ESCAPE 声明时为字面（与 DB 侧字面一致，理论可等价）——纳入 503 为**保守加宽**（仅牺牲少数本可等价的反斜杠搜索，换取规则单一）；**判定函数 `has_wildcard(s) = any(c in s for c in '%_\\')` 为确定性函数（与 §9.1 游标指纹规范化共用同一判定 → 同输入两次执行结果一致，指纹 hash 确定性断言覆盖）**。**纯字面子串**（无 `%`/`_`/`\`）→ 透传上游等价（`%…%` 包裹下与字面子串匹配一致）→ 按 §7 Class A/B 规则走（Class A → 200+degraded（降级披露），Class B → 503）。

### 9.2 complete = LIMIT+1 同 snapshot（行 81, 137）

- 单条 SQL：`LIMIT :limit + 1`；实际返回 `limit+1` 行 → 存在第 `limit+1` 行 = **complete:false**（有下一页）；返回 ≤ `limit` 行 → **complete:true**。
- 判定与列表在同**一只读事务快照**内（§1.2）——同一 snapshot 窗口（行 81 注记，rev-1 M9）。
- 降级路径 = 上游 best-effort complete（`degraded` 披露，行 137）。

### 9.3 allowlist 非空 → `s.directory` IN allowlist 子树谓词 + cursor 指纹（行 78, 85；rev-1 谓词冻结 Fix 3）

- **子树谓词（冻结，弃 LIKE 改二进制前缀）**：每 allowlist 项 `d`（resolve 规范化后，absolute 非 realpath，同 §9.4 与上游存储语义）：

```sql
(s.directory = :d_raw
 OR substr(s.directory, 1, :prefix_len) = :prefix)
-- :prefix     = :d_raw || '/'       独立绑定参数（不做 LIKE 转义）
-- :prefix_len = length(:d_raw) + 1
-- substr 与 = 均为默认 BINARY 比较 → 大小写敏感、无 %/_ 通配语义
```

  多项 OR 合并（allowlist 并集）。
- **弃 LIKE 依据（rev-1 实测，Fix 3）**：旧方案 `= :d OR LIKE :d||'/%' ESCAPE '\'` 三处缺陷——① SQLite 默认 LIKE 对 ASCII **大小写不敏感**（实测 `'Foo' LIKE 'foo'`=1）→ `/foo` 白名单会放行 `/Foo/child`（白名单语义泄漏）；改 `substr =`（默认 BINARY，实测 `'Foo' = 'foo'`=0）→ 全链大小写敏感；② allowlist 根 `/` 会生成 `//%` 前缀不匹配任何单斜杠路径；③ LIKE 转义参数（`%`/`_`/`\`）与 equality 分支复用冲突。`substr`+`=` 异质绑定参数三者全解。
- **根目录特例（冻结）**：allowlist 项 = `/` → 独立分支 `substr(s.directory, 1, 1) = '/'`（匹配**所有非空绝对路径**；真库 2026-08-17 直测：407 行 directory 全部非空且以 `/` 开头）；`/` 不与 `//` 前缀规则混算（长度 1 特判，独立谓词）。
- **边界语义（S-B08，冻结）**：
  - **大小写敏感**：`=`/`substr` BINARY 比较（实测依据见上）；比较双方 = 存储值 vs 规范化后的 allowlist 项；
  - **无通配语义**：`substr`/`=` 不解释 `%`/`_`/`\`——路径段含这些字符时**字面匹配**（如目录 `/a%20b/c` 在白名单 `/a` 子树内正常命中，`%` 不放大）；
  - **前缀边界（子树闭合）**：`/foo` 的子树 = `/foo` 自身 + 所有 `d + '/'` 后代；**不含** `/foobar`、`/foox/…`（`'/'` 闭合语义：`:prefix = :d_raw || '/'`，绝不用裸前缀）；
  - **symlink**：与上游存储语义一致（`directoryColumn` 仅 `absolute()` 不做 realpath 解析，`database/path.ts:53-58`）→ 过滤是**字符串前缀匹配于存储值**，不追踪 fs 实体（防 `..`/symlink 绕过由 allowlist 入口层 B4-4 负责，行 185）；
  - **空 directory 行**：`substr('',…)` 与 `= :d_raw` 谓词对非空 `d_raw` 均不命中 → **允许其中任一（allowlist 非空）查询时天然排除**（legacy 空串行无目录归属，允许语义；既有行为不变）。
- **cursor 指纹**：非空 allowlist 修订（集合变化）进指纹 `f.allowlist-rev`（行 127, 85）——中途变更 → 指纹不匹配 → 400 `invalid_cursor`（重开首屏，行为可预期）。
- 空 allowlist（机制未启用）→ 无该谓词（零影响，启动零差异化）。

### 9.4 legacy 空 directory 规范化复刻（行 87；对齐源码 `database/path.ts:43-59`）

- **上游事实**（v1.18.18 对齐版核对，R4 行号漂移注）：`directoryColumn` 的 `toDriver`/`fromDriver` = `input ? absolute(input) : input`（`database/path.ts:53-58`）——**legacy 会话持久化的空目录保留空串原值**（注释原文：*"Legacy sessions may persist an empty directory. Keep that existing value readable while normalizing and validating every real directory."*，`:43-44`）；非空值规范化（absolute，win32 平台转 storage 斜杠）。
- **DB 投影侧**：空串行在过滤谓词中按**字面空串**参与（`directory=''` 只匹配空串本身）；allowlist 不含空串目录（入口校验拒绝）→ 空 directory 行在 allowlist 非空查询中天然被排除（属允许语义——legacy 行无目录归属）。
- **复刻必要性**（行 87 论证）：若 DB 侧把空串当「无目录」而非字面值，会与 HTTP 路径（上游 fromRow 同名语义）对同一行集**分叉** → 过滤 SQL 必须复刻「空串原样」语义。

### 9.5 测试用例表（17 行集/状态 case + 2 指纹 case ≈ 19 参数化，B3a-B2 落地；断言 = 行集精确匹配 + 指纹 hash 确定性）

| # | 组 | case | 断言 |
|---|---|---|---|
| 1-4 | search 转义矩阵 | ① 普通子串匹配；② 含 `%` 的用户输入 → 字面命中仅含 `%` 的行（不放大为通配）；③ 含 `_` 输入 → 字面；④ 含 `\` 输入 → 字面 | 行集精确匹配（oracle 同 §9.1 转义规则） |
| 5-7 | allowlist 子树谓词（substr 二进制前缀，rev-1） | ⑤ `/foo` 匹配 `/foo` 与 `/foo/sub`，**不匹配** `/foobar`；⑥ `/foo/bar` 子树收窄（`/foo/bar/sub` 命中、`/foo/baz` 不含）；⑦ allowlist 多项（`/a`+`/b`）并集 | 行集精确匹配 |
| 8-9 | complete 边界 | ⑧ 结果恰 `limit` 行 → complete:true；⑨ 结果 `limit+1` 行（有下一页）→ complete:false（LIMIT+1 判定） | complete 布尔精确 |
| 10-11 | legacy 空 directory 分叉 | ⑩ 空 directory 行：allowlist 空 → 出现在结果（无谓词）；⑪ allowlist 非空（不含空串）→ 空 directory 行被排除（`substr('',…)` 与 `= :d_raw` 均不命中，允许语义）——与 HTTP 路径对同一行集不产生第二套行为 | 行集与「既定语义」一致 |
| 12 | 键集下界 | ⑫ cursor 锚点后无更多行 → 返回 0 行 + complete:true（空窗口闭合） | 行集 + complete |
| 13-14 | search × DB 不可用（降级规则，rev-1 Fix 2） | ⑬ search 含 `%`（如 `100%`）× db 不可用 → **503 `auxiliary_unavailable`**（`has_wildcard` 判定）；⑭ search 纯字面 × db 不可用 Class A → **200 + `degraded:true`**（透传等价） | 状态码 + degraded 精确（oracle 同 §7.2 / §9.1） |
| 15-17 | allowlist 二进制前缀边界（rev-1 Fix 3） | ⑮ 大小写：allowlist `/foo` 不匹配 `/Foo/child`（`substr`=` BINARY`；对照旧 LIKE 会误放行——实测 `'Foo' LIKE 'foo'`=1 / `'Foo' = 'foo'`=0）；⑯ 根 `/` 全匹配：allowlist `{/}` → 命中所**有非空绝对路径**（`substr(x,1,1)='/'`）；⑰ 路径段含 `%`/`_` 字面：allowlist `/a` 子树命中 `/a/%20b/c` 与 `/a/dir_%_x`（`%`/`_` 字面不放大）；**同前缀异名不命中**：`/a%20b/c`（`/a` 的同层异名目录，同 `/foobar` 边界）**不在** `/a` 子树 | 行集精确匹配 |
| +2 | 指纹确定性 | 同 input 两次执行 cursor 指纹（search-hash / allowlist-rev）hash 相同；input 变化（search 或 allowlist）hash 变化；`has_wildcard` 判定与指纹规范化共用同一确定性函数 | hash 值相等/不等断言 |

---

## 10. 等价性锚定测试设计（B0-6g）

> **本节点由 B0-6g 泳道填写（2026-08-17）**。权威依据：refactor-plan §5.2（S-B03）/ §2.1(g)（行 141）/ §11；v2.2 行 148（护栏 5）、行 264g（B0 出口门槛 (g)）。交付物 = `tests/test_equivalence_anchor.py` 设计定稿（用例矩阵 + 权威源选型报告），落地于 B3a-B2。

### 10.1 权威源选型（S-B03 结算）

**选型结论：混合——①真实 opencode HTTP handler 进程为「版本升级际 + 发布 gate」的全量校验源；②版本标记 golden 响应为「日常 CI」的默认执行源。禁止以 sidecar mock 期望为唯一权威（双方都排除 sidecar mock：mocked HTTP 只测「符合自己的 mock」，检测不了上游漂移——refactor-plan §5.2）。**

| 权威源 | 定义 | 论证（采纳） | 代价与否决理由 |
|---|---|---|---|
| ① 固定对齐版本真实 opencode HTTP handler 进程 | 契约测试拉起 `opencode-src/current`（v1.18.18）真实 server 进程，走真实路由处理 `/experimental/session`（cursor 分页 + before/limit） | **检测真实上游 schema/payload 漂移**：上游版本升级后唯一能确认「DB 路径结果仍 ≡ HTTP 投影真值」的手段；对齐版本号（readlink 实测 v1.18.18）即侧车宣称的投影真值基线 | CI 重：进程拉起/端口/依赖/timing 不确定性；不适合每次 PR 全量跑 |
| ② 版本标记 golden 响应（自真实上游生成） | 用 ① 的方式离线生成一次 golden 文件（真实 server 跑固定数据集 → 响应快照），带**对齐版本号 + 生成指纹（sha256）**，测试读取 golden 与 DB 路径结果比对 | **离线可跑、CI 稳定、快速、确定性**；版本标记使漂移可追溯（golden 与真实行为仅存在版本滞后窗口，由 ① 按期闭合） | 版本滞后窗口：golden 早于真实行为 → 用 ① 定期全量校验闭合 |

**混合时序**：日常 CI（每 PR / 每 commit）跑 **② golden 对比**（离线、毫秒级）；上游升级、sidecar `current` repoint 或发布 gate 时滚动跑 **① 真实进程全量**，通过后重新生成 golden（覆盖滞后窗口，golden 升版本标记）。两者共用同一参数化矩阵与断言层（§10.4/§10.5），仅数据源不同（golden 文件 vs 实时进程响应）。

### 10.2 设计总览

```
固定数据集 session 样本（§10.3）
   ├─▶ 权威源 ① 真实 server 进程 ──▶ /experimental/session（cursor 翻页取全量）
   │                               └─▶ 在线断言：DB 结果 ≡ 进程响应
   ├─▶ 权威源 ② 生成程序 scripts/generate_golden_sessions.py（离线，复用 ① 的进程路径）
   │       └─▶ tests/golden/sessions-global-<version>.json（版本标记 + 指纹）
   │               └─▶ tests/test_equivalence_anchor.py（日常 CI）比对 DB 结果 ≡ golden
   └─▶ tests/ 内 DB 路径（真实 sidecar 只读连接 + 同数据集注入的 DB fixture）
断言层 ≡（行集 / 字段语义 / 排序 / complete 判定，§10.5）——两端共用
```

### 10.3 固定数据集（session 样本，B3a-B2 fixtures 落地）

覆盖边界所需的夹具行（写入测试用只读 DB fixture，与 golden 生成使用**同一数据集定义**）：

| 边界维度 | 样本设计 | 锚定断言覆盖 |
|---|---|---|
| 排序 tie-break | 构造多行**同 `time_updated`**，依赖 `id DESC` 定序 | (time_updated DESC, id DESC) 排序（行 88 冻结） |
| archived | 混入已归档/未归档行 | archived 过滤行集 |
| parent 层级 | 父/子 session 混合 | parent 谓词行集 |
| search 特殊字符 | title/directory 含 `%` / `_` / 反斜杠 / 空串 | `LIKE ? ESCAPE '\'` 语义 + hash 指纹（§9.1） |
| allowlist | 多 directory 子树 + 空 allowlist 对照 | allowlist 谓词 + cursor 指纹（§9.3） |
| directory 规范化 | `${owner}-${host}` 风格 + legacy 空 directory | 规范化复刻语义（§9.4） |
| 时间边界 | 极端时间戳（0 / 当前时间附近） | 字段语义逐字段等值 |

### 10.4 参数化矩阵（版本 × 4 维）

```
pytest.mark.parametrize 组合 = 版本 × {行集 / 字段语义 / 排序 / complete 判定}
版本维度 = [v1.18.18（当前对齐）] ∪ {未来 repoint 时追加}    # 对齐 opencode-src/current
```

| 维度 | 参数化取值 | 断言目标 |
|---|---|---|
| 行集 | ①全量（无 filter）②cursor 逐页翻取拼接 ≡ 一次全量 ③archived 过滤 ④parent 过滤 ⑤search 过滤 ⑥allowlist 子树 | 同一行集、同一行序（HTTP 投影） |
| 字段语义 | 全投影列集合（v2.2 行 146：id/parent_id/project_id/time_archived/time_updated/directory/title/agent/model/version/summary_*/tokens_*/time_*/revert/permission/metadata + project join 列 **id/name/worktree**（行 74 模板 directory 已实证不存在，R1 关闭））逐字段等值 | 每行每字段 DB 值 ≡ HTTP 投影值 |
| 排序 | `(time_updated DESC, id DESC)`；含同 time_updated tie 样本；升/降序对照 | 行序列全等（含 tie-break） |
| complete | 窗口内 N 行 / N+1 行（LIMIT+1 判定两侧） | complete=true/false 一致（行 81/137） |

### 10.5 断言层（DB 结果 ≡ 权威源结果）

断言恒等式（两端数据源共用：①/② 仅换「权威源结果」取值来源）：

```
assert_rows_equal(db_rows, authoritative_rows)     # 行集 + 字段语义：逐行逐字段等值
assert_order_equal(db_order, authoritative_order)  # 排序：(time_updated DESC, id DESC) 全序一致
assert_complete_equal(db_complete, authoritative_complete)  # LIMIT+1 窗口判定结果一致
```

- 权威源结果 = ①实时进程响应 / ②golden 文件 `sessions` 数组（两者格式一致：上游 SessionInfo 投影 JSON）。
- DB 侧 = sidecar 只读连接 + 同一数据集查询（同 §9.5 的查询组装）；DB fixture 每次测试重建，保证与 golden 数据集同源。
- **版本标记校验**：golden 文件头 `version` 字段 = 当前对齐版本（readlink 实测 v1.18.18）否则 skip/fail；`fingerprint`（sha256 of generators + dataset manifest）不匹配 → 测试失败并提示重新生成 golden（防「golden 过期仍绿」的静默滞后）。

### 10.6 golden 生成 / 更新程序

**« 程序：`scripts/generate_golden_sessions.py`（B3a-B2 落地，本次 B0 交付设计）»**

| 步骤 | 行为 | 产出 |
|---|---|---|
| 1 | 拉起 `opencode-src/current`（v1.18.18）真实 server 进程（临时端口 / 临时 DB） | 运行中真实上游 |
| 2 | 注入固定数据集（§10.3 同源 fixtures） | 确定状态 |
| 3 | 走真实路由 `/experimental/session`（cursor 逐页取全量，同 §10.4 行集维度） | 响应快照 |
| 4 | 序列化 `tests/golden/sessions-global-<version>.json`：`{version, generated_at, fingerprint(sha256), dataset_manifest, sessions[]}` | golden 文件 |
| 5 | （更新路径）上游 repoint / schema 变更后重跑，升 version 标记、重算指纹 | 新版本 golden |

- **更新触发**：①全量校验通过后 / 上游版本升级 / golden 校验失败（指纹或版本不匹配）。
- **CI 集成**：日常 CI 只读 golden（离线、不拉进程）；发布 gate 或 `current` repoint 跑 ① 全量 + 重生成 golden（手动/受控触发，避免 CI 抖动）。

### 10.7 用例矩阵（B0 交付，B3a-B2 落地 `tests/test_equivalence_anchor.py`）

| 用例 ID | 权威源 | 矩阵组合 | 前置 | 断言 |
|---|---|---|---|---|
| EQ-001 | ② golden | 全量表 × 全字段 × 全序 × N 行 window | golden 文件存在且指纹 OK | 行集/字段/排序/complete 全等 |
| EQ-002 | ② golden | cursor 翻页拼接 vs 一次全量 | 数据集 > 单页 | 拼接行集 ≡ 全量行集（同序） |
| EQ-003 | ② golden | archived/parent/search/allowlist 各过滤维度 | 各边界样本 | 过滤后行集 ≡ golden 对应集合 |
| EQ-004 | ② golden | 同 time_updated tie-break | tie 样本 | 排序列全等（id DESC 次序） |
| EQ-005 | ② golden | LIMIT+1 窗口两侧（N / N+1） | 数据集可控行数 | complete 结果一致 |
| EQ-006 | ② golden | 字段语义逐字段（含 summary/tokens/revert/permission 可选列空 vs 有值） | 覆盖可选列样本 | 每字段 DB ≡ 投影 |
| EQ-007 | ① 进程 | 全量表 × 全字段 × 全序 × complete（版本升级/gate 时运行） | 真实 server 可达 | 同上（在线，无 golden） |
| EQ-008 | ① 进程 | schema 漂移哨兵：新增上游字段/改名 | 构造上游变更 | 测试失败 = 禁用辅助信号（行 148 护栏语义） |

### 10.8 落地依赖与待裁决

| 项 | 说明 | 状态 |
|---|---|---|
| 依赖 | §9 查询组装（B3a-B1/B2）+ §6 schema 兼容门 + DB fixture 基建先落地；EQ 测试依赖它们 | 顺序依赖，非阻塞 B0 |
| golden 初版生成 | 需 ① 进程路径可跑（opencode-src/current v1.18.18 构建/启动脚本）——生产环境首次生成 golden 时验证 | 待 B3a-B2 环境就绪 |
| 版本滞后窗口 | golden 由 ① 更新闭合；窗口期内测试可能偏旧——接受并记录（§10.1 时序已定义） | 设计已覆盖 |
| 跨版本矩阵 | 仅当前对齐版本必跑；历史版本 golden 可选保留（若保留，版本维度扩展） | 观察项 |