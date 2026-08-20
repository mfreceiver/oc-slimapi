# D12 — A12 测试质量审计（tests/ 全量 + 580 键 gap 矩阵）

> 审计专项报告 · Phase 2 / A12 · 2026-08-20
> 快照：`0b836e7`（BASELINE_HEAD = 0b836e78c5de62d0c73b8593bf62c6650043dedf，release: v4.4.0）。本文全部 `file:line` / `tests/文件.py::test_name` 证据属该快照。
> 输入：`01-explore/expected-keys.csv`（580 键，**唯一行集来源，未重新推导**）、`01-explore/test-census.md`（109 文件普查）、`01-explore/route-census.csv`（54 路由）、`03-reports/D04-contract-quality.md` §4（72 条硬不变量清单）。
> 纪律：**未运行 pytest（含 --collect-only）**；测试存在性/行为判定全部经 rg + 读文件。证据索引器（函数体级 code/route/selector 提取）在 probes 命名空间运行（/tmp，不入仓），索引 2642 test 函数 == census 计数。
> 产物：`04-final/test-gap-matrix.csv`（580 行）；发现 F-316..F-326；F-018 复核。

---

## 0. 方法

- **矩阵构建**：以 expected-keys.csv 的 (method,path,behavior) 580 三元组为冻结行集；每路由以 route-census test_files 为起点、以字面路由串 rg + 逐测试函数体读断言定位锁定测试；四列填法——`v3_test` = 以 ?v=3（或默认 wire 3/selectorless 回退）锁定该 (路由,行为) 的测试；`v4_test` = 以 ?v=4/wire 4 面；`feature_off_test` = 相应 feature 门关闭态回归；`boundary_test` = 边界值（limit/cap 两侧、枚举边界）。引用形如 `tests/file.py::test_name`，多条 `|` 连接（≤5 代表 + `…`）。
- **gap_severity 判级**：P1 = D04 §4 硬不变量缺锁定且面被消费方直接使用（本轮 **0 条**；唯一无自动化锁定的 INV-03 是流程级 major 绑定规则，非路由面）；P2 = 该行为在本路由与家族代表处均无任何测试（本轮 **0 条**）；P3 = 仅有家族代表/中间件均匀层/函数级锁定（**241 条**）；NONE = 有直接锁定（**339 条**）。
- **「家族代表」约定**（重要）：selector 族错误（unsupported_version/invalid_version_selector/directory 三态）与 cap 族错误产生于共享中间件/共享管线。当断言仅落在家族代表端点（如写组的 abort/create）时，其余路由该行 cite 代表测试并记 P3——这是诚实呈现「锁定存在但非逐路由」的折中，已在 F-317 单列。

## 1. 矩阵总览（580 行）

| 维度 | 值 |
|---|---|
| 行数 / 唯一三元组 | 580 / 580（== expected-keys，集合相等，见 §6） |
| gap_severity 分布 | NONE 339 · P3 241 · **P2 0 · P1 0** |
| 列 NONE 占比 | v3_test 17.2%（100/580）· v4_test **89.3%**（518）· feature_off_test **98.3%**（570）· boundary_test **98.6%**（572） |
| NONE 占比最高的三类键 | ① boundary 类（14 键中 6 键无边界专用测试，靠 v3/v4 列承载；列维 572/580）② feature_off 类（仅 10 路由有该键且全部有锁，但**其余 570 行该列为 NONE**——feature 键本就稀疏）③ v4_face 类（35/53 行四列全 NONE，F-316） |
| P3 行按行为分类 | invalid_version_selector 45 · response_too_large 33 · v4_face 35 · unsupported_version 28 · directory_header_retired 26 · invalid_directory_selector 24 · directory_conflict 29 · request_too_large 11 · 其余零星 |

**核心结论**：v3 面锁定密度很高（v3_test 仅 17.2% NONE，v3_face 50 行 0 缺口，happy 4% 缺口）；系统性缺口集中在 **v4 面的逐路由 HTTP 级扫描缺席**（F-316）与**家族代表式错误锁定**（F-317）。54/54 路由、全部 46 类行为都有至少一层锁定——无 P2/P1 级真空。

## 2. 断言强度全量审

### 2.1 error_* 逐类抽查（精确整body vs code-only）

