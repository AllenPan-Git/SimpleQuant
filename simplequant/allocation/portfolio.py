"""
组合回测：按权重合成各候选的日收益，定期或按偏离阈值再平衡

做法：各成分的持仓市值每日按各自收益率变动；到再平衡日收盘时按目标权重调回，
换手金额 × 单边成本率 从组合资产中扣除。各成分自身的交易成本已包含在其收益序列中。
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..engine.results import compute_metrics, TRADING_DAYS
from .sleeves import risk_stats

REBALANCE = ("monthly", "quarterly", "yearly", "threshold", "none")
THRESHOLD = 0.05          # 按偏离再平衡：任一成分权重偏离目标超过 5 个百分点
COST = 0.0005             # 再平衡单边成本率（佣金 + 滑点的近似）


@dataclass
class PortfolioResult:
    equity: pd.DataFrame          # value / benchmark（等权配置）/ drawdown
    weights: pd.DataFrame         # 每日收盘（再平衡后）的实际权重
    metrics: dict
    stats: dict                   # 组合的 cagr / volatility / max_drawdown / risk（风险等级）
    contrib: pd.DataFrame         # 每个成分：目标权重、收益贡献、风险贡献、自身指标
    corr: pd.DataFrame
    n_rebalances: int
    cost: float                   # 再平衡成本合计（元）
    notes: list = field(default_factory=list)


def common_returns(series: dict[str, pd.Series]) -> pd.DataFrame:
    """各成分收益率按共同区间对齐：从最晚开始的成分起，到最早结束的成分止；中间某成分停牌的日子收益为 0"""
    if not series:
        raise ValueError("no sleeves / 没有配置成分")
    start = max(s.index[0] for s in series.values())
    end = min(s.index[-1] for s in series.values())
    if start >= end:
        raise ValueError("sleeves have no overlapping period / 各成分没有共同的数据区间")
    df = pd.DataFrame(series).loc[start:end]
    # 现金类按完整日历计息，其他成分按各自交易日；以非现金成分的交易日为准
    return df.fillna(0.0)


def _period_key(idx: pd.DatetimeIndex, rule: str) -> np.ndarray:
    if rule == "monthly":
        return (idx.year * 12 + idx.month).to_numpy()
    if rule == "quarterly":
        return (idx.year * 4 + (idx.month - 1) // 3).to_numpy()
    return idx.year.to_numpy()


def _simulate(R: np.ndarray, w: np.ndarray, idx: pd.DatetimeIndex, rebalance: str, cost: float, cash: float):
    n, k = R.shape
    hold = cash * w
    values, weights, pnl = np.empty(n), np.empty((n, k)), np.zeros(k)
    reb_days, total_cost = 0, 0.0
    period = _period_key(idx, rebalance) if rebalance in ("monthly", "quarterly", "yearly") else None
    for i in range(n):
        gain = hold * R[i]
        pnl += gain
        hold = hold + gain
        v = hold.sum()
        due = False
        if i < n - 1 and v > 0:
            if period is not None:
                due = period[i + 1] != period[i]          # 本期最后一个交易日收盘再平衡
            elif rebalance == "threshold":
                due = np.abs(hold / v - w).max() > THRESHOLD
        if due:
            target = v * w
            c = np.abs(target - hold).sum() * cost
            total_cost += c
            v -= c
            hold = v * w
            reb_days += 1
        values[i] = v
        weights[i] = hold / v if v > 0 else w
    return values, weights, pnl, reb_days, total_cost


def backtest(returns: pd.DataFrame, weights: dict[str, float], rebalance: str = "quarterly",
             cost: float = COST, cash: float = 100_000.0, risk_free: float = 0.02) -> PortfolioResult:
    """
    :param returns: common_returns() 的结果，列为成分 id
    :param weights: {成分 id: 目标权重}，会归一化；权重为 0 的成分不参与
    """
    if rebalance not in REBALANCE:
        raise ValueError(f"unknown rebalance rule {rebalance}")
    cols = [c for c in returns.columns if weights.get(c, 0) > 0]
    if not cols:
        raise ValueError("all weights are zero / 全部权重为零")
    w = np.array([weights[c] for c in cols], dtype=float)
    w = w / w.sum()
    R = returns[cols].to_numpy()
    idx = returns.index
    values, wh, pnl, n_reb, total_cost = _simulate(R, w, idx, rebalance, cost, cash)

    # 参照：全部候选等权、同样的再平衡规则
    ew = np.full(returns.shape[1], 1 / returns.shape[1])
    bench, *_ = _simulate(returns.to_numpy(), ew, idx, rebalance, cost, cash)

    # 起点：第一天收益之前的那一刻
    start = idx[0] - pd.Timedelta(days=1)
    value = pd.Series(np.r_[cash, values], index=idx.insert(0, start))
    benchmark = pd.Series(np.r_[cash, bench], index=value.index)
    equity = pd.DataFrame({"value": value, "benchmark": benchmark})
    equity["drawdown"] = equity["value"] / equity["value"].cummax() - 1
    metrics = compute_metrics(equity["value"], equity["benchmark"], pd.DataFrame(columns=["pnl_net"]), cash, risk_free)
    port_ret = equity["value"].pct_change().dropna()

    cov = returns[cols].cov().to_numpy() * TRADING_DAYS
    port_var = float(w @ cov @ w)
    risk_contrib = w * (cov @ w) / port_var if port_var > 0 else np.full(len(w), np.nan)
    rows = []
    for i, c in enumerate(cols):
        st = risk_stats(returns[c])
        rows.append({"id": c, "weight": w[i], "return_contrib": pnl[i] / cash, "risk_contrib": float(risk_contrib[i]), **st})
    return PortfolioResult(equity=equity, weights=pd.DataFrame(wh, index=idx, columns=cols), metrics=metrics,
                           stats=risk_stats(port_ret), contrib=pd.DataFrame(rows), corr=returns[cols].corr(),
                           n_rebalances=n_reb, cost=total_cost)
