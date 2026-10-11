"""
机器学习合成因子（stocks/ml.py）：四个 numpy 模型的基本性质、防前视、选股流程接入
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.engine import BrokerConfig
from simplequant.stocks import build_schedule, run_selection, Panel
from simplequant.stocks import ml
from simplequant.stocks.selection import eligible_mask, effective_filters

from test_stocks import make_panel


def test_ridge_matches_least_squares():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(500, 3))
    y = X @ [0.5, -0.2, 0.0] + 0.1 + rng.normal(0, 0.1, 500)
    m = ml.Ridge(alpha=0).fit(X, y)
    ref, *_ = np.linalg.lstsq(np.column_stack([X, np.ones(500)]), y, rcond=None)
    assert m.coef == pytest.approx(ref[:3], abs=1e-6)
    assert m.predict(X[:5]) == pytest.approx(np.column_stack([X[:5], np.ones(5)]) @ ref, abs=1e-6)
    shrunk = ml.Ridge(alpha=1).fit(X, y)                     # 惩罚 = 样本数：系数约收缩一半
    assert np.abs(shrunk.coef).sum() == pytest.approx(np.abs(m.coef).sum() / 2, rel=0.05)
    assert shrunk.importance().argmax() == 0


def test_knn_averages_nearest():
    X = np.array([[0.0], [1.0], [2.0], [10.0]])
    y = np.array([1.0, 2.0, 3.0, 100.0])
    assert ml.KNN(k=1).fit(X, y).predict(np.array([[0.9], [9.0]])).tolist() == [2.0, 100.0]
    assert ml.KNN(k=3).fit(X, y).predict(np.array([[0.0]]))[0] == pytest.approx(2.0)
    assert ml.KNN(k=10).fit(X, y).predict(np.array([[0.0]]))[0] == pytest.approx(y.mean())   # K 超过样本数


def test_tree_learns_step_and_interaction():
    """线性模型学不到的：阶梯函数、两个因子的交互（同号才好）"""
    rng = np.random.default_rng(1)
    X = rng.normal(size=(4000, 3))
    y = np.where(X[:, 0] > 0.5, 0.3, -0.1) + np.where(X[:, 1] * X[:, 2] > 0, 0.2, -0.2) + rng.normal(0, 0.05, 4000)
    Xt = rng.normal(size=(2000, 3))
    yt = np.where(Xt[:, 0] > 0.5, 0.3, -0.1) + np.where(Xt[:, 1] * Xt[:, 2] > 0, 0.2, -0.2)
    corr = lambda m: np.corrcoef(m.predict(Xt), yt)[0, 1]
    rf = ml.RandomForest(trees=30, depth=6).fit(X, y)
    gb = ml.GradientBoosting(trees=150, depth=3, lr=0.1).fit(X, y)
    assert corr(rf) > 0.95 and corr(gb) > 0.95
    assert corr(ml.Ridge(alpha=0).fit(X, y)) < 0.7
    for m in (rf, gb):
        imp = m.importance()
        assert imp.sum() == pytest.approx(1) and imp.min() > 0.05      # 三个因子都有用
    again = ml.RandomForest(trees=30, depth=6).fit(X, y)                  # 固定种子：结果完全相同
    assert np.array_equal(again.predict(Xt), rf.predict(Xt))


def test_tree_respects_min_leaf_and_depth():
    X = np.arange(100, dtype=float).reshape(-1, 1)
    y = (X[:, 0] > 95).astype(float)                    # 只有 4 个样本不同：叶子至少 20 个，切不出来
    m = ml.GradientBoosting(trees=1, depth=6, lr=1.0).fit(X, y)
    assert len(m.trees[0]["feat"]) <= 2 ** 7
    t, _ = ml.grow_tree(ml.Binner(X).transform(X), y, np.ones(100), 33, 2, 20)
    leaves = t["feat"] < 0
    assert leaves.sum() <= 4


def spec(model="ridge", **kw):
    return {"kind": "selection", "universe": "hs300", "top_n": 5, "rebalance": "monthly",
            "factors": [{"key": "ep", "weight": 1, "direction": 1}, {"key": "ret20", "weight": 1, "direction": -1}],
            "weighting": "ml", "ml": {"model": model, "window": 252, **kw}}


@pytest.fixture(scope="module")
def panel():
    return make_panel(n=60, days=500)


@pytest.mark.parametrize("model", ["ridge", "knn", "rf", "gbdt"])
def test_ml_selection_runs(panel, model):
    """每种模型都能走完选股回测；合成市场里 EP 有效，训练出的模型样本外 IC 为正"""
    s = spec(model, **({"trees": 20} if model in ("rf", "gbdt") else {}))
    sched = build_schedule(panel, s, start=panel.calendar[300])
    info = sched.ml
    assert info["model"] == model and sched.picks
    assert info["trainings"] and not info["fallback"]
    assert info["summary"]["oos"]["ml"] > 0
    if model == "knn":
        assert info["importance"] is None
    else:
        assert info["importance"]["ep"] > info["importance"]["ret20"]
    res = run_selection(panel, s, BrokerConfig(), start=panel.calendar[300])
    assert res.metrics["total_return"] == res.metrics["total_return"]


def test_ml_falls_back_when_history_short(panel):
    """训练样本不足的调仓日按手动权重打分"""
    sched = build_schedule(panel, spec(), start=panel.calendar[40])
    info = sched.ml
    assert info["fallback"] and info["fallback"][0] == min(sched.picks)
    assert info["trainings"][0]["date"] > info["fallback"][-1]


def test_ml_no_lookahead(panel):
    """调仓日 T 的得分不受 T 之后价格的影响（训练样本的目标最晚用到 T 日开盘价）"""
    T = panel.calendar[350]
    s = spec("gbdt", trees=10)
    mask = eligible_mask(panel, effective_filters(panel, s))
    a, _ = ml.ml_scores(panel, s, mask, pd.DatetimeIndex([T]), 20)
    rng = np.random.default_rng(5)
    fields = {k: v.copy() for k, v in panel.fields.items()}
    after = fields["close"].index > T
    for k in ("open", "close", "high", "low", "raw_open", "raw_close"):
        fields[k].loc[after] *= rng.uniform(0.5, 1.5, (after.sum(), fields[k].shape[1]))
    other = Panel(fields=fields, member=panel.member, tradable=panel.tradable, is_st=panel.is_st,
                  listed_days=panel.listed_days, can_buy=panel.can_buy, can_sell=panel.can_sell,
                  benchmark=panel.benchmark, names=panel.names)
    b, _ = ml.ml_scores(other, s, mask, pd.DatetimeIndex([T]), 20)
    pd.testing.assert_frame_equal(a, b)
    # 反过来：改动 T 当天的开盘价会改变训练目标（T−21 日样本的目标用到 T 日开盘价）
    fields["open"].loc[T] *= rng.uniform(0.5, 1.5, fields["open"].shape[1])
    b2, _ = ml.ml_scores(other, s, mask, pd.DatetimeIndex([T]), 20)
    assert not a.equals(b2)


def test_ml_config_defaults():
    assert ml.config({}) == {"model": "rf", "window": 756, "trees": 100, "depth": 5}
    assert ml.config({"ml": {"model": "knn", "k": "30"}}) == {"model": "knn", "window": 756, "k": 30}


# ================= 接入：可信度检查、方案文件、描述、滚动优化、导出 =================
def test_check_ml():
    from simplequant.engine.credibility import check_ml
    keys = lambda s: [(f["key"].split(".")[1], f["level"]) for f in check_ml(s)]
    base = {"train_ic": 0.10, "n_oos": 40, "n_fallback": 0, "n_train": 30}
    assert keys({**base, "oos": {"ml": 0.08, "icir": 0.05}}) == [("ml_oos_ok", "ok")]
    assert keys({**base, "oos": {"ml": 0.03, "icir": 0.05}}) == [("ml_overfit", "warn"), ("ml_below_icir", "note")]
    assert keys({**base, "oos": {"ml": -0.01, "icir": 0.05}, "n_oos": 5, "n_fallback": 3}) == [
        ("ml_fallback", "note"), ("ml_few", "note"), ("ml_oos_neg", "warn"), ("ml_below_icir", "note")]
    assert keys({**base, "oos": {"ml": float("nan")}}) == [("ml_no_oos", "note")]
    f = check_ml({**base, "oos": {"ml": 0.08}})[0]
    assert f["args"]["oos"] == "0.080"                    # IC 以字符串传给界面，不显示成百分数


def test_ml_plan_round_trip():
    from simplequant import plan as P
    s = spec("knn", k=50)
    plan = P.make_selection_plan("x", s, "2021-01-04", "2022-01-04", "2018-01-02", BrokerConfig())
    assert plan["version"] == P.ML_VERSION == 3            # 旧版软件读到会提示更新，而不是悄悄按手动权重算
    got = P.loads(P.dumps(plan))
    assert got["strategy"]["ml"] == {"model": "knn", "window": 252, "k": 50}
    plain = P.make_selection_plan("x", {**s, "weighting": "icir"}, "2021-01-04", "2022-01-04", "2018-01-02",
                                  BrokerConfig())
    assert plain["version"] == P.SELECTION_VERSION
    for bad in ({"model": "lstm"}, None, {"model": "rf", "trees": "many"}):
        p = dict(plan, strategy={**s, "ml": bad})
        with pytest.raises(P.PlanError):
            P.loads(P.dumps(p))


def test_ml_describe_tunables_warmup_export():
    from simplequant import strategies
    from simplequant.export import check_selection_exportable
    from simplequant.stocks.selection import panel_warmup_days, WARMUP_DAYS
    from simplequant.stocks.walkforward import selection_tunables
    s = spec("gbdt")
    text = strategies.describe(s, "zh")
    assert "梯度提升树" in text and "训练窗口（交易日） 252" in text and "学习率 0.05" in text
    assert "Gradient boosting" in strategies.describe(s, "en")
    names = [x.path for x in selection_tunables(s)]
    assert {"ml.window", "ml.trees", "ml.depth", "ml.lr"} <= set(names) and "ic_lookback" not in names
    assert panel_warmup_days({**s, "weighting": "icir"}) == WARMUP_DAYS
    assert panel_warmup_days(s) == WARMUP_DAYS + int((252 + 21) * 365 / 240)
    with pytest.raises(ValueError, match="按名单调仓"):
        check_selection_exportable(s, "joinquant")
    check_selection_exportable(s, "joinquant", follow=True)       # 按名单调仓可以
