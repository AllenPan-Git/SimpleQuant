"""
规则策略的 JSON 结构、校验与描述（中英双语）

规则示例：
{
  "buy":  {"logic": "all", "conditions": [
      {"left": {"ind": "sma", "params": {"period": 5}}, "op": "cross_above",
       "right": {"ind": "sma", "params": {"period": 20}}},
      {"left": {"ind": "rsi", "params": {"period": 14}}, "op": "<", "right": {"value": 70}}
  ]},
  "sell": {"logic": "any", "conditions": [
      {"left": {"ind": "sma", "params": {"period": 5}}, "op": "cross_below",
       "right": {"ind": "sma", "params": {"period": 20}}},
      {"left": {"ind": "pnl_pct"}, "op": "<", "right": {"value": -8}}
  ]},
  "position_pct": 95
}
"""

from ..i18n import L, pick, tr

PERIOD = ("period", L("周期", "Period"), 20)
P14 = ("period", L("周期", "Period"), 14)
YEARS = ("years", L("回看年数", "Lookback (years)"), 5)
BARS_PER_YEAR = 244      # 估值百分位：一年按 244 个交易日算；有效数据不到一年时为空
VALUATION_SOURCES = L("BaoStock 股票日线逐日提供；AKShare 下载常见宽基 ETF / 指数时附带指数估值（每月更新）",
                      "daily in BaoStock stock data; AKShare adds index valuation (updated monthly) to common broad-index ETFs / indices")
PCT_DESC = L("当前估值在过去 N 年里的位置：0 = 最低，100 = 最高。不足 N 年时用已有数据（至少 1 年）",
             "where the current valuation sits within the past N years: 0 = lowest, 100 = highest. Uses the data available if shorter (at least 1 year)")
ERP_DESC = L("100 / 市盈率 − 10 年期国债收益率，单位 %；越高表示股票相对债券越便宜。市盈率为负时为空。"
             "需要数据里有市盈率（同「市盈率」），并先在「数据」页下载利率数据",
             "100 / P/E − 10Y government yield, in %; higher means stocks are cheaper relative to bonds. Empty when P/E is negative. "
             "Needs P/E in the data (as for P/E) and rates downloaded on the Data page")

GROUPS = {
    "price": L("价格", "Price"),
    "trend": L("趋势", "Trend"),
    "oscillator": L("摆动", "Oscillator"),
    "volume": L("量能", "Volume"),
    "volatility": L("波动", "Volatility"),
    "valuation": L("估值/换手", "Valuation / turnover"),
    "macro": L("利率/利差", "Rates / spreads"),
    "position": L("持仓", "Position"),
}

