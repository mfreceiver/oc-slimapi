# oc-slimapi v3 wire 契约（design-v3 rev5）

> 状态：**DRAFT rev5**（2026-08-16；rev1 8 blocking → rev2 → rev3（4B+3C）→ rev4（5B+3C）→ rev5 按四评 2 blocking + 4 conditions 修订；待五评转正式）。
> 方向决策（不可推翻）：`docs/ocmar/plans/2026-08-16-single-entry-roadmap.md` §5。v2 权威：`docs/specs/v2-contract.md`。
> 条款标 **[冻结]** 或 **[计划]**（非契约承诺）。

---

## §0 继承基线与差异清单 [冻结]

1. **v3 = v2 契约在基线 `v1.6.0`（commit `421ffb4`）的全量继承 + 本文件逐条差异覆盖**。凡未提及语义（端点集、参数、投影、SSE 帧形、资源上限、错误映射、gzip 族划分、指纹、catalog TTL/coalescing、token stream 帧形等）**逐字沿用 v2**。
2. 差异面（且仅此）：§2 选择器；§3 发现端点；§3a health/ready 版本身份；§4 envelope；§5 directory 接受形式（**含写路径**）；§6 ETag/Vary/304；§9 移除观测。
3. **原子上线**：`available` 含 `3` 当且仅当 §2–§7 全部 surface（含 §5.2 catch-all 写路径 directory query）+ §9 观测 + §10 测试矩阵全部就绪并通过——单一 release，不存在部分 v3，不存在"头已退役"与"头仍被接受"并存的公告。

## §1 头退役范围（按方向拆分）[冻结]

| 头 | 方向 | v3（`?v=3`）行为 | 彻底移除时点 |
|---|---|---|---|
| `X-Slimapi-Version` | 请求→sidecar | **忽略**（出现不报错；`v` 优先） | v2 退役（随契约 ID bump 一并移除，不再单独 bump——见 §9.3） |
| `X-Opencode-Directory` | 请求→sidecar | **仍解析**（并行期兼容输入，参与 §5.4 冲突判定）——canonical 是 query，头是兼容形式 | v2 退役 |
| `X-Next-Cursor` | sidecar→响应 | envelope 路由**不再产出**（§4；v2 语义请求照旧产出） | v2 退役 |
| `X-Complete` | sidecar→响应 | 同上 | v2 退役 |

不在退役清单：`X-Slimapi-Subscriber-ID`、`X-Accel-Buffering`（server→client，保留）；`ETag`/`Vary`/`Cache-Control`/`Content-Encoding`（标准头，保留）。

## §2 版本选择器状态机 [冻结]

选择器 = query 参数 `v`（sidecar **保留参数**，dispatch 层消费，**永不转发上游**——对 `/slimapi/**` 与 catch-all 代理路径一律成立；其余 query 原样转发）。

**词法**：合法值 = `^[1-9][0-9]*$`（无符号/无空白/无前导零/非零首位/非空；`03`、`+3`、` 3`、`3.0`、`0`、空串 → 一律**词法非法**）。

| 请求形态 | 判定 | 行为 |
|---|---|---|
| 无 `v`（`/slimapi/**` 路由） | v2（缺省） | **现行 v2 管线，含 `X-Slimapi-Version` 头门禁**（缺头 → `version_required`）。缺省 ≠ "无头即 v2"。 |
| 无 `v`（catch-all / 非 `/slimapi` 路径） | 不适用 | 原样透传（legacy 行为逐字节不变）；观测 `selectorResult=not_applicable`（§9）。 |
| `v=3` | v3 | v3 语义（任意路径含 catch-all）；版本头若同时出现被忽略不报错。 |
| `v=2` | v2（显式） | 同缺省（`/slimapi/**` 含头门禁；catch-all 上等同无 `v` 透传但剥离 `v`）。 |
| `v` 词法合法但值不支持（4、5、1…） | 不支持 | 400 `{"code":"unsupported_version","supported":[2,3]}`。 |
| `v` 词法非法（含 `0`）/ 多值不同 | 畸形 | 400 `{"code":"invalid_version_selector"}`。多值**同值**宽容接受。 |
| `GET /slimapi/versions`（归一化路径） | 无条件豁免 | 不经选择器、不经版本头门禁（§3）；**仅 GET**——其他方法 **405 优先于一切**：非 GET 请求不看 `v`（无 `v`/`v=3`/`v=0`/多值均同）直接 405，`Allow: GET`；豁免不扩展到非 GET。 |

SSE 两端点同表；畸形/不支持在**开流前** 400（普通 JSON 错误体）。

