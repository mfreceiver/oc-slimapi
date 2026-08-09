# chat-toolcard oc-slimapi 侧终审#4 评审 + 实施方案

> **bundle**: `bundle-chat-toolcard-A`（阶段 A：方案完善，不开工实现）
> **范围**: 只读评审 + 方案文档产出。**未触碰 `src/`、`tests/` 任何代码**（详见 §6 自查声明）。
> **协同**: ocdroid Android 客户端 × oc-slimapi slim 后端，协同点 = `diffStats` wire 契约。
> **SSOT 基线**: `chat-toolcard-investigation.md`（ocdroid，已 rev-ogpt 评审）+ 本文。
> **日期**: 2026-08-09

---

## 1. 评审发现（Review Findings）

### F1【BUG · 阻塞客户端】`_patch()` 把 diffStats 写到了客户端不读的顶层位置

**位置**: `src/oc_slimapi/skeleton.py:256-260`，函数 `_patch()`。

```python
# 当前（错误）
if isinstance(files, list):
    diffStats = _compute_diffstats_from_files(files)
    if diffStats is not None:
        result["diffStats"] = diffStats          # ← 顶层 result["diffStats"]
```

**对照 `_tool()`（正确）**: `src/oc_slimapi/skeleton.py:224-232` 写到 `thin_state["metadata"]["diffStats"]`，即 wire 路径 `state.metadata.diffStats`。

**为什么是 bug**: wire 契约（handoff 终审#4 + ocdroid `PartDisplayExtensions.displayDiffStats`）规定 diffStats 只有一个消费入口——

```
state.metadata?.get("diffStats")   // ocdroid 读法（JsonObject → additions/deletions）
```

`_patch()` 写到 part 顶层 `result["diffStats"]`，Part 序列化器（`PartStateSerializer`）只删 `diagnostics`、保留 `state.metadata.diffStats`，**不会**把顶层 `diffStats` 搬进 `state.metadata`。结果：**patch 类工具卡（apply_patch / 多文件 write）永远显示不出 diffStats 徽章**；edit 类（走 `_tool()`）正常。

**既有测试固化了错误行为**: `tests/test_skeleton.py:563 / 580 / 627` 三处断言 `result["diffStats"]`（顶层），改实现必须同步改这三处断言到 `result["state"]["metadata"]["diffStats"]`。

**严重度**: 高。这是 wire 契约的事实偏离——`_tool()` 与 `_patch()` 对同一字段给了两个不同 wire 位置，客户端只能读其中一个。

### F2【契约文档缺口】diffStats 未在 `v2-contract.md` 明文记录

`docs/specs/v2-contract.md` grep `diffStats` = 0 命中。批次4（commit d57f4e4）实现了 diffStats 投影但未写进契约文档。`docs/specs/design-v2.md` / `INTERFACE_MAP.md` 同样无记录。

**影响**: 后续 agent / 运维无法从契约文档得知 diffStats 的 wire 位置、字段集、来源（filediff vs files[]）。契约权威（`v2-contract.md`）与实现脱节，违反 AGENTS.md「契约权威」硬规则。

### F3【设计确认 · 非 bug】digest 帧不携带 part state / diffStats

**确认依据**: `v2-contract.md:247-250` —— digest 字段集 = `{sessionID, directory, status?, messageID?, updatedAt?, archived?, deleted?, lastError?, turnIncarnation?, turn?}`，**不含** part state、不含 diffStats。

这是 v2 的有意设计（lite-v2 删除了 Stage B part-tracking，见 `v2-contract.md:268`）。diffStats 只能通过骨架投影（`/slimapi/messages/{sid}` 或 `/slimapi/messages/{sid}/full/{mid}`）获得。

### F4【可见性路径确认】客户端看到 running→completed + diffStats 的可靠路径

**契约依据**: `v2-contract.md:330`——

> part/message 完成仍走既有路径：`message.updated`(step-finish) → digest → 客户端 `/messages/{sid}` 或 `/full/{mid}` 拉权威全文。

**完整链路**（slimapi 侧可保证部分 + 上游依赖部分）:

