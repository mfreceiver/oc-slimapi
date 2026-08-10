from types import SimpleNamespace

import oc_slimapi.app as app_mod


def test_main_passes_graceful_shutdown_timeout(monkeypatch):
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    # 用 SimpleNamespace 整体替换模块级 frozen settings：避免 monkeypatch
    # frozen dataclass 的 validate 属性在构造后不可靠（frozen 实例禁止赋值）。
    fake_settings = SimpleNamespace(host="127.0.0.1", port=4097, validate=lambda: None)
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(app_mod.uvicorn, "run", fake_run)
    app_mod.main()
    assert captured["kwargs"]["timeout_graceful_shutdown"] == 5.0
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 4097
