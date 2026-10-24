"""退市处理（最后交易日强制卖出、退市日按 0 或最后价格结算）与全 A 股票池"""

import numpy as np
import pandas as pd
import pytest

from simplequant.stocks import StockStore, build_panel, build_schedule, run_selection
from simplequant.stocks.panel import (DELIST_CASH, DELIST_ZERO, ENDGAME_DAYS, EXIT_GAP_DAYS, delisting,
                                      delisting_period, exit_frame)
from simplequant.stocks.store import listed_snapshots
from test_stocks import SPEC, fake_fetcher, make_panel


def test_delisting_rules():
    cal = pd.bdate_range("2024-01-01", "2024-06-28")
    codes = ["sh.600001", "sh.600002", "sh.600003", "sh.600004", "sh.600005"]
    raw_close = pd.DataFrame(5.0, index=cal, columns=codes)
    tradable = pd.DataFrame(True, index=cal, columns=codes)
    is_st = pd.DataFrame(False, index=cal, columns=codes)
    last = pd.Timestamp("2024-03-01")
    tradable.loc[cal > last, codes[:3]] = False
    is_st.loc[:, "sh.600001"] = True                       # ST：困境退市
    raw_close.loc[:, "sh.600002"] = 9.9                    # 非 ST、股价 ≥ 1：吸收合并
    tradable.loc[cal > pd.Timestamp("2024-01-05"), "sh.600004"] = False   # 长期停牌后退市
    out = pd.Series({"sh.600001": pd.Timestamp("2024-03-12"), "sh.600002": pd.Timestamp("2024-03-09"),  # 周六
                     "sh.600003": pd.NaT, "sh.600004": pd.Timestamp("2024-05-31"),
                     "sh.600005": pd.Timestamp("2024-08-01")})                 # 区间之后才退市
    exit_dates, delist = delisting(raw_close, tradable, is_st, out, {"sh.600002": "葛洲坝"})
    assert exit_dates == {"sh.600001": last, "sh.600002": last}
    assert delist["sh.600001"] == (pd.Timestamp("2024-03-12"), DELIST_ZERO)
    assert delist["sh.600002"] == (pd.Timestamp("2024-03-11"), DELIST_CASH)      # 退市日不是交易日：下一个交易日结算
    assert (pd.Timestamp("2024-05-31") - pd.Timestamp("2024-01-05")).days > EXIT_GAP_DAYS
    assert delist["sh.600004"][0] == pd.Timestamp("2024-05-31") and "sh.600004" not in exit_dates
    assert "sh.600003" not in delist and "sh.600005" not in delist

    # 简称带「退」或股价低于 1 元也算困境退市
    raw_close.loc[:, "sh.600002"] = 0.8
    assert delisting(raw_close, tradable, is_st, out, {})[1]["sh.600002"][1] == DELIST_ZERO
    raw_close.loc[:, "sh.600002"] = 9.9
    assert delisting(raw_close, tradable, is_st, out, {"sh.600002": "退市西水"})[1]["sh.600002"][1] == DELIST_ZERO

    ex = exit_frame(exit_dates, cal, codes)
    assert not ex.loc[:last - pd.Timedelta(days=1)].any().any() and ex.loc[last:, "sh.600001"].all()


def test_delisting_period():
    """整理期 = 最后一次 ST 的次日到最后交易日（中间停牌的日子不算起点）；没有 ST 记录的取最后 ENDGAME_DAYS 个交易日"""
    cal = pd.bdate_range("2024-01-01", "2024-03-29")
    codes = ["sh.600001", "sh.600002", "sh.600003"]
    tradable = pd.DataFrame(True, index=cal, columns=codes)
    is_st = pd.DataFrame(False, index=cal, columns=codes)
    last = cal[50]
    is_st.loc[:cal[30], "sh.600001"] = True
    tradable.loc[cal[31]:cal[34], "sh.600001"] = False     # ST 摘帽前停牌几天，整理期从复牌那天算
    tradable.loc[cal > last, codes] = False
    assert delisting_period(tradable, is_st, {}) is None
    per = delisting_period(tradable, is_st, {"sh.600001": last, "sh.600002": last})
    assert per["sh.600001"][per["sh.600001"]].index.tolist() == list(cal[35:51])
    assert per["sh.600002"].sum() == ENDGAME_DAYS and per.at[last, "sh.600002"]
    assert not per["sh.600003"].any()


def test_exclude_st_also_excludes_delisting_period():
    p = make_panel()
    code, d = p.codes[0], p.calendar[5]
    assert p.eligible(True).at[d, code]
    p.delisting_period = pd.DataFrame(False, index=p.calendar, columns=p.codes)
    p.delisting_period.at[d, code] = True
    assert not p.eligible(True).at[d, code]
    assert p.eligible(False).at[d, code]             # 不排除 ST 时可以买


def _delist_one(sellable: bool, mode: int):
    """首个调仓后持有的一只股票在第 3 个交易日最后一次交易，10 个交易日后退市"""
    p = make_panel()
    sched = build_schedule(p, SPEC)
    d0 = list(sched.picks)[0]
    code = sched.picks[d0][0]
    j = p.codes.index(code)
    k = p.calendar.get_loc(d0) + 3                         # 最后交易日
    settle = p.calendar[k + 10]
    for f in ("open", "high", "low", "close", "raw_open", "raw_close"):
        p.fields[f].iloc[k + 1:, j] = p.fields[f].iloc[k, j]
    p.tradable.iloc[k + 1:, j] = False
    p.can_buy.iloc[k:, j] = False
    p.can_sell.iloc[k + 1:, j] = False
    p.can_sell.iloc[k, j] = sellable
    p.exit = exit_frame({code: p.calendar[k]}, p.calendar, p.codes)
    p.exit_dates = {code: p.calendar[k]}
    p.delist = {code: (settle, mode)}
    return p, code, k, settle, run_selection(p, SPEC)


