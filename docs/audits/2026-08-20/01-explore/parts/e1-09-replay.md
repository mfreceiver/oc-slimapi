# E1-09 精读卡片：SSE replay 数据层 + wire 层

> 审计基线：2026-08-20 工作树。引用格式 `src/oc_slimapi/...:行号`。两文件全文精读（非抽样）。

---

### src/oc_slimapi/sse/replay_log.py（598 行）

- **职责**：v4 SSE replay 的**纯数据结构层**（B3b-1；design-v4-sse-replay §3.4）。有界环形重放日志：全局域 `"g"` + 每订阅 sid 惰性创建的 `"t:<sid>"` 域；per-domain 单调 seq（从 1 起，tombstone 同样占 seq 保无洞）；进程级 epoch（16-hex 随机 boot nonce，仅相等比较）；跨上游丢失的 per-domain barrier 低水位（`seq <= watermark` → `reconnect_no_replay`，禁跨 barrier 补帧；barrier 是元数据，不受 count/bytes/TTL 驱逐）。分类 ③ epoch / ④ barrier→window→gap 在 `replay()` 内按冻结短路序实现；①②语法/端点匹配不在本层（docstring `replay_log.py:18-25`）。asyncio 单线程无锁模型（`replay_log.py:33-35`）。

- **对外符号**（`__all__` `replay_log.py:50-69`）：
  - 模块级：
    - `GLOBAL_DOMAIN = "g"`（78）— 全局域 key；token 域恒 `"t:"` 前缀故不可能与之相撞（76-78 注释）。
    - `FRAME_KIND_BUSINESS / FRAME_KIND_TOMBSTONE`（83-84）— 帧种类；tombstone 仍带 `id:` 并占 seq。
    - `RESYNC_EPOCH_CHANGED / RESYNC_REPLAY_EXPIRED / RESYNC_REPLAY_GAP / RESYNC_RECONNECT_NO_REPLAY`（88-91）— log 层可裁决的四个冻结 resync reason（§7.2）。
    - `DEFAULT_REPLAY_MAX_COUNT=2048 / DEFAULT_REPLAY_MAX_BYTES=64MiB / DEFAULT_REPLAY_TTL_S=900.0`（93-95）— 三维默认界。
    - `new_epoch()`（101-108）— `secrets.token_hex(8)` 产 16 位小写 hex 进程 nonce。
    - `token_domain(sid)`（111-113）— `f"t:{sid}"`；对 sid 无任何校验。
    - `_default_size_of(payload)`（116-129）— bytes bound 计费：bytes/str 取 len，可 JSON 序列化取序列化长，其余计 0；可经 ctor 注入。
  - dataclass（均 frozen+slots）：
    - `ReplayEntry`（136-158）— 一条保留帧：`domain/seq/payload/kind/appended_at/size/order`（order=进程级 append 计数，用于跨域 bytes 驱逐排序）；property `is_tombstone`（156-158）。
    - `ReplayFrames`（161-170）— 成功结果：严格递增**连续**的 entries 元组；空元组=已追平（非 resync）。
    - `ReplayResync`（173-182）— 服务端决定 resync；reason ∈ 四冻结值。
    - `ReplayIgnoreReset`（185-197）— 忽略游标按首连处理（future cursor）；携带 `seq`。
    - `ReplayOutcome`（198）— 上述三者 Union。
  - `_DomainState`（205-231，内部）— 单域状态：`entries(deque)/next_seq/last_seq/bytes/barrier_watermark/last_touch`；property `window_start`（227-230）。
  - `ReplayLog`（237-598）：
    - `__init__`（260-302）— 校验 epoch 格式/max_count≥1/max_bytes≥1/ttl_s 有限且>0（nan/inf fail-closed，281-289 rev-gate MAJOR-1）；初始化 outcome Counter、可注入 clock/size_of。
    - `has_domain`（306）/`domain_keys`（309）/`domain_count`（312）/`frame_count`（315）/`domain_frame_count`（318）— 只读盘点。
    - `last_seq(domain)`（322-325）— 域最大已发布 seq（0=未创建）。
    - `window_start(domain)`（327-331）— 窗口下界（最老保留 seq；None=空/未知域）。
    - `barrier_watermark(domain)`（333-336）— 当前 barrier 水位或 None。
    - `metrics_snapshot()`（338-347）— 展平 outcome 计数 + domains/frames/bytes/barriers 给 /slimapi/metrics。
    - `append(domain, payload, *, kind)`（351-395）— 发布一帧：close 后 RuntimeError；惰性建域；先 TTL 驱逐头部分配 seq（tombstone 同占 seq，REPLAY-012），再 count/bytes 驱逐；**记录 published 而非 delivered**（360-363：背压溢出帧仍入日志）。
    - `replay(domain, after_seq, epoch)`（399-468）— 分类重连游标，冻结短路序：③ epoch 不等→`epoch_changed`（423-425，dominates）→ ④a `after_seq<=watermark`→`reconnect_no_replay`（434-436，`<=` 含水位帧本身，rev-5 勘误）→ ④b future cursor→`ReplayIgnoreReset`（440-442）→ ④c 窗口空/expired（448-458；`after_seq==last` 为 up_to_date 空帧）→ ④d 连续性防御扫描→`replay_gap`（459-466，设计上不可达）→ 否则返回连续 `ReplayFrames`（467-468）。每个出口都记 outcome 计数。
    - `write_barrier(domain=None)`（472-493）— 写上游丢失低水位：None=全域（含离线 token 域）；watermark=写时 last_seq，单调不降（491）；barrier 后新建的域无水位。
    - `recycle_domain(domain)`（495-511）— 清帧清 bytes，**保留 seq 计数与 barrier**（REPLAY-018 fail-safe：回收域旧 cursor 永不退化为首连语义）；返回域是否存在。
    - `sweep(now=None)`（513-539）— 全域 TTL 头部驱逐 + barrier GC（条件 `entries[0].seq > watermark+1`，537，rev-gate R5 off-by-one 修正：窗口下界越过 W+1 后 cursor W 自带 replay_expired，barrier 才真冗余；空窗口保 barrier——cursor==watermark==last 必须仍被拦截）。
    - `closed` property（541-544）/ `close()`（546-555）— 幂等关停；置 _closed + 清域；append 此后 fail loud。
    - `_ttl_evict_head`（559-566）— 严格大于 ttl_s 才驱逐（恰 ttl_s 年龄仍可 replay）。
    - `_evict_for_count`（568-570）— 本域 count ring（保最新 ≥1 帧）。
    - `_evict_for_bytes`（572-592）— 进程级 bytes：删全局最老（min head order）直到达标或只剩 1 帧（单帧超预算仍保留——不丢刚接受的帧）。
    - `_drop_head`（594-598）— popleft + 双记账（域 bytes/总 bytes）扣减。

