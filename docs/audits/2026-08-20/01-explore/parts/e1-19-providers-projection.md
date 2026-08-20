### src/oc_slimapi/providers_projection.py（433）
- 职责：v4-contract §12 providers 白名单投影的纯逻辑模块（decode→validate→project→count→serialize→cap→gzip→ETag ⑥-⑪ 全链一个 worker 作业）。
- 对外符号：
  - `MAX_PROVIDERS=256` / `MAX_MODELS_PER_PROVIDER=1024` / `MAX_VARIANTS_PER_MODEL=64` / `MAX_PROJECTED_BODY_BYTES=8_388_608`（:54-57，§12.4 冻结 wire 常量，禁 env 覆盖）
  - `PROVIDERS_REPRESENTATION_VERSION=b"providers-projection-v2"`（:66，修订三 2026-08-20 恢复 `limit` 子对象导致指纹 bump）
  - `_ORJSON_INT_MIN=-(2**63)` / `_ORJSON_INT_MAX=2**64-1`（:80-81，limit int 值域 = orjson 可序列化范围，超界走省略路径）
  - `ProviderUpstreamMalformed(ValueError)`（:86，§12.5.3 → 502 `provider_upstream_malformed`）
  - `ProviderProjectionLimit(Exception)`（:97，§12.5.3 → 413 `provider_projection_limit`；`limit`/`limit_value` 属性）
  - `providers_rep_version(config) -> bytes|None`（:111，§12.6 指纹 = etag-v1 \0 providers-projection-v2 \0 四常量 \0 wire=v4；etag_enabled=false → None；骨架 config 字段不参与）
  - `_reject_duplicate_members` / `_loads_strict`（:137/:149，stdlib json 严格解码，orjson 会静默吞重复键故走 stdlib）
  - `_ensure_utf8` / `_require_str` / `_validate`（:161/:180/:187，⑦ 全量校验：顶层恰两键、逐串 UTF-8 可编码（lone surrogate → malformed）、models key==Model.id、嵌套 providerID 一致、provider id 全局唯一、default 三元组）
  - `_utf8_key` / `_project`（:288/:295，§12.2 UTF-8 字节序排序 + §12.4 first-triggered-wins 计数 tripwire（不截断）；optional 键 string-else-omit；`variants` 只发排序键数组；修订三 `limit` 子键白名单 {context,input,output} 逐子键 int-else-omit、bool 排除、零存活子键 → 整键省略）
  - `project_and_pack(body, *, accept_encoding, rep_version)`（:376，单一 worker 作业 ⑥-⑪；返回 (encoded, headers)，headers 含 Vary: Accept-Encoding 恒发 + ETag（strong identity/weak gzip，恒 hash canonical identity 字节）；⑫ If-None-Match 判断留在调用方主上下文）
- 依赖：`etag`（compute_etag）、`gzip_util`（compress_if_beneficial）、orjson/json/gzip。
- 被依赖：`routes/read_groups.py`（:62 import，:353-355 映射 413）。
- 状态/可变性：无（纯函数模块）。
- 错误路径：`ValueError` 兜底归一为 ProviderUpstreamMalformed（:421-426，orjson JSONEncodeError/UnicodeEncodeError 防泄漏 500）；ProviderProjectionLimit 原样上抛。
- 疑问点：
  1. `provider_projection_limit` 为 wire 码但 inventory 正则（code= 单行模式）未捕获（多行构造 read_groups.py:353-355）——E2/A4 全量对账须以 rg 为准修正（34 → ≥35）。
  2. `_loads_strict` 用 stdlib json（重复键拒绝）→ 大 body 解码性能低于 orjson，但 8MiB cap 在 ⑩（投影后）而非解码前——解码前的上游 body 上限由路由 read cap 承担（E5 场景 6 核对）。
  3. `_validate` 对 `models.values()` 遍历两次校验（:227-243 类型 + :249-257 关系），无复杂度问题但 O(2N)。
  4. `providers_rep_version` 不含 Accept-Encoding 协商状态（coding 区分由 compute_etag 的 actual 参数承载）——与 etag.py 一致，无问题，记录以免误判。

### src/oc_slimapi/sse/__init__.py（1）
- 职责：包 docstring（"Curated SSE bridge package."），无再导出。
- 对外符号：无。依赖：无。被依赖：包标记。状态：无。错误路径：无。
- 疑问点：无。

### src/oc_slimapi/sse/tokenstream/__init__.py（8）
- 职责：tokenstream 包聚合门面——从 frames/models/hub/subscriber 再导出 16 符号（含 `_connected_frame` 等 8 个私有名）。
- 对外符号：`STOP`、`sse_frame`、`PartKey`、`DeltaAccumulator`、`LivePart`、`_TokenMetrics`、`TokenStreamHub`、`TokenSubscriber`、`TokenSubscriberCapacityError`、`TokenStreamRegistry` 及 6 个 `_xxx_frame`/`_now_ms` 私有再导出。
- 依赖：.frames/.models/.hub/.subscriber。被依赖：`sse/token_hub.py` shim 经包路径转发；生产代码直接 import 点见 E1-05 卡（app.py:36、routes/token_stream.py:61、global_hub.py:55）。
- 状态：无。错误路径：无。
- 疑问点：私有符号（`_frame` 系列、`_now_ms`、`_TokenMetrics`）经 `__init__` 公开化——与 frames.py 卡片「三处双实现漂移」疑问叠加，扩大了 shim 面积。