| 错误类 | 断言方式 | 代表证据 |
|---|---|---|
| unsupported_version | **精确整body**（`{"code","supported":[3,4]}`） | test_terminal_matrix.py:122、test_write_groups.py:286 |
| invalid_version_selector | 精确整body（单键） | test_terminal_matrix.py:165 |
| directory_conflict / header_retired / invalid_directory_selector | 精确整body（含 queryDirectory/headerDirectory 附加字段） | test_terminal_matrix.py:246-265、test_v4_dual_window.py:345-356 |
| directory_retired_in_v4 | 精确整body（RETIREMENT_BODY 逐键 + hint 全文） | test_v4_dual_window.py:271 |
| method_not_applicable（405） | 精确（code/method/allow 三键 + Allow 头字面 + no-store/Vary 信封惯例 + 零上游 IO） | test_method_boundary_v4.py:167-190 |
| tokens_stream_retired_in_v4 | 精确整body | test_sse_replay_wire.py:1079 |
| auxiliary_unavailable（503） | 精确 + Retry-After:30 + 六项泄露负向断言 | test_sessions_v4_matrix.py:268-277 |
| upstream_unavailable / upstream_http_\<N\> / transform_busy / too_large 族 | **多数 code-only**（217 处 `["code"]==` vs 全仓 73 处整body） | test_children_routes.py:280-292、test_permissions.py:294-346 等 |

计量：`resp.json() == {` 73 处 vs `["code"] == ` 217 处。transform_busy 的 Retry-After 在 children/directories/sessions 有例外锁定。**结论**：selector/providers 族（消费方直接解析的错误面）为精确锁定；上游映射族为 code 子集——F-320（P3）。

### 2.2 「逐字节等价」类声明核对

- **真·字节级（4 族）**：① test_v3_rawbody_regression.py:198-227——sessions 以 PYTHONHASHSEED=0 子进程取现场、body_hex 全等 + ETag 全等 + `--capture` 再生成通道；health/versions 同态归一 sidecarVersion 后字节比较（文件头如实披露该限定）；② test_sse_replay_wire.py:2480/2498/2514——events/stream v3 帧序列整 body 字节字面量全等 ×3；③ test_expand_href_v4.py:271-287——href 冻结字节子串 + 负向 `?v=4` 全body不出现；④ test_providers_projection_v4.py:967——v3 透传整 body 字节全等。
- **命名强于断言（4 例）**：test_versions_readiness.py:504（`byte_unchanged` → 4 键值）、test_v3_envelope.py:133（`byte_verbatim` → startswith+键序）、test_v4_dual_window.py:197（health "byte-regression" → 键级+负向）、test_messages_routes.py:803（cursor 字符串相等）→ **F-326**。
- test_v4_dual_window 双窗族：sessions echo 的 directory 梯子四断言全部 body 精确；versions 载荷跨视图整 payload 相等（test_versions_payload_identical_across_views:569）+ 键序冻结（test_versions_shape_exact:26-31）——强度合格，唯 health v3 回归是键级（见 F-326）。
- test_versions_* 族：caps3 整 dict 全等（:515-534）、caps4 六键集合+四静值（:505-527）、readiness 35 用例含全子集穷尽（test_ready_derivation_exhaustive_all_subsets:327）——v3 §3/§3a 与 v4 §3.1/§3.3 的锁定充分。

### 2.3 D04 移交项复核（v4 §11.3「144 格」）

**关闭**：test_sessions_v4_matrix.py:152-167 `MATRIX_IDS = REQ_CASES(3×4=12) × DB_STATES(3) × AL_STATES(2) × CURSOR_STATES(2)` = **144 参数 case**，由 :211 `test_matrix_144_degradation_cells` 逐格跑（每格断言 200/降级 200/503 三分支 + mirror oracle 对照）。census「36 参数化测试」计的是文件内测试函数数，与此不矛盾。D04 §1 §11 行的疑点（「未见逐格 144 断言」）**不成立**——144 格字面兑现。

## 3. flaky 风险复核（test-census §2 复核）

三类计数（A12 口径，函数体级重提取；与 census 行级口径方向一致无矛盾）：

| 类别 | A12 计数 | 说明 |
|---|---|---|
| **真延迟**（>0 墙钟 sleep） | **148 处，合计 ≈85.9s** | Top：10.0s×2（batch3 取消路径）、5s×7（子进程 argv）、4.0/3.5/2.2/3.0/1.2s×5；最重串行链 >30s 集中在 actions/full_absorb/transform/catalog_cache/leased_singleflight |
| **可控注入**（时间虚拟化） | **22 处** | counting_sleep/fast_sleep/FakeClock×2/_FrozenClock/_CountingDateTime（batch3、token_hub_lifecycle/flush、dbaux_lifecycle、replay_log、post_actions_v4、traffic_snapshot） |
| **真实时钟读参与断言** | **6 处** | b1b_sweep_shadow:149-151 与 post_actions_v4:406,604 夹逼（容忍型低危）；transform:358-390、full_absorb:180-197、messages_routes:338-340 **monotonic 参与行为断言**（高负载假失败向量）；globalhub_retired_gate:325 |

