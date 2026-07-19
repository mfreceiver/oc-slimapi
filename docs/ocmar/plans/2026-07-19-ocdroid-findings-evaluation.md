# ocdroid 评审发现修复 + v1 遗留补齐（F1–F5/§5/G1/G6/D1–D8）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 ocdroid 接口评审的 F1–F5 + §5 文档重构 + 补齐 v1 遗留 G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步）；全部加性变更不 bump wire 版本。

**Architecture:** 代码 5 文件（sessions.py / questions.py / app.py / hub.py / messages.py），文档 8 文件。T1–T4 修 F1/F2/F3；T5–T6 实现 G1（hub.py）；T7 实现 G6（messages.py）；T8–T10 文档同步。

**Tech Stack:** Python 3 + FastAPI + httpx + orjson + pytest（asyncio）。测试用 `httpx.MockTransport` + `ASGITransport`（`tests/conftest.py`）。

**Spec：** `docs/ocmar/specs/2026-07-19-ocdroid-findings-evaluation-design.md`

## Global Constraints

- **不 bump `X-Slimapi-Version`**：所有变更加性（spec §4.1）；wire API 维持 `1`，`ACCEPTED_CLIENT_VERSIONS=(1,1)` 不动。
- **不 commit**：每 task 仅 `git diff` 记录；ocmar 默认不 commit，除非用户显式要求。
- **校验**：`./scripts/check.sh`（= `pytest tests/`）。每 task 完成后必须 green。
- **契约权威**：`docs/v1-contract.md` 是 wire 基准；本 plan 契约改动是「实现放宽/补齐 → 契约同步」。
- **loopback-only / stunnel mTLS** 是安全边界；allowlist 非安全边界（spec §4.2）。
- **日期**：`2026-07-19`。
- **route_secret**：测试用 `"x" * 32`（`tests/test_questions_routes.py:24`），不入库真实 secret。
- **G1/G6 关键约束**：G1 走实时 `/global/event`（session.error 非 durable，不涉 `/sync/history`）；G6 discover 先行、mid 404 非整 404、全 mid 404 仍 200。

---

## Parallelization（Wave / Lane 结构 — 可并发部分）

**原则**：同一 wave 内各 task 写域完全不相交（不同文件），可派并行 fixer；wave 间存在语义依赖（下一 wave 的 task 需上一 wave 产物），必须串行。所有 fixer 共用同一 working tree（不开 worktree）；并行 fixer 各自只跑**本模块测试**，wave 边界由编排者跑**全量 `./scripts/check.sh`** 再放行下一 wave。

### Wave A — 4 fixer 并行（无跨 task 依赖；写域不相交）

| Lane | Task | 写域 | 本模块测试（fixer 自跑） |
|---|---|---|---|
| sessions | **T1** | `sessions.py` + `app.py` + `test_sessions_routes.py`（仅新增 2 测试，不动 T4 待改的 status 测试） | `pytest tests/test_sessions_routes.py`（T1 仅加 `load_products`/`warm_allowlist` 测试；status 测试原样 green） |
| questions | **T2** | `questions.py`（`_token` async + 3 caller）+ `test_questions_routes.py` | `pytest tests/test_questions_routes.py` |
| hub | **T5** | `sse/hub.py`（模块级 sentinel + `_sanitize_error_message`）+ `test_hub.py`（8 sanitize 测试） | `pytest tests/test_hub.py -k sanitize` |
| messages | **T7** | `routes/messages.py`（`/full` 路由）+ `test_messages_routes.py`（8 g6 测试） | `pytest tests/test_messages_routes.py -k g6` |

**Wave A 出口门控**：4 fixer 全部 implement→self-test green 后，编排者跑 `./scripts/check.sh`；green 后派 4 个 task-reviewer 并行评审；fix 循环；再 `check.sh` green → 进 Wave B。

### Wave B — 3 fixer 并行（依赖 Wave A 已合入 tree；写域不相交）

| Lane | Task | 写域 | 依赖 | 本模块测试 |
|---|---|---|---|---|
| questions | **T3** | `questions.py`（`_aggregate` null）+ `test_questions_routes.py` | **T1**（`load_products(app)` 签名）+ T2（文件锁已释放） | `pytest tests/test_questions_routes.py` |
| sessions | **T4** | `sessions.py`（`normalize_directory` + `session_status`）+ `test_sessions_routes.py`（改写 `test_status_allowlist_miss_*`） | T1（文件锁已释放） | `pytest tests/test_sessions_routes.py` |
| hub | **T6** | `sse/hub.py`（`DigestFields.last_error` + `publish` 分派 + `flush` 合并 + clear/deleted）+ `test_hub.py`（7 g1 测试 + 改 `test_message_part_delta_*`） | **T5**（用 `_UNSET`/`ABORT_NAME`/`_sanitize_error_message`）+ 文件锁 | `pytest tests/test_hub.py` |

**Wave B 出口门控**：同 A——3 fixer 完成后全量 `check.sh` + 3 并行 reviewer + fix 循环 + `check.sh` → 进 Wave C。

### Wave C — 3 fixer 并行（依赖全部代码已 verified；文档写域不相交）

| Lane | Task | 写域 | 依赖 |
|---|---|---|---|
| contract | **T8** | `docs/v1-contract.md` | T1–T7 实际行为 |
| design/spec | **T9** | `docs/design-v2.md` + `docs/v1-impl-spec.md` + `AGENTS.md` | T1–T7 |
| client/map/status | **T10** | `docs/CLIENT_CHANGES.md` + `docs/INTERFACE_MAP.md` + `docs/v1-contract-implementation-status.md` + `CHANGELOG.md` | T1–T7 |

**Wave C 出口门控**：3 fixer 完成 → `check.sh`（确认文档无副作用）+ 3 并行 reviewer（人工核交叉引用）→ 终门控。

### 写域不变量（编排者必检）

- 同一 wave 内任两 task 的写域交集 = ∅（上表已保证）。
- 跨 wave 同 lane（sessions: T1→T4；questions: T2→T3；hub: T5→T6）串行，无并发。
- 文档 wave（C）必须在代码 wave（A/B）全 verify 后启动——文档要反映实际行为。
- 并行 fixer 严禁跑全量 `check.sh`（其它 lane 在 flux）；全量门控只在 wave 边界由编排者跑。

### Fan-out 上限

- 每 wave 最多 4 fixer（Wave A）；Wave B/C 各 3。符合「2–4 fixer 并行」预期。
- Reviewer 同波并行（最多 4），与 implementer 不重叠（writer ≠ reviewer）。

---

## Task 1: F3 基础 — `load_products(app)` 签名 + 启动 allowlist 暖机

**Files:**
- Modify: `src/oc_slimapi/routes/sessions.py`
- Modify: `src/oc_slimapi/app.py`
- Test: `tests/test_sessions_routes.py`

**Interfaces:**
- Consumes: `app.state.upstream`、`app.state.directory_allowlist`
- Produces: `async def load_products(app: FastAPI) -> list[dict]`（签名 `request`→`app`）；`async def warm_allowlist(app: FastAPI) -> None`（新；吞错）

**Acceptance Criteria:**
- `T1-C1`: `load_products(app)` 直接调，mock upstream 返 `[{"id":"p1","worktree":"/app"}]` + `/project/p1/directories→[]` → `app.state.directory_allowlist=={"/app"}`（test `test_load_products_takes_app_state`）。
- `T1-C2`: `warm_allowlist(app)` 在 upstream 抛 `httpx.ConnectError` 时不 raise，allowlist 保持空 set（test `test_warm_allowlist_swallows_upstream_error`）。
- `T1-C3`: 既有 `/slimapi/projects` 路由测试 green（`test_projects_failure_renders_code`、`test_projects_4xx_returns_502_upstream_http_n`）。
- `T1-C4`: `app.py` lifespan 中 `smoke(app)` 之后调 `await sessions.warm_allowlist(app)`（code review）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_sessions_routes.py` 末尾：

```python
async def test_load_products_takes_app_state(upstream_factory):
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([{"id": "p1", "worktree": "/app"}]),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        return httpx.Response(404, content=b"[]")

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    result = await sessions_mod.load_products(app)
    assert any(p["id"] == "p1" for p in result)
    assert app.state.directory_allowlist == {"/app"}


async def test_warm_allowlist_swallows_upstream_error(upstream_factory):
    from oc_slimapi.routes import sessions as sessions_mod

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated", request=request)

    upstream = upstream_factory(handler)
    app = _build_app(upstream)
    await sessions_mod.warm_allowlist(app)
    assert app.state.directory_allowlist == set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_sessions_routes.py::test_load_products_takes_app_state tests/test_sessions_routes.py::test_warm_allowlist_swallows_upstream_error -v`
Expected: FAIL（签名不匹配 / `warm_allowlist` 不存在）。

- [ ] **Step 3: 重构 `load_products` 签名 + 新增 `warm_allowlist`**

`src/oc_slimapi/routes/sessions.py`：

```python
from fastapi import FastAPI  # 顶部加（若未有）

