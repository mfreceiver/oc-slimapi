# AGENTS.md - oc-slimapi

> 本文件是给在此仓库工作的 agent 的**入口索引**。契约权威、发版流程、接口变更记录分别下沉到具名文档；**不要**在本文件复制长流程。

---

## 项目是什么

`oc-slimapi` 是 **ocdroid**（Android 客户端）与 **opencode**（上游 server）之间的 **Python 省流 sidecar**：

```text
ocdroid ──(stunnel mTLS 14097)──▶ oc-slimapi :4097 (loopback)
                                      │ loopback HTTP
                                      └────────▶ opencode :4096 (legacy /session API)

ocdroid ──(stunnel mTLS 14096)──▶ opencode :4096   # 直连回退，不经 sidecar
                                                    # 目标态：ocdroid 完成 C1/C3 前置后此直连退役，仅服务匿名消费方（见 docs/specs/CLIENT_CHANGES.md「直连退役」）
```

- **只**通过 HTTP 调 opencode **legacy** `/session/**`（及 `/global/event` 等），并经只读投影源读 opencode SQLite（4.0.0 起生产启用，`mode=ro` 的 v4 DB 投影通道；**绝无写入**——写域约束见硬规则「SQLite 写域」）。
- 为 ocdroid 提供：消息 skeleton 投影、策展 SSE（`session.digest` + q/p 直推 + `slimapi.meta` 首帧）、T3 资源限制、`/slimapi/**` `?v=` 选择器（4.8.0 起 v4-only 单版本窗口：`?v=4` 唯一合法；4.0.0–4.7.0 曾为 (3,4) 双版本；catch-all 反代已于 3.0.0 关闭）。
- **权威契约**：[`docs/specs/v4-contract.md`](docs/specs/v4-contract.md)（**现行契约权威**，4.8.0 起 v4-only 自包含；与 design / INTERFACE_MAP 冲突时以契约为准）。[`docs/specs/v3-contract.md`](docs/specs/v3-contract.md) 为 ≤4.7.0 历史存档（v3 wire 版本已退役）；[`docs/specs/v2-contract.md`](docs/specs/v2-contract.md) 为 ≤2.x 历史契约（v2 语义已于 3.0.0 退役）。
- **设计 / 接口追踪**：[`docs/specs/design-v2.md`](docs/specs/design-v2.md)、[`docs/specs/INTERFACE_MAP.md`](docs/specs/INTERFACE_MAP.md)。
- **客户端配套说明**：[`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md)（给 ocdroid 开发者的改动清单）。

本仓库 **不** 是 ocdroid 的子模块；与 ocdroid **并列** 开发、独立发版。ocdroid 侧对接规约见 ocdroid 仓库内 `docs/slim-mode-api-routing.md`（路径/版本以 **本仓库契约** 为准；若 ocdroid 文档滞后，以本仓库 `docs/specs/v4-contract.md` + `CHANGELOG.md` 为准）。

---

## 与 ocdroid / opencode 源码的关系

| 组件 | 路径（本机并列布局） | 角色 |
|---|---|---|
| **oc-slimapi**（本仓库） | `/home/mar/personal_projects/oc-slimapi` | 省流 sidecar |
| **ocdroid** | `/home/mar/personal_projects/ocdroid` | Android 客户端 |
| **opencode 源码（在本仓 `src-ref/`）** | `/home/mar/personal_projects/oc-slimapi/src-ref/opencode/current` | 上游 server 源码快照，供对照 legacy 行为 |

**opencode 源码目录（相对本仓根）**：`src-ref/opencode/current/`（稳定符号链接 → 当前对齐版本的子目录）。  
**当前对齐版本**：`src-ref/opencode/current` → **v1.18.21**（完整 monorepo 树；非部分抽取；2026-08-22 自 v1.18.18 更新，sidecar 契约相关文件 diff 确认零漂移——message-v2 分页/session·event·global handlers/session-id schema 均 unchanged，变更面集中在 plugin/provider/session 内部/tool 层）。后续定期更新时，放入新版本目录后仅 repoint `current` 符号链接即可，本仓文档路径不需改。`src-ref/` 在本仓库 `.gitignore` 中，不污染 git（ocdroid 侧的 `opencode-src/` 已于 2026-08-22 整体迁入本仓并清理旧版本快照）。

### 上游对照常用路径（相对 `src-ref/opencode/current/`）

排查 cursor / 消息分页 / SSE / session 字段时优先读这些（以树内实际文件为准）：

| 主题 | 相对路径 |
|---|---|
| 消息分页 + cursor | `packages/opencode/src/session/message-v2.ts` |
| HTTP handlers（session/message） | `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`、`packages/server/src/handlers/session.ts` |
| HTTP handlers（event/SSE） | `packages/opencode/src/server/routes/instance/httpapi/handlers/event.ts`、`packages/server/src/handlers/event.ts` |
| event groups | `packages/opencode/src/server/routes/instance/httpapi/groups/event.ts`、`packages/protocol/src/groups/event.ts` |
| session 核心 / 投影 | `packages/core/src/session.ts`、`packages/schema/src/v1/session.ts` |

**约定**：改 sidecar 行为前，若涉及上游语义（`before` cursor、`Link` 头、`time.archived`、`/global/event` 帧形），**先读上述源码再改**；不要凭记忆编造 opencode 行为。

---

## 流程入口（优先用脚本，不要手拼命令）

| 任务 | 入口 | 规则 / 细节 |
|---|---|---|
| 改动后校验（必做） | `./scripts/check.sh` | 实际三项：pytest + **路由↔文档一致性**（[`scripts/check_routes_doc.py`](scripts/check_routes_doc.py)：每个 `/slimapi` 路由须在 INTERFACE_MAP 有记录，**防漂移**）+ `compileall src` 字节码编译；门禁细则见 [`docs/release.md`](docs/release.md) §质量门禁 与 [`docs/develop.md`](docs/develop.md) |
| 发版（tag + changelog） | `./scripts/release.sh <patch\|minor\|major>` | **[`docs/release.md`](docs/release.md)**（发版规范权威） |
| 接口行为变更记录 | 编辑 [`CHANGELOG.md`](CHANGELOG.md) | 每次**破坏/加性 wire 行为**变更必须记；ocdroid 对接以本文件为准 |
| 契约 / 设计 | `docs/specs/v4-contract.md`（现行契约权威）、`docs/specs/design-v2.md`、`docs/specs/INTERFACE_MAP.md`（`v3-contract.md`/`v2-contract.md` 为历史存档） | 版本协商 = `?v=` selector + `GET /slimapi/versions`（`X-Slimapi-Version` 头已于 3.0.0 删除）；破坏性变更走 major 发版 + 契约修订——**owner 例外**（2026-08-21，与 `docs/release.md:37-38/54-55` 裁定对齐）：wire 版本不变的破坏性 wire 形状变更，经 owner 批准可发 **minor**，须在权威契约修订头 + CHANGELOG Changed 显式记录例外与客户端必改点（首例：4.9.0 `v4-contract.md` §10.2 修订四 patch files 归一化） |
| 省流 / 路由审计（advisory） | access log `access-YYYY-MM-DD.jsonl`（按天）+ snapshot `traffic-snapshot-YYYY-MM-DD.jsonl` + [`docs/specs/INTERFACE_MAP.md`](docs/specs/INTERFACE_MAP.md) | 查“哪些请求未省流”：按 `bucket=="passthrough"` 过滤 access log、聚合 `method+path`，再对照 INTERFACE_MAP 看有无 `/slimapi` 等价省流路由。**3.0.0 终局注记**：catch-all 已关闭，`passthrough` 桶现为**哨兵桶**（只剩 404/405 拒绝，`upIn`/`upOut` 恒 0——任何 2xx 出现即意外穿透，见 traffic-accounting.md §3.2）；未省流分析现以 `other` 桶与 INTERFACE_MAP 对照为主。文件位置/查询见 [`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md)（生产落 `~/.local/state/oc-slimapi/logs/`，`RETAIN_DAYS=3` 自动清理）；新增 `/slimapi` 路由必须同步进 INTERFACE_MAP（否则 check.sh 失败） |

