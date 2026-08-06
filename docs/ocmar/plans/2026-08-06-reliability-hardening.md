# reliability-hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 oracle 评审发现的两处裸 500、同步文档与代码、清理死代码、补齐内存防线一致性（spec P0 三项 + P1 快速三项）。

**Architecture:** 全部为就地修复 + 就地回归测试，无新模块、无 schema 变更、无契约 bump。catch-all 错误映射与 thin 路由对齐同一 `code=upstream_unavailable`；sessions list 流式化复用 messages list 已验证的 `read_with_cap` 范式；文档同步以代码为唯一真相源。

**Tech Stack:** Python 3、FastAPI、httpx (MockTransport + ASGITransport for tests)、pytest、orjson。验证命令：`./scripts/check.sh`（= `pytest tests/` + 路由↔文档存在性校验）。

## Global Constraints

- **不 bump `X-Slimapi-Version`**：所有改动是加性修复或补齐已声明行为，无破坏性 wire 变更（spec §2/§4）。
- **不动 catch-all timeout**：`proxy.py:172-177` per-request timeout（SSE=None / command=300s / 其他=30s）保持不变（用户已确认）。
- **错误码统一**：所有上游不可用路径用 `503` + `code=upstream_unavailable`；body 超限用 `413` + `code=response_too_large`，与三个兄弟端点一致。
- **ocmar 默认不 commit**：每 task 记 diff，除非用户显式要求。
- **测试范式**：`httpx.MockTransport(handler)` + `httpx.ASGITransport(app)`，handler 内 `raise httpx.ConnectError("simulated", request=request)` 模拟网络错误。
- **不触碰 turn-fence 语义**：T2 的 catch-all 包装不影响 `proxy.py:178-196` 的 bump-before-send（send 失败产生的 turn hole 由 ocdroid lex 容忍，见 `proxy.py:184-187` 注释）。

---

## File Structure

| 文件 | 职责 | 本 plan 动作 |
|---|---|---|
| `src/oc_slimapi/routes/messages.py` | skeleton list/full 投影路由 | Task 1 改：worker 加非 list 守卫 + 路由层 catch ValueError |
| `src/oc_slimapi/proxy.py` | catch-all 反代 + shell deny-list | Task 2 改：`client.send` 包 RequestError → 503 |
| `CHANGELOG.md` | 接口行为变更记录 | Task 2 改：记 catch-all 错误映射变更 |
| `src/oc_slimapi/capabilities.py` | Opt-A 能力解析（死代码） | Task 3 删 |
| `tests/test_capabilities.py` | capabilities 单测 | Task 3 删 |
| `src/oc_slimapi/routes/sessions.py` | sessions list/discover 路由 | Task 4 改：list 端点流式化 + read_with_cap |
| `deploy/oc-slimapi.service` | systemd user unit 模板 | Task 5 改：补 MemoryMax |
| `docs/specs/INTERFACE_MAP.md` | 端点级实现追踪 | Task 6 改：4 处语义同步 |
| `docs/specs/design-v2.md` | 设计文档 | Task 6 改：§1.5 smoke 表述 |

写域隔离：6 个 task 的修改文件无重叠（除 Task 6 文档同步引用前 5 个 task 的代码态，故 Task 6 必须最后执行）。

---

## Task 1: messages list + full 非 list/dict body → 503 守卫 〔spec T1，P0，grilling 扩展覆盖 full〕

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py:61-81`（list worker `_project_list_sorted_and_pack`）+ `:313-321`（list 路由层 catch）+ `:418-426`（full 路由层 catch）
- Modify: `src/oc_slimapi/transform.py:88-103`（full worker `strip_diagnostics_and_pack`）
- Test: `tests/test_messages_routes.py`（追加 list 3 例 + full 2 例回归测试）

**Interfaces:**
- Consumes: 无（就地修复）
- Produces: list worker `_project_list_sorted_and_pack` 在非 list body 时抛 `ValueError`；full worker `strip_diagnostics_and_pack`（transform.py）在非 dict body 时抛 `ValueError`；list 与 full 两条路由的 catch 都把 `(orjson.JSONDecodeError, ValueError)` 统一映射 503。

**grilling 背景**：oracle 报告只覆盖 list 端点，但 `full/{mid}` 端点用 `strip_diagnostics_and_pack`（期待 dict，skeleton.py:275 `strip_diagnostics_message(message: dict)`），上游返回 list/null/scalar 时同样 AttributeError → 裸 500（路由层 messages.py:423 只 catch `JSONDecodeError`）。两处同类、同范式，仅守卫方向相反（list 要 list，full 要 dict）。

**Acceptance Criteria:**
- `T1-C1`: `tests/test_messages_routes.py` 新增 `test_messages_list_non_list_dict_body_returns_503` → PASS，断言上游返回 `{"unexpected":"shape"}` 时 `status==503` 且 `code==upstream_unavailable`。
- `T1-C2`: 同文件新增 `test_messages_list_non_list_null_body_returns_503` → PASS，上游返回 `null` 时 `status==503`。
- `T1-C3`: 同文件新增 `test_messages_list_scalar_list_body_returns_503` → PASS，上游返回 `[1,2,"x"]`（标量 list）时 `status==503`。
- `T1-C4`: 既有 `test_skeleton_messages_route_returns_projected_json` 等正常 list/full 用例全绿（无回归）。
- `T1-C5`: `tests/test_messages_routes.py` 新增 `test_message_full_non_dict_list_body_returns_503` → PASS，`GET /slimapi/messages/{sid}/full/{mid}` 上游返回 list（非 dict）时 `status==503` 且 `code==upstream_unavailable`。
- `T1-C6`: 同文件新增 `test_message_full_non_dict_null_body_returns_503` → PASS，上游返回 `null` 时 `status==503`。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_messages_routes.py` 追加（沿用同文件 `upstream_factory` / `_settings` / `_build_app` / `VERSION_HEADERS` 既有 fixture，参考 `test_sessions_routes.py:158-174` 的非 array 范式）：