# 指标（因子）白名单：
#   label 名称 / group 分组 / params (名, 显示名, 默认值) / lines 多条输出线
#   desc 简要说明（也用于自然语言提示词）/ requires 需要数据里有的列 / position 依赖持仓
INDICATORS = {
    # ---- 价格 ----
    "close": {"label": L("收盘价", "Close"), "group": "price", "params": []},
    "open": {"label": L("开盘价", "Open"), "group": "price", "params": []},
    "high": {"label": L("最高价", "High"), "group": "price", "params": []},
    "low": {"label": L("最低价", "Low"), "group": "price", "params": []},
    "highest": {"label": L("前N根K线最高价", "Prior N-bar high"), "group": "price", "params": [PERIOD],
                "desc": L("不含当前K线，用于突破", "excludes the current bar; for breakouts")},
    "lowest": {"label": L("前N根K线最低价", "Prior N-bar low"), "group": "price", "params": [PERIOD],
               "desc": L("不含当前K线", "excludes the current bar")},
    # ---- 趋势 ----
    "sma": {"label": L("均线 MA", "SMA"), "group": "trend", "params": [PERIOD]},
    "ema": {"label": L("指数均线 EMA", "EMA"), "group": "trend", "params": [PERIOD]},
    "macd": {"label": L("MACD", "MACD"), "group": "trend",
             "params": [("fast", L("快线", "Fast"), 12), ("slow", L("慢线", "Slow"), 26),
                        ("signal", L("信号线", "Signal"), 9)],
             "lines": {"dif": L("DIF", "DIF"), "dea": L("DEA", "DEA"), "hist": L("MACD柱", "Histogram")},
             "desc": L("金叉 = DIF 上穿 DEA", "golden cross = DIF crosses above DEA")},
    "ma_slope": {"label": L("均线斜率(%)", "MA slope (%)"), "group": "trend",
                 "params": [PERIOD, ("n", L("比较K线数", "Lookback bars"), 5)],
                 "desc": L("均线相对 n 根K线前的变化百分比，>0 表示均线向上",
                           "% change of the MA vs n bars ago; >0 means the MA is rising")},
    "bias": {"label": L("乖离率 BIAS(%)", "BIAS (%)"), "group": "trend", "params": [PERIOD],
             "desc": L("收盘价偏离均线的百分比", "% distance of close from its MA")},
    "adx": {"label": L("DMI/ADX 趋向指标", "DMI / ADX"), "group": "trend", "params": [P14],
            "lines": {"adx": L("ADX", "ADX"), "pdi": L("+DI", "+DI"), "mdi": L("-DI", "-DI")},
            "desc": L("ADX>25 通常表示趋势明显；+DI 上穿 -DI 偏多",
                      "ADX>25 usually means a strong trend; +DI crossing above -DI is bullish")},
    "roc": {"label": L("N根K线涨幅(%)", "N-bar change (%)"), "group": "trend", "params": [PERIOD],
            "desc": L("动量因子", "momentum")},
    # ---- 摆动 ----
    "rsi": {"label": L("RSI", "RSI"), "group": "oscillator", "params": [P14],
            "desc": L("0~100，<30 超卖，>70 超买", "0-100; <30 oversold, >70 overbought")},
    "kdj": {"label": L("KDJ", "KDJ"), "group": "oscillator",
            "params": [("period", L("周期", "Period"), 9), ("m1", L("K平滑", "K smoothing"), 3),
                       ("m2", L("D平滑", "D smoothing"), 3)],
            "lines": {"k": L("K", "K"), "d": L("D", "D"), "j": L("J", "J")},
            "desc": L("0~100，K 上穿 D 为金叉，<20 超卖", "0-100; K crossing above D is a golden cross, <20 oversold")},
    "cci": {"label": L("CCI 顺势指标", "CCI"), "group": "oscillator", "params": [PERIOD],
            "desc": L(">100 强势，<-100 弱势", ">100 strong, <-100 weak")},
    "wr": {"label": L("威廉指标 WR", "Williams %R"), "group": "oscillator", "params": [P14],
           "desc": L("-100~0，<-80 超卖，>-20 超买", "-100 to 0; <-80 oversold, >-20 overbought")},
    # ---- 量能 ----
    "volume": {"label": L("成交量", "Volume"), "group": "volume", "params": []},
    "vol_ma": {"label": L("均量", "Volume MA"), "group": "volume", "params": [PERIOD]},
    "vol_ratio": {"label": L("量比", "Volume ratio"), "group": "volume", "params": [("period", L("周期", "Period"), 5)],
                  "desc": L("当前成交量 ÷ 前N根K线平均量，>2 为明显放量",
                            "volume ÷ average of the prior N bars; >2 is a clear volume surge")},
    "obv": {"label": L("OBV 能量潮", "OBV"), "group": "volume", "params": [],
            "desc": L("累计量能，常与其均线比较", "cumulative volume flow")},
    # ---- 波动 ----
    "boll": {"label": L("布林带", "Bollinger"), "group": "volatility",
             "params": [PERIOD, ("dev", L("标准差倍数", "Std dev"), 2.0)],
             "lines": {"upper": L("上轨", "Upper"), "mid": L("中轨", "Middle"), "lower": L("下轨", "Lower")}},
    "atr": {"label": L("ATR 真实波幅", "ATR"), "group": "volatility", "params": [P14]},
    "volatility": {"label": L("收益波动率(%)", "Return volatility (%)"), "group": "volatility", "params": [PERIOD],
                   "desc": L("近N根K线收益率的标准差（%，未年化）", "std dev of the last N bar returns (%, not annualized)")},
    # ---- 估值 / 换手（需要数据里有对应列） ----
    "turnover": {"label": L("换手率(%)", "Turnover (%)"), "group": "valuation", "params": [], "requires": "turnover",
                 "desc": L("BaoStock 股票日线提供；AKShare 日线在东财接口可用时也有",
                           "from BaoStock stock daily data; AKShare daily data has it when its Eastmoney endpoint works")},
    "pe": {"label": L("市盈率 PE(TTM)", "P/E (TTM)"), "group": "valuation", "params": [], "requires": "pe",
           "desc": VALUATION_SOURCES},
    "pb": {"label": L("市净率 PB", "P/B"), "group": "valuation", "params": [], "requires": "pb",
           "desc": VALUATION_SOURCES},
    "pe_pct": {"label": L("市盈率历史百分位(%)", "P/E percentile (%)"), "group": "valuation", "params": [YEARS],
               "requires": "pe", "desc": PCT_DESC},
    "pb_pct": {"label": L("市净率历史百分位(%)", "P/B percentile (%)"), "group": "valuation", "params": [YEARS],
               "requires": "pb", "desc": PCT_DESC},
    "ps": {"label": L("市销率 PS(TTM)", "P/S (TTM)"), "group": "valuation", "params": [], "requires": "ps",
           "desc": L("仅 BaoStock 股票日线提供", "BaoStock stock daily data only")},
    # ---- 利率 / 信用利差（中债收益率曲线，所有标的共用；见 simplequant/bonds/rates.py） ----
    "cgb10y": {"label": L("10年期国债收益率(%)", "10Y government bond yield (%)"), "group": "macro", "params": [],
               "requires": "cgb10y", "macro": True,
               "desc": L("中债国债收益率曲线；需先在「数据」页下载利率数据", "ChinaBond curve; download rates on the Data page first")},
    "term_spread": {"label": L("期限利差 10年−1年(BP)", "Term spread 10Y−1Y (bp)"), "group": "macro", "params": [],
                    "requires": "term_spread", "macro": True,
                    "desc": L("10 年减 1 年期国债收益率，单位 BP；收窄常被视为经济预期转弱",
                              "10Y minus 1Y government yield in bp; flattening often signals weaker growth expectations")},
    "credit_spread": {"label": L("信用利差 3年AAA中票−国债(BP)", "Credit spread 3Y AAA MTN − govt (bp)"), "group": "macro",
                      "params": [], "requires": "credit_spread", "macro": True,
                      "desc": L("3 年期中短期票据（AAA）减 3 年期国债收益率，单位 BP；走阔通常表示风险偏好下降",
                                "3Y AAA medium-term notes minus 3Y government yield in bp; widening usually means risk aversion")},
    # 股债利差：本标的的盈利收益率减 10 年期国债收益率；既要估值列，又要利率数据
    "erp": {"label": L("股债利差 1/PE−10年国债(%)", "Equity risk premium 1/PE − 10Y yield (%)"), "group": "macro",
            "params": [], "requires": ("pe", "cgb10y"), "desc": ERP_DESC},
    "erp_pct": {"label": L("股债利差历史百分位(%)", "Equity risk premium percentile (%)"), "group": "macro",
                "params": [YEARS], "requires": ("pe", "cgb10y"),
                "desc": L("当前股债利差在过去 N 年里的位置：100 = 股票相对债券最便宜。不足 N 年时用已有数据（至少 1 年）",
                          "where the current premium sits within the past N years: 100 = stocks cheapest relative to bonds. "
                          "Uses the data available if shorter (at least 1 year)")},
    # ---- 持仓 ----
    "pnl_pct": {"label": L("持仓收益率(%)", "Position P&L (%)"), "group": "position", "params": [], "position": True,
                "desc": L("用于止损/止盈，例如 < -8", "for stop loss / take profit, e.g. < -8")},
}


