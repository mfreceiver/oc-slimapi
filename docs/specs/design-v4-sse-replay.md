# 设计方案：SSE 重放协议（v4）

> **v4 设计稿（B0-3）**：本文件定义 v4 SSE 重放协议（`id:` + `Last-Event-ID` 恢复）的完整设计。**现行 wire 契约以 `docs/specs/v3-contract.md` §7 为准（帧名帧形冻结，v2.2 行 153）**；本设计仅在 v4 生效。
>
> 状态：**设计稿 v4（B0 批）— 协议设计**
> 性质：**v4-only 加性 wire 行为**（v4 版本协商后启用；v3 帧名帧形零变化；落地时 `sseReplay` 能力键随实现同批广告，B3b）
> 关联：契约 `docs/specs/v4-contract.md` §7（随本设计定稿）；实现 `src/oc_slimapi/sse/replay_log.py`（v4 新建组件）+ `src/oc_slimapi/routes/events.py`、`src/oc_slimapi/routes/token_stream.py`、`src/oc_slimapi/sse/global_hub.py`（v4 扩展）；权威基准 `docs/system-architecture-proposal-2026-08-17.md`（v2.2）

## 修订记录

| 版本 | 内容 |
|---|---|
| v1（本稿） | B0-3 协议设计稿；含 S-B01 四项协议裁决门槛（①已裁决，②③④设计提案待 owner 裁决） |

---

## 1. 背景与基准

### 1.1 骨干事实（v2.2 引用）

- **v4-only**：SSE `id:` / `Last-Event-ID` 重放为 v4 引入的能力，v3 契约 §7（行 165-170）冻结「帧名帧形零变化 + Last-Event-ID 无重放 API」（行 153、v3-contract §7 行 167-168）。重放协议只在 v4 生效，落地时 `sseReplay` 能力键与实现同批广告。
- **现有 meta 帧已实现**（行 154）：`slimapi.meta` 首帧（`subscriberId` + `tokens`）双端点都有（`token_stream.py:187-192`、`events.py:78-82/91-94`）——v4 仅 additive 扩展（capabilities 摘要 + epoch/seq 基线字段），v3 形状不动。
- **现有 GlobalHub pending（250ms debounce）与 tombstone 队列不是 replay log**（行 153）：pending 是发布前合并窗口，tombstone 是已撤销消息索引——v4 新建独立有界环形重放日志组件 `sse/replay_log.py`，与既有 token 域重放队列（`TOKEN_REMOVED_MESSAGES` cap 1000 / TTL 24h，`config.py:72-73`）**并存不混用**。
- **静态能力键**（行 140）：`sseReplay` 为 v4 存在即广告的静态键，不随 DB 抖动；瞬态可用性走 503 + health 扩展字段。

### 1.2 协议目标 / 非目标

**目标**：SSE 断连重连后按 `Last-Event-ID` 增量恢复，不丢帧、不重复、ID 单调不倒退；缺口可判定（区分「消费者缺席 seq」vs「日志逐出」）；重连成本最小化（优先补帧，必要时 resync 全量）。

**非目标**：不做消息级 durable 持久化（重放日志有界内存，非落盘）；不做幂等消费协议（客户端按 ID 去重自行负责）；不替换现有 `/since` / `/sessions` 真值（resync → 客户端走现有全量路径）；不改 v3 帧名帧形。

---

## 2. S-B01 协议裁决门槛（B0 出门 gate）

> 来源：refactor-plan §8.1 问题 5，四项裁决未收敛则 B0 不出门、`sseReplay:true` 不得进 v4 capability。①为已裁决项；②③④为本稿设计提案，**待 owner 裁决**。

### 2.1 ① tokens=1 统一流 —— [已裁决，owner 2026-08-17]

**裁决：v4 禁止复用 `/events?tokens=1` 统一流。** `/events?tokens=1` 在 v4 返回 400，错误码 `tokens_stream_retired_in_v4`；token 流必须走独立 `/sessions/{sid}/stream`。

**理由**：

