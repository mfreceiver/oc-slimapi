# 下一批接口收编评估：流量 × 需求 × 上游三方交叉（2026-08-17）

> 证据源：① `docs/api-census-opencode-2026-08-17.md`（上游 v1.18.18 普查）② `docs/webui-needs-audit-2026-08-17.md`（webui 审计）③ 本仓 access log / traffic-snapshot 实测（2026-08-14~17）
> 决策输入：P0/P1/P2 排名 + 专有/通用归属 + webui bug 与真实缺口区分 + 客户端归因现状

---

## 0. 流量实测基线（2026-08-17 snapshot，当日累计）

| bucket | upIn | downOut | reqs | 省流比 |
|---|---|---|---|---|
| messages | 75,354,499 | 2,392,802 | 108 | 3.2% |
| events_sse | 12,824,920 | 62,780 | 10 | 0.5% |
| sessions | 10,820,126 | 266,490 | 8,959 | 2.5% |
| questions | 8,320,017 | 12,231 | 151 | 0.1% |
| other | 2,376,652 | 5,899 | 55 | 0.2% |
| agent | 2,187,342 | 5,068 | 51 | 0.2% |
| command | 1,928,052 | 4,136 | 51 | 0.2% |
| providers | 28,547 | 2,432 | 1 | 8.5% |
| write_session | 28,319 | 16,525 | 49 | 58.4% |
| session_single | 2,084 | 691 | 2 | 33.2% |
| **TOTAL** | **113,870,558** | **2,771,613** | **9,447** | **2.43%** |

**4 日请求热度（access log 2026-08-14~17，ocdroid 为主）**：`GET /slimapi/sessions` 40,539 次 / `GET /slimapi/sessions/status` 30,587 次 / `GET /slimapi/questions` 5,217 次 / `GET /slimapi/messages/{id}` 4,936 次 / agent+command 各 ~1,745 次。

**404 缺口信号（微弱）**：`GET /question` ×52（ocdroid 旧版本打裸路径，非 webui）、`/session/status` ×5、杂项探测——现有 45 条收编基本覆盖实际消费面。

**客户端归因实测（2026-08-17）**：`client` 字段分布 ocdroid 47,048（96.9%）/ oc-webui 149（0.3%）/ 无主 1,287（2.7%）。⚠️ 无主 2.7% 未归因（疑似 tailscale serve 探活/smoke/未带头调用方），后续按 `client==null` 过滤聚合定位。

---

## 1. P0 — 直接砍流量大头（数据+需求双命中）

| # | 接口/能力 | 证据交叉 | 预期收益 |
|---|---|---|---|
| 1 | **merged 内联预算/截断** | webui 首页拉 2×110KB merged 全文，上屏仅 200 字摘要（1%）；messages 桶占上游流量 **66%**（75.4MB/日） | 单点最大：merged 内联文本加 cap（如 400 字 + `truncated` 标记），全文下沉已有 `/full`+expand |
| 2 | **跨目录"收藏会话"聚合端点** | webui 收藏 30 目录 × 每目录 2 请求，digest 后 5s trailing 整组重拉——全仓最重放大点；sessions 桶 **8,959 请求/日** | N×2 → 1 请求；镜像 questions 跨目录聚合的既有模式，上游 `/experimental/session`（cursor 就绪）可直接复用 |
| 3 | **digest 增量定位** | digest 帧只带 sid，客户端无法定位变更 → 100 会话+全量 status 整表重拉 | digest 帧携带 `{directory, 变更 sid 列表+原因}`，客户端用**已有** `GET /slimapi/session/{sid}` 精拉；纯加性帧字段 |

## 2. P1 — 契约欠账 + 重连成本

| # | 接口/能力 | 证据交叉 | 说明 |
|---|---|---|---|
| 4 | **SSE `id:`/Last-Event-ID 重放** | webui 两路 SSE 断线均全量恢复（sidecar 从不发 `id:`）；上游 `/sync/history`（lastSeq 增量）与 `/api/session/:sid/history`（after seq）**自带重放原语** | sidecar 记 per-stream seq → 重连只补缺口；events_sse 桶 12.8MB/日大头是重连全量恢复 |
| 5 | **`slimapi.meta` 首帧** | v3 契约已冻结、实现为零（webui 代码注释确认） | 契约欠账，小工作量 |
| 6 | **q/p IMMEDIATE 帧载荷直投** | asked 帧已带 `{directory,type,properties}` 却被当"变更信号"触发 2s 后全量重拉两个聚合端点；questions 桶 8.3MB 上游换 12KB 下行（0.1%，fan-out 成本在服务端） | 帧内直插完整 question/permission 对象 → 免重拉；双端小改 |

## 3. P2 — 确定未来需求（已表态/必然到来）

| # | 接口/能力 | 证据交叉 | 前置 |
|---|---|---|---|
| 7 | **`/file` 文件树通路 + directory 白名单** | 用户原话"保留，后续会希望浏览文件"（webui HANDOFF）；渲染四道防线已就位仅缺数据通道 | 安全前置必须先做：sidecar directory 白名单 → serve mount → renderer 解禁 |
| 8 | **`/api/session/{sid}/context` 收编** | 上游 compaction 后活跃上下文**全量重放**（L 级）；ocdroid 3.1.0 适配轮引入 compaction_full 后自然衔接 | 投影复用 skeleton 管线 |
| 9 | **v2 `/api/**` cursor 分页通道评估** | 上游同端口已挂 18 group `/api/**`（message limit≤200 不透明 cursor、session anchor cursor）；长期可作标准分页/增量迁移通道 | 暂不急——v1.18.x 相对 v1.17.20 **零 server 端点变更**，无版本跟进压力 |