## §3 发现端点 [冻结]

```
GET /slimapi/versions → 200
{"current": 3, "available": [2, 3],
 "capabilities": {"2": {"etag": true, "contentFingerprint": true, "thinRoutes": ["todo","children","diff"]},
                   "3": {"envelope": ["messages","sessions"], "directoryQuery": true, "versionHeaderOptional": true}},
 "sidecarVersion": "1.7.0"}
```

约束：`current ∈ available`；`available` 唯一升序；`capabilities` = map（key=版本字符串，value=object[string→bool|string[]]）；**消费方必须忽略未知字段与未知 capability key**；`sidecarVersion` = 动态包版本（importlib metadata，同 `/slimapi/health` 的 `sidecar.version`，非硬编码）；`Cache-Control: no-store`；无 ETag（低频小响应，304 收益可忽略——`no-store` 与 ETag 可并存是本仓既有实践，见 §6，此处是收益取舍而非语义约束）；gzip 族 = 与 health/ready/metrics 同族（`json_response` 无条件压缩族，`Vary: Accept-Encoding`）。`available` 含 `3` 的条件 = §0.3 原子全集。

## §3a health/ready 的版本身份 [冻结]

`/slimapi/health`、`/slimapi/ready` 按 §2 状态机协商，两端点**字段集各自沿用现 shape**（不新增/删除字段），仅既有字段的取值随 selector 视图：

- **health v2 视图**（无 `v` 或 `v=2`）：逐字节沿用现行为——含 `slimapi.slimapi_contract: 2`（仅 health 有此字段，ready 现无任何 contract 标识字段——`routes/health.py:14-68` vs `:71-104`）；v3 视图该字段值改 `3`。ready 两视图均**不新增** contract 字段（保持现 shape；版本协商归 `/versions`+health）。
- **`server.api_version` / accepted range**（两端点共有字段）：v2 视图冻结 `2` / `[2,2]`（v2 客户端可见的历史快照语义，非实现 bug）；v3 视图 `3` / `[2,3]`。`schema.clientMin/clientMax` 同步视图（v2 视图 [2,2]，v3 视图 [2,3]）。
- **退役后（契约 ID bump 3 时同步生效）**：仅 v3 视图存续——`available:[3]`、accepted `[3,3]`、`schema.clientMin/clientMax=[3,3]`、health `slimapi_contract:3`；`?v=2`/无 `v` 请求按新 major 契约处理（404 或 unsupported，属退役发版范畴）。

## §4 envelope [冻结]（仅 messages/sessions；status 不 envelope 化）

1. **`GET /slimapi/messages/{sid}`（v=3）**：`{"items": [<v2 裸数组逐字>], "nextCursor": <string|null>}`——语义同 v2 `X-Next-Cursor`（游标不回退照旧）；无 `complete`。
2. **`GET /slimapi/sessions`（v=3）**：`{"items": [...], "complete": <bool>}`——语义同 v2 `X-Complete`，**继承其非权威性强制语言**（v2 §`X-Complete` 条款全文并入：不得据此判定权威全集/空/覆盖完整性/冷启动结束）；无 `nextCursor`。
3. **`GET /slimapi/sessions/status`（v=3）**：**不 envelope 化**（v2 为 `Record<SessionID,…>` map，无分页头）；v3 差异仅 §5。
4. 边界：错误响应不 envelope；304 无 body（§6.4）；`?v=3` 与其他 query 任意组合。

## §5 directory 矩阵 [冻结]

