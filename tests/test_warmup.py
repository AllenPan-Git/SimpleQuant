"""区间开始前的数据只预热指标（trade_start）：单次回测、批量、参数优化、样本内外切分、滚动优化（待办 30）"""

import pandas as pd

from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.batch import run_batch
from simplequant.engine.optimize import optimize, split_prices, oos_start
from simplequant.engine.walkforward import walk_forward
from simplequant.rules import make_ind
from test_valuation import _val_prices, _pct_reference

BROKER = BrokerConfig()
PATH = "rule.buy.conditions.0.right.value"


def _spec(threshold=40):
    return {"kind": "rule", "rule": {
        "buy": {"logic": "all", "conditions": [{"left": make_ind("pe_pct", years=1), "op": "<",
                                                "right": {"value": threshold}}]},
        "sell": {"logic": "any", "conditions": [{"left": make_ind("pe_pct", years=1), "op": ">",
                                                 "right": {"value": 80}}]},
        "position_pct": 90}}


def _data():
    df = _val_prices(n=900)
    return df, df.index[400]


def test_warm_run_trades_from_start_with_full_lookback():
    df, ts = _data()
    warm = run_backtest({"A": df}, *strategies.resolve(_spec()), BROKER, trade_start=ts, with_panels=True)
    cold = run_backtest({"A": df.loc[ts:]}, *strategies.resolve(_spec()), BROKER)
    assert warm.equity.index[0] == ts and warm.trade_start == ts and warm.warm_prices["A"] is df
    assert warm.prices["A"].index[0] == ts
    assert all(s.index[0] >= ts for p in warm.panels["A"] for s in p["lines"].values())
    # 第一笔买入：区间开始后百分位（按全部历史算）第一次低于门槛的下一根 K 线
    ref = _pct_reference(df["pe"], 244, 244).loc[ts:]
    signal = ref[ref < 40].index[0]
    first = pd.Timestamp(warm.orders["time"].iloc[0]).normalize()
    assert first == df.index[df.index.get_loc(signal) + 1]
    # 只用区间内的数据：头 243 根 K 线没有百分位，买入只会更晚
    assert pd.Timestamp(cold.orders["time"].iloc[0]).normalize() >= df.index[df.index.get_loc(ts) + 243]
    assert first < df.index[df.index.get_loc(ts) + 243]


def test_batch_and_optimize_pass_trade_start():
    df, ts = _data()
    warm = run_backtest({"A": df}, *strategies.resolve(_spec()), BROKER, trade_start=ts)
    row = run_batch({"A": df}, _spec(), BROKER, workers=1, trade_start=ts).iloc[0]
    assert row["start"] == ts and row["total_return"] == warm.metrics["total_return"]
    grid = optimize({"A": df}, _spec(), {PATH: [30.0, 40.0]}, BROKER, workers=1, trade_start=ts)
    assert grid.loc[grid[PATH] == 40.0, "total_return"].iloc[0] == warm.metrics["total_return"]


def test_split_keeps_warm_data_and_cuts_after_start():
    df, ts = _data()
    ins, oos, cut = split_prices({"A": df}, 0.3, ts)
    assert ins["A"].index[0] == df.index[0] and ins["A"].index[-1] <= cut
    assert oos["A"].index[0] == df.index[0] and oos["A"].index[-1] == df.index[-1]
    assert ts < cut < df.index[-1]
    _, cold_oos, cold_cut = split_prices({"A": df.loc[ts:]}, 0.3)
    assert cut == cold_cut and oos_start(oos, cut) == cold_oos["A"].index[0]


def test_walk_forward_windows_start_after_trade_start():
    df, ts = _data()
    res = walk_forward({"A": df}, _spec(), {PATH: [30.0, 40.0]}, {PATH: False}, BROKER,
                       train_months=12, test_months=6, workers=1, trade_start=ts)
    assert res.windows["train_start"].iloc[0] == ts
    assert res.equity.index[0] >= ts
