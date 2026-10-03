"""风险测评与资产配置"""

import numpy as np
import pandas as pd
import pytest

from simplequant import allocation as A
from simplequant.allocation import allocate, portfolio, questionnaire as Q, sleeves as S
from simplequant.data import library
from conftest import make_prices


def answers(choice):
    """每题选同一个位置的选项（超出则取最后一项）"""
    return {q["key"]: min(choice, len(q["options"]) - 1) for q in Q.QUESTIONS}


# ---------------- 风险测评 ----------------
def test_questionnaire_extremes_and_caps():
    low, high = A.evaluate(answers(0)), A.evaluate(answers(9))
    assert low.level == 1 and low.max_dd == 0.03 and low.years == 1
    assert Q.MIN_SCORE <= low.score < high.score <= Q.MAX_SCORE
    # 年龄一题的最后一项（65 岁以上）得分最低，但总分仍应在 C4 以上
    assert high.max_dd == 0.50 and high.years == 10 and high.level >= 4

    a = answers(3)
    a["loss"] = 0                        # 不能接受任何亏损：无论总分多少只能是 C1
    p = A.evaluate(a)
    assert p.level == 1 and p.score_level > 1 and p.max_dd == 0.03
    a = answers(3)
    a["horizon"] = 0                     # 投资期限 1 年以内：最高 C2
    assert A.evaluate(a).level <= 2


def test_questionnaire_levels_monotonic_and_complete():
    levels = [A.evaluate(answers(i)).level for i in range(5)]
    assert levels == sorted(levels) and levels[0] == 1 and levels[-1] >= 4
    with pytest.raises(ValueError):
        A.evaluate({"age": 0})


def test_every_question_text_bilingual():
    for q in Q.QUESTIONS:
        assert q["text"]["zh"] and q["text"]["en"]
        assert all(o["text"]["zh"] and o["text"]["en"] for o in q["options"])
    assert all(v["zh"] and v["en"] for v in list(A.LEVELS.values()) + list(A.LEVEL_DESC.values()))


def test_profile_roundtrip(tmp_path):
    p = A.evaluate(answers(2))
    A.save_profile(p, tmp_path)
    assert A.load_profile(tmp_path) == p
    assert A.load_profile(tmp_path / "none") is None


# ---------------- 候选与风险等级 ----------------
def test_risk_grade():
    assert S.risk_grade(0.0, 0.0) == 1
    assert S.risk_grade(0.02, -0.049) == 2          # 国债 ETF
    assert S.risk_grade(0.16, -0.305) == 4          # 黄金
    assert S.risk_grade(0.17, -0.385) == 5          # 沪深300
    assert S.risk_grade(0.30, -0.01) == 5           # 波动率与回撤取较高的一级


def test_cash_sleeve_and_stats():
    cal = pd.bdate_range("2020-01-01", periods=253)
    r = S.sleeve_returns({"id": "cash", "kind": "cash", "class": "cash", "rate": 0.02}, cal)
    assert len(r) == 252 and np.isclose((1 + r).prod(), 1.02)
    st = S.risk_stats(r)
    assert st["risk"] == 1 and st["max_drawdown"] == 0 and np.isclose(st["cagr"], 0.02)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    d = tmp_path / "lib"
    for fn in (library.save, library.load, library.exists, library.list_datasets, library.delete):
        monkeypatch.setattr(fn, "__defaults__", (d,))
    return d


def save(sym, **kw):
    return library.save(make_prices(**kw), library.DatasetMeta(id=f"x_{sym}", name=sym, symbol=sym,
                                                                source="akshare", freq="1d", adjust="hfq"))


def test_hold_and_timing_sleeves(lib):
    save("510300", n=300)
    hold = S.sleeve_returns({"id": "h", "kind": "hold", "class": "equity", "symbol": "510300"})
    df = library.load("x_510300")
    assert np.allclose(hold.to_numpy(), df["close"].pct_change().dropna().to_numpy())
    timing = S.sleeve_returns({"id": "t", "kind": "timing", "class": "equity", "symbol": "510300",
                               "strategy": "tpl:sma_cross"})
    assert len(timing) == len(hold) and not np.allclose(timing.to_numpy(), hold.to_numpy())
    assert S.missing_data([{"kind": "hold", "symbol": "510300"}, {"kind": "hold", "symbol": "511010"},
                           {"kind": "cash"}]) == ["511010"]


def test_load_returns_common_period_and_errors(lib):
    save("A", n=300, start="2021-01-04")
    save("B", n=300, start="2021-03-01", seed=1)
    sleeves = [{"id": "cash", "kind": "cash", "class": "cash", "rate": 0.015},
               {"id": "a", "kind": "hold", "class": "equity", "symbol": "A"},
               {"id": "b", "kind": "hold", "class": "bond", "symbol": "B"},
               {"id": "c", "kind": "hold", "class": "alt", "symbol": "NONE"}]
    R, errors = A.load_returns(sleeves)
    assert list(R.columns) == ["a", "b", "cash"] and set(errors) == {"c"}
    assert R.index[0] > pd.Timestamp("2021-03-01") and R.index[-1] == library.load("x_A").index[-1]
    assert not R.isna().any().any()


