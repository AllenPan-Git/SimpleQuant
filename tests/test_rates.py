"""
利率与信用利差（中债收益率曲线）：解析、增量更新、按日期并入行情、规则条件、导出限制
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from simplequant import strategies
from simplequant.bonds.rates import RatesStore, process, to_macro, attach, MACRO_COLUMNS
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.rules import make_ind, macro_columns, required_columns, validate


def raw_curves(days):
    rows = []
    for i, d in enumerate(days):
        rows.append({"曲线名称": "中债国债收益率曲线", "日期": d.strftime("%Y-%m-%d"), "3月": 1.0, "6月": 1.1,
                     "1年": 1.2, "3年": 1.5, "5年": 1.7, "7年": 1.8, "10年": 2.0 + i * 0.001, "30年": 2.3})
        rows.append({"曲线名称": "中债中短期票据收益率曲线(AAA)", "日期": d.strftime("%Y-%m-%d"), "3月": 1.5, "6月": 1.6,
                     "1年": 1.7, "3年": 2.0 + i * 0.002, "5年": 2.2, "7年": 2.4, "10年": 2.6, "30年": None})
        rows.append({"曲线名称": "中债商业银行普通债收益率曲线(AAA)", "日期": d.strftime("%Y-%m-%d"), "3月": 9, "6月": 9,
                     "1年": 9, "3年": 9, "5年": 9, "7年": 9, "10年": 9, "30年": 9})
    return pd.DataFrame(rows)


def test_process_and_macro():
    days = pd.bdate_range("2025-01-02", periods=5)
    m = to_macro(process(raw_curves(days)))
    assert list(m.columns) == MACRO_COLUMNS and len(m) == 5
    assert m["cgb10y"].iloc[0] == pytest.approx(2.0)
    assert m["term_spread"].iloc[0] == pytest.approx(80)          # (2.0 − 1.2) × 100
    assert m["credit_spread"].iloc[0] == pytest.approx(50)        # (2.0 − 1.5) × 100
    assert m["credit_spread"].iloc[4] == pytest.approx(50.8)       # (2.008 − 1.5) × 100


def test_store_update_fetches_missing_and_current_years(tmp_path):
    calls = []

    def fetch(year, today):
        calls.append(year)
        end = min(dt.date(year, 12, 31), today)
        return process(raw_curves(pd.bdate_range(f"{year}-01-02", end)))
    st = RatesStore(tmp_path / "rates.parquet", fetch=fetch)
    assert not st.ready() and st.stale()
    st.update(2023, today=dt.date(2025, 3, 10))
    assert calls == [2023, 2024, 2025]
    st.update(2023, today=dt.date(2025, 3, 12))
    assert calls[3:] == [2025]                                     # 往年已完整，只重下当年
    m = st.load()
    assert m.index[0] == pd.Timestamp("2023-01-02") and m.index[-1] == pd.Timestamp("2025-03-12")
    assert not m.index.duplicated().any()


def test_attach_daily_and_intraday():
    macro = pd.DataFrame({"cgb10y": [2.0, 2.1, 2.2], "term_spread": [80, 81, 82], "credit_spread": [50, 60, 70]},
                         index=pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]))
    daily = pd.DataFrame({"close": [1.0, 1.0, 1.0, 1.0]},
                         index=pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]))
    out = attach({"A": daily}, {"credit_spread"}, macro)["A"]
    assert list(out["credit_spread"]) == [50, 60, 70, 70]          # 当天值；缺的日子沿用最近一天
    assert "cgb10y" not in out and "credit_spread" not in daily     # 只并入需要的列，不改原表
    mins = pd.DataFrame({"close": 1.0}, index=pd.to_datetime(["2025-01-03 09:31", "2025-01-03 09:32",
                                                              "2025-01-06 09:31"]))
    out = attach({"A": mins}, {"credit_spread"}, macro)["A"]
    assert list(out["credit_spread"]) == [50, 50, 60]              # 分钟线用前一天的值


SPREAD_RULE = {"buy": {"logic": "all", "conditions": [
                   {"left": make_ind("credit_spread"), "op": "<", "right": {"value": 60}}]},
               "sell": {"logic": "all", "conditions": [
                   {"left": make_ind("credit_spread"), "op": ">", "right": {"value": 90}}]},
               "position_pct": 90}


def test_rule_uses_credit_spread():
    assert validate(SPREAD_RULE, "zh") == []
    assert macro_columns(SPREAD_RULE) == {"credit_spread"} and required_columns(SPREAD_RULE) == {"credit_spread"}
    df = make_prices(n=200)
    spread = pd.Series(np.where(np.arange(200) % 80 < 40, 50.0, 100.0), index=df.index)    # 40 天低、40 天高交替
    macro = pd.DataFrame({"cgb10y": 2.0, "term_spread": 50.0, "credit_spread": spread})
    prices = attach({"510300": df}, {"credit_spread"}, macro)
    res = run_backtest(prices, *strategies.resolve({"kind": "rule", "rule": SPREAD_RULE}), BrokerConfig())
    o = res.orders
    buys, sells = o[o["side"] == "buy"], o[o["side"] == "sell"]
    assert len(buys) == 3 and len(sells) == 2
    for t in buys["time"]:                                 # 信号日（前一根 K 线）利差低
        assert spread.loc[:t].iloc[-2] < 60
    for t in sells["time"]:
        assert spread.loc[:t].iloc[-2] > 90


def test_rates_rules_cannot_be_exported():
    from simplequant.export import single_asset_script, platform_script
    from dataclasses import asdict
    spec = {"kind": "rule", "rule": SPREAD_RULE}
    with pytest.raises(ValueError, match="利率"):
        single_asset_script(spec, [{"name": "510300", "source": "akshare", "symbol": "510300"}], asdict(BrokerConfig()))
    with pytest.raises(ValueError, match="利率"):
        platform_script("joinquant", spec, [{"name": "510300", "symbol": "510300", "asset": "etf"}],
                        asdict(BrokerConfig()), "2024-01-01", "2024-12-31")


ERP_RULE = {"buy": {"logic": "all", "conditions": [
                {"left": make_ind("erp"), "op": ">", "right": {"value": 4}}]},
            "sell": {"logic": "all", "conditions": [
                {"left": make_ind("erp"), "op": "<", "right": {"value": 2}}]},
            "position_pct": 90}


def test_rule_uses_equity_risk_premium():
    """股债利差 = 100 / PE − 10 年国债；要市盈率列和利率数据两样，市盈率为负时为空（不触发条件）"""
    assert validate(ERP_RULE, "zh") == []
    assert required_columns(ERP_RULE) == {"pe", "cgb10y"} and macro_columns(ERP_RULE) == {"cgb10y"}
    df = make_prices(n=200)
    pe = pd.Series(np.where(np.arange(200) % 80 < 40, 12.5, 25.0), index=df.index)      # 1/PE 8% 与 4% 交替
    pe.iloc[150:] = -10.0                                                                 # 亏损：利差为空
    df["pe"] = pe
    macro = pd.DataFrame({"cgb10y": 2.5, "term_spread": 50.0, "credit_spread": 50.0}, index=df.index)
    prices = attach({"510300": df}, {"cgb10y"}, macro)
    res = run_backtest(prices, *strategies.resolve({"kind": "rule", "rule": ERP_RULE}), BrokerConfig())
    o = res.orders
    buys, sells = o[o["side"] == "buy"], o[o["side"] == "sell"]
    assert len(buys) == 2 and len(sells) == 2                     # 8 − 2.5 = 5.5 > 4 买；4 − 2.5 = 1.5 < 2 卖
    for t in buys["time"]:
        assert pe.loc[:t].iloc[-2] == 12.5
    assert o["time"].max() < df.index[151]                        # 市盈率为负以后不再有信号


def test_erp_percentile_and_export():
    from simplequant.export.python_script import _line_expr
    rule = {"buy": {"logic": "all", "conditions": [
                {"left": make_ind("erp_pct", years=1), "op": ">", "right": {"value": 80}}]},
            "sell": {"logic": "all", "conditions": [
                {"left": make_ind("erp_pct", years=1), "op": "<", "right": {"value": 20}}]},
            "position_pct": 90}
    assert validate(rule, "zh") == [] and macro_columns(rule) == {"cgb10y"}
    df = make_prices(n=600)
    df["pe"] = 10 + 5 * np.sin(np.arange(600) / 40)
    macro = pd.DataFrame({"cgb10y": 2.0, "term_spread": 50.0, "credit_spread": 50.0}, index=df.index)
    res = run_backtest(attach({"A": df}, {"cgb10y"}, macro), *strategies.resolve({"kind": "rule", "rule": rule}),
                       BrokerConfig())
    o = res.orders
    assert len(o) and o["time"].min() >= df.index[244]           # 至少一年有效值才有百分位
    for t in o[o["side"] == "buy"]["time"]:                      # 买在市盈率低（利差高）的时候
        assert df["pe"].loc[:t].iloc[-2] < 10
    for key in ("erp", "erp_pct"):
        with pytest.raises(ValueError):
            _line_expr(make_ind(key))
