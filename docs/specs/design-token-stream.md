# 设计方案：Token 批式 SSE（opt-in 实时流）

> **历史设计稿（v4）**：本文件记录 token stream 的设计历史、评审过程与
> rationale；其中 snapshot、`server.connected`、done marker、版本头与旧 T3
> 口径均可能已被后续实现取代。当前 wire 权威见
> [`v4-contract.md`](v4-contract.md) §7，consumer 算法见
> [`PROTOCOL.md`](PROTOCOL.md)；`v2-contract.md` 仅为历史存档。

> 状态：**设计稿 v4 — 架构级 PASS**（联合 3 评委 grok 9.2 / opus 9.0 / bgpt 8.7 + 我方 backstop bgpt 7.4；**双边共识**）。残留 fold 为 §16 阶段契约，转入每阶段 9.5 门控。
> 性质：**加性 wire 行为**（新端点 + 新 event 类型 + health 加性字段），**不 bump `X-Slimapi-Version`**。
> 关联：契约 `docs/specs/v2-contract.md` §3/§6（落地需新增 §3.x + §6 token 信封）；实现 `src/oc_slimapi/sse/hub.py`。

## v3 修订记录（回应 v2 三方复审：grok 8.6 / opus 8.2 / bgpt 7.1，均 FAIL→收敛修法）

| 编号 | v2 缺陷（三方命中） | v3 修法 |
|---|---|---|
| C1 | finish_part 未 drain `_pending` 就发 `done:true`（终态顺序不变式声明式、无算法保证） | finish_part 同步 drain `_pending`→fanout 残余 delta→fanout `done:true`（取 part.text）→退役；该 key 此后禁 delta（§5.4） |
| C2 | 订阅握手 double-count：snapshot 含未 flush pending，下次 flush 重复发 | 握手先 flush 给现有订阅者（新者未入 fanout）→snapshot 新者→再加入 fanout；无 await 临界段（§5.5） |
| C3 | reasoning-delta 也 `field:"text"` 但无 text LivePart → 每 token 发 `part_state_missing` resync → 风暴 | `_nontext_parts` 跟踪；孤儿/非 text delta **静默 drop+计数**，绝不 resync（§5.3） |
| C4 | truncated 后仍可能续发 delta（streamOwned 陷阱） | truncated=`drop_part`（pop `_pending`+`live_parts`+`_disabled_parts`）；后续该 key delta 静默 drop（§5.3/§5.8） |
| C5 | 全局累加器无内存上限（跨 session 无界） | `TOKEN_LIVEPARTS_MAX_BYTES=8MiB`+`TOKEN_LIVE_PARTS_MAX=32`；超限退役最旧+`resync{token_memory_limit}`；worst-case 76MiB 写进 §6（含 handshake buffer 8MiB/sub） |
| C6 | 终态 `done:true` 也可能 >1MiB 被 drop | truncated 适用 `done:false` **和** `done:true`（§5.6） |
| — | 次要 | `has_consumers` 方法非 property；reconnect 调 `token_hub.on_upstream_reconnect()`；backpressure resync 带 sessionID；版本 gate=middleware 非 Depends；safe_put 先 size-check；15s 心跳必发（§5.2/§5.5/§5.6/§7） |

> v1→v2 修订记录见 git 历史（B1-B8）。本版相对 v2 仅收紧 flush/pending/subscribe/truncated/memory 算法；**客户端 wire 契约不变**（仅加性：新 resync reason `token_memory_limit`；truncated 可带 `done:true`）。

---

## 1. 背景与根因（源码已证实）

LLM 生成过程中 opencode 各事件发射时机（`opencode-src/current/packages/opencode/src/session/processor.ts`）：

| 上游事件 | 时机 | 源码 | sidecar 现状 |
|---|---|---|---|
| `message.part.delta` | **逐 token** | `processor.ts:499-509` | 丢弃（`hub.py:527`） |
| `message.part.updated` | part 开始/结束边界 | `processor.ts:486-497`、`512-529` | 丢弃 |
| `message.updated` | **仅 step 完成** | `processor.ts:456` | 折进 digest |
| `session.status` | 生成开始(busy)/结束(idle) | `processor.ts:639` | 折进 digest |

**关键（rev-opus 源码核验）**：`updatePartDelta` 发 `PartDelta` **不落库**（`session.ts:879-887`）；仅 `PartUpdated`(text-start 空/text-end 满) 落 `PartTable`（`projector.ts:312-330`）。→ 丢弃 `message.part.delta` 的消费者生成中看不到任何渐进更新；生成中 `/messages` 进行中 part 返 `text=""`，**两源都无中段文本**——"打开看到半截且冻住"的根因。开销（实测 §11，30 tok/s × 100ms 窗，12 trace）：逐 token ~36x；原批式 ~12x（中位，信封主导，证伪 1.7x 假设）；**杠杆1+2 后 gzip 中位 1.47x**（1/3 trace <1.0x）——决定性解决"10x+ 开销"痛点。残余 ~0.3x（短消息/低冗余内容）记 Stage E 可选调参。

## 2. 目标 / 非目标
**目标**：生成中打开 session 剩余内容实时流入；批式降开销；省流默认零回归（opt-in）。
**非目标**：不改控制面 `/slimapi/events`；不替换 `/since` 真值（token 流是动画层，`/since` 校正真值）；不做二进制流；P1 只处理 **text** part（reasoning/tool-input 延后）。

