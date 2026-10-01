"""
选股策略的滚动优化
每组参数只回测一次、各窗口从资产曲线上截取：拼出的样本外曲线必须与"所选参数单独回测"的收益逐日一致
"""

import pandas as pd
import pytest

from test_stocks import make_panel, SPEC
from simplequant.engine import BrokerConfig
from simplequant.stocks import build_schedule, run_selection
from simplequant.stocks.walkforward import walk_forward_selection, selection_tunables, selection_grid
from simplequant.strategies import set_path

BROKER = BrokerConfig(cash=1_000_000, commission=0.00025, stamp_duty=0.0005)
SPEC2 = {**SPEC, "factors": [{"key": "ep", "weight": 1, "direction": 1}, {"key": "ret20", "weight": 1, "direction": -1}]}


@pytest.fixture(scope="module")
def panel():
    return make_panel(n=40, days=700, seed=1)


@pytest.mark.parametrize("spec", [
    SPEC2,
    {**SPEC2, "weighting": "icir", "ic_lookback": 120, "rebalance": "weekly"},
    {**SPEC2, "weighting": "ic", "rebalance": 10},
])
def test_schedule_cache_gives_same_picks(panel, spec):
    cache = {}
    build_schedule(panel, {**spec, "top_n": 3}, cache=cache)          # 先用别的参数把缓存填上
    assert build_schedule(panel, spec, cache=cache).picks == build_schedule(panel, spec).picks


def test_tunables_and_grid():
    paths = [tb.path for tb in selection_tunables(SPEC2)]
    assert paths == ["top_n", "factors.0.weight", "factors.1.weight", "position_pct"]
    assert [tb.path for tb in selection_tunables({**SPEC2, "weighting": "ic"})] == ["top_n", "ic_lookback", "position_pct"]
    assert [tb.path for tb in selection_tunables(SPEC)] == ["top_n", "position_pct"]     # 单因子时权重无意义
    grid = selection_grid(SPEC2, {"factors.0.weight": [0, 1], "factors.1.weight": [0, 1]})
    assert len(grid) == 3                                     # 两个权重都为 0 的组合被剔除


@pytest.mark.parametrize("workers", [1, 2])
def test_walk_forward_stitches_chosen_runs(panel, workers):
    axes = {"top_n": [3, 8], "rebalance": ["monthly", 10]}
    start = panel.calendar[120]
    res = walk_forward_selection(panel, SPEC2, axes, BROKER, "sharpe", train_months=6, test_months=3,
                                 start=start, workers=workers)
    assert len(res.windows) >= 4 and len(res.grid) == 4
    assert res.equity["value"].iloc[0] == pytest.approx(BROKER.cash)
    assert set(res.params.columns) == {"test_start", "top_n", "rebalance"}

    oos = res.equity["value"].pct_change().dropna()
    for _, w in res.windows.iterrows():
        spec = set_path(set_path(SPEC2, "top_n", int(w["top_n"])), "rebalance", w["rebalance"])
        eq = run_selection(panel, spec, BROKER, start=start).equity["value"]
        own = eq.loc[w["train_end"]:w["test_end"]].pct_change().dropna()
        pd.testing.assert_series_equal(oos.loc[own.index], own, check_names=False)

    # 训练期里被选中的组合，指标确实最高
    w = res.windows.iloc[-1]
    best = None
    for top_n in axes["top_n"]:
        for reb in axes["rebalance"]:
            spec = set_path(set_path(SPEC2, "top_n", top_n), "rebalance", reb)
            eq = run_selection(panel, spec, BROKER, start=start).equity["value"]
            i0 = eq.index.searchsorted(w["train_start"])
            v = eq.iloc[max(i0 - 1, 0):eq.index.searchsorted(w["train_end"], side="right")]
            r = v.pct_change().dropna()
            sharpe = (r - 0.02 / 252).mean() / r.std() * 252 ** 0.5
            if best is None or sharpe > best[0]:
                best = (sharpe, top_n, reb)
    assert (w["top_n"], w["rebalance"]) == best[1:]
    assert w["train_metric"] == pytest.approx(best[0])


def test_single_combo_equals_baseline(panel):
    """只有原参数一组时，滚动优化的样本外曲线就是原参数的曲线"""
    res = walk_forward_selection(panel, SPEC2, {"top_n": [SPEC2["top_n"]]}, BROKER, "sharpe", 6, 3,
                                 start=panel.calendar[120], workers=1)
    pd.testing.assert_series_equal(res.equity["value"], res.equity["baseline"], check_names=False)
    assert res.metrics["total_return"] == pytest.approx(res.baseline_metrics["total_return"])


def test_walk_forward_rejects_bad_setups(panel):
    with pytest.raises(ValueError):
        walk_forward_selection(panel, SPEC2, {"top_n": [5]}, BROKER, train_months=60, test_months=6, workers=1)
    with pytest.raises(ValueError):
        walk_forward_selection(panel, SPEC2, {"top_n": list(range(1, 300))}, BROKER, workers=1)
