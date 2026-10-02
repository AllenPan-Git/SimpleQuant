"""
数据目录（集中在这里，便于打包成 exe）

- 源码运行：沿用项目目录，<项目>/data_cache、<项目>/user_strategies
- 打包版：程序目录可能不能写，改到用户数据目录（system.user_data_dir）：Windows %LOCALAPPDATA%\\SimpleQuant，
  macOS ~/Library/Application Support/SimpleQuant，Linux $XDG_DATA_HOME/SimpleQuant（默认 ~/.local/share）
- 环境变量 SIMPLEQUANT_DATA 可指定其他目录（优先级最高）
"""

import os
import sys
from pathlib import Path

from .system import user_data_dir

PROJECT_DIR = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)


def _data_root() -> Path:
    if env := os.environ.get("SIMPLEQUANT_DATA"):
        return Path(env)
    if FROZEN:
        return user_data_dir()
    return PROJECT_DIR


DATA_ROOT = _data_root()
CACHE_DIR = DATA_ROOT / "data_cache"            # 行情库、选股数据、模拟盘
STRATEGY_DIR = DATA_ROOT / "user_strategies"    # 保存的策略（JSON）
FACTOR_DIR = DATA_ROOT / "user_factors"         # 自定义选股因子（JSON，含代码）
