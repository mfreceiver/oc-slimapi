# opencode 原生 web UI 能力基线（2026-08-17，exp-w 探索）

> 对象：opencode-src/current 的 `packages/app`（@opencode-ai/app）。角色：**对标基线**（非消费者）——「不逊于原生 web」的差距清单来源。

## ① UI 包与技术栈

- **UI**：`packages/app` — Vite + **SolidJS** SPA + TailwindCSS + @tanstack/solid-query + @solidjs/router；组件库 `packages/ui`；消息/差异组件 `packages/session-ui`
- **API 客户端**：`packages/client`（生成）+ `packages/sdk-next`（v2 typed）
- **服务方式**（ui.ts:78-107）：内嵌模式（构建时生成 opencode-web-ui.gen.ts 静态映射）/ 代理模式（降级反代 app.opencode.ai）；uiRoute catch-all `GET /*` 挂在全部 API 路由后（server.ts:194-203）

## ② Feature→API 映射（按域精简）

| 域 | 端点集 |
|---|---|
| 核心会话流 | `GET /api/session`（cursor 分页）、`POST /api/session`、`GET /api/session/:id`、`POST .../prompt`、`POST .../interrupt`、`POST .../wait`、`GET .../history?after=`、`GET .../message?cursor=`、`GET .../message/:mid`、`GET .../event?after=`（会话 SSE）、`GET /api/event`（全局 SSE）、`GET /api/session/active`、`GET /api/health`（10s 轮询） |
| 会话管理 | compact / agent / model 切换 / **revert 三段式（stage/clear/commit）** / fork / archive（v2）/ rename / delete |
| 上下文用量 | `GET /api/session/:id/context` |
| 文件 | `GET /api/fs/list`（树）、`GET /api/fs/find`（搜索） |
| VCS | `GET /api/vcs/status`、`GET /api/vcs/diff?mode=git|branch` |
| 终端 PTY | CRUD ×5 + WebSocket（ticket 鉴权） |
| Provider/OAuth | model/provider/integration 列表 + connect/key|oauth + attempt 状态机 + credential PATCH/DELETE |
| 权限/问题 | permission request/saved/session 级 CRUD + question request/reply/reject |
| 辅助 | agent/command/skill/reference 列表、location、project 列表/current、experimental project copy |

## ③ 移动端/流量适配现状

- **PWA**：manifest（standalone + maskable icons）+ apple touch + viewport-fit=cover + safe-area；**无 service worker**
- **响应式**：media query 双端布局、移动滑出 sidebar、底部 tab（session|changes）、titlebar 位置可调、touch 事件处理
- **流量优化**：SSE 16ms 帧内 coalesce（coalesceServerEvents server-sdk.tsx:79-139）、虚拟滚动（solid-virtual）、cursor 分页、TanStack Query 缓存；**无压缩协商/图片懒加载/代码分割（除 NewSession）**——局域网假设，不在意流量

## ④ 对 slimapi 的对标差距清单

**slimapi 已对齐**：会话列表/创建/prompt/中断/消息历史/事件 SSE/文件/VCS（经 /slimapi 读组）。

**原生 web 有而 slimapi 未收编**（按两消费者需求重排优先级）：

| 能力 | 端点 | 移动端价值 |
|---|---|---|
| 会话 fork | `POST /api/session/:id/fork` | **已有 slimapi 路由**（write_groups fork）——对标无缺口，仅 v2 形态差异 |
| compact/summarize | v2 compact | slimapi 已有 summarize——无缺口 |
| revert 三段式 | stage/clear/commit | slimapi 已有单发 revert——形态差异（v2 更安全：stage→预览→commit） |
| 切换 agent/model | `POST .../agent` `/model` | **缺口**：ocdroid 现用 create 时指定 + 无运行中切换；中价值 |
| context 用量 | `GET .../context` | **缺口**：ocdroid ChatContextUsageDialog 现依赖估算；中价值（配额感知） |
| fs/find 内容搜索 | `/api/fs/find` | slimapi 已有 find/file；**文件名 vs 全文** 差异待确认 |
| PTY | CRUD+WS | **不做**（移动端无终端场景，webui 也未要求） |
| Provider OAuth | 完整流 | **不做**（桌面端完成配置；移动/web 远程场景仅查看） |
| skill/reference 列表 | GET | 低优先（command 已覆盖主场景） |
| 会话导出 | export | 低优先 |

**原生 web 的优化手法可借鉴**：SSE coalesce（16ms 帧合并）sidecar 已有更强 digest；cursor 分页方向一致。

## ⑤ 结论

原生 web 的能力面 ≈ v2 `/api/**` 全集。slimapi 核心（会话/消息/SSE/文件/VCS）已对齐且有更强的省流投影；**真实缺口 = 运行中 agent/model 切换、context 用量、revert 三段式**；PTY/OAuth/Project copy 明确不做。