def macro_columns(rule: dict) -> set[str]:
    """规则用到的利率列（不在行情数据里，回测前从利率数据并入）"""
    return {c for c in required_columns(rule) if INDICATORS.get(c, {}).get("macro")}


def required_columns(rule: dict) -> set[str]:
    """规则用到的因子需要数据里有哪些列（如 pe / turnover）"""
    cols = set()
    for side in ("buy", "sell"):
        for c in (rule.get(side) or {}).get("conditions", []):
            for s in (c.get("left") or {}, c.get("right") or {}):
                cols.update(requires_of(s.get("ind")))
    return cols


def requires_of(ind: str | None) -> tuple[str, ...]:
    """指标需要的列；requires 可以是一个列名，也可以是几个（股债利差要市盈率和国债收益率）"""
    req = INDICATORS.get(ind, {}).get("requires")
    return (req,) if isinstance(req, str) else tuple(req or ())

OPS = {
    "cross_above": L("上穿", "crosses above"),
    "cross_below": L("下穿", "crosses below"),
    ">": L("大于", ">"),
    "<": L("小于", "<"),
    ">=": L("大于等于", "≥"),
    "<=": L("小于等于", "≤"),
}
CROSS_OPS = ("cross_above", "cross_below")
MAX_DAYS = 60            # 条件「连续 N 根 K 线满足」的上限
MAX_COOLDOWN = 250       # 卖出后多少根 K 线内不再买入的上限
MAX_ADDS = 5             # 分批建仓：最多加仓几次
ADD_WHEN = {             # 什么时候加仓
    "gain": L("比上次买入时上涨", "price rises from the last buy by"),
    "drop": L("比上次买入时下跌", "price falls from the last buy by"),
    "signal": L("买入条件再次满足", "buy conditions are met again"),
}
LOGICS = {"all": L("全部满足", "ALL of"), "any": L("任一满足", "ANY of")}


