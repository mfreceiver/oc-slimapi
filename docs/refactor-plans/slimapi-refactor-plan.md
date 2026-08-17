# oc-slimapi 侧改造方案：v4 全局门面工程执行计划（slimapi-refactor-plan）

> **性质**：本文档是 `docs/system-architecture-proposal-2026-08-17.md`（下称 **v2.2**）在 oc-slimapi 侧的**工程执行细化**。所有技术裁决**引用 v2.2 原文（行号）**，本方案不新增决策。发现 v2.2 内部矛盾或缺口 → 见 **§8 开放问题**，不擅自决定。
> **依据基线**：v2.2（310 行）+ `docs/slimapi-complexity-map-2026-08-17.md`（15 有状态组件基线）+ `docs/specs/v3-contract.md`（现契约）+ `AGENTS.md` + `docs/release.md` + src/ 代码事实。
> **写域纪律**：本文档为唯一可写文件；**禁止修改任何 src/、tests/、既有 docs/**（AGENTS.md / release.md / operations.md / v3-contract.md 仅作为本方案中"待修订"任务的目标被引用，修订动作在对应批次执行期按本方案 §2.1 B0-6f 落地）。
> **核准修订（2026-08-17，fixer-glm）**：① P1 minor 版本号 3.2.0 → **3.3.0**（3.2.0 已被「text 正文永远全量内联」发版占用：pyproject=3.2.0、commit `5f1d1b5`、CHANGELOG `[3.2.0]`；v2.2 行 243/275 的「3.2.0」为撰写时点的下一顺位 minor，现实顺延，非技术决策变更）；② **B2 与已发布 [3.2.0] 全量内联契约决策正面冲突**，升 owner 裁决、批次冻结（§8.1 问题 1）；③ §8 开放问题 10 项重组为 **4 项待裁决 + 7 项已就地解决**（§8.2 存档并写回对应批次）；④ 事实抽查 26 处（v2.2 行号 / src file:line / 契约条款），另修 tests/sse 路径、context 引文行号两处小误。
>
> **核准修订（2026-08-17，fixer-ds，R2 双评审收敛）**：⑤ **§8.1 问题 1 已裁决关闭**（owner v2.2 §3.3 定稿）：`TextPart.text` **永不截取、永远全量内联**（[3.2.0] 契约胜出）；400 码点 merged 截断**废止**；裁剪仅两模式（折叠内容 `omitted`+`expandRefs` 默认不加载 / 未展开缩略信息如 diffStats）。B2 冻结块**解冻**，任务改写为「merged 恢复/折叠范围对齐两模式裁剪的兼容验证」（§2.4），回归 P1 3.3.0；⑥ **v2.2 §5 组件账目 15→17（phase-aware：P1 16 → P3 18 → B6 后 17；sweep 调度器 + DB-aux 生命周期 + replay log − singleflight 合并）**——§8.1 问题 4 口径随 v2.2 修订闭合，B6-3 复核按 17 账目（§2.8）；⑦ R2 双评审（rev-sgpt 8.6 / oracle 9.1）8+2 Blocking 逐项修复：SSE 协议裁决门槛（S-B01，新增 §8.1 问题 5）、DB 连接所有权模型（S-B02，B0-5）、等价性权威源（S-B03，B0-6g/§5.2）、双版本 config 语义冻结（S-B04，B3a-A1）、allowlist 契约自洽（S-B05，B4-4/§5.3）、B1b sweep 两阶段（S-B06，§2.3）、写域单文件唯一 owner（S-B07，§3）、门槛测试入口粒度（S-B08，B0-6）、降级矩阵 allowlist 维度（ora B-2，§5.3）+ 非阻塞 n1-n7 + n2 一并落地；⑧ 事实勘误三处：`sse/singleflight.py` SingleFlight 类 `:121`（非 329 起）、`skeleton.py` SESSION_KEYS `~:700-703`、`tests/test_*.py` **77** 个（另含 conftest.py 等非测试文件，78 为 `tests/*.py` 总文件数）。

---

## 1. 目标与范围

### 1.1 目标（承接 v2.2 §2 全局门面，v2.2 行 36-40）

v2.2 行 38 定案：**不改上游，slimapi 升级全局门面**——全局读（零扇出）+ 定向写（directory 路由）+ 增量优先（cursor/seq/id:）。本方案把该门面在 sidecar 侧细化为 7 条工程线（全部可回溯 v2.2）：

| 工程线 | v2.2 出处 | sidecar 侧落地面 |
|---|---|---|
| 全局会话目录 v4（DB 投影源 + 降级矩阵） | §3.1（行 46-149） | selector 双版本 + routes/sessions v4 分叉 + 新 DB 辅助模块 + cursor 模块 |
| 增量事件：digest 定位 + SSE id:/重放 | §3.2（行 150-155） | DigestFields changed 字段（B1a）+ 有界重放日志（B3b） |
| q/p 事件驱动 + 低频 sweep | §3.2a（行 157-167） | 事件驱动确认 + resync 对账 + 30min jitter sweep + access-log 证明（B1b） |
| 消息面裁剪一致性 | §3.3（行 168-171） | **400 码点 merged 截断已废止**（[3.2.0] `TextPart.text` 永远全量内联胜出，§8.1 问题 1 已裁决关闭）；B2 剩余 = **merged 折叠/恢复范围对齐两模式裁剪的兼容验证**（折叠内容 omitted+expandRefs 默认不加载 / 缩略信息如 diffStats，两模式，§2.4） |
| 能力缺口补齐 | §3.4（行 173-180） | context / agent / model / revert 三段式路由（B4） |
| 目录白名单 fail-closed | §3.5（行 181-189） | `OC_SLIMAPI_DIRECTORY_ALLOWLIST` + /file 403 + 全局面过滤（B4） |
| 复杂度削减 | §5（行 205-217） | SingleFlight 合并 + sticky/三形状退役（B6） |

版本轨道（v2.2 行 240-249）：3.x minor（加性）→ 4.0.0 major（wire (3,4)）→ 5.0.0 major（wire (4,4)）。

### 1.2 不做什么（显式排除）

1. **不改上游 opencode**（v2.2 行 38, 40）；上游演进触发的能力回收见 v2.2 §9 观察项（行 297-300）。
2. **不写上游 SQLite**（v2.2 行 107, 144, 308）：sidecar 代码路径**零 DDL/DML/PRAGMA 写**；索引建立 = 运维动作（行 108），不在 sidecar 内。
3. **不实现 60s 定时兜底**（v2.2 行 159, 287 决策 3）：285× 流量放大实证否决。
4. **不做 PTY / OAuth / Project copy**（v2.2 行 32）；**不做 zstd**（行 200, 289 决策 5）。
5. **DB 辅助源范围冻结**：仅限 v4 sessions 投影（session 表 + project 表 LEFT JOIN 冻结列集），**不扩展到 message/part/事件溯源**（v2.2 行 145 护栏 2）。
6. **v4 不做 sidecar 行缓存**：一条 SQL 一次组装（v2.2 行 139）。
7. 本方案**不产出任何 src/ / tests/ 修改**——本文档交付后，B0 批次以设计文档形式启动实现（§2.1）。
8. **不做 merged 400 码点截断**（v2.2 行 168-171 该要素裁决已废止）：[3.2.0] `TextPart.text` 永远全量内联为已发布终态（v3-contract §4a），B2 仅做兼容验证（§2.4），不引入任何新截断/阈值。

---

## 2. 批次执行计划（对齐 v2.2 §8 批次表，行 260-276）

### 2.0 批次总览与发版路线图

```
P0  B0  规范先行 + 设计实证（纯设计/实证，零 src 改动，无发版）
P1  B1a + B1b + B2（兼容验证）+ B4  →  ./scripts/release.sh minor → 3.3.0（wire 仍 v3）
P2  B5a 消费者兼容版（ocdroid/webui 侧；本仓零产出，仅冻结接口 → §4）
P3  B3a（独立 rev gate）→ B3b（独立 rev gate）→  ./scripts/release.sh major → 4.0.0（wire (3,4)）
P4  v3 流量归零判据（行 248）→ B6 收尾 →  ./scripts/release.sh major → 5.0.0（wire (4,4)）
```

批间依赖（v2.2 行 264-273）：B0 → {B1a, B1b, B2, B4, B5a}；B0 + P2 → B3a；B3a → B3b；B5b → B6。

---

### 2.1 B0 规范先行（v2.2 行 264；风险：低——纯设计+实证）

v2.2 行 264 原文：*"规范先行 + 设计实证：v4-contract delta 全章（§7 清单）+ 双版本 selector 结构设计 + SSE 重放协议设计 + q/p 载荷核对 + DB 投影源设计评审 + release.md/AGENTS.md/operations.md 同步修订。硬出口门槛：(a)…(g)"*。

本批**不触碰 src/**（唯一例外：门槛 (a) 的 WAL 陈旧读测试，可在本批以"新增 CI 测试"形式先行落地——见 B0-6a）。全部输出 = 设计/契约/实证文档。

#### B0-1 v4-contract delta 全章写作（v2.2 行 254 的 11 章清单展开）

- **改动文件**：`docs/specs/v4-contract.md`（新文件，采用 v3 同款「继承基线与差异清单」结构，v3-contract §0 先例）
- **实现要点**（逐章，行 254 原清单展开为可写作任务）：
  - §0/1 版本原则与 v3/v4 并存退役规则（含 (3,4)→(4,4) 收窄即 major 的写入，行 248）
  - §2 selector 双版本状态表：`supported:[3,4]`、request-scope wireVersion、跨版本错误优先级
  - §3 versions/health 双视图 + `capabilities["4"]` 能力键（`globalSessions` / `sseReplay` / `qpImmediateFull` / `auxiliaryFilters`，**静态能力键**，不随 DB 抖动，行 140）+ health 瞬态字段（`auxiliary: {available, mode:"db"|"http"}`）
  - §4 sessions 参数矩阵：archived 三态 / parent 四态（省略 = all）/ **v3 参数 roots/start 在 v4 显式拒绝（422，不依赖 FastAPI 未知 query 默认忽略）**/ 排序（(time_updated DESC, id DESC) 冻结，上游 session.ts:571-572 复合排序事实）/ complete（LIMIT+1 同 snapshot）/ cursor 绑定（f 含 search-hash + allowlist-rev）/ 错误族（`invalid_cursor` + 503 `auxiliary_unavailable` **完整降级矩阵表（含 allowlist 维度，见 §5.3）** + Retry-After + `degraded` 响应 schema）+ **跨版本错误优先级真值表**（malformed cursor vs auxiliary unavailable / 指纹不匹配 vs 熔断 / directory_retired vs 参数错误 / repeated v vs 路由错误——组合全冻结，进 §5.4 测试矩阵）
  - §5 directory 消费矩阵：v3 继续消费；v4 sessions 拒绝（400 `directory_retired_in_v4`）；allowlist 作用域全覆盖（含 DB 谓词）
  - §6 ETag：v3 原样；v4 sessions 无 ETag；validator 版本隔离
  - §7 SSE id: 语法 / epoch / seq / 作用域 / 重放顺序 / gap 处理 + meta v4 扩展 + q/p 精确 JSON schema——**协议成文前提 = §8.1 问题 5 四项裁决门槛全部收敛（B0 出门 gate，S-B01）**；能力键广告时序（sseReplay/qpImmediateFull 随 B3b 实现同批启用，见 §4.1）
  - §8 错误族：403 vs 400 族优先级；503 Retry-After 规范
  - §9 观测：selectorResult=v4 / wireVersion 维度、replay hit/miss/gap、DB 辅助查询延迟/降级/熔断计数、v3 退役判据
  - §10 路由全集逐条（版本 / directory / allowlist / ETag / 错误 / 上游源）
  - §11 测试矩阵（跨版本 / 重启 / 过期 / 背压 / 冷启动 / DB schema 变更兼容 / 运行中迁移 / ro-vs-immutable WAL 测试 / EQP 全矩阵 / 降级矩阵逐格 / **等价性锚定——锚定权威源为固定对齐版本真实 opencode HTTP handler（契约测试拉起真实上游进程）或有版本来源证明的 golden 响应（自真实上游生成 + 版本标记），不得以 sidecar 自身 mock 为权威（S-B03）**）
- **验收标准**：11 章齐全；每条可回溯 v2.2 §3.1-3.5 行号；ocdroid/webui 开发者可仅凭本文件完成 B5a/B5b 对接开发（§4）
- **测试**：无代码测试；经 `scripts/check_routes_doc.py` 语义关键词检查（新 /slimapi 路由关键词须同步，见 B0-6f）

#### B0-2 双版本 selector 结构设计（v2.2 行 264 + 行 254 §2）

- **改动文件**：`docs/specs/design-v4-selector.md`（新）或并入 v4-contract §2 设计说明
- **实现要点**：
  - 现状锚点：`selector.py:110` `SUPPORTED_WIRE_VERSION = ACCEPTED_CLIENT_VERSIONS[1]`（=3）；`wire_view_from_scope()`（selector.py:229-238）恒返 3——设计为 scope 内存储 request-scope wireVersion
  - `_DIRECTORY_CONSUMING_PATTERNS`（selector.py:116-161）的**版本分叉**：v4 sessions 从消费集移除（行 66, 136）
  - 跨版本错误优先级：v4 专属 `directory_retired_in_v4` 在既有优先级链（v3 §8.3：405 versions → selector 400 → directory 400 → 404）中的定位；v3 收到 v4 参数（archived/parent/cursor）→ 422（行 136）
  - `_consume_v3_directory()`（selector.py:432-497）的版本化改造方案
  - 观测：selectorResult 枚举增 v4 维度（现状 selector.py:77-82 枚举：absent/v2/v3/rejected/exempt/not_applicable）
- **验收标准**：设计被 rev gate 通过；v4-contract §2 状态表可直接落地为 B3a-A 测试用例
- **测试**：无（设计先行；落地测试在 B3a-A）

#### B0-3 SSE 重放协议设计（v2.2 行 264 + 行 153）

