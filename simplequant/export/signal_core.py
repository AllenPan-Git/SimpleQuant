"""
SimpleQuant 信号核心：与平台无关的纯 Python 实现（只依赖 numpy，兼容 Python 3.6）

导出到聚宽 / 掘金 / QMT 时，这个文件的正文原样嵌入生成的策略里；各平台只负责取行情、查持仓、下单。
- 指标算法逐项对照 Backtrader（SMA 用 fsum、EMA/SMMA 用 SMA 做初值、交叉用"上一个非零差值"等），
  预热期（多少根 K 线后才开始交易）也与 Backtrader 相同
- 下单数量的计算照搬 SimpleQuant 的 BaseStrategy.order_target_pct（整手、预留 1% 资金、资金不足一手时跳过）
测试保证：在模拟的平台上逐日运行，每一笔委托都与 SimpleQuant 回测相同（tests/test_platform_export.py）。

注意：这个文件会在 Python 3.6 上运行，不要使用 3.6 之后的语法（海象运算符、类型注解里的 | 等）。
"""

import json
import math

import numpy as np

NAN = float("nan")
LOT = 100
CASH_BUFFER = 0.01
T_PLUS_1 = True       # 当天开盘买入的，当天收盘出现卖出信号也不能卖（次日再卖）


# ============================== 指标（与 Backtrader 逐项一致） ==============================
def _first_valid(x):
    for i, v in enumerate(x):
        if not math.isnan(v):
            return i
    return len(x)


def _sma(x, n):
    """Backtrader SMA：每个窗口用 math.fsum 求和"""
    x = list(x)
    out = [NAN] * len(x)
    for i in range(_first_valid(x) + n - 1, len(x)):
        out[i] = math.fsum(x[i - n + 1:i + 1]) / n
    return np.array(out)


def _smooth(x, n, alpha):
    """Backtrader ExponentialSmoothing：前 n 个值的均值做初值，之后 prev*(1-alpha) + x*alpha"""
    x = list(x)
    out = [NAN] * len(x)
    start = _first_valid(x) + n - 1
    alpha1 = 1.0 - alpha
    if start < len(x):
        prev = out[start] = math.fsum(x[start - n + 1:start + 1]) / n
        for i in range(start + 1, len(x)):
            prev = out[i] = prev * alpha1 + x[i] * alpha
    return np.array(out)


def _ema(x, n):
    return _smooth(x, n, 2.0 / (1.0 + n))


def _smma(x, n):
    return _smooth(x, n, 1.0 / n)


def _shift(x, k):
    out = np.full(len(x), NAN)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def _rolling(x, n, func):
    x = list(x)
    out = [NAN] * len(x)
    for i in range(_first_valid(x) + n - 1, len(x)):
        out[i] = func(x[i - n + 1:i + 1])
    return np.array(out)


def _stddev(x, n):
    """Backtrader StdDev：sqrt(|SMA(x^2) - SMA(x)^2|)"""
    meansq = _sma(np.asarray(x) ** 2, n)
    mean = _sma(x, n)
    return np.array([NAN if math.isnan(a) or math.isnan(b) else abs(a - pow(b, 2)) ** 0.5
                     for a, b in zip(meansq, mean)])


def _up_down(close):
    prev = _shift(close, 1)
    return np.maximum(close - prev, 0.0), np.maximum(prev - close, 0.0)   # NaN 保留在第一个位置


def _atr(bars, n):
    prev = _shift(bars["close"], 1)
    tr = np.maximum(bars["high"], prev) - np.minimum(bars["low"], prev)
    tr[0] = NAN
    return _smma(tr, n)


def _div_or(a, b, zero):
    """Backtrader DivByZero：分母为 0 时返回 zero"""
    return np.array([NAN if math.isnan(x) or math.isnan(y) else (x / y if y != 0 else zero) for x, y in zip(a, b)])


def _rsi(close, n):
    up, down = _up_down(close)
    up[0] = down[0] = NAN
    maup, madown = _smma(up, n), _smma(down, n)
    out = []
    for u, d in zip(maup, madown):
        if math.isnan(u) or math.isnan(d):
            out.append(NAN)
        elif d == 0:                        # safediv：0/0 → 50，x/0 → 100
            out.append(50.0 if u == 0 else 100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + u / d))
    return np.array(out)


