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
