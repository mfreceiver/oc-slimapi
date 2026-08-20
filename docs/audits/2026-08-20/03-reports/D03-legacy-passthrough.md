# D03 — A3 legacy / 透传遗留审计报告

| 项 | 值 |
|---|---|
| 专项 | A3（legacy / 透传遗留） |
| 快照 | 0b836e7（HEAD = release v4.4.0） |
| 日期 | 2026-08-20 |
| 输入 | 01-explore/parts/e1-16（proxy/selector）、e1-05（shim 四文件）、e1-13（traffic 七文件）、file-cards.md、dataflows.md 场景 10、state-machines.md 卡 2/卡 15、route-census.md、CHANGELOG.md [3.0.0]（L1051-1073）、docs/specs/v3-contract.md §8-§10 |
| 方法 | 逐项 rg 负向/正向证据 + 上游源码（opencode-src/current = v1.18.18）只读对照 + 生产 systemd unit 只读核对；全部证据 `file:line` |
| 纪律 | 仓库零写入（本报告与 02-findings 指定文件除外）；未运行 pytest |

---

## 0. 结论摘要

- **proxy.py 终局干净**：docstring 声称的五项退役职责（turn-fence S2 / shell-PTY deny list / directory 校验 / raw-query 转发 / catch-all SSE 记账）在仓内确无残余实现；404/501 形状与 v3 契约 §8.2/§8.3 一致。但审计发现两处**边界外缝隙**：FastAPI 默认 docs 路由穿透（F-137，P2）与 非-7-方法裸 405 / WS 501 语义（F-142，P3）。
- **两个 shim 均有真实生产使用者，不可直接下线**；可下线路径 = 3 处 src import 迁移 + 35 个测试文件机械替换；当前可零成本摘除的仅 hub.py:17 的 `_LAST_UPDATED_AT_BY_SID_MAX` re-export。
- **v2 残余 68 处 rg 匹配中，行为级残余为零**；残余全部为注释/观测冻结枚举/上游 v2 session 组路径族/事件类型名/内部里程碑名，仅 2 个死常量 + 1 个死 re-export 是真死码。
- **passthrough 桶仍可达但语义已变质**：仅非 `/slimapi` 请求命中，且全部为 404/405 拒绝噪声——**除** FastAPI 默认 docs 四路由在该桶产生 200（F-137），破坏「该桶 200 = 意外穿透」哨兵性质。
- **actions manifest 非死配置**（生产 unit override 已启用 + `~/.config/oc-slimapi/actions.toml` 存在）；**qp_sweep 阶段 1 shadow 价值悬置**（metrics 暴露无消费方、阶段 2 无排期，F-140）。
- **F-024 七项全部 rg 验证成立为真死码/只写状态**（详见 §8 裁决表），升级 verified。
- 裁决表共 **26 个编号行 / 28 个子项**（R20 拆 7 子项）：保留理由**成立 10**、**不成立 14 + 部分不成立 1**（合计「保留理由不成立」**15** 子项）、**悬置 3**（R18/R19 同属 F-140 单一决策；R23 偏不成立待 A4 契约对照）。

新发现：**F-136 ~ F-142**（7 条）；更新：F-001/F-002/F-024 升 verified，F-005/F-017/F-019 补 A3 侧证据（仍 draft，终态归 A13/A8/A14）。

---

## 1. proxy.py 终局审计（51 行）

### 1.1 边界性质与 v3 契约 §8 一致性

代码事实（src/oc_slimapi/proxy.py 全文 51 行）：

- `install_proxy(app)`（proxy.py:31）注册且仅注册两条路由：
  - `@app.websocket("/{path:path}")` → `websocket_not_supported`（proxy.py:34-38）：`accept()` → `send_json({"code":"websocket_not_supported","status":501})` → `close(1011)`。
  - `@app.api_route("/{path:path}", methods=[GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS])` → `catch_all_closed`（proxy.py:40-51）：恒返回 `error_response("thin_route_not_found", 404, accept_encoding=…)`（gzip 协商 + `Vary: Accept-Encoding`，经 gzip_util.json_response 同路）。
- 零上游 IO：本文件 import 仅 `fastapi` + `.gzip_util.error_response`（proxy.py:26-28）——无 httpx、无 `app.state.upstream`、无 query/body 读取（`path` 形参未用，proxy.py:44）。

契约对照（docs/specs/v3-contract.md §8.2/§8.3）：

| 契约条款 | 实现 | 一致性 |
|---|---|---|
| §8.2-3.0.0「未收编路径 → 404 `{"code":"thin_route_not_found"}`」 | error_response 产出 `{"code":"thin_route_not_found"}` | ✅ 一致（tests/test_proxy.py:60-113 参数化 7 方法 × 多路径钉死，且断言 `seen == []` 上游零接触） |
| §8.2-3.0.0「WebSocket 仍 501 stub」 | proxy.py:34-38 | ✅ 一致；注意呈现层级 = WS 握手成功(101)后应用层 JSON+close(1011)，**非 HTTP 层 501**（state-machines D2-2 种子，并入 F-142 记录） |
| §8.3 优先链 ①405→②版本400→(method 405)→③directory 400→④404 | selector 先于路由栈应答（§1.3 矩阵）；proxy 只表达 ④ | ✅ 一致（tests/test_terminal_matrix.py:201-300 优先级组） |
| CHANGELOG [3.0.0]「turn fence 迁移至 write_groups」 | write_groups.py:178-184 | ✅ 见 §1.2 第 1 项 |

