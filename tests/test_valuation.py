"""ETF / 指数附带的指数估值（data/valuation.py）与估值百分位指标"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from simplequant import strategies
from simplequant.data import valuation
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.rules import make_ind, validate, required_columns


def _monthly_val(index):
    """月末一个点（再加最后一天），模拟乐咕乐股的数据"""
    days = index[index.to_series().groupby(index.to_period("M")).transform("max") == index].union(index[-1:])
    rng = np.random.default_rng(1)
    return pd.DataFrame({"pe": 12 + rng.normal(0, 2, len(days)).cumsum() * 0.2,
                         "pb": 1.5 + rng.normal(0, 0.1, len(days))}, index=days)


def test_attach_uses_latest_published_value():
    df = make_prices(n=120)
    val = _monthly_val(df.index)
    out = valuation.attach(df, val)
    assert list(out.columns[-2:]) == ["pe", "pb"]
    for day in df.index[::7]:
        last = val.loc[:day]
        expect = last["pe"].iloc[-1] if len(last) else np.nan
        assert (np.isnan(expect) and np.isnan(out.at[day, "pe"])) or out.at[day, "pe"] == expect
    assert out["pe"].isna().sum() == (df.index < val.index[0]).sum()       # 第一次公布之前为空
    assert out[["open", "close"]].equals(df[["open", "close"]])


def test_index_of_and_try_attach(monkeypatch):
    assert valuation.index_of("510300", "etf") == "000300" and valuation.index_of("000905", "index") == "000905"
    assert valuation.index_of("518880", "etf") is None and valuation.index_of("600519", "stock") is None
    df = make_prices(n=60)
    monkeypatch.setattr(valuation, "index_valuation", lambda code: _monthly_val(df.index))
    out = valuation.try_attach(df, "510300", "etf")
    assert out.attrs["valuation"] == "000300" and "pe" in out
    assert "pe" not in valuation.try_attach(df, "518880", "etf")

    def boom(code):
        raise ConnectionError("down")
    monkeypatch.setattr(valuation, "index_valuation", boom)
    assert valuation.try_attach(df, "510300", "etf") is df                 # 接口失败不影响行情


def test_akshare_fetch_attaches_valuation(monkeypatch):
    from simplequant.data import akshare_src
    raw = make_prices(n=80).reset_index()
    monkeypatch.setattr(akshare_src, "eastmoney", lambda *a, **k: raw)
    monkeypatch.setattr(valuation, "index_valuation", lambda code: _monthly_val(pd.DatetimeIndex(raw["datetime"])))
    df = akshare_src.AKShareSource().fetch("510300", "2021-01-01", "2021-12-31", asset="etf")
    assert df.attrs["valuation"] == "000300" and df.attrs["provider"] == "eastmoney" and df["pe"].notna().any()


def _pct_reference(s: pd.Series, window: int, min_count: int) -> pd.Series:
    out = []
    for i in range(len(s)):
        cur, vals = s.iloc[i], s.iloc[max(0, i - window + 1):i + 1].dropna()
        out.append(np.nan if np.isnan(cur) or len(vals) < min_count
                   else ((vals < cur).sum() + (vals == cur).sum() / 2) / len(vals) * 100)
    return pd.Series(out, index=s.index)


def _val_prices(n=800, seed=0):
    df = make_prices(n=n, seed=seed)
    return valuation.attach(df, _monthly_val(df.index))


def _first_buy_rule(threshold):
    return {"buy": {"logic": "all", "conditions": [
                {"left": make_ind("pe_pct", years=1), "op": "<", "right": {"value": threshold}}]},
            "sell": {"logic": "any", "conditions": []}, "position_pct": 90}


@pytest.mark.parametrize("threshold", [15, 40, 80])
def test_pe_percentile_rule_matches_reference(threshold):
    """规则「市盈率百分位 < x」第一次满足的那天，与按定义逐日算出的百分位一致（成交在下一根 K 线）"""
    df = _val_prices()
    ref = _pct_reference(df["pe"], 244, 244)
    res = run_backtest({"A": df}, *strategies.resolve({"kind": "rule", "rule": _first_buy_rule(threshold)}),
                       BrokerConfig())
    signal = ref[ref < threshold].index[0]
    assert pd.Timestamp(res.orders["time"].iloc[0]).normalize() == df.index[df.index.get_loc(signal) + 1]


def test_pe_percentile_schema_and_export():
    from dataclasses import asdict
    from test_export import exec_script
    from simplequant.export import single_asset_script
    rule = {"buy": {"logic": "all", "conditions": [{"left": make_ind("pe_pct", years=1), "op": "<", "right": {"value": 30}}]},
            "sell": {"logic": "any", "conditions": [{"left": make_ind("pb_pct", years=2), "op": ">", "right": {"value": 70}}]},
            "position_pct": 90}
    assert validate(rule) == [] and required_columns(rule) == {"pe", "pb"}
    prices = {"A": _val_prices(n=900)}
    spec = {"kind": "rule", "rule": rule}
    broker = BrokerConfig()
    code = single_asset_script(spec, [{"name": "A", "source": "csv", "path": "A.csv"}], asdict(broker))
    assert "class Percentile" in code and "Percentile(d.pe, window=244, min_count=244)" in code
    ns = exec_script(code)
    strat = ns["run"](prices)
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    ours = run_backtest(prices, *strategies.resolve(spec), broker).orders
    assert len(ours) > 1
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.reset_index(drop=True))
    with pytest.raises(ValueError):                    # ETF 的指数估值来自乐咕乐股，导出的脚本里没有
        single_asset_script(spec, [{"name": "510300", "source": "akshare", "symbol": "510300", "asset": "etf"}],
                            asdict(broker))


def test_percentile_rule_warms_up_with_data_before_range(monkeypatch):
    """规则用到估值百分位：区间开始前的数据只预热，先说明一句；区间前的估值不足回看年数时再提示（待办 30）"""
    import datetime as dt
    from types import SimpleNamespace
    import gui.components as C
    cal = pd.bdate_range("2012-01-02", "2026-09-30")
    df = pd.DataFrame({"close": 1.0, "pe": np.where(cal >= "2013-01-01", 12.0, np.nan)}, index=cal)
    monkeypatch.setattr(C.library, "load", lambda i: df)
    by_id = {"a": SimpleNamespace(name="510300", symbol="510300", source="akshare", adjust="hfq", freq="1d",
                                  start="2012-01-02", end="2026-09-30", extra={})}
    pick = SimpleNamespace(ids=["a"], start=dt.date(2020, 1, 2), end=dt.date(2026, 9, 30),
                           date_range=(dt.date(2020, 1, 2), dt.date(2026, 9, 30)))
    rule = {"buy": {"logic": "all", "conditions": [{"left": make_ind("pe_pct", years=10), "op": "<", "right": {"value": 20}}]},
            "sell": {"logic": "all", "conditions": [{"left": make_ind("pe"), "op": ">", "right": {"value": 30}}]}}
    spec = {"kind": "rule", "rule": rule}
    assert C.warm_start(pick, spec) == pd.Timestamp("2020-01-02")
    prices = C.load_prices(by_id, pick, warm=True)
    assert prices["510300"].index[0] == cal[0] and prices["510300"].index[-1] == pd.Timestamp("2026-09-30")
    (info, k1), (short, k2) = C.percentile_warnings(by_id, pick, spec)
    assert (k1, k2) == ("info", "warning") and "2020-01-02" in info
    assert "10 年" in short and "约 7.5 年" in short          # 工作日日历每年约 261 天，按 244 折算
    rule["buy"]["conditions"][0]["left"] = make_ind("pe_pct", years=5)     # 区间前的估值够 5 年：只有说明
    assert [k for _, k in C.percentile_warnings(by_id, pick, spec)] == ["info"]
    pick.start = dt.date(2012, 6, 1)                           # 区间前没有估值数据：只有说明
    assert [k for _, k in C.percentile_warnings(by_id, pick, spec)] == ["info"]
    pick.start = dt.date(2012, 1, 2)                           # 区间前没有任何数据：不提示
    assert C.percentile_warnings(by_id, pick, spec) == []
    rule["buy"]["conditions"][0]["left"] = make_ind("pe")      # 没用百分位：照常截到区间内，不提示
    assert C.warm_start(pick, spec) is None and C.percentile_warnings(by_id, pick, spec) == []
    assert C.percentile_warnings(by_id, pick, {"kind": "template"}) == []
