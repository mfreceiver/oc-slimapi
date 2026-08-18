"""v4 sessions keyset 翻页 cursor（v4-contract §4.5；design-v4-dbaux §9.1/§9.3）。

cursor = base64url(JSON ``{"t": <time_updated>, "i": <id>,
"f": {archived, parent, search-hash, allowlist-rev}}``)（无 padding）——
复合键 + 过滤上下文指纹。契约原文（§4.5）：

- 承诺：确定性排序（§4.1 冻结 ``(time_updated DESC, id DESC)``）；
  **不承诺**并发更新零重复零遗漏（跨边界重见为预期行为，契约明示）。
- 指纹不匹配当前请求参数 → 400 ``invalid_cursor``（提示重开首屏）。
- 语法校验纯内存、与 DB 状态无关 → **优先于 503**（§8.3：
  「malformed cursor vs auxiliary unavailable → 400 优先（语法校验先于
  降级判定）」；调用方 B4 负责错误码映射）。

指纹语义：

- ``search_hash``：输入 = ``normalized_search``（**trim 后、LIKE 转义前**，
  §4.5 冻结——四个消费点的唯一输入源）；sha256 截断 16 hex；
  ``None``（参数缺席）→ 固定哨兵 ``""``。``None`` 与显式空串 **不等价**
  （哨兵 ≠ sha256("")）——缺席与「显式空 search」是不同请求形态。
- ``allowlist_rev``：对**规范化后（strip、去空项）排序去重**的 allowlist
  项列表做 sha256 截断 16 hex（同一集合两次计算相同；顺序无关；
  集合变化 → 不同）；空 allowlist（``None`` 或全空项）→ 哨兵 ``""``
  （机制未启用，无该谓词）。
- 哨兵 ``""`` 不可能出现在 16-hex 摘要输出中，无碰撞风险。
- ``parent`` 省略 = ``"all"``、``archived`` 省略 = ``"omit"``（§4.1 冻结）
  ——指纹存归一化后的值，故「省略」与「显式默认值」不误判为不匹配。

本模块为纯函数：不做 IO、不触 DB；encode/decode 皆确定性（同一输入
两次 encode 逐字节相同——JSON 键序固定 t/i/f、compact 分隔符、
ensure_ascii）。
"""
from __future__ import annotations

import hashlib
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as _B64DecodeError
from dataclasses import dataclass
from typing import Iterable, Mapping, TypedDict

# §4.1 冻结：参数省略时的默认值（进指纹的归一化形态）。
ARCHIVED_DEFAULT = "omit"
PARENT_DEFAULT = "all"

# §4.5：sha256 截断 16 hex（契约允许 8-16，取上限强化抗碰撞性）。
_HASH_HEX_LEN = 16
# 空 search / 空 allowlist 的固定哨兵（见模块 docstring）。
_EMPTY_SENTINEL = ""

# 标准 base64url 字母表（RFC 4648 §5），无 padding：预检必做——
# urlsafe_b64decode(validate=False) 会静默丢弃非字母表字符。
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_KEYS = frozenset({"t", "i", "f"})
_FINGERPRINT_KEYS = frozenset({"archived", "parent", "search_hash", "allowlist_rev"})