### 1.2 docstring 退役职责逐项负向证据（proxy.py:16-24 声称 "moved or deleted"）

| # | 声称退役的职责 | 负向/转移证据（rg 全仓） | 裁决 |
|---|---|---|---|
| 1 | Turn-fence S2 bump（prompt_async/abort）→ 移至 write_groups `_write_passthrough` | **moved 属实**：routes/write_groups.py:182-184（`is_turn_bumping_path`/`extract_sid_from_path` import 于 write_groups.py:85；bump-before-send 于 `await …send` 之前，write_groups.py:186-192）；turn_registry.py:281 注释自证「terminal keeps the S2 turn-fence bump on the annexed write routes」；tests/test_turn_registry.py:470 迁移钉死 | ✅ 无残余 |
| 2 | Shell/PTY deny list | **deleted 属实**：全 src 无任何 shell/pty 路径表（rg `shell|pty|deny` 仅剩 proxy.py:21 docstring 自述 + actions.py 无关的 `shell=False`）；tests/test_proxy.py:116-127 钉死 `/session/{sid}/shell`、`/pty`、`/api/pty/x` → 404（非 403）。**但发现死配置残余**：`config.py:491-498` `shell_deny_list_enabled`（env `OC_SLIMAPI_SHELL_DENY_LIST_ENABLED`）仍存在，唯一消费者是 app.py:636,644 启动日志行；且 config.py:492-493 注释仍称「path table is code-level in proxy.py」——已不成立；INTERFACE_MAP.md:149 仍把它写成有效 Ops 开关 | ⚠️ 实现已删，**配置旋钮+文档残余 → F-136** |
| 3 | directory header/query 校验（closed surface 上） | **deleted 属实**：closed surface 一律 404（tests/test_proxy.py:130-147：`X-Opencode-Directory: ../escape` / `?directory=../escape` 均 404、零上游接触）。directory 校验的**活**实现唯二：selector 消费梯（selector.py:636-717，仅 `/slimapi` consuming 集）与 routes 层 `validate_directory`（13 处） | ✅ 无残余 |
| 4 | raw-query 逐字转发 | **deleted 属实**：proxy.py 不读 query；raw query 机器仅剩 selector 的 `_strip_query_keys/_strip_v_segments`（selector.py:478-499，v 剥离——契约 §5.2 活语义）与 routes 的 `_raw_upstream_url`（_read_passthrough.py:103-115，tolerant 路由保真转发 + 幂等 v 剥离——防御 selector-less 栈）；无任何转发器形态代码 | ✅ 无残余 |
| 5 | catch-all SSE 透传记账 | **deleted 属实**：`sse_open/sse_close` 生产调用点仅 routes/events.py:182,242 与 routes/token_stream.py:252,307（两个 `/slimapi` SSE 端点）；tests/test_proxy_sse_observability.py:4-14 钉死 `/event`、`/global/event` → 404 + 恰一行普通 request 行、无 lifecycle 行、sseActive 各维零移动 | ✅ 无残余 |
| （附） | 上游字节计数 | write_groups.py:151-154 注释自证 parity（收编写管线对 buffered body 计 upOut，`stash_up_out`）；proxy 无计数 | ✅ 迁移完整 |

### 1.3 install_proxy 挂载点与到达条件矩阵

挂载序（app.py:735-762）：`register_error_handlers`（735）→ `add_middleware(SlimapiSelectorMiddleware)`（747）→ `add_middleware(TrafficAccountingMiddleware)`（753）→ `add_middleware(RequestIdMiddleware)`（755）→ 18 个 router include（760-761）→ **`install_proxy(app)`（762，最后）**。Starlette last-added-outermost ⇒ 实际栈序 RequestId ⊃ TrafficAccounting ⊃ Selector ⊃ 路由(含 catch-all)。catch-all 注册于全部 router 之后，`/slimapi` 收编路由永不被遮蔽（app.py:756-758 注释自证 design §5.1）。

到达条件矩阵（复用 state-machines.md 卡 2，A3 修订两项：**加 FastAPI docs 路由行；加 `//` 变体行**）：

| 请求形态 | 先行拦截 | 到达 catch-all 条件 | 结果 | 备注 |
|---|---|---|---|---|
| 非 `/slimapi` HTTP，7 方法内，非 docs 四路径 | selector 零触碰（selector.py:516-523，仅 stash `not_applicable`） | 路由表必 miss（全部收编路由带 `/slimapi` 前缀） | 404 `thin_route_not_found` | 桶=passthrough |
| **非 `/slimapi` 且 ∈ {`/docs`,`/redoc`,`/openapi.json`,`/docs/oauth2-redirect`}** | 无（FastAPI `__init__` 期注册的默认路由，app.py:734 未传 `docs_url=None` 等） | **不到达**（先命中 docs 路由） | **200** | **F-137：passthrough 桶出现合法 200，破坏哨兵性质** |
| `/slimapi` HTTP，v/directory/method 任一 400/405 | selector ①②③（selector.py:533-619） | **不可达**（selector 直接应答） | — | proxy.py:9-14 注释即此约定 |
| `/slimapi` HTTP，admitted 但路由未收编 | selector `_forward`（selector.py:620，strip v） | 路由表 miss | 404 `thin_route_not_found` | 桶=对应 `/slimapi` 前缀桶或 `other` |
| `/slimapi//versions` GET（斜杠折叠变体） | selector 豁免放行（`_normalize_path` 命中，selector.py:533） | 路由层见**原始** path → miss | 404 | 「selector 放行、路由 404」已知形态（归口 A4，此处记矩阵行） |
| WS 任意路径 | selector/traffic 均只处理 http scope（selector.py:511-513；traffic_accounting.py:162-165） | 恒可达 | 501 JSON + close(1011) | 无记账、无 X-Request-ID 回显（F-023 关联） |
| TRACE/CONNECT 等域外方法 | Starlette 路由层 | catch-all methods 列表不含（proxy.py:42） | Starlette 原生 405（**非 coded 体**） | F-142 |

