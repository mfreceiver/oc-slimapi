# Stage B Implementation Spec — partEventRevision + /full 304 fingerprint

- **日期**: 2026-07-27
- **状态**: 实施基线（fixer 实施依据 + rev-ogpt 评审对象）
- **P0 冻结引用**: 2026-07-27 双方冻结（R1+R2 / 第2类 replacement / partEventRevision / Success 语义）
- **上游对齐**: opencode v1.18.4（`PartID = Identifier.ascending("part")`，字典序=创建序）

---

## 1. 目标

落地 P0 冻结条款的 sidecar 侧改动：

1. digest 携带 `partEventRevisions`（per-message map），让 client 检测 part 变更触发 R1/R2 恢复。
2. `/full/{mid}?known={maxPartId,partCount}` → 一致 304 Not Modified，不一致正常 full body（R2 单条优化）。
3. token stream 帧（snapshot/delta/truncated）携带 `partEventRevision`，支持 token-only 恢复。
4. reconnect 时清 part 状态 map → 触发 client R1 /full 恢复。
5. X-Since-Complete 维持 `/since` 专属（不改 `/full`）。

---

## 2. P0 冻结条款（实现约束）

| 条款 | 约束 |
|---|---|
| P0-1 R2 | 单条 `/full/{mid}?known={maxPartId,partCount}` → 304（batch 不支持 known） |
| P0-3 partEventRevision | 初值 0（Int?）；part 创建=0，text 追加(message.part.updated)+1；250ms debounce（digest 既定）；reconnect 清 map→R1；token 携带 revision |
| P0-4 X-Since-Complete | `/since` 专属，`/full` 不发 |

**partEventRevision 递增语义（本 spec 明确）**：
- 触发点 = `message.part.updated` 事件（part 状态变更：text-start / text-end / append）
- `message.part.delta`（token 流）**不递增**（client 实时收 delta，无需 revision 检测）
- 新 partID（不在缓存）→ revision = 0（part 创建）
- 已有 partID → revision += 1

---

## 3. 数据结构设计

### 3.1 GlobalHub 新增 per-session part 状态缓存

`src/oc_slimapi/sse/hub.py` `GlobalHub.__init__` 新增：

```python
# Stage B: per-session per-message part 状态缓存。
# 从 message.part.updated 事件维护，支撑 digest partEventRevisions
# + /full/{mid}?known= 304 fingerprint + token frame revision。
# reconnect 时整体清空（触发 client R1 /full 恢复）。
self._part_state: dict[str, dict[str, dict[str, int]]] = {}
# 结构: {sessionID: {messageID: {partID: partEventRevision}}}
```

`MessagePartState` 不单独建类——直接用嵌套 dict（partID → revision int），足够且零 dataclass 开销。

派生量（不存储，按需计算）：
- `partCount = len(part_map)`（某 message 的 part 数）
- `maxPartId = max(part_map.keys())`（字符串字典序；opencode PartID.ascending 字典序=创建序，正确）
- message-level `partEventRevision = max(part_map.values()) if part_map else 0`

### 3.2 DigestFields 扩展

`src/oc_slimapi/sse/hub.py` `DigestFields`（现 line 149-194）新增字段：

```python
@dataclass
class DigestFields:
    # ... 现有字段不变 ...
    directory: str | None = None
    status: str | None = None
    message_id: str | None = None
    updated_at: Any = None
    archived: int | None = None
    children_version: int | None = None
    deleted: bool = False
    last_error: Any = _UNSET
    # Stage B 新增：debounce 窗口内触及的 message → max partEventRevision。
    # 空 dict → to_payload 不输出该字段（向后兼容，无 part 事件的 digest 形态不变）。
    part_revisions: dict[str, int] = field(default_factory=dict)
```

`to_payload` 末尾新增：
```python
if self.part_revisions:
    payload["partEventRevisions"] = dict(self.part_revisions)
return payload
```

---

## 4. 改动点

### 改动 1: publish message.part.updated 路由扩展（hub.py:690-696）

现状（line 690-696）：message.part.updated 只路由到 token hub，return。

改为：**先更新 _part_state + DigestFields.part_revisions，再路由 token hub（传 revision），再 return**。

```python
if event_type in ("message.part.delta", "message.part.updated"):
    if event_type == "message.part.updated":
        # Stage B: 更新 part 状态缓存 + digest part_revisions。
        part = props.get("part")
        if isinstance(part, dict):
            psid = part.get("sessionID")
            pmid = part.get("messageID")
            ppid = part.get("id")
            if isinstance(psid, str) and isinstance(pmid, str) and isinstance(ppid, str) \
               and psid and pmid and ppid:
                session_parts = self._part_state.setdefault(psid, {}).setdefault(pmid, {})
                if ppid in session_parts:
                    session_parts[ppid] += 1  # 已有 part → text 追加 +1
                else:
                    session_parts[ppid] = 0   # 新 part → 创建 = 0
                msg_revision = max(session_parts.values()) if session_parts else 0
                # 写入 debounce 窗口的 digest 累积（per-session entry）。
                # 注意：message.part.updated 的 session 归属用 part.sessionID
                # （与 MESSAGE_EVENTS 的 _extract_session_id 一致语义）。
                entry = self.pending.setdefault(psid, DigestFields())
                entry.part_revisions[pmid] = msg_revision
                # part_revision 传给 token hub（per-part revision，见改动 4）。
                if self._token_hub is not None:
                    self._token_hub.on_part_updated(props, part_revision=session_parts[ppid])
                return
    # message.part.delta 或 message.part.updated 但 part 字段缺失：原 token hub 路由。
    if self._token_hub is not None:
        if event_type == "message.part.delta":
            self._token_hub.on_part_delta(props)
        else:
            self._token_hub.on_part_updated(props)
    return
```

