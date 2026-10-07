"""批量回测（engine/batch.py）"""

import numpy as np
import pandas as pd

from simplequant import strategies
from simplequant.engine import BrokerConfig, run_backtest
from simplequant.engine.batch import run_batch, summarize

SPEC = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}
BROKER = BrokerConfig(commission=0.0001, min_commission=0, slippage=0)


def walk(seed, n=300, drift=0.0):
    rng = np.random.default_rng(seed)
    close = 10 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    idx = pd.bdate_range("2021-01-04", periods=n)
    return pd.DataFrame({"open": close * (1 + rng.normal(0, 0.003, n)), "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1e5}, index=idx)


def test_each_asset_matches_single_backtest():
    prices = {"A": walk(1), "B": walk(2, drift=0.002), "C": walk(3, n=200), "short": walk(4, n=10)}
    df = run_batch(prices, SPEC, BROKER, workers=1)
    assert list(df["symbol"]) == list(prices)
    assert df.loc[df["symbol"] == "short", "error"].iloc[0]
    for name in ("A", "B", "C"):
        one = run_backtest({name: prices[name]}, *strategies.resolve(SPEC), BROKER).metrics
        row = df[df["symbol"] == name].iloc[0]
        assert np.isclose(row["total_return"], one["total_return"])
        assert np.isclose(row["benchmark_return"], one["benchmark_return"])
        assert row["start"] == prices[name].index[0]          # 每个标的用自己的区间

    s = summarize(df)
    assert s["n"] == 4 and s["failed"] == 1 and s["ok"] == 3
    ok = df[df["error"].isna()]
    assert s["beat"] == int((ok["total_return"] > ok["benchmark_return"]).sum())
    assert np.isclose(s["median_return"], ok["total_return"].median())


def test_parallel_same_as_sequential():
    prices = {f"S{i}": walk(10 + i) for i in range(5)}
    a = run_batch(prices, SPEC, BROKER, workers=1)
    b = run_batch(prices, SPEC, BROKER, workers=2)
    pd.testing.assert_series_equal(a["total_return"], b["total_return"])


def test_summary_all_failed():
    df = run_batch({"x": walk(1, n=5)}, SPEC, BROKER, workers=1)
    assert summarize(df) == {"n": 1, "failed": 1}
