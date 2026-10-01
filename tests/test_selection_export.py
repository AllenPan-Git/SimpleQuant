"""
多因子选股导出到聚宽 / 掘金 / QMT：
1. 选股核心（selection_core）在同样的数据上，每个调仓日选出的名单与 build_schedule 完全相同
2. 用模拟的平台接口逐日运行导出的策略：名单与 SimpleQuant 相同；聚宽版的每笔委托与 run_selection 相同
"""

import ast

import numpy as np
import pandas as pd
import pytest

from simplequant.engine import BrokerConfig
from test_platform_export import NUMPY_SHADOWS
from simplequant.export import selection_core as core
from simplequant.stocks import Panel, build_schedule
from simplequant.stocks.panel import trading_constraints

CODES = 30


def make_panel(days=520, seed=0) -> Panel:
    """带成分股变动、ST、停牌、新上市、估值与市值的合成面板"""
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2021-01-04", periods=days)
    codes = [f"sh.600{i:03d}" for i in range(CODES - 10)] + [f"sz.000{i:03d}" for i in range(10)]
    n = len(codes)
    quality = rng.normal(0, 1, n)
    rets = 0.0008 * quality + rng.normal(0, 0.015, (days, n))
    close = 10 * np.exp(np.cumsum(rets, axis=0))
    open_ = np.vstack([close[0], close[:-1]]) * (1 + rng.normal(0, 0.004, (days, n)))
    df = lambda a: pd.DataFrame(a, index=cal, columns=codes)  # noqa: E731
    ep = 1 / 20 + 0.01 * quality + rng.normal(0, 0.005, (days, n))
    pe = 1 / ep
    pe[:, 3] = -pe[:, 3]                                  # 亏损股
    fields = {
        "open": df(open_), "close": df(close), "high": df(np.maximum(open_, close) * 1.01),
        "low": df(np.minimum(open_, close) * 0.99), "raw_open": df(open_), "raw_close": df(close),
        "raw_preclose": df(np.vstack([close[0], close[:-1]])), "adj_factor": df(np.ones((days, n))),
        "volume": df(rng.uniform(5e5, 2e6, (days, n))), "turnover": df(rng.uniform(0.5, 3, (days, n))),
        "pe": df(pe), "pb": df(rng.uniform(-0.5, 4, (days, n))), "ps": df(rng.uniform(1, 6, (days, n))),
        "mcap": df(close * rng.uniform(1e8, 5e9, n)),
    }
    fields["amount"] = fields["volume"] * fields["close"]
    tradable = df(np.ones((days, n), dtype=bool))
    # 停牌：停牌日价格沿用、成交量 0（与平台数据一致）
    for j, (a, b) in {5: (100, 130), 12: (300, 305), 20: (0, 40)}.items():
        tradable.iloc[a:b, j] = False
        for f in ("open", "close", "raw_open", "raw_close", "high", "low"):
            fields[f].iloc[a:b, j] = fields[f].iloc[a - 1, j] if a else np.nan
        fields["volume"].iloc[a:b, j] = 0 if a else np.nan
        fields["amount"].iloc[a:b, j] = 0 if a else np.nan
    # 新上市：之前没有任何数据
    for f in fields:
        fields[f].iloc[:200, 25] = np.nan
    tradable.iloc[:200, 25] = False
    is_st = df(np.zeros((days, n), dtype=bool))
    is_st.iloc[150:400, 7] = True
    member = df(np.ones((days, n), dtype=bool))
    member.iloc[:250, 28:] = False                        # 中途调入
    member.iloc[260:, 0] = False                          # 中途调出
    ipo = pd.Series(pd.Timestamp("2015-01-05"), index=codes)
    ipo.iloc[25] = cal[200]
    listed = df((cal.values[:, None] - ipo.values[None, :]).astype("timedelta64[D]").astype(float))
    can_buy, can_sell = trading_constraints(fields["raw_open"], fields["raw_preclose"], tradable, is_st)
    return Panel(fields=fields, member=member, tradable=tradable, is_st=is_st, listed_days=listed,
                 can_buy=can_buy, can_sell=can_sell, benchmark=pd.Series(close.mean(axis=1), index=cal),
                 names={c: c for c in codes}, has_fin=True), ipo


