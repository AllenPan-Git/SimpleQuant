"""
内置策略模板
单标的类模板在选了多个标的时，对每个标的独立运行，资金平均分配。
"""

import backtrader as bt

from ..engine.base_strategy import BaseStrategy
from ..i18n import L
from .registry import Param, Template, register

PCT = Param("position_pct", L("仓位比例(%)", "Position (%)"), 95, 10, 100, 5,
            L("满足买入条件时投入的资金比例", "Share of capital invested when the buy condition is met"))


class _PerAsset(BaseStrategy):
    """单标的策略的公共部分：每个标的分到 position_pct / N 的资金"""
    params = (("position_pct", 95),)

    @property
    def slot_pct(self) -> float:
        return self.p.position_pct / 100 / len(self.datas)


class BuyHold(_PerAsset):
    def next(self):
        for d in self.datas:
            if self.getposition(d).size == 0:
                self.order_target_pct(d, self.slot_pct, ("reason.hold", {}))


class SmaCross(_PerAsset):
    """双均线：快线上穿慢线买入，下穿卖出"""
    params = (("fast", 5), ("slow", 20))

    def __init__(self):
        super().__init__()
        self.cross = {d: bt.ind.CrossOver(bt.ind.SMA(d.close, period=int(self.p.fast)),
                                          bt.ind.SMA(d.close, period=int(self.p.slow))) for d in self.datas}
        for c in self.cross.values():
            c.plotinfo.plot = False          # +1/-1 的交叉信号不画在图上

    def next(self):
        args = {"fast": self.p.fast, "slow": self.p.slow}
        for d in self.datas:
            if self.cross[d][0] > 0:
                self.order_target_pct(d, self.slot_pct, ("reason.ma_up", args))
            elif self.cross[d][0] < 0:
                self.order_target_pct(d, 0, ("reason.ma_down", args))


class RsiReversion(_PerAsset):
    """RSI 均值回归：RSI 低于超卖线买入，高于超买线卖出"""
    params = (("period", 14), ("low", 30), ("high", 70))

    def __init__(self):
        super().__init__()
        self.rsi = {d: bt.ind.RSI(d.close, period=int(self.p.period), safediv=True) for d in self.datas}

    def next(self):
        for d in self.datas:
            r = self.rsi[d][0]
            if r < self.p.low:
                self.order_target_pct(d, self.slot_pct, ("reason.rsi_low", {"v": r}))
            elif r > self.p.high:
                self.order_target_pct(d, 0, ("reason.rsi_high", {"v": r}))


class BollBreakout(_PerAsset):
    """布林带突破：收盘价突破上轨买入，跌破中轨卖出"""
    params = (("period", 20), ("devfactor", 2.0))

    def __init__(self):
        super().__init__()
        self.boll = {d: bt.ind.BollingerBands(d.close, period=int(self.p.period), devfactor=self.p.devfactor)
                     for d in self.datas}

    def next(self):
        for d in self.datas:
            b = self.boll[d]
            if d.close[0] > b.top[0]:
                self.order_target_pct(d, self.slot_pct, ("reason.boll_up", {}))
            elif d.close[0] < b.mid[0]:
                self.order_target_pct(d, 0, ("reason.boll_mid", {}))


class MomentumRotation(BaseStrategy):
    """
    动量轮动
    每隔 rebalance_bars 根 K 线，持有过去 period 根 K 线涨幅最高的标的；
    若所有标的涨幅都 <= 0 则空仓。换仓时先卖出，下一根 K 线资金到账后再买入。
    """
    params = (("period", 20), ("rebalance_bars", 5), ("position_pct", 95))

    def __init__(self):
        super().__init__()
        self.mom = {d: bt.ind.PercentChange(d.close, period=int(self.p.period)) for d in self.datas}
        self.counter = 0
        self.pending_target = None

    def next(self):
        pct = self.p.position_pct / 100
        if self.pending_target is not None and not any(self.has_pending(d) for d in self.datas):
            self.order_target_pct(self.pending_target, pct, ("reason.rot_buy", {}))
            self.pending_target = None

        self.counter += 1
        if self.counter % int(self.p.rebalance_bars) != 0:
            return

        scores = {d: self.mom[d][0] for d in self.datas}
        best = max(scores, key=scores.get)
        holding = next((d for d in self.datas if self.getposition(d).size > 0), None)

        if scores[best] <= 0:
            if holding is not None:
                self.order_target_pct(holding, 0, ("reason.rot_all_neg", {}))
            self.pending_target = None
            return

        if holding is best:
            return
        if holding is not None:
            if self.order_target_pct(holding, 0, ("reason.rot_switch", {"name": best._name})) is not None:
                self.pending_target = best
        else:
            self.order_target_pct(best, pct, ("reason.rot_best", {"v": scores[best] * 100}))


register(Template(
    "buy_hold", L("买入持有", "Buy & hold"),
    L("第一根 K 线建仓后一直持有，一般用作对比基准。", "Buys on the first bar and holds. Mostly used as a baseline."),
    BuyHold, [PCT]))
register(Template(
    "sma_cross", L("双均线交叉", "SMA crossover"),
    L("短期均线上穿长期均线时买入，下穿时卖出。经典的趋势跟踪策略。",
      "Buy when the short moving average crosses above the long one; sell when it crosses below. Classic trend following."),
    SmaCross, [
        Param("fast", L("短期均线周期", "Short MA period"), 5, 2, 120, 1),
        Param("slow", L("长期均线周期", "Long MA period"), 20, 5, 250, 1),
        PCT,
    ], constraint=lambda p: p["fast"] < p["slow"]))
register(Template(
    "rsi_reversion", L("RSI 超买超卖", "RSI mean reversion"),
    L("RSI 低于超卖线时买入，高于超买线时卖出。适合震荡行情。",
      "Buy when RSI drops below the oversold line; sell above the overbought line. Suits range-bound markets."),
    RsiReversion, [
        Param("period", L("RSI 周期", "RSI period"), 14, 2, 60, 1),
        Param("low", L("超卖线（低于则买入）", "Oversold (buy below)"), 30, 5, 50, 1),
        Param("high", L("超买线（高于则卖出）", "Overbought (sell above)"), 70, 50, 95, 1),
        PCT,
    ], constraint=lambda p: p["low"] < p["high"]))
register(Template(
    "boll_breakout", L("布林带突破", "Bollinger breakout"),
    L("收盘价突破布林带上轨时买入，跌破中轨时卖出。",
      "Buy when the close breaks above the upper band; sell when it falls below the middle band."),
    BollBreakout, [
        Param("period", L("布林带周期", "Band period"), 20, 5, 120, 1),
        Param("devfactor", L("标准差倍数", "Std dev multiplier"), 2.0, 0.5, 4.0, 0.1),
        PCT,
    ]))
register(Template(
    "momentum_rotation", L("动量轮动", "Momentum rotation"),
    L("定期比较多个标的的近期涨幅，只持有最强的一个；全部下跌时空仓。至少需要 2 个标的。",
      "Periodically holds only the asset with the strongest recent return; goes to cash when all are falling. Needs at least 2 assets."),
    MomentumRotation, [
        Param("period", L("动量回看周期（K线数）", "Lookback (bars)"), 20, 2, 250, 1),
        Param("rebalance_bars", L("调仓间隔（K线数）", "Rebalance every (bars)"), 5, 1, 60, 1),
        PCT,
    ], min_assets=2))