- **依赖**：标准库 `math/re/secrets/time/collections.(Counter,deque)/dataclasses/typing` + 第三方 `orjson`（仅 `_default_size_of` 序列化计费）。无本仓内部依赖。

- **被依赖**（rg 反查）：
  - `src/oc_slimapi/app.py:34`（import `ReplayLog, new_epoch`）；`app.py:425-455` lifespan 构造（settings.replay_max_count / replay_max_bytes_kb*1024 / replay_ttl_s）+ sweep 任务；`app.py:490` `hubs.set_replay_log`；`app.py:540` `TokenStreamHub(replay_log=...)`。
  - `src/oc_slimapi/sse/global_hub.py:48-54,76-96,536-545`（持有/注入）；`global_hub.py:413` 上游首次确认丢失时 `write_barrier()`（全域）；`global_hub.py:552-570` `_replay_publish` append(GLOBAL_DOMAIN) + `sse_id_line`。
  - `src/oc_slimapi/sse/tokenstream/hub.py:85-91,252-271`；`hub.py:1375-1394` `_replay_publish_token`（在 no-subscriber 早退**之前** append，REPLAY-007）；`hub.py:1405-1424` `_write_replay_barrier(sid)`（idle retire / memory eviction / session deletion 三处状态失效源无条件单域写 barrier）。
  - `src/oc_slimapi/sse/registry.py:80-138`（replay_log 经 HubRegistry 转发）。
  - `src/oc_slimapi/routes/events.py:8-15,103-206`（classify/meta/帧补发消费）；`routes/token_stream.py:53-60,144-277`；`routes/metrics.py:97-102`（metrics_snapshot + epoch）；`src/oc_slimapi/sse/replay_wire.py:38-48`（见下卡）。
  - 配置：`src/oc_slimapi/config.py:662-671`（OC_SLIMAPI_REPLAY_COUNT/BYTES_KB/TTL_S）+ `config.py:1113-1128` fail-closed 校验。
  - 测试：`tests/test_replay_log.py`（数据层专测）、`tests/test_sse_replay_wire.py`、`tests/test_metrics_replay_block.py`。

