# Stage B Implementation Spec v0.5 Delta

- **日期**: 2026-07-27
- **基线**: v0.4（`-stage-b-impl-spec-v0.4-delta.md`）+ fix-1 v0.4 实现
- **触发**: rev-ogpt 二审 6.5/10 NEEDS-FIX（新 CRITICAL 1 + MAJOR 2/3/4）
- **范围**: CRITICAL 1 + MAJOR 2 + MAJOR 3（sidecar 单方，不改 wire 形态）；MAJOR 4 待 ocdroid council，v0.5 不含

## v0.5 变更（叠加 v0.4，冲突处 v0.5 为准）

### K. CRITICAL 1 修复：messageEventSeq 改 per-session 全局单调序号
**问题**：v0.4 `_bump_message_seq` 淘汰 message 后重触及 seq=1，破坏单调（client 持 seq=10 → `1>10` false 漏检）+ ABA 304（重建三元组等于旧 known）+ per-part revision 从 0 覆盖 token hub。

**修复**：
- GlobalHub 新增 `self._session_event_seq: dict[str, int] = {}`（sessionID → 全局事件计数器）
- 每次 `message.part.updated` / `message.part.removed`（该 session 任何 message）→ `_session_event_seq[sid] += 1`
- 该 message 的 seq = `_session_event_seq[sid]` 当前值（赋给 `_part_state[sid][mid]["seq"]` + `content_revisions[mid]`）
- `_bump_message_seq` 改为：先 bump 全局 seq，再赋给 message（淘汰 message 不重置全局 seq；重触及 seq=当前全局值，远大于旧，单调）
- `resync_all()` 清 `_session_event_seq`（reconnect 归零，client 不信任 → R1）
- per-part revision 不受影响（独立计数，淘汰后重触及仍从 0——但 token hub 的 per-part revision 是 part-level dedup，重触及新 part=0 是正确的，**不覆盖** token hub 已有更高 revision：on_part_updated 仅当 key 存在才更新，新 part 是新 key）

**单调性证明**：全局 seq 单调递增；message seq = 赋值时的全局 seq 快照；同一 message 后续事件 → 全局 seq 更大 → message seq 更大。淘汰不重置全局 seq。reconnect 归零（client 不信任，既定）。

### L. MAJOR 2 修复：removal of unknown message 也产生 digest
**问题**：v0.4 `message.part.removed` 仅 `msg_entry is not None` 时推进；cap 淘汰后 message 未知 → removal 静默丢弃 → client 不重拉 → 永久保留已删 part。

**修复**：
- `message.part.removed`：即使 `_part_state[sid][mid]` 不存在（被淘汰/未知），也 bump 全局 seq + `entry.content_revisions[mid] = seq`
- client 收 digest → strict `>` 检测 → R1（/full/{mid}?known= 无缓存 fingerprint → 200，client 拿最新 parts 自愈）
- 若 `_part_state[sid][mid]` 存在：仍 pop partID（现有 v0.4 逻辑）+ bump seq
- 若不存在：仅 bump seq + content_revisions（不操作 parts map，因为本来就没有）

### M. MAJOR 3 修复：X-Message-Event-Seq body 前后稳定性检查
**问题**：v0.4 header 在 upstream await 前取样；body 拉取/转换期间 part event → header seq 不对应返回 body。

**修复**（`/full/{mid}` handler）：
- body 前：`seq_pre = fp[2] if (fp := hub.get_part_fingerprint(sid, mid)) else 0`
- body 后（transform 完成后、Response 构造前）：`seq_post = fp2[2] if (fp2 := hub.get_part_fingerprint(sid, mid)) else 0`
- `X-Message-Event-Seq`：
  - `seq_pre == seq_post` → 发 `seq_post`（可信快照）
  - `seq_pre != seq_post` → 发 `0`（不可信，client 视为无 baseline → R1）
- 304 短路路径不变（不涉及 body，fingerprint 已对齐）
- 注意：稳定性检查在 same event loop 无-await 区间外的两次读取间有 await（body 拉取），seq 可能变——这正是要检测的

### N. MAJOR 4（v0.5 不含，待 ocdroid council）
- `message.removed` wire tombstone：等 council 确认（/since 自愈 vs digest `removedMessages` 字段）
- v0.5 保留 v0.4 的 message.removed sidecar cache 清理（不回退），仅 wire tombstone 待定

### O. MINOR 5：测试补充
1. cap 淘汰后重触及 message：seq = 当前全局值（远大于旧），不归零；digest contentRevisions 严格 `>` 旧值
2. unknown message（被淘汰）的 message.part.removed：产生 digest（全局 seq）；client R1 → /full 200
3. X-Message-Event-Seq 稳定性：body 期间 seq 变化 → header 发 0；无变化 → 发 seq
4. skeleton 200 path 的 X-Message-Event-Seq（v0.4 已实现，补测试断言）
5. per-part revision 不覆盖：token hub 已有 part rev=5，_part_state 该 part 淘汰后重触及 → on_part_updated 新 part（不同 key）rev=0，不覆盖旧 key

## 文件改动（叠加 fix-1 v0.4）
- `src/oc_slimapi/sse/hub.py`: `_session_event_seq`；`_bump_message_seq` 改全局 seq；message.part.removed unknown 也 bump + digest；resync_all 清 `_session_event_seq`
- `src/oc_slimapi/routes/messages.py`: /full X-Message-Event-Seq 稳定性检查（body 前后比对）
- `tests/test_stage_b_part_revision.py`: O 测试补充

## 实施顺序
K（全局 seq）→ L（unknown removal）→ M（header 稳定性）→ O（测试）→ `./scripts/check.sh`
