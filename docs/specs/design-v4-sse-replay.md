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
| 2026-08-17 rev-1 | 评审修复（rev-sgpt FAIL 后 R2）：全局流单序列（per-directory 域废除，全实例单连接策展流事实）；epoch 四类输入唯一语义（旧合法 epoch → resync{epoch_changed}；future/非法/跨端点 → 忽略重置）；错误体扁平化（仓库 CodedHTTPException 约定）；tombstone 序列语义（replay 复用既有 `message.removed` 帧形，seq 无空洞） |
| 2026-08-17 rev-2 | 评审修复 R3：epoch 改**随机 boot nonce**（16 hex，非墙钟无序不比较——墙钟回拨/碰撞不可靠；判定 = epoch ≠ 当前 → 一律 resync{epoch_changed}，弃 future-epoch 区分）；ID 编入**域标签**（`g:<epoch>:<seq>` / `t:<sid>:<epoch>:<seq>`——无标签时跨端点域不可判定）；REPLAY-010 跨 epoch 字典序断言废除（nonce 无序，改为分段断言 + resync 界标） |
| 2026-08-17 rev-3 | 评审修复 R4：§2.3 ID 语法与 §3.2 统一（域标签单一语法）；**上游断连恢复状态机补全**（两端点表行 + §3.5 触发条件冻结 + REPLAY-014——fanout resync{reconnect_no_replay}，epoch 不变 seq 不重置，禁 replay log 补上游缺口）；**Last-Event-ID 分类优先级冻结**（语法→域→epoch→seq 严格短路序 + REPLAY-015 组合输入）；search 规范化扩至 HTTP 降级路径（第四消费点，hash 输入 = trim 后转义前精确化） |
| 2026-08-17 rev-4 | 评审修复 R5：**上游断连持久 barrier**（§3.4——每受影响域日志写 low-watermark，Last-Event-ID 不晚于水位即拦截（本行原文"早于"，边界经 rev-5 勘误为 **≤**）→ resync{reconnect_no_replay}，防离线客户端跨缺口静默重放 + REPLAY-017）；**触发时点勘误**（首次确认 loss 即触发——EOF `global_hub.py:894-904`/异常 `:913-922` 为主，成功重连 `:847-863` 仅兜底，非"恢复成功才触发"）；REPLAY 重编号（心跳复归 014，新增 015/016/017，共 17 条）；REPLAY-016 旧 epoch 分支补 meta 恒首帧断言 |
| 2026-08-17 rev-5 | 评审修复 R6：**barrier 边界 off-by-one 勘误**（判定 = seq **≤** 水位一律拦截——水位本身对应的帧亦发布于缺口前，客户端不知情；REPLAY-017 边界三连断言：水位-1/水位/水位+1）；**写入范围冻结**（全局域 + 当前 epoch 内全部已创建 per-sid 域，不限在线订阅者 + REPLAY-018 token 离线变体）；**保留生命周期冻结**（不受 count/bytes/TTL 逐出；仅窗口下界严格越过后可删；域回收保留失效水位/fail-safe resync；进程重启归 epoch_changed）；措辞定位 = S-B01④提案内冻结口径（随④待 owner 终裁）；REPLAY 计数 17→18 |
| 2026-08-17 owner 终裁 | **S-B01 四项全部裁决（omni-orch 通知）**：②meta 重连语义、③token ID 作用域（per-sid + 全局流单序列）、④两端点状态机逐帧序列表（含上游断连 barrier、四条通用不变量、REPLAY-001~018）**按本稿 §2.2-2.4 提案原文全部通过冻结**；resync reason 值域冻结 = `epoch_changed`/`replay_expired`/`replay_gap`/`reconnect_no_replay`；v4-contract §7 收敛冻结记录已同步 |
| 2026-08-18 rev-6 收紧（B3b-5） | rev-6 PASS-with-notes 3 MINOR notes 落档：①§3.4「seq > 水位」分支补全 future 语义边界（future → 忽略重置按首连，先于窗口判定）；②REPLAY-017 水位+1 分支显式「仅补发 102、不重复 101」（replay 区间半开 `(lastID, 窗口尾]`）；③域回收 fail-safe resync reason 显式冻结 = `reconnect_no_replay`（两种内部策略 wire 结果一致）。均为措辞收紧/边界明确化，**已裁协议语义零变更**（实现 `sse/replay_log.py` B3b-1/2 已按此语义落地） |

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

> 来源：refactor-plan §8.1 问题 5，四项裁决未收敛则 B0 不出门、`sseReplay:true` 不得进 v4 capability。**S-B01 四项已全部 owner 终裁（2026-08-17）**：①tokens=1 禁止复用（400）；②③④按本稿 §2.2-2.4 提案原文冻结。

