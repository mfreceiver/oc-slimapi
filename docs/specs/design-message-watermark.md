# 设计稿：消息内容指纹（contentFingerprint）— traffic Batch 4 / B3

> 状态：**已冻结**（计划 `docs/ocmar/plans/2026-08-16-traffic-optimization-plan.md` §6 v1.3 定稿；本稿为字段级 spec 权威，v2-contract §消息列表加性节为 wire 摘要）。
> 定位：单边可冻结子集——指纹的**生成语义**由本仓单方冻结；客户端**消费行为语义**（token idle/resync、reconcile 三分法、watermark 推进规则）不在本仓冻结范围，走 ocdroid 联合计划联调（外部 lane 项 4）。

---

## 1. 上游源码勘察锚点（opencode v1.18.16，`opencode-src/current/`）

| 事实 | 锚点 |
|---|---|
| `Message.Info = User \| Assistant`（判别 `role`） | `packages/schema/src/v1/session.ts:490-491` |
| `User.time = { created: Timestamp }` | `packages/schema/src/v1/session.ts:335-337` |
| `Assistant.time = { created, completed? }` | `packages/schema/src/v1/session.ts:456-459` |
| `Timestamp = Schema.Finite ≥ 0`（schema 仅约束 finite/非负，**无粒度约束**；实际消息创建用 `Date.now()` —— 毫秒级，见 `prompt.ts:281,436`、`compaction.ts:468`） | `packages/schema/src/v1/session.ts:15` |
| 消息载体 `WithParts = { info, parts }`；`Part` 12 类 union | `packages/schema/src/v1/session.ts:493-499, 357-372` |
| part 级 time（如 `TextPart.time? = { created, completed? }`、`ToolState.time = { start, end }`） | `packages/schema/src/v1/session.ts:108-115, 283-297` |
| 上游消息排序 = `info.time.created` **+ `info.id` tie-break**；DB 分页 `orderBy(desc(time_created), desc(id))` | `packages/opencode/src/session/message-v2.ts:439`（DB orderBy）；`:600-604`（`isAfter`：created 并列时按 `info.id` 字典序） |
| **上游 `Message.Info` 无 `updatedAt` 字段**（全 schema 无此名） | `packages/schema/src/v1/session.ts`（全文 grep 无命中） |

**同毫秒追加可达性**：`time` 实际为 `Date.now()` 毫秒级时间戳（schema 本身无粒度约束）。同一毫秒内多条消息落地（如批量/自动重试场景，`v2-contract.md:434` 已记录上游同毫秒批量 = 同时间戳 + `id` 次键 tie-break）、同一 assistant 消息同毫秒内多次 part 追加/修订，在上游时间戳上**不可区分**（排序键 `time.created` 并列）。这是既有 `(updatedAt, messageId)` 双水印在毫秒粒度上存在并列盲区的根源——内容指纹的动机。

**排序 tie-break 影响评估**：上游排序在 `time.created` 并列时按 `info.id` 字典序决出（`isAfter`），DB 分页同为 `(time_created, id)` 复合序。该 tie-break **不影响指纹语义**——指纹是**单消息内容**的函数，与跨消息排序无关；parts 序按上游序保持（消息内序不受列表排序影响）。tie-break 只影响列表页内消息的呈现顺序，`X-Next-Cursor` 分页一致性由上游保证，与指纹生成无交集。

**updatedAt 语义澄清**：sidecar digest 帧的 `updatedAt` 是 **sidecar wall-clock 观察时间**（`src/oc_slimapi/sse/global_hub.py:142-145`），不是上游内容版本——联合计划 §4.7（:225-233）已明确"服务端时间回拨、非时间序列 messageId、**同 tuple 内容变化**或旧 message 修订时，不得仅凭 tuple 最大值清 dirty"。

## 2. 与既有双水印 / digest 框架的关系（联合计划锚点）

联合计划：`ocdroid 仓 docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`。

