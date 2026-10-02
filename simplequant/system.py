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
