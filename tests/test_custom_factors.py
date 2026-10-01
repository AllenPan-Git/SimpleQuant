"""
自定义选股因子（选股页「自定义因子」标签）
核心：用代码写出的因子与内置因子完全等价；保存后在研究、选股、模拟盘里都能用；出错时指出行号
"""

import numpy as np
import pandas as pd
import pytest

from test_stocks import make_panel, SPEC
from simplequant import strategies
from simplequant.engine import BrokerConfig
from simplequant.export.selection_platforms import check_selection_exportable
from simplequant.stocks import FACTORS, GROUPS, compute, run_selection, custom_factors as cf
from simplequant.stocks.custom_factors import FactorCodeError

RET20 = 'def factor(p):\n    """20日收益"""\n    return p["close"] / p["close"].shift(20) - 1\n'


@pytest.fixture(autouse=True)
def tmp_factor_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "FACTOR_DIR", tmp_path / "factors")
    cf.load_all()
    yield
    monkeypatch.undo()
    cf.load_all()                         # 恢复成真实目录里的登记


@pytest.fixture(scope="module")
def panel():
    return make_panel()


def test_code_factor_equals_builtin(panel):
    pd.testing.assert_frame_equal(cf.evaluate(RET20, panel), compute(panel, "ret20"), check_freq=False)


def test_save_registers_and_selection_matches_builtin(panel):
    key = cf.save_factor("我的20日收益", RET20, direction=-1, desc="反转")
    assert key.startswith("u_") and FACTORS[key]["group"] == "custom" and "custom" in GROUPS
    assert FACTORS[key]["direction"] == -1 and not FACTORS[key]["requires_fin"]
    spec_builtin = {**SPEC, "factors": [{"key": "ret20", "weight": 1, "direction": -1}]}
    spec_custom = {**SPEC, "factors": [{"key": key, "weight": 1, "direction": -1}]}
    a = run_selection(panel, spec_builtin, BrokerConfig(), start=str(panel.calendar[100].date()))
    b = run_selection(panel, spec_custom, BrokerConfig(), start=str(panel.calendar[100].date()))
    pd.testing.assert_frame_equal(a.orders, b.orders)
    assert "我的20日收益" in strategies.describe({**spec_custom, "universe": "hs300"})


def test_edit_keeps_key_and_delete_unregisters():
    key = cf.save_factor("动量", RET20)
    key2 = cf.save_factor("动量改名", RET20.replace("20", "60"), key=key)
    assert key2 == key and FACTORS[key]["name"] == "动量改名" and len(cf.list_factors()) == 1
    assert cf.save_factor("动量改名", RET20) == key                     # 同名 = 覆盖原来那个
    cf.delete_factor(key)
    assert key not in FACTORS and not cf.list_factors()
    with pytest.raises(ValueError, match="不存在"):
        compute(make_panel(n=5, days=30), key)
    # 已删除的因子：描述不报错
    assert strategies.describe({**SPEC, "universe": "hs300", "factors": [{"key": key, "weight": 1, "direction": 1}]})


def test_requires_fin_detected():
    assert cf.uses_fin('def factor(p):\n    return p["roe"]\n')
    assert cf.uses_fin('def factor(p):\n    return np.log(p["mcap"])\n')
    assert not cf.uses_fin(RET20)


def test_nan_and_inf_are_cleaned(panel):
    code = 'def factor(p):\n    return 1 / (p["close"] - p["close"])\n'
    key = cf.save_factor("除零", code)
    out = compute(panel, key)
    assert not np.isinf(out.to_numpy()).any()


@pytest.mark.parametrize("code, where", [
    ("", "空"),
    ("def factor(p)\n    return 1\n", "第 1 行"),
    ("def f(p):\n    return 1\n", "def factor(p)"),
    ("x = undefined\ndef factor(p):\n    return p['close']\n", "第 1 行"),
])
def test_compile_errors(code, where):
    with pytest.raises(FactorCodeError) as ei:
        cf.compile_factor(code)
    assert where in str(ei.value)


def test_runtime_errors_point_at_line(panel):
    with pytest.raises(FactorCodeError) as ei:
        cf.evaluate('def factor(p):\n    x = p["close"]\n    return p["no_such_field"]\n', panel)
    assert "第 3 行" in str(ei.value) and "no_such_field" in str(ei.value)
    with pytest.raises(FactorCodeError, match="DataFrame"):
        cf.evaluate('def factor(p):\n    return p["close"].iloc[-1]\n', panel)
    with pytest.raises(FactorCodeError, match="交易日"):
        cf.evaluate('def factor(p):\n    return p["close"].reset_index(drop=True)\n', panel)


def test_result_is_aligned_to_panel(panel):
    code = 'def factor(p):\n    return p["close"].iloc[:, :5]\n'           # 只算了 5 只股票
    out = cf.evaluate(code, panel)
    assert out.shape == panel["close"].shape and out.iloc[:, 5:].isna().all().all()


def test_bad_files_are_ignored(tmp_path):
    d = tmp_path / "factors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "u_00000000.json").write_text("{not json", encoding="utf-8")
    (d / "u_11111111.json").write_text('{"key": "evil", "name": "x", "code": ""}', encoding="utf-8")
    assert cf.list_factors() == {}
    cf.delete_factor("../../etc")                                      # 非法键：什么都不做


def test_platform_export_refused_but_follow_ok():
    key = cf.save_factor("动量", RET20)
    spec = {**SPEC, "universe": "hs300", "factors": [{"key": key, "weight": 1, "direction": 1}]}
    with pytest.raises(ValueError, match="按名单调仓"):
        check_selection_exportable(spec, "joinquant")
    check_selection_exportable(spec, "joinquant", follow=True)


def test_skeleton_runs(panel):
    for lg in ("zh", "en"):
        out = cf.evaluate(cf.skeleton(lg), panel)
        assert out.notna().to_numpy().mean() > 0.9
