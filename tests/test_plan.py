"""回测方案文件（simplequant/plan.py）"""

import json

import pytest

from simplequant import plan as P
from simplequant.data.library import DatasetMeta
from simplequant.engine import BrokerConfig

RULE = {"kind": "rule", "rule": {
    "buy": {"logic": "all", "conditions": [{"left": {"ind": "rsi", "params": {"period": 14}}, "op": "<", "right": {"value": 30}}]},
    "sell": {"logic": "any", "conditions": [{"left": {"ind": "rsi", "params": {"period": 14}}, "op": ">", "right": {"value": 70}}]}}}


def meta(sym="510300", start="2020-01-02", end="2026-09-30", provider="eastmoney", **kw):
    return DatasetMeta(id=f"akshare_{sym}_{start}_{end}_{provider}", name=f"{sym} 测试", symbol=sym, source="akshare",
                       freq="1d", adjust="hfq", start=start, end=end, extra={"asset": "etf", "provider": provider}, **kw)


def test_round_trip_keeps_everything():
    broker = BrokerConfig(cash=100000, commission=0.00005, min_commission=0, slippage=0.0005, price_limit=False)
    plan = P.make_plan("RSI 超买超卖", {"name": "x", **RULE}, [meta(), meta("510500")], "2020-01-02", "2026-09-29",
                       broker, "batch", {"total_return": 0.3758, "trades": 7, "sharpe": 0.5})
    back = P.loads(P.dumps(plan).encode("utf-8"))
    assert back["strategy"] == RULE                                    # 名称不进策略描述
    assert back["assets"][0] == {"name": "510300 测试", "symbol": "510300", "source": "akshare", "freq": "1d",
                                 "adjust": "hfq", "asset": "etf", "provider": "eastmoney"}
    assert P.broker_of(back) == broker
    assert back["mode"] == "batch" and back["range"] == ["2020-01-02", "2026-09-29"]
    assert back["result"] == {"total_return": 0.3758, "trades": 7}


def test_code_strategy_refused_both_ways():
    with pytest.raises(P.PlanError) as e:
        P.make_plan("x", {"kind": "code", "code": "print(1)"}, [meta()], "2020-01-02", "2021-01-01", BrokerConfig())
    assert e.value.key == "plan.err_code"
    plan = P.make_plan("x", RULE, [meta()], "2020-01-02", "2021-01-01", BrokerConfig())
    plan["strategy"] = {"kind": "code", "code": "import os"}
    with pytest.raises(P.PlanError) as e:
        P.loads(P.dumps(plan))
    assert e.value.key == "plan.err_code"


@pytest.mark.parametrize("text", [b"\xff\xfe", "not json", json.dumps({"format": "other"}),
                                  json.dumps({"format": P.FORMAT, "version": 1, "strategy": RULE, "assets": [],
                                              "range": ["2020-01-01", "2021-01-01"]})])
def test_bad_files(text):
    with pytest.raises(P.PlanError):
        P.loads(text)


def test_newer_version_and_unknown_fields():
    plan = P.make_plan("x", RULE, [meta()], "2020-01-02", "2021-01-01", BrokerConfig())
    plan["broker"]["future_option"] = 1                                # 以后版本新增的费率项：忽略
    assert "future_option" not in P.loads(P.dumps(plan))["broker"]
    plan["version"] = P.VERSION + 1
    with pytest.raises(P.PlanError) as e:
        P.loads(P.dumps(plan))
    assert e.value.key == "plan.err_newer"


def test_invalid_rule_refused():
    plan = P.make_plan("x", RULE, [meta()], "2020-01-02", "2021-01-01", BrokerConfig())
    plan["strategy"]["rule"]["buy"]["conditions"][0]["op"] = "rm -rf"
    with pytest.raises(P.PlanError) as e:
        P.loads(P.dumps(plan))
    assert e.value.key == "plan.err_rule"


