# E6 文档与部署面精读笔记（审计 Phase 1 · 2026-08-20）

> 只读审计产物。本文件为唯一写入物；仓库其余文件零改动。精读基线：main 工作树（包版本 4.4.0，2026-08-20；wire 钉死 `(3,4)`）。
> 引用格式：`文件:行号`；契约节引用 `v3 §n` = `docs/specs/v3-contract.md`，`v4 §n` = `docs/specs/v4-contract.md`。
> 本笔记只记录事实与漂移候选，不做裁决（裁决归 Phase 2+）。

---

## 1. v3-contract.md 逐节摘要（§0–§12，含 §3a/§4a/§4b/§5.7a，共 16 单元）

### §0 继承基线与差异清单 [冻结]（v3-contract.md:10-20）
- v3 = v2 契约在基线 v1.6.0（commit 421ffb4）的全量继承 + 逐条差异覆盖；未提及语义逐字沿用 v2（§0.1）。
- 差异面穷举：§1 头退役 / §2 选择器 / §3+§3a 发现与 health / §4 envelope / §4a+§4b expand（[3.1.0]）/ §5 directory / §6 ETag / §7 SSE / §8 错误与 catch-all / §9 观测 / §10 路由收编（读组 7→8→9、写 12→17）。
- 两步走原子性：sidecar 2.0.0（v2/v3 并行）→ ocdroid 3.0.0（全量切 v3 + smoke）→ sidecar 3.0.0（删 v2/头/catch-all 关闭）；前置判据 §9.3。
- 修订史内嵌：3.1.0（expand 收编，wire 不 bump）、3.3.0（digest changed / text 全量内联 B2 / 第 9 读组 + 写 13-17 / allowlist §5.7a）。
- 2026-08-19 修订：4.0.0 起 `(3,4)` 双版本窗口，v3 语义冻结不变，`?v=4` 差异由 v4-contract 规定（§0.6）。
- 可测试断言：读组/写端点计数（9 读 + 17 写）、`?v=3` 视图 = 本文件全量语义。

### §1 头退役范围 [冻结]（v3-contract.md:22-32）
- 五头退役表：`X-Slimapi-Version`（出现不解读）、`X-Opencode-Directory`（消费集出现 → 400 `directory_header_retired`）、`X-Next-Cursor`/`X-Complete`（envelope 化替代）、`X-Slimapi-Subscriber-ID`（meta 帧替代）；2.0.0 并行期 vs 3.0.0 终态两列。
- 不退役清单：`X-Client-*`、`X-Request-ID`、标准缓存头（ETag/Vary/Cache-Control/Content-Encoding）、`X-Accel-Buffering`。
- 可测试断言：各头「出现不报错/出现报错」的精确行为；响应侧永不产出已退役头。

### §2 版本选择器状态机 [冻结]（v3-contract.md:34-53）
- selector 仅覆盖 `/slimapi/**`；非-slim catch-all 零消费零剥离（2.0.0 期；3.0.0 起该类别消失）。
- `v` 为 sidecar 保留参数，dispatch 层消费剥离、永不转发；词法 `^[1-9][0-9]*$`，`0/03/+3/ 3/3.0/空串` → 400 `invalid_version_selector`；多值同值宽容折叠、异值 400。
- 请求形态表：无 `v` / `v=2`（并行期 v2 管线含头门禁；3.0.0 后 → 400 `unsupported_version supported:[3]`）；`v=3` v3 语义；合法但不支持 → 400 `unsupported_version`。
- `GET /slimapi/versions` 无条件豁免；非 GET → 405 + `Allow: GET`，优先级高于一切。
- SSE 两端点同表；畸形/不支持开流前 400 普通 JSON。
- 可测试断言：§2 表逐行 + 词法边界全集（§11.1）。

### §3 发现端点 [冻结]（v3-contract.md:55-66）
- `GET /slimapi/versions` 形状：`{current, available, capabilities, sidecarVersion}`；`current ∈ available`、升序、消费方忽略未知字段、`Cache-Control: no-store`、无 ETag。
- `capabilities["3"]` 含 envelope/directoryQuery/writeRoutes/readRoutes + `expand`（categories 12 类目表序 + `fragmentMaxBytes` = `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 运行时值，[3.1.0] 形状冻结）。
- 单一事实源：`src/oc_slimapi/traffic.py::EXPAND_CATEGORIES`（versions 广告与流量记账同源）；客户端以 key 存在性探测，勿用 sidecarVersion 字符串比较。
- 3.0.0 起 `available:[3]`；（2026-08-19 注记后实际已进入 (3,4) 窗口，见 v4 §3.1。）

### §3a health 双视图 [冻结]（v3-contract.md:68-74）
- `/slimapi/health`：根级 `slimapi_contract`；`server.api_version` 与 `schema.version` 同源同值（禁止 3/2 组合）；`accepted_client_versions` 2.0.0=[2,3]、3.0.0=[3,3]。
- `/slimapi/ready` 无 contract 标识字段（部署探针）；schema 三字段同源规则同上。
- health/ready 属 `/slimapi` 表面（照旧要求 `?v=`；versions 是唯一豁免端点）。
- `features.allowlist`（[3.3.0]）：`{enabled: bool}` + hub 可达时 `droppedEvents`；只报布值不泄露清单内容；ready 不加。

### §4 envelope [冻结]（v3-contract.md:76-81）
- 仅 messages/sessions 列表 envelope 化：messages `{items, nextCursor}`（游标不回退照旧、无 complete）；sessions `{items, complete}`（继承非权威性强制语言、无 nextCursor）。
- `/slimapi/sessions/status` 不 envelope 化（map 无分页头）。
- 边界：错误响应不 envelope；304 无 body；`?v=3` 与其他 query 任意组合。

### §4a 消息投影缩减与 expandRefs [冻结]（v3-contract.md:83-98）
- 原则：整字段保留或整字段省略、不部分截断；UTF-8 字节计数；省略字段（除 /full-only 清单）必带 `expandRefs`。
- 阈值：`info.summary.diffs` 恒省略→ref；`TextPart.text` [3.2.0] 起永远全量内联（无阈值、无 `truncated`，B2 断言矩阵锁定）；`ReasoningPart.text` >2048 折叠；tool state.output/error 现状阈值（4KB/字段、16KB/消息）+ refs；/full-only 穷举清单不生成 refs。
- PatchPart（P0 修复）：`files: string[]` 原样 verbatim（上游 v1.18.16+），不省略不生成 refs。
- `expandRefs` 为 sidecar 拥有键（上游同名键剥离/确定性替换）；元素 `{category, messageID, partID?, href}`；去重/排序确定性；每 part ≤5 refs。
- 可渲染性：`text:null` + expandRefs 的 part 计为可渲染；消息级 diffs 不参与判定。
- merged 语义（best-effort）：候选 = 占位 ∪ ref 消息（diffs 永不 batch 恢复）；placeholder-first；交集去重；降级保留 skeleton 是特性而非缺陷。
- `contentFingerprint` 语义 2026-08-19 就地载明：`<vN>:<sha256hex>`、skeleton 字节派生、merged splice 后重算、降级不重算、确定性无单调性、跨表示模式不可比较、开关参与 REP_VERSION。

### §4b expand 片段端点 [冻结]（v3-contract.md:100-147）
- 两路由（消息级仅 `info_summary_diffs`；part 级其余 11 类目）；v3-only、消费 `?directory=`；category str + 手工白名单校验（防 FastAPI 422）→ 400 `invalid_expand_category`；恒 200、`no-store`、无 ETag。
- 12 类目表冻结（`traffic.py::EXPAND_CATEGORIES` 表序为单一事实源）；state 类全部 tool-only（PatchPart 无 state）；`part_snapshot` 覆盖 step-start/finish。
- 8 步冻结求值序（category→级别→transform 准入 503→single-flight GET（sid 404→`session_not_found`、5xx→503、4xx→502 `upstream_http_N`）→cap 读 413 源→decode 503→parts 定位 502/404→类型 400→提取/嵌套校验→片段 cap 413）；503/413(源)/502 可先于 404/400——冻结为契约。
- 响应 envelope `{category, messageID[, partID], data}`；缺失 vs 显式 null 均 `data.<key>=null`；读当前态（非快照）。
- 配置：`OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 默认 8 MiB、界 [1KiB, 32MiB]、非法启动 RuntimeError；聚合内存信封 `max(resp, expand) × transforms` 超限 startup 拒绝。
- 观测：ledger/snapshot/metrics `expand` 块（category × status；非法折叠 `invalid` 桶防基数 DoS）。