- **改动文件**：`docs/specs/design-v4-sse-replay.md`（新）
- **实现要点**（行 153 全部要点展开）：进程 epoch + 单调 seq；ID 作用域（全局流 vs token 流**独立**）；帧 ID 分配（业务帧/digest/meta 有；heartbeat 无）；有界重放日志（count/bytes/TTL）；expired/future/gap 处理（gap → resync+snapshot）；与 meta-first / 背压 / 上游重连的顺序。**明确**：现 GlobalHub pending（250ms debounce）与 tombstone 队列**不是** replay log——v4 新建有界环形日志组件（行 153）；与既有 token 域重放队列（`TOKEN_REMOVED_MESSAGES` cap 1000 / TTL 24h，config.py:72-73）**并存不混用**。**设计必答题**：背压溢出帧是否入重放日志；断连后 gap 由重放日志补还是 resync 全量——属本协议设计要素（行 153 已列「与背压的顺序」），随设计文档 rev gate 把关。
- **⚠ 协议裁决门槛（S-B01，B0 出门 gate——§8.1 问题 5 四项，未收敛则 B0 不出门、`sseReplay:true` 不得进 v4 capability）**，本设计文档**必须先回答**下述四项才能成文 §7：
  1. **tokens=1 统一流**（`/events?tokens=1` 同连接复用全局 digest 帧 + token 帧）：二选一裁决——①单 Last-Event-ID 复合 cursor（全局+token 双序列编码，如 `g<epoch>-<seq>;t<epoch>-<seq>`）；②**v4 禁止复用**（tokens=1 请求在 v4 返回 400，token 流必须走独立 `/sessions/{sid}/stream`）。现状 `/events` 复用全局/token 帧 + 单 Last-Event-ID **无法**恢复双序列（S-B01 论证：meta-first 与重放顺序矛盾——重连新 meta 分配新 seq 后再发旧 replay 帧 = 线上 ID 倒退）；
  2. **meta 重连语义**：重连后 meta 帧是否带 `id:`、epoch 是否更换、线序定义（meta → replay 帧 → 新帧的严格顺序 = 无 ID 倒退）；
  3. **token ID 作用域**：全局序列 / per-sid / 每连接——全局序列会因其他 sid 消费 seq 出现合法空洞，**gap 不能当丢帧**（gap 判定须区分「消费者缺席 seq」vs「日志逐出」）；
  4. **两端点状态机逐帧序列表**：`/events` 与 `/sessions/{sid}/stream` 各自的 replay / snapshot / resync 帧序列（含 epoch 切换、背压溢出、subscriber 溢出重连场景）。
- **能力键时序（n1）**：`sseReplay` / `qpImmediateFull` capability 与实现**同批启用**——B3a 的 `capabilities["4"]` **不**含此二键；随 B3b 实现落地同期广告（§2.7 B3b-5、§4.1）。
- **验收标准**：协议含可落地的帧形/ID 语法 + 上述四项裁决记录（每项含选定方案与理由）；v4-contract §7 有对应章节；`sseReplay` 键语义冻结与 §4.1 时序表一致
- **测试**：协议矩阵用例表（重放/缺口/过期/重启/背压/**tokens=1 双序列**/ID 无倒退断言），落地于 B3b

#### B0-4 q/p 载荷核对（v2.2 行 164 "载荷直投评估前置（B1b 先行）"）

- **改动文件**：核对报告（进 `docs/specs/design-v4-qp-payload.md` 或 B0 实证记录）
- **实现要点**：逐字段核对 GlobalHub 转发的 q/p 帧 `properties` 是否已是上游完整对象（question.asked / permission.asked payload；上游锚点：opencode-src/current 的 event-v2-bridge / question.ts / permission.ts，按 AGENTS.md 约定读源码核对）。结论二选一：**已完整** → B1b 零 wire 变更、纯客户端改动（webui 直投）；**缺字段** → 缺口清单，移 B3b v4-only 补全（行 164）
- **验收标准**：核对结论定稿；`qpImmediateFull` 能力键语义随之冻结（时序自洽——原 §8 问题 7 已就地解决，见 §8.2：B0-4 属 B0 批先于 v4-contract §3 定稿；若缺字段，补全随 B3b 同入 4.0.0 同一 major（行 245-247），键发布时语义已终态、静态性（行 140）不破）
- **测试**：核对用例（帧样例 × 上游 schema 逐字段）

#### B0-5 DB 投影源设计评审（v2.2 行 264 + §3.1 全文）

- **改动文件**：`docs/specs/design-v4-dbaux.md`（新）
- **实现要点**（v2.2 §3.1 全部裁决的工程化）：
  - **连接生命周期**（行 90-101）：`mode=ro` 主路径（行 94）+ `PRAGMA query_only=ON` 防御层；每查询 `BEGIN…COMMIT` 短生命周期只读事务（行 95）；**`immutable=1` 完全弃用**（行 96，不作主路径不作降级档）；启动 ro 打开 + schema 门探测，失败 → 禁用辅助源全降级 HTTP（行 97）
  - ***connection ownership and concurrency（S-B02 独立小节，B0 设计必含）***：并发执行模型二选一且论证——**①每查询新建短连接**（连接即事务边界、无共享状态、天然隔离；代价：每次 open/close 开销 + fd 抖动）；**②单连接 + asyncio.Lock 串行化短事务 + 同步 sqlite3 查询经现有 TransformPool/独立 executor 线程池 offload**（不阻塞 event loop；代价：共享连接生命周期状态机复杂）。**推荐②**：sidecar 现为单事件循环架构（app.py lifespan 装配 + TransformPool offload 先例，app.py:292），每请求建连在 T3/高并发下有 fd 压力；单连接 + 锁与 `mode=ro` 单 fd 语义吻合——**且 R3 补强：单连接 = schema 门 / 熔断器 / inode generation 的**单一状态机**（swap/重探/熔断全部围绕同一连接对象是串行化前提，多连接会让各连接持有不一致的 generation/熔断状态）**。本小节必须冻结：连接 swap（inode 变化重开）的 generation 计数 + 锁语义（swap 期间活跃查询完成或失败后锁交接，禁止查询持锁跨 swap）；查询异常强制 `ROLLBACK`/`finally` 收尾（防止 `cannot start a transaction within a transaction`）；`PRAGMA busy_timeout` 值；**重探（schema 重探/inode 重开）与活跃查询的串行化边界**（重探须独占连接，等待锁队列清空）；同步 sqlite3 调用绝不直接跑在 event loop 线程（统一 offload）；**P99 熔断样本口径**：滑动窗口（如 60s）+ 最小样本数（如 ≥10 次查询才计 P99，冷启动前 M 次 warmup 豁免——与 oracle n3 联动）+ 熔断恢复探针 + hysteresis（恢复阈值低于熔断阈值，防抖）；***线程亲和性冻结（R3 修复：`check_same_thread` 默认 True 下，连接在 event-loop 线程创建、查询在 executor worker 执行 = 立即线程错误——方案 ② 下必须显式冻结，二选一）***：**方案 1（推荐）——专属 `ThreadPoolExecutor(max_workers=1)`：connection 的建立/查询/rollback/重开/关闭全部在该 worker 内执行，event loop 侧仅经 async 封装等待结果**（线程归属恒定 = 从不触碰 `check_same_thread` 默认行为，亦无需 `asyncio.Lock` 保护连接本身——worker 串行 + 单线程天然免锁；TransformPool 复用此池或独立小池，但 max_workers 必须固定 1，不可用共享多 worker 池跑 DB 查询）；**方案 2——`check_same_thread=False` + 所有连接访问经同一 async lock**（允许任何线程访问，锁防并发；代价：换线程语义需测试覆盖，风险更高，多线程/换代测试必做）。**B0-5 冻结选型（推荐方案 1）**，两方案的线程归属/锁/换代语义表格进设计文档；B3a-B1 并发阻断测试补**线程亲和用例**（R4 断言语义：worker 外线程直接访问连接/游标 → **断言被 `check_same_thread` 拒绝**——方案 1 下这是期望的安全性质；经专属 worker 封装的 async 调用 → 成功；换代后旧引用失效）
  - **DB 路径解析**（行 98）：`OC_SLIMAPI_OPENCODE_DB` 显式配置（生产推荐）+ 默认复刻上游解析（`OPENCODE_DB` env → InstallationChannel 分库：latest/beta/prod → `opencode.db`，否则 `opencode-<channel>.db`；`:memory:` → 禁用辅助）+ **启动 log 实际解析路径**
  - **inode/mtime 定期校验**（行 99）：备份恢复 / channel 切换换 DB 文件 → 持旧 fd 读已删 inode → 重开重探（挂熔断器周期）
  - **错误触发的 schema 重探**（行 100）：查询错误分类（`SQLITE_SCHEMA` / no such table|column / I/O / WAL-SHM 不可达）→ 熔断禁用 → 周期重探恢复
  - **索引策略**（行 102-110）：首期无索引直跑（真库 384 行温测 ~0.015ms 为基线，行 106）；`P99 < 20ms` 熔断护栏（行 106, 147）；sidecar 永不写 DB 含 DDL；索引 = 运维手册动作（`CREATE INDEX IF NOT EXISTS` + `PRAGMA index_xinfo` 定义校验，行 108）
  - **schema 兼容门全投影列版**（行 146）：session 表 + 全部投影列（id/parent_id/project_id/time_archived/time_updated/directory/title/agent/model/version/summary_*/tokens_*/time_*/revert/permission/metadata）+ project 表 join 列齐备，否则禁用辅助降级 HTTP
  - **降级矩阵逐格冻结**（行 111-124）：12 格行为表（见 §5.3）
  - **组装容忍语义**（行 84）：project join 缺行 → `project=null`；JSON 列解析失败 → 跳过 + warning log；键集内行缺失即跳过
- **验收标准**：设计评审通过；每格降级行为进 v4-contract §4；B3a-B 可直接按此实现
- **测试**：无（落地于 B3a-B；本批仅冻结设计）

#### B0-6 七项硬出口门槛执行（v2.2 行 264 (a)-(g)）

