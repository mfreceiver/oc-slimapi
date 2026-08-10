# access-log bounded async writer — Design

> **状态：设计稿（DESIGN ONLY）。本文档在 Task 7 批次内**仅作为设计产出**，未落地任何代码改动。实施需另开实施计划并经 reviewer 放行（见 [§8 不在本计划范围内](#8-不在本计划范围内)，以及 `docs/implementation-batches-2026-08-09.md` Task 7 的 Acceptance Criteria T7-C4）。文档内任何"writer 线程 / queue / drop / drain"描述均为**目标设计语义**，**不代表**当前 `src/oc_slimapi/access_log.py` 的运行行为。

| 字段 | 值 |
|---|---|
| 来源批次 | `docs/implementation-batches-2026-08-09.md` Task 7（C 节 L629-668，F 节矩阵 T7 行）|
| 前序依赖 | Task 6（已 PASS）：`DailyAccessHandler.emit` 单调用 `write(msg + "\n")`，缩小应用层半行窗口，**best-effort，非 fsync/事务级原子** |
| 产出物 | 本设计文档（唯一）|
| 冻结项 | 5 项（[§3](#3-冻结设计决策)），全部为确定值/策略，无"待定/TBD" |

---

## 1. 背景与动机

### 1.1 现状（Task 6 之后）

`src/oc_slimapi/access_log.py` 的 `DailyAccessHandler.emit` 当前形态：

```python
def emit(self, record: logging.LogRecord) -> None:
    try:
        today = datetime.fromtimestamp(record.created).date()
        if self._current_date != today:
            self._close_current_fh()
            self._ensure_dir()
            self._current_fh = self._open_file(today)
            self._current_date = today
        msg = self.format(record)
        if self._current_fh is None:
            return
        self._current_fh.write(msg + "\n")   # P1-2: 单调用行写入
        self._current_fh.flush()
    except Exception:
        self.handleError(record)
```

Task 6 把原先的两次 `write`（先内容、后 `\n`）合成一次 `write(msg + "\n")`，缩小了**应用层**两次调用之间的半行窗口。`DailyAccessHandler` 依赖 `logging.Handler` 自带的 per-handler lock 提供线程安全，因此在多线程并发 `emit` 下不会交错写文件。

### 1.2 Task 6 的局限

Task 6 的注释如实标注 `best-effort，非 fsync/事务`。它解决的只是"两次 `write` 之间的应用层交错"，但并未提供以下保证：

1. **进程崩溃 / 断电 / OOM kill**：内核 page cache 中尚未落盘的字节会丢失，`write(msg + "\n")` 并不保证整行原子落盘。这一层需要 fsync / 事务 / WAL，**不在本设计范围内**（本设计只解决应用层并发与背压）。
2. **同步 I/O 阻塞事件循环**：`emit` 在调用线程内同步执行 `open` / `write` / `flush`。在 uvicorn / asyncio 单线程模型下，若 access log 的 `flush()` 发生磁盘抖动（例如 NFS / 慢盘 / fsync 抖动），会直接阻塞当前请求线程或事件循环。当前 oc-slimapi 是 loopback 单进程服务，请求路径与 access log 共享线程，磁盘抖动会被放大到请求尾延迟（request-end 的 `write_access_log` 调用）。
3. **无背压**：若磁盘持续慢，所有 emit 调用都同步等待磁盘，请求线程被逐个阻塞——没有"队列满 → 丢弃 → 保护请求路径"的机制。
4. **无统一 drop 告警**：因为不存在丢弃路径，也就不存在"丢了多少"的可观测性。

### 1.3 为何需要队列化 writer

引入**有界队列 + 单独 writer 线程**可从根本上解耦：

- **彻底解耦应用线程与磁盘 I/O**：应用线程只做 `queue.put_nowait(record)`（非阻塞），永不等待磁盘；磁盘抖动只影响 writer 线程，不影响请求尾延迟。
- **提供背压**：有界队列满时按确定策略（`drop_newest`）丢弃，保护请求路径与 writer，防止 backlog 雪崩。
- **统一 drop 告警**：所有丢弃事件集中在一个点（队列满分支），可施加 60s rate-limit，输出带窗口计数的 summary。
- **消除并发写竞争**：单 writer 线程独占文件句柄，跨天切换 / flush / open / close 全在单线程内串行，无锁、无 TOCTOU。

> 重申：本设计**仍不是** fsync / 事务级原子保证。崩溃 / 断电层面的原子性需要 fsync 或事务存储，超出本设计目标。本设计只承诺：**消除应用层并发竞争（单 writer）、消除请求路径上的同步磁盘 I/O 阻塞（队列解耦）、提供过载下的有损降级（drop + 告警）**。

---

## 2. 架构概览

### 2.1 数据流

```
┌──────────────────────────────────────────────────────────────────┐
│  Event loop / request threads  (uvicorn worker)                  │
│                                                                  │
│  traffic-accounting middleware                                   │
│       │                                                          │
│       │ write_access_log(logger, method=..., path=..., ...)      │
│       ▼                                                          │
│  logging.Logger.info(json_line)                                  │
│       │                                                          │
│       ▼                                                          │
│  DailyAccessHandler.emit(record)   [退化为: format + 入队]        │
│     ─── line = self.format(record)                               │
│     ─── try: queue.put_nowait(line)                              │
│         except QueueFull: drop_newest(line) + throttled warn     │
│                                                                  │
│   ※ emit 不再 open/write/flush 文件; 永不阻塞调用线程             │
└────────────────────────────┬─────────────────────────────────────┘
                             │  put_nowait  (非阻塞; 队列满则丢弃)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  Bounded queue                                                   │
│      queue.Queue(maxsize = 1024)                                 │
│   ┌────┬────┬────┬────┬─ ··· ──┬────┐                            │
│   │ L1 │ L2 │ L3 │ L4 │        │ Ln │   (Li = (record_date, formatted JSON line))│
│   └────┴────┴────┴────┴────────┴────┘                            │
│   <--- oldest (先入先落盘) ---> newest (队列满时被 drop) --->     │
└────────────────────────────┬─────────────────────────────────────┘
                             │  queue.get()  (writer 线程阻塞等待)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  Single writer daemon thread   (独占文件句柄)                     │
│                                                                  │
│   loop:                                                          │
│     record_date, line = queue.get(...)  # 元组解包，record_date 判跨天   │
│     # 跨天切换 (单线程, 无锁):                                    │
│     if record_date != self._current_date:                        │
│         close old fh; ensure_dir; open new fh                    │
│     fh.write(line); fh.flush()                                   │
│     on Exception → handleError + writeErrors++  (继续 loop)      │
│                                                                  │
│   shutdown:                                                      │
│     drain (≤ 5s) → 残余计入 dropped → final drop summary          │
│     → close fh → exit thread                                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
                 access-YYYY-MM-DD.jsonl   (磁盘)
```

### 2.2 线程模型

| 角色 | 数量 | 职责 | 是否触磁盘 |
|---|---|---|---|
| 请求 / 事件循环线程 | 1（uvicorn asyncio 主线程，可含 `to_thread` 池）| 处理 HTTP / SSE；在 request-end 调 `write_access_log` → `emit` → `put_nowait` | **否** |
| **writer daemon 线程** | **1**（全局唯一，进程级）| 串行消费队列；独占 fh；跨天切换；flush；shutdown drain | **是**（唯一）|
| maintenance（compress/prune）| 经 `asyncio.to_thread` 复用默认线程池 | gzip/prune 旧文件（**不触碰** writer 线程的当前 fh，经 `_active_handler_ref.current_path` 避让，见 [§4](#4-与现有-dailyaccesshandler-的关系)）| 是（对历史文件）|

**关键不变量**：文件句柄 (`self._current_fh`) 与日期状态 (`self._current_date`) **仅 writer 线程读写**。请求线程永远不接触 fh。

---

## 3. 冻结设计决策

以下 5 项为本设计的**冻结值/策略**，均为确定值，无"待定/视情况/后续确定"。实施计划必须以此为契约基线，不得偏离；如需变更，必须走正式设计 bump（类比 wire 契约 bump 流程）。

> 工程文档 `docs/implementation-batches-2026-08-09.md` L647-664 给出的实现形状与本节一致；本节是实施前最后冻结的设计权威。

### 3.1 队列容量上限 = **1024 条**

| 字段 | 值 |
|---|---|
| 上限 | **1024 条**待写入记录 |
| 队列类型 | 有界队列（`maxsize=1024`）|

**依据**：

- access log 每请求一行 JSONL，单条记录为紧凑 JSON（约 200–400 字节，含 `ts/method/path/bucket/status/durationMs/down*/up*/requestId/client*` 等字段）。
- 1024 条满载 ≈ 200–400 KiB RSS（仅 payload，不含 Python 对象开销），**有界且可预测**，不会因队列无界增长而吃爆内存。
- oc-slimapi 是 loopback 单用户 sidecar（服务 ocdroid 单客户端），稳态 QPS 远低于千级；1024 容量给出充足突发余量（典型场景下队列深度长期接近 0）。
- **过大**（如 65536）的代价：RSS 上升、记录在队列中滞留时间变长（尾延迟变差）、进程崩溃时丢失面变大。
- **过小**（如 16）的代价：磁盘稍有抖动即触发 drop，可观测性数据损失率高。
- 1024 = 2^10，符合队列容量约定的 2 的幂惯例。

### 3.2 溢出策略 = **drop_newest**

| 字段 | 值 |
|---|---|
| 策略 | **drop_newest**（队列满时丢弃最新到达的记录，保留已排队的旧记录）|
| 触发条件 | `queue.put_nowait(line)` 抛出 `queue.Full` |
| 行为 | 丢弃当前 line → `dropped` 计数 +1 → 触发 60s rate-limit 告警（见 [§3.4](#34-丢弃告警-rate-limit--60-秒)）|

**依据**：

- 保护已排队旧记录优先落盘：旧记录已经"等过"，丢弃它们等于浪费此前入队的工作；丢弃最新记录则保留了对已排队数据的 FIFO 承诺。
- 防止 backlog 雪崩：writer 持续慢时，drop_newest 把队列深度硬钉在上限 1024，**永不无界增长**，writer 不会被越来越长的队列压垮。
- 与替代策略对比：
  - **drop_oldest**：丢弃最旧记录、为新记录腾位。会让已排队的旧记录在最接近落盘时被踢出，**forensic 完整性更差**（丢失时间跨度最大的记录），且 writer 永远在追最新尾部，类似活锁。
  - **block（`put` 阻塞）**：会反向阻塞请求线程，违背"写路径与请求完全解耦"的硬约束（见 [§3.3](#33-单-writer-线程独占写入) 末尾的不允许项）。
  - **drop-all（清空队列）**：突发信息全丢，过于激进。
- access log 是 **best-effort 可观测性数据**，部分丢失可接受；过载时**保留有序前缀 > 保留最新尾部**（前缀覆盖更长的时间跨度，对事后排查更有价值）。

### 3.3 单 writer 线程独占写入

| 字段 | 值 |
|---|---|
| 线程数 | **1 个**后台 daemon 线程（进程级唯一）|
| 独占对象 | 文件句柄 `_current_fh`、日期状态 `_current_date`、open/write/flush/close |
| 应用线程职责 | 仅 `queue.put_nowait(line)`（非阻塞）|

**依据**：

- **消除并发 write 竞争**：当前 `DailyAccessHandler` 依赖 `logging.Handler` 的 per-handler lock 串行化 `emit`；改为单 writer 后，所有 `fh.write`/`fh.flush` 在单线程内串行，**无需任何锁**，无 GIL 下的 `write()` 竞争、无 fh 状态跨线程可见性问题。
- **跨天切换无锁、无 TOCTOU**：`_current_date != today` 判断 + `_close_current_fh` + `_open_file` 序列全部在 writer 线程内，单线程串行天然原子；不会出现"线程 A 切换 fh 中途、线程 B 看到半切换状态"的窗口。
- **请求路径零磁盘阻塞**：应用线程的 `emit` 退化为 `format + put_nowait`，`put_nowait` 仅短暂获取队列内部互斥锁检查容量（CPython `queue.Queue` 实现，微秒级），**永不等待磁盘 I/O**。磁盘抖动只影响 writer 线程的 `queue.get` 消费速率，反映为队列深度上升 → 触发 drop（见 [§3.2](#32-溢出策略--drop_newest)），而不是请求尾延迟。
- **daemon 属性**：writer 线程标记为 daemon，确保进程退出时不会被一个阻塞在 `queue.get` 的非 daemon 线程卡住；真正的 drain 由显式 shutdown 序列负责（见 [§3.5](#35-shutdown-drain-超时--5-秒) + [§6](#6-关闭顺序)）。

**硬约束（不允许项）**：

- **不允许无界 queue**（必须有 `maxsize`，见 [§3.1](#31-队列容量上限--1024-条)）。
- **不允许业务请求 await writer**（写路径与请求完全解耦；`emit` 必须在请求线程内同步返回，不 await 任何 future）。

### 3.4 丢弃告警 rate-limit = **60 秒**

| 字段 | 值 |
|---|---|
| 窗口 | **60 秒** |
| 触发事件 | `drop_newest`（队列满，见 [§3.2](#32-溢出策略--drop_newest)）|
| 节流语义 | 首次 drop **立即**告警；同一窗口（60s）内后续 drop **静默计数**，窗口结束时输出**一条 summary** |

**Summary 形态**（写入 **maintenance logger** `access_log.maintenance`，**不得**写入 access logger `oc_slimapi.access` 自身——否则 access logger 的告警会再次触发 emit → 递归）：

```
# 维护日志（人类可读，走 journald / stderr）
WARNING access_log.maintenance: access_log.drop_summary window_s=60 dropped=147 queue_depth=1024
```

（计数器字段 `dropped` 为本 60s 窗口内的 drop 计数；`queue_depth` 为窗口结束时的快照深度。）

**依据**：

- **防告警风暴**：writer 持续慢时，drop 速率可达每秒数千条；若每次 drop 都告警，会瞬间产生数千行 WARNING，淹没 journald、撑爆 jq 解析的日志文件——告警本身变成二次故障。
- **60s 窗口**对齐典型运维监控抓取间隔（Prometheus 15s/30s/60s scrape），且对人类运维足够稀疏、可读。
- **首条立即**保证单次/偶发 drop 不被窗口吞掉（单次抖动也要可见）。
- summary 含窗口内 drop 计数 → 运维能定量评估过载程度，而不是只看到"丢过"。

### 3.5 shutdown drain 超时 = **5 秒**

| 字段 | 值 |
|---|---|
| 超时 | **5 秒** |
| drain 范围 | shutdown 信号到达时已在队列中的全部记录 |
| 超时后行为 | 强制退出；残余未落盘记录计入 `dropped`，输出**最终 drop summary** |

**依据**：

- **与 systemd graceful shutdown 协调**：`deploy/oc-slimapi.service` 是 user-level systemd 单元，实际设置 `TimeoutStopSec=15`（L24，注释明言配合 uvicorn 5s graceful window）；uvicorn 配置 `timeout_graceful_shutdown=5.0`（`app.py` L76 `_GRACEFUL_SHUTDOWN_TIMEOUT`，L536）。access-log drain（≤5s）与活跃连接 drain 共享该 5s uvicorn graceful 窗口的预算。**推荐 access-log drain 先于连接 drain 执行**（通过 lifespan shutdown 顺序控制，确保日志落盘优先）。5s drain 在 systemd 15s 预算内可行：15s systemd 窗口内，access-log drain（5s）与连接 drain（5s）顺序执行后仍有约 5s 余量给后续 shutdown 步骤，不存在 85s 空余的错觉。
- **5s 是尽力而为的上限**：drain 实际完成时间 = `min(队列残余条数 × 单条 write+flush 延迟, 5s)`，假设单条 write/flush 延迟有界。正常情况远小于 5s；只有磁盘极慢或队列接近满（1024 条 × 数 ms flush ≈ 数秒）时才会逼近 5s。write 挂死场景（D-state / NFS hang）由 systemd `TimeoutStopSec=15` 的 SIGKILL 兜底（SIGTERM → 15s → SIGKILL）。
- **超时后丢弃是确定性降级**：access log 是 best-effort 数据，进程关闭时不值得为它无限延长关停；超时丢弃并计入 summary，保证"丢了多少"可见。
- **最终 drop summary**：drain 超时退出前，writer 线程输出一条最终 summary（残余计数），与 [§3.4](#34-丢弃告警-rate-limit--60-秒) 的 rate-limit summary 同形态，便于运维归因。

### 3.6 metrics（计数项，冻结）

> 本节为工程文档 L657 列出的计数项，本设计一并冻结（满足 `T7-C1` 的 metrics 要求）。计数项名称冻结，暴露方式（日志 vs `/slimapi/metrics.traffic` 扩展字段）属实施细节，见 [§7](#7-未决项--后续实施笔记)。

| 计数项 | 类型 | 语义 |
|---|---|---|
| `queued` | gauge | 当前队列深度（实时排队数）|
| `written` | counter | 累计成功写出（write+flush 成功）的记录数 |
| `dropped` | counter | 累计因队列满（drop_newest）+ shutdown drain 超时被丢弃的记录数 |
| `writeErrors` | counter | 累计 writer 线程内的写错误（`fh.write` / `fh.flush` / reopen 失败）次数 |
| `queueDepth` | gauge（高水位）| 峰值队列深度（进程生命周期内的 max observed）|

---

## 4. 与现有 DailyAccessHandler 的关系

### 4.1 职责迁移

`DailyAccessHandler`（`src/oc_slimapi/access_log.py` L110-202）当前承担两件事：

1. **格式化**（`self.format(record)` → JSON line 字符串）。
2. **文件写入**：跨天判断 / open / close fh / write / flush。

迁移后职责拆分：

| 职责 | 迁移前 | 迁移后 |
|---|---|---|
| `format(record)` | `emit` | **保留在 `emit`**（应用线程内执行）|
| 跨天判断 + fh 切换 | `emit` | **迁入 writer 线程** |
| 跨天切换的日期来源 | `record.created`（现有 emit docstring L176-179 明确用 `datetime.fromtimestamp(record.created).date()` 而非 `date.today()`） | **元组中的 `record_date`（源自 `record.created`）**，`emit` 入队前算好并随元组传递，writer 线程直接解包使用，不调用 `date.today()`。此设计继承了现有 emit 的语义，消除 `ts` 字段日期与文件名日期不一致的跨午夜窗口。 |
| `write` / `flush` | `emit` | **迁入 writer 线程** |
| 入队 + drop_newest + 告警 | —（不存在）| **新增到 `emit`** |
| `_current_fh` / `_current_date` 字段所有权 | `emit` 读写（受 Handler lock 保护）| **仅 writer 线程读写** |
| `current_path` 属性（被 maintenance 查询）| 同步可读 | 见 [§4.3](#43-current_path-的跨线程可读性) |

`emit` 退化后的形态（**设计语义，非已实现**）：

```python
def emit(self, record: logging.LogRecord) -> None:
    # 应用线程内执行; 永不阻塞, 永不触磁盘
    try:
        line = self.format(record)
        # record_date 取自 record.created（与现有 DailyAccessHandler.emit 一致），
        # writer 线程用它判断跨天切换，消除 ts 字段日期与文件名日期不一致的
        # 跨午夜窗口。详见 §4 职责迁移表"跨天切换日期来源"行。
        record_date = datetime.fromtimestamp(record.created).date()
        try:
            self._queue.put_nowait((record_date, line))
        except queue.Full:
            self._on_drop_newest(line)   # drop + 60s rate-limit 告警 + dropped++
    except Exception:
        self.handleError(record)
```

入队内容从 `line` 改为 `(record_date, line)` 元组后，writer 线程从 `queue.get()` 直接解包获得 `record_date`，无需对 JSON line 做额外解析即可判断跨天切换（[§2.1](#21-数据流) 架构图）。这一设计继承了现有 `DailyAccessHandler.emit` 用 `record.created` 而非 `date.today()` 判定目标文件的语义（docstring L176-179），消除 `ts` 字段日期与文件名日期不一致的跨午夜窗口。

### 4.2 setup_access_log 与线程生命周期

`setup_access_log`（L210-258）当前安装 `DailyAccessHandler` 实例。迁移后还需**启动 writer 线程**（或由 handler `__init__` 启动）。writer 线程的生命周期绑定到 handler：handler 销毁时触发 shutdown drain + join（见 [§6](#6-关闭顺序)）。

`_active_handler_ref`（L62）仍指向 handler 实例，maintenance（compress/prune）经它查询 `current_path` 以避让 live fh——这一契约**保持不变**。

### 4.3 `current_path` 的跨线程可读性

迁移后 `_current_fh` / `_current_date` 仅 writer 线程写。`current_path` 属性（L154-168，被 `compress_old_access_logs` L384-392 经 `_active_handler_ref` 读取）需跨线程读 → 必须保证读方看到的值是一致的快照（不能读到半切换状态）。

**约束**（实施必须满足，具体机制见 [§7](#7-未决项--后续实施笔记)）：

- writer 线程更新 `_current_date` / `_current_fh` 时，要么在一个轻量锁下发布，要么利用 Python attribute write 在 GIL 下的原子性（单字节码 `STORE_ATTR` 原子）。
- 读方（maintenance）经 `_MAINT_LOCK` 串行化 compress/prune，已与 setup 互斥；与 writer 线程的同步需显式约定（推荐：writer 线程持有一个独立轻量锁保护 `_current_date` 的发布，`current_path` 在同一锁下快照）。

> 这一同步细节属于实施层面；设计层只冻结"`current_path` 必须持续可被 maintenance 安全查询，不得返回半切换状态"这一不变量。

### 4.4 maintenance 循环不受影响

`run_access_log_maintenance_loop`（L574-620）的 compress / prune 经 `asyncio.to_thread` 在默认线程池跑，与 writer 线程是**两个独立线程**，经 `_MAINT_LOCK` + `_active_handler_ref.current_path` 避让。本设计**不改变** maintenance 的契约，只把"writer 线程的当前 fh"从"handler 同步 emit 持有"改为"writer 线程独占持有"。maintenance 侧只需保证 [§4.3](#43-current_path-的跨线程可读性) 的快照可读性。

---

## 5. 背压与降级

### 5.1 正常路径（背压未触发）

请求线程 → `emit` → `format` + `put_nowait`（命中队列有空位，O(1)）→ 立即返回。writer 线程消费速率 ≥ 入队速率，`queued` 长期接近 0，无 drop。

### 5.2 过载路径（背压触发）

writer 消费速率 < 入队速率 → 队列深度上升 → 触顶 1024 → 后续 `put_nowait` 抛 `queue.Full` → `drop_newest`：

1. 当前 line 被丢弃，`dropped++`。
2. 60s rate-limit 告警（[§3.4](#34-丢弃告警-rate-limit--60-秒)）：窗口首条立即告警，窗口内静默计数，窗口末输出 summary。
3. **请求线程不阻塞**，`emit` 正常返回，请求路径不受影响。

过载持续时：队列恒满（`queueDepth` 峰值 = 1024），`dropped` 持续上升，每 60s 一条 summary。运维据 summary 决定是否扩容 / 排查磁盘。

### 5.3 writer 线程异常降级

writer 线程在 `fh.write` / `fh.flush` / `open` 上抛异常（磁盘满 / 权限错 / IO 错）时：

- **单条错误**：catch → `writeErrors++` → 记录一条 warning（同样施加 60s rate-limit，与 drop 告警共用节流器或独立节流器，实施决定）→ **继续 loop**（不退出 writer 线程）。
- **fh 不可用**：尝试 reopen；reopen 失败 → 继续重试（每轮 loop 重试一次），`writeErrors` 持续累加，队列自然积压 → 触发 drop_newest。
- **推荐**：writer 线程**永不主动退出**（退出会让整个 access log pipeline 永久死亡，需进程重启才能恢复）。即使 fh 持续不可用，writer 也保持 loop，把过载转为 drop（`dropped` 计数），而不是让管道死亡。这与现有 best-effort 契约一致（`access_log.py` 模块文档 L19-22："Every write / compress / prune failure is caught, logged as a warning, and never propagated — the access log must never crash the application."）。
- **不回退到同步直写**：一旦回退，请求线程会再次同步等待磁盘，违背解耦目标。设计层**推荐不回退**；若实施层认为极端情况下需要回退，必须另开设计 bump（不在本设计冻结范围）。

### 5.4 init 失败

`setup_access_log` 当前在目录创建 / handler 安装失败时 disable logger（L251-257）。迁移后：writer 线程启动失败（极罕见，主要是线程创建资源限制）→ 同样 disable logger（`logger.disabled = True`），`write_access_log` 走 no-op 路径（`write_access_log` L292-293 已有 `if logger.disabled: return`）。**access log 失败永不 crash 启动**的契约保持不变。

---

## 6. 关闭顺序

进程关闭（lifespan shutdown / SIGTERM / `stop_event.set()`）时，按下列顺序停 writer 线程。**总预算 = drain ≤ 5s**（[§3.5](#35-shutdown-drain-超时--5-秒)）。

```
1. shutdown 信号到达 (app lifespan / SIGTERM)
        │
        ▼
2. 标记 writer 进入 shutdown: stop_accepting = True
   (后续 emit 的 put_nowait 直接 no-op + dropped++, 不再入队)
        │
        ▼
3. writer 线程切到 drain 模式:
   deadline = now() + 5s
   while queue not empty and now() < deadline:
       line = queue.get(timeout=...)
       跨天判断 + fh.write + fh.flush   (与正常路径相同)
       written++
        │
        ▼
4. 退出条件二选一:
   (a) queue 空 → drain 完成, 全部落盘
   (b) now() >= deadline (5s 到) → 残余 N 条未落盘
        │
        ▼
5. 若 (b): 残余 N 计入 dropped, 输出最终 drop summary
   (形态同 §3.4 的 rate-limit summary, 标记为 shutdown final)
        │
        ▼
6. writer 线程 close fh, 退出 loop, join
        │
        ▼
7. 进程继续后续 shutdown 步骤 (maintenance loop stop, opencode client close, ...)
```

**关键点**：

- 步骤 2 的 `stop_accepting` 保证 shutdown 期间新到达的请求不再追加队列（避免 drain 期间无限延长）；这些被拒入队的记录计入 `dropped`，与 drain 超时残余一同进 final summary。
- 步骤 3 的 drain 复用正常写入逻辑（跨天切换同样生效——若 shutdown 跨午夜，drain 仍能正确切 fh）。
- 步骤 4(b) 的 5s 是**挂钟时间**硬上限，不受单条 flush 延迟影响（即使单条 flush 卡 4s，剩余 1s 也用于尽力排空）。
- 步骤 6 的 `close fh` 与现有 `DailyAccessHandler.close` / `_close_current_fh`（L143-150, L199-202）一致，best-effort（close 异常被吞）。

---

## 7. 未决项 / 后续实施笔记

> 本节列出**实施层面**待定细节。**冻结 5 项（[§3.1–§3.5](#3-冻结设计决策)）+ metrics 计数项（[§3.6](#36-metrics计数项冻结)）均不属于未决项**，实施必须以冻结值为契约基线。

1. **Queue 具体类型**：推荐 stdlib `queue.Queue(maxsize=1024`）—— 无外部依赖、`put_nowait` / `get` 语义清晰、CPython 实现成熟。是否需要自定义队列（例如带高水位 hook）由实施决定。
2. **线程命名**：推荐 `"oc-slimapi-access-writer"`（便于 `py-spy` / `pthread_getname` 排查）。是否额外编号（多 worker 场景，本设计是单 writer，暂不需要）由实施决定。
3. **writer 线程启动 / join 的归属**：
   - 选项 A：`setup_access_log` 启动，handler 持有线程引用，`handler.close()` 触发 shutdown + join。
   - 选项 B：app lifespan（`app.py`）启动 / 关闭，handler 只暴露队列。
   - 选项 A 更内聚（handler 自包含），推荐 A；最终由实施决定。
4. **shutdown 信号传递机制**：经 `queue.put` 投递 sentinel（如 `None`）让 writer `get` 返回；还是经独立 `threading.Event` 标记 `stop_accepting` + writer 用 `queue.get(timeout=...)` 轮询 Event？实施决定（sentinel 更简单，Event 更明确——两者都能满足 [§6](#6-关闭顺序) 的顺序契约）。
5. **`current_path` 跨线程快照机制**（[§4.3](#43-current_path-的跨线程可读性) 约束）：推荐 writer 线程持有一个轻量 `threading.Lock`，在更新 `_current_date` / `_current_fh` 时持锁，`current_path` 读方在同一锁下快照。是否复用 `_setup_lock` 还是新增独立锁，由实施决定。
6. **与 `run_access_log_maintenance_loop` 的交互验证**：migration 后需回归测试 `compress_old_access_logs` 的 `_active_handler_ref.current_path` 避让逻辑（L384-421）在 writer 线程独占 fh 下仍正确（即 maintenance 不会 unlink writer 正在写的 .jsonl）。这是实施计划必备的回归用例，**不是**设计层冻结项。
7. **metrics 暴露方式**：`queued` / `written` / `dropped` / `writeErrors` / `queueDepth` 计数项名称已冻结（[§3.6](#36-metrics计数项冻结)）；暴露到 `access_log.maintenance` 日志、还是扩展 `/slimapi/metrics.traffic`（见 AGENTS.md）的响应字段、还是两者，属实施决定。
8. **drop 告警与 writeError 告警是否共用节流器**：两者都是"过载类"告警，可共用一个 60s 节流器，也可独立。实施决定。
9. **回归测试矩阵**（实施需覆盖，非设计冻结）：
   - `put_nowait` 命中满队列 → drop_newest + `dropped++` + 节流告警。
   - writer 单条 `fh.write` 抛异常 → `writeErrors++` + 继续loop。
   - shutdown drain 在队列空时立即返回（< 5s）。
   - shutdown drain 在队列满 + 慢盘下 5s 超时 → final summary + 残余计入 `dropped`。
   - 跨天切换在 writer 线程内正确（`ts` 字段日期 == 文件名日期）。
    - `current_path` 在 writer 线程切换 fh 中途被 maintenance 读取 → 返回一致快照（不返回半切换）。
10. **风险与回滚路径**（实施参考，非设计冻结）：
    - **原理风险**：队列 + 单 writer 引入的 drop 路径对用户透明，运维可能未察觉 access log 缺失。缓解：每 60s drop summary + 最终 shutdown summary 使 drop 可见。
    - **回滚路径**：若实施后生产中发现不可接受的数据丢失或尾延迟恶化，回滚到当前同步直写形态：在相关 PR 中保留 `DailyAccessHandler.emit` 的现有同步写入逻辑，切换 `write_access_log` 是否经 writer 走由 feature flag 控制（如环境变量 `OC_SLIMAPI_ACCESS_LOG_ASYNC_WRITER`，缺省启用）；回滚时仅设 flag 为 `false` + 重启即可，无需代码变更。此回滚路径对应 `db/migrate` 的 reversible 模式——实施时 **必须在同一个 PR 内同时包含 forward 实现与 flag 回退机制**，确保不依赖后续修补。
    - **回滚范围**：回滚后 access log 回到 Task 6 单调用同步直写形态（blocking write + flush，无队列/drop），解释度无损失但引入阻塞回归（同 [§1.2](#12-task-6-的局限) 第 2 点）。回滚**不影响**其他子系统。

---

## 8. 不在本计划范围内

**本设计文档仅为 Task 7 的设计产出，未实施任何代码改动。** 具体：

- **未修改**任何 `.py` 文件（`src/oc_slimapi/access_log.py` 的 `DailyAccessHandler.emit` / `setup_access_log` / `write_access_log` / maintenance 循环**保持现状**）。
- **未修改**任何测试文件（`tests/test_access_log.py` 保持现状）。
- **未修改**任何其它文档（`docs/operations.md` / `docs/specs/INTERFACE_MAP.md` / `CHANGELOG.md` 等保持现状）。
- **未修改** `docs/implementation-batches-2026-08-09.md`（本设计的来源批次文档）。

实施本设计（让 writer 线程真正落地）需**另开实施计划**，且必须：

1. 以本文件 [§3](#3-冻结设计决策) 的 5 项冻结值 + [§3.6](#36-metrics计数项冻结) 计数项为契约基线。
2. 经 reviewer 放行（对应 `T7-C3` reviewer gate）。
3. 覆盖 [§7](#7-未决项--后续实施笔记) 第 9 项的回归测试矩阵。
4. 走 `docs/release.md` 的发版流程（若涉及 wire / 运维行为变更，记入 `CHANGELOG.md`）。

本设计文档不构成对 access log 当前运行行为的任何承诺——当前 `DailyAccessHandler.emit` 仍是 Task 6 之后的**同步、best-effort、单调用行写入**形态。
