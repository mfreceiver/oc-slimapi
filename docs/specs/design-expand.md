# Expand 功能设计方案 v5

> 日期：2026-08-17（v5，应用 rev-sgpt 第四轮 APPROVED WITH CHANGES 的修改项）
> 状态：**R4 评审通过（条件修改已全部应用）**，可进入 v3-contract 修订与实现阶段
> 上游对齐基准：opencode v1.18.16（`opencode-src/current/packages/schema/src/v1/session.ts`）

---

## 0. 评审裁定记录

### 0.1 R1 阻塞项（已裁定并稳定）

| # | R1 阻塞项 | 裁定 | 状态 |
|---|---|---|---|
| 1 | part 级请求缺 messageID | 采纳 | v2 起解决，保持 |
| 2 | 投影缩减是减性变更 | **owner 坚持 wire v3 内缩减** | v4 补齐契约 carve-out 与冻结计数（§8） |
| 3 | Vary 违反 terminal 契约 | 采纳 | v2 起解决，保持 |
| 4 | 引用 schema 未冻结 | 采纳 | v4 修正错误码命名唯一化（§3.1） |

### 0.2 R2 阻塞项（v3 已处理，v4 收尾）

| # | R2 阻塞项 | v3 处理 | v4 收尾 |
|---|---|---|---|
| B1 | category 表与 v1.18.16 不符 | tool-only state、删 `part_files_full`、snapshot 双覆盖 | R3 确认 RESOLVED；修正数量为 **12**（§2.2） |
| B2 | merged null-free 保证不可兑现 | 候选集扩展 | **放弃 null-free 承诺**，改为与现有 merge 架构相容的诚实模型（§4.3） |
| B3 | 契约落位/发版/时序 | 本体落位 + major + 时序 | R3 确认方向正确；补 §10 carve-out 与冻结计数修订（§8） |

### 0.3 R3 阻塞项与 major（本轮全部处理）

| # | R3 发现 | v4 处理 |
|---|---|---|
| B（新） | merged "无 null 残留"与现有架构不相容（merge 只替换 parts；16 条/8 MiB 预算下不可能全还原；ref 候选挤占 placeholder） | §4.3 重写：**placeholder-first 优先级冻结 + parts 级 best-effort + diffs 永不在 merged 批量还原 + 显式不承诺 null-free** |
| M1 | category 数 11 vs 实际 12 | 全文统一 **12**（§2.2、§6、§8、§12） |
| M2 | 错误码命名两版并存（`part_missing` vs `reason:"part_missing"`；`category_mismatch` vs `expand_category_mismatch`） | 唯一化：错误码 = `expand_target_not_found`（附 `reason:"part_missing"` 字段）与 `expand_category_mismatch`；其余写法全部删除（§3.1） |
| M3 | §10 全节 raw-proxy 条款与 expand 冲突；§0:14 / §8:107 / §10:121-123 冻结计数未更新 | §8 明确 carve-out 清单与计数修订（读组 7→8） |
| M4 | 嵌套提取值类型畸形未定义 | §3.3 冻结统一规则：**存在但类型不符 → 502；缺失或 JSON null → 200 + data 键 null** |
| m1 | 消息级 category 带多余 partID 的行为未定义 | §3.1 冻结：400 `expand_category_mismatch`（拒绝，不忽略） |
| m2 | 兼容矩阵缺 old+old 基线行 | §10 补 |
| m3 | "所有被省略字段"与 /full-only 清单矛盾 | §1.3 改为"§2.2 映射表内的被省略字段" |

### 0.4 R4（APPROVED WITH CHANGES；修改项已全部应用于 v5）

