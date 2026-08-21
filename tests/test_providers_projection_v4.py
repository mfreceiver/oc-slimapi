"""v4-contract §12 provider safe projection tests (TDD).

`GET /slimapi/config/providers?v=4` — projection pipeline:

* wire schema: top level exactly {providers, default}; unknown nested
  fields recursively discarded; ``models`` map key == Model.id;
  ``variants`` → sorted map-key array; optional keys (source/status)
  omitted unless well-typed;
* §12.2 ordering (providers by id byte order, models by map-key byte
  order, variants by key byte order, default keys sorted);
* §12.3 default triple validation;
* §12.4 four frozen limits (providers=256, models_per_provider=1024,
  variants_per_model=64, projected_body_bytes=8 MiB) — exact / +1;
* §12.5 error contract (502 provider_upstream_malformed, 413
  provider_projection_limit w/ limit+limitValue, 502 upstream_http_N,
  503 upstream_unavailable / transform_busy, 413 response_too_large
  with limitBytes) + twelve-step evaluation order (body cap BEFORE
  parse; permit AFTER body cap so network wait never holds it;
  transform_busy BEFORE malformed);
* §12.6 ETag (canonical bytes = wire body, identity strong / gzip
  weak, Vary: Accept-Encoding, 304 via If-None-Match; a foreign
  validator never 304s the v4 view);
* no-selector byte-identical passthrough (selector-less stack keeps the
  frozen default-view passthrough).
"""

from __future__ import annotations

import gzip
import json
from contextlib import asynccontextmanager

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import etag as etag_mod
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.providers_projection import (
    MAX_MODELS_PER_PROVIDER,
    MAX_PROJECTED_BODY_BYTES,
    MAX_PROVIDERS,
    MAX_VARIANTS_PER_MODEL,
    PROVIDERS_REPRESENTATION_VERSION,
    ProviderProjectionLimit,
    project_and_pack,
    providers_rep_version,
)
from oc_slimapi.routes import read_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
GZIP_OK = {"Accept-Encoding": "gzip"}
ROUTE = "/slimapi/config/providers"
UPSTREAM_PATH = "/config/providers"


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=1024 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _canonical(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _model(mid: str, pid: str, *, name: str | None = None,
           status=None, variants=None, extra: dict | None = None) -> dict:
    """An upstream Model entry (§12.1 upstream shape)."""
    out: dict = {
        "id": mid,
        "providerID": pid,
        "api": {"type": "native", "settings": {"seed": "LEAK"}},
        "name": name if name is not None else f"name-{mid}",
        "capabilities": {"attachment": False},
        "cost": {"input": 3},
        "limit": {"context": 8192},
        "status": status,
        "options": {"seed": "LEAK"},
        "headers": {"x-leak": "1"},
        "release_date": "2020-01-01",
    }
    if status is None:
        # absent, not null — absent and null are both omit-on-output, but
        # exercise the absent form here; the null form has its own case.
        del out["status"]
    if variants is not None:
        out["variants"] = variants
    if extra:
        out.update(extra)
    return out


def _rich_doc() -> dict:
    """A valid upstream ConfigProvidersResult with junk at every level."""
    return {
        "providers": [
            {
                "id": "openai",
                "name": "OpenAI",
                "source": 123,  # non-string optional → omitted
                "env": ["OPENAI_API_KEY"],  # discarded
                "key": "sk-leak",  # discarded
                "options": {"leak": True},  # discarded
                "models": {
                    "gpt-4o": _model(
                        "gpt-4o", "openai",
                        status=None,
                        extra={"family": "gpt", "unknown_model_key": [1, 2]},
                    ),
                },
            },
            {
                "id": "anthropic",
                "name": "Anthropic",
                "source": "config",
                "unknown_provider_key": {"deep": {"junk": 1}},
                "models": {
                    "claude-sonnet-4": _model(
                        "claude-sonnet-4", "anthropic", status="active",
                        variants={"chrome": {"junk": 1}, "vscode": {},
                                  "a-b": {"x": None}},
                    ),
                    "claude-opus-4": _model("claude-opus-4", "anthropic"),
                    "claude-haiku": _model(
                        "claude-haiku", "anthropic",
                        variants={},  # empty map → []
                    ),
                },
            },
        ],
        "default": {"anthropic": "claude-sonnet-4", "openai": "gpt-4o"},
    }


def _rich_projected() -> dict:
    return {
        "default": {"anthropic": "claude-sonnet-4", "openai": "gpt-4o"},
        "providers": [
            {
                "id": "anthropic",
                "name": "Anthropic",
                "source": "config",
                "models": [
                    {"id": "claude-haiku", "name": "name-claude-haiku",
                     "providerID": "anthropic",
                     "limit": {"context": 8192}, "variants": []},
                    {"id": "claude-opus-4", "name": "name-claude-opus-4",
                     "providerID": "anthropic",
                     "limit": {"context": 8192}},
                    {"id": "claude-sonnet-4", "name": "name-claude-sonnet-4",
                     "providerID": "anthropic", "status": "active",
                     "limit": {"context": 8192},
                     "variants": ["a-b", "chrome", "vscode"]},
                ],
            },
            {
                "id": "openai",
                "name": "OpenAI",
                "models": [
                    {"id": "gpt-4o", "name": "name-gpt-4o",
                     "providerID": "openai",
                     "limit": {"context": 8192}},
                ],
            },
        ],
    }


def _simple_doc(n_providers: int = 1, n_models: int = 1,
                n_variants: int | None = None) -> dict:
    providers = []
    default = {}
    for p in range(n_providers):
        pid = f"p{p:04d}"
        models = {}
        for m in range(n_models):
            mid = f"m{m:05d}"
            variants = None
            if n_variants is not None:
                variants = {f"v{i:03d}": {"junk": i} for i in range(n_variants)}
            models[mid] = _model(mid, pid, variants=variants)
        providers.append({"id": pid, "name": pid, "models": models})
        default[pid] = "m00000" if n_models else ""
    if n_models == 0:
        # a provider with zero models cannot be a default target
        default = {}
    return {"providers": providers, "default": default}


def _build_app(handler, *, settings: Settings | None = None):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes))
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