### §5 directory 矩阵（含 §5.7a）[冻结]（v3-contract.md:149-166）
- canonical = `?directory=`（v=3）；`v` 在 `/slimapi/**` 无条件剥离；`directory` 消费/转换限 v3（转上游 `X-Opencode-Directory`，wire 等价）。
- 消费集穷举（messages×5 含 expand、sessions 列表+status、todo/children/diff、agent/command、§10 全部收编路由按上游组声明）；catch-all 不在消费集。
- 双现规则：归一化同值正常；异值 → 400 `directory_conflict`（附双值）；非消费集宽容忽略。
- stream 例外：query-only 接受 no-op；query+头异值 → 400 `directory_not_allowed`（token_stream.py 实际语义，rev6 表述有误以此为准）；v3 前置多值异值 → 400 `invalid_directory_selector`。
- §5.7a allowlist fail-closed（[3.3.0]，部署配置面）：env 三态（未配置=零变化 / 显式空=`/file/**` 三端点全 403 / 非空=子树放行+SSE 帧过滤）；canonical 匹配 = normalize+realpath（防 `..`/symlink 绕过）；候选实时解析不缓存、根按值缓存且重应用配置失效；相对 directory 不授权；403 统一错误体不泄露存在性；授权后转发 canonical directory（TOCTOU 缓解）；`droppedEvents` 计数经 health 暴露。

### §6 ETag / Vary / 304 [冻结]（v3-contract.md:168-173）
- validator 域隔离：`representation_version` 含 wire 版本标记（v2/v3 互不匹配）。
- Vary 现状（2026-08-19 修订注记）：**全部 `/slimapi` ETag 路由恒单值 `Vary: Accept-Encoding`**（directory 维度随头退役移除）；历史双值形态仅 2.0.0 并行期。
- ETag 全集 = §10.a 全部 GET；§10.b 写路由不启用；上游 ETag 不透传；expand 两路由不在 ETag 全集（恒 200）。
- v3 304 头集合：仅 `ETag` + `Vary` + `Cache-Control: no-store`；不复制 X-Next-Cursor/X-Complete。

### §7 SSE [冻结]（v3-contract.md:175-193）
- 两端点接受 `?v=3`；帧名/帧形/Last-Event-ID/resync/heartbeat 零变化；畸形/不支持开流前 400。
- digest `changed` 字段（[3.3.0] B1a 最小语义）：恒单元素 `[本帧sid]`、零新增状态；仅 digest 帧携带。
- meta 帧：开流首帧 `slimapi.meta`（早于业务帧/heartbeat/resync 回放）；`tokens` 取值冻结（/events 按 tokens=1，/stream 恒 true）；SSE 不做 content-encoding。
- §7.5 `lastError` sticky：`session.error` 记录+立即 flush；下一次 `busy` 显式 `lastError:null` 清除；`session.deleted` 省略字段；sticky 进程内无持久化、FIFO 10,000 sid。
- §7.6 digest `status` 恒字符串（上游字符串/对象信封两形态归一化；信封无效忽略该次更新）。
- §7.7 SSE 恒 identity、无 Vary 头（响应头 = no-cache,no-transform + X-Accel-Buffering:no）。
- §7.8 水位定位与盲区：`updatedAt` 仅触发器（wall-clock、重启即丢）；盲区一 `message.removed` 不进 digest、盲区二断连无补偿；双轨消费必选（digest 精拉 + 周期 304 对账兜底）；重启/resync 全失效重拉。

### §8 错误体与 catch-all 终局 [冻结]（v3-contract.md:195-201）
- 错误体沿用 v2 全集 + 新增：`unsupported_version`/`invalid_version_selector`/`directory_conflict`/`invalid_directory_selector`/`directory_header_retired` + expand 族（§4b.3）。
- catch-all：2.0.0 盲转零消费零剥离；3.0.0 关闭，未收编路径 404 `thin_route_not_found`；收编全集闭包冻结。
- 终态错误优先级链（高→低）：①非 GET versions→405 → ②selector 400 → ③directory 400（多值→双现→头退役）→ ④404 thin_route_not_found；expand 路由内错误按 §4b.3，selector/directory 层仍先于路由内错误。

### §9 观测与移除判据 [冻结]（v3-contract.md:203-212）
- access log 加性字段：`wireVersion`/`selectorResult`（catch-all=not_applicable）/`directoryForm`/`recordType`（request|sse_open|sse_close）/`lifecycleId`。
- snapshot 聚合矩阵（≥30 天）：`date × selectorResult × wireVersion × directoryForm × recordType × statusClass × bucket`；`sseActive` 四维 `{v2,v3,absent,not_applicable}` + 跨日 carry-in 孤儿校正。
- sidecar 3.0.0 启动判据六条显式谓词（①ocdroid 已发+smoke ②REST/SSE v2 流量归零 ③directory 头归零 ④catch-all 全收敛 ⑤webui 全 v3 ⑥书面确认）。

### §10 路由收编全集 [冻结]（v3-contract.md:214-260）
- 读 9 组（既有 7 + `messages.expand` 第 8 [3.1.0] + `session.context` 第 9 [3.3.0]）+ 写 17 端点（12 + agent/model/revert 三段式 #13-17）。
- 统一行为（上游快照 v1.18.16 基准）：成功 2xx 逐字透传；4xx 逐字透传；上游 5xx/网络 → 503 `upstream_unavailable`（已知迁移点）；admission 413；纯 raw 受控代理不占 transform 池；响应头透传集冻结（Content-Type/Location/Retry-After/X-Request-ID/Last-Request-ID）；Content-Encoding 不透传（实体字节口径）。
- `messages.expand` carve-out：转换端点（自有错误码/占池/变换 body），raw 条款不适用。
- 错误 body 读取受 response cap 保护（超限降级 503）；session 单查投影仅 2xx+JSON object 执行、经转换池 offload。
- `session.context` 与 #13-17 不消费 directory（v2 session 组按 sid 自路由，宽容剥离不转发）。

### §11 测试矩阵 [冻结]（v3-contract.md:262-282）
- 18 项门控用例面：selector 全状态/词法边界、directory 组合（含 stream 守卫）、ETag 全集、envelope、错误面、SSE meta/首帧序、versions 豁免/405、观测字段（sseActive 四维跨日公式）、raw 读 7 组回归、写 12 端点回归、catch-all raw-query 保序（proxy.py:182-203 锁定）、退役形态模拟、存量回归、expand 回归矩阵（design-expand §12 全集）、B1a changed、B2 text 全量内联断言（grep 负向断言 `truncated`）、B4 新路由回归、B4-4 allowlist 三态矩阵。

### §12 里程碑 [计划]（v3-contract.md:284-288）
- M1 = sidecar 2.0.0（A/B/C/D 四批，双 9.5 门控）；M2 = ocdroid 3.0.0；M3 = sidecar 3.0.0（§9.3 判据 → 删 v2/头/catch-all）。历史计划节（均已执行完毕）。

---

## 2. v4-contract.md 逐节摘要（§0–§17+附录，共 19 单元）

### §0 版本原则与并存退役规则 [冻结]（v4-contract.md:11-17）
- 双版本期：4.0.0 起 `ACCEPTED_CLIENT_VERSIONS=(3,4)`；`?v=3` 逐字节不变；无 `v`/旧版本 → 400 `unsupported_version supported:[3,4]`。
- major 与 wire 协议版本绑定（release.md §1.1 铁律）；版本窗任何变更 = major。
- v3 退役判据（2026-08-19 owner 改写）：协议封顶 4 系、**(3,4) 永久双版本窗口**、原预定 major 退役发版已取消；未来评估纯观测性（wireVersion 占比 + SSE active），不预设版本窗。
- 消费者回退语义：503 不自动回退 v3；v3 目录级浏览仅经用户显式整体版本重协商，且是功能降级非等价回退。
- 2026-08-19 正式修订纪律：修订直接落 `?v=4` 视图（v4 无消费方）、按 feature ID 独立门控（§3.3）、`?v=3` 零改动。

### §1 头与参数总则 [冻结]（v4-contract.md:19-23）
- `X-Slimapi-Version` 头维持删除（不解读不报错）；`?v=` selector v3 词法/消费规则不变、支持集扩 `[3,4]`、多值同值折叠不变；其余保留参数按 §5。

