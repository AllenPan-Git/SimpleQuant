"""
组合模拟账户（资产配置第二期）
核心：逐日推进的结果与一次性推进完全一致，且与组合回测（allocation.backtest）一致
"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_paper import truncate_panel
from test_stocks import make_panel, SPEC
from simplequant import allocation as A
from simplequant.allocation import sleeves as SL
from simplequant.paper import account as account_mod, portfolio as PF, load_account

DAYS = 300
TIMING = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}


@pytest.fixture(autouse=True)
def tmp_paper_root(tmp_path, monkeypatch):
    monkeypatch.setattr(account_mod, "PAPER_ROOT", tmp_path / "paper")
    monkeypatch.setattr(SL, "find_dataset", lambda symbol: None)


def sleeves(selection=False):
    out = [{"id": "cash", "name": {"zh": "现金", "en": "Cash"}, "class": "cash", "kind": "cash", "rate": 0.015},
           {"id": "bond", "name": "国债", "class": "bond", "kind": "hold", "symbol": "B"},
           {"id": "tm", "name": "择时", "class": "equity", "kind": "timing", "symbol": "E", "strategy": "tpl:sma_cross"}]
    if selection:
        out.append({"id": "sel", "name": "选股", "class": "equity", "kind": "selection", "strategy": "x", "spec": SPEC})
    return out


WEIGHTS = {"cash": 0.2, "bond": 0.4, "tm": 0.25, "sel": 0.15}


class World:
    """假数据：两只 ETF 的行情 + 选股面板，可以截到某一天"""

    def __init__(self, monkeypatch):
        self.prices = {"B": make_prices(n=DAYS, seed=1, amp=0.02, drift=0.0002), "E": make_prices(n=DAYS, seed=2)}
        self.panel = make_panel(days=DAYS)
        idx = self.prices["B"].index
        self.cal = idx.append(pd.bdate_range(idx[-1] + pd.Timedelta(days=1), periods=60))
        self.cut = idx[-1]
        monkeypatch.setattr(PF, "load_selection_panel", lambda sub, store: truncate_panel(self.panel, self.cut))

    def at(self, acc, d):
        """把数据截到 d 并写进账户目录（代替 refresh_data）"""
        self.cut = pd.Timestamp(d)
        (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
        for sym, df in self.prices.items():
            df.loc[:d].to_parquet(acc.dir / "prices" / f"{sym}.parquet")


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def make(world, start_i, rebalance="monthly", selection=False, name="组合"):
    start = world.prices["B"].index[start_i].date().isoformat()
    return PF.create_account(name, sleeves(selection), WEIGHTS, rebalance, 100_000, start)


def test_create_freezes_strategy_and_normalizes_weights(world):
    acc = make(world, 100, selection=True)
    acc = load_account(acc.id)
    assert acc.kind == "portfolio" and acc.broker == {"cash": 100_000.0}
    by_id = {s["id"]: s for s in acc.spec["sleeves"]}
    assert by_id["tm"]["spec"] == {"kind": "template", "template": "sma_cross", "params": {}}
    assert by_id["bond"]["source"] == "akshare" and by_id["bond"]["asset"] == "etf"
    assert np.isclose(sum(acc.spec["weights"].values()), 1)
    zero = PF.create_account("零", sleeves(), {"cash": 1, "bond": 0, "tm": 0}, "none", 1000, acc.start)
    assert [s["id"] for s in zero.spec["sleeves"]] == ["cash"]
    with pytest.raises(ValueError):
        PF.create_account("空", sleeves(), {}, "none", 1000, acc.start)


@pytest.mark.parametrize("rebalance", ["monthly", "threshold"])
def test_daily_progression_equals_one_shot(world, rebalance):
    idx = world.prices["B"].index
    acc = make(world, 100, rebalance, selection=True)
    for i in list(range(100, DAYS, 6)) + [DAYS - 1]:
        world.at(acc, idx[i])
        s = PF.run_portfolio(acc, world.cal)
        assert not s["divergence"]
    one = make(world, 100, rebalance, selection=True, name="一次")
    world.at(one, idx[-1])
    s1 = PF.run_portfolio(one, world.cal)
    acc = load_account(acc.id)
    np.testing.assert_allclose(acc.nav()["value"].values, one.nav()["value"].values, rtol=1e-9)
    np.testing.assert_allclose(acc.nav()["benchmark"].values, one.nav()["benchmark"].values, rtol=1e-9)
    pd.testing.assert_frame_equal(acc.rebalances(), one.rebalances())
    assert len(acc.rebalances()) > 0 and s["value"] == pytest.approx(s1["value"])
    assert acc.data_through == idx[-1].date().isoformat()


def test_matches_allocation_backtest(world):
    """与组合回测一致：同样的成分收益、权重、再平衡规则（回测最后一天不再平衡，比较到倒数第二天）"""
    idx = world.prices["B"].index
    acc = make(world, 100)
    world.at(acc, idx[-1])
    PF.run_portfolio(acc, world.cal)
    start = idx[100]
    R = pd.DataFrame({
        "cash": PF._cash_returns(sleeves()[0], idx[idx > start]),
        "bond": world.prices["B"]["close"].pct_change().loc[idx > start],
        "tm": PF._replay_sleeve(acc, acc.spec["sleeves"][2], world.cal, None)["returns"],
    }).fillna(0.0)
    res = A.backtest(R, {"cash": 0.2, "bond": 0.4, "tm": 0.25}, "monthly", cash=100_000)
    nav = acc.nav()["value"]
    np.testing.assert_allclose(nav.values[1:-1], res.equity["value"].values[1:-1], rtol=1e-9)


def test_new_account_build_signals(world):
    idx = world.prices["B"].index
    acc = make(world, 150)
    world.at(acc, idx[150])
    s = PF.run_portfolio(acc, world.cal)
    assert s["value"] == 100_000 and s["execute_on"] == idx[151].date().isoformat()
    build = [x for x in s["signals"] if x["reason"] == ["pf.reason_build", {}]]
    # 买入持有成分按权重买入；择时成分刚开始还没有持仓，只有它自己的信号
    assert [x["sleeve"] for x in build] == ["bond"] and build[0]["side"] == "buy"
    assert build[0]["est_value"] == pytest.approx(100_000 * 0.4 / 0.85)
    rows = {r["id"]: r for r in s["sleeves"]}
    assert rows["cash"]["value"] == pytest.approx(100_000 * 0.2 / 0.85) and rows["tm"]["target"] == pytest.approx(0.25 / 0.85)


def test_rebalance_signal_at_period_end(world):
    idx = world.prices["B"].index
    acc = make(world, 100)
    month_end = next(d for i, d in enumerate(idx[:-1]) if i > 130 and idx[i + 1].month != d.month)
    i = idx.get_loc(month_end)
    world.at(acc, idx[i - 1])
    s0 = PF.run_portfolio(acc, world.cal)
    assert not s0["rebalance_due"] and s0["next_rebalance"] == month_end.date().isoformat()
    world.at(acc, month_end)
    s = PF.run_portfolio(acc, world.cal)
    assert s["rebalance_due"] and s["execute_on"] == idx[i + 1].date().isoformat()
    reb = [x for x in s["signals"] if x["reason"] == ["pf.reason_rebalance", {}]]
    assert reb and {x["sleeve"] for x in reb} <= {"bond", "tm"}
    # 再平衡后各成分回到目标权重
    for r in s["sleeves"]:
        assert r["weight"] == pytest.approx(r["target"], abs=1e-9)
    led = acc.rebalances()
    day = led[pd.to_datetime(led["time"]) == month_end]
    # 成本按调整金额分摊：每行成本 / 调整金额相同，合计等于本次再平衡成本
    assert len(day) > 1 and np.allclose(day["cost"] / day["amount"].abs(), (day["cost"] / day["amount"].abs()).iloc[0])
    assert day["cost"].sum() > 0


def test_divergence_when_history_changes(world):
    idx = world.prices["B"].index
    acc = make(world, 100, "none")
    world.at(acc, idx[150])
    PF.run_portfolio(acc, world.cal)
    world.prices["B"].loc[idx[120], "close"] *= 1.05          # 数据被修订
    world.at(acc, idx[160])
    assert PF.run_portfolio(acc, world.cal)["divergence"]


def test_next_rebalance():
    cal = pd.bdate_range("2026-01-01", "2026-12-31")
    assert PF.next_rebalance("quarterly", cal, "2026-02-10") == pd.Timestamp("2026-03-31")
    assert PF.next_rebalance("monthly", cal, "2026-03-31") == pd.Timestamp("2026-03-31")
    assert PF.next_rebalance("threshold", cal, "2026-02-10") is None


def test_run_all_dispatches_portfolio(world, monkeypatch):
    from simplequant.paper import runner
    idx = world.prices["B"].index
    acc = make(world, 100)
    world.at(acc, idx[120])
    monkeypatch.setattr(runner, "load_calendar", lambda: world.cal)
    monkeypatch.setattr(runner, "latest_expected_day", lambda cal: idx[120])
    out = runner.run_all([acc], store=object(), refresh=False, log=lambda m: None)
    assert out == {acc.id: ""} and load_account(acc.id).data_through == idx[120].date().isoformat()