### 2.1 ① tokens=1 统一流 —— [已裁决，owner 2026-08-17]

**裁决：v4 禁止复用 `/events?tokens=1` 统一流。** `/events?tokens=1` 在 v4 返回 400，错误码 `tokens_stream_retired_in_v4`；token 流必须走独立 `/sessions/{sid}/stream`。

**理由**：

1. **单 Last-Event-ID 无法恢复双序列**：统一流内全局控制帧（digest/q/p）与 token 帧混排，若纳入同一序列，则 digest 合并节拍（250ms）与 token 节拍（~100ms L2-A）不同频，重连后单 Last-Event-ID 无法同时表达「全局帧已收到 N、token 帧已收到 M」两个游标。
2. **meta-first 与重放顺序结构性矛盾**：v3 冻结 meta 恒首帧（行 168）。若统一流在重连时先发新 meta（新 seq）再发旧 replay 帧，则线上 ID 倒退（replay 帧 seq < meta 帧 seq）——违反单调性协议不变量。
3. **webui / ocdroid 本就分离两连接**（`/slimapi/events` 控制面 + `/stream` token 面），禁统一流不新增任何额外连接成本；两连接各自独立 ID 域（§3.2），互不干扰。

**落地**：v4 路由层 `events.py` 对 `tokens == "1"` 直接 400 `tokens_stream_retired_in_v4`（v3 行为在 v3 契约冻结，v4 才拦截）；`INTERFACE_MAP` 同步记录。

### 2.2 ② meta 重连语义 —— [已裁决，owner 2026-08-17]

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

### 2.3 ③ token ID 作用域 —— [已裁决，owner 2026-08-17]

**提案：token 流 per-sid 序列（每 sid 独立计数）；全局流 `/events` = 该全局输出流自身的单一序列（一个 epoch 一个 seq 计数器，全实例所有策展帧共序）。**

> 编排者实证（2026-08-17 rev-1）：`/slimapi/events` 是**单连接全实例策展流**（不按 directory 绑定连接）——per-directory ID 域在物理上不可实现，全局流单一序列为自然选择。

| 候选 | 决策 | 论证 / 否决理由 |
|---|---|---|
| **per-sid（token 流）** | **选定** | token 流端点天然绑定 sid；seq 只由该 sid 的帧推进，空洞只可能来自日志逐出（真实丢帧）→ gap 判定干净（逐出 vs 缺席可区分）；`Last-Event-ID` 直接映射「该 sid 看到哪」 |
| **全局流单序列（`/events`）** | **选定** | 单连接全实例流 = 无 directory 绑定 → 单一 seq 计数器即该输出流自身的线序；全实例策展帧（digest/q/p/error）共序、严格单调；无跨目录重复 ID、无单连接 seq 乱序；`seqBase` 单一值可表达；gap 判定干净（该流无跨域消费，空洞只来自逐出） |
| **per-directory（全局流）** | 否决 | 与全实例单连接事实冲突：①单连接内跨 directory 帧会重复 ID（每 directory 各从 1 起）；②单连接 seq 不单调（多目录交错）；③`Last-Event-ID` 无 directory 信息，无法定位「哪个域看到哪」；④`seqBase` 无法表达多域。属不可实现设计 |
| 全局跨端点统一序列 | 否决 | token 流独立端点独立域已裁（§2.1-1 双序列矛盾同理）；全局流单序列仅覆盖 `/events` 自身，不与 `/stream` 共享 |
| 每连接序列 | 否决 | 重连后 seq 归零，`Last-Event-ID` 无法跨连接定位（重开 APP = 新连接 = 无法回放），重放失去意义 |

**ID 语法（冻结，与 §3.2 同一）**：`id: g:<epoch>:<seq>`（全局流）；`id: t:<sid>:<epoch>:<seq>`（token 流）——域标签 + epoch（16 hex 随机 boot nonce）+ seq（纯十进制）。全局流与 token 流独立 ID 域：全局流 = `(epoch, 全实例单序列 seq)`；token 流 = `(epoch, per-sid seq)`。客户端不得跨流混用 `Last-Event-ID`（域标签使跨端点/跨 sid 混用可机械判定，§3.2）。

### 2.4 ④ 两端点状态机逐帧序列表 —— [已裁决，owner 2026-08-17]

**状态机：`CONNECTING → ESTABLISHED → (RECONNECT → REPLAYING → ESTABLISHED)`；resync 为终止性转移（进入 `RESYNCED` 全量路径）。**

#### 全局流 `GET /slimapi/events?v=4`

