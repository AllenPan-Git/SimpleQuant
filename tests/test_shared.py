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
    assert shared.data_source_text(em, "zh") == "东方财富 · 后复权"
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