| # | 门槛（v2.2） | 执行方式 | 测试入口（S-B08：每门槛固定测试模块/参数化 case 数/命令/断言摘要，进 CI 或 B0 实证脚本） |
|---|---|---|---|
| (a) | ro-vs-immutable WAL 陈旧读测试进 CI（行 264a；实证行 92） | **草稿库复现进 CI**：临时 SQLite + WAL 模式构造——① `immutable=1` 不读 live `-wal`（count=1 vs 2）；② 表建于 WAL 时 `no such table`；③ ro 路径经 `-shm` 正常读 WAL 内容。守护"immutable 永不启用"决策（行 96） | **新 `tests/test_wal_staleness.py`**（进 CI，pytest 收集即执行）：参数化 3 case（immutable 脏读回退 / 表不可见 / ro 正常读）；命令 `pytest tests/test_wal_staleness.py -q`；断言 = ①②③ 逐 case 的状态码/结果集合差异（count 值、异常类型） |
| (b) | EQP 全过滤矩阵 + 真库 P99（行 264b；实证行 104-106） | 1,000 行草稿库全矩阵：archived 3 × parent 4 × cursor 2 × search 2 = 48 组合，落成可重复测试/脚本；真库 P99 < 20ms 实测数据进设计文档；结论 = 索引必要性（当前无索引直跑成立则 DDL 程序保持运维态） | **B0 实证脚本 `scripts/eqp_matrix.py` + 报告**（设计实证，结论进 design-v4-dbaux.md）：48 组合参数化；命令 `.venv/bin/python scripts/eqp_matrix.py --rows 1000 --out /tmp/eqp.json`；**断言 = planner 关键特征而非全文案**（SQLite 版本文案会漂——S-B08：仅断言 `EXPLAIN QUERY PLAN` 的扫表 vs 索引、`USE TEMP B-TREE FOR ORDER BY` 出现与否、输出行数）；真库 P99 为数据采集非断言（温测 ~0.015ms 复测，行 106 基线） |
| (c) | DB 路径解析设计（行 264c；裁决行 98） | env + channel 复刻规则成文档 + 单元测试用例表（`OC_SLIMAPI_OPENCODE_DB` / `OPENCODE_DB` / channel 分库 / `:memory:` → 禁用）；解析逻辑伪代码定稿 | **新 `tests/test_db_path_resolution.py`**（落地于 B3a-B1，B0 先固化用例表）：~10 参数化 case（显式 env 优先 / OPENCODE_DB 继承 / channel latest\|beta\|prod 分库名 / `:memory:` 禁用 / 路径不存在禁用 / 相对绝对路径规范化 / `~` 展开 / 尾斜杠 / 空白 / 双 env 冲突）＋**runtime 步骤**（冷启动：启动 log 断言实际解析路径；运行中 inode swap：替换 DB 文件后观察重开重探日志、期间查询不挂死——B3a-B1 阻断测试） |
| (d) | 降级矩阵逐格冻结（行 264d；矩阵行 113-121） | 每格写死：200+`degraded:true`（可等价表达组）/ 503 `auxiliary_unavailable`（不可表达组）/ `Retry-After` / 错误体不泄露 DB 细节（行 122）；`degraded` 语义冻结（行 123：只表数据源降级+强度弱化，过滤语义永不降级）；**allowlist 列为正式维度（ora B-2，见 §5.3：allowlist 非空 + DB 熔断 → 503 fail-closed，不做会出真子集的首N行后置过滤）** | **新 `tests/test_degradation_matrix.py`**（B3a-B4 落地，B0 先冻结 12 格生成规则）：参数化 = §5.3 表每行机械展开 × {DB 可用/禁用/熔断} 三态 × allowlist {空/非空} 两态（**R3 口径升级：约 12×3×2 ≈ 72 格**，§5.3 矩阵已两维——仅此三处此前掉队）；断言 = 状态码 / `degraded` 语义 / Retry-After 头 / 错误体**不包含** DB 路径/schema 字样（负向断言） |
| (e) | search/complete/allowlist SQL 语义冻结（行 264e；行 81, 86, 85, 87） | search = `LIKE ? ESCAPE '\'` + 规范化 hash 进 cursor 指纹（行 86）；complete = LIMIT+1 同 snapshot（行 81, 137）；allowlist 非空 → `s.directory IN allowlist 子树谓词` + cursor 指纹（行 78, 85）；legacy 空 directory 规范化复刻（path.ts:41-52，行 87）——四条逐条进设计文档 + 测试用例表 + **allowlist SQL 边界语义（S-B08）**：`%`/`_`/`\` 字面转义、`/foo` vs `/foobar` 前缀边界（子树 = 目录自身所有后代，不含 `foobar` 这样的同层异名前缀）、symlink/case 敏感性 | **新 `tests/test_sql_semantics.py`**（B3a-B2 落地，B0 先固化用例表）：~12 参数化 case（search 转义矩阵 4、allowlist 前缀边界 3、complete 边界 2、legacy 空 directory 分叉 2、键集下界 1）；断言 = 行集精确匹配/游标指纹 hash 值确定性（**同一输入两次执行 hash 相同 = 规范化算法确定性**） |
| (f) | AGENTS / release.md / operations.md 修订（行 264f；行 256 + 行 109） | ① AGENTS.md「不写 SQLite」措辞（行 109 原文："禁止写入/修改上游 opencode SQLite 业务数据；sidecar 代码路径零 DDL/DML/PRAGMA 写；索引建立属显式运维动作（含定义校验），不在 sidecar 内"）；② `docs/release.md:42-47` 陈旧 v2 / X-Slimapi-Version 描述修订（行 256）+ **P3 发布前置 checklist（n5：ocdroid B5a 已发 + webui B5a 已发 → 方可执行 sidecar major）**；③ `docs/operations.md`：DB 路径解析 / 索引运维程序 / 熔断排障 + **runbook（n6：升级 opencode 后第一步观察 `health` 的 `auxiliary.available`/`mode`——熔断 = 等价性失败的信号，对照 §6.2 禁用链排查）**；④ `scripts/check_routes_doc.py` 语义关键词检查同步新路由关键词 | 无新测试模块；命令 = `./scripts/check.sh` 仍绿 + **grep 负向断言**（`rg -n "X-Slimapi-Version" docs/release.md` 无命中 = 陈旧描述已除；`rg -n "4.0.0" docs/operations.md` 有 runbook 章节锚点）；检查清单进 check.sh 注释或 B0 实证记录 |
| (g) | 等价性锚定测试设计（行 264g；护栏 5 行 148） | 设计：sidecar 契约测试**逐版本**锚定 DB 投影 ≡ HTTP 投影（同一行集同字段语义）；上游演进时等价性测试失败 = 禁用辅助的信号（行 148）。**权威源修正（S-B03，自证循环防御）**：锚定源 = ①固定对齐版本的真实 opencode HTTP handler 进程（契约测试拉起 `opencode-src/current` 的 server 进程，走真实路由处理 `/experimental/session`——检测上游 schema/payload 真实漂移）；或 ②自真实上游生成、带版本标记（对齐版本号 + 生成指纹）的 golden 响应文件（离线可跑、CI 稳定；代价 = golden 与真实行为存在版本滞后窗口）。二选一进设计文档，**禁止**以 sidecar mock 期望为唯一权威（mocked HTTP 只测"符合自己的 mock"，检测不了上游漂移）；测试参数化 = 版本 × {行集 / 字段语义 / 排序 / complete 判定} | **`tests/test_equivalence_anchor.py` 设计定稿**（落地于 B3a-B2，B0 先交付用例矩阵与权威源选型报告）：断言 = DB 路径结果 ≡ 权威源结果（行集 + 字段语义 + `(time_updated DESC, id DESC)` 排序 + LIMIT+1 complete 判定） |

- **验收标准**（B0 批级，逐门槛）：(a) `tests/test_wal_staleness.py` 在 CI 全绿；(b) `scripts/eqp_matrix.py` 48 组合可重复执行 + 真库 P99 数据进设计文档；(c) 路径解析用例表 + 伪代码定稿（runtime 冷启动/swap 步骤进 B3a-B1 阻断测试）；(d) **12 格 × DB 三态 × allowlist 两态（≈72 格）生成规则冻结** + 逐格语义进 v4-contract §4（R3 口径：§5.3 矩阵两维对齐）；(e) 四条 SQL 语义 + allowlist 边界冻结进设计文档与用例表；(f) 三治理文件修订完成（check.sh 仍绿 + grep 负向断言过）；(g) 等价性测试设计定稿（权威源选型 + 用例矩阵）。**每批预计新增测试数入 §5.1**
- **测试**：门槛 (a) 为新增 CI 测试（3 case）；(b)(c)(d)(e) 为 B0 实证/用例表（落地测试模块于 B3a 对应任务，B0 只冻结设计）；B0 自身不触碰 src/

**B0 依赖**：无（v2.2 行 264）　**B0 发版**：无（纯设计+实证）

---

### 2.2 B1a digest 定位字段（v2.2 行 265；风险：低）

v2.2 行 152：*"digest 帧增强（v3 安全加性，B1a）：`session.digest` 增可忽略字段 `changed: [sid…]`——仅此一项新增……v3 契约 §7 正式修订（帧形加字段，旧客户端忽略）；消费端从整表重拉 → 定向精拉"*。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B1a-1 changed 字段实现 | `src/oc_slimapi/sse/hub_types.py`（DigestFields 区域，行 134-196）、`src/oc_slimapi/sse/global_hub.py`（publish/flush 路径） | DigestFields 增 `changed` 字段；flush 时聚合本窗口内发生变化的 sid 列表进帧载荷（**触发语义**见 §8.1 问题 2——核准补充事实：digest 帧为 per-sid 逐帧产出（sse/global_hub.py:392,419），changed 聚合口径待 owner 裁决；最小语义 = 与帧字段一致的 sid 集） | 帧形加字段且**仅此一项**（v2.2 行 152 "仅此一项新增"）；v3 旧客户端忽略（契约既有"消费方必须忽略未知字段"） | digest 帧 changed 出现/缺席断言；SSE 帧形回归（v3 §11.6 测试面扩展） |
| B1a-2 契约修订 | `docs/specs/v3-contract.md` §7 | 帧形加字段的正式修订 + 旧客户端兼容说明（v2.2 行 152） | §7 修订完成；`[冻结]` 标注 | — |
| B1a-3 CHANGELOG | `CHANGELOG.md`（3.3.0 节） | 行为描述：digest changed 字段（供 ocdroid 定向精拉） | 3.3.0 节条目齐（release.md §3.2 前置） | — |

**B1a 依赖**：B0　**B1a 发版**：P1 minor 3.3.0（v2.2 行 242, 275；3.2.0 已被 text 全量内联发版占用，顺延一位——见头注核准修订）

---

### 2.3 B1b q/p 事件驱动 + 30min sweep（v2.2 行 266；风险：中）

v2.2 行 266：*"q/p 事件驱动 + resync 对账 + 30min jitter sweep（载荷核对若「已完整」→ 零 wire 变更；否则 v4 部分移 B3b）；**exit criteria：sweep 流量 < 变更前基线（access-log 证明）**"*。

> 现状核实（代码事实）：questions/permissions 聚合路由为**请求驱动**的两阶段 fan-out（routes/questions.py:408-436、routes/permissions.py:428-458），**无任何定时轮询**——"60s 定时兜底"仅为 v2.1 拟议、从未实现（v2.2 行 159 废除的对象），故本批 = 新增 sweep + resync 对账，而非"删除 60s 定时"。
>
> **⚠ 数学事实（S-B06，v2.2 行 163/166 自相矛盾——rev-sgpt 复核）**：行 163 定「每目录 30min ± jitter 错峰」→ 30 目录 × 48 次/日 = **1,440 次/日满调度**，与行 166「预期 <50 次/日量级」**不可同时成立**；且基线测量不可闭环（生产 access log RETAIN_DAYS=3 + 共享 bucket 无 sweep marker + 同发布无预观测窗）。**修法（本批拆两阶段，与 oracle n1 收敛）见 B1b-4/5**；「<50 次/日」退出验收文本，exit criteria 改为对阶段口径可验收。
>
> **写域限制（S-B07）**：`sse/global_hub.py` 为 **B1a 独占 owner**（§3.1）；B1b 对其仅允许**纯注释级标注**或完全不动——q/p 直推（行 525-535）与 resync_all（行 815-829）的确认/说明属只读核实；任何行为编辑并入 B1a 串行链（§3.2 串行标 3）。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B1b-1 载荷核对结论落地 | （B0-4 输出决策） | 完整 → 本批零 wire 变更；缺字段 → q/p 补全移 B3b（v4-only） | 决策记录进 CHANGELOG/契约 | — |
| B1b-2 事件驱动确认 | `src/oc_slimapi/sse/global_hub.py`（q/p IMMEDIATE 直推路径，行 525-535，**只读核实**） | 确认 q/p asked 帧已事件驱动直推（零新增上游连接，行 155, 161）；核实冷目录不发事件的兜底路径——**确认结论记录为设计事实，不留代码改动** | 事件驱动常态零轮询成立（核实报告） | 帧直推回归（现状测试不动） |
| B1b-3 resync 驱动对账 | `src/oc_slimapi/sse/global_hub.py`（resync_all，行 815-829，**只读核实**）+ questions/permissions 路由 | 核实 SSE 重连 / 客户端 resync 请求 / digest 变更涉及 q/p 时的对账触发路径（按需非定时，行 162）；聚合路由语义保留（权威校准 + resync 后全量对账，行 165）；触发点调整（若需）走 B1a 串行链 | 对账触发点与既有 resync 生命周期一致 | 对账触发时序测试（B1a 链上） |
| B1b-4 **阶段 1：sweep 真 shadow/dry-run 观测与分类（S-B06 + R3 修复）** | 新 `src/oc_slimapi/qp_sweep.py`（先 skeleton：marker 写入 + **shadow 调度模拟**，不发起真实请求）+ `config.py`（sweep 参数段）+ `app.py`（lifespan 装配段） | **真 shadow/dry-run（R3：当前系统无 30min sweep，发真实请求本身就是行为变更——阶段 1 必须零真实 sweep 请求）**：沿用 30min ± jitter（每目录错峰，行 163）**纯模拟调度**——每次触点仅计算该目录是否处于 cold set（skip 谓词评估）+ **预计调度次数**，**把决策写 observability marker（bucket=`sweep` 或标记字段的 shadow 记录）而不发送任何 q/p HTTP 请求**；sweep 请求带专属 marker（阶段 2 才真正发出）；并发上限 `questions_semaphore`/`permissions_semaphore`（app.py:373,379）仅模拟计数；**预算护栏**（行 163 每 sweep 字节/耗时上限——阶段 1 以估算值记账）；**cold-set 识别算法定稿**：skip 谓词 =「该目录在近 T（如 30min）内有事件活动（GlobalHub 有 q/p 事件记录）或近 T 内有客户端 q/p 请求 → 跳过」；目录**进入** cold set = 连续 3×T 无活动；**退出** = 活动恢复（事件/请求）；全目录 round-robin 错峰，每次触发仅扫仍在 cold set 的子集；**全局日预算**（如 100 次/日）为**上限闸**——即使 cold set 满额，也按预算限额调度，超出排队次日 | 观察期（默认 ≥7 天）内仅采集 shadow marker 计数、**模拟**请求次数、命中/跳过统计；**零真实 sweep 请求发出（可经 access-log 反向断言：阶段 1 全期内无真实 `bucket=sweep` 请求）——这才是"变更前基线"的真正含义**；预算闸生效；**不构成验收（阶段 2 才算 exit criteria）** | sweep 调度 jitter 单测；cold-set 进出条件单测；预算护栏边界；**shadow 零逃逸断言（模拟调度不产生真实请求）**；sweep × 并发上限交互 |
| B1b-5 **阶段 2：sweep 启用 + 基线对比（S-B06 + R3 修复 + R4 基线公式修正）** | 阶段 1 输出 + 观测段（access_log/traffic 账目） | 阶段 1 观察窗口结束后：评估 cold-set 算法实际命中（预期 = 绝大多数目录被 skip，实际 sweep 请求数远小于 30×48 满调度）；**基线对比（R4 修正：基线 = 阶段 1 的实际事件驱动/客户端请求量（纯当前行为测度，151 次/日本底，行 159）——shadow 模拟 sweep 数**不加入基线 **（人为抬高基线会让真实总流量超过 151 次/日仍通过验收）；shadow 模拟计数仅用于阶段 2 预期流量预估与调参（单独列示）**；**exit criteria（对阶段口径可验收）**：阶段 2 稳态周（连续 7 日）真实 sweep 请求次数 + 其他 q/p 相关请求次数**合计 < 阶段 1 同期实际基线**，且 cold-set 长尾目录可达（行 163「仅覆盖冷目录长尾」语义兑现）；**不达标回退** = 调大 T / 调小日预算 / 增 skip 谓词（§6.1） | 稳态 7 日 access-log 证明（sweep marker 维度可区分）；日预算达标 | access-log 断言（marker/RETAIN_DAYS=3 滚动窗或 traffic snapshot 支撑多日窗口） |
| B1b-6 观测落地 | `src/oc_slimapi/access_log.py` / `traffic.py`（q/p 账目段） | sweep 请求 bucket=`sweep` 可区分（阶段 1 即落地）；观测维度 = {sweep 触发数, cold 命中, skip 计数, 字节, 延迟} | 观测字段落地（阶段 1 交付） | access-log 断言 |

**B1b 依赖**：B0（阶段 1 → 阶段 2 串行）　**B1b 发版**：阶段 1（sweep **shadow 骨架 + marker 观测 + cold-set 判定 + 零真实请求**）随 P1 minor 3.3.0（v2.2 行 275 并入 P1-P2；版本号顺延见头注核准修订）；阶段 2（sweep 启用 + 稳态 7 日 exit 验证）在 3.3.0 发布后的维护 minor 窗口内独立验收——**exit criteria 不达标则 §6.1 回退路径，不回滚已发布的 3.3.0**（阶段 1 行为 = shadow 观测/零真实请求，**无 wire/semantic 变化且无真实流量变化**——R3 修复：真 shadow 才使"3.3.0 无行为变化"成立）

---

### 2.4 B2 merged 折叠/恢复范围对齐两模式裁剪兼容验证（v2.2 行 267；风险：低）

> **⚠ 裁决已关闭（2026-08-17，owner v2.2 §3.3 定稿）**：v2.2 行 170 的「merged 截断 cap（400 码点）」**废止**。v3-contract §4a 自 **[3.2.0]**（owner 决策 2026-08-17，commit `5f1d1b5`，**已发版**：pyproject=3.2.0、CHANGELOG `[3.2.0]`）起 `TextPart.text` **永不截取、永远全量内联、不折叠、无阈值**（决策理由："对话正文是 chat 核心浏览内容，无论字节数一律呈现"），范围覆盖缺省 skeleton 与 `mode=merged`；2048 UTF-8 字节阈值自该版起**仅适用 `ReasoningPart.text`**。**裁剪仅两模式**：① 折叠内容 = `omitted` + `expandRefs` 默认不加载（sidecar 拥有键）；② 未展开缩略信息（如 `info.summary.diffs`）按 skeleton 缩减规则省略——**均无 text 截断**。
>
> 因此本批**冻结块解冻**，不再实现任何截断/阈值；B2 任务改写为**兼容验证**：确认 merged（及缺省 skeleton）正文全量内联现状与两模式裁剪规则一致、无遗留 400 码点/2048 字节/`truncated:true` 引用，并在契约补注记锁死（防回归到截断方案）。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B2-1 兼容验证（merged + 两模式一致性） | `src/oc_slimapi/routes/messages.py`（merged 路径，**只读验证**）+ `skeleton.py`（投影路径，只读验证） | 核验：① merged 路径 `TextPart.text` 全量内联（[3.2.0] 行为）；② `omitted`+`expandRefs` 折叠按现状一致（不额外加载）；③ `info.summary.diffs` 等缩略信息仅按两模式省略；④ **grep 负向断言（R3 作用域限定——不可全仓扫，本方案/CHANGELOG/v3-contract 含合法的历史废止说明）**：**代码层**：`rg -n "truncated|400" src/oc_slimapi/routes/messages.py src/oc_slimapi/skeleton.py src/oc_slimapi/schema.py` 断言无截断分支/400 码点常量/`truncated:true` 字面量；**schema 层**：断言 merged 响应与 v3/v4 契约 schema 中**无 `TextPart.truncated` 字段**（契约文件含 `truncated` 仅允许出现在废止说明注记，可加 grep 排除锚点或人工核对）；**允许** changelog/裁决注记/设计历史出现「400 码点已废止」说明（本方案 :519 等、v3-contract §4a 注记）——这些是防腐记录不是残留。**发现不一致 → 列为缺陷走正常修复（非本批范围变更），本批默认输出 = 验证通过报告** | 验证报告记录四核验点结论；代码/schema 层零截断残留（严格口径）；历史废止说明保留（宽松口径） | 新增**兼容断言测试**（merged 响应 `TextPart.text` 与缺省 skeleton 同源一致、无 `truncated` 字段泄漏；与 [3.2.0] 契约测试面接续，v3 §11.6 测试面扩展） |
| B2-2 契约注记 | `docs/specs/v3-contract.md` §4a | 在 §4a [3.2.0] 决策旁补注记：**v2.2 行 170 的 400 码点截断裁决已废止**（owner 2026-08-17 定稿），全量内联为该文现状，裁剪仅两模式且均无 text 截断；`truncated` 字段不进入 v3/v4 契约 | 注记落位 §4a；无 `truncated` 字段入契约 | — |
| B2-3 CHANGELOG | `CHANGELOG.md`（3.3.0 节） | 行为说明：merged 正文全量内联（[3.2.0] 已实现）经兼容验证无残留截断路径；400 码点方案明确废弃 | 条目齐 | — |

**B2 依赖**：B0　**B2 发版**：P1 minor 3.3.0（恢复正常进 P1；v2.2 行 267 该要素经 §8.1 问题 1 裁决修订为验证型任务）

---

### 2.5 B4 能力补齐路由 + allowlist fail-closed（v2.2 行 268；风险：低）

v2.2 行 268 + §3.4（行 173-180）+ §3.5（行 181-189）。**v=3 即可用（加性）**。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B4-1 context 路由 | `src/oc_slimapi/routes/`（新 context 路由）+ `INTERFACE_MAP.md` | `GET /slimapi/session/{sid}/context`（v2.2 行 178 表 + 行 172：v2 context，token 用量感知）；上游端点/payload 形状实现前核对（opencode-src/current，AGENTS.md 约定——原 §8 问题 4 已就地解决为实现前置条件，见 §8.2） | 路由可用、directory 消费集一致、INTERFACE_MAP 记录（check_routes_doc 绿） | happy/错误面（沿用 §10 两级制模式） |
| B4-2 agent/model 路由 | 同上（新路由） | `POST /slimapi/session/{sid}/agent` + `/model`（行 177 表：运行中切换） | 同上 | 同上 + 切换后状态断言 |
| B4-3 revert 三段式 | 同上（新路由） | `POST /slimapi/session/{sid}/revert/stage\|clear\|commit`（行 179 表：预览-确认回滚）；与既有单步 `POST /slimapi/session/{id}/revert`（v3 §10.b #8）为**加性并存**——单步保持不动；上游三段式端点/payload 形状实现前核对（原 §8 问题 3 已就地解决，见 §8.2） | 同上 | 三段式时序 + 与单步 revert 并存回归 |
| B4-4 allowlist fail-closed | `config.py`（allowlist 段）+ 路由层（read_groups.py / sessions / SSE 帧过滤点）+ `app.py`（启动 warning）+ `routes/health.py`（allowlist 字段） | **env 语义三态冻结（S-B05，消解"空→403"×"空配置零行为变化"矛盾）**：`OC_SLIMAPI_DIRECTORY_ALLOWLIST` **未配置（env 未设）= 机制未启用 → 全部端点现状行为不变、零 breaking（行 185 语义）；显式空列表（env 设为空串）= 机制启用、列表为空 → `/slimapi/file/**` 403 `directory_not_allowed` + 启动 warning（行 184）、其余端点不限制；非空列表 = 白名单目录子树过滤**（resolve 后前缀匹配，防 `..`/symlink/大小写绕过）——三分法论证：未配置与显式空在 env 解析上可辨（None vs 空串），P1 零 breaking 承诺仅约束未配置路径（现状部署无该 env = 零变化），显式空是操作者主动启用 fail-closed 的显式声明；全局面过滤语义（sessions/directories 列表、digest/q/p 帧、事件流，行 186）；**SSE/事件流未知目录 fail-closed 规则冻结**：事件帧负载含 directory 且不在白名单（或仅有 sid 无法判定目录/缓存缺失）→ **丢弃 + 计数度量（`allowlist_dropped_events`），不放行不泄漏**；403 不泄露目录存在性（统一错误体，行 188）；**health 响应补 `allowlist: {enabled: bool}`**（= 机制已启用，未配置=false；B3a-A3 双视图保持含此字段，对比 auxiliary 双视图） | fail-closed 矩阵全绿；**未配置 = 零行为变化（P1 兼容性回归面）**；显式空/非空 = 矩阵语义正确 | allowlist 矩阵：未配置/显式空/非空/子树/`..`/symlink/大小写/403 错误体不泄露/health `allowlist.enabled` 字段/SSe 丢弃+计数 |
| B4-4b 联合发布门槛（S-B05） | —（发布流程项） | 若发布时 operators **显式启用了 allowlist**（非未配置），ocdroid 侧必须已同步适配（`/file` 403 语义、必配 allowlist 或免除 file 依赖）——列为 P1 发布 checklist 项（release.md 修订见 B0-6f）；未启用则无此门槛 | P1 发布 checklist 含该条件项 | — |
| B4-5 INTERFACE_MAP 同步 | `docs/specs/INTERFACE_MAP.md` + `scripts/check_routes_doc.py` | 新路由逐条记录（防漂移，AGENTS.md 铁律） | check.sh 绿 | — |
| B4-6 CHANGELOG | `CHANGELOG.md`（3.3.0 节） | 新路由 + allowlist 行为 | 条目齐 | — |

**B4 依赖**：B0　**B4 发版**：P1 minor 3.3.0（版本号顺延见头注核准修订）

---

### 2.6 B3a selector 双版本 + v4 sessions DB 投影源（v2.2 行 269；风险：**高**——selector+DB 双重）

v2.2 行 269：*"wire v4 第一刀：selector 双版本结构性改造 + v4 sessions DB 投影源（mode=ro/短事务/schema 门/降级矩阵/熔断/inode 校验）——独立 rev gate"*。**依赖：B0 + P2（B5a 就绪）**（行 269）。

> **执行纪律**：本批粒度最细，分 **阶段 A（selector）→ 阶段 B（DB 投影）** 串行执行（routes/sessions.py 同文件，写域冲突见 §3）。两个阶段各自独立 commit，整批一次独立 rev gate。**前提**：v4-contract（B0-1）已定稿、B5a 消费者兼容版已发布（P2）。

#### 阶段 A：selector 双版本结构性改造

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B3a-A1 版本门 + config 迁移 | `src/oc_slimapi/versioning.py` + `src/oc_slimapi/config.py`（版本段） | `ACCEPTED_CLIENT_VERSIONS: (3,3) → (3,4)`（行 245）；`SERVER_API_VERSION` 单一常量 → **按请求视图取值**：v3 视图=3、v4 视图=4、同源同值、禁止错配组合（原 §8 问题 2 已就地解决，见 §8.2——v3-contract §3a 冻结先例：v2/v3 期 v2 视图=2 / v3 视图=3、`schema.version` 与 `server.api_version` 同源同值（health.py:37 现状）+ v2.2 行 254 §3「双视图」）；**config 迁移（S-B04，语义冻结）**：`Settings.server_api_version` 现为 env `OC_SLIMAPI_SERVER_API_VERSION` 驱动（config.py:272-278）+ 校验（config.py:562-588）——**双版本期删除该 env 对视图的影响**：`server_api_version` 改为恒等于 `SERVER_API_VERSION` 常量（=4）并废弃 env 读取（设置时启动 warning 忽略），理由：单值 config 无法表达双视图，视图由请求 wireVersion 决定（本任务 A1），env 只会在 (3,4) 期制造 3/4 错配风险；**`/versions.current` 冻结 = 4（(3,4) 期恒为最新主版本，与 `available:[3,4]` 并存是过渡期标准形态）**；`config.validate` fail-closed 仍钉死（env 不可放宽/收窄） | 版本门启动校验 + config 迁移向后兼容（旧 env 设置不破坏启动） | 版本门启动校验；config 迁移测试（旧 env 警告路径） |
| B3a-A2 selector 双版本 | `src/oc_slimapi/selector.py` | `supported:[3,4]`；request-scope wireVersion 入 scope state；selectorResult 枚举增 v4 维度；**v4 sessions 从 `_DIRECTORY_CONSUMING_PATTERNS` 移除** → v4 sessions 收到 directory → 400 `directory_retired_in_v4`（行 66, 136）；跨版本错误优先级整合（B0-2 设计落地）——**§4 真值表全部组合可枚举实现（S-B04：malformed cursor vs auxiliary unavailable / 指纹不匹配 vs 熔断 / directory_retired vs 参数错误 / repeated v vs 路由错误，优先级链写入契约 §8 与测试矩阵）**；v3 收到 archived/parent/cursor → 422（行 136） | v3 全回归逐字节不变；v=4 双版本语义正确；真值表组合全断言 | selector 双版本全矩阵（v3/v4/无 v/非法/多值/versions 豁免/跨版本错误优先级真值表）；既有 selector 测试全绿 |
| B3a-A3 versions/health 双视图 | `src/oc_slimapi/routes/versions.py`、`src/oc_slimapi/routes/health.py` | `/slimapi/versions`: `available:[3,4]`、`current: 4`（S-B04 冻结）；**`capabilities["4"] = {globalSessions, auxiliaryFilters}`（静态能力键，行 140, 254）——`sseReplay`/`qpImmediateFull` 不在本批广告（n1：能力键与实现同批启用，随 B3b 落地，§2.7 B3b-5/§4.1）**；health 双视图（按请求 wireVersion 返回 3/4，行 254 §3）+ 瞬态 `auxiliary: {available, mode:"db"\|"http"}`（行 140）+ `allowlist: {enabled: bool}`（B4-4 字段，双视图保持） | 双视图正确；capabilities 恒定不随 DB 抖动；sseReplay/qpImmediateFull 未出现 | versions/health 双视图矩阵（含 allowlist 字段、sseReplay 缺席断言） |
| B3a-A4 观测扩展 | `src/oc_slimapi/traffic.py`、`access_log.py`、`routes/metrics.py` | access log `selectorResult=v4` / `wireVersion="4"`（行 254 §9）；DB 辅助查询延迟/降级/熔断计数（行 254 §9, 140） | 观测字段含 v4 维度与 DB 辅助指标 | 观测断言 |

#### 阶段 B：v4 sessions DB 投影源

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B3a-B1 连接生命周期模块 | 新 `src/oc_slimapi/dbaux/`（或 `db_auxiliary.py`）+ `config.py`（DB 段）+ `app.py`（lifespan 装配段） | **mode=ro 主路径**（行 94）+ `query_only=ON` 防御；**短生命周期只读事务**（每查询 BEGIN…COMMIT snapshot，行 95）；**immutable 完全弃用**（行 96）；启动 ro 打开 + **schema 门**（全投影列版，行 146）；**错误分类重探**（SQLITE_SCHEMA / no such table\|column / I/O / WAL-SHM 不可达 → 熔断禁用 → 周期重探恢复，行 100）；**inode/mtime 定期校验**（行 99）；**DB 路径解析**（`OC_SLIMAPI_OPENCODE_DB` / `OPENCODE_DB` / channel 分库 / `:memory:` 禁用 / 启动 log，行 98）；**P99 < 20ms 熔断护栏**（行 106, 147）；**connection ownership and concurrency 落地（S-B02，B0-5 设计实现；含 R3 线程亲和冻结——选型方案 1 专属 worker 或方案 2 `check_same_thread=False`+async lock，B0-5 定稿）**：选定执行模型（推荐 单连接 + 专属 `ThreadPoolExecutor(max_workers=1)` 全操作、或 asyncio.Lock 串行短事务 + sqlite3 offload）、swap generation/锁语义、查询异常强制 ROLLBACK/finally、busy_timeout、重探与活跃查询串行化边界、同步调用不阻塞 event loop、P99 滑动窗口+最小样本+冷启动 warmup 豁免+hysteresis | 全部连接裁决落地；探针失败 → 禁用辅助全降级 HTTP（行 97）；不试 immutable | schema 门（缺列/缺表/缺 join 列）；熔断（P99 超限/错误分类/最小样本/恢复探针/hysteresis）；inode 变化重探；路径解析用例表；**并发模型阻断测试**（并发查询 × 事务重叠 → 无 `cannot start a transaction within a transaction`；swap 期间查询不挂死；**线程亲和用例（R3 修复 + R4 断言方向修正）：① worker 外线程直接访问连接/游标 → 断言被 sqlite3 `check_same_thread` 拒绝抛错 / 被封 layers 拦截（方案 1 下这是**期望行为**——证明线程归属恒定、安全性质成立）；② 经专属 worker 封装的 async 调用 → 成功（断言唯一合法通道可用）；③ 换代后旧连接引用失效断言保留**）；**ro-vs-immutable WAL CI 测试**（B0-6a 落地） |
| B3a-B2 投影 SQL 组装 | 新 `dbaux` 内查询模块（或并入 B1）+ `src/oc_slimapi/skeleton.py`（v4 投影函数） | 一条 SQL（行 70-82）：session LEFT JOIN project；archived 三态谓词；parent 四态谓词（省略 = all，行 65）；search `LIKE ? ESCAPE '\'`（行 86）；allowlist 子树谓词（行 78）；keyset `(time_updated, id) < (:t, :i)` 下推（行 88）；`LIMIT :limit + 1` → complete（行 81, 137）；排序 `(time_updated DESC, id DESC)` 冻结；组装容忍（行 84）；legacy 空 directory 规范化复刻（path.ts:41-52，行 87）；**SessionSkeletonV4 投影**（`project` 对象 + v4-only 字段，行 134；现 SESSION_KEYS 已含 directory——新投影为 project 而设） | SQL 语义与 B0-6e 冻结一致；投影字段契约冻结 | EQP 全矩阵（B0-6b 落地）；组装容忍用例；legacy 空 directory 分叉测试；**等价性锚定**（DB ≡ HTTP，B0-6g 落地，行 148） |
| B3a-B3 cursor 模块 | 新 `dbaux` 内 cursor 模块 | base64url(JSON `{t, i, f:{archived, parent, search-hash, allowlist-rev}}`)（行 127）；指纹不匹配 → 400 `invalid_cursor`（行 85, 129）；承诺 = 确定性排序、不承诺并发零重复零遗漏（行 128） | cursor 编解码/指纹校验全绿 | cursor 指纹矩阵（参数变更 → 400）；边界/畸形 |
| B3a-B4 routes/sessions v4 分叉 | `src/oc_slimapi/routes/sessions.py` + `envelope.py` | v=4 走 DB 投影源；v=3 现状**逐字节不变**；**v3 参数显式拒绝（S-B04）**：v4 路由收到 `roots`/`start` → **422**（FastAPI 未知 query 默认忽略——**须显式声明并断言，不依赖默认行为**；与 v3 收 v4 参数 → 422（行 136）对称，同归"参数版本不匹配"族）；**降级矩阵执行**（行 113-121 + ora B-2 allowlist 维度）：DB 可用 → 全过滤入 SQL 谓词（含 allowlist）；**DB 熔断/禁用 + allowlist 非空 → 一律 503 `auxiliary_unavailable`（fail-closed，裁决选②——不做首N行后置过滤，避免真子集风险（上游 listGlobal 单窗 LIMIT 首 N 行可能全不在白名单 → 空/缺行）；allowlist 为空 → HTTP 降级路径 + `degraded:true`**；`archived=omit\|all` + `parent∈{all,none}` + 无 cursor + search 任意 → 200 + `degraded:true`（parent=none→roots=true；parent=all→不过滤；search 原生透传）；`archived=only` → 503 `auxiliary_unavailable`；`parent=only/<sid>` → 503；带 cursor → 503；503 附 Retry-After、错误体不泄露 DB 细节（行 122）；`degraded?:true` 进正式成功响应 schema（行 64）；**v4 sessions 无 ETag**（行 254 §6）；limit 500 为 v4 域（行 137）；**`/slimapi/directories` 不在本批**：保持现形态（/experimental/session 发现）仅叠加 allowlist 过滤（行 183, 186），不扩 DB 投影（行 145 范围冻结——原 §8 问题 8 已就地解决，见 §8.2） | 降级矩阵逐格正确（§5.3，含 allowlist 维度）；v3 路径零回归；`directory_retired_in_v4` 于 selector 层先拦截；roots/start 422 断言 | 降级矩阵逐格测试（**12 格 × DB 三态 × allowlist 两态 ≈ 72 格**，R3 口径对齐）；v3/v4 同路由分叉回归；roots/start 显式 422 |
| B3a-B5 观测落地 | `src/oc_slimapi/routes/health.py`（auxiliary 字段）、`routes/metrics.py`、`access_log.py` | health 瞬态 `auxiliary {available, mode}`（行 140）；metrics 降级计数/查询延迟/熔断计数（行 254 §9） | 观测字段真实反映 DB 辅助状态 | 观测断言（DB 可用/禁用/熔断三态） |
| B3a-B6 契约同步 | `docs/specs/v4-contract.md`、`INTERFACE_MAP.md`、`CHANGELOG.md`（4.0.0 节） | 实现与契约一致（AGENTS.md 铁律：实现与契约冲突 → 先改实现或走正式修订） | v4-contract 与实现逐条对账 | check.sh（含 check_routes_doc） |

**B3a 依赖**：B0 + P2（B5a 就绪）　**B3a 发版**：P3 major 4.0.0（独立 rev gate；与 B3b 同 major 分批落地，行 245-247）

---

### 2.7 B3b SSE id:/重放日志 + q/p 帧补全（v2.2 行 270；风险：**高**——重放协议）

v2.2 行 270：*"wire v4 第二刀：SSE id:/重放日志 + q/p 帧补全（若需）——独立 rev gate"*。**依赖：B3a**。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B3b-1 有界环形重放日志 | 新 `src/oc_slimapi/sse/replay_log.py` + `config.py`（上限参数段）+ `app.py`（装配段） | v4 新有状态组件（行 213 "+1"）：count/bytes/TTL 上限；进程 epoch + 单调 seq；全局流与 token 流**独立 ID 域**（行 153）；帧 ID 分配（业务帧/digest/meta 有；heartbeat 无）；与既有 tombstone 队列并存不混用（B0-3 设计落地） | 日志上限（count/bytes/TTL）生效；ID 单调 | 上限边界；epoch 重启；独立域隔离 |
| B3b-2 SSE id:/重放 | `src/oc_slimapi/routes/events.py`、`routes/token_stream.py`、`sse/global_hub.py`（产出路径） | **前提**：§8.1 问题 5 四项协议裁决已收敛（S-B01，B0 出门 gate）；v4 端点发 `id:`；`Last-Event-ID` 触发重放；expired/future/gap 处理（gap → resync+snapshot）；与 meta-first（meta 仍首帧）/**背压**/上游重连的顺序（行 153）；**tokens=1 复用流按 §8.1 问题 5 裁决执行（复合 cursor 或 v4 禁止复用）**；**v3 SSE 帧名帧形零变化**（行 153 冻结） | 重放协议全矩阵绿；ID 无倒退断言；v3 SSE 零回归 | 重放矩阵：正常/缺口/过期/未来/背压/重连；gap → resync 断言；tokens=1 双序列；ID 单调无倒退（S-B01 状态机逐帧序列表） |
| B3b-3 q/p 帧补全 | `src/oc_slimapi/sse/global_hub.py`（q/p 直推路径）+ `v4-contract.md` §7 | **仅当 B0-4 核对结论 = 缺字段**（行 164, 270 "若需"）：question.asked / permission.asked 帧补全 properties 为完整对象（v4-only） | 补全后 `qpImmediateFull` 语义成立 | 帧补全用例（对照上游 schema） |
| B3b-4 meta v4 扩展 | `src/oc_slimapi/routes/events.py`、`routes/token_stream.py` | `slimapi.meta` v4 additive 扩展：capabilities 摘要 + epoch/seq 基线字段（行 154）；v3 形状不动 | v3 meta 帧零变化；v4 扩展字段出现 | meta 双版本断言 |
| B3b-5 契约/观测/能力广告同步 | `v4-contract.md` §7/§9、`INTERFACE_MAP.md`、`CHANGELOG.md`（4.0.0 节）、`routes/versions.py` | replay hit/miss/gap 观测（行 254 §9）；**能力键广告（n1）**：本批实现落地时 `capabilities["4"]` 追加 `sseReplay`（+`qpImmediateFull`：若 B0-4 核对=已完整则现状已成立，随本批与 sseReplay 同批广告；若需补全则由 B3b-3 完成后再广告）——**与实现同批启用，绝不提前广告** | 契约与实现一致；能力键仅在实现落地后出现 | check.sh + versions 双视图能力键断言（B3a 时期缺席 → B3b 时期出现） |

