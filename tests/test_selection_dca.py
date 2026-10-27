"""
选股回测的定投（待办 38 收尾）：每期追加的钱留作现金，到下一个调仓日开盘再买；
先卖落选、买新入选，剩下的按目标仓位补到继续入选的持仓上（只补不减）
"""

import dataclasses

import pandas as pd
import pytest

from simplequant import plan as P
from simplequant.engine import BrokerConfig
from simplequant.engine.results import hold_with_flows
from simplequant.engine.runner import contribution_on_days
from simplequant.export.selection_platforms import selection_platform_script
from simplequant.stocks import run_selection
from simplequant.stocks.selection import Schedule, rebalance_dates
from test_selection_export import make_panel

BASE = {"kind": "selection", "universe": "hs300", "top_n": 5, "rebalance": "monthly", "position_pct": 95,
        "factors": [{"key": "ret20", "weight": 1, "direction": -1}]}
BROKER = BrokerConfig(cash=1_000_000, commission=0.00025, stamp_duty=0.0005, slippage=0)


@pytest.fixture(scope="module")
def panel():
    return make_panel()[0]


@pytest.fixture(scope="module")
def start(panel):
    return str(panel.calendar[250].date())


def fixed_schedule(panel, start, n=4):
    """每月调仓、名单固定不变的 n 只（全程可买卖）：调仓日只有补仓，没有换股"""
    cal = panel.calendar[panel.calendar >= pd.Timestamp(start)]
    ok = [c for c in panel.can_buy.columns
          if panel.can_buy[c].loc[cal].all() and panel.can_sell[c].loc[cal].all()][:n]
    assert len(ok) == n
    dates = rebalance_dates(cal, "monthly")
    return Schedule(picks={d: list(ok) for d in dates}, scores={d: {} for d in dates})


def test_no_contribution_unchanged(panel, start):
    plain = run_selection(panel, BASE, BROKER, start=start)
    zero = run_selection(panel, BASE, dataclasses.replace(BROKER, contribution=0), start=start)
    pd.testing.assert_frame_equal(plain.equity, zero.equity)
    assert "invested" not in plain.equity and "invested" not in plain.metrics


def test_contribution_metrics_and_benchmark(panel, start):
    b = dataclasses.replace(BROKER, contribution=50_000)
    res = run_selection(panel, BASE, b, start=start)
    flows = contribution_on_days(panel.calendar, b, start)
    m, eq = res.metrics, res.equity
    assert m["contributions"] == len(flows) > 5
    assert m["invested"] == pytest.approx(1_000_000 + 50_000 * len(flows))
    assert eq["invested"].iloc[-1] == pytest.approx(m["invested"])
    assert m["total_return"] == pytest.approx(m["final_value"] / m["invested"] - 1)
    bench = panel.benchmark.reindex(eq.index).ffill()
    pd.testing.assert_series_equal(eq["benchmark"], hold_with_flows(bench / bench.iloc[0], b.cash, flows),
                                   check_names=False)
    assert sum(1 for _, k, _ in res.logs if k == "log.contribution") == len(flows)


def test_new_money_is_invested_on_rebalance_day(panel, start):
    """月度追加与月度调仓同一天：当天开盘就用上，收盘后现金只剩约 5%（仓位 95%）加不足一手的零头"""
    res = run_selection(panel, BASE, dataclasses.replace(BROKER, contribution=50_000), start=start)
    eq = res.equity
    traded = sorted(set(res.orders["time"].dt.normalize()))
    ratio = (eq["cash"] / eq["value"]).loc[traded]
    assert ratio.max() < 0.07


def test_top_up_held_names_without_selling(panel, start):
    sched = fixed_schedule(panel, start)
    b = dataclasses.replace(BROKER, contribution=100_000)
    plain = run_selection(panel, BASE, BROKER, schedule=sched, start=start)
    dca = run_selection(panel, BASE, b, schedule=sched, start=start)
    # 不追加：名单不变，只有第一天的买入
    assert (plain.orders["side"] == "buy").sum() == 4 and (plain.orders["side"] == "sell").sum() == 0
    # 追加：不卖，每个调仓日（最后一个在数据末尾，没有下一个开盘）都给低于目标仓位的补仓；超过目标的不动
    o = dca.orders
    assert (o["side"] == "sell").sum() == 0
    buys = o[o["side"] == "buy"].groupby(o["time"].dt.normalize())["symbol"].nunique()
    assert len(buys) == len(sched.picks) - 1 and buys.iloc[0] == 4 and (buys >= 1).all() and (buys < 4).any()
    assert o[o["side"] == "buy"]["symbol"].value_counts().min() > 3
    eq = dca.equity
    assert (eq["cash"] / eq["value"]).iloc[-1] < 0.12


def test_weekly_money_waits_for_monthly_rebalance(panel, start):
    sched = fixed_schedule(panel, start)
    res = run_selection(panel, BASE, dataclasses.replace(BROKER, contribution=20_000, contribution_freq="weekly"),
                        schedule=sched, start=start)
    cal = panel.calendar
    exec_days = {cal[cal.get_loc(d) + 1] for d in sched.picks if cal.get_loc(d) + 1 < len(cal)}
    assert set(res.orders["time"].dt.normalize()) <= exec_days
    assert res.metrics["contributions"] > 3 * len(exec_days) // 2


def test_cash_kept_during_skipped_months(panel, start):
    spec = {**BASE, "skip_months": [4]}
    res = run_selection(panel, spec, dataclasses.replace(BROKER, contribution=50_000), start=start)
    eq, o = res.equity, res.orders
    april = eq.index[eq.index.month == 4]
    buys = o[o["side"] == "buy"]["time"].dt.normalize()
    assert not buys.isin(april).any()
    assert (eq.loc[april, "cash"].diff().dropna() >= -1e-6).all()       # 空仓期间现金只增不减
    assert eq.loc[april, "cash"].iloc[-1] / eq.loc[april, "value"].iloc[-1] > 0.99


def test_plan_version_and_platform_export(panel, start):
    b = dataclasses.replace(BROKER, contribution=2_000)
    plan = P.make_selection_plan("dca", BASE, start, "2022-12-31", "2021-01-04", b)
    assert plan["version"] == P.SEL_DCA_VERSION <= P.VERSION
    assert P.broker_of(plan).contribution == 2_000
    assert P.make_selection_plan("x", BASE, start, "2022-12-31", "2021-01-04", BROKER)["version"] \
        == P.SELECTION_VERSION
    with pytest.raises(ValueError, match="追加资金"):
        selection_platform_script("joinquant", BASE, dataclasses.asdict(b), start, "2022-12-31", "2021-01-04")