1. **canonical：`?directory=` query**（v=3）；语义与 v2 头逐字相同（选工作目录实例；缺省 = sidecar 默认）。
2. **消费集**（directory 参与路由/转发的端点，= v2 端点表接受头的全集）：`messages/{sid}`、`sessions`（列表 + status）、`todo`/`children`/`diff`、`agent`/`command`、**catch-all 代理路径（含写：prompt_async/PATCH session 等）**——dispatch 层将 query 形式转换为上游 `X-Opencode-Directory` 转发（实现细节，wire 上等价）。**作用域（关键）**：`v`/`directory` 的消费剥离**仅适用于 `v=3` 请求**（含 catch-all）；`v=2` 与无 `v` 的 catch-all **保留 legacy raw query 原样转发**（`proxy.py:182-203` 现行为，一字不动——无 `v` 的 `directory` query 也照旧透传，属 legacy 上游可见参数）。
3. **directory query 多值**（消费集 + catch-all `v=3`）：`?directory=a&directory=a` 多值**同值** → 折叠为单值正常处理；多值**异值** → 400 `{"code":"invalid_directory_selector"}`（转单一上游头要求无歧义单值；§8 同步）。`v` 多值规则见 §2（同值宽容/异值 `invalid_version_selector`）。
4. **双现规则（消费集除 stream 外）**：query 与头同时出现——归一化后同值 → 正常；不同值 → 400 `{"code":"directory_conflict","queryDirectory":<str>,"headerDirectory":<str>}`（字段名冻结）。
5. **stream 例外（唯一非消费集守卫继承）**：`/slimapi/sessions/{sid}/stream` 的 directory 是 no-op，但 v2 结构性冲突守卫**逐字继承**（`routes/token_stream.py:51-85`）：query 与头同现且归一化后不同值 → 400 `directory_not_allowed`（沿用既有错误码，不改为 `directory_conflict`）；同值/仅一头/均无 → 正常（no-op）。其余非消费集端点（`questions`/`permissions`/`events`/`health`/`versions`/`directories`/`actions`）无冲突检查，任何形式宽容忽略。
6. 并行期 v3 兼容头（§1 表）；v2 语义请求照旧仅头。

## §6 ETag / Vary / 304 [冻结]

