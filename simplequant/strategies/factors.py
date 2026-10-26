"""
Backtrader 里没有现成实现（或实现与国内习惯不同）的因子
"""

import backtrader as bt
import numpy as np


class KDJ(bt.Indicator):
    """
    国内常用的 KDJ：
        RSV = (收盘 - N日最低) / (N日最高 - N日最低) × 100
        K = (m1-1)/m1 × K昨 + 1/m1 × RSV，D 同理对 K 平滑，J = 3K - 2D
    K、D 初值取 50（与通达信、同花顺一致）
    """
    lines = ("k", "d", "j")
    params = (("period", 9), ("m1", 3), ("m2", 3))

    def __init__(self):
        hi = bt.ind.Highest(self.data.high, period=self.p.period)
        lo = bt.ind.Lowest(self.data.low, period=self.p.period)
        self.rsv = bt.DivByZero(self.data.close - lo, hi - lo, zero=0.5) * 100
        self.addminperiod(self.p.period)

    def nextstart(self):
        self.lines.k[0] = (50 * (self.p.m1 - 1) + self.rsv[0]) / self.p.m1
        self.lines.d[0] = (50 * (self.p.m2 - 1) + self.lines.k[0]) / self.p.m2
        self.lines.j[0] = 3 * self.lines.k[0] - 2 * self.lines.d[0]

    def next(self):
        self.lines.k[0] = (self.lines.k[-1] * (self.p.m1 - 1) + self.rsv[0]) / self.p.m1
        self.lines.d[0] = (self.lines.d[-1] * (self.p.m2 - 1) + self.lines.k[0]) / self.p.m2
        self.lines.j[0] = 3 * self.lines.k[0] - 2 * self.lines.d[0]


class OBV(bt.Indicator):
    """能量潮：收盘上涨累加成交量，下跌累减，持平不变"""
    lines = ("obv",)

    def nextstart(self):
        self.lines.obv[0] = 0.0

    def next(self):
        change = self.data.close[0] - self.data.close[-1]
        step = self.data.volume[0] if change > 0 else -self.data.volume[0] if change < 0 else 0.0
        self.lines.obv[0] = self.lines.obv[-1] + step


class EquityRiskPremium(bt.Indicator):
    """股债利差（%）= 100 / 市盈率 − 10 年期国债收益率；市盈率不为正或任一项为空时为空"""
    lines = ("erp",)

    def next(self):
        pe, rate = self.data0[0], self.data1[0]
        self.lines.erp[0] = 100 / pe - rate if pe > 0 and rate == rate else float("nan")


class Percentile(bt.Indicator):
    """
    历史百分位（0~100）：当前值在过去 window 根 K 线（含当前）里排在什么位置，
    = (比它小的个数 + 相等个数 / 2) / 有效个数 × 100。空值不计；有效值不到 min_count 个时为空。
    估值百分位用：市盈率 / 市净率处在过去几年的什么水平
    """
    lines = ("pct",)
    params = (("window", 1220), ("min_count", 244))

    def next(self):
        cur = self.data[0]
        if cur != cur:                                   # 当前值为空
            self.lines.pct[0] = float("nan")
            return
        vals = np.asarray(self.data.get(size=min(len(self.data), self.p.window)), dtype=float)
        vals = vals[~np.isnan(vals)]
        if len(vals) < self.p.min_count:
            self.lines.pct[0] = float("nan")
            return
        self.lines.pct[0] = ((vals < cur).sum() + (vals == cur).sum() / 2) / len(vals) * 100
