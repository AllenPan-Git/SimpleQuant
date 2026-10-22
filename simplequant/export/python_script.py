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
from ..engine import limits as limits_module
from ..engine.market import t0_names
from ..i18n import pick
from ..rules import INDICATORS, default_line
from ..rules.schema import BARS_PER_YEAR
from ..strategies import templates as tpl_module
from ..strategies.factors import KDJ, OBV, Percentile
from ..engine.results import xirr
from ..strategies.registry import TEMPLATES
from ..stocks.dividends import tax_rate
from ..system import venv_python


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
    # 顶部 import 由脚本统一写；函数里的相对导入（避开循环导入用）在独立脚本里无法执行，所需函数另行附上
    body = [ln for ln in lines[i:] if not ln.startswith(("import ", "from ")) and not ln.lstrip().startswith("from .")]
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
    if ind in ("pe_pct", "pb_pct"):
        col, years = INDICATORS[ind]["requires"], n("years")
        return f"{ind}_{years}", f"Percentile(d.{col}, window={years * BARS_PER_YEAR}, min_count={BARS_PER_YEAR}).pct"
    if INDICATORS.get(ind, {}).get("macro"):
        raise ValueError("rates indicators are not supported in the exported script yet / 利率类条件暂不支持导出为独立脚本")
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

    def operand(spec, at="0"):
        """(取值表达式, 指标线)；at = 取第几根之前的值（"0" 为当前，"-k" 配合「连续 N 根 K 线」）"""
        if "value" in spec:
            return repr(float(spec["value"])), None
        if spec["ind"] == "pnl_pct":
            return "self.pnl_pct(d)", None
        name, expr = _line_expr(spec)
        if name not in names:
            names[name] = expr
            lines_init.append(f'            i["{name}"] = {expr}')
        return f'i["{name}"][{at}]', f'i["{name}"]'

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
            elif c.get("days", 1) > 1:
                expr = f"{operand(c['left'], '-k')[0]} {ops[c['op']]} {operand(c['right'], '-k')[0]}"
                conds[side].append(f"self.streak(d, {int(c['days'])}, lambda k: {expr})")
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
    scale = rule.get("scale") or {}
    return f'''class Strategy(_PerAsset):
    """规则策略（由 SimpleQuant 生成）
{_indent(strategies.describe({"kind": "rule", "rule": rule}, lang), 4)}
    """
    params = (("position_pct", {rule.get("position_pct", 95)!r}),
              ("cooldown", {int(rule.get("cooldown", 0))!r}),      # 卖出后多少根 K 线内不再买入（0 = 不限制）
              ("adds", {int(scale.get("adds", 0))!r}),          # 分批建仓：最多加仓几次（0 = 一次买满）
              ("add_when", {scale.get("when", "")!r}),     # 加仓时机：gain 上涨 / drop 下跌 / signal 买入条件再次满足
              ("add_pct", {float(scale.get("pct", 0) or 0)!r}))     # 比上次买入时上涨 / 下跌多少（%）

    def __init__(self):
        super().__init__()
        self.ind = {{}}
        self._sold_at, self._adds, self._ref = {{}}, {{}}, {{}}
        for d in self.datas:
            i = {{}}
{init}
            self.ind[d] = i

    def pnl_pct(self, d):
        """持仓收益率（%）；空仓时为 NaN（比较结果为假）"""
        pos = self.getposition(d)
        return (d.close[0] / pos.price - 1) * 100 if pos.size > 0 and pos.price > 0 else float("nan")

    def streak(self, d, n, check):
        """连续 n 根 K 线（含当前）都满足；check(k) 判断 k 根之前那根"""
        return all(len(d) > k and check(k) for k in range(n))

    def buy_signal(self, d):
        i = self.ind[d]
        return {joined("buy")}

    def sell_signal(self, d):
        i = self.ind[d]
        return {joined("sell")}

    def add_due(self, d):
        if self.p.add_when == "signal":
            return self.buy_signal(d)
        pct, ref = self.p.add_pct / 100, self._ref[d]
        return d.close[0] >= ref * (1 + pct) if self.p.add_when == "gain" else d.close[0] <= ref * (1 - pct)

    def next(self):
        parts = self.p.adds + 1          # 仓位分几份：首次买 1 份，每次加仓再买 1 份
        for d in self.datas:
            holding = self.getposition(d).size > 0
            if not holding:
                if self.p.cooldown and len(d) - self._sold_at.get(d, -10 ** 9) <= self.p.cooldown:
                    continue
                if self.buy_signal(d) and \\
                        self.order_target_pct(d, self.slot_pct / parts, ("reason.rule_buy", {{}})) is not None:
                    self._adds[d], self._ref[d] = 0, d.close[0]
            elif self.sell_signal(d):
                if self.order_target_pct(d, 0, ("reason.rule_sell", {{}})) is not None:
                    self._sold_at[d] = len(d)
            elif self.p.adds and d in self._ref and self._adds[d] < self.p.adds and self.add_due(d):
                k = self._adds[d] + 1
                if self.order_add_pct(d, self.slot_pct / parts, ("reason.rule_add", {{"k": k}})) is not None:
                    self._adds[d], self._ref[d] = k, d.close[0]
'''


