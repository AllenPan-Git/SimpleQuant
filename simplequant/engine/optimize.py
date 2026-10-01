"""
参数网格优化
- 对策略描述中任意数值（模板参数、规则里的指标周期/比较值/仓位）做网格搜索
- 多进程并行；进程池不可用时自动退回单进程
- 支持样本内/样本外切分：在前段数据上找最优参数，再到后段数据上检验，识别过拟合
"""

import itertools
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

from ..strategies import resolve, set_path, is_valid_combo
from ..strategies.code_strategy import explain_error
from .runner import run_backtest, BrokerConfig

MAX_COMBOS = 500
# 可作为优化目标的指标（均为越大越好；最大回撤是负数，越接近 0 越好）
TARGET_METRICS = ["sharpe", "total_return", "cagr", "calmar", "max_drawdown", "excess_return",
                  "win_rate", "profit_factor"]


def value_range(start: float, stop: float, step: float, is_int: bool) -> list:
    """闭区间等差序列"""
    if step <= 0 or stop < start:
        return [int(start) if is_int else float(start)]
    n = int(np.floor((stop - start) / step + 1e-9)) + 1
    vals = [start + i * step for i in range(n)]
    return sorted({int(round(v)) for v in vals}) if is_int else [round(v, 6) for v in vals]


def build_grid(spec: dict, axes: dict[str, list]) -> list[tuple[dict, dict]]:
    """返回 [(参数组合, 对应策略描述)]，已剔除模板约束不允许的组合"""
    paths = list(axes)
    grid = []
    for values in itertools.product(*(axes[p] for p in paths)):
        combo = dict(zip(paths, values))
        s = spec
        for p, v in combo.items():
            s = set_path(s, p, v)
        if is_valid_combo(s):
            grid.append((combo, s))
    return grid


def split_prices(prices: dict[str, pd.DataFrame], oos_frac: float):
    """按公共时间区间切分成 (样本内, 样本外, 切分时刻)"""
    start = max(df.index[0] for df in prices.values())
    end = min(df.index[-1] for df in prices.values())
    cut = start + (end - start) * (1 - oos_frac)
    ins = {k: df[(df.index >= start) & (df.index <= cut)] for k, df in prices.items()}
    oos = {k: df[(df.index > cut) & (df.index <= end)] for k, df in prices.items()}
    return ins, oos, cut


# ---------- 工作进程 ----------
_G: dict = {}


def _init(prices, broker):
    _G["prices"], _G["broker"] = prices, broker


def _run(spec):
    try:
        cls, params = resolve(spec)
        return run_backtest(_G["prices"], cls, params, _G["broker"]).metrics
    except Exception as e:  # noqa: BLE001 - 单个组合失败不影响整体
        return {"error": explain_error(e)}


def default_workers() -> int:
    return max(1, min((os.cpu_count() or 2) - 1, 8))


def optimize(prices: dict[str, pd.DataFrame], spec: dict, axes: dict[str, list],
             broker: BrokerConfig | None = None, workers: int | None = None, progress=None) -> pd.DataFrame:
    """
    :param axes: {参数路径: 取值列表}
    :param progress: 回调 progress(已完成数, 总数)
    :return: 每个组合一行，列 = 参数路径 + 各项指标（出错的组合有 error 列）
    """
    broker = broker or BrokerConfig()
    grid = build_grid(spec, axes)
    if not grid:
        raise ValueError("no valid parameter combinations / 没有有效的参数组合")
    if len(grid) > MAX_COMBOS:
        raise ValueError(f"too many combinations ({len(grid)} > {MAX_COMBOS}) / 组合数过多")

    n, results = len(grid), [None] * len(grid)
    workers = workers or default_workers()

    def sequential():
        _init(prices, broker)
        for i, (_, s) in enumerate(grid):
            results[i] = _run(s)
            if progress:
                progress(i + 1, n)

    if workers <= 1 or n < 4:
        sequential()
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(prices, broker)) as ex:
                futures = {ex.submit(_run, s): i for i, (_, s) in enumerate(grid)}
                for done, f in enumerate(as_completed(futures), 1):
                    results[futures[f]] = f.result()
                    if progress:
                        progress(done, n)
        except (BrokenProcessPool, OSError):
            sequential()

    return pd.DataFrame([{**combo, **m} for (combo, _), m in zip(grid, results)])


def best_row(df: pd.DataFrame, metric: str) -> pd.Series:
    ok = df[df[metric].notna()] if metric in df else df.iloc[0:0]
    if ok.empty:
        raise ValueError("all combinations failed / 所有组合都失败了")
    return ok.sort_values(metric, ascending=False).iloc[0]


def apply_combo(spec: dict, row: pd.Series, is_int: dict[str, bool]) -> dict:
    """把结果表中某一行的参数写回策略描述；is_int: {参数路径: 是否整数}"""
    s = spec
    for p, as_int in is_int.items():
        s = set_path(s, p, int(round(row[p])) if as_int else float(row[p]))
    return s
