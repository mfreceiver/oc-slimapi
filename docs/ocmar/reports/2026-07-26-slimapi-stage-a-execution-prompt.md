# oc-slimapi 阶段 A 新会话执行提示词

> 将下方提示词交给 oc-slimapi 新会话。执行前必须由用户明确指定 owner、目标版本和是否允许生成 commit/tag；未明确时只能勘察，不得写代码或发布。

```text
你负责 oc-slimapi Slim 会话消息可靠性联合计划的阶段 A 自身任务。

先阅读：
- AGENTS.md
- docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md
- docs/ocmar/plans/2026-07-26-slim-state-message-repair.md
- docs/ocmar/reviews/2026-07-26-rev-gpt-class1-slim-repair.md

当前基线：
- 第1类 sidecar 修复已经在工作树完成：/since oldest-first 整页过滤、X-Since-Complete、SSE per-sid flush、archived 类型防护、full 错误归一、strip in-place。
- 已验证 `./scripts/check.sh`：1022 passed，19 条 /slimapi 路由与 INTERFACE_MAP 一致。
- 工作树尚未提交；联合计划已 rev-gpt 9.5/10 PASS，但按 D-GATE 只允许阶段 A，不允许实施阶段 B 联调协议。

你的任务：

1. 只读核对工作树与计划中的阶段 A 交付物，确认没有未收口的第1类回归。
2. 保持以下 wire 语义，不新增 revision、partCount、generation 或 token 终态协议：
   - `/since/{ts}` 页内 oldest-first 整页过滤；
   - `X-Since-Complete: true|false` 的既定语义；
   - full 非法/空 JSON和中途读取失败为 503 upstream_unavailable；
   - SSE immediate event 只 flush 目标 sid；
   - archived 接受真实 int（含0）但拒绝 bool。
3. 运行并记录：
   - `./scripts/check.sh`
   - `git diff --check`
   - 必要的 since/hub/full 定向测试。
4. 如果用户已明确授权发布：按 docs/release.md 和项目规定生成 commit/tag/artifact SHA-256/不可变 check log；不要自行改变版本号或发布范围。
5. 如果用户未明确授权发布：只输出核对报告和待授权清单，不 commit、不 tag、不 push。

阶段 A 禁止事项：
- 不实现 ocdroid 侧 watermark/reconcile；
- 不新增第2类 wire 字段；
- 不做 slim + SSE 开/关真实联调；
- 不擅自修改 `X-Slimapi-Version`；
- 不把阶段 B 的 union/replacement 或 token done/idle 语义写成已冻结实现。

交付格式：
1. 工作树与测试证据；
2. 版本/commit/tag/artifact 证据（若获授权）；
3. 阶段 A 完成项与剩余风险；
4. 是否满足进入阶段 B 的 sidecar 侧条件；
5. 明确声明未实施阶段 B。
```