| # | R4 发现 | v5 处理 |
|---|---|---|
| M1 | §3.1 求值序与 `_fetch_full_shared` 真实阶段不符（错误状态先 drain、cap-read 先于 JSON 解码、顶层是 dict 非 list） | §3.1 按真实阶段序重写：send→status 映射→cap-read→decode→shape→locate→serialize |
| M2 | input/metadata/attachments 三 category 冻结 schema 与统一 null 规则矛盾 | §2.2 改为 `object\|null` / `object[]\|null`；§12 补 missing 与显式 null 双断言 |
| M3 | `max_expand_response_bytes` 未纳入全局内存信封 | §3.2 补 aggregate 公式 `max(两 cap) × max_transforms` + startup 拒绝 + §12 配置测试 |
| min1 | 占位 ∩ ref 交集消息去重行为未冻结 | §4.3 冻结：按 mid 去重、占位身份优先、1 槽位、1 次 full fetch |
| nit | "非 list body" 笔误；§12 "body 畸形→502" 措辞过宽 | §3.1 改"顶层非 dict body"；§12 拆分为 503（解码失败）/502（解析成功后结构畸形）两类断言 |

---

## 1. 背景与目标

### 1.1 当前 skeleton 投影的残留流量（实测）

数据源：`/home/mar/.local/share/opencode/opencode.db`（message 56 MB / 24,821 行；part 1.2 GB / 410,655 行）。SQLite 内字节为**原始未压缩值**，非 wire 实测；验收以 access-log `downOut`（wire 级、协商 gzip 已计入的传输字节，`middleware/traffic_accounting.py:17-22`、`docs/manual/traffic-accounting.md:238`）前后同窗口对比为准。

| 字段 | 实测（原始字节） | 投影现状 |
|---|---|---|
| `message.info.summary.diffs` | avg 105 KB/消息，max 1 MB 样本；1,516 user 消息携带 | 完整深拷贝 |
| `ReasoningPart.text` | avg 2.5 KB，max 50 KB（724 抽样） | 完整保留 |
| `TextPart.text` 长尾 | avg 1.1 KB，max 30 KB（654 抽样） | 完整保留 |
| `ToolPart.state.output/error` | state avg 9.6 KB（1,571 抽样） | >4 KB/字段省略，无补齐通道 |

### 1.2 缩减的兼容性边界（owner 决策，不再复议）

- **决策**：`summary.diffs` 置 null、`text`/`reasoning` 超 2 KB 省略，在 **wire v3 内直接缩减默认投影**。不引入 opt-in mode、不升 wire 版本。
- **理由**：ocdroid 是 `/slimapi/messages` 唯一消费方，同机部署、同运营者、可控发版窗口。缩减字段均有 expand 补齐通道，`/full` 语义不变。
- **义务**：规范性内容并入 `v3-contract.md`（文档修订注记，wire selector 不变）；`CHANGELOG.md` ⚠️；包版本 minor（owner 2026-08-17 决策：major 与 wire 协议版本绑定，wire 未 bump 不发 major）；`CLIENT_CHANGES.md`；部署时序 §10。

### 1.3 目标

1. **§2.2 映射表内**的被省略字段具备按需单字段获取端点（/full-only 清单见 §2.3）；
2. 服务端投影时自动生成 `expandRefs`，客户端零猜测；
3. messages 路由 downOut 显著下降（§9 验收）。

---

## 2. 端点设计

### 2.1 路由

```
GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=3[&directory=...]            # 消息级
GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v=3[&directory=...]   # part 级
```

- 挂现有 messages router，不改 app.py；首段静态路径 `expand` 与 `full` 不冲突（messages.py:1032）；
- 复用 `_resolve_messages_directory`、`_fetch_full_shared`（singleflight）、transform pool 准入；
- `category` 以 str 接收 + 手工白名单校验（不用 Enum 路径参数，避免 FastAPI 422）→ 400 `invalid_expand_category`。

### 2.2 Category 枚举（冻结，**12 项**；按 v1.18.16 schema 校正）

