"""
导出到聚宽 / 掘金 / QMT：
1. 信号核心的每个指标与 Backtrader 逐值一致（含预热期）
2. 用模拟的平台接口逐日运行导出的策略，每一笔成交都必须与 SimpleQuant 回测相同
   （模拟平台按 SimpleQuant 的成交规则撮合：次日开盘价 ± 滑点并限制在当日高低价内、A 股费用）
"""

import ast
import datetime as dt
import sys
import types

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_export import RICH_RULE
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.runner import build_cerebro
from simplequant.export import platform_script, platform_code, encode_script
from simplequant.export import signal_core as core
from simplequant.rules import INDICATORS, make_ind
from simplequant.strategies import TEMPLATES
from simplequant.strategies.rule_strategy import RuleStrategy, _build_line

BROKER = BrokerConfig(cash=200_000, commission=0.00025, min_commission=5, stamp_duty=0.0005, slippage=0.0005)
BROKER_DICT = {k: getattr(BROKER, k) for k in ("cash", "commission", "min_commission", "stamp_duty", "slippage",
                                                "t_plus_1")}
SYMBOLS = {"A": "510300", "B": "159915", "C": "600000"}


# ============================== 1. 指标与 Backtrader 一致 ==============================
def _all_specs():
    for ind, meta in INDICATORS.items():
        if meta.get("position") or meta.get("requires"):
            continue
        for line in meta.get("lines") or [None]:
            spec = make_ind(ind)
            if line:
                spec["line"] = line
            yield spec
    yield make_ind("sma", period=7)
    yield {**make_ind("macd", fast=5, slow=35, signal=5), "line": "hist"}
    yield {**make_ind("kdj", period=14, m1=4, m2=2), "line": "j"}
    yield {**make_ind("boll", period=10, dev=1.5), "line": "upper"}


def _bt_line(spec, df):
    import backtrader as bt

    class Rec(bt.Strategy):
        params = (("t_plus_1", True),)

        def __init__(self):
            self.line = _build_line(self.data, spec, {})

    strat = build_cerebro({"A": df}, Rec, {}, BrokerConfig(), None).run()[0]
    obj = strat.line
    arr = obj.array if hasattr(obj, "array") else obj.lines[0].array
    return np.array(arr, dtype=float)


@pytest.mark.parametrize("spec", list(_all_specs()), ids=lambda s: f"{s['ind']}-{s.get('line', '')}")
def test_indicator_matches_backtrader(spec):
    df = make_prices(seed=5, n=300)
    df.iloc[40:43, df.columns.get_loc("volume")] = 0          # 覆盖除零分支
    ours, _ = core.sq_indicator({k: df[k].values for k in df.columns}, spec)
    ours = ours[spec.get("line") or next(iter(ours))]
    theirs = _bt_line(spec, df)
    assert len(ours) == len(theirs)
    np.testing.assert_array_equal(np.isnan(ours), np.isnan(theirs))       # 预热期一致
    ok = ~np.isnan(theirs)
    np.testing.assert_allclose(ours[ok], theirs[ok], rtol=1e-9, atol=1e-9)


def _warmup_bt(rule, df):
    strat = build_cerebro({"A": df}, RuleStrategy, {"rule": rule}, BrokerConfig(), None).run()[0]
    return strat._minperiod


@pytest.mark.parametrize("spec", list(_all_specs()), ids=lambda s: f"{s['ind']}-{s.get('line', '')}")
def test_warmup_matches_backtrader(spec):
    df = make_prices(seed=5, n=200)
    lines = {"A": core._Lines({k: df[k].values for k in df.columns})}
    for cond in ({"left": spec, "op": ">", "right": {"value": 0}},
                 {"left": spec, "op": "cross_above", "right": {"value": 0}},
                 {"left": make_ind("close"), "op": "cross_below", "right": spec}):
        rule = {"buy": {"conditions": [cond]}}
        assert core._rule_warmup(rule, lines) == _warmup_bt(rule, df), cond


