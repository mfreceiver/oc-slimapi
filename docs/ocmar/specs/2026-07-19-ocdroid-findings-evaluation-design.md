# 评估并修复 ocdroid 接口评审发现 + 补齐 v1 遗留（F1–F5 / §5 / G1 / G6 / D1–D8）— 设计 spec

- **日期**：2026-07-19
- **slug**：`ocdroid-findings-evaluation`
- **基线**：`origin/main`（commit `9373550`，working tree 干净）
- **owner**：`ocmar-ocdroid-findings-evaluation`
- **输入**：ocdroid《slimapi 接口评审报告》§3–§6（F1–F5 + §5 文档建议）+ 本仓 v1 文档遗留审计（G1/G6/D1–D8）
- **状态**：待用户确认（范围已较首版扩大，需 re-confirm）

---

## 1. 需求回顾

ocdroid 完成 app 开发后提交了 oc-slimapi v1（B1，2026-07-18）接口评审报告（5 项发现 F1–F5 + §5 文档建议）。本 spec 在「评估 + 修复 F1–F5/§5」基础上，**经用户确认扩大范围**，一并补齐本仓 v1 文档遗留审计发现的 **G1**（错误可见性，原 impl-spec §7 承诺但未实现）、**G6**（批量展开端点，原 impl-spec §8 承诺但未实现，ocdroid 现走 404 fallback）、以及 **D1–D8** 八项文档同步。

### 用户已确认的修复方向（brainstorming 🔑1 + 🔑2 扩范围）

| 项 | 选定方向 | 来源 |
|---|---|---|
| **F1** q/p directory 必填 | 两者都做：sidecar 允许 null=聚合 allowlist + 契约写明 cold-start 顺序 | ocdroid 报告 |
| **F2** listed-but-rejected | 放宽 per-session status 的 allowlist（sid 自洽即能力） | ocdroid 报告 |
| **F3** allowlist 冷启动 400 窗 | 三管齐下：`_token` 走 `require_directory` + 启动 warm-up + 文档 | ocdroid 报告 |
| **F4** CLIENT_CHANGES SSE 过期 | 同步 SSE 节与 INTERFACE_MAP §3 一致 | ocdroid 报告 |
| **F5** `accepted:[1,1]` 标注 | contract §1 加闭区间说明 | ocdroid 报告 |
| **§5** 文档结构改进 | 全量（directory 三态表 / allowlist 独立节 / CLIENT_CHANGES 同步纪律 / 跨端点一致性） | ocdroid 报告 |
| **G1** 错误可见性 | **本批实现**：`digest.lastError` + session-less `event: session.error` 帧 + 脱敏算法；B0 已验证 GO | 本仓审计 + exp-1/2 调研 |
| **G6** 批量展开 | **本批实现**：`GET /slimapi/messages/{sid}/full?ids=`（discover 先行、mid 级 envelope、累计 413） | 本仓审计 + exp-3 调研 |
| **D1–D8** 文档同步 | 全折叠进本批 | 本仓审计 |

### 范围边界

- **在范围内**：F1–F5 全修 + §5 文档重构 + G1 实现 + G6 实现 + D1–D8 文档同步 + 受影响配套文档（INTERFACE_MAP / impl-status / design-v2 / CLIENT_CHANGES / CHANGELOG / AGENTS.md）。
- **不在范围内**：
  - 不 bump `X-Slimapi-Version`（所有变更加性，详见 §4）。
  - 不发版（不打 git tag、不动 `pyproject.toml`）；release 留给用户显式触发。
  - 不改 ocdroid 仓库（客户端侧动作仅记 CLIENT_CHANGES）。
  - 不动 §10 显式延后项（skeleton 缓存 / 多用户 / circuit breaker / durable SSE replay 等）。
  - 不实现 G3-B（独立 latest 探针）、G7-strict、G9–G13（impl-spec §16 列的 v2 项）。
  - 不动 `/sync/history` 的 error 回放（session.error 非 durable，exp-1 确认；G1 仅走实时 `/global/event`）。

---

## 2. 成功标准

