"""
操作系统相关的小工具（Windows / macOS / Linux 的差别集中在这里）
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

SYSTEM = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
WINDOWS, MACOS, LINUX = SYSTEM == "windows", SYSTEM == "macos", SYSTEM == "linux"


def arch(machine: str | None = None) -> str:
    m = (machine or platform.machine()).lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(m, m)


def target(system: str = SYSTEM, machine: str | None = None) -> str:
    """
    发布包的平台名：windows（只有 64 位）、macos-arm64、macos-x86_64、linux-x86_64……
    自动更新按它取对应的清单（manifest-<平台名>.json，Windows 沿用 manifest.json）
    """
    return "windows" if system == "windows" else f"{system}-{arch(machine)}"


TARGET = target()


def user_data_dir(system: str = SYSTEM) -> Path:
    """打包版的数据目录（每个用户一份）"""
    home = Path.home()
    if system == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "SimpleQuant"
    if system == "macos":
        return home / "Library" / "Application Support" / "SimpleQuant"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "SimpleQuant"


def app_dir(executable: str | None = None, system: str = SYSTEM) -> Path:
    """
    打包版的程序目录（自动更新整体替换的范围）
    Windows / Linux：可执行文件所在的文件夹；macOS：SimpleQuant.app（可执行文件在 .app/Contents/MacOS 下）
    """
    exe = Path(executable or sys.executable).resolve()
    if system == "macos" and exe.parent.name == "MacOS" and exe.parents[1].name == "Contents":
        return exe.parents[2]
    return exe.parent


def venv_python(project_dir: str | Path, system: str = SYSTEM) -> str:
    """源码版虚拟环境里的 Python（导出脚本的说明里用）"""
    sep = "\\" if system == "windows" else "/"
    parts = (".venv", "Scripts", "python.exe") if system == "windows" else (".venv", "bin", "python")
    return sep.join([str(project_dir).rstrip("\\/"), *parts])


def open_folder(path: str | Path):
    """在文件管理器里打开文件夹"""
    if WINDOWS:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if MACOS else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def relaunch_cmd(pid: int, argv: list[str], system: str = SYSTEM) -> list[str]:
    """等进程 pid 退出后重新启动 argv 的命令（换数据位置后重启用）。先等旧进程完全退出，
    新进程才不会和它同时写界面偏好、日志，也不会删旧数据时碰上还开着的文件"""
    if system == "windows":
        exe, args = argv[0], argv[1:]
        quote = lambda s: "'" + s.replace("'", "''") + "'"      # noqa: E731 - PowerShell 单引号字符串
        start = f"Start-Process -FilePath {quote(exe)}"
        if args:   # Start-Process 把数组元素直接用空格拼起来、不加引号；先按 Windows 命令行规则拼成一个字符串
            start += " -ArgumentList " + quote(subprocess.list2cmdline(args))
        script = f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; {start}"
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]
    import shlex
    return ["/bin/sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; exec {shlex.join(argv)}"]


# 本进程按数据目录算出来、写进环境变量的路径：不能带给重启后的新进程（数据目录可能刚换了位置）
RELAUNCH_DROP = ("NICEGUI_STORAGE_PATH",)


def relaunch_env(environ=None) -> dict:
    """重启后的新进程用的环境变量。
    gui.app 用 setdefault 设 NICEGUI_STORAGE_PATH（界面偏好、最近的报告存在哪）；3.2.0～3.2.1 把它原样传给新进程，
    更改数据保存位置后新进程仍往旧位置存，旧位置随后被删掉，每次保存都报 FileNotFoundError、这次运行里的偏好存不下来"""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in RELAUNCH_DROP}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"    # 打包版：新进程按独立程序启动，不沿用本进程的解压目录等设置
    return env


def relaunch_after_exit(argv: list[str]):
    """启动等待脚本（之后调用方应立即退出程序）"""
    env = relaunch_env()
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    cmd = relaunch_cmd(os.getpid(), argv)
    if WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:   # 脱离可能存在的作业对象，程序退出时脚本不会被一起结束
            subprocess.Popen(cmd, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
