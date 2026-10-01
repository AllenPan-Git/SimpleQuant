"""
分红送转：数据处理、红利税、选股回测的「现金分红并扣红利税」模式
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.engine import BrokerConfig
from simplequant.stocks import Panel, run_selection, StockStore
from simplequant.stocks.dividends import (process_dividends, align, tax_rate, split_factor, missing_events,
                                          process_rights)
from simplequant.stocks.panel import trading_constraints, add_dividends
from simplequant.stocks.selection import Schedule

FREE = BrokerConfig(cash=100_000, commission=0, min_commission=0, stamp_duty=0, slippage=0)
SPEC = {"kind": "selection", "universe": "hs300", "factors": [{"key": "ep", "weight": 1, "direction": 1}],
        "top_n": 1, "rebalance": "monthly", "position_pct": 95}


def raw_rows(*rows):
    cols = ["code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate", "dividPlanDate",
            "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate", "dividCashPsBeforeTax",
            "dividCashPsAfterTax", "dividStocksPs", "dividCashStock", "dividReserveToStockPs"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def row(ex, cash="", stocks="", reserve=""):
    return ["sh.600000", "", "", "", "", "", ex, "", "", cash, "文字说明", stocks, "10派3.2元", reserve]


def test_process_dividends_parses_and_merges():
    df = process_dividends(raw_rows(row("2023-07-21", "0.32", "0.000000"),
                                    row("", "0.5"),                              # 未实施：没有除权除息日
                                    row("2023-07-21", "0.32", "0.000000"),       # BaoStock 的重复记录：去掉
                                    row("2024-06-19", "0.1", "0.2"),
                                    row("2024-06-19", "", "", "0.3"),             # 同一天分开公告：合并
                                    row("2025-01-02", "0", "0", "0")))            # 全为 0：丢弃
    assert df["ex_date"].dt.strftime("%Y-%m-%d").tolist() == ["2023-07-21", "2024-06-19"]
    assert df["cash"].tolist() == [0.32, 0.1]
    assert df["bonus"].tolist() == [0.0, 0.2] and df["reserve"].tolist() == [0.0, 0.3]
    assert process_dividends(pd.DataFrame()).empty


def test_align_moves_to_next_trading_day_and_drops_earlier():
    cal = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"])
    div = pd.DataFrame({"ex_date": pd.to_datetime(["2023-12-01", "2024-01-04", "2024-02-01"]),
                        "cash": [9.0, 0.5, 1.0], "bonus": [0.0, 0.0, 0.0], "reserve": [0.0, 0.0, 0.0]})
    ev = align(div, cal)
    assert ev["cash"].tolist() == [0.0, 0.0, 0.5]


def test_tax_rate_holding_periods():
    assert tax_rate("2024-01-10", "2024-02-10") == 0.20      # 持有到交割前一日恰好 1 个月内
    assert tax_rate("2024-01-10", "2024-02-12") == 0.10
    assert tax_rate("2024-01-10", "2025-01-11") == 0.10      # 含 1 年
    assert tax_rate("2024-01-10", "2025-01-13") == 0.0


def test_split_factor():
    b = pd.Series([0.0, 0.2, 0.0])
    r = pd.Series([0.0, 0.3, 1.0])
    assert split_factor(b, r).round(6).tolist() == [1.0, 1.5, 3.0]


# ---------------- 回测 ----------------
def two_stock_panel(ex_day=5, cash=0.5, reserve=0.0, days=40) -> Panel:
    """A：不复权价 10，第 ex_day 天除权除息；后复权价恒为 10。B：价格恒为 10"""
    cal = pd.bdate_range("2024-01-02", periods=days)
    codes = ["sh.600001", "sh.600002"]
    raw = pd.DataFrame(10.0, index=cal, columns=codes)
    raw.iloc[ex_day:, 0] = (10.0 - cash) / (1 + reserve)
    adj = raw.iloc[0] / raw                                   # 后复权因子：使后复权价恒为 10
    pre = raw.shift(1).bfill()
    pre.iloc[ex_day, 0] = raw.iloc[ex_day, 0]                 # 除权参考价
    df = lambda v: pd.DataFrame(v, index=cal, columns=codes)
    fields = {f: raw * adj for f in ("open", "high", "low", "close")}
    fields.update(raw_open=raw, raw_close=raw, raw_preclose=pre, adj_factor=adj, volume=df(1e6))
    ev = pd.DataFrame(0.0, index=cal, columns=codes)
    fields["div_cash"], fields["div_bonus"], fields["div_reserve"] = ev.copy(), ev.copy(), ev.copy()
    fields["div_cash"].iloc[ex_day, 0] = cash
    fields["div_reserve"].iloc[ex_day, 0] = reserve
    tradable, is_st = df(True), df(False)
    can_buy, can_sell = trading_constraints(raw, pre, tradable, is_st)
    return Panel(fields=fields, member=df(True), tradable=tradable, is_st=is_st, listed_days=df(1000.0),
                 can_buy=can_buy, can_sell=can_sell, benchmark=pd.Series(10.0, index=cal),
                 names={c: c for c in codes}, div_codes=frozenset(codes))


def run(panel, mode, switch_day=20):
    cal = panel.calendar
    sched = Schedule(picks={cal[0]: ["sh.600001"], cal[switch_day]: ["sh.600002"]}, scores={})
    return run_selection(panel, {**SPEC, "dividend": mode}, FREE, schedule=sched)


def test_cash_dividend_credited_and_taxed_on_sale():
    p = two_stock_panel()
    re_, cash = run(p, "reinvest"), run(p, "cash")
    shares = int(cash.orders.iloc[0]["size"])
    assert shares == 9400                                    # 95% 资金 / 10 元，扣预留后取整手
    # 持有期间两种模式资产相同（现金分红只是从股价转到现金）
    assert cash.equity["value"].iloc[:21].round(2).tolist() == re_.equity["value"].iloc[:21].round(2).tolist()
    assert cash.metrics["dividend_cash"] == pytest.approx(shares * 0.5)
    # 持有 20 个交易日（不到 1 个月）卖出：按 20% 扣税
    assert cash.metrics["dividend_tax"] == pytest.approx(shares * 0.5 * 0.2)
    assert "dividend_cash" not in re_.metrics
    keys = [k for _, k, _ in cash.logs]
    assert "sel.dividend" in keys and "sel.dividend_tax" in keys
    sell = cash.orders[(cash.orders["side"] == "sell")].iloc[0]
    assert sell["size"] == shares and sell["price"] == pytest.approx(9.5)


def test_long_holding_is_tax_free():
    p = two_stock_panel(days=300)
    res = run(p, "cash", switch_day=280)                    # 持有一年以上
    assert res.metrics["dividend_tax"] == 0


def test_capitalization_shares_increase_share_count():
    p = two_stock_panel(cash=0.0, reserve=1.0)               # 10 转 10：不复权价减半
    res = run(p, "cash")
    buy, sell = res.orders.iloc[0], res.orders[res.orders["side"] == "sell"].iloc[0]
    assert sell["size"] == 2 * buy["size"] and sell["price"] == pytest.approx(5.0)
    assert res.metrics["dividend_cash"] == 0 and res.metrics["dividend_tax"] == 0
    # 没有现金分红：资产与后复权完全一样
    re_ = run(p, "reinvest")
    assert res.equity["value"].round(2).tolist() == re_.equity["value"].round(2).tolist()


def test_stock_without_dividend_data_falls_back_to_reinvest():
    p = two_stock_panel()
    p.div_codes = frozenset()
    res, re_ = run(p, "cash"), run(p, "reinvest")
    assert res.div_missing == ["sh.600001", "sh.600002"]
    assert res.equity["value"].round(2).tolist() == re_.equity["value"].round(2).tolist()


def test_store_dividends_and_panel(tmp_path):
    st = StockStore(tmp_path, login=lambda: None)
    div = pd.DataFrame({"ex_date": pd.to_datetime(["2024-01-04"]), "cash": [0.5], "bonus": [0.0], "reserve": [0.0]})
    calls = []

    def fetch(code, years):
        calls.append((code, tuple(years)))
        return code, (div if code == "sh.600001" else process_dividends(pd.DataFrame())), ""
    assert st.update_dividends(["sh.600001", "sh.600002"], 2023, workers=1, fetch=fetch) == {}
    assert st.has_div("sh.600001") and st.has_div("sh.600002") and st.load_div("sh.600002").empty
    assert st.update_dividends(["sh.600001"], 2023, workers=1, fetch=fetch) == {}    # 今天已检查过
    assert len(calls) == 2
    cal = pd.bdate_range("2024-01-02", periods=5)
    codes = ["sh.600001", "sh.600002", "sh.600003"]
    flat = pd.DataFrame(10.0, index=cal, columns=codes)
    frames = {"raw_close": flat, "raw_preclose": flat, "adj_factor": flat / 10}
    have, patched = add_dividends(frames, st, codes, cal)
    assert have == {"sh.600001", "sh.600002"} and patched is None
    assert frames["div_cash"]["sh.600001"].tolist() == [0, 0, 0.5, 0, 0]
    assert frames["div_cash"]["sh.600003"].sum() == 0


def _daily(closes, precloses, factors):
    idx = pd.bdate_range("2024-01-02", periods=len(closes))
    return pd.DataFrame({"raw_close": closes, "raw_preclose": precloses, "adj_factor": factors}, index=idx)


def test_missing_events_special_dividend_rights_and_factor_fix():
    """茅台式特别分红（分红表漏记）补成现金；配股视同全额参与（股数增加、无现金）；因子修正不管"""
    # 第 2 天：特别分红 0.3（参考价 10 - 0.3）；第 4 天：10 配 3、配股价 5 → 参考价 (9.7 + 1.5) / 1.3；
    # 第 6 天：复权因子变了但价格没变；第 7 天：分红表里有的 0.2 元分红
    x_rights = (9.7 + 0.3 * 5) / 1.3
    d = _daily([10, 9.7, 9.7, x_rights, x_rights, x_rights, x_rights - 0.2],
               [10, 9.7, 9.7, x_rights, x_rights, x_rights, x_rights - 0.2],
               [1.0, 10 / 9.7, 10 / 9.7, 10 / 9.7 * 9.7 / x_rights, 10 / 9.7 * 9.7 / x_rights,
                10 / x_rights * 1.01, 10 / x_rights * 1.01 * x_rights / (x_rights - 0.2)])
    div = pd.DataFrame({"ex_date": [d.index[6]], "cash": [0.2], "bonus": [0.0], "reserve": [0.0]})
    out = missing_events(d, div, rights=[d.index[2]])
    assert out["ex_date"].tolist() == [d.index[1], d.index[3]]
    assert out["cash"].tolist() == pytest.approx([0.3, 0.0])
    assert out["reserve"].iloc[1] == pytest.approx(9.7 / x_rights - 1)      # 视同配股后股数增加，总市值不变
    # 当天是 ST：破产重整的转增，同样不算现金
    st_day = d.assign(is_st=[0, 1, 0, 0, 0, 0, 0])
    assert missing_events(st_day, div, rights=[d.index[2]])["cash"].tolist() == [0.0, 0.0]
    # 没有配股表：漏记的一律当作现金分红
    assert missing_events(d, div, rights=None)["cash"].tolist() == pytest.approx([0.3, 9.7 - x_rights], abs=1e-4)


def test_missing_events_merged_into_panel_and_selection(tmp_path):
    st = StockStore(tmp_path, login=lambda: None)
    st.update_dividends(["sh.600001"], 2023, workers=1, fetch=lambda code, years: (code, process_dividends(pd.DataFrame()), ""))
    cal = pd.bdate_range("2024-01-02", periods=4)
    frames = {"raw_close": pd.DataFrame({"sh.600001": [10, 9.7, 9.7, 9.7]}, index=cal),
              "raw_preclose": pd.DataFrame({"sh.600001": [10, 9.7, 9.7, 9.7]}, index=cal),
              "adj_factor": pd.DataFrame({"sh.600001": [1, 10 / 9.7, 10 / 9.7, 10 / 9.7]}, index=cal)}
    have, patched = add_dividends(frames, st, ["sh.600001"], cal)
    assert frames["div_cash"]["sh.600001"].tolist() == pytest.approx([0, 0.3, 0, 0])
    assert patched["code"].tolist() == ["sh.600001"]


def test_process_rights():
    raw = pd.DataFrame({"股票代码": ["600030", "1555"], "股权登记日": ["2022-01-18", "2021-12-14"]})
    df = process_rights(raw)
    assert df["code"].tolist() == ["sh.600030", "sz.001555"]
    assert process_rights(pd.DataFrame()).empty
