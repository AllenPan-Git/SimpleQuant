"""
因子研究：IC 分析与分层回测（pandas 向量化）

时间约定（与选股回测一致，避免用到未来数据）：
    T 日收盘算出因子 → T+1 日开盘买入 → 持有 h 个交易日 → T+1+h 日开盘卖出
    未来收益 = open[T+1+h] / open[T+1] - 1（后复权开盘价）
采样：每 h 个交易日取一个截面，使相邻两期的持有区间不重叠。
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


def forward_returns(panel, horizon: int) -> pd.DataFrame:
    o = panel["open"]
    return o.shift(-(1 + horizon)) / o.shift(-1) - 1


def sample_dates(calendar: pd.DatetimeIndex, horizon: int, warmup: int = 0) -> pd.DatetimeIndex:
    usable = calendar[warmup: len(calendar) - horizon - 1]
    return usable[::horizon]


def rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 10) -> pd.Series:
    """每行的 Spearman 秩相关"""
    both = factor.notna() & fwd.notna()
    f = factor.where(both).rank(axis=1)
    r = fwd.where(both).rank(axis=1)
    fc, rc = f.sub(f.mean(axis=1), axis=0), r.sub(r.mean(axis=1), axis=0)
    num = (fc * rc).sum(axis=1)
    den = np.sqrt((fc ** 2).sum(axis=1) * (rc ** 2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    return ic.where(both.sum(axis=1) >= min_names)


@dataclass
class ICSummary:
    mean: float
    std: float
    ir: float            # 每期 IC 均值 / 标准差
    ir_annual: float     # 年化：× sqrt(252 / h)
    t_stat: float
    positive: float      # IC > 0 的比例
    n: int


def summarize_ic(ic: pd.Series, horizon: int) -> ICSummary:
    ic = ic.dropna()
    n = len(ic)
    mean, std = (float(ic.mean()), float(ic.std())) if n else (np.nan, np.nan)
    ir = mean / std if n > 1 and std > 0 else np.nan
    return ICSummary(mean=mean, std=std, ir=ir, ir_annual=ir * np.sqrt(252 / horizon) if ir == ir else np.nan,
                     t_stat=ir * np.sqrt(n) if ir == ir else np.nan, positive=float((ic > 0).mean()) if n else np.nan,
                     n=n)


def quantile_returns(factor: pd.DataFrame, fwd: pd.DataFrame, groups: int = 5) -> pd.DataFrame:
    """
    每期把股票按因子值从小到大分成 groups 组（第 1 组最小，第 groups 组最大），返回各组的平均收益。
    列：1..groups；索引：采样日期
    """
    both = factor.notna() & fwd.notna()
    pct = factor.where(both).rank(axis=1, pct=True)
    grp = np.ceil(pct * groups).clip(1, groups)
    out = {}
    for g in range(1, groups + 1):
        out[g] = fwd.where(grp == g).mean(axis=1)
    return pd.DataFrame(out)


@dataclass
class FactorReport:
    ic: pd.Series                 # 每期 IC
    summary: ICSummary
    group_returns: pd.DataFrame   # 每期各组收益（列 1..G）
    cumulative: pd.DataFrame      # 各组 + 多空的累计净值
    annual: pd.Series             # 各组年化收益
    monotonicity: float           # 组号与组年化收益的秩相关（±1 为完全单调）
    top_turnover: float           # 因子值最大组每期平均换手比例


def analyze(panel, factor: pd.DataFrame, mask: pd.DataFrame, horizon: int = 20, groups: int = 5,
            warmup: int = 0, start=None) -> FactorReport:
    """start：只统计这天之后的截面（之前的数据只用来预热因子）"""
    fwd = forward_returns(panel, horizon)
    cal = panel.calendar if start is None else panel.calendar[panel.calendar >= pd.Timestamp(start)]
    dates = sample_dates(cal, horizon, warmup)
    f = factor.where(mask).loc[dates]
    r = fwd.loc[dates]
    ic = rank_ic(f, r)
    gr = quantile_returns(f, r, groups).dropna(how="all")
    gr["long_short"] = gr[groups] - gr[1]
    cum = (1 + gr.fillna(0)).cumprod()
    periods_per_year = 252 / horizon
    annual = cum.iloc[-1] ** (periods_per_year / max(len(cum), 1)) - 1 if len(cum) else pd.Series(dtype=float)
    g_ann = annual.drop("long_short") if len(annual) else annual
    # 秩相关 = 对秩做 Pearson（不依赖 scipy）
    mono = (float(pd.Series(range(1, groups + 1), dtype=float).corr(g_ann.reset_index(drop=True).rank()))
            if len(g_ann) else np.nan)

    # 顶组换手：相邻两期顶组成分的变化比例
    pct = f.rank(axis=1, pct=True)
    top = pct > (groups - 1) / groups
    changes = []
    prev = None
    for d in top.index:
        cur = set(top.columns[top.loc[d].values])
        if prev is not None and prev:
            changes.append(1 - len(cur & prev) / len(prev))
        prev = cur
    return FactorReport(ic=ic, summary=summarize_ic(ic, horizon), group_returns=gr, cumulative=cum, annual=annual,
                        monotonicity=mono, top_turnover=float(np.mean(changes)) if changes else np.nan)
