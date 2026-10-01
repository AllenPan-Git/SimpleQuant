"""
Windows 定时任务：每个交易日傍晚自动运行模拟盘（电脑需要开着）
源码版运行项目根目录下的 paper_daily.bat；打包版运行 SimpleQuant.exe --paper。任务名固定，便于查询和删除。
"""

import subprocess
import sys

from ..paths import FROZEN, PROJECT_DIR

TASK_NAME = "SimpleQuantPaperDaily"
BAT = PROJECT_DIR / "paper_daily.bat"


def task_command() -> str:
    return f'"{sys.executable}" --paper' if FROZEN else f'"{BAT}"'


def _schtasks(*args) -> tuple[bool, str]:
    try:
        r = subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="gbk", errors="replace",
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def create_task(at: str = "19:00") -> tuple[bool, str]:
    """周一到周五 at 运行（节假日运行也无妨：没有新数据时不会产生新成交）"""
    return _schtasks("/Create", "/F", "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/TN", TASK_NAME,
                     "/TR", task_command(), "/ST", at)


def delete_task() -> tuple[bool, str]:
    return _schtasks("/Delete", "/F", "/TN", TASK_NAME)


def task_exists() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME)[0]
