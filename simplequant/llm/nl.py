"""
自然语言 -> 规则策略

流程：用户描述 → 大模型按 LLM_SCHEMA 输出 JSON → 转成规则 → 用 rules.validate 校验
      → 若有错误，把错误发回模型修正一次 → 返回 NLResult 给界面确认
大模型只生成规则数据，不生成任何代码。

LLM_SCHEMA 专门为大模型设计：所有字段必填、没有可变键（参数用 [{name, value}] 列表），
这样能满足 Claude / OpenAI 严格 JSON Schema 模式的要求；再由 to_rule() 转成内部规则格式。
"""

import json
from dataclasses import dataclass, field

from ..i18n import pick
from ..rules import INDICATORS, OPS, LOGICS, validate, default_params, default_line, describe_rule
from .providers import Provider, extract_json

IND_KEYS = list(INDICATORS)
LINE_KEYS = sorted({ln for m in INDICATORS.values() for ln in (m.get("lines") or {})})

_OPERAND = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["indicator", "value"]},
        "ind": {"type": "string", "enum": IND_KEYS},
        "line": {"type": "string", "enum": LINE_KEYS + [""]},
        "params": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "number"}},
            "required": ["name", "value"], "additionalProperties": False}},
        "value": {"type": "number"},
    },
    "required": ["kind", "ind", "line", "params", "value"],
    "additionalProperties": False,
}
_CONDITION = {
    "type": "object",
    "properties": {"left": _OPERAND, "op": {"type": "string", "enum": list(OPS)}, "right": _OPERAND},
    "required": ["left", "op", "right"],
    "additionalProperties": False,
}
_SIDE = {
    "type": "object",
    "properties": {"logic": {"type": "string", "enum": list(LOGICS)},
                   "conditions": {"type": "array", "items": _CONDITION}},
    "required": ["logic", "conditions"],
    "additionalProperties": False,
}
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "understood": {"type": "string"},
        "symbols": {"type": "array", "items": {"type": "string"}},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "buy": _SIDE,
        "sell": _SIDE,
        "position_pct": {"type": "number"},
    },
    "required": ["name", "understood", "symbols", "unsupported", "buy", "sell", "position_pct"],
    "additionalProperties": False,
}

LANG_NAMES = {"zh": "Simplified Chinese", "en": "English"}


def _catalog() -> str:
    rows = []
    for key, m in INDICATORS.items():
        params = ", ".join(f"{n}={d}" for n, _, d in m["params"]) or "none"
        lines = f"; lines: {', '.join(m['lines'])}" if m.get("lines") else ""
        desc = f"; {pick(m['desc'], 'en')}" if m.get("desc") else ""
        rows.append(f"- {key}: {pick(m['label'], 'en')} / {pick(m['label'], 'zh')}; params: {params}{lines}{desc}")
    return "\n".join(rows)


def system_prompt(lang: str = "zh") -> str:
    return f"""You translate a trader's plain-language strategy description into a rule for the SimpleQuant backtester (A-share stocks and ETFs). Output data only; never code.

How a rule works:
- "buy" conditions are checked only while flat; "sell" conditions only while holding. "logic" is "all" (AND) or "any" (OR).
- Signals are evaluated at each bar's close and filled at the next bar's open. A-share lots of 100 shares and T+1 are handled by the engine.
- position_pct is the % of capital used per buy (default 95 if the user does not say; "half position/半仓" = 50).
- An empty sell list means hold until the end.

Each condition is: left operand, op, right operand.
- op: cross_above / cross_below are crossing events (e.g. golden cross); >, <, >=, <= compare current values.
- operand kind "indicator": set ind (from the catalog), line (only for multi-line indicators, else ""), params as [{{"name","value"}}] using only that indicator's parameter names (omit a parameter to use its default); set value to 0.
- operand kind "value": a plain number in value; set ind to "close", line "", params [].
- The left operand must be an indicator.

Indicator catalog (key: English / Chinese name; params with defaults):
{_catalog()}

Translation conventions:
- Stop loss of X% → sell condition pnl_pct < -X. Take profit of X% → sell condition pnl_pct > X. Both usually go in a sell block with logic "any".
- MACD golden cross → macd line dif cross_above macd line dea. KDJ golden cross → kdj k cross_above kdj d.
- "N-day high breakout / 突破N日新高" → close > highest(period=N). "above the N-day moving average / 站上N日线" → close > sma(period=N) (use cross_above if they say "crosses").
- "volume surge / 放量" without a number → vol_ratio > 2. Days means bars on daily data.
- Valuation (pe, pb, ps) and turnover only work when the data has those columns; still use them if the user asks.

Be honest about limits:
- Anything the catalog and rule format cannot express (e.g. ROE or other financial-statement factors, ranking/selecting among many stocks, holding-period or calendar exits, pyramiding or dynamic sizing, short selling, market-wide filters) goes into "unsupported" as short phrases, and is left out of the rule. Never silently approximate; if you do approximate something, say exactly how in "understood".
- If the description is not a trading strategy at all, return empty condition lists and explain in "unsupported".

Other fields:
- understood: one short paragraph restating the rule you built, written in {LANG_NAMES.get(lang, 'English')}.
- name: a short strategy name in {LANG_NAMES.get(lang, 'English')}.
- symbols: 6-digit codes the user names or clearly refers to (e.g. 沪深300ETF → 510300, 中证500ETF → 510500, 创业板ETF → 159915, 贵州茅台 → 600519). Empty if none or unsure.
- unsupported: phrases in {LANG_NAMES.get(lang, 'English')}."""


