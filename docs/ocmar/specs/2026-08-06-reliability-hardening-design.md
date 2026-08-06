# Spec: reliability-hardening (2026-08-06)

> **来源**：oracle 架构评审（ses_02a89bb16ffesnnOx9JXPQbuzC）P0 全部 + P1 快速三项。
> **风险层级**：Standard（聚焦 bug 修复 + 文档同步 + 死代码清理；无 schema/权限/生产配置 schema 变更；catch-all 错误映射属加性 ops 面修复，非破坏性 wire 变更）。
> **不做契约 bump**（`X-Slimapi-Version` 不动）：所有改动都是补齐已声明行为或修裸 500，无 client 依赖现有 500。

---

## 1. 原始需求

oracle 评审发现两类已验证缺陷 + 一组文档语义漂移 + 两处轻量一致性缺口，用户选定范围 = **P0 三项 + P1 快速三项**。

一句话目标：**消除两处已复现的裸 500、同步文档与代码、清理死代码、补齐内存防线一致性。**

---

## 2. 范围（6 项，逐条：现状证据 → 目标 → 验证）

### T1 · messages 列表端点非 list body → 裸 500 〔P0，bugfix〕

**现状证据**：
- `src/oc_slimapi/routes/messages.py:61-81` `_project_list_sorted_and_pack`：第 73 行 `if isinstance(parsed, list): parsed.sort(...)` 只 gate sort；第 75 行 `skeleton_messages(parsed)` **无条件执行**。
- 上游返回 dict / None / `[1,2]`（标量 list）时：`skeleton_messages` 对非 dict 元素 → `AttributeError: 'str' object has no attribute 'get'`（已实测复现，oracle 报告）。
- 路由层 `messages.py:313-321` 只 catch `orjson.JSONDecodeError`，`AttributeError`/`TypeError` 穿透成 FastAPI 裸 500。
- **对比**：兄弟端点 `sessions.py:63-70`、`agent.py`、`questions.py`、`command.py` 全部有 `isinstance` 守卫 + 503 映射 + 回归测试，唯独此核心读端点缺失。

**目标**：上游 body 解析成功但非 list（dict/None/scalar/scalar-list）→ `503 upstream_unavailable`（与 `JSONDecodeError` 同 code，对齐 §7 契约精神：畸形 body = 上游不可用）。

**实现位置**：`_project_list_sorted_and_pack` 内（worker 线程），抛 `ValueError`；路由层把 `ValueError` 一并映射 503（与现有 `JSONDecodeError` catch 合并）。这样守卫与数据接触点同处，不污染路由层。

**验证**：
- 回归测试 `test_messages_routes.py`：dict / None / `[1,2,"x"]` 三例 → 断言 503 + `code=upstream_unavailable`。
- 既有 list body 用例全绿（不回归）。

---

### T2 · catch-all 反代上游异常 → 裸 500 〔P0，bugfix，记 CHANGELOG〕

**现状证据**：
- `src/oc_slimapi/proxy.py:197` `response = await client.send(upstream_request, stream=True)` 无 try/except。
- `httpx.ConnectError` / `ReadTimeout` / `RemoteProtocolError` 等穿透成 FastAPI 裸 500（已用 MockTransport 实测复现，oracle 报告）。
- 所有**写路径**（发消息 / abort / q-p 应答 / prompt / prompt_async）走 catch-all，客户端无法区分"opencode 挂了"与"sidecar bug"。
- **对比**：thin 路由（sessions/messages/agent/command/questions）统一 `httpx.RequestError → 503 upstream_unavailable`，catch-all 是唯一缺口。
- INTERFACE_MAP line 55 已诚实自认「异常没有统一映射，可能成为 500」。

**目标**：catch-all `client.send` 抛 `httpx.RequestError`（含 `ConnectError`/`ReadTimeout`/`PoolTimeout` 等）→ `503 upstream_unavailable`（与 thin 路由同 code）。