```python
async def test_messages_list_non_list_dict_body_returns_503(upstream_factory):
    """Upstream 200 but JSON is a dict (not list) → 503 upstream_unavailable.
    Regression: skeleton_messages received a dict → AttributeError → bare 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"unexpected":"shape"}',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_non_list_null_body_returns_503(upstream_factory):
    """Upstream 200 but JSON is null → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"null",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


async def test_messages_list_scalar_list_body_returns_503(upstream_factory):
    """Upstream 200 but JSON is a list of scalars → 503 upstream_unavailable.
    Regression: skeleton_message received a str element → AttributeError → bare 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[1, 2, "x"]',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_routes.py::test_messages_list_non_list_dict_body_returns_503 tests/test_messages_routes.py::test_messages_list_non_list_null_body_returns_503 tests/test_messages_routes.py::test_messages_list_scalar_list_body_returns_503 -v`
Expected: 3 FAIL（裸 500 — `AttributeError: 'str'/'dict' object has no attribute 'get'` 或 `'NoneType'`，FastAPI 默认 500 handler 返回 500 而非 503）。

- [ ] **Step 3: Implement the guard in the worker**

`src/oc_slimapi/routes/messages.py:61-81` 当前：
```python
def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """..."""
    parsed = orjson.loads(body)
    if isinstance(parsed, list):
        parsed.sort(key=_created_sort_key)
    projected = skeleton_messages(parsed)
```
改为（把 `if isinstance... sort` 收紧为 `if not isinstance: raise`，守卫与数据接触点同处）：
```python
def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """..."""
    parsed = orjson.loads(body)
    if not isinstance(parsed, list):
        # Mirrors sessions.py non-list guard: a non-list body (dict/null/
        # scalar-list) would make skeleton_messages iterate wrong types
        # → AttributeError. Treat as malformed upstream → route maps to 503.
        raise ValueError("upstream message body is not a list")
    parsed.sort(key=_created_sort_key)
    projected = skeleton_messages(parsed)
```

- [ ] **Step 4: Extend the route-layer catch to include ValueError**

`src/oc_slimapi/routes/messages.py:318` 当前：
```python
                except orjson.JSONDecodeError as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
```
改为：
```python
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_routes.py::test_messages_list_non_list_dict_body_returns_503 tests/test_messages_routes.py::test_messages_list_non_list_null_body_returns_503 tests/test_messages_routes.py::test_messages_list_scalar_list_body_returns_503 -v`
Expected: 3 PASS。

- [ ] **Step 6: Run full messages test file for regression (list)**

Run: `.venv/bin/pytest tests/test_messages_routes.py -v`
Expected: 全绿（含既有 list 投影、413、503 transform_busy 等用例）。

- [ ] **Step 7: Write failing tests for full/{mid} non-dict body**

在 `tests/test_messages_routes.py` 追加（full 端点路径 `/slimapi/messages/{sid}/full/{mid}`，期待 dict；上游若返回 list/null → `strip_diagnostics_message(dict)` 收到非 dict → 裸 500）：

```python
async def test_message_full_non_dict_list_body_returns_503(upstream_factory):
    """GET /slimapi/messages/{sid}/full/{mid}: upstream returns a list (not a
    single-message dict) → 503 upstream_unavailable.
    Regression: strip_diagnostics_message received a list → AttributeError → bare 500.
    Mirrors the list-endpoint non-list guard (grilling扩展覆盖 full)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[{"info":{"id":"m1"},"parts":[]}]',
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()


async def test_message_full_non_dict_null_body_returns_503(upstream_factory):
    """GET /slimapi/messages/{sid}/full/{mid}: upstream returns null → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"null",
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/messages/s1/full/m1", headers=VERSION_HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_unavailable"
    finally:
        app.state.transforms.shutdown()
```