1. **单 Last-Event-ID 无法恢复双序列**：统一流内全局控制帧（digest/q/p）与 token 帧混排，若纳入同一序列，则 digest 合并节拍（250ms）与 token 节拍（~100ms L2-A）不同频，重连后单 Last-Event-ID 无法同时表达「全局帧已收到 N、token 帧已收到 M」两个游标。
2. **meta-first 与重放顺序结构性矛盾**：v3 冻结 meta 恒首帧（行 168）。若统一流在重连时先发新 meta（新 seq）再发旧 replay 帧，则线上 ID 倒退（replay 帧 seq < meta 帧 seq）——违反单调性协议不变量。
3. **webui / ocdroid 本就分离两连接**（`/slimapi/events` 控制面 + `/stream` token 面），禁统一流不新增任何额外连接成本；两连接各自独立 ID 域（§3.2），互不干扰。

**落地**：v4 路由层 `events.py` 对 `tokens == "1"` 直接 400 `tokens_stream_retired_in_v4`（v3 行为在 v3 契约冻结，v4 才拦截）；`INTERFACE_MAP` 同步记录。

### 2.2 ② meta 重连语义 —— [设计提案，待 owner 裁决]

**提案：重连后 meta 帧不带 `id:`；epoch 不随 SSE 重连更换；线序 = meta（无 ID）→ replay 帧（seq 严格递增）→ 新帧（seq 继续）。**

| 子项 | 提案 | 论证 | 备选与否决理由 |
|---|---|---|---|
| meta 帧是否带 `id:` | **不带**。meta 是连接级协商帧（v3 行 168 冻结：`subscriberId`/`tokens`），非业务事件，不参与重放序列 | meta 恒首帧若分配当前序列 seq，紧随的 replay 帧（旧 seq）必然导致线上 ID 倒退（同 2.1-2）；meta 无 id 则线序 = 首帧 announce → 旧 seq replay → 新 seq 继续，全程单调 | 备选：meta 分配「序列起点」seq。否决：meta 与业务帧语义层级不同，混入同一 seq 域使序号含义混乱、日志窗口计数失真 |
| epoch 是否更换 | **不随重连更换**。epoch = 进程级（启动时生成，§3.1），仅进程重启更换 | 同进程内重连，历史帧与日志窗口仍有效，epoch 更换会使所有旧 Last-Event-ID 失效 → 浪费窗口内可补帧 | 备选：每次重连换 epoch。否决：丢失窗口内补帧能力，重连必 resync，与重放目标相悖 |
| 线序定义 | **严格 meta → replay 帧 → 新帧**，全程 `(epoch, seq)` 单调不减（无 ID 倒退不变量） | 客户端按 (epoch,seq) 校验可检测一切乱序/倒退；重放帧前不插任何新帧 | 备选：meta 后先新帧再补 replay。否决：新帧 seq 已超越 replay 帧 → 倒退 |

**meta v4 扩展字段（additive，v3 字段不变）**：

```
event: slimapi.meta
data: {
  "subscriberId": "<id>",        # v3 冻结语义不变
  "tokens": <bool>,              # v3 冻结语义不变
  "capabilities": { "sseReplay": true },   # v4 新增：本流支持重放
  "epoch": "<epoch>",            # v4 新增：进程 epoch（§3.1）
  "seqBase": <max_seq>           # v4 新增：本域当前已发布最大 seq（重连基线）
}
```

### 2.3 ③ token ID 作用域 —— [设计提案，待 owner 裁决]

**提案：per-sid 序列 `<epoch>-<seq>`，每 sid 独立计数；全局流 `/events` 侧对称采用 per-directory 域（论证同构）。**

| 候选 | 决策 | 论证 / 否决理由 |
|---|---|---|
| **per-sid（token 流）** | **选定** | seq 只由该 sid 的帧推进，空洞只可能来自日志逐出（真实丢帧）→ gap 判定干净；`Last-Event-ID` 直接映射「该 sid 看到哪」；与 `/stream` 端点天然匹配（订阅即绑 sid） |
| **per-directory（全局流 `/events`）** | **选定**（对称应用于全局流） | 全局流服务端已按 directory 过滤（v3 §5.6）；seq 按 directory 域分配，digest/q/p 帧在同一 directory 域内单调，跨 directory 事件不污染他域序列 |
| 全局序列 | 否决 | 其他 sid/directory 的事件帧占用全局 seq → 特定订阅者出现「合法空洞」（消费者缺席 seq），gap 判定无法区分缺席 vs 逐出 → 误 resync 风暴或漏判（2.1-1 同理） |
| 每连接序列 | 否决 | 重连后 seq 归零，`Last-Event-ID` 无法跨连接定位（重开 APP = 新连接 = 无法回放），重放失去意义 |