### F1–F5 + §5（原批次）
1. **F1**：`GET /slimapi/questions` + `/permissions` 不传 `directory` → 200，聚合 sidecar allowlist 全部 dir；空 allowlist（含刷新失败）→ 200 空 envelope。
2. **F2**：`GET /slimapi/sessions/{sid}/status` 对非白名单 dir 的 sid → 200，不再 400；批量 `/sessions/status` 行为不变。
3. **F3**：(a) routeToken 应答在冷启动 allowlist 空时，对可发现 dir 成功（自动刷新）；不可发现 dir 仍 400；(b) lifespan 启动主动 warm `load_projects`，失败仅 log 不阻断。
4. **F4**：`CLIENT_CHANGES.md` SSE 节不再出现 `?directory/sessionId/stream`。
5. **F5**：`v1-contract.md §1` 给 `accepted:[1,1]` 加闭区间说明。
6. **§5**：`v1-contract.md` 新增独立 §「directory 三态语义表」+ §「allowlist 机制」；cold-start 顺序写明含暖机；跨端点一致性。

### G1（错误可见性）
7. **G1-A**（有 sid）：`session.error`（非 abort）带 sessionID 到达 → 立即 flush digest，含 `lastError:{name,message,at}`；跨 debounce 窗口 sticky，直到 clear。
8. **G1-B**（无 sid）：`session.error` 无 sessionID → 立即推 `event: session.error` 帧 `{directory?,name,message,at}`（不走 debounce）。
9. **abort 过滤**：`error.name=="MessageAbortedError"` → 静默丢弃（不写 lastError、不发 B 帧）。
10. **clear**：该 session 出现新 `status=busy` → 显式 `lastError:null` digest 帧；`deleted=true` 后不保留 lastError。
11. **脱敏**：`message` 经「首行→剥绝对路径→剥 stack frame→剥 secret→截断 ≤512」算法；缺失回落 `name` 或 `"(no detail)"`；golden 测试覆盖 Unix/Win 路径、`at file:line:col`、JSON secret、多行堆栈。
12. **契约同步**：contract §3 digest 字段加 `lastError?`；新增 `session.error` 帧定义。

### G6（批量展开）
13. **新端点**：`GET /slimapi/messages/{sid}/full?ids=m1,m2,...`（1–20 mid，逗号分隔，去重保序）。
14. **discover 先行**：`GET /session/{sid}`（带 directory 头）→ 404→404 `session_not_found`（不拉任何 mid）；5xx/timeout→503；其它 4xx→502。
15. **mid 级 envelope**：mid 2xx→`items[]`；mid 404→`errors[] message_not_found`；mid >`max_message_bytes`→`errors[] message_too_large`；累计 >`max_response_bytes`→413 `response_too_large` 中止后续；mid `httpx.RequestError`→整请求 503。**全 mid 404 仍 200+全 errors**。
16. **定序**：`items[]` 严格按 `ids` 去重后顺序（并发回填后重排）。
17. **参数校验**：`ids` 缺失→422（FastAPI）；空/超 20/解析失败→400 `invalid_ids`；不校验 mid 字符集。
18. **路由顺序**：`@router.get("/full")` 注册先于 `@router.get("/full/{mid}")`（impl-spec §8 MUST）。
19. **契约同步**：contract §2 端点表加 G6 行；§7 错误码加 `invalid_ids`/`message_not_found`（envelope）。

### 文档与回归
20. **D1–D8**：见 §13 清单全修。
21. **回归**：`./scripts/check.sh` EXIT=0、FAILURES=0；既有测试不破坏（仅本 spec 明确改语义的测试同步改）。

---

## 3. 发现准确性核验

### F1–F5（ocdroid 报告，已对照源码）

