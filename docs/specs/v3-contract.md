# oc-slimapi v3 wire 契约（design-v3 rev2）

> 状态：**DRAFT rev2**（2026-08-16，按 rev-1 设计评审 FAIL 6.8 的 8 blocking + 2 conditions 重写；待复评转正式）。
> 方向决策（不可推翻）：`docs/ocmar/plans/2026-08-16-single-entry-roadmap.md` §5。v2 权威：`docs/specs/v2-contract.md`。
> 本版起所有条款标 **[冻结]**（design-v3 评审通过后即为正式契约文字）或 **[计划]**（非契约承诺）。webui 已收到的方向性通知（无版本段路径/发现端点/envelope 字段名/directory query/SSE 帧不变/错误体不变/gzip-ETag 不变）全部保留；其中标过"范围可能增删"的草案细节（envelope 适用集）本版冻结为 messages/sessions 两端点（status 除外，理由见 §4.3）。

---

## §0 继承基线与差异清单 [冻结]

1. **v3 = v2 契约在基线 `v1.6.0`（commit `421ffb4`）的全量继承 + 本文件逐条差异覆盖**。凡本文件未提及的语义（端点集、参数、投影、SSE 帧形、资源上限 `max_response_bytes`/T3、错误映射、gzip 族划分、coalescing/缓存等内部行为披露）**逐字沿用 v2 契约**。
2. 差异面（且仅此差异面）：§2 版本选择器；§3 发现端点（新增端点）；§4 envelope（messages/sessions 响应形状）；§5 directory 接受形式；§6 ETag/Vary/304 细则；§9 v2 移除观测（access log 加性字段）。
3. v3 上线为**原子开关**（单一 release，`available` 含 `3` 当且仅当 §2-§7 全部 surface 同时可用；不存在部分 v3）。

## §1 总则与头退役范围 [冻结]

1. 服务路径**无版本段**（`/slimapi/*` 不变）；v3 语义由 §2 选择器激活。
2. **退役头清单（穷举，仅此四个，客户端→sidecar 方向）**：`X-Slimapi-Version`、`X-Opencode-Directory`、`X-Next-Cursor`、`X-Complete`。v3 请求不要求也不解析前两个，不产出后两个（§4/§6）。
3. **不在退役清单**（澄清）：`X-Slimapi-Subscriber-ID`（server→client 响应头，订阅追踪）与 `X-Accel-Buffering`（代理提示）保留；`ETag`/`Vary`/`Cache-Control`/`Content-Encoding` 标准 HTTP 头保留；并行期 v2 语义下四个退役头照常工作（§2.2）。

## §2 版本选择器状态机 [冻结]

选择器 = query 参数 `v`（sidecar **保留参数**，dispatch 层消费，**永不转发上游**；与 `?directory=` 同层剥离后余参照常路由）。

| 请求形态 | 判定 | 行为 |
|---|---|---|
| 无 `v` | v2（缺省） | **完全现行 v2 管线，含 `X-Slimapi-Version` 头门禁**（缺头 → `version_required`，同 v2）。缺省 ≠ "无头即 v2"。 |
| `v=3` | v3 | v3 语义：无版本头要求；头若同时出现**被忽略不报错**（迁移期宽容，`v` 优先级高于头）。 |
| `v=2` | v2（显式） | 同缺省 v2 管线（含头门禁）。 |
| `v` ∈ 其他整数（4、1…） | 不支持 | 400 `unsupported_version`，body `{"code":"unsupported_version","supported":[2,3]}`。 |
| `v` 非整数 / 空 / 多值不同（`v=2&v=3`） | 畸形 | 400 `invalid_version_selector`。多值**同值**（`v=3&v=3`）宽容接受。 |
| `GET /slimapi/versions`（归一化路径） | 无条件豁免 | 不经选择器、不经版本头门禁、可 gzip（§3）。 |

SSE 端点同表（`/slimapi/events?v=3`、`/slimapi/sessions/{sid}/stream?v=3`）；畸形/不支持在**开流前** 400（普通 JSON 错误体）。

## §3 发现端点 [冻结]

```
GET /slimapi/versions → 200
{"current": 3, "available": [2, 3],
 "capabilities": {"2": {"etag": true, "contentFingerprint": true, "thinRoutes": ["todo","children","diff"]},
                   "3": {"envelope": ["messages","sessions"], "directoryQuery": true, "protocolHeadersRetired": true}},
 "sidecarVersion": "1.7.0"}
```

约束：`current ∈ available`；`available` 唯一、升序；`capabilities` = map（key=版本字符串，value=object[string→bool|string[]]）；**消费方必须忽略未知字段与未知 capability key**（加性演进位）；`Cache-Control: no-store`（发现必须新鲜）；按 `Accept-Encoding` 协商 gzip、`Vary: Accept-Encoding`；无 ETag（no-store 下无意义）。`available` 含 `3` 的条件见 §0.3。

## §4 envelope [冻结]（仅 messages/sessions；status 不 envelope 化）

1. **`GET /slimapi/messages/{sid}`（v=3）**：`{"items": [<v2 裸数组逐字>], "nextCursor": <string|null>}`——`nextCursor` 语义与 v2 `X-Next-Cursor` 逐字相同（游标不回退承诺照旧）；无 `complete` 字段（v2 messages 本无 `X-Complete`）。
2. **`GET /slimapi/sessions`（v=3）**：`{"items": [...], "complete": <bool>}`——`complete` 语义与 v2 `X-Complete` 逐字相同，**继承其非权威性强制语言**（不得据此判定权威全集/空/覆盖完整性，v2 §`X-Complete` 条款全文并入）；无 `nextCursor`（v2 sessions 无前向 cursor）。
3. **`GET /slimapi/sessions/status`（v=3）**：**不 envelope 化**——v2 形状本为 `Record<SessionID,…>` map，无分页头存在，envelope 无可迁移对象；v3 差异仅 §5 directory 形式。
4. 边界：**错误响应不 envelope 化**（4xx/5xx 沿用 v2 错误体）；**304 无 body**（§6.4）；`?v=3` 与其他 query（limit/start/roots/search/mode/…）任意组合，envelope 只改外壳。