**ID 语法（冻结）**：`id: <epoch>:<seq>`（冒号分隔，纯十进制数字）。全局流与 token 流独立 ID 域：全局流 = `(epoch, per-directory seq)`；token 流 = `(epoch, per-sid seq)`。客户端不得跨流混用 `Last-Event-ID`（不同流 ID 域不相交）。

### 2.4 ④ 两端点状态机逐帧序列表 —— [设计提案，待 owner 裁决]

**状态机：`CONNECTING → ESTABLISHED → (RECONNECT → REPLAYING → ESTABLISHED)`；resync 为终止性转移（进入 `RESYNCED` 全量路径）。**

#### 全局流 `GET /slimapi/events?v=4`

| 场景 | 状态转移 | 逐帧序列（严格序） | 说明 |
|---|---|---|---|
| 首连（无 Last-Event-ID） | CONNECTING → ESTABLISHED | `meta`（无 id，seqBase=当前）→ 业务帧（seq 从 seqBase+1 递增）→ heartbeat（无 id） | 无重放 |
| 重连（Last-Event-ID 在日志窗口内，epoch 匹配） | CONNECTING → REPLAYING → ESTABLISHED | `meta`（无 id）→ replay 帧（seq = lastID+1 … 窗口尾，严格递增，全部带 `id:`）→ 新帧（seq 继续）→ heartbeat | **补帧路径**，无 gap 无 resync |
| 重连（Last-Event-ID epoch 不匹配） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id，epoch=新）→ `resync{reason:"epoch_changed"}`（无 id）→ 新帧 | 旧 epoch 全部失效 → resync 提示，客户端走全量 |
| 重连（Last-Event-ID 过期：早于日志窗口起点） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id）→ `resync{reason:"replay_expired"}`（无 id）→ 新帧 | 缺帧太多，补帧成本 ≈ 全量 → resync |
| 重连（Last-Event-ID future：seq > 当前已发布最大 seq） | CONNECTING → ESTABLISHED | 忽略该 ID（视为无 Last-Event-ID）→ `meta`（无 id）→ 新帧 | future ID 无法确认 → 忽略重置，客户端去重自担 |
| 重连（窗口内缺 seq = gap，如日志逐出边缘） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id）→ `resync{reason:"replay_gap"}`（无 id）→ 新帧 | gap 无法以补帧缝合（缺公开发布记录）→ resync+snapshot 提示 |
| 背压溢出（subscriber 队列满，帧被丢弃未送达） | ESTABLISHED →（客户端断连）→ 重连 | 溢出帧**已入重放日志**（§3.5，记录「已发布帧」）→ 重连后走补帧路径恢复 | 溢出丢帧正是重放日志的用途；不断连则客户端自按 ID 去重 |
| subscriber 溢出（新接入超限） | —（开流前） | 503 `{code:"subscriber_capacity_exceeded", limit, current}` + `Retry-After` | 同 v3 冻结行为（events.py:47-55），非重放场景 |

#### token 流 `GET /slimapi/sessions/{sid}/stream?v=4`（tokens 恒 true）

| 场景 | 状态转移 | 逐帧序列（严格序） | 说明 |
|---|---|---|---|
| 首连（无 Last-Event-ID） | CONNECTING → ESTABLISHED | `meta`（无 id，seqBase=当前）→ token 帧（seq 递增）→ heartbeat | 无重放 |
| 重连（Last-Event-ID 在窗口内，epoch 匹配） | CONNECTING → REPLAYING → ESTABLISHED | `meta`（无 id）→ replay token 帧（seq = lastID+1 … 窗口尾）→ 新 token 帧 → heartbeat | 补帧；tombstone 帧（消息已撤销）跳过不发（借 `TOKEN_REMOVED_MESSAGES`，§3.5） |
| 重连（epoch 不匹配 / 过期 / gap） | CONNECTING → RESYNCED → ESTABLISHED | `meta` → `resync{reason:"epoch_changed"|"replay_expired"|"replay_gap"}` → 新 token 帧 | 同全局流语义 |
| `/stream` 上 tokens=1 语义 | CONNECTING | 恒 true（v3 冻结） | 不适用 400（400 仅限 `/events?tokens=1`，§2.1） |

