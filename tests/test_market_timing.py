"""
选股的大盘条件（待办 40）：
1. 逐日判断（selection_core._cond_series / sel_market_cash）与 signal_core 在每一天单独判断的结果相同
2. 回测：空仓那天的下一个开盘全部卖出，空仓期间不买，恢复那天选股、次日买回
3. 方案文件版本号与格式检查
"""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from simplequant import plan as P
from simplequant.export import selection_core as core
from simplequant.export import signal_core as sig
from simplequant.stocks import build_schedule, run_selection
from simplequant.stocks.selection import market_cash, market_errors
from test_selection_export import make_panel

MA = {"left": {"ind": "close"}, "op": "<", "right": {"ind": "sma", "params": {"period": 20}}}
BASE = {"kind": "selection", "universe": "hs300", "top_n": 5, "rebalance": "monthly", "position_pct": 95,
        "factors": [{"key": "ret20", "weight": 1, "direction": -1}]}


def wave(n=400, seed=1):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(0.02 * np.sin(np.arange(n) / 15) + rng.normal(0, 0.01, n)))


CONDS = [
    MA,
    {**MA, "days": 3},
    {"left": {"ind": "close"}, "op": "cross_below", "right": {"ind": "ema", "params": {"period": 30}}},
    {"left": {"ind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}, "line": "dif"}, "op": "cross_above",
     "right": {"ind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}, "line": "dea"}},
    {"left": {"ind": "rsi", "params": {"period": 14}}, "op": ">=", "right": {"value": 60}, "days": 2},
    {"left": {"ind": "boll", "params": {"period": 20, "dev": 2.0}, "line": "lower"}, "op": ">",
     "right": {"ind": "close"}},
]


@pytest.mark.parametrize("cond", CONDS)
def test_daily_series_matches_signal_core_each_day(cond):
    close = wave()
    lines = sig._Lines({"close": close})
    got = core._cond_series(cond, lines, len(close))
    for i in range(1, len(close)):
        sub = sig._Lines({"close": close[:i + 1]})
        assert bool(got[i]) == sig._check(cond, sub, {}, close[:i + 1]), i


def test_state_machine():
    close = np.r_[np.full(30, 10.0), np.full(10, 8.0), np.full(10, 12.0), np.full(10, 8.0)]
    only_out = {"market": {"sell": {"conditions": [MA]}}}
    cash = core.sel_market_cash(only_out, close, 0)
    assert not cash[:30].any() and cash[30:40].all()       # 跌破均线就空仓
    assert not cash[40:50].any()                            # 不再满足 → 恢复
    assert cash[50]
    # 恢复条件：收盘 > 11 才回来
    both = {"market": {"sell": {"conditions": [MA]},
                       "buy": {"conditions": [{"left": {"ind": "close"}, "op": ">", "right": {"value": 11}}]}}}
    cash = core.sel_market_cash(both, np.r_[close[:40], np.full(5, 10.5), close[40:]], 0)
    assert cash[30:45].all() and not cash[45:55].any()     # 10.5 不满足恢复条件，仍空仓
    # 回测开始前不算
    assert not core.sel_market_cash(only_out, close, 35)[:35].any()
    assert not core.sel_market_cash({"market": {"buy": {"conditions": [MA]}}}, close, 0).any()


@pytest.fixture(scope="module")
def panel():
    return make_panel()[0]


def test_backtest_sells_on_exit_and_rebuys_on_reentry(panel):
    spec = {**BASE, "market": {"sell": {"logic": "all", "conditions": [MA]}}}
    start = str(panel.calendar[250].date())
    cash = market_cash(panel, spec, start)
    assert 20 < cash.sum() < cash[start:].size - 20
    sched = build_schedule(panel, spec, start)
    flips = cash.index[cash.values != np.r_[False, cash.values[:-1]]]
    assert set(flips) <= set(sched.picks)
    for d, names in sched.picks.items():
        assert (names == []) == bool(cash[d]), d          # 空仓日名单为空；恢复日、其他调仓日照常选股
    res = run_selection(panel, spec, start=start)
    orders = res.orders
    cal = panel.calendar
    for d in flips:
        nxt = cal[cal.get_loc(d) + 1] if cal.get_loc(d) + 1 < len(cal) else None
        if nxt is None:
            continue
        day = orders[orders["time"].dt.normalize() == nxt]
        if cash[d]:
            assert (day["side"] == "sell").all()
        else:
            assert (day["side"] == "buy").any()
    # 空仓期间（进入空仓后的第二个交易日起）没有任何买入
    held_days = [cal[i + 1] for i in range(len(cal) - 1) if cash.iloc[i] and cal[i] >= pd.Timestamp(start)]
    buys = orders[orders["side"] == "buy"]["time"].dt.normalize()
    assert not buys.isin(held_days).any()
    plain = run_selection(panel, BASE, start=start)
    assert res.metrics["total_return"] != plain.metrics["total_return"]


def test_needs_index_prices(panel):
    old = dataclasses.replace(panel, index_close=None)
    with pytest.raises(ValueError, match="指数"):
        build_schedule(old, {**BASE, "market": {"sell": {"conditions": [MA]}}}, str(panel.calendar[250].date()))
    assert build_schedule(old, BASE, str(panel.calendar[250].date())).picks     # 没设大盘条件不受影响


def test_errors_and_plan_version():
    ok = {**BASE, "market": {"sell": {"logic": "any", "conditions": [MA]}}}
    assert market_errors(ok) == [] and market_errors(BASE) == []
    atr = {"left": {"ind": "atr", "params": {"period": 14}}, "op": ">", "right": {"value": 1}}
    pnl = {"left": {"ind": "pnl_pct"}, "op": "<", "right": {"value": -5}}
    for bad in ({"sell": {"conditions": [atr]}}, {"sell": {"conditions": [pnl]}},
                {"sell": {"conditions": [MA]}, "buy": {"conditions": [atr]}},
                {"buy": {"conditions": [MA]}}, "x"):
        assert market_errors({**BASE, "market": bad}), bad
    plan = P.make_selection_plan("x", ok, "2021-01-04", "2022-01-04", "2020-01-02", P.BrokerConfig())
    assert plan["version"] == P.MARKET_VERSION <= P.VERSION
    assert P.loads(P.dumps(plan))["strategy"]["market"] == ok["market"]
    plan["strategy"]["market"] = {"sell": {"conditions": [atr]}}
    with pytest.raises(P.PlanError):
        P.loads(P.dumps(plan))


def test_describe():
    from simplequant import strategies
    text = strategies.describe({**BASE, "market": {"sell": {"conditions": [MA]}}})
    assert "【大盘空仓条件】收盘价 小于 均线 MA(20)" in text and "空仓条件不再满足" in text