| 阶段 | 上游事件 | slim 处理 | slim 输出帧 | slimapi 可保证? |
|---|---|---|---|---|
| 工具开始 | `session.status(busy)` | 进 digest `status` | `session.digest{status:busy}` | ✅ `global_hub.py:545-548` |
| 工具运行中 | `tool.*`（tool.call/tool.input） | **丢弃**（catch-all, `global_hub.py:813`） | 无 | ✅ |
| part 边界 | `message.part.updated` | 路由 token hub，**不进 digest**（`global_hub.py:720-750`） | 无 digest 帧 | ✅ |
| 工具完成 | `message.updated`(step-finish) | 进 digest `messageID`+`updatedAt` | `session.digest{messageID, updatedAt}` | ✅ `global_hub.py:619-635` ** iff 上游发 `message.updated`** |
| 客户端拉骨架 | (客户端主动) | skeleton 投影 | `/messages/{sid}` 200，part `state.status=completed` + `state.metadata.diffStats` | ✅（修 F1 后） |
| 会话空闲 | `session.status(idle)` | 进 digest `status` | `session.digest{status:idle}` | ✅ |

**唯一上游依赖**: opencode 在工具 part 完成时**是否**发 `message.updated`。契约 line 330 断言「step-finish → message.updated」，但这正是 skeleton.py:98-101 / 217-219 标注的「digest 对账待实测」项。**这是 §4 实测方案的核心验证目标**。

---

## 2. 逐项实施方案

### 项 1【核心】`_patch()` diffStats 契约统一

**目标**: 把 diffStats 从顶层 `result["diffStats"]` 改写到 `result["state"]["metadata"]["diffStats"]`，与 `_tool()` 和 wire 契约统一。`files[]` 兜底保留。

**改动点**: `src/oc_slimapi/skeleton.py`，函数 `_patch()`（行 239-281）。

#### 改动前（行 251-260 + 行 260-281）

```python
    # Inject compact diffStats from files[] (computed, injected AFTER
    # thresholding — no thresholding is applied to diffStats). Patch parts
    # project files[] with additions/deletions; diffStats is a compact
    # aggregate consistent with the per-file data. digest 对账为后续 SSE
    # 实测验证项，本轮不实现。
    if isinstance(files, list):
        diffStats = _compute_diffstats_from_files(files)
        if diffStats is not None:
            result["diffStats"] = diffStats                  # ← BUG: 顶层
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        source_input = state.get("input")
        if isinstance(source_input, dict):
            path_input = _pick(source_input, {"path", "filePath", "file_path"})
            if path_input:
                thin_state["input"] = path_input
            omitted.extend(
                f"state.input.{key}"
                for key in source_input if key not in {"path", "filePath", "file_path"}
            )
        for key in SKELETON_INLINE_FIELDS:
            _maybe_inline_state_field(thin_state, state, key, omitted, budget)
        result["state"] = thin_state
    for key in part:
        if key not in PART_IDS | {"files", "metadata", "state"}:
            omitted.append(key)
    return _mark(result, omitted)
```

#### 改动后

**删除** 行 251-260 的旧 diffStats 块；**在 `result["state"] = thin_state` 之后**（原行 277 后）插入新的统一注入块。理由：diffStats 目标位置是 `result["state"]["metadata"]`，必须在 `result["state"]` 赋值之后操作；并处理 patch 有 `files[]` 但无 `state` 的边界（此时需创建最小 `state.metadata` 容器，否则客户端 `state.metadata?.get(...)` 链断）。

```python
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        source_input = state.get("input")
        if isinstance(source_input, dict):
            path_input = _pick(source_input, {"path", "filePath", "file_path"})
            if path_input:
                thin_state["input"] = path_input
            omitted.extend(
                f"state.input.{key}"
                for key in source_input if key not in {"path", "filePath", "file_path"}
            )
        # Thresholded like _tool: inline small output/error, omit large or
        # budget-spent. Patch parts share the per-message budget with tool
        # parts (part order) so neither can starve the other.
        for key in SKELETON_INLINE_FIELDS:
            _maybe_inline_state_field(thin_state, state, key, omitted, budget)
        result["state"] = thin_state
    # Inject compact diffStats from files[] into state.metadata.diffStats,
    # mirroring _tool() (skeleton.py:224-232). ocdroid reads
    # state.metadata?.get("diffStats") — a top-level result["diffStats"] is
    # NEVER read (PartStateSerializer only drops diagnostics, does not
    # relocate). Injected AFTER thresholding; the ~50 B object is never
    # omit-eligible. A patch part may carry files[] WITHOUT an upstream
    # state object — create the minimal state.metadata container so the
    # client read path (state.metadata?.get) does not chain-break.
    if isinstance(files, list):
        diffStats = _compute_diffstats_from_files(files)
        if diffStats is not None:
            if "state" not in result:
                result["state"] = {}
            thin_state = result["state"]
            if "metadata" not in thin_state:
                thin_state["metadata"] = {}
            thin_state["metadata"]["diffStats"] = diffStats
    for key in part:
        if key not in PART_IDS | {"files", "metadata", "state"}:
            omitted.append(key)
    return _mark(result, omitted)
```

