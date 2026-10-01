"""
择时回测的「现金分红」模式：行情换算、除息日记账、红利税、基准、不支持时保留原数据
"""

import pandas as pd
import pytest

from conftest import make_prices
from simplequant.data import cash_dividend as cd
from simplequant.data.library import DatasetMeta
from simplequant.engine import run_backtest, BrokerConfig, BaseStrategy
from simplequant.strategies import resolve

FREE = BrokerConfig(cash=100_000, commission=0, min_commission=0, stamp_duty=0, slippage=0)


def flat(prices, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(prices))
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1e6}, index=idx)
    df.index.name = "datetime"
    return df.astype(float)


def events(*rows):
    return pd.DataFrame([{"ex_date": pd.Timestamp(d), "cash": c, "bonus": b, "reserve": r} for d, c, b, r in rows],
                        columns=["ex_date", "cash", "bonus", "reserve"])


# 不复权价：10 元 → 第 5 天派 0.5 元（价格 9.5）→ 第 8 天 10 送 10（价格减半）→ 第 11 天每股派 0.1
RAW = flat([10.0] * 5 + [9.5] * 3 + [4.75] * 3 + [4.65] * 4)
DAYS = RAW.index
EVENTS = events((DAYS[5], 0.5, 0, 0), (DAYS[8], 0, 1.0, 0), (DAYS[11], 0.1, 0, 0))


def test_build_is_continuous_and_records_events():
    out = cd.build(RAW, RAW, EVENTS, taxed=True)
    assert out["close"].tolist() == pytest.approx([10.0] * 15)          # 除息、送股都不再跳空
    assert out["div_keep"].iloc[5] == pytest.approx(0.95)
    assert out["div_cash"].iloc[5] == pytest.approx(0.5)
    assert out["div_keep"].iloc[8] == 1.0 and out["div_cash"].iloc[8] == 0.0
    assert out["div_taxable"].iloc[8] == pytest.approx(1.0 / 0.95)       # 送股按 1 元面值计税
    # 一「行情股」此时相当于 2 / 0.95 股真实股票
    assert out["div_cash"].iloc[11] == pytest.approx(0.1 * 2 / 0.95)
    assert out["div_keep"].iloc[11] == pytest.approx(4.65 / 4.75)
    ev = cd.events_of(out)
    assert sorted(ev) == [DAYS[i].date() for i in (5, 8, 11)]
    assert cd.build(RAW, RAW, EVENTS, taxed=False)["div_taxable"].eq(0).all()


def test_build_minute_bars_marks_first_bar_of_the_day():
    idx = pd.DatetimeIndex([f"2024-01-0{d} {h}" for d in (2, 3, 4) for h in ("10:30", "11:30", "14:00", "15:00")])
    px = [10.0] * 4 + [9.5] * 8
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1.0}, index=idx)
    out = cd.build(df, df, events(("2024-01-03", 0.5, 0, 0)), taxed=False)
    assert out["close"].tolist() == pytest.approx([10.0] * 12)
    assert out["div_cash"].tolist() == [0.0] * 4 + [0.5] + [0.0] * 7


class BuyThenSell(BaseStrategy):
    params = (("sell_at", None),)

    def next(self):
        d, n = self.datas[0], len(self)
        if n == 1:
            self.order_target_pct(d, 0.5)
        if self.p.sell_at and n == self.p.sell_at:
            self.order_target_pct(d, 0.0)


def test_buy_and_hold_receives_cash_and_value_matches_real_account():
    prices = {"A": cd.build(RAW, RAW, EVENTS, taxed=False)}
    res = run_backtest(prices, BuyThenSell, {}, FREE)
    shares = 5000                                    # 半仓：第 2 天开盘以 10 元买入 50 手
    cash_left = 100_000 - shares * 10
    first, second = shares * 0.5, shares * 2 * 0.1   # 送股后 9800 股，每股 0.1
    assert res.metrics["dividend_cash"] == pytest.approx(first + second)
    assert res.metrics["dividend_tax"] == 0
    # 真实账户：9800 股 × 4.65 + 现金
    assert res.metrics["final_value"] == pytest.approx(cash_left + first + second + shares * 2 * 4.65)
    assert any(k == "log.dividend" for _, k, _ in res.logs)
    # 基准（买入持有、分红留作现金）：期末 = 4.65 × 2 + 0.5 + 0.2 = 10.0
    assert res.equity["benchmark"].iloc[-1] == pytest.approx(100_000 * 10.0 / 10.0)


