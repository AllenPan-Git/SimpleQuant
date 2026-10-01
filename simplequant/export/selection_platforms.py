"""
把多因子选股策略导出成聚宽 / 掘金 / QMT 的策略代码（第二期）

两种方式：
- 平台计算：嵌入 selection_core.py，在平台上取历史成分股、行情、估值数据，按 SimpleQuant 的算法选股；
  可以一直往后跑（模拟盘）。只支持价量、估值、换手、市值因子和市值中性化（财务因子、行业分类各平台口径不同）。
- 按名单调仓：直接写入 SimpleQuant 回测算出的每期名单，平台只负责调仓；支持所有因子，但只能跑到数据截止日，
  适合单独对比成交与成本。
两种方式都会写入 SimpleQuant 的每期名单，平台运行时打印「与 SimpleQuant 重合几只」。
"""

import datetime as dt
import inspect
import json
import pprint

from .. import strategies
from ..i18n import L, pick
from . import selection_core
from .platforms import PLATFORMS, platform_code

INDEX_CODE = {"hs300": "000300", "zz500": "000905", "sz50": "000016"}
FIN_FACTORS = ("roe", "gross_margin", "net_margin", "np_yoy", "rev_yoy")
QMT_UNSUPPORTED = ("ep", "bp", "sp", "size", "turn20")


def check_selection_exportable(spec: dict, platform: str, follow: bool = False, lang: str = "zh") -> None:
    if follow:
        return
    keys = [f["key"] for f in spec["factors"]]
    custom = [k for k in keys if k.startswith("u_")]
    if custom:
        raise ValueError(pick(L("策略使用了自定义因子，无法自动转换为平台代码，请使用「按名单调仓」方式导出",
                                "Custom factors cannot be translated for platforms; use the 'follow picks' export instead"),
                              lang))
    fin = [k for k in keys if k in FIN_FACTORS]
    if fin:
        raise ValueError(pick(L(f"用到了财务因子（{', '.join(fin)}），各平台的财务数据口径和公告日对齐方式不同，"
                                "暂不支持在平台上计算；可以用「按名单调仓」导出",
                                f"Financial factors ({', '.join(fin)}) differ across platforms and can't be computed "
                                "there yet; use the 'follow picks' export instead"), lang))
    if (spec.get("neutralize") or {}).get("industry"):
        raise ValueError(pick(L("行业中性化采用 BaoStock 行业分类，与各平台不同，暂不支持在平台上计算，请使用「按名单调仓」方式导出",
                                "Industry neutralization uses BaoStock's classification and can't be reproduced on "
                                "platforms; use the 'follow picks' export instead"), lang))
    if platform == "qmt":
        bad = [k for k in keys if k in QMT_UNSUPPORTED]
        if bad or (spec.get("neutralize") or {}).get("size"):
            raise ValueError(pick(L("QMT 版本仅支持价量因子（收益、动量、波动率、Amihud），估值、换手、市值因子"
                                    "及市值中性化暂不支持，请使用「按名单调仓」方式导出",
                                    "The QMT export supports price/volume factors only; use the 'follow picks' "
                                    "export instead"), lang))
    if spec["universe"] not in INDEX_CODE:
        raise ValueError(f"Unknown universe / 未知股票池 {spec['universe']}")


def _core_source() -> str:
    lines = inspect.getsource(selection_core).splitlines()
    i = 1
    while '"""' not in lines[i]:
        i += 1
    body = [ln for ln in lines[i + 1:] if not ln.startswith(("import ", "from "))]
    return "\n".join(body).strip() + "\n"