| Category | 级别 | 适用 part 类型 | 返回 `data` | 来源路径（session.ts 行号） |
|---|---|---|---|---|
| `info_summary_diffs` | 消息级 | — | `{ diffs: FileDiff[] \| null }` | `info.summary.diffs` |
| `part_text` | part | `text` | `{ text: string \| null }` | `part.text`（:105） |
| `part_reasoning` | part | `reasoning` | `{ text: string \| null }` | `part.text`（:121） |
| `part_state_output` | part | **tool** | `{ output: string \| null }` | `state.output`（ToolStateCompleted :280+） |
| `part_state_error` | part | **tool** | `{ error: string \| null }` | `state.error`（ToolStateError :292+） |
| `part_state_input_full` | part | **tool** | `{ input: object \| null }` | `state.input`（:261/:268 等） |
| `part_state_metadata_full` | part | **tool** | `{ metadata: object \| null }`（删 `diagnostics`） | `state.metadata` |
| `part_state_attachments` | part | **tool** | `{ attachments: object[] \| null }` | `state.attachments`（:288） |
| `part_url` | part | `file` | `{ url: string \| null }` | `part.url`（:176） |
| `part_source` | part | `file` | `{ source: object \| null }` | `part.source`（:177） |
| `part_snapshot` | part | `step-start` / `step-finish` | `{ snapshot: string \| null }` | `part.snapshot`（:236/:244，均 optional） |
| `compaction_full` | part | `compaction` | 完整 compaction part（剥离 `expandRefs`） | 整 part |

**相对 v2 的变更**（R2-B1，R3 确认）：
- PatchPart（:94-99）= `{type:"patch", hash, files: string[]}`，**无 state** → state 类 category 全部 **tool-only**；跨类型请求 → 400 `expand_category_mismatch`。
- 无 `part_files_full`：`files` 是纯路径数组（12,334 条 patch part 实测单条几十字节），skeleton 原样保留即可。
- `part_snapshot` 覆盖 step-start 与 step-finish（schema 证实两类型均有 optional snapshot）。

### 2.3 不提供 expand 的省略（/full-only，显式穷举）

| 省略项 | 理由 |
|---|---|
| `state.structured` / `state.result` / `state.raw` | 客户端无消费场景 |
| tool input 非白名单单个 key | 已有 `part_state_input_full` 整体补齐 |
| step-finish 的 `reason`/`cost`/`tokens` | 实测极小（reason ~15B、cost float、tokens ~100B） |
| reasoning 的 `metadata`/`time`、text 的 `synthetic`/`ignored`/`time` | 极小 |
| 未知上游字段、`omitted:["*"]`（compaction 超限除外）、`thin_placeholder` 的 `omitted:["parts"]` | 无语义锚点或无目标 |

---

## 3. 服务端实现

### 3.1 求值顺序与错误码（唯一规范版本）

middleware 层遵循 v3-contract §8.3（selector/directory 400 先于路由 404）；路由内部按**实际执行序**冻结：

```
1. category ∈ 白名单（str 校验）       → 400 invalid_expand_category（附 validCategories，按 §2.2 表序）
2. 级别匹配：part 级缺 partID，或消息级 category 携带 partID
                                        → 400 expand_category_mismatch（附 expectedLevel）
3. transform pool 准入                  → 503 transform_busy（Retry-After）
4. _fetch_full_shared 上游 GET（按真实阶段序冻结，R4-M1 修正）：
   a. 发送/网络失败                      → 503 upstream_unavailable（upstream_errors.py:32,45）
   b. 上游错误状态（≥400）：drain 失败   → 503 upstream_unavailable；drain 成功按状态映射：
      - 404（sid 级）                   → 404 session_not_found（upstream_errors.py:81，与 tests/test_messages_routes.py:659-677 锁定一致）
      - 5xx                            → 503 upstream_unavailable
      - 其余 4xx                        → 502 upstream_http_{N}（upstream_errors.py:83）
   c. 上游 2xx：cap-read 网络失败        → 503 upstream_unavailable
      源 body 超 max_message_bytes       → 413 expand_source_too_large（limitBytes）——**先于 JSON 解码**（超限且畸形 body → 413 而非 503）
   d. JSON 解码失败 / 顶层非 dict body   → 503 upstream_unavailable（单消息端点顶层为 dict，非 list）
5. parts 定位 partID：缺失/null/标量/元素非对象/重复 partID
                                        → 502 upstream_invalid_shape（**解析成功后**的结构畸形，区别于 4d 解码失败）
   partID 未命中                        → 404 expand_target_not_found（附 reason:"part_missing"）
6. part.type ∈ category 适用集           → 400 expand_category_mismatch（附 expectedTypes）
7. 提取（含嵌套类型校验，§3.3）+ 序列化  → 502 upstream_invalid_shape / 413 expand_fragment_too_large（limitBytes）
```