def test_exit_sells_at_last_trading_day_open():
    p, code, k, settle, res = _delist_one(True, DELIST_ZERO)
    sells = res.orders[(res.orders["symbol"] == code) & (res.orders["side"] == "sell")]
    assert list(sells["time"])[:1] == [p.calendar[k]] and sells["price"].iloc[0] > 0
    assert any(key == "sel.exit_sell" and kw["name"] == code for _, key, kw in res.logs)
    assert not any(key.startswith("sel.delist") for _, key, _ in res.logs)


def test_unsold_distressed_delisting_is_written_off():
    p, code, k, settle, res = _delist_one(False, DELIST_ZERO)
    rows = res.orders[res.orders["symbol"] == code]
    sell = rows[rows["side"] == "sell"].iloc[0]
    assert sell["time"] == settle and sell["price"] == 0 and sell["value"] == 0 and sell["commission"] == 0
    assert sell["size"] == rows[rows["side"] == "buy"]["size"].sum()
    trade = res.trades[res.trades["symbol"] == code].iloc[0]
    buy_value = rows[rows["side"] == "buy"]["value"].sum()
    assert trade["pnl"] == pytest.approx(-buy_value, rel=1e-6) and trade["pnl_net"] < trade["pnl"]
    assert trade["bars"] > 10
    # 与按最后价格折现的同一回测比：结算前完全一样，结算当天正好少了这只股票的市值
    cash = _delist_one(False, DELIST_CASH)[-1]
    mv = sell["size"] * p["raw_close"].iloc[k, p.codes.index(code)]
    before = res.equity.index < settle
    assert res.equity["value"][before].equals(cash.equity["value"][before])
    assert cash.equity.loc[settle, "value"] - res.equity.loc[settle, "value"] == pytest.approx(mv, rel=1e-6)
    assert any(key == "sel.delist_zero" and kw["name"] == code for _, key, kw in res.logs)
    assert code not in {x["symbol"] for x in res.positions}


def test_unsold_merger_delisting_settles_in_cash():
    p, code, k, settle, res = _delist_one(False, DELIST_CASH)
    sell = res.orders[(res.orders["symbol"] == code) & (res.orders["side"] == "sell")].iloc[0]
    last = p["raw_close"].iloc[k, p.codes.index(code)]
    assert sell["time"] == settle and sell["price"] == pytest.approx(last)
    assert sell["value"] == pytest.approx(sell["size"] * last)
    assert any(key == "sel.delist_cash" for _, key, _ in res.logs)
    # 现金之后用于下一次调仓买入：最后一次调仓后的持仓仍是 top_n 只
    assert len(res.positions) == SPEC["top_n"]


def test_listed_snapshots():
    basics = pd.DataFrame({"code": ["sh.600001", "sh.600002", "sh.600003"], "name": ["A", "B", "C"],
                           "ipo_date": pd.to_datetime(["2010-01-01", "2024-02-15", "2010-01-01"]),
                           "out_date": pd.to_datetime([None, None, "2024-02-29"])})
    snaps = listed_snapshots(basics, pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-31"]))
    got = snaps.groupby("date")["code"].apply(list).to_dict()
    assert got == {pd.Timestamp("2024-01-31"): ["sh.600001", "sh.600003"],
                   pd.Timestamp("2024-02-29"): ["sh.600001", "sh.600002"],          # 退市日当天不算
                   pd.Timestamp("2024-03-31"): ["sh.600001", "sh.600002"]}


def test_all_a_universe_and_panel_delisting(tmp_path, monkeypatch):
    """全 A：成员来自 basics（含已退市）；面板按 basics 的退市日期设置强制卖出与结算"""
    def fetch(code, start, end):
        return fake_fetcher(code, start, "2024-02-16" if code == "sh.600002" else end)

    st = StockStore(tmp_path, fetcher=fetch, login=lambda: None, worker_init=lambda: None)
    basics = pd.DataFrame({"code": ["sh.600001", "sh.600002"], "name": ["A", "退市B"],
                           "ipo_date": pd.to_datetime(["2010-01-01", "2010-01-01"]),
                           "out_date": pd.to_datetime([None, "2024-02-26"])})
    monkeypatch.setattr(StockStore, "update_basics", lambda self: (basics.to_parquet(self.root / "basics.parquet"),
                                                                    basics)[1])
    uni = st.update_universe("all", "2024-01-01", "2024-03-29")
    assert set(uni.loc[uni["date"] == pd.Timestamp("2024-03-29"), "code"]) == {"sh.600001"}
    st.update(["sh.600001", "sh.600002"], "2024-01-01", "2024-03-29", workers=1)
    cal = pd.bdate_range("2024-01-01", "2024-03-29")
    pd.DataFrame({"open": 1.0, "close": 1.0}, index=cal).to_parquet(tmp_path / "index" / "sh.000001.parquet")
    p = build_panel(st, "all", "2024-01-01", "2024-03-29")
    assert p.codes == ["sh.600001", "sh.600002"]
    assert p.exit_dates == {"sh.600002": pd.Timestamp("2024-02-16")}
    assert p.delist == {"sh.600002": (pd.Timestamp("2024-02-26"), DELIST_ZERO)}     # 简称带「退」
    assert not p.can_buy.loc["2024-02-16":, "sh.600002"].any() and p.can_buy.loc["2024-02-15", "sh.600002"]
    assert p.exit is not None and not p.exit["sh.600001"].any()
