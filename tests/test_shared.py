"""ui/shared.py：与界面框架无关的页面逻辑"""

import ast
from pathlib import Path

import pandas as pd

from ui import shared

ROOT = Path(__file__).resolve().parents[1]


def test_no_ui_framework_imports():
    tree = ast.parse((ROOT / "ui" / "shared.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"streamlit", "nicegui"}


def test_parse_symbols():
    assert shared.parse_symbols(["510300"], " 159915，510300, 518880 ,") == ["510300", "159915", "518880"]
    assert shared.parse_symbols([], "") == []


def test_slice_prices_includes_end_day():
    idx = pd.date_range("2024-01-01 09:31", periods=3 * 240, freq="min")
    df = pd.DataFrame({"close": range(len(idx))}, index=idx)
    out = shared.slice_prices({"x": df}, (pd.Timestamp("2024-01-01").date(), pd.Timestamp("2024-01-01").date()))
    assert out["x"].index.min().date() == out["x"].index.max().date() == pd.Timestamp("2024-01-01").date()
    assert len(out["x"]) > 0


def test_presets_clean_roundtrip():
    for key, make in shared.PRESETS.items():
        rule = make()
        cleaned = shared.clean(rule)
        assert all("_id" not in c for side in ("buy", "sell") for c in cleaned[side]["conditions"]), key
        assert all("_id" in c for side in ("buy", "sell") for c in rule[side]["conditions"]), key   # 原规则不被改
        again = shared.with_ids(cleaned)
        assert shared.clean(again) == cleaned


def test_formatting():
    assert shared.fmt_metric("total_return", 0.1234) == "12.34%"
    assert shared.fmt_metric("final_value", 123456.7) == "123,457"
    assert shared.fmt_metric("sharpe", float("nan")) == "—"
    assert shared.export_filename("模板：双均线 / MA") == "双均线_MA.py"
    assert shared.export_filename("：") == "strategy.py"


def test_tables_translate(tmp_path):
    orders = pd.DataFrame({"side": ["buy", "sell"], "price": [1.0, 1.1]})
    zh, en = shared.orders_table(orders, "zh"), shared.orders_table(orders, "en")
    assert list(zh.columns) != list(en.columns)
    assert zh.iloc[0, 0] != "buy" and en.iloc[0, 0] != "buy"


def test_asset_names_tell_same_symbol_apart():
    """同一代码的后复权与不复权数据在回测里是两个标的（第七期录屏发现组合回测只剩一份）"""
    from simplequant.data.library import DatasetMeta
    from simplequant.engine.limits import _code6
    from simplequant.engine.market import symbol_of
    metas = [DatasetMeta(id="a", name="515080", symbol="515080", source="akshare", freq="1d", adjust="hfq",
                         start="2020-01-02", end="2026-09-30"),
             DatasetMeta(id="b", name="515080", symbol="515080", source="akshare", freq="1d", adjust="",
                         start="2020-01-02", end="2026-09-30"),
             DatasetMeta(id="c", name="510300 沪深300ETF", symbol="510300", source="akshare", freq="1d", adjust="hfq",
                         start="2020-01-02", end="2026-09-30")]
    by_id = {m.id: m for m in metas}
    names = shared.asset_names(by_id, ["a", "b", "c"], "zh")
    assert len(set(names.values())) == 3
    assert names["c"] == "510300 沪深300ETF"                         # 不重名的不变
    assert "不复权" in names["b"] and "后复权" in names["a"]
    assert all(_code6(n) == symbol_of(n) == "515080" for n in (names["a"], names["b"]))   # 仍能识别代码
    assert shared.asset_names(by_id, ["a", "c"], "zh") == {"a": "515080", "c": "510300 沪深300ETF"}


def test_data_sources_and_fallback():
    """回测报告注明数据来源；东方财富连不上改用新浪的股票数据要提醒复权口径不同"""
    from simplequant.data.library import DatasetMeta
    em = DatasetMeta(id="a", name="510300", symbol="510300", source="akshare", freq="1d", adjust="hfq",
                     extra={"asset": "etf", "provider": "eastmoney"})
    sina = DatasetMeta(id="b", name="600519", symbol="600519", source="akshare", freq="1d", adjust="hfq",
                       extra={"asset": "stock", "provider": "sina+factors"})
    old = DatasetMeta(id="c", name="510500", symbol="510500", source="akshare", freq="1d", adjust="")
    csv = DatasetMeta(id="d", name="X", symbol="X", source="csv", freq="1d", adjust="hfq")
    assert shared.data_source_text(em, "zh") == "东方财富 · 后复权（加法）"       # 改为等比之前下载的
    assert shared.data_source_text(em, "en") == "Eastmoney · Back-adjusted (additive)"
    new = DatasetMeta(id="f", name="600519", symbol="600519", source="akshare", freq="1d", adjust="hfq",
                      extra={"asset": "stock", "provider": "eastmoney+factors", "adjust_method": "ratio"})
    assert shared.data_source_text(new, "zh") == "东方财富·不复权 × BaoStock 复权因子 · 后复权"
    assert shared.data_source_text(sina, "zh") == "新浪财经·等比复权 · 后复权"
    assert shared.data_source_text(old, "zh") == "AKShare · 不复权"          # 旧数据没记录接口
    assert shared.data_source_text(csv, "zh") == "CSV 导入"
    assert shared.sina_fallback(sina) and not shared.sina_fallback(em) and not shared.sina_fallback(old)
    idx = DatasetMeta(id="e", name="000300", symbol="000300", source="akshare", freq="1d", adjust="",
                      extra={"asset": "index", "provider": "sina"})
    assert not shared.sina_fallback(idx)                                      # 指数本来就先用新浪

    assert shared.sources_line({}, "zh") == ""
    assert shared.sources_line({"A": "东方财富 · 后复权", "B": "东方财富 · 后复权"}, "zh") == "数据来源：东方财富 · 后复权"
    line = shared.sources_line({"A": "东方财富 · 后复权", "B": "新浪财经 · 后复权"}, "zh")
    assert line == "数据来源：东方财富 · 后复权：A；新浪财经 · 后复权：B"
    many = shared.sources_line({str(i): "东方财富 · 后复权" for i in range(8)} | {"Z": "新浪财经 · 后复权"}, "en")
    assert many == "Data source: 东方财富 · 后复权: 8 assets; 新浪财经 · 后复权: Z"


def test_spec_matching_ignores_name_and_broker():
    """比较策略只看规则：名称、资金不同算同一个；同规则多个已保存时优先同名；模拟账户含暂停的、读坏的跳过"""
    from types import SimpleNamespace
    a = {"kind": "template", "template": "rsi", "params": {"n": 14}}
    saved = {"甲": dict(a), "RSI": {**a, "broker": {"cash": 5e4}}, "别的": {**a, "params": {"n": 6}}}
    assert shared.saved_name({**a, "name": "RSI"}, saved) == "RSI"
    assert shared.saved_name(a, saved) == "甲"
    assert shared.saved_name({**a, "params": {"n": 20}}, saved) is None

    class Broken:
        @property
        def spec(self):
            raise OSError("坏了")
    accs = [SimpleNamespace(spec={**a, "name": "x"}), SimpleNamespace(spec=saved["别的"]), Broken()]
    assert shared.paper_count(a, accs) == 1


def test_progress_steps():
    """四步进度：完成的打勾，第一个没完成的是「建议下一步」；验证要做过参数优化或滚动优化才算完成"""
    def run(**kw):
        args = dict(saved="RSI", dirty=False, ret=0.4422, verify=None, paper=0) | kw
        return {s["key"]: s for s in shared.progress_steps("zh", **args)}

    s = run(verify={"credibility": True, "random": True, "phase": True, "optimize": False, "walkforward": False})
    assert [s[k]["done"] for k in shared.PROGRESS_STEPS] == [True, True, False, False]
    assert s["setup"]["v"] == "已保存" and "RSI" in s["setup"]["title"]
    assert s["backtest"]["v"] == "44.22%"
    assert s["verify"]["next"] and s["verify"]["k"] == "建议下一步 · 03" and s["verify"]["v"] == "验证"
    assert "3 / 5 项" in s["verify"]["title"]
    assert s["paper"]["v"] == "—" and not s["paper"]["next"]

    s = run(verify={"credibility": True, "optimize": True}, paper=2)
    assert all(x["done"] for x in s.values()) and not any(x["next"] for x in s.values())
    assert s["verify"]["v"] == "2 / 2 项" and s["paper"]["v"] == "2 个账户"

    s = run(dirty=True)                         # 有改动未保存：第一步没完成，建议先保存
    assert s["setup"]["next"] and "有改动未保存" in s["setup"]["title"]
    s = run(saved=None, ret=None)
    assert s["setup"]["next"] and s["backtest"]["v"] == "未运行" and not s["backtest"]["done"]
    assert run(verify=None)["verify"]["title"] == "03 验证"     # 不知道做了哪些：只写步骤名
    en = {x["key"]: x for x in shared.progress_steps("en", None, False, None, None, 0)}
    assert en["setup"]["k"] == "Suggested next · 01" and en["backtest"]["k"] == "02 Backtest"