@pytest.fixture(scope="module")
def panel_ipo():
    return make_panel()


def core_schedule(panel, ipo, spec, start):
    """逐日调用选股核心（只用信号日及以前、最近 sel_window 个交易日的数据），返回 {调仓日: 名单}"""
    cal = panel.calendar
    days = [str(d.date()) for d in cal]
    codes = panel.codes
    need = core.sel_fields(spec)
    win = core.sel_window(spec)
    filters = {"exclude_st": True, "min_list_days": 250, **(spec.get("filters") or {})}
    out = {}
    for i in range(len(cal) - 1):
        if not core.sel_is_rebalance(spec, days[:i + 1], days[i + 1], start):
            continue
        lo = max(0, i + 1 - win)
        data = {f: panel[f].iloc[lo:i + 1].values for f in need}
        data["volume"] = np.where(panel.tradable.iloc[lo:i + 1].values, data["volume"], 0)
        mask = core.sel_mask(data, panel.member.iloc[lo:i + 1].values, panel.is_st.iloc[lo:i + 1].values,
                             [str(ipo[c].date()) for c in codes], days[lo:i + 1], filters)
        picks = core.sel_pick(spec, data, mask, codes)
        if picks is not None:
            out[cal[i]] = picks
    return out


SPECS = {
    "price_manual": {"factors": [{"key": "ret20", "weight": 1, "direction": -1},
                                 {"key": "vol60", "weight": 2, "direction": -1},
                                 {"key": "mom_120_20", "weight": 1, "direction": 1}]},
    "value_liquidity": {"factors": [{"key": "ep", "weight": 1}, {"key": "bp", "weight": 0.5},
                                    {"key": "sp", "weight": 1}, {"key": "turn20", "weight": 1, "direction": -1},
                                    {"key": "amihud20", "weight": 1}], "rebalance": "weekly"},
    "size_neutral": {"factors": [{"key": "ep", "weight": 1}, {"key": "ret5", "weight": 1, "direction": -1},
                                 {"key": "size", "weight": 1, "direction": -1}],
                     "neutralize": {"industry": False, "size": True}, "rebalance": 15},
    "ic": {"factors": [{"key": "ep", "weight": 1}, {"key": "ret60", "weight": 1, "direction": -1},
                       {"key": "vol60", "weight": 1, "direction": -1}], "weighting": "ic", "ic_lookback": 120},
    "icir": {"factors": [{"key": "ep", "weight": 1}, {"key": "turn20", "weight": 1, "direction": -1}],
             "weighting": "icir", "ic_lookback": 60, "rebalance": "weekly",
             "filters": {"exclude_st": False, "min_list_days": 60}},
}


def full_spec(name):
    return {"kind": "selection", "universe": "hs300", "top_n": 8, "rebalance": "monthly", "position_pct": 95,
            "filters": {"exclude_st": True, "min_list_days": 250}, **SPECS[name]}


@pytest.mark.parametrize("name", list(SPECS))
def test_core_matches_build_schedule(panel_ipo, name):
    panel, ipo = panel_ipo
    spec = full_spec(name)
    start = str(panel.calendar[150].date())
    # 数据最后一天在 SimpleQuant 里总被当作"月末/周末"；平台上它之后没有交易日，不算调仓日
    ours = {d: v for d, v in build_schedule(panel, spec, start=start).picks.items() if d != panel.calendar[-1]}
    assert len(ours) > 5
    got = core_schedule(panel, ipo, spec, start)
    assert list(got) == list(ours)
    for d in ours:
        assert got[d] == ours[d], d


