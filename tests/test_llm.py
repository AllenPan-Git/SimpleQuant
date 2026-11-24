"""
自然语言策略与大模型接口
- 不联网：Claude / OpenAI 通过模拟 HTTP 传输走真实 SDK 调用路径；LiteLLM 用其 mock_response
- 覆盖：JSON Schema 是否符合严格模式要求、JSON 提取、输出转规则、校验失败后自动修正、各接口请求格式
"""

import json

import httpx
import httpx2
import pytest

from conftest import make_prices
from simplequant.engine import run_backtest
from simplequant.llm import (LLMConfig, PRESETS, LLM_SCHEMA, translate, extract_json, get_provider, LLMError,
                             save_config, load_config, system_prompt)
from simplequant.llm.nl import to_rule
from simplequant.llm.providers import AnthropicProvider, OpenAIProvider, LiteLLMProvider
from simplequant.rules import INDICATORS, validate
from simplequant.strategies import resolve


def operand(ind=None, value=None, line="", **params):
    if value is not None:
        return {"kind": "value", "ind": "close", "line": "", "params": [], "value": value}
    return {"kind": "indicator", "ind": ind, "line": line, "params": [{"name": k, "value": v} for k, v in params.items()],
            "value": 0}


def llm_output(buy, sell=(), logic_sell="any", pct=95, unsupported=()):
    return {"name": "测试", "understood": "……", "symbols": ["510300", "abc"], "unsupported": list(unsupported),
            "buy": {"logic": "all", "conditions": list(buy)}, "sell": {"logic": logic_sell, "conditions": list(sell)},
            "position_pct": pct}


MACD_STOP = llm_output(
    buy=[{"left": operand("macd", line="dif"), "op": "cross_above", "right": operand("macd", line="dea")},
         {"left": operand("rsi", period=14), "op": "<", "right": operand(value=70)}],
    sell=[{"left": operand("pnl_pct"), "op": "<", "right": operand(value=-8)},
          {"left": operand("macd", line="dif"), "op": "cross_below", "right": operand("macd", line="dea")}],
    pct=80, unsupported=["ROE 大于 15%"])


class FakeProvider:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, system, messages, schema=None):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "schema": schema})
        return self.replies.pop(0)


# ---------------- Schema / 转换 ----------------
def _walk(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk(v)


def test_schema_is_strict_mode_compatible():
    """Claude / OpenAI 严格模式要求：每个对象都 additionalProperties=false 且所有属性必填"""
    objs = list(_walk(LLM_SCHEMA))
    assert len(objs) >= 5
    for o in objs:
        assert o["additionalProperties"] is False
        assert set(o["required"]) == set(o["properties"])
    assert set(LLM_SCHEMA["properties"]["buy"]["properties"]["conditions"]["items"]["properties"]["left"]
               ["properties"]["ind"]["enum"]) == set(INDICATORS)


def test_system_prompt_lists_every_indicator():
    text = system_prompt("zh")
    for k in INDICATORS:
        assert f"- {k}:" in text
    assert "Simplified Chinese" in text and "English" in system_prompt("en")


@pytest.mark.parametrize("reply", [
    '{"a": 1}', '```json\n{"a": 1}\n```', '好的，结果如下：{"a": 1} 希望有帮助', '```\n{"a": 1}\n```'])
def test_extract_json(reply):
    assert extract_json(reply) == {"a": 1}


def test_extract_json_errors():
    with pytest.raises(LLMError):
        extract_json("抱歉，我无法回答")
    with pytest.raises(LLMError):
        extract_json("{not json}")


def test_to_rule_conversion():
    rule = to_rule(MACD_STOP)
    assert validate(rule) == []
    buy = rule["buy"]["conditions"]
    assert buy[0]["left"] == {"ind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}, "line": "dif"}
    assert buy[1]["right"] == {"value": 70.0}
    assert rule["position_pct"] == 80
    # 参数：未知参数丢弃、整数参数取整、非法输出线回到默认
    r2 = to_rule(llm_output([{"left": operand("sma", period=10.4, bogus=3), "op": ">", "right": operand("kdj", line="zzz")}]))
    c = r2["buy"]["conditions"][0]
    assert c["left"]["params"] == {"period": 10}
    assert c["right"]["line"] == "k"


def test_translate_happy_path_and_backtest():
    fake = FakeProvider([json.dumps(MACD_STOP, ensure_ascii=False)])
    res = translate("MACD 金叉且 RSI<70 买入，亏 8% 或死叉卖出，八成仓位，要求 ROE>15%", fake)
    assert res.ok and len(fake.calls) == 1
    assert fake.calls[0]["schema"] is LLM_SCHEMA
    assert res.unsupported == ["ROE 大于 15%"]
    assert res.symbols == ["510300"]                 # 非 6 位数字的代码被过滤
    # 生成的规则能直接回测
    out = run_backtest({"A": make_prices()}, *resolve(res.spec))
    assert out.metrics["final_value"] > 0


def test_translate_repairs_invalid_rule_once():
    bad = llm_output([{"left": operand("pnl_pct"), "op": "cross_above", "right": operand(value=5)}])
    good = llm_output([{"left": operand("close"), "op": ">", "right": operand("sma", period=20)}])
    fake = FakeProvider([json.dumps(bad), json.dumps(good)])
    res = translate("持仓收益上穿 5% 就买", fake)
    assert res.ok and len(fake.calls) == 2
    repair = fake.calls[1]["messages"]
    assert repair[1]["role"] == "assistant" and "failed validation" in repair[2]["content"]


def test_translate_reports_errors_if_still_invalid():
    bad = llm_output([], sell=[{"left": operand("pnl_pct"), "op": "<", "right": operand(value=-5)}])
    fake = FakeProvider([json.dumps(bad), json.dumps(bad)])
    res = translate("亏 5% 卖出", fake)
    assert not res.ok and any("买入条件" in e for e in res.errors) and len(fake.calls) == 2
    assert "Never invent" in fake.calls[1]["messages"][2]["content"]


def test_not_a_strategy_is_not_repaired_into_a_fake_rule():
    """模型返回空规则（不是交易策略）时不要求修正：实测 Gemini 被要求修正后会编造"收盘价 > 0"来通过校验"""
    empty = {**llm_output([], unsupported=["不是交易策略"]), "symbols": []}
    fake = FakeProvider([json.dumps(empty, ensure_ascii=False)])
    res = translate("今天天气怎么样？", fake)
    assert len(fake.calls) == 1
    assert not res.ok and res.unsupported == ["不是交易策略"]


# ---------------- 配置 ----------------
def test_config_roundtrip(tmp_path):
    cfg = LLMConfig.from_preset("deepseek", api_key="sk-test")
    path = save_config(cfg, tmp_path / "llm.json")
    back = load_config(path)
    assert back == cfg and back.base_url == "https://api.deepseek.com" and back.provider == "openai"
    assert load_config(tmp_path / "missing.json") is None


def test_every_preset_builds_a_provider():
    for key in PRESETS:
        prov = get_provider(LLMConfig.from_preset(key))
        assert prov.cfg.preset == key


# ---------------- Claude（真实 SDK + 模拟 HTTP） ----------------
def anthropic_transport(seen, text, stop="end_turn", status=200):
    def handler(request):
        seen.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    import anthropic
    return anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))