**关键不变量**:
- `result["files"]`（行 242-247）**保留不动**——files[] 是 per-file 明细兜底，diffStats 是聚合，两者共存。
- `_compute_diffstats_from_files`（行 127-146）**不改**——纯函数，已正确。
- `_is_renderable`（行 425-426）patch 分支 `bool(part.get("files") or part.get("metadata") or part.get("state"))`：修复后「有 files[] 无 state」的 patch 现在会创建 `result["state"]={"metadata":{"diffStats":...}}`，`_is_renderable` 返回 True（本就应 True，因为 files[] 存在）——**无回归**。

#### 配套测试改动（`tests/test_skeleton.py`，由代码修改 agent 同步）

| 行号 | 当前断言（错误） | 改后断言 |
|---|---|---|
| 563 | `assert result["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}` | `assert result["state"]["metadata"]["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}` |
| 580 | `assert "diffStats" not in result` | `assert "diffStats" not in result.get("state", {}).get("metadata", {})` |
| 627 | `assert result["diffStats"] == {"additions": 18, "deletions": 10, "files": 3}` | `assert result["state"]["metadata"]["diffStats"] == {"additions": 18, "deletions": 10, "files": 3}` |

**新增测试用例**（建议补，验证边界）:

```python
def test_patch_with_files_but_no_state_creates_state_metadata():
    """Patch 有 files[] 但无 upstream state → 仍创建 state.metadata.diffStats,
    客户端读链 state.metadata?.get("diffStats") 不断。"""
    source = [{
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "metadata": {"path": "src/foo.ts"},
            "files": [{"path": "src/foo.ts", "additions": 7, "deletions": 2}],
            # 注意：无 "state" 键
        }],
    }]
    result = skeleton_messages(source)[0]["parts"][0]
    assert result["state"]["metadata"]["diffStats"] == {"additions": 7, "deletions": 2, "files": 1}
    assert "diffStats" not in result  # 顶层不再有 diffStats


def test_patch_and_tool_diffstats_same_wire_location():
    """同一消息内 tool part 与 patch part 的 diffStats 都在 state.metadata.diffStats,
    客户端用同一读法消费。"""
    source = [{
        "info": {"id": "m1"},
        "parts": [
            {"id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
             "state": {"status": "completed", "metadata": {
                 "filediff": {"file": "a.ts", "additions": 3, "deletions": 1}}},
             },
            {"id": "p2", "type": "patch", "messageID": "m1",
             "files": [{"path": "b.ts", "additions": 5, "deletions": 2}],
             "state": {"status": "completed"},
             },
        ],
    }]
    parts = skeleton_messages(source)[0]["parts"]
    assert parts[0]["state"]["metadata"]["diffStats"] == {"additions": 3, "deletions": 1, "files": 1}
    assert parts[1]["state"]["metadata"]["diffStats"] == {"additions": 5, "deletions": 2, "files": 1}
```

---

### 项 2【契约对齐】diffStats 字段集确认 + 文档补录

**字段契约**（与 ocdroid `toIntOrNull()` 容错对齐）:

```json
state.metadata.diffStats = {
  "additions": <int ≥0>,   // ocdroid: toIntOrNull() → null 容错
  "deletions": <int ≥0>,   // ocdroid: toIntOrNull() → null 容错
  "files":     <int ≥1>    // 文件数（list len / 单文件=1）
}
```

**slimapi 侧保证**（`_compute_diffstats` 行 78-124 + `_compute_diffstats_from_files` 行 127-146）:
- 三个字段恒为 Python `int`（`int(val) or 0` 兜底，非 finite 防御性归零）。
- `additions` / `deletions` 缺失 → 0；`files` = list 长度（≥1，因空 list 返回 None 不注入）。
- ocdroid 侧 `toIntOrNull()` 对 null / 非数字字符串返回 null，slimapi 永不发 null（恒 int）——**容错对齐，slimapi 侧更严格**。

**文档补录动作**（建议，**不**在本任务执行——属契约文档变更，需 omni 批准 + 走 §2.1 流程）:

在 `docs/specs/v2-contract.md` §2（HTTP 契约）骨架投影小节增补一段：

> **diffStats（v2+additive，未 bump）**: tool / patch part 的 `state.metadata` 注入紧凑聚合 `diffStats = {additions:int≥0, deletions:int≥0, files:int≥1}`。来源：tool part ← `state.metadata.filediff`（单 dict 或 list）；patch part ← `files[]`。注入位置统一为 `state.metadata.diffStats`（ocdroid `PartDisplayExtensions.displayDiffStats` 唯一读入口）。注入在 thresholding 之后，~50 B 永不进 omit 集合。无 filediff / 无 files[] → 不注入（键缺省）。

**同步**: `docs/specs/INTERFACE_MAP.md` 的 `/slimapi/messages` 条目增补 diffStats 投影说明。

---

## 3. digest 实时对账实测方案

### 3.1 验证目标

确认 slim SSE 丢弃所有 `tool.*` / `message.part.*` 事件后，客户端能否**可靠**看到 running→completed + diffStats。分解为两个子问题:

- **Q1（slimapi 可保证）**: 上游发 `message.updated` 时，slim 是否必然吐 `session.digest{messageID, updatedAt}` nudge？
- **Q2（上游依赖，需 live 实测）**: opencode 在工具 part 完成（running→completed + filediff 落库）时，**是否**发 `message.updated`？发的时机是否在 filediff 落库之后（否则客户端拉骨架会拿到无 diffStats 的中间态）？

### 3.2 测试架构

复用既有 SSE hub 单测模式（直接调 `GlobalHub.publish()` 注入事件，断言 subscriber queue 帧序列）。**不打真实 httpx 上游**——既有 `tests/test_traffic_sse.py` 注释已说明 mock 上游 busy-loop 难写。

```python
# 测试夹具骨架（tests/test_digest_toolcard_reconcile.py，新增文件）
import asyncio
import orjson
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import Subscriber, sse_frame

async def _drain(subscriber, timeout=0.5):
    """抽出 subscriber queue 当前已入队的所有帧（不阻塞）。"""
    frames = []
    try:
        for _ in range(subscriber.queue.qsize()):
            item = subscriber.queue.get_nowait()
            if item is not subscriber.__class__.__mro__[0]:  # skip sentinel
                frames.append(item)
            subscriber.ack(item)
    except asyncio.QueueEmpty:
        pass
    return frames

def _make_global_event(directory, event_type, properties):
    """构造上游 /global/event 单帧 JSON（global_hub.publish 入参形状）。"""
    return {
        "directory": directory,
        "payload": {"type": event_type, "properties": properties},
    }
```

### 3.3 用例 A：tool.* 与 message.part.* 不进 digest（slimapi 保证）

**上游注入序列**（模拟一次 edit 工具调用全过程）:

```python
def test_tool_events_dropped_part_events_no_digest_bump():
    hub = GlobalHub(client=None)
    sub = Subscriber()
    hub.subscribers.add(sub)

    # T0: session busy
    hub.publish(_make_global_event("/d", "session.status",
                {"sessionID": "s1", "status": "busy"}))
    # T1: message.updated（工具开始，messageID 出现）
    hub.publish(_make_global_event("/d", "message.updated",
                {"sessionID": "s1", "info": {"id": "m1"}}))
    # T2: tool.* — 应丢弃
    hub.publish(_make_global_event("/d", "tool.call",
                {"sessionID": "s1", "messageID": "m1", "tool": "edit"}))
    hub.publish(_make_global_event("/d", "tool.input",
                {"sessionID": "s1", "tool": "edit", "input": {"filePath": "a.ts"}}))
    # T3: message.part.updated（part state running）— 不进 digest
    hub.publish(_make_global_event("/d", "message.part.updated",
                {"sessionID": "s1", "part": {
                    "id": "p1", "messageID": "m1", "sessionID": "s1",
                    "type": "tool", "state": {"status": "running"}}}))
    # T4: message.updated（step-finish，工具完成）— 应进 digest
    hub.publish(_make_global_event("/d", "message.updated",
                {"sessionID": "s1", "info": {"id": "m1"}}))
    # T5: session idle
    hub.publish(_make_global_event("/d", "session.status",
                {"sessionID": "s1", "status": "idle"}))

    hub.flush()  # 强制吐 debounce 窗口内所有 pending
    frames = _drain(sub)

    # 断言：只有 session.digest 帧，无 tool.* / message.part.* 帧透传
    digest_payloads = [
        orjson.loads(f.split(b"data: ", 1)[1]) for f in frames
        if f.startswith(b"event: session.digest") or b"sessionID" in f
    ]
    # 过滤掉 server.connected 首帧（subscribe 时入队，本测试用 hub.subscribers.add 绕过）
    assert all(b"tool.call" not in f and b"tool.input" not in f for f in frames)
    assert all(b"message.part.updated" not in f for f in frames)
    # digest 序列：busy → (messageID m1 + updatedAt) → idle
    assert any(p.get("status") == "busy" for p in digest_payloads)
    assert any(p.get("messageID") == "m1" for p in digest_payloads)
    assert any(p.get("status") == "idle" for p in digest_payloads)
```