| 发现 | 源码证据 | 结论 |
|---|---|---|
| F1 | `questions.py:70,75` `directory: list[str] = Query(...)` 必填；null→422 | 属实 |
| F2 | `sessions.py:74` list `directory: str\|None=None`（null 不过滤）；`sessions.py:162` status 走 `require_directory`；messages soft 对同 sid 200 | 属实 |
| F3 | `questions.py:102` `_token` 直接查 allowlist 不刷新；impl-status.md:237 诚实声明 | 属实 |
| F4 | `CLIENT_CHANGES.md:57` 写 `?directory=...&sessionId=...`；INTERFACE_MAP §3「参数完全移除」 | 属实 |
| F5 | `versioning.py:9` `ACCEPTED_CLIENT_VERSIONS=(1,1)`；body `accepted:[1,1]` 闭区间 | 属实（非 bug） |

### G1 / G6（本仓审计，已对照源码 + opencode 源码）

| 项 | 证据 | 结论 |
|---|---|---|
| **G1 未实现** | `grep lastError\|session.error` 在 `src/` 零命中；hub.py `publish()` catch-all 丢弃 `session.error`（design-v2 §1.10 line 110 显式列丢弃）；contract §3 digest 无 `lastError`；impl-status 不审计 G1 | impl-spec §7 承诺未兑现，B0 决策未记录 |
| **G1 可行性（B0 GO）** | opencode `schema/src/v1/session.ts:651-657` 定义 `session.error`；`event-v2-bridge.ts:35-44` 桥接 GlobalBus；`handlers/global.ts:36-52` SSE 下推；`plugin/index.ts:136`+`skill/index.ts:114` 无 sid 实发；`processor.ts:648-655`+`message-v2.ts:608-614` abort 实发 name=`MessageAbortedError`；TUI `app.tsx:1021` 同 name 过滤 | **GO**（exp-1 验证） |
| **G6 未实现** | messages.py 仅有 `/since/{ts}` / `""` / `/full/{mid}` 三路由，无 `/full` batch；ocdroid 调该路径→404 `thin_route_not_found`→N 并行回退（报告 §3.1 row 11「✓」具误导） | impl-spec §8 承诺未兑现 |

### D1–D8（本仓文档审计）

| # | 位置 | 问题 |
|---|---|---|
| D1 | `design-v2.md §1.7` | q/p `directory`「repeated，去重 ≤32」隐含必填 → 随 F1 改可选 |
| D2 | `design-v2.md §1.9` | per-session status「fan-out 失败→503」→ 同步 B1 三态 + F2 放宽 |
| D3 | `design-v2.md §1.4` | `limit 0→400` → 实际 FastAPI `ge=1` → 422 |
| D4 | `design-v2.md §3 line 160` | SSEClient「`/global/event`→`/event` 裸帧归一化」→ v2 已废，改 `/slimapi/events` |
| D5 | `design-v2.md §3 line 162` | 引用 `thin.session.dirty` 事件 → 实际帧是 `session.digest` |
| D6 | `v1-contract.md §11` | 5 项待补缺口仍带 🆕 / 「驱动 lane 派发」措辞 → impl-status 已全 ✅，标 closed |
| D7 | `v1-impl-spec.md §1` | B0/B2/B4 批次顺序含「B0 决策」pending → 记录 B0 实际结局（GO） |
| D8 | `AGENTS.md` | 称 `current→v1.17.20` 但实链 `v1.18.3`（exp-1 发现）；impl-spec.md:68 已引 v1.18.3 → 对齐 |

---

## 4. 设计决策

### 4.1 版本双轨分析（不 bump wire API 版本）

| 变更 | 类型 | wire 影响 | bump？ |
|---|---|---|---|
| F1 null directory | 加性（422→200） | 是 | **否** |
| F2 status 放宽 | 加性（400→200） | 是 | **否** |
| F3 `_token` 刷新 + 启动 warm-up | 内部鲁棒性 | 无新失败模式 | **否** |
| G1 `digest.lastError` 字段 | 加性（新可选字段；不读它的客户端不受影响） | 是（加性） | **否** |
| G1 `event: session.error` 帧 | 加性（新 SSE event；不处理它的客户端忽略即可） | 是（加性） | **否** |
| G6 `GET .../full?ids=` 新端点 | 加性（新端点；ocdroid 现走 404 fallback，升级后首调 200） | 是（加性） | **否** |
| G6 `invalid_ids` / `message_not_found` code | 加性（新 code） | 是（加性） | **否** |
| F4/F5/§5/D1–D8 文档 | 文档 | 无 | **否** |

