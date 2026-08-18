"""B3b-1 — 有界环形重放日志（design-v4-sse-replay §3.4 + v4-contract §7.2）。

纯日志层语义：epoch 校验 / barrier / 窗口 / TTL / 环形覆盖 / tombstone 记账 /
config fail-closed / app 装配。

REPLAY-001~018 矩阵分割（§4）：
* 本文件落地 = 日志层语义（seq 分配、环形逐出、TTL、barrier 水位判定、
  tombstone 消耗 seq、域隔离、epoch 归类、count/bytes 界）。
* 归 B3b-2（SSE wire 层，本文件**不落**）：resync 帧下发 / id: 头生成 /
  Last-Event-ID ①语法②端点域解析 / meta 首帧与状态机 / heartbeat 无 id /
  tokens=1 → 400 / 上游断连 fanout resync / 背压断连重连。具体：
  REPLAY-001（meta/首帧线序）、002（wire 重放帧序）、003/004/005 的
  ①②类输入分流、006（服务端不发 snapshot 帧）、007/008（背压恢复）、
  009（tokens=1 400）、010（连接级 ID 无倒退断言）、011（双流 ID 域独立
  的 wire 侧）、013（meta additive）、014（heartbeat）、015（断连 fanout）。
* 本文件承接的矩阵 case（日志层等价物）：002/003/004/005/010/011/012/
  016/017/018 —— 见各 test 注释。
"""
from __future__ import annotations

import asyncio
import importlib
import re

import pytest

from oc_slimapi.sse.replay_log import (
    DEFAULT_REPLAY_MAX_BYTES,
    DEFAULT_REPLAY_MAX_COUNT,
    DEFAULT_REPLAY_TTL_S,
    FRAME_KIND_TOMBSTONE,
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayIgnoreReset,
    ReplayLog,
    ReplayResync,
    new_epoch,
    token_domain,
)

_EPOCH = "0123456789abcdef"  # valid 16-hex lowercase nonce for injection