| 场景 | 状态转移 | 逐帧序列（严格序） | 说明 |
|---|---|---|---|
| 首连（无 Last-Event-ID） | CONNECTING → ESTABLISHED | `meta`（无 id，seqBase=当前）→ 业务帧（seq 从 seqBase+1 递增）→ heartbeat（无 id） | 无重放 |
| 重连（Last-Event-ID 在日志窗口内，epoch 匹配） | CONNECTING → REPLAYING → ESTABLISHED | `meta`（无 id）→ replay 帧（seq = lastID+1 … 窗口尾，严格递增，全部带 `id:`）→ 新帧（seq 继续）→ heartbeat | **补帧路径**，无 gap 无 resync |
| 重连（旧 epoch：格式合法且 epoch ≠ 当前，进程重启场景——随机 nonce 无序，不比较大小） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id，epoch=新）→ `resync{reason:"epoch_changed"}`（无 id）→ 新帧 | 旧 epoch 全部失效（重启即换 epoch）→ resync 提示，客户端走全量对齐 |
| 重连（Last-Event-ID 过期：早于日志窗口起点） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id）→ `resync{reason:"replay_expired"}`（无 id）→ 新帧 | 缺帧太多，补帧成本 ≈ 全量 → resync |
| 重连（future：同 epoch 且 seq > 当前已发布 max） | CONNECTING → ESTABLISHED | 忽略该 ID（视为无 Last-Event-ID）→ `meta`（无 id）→ 新帧 | future ID 无法确认 → 忽略重置，客户端去重自担 |
| 重连（格式非法 / 跨端点域） | CONNECTING → ESTABLISHED | 忽略该 ID（视为无 Last-Event-ID）→ `meta`（无 id）→ 新帧 | 非法/跨域无法解析 → 忽略重置，同 future 路径 |
| 重连（窗口内缺 seq = gap，如日志逐出边缘） | CONNECTING → RESYNCED → ESTABLISHED | `meta`（无 id）→ `resync{reason:"replay_gap"}`（无 id）→ 新帧 | gap 无法以补帧缝合（缺公开发布记录）→ resync；客户端 resync 后**自行 HTTP 全量拉取**（snapshot 为客户端动作，服务端不发 snapshot 帧） |
| 背压溢出（subscriber 队列满，帧被丢弃未送达） | ESTABLISHED →（客户端断连）→ 重连 | 溢出帧**已入重放日志**（§3.5，记录「已发布帧」）→ 重连后走补帧路径恢复 | 溢出丢帧正是重放日志的用途；不断连则客户端自按 ID 去重 |
| **上游断连后恢复（rev-3 补，rev-4 勘误触发时点）** | ESTABLISHED → RESYNCED → ESTABLISHED（存量连接，无重连） | `resync{reason:"reconnect_no_replay"}`（无 id，fanout 全部存量订阅者）→ 恢复后新帧（seq 继续） | **v3 现行为延续**（`global_hub.py:825` fanout 帧形锚点）；**触发时点 = 首次确认上游 loss 即触发**（EOF 路径 `global_hub.py:894-904` / 异常路径 `:913-922`，带 `_upstream_loss_notified` 防重；成功重连路径 `:847-863` 仅作未通知时兜底——rev-4 勘误：非"恢复成功才触发"）；断连期间 sidecar 观察不到上游事件 → replay log 不含缺口帧 + **日志已写 barrier（§3.4）** → 必须 resync 提示客户端 HTTP 全量对齐；恢复时该 sid 的 pending live 缓冲清空（重订阅重建）；**epoch 不变**（sidecar 未重启，日志仍有效）；seq 不重置（新帧继续单调） |
| subscriber 溢出（新接入超限） | —（开流前） | 503 `{code:"subscriber_capacity_exceeded", limit, current}` + `Retry-After` | 同 v3 冻结行为（events.py:47-55），非重放场景 |

#### token 流 `GET /slimapi/sessions/{sid}/stream?v=4`（tokens 恒 true）

| 场景 | 状态转移 | 逐帧序列（严格序） | 说明 |
|---|---|---|---|
| 首连（无 Last-Event-ID） | CONNECTING → ESTABLISHED | `meta`（无 id，seqBase=当前）→ token 帧（seq 递增）→ heartbeat | 无重放 |
| 重连（Last-Event-ID 在窗口内，epoch 匹配） | CONNECTING → REPLAYING → ESTABLISHED | `meta`（无 id）→ replay token 帧（seq = lastID+1 … 窗口尾）→ 新 token 帧 → heartbeat | 补帧；已撤销消息（tombstone）以既有 `message.removed` 帧形照常回放（带 `id:`，seq 连续）——见 §3.5 tombstone 裁决 |
| 重连（旧 epoch / 过期 / gap） | CONNECTING → RESYNCED → ESTABLISHED | `meta` → `resync{reason:"epoch_changed"|"replay_expired"|"replay_gap"}` → 新 token 帧 | 同全局流语义（epoch_changed 仅限旧 epoch 场景） |
| **上游断连后恢复（rev-3 补）** | ESTABLISHED → RESYNCED → ESTABLISHED（存量连接，无重连） | `resync{reason:"reconnect_no_replay"}`（无 id，fanout 至全部存量订阅者）→ 恢复后新 token 帧（seq 继续） | **v3 现行为延续**（`tokenstream/hub.py:1896-1900` 对每有订阅者的 sid fanout）；断连期间 sidecar 观察不到上游事件 → replay log 不含缺口帧 + **日志已写 barrier（§3.4）** → **必须 resync** 提示客户端 HTTP 全量对齐；恢复时该 sid 的 pending live 缓冲清空（重订阅重建）；**epoch 不变**（sidecar 未重启，日志仍有效——resync 表「上游侧有缺口」非「日志失效」）；seq 不重置（新帧继续单调） |
| `/stream` 上 tokens=1 语义 | CONNECTING | 恒 true（v3 冻结） | 不适用 400（400 仅限 `/events?tokens=1`，§2.1） |

