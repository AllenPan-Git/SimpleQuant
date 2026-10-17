"""调仓日检验（engine/phase.py + credibility.check_phase）"""

import pandas as pd

from simplequant.engine import run_backtest, BrokerConfig
from simplequant.engine.credibility import check_phase
from simplequant.engine.phase import phases, timing_cycle, selection_cycle, run_timing, MAX_PHASES
from simplequant.stocks.selection import rebalance_dates
from simplequant.strategies import resolve
from conftest import make_prices


def rot(every=5):
    return {"kind": "template", "template": "momentum_rotation", "params": {"period": 20, "rebalance_bars": every}}


def test_phases_cover_cycle_and_cap():
    assert phases(1) == [] and phases(5) == [1, 2, 3, 4]
    ks = phases(60)
    assert len(ks) == MAX_PHASES - 1 and ks[0] > 0 and ks[-1] < 60 and ks == sorted(set(ks))


def test_cycles():
    assert timing_cycle(rot(7)) == 7
    assert timing_cycle({"kind": "template", "template": "fixed_weight", "params": {}}) == 20      # 默认值
    assert timing_cycle({"kind": "template", "template": "sma_cross", "params": {}}) == 0
    assert timing_cycle({"kind": "rule", "rule": {}}) == 0
    assert selection_cycle({"rebalance": "monthly"}) == 20 and selection_cycle({"rebalance": "weekly"}) == 5
    assert selection_cycle({"rebalance": 10}) == 10


def test_rebalance_dates_phase():
    cal = pd.bdate_range("2024-01-01", "2024-04-30")
    assert list(rebalance_dates(cal, 10, 3)) == list(cal[3::10])
    base = rebalance_dates(cal, "monthly")
    assert list(rebalance_dates(cal, "monthly", 0)) == list(base)
    early = rebalance_dates(cal, "monthly", 2)                  # 每月倒数第 3 个交易日
    assert len(early) == len(base)
    assert all(cal.get_loc(b) - cal.get_loc(e) == 2 for b, e in zip(base, early))
    assert len(rebalance_dates(cal, "weekly", 4)) == len(rebalance_dates(cal, "weekly"))


def test_rotation_phase_shifts_rebalance_days():
    prices = {"A": make_prices(n=300, seed=1, period=40), "B": make_prices(n=300, seed=2, period=55)}
    broker = BrokerConfig()
    cls, params = resolve(rot(5))
    days = []
    for k in (0, 2):
        res = run_backtest(prices, cls, {**params, "phase": k}, broker)
        days.append(set(res.orders["time"]))
    assert days[0] != days[1]
    rows = run_timing(prices, rot(3), broker)                   # 2 个相位，单进程
    assert list(rows["phase"]) == [1, 2] and rows["total_return"].notna().all()
    one = run_backtest(prices, cls, {**resolve(rot(3))[1], "phase": 1}, broker)
    assert rows["total_return"].iloc[0] == one.metrics["total_return"]
    assert run_timing(prices, {"kind": "template", "template": "sma_cross", "params": {}}, broker).empty


def _rows(rets, dd=-0.2):
    return pd.DataFrame({"phase": range(1, len(rets) + 1), "total_return": rets, "max_drawdown": dd})


def keys(found):
    return [f["key"].split(".", 1)[1] for f in found]


def test_check_phase_levels():
    m = {"total_return": 2.67, "max_drawdown": -0.355}           # 第十六期二八轮动 20/5，2014 起
    found = check_phase(m, _rows([1.41, 1.31, 1.31, 2.24], -0.42))
    assert keys(found) == ["phase_high"] and found[0]["level"] == "warn"
    a = found[0]["args"]
    assert a["n"] == 5 and a["m"] == 4 and a["rank"] == 1 and a["hi"] == 2.67 and a["lo"] == 1.31
    assert a["dd_lo"] == -0.42 and a["dd_hi"] == -0.355
    assert keys(check_phase({"total_return": -0.054, "max_drawdown": -0.3},
                            _rows([0.208, 0.038, 0.213, 0.223]))) == ["phase_low"]
    assert keys(check_phase({"total_return": 0.30}, _rows([0.31, 0.29, 0.32, 0.30]))) == ["phase_flat"]
    assert keys(check_phase({"total_return": 0.30}, _rows([0.20, 0.45, 0.30, 0.28]))) == ["phase_wide"]
    assert check_phase({"total_return": 0.3}, _rows([0.2])) == []                    # 相位太少