def test_sell_deducts_dividend_tax_and_trade_pnl_includes_dividend():
    raw = flat([10.0] * 5 + [9.5] * 10)
    prices = {"A": cd.build(raw, raw, events((raw.index[5], 0.5, 0, 0)), taxed=True)}
    res = run_backtest(prices, BuyThenSell, {"sell_at": 9}, FREE)
    tax = 5000 * 0.5 * 0.20                          # 持股不到 1 个月：20%
    assert res.metrics["dividend_tax"] == pytest.approx(tax)
    assert res.metrics["final_value"] == pytest.approx(100_000 - tax)   # 价格跌掉的正好是到账的分红
    assert res.trades["pnl"].iloc[0] == pytest.approx(0.0, abs=1e-6)   # 平仓盈亏包含收到的分红
    assert res.orders["size"].iloc[-1] == pytest.approx(5000 * 0.95)    # 行情股数随分红缩减
    assert any(k == "log.dividend_tax" for _, k, _ in res.logs)


def test_partial_sell_taxes_only_the_sold_part():
    class HalfOut(BaseStrategy):
        def next(self):
            d, n = self.datas[0], len(self)
            if n == 1:
                self.order_target_pct(d, 0.5)
            if n == 9:
                self.sell(data=d, size=self.getposition(d).size / 2)

    raw = flat([10.0] * 5 + [9.5] * 10)
    prices = {"A": cd.build(raw, raw, events((raw.index[5], 0.5, 0, 0)), taxed=True)}
    res = run_backtest(prices, HalfOut, {}, FREE)
    assert res.metrics["dividend_tax"] == pytest.approx(5000 * 0.5 * 0.20 / 2)


def test_without_events_cash_mode_matches_plain_backtest():
    df = make_prices()
    cls, params = resolve({"kind": "template", "template": "sma_cross"})
    plain = run_backtest({"A": df}, cls, params, BrokerConfig())
    cash = run_backtest({"A": cd.build(df, df, cd.EMPTY, taxed=True)}, cls, params, BrokerConfig())
    assert plain.metrics["trades"] > 3
    pd.testing.assert_frame_equal(plain.orders, cash.orders)
    assert cash.metrics["final_value"] == pytest.approx(plain.metrics["final_value"])
    assert cash.metrics["dividend_cash"] == 0


@pytest.mark.parametrize("key", ["buy_hold", "sma_cross", "rsi_reversion", "boll_breakout", "momentum_rotation"])
def test_templates_run_in_cash_mode(key):
    datas = {}
    for i, name in enumerate("AB"):
        df = make_prices(seed=i + 1)
        ex = df.index[[100, 250]]
        datas[name] = cd.build(df, df, events((ex[0], 0.2, 0, 0), (ex[1], 0.1, 0.5, 0)), taxed=True)
    res = run_backtest(datas, *resolve({"kind": "template", "template": key}), BrokerConfig())
    assert res.metrics["final_value"] > 0
    assert (res.equity["cash"] >= -1e-6).all()


def test_unexplained_jump_detected():
    raw = flat([10.0] * 8 + [5.0] * 5)                       # 份额折算 1 拆 2，分红数据里没有
    hfq = flat([10.0] * 13)
    out = cd.build(raw, raw, cd.EMPTY, taxed=False)
    assert cd.unexplained(out, hfq, orig_adjusted=True) == [raw.index[8]]
    assert cd.unexplained(out, raw, orig_adjusted=False) == [raw.index[8]]
    assert cd.unexplained(cd.build(RAW, RAW, EVENTS, taxed=True), hfq.reindex(RAW.index, method="ffill"), True) == []


def test_asset_kind():
    def m(symbol, source="akshare", **extra):
        return DatasetMeta(id="x", name=symbol, symbol=symbol, source=source, freq="1d", extra=extra)
    assert cd.asset_kind(m("510300", asset="etf")) == "fund"
    assert cd.asset_kind(m("000300", asset="index")) == "index"
    assert cd.asset_kind(m("600519", source="baostock")) == "stock"
    assert cd.asset_kind(m("159915", source="tdx_local")) == "fund"
    assert cd.asset_kind(m("my.csv", source="csv")) is None