- **状态/可变性**：单例（app.state.replay_log，进程生命周期）。可变状态全在 `ReplayLog`：`_domains: dict[str,_DomainState]`（域壳进程内**永不删除**，253-257——删除会使 next_seq 回归造成 ID 回退；真正 GC=进程重启换 epoch）、`total_bytes`（进程级 bytes 记账，**可长期超 max_bytes**，见疑点 7）、`_order`（跨域驱逐排序计数）、`_closed`。`_DomainState.entries` 只从头部弹出（head-only 驱逐不变式 → 窗口恒连续，205-212 docstring）。`replay_outcomes_total: Counter` 只增。`clock` 默认 `time.monotonic`（TTL 与壁钟无关，重启自然换 epoch 故无需持久化时间）。barrier_watermark 只在 `write_barrier` 单调上调、`sweep` 中可清 None；**不受任何帧驱逐影响**（元数据）。

- **错误路径**：
  - ctor `ValueError`：epoch 非 16-hex 小写（272-276）、max_count<1（277）、max_bytes<1（279）、ttl_s≤0 或非有限（281-289，nan 绕过 `<=0` 的坑已堵）。
  - `append`：`RuntimeError`（closed，365-366）；`ValueError`（domain 非非空 str，367-368）。生产调用方均 try/except 降级为 id-less fanout + warning（global_hub.py:565-569 / tokenstream hub.py:1390-1394）。
  - `replay`：`ValueError`（after_seq 非非负 int / 是 bool，421-422）。其余一切"错误"以返回值表达（ReplayResync×4 / ReplayIgnoreReset）。
  - `close` 幂等；close 后 replay/sweep/write_barrier **不报错**（见疑点 2）。