sleep(0) 让步 25 处（无墙钟依赖）。random/urandom 13 处全为安全用法（不可压缩体/shuffle/重生成循环；v4_fixture.py:86-87 显式规避 hash() 为正面样例）。结论维持 census：唯一真 flaky 向量是 3 处 monotonic 行为断言 + 大量 0.05-0.3s 竞态窗口的调度敏感性 → **F-321**（P3）。

## 4. 金样体系与 v4_fixture 可维护性

- **规模与消费**：tests/golden/ 2 件（mirror 21,485B / real 31,219B，均 v1.18.18 钉版）；消费方仅 test_equivalence_anchor.py（EQ-001..008）+ v4_fixture 自身（build_db_from_real_golden 逆映射）。in-tree 数据另含 msg40.json（443KB，test_skeleton 单消费）与孤儿 g_f1/（F-324）。
- **再生成机制**：mirror = `.venv/bin/python tests/v4_fixture.py --write-golden`（:868-880，golden 头内嵌同串 regenerate_hint）；real = `OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN=1 pytest -k eq007_real_golden`（需本机 1.18.18 二进制，:513-517）。**评审成本**：golden 头四元组（version/generator/dataset fingerprint/digest）+ regenerate_hint 使漂移定位可机械执行；real golden 需二进制在场是唯一摩擦（缺席时仅无法再生成，校验仍无条件执行）。
- **漂移检测（三层，实读确认）**：`validate_golden`（:450-482，头逐键强制 + 载荷与镜像 oracle 全量相等 + response_fingerprint 交叉定位数据集层/管线层漂移）；`validate_real_golden_ci`（:642-764，顶层键集 frozenset 冻结 + dataset_manifest 全字典相等 + 注入清单逐字段比对 + 排序单调性）；装载即校验（load_golden/load_real_golden assert 失败即红，real 校验不随二进制缺席而 skip）。
- **确定性保障**：FIXED_NOW_MS 常数（:40）、sha256 派生代替 hash()（:86-87）、ALIGNED_VERSION 常数（:33）——设计自洽。
- **可维护性判定**：v4_fixture.py 884 行承载「七维度数据集 + 独立镜像 oracle + 双 golden 生成/校验 + DB 重建」，文件头 S-B03 隔离铁律（不 import 生产谓词、stdlib json 而非 orjson）是防「oracle 与生产同错」的关键设计且被遵守。**风险**：oracle 与生产投影的语义漂移只能靠 real golden（真实上游）兜底——eq007 缺席二进制时 mirror oracle 独大；golden 数量少（2）评审成本低。整体评价：**本仓测试基建的最强项，无新增发现**。

## 5. 测试反模式清点

| 检查 | 结果 |
|---|---|
| `assert True` / 恒真 | `assert True` 0 处；恒真式 1 处：test_traffic_ledger.py:588 `… or True` → **F-319** |
| pytest.skip / skipif | 1 处：test_equivalence_anchor.py:689（真实二进制缺席门控，**正当**——版本漂移时 fail 而非 skip，:690-697） |
| xfail | `pytest.mark.xfail` **0 处**；4 处注释声称存在 xfail 标记（已随修复移除未同步）→ **F-318** |
| 空体/`pass` 用例 | 0（10 处 `pass` 均为 except 块/空回调/类体，正当） |
| 自定义 marker | `@pytest.mark.integration`（test_hub_behavior_lock.py:397）未注册 → **F-323** |
| mock 过度判定 | 上游 mock 全走 httpx 原生 MockTransport（37 文件）+ ASGITransport（69 文件）+ conftest 单一 upstream_factory；无 respx 抽象层；真实资源仅三类且全部隔离（tmp_path sqlite / 短命子进程 / 隔离 ephemeral opencode 实例 + 真实 WAL 库）。**结论：mock 边界健康，无「测 mock 而非测行为」面**；反向问题（respx 死依赖）归 F-018 |
| 死数据 | tests/fixtures/g_f1/ 零引用 → **F-324** |

## 6. CSV 自查声明（人工保证，未运行外部校验器）

