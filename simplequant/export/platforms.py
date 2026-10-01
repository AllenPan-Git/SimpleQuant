"""
把模板 / 规则策略导出成聚宽（JoinQuant）、掘金（MyQuant / gm）、迅投 QMT 能直接导入运行的策略代码

结构：嵌入平台无关的信号核心（signal_core.py，指标与下单数量与 SimpleQuant 一致）+ 每个平台一层很薄的对接代码
（取到上一交易日为止的日线、查持仓、下单）。时序与 SimpleQuant 相同：用上一交易日收盘的数据产生信号，当天开盘成交。

限制（第一期）：只支持日线；不支持估值/换手类因子（各平台取法不同）；多因子选股另行导出。
"""

import datetime as dt
import inspect
import pprint
import re

from .. import strategies
from ..engine.market import t0_names
from ..i18n import L, pick
from ..rules.schema import required_columns
from . import signal_core

PLATFORMS = {
    "joinquant": {"label": L("聚宽 JoinQuant", "JoinQuant"), "ext": "py", "encoding": "utf-8"},
    "myquant": {"label": L("掘金 MyQuant", "MyQuant (gm)"), "ext": "py", "encoding": "utf-8"},
    "qmt": {"label": L("迅投 QMT", "QMT"), "ext": "py", "encoding": "gbk"},     # QMT 策略编辑器按 GBK 读取
}

_EXCHANGE_SUFFIX = {
    "joinquant": {"SH": ".XSHG", "SZ": ".XSHE", "BJ": ".BJSE"},
    "qmt": {"SH": ".SH", "SZ": ".SZ", "BJ": ".BJ"},
}
_EXCHANGE_PREFIX = {"myquant": {"SH": "SHSE.", "SZ": "SZSE.", "BJ": "BJSE."}}


def exchange_of(symbol: str, asset: str = "") -> str:
    """6 位代码 → 交易所（SH / SZ / BJ）"""
    s = re.sub(r"\D", "", symbol)
    low = symbol.lower()
    if low.startswith(("sh", "sz", "bj")):
        return low[:2].upper()
    if len(s) != 6:
        raise ValueError(f"Cannot tell the exchange of '{symbol}'; platform export needs a 6-digit code / "
                         f"无法识别「{symbol}」的交易所：导出到平台需要 6 位证券代码")
    if asset == "index":
        return "SZ" if s.startswith("399") else "BJ" if s.startswith("899") else "SH"
    if s[:2] == "11" or s[0] in "569":          # 沪市：股票 6、ETF 5、B 股 9、可转债 11x
        return "SH"
    if s[0] in "48":
        return "BJ"
    return "SZ"


def platform_code(symbol: str, asset: str, platform: str) -> str:
    s = re.sub(r"\D", "", symbol)
    ex = exchange_of(symbol, asset)
    if platform in _EXCHANGE_PREFIX:
        return _EXCHANGE_PREFIX[platform][ex] + s
    return s + _EXCHANGE_SUFFIX[platform][ex]


def check_exportable(spec: dict, lang: str = "zh") -> None:
    if spec.get("kind") == "selection":
        raise ValueError(pick(L("多因子选股策略暂不支持从此处导出至平台，请在选股页导出",
                                "Selection strategies cannot be exported to platforms here; use the Stock selection page"), lang))
    if spec.get("kind") == "code":
        raise ValueError(pick(L("自定义代码策略无法自动转换为平台代码，请使用「导出 Python 脚本」",
                                "Custom-code strategies cannot be translated for platforms; use \"Export Python script\" instead"),
                              lang))
    if spec.get("kind") == "rule" and required_columns(spec["rule"]):
        cols = ", ".join(sorted(required_columns(spec["rule"])))
        raise ValueError(pick(L(f"规则用到了估值、换手或利率数据（{cols}），各平台取法不同，暂不支持导出到平台",
                                f"The rule uses valuation, turnover or rates data ({cols}); not supported for platform export yet"),
                              lang))


def _core_source() -> str:
    """signal_core.py 去掉模块文档字符串和 import"""
    lines = inspect.getsource(signal_core).splitlines()
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
  3. 运行回测，把成交记录与 SimpleQuant 回测页的「成交记录」对照

与 SimpleQuant 的差异：
  - 聚宽用「动态前复权」价格计算指标；比值类指标（均线交叉、RSI、涨幅等）不受影响，
    与固定数值比较的绝对值类条件（如 MACD柱 < -0.5、收盘价 > 10）可能在个别日子不同
  - 开盘按市价成交，滑点按聚宽的价差模型设置为同等水平；停牌、涨跌停无法成交时聚宽会撤单
  - 持仓收益率（止损/止盈）用聚宽的持仓成本 avg_cost 计算""",
    "myquant": """\