def test_core_is_python36_compatible():
    import inspect
    ast.parse(inspect.getsource(core), feature_version=(3, 6))


def test_orders_follow_simplequant_rules():
    state = {"retry": ["C"]}
    book = {"cash": 1000.0, "value": 100_000.0, "pos": {"A": 500, "B": 300, "C": 200}}
    market = {"A": {"open": 10.0, "can_buy": True, "can_sell": True},
              "B": {"open": 20.0, "can_buy": True, "can_sell": False},
              "C": {"open": 5.0, "can_buy": True, "can_sell": True},
              "D": {"open": 30.0, "can_buy": True, "can_sell": True},
              "E": {"open": 8.0, "can_buy": False, "can_sell": True}}
    orders = core.sel_orders(["D", "E", "A"], book, market, state, 90)
    # C 重试卖出；B 跌停卖不出 → 待重试；A 继续持有；E 买不进；D 的钱不够一手（重试卖出的钱不计入）
    assert [(c, d) for c, d, _ in orders] == [("C", -200)]
    assert state["retry"] == ["B"]
    # 资金充足：每只 = 总资产 × 90% / 2 = 45000，D 按 30 元 × 1.01 取整手 → 1400 股
    book["cash"] = 50_000.0
    market["B"]["can_sell"] = True
    orders = core.sel_orders(["D", "A"], book, market, {"retry": []}, 90)
    assert [(c, d) for c, d, _ in orders] == [("B", -300), ("C", -200), ("D", 1400)]


# ============================== 2. 模拟平台逐日运行导出的脚本 ==============================
import copy  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

from simplequant.export import selection_platform_script, platform_code, encode_script  # noqa: E402
from simplequant.stocks import run_selection  # noqa: E402

BROKER = BrokerConfig(cash=1_000_000, commission=0.00025, min_commission=5, stamp_duty=0.0005, slippage=0.0005)
BROKER_DICT = {k: getattr(BROKER, k) for k in ("cash", "commission", "min_commission", "stamp_duty", "slippage")}


class Sim:
    """按 SimpleQuant 规则撮合的账户：开盘价 ± 滑点并限制在当日高低价内，A 股费用"""

    def __init__(self, panel, platform):
        self.p = panel
        self.code = {c: platform_code(c, "stock", platform) for c in panel.codes}
        self.name = {v: k for k, v in self.code.items()}
        self.cash, self.pos, self.fills = BROKER.cash, {}, []

    def raw(self, field, code, day):
        return float(self.p[field].loc[day, self.name[code]])

    def fill(self, code, delta, day, price=None):
        s = BROKER.slippage
        o, hi, lo = (self.raw(f, code, day) for f in ("raw_open", "high", "low"))
        if price is None:
            price = min(o * (1 + s), hi) if delta > 0 else max(o * (1 - s), lo)
        value = abs(delta) * price
        comm = max(value * BROKER.commission, BROKER.min_commission)
        if delta > 0:
            self.cash -= value + comm
        else:
            comm += value * BROKER.stamp_duty
            self.cash += value - comm
        self.pos[code] = self.pos.get(code, 0) + delta
        assert self.pos[code] >= 0, "short position"
        if self.pos[code] == 0:
            del self.pos[code]
        self.fills.append({"time": day, "symbol": self.name[code], "side": "buy" if delta > 0 else "sell",
                           "size": abs(delta), "price": price, "commission": comm})

    def value_before_open(self, day):
        """Backtrader 在开盘时的 getvalue()：按上一交易日收盘价估值"""
        i = self.p.calendar.get_loc(day)
        return self.cash + sum(s * self.raw("raw_close", c, self.p.calendar[i - 1]) for c, s in self.pos.items())

    def orders(self):
        return pd.DataFrame(self.fills, columns=["time", "symbol", "side", "size", "price", "commission"])