依据：契约 §1「仅破坏性变更 bump；加性同版本」+ AGENTS.md「版本双轨」。所有变更使 sidecar 更宽松/更鲁棒/更完整，ocdroid 现有代码不因升级而坏。CHANGELOG 记 `[Unreleased]` 加性条目。

### 4.2 F2 放宽的安全依据

allowlist **非安全边界**（INTERFACE_MAP §7 G7-soft 明示；隔离靠 stunnel mTLS + 网络边界）。per-session status 是读操作，sid 是能力凭证（客户端仅从 list/SSE/routeToken 合法渠道获知 sid）。放宽与 `/messages/{sid}`（G7-soft）对齐。

### 4.3 G1 三态 sentinel（exp-2 关键决策）

`DigestFields` 现有「`None`=省略」约定无法承载 lastError 的三态（未变化/显式 null 清除/对象）。引入模块级 `_UNSET = object()`：
- `_UNSET`（默认）→ `to_payload` 省略 `lastError`
- `None` → 输出 `"lastError": null`（显式 clear 帧）
- `dict` → 输出对象 `{name, message, at?}`

### 4.4 G1 sticky 跨窗口（exp-2 关键决策）

`flush()` 现 `snapshot, self.pending = self.pending, {}`（hub.py:278）整体清空，与 lastError 跨窗口 sticky 不兼容。**新增独立持久层** `self.sticky_last_error: dict[sid, dict | None]`：flush 后按持久层在新 `pending` 中预置 `DigestFields(last_error=...)`；deleted 时 pop。不动 archived/deleted 既有 sticky 逻辑。

### 4.5 G1 sid 提取（exp-2 + exp-1）

`_extract_session_id()`（hub.py:414）对 session.error 不可靠（error 结构特殊）。G1 分支显式从 `props.sessionID`（即 `payload.properties.sessionID`，exp-1 确认 schema）直接取，不依赖通用 helper。

### 4.6 G1 脱敏算法（impl-spec §7 硬约束 4）

纯函数 `_sanitize_error_message(message: str | None, fallback_name: str) -> str`：
1. `message` 缺失/空 → 返回 `fallback_name` 或 `"(no detail)"`。
2. 取首行（`split("\n")[0]`）。
3. regex 剥 Unix 绝对路径（`/[\w./\-]+`）+ Windows（`[A-Za-z]:[\\/][\w\\./\-]+`）→ `<path>`。
4. regex 剥 stack frame（`\bat\b\s+\S+:\d+:\d+`）→ 移除。
5. regex 剥 secret（`(?:token|key|bearer|password|secret)["'\s:=]+[\w\-./=]+`，case-insensitive）→ `<redacted>`。
6. 截断 ≤512 字符。

### 4.7 G6 admission 策略（exp-3 关键决策）

- **full 模式（默认）**：**不进 pool admission**（对齐 `/full/{mid}` full 先例 messages.py:445–479），仅局部 `asyncio.Semaphore(4)` 限 mid 并发 + 共享 `total_bytes` 计数。
- **skeleton 模式**：单 pool admission 包整批（仿 `messages_since` L260–355），admission 内 `Semaphore(4)` 并发拉 mid body，每 mid `pool.offload(skeleton_message)`。
- `TransformBusy` 外层统一 `_busy_response`（既有模式）。

### 4.8 G6 失败语义边界

- discover 404→404 `session_not_found`，**不拉任何 mid**（impl-spec §8 L266）。
- mid 404→`errors[] message_not_found`（**非**透传 404）。
- mid >`max_message_bytes`→`errors[] message_too_large`（**不**整请求 413）。
- 累计 >`max_response_bytes`→整请求 413 `response_too_large` 中止后续 mid。
- mid `httpx.RequestError`→整请求 503 `upstream_unavailable`。
- **全 mid 404 仍 200 + 全 errors**（impl-spec §8 L257）。

---

