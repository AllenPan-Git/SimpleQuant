"""
运行导出的选股脚本（打包版没有 Python 环境，由 SimpleQuant 自己执行）

    SimpleQuant --run-script 选股.py        （源码版：python main.py --run-script 选股.py）

输出同时写入脚本旁边的 <脚本名>.log（打包成窗口程序后没有控制台，日志是唯一能看到输出的地方）；
界面里的「运行选股脚本」用子进程调用这个入口，并实时显示日志。
"""

import runpy
import sys
import traceback
from pathlib import Path

from ..paths import FROZEN, PROJECT_DIR


def log_path(script: Path) -> Path:
    return Path(script).with_suffix(".log")


def command(script: Path) -> list[str]:
    """运行脚本的命令行（子进程用）"""
    if FROZEN:
        return [sys.executable, "--run-script", str(script)]
    return [sys.executable, str(PROJECT_DIR / "main.py"), "--run-script", str(script)]


class _Tee:
    """同时写到原来的输出（可能为 None）和日志文件"""

    def __init__(self, stream, f):
        self.stream, self.f = stream, f

    def write(self, s):
        if self.stream is not None:
            try:
                self.stream.write(s)
            except (OSError, ValueError):
                pass
        self.f.write(s)
        self.f.flush()
        return len(s)

    def flush(self):
        if self.stream is not None:
            try:
                self.stream.flush()
            except (OSError, ValueError):
                pass
        self.f.flush()


def run(script: str | Path) -> int:
    script = Path(script).resolve()
    if not script.exists():
        print(f"file not found / 找不到文件: {script}", file=sys.stderr)
        return 2
    with open(log_path(script), "w", encoding="utf-8") as f:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(old_out, f), _Tee(old_err, f)
        try:
            runpy.run_path(str(script), run_name="__main__")
            print("\n[done / 完成]")
            return 0
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 0
        except BaseException:  # noqa: BLE001 - 脚本里的任何错误都写进日志
            traceback.print_exc()
            print("\n[failed / 出错]")
            return 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