使用方法（掘金量化免费注册即可回测，不需要券商账户）：
  1. 安装掘金终端并登录；新建一个空策略，记下策略 ID；在「系统设置 → 密钥管理」复制 token
  2. pip install gm；把本文件保存为 main.py，填入下方 run() 里的 strategy_id 和 token
  3. 运行 python main.py，回测结果在掘金终端里查看；回测区间 {start} ~ {end}，初始资金 {cash:,.0f}

与 SimpleQuant 的差异：
  - 在每天收盘的日线上产生信号，用 backtest_match_mode=0 在下一根日线开盘价成交（与 SimpleQuant 相同）
  - 掘金用前复权（ADJUST_PREV，复权基准为当天）价格计算指标；绝对值类条件可能在个别日子不同
  - 掘金回测的佣金只有一个比例参数（没有最低 5 元、没有单独的印花税），小资金时成本略低
  - 持仓收益率（止损/止盈）用掘金的持仓均价 vwap 计算""",
    "qmt": """\
使用方法（需要在开通了 QMT 的券商账户下使用；本文件为 GBK 编码，QMT 策略编辑器直接打开即可）：
  1. QMT → 模型研究 → 新建 Python 策略，清空后粘贴本文件全部内容（或直接导入本文件）
  2. 回测参数：周期「日线」，复权方式「前复权」，开始日期 {start}，结束日期 {end}，
     初始资金 {cash:,.0f}，滑点设为 0（滑点已经算在下单价格里），手续费由 init 中的 set_commission 设置
  3. 运行回测，对照 SimpleQuant 的成交记录

与 SimpleQuant 的差异：
  - 每根日线用「到上一交易日为止」的数据计算信号，以当天开盘价（含滑点）限价下单，等同于 SimpleQuant 的次日开盘成交
  - QMT 用前复权价格计算指标；绝对值类条件可能在个别日子不同
  - 为了结果可复现，回测时的资金与持仓由本脚本按 SimpleQuant 的成交规则记账
  - 本脚本只在回测模式下下单；模拟/实盘模式下不做任何操作（实盘对接需要风控，属于后续步骤）""",
}


def _header(platform: str, title: str, spec: dict, start: str, end: str, broker: dict, lang: str) -> str:
    coding = "#coding:gbk\n" if platform == "qmt" else "# -*- coding: utf-8 -*-\n"
    notes = NOTES[platform].format(start=start, end=end, cash=broker["cash"])
    return f'''{coding}"""
{title} —— {pick(PLATFORMS[platform]["label"], "zh")}
由 SimpleQuant 导出于 {dt.datetime.now():%Y-%m-%d %H:%M}（Exported by SimpleQuant）

{strategies.describe(spec, lang)}

