"""
择时回测按真实股价凑整手（行情带不复权收盘价 raw_close 时，见 engine/base_strategy.real_factor）
"""

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_export import exec_script
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.export.python_script import single_asset_script
from ui.shared import real_orders

BROKER = BrokerConfig(cash=100_000)


def adjusted(raw: pd.DataFrame, factor) -> pd.DataFrame:
    """不复权行情 × 复权因子，附上 raw_close（与 data/ratio_adjust.apply 相同）"""
    f = pd.Series(factor, index=raw.index) if np.ndim(factor) else pd.Series(float(factor), index=raw.index)
    out = raw.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = raw[c] * f
    out["raw_close"] = raw["close"]
    return out


def spec(template, **params):
    return {"kind": "template", "template": template, "params": params}


def test_lots_use_real_price():
    """后复权价是真实价的 50 倍：按真实股价凑整手能买满仓；按复权价一手要 50 倍的钱，只能买到一半左右"""
    raw = make_prices(n=200)
    adj = adjusted(raw, 50.0)
    cls, params = strategies.resolve(spec("buy_hold"))
    res = run_backtest({"510300": adj}, cls, params, BROKER)
    real = real_orders(res.orders, res.prices)
    assert real["size"].iloc[0] % 100 == 0 and real["size"].iloc[0] >= 9000
    assert real["price"].iloc[0] == pytest.approx(res.orders["price"].iloc[0] / 50)
    assert (real["value"] == res.orders["value"]).all()                     # 成交额不变
    assert res.orders["value"].iloc[0] > 90_000

    old = run_backtest({"510300": adj.drop(columns="raw_close")}, cls, params, BROKER)
    assert old.orders["size"].iloc[0] == 100 and old.orders["value"].iloc[0] < 60_000

    pos = res.positions[0]                                                  # 期末持仓也换算成真实股数、真实价格
    assert pos["size"] == real["size"].iloc[0] and pos["price"] == pytest.approx(raw["close"].iloc[-1])


def test_lots_after_factor_change_and_rebalance_without_odd_lots():
    """除息后因子变大、持有的真实股数不再是整手：再平衡时只按整手买卖，不为零头付最低佣金"""
    a, b = make_prices(n=400, seed=1), make_prices(n=400, seed=2, drift=-0.0002)
    fa = np.where(np.arange(400) >= 150, 1.04, 1.0) * np.where(np.arange(400) >= 300, 1.03, 1.0)
    prices = {"510300": adjusted(a, fa * 3), "511010": adjusted(b, 1.0)}
    cls, params = strategies.resolve(spec("fixed_weight", weight=60, rebalance_bars=10, band=0))
    res = run_backtest(prices, cls, params, BROKER)
    real = real_orders(res.orders, res.prices)
    assert len(real) > 10
    assert (real["size"].round(6) % 100 == 0).all()


def test_python_script_export_matches_with_raw_close():
    """导出的独立脚本同样按真实股价凑整手，逐笔一致"""
    prices = {"510300": adjusted(make_prices(n=300), np.linspace(20, 23, 300))}
    sp = spec("sma_cross", fast=5, slow=20)
    code = single_asset_script(sp, [{"name": "510300", "source": "csv", "path": "510300.csv"}], asdict(BROKER))
    ns = exec_script(code)
    strat = ns["run"](prices)
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    ours = run_backtest(prices, *strategies.resolve(sp), BROKER).orders
    assert len(ours) > 4
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.reset_index(drop=True))