def long_frame(panel, sim, codes, days, mapping, date_col, code_col, date_fmt=lambda d: d, skip_suspended=False):
    """平台返回的长表：每行一个 日期 × 代码；停牌日成交量为 0（skip_suspended=True 时没有这一行）"""
    rows = []
    for d in days:
        for c in codes:
            n = sim.name[c]
            if skip_suspended and not panel.tradable.loc[d, n]:
                continue
            r = {date_col: date_fmt(d), code_col: c}
            for theirs, ours in mapping.items():
                if ours == "volume":
                    r[theirs] = panel["volume"].loc[d, n] if panel.tradable.loc[d, n] else 0.0
                else:
                    r[theirs] = panel[ours].loc[d, n]
            rows.append(r)
    return pd.DataFrame(rows, columns=[date_col, code_col] + list(mapping))


def _load(code, modules):
    for name, mod in modules.items():
        sys.modules[name] = mod
    ns = {"__name__": "exported_selection", **NUMPY_SHADOWS}     # 模拟聚宽全局里 numpy 的 sum / any / all 等
    exec(compile(code, "exported.py", "exec"), ns)
    return ns


def _window(cal, end, count=None, start=None):
    days = cal[cal <= pd.Timestamp(end)]
    if start is not None:
        return days[days >= pd.Timestamp(start)]
    return days[-count:]


def run_joinquant(code, panel, ipo, start):
    sim = Sim(panel, "joinquant")
    cal = panel.calendar
    jq = types.ModuleType("jqdata")
    registered, logs, state = [], [], {}

    class Pos:
        def __init__(self, n):
            self.total_amount = n

    class Portfolio:
        positions = property(lambda self: {c: Pos(n) for c, n in sim.pos.items()})
        available_cash = property(lambda self: sim.cash)
        total_value = property(lambda self: sim.value_before_open(state["today"]))

    def get_price(chunk, end_date, count, frequency, fields, skip_paused, fq, panel):
        assert fq == "pre" and not skip_paused and panel is False and frequency == "daily"
        mapping = {"open": "open", "close": "close", "volume": "volume", "money": "amount"}
        return long_frame(P, sim, chunk, _window(cal, end_date, count), {f: mapping[f] for f in fields}, "time", "code")

    def get_valuation(chunk, end_date, count, fields):
        assert len(chunk) * count <= 4000
        mapping = {"pe_ratio": "pe", "pb_ratio": "pb", "ps_ratio": "ps", "turnover_ratio": "turnover",
                   "market_cap": "mcap"}
        return long_frame(P, sim, chunk, _window(cal, end_date, count), {f: mapping[f] for f in fields},
                          "day", "code", date_fmt=lambda d: d.date())

    def get_extras(info, codes, end_date, count, df):
        assert info == "is_st" and df
        days = _window(cal, end_date, count)
        return pd.DataFrame({c: P.is_st.loc[days, sim.name[c]].values for c in codes}, index=days)

    def _protected(style):
        """聚宽：科创板市价单必须带 0 < 保护价 < 1 万元（2026-09-30 实测拒单）"""
        assert style is not None and 0 < style.limit_price < 10000

    def get_current_data():
        day = state["today"]
        out = {}
        for c, n in sim.code.items():
            o = float(P["raw_open"].loc[day, c])
            out[n] = types.SimpleNamespace(paused=not P.tradable.loc[day, c], day_open=o,
                                           high_limit=o + (1 if P.can_buy.loc[day, c] else 0),
                                           low_limit=o - (1 if P.can_sell.loc[day, c] else 0))
        return out

    P = panel
    noop = lambda *a, **k: None  # noqa: E731
    jq.__dict__.update(
        get_price=get_price, get_valuation=get_valuation, get_extras=get_extras, get_current_data=get_current_data,
        get_trade_days=lambda start_date, end_date: [d.date() for d in _window(cal, end_date, start=start_date)],
        get_index_stocks=lambda index, date: [sim.code[c] for c in P.codes if P.member.loc[date, c]],
        get_all_securities=lambda types: pd.DataFrame({"start_date": [ipo[sim.name[c]].date() for c in sim.name]},
                                                      index=list(sim.name)),
        order=lambda c, n, style=None: (_protected(style), sim.fill(c, n, state["today"])),
        order_target=lambda c, n, style=None: (_protected(style), sim.fill(c, n - sim.pos[c], state["today"])),
        MarketOrderStyle=lambda limit_price: types.SimpleNamespace(limit_price=limit_price),
        set_benchmark=noop, set_option=noop, set_order_cost=noop, OrderCost=lambda **k: k, set_slippage=noop,
        PriceRelatedSlippage=lambda x: x, run_daily=lambda f, time: registered.append((f, time)),
        g=types.SimpleNamespace(), log=types.SimpleNamespace(info=logs.append, set_level=noop))
    ns = _load(code, {"jqdata": jq})
    ctx = types.SimpleNamespace(portfolio=Portfolio(), previous_date=None, current_dt=None)
    ns["initialize"](ctx)
    (func, when), = registered
    assert when == "open"
    for i, day in enumerate(cal):
        if day < pd.Timestamp(start):
            continue
        state["today"] = day
        ctx.previous_date, ctx.current_dt = cal[i - 1].date(), day.to_pydatetime().replace(hour=9, minute=30)
        func(ctx)
    return sim.orders(), logs


