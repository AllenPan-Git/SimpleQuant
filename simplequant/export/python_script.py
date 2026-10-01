"""
把策略导出成可以独立运行的 Python 脚本（只需要 backtrader、pandas 和数据源库）

- 模板策略：直接嵌入 SimpleQuant 实际运行的策略类源码，逻辑完全相同
- 规则策略（条件积木 / AI 生成）：生成可读的指标与条件代码
- 代码策略（策略页代码编辑区）：原样放入用户代码
- 两者共用嵌入的策略基类（整手、T+1、预留资金）和 A 股成本模型
测试保证：导出脚本的每一笔成交都与 SimpleQuant 回测相同（tests/test_export.py）。

多因子选股涉及成分股、财务数据、中性化，目前导出为调用 SimpleQuant 引擎的脚本（见 selection_script）。
"""

import datetime as dt
import inspect
import json
import pprint
from pathlib import Path

from .. import strategies
from ..engine import base_strategy as base_module
from ..engine.commission import AShareCommission
from ..i18n import pick
from ..rules import INDICATORS, default_line
from ..strategies import templates as tpl_module
from ..strategies.factors import KDJ, OBV
from ..strategies.registry import TEMPLATES


def _module_body(module) -> str:
    """模块源码去掉开头的文档字符串和 import"""
    src = inspect.getsource(module)
    lines = src.splitlines()
    i = 0
    if lines and lines[0].startswith('"""'):
        i = 1
        while i < len(lines) and '"""' not in lines[i]:
            i += 1
        i += 1
    body = [ln for ln in lines[i:] if not ln.startswith(("import ", "from "))]
    return "\n".join(body).strip() + "\n"


# ---------------- 规则 → 代码 ----------------
PRICE_LINES = ("close", "open", "high", "low", "volume")


def _line_expr(spec: dict) -> tuple[str, str]:
    """(变量名, Backtrader 表达式)；表达式里用 d 表示当前标的"""
    ind, p = spec["ind"], spec.get("params") or {}
    line = spec.get("line", default_line(ind))
    n = lambda k: int(p[k])  # noqa: E731
    if ind in PRICE_LINES:
        return ind, f"d.{ind}"
    if ind in ("turnover", "pe", "pb", "ps"):
        return ind, f"d.{ind}"
    if ind == "obv":
        return "obv", "OBV(d).obv"
    # 只有一个"周期"参数的指标：(变量名前缀, 表达式模板)
    single = {
        "sma": ("sma", "bt.ind.SMA(d.close, period={N})"),
        "ema": ("ema", "bt.ind.EMA(d.close, period={N})"),
        "rsi": ("rsi", "bt.ind.RSI(d.close, period={N}, safediv=True)"),
        "atr": ("atr", "bt.ind.ATR(d, period={N})"),
        "roc": ("roc", "bt.ind.PercentChange(d.close, period={N}) * 100"),
        "highest": ("high", "bt.ind.Highest(d.high, period={N})(-1)"),
        "lowest": ("low", "bt.ind.Lowest(d.low, period={N})(-1)"),
        "vol_ma": ("vol_ma", "bt.ind.SMA(d.volume, period={N})"),
        "bias": ("bias", "(d.close / bt.ind.SMA(d.close, period={N}) - 1) * 100"),
        "cci": ("cci", "bt.ind.CCI(d, period={N})"),
        "wr": ("wr", "bt.ind.WilliamsR(d, period={N})"),
        "vol_ratio": ("vol_ratio", "bt.DivByZero(d.volume, bt.ind.SMA(d.volume, period={N})(-1), zero=float('nan'))"),
        "volatility": ("volatility", "bt.ind.StdDev(bt.ind.PctChange(d.close, period=1), period={N}) * 100"),
    }
    if ind in single:
        prefix, expr = single[ind]
        return f"{prefix}_{n('period')}", expr.format(N=n("period"))
    if ind == "ma_slope":
        return (f"ma_slope_{n('period')}_{n('n')}",
                f"(lambda ma: (ma / ma(-{n('n')}) - 1) * 100)(bt.ind.SMA(d.close, period={n('period')}))")
    if ind == "macd":
        attr = {"dif": "macd", "dea": "signal"}.get(line)
        base = f"bt.ind.MACD(d.close, period_me1={n('fast')}, period_me2={n('slow')}, period_signal={n('signal')})"
        name = f"macd_{n('fast')}_{n('slow')}_{n('signal')}_{line}"
        return name, (f"{base}.{attr}" if attr else f"(lambda m: m.macd - m.signal)({base})")
    if ind == "boll":
        attr = {"upper": "top", "mid": "mid", "lower": "bot"}[line]
        return (f"boll_{n('period')}_{str(float(p['dev'])).replace('.', '_')}_{line}",
                f"bt.ind.BollingerBands(d.close, period={n('period')}, devfactor={float(p['dev'])}).{attr}")
    if ind == "adx":
        attr = {"adx": "adx", "pdi": "plusDI", "mdi": "minusDI"}[line]
        return f"dmi_{n('period')}_{line}", f"bt.ind.DirectionalMovement(d, period={n('period')}).{attr}"
    if ind == "kdj":
        return (f"kdj_{n('period')}_{n('m1')}_{n('m2')}_{line}",
                f"KDJ(d, period={n('period')}, m1={n('m1')}, m2={n('m2')}).{line}")
    raise ValueError(f"Unknown indicator / 未知指标 {ind}")


