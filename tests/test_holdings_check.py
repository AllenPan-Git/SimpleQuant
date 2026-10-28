"""
持仓诊断（待办 44）：识别粘贴的持仓、读取模拟账户；集中度、风格百分位、相关性、假设持有一年
"""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from simplequant.stocks import diagnosis as D
from test_stocks import make_panel

BASICS = pd.DataFrame({"code": ["sh.600519", "sz.000001", "sh.600036", "sh.600100"],
                       "name": ["贵州茅台", "平安银行", "招商银行", "同方股份"]})


def test_parse_broker_table():
    text = """证券代码 证券名称 持仓数量 可用数量 成本价
600519 贵州茅台 200 200 1500.00
000001	平安银行	5,000	5000	10.2
招商银行 3000
600519 贵州茅台 100
002594 比亚迪 300
600036 招商银行 --
合计 9999
"""
    items, bad = D.parse_text(text, BASICS)
    assert {h["code"]: h["shares"] for h in items} == {"sh.600519": 300, "sz.000001": 5000, "sh.600036": 3000}
    assert bad == ["002594 比亚迪 300", "600036 招商银行 --"]          # 不在股票列表里、没有股数；表头和合计跳过


def test_parse_name_with_digits():
    basics = pd.concat([BASICS, pd.DataFrame({"code": ["sh.601360"], "name": ["三六零"]})], ignore_index=True)
    basics.loc[basics["code"] == "sh.600100", "name"] = "同方100"
    items, bad = D.parse_text("600100 同方100 400\n601360 三六零 1200", basics)
    assert {h["code"]: h["shares"] for h in items} == {"sh.600100": 400, "sh.601360": 1200} and not bad


def test_from_account_stocks_only():
    state = {"positions": [{"symbol": "sh.600519", "size": 100}, {"symbol": "510300", "size": 1000},
                           {"symbol": "sz.123001", "size": 0}, {"symbol": "sh.600519", "size": 100}]}
    assert D.from_account(state) == [{"code": "sh.600519", "shares": 200}]
    assert D.from_account({}) == []


def test_holdings_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "HOLDINGS_FILE", tmp_path / "h.json")
    assert D.load_holdings() == []
    D.save_holdings([{"code": "sh.600519", "shares": 200.0}])
    assert D.load_holdings() == [{"code": "sh.600519", "shares": 200.0}]


def test_percentile_matches_overview_rule():
    pool = pd.Series([1.0, 2, 3, 4, np.nan])
    assert D.percentile(pd.Series([3.0, 0.5, np.nan]), pool).tolist()[:2] == [62.5, 0.0]
    assert np.isnan(D.percentile(pd.Series([3.0]), pool.iloc[-1:]).iloc[0])


@pytest.fixture(scope="module")
def panel():
    p = make_panel(n=40, days=400)
    codes = p.codes
    fields = dict(p.fields)
    fields["mcap"] = fields["raw_close"] * pd.Series(np.arange(1, 41) * 1e8, index=codes)
    fields["roe"] = fields["close"] * 0 + pd.Series(np.linspace(-5, 30, 40), index=codes)
    fields["pe"] = fields["pe"].copy()
    fields["pe"][codes[0]] = -10.0                    # 亏损
    ind = pd.Series(["J66银行"] * 20 + ["C15酒"] * 20, index=codes)
    return dataclasses.replace(p, fields=fields, industry=ind, bench_total_return=True)


def test_diagnose(panel):
    codes = panel.codes
    holdings = [{"code": codes[0], "shares": 1000}, {"code": codes[39], "shares": 3000},
                {"code": codes[25], "shares": 2000}, {"code": "sh.688999", "shares": 100}]
    d = D.diagnose(holdings, panel, panel)
    asof = panel.calendar[-1]
    assert d.asof == asof and d.missing == ["sh.688999"] and d.pool_size == 40 and d.bench_total_return
    h = d.holdings.set_index("code")
    value = {c: s * panel["raw_close"].loc[asof, c] for c, s in ((codes[0], 1000), (codes[39], 3000), (codes[25], 2000))}
    total = sum(value.values())
    assert h["value"].to_dict() == pytest.approx(value)
    assert list(d.holdings["value"]) == sorted(d.holdings["value"], reverse=True)

    w = np.array([v / total for v in value.values()])
    c = d.concentration
    assert c["n"] == 3 and c["top1"] == pytest.approx(w.max()) and c["top3"] == pytest.approx(1)
    assert c["effective_n"] == pytest.approx(1 / (w ** 2).sum()) and c["industries"] == 2
    ind = d.industries.set_index("industry")
    assert ind.loc["C15酒", "weight"] == pytest.approx((value[codes[39]] + value[codes[25]]) / total)
    mcap = panel["mcap"].loc[asof]
    assert ind.loc["C15酒", "pool_weight"] == pytest.approx(mcap[codes[20:]].sum() / mcap.sum())

    # 风格：ROE 按值排在第几（第 40 只最大）；亏损股的市盈率百分位最高；没有分红数据时股息率为空
    assert h.loc[codes[39], "roe_pct"] == pytest.approx(39.5 / 40 * 100)
    assert h.loc[codes[0], "roe_pct"] == pytest.approx(0.5 / 40 * 100)
    assert h.loc[codes[0], "pe_pct"] == pytest.approx(100 - 0.5 / 40 * 100)
    assert h["pb_pct"].tolist() == pytest.approx([50.0] * 3)         # 市净率都相同
    assert np.isnan(d.style_pct["dy"]) and d.style_cover["dy"] == 0
    assert d.style_pct["roe"] == pytest.approx(float((h["roe_pct"] * h["weight"]).sum()))
    assert d.style_cover["roe"] == pytest.approx(1)

    # 相关性：过去一年日收益
    win = panel.calendar[-D.LOOKBACK_DAYS - 1:]
    r = panel["close"].loc[win, [codes[0], codes[39]]].pct_change()
    assert d.corr.shape == (3, 3) and np.allclose(np.diag(d.corr), 1)
    assert d.corr.loc[codes[0], codes[39]] == pytest.approx(r.corr().iloc[0, 1])
    assert len(d.top_pairs) == 3 and d.top_pairs[0][2] >= d.top_pairs[-1][2]

    # 假设持有一年：最后一天就是现在的市值；基准从同一金额开始
    assert len(d.curve) == D.LOOKBACK_DAYS + 1 and not d.short_history
    assert d.curve["value"].iloc[-1] == pytest.approx(total)
    assert d.curve["benchmark"].iloc[0] == pytest.approx(d.curve["value"].iloc[0])
    bench = panel.benchmark.loc[win]
    assert d.perf["benchmark"]["total_return"] == pytest.approx(bench.iloc[-1] / bench.iloc[0] - 1)
    assert d.perf["portfolio"]["max_drawdown"] == pytest.approx(d.curve["drawdown"].min())


def test_diagnose_single_and_short_history(panel):
    close = panel["close"].copy()
    close.iloc[:300, 5] = np.nan                       # 第 300 天才上市
    p = dataclasses.replace(panel, fields={**panel.fields, "close": close})
    d = D.diagnose([{"code": p.codes[5], "shares": 100}], p, p)
    assert d.corr is None and np.isnan(d.avg_corr)
    assert d.short_history and d.curve.index[0] == p.calendar[300]
    assert d.concentration["effective_n"] == pytest.approx(1)


def test_diagnose_nothing_local(panel):
    d = D.diagnose([{"code": "sh.688999", "shares": 100}], panel, None)
    assert d.holdings.empty and d.missing == ["sh.688999"] and d.curve is None
