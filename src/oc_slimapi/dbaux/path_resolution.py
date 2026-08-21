"""DB 路径解析（design-v4-dbaux §3，B0-6(c) / v2.2 行 98）。

解析优先级（§3.1 冻结，rev-1 fail-closed 修订）：

1. ``OC_SLIMAPI_OPENCODE_DB`` 显式配置（生产推荐，最高优先）；
   ``":memory:"`` → 禁用辅助（reason=explicit-memory）；
2. ``OPENCODE_DB`` 上游 env（可观测、无歧义）：``":memory:"`` → 禁用；
   绝对路径（含 ``~`` 前缀）→ 直接用；相对路径 → 挂数据目录；
3. channel 候选发现（不猜测，R3 冻结）：恰一个 ``opencode*.db`` →
   采用 + warning；多候选/零候选 → 禁用（path_ambiguous | not_found）。

数据目录 = ``XDG_DATA_HOME``（或 ``~/.local/share``）+ ``/opencode``
（上游 ``global.ts:10-11`` 复刻）。解析结果（source/reason/warning）由
调用方进启动 log（§1.4）。

本模块为纯函数：不打开连接、不做 IO 副作用（glob/stat 属读取性探测，
是路径「解析」的一部分）。
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

# §3.1: sidecar 自有显式配置 env（最高优先）。
ENV_EXPLICIT_DB = "OC_SLIMAPI_OPENCODE_DB"
# §3.2: 上游 flag.ts:47 — OPENCODE_DB = process.env["OPENCODE_DB"]。
ENV_UPSTREAM_DB = "OPENCODE_DB"
# §3.2: XDG 数据目录 env。
ENV_XDG_DATA_HOME = "XDG_DATA_HOME"

_MEMORY = ":memory:"
_CANDIDATE_GLOB = "opencode*.db"

# 候选发现 warning（§3.3 伪代码原文措辞——启动 log / 测试断言锚点）。
SINGLE_CANDIDATE_WARNING = "channel 未观测（编译期常量），单候选采用"


@dataclass(frozen=True)
class ResolvedPath:
    """解析成功：打开这个文件（mode=ro）。"""

    path: str
    # "explicit-env" | "upstream-env" | "upstream-env-relative" | "candidate-discovery"
    source: str
    warning: str | None = None


@dataclass(frozen=True)
class DisabledResolution:
    """解析失败（fail-closed）：禁用辅助源，全降级 HTTP（§7）。

    reason ∈ explicit-memory | upstream-memory | path_ambiguous | not_found。
    """

    reason: str
    detail: Any = None


def _clean(raw: str) -> str:
    """空白 trim（§3.3 要点：strip）。"""
    return raw.strip()


def _expanduser(p: str, home: str | None) -> str:
    if p.startswith("~") and home is not None:
        return p.replace("~", home, 1)
    if p.startswith("~"):
        return os.path.expanduser(p)
    return p


def _data_dir(env: Mapping[str, str], home: str | None) -> str:
    """global.ts:10-11 复刻：XDG_DATA_HOME 或 ~/.local/share + "/opencode"。"""
    xdg = _clean(env.get(ENV_XDG_DATA_HOME, "") or "")
    base = xdg if xdg else "~/.local/share"
    return os.path.join(_expanduser(base, home), "opencode")


def resolve_db_path(
    *,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
) -> ResolvedPath | DisabledResolution:
    """§3.3 解析伪代码定稿的直接落地。

    ``env``/``home`` 可注入（测试隔离）；缺省读进程环境。返回值进启动
    log（§1.4）；Disabled 不打开连接（§1.4-3）。
    """
    env = os.environ if env is None else dict(env)

    # 1. sidecar 显式配置（生产推荐，R3 后最高优先）。
    explicit = _clean(env.get(ENV_EXPLICIT_DB, "") or "")
    if explicit:
        if explicit == _MEMORY:
            return DisabledResolution(reason="explicit-memory")
        p = _expanduser(explicit, home)
        return ResolvedPath(path=os.path.normpath(p), source="explicit-env")

    data_dir = _data_dir(env, home)

    # 2. 上游 OPENCODE_DB env（可观测，无 channel 猜测）。
    upstream = _clean(env.get(ENV_UPSTREAM_DB, "") or "")
    if upstream:
        if upstream == _MEMORY:
            return DisabledResolution(reason="upstream-memory")
        # isAbsolute 复刻（POSIX 语义）+ ``~`` 前缀展开（§3.3 伪代码）。
        if os.path.isabs(upstream) or upstream.startswith("~"):
            return ResolvedPath(
                path=os.path.normpath(_expanduser(upstream, home)),
                source="upstream-env",
            )
        # 相对路径 → 挂数据目录（database.ts:44-46 复刻）。
        return ResolvedPath(
            path=os.path.normpath(os.path.join(data_dir, upstream)),
            source="upstream-env-relative",
        )

    # 3. channel 候选发现（fail-closed，R3 冻结）。OPENCODE_DISABLE_CHANNEL_DB
    #    情形由候选枚举自然覆盖——该开关只影响上游建库名，sidecar 按盘上
    #    文件事实判定（§3.3 注）。
    candidates = sorted(glob.glob(os.path.join(glob.escape(data_dir), _CANDIDATE_GLOB)))
    if len(candidates) == 1:
        return ResolvedPath(
            path=os.path.normpath(candidates[0]),
            source="candidate-discovery",
            warning=SINGLE_CANDIDATE_WARNING,
        )
    if len(candidates) > 1:
        return DisabledResolution(reason="path_ambiguous", detail=candidates)
    return DisabledResolution(reason="not_found", detail=data_dir)


def stat_inode_marker(path: str) -> tuple[int, int] | None:
    """§4.1 inode/mtime 标记：``(st_ino, st_mtime_ns)``；stat 失败 → None。

    ``-wal``/``-shm`` 不参与（由主文件 inode 变化代表换库）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


__all__ = [
    "ENV_EXPLICIT_DB",
    "ENV_UPSTREAM_DB",
    "ENV_XDG_DATA_HOME",
    "SINGLE_CANDIDATE_WARNING",
    "DisabledResolution",
    "ResolvedPath",
    "resolve_db_path",
    "stat_inode_marker",
]
