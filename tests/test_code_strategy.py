"""
代码策略（策略页的代码编辑区）
核心：模板 / 规则转成代码后，回测的每一笔成交都与原策略相同；代码出错时指出行号
"""

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_export import exec_script, BROKER
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.optimize import optimize
from simplequant.export import single_asset_script, platform_script
from simplequant.strategies import TEMPLATES, code_strategy as cs
from simplequant.strategies.code_strategy import CodeError
from ui.shared import PRESETS

PRICES = {"A": make_prices(seed=1), "B": make_prices(seed=2, drift=0.0006)}


def same_orders(spec_a, spec_b, prices=PRICES):
    a = run_backtest(prices, *strategies.resolve(spec_a), BROKER).orders
    b = run_backtest(prices, *strategies.resolve(spec_b), BROKER).orders
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    return len(a)


def code_spec(code, **params):
    return {"kind": "code", "code": code, "params": params}


# ---------------- 转成代码 ----------------
@pytest.mark.parametrize("key", list(TEMPLATES))
def test_template_to_code_trades_identically(key):
    spec = {"kind": "template", "template": key, "params": {}}
    code = cs.to_code(spec)
    assert "class Strategy(" in code and "迁移自" not in code
    assert same_orders(spec, code_spec(code)) > 0


def test_template_to_code_keeps_current_params():
    spec = {"kind": "template", "template": "sma_cross", "params": {"fast": 7, "slow": 30}}
    code = cs.to_code(spec)
    assert cs.literal_params(code) == {"fast": 7, "slow": 30, "position_pct": 95}
    same_orders(spec, code_spec(code))


def test_rotation_template_keeps_min_assets():
    code = cs.to_code({"kind": "template", "template": "momentum_rotation", "params": {}})
    assert strategies.min_assets(code_spec(code)) == 2


@pytest.mark.parametrize("preset", list(PRESETS))
def test_rule_to_code_trades_identically(preset):
    spec = {"kind": "rule", "rule": PRESETS[preset]()}
    assert same_orders(spec, code_spec(cs.to_code(spec))) > 0


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_skeleton_runs_and_trades(lang):
    spec = code_spec(cs.skeleton(lang))
    assert not cs.check(spec["code"], lang)
    res = run_backtest(PRICES, *strategies.resolve(spec), BROKER)
    assert len(res.orders) > 4
    # 理由直接写成一句话也能显示
    from simplequant.i18n import render
    buy = next(kw for _, key, kw in res.logs if key == "log.buy")
    assert ("快线在慢线上方" if lang == "zh" else "fast MA above slow MA") in render("log.buy", buy, lang)


# ---------------- 参数 ----------------
def test_params_override_and_tunables():
    code = cs.skeleton()
    tbs = {tb.path: tb for tb in strategies.tunables(code_spec(code, fast=8))}
    assert set(tbs) == {"params.position_pct", "params.fast", "params.slow"}
    assert tbs["params.fast"].value == 8 and tbs["params.fast"].is_int
    assert tbs["params.position_pct"].max == 100
    # 覆盖参数等价于把代码里的默认值改掉
    same_orders(code_spec(code, fast=8, slow=30), code_spec(cs.set_params(code, {"fast": 8, "slow": 30})))


def test_set_params_adds_missing_params_block():
    code = 'class Strategy(PerAsset):\n    """说明"""\n\n    def next(self):\n        pass\n'
    new = cs.set_params(code, {"period": 10})
    assert cs.literal_params(new) == {"period": 10}
    assert not cs.check(new)
    assert cs.set_params(new, {"period": np.int64(12)}).count("params = (('period', 12),)") == 1


def test_materialize_writes_overrides_back():
    spec = code_spec(cs.skeleton(), fast=9)
    code = cs.materialize(spec)
    assert cs.literal_params(code)["fast"] == 9
    same_orders(spec, code_spec(code))


def test_describe_uses_docstring_and_params():
    text = strategies.describe(code_spec(cs.skeleton(), slow=40))
    assert "我的策略" in text and "slow=40" in text and "fast=5" in text
    assert "【自定义代码】" in strategies.describe(code_spec("class Strategy(PerAsset):\n    pass\n"))
    assert strategies.describe(code_spec("def (")).startswith("【自定义代码】")      # 语法错误也不报错


def test_optimize_code_strategy_in_worker_processes():
    code = cs.skeleton()
    df = optimize({"A": make_prices(n=500)}, code_spec(code), {"params.fast": [3, 5], "params.slow": [20, 30]},
                  BrokerConfig(), workers=2)
    assert len(df) == 4 and "error" not in df and df["total_return"].notna().all()


