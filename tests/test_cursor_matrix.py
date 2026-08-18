"""B3a-B3 cursor 模块测试矩阵（v4-contract §11.4：编解码/指纹矩阵/边界/畸形/确定性）。

覆盖面（工单 ~20-25 case）：

- 编解码往返：参数组合 + 极端值（t=0 / t 巨大 / i 含 unicode 与
  ``ses_%_\\`` 特殊字符）；base64url 输出字符集断言。
- 语法非法矩阵：字母表外字符（``+`` ``/`` ``=`` padding）、非法 base64url
  长度、非法 UTF-8、非法 JSON、顶层非对象、缺 t/i/f、f 缺子键/多子键、
  顶层多键、类型错（t 字符串/bool/float、i 非 str、f 子值非 str）。
- 指纹矩阵：archived/parent/search/allowlist 各维变化 → mismatch True；
  等价形态（parent 省略 vs 显式 "all"；archived 省略 vs "omit"；search
  首尾空白 trim 等价；allowlist 顺序无关/空白项/None vs 空集）→ False。
- 确定性：同输入两次 encode 逐字节相同；search_hash / allowlist_rev
  同输入两次相同（§11.6）。
- 边界：raw=None → None；raw="" → None（空串视同缺席）；10KB 合法
  cursor 正常解码（不过度防御）。
"""
from __future__ import annotations

import base64
import json
import re

import pytest

from oc_slimapi.dbaux.cursor import (
    InvalidCursorError,
    allowlist_rev,
    build_fingerprint,
    decode_cursor,
    encode_cursor,
    fingerprint_mismatch,
    normalize_archived,
    normalize_parent,
    search_hash,
)

_HEX16_RE = re.compile(r"[0-9a-f]{16}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


def _fp(
    *,
    archived: str | None = None,
    parent: str | None = None,
    search: str | None = None,
    allowlist: tuple[str, ...] | None = None,
):
    """build_fingerprint 的便捷封装（默认 = 全默认参数请求）。"""
    return build_fingerprint(
        archived=archived, parent=parent, search=search, allowlist=allowlist
    )


def _mint(raw_json: str | bytes) -> str:
    """绕过 encode_cursor 直接铸造 cursor（构造畸形样本）。"""
    blob = raw_json.encode("utf-8") if isinstance(raw_json, str) else raw_json
    return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


_VALID_F = '{"archived":"omit","parent":"all","search_hash":"","allowlist_rev":""}'


# ---------------------------------------------------------------------------
# 编解码往返
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "i"),
    [
        (0, "ses_1"),
        (1755000000000, "ses_normal"),
        (10**18, "ses_huge_t"),  # 极端 t
        (1755000000000, "ses_%_\\_like_specials"),  # LIKE 通配/转义字符
        (1755000000000, "ses_ünïcödé_会话"),  # unicode（ensure_ascii 转义后仍是 ASCII cursor）
        (1755000000000, ""),  # 空 id（形态合法即解码——业务校验归 B4）
    ],
)
def test_roundtrip_param_combinations(t: int, i: str) -> None:
    fp = _fp(archived="only", parent="ses_parent", search="标题子串", allowlist=("/a", "/b"))
    decoded = decode_cursor(encode_cursor(t, i, fp))
    assert decoded is not None
    assert decoded.t == t
    assert decoded.i == i
    assert decoded.f == fp


def test_encoded_output_is_base64url_charset() -> None:
    """输出仅 [A-Za-z0-9_-]，无 padding、无 +/、纯 ASCII。"""
    raw = encode_cursor(
        1755000000000, "ses_ünïcödé_%_\\", _fp(search="空白 検索", allowlist=("/a/b", "/c"))
    )
    assert _B64URL_RE.fullmatch(raw) is not None
    assert "=" not in raw and "+" not in raw and "/" not in raw
    raw.encode("ascii")  # 不抛即纯 ASCII


def test_encode_deterministic_byte_identical() -> None:
    """同一输入两次 encode 逐字节相同（§4.5 确定性）。"""
    fp = _fp(search="x", allowlist=("/a",))
    assert encode_cursor(5, "ses_a", fp) == encode_cursor(5, "ses_a", fp)


def test_cursor_json_shape() -> None:
    """JSON 顶层恰 {t,i,f} 且 f 恰四子键（wire 形状锚定）。"""
    raw = encode_cursor(7, "ses_x", _fp())
    doc = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert set(doc) == {"t", "i", "f"}
    assert set(doc["f"]) == {"archived", "parent", "search_hash", "allowlist_rev"}