### §2 selector 双版本状态表 [冻结]（v4-contract.md:25-39）
- 五行状态表：`v=3`/`v=4`/无 v 或合法不支持（400 supported:[3,4]）/词法非法（400）/versions 豁免+405 优先。
- request-scope `wireVersion`（"3"|"4"）写入 scope state，路由/health/versions 同源读取，禁止错配组合（S-B04）；v4 能力只能经 selector 显式进入。
- directory 消费集版本分叉：v4 仅将 `^/slimapi/sessions$`（全局列表）移出消费集，其余路由全部保留 v3 消费语义。
- 观测：`selectorResult` 增 `v4`、`wireVersion` 增 "4"。

### §3 发现端点与能力面 [冻结]（v4-contract.md:41-104）
- **§3.1 versions**：`current=4`、`available=[3,4]`；`capabilities["4"]` 静态四键（globalSessions/auxiliaryFilters/sseReplay/qpImmediateFull——存在即广告、不随 DB 抖动）；广告时序 n1（sseReplay/qpImmediateFull 与实现同批）；expand 探测注记（`capabilities["4"]` 无 expand 键，探测读 `capabilities["3"].expand`）；修订扩展键 `readiness` + `expand` 随批次加性。
- **§3.2 health 双视图**：按请求 wireVersion 返回 3/4 视图（同源同值）；v4 新增 `auxiliary: {available, mode}` + `allowlist: {enabled}`；ready 形状不变。
- **§3.3 readiness 门禁**（修订冻结）：`ReadinessGate = {ready, required, satisfied}`；feature 全集 U 修订二后 **十 ID**（selector.v4 … session.post-actions.v4 第 10）；`required ≡ U`、规范化 `f(A)=去重+UTF-8 字节序排序`、未知 ID 拒绝；`ready ⇔ f(required) ⊆ f(satisfied)` 派生值不允许独立翻转；**按 feature ID 独立门控**（ready 仅聚合指示器）；`session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied`（蕴含⑦）；expand 键双向 iff 不变量（四种组合穷尽）；discovery contradiction 七条件单一结局（客户端侧分类，非服务端错误码）；opt-in 公式三条件合取；两条例外性说明（405 fallback 语义、4.2.0 coded 405 历史基线）。

### §4 GET /slimapi/sessions（v4 全局会话目录）[冻结]（v4-contract.md:106-197）
- 数据源：DB 投影源（SQLite session LEFT JOIN project，mode=ro）常态；上游 HTTP `/experimental/session` schema 权威 + 降级。
- **§4.1 参数矩阵**：`archived` 三态/`parent` 四态/`search` 字面子串/`cursor` opaque/`limit` 1..500；v3 收 v4 参数（roots/start）与 v4 收 v3 参数（archived/parent/cursor）互 422；directory 任何形式 → 400 `directory_retired_in_v4`；`parent=only` = `parent_id IS NOT NULL`（真库实证）；排序冻结 `(time_updated DESC, id DESC)`；complete = LIMIT+1 窗口；零缓存一条 SQL。
- **§4.2 降级矩阵**：72 格（req 12 × db 3 × al 2）+ cursor 正交轴（144 case）；db avail 全 200 SQL 谓词下推；db 不可用 × allowlist 非空全 503（fail-closed，不做首 N 行后置过滤）；× 空：search 含通配 `%/_/\` → 503、cursor → 503、Class A（4 req）200+degraded、Class B（8 req）503；`degraded:true` 只表数据源降级、过滤语义永不降级；503 统一 `Retry-After: 30`、错误体不泄露 DB 细节。
- **§4.3 错误族**：`invalid_cursor`（先于 503）/`auxiliary_unavailable`/`directory_retired_in_v4`/422 参数版本不匹配。
- **§4.4 ETag**：v4 sessions 发布态无 ETag/Vary/304（修订目标 §15：增 ETag + Vary 修正，随 `representation.vary.v4` 门控）。
- **§4.5 cursor**：base64url(JSON `{t,i,f}`) 复合键 + 过滤上下文指纹；不承诺并发零重复零遗漏；指纹不匹配 → 400；keyset SQL 下推；search-hash 输入 = trim 后转义前；allowlist-rev 确定性。
- **§4.6 search/allowlist SQL 语义**：`LIKE ... ESCAPE '\'` 字面子串；allowlist 子树谓词**二进制前缀弃 LIKE**（`=` + substr 前缀，大小写敏感、`/foo` 不含 `/foobar`、根 `/` 特例）；`/slimapi/directories` 保持现形态不升 DB。

### §5 directory 消费矩阵 [冻结]（v4-contract.md:199-213）
- §5.1 v3 全部语义逐字沿用；§5.2 v4 仅全局列表整体退役（query/header/混合一律 400 `directory_retired_in_v4`，selector 层拦截先于路由、不泄露存在性），其余路由 v3 语义原样。
- allowlist 作用域全覆盖（非空时全局列表 SQL 谓词/directories 列表/digest/q/p 帧/事件流均过滤；`/file/**` fail-closed）。
- 修订二加注：三条 POST 等效动作的 directory 消费 ≡ 各自等效目标路由；门控未激活期间 selector 层 405 先行。

### §6 ETag / Vary / 304 [冻结]（v4-contract.md:215-217）
- v3 原样；v4 sessions 无 ETag（§4.4 已含差异）。

### §7 SSE id: / 重放（v4-only，B3b 已落地）（v4-contract.md:219-272）
- **§7.0 四项 owner 终裁（S-B01）**：①tokens=1 统一流 v4 禁止（400）；②meta 无 id、epoch 不随重连换、线序 meta→replay→新帧单调；③token 流 per-sid 序列、全局流单一序列；④两端点逐帧状态机（8 场景 × 2 端点、四条通用不变量）。resync reason 值域冻结四值。
- **§7.1 id 语法**：`g:<epoch>:<seq>` / `t:<sid>:<epoch>:<seq>`；epoch = 随机 boot nonce 16hex（非墙钟、不比较大小、重启必换）；域标签机械判定（跨端点/跨 sid/格式非法 → 忽略+重置）；业务帧/digest 分配 id，meta/resync/heartbeat 无 id；ID 无倒退不变式。
- **§7.2 重放语义**：有界重放日志（count/bytes/TTL 三维，环形覆盖）；Last-Event-ID 四类拆分（旧 epoch→`epoch_changed`、future→忽略重置、非法/跨域→忽略重置、窗口内 replay/`replay_expired`/`replay_gap`）；分类优先级严格短路序（语法→域→epoch→seq）；上游断连 barrier（low-watermark，写入范围 = 全局域 + 当前 epoch 全部 per-sid 域；seq ≤ 水位一律 resync；禁止跨 barrier 补帧；不受逐出）；背压溢出帧入日志（记录已发布非已送达）。
- **§7.3 tokens=1**：v4 → 400 `tokens_stream_retired_in_v4`（附 hint）；token 流端点 v4 起独立 id。
- **§7.4 q/p 载荷**：逐字段核对已完整（零裁剪）；`qpImmediateFull` 语义 = 现状已成立（B1b 零 wire 变更）；digest `changed` v4 沿用。
- **§7.5 与 v3 同步语义**：status 恒字符串/lastError sticky/SSE 恒 identity/水位双轨消费——两视图一致全文复述；v4 附加：meta 首帧字段序 `subscriberId, tokens, capabilities, epoch, seqBase`（capabilities 恒一键 sseReplay）、welcome 帧抑制（v4 不产出 server.connected）。

### §8 错误族与优先级 [冻结]（v4-contract.md:274-309）
- §8.1 新增码：`directory_retired_in_v4`/`tokens_stream_retired_in_v4`/`invalid_cursor`/`auxiliary_unavailable`/422 参数版本不匹配。
- §8.2 403（allowlist）与 400 族命名区分；403 不泄露存在性。
- §8.3 跨版本优先级真值表总链：405 versions → selector version 400 → selector directory 400 → 路由 422 → 路由 400 invalid_cursor → 路由 503 → 404/其余；四组合裁决。
- §8.4 修订新增码：`method_not_applicable`（405，修订二后适用面收窄为过渡态）、`provider_upstream_malformed`（502）、`provider_projection_limit`（413）；复用码清单；优先级插列（②与③之间插 method 405）。

### §9 观测 [冻结]（v4-contract.md:311-329）
- 维度扩展：`selectorResult`+v4、`wireVersion`+"4"、SSE active 同步；DB 辅助指标（延迟 P50/P99/降级/熔断/重探/inode swap）；replay 指标（hit/miss/gap/resync）。
- bucket：v4 sessions 归既有桶 + degraded 标记维度；`auxiliary.available=false` = runbook 信号（operations.md §7）。
- §9.4 v3 退役判据 P4：无预定退役版本，纯观测性触发条件。