要点：503/413(源)/502 **可能先于** 404(part)/400(类型) 出现——"准入在前、先取后析"既有管线（与 /full 同构）的固有顺序，如实冻结为契约。**错误码唯一命名**：全文仅存在 `expand_target_not_found`（附 `reason` 字段）与 `expand_category_mismatch` 两个码，无 `part_missing`/`category_mismatch` 独立码。

### 3.2 双上限

| 上限 | 配置 | 默认 | 检测点 | 错误 |
|---|---|---|---|---|
| 源消息 | `max_message_bytes`（既有） | 32 MiB | `_fetch_full_shared` 读 body | 413 `expand_source_too_large` |
| 片段响应 | `max_expand_response_bytes` | 8 MiB | `orjson.dumps` 后、gzip 前 | 413 `expand_fragment_too_large` |

`OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` env 配置，startup 校验 **1 KiB ≤ v ≤ 32 MiB**（config.py :554-622 校验段同模式）。

**全局内存信封（R4-M3）**：transform 输出项的 aggregate 校验（config.py:576-598）按 `max(max_response_bytes, max_expand_response_bytes) × max_transforms` 计账——expand worker 同时持有原始 full-message bytes、解析对象、片段序列化 bytes、可选 gzip bytes，不能按普通 response cap 低估。startup 拒绝 expand cap 配置导致信封超限的组合（§12 配置测试）。

### 3.3 Extractor 契约

- 纯函数 `(msg: dict, partID: str|None) -> dict`；解析、提取、序列化、gzip 全部在 transform pool worker 线程（`to_thread`），不占事件循环；
- **定位**：`parts` 缺失/null/标量/元素非对象/重复 partID → 502 `upstream_invalid_shape`；partID 未命中 → 404（`reason:"part_missing"`）；
- **类型校验**：`part.type` 不适用 → 400 `expand_category_mismatch`；
- **嵌套类型异常统一规则（R3-M4 冻结）**：目标字段**存在但类型与冻结 schema 不符**（state 为标量、input 非对象、attachments 非数组、summary/diffs 类型错、snapshot/source/url 类型不符）→ 502 `upstream_invalid_shape`；**缺失或值为 JSON null** → 200 + `data` 对应键 `null`；
- `part_state_metadata_full`：白名单式构造返回值并显式删 `metadata.diagnostics`（与 /full LSP 剥离一致，messages.py:1122-1127）；
- `compaction_full`：返回完整 part，剥离 sidecar 注入键 `expandRefs`；
- 所有 extractor 白名单构造，不暴露 category 外字段。

### 3.4 stale reference 语义

当前态读：返回该 ID 当前字段值；part 已删 → 404（`reason:"part_missing"`），类型已变 → 400 `expand_category_mismatch`；客户端据此刷新 skeleton。成功响应携带 `messageID`/`partID` 供对账。

### 3.5 singleflight 定位（如实）

in-flight 合并 + 1 秒成功结果 grace，非缓存（singleflight.py:15-23, 155-175, 202-232）。1 秒后顺序 expand 再次完整 GET 源消息；同消息多 category 并发仅省上游 GET，各自独立解析。缓解：transform 池限并发、metrics 观测重复率；批量端点与解析 grace 缓存列为后续优化。

---

## 4. 投影策略（冻结）

### 4.1 字段规则

| 字段 | 规则 | 阈值 |
|---|---|---|
| `info.summary.diffs` | **总是省略** → `null` + 消息级 expandRefs；summary 其余 key 保留 | 无条件 |
| `TextPart.text` | ~~UTF-8 编码字节 > 阈值 → 省略为 `null` + expandRefs；否则完整保留~~ **[3.2.0] 已废止：永远全量内联，不折叠**（owner 决策 2026-08-17，对话正文不缩减；wire 见 v3-contract.md §4a.1） | ~~`text_inline_max_bytes=2048`~~ 无 |
| `ReasoningPart.text` | 同上（>阈值省略为 `null` + expandRefs）——**仍有效** | `reasoning_inline_max_bytes=2048` |
| `ToolPart.state.output/error` | 现状不变（4 KB/字段、16 KB/消息），省略时新增 expandRefs | 现状 |
| 其余（tool input/metadata/attachments、file url/source、snapshot、compaction） | 现状投影不变，按 §5.3 映射生成 expandRefs | 现状 |