def test_claude_request_uses_structured_output_and_fallback():
    seen = []
    prov = AnthropicProvider(LLMConfig.from_preset("claude", api_key="test"))
    reply = prov.chat("sys", [{"role": "user", "content": "hi"}], LLM_SCHEMA,
                      http_client=anthropic_transport(seen, json.dumps(MACD_STOP)))
    assert extract_json(reply)["name"] == "测试"
    body = seen[0]["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0]["headers"]["anthropic-beta"]


def test_claude_without_fallback_for_proxy_or_other_models():
    for cfg in (LLMConfig.from_preset("claude", base_url="https://proxy.example.com"),
                LLMConfig.from_preset("claude", model="claude-haiku-4-5")):
        kw = AnthropicProvider(cfg).build_request("s", [], LLM_SCHEMA)
        assert "fallbacks" not in kw and "betas" not in kw


def test_claude_retries_without_fallback_on_400():
    """账户不接受 fallbacks 时（400），去掉该参数再请求一次"""
    import anthropic
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    out = AnthropicProvider(LLMConfig.from_preset("claude", api_key="t")).chat("s", [{"role": "user", "content": "x"}], None, http_client=http)
    assert out == "OK" and "fallbacks" in seen[0] and "fallbacks" not in seen[-1]


def test_claude_refusal_and_json_mode_hint():
    prov = AnthropicProvider(LLMConfig.from_preset("claude", api_key="t"))
    with pytest.raises(LLMError):
        prov.chat("s", [{"role": "user", "content": "x"}], None, http_client=anthropic_transport([], "", stop="refusal"))
    kw = AnthropicProvider(LLMConfig.from_preset("claude", json_mode="prompt")).build_request("s", [], LLM_SCHEMA)
    assert "output_config" not in kw and "JSON Schema" in kw["system"]


# ---------------- OpenAI 兼容（真实 SDK + 模拟 HTTP） ----------------
def openai_transport(seen, text):
    def handler(request):
        seen.append({"url": str(request.url), "body": json.loads(request.content), "auth": request.headers.get("authorization")})
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("preset,mode", [("openai", "schema"), ("deepseek", "json_object"), ("ollama", "json_object"),
                                         ("compatible", "prompt"), ("gemini", "json_object")])
def test_openai_compatible_request_format(preset, mode):
    seen = []
    cfg = LLMConfig.from_preset(preset, json_mode=mode, api_key="" if preset == "ollama" else "sk-x")
    cfg.model = cfg.model or "some-model"
    cfg.base_url = cfg.base_url or ("https://api.example.com/v1" if preset == "compatible" else "")
    reply = OpenAIProvider(cfg).chat("sys", [{"role": "user", "content": "hi"}], LLM_SCHEMA,
                                     http_client=openai_transport(seen, "```json\n" + json.dumps(MACD_STOP) + "\n```"))
    assert extract_json(reply)["position_pct"] == 80
    body = seen[0]["body"]
    assert body["messages"][0]["role"] == "system"
    if mode == "schema":
        assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"]
    elif mode == "json_object":
        assert body["response_format"] == {"type": "json_object"} and "JSON Schema" in body["messages"][0]["content"]
    else:
        assert "response_format" not in body and "JSON Schema" in body["messages"][0]["content"]
    if cfg.base_url:
        assert seen[0]["url"].startswith(cfg.base_url.rstrip("/"))
    if preset == "ollama":
        assert seen[0]["auth"] == "Bearer ollama"


def test_gemini_reads_its_own_key_env(monkeypatch):
    """Gemini 预设：key 留空时读 GEMINI_API_KEY，请求发到 Google 的 OpenAI 兼容地址"""
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []
    cfg = LLMConfig.from_preset("gemini")
    reply = OpenAIProvider(cfg).chat("sys", [{"role": "user", "content": "hi"}], None,
                                     http_client=openai_transport(seen, "OK"))
    assert reply == "OK"
    assert seen[0]["auth"] == "Bearer gm-key"
    assert seen[0]["url"].startswith("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")


def test_translate_end_to_end_through_openai_sdk():
    seen = []
    http = openai_transport(seen, json.dumps(MACD_STOP))
    prov = OpenAIProvider(LLMConfig.from_preset("deepseek", api_key="sk-x"))
    prov_chat = prov.chat
    prov.chat = lambda s, m, schema=None: prov_chat(s, m, schema, http_client=http)
    res = translate("MACD 金叉买入", prov)
    assert res.ok and res.spec["rule"]["position_pct"] == 80


# ---------------- 自然语言 → 多因子选股 ----------------
def sel_output(**kw):
    base = {"name": "低估值小盘", "understood": "……", "unsupported": ["股息率"], "universe": "zz500",
            "factors": [{"key": "ep", "direction": "higher", "weight": 2}, {"key": "size", "direction": "lower", "weight": 1}],
            "weighting": "manual", "top_n": 20, "rebalance": "days", "rebalance_days": 10, "exclude_st": True,
            "min_list_days": 365, "neutralize_industry": True, "neutralize_size": False, "position_pct": 90}
    return {**base, **kw}


def test_selection_spec_new_fields_roundtrip():
    from simplequant.llm import nl
    out = {
        "name": "?????", "understood": "...", "unsupported": [], "universe": "hs300",
        "factors": [{"key": "ep", "direction": "higher", "weight": 1}],
        "weighting": "manual", "top_n": 10, "keep_n": 15, "rebalance": "monthly",
        "rebalance_days": 20, "skip_months": [1, 4], "drawdown_stop": 15.0,
        "exclude_st": True, "min_list_days": 250, "neutralize_industry": False,
        "neutralize_size": False, "position_pct": 95, "max_price": 30.0,
        "mcap_rank_min": 50, "mcap_rank_max": 300, "min_mcap_pct": 10.0,
        "max_debt_ratio": 60.0, "min_current_ratio": 1.5, "min_cfo_np": 0.8
    }
    spec = nl.to_selection_spec(out)
    assert spec["keep_n"] == 15
    assert spec["skip_months"] == [1, 4]
    assert spec["drawdown_stop"] == 15.0
    assert spec["filters"]["max_price"] == 30.0
    assert spec["filters"]["mcap_rank_min"] == 50.0
    assert spec["filters"]["min_mcap_pct"] == 10.0
    assert spec["filters"]["max_debt_ratio"] == 60.0
    from_spec = nl.from_selection_spec(spec)
    assert from_spec["keep_n"] == 15
    assert from_spec["max_price"] == 30.0
    assert from_spec["min_mcap_pct"] == 10.0


def test_selection_schema_is_strict_mode_compatible():
    from simplequant.llm import selection_schema
    from simplequant.stocks import FACTORS
    s = selection_schema()
    for o in _walk(s):
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])
    assert set(s["properties"]["factors"]["items"]["properties"]["key"]["enum"]) == set(FACTORS)