**实现位置**：`proxy.py` catch-all handler，在 `client.send` 外包 `try/except httpx.RequestError`。注意 turn-fence 逻辑（`proxy.py:178-196`）在 send **之前**已 bump，send 失败产生的 turn hole 由 ocdroid lex 容忍（代码注释 `proxy.py:184-187` 已说明），本改动不触碰 fence 语义。

**timeout 不动**（用户已确认）：`proxy.py:172-177` per-request timeout（SSE=None / command=300s / 其他=30s）保持不变。ocdroid 生产走 prompt_async 已规避同步路径的 30s 紧约束。

**验证**：
- 回归测试 `test_proxy.py`（或对应 catch-all 测试文件）：MockTransport 抛 `ConnectError` / `ReadTimeout` → 断言 503 + `code=upstream_unavailable`。
- 既有正常透传、SSE 流式、command 300s 用例全绿。
- CHANGELOG.md 记一条：catch-all 上游网络异常从裸 500 改为 503 `upstream_unavailable`（加性，不 bump）。

---

### T3 · 修三处文档语义漂移 + 路由清单补 questions 〔P0，doc-sync〕

**现状证据**（以代码为准）：

| # | 文档 | 现表述 | 代码实际 |
|---|---|---|---|
| 3a | `INTERFACE_MAP.md` §3 | `message.part.removed`（flat props）→ digest 更新 | v2 契约 §3 line 146 明确"不再触发 digest"；`global_hub.py:605-628` 只路由 token hub，不碰 digest |
| 3b | `INTERFACE_MAP.md` §4（ready 行） | `schema_degraded` 让 messages 路由自动降级 full | messages 路由**从不读** `schema_degraded`；v2 恒 skeleton。`schema_degraded` 仅由 `app.py` smoke 写入、`health.py` 回显 |
| 3c | `design-v2.md` §1.5 | "启动字段漂移 smoke 已移除" | `app.py:35-56` `smoke()` 仍在运行消息字段校验并设 `schema_degraded` |
| 3d | `INTERFACE_MAP.md` §0 | 路由注册清单漏 `questions` | `app.py:327` 注册了 `questions.router` |

**目标**：以代码为唯一真相源统一四处表述：
- 3a：删/改正 `message.part.removed → digest` 描述，明确 v2 不触发 digest。
- 3b：`schema_degraded` 在 ready 行改为"仅诊断回显，不触发自动降级"。
- 3c：design-v2 §1.5 改为"smoke 保留，运行消息字段校验，异常时设 `schema_degraded` 供 health/ready 回显"。
- 3d：§0 注册清单补 questions（health → agent → command → questions → sessions → messages → events → metrics → token_stream）。

**验证**：
- `./scripts/check.sh` 通过（含 `check_routes_doc.py` 路由存在性校验）。
- 人工 diff 四处表述与代码一致。

---

### T4 · 删 capabilities.py 死代码 〔P1，cleanup〕

**现状证据**：
- `src/oc_slimapi/capabilities.py`（125 行）+ `tests/test_capabilities.py` 仍在仓内。
- Opt-A 已随 v2 删除（契约 §1 line 40：`X-Slimapi-Capabilities` 忽略）；全 src 无任何引用（oracle grep 仅自引用）。

**目标**：删除 `capabilities.py` + `test_capabilities.py`。

**验证**：`./scripts/check.sh` 通过；无 import 残留。

---

### T5 · sessions list 接 read_with_cap 〔P1，robustness〕

**现状证据**：
- `src/oc_slimapi/routes/sessions.py:42-44` 注释自认「Known limitation vs messages: the single-response body is not yet bounded by read_with_cap (no 413 on oversize)」。
- `sessions.py:48` 用 `request.app.state.upstream.get(...)`（非流式，`response.content` 全 buffer）；上游 `/session?limit=1000` body 无上限。
- **对比**：`messages.py:295`、`agent.py:118`、`command.py:128` 全部用流式 `client.stream` + `read_with_cap(response, config.max_response_bytes)`，超限 → 413 `response_too_large`。

**目标**：sessions list 改为流式 GET + `read_with_cap(config.max_response_bytes)`，超限 → 413 `response_too_large`（与三个兄弟端点一致）。cap 用同一配置项 `max_response_bytes`（默认 64MiB，`config.py:157`）。

