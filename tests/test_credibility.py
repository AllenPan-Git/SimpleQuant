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


def test_open_positions_loss_and_gain():
    from simplequant.engine.credibility import open_pnl
    pnl = [3000, 2000, 1000, -500, 1500, 800]                      # 已平仓 +7.8%
    lose = [{"symbol": "A", "size": 1000, "price": 9.0, "cost": 15.0}]      # 浮亏 6000 = -6%
    assert open_pnl(lose, 1e5) == -0.06
    k = keys(check_backtest({"total_return": 0.018, "initial_cash": 1e5}, trades(pnl), positions=lose))
    assert k["open_loss"]["level"] == "warn"
    assert k["open_loss"]["args"] == {"k": 1, "open": -0.06, "closed": 0.078, "total": 0.018}
    gain = [{"symbol": "A", "size": 1000, "price": 25.0, "cost": 15.0}]     # 浮盈 10%，超过总收益一半
    k = keys(check_backtest({"total_return": 0.178, "initial_cash": 1e5}, trades(pnl), positions=gain))
    assert k["open_gain"]["level"] == "note" and "open_loss" not in k
    small = [{"symbol": "A", "size": 100, "price": 14.0, "cost": 15.0}]     # 浮亏 0.1%：不提示
    k = keys(check_backtest({"total_return": 0.077, "initial_cash": 1e5}, trades(pnl), positions=small))
    assert "open_loss" not in k and "open_gain" not in k
    k = keys(check_backtest({"total_return": -0.06, "initial_cash": 1e5}, trades([]), positions=lose))
    assert set(k) == {"no_trades"}                                   # 没有已平仓交易：只说 no_trades


def test_check_grid_years():
    df = pd.DataFrame({"sharpe": np.linspace(0, 1, 10), "total_return": np.linspace(0.01, 0.2, 10)})
    years = pd.DataFrame({"year": [2020, 2021, 2022, 2023], "ret": [0.3, -0.1, -0.05, 0.0],
                          "rank": [1, 9, 8, 6], "n": [10] * 4, "top": [0.3] * 4, "median": [0.0] * 4})
    k = keys(check_grid(df, "sharpe", {"is_best": {}}, years))
    assert k["years_unstable"]["level"] == "warn"
    assert k["years_unstable"]["args"] == {"years": 4, "upper": 1, "first": 1}
    years["rank"] = [1, 2, 5, 9]
    assert keys(check_grid(df, "sharpe", {"is_best": {}}, years))["years_stable"]["args"]["upper"] == 3
    assert not {"years_stable", "years_unstable"} & set(keys(check_grid(df, "sharpe", {"is_best": {}}, years.head(2))))


def _wave_prices(n=400):
    idx = pd.bdate_range("2020-01-01", periods=n)
    px = 10 + 3 * np.sin(np.arange(n) * 2 * np.pi / 40)
    return {"A": pd.DataFrame({"open": px, "close": px}, index=idx)}


def _wave_trades(prices, phase):
    """每个周期买一次、持有 10 根：phase=35 从低位涨到高位（最好的时机），phase=15 反过来（最差）"""
    idx = prices["A"].index
    rows = [{"symbol": "A", "open_time": idx[s], "close_time": idx[s + 10], "bars": 10, "pnl": 0, "pnl_net": 0}
            for s in range(phase, len(idx) - 11, 40)]
    return pd.DataFrame(rows)


def test_random_rank():
    from simplequant.engine.credibility import random_rank
    prices = _wave_prices()
    good = random_rank(prices, _wave_trades(prices, 35))
    bad = random_rank(prices, _wave_trades(prices, 15))
    assert good["n"] == 9 and bad["n"] == 10 and good["runs"] == 1000
    assert good["beat"] > 0.99 and bad["beat"] < 0.01
    assert random_rank(prices, _wave_trades(prices, 35), seed=0) == good           # 固定种子，结果可复现
    assert random_rank(prices, _wave_trades(prices, 35).head(3)) is None           # 笔数太少不做
    assert random_rank({}, _wave_trades(prices, 35)) is None


def test_check_backtest_random_levels():
    prices = _wave_prices()
    m = {"total_return": 0.1, "initial_cash": 1e5}
    assert "random_ok" in keys(check_backtest(m, _wave_trades(prices, 35), prices=prices))
    k = keys(check_backtest(m, _wave_trades(prices, 15), prices=prices))
    assert k["random_bad"]["level"] == "warn" and k["random_bad"]["args"]["n"] == 10
    assert not {"random_ok", "random_mid", "random_bad"} & set(keys(check_backtest(m, _wave_trades(prices, 35))))


def test_random_rank_on_real_backtest():
    """真实回测的交易记录（时间戳来自 backtrader）能对上 K 线"""
    from conftest import make_prices
    from simplequant import strategies
    from simplequant.engine import run_backtest, BrokerConfig
    from simplequant.engine.credibility import random_rank
    cls, params = strategies.resolve({"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}})
    res = run_backtest({"A": make_prices()}, cls, params, BrokerConfig())
    r = random_rank(res.prices, res.trades)
    assert r is not None and r["n"] == len(res.trades) and 0 <= r["beat"] <= 1
