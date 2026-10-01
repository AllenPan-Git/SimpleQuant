"""
可转债：条款解析、日线合并、付息复权、涨跌幅、强赎 / 退市卖出、每手 10 张、选股条件
数据用合成的（不联网）；下载流程用替身接口跑一遍
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.bonds.store import CBStore, parse_coupons, merge_daily, process_info
from simplequant.bonds.panel import (build_cb_panel, coupon_events, adj_factor, rating_score, cb_filters,
                                     CB_DEFAULT_FILTERS, FIRST_DAY_UP)
from simplequant.engine import BrokerConfig
from simplequant.stocks import run_selection, factors_for, FACTORS
from simplequant.stocks.selection import eligible_mask


# ---------------- 条款与日线 ----------------
@pytest.mark.parametrize("text, expect", [
    ("本次发行可转债票面利率:第一年为0.2%、第二年为0.5%、第三年为1.0%、第四年为1.5%、第五年为1.8%、第六年为2.0%。",
     [0.2, 0.5, 1.0, 1.5, 1.8, 2.0]),
    ("第一年0.30%、第二年0.50%、第三年1.00%、第四年1.50%、第五年1.80%、第六年2.00%。", [0.3, 0.5, 1.0, 1.5, 1.8, 2.0]),
    ("第一年至第六年分别为0.3%、0.5%、1.0%、1.5%、1.8%、2.0%", [0.3, 0.5, 1.0, 1.5, 1.8, 2.0]),
    ("第1年0.4%，第2年0.6%，第3年1.0%，第4年1.5%，第5年2.0%", [0.4, 0.6, 1.0, 1.5, 2.0]),
    ("票面利率为1.0%", None), (None, None),
])
def test_parse_coupons(text, expect):
    assert parse_coupons(text) == expect


def test_process_info_uses_coupon_years_for_maturity():
    """提前赎回的转债东方财富 BOND_EXPIRE 是实际存续年数（如 4.663），到期日按票面利率的年数算"""
    raw = {"SECURITY_CODE": "111001", "SECURITY_NAME_ABBR": "测试转债", "VALUE_DATE": "2021-11-12 00:00:00",
           "BOND_EXPIRE": "4.663", "INTEREST_RATE_EXPLAIN": "第一年0.3%、第二年0.5%、第三年1.0%、第四年1.5%、第五年1.8%、第六年2.0%",
           "EXECUTE_REASON_SH": "4", "NOTICE_DATE_SH": "2026-06-03 00:00:00", "DELIST_DATE": "2026-07-07 00:00:00"}
    out = process_info(raw)
    assert out["term_years"] == pytest.approx(4.663)
    assert out["coupons"] == "0.3,0.5,1,1.5,1.8,2"
    assert out["maturity"] == pd.Timestamp("2027-11-12")
    assert out["redeem_reason"] == "4" and out["redeem_notice"] == pd.Timestamp("2026-06-03")


def test_merge_daily_keeps_em_only_days_as_untradable():
    sina = pd.DataFrame({"date": ["2024-01-03", "2024-01-04"], "open": [101, 102], "high": [103, 104],
                         "low": [100, 101], "close": [102, 103], "volume": [1000, 2000]})
    value = pd.DataFrame({"DATE": ["2024-01-01 00:00:00", "2024-01-02 00:00:00", "2024-01-03 00:00:00",
                                   "2024-01-04 00:00:00"],
                          "FCLOSE": [None, 100.5, 102.0, 103.0], "PUREBONDVALUE": [90, 90, 90.1, 90.2],
                          "SWAPVALUE": [80, 81, 82, 83], "SWAPPRICE": [10, 10, 10, 9.5], "SYFE": [None, None, 5e8, 5e8]})
    df = merge_daily(sina, value)
    assert list(df.index.strftime("%m-%d")) == ["01-02", "01-03", "01-04"]      # 上市前（收盘价为空）去掉
    assert df.loc["2024-01-02", "sina"] == 0 and df.loc["2024-01-02", "raw_open"] == 100.5
    assert df.loc["2024-01-04", "conv_price"] == 9.5 and df.loc["2024-01-04", "remain_size"] == 5.0


# ---------------- 付息复权 ----------------
def test_coupon_events_and_adj_factor():
    cal = pd.bdate_range("2021-01-01", "2024-12-31")
    row = {"coupons": "0.5,1,1.5,2", "value_date": pd.Timestamp("2021-03-13")}       # 2022-03-13 是周日
    ev = coupon_events(row, cal, cal[-1])
    assert [d.strftime("%Y-%m-%d") for d, _ in ev] == ["2022-03-14", "2023-03-13", "2024-03-13"]
    assert [c for _, c in ev] == pytest.approx([0.4, 0.8, 1.2])                 # 扣 20% 税；最后一年不算
    close = pd.Series(110.0, index=cal)
    f = adj_factor(close, ev)
    assert f.loc["2022-03-11"] == 1.0
    assert f.loc["2022-03-14"] == pytest.approx(110 / 109.6)
    # 只算退市前的付息
    assert len(coupon_events(row, cal, pd.Timestamp("2023-01-01"))) == 1


def test_rating_score():
    assert rating_score("AAA") > rating_score("AA+") > rating_score("AA-") > rating_score("A+") > rating_score("CCC")
    assert rating_score(None) == 0


# ---------------- 合成数据：面板与回测 ----------------
CAL = pd.bdate_range("2022-06-01", "2023-06-30")


def _daily(start, end, base, drift=0.0, seed=0):
    days = CAL[(CAL >= pd.Timestamp(start)) & (CAL <= pd.Timestamp(end))]
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(drift, 0.01, len(days))))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, len(days)))
    df = pd.DataFrame({"raw_open": open_, "raw_high": np.maximum(open_, close) * 1.003,
                       "raw_low": np.minimum(open_, close) * 0.997, "raw_close": close,
                       "volume": 1e5, "bond_value": 95.0, "conv_value": close / 1.2, "conv_price": 10.0,
                       "remain_size": np.nan, "sina": 1.0}, index=days)
    df.index.name = "date"
    return df


BONDS = {
    # 代码: (上市, 最后交易日, 起价, 名称, 赎回原因, 赎回公告, 退市日)
    "113001": ("2022-06-01", "2023-06-30", 105, "甲转债", "", None, None),
    "113002": ("2022-06-01", "2023-01-20", 120, "乙转债", "4", "2023-01-05", "2023-01-30"),   # 强赎
    "123003": ("2022-06-01", "2023-06-30", 110, "丙转债", "", None, None),
    "123004": ("2022-09-15", "2023-06-30", 100, "丁转债", "", None, None),                    # 上市晚
    "113005": ("2022-06-01", "2023-06-30", 140, "戊转债", "", None, None),                    # 高价
}


@pytest.fixture
def store(tmp_path):
    st = CBStore(tmp_path / "cb")
    lst, info = [], []
    for i, (c, (lst_d, last, base, name, reason, notice, delist)) in enumerate(BONDS.items()):
        _daily(lst_d, last, base, seed=i).to_parquet(st._path(c))
        lst.append({"code": c, "name": name, "stock_code": f"60000{i}", "stock_name": "", "rating": "AA",
                    "listing_date": pd.Timestamp(lst_d), "issue_size": 5.0 + i})
        info.append({"code": c, "name": name, "stock_code": f"60000{i}", "listing_date": pd.Timestamp(lst_d),
                     "delist_date": pd.Timestamp(delist) if delist else pd.NaT, "value_date": pd.Timestamp("2021-06-15"),
                     "term_years": 6.0, "pay_day": "06-15", "coupon_text": "", "rating": "AA", "issue_size": 5.0 + i,
                     "redeem_reason": reason, "redeem_notice": pd.Timestamp(notice) if notice else pd.NaT,
                     "redeem_price": np.nan, "init_conv_price": 10.0, "coupons": "0.3,0.5,1,1.5,1.8,2",
                     "maturity": pd.Timestamp("2027-06-15")})
    pd.DataFrame(lst).to_parquet(st.root / "list.parquet")
    pd.DataFrame(info).to_parquet(st.root / "info.parquet")
    pd.DataFrame({"open": 300.0, "close": np.linspace(300, 320, len(CAL))}, index=CAL).to_parquet(st.root / "index.parquet")
    return st


def test_panel_basics(store):
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    assert p.kind == "cb" and p.lot_size == 10 and set(p.codes) == set(BONDS)
    # 强赎：公告日（2023-01-05）的下一个交易日起必须卖出；公告当天起不再入选
    assert p.exit_dates["113002"] == pd.Timestamp("2023-01-06")
    assert not p.exit.loc["2023-01-05", "113002"] and p.exit.loc["2023-01-06", "113002"]
    assert p.member.loc["2023-01-04", "113002"] and not p.member.loc["2023-01-05", "113002"]
    assert not p.can_buy.loc["2023-01-06", "113002"] and p.can_sell.loc["2023-01-06", "113002"]
    # 上市前不在范围内
    assert not p.member.loc["2022-09-14", "123004"] and p.member.loc["2022-09-15", "123004"]
    # 2022-06-15 付息（第一年 0.3%，税后 0.24）→ 复权因子
    assert p["adj_factor"].loc["2022-06-14", "113001"] == 1.0
    assert p["adj_factor"].loc["2022-06-15", "113001"] > 1.0
    # 双低 = 价格 + 溢价率
    prem = (p["raw_close"] / p["conv_value"] - 1) * 100
    pd.testing.assert_frame_equal(p["double_low"], p["raw_close"] + prem, check_names=False)


def test_first_day_and_post_2022_limits(store):
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    d = pd.Timestamp("2022-09-15")
    assert p.can_buy.loc[d, "123004"]                 # 首日开盘 100 < 157.3
    raw = p["raw_open"].copy()
    raw.loc[d, "123004"] = FIRST_DAY_UP
    from simplequant.bonds.panel import _limits
    first = pd.DataFrame(False, index=raw.index, columns=raw.columns)
    first.loc[d, "123004"] = True
    ok_buy, _ = _limits(raw, p["raw_preclose"], first)
    assert not ok_buy.loc[d, "123004"]                # 开盘即触及首日上限：买不进
    # 2022-08-01 之前没有涨跌幅限制
    raw2 = p["raw_open"].copy()
    raw2.loc["2022-07-01", "113001"] = p["raw_preclose"].loc["2022-07-01", "113001"] * 1.3
    ok_buy, _ = _limits(raw2, p["raw_preclose"], first)
    assert ok_buy.loc["2022-07-01", "113001"]
    raw2.loc["2022-08-02", "113001"] = p["raw_preclose"].loc["2022-08-02", "113001"] * 1.2
    ok_buy, _ = _limits(raw2, p["raw_preclose"], first)
    assert not ok_buy.loc["2022-08-02", "113001"]


def test_filters(store):
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    f = cb_filters({"max_price": 130, "min_amount": None, "min_remain_years": None, "max_premium": None})
    m = eligible_mask(p, f)
    high = p["raw_close"]["113005"] > 130
    assert not (m["113005"] & high).any()
    assert CB_DEFAULT_FILTERS["exclude_st"] is False and CB_DEFAULT_FILTERS["max_price"] == 130


SPEC = {"kind": "selection", "universe": "cb", "factors": [{"key": "cb_double_low", "weight": 1, "direction": -1}],
        "top_n": 3, "rebalance": "monthly", "position_pct": 95,
        "filters": {"max_price": None, "min_amount": None, "min_remain_years": None}}


def test_selection_sells_called_bond_and_uses_lot_10(store):
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    spec = {**SPEC, "top_n": 5}                     # 全部 5 只都持有：113002 一直在名单里，直到强赎
    res = run_selection(p, spec, BrokerConfig(cash=100_000, commission=0.00005, min_commission=0, stamp_duty=0))
    o = res.orders
    assert (o["size"] % 10 == 0).all() and (o["size"] % 100 != 0).any()
    sells = o[(o["symbol"] == "113002") & (o["side"] == "sell")]
    assert pd.Timestamp("2023-01-06") in set(pd.to_datetime(sells["time"]).dt.normalize())
    # 卖出后不再持有，也不再买入
    later = o[(o["symbol"] == "113002") & (pd.to_datetime(o["time"]) > pd.Timestamp("2023-01-06"))]
    assert later.empty
    assert any(k == "sel.exit_sell" for _, k, _ in res.logs)


def test_pending_includes_exit_for_paper(store):
    """模拟盘：强赎公告在最后一天发布时，明天开盘的委托里有卖出"""
    p = build_cb_panel(store, "2022-06-01", "2023-01-05")
    assert p.exit_dates["113002"] == pd.Timestamp("2023-01-06")
    spec = {**SPEC, "top_n": 5}
    res = run_selection(p, spec, BrokerConfig(cash=100_000), next_days=pd.DatetimeIndex(["2023-01-06"]))
    assert any(x["symbol"] == "113002" and x["side"] == "sell" and x["reason"][0] == "sel.reason_exit"
               for x in res.pending)


def test_cb_factors_registered():
    keys = factors_for("cb")
    assert "cb_double_low" in keys and "ret20" in keys and "ep" not in keys and "turn20" not in keys
    assert "cb_double_low" not in factors_for("stock")
    assert FACTORS["cb_double_low"]["direction"] == -1


def test_size_neutralization_uses_issue_size(store, monkeypatch):
    from simplequant.stocks import factors as F
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    used = []
    real = F.compute
    monkeypatch.setattr(F, "compute", lambda panel, key: used.append(key) or real(panel, key))
    F.factor_zscores(p, "cb_double_low", p.eligible(False, 0), {"size": True})
    assert used == ["cb_double_low", "cb_issue_size"]
    assert np.isfinite(real(p, "cb_issue_size").iloc[-1]).all()


# ---------------- 下载流程（替身接口） ----------------
def test_store_update_flow(tmp_path):
    days = pd.bdate_range("2024-01-01", "2024-03-29")
    calls = {"daily": 0, "value": []}

    def f_list():
        return pd.DataFrame({"code": ["113100", "123200"], "name": ["A转债", "B转债"], "stock_code": ["600100", "000200"],
                             "stock_name": ["", ""], "listing_date": pd.to_datetime(["2024-01-02", "2024-01-02"]),
                             "issue_size": [5.0, 3.0], "rating": ["AA", "AA-"]})

    def f_info(code):
        return process_info({"SECURITY_CODE": code, "SECURITY_NAME_ABBR": code, "LISTING_DATE": "2024-01-02",
                             "VALUE_DATE": "2023-12-20", "INTEREST_RATE_EXPLAIN": "第一年0.3%、第二年0.5%"})

    def f_daily(code):
        calls["daily"] += 1
        return pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "open": 100.0, "high": 101.0, "low": 99.0,
                             "close": 100.5, "volume": 1000})

    def f_value(code, since=None):
        calls["value"].append(since)
        d = days if since is None else days[days >= pd.Timestamp(since)]
        return pd.DataFrame({"DATE": d.strftime("%Y-%m-%d"), "FCLOSE": 100.5, "PUREBONDVALUE": 95.0,
                             "SWAPVALUE": 90.0, "SWAPPRICE": 10.0, "SYFE": None})

    def f_index(start, end):
        return pd.DataFrame({"open": 400.0, "close": 401.0}, index=pd.DatetimeIndex(days, name="date"))

    st = CBStore(tmp_path / "cb", fetchers={"list": f_list, "info": f_info, "daily": f_daily, "value": f_value,
                                            "index": f_index})
    errs = st.update_all("2023-01-01", "2024-03-29", workers=2)
    assert errs == {} and st.ready() and st.has("113100") and st.has("123200")
    assert calls["value"] == [None, None]
    assert len(st.load("113100")) == len(days)
    # 已更新到 end：再次运行不重下
    st.update(st.wanted("2023-01-01"), "2024-03-29")
    assert calls["daily"] == 2
    # 下一个交易日：增量（东方财富从最后日期往前 10 天起取）
    st.update(st.wanted("2023-01-01"), "2024-04-01")
    assert calls["daily"] == 4 and calls["value"][-1] == "2024-03-19"
    assert len(st.load("113100")) == len(days)


# ---------------- 模拟盘：逐日运行与一次性回测一致 ----------------
def test_cb_paper_daily_progression_equals_full_backtest(store, tmp_path, monkeypatch):
    from simplequant.paper import account as account_mod, create_account, run_account, load_account
    from simplequant.paper.calendar import next_trading_days
    monkeypatch.setattr(account_mod, "PAPER_ROOT", tmp_path / "paper")
    spec = {**SPEC, "top_n": 3, "rebalance": 10}
    broker = BrokerConfig(cash=200_000, commission=0.00005, min_commission=0, stamp_duty=0)
    full_panel = build_cb_panel(store, "2022-06-01", "2023-06-30")
    cal = full_panel.calendar.append(pd.bdate_range("2023-07-03", periods=40))
    start = "2022-08-01"
    full = run_selection(full_panel, spec, broker, start=start,
                         next_days=next_trading_days(cal, full_panel.calendar[-1]))
    acc = create_account("转债", spec, broker, start, universe="cb")
    states = []
    days = full_panel.calendar[full_panel.calendar >= pd.Timestamp(start)]
    for d in list(days[::9]) + [days[-1]]:
        states.append(run_account(acc, cal, panel=build_cb_panel(store, "2022-06-01", d.date().isoformat())))
    acc = load_account(acc.id)
    assert not any(s["divergence"] for s in states)
    got = [(pd.Timestamp(r.time), r.symbol, r.side, r.size) for r in acc.fills().itertuples()]
    want = [(pd.Timestamp(r.time), r.symbol, r.side, r.size) for r in full.orders.itertuples()]
    assert got == want and len(want) > 10
    assert any(s == "113002" and side == "sell" and t == pd.Timestamp("2023-01-06") for t, s, side, _ in want)


def test_every_cb_factor_computes(store):
    p = build_cb_panel(store, "2022-06-01", "2023-06-30")
    from simplequant.stocks import compute
    for key in factors_for("cb"):
        if FACTORS[key].get("custom"):
            continue
        v = compute(p, key)
        assert v.shape == p["close"].shape
        assert v.iloc[-1].notna().sum() >= 3, key
