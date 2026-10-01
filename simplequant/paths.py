"""
数据目录（集中在这里，便于打包成 exe）

- 源码运行：沿用项目目录，<项目>/data_cache、<项目>/user_strategies
- 打包成 exe 后：程序目录可能不能写，改到 %LOCALAPPDATA%\\SimpleQuant
- 环境变量 SIMPLEQUANT_DATA 可指定其他目录（优先级最高）
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
FROZEN = getattr(sys, "frozen", False)


def _data_root() -> Path:
    if env := os.environ.get("SIMPLEQUANT_DATA"):
        return Path(env)
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SimpleQuant"
    return PROJECT_DIR


DATA_ROOT = _data_root()
CACHE_DIR = DATA_ROOT / "data_cache"            # 行情库、选股数据、模拟盘
STRATEGY_DIR = DATA_ROOT / "user_strategies"    # 保存的策略（JSON）
FACTOR_DIR = DATA_ROOT / "user_factors"         # 自定义选股因子（JSON，含代码）