# ============================== 2. 模拟平台 ==============================
class Sim:
    """按 SimpleQuant 规则撮合的简易账户"""

    def __init__(self, prices, codes):
        self.prices, self.codes = prices, codes              # codes: 名称 → 平台代码
        self.names = {c: n for n, c in codes.items()}
        self.cash = BROKER.cash
        self.pos = {}                                         # 代码 → {"shares", "cost"}
        self.fills = []

    def bar(self, code, day):
        return self.prices[self.names[code]].loc[day]

    def fill(self, code, delta, day, price=None):
        bar = self.bar(code, day)
        s = BROKER.slippage
        if price is None:
            price = min(bar["open"] * (1 + s), bar["high"]) if delta > 0 else max(bar["open"] * (1 - s), bar["low"])
        value = abs(delta) * price
        comm = max(value * BROKER.commission, BROKER.min_commission)
        p = self.pos.setdefault(code, {"shares": 0, "cost": 0.0})
        if delta > 0:
            p["cost"] = (p["shares"] * p["cost"] + value) / (p["shares"] + delta)
            self.cash -= value + comm
        else:
            comm += value * BROKER.stamp_duty
            self.cash += value - comm
        p["shares"] += delta
        if p["shares"] == 0:
            del self.pos[code]
        self.fills.append({"time": day, "symbol": self.names[code], "side": "buy" if delta > 0 else "sell",
                           "size": abs(delta), "price": price, "value": value, "commission": comm})

    def orders(self):
        return pd.DataFrame(self.fills, columns=["time", "symbol", "side", "size", "price", "value", "commission"])


# 聚宽执行策略前全局里已有 numpy 的同名函数（实测 sum 是 numpy.sum）；np.any(生成器) 恒为真，
# 导出脚本必须自己恢复内置函数
NUMPY_SHADOWS = {"sum": np.sum, "any": np.any, "all": np.all, "min": np.min, "max": np.max,
                 "abs": np.abs, "round": np.round}


def _load(code, platform_modules):
    for name, mod in platform_modules.items():
        sys.modules[name] = mod
    mod = types.ModuleType("exported_platform_strategy")
    mod.__dict__.update(NUMPY_SHADOWS)        # 聚宽的运行环境里 sum / any / all 等是 numpy 的同名函数
    exec(compile(code, "exported.py", "exec"), mod.__dict__)
    return mod.__dict__


def run_joinquant(code, prices):
    codes = {n: platform_code(SYMBOLS[n], "", "joinquant") for n in prices}
    sim = Sim(prices, codes)
    days = next(iter(prices.values())).index
    jq = types.ModuleType("jqdata")
    registered = []

    class Position:
        def __init__(self, p):
            self.total_amount, self.avg_cost = p["shares"], p["cost"]

    class Portfolio:
        @property
        def positions(self):
            return {c: Position(p) for c, p in sim.pos.items()}

        @property
        def available_cash(self):
            return sim.cash

    ctx = types.SimpleNamespace(portfolio=Portfolio(), previous_date=None)
    state = {"today": None}

    def get_price(code, start_date, end_date, frequency, fields, skip_paused, fq):
        df = prices[sim.names[code]]
        return df.loc[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date)), fields]

    def protected(style):
        """聚宽：科创板市价单必须带 0 < 保护价 < 1 万元（2026-09-30 实测拒单）"""
        assert style is not None and 0 < style.limit_price < 10000

    def order(code, amount, style=None):
        protected(style)
        sim.fill(code, amount, state["today"])

    def order_target(code, amount, style=None):
        protected(style)
        sim.fill(code, amount - sim.pos[code]["shares"], state["today"])

    def get_current_data():
        out = {}
        for code, name in sim.names.items():
            o = float(prices[name]["open"].asof(pd.Timestamp(state["today"])))
            out[code] = types.SimpleNamespace(high_limit=round(o * 1.1, 3), low_limit=round(o * 0.9, 3))
        return out

    noop = lambda *a, **k: None  # noqa: E731
    jq.__dict__.update(
        get_price=get_price, order=order, order_target=order_target, set_benchmark=noop, set_option=noop,
        get_current_data=get_current_data, MarketOrderStyle=lambda limit_price: types.SimpleNamespace(limit_price=limit_price),
        set_order_cost=noop, OrderCost=lambda **k: k, set_slippage=noop, PriceRelatedSlippage=lambda x: x,
        run_daily=lambda f, time: registered.append((f, time)), g=types.SimpleNamespace(),
        log=types.SimpleNamespace(info=noop, set_level=noop))
    ns = _load(code, {"jqdata": jq})
    ns["initialize"](ctx)
    (func, when), = registered
    assert when == "open"
    for i, day in enumerate(days):
        state["today"] = day
        ctx.previous_date = (days[i - 1] if i else day - pd.Timedelta(days=1)).date()
        func(ctx)
    return sim.orders()