**通用不变量（两端点强制，测试断言点）**：①meta 恒首帧（v3 行 168 冻结延续）；②带 `id:` 的帧按 (epoch, seq) 严格单调不减；③无 `id:` 的帧（meta/resync/heartbeat）不参与序列；④replay 帧序列内不插入任何新帧。

---

## 3. 协议主体设计

### 3.1 进程 epoch + 单调 seq 生成规则

- **epoch**：进程启动时生成，格式 = `unixtime_ms`（13 位十进制），例 `1755500000000`。同一毫秒双重启动以启动顺序自增后缀 ±0 防撞（进程级单点生成，串行递增）。**重启必换**（进程退出即失效；日志窗口按 epoch 整体失效、不跨 epoch 复用）。
- **seq**：u64 单调递增，per 域（directory / sid）独立计数，从 1 起。发布侧在帧入重放日志时分配（分配先于送达，因此断连后按日志补帧 seq 连续无空洞）。
- **分配点**：`global_hub.publish()` / token 域发布路径（`token_hub`）在帧「已发布」时分配 seq 并写入重放日志——**日志记录的是已发布帧，而非已送达帧**（§3.5 背压裁决）。

### 3.2 ID 语法（冻结）与独立 ID 域

```
id: <epoch>:<seq>         # 例：id: 1755500000000:42
```

- 两端点（`/events` 与 `/stream`）各自独立 ID 域：`/events` 域键 = `directory`；`/stream` 域键 = `sid`。同一 epoch 下两域 seq 空间不相交。
- 客户端 `Last-Event-ID` 请求头携带 `<epoch>:<seq>`，服务端按**当前端点的域**解析（跨域 ID 视为 future/无效，忽略重置）。
- 禁止在 v3 端点使用本协议（v3 无 id 帧，行为不变）。

### 3.3 帧类型与 ID 分配

| 帧类型 | 事件 | 带 `id:`？ | 理由 |
|---|---|---|---|
| 业务帧 | digest / q/p / error / token 帧 | **带** | 重放主体；入重放日志（§3.5） |
| meta | `slimapi.meta` | 不带 | 连接级协商帧，不进序列（2.2） |
| heartbeat | `heartbeat` | 不带 | v3 冻结保活帧，不参与重放（日志不记录 heartbeat） |
| resync | `resync` | 不带 | 终止性提示帧（客户端转全量），无续读意义 |

### 3.4 有界重放日志（v4 新建组件 `sse/replay_log.py`）

**三维上限 + 环形覆盖**（per 域独立窗口，进程内存、非落盘）：

| 维度 | 默认上限 | 语义 |
|---|---|---|
| count | 2048 帧/域 | 每域环形窗口条目数上限 |
| bytes | 64 MiB（全进程总账） | 帧字节总量上限（防大 digest/大 token 帧撑爆） |
| TTL | 15 min/帧 | 帧入日志时间 + TTL，到期逐出（滑窗） |

- **覆盖策略**：环形缓冲，逐出最旧；任一维度触顶即逐出最旧帧，直至低于阈值。逐出后对应 seq 区间不可补 → 客户端 Last-Event-ID 落此区间 = 过期 → resync（2.4 表格）。
- **与现有组件边界（明确）**：
  - GlobalHub pending（250ms debounce）＝**发布前**的合并窗口（digest 合并节拍）——不属于重放日志，v4 不把 pending 当日志；
  - tombstone 队列（`TOKEN_REMOVED_MESSAGES` cap 1000 / TTL 24h，`config.py:72-73`）＝已撤销消息索引，供 token 重放跳帧——是**既有 token 域重放机制的配套索引**，与 v4 重放日志并存不混用：重放日志管「帧已公开发布 + seq 连续」，tombstone 管「其中哪些消息已被撤销须跳过」；
  - **v4 新建组件**：重放日志另行实现（环形 deque + 元数据），不并入 pending/tombstone 数据结构。

### 3.5 expired / future / gap 处理与背压必答

