"""Backtrader 原生图表：不弹窗生成 PNG、区间限制、画图时的交易与回测一致"""

import matplotlib
import pandas as pd
import pytest

from conftest import make_prices
from simplequant import strategies
from simplequant.engine import BrokerConfig, run_backtest
from simplequant.engine.native_plot import render, MAX_BARS
from simplequant.engine.runner import build_cerebro
from simplequant.rules import make_ind

RULE = {"kind": "rule", "rule": {
    "buy": {"logic": "all", "conditions": [
        {"left": make_ind("macd"), "op": "cross_above", "right": {**make_ind("macd"), "line": "dea"}}]},
    "sell": {"logic": "any", "conditions": [
        {"left": make_ind("macd"), "op": "cross_below", "right": {**make_ind("macd"), "line": "dea"}}]},
    "position_pct": 90}}


def test_render_png_without_gui_backend():
    prices = {"A": make_prices(n=400)}
    png = render(prices, *strategies.resolve(RULE), BrokerConfig(), start=prices["A"].index[150], lang="en")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    assert matplotlib.get_backend().lower() == "agg"


def test_render_rejects_too_long_range():
    prices = {"A": make_prices(n=MAX_BARS + 100)}
    with pytest.raises(ValueError, match="K 线"):
        render(prices, *strategies.resolve(RULE), BrokerConfig())


def test_shared_indicators_are_built_once():
    """规则里用到 MACD 的 DIF 和 DEA、上穿和下穿：只建一个 MACD、一个交叉（图上也只画一次）"""
    import backtrader as bt
    prices = {"A": make_prices(n=200)}
    strat = build_cerebro(prices, *strategies.resolve(RULE), BrokerConfig()).run()[0]
    kinds = [type(i).__name__ for i in strat.getindicators()]
    assert kinds.count("MACD") == 1 and kinds.count("CrossOver") == 1
    assert not next(i for i in strat.getindicators() if isinstance(i, bt.ind.CrossOver)).plotinfo.plot


def test_plotting_run_trades_match_backtest():
    """原生图使用同一套回测设置：加了观察器后成交不变"""
    prices = {"A": make_prices(n=400)}
    broker = BrokerConfig(cash=300_000, commission=0.0003, stamp_duty=0.0005)
    cerebro = build_cerebro(prices, *strategies.resolve(RULE), broker)
    from simplequant.engine.native_plot import _observers
    for obs in _observers("zh"):
        cerebro.addobserver(obs)
    strat = cerebro.run()[0]
    plotted = pd.DataFrame(strat.order_records)
    ours = run_backtest(prices, *strategies.resolve(RULE), broker).orders
    pd.testing.assert_frame_equal(plotted.reset_index(drop=True), ours.reset_index(drop=True))