**终局矩阵结论**：catch-all 在 HTTP 面只可能收到「已通过全部 selector 门但无路由」的请求——405/400 先于 404 的链路性质成立，与 §8.3 一致；唯二例外是框架自带的 docs 路由（200，F-137）与域外方法裸 405（F-142），均非 selector/catch-all 逻辑错误，而是**装配面**（FastAPI 默认路由 + methods 列表广度）缝隙。

### 1.4 附带核对

- 404 体 gzip 路径：`error_response` → `json_response` 无 MIN_GZIP_BYTES/收益门（gzip_util.py:110-123）——~34B 的 404 体在 `Accept-Encoding: gzip` 下仍压缩、可净膨胀（tests/test_proxy.py:150-166 只钉 gzip 生效，未钉收益）。该不对称归 **F-026**（A9 主辖），此处仅交叉引用。
- `X-Slimapi-Version`：proxy.py 零读取；全 src 零读取（rg 命中仅 config.py:610 注释与 tests 断言不解读，如 tests/test_health.py:215-224、test_terminal_matrix.py:140）——3.0.0 删除声明实现面干净。✅

---

## 2. shim 清查

### 2.1 sse/hub.py（42 行，纯 re-export）

re-export 表（hub.py:17-42）：global_hub 2 符号（`GlobalHub`、`_LAST_UPDATED_AT_BY_SID_MAX`）+ hub_types 22 符号 + registry 1 符号（`HubRegistry`），共 25 个。

真实使用者（rg 正向，file:line）：

| 消费面 | 位置 | 用到的符号 |
|---|---|---|
| 生产-运行时 | app.py:33 | `HubRegistry` |
| 生产-运行时 | routes/events.py:7 | `STOP`、`SubscriberCapacityError`、`sse_frame` |
| 生产-类型 | sse/tokenstream/hub.py:108、tokenstream/subscriber.py:24（TYPE_CHECKING） | `HubRegistry` |
| 测试 | **28 个文件**（`rg -l "from oc_slimapi\.sse\.hub import" tests/`；清单见证据附录） | 全集 |

生产实际消费 **4/25** 符号；`_LAST_UPDATED_AT_BY_SID_MAX`（hub.py:17）经 shim **零消费者**（测试直连 global_hub，tests/test_batch3_lifecycle.py:1138）——可立即摘除的一行（F-138）。

### 2.2 sse/token_hub.py（23 行，纯 re-export）

re-export 表（token_hub.py:5-23）：tokenstream 18 符号（含 9 个下划线私有符号，`# noqa: F401`）。

真实使用者：

| 消费面 | 位置 | 用到的符号 |
|---|---|---|
| 生产-运行时 | app.py:36 | `TokenStreamHub`、`TokenStreamRegistry` |
| 生产-运行时 | routes/token_stream.py:61-66 | `STOP`、`TokenSubscriberCapacityError`、`_resync_frame`、`sse_frame` |
| 生产-类型 | sse/global_hub.py:55（TYPE_CHECKING） | `TokenStreamHub` |
| 测试 | 7 个文件（test_token_hub / test_token_hub_lifecycle / test_token_hub_flush / test_token_stream_route / test_events_tokens / test_v3_sse_meta / test_sse_replay_wire） | 全集 |

生产实际消费 **6/18** 符号。不一致并存：sse/registry.py:31 类型引用**直连** `.tokenstream`——同一语义两条导入路径（风格漂移）。

### 2.3 下线评估

| 维度 | hub.py | token_hub.py |
|---|---|---|
| 可否立即删除 | 否（2 个运行时 import 点 + 2 个 TYPE_CHECKING 点 + 28 测试文件） | 否（3 个 import 点 + 7 测试文件） |
| 迁移成本 | 机械替换 4 处 src + 28 测试文件（低风险、纯 import 行改写） | 机械替换 3 处 src + 7 测试文件 |
| 立即可做 | 摘除 hub.py:17 `_LAST_UPDATED_AT_BY_SID_MAX`（零消费者） | 无（全部 re-export 均有经 shim 消费者） |
| 收益 | 收窄 6 个下划线私有符号的固化公共 API 面；消除 frames.py 复制论证的根（见 F-141） | 同左；统一 registry/global_hub 双路径 |
| 建议 | 保留至下一次触碰 sse 包的 minor：批量迁 import 后删 shim；无退役时间表则漂移风险常驻（e1-05 疑点 5 同判） | 同左 |