**通用不变量（两端点强制，测试断言点）**：①meta 恒首帧（v3 行 168 冻结延续）；②带 `id:` 的帧按 (epoch, seq) 严格单调不减；③无 `id:` 的帧（meta/resync/heartbeat）不参与序列；④replay 帧序列内不插入任何新帧。

---

## 3. 协议主体设计

### 3.1 进程 epoch + 单调 seq 生成规则

- **epoch**：进程启动时生成的**随机 boot nonce**（16 hex，如 `a3f1c09d7b2e48f5`；os.urandom 截断），**非墙钟、无序、不比较大小**（rev-2：unixtime_ms 依赖时钟严格单调——NTP 回拨/VM 恢复会使新进程 epoch 小于旧进程，真实旧 epoch 被误判 future；同毫秒重启可碰撞）。**重启必换**（随机 64bit 碰撞概率可忽略；进程退出即失效；日志窗口按 epoch 整体失效、不跨 epoch 复用）。判定规则冻结：**`epoch ≠ 当前 epoch` 一律 = 旧世界 → `resync{epoch_changed}`**（不做 future-epoch 区分——随机 nonce 下"大于当前的 epoch"与"旧 epoch"不可区分且无须区分，两者都是重启前世界，统一 resync 安全且幂等）。
- **seq**：u64 单调递增，按 ID 域独立计数，从 1 起：**全局流 `/events` = 全实例单计数器**（该输出流自身线序）；**token 流 = 每 sid 一计数器**。发布侧在帧入重放日志时分配（分配先于送达，因此断连后按日志补帧 seq 连续无空洞）。
- **分配点**：`global_hub.publish()` / token 域发布路径（`token_hub`）在帧「已发布」时分配 seq 并写入重放日志——**日志记录的是已发布帧，而非已送达帧**（§3.5 背压裁决）。

### 3.2 ID 语法（冻结）与独立 ID 域

```
id: g:<epoch>:<seq>              # 全局流 /events（g = global 域标签）
id: t:<sid>:<epoch>:<seq>        # token 流 /sessions/{sid}/stream（t = token 域标签 + sid）
```

- **域标签编入 ID**（rev-2：无标签时两域可同时产生 `epoch:5`，"跨端点域 ID 无效"不可判定——服务端无法识别 ID 来自另一端点）。`g`/`t` 前缀 + token 域含 sid → 服务端可机械判定：①前缀非 `g`/`t` → 格式非法；②`g:` ID 到达 `/stream` 或 `t:` ID 到达 `/events` → 跨端点域；③`t:<sid>` 与请求路径 sid 不符 → 跨 sid 域——三者一律忽略 + 重置（按首连）。
- 两端点各自独立 ID 域：`/events` 域 = **全实例单序列**；`/stream` 域 = **sid 级序列**（域键 = sid，编入 ID）。
- 客户端 `Last-Event-ID` 请求头携带完整域标签 ID，服务端按上述三条机械判定；跨端点/跨 sid 混用属客户端协议违约，结果定义为忽略重置（不报错、不 resync）。
- 禁止在 v3 端点使用本协议（v3 无 id 帧，行为不变）。

### 3.3 帧类型与 ID 分配

| 帧类型 | 事件 | 带 `id:`？ | 理由 |
|---|---|---|---|
| 业务帧 | digest / q/p / error / token 帧 | **带** | 重放主体；入重放日志（§3.5） |
| meta | `slimapi.meta` | 不带 | 连接级协商帧，不进序列（2.2） |
| heartbeat | `heartbeat` | 不带 | v3 冻结保活帧，不参与重放（日志不记录 heartbeat） |
| resync | `resync` | 不带 | 终止性提示帧（客户端转全量），无续读意义 |

### 3.4 有界重放日志（v4 新建组件 `sse/replay_log.py`）