async def load_products(app: FastAPI) -> list[dict]:
    client = app.state.upstream
    response = await client.get("/project")
    response.raise_for_status()
    projects = response.json()
    semaphore = asyncio.Semaphore(8)

    async def decorate(project: dict) -> dict:
        async with semaphore:
            result = await client.get(f"/project/{quote(str(project['id']), safe='')}/directories")
            result.raise_for_status()
            raw_directories = result.json()
        directories = []
        for item in raw_directories if isinstance(raw_directories, list) else []:
            if not isinstance(item, dict):
                continue
            path = item.get("directory", item.get("path"))
            if isinstance(path, str):
                directories.append({"path": path.rstrip("/") or "/", "strategy": item.get("strategy")})
        worktree = project.get("worktree")
        return {"id": project.get("id"), "name": project.get("name"),
                "worktree": worktree, "directories": directories}

    output = await asyncio.gather(*(decorate(item) for item in projects if isinstance(item, dict)))
    allowlist = {
        path.rstrip("/") or "/"
        for project in output
        for path in ([project.get("worktree")] + [item["path"] for item in project["directories"]])
        if isinstance(path, str) and path.startswith("/")
    }
    app.state.directory_allowlist = allowlist
    return output


async def warm_allowlist(app: FastAPI) -> None:
    """Best-effort allowlist warm-up at startup. Swallows upstream errors so a
    not-yet-ready opencode does not block sidecar boot; lazy refresh via
    require_directory remains the fallback."""
    try:
        await load_products(app)
    except Exception:
        pass
```

`require_directory` 与 `projects` 路由的 caller 改 `request.app`：

```python
async def require_directory(request: Request, directory: str) -> str:
    normalized = directory.rstrip("/") or "/"
    if normalized not in request.app.state.directory_allowlist:
        try:
            await load_products(request.app)
        except Exception as exc:
            raise CodedHTTPException(503, code="upstream_unavailable",
                                     message="cannot refresh directory allowlist") from exc
    if normalized not in request.app.state.directory_allowlist:
        raise CodedHTTPException(400, code="directory_not_allowed")
    return normalized


@router.get("/projects")
async def projects(request: Request):
    try:
        payload = await load_products(request.app)
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc)
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    return json_response(payload, accept_encoding=request.headers.get("accept-encoding"))
```

- [ ] **Step 4: app.py lifespan 加暖机**

`src/oc_slimapi/app.py`（lifespan 内 `await smoke(app)` 之后、`yield` 之前）：

```python
    await smoke(app)
    # F3: best-effort allowlist warm-up so the first routeToken-bearing reply
    # does not hit a cold allowlist. Failure is non-fatal (lazy refresh fallback).
    await sessions.warm_allowlist(app)
    try:
        yield
```

- [ ] **Step 5: 跑全量 green**

Run: `./scripts/check.sh`
Expected: 全 PASS。

- [ ] **Step 6: 记录 diff**

```bash
git rev-parse HEAD; git diff --stat <baseline>
```

---

## Task 2: F3 routeToken — `_token` async + `require_directory` 刷新

**Files:**
- Modify: `src/oc_slimapi/routes/questions.py`（`_token` + 3 caller `await`）
- Test: `tests/test_questions_routes.py`

**Interfaces:**
- Consumes: `require_directory`（Task 1 后不变）
- Produces: `async def _token(request, token, kind, request_id, session_id=None) -> str`

**Acceptance Criteria:**
- `T2-C1`: cold allowlist（`set()`）+ handler 返有效 `/project`（含 `/app`）+ routeToken dir=`/app` 的 reply → 204（test `test_token_refreshes_cold_allowlist_then_reply`）。
- `T2-C2`: 既有 `test_questions_token_directory_not_allowed` 仍 PASS（handler 返空 `/project`，刷新后仍空 → 仍 400）。
- `T2-C3`: `_token` async；reply/reject/permission 三 caller `await _token(...)`（code review）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_questions_routes.py`：

```python
async def test_token_refreshes_cold_allowlist_then_reply(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/project":
            return httpx.Response(200, content=orjson.dumps([{"id": "p1", "worktree": "/app"}]),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/project/p1/directories":
            return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})
        if request.url.path == "/question/q1/reply":
            return httpx.Response(204)
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set())
    secret = app.state.route_secret
    token = issue_route_token(secret, kind="question", request_id="q1", session_id=None, directory="/app")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/slimapi/questions/q1/reply", headers=VERSION_HEADERS,
                                     json={"answers": [["a"]], "routeToken": token})
    assert response.status_code == 204
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_questions_routes.py::test_token_refreshes_cold_allowlist_then_reply -v`
Expected: FAIL（当前 `_token` 直接查空 allowlist → 400）。

- [ ] **Step 3: `_token` async + `require_directory`**

`src/oc_slimapi/routes/questions.py`：

```python
async def _token(request: Request, token: str, kind: str, request_id: str, session_id: str | None = None) -> str:
    try:
        payload = verify_route_token(token, request.app.state.route_secret, kind=kind,
                                     request_id=request_id, session_id=session_id)
    except RouteTokenError as exc:
        raise CodedHTTPException(400, code="invalid_route_token") from exc
    return await require_directory(request, payload["directory"])


@router.post("/questions/{qid}/reply")
async def reply(request: Request, qid: str, body: ReplyBody):
    directory = await _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reply", directory, {"answers": body.answers})


@router.post("/questions/{qid}/reject")
async def reject(request: Request, qid: str, body: TokenBody):
    directory = await _token(request, body.routeToken, "question", qid)
    return await _post(request, f"/question/{qid}/reject", directory, {})


@router.post("/sessions/{sid}/permissions/{pid}")
async def permission(request: Request, sid: str, pid: str, body: PermissionBody):
    directory = await _token(request, body.routeToken, "permission", pid, sid)
    return await _post(request, f"/session/{sid}/permissions/{pid}", directory, {"response": body.response})
```

- [ ] **Step 4: 跑测试 green**

Run: `.venv/bin/pytest tests/test_questions_routes.py -v`
Expected: 全 PASS（含 `test_questions_token_directory_not_allowed` 仍 PASS）。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS。

- [ ] **Step 6: 记录 diff**

---

## Task 3: F1 — `/questions` + `/permissions` 允许 null directory

**Files:**
- Modify: `src/oc_slimapi/routes/questions.py`（`_aggregate` + 两路由签名）
- Test: `tests/test_questions_routes.py`

**Interfaces:**
- Consumes: `load_products(request.app)`（Task 1）、`request.app.state.directory_allowlist`
- Produces: q/p 入参 `directory: list[str] | None = Query(None)`

