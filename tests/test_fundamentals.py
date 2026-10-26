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


def _align_reference(fin, calendar, field):
    """改用 numpy 之前的逐行写法，用来核对 align_all"""
    df = fin[["stat_date", "pub_date", field]].dropna()
    if df.empty:
        return pd.Series(np.nan, index=calendar)
    df = df.sort_values(["pub_date", "stat_date"])
    df = df[df["stat_date"] >= df["stat_date"].cummax()]
    idx = calendar.searchsorted((df["pub_date"] + pd.Timedelta(days=1)).values, side="left")
    s, stat = pd.Series(np.nan, index=calendar), pd.Series(pd.NaT, index=calendar)
    for i, v, sd in zip(idx, df[field].values, df["stat_date"].values):
        if i < len(calendar):
            s.iloc[i], stat.iloc[i] = v, sd
    s, stat = s.ffill(), stat.ffill()
    from simplequant.stocks.fundamentals import STALE_DAYS
    return s.mask(((calendar.values - stat.values) > np.timedelta64(STALE_DAYS, "D")) | stat.isna())


def test_align_all_matches_row_by_row_version():
    from simplequant.stocks.fundamentals import FIELDS, align_all
    rng = np.random.default_rng(3)
    cal = pd.bdate_range("2019-01-01", "2024-12-31")
    for _ in range(30):
        n = int(rng.integers(1, 25))
        stat = pd.to_datetime(rng.choice(pd.date_range("2016-03-31", "2024-12-31", freq="QE"), n))
        pub = stat + pd.to_timedelta(rng.integers(10, 900, n), unit="D")      # 有迟到很久的、有同一天公告几条的
        pub = pub.where(rng.random(n) > 0.2, pub[0])
        fin = pd.DataFrame({"stat_date": stat, "pub_date": pub})
        for f in FIELDS:
            v = rng.normal(0, 1, n)
            v[rng.random(n) < 0.2] = np.nan
            fin[f] = v
        fin.loc[rng.random(n) < 0.1, "pub_date"] = pd.NaT
        got = align_all(fin, cal, FIELDS)
        for j, f in enumerate(FIELDS):
            np.testing.assert_array_equal(got[:, j], _align_reference(fin, cal, f).values)


# ---------------- 排雷数据（资产负债表 + 现金流量表） ----------------
def risk_raw(rows):
    """rows: (报告期, 公告日, 资产负债率, 流动比率, 现金流/净利润, 现金流/营收)；权益乘数按负债率推出，
    liabilityToAsset 故意写成被多除了 100 的值（BaoStock 有一段是这样），不应被用到"""
    bal = pd.DataFrame([[r[0], r[1], float(r[2]) / 100, 1 / (1 - float(r[2])), r[3]] for r in rows],
                       columns=["statDate", "pubDate", "liabilityToAsset", "assetToEquity", "currentRatio"]).astype(str)
    cash = pd.DataFrame([[r[0], r[1], r[4], r[5]] for r in rows],
                        columns=["statDate", "pubDate", "CFOToNP", "CFOToOR"]).astype(str)
    return bal, cash


def test_risk_fields_and_loss_makers():
    from simplequant.stocks.fundamentals import process_risk, derive_risk
    bal, cash = risk_raw([
        ["2023-12-31", "2024-03-29", "0.55", "1.8", "1.2", "0.15"],
        ["2023-09-30", "2023-10-28", "1.25", "0.4", "0.5", "0.1"],    # 资不抵债：权益乘数为负，负债率 125%
        ["2024-03-31", "2024-04-26", "0.60", "", "-0.8", "0.10"],     # 现金流为正、比值为负：净利润为负 → 空
        ["2024-06-30", "2024-08-28", "0.62", "1.5", "2.0", "-0.05"],  # 都为负（亏损且现金流为负）：比值为正但也是空
        ["2024-09-30", "2024-10-30", "0.58", "1.6", "-0.5", "-0.02"], # 盈利、现金流为负：保留负值
    ])
    cash.loc[0, "pubDate"] = "2024-03-30"                              # 两张表公告日不同：取较晚的
    d = derive_risk(process_risk(bal, cash))
    d = d.iloc[1:]                                                     # 按报告期排序后第一行是资不抵债那期
    assert derive_risk(process_risk(bal, cash))["debt_ratio"].iloc[0] == pytest.approx(125)
    assert d["pub_date"].iloc[0] == pd.Timestamp("2024-03-30")
    assert d["debt_ratio"].tolist() == pytest.approx([55, 60, 62, 58])
    assert np.isnan(d["current_ratio"].iloc[1])
    assert d["cfo_np"].iloc[0] == pytest.approx(1.2) and d["cfo_np"].iloc[3] == pytest.approx(-0.5)
    assert d["cfo_np"].iloc[1:3].isna().all()