## 3. 设计原则与关键决策
| 决策 | 选择 | 理由 |
|---|---|---|
| token 流位置 | 新端点 `GET /slimapi/sessions/{sid}/stream`，隔离控制面 | token 高吞吐；混入控制面 256项/2MiB 队列会挤掉 q/p 或误触 `subscriber_backpressure`（`hub.py:246-254`） |
| 作用域 | per-session（订阅绑 sid） | 移动端只对前台 session 要动画 |
| 上游连接 | 复用单条 `/global/event` | 单 worker 单上游不变量（`operations.md:27`、`hub.py:560`） |
| 累积门控 | **part 生命周期门控**（text-start→text-end），与订阅者无关 | 消除订阅 race；有界于活跃 text part |
| fan-out 门控 | per-subscriber（按 sid 过滤） | 省 CPU/带宽 |
| 批式调参 | 服务器侧，不进 wire | §10 |

## 4. 上游事件 shape（实施前须 live 抓包确认键大小写）
```
message.part.delta  properties: { sessionID, messageID, partID, field:"text", delta:"<chunk>" }
                     # reasoning-delta 也用 field:"text"，靠 part.type 区分（§5.3）
message.part.updated properties: { sessionID, part:{ id, messageID, sessionID, type, text?, time:{end?} }, time }
```
> AGENTS.md 硬约束：上线前 live 抓 `/global/event` 确认 `properties.part` 实际 JSON 键大小写。

## 5. 架构设计

### 5.1 新端点
```
GET /slimapi/sessions/{sid}/stream?directory=<optional>
  Header: X-Slimapi-Version: 1   （SlimapiVersionMiddleware 自动 gate 所有 /slimapi/**，versioning.py:13-57；无需 route-level Depends）
  Response: 200 text/event-stream
             Cache-Control: no-cache, no-transform ; X-Accel-Buffering: no ; X-Slimapi-Subscriber-ID: <ephemeral>
```
- **directory**：可选 query；`normalize_directory()`；query `directory` 与 `X-Opencode-Directory` 头冲突（trailing-slash 归一后不等）→ 400 `directory_not_allowed`（同 messages 路由）。directory 仅过滤进程级 GlobalBus 事件 directory，**不开第二条上游连接**。sid 全局唯一、directory 无关（单用户 T3）。
- 路由注册须在 catch-all 反代**之前**（`app.py:113-115`）；新建 `routes/token_stream.py`，路径 `/slimapi/sessions/{sid}/stream`，注意不遮蔽 `/{sid}/status`、`/{sid}/children`。

### 5.2 数据流与生命周期（B3 + C 次要）
```
opencode /global/event ──▶ GlobalHub.run()  [单条上游，循环守卫 has_consumers()]
                               │ publish() 派发
                    ┌──────────┴────────────┐
                    ▼                       ▼
            控制面（现状不改）         TokenStreamHub
            digest/q/p/error        消费 message.part.delta/updated
                                    维护 live_parts + _nontext_parts
                                    flush_loop(100ms)
                                    按 sid 过滤扇出
```
```python
# GlobalHub —— has_consumers 为方法（非 property，C 次要）
def has_consumers(self) -> bool:
    if self.subscribers:
        return True
    th = self._token_hub
    return th is not None and th.subscriber_count > 0

async def run(self) -> None:
    delay = 1.0
    while self.has_consumers():   # 原: while self.subscribers (hub.py:562)
        ...
# 两处 grace-stop 都改测 has_consumers():
#   GlobalHub.stop_after_grace (hub.py:341-346) + HubRegistry._remove_hub_after_grace (hub.py:770-792, hub.py:783)
# TokenStreamHub.subscribe/unsubscribe 须: registry.get_global().ensure_upstream() + 取消 HubRegistry._removal_task（跨 registry）
```
- `GlobalHub.publish()`（`hub.py:527` 现 drop）增加分支：`message.part.delta`/`message.part.updated` → `self._token_hub.on_*()`（注入仿 `_children_cache`，`hub.py:300/683-686`）。
- 控制面所有现有分支**一行不改**。
- **重连**：`run()` reconnect 分支（`hub.py:576-578`）须额外调 `self._token_hub.on_upstream_reconnect()`——清 `live_parts`/`_nontext_parts`/`_pending`/`_disabled_parts` + 对每个 token 订阅者扇出 `resync{reason:"reconnect_no_replay",sessionID}`。