### §10 路由全集逐条 [冻结]（v4-contract.md:331-362）
- 计数口径 = 路由 × 方法表行：51 条（read 26 + write 17 + SSE 2 + 发现/运维 6）；已发布 v4 差异仅 4 条（sessions/events/stream/versions）；其余 47 条零 v4 差异。
- 显式注载：session 单查 `?v=4` 不升级骨架（恒 v3 skeleton）；sessions/status 零分叉；expand 探测口径。
- 修订追加差异面表：providers 投影 / 单查 parity / expand href / versions readiness / 表示层 / 三条 POST（修订二已激活：POST≡PATCH、archive 便捷、POST delete≡DELETE；`?v=3` 恒 404）；修订二实施后 54 条（write 20）。

### §11 测试矩阵（v4-contract.md:364-379）
- 12 项用例面（11.1–11.12）：跨版本组合、selector 分叉、降级矩阵 144 case、cursor 指纹、SQL 语义 ~19 case、DB 生命周期/并发阻断、WAL 陈旧读（B0 已落地 `test_wal_staleness.py`）、等价性锚定（S-B03 禁 mock 自证）、EQP 48 组合（脚本已落地）、SSE 重放 REPLAY-001~018 + barrier、schema 变更兼容、冷启动。

### §12 Provider 安全投影 [修订冻结 + 修订三 2026-08-20 已发版 v4.4.0]（v4-contract.md:381-529）
- `?v=4` 差异面（feature `providers.redacted.v4` 已 satisfied）；`?v=3` 恒透传。
- §12.1 schema：顶层恰 `providers`+`default`；ProviderEntry/ModelEntry 白名单；嵌套未知字段递归丢弃；optional 字段策略（source/status/variants/limit 省略不报错，绝无 null）；**修订三**：ModelEntry 恢复 optional `limit`（子键恰 {context,input,output}，逐子键 int-else-omit、bool 排除、orjson 边界 `[-2^63, 2^64-1]`、零错误路径）。
- §12.2 排序/唯一性：providers/models/variants/default 全 UTF-8 字节序；canonical body 确定性。
- §12.3 default 逐 key 三重校验（provider 存在/model 经该 provider/跨层一致），任一失败 = malformed。
- §12.4 四限额 wire 常量：256 providers / 1024 models/provider / 64 variants/model / 8 MiB projected body——独立 fail-closed tripwire、最先生效者触发、无静默截断；与源上限判然两事。
- §12.5 错误契约：上游状态映射（非 200 2xx→502 malformed、3xx/4xx→502 upstream_http_N、5xx/网络/错误体超 cap→503）；十二步求值序 + offload 边界（⑥-⑪ 全 CPU 工作 in worker，网络等待不持 permit，transform_busy 仅可能发生在 ⑤）；错误表逐类目（三带 502/413/503 语义区分；422 不适用）；错误体纪律（零上游细节）。
- §12.6 ETag/缓存：canonical 字节 = orjson OPT_SORT_KEYS 输出 = wire body = 哈希输入（同一字节双重身份）；REP_VERSION 域隔离 + 修订三指纹 bump v1→v2；`Vary: Accept-Encoding` 强制（ETag 关闭仍发）；`no-store` 恒发。

### §13 Session 单查 parity [修订冻结]（v4-contract.md:531-618）
- `?v=4` 单查升级为与列表同源 canonical `SessionSkeletonV4`（feature `session.single.projection.v4`）；`?v=3` 恒 v3 skeleton。
- §13.1 形状：v3 SESSION_KEYS 投影 + project 对象 + tokens 五列平铺 + `partial`/`degraded` 标记；单查 = 裸对象（无 envelope）；无 effectiveStatus/Turn/cost 聚合等（§17）。
- §13.2 字段真值表：required/null 语义 + fallback 可表示性；§13.2a 整响应失败规则（id/directory/title/time.created/time.updated 不可得 → 503 复用 `auxiliary_unavailable`，不发明占位值）；§13.2b 可 null 字段三态（业务 null 不 partial / 来源不可得 null+partial+degraded / 正常值）；§13.2c 单 item 失败边界（不可表示 item 混入 = 整响应失败；可表示 partial item 正常入列）。
- §13.3 来源策略：list 与 single 均 dbaux-primary、同一 snapshot boundary、**同一 projector 不变量**（分裂投影 = 实现违约）；whole-response native fallback、禁止逐字段跨源拼接；fallback 是否允许由 §4.2 矩阵判定。
- §13.4 公式：`envelope.degraded == (任一 item.degraded) ∨ native fallback`；`partial ⇒ degraded` 单向；complete 仅表分页完备性；envelope degraded 修订后为 required 布尔。
- §13.5 project join 三不变量（ID 一致/worktree 非空/join 成功）；「无 project（absent）」vs「join 不可用（null+标记）」两种 wire 形态不得混同。

### §14 expand href 与能力闭环 [修订冻结]（v4-contract.md:620-627)
- 12 类目有序清单原样照抄 v3；`capabilities["4"].expand` 随 `messages.expand.v4` satisfied 广告；href canonical 形态：`v` 第一、directory 第二、无其他 key，`v` 值来自解析后 selector（v4 响应 → `?v=4`）；端点求值序/错误族/envelope/no-store 全继承 v3 §4b。

### §15 表示层：Vary 规则与 v4 ETag [修订冻结]（v4-contract.md:629-637）
- 现状注记：4.0.0 `_v4_json_response` 显式删 Vary = 已知 bug；修订目标：凡可随 Accept-Encoding 变化的 v4 表示必带 `Vary: Accept-Encoding`（SSE 显式例外）。
- v4 sessions 列表 ETag 新增（canonical 口径同 §12.6；identity 强/gzip 弱 W/）；304 头集 = ETag+Vary+no-store；`merged_vary` helper 恰只需单值形态；域隔离（缓存键/singleflight 键/REP_VERSION 含 wire-view）。

### §16 POST 等效动作族 + method 边界 [修订冻结；修订二已发版 v4.3.0]（v4-contract.md:639-689）
- 三视图操作表（V3 / V4 过渡态 / V4 激活态）；PATCH/DELETE 两视图继续可用不退役。
- §16.1 过渡态 405（v4.2.0 现行为，冻结值不回收）：精确响应（Allow 字面/allow 数组/coded body/no-store/不转发）；优先级插列；适用范围精确限定（仅三条组合 × selector 已选 v4 × 两 ID 合取）。
- §16.2 POST 等效动作族（`session.post-actions.v4` 已 satisfied 激活）：a) POST≡PATCH 逐字节等效；b) POST delete≡DELETE（实体处理完全相同、非幂等可接受 owner q1、上游递归删子+吞错如实继承）；c) archive 便捷（octet 层缺省判据、合成体 `{"time":{"archived":<ms>}}` 紧凑形、错误映射零偏差）；directory 消费 ≡ 等效目标；无新 SSE/缓存语义。
- §16.3 组合优先级与蕴含依赖：四位组合表穷尽（∉/∉ 框架 404、∈/∉ coded 405、∈/∈ 等效路由、∉/∈ contradiction）；声明式组合优先级；Allow 字面为过渡窗口行为；门控翻转前后 v3 与主路径逐字节不变。

### §17 修订 non-goals [修订冻结；修订二收紧]（v4-contract.md:691-700）
- 能力边界声明（非待点亮 feature）：无 project status/effectiveStatus/subagentList 聚合、无独立 Turn 资源、无 exact merged、无 512B preview/generic fragment、**cascade 编排层永久 non-goal**（owner q1）、**cross-session search 永久 non-goal**（owner q3）；原「POST-only update」deferred 候选已被修订二激活移除。

### 附：与设计文档的对应（v4-contract.md:704-713）
- §2/§8.3 ↔ design-v4-selector.md；§4 全量 ↔ design-v4-dbaux.md；§7 ↔ design-v4-sse-replay.md + design-v4-qp-payload.md；§3 能力键时序 ↔ refactor-plan §4.1（n1）。B0-1 产出、随 4.0.0 发版定稿。

---

## 3. 精读文档要点与漂移点清单

