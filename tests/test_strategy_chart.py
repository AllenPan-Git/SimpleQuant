"""交互式策略图：指标提取（规则 / 模板）与图表结构"""

import numpy as np
import pytest

from conftest import make_prices
from test_export import RICH_RULE
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.rules import make_ind
from ui.charts import strategy_chart


def run(spec, prices):
    return run_backtest(prices, *strategies.resolve(spec), BrokerConfig(), with_panels=True)


def test_rule_panels_follow_the_rule():
    res = run({"kind": "rule", "rule": RICH_RULE}, {"A": make_prices(seed=3, n=500)})
    panels = res.panels["A"]
    by_ind = {p["spec"]["ind"]: p for p in panels}
    # MACD 的 DIF/DEA/柱在同一栏；条件 "MACD柱 < -0.5" 的阈值成为参考线
    assert set(by_ind["macd"]["lines"]) == {"dif", "dea", "hist"} and -0.5 in by_ind["macd"]["hlines"]
    assert {30, 70} <= set(by_ind["rsi"]["hlines"])
    # 价格类叠加在 K 线上，其它单独一栏；乖离率、波动率画的是最终值而不是中间步骤
    assert by_ind["boll"]["overlay"] and by_ind["lowest"]["overlay"] and not by_ind["bias"]["overlay"]
    vol = by_ind["volatility"]["lines"]["volatility"].dropna()
    assert 0 < vol.median() < 20            # 百分比口径
    # 不画持仓收益率、原始价格
    assert "pnl_pct" not in by_ind and "close" not in by_ind
    for p in panels:
        for s in p["lines"].values():
            assert len(s) == 500


def test_template_panels_hide_crossover():
    res = run({"kind": "template", "template": "sma_cross", "params": {}}, {"A": make_prices(seed=1), "B": make_prices(seed=2)})
    for name in ("A", "B"):
        names = [p["name"] for p in res.panels[name]]
        assert names == ["SMA (5)", "SMA (20)"]
        assert all(p["overlay"] for p in res.panels[name])


def test_panels_not_collected_by_default():
    res = run_backtest({"A": make_prices()}, *strategies.resolve({"kind": "rule", "rule": RICH_RULE}), BrokerConfig())
    assert res.panels == {}


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_chart_structure(lang):
    prices = {"A": make_prices(seed=3, n=500)}
    res = run({"kind": "rule", "rule": RICH_RULE}, prices)
    fig = strategy_chart(prices["A"], res.orders, res.trades, res.panels["A"], "A", lang)
    subs = [p for p in res.panels["A"] if not p["overlay"]]
    n_rows = 3 + len(subs)
    # hoversubplots 只联动挂在同一条 x 轴上的栏：所有栏共用唯一的 x 轴，各栏 y 轴都锚定它
    axes = [k for k in fig.to_plotly_json()["layout"] if k.startswith("xaxis")]   # 浏览器实际收到的
    assert axes == ["xaxis"] and {t.xaxis for t in fig.data} == {"x"}
    assert {t.yaxis for t in fig.data} <= {"y" if i == 1 else f"y{i}" for i in range(1, n_rows + 1)}
    assert all(fig.layout[f"yaxis{'' if i == 1 else i}"].anchor == "x" for i in range(1, n_rows + 1))
    assert fig.layout.xaxis.anchor == f"y{n_rows}"
    assert all(s.xref in ("x", "x domain", "paper") for s in fig.layout.shapes)
    # K 线默认的缩略图滑块会压住下面各栏：必须关掉
    assert fig.layout.xaxis.rangeslider.visible is False
    assert fig.layout.hovermode == "x unified" and fig.layout.hoversubplots == "axis"
    assert len(fig.layout.shapes) >= sum(len(p["hlines"]) for p in subs) + 1
    names = {t.name for t in fig.data}
    assert ("买入" if lang == "zh" else "Buy") in names
    assert any("MACD(12,26,9)" in (n or "") for n in names)