class InvalidCursorError(ValueError):
    """cursor 语法非法（§4.3 → 400 ``invalid_cursor``；优先于 503，§8.3）。

    ``reason`` 为粗粒度诊断标签（charset/decode/json/shape/type/
    empty_anchor），进日志不进 wire 错误体（§4.2：错误体不泄露内部细节）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid cursor: {reason}")
        self.reason = reason


class CursorFingerprint(TypedDict):
    """过滤上下文指纹（§4.5）：归一化后的请求参数摘要。"""

    archived: str
    parent: str
    search_hash: str
    allowlist_rev: str


@dataclass(frozen=True)
class CursorPayload:
    """解码成功的 cursor：keyset 复合键 (t, i) + 指纹 f。"""

    t: int
    i: str
    f: CursorFingerprint


def search_hash(normalized_search: str | None) -> str:
    """§4.5：sha256(normalized_search) 截断 16 hex；``None`` → 哨兵 ``""``。

    输入 = trim 后、LIKE 转义**前**的串（调用方保证已 trim——本函数
    不再二次归一化，保证「唯一输入源」单一职责）；同一输入两次执行
    必相同（§11.6 确定性断言）。
    """
    if normalized_search is None:
        return _EMPTY_SENTINEL
    return hashlib.sha256(normalized_search.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]


def allowlist_rev(entries: Iterable[str] | None) -> str:
    """§4.5：非空 allowlist 集合修订版本（同一集合相同；集合变化 → 不同）。

    规范化：逐项 strip、去空项 → 排序去重 → canonical JSON（定界符
    注入免疫：项内容含 ``\\n``/``,``
    也不会碰撞）→ sha256 截断 16 hex。
    ``None`` / 全空项 → 哨兵 ``""``（机制未启用）。空串项被丢弃：
    空路径不可能构成有效子树谓词，与其余项并存时视为配置噪声。
    """
    normalized = {e.strip() for e in (entries or ()) if e and e.strip()}
    if not normalized:
        return _EMPTY_SENTINEL
    canonical = json.dumps(sorted(normalized), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]


def normalize_archived(raw: str | None) -> str:
    """§4.1：archived 省略（``None``/空串）= ``"omit"``；其余原样（枚举校验归 B4/FastAPI）。"""
    return ARCHIVED_DEFAULT if raw is None or raw == "" else raw


def normalize_parent(raw: str | None) -> str:
    """§4.1：parent 省略（``None``/空串）= ``"all"``；其余原样（"all"/"none"/"only"/"<sid>"）。"""
    return PARENT_DEFAULT if raw is None or raw == "" else raw


def build_fingerprint(
    *,
    archived: str | None,
    parent: str | None,
    search: str | None,
    allowlist: Iterable[str] | None,
) -> CursorFingerprint:
    """从请求原始参数组装指纹（归一化集中地——B4 编码与比对共用同一入口）。

    ``search`` 为 **raw** 值：此处施加 §4.5 唯一规范化 ``trim(raw)``
    后再 hash（``?search=`` 显式空串 → trim 后空串 → sha256("")，
    非 None 哨兵——缺席与显式空不等价，见模块 docstring）。
    """
    trimmed = None if search is None else search.strip()
    return CursorFingerprint(
        archived=normalize_archived(archived),
        parent=normalize_parent(parent),
        search_hash=search_hash(trimmed),
        allowlist_rev=allowlist_rev(allowlist),
    )


def fingerprint_mismatch(
    payload_f: Mapping[str, str] | None,
    current_f: Mapping[str, str] | None,
) -> bool:
    """指纹比对：任一差异（含键集差异/畸形输入）→ True → 调用方 400。"""
    if not isinstance(payload_f, Mapping) or not isinstance(current_f, Mapping):
        return True
    return dict(payload_f) != dict(current_f)


def encode_cursor(t: int, i: str, fingerprint: CursorFingerprint) -> str:
    """编码：base64url(JSON) 无 padding；确定性（键序固定、compact、ensure_ascii）。"""
    payload = {"t": t, "i": i, "f": dict(fingerprint)}
    blob = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


def decode_cursor(raw: str | None) -> CursorPayload | None:
    """解码 + 语法校验（纯内存，§8.3 优先于 503）。

    - ``None`` / ``""`` → ``None``（cursor 缺席——空串视同缺席，
      与其余 query 参数的 FastAPI 观测形态一致）；
    - 字母表外字符（含标准 base64 的 ``+`` ``/`` 与 padding ``=``）、
      非法 base64url 长度、非法 UTF-8、非法 JSON、顶层非对象、键集
      非 ``{t,i,f}``、f 子键集非全量四键、类型错（t 非 int、bool 亦拒）、
      ``i`` 为空串（keyset 锚点必须有可比对的行 id——rev gate BLOCKER-2：
      空锚点曾在 SQL 构造层才被拒，DB 可用时逃逸为 500；解码层是 wire
      输入的第一道门，拒绝属语法域，非业务值域）→
      :class:`InvalidCursorError`；
    - 其余结构与类型合法即成功（**不过度防御**：超长但合法的 cursor、
      含控制字符的 ``i`` 正常解码——它们是合法的行 id 形态；业务值域
      校验归 B4）。
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or _B64URL_RE.fullmatch(raw) is None:
        raise InvalidCursorError("charset")
    try:
        blob = urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (_B64DecodeError, ValueError):
        raise InvalidCursorError("decode") from None
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidCursorError("json") from None
    if not isinstance(doc, dict) or set(doc) != _CURSOR_KEYS:
        raise InvalidCursorError("shape")
    f = doc["f"]
    if not isinstance(f, dict) or set(f) != _FINGERPRINT_KEYS:
        raise InvalidCursorError("shape")
    # bool 是 int 子类：JSON true → Python True，显式拒绝。
    if isinstance(doc["t"], bool) or not isinstance(doc["t"], int):
        raise InvalidCursorError("type")
    if not isinstance(doc["i"], str) or not all(isinstance(v, str) for v in f.values()):
        raise InvalidCursorError("type")
    if doc["i"] == "":
        # BLOCKER-2：空 i = 空 keyset 锚点。SQL 构造层不补救 wire 输入
        # （build_sessions_query 对空锚点 fail-fast），此处解码层统一拒绝
        # → 400 invalid_cursor（优先于 503）。
        raise InvalidCursorError("empty_anchor")
    return CursorPayload(t=doc["t"], i=doc["i"], f=CursorFingerprint(**f))


__all__ = [
    "ARCHIVED_DEFAULT",
    "PARENT_DEFAULT",
    "CursorFingerprint",
    "CursorPayload",
    "InvalidCursorError",
    "allowlist_rev",
    "build_fingerprint",
    "decode_cursor",
    "encode_cursor",
    "fingerprint_mismatch",
    "normalize_archived",
    "normalize_parent",
    "search_hash",
]
