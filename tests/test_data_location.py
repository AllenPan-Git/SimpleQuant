"""数据保存位置：选位置、复制、路标、启动时读路标、清理旧位置"""

import json

import pytest

from simplequant import data_location as loc, paths


def _fake_data(root, files=None):
    files = files or {"data_cache/a.json": "1", "data_cache/paper/x/account.json": "{}",
                      "user_strategies/s.json": "2", "llm.json": "{}", "logs/app.log": "log"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    default = tmp_path / "local" / "SimpleQuant"
    _fake_data(default)
    monkeypatch.setattr(loc, "user_data_dir", lambda: default)
    monkeypatch.setattr(paths, "DATA_ROOT", default)
    monkeypatch.setattr(paths, "POINTER", default / "location.json")
    return default


def test_plan_adds_subfolder(env, tmp_path):
    d = tmp_path / "D"
    (d / "别的文件.txt").parent.mkdir(parents=True)
    (d / "别的文件.txt").write_text("x", encoding="utf-8")
    p = loc.plan(str(d))
    assert p.target == d / "SimpleQuant" and not p.existing and p.need > 0


def test_plan_uses_empty_simplequant_folder_and_existing_data(env, tmp_path):
    assert loc.plan(tmp_path / "E" / "SimpleQuant").target == tmp_path / "E" / "SimpleQuant"
    old = _fake_data(tmp_path / "old")
    p = loc.plan(old)
    assert p.target == old and p.existing and p.need == 0


@pytest.mark.parametrize("picked, code", [("", "empty"), ("relative/dir", "relative")])
def test_plan_rejects_bad_input(env, picked, code):
    with pytest.raises(loc.LocationError) as e:
        loc.plan(picked)
    assert e.value.code == code


def test_plan_rejects_same_and_nested(env):
    with pytest.raises(loc.LocationError) as e:
        loc.plan(env)
    assert e.value.code == "same"
    with pytest.raises(loc.LocationError) as e:
        loc.plan(env / "data_cache" / "sub")
    assert e.value.code == "nested"


def test_plan_checks_free_space(env, tmp_path, monkeypatch):
    monkeypatch.setattr(loc, "free_bytes", lambda p: 10)
    with pytest.raises(loc.LocationError) as e:
        loc.plan(tmp_path / "D")
    assert e.value.code == "space" and e.value.kw["free"] == 10


def test_move_round_trip(env, tmp_path, monkeypatch):
    """复制 → 写路标 → 下次启动读到新位置 → 清理旧位置（只删 SimpleQuant 的条目，路标留下）"""
    (env / "别人的.txt").write_text("keep", encoding="utf-8")
    target = loc.plan(tmp_path / "D").target
    seen = []
    n = loc.copy_data(target, progress=lambda f, i, k: seen.append(f))
    assert n == 5 and seen[-1] == pytest.approx(1.0)
    assert (target / "data_cache" / "paper" / "x" / "account.json").exists()
    assert not (target / "别人的.txt").exists()

    loc.switch(target, cleanup=True)
    assert paths.read_pointer() == {"data_root": str(target), "cleanup": str(env)}

    # 下次启动：paths._data_root 读路标
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.delenv("SIMPLEQUANT_DATA", raising=False)
    assert paths._data_root() == target

    assert loc.cleanup_pending(root=target) is True
    assert not (env / "data_cache").exists() and not (env / "llm.json").exists()
    assert (env / "别人的.txt").exists() and (env / "location.json").exists()
    assert paths.read_pointer() == {"data_root": str(target)}
    assert loc.cleanup_pending(root=target) is None


def test_move_back_to_default_drops_data_root(env, tmp_path):
    other = _fake_data(tmp_path / "D" / "SimpleQuant")
    loc.switch(env, cleanup=True, src=other)
    assert paths.read_pointer() == {"cleanup": str(other)}


def test_cleanup_skips_when_new_location_not_in_use(env, tmp_path):
    """路标指向的位置打不开、退回了默认目录：旧数据是唯一一份，不删"""
    loc.switch(tmp_path / "gone", cleanup=True)
    assert loc.cleanup_pending(root=env) is False
    assert (env / "data_cache" / "a.json").exists()


def test_existing_data_keeps_old(env, tmp_path):
    old = _fake_data(tmp_path / "old")
    loc.switch(old, cleanup=False)
    assert loc.cleanup_pending(root=old) is None
    assert (env / "data_cache").exists()


def test_missing_location_falls_back(env, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "user_data_dir", lambda: env)
    monkeypatch.delenv("SIMPLEQUANT_DATA", raising=False)
    (env / "location.json").write_text(json.dumps({"data_root": str(tmp_path / "X:" / "gone")}), encoding="utf-8")
    assert paths._data_root() == env
    assert paths.LOCATION_MISSING == tmp_path / "X:" / "gone"
    monkeypatch.setattr(paths, "LOCATION_MISSING", None)


def test_copy_refuses_while_paper_running(env, tmp_path):
    from simplequant.paper import runner
    lock = env / "data_cache" / "paper" / "run.lock"
    with open(lock, "a+") as f:
        assert runner._try_lock(f)
        try:
            with pytest.raises(loc.Busy):
                loc.copy_data(tmp_path / "D" / "SimpleQuant")
        finally:
            runner._unlock(f)


def test_relaunch_env_drops_storage_path():
    """重启后的新进程不沿用本进程的界面存储目录：否则换了数据位置后仍往旧位置存，旧位置一删就报 FileNotFoundError"""
    from simplequant.system import relaunch_env
    env = relaunch_env({"NICEGUI_STORAGE_PATH": r"C:\old\data_cache\gui_storage", "PATH": "x"})
    assert env == {"PATH": "x", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    assert "PYINSTALLER_RESET_ENVIRONMENT" in relaunch_env()


def test_relaunch_cmd():
    from simplequant.system import relaunch_cmd
    win = relaunch_cmd(42, [r"C:\Users\O'Brien\SimpleQuant.exe", "--x", "a b"], system="windows")[-1]
    assert win.startswith("Wait-Process -Id 42 ")
    assert r"-FilePath 'C:\Users\O''Brien\SimpleQuant.exe'" in win
    assert """-ArgumentList '--x "a b"'""" in win
    assert relaunch_cmd(42, ["/Applications/SimpleQuant.app/Contents/MacOS/SimpleQuant"], system="macos")[-1] == \
        "while kill -0 42 2>/dev/null; do sleep 0.3; done; exec /Applications/SimpleQuant.app/Contents/MacOS/SimpleQuant"