NOTES = {
    "joinquant": """\
使用方法（聚宽免费注册即可回测，不需要券商账户）：
  1. 聚宽 → 我的策略 → 新建策略 → 股票策略，清空编辑器后粘贴本文件全部内容
  2. 回测设置：开始日期 {start}，结束日期 {end}，初始资金 {cash:,.0f}，频率「每天」
  3. 运行回测：日志里每个调仓日会打印选出的股票和「与 SimpleQuant 重合几只」；再对照收益曲线

与 SimpleQuant 的差异：
  - 成分股、行情、估值、ST 用聚宽的数据；估值口径（PE TTM 等）有细微差别，排名靠近 top_n 边界的股票可能互换
  - 按 IC 加权时，过去每天的成分股按当天查询（SimpleQuant 用每月一次的快照）
  - 分红在聚宽是现金，在 SimpleQuant 是后复权价格里的涨幅；总资产用聚宽开盘时的 total_value
  - 涨停买不进、跌停卖不出（之后每天重试）、停牌与 SimpleQuant 规则相同，由聚宽的涨跌停价判断""",
    "myquant": """\
使用方法（掘金量化免费注册即可回测，不需要券商账户）：
  1. 安装掘金终端并登录；新建一个空策略，记下策略 ID；在「系统设置 → 密钥管理」复制 token
  2. pip install gm；把本文件保存为 main.py，填入下方 run() 里的 strategy_id 和 token
  3. 运行 python main.py；回测区间 {start} ~ {end}，初始资金 {cash:,.0f}；控制台会打印每期名单与重合只数

与 SimpleQuant 的差异：
  - 在调仓日收盘选股、下单，backtest_match_mode=0 在下一交易日开盘价成交（与 SimpleQuant 相同）；
    但下单股数只能按调仓日的收盘价估算（SimpleQuant 用执行日开盘价），涨跌停/停牌由掘金撮合时拒绝，
    没卖掉的股票之后每天收盘重新下单
  - 估值用 stk_get_daily_valuation（pe_ttm / pb_mrq / ps_ttm），换手率用 stk_get_daily_basic（turnrate），
    市值用 stk_get_daily_mktvalue（tot_mv）；按 IC 加权时 ST 状态用信号日当天的
  - 掘金回测的佣金只有一个比例参数（没有最低 5 元、没有单独的印花税）""",
    "qmt": """\
使用方法（需要在开通了 QMT 的券商账户下使用；本文件为 GBK 编码，QMT 策略编辑器直接打开即可）：
  1. QMT → 模型研究 → 新建 Python 策略，清空后粘贴本文件全部内容（或直接导入本文件）
  2. 回测参数：主图品种选 {index}，周期「日线」，开始日期 {start}，结束日期 {end}，初始资金 {cash:,.0f}，
     滑点设为 0（滑点已经算在下单价格里），手续费由 init 中的 set_commission 设置
  3. 运行回测；日志会打印每期名单与重合只数

与 SimpleQuant 的差异：
  - 成分股用 get_sector 按日期查询，ST 用 get_his_st_data，上市日期用 get_instrumentdetail 的 OpenDate
  - 每根日线用「到上一交易日为止」的数据选股，以当天开盘价（含滑点）限价下单；涨跌停按不复权昨收自行判断
  - 为了结果可复现，资金与持仓由本脚本按 SimpleQuant 的成交规则记账（不含分红）
  - 本脚本只在回测模式下下单；模拟/实盘模式下不做任何操作""",
}

FOLLOW_NOTE = """
【按名单调仓】本文件不在平台上计算因子，直接按下面 EXPECTED 里 SimpleQuant 算好的每期名单调仓，
只能回测到 {last}；用来单独对比平台和 SimpleQuant 的成交、费用和分红处理。
这种方式下名单与 SimpleQuant 完全相同，日志不打印「重合几只」，而是每个调仓日打印「按 SimpleQuant 名单调仓」；
请在日志里搜索 ERROR，确认没有委托失败。"""


def _header(platform, title, spec, start, end, broker, follow, expected, lang) -> str:
    coding = "#coding:gbk\n" if platform == "qmt" else "# -*- coding: utf-8 -*-\n"
    index = platform_code(INDEX_CODE[spec["universe"]], "index", platform)
    notes = NOTES[platform].format(start=start, end=end, cash=broker["cash"], index=index)
    if follow:
        notes += "\n" + FOLLOW_NOTE.format(last=max(expected) if expected else end)
    return f'''{coding}"""
{title} —— {pick(PLATFORMS[platform]["label"], "zh")}
由 SimpleQuant 导出于 {dt.datetime.now():%Y-%m-%d %H:%M}（Exported by SimpleQuant）

{strategies.describe(spec, lang)}

{notes}
"""
'''