1. **validator 域隔离**：`representation_version` 输入含 wire 版本标记——v2/v3 validator 互不匹配（envelope body 不同，防跨语义误 304）。
2. **Vary**：并行期 directory 头仍被接受 → 4 路由（messages/sessions/agent/command）`Vary: Accept-Encoding, X-Opencode-Directory` 照旧；`?v=`/`?directory=` 属 URI（进 cache key），不加 Vary。v2 退役时同步评估去 directory Vary 值。
3. ETag 生成/`If-None-Match`/`*`/judge 三态（coding-specific）沿用 v2/Batch 2；envelope 路由 canonical 输入 = envelope body。
4. **v3 304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`；不复制 `X-Next-Cursor`/`X-Complete`——客户端从缓存 envelope 取（body 内字段随 validator 命中天然一致）。

## §7 SSE [冻结]

两端点（`events`、`/stream`）接受 `?v=3`（§2 状态机）；帧名/帧形/`Last-Event-ID`/resync/heartbeat 零变化。组合：`?v=3&tokens=1` 合法；`?v=3&directory=` 按 §5.2/§5.5（events 宽容忽略；stream 守卫继承）；选择器畸形 → 开流前 400 普通 JSON。

## §8 错误体 [冻结]

`{"code": …, …上下文字段}` 与 code 集合沿用 v2 全集；新增仅：`unsupported_version`（`supported` 数组）、`invalid_version_selector`、`directory_conflict`（`queryDirectory`/`headerDirectory`，§5.4）、`invalid_directory_selector`（§5.3 directory query 多值异值）。`directory_not_allowed`（stream 守卫）为 v2 既有 code，不新增。422 形态不变。

## §9 v2 移除判据与观测 [冻结]（对实现的硬要求）

1. **access log 加性字段**（落点 `traffic_accounting.py`/`access_log.py`，随批次 1 交付）：
   - `wireVersion`: `"2" | "3" | null`（null = 无法归属：rejected/exempt/not_applicable 场景）；
   - `selectorResult`: `"absent" | "v2" | "v3" | "rejected" | "exempt"`（/versions）`| "not_applicable"`（catch-all 无 `v` 透传、非 `/slimapi` 路径；catch-all 带 `v` 时按 §2 归入 v2/v3/rejected）；
   - `directoryForm`: `"query" | "header" | "both" | "absent" | null`（null = 非消费路由）；
   - `recordType`: `"request" | "sse_open" | "sse_close"`（SSE 长连接建立/断开各记一行，消除盲区；**现有"每请求一行"消费口径改为按 `recordType=="request"` 过滤**——`traffic-accounting.md` 手册同步更新，属批次 1 交付物）；
   - **SSE lifecycle 关联**：每条 SSE 生命周期分配**服务端唯一 `lifecycleId`**（进程内单调计数器，重启即换代；`X-Request-ID` 可被客户端复用，`middleware/request_id.py:29-59`，**不得作唯一配对键**——仅作关联辅助）；`sse_open` 与 `sse_close` 共享同一 `lifecycleId`（同时记录 `requestId` 供跨日志关联）；`sse_close` 附 `status`、`durationMs`、`downOut` 字节归属；**重启孤儿保守处理**——进程重启后未配对的 `sse_open` 视为关闭于重启时刻（判据②窗口内出现即计曾活跃），实现可在启动时为前一进程全部未配对 open 补记 `sse_close`（`status:"orphaned_reboot"`）。
2. **数据留存（批次 1 交付物）——重启安全日聚合 schema**：判据依赖的按日聚合持久化到 **traffic snapshot**（现保留 30 天 ≥ 判据窗口），聚合维度冻结为：`date` × `selectorResult` × `wireVersion` × `directoryForm` × `recordType` × `statusClass`（"2xx"|"3xx"|"4xx"|"5xx"）× `bucket` → `{requests: int, bytesUpIn/bytesDownOut…: int}`，外加上一天 `sseActive`（open-close 配对差 + 孤儿折算）；access log 明细保留期维持 `RETAIN_DAYS=3` 不变（聚合满足判据，明细仅排障）。跨窗口、跨重启判定依据 = snapshot 聚合 + 孤儿 close 规则。判据可机械化执行：①=窗口内无任何 `selectorResult∈{absent,v2} ∧ recordType=request ∧ statusClass=2xx` 聚合行；②=窗口内 `recordType=sse_open ∧ wireVersion∈{2,null-v2} ` 聚合行为 0（含孤儿行）；⑤=无 `directoryForm∈{header,both} ∧ statusClass=2xx` 行。
3. **移除判据（全部满足才启动 v2 退役）**：① 连续 ≥14 天成功 v2 REST 请求为 0（snapshot 聚合 `selectorResult ∈ {absent,v2}` 且 2xx）；② 窗口内无活跃 v2 SSE（lifecycle 记录 + 孤儿规则）；③ ocdroid 组书面确认改造完成；④ webui 生产流量全 `v=3`；⑤ 窗口内 `directoryForm ∈ {header,both}` 的成功请求为 0（**含写路径**——头形式全退役前置）。退役 = major：契约 ID 常量 bump 3、`X-Slimapi-Version` 请求头移除、v2 管线删除；**同步收窄公告**——`/versions` `available:[3]`、accepted `[3,3]`、health/ready 视图仅 v3（§3a 退役后条款）。

## §10 测试矩阵 [冻结]

`available` 公告（批次 1 发版）**前必须全部通过**：selector 全状态（§2 表逐行，含词法边界 `03`/`+3`/` 3`/`3.0`/`0`/空/多值同值异值 + catch-all 带 `v` 各形态 + 非 GET /versions → **405 优先于 selector 判定**（无 `v`/`v=3`/`v=0`/多值均 405 + `Allow: GET`））× directory 组合（无/仅 query/仅头/双现同值/双现冲突/非消费集忽略/questions-permissions 无 directory 断言 + **stream 守卫**：双现异值→`directory_not_allowed`、同值正常 + **多值**：query 同值折叠正常/异值→`invalid_directory_selector` + **legacy 透传**：无 `v`/`v=2` catch-all 的 `directory` query 原样转发断言）× ETag 4 路由（identity/gzip × 200/304，v2/v3 validator 隔离断言）× envelope 两端点（nextCursor 语义/complete 非权威语言复制）× health/ready 双视图（health：v2 逐字节快照含 `slimapi_contract:2`/v3 视图 `3`；ready：两视图**均不新增** contract 字段、api_version/range 视图断言 [2,2]/[2,3]）× 错误面（413/422/transform_busy/version_required + 四新 code）× 两 SSE 端点（v3 开流/畸形开流前 400/lifecycle 记录含 lifecycleId 配对与孤儿 close）× `/versions` 端点（豁免/形状/capability/未知字段容忍/非 GET 405）× 观测字段（null/exempt/not_applicable/recordType/directoryForm/catch-all 带 v 归属/lifecycleId 单调唯一）× snapshot 聚合维度（date×selectorResult×wireVersion×directoryForm×recordType×statusClass×bucket + sseActive 重启安全）× **catch-all 写代表**（`POST /session/{sid}/prompt_async?v=3&directory=X`、`PATCH /session/{id}?v=3`——dispatch 剥离 v/directory、转发上游头、其余 query 原样；无 v 形态逐字节回归）× **存量回归：旧 ocdroid 形态（无 v + header=2）逐字节不变**。

## §11 里程碑 [计划——非契约承诺]

**批次 1 = §0.3 原子全集**（§2–§7 含写路径 directory query + §3a + §9 观测与 snapshot 维度 + §10 矩阵）→ 门控评审 → 发版公告 `available:[2,3]`；批次 2 = catch-all→allowlist（Phase 2 独立轴，非 v3 wire 范畴）；v2 退役 = §9.3 判据触发（major）。联调 tag 经跨会话通知送达 webui/ocdroid。