def test_to_selection_spec_and_backtestable():
    from simplequant.llm import to_selection_spec, validate_selection
    spec = to_selection_spec(sel_output())
    assert spec == {"kind": "selection", "universe": "zz500",
                    "factors": [{"key": "ep", "weight": 2.0, "direction": 1}, {"key": "size", "weight": 1.0, "direction": -1}],
                    "top_n": 20, "rebalance": 10, "filters": {"exclude_st": True, "min_list_days": 365},
                    "position_pct": 90.0, "weighting": "manual", "neutralize": {"industry": True, "size": False}}
    assert validate_selection(spec) == []
    # 未知因子被丢弃；调仓"monthly"不受 rebalance_days 影响
    spec2 = to_selection_spec(sel_output(factors=[{"key": "dividend", "direction": "higher", "weight": 1}],
                                         rebalance="monthly"))
    assert spec2["factors"] == [] and spec2["rebalance"] == "monthly"
    assert validate_selection(spec2, "en") == ["At least one factor is required"]


def test_translate_selection_with_repair_and_runs():
    from simplequant.llm import translate_selection
    from test_stocks import make_panel
    from simplequant.stocks import run_selection
    bad = sel_output(universe="hs300", rebalance="monthly", top_n=500)    # 只数超过 100 → 要求修正
    good = sel_output(universe="hs300", rebalance="monthly",
                      factors=[{"key": "ep", "direction": "higher", "weight": 1},
                               {"key": "ret20", "direction": "lower", "weight": 1}], neutralize_industry=False)
    fake = FakeProvider([json.dumps(bad), json.dumps(good)])
    res = translate_selection("每月选沪深300里便宜、最近跌得多的20只，还要高股息", fake)
    assert res.ok and len(fake.calls) == 2 and res.unsupported == ["股息率"]
    assert "ep" in fake.calls[0]["system"] and fake.calls[0]["schema"]["properties"]["universe"]["enum"]
    out = run_selection(make_panel(), res.spec)
    assert out.metrics["final_value"] > 0


def test_selection_prompt_lists_every_factor():
    from simplequant.llm.nl import selection_system_prompt
    from simplequant.stocks import FACTORS
    text = selection_system_prompt("en")
    assert all(f"- {k}:" in text for k in FACTORS)


def test_from_selection_spec_roundtrip():
    from simplequant.llm import to_selection_spec, from_selection_spec
    from simplequant.llm.nl import selection_schema
    spec = to_selection_spec(sel_output())
    out = from_selection_spec(spec)
    assert set(out) == set(selection_schema()["properties"]) - {"name", "understood", "unsupported"}
    assert to_selection_spec(out) == spec
    gone = {**spec, "factors": spec["factors"] + [{"key": "u_deadbeef", "weight": 1.0, "direction": 1}]}
    assert from_selection_spec(gone)["factors"] == out["factors"]          # 已删除的自定义因子不发给模型


