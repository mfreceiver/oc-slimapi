# oc-slimapi 4.11.0 交接简报（webui / ocdroid 组）

> **基线**：sidecar v4.11.0（已上线生产）；wire 版本仍为 **v4-only**（`?v=4` 唯一合法）。
> **本批次全部加性，客户端零必改**——不接入任何新能力则行为与 4.10.x 完全一致（唯一例外见 §3.2 validator 轮换，一次性自动重拉）。
> 权威规范：`docs/specs/v4-contract.md` 修订五；消费要点：`docs/specs/CLIENT_CHANGES.md` §4.11.0；变更记录：`CHANGELOG.md` [4.11.0]。
> 本批次 = 流量优化族 P1–P6（4.11.0）+ 批量详情（4.10.0）+ 服务端新鲜度/观测（4.10.1，客户端零感知）。

---

## 0. 一页总览

| # | 能力 | 端点/面 | 类型 | 收益预期 | 建议优先级 | 主要消费方 |
|---|---|---|---|---|---|---|
| P1 | messages `?since=` 前向差分 | `GET /slimapi/messages/{sid}` | 新参数+响应键 | messages 桶 -70~90%（12h 观测该桶 1.53GB，最大流量驱动项） | **P0 最高** | webui、ocdroid |
| P4 | digest `messagesRevision` | `/slimapi/events` digest 帧 | 新字段 | P1 的变化触发器（免轮询） | **P0**（与 P1 配套） | webui、ocdroid |
| P5 | 裸二进制直读 | `GET /slimapi/file/raw` | 新路由 | 图片/附件省 base64 4/3 膨胀 + 收编直连 | P1 | 双端（图片渲染方） |
| P2 | thin 路由 ETag/304 | `todo`/`children`/`diff` | 行为变更（恒200→可304） | 未变化轮询零 body | P1 | webui |
| 4.10.0 | 批量 session 详情 | `POST /slimapi/sessions/details` | 新路由（已上线） | 逐 session 轮询 6.2k 次/12h → 数百次 | **P0**（webui 已可迁移） | webui |
| P3 | readiness 第 11 项 | `/slimapi/versions` capabilities | ID 扩容 | 能力探测口径同步 | P2（核对项） | 双端 |
| P6 | health auxiliary 消费 | `GET /slimapi/health` | 文档指引（面早已在） | 降级感知/退避 | P2 | 双端 |

---

## 1. 新接口

### 1.1 `GET /slimapi/file/raw`（P5，v4-contract §19）— 裸二进制直读

上游 `type=binary` 信封（base64 包装）解码为**裸 bytes** 下发。图片/附件渲染专用。

```
GET /slimapi/file/raw?path=<必填>&v=4[&directory=<可选>]
```

| 面行为 | |
|---|---|
| 成功 200 | 裸 bytes body；`Content-Type` 保真（上游 MIME 非法/缺失 → 回退 `application/octet-stream`） |
| `type=text` | 按 `text/plain; charset=utf-8` 下发（非二进制） |
| 编码 | binary 恒 identity（声明 gzip 也不压缩）；text 走常规 gzip 协商 |
| 缓存 | `Cache-Control: no-store`（不可存储缓存）；binary 强 ETag + `If-None-Match` 304 可用；text 弱 ETag（`W/"…"`） |
| 错误 | `path` 缺失 → 400 `invalid_params`；信封畸形 → 502 `raw_decode_failed`；超限 → 413 `response_too_large`；上游 4xx **verbatim 透传**（含 404）；上游 5xx/网络 → 503 `upstream_unavailable` |
| directory 语义 | 与 `/slimapi/file` 组一致（`?directory=` 或 `X-Opencode-Directory` 头，allowlist 拒绝 → 403） |

**推荐用法**：`HttpImageHolder` / 图片加载从直连 opencode 或 `/slimapi/file` 迁到本端点；404/失败降级路径维持既有兜底。

### 1.2 `POST /slimapi/sessions/details`（4.10.0 已上线，同族提醒）