@asynccontextmanager
async def _stack(handler, *, settings: Settings | None = None):
    app, seen = _build_app(handler, settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://slimapi") as client:
        yield client, app, seen


def _ok(content: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content,
                              headers={"Content-Type": "application/json"})
    return handler


def _json_response(status: int, payload) -> httpx.Response:
    return httpx.Response(status, content=orjson.dumps(payload),
                          headers={"Content-Type": "application/json"})


def _raw(status: int, body: bytes,
         content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(status, content=body,
                          headers={"Content-Type": content_type})


# --- §12.1/§12.2 happy path + deterministic discard ------------------------


async def test_v4_happy_path_projection_shape_and_determinism():
    doc = _rich_doc()
    async with _stack(_ok(_canonical(doc))) as (client, _app, _seen):
        r1 = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        r2 = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("application/json")
    assert r1.headers["vary"] == "Accept-Encoding"
    assert r1.headers["cache-control"] == "no-store"
    assert r1.content == _canonical(_rich_projected())
    # deterministic: same upstream input → byte-identical output + ETag
    assert r2.content == r1.content
    assert r2.headers.get("etag") == r1.headers.get("etag")
    assert r1.headers["etag"].startswith('"')  # identity → strong


async def test_v4_key_order_is_canonical_not_upstream():
    # upstream providers/models in NON-sorted order → projected sorted
    doc = {
        "providers": [
            {"id": "zzz", "name": "Z", "models": {
                "b-model": _model("b-model", "zzz"),
                "a-model": _model("a-model", "zzz"),
            }},
            {"id": "aaa", "name": "A", "models": {
                "m": _model("m", "aaa"),
            }},
        ],
        "default": {"zzz": "b-model", "aaa": "m"},
    }
    async with _stack(_ok(_canonical(doc))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["providers"]] == ["aaa", "zzz"]
    assert [m["id"] for m in body["providers"][1]["models"]] == [
        "a-model", "b-model"]
    assert list(body["default"].keys()) == ["aaa", "zzz"]  # sorted


async def test_v4_optional_keys_all_omission_forms():
    doc = {
        "providers": [{
            "id": "p", "name": "P", "source": None,  # null → omitted
            "models": {
                "m1": _model("m1", "p", status=123),  # non-string → omitted
                "m2": _model("m2", "p", status="beta"),
                "m3": _model("m3", "p", variants={}),
            },
        }],
        "default": {},
    }
    async with _stack(_ok(_canonical(doc))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    provider = r.json()["providers"][0]
    assert "source" not in provider
    models = {m["id"]: m for m in provider["models"]}
    assert "status" not in models["m1"]
    assert models["m2"]["status"] == "beta"
    assert models["m3"]["variants"] == []
    assert "variants" not in models["m1"]
    assert "variants" not in models["m2"]


# --- [2026-08-20 修订三] ModelEntry optional `limit` projection -------------
#
# 修订三（owner 批准；消费方 oc-webui 已确认嵌套形状）：ModelEntry 恢复
# optional ``limit``——模型规格参数（上下文窗口分母），非敏感信息。子键
# 白名单恰好 {context, input, output}，逐子键独立 int-else-omit（bool 显式
# 排除——isinstance(True, int) 为 True 是 Python 陷阱）；limit 的任何上游
# 形态都不产生 malformed；零子键存活 → 整键省略（绝无 "limit": null /
# "limit": {}）。


def _model_no_limit(mid: str, pid: str) -> dict:
    """Upstream Model with the limit key entirely absent."""
    model = _model(mid, pid)
    del model["limit"]
    return model


async def test_v4_limit_three_int_subkeys_passthrough_verbatim():
    # webui 实际案例量级：context=1000000 大窗口 + input/output 逐字透传。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"context": 1000000, "input": 500000,
                      "output": 64000}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    model = r.json()["providers"][0]["models"][0]
    assert model["limit"] == {"context": 1000000, "input": 500000,
                              "output": 64000}


async def test_v4_limit_absent_null_non_object_omitted_never_malformed():
    # limit absent / null / "str" / 42 / [1] / true → 整键省略，一律 200
    # （§12.1 optional 省略策略；无新增 malformed 路径）。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {
            "m0": _model_no_limit("m0", "p0"),
            "m1": _model("m1", "p0", extra={"limit": None}),
            "m2": _model("m2", "p0", extra={"limit": "8192"}),
            "m3": _model("m3", "p0", extra={"limit": 42}),
            "m4": _model("m4", "p0", extra={"limit": [1]}),
            "m5": _model("m5", "p0", extra={"limit": True}),
        },
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200  # 修订三：limit 无任何 malformed 形态
    models = {m["id"]: m for m in r.json()["providers"][0]["models"]}
    for mid in ("m0", "m1", "m2", "m3", "m4", "m5"):
        assert "limit" not in models[mid], mid


async def test_v4_limit_subkeys_independent_int_else_omit():
    # 子键 null / str / float → 该子键省略、其余存活；子键 bool → 省略
    # （防 isinstance(True, int) 陷阱——显式排除 bool）。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {
            "m0": _model("m0", "p0", extra={
                "limit": {"context": None, "input": 100, "output": 200}}),
            "m1": _model("m1", "p0", extra={
                "limit": {"context": "8192", "input": 100, "output": 200}}),
            "m2": _model("m2", "p0", extra={
                "limit": {"context": 8192.5, "input": 100, "output": 200}}),
            "m3": _model("m3", "p0", extra={
                "limit": {"context": True, "input": False, "output": 200}}),
        },
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    models = {m["id"]: m for m in r.json()["providers"][0]["models"]}
    assert models["m0"]["limit"] == {"input": 100, "output": 200}
    assert models["m1"]["limit"] == {"input": 100, "output": 200}
    assert models["m2"]["limit"] == {"input": 100, "output": 200}
    assert models["m3"]["limit"] == {"output": 200}