def rule_strategy_code(rule: dict, lang: str = "zh") -> str:
    """把规则生成可读的 Backtrader 策略类（继承嵌入的 _PerAsset）"""
    lines_init, names = [], {}
    cross_n = 0
    conds = {"buy": [], "sell": []}
    ops = {">": ">", "<": "<", ">=": ">=", "<=": "<="}

    def operand(spec):
        if "value" in spec:
            return repr(float(spec["value"])), None
        if spec["ind"] == "pnl_pct":
            return "self.pnl_pct(d)", None
        name, expr = _line_expr(spec)
        if name not in names:
            names[name] = expr
            lines_init.append(f'            i["{name}"] = {expr}')
        return f'i["{name}"][0]', f'i["{name}"]'

    for side in ("buy", "sell"):
        for c in (rule.get(side) or {}).get("conditions", []):
            left_v, left_line = operand(c["left"])
            if c["op"] in ("cross_above", "cross_below"):
                right = c["right"]
                right_line = repr(float(right["value"])) if "value" in right else operand(right)[1]
                key = f"cross_{cross_n}"
                cross_n += 1
                lines_init.append(f'            i["{key}"] = bt.ind.CrossOver({left_line}, {right_line})')
                conds[side].append(f'i["{key}"][0] {">" if c["op"] == "cross_above" else "<"} 0')
            else:
                right_v, _ = operand(c["right"])
                conds[side].append(f"{left_v} {ops[c['op']]} {right_v}")

    def joined(side):
        cs = conds[side]
        if not cs:
            return "False"
        glue = " and " if (rule.get(side) or {}).get("logic", "all") == "all" else " or "
        return glue.join(f"({x})" for x in cs)

    init = "\n".join(lines_init) or "            pass"
    return f'''class Strategy(_PerAsset):
    """规则策略（由 SimpleQuant 生成）
{_indent(strategies.describe({"kind": "rule", "rule": rule}, lang), 4)}
    """
    params = (("position_pct", {rule.get("position_pct", 95)!r}),)

    def __init__(self):
        super().__init__()
        self.ind = {{}}
        for d in self.datas:
            i = {{}}
{init}
            self.ind[d] = i

    def pnl_pct(self, d):
        """持仓收益率（%）；空仓时为 NaN（比较结果为假）"""
        pos = self.getposition(d)
        return (d.close[0] / pos.price - 1) * 100 if pos.size > 0 and pos.price > 0 else float("nan")

    def buy_signal(self, d):
        i = self.ind[d]
        return {joined("buy")}

    def sell_signal(self, d):
        i = self.ind[d]
        return {joined("sell")}

    def next(self):
        for d in self.datas:
            holding = self.getposition(d).size > 0
            if not holding and self.buy_signal(d):
                self.order_target_pct(d, self.slot_pct, ("reason.rule_buy", {{}}))
            elif holding and self.sell_signal(d):
                self.order_target_pct(d, 0, ("reason.rule_sell", {{}}))
'''


def _indent(text: str, n: int) -> str:
    return "\n".join(" " * n + ln for ln in text.splitlines())