| 锚点 | 内容 | 与指纹的关系 |
|---|---|---|
| §4.1（:123-131） | `(updatedAt, messageId)` 双水印 = **位置水印，不是完整性证明** | tuple 仍是基线与推进主序；指纹不参与 watermark 推进规则 |
| §3:4（:109） | "message-level revision：同一 assistant 消息多次追加的稳定 identity/revision/排序/幂等"——客户端必须等待 sidecar 权威结论 | 指纹是本仓对该问题的**单边可冻结答案**（内容证据而非 revision 序） |
| §4.3（:173-190） | 推荐 messageRevision + partsRevision/count | 见 §5 方案乙否决——有状态 revision 漏事件即假阴性 |
| §4.4（:190-198） | token idle/resync 语义 | 消费侧联调项，本仓不冻结 |
| §4.5（:199-208） | 旧 sidecar 降级边界三层发现机制 | 指纹落地后第 2 层 bounded probe 的判变证据从"无法证明"升级为"字符串不等即变" |
| §4.7（:225-233） | "同 tuple 内容变化不得仅凭 tuple 清 dirty" | 指纹 = 该场景的**补充证据**：tuple 不变 + 指纹变 ⟹ 内容变 ⟹ 保留 dirty 重拉 |

**关系声明（冻结）**：指纹是 `(updatedAt, messageId)` 双水印的**补充证据**，用于"tuple 未变但内容可能已变"的判定强化；**不替代**水印推进规则（`localApplied`/`remote` 语义不变，联合计划 §4.1/§4.7 全文继续有效）。客户端消费 = 同表示模式内**字符串不等即重拉**（建议随 B2 digest 驱动接入）。

## 3. 方案对比与乙否决（含 rev-1 blocking 4 假阴性论证）

| 维度 | **甲：无状态内容指纹（选定）** | 乙：有状态 revision（否决） |
|---|---|---|
| 语义 | 指纹 = 最终表示内容的纯函数（SHA-256） | revision = 服务端观测事件的单调计数 |
| 漏 digest 事件 | **无假阴性**：漏事件不产生假"未变化"——指纹在下次 REST 读出时如实反映内容 | **假阴性**（rev-1 blocking 4）：digest 漏发/丢失 → revision 不动 → 客户端被错误告知"未变化" |
| 重启/冷启动 | 同内容同指纹（纯函数可穿越重启） | 进程状态丢失；`turnIncarnation` 只治重启**不治漏事件**（重启代数 ≠ 事件完整性）；另有冷启动窗口（重启后首读无 revision 基线） |
| 单调性/时序 | 不提供（显式放弃，见 §4） | 提供单调序，但前提是事件不漏——前提不可保证 |
| 状态量 | 零（每消息一次 hash） | 进程内 per-message 计数器 + 失效逻辑 |
| 可冻结性 | **单边可冻结**（生成语义不依赖客户端行为） | 语义依赖事件观测完备性，无法单方承诺 |

**乙否决结论**：乙的价值（单调序）建立在"事件不漏"的不可保证前提上；其失败模式恰是最需要它的场景（事件丢失）。甲以显式放弃单调性换取"无观测型假阴性"的正确性。选甲。

## 4. 字段级 spec（冻结）

### 4.1 字段

- 名称：`contentFingerprint`（加性，消息对象顶层，与 `info`/`parts` 平级）。
- 类型：`string`，格式 `"<vN>:<sha256hex>"`——版本前缀 `v` + 整数 N + `:` + **全量** sha256 十六进制（64 字符，不截断）。
- `FINGERPRINT_VERSION`（当前 `1`）是**独立常量**：bump 条件 = **指纹输入的规范化规则变化**（增删参与字段、序列化规则改变）；**不与包版本 / `REP_VERSION` 绑定**——包发布本身不 bump vN。

### 4.2 输入与生成位置（v1.2 冻结："指纹是最终消息内容的函数，非 skeleton 期产物"）

- **输入** = 该消息**最终对外表示内容**：投影保留字段（`info` + thin `parts`）+ 最终 parts 集合。
- **生成点** = 消息最终组装点：
  - 非 merged 列表：`skeleton_message()` 投影完成时（`src/oc_slimapi/skeleton.py`）。
  - `mode=merged`：**full parts splice 完成后重算**——`src/oc_slimapi/routes/messages.py` `_merge_fulls_and_pack` splice 站点对每条**被 splice** 消息调用 `recompute_fingerprint(msg)` 覆盖 skeleton 期指纹（重算成本 = per-message 一次 hash，merged 本就是重路径）。
