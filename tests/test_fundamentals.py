"""
第二期：财务因子（按公告日对齐）、行业/市值中性化、IC 加权合成
重点验证"不会用到未来数据"
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from test_stocks import make_panel, SPEC
from simplequant.stocks import StockStore, build_schedule, run_selection
from simplequant.stocks import factors as F
from simplequant.stocks.fundamentals import quarters, process_profit, derive, align
from simplequant.stocks.research import forward_returns, rank_ic


def fin_rows(rows):
    raw = pd.DataFrame(rows, columns=["statDate", "pubDate", "roeAvg", "gpMargin", "npMargin", "netProfit",
                                      "MBRevenue", "epsTTM", "totalShare"]).astype(str)
    return process_profit(raw)


FIN = fin_rows([
    ["2022-12-31", "2023-03-30", "0.20", "0.5", "0.2", "100", "1000", "1", "10"],
    ["2023-03-31", "2023-04-28", "0.06", "0.5", "0.2", "30", "", "1", "10"],
    ["2023-06-30", "2023-08-25", "0.11", "0.5", "0.2", "60", "600", "1", "10"],
    ["2023-09-30", "2023-10-27", "0.16", "0.5", "0.2", "90", "", "1", "12"],
    ["2023-12-31", "2024-03-29", "0.22", "0.5", "0.2", "120", "1300", "1", "12"],
    ["2022-06-30", "2022-08-20", "0.10", "0.5", "0.2", "50", "500", "1", "10"],
    ["2022-03-31", "2022-04-20", "0.05", "0.5", "0.2", "25", "", "1", "10"],
])
CAL = pd.bdate_range("2023-01-02", "2024-06-28")


def test_quarters():
    q = quarters(2025, dt.date(2026, 5, 10))
    assert q[0] == (2025, 1) and q[-1] == (2026, 1)
    assert quarters(2025, dt.date(2026, 1, 5))[-1] == (2025, 4)


def test_derive_annualizes_roe_and_computes_yoy():
    d = derive(FIN).set_index("stat_date")
    assert d.loc["2023-03-31", "roe"] == pytest.approx(24.0)          # 6% × 4
    assert d.loc["2023-09-30", "roe"] == pytest.approx(16 * 4 / 3)
    assert d.loc["2023-06-30", "np_yoy"] == pytest.approx(20.0)      # 60 vs 50
    assert d.loc["2023-12-31", "rev_yoy"] == pytest.approx(30.0)     # 1300 vs 1000
    assert np.isnan(d.loc["2023-03-31", "rev_yoy"])                  # 一季报没有营收


def test_align_uses_data_only_after_announcement():
    fin = derive(FIN)
    roe = align(fin, CAL, "roe")
    # 2023 一季报 4/28（周五）公告 → 4/28 当天仍用 2022 年报，下一个交易日 5/1 起才用一季报
    assert roe["2023-04-28"] == pytest.approx(20.0)
    assert roe["2023-05-01"] == pytest.approx(24.0)
    # 营收同比只在半年报/年报有：三季报公告后仍沿用半年报的值，而不是变成空值
    rev = align(fin, CAL, "rev_yoy")
    assert np.isnan(rev["2023-08-25"]) and rev["2023-08-28"] == pytest.approx(20.0)
    assert rev["2023-11-15"] == pytest.approx(20.0)
    assert rev["2024-04-01"] == pytest.approx(30.0)


def test_late_restated_old_period_does_not_override_newer():
    fin = derive(pd.concat([FIN, fin_rows([["2023-03-31", "2023-11-20", "0.99", "0.5", "0.2", "30", "", "1", "10"]])])
                 .drop_duplicates("stat_date", keep="last"))
    roe = align(fin, CAL, "roe")
    assert roe["2023-11-21"] == pytest.approx(16 * 4 / 3)            # 仍是三季报，不被迟到的一季报覆盖


def test_stale_data_expires():
    fin = derive(FIN[FIN["stat_date"] <= "2023-03-31"])
    roe = align(fin, pd.bdate_range("2023-01-02", "2024-12-31"), "roe")
    assert roe["2024-01-02"] == pytest.approx(24.0)
    assert np.isnan(roe["2024-09-02"])                               # 超过 15 个月未更新


# ---------------- 中性化 ----------------
def test_neutralize_removes_industry_and_size_exposure():
    rng = np.random.default_rng(0)
    n, days = 120, 30
    codes = [f"c{i}" for i in range(n)]
    ind = pd.Series(np.repeat(["银行", "医药", "电子", "食品"], n // 4), index=codes)
    size = pd.DataFrame(rng.normal(size=(days, n)), columns=codes)
    ind_effect = ind.map({"银行": 3.0, "医药": -1.0, "电子": 0.5, "食品": 0.0}).values
    raw = pd.DataFrame(ind_effect + 2 * size.values + rng.normal(size=(days, n)), columns=codes)
    out = F.neutralize(F.zscore(raw), ind, size)
    for d in range(days):
        row = out.iloc[d]
        assert abs(row.corr(size.iloc[d])) < 0.05
        assert row.groupby(ind).mean().abs().max() < 0.05
        assert abs(row.mean()) < 1e-9 and abs(row.std() - 1) < 1e-9


def test_small_industries_grouped():
    ind = pd.Series(["A"] * 5 + ["B"] * 2 + ["C"], index=range(8))
    assert F.group_small_industries(ind).tolist() == ["A"] * 5 + ["其他"] * 3


# ---------------- IC 加权 ----------------
def test_ic_weights_use_only_realized_ic():
    ic = pd.Series(np.arange(300, dtype=float), index=pd.bdate_range("2024-01-01", periods=300))
    w = F.ic_weights(ic, horizon=20, lookback=60, mode="ic")
    d = 200
    # T=d 时已知的 IC 截止到 d-21（IC[d'] 需要 d'+21 日开盘价）
    assert w.iloc[d] == pytest.approx(ic.iloc[d - 21 - 59: d - 21 + 1].mean())


def test_ic_weighting_learns_sign_and_has_no_lookahead():
    p = make_panel(days=500)
    spec = {**SPEC, "factors": [{"key": "ep", "weight": 1, "direction": -1}], "weighting": "ic", "ic_lookback": 120}
    # 手动方向故意设错（-1）；IC 加权应自动学到 EP 越大越好，选出高 EP 的股票
    sched = build_schedule(p, spec)
    late = [d for d in sched.picks if d > p.calendar[250]]
    ep = F.compute(p, "ep")
    for d in late:
        assert ep.loc[d, sched.picks[d]].mean() > ep.loc[d].median()
    # 截断未来数据后，截断前的选股完全相同
    cut = p.calendar[400]
    short = make_panel(days=500)
    for f in short.fields:
        short.fields[f] = short.fields[f].loc[:cut]
    for attr in ("member", "tradable", "is_st", "listed_days", "can_buy", "can_sell"):
        setattr(short, attr, getattr(short, attr).loc[:cut])
    part = build_schedule(short, spec)
    common = [d for d in part.picks if d < cut]
    assert len(common) > 10 and all(part.picks[d] == sched.picks[d] for d in common)


def test_neutralized_selection_spreads_across_industries():
    p = make_panel(n=40)
    # 让 EP 完全由行业决定：不中性化时只会选到同一个行业
    ind = pd.Series(["银行"] * 10 + ["医药"] * 10 + ["电子"] * 10 + ["食品"] * 10, index=p.codes)
    p.industry = ind
    boost = ind.map({"银行": 1.0, "医药": 0.0, "电子": 0.0, "食品": 0.0})
    p.fields["pe"] = 1 / (1 / p.fields["pe"] + boost.values * 0.5)
    plain = build_schedule(p, SPEC)
    neutral = build_schedule(p, {**SPEC, "neutralize": {"industry": True}})
    d = list(plain.picks)[-1]
    assert set(ind[plain.picks[d]]) == {"银行"}
    assert len(set(ind[neutral.picks[d]])) >= 2
    res = run_selection(p, {**SPEC, "neutralize": {"industry": True}})
    assert res.metrics["final_value"] > 0


def test_financial_factors_nan_without_data():
    p = make_panel()
    for k in ("roe", "np_yoy", "size"):
        assert F.compute(p, k).isna().all().all()


# ---------------- 存储：财务数据增量 ----------------
def test_fundamentals_store_incremental(tmp_path):
    calls = []

    def fake(code, qs):
        calls.append((code, len(qs)))
        ends = [pd.Timestamp(y, q * 3, 1) + pd.offsets.MonthEnd(0) for y, q in qs]
        rows = [[e.strftime("%Y-%m-%d"), (e + pd.Timedelta(days=25)).strftime("%Y-%m-%d"), "0.1", "0.5", "0.2",
                 "10", "", "1", "10"] for e in ends]
        return code, fin_rows(rows), ""

    st = StockStore(tmp_path, login=lambda: None, worker_init=lambda: None)
    st.update_fundamentals(["sh.600000"], 2024, workers=1, fetch=fake)
    n_all = calls[-1][1]
    assert n_all >= 6 and len(st.load_fin("sh.600000")) == n_all
    assert st.plan_fundamentals(["sh.600000"], 2024) == {}                   # 今天已检查过
    import json
    (tmp_path / "fin_manifest.json").write_text(json.dumps({"sh.600000": "2000-01-01"}))
    jobs = st.plan_fundamentals(["sh.600000"], 2024)
    assert len(jobs["sh.600000"][0]) == 2                                   # 只重下最近两个季度