原则：**整字段保留或整字段省略，不做部分截断**。多字节按 UTF-8 编码字节计数。

### 4.2 PatchPart 存量 bug 修复（P0 前置）

`_patch()`（skeleton.py:328-378）假定 `files` 为 dict 数组，对 v1.18.16 的 `string[]` 产出**空列表**（12,334 条 patch part 全受影响）。修复：`files: string[]` 原样保留 + 保留 `hash`。独立于 expand 先行合入。

### 4.3 可渲染性与 mode=merged（R3-B 重写：与现有架构相容的诚实模型）

**skeleton 模式**：`text/reasoning` part 即使 `text:null`（携带 expandRefs）**计为可渲染**——客户端看到 part 骨架 + 展开入口，而非整页 placeholder。消息级省略（diffs）不参与可渲染判定。

**merged 模式（best-effort，显式不承诺 null-free）**：

1. **候选集**：`_placeholder_pairs()` 候选从"仅占位消息"扩展为"**占位消息 ∪ 任一 part 携带 expandRefs 的消息**"；**消息级 `info.expandRefs`（diffs）不进入候选**——diffs avg 105 KB，批量还原会瞬间吃光预算，且列表场景不需要。**交集去重（R4-min1 冻结）**：同一消息同时属于两类时按 mid 去重——占位身份优先、只占 1 个 `merged_max_fulls_per_page` 槽位、只发起 1 次 full fetch（singleflight 自然合并），归入高优先级占位队列。
2. **优先级（冻结）**：**placeholder-first**——占位消息优先、按页面顺序占用预算；ref 候选仅在剩余预算（默认 16 条 full / 8 MiB，config.py:422-426）内按页面顺序 best-effort 还原。占位消息行为与现状**完全一致**（不被 ref 候选挤占）。
3. **还原范围**：与现有 merge 相同，**仅替换 `parts`**（messages.py:606-610, 635-637）；`info.summary.diffs` 在 merged 输出中**保持 null + expandRefs**，客户端按需展开或走 /full。
4. **降级**：预算耗尽 / 源超限 / 上游失败 / 畸形 body → 该消息保留 skeleton（含 null text + expandRefs），客户端有展开入口兜底——**这是特性而非缺陷**：merged 尽力还原，未还原者仍有 per-fragment 通道。

**契约表述**：merged 语义修订为"消除占位 + best-effort 还原 parts 级可展开省略"；**不承诺** null-free。placeholder 不变量（生成与 merged 行为）测试锁定不变。

### 4.4 指纹/ETag

`contentFingerprint` 与列表 ETag 基于 skeleton 字节，缩减后自动确定性变化；测试锁定（§12）。

---

## 5. 引用 schema（冻结；规范性副本随修订并入 v3-contract.md）

### 5.1 形状与放置

sidecar 专属键 **`expandRefs`**：

```json
{
  "info": { "id": "msg_x", "summary": { "diffs": null },
    "expandRefs": [ { "category": "info_summary_diffs", "messageID": "msg_x",
      "href": "/slimapi/messages/ses_s/expand/info_summary_diffs/msg_x?v=3" } ] },
  "parts": [
    { "id": "prt_y", "type": "reasoning", "text": null,
      "hasFull": true, "omitted": ["text"],
      "expandRefs": [ { "category": "part_reasoning", "messageID": "msg_x", "partID": "prt_y",
        "href": "/slimapi/messages/ses_s/expand/part_reasoning/msg_x/prt_y?v=3" } ] }
  ]
}
```

### 5.2 规则

