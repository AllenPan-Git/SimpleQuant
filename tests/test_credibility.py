"""回测可信度检查"""

import numpy as np
import pandas as pd

from simplequant.engine.credibility import trade_stats, check_backtest, check_grid, BOOT_RUNS


def trades(pnls):
    return pd.DataFrame({"pnl_net": pnls})


def keys(findings):
    return {f["key"].split(".", 1)[1]: f for f in findings}


def test_trade_stats_top_and_rest():
    s = trade_stats(trades([5000, -1000, 3000, 200, -500, 8000]), 100_000, 0.20)
    assert s["n"] == 6 and s["top_n"] == 3
    assert np.isclose(s["top_return"], 0.16)
    assert np.isclose(s["ex_top_return"], 0.04)           # 未平仓部分（0.20 − 0.147）也算在里面
    assert s["boot_lo"] <= 0.20 <= s["boot_hi"]


def test_bootstrap_reproducible_and_centered():
    pnl = list(np.random.default_rng(1).normal(100, 1000, 50))
    a = trade_stats(trades(pnl), 100_000, sum(pnl) / 100_000)
    b = trade_stats(trades(pnl), 100_000, sum(pnl) / 100_000)
    assert a == b                                          # 同一结果每次显示相同的区间
    assert a["boot_lo"] < sum(pnl) / 100_000 < a["boot_hi"]
    assert 0 < a["boot_loss"] < 1


def test_no_and_few_trades():
    assert set(keys(check_backtest({"total_return": 0.1, "initial_cash": 1e5}, trades([])))) == {"no_trades"}
    k = keys(check_backtest({"total_return": 0.05, "initial_cash": 1e5}, trades([3000, 2000])))
    assert k["few_trades"]["level"] == "warn" and k["few_trades"]["args"]["n"] == 2
    assert "boot_ok" not in k and "boot_bad" not in k     # 太少不做重抽样


def test_concentrated_profit_warns():
    pnl = [30000, 20000] + [-1000] * 23                    # 两笔大赚，其余小亏
    k = keys(check_backtest({"total_return": 0.27, "initial_cash": 1e5}, trades(pnl)))
    assert k["some_trades"]["level"] == "note"
    assert k["concentrated"]["level"] == "warn" and k["concentrated"]["args"]["rest"] < 0


def test_steady_profit_passes():
    pnl = [1000 + 100 * (i % 5) for i in range(40)]
    k = keys(check_backtest({"total_return": sum(pnl) / 1e5, "initial_cash": 1e5}, trades(pnl)))
    assert {"enough_trades", "spread", "boot_ok"} <= set(k)
    assert all(f["level"] == "ok" for f in k.values())
    assert k["boot_ok"]["args"]["runs"] == BOOT_RUNS


def test_luck_dependent_profit_warns():
    pnl = [5000, -4800] * 20 + [600]
    k = keys(check_backtest({"total_return": sum(pnl) / 1e5, "initial_cash": 1e5}, trades(pnl)))
    assert k["boot_bad"]["level"] == "warn" and k["boot_bad"]["args"]["loss"] >= 0.25


def test_tuned_note():
    k = keys(check_backtest({"total_return": 0.0, "initial_cash": 1e5}, trades([]), tried=60))
    assert k["tuned"]["level"] == "warn" and k["tuned"]["args"]["n"] == 60
    assert keys(check_backtest({"total_return": 0.0, "initial_cash": 1e5}, trades([]), tried=6))["tuned"]["level"] == "note"
    assert "tuned" not in keys(check_backtest({"total_return": 0.0, "initial_cash": 1e5}, trades([])))


def test_check_grid():
    df = pd.DataFrame({"sharpe": np.linspace(-1, 1.5, 30), "total_return": np.linspace(-0.3, 0.2, 30)})
    k = keys(check_grid(df, "sharpe", {"is_best": {"sharpe": 1.5, "cagr": 0.1}}))
    assert k["grid_mostly_lose"]["level"] == "warn" and k["grid_mostly_lose"]["args"]["n"] == 30
    assert "no_oos" in k

    df["total_return"] = np.linspace(0.01, 0.2, 30)
    cmp = {"is_best": {"sharpe": 1.5, "cagr": 0.1}, "oos_best": {"sharpe": 0.2, "cagr": 0.01}, "oos_orig": {}}
    k = keys(check_grid(df, "sharpe", cmp))
    assert k["grid_many"]["args"]["share"] == 1.0
    assert k["oos_worse"]["level"] == "warn" and k["oos_worse"]["args"]["oos_cagr"] == 0.01
    cmp["oos_best"] = {"sharpe": 1.2, "cagr": 0.08}
    assert "oos_ok" in keys(check_grid(df.head(5), "sharpe", cmp))
    assert "grid_many" not in keys(check_grid(df.head(5), "sharpe", cmp))     # 组数少不提示


def test_losing_backtest_only_gives_range():
    pnl = [5000, -5200] * 20
    k = keys(check_backtest({"total_return": sum(pnl) / 1e5, "initial_cash": 1e5}, trades(pnl)))
    assert k["boot_neg"]["level"] == "note" and "boot_bad" not in k