| 情况 | 判定 | 行为 |
|---|---|---|
| Last-Event-ID 过期（seq < 日志窗口起点） | 窗口起点 > lastID | `resync{reason:"replay_expired"}` + 新帧（2.4） |
| Last-Event-ID future（seq > 当前已发布 max，或 epoch 不匹配 / 跨域 / 格式非法） | 无法确认存在过的帧 | 忽略 + 重置（按无 Last-Event-ID 处理），新帧继续；客户端按 (epoch,seq) 去重自担 |
| gap（窗口内缺 seq，非自然逐出的单点/区间缺失） | 窗口内 `lastID+1` 帧不存在或区间不连续 | `resync{reason:"replay_gap"}` + 新帧，客户端转 snapshot（全量路径） |

**背压必答题（B0-3 工单必答）**：

1. **溢出帧是否入重放日志？——入**。重放日志记录「**已发布帧**」而非「已送达帧」：subscriber 队列满（`DEFAULT_SSE_QUEUE_ITEMS=256` / `DEFAULT_SSE_BUFFER_BYTES=2MiB`）丢的是送达，不是发布；溢出丢帧正是重放日志要解决的场景（订阅者断连重连后由日志补回）。
2. **断连后 gap 由重放日志补还是 resync 全量？——缺口在日志窗口内 → 补帧；超出窗口 → resync**。补帧成本 < resync 全量成本时补帧（窗口内、epoch 匹配、无 gap）；窗口外/epoch 过期/检测到无法缝合的 gap → resync 全量（2.4 表格各行已列）。

### 3.6 与 meta-first 顺序 & 能力键时序

- **meta 恒首帧**（v3 行 168 冻结延续）：任何重放/新帧之前必有 meta；v4 meta additive 扩展 `capabilities`/`epoch`/`seqBase`（2.2）。客户端以 meta 为首帧建立基线，其后帧按线序校验。
- **能力键时序（引用 refactor-plan §4.1）**：`sseReplay` 与 `qpImmediateFull` 均**与实现同批启用**——B3a 的 `capabilities["4"] = {globalSessions, auxiliaryFilters}`（B3a-A3）**不广告** `sseReplay`/`qpImmediateFull`；B3b 实现落地（重放协议 + q/p 帧补全/直投确认）同一批在 `capabilities["4"]` 追加广告（§2.7 B3b-5、§4.1 时序表）。探测不到键的客户端继续 v=3 行为，不受影响。

---

## 4. 协议矩阵用例表（落地于 B3b 测试）

> 矩阵用例将落为 B3b 的协议测试（SSE 帧级断言）。前置/动作/期望帧序列/断言点四列；断言点即测试断言代码锚。