def _template_code(key: str, params: dict) -> str:
    tpl = TEMPLATES[key]
    src = inspect.getsource(tpl.cls)
    return (src + f"\n\nStrategy = {tpl.cls.__name__}\nSTRATEGY_PARAMS = {params!r}\n")


def _uses(rule: dict, ind: str) -> bool:
    return any(s.get("ind") == ind for side in ("buy", "sell") for c in (rule.get(side) or {}).get("conditions", [])
               for s in (c["left"], c["right"]))


# ---------------- 数据与运行部分（固定文本） ----------------
DATA_CODE = r'''
# ============================== 数据 ==============================
COLUMN_ALIASES = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume",
                  "换手率": "turnover", "turn": "turnover", "peTTM": "pe", "pbMRQ": "pb", "psTTM": "ps"}
EXTRA = ["turnover", "pe", "pb", "ps"]


def _normalize(df):
    df = df.rename(columns={c: COLUMN_ALIASES.get(c, c) for c in df.columns})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    cols = ["open", "high", "low", "close", "volume"] + [c for c in EXTRA if c in df.columns]
    df = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.drop(columns=[c for c in EXTRA if c in df.columns and df[c].isna().all()])
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    return df[df["close"] > 0].ffill().astype(float)


def load_akshare(symbol, start, end, adjust, asset):
    import akshare as ak
    s, e = start.replace("-", ""), end.replace("-", "")
    if asset == "etf":
        raw = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=s, end_date=e, adjust=adjust)
    elif asset == "stock":
        raw = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=s, end_date=e, adjust=adjust)
    else:
        raw = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=s, end_date=e)
    return _normalize(raw)


def load_baostock(symbol, start, end, adjust):
    import baostock as bs
    prefix = "sh" if symbol[:1] in "569" else "bj" if symbol[:1] in "48" else "sz"
    flag = {"hfq": "1", "qfq": "2", "": "3"}[adjust]
    bs.login()
    try:
        rows = []
        for fields in ("date,open,high,low,close,volume,turn,peTTM,pbMRQ,psTTM", "date,open,high,low,close,volume"):
            rs = bs.query_history_k_data_plus(f"{prefix}.{symbol}", fields, start_date=start, end_date=end,
                                              frequency="d", adjustflag=flag)
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                return _normalize(pd.DataFrame(rows, columns=rs.fields))
        raise ValueError(f"no data for {symbol}")
    finally:
        bs.logout()


def load(item):
    if item["source"] == "csv":
        return _normalize(pd.read_csv(item["path"], encoding="utf-8-sig"))
    if item["source"] == "baostock":
        return load_baostock(item["symbol"], item["start"], item["end"], item["adjust"])
    return load_akshare(item["symbol"], item["start"], item["end"], item["adjust"], item.get("asset", "etf"))


class Feed(bt.feeds.PandasData):
    lines = tuple(EXTRA)
    params = tuple((c, -1) for c in EXTRA)
'''

RUN_CODE = r'''
# ============================== 运行 ==============================
class Equity(bt.Analyzer):
    def start(self):
        self.rows = []

    def next(self):
        self.rows.append((self.strategy.datetime.datetime(0), self.strategy.broker.getvalue()))


def run(prices: dict):
    """prices: {名称: DataFrame(open, high, low, close, volume, ...)}；返回 Backtrader 策略实例"""
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(BROKER["cash"])
    cerebro.broker.addcommissioninfo(AShareCommission(commission=BROKER["commission"],
                                                      min_commission=BROKER["min_commission"],
                                                      stamp_duty=BROKER["stamp_duty"]))
    if BROKER["slippage"] > 0:
        cerebro.broker.set_slippage_perc(perc=BROKER["slippage"])
    for name, df in prices.items():
        extras = {c: c for c in EXTRA if c in df.columns}
        cerebro.adddata(Feed(dataname=df, name=name, datetime=None, openinterest=-1, **extras), name=name)
    cerebro.addstrategy(Strategy, t_plus_1=BROKER["t_plus_1"], **STRATEGY_PARAMS)
    cerebro.addanalyzer(Equity, _name="equity")
    return cerebro.run()[0]


def report(strat):
    eq = pd.Series(dict(strat.analyzers.equity.rows))
    daily = eq.resample("D").last().dropna()
    rets = daily.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / BROKER["cash"] - 1
    print(f"区间 / Period        {eq.index[0]:%Y-%m-%d} ~ {eq.index[-1]:%Y-%m-%d}")
    print(f"最终资产 / Final     {eq.iloc[-1]:,.2f}")
    print(f"总收益 / Return      {total:.2%}")
    print(f"年化 / CAGR          {(1 + total) ** (1 / years) - 1:.2%}")
    print(f"最大回撤 / Max DD    {(eq / eq.cummax() - 1).min():.2%}")
    if len(rets) > 1 and rets.std() > 0:
        print(f"夏普 / Sharpe        {(rets - 0.02 / 252).mean() / rets.std() * 252 ** 0.5:.2f}")
    print(f"完整交易 / Trades    {len(strat.trade_records)}")
    orders = pd.DataFrame(strat.order_records, columns=ORDER_COLUMNS)
    if len(orders):
        orders.to_csv("orders.csv", index=False, encoding="utf-8-sig")
        print("成交记录已保存 / fills saved: orders.csv")


if __name__ == "__main__":
    report(run({item["name"]: load(item) for item in DATA}))
'''