```
POST /slimapi/sessions/details?v=4
{"sids": ["ses_…", …]}        // 去重后 ≤50，>50 → 400 too_many_sids
→ 200 {"sessions":[<骨架>…], "missing":["ses_…"]}
```

item 形状与 `GET /slimapi/session/{sid}` 逐字段一致（同一 projector）；dbaux 路径 `degraded:false`、native 回退 `degraded:true`；任一 item 不可表示 → 整批 503 fail-closed（不混装部分数据）。**webui 逐 session 轮询（12h 观测 5.5k+ 次单查）应迁移到本端点**。

---

## 2. 既有接口的新能力

### 2.1 messages `?since=` 前向差分（P1，v4-contract §10.3）— 本批次核心

**问题**（12h 观测）：热点 session 被全量重拉 ~2 次/分钟（单 session 351MB、占总流量 23%）——列表接口当轮询用。

**消费闭环**：

```
① 首次全量   GET /slimapi/messages/{sid}?v=4&limit=100
             → 200 {items:[…], nextCursor, nextSince:"<token>"}   ← 持久化 nextSince
② 订阅变化   GET /slimapi/events?v=4（digest 帧；或退化为低频轮询③）
③ 差分刷新   GET /slimapi/messages/{sid}?v=4&limit=100&since=<token>
             → 200 {items:[变更投影…], nextCursor, removed:[消失的mid…], nextSince:"<新token>"}
```

| 语义要点 | |
|---|---|
| `items` | 差分窗口内**新增 + fingerprint 变化**的消息投影（非全量） |
| `removed` | 条件键（无删除则**键缺席**，非空数组）；逐 mid 删除本地条目；**无假阳性**（契约不变量），漏报可能（保守分支）→ 用 P4 revision 对账兜底 |
| `nextSince` | 条件键；**键缺席 = 并发竞争降级或 `before` 响应** → 丢弃旧 token，走①重新拿；不要拿旧 token 重试差分 |
| **reset 识别** | 无显式标记键：200 且 `items` 呈全量形状 + 携带新 `nextSince` → **全量替换本地视图**。触发：服务重启/503 后、limit/directory/mode 变化、服务端缓存逐出——属正常路径非错误 |
| token 域 | `{v:1, epoch, sid, cq_hash, gen}` **进程域**：跨 sidecar 重启必然 reset；不可跨 session 复用（sid 失配 400） |
| 400（真错误，修客户端 bug） | `since`+`before` 同现；token 语法损坏；sid 失配；token >512B——错误码一律 `invalid_params` |
| 其余失效 | 一律**静默 reset**（200 全量 + 新 token），不报错 |

**Do / Don't**：
- ✅ digest 帧（P4）的 `messagesRevision` 变化 → 触发③；SSE 断线期间退化为低频轮询③。
- ✅ reset 后重新持久化新 token。
- ❌ 不要定时盲拉全量列表替代差分（这正是被观测到的反模式）。
- ❌ 不要缓存/复用跨进程的 token；不要在 `before` 翻页响应里找 `nextSince`（那里没有）。

### 2.2 thin 三路由 ETag/304（P2，v4-contract §6.4）

```
GET /slimapi/sessions/{sid}/todo?v=4       ← 注意前缀是复数 sessions
GET /slimapi/sessions/{sid}/children?v=4
GET /slimapi/sessions/{sid}/diff?v=4
```

4.11.0 起三路由入 ETag 全集：响应携带 `ETag`，回发 `If-None-Match` 重放，内容未变 → **304**（≤4.10.x 恒 200 全量）。`Vary: Accept-Encoding`、`Cache-Control: no-store` 语义不变。客户端缓存三路由响应 + 回发 validator 即自动受益；不接入则行为不变。

### 2.3 digest `messagesRevision`（P4，v4-contract §7.5）