# ---------------------------------------------------------------------------
# 语法非法矩阵 → InvalidCursorError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "a+b/cd==",  # 标准 base64 字符 + padding
        "ab+cd",  # 仅 +
        "ab/cd",  # 仅 /
        "abcd=",  # 仅 padding =
        "ab cd",  # 空白
        "ab.cd",  # 其他字母表外字符
        "abcd€",  # 非 ASCII
    ],
)
def test_invalid_charset_raises(raw: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(raw)


def test_invalid_base64url_length_raises() -> None:
    # len % 4 == 1（如 "abcde"）非法 base64 长度。
    with pytest.raises(InvalidCursorError):
        decode_cursor("abcde")


def test_invalid_utf8_raises() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(b"\xff\xfe\xfd"))


def test_invalid_json_raises() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint("{not json"))


@pytest.mark.parametrize("doc", ['[1, 2]', '"str"', "42", "null", "true"])
def test_non_object_json_raises(doc: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(doc))


@pytest.mark.parametrize(
    "doc",
    [
        f'{{"i": "ses_x", "f": {_VALID_F}}}',  # 缺 t
        f'{{"t": 1, "f": {_VALID_F}}}',  # 缺 i
        '{"t": 1, "i": "ses_x"}',  # 缺 f
        f'{{"t": 1, "i": "ses_x", "f": {_VALID_F}, "extra": 1}}',  # 顶层多键
    ],
)
def test_missing_or_extra_top_keys_raises(doc: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(doc))


@pytest.mark.parametrize(
    "f_json",
    [
        '{"parent": "all", "search_hash": "", "allowlist_rev": ""}',  # f 缺 archived
        '{"archived": "omit", "search_hash": "", "allowlist_rev": ""}',  # f 缺 parent
        '{"archived": "omit", "parent": "all", "allowlist_rev": ""}',  # f 缺 search_hash
        '{"archived": "omit", "parent": "all", "search_hash": ""}',  # f 缺 allowlist_rev
        # f 多子键
        '{"archived": "omit", "parent": "all", "search_hash": "", "allowlist_rev": "", "x": "y"}',
        '"not-a-dict"',  # f 非 dict
    ],
)
def test_fingerprint_subkey_shape_raises(f_json: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(f'{{"t": 1, "i": "ses_x", "f": {f_json}}}'))


@pytest.mark.parametrize(
    "t_json",
    ['"1755000000000"', "true", "1.5", "null", "[1]"],
)
def test_wrong_t_type_raises(t_json: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(f'{{"t": {t_json}, "i": "ses_x", "f": {_VALID_F}}}'))


def test_wrong_i_and_f_value_types_raise() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(_mint(f'{{"t": 1, "i": 42, "f": {_VALID_F}}}'))
    with pytest.raises(InvalidCursorError):
        decode_cursor(
            _mint(
                '{"t": 1, "i": "ses_x",'
                ' "f": {"archived": 1, "parent": "all", "search_hash": "", "allowlist_rev": ""}}'
            )
        )


# ---------------------------------------------------------------------------
# 指纹矩阵
# ---------------------------------------------------------------------------


def test_fingerprint_identical_no_mismatch() -> None:
    assert fingerprint_mismatch(_fp(), dict(_fp())) is False
    assert fingerprint_mismatch(_fp(archived="all", parent="none", search="q", allowlist=("/a",)), dict(_fp(archived="all", parent="none", search="q", allowlist=("/a",)))) is False


def test_archived_change_mismatches() -> None:
    assert fingerprint_mismatch(_fp(archived="only"), dict(_fp())) is True


def test_parent_change_mismatches() -> None:
    assert fingerprint_mismatch(_fp(parent="none"), dict(_fp())) is True
    assert fingerprint_mismatch(_fp(parent="ses_123"), dict(_fp(parent="only"))) is True


def test_parent_omitted_equals_explicit_all() -> None:
    """省略 vs 显式 "all" 归一化等价，不误判（§4.1 冻结）。"""
    assert _fp(parent=None) == _fp(parent="all")
    assert fingerprint_mismatch(_fp(parent=None), dict(_fp(parent="all"))) is False


def test_archived_omitted_equals_explicit_omit() -> None:
    assert _fp(archived=None) == _fp(archived="omit")
    assert fingerprint_mismatch(_fp(archived=None), dict(_fp(archived="omit"))) is False