- **疑问点**（20 条）：
  1. **epoch 唯一性（boot nonce 冲突）**（101-108,270-276）：epoch=64-bit 随机；两进程碰撞（单次 ~2⁻⁶⁴，生日界 ~2³² 次重启）时 ③ 相等检查失效——旧 cursor 落入窗口判定：seq 超前→`ReplayIgnoreReset` **静默首连**（185-196），seq 恰在窗内→跨代际补发新进程帧。无 pid/启动时间复合校验。设计接受（§7.1 frozen），残余风险记录。
  2. **close() 后 replay() 不设防**（546-555 vs 399-468）：`_closed` 只挡 append（365）；close 清域后 in-flight replay 得到「空域」语义（epoch 未变：cursor>0→IgnoreReset，==0→up_to_date 空帧）而非显式错误——与 append 的 fail-loud 不对称；shutdown 竞态下新分类结果误导。
  3. **barrier GC 只在 sweep**（513-539,尤其 537）：count/bytes 驱逐已使 `entries[0].seq > watermark+1` 后、下次 sweep 前的窗口内，cursor≤watermark 报 `reconnect_no_replay` 而非 `replay_expired`——reason 随 sweep 时序漂移（客户端动作等价：都 HTTP 全量对齐，wire 无害；语义/统计口径有差异）。
  4. **空窗口 barrier 永久保留**（536 条件 `state.entries and ...`；526-529 注释自陈）：空窗 + cursor<last 时 replay_expired 本就自带拦截，barrier 冗余却保留（直到有新帧把下界推过 W+2 或进程重启）；叠加 recycle_domain 保 barrier（495-511）→ barrier 生命周期远超必要性。保守正确，无误但需知晓。
  5. **write_barrier 指定域不存在时静默 no-op**（488-489 `targets=[]`）：per-sid invalidation（tokenstream/hub.py:1420）若早于该域首帧，barrier 落空；此后 cursor=0 重连不受拦截——首连语义本无旧状态可失效，语义无害，但 hub 侧调用顺序无保证、本层不设防。
  6. **read path 有副作用**（428-429）：replay() 先做该域 TTL 驱逐再分类——重连读取本身会推进过期驱逐，「判定结果依赖调用时刻 TTL 状态」；测试必须注入 clock 才确定。
  7. **单帧超预算长期突破 bytes bound**（572-592 `frames > 1`，577 先查 `<=`）：只剩 1 帧时即使超预算也停——`total_bytes` 可长期 > max_bytes，`metrics_snapshot()["bytes"]`（343）会显示超支；属设计裕度（"never drops the frame it just accepted"），运维告警阈值需知晓。另：bytes 驱逐可删**别的域**的头（跨域副作用，572-592）——全局内存压力下 token 域补发窗口被「全局最老帧」驱逐侵蚀，跨域影响面值得审计备案。
  8. **_evict_for_bytes 复杂度**（580-592）：每删一帧全量扫所有域 head 取 min order，O(域数×驱逐数)；巨帧 append 触发批量驱逐时最坏 O(N²)。无堆结构。性能疑点非正确性。
  9. **_default_size_of 0 计费漏洞**（124-129）：非 bytes/str 且不可 JSON 序列化 payload 计 0——bytes 维度对其无约束（count/TTL 兜底）。生产 payload 均为 bytes 帧（两 hub 调用点），len() 精确；仅非常规调用方受影响。
  10. **ReplayIgnoreReset 静默性**（185-196,437-442）：future cursor（含「域从未创建 + cursor>0」路径，437-438 注释）不回 resync 不补帧——客户端对齐完全依赖其主动采纳 meta.seqBase（routes 在 handler 冻结 seqBase）；若客户端忽略游标倒挂，服务端无再纠正信号。契约义务全在客户端侧。
  11. **未创建域 cursor=0 记为 up_to_date**（448-451）：state=None → last=0、entries=()，after_seq==0 与「已创建且恰好追平」不可区分；metrics 词表无 unknown_domain 维度（routes/metrics.py:97-102 直接展平），统计口径盲区。
  12. **outcome 计数键无常量冻结**（297; 424/435/441/450/452/457/465/467）：`RESYNC_*` 只覆盖 resync reason；`"ignore_reset"/"up_to_date"/"replayed"` 等为裸字符串，仅 tests/test_metrics_replay_block.py:41 词表冻结——键名漂移风险。
  13. **last_touch 写而不读**（216,225,392,430,493,510）：全仓 rg 无任何读取点——死状态（或预留「按空闲回收域」用，现回收策略在 replay_wire sweep 以帧数==0 为准，见下卡疑点 6）。
  14. **并发正确性依赖 routes 顺序不变量**（33-35 无锁声明成立的前提）：本模块方法内无 await，单 loop 下 append/replay/sweep 各自原子 ✓；但「不重不漏」还需 handler 中 **classify(T0) 先于 subscribe(T1)**（events.py:110-114 / token_stream.py:151-155 注释）：replay 覆盖 ≤last@T0，queue 覆盖 attach 后发布帧。该不变量不在本模块强制——若未来把 classify 挪进 generator 首帧期即出现 gap/dup。API 无防御。
  15. **环形覆盖 × 在途重连（任务点名）**（443-447）：`replay()` 返回前 `tuple(...)` 即时快照拷贝 deque——此后 popleft 驱逐（count/bytes/TTL/sweep）不影响已返回 entries（frozen dataclass + payload 引用保活）→ 在途重连可安全逐帧 yield 已被覆盖帧。正确性关键一行，成立 ✓（payload 生命周期由 hub 侧保证不再复用 buffer，本层只持引用）。
  16. **「背压溢出帧仍入日志」×「窗口无空洞补发」组合（任务点名）**（360-363; tokenstream/hub.py:1378-1394 在 no-subscriber 早退前 append）：背压/离线期发布的帧照样占 seq 可补发；配合 v4 silent-STOP（域外 reason 不发 resync，hub_types.py:311-317 / subscriber.py:444-460），客户端恢复路径唯一 = Last-Event-ID 重连 → 本层窗口判定。组合闭环前提 = 窗口未过期且无 barrier；若溢出期间该 sid 恰逢 idle/evict/deleted（tokenstream/hub.py:1420 写 barrier），`after_seq<=watermark` → `reconnect_no_replay`（434 先于窗口判定）——补发让位于屏障，组合语义一致（屏障优先）✓。
  17. **resync 四值短路序自洽性（任务点名）**（399-468）：③ epoch（423 dominates）→④a barrier（434）→④b future（440）→④c expired（448-458）→④d gap（459-466 防御）。两处顺序自洽验证：barrier 先于 future——watermark≤last 恒成立（491 取 last_seq 且 last 只增），`after_seq<=watermark≤last` 必非 future ✓；`entries[0].seq != after_seq+1`→expired（454-458）先于 gap 扫描，gap 分支靠 head-only 驱逐不变式保持不可达，若破则 fail as `replay_gap` 不静默服务带洞 replay ✓。
  18. **`after_seq <= watermark` 边界**（434，rev-5 勘误 `<=` 而非 `<`）：水位帧自身（seq==watermark）在丢失前已发布——该帧是否真送达过客户端不可知，按「已拦截至水位」处理 ✓ 冻结语义。
  19. **epoch 参数宽容**（399,423）：replay() 对传入 epoch 只做相等比较、不校验 16-hex 语法（parse 层已过滤）；`None` epoch 会命中 `epoch_changed` 计数——公开 API 语义上可接受，未来直调方需知。
  20. **recycle_domain 实际语义（任务点名「被覆盖帧×在途重连」邻接）**（495-511）：唯一生产调用方是 replay_wire sweep（对帧数已 0 的域）→ `while state.entries` 体不执行，效果仅刷新 last_touch（无读者，疑点 13）+返回 True——在当前 wiring 下近乎 no-op；域壳本就永不删除（253-257）。设计文档称「回收策略」，实现是幂等清空 + 保壳保 seq 保 barrier。