**shim 可下线结论（一句话）**：两个 shim 均有真实生产使用者、不可直接删除；下线是一次低风险机械迁移（合计 7 处 src import + 35 个测试文件），建议随下一次 sse 包 minor 一并收敛，当前唯一可零成本摘除的是 hub.py:17 的 `_LAST_UPDATED_AT_BY_SID_MAX` re-export。

附带：sse/tokenstream/frames.py:25-36 复制 `sse_frame`/`_now_ms` 的理由（「hub.py re-export 会成环」）**失效**——hub_types.py 是叶子（仅 import stdlib/orjson/logging_config/replay_wire，hub_types.py:11-25；replay_wire 仅 import replay_log），frames 可直连 hub_types 而无环。→ F-141。

---

## 3. v2 残余分类清单（`rg -n -i "\bv2\b" src/` = 68 匹配，人工过滤后 6 类）

| 类 | 计数 | 代表证据 | 裁决 |
|---|---|---|---|
| C1 观测枚举死值（wire 冻结域） | 5 | selector.py:99（`SELECTOR_V2` 常量，**零引用死常量**）、selector.py:109 / traffic_snapshot.py:111 / traffic.py:616 / access_log.py:310（`v2` dim 文档化） | 域值=契约 §9.2 冻结（3.0.0 changelog 明示「absent/v2 维度自然归零是预期」）→ **保留成立**，5.0.0 收敛 (4,4) 时一并修剪；但 `SELECTOR_V2`/`SELECTOR_ABSENT` 两个**常量符号**本身零引用 → F-138 |
| C2 历史/定位注释 | ~18 | proxy.py:3、app.py:737、envelope.py:3-5（「v2 bytes」= v3 信封拼接的裸数组段，活语义描述）、etag.py:65、_catalog_common.py:277 | 保留成立（注释性） |
| C3 仓内里程碑名「lite-v2」 | ~14 | messages.py:68,142,959,976,1152、global_hub.py:118,171,179,740,795,798,868,869、hub.py:3、health.py:32、tokenstream/hub.py:639 | 保留成立（指 1.0.0 内部重构纪元，非 wire v2） |
| C4 上游 opencode「v2 session 组」路径族（活的） | ~14 | write_groups.py:27-31,492-577、read_groups.py:100-102,599-624、traffic.py:174 | 保留成立（上游 `/api/session/**` 是现行 API 组名，正确用法） |
| C5 上游事件类型名 `question.v2.asked`/`permission.v2.*`（活的） | 3 | hub_types.py:74-76 | 保留成立（上游实发类型；注意 `.resolved` 后缀为幽灵 → F-001） |
| C6 无关同名词 | ~4 | providers_projection.py:66-67（`providers-projection-v2` 表示域指纹，与 wire v2 无关）、skeleton.py:818 / dbaux/path_resolution.py:1 / dbaux/lifecycle.py:64（内部设计文档 v2.2 行号） | 保留成立 |

**分类结论**：行为级 v2 残余为零；死码仅 C1 的 2 个常量符号 + §2.1 的 1 个死 re-export（均归 F-138）。`X-Slimapi-Version` 不解读的实现面已验证干净（§1.4）。

---

## 4. 宽容路由语义与匿名消费方关系（v3 §10.a）

对象：`GET /slimapi/api/session/active`（read_groups.py:582-588，桶 `session_active` traffic.py:186-187）与 `GET /slimapi/global/health`（read_groups.py:591-596，桶 `global_health` traffic.py:188-189）。

代码事实：

- 两者均为一等收编受控代理路由：`read_passthrough_get(request, upstream_path=…)`，**不传 `directory`、不传 `project`** → 纯 identity 透传、不占转换池（_read_passthrough.py:157-200 注释冻结规则）、ETag 启用（§10.a 缺省）。
- directory 宽容语义的实现链：两路由不在 selector `_DIRECTORY_CONSUMING_PATTERNS`（selector.py:143-188）→ selector 放行不消费（§5.5 tolerant）→ `_raw_upstream_url` 把 query（**含 `?directory=`**）逐字转发上游（_read_passthrough.py:103-115）；入站 `X-Opencode-Directory` 头不被这两个 handler 绑定（它们不调 `_resolve`）→ 按「thin 路由不自动转发客户端头」静默丢弃。
- 上游锚点：`groups/session.ts:146-152`（/api/session/active 无 query）与 `groups/global.ts:76-80`（/global/health 无 query）——上游不读这些参数，转发冗余字节无害。

与匿名消费方的关系评估（引 §8.2-3.0.0「收编全集 = ocdroid StandardApi 全量端点闭包 + **匿名消费方实测基线**」）：

1. 这两条是 3.0.0 关闭 catch-all 后匿名消费方**经 sidecar** 的仅存全局面（无 directory 维度）；关闭 catch-all 使它们从「宽容忽略」升级为「匿名消费方的强制入口」——保留必要，且 §10.a 已冻结（「不消费」列）。
2. 语义与契约一致：无歧义、无死代码；`?directory=` 逐字转发属 §5.2 冻结行为（tolerant 集），非遗留缺陷。
3. 观测完备：专属桶 + selectorResult=v3/v4 正常入账；无 passthrough 混入。
4. 注意点（记录非缺陷）：目标态下匿名消费方可走 stunnel 14096 直连（AGENTS.md 顶部图注），与本路由并存的分流边界**无文档化判据**（何时直连何时走 sidecar）——归 A14 文档面，D03 仅记录。