- 数组，元素 `{category, messageID, partID?, href}`；`href` 含 `?v=3`，directory 由客户端追加；
- **去重**：每 `(category, messageID, partID?)` 至多 1 条；tool input 多 key 省略 → 仅 1 条 `part_state_input_full`；
- **排序**：category 字典序，同 category 内 partID 字典序（确定性）；
- **生成条件**（同时满足）：省略在 §5.3 映射表内；上游该字段省略时刻非 null/非空；category 与 part 类型匹配；
- **基数上界**：每 part 理论 ≤ 5（tool part 可同时省略 input/metadata/attachments/output/error 各 1 条；output/error 在合法 ToolState 下实际互斥，上界保守安全）；每消息 ≤ 1 + Σ(每 part 上界)，受消息结构约束；每条 raw 120–180 B，gzip 后 <100 B/消息；
- 空值/未知字段/§2.3 清单不生成。

### 5.3 omitted → category 映射（冻结）

| omitted | category |
|---|---|
| `info.summary.diffs` | `info_summary_diffs` |
| text part 的 `text` | `part_text` |
| reasoning part 的 `text` | `part_reasoning` |
| tool `state.output` | `part_state_output` |
| tool `state.error` | `part_state_error` |
| tool `state.input`（存在非白名单 key） | `part_state_input_full` |
| tool `state.metadata` | `part_state_metadata_full` |
| tool `state.attachments` | `part_state_attachments` |
| file `url`（省略时） | `part_url` |
| file `source` | `part_source` |
| step-start / step-finish 的 `snapshot` | `part_snapshot` |
| compaction 整体超限 | `compaction_full` |
| patch `files` | 无（原样保留，§4.2） |
| step-finish `reason/cost/tokens`、reasoning `metadata/time`、text `synthetic/ignored/time`、`state.structured/result/raw`、`*`、`parts`(placeholder) | 无（/full-only，§2.3） |

实现：`_mark()` 增加 category 归类参数；step 投影在省略 snapshot 时补记 omitted；`skeleton_message()` 在深拷贝 info 后追加消息级 `expandRefs`。

---

## 6. 响应与头（冻结）

```
200 {"category":"part_state_output","messageID":"msg_x","partID":"prt_y","data":{"output":"..."}}
    Cache-Control: no-store
    Vary: Accept-Encoding
    Content-Encoding: gzip（按协商）
错误体同 §3.1；Vary 恒为 Accept-Encoding；无 ETag/304。
```

`/slimapi/versions` capabilities["3"] 增：`"expand": {"categories":[<§2.2 表序，12 项>], "fragmentMaxBytes": <配置值>}`。

---

## 7. 与现有机制的关系

| 端点 | 粒度 | 场景 |
|---|---|---|
| `/messages/{sid}` | 列表（缩减 skeleton + expandRefs） | 列表滚动 |
| `/messages/{sid}?mode=merged` | 服务端展开列表（占位消除 + parts 级 best-effort；diffs 保留展开入口） | 一次性看全页 |
| `/full/{mid}` | 整条消息全部（**语义不变**，测试锁定字节级不变） | 详情页 |
| `/expand/{cat}/{mid}[/partID]` | 单字段 | 列表内展开片段 |

---

## 8. 契约落位（R3-M3 补 carve-out 与计数）

`docs/specs/v3-contract.md` 修订（文档修订注记，wire selector 仍 v=3）：

| 节 | 并入内容 |
|---|---|
| §0（:14） | 投影基线修订说明 + **读组计数 7→8 修订注记** |
| §3 | capabilities `expand` 对象（12 categories） |
| §5 | expand 两路由纳入 directory 消费集（同步 `selector.py _DIRECTORY_CONSUMING_PATTERNS`） |
| §6 | expand 响应头（no-store、Vary、无 ETag） |
| §8（:107） | 新错误码入 §8.3 链 + §3.1 路由内求值序（注明独立于 middleware 链） |
| §10（:121-123） | **raw-passthrough 全节条款 carve-out**：新增第 8 读组 `messages.expand`（两路由），显式声明该组**非 raw 受控代理**——变换成功 body、生成自有错误码、占用 transform pool；其余 7 读组条款不变。路由表含 **12 category 完整 schema**（适用类型/data 形状/来源路径）+ expandRefs schema + §5.3 映射表 + 双上限 + null 语义 + merged best-effort 语义 |
| §11 | 测试矩阵（§12 全集） |