def test_translate_selection_modifies_base():
    from simplequant.llm import translate_selection
    base = {"kind": "selection", "universe": "zz500",
            "factors": [{"key": "ep", "weight": 2.0, "direction": 1}], "top_n": 20, "rebalance": "monthly",
            "filters": {"exclude_st": True, "min_list_days": 250}, "position_pct": 95, "weighting": "icir",
            "ic_lookback": 120, "neutralize": {"industry": False, "size": False}, "dividend": "cash"}
    reply = sel_output(universe="zz500", rebalance="monthly", top_n=30, min_list_days=250, position_pct=95,
                       neutralize_industry=False, weighting="icir",
                       factors=[{"key": "ep", "direction": "higher", "weight": 2},
                                {"key": "vol60", "direction": "lower", "weight": 1}])
    fake = FakeProvider([json.dumps(reply)])
    res = translate_selection("加个低波动，持有30只", fake, base=base)
    msg = fake.calls[0]["messages"][0]["content"]
    assert msg.startswith("Current strategy:") and msg.endswith("Change request:\n加个低波动，持有30只")
    assert json.loads(msg.split("\n")[1])["factors"] == [{"key": "ep", "direction": "higher", "weight": 2.0}]
    assert "Change request" in fake.calls[0]["system"]
    assert res.ok and res.spec["top_n"] == 30 and [f["key"] for f in res.spec["factors"]] == ["ep", "vol60"]
    assert res.spec["ic_lookback"] == 120 and res.spec["dividend"] == "cash"     # 模型管不到的设置沿用原方案


def test_translate_selection_modify_keeps_cb_filters():
    from simplequant.llm import translate_selection
    base = {"kind": "selection", "universe": "cb", "factors": [{"key": "cb_double_low", "weight": 1.0, "direction": -1}],
            "top_n": 10, "rebalance": "weekly", "position_pct": 95, "weighting": "manual",
            "filters": {"exclude_st": False, "min_list_days": 0, "max_price": None, "min_amount": 500.0},
            "neutralize": {"industry": False, "size": False}}
    reply = sel_output(universe="cb", factors=[{"key": "cb_double_low", "direction": "lower", "weight": 1}],
                       top_n=20, rebalance="weekly", min_list_days=0, exclude_st=False, max_price=0,
                       neutralize_industry=False)
    res = translate_selection("持有20只", FakeProvider([json.dumps(reply)]), base=base)
    assert res.ok and res.spec["top_n"] == 20
    assert res.spec["filters"]["max_price"] is None and res.spec["filters"]["min_amount"] == 500.0
    reply["max_price"] = 120
    res = translate_selection("价格低于120", FakeProvider([json.dumps(reply)]), base=base)
    assert res.spec["filters"]["max_price"] == 120.0


def test_selection_llm_factor_filters_and_exempt_roundtrip():
    """待办 49：AI 选股支持金融业排雷豁免与因子过滤（含数值 0 下限），增量修改保留手动行业筛选"""
    from simplequant.llm import to_selection_spec, from_selection_spec, validate_selection, translate_selection
    raw = sel_output(
        max_debt_ratio=70,
        risk_exempt_fin=True,
        factor_filters=[
            {"key": "ret5", "mode": "pct", "scope": "pool", "has_min": True, "min": 10, "has_max": True, "max": 90},
            {"key": "ep", "mode": "value", "scope": "pool", "has_min": True, "min": 0, "has_max": False, "max": 0},
        ],
    )
    spec = to_selection_spec(raw)
    assert spec["filters"]["risk_exempt_fin"] is True
    assert spec["filters"]["factor_filters"] == [
        {"key": "ret5", "mode": "pct", "min": 10.0, "max": 90.0},
        {"key": "ep", "mode": "value", "min": 0.0},
    ]
    assert validate_selection(spec) == []
    assert to_selection_spec(from_selection_spec(spec)) == spec

    # 非法上下限与跨品种因子报错
    bad = {**spec, "filters": {**spec["filters"], "factor_filters": [{"key": "ret5", "mode": "pct", "min": 80, "max": 20}]}}
    assert any("下限" in e for e in validate_selection(bad, "zh"))

    # 增量修改时，基础策略中手设的行业排除条件保持不丢
    base = {**spec, "filters": {**spec["filters"], "industry": {"mode": "exclude", "items": ["J66货币金融服务"]}}}
    reply = {**from_selection_spec(base), "name": "改", "understood": "持有15只", "unsupported": [], "top_n": 15}
    res = translate_selection("持有15只", FakeProvider([json.dumps(reply)]), base=base)
    assert res.ok and res.spec["top_n"] == 15
    assert res.spec["filters"]["industry"] == {"mode": "exclude", "items": ["J66货币金融服务"]}


def test_explain_result_prompt_has_yardsticks():
    from simplequant.llm import explain_result
    fake = FakeProvider(["- 偏弱"])
    metrics = {"cagr": 0.0321, "sharpe": 0.2042, "benchmark_return": -0.0585, "excess_return": 0.1927}
    assert explain_result(fake, "沪深300 · 每周", metrics, "zh") == "- 偏弱"
    system, msg = fake.calls[0]["system"], fake.calls[0]["messages"][0]["content"]
    assert "Simplified Chinese" in system and fake.calls[0]["schema"] is None
    for s in ("risk-free rate of about 2%", "Sharpe ratio: below 0.5 is weak", "Calmar", "benchmark return is negative",
              "Low volatility or a small drawdown is not good risk control by itself"):
        assert s in system
    assert "沪深300" in msg and '"sharpe": 0.2042' in msg


def test_describe_selection_both_languages():
    from simplequant import strategies
    from simplequant.llm import to_selection_spec
    spec = to_selection_spec(sel_output(weighting="ic"))
    zh, en = strategies.describe(spec, "zh"), strategies.describe(spec, "en")
    assert "中证500" in zh and "按 IC 加权" in zh and "行业" in zh
    assert "CSI 500" in en and "IC-weighted" in en and "every 10 trading days" in en