**裁决：保留理由成立**（契约冻结 + 匿名消费方实测基线承载）。

---

## 5. passthrough 记账现存来源与 not_applicable 维度

### 5.1 `passthrough` 桶可达性（确定结论）

`bucketize`（traffic.py:91-192）：非 `/slimapi/` 前缀的一切路径 → `"passthrough"`（traffic.py:191-192）；空 path → `"other"`（traffic.py:99-100）。

3.0.0 后的命中路径全集：

| 来源 | 状态码 | 说明 |
|---|---|---|
| 任意非 `/slimapi` 路径 × 7 注册方法 | 404 `thin_route_not_found`（catch_all_closed） | selector **不拦截**非 `/slimapi` 请求（只 stash not_applicable 后放行，selector.py:516-523）→ 请求**确实到达** catch-all 并被记账（bucket=passthrough、errors4xx） |
| 任意非 `/slimapi` 路径 × TRACE/CONNECT 等 | 405（Starlette 原生） | 同桶入账 |
| **`/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect`**（GET/HEAD） | **200** | **F-137**：FastAPI 默认路由未关闭（app.py:734），实测挂在路由表（introspection：4 条 Route） |
| 上游流量（upIn/upOut） | — | **永不**：catch-all 零上游 IO（§1.1），passthrough 桶 upIn/upOut 恒 0 |

**确定结论**：catch-all 关闭后 passthrough 桶仍可达，但语义已从「真实过境流量」变质为「拒绝噪声 + 框架自带 docs 面 200」；运维口径「按 bucket==passthrough 找未省流请求」（AGENTS.md:65）与「ratios.passthrough ≈ 1.0 透传基线」（docs/manual/traffic-accounting.md:112,138,273）已失真——手册自身在 :153/:179 又正确记录了 3.0.0 关闭事实，**同文档内自相矛盾** → F-139。

### 5.2 `not_applicable` 维度现状

- **REST 矩阵**：活——所有非 `/slimapi` 请求由 selector stash `not_applicable`（selector.py:104,516-523），与 passthrough 桶行一一配对；作为「未省流面是否复发」的哨兵维度保留（dataflows 场景 10 D1 同判）。
- **SSE 生命周期行（sseActive 表）**：**生产不可达死维**——两个 SSE 端点均在 `/slimapi/**`（events/token_stream），selector 必给 v3/v4；`not_applicable` 只对非 `/slimapi` 请求产生，而它们 404、永不打开流（tests/test_proxy_sse_observability.py 钉死）。同理 `v2` dim 生产不可达；`absent` 仅作未知值折叠兜底（sse_observability.py:59）。域值保留 = 契约 §9.2 冻结；但 sse_observability.py:12-13 docstring 的 dim 列表**漏 v4 且列 not_applicable**，文档漂移 → 并入 F-139。
- `_SSE_DIMS`（traffic_snapshot.py:111）与 `selector.SSE_RESULT_DIMS`（selector.py:109）手工双拷贝（注释自认 "grep-verified"）——保留成立（有纪律注释），改进项记录（import 复用可消除）。

---

## 6. actions manifest 死配置检查 + qp_sweep 阶段 1 shadow 价值

### 6.1 actions manifest：**非死配置，生产已启用**

- 配置面：`actions_file`（config.py:507，env `OC_SLIMAPI_ACTIONS_FILE`，None=禁用）+ `actions_max_concurrent`（config.py:509）。
- deploy 面：repo 单元 deploy/oc-slimapi.service:60 为注释掉的 opt-in 行（默认关，符合「unset = feature disabled」设计）；示例 manifest deploy/actions.manifest.example.toml（4 动作）与 docs/operations.md:587-630 一致。
- **生产实况（本机只读核对）**：生产 unit override 已启用 `Environment=OC_SLIMAPI_ACTIONS_FILE=/home/mar/.config/oc-slimapi/actions.toml`（`systemctl --user cat oc-slimapi` L82），且 `~/.config/oc-slimapi/actions.toml` 存在 → 路由活、manifest 活、审计链活（actions.py:16-18 非破坏设计面）。
- 裁决：**保留理由成立**（非死配置）。关联遗留：actions 两路由的权威 wire 规范留存于 v2-contract §2（F-019，A14 终判）；repo 单元 L32-33 的过时 env 钉扎归 F-004/F-005（A13）。

### 6.2 qp_sweep 阶段 1 shadow：**价值悬置 → F-140**