**断言要点**:
1. `tool.call` / `tool.input` 帧**绝不**出现在 subscriber queue（catch-all 丢弃，`global_hub.py:813`）。
2. `message.part.updated` **不**触发 digest `updatedAt` bump（`global_hub.py:720-750` return 不碰 pending）。
3. `message.updated` **必然**产出带 `messageID` 的 digest 帧（`global_hub.py:619-635`）。

### 3.4 用例 B：骨架投影在工具完成后正确含 diffStats（slimapi 保证，依赖项 1 修复后）

```python
def test_skeleton_after_tool_complete_has_diffstats_in_state_metadata():
    """模拟 message.updated 后客户端拉 /messages/{sid}，骨架含
    state.status=completed + state.metadata.diffStats。"""
    from oc_slimapi.skeleton import skeleton_messages
    upstream_message = [{
        "info": {"id": "m1", "time": {"created": 1}},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1", "tool": "edit",
            "state": {
                "status": "completed",
                "title": "src/foo.ts",
                "metadata": {
                    "filediff": {"file": "src/foo.ts", "additions": 12, "deletions": 4},
                },
            },
        }],
    }]
    part = skeleton_messages(upstream_message)[0]["parts"][0]
    assert part["state"]["status"] == "completed"
    assert part["state"]["metadata"]["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}


def test_skeleton_after_patch_complete_has_diffstats_in_state_metadata():
    """patch part 同样在 state.metadata.diffStats（项1 修复后）。"""
    from oc_slimapi.skeleton import skeleton_messages
    upstream_message = [{
        "info": {"id": "m1", "time": {"created": 1}},
        "parts": [{
            "id": "p1", "type": "patch", "messageID": "m1",
            "files": [{"path": "src/foo.ts", "additions": 12, "deletions": 4}],
            "state": {"status": "completed", "title": "Patch"},
        }],
    }]
    part = skeleton_messages(upstream_message)[0]["parts"][0]
    assert part["state"]["status"] == "completed"
    assert part["state"]["metadata"]["diffStats"] == {"additions": 12, "deletions": 4, "files": 1}
```

### 3.5 用例 C：live 实测 opencode 是否在工具完成时发 message.updated（Q2，上游依赖）

**此项不是单测**——是手动 / 集成实测步骤，回答 Q2:

1. **环境**: 启 oc-slimapi（`.venv/bin/python -m oc_slimapi.app`）+ opencode（`:4096`）。
2. **抓上游 SSE**: 在 slimapi 与 opencode 之间插观察点——临时改 `global_hub.py:run()` 在 `self.publish(orjson.loads(...))` 前打印 `event_type`，或用 `mitmproxy` / `socat` 旁路抓包。
3. **触发**: 用 ocdroid 或 curl 对 opencode 发一个 edit 工具调用（`POST /session/{sid}/prompt`，prompt 让模型改一个文件）。
4. **记录上游事件序列**，预期看到（按时间）:
   - `session.status(busy)`
   - `message.updated` 或 `message.appended`（消息创建）
   - `tool.call` / `tool.input`（可能）
   - `message.part.updated`（part state running → completed）
   - **`message.updated`（关键：工具完成 nudge）← 必须确认存在 + 时机**
   - `session.status(idle)`
