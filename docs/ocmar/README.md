# docs/ocmar/ — 冻结的工作产物存档

> **冻结快照，非当前权威。** 本目录收录 2026-07 ~ 2026-08 期间的 ocmar 工作流产物（reports / specs / plans / reviews / evidence），均为**一次性会话产物**：handoff prompt、execution prompt、单次评审、单次 checklog、阶段设计草稿等。

## 当前权威在哪

| 想找 | 去这里 |
|---|---|
| Wire 契约（端点 / 帧形 / 版本头 / 错误码） | `docs/specs/v2-contract.md` |
| 当前态设计（架构 / 骨架 / SSE / 反代） | `docs/specs/design-v2.md` |
| 端点级实现追踪 | `docs/specs/INTERFACE_MAP.md` |
| ocdroid 侧配套改动 | `docs/specs/CLIENT_CHANGES.md` |
| Token stream 设计历史与 rationale | `docs/specs/design-token-stream.md` |
| 发版 / 运维 / 流量手册 | `docs/release.md`、`docs/operations.md`、`docs/develop.md`、`docs/manual/traffic-accounting.md` |

## 本目录内容性质

- **非权威**：所有设计决策的最终态已收敛进 `docs/specs/` 顶层权威文档。本目录文件反映的是设计/评审/交接的**过程**，可能与当前实现存在出入，**不得**当作行为基准。
- **保留原因**：仍存在的文件**绝大多数**被 `CHANGELOG.md` / `docs/specs/*` / `docs/operations.md` / `src/**` 等**永久文档直接引用**，作为历史上下文锚点（"见某次评审 / 某份交接的 rationale"）。个别文件（如 `specs/2026-07-27-stage-b-impl-spec.md`）作为同系列 delta 文件（v0.4/0.5/0.6-delta）的**基线锚点**保留。这些引用是单向冻结的，本目录文件不再更新。
- **已清理**：未被任何文档引用的纯一次性产物（execution-prompt、handoff-prompt、单次 checklog、从未实施的 stage-B 系列草稿等）已于 2026-08-09 批量移除。如需找回，可通过 `git log -- docs/ocmar/` 在历史中检索。

## 维护规则

- **不要**在本目录新增"权威设计"——新设计直接进 `docs/specs/`。
- **不要**回填更新本目录文件以"对齐现状"——它们是冻结快照，对齐工作在 `docs/specs/` 完成。
- 新的 ocmar 工作流产物若需留档，写完即止，不维护。
