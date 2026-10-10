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
from simplequant.stocks.factors import factors_for
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


def test_dividend_fields_are_stock_only():
    """用到分红字段的因子只适用于股票（可转债面板没有这些字段，第七期发现会报错）"""
    code = 'def factor(p):\n    return p["div_cash"].rolling(250, min_periods=1).sum() / p["raw_close"]\n'
    assert cf.assets_of(code) == ("stock",) and cf.uses_div(code) and not cf.uses_fin(code)
    assert cf.assets_of('def factor(p):\n    return p["raw_close"]\n') == ("stock", "cb")
    key = cf.save_factor("股息", code)
    assert FACTORS[key]["requires_div"] and FACTORS[key]["assets"] == ("stock",)
    assert "div_cash" in cf.skeleton("zh") and "div_cash" in cf.skeleton("en")


def _with_dividends(panel, events: dict, have=None):
    """events：{(日期序号, 代码): (每股现金, 每股送股, 每股转增)}"""
    import dataclasses
    fields = dict(panel.fields)
    for k in ("cash", "bonus", "reserve"):
        fields[f"div_{k}"] = panel["close"] * 0.0
    for (i, c), vals in events.items():
        for k, v in zip(("cash", "bonus", "reserve"), vals):
            fields[f"div_{k}"].iloc[i, panel.codes.index(c)] = v
    return dataclasses.replace(panel, fields=fields,
                               div_codes=frozenset(panel.codes if have is None else have))


def test_dividend_yield_factor(panel):
    a, b, c = panel.codes[:3]
    p = _with_dividends(panel, {(100, a): (0.3, 0, 0), (220, a): (0.2, 0, 0),       # 年报 + 中期
                                (100, b): (0.5, 0, 0.5), (280, b): (0.4, 0, 0)},     # 10 转 5 后再分红
                        have=[a, b])
    dy = compute(p, "dy")
    raw = p["raw_close"]
    assert dy[a].iloc[99] == 0                                         # 还没分红
    assert dy[a].iloc[150] == pytest.approx(0.3 / raw[a].iloc[150])
    assert dy[a].iloc[250] == pytest.approx(0.5 / raw[a].iloc[250])   # 两次相隔不到 200 天，合计
    assert dy[a].iloc[399] == pytest.approx(0.5 / raw[a].iloc[399])   # 最近一次（第 220 天）往前 200 天都算
    # 转增后：之前那次 0.5 元/股按新股本折成 0.5 / 1.5
    assert dy[b].iloc[300] == pytest.approx((0.5 / 1.5 + 0.4) / raw[b].iloc[300])
    assert dy[b].iloc[150] == pytest.approx(0.5 / 1.5 / raw[b].iloc[150])        # 转增当天起按新股本
    assert dy[c].isna().all()                                         # 没有分红数据：不参与排名，不是 0
    assert compute(panel, "dy").isna().all().all()                    # 没下载分红数据
    assert FACTORS["dy"]["requires_div"] and "dy" in factors_for("stock") and "dy" not in factors_for("cb")


def test_review_flags_lookahead_and_unknown_fields():
    assert cf.review(RET20) == [] and cf.review(cf.skeleton("zh")) == [] and cf.review(cf.skeleton("en")) == []
    code = ('def factor(q):\n'
            '    a = q["close"].shift(-5)\n'
            '    b = q["close"].pct_change(periods=-1)\n'
            '    c = q["volume"].bfill() + q["amount"].fillna(method="backfill")\n'
            '    d = q["close"].rolling(5, center=True).mean()\n'
            '    return a + b + c + d + q["roe"] + q["nope"]\n')
    msgs = cf.review(code)
    assert len(msgs) == 5, msgs                       # 按行排列；同一行同样的问题只报一次
    assert msgs[0].startswith("第 2 行") and "shift" in msgs[0] and "pct_change" in msgs[1]
    assert "bfill" in msgs[2] and "center=True" in msgs[3] and msgs[4].startswith("第 6 行") and 'q["nope"]' in msgs[4]
    assert cf.review('def factor(p):\n    return p["close"].shift(5) + p["div_cash"] + p["premium"]\n') == []


def test_review_strict_blocks_imports_and_io():
    code = ('import os\n'
            'def factor(p):\n'
            '    open("x.txt").read()\n'
            '    pd.read_csv("x.csv")\n'
            '    p["close"].to_csv("y.csv")\n'
            '    return p["close"].__class__\n')
    assert cf.review(code) == []                      # 自己写的代码可以 import、读写文件
    msgs = cf.review(code, "en", strict=True)
    assert len(msgs) == 5 and msgs[0].startswith("Line 1:") and "import" in msgs[0], msgs
