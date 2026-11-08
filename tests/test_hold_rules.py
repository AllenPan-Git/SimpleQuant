"""
待办 51：规则的持仓类条件「持仓最高点回撤（%）」dd_from_high 与「持仓天数」hold_days
（BaseStrategy.drawdown_from_high / hold_days；导出脚本与平台导出的一致性见 test_export / test_platform_export）
"""

import numpy as np
import pandas as pd
import pytest

from simplequant import plan as P
from simplequant.engine import BrokerConfig, run_backtest
from simplequant.rules import validate, describe_rule
from simplequant.strategies import resolve

BROKER = BrokerConfig(cash=100_000, commission=0, min_commission=0, slippage=0,
                      price_limit=False)      # 开高低收相同的下跌 K 线会被当成一字跌停
ALWAYS = {"logic": "all", "conditions": [{"left": {"ind": "close"}, "op": ">", "right": {"value": 0}}]}


def prices(close):
    idx = pd.bdate_range("2022-01-03", periods=len(close))
    c = pd.Series(np.asarray(close, dtype=float), index=idx)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6})


def run(close, sell, **rule):
    spec = {"kind": "rule", "rule": {"buy": ALWAYS, "sell": sell, "position_pct": 95, **rule}}
    df = prices(close)
    return df, run_backtest({"a": df}, *resolve(spec), broker=BROKER)


def cond(ind, op, v):
    return {"left": {"ind": ind}, "op": op, "right": {"value": v}}


def test_hold_days_counts_fill_bar_as_day_one():
    """第 0 根收盘发买入、第 1 根开盘成交（第 1 天）；第 5 根收盘满 5 天发卖出、第 6 根开盘成交"""
    df, res = run(np.full(40, 10.0), {"logic": "any", "conditions": [cond("hold_days", ">=", 5)]})
    o = res.orders
    assert o["side"].tolist()[:2] == ["buy", "sell"]
    assert o["time"].iloc[0] == df.index[1] and o["time"].iloc[1] == df.index[6]
    # 卖出当天收盘再发买入，下一根开盘买回：之后每轮同样持有 5 根
    assert o["time"].iloc[2] == df.index[7] and o["time"].iloc[3] == df.index[12]


def test_hold_days_not_reset_by_adding():
    close = 10 * 1.03 ** np.arange(40)                 # 一路上涨，每根都触发「上涨 2% 加仓」
    df, res = run(close, {"logic": "any", "conditions": [cond("hold_days", ">=", 6)]},
                  scale={"adds": 2, "when": "gain", "pct": 2})
    o = res.orders
    assert o["side"].tolist()[:4] == ["buy", "buy", "buy", "sell"]
    assert o["time"].iloc[3] == df.index[7]


def test_drawdown_from_high_trails_highest_close_since_entry():
    # 建仓前的高点（20）不算；建仓后最高收盘 13，回落超过 10%（< 11.7）的那根收盘发卖出
    close = [20, 10, 11, 12, 13, 12.5, 12, 11.8, 11.6, 11, 11, 11]
    df, res = run(close, {"logic": "any", "conditions": [cond("dd_from_high", ">", 10)]}, cooldown=50)
    o = res.orders
    assert o["side"].tolist() == ["buy", "sell"]
    assert o["time"].iloc[1] == df.index[9]          # 第 8 根（11.6）收盘发出，第 9 根开盘成交


def test_drawdown_counts_from_fill_bar_close_not_signal_bar():
    """信号那根（20）不算，成交那根（10）起算：之后一路走平，不会卖"""
    df, res = run([20, 10, 10, 10, 10, 10], {"logic": "any", "conditions": [cond("dd_from_high", ">", 10)]})
    assert res.orders["side"].tolist() == ["buy"]


def test_validation_and_description():
    rule = {"buy": ALWAYS, "sell": {"logic": "any", "conditions": [cond("dd_from_high", ">", 8),
                                                                    cond("hold_days", ">=", 20)]}}
    assert validate(rule) == []
    text = describe_rule(rule, "zh")
    assert "持仓最高点回撤" in text and "持仓天数" in text
    bad = {"buy": ALWAYS, "sell": {"conditions": [{**cond("hold_days", ">=", 3), "days": 2}]}}
    assert validate(bad)
    bad = {"buy": ALWAYS, "sell": {"conditions": [{"left": {"ind": "dd_from_high"}, "op": "cross_above",
                                                   "right": {"value": 5}}]}}
    assert validate(bad)


def test_plan_version():
    rule = {"buy": ALWAYS, "sell": {"logic": "any", "conditions": [cond("hold_days", ">=", 20)]}}
    spec = {"kind": "rule", "rule": rule}
    assert P.timing_version(spec, BrokerConfig()) == P.HOLD_RULE_VERSION <= P.VERSION
    plain = {"kind": "rule", "rule": {"buy": ALWAYS, "sell": {"conditions": [cond("pnl_pct", "<", -8)]}}}
    assert P.timing_version(plain, BrokerConfig()) == P.TIMING_VERSION


def test_nl_keeps_days_one_for_position_indicators():
    from simplequant.llm.nl import to_rule
    out = {"buy": {"logic": "all", "conditions": [
        {"left": {"kind": "indicator", "ind": "close", "line": "", "params": [], "value": 0}, "op": ">",
         "right": {"kind": "value", "ind": "close", "line": "", "params": [], "value": 0}, "days": 1}]},
        "sell": {"logic": "any", "conditions": [
        {"left": {"kind": "indicator", "ind": "dd_from_high", "line": "", "params": [], "value": 0}, "op": ">",
         "right": {"kind": "value", "ind": "close", "line": "", "params": [], "value": 10}, "days": 3}]}}
    rule = to_rule(out)
    assert rule["sell"]["conditions"][0]["left"]["ind"] == "dd_from_high"
    assert "days" not in rule["sell"]["conditions"][0] and validate(rule) == []