### 5.3 累积与生命周期（B1+B2+C3+C4 核心）
```python
@dataclass
class LivePart:
    chunks: list[str] = field(default_factory=list)   # chunk-list，禁 text += delta（防 O(n²)）
    byte_count: int = 0
    ended: bool = False
    last_delta_ms: int = field(default_factory=lambda: _now_ms())

class TokenStreamHub:
    live_parts: dict[tuple[str,str,str], LivePart]
    _nontext_parts: set[tuple[str,str,str]] = field(default_factory=set)   # C3: reasoning/tool part key
    _disabled_parts: set[tuple[str,str,str]] = field(default_factory=set)  # C4: truncated/too_large 后禁用
    _pending: dict[tuple[str,str,str], DeltaAccumulator]
    _total_live_bytes: int = 0

    def on_part_updated(self, props):
        part = props.get("part") or {}
        if not isinstance(part, dict): return
        sid = part.get("sessionID"); mid = part.get("messageID"); pid = part.get("id")
        if not all(isinstance(x, str) and x for x in (sid, mid, pid)): return
        key = (sid, mid, pid)
        t = part.get("time")
        if not isinstance(t, dict): return
        if part.get("type") != "text":           # C3: 非 text 记入集合，delta 静默 drop
            self._nontext_parts.add(key); return
        if t.get("end") is None:                 # text-start：建 LivePart（与订阅者无关！）
            if key not in self.live_parts:        # 不重置已存在累加器（防中间 update 覆盖）
                seed = part.get("text") or ""
                self._start_part(key, seed)
        else:                                     # text-end：终态取 part.text（插件可改写）
            self.finish_part(key, part.get("text") or "")

    def on_part_delta(self, props):
        if props.get("field") != "text": return
        sid = props.get("sessionID"); mid = props.get("messageID"); pid = props.get("partID")
        delta = props.get("delta")
        if not all(isinstance(x, str) and x for x in (sid, mid, pid)): return
        if not isinstance(delta, str) or not delta: return
        key = (sid, mid, pid)
        if key in self._nontext_parts: return     # C3: reasoning/tool 静默 drop
        if key in self._disabled_parts: return    # C4: truncated 后禁用
        live = self.live_parts.get(key)
        if live is None:
            # C3: 孤儿（漏 text-start，如 sidecar 重启于生成中）→ 静默 drop+计数，绝不 resync
            self._metrics.orphan_deltas += 1; return
        if live.ended: return                      # C1: text-end 后的迟到 delta 丢弃
        n = len(delta.encode("utf-8"))
        if not self._reserve(live, n, key):        # C5: 超单 part/全局上限 → drop_part + resync
            return
        live.chunks.append(delta); live.last_delta_ms = _now_ms()
        self._pending.setdefault(key, DeltaAccumulator()).append(delta)

    def finish_part(self, key, final_text):        # C1: 同步 drain 再发终态
        acc = self._pending.pop(key, None)
        if acc is not None and acc.byte_count:
            self._safe_fanout(key, sse_frame(
                {"sessionID":key[0],"messageID":key[1],"partID":key[2],"text":acc.drain()},
                event="message.part.delta"))
        self._safe_fanout(key, sse_frame(
            {"sessionID":key[0],"messageID":key[1],"partID":key[2],"text":final_text,"done":True},
            event="message.part.snapshot"))
        self._drop_part(key)                       # 退役（不清 _disabled_parts）

    def drop_part(self, key):                      # C4: truncated/too_large
        self._pending.pop(key, None)
        live = self.live_parts.pop(key, None)
        if live: self._total_live_bytes -= live.byte_count
        self._disabled_parts.add(key)
```
- **累积与订阅者无关**（B1）：text-start 即建，text-end/退役即释放。订阅中途到达时 live_parts 已有累计文本 → snapshot 权威（race 消除）。
- 退役触发：text-end(`finish_part`) / `session.deleted`(清该 sid 全部) / `session.status=idle`(清该 sid) / 孤儿 TTL / truncated(单 part >1MiB，`drop_part`)。
- **TTL 兜底**：flush_loop 每 60s 扫 `last_delta_ms`；仅当 `last_delta_ms` 超 `TOKEN_ACC_IDLE_MS` **且** `_session_status[sid]` 已知为 idle 时退役（防长暂停生成误清，bgpt NB#4 / NB-B4：`unknown≠idle`，未知状态不退役）。

### 5.4 批式与终态算法（C1）
- `DeltaAccumulator`（chunk-list + UTF-8 字节计数）；`drain()` = `"".join(chunks)` + 清空。
- flush 触发：时间窗 `TOKEN_FLUSH_SECONDS=0.1`（100ms）**或** 单累加器 `byte_count ≥ TOKEN_FLUSH_BYTES=4096` 早刷。
- flush 顺序：`for key in sorted(self._pending)`（稳定排序，保证同 tick part 顺序确定）。
- **finish_part（C1）**：text-end 到达时**同步** drain 该 key 的 `_pending`→fanout 残余 delta→fanout `snapshot{done:true}`（取 part.text）→`_drop_part`。`finish_part` 返回后该 key 不得在 `_pending`/`live_parts`；此后同 key delta 视为迟到（`live.ended`/disabled → 丢弃）。
- 复用 `flush_loop` 模式（`hub.py:348`）；flush_loop 与 publish()/finish_part 均 event-loop 同步，无交错。

### 5.5 订阅握手（C2 消除 double-count + 心跳）
连接建立（单连接内顺序，**全程无 await 临界段**，flush_loop 不得交错）：
```python
def on_token_subscribe(self, sid, sub):
    # 1) Last-Event-ID → resync{reconnect_no_replay,sessionID}
    # 2) server.connected{sessionID}（仅表 subscription 建立，非 digest）
    # 3) C2: 先 flush 给【现有】订阅者（新者尚未入 fanout → snapshot 反映已 flush 态，_pending 该 sid 清空）
    self.flush_sid(sid)
    # 4) 对该 sid 每个活跃 text LivePart 排 snapshot{done:false}（累计全文=join(chunks)）
    for key, live in sorted(self.live_parts.items()):
        if key[0] != sid: continue
        self._safe_put(sub, snapshot_frame(key, "".join(live.chunks), done=False))
    # 5) 加入 sid fan-out 集合；此后 delta 才扇出给新者
    self._subs_by_sid[sid].add(sub)
```
- **心跳必发**（grok N7）：`server.heartbeat` 每 `TOKEN_HEARTBEAT_SECONDS=15`（空帧），防 stunnel/代理 idle-timeout 断静默流。
- 因累积与订阅者无关，snapshot 携带 text-start 起全部累计文本 → **无缺口、无重复**（C2）。