**三维上限 + 环形覆盖**（按 ID 域分窗：全局流单窗 + 每 sid 一窗；进程内存、非落盘）：

| 维度 | 默认上限 | 语义 |
|---|---|---|
| count | 2048 帧/域 | 每 ID 域环形窗口条目数上限 |
| bytes | 64 MiB（全进程总账） | 帧字节总量上限（防大 digest/大 token 帧撑爆） |
| TTL | 15 min/帧 | 帧入日志时间 + TTL，到期逐出（滑窗） |

- **覆盖策略**：环形缓冲，逐出最旧；任一维度触顶即逐出最旧帧，直至低于阈值。逐出后对应 seq 区间不可补 → 客户端 Last-Event-ID 落此区间 = 过期 → resync（2.4 表格）。
- **上游断连 barrier（S-B01④已裁决冻结，owner 2026-08-17；low-watermark 数据结构为实现细节）**：sidecar 检测到上游 loss 时，除 fanout 存量订阅者外，写入不可跨越的 barrier（low-watermark = 断连时刻该域已发布 max seq；进程内存**日志元数据，非帧**）。**写入范围（受影响域全集）**：全局域 + 当前 epoch 内已创建的**全部** per-sid 域——**不限于断连时有在线订阅者的 sid**（离线 token 客户端同样须被拦截）。**重放判定增补一条（优先级在 ④窗口判定内）**：Last-Event-ID 的 seq **≤ 本域最近 barrier 水位** → 一律 `resync{reason:"reconnect_no_replay"}`——水位本身对应的帧亦发布于缺口之前，持有该 ID 的客户端不知情，**等于水位同样拦截**（不得补发 barrier 后窗口内帧）；seq **> 水位** → 走完整第④级分类：**future（同 epoch 且 seq > 该域已发布 max）→ 忽略 + 重置按首连**（§3.5 表——future 判定先于窗口判定，非「窗口/过期/gap」的输入；越过 barrier 并不豁免 future 语义边界，rev-6 MINOR① 收紧注记）；否则正常窗口/过期/gap 判定。barrier 前后之间存在 sidecar 未观察到的上游事件缺口，**禁止跨 barrier 补帧**（窗口内连续不构成补帧依据）。**保留生命周期（不作为普通帧逐出）**：barrier 不受 count/bytes/TTL 三维逐出；仅当日志窗口下界已**严格越过** barrier 水位（此后所有 seq ≤ barrier 的 cursor 必被 `replay_expired` 拦截，barrier 判定冗余）方可删除；per-sid 域对象因零订阅者回收时，须**保留失效水位**（或使同 epoch 旧 cursor fail-safe 进 resync——**reason 显式冻结 = `reconnect_no_replay`**：两种内部策略（「保留失效水位」按水位拦截 / 「回收即 fail-safe」无条件拦截）的 wire 结果一致，客户端不可区分；rev-6 MINOR③ 收紧注记——**不得**视作普通首连/空日志放行）；进程重启 = epoch 更换，旧 epoch cursor 由 `epoch_changed` 拦截（barrier 随进程消亡无影响）。上游恢复后新帧继续单调（seq 不重置，barrier 不消耗 seq）；多轮断连 → 每轮各写一 barrier（水位单调递增，判定只看最近一个即可）。
- **与现有组件边界（明确）**：
  - GlobalHub pending（250ms debounce）＝**发布前**的合并窗口（digest 合并节拍）——不属于重放日志，v4 不把 pending 当日志；
  - tombstone 队列（`TOKEN_REMOVED_MESSAGES` cap 1000 / TTL 24h，`config.py:72-73`）＝已撤销消息索引（`message.removed` tombstone，`tokenstream/hub.py:277`）——是**既有 token 域重放机制的配套索引**，与 v4 重放日志并存不混用：重放日志管「帧已公开发布 + seq 分配」，tombstone 管「哪些消息已撤销须以 `message.removed` 帧回放」（§3.5 tombstone 裁决）；
  - **v4 新建组件**：重放日志另行实现（环形 deque + 元数据），不并入 pending/tombstone 数据结构。

### 3.5 expired / future / gap 处理与背压必答

**Last-Event-ID 四类输入拆分（rev-1 冻结唯一语义，2026-08-17）**：

**分类优先级（rev-3 冻结，严格按序短路）**：①完整语法校验（域标签 + epoch 16hex + seq 十进制）→ ②端点标签与路径 sid 校验（`g:` 只属 `/events`；`t:` 只属 `/stream` 且 sid 须匹配路径）→ ③epoch 比对（仅对**已通过 ②的正确域 ID** 进行）→ ④seq/窗口比对（仅同 epoch）。**组合输入按最先命中者短路**：`t:<other-sid>:<旧epoch>:5` 到 `/events` = ②跨端点域命中 → 忽略重置（**不再**走 ③epoch_changed——跨域 ID 的 epoch 字段对本端点无意义）；`g:<旧epoch>:5` 到 `/events` = ②过 ③命中 → resync{epoch_changed}。