---

## 4. 专有 vs 通用需求归属

| 需求 | 归属 | 依据 |
|---|---|---|
| merged/skeleton 长文本截断 | **通用** | messages 桶 75.4MB/日（66%）由 ocdroid 主导；任何客户端列表渲染只需摘要 |
| digest 增量定位字段 | **通用** | session.digest 是两客户端共同消费的策展帧 |
| SSE `id:`/Last-Event-ID 重放 | **通用** | 两客户端都是长连+全量恢复模式 |
| `slimapi.meta` 首帧 | **通用（契约级）** | v3 契约冻结项，与客户端无关 |
| q/p IMMEDIATE 帧载荷直投 | **通用偏 webui** | questions fan-out 8.3MB/日上游成本在服务端；webui 的 2s-trailing 重拉模式最典型，ocdroid 同样消费聚合端点 |
| 跨目录收藏聚合 | **webui 专有** | ocdroid 是单目录 focus 模型，无 30 目录收藏扇出场景 |
| `/file` 文件树通路 | **webui 专有** | 用户表态出自 webui 语境；sidecar 侧 `/slimapi/file` 读组已收编，缺 webui serve mount + renderer 解禁 + sidecar directory 白名单 |
| `getCommands` 恢复 | **webui 专有** | 死代码复活需求，probe 已就绪 |
| ETag/304 启用 | **webui 侧欠账** | sidecar ETag 基础设施已有，webui 客户端零实现（README 漂移） |

## 5. webui bug/误用 vs sidecar 真实缺口

**webui 侧问题（bug/误用/漂移）**：
- `README.md:16` "ETag/304 复用" 与实现不符（`endpoints.ts:32` 明确范围外）——文档漂移
- q/p `asked` 帧已带 `{directory,type,properties}` 载荷，webui 只当变更信号 → **半误用**（若核对 `hub_types.py` 帧载荷字段不全则升级为缺口）
- 收藏目录逐目录拉取而不用已有跨目录聚合——**疑似误用或聚合端点语义不足**（webui 需要 per-directory `roots:true,limit:20` 语义，需确认现有聚合端点是否满足）
- merged 首页 2×110KB 只上屏 200 字——webui 自开的 `serverMerge` 开关 + 渲染只取摘要的错配（可先关 merged 缓解），根因是 merged 无截断 cap
- `getCommands` 死代码 + probe 仍探测（无害漂移）

**sidecar 真实缺口**：
- merged 内联无截断（契约级缺失，`truncated` 标记不存在）
- digest 帧无变更定位字段（只有 sid，无法增量）
- SSE 从不发 `id:`（重连必全量恢复）——SSE 规范能力缺失
- `slimapi.meta` 首帧契约冻结但零实现
- directory 白名单（安全缺口，`/file` 通路前置）
- `/api/session/{sid}/context` 未收编（compaction 后全量重放，ocdroid 3.1.0 适配后自然到来）

**另发现**：404 的 `GET /question` ×52 是 ocdroid **旧版本**（clientVer 0.26/0.28/3.0.0 多版本共存）打未收编裸路径，属 ocdroid 版本迁移残留，非 webui。

## 6. 客户端区分字段现状（排查能力）

- webui **REST+SSE 全路径**发 `X-Client-Name: oc-webui` + `X-Client-Version`（`client.ts:93-94`、`connect.ts:74-75`、`useTokenStream.ts:66-67`）
- sidecar access log 已记 `client/clientVer/clientId` 三字段
- 实测：ocdroid 96.9% / oc-webui 0.3% / 无主 2.7%——**当前流量数据几乎全部来自 ocdroid**；webui 痛点多为"单次体验成本"（per-load 2×110KB）而非总量占比；流量维度绝对大头收益落在 ocdroid 侧

## 7. 三维度优先排名

**流量（字节/日）**：
1. merged/skeleton 截断 — messages 桶 75.4MB（66%），唯一单点能砍大头
2. SSE 重连增量 — events_sse 12.8MB，重连全量恢复是大头
3. 跨目录聚合 + q/p 载荷直投 — sessions 10.8MB + questions fan-out 8.3MB 上游
4. digest 增量定位 — 间接压缩 sessions 桶

**请求频次（服务器压力/放大系数）**：
1. 跨目录收藏聚合 — 30 目录 × 2 请求/次 digest，线性放大，最重放大点
2. digest 增量定位 — sessions 40.5K + status 30.6K 次/4天，整表重拉是主因
3. q/p 载荷直投 — 每次 asked → 2 聚合端点全量重拉
4. SSE `id:` — 低频但单次 L 级成本

**功能缺失（契约完整性/既定路线）**：
1. `slimapi.meta` 首帧 — 契约已冻结未实现，最明确欠账
2. SSE `id:`/Last-Event-ID — SSE 规范能力
3. directory 白名单 — 用户已表态的 `/file` 功能安全前置
4. merged `truncated` 标记 — 截断配套契约字段
5. `/api/context` 收编 — ocdroid 3.1.0 适配后自然衔接

## 8. 综合建议

- **下一轮 P0 设计稿主体**：流量 #1（merged 截断）+ 频次 #1/#2（跨目录聚合 + digest 定位）
- **同轮并入（SSE 域）**：功能缺失 #1/#2（meta 帧 + SSE id:）
- **webui 侧确认单**（发 webui 组后再定案）：ETag 启用计划、收藏扇出改用现有聚合端点的可行性、q/p 帧载荷字段是否够直投
- **附注**：树内 opencode 实为 v1.18.18（AGENTS.md 写 1.18.16 滞后）；v1.18.0→18 全部变更在 UI 层，HTTP API 面零增删改——收编窗口稳定。