def _operand(o: dict) -> dict:
    if o.get("kind") == "value":
        return {"value": float(o.get("value", 0))}
    ind = o.get("ind") if o.get("ind") in INDICATORS else "close"
    defaults = default_params(ind)
    given = {p["name"]: p["value"] for p in o.get("params") or [] if p.get("name") in defaults}
    params = {n: (int(round(given[n])) if isinstance(d, int) else float(given[n])) if n in given else d
              for n, d in defaults.items()}
    spec = {"ind": ind, "params": params}
    lines = INDICATORS[ind].get("lines")
    if lines:
        spec["line"] = o.get("line") if o.get("line") in lines else default_line(ind)
    return spec


def to_rule(out: dict) -> dict:
    """大模型输出 → 内部规则格式"""
    rule = {}
    for side in ("buy", "sell"):
        block = out.get(side) or {}
        rule[side] = {
            "logic": block.get("logic") if block.get("logic") in LOGICS else "all",
            "conditions": [{"left": _operand(c.get("left") or {}), "op": c.get("op"), "right": _operand(c.get("right") or {})}
                           for c in block.get("conditions") or []],
        }
    pct = out.get("position_pct", 95)
    rule["position_pct"] = float(pct) if isinstance(pct, (int, float)) and 0 < pct <= 100 else 95.0
    return rule


@dataclass
class NLResult:
    spec: dict                 # {"kind": "rule", "rule": ...}
    name: str
    understood: str
    unsupported: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # 仍未通过校验的问题（为空表示可以直接回测）
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _generate(text: str, provider: Provider, system: str, schema: dict, convert, check, repair_rounds: int,
              empty=None) -> dict:
    """
    调用模型 → 转换 → 校验；校验不通过就把错误发回模型修正（最多 repair_rounds 次）。返回模型的原始输出
    empty(out) 为真表示模型认为这段话表达不了（如不是交易策略）：不再要求修正，否则模型会为了通过校验编造条件
    """
    messages = [{"role": "user", "content": text.strip()}]
    out = {}
    for attempt in range(repair_rounds + 1):
        out = extract_json(provider.chat(system, messages, schema))
        errors = check(convert(out), "en")
        if not errors or (empty is not None and empty(out)):
            break
        if attempt < repair_rounds:
            messages += [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                         {"role": "user", "content": "That result failed validation:\n- " + "\n- ".join(errors)
                                                     + "\nReturn the corrected JSON. Never invent conditions or factors "
                                                       "the user did not ask for just to pass validation; if the "
                                                       "request cannot be expressed, return empty lists and explain "
                                                       "in \"unsupported\"."}]
    return out


def _empty_rule(out: dict) -> bool:
    return not ((out.get("buy") or {}).get("conditions") or (out.get("sell") or {}).get("conditions"))


def translate(text: str, provider: Provider, lang: str = "zh", repair_rounds: int = 1) -> NLResult:
    """自然语言 → 择时规则"""
    out = _generate(text, provider, system_prompt(lang), LLM_SCHEMA, to_rule, validate, repair_rounds, _empty_rule)
    rule = to_rule(out)
    return NLResult(spec={"kind": "rule", "rule": rule}, name=out.get("name") or "", understood=out.get("understood") or "",
                    unsupported=list(out.get("unsupported") or []),
                    symbols=[s for s in out.get("symbols") or [] if isinstance(s, str) and s.isdigit() and len(s) == 6],
                    errors=validate(rule, lang), raw=out)


