"""
滚动（walk-forward）参数优化

    窗口 1: [ 训练 ] → [ 测试 ]
    窗口 2:      [ 训练 ] → [ 测试 ]          滚动：训练期长度固定，整体向前推
    窗口 3:           [ 训练 ] → [ 测试 ]     锚定：训练期始终从最早的数据开始、逐渐变长

每个窗口只用训练期挑出最优参数，再看它在紧接着的测试期表现；把所有测试期拼起来，
得到一条完全样本外的资产曲线——这才是"用这套方法调参"在真实使用中的预期表现。

拼接方式（用户可选）：
- continuous 延续持仓（默认）：每个窗口用选出的参数从训练期开头一直跑到测试期结束，只取测试期内的
  每日收益逐段相乘。相当于"新参数一直在运行"，持仓状态自然延续；换参数时的调仓成本略有少算。
- restart 每段重新开始：每个测试期从空仓开始、用上一段结束时的资金接力；每段结束时按全部卖出
  扣除佣金、印花税和滑点。更保守，但趋势策略会错过测试期开始时已在进行的行情。
"""

STITCH_MODES = ("continuous", "restart")

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from ..strategies import resolve
from .optimize import optimize, best_row, apply_combo, build_grid
from .results import compute_metrics, build_benchmark
from .runner import run_backtest, BrokerConfig

MAX_BACKTESTS = 5000


@dataclass
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_windows(index: pd.DatetimeIndex, train_months: int, test_months: int, anchored: bool = False) -> list[Window]:
    """按自然月划分窗口，再对齐到实际有数据的交易日；测试期首尾相接、互不重叠"""
    index = pd.DatetimeIndex(index)
    first, last = index[0], index[-1]
    windows = []
    cut = first + pd.DateOffset(months=train_months)
    while True:
        test_start_i = index.searchsorted(cut)
        if test_start_i >= len(index):
            break
        nxt = cut + pd.DateOffset(months=test_months)
        test_end_i = index.searchsorted(nxt) - 1
        train_from = first if anchored else cut - pd.DateOffset(months=train_months)
        train_start_i = index.searchsorted(train_from)
        if test_end_i < test_start_i or test_start_i - train_start_i < 20:
            break
        windows.append(Window(index[train_start_i], index[test_start_i - 1], index[test_start_i],
                              index[min(test_end_i, len(index) - 1)]))
        if nxt > last:
            break
        cut = nxt
    return windows


@dataclass
class WalkForwardResult:
    windows: pd.DataFrame          # 每个窗口：日期、选出的参数、训练期指标、测试期指标
    equity: pd.DataFrame           # 样本外拼接：value / baseline（原参数）/ benchmark（买入持有）/ drawdown
    metrics: dict                  # 拼接后的样本外指标
    baseline_metrics: dict         # 原参数在同一样本外区间的指标
    params: pd.DataFrame           # 每个窗口选出的参数（画参数稳定性）
    wfe: float                     # 滚动效率 = 样本外年化 / 样本内平均年化
    notes: list = field(default_factory=list)


def _slice(prices: dict, start=None, end=None) -> dict:
    return {k: df.loc[start:end] for k, df in prices.items()}


def count_backtests(spec: dict, axes: dict, n_windows: int) -> int:
    return len(build_grid(spec, axes)) * n_windows + 2 * n_windows