**B3b 依赖**：B3a　**B3b 发版**：P3 major 4.0.0（独立 rev gate）

---

### 2.8 B6 singleflight 合并 + sticky/三形状退役（v2.2 行 273；风险：低）

v2.2 行 273：*"v4 稳定后清理：singleflight 合并 + sticky/三形状退役（生产流量证明前提）"*。

> 前提（v2.2 行 273, 211）：生产流量证明（对齐 P4 判据——v3 流量归零，行 248）+ B5b（消费者 v4 适配完成）。依赖 B5b（行 273）。

| 任务 | 改动文件/模块 | 实现要点 | 验收标准 | 测试 |
|---|---|---|---|---|
| B6-1 SingleFlight 合并 | `src/oc_slimapi/leased_singleflight.py`（LeasedSingleFlight，行 160 起）+ `src/oc_slimapi/sse/singleflight.py`（SingleFlight，**行 121 起**——n7 勘误）→ 单一实现 | 双实现（~770 行，复杂度 map ④）合并为一个；保留各自语义（lease/预算/去重/超时）；**纯内部重构、wire 无关**（v2.2 行 210 "~300 行净减，保留主张"） | 行为等价（去重/预算/超时语义）；回归全绿；wire 零变化 | 合并后全量回归；两调用方（/full、列表路由）行为等价断言 |
| B6-2 404-sticky 退役 | `src/oc_slimapi/sse/global_hub.py`（sticky_last_error 区域） | v4 capabilities 探测替代后（行 211 "sidecar+ocdroid 双减"）；sidecar 侧简化 sticky_last_error 相关状态（ocdroid 侧三形状解析随 v4 退役，行 212, 226） | 退役不破坏 v4 行为（capabilities 探测路径） | 退役后 sticky 相关测试更新 |
| B6-3 账目/文档同步 | `CHANGELOG.md`（4.x / 5.0.0 节）、复杂度账目 | **净账目按 v2.2 §5 修订后 17（phase-aware）复核（ora B-1）**：P1 16（现 15 + sweep 调度器）→ P3 18（+DB-aux 生命周期 + replay log）→ **B6 后 17（singleflight 合并 −1）**；B6-3 复核该各阶段数字与 §8.1 问题 4 关闭口径一致（sweep 调度器与 DB-aux 生命周期独立计账已由 v2.2 §5 裁决） | 文档与实现一致；账目阶段数字可回溯 v2.2 §5 | check.sh |