配套：`INTERFACE_MAP.md` 两路由行 + 纳入 `check_routes_doc.py` 语义关键词检查；`CLIENT_CHANGES.md`；`CHANGELOG.md` ⚠️ 减性 + 加性条目。

---

## 9. 流量证据与验收

- **原始依据**（SQLite 原始字节，§1.1）：diffs ≈159 MB、reasoning ≈60 MB（超 2 KB 部分 40–50%）、text ≈23 MB（超阈值 <20%）。
- **预期**：messages bucket downOut 中 diffs 项 -60–80%（列表几乎不展开）、text/reasoning -20–40%。SQLite 原始字节 ≠ wire 收益；**验收 = access-log `downOut`（wire 级，gzip 已计入）同窗口前后对比**。
- **交叉点**：单消息展开 ≥4 片段累计可能接近一次 /full——**估算**（头开销 ~200 B/请求 + 独立 gzip 上下文损失量级推断），非实测；客户端策略：详情页直接 /full，列表零散片段用 expand（写入 CLIENT_CHANGES）。expand 不省 loopback upIn。
- 引用开销：每条 120–180 B raw，典型消息 1–3 条，gzip 后 <100 B/消息。

---

## 10. 部署时序与兼容矩阵

**时序（同运营者、同机、两仓可控）**：

1. **ocdroid 先发**：容忍旧 sidecar——无 `expandRefs` 按现状渲染全文；expand 404 → 回退 /full；
2. **sidecar 后发（包版本 minor；wire 仍 v=3）**：缩减生效，唯一消费方已具备渲染能力；
3. 窗口期任意组合无功能性破坏（减性影响 owner 接受）。

| 客户端 \ sidecar | 旧 sidecar | 新 sidecar |
|---|---|---|
| **旧客户端** | 基线：现状全文渲染（不受影响） | **已知减性影响**（owner 接受）：长 text/reasoning 显示为空 + "查看全文"仍可用（/full 不变）；diffs 缺失不影响列表基础功能 |
| **新客户端** | 无 expandRefs/expand 路由 → 现状全文渲染；expand 请求 404 → 回退 /full | 完整 expand 体验 |

---

## 11. 实施计划

| 阶段 | 内容 | 估算 |
|---|---|---|
| P0 | **`_patch()` string[] 修复**（独立先行） | ~20 行 + 测试 |
| P1 | selector directory 消费集 + expand 路由骨架 + 错误映射（复用 upstream_errors 既有码） | ~130 行 |
| P2 | 12 个 extractor + 双上限 + worker 线程化 | ~240 行 |
| P3 | skeleton：阈值缩减 + expandRefs 生成 + **merged 候选扩展（placeholder-first）** | ~170 行 |
| P4 | 配置项 + versions capabilities + metrics 计数 | ~70 行 |
| P5 | 测试（§12 矩阵） | ~900 行 |
| P6 | 契约五文档 + check_routes_doc 语义检查 + check.sh 绿 | ~120 行 |

---

## 12. 测试矩阵（P5 验收范围）