---

### src/oc_slimapi/sse/replay_wire.py（282 行）

- **职责**：v4 SSE replay 的 **wire 层**（B3b-2；design-v4-sse-replay §4 / v4-contract §7）：`id:` 行生成（`g:<epoch>:<seq>` / `t:<sid>:<epoch>:<seq>`——统一为 `<domain>:<epoch>:<seq>`）；Last-Event-ID 解析 + 分类 ①语法/②端点-sid 匹配（③④委托 `ReplayLog.replay` 保持冻结短路序）；v4 `slimapi.meta` 加性扩展字段（capabilities/epoch/seqBase，meta 帧本身不带 id）；周期维护循环 `replay_sweep_loop`（TTL GC + barrier GC + 空域 recycle，app.py wiring）；冻结四值 resync reason 域 `V4_RESYNC_REASONS`（生产 allowlist，非测试 oracle）。①②违规一律 **ignore+reset**（静默首连，不发 resync——客户端协议违规不是服务端状态变化，20-29 docstring）。

- **对外符号**（`__all__` `replay_wire.py:50-59`）：
  - `V4_RESYNC_REASONS: frozenset`（61-77）— rev-gate R3 BLOCKER-1 冻结 v4 `resync.reason` 值域（四值，从 replay_log 导入同源）；域外 legacy reason（subscriber_backpressure/token_memory_limit/session_idle/session_deleted…）在 v4 走静默 STOP 路线（断连本身是信号）。
  - `_EPOCH_RE`（82）/`_SEQ_RE`（83）— §7.1 语法：epoch 恰 16 小写 hex；seq 十进制（容忍前导零——值域是整数，79-81 注释）。
  - `_GLOBAL_SEGMENTS = 3`（89）— global id 恰 3 冒号段；token ≥4 段，sid 取 label 与尾部 epoch/seq 对之间的一切（rsplit 语义，86-88 注释：sid 含冒号仍可 round-trip）。
  - `META_CAPABILITY_KEYS: dict`（91-96）— v4 meta 帧能力广告 `{"sseReplay": True}`；常量 dict 单源供 meta 帧与 versions 端点共用防漂移。
  - `DEFAULT_SWEEP_INTERVAL_S = 60.0`（98-101）— 维护节拍（远低于 15min TTL 使空闲域收敛）；**不在 `__all__`**。
  - `sse_id_line(domain, epoch, seq) -> bytes`（104-113）— 生成 `id: {domain}:{epoch}:{seq}\n`（含尾换行）ASCII 编码；domain 即 log 域 key（"g" / "t:<sid>"）恰为 wire id 前缀段。
  - `frame_with_id(frame, domain, epoch, seq)`（116-123）— 已序列化 SSE 帧块前缀加 id 行；不重序列化帧本体（字节同一性）。
  - `parse_last_event_id(header, *, token_sid=None) -> (epoch,seq)|None`（126-166）— 分类 ①②：global 端点（token_sid=None）只收恰 3 段 `g:` 开头（151；`t:` id 到 /events 是跨端点违规，不管后续）；token 端点只收 ≥4 段 `t:` 且重组 sid == 路径 sid（157-162）；epoch/seq 正则终检（164）。**任何违规→None**（调用方按 ignore+reset 处理）。
  - `classify_reconnect(header, replay, *, domain, token_sid=None)`（169-209）— 完整 ①②③④：无头/①②违规→None（首连语义）；③④委托 `replay.replay()`（短路序与 outcome 计数单点保持，196-198 注释）。
  - `meta_v4_extension(epoch, seq_base) -> dict`（212-226）— B3b-4 加性三键 `capabilities/epoch/seqBase`；seqBase=连接时该域最大已发布 seq（首连后首个 id 帧= seqBase+1）；meta 帧自身无 id（§7.0 终裁②）。
  - `replay_sweep_loop(replay, *, interval_s, stop_event)`（229-282）— 周期维护协程：每 tick `sweep()`（TTL+barrier GC）→ 对帧数 0 的非全局域 `recycle_domain`（保 seq 壳/barrier）；best-effort（异常 warning 继续；RuntimeError=closed 竞态静默退出）；stop_event 唤醒即返回。

