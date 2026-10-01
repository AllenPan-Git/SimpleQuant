"""
Backtrader 里没有现成实现（或实现与国内习惯不同）的因子
"""

import backtrader as bt


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