- 现状：`QpSweepShadow` 生产装配活（app.py:501-517，`qp_sweep_enabled` 默认 true config.py:622-625；observer 接线 global_hub.set_directory_observer）；纯影子零上游 IO（qp_sweep.py:1-7 模块头自证）。
- 观测暴露：`GET /slimapi/metrics` 的 `sweep` 块（routes/metrics.py:42-44；key 集 triggers_total/cold_hits/skips/budget_exhausted/est_bytes_total/known_directories）。
- **消费侧为空（metrics 暴露 ≠ 消费）**：rg 全 docs（除审计计划自身）——operations.md、traffic-accounting.md、INTERFACE_MAP.md:98 均无 sweep 块的读取方法/jq 配方/告警阈值；docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md:709 的「告警建议（sweep skips 阈值）」仍是 Phase 2 计划产物。
- **阶段 2 状态**：仍是计划、无排期、无取消记录——system-architecture-proposal-2026-08-17.md §3.2a/L322 冻结了阶段 2 exit criteria（7 日窗公式），CHANGELOG [3.3.0] L174 明示 sweep 桶「保留给阶段2 真实请求分类（阶段1 恒空）」；仓内无任何真实 sweep 代码（routes/metrics.py:40-41 注释 "no HTTP sweep is issued"；traffic.py:109-110 的 `sweep` 桶无路由可命中）。
- 裁决：**悬置**——shadow 本身按设计安全（零逃逸、有预算闸、state-machines 卡 15 无缺陷级疑点），但其存在理由（「为阶段 2 提供数据」）依赖一个无排期的后续阶段，且产出（sweep 块）无人消费；需要 owner 决策：①给阶段 2 排期并补消费文档/告警，或②声明放弃并连同 `sweep` 桶、`directory_source` 参数一并退役。→ F-140

---

## 7.（并入 §8）F-024 死代码合集逐项验证

## 8. F-024 逐项裁决表（全部 rg 验证，F-024 升 verified）

| 项 | 位置 | 验证证据（file:line） | 裁决 | 建议处置 |
|---|---|---|---|---|
| `_busy_sids` | sse/tokenstream/hub.py:283（docstring 240 自称「O(1) busy lookup mirror」） | 全 src **零成员读取**（唯一读表达式是自身 prune 的 `len()`，hub.py:2058；写点 1056-1058、pop 1061/1113、clear 2176）；仅测试白盒断言（test_token_hub_lifecycle.py:533-788） | **保留理由不成立**（只写不读的镜像表 + docstring 声称的用途无消费者） | 删除，或真正接线为 `_session_status` 的 O(1) 查询面并删 docstring 虚claim |
| `last_touch`（DomainState） | sse/replay_log.py:216（__slots__）,225,392,430,493,510 | 全 src **零读取**（rg `\.last_touch` 无读表达式）；TTL/recycle 决策均不经它（sweep 用 entry TTL 头部；recycle 由 replay_wire frame_count==0 驱动） | **保留理由不成立**（只写状态） | 删除；或作为 idle-domain 观测暴露（若保留须有消费者） |
| `recycle_domain` 近 no-op | sse/replay_log.py:495-513；调用点 replay_wire.py:274-277 | 唯一调用点条件 `domain_frame_count(domain) == 0` ⇒ while 循环零迭代、bytes 已 0、domain shell **不删**（`_domains` dict 保留）；净效果 = 写一次 write-only 的 `last_touch` + return True | **保留理由不成立（as wired）**：设计意图（§3.4 内存回收）被调用点条件 defeating——sweep 已清空 entries，recycle 无可回收；seq 单调保留语义本就不需要这次调用 | 二选一：删除调用+方法；或把回收条件改为「TTL 过期的非空 domain」使其有真实回收量（需对齐 REPLAY-018 seq shell 保留约束） |
| `directory_source` | qp_sweep.py:41,62,124-129 | 生产装配不传（app.py:504-509 仅 activity/interval/budget）→ `_ingest_directory_source` 生产恒 no-op（qp_sweep.py:127）；仅 tests/test_b1b_sweep_shadow.py:368-383 注入 | **保留理由不成立**（生产死参数；测试专用注入点未标注） | 删除参数（测试改为直喂 `observe_directory`）；或注明 test-seam 保留 |
| `strip_hop_by_hop` + `HOP_BY_HOP`/`FORBIDDEN_*` | upstream.py:11-37,49-111 | src 消费者**零**（rg 全仓：仅 upstream.py 自身 + tests/test_upstream.py:6-190 六用例）；反代退役后无转发路径 | **保留理由不成立**（生产死代码；Set-Cookie comma-merge caveat 一并成遗留） | 删除函数+常量+测试；若声称「预留受控代理框架复用」须有文档化计划（当前无） |
| `build_sessions_query` dead import | routes/sessions.py:12 | sessions.py 内出现次数=1（即 import 行自身）；真实消费在 dbaux 内部与测试（test_sql_semantics.py:15、test_eqp_matrix.py:15 经包导入） | **保留理由不成立**（死 import） | 删除 import 行（零行为影响） |
| `_V4_PARENT_RESERVED` | routes/sessions.py:272 | 全 src+tests **零引用**；保留词语义以字面量内联实现（sessions.py:405,499,506 `in ("all","none")` / `== "none"`），常量从未成为单一真源 | **保留理由不成立**（死常量 + 意图未接线） | 删除；或接线替换内联字面量（对齐 v4-contract §4.1 `parent=all|none|only|<sid>`） |

**F-024 汇总**：7/7 验证为真（死码 4 + 只写状态 2 + no-op-as-wired 1），无一项保留理由成立；全部为低风险清理项，无行为影响（除 recycle 重接线选项外）。

---

## 9. 总裁决表（A3 遗留物全景，26 行）