def _header(title: str, spec: dict, lang: str) -> str:
    desc = strategies.describe(spec, lang)
    return f'''r"""
{title}
由 SimpleQuant 导出于 {dt.datetime.now():%Y-%m-%d %H:%M}（Exported by SimpleQuant）

{desc}

运行 / Run:
    pip install backtrader pandas akshare baostock
    python {{filename}}

规则与 SimpleQuant 回测一致：信号在 K 线收盘产生、下一根开盘成交；100 股整手；T+1；佣金最低 5 元、卖出印花税；滑点。
"""

import math

import backtrader as bt
import numpy as np
import pandas as pd
'''


def single_asset_script(spec: dict, data: list[dict], broker: dict, title: str = "SimpleQuant strategy",
                        lang: str = "zh", filename: str = "strategy.py") -> str:
    """
    :param spec: 模板或规则策略描述
    :param data: [{"name", "source": "akshare"|"baostock"|"csv", "symbol", "asset", "adjust", "start", "end", "path"}]
    :param broker: BrokerConfig 字段
    """
    if spec.get("kind") == "selection":
        raise ValueError("use selection_script for selection strategies / 选股策略请用 selection_script")
    parts = [_header(title, spec, lang).replace("{filename}", filename)]
    parts.append("# ============================== 设置 ==============================\n"
                 f"DATA = {pprint.pformat(data, width=110, sort_dicts=False)}\n\n"
                 f"BROKER = {pprint.pformat(broker, width=110, sort_dicts=False)}\n")
    parts.append(DATA_CODE)
    parts.append("\n# ============================== 交易成本 ==============================\n"
                 + inspect.getsource(AShareCommission) + "\n")
    parts.append("\n# ============================== 策略基类（整手、T+1、预留资金） ==============================\n"
                 + _module_body(base_module))
    parts.append("\n\n" + inspect.getsource(tpl_module._PerAsset))
    if spec.get("kind") == "code":          # 自己写的代码：原样放入，可用名字与 SimpleQuant 里相同
        code = spec["code"]
        parts.append("\nPerAsset = _PerAsset\n")
        if "KDJ" in code:
            parts.append("\n\n" + inspect.getsource(KDJ))
        if "OBV" in code:
            parts.append("\n\n" + inspect.getsource(OBV))
        params = {k: v for k, v in (spec.get("params") or {}).items() if k != "t_plus_1"}
        parts.append("\n\n# ============================== 策略 ==============================\n"
                     + code.rstrip() + f"\n\n\nSTRATEGY_PARAMS = {params!r}\n")
    elif spec.get("kind") == "rule":
        rule = spec["rule"]
        if _uses(rule, "kdj"):
            parts.append("\n\n" + inspect.getsource(KDJ))
        if _uses(rule, "obv"):
            parts.append("\n\n" + inspect.getsource(OBV))
        parts.append("\n\n# ============================== 策略 ==============================\n"
                     + rule_strategy_code(rule, lang)
                     + f"\n\nSTRATEGY_PARAMS = {{}}\n")
    else:
        cls, params = strategies.resolve(spec)
        params = {k: v for k, v in params.items() if k != "t_plus_1"}
        parts.append("\n\n# ============================== 策略 ==============================\n"
                     + _template_code(spec["template"], params))
    parts.append(RUN_CODE)
    return "\n".join(parts)