| 情况 | 判定 | 行为 |
|---|---|---|
| **旧 epoch**（格式合法、epoch ≠ 当前，进程重启场景——随机 nonce 无序不比较） | epoch 解析 ≠ 当前 epoch | `resync{reason:"epoch_changed"}` + 新帧（2.4）——客户端按新 epoch 全量对齐 |
| Last-Event-ID 过期（同 epoch，seq < 日志窗口起点） | 窗口起点 > lastID | `resync{reason:"replay_expired"}` + 新帧（2.4） |
| Last-Event-ID future（同 epoch，seq > 当前已发布 max） | 无法确认存在过的帧 | 忽略 + 重置（按无 Last-Event-ID 处理），新帧继续；客户端按 (epoch,seq) 去重自担 |
| 格式非法 / 跨端点域 | 无法解析或端点域不匹配 | 忽略 + 重置（同 future 路径） |
| gap（窗口内缺 seq，非自然逐出的单点/区间缺失） | 窗口内 `lastID+1` 帧不存在或区间不连续 | `resync{reason:"replay_gap"}` + 新帧；**客户端 resync 后自行 HTTP 全量拉取（如 `/slimapi/sessions` 首屏 / token 域重拉消息投影）——snapshot 是客户端动作，服务端不发 snapshot 帧**（v2.2 行 153 "snapshot" 措辞注解为客户端全量获取行为） |
| **上游断连后恢复**（sidecar↔`/global/event` 断开期间漏观察上游事件；v3 现行为延续 rev-3 冻结、rev-4 勘误触发时点） | 非 Last-Event-ID 输入——**首次确认上游 loss 即触发**（EOF `global_hub.py:894-904` / 异常 `:913-922`，`_upstream_loss_notified` 防重；成功重连 `:847-863` 仅未通知时兜底） | `resync{reason:"reconnect_no_replay"}`（无 id，fanout 全部存量订阅者）→ 恢复后新帧；**epoch 不变、seq 不重置**；**同时在每受影响域日志写 barrier（§3.4——离线客户端后续重连按 barrier 判定 resync，防跨缺口静默重放）**；token 域另清空该 sid 的 pending live 缓冲（重订阅重建，`tokenstream/hub.py:1896-1900` v3 锚点）；客户端按 v3 语义 HTTP 全量对齐。**禁止**依赖 replay log 补上游断连缺口——日志只录「sidecar 已发布帧」，不含断连期间上游事件 |