PLATFORM_IMPORTS = {"joinquant": "from jqdata import *  # noqa: F401,F403\n",
                    "myquant": "from gm.api import *  # noqa: F401,F403\n", "qmt": ""}


def _dict_lines(d: dict) -> str:
    """每个键一行（名单可能有上百期）"""
    if not d:
        return "{}"
    lines = [f"    {json.dumps(k)}: {json.dumps(v)}," for k, v in d.items()]
    return "{\n" + "\n".join(lines) + "\n}"


def _settings(platform, spec, start, end, panel_start, broker, follow, expected) -> str:
    keep = ("cash", "commission", "min_commission", "stamp_duty", "slippage")
    spec = {k: spec[k] for k in ("kind", "universe", "factors", "top_n", "rebalance", "filters", "position_pct",
                                 "weighting", "ic_lookback", "neutralize") if k in spec}
    exp = {str(d)[:10]: [platform_code(c, "stock", platform) for c in codes] for d, codes in (expected or {}).items()}
    return f'''
import datetime
import math
import time
import warnings

import numpy as np
{PLATFORM_IMPORTS[platform]}
# 聚宽等平台的全局里 sum / any / all / min / max 等可能是 numpy 的同名函数（np.any(生成器) 恒为真），这里恢复成 Python 内置函数
from builtins import abs, all, any, max, min, round, sum  # noqa: E402,A004

# ============================== 设置 ==============================
SPEC = {pprint.pformat(spec, width=110, sort_dicts=False)}
INDEX = "{platform_code(INDEX_CODE[spec["universe"]], "index", platform)}"
# 数据起点（与 SimpleQuant 面板起点相同：「每 N 天调仓」从这天数起，按 IC 加权也只用这天之后的数据）
PANEL_START = "{panel_start}"
START = "{start}"          # 回测开始日，之前不调仓；请在平台上从这天开始回测
END = "{end}"
BROKER = {pprint.pformat({k: broker[k] for k in keep}, width=110, sort_dicts=False)}
# True：不计算因子，按 EXPECTED 里 SimpleQuant 的名单调仓
FOLLOW = {follow}
# SimpleQuant 回测的每期名单（信号日 → 股票，按得分从高到低），用于对照
EXPECTED = {_dict_lines(exp)}

'''


CORE_TITLE = "# ============================== SimpleQuant 选股核心（与平台无关） ==============================\n"


CHUNKS = '''

def _chunks(items, n):
    items = list(items)
    for i in range(0, len(items), max(1, n)):
        yield items[i:i + max(1, n)]
'''