**B6 依赖**：B5b + 生产流量证明　**B6 发版**：singleflight 合并（wire 无关）可随 4.x 维护 minor；sticky/三形状退役随 P4 major 5.0.0 一并落地（行 248）

---

### 2.9 release.sh 调用点汇总

| 发版 | 命令 | 包版本 | wire | 批次 | 契约/CHANGELOG 前置（release.md §3.2） |
|---|---|---|---|---|---|
| P1 | `./scripts/release.sh minor` | 3.3.0 | v3 不变（加性） | B1a + B1b（阶段 1）+ B2（兼容验证）+ B4 | v3-contract §4a 注记/§7 修订 + CHANGELOG `## [3.3.0]` 节 + 发布 checklist（allowlist 启用时 ocdroid 已适配，B4-4b） |
| P3 | `./scripts/release.sh major` | 4.0.0 | **(3,4)**（versioning.py bump） | B3a → B3b（各自独立 rev gate；能力键 sseReplay/qpImmediateFull 随 B3b 广告，n1） | v4-contract 定稿 + CHANGELOG `## [4.0.0]` 节 + versioning.py 同步（release.md §1.2 铁律）+ **P3 发布前置 checklist（n5，进 release.md 修订）：ocdroid B5a 已发 + webui B5a 已发 → 方可 major** |
| P4 | `./scripts/release.sh major` | 5.0.0 | **(4,4)**（收窄 = major，行 248） | B6 + v3 删除 | v4-contract §0 退役规则（(3,4)→(4,4) 写入，行 248）+ CHANGELOG |

铁律对照：**major 与 wire 协议版本绑定**（v2.2 行 238；release.md §1.1）；发版前 check.sh 绿 + 目标版本节已存在（release.md §3.2/§4）；B3a 不得早于 P2（B5a 就绪）发布（行 269）。

---

## 3. 写域矩阵（omni-orch 并行开发用）

> 用途：三项目并行开发体系下，sidecar 侧多泳道并发时的**文件归属与排他**依据。原则：**写域不重叠 → 可并行；同文件/依赖链 → 必须串行**。

### 3.1 域 → 文件归属表