| # | 遗留物 | 类别 | 裁决 | 关联发现 |
|---|---|---|---|---|
| R1 | proxy.py 终端 404/WS 501 边界本体 | 实现 | **成立**（契约 §8.2 对齐、测试钉死） | — |
| R2 | proxy.py docstring 五项退役职责声明 | 文档 | **成立**（逐项负向证据 §1.2） | — |
| R3 | `shell_deny_list_enabled` 死配置旋钮（config.py:491-498 + app.py:636,644 启动日志） | 死配置 | **不成立** | F-136 |
| R4 | INTERFACE_MAP.md:149 deny-list「Ops 开关」行 + config.py:492-493「表在 proxy.py」假注释 | 文档漂移 | **不成立** | F-136 |
| R5 | FastAPI 默认 docs/openapi 四路由（app.py:734 未关闭） | 装配缝隙 | **不成立** | F-137 |
| R6 | sse/hub.py shim（25 re-export） | 兼容层 | **成立**（生产 4 符号 + 28 测试文件在用） | — |
| R7 | sse/token_hub.py shim（18 re-export） | 兼容层 | **成立**（生产 6 符号 + 7 测试文件在用） | — |
| R8 | hub.py:17 `_LAST_UPDATED_AT_BY_SID_MAX` re-export | 死导出 | **不成立**（经 shim 零消费者） | F-138 |
| R9 | `SELECTOR_V2`/`SELECTOR_ABSENT` 常量（selector.py:98-99） | 死常量 | **不成立**（零引用；字符串值属冻结观测域另行保留） | F-138 |
| R10 | SSE_RESULT_DIMS/_SSE_DIMS 的 `v2`/`absent` 死维值 | 观测冻结域 | **成立**（契约 §9.2 冻结、归零预期已文档化；5.0.0 修剪） | — |
| R11 | `_SSE_DIMS` 与 SSE_RESULT_DIMS 手工双拷贝 | 漂移风险 | **成立**（有 grep-verified 纪律注释；改进项） | — |
| R12 | `passthrough` 桶（traffic.py:191-192） | 观测 | **成立**（哨兵维度：桶内 200 = 意外穿透——正因如此 R5 是缺陷） | F-137 关联 |
| R13 | AGENTS.md:65 + traffic-accounting.md:112/138/273 的 passthrough 运维口径 | 文档漂移 | **不成立**（3.0.0 后该桶=拒绝噪声；手册 :153/:179 自证矛盾） | F-139 |
| R14 | sse_observability.py:12-13 dim 列表（漏 v4、列不可达 not_applicable） | 文档漂移 | **不成立** | F-139 |
| R15 | `X-Slimapi-Version` 不解读实现面 | 实现 | **成立**（全 src 零读取；tests 钉死不解读） | — |
| R16 | v2 注释残余 68 处（C1-C6 分类） | 注释 | **成立**（除 R8/R9 已单列的死符号外全为活语义/历史定位） | — |
| R17 | actions manifest 部署配置 | 配置 | **成立**（生产 unit override 已启用 + manifest 文件在位） | F-019（规范留 v2-contract） |
| R18 | qp_sweep 阶段 1 shadow | 观测机制 | **悬置**（暴露无消费、阶段 2 无排期 → 需 owner 决策） | F-140 |
| R19 | `sweep` 死桶（traffic.py:109-110，恒空） | 预留 | **悬置**（与 R18 同一决策：排期或删） | F-140 |
| R20a | `_busy_sids`（tokenstream） | 只写状态 | **不成立** | F-024 |
| R20b | `last_touch`（replay_log） | 只写状态 | **不成立** | F-024 |
| R20c | `recycle_domain` as-wired no-op | no-op | **不成立** | F-024 |
| R20d | `directory_source`（qp_sweep） | 生产死参数 | **不成立** | F-024/F-140 |
| R20e | `strip_hop_by_hop` + 常量族（upstream.py） | 生产死码 | **不成立** | F-024 |
| R20f | `build_sessions_query` 死 import（sessions.py:12） | 死 import | **不成立** | F-024 |
| R20g | `_V4_PARENT_RESERVED`（sessions.py:272） | 死常量 | **不成立** | F-024 |
| R21 | global_hub.py:11 `import logging` 死 import；hub_types.py:27 `logger` 死变量 | 死符号 | **不成立** | F-138 |
| R22 | frames.py 复制 `sse_frame`/`_now_ms` 的失效环论证（frames.py:25-36） | 结构债 | **部分不成立**（hub_types 叶子化后论证失效，可去重） | F-141 |
| R23 | proxy 域外方法裸 405（非 coded 体）+ WS 501 accept-后-close 语义 | 契约一致性 | **悬置偏不成立**（§8 错误体统一性的结构性例外，无契约豁免条款） | F-142 |

计数：26 行 = 成立 9（R1,R2,R6,R7,R10,R11,R12,R15,R16,R17 中取 10？——精确口径见下行）。

> **精确口径**：成立 10（R1,R2,R6,R7,R10,R11,R12,R15,R16,R17）；不成立 14（R3,R4,R5,R8,R9,R13,R14,R20a-g×7,R21）；部分不成立 1（R22）；悬置 3（R18,R19,R23——其中 R18/R19 同属 F-140 单一决策、R23 偏不成立待 A4 契约对照）。合计 28 子项/26 编号行；**「保留理由不成立」= 15 子项**（14 全不成立 + R22 部分）。

---

## 10. 新发现与更新索引