**关键**：
- message.part.updated 的 session/message/part ID 从 `props.part` 提取（与 tokenstream `on_part_updated` line 375-381 一致口径）。
- digest entry 用 `part.sessionID` 作为 pending key（与 MESSAGE_EVENTS 用 `_extract_session_id` 一致）。
- part_revision 传 token hub（改动 4）。

### 改动 2: DigestFields + to_payload（hub.py:149-194）

见 §3.2。`part_revisions` 默认空 dict，无 part 事件的 digest payload 形态完全不变（向后兼容）。

### 改动 3: /full/{mid}?known= 304 短路（messages.py:986+）

`message()` handler（`/full/{mid}`）新增 `known_max_part_id` + `known_part_count` query 参数 + 304 短路。

签名改为：
```python
@router.get("/full/{mid}")
async def message(
    request: Request, sid: str, mid: str,
    mode: Literal["skeleton", "full"] = "full",
    directory: str | None = None,
    known_max_part_id: str | None = Query(None, alias="known.maxPartId"),
    known_part_count: int | None = Query(None, alias="known.partCount", ge=0),
):
```

304 短路（在现有 full/skeleton 逻辑之前，directory 解析之后）：
```python
# Stage B R2: fingerprint 304 短路。仅当 client 提供 known 且 sidecar 缓存命中。
if known_max_part_id is not None and known_part_count is not None:
    hub = getattr(request.app.state, "global_hub", None)
    if hub is not None:
        fp = hub.get_part_fingerprint(sid, mid)  # 见改动 3b
        if fp is not None and fp == (known_max_part_id, known_part_count):
            return Response(
                None, status_code=304,
                headers={"Cache-Control": "no-store"},
            )
        # 不一致或无缓存 → 继续正常 full body 流程
```

**改动 3b: GlobalHub.get_part_fingerprint 公开方法**（hub.py GlobalHub 新增）：
```python
def get_part_fingerprint(self, sid: str, mid: str) -> tuple[str, int] | None:
    """返回 (maxPartId, partCount) 或 None（无缓存）。

    供 /full/{mid}?known= 304 比对。maxPartId = max(partIDs) 字符串字典序
    （opencode PartID.ascending 字典序=创建序）。
    """
    msg_parts = self._part_state.get(sid, {}).get(mid)
    if not msg_parts:
        return None
    return (max(msg_parts.keys()), len(msg_parts))
```

**GlobalHub 访问**：routes 通过 `request.app.state.global_hub`（确认 app.py wiring；若现有 HubRegistry 注册名不同，fixer 对齐现有命名）。

**注意**：known 参数用 `known.maxPartId` / `known.partCount` 点号 alias（query string 形如 `?known.maxPartId=part_xxx&known.partCount=3`）。FastAPI Query alias 支持点号。

### 改动 4: token frames 携带 partEventRevision（frames.py + tokenstream/hub.py）

#### 4a. tokenstream/hub.py on_part_updated 签名 + revision 记录

`on_part_updated`（line 360）签名改为 `(self, props, part_revision: int | None = None)`。

新增 per-part revision 记录：
```python
# TokenStreamHub.__init__ 新增
self._part_revisions: dict[PartKey, int] = {}
```

`on_part_updated` 内：解析 key 后，若 `part_revision is not None` → `self._part_revisions[key] = part_revision`。

#### 4b. frames.py 帧构建加 partEventRevision

`_snapshot_frame` / `_delta_frame` / `_truncated_frame` 新增 `part_revision: int | None = None` 参数：
```python
def _snapshot_frame(key, text, done, part_revision=None):
    payload = {"sessionID": key[0], "messageID": key[1], "partID": key[2], "done": done}
    if text is not None:
        payload["text"] = text
    if part_revision is not None:
        payload["partEventRevision"] = part_revision
    return sse_frame(payload, event="message.part.snapshot")
```
`_delta_frame` / `_truncated_frame` 同理加 `part_revision` 参数 + 可选 `partEventRevision` 字段。

#### 4c. tokenstream/hub.py 调用 frames 时传 revision

所有调用 `_snapshot_frame` / `_delta_frame` / `_truncated_frame` 的点，传 `part_revision=self._part_revisions.get(key)`：
- `_delta_frame`（flush / flush_sid / on_part_delta early-flush / finish_part residual drain）
- `_snapshot_frame`（finish_part terminal marker、attach_subscriber handshake、_evict_part_for_memory re-snapshot）
- `_truncated_frame`（_emit_snapshot_or_truncated / _nodrop / _truncate_part_for_all）