def run_qmt(code, panel, ipo, start):
    sim = Sim(panel, "qmt")
    cal = panel.calendar
    labels = [d.strftime("%Y%m%d") for d in cal]
    P = panel

    def st_spans(name):
        s = P.is_st[name]
        spans, begin = [], None
        for d, v in s.items():
            if v and begin is None:
                begin = d
            if not v and begin is not None:
                spans.append([begin.strftime("%Y%m%d"), (d - pd.Timedelta(days=1)).strftime("%Y%m%d")])
                begin = None
        if begin is not None:
            spans.append([begin.strftime("%Y%m%d"), "20991231"])
        return {"ST": spans} if spans else {}

    class Ctx:
        do_back_test = True
        barpos = 0

        def set_universe(self, codes):
            assert codes == ["000300.SH"]

        def set_commission(self, kind, values):
            assert kind == 0 and len(values) == 6

        def get_bar_timetag(self, pos):
            return pos

        def is_last_bar(self):
            return self.barpos == len(cal) - 1

        def get_trading_dates(self, stock, start_date, end_date, count, period):
            assert count == -1 and period == "1d"
            return [x for x in labels if start_date <= x <= end_date]

        def get_sector(self, index, ms):
            day = pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(None) + pd.Timedelta(hours=12)
            day = day.normalize()
            return [sim.code[c] for c in P.codes if P.member.loc[day, c]]

        def get_instrumentdetail(self, code):
            return {"OpenDate": ipo[sim.name[code]].strftime("%Y%m%d")}

        def get_his_st_data(self, code):
            return st_spans(sim.name[code])

        def get_market_data_ex(self, fields, codes, period, start_time, end_time, dividend_type, fill_data):
            assert period == "1d" and fill_data is False
            src = {"open": "open", "close": "close", "volume": "volume", "amount": "amount"} \
                if dividend_type == "front" else {"open": "raw_open", "high": "high", "low": "low",
                                                  "preClose": "raw_preclose"}
            days = [d for d, x in zip(cal, labels) if start_time <= x <= end_time]
            out = {}
            for c in codes:
                n = sim.name[c]
                ok = [d for d in days if P.tradable.loc[d, n]]          # 停牌日没有数据
                out[c] = pd.DataFrame({f: [float(P[src[f]].loc[d, n]) for d in ok] for f in fields},
                                      index=[d.strftime("%Y%m%d") for d in ok])
            return out

    ctx = Ctx()
    today = {}
    ns = _load(code, {})
    ns.update(timetag_to_datetime=lambda tag, fmt: labels[tag],
              order_shares=lambda c, n, style, price, context: sim.fill(c, n, today["day"], price=price))
    ns["init"](ctx)
    logs = []
    ns["print"] = logs.append
    for i, day in enumerate(cal):
        ctx.barpos, today["day"] = i, day
        ns["handlebar"](ctx)
        ns["handlebar"](ctx)          # 同一根 K 线重复调用不应重复下单
    assert ns["G"].book["cash"] == pytest.approx(sim.cash)
    assert ns["G"].book["pos"] == sim.pos
    return sim.orders(), logs