**Acceptance Criteria:**
- `T3-C1`: 不传 `directory` + allowlist={`/app`} + handler `/question` 返 `[{"id":"q1"}]` → 200，`items[0].directory=="/app"` + 有 `routeToken`（test `test_questions_null_directory_aggregates_allowlist`）。
- `T3-C2`: 不传 `directory` + allowlist 空 + handler `/project` 返空 → 200 `{"items":[],"errors":[]}`（test `test_questions_null_directory_empty_allowlist_returns_empty_envelope`）。
- `T3-C3`: explicit 不变（`test_questions_directory_count_bounds`、`test_aggregate_empty_directories_invalid_count` 仍 PASS）。
- `T3-C4`: 两路由签名一致 `Query(None)`（code review）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_questions_routes.py`：

```python
async def test_questions_null_directory_aggregates_allowlist(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/question":
            return httpx.Response(200, content=orjson.dumps([{"id": "q1", "sessionID": "ses_1"}]),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist={"/app"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["directory"] == "/app"
    assert "routeToken" in body["items"][0]
    assert body["errors"] == []


async def test_questions_null_directory_empty_allowlist_returns_empty_envelope(upstream_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream, allowlist=set())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/questions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"items": [], "errors": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_questions_routes.py::test_questions_null_directory_aggregates_allowlist tests/test_questions_routes.py::test_questions_null_directory_empty_allowlist_returns_empty_envelope -v`
Expected: FAIL（`Query(...)` 必填 → 422）。

- [ ] **Step 3: `_aggregate` 支持 null + 签名 `Query(None)`**

`src/oc_slimapi/routes/questions.py`：

```python
from .sessions import require_directory, load_products  # 加 load_products


async def _aggregate(request: Request, kind: Literal["question", "permission"], directories: list[str] | None):
    if directories is not None:
        unique = list(dict.fromkeys(directories))
        if not unique or len(unique) > 32:
            raise CodedHTTPException(400, code="invalid_directory_count")
        checked = [await require_directory(request, d) for d in unique]
    else:
        # F1: null = aggregate the sidecar's whole scope (allowlist). NOT subject
        # to the 1–32 guard — that constrains client-supplied lists; null means
        # "sidecar's whole scope" sized by ops via opencode project list.
        allowlist = request.app.state.directory_allowlist
        if not allowlist:
            try:
                await load_products(request.app)
            except Exception:
                pass
            allowlist = request.app.state.directory_allowlist
        checked = sorted(allowlist)  # deterministic; may be []

    async def fetch(directory: str):
        try:
            response = await request.app.state.upstream.get(
                f"/{kind}", params={"directory": directory},
                headers=forward_directory_headers(directory), timeout=2.0,
            )
            if response.status_code >= 400:
                return None, {"directory": directory, "code": f"upstream_http_{response.status_code}"}
            output = []
            for item in response.json():
                if not isinstance(item, dict) or not _request_id(item):
                    continue
                enriched = dict(item)
                enriched["directory"] = directory
                enriched["routeToken"] = issue_route_token(
                    request.app.state.route_secret, kind=kind,
                    request_id=_request_id(item), session_id=item.get("sessionID"),
                    directory=directory,
                )
                output.append(enriched)
            return output, None
        except httpx.TimeoutException:
            return None, {"directory": directory, "code": "upstream_timeout"}
        except Exception:
            return None, {"directory": directory, "code": "upstream_error"}

    results = await asyncio.gather(*(fetch(d) for d in checked))
    items = [item for group, _ in results if group for item in group]
    errors = [e for _, e in results if e]
    status = 503 if results and len(errors) == len(results) else 200
    return json_response({"items": items, "errors": errors}, status_code=status,
                         accept_encoding=request.headers.get("accept-encoding"))


@router.get("/questions")
async def questions(request: Request, directory: list[str] | None = Query(None)):
    return await _aggregate(request, "question", directory)


@router.get("/permissions")
async def permissions(request: Request, directory: list[str] | None = Query(None)):
    return await _aggregate(request, "permission", directory)
```

- [ ] **Step 4: 跑测试 green**

Run: `.venv/bin/pytest tests/test_questions_routes.py -v` → 全 PASS。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS。

- [ ] **Step 6: 记录 diff**

---

## Task 4: F2 — 放宽 per-session status 的 allowlist

**Files:**
- Modify: `src/oc_slimapi/routes/sessions.py`（提取 `normalize_directory` + `session_status` 改用之）
- Test: `tests/test_sessions_routes.py`（改写 `test_status_allowlist_miss_returns_400`）

**Interfaces:**
- Produces: `def normalize_directory(directory: str) -> str`（纯函数）

**Acceptance Criteria:**
- `T4-C1`: per-session status 对非白名单 dir 的 sid → 200（有效 status map 返其值，缺 sid 返 idle），不再 400（test `test_status_allowlist_miss_relaxed_returns_status`）。
- `T4-C2`: allowlisted sid 不变（`test_status_map_missing_sid_returns_idle` 等 PASS，不改）。
- `T4-C3`: 批量 `/sessions/status` 仍必填+校验（`test_batch_status_allowlist_miss_renders_code` PASS，不改）。
- `T4-C4`: `normalize_directory` 纯函数存在；`require_directory` 复用（code review）。

- [ ] **Step 1: 改写失败测试**

`tests/test_sessions_routes.py` 替换 `test_status_allowlist_miss_returns_400`（line 102–116）为：

```python
async def test_status_allowlist_miss_relaxed_returns_status(upstream_factory):
    """T4-C1/F2: per-session status 放宽 allowlist —— sid 自洽即能力。
    discover 得 /secret（非白名单）+ status map 有效 → 200，不再 400。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/ses_x":
            return httpx.Response(200, content=orjson.dumps({"id": "ses_x", "directory": "/secret"}),
                                  headers={"Content-Type": "application/json"})
        if request.url.path == "/session/status":
            return httpx.Response(200, content=orjson.dumps({"ses_x": {"type": "busy"}}),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"})

    upstream = upstream_factory(handler)
    app = _build_app(upstream)  # allowlist 默认空；/secret 不在内
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/sessions/ses_x/status", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"type": "busy"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_sessions_routes.py::test_status_allowlist_miss_relaxed_returns_status -v`
Expected: FAIL（当前 `require_directory` → 400）。

- [ ] **Step 3: 提取 `normalize_directory` + `session_status` 放宽**

`src/oc_slimapi/routes/sessions.py`：

```python
def normalize_directory(directory: str) -> str:
    """Strip trailing slash (keep root '/'). Pure; no allowlist check."""
    return directory.rstrip("/") or "/"


async def require_directory(request: Request, directory: str) -> str:
    normalized = normalize_directory(directory)
    if normalized not in request.app.state.directory_allowlist:
        try:
            await load_products(request.app)
        except Exception as exc:
            raise CodedHTTPException(503, code="upstream_unavailable",
                                     message="cannot refresh directory allowlist") from exc
    if normalized not in request.app.state.directory_allowlist:
        raise CodedHTTPException(400, code="directory_not_allowed")
    return normalized
```

`session_status`（原 `directory = await require_directory(request, directory)` 行 ~162）改为：

```python
    # F2: per-session status is a read keyed by sid (capability). allowlist is
    # not a security boundary (stunnel mTLS is); normalize without gating,
    # aligning with /messages/{sid} (G7-soft). Batch /sessions/status still gates.
    directory = normalize_directory(directory)
```

- [ ] **Step 4: 跑测试 green**

Run: `.venv/bin/pytest tests/test_sessions_routes.py -v` → 全 PASS。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS。

- [ ] **Step 6: 记录 diff**

---

## Task 5: G1 脱敏 — `_sanitize_error_message` 纯函数 + golden 测试

**Files:**
- Modify: `src/oc_slimapi/sse/hub.py`（模块级 `_UNSET` sentinel + `_sanitize_error_message()` + 已编译 regex）
- Test: `tests/test_hub.py`（golden 用例）

**Interfaces:**
- Consumes: 无
- Produces: `def _sanitize_error_message(message: str | None, fallback_name: str | None) -> str`；模块级 `_UNSET = object()`、`ABORT_NAME = "MessageAbortedError"`

**Acceptance Criteria:**
- `T5-C1`: Unix 绝对路径 → `<path>`（test `test_sanitize_strips_unix_paths`）。
- `T5-C2`: Windows 绝对路径 → `<path>`（test `test_sanitize_strips_windows_paths`）。
- `T5-C3`: stack frame `at file:line:col` → 剥离（test `test_sanitize_strips_stack_frames`）。
- `T5-C4`: secret（token/key/bearer=...）→ `<redacted>`（test `test_sanitize_strips_secrets`）。
- `T5-C5`: 多行 → 仅首行（test `test_sanitize_takes_first_line`）。
- `T5-C6`: message 缺失 → `fallback_name`；均缺 → `"(no detail)"`（test `test_sanitize_missing_message_*`）。
- `T5-C7`: 超 512 字符 → 截断 512（test `test_sanitize_truncates_long`）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_hub.py`（顶部若未有 `from oc_slimapi.sse.hub import _sanitize_error_message` 按需 import）：

```python
def test_sanitize_strips_unix_paths():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("open(/home/bob/secret.txt) failed", None) == "open(<path>) failed"


def test_sanitize_strips_windows_paths():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("load C:\\Users\\bob\\file.txt failed", None) == "load <path> failed"


def test_sanitize_strips_stack_frames():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("boom at app.ts:10:5", None) == "boom"
    assert _sanitize_error_message("err at module.js:42", None) == "err"


def test_sanitize_strips_secrets():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("token=abc123-xyz leaked", None) == "<redacted> leaked"
    assert _sanitize_error_message('Authorization: Bearer abc.def', None) == "<redacted>"


def test_sanitize_takes_first_line():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("main error\n  at a:1:1\n  at b:2:2", None) == "main error"


def test_sanitize_missing_message_uses_fallback_name():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message(None, "UnknownError") == "UnknownError"
    assert _sanitize_error_message("", "UnknownError") == "UnknownError"


def test_sanitize_missing_message_and_name():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message(None, None) == "(no detail)"


def test_sanitize_truncates_long():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert len(_sanitize_error_message("x" * 600, None)) == 512
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_hub.py -k sanitize -v`
Expected: FAIL（`_sanitize_error_message` 不存在 → ImportError）。

- [ ] **Step 3: 实现 `_sanitize_error_message` + sentinel**

`src/oc_slimapi/sse/hub.py` 顶部（import 区 + `STOP` sentinel 附近）加：

```python
import re

_UNSET = object()  # three-state sentinel for DigestFields.last_error
ABORT_NAME = "MessageAbortedError"

_UNIX_PATH_RE = re.compile(r"/(?:[A-Za-z0-9._\-]+/)*[A-Za-z0-9._\-]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:(?:[\\/][A-Za-z0-9._\-]+)+")
_STACK_FRAME_RE = re.compile(r"\s*\bat\s+\S+?:\d+(?::\d+)?", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(token|key|bearer|password|passwd|secret|api[_-]?key|authorization)\b"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9._\-/=+]+"
)


def _sanitize_error_message(message: str | None, fallback_name: str | None) -> str:
    """G1 desensitization (impl-spec §7 硬约束 4): first line → strip abs paths
    → strip stack frames → strip secrets → truncate ≤512. Missing message falls
    back to the error name, else "(no detail)"."""
    if not message or not isinstance(message, str):
        return fallback_name or "(no detail)"
    first_line = message.split("\n", 1)[0]
    first_line = _WIN_PATH_RE.sub("<path>", first_line)   # Windows first (drive letter)
    first_line = _UNIX_PATH_RE.sub("<path>", first_line)
    first_line = _STACK_FRAME_RE.sub("", first_line)
    first_line = _SECRET_RE.sub("<redacted>", first_line)
    first_line = first_line.strip()
    if len(first_line) > 512:
        first_line = first_line[:512]
    return first_line or fallback_name or "(no detail)"
```

- [ ] **Step 4: 跑测试 green**

Run: `.venv/bin/pytest tests/test_hub.py -k sanitize -v` → 全 PASS。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS（仅新增纯函数，无副作用）。

- [ ] **Step 6: 记录 diff**

---

## Task 6: G1 核心 — `DigestFields.last_error` + `publish()` session.error 分派 + sticky/clear

**Files:**
- Modify: `src/oc_slimapi/sse/hub.py`（`DigestFields` + `GlobalHub.sticky_last_error` + `publish()` 新分支 + `flush()` 合并 sticky + `session.status busy` clear + `session.deleted` 清除）
- Test: `tests/test_hub.py`（**改** `test_message_part_delta_produces_no_frames` + 新增 7 类 G1 测试）

**Interfaces:**
- Consumes: `_UNSET`、`ABORT_NAME`、`_sanitize_error_message`（Task 5）
- Produces: `DigestFields.last_error` 三态；`GlobalHub.sticky_last_error: dict[str, dict]`

**Acceptance Criteria:**
- `T6-C1`: `session.error` 带 sid（非 abort）→ 立即出 digest 含 `lastError:{name,message,at}`，**不调 `hub.flush()` 也要立即出现**（test `test_g1_a_immediate_flush_with_last_error`）。
- `T6-C2`: `session.error` 无 sid → 立即出 `event_name=="session.error"` 帧，payload 含 name/message（test `test_g1_b_session_less_frame`）。
- `T6-C3`: `error.name=="MessageAbortedError"` → 无任何帧（test `test_g1_abort_filtered`）。
- `T6-C4`: error → flush → 再发 unrelated `session.status` → 新 digest 仍带 lastError（sticky 跨窗口）（test `test_g1_sticky_across_windows`）。
- `T6-C5`: error → `session.status busy` → 出含 `"lastError": None` 的 digest；之后该 sid 新 digest 不再带 lastError（test `test_g1_clear_on_busy`）。
- `T6-C6`: error → `session.deleted` → digest 不含 lastError（test `test_g1_deleted_clears`）。
- `T6-C7`: 既有 `test_message_part_delta_produces_no_frames` 改写后 PASS（删 session.error 丢弃断言，改用 abort 仍丢）。
- `T6-C8`: `flush()` 合并 sticky（`fields.last_error is _UNSET and sid in sticky_last_error` → 回填）（code review）。

- [ ] **Step 1: 写失败测试（7 类）+ 改既有测试**

先改 `tests/test_hub.py` 的 `test_message_part_delta_produces_no_frames`（line 205–220）：把其中 `hub.publish(make_global_event("/proj", "session.error", {"sessionID": "s1"}))` 改为 abort event（仍应丢）：

```python
# 在该测试内，把 session.error 那行改为：
hub.publish(make_global_event("/proj", "session.error", {
    "sessionID": "s1",
    "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
}))
# 然后该测试仍断言无帧（abort 被过滤）
```

追加 G1 测试到 `tests/test_hub.py`（用既有 `make_global_event` / `parse_event` / `drain_queue` helpers，见 test_hub.py:37–74）：

```python
async def test_g1_a_immediate_flush_with_last_error():
    hub = GlobalHub(upstream_client_stub, directory="/p")  # 用既有 fixture 构造
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom at app.ts:1:1"},
                  "name": "UnknownError"},
    }))
    frames = await drain_queue(sub)
    digests = [f for f in frames if f.get("event") == "session.digest"]
    assert any(d["data"].get("lastError", {}).get("name") == "UnknownError" for d in digests)
    # message 经脱敏（剥 stack frame）
    assert all("app.ts" not in d["data"].get("lastError", {}).get("message", "") for d in digests)


async def test_g1_b_session_less_frame():
    hub = ...
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "error": {"name": "UnknownError", "data": {"message": "plugin load failed"}},
    }))
    frames = await drain_queue(sub)
    err_frames = [f for f in frames if f.get("event") == "session.error"]
    assert len(err_frames) == 1
    assert err_frames[0]["data"]["name"] == "UnknownError"
    assert err_frames[0]["data"]["message"] == "plugin load failed"
    assert err_frames[0]["data"].get("directory") == "/p"


async def test_g1_abort_filtered():
    hub = ...
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "sessionID": "s1",
        "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
    }))
    frames = await drain_queue(sub)
    assert not any(f.get("event") == "session.digest" and "lastError" in f["data"] for f in frames)
    assert not any(f.get("event") == "session.error" for f in frames)


async def test_g1_sticky_across_windows():
    hub = ...
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "sessionID": "s1", "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(sub)  # 清掉 immediate digest
    # 下一窗口前发 unrelated session.status
    hub.publish(make_global_event("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
    hub.flush()
    frames = await drain_queue(sub)
    digests = [f for f in frames if f.get("event") == "session.digest" and f["data"].get("sessionID") == "s1"]
    assert any(d["data"].get("lastError", {}).get("name") == "UnknownError" for d in digests)


async def test_g1_clear_on_busy():
    hub = ...
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "sessionID": "s1", "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(sub)
    hub.publish(make_global_event("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
    frames = await drain_queue(sub)
    clear_digests = [f for f in frames if f.get("event") == "session.digest"
                     and f["data"].get("sessionID") == "s1" and "lastError" in f["data"]]
    assert any(d["data"]["lastError"] is None for d in clear_digests)
    # 之后新 status 不再带 lastError
    hub.publish(make_global_event("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
    hub.flush()
    frames2 = await drain_queue(sub)
    digests2 = [f for f in frames2 if f.get("event") == "session.digest" and f["data"].get("sessionID") == "s1"]
    assert all("lastError" not in d["data"] for d in digests2)


async def test_g1_deleted_clears():
    hub = ...
    sub = await hub.subscribe()
    hub.publish(make_global_event("/p", "session.error", {
        "sessionID": "s1", "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(sub)
    hub.publish(make_global_event("/p", "session.deleted", {"sessionID": "s1"}))
    frames = await drain_queue(sub)
    digests = [f for f in frames if f.get("event") == "session.digest" and f["data"].get("sessionID") == "s1"]
    assert all("lastError" not in d["data"] for d in digests)
```

（测试用 fixture / helper 名以 `tests/test_hub.py` 既有为准；implementer 按实际签名对齐 `make_global_event` / `drain_queue` / `GlobalHub` 构造。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_hub.py -k g1 -v`
Expected: FAIL（G1 行为未实现）。

- [ ] **Step 3: 实现 G1 核心**

`src/oc_slimapi/sse/hub.py`：

3a. `DigestFields` 加字段（在 `deleted` 之后）：

```python
    last_error: Any = _UNSET  # three-state: _UNSET=omit, None=explicit clear, dict=object
```

`to_payload` 末尾追加：

```python
        if self.last_error is not _UNSET:
            payload["lastError"] = self.last_error
```

3b. `GlobalHub.__init__` 加持久层：

```python
        self.sticky_last_error: dict[str, dict] = {}  # sid -> lastError dict (cleared = popped)
```

3c. `flush()` 合并 sticky（在 `for sid, fields in snapshot.items():` 循环内，序列化前）：

```python
        for sid, fields in snapshot.items():
            if fields.last_error is _UNSET and sid in self.sticky_last_error:
                fields.last_error = self.sticky_last_error[sid]
            frame = sse_frame(fields.to_payload(sid), event="session.digest")
            for subscriber in tuple(self.subscribers):
                subscriber.put(frame)
            if self.subscribers:
                self.emitted_frames_total += len(self.subscribers)
```

3d. `publish()` 新增 `session.error` 分支（MESSAGE_EVENTS `return` 之后、catch-all 之前）：

```python
        elif event_type == "session.error":
            err = props.get("error") if isinstance(props, dict) else None
            err = err if isinstance(err, dict) else {}
            name = err.get("name")
            if name == ABORT_NAME:
                return  # abort 静默丢（impl-spec §7 行 166）
            raw_msg = ((err.get("data") or {}).get("message")) if isinstance(err.get("data"), dict) else None
            message = _sanitize_error_message(raw_msg, name)
            at = _now_ms()  # 复用 hub.py 既有 helper（line 356 已引用）；勿用 time.time()
            sid = props.get("sessionID") if isinstance(props, dict) else None
            if isinstance(sid, str) and sid:
                entry = self.pending.setdefault(sid, DigestFields())
                if entry.deleted:
                    return
                last_error_obj = {"name": (name or "")[:128], "message": message, "at": at}
                entry.last_error = last_error_obj
                self.sticky_last_error[sid] = last_error_obj
                self.flush()  # G1-A 立即触发
            else:
                # G1-B session-less 立即直推（不走 debounce）
                frame_payload = {"name": (name or "")[:128], "message": message, "at": at}
                if directory:
                    frame_payload["directory"] = directory
                frame = sse_frame(frame_payload, event="session.error")
                for subscriber in tuple(self.subscribers):
                    subscriber.put(frame)
                if self.subscribers:
                    self.emitted_frames_total += len(self.subscribers)
            return
```

3e. `session.status` 分支内追加 clear（在写 `entry.status` 之后）：

```python
            if props.get("status") == "busy" and sid in self.sticky_last_error:
                self.sticky_last_error.pop(sid, None)
                entry.last_error = None  # 显式 null → clear 帧
                self.flush()
```

3f. `session.deleted` 分支内追加清除（在 `entry.deleted = True` 之后）：

```python
            self.sticky_last_error.pop(sid, None)
            entry.last_error = _UNSET  # deleted digest 不含 lastError
```

（`_now_ms()` / `_UNSET` / `ABORT_NAME` / `_sanitize_error_message` 均 hub.py 既有/Task 5 新增，无需新增 import。）

- [ ] **Step 4: 跑定向测试 green**

Run: `.venv/bin/pytest tests/test_hub.py -v` → 全 PASS（含 G1 7 类 + 改写的 `test_message_part_delta_produces_no_frames`）。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS。

- [ ] **Step 6: 记录 diff**

---

## Task 7: G6 — `GET /slimapi/messages/{sid}/full?ids=` 批量展开

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py`（L435–436 间插 `@router.get("/full")`）
- Test: `tests/test_messages_routes.py`

**Interfaces:**
- Consumes: `_resolve_messages_directory`（messages.py）、`_raise_upstream_status`（sessions.py）、`read_with_cap` / `TransformPool` / `TransformBusy`（transform.py）、`skeleton_message`（skeleton.py）、`forward_directory_headers`（upstream.py）
- Produces: `GET /slimapi/messages/{sid}/full?ids=` 路由

**Acceptance Criteria:**
- `T7-C1`: `ids` 缺失 → 422（FastAPI）（test `test_g6_ids_missing_returns_422`）。
- `T7-C2`: `ids` 空 / >20 / 解析失败 → 400 `invalid_ids`（test `test_g6_ids_invalid_count`）。
- `T7-C3`: discover `/session/{sid}` 404 → 404 `session_not_found`，**0 个 mid 调用**（test 用 `calls["count"]` 断言，`test_g6_session_not_found_no_mid_fetch`）。
- `T7-C4`: mid 部分 404 → 200 + `items[]`（成功的）+ `errors[] message_not_found`（test `test_g6_partial_mid_failure`）。
- `T7-C5`: 全 mid 404 → 仍 200 + 全 `errors[]`（test `test_g6_all_mid_missing_still_200`）。
- `T7-C6`: 累计字节超限 → 413 `response_too_large`，`calls["count"]` 锁定在第 K 个 mid（test `test_g6_cumulative_byte_budget`，仿 `test_messages_since_enforces_cumulative_byte_budget`）。
- `T7-C7`: `items[]` 严格按 `ids` 去重后顺序（test `test_g6_items_strict_order`，用重复 id）。
- `T7-C8`: 路由顺序：`GET /full` 不被 `/full/{mid}` 吞（test `test_g6_route_not_shadowed`，请求 `/full?ids=m1` 不命中 `/full/{mid}`）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_messages_routes.py`（复用既有 `_settings` / `_build_app` / `upstream_factory` / `_msg`）：

```python
async def test_g6_ids_missing_returns_422(app_and_client):
    app, client = app_and_client(_settings())
    response = await client.get("/slimapi/messages/s1/full", headers=VERSION_HEADERS)
    assert response.status_code == 422


async def test_g6_ids_invalid_count(app_and_client):
    app, client = app_and_client(_settings())
    # 空（仅逗号/空白）
    r1 = await client.get("/slimapi/messages/s1/full?ids=,,", headers=VERSION_HEADERS)
    assert r1.status_code == 400 and r1.json()["code"] == "invalid_ids"
    # >20
    big = ",".join(f"m{i}" for i in range(21))
    r2 = await client.get(f"/slimapi/messages/s1/full?ids={big}", headers=VERSION_HEADERS)
    assert r2.status_code == 400 and r2.json()["code"] == "invalid_ids"


async def test_g6_session_not_found_no_mid_fetch(upstream_factory):
    calls = {"count": 0}
    def handler(request):
        calls["count"] += 1
        return httpx.Response(404, content=b'{"error":"no session"}')
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS)
        assert r.status_code == 404 and r.json()["code"] == "session_not_found"
        assert calls["count"] == 1  # 只 discover，没拉 mid
    finally:
        app.state.transforms.shutdown()


async def test_g6_partial_mid_failure(upstream_factory):
    def handler(request):
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        if request.url.path == "/session/s1/message/m_ok":
            return httpx.Response(200, content=orjson.dumps(_msg("m_ok", 100)))
        if request.url.path == "/session/s1/message/m_missing":
            return httpx.Response(404, content=b'{}')
        return httpx.Response(404)
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m_ok,m_missing", headers=VERSION_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert any(e["code"] == "message_not_found" and e["messageID"] == "m_missing" for e in body["errors"])
    finally:
        app.state.transforms.shutdown()


async def test_g6_all_mid_missing_still_200(upstream_factory):
    def handler(request):
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        return httpx.Response(404)
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert len(body["errors"]) == 2
    finally:
        app.state.transforms.shutdown()


async def test_g6_cumulative_byte_budget(upstream_factory):
    calls = {"count": 0}
    # max_response_bytes=64KiB；每 mid body ~稍大；累计第 2 个超限
    def handler(request):
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        calls["count"] += 1
        return httpx.Response(200, content=orjson.dumps(_msg("m", 100, text="y" * 40000)))
    upstream = upstream_factory(handler)
    app = _build_app(_settings(max_response_bytes=64 * 1024), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m1,m2", headers=VERSION_HEADERS)
        assert r.status_code == 413 and r.json()["code"] == "response_too_large"
        # 第 2 个 mid 触发累计超限（discover 不计）
    finally:
        app.state.transforms.shutdown()


async def test_g6_items_strict_order(upstream_factory):
    def handler(request):
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        mid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=orjson.dumps(_msg(mid, 100)))
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m3,m1,m3,m2", headers=VERSION_HEADERS)
        body = r.json()
        ids = [m["info"]["id"] for m in body["items"]]
        assert ids == ["m3", "m1", "m2"]  # 去重保序
    finally:
        app.state.transforms.shutdown()


async def test_g6_route_not_shadowed(upstream_factory):
    def handler(request):
        if request.url.path == "/session/s1":
            return httpx.Response(200, content=orjson.dumps({"id": "s1"}))
        return httpx.Response(200, content=orjson.dumps(_msg("m1", 100)))
    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/slimapi/messages/s1/full?ids=m1", headers=VERSION_HEADERS)
        # 若被 /full/{mid} 吞，会 422（{mid} 缺）或不同行为；这里断言 200 + envelope
        assert r.status_code == 200
        assert "items" in r.json()
    finally:
        app.state.transforms.shutdown()
```

注：`app_and_client` / `_msg` 签名以 `tests/test_messages_routes.py` 既有为准（implementer 对齐）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/pytest tests/test_messages_routes.py -k g6 -v`
Expected: FAIL（路由不存在 → 404 `thin_route_not_found`）。

- [ ] **Step 3: 实现 `message_batch` 路由**

`src/oc_slimapi/routes/messages.py`，在 list 端点（`@router.get("")` 结束）与 `@router.get("/full/{mid}")` 之间（约 L435–436）插入：

```python
@router.get("/full")
async def message_batch(
    request: Request,
    sid: str,
    ids: str,
    mode: Literal["skeleton", "full"] = "full",
    directory: str | None = None,
):
    """G6 batch multi-mid expand (impl-spec §8). discover-first; mid-level
    partial failures into errors[]; cumulative byte budget 413. Registered
    BEFORE /full/{mid} per spec MUST (segment count differs so no actual
    collision, but order is spec-mandated)."""
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    # ids parse: split + strip + dedupe保序 + 1–20 guard (no charset check)
    order = list(dict.fromkeys(s.strip() for s in ids.split(",") if s.strip()))
    if not order or len(order) > 20:
        raise CodedHTTPException(400, code="invalid_ids")

    config = request.app.state.config
    pool = request.app.state.transforms

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
        _raise_upstream_status(exc, sid=sid)  # 404→session_not_found (no mid fetch); 其它→502/503

    sem = asyncio.Semaphore(4)
    state = {"total": 0, "aborted": False}
    succeeded: dict[str, dict] = {}
    errors: list[dict] = []

    async def fetch_one(mid: str):
        if state["aborted"]:
            return
        async with sem:
            upstream_request = request.app.state.upstream.build_request(
                "GET", f"/session/{sid}/message/{mid}",
                headers=forward_directory_headers(directory),
            )
            try:
                response = await request.app.state.upstream.send(upstream_request, stream=True)
            except httpx.RequestError:
                state["aborted"] = True
                return
            try:
                if response.status_code == 404:
                    errors.append({"messageID": mid, "code": "message_not_found"})
                    return
                if response.status_code >= 400:
                    errors.append({"messageID": mid, "code": f"upstream_http_{response.status_code}"})
                    return
                remaining = config.max_response_bytes - state["total"]
                mid_cap = min(config.max_message_bytes, max(0, remaining))
                try:
                    body, n = await read_with_cap(response, mid_cap)
                except httpx.RequestError:
                    state["aborted"] = True
                    return
            finally:
                await response.aclose()
        if body is None:
            if n > config.max_message_bytes:
                errors.append({"messageID": mid, "code": "message_too_large"})
                return
            state["aborted"] = True
            return
        state["total"] += n
        if mode == "skeleton":
            projected = await pool.offload(lambda b=body: skeleton_message(orjson.loads(b)))
            succeeded[mid] = projected
        else:
            succeeded[mid] = orjson.loads(body)

    try:
        if mode == "skeleton":
            async with pool:
                await asyncio.gather(*(fetch_one(mid) for mid in order))
        else:
            await asyncio.gather(*(fetch_one(mid) for mid in order))
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))

    if state["aborted"]:
        return error_response("response_too_large", 413, limit=config.max_response_bytes,
                              accept_encoding=request.headers.get("accept-encoding"))

    items = [succeeded[mid] for mid in order if mid in succeeded]
    return json_response({"items": items, "errors": errors},
                         headers={"Cache-Control": "no-store"},
                         accept_encoding=request.headers.get("accept-encoding"))
```

注：顶部需 `import asyncio`（若未有）；`error_response` / `json_response` 已 import；`_raise_upstream_status` 从 `sessions` import。

- [ ] **Step 4: 跑定向测试 green**

Run: `.venv/bin/pytest tests/test_messages_routes.py -k g6 -v` → 全 PASS。

- [ ] **Step 5: 全量 green**

Run: `./scripts/check.sh` → 全 PASS。

- [ ] **Step 6: 记录 diff**

---

## Task 8: 契约文档 — `v1-contract.md` 全面更新

**Files:**
- Modify: `docs/v1-contract.md`

**Acceptance Criteria:**
- `T8-C1`: 头部 changelog +`2026-07-19 · v1（additive）`（F1/F2/F3/G1/G6）。
- `T8-C2`: §1 加 `[1,1]` 闭区间说明（F5）。
- `T8-C3`: §2 端点表：q/p directory 可选；per-session status sid 自洽；**新增 G6 行** `GET /slimapi/messages/{sid}/full?ids=`。
- `T8-C4`: §3 digest 字段加 `lastError?`；新增 `event: session.error` 帧定义（G1）。
- `T8-C5`: §4 cold-start 含启动暖机 + null q/p 顺序。
- `T8-C6`: §7 错误码：加 `invalid_ids`(400) / `message_not_found`(404 envelope)（G6）；注明 `directory_not_allowed` 不再适用 per-session status（F2）。
- `T8-C7`: 新增 §「directory 三态语义表」+ §「allowlist 机制」（§5 建议 1/2）。
- `T8-C8`: §11 待补缺口标 closed（D6）。
- `T8-C9`: 头部加 CLIENT_CHANGES 同步纪律（§5 建议 3）。

- [ ] **Step 1: 编辑 `docs/v1-contract.md`**

1. **头部 changelog** 追加：
   > - **2026-07-19 · v1（additive）**：`/slimapi/questions`+`/permissions` 的 `directory` 改可选（null=聚合 allowlist）；`/slimapi/sessions/{sid}/status` 放宽 allowlist（sid 自洽）；sidecar 启动主动 warm `/project` 暖 allowlist；routeToken 应答路径 allowlist miss 自动刷新；**新增 G1** `session.digest` 加 `lastError?` 字段 + 新 `event: session.error` session-less 帧；**新增 G6** `GET /slimapi/messages/{sid}/full?ids=` 批量展开端点（envelope + mid 级部分失败）。均加性，**不** bump `X-Slimapi-Version`。

2. **§1** `ACCEPTED_CLIENT_VERSIONS=(1,1)` 句后加：
   > `accepted:[1,1]` 是闭区间 `[min,max]`，当前 `min=max=1`（仅接受整数 `1`）。

3. **§2 端点表**：
   - q/p 行说明 → `跨目录聚合 pending（?directory repeated 1-32 **可选**；null=聚合 allowlist 全部 dir），每条带 routeToken`。
   - per-session status 行说明 → `单 ses status（id→directory 自洽；**sid 为能力凭证，不受 allowlist 约束**）`。
   - **新增行**（在 `/full/{mid}` 行之后）：`| GET | /slimapi/messages/{sid}/full?ids= | A | 🔒 (G6 🆕) | 批量展开（1–20 mid，discover 先行，mid 级 envelope errors[]，累计 413）|`。

4. **§3 SSE 契约**：`session.digest` 字段加 `lastError?`：
   > `lastError`←`session.error` 经脱敏后的 `{name,message,at}`；sticky 跨窗口，`status=busy` 清除（显式 null 帧），`deleted=true` 后不保留。
   
   新增帧类型：
   > - `session.error`（G1-B，无 sid 时立即直推）：`{directory?,name,message,at}`。abort（`MessageAbortedError`）静默丢弃。

5. **§4 cold-start** 整节替换为（spec §6.4 plan T8 Step 1.4 内容）：
   ```
   - sidecar 启动暖机：lifespan 调一次 /project 预热 allowlist（best-effort）。
   - 客户端冷启动顺序：(1) 可选 GET /slimapi/projects；(2) GET /slimapi/sessions（null OK）；(3) GET /slimapi/questions + /permissions（directory 可选；null=聚合 allowlist）；(4) GET /slimapi/messages/{sid}/since/{ts}。
   - resync = 复用冷启动流程。
   ```

6. **§7 错误码**：
   - 加 `| 400 | invalid_ids | G6 batch ids 空/超 20/解析失败 |` 与 `| 404 | message_not_found | G6 envelope errors[] mid 级 404 |`。
   - 在 `directory_not_allowed` 行后加：「不再适用 per-session status（F2）；仍适用 sessions 显式 dir / 批量 status / q/p 显式 dir / messages soft / routeToken 刷新后 miss」。

7. **新增 §12 directory 三态语义表 + §13 allowlist 机制**（用 spec §5.2 / impl-spec 内容；plan Task 5/T8 的 directory 表与 allowlist 节，照 spec §13 与本 plan 既有结构）。

8. **§11** 待补缺口每项标 `✅ 已闭环（impl-status §11）`，去掉「驱动 lane 派发」措辞。

9. **头部** CLIENT_CHANGES 同步纪律：
   > **同步纪律**：本文件 changelog 条目须同时列出受影响的 `docs/CLIENT_CHANGES.md` 小节。

- [ ] **Step 2: 校验**

Run: `./scripts/check.sh` → 全 PASS（文档无副作用）。Manual: reviewer 核各节 + 交叉引用。

- [ ] **Step 3: 记录 diff**

---

## Task 9: 配套文档 — `design-v2.md` + `v1-impl-spec.md` + `AGENTS.md`（D1–D8 + G1/G6）

**Files:**
- Modify: `docs/design-v2.md`（D1–D5 + G1/G6）
- Modify: `docs/v1-impl-spec.md`（D7 + G1/G6 状态）
- Modify: `AGENTS.md`（D8）

**Acceptance Criteria:**
- `T9-C1`: design-v2 §1.4 `limit 0→400` 改 422（D3）。
- `T9-C2`: design-v2 §1.7 q/p directory 改可选（D1）。
- `T9-C3`: design-v2 §1.9 status 同步 B1 三态 + F2 放宽（D2）。
- `T9-C4`: design-v2 §1.10「全部丢弃」列表删 `session.error`（G1）。
- `T9-C5`: design-v2 §3 line 160 SSEClient 改 `/slimapi/events`（D4）；line 162 删 `thin.session.dirty`（D5）。
- `T9-C6`: design-v2 新增 §1.x G6 批量端点节（参数 + envelope + discover 先行 + 失败语义）。
- `T9-C7`: v1-impl-spec §1 B0 决策记录为 GO（D7）；§7 G1 标 ✅ 已实现；§8 G6 标 ✅ 已实现。
- `T9-C8`: AGENTS.md「当前对齐版本」改 `v1.18.3`（D8）。

- [ ] **Step 1: 编辑 `docs/design-v2.md`**

- §1.4 line 45：`limit(int **1–200** 默认40，**0→400**)` → `limit(int **1–200** 默认40，**0→422** FastAPI ge=1)`。
- §1.7 line 68：`directory(repeated，?directory=/a&directory=/b，每项∈allowlist，去重 ≤32)` → `directory(repeated，**可选**；显式传时 ?directory=/a&directory=/b 每项∈allowlist 去重 ≤32；null=聚合 allowlist 全部 dir)`。
- §1.9 line 87：`单 sid：...fan-out 失败→503` → `单 sid：discover /session/{sid}（B1 三态：404→session_not_found / 其它 4xx→502 / 5xx→503）；**F2：放宽 allowlist，sid 自洽即能力，normalize 不 gate**`。
- §1.10 line 110：`全部丢弃：message.part.delta/.updated/.removed、tool.*、session.error、message.removed、未知类型` → 删 `session.error`（改：`全部丢弃：message.part.delta/.updated/.removed、tool.*、message.removed、未知类型（注：session.error 经 G1 处理，见 §1.x）`）。
- §3 line 160：`SSEClient.kt：URL /global/event→/event；裸帧归一化...` → `SSEClient.kt：连接单一 /slimapi/events（**无 query 参数**，v2 全实例聚合）；curated 帧解析（session.digest / session.error / question.* / permission.* / heartbeat / resync）`。
- §3 line 162：`增量 reducer：处理 thin.session.dirty...` → `增量 reducer：处理 session.digest（debounced 时间戳锚点拉取 /since/{ts}）、event:resync（前台 catch-up）、event:session.error（G1，UI banner/toast）`。
- 新增 §1.13 G6：
  ```
  ### 1.13 GET /slimapi/messages/{sid}/full?ids=（批量展开，G6）
  - 参数：sid；ids(query, 必填, 逗号分隔 messageId 1–20, 去重保序)；mode(skeleton|full 默认 full)；directory(soft)。
  - discover 先行：GET /session/{sid}（带 directory 头）；404→404 session_not_found（不拉 mid）；其它 4xx→502；5xx→503。
  - 并发 ≤4 拉 N mid；mid 404→errors[] message_not_found；mid >max_message_bytes→errors[] message_too_large；累计 >max_response_bytes→413 response_too_large 中止；全 mid 404 仍 200+全 errors。
  - items[] 严格按 ids 去重后序；Cache-Control:no-store。
  ```

- [ ] **Step 2: 编辑 `docs/v1-impl-spec.md`**

- §1 B0 决策节：在 B0 描述后追加：
  > **B0 决策结果（2026-07-19，GO）**：经 opencode v1.18.3 源码核验，`session.error` 实发于 `/global/event`（`schema/src/v1/session.ts:651-657` + `event-v2-bridge.ts:35-44` + `handlers/global.ts:36-52`），`sessionID` optional，abort name=`MessageAbortedError`（TUI `app.tsx:1021` 同名过滤）。G1 按 §7 实现。
- §7 标题加 `**[✅ 已实现 2026-07-19]**`；§8 标题加同。
- §1 B4 / §8 末尾加「**[✅ 已实现 2026-07-19，GET /slimapi/messages/{sid}/full?ids=]**」。

- [ ] **Step 3: 编辑 `AGENTS.md`**

「当前对齐版本」节：`opencode v1.17.20` → `opencode v1.18.3`（与 `current` 实链 + impl-spec.md:68 一致）。

- [ ] **Step 4: 校验**

Run: `./scripts/check.sh` → 全 PASS。Manual: reviewer 核交叉引用。

- [ ] **Step 5: 记录 diff**

---

## Task 10: 配套文档 — `CLIENT_CHANGES.md`(F4) + `INTERFACE_MAP.md` + impl-status + `CHANGELOG.md`

**Files:**
- Modify: `docs/CLIENT_CHANGES.md`（F4 SSE + G1/G6）
- Modify: `docs/INTERFACE_MAP.md`（§0/§1/§3/§7）
- Modify: `docs/v1-contract-implementation-status.md`
- Modify: `CHANGELOG.md`

**Acceptance Criteria:**
- `T10-C1`: CLIENT_CHANGES SSE 节重写，无 `?directory/sessionId/stream`（F4）；新增 G1 `lastError`/`session.error` + G6 batch 端点客户端说明。
- `T10-C2`: INTERFACE_MAP §0 `normalize_directory`；§1 表 q/p / status / G6 行；§3 加 `session.error`；§7 G2 status allowlist 行改 F2 放宽。
- `T10-C3`: impl-status §2 表 q/p/status/G6 行；§3 digest `lastError`；诚实声明 routeToken 改已修复；G1/G6 落地条目。
- `T10-C4`: CHANGELOG `[Unreleased]` Added（F1 null / F3 warm-up / G1 / G6）/ Changed（F2 / F3 routeToken）/ Fixed（F4/F5/§5/D1–D8）。

- [ ] **Step 1: `docs/CLIENT_CHANGES.md` SSE 节重写 + G1/G6**

替换 `## SSE` 整节（line 54–67）为：

```markdown
## SSE

- 连接单一 `GET /slimapi/events`（**无 query 参数**——`directory`/`sessionId`/`stream` 在 v2 重写后已完全移除；全实例、全目录聚合，每事件自带 `directory`）。
- curated 帧类型：
  - `session.digest`（debounce 250ms/session）：`{sessionID,directory,status?,messageID?,updatedAt?,archived?,deleted?,lastError?}`。
  - **`session.error`（G1，立即直推，无 sid 时）**：`{directory?,name,message,at}`。客户端 UI：有 sid 已含在 digest 的 `lastError`（该 session banner）；无 sid → 全局 toast。
  - `question.asked`/`v2.asked`、`permission.asked`/`resolved`/`v2.asked`/`v2.resolved`（立即直推）。
  - `server.connected`（订阅即吐）、`server.heartbeat`（10s）、`resync`（重连 `{"reason":"reconnect_no_replay"}`，**无 replay**）。
- digest `lastError`：sticky 跨窗口，`status=busy` 清除（显式 `null` 帧）；客户端据此显隐 session 错误 banner。`MessageAbortedError` 被 sidecar 过滤，不下发。
- 客户端所有 `/slimapi/**` 请求（含 SSE）须带 `X-Slimapi-Version: 1`；连接时读 `/slimapi/health` 自检。

## 批量展开（G6，新）

- `GET /slimapi/messages/{sid}/full?ids=m1,m2,...`（1–20 mid，逗号分隔，去重保序）：批量展开多条 message。
- 响应 envelope：`{"items":[...], "errors":[{"messageID":..,"code":"message_not_found|message_too_large|upstream_http_N"}]}`；**mid 级部分失败仍 200 + errors[]**；全 mid 404 仍 200。
- session 不存在 → 404 `session_not_found`（top-level，非 envelope）；ids 缺失→422；空/超 20/解析失败→400 `invalid_ids`。
- 累计字节超限 → 413 `response_too_large`（整请求，非单 mid）。
- 推荐使用此端点替代「N 并行 `/full/{mid}`」（ocdroid 现走 404 fallback，升级后首调即 200）。
```

- [ ] **Step 2: `docs/INTERFACE_MAP.md`**

- §0 `require_directory` 描述改：
  > `require_directory()` ... allowlist miss 时刷新一次。**per-session `GET /slimapi/sessions/{sid}/status` 不走此 gate（F2 放宽，用 `normalize_directory` 仅规范化）**。新增 `normalize_directory()` 纯函数（`require_directory` 复用）。
- §1 表：
  - q/p 行参数列：`directory:list[str]? repeated query，**可选**；显式传去重 1–32；null=聚合 allowlist`。
  - per-session status 行：注明 F2 `normalize_directory` 不 gate。
  - 新增 G6 行（在 `/full/{mid}` 行后）：参数/上游/sidecar 处理/返回/坑（discover 先行、envelope、mid 级 errors、累计 413、路由注册先于 `/full/{mid}`）。
- §3 events 行：吐出帧加 `session.error`（G1-B）+ digest 字段加 `lastError?`。
- §7 G2 节：status `directory ∉ allowlist → 400` 行改为 `→ 200（F2 放宽）`。

- [ ] **Step 3: `docs/v1-contract-implementation-status.md`**

- §2 表：q/p 行 `🔄 F1：directory 可选`；per-session status 行 `🔄 F2：放宽 allowlist`；新增 G6 行 `🆕 G6：批量展开`；新增 G1 落地（§3 行）。
- §3 digest 字段加 `lastError?`；新增 `session.error` 帧说明（G1）。
- 「诚实声明」routeToken 条目（line 237）改为：
  > - **routeToken-allowlist 时序（F3 已修复）**：sidecar `lifespan` 启动 warm `/project`；`_token` 走 `require_directory`（miss 自动刷新）。冷启动空窗不再致首个合法 reply 400。
- 速查总表 relevant 行更新。

- [ ] **Step 4: `CHANGELOG.md` `[Unreleased]`**

替换空 Added/Changed/Fixed 节为：

```markdown
## [Unreleased]

> 本批次（2026-07-19）所有变更加性，**不** bump `X-Slimapi-Version`（仍为 `1`）。

### Added

- **F1 `/slimapi/questions` + `/permissions` null directory 聚合**：`directory` 由必填改可选；不传时聚合 allowlist 全部 dir。消除 cold-start 422。
- **F3 allowlist 启动暖机**：`lifespan` 启动主动 `load_products`（best-effort）。
- **G1 错误可见性**：`session.digest` 加 `lastError?` 字段（`{name,message,at}`，sticky，`status=busy` 清除，`deleted` 后不保留）；新 `event: session.error` session-less 帧（无 sid 时立即直推）；`MessageAbortedError` 静默过滤；message 脱敏（首行/剥路径/剥 stack/剥 secret/截断 512）。
- **G6 批量展开**：`GET /slimapi/messages/{sid}/full?ids=`（1–20 mid，discover 先行，mid 级 envelope errors[]，累计 413）。

### Changed

- **F2 `/slimapi/sessions/{sid}/status` 放宽 allowlist**：sid 自洽即能力，`normalize_directory` 不 gate；与 messages soft 对齐。批量 status 不变。
- **F3 routeToken 应答 allowlist 刷新**：`_token` 走 `require_directory`（miss 自动刷新）。

### Fixed

- **F4 文档**：`CLIENT_CHANGES.md` SSE 节同步 INTERFACE_MAP §3。
- **F5 文档**：契约 §1 `accepted:[1,1]` 闭区间说明。
- **§5 文档**：契约新增 directory 三态语义表 + allowlist 机制节 + cold-start 暖机 + CLIENT_CHANGES 同步纪律。
- **D1–D8 文档**：design-v2（§1.4 limit 422 / §1.7 q/p 可选 / §1.9 status / §1.10 删 session.error / §3 SSEClient + 删 thin.session.dirty）、impl-spec（B0 决策记录 GO / G1·G6 标已实现）、AGENTS.md（对齐版本 v1.18.3）、契约 §11 标 closed。
```

- [ ] **Step 5: 校验**

Run: `./scripts/check.sh` → 全 PASS。Manual: reviewer 核 4 文件 + 交叉引用。

- [ ] **Step 6: 记录 diff**

---

## Criterion Ownership Matrix

| Criterion ID | Spec req | Owner | Deps | Verification | Final-only? |
|---|---|---|---|---|---|
| T1-C1 | F3 load_products(app) | T1 | — | `pytest ...::test_load_products_takes_app_state` PASS | N |
| T1-C2 | F3 warm_allowlist 吞错 | T1 | — | `pytest ...::test_warm_allowlist_swallows_upstream_error` PASS | N |
| T1-C3 | T1 无回归 | T1 | — | `pytest ...::test_projects_*` PASS | N |
| T1-C4 | F3 lifespan 暖机 | T1 | — | code review app.py | Y |
| T2-C1 | F3 routeToken cold 刷新 | T2 | T1 | `pytest ...::test_token_refreshes_cold_allowlist_then_reply` 204 | N |
| T2-C2 | F3 不可发现仍 400 | T2 | T1 | `pytest ...::test_questions_token_directory_not_allowed` PASS | N |
| T2-C3 | F3 _token async | T2 | — | code review | Y |
| T3-C1 | F1 null 聚合 | T3 | T1 | `pytest ...::test_questions_null_directory_aggregates_allowlist` 200 | N |
| T3-C2 | F1 null 空 envelope | T3 | T1 | `pytest ...::test_questions_null_directory_empty_allowlist_returns_empty_envelope` 200 | N |
| T3-C3 | F1 explicit 不变 | T3 | — | `pytest ...::test_questions_directory_count_bounds` PASS | N |
| T3-C4 | F1 permissions null | T3 | — | code review | Y |
| T4-C1 | F2 status 放宽 200 | T4 | — | `pytest ...::test_status_allowlist_miss_relaxed_returns_status` 200 | N |
| T4-C2 | F2 allowlisted 不变 | T4 | — | `pytest ...::test_status_map_missing_sid_returns_idle` PASS | N |
| T4-C3 | F2 批量不变 | T4 | — | `pytest ...::test_batch_status_allowlist_miss_renders_code` PASS | N |
| T4-C4 | F2 normalize_directory | T4 | — | code review | Y |
| T5-C1..C7 | G1 脱敏 golden | T5 | — | `pytest tests/test_hub.py -k sanitize` PASS | N |
| T6-C1 | G1-A 立即 flush | T6 | T5 | `pytest ...::test_g1_a_immediate_flush_with_last_error` PASS | N |
| T6-C2 | G1-B session-less | T6 | T5 | `pytest ...::test_g1_b_session_less_frame` PASS | N |
| T6-C3 | G1 abort 过滤 | T6 | T5 | `pytest ...::test_g1_abort_filtered` PASS | N |
| T6-C4 | G1 sticky 跨窗口 | T6 | T5 | `pytest ...::test_g1_sticky_across_windows` PASS | N |
| T6-C5 | G1 clear on busy | T6 | T5 | `pytest ...::test_g1_clear_on_busy` PASS | N |
| T6-C6 | G1 deleted 清除 | T6 | T5 | `pytest ...::test_g1_deleted_clears` PASS | N |
| T6-C7 | 既有 delta 测试改写 | T6 | T5 | `pytest ...::test_message_part_delta_produces_no_frames` PASS | N |
| T6-C8 | flush 合并 sticky | T6 | T5 | code review | Y |
| T7-C1 | G6 ids 缺失 422 | T7 | — | `pytest ...::test_g6_ids_missing_returns_422` PASS | N |
| T7-C2 | G6 ids invalid | T7 | — | `pytest ...::test_g6_ids_invalid_count` PASS | N |
| T7-C3 | G6 discover 先行 | T7 | — | `pytest ...::test_g6_session_not_found_no_mid_fetch` PASS | N |
| T7-C4 | G6 部分 mid 失败 | T7 | — | `pytest ...::test_g6_partial_mid_failure` PASS | N |
| T7-C5 | G6 全 mid 404 仍 200 | T7 | — | `pytest ...::test_g6_all_mid_missing_still_200` PASS | N |
| T7-C6 | G6 累计 413 | T7 | — | `pytest ...::test_g6_cumulative_byte_budget` PASS | N |
| T7-C7 | G6 items 定序 | T7 | — | `pytest ...::test_g6_items_strict_order` PASS | N |
| T7-C8 | G6 路由不被吞 | T7 | — | `pytest ...::test_g6_route_not_shadowed` PASS | N |
| T8-C1..C9 | 契约文档 | T8 | T1–T7 | manual review | Y |
| T9-C1..C8 | design-v2/impl-spec/AGENTS | T9 | T8 | manual review | Y |
| T10-C1..C4 | CLIENT_CHANGES/INTERFACE_MAP/impl-status/CHANGELOG | T10 | T8 | manual review | Y |
| **跨** | spec §2 全量 green | All | — | `./scripts/check.sh` EXIT=0 FAILURES=0 | **Y** |
| **跨** | spec §4.1 不 bump | All | — | `versioning.py` `ACCEPTED_CLIENT_VERSIONS==(1,1)` 未改 | **Y** |

---

## Self-Review

1. **Spec coverage**：F1→T3；F2→T4；F3→T1+T2；F4→T10；F5→T8；§5→T8；G1→T5+T6；G6→T7；D1–D5→T9；D6→T8；D7→T9；D8→T9。spec §5 文件清单全覆盖。
2. **Placeholder scan**：无 TBD/TODO；每步含实际代码或文档节；测试代码完整可跑（部分 helper 名标注「以既有为准」是合理 — implementer 读 conftest/既有测试对齐，非占位）。
3. **Type consistency**：`load_products(app)` T1 定、T3/T1 调；`warm_allowlist` T1 定、app.py 调；`_token` async T2 定、3 caller T2 await；`normalize_directory` T4 定、`require_directory`+`session_status` T4 复用；`_UNSET`/`ABORT_NAME`/`_sanitize_error_message` T5 定、T6 用；`sticky_last_error` T6 定、T6 用。
4. **Acceptance observability**：每条 = `pytest::test → 期望` 或 `code review <what>` 或 `manual review <section>`；无模糊语。
5. **风险复扫**：G1 sticky 用 flush-合并（非 reseed），与 spec §4.4 一致且更正确；G6 累计字节在 `Semaphore(4)` 临界段外累加可能有轻微欠计（测试用低 cap 压测，acceptable — 单 mid cap 仍精确）；G6 discover 与 mid 404 语义分离由 calls-count 测试锁定；G1 `_extract_session_id` 不用、显式取 `props.sessionID`（exp-1 确认 schema）。