> 任何 release / tag / 版本号 / changelog 写入，都不得由 agent 自由发挥命令，必须走 `scripts/release.sh` 或 `docs/release.md` 写明的步骤。

---

## 硬规则（不可违反）

- **改动校验必做**：每次改 Python / 契约相关行为后，必须 `./scripts/check.sh` 通过才算改动完成（实际三项：`pytest tests/` + 路由↔文档一致性 gate + `compileall src`，见 `docs/develop.md`「测试 / 质量门禁」）。
- **契约权威**：wire 行为以 `docs/specs/v4-contract.md` 为准（`v3-contract.md` 为 ≤4.7.0 历史存档，`v2-contract.md` 为 ≤2.x 历史契约）；实现与契约冲突 → 先改实现或走正式契约修订（见 `docs/release.md`），**禁止**静默偏离契约。
- **SQLite 写域**：禁止写入/修改上游 opencode SQLite 业务数据；sidecar 代码路径零 DDL/DML/PRAGMA 写；索引建立属显式运维动作（含定义校验），不在 sidecar 内。wire contract 只冻结可观察语义（参数/错误/降级/degraded），**不冻结 SQLite 实现手段**；实现边界进本文件 / 架构设计文档 / `docs/operations.md`。
- **版本双轨**：
  - **包版本**（semver，git tag `vX.Y.Z` + `pyproject.toml`）：产品/发版版本。
  - **Wire API 版本**（整数，`versioning.py` 中 `ACCEPTED_CLIENT_VERSIONS`，当前 `(4,4)`——4.8.0 起 v4-only 单版本窗口）：协商经 `?v=` selector + `/slimapi/versions` 发现端点；`X-Slimapi-Version` 请求头已于 3.0.0 删除（出现不解读）。