def run_myquant(code, panel, ipo, start):
    sim = Sim(panel, "myquant")
    cal = panel.calendar
    P = panel
    queue, logs = [], []
    gm, api = types.ModuleType("gm"), types.ModuleType("gm.api")
    gm.api = api

    class Account:
        cash = property(lambda self: {"available": sim.cash, "nav": sim.cash + sum(
            s * sim.raw("raw_close", c, state["today"]) for c, s in sim.pos.items())})

        def positions(self):
            return [{"symbol": c, "volume": s} for c, s in sim.pos.items()]

    state = {}

    def history(symbol, frequency, start_time, end_time, fields, adjust, df, adjust_end_time=None):
        assert frequency == "1d" and df
        codes = symbol.split(",")
        names = [f for f in fields.split(",") if f not in ("symbol", "eob")]
        if adjust == "NONE":
            mapping = {"close": "raw_close"}
        else:
            assert adjust == "PREV" and adjust_end_time == end_time[:10]
            mapping = {"open": "open", "close": "close", "volume": "volume", "amount": "amount"}
        return long_frame(P, sim, codes, _window(cal, end_time[:10], start=start_time[:10]),
                          {f: mapping[f] for f in names}, "eob", "symbol", skip_suspended=True)

    def daily(mapping):
        def fn(symbol, fields, start_date, end_date, df):
            out = long_frame(P, sim, [symbol], _window(cal, end_date, start=start_date),
                             {f: mapping[f] for f in fields.split(",")}, "trade_date", "symbol",
                             date_fmt=lambda d: d.strftime("%Y-%m-%d"))
            return out.drop(columns=["symbol"])
        return fn

    def order_volume(symbol, volume, order_type, side, position_effect):
        assert order_type == "MARKET" and (side == "BUY") == (position_effect == "OPEN")
        queue.append((symbol, volume if side == "BUY" else -volume))

    def history_instruments(symbols, fields, start_date, end_date, df):
        d = pd.Timestamp(end_date)
        return pd.DataFrame([{"symbol": c, "trade_date": d, "sec_level": 2 if P.is_st.loc[d, sim.name[c]] else 1}
                             for c in symbols.split(",")])

    subscribed = {}
    str_days = [str(d.date()) for d in cal]
    api.__dict__.update(
        subscribe=lambda **k: subscribed.update(k), history=history, order_volume=order_volume, run=lambda **k: None,
        stk_get_daily_valuation=daily({"pe_ttm": "pe", "pb_mrq": "pb", "ps_ttm": "ps"}),
        stk_get_daily_basic=daily({"turnrate": "turnover"}), stk_get_daily_mktvalue=daily({"tot_mv": "mcap"}),
        stk_get_index_constituents=lambda index, trade_date: pd.DataFrame(
            {"symbol": [sim.code[c] for c in P.codes if P.member.loc[pd.Timestamp(trade_date), c]]}),
        get_symbol_infos=lambda sec_type1, symbols, df: pd.DataFrame(
            [{"symbol": c, "listed_date": ipo[sim.name[c]]} for c in symbols]),
        get_history_instruments=history_instruments,
        get_trading_dates=lambda exchange, start_date, end_date: [d for d in str_days if start_date <= d <= end_date],
        get_next_trading_date=lambda exchange, date: next((d for d in str_days if d > date), "2099-01-01"),
        ADJUST_PREV="PREV", ADJUST_NONE="NONE", OrderType_Market="MARKET", OrderSide_Buy="BUY", OrderSide_Sell="SELL",
        PositionEffect_Open="OPEN", PositionEffect_Close="CLOSE", MODE_BACKTEST=2)
    ns = _load(code, {"gm": gm, "gm.api": api})
    ns["print"] = logs.append
    ctx = types.SimpleNamespace(account=lambda: Account(), now=None)
    ns["init"](ctx)
    assert subscribed == {"symbols": "SHSE.000300", "frequency": "1d", "count": 1}
    for i, day in enumerate(cal):
        # 前一天收盘的委托在今天开盘成交（backtest_match_mode=0）；涨跌停/停牌时掘金拒绝
        for symbol, delta in queue:
            n = sim.name[symbol]
            ok = P.can_buy.loc[day, n] if delta > 0 else P.can_sell.loc[day, n]
            if ok and (delta > 0 or sim.pos.get(symbol, 0) >= -delta):
                cost = abs(delta) * sim.raw("raw_open", symbol, day) * (1 + BROKER.slippage) * 1.001
                if delta < 0 or cost <= sim.cash:
                    sim.fill(symbol, delta, day)
        queue.clear()
        if day < pd.Timestamp(start) - pd.Timedelta(days=5):
            continue
        state["today"] = day
        ctx.now = day.to_pydatetime().replace(hour=15)
        ns["on_bar"](ctx, [])
    return sim.orders(), logs


