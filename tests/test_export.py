"""
导出独立 Python 脚本：导出脚本的每一笔成交必须与 SimpleQuant 回测完全相同
（在隔离的命名空间里执行生成的代码，不依赖 simplequant 包）
"""

import ast
from dataclasses import asdict

import pandas as pd
import pytest

from conftest import make_prices
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.export import single_asset_script, selection_script
from simplequant.rules import make_ind
from simplequant.strategies import TEMPLATES

BROKER = BrokerConfig(cash=200_000, commission=0.00025, min_commission=5, stamp_duty=0.0005, slippage=0.0005)


def exec_script(code: str) -> dict:
    """把生成的代码当作一个独立模块执行（Backtrader 需要在 sys.modules 里找到策略所在模块）"""
    import sys
    import types
    mod = types.ModuleType("exported_strategy")
    sys.modules[mod.__name__] = mod
    exec(compile(code, "exported_strategy.py", "exec"), mod.__dict__)   # 不会触发 if __name__ == "__main__"
    return mod.__dict__


def export_and_compare(spec, prices):
    code = single_asset_script(spec, [{"name": n, "source": "csv", "path": f"{n}.csv"} for n in prices], asdict(BROKER))
    ast.parse(code)
    assert "simplequant" not in code.split('"""', 2)[2]          # 文档说明之后不再依赖 simplequant
    ns = exec_script(code)
    strat = ns["run"](prices)
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    ours = run_backtest(prices, *strategies.resolve(spec), BROKER).orders
    assert len(ours) > 0
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.reset_index(drop=True))
    return code


@pytest.mark.parametrize("key", list(TEMPLATES))
def test_every_template_exports_identically(key):
    prices = {"A": make_prices(seed=1), "B": make_prices(seed=2, drift=0.0006)}
    export_and_compare({"kind": "template", "template": key, "params": {}}, prices)


RICH_RULE = {
    "buy": {"logic": "all", "conditions": [
        {"left": make_ind("macd"), "op": "cross_above", "right": {**make_ind("macd"), "line": "dea"}},
        {"left": make_ind("rsi", period=14), "op": "<", "right": {"value": 70}},
        {"left": make_ind("close"), "op": ">", "right": {**make_ind("boll"), "line": "lower"}},
        {"left": make_ind("vol_ratio", period=5), "op": ">", "right": {"value": 0.3}},
    ]},
    "sell": {"logic": "any", "conditions": [
        {"left": {**make_ind("kdj"), "line": "j"}, "op": ">", "right": {"value": 100}},
        {"left": {"ind": "pnl_pct"}, "op": "<", "right": {"value": -6}},
        {"left": make_ind("close"), "op": "cross_below", "right": make_ind("lowest", period=10)},
        {"left": make_ind("ma_slope", period=20, n=5), "op": "<", "right": {"value": -2}},
        {"left": make_ind("bias", period=20), "op": ">", "right": {"value": 8}},
        {"left": make_ind("obv"), "op": "<", "right": make_ind("obv")},
        {"left": {**make_ind("adx"), "line": "mdi"}, "op": ">", "right": {**make_ind("adx"), "line": "pdi"}},
        {"left": make_ind("volatility", period=20), "op": ">", "right": {"value": 5}},
        {"left": {**make_ind("macd"), "line": "hist"}, "op": "<", "right": {"value": -0.5}},
    ]},
    "position_pct": 80,
}


def test_rich_rule_exports_identically():
    prices = {"A": make_prices(seed=3, n=500)}
    code = export_and_compare({"kind": "rule", "rule": RICH_RULE}, prices)
    assert "class KDJ" in code and "class OBV" in code
    assert 'bt.ind.CrossOver(i["macd_12_26_9_dif"], i["macd_12_26_9_dea"])' in code


def test_every_indicator_can_be_exported():
    from simplequant.rules import INDICATORS
    from simplequant.export.python_script import _line_expr
    for key, meta in INDICATORS.items():
        if meta.get("position"):
            continue
        if meta.get("macro"):                     # 利率条件不支持导出（见 test_rates.py）
            with pytest.raises(ValueError):
                _line_expr(make_ind(key))
            continue
        for line in (meta.get("lines") or {None: None}):
            spec = make_ind(key)
            if line:
                spec["line"] = line
            name, expr = _line_expr(spec)
            assert name.isidentifier() or name in ("close", "open", "high", "low", "volume")
            compile(expr, "<expr>", "eval")


def test_script_has_no_relative_imports():
    """BaseStrategy 里为避开循环导入写在函数内的相对导入，导出时换成附带的函数源码"""
    code = single_asset_script({"kind": "template", "template": "rsi_reversion", "params": {}},
                               [{"name": "A", "source": "csv", "path": "A.csv"}], asdict(BROKER))
    assert not [ln for ln in code.splitlines() if ln.lstrip().startswith("from .")]
    ns = exec_script(code)
    assert ns["tax_rate"]("2024-01-02", "2024-01-20") == 0.20 and ns["tax_rate"]("2023-01-02", "2024-06-03") == 0.0


def test_script_header_and_selection_script():
    code = single_asset_script({"kind": "template", "template": "sma_cross", "params": {"fast": 10}},
                               [{"name": "510300", "source": "akshare", "symbol": "510300", "asset": "etf",
                                 "adjust": "hfq", "start": "2020-01-01", "end": "2026-09-28"}], asdict(BROKER),
                               title="双均线", filename="ma.py")
    assert "python ma.py" in code and "短期均线周期=10" in code and "load_akshare" in code
    spec = {"kind": "selection", "universe": "hs300", "factors": [{"key": "ep", "weight": 1, "direction": 1}],
            "top_n": 10, "rebalance": "monthly", "filters": {"exclude_st": True, "min_list_days": 250}, "position_pct": 95}
    sel = selection_script(spec, asdict(BROKER), "2021-01-04", r"D:\p")
    ast.parse(sel)
    assert "run_selection" in sel and "'universe': 'hs300'" in sel
