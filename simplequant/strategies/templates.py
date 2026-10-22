"""
内置策略模板
单标的类模板在选了多个标的时，对每个标的独立运行，资金平均分配。
"""

import backtrader as bt

from ..engine.base_strategy import BaseStrategy
from ..rules.schema import BARS_PER_YEAR
from .factors import Percentile
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
    phase：调仓日在周期里提前几根 K 线（界面上没有，调仓日检验 engine/phase.py 用）
    """
    params = (("period", 20), ("rebalance_bars", 5), ("position_pct", 95), ("phase", 0))

    def __init__(self):
        super().__init__()
        self.mom = {d: bt.ind.PercentChange(d.close, period=int(self.p.period)) for d in self.datas}
        self.counter = int(self.p.phase)
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


class FixedWeight(BaseStrategy):
    """
    定比再平衡（如股债 60/40）
    第一个标的占 weight%，其余标的平分剩下的比例，合计投入 position_pct 的资金。
    每隔 rebalance_bars 根 K 线检查一次，任一标的实际比例偏离目标超过 band 个百分点时全部调回目标：
    先卖出超配的、用现有资金买入低配的，下一根 K 线卖出的资金到账后再补足。
    phase：同动量轮动
    """
    params = (("weight", 60), ("rebalance_bars", 20), ("band", 5), ("position_pct", 95), ("phase", 0))

    def __init__(self):
        super().__init__()
        self.counter = int(self.p.phase)
        self.top_up = False

    def targets(self) -> dict:
        total = self.p.position_pct / 100
        first = self.p.weight / 100
        rest = (1 - first) / (len(self.datas) - 1)
        return {d: total * (first if i == 0 else rest) for i, d in enumerate(self.datas)}

    def weight_of(self, d) -> float:
        return self.getposition(d).size * d.close[0] / self.broker.getvalue()

    def next(self):
        targets = self.targets()
        if self.top_up and not any(self.has_pending(d) for d in self.datas):
            self.top_up = False
            for d, w in targets.items():
                if self.weight_of(d) < w:
                    self.order_target_pct(d, w, ("reason.fw_buy", {"w": w * 100}))

        self.counter += 1
        if (self.counter - 1) % int(self.p.rebalance_bars) != 0:
            return
        drift = {d: self.weight_of(d) - w for d, w in targets.items()}
        if max(abs(x) for x in drift.values()) * 100 <= self.p.band:
            return
        for d, x in drift.items():
            if x > 0 and self.order_target_pct(d, targets[d], ("reason.fw_sell", {"w": targets[d] * 100})) is not None:
                self.top_up = True
        for d, x in drift.items():
            if x < 0:
                self.order_target_pct(d, targets[d], ("reason.fw_buy", {"w": targets[d] * 100}))


class Dca(BaseStrategy):
    """
    定投：开始那天投入初始资金，之后每笔追加资金（BrokerConfig.contribution）到账当天买入，多个标的平分。
    凑不满一手的零头留在账上、下一期一起买。multiplier 为 1 即定期定额；估值定投见 ValueDca
    """

    def __init__(self):
        super().__init__()
        self._first = True
        self.reserve = 0.0          # 估值偏高时少投、留作储备的资金（估值偏低时多投先用它）

    def multiplier(self, d) -> tuple[float, tuple]:
        """本期对 d 的投入倍数与下单理由"""
        return 1.0, ("reason.dca", {})

    def next(self):
        day = self.datas[0].datetime.date(0)
        if self.p.trade_start is not None and day < self.p.trade_start:
            return
        base = self.broker.getcash() if self._first else self.new_cash
        self._first = False
        if base <= 0 or any(self.has_pending(d) for d in self.datas):
            return
        share = base / len(self.datas)
        plan = [(d, *self.multiplier(d)) for d in self.datas]
        cash = self.broker.getcash()
        # 少投的部分记入储备，多投的部分从储备里出（储备不够就只投手上有的）
        self.reserve = min(max(self.reserve + sum(share * (1 - m) for _, m, _ in plan), 0.0), cash)
        wants = [share * m for _, m, _ in plan]
        if sum(wants) <= 0:
            return
        spend = cash - self.reserve            # 含之前凑不满一手的零头
        for (d, _, reason), w in zip(plan, wants):
            if w > 0:
                self.order_value(d, spend * w / sum(wants), reason)


class ValueDca(Dca):
    """
    估值定投：按市盈率在过去 years 年里的百分位调整每期投入——低于 low 投 cheap_mult 倍，高于 high 投 dear_mult 倍，
    其余 1 倍。百分位算不出（没有估值数据或历史不足一年）时按 1 倍。
    储备按 reserve_rate（年化 %，按自然日复利）计息，相当于放在货币基金里；利息算收益、不算投入
    """
    params = (("years", 5), ("low", 30), ("high", 70), ("cheap_mult", 2.0), ("dear_mult", 0.5),
              ("reserve_rate", 2.0))
    needs_open = True               # 开盘前给储备计息（runner 据此开 cheat_on_open）

    def __init__(self):
        super().__init__()
        self.pct = {d: Percentile(d.pe, window=int(self.p.years) * BARS_PER_YEAR, min_count=BARS_PER_YEAR).pct
                    for d in self.datas}
        self._no_pct = set()
        self._accrued = None        # 储备利息算到哪一天
        self.interest = 0.0         # 储备累计利息

    def next_open(self):
        super().next_open()
        self._accrue()

    def _accrue(self):
        """开盘前给储备计上次以来的利息（和追加资金一样，开盘前 add_cash 收盘时才可用，next 里的储备不会被现金截掉）"""
        day = max((d.datetime.date(0) for d in self.datas if len(d)), default=None)
        if day is None:
            return
        if self._accrued is not None and self.reserve > 0 and self.p.reserve_rate:
            amount = self.reserve * ((1 + self.p.reserve_rate / 100) ** ((day - self._accrued).days / 365) - 1)
            if amount > 0:
                self.broker.add_cash(amount)
                self.reserve += amount
                self.interest += amount
        self._accrued = day

    def multiplier(self, d):
        v = self.pct[d][0]
        if v != v:
            if d not in self._no_pct:
                self._no_pct.add(d)
                self.log("log.no_pe_pct", d, name=d._name)
            return 1.0, ("reason.dca", {})
        if v < self.p.low:
            m = float(self.p.cheap_mult)
        elif v > self.p.high:
            m = float(self.p.dear_mult)
        else:
            m = 1.0
        return m, ("reason.value_dca", {"v": v, "m": m})


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
register(Template(
    "fixed_weight", L("定比再平衡", "Fixed-weight rebalancing"),
    L("按固定比例持有多个标的（如股票 ETF 与债券 ETF 60/40），定期检查，偏离超过阈值时调回目标比例。"
      "第一个标的占设定比例，其余标的平分剩余部分。至少需要 2 个标的。",
      "Holds several assets at fixed weights (e.g. 60/40 equity and bond ETFs) and periodically rebalances "
      "when any weight drifts beyond the band. The first asset gets the set weight; the rest share the remainder "
      "equally. Needs at least 2 assets."),
    FixedWeight, [
        Param("weight", L("第一个标的的比例(%)", "Weight of the first asset (%)"), 60, 0, 100, 5),
        Param("rebalance_bars", L("检查间隔（K线数）", "Check every (bars)"), 20, 1, 250, 1),
        Param("band", L("偏离阈值（百分点）", "Drift band (percentage points)"), 5, 0, 30, 1,
              L("任一标的实际比例与目标相差超过该值时再平衡；0 表示每次检查都调回目标",
                "Rebalance when any weight is off target by more than this; 0 rebalances at every check")),
        PCT,
    ], min_assets=2))
register(Template(
    "dca", L("定期定额定投", "Dollar-cost averaging"),
    L("开始时投入初始资金，之后每期追加的资金到账就买入，多个标的平分。需要在资金设置里填写每期追加金额；"
      "零头凑不满一手的留到下一期。",
      "Invests the starting cash, then buys with each periodic contribution as it arrives, split equally across "
      "assets. Set the contribution amount in the capital settings; amounts too small for a lot roll over."),
    Dca, []))
register(Template(
    "value_dca", L("估值定投", "Valuation-based DCA"),
    L("在定期定额的基础上按市盈率历史百分位调整每期投入：估值低时多投、高时少投，少投的钱留作储备，低估时再用。"
      "需要带市盈率的数据（股票日线，或下载时含指数估值的 ETF）和每期追加金额。储备按设定的年化收益计息（相当于放在货币基金里）。",
      "Like dollar-cost averaging, but scales each contribution by the P/E percentile: more when cheap, less when "
      "expensive. Money held back is kept as a reserve for cheap periods, earning the set annual rate (like a money "
      "market fund). Needs data with "
      "P/E (stock daily data, or ETFs downloaded with index valuation) and a contribution amount."),
    ValueDca, [
        Param("years", L("百分位回看年数", "Percentile lookback (years)"), 5, 1, 20, 1),
        Param("low", L("低估线（百分位低于）", "Cheap below (percentile)"), 30, 0, 50, 5),
        Param("high", L("高估线（百分位高于）", "Expensive above (percentile)"), 70, 50, 100, 5),
        Param("cheap_mult", L("低估时投入倍数", "Multiplier when cheap"), 2.0, 1.0, 5.0, 0.5),
        Param("dear_mult", L("高估时投入倍数", "Multiplier when expensive"), 0.5, 0.0, 1.0, 0.1,
              L("0 表示高估时暂停投入", "0 pauses contributions when expensive")),
        Param("reserve_rate", L("储备年化收益（%）", "Reserve annual yield (%)"), 2.0, 0.0, 10.0, 0.5,
              L("少投留下的钱按这个收益率计息，相当于放在货币基金里；0 表示不计息",
                "Interest on money held back, like a money market fund; 0 means none")),
    ], constraint=lambda p: p["low"] < p["high"]))
