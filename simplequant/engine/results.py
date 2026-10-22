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
    trade_start: pd.Timestamp | None = None           # 有预热段时的交易起点（prices / panels 已从这天截取）
    warm_prices: dict = field(default_factory=dict)   # 有预热段时：含预热段的完整行情（原生图重新运行用）


def flows_on_bars(index: pd.DatetimeIndex, flows: pd.Series) -> pd.Series:
    """追加资金按日期落到当天（或之后）的第一根 K 线上；区间外的不算"""
    out = pd.Series(0.0, index=index)
    flows = flows[(flows.index >= index[0].normalize()) & (flows.index <= index[-1])]
    for day, amount in flows.items():
        pos = index.searchsorted(day)
        if pos < len(index):
            out.iloc[pos] += amount
    return out


def unit_value(equity: pd.Series, flows: pd.Series | None) -> pd.Series:
    """
    扣除追加资金后的单位净值（第一根 K 线 = 1，即时间加权收益）：追加的钱在开盘前到账、计入当天的总资产，
    所以当天收益 = (总资产 − 当天追加) / 前一天总资产 − 1。没有追加资金时就是 equity / equity[0]
    """
    if flows is None or not len(flows):
        return equity / equity.iloc[0]
    f = flows_on_bars(equity.index, flows)
    rets = (equity - f) / equity.shift(1) - 1
    return (1 + rets.fillna(0.0)).cumprod()


def nav_of(equity: pd.DataFrame) -> pd.Series:
    """回测资产曲线的单位净值，乘第一天的总资产（与 value 同量级）：定投时扣除追加资金（按 invested 列），否则就是 value"""
    if "invested" not in equity:
        return equity["value"]
    f = equity["invested"].diff().fillna(0.0)
    return unit_value(equity["value"], f[f != 0]) * equity["value"].iloc[0]


def apply_flows(nav: pd.Series, flows: pd.Series, initial: float) -> pd.Series:
    """unit_value 的逆运算：按单位净值的每日收益和同样的追加资金还原总资产（滚动优化拼接样本外曲线用）"""
    f = flows_on_bars(nav.index, flows).to_numpy()
    rets = nav.pct_change().fillna(0.0).to_numpy()
    out, v = np.empty(len(nav)), float(initial)
    for i in range(len(nav)):
        v = v * (1 + rets[i]) + f[i]
        out[i] = v
    return pd.Series(out, index=nav.index)


def xirr(dates, amounts) -> float:
    """
    资金加权年化收益率：使 Σ 现金流 / (1 + r)^(距第一笔的年数) = 0 的 r（投入为负、期末市值为正）。
    二分法求解；无解（如全部同号）时返回 NaN
    """
    t = np.array([(pd.Timestamp(d) - pd.Timestamp(dates[0])).days / 365.25 for d in dates])
    a = np.asarray(amounts, dtype=float)
    if not (a < 0).any() or not (a > 0).any() or t[-1] <= 0:
        return np.nan
    npv = lambda r: float((a / (1 + r) ** t).sum())
    lo, hi = -0.9999, 1.0
    while npv(hi) > 0 and hi < 1e6:        # 投入在前、市值在后：npv 随 r 递减
        hi *= 2
    if npv(lo) * npv(hi) > 0:
        return np.nan
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def compute_metrics(equity: pd.Series, benchmark: pd.Series, trades: pd.DataFrame,
                    initial_cash: float, risk_free: float = 0.02, flows: pd.Series | None = None) -> dict:
    """
    flows：定投追加的资金（日期 -> 金额，不含初始资金）。有追加时：
    - 总收益 = 期末市值 / 累计投入 − 1，基准同口径（同样的现金流买入持有）；另给资金加权年化 xirr
    - 年化、波动、夏普、回撤按扣除追加资金后的单位净值算（否则追加的钱会被当成收益、掩盖回撤）
    """
    if flows is not None and len(flows):
        flows = flows[(flows.index >= equity.index[0].normalize()) & (flows.index <= equity.index[-1])]
    if flows is not None and len(flows):
        return _metrics_with_flows(equity, benchmark, trades, initial_cash, risk_free, flows)
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


def _metrics_with_flows(equity, benchmark, trades, initial_cash, risk_free, flows) -> dict:
    nav = unit_value(equity, flows) * initial_cash          # 按单位净值算年化、波动、夏普、回撤
    m = compute_metrics(nav, benchmark, trades, initial_cash, risk_free)
    invested = initial_cash + float(flows.sum())
    final, bench_final = float(equity.iloc[-1]), float(benchmark.iloc[-1])
    dates = [equity.index[0], *flows.index, equity.index[-1]]
    paid = [-initial_cash, *(-flows.values)]
    total, bench_ret = final / invested - 1, bench_final / invested - 1
    m.update({
        "final_value": final,
        "total_return": total,
        "benchmark_return": bench_ret,
        "excess_return": total - bench_ret,
        "invested": invested,                  # 累计投入（含初始资金）
        "contributions": len(flows),           # 追加了几次
        "profit": final - invested,
        "xirr": xirr(dates, paid + [final]),
        "benchmark_xirr": xirr(dates, paid + [bench_final]),
        "twr": float(nav.iloc[-1] / initial_cash - 1),    # 时间加权收益（不受投入节奏影响）
    })
    return m


def build_benchmark(prices: dict[str, pd.DataFrame], index: pd.DatetimeIndex, initial_cash: float,
                    flows: pd.Series | None = None) -> pd.Series:
    """
    等权买入并持有所有标的，作为基准（现金分红模式下分红以现金留在账上，不再买回）。
    flows：定投追加的资金，每笔在到账那根 K 线按收盘价买入同样的组合（与策略同样的现金流）
    """
    normed = [_hold_value(df) for df in prices.values()]
    avg = pd.concat(normed, axis=1).ffill().mean(axis=1)
    curve = avg.reindex(index, method="ffill").bfill()
    if flows is None or not len(flows):
        return (curve * initial_cash).rename("benchmark")
    units = flows_on_bars(index, flows) / curve
    units.iloc[0] += initial_cash               # 初始资金的份数与无追加时一致（curve × initial_cash）
    return (units.cumsum() * curve).rename("benchmark")


def _hold_value(df: pd.DataFrame) -> pd.Series:
    """买入 1 元并一直持有的市值"""
    if "div_keep" not in df.columns:
        return df["close"] / df["close"].iloc[0]
    keep, per_share = df["div_keep"].copy(), df["div_cash"].copy()
    keep.iloc[0], per_share.iloc[0] = 1.0, 0.0          # 第一根 K 线才买入，当天的除息与它无关
    held = keep.cumprod()
    cash = (held.shift(1, fill_value=1.0) * per_share).cumsum()
    return (held * df["close"] + cash) / df["close"].iloc[0]
