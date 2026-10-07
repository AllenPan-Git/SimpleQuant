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