# ---------------- 出错 ----------------
@pytest.mark.parametrize("code, where", [
    ("", "空"),
    ("class Strategy(PerAsset)\n    pass\n", "第 1 行"),
    ("x = 1\n", "class Strategy"),
    ("class Strategy:\n    pass\n", "PerAsset"),
    ("import os\nclass Strategy(PerAsset):\n    y = undefined_name\n", "第 3 行"),
])
def test_check_reports_errors(code, where):
    errors = cs.check(code)
    assert errors and where in errors[0]
    with pytest.raises(CodeError):
        strategies.resolve(code_spec(code))


def test_syntax_check_does_not_execute():
    assert cs.check_syntax("class Strategy(PerAsset):\n    raise SystemExit\n") == []


def test_runtime_error_points_at_line():
    code = cs.skeleton().replace("holding = self.getposition(d).size > 0", "holding = 1 / 0")
    with pytest.raises(ZeroDivisionError) as ei:
        run_backtest(PRICES, *strategies.resolve(code_spec(code)), BROKER)
    msg = cs.explain_error(ei.value)
    line = next(i for i, ln in enumerate(code.splitlines(), 1) if "1 / 0" in ln)
    assert f"第 {line} 行" in msg and "1 / 0" in msg
    assert cs.explain_error(ValueError("x")) == "x"                   # 不是用户代码里的错误：原样


def test_optimize_reports_code_error_per_combo():
    code = cs.skeleton().replace("def next(self):", "def next(self):\n        assert self.p.fast != 3")
    df = optimize({"A": make_prices(n=300)}, code_spec(code), {"params.fast": [3, 5]}, BrokerConfig(), workers=1)
    assert "第" in df.loc[df["params.fast"] == 3, "error"].iloc[0]
    assert pd.isna(df.loc[df["params.fast"] == 5, "error"].iloc[0])


# ---------------- 导出 / 模拟盘 ----------------
def test_python_script_export_trades_identically():
    spec = code_spec(cs.skeleton(), fast=8)
    code = single_asset_script(spec, [{"name": n, "source": "csv", "path": f"{n}.csv"} for n in PRICES],
                               asdict(BROKER))
    assert "simplequant" not in code.split('"""', 2)[2]
    ns = exec_script(code)
    strat = ns["run"](PRICES)
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    ours = run_backtest(PRICES, *strategies.resolve(spec), BROKER).orders
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.reset_index(drop=True))


def test_python_script_export_with_kdj():
    rule = PRESETS["ma"]()
    rule["buy"]["conditions"].append({"left": {"ind": "kdj", "line": "j", "params": {"period": 9, "m1": 3, "m2": 3}},
                                      "op": "<", "right": {"value": 100}})
    spec = code_spec(cs.to_code({"kind": "rule", "rule": rule}))
    code = single_asset_script(spec, [{"name": "A", "source": "csv", "path": "A.csv"}], asdict(BROKER))
    assert "class KDJ" in code
    exec_script(code)["run"]({"A": PRICES["A"]})


def test_platform_export_refused_with_reason():
    with pytest.raises(ValueError, match="导出 Python 脚本"):
        platform_script("joinquant", code_spec(cs.skeleton()), [{"name": "A", "symbol": "510300", "asset": "etf"}],
                        asdict(BROKER), "2021-01-01", "2022-01-01")


def test_paper_account_with_code_strategy(tmp_path, monkeypatch):
    from simplequant.paper import account as account_mod, create_account, run_account
    monkeypatch.setattr(account_mod, "PAPER_ROOT", tmp_path / "paper")
    prices = {"510300": make_prices(n=300)}
    df = prices["510300"]
    cal = df.index.append(pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=20))
    spec = code_spec(cs.skeleton())
    start = df.index[100].date().isoformat()
    acc = create_account("代码", spec, BrokerConfig(), start,
                         assets=[{"source": "akshare", "symbol": "510300", "asset": "etf", "name": "510300"}])
    for i in (150, 220, 299):
        s = run_account(acc, cal, prices={"510300": df.iloc[:i + 1]})
        assert not s["divergence"]
    full = run_backtest(prices, *strategies.resolve(spec), BrokerConfig(), trade_start=start)
    assert len(acc.fills()) == len(full.orders) > 0
    for sig in s.get("signals") or []:                 # 一句话理由存成 [文本, {}]
        assert sig["reason"] is None or isinstance(sig["reason"], list)


def test_saved_code_strategy_roundtrip(tmp_path):
    spec = code_spec(cs.skeleton(), fast=6)
    strategies.save_strategy("我的代码", spec, tmp_path)
    back = strategies.list_strategies(tmp_path)["我的代码"]
    assert back == spec and strategies.runnable_on_single_assets(back)