### 3.1 AGENTS.md（仓库根）
要点：
- 入口索引定位：项目拓扑（ocdroid ↔ sidecar :4097 ↔ opencode :4096，stunnel mTLS 14097/14096）、并列仓库布局（ocdroid / opencode-src/current → v1.18.18）、上游对照常用路径表、流程入口（check.sh / release.sh / CHANGELOG / 契约修订）。
- 硬规则：check.sh 必做、契约权威（v3-contract，冲突以契约为准）、SQLite 写域禁令（零 DDL/DML/PRAGMA 写；索引属运维显式动作）、版本双轨、main 分支、禁止事项（手写 tag / 无 CHANGELOG 发 wire 变更 / secret 入仓）、写域纪律。
- 流程表声明 check.sh = pytest + 路由↔文档一致性 + release.md §质量门禁。

漂移点（记录不裁决）：
- **D1**：AGENTS.md 硬规则「版本双轨」写 `ACCEPTED_CLIENT_VERSIONS，当前 [3,3]` —— 与 `versioning.py:44` 的 `(3, 4)`、`SERVER_API_VERSION=4`（versioning.py:38）冲突（未随 4.0.0 更新）。
- **D2**：AGENTS.md 流程表/常用命令注「check.sh（当前 = pytest tests/）」—— 实际 `scripts/check.sh` 为三项：pytest + check_routes_doc.py + `compileall src`（develop.md:70-79 已正确记三项）。
- **D3**：AGENTS.md 项目定位句「当前**不读** opencode SQLite（v4 起经只读投影源 mode=ro 读…）」—— 自相矛盾式表述；4.0.0 已发版且 operations.md §7 声明「功能随 4.0.0 生效（生产已部署）」，dbaux 读已为现状，该句口径滞后。

### 3.2 docs/release.md
要点：
- 发版唯一入口 `release.sh patch|minor|major`；§1.1 版本语义表（**major 与 wire 协议版本绑定铁律**：仅 ACCEPTED_CLIENT_VERSIONS bump 才 major）；§1.2 wire 版本双轨（`?v=` selector + versions 发现）；§1.3 双版本期说明（(3,4)→(4,4) 收窄 = 下一次 major 5.0.0）。
- §2 发版前清单含 allowlist 部署状态确认（B4-4b 联合门槛：显式空/非空前置 ocdroid 回执）；§2.1 P3 major 前置（消费者兼容版先发）。
- §3.2 release.sh 实现约定（分支/干净/门禁/CHANGELOG 目标节必须存在/commit+annotated tag/不自动 push）；§3.3 发版后三步（pull + **editable reinstall**（否则 health.version 滞后）+ restart）。
- §6 禁止事项七条；§7 文件职责表。

漂移点：
- **D22**：release.md §1.2/§7 文件职责表把 wire 契约权威仅指向 `v3-contract.md`（「接受区间见 versioning.py 与 v3-contract §1/§2」），未列 `v4-contract.md` —— 与 AGENTS.md 相关文档索引（v3+v4 并列）不同步；§1.3 已有双版本期文字但职责表未跟上。轻度滞后。

### 3.3 docs/operations.md（重点：部署姿态 / §5.5 去重）
要点：
- §1 部署拓扑：双入口（127.0.0.1:4097 推荐 loopback / 0.0.0.0:4097 明文直连=Tailscale ACL 依赖）；upstream 固定 loopback（SSRF guard）；**单进程单 worker**（多 worker 重复建 SSE 禁止）；§1.1 依赖（Python ≥3.11 实测 3.14.4）。
- §2 安装 + 升级必 reinstall（dist-info 机制，v0.4.0 踩坑记录）；§3.2 service 单元（**operations.md:122 明确 deploy/oc-slimapi.service 为权威模板，本节示例冲突以 deploy 为准**）；§3.3 deployment revision 注入（env / LoadCredential）；§3.4 enable+Linger。
- §4 shutdown 语义：uvicorn `timeout_graceful_shutdown` 5s 宽限 + systemd `TimeoutStopSec=15` 上限（覆盖默认 90s SIGKILL）；须 ≥5s。
- §5 日志策略：journald（应用）+ 落盘（access log 按天 + snapshot 周期 300s）；§5.2.1 incarnation 状态文件分离（T9/P1-4，单调迁移不 reset）；§5.3 维护（启动压缩/legacy 迁移不跨目录/`access-legacy-*` 不受 retain/maintenance loop 1h）；§5.4 磁盘估算（access 50–100MB/天 raw、snapshot ~300KB/天）。
- **§5.5 Fan-out 与内存预算（内部 knob）**：questions 三 knob（2MiB per-dir / 16MiB aggregate / 并发 8）+ permissions 同款 + merged 三 knob（16 fulls/页、8MiB、并发 8，**不占 transform 池**）+ transform absorb 2.5s + **catalog TTL 缓存 4 knob + 去重（coalescing）4 knob + 指纹开关**；默认容量退化说明（RAW_FETCH 64MiB × RESPONSE 64MiB ⇒ 默认仅 1 并发 flight，刻意保守）；聚合内存校验 `RAW_FETCH + TRANSFORMS×max(...) ≤ 576MiB`。
- §6 健康自检（health/ready curl）；§7 v4 DB 辅助源运维（路径解析 / **索引运维 = 显式运维动作含 PRAGMA index_xinfo 校验** / 熔断 P99>20ms / runbook 升级 opencode 后第一步看 `auxiliary.available`）；§8 ocdroid 接入表；§9 排障速查；§11 G-ACL 部署姿态（0.0.0.0+14097 mTLS 边界验证探针）；§12 actions 管理（风险声明/manifest/运维注意）。

