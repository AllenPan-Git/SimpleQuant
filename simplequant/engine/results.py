"""
回测结果整理与绩效指标
指标统一基于"每日收盘权益"计算，这样日线和分钟线回测的夏普、波动率口径一致。
"""

from dataclasses import dataclass, field

import backtrader as bt
import numpy as np
import pandas as pd

TRADING_DAYS = 252


class EquityRecorder(bt.Analyzer):
    """逐 bar 记录总资产与现金（prenext 阶段也记录）"""

    def start(self):
        self.rows = []

    def next(self):
        self.rows.append((self.strategy.datetime.datetime(0), self.strategy.broker.getvalue(),
                          self.strategy.broker.getcash()))

    def get_analysis(self):
        df = pd.DataFrame(self.rows, columns=["datetime", "value", "cash"]).set_index("datetime")
        return df[~df.index.duplicated(keep="last")]


@dataclass
class BacktestResult:
    equity: pd.DataFrame                  # value, cash, benchmark, drawdown
    orders: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict
    logs: list = field(default_factory=list)
    prices: dict = field(default_factory=dict)   # 名称 -> 行情 DataFrame（画 K 线用）
    lot_too_big: list = field(default_factory=list)   # 因资金不足一手而没能买入的标的
    t0: list = field(default_factory=list)            # 按 T+0 交易的标的
    pending: list = field(default_factory=list)       # 最后一根 K 线上未成交的委托（下一个开盘执行）
    positions: list = field(default_factory=list)     # 结束时的持仓
    panels: dict = field(default_factory=dict)        # 标的名 -> 指标栏（交互式策略图用，见 engine/panels.py）


def compute_metrics(equity: pd.Series, benchmark: pd.Series, trades: pd.DataFrame,
                    initial_cash: float, risk_free: float = 0.02) -> dict:
    final = float(equity.iloc[-1])
    total_ret = final / initial_cash - 1
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    cagr = (final / initial_cash) ** (365.25 / days) - 1 if final > 0 else -1.0

    daily = equity.resample("D").last().dropna()
    rets = daily.pct_change().dropna()
    vol = rets.std() * np.sqrt(TRADING_DAYS) if len(rets) > 1 else 0.0
    excess = rets - risk_free / TRADING_DAYS
    sharpe = excess.mean() / rets.std() * np.sqrt(TRADING_DAYS) if len(rets) > 1 and rets.std() > 0 else np.nan

    drawdown = equity / equity.cummax() - 1
    max_dd = float(drawdown.min())

    n = len(trades)
    pnl = trades["pnl_net"] if n else pd.Series(dtype=float)
    wins = int((pnl > 0).sum())
    gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()

    bench_ret = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1)
    # 键名见 simplequant/i18n.py 中的 "m.<键>"
    return {
        "initial_cash": initial_cash,
        "final_value": final,
        "total_return": total_ret,
        "cagr": cagr,
        "volatility": float(vol),
        "sharpe": float(sharpe),
        "max_drawdown": max_dd,
        "calmar": cagr / abs(max_dd) if max_dd < 0 else np.nan,
        "trades": n,
        "win_rate": wins / n if n else np.nan,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else np.nan,
        "benchmark_return": bench_ret,
        "excess_return": total_ret - bench_ret,
    }


def build_benchmark(prices: dict[str, pd.DataFrame], index: pd.DatetimeIndex, initial_cash: float) -> pd.Series:
    """等权买入并持有所有标的，作为基准（现金分红模式下分红以现金留在账上，不再买回）"""
    normed = [_hold_value(df) for df in prices.values()]
    avg = pd.concat(normed, axis=1).ffill().mean(axis=1)
    return (avg.reindex(index, method="ffill").bfill() * initial_cash).rename("benchmark")


def _hold_value(df: pd.DataFrame) -> pd.Series:
    """买入 1 元并一直持有的市值"""
    if "div_keep" not in df.columns:
        return df["close"] / df["close"].iloc[0]
    keep, per_share = df["div_keep"].copy(), df["div_cash"].copy()
    keep.iloc[0], per_share.iloc[0] = 1.0, 0.0          # 第一根 K 线才买入，当天的除息与它无关
    held = keep.cumprod()
    cash = (held.shift(1, fill_value=1.0) * per_share).cumsum()
    return (held * df["close"] + cash) / df["close"].iloc[0]