**注意**：`_part_revisions` 在 part 退役（drop_part / _retire_session）时一并清理（见改动 5）。

### 改动 5: 生命周期清理（reconnect / session.deleted / part 退役）

#### 5a. GlobalHub reconnect 清 map（触发 client R1）
`on_upstream_reconnect`（或等价 reconnect 钩子，定位现有 `_notify_upstream_loss` / `resync_all` 附近的 reconnect 路径）：
```python
self._part_state.clear()
```
与现有 `resync_all` 的 tombstone 清理同位置/时机。

#### 5b. GlobalHub session.deleted 清理
publish `session.deleted` 分支（line 553-560）末尾加：
```python
self._part_state.pop(session_id, None)
```

#### 5c. TokenStreamHub part 退役清理 _part_revisions
- `drop_part` / `_retire_session` / `ttl_sweep` 退役 part 时，`self._part_revisions.pop(key, None)`。
- `on_session_deleted`（token hub）清该 sid 全部 `_part_revisions`。

---

## 5. Wire 形态（告知 ocdroid）

### 5.1 digest（session.digest 事件）
```json
{
  "sessionID": "ses_xxx",
  "updatedAt": 1754000000000,
  "partEventRevisions": {"msg_aaa": 2, "msg_bbb": 1}
}
```
- `partEventRevisions`: optional。无 part 事件的 digest 不带该字段（向后兼容）。
- value = 该 message 的 max part revision（message-level watermark）。

### 5.2 token snapshot/delta/truncated 帧
```json
{"sessionID":"...", "messageID":"...", "partID":"...", "done":false, "text":"...", "partEventRevision":0}
```
- `partEventRevision`: optional（缓存命中时带，per-part revision）。

### 5.3 /full/{mid}?known= 304
- Request: `GET /slimapi/messages/{sid}/full/{mid}?known.maxPartId=part_zzz&known.partCount=3`
- 一致: `304 Not Modified`（空 body，`Cache-Control: no-store`）
- 不一致/无缓存: 正常 full body（200）

---

## 6. 边界条件

| 场景 | 行为 |
|---|---|
| sidecar 冷启动（_part_state 空） | digest 无 partEventRevisions；/full?known= 无缓存→正常 200 full body；token 帧无 partEventRevision（part_revision=None） |
| reconnect | _part_state.clear()；digest 无 partEventRevisions；client 触发 R1 /full 重建 |
| session.deleted | _part_state.pop(sid)；token hub _part_revisions 清该 sid |
| part 退役（truncated/evict/TTL） | token hub _part_revisions.pop(key) |
| known 参数部分提供（只 maxPartId 或只 partCount） | 不触发 304（要求两者齐全） |
| known 参数 + mode=skeleton | 仍走 304 短路（fingerprint 与 mode 正交） |
| schema_degraded（mode 强制 full） | 304 短路在 mode 逻辑之前，正常生效 |

---

## 7. 测试要求（check.sh = pytest tests/）

### 7.1 新增测试
- `part_state` 维护：message.part.updated text-start→revision 0，text-end→+1，append 新 part→0。
- digest partEventRevisions：debounce 窗口累积多 message，flush payload 含 map。
- /full/{mid}?known= 304：一致→304，不一致→200，无缓存→200，部分参数→200。
- token 帧 partEventRevision：snapshot/delta/truncated 携带 revision。
- reconnect 清 _part_state。
- session.deleted 清 _part_state。
- 向后兼容：无 part 事件的 digest payload 不含 partEventRevisions（快照测试）。

### 7.2 不破坏现有测试
- digest 现有字段/形态不变（partEventRevisions optional）。
- token 帧现有字段不变（partEventRevision optional）。
- /full/{mid} 无 known 参数时行为完全不变。

---

## 8. 文件清单

| 文件 | 改动 |
|---|---|
| `src/oc_slimapi/sse/hub.py` | DigestFields.part_revisions + to_payload；GlobalHub._part_state + get_part_fingerprint；publish message.part.updated 路由；reconnect/session.deleted 清理 |
| `src/oc_slimapi/sse/tokenstream/hub.py` | on_part_updated 签名 + _part_revisions；调用 frames 传 revision；退役清理 |
| `src/oc_slimapi/sse/tokenstream/frames.py` | _snapshot_frame/_delta_frame/_truncated_frame 加 part_revision 参数 |
| `src/oc_slimapi/routes/messages.py` | /full/{mid} 加 known 参数 + 304 短路 |
| `tests/` | 新增 partEventRevision + 304 测试 |

---

## 9. 实施顺序（建议）

1. 改动 2（DigestFields 字段）+ 改动 1（publish 路由 + _part_state）—— 核心数据流
2. 改动 3（/full known 304 + get_part_fingerprint）
3. 改动 4（token frames revision）
4. 改动 5（生命周期清理）
5. 测试（§7）
6. `./scripts/check.sh` 通过