漂移点：
- **D4（P1 级矛盾证据）**：operations.md:92-94 注释声称「OC_SLIMAPI_SERVER_API_VERSION / OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS 已弃用……**生产 unit 已同款清理，模板不再示例**」—— 但权威模板 `deploy/oc-slimapi.service:32-33` **仍含这两行**（`SERVER_API_VERSION=2` / `ACCEPTED_CLIENT_VERSIONS=2,2`）。「模板不再示例」为不实陈述（详见 §4 对账）。
- **D5**：operations.md §6.2 health 期望响应示例（:353-361）写 `server.api_version: 3`、`accepted_client_versions: [3,3]`、`sidecar.version 1.1.1`、features 块无 `allowlist` 键 —— 滞后于 (3,4) 双版本（v4 视图 `auxiliary` 字段）与 [3.3.0] `features.allowlist`；§8 表格「supported:[3]」口径同款滞后（应为 [3,4]）。
- **D6**：operations.md §3.2 内嵌 unit 示例缺 `TimeoutStopSec=15` 与 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30` 两行（deploy 模板均有；§4/§5.3/§5.4 文字却引用了这两个值）—— 示例/文字/模板三处不同步（有「以 deploy 为准」兜底句，低危）。
- **D7**：operations.md §9 排障「无 ?v=3 → unsupported_version」未提 `v=4` 现为合法（[3] vs [3,4] 口径滞后，与 D5 同族）。

### 3.4 docs/develop.md
要点：
- 安装（venv/PEP 668）；配置速查表（14 项高频 knob；声明完整权威清单 = config.py Settings dataclass 35+ 项）；运行（单 worker 约束）；生产速查；`?v=3` 终态说明 + versions 唯一豁免 + catch-all 404；测试/质量门禁三项（pytest + 路由 gate + compileall；本机验证不用 CI——用户决策 2026-08-09）；gzip 检查 curl。

漂移点：
- **D8**：develop.md:23-24 配置表列 `OC_SLIMAPI_SERVER_API_VERSION`（默认 `3`）/`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS`（默认 `3,3`）为常规 knob —— 双重滞后：实际 `versioning.py` 钉 `SERVER_API_VERSION=4` / `(3,4)`，且两 env 均已弃用（前者设置仅 warning+忽略 config.py:796-804；后者设非 (3,4) 值 **启动 RuntimeError** config.py:817-822）；表内未标弃用状态（operations.md:92-94 已标）。

### 3.5 docs/specs/CLIENT_CHANGES.md
要点：
- ocdroid 侧改动清单（仅文档）：3.2.0 text 全量内联（零必改）；3.1.0 expand（投影变更影响/端点用法/部署顺序 2×2 矩阵/回退规则——仅 `thin_route_not_found` 回退 /full）；模型 hasFull/omitted；消息加载（thin_placeholder message-level 替换、/full 剥 diagnostics）；sessions 完整性头；questions/permissions 聚合 envelope（authoritativeDirectories 三项判据含 truncated）；catalog skeleton；directories 项目切换器（含 MRU 降级方案 SSOT）；actions（markdown sandbox 渲染等）；ETag 接入建议（应用层自存验证器、coding 固定）；contentFingerprint 消费（同模式内比较、无单调性）；**2026-08-19 双轨 catch-up（digest 精拉 + 周期 304 对账必选）**；T17 todo/children；路由失败策略（circuit breaker 3 次/5 分钟）；客户端标识头；错误体形状；SSE 帧类型；token stream 生命周期/streamOwned 算法/resync 分档表；四能力接入（tokenCoalesce/permissionEvents/serverMerge/transformAbsorb）；直连退役（C1/C3 前置）。

漂移点：
- **D9**：文件头（:1-5）「Aligned with v2-contract.md (lite-v2)」+ 多处「wire 版本未 bump（仍 2）」+ token stream 节「wire 以 v2-contract §3.x/§6.x 为准」（:356）—— v2 契约已于 3.0.0 退役（AGENTS.md 口径：以本仓 v3/v4 契约 + CHANGELOG 为准），权威指针整体滞后。
- **D10**：多处「须带 `X-Slimapi-Version: 2`」（:112、:344、:361、:486 C3 前置）—— 头已于 3.0.0 删除（v3 §1），现行唯一通道 `?v=3`。
- **D11**：「消息加载与展开」节「翻页用 X-Next-Cursor」（:57）与「sessions 列表完整性头 X-Complete」（:75-81）整节 —— 两头已于 3.0.0 移除，v3 envelope（body 内 nextCursor/complete）替代；文档未标注废止。
- **D12**：token stream 节「本 token stream 是 SSE 但允许 gzip（首个 SSE gzip 例外）」三层语义（:366）—— v3 终态 SSE 恒 identity、不 gzip（v3 §7.3/§7.7；INTERFACE_MAP §3.1 已注明 lever2 压缩路径随 v3 终态移除）。
- **D13**：token stream「truncated 帧（…携带自身的 partEventRevision）」（:380）与「partEventRevision 必须 strict > 去重（契约 §3.x.2）」（:413-414，锚 v2 契约）—— events tokens=1 帧明确无 partEventRevision（:443 自述）；per-session 帧语义与已退役 v2 契约挂钩，需以现行实现复核（候选滞后，未裁决）。

### 3.6 docs/specs/INTERFACE_MAP.md
要点：
- 端点级实现追踪（§0 全局约束：host/upstream/注册顺序/selector 终态/json_response/directory 结构守卫/allowlist 三态/422 惯例/客户端标识头/access log 发现规则；§1 REST 读接口逐行；§3 curated SSE + §3.1 token stream；§4 health/versions/ready/metrics/catch-all/WS；§5 skeleton 精确规则；§7 directory 转发与 shell deny-list）。
- check_routes_doc.py 的对账对象（路由↔INTERFACE_MAP 一致性 gate 的文档侧）。

漂移点：
- **D14**：头部横幅（:7）+ §0 selector 段（:14）+ §4 versions/health/ready 行（:95-97）整体为「M3 v3-only 终态（2026-08-16）/ supported:[3] / current=3 available=[3] / accepted [3,3]」—— 与 4.0.0 双版本窗口 (3,4) 冲突（v4 差异已在部分行内补注记，但 versions 行「current=3、available=[3]」、health 行「恒 3/[3,3]」未更新；v4-contract §3.1/§3.2 为 current=4/available=[3,4]/双视图）。
- **D15**：§4 metrics 行（:98）「须带 `X-Slimapi-Version:int`…缺/坏/越界版本头→门闩 400」—— 头已删除（3.0.0），metrics 现与其他 /slimapi 路由同走 `?v=` selector。
- **D16**：多行仍写双值 Vary「`Accept-Encoding, X-Opencode-Directory`」（§1 sessions/messages/agent/command/todo/children/diff 行的 ETag 注记、§1 file/vcs/find/providers/session 单查行的返回列、写路由行）—— 与 v3 §6.2（2026-08-19 修订注记：全部 /slimapi ETag 路由恒单值 `Accept-Encoding`）及其自身头部横幅「Vary 全路由收缩为单值」冲突（**文内自相矛盾**；测试 `test_vary_directory_unconditional.py` 存在暗示实现已单值）。
- **D17**：§4 catch-all 行收编面描述「读 7 组 + 写 12 端点」（:99）—— 计数滞后于 v3 §10 现行 9 读组 + 17 写端点（+ v4 三条 POST 共 54 条，v4 §10/CHANGELOG 4.3.0 口径）。历史口径（3.0.0 时点正确）未加注。

### 3.7 docs/manual/traffic-accounting.md
要点：
- 术语澄清（`metrics.traffic` 非独立路由）；§1 四腿字节 + `downOutOverUpIn` 判据；§2 快速查询（mTLS/明文入口）；§3 桶说明（含 §3.3 permissions 归 `other` 桶口径、events `tokens=1` 增量流量记账）；§4 SSE fanout 例外（多订阅比值可 >1.0）；§5 access log 离线分析（字段表含 v3/v4 加性字段 `wireVersion`/`selectorResult`/`directoryForm`/`recordType`/`lifecycleId`/`sessionsSource`/`degraded503`；SSE 生命周期行；按天切分/压缩/legacy 迁移；jq 例程）；§6 env 配置表；§7 读数限制（downIn GET=0、SSE LF 估算、coalescing upIn 不入桶等）；§9 snapshot（cumulative/runId 分段/inactive fail-loud/§9.4 v3 观测节 matrix + sseActive 四维 + 跨日 carry-in 纯函数）。

漂移点：
- **D18**：§2（:34）与头部横幅「v3-only 终态：缺 v / v=2 / 不支持值 → 400 `unsupported_version supported:[3]`」—— 与 (3,4) 窗口冲突（`v=4` 合法、supported 应为 [3,4]）；且与自身 §5.1 字段表（:169-170 已载 wireVersion "4"/selectorResult v4）**文内不一致**。

---

## 4. deploy/oc-slimapi.service 全文精读与三方对账

### 4.1 文件定性（deploy/oc-slimapi.service，73 行）
- systemd **user** service 模板（:1-8 头注释：不含 ProtectSystem 等 sandbox——user manager 无权设置，会 status=218 失败；隔离靠 stunnel mTLS + Tailscale ACL）。
- 结构：`[Unit]` After=network.target；`[Service]` Type=simple、WorkingDirectory/ExecStart 指向仓库 .venv、Restart=on-failure + 5s、`TimeoutStopSec=15`（:24）、12 条 `Environment=`、StateDirectory=oc-slimapi、journald 输出、`MemoryMax=384M`（:70）；`[Install]` WantedBy=default.target。
- `operations.md:122` 声明本文件为**权威模板**（operations §3.2 示例冲突以 deploy 为准）。

### 4.2 12 条 Environment= 逐行 + 三方对账表（deploy ↔ operations.md ↔ config.py/versioning.py）

| # | deploy 行 | 值 | config.py / versioning.py 事实 | operations.md 状态 | 对账结论 |
|---|---|---|---|---|---|
| 1 | :28 `OC_SLIMAPI_HOST` | `0.0.0.0` | 默认 `127.0.0.1`（config.py:356）；validate 允许 {127.0.0.1,::1,localhost,0.0.0.0}（:773） | §1/§3.2/§11 一致（Tailscale 直连姿态，明文需边界防护） | **有意覆盖**，一致 |
| 2 | :29 `OC_SLIMAPI_PORT` | `4097` | 默认 4097（config.py:357） | 一致 | 一致 |
| 3 | :30 `OC_SLIMAPI_UPSTREAM` | `http://127.0.0.1:4096` | 默认同值（config.py:358）；强制 loopback HTTP（:779-782） | 一致 | 一致 |
| 4 | :31 `OC_SLIMAPI_MAX_MESSAGE_BYTES` | `33554432`（32 MiB） | 默认 32 MiB（config.py:359）；上限 256 MiB | 一致 | 一致 |
| 5 | :32 `OC_SLIMAPI_SERVER_API_VERSION` | `2` | **已弃用**：`server_api_version` 钉死常量 `SERVER_API_VERSION=4`（config.py:436、versioning.py:38）；env 存在仅产生启动 warning 并被忽略（config.py:796-804） | **operations.md:92-94 声称「生产 unit 已同款清理，模板不再示例」—— deploy 模板 :32 仍在** | **残留（P1 矛盾证据 A）**；行为影响 = 每次启动一条 deprecation warning |
| 6 | :33 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `2,2` | **钉死冲突**：`ACCEPTED_CLIENT_VERSIONS=(3,4)`（versioning.py:44）；env 解析后必须等于钉死值否则 `RuntimeError`（config.py:817-822）；validate() 在 lifespan 启动调用（app.py:196、app.py:771） | 同上（:92-94 声称已清理）；且 operations.md 措辞「设置无效」**低估严重性**——设 `2,2` 不是「无效」而是 **startup-fatal**（服务 crash-loop，Restart=on-failure 反复拉起） | **残留（P1 矛盾证据 B，比 :32 更严重）**：按模板原样部署的 unit 无法通过 config.validate() |
| 7 | :34 `PYTHONUNBUFFERED=1` | — | 非 OC_SLIMAPI_ 前缀，config.py 不读（Python 标准 env，journald 实时输出） | §3.2 示例同款（:95） | 一致 |
| 8 | :40 `OC_SLIMAPI_ACCESS_LOG_DIR` | `%S/oc-slimapi/logs` | 代码默认相对 `logs`（config.py:533）；生产覆盖到 StateDirectory | 一致（:103） | 有意覆盖，一致 |
| 9 | :41 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `%S/oc-slimapi/logs/traffic-snapshot.jsonl` | 代码默认 `logs/traffic-snapshot.jsonl`（config.py:552-554） | 一致（:104） | 有意覆盖，一致 |
| 10 | :45 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` | `30` | 代码默认 `0`=不删（config.py:561-563）；deploy 注释（:42-44）Task 10/P2-1 | **operations.md §3.2 内嵌示例缺此行**；§5.3/§5.4 文字有「生产 unit 配置 30」 | 覆盖一致，但 ops 示例缺行（见 D6） |
| 11 | :46 `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `3` | 代码默认 `0`=不删（config.py:537） | 一致（:105；§5.3/§5.4 同） | 有意覆盖，一致 |
| 12 | :54 `OC_SLIMAPI_STATE_DIR` | `%S/oc-slimapi` | 代码默认 `state`（config.py:648）；T9/P1-4 incarnation 分离 | 一致（:106；§5.2.1 详述迁移语义） | 有意覆盖，一致 |