## 5. 文件结构（改/建清单）

### 5.1 代码（`src/oc_slimapi/`）

| 文件 | 改动 | 项 |
|---|---|---|
| `routes/sessions.py` | (a) 提取 `normalize_directory(d)`；(b) `require_directory` 复用；(c) `load_products(request)`→`load_products(app)` + caller；(d) 新增 `warm_allowlist(app)`；(e) per-session status 用 `normalize_directory` 替代 `require_directory` | F2+F3 |
| `routes/questions.py` | (a) `/questions`+`/permissions` 入参 `Query(None)`；(b) `_aggregate` null 路径；(c) `_token` async + `require_directory` + caller await | F1+F3 |
| `app.py` | `lifespan` 加 `await sessions.warm_allowlist(app)`（smoke 后） | F3 |
| `sse/hub.py` | (a) 模块级 `_UNSET` sentinel + `_sanitize_error_message()`；(b) `DigestFields.last_error` 三态 + `to_payload`；(c) `GlobalHub.sticky_last_error` 持久层；(d) `publish()` 加 `session.error` 分派（abort 过滤 / sid 显式取 / G1-A 立即 flush / G1-B IMMEDIATE 直推带 event 头）；(e) `flush()` 保留 sticky；(f) `session.status busy` 触发 clear；(g) `session.deleted` 清 lastError | G1 |
| `routes/messages.py` | 新增 `@router.get("/full")`（插 L435–436 间，先于 `/full/{mid}`）：`message_batch` handler（directory 解析→discover 先行→`Semaphore(4)` 并发 mid→envelope 重组） | G6 |

### 5.2 文档（`docs/` + `AGENTS.md`）

| 文件 | 改动 | 项 |
|---|---|---|
| `v1-contract.md` | 头部 changelog +2026-07-19；§1 `[1,1]` 闭区间（F5）；§2 端点表（q/p 可选 / status 自洽 / **G6 新端点**）；§3 digest 加 `lastError?` + 新增 `session.error` 帧定义（G1）；§4 cold-start 含暖机；§7 错误码加 `invalid_ids`/`message_not_found`（G6）+ `directory_not_allowed` 适用范围（F2）；新增 §「directory 三态语义表」+ §「allowlist 机制」（§5）；§11 标 closed（D6）；CLIENT_CHANGES 同步纪律 | F1/F2/F3/F5/§5/G1/G6/D6 |
| `CLIENT_CHANGES.md` | SSE 节重写（F4）；新增 G1 `lastError`/`session.error` 客户端解析说明 + G6 batch 端点说明 | F4/G1/G6 |
| `INTERFACE_MAP.md` | §0 `require_directory` + `normalize_directory`；§1 表 q/p / status / **G6 行**；§3 SSE 加 `session.error` 帧；§7 G2 status allowlist 行（F2） | F1/F2/F3/G1/G6 |
| `v1-contract-implementation-status.md` | §2 表 q/p/status/G6 行；§3 digest `lastError`；「诚实声明」routeToken 改已修复；新增 G1/G6 落地条目 | F1/F2/F3/G1/G6 |
| `design-v2.md` | §1.4 limit 422（D3）；§1.7 q/p 可选（D1）；§1.9 status B1+F2（D2）；§1.10 丢弃列表删 `session.error`（G1）；§3 SSEClient `/slimapi/events`（D4）+ 删 `thin.session.dirty`（D5）；新增 G6 §1.x | D1–D5/G1/G6 |
| `v1-impl-spec.md` | §1 B0 决策记录 GO（D7）；§7 G1 标已实现；§8 G6 标已实现；§11 `invalid_ids`/`message_not_found` 状态更新 | D7/G1/G6 |
| `CHANGELOG.md` | `[Unreleased]` Added（F1 null / F3 warm-up / **G1 lastError+session.error** / **G6 batch 端点**）/ Changed（F2 status / F3 routeToken 刷新）/ Fixed（F4/F5/§5/D1–D8 文档） | 全部 |
| `AGENTS.md` | 「当前对齐版本」改 `v1.18.3`（D8） | D8 |

