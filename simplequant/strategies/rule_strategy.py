"""
规则策略：把规则 JSON（见 simplequant/rules/schema.py）编译成 Backtrader 指标并执行
"""

import json
import math
import operator

import backtrader as bt

from ..rules.schema import CROSS_OPS, default_line, validate
from .factors import KDJ, OBV
from .templates import _PerAsset

_CMP = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}


def _multi_line_base(d, ind: str, p: dict):
    """多输出线的指标本体（MACD / 布林带 / DMI / KDJ）；同一标的、同一组参数只需要建一次"""
    if ind == "macd":
        return bt.ind.MACD(d.close, period_me1=int(p["fast"]), period_me2=int(p["slow"]), period_signal=int(p["signal"]))
    if ind == "boll":
        return bt.ind.BollingerBands(d.close, period=int(p["period"]), devfactor=float(p["dev"]))
    if ind == "adx":
        return bt.ind.DirectionalMovement(d, period=int(p["period"]))
    if ind == "kdj":
        return KDJ(d, period=int(p["period"]), m1=int(p["m1"]), m2=int(p["m2"]))
    return None


def _build_line(d, spec: dict, bases: dict | None = None):
    """
    bases：{(标的, 指标, 参数): 指标本体} 缓存。规则里既用 MACD 的 DIF 又用 DEA 时共用同一个 MACD，
    图表上也只画一次。
    """
    ind, p = spec["ind"], spec.get("params") or {}
    line = spec.get("line", default_line(ind))
    if ind in ("close", "open", "high", "low", "volume"):
        return getattr(d, ind)
    if ind in ("macd", "boll", "adx", "kdj"):
        key = (id(d), ind, json.dumps(p, sort_keys=True))
        if bases is None or key not in bases:
            base = _multi_line_base(d, ind, p)
            if bases is not None:
                bases[key] = base
        base = bases[key] if bases is not None else base
        if ind == "macd":
            return {"dif": base.macd, "dea": base.signal, "hist": base.macd - base.signal}[line]
        if ind == "boll":
            return {"upper": base.top, "mid": base.mid, "lower": base.bot}[line]
        if ind == "adx":
            return {"adx": base.adx, "pdi": base.plusDI, "mdi": base.minusDI}[line]
        return {"k": base.k, "d": base.d, "j": base.j}[line]
    if ind == "sma":
        return bt.ind.SMA(d.close, period=int(p["period"]))
    if ind == "ema":
        return bt.ind.EMA(d.close, period=int(p["period"]))
    if ind == "rsi":
        return bt.ind.RSI(d.close, period=int(p["period"]), safediv=True)
    if ind == "atr":
        return bt.ind.ATR(d, period=int(p["period"]))
    if ind == "roc":
        return bt.ind.PercentChange(d.close, period=int(p["period"])) * 100
    if ind == "highest":  # 不含当前 K 线，便于写"突破前 N 日高点"
        return bt.ind.Highest(d.high, period=int(p["period"]))(-1)
    if ind == "lowest":
        return bt.ind.Lowest(d.low, period=int(p["period"]))(-1)
    if ind == "vol_ma":
        return bt.ind.SMA(d.volume, period=int(p["period"]))
    # ---- 以下为扩充的择时因子 ----
    if ind == "ma_slope":
        ma = bt.ind.SMA(d.close, period=int(p["period"]))
        return (ma / ma(-int(p["n"])) - 1) * 100
    if ind == "bias":
        return (d.close / bt.ind.SMA(d.close, period=int(p["period"])) - 1) * 100
    if ind == "cci":
        return bt.ind.CCI(d, period=int(p["period"]))
    if ind == "wr":
        return bt.ind.WilliamsR(d, period=int(p["period"]))
    if ind == "vol_ratio":  # 与前 N 根（不含当前）的均量比较
        return bt.DivByZero(d.volume, bt.ind.SMA(d.volume, period=int(p["period"]))(-1), zero=float("nan"))
    if ind == "obv":
        return OBV(d).obv
    if ind == "volatility":
        return bt.ind.StdDev(bt.ind.PctChange(d.close, period=1), period=int(p["period"])) * 100
    if ind in ("turnover", "pe", "pb", "ps"):
        return getattr(d, ind)
    raise ValueError(f"Unknown indicator / 未知指标 {ind}")


