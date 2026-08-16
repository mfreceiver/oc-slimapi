# oc-slimapi v3 wire 契约（草案）

> 状态：**DRAFT — 面向新消费方（oc-webui）的初版接入基准**。用户已确认 v3 方向（2026-08-16，见 `docs/ocmar/plans/2026-08-16-single-entry-roadmap.md` §5 决策记录）：无版本段路径 + 发现端点 + 自定义头全退役 + envelope 化 + `?directory=` 转正 + v2/v3 并行期。
> 每节标注稳定度：**[稳定]** = 用户已拍板，不会变；**[草案]** = 当前倾向，design-v3 评审（rev 面板 ≥9.5）可能微调（字段名/机制细节），消费方实现时隔离到常量层。
> v2 权威不变：`docs/specs/v2-contract.md`。v3 实施前本文件不构成服务端承诺；首个可联调里程碑见 §8。

---

## 1. 总则 [稳定]

- 服务路径**无版本段**：不引入 `/slimapi/v3/…`，v2/v3 共用 `/slimapi/*` 同一路径族。
- v3 语义经**请求级版本选取**激活（机制 **[草案]**：`?v=3` query 参数；缺省 = v2 现行语义，保证 ocdroid 等存量消费方零感知）。
- v3 下**客户端→sidecar 自定义头全部退役**：`X-Slimapi-Version`、`X-Opencode-Directory`、`X-Next-Cursor`、`X-Complete` 不再要求也不再产出（304 的 `ETag`/`Vary`/`Cache-Control` 为标准 HTTP 头，保留）。
- 版本发现代替版本协商：客户端先探 `GET /slimapi/versions`，按自身规则选取用法（见 §2）。

## 2. 版本发现端点 [草案——形状字段可能微调，路径稳定]

```
GET /slimapi/versions
```

- **无 `X-Slimapi-Version` 门禁、无 `?v=` 要求**（否则鸡生蛋）——任何客户端裸 GET 即可。
- 响应 200 JSON：

```json
{
  "current": 3,
  "available": [2, 3],
  "capabilities": {
    "2": {"etag": true, "contentFingerprint": true, "thinRoutes": ["todo", "children", "diff"]},
    "3": {"envelope": true, "directoryQuery": true, "headersRetired": true}
  },
  "sidecarVersion": "1.7.0"
}
```

- 字段语义：`current` = 本机推荐版本；`available` = 可选版本列表；`capabilities` = 每版本能力开关（消费方 feature-detection 用）；`sidecarVersion` = 包版本（诊断用）。
- 老消费方不认识该端点 → 不调用，零影响（加性端点）。

## 3. envelope 化 [草案——字段名稳定，适用范围可能增删]

v3 下分页/完成度从响应头移入 body：

```json
{"items": [ … ], "nextCursor": "msg_01H…|null", "complete": true}
```

- 适用端点（v3 语义激活时）：`GET /slimapi/messages/{sid}`、`GET /slimapi/sessions`、`GET /slimapi/sessions/status`。
- `nextCursor`：string 或 `null`（无更多页）；`complete`：bool（全量加载完成）。
- v2 路径行为不变（`X-Next-Cursor`/`X-Complete` 头继续产出，直至 v2 移除）。

## 4. `?directory=` [稳定——todo/children/diff 已实现此语义，v3 推广全路由]

- 参数名 `directory`，单值 string；语义与现 `X-Opencode-Directory` 头**逐字相同**（选择 opencode 工作目录实例）。
- 缺省 = sidecar 默认目录（现头行为）。
- v3 激活时：`?directory=` 唯一接受形式；v2 语义下头继续接受（并行期双轨）。

## 5. SSE [草案]

- 事件名/帧形/**不变**（`session.digest`/`token`/q/p 直推/`server.connected`/heartbeat/resync；`Last-Event-ID` 语义不变）。
- SSE 版本选取 **[草案]**：`GET /slimapi/events?v=3`——SSE 无常规请求头协商位，v3 语义经同款 query 选取；v3 对 events 的实际差异极小（events 本就不用被退役的四头，除 `X-Slimapi-Version` 门禁外零变化）。**消费方可先按 v2 帧形实现，v3 落地后无需改帧解析。**

## 6. 错误体 [稳定——与 v2 逐字相同]

`{"code": "...", …上下文字段}` 结构沿用；code 集合不因 v3 变化（`session_not_found`/`upstream_unavailable`/`transform_busy`/`response_too_large`/`invalid_directory`/…）。唯一新增（随发现端点）：不存在/方法错 → 普通 404/405（FastAPI 默认），无自定义 code。

## 7. gzip / ETag [稳定——v3 不变]

内容协商、`Vary: Accept-Encoding[, X-Opencode-Directory]`（v3 下 directory 经 query，`Vary` 值是否去 directory 待 design-v3 评审）、`ETag`/`If-None-Match`/304 行为全部沿用 v2 语义（Batch 2 已实现）。envelope 化后 ETag 输入 = envelope body。

## 8. 可用性与里程碑 [计划——非承诺]

| 里程碑 | 内容 | 状态 |
|---|---|---|
| v1.6.0（已发） | v2 全量 + thin 路由齐（todo/children/diff） | ✅ 生产在线 |
| design-v3 定稿 | 本草案经 rev 面板门控评审（≥9.5）转正式契约 | 启动中 |
| v3 批次 1 | 发现端点 `GET /slimapi/versions` + `?v=3` 机制骨架 + envelope（messages/sessions/status） | 待实施（门控后） |
| v3 批次 2 | `?directory=` 全路由推广 + 头退役验证 + 契约/CHANGELOG | 待实施 |
| v2 移除 | ocdroid 改造完成、v2 流量归零后（access log 判定） | 远期 |

oc-webui 实现建议：**帧解析/SSE/错误体/gzip/ETag 直接按 §5-§7（稳定）实现；envelope/发现端点按 §2-§3 草案实现并把字段名隔离到常量层**；在发现端点上线前以 `GET /slimapi/health`（`server.api_version`）做临时自检。首个可联调 tag 会经跨会话通知送达。