# ---------------- LiteLLM（mock_response） ----------------
def test_litellm_provider():
    pytest.importorskip("litellm")   # 可选依赖，不在 requirements.txt 里
    cfg = LLMConfig.from_preset("litellm", api_key="sk-x")
    cfg.model = "deepseek/deepseek-chat"
    reply = LiteLLMProvider(cfg).chat("sys", [{"role": "user", "content": "hi"}], LLM_SCHEMA,
                                      mock_response=json.dumps(MACD_STOP))
    assert extract_json(reply)["name"] == "测试"


# ---------------- 设置文件位置（exe 版和其他数据放在一起） ----------------
def test_config_dir_source_vs_frozen(monkeypatch, tmp_path):
    from simplequant.llm import config
    monkeypatch.delenv("SIMPLEQUANT_HOME", raising=False)
    monkeypatch.setattr(config, "FROZEN", False)
    assert config._config_dir() == config.LEGACY_DIR          # 源码版：不放进项目目录，免得 key 被提交
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "SimpleQuant")
    assert config._config_dir() == tmp_path / "SimpleQuant"
    monkeypatch.setenv("SIMPLEQUANT_HOME", str(tmp_path / "custom"))
    assert config._config_dir() == tmp_path / "custom"         # 环境变量优先


def test_migrate_legacy_copies_once(tmp_path):
    from simplequant.llm import config
    legacy, new = tmp_path / "old" / "llm.json", tmp_path / "new" / "llm.json"
    assert not config.migrate_legacy(new, legacy)               # 旧位置也没有：什么都不做
    legacy.parent.mkdir()
    legacy.write_text('{"provider": "openai", "model": "m1"}', encoding="utf-8")
    assert config.migrate_legacy(new, legacy)
    assert config.load_config(new).model == "m1" and legacy.exists()
    legacy.write_text('{"provider": "openai", "model": "m2"}', encoding="utf-8")
    assert not config.migrate_legacy(new, legacy)               # 新位置已有设置：不覆盖
    assert config.load_config(new).model == "m1"


# ---------------- 自然语言 → 自定义因子代码 ----------------
def factor_output(code, direction="higher", unsupported=()):
    return {"name": "放量上涨", "understood": "……", "unsupported": list(unsupported), "code": code,
            "direction": direction, "desc": "上涨日成交额占比"}


UP_VOLUME = ('def factor(p):\n'
             '    """近 20 天上涨日成交额占比"""\n'
             '    up = p["amount"].where(p["close"] > p["close"].shift(1), 0.0)\n'
             '    total = p["amount"].rolling(20, min_periods=15).sum()\n'
             '    return up.rolling(20, min_periods=15).sum() / total.where(total > 0)\n')


def test_factor_schema_is_strict_mode_compatible():
    from simplequant.llm import FACTOR_SCHEMA
    for obj in _walk(FACTOR_SCHEMA):
        assert obj.get("additionalProperties") is False and set(obj["required"]) == set(obj["properties"])


def test_generate_factor_repairs_lookahead_and_runs():
    from simplequant.llm import generate_factor
    from simplequant.stocks import custom_factors
    from test_stocks import make_panel
    bad = factor_output(UP_VOLUME.replace("shift(1)", "shift(-1)"))
    fake = FakeProvider([json.dumps(bad), json.dumps(factor_output(UP_VOLUME, unsupported=["新闻情绪"]))])
    res = generate_factor("近20天放量上涨的程度，再加上新闻情绪", fake)
    assert res.ok and len(fake.calls) == 2 and res.unsupported == ["新闻情绪"] and res.name == "放量上涨"
    assert "shift" in fake.calls[1]["messages"][-1]["content"]           # 偷看未来的错误发回模型修正
    assert "amount (value traded" in fake.calls[0]["system"] and "No look-ahead" in fake.calls[0]["system"]
    assert res.spec == {"kind": "factor", "code": UP_VOLUME, "direction": 1, "desc": "上涨日成交额占比"}
    out = custom_factors.evaluate(res.spec["code"], make_panel())
    assert out.notna().mean().mean() > 0.5


def test_generate_factor_unsupported_and_still_bad():
    from simplequant.llm import generate_factor
    fake = FakeProvider([json.dumps(factor_output("", unsupported=["分析师评级"]))])
    res = generate_factor("分析师评级上调的股票", fake)
    assert len(fake.calls) == 1 and res.ok and res.spec["code"] == ""        # 做不到就不写代码，也不要求修正
    imp = factor_output("import os\ndef factor(p):\n    return p['close']\n", direction="lower")
    fake = FakeProvider([json.dumps(imp)] * 2)
    res = generate_factor("收盘价", fake, lang="en")
    assert len(fake.calls) == 2 and not res.ok and "import" in res.errors[0] and res.spec["direction"] == -1


def test_generate_factor_modifies_code_and_cb_prompt():
    from simplequant.llm import generate_factor
    fake = FakeProvider([json.dumps(factor_output(UP_VOLUME))])
    generate_factor("窗口改成60天", fake, kind="cb", base_code=UP_VOLUME)
    msg = fake.calls[0]["messages"][0]["content"]
    assert msg.startswith("Current code:\n") and msg.endswith("Change request:\n窗口改成60天")
    assert "double_low" in fake.calls[0]["system"] and "roe" not in fake.calls[0]["system"]