def _kdj(bars, n, m1, m2):
    hi = _rolling(bars["high"], n, max)
    lo = _rolling(bars["low"], n, min)
    rsv = _div_or(bars["close"] - lo, hi - lo, 0.5) * 100
    k, d, j = (np.full(len(rsv), NAN) for _ in range(3))
    start = n - 1
    for i in range(start, len(rsv)):
        pk = 50.0 if i == start else k[i - 1]
        pd_ = 50.0 if i == start else d[i - 1]
        if i == start:
            k[i] = (pk * (m1 - 1) + rsv[i]) / m1
            d[i] = (pd_ * (m2 - 1) + k[i]) / m2
        else:
            k[i] = (k[i - 1] * (m1 - 1) + rsv[i]) / m1
            d[i] = (d[i - 1] * (m2 - 1) + k[i]) / m2
        j[i] = 3 * k[i] - 2 * d[i]
    return k, d, j


def _obv(bars):
    close, vol = bars["close"], bars["volume"]
    out = np.full(len(close), NAN)
    if len(close):
        out[0] = 0.0
    for i in range(1, len(close)):
        change = close[i] - close[i - 1]
        step = vol[i] if change > 0 else -vol[i] if change < 0 else 0.0
        out[i] = out[i - 1] + step
    return out


def _dmi(bars, n):
    atr = _atr(bars, n)
    high, low = bars["high"], bars["low"]
    upmove = high - _shift(high, 1)
    downmove = _shift(low, 1) - low
    plus = np.where((upmove > downmove) & (upmove > 0.0), upmove, 0.0)
    minus = np.where((downmove > upmove) & (downmove > 0.0), downmove, 0.0)
    plus[0] = minus[0] = NAN
    pdi = 100.0 * _smma(plus, n) / atr
    mdi = 100.0 * _smma(minus, n) / atr
    adx = 100.0 * _smma(np.abs(pdi - mdi) / (pdi + mdi), n)
    return adx, pdi, mdi


def sq_indicator(bars, spec):
    """
    返回 (输出线 {名: 数组}, 指标本体的预热期)
    预热期是 Backtrader 里这个指标对象的 minperiod：即使规则只用到其中一条线（如 DMI 的 +DI），
    策略也要等整个指标就绪才开始运行
    """
    ind, p = spec["ind"], spec.get("params") or {}
    n = lambda k: int(p[k])  # noqa: E731
    close = bars["close"]
    if ind in ("close", "open", "high", "low", "volume"):
        return {ind: bars[ind]}, 1
    if ind == "sma":
        return {"sma": _sma(close, n("period"))}, n("period")
    if ind == "ema":
        return {"ema": _ema(close, n("period"))}, n("period")
    if ind == "rsi":
        return {"rsi": _rsi(close, n("period"))}, n("period") + 1
    if ind == "atr":
        return {"atr": _atr(bars, n("period"))}, n("period") + 1
    if ind == "roc":
        return {"roc": (close / _shift(close, n("period")) - 1.0) * 100}, n("period") + 1
    if ind == "highest":                   # 不含当前 K 线
        return {"highest": _shift(_rolling(bars["high"], n("period"), max), 1)}, n("period") + 1
    if ind == "lowest":
        return {"lowest": _shift(_rolling(bars["low"], n("period"), min), 1)}, n("period") + 1
    if ind == "vol_ma":
        return {"vol_ma": _sma(bars["volume"], n("period"))}, n("period")
    if ind == "ma_slope":
        ma = _sma(close, n("period"))
        return {"ma_slope": (ma / _shift(ma, n("n")) - 1) * 100}, n("period") + n("n")
    if ind == "bias":
        return {"bias": (close / _sma(close, n("period")) - 1) * 100}, n("period")
    if ind == "cci":
        tp = (bars["high"] + bars["low"] + close) / 3.0
        mean = _sma(tp, n("period"))
        meandev = _sma(np.abs(tp - mean), n("period"))
        return {"cci": (tp - mean) / (0.015 * meandev)}, 2 * n("period") - 1
    if ind == "wr":
        h = _rolling(bars["high"], n("period"), max)
        lo = _rolling(bars["low"], n("period"), min)
        return {"wr": -100.0 * (h - close) / (h - lo)}, n("period")
    if ind == "vol_ratio":                 # 与前 N 根（不含当前）的均量比较
        return {"vol_ratio": _div_or(bars["volume"], _shift(_sma(bars["volume"], n("period")), 1), NAN)}, \
            n("period") + 1
    if ind == "obv":
        return {"obv": _obv(bars)}, 1
    if ind == "volatility":
        pct = close / _shift(close, 1) - 1.0
        return {"volatility": _stddev(pct, n("period")) * 100}, n("period") + 1
    if ind == "macd":
        dif = _ema(close, n("fast")) - _ema(close, n("slow"))
        dea = _ema(dif, n("signal"))
        return {"dif": dif, "dea": dea, "hist": dif - dea}, n("slow") + n("signal") - 1
    if ind == "boll":
        mid = _sma(close, n("period"))
        dev = float(p["dev"]) * _stddev(close, n("period"))
        return {"upper": mid + dev, "mid": mid, "lower": mid - dev}, n("period")
    if ind == "adx":                       # Backtrader 的 DirectionalMovement 含 ADXR，预热 3N
        adx, pdi, mdi = _dmi(bars, n("period"))
        return {"adx": adx, "pdi": pdi, "mdi": mdi}, 3 * n("period")
    if ind == "kdj":
        k, d, j = _kdj(bars, n("period"), n("m1"), n("m2"))
        return {"k": k, "d": d, "j": j}, n("period")
    raise ValueError("unsupported indicator / 不支持的指标: " + str(ind))