| 写域 | 文件/模块（归属独占） | 涉及批次 |
|---|---|---|
| **selector/versioning 域** | `src/oc_slimapi/selector.py`、`src/oc_slimapi/versioning.py`、`routes/versions.py`、`routes/health.py`（selector/views 段）、`config.py`（版本段）、`app.py`（middleware 装配段）、`tests/test_selector*.py`、`tests/test_terminal_matrix.py`、`tests/test_health*.py`、`tests/test_versions_route.py`、`tests/test_v3_directory.py` | B0-2（设计）、B3a-A |
| **routes/sessions + DB 投影域** | `src/oc_slimapi/routes/sessions.py`、`skeleton.py`（SESSION_KEYS / v4 投影段）、`envelope.py`、新 `dbaux/`（连接/查询/cursor）、`config.py`（DB 路径/allowlist 段）、`app.py`（lifespan DB 装配段）、`tests/test_sessions*.py`、`tests/test_skeleton*.py`、新 `tests/test_dbaux*.py` | B0-5/6（设计）、B3a-B、B4（sessions 交互） |
| **SSE/replay 域** | `src/oc_slimapi/sse/global_hub.py`、`sse/hub.py`、`sse/registry.py`、`sse/hub_types.py`、`sse/token_hub.py`、`sse/tokenstream/*`、`routes/events.py`、`routes/token_stream.py`、新 `sse/replay_log.py`、SSE 相关测试（**核准勘误：仓库无 tests/sse/ 目录**，SSE 测试平铺于 tests/：`test_hub.py`、`test_hub_behavior_lock.py`、`test_token_hub*.py`、`test_events_tokens.py`、`test_sse_logging.py`、`test_token_subscriber_overflow.py`、`test_token_stream_route.py`、`test_v3_sse_meta.py`、`test_proxy_sse_observability.py` 等） | B1a（hub_types changed）、B3b（replay）、B4-4（事件流帧过滤挂钩） |
| **digest/q/p 域** | `sse/global_hub.py`（digest changed 段）、`sse/hub_types.py`（DigestFields）、`routes/questions.py`、`routes/permissions.py`、新 `qp_sweep.py`、`config.py`（sweep 参数段）、`app.py`（sweep 装配段）、`tests/test_questions*.py`、`tests/test_permissions.py`、`tests/test_hub*.py` | B1a、B1b、B3b（q/p 补全）、B4-4（digest/q/p 帧过滤挂钩） |
| **routes（messages/新路由）域** | `src/oc_slimapi/routes/messages.py`、`routes/write_groups.py`、`routes/read_groups.py`（allowlist 挂钩点）、新 context/agent/model/revert 路由文件、`tests/test_messages*.py`、`tests/test_write_groups*.py` | B2（merged 兼容验证，只读）+ B4（新路由 + allowlist 挂钩） |
| **config/metrics 域** | `config.py`（通用段）、`traffic.py`、`traffic_snapshot.py`、`access_log.py`、`routes/metrics.py`、`sse_observability.py`、`tests/test_config.py`、`tests/test_traffic*.py`、`tests/test_access_log*.py`、`tests/test_metrics.py` | B0-6（观测设计）、B3a-A4、B3a-B5、B1b-5/6 |
| **docs/contract 域** | `docs/specs/v4-contract.md`（新）、`docs/specs/v3-contract.md`、`docs/specs/INTERFACE_MAP.md`、`CHANGELOG.md`、`docs/release.md`、`AGENTS.md`、`docs/operations.md`、`scripts/check_routes_doc.py` | B0 全部、B1a-2、B2-2、B4-5/6、B3a-B6、B3b-5 |

### 3.1a 单文件唯一 owner 声明（S-B07，R2 关键修订）

> 上表「涉及批次」列仅表示**相关性**；**写入权**按下述**单文件唯一 owner** 规则（评审阻断项，防止同文件并发写冲突）：

| 文件 | 唯一 owner（写入） | 旁路批次（仅只读/注释级/等待串行链） |
|---|---|---|
| `src/oc_slimapi/sse/global_hub.py` | **B1a**（digest changed）→ 完成后移交 B4-4（事件分派过滤挂钩，**串行链 B1a → B4-4**） | B1b（q/p 直推/resync **只读核实**，m 级不允许——见 §2.3 写域限制）；B3b（replay 产出路径，B3b 依赖 B3a 且与 B1a 不同 major，天然串行） |
| `src/oc_slimapi/config.py` | **显式串行链（R3 时序修正 + R4 发版标记位修正：按发版相位排序，Batching 1）**：`B4-4 allowlist 段 → B1b-4 sweep 段 →（P1 3.3.0 发版 ↔ §2.9）→ B3a-A1 版本段 → B3a-B1 DB 段 → B3b-1 上限参数段 →（P3 4.0.0 发版——B3a+B3b 两批**全部完成后**才发版，v2.2 行 245-247 冻结）`——各段不同任务按序合入，禁止并行编辑（886 行全仓最大共享面）；**发版相位标注嵌入链中**：P1（B4-4/B1b-4）段完成即发；P3（B3a-A1/B3a-B1/B3b-1）段**整体完成**后统一发 4.0.0，字面顺序即合入顺序 | 全部批次 |
| `src/oc_slimapi/app.py` | **与 config.py 同序串行链**（lifespan 装配段：`B4-4 allowlist 装配 → B1b-4 sweep 装配 →（P1 3.3.0 发版）→ B3a-A1 middleware 版本段 → B3a-B1 DB 装配段 → B3b-1 replay log 装配段 →（P3 4.0.0 发版——两批全部完成后，R4 修正）`） | 全部批次 |
| `src/oc_slimapi/routes/sessions.py` | **B3a-B4**（v4 分叉落地） | B4（allowlist 对 sessions 列表的过滤挂钩——并入 B3a-B4 或 B3a-B4 后串行） |
| `src/oc_slimapi/sse/hub_types.py` | **B1a**（DigestFields changed） | B3b（meta 扩展——与 B1a 不同 major 天然串行） |
| `docs/specs/v3-contract.md` / `CHANGELOG.md` / `INTERFACE_MAP.md` | **docs/contract 域集成批串行合并**（B0 → B1a → B2 → B4 → B3a → B3b 逐批按序提交，禁止并行编辑同文档） | 各批次的契约条目经单泳道合并 |

**并行禁区（re-check 结论，S-B07 + R3 时序修正 + R4 发版标记位修正）**：B1a / B1b / B4-4 **三方同时编辑 `sse/global_hub.py` 的不同语义区域**（digest changed / q/p 直推确认 / 事件帧过滤）→ 收紧为**单一 owner + 两条串行链**（global_hub 链：B1a → B4-4；config/app 链：**B4-4 → B1b-4 →（P1 3.3.0 发版）→ B3a-A1 → B3a-B1 → B3b-1 →（P3 4.0.0 发版——B3a+B3b 全部完成后，与 §2.9/§2.7 行 263 一致**）；B3a-A / B3a-B / B1b 对 `config.py` 的编辑全部并入 config 串行链。

### 3.2 并行 / 串行关系

**可并行（写域不重叠 + owner 不冲突）**：
- B0 内部：六份设计/契约文档（v4-contract / selector 设计 / SSE 重放设计 / q/p 核对 / DB 投影设计 / 治理修订）**内容互相独立**，可并行起草；唯 v4-contract 依赖其余设计结论 → 顺序 = 各设计先行、v4-contract 汇总后行（同域内串行）
- B1a（digest/q/p 域 + global_hub owner）与 B2（routes/messages 域，**只读验证**）与 B4 的**新路由部分**（B4-1/2/3）：文件不重叠 → **可并行**；B4-4（allowlist）中的 config/app 编辑入 config 串行链
- B1b（qp_sweep 新文件 + questions/permissions + 观测）与 B1a：`qp_sweep.py` 为 B1b 独有新文件、questions/permissions 为 B1b 独占 → **并行**；global_hub 只读核实 + config/app 入串行链
- B3a-A 内部：A1/A2（selector/versioning/config 版本段）与 A4（traffic/access_log/metrics）文件不重叠 → 可并行；A3（versions/health）与 A1/A2 部分共享 routes/health.py + config → 协调/串行
- B3a-A 与 B3b：**共享 `config.py`/`app.py`——不并行编辑，按 §3.1a 串行链顺序集成（B3a-A1/A2/A3 版本段 → B3a-B1 DB 段 → B3b-1 →（P3 4.0.0 发版——两批全部完成后，R4 修正））**；且批次依赖 B3a → B3b（行 270）决定**合入顺序**

**必须串行（依赖 / 同文件 / owner）**：
1. **B0 → 一切**（所有批次依赖 B0 设计/契约定稿，行 264-273）
2. **B3a-A → B3a-B**：routes/sessions.py 同文件（v3 保持 + v4 分叉），且 v4 路由依赖 selector 双版本就绪（v=4 才能进路由）——阶段 A 先行
3. **B1a → B4-4**（`sse/global_hub.py` 事件分派路径：digest changed 先落、事件帧过滤挂钩后落）；**B1a → B1b**（global_hub 任何必要编辑经由 B1a 串行链——B1b 默认只读核实）→ B1b 的 sweep 组装依赖 B0 + B1a 完毕
4. **B3a → B3b**（行 270）；**B3a 依赖 P2（B5a 就绪）**（行 269）
5. **P2（消费者 B5a）→ P3（sidecar 4.0.0）**：发版时序铁律（v2.2 行 244, 269）
6. B4-4（allowlist）与 B1b-4（sweep）：`config.py` 同文件不同段 → **入 config 串行链（R3 修正 + R4 发版标记位修正，发版时序一致序）**：B4-4 → B1b-4 →（P1 3.3.0 发版）→ B3a-A1 → B3a-B1 → B3b-1 →（P3 4.0.0 发版——B3a+B3b 全部完成后，v2.2 行 245-247 冻结）；**B3b 必须紧随其后串行合入（B3b-1 共享 config/app，且依赖 B3a，§2.7 行 253）**
7. **docs/contract 域**：所有涉及 v3-contract / v4-contract / CHANGELOG / INTERFACE_MAP 的批次条目**集成批串行合并**（单泳道按 B0→B3b 顺序提交，禁止多泳道并行编辑共享文档）

### 3.3 冲突协调规则
- 同文件同段：严格串行；同文件不同段：可并行但 commit 前 rebase 协调（建议同批同文件任务由同一泳道或顺序执行）——**§3.1a 单文件 owner 表优先于本条的宽松表述**
- `config.py`（886 行，全仓最大共享面之一）为热点：allowlist 段（B4-4）、sweep 段（B1b-4）、版本段（B3a-A1）、DB 段（B3a-B1）、replay 上限参数段（B3b-1）——**按 §3.1a 串行链顺序扩展（R4 修正：B4-4 → B1b-4 →（P1 3.3.0 发版）→ B3a-A1 → B3a-B1 → B3b-1 →（P3 4.0.0 发版，两批全部完成后），发版相位对齐）**
- `v3-contract.md`：B1a（§7）与 B2（§4a 注记）不同章节——可并行编辑但同文件 → 集成批串行合并更安全
- 所有新增 `/slimapi` 路由（B4、B3a）必须同步 `INTERFACE_MAP.md`（check_routes_doc 防漂移，AGENTS.md 铁律）——docs/contract 域独占

---

## 4. 与其他两项目的接口冻结点

### 4.1 v4 wire 契约条目（ocdroid / webui 依赖清单）

**capabilities["4"] 能力键**（v2.2 行 254；**静态能力键**，不随 DB 抖动，行 140）：

| 键 | 语义 | 消费方 | 广告批次（n1：与实现同批启用） |
|---|---|---|---|
| `globalSessions` | v4 全局会话目录可用（DB 投影源或降级） | ocdroid B5a 探测、webui | **B3a** |
| `sseReplay` | SSE id:/Last-Event-ID 重放可用 | ocdroid（B5b 实现重放逻辑） | **B3b**（B3a 时期**不**广告） |
| `qpImmediateFull` | q/p 帧载荷直投完整（语义由 B0-4 结论在 v4-contract §3 定稿时冻结；若需补全随 B3b 同入 4.0.0 同一 major（行 245-247），键保持静态（行 140）——原 §8 问题 7 已就地解决，见 §8.2） | webui（q/p 直投） | **B3b**（B3a 时期**不**广告） |
| `auxiliaryFilters` | archived/parent 过滤能力广告（静态） | ocdroid/webui 能力探测 | **B3a** |

**ocdroid 依赖字段/错误码/路由**（B5a 适配，v2.2 行 223-227）：
- `capabilities["4"]` 探测：不存在 → 继续 v=3（行 223）；**sseReplay/qpImmediateFull 在 B3a 期不探测/不依赖（B3b 才广告，n1）**
- **DirectoryHeaderInterceptor 豁免 `/slimapi/sessions` directory 注入**（行 66）：v4 sessions 收到 directory → 400 `directory_retired_in_v4`
- status 改单次全局调用（行 138, 223）
- v4 sessions 语义：`parent=none` 替代 `roots=true`；parent 省略默认 = all（行 65, 224）；**v4 请求不再发送 `roots`/`start`（v4 显式 422 拒绝，S-B04）**
- cursor-aware 翻页（行 224）；404-sticky + 三形状解析随 v4 退役（行 226，生产流量证明后）
- **allowlist 三态（S-B05）**：sidecar 未配置 = 零行为变化；显式启用（显式空/非空）时 `/file` 403 `directory_not_allowed`——ocdroid 须在发布时同步适配（B4-4b 联合门槛）

**webui 依赖**（行 229-232）：
- q/p 帧载荷直投（若 B0-4 核对为完整，纯客户端改动，行 231；能力键 `qpImmediateFull` 随 B3b 广告后启用）
- merged 正文全量内联（[3.2.0] 已实现，零改动受益；400 码点方案已废止，webui 无需处理 `truncated`）
- 收藏扇出 → 全局列表一次拉取 + 客户端分组（**cursor-aware**：全局 limit ≤500 不保证含全部收藏根会话，行 232）

**错误码/响应字段冻结清单**（v4-contract §4/§8）：
- `invalid_cursor`（400，指纹不匹配，行 85, 129）
- `auxiliary_unavailable`（503，降级矩阵，行 118-120）+ `Retry-After`（行 122）——**含 allowlist 非空 + DB 熔断维度（fail-closed，ora B-2）**；**消费者注记（终检统一裁决：503 = 显式错误，客户端不自动回退 v3——维持当前 wire 版本，按 Retry-After/partial/手动重试处理；v3 目录级浏览仅经用户显式触发的整体版本重协商（available 含 3 时覆写 selectedWireVersion=3，全端点一致），且是**功能降级非等价回退**（跨目录 parent/archived 过滤与全局 cursor 翻页在 v3 无对应语义）——UX 设计按「功能降级」建模，与 D/W 方案 fail-closed 语义一致（D :405 / W B5b-1 cursor 三分支）**
- `directory_retired_in_v4`（400，v4 sessions 拒绝 directory，行 66, 136）
- `degraded: true` 成功响应 schema（行 64）；v3 收到 v4 参数 → 422（行 136）；**v4 收到 v3 参数（roots/start）→ 422（显式声明，S-B04）**
- 503 错误体不泄露 DB 路径/schema（行 122）+ 403 不泄露目录存在性（行 188）
- SSE id: 语法/epoch/seq/作用域/重放顺序/gap 处理（v4-only，行 153；**四项协议裁决门槛见 §8.1 问题 5**）——ocdroid 需实现 Last-Event-ID 重放（B5b）
- **health 响应 `allowlist: {enabled: bool}`（S-B05，P1 起单视图、B3a 起双视图）与 `auxiliary: {available, mode}`（行 140）**——ocdroid 感知 sidecar 过滤/降级状态的官方通道