- 由此对 merged/非 merged 一致成立：full 明细变化（parts 内容变）→ 最终内容变 → 指纹变；skeleton 字段变 → 指纹变。

### 4.3 规范化规则（冻结）

1. **排除 `contentFingerprint` 字段自身**（防自引用）——重算含旧指纹的输入与无指纹输入产出相同指纹。
2. canonical 序列化 = `orjson.dumps(msg_without_fingerprint, option=OPT_SORT_KEYS)`；**parts 按上游顺序，不重排**（上游序即语义序；列表顶层 sort 仅发生在消息间，不触碰消息内 parts）。
3. 数值/字符串原样参与（不做数值规范化——投影产物无浮点歧义）。
4. SHA-256 全量 hexdigest，`"v1:"` 前缀。

### 4.4 终态语义（v1.3 密码学严谨化；发版即冻结）

同一 `vN` 下：

1. **确定性**：相同规范化输入必得相同指纹（确定性构造保证；跨进程/跨重启成立——纯函数无状态）。
2. **指纹不同 ⟹ 规范化输入不同**（逆否命题，构造保证，无前提）。
3. **相同指纹指示内容相同**——仅以 **SHA-256 碰撞概率可忽略**（2^-256 量级）为前提的**工程保证，非数学双射**。
4. **不提供单调性/时序语义**（显式声明）：无 revision 排序——设计选择：无状态使指纹不依赖事件观测，在碰撞可忽略工程模型下不产生观测型假阴性；以显式放弃单调性换取正确性。**不得**从指纹推导"先后/新旧"。
5. **跨表示模式不可比较**（v1.3）：比较命名空间 = **单一表示模式**。默认 skeleton 列表与 `mode=merged` 的最终 parts 表示不同（skeleton parts vs full parts），同一上游消息在两模式下通常得到**不同指纹**——"指纹 = 最终表示的函数"的直接推论。**客户端不得跨模式比较指纹**。实现侧 `vN` 前缀**不**区分模式（模式属请求参数而非指纹规范），以契约文字约束比较范围。
6. **digest 事件无关性**：指纹是内容的纯函数——不发/乱序/重复 digest 三态下指纹不变；漏 digest 不产生假"未变化"。
7. merged **降级语义**：full 获取失败 / 预算不足 / 坏 JSON / 非 dict / `parts` 非 list 五类降级路径**不执行重算**——消息保留 skeleton 期指纹（最终对外表示即原 skeleton，指纹与其一致）。
8. `message_fingerprint_enabled=false`（ops 回退）：字段缺省，响应与今天逐字节一致；`REP_VERSION` 同步纳入该开关状态（Batch 2 联动——ETag 全变，不误 304）。

### 4.5 golden vector（固定输入 → 固定指纹）

输入（消息投影产物，含旧指纹——演示排除自身规则）：

```json
{
  "info": {"id": "msg_golden", "role": "user", "time": {"created": 1000}},
  "parts": [{"id": "prt_1", "type": "text", "text": "hello"}],
  "contentFingerprint": "v1:0000000000000000000000000000000000000000000000000000000000000000"
}
```

期望输出（`sha256` over `orjson.dumps({info, parts}, OPT_SORT_KEYS)`）：

```
v1:e8b0deefd04c0f5d293ef1afd54c4f4b9dd0e190f52e07b5a5281fda3dce6f71
```

（实现后由测试锁定；若实现使该值变化 = 规范化规则变化 = 必须 bump `FINGERPRINT_VERSION`。）

## 5. 实现映射

| 冻结项 | 实现位置 |
|---|---|
| `FINGERPRINT_VERSION=1` + `compute_message_fingerprint` + `recompute_fingerprint` | `src/oc_slimapi/skeleton.py` |
| `skeleton_message/messages(..., fingerprint=False)` 加性注入 + `SkeletonLimits.fingerprint` 加性字段（纯函数默认不加——既有测试不变；路由经 `SkeletonLimits(..., fingerprint=config.message_fingerprint_enabled)` 传真值，worker 签名不变） | `src/oc_slimapi/skeleton.py` |
| 非 merged 生成点 | `routes/messages.py` 两路径构造 `SkeletonLimits` 处（lease/直连）→ `_project_list_sorted_and_pack` 投影完成时注入 |
| merged splice 重算点 | `routes/messages.py` `_merge_fulls_and_pack` splice 分支（五类降级不进分支=不重算） |
| `message_fingerprint_enabled=True`（env `OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED`） | `src/oc_slimapi/config.py` |
| REP_VERSION 联动 | `src/oc_slimapi/etag.py:68-69`（Batch 2 已预埋真开关读取，config 落地即联动） |