另：`:60` 一条**注释态** `#Environment=OC_SLIMAPI_ACTIONS_FILE=%h/.config/oc-slimapi/actions.toml`（opt-in，默认禁用）—— 与 operations.md §12.2/§12.3 部署步骤一致（当前生产实例另配 4-action manifest，见 operations.md:589-624）。

**deploy 对账差异合计 5 条**（#5 弃用残留+矛盾声明、#6 钉死冲突 startup-fatal+矛盾声明、#10 ops 示例缺行、#1 有意覆盖默认值〔记录性〕、以及 §4.3 所述 TimeoutStopSec 差异〔ops §3.2 示例缺行〕）。

### 4.3 TimeoutStopSec 与关停超时链
- deploy:21-24 注释 + `TimeoutStopSec=15`：systemd SIGTERM 后 15s 上限；**高于** uvicorn `timeout_graceful_shutdown=5.0`（`app.py:97` `_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0`，`app.py:780` 传入），给活跃 SSE 连接 drain 机会；**低于** systemd 默认 90s SIGKILL。
- operations.md §4（:164）与该链一致（5s 宽限 / 15s 上限 / 须 ≥5s 调整指引）；**但 operations.md §3.2 内嵌 unit 示例（:75-120）无 `TimeoutStopSec` 行** —— 若有人照 §3.2 示例而非 deploy 模板部署，将回落 systemd 默认 90s（行为退化但非故障；D6 已记）。

### 4.4 P1 矛盾证据汇总（operations.md ↔ deploy 模板）
- **证据**：`operations.md:92-94`（§3.2 示例内注释）原文：「OC_SLIMAPI_SERVER_API_VERSION / OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS 已弃用（4.0.0 起接受区间钉死 (3,4) fail-closed，设置无效；SERVER_API_VERSION 设置仅产生启动 warning 并被忽略）——**生产 unit 已同款清理，模板不再示例**」。
- **事实**：权威模板 `deploy/oc-slimapi.service:32-33` 仍逐行示例两 env（值 `2` / `2,2`）；且 operations.md:122 自己声明「仓库内 deploy/oc-slimapi.service 是同结构模板（含注释），以它为权威模板（本节示例若与之冲突以 deploy 为准）」。
- **后果分级**（记录不裁决）：:32 仅产生启动 warning（cosmetic 残留）；:33 与钉死 (3,4) 冲突 —— 若该模板被原样复制部署，`Settings.validate()` 抛 RuntimeError，服务无法启动（crash-loop）。即 operations.md 的「已清理」声明在模板侧不成立，「设置无效」的措辞对 :33 而言不成立（应为「启动拒绝」）。

---

## 5. docs/specs/ 其余文档扫读：与现行 v3/v4 契约冲突/滞后清单

| # | 文档 | 状态声明 | 冲突/滞后条目 |
|---|---|---|---|
| S1 | `design-v2.md`（:1-13） | 「aligned with v2-contract.md (lite-v2)」；AGENTS.md 仍将其列为「当前态设计」入口 | ① 头部硬约束「版本走必填请求头 `X-Slimapi-Version`」—— 头已于 3.0.0 删除；② 「禁 SQLite」—— v4 起 dbaux 只读 SQLite 生产启用（AGENTS.md 硬规则已改口「绝无写入」而非「禁读」）；③ 上游对齐与行号引用基于 v2 时代。作为「当前态设计」入口会误导（历史 rationale 有效） |
| S2 | `design-token-stream.md`（:1-8） | 已自我定位「历史设计稿（v4）」 | 头部「当前 wire 契约以 **v2-contract §3.x** 为准」—— v2 契约已退役，现行 wire = v3-contract §7（AGENTS.md 索引行已代为更正，文档内指针未改） |
| S3 | `v2-contract.md`（:1-30） | 文件头部仍自称「**This is the AUTHORITATIVE wire contract**」 | 无退役横幅/状态声明（grep 无「已退役」标记）；与 v3 §0.1「v3 视图 wire 权威 = 本文件」及 AGENTS.md「≤2.x 历史契约」定性冲突 —— 读者从文件本身无法得知已退役（operations.md §5.2.1 等已标注「历史」，但文件自身未标） |
| S4 | `design-expand.md`（:1-6） | v5 定稿、R4 评审通过 | 上游对齐基准 v1.18.16（:5）—— AGENTS.md 声明 current 已 repoint v1.18.18；属快照对齐滞后（契约 v3 §4b 亦注明 v1.18.16 基准并要求 repoint 时复核） |
| S5 | `design-message-watermark.md`（:1-5） | 已冻结；自定位「字段级 spec 权威，v2-contract §消息列表加性节为 wire 摘要」 | wire 摘要指针指向已退役 v2 契约 —— 现行 wire 载体为 v3 §4a.6（2026-08-19 就地载明）；文档指针滞后 |
| S6 | `design-v4-selector.md` | B0-2 设计冻结，落地 B3a-A | 无冲突（与 v4 §2/§8.3 配套声明一致） |
| S7 | `design-v4-dbaux.md` | B0-5 工程化设计冻结，v4 §4 为 wire 权威 | 无冲突（自声明契约优先级正确；上游对齐 v1.18.18 与 AGENTS.md 一致） |
| S8 | `design-v4-sse-replay.md` | B0-3 设计稿 v4 + owner 终裁记录 | 无冲突（rev-6 收紧注记与 v4 §7.2 一致；「现行 wire 契约以 v3-contract §7 为准」表述正确） |
| S9 | `design-v4-qp-payload.md` | B0-4 核对报告（结论：已完整） | 无冲突 |
| S10 | `access-log-writer-design.md`（:1-12） | **DESIGN ONLY**，自带声明「不代表当前 access_log.py 运行行为」 | 无冲突（自声明充分） |
| S11 | `chat-toolcard-slimapi-plan.md`（:1-25） | 2026-08-09 阶段 A 方案（未实施声明） | 历史评审/方案文档：F1 指控 skeleton `_patch()` diffStats 位置 bug —— 是否已修复需实现面核（超出本单元范围，移交实现面单元）；引 skeleton.py 行号为 2026-08-09 快照 |
| S12 | `traffic-route-todo-2026-08-10.md` / `traffic-route-children-2026-08-10.md` | 头部「**PROPOSAL — NOT IMPLEMENTED**. No code, no contract, no INTERFACE_MAP」 | 状态声明滞后：todo/children 路由**已实现并收编**（INTERFACE_MAP §1 有行、v3 §10 消费集、CHANGELOG 2026-08-16 T17/T18）—— 设计稿落地后未回写状态横幅 |

---

## 6. 四方索引表（契约节 ↔ 实现模块 ↔ 代表测试 ↔ 设计文档）