- [ ] **Step 8: Run full tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages_routes.py::test_message_full_non_dict_list_body_returns_503 tests/test_messages_routes.py::test_message_full_non_dict_null_body_returns_503 -v`
Expected: 2 FAIL（裸 500 — `strip_diagnostics_message` 对 list/null → AttributeError，FastAPI 默认 500 handler）。

- [ ] **Step 9: Implement the guard in the full worker (transform.py)**

`src/oc_slimapi/transform.py:101-103` 当前：
```python
    parsed = orjson.loads(body)
    projected = strip_diagnostics_message(parsed)
    return _pack_json(projected, accept_encoding)
```
改为（`strip_diagnostics_message` 签名 `message: dict`，守卫与数据接触点同处）：
```python
    parsed = orjson.loads(body)
    if not isinstance(parsed, dict):
        # /full/{mid} expects a single message dict. A non-dict body (list/
        # null/scalar) would make strip_diagnostics_message raise
        # AttributeError. Treat as malformed upstream → route maps to 503.
        raise ValueError("upstream single-message body is not a dict")
    projected = strip_diagnostics_message(parsed)
    return _pack_json(projected, accept_encoding)
```

- [ ] **Step 10: Extend the full route-layer catch to include ValueError**

`src/oc_slimapi/routes/messages.py:423` 当前（full/{mid} 路由）：
```python
                except orjson.JSONDecodeError as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
```
改为：
```python
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
```
（注意：此 catch 在 `messages.py:418-426`，与 list 路由的 catch `:313-321` 是不同的 try 块，需分别改。）

- [ ] **Step 11: Run full tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages_routes.py::test_message_full_non_dict_list_body_returns_503 tests/test_messages_routes.py::test_message_full_non_dict_null_body_returns_503 -v`
Expected: 2 PASS。

- [ ] **Step 12: Run full test files for regression (messages + transform)**

Run: `.venv/bin/pytest tests/test_messages_routes.py tests/test_transform.py -v`
Expected: 全绿（含 list + full 全部用例；transform 单元测试含 `test_transform.py:119-123` 的 JSONDecodeError 路径不回归）。

- [ ] **Step 13: Record diff**

```bash
git rev-parse HEAD          # baseline
git diff --stat             # 应含 messages.py + transform.py + test_messages_routes.py
# ocmar default: do NOT commit
```

---

## Task 2: catch-all 反代上游异常 → 503 〔spec T2，P0〕

**Files:**
- Modify: `src/oc_slimapi/proxy.py:197`（`client.send` 包 try/except）+ 顶部 import（确保 `httpx`、`CodedHTTPException` 可用）
- Modify: `CHANGELOG.md`（记 wire 行为变更）
- Test: `tests/test_proxy.py`（追加 catch-all 网络错误测试）

**Interfaces:**
- Consumes: `oc_slimapi.errors.CodedHTTPException`（thin 路由已在用，见 `messages.py`/`sessions.py`）
- Produces: catch-all handler 在 `httpx.RequestError` 时返回 `503 {"code":"upstream_unavailable"}`，与 thin 路由一致。

**Acceptance Criteria:**
- `T2-C1`: `tests/test_proxy.py` 新增 `test_catch_all_upstream_connect_error_returns_503` → PASS，catch-all 路径（如 `POST /session/ses_x/message`）handler 抛 `ConnectError` 时 `status==503` 且 `code==upstream_unavailable`。
- `T2-C2`: 同文件新增 `test_catch_all_upstream_read_timeout_returns_503` → PASS，handler 抛 `ReadTimeout` 时 `status==503`。
- `T2-C3`: 既有 `test_normal_route_proxied`、`test_shell_endpoint_denied` 等正常透传/deny 用例全绿。
- `T2-C4`: `CHANGELOG.md` 新增一条记录：catch-all 上游网络异常从裸 500 改为 `503 upstream_unavailable`（加性，注明不 bump 版本）。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_proxy.py` 追加（沿用同文件 `_settings` / `_build_app` / `upstream_factory`；catch-all 路径 = 非 `/slimapi/` 非 shell 的任意 opencode 路径）：

```python
async def test_catch_all_upstream_connect_error_returns_503(upstream_factory):
    """catch-all proxy: client.send raises httpx.ConnectError → 503 upstream_unavailable.
    Regression: previously escaped as bare FastAPI 500 (INTERFACE_MAP §4 known gap)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_x/message",
            content=b'{"role":"user","content":"hi"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


async def test_catch_all_upstream_read_timeout_returns_503(upstream_factory):
    """catch-all proxy: client.send raises httpx.ReadTimeout → 503 upstream_unavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_x/message",
            content=b'{"role":"user","content":"hi"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_proxy.py::test_catch_all_upstream_connect_error_returns_503 tests/test_proxy.py::test_catch_all_upstream_read_timeout_returns_503 -v`
