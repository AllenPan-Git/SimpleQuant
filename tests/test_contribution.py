"""
定投：定期追加资金（runner.contribution_flows、BaseStrategy._contribute / order_value）与对应的指标（XIRR、单位净值）
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.engine import BrokerConfig
from simplequant.engine.base_strategy import BaseStrategy
from simplequant.engine.results import xirr, unit_value
from simplequant.engine.runner import contribution_flows, run_backtest
from simplequant.strategies.templates import BuyHold


def prices(close, start="2022-01-03") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(close))
    c = pd.Series(np.asarray(close, dtype=float), index=idx)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6})


class Dca(BaseStrategy):
    """第一根 K 线和每笔追加资金到账当天，用全部可用现金买入（凑不满一手的零头留到下一期）"""
    def next(self):
        if len(self) == 1 or self.new_cash:
            self.order_value(self.datas[0], self.broker.getcash(), ("", {}))


def broker(**kw):
    return BrokerConfig(**{"cash": 10_000, "contribution": 2_000, "slippage": 0, "commission": 0,
                           "min_commission": 0, **kw})


def test_flows_first_day_of_each_period_after_the_first():
    df = prices(np.full(300, 10.0))                       # 2022-01-03 ~ 2023-02-24
    flows = contribution_flows({"a": df}, broker())
    assert flows.index[0] == pd.Timestamp("2022-02-01") and (flows == 2_000).all()
    assert len(flows) == 13 and flows.index[-1] == pd.Timestamp("2023-02-01")
    # 交易起点所在的那一期不追加
    later = contribution_flows({"a": df}, broker(), trade_start="2022-06-15")
    assert later.index[0] == pd.Timestamp("2022-07-01")
    weekly = contribution_flows({"a": df}, broker(contribution_freq="weekly"))
    assert weekly.index[0] == pd.Timestamp("2022-01-10") and len(weekly) == 59
    assert contribution_flows({"a": df}, broker(contribution=0)).empty


def test_xirr_and_unit_value():
    assert xirr(["2020-01-01", "2021-01-01"], [-100, 110]) == pytest.approx(0.10, abs=1e-3)
    # 两笔各投 100，期末 200：收益为 0
    assert xirr(["2020-01-01", "2020-07-01", "2021-01-01"], [-100, -100, 200]) == pytest.approx(0, abs=1e-6)
    assert np.isnan(xirr(["2020-01-01", "2021-01-01"], [-100, -10]))
    idx = pd.bdate_range("2022-01-03", periods=4)
    eq = pd.Series([100.0, 110.0, 160.0, 176.0], index=idx)          # 第 3 天追加 50
    nav = unit_value(eq, pd.Series([50.0], index=[idx[2]]))
    assert list(nav.round(6)) == [1.0, 1.1, 1.1, 1.21]


def test_flat_price_no_fake_return_or_drawdown():
    """价格不变：追加的钱不算收益，总资产涨了但回撤、收益都为 0"""
    res = run_backtest({"a": prices(np.full(300, 10.0))}, Dca, broker=broker())
    m = res.metrics
    assert m["invested"] == 10_000 + 13 * 2_000 and m["contributions"] == 13
    assert m["final_value"] == pytest.approx(m["invested"])
    assert m["total_return"] == pytest.approx(0) and m["xirr"] == pytest.approx(0, abs=1e-9)
    assert m["max_drawdown"] == pytest.approx(0) and res.equity["drawdown"].min() == pytest.approx(0)
    assert res.equity["invested"].iloc[-1] == m["invested"]
    assert sum(1 for _, k, _ in res.logs if k == "log.contribution") == 13
    buys = res.orders[res.orders["side"] == "buy"]
    # 留 1% 余量：初始 1 万只买 900 股，剩下的 1000 元和每期的 2000 元一起凑 2 手，零头一直滚到下一期
    assert len(buys) == 14 and buys["size"].iloc[0] == 900 and (buys["size"].iloc[1:] == 200).all()
    assert res.equity["cash"].iloc[-1] == pytest.approx(1_000)


def test_rising_price_matches_same_cashflow_benchmark():
    """每笔到账就买：与基准（同样的现金流买入持有）几乎一样；XIRR 高于 0、低于单位净值的年化"""
    close = 10 * 1.0005 ** np.arange(500)
    res = run_backtest({"a": prices(close)}, Dca, broker=broker())
    m = res.metrics
    assert m["xirr"] > 0 and m["benchmark_xirr"] > 0
    assert m["excess_return"] == pytest.approx(0, abs=0.01)           # 只差整手零头与次日开盘买入
    assert m["benchmark_return"] == pytest.approx(res.equity["benchmark"].iloc[-1] / m["invested"] - 1)
    assert m["total_return"] < m["twr"]       # 后投入的钱涨的时间短，按投入算的总收益低于时间加权收益


def test_cash_waits_when_strategy_ignores_it():
    """不理会追加资金的策略（买入持有只买一次）：钱留作现金，单位净值不因到账跳升"""
    res = run_backtest({"a": prices(10 * 1.001 ** np.arange(300))}, BuyHold, broker=broker())
    m = res.metrics
    assert len(res.orders) == 1 and res.equity["cash"].iloc[-1] > 13 * 2_000
    plain = run_backtest({"a": prices(10 * 1.001 ** np.arange(300))}, BuyHold, broker=broker(contribution=0))
    assert m["max_drawdown"] == pytest.approx(plain.metrics["max_drawdown"])
    assert "xirr" not in plain.metrics and "invested" not in plain.equity


def run_tpl(key, df, params=None, **kw):
    from simplequant.strategies import resolve
    cls, p = resolve({"kind": "template", "template": key, "params": params or {}})
    return run_backtest({"a": df}, cls, p, broker=broker(**kw.pop("broker", {})), **kw)


def test_dca_template_buys_every_contribution():
    res = run_tpl("dca", prices(10 * 1.0005 ** np.arange(300)))
    buys = res.orders[res.orders["side"] == "buy"]
    assert len(buys) == 14 and res.metrics["contributions"] == 13
    assert res.equity["cash"].iloc[-1] < 1_100                        # 只剩凑不满一手的零头
    # 两个标的平分
    from simplequant.strategies import resolve
    two = run_backtest({"a": prices(np.full(300, 10.0)), "b": prices(np.full(300, 5.0))},
                       *resolve({"kind": "template", "template": "dca"}), broker=broker())
    by = two.orders.groupby("symbol")["value"].sum()
    assert by["a"] == pytest.approx(by["b"], rel=0.1)


def valuation_prices():
    """价格恒为 1；市盈率前 300 天从 10 升到 20（预热），之后 100 天 25（高估）、再 100 天 9（低估）"""
    df = prices(np.full(500, 1.0))
    df["pe"] = np.r_[np.linspace(10, 20, 300), np.full(100, 25.0), np.full(100, 9.0)]
    return df


def test_value_dca_holds_back_when_expensive_and_spends_reserve_when_cheap():
    df = valuation_prices()
    start = df.index[300]
    res = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 0}, trade_start=start)
    buys = res.orders[res.orders["side"] == "buy"].set_index("time")["value"]
    dear = buys[buys.index < df.index[400]]
    cheap = buys[buys.index >= df.index[400]]
    assert dear.iloc[0] == pytest.approx(4_900)                     # 初始 1 万按 0.5 倍：投 5000（留 1% 余量取整手）
    assert (dear.iloc[1:] <= 1_100).all() and (dear.iloc[1:] >= 900).all()     # 每期 2000 只投一半
    # 低估：每期投 2 倍（多出的部分来自储备），储备用完后只投当期的钱
    # 高估 5 期攒下 5 × 1000 + 初始的 5000 → 低估时每期 2000 + 2000 储备
    assert (cheap == 4_000).all() and len(cheap) == 4
    assert res.pending[0]["size"] == 4_000                           # 最后一期的买单要到下一个开盘才成交
    assert res.equity["cash"].iloc[-1] == pytest.approx(4_100)
    reasons = [kw["reason"] for _, k, kw in res.logs if k == "log.buy"]
    assert reasons[0] == ("reason.value_dca", {"v": pytest.approx(100, abs=1), "m": 0.5})
    # 高估时暂停：这段时间只买初始资金之外的零头
    stop = run_tpl("value_dca", df, {"years": 1, "dear_mult": 0, "reserve_rate": 0}, trade_start=start)
    sb = stop.orders.set_index("time")["value"]
    assert sb[sb.index < df.index[400]].sum() == 0


def test_value_dca_reserve_earns_interest():
    """储备按年化收益复利计息：利息算收益不算投入，低估时连同利息一起投出去"""
    df = valuation_prices()
    start = df.index[300]
    plain = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 0}, trade_start=start)
    paid = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 10}, trade_start=start)
    assert paid.metrics["invested"] == plain.metrics["invested"]
    gain = paid.equity["value"].iloc[-1] - plain.equity["value"].iloc[-1]
    # 高估的约 140 个自然日储备从 5000 攒到约 1 万（利息约 290 元），低估时储备逐期用掉之前也在计息（约 200 元）
    assert 400 < gain < 600
    assert paid.metrics["xirr"] > plain.metrics["xirr"]
    # 没有追加资金也计息（初始资金高估时只投一半）
    alone = run_tpl("value_dca", df, {"years": 1, "reserve_rate": 10}, trade_start=start,
                    broker={"contribution": 0})
    assert alone.equity["cash"].iloc[-1] > 5_000


def test_value_dca_without_valuation_acts_like_dca():
    df = prices(10 * 1.0005 ** np.arange(300))
    plain, val = run_tpl("dca", df), run_tpl("value_dca", df)
    assert plain.orders["size"].tolist() == val.orders["size"].tolist()
    assert sum(1 for _, k, _ in val.logs if k == "log.no_pe_pct") == 1


@pytest.mark.parametrize("key, params", [("dca", {}), ("value_dca", {"years": 1})])
def test_script_export_with_contributions_matches(key, params, capsys, tmp_path, monkeypatch):
    """导出的脚本按同样的日期追加资金（估值定投连同储备利息），成交与软件里的回测逐笔相同"""
    monkeypatch.chdir(tmp_path)                          # report() 把成交记录写到当前目录的 orders.csv
    from dataclasses import asdict
    from simplequant.export.python_script import single_asset_script
    from tests.test_export import exec_script
    df = valuation_prices()
    df["close"] = df["open"] = df["high"] = df["low"] = 10 * 1.0005 ** np.arange(len(df))
    b = broker(commission=0.00025, min_commission=5)
    spec = {"kind": "template", "template": key, "params": params}
    code = single_asset_script(spec, [{"name": "a", "source": "csv", "path": "a.csv"}], asdict(b))
    ns = exec_script(code)
    strat = ns["run"]({"a": df})
    exported = pd.DataFrame(strat.order_records, columns=ns["ORDER_COLUMNS"])
    from simplequant.strategies import resolve
    ours = run_backtest({"a": df}, *resolve(spec), broker=b)
    assert len(ours.orders) > 20
    pd.testing.assert_frame_equal(exported.reset_index(drop=True), ours.orders.reset_index(drop=True))
    ns["report"](strat)
    out = capsys.readouterr().out
    assert f"{ours.metrics['invested']:,.2f}" in out and f"{ours.metrics['xirr']:.2%}" in out
    assert f"{ours.metrics['total_return']:.2%}" in out


def test_value_dca_warm_up_uses():
    from gui.components import _pct_uses
    assert _pct_uses({"kind": "template", "template": "value_dca", "params": {"years": 3}}) == \
        [{"ind": "pe_pct", "params": {"years": 3}}]
    assert _pct_uses({"kind": "template", "template": "dca"}) == []


def test_apply_flows_inverts_unit_value():
    from simplequant.engine.results import apply_flows
    idx = pd.bdate_range("2022-01-03", periods=4)
    eq = pd.Series([100.0, 110.0, 160.0, 176.0], index=idx)
    flows = pd.Series([50.0], index=[idx[2]])
    assert list(apply_flows(unit_value(eq, flows), flows, 100).round(6)) == list(eq)


def test_optimize_and_walk_forward_ignore_contributed_cash():
    """价格不变：参数优化的逐年收益、滚动优化拼出的样本外收益都不能把追加的钱算成收益"""
    from simplequant.engine.optimize import optimize
    from simplequant.engine.walkforward import walk_forward
    df = prices(np.full(800, 10.0), start="2020-01-01")
    df["pe"] = np.linspace(10, 20, 800)                  # 一直高估：储备不计息，否则利息是真实收益
    spec = {"kind": "template", "template": "value_dca", "params": {"reserve_rate": 0}}
    grid = optimize({"a": df}, spec, {"params.years": [1, 2]}, broker(), workers=1)
    years = [c for c in grid.columns if c.startswith("year:")]
    assert len(years) >= 3 and np.allclose(grid[years].to_numpy(dtype=float), 0, atol=1e-9)
    assert np.allclose(grid["total_return"], 0, atol=1e-9)
    res = walk_forward({"a": df}, spec, {"params.years": [1, 2]}, {"params.years": True}, broker(),
                       metric="total_return", train_months=12, test_months=6, workers=1)
    eq, m = res.equity, res.metrics
    assert m["invested"] == pytest.approx(eq["invested"].iloc[-1]) and m["contributions"] > 10
    assert eq["value"].iloc[-1] == pytest.approx(eq["invested"].iloc[-1], rel=1e-6)
    assert m["total_return"] == pytest.approx(0, abs=1e-6) and m["xirr"] == pytest.approx(0, abs=1e-4)
    assert eq["drawdown"].min() == pytest.approx(0, abs=1e-9)
    assert res.baseline_metrics["total_return"] == pytest.approx(0, abs=1e-6)
    with pytest.raises(ValueError, match="延续持仓"):
        walk_forward({"a": df}, spec, {"params.years": [1, 2]}, {"params.years": True}, broker(),
                     metric="total_return", train_months=12, test_months=6, workers=1, stitch="restart")
