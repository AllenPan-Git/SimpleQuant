"""
按品种执行 T+1 / T+0（engine/market.py），以及股债定比再平衡模板
回测、独立脚本导出、平台导出三处的成交必须一致
"""

import pandas as pd
import pytest

from conftest import make_prices
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.market import is_t0, t0_names
from simplequant.rules import make_ind
from simplequant.strategies import resolve

import test_export
import test_platform_export as tpe


@pytest.mark.parametrize("name, t0", [
    ("511010 国债ETF", True), ("511880", True), ("511380 可转债ETF", True),
    ("513100 纳指ETF", True), ("518880 黄金ETF", True),
    ("113052 兴业转债", True), ("123107", True), ("128013 洪涛转债", True), ("019742", True),
    ("510900 H股ETF", True), ("159934", True), ("159649 国开债ETF", True), ("159920 恒生ETF", True),
    ("510300 沪深300ETF", False), ("159915 创业板ETF", False), ("588000", False), ("512400 有色金属ETF", False),
    ("159562 黄金股ETF", False), ("600000", False), ("000001 平安银行", False), ("300750", False),
    ("国债ETF", True), ("my_data", False), ("", False),
])
def test_is_t0(name, t0):
    assert is_t0(name) is t0


def test_t0_names_keeps_order():
    assert t0_names(["510300 沪深300ETF", "511010 国债ETF", "518880"]) == ("511010 国债ETF", "518880")


# 每根 K 线都同时满足买入、卖出条件：空仓就买，持仓就卖
FLIP = {"buy": {"logic": "all", "conditions": [{"left": make_ind("close"), "op": ">", "right": {"value": 0}}]},
        "sell": {"logic": "all", "conditions": [{"left": make_ind("close"), "op": ">", "right": {"value": 0}}]},
        "position_pct": 45}


def test_daily_t0_asset_sells_one_day_earlier():
    """日线：买入成交当天收盘的卖出信号，T+1 品种要再等一天，T+0 品种当天即可下单"""
    df = make_prices(n=30)
    prices = {"510300 沪深300ETF": df, "511010 国债ETF": df}
    cls, params = resolve({"kind": "rule", "rule": FLIP})
    res = run_backtest(prices, cls, params, BrokerConfig())
    assert res.t0 == ["511010 国债ETF"]
    first_sell = res.orders[res.orders["side"] == "sell"].groupby("symbol")["time"].first()
    assert first_sell["511010 国债ETF"] == df.index[2]
    assert first_sell["510300 沪深300ETF"] == df.index[3]

    res0 = run_backtest(prices, cls, params, BrokerConfig(t_plus_1=False))
    assert res0.t0 == []
    first_sell0 = res0.orders[res0.orders["side"] == "sell"].groupby("symbol")["time"].first()
    assert (first_sell0 == df.index[2]).all()


T0_PRICES = {"510300 沪深300ETF": make_prices(seed=1, n=120), "511010 国债ETF": make_prices(seed=2, n=120)}


def test_t0_python_script_export_matches():
    test_export.export_and_compare({"kind": "rule", "rule": FLIP}, T0_PRICES)


@pytest.mark.parametrize("platform", list(tpe.RUNNERS))
def test_t0_platform_export_matches(platform, monkeypatch):
    monkeypatch.setitem(tpe.SYMBOLS, "510300 沪深300ETF", "510300")
    monkeypatch.setitem(tpe.SYMBOLS, "511010 国债ETF", "511010")
    code = tpe.export_and_compare(platform, {"kind": "rule", "rule": FLIP}, T0_PRICES)
    assert "T0_NAMES = ['511010 国债ETF']" in code


# ---------------- 定比再平衡 ----------------
FW = {"kind": "template", "template": "fixed_weight", "params": {"weight": 50, "rebalance_bars": 5, "band": 0}}


def _volatile():
    return {"A": make_prices(seed=1, n=300, amp=0.3, period=40), "B": make_prices(seed=2, n=300, amp=0.05)}


def test_fixed_weight_builds_target_weights():
    """按信号当天收盘价计算：第一个标的 95% × 60%，其余平分 95% × 40%（成交价是次日开盘价，会略有出入）"""
    prices = {"A": make_prices(seed=1), "B": make_prices(seed=2), "C": make_prices(seed=3)}
    spec = {"kind": "template", "template": "fixed_weight", "params": {"weight": 60}}
    res = run_backtest(prices, *resolve(spec), BrokerConfig(cash=1_000_000))
    first = res.orders[res.orders["time"] == res.orders["time"].iloc[0]].set_index("symbol")["size"]
    day0 = prices["A"].index[0]
    weights = {n: first[n] * prices[n].loc[day0, "close"] / 1_000_000 for n in prices}
    assert weights["A"] == pytest.approx(0.57, abs=0.002)
    assert weights["B"] == pytest.approx(0.19, abs=0.002)
    assert weights["C"] == pytest.approx(0.19, abs=0.002)


def test_fixed_weight_band_limits_trading():
    prices = _volatile()
    tight = run_backtest(prices, *resolve(FW), BrokerConfig(cash=1_000_000)).orders
    loose_spec = {**FW, "params": {**FW["params"], "band": 20}}
    loose = run_backtest(prices, *resolve(loose_spec), BrokerConfig(cash=1_000_000)).orders
    assert (tight["side"] == "sell").sum() > 5
    assert len(loose) < len(tight)


def test_fixed_weight_tops_up_after_selling():
    """卖出超配的资金到账后，下一根 K 线补足低配的"""
    res = run_backtest(_volatile(), *resolve(FW), BrokerConfig(cash=1_000_000))
    reasons = [(t, kw["reason"][0]) for t, key, kw in res.logs if key in ("log.buy", "log.sell")]
    sells = {t for t, r in reasons if r == "reason.fw_sell"}
    days = sorted({t for t, _ in reasons})
    assert any(t in sells and nxt not in sells and (nxt, "reason.fw_buy") in reasons
               for t, nxt in zip(days, days[1:]) if (nxt - t) <= pd.Timedelta(days=4))


def test_fixed_weight_python_script_export_matches():
    test_export.export_and_compare(FW, _volatile())


@pytest.mark.parametrize("platform", list(tpe.RUNNERS))
def test_fixed_weight_platform_export_matches(platform):
    tpe.export_and_compare(platform, FW, _volatile())