### 4.2 发版时序（P2 消费者先行 → P3 sidecar major）

```
P0  B0 规范先行（契约定稿）——ocdroid/webui 开发者凭 v4-contract 开始 B5a 开发
P1  sidecar 3.3.0 minor（B1a+B1b 阶段 1+B2 兼容验证+B4，零 breaking）——ocdroid/webui 渐进采纳
P2  B5a 消费者兼容版（capabilities["4"] 探测 + v3 回退 + 拦截器豁免 + status 全局单调）——先于 B3 发布（行 271 "先于 B3 发布"）
P3  sidecar 4.0.0 major（B3a+B3b，wire (3,4)）——消费者 v4 客户端经 B5a 探测自动启用；**前置 checklist（n5）**：ocdroid B5a 已发 + webui B5a 已发 → 方可执行 major
P4  v3 流量归零判据（access log + SSE active，行 248）→ sidecar 5.0.0 major（(4,4) 删 v3）——B5b 消费者 v4 适配完成后
```

**冻结点**：B3a 依赖 P2（B5a 就绪，行 269）——sidecar 4.0.0 不得早于消费者兼容版发布；sidecar 侧在本仓只需冻结契约，B5a/B5b 执行在 ocdroid/webui 泳道。

---

## 5. 测试与验收策略

### 5.1 基线 + 增量（2,199 collected tests）

- **基线**：2,199 tests / **77 个 `test_*.py`**（n7 勘误：`tests/*.py` 78 个含 `conftest.py` 等非测试文件；v2.2 行 28, 216 引用值按此口径校准）
- **每批增量计划（预计新增参数化 case 数，S-B08：混入 §5/CB 计划供排程）**：
  - B0：无 src 测试（纯设计+实证）；门槛 (a) WAL 陈旧读 CI 测试 **3 case**（`tests/test_wal_staleness.py`）；(b) EQP 矩阵 48 参数化（B0 实证脚本，B3a-B2 落地）；(c) 路径解析 ~10 case（B3a-B1 落地）；(d) 降级矩阵 **12 格 × DB 三态 × allowlist 两态 ≈ 72 case**（B3a-B4 落地，R3 口径对齐 §5.3 两维）；(e) SQL 语义 ~12 case（B3a-B2 落地）；(g) 等价性锚定用例矩阵（B3a-B2 落地）
  - B1a：digest changed 帧测试（SSE 帧形扩展，v3 §11.6 测试面）**~6 case**
  - B1b：两阶段 sweep 调度/cold-set 进出/预算/日预算闸/access-log 断言 **~18 case**（阶段 1 交付 shadow 模拟测试 + 零逃逸断言，阶段 2 交付对比断言）
  - B2：兼容验证断言（merged 正文与缺省 skeleton 同源、无 `truncated` 泄漏）**~6 case**
  - B4：新路由（context/agent/model/revert）+ allowlist 三态矩阵（未配置/显式空/非空）**~30 case**
  - B3a：selector 双版本全矩阵 + 跨版本错误优先级真值表 + config 迁移 + DB 投影源全测试面（schema 门/熔断含最小样本与 hysteresis/并发模型/inode/路径解析/降级矩阵逐格/EQP/等价性锚定/cursor/roots-422）**~120 case**
  - B3b：重放协议全矩阵（gap/过期/背压/重启 epoch/重连/tokens=1 双序列/ID 无倒退）**~20 case**
  - B6：合并后回归 + 退役相关测试更新（净变化保守为 0 附近）
- **净变化**：v4 测试面新增（预计净增 ~180-200 参数化 case）> sticky/fan-out 高频面删除（行 216 "不承诺具体数，实测复核"）

### 5.2 等价性锚定（行 148, 264g；权威源修正 S-B03）

- 契约测试**逐版本**锚定：DB 投影 ≡ 权威源投影（同一行集同字段语义）
- **权威源二选一（S-B03，自证循环防御——mocked HTTP 由 sidecar 自身期望构造 = 只证"符合自己的 mock"，检测不了上游 schema 漂移）**：① **固定对齐版本的真实 opencode HTTP handler 进程**（契约测试拉起 `opencode-src/current` 的 server，走真实路由处理 `/experimental/session`，检测真实上游漂移）；② **版本标记 golden 响应**（自真实上游生成 + 对齐版本号 + 生成指纹，离线可跑 CI 稳定，容忍版本滞后窗口）。选型结论进 B0-6g 设计文档；**禁止**以 sidecar mock 期望为唯一权威
- 测试设计：对同一数据集（草稿库），DB 路径结果与权威源结果对比（行集、字段语义、`(time_updated DESC, id DESC)` 排序（n4：上游 session.ts:571-572 为该复合排序，非单键）、LIMIT+1 complete 判定）
- 上游演进时等价性测试失败 = 禁用辅助的信号（行 148）——**联动运维 runbook（n6，B0-6f③）：升级 opencode 后第一步观察 health `auxiliary.available`/`mode`，熔断 = 等价性失败的信号，对照 §6.2 禁用链排查**

### 5.3 降级矩阵逐格（行 111-124, 264d；allowlist 维度 = ora B-2）

全部可降级格全测（v2.2 行 113-121 矩阵）。**矩阵正式维度 = 请求状态 × allowlist 状态**（allowlist 列新增，ora B-2——上游 listGlobal 单窗 LIMIT 首 N 行后置过滤 = 真子集风险：极端首屏空而白名单内有会话，违反「过滤语义永不降级」（行 123））：

| 请求状态 | allowlist 为空（未配置机制） | allowlist 非空 |
|---|---|---|
| `archived=omit\|all` + `parent∈{all,none}` + 无 cursor + search 任意 | 200 + `degraded:true`（**排序等价、cursor 翻页强度退化**——n4：弱的是单键 cursor 翻页与分页强度，非排序本身；complete 退 best-effort，degraded 披露） | **503 `auxiliary_unavailable`（fail-closed，裁决选②）** |
| `archived=only` | 503 `auxiliary_unavailable` | 503 `auxiliary_unavailable` |
| `parent=only` / `parent=<sid>` | 503 `auxiliary_unavailable` | 503 `auxiliary_unavailable` |
| 带 `cursor`（任何组合） | 503 `auxiliary_unavailable` | 503 `auxiliary_unavailable` |

- **ora B-2 裁决论证**：allowlist 非空 + DB 源不可用（熔断/禁用）时，候选路径①「内部循环翻页凑满 N 行白名单结果或穷尽」被否——理由：(a) 上游 `/experimental/session` 单窗 LIMIT 首 N 行后置过滤可能产生**真子集/空结果**（极端首屏全部非白名单），翻页凑行需放大请求且破坏省流目标；(b) 白名单是安全敏感场景，**宁可 503 不给子集**——与 B4-4 全局 fail-closed（行 184, 188）同构；(c) **单连接 + 全过滤入 SQL 谓词 = schema 门 / 熔断 / inode generation 单一状态机**（R3 补强：DB 源本是单连接串行模型，降级路径若做循环翻页会撕裂该状态机——翻页分页的循环语义与单快照原子性（行 81）冲突，进一步支持选②而非维护两套源语义）；选②语义 =「DB 熔断 = 过滤能力不确定 → 拒服务」符合行 123「无法等价表达 → 503」。**允许的例外**：allowlist 机制未启用（env 未配置，S-B05 三态）→ 无过滤义务 → 走 HTTP 降级路径无额外障碍（左列语义）
- 每格断言：状态码、`degraded` 语义、`Retry-After`、错误体不泄露 DB 路径/schema 细节（行 122）+ 503 不泄露白名单内容
- 边界原则：`degraded:true` 只表数据源降级+强度弱化；**过滤语义永不降级**——可等价 → 200+degraded，不可等价 → 503（行 123）；allowlist 维度上「过滤语义」= 白名单 ⊆ 结果集（放行不失、禁止不漏）

### 5.4 跨版本矩阵（v2.2 行 254 §11）

- 版本维度：v3 / v4 / 无 v / 非法 v / 多值
- 路由维度：sessions（列表/status）、messages、SSE 两端点、新路由（context/agent/model/revert）、/file
- 参数维度：directory（v4 sessions 拒绝 → `directory_retired_in_v4`）、archived/parent/cursor（v3 → 422）、**roots/start（v4 显式 422，S-B04——不依赖 FastAPI 未知 query 忽略）**、search/limit
- 状态维度：DB 可用 / 禁用 / 熔断（三态 × 降级矩阵 × allowlist 空/非空——ora B-2 维度）
- **跨版本错误优先级真值表（S-B04，逐组合断言，全量进契约 §8）**：malformed cursor vs 辅助不可用（cursor 语法校验先于降级判定 → `invalid_cursor` 优先）／指纹不匹配 vs 熔断（指纹校验在查询前 → 400 先于 503）／`directory_retired_in_v4` vs 参数错误（selector 层 directory 400 先于路由层 422）／repeated v vs 路由错误（重复合法 v 值折叠后正常路由，不因重复而 400）
- 生命周期维度：重启（epoch 变化）/ 冷启动（含 P99 熔断 warmup 豁免，S-B02）/ 背压 / 过期 / 上游重连 / **运行中 DB 文件 swap（inode 变化，S-B02）**
- ETag 域隔离：v3/v4 validator 互不匹配（行 254 §6）

### 5.5 check.sh 门禁

- 每次改动后 `./scripts/check.sh`（pytest + check_routes_doc）
- 新 `/slimapi` 路由必须进 `INTERFACE_MAP.md`（防漂移，AGENTS.md 铁律）
- 发版前 check.sh 绿（release.md §4）+ 目标版本节已存在（release.md §3.2）

---

## 6. 风险与回退

### 6.1 每批回退路径

| 批次 | 失败模式 | 回退路径 |
|---|---|---|
| B0 | 设计矛盾 / 实证推翻 | 设计迭代，不进入实现；开放问题清单先行裁决（§8.1） |
| B1a | changed 字段触发语义争议 | 缩小到最小语义（仅字段形状冻结，触发语义文档化）；最坏撤销字段（v3 加性可忽略字段，旧客户端零影响）——**回滚契约注记（n2）**：changed 字段撤回后，已采用定向精拉的新客户端必须明确回退全量刷新策略（撤回语义与回退路径写入契约 §7 修订+CHANGELOG，防新老客户端悬挂在不可用字段上） |
| B1b | 阶段 2 sweep 流量超基线（exit criteria 不达标） | 阶段 1 已发布的 3.3.0 为**真 shadow/dry-run（零真实 sweep 请求，仅观测 marker 与模拟调度——无 wire/semantic/流量变化，不回滚）**（R3 修复：shadow 才使"阶段 1 无行为变化"成立）；阶段 2 未达标 → 调大冷目录静默窗 T / 调小日预算 / 增 skip 谓词；最坏回退 = 维持 shadow 观测态不启用 sweep（恢复现状：事件驱动 + 客户端 resync，行 161-162），access-log 记录结论 |
| B2 | 兼容验证发现 merged 折叠/全量内联与两模式不一致 | 验证报告定位不一致点 → 按现状契约修复/对齐（[3.2.0] 全量内联为终态，非本批决策变更）；不可能"截断回归"——400 码点方案已废止（§8.1 问题 1 关闭） |
| B4 | 新路由上游语义核对失败 | 未收编路由保持 catch-all 透传（旧行为不变）；allowlist 误伤 → 配置回退为空（不限制，现状兼容，行 185） |
| B3a-A | selector 双版本回归 | 4.0.0 发布前 = 不发布；发布后 = v3 语义逐字节保持（双版本并存，v4 客户端可回退 v=3，行 223 B5a 探测） |
| B3a-B | DB 投影源错误/陈旧读 | **整体禁用开关（§6.2）**：熔断/探针失败 → 全降级 HTTP（行 97）；降级矩阵已定义 503 语义（行 118-120），客户端见 `auxiliary_unavailable` **不自动回退 v3（终检统一裁决：503 = 显式错误，维持当前 wire 版本，按 Retry-After/partial/手动重试处理；v3 目录级浏览仅经用户显式触发的整体版本重协商（available 含 3 时覆写 selectedWireVersion=3，全端点一致）**，且是**功能降级非等价回退**——v4 的跨目录 parent/archived 过滤与 cursor 全局翻页在 v3 无对应语义；功能落差写清防 ocdroid/webui 按"等价回退"设计 UX，v4-contract §4 消费者注记对齐 D :405 / W B5b-1 cursor 三分支的 fail-closed 语义**）；**allowlist 非空时熔断 = 503 fail-closed（ora B-2 选②，不存在"降级到后置过滤"的中间态）**；最坏 = 关闭 DB 投影功能（v4 sessions 全 503），v3 不受影响 |
| B3b | 重放协议 bug | v4-only 特性，v3 SSE 帧名帧形冻结不变（行 153）；回退 = v4 客户端不使用 Last-Event-ID（退化为现状 resync 全量） |
| B6 | 合并破坏去重语义 | 保留双实现（行 210 撤回主张）；sticky/三形状退役失败 → 保留（生产流量证明前提不满足则不执行） |

### 6.2 DB 投影源整体禁用开关（运维路径，全降级 HTTP）

v2.2 行 96-97, 100 定义的禁用链（**运维无需改代码**）：

