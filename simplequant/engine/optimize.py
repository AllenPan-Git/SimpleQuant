"""
参数网格优化
- 对策略描述中任意数值（模板参数、规则里的指标周期/比较值/仓位）做网格搜索
- 多进程并行；进程池不可用时自动退回单进程
- 支持样本内/样本外切分：在前段数据上找最优参数，再到后段数据上检验，识别过拟合
- 每个组合另记各自然年的收益（列名 year:2021 …），用来看最优参数逐年在所有组合里排第几
"""

import itertools
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

from ..strategies import resolve, set_path, is_valid_combo
from ..strategies.code_strategy import explain_error
from .results import nav_of
from .runner import run_backtest, BrokerConfig

MAX_COMBOS = 500
YEAR = "year:"           # 结果表里各年收益的列名前缀
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


def split_prices(prices: dict[str, pd.DataFrame], oos_frac: float, trade_start=None):
    """
    按公共时间区间切分成 (样本内, 样本外, 切分时刻)
    trade_start：行情里带了预热段时只按这天之后切分，样本内、样本外都保留之前的全部数据作为预热
    （样本外的交易起点用 oos_start）
    """
    start = max(df.index[0] for df in prices.values())
    if trade_start is not None:
        start = max(start, pd.Timestamp(trade_start))
    end = min(df.index[-1] for df in prices.values())
    cut = start + (end - start) * (1 - oos_frac)
    if trade_start is not None:
        ins = {k: df[df.index <= cut] for k, df in prices.items()}
        oos = {k: df[df.index <= end] for k, df in prices.items()}
    else:
        ins = {k: df[(df.index >= start) & (df.index <= cut)] for k, df in prices.items()}
        oos = {k: df[(df.index > cut) & (df.index <= end)] for k, df in prices.items()}
    return ins, oos, cut


def oos_start(prices: dict[str, pd.DataFrame], cut) -> pd.Timestamp:
    """切分时刻之后的第一根 K 线（带预热段时样本外从这里开始交易）"""
    return min(df.index[df.index > cut][0] for df in prices.values() if (df.index > cut).any())


# ---------- 工作进程 ----------
_G: dict = {}


def _init(prices, broker, trade_start=None):
    _G["prices"], _G["broker"], _G["trade_start"] = prices, broker, trade_start


def yearly_returns(value: pd.Series, initial_cash: float) -> dict:
    """
    各自然年的收益：年末资产 / 上年末资产 − 1（第一年相对初始资金）。返回 {"year:2021": 0.12, ...}
    定投时传单位净值（nav_of），否则追加的资金会算成收益
    """
    if value.empty:
        return {}
    ends = value.groupby(value.index.year).last()
    prev = ends.shift(1).fillna(initial_cash)
    return {f"{YEAR}{y}": float(e / p - 1) if p else np.nan for y, e, p in zip(ends.index, ends, prev)}


def _run(spec):
    try:
        cls, params = resolve(spec)
        res = run_backtest(_G["prices"], cls, params, _G["broker"], trade_start=_G.get("trade_start"))
        return {**res.metrics, **yearly_returns(nav_of(res.equity), _G["broker"].cash)}
    except Exception as e:  # noqa: BLE001 - 单个组合失败不影响整体
        return {"error": explain_error(e)}


def default_workers() -> int:
    return max(1, min((os.cpu_count() or 2) - 1, 8))


def optimize(prices: dict[str, pd.DataFrame], spec: dict, axes: dict[str, list],
             broker: BrokerConfig | None = None, workers: int | None = None, progress=None,
             trade_start=None) -> pd.DataFrame:
    """
    :param axes: {参数路径: 取值列表}
    :param trade_start: 行情里带了预热段时，交易从这天开始（见 runner.run_backtest）
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
        _init(prices, broker, trade_start)
        for i, (_, s) in enumerate(grid):
            results[i] = _run(s)
            if progress:
                progress(i + 1, n)

    if workers <= 1 or n < 4:
        sequential()
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(prices, broker, trade_start)) as ex:
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


def year_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if isinstance(c, str) and c.startswith(YEAR)]


def yearly_ranks(df: pd.DataFrame, best: pd.Series) -> pd.DataFrame:
    """
    最优参数在每一年的所有组合里排第几（按当年收益，1 = 最好）
    :return: 每年一行：year, ret（最优参数当年收益）, rank, n（当年有结果的组合数）, top（当年最好的收益）, median
    """
    rows = []
    for c in year_columns(df):
        vals = df[c].dropna()
        mine = best.get(c, np.nan)
        if vals.empty or not np.isfinite(mine):
            continue
        rows.append(dict(year=int(c[len(YEAR):]), ret=float(mine), rank=int((vals > mine + 1e-12).sum()) + 1,
                         n=len(vals), top=float(vals.max()), median=float(vals.median())))
    return pd.DataFrame(rows, columns=["year", "ret", "rank", "n", "top", "median"])


def apply_combo(spec: dict, row: pd.Series, is_int: dict[str, bool]) -> dict:
    """把结果表中某一行的参数写回策略描述；is_int: {参数路径: 是否整数}"""
    s = spec
    for p, as_int in is_int.items():
        s = set_path(s, p, int(round(row[p])) if as_int else float(row[p]))
    return s