- message 域 digest 帧新增 `messagesRevision: <int>`（session-only digest **无**此键）。
- 用途 = **变化信号**：revision 变了 → 触发 P1 差分或 If-None-Match 精拉；revision 没变 → 什么都不用做。
- **进程级单调，不得跨重启比较**；SSE 重连/upstream resync 后可继续比较（resync 帧后收到更小值属正常，以进程内最新值为准）。不承载 per-sid 语义、不是序号承诺。

---

## 3. 变动与注意点

### 3.1 readiness 扩为十一项（P3）

`GET /slimapi/versions` → `capabilities["4"].readiness.required` 新增第 11 ID **`sessions.details.v4`**（retroactive 正名，批量详情面 4.10.0 已生效）。**硬编码十项清单的客户端需同步**——少识别一项会误判 `required ⊄ satisfied`（contradiction）。

### 3.2 messages ETag validator 轮换（D6，一次性）

messages 路由 ETag 域标签统一为窗口版本 4 → 升级后旧 v4 ETag **全部自然失效**，缓存该路由的客户端会经历一轮全量重拉，之后恢复正常。无需处理，知悉即可。

### 3.3 health auxiliary 消费（P6，文档性）

`GET /slimapi/health?v=4` 的 `auxiliary` 块（`available`/`mode`）反映 DB 投影源状态：`degraded` 时 sessions/messages 系可能 503 `auxiliary_unavailable` + `Retry-After: 30`——按 503 语义退避即可，不必解析错误体内部。

---

## 4. 能力探测（运行时）

| 探测 | 信号 |
|---|---|
| P1 since | 请求 messages 不带 `since`，响应含 `nextSince` 键 = 支持（缺键 = 旧 sidecar，走全量） |
| P2 ETag | thin 响应头有 `ETag` = 支持 |
| P4 | digest 帧含 `messagesRevision` = 支持 |
| P5 | `GET /slimapi/file/raw` 非 404 `thin_route_not_found` = 路由存在 |
| 4.10.0 批量 | `POST /slimapi/sessions/details` 非 404 = 支持 |
| readiness | `capabilities["4"].readiness.satisfied` 含对应 ID |

版本底座：`GET /slimapi/versions` → `available:[4]`；所有 `/slimapi/**` 请求必带 `?v=4`。

---

## 5. 错误面速查

| 码 | 场景 | 客户端动作 |
|---|---|---|
| 400 `invalid_params` | since token 损坏/sid 失配/`since`+`before` 同现；file/raw 缺 `path` | 修客户端 bug，走全量重来 |
| 413 `response_too_large` | file/raw 信封超 cap | 放弃该资源或走 `/slimapi/file` 展开路径 |
| 502 `raw_decode_failed` | file/raw 上游信封畸形 | 当作资源损坏，走降级渲染 |
| 503 `upstream_unavailable` | 上游 5xx/网络；file/raw 亦然 | 指数退避重试 |
| 503 `auxiliary_unavailable` | dbaux 降级（+`Retry-After: 30`） | 按 Retry-After 退避 |
| 503 `transform_busy` | 转换池满（+`Retry-After`） | 按头退避 |
| 404 `thin_route_not_found` | 旧 sidecar 无该路由 | 回退既有路径（file/raw → 直连或 `/slimapi/file`） |
| 200 全量 + 新 nextSince | since **reset**（非错误） | 全量替换本地视图 |

---

## 6. 效果预期与观测口径

- 12h 基线（2026-08-21 20:07→08:07）：总请求 17.3k / 上行 1.74GB；messages 桶 1.53GB（93% 已被投影压缩，但全量重拉模式使**压缩后仍是最大项**）；热点 session 351MB/625 次拉取；逐 session 单查 5.5k 次。
- 迁移后预期：webui 列表刷新走 P1+P4 → messages 桶再降 70–90%（~0.4–1.2GB/12h）；单查轮询走 4.10.0 批量 → 请求数 -90%+；thin 走 304 → 未变化轮询零 body。
- 服务端观测：运维按天 access log 聚合验证（`docs/operations.md` §13.3 有现成脚本）；客户端侧可自记 since 请求的响应字节对比全量字节。
