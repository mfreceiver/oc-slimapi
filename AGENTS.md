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
```

- **只**通过 HTTP 调 opencode **legacy** `/session/**`（及 `/global/event` 等），**不读** opencode SQLite。
- 为 ocdroid 提供：消息 skeleton 投影、策展 SSE（`session.digest` + q/p 直推）、routeToken 写端点、T3 资源限制、`/slimapi/**` 版本门禁 + catch-all 反代。
- **权威契约**：[`docs/v1-contract.md`](docs/v1-contract.md)（唯一 wire 基准；与 design / INTERFACE_MAP 冲突时以契约为准）。
- **设计 / 接口追踪**：[`docs/design-v2.md`](docs/design-v2.md)、[`docs/INTERFACE_MAP.md`](docs/INTERFACE_MAP.md)。
- **客户端配套说明**：[`docs/CLIENT_CHANGES.md`](docs/CLIENT_CHANGES.md)（给 ocdroid 开发者的改动清单）。

本仓库 **不** 是 ocdroid 的子模块；与 ocdroid **并列** 开发、独立发版。ocdroid 侧对接规约见 ocdroid 仓库内 `docs/slim-mode-api-routing.md`（路径/版本头以 **本仓库契约** 为准；若 ocdroid 文档滞后，以本仓库 `docs/v1-contract.md` + `CHANGELOG.md` 为准）。

---

## 与 ocdroid / opencode 源码的关系

| 组件 | 路径（本机并列布局） | 角色 |
|---|---|---|
| **oc-slimapi**（本仓库） | `/home/mar/personal_projects/oc-slimapi` | 省流 sidecar |
| **ocdroid** | `/home/mar/personal_projects/ocdroid` | Android 客户端 |
| **opencode 源码（在 ocdroid 内）** | `/home/mar/personal_projects/ocdroid/opencode-src/current` | 上游 server 源码快照，供对照 legacy 行为 |

**opencode 源码目录（相对 ocdroid 根）**：`opencode-src/current/`（稳定符号链接 → 当前对齐版本的子目录）。  
**当前对齐版本**：opencode **v1.18.3**（完整 monorepo 树；非部分抽取）。后续定期更新时，仅 repoint `current` 符号链接即可，本仓文档路径不需改。`opencode-src/` 在 ocdroid `.gitignore` 中，符号链接不污染 ocdroid git。

### 上游对照常用路径（相对 `opencode-src/current/`）

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
| 改动后校验（必做） | `./scripts/check.sh` | 本文「硬规则」+ [`docs/release.md`](docs/release.md) §质量门禁 |
| 发版（tag + changelog） | `./scripts/release.sh <patch\|minor\|major>` | **[`docs/release.md`](docs/release.md)**（发版规范权威） |
| 接口行为变更记录 | 编辑 [`CHANGELOG.md`](CHANGELOG.md) | 每次**破坏/加性 wire 行为**变更必须记；ocdroid 对接以本文件为准 |
| 契约 / 设计 | `docs/v1-contract.md`、`docs/design-v2.md`、`docs/INTERFACE_MAP.md` | 契约只在破坏性变更时 bump `X-Slimapi-Version` |

> 任何 release / tag / 版本号 / changelog 写入，都不得由 agent 自由发挥命令，必须走 `scripts/release.sh` 或 `docs/release.md` 写明的步骤。

---

## 硬规则（不可违反）

- **改动校验必做**：每次改 Python / 契约相关行为后，必须 `./scripts/check.sh` 通过才算改动完成（当前 = `pytest tests/`）。
- **契约权威**：wire 行为以 `docs/v1-contract.md` 为准；实现与契约冲突 → 先改实现或走正式契约 bump（见 `docs/release.md`），**禁止**静默偏离契约。
- **版本双轨**：
  - **包版本**（semver，git tag `vX.Y.Z` + `pyproject.toml`）：产品/发版版本。
  - **Wire API 版本**（整数头 `X-Slimapi-Version`，`versioning.py` 中 `ACCEPTED_CLIENT_VERSIONS`）：仅**破坏性**协议变更 bump；加性变更不 bump。
- **Git 分支**：主线 `main`；发版在 `main` 上打 tag。
- **禁止**：手写随意 tag 跳过 `release.sh`；在未更新 `CHANGELOG.md` 的情况下发布 wire 行为变更；把 secret / `.venv` / 本机路径密钥提交进仓。
- **写域纪律**：多 agent 并行时严守文件归属；`docs/v1-contract.md` 非用户明确要求不要改。

---

## 常用命令速查

```bash
# 环境
python -m venv .venv
.venv/bin/pip install -e '.[test]'

# 改动校验（必做）
./scripts/check.sh

# 开发：本地手动跑（临时 secret）
openssl rand -base64 48 > /tmp/oc-slimapi-route-secret && chmod 600 /tmp/oc-slimapi-route-secret
OC_SLIMAPI_ROUTE_SECRET_FILE=/tmp/oc-slimapi-route-secret \
  .venv/bin/python -m oc_slimapi.app

# 生产：systemd user 服务（部署/日志/自启见 docs/operations.md）
systemctl --user start oc-slimapi
journalctl --user -u oc-slimapi -f

# 发版（见 docs/release.md）
./scripts/release.sh patch    # | minor | major
```

---

## 相关文档索引

| 文件 | 用途 |
|---|---|
| [`docs/v1-contract.md`](docs/v1-contract.md) | **Wire 契约权威** |
| [`docs/design-v2.md`](docs/design-v2.md) | 当前态设计（接口/骨架/部署） |
| [`docs/INTERFACE_MAP.md`](docs/INTERFACE_MAP.md) | 端点级实现追踪 |
| [`docs/CLIENT_CHANGES.md`](docs/CLIENT_CHANGES.md) | ocdroid 侧配套改动清单 |
| [`CHANGELOG.md`](CHANGELOG.md) | **接口行为变更记录**（给 ocdroid / 运维） |
| [`docs/release.md`](docs/release.md) | **发版流程规范**（本仓库权威） |
| [`docs/operations.md`](docs/operations.md) | **部署 / 运维 / 日志**（systemd、journald、排障） |
| [`docs/develop.md`](docs/develop.md) | 开发 / 运行 / 测试备忘 |
| ocdroid `docs/slim-mode-api-routing.md` | 客户端 slim 路由规约（对照用；冲突以本仓契约 + CHANGELOG 为准） |