async def test_v4_limit_all_subkeys_non_int_omits_whole_key():
    # 三子键全部非 int → limit 整键省略（绝无 "limit": {}）；本 doc 仅
    # 一个 model，故整个响应体不得出现 "limit" 字样。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"context": "x", "input": None, "output": True}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert "limit" not in r.json()["providers"][0]["models"][0]
    assert b'"limit"' not in r.content  # 无 {} / null / 任何形态


async def test_v4_limit_unknown_subkeys_dropped():
    # 未知子键（limit.reasoning）丢弃不报错——与递归丢弃未知字段一致。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"context": 8192, "reasoning": 1}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert r.json()["providers"][0]["models"][0]["limit"] == \
        {"context": 8192}


async def test_v4_limit_subkeys_canonical_byte_order():
    # 上游子键乱序（output/input/context）→ canonical 输出按 UTF-8 字节序
    # context < input < output（OPT_SORT_KEYS，§12.6 口径）。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"output": 3, "input": 2, "context": 1}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert b'"limit":{"context":1,"input":2,"output":3}' in r.content
    idx = r.content.index(b'"limit"')
    segment = r.content[idx:idx + 64]
    assert segment.index(b'"context"') < segment.index(b'"input"') < \
        segment.index(b'"output"')


async def test_v4_limit_revision3_bumps_representation_fingerprint():
    # 修订三字段集演进必须轮换表示域指纹：同输入下与 bump 前的
    # providers-projection-v1 validator 不同（旧 v4 ETag 全部自然失效
    # 重拉；v3 校验器域不受影响——见 test_v4_validator_domain_isolated_
    # from_v3）。
    assert PROVIDERS_REPRESENTATION_VERSION == b"providers-projection-v2"
    rep = providers_rep_version(_settings())
    assert b"providers-projection-v2" in rep
    assert b"providers-projection-v1" not in rep
    canonical = _canonical({"providers": [], "default": {}})
    old = etag_mod.compute_etag(
        canonical, "identity", b"providers-projection-v1")
    new = etag_mod.compute_etag(canonical, "identity", rep)
    assert old != new