# ---------------- 组合回测 ----------------
def returns_frame(n=500, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    return pd.DataFrame({"bond": rng.normal(0.0002, 0.002, n), "stock": rng.normal(0.0004, 0.015, n),
                         "cash": np.full(n, 0.00006)}, index=idx)


def test_backtest_without_rebalance_is_buy_and_hold():
    R = returns_frame()
    res = A.backtest(R, {"bond": 0.5, "stock": 0.5}, "none", cost=0.001)
    expect = 0.5 * (1 + R["bond"]).prod() + 0.5 * (1 + R["stock"]).prod()
    assert np.isclose(res.equity["value"].iloc[-1] / 100_000, expect)
    assert res.n_rebalances == 0 and res.cost == 0
    # 收益贡献之和 = 总收益；风险贡献之和 = 1
    assert np.isclose(res.contrib["return_contrib"].sum(), expect - 1)
    assert np.isclose(res.contrib["risk_contrib"].sum(), 1)


def test_backtest_rebalance_rules():
    R = returns_frame()
    months = A.backtest(R, {"bond": 0.6, "stock": 0.4}, "monthly")
    quarters = A.backtest(R, {"bond": 0.6, "stock": 0.4}, "quarterly")
    years = A.backtest(R, {"bond": 0.6, "stock": 0.4}, "yearly")
    n_months = len(pd.PeriodIndex(R.index, freq="M").unique())
    assert months.n_rebalances == n_months - 1
    assert quarters.n_rebalances < months.n_rebalances and years.n_rebalances == 1
    assert months.cost > 0
    # 再平衡日收盘后权重回到目标
    w = months.weights
    month_end = w.index[:-1][w.index.month[1:] != w.index.month[:-1]]
    assert np.allclose(w.loc[month_end, "bond"], 0.6)
    thr = A.backtest(R, {"bond": 0.6, "stock": 0.4}, "threshold")
    assert (thr.weights["bond"] - 0.6).abs().max() <= portfolio.THRESHOLD + 0.05
    with pytest.raises(ValueError):
        A.backtest(R, {"bond": 0}, "monthly")


def test_class_weights_redistribute_missing():
    cw = allocate.class_weights(1, {"bond", "equity"})          # 保守型没有现金类：债券类承担全部
    assert cw == {"bond": 1.0, "equity": 0.0}
    cw = allocate.class_weights(3, {"bond", "equity", "alt", "cash"})
    assert np.isclose(sum(cw.values()), 1) and cw == allocate.CLASS_WEIGHTS[3]
    assert allocate.class_weights(1, {"equity"}) == {"equity": 1.0}


def test_inner_weights_inverse_vol():
    R = returns_frame()
    R["stock2"] = R["stock"] * 2
    sl = [{"id": "stock", "class": "equity"}, {"id": "stock2", "class": "equity"}, {"id": "bond", "class": "bond"}]
    w = allocate.inner_weights(R, sl)
    assert np.isclose(w["stock"], 2 / 3) and np.isclose(w["stock2"], 1 / 3) and w["bond"] == 1


SLEEVES = [{"id": "cash", "class": "cash"}, {"id": "bond", "class": "bond"}, {"id": "stock", "class": "equity"}]


def crash_frame():
    """股票先跌 40% 再涨回：用来触发回撤约束"""
    R = returns_frame(n=750)
    R.iloc[100:200, R.columns.get_loc("stock")] = -0.005
    return R


def test_suggest_shifts_to_meet_drawdown():
    R = crash_frame()
    loose = A.suggest(5, 0.90, SLEEVES, R)
    assert loose["shifted"] == 0 and loose["within_limit"]
    tight = A.suggest(5, 0.10, SLEEVES, R)
    assert tight["shifted"] > 0 and tight["within_limit"]
    assert abs(tight["result"].metrics["max_drawdown"]) <= 0.10
    assert tight["weights"]["stock"] < loose["weights"]["stock"]
    assert np.isclose(sum(tight["weights"].values()), 1)


def test_suggest_reports_when_limit_unreachable():
    R = crash_frame()
    res = A.suggest(5, 0.10, [{"id": "stock", "class": "equity"}], R)       # 只有股票，无处可移
    assert res["shifted"] == 0 and not res["within_limit"]
    keys = {c["key"] for c in A.checks(5, 0.10, res["result"])}
    assert "alloc.dd_over" in keys and "alloc.hindsight" in keys


def test_checks():
    R = returns_frame(n=300)                          # 约 1.2 年
    res = A.backtest(R, {"bond": 0.2, "stock": 0.8}, "quarterly")
    out = {c["key"]: c for c in A.checks(1, 0.50, res, shifted=0.1, names={"stock": "股票"})}
    assert out["alloc.short_period"]["level"] == "warn"
    assert out["alloc.shifted"]["args"]["w"] == 0.1
    assert out["alloc.risk_over"]["args"]["c"] == "C1"
    assert out["alloc.risk_concentrated"]["args"]["name"] == "股票"
    assert all(isinstance(v, (str, float, int)) for c in out.values() for v in c["args"].values())


def test_config_roundtrip(tmp_path):
    cfg = A.load_config(tmp_path)
    assert cfg["enabled"] == [s["id"] for s in A.DEFAULT_SLEEVES] and cfg["rebalance"] == "quarterly"
    cfg["custom"].append({"id": "custom_x", "name": "x", "class": "equity", "kind": "hold", "symbol": "510300"})
    A.save_config(cfg, tmp_path)
    assert A.all_sleeves(A.load_config(tmp_path))[-1]["id"] == "custom_x"
