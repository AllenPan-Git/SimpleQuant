"""滚动参数优化：窗口划分、不用未来数据、两种拼接方式"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from simplequant.engine import BrokerConfig
from simplequant.engine.walkforward import make_windows, walk_forward

SPEC = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}
AXES = {"params.fast": [3, 5, 10], "params.slow": [20, 40]}
IS_INT = {"params.fast": True, "params.slow": True}
BROKER = BrokerConfig(cash=100_000)


def prices(n=1300, seed=0):
    return {"A": make_prices(n=n, seed=seed, amp=0.2, period=90)}


@pytest.mark.parametrize("anchored", [False, True])
def test_make_windows(anchored):
    idx = pd.bdate_range("2020-01-01", "2024-12-31")
    ws = make_windows(idx, 24, 6, anchored)
    assert len(ws) == 6
    for a, b in zip(ws, ws[1:]):
        assert idx.get_loc(b.test_start) == idx.get_loc(a.test_end) + 1       # 测试期首尾相接、不重叠
    for w in ws:
        assert w.train_end < w.test_start and idx.get_loc(w.test_start) == idx.get_loc(w.train_end) + 1
        assert (w.train_start == idx[0]) if anchored else (w.test_start - w.train_start).days < 2 * 366 + 5
    assert ws[0].test_start == pd.Timestamp("2022-01-03")


@pytest.mark.parametrize("stitch", ["continuous", "restart"])
def test_no_lookahead(stitch):
    """截掉某个窗口测试期之后的数据，这个窗口及之前的结果必须完全相同"""
    p = prices()
    full = walk_forward(p, SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1, stitch=stitch)
    k = 2
    cut = full.windows.loc[k, "test_end"]
    part = walk_forward({n: df.loc[:cut] for n, df in p.items()}, SPEC, AXES, IS_INT, BROKER,
                        train_months=12, test_months=6, workers=1, stitch=stitch)
    cols = ["test_start", "test_end", "params.fast", "params.slow", "train_metric", "test_return"]
    pd.testing.assert_frame_equal(part.windows[cols].reset_index(drop=True),
                                  full.windows.loc[:k, cols].reset_index(drop=True))
    common = part.equity.index
    np.testing.assert_allclose(part.equity["value"].values, full.equity.loc[common, "value"].values, rtol=1e-9)


def test_single_combo_continuous_matches_static_run():
    """只有一组参数时，"延续持仓"拼出的曲线应与一直用这组参数运行几乎相同"""
    res = walk_forward(prices(), SPEC, {"params.fast": [5], "params.slow": [20]}, IS_INT, BROKER,
                       train_months=12, test_months=6, workers=1)
    # 差别只来自每个窗口重新起算时的整手取整
    assert res.metrics["total_return"] == pytest.approx(res.baseline_metrics["total_return"], rel=0.03)
    assert (res.windows["params.fast"] == 5).all()


def test_restart_mode_chains_capital_and_charges_exit_costs():
    p = prices()
    res = walk_forward(p, SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1, stitch="restart")
    cont = walk_forward(p, SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1)
    assert res.equity["value"].iloc[0] == pytest.approx(100_000)
    # 两种方式窗口划分和选出的参数相同，只是拼接方式不同
    pd.testing.assert_frame_equal(res.windows[["params.fast", "params.slow"]], cont.windows[["params.fast", "params.slow"]])
    assert res.metrics["total_return"] != pytest.approx(cont.metrics["total_return"], abs=1e-6)
    # 每段第一天从空仓开始：资产与上一段结束时相同
    for w in res.windows.itertuples():
        before = res.equity["value"].loc[:w.test_start].iloc[-2]
        assert res.equity["value"].loc[w.test_start] == pytest.approx(before)


def test_outputs_and_limits():
    res = walk_forward(prices(), SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1)
    assert len(res.params) == len(res.windows) >= 5
    assert set(res.equity.columns) == {"value", "baseline", "benchmark", "drawdown"}
    assert res.equity.index[0] == res.windows.loc[0, "train_end"]
    assert np.isfinite(res.metrics["sharpe"])
    with pytest.raises(ValueError):
        walk_forward(prices(), SPEC, {"params.fast": list(range(2, 60)), "params.slow": list(range(61, 160))},
                     IS_INT, BROKER, train_months=12, test_months=1, workers=1)
    with pytest.raises(ValueError):
        walk_forward(prices(n=150), SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1)


def test_charts_serialize_for_browser():
    """图里不能留 pandas.Timestamp：NiceGUI 用 orjson 发给浏览器，遇到 Timestamp 会报错，结果区整块不显示"""
    import orjson
    from ui.charts import walkforward_chart, param_stability_chart
    res = walk_forward(prices(), SPEC, AXES, IS_INT, BROKER, train_months=12, test_months=6, workers=1)
    for fig in (walkforward_chart(res.equity, res.windows), param_stability_chart(res.params, {})):
        orjson.dumps(fig.to_plotly_json(), option=orjson.OPT_SERIALIZE_NUMPY)