> 实现模块均为 `src/oc_slimapi/` 下路径；测试均为 `tests/` 下文件。「设计文档」= 该契约节是否有具名设计权威（无 = 契约即唯一权威/设计散见于计划文档）。

### 6.1 v3-contract

| 契约节 | 实现模块 | 代表测试 | 设计文档 |
|---|---|---|---|
| §0/§1 继承与头退役 | `versioning.py`、`selector.py`、`middleware/` | `test_selector.py`、`test_v3_rawbody_regression.py` | design-v2（历史）；design-v4-selector（§1 交叉） |
| §2 selector 状态机 | `selector.py` | `test_selector.py`、`test_selector_query_strip.py` | design-v4-selector.md |
| §3 versions 发现 | `routes/versions.py`、`discovery.py` | `test_versions_route.py` | 无（refactor-plan §4.1 时序） |
| §3a health 双视图 | `routes/health.py` | `test_health.py`、`test_health_dual_view.py`、`test_health_features.py` | 无 |
| §4 envelope | `envelope.py`、`routes/messages.py`、`routes/sessions.py` | `test_v3_envelope.py` | 无 |
| §4a 投影缩减/expandRefs/指纹 | `skeleton.py`、`etag.py` | `test_skeleton.py`、`test_skeleton_expand.py`、`test_b2_merged_text_compat.py`、`test_messages_merged.py`、`test_message_fingerprint.py` | design-expand.md §4/§5；design-message-watermark.md（指纹） |
| §4b expand 端点 | `routes/messages.py`（expand 路由）、`traffic.py`（EXPAND_CATEGORIES） | `test_expand_routes.py`、`test_expand_config.py` | design-expand.md §2/§3/§6/§12 |
| §5 directory 矩阵 + §5.7a allowlist | `directory.py`、`routes/_read_passthrough.py`、`sse/global_hub.py`、`config.py`（allowlist 族） | `test_v3_directory.py`、`test_directory.py`、`test_b4_allowlist.py` | 无（B4-4 语义全量内嵌契约 §5.7a） |
| §6 ETag/Vary/304 | `etag.py` | `test_etag.py`、`test_v3_etag_domain.py`、`test_vary_directory_unconditional.py`、`test_gzip_negotiation.py` | 无（v2 §6.2 机制继承注记） |
| §7 SSE（digest/lastError/水位/表示层） | `sse/global_hub.py`、`sse/hub.py`、`sse/hub_types.py`、`routes/events.py` | `test_hub.py`、`test_hub_behavior_lock.py`、`test_b1a_digest_changed.py`、`test_session_status_object_format.py`、`test_v3_sse_meta.py`、`test_token_hub*.py` | design-token-stream.md（历史 rationale；现行 = 契约 §7） |
| §8 错误与 catch-all 终局 | `errors.py`、`proxy.py`、`selector.py` | `test_errors.py`、`test_proxy.py`、`test_terminal_matrix.py` | 无 |
| §9 观测 | `access_log.py`、`traffic_snapshot.py`、`sse_observability.py`、`middleware/traffic_accounting.py` | `test_access_log_v3_fields.py`、`test_traffic_snapshot_v3.py`、`test_proxy_sse_observability.py` | access-log-writer-design.md（DESIGN ONLY 未落地） |
| §10 路由收编（读 9 组 + 写 17） | `routes/read_groups.py`、`routes/write_groups.py`、`routes/_read_passthrough.py` | `test_read_groups.py`、`test_write_groups.py`、`test_b4_new_routes.py`、`test_check_routes_doc.py`（gate） | 无（INTERFACE_MAP 为追踪面） |
| §11 测试矩阵 | （tests/ 整体） | — | — |
| §12 里程碑 | （流程历史，已完成） | — | — |

### 6.2 v4-contract

| 契约节 | 实现模块 | 代表测试 | 设计文档 |
|---|---|---|---|
| §0/§1/§2 双版本原则与 selector | `selector.py`、`versioning.py` | `test_v4_dual_window.py`、`test_selector.py` | design-v4-selector.md |
| §3.1 versions 能力面 | `routes/versions.py`、`features.py` | `test_versions_route.py` | refactor-plan §4.1（n1 时序） |
| §3.2 health 双视图（auxiliary） | `routes/health.py`、`dbaux/lifecycle.py` | `test_health_dual_view.py`、`test_degraded_observability.py` | 无 |
| §3.3 readiness 门禁 | `readiness.py` | `test_versions_readiness.py`、`test_readiness_gating_integration.py` | 无（2026-08-19 rebaseline 计划 §4-§7，docs/ocmar/plans/） |
| §4 sessions 全局（4.1–4.6） | `dbaux/projection.py`、`dbaux/cursor.py`、`dbaux/lifecycle.py`、`dbaux/path_resolution.py`、`routes/sessions.py` | `test_sessions_v4_matrix.py`、`test_cursor_matrix.py`、`test_sql_semantics.py`、`test_dbaux_lifecycle.py`、`test_wal_staleness.py`、`test_equivalence_anchor.py`、`test_eqp_matrix.py`、`test_db_path_resolution.py` | design-v4-dbaux.md |
| §5 directory 消费矩阵 | `selector.py`、`directory.py` | `test_v4_dual_window.py`、`test_b4_allowlist.py` | design-v4-selector.md §2.3/§2.4 |
| §6 ETag（v4 sessions 例外） | `etag.py` | `test_etag.py` | 无 |
| §7 SSE id:/重放 | `sse/replay_log.py`、`sse/replay_wire.py`、`routes/events.py`、`routes/token_stream.py`、`sse/global_hub.py` | `test_sse_replay_wire.py`、`test_replay_log.py`、`test_events_tokens.py`、`test_metrics_replay_block.py`、`test_token_subscriber_overflow.py` | design-v4-sse-replay.md、design-v4-qp-payload.md |
| §8 错误族与优先级 | `errors.py`、`selector.py` | `test_terminal_matrix.py`、`test_errors.py` | design-v4-selector.md §3 |
| §9 观测 | `traffic.py`、`sse_observability.py`、`dbaux/lifecycle.py` | `test_v4_observability.py`、`test_degraded_observability.py`、`test_dbaux_metrics.py`、`test_traffic_latency.py` | 无 |
| §10 路由全集（51→54 条） | 各 `routes/*` | `test_check_routes_doc.py`（计数同口径 gate） | INTERFACE_MAP（追踪面） |
| §11 测试矩阵 | （tests/ 整体） | — | design-v4-dbaux §10（等价性）/design-v4-sse-replay §4（REPLAY 表） |
| §12 providers 投影 | `providers_projection.py`、`routes/read_groups.py`、`etag.py` | `test_providers_projection_v4.py` | 无（rebaseline 计划 §4） |
| §13 session 单查 parity | `dbaux/projection.py`、`routes/read_groups.py` | `test_session_single_v4.py` | 无（rebaseline 计划 §5） |
| §14 expand href v4 | `skeleton.py` | `test_expand_href_v4.py` | 无（rebaseline 计划 §6） |
| §15 表示层 Vary/v4 ETag | `etag.py`、`routes/sessions.py` | `test_sessions_v4_representation.py` | 无（rebaseline 计划 §7） |
| §16 POST 等效动作族/method 边界 | `routes/write_groups.py`、`selector.py` | `test_method_boundary_v4.py`、`test_post_actions_v4.py` | 无（修订二 owner 裁决 q1/q2） |
| §17 non-goals | —（边界声明） | —（负向由既有测试覆盖） | —（裁决内嵌契约） |
| 附录（设计对应表） | — | — | v4-contract.md:704-712 自载 |

---

## 7. 计数汇总

- v3-contract 节摘要：**16** 单元（§0/§1/§2/§3/§3a/§4/§4a/§4b/§5(含5.7a)/§6/§7/§8/§9/§10/§11/§12）。
- v4-contract 节摘要：**19** 单元（§0/§1/§2/§3(3.1-3.3)/§4(4.1-4.6)/§5/§6/§7(7.0-7.5)/§8/§9/§10/§11/§12/§13/§14/§15/§16/§17/附录）。
- 漂移点：**D1–D22 共 22 条**（§3 精读七文档）+ specs 扫读滞后 **S1–S12 共 12 条**（§5）；其中 S6/S7/S8/S9/S10 无冲突（记录为「无冲突」行，实质冲突/滞后 7 条：S1–S5、S11、S12）。
- deploy 三方对账差异：**5 条**（§4.2 表 + §4.3；其中 :32-33 两行构成对 operations.md:92-94「已清理」声明的 P1 级矛盾证据，:33 为 startup-fatal 级）。