def default_params(ind: str) -> dict:
    return {name: default for name, _, default in INDICATORS[ind]["params"]}


def default_line(ind: str) -> str | None:
    lines = INDICATORS[ind].get("lines")
    return next(iter(lines)) if lines else None


def make_ind(ind: str, **params) -> dict:
    spec = {"ind": ind, "params": {**default_params(ind), **params}}
    if default_line(ind):
        spec["line"] = default_line(ind)
    return spec


def validate(rule: dict, lang: str = "zh") -> list[str]:
    errors = []
    for side in ("buy", "sell"):
        side_label = tr(f"rule.{side}", lang)
        block = rule.get(side) or {}
        if block.get("logic", "all") not in LOGICS:
            errors.append(tr("err.logic", lang, side=side_label))
        conds = block.get("conditions") or []
        if side == "buy" and not conds:
            errors.append(tr("err.no_buy", lang))
        for i, c in enumerate(conds, 1):
            prefix = tr("err.cond", lang, side=side_label, i=i)
            left, right, op = c.get("left") or {}, c.get("right") or {}, c.get("op")
            if op not in OPS:
                errors.append(prefix + tr("err.op", lang, op=op))
            for s in (left, right):
                if "value" in s:
                    if not isinstance(s["value"], (int, float)):
                        errors.append(prefix + tr("err.value_num", lang))
                    continue
                ind = s.get("ind")
                if ind not in INDICATORS:
                    errors.append(prefix + tr("err.unknown_ind", lang, ind=ind))
                    continue
                label = pick(INDICATORS[ind]["label"], lang)
                lines = INDICATORS[ind].get("lines")
                if lines and s.get("line", default_line(ind)) not in lines:
                    errors.append(prefix + tr("err.no_line", lang, ind=label, line=s.get("line")))
                for name, _, _ in INDICATORS[ind]["params"]:
                    v = (s.get("params") or {}).get(name)
                    if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                        errors.append(prefix + tr("err.param_pos", lang, ind=label, name=name))
            if "value" in left:
                errors.append(prefix + tr("err.left_ind", lang))
            if op in CROSS_OPS and any(INDICATORS.get(s.get("ind"), {}).get("position") for s in (left, right)):
                errors.append(prefix + tr("err.pnl_cross", lang))
            days = c.get("days", 1)
            if not _int_in(days, 1, MAX_DAYS):
                errors.append(prefix + tr("err.days", lang, max=MAX_DAYS))
            elif days > 1 and op in CROSS_OPS:
                errors.append(prefix + tr("err.days_cross", lang))
            elif days > 1 and any(INDICATORS.get(s.get("ind"), {}).get("position") for s in (left, right)):
                errors.append(prefix + tr("err.days_pnl", lang))
    pct = rule.get("position_pct", 95)
    if not isinstance(pct, (int, float)) or not 0 < pct <= 100:
        errors.append(tr("err.pct", lang))
    if not _int_in(rule.get("cooldown", 0), 0, MAX_COOLDOWN):
        errors.append(tr("err.cooldown", lang, max=MAX_COOLDOWN))
    scale = rule.get("scale")
    if scale:
        if not _int_in(scale.get("adds"), 1, MAX_ADDS):
            errors.append(tr("err.adds", lang, max=MAX_ADDS))
        if scale.get("when") not in ADD_WHEN:
            errors.append(tr("err.add_when", lang))
        elif scale["when"] != "signal" and not (isinstance(scale.get("pct"), (int, float)) and 0 < scale["pct"] < 100):
            errors.append(tr("err.add_pct", lang))
    return errors