# ---------------- AI 解读：交易明细与期末未平仓 ----------------
def test_trade_details_closed_and_open():
    from simplequant.llm import trade_details
    res = run_backtest({"A": make_prices()}, *resolve({"kind": "rule", "rule": to_rule(MACD_STOP)}))
    d = trade_details(res)
    c = d["closed_trades"]
    assert c["count"] == len(res.trades) > 0 and len(c["best"]) <= 3 and len(c["worst"]) <= 3
    assert all(x["pnl_return"] > 0 for x in c["best"]) and all(x["pnl_return"] < 0 for x in c["worst"])
    assert c["closed_pnl_return"] == pytest.approx(res.trades["pnl_net"].sum() / res.metrics["initial_cash"], abs=1e-4)
    # 只买不卖：没有已平仓交易，收益全在期末持仓里
    hold = to_rule(llm_output(buy=[{"left": operand("close"), "op": ">", "right": operand(value=0)}]))
    res = run_backtest({"A": make_prices()}, *resolve({"kind": "rule", "rule": hold}))
    d = trade_details(res)
    assert d["closed_trades"] == {"count": 0}
    op = d["open_positions_at_end"]
    assert op["count"] == 1 and op["positions"][0]["symbol"] == "A"
    assert op["unrealized_return"] == pytest.approx(res.metrics["total_return"], abs=0.01)   # 差的是手续费


def test_trade_details_names_and_explain_message():
    import pandas as pd
    from types import SimpleNamespace
    from simplequant.llm import trade_details, explain_result
    trades = pd.DataFrame({"symbol": ["sh.600000", "sh.600000", "sz.000001"], "open_time": ["2024-01-02"] * 3,
                           "close_time": ["2024-02-01"] * 3, "bars": [20, 10, 5], "pnl": [5000, -2000, 1000],
                           "pnl_net": [5000.0, -2000.0, 1000.0]})
    res = SimpleNamespace(trades=trades, metrics={"initial_cash": 100000, "total_return": -0.06, "limit_blocked": 2},
                          positions=[{"symbol": "sz.000001", "size": 1000, "price": 9.0, "cost": 19.0}])
    d = trade_details(res, {"sh.600000": "浦发银行", "sz.000001": "平安银行"})
    assert d["closed_trades"]["best"][0] == {"symbol": "浦发银行(600000)", "open": "2024-01-02", "close": "2024-02-01",
                                             "bars": 20, "pnl_return": 0.05}
    assert d["closed_trades"]["pnl_by_symbol_top"] == {"浦发银行(600000)": 0.03, "平安银行(000001)": 0.01}
    assert d["open_positions_at_end"]["unrealized_return"] == -0.1
    assert d["open_positions_at_end"]["positions"][0]["price_vs_cost"] == pytest.approx(-0.5263, abs=1e-4)
    assert d["orders_blocked_by_limit_or_suspension"] == 2
    fake = FakeProvider(["- 期末浮亏"])
    explain_result(fake, "择时", {"win_rate": 0.67}, "zh", d)
    msg, system = fake.calls[0]["messages"][0]["content"], fake.calls[0]["system"]
    assert "Trade details" in msg and '"unrealized_return": -0.1' in msg and "平安银行(000001)" in msg
    assert "closed trades only" in system and "positions still open at the end" in system


def test_to_rule_pacing():
    """模型输出的连续天数、冷却期、分批建仓转成规则；不合适的设置被去掉或截到范围内"""
    ind = lambda k, **p: {"kind": "indicator", "ind": k, "line": "", "value": 0,
                          "params": [{"name": n, "value": v} for n, v in p.items()]}
    val = lambda v: {"kind": "value", "ind": "close", "line": "", "params": [], "value": v}
    out = {"buy": {"logic": "all", "conditions": [
               {"left": ind("close"), "op": ">", "right": ind("sma", period=20), "days": 3},
               {"left": ind("close"), "op": "cross_above", "right": ind("sma", period=60), "days": 2}]},
           "sell": {"logic": "any", "conditions": [{"left": ind("pnl_pct"), "op": "<", "right": val(-5), "days": 4}]},
           "position_pct": 90, "cooldown": 999, "scale": {"adds": 2, "when": "drop", "pct": 5}}
    rule = to_rule(out)
    assert [c.get("days", 1) for c in rule["buy"]["conditions"]] == [3, 1]       # 上穿不能连续
    assert "days" not in rule["sell"]["conditions"][0]                              # 持仓收益率不能连续
    assert rule["cooldown"] == 250 and rule["scale"] == {"adds": 2, "when": "drop", "pct": 5.0}
    assert validate(rule) == []
    plain = to_rule({**out, "cooldown": 0, "scale": {"adds": 0, "when": "none", "pct": 0}})
    assert "cooldown" not in plain and "scale" not in plain
    assert to_rule({**out, "scale": {"adds": 1, "when": "signal", "pct": 3}})["scale"] == {"adds": 1, "when": "signal"}
    assert validate(to_rule({**out, "scale": {"adds": 1, "when": "gain", "pct": 0}}))     # 缺幅度：校验报错，发回模型


def test_hosted_device_id_and_token(tmp_path, monkeypatch):
    from simplequant.llm import hosted
    tel = tmp_path / "telemetry.json"
    uid = "3f2b8c1e-9a4d-4b7e-8c2a-1d5e6f7a8b9c"
    tel.write_text(json.dumps({"install_id": uid}), encoding="utf-8")
    cdir = tmp_path / "cfg"
    # 首次优先复用 telemetry 的 install_id 并持久化到 device_id
    assert hosted.device_id(config_dir=cdir, telemetry_path=tel) == uid
    assert (cdir / "device_id").read_text(encoding="utf-8").strip() == uid
    # 删除 telemetry.json 后依然稳定返回同一 ID
    tel.unlink()
    assert hosted.device_id(config_dir=cdir, telemetry_path=tel) == uid

    monkeypatch.setattr(hosted, "device_id", lambda: uid)
    assert hosted.hosted_token("") == f"trial:{uid}"
    assert hosted.hosted_token("  sq-ABC12345  ") == "sq-ABC12345"