def test_check_assets_matches_local_data():
    plan = P.loads(P.dumps(P.make_plan("x", RULE, [meta(), meta("510500"), meta("159915")],
                                       "2020-01-02", "2026-09-29", BrokerConfig())))
    plan["assets"][2]["source"] = "csv"
    local = [meta(start="2021-01-04"),                                  # 区间不够
             meta(provider="sina"), meta(),                             # 优先用行情来源相同的
             meta("510500", provider="sina")]
    rows = P.check_assets(plan, local)
    assert [r["status"] for r in rows] == ["ok", "ok", "manual"]
    assert rows[0]["meta"].extra["provider"] == "eastmoney"
    assert P.provider_differs(rows[1]["asset"], rows[1]["meta"])
    assert P.check_assets(plan, [])[0]["status"] == "download"


def test_compare():
    assert P.compare(None, {"total_return": 0.1}) is None
    assert P.compare({"total_return": 0.3758, "trades": 7}, {"total_return": 0.37581, "trades": 7}) == "same"
    assert P.compare({"total_return": 0.3758, "trades": 7}, {"total_return": 0.3758, "trades": 8}) == "differs"
    assert P.compare({"total_return": 0.3758}, {"total_return": 0.40, "trades": 7}) == "differs"


# ---------------- 多因子选股方案 ----------------
SEL = {"kind": "selection", "universe": "hs300", "factors": [{"key": "ep", "weight": 1.0, "direction": 1}],
       "top_n": 10, "rebalance": "monthly", "filters": {"exclude_st": True, "min_list_days": 250},
       "position_pct": 95, "weighting": "manual", "ic_lookback": 252,
       "neutralize": {"industry": False, "size": False}}
DY = 'def factor(p):\n    return p["div_cash"].rolling(250, min_periods=1).sum() / p["raw_close"]\n'


@pytest.fixture
def factor_dir(tmp_path, monkeypatch):
    from simplequant.stocks import custom_factors as cf
    monkeypatch.setattr(cf, "FACTOR_DIR", tmp_path / "factors")
    cf.load_all()
    yield cf
    monkeypatch.undo()
    cf.load_all()


def test_selection_round_trip_with_custom_factor(factor_dir):
    key = factor_dir.save_factor("简单股息率", DY, desc="过去一年")
    spec = {**SEL, "factors": SEL["factors"] + [{"key": key, "weight": 2.0, "direction": 1}], "name": "x"}
    broker = BrokerConfig(cash=1_000_000, commission=0.00025, stamp_duty=0.0005)
    plan = P.make_selection_plan("沪深300 低市盈率", spec, "2021-01-04", "2026-09-29", "2019-12-01", broker,
                                 {"total_return": 0.5477, "trades": 120, "sharpe": 0.4})
    assert plan["version"] == P.SELECTION_VERSION > P.TIMING_VERSION        # 旧版软件会提示更新，而不是读错
    back = P.loads(P.dumps(plan))
    assert P.is_selection(back) and back["strategy"] == {k: v for k, v in spec.items() if k != "name"}
    assert back["custom_factors"] == {key: {"name": "简单股息率", "code": DY, "direction": 1, "desc": "过去一年"}}
    assert back["warmup_start"] == "2019-12-01" and back["range"] == ["2021-01-04", "2026-09-29"]
    assert P.broker_of(back) == broker and back["result"] == {"total_return": 0.5477, "trades": 120}
    assert P.needs(back) == {"fin": False, "div": True, "industry": False}     # 自定义因子用了分红字段

    factor_dir.delete_factor(key)
    with pytest.raises(P.PlanError) as e:                                   # 因子已删除：不能导出
        P.make_selection_plan("x", spec, "2021-01-04", "2026-09-29", "2020-01-02", broker)
    assert e.value.key == "plan.err_factor_missing"


