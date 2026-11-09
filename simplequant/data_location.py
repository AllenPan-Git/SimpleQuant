"""
数据保存位置（用户反馈：数据全在 C 盘，希望能换到别的盘）

打包版的数据默认在 system.user_data_dir()（Windows %LOCALAPPDATA%\\SimpleQuant）。换位置的做法：
1. 后台把当前数据目录里属于 SimpleQuant 的条目（ENTRIES）复制到新位置，核对文件数和大小
2. 在默认目录写路标 location.json：{"data_root": 新位置, "cleanup": 旧位置}；paths.py 启动时读它
3. 重启程序。各模块在导入时就记下了 CACHE_DIR 等路径，所以运行中切换不了，必须重启
4. 新位置启动成功后，后台删除旧位置的这些条目（cleanup_pending），腾出空间

只复制、删除 ENTRIES 里列出的条目：新位置可能是用户自己的文件夹（比如选了已有数据的源码目录），
别的文件一律不碰。选中的文件夹里已经有 SimpleQuant 数据（重装系统后指回去）时直接改用那里，不复制，旧位置也不删。
"""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import paths
from . import system
from .system import user_data_dir

# 数据目录里属于 SimpleQuant 的条目（见 paths.py、llm/config.py、migrate.py、update/client.py、main.py）
ENTRIES = ("data_cache", "user_strategies", "user_factors", "logs", "updates", "scripts", "llm.json", ".migrated")
FOLDER_NAME = "SimpleQuant"
SPARE = 1.05            # 目标盘至少要有数据大小的 1.05 倍 + 100 MB 可用空间
SPARE_MB = 100


class LocationError(ValueError):
    """不能搬到这个位置；code 对应界面文字 loc.err_<code>"""

    def __init__(self, code: str, **kw):
        super().__init__(code)
        self.code, self.kw = code, kw


def enabled() -> bool:
    """只有打包版、且没用环境变量指定目录时才能在界面里改（源码版的数据就在项目目录）"""
    return paths.FROZEN and not os.environ.get("SIMPLEQUANT_DATA")


def on_system_drive(root: Path) -> bool:
    """首页只在数据位于 Windows 系统盘时询问（macOS / Linux 一般只有一个分区）"""
    return system.WINDOWS and Path(root).drive.upper() == os.environ.get("SystemDrive", "C:").upper()


def is_data_dir(d: Path) -> bool:
    return (Path(d) / "data_cache").is_dir()


def _files(root: Path) -> list[Path]:
    out = []
    for name in ENTRIES:
        p = root / name
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in p.rglob("*") if f.is_file()]
    return out


def size_bytes(root: Path) -> int:
    total = 0
    for f in _files(Path(root)):
        try:
            total += f.stat().st_size
        except OSError:          # 统计途中被删掉（比如日志轮换）
            pass
    return total


def _existing_parent(p: Path) -> Path | None:
    for d in (p, *p.parents):
        if d.exists():
            return d
    return None


def free_bytes(p: Path) -> int | None:
    d = _existing_parent(Path(p))
    return shutil.disk_usage(d).free if d else None


def _inside(a: Path, b: Path) -> bool:
    """a 在 b 里面（或就是 b）"""
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def _same(a: Path, b: Path) -> bool:
    return _inside(a, b) and _inside(b, a)


@dataclass
class Plan:
    target: Path          # 最终的数据目录
    existing: bool        # 那里已经有 SimpleQuant 数据：直接改用，不复制
    need: int             # 要复制的字节数（existing 时为 0）