RUNNERS = {"joinquant": run_joinquant, "myquant": run_myquant, "qmt": run_qmt}


def compare_with_simplequant(exported, panel, spec, start):
    ours = run_selection(panel, spec, BROKER, start=start).orders
    assert len(ours) > 10
    key = ["time", "symbol", "side"]
    ours = ours.sort_values(key, kind="stable").reset_index(drop=True)
    exported = exported.sort_values(key, kind="stable").reset_index(drop=True)
    pd.testing.assert_frame_equal(exported[["time", "symbol", "side", "size"]], ours[["time", "symbol", "side", "size"]],
                                  check_dtype=False)
    np.testing.assert_allclose(exported["price"], ours["price"], rtol=1e-12)
    np.testing.assert_allclose(exported["commission"], ours["commission"], rtol=1e-9)


def blocked_panel(panel):
    """部分股票隔几天就开盘跌停（卖不出）或开盘涨停（买不进）：改开盘价，再按涨跌停规则重新判断"""
    p = copy.copy(panel)
    f = {k: v.copy() for k, v in panel.fields.items()}
    pre = f["raw_preclose"]
    rows = np.arange(len(pre))
    for cols, every, mult in ((slice(0, 4), 7, 0.9), (slice(4, 8), 5, 1.1)):
        hit = np.zeros(pre.shape, dtype=bool)
        hit[200:480, cols] = (rows[200:480, None] % every == 0)
        hit &= panel.tradable.values & ~panel.is_st.values
        limit = (pre * mult).round(2)
        for k in ("raw_open", "open"):
            f[k] = f[k].mask(hit, limit)
    f["high"] = np.maximum(f["high"], f["open"])
    f["low"] = np.minimum(f["low"], f["open"])
    p.fields = f
    p.can_buy, p.can_sell = trading_constraints(f["raw_open"], f["raw_preclose"], panel.tradable, panel.is_st)
    assert (~p.can_buy).sum().sum() > 50 and (~p.can_sell).sum().sum() > 50
    return p