# ---------------- 自然语言 → 多因子选股 ----------------
def selection_schema() -> dict:
    from ..stocks import FACTORS, UNIVERSES, WEIGHTING
    factor = {"type": "object",
              "properties": {"key": {"type": "string", "enum": list(FACTORS)},
                             "direction": {"type": "string", "enum": ["higher", "lower"]},
                             "weight": {"type": "number"}},
              "required": ["key", "direction", "weight"], "additionalProperties": False}
    props = {
        "name": {"type": "string"}, "understood": {"type": "string"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "universe": {"type": "string", "enum": list(UNIVERSES)},
        "factors": {"type": "array", "items": factor},
        "weighting": {"type": "string", "enum": list(WEIGHTING)},
        "top_n": {"type": "number"},
        "rebalance": {"type": "string", "enum": ["monthly", "weekly", "days"]},
        "rebalance_days": {"type": "number"},
        "exclude_st": {"type": "boolean"},
        "min_list_days": {"type": "number"},
        "neutralize_industry": {"type": "boolean"},
        "neutralize_size": {"type": "boolean"},
        "position_pct": {"type": "number"},
        "max_price": {"type": "number"},
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def selection_system_prompt(lang: str = "zh") -> str:
    from ..stocks import FACTORS, UNIVERSES, GROUPS, factor_assets
    for_text = {("stock",): "stocks only", ("cb",): "convertible bonds only", ("stock", "cb"): "stocks and convertible bonds"}
    cat = "\n".join(
        f"- {k}: {pick(m['label'], 'en')} / {pick(m['label'], 'zh')}; group {pick(GROUPS[m['group']], 'en')}; "
        f"usual direction {'higher' if m['direction'] > 0 else 'lower'} is better"
        + (f"; {pick(m['desc'], 'en')}" if m.get("desc") else "")
        + ("; needs financial data" if m.get("requires_fin") else "")
        + ("; needs dividend data" if m.get("requires_div") else "")
        + f"; for {for_text.get(tuple(factor_assets(k)), 'stocks only')}"
        for k, m in FACTORS.items())
    unis = ", ".join(f"{k} = {pick(u['label'], 'en')} / {pick(u['label'], 'zh')}" for k, u in UNIVERSES.items())
    language = LANG_NAMES.get(lang, "English")
    return f"""You translate a plain-language stock-picking idea into a multi-factor selection strategy for the SimpleQuant backtester (Chinese A-shares). Output data only; never code.

How it works: on each rebalance day the eligible members of the index universe are scored on the chosen factors (each factor is winsorized and standardized, flipped by its direction, multiplied by its weight and summed); the top_n stocks are bought in equal weight at the next open and held until the next rebalance.

Universes: {unis}. Default hs300.
Factor catalog (key: English / Chinese name):
{cat}

Conventions:
- "cheap / low valuation / 低估值" → ep higher (and/or bp higher). "high ROE / quality / 高质量" → roe higher. "profitable growth / 成长" → np_yoy and/or rev_yoy higher.
- "small caps / 小盘" → size lower; "large caps / 大盘" → size higher. "low volatility / 低波动" → vol60 lower. "low turnover / 冷门" → turn20 lower.
- "short-term reversal / 超跌反弹" → ret20 lower (or ret5 lower). "momentum / 强者恒强" → mom_120_20 higher.
- direction is "higher" or "lower" (higher/lower factor values are better). weight defaults to 1; use larger weights only when the user stresses a factor.
- weighting: "manual" unless the user asks to let the data decide weights ("按 IC 加权" → "ic", "按 ICIR" → "icir"). With ic/icir the directions are learned from past IC.
- rebalance: "monthly" by default; "weekly"; or "days" with rebalance_days = N for "every N trading days" (otherwise set rebalance_days to 20).
- top_n default 10; "hold 20 stocks / 持有20只" → 20. exclude_st default true. min_list_days default 250 ("exclude new listings under a year").
- "industry neutral / 行业中性 / 各行业分散" → neutralize_industry true. "size neutral / 市值中性" → neutralize_size true. Default false.
- position_pct default 95.
- Convertible bonds ("可转债 / 转债 / convertible bonds") → universe cb, and only factors marked for convertible bonds. "双低 / double-low" → cb_double_low lower. "低价转债" → cb_price lower. "低溢价 / 低转股溢价率" → cb_premium lower. "接近债底 / 纯债溢价率低" → cb_bond_premium lower. "小规模转债" → cb_issue_size lower. For cb set exclude_st false and min_list_days 0 unless asked. max_price: the price cap for cb ("价格低于 130 元" → 130); 0 keeps the default (130). For stock universes set max_price to 0.

Be honest about limits: anything not expressible with this catalog and these settings (e.g. factors not listed such as dividend yield, analyst ratings, specific industries to include/exclude, stop-losses, market-timing overlays, universes other than those listed) goes into "unsupported" as short phrases written in {language} and is left out. Do not use a different factor as a stand-in for an unsupported one (e.g. never use ep in place of dividend yield); the user decides whether to add something else. If none of what the user asked for can be expressed (or the text is not a stock-picking idea at all), return an empty factors list and explain in "unsupported".
Editing an existing strategy: if the message starts with "Current strategy", the text after "Change request" asks for changes to that strategy (e.g. "add low volatility", "hold 30 instead"). Start from the current strategy and change only what is asked; copy every other field unchanged, including factor weights and directions. Removing all factors is allowed only if the user asks for it.

understood: one short paragraph in {language} restating the strategy you built (when editing, begin with what you changed). name: short name in {language}."""


def to_selection_spec(out: dict) -> dict:
    from ..stocks import FACTORS, UNIVERSES, WEIGHTING
    factors = [{"key": f["key"], "weight": float(f.get("weight", 1) or 1),
                "direction": 1 if f.get("direction") == "higher" else -1}
               for f in out.get("factors") or [] if f.get("key") in FACTORS]
    reb = out.get("rebalance", "monthly")
    rebalance = int(out.get("rebalance_days") or 20) if reb == "days" else (reb if reb in ("monthly", "weekly") else "monthly")
    return {
        "kind": "selection",
        "universe": out.get("universe") if out.get("universe") in UNIVERSES else "hs300",
        "factors": factors,
        "top_n": int(round(out.get("top_n") or 10)),
        "rebalance": rebalance,
        "filters": _filters(out),
        "position_pct": float(out.get("position_pct") or 95),
        "weighting": out.get("weighting") if out.get("weighting") in WEIGHTING else "manual",
        "neutralize": {"industry": bool(out.get("neutralize_industry")), "size": bool(out.get("neutralize_size"))},
    }


def _filters(out: dict) -> dict:
    from ..stocks import universe as U
    if U.kind(out.get("universe") or "") == "cb":
        from ..bonds.panel import cb_filters
        f = cb_filters({"min_list_days": int(out.get("min_list_days") or 0)})
        if out.get("max_price"):
            f["max_price"] = float(out["max_price"])
        return f
    return {"exclude_st": bool(out.get("exclude_st", True)), "min_list_days": int(out.get("min_list_days") or 0)}


def from_selection_spec(spec: dict) -> dict:
    """选股策略描述 → 大模型输出格式（to_selection_spec 的逆）；用于「在当前方案基础上修改」"""
    from ..stocks import FACTORS, universe as U
    reb = spec.get("rebalance", "monthly")
    flt = spec.get("filters") or {}
    neutral = spec.get("neutralize") or {}
    return {
        "universe": spec.get("universe", "hs300"),
        "factors": [{"key": f["key"], "direction": "higher" if f.get("direction", 1) > 0 else "lower",
                     "weight": float(f.get("weight", 1))} for f in spec.get("factors") or [] if f["key"] in FACTORS],
        "weighting": spec.get("weighting", "manual"),
        "top_n": int(spec.get("top_n", 10)),
        "rebalance": reb if isinstance(reb, str) else "days",
        "rebalance_days": 20 if isinstance(reb, str) else int(reb),
        "exclude_st": bool(flt.get("exclude_st", True)),
        "min_list_days": int(flt.get("min_list_days") or 0),
        "neutralize_industry": bool(neutral.get("industry")),
        "neutralize_size": bool(neutral.get("size")),
        "position_pct": float(spec.get("position_pct", 95)),
        "max_price": float(flt.get("max_price") or 0) if U.kind(spec.get("universe") or "") == "cb" else 0,
    }


def _merge_base(base: dict, spec: dict, out: dict) -> dict:
    """修改模式：模型管不到的设置（IC 回看期、分红方式、可转债成交额下限等）沿用原方案"""
    from ..stocks import universe as U
    merged = {**base, **spec}
    if U.kind(base.get("universe") or "") == U.kind(spec["universe"]):
        flt = {**(base.get("filters") or {}), "min_list_days": spec["filters"]["min_list_days"]}
        if U.kind(spec["universe"]) == "cb":
            if out.get("max_price"):
                flt["max_price"] = float(out["max_price"])
        else:
            flt["exclude_st"] = spec["filters"]["exclude_st"]
        merged["filters"] = flt
    return merged


def validate_selection(spec: dict, lang: str = "zh") -> list[str]:
    from ..i18n import tr
    from ..stocks import FACTORS, factor_assets, universe as U
    errors = []
    if not spec.get("factors"):
        errors.append(tr("err.sel_no_factor", lang))
    kind = U.kind(spec.get("universe") or "")
    for f in spec.get("factors") or []:
        if kind not in factor_assets(f["key"]):
            errors.append(tr("err.sel_factor_kind", lang, name=pick(FACTORS[f["key"]]["label"], lang)))
    if not 1 <= spec.get("top_n", 0) <= 100:
        errors.append(tr("err.sel_top_n", lang))
    if not isinstance(spec.get("rebalance"), str) and not 1 <= int(spec["rebalance"]) <= 250:
        errors.append(tr("err.sel_rebalance", lang))
    if not 0 < spec.get("position_pct", 95) <= 100:
        errors.append(tr("err.pct", lang))
    return errors


def translate_selection(text: str, provider: Provider, lang: str = "zh", repair_rounds: int = 1,
                        base: dict | None = None) -> NLResult:
    """自然语言 → 多因子选股策略；给了 base 则把这段话当作对 base 的修改"""
    msg = text.strip()
    if base:
        msg = ("Current strategy:\n" + json.dumps(from_selection_spec(base), ensure_ascii=False)
               + "\n\nChange request:\n" + msg)
    out = _generate(msg, provider, selection_system_prompt(lang), selection_schema(), to_selection_spec,
                    validate_selection, repair_rounds, lambda o: not o.get("factors"))
    spec = to_selection_spec(out)
    if base:
        spec = _merge_base(base, spec, out)
    return NLResult(spec=spec, name=out.get("name") or "", understood=out.get("understood") or "",
                    unsupported=list(out.get("unsupported") or []), errors=validate_selection(spec, lang), raw=out)


def explain_system_prompt(lang: str = "zh") -> str:
    return f"""You are a careful, candid quantitative analyst explaining a backtest to a non-programmer, who may act on what you say. Write in {LANG_NAMES.get(lang, 'English')}, 4-6 short bullet points: the overall verdict, absolute and relative return, risk, trading behaviour, one or two concrete ideas to try next, and a reminder about overfitting if relevant. Do not invent numbers that are not given.

Judge it by these yardsticks, and say plainly when it is weak:
- Absolute return first: compare the annualized return (cagr) with a risk-free rate of about 2% a year (government bonds, money-market funds). An annualized return near or below that means the strategy earned little or nothing for the risk taken, whatever the excess return.
- Sharpe ratio: below 0.5 is weak, 0.5-1 is moderate, above 1 is good (for a backtest, before real-world frictions). Calmar ratio (cagr / max drawdown): below 0.5 is weak, 0.5-1 is moderate, above 1 is good.
- If the benchmark return is negative, point out that part of the excess return comes from the benchmark falling, not from the strategy making money; never present a large excess return as strong performance on its own.
- Low volatility or a small drawdown is not good risk control by itself; it only counts together with an adequate return (a strategy that barely moves will also show low risk).
- Keep the tone neutral. Do not use praise words for weak numbers, and do not soften a weak verdict."""


def explain_result(provider: Provider, spec_text: str, metrics: dict, lang: str = "zh") -> str:
    """用大白话解读回测结果"""
    system = explain_system_prompt(lang)
    user = f"Strategy:\n{spec_text}\n\nMetrics (ratios, not percents):\n" + json.dumps(
        {k: (round(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()}, ensure_ascii=False)
    return provider.chat(system, [{"role": "user", "content": user}], None).strip()


def ping(provider: Provider) -> str:
    """测试连接：能收到回复即可"""
    return provider.chat("Reply with exactly: OK", [{"role": "user", "content": "ping"}], None).strip()