- **依赖**：标准库 `asyncio/re/typing`；内部 `from .replay_log import GLOBAL_DOMAIN, ReplayOutcome, RESYNC_*×4`（38-45）+ TYPE_CHECKING `ReplayLog`（47-48）；函数内延迟 `from ..logging_config import get_logger`（255，避启动成本/循环依赖）。

- **被依赖**（rg 反查）：
  - `src/oc_slimapi/app.py:35,447-455`（replay_sweep_loop lifespan wiring，`interval_s=_REPLAY_SWEEP_INTERVAL_S` + stop_event；LIFO 保证 sweep 先于 replay_log.close 停）。
  - `src/oc_slimapi/routes/events.py:15,115-118`（classify_reconnect 全局域）；`events.py:151-153`（meta_v4_extension）；`events.py:201-203`（frame_with_id 补发）。
  - `src/oc_slimapi/routes/token_stream.py:60,155-158`（classify + token_sid）；`token_stream.py:191-195`（meta seqBase=last_seq(token_domain(sid))）；`token_stream.py:275-276`（frame_with_id）。
  - `src/oc_slimapi/sse/global_hub.py:49,570`（sse_id_line 全局域 id 行）。
  - `src/oc_slimapi/sse/tokenstream/hub.py:91,1394`（sse_id_line token 域）；`hub.py:85-91`（imports）。
  - `src/oc_slimapi/sse/hub_types.py:25,311-317` 与 `src/oc_slimapi/sse/tokenstream/subscriber.py:20,444-489`（V4_RESYNC_REASONS 门控：v4 域外 reason → 仅 STOP 不发 resync）。
  - `src/oc_slimapi/routes/versions.py:53,87-95`（`**META_CAPABILITY_KEYS` 把 sseReplay 广告进 /slimapi/versions）。
  - 测试：`tests/test_sse_replay_wire.py`（导入同批常量做 oracle）。

- **状态/可变性**：**本模块完全无状态**——全部纯函数（sse_id_line/frame_with_id/parse/classify/meta_v4_extension）+ 一个协程（loop 状态仅局部）。所有可变状态在委托的 `ReplayLog`（见上卡）。`META_CAPABILITY_KEYS` 是模块级**可变 dict**（疑点 9）；`DEFAULT_SWEEP_INTERVAL_S` 与 app.py:91 `_REPLAY_SWEEP_INTERVAL_S` 双定义（疑点 10）。