JOINQUANT = r'''

# ============================== 聚宽对接 ==============================
VALUATION = {"pe": "pe_ratio", "pb": "pb_ratio", "ps": "ps_ratio", "turnover": "turnover_ratio", "mcap": "market_cap"}
PRICE = {"open": "open", "close": "close", "volume": "volume", "amount": "money"}


def initialize(context):
    set_benchmark(INDEX)
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    log.set_level("order", "error")
    set_order_cost(OrderCost(open_tax=0, close_tax=BROKER["stamp_duty"],
                             open_commission=BROKER["commission"], close_commission=BROKER["commission"],
                             close_today_commission=0, min_commission=BROKER["min_commission"]), type="stock")
    # 聚宽的滑点是买卖价差：买入价上浮一半、卖出价下调一半
    set_slippage(PriceRelatedSlippage(2 * BROKER["slippage"]), type="stock")
    g.sq_state = {"retry": []}
    g.sq_members = {}
    g.sq_ipo = None
    run_daily(sq_trade, time="open")


class _Data(object):
    def members(self, day):
        return get_index_stocks(INDEX, date=day)

    def ipo(self, codes):
        if g.sq_ipo is None:
            info = get_all_securities(types=["stock"])
            g.sq_ipo = dict((c, str(d)[:10]) for c, d in zip(info.index, info["start_date"]))
        return [g.sq_ipo.get(c) for c in codes]

    def bars(self, codes, dates, fields, full):
        out = dict((f, np.full((len(dates), len(codes)), NAN)) for f in fields)
        pos = dict((d, i) for i, d in enumerate(dates))
        col = dict((c, j) for j, c in enumerate(codes))
        price = dict((f, PRICE[f]) for f in fields if f in PRICE)
        for chunk in _chunks(codes, 100):
            df = get_price(chunk, end_date=dates[-1], count=len(dates), frequency="daily", fields=list(price.values()),
                           skip_paused=False, fq="pre", panel=False)
            sel_fill(out, df.to_dict("records"), "time", "code", price, pos, col)
        val = dict((f, VALUATION[f]) for f in fields if f in VALUATION)
        if val:
            count = len(dates) if full else min(25, len(dates))
            for chunk in _chunks(codes, 4000 // count):
                df = get_valuation(chunk, end_date=dates[-1], count=count, fields=list(val.values()))
                sel_fill(out, df.to_dict("records"), "day", "code", val, pos, col)
        return out

    def st(self, codes, dates, full):
        out = np.zeros((len(dates), len(codes)), dtype=bool)
        n = len(dates) if full else 1
        df = get_extras("is_st", codes, end_date=dates[-1], count=n, df=True)
        pos = dict((d, i) for i, d in enumerate(dates))
        for day, row in df.iterrows():
            i = pos.get(str(day)[:10])
            if i is not None:
                out[i] = [bool(row.get(c, False)) for c in codes]
        return out


def sq_trade(context):
    today = str(context.current_dt.date())
    calendar = [str(d) for d in get_trade_days(start_date=PANEL_START, end_date=context.previous_date)]
    if not calendar:
        return
    target = sel_target(SPEC, calendar, today, START, _Data(), g.sq_members, EXPECTED, FOLLOW, log.info)
    positions = context.portfolio.positions
    held = dict((c, positions[c].total_amount) for c in list(positions.keys()) if positions[c].total_amount > 0)
    if target is None and not g.sq_state["retry"]:
        return
    cur = get_current_data()
    market = {}
    for c in set(held) | set(target or []):
        d = cur[c]
        ok = not d.paused and d.day_open == d.day_open and d.day_open > 0
        market[c] = {"open": d.day_open, "can_buy": ok and d.day_open < d.high_limit - 0.001,
                     "can_sell": ok and d.day_open > d.low_limit + 0.001}
    book = {"cash": context.portfolio.available_cash, "value": context.portfolio.total_value, "pos": held}
    for code, delta, reason in sel_orders(target, book, market, g.sq_state, SPEC.get("position_pct", 95)):
        log.info("%s %s %d: %s" % ("买入 buy" if delta > 0 else "卖出 sell", code, abs(delta), reason))
        # 科创板市价单必须带保护价（否则聚宽拒单）；用当天涨跌停价作保护价，对其他股票没有影响
        if delta < 0:
            order_target(code, 0, MarketOrderStyle(cur[code].low_limit))
        else:
            order(code, delta, MarketOrderStyle(cur[code].high_limit))
'''