def _int_in(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def describe_ind(spec: dict, lang: str = "zh") -> str:
    if "value" in spec:
        return f"{spec['value']:g}"
    meta = INDICATORS.get(spec.get("ind"), {"label": spec.get("ind"), "params": []})
    params = spec.get("params") or {}
    args = ",".join(f"{params.get(n, d):g}" for n, _, d in meta["params"])
    text = pick(meta["label"], lang) + (f"({args})" if args else "")
    if meta.get("lines"):
        text += "·" + pick(meta["lines"].get(spec.get("line", default_line(spec["ind"])), ""), lang)
    return text


def describe_condition(c: dict, lang: str = "zh") -> str:
    text = f"{describe_ind(c['left'], lang)} {pick(OPS.get(c['op'], c['op']), lang)} {describe_ind(c['right'], lang)}"
    days = c.get("days", 1)
    return text + tr("rule.days", lang, n=days) if isinstance(days, int) and days > 1 else text


def describe_scale(rule: dict, lang: str = "zh") -> str:
    """分批建仓的说明；没有设置时为空"""
    s = rule.get("scale")
    if not s:
        return ""
    when = pick(ADD_WHEN.get(s.get("when"), L(str(s.get("when")), str(s.get("when")))), lang) + (f" {s.get('pct', 0):g}%" if s.get("when") != "signal" else "")
    return tr("rule.scale", lang, k=s.get("adds", 1), parts=s.get("adds", 1) + 1, when=when)


def describe_rule(rule: dict, lang: str = "zh") -> str:
    parts = []
    for side in ("buy", "sell"):
        label = tr(f"rule.{side}", lang)
        block = rule.get(side) or {}
        conds = block.get("conditions") or []
        if not conds:
            parts.append(f"【{label}】" + tr("rule.none_sell" if side == "sell" else "rule.none", lang))
            continue
        joiner = tr("rule.and" if block.get("logic", "all") == "all" else "rule.or", lang)
        parts.append(f"【{label}】" + joiner.join(describe_condition(c, lang) for c in conds))
    parts.append(f"【{tr('rule.position', lang)}】{rule.get('position_pct', 95):g}%")
    if scale := describe_scale(rule, lang):
        parts.append(f"【{tr('rule.scale_title', lang)}】{scale}")
    if rule.get("cooldown"):
        parts.append(f"【{tr('rule.cooldown_title', lang)}】" + tr("rule.cooldown", lang, n=rule["cooldown"]))
    return "\n".join(parts)