def walk_forward(prices: dict[str, pd.DataFrame], spec: dict, axes: dict[str, list], is_int: dict[str, bool],
                 broker: BrokerConfig, metric: str = "sharpe", train_months: int = 24, test_months: int = 6,
                 anchored: bool = False, workers: int | None = None, progress=None,
                 stitch: str = "continuous") -> WalkForwardResult:
    """
    :param stitch: "continuous" 延续持仓 / "restart" 每段重新开始（见模块说明）
    :param progress: progress(当前窗口序号, 窗口总数, 窗口内已完成, 窗口内总数)
    """
    if stitch not in STITCH_MODES:
        raise ValueError(f"unknown stitch mode {stitch}")
    capital = broker.cash
    common = sorted(set.intersection(*[set(df.index) for df in prices.values()]))
    windows = make_windows(pd.DatetimeIndex(common), train_months, test_months, anchored)
    if not windows:
        raise ValueError("not enough data for one train+test window / 数据不够一个训练期加测试期")
    total = count_backtests(spec, axes, len(windows))
    if total > MAX_BACKTESTS:
        raise ValueError(f"too many backtests ({total} > {MAX_BACKTESTS}) / 回测次数过多，请缩小参数范围或加长测试期")

    rows, param_rows, segments, test_trades, notes = [], [], [], [], []
    for w_i, w in enumerate(windows, 1):
        train = _slice(prices, w.train_start, w.train_end)
        grid = optimize(train, spec, axes, broker, workers=workers,
                        progress=(lambda d, n, w_i=w_i: progress(w_i, len(windows), d, n)) if progress else None)
        try:
            best = best_row(grid, metric)
            chosen = apply_combo(spec, best, is_int)
            train_metric, train_cagr = float(best[metric]), float(best.get("cagr", np.nan))
        except ValueError:                 # 这个窗口所有组合都失败：沿用原参数
            chosen, train_metric, train_cagr = spec, np.nan, np.nan
            best = pd.Series({p: np.nan for p in axes})
            notes.append(f"window {w_i}: all combinations failed, original parameters used")

        if stitch == "continuous":
            # 用选出的参数从训练期开头跑到测试期结束，只取测试期（含测试期前一天作为收益起点）
            run = run_backtest(_slice(prices, w.train_start, w.test_end), *resolve(chosen), broker)
            test_eq = run.equity["value"].loc[w.train_end:w.test_end]
            seg = test_eq.pct_change().dropna()
        else:
            # 测试期从空仓开始（之前的数据只预热指标），资金接力；结束时按全部卖出扣成本
            seg_broker = replace(broker, cash=capital)
            run = run_backtest(_slice(prices, w.train_start, w.test_end), *resolve(chosen), seg_broker,
                               trade_start=w.test_start)
            eq = run.equity
            values = pd.concat([pd.Series([capital], index=[w.train_end]), eq["value"]])
            seg = values.pct_change().dropna()
            # 每段（包括最后一段）结束都按全部卖出扣成本，保证结果与之后是否还有数据无关
            held = float(eq["value"].iloc[-1] - eq["cash"].iloc[-1])
            exit_cost = held * (broker.commission + broker.stamp_duty + broker.slippage)
            seg.iloc[-1] = (1 + seg.iloc[-1]) * (1 - exit_cost / eq["value"].iloc[-1]) - 1
            capital = capital * (1 + seg).prod()
        segments.append(seg)
        trades = run.trades
        if len(trades):
            test_trades.append(trades[pd.to_datetime(trades["close_time"]) >= w.test_start])
        seg_value = (1 + seg).prod() - 1
        years = max((w.test_end - w.test_start).days / 365.25, 1 / 365.25)
        rows.append({"window": w_i, "train_start": w.train_start, "train_end": w.train_end,
                     "test_start": w.test_start, "test_end": w.test_end,
                     **{p: best[p] for p in axes}, "train_metric": train_metric, "train_cagr": train_cagr,
                     "test_return": seg_value, "test_cagr": (1 + seg_value) ** (1 / years) - 1,
                     "test_max_drawdown": float(((1 + seg).cumprod() / (1 + seg).cumprod().cummax() - 1).min())})
        param_rows.append({"test_start": w.test_start, **{p: best[p] for p in axes}})

    # 拼接样本外资产曲线
    returns = pd.concat(segments)
    returns = returns[~returns.index.duplicated(keep="first")].sort_index()
    oos_start = windows[0].test_start
    value = broker.cash * (1 + returns).cumprod()
    value = pd.concat([pd.Series([broker.cash], index=[windows[0].train_end]), value])

    # 参照 1：原参数从数据开头一直运行，取样本外区间的收益（与"延续持仓"口径一致）
    base = run_backtest(_slice(prices, None, windows[-1].test_end), *resolve(spec), broker)
    base_ret = base.equity["value"].pct_change().reindex(value.index).fillna(0)
    base_ret.iloc[0] = 0.0
    baseline = broker.cash * (1 + base_ret).cumprod()
    # 参照 2：买入持有
    bench = build_benchmark(_slice(prices, windows[0].train_end, windows[-1].test_end), value.index, broker.cash)

    equity = pd.DataFrame({"value": value, "baseline": baseline, "benchmark": bench})
    equity["drawdown"] = equity["value"] / equity["value"].cummax() - 1
    trades_all = pd.concat(test_trades) if test_trades else pd.DataFrame(columns=base.trades.columns)
    metrics = compute_metrics(equity["value"], equity["benchmark"], trades_all, broker.cash, broker.risk_free)
    base_metrics = compute_metrics(equity["baseline"], equity["benchmark"],
                                   base.trades[pd.to_datetime(base.trades["close_time"]) >= oos_start]
                                   if len(base.trades) else base.trades, broker.cash, broker.risk_free)
    table = pd.DataFrame(rows)
    is_cagr = table["train_cagr"].mean()
    wfe = metrics["cagr"] / is_cagr if is_cagr and is_cagr > 0 else np.nan
    return WalkForwardResult(windows=table, equity=equity, metrics=metrics, baseline_metrics=base_metrics,
                             params=pd.DataFrame(param_rows), wfe=float(wfe), notes=notes)
