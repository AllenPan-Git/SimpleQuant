"""
定时任务：每个交易日傍晚自动运行模拟盘（电脑需要开着）

- Windows：任务计划程序（schtasks）
- macOS：launchd 用户代理（~/Library/LaunchAgents），睡眠中错过的会在唤醒后补跑
- Linux：systemd 用户定时器（~/.config/systemd/user，Persistent=true 关机错过的开机后补跑）；
  没有 systemd 用户实例时（部分精简系统、WSL）改用 crontab

源码版运行项目根目录下的 paper_daily.bat / paper_daily.sh；打包版运行 SimpleQuant --paper。任务名固定，便于查询和删除。
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..paths import FROZEN, PROJECT_DIR
from ..system import SYSTEM

TASK_NAME = "SimpleQuantPaperDaily"
BAT = PROJECT_DIR / "paper_daily.bat"
SH = PROJECT_DIR / "paper_daily.sh"
LAUNCHD_LABEL = "com.simplequant.paperdaily"
SYSTEMD_UNIT = "simplequant-paper"
CRON_MARK = f"# {TASK_NAME}"


def task_argv() -> list[str]:
    if FROZEN:
        return [sys.executable, "--paper"]
    return [str(BAT)] if SYSTEM == "windows" else ["/bin/sh", str(SH)]


def task_command() -> str:
    """显示给用户看的命令"""
    if SYSTEM == "windows":
        return f'"{sys.executable}" --paper' if FROZEN else f'"{BAT}"'
    return shlex.join(task_argv())


def backend() -> str:
    """windows / launchd / systemd / cron"""
    if SYSTEM == "windows":
        return "windows"
    if SYSTEM == "macos":
        return "launchd"
    return "systemd" if _systemd_ok() else "cron"


def create_task(at: str = "19:00") -> tuple[bool, str]:
    """周一到周五 at 运行（节假日运行也无妨：没有新数据时不会产生新成交）；成功时说明可能为空"""
    b = backend()
    if b == "windows":
        return _schtasks("/Create", "/F", "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/TN", TASK_NAME,
                         "/TR", task_command(), "/ST", at)
    try:
        hour, minute = (int(x) for x in at.split(":")[:2])
    except ValueError:
        return False, f"invalid time: {at}"
    return {"launchd": _launchd_create, "systemd": _systemd_create, "cron": _cron_create}[b](hour, minute)


def delete_task() -> tuple[bool, str]:
    b = backend()
    if b == "windows":
        return _schtasks("/Delete", "/F", "/TN", TASK_NAME)
    return {"launchd": _launchd_delete, "systemd": _systemd_delete, "cron": _cron_delete}[b]()


def task_exists() -> bool:
    b = backend()
    if b == "windows":
        return _schtasks("/Query", "/TN", TASK_NAME)[0]
    if b == "launchd":
        return _launchd_plist().exists()
    if b == "systemd":
        return _run("systemctl", "--user", "is-enabled", f"{SYSTEMD_UNIT}.timer")[0]
    return any(CRON_MARK in line for line in _crontab_lines())


def _run(*args, encoding: str = "utf-8", stdin: str | None = None) -> tuple[bool, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, encoding=encoding, errors="replace",
                           timeout=30, input=stdin,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 打包版不闪控制台窗口
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


# ---------- Windows ----------
def _schtasks(*args) -> tuple[bool, str]:
    return _run("schtasks", *args, encoding="gbk")


# ---------- macOS：launchd ----------
def _launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_create(hour: int, minute: int) -> tuple[bool, str]:
    plist = _launchd_plist()
    plist.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(sys.executable).parent if FROZEN else PROJECT_DIR
    plist.write_bytes(plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": task_argv(),
        "WorkingDirectory": str(cwd),
        "StartCalendarInterval": [{"Weekday": d, "Hour": hour, "Minute": minute} for d in range(1, 6)],
        "StandardOutPath": os.devnull, "StandardErrorPath": os.devnull,   # 日志由程序自己写 daily.log
    }))
    domain = f"gui/{os.getuid()}"
    _run("launchctl", "bootout", domain, str(plist))                  # 已加载时先卸下，才能读到新时间
    ok, msg = _run("launchctl", "bootstrap", domain, str(plist))
    if not ok:                                                        # 旧版 macOS
        ok, msg = _run("launchctl", "load", "-w", str(plist))
    return (True, "") if ok else (False, msg)


def _launchd_delete() -> tuple[bool, str]:
    plist = _launchd_plist()
    if plist.exists():
        if not _run("launchctl", "bootout", f"gui/{os.getuid()}", str(plist))[0]:
            _run("launchctl", "unload", "-w", str(plist))
        plist.unlink()
    return True, ""


# ---------- Linux：systemd 用户定时器 ----------
def _systemd_ok() -> bool:
    return bool(shutil.which("systemctl")) and _run("systemctl", "--user", "show-environment")[0]


def _systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemd_create(hour: int, minute: int) -> tuple[bool, str]:
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SYSTEMD_UNIT}.service").write_text(
        "[Unit]\nDescription=SimpleQuant paper trading daily run\n\n"
        f"[Service]\nType=oneshot\nExecStart={' '.join(_systemd_quote(a) for a in task_argv())}\n",
        encoding="utf-8")
    (d / f"{SYSTEMD_UNIT}.timer").write_text(
        "[Unit]\nDescription=SimpleQuant paper trading daily run\n\n"
        f"[Timer]\nOnCalendar=Mon..Fri *-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
    ok, msg = _run("systemctl", "--user", "daemon-reload")
    if ok:
        ok, msg = _run("systemctl", "--user", "enable", f"{SYSTEMD_UNIT}.timer")
    if ok:
        ok, msg = _run("systemctl", "--user", "restart", f"{SYSTEMD_UNIT}.timer")    # 让新时间生效
    return (True, "") if ok else (False, msg)


def _systemd_delete() -> tuple[bool, str]:
    _run("systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer")
    d = _systemd_dir()
    for ext in ("timer", "service"):
        (d / f"{SYSTEMD_UNIT}.{ext}").unlink(missing_ok=True)
    _run("systemctl", "--user", "daemon-reload")
    return True, ""


# ---------- Linux：crontab ----------
def _crontab_lines() -> list[str]:
    ok, out = _run("crontab", "-l")
    return out.splitlines() if ok else []          # 还没有 crontab 时返回非零（no crontab for user）


def _crontab_write(lines: list[str]) -> tuple[bool, str]:
    ok, msg = _run("crontab", "-", stdin="\n".join(lines) + "\n" if lines else "")
    return (True, "") if ok else (False, msg)


def _cron_create(hour: int, minute: int) -> tuple[bool, str]:
    keep = [ln for ln in _crontab_lines() if CRON_MARK not in ln]
    return _crontab_write(keep + [f"{minute} {hour} * * 1-5 {shlex.join(task_argv())} {CRON_MARK}"])


def _cron_delete() -> tuple[bool, str]:
    lines = _crontab_lines()
    keep = [ln for ln in lines if CRON_MARK not in ln]
    return _crontab_write(keep) if len(keep) != len(lines) else (True, "")