def run_myquant(code, prices):
    codes = {n: platform_code(SYMBOLS[n], "", "myquant") for n in prices}
    sim = Sim(prices, codes)
    days = next(iter(prices.values())).index
    queue = []
    gm, api = types.ModuleType("gm"), types.ModuleType("gm.api")
    gm.api = api

    class Account:
        cash = property(lambda self: {"available": sim.cash})

        def position(self, symbol, side):
            p = sim.pos.get(symbol)
            return {"volume": p["shares"], "vwap": p["cost"]} if p else None

    def history(symbol, frequency, start_time, end_time, fields, adjust, adjust_end_time, skip_suspended, df):
        assert frequency == "1d" and adjust == "PREV" and adjust_end_time == end_time[:10]
        data = prices[sim.names[symbol]]
        return data.loc[(data.index >= pd.Timestamp(start_time[:10])) & (data.index <= pd.Timestamp(end_time[:10])),
                        fields.split(",")].reset_index(drop=True)

    def order_volume(symbol, volume, order_type, side, position_effect):
        assert order_type == "MARKET" and (side == "BUY") == (position_effect == "OPEN")
        queue.append((symbol, volume if side == "BUY" else -volume))

    subscribed = {}
    api.__dict__.update(
        subscribe=lambda **k: subscribed.update(k), history=history, order_volume=order_volume, run=lambda **k: None,
        ADJUST_PREV="PREV", PositionSide_Long="LONG", OrderType_Market="MARKET", OrderSide_Buy="BUY",
        OrderSide_Sell="SELL", PositionEffect_Open="OPEN", PositionEffect_Close="CLOSE", MODE_BACKTEST=2)
    ns = _load(code, {"gm": gm, "gm.api": api})
    ctx = types.SimpleNamespace(account=lambda: Account(), now=None)
    ns["init"](ctx)
    assert subscribed["frequency"] == "1d" and set(subscribed["symbols"]) == set(codes.values())
    for i, day in enumerate(days):
        # 下单成交在下一根日线开盘（backtest_match_mode=0）
        for symbol, delta in queue:
            sim.fill(symbol, delta, day)
        queue.clear()
        ctx.now = day.to_pydatetime().replace(hour=15)
        ns["on_bar"](ctx, [])
    return sim.orders()


def run_qmt(code, prices):
    codes = {n: platform_code(SYMBOLS[n], "", "qmt") for n in prices}
    sim = Sim(prices, codes)
    days = next(iter(prices.values())).index
    labels = [d.strftime("%Y%m%d") for d in days]

    class Ctx:
        do_back_test = True
        barpos = 0

        def set_universe(self, codes_):
            assert set(codes_) == set(codes.values())

        def set_commission(self, kind, values):
            assert kind == 0 and len(values) == 6

        def get_bar_timetag(self, pos):
            return pos

        def is_last_bar(self):
            return self.barpos == len(days) - 1

        def get_market_data_ex(self, fields, stock_code, period, start_time, end_time, dividend_type, fill_data):
            assert period == "1d" and dividend_type == "front"
            out = {}
            for c in stock_code:
                df = prices[sim.names[c]][fields].copy()
                df.index = [d.strftime("%Y%m%d") for d in df.index]
                out[c] = df[(df.index >= start_time) & (df.index <= end_time)]
            return out

    ctx = Ctx()
    today = {}

    def order_shares(code_, shares, style, price, context):
        assert style == "fix"
        sim.fill(code_, shares, today["day"], price=price)

    ns = _load(code, {})
    ns.update(timetag_to_datetime=lambda tag, fmt: labels[tag], order_shares=order_shares)
    ns["init"](ctx)
    for i, day in enumerate(days):
        ctx.barpos, today["day"] = i, day
        ns["handlebar"](ctx)
        ns["handlebar"](ctx)          # 同一根 K 线重复调用（实时行情推送时会发生）不应重复下单
    # 脚本自己记的账必须与模拟账户一致
    assert ns["G"].book["cash"] == pytest.approx(sim.cash)
    assert {n: p["shares"] for n, p in ns["G"].book["pos"].items()} == \
        {sim.names[c]: p["shares"] for c, p in sim.pos.items()}
    return sim.orders()


RUNNERS = {"joinquant": run_joinquant, "myquant": run_myquant, "qmt": run_qmt}


