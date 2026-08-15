# L1 基线证据归档（2026-08-15）

> 来源：`docs/ocmar/plans/2026-08-15-l1-l3-slim-consolidation.md`「决策记录」章节 + 2026-08-15 实测基线。供 L2/L3 评审与 ocdroid 对接引用。

## 1. check.sh 基线

- **1425 passed** + 路由↔文档一致（15 条 `/slimapi` 路由均已在 INTERFACE_MAP 记录）+ compileall ✅
- 基线 commit：`e68e337c7e1f99ad5999fe7470aa98dd287fbbcb`（2026-08-15 实测）

## 2. 流量实证摘要

- **ocdroid 40853 reqs** / **16 条 `/slimapi` 路由** / **0 passthrough**
- **2 台设备**，客户端版本 **0.23.3-0.24.0**
- 观察窗口：**4 天**，2026-08-12 ~ 08-15

## 3. TS 换核废弃决策

1. **TS 换核废弃**：双边独立核验（oc-slimapi orchestrator 本机 pinned 快照 + npm registry 实查；ocdroid 侧 librarian 第二 lane）互证——`@opencode-ai/core` / `@opencode-ai/server` 均 `private: true`、npm 上 `0.0.0-reserved.0` 占位、内核编译进 175MB bun 单体二进制、主包无 exports 不可 import、官方 slack 网关先例也是子进程 + SDK client。Python sidecar 吃 HTTP 面为唯一受支持形态。
2. **跟踪触发器**（不启动、只观察）：`@opencode-ai/core` 或 `@opencode-ai/server` 正式发布且 exports 稳定，或未发布的 `packages/sdk-next`（workspace 依赖 core+server+client+effect）正式发布含内核 SDK → 届时重估 dslima A0 式「import 原生 fold」。
3. **上游仓库迁移**：sst/opencode → anomalyco/opencode（301），后续源码引用换新地址。

## 4. ocdroid 仅 slimapi 连接结论

4. **ocdroid 仅 slimapi 连接可行性**：4 天 access log 实证 ocdroid 40853 reqs 100% 走 `/slimapi/**`、0 passthrough；客户端代码无 slim→direct 自动回退；直连无 slim 不可替代能力（方向相反：skeleton/策展 SSE/token stream 为 slim 独占）。
