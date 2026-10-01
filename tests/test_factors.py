"""择时因子：数值与独立的 pandas 参考实现对比，并确认每个因子都能在规则里跑通"""

import math

import backtrader as bt
import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from simplequant.data import normalize
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.runner import _feed
from simplequant.rules import INDICATORS, make_ind, validate, required_columns
from simplequant.strategies import resolve
from simplequant.strategies.rule_strategy import _build_line


def with_factor_columns(df):
    rng = np.random.default_rng(7)
    df = df.copy()
    df["turnover"] = rng.uniform(0.2, 3.0, len(df))
    df["pe"] = 20 + np.cumsum(rng.normal(0, 0.3, len(df)))
    df["pb"] = 2 + np.cumsum(rng.normal(0, 0.02, len(df)))
    df["ps"] = 5 + np.cumsum(rng.normal(0, 0.05, len(df)))
    # 利率列（回测前由 simplequant/bonds/rates.py 并入）
    df["cgb10y"] = 2.5 + np.cumsum(rng.normal(0, 0.01, len(df)))
    df["term_spread"] = 60 + np.cumsum(rng.normal(0, 1, len(df)))
    df["credit_spread"] = 70 + np.cumsum(rng.normal(0, 1, len(df)))
    return df


def line_values(df, spec) -> pd.Series:
    """在 Backtrader 里计算某个因子，逐 bar 取值"""
    class Rec(bt.Strategy):
        def __init__(self):
            self.line = _build_line(self.data, spec)
            self.vals = []

        def prenext(self):
            self.vals.append(math.nan)

        def next(self):
            self.vals.append(self.line[0])

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(_feed(df, "A"))
    cerebro.addstrategy(Rec)
    strat = cerebro.run()[0]
    return pd.Series(strat.vals, index=df.index[:len(strat.vals)])


@pytest.fixture
def prices():
    return with_factor_columns(make_prices(n=300))


def test_kdj_matches_reference(prices):
    n, m1, m2 = 9, 3, 3
    lo, hi = prices["low"].rolling(n).min(), prices["high"].rolling(n).max()
    rsv = ((prices["close"] - lo) / (hi - lo) * 100).dropna()
    k, d, ks, ds = 50.0, 50.0, [], []
    for v in rsv:
        k = (k * (m1 - 1) + v) / m1
        d = (d * (m2 - 1) + k) / m2
        ks.append(k)
        ds.append(d)
    ref_k, ref_d = pd.Series(ks, index=rsv.index), pd.Series(ds, index=rsv.index)
    got_k = line_values(prices, {"ind": "kdj", "params": {"period": n, "m1": m1, "m2": m2}, "line": "k"}).dropna()
    got_j = line_values(prices, {"ind": "kdj", "params": {"period": n, "m1": m1, "m2": m2}, "line": "j"}).dropna()
    pd.testing.assert_series_equal(got_k, ref_k, check_names=False, rtol=1e-9)
    pd.testing.assert_series_equal(got_j, 3 * ref_k - 2 * ref_d, check_names=False, rtol=1e-9)


def test_obv_matches_reference(prices):
    sign = np.sign(prices["close"].diff()).fillna(0)
    ref = (sign * prices["volume"]).cumsum()
    got = line_values(prices, make_ind("obv"))
    pd.testing.assert_series_equal(got, ref, check_names=False, rtol=1e-9)


def test_vol_ratio_bias_slope_volatility_match_reference(prices):
    c, v = prices["close"], prices["volume"]
    checks = {
        "vol_ratio": (make_ind("vol_ratio", period=5), v / v.rolling(5).mean().shift(1)),
        "bias": (make_ind("bias", period=20), (c / c.rolling(20).mean() - 1) * 100),
        "ma_slope": (make_ind("ma_slope", period=20, n=5), (c.rolling(20).mean() / c.rolling(20).mean().shift(5) - 1) * 100),
        "volatility": (make_ind("volatility", period=20), c.pct_change().rolling(20).std(ddof=0) * 100),
    }
    for name, (spec, ref) in checks.items():
        got = line_values(prices, spec)
        both = pd.concat([got, ref], axis=1).dropna()
        assert len(both) > 200, name
        np.testing.assert_allclose(both.iloc[:, 0], both.iloc[:, 1], rtol=1e-6, err_msg=name)


def test_valuation_columns_pass_through(prices):
    for col in ("turnover", "pe", "pb", "ps", "cgb10y", "term_spread", "credit_spread"):
        got = line_values(prices, make_ind(col))
        np.testing.assert_allclose(got.values, prices[col].values)


@pytest.mark.parametrize("ind", [k for k, m in INDICATORS.items() if not m.get("position")])
def test_every_indicator_runs_in_a_rule(ind, prices):
    """每个因子都能放进规则：左边 = 因子，右边 = 它自己的中位数，应该产生交易"""
    spec = make_ind(ind)
    values = line_values(prices, spec).dropna()
    assert len(values) > 100, ind
    threshold = float(values.median())
    rule = {"buy": {"logic": "all", "conditions": [{"left": spec, "op": ">", "right": {"value": threshold}}]},
            "sell": {"logic": "all", "conditions": [{"left": spec, "op": "<", "right": {"value": threshold}}]},
            "position_pct": 90}
    assert validate(rule) == []
    res = run_backtest({"A": prices}, *resolve({"kind": "rule", "rule": rule}), BrokerConfig())
    assert res.metrics["trades"] > 0, ind


def test_no_indicator_removed():
    """已保存的策略依赖这些键，只能增加不能删除"""
    original = {"close", "open", "high", "low", "volume", "sma", "ema", "rsi", "macd", "boll", "atr", "roc",
                "highest", "lowest", "vol_ma", "pnl_pct"}
    assert original <= set(INDICATORS)


def test_missing_factor_column_never_trades():
    """数据里没有 PE 列时，PE 条件永远不成立（不会误触发买入）"""
    df = make_prices(n=200)
    rule = {"buy": {"logic": "all", "conditions": [{"left": make_ind("pe"), "op": ">", "right": {"value": 0}}]},
            "sell": {"logic": "any", "conditions": []}, "position_pct": 90}
    assert required_columns(rule) == {"pe"}
    res = run_backtest({"A": df}, *resolve({"kind": "rule", "rule": rule}), BrokerConfig())
    assert res.orders.empty


def test_normalize_keeps_factor_columns():
    raw = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "open": ["1", "2"], "high": ["2", "3"],
                        "low": ["1", "1"], "close": ["1.5", "2.5"], "volume": ["10", "20"],
                        "turn": ["0.5", "0.6"], "peTTM": ["30", "31"], "pbMRQ": ["", ""], "psTTM": ["5", "6"]})
    df = normalize(raw)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "turnover", "pe", "ps"]  # pb 全空被丢弃
    assert df["pe"].tolist() == [30.0, 31.0]