def selection_script(spec: dict, broker: dict, start: str, project_dir: str, title: str = "SimpleQuant selection",
                     lang: str = "zh", exe: str | None = None) -> str:
    """
    多因子选股：生成调用 SimpleQuant 引擎的脚本
    :param exe: 打包版 SimpleQuant.exe 的路径；给出时说明改为用 exe 运行（exe 自带引擎，不需要装 Python）
    """
    from ..stocks import FACTORS
    needs_fin = any(FACTORS.get(f["key"], {}).get("requires_fin") for f in spec["factors"]) or \
        bool((spec.get("neutralize") or {}).get("size"))
    if exe:
        how = f'''请用 SimpleQuant 运行（不需要安装 Python）：
    - 在 SimpleQuant「多因子选股」页的「导出」菜单点「运行选股脚本」，选择这个文件；或
    - 命令行："{exe}" --run-script 本文件.py'''
    else:
        how = f'''请用 SimpleQuant 的 Python 环境执行：
    "{project_dir}\\.venv\\Scripts\\python.exe" 本文件.py
（打包版 SimpleQuant.exe 也能运行：选股页「导出」菜单 →「运行选股脚本」）'''
    return f'''r"""
{title}
由 SimpleQuant 导出于 {dt.datetime.now():%Y-%m-%d %H:%M}

{strategies.describe(spec, lang)}

说明：多因子选股需要成分股、财务数据与中性化计算，这个脚本调用 SimpleQuant 的引擎运行。
{how}
会先增量更新数据，再回测并打印结果，在本文件所在目录保存成交记录 orders.csv 和最新选股 picks.csv。
"""

import os
import sys
if not getattr(sys, "frozen", False):          # 源码版：从项目目录导入 SimpleQuant；打包版已自带
    sys.path.insert(0, r"{project_dir}")

import pandas as pd
from simplequant.engine import BrokerConfig
from simplequant.paper.calendar import load_calendar, latest_expected_day
from simplequant.stocks import StockStore, UNIVERSES, build_panel, run_selection

SPEC = {pprint.pformat(spec, width=100, sort_dicts=False)}
BROKER = BrokerConfig(**{broker!r})
START = "{start}"
NEEDS_FIN = {needs_fin!r}
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


if __name__ == "__main__":
    store = StockStore()
    end = latest_expected_day(load_calendar()).date().isoformat()
    print("updating data / 更新数据…", flush=True)
    uni = store.update_universe(SPEC["universe"], START, end)
    store.update_index(UNIVERSES[SPEC["universe"]]["index"], START, end)
    codes = sorted(uni["code"].unique())
    store.update(codes, START, end)
    if NEEDS_FIN:
        store.update_fundamentals(codes, int(START[:4]) - 1)
    if SPEC.get("dividend") == "cash":
        store.update_dividends(codes, int(START[:4]) - 1)
    first = store.load_index(UNIVERSES[SPEC["universe"]]["index"]).index[0]
    print("backtesting / 回测…", flush=True)
    panel = build_panel(store, SPEC["universe"], str(first.date()), end)
    res = run_selection(panel, SPEC, BROKER, start=max(START, str((first + pd.Timedelta(days=400)).date())))
    for k in ("total_return", "cagr", "max_drawdown", "sharpe", "benchmark_return", "turnover_annual"):
        print(f"{{k:18s}} {{res.metrics[k]:.4f}}")
    res.orders.to_csv(os.path.join(OUT_DIR, "orders.csv"), index=False, encoding="utf-8-sig")
    last = max(res.schedule.picks)
    pd.DataFrame({{"code": res.schedule.picks[last],
                  "name": [res.names.get(c, c) for c in res.schedule.picks[last]]}}).to_csv(
        os.path.join(OUT_DIR, "picks.csv"), index=False, encoding="utf-8-sig")
    print(f"latest picks ({{last.date()}}) saved to / 最新选股已保存到 {{os.path.join(OUT_DIR, 'picks.csv')}}")
'''