def test_hosted_quota_and_pack_download_roundtrip(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from simplequant.llm import hosted

    payload_zip = b"PK\x03\x04dummy-zip-bytes" * 100

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/v1/ai/quota":
                auth = self.headers.get("Authorization", "")
                if auth == "Bearer sq-BAD":
                    body = json.dumps({"error": {"code": "invalid_voucher", "message": "bad"}}).encode()
                    self.send_response(401)
                else:
                    body = json.dumps({"mode": "trial", "ai_total": 5, "ai_left": 3, "pack_total": 0, "pack_left": 0}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/v1/pack/list":
                body = json.dumps({"packs": {"stocks": {"available": True, "size": len(payload_zip), "uploaded": "2026-11-23T00:00:00Z"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode())
            if data.get("code") == "sq-EMPTY":
                body = json.dumps({"error": {"code": "pack_exhausted", "message": "no left"}}).encode()
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(payload_zip)))
            self.send_header("X-SQ-Pack-Left", "2")
            self.end_headers()
            self.wfile.write(payload_zip)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        monkeypatch.setenv("SIMPLEQUANT_HOSTED_URL", f"http://127.0.0.1:{port}")
        q = hosted.query_quota("")
        assert q["ai_left"] == 3
        with pytest.raises(hosted.HostedError, match="额度码无效"):
            hosted.query_quota("sq-BAD")

        packs = hosted.list_cloud_packs()
        assert packs["stocks"]["available"] is True and packs["stocks"]["size"] == len(payload_zip)

        prog = []
        dest, left = hosted.download_cloud_pack("sq-OK1234", "stocks", tmp_path / "stocks.zip",
                                                on_progress=lambda d, tot: prog.append((d, tot)))
        assert dest.read_bytes() == payload_zip and left == 2 and prog[-1] == (len(payload_zip), len(payload_zip))

        with pytest.raises(hosted.HostedError, match="下载次数已用完"):
            hosted.download_cloud_pack("sq-EMPTY", "stocks", tmp_path / "fail.zip")
        assert not (tmp_path / "fail.zip").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_hosted_openai_provider_402_friendly_message(monkeypatch):
    import httpx
    from simplequant.llm import LLMConfig, LLMError, get_provider, hosted

    monkeypatch.setattr(hosted, "device_id", lambda: "3f2b8c1e-9a4d-4b7e-8c2a-1d5e6f7a8b9c")
    seen_auth = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization"))
        return httpx.Response(402, json={"error": {"code": "trial_exhausted", "message": "out"}})

    cfg = LLMConfig.from_preset("hosted")
    prov = get_provider(cfg)
    with pytest.raises(LLMError, match="免费体验次数已用完"):
        prov.chat("sys", [{"role": "user", "content": "hi"}],
                  http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen_auth == ["Bearer trial:3f2b8c1e-9a4d-4b7e-8c2a-1d5e6f7a8b9c"]


def test_cb_store_export_import_zip(tmp_path):
    import io
    import zipfile
    import pandas as pd
    from simplequant.bonds import CBStore

    src = CBStore(tmp_path / "src")
    (src.root / "daily").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"code": ["110001"], "name": ["测试转债"]}).to_parquet(src.root / "list.parquet")
    pd.DataFrame({"raw_close": [101.0]}).to_parquet(src.root / "daily" / "110001.parquet")
    (src.root / "manifest.json").write_text(json.dumps({"110001": "2026-11-20"}), encoding="utf-8")

    raw = src.export_zip()
    dst = CBStore(tmp_path / "dst")
    assert dst.import_zip(io.BytesIO(raw)) == 1
    assert (dst.root / "daily" / "110001.parquet").exists()
    assert json.loads((dst.root / "manifest.json").read_text(encoding="utf-8"))["110001"] == "2026-11-20"

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    bad.seek(0)
    with pytest.raises(ValueError, match="不认识的文件"):
        dst.import_zip(bad)


def test_translate_modifies_base_rule():
    from simplequant.llm import translate, from_rule
    from simplequant.llm.nl import to_rule

    base_rule = to_rule(MACD_STOP)
    back = from_rule(base_rule)
    assert to_rule(back) == base_rule

    updated = {**MACD_STOP, "cooldown": 5}
    fake = FakeProvider([json.dumps(updated)])
    res = translate("卖出后冷却5天", fake, base={"kind": "rule", "rule": base_rule})
    msg = fake.calls[0]["messages"][0]["content"]
    assert msg.startswith("Current rule:") and msg.endswith("Change request:\n卖出后冷却5天")
    assert "Editing an existing rule" in fake.calls[0]["system"]
    assert res.ok and res.spec["rule"]["cooldown"] == 5


def test_explain_holdings_summary_and_prompt():
    import pandas as pd
    from simplequant.stocks.diagnosis import Diagnosis
    from simplequant.llm import explain_holdings, holdings_summary

    df = pd.DataFrame([
        {"code": "sh.600519", "name": "贵州茅台", "shares": 100.0, "price": 1500.0, "value": 150000.0,
         "weight": 0.6, "industry": "C15酒、饮料和精制茶制造业", "pe_pct": 65.0, "pb_pct": 80.0, "roe_pct": 95.0, "stale": False},
        {"code": "sh.601398", "name": "工商银行", "shares": 20000.0, "price": 5.0, "value": 100000.0,
         "weight": 0.4, "industry": "J66货币金融服务", "pe_pct": 12.0, "pb_pct": 10.0, "roe_pct": 40.0, "stale": False},
    ])
    inds = pd.DataFrame([
        {"industry": "C15酒、饮料和精制茶制造业", "weight": 0.6, "pool_weight": 0.1},
        {"industry": "J66货币金融服务", "weight": 0.4, "pool_weight": 0.15},
    ])
    corr = pd.DataFrame([[1.0, 0.25], [0.25, 1.0]], index=["贵州茅台", "工商银行"], columns=["贵州茅台", "工商银行"])
    diag = Diagnosis(
        asof=pd.Timestamp("2026-11-20"), holdings=df, pool_median={"mcap": 1e10},
        style_pct={"mcap": 90.0, "pe": 44.0, "pb": 52.0, "dy": 70.0, "roe": 73.0},
        style_cover={"mcap": 1.0, "pe": 1.0, "pb": 1.0, "dy": 1.0, "roe": 1.0},
        concentration={"n": 2, "top1": 0.6, "top3": 1.0, "effective_n": 1.92, "industries": 2},
        industries=inds, corr=corr, avg_corr=0.25, top_pairs=[("贵州茅台", "工商银行", 0.25)],
        curve=None, perf={"portfolio": {"total_return": 0.12, "volatility": 0.18, "max_drawdown": -0.1},
                          "benchmark": {"total_return": 0.08, "volatility": 0.16, "max_drawdown": -0.12}, "beta": 0.9},
        pool_size=300,
    )
    summary = holdings_summary(diag, "沪深300")
    assert summary["concentration"]["effective_n"] == 1.92
    assert summary["industry_comparison"][0]["industry"] == "酒、饮料和精制茶制造业"

    fake = FakeProvider(["- 集中度偏高"])
    out = explain_holdings(fake, diag, "沪深300", "zh")
    assert out == "- 集中度偏高"
    assert "NEVER recommend buying, selling" in fake.calls[0]["system"]
    assert "沪深300" in fake.calls[0]["messages"][0]["content"]


def test_review_credibility_prompt():
    from simplequant.llm import review_credibility

    fake = FakeProvider(["- 过拟合风险：中"])
    checks = [
        {"level": "warn", "key": "cred.few_trades", "args": {"n": 8}},
        {"level": "warn", "key": "cred.concentrated", "args": {"k": 3, "top": 0.25, "rest": -0.02, "total": 0.23}},
    ]
    extra = {"walk_forward": {"windows": 4, "wfe": 0.65, "oos_metrics": {"cagr": 0.06}}}
    out = review_credibility(fake, "双均线", {"cagr": 0.12, "sharpe": 1.1}, checks, extra, "zh")
    assert out == "- 过拟合风险：中"
    assert "overfitting risk rating" in fake.calls[0]["system"]
    body = fake.calls[0]["messages"][0]["content"]
    assert "8" in body and "25.0%" in body and "walk_forward" in body


def test_cloud_ui_quota_error_detection():
    from simplequant.llm import HostedError
    from gui.cloud_ui import is_quota_error

    assert is_quota_error(HostedError("x", status=402, code="trial_exhausted"))
    assert is_quota_error(RuntimeError("当前设备的免费体验次数已用完"))
    assert not is_quota_error(RuntimeError("network timeout"))


def test_explain_allocation_and_compare_runs():
    import pandas as pd
    from types import SimpleNamespace
    from simplequant.llm import allocation_summary, explain_allocation, compare_runs

    idx = pd.date_range("2024-01-02", periods=6, freq="B")
    R = pd.DataFrame({"a": [0.01, -0.01, 0.02, 0.0, 0.01, -0.005], "b": [0.002, 0.003, -0.001, 0.002, 0.001, 0.002]}, index=idx)
    eq = pd.DataFrame({"value": [1.0, 1.005, 1.01, 1.012, 1.02, 1.018], "benchmark": [1.0, 0.99, 1.01, 1.01, 1.02, 1.015]}, index=idx)
    res = SimpleNamespace(metrics={"cagr": 0.07, "volatility": 0.08, "max_drawdown": -0.05, "sharpe": 0.85},
                          stats={"risk": 3}, n_rebalances=4, equity=eq)
    r = {"profile": SimpleNamespace(level=3, max_dd=0.15), "res": res, "R": R, "manual": False,
         "rebalance": "quarterly", "weights": {"a": 0.4, "b": 0.6},
         "sleeves": [{"id": "a", "class": "equity"}, {"id": "b", "class": "bond"}]}
    alloc_checks = [{"level": "ok", "key": "alloc.dd_within", "args": {"dd": 0.05, "limit": 0.15}}]
    s = allocation_summary(r, {"a": "沪深300ETF", "b": "国债ETF"}, {"equity": "股票", "bond": "债券"}, "稳健型", alloc_checks)
    assert s["risk_profile"]["level"] == "C3" and len(s["class_mix"]) == 2
    assert len(s["yearly_returns"]) == 1 and s["yearly_returns"][0]["year"] == 2024
    assert "5.0%" in s["checks"][0]["detail"] and "15.0%" in s["checks"][0]["detail"]

    fake1 = FakeProvider(["- 风险等级匹配"])
    assert explain_allocation(fake1, s, "zh") == "- 风险等级匹配"
    assert "NEVER recommend buying, selling" in fake1.calls[0]["system"]
    assert fake1.calls[0]["messages"][0]["content"].startswith("Asset allocation backtest summary:\n")

    fake2 = FakeProvider(["- 回撤明显收窄"])
    prev = {"name": "旧版", "metrics": {"total_return": 0.2, "max_drawdown": -0.18}, "setup": {"data": "510300"}}
    curr = {"name": "新版", "metrics": {"total_return": 0.18, "max_drawdown": -0.10}, "setup": {"data": "510300"}}
    assert compare_runs(fake2, prev, curr, ["新增 8% 移动止损"], "旧规则", "新规则", "zh") == "- 回撤明显收窄"
    assert fake2.calls[0]["messages"][0]["content"].startswith("Backtest A/B comparison:\n")