- **错误路径**：
  - `parse_last_event_id`：一切①②违规→`None`（无异常；除疑点 2 的 int() 超长串边界）。
  - `classify_reconnect`：无头→None；①②→None；③④异常透传 replay 的 `ValueError`（after_seq 非法——parse 产物恒为非负 int，实际不可达）。
  - `sse_id_line`：`.encode("ascii")` 对非 ASCII sid 抛 `UnicodeEncodeError`（113）——生产两调用点均在 hub 的 try/except 内降级为 id-less fanout + warning（global_hub.py:565-569 / tokenstream hub.py:1390-1394）。
  - `replay_sweep_loop`：`asyncio.TimeoutError`→继续下一 tick（264-265）；`RuntimeError`（closed 竞态）→静默 return（278-280）；其他 `Exception`→`logger.warning` 继续（281-282）；closed 先查双保险（269-271）。

- **疑问点**（14 条）：
  1. **id 生成/解析对称性总检（任务点名）**（104-113 vs 145-166）：global 严格对称（生成恰 3 段 / 解析恰 3 段+`g` 首段，151）✓；token 生成 `t:<sid>:<epoch>:<seq>`，解析 ≥4 段+`t` 首段+`sid=":".join(parts[1:-2])`（157-159）——rsplit 固定尾部两段定界，**sid 含任意冒号 round-trip 无歧义**（测试 `test_sse_replay_wire.py:499`：`t:a:b:<epoch>:5` ↔ sid="a:b"）✓。**单向宽容不对称（方向安全）**：解析容忍 seq 前导零（83+166 `int()`）与 cursor=0，生成端永不产这两种形式（seq≥1 无前导零）——不会构成 round-trip 冲突。拒绝面完备：大写 hex、非数字 seq、段数错、错 label、跨端点、跨 sid 全→None ✓。
  2. **`int(seq_text)` 超长数字串抛 ValueError**（166）：`_SEQ_RE` 限定纯数字但**不限长度**；Python ≥3.11（pyproject `requires-python = ">=3.11"`）默认 int 最大 4300 位——`int("9"*5000)` 抛 `ValueError`，parse 未 catch → classify 冒泡（209）→ route handler 无此异常分支 → 潜在 500。畸形 header 本应 ignore+reset（139-141 语义），此处漏防（唯一触发面：恶意/损坏的超长 Last-Event-ID）。
  3. **空 sid 与非 ASCII sid 不设防**（159-162 / 113）：`token_domain("")="t:"`（replay_log.py:111-113 无校验）→ id `t::<epoch>:<seq>` 在 token_sid="" 时被接受（依赖路由层路径参数 sid 非空）；`sse_id_line` 的 ascii 编码对非 ASCII sid 抛错，hub 侧降级后 **v4 wire 出现无 id 业务帧**（§7.1 一致性破口，与 global_hub.py:553-554 "degrades to id-less fan-out" 有意降级同源）——降级可见性仅一条 warning。
  4. **classify_reconnect 的 domain 与 token_sid 无耦合校验**（169-209）：domain="g" 配 token_sid="s1" 会按 token 语法 parse 而查 "g" 域；消费侧 frame_with_id 也由 routes 重传 domain（events.py:201-203 / token_stream.py:275-276）而不用 `entry.domain`（ReplayEntry 自带域字段）——两处一致性纯靠调用约定，type-level 不防错配。
  5. **barrier 水位原子性/并发重连竞态（任务点名）**：本模块纯函数无状态，分类原子性= `ReplayLog.replay` 单 loop 原子（见上卡疑点 14/15）+ routes 的 classify(T0)→subscribe(T1) 顺序不变量 + meta seqBase 同在 handler 冻结（events.py:149-155 注释：防 T0/T1 间发布帧使 seqBase 超前于 replay plan）→ 客户端序列 meta→replay(≤last@T0)→queue(>last@T0) 严格递增，无重无漏 ✓。竞态残余面：**ReplayIgnoreReset 分支** seqBase=last ≪ 客户端 cursor，靠客户端主动倒退对齐（契约义务，服务端无强制）；若客户端不采纳将永久超前。
  6. **sweep 的 recycle 实效是 no-op**（273-277）：对 `domain_frame_count==0` 的域调 `recycle_domain`——帧已空（while 不执行）、域壳永不删、seq/barrier 本就保留 → 实际效果仅刷新 last_touch（无读者，上卡疑点 13）。「expired-domain recycle policy」在实现层是文档性行为；GLOBAL_DOMAIN 跳过（274-275）正确（共享序列）。
  7. **sweep loop 时序**（258-267）：先 sleep 后 sweep——首 tick 延迟一个 interval（60s 内过期帧不清理，可接受）；stop_event 唤醒后**不做最后一次 sweep** 即 return（266-267）——残留帧由 close() 清（app.py:433-441 LIFO 顺序保证）✓；`except asyncio.TimeoutError` 在 3.11+ 与内建 TimeoutError 合一，写法可达 ✓。
  8. **V4_RESYNC_REASONS 门控覆盖面**（61-77）：hub 侧两处门控（hub_types.py:311-317 / subscriber.py:444-489）把域外 reason 转 silent-STOP；但 **route 层直接 yield resync**（events.py:196-198 / token_stream.py:270-272）不经门控——安全仅因 replay_plan 的 reason 来自 log 层封闭四值 + v3 fallback 亦为 `RESYNC_RECONNECT_NO_REPLAY`（events.py:124-126 / token_stream.py:164-167）；无运行时 assert，log 层若加第五 reason 会未经门控直接上 wire——值域封闭性靠字面量纪律。
  9. **META_CAPABILITY_KEYS 可变性**（96）：普通 dict 而非 MappingProxy/frozenset——注释（91-95）自称为防漂移单源，防的是「两处字面量」漂移（versions.py:95 与 meta 共用 ✓），不防运行时篡改；低风险。
  10. **sweep 间隔双定义**（101 vs app.py:91 `_REPLAY_SWEEP_INTERVAL_S = 60.0`）：app.py 显式传参不用本模块默认值——两处 60.0 各自维护；且 `DEFAULT_SWEEP_INTERVAL_S` 不在 `__all__`（50-59），星号导入不可见。当前值一致，属 drift 隐患。
  11. **无头/空串双检冗余**（200-201 vs 143）：classify 与 parse 各查一次 `if not header`——空 Last-Event-ID 头等价无头（first-connect），测试 492-493 冻结；冗余无害。
  12. **meta 字段顺序契约靠调用方**（212-226）：docstring 承诺顺序 `subscriberId, tokens, capabilities, epoch, seqBase`——本函数只产后三键，顺序由 routes 的 `dict.update`（py3.7+ 有序 dict）保证 ✓；「首帧恰为 seqBase+1」承诺依赖 handler 冻结时序（疑点 5）。
  13. **sweep loop 持续性异常无退避**（268-282）：非 RuntimeError 持续抛（如注入 clock 故障）→ 每 60s 一条 warning，无退避/升级/自终止——故障被 best-effort 吞掉。
  14. **token_sid 参数语义重载**（127,146-163）：None=global 端点、非 None=token 端点——用参数存在性区分端点而非显式枚举；错配调用 `token_sid=None` + domain="t:x" 会按 global 语法解析 token id → 段数 4≠3 拒绝（碰巧 fail-safe）✓，反向错配见疑点 4。