### 5.3 测试（`tests/`）

| 文件 | 新增/改测试 | 项 |
|---|---|---|
| `test_sessions_routes.py` | `load_products(app)` / `warm_allowlist` 吞错（T1）；per-session status 放宽 200（T4，改写 `test_status_allowlist_miss_returns_400`） | F2/F3 |
| `test_questions_routes.py` | null 聚合 + 空 envelope（T3）；cold allowlist routeToken 刷新成功（T2） | F1/F3 |
| `test_hub.py` | **改** `test_message_part_delta_produces_no_frames`（删 session.error 丢弃断言）；新增 G1-A 立即 flush / G1-B session-less 帧 / sticky 跨窗口 / clear-on-busy / abort 过滤 / deleted 清除 / 脱敏 golden | G1 |
| `test_messages_routes.py` | G6：ids 缺失 422 / 空·超 20 →400 / 整 session 404（discover 先行+0 mid 调用）/ mid 部分失败 200+errors / 全 mid 404 仍 200 / 累计 413（calls count 锁）/ 路由顺序 / items 定序 | G6 |

---

## 6. 关键设计细节

### 6.1 F1 `_aggregate` null 路径

```python
async def _aggregate(request, kind, directories: list[str] | None):
    if directories is not None:
        unique = list(dict.fromkeys(directories))
        if not unique or len(unique) > 32:
            raise CodedHTTPException(400, code="invalid_directory_count")
        checked = [await require_directory(request, d) for d in unique]
    else:
        allowlist = request.app.state.directory_allowlist
        if not allowlist:
            try: await load_projects(request.app)
            except Exception: pass
            allowlist = request.app.state.directory_allowlist
        checked = sorted(allowlist)  # 可能 []；null 路径不受 1–32 守卫约束
    # fan-out / envelope 不变；checked==[] → items/errors==[]，status=200
    ...
    status = 503 if results and len(errors) == len(results) else 200
```

### 6.2 F2 per-session status 放宽

```python
def normalize_directory(directory: str) -> str:
    return directory.rstrip("/") or "/"

async def require_directory(request, directory):
    normalized = normalize_directory(directory)
    ...  # 既有 allowlist 逻辑不变

# session_status（原 require_directory 调用）
directory = normalize_directory(directory)  # 仅规范化，不 gate
```

### 6.3 F3 启动 warm-up + `_token` 刷新

```python
# sessions.py
async def warm_allowlist(app):
    try: await load_products(app)
    except Exception: pass

# app.py lifespan（smoke 后）
await sessions.warm_allowlist(app)

# questions.py
async def _token(request, token, kind, request_id, session_id=None) -> str:
    try: payload = verify_route_token(...)
    except RouteTokenError as exc: raise CodedHTTPException(400, code="invalid_route_token") from exc
    return await require_directory(request, payload["directory"])  # miss 自动刷新
# reply/reject/permission: await _token(...)
```

### 6.4 G1 hub.py 改动骨架（exp-2 插点）

