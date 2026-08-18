"""B3a-B1 — DB 路径解析（design-v4-dbaux §3.3 伪代码 / §3.4 用例表）。

11 case 逐条对应 §3.4 表；env/home 全注入（tmp/monkeypatch 隔离，CI 无
``~/.local/share/opencode`` 真库依赖）。
"""
from __future__ import annotations

from pathlib import Path

from oc_slimapi.dbaux.path_resolution import (
    SINGLE_CANDIDATE_WARNING,
    DisabledResolution,
    ResolvedPath,
    resolve_db_path,
    stat_inode_marker,
)


def _env(**kw) -> dict[str, str]:
    base = {"HOME": "/home/tester"}
    base.update(kw)
    return base


def test_case1_explicit_env_wins_over_everything(tmp_path: Path):
    data = tmp_path / "share" / "opencode"
    data.mkdir(parents=True)
    (data / "opencode.db").touch()
    (data / "opencode-local.db").touch()
    res = resolve_db_path(
        env=_env(
            OC_SLIMAPI_OPENCODE_DB="/x/y.db",
            OPENCODE_DB="/z.db",
            XDG_DATA_HOME=str(tmp_path / "share"),
        ),
    )
    assert isinstance(res, ResolvedPath)
    assert res.path == "/x/y.db"
    assert res.source == "explicit-env"
    assert res.warning is None


def test_case2_upstream_env_absolute(tmp_path: Path):
    res = resolve_db_path(env=_env(OPENCODE_DB="/z.db"))
    assert isinstance(res, ResolvedPath)
    assert res.path == "/z.db"
    assert res.source == "upstream-env"


def test_case3_upstream_env_relative(tmp_path: Path):
    res = resolve_db_path(env=_env(OPENCODE_DB="rel/db.db", XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, ResolvedPath)
    expected = str(tmp_path / "opencode" / "rel" / "db.db")
    assert res.path == expected
    assert res.source == "upstream-env-relative"


def test_case4_single_candidate_discovery_with_channel_name(tmp_path: Path):
    data = tmp_path / "opencode"
    data.mkdir(parents=True)
    (data / "opencode-local.db").touch()
    res = resolve_db_path(env=_env(XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, ResolvedPath)
    assert res.path == str(data / "opencode-local.db")
    assert res.source == "candidate-discovery"
    assert res.warning == SINGLE_CANDIDATE_WARNING


def test_case4b_single_candidate_plain_name(tmp_path: Path):
    data = tmp_path / "opencode"
    data.mkdir(parents=True)
    (data / "opencode.db").touch()
    res = resolve_db_path(env=_env(XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, ResolvedPath)
    assert res.path == str(data / "opencode.db")


def test_case5_multiple_candidates_fail_closed(tmp_path: Path):
    data = tmp_path / "opencode"
    data.mkdir(parents=True)
    (data / "opencode.db").touch()
    (data / "opencode-local.db").touch()
    res = resolve_db_path(env=_env(XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, DisabledResolution)
    assert res.reason == "path_ambiguous"
    assert sorted(res.detail) == [
        str(data / "opencode-local.db"),
        str(data / "opencode.db"),
    ]


def test_case6_zero_candidates_fail_closed(tmp_path: Path):
    # 目录存在但无 opencode*.db / 目录不存在 — 两形态同归 not_found。
    empty = tmp_path / "opencode"
    empty.mkdir(parents=True)
    res = resolve_db_path(env=_env(XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, DisabledResolution)
    assert res.reason == "not_found"
    missing = tmp_path / "does-not-exist"
    res2 = resolve_db_path(env=_env(XDG_DATA_HOME=str(missing)))
    assert isinstance(res2, DisabledResolution)
    assert res2.reason == "not_found"


def test_case7_memory_disables_both_envs(tmp_path: Path):
    res = resolve_db_path(
        env=_env(OC_SLIMAPI_OPENCODE_DB=":memory:", OPENCODE_DB="/z.db")
    )
    assert isinstance(res, DisabledResolution)
    assert res.reason == "explicit-memory"
    res2 = resolve_db_path(env=_env(OPENCODE_DB=":memory:"))
    assert isinstance(res2, DisabledResolution)
    assert res2.reason == "upstream-memory"


def test_case8_relative_path_normalized(tmp_path: Path):
    res = resolve_db_path(env=_env(OPENCODE_DB="./a.db", XDG_DATA_HOME=str(tmp_path)))
    assert isinstance(res, ResolvedPath)
    assert res.path == str(tmp_path / "opencode" / "a.db")
    assert "/./" not in res.path and not res.path.endswith("/.")


def test_case9_tilde_expansion(tmp_path: Path):
    home = tmp_path / "home"
    res = resolve_db_path(env=_env(OC_SLIMAPI_OPENCODE_DB="~/db.db"), home=str(home))
    assert isinstance(res, ResolvedPath)
    assert res.path == str(home / "db.db")


def test_case10_trailing_slash_and_whitespace(tmp_path: Path):
    res = resolve_db_path(env=_env(OC_SLIMAPI_OPENCODE_DB="/x/y/"))
    assert isinstance(res, ResolvedPath)
    assert res.path == "/x/y"  # normpath 尾斜杠归一
    res2 = resolve_db_path(env=_env(OC_SLIMAPI_OPENCODE_DB=" /x/y.db "))
    assert isinstance(res2, ResolvedPath)
    assert res2.path == "/x/y.db"  # strip


def test_case11_both_envs_conflict_explicit_wins_silently(tmp_path: Path):
    res = resolve_db_path(
        env=_env(OC_SLIMAPI_OPENCODE_DB="/x/y.db", OPENCODE_DB="/z.db")
    )
    assert isinstance(res, ResolvedPath)
    assert res.path == "/x/y.db"
    assert res.source == "explicit-env"  # 优先级冻结：不告警不合并


def test_xdg_default_falls_back_to_home_share(tmp_path: Path):
    """XDG_DATA_HOME 未设置 → ``~/.local/share/opencode``（global.ts 复刻）。"""
    data = tmp_path / "home" / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    (data / "opencode.db").touch()
    res = resolve_db_path(env=_env(), home=str(tmp_path / "home"))
    assert isinstance(res, ResolvedPath)
    assert res.path == str(data / "opencode.db")
    assert res.source == "candidate-discovery"


def test_stat_inode_marker(tmp_path: Path):
    p = tmp_path / "db.sqlite"
    p.write_bytes(b"a")
    m1 = stat_inode_marker(str(p))
    assert m1 is not None and len(m1) == 2
    p.write_bytes(b"bb")  # 内容变化（同 inode 或 mtime 变化）
    m2 = stat_inode_marker(str(p))
    assert m2 is not None
    # tmp 替换 → inode 变化
    p.unlink()
    p.write_bytes(b"c")
    m3 = stat_inode_marker(str(p))
    assert m3 is not None and m3[0] != m1[0]
    assert stat_inode_marker(str(tmp_path / "missing.db")) is None
