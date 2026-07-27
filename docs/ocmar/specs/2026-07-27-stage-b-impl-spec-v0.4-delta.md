# Stage B Implementation Spec v0.4 Delta

- **日期**: 2026-07-27
- **基线**: `2026-07-27-stage-b-impl-spec.md`（v1）+ fix-1 实现
- **触发**: rev-ogpt 5.0/10 NEEDS-FIX（2 CRITICAL + MAJOR 5）→ 双方 v0.4 重新冻结
- **双方冻结**: 2026-07-27 v0.4 unanimous（digest messageEventSeq / R2 known+contentRevision / removal / cap 500）

## v0.4 变更（叠加 v1，冲突处以 v0.4 为准）

### A. digest revision 语义改（CRITICAL 2 修复）
- `DigestFields.part_revisions` → **`content_revisions`**（wire 名 `contentRevisions`）
- value 从 `max(per-part revision)` 改为 **`messageEventSeq`**（message-level 单调事件序号）
- messageEventSeq 维护：message 首次 part 事件触及 → 1；每后续事件 → += 1
- 触发事件：`message.part.updated`（含新 part 创建 / text 追加）+ `message.part.removed`
- 无缓存（冷启动 / 未触及）→ 该 message 不在 content_revisions map（或 client 视为 0，不信任 → R1）

### B. _part_state 结构 + messageEventSeq
```python
# GlobalHub._part_state
{sessionID: {messageID: {"parts": {partID: per_part_revision}, "seq": messageEventSeq_int}}}
```
- `per_part_revision`：保留供 token 帧 dedup（新 part=0，该 part 的 message.part.updated +1）
- `messageEventSeq`（`seq`）：message-level 单调序号（digest / R2 / X-Message-Event-Seq 头用）
- **LRU cap 500 message/session**：超限淘汰最旧 message（防无限增长，rev-ogpt MAJOR 5）

### C. R2 known 加 messageEventSeq（CRITICAL 1 修复）
- known = `{maxPartId, partCount, messageEventSeq}`
- `get_part_fingerprint(sid, mid)` 返回 `(maxPartId, partCount, messageEventSeq)` 三元组
- `/full/{mid}?known.maxPartId=&known.partCount=&known.messageEventSeq=` 三者全一致 → 304
- message.part.removed 后 messageEventSeq 前进 → 旧 known 不命中 → 200（正确）

### D. /full 200 加 X-Message-Event-Seq 头（MUST）
- `/full/{mid}` 200 响应加 `X-Message-Event-Seq: <int>` 头
- 值 = 该 message 的 messageEventSeq
- 无缓存（冷启动 / 未触及）→ `X-Message-Event-Seq: 0`（client 不信任 0 → R1，与重启归零一致）
- 注意：304 短路路径不加该头（304 无 body，fingerprint 已对齐）

### E. removal 处理（MAJOR 5）— publish 新增两个事件路由
**opencode v1.18.4 载荷（schema session.ts:604-628）**：
- `message.part.removed`: props = `{sessionID, messageID, partID}`（扁平）
- `message.removed`: props = `{sessionID, messageID}`（扁平）

**sidecar publish 路由**：
- `message.part.removed`：
  - `_part_state[sessionID][messageID].parts.pop(partID, None)`
  - `seq` += 1
  - `entry.content_revisions[messageID] = seq`（digest 通知 client partCount 变）
- `message.removed`：
  - `_part_state[sessionID].pop(messageID, None)`
  - digest 不带该 message contentRevision（已删）

**partID 不复用**：sidecar 不生成 partID（只记录上游 PartID.ascending），自然满足。

### F. MAJOR 3 修复：reconnect 清 pending content_revisions
- `resync_all()` 除了 `self._part_state.clear()`，**还清 `self.pending` 所有 entry 的 `content_revisions`**
- 防 part.updated 进 debounce 后、flush 前 reconnect → client 收 resync 后再收旧 epoch contentRevisions
- 实现建议：遍历 `self.pending.values()`，每个 `entry.content_revisions.clear()`

### G. MAJOR 4 修复：truncated 帧顺序
- `_truncate_part_for_all`：先 `rev = self._part_revisions.get(key)`，再 `drop_part(key)`（后者删 `_part_revisions[key]`），构建 truncated 帧用捕获的 `rev`
- `_emit_snapshot_or_truncated` oversized 路径（`809-813`）同理：先捕获再清

### H. token 帧保留 per_part_revision（dedup，不变）
- token snapshot/delta/truncated 的 `partEventRevision` = `per_part_revision`（part-level）
- 与 messageEventSeq 独立递增（per_part: 新 part=0 / part updated +1；messageEventSeq: message 任意 part 事件 +1）

### I. 文档更新（MAJOR 6）
- `docs/specs/v1-contract.md`: 加 contentRevisions / X-Message-Event-Seq / R2 known 三参数 / removal 语义
- `docs/specs/INTERFACE_MAP.md`: `/full/{mid}` 加 known 参数 + 304 + X-Message-Event-Seq；message.part.updated/removed 不再全丢
- `docs/specs/CLIENT_CHANGES.md`: Stage B wire 变更
- `CHANGELOG.md`: Unreleased 加 Stage B wire 行为变更（contentRevisions / X-Message-Event-Seq / R2 / removal）
- 注意：`scripts/check_routes_doc.py` 校验路由↔文档一致，文档更新必须同步

### J. 反例测试（MINOR 7，rev-ogpt issue 7）
新增测试覆盖：
1. 同 part text 更新（revision+1）但 maxPartId/partCount 不变 → known 不含 messageEventSeq 时 304 错误；含 messageEventSeq 时不命中 → 200（验证 C 修复）
2. 事件缓存不完整（冷启动后首见既有 part）→ 不返回 304（无缓存或 seq 不匹配）
3. 跨 reconnect pending 泄漏：part.updated → reconnect before flush → 后续 digest 不含旧 contentRevisions（验证 F）
4. 真实 truncate 路径 `_truncate_part_for_all` / oversized handshake → truncated 帧含 partEventRevision（验证 G）
5. message.part.removed → messageEventSeq 前进 + partCount 变 + digest 通知（验证 E）
6. message.removed → _part_state 删该 message + 后续 /full known 无缓存 → 200（验证 E）
7. 多 part 下 message watermark 单调前进：新 part 创建 → seq+1；低 revision part 多次更新 → seq 每次前进（验证 A，与 v1 max 反例对比）

## 文件改动（叠加 fix-1 v1 实现）
- `src/oc_slimapi/sse/hub.py`: DigestFields content_revisions；_part_state 结构 + messageEventSeq + LRU cap 500；publish message.part.updated/removed + message.removed 路由；get_part_fingerprint 三元组；resync_all 清 pending content_revisions
- `src/oc_slimapi/sse/tokenstream/hub.py`: truncated 帧顺序修复（G）
- `src/oc_slimapi/routes/messages.py`: /full known.messageEventSeq（C）+ X-Message-Event-Seq 头（D）
- `docs/specs/v1-contract.md` / `INTERFACE_MAP.md` / `CLIENT_CHANGES.md` / `CHANGELOG.md`（I）
- `tests/`: 反例测试（J）

## 实施顺序
1. A+B（数据结构 + messageEventSeq）→ digest 正确性基础
2. E（removal 路由）→ _part_state 完整维护
3. C+D（R2 known seq + /full 头）→ wire 完整
4. F+G（reconnect 清 pending + truncated 顺序）→ MAJOR 修复
5. I（文档）→ 路由↔文档一致
6. J（反例测试）→ 验证
7. `./scripts/check.sh` 通过