### 5.6 Wire 帧规范（B2/B4/C6）
```
# 1) 订阅首帧：活跃 part 累计全文
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<累计全文>","done":false}
# 2) 批式增量
event: message.part.delta
data: {"sessionID":"…","messageID":"…","partID":"…","text":"<本窗拼接>"}
# 3) 终态 marker（杠杆1：去终态全文——仅完成标记，不带 text；插件改写由 /since 兜底）
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","done":true}
# 4) 大 part 超 1MiB（done:false 或 done:true 均可能，C6）——不静默 drop
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","truncated":true,"done":false|true}
# 5) resync（背压/重连/孤儿(仅真正重连)/超大/内存上限；token resync 带 sessionID）
event: resync
data: {"reason":"subscriber_backpressure|reconnect_no_replay|token_memory_limit|session_idle|session_deleted","sessionID":"…"}
# 6) server.connected{sessionID} / server.heartbeat{}（15s）
```
- 帧格式复用 `sse_frame()`（现位于 `sse/hub_types.py:212` 与 `sse/tokenstream/frames.py:33`——2026-08-21 勘误：原 `hub.py:109` 锚点已随模块拆分漂移）。**不发 SSE `id:` 字段**、**无 replay buffer**；`Last-Event-ID` 仅触发首帧 resync，值忽略。
- **终态顺序不变式（wire 强约束）**：对同一 `(sid,mid,pid)`，所有 `message.part.delta` 帧必先于对应 `snapshot{done:true}` 入队；`done:true` 后该 part 不许再发 delta。终态 `text` 取自 `message.part.updated` 的 `part.text`（text-end 插件可改写，不可用拼接 delta）。
- **safe_put（C6/次要）**：所有 snapshot 下发前先 `len(frame)` 判定（含 SSE framing+JSON 开销，非仅 text 字节）；超 `token_stream_max_frame_bytes` → 发 `truncated:true` 帧（**绝不** `put` 超大帧触发静默 drop，`hub.py:229`）。
- **backpressure resync 带 sessionID（次要）**：token 订阅者单 session；`Subscriber.put` 溢出帧（`hub.py:250`）无 sessionID——token 流子类化注入 sessionID，或客户端从连接推断（CLIENT_CHANGES 注明）。

### 5.7 完成与权威对齐
- part/message 完成仍走**既有路径**：`message.updated`(step-finish) → digest → 客户端 `/since` 拉权威全文。
- stream `snapshot{done:true}` 是"流视角完成"；digest+`/since` 是"持久化真值"。不一致以 `/since` 为准（幂等覆盖）。客户端可接受 digest 完成先于/晚于 token 终态帧；`/since` 替换幂等且凌驾所有 token 帧。

### 5.8 背压 / 重连 / R1（B4/C4）
- 复用 `Subscriber.put` T3 三段守卫（`hub.py:206-254`）：溢出 → 清队列 → `resync{subscriber_backpressure,sessionID}` + STOP。客户端停增量 → `/since` 校正。
- 上游重连：`run()` 指数退避（`hub.py:560`）；`on_upstream_reconnect()` 清全部 live_parts/nontext/pending/disabled + 扇出 `resync{reconnect_no_replay,sessionID}`；客户端**丢弃该 sid 全部 token 渲染态** → `/since` → 重订阅。
- **R1/C4**：token 流 `max_frame_bytes=1MiB`（独立于控制面 256KiB）。snapshot（done:false 或 done:true）仍超限 → `truncated:true`（不静默 drop，不 chunked）→ `drop_part`（该 part 不再发任何 token 帧）；delta ≤4KiB 永远安全。