async def test_v4_limit_int_range_is_orjson_serializable():
    # P1 回归（rev-cgpt 修订三轮询）：int-else-omit 的「int」= 冻结
    # canonical 算法（orjson）可序列化整数范围 [-(2**63), 2**64-1]
    # （2026-08-20 实证：2**64-1 OK、2**64 与 -(2**63)-1 抛
    # TypeError "Integer exceeds 64-bit range"）。边界四点 + 10**30：
    # 超界 → 与非 int 同路径省略该子键，响应 200，绝无 502
    # provider_upstream_malformed（「limit 任意上游形态零错误」冻结）。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {
            "m0": _model("m0", "p0", extra={
                "limit": {"context": 2**64 - 1}}),           # 上界逐字透传
            "m1": _model("m1", "p0", extra={
                "limit": {"context": 2**64, "output": 8}}),   # 超上界省略
            "m2": _model("m2", "p0", extra={
                "limit": {"context": -(2**63)}}),             # 下界逐字透传
            "m3": _model("m3", "p0", extra={
                "limit": {"context": -(2**63) - 1, "input": 7}}),  # 超下界省略
            "m4": _model("m4", "p0", extra={
                "limit": {"context": 10**30}}),               # 远超界省略
        },
    }])
    # orjson 无法序列化超界整数（正是被测风险），故上游载荷经 stdlib
    # json 编码——模拟任意异构上游的 wire 形态，与 _surrogate_json 同法。
    async with _stack(_ok(json.dumps(doc).encode("ascii"))) as (
            client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200  # 不 502、不 500——零错误路径
    assert b"provider_upstream_malformed" not in r.content
    models = {m["id"]: m for m in r.json()["providers"][0]["models"]}
    assert models["m0"]["limit"] == {"context": 2**64 - 1}
    assert models["m1"]["limit"] == {"output": 8}
    assert models["m2"]["limit"] == {"context": -(2**63)}
    assert models["m3"]["limit"] == {"input": 7}
    assert "limit" not in models["m4"]  # 唯一子键超界 → 整键省略
    assert b"18446744073709551616" not in r.content  # 2**64 不在 wire 上
    assert b"1000000000000000000000000000000" not in r.content


async def test_v4_limit_zero_and_negative_ints_passthrough_verbatim():
    # P2-1：0/负整数是合法 int → 逐字透传（钉住：不得加正数过滤）。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"context": 0, "input": -1, "output": -5}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert r.json()["providers"][0]["models"][0]["limit"] == \
        {"context": 0, "input": -1, "output": -5}


async def test_v4_limit_subkey_nested_object_or_array_omitted():
    # P2-2：子键值为嵌套 object/array → 该子键省略、其余合法 int 存活。
    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", extra={
            "limit": {"context": {"a": 1}, "input": [1, 2],
                      "output": 4096}})},
    }])
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert r.json()["providers"][0]["models"][0]["limit"] == \
        {"output": 4096}


# --- §12.1 malformed matrix ------------------------------------------------


def _malformed_cases() -> list[tuple[str, bytes]]:
    good = _rich_doc()
    cases: list[tuple[str, bytes]] = []

    def mut(name: str, fn) -> None:
        import copy
        doc = copy.deepcopy(good)
        fn(doc)
        cases.append((name, _canonical(doc)))

    mut("extra_top_level_key", lambda d: d.update({"current": []}))
    mut("missing_default", lambda d: d.pop("default"))
    mut("missing_providers", lambda d: d.pop("providers"))
    mut("providers_not_list", lambda d: d.update({"providers": {}}))
    mut("default_not_map", lambda d: d.update({"default": ["x"]}))
    mut("provider_entry_not_object", lambda d: d["providers"].append("x"))
    mut("provider_missing_name",
        lambda d: d["providers"][0].pop("name"))
    mut("provider_missing_models",
        lambda d: d["providers"][0].pop("models"))
    mut("provider_models_not_map",
        lambda d: d["providers"][0].update({"models": []}))
    mut("model_not_object",
        lambda d: d["providers"][1]["models"].update({"x": 5}))
    mut("model_missing_id",
        lambda d: d["providers"][1]["models"]["claude-opus-4"].pop("id"))
    mut("map_key_not_model_id",
        lambda d: d["providers"][1]["models"]["claude-opus-4"].update(
            {"id": "claude-opus-4 "}))
    mut("model_missing_name",
        lambda d: d["providers"][1]["models"]["claude-opus-4"].pop("name"))
    mut("model_missing_provider_id",
        lambda d: d["providers"][1]["models"]["claude-opus-4"].pop(
            "providerID"))
    mut("model_provider_id_mismatch",
        lambda d: d["providers"][1]["models"]["claude-opus-4"].update(
            {"providerID": "openai"}))
    mut("duplicate_provider_id",
        lambda d: d["providers"].append(
            {"id": "anthropic", "name": "dup", "models": {}}))
    mut("default_key_unknown",
        lambda d: d["default"].update({"nope": "gpt-4o"}))
    mut("default_value_not_model_of_provider",
        lambda d: d["default"].update({"openai": "claude-sonnet-4"}))
    mut("default_value_not_string",
        lambda d: d["default"].update({"openai": None}))
    mut("variants_not_map",
        lambda d: d["providers"][1]["models"]["claude-sonnet-4"].update(
            {"variants": ["chrome"]}))
    mut("provider_id_not_string",
        lambda d: d["providers"][0].update({"id": 7}))

    cases.append(("top_level_not_object", b"[1,2,3]"))
    cases.append(("bad_json", b"{not json"))
    cases.append(("empty_body", b""))
    cases.append(("duplicate_json_member",
                  b'{"providers": [], "default": {}, "default": {}}'))
    cases.append(("duplicate_json_member_nested",
                  b'{"providers": [], "default": {"a": "b", "a": "c"}}'))
    return cases