def test_search_change_mismatches() -> None:
    assert fingerprint_mismatch(_fp(search="foo"), dict(_fp())) is True
    assert fingerprint_mismatch(_fp(search="foo"), dict(_fp(search="bar"))) is True


def test_search_trim_equivalence() -> None:
    """hash 输入 = trim 后的串：" foo " 与 "foo" 同指纹（§4.5）。"""
    assert _fp(search=" foo ") == _fp(search="foo")
    assert fingerprint_mismatch(_fp(search="  foo\t"), dict(_fp(search="foo"))) is False


def test_search_absent_vs_explicit_empty_distinct() -> None:
    """None（哨兵 ""）≠ 显式空串（sha256("")）——设计决策：缺席与显式空是不同请求形态。"""
    assert fingerprint_mismatch(_fp(search=""), dict(_fp())) is True


def test_allowlist_set_change_mismatches() -> None:
    assert fingerprint_mismatch(_fp(allowlist=("/a",)), dict(_fp())) is True
    assert fingerprint_mismatch(_fp(allowlist=("/a",)), dict(_fp(allowlist=("/a", "/b")))) is True


def test_allowlist_order_independent_and_noise_normalized() -> None:
    """{a,b} 与 {b,a} 同 rev；空白项/空白包裹项为配置噪声（strip+去空+去重）。"""
    assert _fp(allowlist=("/a", "/b")) == _fp(allowlist=("/b", "/a"))
    assert _fp(allowlist=("/a", "/b", "/a")) == _fp(allowlist=("/a", "/b"))
    assert _fp(allowlist=("/a ", " /b", "")) == _fp(allowlist=("/b", "/a"))
    assert fingerprint_mismatch(_fp(allowlist=("/a", "/b")), dict(_fp(allowlist=("/b", "/a")))) is False


def test_allowlist_empty_and_none_equivalent() -> None:
    """None / () / 全空项 → 同一哨兵（机制未启用）。"""
    assert _fp(allowlist=None) == _fp(allowlist=())
    assert _fp(allowlist=None) == _fp(allowlist=("", "  "))
    assert fingerprint_mismatch(_fp(allowlist=None), dict(_fp(allowlist=()))) is False


def test_fingerprint_mismatch_malformed_inputs() -> None:
    """畸形输入（非 Mapping）按不匹配处理（fail-closed → 400）。"""
    assert fingerprint_mismatch(None, dict(_fp())) is True
    assert fingerprint_mismatch(dict(_fp()), "not-a-dict") is True


# ---------------------------------------------------------------------------
# 确定性（§11.6：同输入两次执行 hash 相同）
# ---------------------------------------------------------------------------


def test_search_hash_deterministic_and_hex16() -> None:
    a, b = search_hash("some normalized search"), search_hash("some normalized search")
    assert a == b
    assert _HEX16_RE.fullmatch(a) is not None
    assert search_hash("foo") != search_hash("bar")


def test_search_hash_none_sentinel() -> None:
    assert search_hash(None) == ""
    assert search_hash(None) == search_hash(None)


def test_allowlist_rev_deterministic_and_hex16() -> None:
    a, b = allowlist_rev(("/a", "/b")), allowlist_rev(("/b", "/a"))
    assert a == b
    assert _HEX16_RE.fullmatch(a) is not None
    assert allowlist_rev(("/a",)) != allowlist_rev(("/a", "/b"))


def test_allowlist_rev_empty_sentinel() -> None:
    assert allowlist_rev(None) == ""
    assert allowlist_rev(()) == ""
    assert allowlist_rev(("", "  ")) == ""


def test_normalize_defaults() -> None:
    assert normalize_archived(None) == "omit"
    assert normalize_archived("") == "omit"
    assert normalize_archived("only") == "only"
    assert normalize_parent(None) == "all"
    assert normalize_parent("") == "all"
    assert normalize_parent("ses_x") == "ses_x"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_absent_cursor_decodes_none() -> None:
    assert decode_cursor(None) is None


def test_empty_string_cursor_treated_as_absent() -> None:
    """raw="" 视同缺席（设计决策：与 query 参数空串观测形态一致）。"""
    assert decode_cursor("") is None


def test_10kb_valid_cursor_decodes() -> None:
    """超长但结构合法的 cursor 正常解码（不过度防御）。"""
    i = "x" * 10_000
    decoded = decode_cursor(encode_cursor(1755000000000, i, _fp()))
    assert decoded is not None
    assert decoded.i == i