{notes}
"""
'''


# 平台的 import * 放在最前面，避免覆盖信号核心里的函数名
PLATFORM_IMPORTS = {"joinquant": "from jqdata import *  # noqa: F401,F403\n",
                    "myquant": "from gm.api import *  # noqa: F401,F403\n", "qmt": ""}


def _settings(spec: dict, items: list[dict], platform: str, start: str, end: str, broker: dict) -> str:
    secs = [(it["name"], platform_code(it["symbol"], it.get("asset", ""), platform)) for it in items]
    keep = ("cash", "commission", "min_commission", "stamp_duty", "slippage", "t_plus_1")
    imports = PLATFORM_IMPORTS[platform]
    return f'''
import json
import math

import numpy as np
{imports}
# 聚宽等平台的全局里 sum / any / all / min / max 等可能是 numpy 的同名函数（np.any(生成器) 恒为真），这里恢复成 Python 内置函数
from builtins import abs, all, any, max, min, round, sum  # noqa: E402,A004

# ============================== 设置 ==============================
# (SimpleQuant 中的名称, 平台代码)
SECURITIES = {pprint.pformat(secs, width=110)}
# 指标从这天起算（与 SimpleQuant 回测的数据起点相同，均线等指标的数值才会一致）；回测也请从这天开始
HISTORY_START = "{start}"
BACKTEST_END = "{end}"
BROKER = {pprint.pformat({k: broker[k] for k in keep}, width=110, sort_dicts=False)}
SPEC = {pprint.pformat(spec, width=110, sort_dicts=False)}
FIELDS = ["open", "high", "low", "close", "volume"]
CODE = dict(SECURITIES)

'''


CORE_TITLE = "# ============================== SimpleQuant 信号核心（与平台无关） ==============================\n"

JOINQUANT = r'''

# ============================== 聚宽对接 ==============================
def initialize(context):
    set_benchmark(SECURITIES[0][1])
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    log.set_level("order", "error")
    for kind in ("stock", "fund"):
        set_order_cost(OrderCost(open_tax=0, close_tax=BROKER["stamp_duty"],
                                 open_commission=BROKER["commission"], close_commission=BROKER["commission"],
                                 close_today_commission=0, min_commission=BROKER["min_commission"]), type=kind)
    # 聚宽的滑点是买卖价差：买入价上浮一半、卖出价下调一半
    set_slippage(PriceRelatedSlippage(2 * BROKER["slippage"]))
    g.sq_state = {}
    run_daily(sq_trade, time="open")


def _history(code, end_date):
    df = get_price(code, start_date=HISTORY_START, end_date=end_date, frequency="daily", fields=FIELDS,
                   skip_paused=True, fq="pre")
    df = df.dropna(subset=["close"])
    return {k: df[k].values for k in FIELDS}


def _book(context):
    pos = {}
    positions = context.portfolio.positions
    for name, code in SECURITIES:
        if code in positions and positions[code].total_amount > 0:
            pos[name] = {"shares": positions[code].total_amount, "cost": positions[code].avg_cost}
    return {"cash": context.portfolio.available_cash, "pos": pos}


def sq_trade(context):
    hist = {name: _history(code, context.previous_date) for name, code in SECURITIES}
    orders = sq_step(SPEC, hist, _book(context), g.sq_state, log.info)
    cur = get_current_data() if orders else None
    for name, delta, reason in sorted(orders, key=lambda o: o[1] > 0):     # 先卖后买
        code = CODE[name]
        log.info("%s %s %d: %s" % ("买入 buy" if delta > 0 else "卖出 sell", name, abs(delta), reason))
        held = context.portfolio.positions[code].total_amount if code in context.portfolio.positions else 0
        # 科创板市价单必须带保护价（否则聚宽拒单）；用当天涨跌停价作保护价，对其他品种没有影响
        style = MarketOrderStyle(cur[code].low_limit if delta < 0 else cur[code].high_limit)
        if delta < 0 and held + delta <= 0:
            order_target(code, 0, style)
        else:
            order(code, delta, style)
'''

MYQUANT = r'''

# ============================== 掘金对接 ==============================
def init(context):
    context.sq_state = {}
    # 每天收盘的日线到达时计算信号；backtest_match_mode=0 → 在下一根日线开盘价成交
    subscribe(symbols=[code for _, code in SECURITIES], frequency="1d", count=1, wait_group=True)


def _history(code, today):
    df = history(symbol=code, frequency="1d", start_time=HISTORY_START + " 00:00:00", end_time=today + " 23:59:59",
                 fields="open,high,low,close,volume", adjust=ADJUST_PREV, adjust_end_time=today,
                 skip_suspended=True, df=True)
    if df is None or len(df) == 0:
        return {k: [] for k in FIELDS}
    df = df.dropna(subset=["close"])
    return {k: df[k].values.astype(float) for k in FIELDS}


def _book(context):
    account = context.account()
    pos = {}
    for name, code in SECURITIES:
        p = account.position(symbol=code, side=PositionSide_Long)
        if p and p["volume"] > 0:
            pos[name] = {"shares": p["volume"], "cost": p["vwap"]}
    return {"cash": account.cash["available"], "pos": pos}


def on_bar(context, bars):
    today = context.now.strftime("%Y-%m-%d")
    hist = {name: _history(code, today) for name, code in SECURITIES}
    orders = sq_step(SPEC, hist, _book(context), context.sq_state, print)
    for name, delta, reason in sorted(orders, key=lambda o: o[1] > 0):     # 先卖后买
        print("%s %s %s %d: %s" % (today, "买入 buy" if delta > 0 else "卖出 sell", name, abs(delta), reason))
        order_volume(symbol=CODE[name], volume=abs(delta), order_type=OrderType_Market,
                     side=OrderSide_Buy if delta > 0 else OrderSide_Sell,
                     position_effect=PositionEffect_Open if delta > 0 else PositionEffect_Close)


if __name__ == "__main__":
    run(strategy_id="请填写策略ID / your strategy id",
        filename="main.py",
        mode=MODE_BACKTEST,
        token="请填写你的 token / your token",
        backtest_start_time=HISTORY_START + " 08:00:00",
        backtest_end_time=BACKTEST_END + " 16:00:00",
        backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=BROKER["cash"],
        backtest_commission_ratio=BROKER["commission"],
        backtest_slippage_ratio=BROKER["slippage"],
        backtest_match_mode=0)
'''

QMT = r'''

# ============================== QMT 对接 ==============================
class _Global(object):
    """QMT 回测时 ContextInfo 上新设的属性在每根 K 线后会被回滚，跨 K 线的状态放在模块级对象里"""
    pass


G = _Global()


def init(ContextInfo):
    ContextInfo.set_universe([code for _, code in SECURITIES])
    # 按比例收费：[买入印花税, 卖出印花税, 买入佣金, 卖出佣金, 平今佣金, 最低佣金]
    ContextInfo.set_commission(0, [0, BROKER["stamp_duty"], BROKER["commission"], BROKER["commission"],
                                   BROKER["commission"], BROKER["min_commission"]])
    G.state, G.last_day = {}, None
    G.book = {"cash": float(BROKER["cash"]), "pos": {}}


def _fill(name, delta, price):
    """按 SimpleQuant 的成本规则记账（佣金按成交额、最低 min_commission；卖出另收印花税）"""
    value = abs(delta) * price
    comm = max(value * BROKER["commission"], BROKER["min_commission"]) if value > 0 else 0.0
    pos = G.book["pos"].setdefault(name, {"shares": 0, "cost": 0.0})
    if delta > 0:
        pos["cost"] = (pos["shares"] * pos["cost"] + value) / (pos["shares"] + delta)
        G.book["cash"] -= value + comm
    else:
        G.book["cash"] += value - comm - value * BROKER["stamp_duty"]
    pos["shares"] += delta
    if pos["shares"] <= 0:
        del G.book["pos"][name]


def handlebar(ContextInfo):
    if not ContextInfo.do_back_test:
        if ContextInfo.is_last_bar():
            print("SimpleQuant 导出的策略只在回测模式下下单 / backtest mode only")
        return
    day = timetag_to_datetime(ContextInfo.get_bar_timetag(ContextInfo.barpos), "%Y%m%d")
    if day < HISTORY_START.replace("-", "") or day == G.last_day:
        return
    G.last_day = day
    data = ContextInfo.get_market_data_ex(FIELDS, [code for _, code in SECURITIES], period="1d",
                                          start_time=HISTORY_START.replace("-", ""), end_time=day,
                                          dividend_type="front", fill_data=False)
    hist, today = {}, {}
    for name, code in SECURITIES:
        df = data.get(code)
        if df is None:
            hist[name] = {k: [] for k in FIELDS}
            continue
        df = df.dropna(subset=["close"])
        dates = [str(x)[:8] for x in df.index]
        past = [i for i, d in enumerate(dates) if d < day]
        hist[name] = {k: df[k].values[past].astype(float) for k in FIELDS}
        if day in dates:
            today[name] = df.iloc[dates.index(day)]
    orders = sq_step(SPEC, hist, G.book, G.state, print)
    for name, delta, reason in sorted(orders, key=lambda o: o[1] > 0):     # 先卖后买
        bar = today.get(name)
        if bar is None:                                                     # 停牌
            continue
        slip = BROKER["slippage"]
        price = min(bar["open"] * (1 + slip), bar["high"]) if delta > 0 else max(bar["open"] * (1 - slip), bar["low"])
        print("%s %s %s %d @ %.3f: %s" % (day, "买入 buy" if delta > 0 else "卖出 sell", name, abs(delta), price, reason))
        order_shares(CODE[name], delta, "fix", price, ContextInfo)
        _fill(name, delta, price)
'''

ADAPTERS = {"joinquant": JOINQUANT, "myquant": MYQUANT, "qmt": QMT}


def platform_script(platform: str, spec: dict, items: list[dict], broker: dict, start: str, end: str,
                    title: str = "SimpleQuant strategy", lang: str = "zh") -> str:
    """
    :param items: [{"name": SimpleQuant 中的名称, "symbol": 6 位代码, "asset": "etf"|"stock"|"index"|...}]
    :param broker: BrokerConfig 字段（cash, commission, min_commission, stamp_duty, slippage）
    """
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform / 未知平台 {platform}")
    check_exportable(spec, lang)
    spec = {k: v for k, v in spec.items() if k in ("kind", "rule", "template", "params")}
    if spec["kind"] == "template":
        spec["params"] = strategies.resolve(spec)[1]
        spec["params"].pop("t_plus_1", None)
    return (_header(platform, title, spec, start, end, broker, lang)
            + _settings(spec, items, platform, start, end, broker)
            + CORE_TITLE + _core_source()
            + '\nT_PLUS_1 = BROKER.get("t_plus_1", True)\n'
            + f'T0_NAMES = {list(t0_names(it["name"] for it in items))!r}\n'
            + ADAPTERS[platform])


def encode_script(code: str, platform: str) -> bytes:
    return code.encode(PLATFORMS[platform]["encoding"], errors="replace")