```python
# hub.py 模块级
_UNSET = object()
ABORT_NAME = "MessageAbortedError"

def _sanitize_error_message(message, fallback_name): ...  # §4.6 算法

@dataclass
class DigestFields:
    directory: str | None = None
    status: str | None = None
    message_id: str | None = None
    updated_at: Any = None
    archived: int | None = None
    deleted: bool = False
    last_error: Any = _UNSET  # 三态：_UNSET/None/dict
    def to_payload(self, sid):
        payload = {"sessionID": sid}
        ...  # 既有字段
        if self.last_error is not _UNSET:
            payload["lastError"] = self.last_error  # None→null, dict→对象
        return payload

class GlobalHub:
    def __init__(self, ...):
        ...
        self.sticky_last_error: dict[str, dict | None] = {}  # 跨窗口持久层

    def flush(self):
        if not self.pending: return
        snapshot, self.pending = self.pending, {}
        # 保留 sticky：为仍有未清 lastError 的 sid 预置新 DigestFields
        for sid, prev in list(self.sticky_last_error.items()):
            if prev is not None and sid not in self.pending:
                self.pending[sid] = DigestFields(last_error=prev)
        for sid, fields in snapshot.items():
            frame = sse_frame(fields.to_payload(sid), event="session.digest")
            for sub in tuple(self.subscribers): sub.put(frame)
            ...

    # publish() 新增分支（MESSAGE_EVENTS return 之后、catch-all 之前）
    elif event_type == "session.error":
        err = props.get("error") or {}
        name = err.get("name")
        if name == ABORT_NAME:
            return  # abort 静默丢
        raw_msg = (err.get("data") or {}).get("message")
        message = _sanitize_error_message(raw_msg, name or "(no detail)")
        at = int(asyncio.get_event_loop().time() * 1000)  # 或事件到达时间
        sid = props.get("sessionID")  # 显式取，不走 _extract_session_id
        if isinstance(sid, str) and sid:
            entry = self.pending.setdefault(sid, DigestFields())
            if entry.deleted:
                return
            entry.last_error = {"name": (name or "")[:128], "message": message, "at": at}
            self.sticky_last_error[sid] = entry.last_error
            self.flush()  # G1-A 立即触发
        else:
            # G1-B session-less 立即直推
            frame_payload = {"name": (name or "")[:128], "message": message, "at": at}
            if directory: frame_payload["directory"] = directory
            frame = sse_frame(frame_payload, event="session.error")
            for sub in tuple(self.subscribers): sub.put(frame)
        return

    # session.status 分支内追加 clear 触发
    # if event_type == "session.status" and props.get("status") == "busy":
    #     if sid in self.sticky_last_error and self.sticky_last_error[sid] is not None:
    #         self.sticky_last_error[sid] = None
    #         entry = self.pending.setdefault(sid, DigestFields())
    #         entry.last_error = None  # 显式 null → clear 帧
    #         self.flush()

    # session.deleted 分支内追加清除
    # self.sticky_last_error.pop(sid, None)
    # entry.last_error = _UNSET  # deleted digest 不带 lastError
```

### 6.5 G6 messages.py 改动骨架（exp-3 插点）

```python
# 插在 messages.py L435–436 间（先于 /full/{mid}）
@router.get("/full")
async def message_batch(
    request: Request, sid: str,
    ids: str,  # FastAPI 绑定；缺失→422
    mode: Literal["skeleton", "full"] = "full",
    directory: str | None = None,
):
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    # ids 解析：split + strip + dedupe 保序 + 1–20 守卫
    order = list(dict.fromkeys(s.strip() for s in ids.split(",") if s.strip()))
    if not order or len(order) > 20:
        raise CodedHTTPException(400, code="invalid_ids")
    config = request.app.state.config
    # discover 先行（带 directory 头，spec §8 L266）
    try:
        resp = await request.app.state.upstream.get(
            f"/session/{sid}", headers=forward_directory_headers(directory),
        )
    except httpx.RequestError:
        raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc, sid=sid)  # 404→session_not_found，不再拉 mid
    # 并发拉 mid
    sem = asyncio.Semaphore(4)
    total = 0
    succeeded: dict[str, dict] = {}
    errors: list[dict] = []
    aborted = False

    async def fetch_one(mid: str):
        nonlocal total, aborted
        if aborted: return
        async with sem:
            upstream_request = request.app.state.upstream.build_request(
                "GET", f"/session/{sid}/message/{mid}",
                headers=forward_directory_headers(directory),
            )
            response = await request.app.state.upstream.send(upstream_request, stream=True)
            try:
                if response.status_code == 404:
                    errors.append({"messageID": mid, "code": "message_not_found"}); return
                if response.status_code >= 400:
                    errors.append({"messageID": mid, "code": f"upstream_http_{response.status_code}"}); return
                body, n = await read_with_cap(response, min(
                    config.max_message_bytes,
                    config.max_response_bytes - total,
                ))
            except httpx.RequestError:
                aborted = True; errors.clear()
                raise CodedHTTPException(503, code="upstream_unavailable")
            finally:
                await response.aclose()
        if body is None:
            # 区分单 mid 超限 vs 累计超限
            if n > config.max_message_bytes:
                errors.append({"messageID": mid, "code": "message_too_large"}); return
            aborted = True; errors.clear()
            return error_response("response_too_large", 413, limit=config.max_response_bytes,
                                  accept_encoding=request.headers.get("accept-encoding"))
        total += n
        if mode == "skeleton":
            succeeded[mid] = await request.app.state.transforms.offload(
                lambda b=body: skeleton_message(orjson.loads(b)))
        else:
            succeeded[mid] = orjson.loads(body)

    try:
        if mode == "skeleton":
            async with request.app.state.transforms:
                await asyncio.gather(*(fetch_one(mid) for mid in order))
        else:
            await asyncio.gather(*(fetch_one(mid) for mid in order))
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))

    items = [succeeded[mid] for mid in order if mid in succeeded]
    return json_response({"items": items, "errors": errors},
                         headers={"Cache-Control": "no-store"},
                         accept_encoding=request.headers.get("accept-encoding"))
```