@pytest.mark.parametrize("name,payload", _malformed_cases(),
                         ids=[c[0] for c in _malformed_cases()])
async def test_v4_malformed_matrix(name, payload):
    async with _stack(_ok(payload)) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502, name
    assert r.json() == {"code": "provider_upstream_malformed"}
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["vary"] == "Accept-Encoding"


async def test_v4_non_200_2xx_is_malformed():
    async with _stack(
            lambda req: _raw(204, b"")) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502
    assert r.json() == {"code": "provider_upstream_malformed"}


async def test_v4_empty_providers_ok():
    async with _stack(_ok(b'{"providers": [], "default": {}}')) as (
            client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    assert r.content == b'{"default":{},"providers":[]}'


# --- §12.3 default triple validation ---------------------------------------


async def test_v4_default_triple_cross_provider_model_invalid():
    # model lives under provider "b" but has providerID "a" would already
    # fail the relational check; here the value is a model of ANOTHER
    # provider — fails rule (2) of the triple.
    doc = {
        "providers": [
            {"id": "a", "name": "A", "models": {
                "ma": _model("ma", "a")}},
            {"id": "b", "name": "B", "models": {
                "mb": _model("mb", "b")}},
        ],
        "default": {"a": "mb"},
    }
    async with _stack(_ok(_canonical(doc))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502
    assert r.json() == {"code": "provider_upstream_malformed"}


async def test_v4_default_triple_happy():
    doc = _simple_doc(n_providers=3, n_models=2)
    async with _stack(_ok(_canonical(doc))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == {"p0000": "m00000", "p0001": "m00000",
                               "p0002": "m00000"}


# --- §12.4 four limits: exact / +1 -----------------------------------------


async def test_v4_limit_providers_exact_and_over():
    exact = _simple_doc(n_providers=MAX_PROVIDERS, n_models=0)
    over = _simple_doc(n_providers=MAX_PROVIDERS + 1, n_models=0)
    async with _stack(_ok(_canonical(exact))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        assert r.status_code == 200
        assert len(r.json()["providers"]) == MAX_PROVIDERS
    async with _stack(_ok(_canonical(over))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 413
    assert r.json() == {"code": "provider_projection_limit",
                        "limit": "providers", "limitValue": MAX_PROVIDERS}
    assert r.headers["cache-control"] == "no-store"


async def test_v4_limit_models_exact_and_over():
    exact = _simple_doc(n_providers=1, n_models=MAX_MODELS_PER_PROVIDER)
    over = _simple_doc(n_providers=1,
                       n_models=MAX_MODELS_PER_PROVIDER + 1)
    async with _stack(_ok(_canonical(exact))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        assert r.status_code == 200
        assert len(r.json()["providers"][0]["models"]) == \
            MAX_MODELS_PER_PROVIDER
    async with _stack(_ok(_canonical(over))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 413
    assert r.json() == {"code": "provider_projection_limit",
                        "limit": "models_per_provider",
                        "limitValue": MAX_MODELS_PER_PROVIDER}


async def test_v4_limit_variants_exact_and_over():
    exact = _simple_doc(n_providers=1, n_models=1,
                        n_variants=MAX_VARIANTS_PER_MODEL)
    over = _simple_doc(n_providers=1, n_models=1,
                       n_variants=MAX_VARIANTS_PER_MODEL + 1)
    async with _stack(_ok(_canonical(exact))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        assert r.status_code == 200
        assert len(r.json()["providers"][0]["models"][0]["variants"]) == \
            MAX_VARIANTS_PER_MODEL
    async with _stack(_ok(_canonical(over))) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 413
    assert r.json() == {"code": "provider_projection_limit",
                        "limit": "variants_per_model",
                        "limitValue": MAX_VARIANTS_PER_MODEL}


async def test_v4_limit_projected_body_bytes():
    # 128 × 1024 entries with ~50-char ids/names ≈ 20 MiB identity —
    # over the 8 MiB frozen cap while every count limit is at/below its
    # own cap (models exactly at limit, providers below).
    providers = []
    for p in range(128):
        pid = f"provider-{p:03d}"
        filler = "x" * 50
        models = {}
        for m in range(MAX_MODELS_PER_PROVIDER):
            mid = f"{filler}-{m:05d}"
            models[mid] = _model(mid, pid, name=filler)
        providers.append({"id": pid, "name": pid, "models": models})
    doc = {"providers": providers, "default": {}}
    settings = _settings(max_response_bytes=64 * 1024 * 1024)
    async with _stack(_ok(_canonical(doc)), settings=settings) as (
            client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 413
    assert r.json() == {"code": "provider_projection_limit",
                        "limit": "projected_body_bytes",
                        "limitValue": MAX_PROJECTED_BODY_BYTES}


async def test_v4_limits_are_frozen_wire_constants():
    assert MAX_PROVIDERS == 256
    assert MAX_MODELS_PER_PROVIDER == 1024
    assert MAX_VARIANTS_PER_MODEL == 64
    assert MAX_PROJECTED_BODY_BYTES == 8_388_608


# --- §12.5 upstream status mapping -----------------------------------------


async def test_v4_upstream_4xx_maps_to_502_upstream_http():
    async with _stack(
            lambda req: _raw(404, b'{"error": "nope"}')) as (
            client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502
    assert r.json() == {"code": "upstream_http_404"}
    assert r.headers["cache-control"] == "no-store"


async def test_v4_upstream_3xx_maps_to_502_upstream_http():
    async with _stack(
            lambda req: _raw(302, b"", content_type="text/plain")) as (
            client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502
    assert r.json() == {"code": "upstream_http_302"}


async def test_v4_upstream_5xx_maps_to_503_unavailable():
    async with _stack(
            lambda req: _raw(500, b"boom")) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 503
    assert r.json() == {"code": "upstream_unavailable"}


async def test_v4_network_error_maps_to_503_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    async with _stack(handler) as (client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 503
    assert r.json() == {"code": "upstream_unavailable"}


# --- §12.5.2 evaluation order ----------------------------------------------


async def test_v4_body_cap_precedes_parse():
    # over-cap body that is ALSO invalid JSON → 413 response_too_large
    # (④ before ⑥), with the §12.5.3 limitBytes field.
    settings = _settings(max_response_bytes=128)
    garbage = b"{" + b"x" * 300
    async with _stack(_ok(garbage), settings=settings) as (
            client, _app, _seen):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 413
    assert r.json() == {"code": "response_too_large", "limitBytes": 128}
    assert r.headers["cache-control"] == "no-store"


async def test_v4_transform_busy_precedes_malformed():
    # Permit held → the ⑤ admission failure wins over the ⑥-⑦ malformed
    # body (the fetch + cap DID run — upstream contacted exactly once).
    async with _stack(_ok(b"{not json")) as (client, app, seen):
        pool = app.state.transforms
        await pool.acquire()
        try:
            r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        finally:
            pool.release()
    assert len(seen) == 1  # network wait happened WITHOUT the permit
    assert r.status_code == 503
    assert r.json()["code"] == "transform_busy"
    assert r.headers["retry-after"] == "2"


async def test_v4_permit_not_needed_for_upstream_errors():
    # Permit held, upstream 404 → 502 upstream_http_404 (NOT 503
    # transform_busy): everything before ⑤ never touches the pool.
    async with _stack(
            lambda req: _raw(404, b"nf")) as (client, app, seen):
        pool = app.state.transforms
        await pool.acquire()
        try:
            r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        finally:
            pool.release()
    assert len(seen) == 1
    assert r.status_code == 502
    assert r.json() == {"code": "upstream_http_404"}


async def test_v4_permit_not_needed_for_body_cap():
    # Permit held, over-cap 200 body → 413 response_too_large (④ before ⑤).
    settings = _settings(max_response_bytes=64)
    async with _stack(_ok(b"z" * 200), settings=settings) as (
            client, app, seen):
        pool = app.state.transforms
        await pool.acquire()
        try:
            r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        finally:
            pool.release()
    assert r.status_code == 413
    assert r.json()["code"] == "response_too_large"


async def test_v4_pipeline_is_one_offloaded_worker_job():
    # ⑥-⑪ = ONE pool.offload(project_and_pack, ...) — assert the offload
    # func is exactly the §12 pure worker job (and only one submit).
    async with _stack(_ok(_canonical(_rich_doc()))) as (client, app, _seen):
        calls: list = []
        orig = app.state.transforms.offload

        async def spy(func, /, *args, **kwargs):
            calls.append(func)
            return await orig(func, *args, **kwargs)

        app.state.transforms.offload = spy
        r = await client.get(f"{ROUTE}?v=4", headers=GZIP_OK)
    assert r.status_code == 200
    assert calls == [project_and_pack]


# --- §12.6 ETag / Vary / 304 ------------------------------------------------


async def test_v4_etag_identity_strong_gzip_weak():
    async with _stack(_ok(_canonical(_rich_doc()))) as (client, _app, _s):
        rid = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        assert rid.headers["etag"].startswith('"')
        assert "content-encoding" not in rid.headers
        rgz = await client.get(f"{ROUTE}?v=4", headers=GZIP_OK)
        assert rgz.headers["etag"].startswith('W/"')
        assert rgz.headers["content-encoding"] == "gzip"
        assert rgz.headers["vary"] == "Accept-Encoding"
        # read RAW wire bytes (httpx auto-decompresses .content) and
        # verify the body is GENUINELY the gzip of the identity bytes.
        async with client.stream("GET", f"{ROUTE}?v=4",
                                 headers=GZIP_OK) as resp:
            assert resp.status_code == 200
            assert resp.headers["Content-Encoding"] == "gzip"
            raw = b""
            async for chunk in resp.aiter_raw():
                raw += chunk
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert gzip.decompress(raw) == rid.content


async def test_v4_etag_is_hash_of_served_canonical_bytes():
    # canonical bytes = the actual identity wire body (no reordered copy):
    # the strong ETag must equal compute_etag over r.content with the
    # providers-v4 REP_VERSION — proven transitively by stability +
    # 304 round-trip below, and directly here via project_and_pack.
    async with _stack(_ok(_canonical(_rich_doc()))) as (client, app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        from oc_slimapi.providers_projection import providers_rep_version
        rep = providers_rep_version(app.state.config)
        encoded, headers = project_and_pack(
            _canonical(_rich_doc()), accept_encoding="identity",
            rep_version=rep)
        assert encoded == r.content
        assert headers["ETag"] == r.headers["etag"]


async def test_v4_304_via_if_none_match():
    async with _stack(_ok(_canonical(_rich_doc()))) as (client, _app, _s):
        r1 = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        etag = r1.headers["etag"]
        r2 = await client.get(f"{ROUTE}?v=4", headers={
            **IDENTITY, "If-None-Match": etag})
        # validators are CODING-specific (coding ∈ hash input, §12.6):
        # the gzip view's weak validator is obtained from a gzip 200.
        rgz1 = await client.get(f"{ROUTE}?v=4", headers=GZIP_OK)
        wetag = rgz1.headers["etag"]
        assert wetag.startswith('W/"')
        rgz2 = await client.get(f"{ROUTE}?v=4", headers={
            **GZIP_OK, "If-None-Match": wetag})
        # weak COMPARE: the strong form of the weak tag still matches
        # (W/ is ignored on both sides during comparison).
        rgz3 = await client.get(f"{ROUTE}?v=4", headers={
            **GZIP_OK, "If-None-Match": wetag.removeprefix("W/")})
        rstar = await client.get(f"{ROUTE}?v=4", headers={
            **IDENTITY, "If-None-Match": "*"})
        rmiss = await client.get(f"{ROUTE}?v=4", headers={
            **IDENTITY, "If-None-Match": '"deadbeef"'})
    assert r2.status_code == 304 and r2.content == b""
    assert r2.headers["etag"] == etag
    assert r2.headers["vary"] == "Accept-Encoding"
    assert r2.headers["cache-control"] == "no-store"
    assert rgz2.status_code == 304
    assert rgz2.headers["etag"] == wetag
    assert rgz3.status_code == 304
    assert rstar.status_code == 304
    assert rmiss.status_code == 200


async def test_v4_etag_disabled_still_varies():
    settings = _settings(etag_enabled=False)
    async with _stack(_ok(_canonical(_rich_doc())), settings=settings) as (
            client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
        r304 = await client.get(f"{ROUTE}?v=4", headers={
            **IDENTITY, "If-None-Match": "*"})
    assert r.status_code == 200
    assert "etag" not in r.headers
    assert r.headers["vary"] == "Accept-Encoding"
    assert r304.status_code == 200  # no validator → no 304


# --- selector-less regression (byte-identical passthrough) -------------------


async def test_selector_less_stack_defaults_to_v3_passthrough():
    # (V2b default flip: wire_view_from_scope is constant 4 — the
    # selector-less direct invocation now runs the §12 safe-projection
    # pipeline, same as the ?v=4 face; the frozen v3 byte-identical
    # passthrough face no longer exists.)
    payload = _canonical(_rich_doc())
    handler = _ok(payload)
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    app.state.config = _settings()
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=app.state.config.upstream)
    app.state.schema_degraded = False
    cfg = app.state.config
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=cfg.max_transforms,
        transform_wait_seconds=cfg.transform_wait_seconds,
        max_response_bytes=cfg.max_response_bytes))
    app.include_router(read_groups.router)
    register_error_handlers(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://slimapi") as client:
        r = await client.get(ROUTE, headers=IDENTITY)
    assert r.status_code == 200
    assert r.content == _canonical(_rich_projected())


# --- directory consumption (②) ---------------------------------------------


async def test_v4_directory_forwarded_upstream():
    async with _stack(_ok(_canonical(_rich_doc()))) as (client, _app, seen):
        r = await client.get(f"{ROUTE}?v=4&directory=/w",
                             headers=IDENTITY)
    assert r.status_code == 200
    assert seen[0].headers.get("x-opencode-directory") == "/w"


# --- rev-cgpt P0-4: UTF-8 surrogate defence (§12.5 malformed unification) ---
#
# Python's stdlib JSON decoder materialises escaped lone surrogates
# ("\ud800") as str objects that CANNOT be UTF-8 encoded. Such a string
# in ANY projected/sorted position must surface as 502
# provider_upstream_malformed — never an uncaught UnicodeEncodeError
# (which would leak as an internal 500, violating §12.5.3's
# "decode/schema violations → uniform 502").


def _surrogate_json(doc: dict) -> bytes:
    """Serialise a doc CONTAINING lone-surrogate strings.

    orjson refuses to encode surrogates (JSONEncodeError) — exactly the
    wire hazard under test — so the fixture goes through the stdlib
    encoder, which escapes them (``\\ud800``), matching what a hostile
    upstream would put on the wire.
    """
    return json.dumps(doc, ensure_ascii=True).encode("ascii")


def _one_provider_doc(**mutate) -> dict:
    doc = {
        "providers": [{
            "id": "p0", "name": "P0",
            "models": {"m0": _model("m0", "p0")},
        }],
        "default": {"p0": "m0"},
    }
    doc.update(mutate)
    return doc


async def _expect_malformed(doc: dict):
    async with _stack(_ok(_surrogate_json(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 502, (r.status_code, r.content[:200])
    assert r.json() == {"code": "provider_upstream_malformed"}


async def test_v4_surrogate_provider_id_is_malformed_not_500():
    # surrogate in provider id → _utf8_key sort would raise today.
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "\ud800", "name": "P0", "models": {},
    }]))


async def test_v4_surrogate_model_key_and_id_is_malformed_not_500():
    # map key AND Model.id carry the same surrogate (so the key==id and
    # providerID relational checks pass) — the sort key is the hazard.
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"\ud800": _model("\ud800", "p0")},
    }]))


async def test_v4_surrogate_variant_key_is_malformed_not_500():
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0",
                                variants={"\ud800": {"junk": 1}})},
    }]))