| 编号 | 标题 | 严重度 | 类别 |
|---|---|---|---|
| F-136 | shell/PTY deny-list 死配置残余：`shell_deny_list_enabled` 旋钮无实现可关 + 启动日志仍广播 + INTERFACE_MAP:149 假 Ops 行 + config 注释指向已删的 proxy 路径表 | P3 | smell/ops |
| F-137 | FastAPI 默认 docs/openapi 路由穿透：`/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` 以 200 落 passthrough 桶——破坏「桶 200=意外穿透」哨兵 + 未列契约/INTERFACE_MAP 的 openapi schema 暴露面 | P2 | defect |
| F-138 | 死符号群：SELECTOR_V2/SELECTOR_ABSENT（selector.py:98-99）、hub.py:17 `_LAST_UPDATED_AT_BY_SID_MAX` 死 re-export、global_hub.py:11 死 `import logging`、hub_types.py:27 死 `logger` | P3 | smell |
| F-139 | 3.0.0 终局后观测口径文档漂移：AGENTS.md:65 与 traffic-accounting.md:112/138/273 仍按活透传教查 passthrough（与同文件 :153/:179 矛盾）；sse_observability.py:12 dim 列表漏 v4 | P3 | docs |
| F-140 | qp_sweep 阶段 1 shadow 价值悬置：sweep metrics 块零消费方（无 jq 配方/告警/手册入口）、阶段 2 无排期无取消、`sweep` 桶恒空、`directory_source` 生产死参——需 owner 排期或整体退役 | P3 | risk |
| F-141 | tokenstream/frames.py:25-36 复制 `sse_frame`/`_now_ms` 的「import 环」论证失效（hub_types 为叶子可直连）——双哨兵/双帧函数并存的结构债可消除 | P3 | smell |
| F-142 | proxy 终局边界两缝隙：TRACE/CONNECT 等 → Starlette 裸 405（非 `{"code":…}` coded 体，§8 错误体统一性例外）；WS 501 stub 为「accept 后 1011 close」非握手期 501——两形均无契约豁免记录 | P3 | contract |

更新：F-001、F-002 → **verified**（上游 schema 双源核对：v1/permission.ts:61-65 `permission.replied`、permission.ts:43-45 `permission.v2.replied`，无 `.resolved`；schema/src/v1/session.ts:597-633 消息事件全集无 `message.appended`）；F-024 → **verified**（§8 表）；F-005/F-017/F-019 补 A3 侧证据（仍 draft，终态归 A13/A8/A14）。

---

## 11. 证据附录（本报告新增的关键 file:line 汇编）

- proxy 边界：src/oc_slimapi/proxy.py:26-28,31-51；tests/test_proxy.py:57-194；tests/test_proxy_sse_observability.py:1-14；app.py:734-762。
- turn-fence 迁移：routes/write_groups.py:85,151-154,178-192；turn_registry.py:281-311；tests/test_turn_registry.py:470。
- deny-list 残余：config.py:491-498；app.py:636,644；docs/specs/INTERFACE_MAP.md:149；tests/test_proxy.py:116-127。
- FastAPI docs 路由：app.py:734（`FastAPI(title=…)` 无 docs_url=None）；introspection 实测 4 条 Route（/openapi.json、/docs、/docs/oauth2-redirect、/redoc，均 GET+HEAD）；traffic.py:191-192。
- shim：sse/hub.py:17-42；sse/token_hub.py:5-23；app.py:33,36；routes/events.py:7；routes/token_stream.py:61-66；sse/global_hub.py:55；sse/registry.py:31；sse/tokenstream/{hub.py:108,subscriber.py:24,frames.py:25-36}；测试 28+7 文件清单（rg 命中，正文 §2）。
- v2 残余：selector.py:67,96-99,109,569；traffic_snapshot.py:111；traffic.py:616；access_log.py:310；envelope.py:3-41；providers_projection.py:66-67；hub_types.py:74-76,88-90。
- 宽容路由：read_groups.py:19-32,582-596；_read_passthrough.py:103-115,157-200；traffic.py:186-189；v3-contract.md §10.a 表（active/global health 行）。
- passthrough/not_applicable：traffic.py:91-192；selector.py:104,516-523；sse_observability.py:12,59；traffic-accounting.md:112,138,153,179,273；AGENTS.md:65。
- actions/qp_sweep：config.py:504-509,622-629；deploy/oc-slimapi.service:56-60（+生产 unit L82）；deploy/actions.manifest.example.toml；app.py:501-517；qp_sweep.py:1-7,41,124-129；routes/metrics.py:40-44；CHANGELOG.md:174；docs/system-architecture-proposal-2026-08-17.md:165-166,322。
- F-024 七项：tokenstream/hub.py:240,283,1056-1061,2056-2059,2176；replay_log.py:216,225,392,430,493-513；replay_wire.py:274-277；qp_sweep.py:41,62,124-129；app.py:504-509；upstream.py:11-37,49-111；tests/test_upstream.py:6-190；routes/sessions.py:12,271-272,405,499,506；tests/test_b1b_sweep_shadow.py:368-383。
- 上游真值（opencode-src/current = v1.18.18）：packages/schema/src/v1/permission.ts:55-65（Asked/Replied）；packages/schema/src/permission.ts:43-45（permission.v2.replied）；packages/schema/src/v1/session.ts:597-633（message.* 全集）。