class FakeClock:
    """Injectable monotonic clock — TTL tests never sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _log(**kwargs) -> tuple[ReplayLog, FakeClock]:
    clock = kwargs.pop("clock", None) or FakeClock()
    kwargs.setdefault("epoch", _EPOCH)
    log = ReplayLog(clock=clock, **kwargs)
    return log, clock


def _frames(outcome) -> tuple:
    assert isinstance(outcome, ReplayFrames), f"expected ReplayFrames, got {outcome!r}"
    return outcome.entries


def _resync(outcome, reason: str) -> None:
    assert isinstance(outcome, ReplayResync), f"expected ReplayResync, got {outcome!r}"
    assert outcome.reason == reason


# ---------------------------------------------------------------------------
# epoch（§7.1 / REPLAY-003 日志层）
# ---------------------------------------------------------------------------

def test_epoch_generated_is_16hex_lowercase():
    epoch = new_epoch()
    assert re.fullmatch(r"[0-9a-f]{16}", epoch)


def test_two_instances_distinct_epochs():
    # 随机 boot nonce：两实例（= 两次进程世界）异 epoch，且都是 16hex。
    a = ReplayLog(clock=FakeClock())
    b = ReplayLog(clock=FakeClock())
    assert a.epoch != b.epoch


@pytest.mark.parametrize("bad", ["xyz", "ABCDEFGHIJKLMNOP", "0123456789abcde", "0123456789abcdef0"])
def test_invalid_epoch_rejected(bad):
    with pytest.raises(ValueError):
        ReplayLog(epoch=bad)


def test_ctor_bound_validation():
    with pytest.raises(ValueError):
        ReplayLog(max_count=0)
    with pytest.raises(ValueError):
        ReplayLog(max_bytes=0)
    with pytest.raises(ValueError):
        ReplayLog(ttl_s=0)


# ---------------------------------------------------------------------------
# seq 分配 / 域模型（§7.1 / REPLAY-010/011 日志层）
# ---------------------------------------------------------------------------

def test_seq_starts_at_one_and_is_monotonic():
    log, _ = _log()
    assert [log.append(GLOBAL_DOMAIN, b"f").seq for _ in range(3)] == [1, 2, 3]


async def test_concurrent_append_assigns_unique_seqs():
    # asyncio 单线程模型下跨 task append 同一域：seq 唯一无空洞，
    # 恰好覆盖 1..N（REPLAY-010 日志层前置）。
    log, _ = _log()

    async def one(_i: int) -> int:
        await asyncio.sleep(0)
        return log.append(GLOBAL_DOMAIN, b"x").seq

    seqs = sorted(await asyncio.gather(*(one(i) for i in range(200))))
    assert seqs == list(range(1, 201))


def test_append_rejects_invalid_domain():
    log, _ = _log()
    with pytest.raises(ValueError):
        log.append("", b"f")


def test_global_and_sid_domains_have_independent_seq_counters():
    log, _ = _log()
    g1 = log.append(GLOBAL_DOMAIN, b"g1")
    t1 = log.append(token_domain("ses_A"), b"t1")
    g2 = log.append(GLOBAL_DOMAIN, b"g2")
    assert (g1.seq, t1.seq, g2.seq) == (1, 1, 2)


def test_per_sid_domain_isolation():
    # REPLAY-011 日志层：sid A 的帧不入 sid B 域，也不入全局域。
    log, _ = _log()
    for i in range(3):
        log.append(token_domain("ses_A"), f"a{i}".encode())
        log.append(token_domain("ses_B"), f"b{i}".encode())
        log.append(GLOBAL_DOMAIN, f"g{i}".encode())
    a = _frames(log.replay(token_domain("ses_A"), 0, _EPOCH))
    b = _frames(log.replay(token_domain("ses_B"), 0, _EPOCH))
    g = _frames(log.replay(GLOBAL_DOMAIN, 0, _EPOCH))
    assert [e.payload for e in a] == [b"a0", b"a1", b"a2"]
    assert [e.payload for e in b] == [b"b0", b"b1", b"b2"]
    assert [e.payload for e in g] == [b"g0", b"g1", b"g2"]


def test_token_domain_helper_prefix():
    assert token_domain("ses_X") == "t:ses_X"
    assert GLOBAL_DOMAIN == "g"


def test_domain_lazy_creation_and_introspection():
    log, _ = _log()
    assert not log.has_domain(token_domain("ses_A"))
    assert log.domain_count() == 0
    log.append(token_domain("ses_A"), b"x")
    assert log.has_domain(token_domain("ses_A"))
    assert log.domain_count() == 1
    assert log.last_seq(token_domain("ses_A")) == 1
    assert log.last_seq(token_domain("ses_never")) == 0
    assert log.window_start(token_domain("ses_never")) is None


# ---------------------------------------------------------------------------
# replay 分类（③epoch ④窗口 — §7.2 / REPLAY-002~005 日志层）
# ---------------------------------------------------------------------------

def test_replay_in_window_strictly_increasing():
    # REPLAY-002 日志层：窗口内补帧，seq 严格递增，无 resync。
    log, _ = _log()
    for i in range(5):
        log.append(GLOBAL_DOMAIN, f"f{i}".encode())
    entries = _frames(log.replay(GLOBAL_DOMAIN, 2, _EPOCH))
    assert [e.seq for e in entries] == [3, 4, 5]
    assert [e.payload for e in entries] == [b"f2", b"f3", b"f4"]


def test_replay_up_to_date_cursor_returns_empty_not_resync():
    log, _ = _log()
    for i in range(3):
        log.append(GLOBAL_DOMAIN, b"f")
    assert _frames(log.replay(GLOBAL_DOMAIN, 3, _EPOCH)) == ()


def test_replay_epoch_mismatch_dominates():
    # 优先级③：epoch 不匹配 → epoch_changed，先于域/窗口判定（含未知域）。
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"f")
    _resync(log.replay(GLOBAL_DOMAIN, 1, "ffffffffffffffff"), "epoch_changed")
    _resync(log.replay(token_domain("never_created"), 9, "0000000000000000"), "epoch_changed")
    _resync(log.replay(GLOBAL_DOMAIN, 0, None), "epoch_changed")


def test_replay_future_seq_ignore_reset():
    # 同 epoch 且 seq > 已发布 max → 忽略 + 按首连（REPLAY-005 日志层）。
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"f")
    outcome = log.replay(GLOBAL_DOMAIN, 99, _EPOCH)
    assert isinstance(outcome, ReplayIgnoreReset)
    assert outcome.seq == 99


def test_replay_unknown_domain_with_zero_cursor_is_empty():
    # 从未创建的域 + cursor 0：等价空窗口（首连语义），非 future。
    log, _ = _log()
    assert _frames(log.replay(token_domain("ses_new"), 0, _EPOCH)) == ()


def test_replay_rejects_malformed_after_seq():
    log, _ = _log()
    with pytest.raises(ValueError):
        log.replay(GLOBAL_DOMAIN, -1, _EPOCH)
    with pytest.raises(ValueError):
        log.replay(GLOBAL_DOMAIN, True, _EPOCH)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# count 界（环形覆盖 — §3.4）
# ---------------------------------------------------------------------------

def test_count_bound_ring_overwrites_oldest():
    log, _ = _log(max_count=4)
    for i in range(5):
        log.append(GLOBAL_DOMAIN, f"f{i}".encode())
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 4
    assert log.window_start(GLOBAL_DOMAIN) == 2  # 环形下界推进
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")  # 被覆盖位置
    entries = _frames(log.replay(GLOBAL_DOMAIN, 1, _EPOCH))
    assert [e.seq for e in entries] == [2, 3, 4, 5]


def test_count_bound_default_2049_frames():
    # 设计默认值实测：2049 帧 → 最旧（seq 1）覆盖。
    log, _ = _log()  # 默认 max_count=2048
    for i in range(2049):
        log.append(GLOBAL_DOMAIN, b"x")
    assert DEFAULT_REPLAY_MAX_COUNT == 2048
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 2048
    assert log.window_start(GLOBAL_DOMAIN) == 2
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")
    entries = _frames(log.replay(GLOBAL_DOMAIN, 1, _EPOCH))
    assert len(entries) == 2048
    assert entries[0].seq == 2 and entries[-1].seq == 2049


# ---------------------------------------------------------------------------
# bytes 界（进程级总记账 — §3.4）
# ---------------------------------------------------------------------------

def test_bytes_bound_evicts_oldest_same_domain():
    log, _ = _log(max_bytes=100)
    for i in range(3):
        log.append(GLOBAL_DOMAIN, b"x" * 40)  # 3×40=120 > 100 → 最旧覆盖
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 2
    assert log.window_start(GLOBAL_DOMAIN) == 2
    assert log.total_bytes == 80
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")
    assert [e.seq for e in _frames(log.replay(GLOBAL_DOMAIN, 1, _EPOCH))] == [2, 3]


def test_bytes_bound_is_process_wide_across_domains():
    # bytes 是进程级总界：全局域旧帧可被 per-sid 域新帧的记账逐出。
    log, _ = _log(max_bytes=100)
    log.append(GLOBAL_DOMAIN, b"a" * 40)             # order 1
    log.append(token_domain("ses_A"), b"b" * 40)     # order 2
    assert log.total_bytes == 80
    log.append(token_domain("ses_A"), b"c" * 40)     # 120 > 100 → 全局最旧（g:1）逐出
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 0
    assert log.domain_frame_count(token_domain("ses_A")) == 2
    assert log.total_bytes == 80
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")


def test_bytes_bound_single_oversize_frame_retained():
    # 单帧超总预算：仍记录（日志不丢弃刚接受的帧），其余清空。
    log, _ = _log(max_bytes=10)
    log.append(GLOBAL_DOMAIN, b"s" * 50)
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 1
    assert log.total_bytes == 50
    assert [e.seq for e in _frames(log.replay(GLOBAL_DOMAIN, 0, _EPOCH))] == [1]


def test_bytes_accounting_accuracy_with_mixed_sizes():
    log, _ = _log(max_bytes=1000)
    sizes = [10, 20, 30, 40]
    for s in sizes:
        log.append(GLOBAL_DOMAIN, b"x" * s)
    assert log.total_bytes == sum(sizes)
    # dict payload 走序列化记账（非 bytes/str 路径）
    log.append(token_domain("ses_A"), {"reason": "digest"})
    assert log.total_bytes == sum(sizes) + log._domains[token_domain("ses_A")].entries[0].size


# ---------------------------------------------------------------------------
# TTL（帧过期 — §3.4 / REPLAY-004 日志层）
# ---------------------------------------------------------------------------

def test_ttl_expired_resyncs_replay_expired():
    log, clock = _log(ttl_s=60)
    log.append(GLOBAL_DOMAIN, b"f")
    clock.advance(61)
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 0  # 惰性逐出已清空


def test_ttl_not_expired_replays_normally():
    log, clock = _log(ttl_s=60)
    log.append(GLOBAL_DOMAIN, b"f")
    clock.advance(59)
    assert [e.seq for e in _frames(log.replay(GLOBAL_DOMAIN, 0, _EPOCH))] == [1]


def test_ttl_exact_age_still_replayable():
    # 恰好 ttl 龄仍可重放（严格大于才逐出）；+0.001 后过期。
    log, clock = _log(ttl_s=60)
    log.append(GLOBAL_DOMAIN, b"f")
    clock.advance(60)
    assert [e.seq for e in _frames(log.replay(GLOBAL_DOMAIN, 0, _EPOCH))] == [1]
    clock.advance(0.001)
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")


def test_ttl_partial_window():
    # 2 旧 + 2 新：cursor 0 → 过期；cursor 2 → 正常补 3、4。
    log, clock = _log(ttl_s=60)
    log.append(GLOBAL_DOMAIN, b"f1")
    log.append(GLOBAL_DOMAIN, b"f2")
    clock.advance(61)
    log.append(GLOBAL_DOMAIN, b"f3")
    log.append(GLOBAL_DOMAIN, b"f4")
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_expired")
    entries = _frames(log.replay(GLOBAL_DOMAIN, 2, _EPOCH))
    assert [e.seq for e in entries] == [3, 4]


def test_sweep_evicts_expired_and_returns_count():
    log, clock = _log(ttl_s=60)
    for i in range(3):
        log.append(GLOBAL_DOMAIN, f"f{i}".encode())
    clock.advance(61)
    log.append(token_domain("ses_A"), b"fresh")  # 未过期
    assert log.sweep() == 3
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 0
    assert log.domain_frame_count(token_domain("ses_A")) == 1


def test_append_lazily_ttl_evicts_hot_domain():
    # append 路径对触及域做惰性 TTL 逐出，界不依赖周期 sweep。
    log, clock = _log(ttl_s=60)
    log.append(GLOBAL_DOMAIN, b"old")
    clock.advance(61)
    log.append(GLOBAL_DOMAIN, b"new")
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 1
    assert log.window_start(GLOBAL_DOMAIN) == 2


# ---------------------------------------------------------------------------
# barrier（S-B01④ / REPLAY-015/017/018 日志层）
# ---------------------------------------------------------------------------

def test_barrier_write_covers_global_and_all_sid_domains():
    # §7.2 冻结写入范围：全局域 + 当前 epoch 内全部已创建 per-sid 域
    # （不限在线订阅者）；未创建的域不获 barrier。
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"g1")
    log.append(GLOBAL_DOMAIN, b"g2")
    log.append(token_domain("ses_A"), b"a1")
    log.write_barrier()
    assert log.barrier_watermark(GLOBAL_DOMAIN) == 2
    assert log.barrier_watermark(token_domain("ses_A")) == 1
    assert log.barrier_watermark(token_domain("ses_not_created")) is None


def test_barrier_boundary_triple():
    # REPLAY-017 边界三连（日志层）：watermark-1 / watermark 拦截，
    # watermark+1 正常窗口判定。barrier 后的新帧（101/102 语义）对
    # cursor ≤ 水位**不得**补发（禁跨 barrier 补帧）。
    log, _ = _log()
    for _ in range(3):
        log.append(GLOBAL_DOMAIN, b"pre")
    log.write_barrier()  # watermark = 3
    log.append(GLOBAL_DOMAIN, b"post1")  # seq 4
    log.append(GLOBAL_DOMAIN, b"post2")  # seq 5
    _resync(log.replay(GLOBAL_DOMAIN, 2, _EPOCH), "reconnect_no_replay")
    _resync(log.replay(GLOBAL_DOMAIN, 3, _EPOCH), "reconnect_no_replay")
    entries = _frames(log.replay(GLOBAL_DOMAIN, 4, _EPOCH))
    assert [e.seq for e in entries] == [5]


def test_barrier_monotonic_across_loss_rounds():
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"f1")
    log.write_barrier()
    assert log.barrier_watermark(GLOBAL_DOMAIN) == 1
    log.append(GLOBAL_DOMAIN, b"f2")
    log.write_barrier()  # 多轮断连：水位只增
    assert log.barrier_watermark(GLOBAL_DOMAIN) == 2
    _resync(log.replay(GLOBAL_DOMAIN, 1, _EPOCH), "reconnect_no_replay")


def test_barrier_survives_count_and_ttl_eviction():
    # barrier 是元数据，不受 count/bytes/TTL 逐出：域清空后仍拦截。
    log, clock = _log(max_count=2, ttl_s=60)
    for i in range(4):
        log.append(GLOBAL_DOMAIN, f"f{i}".encode())
    log.write_barrier()  # watermark = 4（此时 count 已留下 3、4）
    clock.advance(120)
    log.sweep()  # TTL 清空整个域
    assert log.domain_frame_count(GLOBAL_DOMAIN) == 0
    assert log.barrier_watermark(GLOBAL_DOMAIN) == 4
    _resync(log.replay(GLOBAL_DOMAIN, 4, _EPOCH), "reconnect_no_replay")


def test_barrier_gc_only_when_window_strictly_passed():
    # 窗口下界严格越过水位（entries[0].seq > wm）→ barrier 冗余可删；
    # 恰好相等（seq == wm）→ 保留（水位帧本身须拦截）。
    log, _ = _log()
    for _ in range(4):
        log.append(GLOBAL_DOMAIN, b"f")
    log.write_barrier()  # wm = 4
    log.append(GLOBAL_DOMAIN, b"post")  # seq 5 → 严格越过成为可能
    while log.window_start(GLOBAL_DOMAIN) <= 4:
        log._drop_head(log._domains[GLOBAL_DOMAIN])
    assert log.window_start(GLOBAL_DOMAIN) == 5
    log.sweep()
    assert log.barrier_watermark(GLOBAL_DOMAIN) is None
    # cursor 3（≤ 旧水位）：barrier 已删 → 不再 reconnect_no_replay，
    # 落入窗口判定 → replay_expired。
    _resync(log.replay(GLOBAL_DOMAIN, 3, _EPOCH), "replay_expired")
    # 相等情形：window_start == wm → 保留
    log2, _ = _log()
    for i in range(4):
        log2.append(GLOBAL_DOMAIN, f"f{i}".encode())
    log2.write_barrier()
    while log2.window_start(GLOBAL_DOMAIN) < 4:
        log2._drop_head(log2._domains[GLOBAL_DOMAIN])
    assert log2.window_start(GLOBAL_DOMAIN) == 4
    log2.sweep()
    assert log2.barrier_watermark(GLOBAL_DOMAIN) == 4
    _resync(log2.replay(GLOBAL_DOMAIN, 4, _EPOCH), "reconnect_no_replay")


def test_barrier_write_single_domain_no_create():
    # 定向 barrier 写入：不强制创建域（未创建域 = barrier 后新建域）。
    log, _ = _log()
    log.append(token_domain("ses_A"), b"a1")
    log.write_barrier(token_domain("ses_B"))  # 不存在 → no-op
    assert log.barrier_watermark(token_domain("ses_B")) is None
    log.write_barrier(token_domain("ses_A"))
    assert log.barrier_watermark(token_domain("ses_A")) == 1


def test_recycle_domain_keeps_seq_counter_and_barrier():
    # REPLAY-018：域回收（TTL 过期/长期无订阅者）保留失效水位，
    # 同 epoch 旧 cursor 不得按首连/空日志处理。
    log, _ = _log()
    for _ in range(3):
        log.append(token_domain("ses_A"), b"f")
    log.write_barrier()  # wm = 3
    log.append(token_domain("ses_A"), b"post1")  # seq 4
    log.append(token_domain("ses_A"), b"post2")  # seq 5 → last=5 > wm
    assert log.recycle_domain(token_domain("ses_A")) is True
    assert log.domain_frame_count(token_domain("ses_A")) == 0
    assert log.total_bytes == 0
    assert log.barrier_watermark(token_domain("ses_A")) == 3
    _resync(log.replay(token_domain("ses_A"), 3, _EPOCH), "reconnect_no_replay")
    _resync(log.replay(token_domain("ses_A"), 1, _EPOCH), "reconnect_no_replay")
    # 水位以上、已发布 max 以下的 cursor → replay_expired（非首连）；
    # 超过 last（回收不重置计数器）→ future 忽略重置。
    _resync(log.replay(token_domain("ses_A"), 4, _EPOCH), "replay_expired")
    outcome = log.replay(token_domain("ses_A"), 9, _EPOCH)
    assert isinstance(outcome, ReplayIgnoreReset)  # 9 > last(5) → future
    assert log.recycle_domain(token_domain("ses_never")) is False


def test_recycle_then_append_continues_seq_no_regression():
    # REPLAY-010 日志层：回收后 seq 计数器不重置（ID 无倒退）。
    log, _ = _log()
    log.append(token_domain("ses_A"), b"f1")
    log.recycle_domain(token_domain("ses_A"))
    entry = log.append(token_domain("ses_A"), b"f2")
    assert entry.seq == 2
    assert log.last_seq(token_domain("ses_A")) == 2


# ---------------------------------------------------------------------------
# tombstone（§7.2 / REPLAY-012 日志层）
# ---------------------------------------------------------------------------

def test_tombstone_consumes_seq_and_replays_with_id():
    # token 域 tombstone：照常消耗 seq、以 message.removed 帧回放（本层
    # 只记 kind，帧形接线归 B3b-2）→ 序列无空洞。
    log, _ = _log()
    log.append(token_domain("ses_A"), b"delta-1")
    tomb = log.append(
        token_domain("ses_A"),
        {"sessionID": "ses_A", "messageID": "msg_1"},
        kind=FRAME_KIND_TOMBSTONE,
    )
    after = log.append(token_domain("ses_A"), b"delta-2")
    assert (tomb.seq, after.seq) == (2, 3)
    assert tomb.is_tombstone
    entries = _frames(log.replay(token_domain("ses_A"), 0, _EPOCH))
    assert [e.kind for e in entries] == ["business", "tombstone", "business"]
    assert entries[1].payload == {"sessionID": "ses_A", "messageID": "msg_1"}


# ---------------------------------------------------------------------------
# 防御分支 / 指标 / 关闭
# ---------------------------------------------------------------------------

def test_gap_defensive_branch_on_non_contiguous_window():
    # 构造内部空洞（公开 API 不可达——逐出只从头端；防御分支验证：
    # 腐败窗口须 fail 为 replay_gap 而非默默服务带洞"重放"）。
    log, _ = _log()
    for i in range(4):
        log.append(GLOBAL_DOMAIN, f"f{i}".encode())
    state = log._domains[GLOBAL_DOMAIN]
    state.entries.remove(state.entries[1])  # 挖掉 seq 2
    _resync(log.replay(GLOBAL_DOMAIN, 0, _EPOCH), "replay_gap")


def test_outcome_counters_and_metrics_snapshot():
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"f1")
    log.replay(GLOBAL_DOMAIN, 0, _EPOCH)             # replayed
    log.replay(GLOBAL_DOMAIN, 1, _EPOCH)             # up_to_date
    log.replay(GLOBAL_DOMAIN, 9, _EPOCH)             # ignore_reset
    log.replay(GLOBAL_DOMAIN, 0, "ffffffffffffffff")  # epoch_changed
    log.write_barrier()
    log.replay(GLOBAL_DOMAIN, 1, _EPOCH)             # reconnect_no_replay
    counts = log.replay_outcomes_total
    assert counts["replayed"] == 1 and counts["up_to_date"] == 1
    assert counts["ignore_reset"] == 1 and counts["epoch_changed"] == 1
    assert counts["reconnect_no_replay"] == 1
    snap = log.metrics_snapshot()
    assert snap["domains"] == 1 and snap["frames"] == 1
    assert snap["barriers"] == 1 and snap["bytes"] == len(b"f1")


def test_close_clears_state_and_append_fails_loud():
    log, _ = _log()
    log.append(GLOBAL_DOMAIN, b"f")
    log.close()
    log.close()  # idempotent
    assert log.domain_count() == 0 and log.total_bytes == 0
    with pytest.raises(RuntimeError):
        log.append(GLOBAL_DOMAIN, b"f")


# ---------------------------------------------------------------------------
# config fail-closed（OC_SLIMAPI_REPLAY_*）
# ---------------------------------------------------------------------------

def test_config_replay_defaults():
    from oc_slimapi.config import Settings

    s = Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
    )
    assert s.replay_max_count == 2048
    assert s.replay_max_bytes_kb == 65536
    assert s.replay_ttl_s == 900.0
    assert DEFAULT_REPLAY_MAX_COUNT == 2048
    assert DEFAULT_REPLAY_MAX_BYTES == 64 * 1024 * 1024
    assert DEFAULT_REPLAY_TTL_S == 900.0


def test_config_replay_env_override(monkeypatch):
    import oc_slimapi.config as config_mod

    monkeypatch.setenv("OC_SLIMAPI_REPLAY_COUNT", "8")
    monkeypatch.setenv("OC_SLIMAPI_REPLAY_BYTES_KB", "16")
    monkeypatch.setenv("OC_SLIMAPI_REPLAY_TTL_S", "30")
    reloaded = importlib.reload(config_mod)
    s = reloaded.Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
    )
    assert (s.replay_max_count, s.replay_max_bytes_kb, s.replay_ttl_s) == (8, 16, 30.0)
    # 收尾 reload 必须在清掉 env 之后做，否则重载的类默认值带着测试 env
    # 泄漏给后续测试（dataclass 字段默认值在类定义时求值）。
    for name in (
        "OC_SLIMAPI_REPLAY_COUNT",
        "OC_SLIMAPI_REPLAY_BYTES_KB",
        "OC_SLIMAPI_REPLAY_TTL_S",
    ):
        monkeypatch.delenv(name)
    importlib.reload(config_mod)


def test_config_replay_env_malformed_int_fails(monkeypatch):
    import oc_slimapi.config as config_mod

    monkeypatch.setenv("OC_SLIMAPI_REPLAY_COUNT", "abc")
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_REPLAY_COUNT"):
        importlib.reload(config_mod)
    monkeypatch.delenv("OC_SLIMAPI_REPLAY_COUNT")
    importlib.reload(config_mod)


@pytest.mark.parametrize("field,value", [
    ("replay_max_count", 0),
    ("replay_max_count", -1),
    ("replay_max_bytes_kb", 0),
    ("replay_ttl_s", 0),
    ("replay_ttl_s", -5.0),
])
def test_config_replay_fail_closed_validation(field, value):
    from oc_slimapi.config import Settings

    s = Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        **{field: value},
    )
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_REPLAY_"):
        s.validate()


# ---------------------------------------------------------------------------
# rev-gate MAJOR-1: 非有限 TTL（nan/inf）必须两层 fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_replay_log_ctor_rejects_non_finite_ttl(ttl):
    with pytest.raises(ValueError, match="finite"):
        ReplayLog(epoch="0" * 16, ttl_s=ttl)


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_settings_validation_rejects_non_finite_ttl(ttl):
    from oc_slimapi.config import Settings

    s = Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        replay_ttl_s=ttl,
    )
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_REPLAY_TTL_S"):
        s.validate()


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "+inf"])
def test_settings_env_non_finite_ttl_fails_closed(monkeypatch, raw):
    """env 注入的非有限 TTL（含 nan 使 age>ttl 恒 False 从而静默禁用
    TTL 的攻击面）必须在 validate() 处 RuntimeError。"""
    import oc_slimapi.config as config_mod

    monkeypatch.setenv("OC_SLIMAPI_REPLAY_TTL_S", raw)
    reloaded = importlib.reload(config_mod)
    s = reloaded.Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
    )
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_REPLAY_TTL_S"):
        s.validate()
    monkeypatch.delenv("OC_SLIMAPI_REPLAY_TTL_S")
    importlib.reload(config_mod)


# ---------------------------------------------------------------------------
# app 装配（app.state 键约定：replay_epoch / replay_log — B3b-2 接口）
# ---------------------------------------------------------------------------

def _test_settings(tmp_path, **overrides):
    from oc_slimapi.config import Settings

    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        smoke_session_id=None,
        access_log_enabled=True,
        access_log_dir=str(tmp_path),
        traffic_snapshot_enabled=False,
        traffic_metrics_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _patch_lifespan(monkeypatch, tmp_path, **overrides):
    from oc_slimapi import app as app_mod

    monkeypatch.setattr(app_mod, "settings", _test_settings(tmp_path, **overrides))

    async def _noop_smoke(app):
        app.state.smoke_status = "not_run"
        app.state.schema_degraded = False

    monkeypatch.setattr(app_mod, "smoke", _noop_smoke)


async def test_lifespan_wires_replay_log(monkeypatch, tmp_path):
    from fastapi import FastAPI

    from oc_slimapi.app import lifespan

    _patch_lifespan(monkeypatch, tmp_path)
    app = FastAPI()
    async with lifespan(app):
        # 稳定键名（B3b-2 接口约定）
        assert re.fullmatch(r"[0-9a-f]{16}", app.state.replay_epoch)
        assert isinstance(app.state.replay_log, ReplayLog)
        assert app.state.replay_log.epoch == app.state.replay_epoch
        # 配置注入（默认值）
        assert app.state.replay_log.max_count == 2048
        assert app.state.replay_log.max_bytes == 65536 * 1024
        assert app.state.replay_log.ttl_s == 900.0
        # 未接线 hub：日志层独立可操作（纯数据结构）
        entry = app.state.replay_log.append(GLOBAL_DOMAIN, b"f")
        assert entry.seq == 1
    # 关闭清理：退出 lifespan 后 close()
    assert app.state.replay_log.domain_count() == 0
    with pytest.raises(RuntimeError):
        app.state.replay_log.append(GLOBAL_DOMAIN, b"f")


async def test_lifespan_two_runs_distinct_epochs(monkeypatch, tmp_path):
    # 进程级 epoch：两次 lifespan（= 两次进程世界）异 epoch（重启必换）。
    from fastapi import FastAPI

    from oc_slimapi.app import lifespan

    _patch_lifespan(monkeypatch, tmp_path)
    epochs = []
    for _ in range(2):
        app = FastAPI()
        async with lifespan(app):
            epochs.append(app.state.replay_epoch)
    assert epochs[0] != epochs[1]
    assert all(re.fullmatch(r"[0-9a-f]{16}", e) for e in epochs)


async def test_lifespan_replay_config_flows_through(monkeypatch, tmp_path):
    from fastapi import FastAPI

    from oc_slimapi.app import lifespan

    _patch_lifespan(
        monkeypatch, tmp_path,
        replay_max_count=7, replay_max_bytes_kb=3, replay_ttl_s=12.0,
    )
    app = FastAPI()
    async with lifespan(app):
        assert app.state.replay_log.max_count == 7
        assert app.state.replay_log.max_bytes == 3 * 1024
        assert app.state.replay_log.ttl_s == 12.0