**tombstone 裁决（rev-1，REPLAY-012）**：token 流重放遇已撤销消息（`message.removed` tombstone）时，**照常消耗该 seq 并发送既有 `message.removed` 轻量撤销帧（保留 `id:`）**——复用既有 wire 帧形（`tokenstream/frames.py:137-151`：`event: message.removed` + `{"sessionID","messageID"}`），**ID 序列无空洞**；客户端收到撤销帧即丢弃该消息缓存。否决"跳过内容仅消耗 seq"：会造成 window 内单点 seq 缺口，客户端无法区分"受控空洞"与真实 gap（判定歧义），且与不变量②（(epoch,seq) 严格单调、无空洞的强形式）冲突。否决"跳过且 seq 留洞"：引入"客户端不得视为 gap"的新协议规则，弱化 gap 判定且与门槛④不变量相悖。

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
| REPLAY-001 首连无重放 | v4 server 已启动；全局流已发布 seq=5 | 新连接 `GET /events?v=4` 无 Last-Event-ID | `meta`（无 id，seqBase=5）→ seq=6… 新帧 → heartbeat | meta 首帧；首业务帧 seq=seqBase+1；meta 无 `id:` |
| REPLAY-002 窗口内补帧 | seq 已发布至 10；Last-Event-ID=`g:<epoch>:6` | 重连携带该 ID | `meta` → replay seq=7,8,9,10（全带 id）→ 新帧 seq=11 | replay 严格升序连续；replay 区间 = (6,10]；无 resync；线序 meta→replay→新帧 |
| REPLAY-003 旧 epoch | 进程重启（epoch 更换）；Last-Event-ID=旧合法 `g:<旧epoch>:42` | 重连携带旧 epoch ID | `meta`（epoch=新）→ `resync{epoch_changed}` → 新帧 | resync 帧在 meta 后、新帧前；旧 ID 不触发任何补帧 |
| REPLAY-004 窗口过期 | Last-Event-ID seq 早于日志窗口起点（已逐出） | 重连携带该 ID | `meta` → `resync{replay_expired}` → 新帧 | resync 语义命中；无补帧尝试 |
| REPLAY-005 future/非法 ID | max_seq=10；Last-Event-ID=`g:<epoch>:99`（seq future）或 `g:<epoch>:abc`（非法）或 `t:<sid>:<epoch>:5` 持到 `/events`（跨端点域） | 重连携带该 ID | 忽略该 ID → `meta` → 新帧 seq=11 | 无 resync；视同首连；不报错 |
| REPLAY-006 gap 检测 | 日志逐出边缘造成窗口内 seq 区间缺失 | 重连携带 lastID = gap 前最后 seq | `meta` → `resync{replay_gap}` → 新帧（服务端不发 snapshot 帧） | gap 走 resync 而非补帧；断言客户端随后自行 HTTP 全量拉取（snapshot 为客户端动作） |
| REPLAY-007 背压溢出恢复 | 订阅者队列满，帧 20-25 未送达但已发布入日志 | 客户端断连，重连 Last-Event-ID=19 | `meta` → replay seq=20…25 → 新帧 | 溢出帧由日志补回（已发布非已送达语义）；无内容丢失 |
| REPLAY-008 背压超窗口 | 溢出量 > 窗口 → 最旧帧被逐出 | 重连 Last-Event-ID = 被逐出区间内 | `meta` → `resync{replay_expired}` → 新帧 | 超窗口回退 resync 全量 |
| REPLAY-009 tokens=1 v4 400（全局流） | `GET /events?v=4&tokens=1` | v4 请求 tokens=1 | **400** JSON `{"code":"tokens_stream_retired_in_v4","hint":"token 流请使用 /slimapi/sessions/{sid}/stream"}` 开流前（普通 JSON，非 SSE；扁平错误体，仓库 CodedHTTPException 约定） | 状态码 400；`code` 精确匹配；`hint` 存在；无任何 SSE 帧发出 |
| REPLAY-010 ID 无倒退断言 | 连续 3 次重连（含一次进程重启 epoch 更换） | 全程录制所有带 id 帧 | 同一 epoch 内同域 seq 严格单调递增；**跨 epoch 不比较**（随机 nonce 无序——重启后新 epoch 新序列从 1 起算，客户端以 meta.epoch 变更 + resync{epoch_changed} 为界切换序号世界） | 断言：每段（同 epoch 同域）内 id 序列无倒退；epoch 切换处必有 resync{epoch_changed}；新段首业务帧 seq 从 seqBase+1 起 |
| REPLAY-011 双流 ID 域独立 | 同一 sid 同时开 `/events?v=4` 与 `/stream?v=4` | 两流各自发布事件 | `/events` 帧 id 按**全局单序列**递增；`/stream` 帧 id 按 **sid 域**递增；两域 seq 互不干扰（同一 sid 事件在两流各占自己计数器） | 单域单序列；跨流无 seq 竞争 |
| REPLAY-012 token 重放含 tombstone | sid 内消息 M 被撤销（`message.removed` 入 `TOKEN_REMOVED_MESSAGES`） | 断连（seq=N，M 帧已发布）→ 重连 Last-Event-ID=N-1 | `meta` → replay 帧自 N 起；M 帧以 `message.removed` 轻量撤销帧回放（**保留 `id:`，seq 无空洞**）→ 新帧 | 已撤销消息以撤销帧回放（客户端丢弃缓存）；seq 仍连续（无 gap 判定） |
| REPLAY-013 meta additive 不破坏 v3 | v3 客户端（无 v4 协商）正常连接 | 同域发布事件 | v3 帧形零变化（无 id:、无 capabilities 字段）；meta 仍为 `subscriberId`+`tokens` | 冻结回归：v3 契约 §7 行 167-168 逐条断言 |
| REPLAY-014 心跳无 ID | 长连接静默 15s | 观察心跳帧 | heartbeat 帧无 `id:` | 心跳不占序列、不入日志 |
| REPLAY-015 上游断连恢复（在线订阅者，rev-3 补） | 订阅者已 ESTABLISHED；sidecar↔上游 `/global/event` 断开数秒后恢复（断连期间上游有新事件） | 存量订阅连接不断开 | `resync{reason:"reconnect_no_replay"}`（无 id）fanout 到达全部存量订阅者（**触发时点 = 首次确认 loss**，EOF/异常路径即发；恢复连接仅为未通知时兜底）→ 恢复后新帧（带 id，seq 继续单调，不重置）；epoch 不变 | resync 帧先于恢复后首个新业务帧；断连缺口**不**经 replay log 补（日志无此帧）；**barrier 已写入日志（断言日志状态）**；客户端随后 HTTP 全量对齐（断言发起）；token 域同场景另断言该 sid pending live 缓冲清空 |
| REPLAY-016 组合输入分类优先级（rev-3 补） | `/events` 收到 `t:<other-sid>:<旧epoch>:5`（跨端点域 + 旧 epoch 组合）；对照 `/events` 收到 `g:<旧epoch>:5`（纯旧 epoch） | 两连接分别携带 | 前者 = 忽略重置（②端点域校验先命中短路，不走 epoch_changed）→ `meta` → 新帧；后者 = `meta` → `resync{epoch_changed}` → 新帧（**meta 恒首帧**，meta/resync 均无 id——不变量①） | 优先级冻结断言：语法→域→epoch→seq 严格短路序；组合输入不再触发 resync；旧 epoch 分支 meta 首帧在场 |
| REPLAY-017 barrier 拦截离线客户端（rev-4 补，rev-5 边界细化；全局域） | 客户端收到 `g:E:100` 后断开（离线）；sidecar↔上游断连（barrier 水位=100）；恢复后发布 `g:E:101`、`g:E:102`（仍同 epoch 同窗口内） | 三连接分别携带 `Last-Event-ID: g:E:99` / `g:E:100` / `g:E:101` 重连 | 前两者 = `meta` → `resync{reason:"reconnect_no_replay"}`（无 id）→ 新帧（seq=103 起），**不得**补发 101/102（seq **≤** 水位一律拦截）；第三者 = 正常窗口判定（seq > 水位且 101/102 在窗口内 → 补发续流；**replay 区间 = (101, 102] 半开——仅补发 102，不重复 101**：Last-Event-ID 本身对应的帧已由客户端持有，inclusive 回放会造成线上重复；rev-6 MINOR② 收紧注记） | 边界三连断言：水位-1 拦截 / **水位本身拦截**（off-by-one 回归锚，rev-5）/ 水位+1 放行走窗口（**且补发集不含 lastID=101 帧——半开区间断言**）；barrier 优先于窗口判定（§3.4） |
| REPLAY-018 barrier 拦截离线 token 客户端（rev-5 补；per-sid 域变体） | sid=S 曾有订阅者后全部离线（最后收至 `t:S:E:50`）；sidecar↔上游断连时 **S 无在线订阅者**（barrier 仍须写入 S 域，水位=50）；恢复后 S 产生新帧 51/52 | 客户端携带 `Last-Event-ID: t:S:E:50` 重连 `/sessions/S/stream` | `meta` → `resync{reason:"reconnect_no_replay"}`（无 id）→ 新帧（seq=53 起）；**不得**补发 51/52 | 受影响域全集断言：无在线订阅者的 per-sid 域同样写 barrier（§3.4 写入范围）；域对象若已回收 → 保留失效水位或 fail-safe resync（**reason = `reconnect_no_replay`，与保留失效水位判定同 wire 结果，rev-6 MINOR③**），不得按空日志首连放行 |