def _indent(text: str, n: int) -> str:
    return "\n".join(" " * n + ln for ln in text.splitlines())


def _template_code(key: str, params: dict) -> str:
    tpl = TEMPLATES[key]
    src = inspect.getsource(tpl.cls)
    if issubclass(tpl.cls, tpl_module.Dca) and tpl.cls is not tpl_module.Dca:      # 估值定投：带上父类与百分位指标
        src = "\n\n".join([f"BARS_PER_YEAR = {BARS_PER_YEAR}\n", inspect.getsource(Percentile),
                           inspect.getsource(tpl_module.Dca), src])
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
RAW = ["raw_close"]          # 不复权收盘价：按真实股价凑整手（与 SimpleQuant 相同）


def _normalize(df):
    df = df.rename(columns={c: COLUMN_ALIASES.get(c, c) for c in df.columns})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    cols = ["open", "high", "low", "close", "volume"] + [c for c in EXTRA + RAW if c in df.columns]
    df = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.drop(columns=[c for c in EXTRA if c in df.columns and df[c].isna().all()])
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    return df[df["close"] > 0].ffill().astype(float)


def load_akshare(symbol, start, end, adjust, asset):
    """
    股票 / ETF 与 SimpleQuant 相同，用等比复权：东方财富不复权日线 × 复权因子
    （东方财富自带的复权价是加法复权，每天的涨跌幅会被压缩）。股票因子来自 BaoStock，ETF 用新浪的累计分红
    """
    import akshare as ak
    s, e = start.replace("-", ""), end.replace("-", "")
    if asset == "index":
        return _normalize(ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=s, end_date=e))
    get = ak.fund_etf_hist_em if asset == "etf" else ak.stock_zh_a_hist
    if not adjust:
        return _normalize(get(symbol=symbol, period="daily", start_date=s, end_date=e, adjust=""))
    df = _normalize(get(symbol=symbol, period="daily", start_date="19900101" if asset == "etf" else s,
                        end_date=e, adjust=""))
    f = _etf_factor(symbol, df) if asset == "etf" else _stock_factor(symbol, df.index)
    if adjust == "qfq":
        f = f / f.iloc[-1]
    df["raw_close"] = df["close"]
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * f
    return df.loc[start:end]


def _steps(steps, dates):
    steps = steps.dropna().sort_index()
    steps = steps[~steps.index.duplicated(keep="last")]
    return steps.reindex(dates.union(steps.index)).ffill().reindex(dates).fillna(1.0)