- **selector/directory**：缺 v / v=2 / 畸形 v → 400；多 directory / header 残留 → 400；selector 错误压制 category 错误；`Vary` 恒 `Accept-Encoding`（含错误响应）；expand 路由 directory 消费生效
- **求值序**：transform 池满 → 503 先于 part 404；源超限 → 413(源) 先于 part 404；上游 sid 404 → **`session_not_found`**；上游网络/5xx → 503 `upstream_unavailable`；上游其余 4xx → 502 `upstream_http_N`；**超限且 JSON 畸形的成功 body → 413（cap-read 先于解码，R4-M1 锁定）**；**错误码 JSON 精确断言**（`expand_target_not_found`+`reason`、`expand_category_mismatch`，无独立 `part_missing`/`category_mismatch` 码）
- **级别匹配（R3-m1）**：part 级缺 partID → 400；**消息级 `info_summary_diffs` 携带 partID → 400**；partID 未命中 → 404；patch part 请求 state 类 → 400；step-finish 请求 `part_snapshot` → 200；类型错配矩阵逐 category
- **extractor**：12 category 各 1 正例 + 类型错配负例；**解码失败/顶层非 dict → 503（upstream_unavailable）与解析成功后 parts/嵌套结构畸形 → 502（upstream_invalid_shape）两类显式区分断言（R4-nit）**；malformed parts（缺失/null/标量/非对象元素/重复 partID）→ 502；**嵌套类型异常**（state 标量、input 非对象、attachments 非数组、diffs 类型错、snapshot/source/url 类型不符）→ 502；metadata 无 diagnostics 泄露；compaction_full 剥离 expandRefs；缺失字段 vs 显式 null → 200 + data 键 null；**input/metadata/attachments 三 category 的 missing 与显式 null 双断言 → 200 + data 键 null（R4-M2）**
- **边界**：源 body 恰好/超 1 字节上限；片段恰在/超 8 MiB（含 wrapper 字节推入超限）；UTF-8 多字节按编码字节计数；2 KB 阈值两侧
- **singleflight**：同消息同/异 category 并发 → 1 次上游 GET；expand 与 /full 并发 → 1 次；跨 directory/消息不合并；**1 秒 grace 后顺序 expand 触发新 GET**；池满 → 503
- **skeleton/引用**：内联阈值内无引用；引用排序去重确定性；tool part 多重省略 ≤5 引用且排序；空/null 上游字段不生成引用；diffs 恒 null + 引用；**patch files string[] 原样保留**（P0 回归）；fingerprint/ETag 确定性变化
- **merged（R3-B 新模型）**：**placeholder 优先级**——ref 候选不挤占占位消息（预算耗尽时占位先得）；长 text/reasoning-only 消息在预算内被还原全文；**预算外消息保留 null + expandRefs（best-effort 语义锁定）**；**merged 输出中 `info.summary.diffs` 恒为 null + expandRefs（不还原，语义锁定）**；占位消息 merged 行为与现状完全一致；混合消息两类候选都按优先级处理；**占位 ∩ ref 交集消息（R4-min1）：按 mid 去重、占位身份优先、仅占 1 个槽位、仅 1 次 full fetch**
- **配置（R4-M3）**：`max_expand_response_bytes` <1 KiB / >32 MiB → startup 拒绝；**expand cap 高于 `max_response_bytes` 且 aggregate 信封（`max(两 cap) × max_transforms`）超限的组合 → startup 拒绝**
- **stale**：part 删除 → 404 + 客户端刷新流；part 类型变更 → 400；字段置 null → 200 data:null
- **兼容**：旧客户端路径骨架结构兼容性快照；**/full/{mid} 输出字节级不变**证明；**兼容矩阵四象限全覆盖**（含 old+old 基线）
- **门禁**：check_routes_doc 语义关键词检查通过（12 category 数量一致）；check.sh 全绿

---

## 13. 关键设计决策

1. **category 枚举而非 JSON path** — 服务端控制暴露面，逐 category 定上限与过滤；
2. **独立端点而非 /full?fields=** — /full 保持"完整消息"语义（测试锁定）；
3. **服务端生成引用** — 投影代码知道省略了什么；
4. **显式 mid/partID** — 上游 API 必需 messageID，利于日志/对账；
5. **wire v3 内缩减（owner 决策）** — 唯一消费方可控发版；规范性内容并入契约本体（含 §10 carve-out）；包版本 minor；客户端先行（§10）；
6. **整字段省略而非截断** — 延续 skeleton 原则；
7. **2 KB 阈值** — 实测分布下保留多数内联、砍掉长尾；
8. **patch files 原样保留 + 存量 bug 修复** — `string[]` 无省流价值；
9. **merged best-effort 而非 null-free** — 与现有 merge 架构（仅替换 parts、预算有限、失败降级）相容：placeholder-first、diffs 永不批量还原、未还原者保留展开入口；不冻结无法兑现的保证。
