import pandas as pd
import pytest

from conftest import make_prices
from simplequant import i18n, strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.optimize import (optimize, value_range, build_grid, split_prices, best_row,
                                         apply_combo, TARGET_METRICS)
from simplequant.rules import INDICATORS, OPS, describe_rule, make_ind
from simplequant.strategies import TEMPLATES, tunables, set_path

SMA = {"kind": "template", "template": "sma_cross", "params": {}}
RULE = {"kind": "rule", "rule": {
    "buy": {"logic": "all", "conditions": [
        {"left": make_ind("sma", period=5), "op": "cross_above", "right": make_ind("sma", period=20)}]},
    "sell": {"logic": "any", "conditions": [
        {"left": {"ind": "pnl_pct"}, "op": "<", "right": {"value": -5}}]},
    "position_pct": 95}}


# ---------------- 参数优化 ----------------
def test_value_range():
    assert value_range(5, 20, 5, True) == [5, 10, 15, 20]
    assert value_range(1.0, 2.0, 0.5, False) == [1.0, 1.5, 2.0]
    assert value_range(3, 3, 1, True) == [3]


def test_grid_respects_template_constraint():
    grid = build_grid(SMA, {"params.fast": [5, 10, 20], "params.slow": [10, 20]})
    combos = [c for c, _ in grid]
    assert all(c["params.fast"] < c["params.slow"] for c in combos)
    assert len(combos) == 3   # (5,10) (5,20) (10,20)


def test_tunables_and_set_path_for_rule():
    paths = {t.path for t in tunables(RULE)}
    assert "rule.buy.conditions.0.left.params.period" in paths
    assert "rule.sell.conditions.0.right.value" in paths
    assert "rule.position_pct" in paths
    s = set_path(RULE, "rule.buy.conditions.0.right.params.period", 30)
    assert s["rule"]["buy"]["conditions"][0]["right"]["params"]["period"] == 30
    assert RULE["rule"]["buy"]["conditions"][0]["right"]["params"]["period"] == 20   # 原对象不变


@pytest.mark.parametrize("workers", [1, 2])
def test_optimize_matches_single_backtest(workers):
    """网格里每一格的结果必须与单独跑一次回测完全一致（串行和多进程都要成立）"""
    prices = {"A": make_prices()}
    axes = {"params.fast": [3, 5, 8], "params.slow": [20, 30]}
    df = optimize(prices, SMA, axes, BrokerConfig(), workers=workers)
    assert len(df) == 6 and "error" not in df
    row = best_row(df, "sharpe")
    spec = apply_combo(SMA, row, {"params.fast": True, "params.slow": True})
    cls, params = strategies.resolve(spec)
    single = run_backtest(prices, cls, params, BrokerConfig()).metrics
    assert single["sharpe"] == pytest.approx(row["sharpe"])
    assert single["final_value"] == pytest.approx(row["final_value"])


def test_optimize_rule_values():
    df = optimize({"A": make_prices()}, RULE, {"rule.sell.conditions.0.right.value": [-3.0, -8.0],
                                               "rule.buy.conditions.0.left.params.period": [3, 5]}, workers=1)
    assert len(df) == 4 and df["final_value"].notna().all()


def test_optimize_limits():
    with pytest.raises(ValueError):
        optimize({"A": make_prices()}, SMA, {"params.fast": list(range(2, 60)), "params.slow": list(range(5, 60))})


def test_split_prices():
    prices = {"A": make_prices(n=100), "B": make_prices(n=120)}
    ins, oos, cut = split_prices(prices, 0.3)
    for k in prices:
        assert ins[k].index.max() <= cut < oos[k].index.min()
    assert len(ins["A"]) + len(oos["A"]) == 100
    assert abs(len(oos["A"]) / 100 - 0.3) < 0.05


# ---------------- 中英双语 ----------------
def _labels():
    yield from i18n.TEXT.values()
    for m in INDICATORS.values():
        yield m["label"]
        yield from (p[1] for p in m["params"])
        yield from (m.get("lines") or {}).values()
    yield from OPS.values()
    for t in TEMPLATES.values():
        yield t.label
        yield t.description
        for p in t.params:
            yield p.label


def test_every_label_has_both_languages():
    for label in _labels():
        assert label.get("zh") and label.get("en"), label


def test_all_metrics_translated():
    res = run_backtest({"A": make_prices()}, *strategies.resolve(SMA))
    for k in res.metrics:
        assert f"m.{k}" in i18n.TEXT, k
    for k in TARGET_METRICS:
        assert f"m.{k}" in i18n.TEXT


def test_render_logs_in_both_languages():
    res = run_backtest({"A": make_prices()}, *strategies.resolve(SMA))
    for lang in ("zh", "en"):
        texts = [i18n.render(key, kw, lang) for _, key, kw in res.logs]
        assert texts and all("{" not in t and "reason." not in t for t in texts)
    assert any("MA5" in t and "crossed" in t for t in (i18n.render(k, kw, "en") for _, k, kw in res.logs))


def test_describe_in_english():
    text = describe_rule(RULE["rule"], "en")
    assert "SMA(5) crosses above SMA(20)" in text and "Position P&L (%) < -5" in text
    assert "SMA crossover" in strategies.describe(SMA, "en")


def test_yearly_returns_and_ranks():
    """各年收益连乘 = 总收益；最优参数逐年排名按当年收益比较所有组合"""
    from simplequant.engine.optimize import yearly_ranks, year_columns
    prices = {"A": make_prices()}
    df = optimize(prices, SMA, {"params.fast": [3, 5, 8], "params.slow": [20, 30]}, BrokerConfig(), workers=1)
    cols = year_columns(df)
    assert len(cols) >= 2
    growth = (1 + df[cols]).prod(axis=1) - 1
    assert growth.to_numpy() == pytest.approx(df["total_return"].to_numpy())
    best = best_row(df, "sharpe")
    yr = yearly_ranks(df, best)
    assert len(yr) == len(cols) and (yr["n"] == 6).all()
    for r in yr.itertuples():
        vals = df[f"year:{r.year}"]
        assert r.rank == int((vals > r.ret + 1e-12).sum()) + 1 and r.top == pytest.approx(vals.max())


def test_param_curve_hover_has_no_padded_zeros():
    """d3 的 :g 会补成 6 位有效数字（-8 → -8.00000），参数值要用 ~g"""
    from ui.charts import param_curve
    df = pd.DataFrame({"v": [-8.0, -3.0], "sharpe": [0.5, 0.8]})
    fig = param_curve(df, "v", "sharpe", "卖出2 · 数值")
    assert "%{x:~g}" in fig.data[0].hovertemplate