def _stock_factor(symbol, dates):
    import baostock as bs
    prefix = "sh" if symbol[:1] in "569" else "bj" if symbol[:1] in "48" else "sz"
    bs.login()
    try:
        rs = bs.query_adjust_factor(code=f"{prefix}.{symbol}", start_date="1990-01-01",
                                    end_date=dates[-1].strftime("%Y-%m-%d"))
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise ConnectionError(rs.error_msg)
    finally:
        bs.logout()
    adj = pd.DataFrame(rows, columns=rs.fields)
    if adj.empty:
        return pd.Series(1.0, index=dates)
    return _steps(pd.Series(pd.to_numeric(adj["backAdjustFactor"]).values,
                            index=pd.to_datetime(adj["dividOperateDate"])), dates)


def _etf_factor(symbol, df):
    """新浪复权表：每个日期起的累计每份分红 u；累计分红不变的一行是份额拆分 / 折算（比例按当天收盘跳空推算）"""
    import json
    import requests
    sym = ("sh" if symbol[:1] in "569" else "sz") + symbol
    r = requests.get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js", timeout=20,
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    if r.status_code == 404:
        return pd.Series(1.0, index=df.index)
    r.raise_for_status()
    t = pd.DataFrame(json.loads(r.text[r.text.index("{"): r.text.rindex("}") + 1]).get("data") or [])
    if t.empty or "u" not in t:
        return pd.Series(1.0, index=df.index)
    t["d"] = pd.to_datetime(t["d"], errors="coerce")
    t = t[t["d"] > pd.Timestamp("1901-01-01")].set_index("d").sort_index()
    u = pd.to_numeric(t["u"], errors="coerce").ffill().fillna(0.0)
    u = u[~u.index.duplicated(keep="last")]
    cash = u.diff().fillna(u)
    close, prev = df["close"], df["close"].shift(1)
    jump = pd.Series(1.0, index=close.index)
    for d, c in cash.items():
        i = close.index.searchsorted(d)
        if i == 0 or i >= len(close):
            continue
        if abs(c) < 1e-9:                          # 拆分 / 折算
            if close.index[i] == d and abs(prev.iloc[i] / close.iloc[i] - 1) >= 0.05:
                jump.iloc[i] *= prev.iloc[i] / close.iloc[i]
        elif c > 0:
            jump.iloc[i] *= prev.iloc[i] / (prev.iloc[i] - c)
    return jump.cumprod()


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
                df = _normalize(pd.DataFrame(rows, columns=rs.fields))
                if adjust:                            # 附上不复权收盘价（按真实股价凑整手）
                    rs = bs.query_history_k_data_plus(f"{prefix}.{symbol}", "date,close", start_date=start,
                                                      end_date=end, frequency="d", adjustflag="3")
                    raw = []
                    while rs.error_code == "0" and rs.next():
                        raw.append(rs.get_row_data())
                    s = pd.DataFrame(raw, columns=rs.fields)
                    s = pd.Series(pd.to_numeric(s["close"], errors="coerce").values, index=pd.to_datetime(s["date"]))
                    df["raw_close"] = s[~s.index.duplicated(keep="last")].reindex(df.index).values
                return df
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
    lines = tuple(EXTRA + RAW)
    params = tuple((c, -1) for c in EXTRA + RAW)
'''

RUN_CODE = r'''
# ============================== 运行 ==============================
class Equity(bt.Analyzer):
    def start(self):
        self.rows = []

    def next(self):
        s = self.strategy
        self.rows.append((s.datetime.datetime(0), s.broker.getvalue(), s.contributed))


def contribution_flows(prices: dict) -> dict:
    """定投追加资金：所有标的交易日里每月 / 每周的第一个交易日（第一期由初始资金投入）。{日期: 金额}"""
    amount = BROKER.get("contribution") or 0
    if amount <= 0:
        return {}
    days = pd.DatetimeIndex(sorted(set().union(*(df.index.normalize() for df in prices.values()))))
    period = days.to_period("W" if BROKER.get("contribution_freq") == "weekly" else "M")
    first = pd.Series(days, index=days).groupby(period).first()
    return {pd.Timestamp(d).date(): float(amount) for d in first.values[1:]}