| 用例 ID | 前置条件 | 动作 | 期望帧序列 | 断言点 |
|---|---|---|---|---|
| REPLAY-001 首连无重放 | v4 server 已启动；domain 有已发布 seq=5 | 新连接 `GET /events?v=4` 无 Last-Event-ID | `meta`（无 id，seqBase=5）→ seq=6… 新帧 → heartbeat | meta 首帧；首业务帧 seq=seqBase+1；meta 无 `id:` |
| REPLAY-002 窗口内补帧 | seq 已发布至 10；Last-Event-ID=`epoch:6` | 重连携带该 ID | `meta` → replay seq=7,8,9,10（全带 id）→ 新帧 seq=11 | replay 严格升序连续；replay 区间 = (6,10]；无 resync；线序 meta→replay→新帧 |
| REPLAY-003 epoch 不匹配 | 进程重启（epoch 更换）；Last-Event-ID=旧 epoch:42 | 重连携带旧 epoch ID | `meta`（epoch=新）→ `resync{epoch_changed}` → 新帧 | resync 帧在 meta 后、新帧前；旧 ID 不触发任何补帧 |
| REPLAY-004 窗口过期 | Last-Event-ID seq 早于日志窗口起点（已逐出） | 重连携带该 ID | `meta` → `resync{replay_expired}` → 新帧 | resync 语义命中；无补帧尝试 |
| REPLAY-005 future ID | max_seq=10；Last-Event-ID=`epoch:99` 或跨域 ID | 重连携带 future ID | 忽略该 ID → `meta` → 新帧 seq=11 | 无 resync；视同首连；不报错 |
| REPLAY-006 gap 检测 | 日志逐出边缘造成窗口内 seq 区间缺失 | 重连携带 lastID = gap 前最后 seq | `meta` → `resync{replay_gap}` → 新帧 | gap 走 resync 而非补帧；snapshot 提示 |
| REPLAY-007 背压溢出恢复 | 订阅者队列满，帧 20-25 未送达但已发布入日志 | 客户端断连，重连 Last-Event-ID=19 | `meta` → replay seq=20…25 → 新帧 | 溢出帧由日志补回（已发布非已送达语义）；无内容丢失 |
| REPLAY-008 背压超窗口 | 溢出量 > 窗口 → 最旧帧被逐出 | 重连 Last-Event-ID = 被逐出区间内 | `meta` → `resync{replay_expired}` → 新帧 | 超窗口回退 resync 全量 |
| REPLAY-009 tokens=1 v4 400（全局流） | `GET /events?v=4&tokens=1` | v4 请求 tokens=1 | **400** JSON `{error:{code:"tokens_stream_retired_in_v4"}}` 开流前（普通 JSON，非 SSE） | 状态码 400；错误码精确匹配；无任何 SSE 帧发出 |
| REPLAY-010 ID 无倒退断言 | 连续 3 次重连（含一次 epoch 更换） | 全程录制所有带 id 帧 | 全体 (epoch, seq) 字典序严格单调不减；epoch 更换后旧 seq 区间不与他域复用 | 断言所有 id: 排序无倒退（协议不变量） |
| REPLAY-011 双流 ID 域独立 | 同一 sid 同时开 `/events?v=4` 与 `/stream?v=4` | 两流各自发布事件 | `/events` 帧 id 按 directory 域递增；`/stream` 帧 id 按 sid 域递增；两域 seq 互不干扰 | 单域单序列；跨流无 seq 竞争 |
| REPLAY-012 token 重放跳过 tombstone | sid 内消息 M 被撤销（入 `TOKEN_REMOVED_MESSAGES`） | 断连（seq=N，M 帧已发布）→ 重连 Last-Event-ID=N-1 | `meta` → replay 帧自 N 起、**跳过 M 帧**（不发其内容，seq 连续推进）→ 新帧 | 已撤销消息不重放；seq 仍连续（无 gap 判定） |
| REPLAY-013 meta additive 不破坏 v3 | v3 客户端（无 v4 协商）正常连接 | 同域发布事件 | v3 帧形零变化（无 id:、无 capabilities 字段）；meta 仍为 `subscriberId`+`tokens` | 冻结回归：v3 契约 §7 行 167-168 逐条断言 |
| REPLAY-014 心跳无 ID | 长连接静默 15s | 观察心跳帧 | heartbeat 帧无 `id:` | 心跳不占序列、不入日志 |

---

## 5. 待裁决清单（B0 出门评审项）

| 编号 | 事项 | 状态 |
|---|---|---|
| 1 | S-B01 ② meta 重连语义（2.2 提案） | 待 owner 裁决 |
| 2 | S-B01 ③ token ID 作用域 = per-sid（+全局流 per-directory 对称应用）（2.3 提案） | 待 owner 裁决 |
| 3 | S-B01 ④ 两端点状态机逐帧序列表（2.4 提案，含通用不变量） | 待 owner 裁决 |
| 4 | 重放日志三维默认上限（count 2048/域、bytes 64MiB、TTL 15min）与覆盖策略 | 设计提案值，可随实现调参（不进 wire） |
| 5 | 日志窗口内 gap 语义（REPLAY-006）在真实的「逐出-补帧」并发边界是否可能出现误判 | 实现期验证，若发现不可达可降级为防御分支（不违背 wire 语义） |

**发现的问题（预登记）**：

- v2.2 行 153 未定义「gap 的严格判定算法」（窗口内缺失即 gap，但逐出是批量最旧优先，单个窗口内出现非尾部缺口的唯一途径是逐出与发布并发——见待裁决 5）；
- `design-token-stream.md` 与 v3-contract §7 对 `resync` 帧的 reason 值域未冻结（现行实现仅 `reconnect_no_replay`）——v4 新增 reason（`epoch_changed`/`replay_expired`/`replay_gap`）为加性扩展，需在 v4-contract §7 冻结值域；不涉及 v3 帧形变更。