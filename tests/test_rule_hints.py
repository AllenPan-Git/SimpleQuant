"""AI 生成规则后的提示：状态 / 事件、说法与比较方式对不上、多次生成不一致"""

from simplequant.rules import make_ind
from simplequant.rules.hints import condition_notes, wording_hints, rule_signature


def rule(op, right=None):
    right = right or make_ind("sma", period=20)
    return {"buy": {"logic": "all", "conditions": [{"left": make_ind("close"), "op": op, "right": right}]},
            "sell": {"logic": "any", "conditions": [{"left": make_ind("pnl_pct"), "op": "<", "right": {"value": -5}}]},
            "position_pct": 95}


def test_condition_kinds():
    notes = condition_notes(rule("cross_above"), "zh")
    assert [(n["side"], n["kind"]) for n in notes] == [("buy", "event"), ("sell", "state")]
    sell_ma = rule(">")
    sell_ma["sell"]["conditions"][0] = {"left": make_ind("close"), "op": "<", "right": make_ind("sma", period=20)}
    assert len(wording_hints(sell_ma, "均线金叉买入，跌破均线卖出")) == 1       # 卖出条件也算
    assert "上穿" in notes[0]["text"]


def test_wording_hints():
    assert len(wording_hints(rule(">"), "收盘价上穿20日均线买入")) == 1            # 说上穿，规则是大于
    assert wording_hints(rule("cross_above"), "收盘价上穿20日均线买入") == []
    assert len(wording_hints(rule("cross_above"), "收盘价站上20日均线买入")) == 1   # 说站上，规则是上穿
    assert wording_hints(rule(">"), "收盘价站上20日均线买入") == []
    assert wording_hints(rule(">", {"value": 70}), "RSI 金叉") == []               # 和固定数值比：不提示
    assert wording_hints(rule(">"), "突破20日新高买入") == []                        # 突破新高本来就是大于
    assert len(wording_hints(rule(">"), "Buy on a golden cross")) == 1


def test_rule_signature():
    a, b = rule(">"), rule(">")
    b["buy"]["conditions"][0]["_id"] = "x1"                                              # 界面加的 id 不算
    assert rule_signature(a) == rule_signature(b)
    assert rule_signature(a) != rule_signature(rule("cross_above"))
    c = rule(">")
    c["position_pct"] = 50
    assert rule_signature(a) != rule_signature(c)