MYQUANT = r'''

# ============================== 掘金对接 ==============================
VALUATION = [("stk_get_daily_valuation", {"pe": "pe_ttm", "pb": "pb_mrq", "ps": "ps_ttm"}),
             ("stk_get_daily_basic", {"turnover": "turnrate"}),
             ("stk_get_daily_mktvalue", {"mcap": "tot_mv"})]
PRICE = {"open": "open", "close": "close", "volume": "volume", "amount": "amount"}


def init(context):
    context.sq_state = {"retry": [], "keep": None}
    context.sq_members = {}
    context.sq_ipo = {}
    # 每天收盘（指数日线到达）时选股、下单；backtest_match_mode=0 → 在下一交易日开盘价成交
    subscribe(symbols=INDEX, frequency="1d", count=1)


def _records(df):
    return [] if df is None or len(df) == 0 else df.to_dict("records")


class _Data(object):
    def __init__(self, context):
        self.context = context

    def members(self, day):
        try:
            return list(stk_get_index_constituents(index=INDEX, trade_date=day)["symbol"])
        except NameError:          # 旧版 gm SDK
            res = get_history_constituents(index=INDEX, start_date=day, end_date=day)
            return list(res[0]["constituents"].keys()) if res else []

    def ipo(self, codes):
        cache = self.context.sq_ipo
        todo = [c for c in codes if c not in cache]
        if todo:
            try:
                rows = _records(get_symbol_infos(sec_type1=1010, symbols=todo, df=True))
            except NameError:      # 旧版 gm SDK
                rows = _records(get_instrumentinfos(symbols=",".join(todo), fields="symbol,listed_date", df=True))
            for r in rows:
                cache[r["symbol"]] = str(r["listed_date"])[:10]
        return [cache.get(c) for c in codes]

    def bars(self, codes, dates, fields, full):
        out = dict((f, np.full((len(dates), len(codes)), NAN)) for f in fields)
        pos = dict((d, i) for i, d in enumerate(dates))
        col = dict((c, j) for j, c in enumerate(codes))
        price = dict((f, PRICE[f]) for f in fields if f in PRICE)
        for chunk in _chunks(codes, 50):
            df = history(symbol=",".join(chunk), frequency="1d", start_time=dates[0] + " 00:00:00",
                         end_time=dates[-1] + " 23:59:59", fields="symbol,eob," + ",".join(price.values()),
                         adjust=ADJUST_PREV, adjust_end_time=dates[-1], df=True)
            sel_fill(out, _records(df), "eob", "symbol", price, pos, col)
        for name, mapping in VALUATION:
            need = dict((k, v) for k, v in mapping.items() if k in fields)
            if not need:
                continue
            fn = globals().get(name)
            if fn is None:
                raise RuntimeError("gm SDK 版本太旧，没有 %s，请 pip install -U gm / please upgrade gm" % name)
            start = dates[0] if full else dates[-min(25, len(dates))]
            for c in codes:
                df = fn(symbol=c, fields=",".join(need.values()), start_date=start, end_date=dates[-1], df=True)
                sel_fill(out, [dict(r, symbol=c) for r in _records(df)], "trade_date", "symbol", need, pos, col)
        return out

    def st(self, codes, dates, full):
        """ST 状态用信号日当天的（sec_level：1 正常、2 ST、3 *ST）"""
        rows = _records(get_history_instruments(symbols=",".join(codes), fields="symbol,trade_date,sec_level",
                                                start_date=dates[-1], end_date=dates[-1], df=True))
        st = set(r["symbol"] for r in rows if r["sec_level"] in (2, 3))
        return np.array([[c in st for c in codes]] * len(dates), dtype=bool)


def on_bar(context, bars):
    t = context.now.strftime("%Y-%m-%d")
    today = str(get_next_trading_date(exchange="SHSE", date=t))[:10]
    calendar = [str(d)[:10] for d in get_trading_dates(exchange="SHSE", start_date=PANEL_START, end_date=t)]
    if not calendar or calendar[-1] != t:
        return
    state = context.sq_state
    target = sel_target(SPEC, calendar, today, START, _Data(context), context.sq_members, EXPECTED, FOLLOW, print)
    account = context.account()
    held = dict((p["symbol"], p["volume"]) for p in account.positions() if p["volume"] > 0)
    if target is not None:
        state["keep"] = list(target)
    # 之前没卖掉的（跌停/停牌时掘金会拒绝委托）：今天收盘再下单
    state["retry"] = sorted(c for c in held if state["keep"] is not None and c not in state["keep"]) \
        if target is None else []
    if target is None and not state["retry"]:
        return
    need = sorted(set(held) | set(target or []))
    close = {}
    for chunk in _chunks(need, 50):
        for r in _records(history(symbol=",".join(chunk), frequency="1d", start_time=t + " 00:00:00",
                                  end_time=t + " 23:59:59", fields="symbol,close", adjust=ADJUST_NONE, df=True)):
            close[r["symbol"]] = float(r["close"])
    market = dict((c, {"open": close.get(c, NAN), "can_buy": c in close, "can_sell": c in close}) for c in need)
    book = {"cash": account.cash["available"], "value": account.cash["nav"], "pos": held}
    for code, delta, reason in sel_orders(target, book, market, state, SPEC.get("position_pct", 95)):
        print("%s %s %s %d: %s" % (t, "买入 buy" if delta > 0 else "卖出 sell", code, abs(delta), reason))
        order_volume(symbol=code, volume=abs(delta), order_type=OrderType_Market,
                     side=OrderSide_Buy if delta > 0 else OrderSide_Sell,
                     position_effect=PositionEffect_Open if delta > 0 else PositionEffect_Close)


if __name__ == "__main__":
    run(strategy_id="请填写策略ID / your strategy id",
        filename="main.py",
        mode=MODE_BACKTEST,
        token="请填写你的 token / your token",
        backtest_start_time=START + " 08:00:00",
        backtest_end_time=END + " 16:00:00",
        backtest_adjust=ADJUST_NONE,
        backtest_initial_cash=BROKER["cash"],
        backtest_commission_ratio=BROKER["commission"],
        backtest_slippage_ratio=BROKER["slippage"],
        backtest_match_mode=0)
'''

