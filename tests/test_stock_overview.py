"""
个股概览（待办 42）：只整理一只股票自己的数据；选股结果里「为什么入选」的因子值与选股时一致
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.stocks import StockStore, run_selection
from simplequant.stocks import overview as O
from simplequant.stocks.selection import pick_details
from test_fundamentals import fin_rows
from test_selection_dca import BASE, BROKER, make_panel

BASICS = pd.DataFrame({"code": ["sh.600519", "sh.600518", "sz.000519", "sh.601519"],
                       "name": ["贵州茅台", "康美药业", "中兵红箭", "大智慧"],
                       "ipo_date": pd.to_datetime(["2001-08-27", "2001-03-19", "1993-10-08", "2011-01-28"]),
                       "out_date": pd.to_datetime([None, None, None, None])})


def test_find_stocks_by_code_and_name():
    assert list(O.find_stocks(BASICS, "600519")["code"]) == ["sh.600519"]
    assert list(O.find_stocks(BASICS, "sh.600519")["code"]) == ["sh.600519"]
    assert list(O.find_stocks(BASICS, "60051")["code"]) == ["sh.600518", "sh.600519"]
    assert list(O.find_stocks(BASICS, "茅台")["code"]) == ["sh.600519"]
    assert O.find_stocks(BASICS, "不存在").empty and O.find_stocks(BASICS, " ").empty


def test_percentile_positive_values_only():
    idx = pd.bdate_range("2020-01-01", periods=300)
    s = pd.Series(np.arange(1, 301, dtype=float), index=idx)
    assert O.percentile(s)["pct"] == pytest.approx((299 + 0.5) / 300 * 100)
    s2 = s.copy()
    s2.iloc[:50] = -5                                     # 亏损的日子不计入
    info = O.percentile(s2)
    assert info["days"] == 250 and info["since"] == idx[50]
    assert info["pct"] == pytest.approx((249 + 0.5) / 250 * 100)
    s2.iloc[-1] = -1                                      # 当前亏损：没有百分位
    assert np.isnan(O.percentile(s2)["pct"])
    assert np.isnan(O.percentile(s.iloc[:100])["pct"])    # 不到一年


def test_financials_annual_plus_latest_interim():
    fin = fin_rows([
        ["2022-12-31", "2023-03-30", "0.20", "0.5", "0.2", "100", "1000", "1", "10"],
        ["2023-06-30", "2023-08-25", "0.11", "0.5", "0.2", "60", "600", "1", "10"],
        ["2023-12-31", "2024-03-29", "0.22", "0.5", "0.2", "120", "1300", "1", "12"],
        ["2024-06-30", "2024-08-20", "0.12", "0.4", "0.2", "66", "700", "1", "12"],
    ])
    f = O.financials(fin)
    assert list(f["stat_date"].dt.strftime("%Y-%m")) == ["2022-12", "2023-12", "2024-06"]
    assert list(f["annual"]) == [True, True, False]
    assert f["roe"].tolist() == pytest.approx([20, 22, 24])          # 中报 ROE 按季度年化
    assert f["np_yoy"].iloc[1] == pytest.approx(20) and f["np_yoy"].iloc[2] == pytest.approx(10)
    assert O.financials(fin.iloc[:3])["stat_date"].dt.month.tolist() == [12, 12]   # 最后一期是年报时不重复


def daily_frame(n=30, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series(10.0, index=idx)
    return pd.DataFrame({"raw_close": close, "raw_preclose": close, "adj_factor": 1.0, "is_st": 0,
                         "pe": 15.0, "pb": 1.5}, index=idx)


def test_dividend_years_yield_and_patched():
    daily = daily_frame()
    d = daily.index
    daily.loc[d[5]:, "raw_close"] = 20.0                 # 第二次分红前股价翻倍
    daily.loc[d[20], "raw_preclose"] = 19.0               # 分红表漏记的 1 元：复权因子变了
    daily.loc[d[20]:, "adj_factor"] = 20 / 19
    div = pd.DataFrame({"ex_date": [pd.Timestamp("2023-06-01"), d[3], d[10]], "cash": [0.5, 0.5, 1.0],
                        "bonus": 0.0, "reserve": [0.0, 0.0, 0.3]})
    years, patched = O.dividend_years(div, daily, rights=[])
    assert patched == 1
    y23, y24 = years.iloc[0], years.iloc[1]
    assert y23["year"] == 2023 and np.isnan(y23["yield"])            # 早于日线：没有股息率
    assert y24["times"] == 3 and y24["cash"] == pytest.approx(2.5) and y24["reserve"] == pytest.approx(0.3)
    assert y24["yield"] == pytest.approx((0.5 / 10 + 1.0 / 20 + 1.0 / 20) * 100)   # 逐次按除息前收盘价
    empty, _ = O.dividend_years(div.iloc[:0], daily.assign(adj_factor=1.0), rights=[])
    assert empty.empty


def test_build_reports_missing_parts(tmp_path):
    st = StockStore(root=tmp_path)
    BASICS.to_parquet(tmp_path / "basics.parquet")
    pd.DataFrame({"code": ["sh.600519"], "industry": ["C15酒、饮料和精制茶制造业"]}).to_parquet(tmp_path / "industry.parquet")
    daily_frame(300).to_parquet(st._path("sh.600519"))
    ov = O.build(st, "sh.600519")
    assert ov.name == "贵州茅台" and ov.industry.startswith("C15") and ov.close == 10
    assert ov.missing == ["fin", "div", "risk"] and np.isnan(ov.mcap)
    assert ov.pct["pe"]["pct"] == pytest.approx(50)                # 全部相等：位于中间
    (tmp_path / "fin").mkdir()
    fin_rows([["2023-12-31", "2024-03-29", "0.22", "0.5", "0.2", "120", "1300", "1", "12"]]) \
        .to_parquet(st._fin_path("sh.600519"))
    ov = O.build(st, "sh.600519")
    assert ov.missing == ["div", "risk"] and ov.mcap == pytest.approx(120) and len(ov.fin) == 1
    nothing = O.build(st, "sz.000519")
    assert nothing.missing == ["daily", "fin", "div", "risk"] and nothing.valuation is None
    assert O.requests_needed(st, "sz.000519", "2024-01-01", "2024-12-31") > 2


def test_pick_details_match_selection_scores():
    """单因子、手动权重：合成得分 = 方向 × 标准分；同一个 cache 第二次不再计算"""
    panel = make_panel()[0]
    start = str(panel.calendar[250].date())
    res = run_selection(panel, BASE, BROKER, start=start)
    cache = {}
    for d in list(res.schedule.picks)[:3]:
        codes = res.schedule.picks[d]
        info = pick_details(panel, BASE, d, codes, cache)
        for c in codes:
            raw, z = info[c]["ret20"]
            assert np.isfinite(raw) and -z == pytest.approx(res.schedule.scores[d][c])
    first = cache["ret20"]
    pick_details(panel, BASE, list(res.schedule.picks)[0], [], cache)
    assert cache["ret20"] is first