def _line_mp(x):
    """一条线从第几根 K 线开始有值（Backtrader 的线 minperiod）"""
    return _first_valid(x) + 1


def sq_cross(a, b, i):
    """
    Backtrader CrossOver 在第 i 根 K 线的值（+1 上穿 / -1 下穿 / 0）：
    与"上一个不为零的差值"比较，所以两线先相等、再分开也算一次交叉
    """
    start = max(_line_mp(a), _line_mp(b)) - 1
    if i < start + 1:
        return 0
    nzd = a[start] - b[start]
    for k in range(start + 1, i):
        d = a[k] - b[k]
        nzd = d if d else nzd
    if nzd < 0 and a[i] > b[i]:
        return 1
    if nzd > 0 and a[i] < b[i]:
        return -1
    return 0


# ============================== 下单数量（与 SimpleQuant BaseStrategy 一致） ==============================
class _Orders(object):
    """
    book: {"cash": 可用资金, "pos": {名称: {"shares": 股数, "cost": 持仓均价}}}
    总资产 = 现金 + Σ 股数 × 最近收盘价；同一根 K 线里多笔买入各自按下单前的现金计算（与 Backtrader 相同）
    """

    def __init__(self, hist, book, state, log):
        self.hist, self.book, self.state, self.log = hist, book, state, log
        self.orders, self.ordered = [], set()
        self.value = book["cash"] + sum(self.shares(n) * self.close(n) for n in hist)

    def shares(self, name):
        return int((self.book["pos"].get(name) or {}).get("shares", 0))

    def close(self, name):
        return float(self.hist[name]["close"][-1])

    def bars(self, name):
        return len(self.hist[name]["close"])

    def can_sell(self, name):
        """T+1：上一次运行时下的买单在最新这根 K 线开盘成交，这根 K 线收盘产生的卖出信号当天不能执行"""
        return not T_PLUS_1 or self.state.get("bought_at", {}).get(name) != self.bars(name) - 1

    def target(self, name, pct, reason):
        """把持仓调整到总资产的 pct（0~1）；下了单返回 True"""
        if name in self.ordered:
            return False
        price = self.close(name)
        if price <= 0:
            return False
        target = int(self.value * pct / price / LOT) * LOT
        current = self.shares(name)
        delta = target - current
        if pct > 0 and current == 0 and target < LOT:
            warned = self.state.setdefault("lot_warned", [])
            if name not in warned:
                warned.append(name)
                self.log("资金不足一手 / cannot afford one lot: %s (%.0f)" % (name, price * LOT))
            return False
        if delta > 0:
            affordable = int(self.book["cash"] / (price * (1 + CASH_BUFFER)) / LOT) * LOT
            delta = min(delta, affordable)
            if delta >= LOT:
                self.state.setdefault("bought_at", {})[name] = self.bars(name)
                return self._add(name, delta, reason)
        elif delta < 0 and current > 0:
            if not self.can_sell(name):
                self.log("T+1: %s 今天买入，明天才能卖出 / bought today, can sell tomorrow" % name)
                return False
            return self._add(name, -current if target <= 0 else delta, reason)
        return False

    def _add(self, name, delta, reason):
        self.orders.append((name, int(delta), reason))
        self.ordered.add(name)
        return True