def run(prices: dict):
    """prices: {名称: DataFrame(open, high, low, close, volume, ...)}；返回 Backtrader 策略实例"""
    flows = contribution_flows(prices)
    # 定投到账（以及估值定投的储备计息）在开盘前处理；broker_coo=False：委托的撮合方式与平时一样
    coo = bool(flows) or getattr(Strategy, "needs_open", False)
    cerebro = bt.Cerebro(stdstats=False, **({"cheat_on_open": True, "broker_coo": False} if coo else {}))
    cerebro.broker.setcash(BROKER["cash"])
    cerebro.broker.addcommissioninfo(AShareCommission(commission=BROKER["commission"],
                                                      min_commission=BROKER["min_commission"],
                                                      stamp_duty=BROKER["stamp_duty"]))
    if BROKER["slippage"] > 0:
        cerebro.broker.set_slippage_perc(perc=BROKER["slippage"])
    if BROKER.get("price_limit", True):
        filler = LimitFiller()
        filler.broker = cerebro.broker
        cerebro.broker.set_filler(filler)
    for name, df in prices.items():
        extras = {c: c for c in EXTRA + RAW if c in df.columns}
        cerebro.adddata(Feed(dataname=df, name=name, datetime=None, openinterest=-1, **extras), name=name)
    t0 = tuple(n for n in T0_NAMES if n in prices) if BROKER["t_plus_1"] else ()
    cerebro.addstrategy(Strategy, t_plus_1=BROKER["t_plus_1"], **({"t0_names": t0} if t0 else {}),
                        **({"contributions": flows} if flows else {}), **STRATEGY_PARAMS)
    cerebro.addanalyzer(Equity, _name="equity")
    return cerebro.run()[0]


def report(strat):
    rows = pd.DataFrame(strat.analyzers.equity.rows, columns=["time", "value", "contributed"]).set_index("time")
    eq, added = rows["value"], rows["contributed"].diff().fillna(0.0)
    # 单位净值：扣除当天到账的追加资金（没有定投时就是资产曲线），年化、回撤、夏普都按它算
    nav = (1 + ((eq - added) / eq.shift(1) - 1).fillna(0.0)).cumprod() * eq.iloc[0]
    daily = nav.resample("D").last().dropna()
    rets = daily.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    invested = BROKER["cash"] + rows["contributed"].iloc[-1]
    total = eq.iloc[-1] / invested - 1
    print(f"区间 / Period        {eq.index[0]:%Y-%m-%d} ~ {eq.index[-1]:%Y-%m-%d}")
    print(f"最终资产 / Final     {eq.iloc[-1]:,.2f}")
    if invested > BROKER["cash"]:
        paid = added[added > 0]
        print(f"累计投入 / Invested  {invested:,.2f}（追加 {len(paid)} 次 / {len(paid)} contributions）")
        print(f"总收益 / Return      {total:.2%}（相对累计投入 / on total invested）")
        r = xirr([eq.index[0], *paid.index, eq.index[-1]], [-BROKER["cash"], *(-paid.values), eq.iloc[-1]])
        print(f"XIRR                 {r:.2%}")
    else:
        print(f"总收益 / Return      {total:.2%}")
    print(f"年化 / CAGR          {(nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1:.2%}")
    print(f"最大回撤 / Max DD    {(nav / nav.cummax() - 1).min():.2%}")
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