1. **显式配置缺失**：`OC_SLIMAPI_OPENCODE_DB` 未设且默认解析失败（`:memory:`、路径不存在、channel 无法判定）→ 启动禁用辅助源（行 98）
2. **启动探针失败**：ro 打开失败 / schema 门不过 → 禁用（**不试 immutable**，行 96-97）
3. **运行中熔断**：查询错误分类（`SQLITE_SCHEMA` / no such table\|column / I/O / WAL-SHM 不可达）→ 熔断禁用 → 周期重探恢复（行 100）
4. **P99 > 20ms 超限** → 熔断降级 + 告警（行 106, 147）
5. **inode/mtime 变化**（备份恢复 / channel 切换）→ 重开重探（行 99）
6. **等价性锚定测试失败** → 人工禁用辅助（行 148）

禁用后效果：**v4 sessions 全走降级矩阵（allowlist 空 → 200+`degraded`；allowlist 非空 → 全 503 `auxiliary_unavailable`，ora B-2 选② fail-closed）**；客户端经 `/slimapi/health` `auxiliary: {available:false, mode:"http"}` 感知（行 140）；**v3 完全不受影响**（双版本隔离）。运维排障文档进 operations.md（B0-6f③，含 runbook：升级 opencode 后观察 auxiliary 状态，熔断=等价性失败信号，n6）。

---

## 7. 工作量估计（omni-orch 排程用）

| 批次 | 规模 | 说明 |
|---|---|---|
| B0 | **L** | 六份设计/契约文档 + 七项硬门槛实证（EQP 全矩阵、WAL CI 测试、路径解析、降级矩阵、SQL 语义、治理修订、等价性设计）+ **SSE 协议四项裁决门槛（S-B01，B0 出门 gate）**；B0 内测试/工具类产出增量（wal_staleness CI、eqp 脚本、各用例表） |
| B1a | **S** | 单一字段扩展（hub_types/global_hub + 契约 §7 + 测试 ~6 case；global_hub 写入权独占） |
| B1b | **M** | **两阶段**（S-B06 + R3 shadow 收紧）：阶段 1（**真 shadow/dry-run 观测：模拟调度 + marker，零真实请求**/cold-set 判定/日预算闸/观测字段）+ 阶段 2（cold-set 启用/稳态对比）；18 测试 case（对齐 §5.1）；global_hub 仅只读核实 |
| B2 | **S－** | **兼容验证型**（400 码点截断废止后无实现量，S-B08）：merged/两模式 only-read 核验 + 契约注记 + 6 断言 case |
| B4 | **M** | 三组新路由（context/agent/model/revert）+ allowlist 三态 fail-closed 全矩阵（含 health 字段、SSE 丢弃+计数、联合门槛）；~30 测试 case |
| B3a | **L** | 最高风险：selector 双版本（A1-A4 含 config 迁移 S-B04）+ DB 投影源全栈（B1-B6：connection ownership 并发模型 S-B02/路径解析/SQL 组装/cursor/降级矩阵含 allowlist 维度/观测/等价性锚定权威源 S-B03）；单一最大测试面 ~120 case |
| B3b | **L** | 重放日志新组件 + SSE id: 协议（受 S-B01 裁决约束）+ meta 扩展 + q/p 补全（若需）+ 能力键广告；~20 测试 case |
| B6 | **M** | singleflight 合并（~770 行双实现 → 1）+ 退役清理 + 账目 17 复核（ora B-1） |

**排程建议**：B0 全程 L 独占；B1a/B1b/B2/B4 在 B0 后按 §3 写域矩阵并行；B3a 独占（A→B 串行）；B3b 紧随 B3a；B6 末尾（生产流量证明后）。

---

## 8. 开放问题（R2 重组：3 项待裁决 + 9 项已裁决/就地解决存档）

> 2026-08-17 核准（fixer-glm）：原 10 项经 v2.2 / v3-contract / src 事实核对重组——7 项可凭既有权威文本就地解决（§8.2 存档），3 项确需 owner / 三方评审裁决，另核准新发现 1 项契约冲突（最高优先级）。
> **2026-08-17 R2（fixer-ds，双评审收敛）**：① §8.1 问题 1（B2×[3.2.0] 冲突）**已由 owner 裁决关闭**（TextPart.text 永不截取全量内联，400 码点废止）→ 移入 §8.2 存档；② §8.1 问题 4（sweep 组件账目）**随 v2.2 §5 修订闭合**（15→17 phase-aware，sweep 调度器 + DB-aux 生命周期独立计账）→ 移入 §8.2 存档，B6-3 按其复核；③ **新增问题 5（SSE 协议裁决门槛，S-B01，rev-sgpt 阻断）**——§8.1 待裁决项净剩 3 项。

### 8.1 待 owner 裁决（B0 后净剩 1 项：S-B01 ②③④ 待终裁）

> **2026-08-17 B0 批写回（omni-orch 会话三项 owner 裁决落定）**：原问题 2（changed 触发语义）、原问题 3（B1b exit 口径）**已裁决关闭**移入 §8.2 存档；S-B01 **①已裁决关闭**（v4 禁止复用），②③④由 B0-3 设计文档（`docs/specs/design-v4-sse-replay.md`）产出裁决方案，随 B0 汇报上报 omni-orch 升 owner 定夺后方可冻结进 v4-contract §7——**B0 出门 gate 仍要求四项全部收敛记录**（①终裁 + ②③④提案在案）。

1. **SSE 协议裁决门槛（S-B01，rev-sgpt 阻断）**：SSE id:/重放的四个协议前提彼此耦合且 v2.2 行 153 未定稿，**不收敛则 B0 不出门、`sseReplay:true` 不得进 v4 capability**（n1 联动）：
   - ① **tokens=1 统一流——已裁决关闭（owner，2026-08-17）**：**v4 禁止复用**——`/events?tokens=1` 请求在 v4 返回 400，token 流必须走独立 `/sessions/{sid}/stream`。理由：单 Last-Event-ID 无法恢复双序列（meta-first 与重放顺序结构性矛盾：重连新 meta 分配新 seq 后发旧 replay 帧 = 线上 ID 倒退）；webui/ocdroid 本就分离两连接，成本最低。已写入 v4-contract §7.0/§7.3 与 design-v4-sse-replay.md（[已裁决]）。
   - ② **meta 重连语义**（**B0-3 设计提案待 owner 终裁**，方案+论证见 `docs/specs/design-v4-sse-replay.md`）：重连 meta 是否带 `id:`、epoch 更换规则、严格线序定义（meta → replay → 新帧的无倒退顺序）；
   - ③ **token ID 作用域**（**B0-3 设计提案待 owner 终裁**，同上）：全局 / per-sid / 每连接——全局序列因其他 sid 消费 seq 产生合法空洞，**gap 不能当丢帧**（须区分「消费者缺席 seq」vs「日志逐出」）；
   - ④ **两端点逐帧状态机**（**B0-3 设计提案待 owner 终裁**，同上）：`/events` 与 `/sessions/{sid}/stream` 各自的 replay / snapshot / resync 完整帧序列表（含 epoch 切换、背压、subscriber 溢出）。
   - 裁决输入 = B0-3 设计文档逐项方案+论证；冻结 gate = 四项终裁记录进 v4-contract §7 定稿（①已完成，②③④随 B0 汇报上报）。

### 8.2 已就地解决 / 已裁决（9 项存档——凭既有权威文本解决，结论已写回对应批次）

| 原问题 | 就地结论 | 依据 | 写回落点 |
|---|---|---|---|
| （R2）**问题 1：B2×[3.2.0] 契约冲突** | **已裁决关闭**（owner v2.2 §3.3 定稿，2026-08-17）：`TextPart.text` **永不截取、永远全量内联**（[3.2.0] 契约胜出）；400 码点 merged 截断**废止**；裁剪仅两模式（折叠内容 omitted+expandRefs 默认不加载 / 未展开缩略信息如 diffStats）；B2 改写为兼容验证（§2.4），回 P1 3.3.0 | v2.2 §3.3（m00476 裁决）+ 本方案 §2.4/§1.2 第 8 条 | §2.4 / §1.1 / §7 |
| **（B0）S-B01 ①：tokens=1 统一流** | **已裁决关闭**（owner，2026-08-17 omni-orch 会话）：**v4 禁止复用**——`/events?tokens=1` 请求在 v4 返回 400，token 流必须走独立 `/sessions/{sid}/stream`。理由：单 Last-Event-ID 无法恢复双序列（meta-first 与重放顺序结构性矛盾：重连新 meta 分配新 seq 后发旧 replay 帧 = 线上 ID 倒退）；webui/ocdroid 本就分离两连接，成本最低 | owner 裁决（B0 工单 PRE 记录）+ v2.2 行 153 | B0-3 / B3b-2 / v4-contract §7.0/§7.3 / design-v4-sse-replay.md |
| **（B0）原 §8.1 问题 2：B1a digest `changed` 触发语义** | **已裁决关闭**（owner，2026-08-17 omni-orch 会话）：**最小语义**——`changed:[本帧sid]`（digest 为 per-sid 逐帧产出，帧出现即 changed，覆盖全部触发 digest 的事件：message.*/status/archived/deleted/updatedAt）；形状保留 `[sid…]` 列表为未来聚合留形；sidecar 零新增状态 | owner 裁决（B0 工单 PRE 记录）+ sse/global_hub.py:392,419 事实 | B1a-1 / v3-contract §7 修订 |
| **（B0）原 §8.1 问题 3：B1b 阶段 2 exit criteria 口径** | **已裁决关闭**（owner，2026-08-17 omni-orch 会话）：**B1b-5 现稿确认**——稳态窗连续 7 日；公式 = 真实 sweep 请求 + 事件驱动 + 客户端 q/p 相关请求合计 < 阶段 1 同期（7 日）实际基线（shadow 模拟计数不入基线）；载体 = traffic snapshot（保留期配置 >7 天）为主、access log（RETAIN_DAYS=3）短窗辅助；cold-set 长尾可达性作并列判据 | owner 裁决（B0 工单 PRE 记录）+ 本方案 §2.3 B1b-5 现稿 | B1b-5 / §6.1 回退 |
| （R2）**问题 4：sweep 组件账目口径** | **随 v2.2 §5 修订闭合**：账目 15→**17 phase-aware**（P1 16 = 15+sweep 调度器 → P3 18 = +DB-aux 生命周期 + replay log → B6 后 17 = −singleflight 合并）——sweep 调度器与 DB-aux 生命周期**独立计账**已由 v2.2 裁决；B6-3 按此复核 | v2.2 §5（m00488 修订）+ 本方案 §2.8 B6-3 | B6-3 / §7 |
| 2（SERVER_API_VERSION 双视图） | `server.api_version`（与 `schema.version` 同源同值）**按请求 wireVersion 取视图值：v3 视图=3、v4 视图=4，禁止错配组合**；实现由单一常量改为视图映射 + **env `OC_SLIMAPI_SERVER_API_VERSION` 双版本期废弃（R2 S-B04 增补）** | v3-contract §3a 冻结先例（v2/v3 期 v2 视图=2 / v3 视图=3、同源同值、禁止 3/2 组合；health.py:37 现状）+ v2.2 行 254 §3「双视图」 | B3a-A1 |
| 3（revert 三段式并存） | 三段式 = **加性新路由**；既有单步 `POST /slimapi/session/{id}/revert`（v3 §10.b #8）保持不动；上游三段式端点/payload 形状 = B4 实现前置核对（非裁决项） | v2.2 §3.4「B4 加性，v=3 即可用」+ 本方案 P1 零 breaking | B4-3 |
| 4（context/agent/model 上游源路径） | 实现前置核对 opencode-src/current 实际 handler 路径与 payload 形状（非 owner 裁决项） | AGENTS.md 既有约定「涉及上游语义先读源码」 | B4-1 / B4-2 |
| 5（降级矩阵 × allowlist） | **R2 修订（ora B-2 裁决，否决 R1 后置过滤方案）**：allowlist 非空 + DB 源不可用 → **一律 503 `auxiliary_unavailable`（fail-closed 选②）**——上游单窗 LIMIT 首 N 行后置过滤 = 真子集/空结果风险（极端首屏全非白名单），违反行 123「过滤语义永不降级」；停止翻页凑行的放大方案（破坏省流目标）；allowlist 未启用（env 未配置）→ 无过滤义务，HTTP 降级路径无额外障碍 | v2.2 行 123 + 行 184-188（fail-closed 一致性）+ ora B-2；行 186 全局面过滤仍成立但仅约束 DB 可用路径 | §5.3 / B3a-B4 / B4-4 |
| 6（重放日志 × 背压溢出） | 溢出帧是否入日志、断连 gap 由重放补还是 resync——属 B0-3 重放协议设计**必答题**（行 153 已列「与 meta-first/背压/上游重连的顺序」为协议要素），随设计文档 rev gate 把关，非 owner 裁决项；**R2 并入 §8.1 问题 5 协议状态机设计（④）** | v2.2 行 153 | B0-3 |
| 7（qpImmediateFull × B0-4 耦合） | 时序自洽：B0-4（B0 批）先于 v4-contract §3 能力键定稿；若缺字段，补全随 B3b 与 B3a 同入 4.0.0 **同一 major**（行 245-247）→ 能力键发布时语义已终态，静态性（行 140）不破；**R2 增补（n1）**：`qpImmediateFull`/`sseReplay` 广告与实现同批（B3b 才广告，B3a 不广告） | v2.2 行 164（核对前置）+ 行 245-247（同一 major 分批）+ n1 | B0-4 / §4.1 / B3a-A3 / B3b-5 |
| 8（directories v4 形态） | `/slimapi/directories` **保持现形态**（/experimental/session 发现 + allowlist 过滤），**不**统一为 DB 投影 | v2.2 行 183「GET /slimapi/directories 保持」+ 行 145 范围冻结（DB 辅助仅限 v4 sessions 投影，不得扩展）+ 行 186 | B3a-B4 |

---

*（完）本方案为 oc-slimapi 侧工程执行细化；技术裁决全部引用 v2.2 行号，未新增决策（2026-08-17 核准修订与 R2 双评审修复除外，见头注）。§8.1 现仅剩 S-B01 ②③④（SSE meta 重连语义 / token ID 作用域 / 逐帧状态机——①tokens=1 与 changed / B1b exit 口径已裁决关闭移入 §8.2）：B0-3 已产出设计提案（`docs/specs/design-v4-sse-replay.md` §2），随 B0 汇报上报 owner 终裁后冻结进 v4-contract §7；§8.2 十二项已凭既有权威文本或 owner 裁决就绪并写回对应批次。*