---

## 5. 待裁决清单（B0 出门评审项）

| 编号 | 事项 | 状态 |
|---|---|---|
| 1 | S-B01 ② meta 重连语义（2.2 提案） | **已裁决（owner，2026-08-17，按 §2.2 提案原文冻结）** |
| 2 | S-B01 ③ token ID 作用域 = per-sid + 全局流单序列（2.3 提案，rev-1 修订） | **已裁决（owner，2026-08-17，按 §2.3 提案原文冻结）** |
| 3 | S-B01 ④ 两端点状态机逐帧序列表（2.4 提案，含通用不变量；rev-1 统一 epoch 四类输入语义） | **已裁决（owner，2026-08-17，按 §2.4 提案原文冻结——含上游断连 barrier 机制与 REPLAY-001~018）** |
| 4 | 重放日志三维默认上限（count 2048/域、bytes 64MiB、TTL 15min）与覆盖策略 | 设计提案值，可随实现调参（不进 wire） |
| 5 | 日志窗口内 gap 语义（REPLAY-006）在真实的「逐出-补帧」并发边界是否可能出现误判 | 实现期验证，若发现不可达可降级为防御分支（不违背 wire 语义） |

**发现的问题（预登记）**：

- v2.2 行 153「snapshot」措辞注解为**客户端动作**（resync 后客户端自行 HTTP 全量拉取，服务端不发 snapshot 帧）——rev-1 已在 §2.4/§3.5 统一；
- v2.2 行 153 未定义「gap 的严格判定算法」（窗口内缺失即 gap，但逐出是批量最旧优先，单个窗口内出现非尾部缺口的唯一途径是逐出与发布并发——见待裁决 5）；
- `design-token-stream.md` 与 v3-contract §7 对 `resync` 帧的 reason 值域未冻结（现行实现仅 `reconnect_no_replay`）——v4 新增 reason（`epoch_changed`/`replay_expired`/`replay_gap`）为加性扩展，需在 v4-contract §7 冻结值域；不涉及 v3 帧形变更。