# ============================== 策略 ==============================
_CMP = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


class _Lines(object):
    """每次运行时按需计算、缓存指标"""

    def __init__(self, bars):
        self.bars, self.cache = bars, {}

    def get(self, spec):
        key = json.dumps({k: v for k, v in spec.items() if k != "line"}, sort_keys=True)
        if key not in self.cache:
            self.cache[key] = sq_indicator(self.bars, spec)
        lines, mp = self.cache[key]
        return lines[spec.get("line") or next(iter(lines))], mp


def _operand_line(lines, spec, n):
    if "value" in spec:
        return np.full(n, float(spec["value"])), 1
    return lines.get(spec)


def _rule_warmup(rule, lines_by_name):
    """所有指标对象与交叉的预热期取最大值（Backtrader 策略的 minperiod）"""
    warm = 1
    for lines in lines_by_name.values():
        n = len(lines.bars["close"])
        for side in ("buy", "sell"):
            for c in (rule.get(side) or {}).get("conditions", []):
                mps, line_mps = [], []
                for s in (c["left"], c["right"]):
                    if s.get("ind") == "pnl_pct":
                        continue
                    arr, mp = _operand_line(lines, s, n)
                    mps.append(mp)
                    line_mps.append(_line_mp(arr) if "value" not in s else 1)
                warm = max([warm] + mps)
                if c["op"] in ("cross_above", "cross_below"):
                    warm = max(warm, max(line_mps) + 1)
    return warm


def _check(cond, lines, book_pos, close):
    op, left, right = cond["op"], cond["left"], cond["right"]
    n = len(close)
    if op in ("cross_above", "cross_below"):
        a, _ = _operand_line(lines, left, n)
        b, _ = _operand_line(lines, right, n)
        return sq_cross(a, b, n - 1) == (1 if op == "cross_above" else -1)

    def value(spec):
        if "value" in spec:
            return float(spec["value"])
        if spec["ind"] == "pnl_pct":
            shares, cost = int(book_pos.get("shares", 0)), float(book_pos.get("cost", 0) or 0)
            return (close[-1] / cost - 1) * 100 if shares > 0 and cost > 0 else NAN
        return float(lines.get(spec)[0][-1])
    x, y = value(left), value(right)
    return not (math.isnan(x) or math.isnan(y)) and _CMP[op](x, y)


def _hit(rule, side, lines, book_pos, close):
    block = rule.get(side) or {}
    conds = block.get("conditions") or []
    if not conds:
        return False
    results = (_check(c, lines, book_pos, close) for c in conds)
    return all(results) if block.get("logic", "all") == "all" else any(results)


def _run_rule(spec, hist, o):
    rule = spec["rule"]
    names = list(hist)
    lines = {n: _Lines(hist[n]) for n in names}
    warm = _rule_warmup(rule, lines)
    if any(len(hist[n]["close"]) < warm for n in names):
        return
    slot = rule.get("position_pct", 95) / 100.0 / len(names)
    for n in names:
        pos = o.book["pos"].get(n) or {}
        holding = o.shares(n) > 0
        if not holding and _hit(rule, "buy", lines[n], pos, hist[n]["close"]):
            o.target(n, slot, "rule buy / 满足买入条件")
        elif holding and _hit(rule, "sell", lines[n], pos, hist[n]["close"]):
            o.target(n, 0, "rule sell / 满足卖出条件")