Expected: 2 FAIL（裸 500）。

- [ ] **Step 3: Wrap client.send in try/except httpx.RequestError**

先确认 `proxy.py` 顶部 import。检查是否有 `from .errors import CodedHTTPException`（thin 路由用它，但 proxy.py 可能未 import）。若无，在 import 区追加：
```python
from .errors import CodedHTTPException
```
（`httpx` 在 proxy.py 已 import，无需补。）

`src/oc_slimapi/proxy.py:197` 当前：
```python
        response = await client.send(upstream_request, stream=True)
```
改为：
```python
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            # Align catch-all with thin routes (sessions/messages/agent/...):
            # upstream connect/read/timeout/pool failures → structured 503
            # upstream_unavailable, not a bare FastAPI 500. NOTE: turn-fence
            # bump above (line ~196) already advanced; the resulting hole on
            # send-failure is tolerated by ocdroid's lex comparison
            # (see comment block above) — no rollback here.
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
```

**重要边界确认**：`httpx.RequestError` 覆盖 `ConnectError`/`ReadTimeout`/`PoolTimeout`/`RemoteProtocolError`/`WriteError` 等**连接与请求建立阶段**错误。mid-stream 断开（send 已返回 StreamingResponse 之后）走 `_counted_upstream_response` 的 finally，不经过此 except —— 此包装只覆盖 `send()` 调用本身。

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_proxy.py::test_catch_all_upstream_connect_error_returns_503 tests/test_proxy.py::test_catch_all_upstream_read_timeout_returns_503 -v`
Expected: 2 PASS。

- [ ] **Step 5: Run full proxy test file for regression**

Run: `.venv/bin/pytest tests/test_proxy.py -v`
Expected: 全绿（含 shell deny、正常透传、trailing-slash 等）。

- [ ] **Step 6: Record CHANGELOG entry**

在 `CHANGELOG.md` 顶部（最新条目区，按现有格式）追加一条。日期 `2026-08-06`，标题层级与现有条目一致。内容要点：
- **Changed**: catch-all 反代（`proxy.py`）上游网络异常（`httpx.RequestError`：connect/read timeout/pool failure）从裸 `500 Internal Server Error` 改为结构化 `503 {"code":"upstream_unavailable"}`，与 thin 路由（sessions/messages/agent/command/questions）错误面对齐。
- 注明：**加性变更，不 bump `X-Slimapi-Version`**（无 client 依赖现有裸 500；新行为是 thin 路由既有行为的补齐）。
- 注明：catch-all per-request timeout（SSE=None / command=300s / 其他=30s）不变。

- [ ] **Step 7: Record diff**

```bash
git rev-parse HEAD
git diff --stat             # 应含 proxy.py + test_proxy.py + CHANGELOG.md
```

---

## Task 3: 删 capabilities 死代码 〔spec T4，P1〕

**Files:**
- Delete: `src/oc_slimapi/capabilities.py`（125 行，Opt-A 能力解析，v2 后无引用）
- Delete: `tests/test_capabilities.py`（88 行，其单测）

**Interfaces:**
- Consumes: 无
- Produces: 无（删除死代码）

**Acceptance Criteria:**
- `T3-C1`: `src/oc_slimapi/capabilities.py` 与 `tests/test_capabilities.py` 不再存在。
- `T3-C2`: `grep -r "capabilities" src/ tests/`（排除 `__pycache__`）无对已删模块的 import 残留（`from oc_slimapi.capabilities` / `from .capabilities`）。
- `T3-C3`: `./scripts/check.sh` 通过（pytest 全绿 — 删除后无 import 错误）。

- [ ] **Step 1: Verify no live references**

Run: `grep -rn "from oc_slimapi.capabilities\|from \.capabilities\|import capabilities" src/ tests/`
Expected: 仅 `tests/test_capabilities.py` 自引用（确认 src 无生产引用）。

- [ ] **Step 2: Delete both files**

```bash
git rm src/oc_slimapi/capabilities.py tests/test_capabilities.py
```

- [ ] **Step 3: Run full check**

Run: `./scripts/check.sh`
Expected: 通过（pytest 全绿 + 路由↔文档校验）。测试数应比之前少（test_capabilities 的用例移除）。

- [ ] **Step 4: Record diff**

```bash
git rev-parse HEAD
git diff --stat --cached      # 两个删除条目
```

---

## Task 4: sessions list 接 read_with_cap → 413 〔spec T5，P1〕

**Files:**
- Modify: `src/oc_slimapi/routes/sessions.py:38-76`（list 端点改为流式 + cap）+ 顶部 import（确保 `read_with_cap` 可用）
- Test: `tests/test_sessions_routes.py`（追加 oversize → 413 测试）

**Interfaces:**
- Consumes: `oc_slimapi.transform.read_with_cap`（签名 `(response, max_bytes, *, chunk_size=64*1024) -> (bytes|None, int)`，None=超限）；`oc_slimapi.config.Settings.max_response_bytes`（默认 64MiB）。
- Produces: sessions list 端点在 body > cap 时返回 `413 {"code":"response_too_large","limit":<cap>}`，与 messages/agent/command 一致。

**Acceptance Criteria:**
- `T4-C1`: `tests/test_sessions_routes.py` 新增 `test_sessions_list_oversize_body_returns_413` → PASS，上游返回 > cap 的 body 时 `status==413` 且 `code==response_too_large` 且 `limit==<test cap>`。
- `T4-C2`: 既有 sessions list 用例全绿：`test_sessions_list_upstream_4xx_returns_502`、`test_sessions_list_upstream_5xx_returns_503`、`test_sessions_list_network_error_returns_503`、`test_sessions_list_upstream_200_bad_json_returns_503`、`test_sessions_list_upstream_200_non_array_json_returns_503`。
- `T4-C3`: sessions list 的 completeness header（`X-Complete` 等，`sessions.py:79+`）在正常路径仍正确返回（既有用例验证）。
- `T4-C4`: `sessions.py:42-44` 的 "Known limitation" 注释删除（limitation 已消除）。

- [ ] **Step 1: Write the failing test**

在 `tests/test_sessions_routes.py` 追加（参考 `test_messages_routes.py:174-197` 的 413 范式 + 本文件 `upstream_factory` / `_build_app` / `VERSION_HEADERS`）：

```python
async def test_sessions_list_oversize_body_returns_413(upstream_factory):
    """sessions list upstream body > max_response_bytes → 413 response_too_large.
    Aligns sessions list with messages/agent/command cap behaviour (closes
    the known limitation noted at sessions.py:42-44)."""
    cap = 4 * 1024
    oversized = b"x" * (cap * 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized,
                              headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, settings=_settings(max_response_bytes=cap))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions", headers=VERSION_HEADERS)
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "response_too_large"
    assert body["limit"] == cap