def export_and_compare(platform, spec, prices):
    items = [{"name": n, "symbol": SYMBOLS[n], "asset": "etf"} for n in prices]
    start, end = (str(next(iter(prices.values())).index[i].date()) for i in (0, -1))
    code = platform_script(platform, spec, items, BROKER_DICT, start, end, "test")
    ast.parse(code, feature_version=(3, 6))                  # 聚宽 / QMT 的 Python 3.6 能解析
    if platform == "qmt":
        assert code.startswith("#coding:gbk") and encode_script(code, "qmt").decode("gbk") == code
    assert "import simplequant" not in code and "from simplequant" not in code    # 不依赖 simplequant 包
    exported = RUNNERS[platform](code, prices)
    ours = run_backtest(prices, *strategies.resolve(spec), BROKER).orders
    assert len(ours) > 0
    ours = ours.copy()
    ours["time"] = pd.to_datetime(ours["time"]).dt.normalize()
    # 同一天里导出脚本先卖后买，SimpleQuant 按下单先后成交：按 (日期, 标的, 方向) 排序后比较
    key = ["time", "symbol", "side"]
    ours = ours.sort_values(key, kind="stable").reset_index(drop=True)
    exported = exported.sort_values(key, kind="stable").reset_index(drop=True)
    pd.testing.assert_frame_equal(exported[["time", "symbol", "side", "size"]].reset_index(drop=True),
                                  ours[["time", "symbol", "side", "size"]], check_dtype=False)
    np.testing.assert_allclose(exported["price"], ours["price"], rtol=1e-12)
    np.testing.assert_allclose(exported["commission"], ours["commission"], rtol=1e-9)
    return code


@pytest.mark.parametrize("platform", list(RUNNERS))
@pytest.mark.parametrize("key", list(TEMPLATES))
def test_templates(platform, key):
    prices = {"A": make_prices(seed=1, n=300), "B": make_prices(seed=2, n=300, drift=0.0006)}
    export_and_compare(platform, {"kind": "template", "template": key, "params": {}}, prices)


@pytest.mark.parametrize("platform", list(RUNNERS))
def test_rich_rule(platform):
    export_and_compare(platform, {"kind": "rule", "rule": RICH_RULE}, {"A": make_prices(seed=3, n=400)})


MULTI_RULE = {
    "buy": {"logic": "any", "conditions": [
        {"left": make_ind("ema", period=10), "op": "cross_above", "right": make_ind("sma", period=30)},
        {"left": {**make_ind("kdj"), "line": "k"}, "op": "cross_above", "right": {**make_ind("kdj"), "line": "d"}},
    ]},
    "sell": {"logic": "any", "conditions": [
        {"left": make_ind("close"), "op": "<", "right": {**make_ind("boll"), "line": "mid"}},
        {"left": {"ind": "pnl_pct"}, "op": ">", "right": {"value": 5}},
        {"left": make_ind("cci"), "op": ">", "right": {"value": 150}},
    ]},
    "position_pct": 90,
}


@pytest.mark.parametrize("platform", list(RUNNERS))
def test_multi_asset_rule(platform):
    prices = {n: make_prices(seed=s, n=350) for n, s in (("A", 11), ("B", 12), ("C", 13))}
    export_and_compare(platform, {"kind": "rule", "rule": MULTI_RULE}, prices)


def test_codes():
    assert platform_code("510300", "etf", "joinquant") == "510300.XSHG"
    assert platform_code("159915", "etf", "joinquant") == "159915.XSHE"
    assert platform_code("600000", "stock", "myquant") == "SHSE.600000"
    assert platform_code("000001", "stock", "myquant") == "SZSE.000001"
    assert platform_code("000300", "index", "qmt") == "000300.SH"
    assert platform_code("399006", "index", "qmt") == "399006.SZ"
    assert platform_code("113050", "", "qmt") == "113050.SH"
    assert platform_code("830799", "stock", "qmt") == "830799.BJ"
    with pytest.raises(ValueError):
        platform_code("my_data", "", "qmt")


def test_unsupported():
    with pytest.raises(ValueError, match="估值"):
        platform_script("joinquant", {"kind": "rule", "rule": {
            "buy": {"conditions": [{"left": {"ind": "pe"}, "op": "<", "right": {"value": 10}}]}}},
            [{"name": "A", "symbol": "600000"}], BROKER_DICT, "2020-01-01", "2021-01-01")
    with pytest.raises(ValueError, match="选股"):
        platform_script("qmt", {"kind": "selection"}, [], BROKER_DICT, "2020-01-01", "2021-01-01")


def test_qmt_does_nothing_outside_backtest():
    items = [{"name": "A", "symbol": "510300", "asset": "etf"}]
    ns = _load(platform_script("qmt", {"kind": "template", "template": "buy_hold", "params": {}}, items,
                               BROKER_DICT, "2021-01-04", "2021-06-01"), {})
    calls = []
    ns.update(order_shares=lambda *a: calls.append(a), timetag_to_datetime=lambda *a: "20210601")
    ctx = types.SimpleNamespace(do_back_test=False, is_last_bar=lambda: True,
                                set_universe=lambda c: None, set_commission=lambda *a: None)
    ns["init"](ctx)
    ns["handlebar"](ctx)
    assert calls == []