（实现细节由 plan 的 TDD 步骤收敛；上面是契约骨架。）

---

## 7. 测试方法（TDD，每 task 先红后绿）

| 测试 | 验证命令 |
|---|---|
| 全量 | `./scripts/check.sh`（= `pytest tests/`） |
| 定向 | `.venv/bin/pytest tests/test_<module>.py -v` |

G1 脱敏 golden、G6 累计 413 calls-count 锁、G1 sticky 跨窗口 是最易出错的测试点，plan 中单列。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| G1 三态 sentinel 与 archived/deleted sticky 逻辑纠缠 | 用独立 `sticky_last_error` 持久层（§4.4），不动既有 sticky 字段；golden 测试覆盖 |
| G1 `_extract_session_id` 对 session.error 失效 | 显式取 `props.sessionID`（§4.5，exp-1 确认 schema 路径） |
| G1 立即 flush 在 `publish()` 同步上下文调用 | `publish()` 与 `flush()` 均同步（exp-2 确认），直接调；与既有 debounce 路径一致 |
| G6 路由顺序被 `/full/{mid}` 吞 | 段数不同本不冲突（exp-3 确认），但仍按 spec MUST 注册在先；加路由顺序测试 |
| G6 discover 与 mid 404 语义混淆 | discover 404→整 404 session_not_found（不拉 mid）；mid 404→envelope errors[]；测试用 calls 计数器证明 discover 404 时 0 mid 调用 |
| G6 累计预算在 N 并发下的计数正确性 | 共享 `total` 在 `Semaphore(4)` 临界段外累加可能欠计；plan 中 TDD 用低 `max_response_bytes` 压测 |
| F1 null 聚合 fan-out 重 | allowlist 典型 ≤ 个位数；null 不受 1–32 守卫（文档写明） |
| F3 启动 warm-up 阻断启动 | 失败仅 log，lazy 刷新回退 |
| 破坏性变更误判→漏 bump | §4.1 逐项分析全加性；final review 复核 |
| 文档大量改动交叉引用断裂 | 文档 task 在代码 task 之后（反映实际行为）；reviewer 人工核 |

---

## 9. 出口标准（definition of done）

- F1–F5 + §5 + G1 + G6 + D1–D8 全部落地，§5 文件清单全覆盖。
- `./scripts/check.sh` EXIT=0、FAILURES=0。
- 独立 verifier live rerun 通过；final code-reviewer verdict=pass。
- CHANGELOG `[Unreleased]` 条目完整；wire API 版本仍为 1（未 bump）。
- 不 commit（除非用户显式要求）。

---

## 10. 后续（非本 spec）

- 用户的下一步选项：`./scripts/release.sh patch` 发版（折叠 `[Unreleased]`）。
- ocdroid 侧动作（仅文档记录）：cold-start 顺序对齐、KDoc 更正（q/p null 语义）、解析 G1 `lastError`/`session.error`、G6 batch 端点对接（去掉 404 fallback）。
- G3-B / G7-strict / durable SSE replay 等 v2 项不动。