**实现注意**：
- 改成 `client.stream("GET", ...)` 后，`isinstance(payload, list)` 守卫（`sessions.py:63-70`）、`raise_for_status`、`httpx.RequestError → 503`、`stash_up_in` 计数都要适配流式（计数改用 `read_with_cap` 返回的 `n_read`，参考 `messages.py:295-303`）。
- 413 与 503 路径都要保证 `response.aclose()`（参考 messages 的 try/finally）。

**验证**：
- 回归测试：上游 body > cap → 413 `response_too_large`。
- 既有 sessions list 正常用例（含非 list 守卫用例）全绿。

---

### T6 · deploy unit 补 MemoryMax 〔P1，ops-consistency〕

**现状证据**：
- `deploy/oc-slimapi.service` 全文无 `MemoryMax`。
- `design-v2.md` §0.10 与 `transform.py:10` docstring 都引用 `MemoryMax=384M`。
- `app.py` 的 max_transforms=1 + max_response_bytes=64MiB + 16MiB inline cap 的内存算术与 384M 自洽。

**目标**：`deploy/oc-slimapi.service` `[Service]` 段补 `MemoryMax=384M`（并按需 `MemoryHigh=256M` 作为软限，可选项，实现时确认 systemd user service 支持）。

**验证**：人工核对 service 模板；docs/operations.md 若有相关部署步骤则同步。

---

## 3. 成功标准（全部满足才算交付）

1. `./scripts/check.sh` 通过（pytest 全绿 + 路由↔文档存在性校验）。
2. T1/T2/T5 各自的新增回归测试存在且通过。
3. T3 四处文档表述与代码逐条一致（人工 diff）。
4. T4 死代码无残留 import。
5. T6 deploy unit 含 MemoryMax。
6. CHANGELOG.md 记 T2 的 wire 行为变更（加性，不 bump 版本）。
7. oracle 评审列出的"关键风险 Top 5"中 #1（catch-all 500）与 #2（messages 500）在本批次后**消失**（#3 文档漂移、#4 认证面、#5 上游 pin 不在本批次范围，#4/#5 属已接受/P2）。

## 4. 不做什么（YAGNI 边界）

- **不 bump `X-Slimapi-Version`**：无破坏性 wire 变更。
- **不动 catch-all timeout**：用户已确认（ocdroid 走 async 规避）。
- **不加应用层鉴权**：单用户 sidecar，违背 YAGNI。
- **不加 GitHub Actions CI**：属 P1 但非"快速"项，本批次不做。
- **不加上游契约 pin 测试**：属 P1 较大独立子项，本批次不做。
- **不动 tokenstream/hub.py**：P2 拆分评审，不在本批次。
- **不改契约 §6 单帧超限语义**：需正式契约修订日志，本批次不做。
- **不加运维核查清单**：P1 独立项，本批次不做。

## 5. 风险

| 风险 | 缓解 |
|---|---|
| T2 包 `httpx.RequestError` 后，SSE 流式响应中途断开被误映射 503 | `client.send` 的异常只发生在**建立连接/发请求**阶段；响应已建立后的 mid-stream 断开由 `_counted_upstream_response` 的 finally 处理（不经过 send 的 except）。实现时确认 except 边界仅在 `send()` 调用 |
| T5 改流式后破坏 sessions list 既有完成度 header（`X-Complete` 等） | 实现时保留 `sessions.py:79+` 的 completeness header 逻辑，只替换 body 读取方式 |
| T3 文档改动引入新的不一致 | 改后逐条与代码核对；plan 阶段 grill 会复查 |
| T4 删除后 check.sh 因 INTERFACE_MAP 引用 capabilities 失败 | 删前 grep 文档/代码确认无引用；oracle 已确认仅自引用 |

## 6. 可审计引用

- oracle 评审会话：`ses_02a89bb16ffesnnOx9JXPQbuzC`（ora-1，可复用作 task-reviewer）
- 契约权威：`docs/specs/v2-contract.md`
- 上一版发版规范：`docs/release.md`