## §5 directory 矩阵 [冻结]

1. **canonical 形式：`?directory=` query**（v=3 时）；语义与 v2 头逐字相同（选择 opencode 工作目录实例；缺省 = sidecar 默认）。
2. **并行期兼容**：v=3 请求仍接受 `X-Opencode-Directory` 头（roadmap 既定）。**双现规则**：query 与头同时出现——归一化后**同值** → 正常；**不同值** → 400 `directory_conflict`（body 含两个收到的值）。
3. 接受矩阵（v=3）：接受 directory 的路由 = v2 端点表中接受头的全集（messages/sessions/status/todo/children/diff/agent/command/questions/permissions）；全局路由（events/health/versions/directories/actions）不接受（出现 → 400 `invalid_directory`？**否**——宽容忽略，与 v2 events 现行为对齐）；token stream（`/stream`）directory 为 no-op 忽略（v2 现行为）；allowlist 写路径（prompt_async/PATCH session 等）在 v3 批次 1 **维持 v2 形式**（仅头），批次 2 随 allowlist 细则一并处理——"头功能不减"的完整兑现点在批次 2，本条为已知的过渡缺口。

## §6 ETag / Vary / 304 [冻结]

1. **validator 域隔离**：`representation_version` 输入增加 wire 版本标记（`v3`）——v2 与 v3 的 validator 互不匹配（body 形状不同，envelope 化使然；防跨语义误 304）。
2. **Vary**：并行期 directory 头仍被接受 → 受影响的 4 路由（messages/sessions/agent/command）`Vary: Accept-Encoding, X-Opencode-Directory` **照旧**；`?v=`/`?directory=` 属 URI 组成，天然进 cache key，不加 Vary 条目。v2 头彻底退役（v2 移除）时同步评估去 `X-Opencode-Directory` Vary 值。
3. ETag 生成、`If-None-Match` 弱比较、`*`、judge 三态逻辑（coding-specific 验证器）全部沿用 v2/Batch 2 语义；envelope 路由的 canonical 输入 = envelope body。
4. **v3 304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`（标准头）；**不复制** `X-Next-Cursor`/`X-Complete`——客户端从**缓存的 envelope** 取 `nextCursor`/`complete`（它们在 200 envelope body 内，随 validator 命中天然一致）。

## §7 SSE [冻结]

- 两端点（`events`、`/stream`）接受 `?v=3`（§2 状态机）；**帧名、帧形、`Last-Event-ID`、resync/heartbeat 语义零变化**（v2 全继承）。
- 组合：`?v=3&tokens=1` 合法（events）；`?v=3&directory=` 在 events 为宽容忽略（§5.3）、在 `/stream` 同；选择器畸形 → 开流前 400（普通 JSON 错误体，非 SSE 帧）。

## §8 错误体 [冻结]

结构 `{"code": "...", …上下文字段}` 与 code 集合沿用 v2 全集；新增仅三个：`unsupported_version`、`invalid_version_selector`、`directory_conflict`（§2/§5 定义）。422（FastAPI 参数校验）形态不变。

## §9 v2 移除判据与观测 [冻结]（对实现的硬要求）

1. **access log 加性字段 `wireVersion`**（"2"|"3"，选择器解析结果；v2 缺省请求记 "2"）+ **`selectorResult`**（"absent"|"v2"|"v3"|"rejected"）——实现落点 `traffic_accounting.py` / `access_log.py`（现仅记 path，:292-308/:299-318），随 v3 批次 1 交付。
2. **SSE 活跃连接观测**：连接建立/断开各记一条 access log（含 `wireVersion`、SSE 标记、时长），消除长连接盲区。
3. **移除判据（全部满足才启动 v2 退役流程）**：① 连续 ≥14 天滚动窗口内成功 v2 REST 请求为 0（`selectorResult=="absent"|"v2"` 且 2xx）；② 窗口内无活跃 v2 SSE 连接（含断开记录核对）；③ ocdroid 组书面确认改造完成；④ webui 生产流量全 `v=3`。退役 = 破坏性变更，走 major 版本 + `X-Slimapi-Version` bump 3。

## §10 最低测试矩阵 [冻结]（正式化前必须存在）

selector 全状态（§2 表逐行）× directory 组合（无/仅 query/仅头/双现同值/双现冲突）× ETag 4 路由（identity/gzip × 200/304，v2/v3 validator 隔离断言）× envelope 两端点（含 nextCursor 游标语义/complete 非权威复制）× 413/422/transform_busy/version_required/三个新错误 code × 两 SSE 端点（v3 开流/畸形 400/长连接 access 记录）× **存量回归：旧 ocdroid 形态（无 v + header=2）逐字节不变**。

## §11 里程碑 [计划——非契约承诺]

v3 批次 1（本契约 §2/§3/§4/§9 观测 + §10 矩阵）→ 门控评审 → 发版（`available:[2,3]` 原子公告）；批次 2（§5.3 写路径 directory + allowlist 联动，随 Phase 2）；v2 退役（§9 判据触发，major）。联调 tag 经跨会话通知送达 webui/ocdroid。
