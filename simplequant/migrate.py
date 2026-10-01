"""
打包版第一次运行：从源码版的项目目录导入数据（行情库、选股数据、模拟盘、保存的策略、自定义因子）

源码版数据在 <项目>/data_cache 等目录，打包版在 %LOCALAPPDATA%\\SimpleQuant（见 paths.py）。
旧目录的候选：打包时记录的项目目录（_build_info.json）、exe 所在目录及其上级（exe 放在项目的 dist 下时）。
只复制、不删除旧数据；已存在的同名文件会被覆盖。导入或选择「不用了」后写标记文件，不再提示。
"""

import json
import shutil
import sys
from pathlib import Path

from .paths import DATA_ROOT, FROZEN

DIRS = ("data_cache", "user_strategies", "user_factors")
SKIP = {"gui_storage"}              # 界面偏好：程序运行中正在使用，不复制
MARK = DATA_ROOT / ".migrated"
BUILD_INFO = Path(__file__).with_name("_build_info.json")


def _is_legacy(d: Path) -> bool:
    return (d / "data_cache").is_dir() and d.resolve() != DATA_ROOT.resolve()


def candidates() -> list[Path]:
    out = []
    try:
        out.append(Path(json.loads(BUILD_INFO.read_text(encoding="utf-8"))["source_dir"]))
    except (OSError, ValueError, KeyError):
        pass
    out += list(Path(sys.executable).resolve().parents)[:4]
    seen, found = set(), []
    for d in out:
        if str(d) not in seen and _is_legacy(d):
            seen.add(str(d))
            found.append(d)
    return found


def pending() -> Path | None:
    """需要提示导入时返回找到的旧目录"""
    if not FROZEN or MARK.exists():
        return None
    found = candidates()
    return found[0] if found else None


def size_mb(src: Path) -> float:
    total = 0
    for name in DIRS:
        d = src / name
        if d.is_dir():
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not (SKIP & set(f.parts)))
    return total / 1e6


def import_from(src: Path, progress=None) -> int:
    """复制旧目录的数据；返回复制的文件数"""
    src = Path(src)
    if not _is_legacy(src):
        raise ValueError(f"no data_cache in / 这里没有 data_cache：{src}")
    files = [f for name in DIRS if (src / name).is_dir() for f in (src / name).rglob("*")
             if f.is_file() and not (SKIP & set(f.relative_to(src).parts))]
    for i, f in enumerate(files, 1):
        target = DATA_ROOT / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        if progress and (i % 50 == 0 or i == len(files)):
            progress(i, len(files))
    dismiss()
    return len(files)


def dismiss():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    MARK.write_text("1", encoding="utf-8")