def plan(picked: str | Path, src: Path | None = None) -> Plan:
    """
    检查用户选的文件夹，算出最终位置。选的是一般文件夹（比如 D:\\）时在里面建 SimpleQuant 子文件夹，
    免得数据散在盘符根目录或混进别的文件里
    """
    src = Path(src or paths.DATA_ROOT)
    raw = str(picked or "").strip().strip('"')
    if not raw:
        raise LocationError("empty")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise LocationError("relative")
    if is_data_dir(p) or _same(p, user_data_dir()):        # 搬回默认目录：那里只剩路标，也直接用
        target = p
    elif p.name.lower() == FOLDER_NAME.lower() and (not p.exists() or not any(p.iterdir())):
        target = p
    else:
        target = p / FOLDER_NAME
    if target.exists() and not target.is_dir():
        raise LocationError("not_dir")
    if _same(target, src):
        raise LocationError("same")
    if _inside(target, src) or _inside(src, target):
        raise LocationError("nested")
    root = _existing_parent(target)
    if root is None:
        raise LocationError("no_drive")
    if is_data_dir(target):
        return Plan(target, True, 0)
    need = size_bytes(src)
    free = free_bytes(target)
    want = int(need * SPARE) + SPARE_MB * 1_000_000
    if free is not None and free < want:
        raise LocationError("space", need=need, free=free)
    _check_writable(target)
    return Plan(target, False, need)


def _check_writable(target: Path):
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".sq_write_test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise LocationError("write", detail=str(e)) from e


class Busy(RuntimeError):
    """模拟盘正在更新（定时任务或程序窗口），这时复制会拷到写了一半的文件"""


def _try_paper_lock(root: Path):
    """拿得到模拟盘的锁就返回打开的文件（调用方负责释放），拿不到抛 Busy；没有模拟盘目录时返回 None"""
    from .paper import runner
    lock = root / "data_cache" / "paper" / "run.lock"
    if not lock.parent.is_dir():
        return None
    f = open(lock, "a+")
    if not runner._try_lock(f):
        f.close()
        raise Busy()
    return f


def _release(f):
    if f is not None:
        from .paper import runner
        runner._unlock(f)
        f.close()


def copy_data(target: Path, src: Path | None = None, progress=None) -> int:
    """复制并核对；返回文件数。复制期间拿着模拟盘的锁，定时任务会等它复制完"""
    src, target = Path(src or paths.DATA_ROOT), Path(target)
    lock = _try_paper_lock(src)
    try:
        # 锁文件正被自己拿着，Windows 上读不了；新位置会重新建
        files = [f for f in _files(src) if f.name != "run.lock"]
        total = sum(f.stat().st_size for f in files) or 1
        done = 0
        for i, f in enumerate(files, 1):
            dst = target / f.relative_to(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            if dst.stat().st_size != f.stat().st_size:
                raise OSError(f"size mismatch / 复制后大小不一致：{f}")
            done += f.stat().st_size
            if progress:
                progress(done / total, i, len(files))
        return len(files)
    finally:
        _release(lock)


def _write_pointer(data: dict, pointer: Path):
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, pointer)


def switch(target: Path, cleanup: bool, src: Path | None = None, pointer: Path | None = None,
           default: Path | None = None):
    """写路标；下次启动起生效。搬回默认目录时不写 data_root；cleanup=False（改用已有数据）时旧位置保留"""
    src, target = Path(src or paths.DATA_ROOT), Path(target)
    pointer, default = Path(pointer or paths.POINTER), Path(default or user_data_dir())
    data = {} if _same(target, default) else {"data_root": str(target)}
    if cleanup:
        data["cleanup"] = str(src)
    _write_pointer(data, pointer)


def cleanup_pending(root: Path | None = None, pointer: Path | None = None) -> bool | None:
    """
    新位置启动成功后删除旧位置的数据（程序启动时在后台调用）。
    返回 None 表示没有要清理的；True 清理完；False 这次没删完（文件被占用、模拟盘正在旧位置更新），下次启动再试
    """
    root, pointer = Path(root or paths.DATA_ROOT), Path(pointer or paths.POINTER)
    data = paths.read_pointer(pointer)
    old = data.get("cleanup")
    if not old:
        return None
    old = Path(old)
    if _same(old, root) or not is_data_dir(root):
        return False          # 新位置没在用（比如退回了默认目录），旧数据是唯一一份，不能删
    if old.is_dir():
        try:
            lock = _try_paper_lock(old)
        except Busy:
            return False
        _release(lock)
        for name in ENTRIES:
            p = old / name
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        if any((old / name).exists() for name in ENTRIES):
            return False
        try:
            old.rmdir()           # 空了才删得掉；默认目录里还有路标，会留下
        except OSError:
            pass
    data.pop("cleanup", None)
    _write_pointer(data, pointer)
    return True
