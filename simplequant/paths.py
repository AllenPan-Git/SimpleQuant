"""
数据目录（集中在这里，便于打包成 exe）

- 源码运行：沿用项目目录，<项目>/data_cache、<项目>/user_strategies
- 打包版：程序目录可能不能写，改到用户数据目录（system.user_data_dir）：Windows %LOCALAPPDATA%\\SimpleQuant，
  macOS ~/Library/Application Support/SimpleQuant，Linux $XDG_DATA_HOME/SimpleQuant（默认 ~/.local/share）
- 打包版可在设置页把数据搬到其他位置（data_location.py）：默认目录里只留一个路标 location.json，
  写着新位置；启动时先读它。新位置打不开（移动硬盘没插、文件夹被删）时本次退回默认目录，记在 LOCATION_MISSING
- 环境变量 SIMPLEQUANT_DATA 可指定其他目录（优先级最高）
"""

import json
import os
import sys
from pathlib import Path

from .system import user_data_dir

PROJECT_DIR = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)


POINTER = user_data_dir() / "location.json"     # 路标：始终在默认目录
LOCATION_MISSING: Path | None = None            # 路标指向的位置打不开时记在这里，首页提示


def read_pointer(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or POINTER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _data_root() -> Path:
    global LOCATION_MISSING
    if env := os.environ.get("SIMPLEQUANT_DATA"):
        return Path(env)
    if FROZEN:
        if pointed := read_pointer().get("data_root"):
            if Path(pointed).is_dir():
                return Path(pointed)
            LOCATION_MISSING = Path(pointed)     # 不自动新建：盘符不在时建不了，建了也是空的，用户会以为数据丢了
        return user_data_dir()
    return PROJECT_DIR


DATA_ROOT = _data_root()
CACHE_DIR = DATA_ROOT / "data_cache"            # 行情库、选股数据、模拟盘
STRATEGY_DIR = DATA_ROOT / "user_strategies"    # 保存的策略（JSON）
FACTOR_DIR = DATA_ROOT / "user_factors"         # 自定义选股因子（JSON，含代码）
