"""
批量回测：同一个策略在多个标的上分别回测（每个标的使用全部资金），比较结果的分布

与多标的组合回测（资金平均分配）不同，这里回答的是「这条规则在多少个标的上有效」：
跑赢买入持有的比例、收益的中位数与最差情形。单个标的的结果可能是运气，多个标的的分布更能说明规则本身。
每个标的使用各自有数据的区间（与所选区间取交集），上市较晚的股票不会拖短其他标的的区间。
"""

from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pandas as pd

from ..strategies import resolve
from ..strategies.code_strategy import explain_error
from .optimize import default_workers
from .runner import run_backtest, BrokerConfig

MIN_BARS = 30            # 少于这么多根 K 线的标的不回测
MAX_ASSETS = 1000

COLUMNS = ["symbol", "start", "end", "total_return", "benchmark_return", "excess_return", "cagr",
           "max_drawdown", "benchmark_max_drawdown", "sharpe", "trades", "win_rate", "holding",
           "lot_too_big", "limit_blocked", "error"]


def _one(name: str, df: pd.DataFrame, spec: dict, broker: BrokerConfig) -> dict:
    row = {"symbol": name, "start": df.index[0] if len(df) else pd.NaT, "end": df.index[-1] if len(df) else pd.NaT}
    if len(df) < MIN_BARS:
        return {**row, "error": f"too few bars ({len(df)}) / K 线不足 {MIN_BARS} 根"}
    try:
        cls, params = resolve(spec)
        res = run_backtest({name: df}, cls, params, broker)
    except Exception as e:  # noqa: BLE001 - 单个标的失败不影响整体
        return {**row, "error": explain_error(e)}
    m, bench = res.metrics, res.equity["benchmark"]
    return {**row, **{k: m.get(k) for k in ("total_return", "benchmark_return", "excess_return", "cagr",
                                            "max_drawdown", "sharpe", "trades", "win_rate", "limit_blocked")},
            "benchmark_max_drawdown": float((bench / bench.cummax() - 1).min()),
            "holding": bool(res.positions), "lot_too_big": bool(res.lot_too_big), "error": None}


def _task(args):
    return _one(*args)


def run_batch(prices: dict[str, pd.DataFrame], spec: dict, broker: BrokerConfig | None = None,
              workers: int | None = None, progress=None) -> pd.DataFrame:
    """
    :param prices: {名称: 行情}，已按所选区间截好
    :param progress: 回调 progress(已完成数, 总数)
    :return: 每个标的一行，列见 COLUMNS；出错的标的只有 symbol / start / end / error
    """
    if not prices:
        raise ValueError("Select data for at least one asset / 请至少选择一个标的的数据")
    if len(prices) > MAX_ASSETS:
        raise ValueError(f"too many assets ({len(prices)} > {MAX_ASSETS}) / 标的过多")
    broker = broker or BrokerConfig()
    tasks = [(name, df, spec, broker) for name, df in prices.items()]
    n, rows = len(tasks), [None] * len(tasks)
    workers = workers or default_workers()

    def sequential():
        for i, task in enumerate(tasks):
            rows[i] = _task(task)
            if progress:
                progress(i + 1, n)

    if workers <= 1 or n < 4:
        sequential()
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_task, task): i for i, task in enumerate(tasks)}
                for done, f in enumerate(as_completed(futures), 1):
                    rows[futures[f]] = f.result()
                    if progress:
                        progress(done, n)
        except (BrokenProcessPool, OSError):
            sequential()
    return pd.DataFrame(rows).reindex(columns=COLUMNS)


def summarize(df: pd.DataFrame) -> dict:
    """结果分布的摘要；比例均为 0~1，没有成功的标的时只有 n / failed"""
    ok = df[df["error"].isna()] if "error" in df else df
    out = {"n": len(df), "failed": len(df) - len(ok)}
    if ok.empty:
        return out
    ret, bench = ok["total_return"].astype(float), ok["benchmark_return"].astype(float)
    trades = ok["trades"].fillna(0).astype(int)
    out.update(
        ok=len(ok),
        beat=int((ret > bench).sum()), beat_share=float((ret > bench).mean()),
        profit=int((ret > 0).sum()), profit_share=float((ret > 0).mean()),
        bench_profit_share=float((bench > 0).mean()),
        median_return=float(ret.median()), median_benchmark=float(bench.median()),
        median_excess=float((ret - bench).median()),
        worst_decile=float(np.quantile(ret, 0.1)), best_decile=float(np.quantile(ret, 0.9)),
        median_drawdown=float(ok["max_drawdown"].astype(float).median()),
        median_benchmark_drawdown=float(ok["benchmark_max_drawdown"].astype(float).median()),
        smaller_drawdown_share=float((ok["max_drawdown"].astype(float)
                                      > ok["benchmark_max_drawdown"].astype(float)).mean()),
        no_trades=int(((trades == 0) & ~ok["holding"].fillna(False).astype(bool)).sum()),     # 没有平仓、期末也没有持仓
        holding=int(ok["holding"].fillna(False).astype(bool).sum()),
        lot_too_big=int(ok["lot_too_big"].fillna(False).astype(bool).sum()),
        median_trades=float(trades.median()),
    )
    return out