5. **判定标准**:
   - ✅ 工具完成时上游发 `message.updated`，且 filediff 已落库（抓 `/session/{sid}/message/{mid}` 看是否有 `state.metadata.filediff`）→ 客户端链路可靠。
   - ⚠️ 上游发 `message.updated` 但 filediff 尚未落库（竞态）→ 客户端拉骨架会拿到无 diffStats 的中间态，需客户端在下次 digest 再拉一次（幂等 reconcile）。
   - ❌ 上游**不**发 `message.updated`（只发 `message.part.updated`）→ slim 不 nudge，客户端看不到完成——**需 omni 决策**：要么推 opencode 上游改，要么 slimapi 在 `message.part.updated` 检测到 `state.status` running→completed 跃迁时主动 bump digest（违背 v2 「part 事件不进 digest」设计，需契约 bump）。

**实测产出**: 一份事件序列 trace（类似 `design-token-stream.md` §11 的实测表），归档进 `docs/specs/chat-toolcard-investigation.md` §B.8。

---

## 4. 风险与依赖

| # | 风险 / 依赖 | 影响 | 缓解 |
|---|---|---|---|
| R1 | **上游依赖（Q2 未实测）**: opencode 工具完成时是否发 `message.updated` + filediff 落库时机 | 若不发或竞态，客户端看不到完成/diffStats | §3.5 live 实测；若失败需 omni 决策（上游改 vs slimapi 主动 bump） |
| R2 | **既有测试固化错误行为**（test_skeleton.py:563/580/627） | 改实现后测试红 | 代码修改 agent 必须同步改这 3 处断言 + 补 §2 新增用例 |
| R3 | **另一个 agent 正在改 src/**（本任务约束） | 写域冲突 | 本任务**不动 src/tests**；实施 agent 拿本方案后在其分支落地，避免与在途改动冲突 |
| R4 | **patch 有 files[] 无 state 的边界** | 修复后创建空 state 容器，`_is_renderable` 行为微变（False→True） | §2 已论证无回归（files[] 存在本就应 renderable）；补 `test_patch_with_files_but_no_state_creates_state_metadata` |
| R5 | **契约文档未记录 diffStats**（F2） | 后续 agent 不知 wire 位置 | 项2 建议补录 v2-contract.md + INTERFACE_MAP.md（需 omni 批准，本任务不做） |
| R6 | **diffStats 注入位置改到 state.metadata，与 `/full/{mid}` 的 diagnostics strip 共存** | strip_diagnostics_message 只 pop diagnostics，不动 diffStats → 无冲突 | 已核 `skeleton.py:382-415`，strip 只动 `metadata.diagnostics`，diffStats 保留。无风险 |

---

## 5. 改完后验证命令

实施 agent 落地项1 后执行（AGENTS.md 硬规则：改动校验必做）:

```bash
# 1. 全量校验（pytest + 路由↔文档一致性）
./scripts/check.sh

# 2. 针对性跑 skeleton + digest 单测（快速反馈）
.venv/bin/pytest tests/test_skeleton.py -v -k "diffstats or patch"
.venv/bin/pytest tests/test_skeleton.py -v -k "tool_with_filediff or tool_without_filediff"

# 3. 若新增 test_digest_toolcard_reconcile.py：
.venv/bin/pytest tests/test_digest_toolcard_reconcile.py -v

# 4. grep 确认无残留顶层 result["diffStats"]（应 0 命中 src）
rg 'result\["diffStats"\]' src/   # 期望无输出
```

**预期**:
- 项1 落地后 `./scripts/check.sh` 全绿（含改后的 3 处测试断言 + 新增用例）。
- `rg 'result\["diffStats"\]' src/` 无输出（顶层写法已全消除）。

---

## 6. 自查声明

- **改动点定位**: 每项改动点均对应到具体 `文件:行 / 函数`:
  - 项1: `src/oc_slimapi/skeleton.py:239-281`（`_patch()`），配 `tests/test_skeleton.py:563/580/627`。
  - 项2: 字段契约核 `skeleton.py:78-146`（`_compute_diffstats` / `_compute_diffstats_from_files`）。
  - §3 实测: 事件路由核 `sse/global_hub.py:513-814`（`publish()`）+ 契约 `v2-contract.md:247-269, 330`。
- **测试设计可执行**: §3.2-3.4 给出 pytest 用例骨架（可直接 paste 进 `tests/`）；§3.5 给出 live 实测步骤（含判定标准）。
- **未触碰 src/tests**: 本任务**仅写本文档**（`docs/specs/chat-toolcard-slimapi-plan.md`）。所有 `src/` / `tests/` 引用均为只读核对，未做任何 edit/write。PRE_AUTH_SCOPE 严守 `docs/specs/` 单文件。