def export(platform, panel, spec, start, follow=False, expected_spec=None):
    sched = build_schedule(panel, expected_spec or spec, start=start)
    code = selection_platform_script(platform, spec, BROKER_DICT, start, str(panel.calendar[-1].date()),
                                     str(panel.calendar[0].date()), expected=sched.picks, follow=follow, title="test")
    ast.parse(code, feature_version=(3, 6))                  # 聚宽 / QMT 的 Python 3.6 能解析
    assert "import simplequant" not in code and "from simplequant" not in code
    if platform == "qmt":
        assert code.startswith("#coding:gbk") and encode_script(code, "qmt").decode("gbk") == code
    return code


def overlap_lines(logs):
    return [ln for ln in logs if "重合" in ln]


@pytest.mark.parametrize("platform,name", [("joinquant", "price_manual"), ("joinquant", "value_liquidity"),
                                           ("joinquant", "size_neutral"), ("joinquant", "ic"),
                                           ("qmt", "price_manual"), ("qmt", "ic")])
def test_exported_trades_match_run_selection(panel_ipo, platform, name):
    """聚宽、QMT：开盘时知道开盘价和涨跌停，每一笔委托都应与 SimpleQuant 相同"""
    panel, ipo = panel_ipo
    panel = blocked_panel(panel)
    spec = full_spec(name)
    if platform == "qmt" and name == "ic":
        spec["factors"] = [f for f in spec["factors"] if f["key"] != "ep"]
    start = str(panel.calendar[150].date())
    orders, logs = RUNNERS[platform](export(platform, panel, spec, start), panel, ipo, start)
    compare_with_simplequant(orders, panel, spec, start)
    lines = overlap_lines(logs)
    assert len(lines) > 5 and all("重合 8/8" in ln for ln in lines), lines


@pytest.mark.parametrize("name", ["value_liquidity", "size_neutral", "icir"])
def test_myquant_picks_match(panel_ipo, name):
    """掘金：收盘下单、次日开盘成交，股数按收盘价估算，只比较名单与持仓"""
    panel, ipo = panel_ipo
    spec = full_spec(name)
    start = str(panel.calendar[150].date())
    orders, logs = run_myquant(export("myquant", panel, spec, start), panel, ipo, start)
    lines = overlap_lines(logs)
    assert len(lines) > 5 and all("重合 8/8" in ln for ln in lines), lines
    assert len(orders) > 10


@pytest.mark.parametrize("platform", list(RUNNERS))
def test_follow_mode_supports_any_factor(panel_ipo, platform):
    """按名单调仓：行业中性化 / 财务因子也能导出，调仓与 SimpleQuant 相同"""
    panel, ipo = panel_ipo
    base = full_spec("price_manual")
    spec = {**base, "factors": base["factors"] + [{"key": "roe", "weight": 1}],
            "neutralize": {"industry": True, "size": False}}
    start = str(panel.calendar[150].date())
    code = export(platform, panel, spec, start, follow=True, expected_spec=base)
    orders, logs = RUNNERS[platform](code, panel, ipo, start)
    assert any("按 SimpleQuant 名单调仓" in ln for ln in logs)
    if platform != "myquant":
        compare_with_simplequant(orders, panel, base, start)


def test_unsupported_selection_exports():
    from simplequant.export import check_selection_exportable
    base = full_spec("price_manual")
    with pytest.raises(ValueError, match="财务"):
        check_selection_exportable({**base, "factors": [{"key": "roe"}]}, "joinquant")
    with pytest.raises(ValueError, match="行业"):
        check_selection_exportable({**base, "neutralize": {"industry": True}}, "myquant")
    with pytest.raises(ValueError, match="QMT"):
        check_selection_exportable(full_spec("value_liquidity"), "qmt")
    check_selection_exportable(full_spec("value_liquidity"), "joinquant")
    check_selection_exportable({**base, "factors": [{"key": "roe"}]}, "qmt", follow=True)
    with pytest.raises(ValueError):
        selection_platform_script("joinquant", base, BROKER_DICT, "2021-01-01", "2022-01-01", "2020-01-01",
                                  follow=True)
