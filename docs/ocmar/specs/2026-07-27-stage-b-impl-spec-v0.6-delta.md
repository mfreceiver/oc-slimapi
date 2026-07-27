# Stage B Implementation Spec v0.6 Delta

- **日期**: 2026-07-27
- **基线**: v0.5（`-v0.5-delta.md`）+ fix-1 v0.5 实现
- **触发**: rev-ogpt 三审 7.7/10 NEEDS-FIX（CRITICAL 1 + MAJOR 2/3 闭合；新 MAJOR per_part_rev 回退）+ MAJOR 4 双方冻结方案 C
- **范围**: Q（per_part_rev 修复，sidecar 内部）+ P（MAJOR 4 tombstone，双方冻结）+ R（测试）

## v0.6 变更（叠加 v0.5）

### Q. 新 MAJOR 修复：per_part_revision 独立递增（不改 wire）
**问题**：GlobalHub `_part_state` 淘汰 message entry 后，同一既有 PartKey（同 part 继续追加，非 partID 复用）再 `message.part.updated` → `_bump_message_seq` 重建 `parts:{}` → 该 part 当新 part `per_part_rev=0` → `on_part_updated(part_revision=0)` 覆盖 TokenStreamHub `_part_revisions[key]`（原本可能=5）→ client strict `>` 漏帧/完成标记。

**修复**：TokenStreamHub 独立维护 per_part_revision，不依赖 GlobalHub 传入：
- **Option B（per-frame revision）**：`on_part_updated(props)` 不再自增 revision。GlobalHub.publish 调 `on_part_updated(props)` 不传 part_revision（token hub 忽略参数）。revision 在**每帧发射时**由 `_next_part_revision(key)` 递增（Per-PartKey increment-and-return）——每个具有独立 delivery 语义的 token 帧（`message.part.snapshot{done:false}` / `message.part.delta` / `message.part.snapshot{done:true}` / `message.part.snapshot{truncated:true}`）获得唯一递增的 `partEventRevision`。
- `_part_revisions[key]` 存储该 PartKey 的当前 revision 数值（非仅"活跃跟踪"）。`_next_part_revision(key)` 方法对相应 key 做 increment-and-return：key 已存在 → `+=1` 后返回；新 key → 置 0 后返回。每次帧发射时调用 `_next_part_revision(key)`，保证该 part 每帧获得唯一递增 revision。
- part 退役（`drop_part`）仍 `_part_revisions.pop(key)`（清除该 part 的 revision 状态）；同一 PartKey 后续重触及（同 partID 被重建）时 revision 从 0 重新开始——因 part 已结束，新生命周期归零正确。
- `on_upstream_reconnect()` **保留** `_part_revisions`（不清除，CRITICAL 1 修复：重连后已有 part 的 revision 序列不中断，客户端 strict `>` 可继续）。
- GlobalHub `_part_state[sid][mid]["parts"]` 的 per_part_rev 值不再被消费（冗余但有记录可删除；fingerprint 只用 keys）。

**正确性**：per-PartKey per-frame revision 严格单调（同一 key 的 `_next_part_revision(key)` 每次 +1）。`drop_part` 清 key 后同 key 归零（part 生命周期已结束，归零合理）。`on_upstream_reconnect` 不清 `_part_revisions` → 已有 part 的 revision 序列不中断。

### P. MAJOR 4：message.removed token stream tombstone + 重放队列（双方冻结方案 C）

#### P.1 publish message.removed 路由（hub.py）
- GlobalHub.publish `message.removed`（props `{sessionID, messageID}` 扁平）：
  - 清 `_part_state[sid][mid]`（v0.4 现有）
  - 路由 TokenStreamHub：`self._token_hub.on_message_removed(sid, mid)`

#### P.2 TokenStreamHub on_message_removed + 重放队列（tokenstream/hub.py）
- `on_message_removed(sid, mid)`：
  - 发 `message.removed` 帧给该 sid 所有 token subscribers（`_fanout_frame`-like，但 event=message.removed）
  - 加入重放队列：`self._removed_messages[(sid, mid)] = _now_ms()`（OrderedDict，插入序）
- 重放队列 `self._removed_messages: OrderedDict[tuple[str,str], int] = {}`：
  - **全局** cap 1000（FIFO 淘汰最旧，`popitem(last=False)` 当超限）
  - **TTL 24h**（清理挂 `ttl_sweep` 或单独 tick：删 `now - ts > 24h` 的 entry）
- `_fanout_message_removed(sid, mid)`：`_message_removed_frame(sid, mid)` → 该 sid 所有 subs

#### P.3 reconnect 重放（attach_subscriber handshake）
- 现状（tokenstream/hub.py attach_subscriber）：① server.connected → ② flush_sid → ③ snapshot live → ④ fanout
- 改为：① server.connected → ② **message.removed 批量重放**（该 sid 未过期 tombstones，按 ts 排序）→ ③ flush_sid → ④ snapshot live → ⑤ fanout
- 重放：遍历 `_removed_messages`，过滤 `key[0]==sid` 且未过期，逐个 `_message_removed_frame` 发给新 sub（sub 尚未进 fanout，直接 sub.put）

#### P.4 frames（tokenstream/frames.py）
- `_message_removed_frame(sid, mid)`：`sse_frame({"sessionID": sid, "messageID": mid}, event="message.removed")`

#### P.5 resync_all 关系
- `resync_all` 清 `_part_state` + `_session_event_seq`，**不清 `_removed_messages`**（重放队列专为 reconnect 服务）
- 全局 cap 1000 + TTL 24h 限制增长

### R. 测试（MINOR + MAJOR 4）
1. **Q 真实淘汰不回退**：m1/p1 推到 token rev=5 → 用其他 message 触发 500 淘汰 m1 → 重发 m1/p1 message.part.updated → token 帧 partEventRevision=6（独立递增，不归零）
2. **P.2 message.removed 帧**：publish message.removed → token subs 收 message.removed 帧 {sessionID, messageID}
3. **P.2 重放队列 cap**：1001 个 message.removed → 队列保持 1000（FIFO 淘汰最旧）
4. **P.2 重放队列 TTL**：24h+ 旧 entry 清理
5. **P.3 reconnect 重放**：message.removed 发生 → token sub 断开重连 → attach 后收 server.connected → message.removed 重放 → snapshot
6. **P.3 重放时序**：server.connected 先于 message.removed 重放先于 snapshot
7. **P.5 resync_all 不清重放队列**：resync 后重放队列保留

## 文件改动（叠加 fix-1 v0.5）
- `src/oc_slimapi/sse/hub.py`: publish message.removed 路由 token hub on_message_removed
- `src/oc_slimapi/sse/tokenstream/hub.py`: on_message_removed + _removed_messages 重放队列 + cap 1000/TTL 24h + attach 重放（P.3 时序）+ on_part_updated 独立递增（Q）
- `src/oc_slimapi/sse/tokenstream/frames.py`: _message_removed_frame（P.4）
- `docs/`: v1-contract/INTERFACE_MAP/CLIENT_CHANGES/CHANGELOG 加 message.removed 帧 + 重放队列 + per_part_rev 独立递增
- `tests/`: Q + P 测试（R）

## 实施顺序
Q（per_part_rev 独立递增，最小改动）→ P.4（frame）→ P.2（on_message_removed + 重放队列）→ P.1（publish 路由）→ P.3（attach 重放时序）→ R（测试）→ docs → `./scripts/check.sh`