def test_prepare_keeps_original_when_unsupported(monkeypatch):
    raw = flat([10.0] * 5 + [9.5] * 5)
    hfq = flat([10.0] * 10)
    monkeypatch.setattr(cd, "fund_dividends", lambda s: events((raw.index[5], 0.5, 0, 0)))
    monkeypatch.setattr(cd, "unadjusted", lambda meta: raw)

    def fail(symbol, start_year):
        raise cd.Unsupported("no_dividends")
    monkeypatch.setattr(cd, "stock_dividends", fail)

    def meta(symbol, source="akshare", adjust="hfq", **extra):
        return DatasetMeta(id=symbol, name=symbol, symbol=symbol, source=source, freq="1d", adjust=adjust,
                           start="2024-01-02", end="2024-01-15", extra=extra)
    items = [("etf", meta("510300", asset="etf"), hfq), ("csv", meta("x", source="csv", adjust=""), hfq),
             ("idx", meta("000300", asset="index"), hfq), ("stk", meta("600519", source="baostock"), hfq)]
    prices, skipped, patched = cd.prepare(items)
    assert skipped == {"csv": "source", "idx": "index", "stk": "no_dividends"} and patched == {}
    assert cd.has_columns(prices["etf"]) and prices["etf"]["div_cash"].sum() == pytest.approx(0.5)
    assert prices["csv"] is hfq


def test_missing_dividend_inferred_from_adjusted_prices(monkeypatch):
    """分红表漏记的分红（如茅台的特别分红）按复权价补上；补不上的（份额折算）保留原数据"""
    raw = flat([10.0] * 5 + [9.5] * 3 + [9.0] * 5)
    hfq = flat([10.0] * 13)                                    # 两次除息复权价都连续
    monkeypatch.setattr(cd, "unadjusted", lambda meta: raw)
    monkeypatch.setattr(cd, "stock_dividends", lambda s, y: (events((raw.index[5], 0.5, 0, 0)), cd.EMPTY))   # 漏了第二次
    meta = DatasetMeta(id="s", name="s", symbol="600519", source="baostock", freq="1d", adjust="hfq",
                       start="2024-01-02", end="2024-01-18")
    out, patched = cd.prepare_one(meta, hfq)
    assert patched == [raw.index[8].date()]
    assert out["div_cash"].iloc[8] == pytest.approx(0.5 * 10 / 9.5)    # 每股 0.5 元 × 一行情股对应的真实股数
    assert out["close"].tolist() == pytest.approx([10.0] * 13)

    split = flat([10.0] * 8 + [5.0] * 5)                        # 份额折算：推算出的"分红"超过前收一半，不补
    monkeypatch.setattr(cd, "unadjusted", lambda meta: split)
    monkeypatch.setattr(cd, "stock_dividends", lambda s, y: (cd.EMPTY, cd.EMPTY))
    with pytest.raises(cd.Unsupported):
        cd.prepare_one(meta, flat([10.0] * 13))


def test_rebase_to_last_matches_raw_price_and_keeps_account_value():
    """模拟盘：缩放到最新价格 = 不复权价，账户（按股数不取整时）结果不变"""
    out = cd.build(RAW, RAW, EVENTS, taxed=True)
    reb = cd.rebase_to_last(out, RAW)
    assert reb["close"].iloc[-1] == pytest.approx(RAW["close"].iloc[-1])
    assert reb["div_keep"].tolist() == out["div_keep"].tolist()
    scale = out["close"].iloc[-1] / RAW["close"].iloc[-1]
    assert reb["div_cash"].iloc[11] == pytest.approx(out["div_cash"].iloc[11] / scale)
    a = run_backtest({"A": out}, BuyThenSell, {"lot_size": 1, "sell_at": 13}, FREE)
    b = run_backtest({"A": reb}, BuyThenSell, {"lot_size": 1, "sell_at": 13}, FREE)
    assert b.metrics["final_value"] == pytest.approx(a.metrics["final_value"], rel=1e-3)
    assert b.metrics["dividend_cash"] == pytest.approx(a.metrics["dividend_cash"], rel=1e-3)
