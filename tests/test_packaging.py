"""
打包相关：命令行运行导出的选股脚本、打包版首次运行导入旧数据、定时任务命令
"""

import ast
import sys
from dataclasses import asdict

import pytest

import main
from simplequant import migrate
from simplequant.engine import BrokerConfig
from simplequant.export import selection_script
from simplequant.export import run_script
from simplequant.paper import schedule


def test_run_script_writes_log_and_returns_code(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text('import sys\nif __name__ == "__main__":\n    print("hello 你好")\n', encoding="utf-8")
    assert main.main(["--run-script", str(ok)]) == 0
    log = run_script.log_path(ok).read_text(encoding="utf-8")
    assert "hello 你好" in log and "[done" in log

    bad = tmp_path / "bad.py"
    bad.write_text('if __name__ == "__main__":\n    1 / 0\n', encoding="utf-8")
    assert run_script.run(bad) == 1
    assert "ZeroDivisionError" in run_script.log_path(bad).read_text(encoding="utf-8")
    assert run_script.run(tmp_path / "missing.py") == 2


def test_run_script_restores_streams(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print(1)", encoding="utf-8")
    before = sys.stdout
    run_script.run(f)
    assert sys.stdout is before


def test_command_source_mode():
    cmd = run_script.command("a.py")
    assert cmd[0] == sys.executable and cmd[1].endswith("main.py") and cmd[2:] == ["--run-script", "a.py"]


def test_selection_script_for_exe_mentions_exe_and_writes_next_to_itself():
    spec = {"kind": "selection", "universe": "hs300", "factors": [{"key": "roe", "weight": 1, "direction": 1}],
            "top_n": 10, "rebalance": "monthly", "position_pct": 95, "dividend": "cash",
            "filters": {"exclude_st": True, "min_list_days": 250}}
    code = selection_script(spec, asdict(BrokerConfig()), "2021-01-04", r"D:\p", exe=r"C:\App\SimpleQuant.exe")
    ast.parse(code)
    ns = {"__name__": "exported", "__file__": "x.py"}
    exec(compile(code, "x.py", "exec"), ns)          # 顶层可以执行（曾经把 JSON 的 true 直接写进 Python）
    assert ns["SPEC"] == spec
    assert r'"C:\App\SimpleQuant.exe" --run-script' in code
    assert "NEEDS_FIN = True" in code and 'div=SPEC.get("dividend") == "cash"' in code and "OUT_DIR" in code
    # 界面里用这一行识别导出的选股脚本
    from gui.pages.selection import SCRIPT_MARKS
    assert SCRIPT_MARKS[0] in code


def test_migrate_copies_data_and_skips_gui_storage(tmp_path, monkeypatch):
    src, dst = tmp_path / "old", tmp_path / "new"
    (src / "data_cache" / "stocks" / "daily").mkdir(parents=True)
    (src / "data_cache" / "stocks" / "daily" / "sh.600000.parquet").write_bytes(b"x" * 10)
    (src / "data_cache" / "gui_storage").mkdir()
    (src / "data_cache" / "gui_storage" / "storage-general.json").write_text("{}")
    (src / "user_strategies").mkdir()
    (src / "user_strategies" / "a.json").write_text("{}")
    monkeypatch.setattr(migrate, "DATA_ROOT", dst)
    monkeypatch.setattr(migrate, "MARK", dst / ".migrated")
    monkeypatch.setattr(migrate, "FROZEN", True)
    monkeypatch.setattr(migrate, "candidates", lambda: [src])
    assert migrate.pending() == src
    assert migrate.size_mb(src) > 0
    seen = []
    assert migrate.import_from(src, lambda i, n: seen.append((i, n))) == 2
    assert (dst / "data_cache" / "stocks" / "daily" / "sh.600000.parquet").exists()
    assert (dst / "user_strategies" / "a.json").exists()
    assert not (dst / "data_cache" / "gui_storage").exists()
    assert seen[-1] == (2, 2)
    assert migrate.pending() is None                  # 导入后不再提示
    with pytest.raises(ValueError):
        migrate.import_from(tmp_path / "nothing")


def test_migrate_not_offered_in_source_mode():
    assert migrate.pending() is None


def test_task_command(monkeypatch):
    assert schedule.task_command() == f'"{schedule.BAT}"'
    monkeypatch.setattr(schedule, "FROZEN", True)
    assert schedule.task_command() == f'"{sys.executable}" --paper'