```

**前置 fixture 改造（grilling 已确认 `_build_app` 现状不接 `max_response_bytes`）**：

`test_sessions_routes.py:17-24` `_settings()` 当前不接受 overrides，`:27-32` `_build_app` 用固定 `_settings()`。先改造两者（向后兼容，既有无参调用不受影响）：

`_settings` 改为：
```python
def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)
```

`_build_app` 加 `settings` 可选参数（其余参数体不变）：
```python
def _build_app(
    upstream: httpx.AsyncClient,
    *,
    hubs: object | None = None,
    turn_registry: object | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-sessions-test")
    app.state.config = settings or _settings()
    ...（其余不变）
```

改造后既有 `_build_app(upstream)` / `_build_app(upstream, hubs=...)` 全兼容；新测试用 `_build_app(upstream, settings=_settings(max_response_bytes=cap))` 注入小 cap。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sessions_routes.py::test_sessions_list_oversize_body_returns_413 -v`
Expected: FAIL（当前 sessions list 用非流式 `upstream.get` 全 buffer，oversize 不触发 413；可能 200 或 json 解析失败 503，而非 413）。

- [ ] **Step 3: Convert sessions list to streaming + read_with_cap**

**对照模板**：`messages.py:275-303`（list 端点流式范式）。

先在 `sessions.py` 顶部 import 区确认/追加（流式化后用到 `read_with_cap` + `orjson.loads`）：
```python
import orjson                          # 若缺则加（替代 response.json()）
from ..transform import read_with_cap  # 若缺则加
```
（`httpx`、`CodedHTTPException`、`raise_upstream_status`、`stash_up_in` 已在用。）

`src/oc_slimapi/routes/sessions.py` list 端点当前（约 line 38-76）：
```python
    try:
        async with request.app.state.transforms as pool:
            try:
                response = await request.app.state.upstream.get(
                    "/session", params=params, headers=forward_directory_headers(directory),
                )
            except httpx.RequestError as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            stash_up_in(request, len(response.content))
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_upstream_status(exc)
            try:
                payload = response.json()
            except Exception as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            if not isinstance(payload, list):
                raise CodedHTTPException(503, code="upstream_unavailable")
            sessions = await pool.offload(_project_sessions, payload)
```

改为（流式 + cap + 错误 body 排空；保留 `isinstance` 守卫、completeness header 逻辑不变）：
```python
    config = request.app.state.config
    try:
        async with request.app.state.transforms as pool:
            # Stream + cap-read so an oversized upstream /session body cannot
            # spike sidecar RSS (mirrors messages.py:275-303). Cap metric =
            # decompressed logical bytes.
            try:
                response = await request.app.state.upstream.send(
                    request.app.state.upstream.build_request(
                        "GET", "/session",
                        params=params, headers=forward_directory_headers(directory),
                    ),
                    stream=True,
                )
            except httpx.RequestError as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            try:
                if response.status_code >= 400:
                    # Drain error body for connection reuse (mirrors messages).
                    err_body = await response.aread()
                    stash_up_in(request, len(err_body))
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise_upstream_status(exc)
                body, n_read = await read_with_cap(response, config.max_response_bytes)
                if body is None:
                    raise CodedHTTPException(
                        413, code="response_too_large",
                        limit=config.max_response_bytes,
                    )
                stash_up_in(request, n_read)
                try:
                    payload = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
                if not isinstance(payload, list):
                    raise CodedHTTPException(503, code="upstream_unavailable")
                sessions = await pool.offload(_project_sessions, payload)
            finally:
                await response.aclose()
```

**关键点**：
1. `build_request` + `send(stream=True)` 替代 `upstream.get`（流式，不预 buffer content）。
2. 错误状态码分支先 `aread()` 排空再 `raise_for_status`（保持连接复用 + 与 thin 路由 status 映射一致）。
3. `read_with_cap` 返回 `body is None` → 413 `response_too_large`。
4. 保留 `isinstance(payload, list)` 守卫（防 dict/null/scalar-list → 500，与 Task 1 同精神）。
5. `orjson.loads(body)` 替代 `response.json()`（body 已是 cap 后的 bytes）。
6. `finally: await response.aclose()` 确保流关闭。
7. 删除原 `sessions.py:42-44` 的 "Known limitation" 注释（limitation 已消除）。

**completeness header**（`sessions.py:79+`，紧跟 list 端点 body 之后）保持不变 —— `sessions` 变量仍在作用域内，后续 `X-Complete` 逻辑无需改动。implementer 改完后通读 list 端点到 `return` 全段，确认 header 逻辑未断。

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_sessions_routes.py::test_sessions_list_oversize_body_returns_413 -v`
Expected: PASS。

- [ ] **Step 5: Run full sessions test file for regression**

Run: `.venv/bin/pytest tests/test_sessions_routes.py -v`
Expected: 全绿。重点确认：4xx→502、5xx→503、network→503、bad-json→503、non-array→503、正常 list 的 completeness header 用例全过。

- [ ] **Step 6: Record diff**

```bash
git rev-parse HEAD
git diff --stat             # sessions.py + test_sessions_routes.py
```

---

## Task 5: deploy unit 补 MemoryMax 〔spec T6，P1〕

**Files:**
- Modify: `deploy/oc-slimapi.service`（`[Service]` 段补 `MemoryMax=384M`）

**Interfaces:**
- Consumes: 无
- Produces: systemd user unit 在运行时有 384M 内存硬限，与 `design-v2.md` §0.10 / `transform.py:10` docstring 引用一致。

**Acceptance Criteria:**
- `T5-C1`: `deploy/oc-slimapi.service` `[Service]` 段含 `MemoryMax=384M`。
- `T5-C2`: （Final-only）与 `design-v2.md` §0.10 / `transform.py:10` docstring 引用的 384M 一致（无文档漂移）。

- [ ] **Step 1: Add MemoryMax line**

`deploy/oc-slimapi.service` 在 `[Service]` 段末尾（`SyslogIdentifier=oc-slimapi` 之后、`[Install]` 之前）追加：
```ini

# Memory hard cap. The arithmetic of max_transforms=1 + max_response_bytes=64MiB
# + 16MiB inline cap + Python/Baseline RSS fits under 384M (see design-v2 §0.10,
# transform.py docstring). cgroup-enforced OOM kill protects the host if a
# runaway upstream body ever bypasses the read_with_cap fences.
MemoryMax=384M
```

**不加 `MemoryHigh`**（spec 确认：去掉可选的 MemoryHigh，只保留 MemoryMax，避免软限语义引入额外运维认知负担）。

- [ ] **Step 2: Verify file syntax sanity**

Run: `systemd-analyze verify deploy/oc-slimapi.service` 或（user service 无 systemd-analyze 时）人工核对 ini 段结构。
Expected: 无语法错误（`MemoryMax=` 是合法 user service 指令）。

- [ ] **Step 3: Record diff**

```bash
git rev-parse HEAD
git diff --stat             # 仅 deploy/oc-slimapi.service
```

---

## Task 6: 文档语义同步 〔spec T3，P0，放最后〕

**依赖**：Task 1-5 完成后执行（基于最终代码态同步文档）。

**Files:**
- Modify: `docs/specs/INTERFACE_MAP.md`（§0 路由清单补 questions；§3 part.removed→digest；§4 schema_degraded）
- Modify: `docs/specs/design-v2.md`（§1.5 smoke 表述）

**Interfaces:** 无（纯文档）。

**Acceptance Criteria:**
- `T6-C1`: `INTERFACE_MAP.md` §0 路由注册清单含 `questions`（顺序与 `app.py:327` 一致：health → agent → command → sessions → messages → events → metrics → questions → token_stream）。
- `T6-C2`: `INTERFACE_MAP.md` §3 中 `message.part.removed` 的描述改为"v2 不触发 digest（仅路由 token hub）"，与 `global_hub.py:605-628` + 契约 §3 line 146 一致。
- `T6-C3`: `INTERFACE_MAP.md` §4（ready 行）`schema_degraded` 描述改为"仅 health/ready 诊断回显，不触发 messages 自动降级（v2 恒 skeleton）"，与代码一致（messages 路由从不读 `schema_degraded`）。
- `T6-C4`: `design-v2.md` §1.5 改为"smoke 保留：`app.py:35-56` 运行消息字段校验，异常设 `schema_degraded` 供 health/ready 回显"，与代码一致。
- `T6-C5`: `./scripts/check.sh` 通过（含 `check_routes_doc.py` 路由存在性校验）。

- [ ] **Step 1: Read current doc state of the four spots**

Read（确认待改的精确文本，避免凭记忆）：
- `docs/specs/INTERFACE_MAP.md`（§0 路由清单、§3 part.removed、§4 schema_degraded）
- `docs/specs/design-v2.md`（§1.5）
- 对照代码真相源：`src/oc_slimapi/app.py:35-56`（smoke）、`app.py:327`（questions 注册）、`global_hub.py:605-628`（part.removed 路由）。

- [ ] **Step 2: Fix §0 route list — add questions**

`INTERFACE_MAP.md` §0 路由注册清单补 `questions`（按 `app.py` 实际注册顺序）。implementer 读 `app.py` 的 `include_router` 序列后写入准确顺序。

- [ ] **Step 3: Fix §3 — message.part.removed no longer triggers digest**

将 §3 中"`message.part.removed`（flat props）→ digest 更新"改为明确：v2 下该事件**仅路由 token hub**，不触发 digest（引用契约 §3 line 146 + `global_hub.py:605-628`）。

- [ ] **Step 4: Fix §4 — schema_degraded is diagnostic-only**

将 §4 ready 行中"`schema_degraded` 让 messages 路由自动降级 full"改为"仅 health/ready 回显，v2 messages 恒 skeleton，不读此字段"。

- [ ] **Step 5: Fix design-v2 §1.5 — smoke retained**

将"启动字段漂移 smoke 已移除"改为"smoke 保留（`app.py:35-56`），异常设 `schema_degraded` 供 health/ready 回显"。

- [ ] **Step 6: Run full check**

Run: `./scripts/check.sh`
Expected: 通过（pytest 全绿 + 路由↔文档存在性校验）。

- [ ] **Step 7: Manual diff review**

人工核对 4 处文档表述与代码逐条一致（self-review：每处改动引用的代码行号正确）。

- [ ] **Step 8: Record diff**

```bash
git rev-parse HEAD
git diff --stat             # INTERFACE_MAP.md + design-v2.md
```

---

## Criterion Ownership Matrix

| Criterion ID | Spec requirement | Owner task | Cross-task deps | Verification (command/test + expected) | Final-only? |
|---|---|---|---|---|---|
| T1-C1 | messages 非 list(dict) → 503 | Task 1 | — | `pytest tests/test_messages_routes.py::test_messages_list_non_list_dict_body_returns_503` → PASS | N |
| T1-C2 | messages 非 list(null) → 503 | Task 1 | — | `pytest ...::test_messages_list_non_list_null_body_returns_503` → PASS | N |
| T1-C3 | messages 标量 list → 503 | Task 1 | — | `pytest ...::test_messages_list_scalar_list_body_returns_503` → PASS | N |
| T1-C4 | messages 既有用例无回归 | Task 1 | — | `pytest tests/test_messages_routes.py tests/test_transform.py` → 全绿 | N |
| T1-C5 | full/{mid} 非 dict(list) → 503 | Task 1 | — | `pytest tests/test_messages_routes.py::test_message_full_non_dict_list_body_returns_503` → PASS | N |
| T1-C6 | full/{mid} 非 dict(null) → 503 | Task 1 | — | `pytest tests/test_messages_routes.py::test_message_full_non_dict_null_body_returns_503` → PASS | N |
| T2-C1 | catch-all ConnectError → 503 | Task 2 | — | `pytest tests/test_proxy.py::test_catch_all_upstream_connect_error_returns_503` → PASS | N |
| T2-C2 | catch-all ReadTimeout → 503 | Task 2 | — | `pytest ...::test_catch_all_upstream_read_timeout_returns_503` → PASS | N |
| T2-C3 | proxy 既有用例无回归 | Task 2 | — | `pytest tests/test_proxy.py` → 全绿 | N |
| T2-C4 | CHANGELOG 记 catch-all 变更 | Task 2 | — | manual: CHANGELOG.md 含新条目，注明加性不 bump | N |
| T3-C1 | capabilities.py 已删 | Task 3 | — | manual: 文件不存在 | N |
| T3-C2 | 无 import 残留 | Task 3 | — | `grep -rn "from oc_slimapi.capabilities\|from \.capabilities" src/ tests/` → 空 | N |
| T3-C3 | check.sh 通过 | Task 3 | — | `./scripts/check.sh` → exit 0 | N |
| T4-C1 | sessions list oversize → 413 | Task 4 | — | `pytest tests/test_sessions_routes.py::test_sessions_list_oversize_body_returns_413` → PASS | N |
| T4-C2 | sessions 既有用例无回归 | Task 4 | — | `pytest tests/test_sessions_routes.py` → 全绿（含 4xx/5xx/network/badjson/nonarray） | N |
| T4-C3 | completeness header 保留 | Task 4 | — | 既有 list 用例断言 X-Complete → PASS | N |
| T4-C4 | 删 known-limitation 注释 | Task 4 | — | manual: sessions.py 不再含 "Known limitation" 注释 | N |
| T5-C1 | deploy 含 MemoryMax=384M | Task 5 | — | manual: deploy/oc-slimapi.service `[Service]` 含 `MemoryMax=384M` | N |
| T5-C2 | 与 design-v2/transform docstring 一致 | Task 5 | — | manual: 384M 三处一致 | Y |
| T6-C1 | §0 补 questions | Task 6 | Task 1-5 | manual: INTERFACE_MAP §0 含 questions | N |
| T6-C2 | §3 part.removed 不触发 digest | Task 6 | — | manual: 与 global_hub.py:605-628 一致 | N |
| T6-C3 | §4 schema_degraded 诊断 only | Task 6 | — | manual: 与代码一致 | N |
| T6-C4 | design-v2 §1.5 smoke 保留 | Task 6 | — | manual: 与 app.py:35-56 一致 | N |
| T6-C5 | check.sh 通过 | Task 6 | — | `./scripts/check.sh` → exit 0 | N |
| FINAL-C1 | 全量 check.sh 通过 | 全 task | Task 1-6 | `./scripts/check.sh` → exit 0 | Y |
| FINAL-C2 | oracle Top5 风险 #1/#2 消除 | 全 task | Task 1-2 | manual: catch-all 500 与 messages 500 已修 | Y |

---

## Self-Review

**1. Spec coverage**：spec §2 的 6 项（T1-T6）逐项对应 plan Task 1-6。spec §3 成功标准 7 条 → FINAL-C1/C2 + 各 task 的 check.sh gate 覆盖。spec §4 YAGNI 边界 → Global Constraints 固化。spec §5 风险 → Task 2/4 的"重要边界确认/关键点"段落覆盖。无遗漏。

**2. Placeholder scan**：无 TBD/TODO。每步含完整代码或精确命令 + expected。Task 6 的文档改动因依赖代码态，给出明确"改什么"但精确文本留到执行时读文件确认（合理 — 文档同步必须基于实际文本）。

**3. Type consistency**：`read_with_cap` 签名 `(response, max_bytes) -> (bytes|None, int)` 在 Task 4 与 transform.py:106-133 一致。`CodedHTTPException(503, code="upstream_unavailable")` 在 Task 1/2/4 与 thin 路由既有用法一致。`code="response_too_large"` + `limit=` 在 Task 4 与 messages.py:298 一致。

**4. Acceptance observability**：每条 criterion 是可执行命令/可检查文件状态，含稳定 `T<N>-C<seq>` id。无"实现完整"类模糊表述。

**风险提示**（执行时注意）：Task 4 是最复杂改动（流式化重写 list 端点 body 读取路径），implementer 须通读 sessions.py list 端点全文（含 completeness header 后续逻辑）再改，避免断链。Task 2 的 except 边界已在 step 3 明确（仅覆盖 send 调用，不含 mid-stream）。