---

### 附：两文件组合语义小结（审计关注项交叉确认）

- **短路序全链**：①语法+②端点/sid（replay_wire `parse_last_event_id`）→③epoch（replay_log:423，dominates）→④a barrier（434）→④b future（440）→④c expired（448-458）→④d gap（459-466 防御不可达）。顺序两处自洽验证通过（barrier≤last 恒成立故先于 future 无冲突；expired 先于 gap 且 gap 靠 head-only 不变式兜底）。
- **barrier 双写路径**：进程级上游丢失 `global_hub.py:413 write_barrier()`（全域含离线 token 域）+ per-sid 状态失效 `tokenstream/hub.py:1420 write_barrier(token_domain(sid))`；水位都取 last_seq 且单调（replay_log:491）。`token_hub.on_upstream_reconnect`（hub.py:2124-2185）清 accumulator 状态但**不**写 barrier（全域 barrier 已由 global_hub 先写）。
- **背压溢出帧入日志 × 窗口补发 × barrier** 三者组合：溢出帧占 seq 可补发（published 语义）；若溢出期间发生 invalidation（barrier 落位），cursor≤watermark 一律 `reconnect_no_replay` → 客户端 HTTP 全量对齐（v4-contract §7.2 冻结恢复路径）——屏障优先于补发，语义闭环一致。
- **在途重连 vs 环形覆盖**：`replay()` 的 tuple 快照（replay_log:444）+ frozen entry + handler 冻结 plan/meta（routes T0<T1）→ 已覆盖帧可安全补发，无「撤回已承诺帧」路径。
