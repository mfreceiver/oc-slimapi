# Slim 会话消息可靠性阶段 B 联调启动提示词

> 只有双方阶段 A 均已独立发布并交换完整证据后，才将本提示词交给双方新会话。任何一方证据缺失时停止在审计阶段，不写联调代码。

```text
你负责启动 oc-slimapi 与 ocdroid 的 Slim 会话消息可靠性阶段 B 联调。

前置条件（全部满足才可实施）：
1. oc-slimapi 阶段 A 已有 commit、tag、artifact SHA-256、不可变 check log；
2. ocdroid 阶段 A 已有 commit、tag、artifact SHA-256、不可变 check log；
3. 双方版本与兼容矩阵已记录；
4. 用户已明确指定本次联调 owner、目标版本和实施范围；
5. 若协议、owner 或范围变化，先重新请求 rev-gpt 评审，不能沿用旧 9.5 门控。

先读取：
- 双方联合计划；
- oc-slimapi 的 v1-contract.md、design-v2.md、CLIENT_CHANGES.md；
- ocdroid 阶段 A 交付报告；
- 两项待冻结事项：Retrofit 原始调用 vs 旧 facade 静态审计精度；full/cursor snapshot merge union vs replacement。

阶段 B 首先冻结设计，不要边实现边改变协议：

1. `since-complete` capability：true/false/缺失的兼容语义；
2. `SlimDrainOutcome`：Success 仅 cursor-null terminal；cap/partial/timeout/取消/失败不推进 bookmark；
3. token-guarded authoritative commit：只承诺进程内，不宣称跨重启恢复；
4. watermark：现有 nullable 字段，统一 `updated > 0L && id.isNotBlank()`；明确同 message 多次追加、重复/乱序 digest、删除和 watermark 不倒退；
5. full/cursor snapshot merge：明确 union 或 replacement，并定义删除边界；
6. token done/idle/resync/backpressure/reconnect：provisional 内容保留与权威替换时序；
7. 集合 merge 继续使用现有 `MessageWithParts`，不创建第二套业务模型。

实施与验收：
- slim + SSE 开启、slim + SSE 关闭最终消息集合一致；
- 同一 assistant message 多次追加最终全文完整；
- SSE reconnect/backpressure、token done/idle 后不清空唯一可见内容；
- 空、完整、截断、partial、失败、超时、限流可区分；
- cursor fallback 有界、去重、退避；
- 重复/乱序 digest 不倒退 watermark；
- dirty 最终收敛、无无界重试；
- full/cursor merge 与删除/替换边界一致。

交付证据：
1. 双方修改文件和接口差异；
2. 联调测试矩阵与实际结果；
3. 兼容旧 sidecar/旧客户端的策略；
4. 是否需要 X-Slimapi-Version bump 及理由；
5. commit、tag、artifact SHA-256、不可变 check log。

在所有前置条件满足前，只能审计和冻结设计，不得写联调实现。
```