async def test_v4_surrogate_default_key_is_malformed_not_500():
    await _expect_malformed(_one_provider_doc(
        default={"\ud800": "m0"}))


async def test_v4_surrogate_default_value_is_malformed_not_500():
    await _expect_malformed(_one_provider_doc(
        default={"p0": "\udfff"}))


async def test_v4_surrogate_deep_required_fields_are_malformed():
    # provider.name / model.name (deep required strings) and the
    # optional-string status must all be UTF-8 encodable when projected.
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P\ud800", "models": {},
    }]))
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", name="m\udfff")},
    }]))
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0", status="active\ud800")},
    }]))
    await _expect_malformed(_one_provider_doc(providers=[{
        "id": "p0", "name": "P0", "source": "config\ud800",
        "models": {},
    }]))


async def test_v4_valid_multibyte_utf8_sorts_and_projects_unchanged():
    # Regression: legitimate CJK / emoji / latin-1 strings project and
    # sort by UTF-8 BYTE order (§12.2): 'A'(0x41) < 'ß'(0xC3 0x9F) <
    # '中'(0xE4 0xB8 0xAD) < '🚀'(0xF0 0x9F 0x9A 0x80).
    doc = {
        "providers": [
            {"id": "🚀", "name": "rocket 🚀 provider", "models": {
                "中-model": _model("中-model", "🚀"),
            }},
            {"id": "A", "name": "plain", "models": {
                "z": _model("z", "A", variants={"中": {}, "🚀": {}, "a": {}}),
            }},
            {"id": "ß", "name": "sharp-s", "models": {}},
            {"id": "中", "name": "cjk", "models": {}},
        ],
        "default": {"A": "z", "🚀": "中-model"},
    }
    async with _stack(_ok(_canonical(doc))) as (client, _app, _s):
        r = await client.get(f"{ROUTE}?v=4", headers=IDENTITY)
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["providers"]] == ["A", "ß", "中", "🚀"]
    by_id = {p["id"]: p for p in body["providers"]}
    assert [m["id"] for m in by_id["🚀"]["models"]] == ["中-model"]
    assert by_id["A"]["models"][0]["variants"] == ["a", "中", "🚀"]
    assert by_id["🚀"]["name"] == "rocket 🚀 provider"
    assert list(body["default"].keys()) == ["A", "🚀"]  # orjson sorted