@pytest.mark.parametrize("change, key", [
    (lambda p: p["strategy"].update(universe="nasdaq"), "plan.err_universe"),
    (lambda p: p["strategy"]["factors"].append({"key": "future_factor"}), "plan.err_factor"),
    (lambda p: p["strategy"]["factors"].append({"key": "u_12345678"}), "plan.err_format"),       # 没有附带代码
    (lambda p: (p["strategy"]["factors"].append({"key": "u_12345678"}),
                p["custom_factors"].update(u_12345678={"name": "坏", "code": "def factor(p) return 1"})),
     "plan.err_factor_code"),
])
def test_selection_plan_refused(change, key):
    plan = P.make_selection_plan("x", SEL, "2021-01-04", "2022-01-04", "2020-01-02", BrokerConfig())
    change(plan)
    with pytest.raises(P.PlanError) as e:
        P.loads(P.dumps(plan))
    assert e.value.key == key


def test_selection_needs():
    plan = P.make_selection_plan("x", {**SEL, "dividend": "cash", "neutralize": {"industry": True, "size": True}},
                                 "2021-01-04", "2022-01-04", "2020-01-02", BrokerConfig())
    assert P.needs(plan) == {"fin": True, "div": True, "industry": True}
    plan = P.make_selection_plan("x", {**SEL, "factors": [{"key": "roe", "weight": 1, "direction": 1}]},
                                 "2021-01-04", "2022-01-04", "2020-01-02", BrokerConfig())
    assert P.needs(plan) == {"fin": True, "div": False, "industry": False}


def test_selection_missing(tmp_path):
    import pandas as pd
    from simplequant.stocks import StockStore
    st = StockStore(root=tmp_path)
    plan = P.loads(P.dumps(P.make_selection_plan("x", {**SEL, "dividend": "cash"}, "2021-01-04", "2022-01-04",
                                                 "2020-01-02", BrokerConfig())))
    assert P.selection_missing(plan, st) == ["data"]
    pd.DataFrame({"date": pd.to_datetime(["2020-01-02"]), "code": ["sh.600000"], "name": ["浦发银行"]}) \
        .to_parquet(tmp_path / "universe" / "hs300.parquet")
    idx = pd.DataFrame({"open": 1.0, "close": 1.0}, index=pd.bdate_range("2020-06-01", "2022-01-04"))
    idx.to_parquet(tmp_path / "index" / "sh.000300.parquet")
    assert P.selection_missing(plan, st) == ["range", "div"]          # 指数从 2020-06 起，不够预热；没有分红
    idx = pd.DataFrame({"open": 1.0, "close": 1.0}, index=pd.bdate_range("2020-01-02", "2022-01-04"))
    idx.to_parquet(tmp_path / "index" / "sh.000300.parquet")
    (tmp_path / "div").mkdir()
    pd.DataFrame({"x": [1]}).to_parquet(st._div_path("sh.600000"))
    assert P.selection_missing(plan, st) == []


def test_update_index_keeps_earlier_days(tmp_path, monkeypatch):
    """下载较短的区间不会把指数（交易日历）更早的部分删掉（录屏环境的沪深300 指数曾只剩 2022 年起）"""
    import pandas as pd
    from simplequant.stocks import StockStore, store as store_mod

    def fake_query(fn, code, fields, start_date, end_date, frequency):
        days = pd.bdate_range(start_date, end_date)
        return pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "open": "1", "close": "2"})
    monkeypatch.setattr(store_mod, "_query", fake_query)
    st = StockStore(root=tmp_path, login=lambda: None)
    st.update_index("sh.000300", "2019-01-01", "2023-12-31")
    st.update_index("sh.000300", "2022-01-01", "2024-06-30")
    idx = st.load_index("sh.000300")
    assert idx.index[0] == pd.Timestamp("2019-01-01") and idx.index[-1] == pd.Timestamp("2024-06-28")
    assert idx.index.is_unique and idx.index.is_monotonic_increasing