def test_risk_store_panel_and_filters(tmp_path):
    """排雷数据单独存、按公告日对齐进面板；排雷条件只在有数据时生效，没下载就报错（不能悄悄当成不限）"""
    from simplequant.stocks.fundamentals import process_risk
    from simplequant.stocks.panel import add_risk, RISK_FILTERS
    from simplequant.stocks.selection import eligible_mask, needs_risk
    calls = []

    def fake(code, qs):
        calls.append((code, len(qs)))
        ends = [pd.Timestamp(y, q * 3, 1) + pd.offsets.MonthEnd(0) for y, q in qs]
        debt = "0.3" if code == "sh.600000" else "0.9"
        bal, cash = risk_raw([[e.strftime("%Y-%m-%d"), (e + pd.Timedelta(days=25)).strftime("%Y-%m-%d"),
                               debt, "2", "1", "0.1"] for e in ends])
        return code, process_risk(bal, cash), ""

    st = StockStore(tmp_path, login=lambda: None, worker_init=lambda: None)
    codes = ["sh.600000", "sz.000001"]
    assert st.risk_requests(codes, 2024) == 2 * 2 * len(quarters(2024))     # 每只每季度 2 次
    assert not st.update_risk(codes, 2024, fetch=fake)
    assert st.has_risk("sh.600000") and not st.has_fin("sh.600000")          # 与财务数据分开存
    assert st.plan_risk(codes, 2024) == {} and st.risk_requests(codes, 2024) == 0

    p = make_panel(n=2)
    p.fields = {k: v.set_axis(codes, axis=1) for k, v in p.fields.items()}
    for name in ("member", "tradable", "is_st", "listed_days", "can_buy", "can_sell"):
        setattr(p, name, getattr(p, name).set_axis(codes, axis=1))
    flt = {"exclude_st": True, "min_list_days": 0, "max_debt_ratio": 50}
    spec = {"factors": [{"key": "ep"}], "filters": flt}
    assert needs_risk(spec) and needs_risk({"factors": [{"key": "cfo_np"}]}) and not needs_risk(SPEC)
    with pytest.raises(ValueError, match="排雷数据"):
        eligible_mask(p, flt)
    cal = p["close"].index
    p.has_risk = add_risk(p.fields, st, codes, cal)
    p.extra_filters = {k: m["fn"] for k, m in RISK_FILTERS.items()}
    assert p.has_risk and p["debt_ratio"]["sz.000001"].dropna().eq(90).all()
    m = eligible_mask(p, flt)
    has = p["debt_ratio"]["sh.600000"].notna()
    assert m.loc[has, "sh.600000"].all() and not m["sz.000001"].any()      # 负债率 90% 的排除
    assert not m.loc[~has, "sh.600000"].any()                                # 还没有公告数据的也不选
    assert eligible_mask(p, {**flt, "max_debt_ratio": None})["sz.000001"].all()   # 留空 = 不限
    assert F.compute(p, "debt_ratio")["sh.600000"].dropna().eq(30).all()


def test_risk_plan_version_and_platform_export():
    from simplequant import plan as P
    from simplequant.export.selection_platforms import check_selection_exportable
    base = {"kind": "selection", "universe": "hs300", "factors": [{"key": "ep", "weight": 1, "direction": 1}],
            "top_n": 10, "rebalance": "monthly", "filters": {"exclude_st": True, "min_list_days": 250}}
    assert P.selection_version(base) == P.SELECTION_VERSION
    risky = {**base, "filters": {**base["filters"], "min_cfo_np": 0.8}}
    assert P.selection_version(risky) == P.RISK_VERSION == P.VERSION
    assert P.needs({"strategy": risky})["risk"] and not P.needs({"strategy": base})["risk"]
    for spec in (risky, {**base, "factors": [{"key": "debt_ratio", "weight": 1, "direction": -1}]}):
        with pytest.raises(ValueError, match="排雷"):
            check_selection_exportable(spec, "joinquant")