async def test_worker_normalizes_unexpected_value_errors(monkeypatch):
    # Defence-in-depth at the ⑥-⑪ job boundary: a serialization-stage
    # ValueError (e.g. orjson refusing a surrogate that somehow slipped
    # past ⑦) must surface as ProviderUpstreamMalformed, never escape
    # the worker as an unclassified exception (route 500).
    import oc_slimapi.providers_projection as pp

    def _boom(value, **_kwargs):
        raise ValueError("synthetic serialization failure")

    # build the body BEFORE patching (the fixture uses orjson.dumps too)
    body = _canonical(_one_provider_doc())
    monkeypatch.setattr(pp.orjson, "dumps", _boom)
    with pytest.raises(pp.ProviderUpstreamMalformed):
        pp.project_and_pack(
            body, accept_encoding=None, rep_version=None)


async def test_worker_limit_exception_passes_through_boundary(monkeypatch):
    # ProviderProjectionLimit is the LEGAL 413 path — the worker
    # boundary normalisation must never swallow it.
    import oc_slimapi.providers_projection as pp

    doc = _one_provider_doc(providers=[{
        "id": "p0", "name": "P0",
        "models": {"m0": _model("m0", "p0",
                                variants={f"v{i:03d}": {}
                                          for i in range(
                                              MAX_VARIANTS_PER_MODEL + 1)})},
    }])
    with pytest.raises(ProviderProjectionLimit):
        pp.project_and_pack(
            _canonical(doc), accept_encoding=None, rep_version=None)