规则与 SimpleQuant 回测一致：信号在 K 线收盘产生、下一根开盘成交；100 股整手；T+1（债券 ETF、可转债等 T+0 品种除外）；佣金最低 5 元、卖出印花税；滑点。
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
                 f"BROKER = {pprint.pformat(broker, width=110, sort_dicts=False)}\n"
                 "# 实行 T+0 的标的（债券 ETF、可转债等），不受 T+1 限制\n"
                 f"T0_NAMES = {list(t0_names(d['name'] for d in data))!r}\n")
    parts.append(DATA_CODE)
    parts.append("\n# ============================== 交易成本 ==============================\n"
                 + inspect.getsource(AShareCommission) + "\n")
    parts.append("\n# ============================== 涨跌停与停牌 ==============================\n"
                 + _module_body(limits_module))
    parts.append("\n# ============================== 策略基类（整手、T+1、预留资金） ==============================\n"
                 + _module_body(base_module))
    parts.append("\n\n" + inspect.getsource(tax_rate))          # BaseStrategy._dividend_tax 用到
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
        if _uses(rule, "pe_pct") or _uses(rule, "pb_pct"):
            if any(d.get("source") == "akshare" for d in data):
                # 导出的脚本下载 ETF / 指数时不带指数估值（乐咕乐股），百分位会一直为空
                raise ValueError("index valuation (Legulegu) is not included in the exported script yet / "
                                 "ETF / 指数附带的指数估值暂不支持导出为独立脚本")
            parts.append("\n\n" + inspect.getsource(Percentile))
        parts.append("\n\n# ============================== 策略 ==============================\n"
                     + rule_strategy_code(rule, lang)
                     + f"\n\nSTRATEGY_PARAMS = {{}}\n")
    else:
        if spec.get("template") == "value_dca" and any(d.get("source") == "akshare" for d in data):
            raise ValueError("index valuation (Legulegu) is not included in the exported script yet / "
                             "ETF / 指数附带的指数估值暂不支持导出为独立脚本")
        cls, params = strategies.resolve(spec)
        params = {k: v for k, v in params.items() if k != "t_plus_1"}
        parts.append("\n\n# ============================== 策略 ==============================\n"
                     + _template_code(spec["template"], params))
    parts.append("\n\n" + inspect.getsource(xirr))             # 定投的资金加权年化
    parts.append(RUN_CODE)
    return "\n".join(parts)


def selection_script(spec: dict, broker: dict, start: str, project_dir: str, title: str = "SimpleQuant selection",
                     lang: str = "zh", exe: str | None = None) -> str:
    """
    多因子选股：生成调用 SimpleQuant 引擎的脚本
    :param exe: 打包版 SimpleQuant 可执行文件的路径；给出时说明改为用它运行（自带引擎，不需要装 Python）
    """
    from ..stocks import FACTORS
    from ..stocks.selection import panel_warmup_days
    needs_fin = any(FACTORS.get(f["key"], {}).get("requires_fin") for f in spec["factors"]) or \
        bool((spec.get("neutralize") or {}).get("size"))
    needs_div = spec.get("dividend") == "cash" or \
        any(FACTORS.get(f["key"], {}).get("requires_div") for f in spec["factors"])
    if exe:
        how = f'''请用 SimpleQuant 运行（不需要安装 Python）：
    - 在 SimpleQuant「多因子选股」页的「导出」菜单点「运行选股脚本」，选择这个文件；或
    - 命令行："{exe}" --run-script 本文件.py'''
    else:
        how = f'''请用 SimpleQuant 的 Python 环境执行：
    "{venv_python(project_dir)}" 本文件.py
（打包版 SimpleQuant 也能运行：选股页「导出」菜单 →「运行选股脚本」）'''
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
from simplequant.stocks import StockStore, run_selection, universe

SPEC = {pprint.pformat(spec, width=100, sort_dicts=False)}
BROKER = BrokerConfig(**{broker!r})
START = "{start}"
NEEDS_FIN = {needs_fin!r}
NEEDS_DIV = {needs_div!r}
WARMUP_DAYS = {panel_warmup_days(spec)!r}       # 机器学习的训练窗口、超过 252 天的 IC 回看期要多取，其余为 400
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


if __name__ == "__main__":
    store = StockStore()
    end = latest_expected_day(load_calendar()).date().isoformat()
    print("updating data / 更新数据…", flush=True)
    universe.update(SPEC["universe"], START, end, store, fin=NEEDS_FIN, div=NEEDS_DIV)
    first = universe.load_benchmark(SPEC["universe"], store).index[0]
    first = max(first, pd.Timestamp(START) - pd.Timedelta(days=WARMUP_DAYS))    # 面板往前多取一段预热因子
    print("backtesting / 回测…", flush=True)
    panel = universe.build(SPEC["universe"], str(first.date()), end, store)
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