class RuleStrategy(_PerAsset):
    params = (("rule", None),)

    def __init__(self):
        super().__init__()
        rule = self.p.rule or {}
        errors = validate(rule, "en")
        if errors:
            raise ValueError("Invalid rule / 规则有误: " + "; ".join(errors))
        self.rule = rule
        self._line_cache, self._bases, self._crosses = {}, {}, {}
        self.checks = {d: {side: [self._compile(d, c) for c in (rule.get(side) or {}).get("conditions", [])]
                           for side in ("buy", "sell")} for d in self.datas}

    def _line(self, d, spec):
        key = (id(d), json.dumps(spec, sort_keys=True))
        if key not in self._line_cache:
            self._line_cache[key] = _build_line(d, spec, self._bases)
        return self._line_cache[key]

    def _cross(self, d, left, right):
        """同一对线的交叉只计算一次（"上穿买入、下穿卖出"共用）；交叉信号本身不画在图上"""
        key = (id(d), json.dumps(left, sort_keys=True), json.dumps(right, sort_keys=True))
        if key not in self._crosses:
            other = float(right["value"]) if "value" in right else self._line(d, right)
            cross = bt.ind.CrossOver(self._line(d, left), other)
            cross.plotinfo.plot = False
            self._crosses[key] = cross
        return self._crosses[key]

    def _operand(self, d, spec):
        """返回一个取当前值的函数"""
        if "value" in spec:
            v = float(spec["value"])
            return lambda: v
        if spec["ind"] == "pnl_pct":
            def pnl():
                pos = self.getposition(d)
                return (d.close[0] / pos.price - 1) * 100 if pos.size > 0 and pos.price > 0 else math.nan
            return pnl
        line = self._line(d, spec)
        return lambda: line[0]

    def _compile(self, d, cond):
        op, left, right = cond["op"], cond["left"], cond["right"]
        if op in CROSS_OPS:
            cross = self._cross(d, left, right)
            sign = 1 if op == "cross_above" else -1
            return lambda: cross[0] * sign > 0
        a, b, cmp = self._operand(d, left), self._operand(d, right), _CMP[op]

        def check():
            x, y = a(), b()
            return not (math.isnan(x) or math.isnan(y)) and cmp(x, y)
        return check

    def _hit(self, d, side) -> bool:
        checks = self.checks[d][side]
        if not checks:
            return False
        results = (c() for c in checks)
        return all(results) if (self.rule.get(side) or {}).get("logic", "all") == "all" else any(results)

    # ---------- 交互式策略图用：规则里实际用到的线 ----------
    OVERLAY = {"sma", "ema", "boll", "highest", "lowest"}        # 与价格同一刻度，叠加在 K 线上
    LEVELS = {"rsi": [30, 70], "kdj": [20, 80], "wr": [-80, -20], "cci": [-100, 100]}

    def chart_groups(self) -> list[dict]:
        """
        按"指标 + 参数"分组（多输出线的指标如 MACD 的 DIF/DEA 放在同一栏）；
        与数值比较的条件，把数值作为参考线（如 RSI < 70 → 70）
        """
        groups = {}
        for side in ("buy", "sell"):
            for c in (self.rule.get(side) or {}).get("conditions", []):
                for pos, other in (("left", "right"), ("right", "left")):
                    s = c[pos]
                    if "value" in s or s["ind"] in ("pnl_pct", "close", "open", "high", "low", "volume"):
                        continue
                    base = {k: v for k, v in s.items() if k != "line"}
                    g = groups.setdefault(json.dumps(base, sort_keys=True), {
                        "spec": base, "overlay": s["ind"] in self.OVERLAY, "lines": {},
                        "hlines": set(self.LEVELS.get(s["ind"], []))})
                    g["lines"][s.get("line") or s["ind"]] = s
                    if "value" in c[other]:
                        g["hlines"].add(float(c[other]["value"]))
        return list(groups.values())

    def next(self):
        for d in self.datas:
            holding = self.getposition(d).size > 0
            if not holding and self._hit(d, "buy"):
                self.order_target_pct(d, self.slot_pct, ("reason.rule_buy", {}))
            elif holding and self._hit(d, "sell"):
                self.order_target_pct(d, 0, ("reason.rule_sell", {}))
