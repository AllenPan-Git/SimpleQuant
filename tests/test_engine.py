import pandas as pd
import pytest

from conftest import make_prices
from simplequant.engine import run_backtest, BrokerConfig, AShareCommission
from simplequant.rules import make_ind
from simplequant.strategies import resolve, TEMPLATES

NO_COST = BrokerConfig(commission=0, min_commission=0, stamp_duty=0, slippage=0)


def test_commission_min_and_stamp_duty():
    c = AShareCommission(commission=0.0002, min_commission=5.0, stamp_duty=0.0005)
    assert c.getcommission(1000, 10.0) == pytest.approx(5.0)            # 2 元 < 最低 5 元
    assert c.getcommission(100000, 10.0) == pytest.approx(200.0)        # 万二
    assert c.getcommission(-1000, 10.0) == pytest.approx(5.0 + 5.0)     # 卖出加收印花税


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_every_template_runs(key):
    datas = {"A": make_prices(seed=1), "B": make_prices(seed=2, drift=0.0006)}
    cls, params = resolve({"kind": "template", "template": key})
    res = run_backtest(datas, cls, params, BrokerConfig())
    assert len(res.equity) == 400
    assert res.metrics["final_value"] > 0
    if key != "buy_hold":
        assert res.metrics["trades"] > 0, key
    assert (res.orders["size"] % 100 == 0).all() or key == "momentum_rotation"


def test_lot_size_and_cash_never_negative(prices):
    cls, params = resolve({"kind": "template", "template": "sma_cross"})
    res = run_backtest({"A": prices}, cls, params, BrokerConfig())
    buys = res.orders[res.orders["side"] == "buy"]
    assert (buys["size"] % 100 == 0).all()
    assert (res.equity["cash"] >= 0).all()


def test_rule_builder_matches_template(prices):
    """规则搭建器拼出的双均线，结果应与内置双均线模板完全一致"""
    tpl_cls, tpl_params = resolve({"kind": "template", "template": "sma_cross",
                                   "params": {"fast": 5, "slow": 20, "position_pct": 95}})
    rule = {
        "buy": {"logic": "all", "conditions": [
            {"left": make_ind("sma", period=5), "op": "cross_above", "right": make_ind("sma", period=20)}]},
        "sell": {"logic": "any", "conditions": [
            {"left": make_ind("sma", period=5), "op": "cross_below", "right": make_ind("sma", period=20)}]},
        "position_pct": 95,
    }
    rule_cls, rule_params = resolve({"kind": "rule", "rule": rule})
    a = run_backtest({"A": prices}, tpl_cls, tpl_params, BrokerConfig())
    b = run_backtest({"A": prices}, rule_cls, rule_params, BrokerConfig())
    assert a.metrics["trades"] > 3
    assert b.metrics["final_value"] == pytest.approx(a.metrics["final_value"])
    pd.testing.assert_frame_equal(a.orders, b.orders)


def test_cross_against_constant_and_stop_loss(prices):
    rule = {
        "buy": {"logic": "all", "conditions": [
            {"left": make_ind("rsi", period=6), "op": "cross_above", "right": {"value": 30}}]},
        "sell": {"logic": "any", "conditions": [
            {"left": make_ind("rsi", period=6), "op": ">", "right": {"value": 75}},
            {"left": {"ind": "pnl_pct"}, "op": "<", "right": {"value": -3}}]},
        "position_pct": 50,
    }
    cls, params = resolve({"kind": "rule", "rule": rule})
    res = run_backtest({"A": prices}, cls, params, NO_COST)
    assert res.metrics["trades"] > 0
    buys = res.orders[res.orders["side"] == "buy"]
    # 50% 仓位：每次买入金额不超过当时总资产的一半
    assert (buys["value"] <= res.equity["value"].max() * 0.5 + 1).all()


def test_t_plus_1_blocks_same_day_sell():
    """分钟数据：买入后下一根就满足卖出条件，但必须等到下一个交易日才卖"""
    idx = pd.date_range("2024-01-02 09:31", periods=240, freq="min")
    idx = idx.append(pd.date_range("2024-01-03 09:31", periods=240, freq="min"))
    df = make_prices(n=len(idx))
    df.index = idx
    rule = {"buy": {"logic": "all", "conditions": [
                {"left": make_ind("close"), "op": ">", "right": {"value": 0}}]},
            "sell": {"logic": "all", "conditions": [
                {"left": make_ind("close"), "op": ">", "right": {"value": 0}}]},
            "position_pct": 90}
    cls, params = resolve({"kind": "rule", "rule": rule})

    res = run_backtest({"A": df}, cls, params, BrokerConfig(t_plus_1=True))
    first_buy = res.orders[res.orders["side"] == "buy"]["time"].iloc[0]
    first_sell = res.orders[res.orders["side"] == "sell"]["time"].iloc[0]
    assert first_sell.date() > first_buy.date()

    res0 = run_backtest({"A": df}, cls, params, BrokerConfig(t_plus_1=False))
    sells0 = res0.orders[res0.orders["side"] == "sell"]
    assert sells0["time"].iloc[0].date() == pd.Timestamp("2024-01-02").date()


def test_lot_too_big_is_reported_once():
    """价格太高、一手都买不起时：不下单，记录一次提示（而不是静默零交易）"""
    df = make_prices()
    df[["open", "high", "low", "close"]] *= 1000          # 约 10000 元/股，一手约 100 万
    cls, params = resolve({"kind": "template", "template": "sma_cross"})
    res = run_backtest({"A": df}, cls, params, BrokerConfig(cash=100_000))
    assert res.orders.empty and res.lot_too_big == ["A"]
    assert sum(1 for _, key, _ in res.logs if key == "log.lot_too_big") == 1
    ok = run_backtest({"A": df}, cls, params, BrokerConfig(cash=5_000_000))
    assert not ok.orders.empty and ok.lot_too_big == []


def test_metrics_on_buy_and_hold_track_benchmark(prices):
    cls, params = resolve({"kind": "template", "template": "buy_hold", "params": {"position_pct": 100}})
    res = run_backtest({"A": prices}, cls, params, NO_COST)
    m = res.metrics
    # 满仓持有（整手取整 + 1% 预留，外加按次日开盘成交）应与基准非常接近
    assert m["total_return"] == pytest.approx(m["benchmark_return"], abs=0.03)
    assert -1 < m["max_drawdown"] < 0