def _run_template(spec, hist, o, state):
    key, p = spec["template"], spec.get("params") or {}
    names = list(hist)
    count = min(len(hist[n]["close"]) for n in names)
    slot = p.get("position_pct", 95) / 100.0 / len(names)
    if key == "buy_hold":
        for n in names:
            if o.shares(n) == 0:
                o.target(n, slot, "buy & hold / 买入持有")
    elif key == "sma_cross":
        fast, slow = int(p.get("fast", 5)), int(p.get("slow", 20))
        if count < slow + 1:
            return
        for n in names:
            c = hist[n]["close"]
            x = sq_cross(_sma(c, fast), _sma(c, slow), len(c) - 1)
            if x > 0:
                o.target(n, slot, "MA%d crosses above MA%d / 均线金叉" % (fast, slow))
            elif x < 0:
                o.target(n, 0, "MA%d crosses below MA%d / 均线死叉" % (fast, slow))
    elif key == "rsi_reversion":
        period = int(p.get("period", 14))
        if count < period + 1:
            return
        for n in names:
            r = _rsi(hist[n]["close"], period)[-1]
            if r < p.get("low", 30):
                o.target(n, slot, "RSI %.1f oversold / 超卖" % r)
            elif r > p.get("high", 70):
                o.target(n, 0, "RSI %.1f overbought / 超买" % r)
    elif key == "boll_breakout":
        period = int(p.get("period", 20))
        if count < period:
            return
        for n in names:
            lines, _ = sq_indicator(hist[n], {"ind": "boll", "params": {"period": period,
                                                                      "dev": float(p.get("devfactor", 2.0))}})
            c = hist[n]["close"][-1]
            if c > lines["upper"][-1]:
                o.target(n, slot, "close above upper band / 突破上轨")
            elif c < lines["mid"][-1]:
                o.target(n, 0, "close below middle band / 跌破中轨")
    elif key == "momentum_rotation":
        period, every = int(p.get("period", 20)), int(p.get("rebalance_bars", 5))
        pct = p.get("position_pct", 95) / 100.0
        if count < period + 1:
            return
        if state.get("pending_target") is not None:           # 上一根 K 线已卖出，资金到账后买入
            o.target(state["pending_target"], pct, "rotation buy / 轮动买入")
            state["pending_target"] = None
        state["counter"] = state.get("counter", 0) + 1
        if state["counter"] % every != 0:
            return
        scores = {n: hist[n]["close"][-1] / hist[n]["close"][-1 - period] - 1.0 for n in names}
        best = max(scores, key=scores.get)
        holding = next((n for n in names if o.shares(n) > 0), None)
        if scores[best] <= 0:
            if holding is not None:
                o.target(holding, 0, "all momentum <= 0 / 全部下跌，空仓")
            state["pending_target"] = None
            return
        if holding == best:
            return
        if holding is not None:
            if o.target(holding, 0, "switch to %s / 换仓" % best):
                state["pending_target"] = best
        else:
            o.target(best, pct, "strongest %s %.2f%% / 动量最强" % (best, scores[best] * 100))
    else:
        raise ValueError("unsupported template / 不支持的模板: " + str(key))


def sq_step(spec, hist, book, state, log=print):
    """
    每个交易日开盘前调用一次。
    :param hist: {名称: {"open","high","low","close","volume": 数组}}，只含已经收盘的 K 线（到上一交易日），旧→新
    :param book: {"cash": 可用资金, "pos": {名称: {"shares": 持股数, "cost": 持仓均价}}}
    :param state: 跨交易日保存的状态（dict，由平台负责保存）
    :return: [(名称, 股数变化(+买/-卖), 理由)]，按下单先后排列
    """
    hist = {n: {k: np.asarray(v, dtype=float) for k, v in bars.items()} for n, bars in hist.items()}
    if not hist or any(len(b["close"]) == 0 for b in hist.values()):
        return []
    o = _Orders(hist, book, state, log)
    if spec["kind"] == "rule":
        _run_rule(spec, hist, o)
    else:
        _run_template(spec, hist, o, state)
    return o.orders