1. **三元组唯一**：580 行 (method,path,behavior) 无重复（脚本断言 `len(keys)==len(set(keys))==580` 通过）。
2. **8 列齐全非空**：表头恰为冻结 schema `method,path,behavior,v3_test,v4_test,feature_off_test,boundary_test,gap_severity`；全部单元格非空，缺测显式 `NONE`；gap_severity 值域实测 `{NONE,P3} ⊆ {P1,P2,P3,NONE}`。
3. **行集合 == expected-keys**：以 csv.DictReader 双向比对，`(exp-got)=0、(got-exp)=0`，**集合相等**；exp 580 行 / got 580 行。
4. **RFC 4180**：csv.writer QUOTE_MINIMAL + `\r\n` 行终止，实测 581 个 CRLF、0 个裸 LF；含逗号/引号字段（如 `upstream_http_<N>` 无逗号，测试引用串无逗号——本轮输出未触发引号包裹，机制在位）。
5. behavior 枚举 = expected-keys 第三列实际值（46 类：happy_path / v3_face / v4_face / feature_off / boundary / error_\<code\> 41 类），未引入新值。

## 7. 发现清单（A12 主辖）

| 编号 | 严重度 | 标题 |
|---|---|---|
| F-316 | P3 | 35/53 路由 v4 面无 HTTP 级测试；test-census「~35 路由全扫」口径失实（函数级 ≠ HTTP 级） |
| F-317 | P3 | 错误路径家族代表式锁定（conflict/multi-value/cap 仅 abort/create 单端点；241 P3 行的主体构成） |
| F-318 | P3 | 两文件声称的 xfail(strict=False) 标记不存在（TDD 脚手架注释过时） |
| F-319 | P3 | 恒真断言 test_traffic_ledger.py:588（`or True`） |
| F-320 | P3 | 错误体断言强度两极：217 code-only vs 73 整body；上游映射族子集断言 |
| F-321 | P3 | flaky 面：148 真延迟 ≈86s + 3 处 monotonic 行为断言（高负载假失败向量） |
| F-322 | P3 | 写组 request_too_large 仅超限侧；全仓唯一 at-cap 边界测试在 actions |
| F-323 | P3 | `pytest.mark.integration` 未注册（PytestUnknownMarkWarning / strict-markers 隐患） |
| F-324 | P3 | 孤儿 fixture tests/fixtures/g_f1/（census §3.3 复核确认） |
| F-325 | P3 | events 订阅上限错误仅 registry 单元级锁定，HTTP 503 映射无路由级断言 |
| F-326 | P3 | 「byte/verbatim/frozen」命名与字段级断言错位；真字节级回归仅 4 族 |

F-018（respx 死依赖）：A15 已终判 verified/P3，A12 复核认同并附记（无测试有效性影响）。

## 8. 负向结论（审计过、判定无问题）

1. **D04 72 条硬不变量的测试锁定复核**：除 INV-03（流程级 major 绑定，无自动化断言——D04 已记 P3）与 INV-60 的 grep 机制未落地（F-156，行为锁定存在）外，70 条均有测试名级直接锁定；矩阵 0 条 P1/P2 与 D04「71/72 锁定」结论互相印证。
2. **readiness 门控双态**（SATISFIED 开/关）10 文件、**功能开关 off 回归**（coalesce/fingerprint/ttl/etag/allowlist/actions-disabled/post-actions gate）覆盖 10 个 feature_off 键全部有锁——矩阵 feature_off 行 0 缺口。
3. **双 authority 等价锚**（EQ-001..008）：golden + 真实进程双源、mirror oracle 独立实现纪律受检无违例。
4. **mock 边界**：全 httpx 原生、无 respx、无端口监听、真实资源全隔离（§5）。
5. **v4 §11 12 项矩阵**：12/12 有对应测试文件且 11.3 的 144 格字面兑现（§2.3，关闭 D04 移交疑点）。
6. **v3 §11 18 项**：D04/F-156 已裁（15/18 + 3 偏差），A12 无追加。

## 9. 总评

测试体系是本仓工程质量的高水位面：2642 用例 / 66k 行，selector 终态、readiness 门控、SSE replay、dbaux 等价锚、providers 投影的锁定密度与断言精度均属上乘，时间虚拟化与真实资源隔离纪律清晰。系统性弱点全部为 P3 级「广度不均」：v4 面逐路由扫描缺席（35 路由）、错误锁定依赖家族代表、错误体子集断言、命名强度高估——共同指向同一根因：**parametrize 与断言强度纪律存在但未全仓强制**。无 P0-P2 级测试真空，无失效测试（除 1 处恒真断言）。

---

*（完）A12 · D12 · 快照 0b836e7 · 2026-08-20*