## 6. 性能实测（2026-08-16，rev-6 C2 补强；编排者复测）

**方法**：进程内 `time.perf_counter` 差分，N=20000 次 × 5 轮（1000 次预热后测量），报告 mean/median/min + 原始轮次。**不加 CI 计时断言**（防 flaky）——证据形式 = 本节数据 + 下方完整复现脚本。

**测试环境**：AMD Ryzen 7 PRO 8845HS（x86_64），开发机 loopback 部署同机，Python 3.14（`.venv`）。

**代表性 payload**（脚本内联，与下表一一对应）：

- `skeleton`：info 7 字段（id/role/time/providerID/modelID/cost/tokens）+ 3 个 text part → orjson 序列化 **398 B**
- `merged_full_10parts`：同 info + 10 个含 `metadata`（agent/step/200 字符 detail）的 full part → **3,360 B**

**结果**：

| payload | body | mean | median | min | 原始轮次（µs/次） |
|---|---|---|---|---|---|
| skeleton | 398 B | **1.27 µs** | 1.23 µs | 1.23 µs | 1.23, 1.23, 1.23, 1.39, 1.30 |
| merged_full_10parts | 3,360 B | **3.76 µs** | 3.70 µs | 3.63 µs | 3.70, 3.63, 3.93, 3.83, 3.70 |

**一页增量估算**：默认列表一页 50 条 → 50 × 1.27 µs ≈ **64 µs**；merged 一页 50 条全 splice 重算 → 50 × 3.76 µs ≈ **188 µs**。两者均在既有投影/splice offload 线程内完成（指纹复用投影产物 dict，无额外序列化往返、零网络/IO）。

**结论（数据支撑）**：per-message 成本为 **µs 量级（<< 1 ms）**，一页全量指纹 < 0.2 ms——相对该页的上游 GET（loopback 数 ms 起）与整页 orjson 序列化本身**可忽略**。

**复现脚本**（临时执行，不入仓；仓库根目录运行）：

```python
# /tmp/bench_fp.py — .venv/bin/python /tmp/bench_fp.py
import time, statistics, orjson
from oc_slimapi.skeleton import compute_message_fingerprint  # 需 sys.path 含 src/ 或 pip install -e

skeleton_msg = {
    "info": {"id": "msg_bench_001", "role": "user", "time": {"created": 1755300000000},
             "providerID": "anthropic", "modelID": "claude-sonnet-4-20250514",
             "cost": 0.012, "tokens": {"input": 1200, "output": 800}},
    "parts": [{"id": f"prt_{i}", "type": "text", "text": "hello fingerprint benchmark"} for i in range(3)],
}
merged_full = dict(skeleton_msg)
merged_full["parts"] = [{"id": f"prt_{i}", "type": "text", "text": "hello fingerprint benchmark",
                         "metadata": {"agent": "build", "step": i, "detail": "x" * 200}} for i in range(10)]

for label, msg in (("skeleton", skeleton_msg), ("merged_full_10parts", merged_full)):
    body_len = len(orjson.dumps(msg))
    for _ in range(1000): compute_message_fingerprint(msg)      # 预热
    rounds = []
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(20000): compute_message_fingerprint(msg)
        rounds.append((time.perf_counter() - t0) / 20000)
    print(f"{label}: body={body_len}B mean={statistics.mean(rounds)*1e6:.2f}us "
          f"median={statistics.median(rounds)*1e6:.2f}us min={min(rounds)*1e6:.2f}us "
          f"rounds={[round(r*1e6, 2) for r in rounds]}")
```

## 7. 开放问题（仅客户端消费侧，联调项）

- token idle 后 bounded probe 是否把"指纹不等"作为 reconcile 三分法的判据权重——联调冻结。
- 指纹驱动的重拉是否可与 `/since` staging 合并去重——联调冻结。
- 多设备指纹比较（同表示模式内）是否需要随 capability 协商下发——联调冻结。

以上均**不影响**本仓生成语义（已冻结）。