- **Git 分支**：主线 `main`；发版在 `main` 上打 tag。
- **禁止**：手写随意 tag 跳过 `release.sh`；在未更新 `CHANGELOG.md` 的情况下发布 wire 行为变更；把 secret / `.venv` / 本机路径密钥提交进仓。
- **写域纪律**：多 agent 并行时严守文件归属；`docs/specs/v3-contract.md` 非用户明确要求不要改。

---

## 常用命令速查

```bash
# 环境
python -m venv .venv
.venv/bin/pip install -e '.[test]'

# 改动校验（必做）
./scripts/check.sh

# 开发：本地手动跑
.venv/bin/python -m oc_slimapi.app

# 生产：systemd user 服务（部署/日志/自启见 docs/operations.md）
systemctl --user start oc-slimapi
journalctl --user -u oc-slimapi -f          # 应用日志（startup/warning/smoke）

# 落盘日志（access log + snapshot，生产落 StateDirectory）
ls ~/.local/state/oc-slimapi/logs/           # access-YYYY-MM-DD.jsonl(.gz) + traffic-snapshot-YYYY-MM-DD.jsonl
# 查询/分析手册：docs/manual/traffic-accounting.md（生产 RETAIN_DAYS=3 自动清理 access log）

# 发版（见 docs/release.md）
./scripts/release.sh patch    # | minor | major
```

---

## 相关文档索引

| 文件 | 用途 |
|---|---|
| [`docs/specs/v3-contract.md`](docs/specs/v3-contract.md) | ≤4.7.0 历史契约存档（v3 wire 版本已退役；`v2-contract.md` 为 ≤2.x 历史契约） |
| [`docs/specs/v4-contract.md`](docs/specs/v4-contract.md) | **Wire 契约权威**（4.0.0 实施基线 + 2026-08-19 正式修订冻结（修订二：POST 等效动作族）；4.8.0 起 v4-only 自包含） |
| [`docs/specs/design-v2.md`](docs/specs/design-v2.md) | v2 时代设计（历史；现行态以 v4 契约 + operations.md 为准） |
| [`docs/specs/INTERFACE_MAP.md`](docs/specs/INTERFACE_MAP.md) | 端点级实现追踪 |
| [`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md) | ocdroid 侧配套改动清单 |
| [`docs/specs/HANDOVER-4.11.0.md`](docs/specs/HANDOVER-4.11.0.md) | 4.11.0 流量优化族交接简报（给 webui/ocdroid 组的新接口/变动/推荐用法总览） |
| [`docs/specs/design-token-stream.md`](docs/specs/design-token-stream.md) | Token stream 设计历史与 rationale（v4 设计稿；当前 wire 契约见 v4-contract.md §7.7） |
| [`CHANGELOG.md`](CHANGELOG.md) | **接口行为变更记录**（给 ocdroid / 运维） |
| [`docs/release.md`](docs/release.md) | **发版流程规范**（本仓库权威） |
| [`docs/operations.md`](docs/operations.md) | **部署 / 运维 / 日志**（systemd、journald、排障） |
| [`docs/develop.md`](docs/develop.md) | 开发 / 运行 / 测试备忘 |
| [`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md) | 流量/省流查询使用手册（`/slimapi/metrics.traffic` + 按天 access log + snapshot；落盘位置/RETAIN_DAYS 见此） |
| ocdroid `docs/slim-mode-api-routing.md` | 客户端 slim 路由规约（对照用；冲突以本仓契约 + CHANGELOG 为准） |
