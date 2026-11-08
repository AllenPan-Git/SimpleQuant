"""
待办 55：闲置现金计息（BrokerConfig.cash_yield）。未买入证券的现金按年化收益率、按自然日复利，
每个交易日开盘前计入；预热期不计息；利息算收益、不算投入
"""

import dataclasses
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from simplequant import plan as P
from simplequant.engine import BrokerConfig
from simplequant.engine.base_strategy import BaseStrategy
from simplequant.engine.runner import run_backtest
from simplequant.strategies.templates import BuyHold
from test_contribution import prices, run_tpl, valuation_prices


class Idle(BaseStrategy):
    """从不下单：资产全是现金"""
    def next(self):
        pass


def broker(**kw):
    return BrokerConfig(**{"cash": 100_000, "slippage": 0, "commission": 0, "min_commission": 0, **kw})


def test_idle_cash_compounds_by_calendar_day():
    df = prices(np.full(300, 10.0))
    res = run_backtest({"a": df}, Idle, broker=broker(cash_yield=0.02))
    eq = res.equity["value"]
    days = (eq.index[-1] - eq.index[0]).days
    assert eq.iloc[0] == pytest.approx(100_000)
    assert eq.iloc[-1] == pytest.approx(100_000 * 1.02 ** (days / 365))
    assert res.metrics["cash_interest"] == pytest.approx(eq.iloc[-1] - 100_000)
    # 周末也计息：周一比周五多三天的利息
    mon = next(i for i, d in enumerate(eq.index) if d.weekday() == 0 and i > 0)
    assert eq.iloc[mon] / eq.iloc[mon - 1] == pytest.approx(1.02 ** (3 / 365))


def test_zero_yield_unchanged():
    df = prices(10 * 1.001 ** np.arange(300))
    plain = run_backtest({"a": df}, BuyHold, broker=broker())
    zero = run_backtest({"a": df}, BuyHold, broker=broker(cash_yield=0.0))
    pd.testing.assert_frame_equal(plain.equity, zero.equity)
    assert "cash_interest" not in plain.metrics


def test_only_cash_earns_interest():
    """买入持有：第一根收盘发信号、第二根开盘成交，这一夜全部现金计息；之后只有建仓剩下的零头计息，持仓不计息"""
    df = prices(10 * 1.001 ** np.arange(300))
    plain = run_backtest({"a": df}, BuyHold, broker=broker())
    paid = run_backtest({"a": df}, BuyHold, broker=broker(cash_yield=0.02))
    assert plain.orders["size"].tolist() == paid.orders["size"].tolist()
    first = 100_000 * (1.02 ** ((df.index[1] - df.index[0]).days / 365) - 1)
    left = plain.equity["cash"].iloc[1] + first
    later = left * (1.02 ** ((df.index[-1] - df.index[1]).days / 365) - 1)
    assert paid.metrics["cash_interest"] == pytest.approx(first + later)


def test_warm_up_earns_nothing():
    df = prices(np.full(300, 10.0))
    start = df.index[100]
    res = run_backtest({"a": df}, Idle, broker=broker(cash_yield=0.05), trade_start=start)
    assert res.equity["value"].iloc[0] == pytest.approx(100_000)
    days = (df.index[-1] - start).days
    assert res.equity["value"].iloc[-1] == pytest.approx(100_000 * 1.05 ** (days / 365))


def test_value_dca_reserve_not_counted_twice():
    """估值定投的储备按自己的 reserve_rate 计息，闲置现金计息不重复算它"""
    df = valuation_prices()
    start = df.index[300]
    a = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 10}, trade_start=start)
    b = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 10}, trade_start=start,
                broker={"cash_yield": 0.10})
    # 储备之外只有凑不满一手的零头在计息，差额很小
    gain = b.equity["value"].iloc[-1] - a.equity["value"].iloc[-1]
    assert 0 < gain < 100


def test_script_export_matches(capsys, tmp_path, monkeypatch):
    """导出的独立脚本同样计息，资产与软件里的回测相同"""
    monkeypatch.chdir(tmp_path)
    from simplequant.export.python_script import single_asset_script
    from simplequant.strategies import resolve
    from tests.test_export import exec_script
    df = prices(10 * (1 + 0.2 * np.sin(np.arange(400) / 20)))
    spec = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}
    b = broker(cash_yield=0.03, commission=0.00025, min_commission=5)
    code = single_asset_script(spec, [{"name": "a", "source": "csv", "path": "a.csv"}], asdict(b))
    ns = exec_script(code)
    strat = ns["run"]({"a": df})
    ours = run_backtest({"a": df}, *resolve(spec), broker=b)
    assert len(ours.orders) >= 4 and ours.metrics["cash_interest"] > 0
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.orders.reset_index(drop=True))
    assert strat.broker.getvalue() == pytest.approx(ours.equity["value"].iloc[-1])


def test_plan_version():
    spec = {"kind": "template", "template": "buy_hold", "params": {}}
    assert P.timing_version(spec, BrokerConfig()) == P.TIMING_VERSION
    assert P.timing_version(spec, BrokerConfig(cash_yield=0.02)) == P.CASH_YIELD_VERSION <= P.VERSION
    assert P.make_plan("x", spec, [], "2020-01-02", "2021-01-04",
                       BrokerConfig(cash_yield=0.02))["broker"]["cash_yield"] == 0.02


# ---------------- 选股 ----------------
from test_selection_dca import BASE, BROKER, fixed_schedule, panel, start  # noqa: E402,F401  (fixtures)
from simplequant.stocks import run_selection  # noqa: E402


def test_selection_zero_yield_unchanged(panel, start):
    plain = run_selection(panel, BASE, BROKER, start=start)
    zero = run_selection(panel, BASE, dataclasses.replace(BROKER, cash_yield=0.0), start=start)
    pd.testing.assert_frame_equal(plain.equity, zero.equity)


def test_selection_skip_months_cash_earns(panel, start):
    """空仓月份全是现金：计息后资产更高；起点仍是初始资金（预热期不计息）"""
    spec = {**BASE, "skip_months": [1, 4]}
    plain = run_selection(panel, spec, BROKER, start=start)
    paid = run_selection(panel, spec, dataclasses.replace(BROKER, cash_yield=0.02), start=start)
    assert paid.equity["value"].iloc[0] == pytest.approx(BROKER.cash)
    assert paid.metrics["cash_interest"] > 0
    assert paid.equity["value"].iloc[-1] > plain.equity["value"].iloc[-1]
    assert P.selection_version(spec, dataclasses.replace(BROKER, cash_yield=0.02)) == P.CASH_YIELD_VERSION


def test_selection_flat_result_earns(panel):
    """模拟盘刚开户、还没到调仓日：资产只有现金利息"""
    from simplequant.stocks.selection import Schedule
    b = dataclasses.replace(BROKER, cash_yield=0.02)
    res = run_selection(panel, BASE, b, schedule=Schedule(picks={}, scores={}),
                        start=str(panel.calendar[-30].date()), allow_empty=True)
    eq = res.equity["value"]
    assert eq.iloc[0] == pytest.approx(BROKER.cash)
    assert eq.iloc[-1] == pytest.approx(BROKER.cash * 1.02 ** ((eq.index[-1] - eq.index[0]).days / 365))