QMT = r'''

# ============================== QMT 对接 ==============================
PRICE = {"open": "open", "close": "close", "volume": "volume", "amount": "amount"}


class _Global(object):
    """QMT 回测时 ContextInfo 上新设的属性在每根 K 线后会被回滚，跨 K 线的状态放在模块级对象里"""
    pass


G = _Global()


def _d8(day):
    return day.replace("-", "")


def _dash(d8):
    d8 = str(d8)[:8]
    return d8[:4] + "-" + d8[4:6] + "-" + d8[6:8]


def init(ContextInfo):
    ContextInfo.set_universe([INDEX])
    # 按比例收费：[买入印花税, 卖出印花税, 买入佣金, 卖出佣金, 平今佣金, 最低佣金]
    ContextInfo.set_commission(0, [0, BROKER["stamp_duty"], BROKER["commission"], BROKER["commission"],
                                   BROKER["commission"], BROKER["min_commission"]])
    G.state, G.members, G.last_day, G.st, G.ipo = {"retry": []}, {}, None, {}, {}
    G.last = {}           # 持仓最近一次的不复权价（停牌时估值用）
    G.book = {"cash": float(BROKER["cash"]), "pos": {}}


class _Data(object):
    def __init__(self, C):
        self.C = C

    def members(self, day):
        ms = int(time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").timetuple()) * 1000)
        return self.C.get_sector(INDEX, ms)

    def ipo(self, codes):
        for c in codes:
            if c not in G.ipo:
                d = (self.C.get_instrumentdetail(c) or {}).get("OpenDate")
                G.ipo[c] = _dash(d) if d and len(str(d)) >= 8 else None
        return [G.ipo[c] for c in codes]

    def bars(self, codes, dates, fields, full):
        out = dict((f, np.full((len(dates), len(codes)), NAN)) for f in fields)
        pos = dict((d, i) for i, d in enumerate(dates))
        src = [PRICE[f] for f in fields if f in PRICE]
        data = self.C.get_market_data_ex(src, codes, period="1d", start_time=_d8(dates[0]), end_time=_d8(dates[-1]),
                                         dividend_type="front", fill_data=False)
        for j, c in enumerate(codes):
            df = data.get(c)
            if df is None:
                continue
            for idx, row in zip(df.index, df.to_dict("records")):
                i = pos.get(_dash(idx))
                if i is not None:
                    for f in fields:
                        if f in PRICE:
                            out[f][i, j] = float(row[PRICE[f]])
        return out

    def st(self, codes, dates, full):
        return np.array([[_is_st(self.C, c, d) for c in codes] for d in dates], dtype=bool)


def _is_st(C, code, day):
    if code not in G.st:
        G.st[code] = C.get_his_st_data(code) or {}
    d8 = _d8(day)
    return any(str(a)[:8] <= d8 <= str(b)[:8] for spans in G.st[code].values() for a, b in spans)


def _limit_pct(code, st, day):
    """涨跌停幅度（与 SimpleQuant 相同）：主板 10%、ST 5%、创业板（2020-08-24 起）/科创板 20%、北交所 30%"""
    num = code[:6]
    if code.endswith(".BJ") or num[0] in "48":
        return 0.30
    if num.startswith("688") or (num.startswith(("300", "301")) and day >= "2020-08-24"):
        return 0.20
    return 0.05 if st else 0.10


def _fill(code, delta, price):
    """按 SimpleQuant 的成本规则记账（佣金按成交额、最低 min_commission；卖出另收印花税）"""
    value = abs(delta) * price
    comm = max(value * BROKER["commission"], BROKER["min_commission"])
    if delta > 0:
        G.book["cash"] -= value + comm
    else:
        G.book["cash"] += value - comm - value * BROKER["stamp_duty"]
    G.book["pos"][code] = G.book["pos"].get(code, 0) + delta
    if G.book["pos"][code] <= 0:
        del G.book["pos"][code]


def handlebar(ContextInfo):
    C = ContextInfo
    if not C.do_back_test:
        if C.is_last_bar():
            print("SimpleQuant 导出的策略只在回测模式下下单 / backtest mode only")
        return
    day8 = timetag_to_datetime(C.get_bar_timetag(C.barpos), "%Y%m%d")
    today = _dash(day8)
    if today < START or day8 == G.last_day:
        return
    G.last_day = day8
    calendar = [_dash(d) for d in C.get_trading_dates(INDEX, _d8(PANEL_START), day8, -1, "1d")]
    calendar = [d for d in calendar if d < today]
    if not calendar:
        return
    target = sel_target(SPEC, calendar, today, START, _Data(C), G.members, EXPECTED, FOLLOW, print)
    held = dict(G.book["pos"])
    if target is None and not G.state["retry"]:
        return
    need = sorted(set(held) | set(target or []))
    raw = C.get_market_data_ex(["open", "high", "low", "preClose"], need, period="1d", start_time=day8,
                               end_time=day8, dividend_type="none", fill_data=False)
    market, bars, value = {}, {}, G.book["cash"]
    for c in need:
        df = raw.get(c)
        rows = [] if df is None else [r for idx, r in zip(df.index, df.to_dict("records")) if str(idx)[:8] == day8]
        bar = rows[0] if rows else None
        if bar is None or not bar["open"] > 0:          # 停牌
            market[c] = {"open": NAN, "can_buy": False, "can_sell": False}
            continue
        bars[c] = bar
        pct = _limit_pct(c, _is_st(C, c, today), today)
        up, down = round(bar["preClose"] * (1 + pct), 2), round(bar["preClose"] * (1 - pct), 2)
        market[c] = {"open": bar["open"], "can_buy": bar["open"] < up - 0.001, "can_sell": bar["open"] > down + 0.001}
        value += held.get(c, 0) * bar["preClose"]
    for c, s in held.items():                           # 停牌的持仓按停牌前的价格估值
        if c not in bars:
            value += s * G.last.get(c, 0.0)
    G.last.update((c, b["preClose"]) for c, b in bars.items())
    book = {"cash": G.book["cash"], "value": value, "pos": held}
    slip = BROKER["slippage"]
    for code, delta, reason in sel_orders(target, book, market, G.state, SPEC.get("position_pct", 95)):
        bar = bars[code]
        price = min(bar["open"] * (1 + slip), bar["high"]) if delta > 0 else max(bar["open"] * (1 - slip), bar["low"])
        print("%s %s %s %d @ %.3f: %s" % (today, "买入 buy" if delta > 0 else "卖出 sell", code, abs(delta), price,
                                         reason))
        order_shares(code, delta, "fix", price, C)
        _fill(code, delta, price)
'''

ADAPTERS = {"joinquant": JOINQUANT, "myquant": MYQUANT, "qmt": QMT}


def selection_platform_script(platform: str, spec: dict, broker: dict, start: str, end: str, panel_start: str,
                              expected: dict | None = None, follow: bool = False,
                              title: str = "SimpleQuant selection", lang: str = "zh") -> str:
    """
    :param expected: SimpleQuant 回测的每期名单 {信号日: [BaoStock 代码如 sh.600000]}；follow=True 时必需
    :param panel_start: SimpleQuant 选股面板的起点（因子预热、每 N 天调仓的起算日）
    """
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform / 未知平台 {platform}")
    if follow and not expected:
        raise ValueError(pick(L("按名单调仓方式需先在选股页运行回测", "Run the selection backtest first"), lang))
    check_selection_exportable(spec, platform, follow, lang)
    return (_header(platform, title, spec, start, end, broker, follow, expected, lang)
            + _settings(platform, spec, start, end, panel_start, broker, follow, expected)
            + CORE_TITLE + _core_source() + CHUNKS + ADAPTERS[platform])