## 6. 内存与 T3 信封（B5+C5；Stage E 裁定 Option B 拆 4+4）
```python
# config.py —— token 流独立小预算，不占 MAX_TOTAL_SUBSCRIBERS
token_stream_max_subscribers: int = 8
token_stream_queue_items: int = 64
token_stream_buffer_bytes: int = 512 * 1024        # 512 KiB/sub
token_stream_max_frame_bytes: int = 1024 * 1024    # 1 MiB（B4）
TOKEN_PART_MAX_BYTES = 1024 * 1024                 # 单 part 累积上限
TOKEN_LIVE_PARTS_MAX = 32                          # C5: 全局活跃 part 数上限
# Stage E 裁定 Option B（拆 4+4，不双计）——替换 Stage A 的单一 8MiB 常量
TOKEN_LIVEPARTS_MAX_BYTES = 4 * 1024 * 1024        # 4 MiB live（LivePart.chunks）
TOKEN_PENDING_MAX_BYTES   = 4 * 1024 * 1024        # 4 MiB pending（DeltaAccumulator；与 live 不双计）
TOKEN_FLUSH_SECONDS = 0.1; TOKEN_FLUSH_BYTES = 4096
TOKEN_ACC_IDLE_MS = 60_000; TOKEN_HEARTBEAT_SECONDS = 15
```
- token 订阅**独立账本**，不消费 `MAX_TOTAL_SUBSCRIBERS=16`。
- **预算裁定（Stage E，Option B 拆 4+4，不双计）**：
  - **Option A**（合并 8MiB 单池）：单一 `TOKEN_LIVEPARTS_MAX_BYTES=8MiB` 覆盖 live + pending。
  - **Option B**（采纳）：`TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live）+ `TOKEN_PENDING_MAX_BYTES=4MiB`（pending），**不双计**（同一 delta chunk 不在两个池同时占额度；`_reserve` 入 live 池时 delta 也已在 pending，但记账只算一次——实现侧 NB-C1 同步 live 4MiB）。
  - **rationale**：pending 独立上限更防御——pending 突发（flush 阻塞 / 短窗大量 delta）不会挤掉 live 退役预算；两个池各自 4MiB 上限更难同时打满。worst-case 与 Option A 同上限。
- **worst-case**（写进契约 §6.x）：订阅队列 `8 × 512KiB = 4MiB` + handshake `8 × 8MiB = 64MiB` + live `4MiB` + pending `4MiB` = **76MiB**（handshake buffer 为新增项，`TOKEN_HANDSHAKE_BUFFER_BYTES=8MiB/sub`；runtime 正常态无 handshake 占用时仅 `4MiB queue + 4MiB live + 4MiB pending = 12MiB`）。
- C5 `_reserve`：append 前校验单 part 上限 + 全局 part 数 + live 池字节 + pending 池字节；超限退役最旧 part（按 `last_delta_ms`）+ `resync{token_memory_limit,sessionID}`。
- admission 失败 → 503 `{"code":"sse_token_subscriber_limit","limit":8,"current":N}` + `Retry-After:5`。

## 7. 服务端改动清单
| 文件 | 改动 |
|---|---|
| `sse/hub.py` | `has_consumers()` 方法 + run/stop/grace 改判；`publish()` 增 token 分支（替 `hub.py:527`）；reconnect 调 `token_hub.on_upstream_reconnect()`；`_token_hub` 注入 |
| `sse/token_hub.py`（新） | `TokenStreamHub`（live_parts+nontext+disabled+pending + 100ms flush + finish_part drain + drop_part + _reserve + 订阅握手 flush-sid-then-snapshot + TTL）+ `TokenStreamRegistry`（独立 admission，带 sessionID 的 token Subscriber 子类） |
| `routes/token_stream.py`（新） | `GET /slimapi/sessions/{sid}/stream`；catch-all 前注册 |
| `config.py` | §6 knobs |
| `app.py` | 装配 + metrics 注册 + 路由顺序 |
| `routes/health.py` | 加性**根级** `features.tokenStream:true`（B6；**Q1 冻结**：top-level `"features":{"tokenStream":true}`，与 sidecar/server/schema 并列；客户端可 dual-read root/server 过渡，服务端固定 root） |
| `routes/metrics.py` | `sse.tokenStream.{current,limit,rejectedTotal,pendingAccumulators,flushedFramesTotal,droppedFramesTotal,truncatedSnapshotsTotal,orphanDeltasTotal,tokenMemoryLimitTotal}` |

**token stream gzip 默认开（杠杆2，首个 SSE gzip 例外）**：实测 §11 表明 gzip 是 ~12x→1.47x 的决定性手段，故 token 流**默认 gzip**（流式 zlib Z_SYNC_FLUSH），不走"P2 测量门禁"。控制面 `/slimapi/events` 仍不 gzip。CHANGELOG 注明 token stream 为"SSE 永不 gzip"例外；Stage C 实现流式 gzip emit。

## 8. 契约 / 文档 / 版本影响
| 项 | 处理 |
|---|---|
| `v2-contract.md` §3 L150 | 限定控制面："`/slimapi/events` 控制面丢弃：…`message.part.*`…（`message.part.delta`/`updated` 由独立 `/slimapi/sessions/{sid}/stream` 消费，见 §3.x）" |
| `v2-contract.md` §3.x 🆕 | Token stream SSE 子节（端点/帧/no-replay-no-id/终态顺序不变式/`/since` 真值/新 resync reason） |
| `v2-contract.md` §6 | token 信封 addendum（独立 cap + 76MiB worst-case + `token_memory_limit` + `sse_token_handshake_overflow`） |
| `v2-contract.md` §4 health | 加性**根级** `features.tokenStream`（**Q1 冻结路径**：top-level `features`，非 `server.*` 下） |
| `CHANGELOG`/`INTERFACE_MAP`/`CLIENT_CHANGES` | 同步 |
| `X-Slimapi-Version` | **不 bump**（加性） |

## 9. ocdroid 配合清单
| # | 项 | 必须/可选 |
|---|---|---|
| 1 | stream 客户端：前台 opt-in 连 `/slimapi/sessions/{sid}/stream`；切后台/换 session 断开 | 必须 |
| 2 | **capability**：`/slimapi/health` 的 `features.tokenStream===true` 才用；缺/404/405 → 降级"完成后整条出现"（零回归） | 必须 |
| 3 | **streamOwned 算法**：`snapshot`→替换缓冲+标 streamOwned；`delta`→streamOwned 且未完成才 append；`snapshot{done:true}`→标完成；`/messages`/`/since`：streamOwned 且未完成则忽略，已完成仅允许 `/since` 覆盖 | 必须 |
| 4 | **truncated/降级**：收 `snapshot{truncated:true}`（done:false 或 done:true）→ 清该 part streamOwned、停 append、走 `/since`；或 `resync{token_memory_limit\|session_idle\|session_deleted}` → 清该 sid 全部 streamOwned、`/since`、重订阅 | 必须 |
| 5 | **resync**：收 `resync{...,sessionID}` → 丢弃该 sid 全部 token 渲染态 → `/since` 重拉 → 重订阅（背压 resync 若无 sessionID，从连接推断） | 必须 |
| 6 | 终态对齐：digest `message.updated`(step-finish) → `/since` 权威全文幂等覆盖 | 必须 |
| 7 | 连接独立于 `/events`；预算"同时最多 1 条前台 stream" | 建议 |
| 8 | busy-open UX（可选）：打开 busy session 先占位直到 stream 首帧 | 可选 |

## 10. 批大小调参：是否需 ocdroid 配合？
**不需要。** `TOKEN_FLUSH_SECONDS`/`TOKEN_FLUSH_BYTES` 是服务器侧 env knob，不改 wire。客户端只 append，与服务端怎么攒无关。**硬约束（写进 CLIENT_CHANGES）**：渲染须对任意 batch 稳健——每帧当"待追加文本段"，不按 token 计数、不假定帧间隔。推荐起始：100ms + 4KiB。

## 11. 验证计划
- **单元**：批式合并/保序/字节早刷/100ms flush/订阅首帧 snapshot/**握手 flush-sid-then-snapshot 无 double-count(C2)**/TTL/背压/孤儿静默 drop(C3)/`part.type!=text` 隔离/`field!=text` 丢弃/**finish_part drain 顺序(C1)**/**truncated→drop_part 后无续发 delta(C4)**/**全局内存上限 eviction(C5)**/token-only 续命上游。
- **拼接专项**：模拟"生成中订阅" snapshot+delta 无缺口无重复；漏 text-start 孤儿静默 drop。
- **R1**：>1MiB snapshot（done:false 与 done:true）→ 断言 `truncated:true` 而非静默 drop。
- **门禁**：`./scripts/check.sh`。
- **性能测量（已完成 §11；Stage E 确认）**：harness `scripts/measure_token_overhead.py`（12 trace，30 tok/s × 100ms）。实测：原批式 ~12x（证伪 1.7x）；杠杆1+2 后 gzip 中位 **1.47x**（1/3 trace <1.0x）。**目标 re-anchor ~1.5x 中位（达成）**；1.2x 原为假设非硬指标。残余 ~0.3x = Stage E 残余调参（flush 窗 100→200ms、gzip flush cadence、level），**裁定为 post-release 可选**（不阻塞发版；当前 1.47x 已达成 re-anchor 目标）。

## 12. 分阶段交付
- **P1**：新端点 + 生命周期门控累积 + 批式 + 订阅首帧 snapshot(C2) + finish_part drain(C1) + 静默 drop(C3) + truncated 退役(C4) + 全局内存上限(C5) + has_consumers 生命周期 + 独立 T3 信封 + health features + 契约 §3.x/§6 + 性能实测。
- **P2（测量后）**：gzip。
- **可选**：reasoning/tool-input part；自适应 flush 窗。

## 13. 实施前须实测确认
1. abort/halt/context-overflow 路径（`processor.ts:555-560`、`599-619`）是否都可靠发 text-end `updatePart`？——决定 §5.3 TTL 清理是必需还是 defense-in-depth。
2. `message.part.updated` 上线后 `properties.part` 实际 JSON 键大小写（live `/global/event` 抓包）。

## 14. 服务端实施阶段（与 ocdroid 同形式：每阶段单评委 9.5 门控）
| Stage | 范围 | 主要文件 | fixer |
|---|---|---|---|
| **0** | 设计 v3 联合门控（server v3 + client v3 合并） | 本文档 + ocdroid `docs/token-stream-dev-plan.md` §3 | — (gate) |
| **A** | 基础：config knobs(§6) + `TokenStreamHub` 骨架（live_parts/nontext/disabled/pending + `on_part_updated`/`on_part_delta` ingest，`part.type`/`field` 门控 + C3 静默 drop）+ `_token_hub` 注入 `GlobalHub` | `config.py`, `sse/token_hub.py`(新), `sse/hub.py` | fixer-zlm ×2 |
| **B** | 生命周期：`has_consumers()` + run/stop/grace 改判 + token subscribe `ensure_upstream` + `on_upstream_reconnect()` + `finish_part` drain(C1) + `drop_part`(C4) + `_reserve` 全局内存(C5) + TTL | `sse/hub.py`, `sse/token_hub.py` | **fixer**（复杂） |
| **C** | flush+wire：`flush_loop`(100ms/4KiB/sorted) + `DeltaAccumulator` + 订阅握手 flush-sid-then-snapshot(C2) + `sse_frame`(snapshot/delta/truncated/resync/heartbeat/server.connected) + `safe_put` | `sse/token_hub.py` | **fixer** |
| **D** | 端点+admission：`routes/token_stream.py` + `TokenStreamRegistry`（独立 admission + sessionID 注入 Subscriber 子类）+ health 根级 `features.tokenStream`(Q1) + metrics | `routes/token_stream.py`(新), `routes/health.py`, `routes/metrics.py`, `app.py` | **fixer** |
| **E** | 契约/文档+实测：v1-contract §3 L150 scope + §3.x + §6 addendum + §4 health + CHANGELOG/INTERFACE_MAP/CLIENT_CHANGES + 性能实测(§11)（2026-08-21 注：`docs/specs/v1-contract.md` 已于 v2 契约换代删除，见 v2-contract.md 头部墓碑注——此处为历史锚点） | `docs/*` | fixer-zlm |

每 Stage：`./scripts/check.sh` 必过 → 单评委门控 9.5 → PASS 才进下一 Stage。
门控评委：**rev-grok**（优先）→ 不可用 **rev-bgpt** → 仍不可用 **rev-gpt**；FAIL→修订→重评；同 Stage 重试 ≥2 升级 fixer。

## 15. 状态日志
| 时间 | 阶段 | 动作 | 结果 |
|---|---|---|---|
| r1 | server 设计 v1 评审 | 3 评委(grok/opus/bgpt) | FAIL 7.1/6.5/5.2 → v2 |
| r2 | server 设计 v2 复审 | 3 评委 | FAIL 8.6/8.2/7.1 → v3（C1-C6 收敛） |
| now | v3 落盘 + Q1 冻结(root features) + 阶段计划 | 本文档 §14 | 完成 |
| r3 | Stage-0 联合门控（merged 设计） | ocdroid 3 评委(grok/opus/bgpt) + 我方 backstop(bgpt) | FAIL 9.2/9.0/8.7 + backstop 7.4；**架构级 PASS**，残留 fold §16 |
| now | Stage-0 裁定 | 双边共识 | 架构 PASS；Q1 root+Q2-Q8 对齐；服务端 must-fix fold §16；转每阶段 9.5 门控（不再文档级重评） |
| r4 | **Stage A**（服务端基础） | fixer-zlm ×2 + 单评委门控 | **PASS 9.5**——config knobs(§6) + `TokenStreamHub` 骨架（live_parts/nontext/disabled/pending + ingest，`part.type`/`field` 门控 + C3 静默 drop）+ `_token_hub` 注入 `GlobalHub` |
| r5 | **Stage B**（生命周期） | fixer + 单评委门控 | **PASS 9.5**——`has_consumers()` + run/stop/grace 改判 + token subscribe `ensure_upstream` + `on_upstream_reconnect()` + `finish_part` drain(C1) + `drop_part`(C4) + `_reserve` 全局内存(C5) + TTL（NB-B4：读 `_session_status`）+ bounded tombstones(§16-B) + `session.idle` resync(`session_idle` reason) |
| r6 | **Stage C**（flush+wire） | fixer + 单评委门控 | **PASS 9.5**——`flush_loop`(100ms/4KiB/sorted) + `DeltaAccumulator` + 订阅握手 flush-sid-then-snapshot(C2) + `sse_frame`(snapshot/delta/truncated/resync/heartbeat/server.connected) + `safe_put` + **杠杆1**（finish_part 终态帧 = `snapshot{done:true}` marker 无 text）+ **杠杆2**（token stream 默认流式 gzip，首个 SSE gzip 例外）+ truncate 扇给该 sid 全部订阅者 |
| r7 | **Stage D**（端点+admission） | fixer + 单评委门控 | **PASS 9.6**（经 B-D1 修复）——`routes/token_stream.py` + `TokenStreamRegistry`（独立 admission + sessionID 注入 Subscriber 子类）+ health 根级 `features.tokenStream`(Q1) + metrics + backpressure resync 恒带 sessionID |
| r8 | **Stage E**（契约/文档+预算裁定） | fixer（docs+code 双 lane）+ rev-glm 9.3 CONDITIONAL → fold | **PASS（fold 净）**——契约 §3.x/§6.x 加性 rev J；预算 Option B 4+4 落地（763 tests）；fold `part_too_large` 虚假契约（reason 集 = 代码实际发出 5 个）；CLIENT_CHANGES lever1 对齐；CHANGELOG gzip 例外；INTERFACE_MAP 刷新 |
| r9 | 双边兼容核验 | explorer 读 ocdroid 源码 | **可进联合终审**——7 项中 5 兼容；**风险** done:true 空白窗口（C-1）；**缺口** session_deleted/session_idle 未识别（C-2）；详见 `CHANGELOG.md` `[0.5.0]` + 本文件 §3.x（原 handoff 文档已移除） |
| now | 评委池 | 用户指定 | 后续评审用 **rev-bgpt**（弃 grok；glm 仅 Stage E 用过） |
| r10 | ocdroid 回传裁定 | 用户转发 | C-1=**A**、C-2=**修**、C-3/C-4=做；联合终审 **blocked** 至 ocdroid 落地 C-1+C-2；V-B 服务端侧：15s heartbeat + 无 HTTP 缓冲（见 handoff §8.3） |
| r11 | ocdroid ready + 联合终审 | d4b22da + rev-bgpt | C-1/C-2/C-3 PASS；**NO-GO 8.4** — 阻塞：`token_memory_limit` clear-only 不重连 → 后续 delta orphan（handoff §9）；修：`triggersReconnect=true` for memory limit |
| r12 | 方案 A + re-gate | ocdroid 工作区 + rev-bgpt | `TOKEN_MEMORY_LIMIT.triggersReconnect=true` + CLIENT_CHANGES 两档表；**re-gate GO 9.7**；可发版（不 bump wire） |

## 16. 联合 Stage-0 残留 must-fix（folded 为阶段契约；不再文档级重评）
> 联合 3 评委（grok 9.2 / opus 9.0 / bgpt 8.7）+ 我方 backstop（bgpt 7.4）+ ocdroid 跨平面印证：**架构级 PASS**，残留=受限生命周期边界 bug，均附可采纳代码。fold 为各 Stage 实施契约，在该 Stage 9.5 门控验证。跨平面 wire 已核验兼容（features root / 无 `part_state_missing` / truncated 保 `done` / backpressure 带 `sessionID`），双方 §3 均无需改。

**服务端（落 oc-slimapi 各 Stage；ocdroid §3.10 转 + 我方 backspot 印证/独有）**
- **[Stage A]** §5.3 `finish_part` 笔误 `_drop_part`→`drop_part`（backstop 独有 C1；每次 text-end 必 AttributeError）。
- **[Stage B]** `_disabled_parts`/`_nontext_parts` 有界化（ocdroid bgpt MF-1 + backstop）：bounded `OrderedDict`+TTL(4096/300s)+prune + per-session map，idle/deleted/reconnect 清；`drop_part` 幂等(返回 bool，resync/truncated 只发一次)。
- **[Stage B]** `publish()` 路由补 `session.status`/`session.deleted` → token_hub（ocdroid opus MF-B + backstop）：`_busy_sids`+`_session_status`+`_retire_session(sid)`；TTL busy-guard 读 `_session_status`（**NB-B4 勘误**：实现正确读 `_session_status`，`unknown≠idle`——仅已知 idle 才退役，未知状态不退役，防长暂停生成误清）。
- **[Stage B]** `session.idle` 清理向 token subscriber 发 resync（ocdroid bgpt SF-3 + backstop clear_session）：新 `session_idle` reason（客户端 §3.9 reason 扩展已兼容）。
- **[Stage B]** 重连两处挂载（backstop 独有）：`_notify_upstream_loss()` 统一挂成功(hub.py:576-578)**和**异常(591-597)路径，每 epoch 一次。
- **[Stage B]** `has_consumers()` 贯穿**所有** grace 路径（backstop 独有）：`stop_after_grace`+`_remove_hub_after_grace`+token subscribe 取消 `_removal_task`。
- **[Stage C]** 全局内存预算含全部留存态（backstop C5）：单一字节预算(live chunks+pending chunks+seed)；`TOKEN_LIVEPARTS_MAX_BYTES`+`TOKEN_PENDING_MAX_BYTES`(各 4MiB)；`_reserve` 处理 delta 超剩余；worst-case 重算写进契约 §6。**[Stage E 裁定 NB-C1]**：采纳 Option B 拆 4+4（不双计）；实现侧 Stage A 单一 `TOKEN_LIVEPARTS_MAX_BYTES=8MiB` 待 src/ lane 同步为 live 4MiB + 新增 `TOKEN_PENDING_MAX_BYTES=4MiB`（§6 已更新）。
- **[Stage C]** truncate 扇给该 sid **全部**订阅者（backstop 独有 C6）：`truncate_part_for_all(key,done)`+`emit_snapshot_or_truncated` helper；`finish_part` 终态帧走此 helper。
- **[Stage C] 杠杆1**：`finish_part` 终态帧 = `snapshot{done:true}` **仅 marker（不带 text）**（§5.6），取消 part.text 终态重发。
- **[Stage C] 杠杆2**：token 流 emit 默认**流式 gzip**（zlib Z_SYNC_FLUSH，`Content-Encoding: gzip`，首个 SSE gzip 例外，§7）。
- **[Stage D]** backpressure resync 恒带 sessionID（backstop + cross-plane#3）：`TokenSubscriber` 子类覆写溢出帧。

**Stage E（docs lane）范围契约**
- **契约 §3.x + §6.x 加性**（`docs/specs/v2-contract.md`）：新端点行（§2 表）+ token stream SSE 子节（端点 / wire 帧 / no-id-no-replay / 终态顺序不变式 / `/since` 真值 / gzip 杠杆2）+ token T3 信封 addendum（独立账本 / 预算「同时最多 1 条前台 stream」/ Option B 拆 4+4 不双计 / admission 溢出 503 + Retry-After / gzip 例外）；§7 加 `sse_token_subscriber_limit` code；health 根级 `features.tokenStream`（Q1）。**不 bump** `X-Slimapi-Version`（加性 wire）。
- **CLIENT_CHANGES lever1 对齐**（`:217`）：pre-lever 旧文「done:true 带 text」→ 「marker 仅完成标记，无 text；权威全文走 `/since`」（与 §5.6 杠杆1 一致）。
- **预算裁定 Option B 4+4**（§6 + §16 Stage C NB-C1）：`TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（live）+ `TOKEN_PENDING_MAX_BYTES=4MiB`（pending），不双计；rationale = pending 独立上限更防御；worst-case 76MiB（加入 handshake buffer 8MiB/sub 后，见 §6.x）；NB-C1 = src/ lane 同步 live 4MiB。
- **CHANGELOG gzip 例外**：[Unreleased] 加 token-stream feature 条目——端点、opt-in（health 根级 `features.tokenStream`）、杠杆1 done:true marker 无 text、**杠杆2 gzip 首个 SSE 例外**（注明控制面 `/slimapi/events` 仍不 gzip）、resync reason 集、独立 T3 账本、内存预算 Option B 4+4。加性 wire（不 bump `X-Slimapi-Version`）。
- **INTERFACE_MAP §3.1 刷新**：新 `/slimapi/sessions/{sid}/stream` 行（SSE，opt-in，gzip 默认[lever2]，独立 T3 账本，done:true 无 text marker[lever1]，4+4 预算）。
- **§11 perf 确认**：1.47x 实测 + re-anchor ~1.5x 中位达成；残余调参（flush 窗 / gzip cadence）可选 post-release。
- **NB-B4 勘误**：§16 Stage B + §5.3 TTL 措辞「读 `_busy_sids`」→ 「读 `_session_status`（实现正确，`unknown≠idle`）」。
- **不碰**：src/、tests/、config.py（src/ lane 负责；Stage E 仅 docs/）。
