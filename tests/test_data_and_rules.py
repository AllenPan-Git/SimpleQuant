import io

import pandas as pd
import pytest

from conftest import make_prices
from simplequant.data import normalize, snapshot_to_bars, resample_bars, library
from simplequant.data.base import exchange_prefix
from simplequant.data.csv_src import CSVSource
from simplequant.rules import validate, describe_rule, make_ind
from simplequant import strategies


def test_normalize_chinese_columns():
    raw = pd.DataFrame({"日期": ["2024-01-03", "2024-01-02"], "开盘": [1, 2], "收盘": [1.5, 2.5],
                        "最高": [2, 3], "最低": [1, 2], "成交量": [100, 200], "成交额": [9, 9]})
    df = normalize(raw)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing and df.index.name == "datetime"


@pytest.mark.parametrize("code,ex", [("510300", "sh"), ("600519", "sh"), ("159915", "sz"),
                                     ("000001", "sz"), ("019742", "sh"), ("830799", "bj")])
def test_exchange_prefix(code, ex):
    assert exchange_prefix(code) == ex


def test_snapshot_resample():
    """3 秒快照：累计成交量差分 + 剔除 last=0 的无效快照"""
    t = pd.date_range("2026-01-05 09:30:00", periods=40, freq="3s")
    snap = pd.DataFrame({"code": "019742.SH", "trade_time": t,
                         "last": [0.0] + [100 + i * 0.01 for i in range(39)],
                         "volume": [0] + list(range(10, 400, 10))})
    bars = snapshot_to_bars(snap, "1min")
    assert len(bars) == 2
    first = bars.iloc[0]
    assert bars.index[0] == pd.Timestamp("2026-01-05 09:31:00")   # 区间结束时刻
    assert first["open"] == pytest.approx(100.0) and first["close"] == pytest.approx(100.18)
    assert bars["volume"].sum() == pytest.approx(390)


def test_snapshot_trims_stale_bars_outside_trading():
    """收盘后仍在推送的快照（成交量不再增加）不应生成 K 线"""
    t = pd.date_range("2026-01-05 14:58:00", periods=200, freq="3s")   # 14:58 ~ 15:07:57
    vol = [min(i, 40) * 100 for i in range(200)]                        # 前 2 分钟有成交，之后不变
    bars = snapshot_to_bars(pd.DataFrame({"trade_time": t, "last": 100.0, "volume": vol}), "1min")
    assert bars.index[-1] <= pd.Timestamp("2026-01-05 15:01:00")
    assert (bars["volume"] > 0).all()


def test_csv_source_detects_snapshot_and_kline():
    kline = make_prices(n=30).reset_index()
    buf = io.StringIO(kline.to_csv(index=False))
    assert len(CSVSource().fetch(buf)) == 30

    t = pd.date_range("2026-01-05 09:30:00", periods=100, freq="3s")
    snap = pd.DataFrame({"trade_time": t, "last": 100.0, "volume": range(100)})
    buf = io.StringIO(snap.to_csv())   # 带 pandas 无名索引列
    assert len(CSVSource().fetch(buf, resample="1min")) == 5


def test_resample_bars():
    idx = pd.date_range("2024-01-02 09:31", periods=10, freq="min")
    df = make_prices(n=10)
    df.index = idx
    out = resample_bars(df, "5min")
    assert out["volume"].sum() == pytest.approx(df["volume"].sum())
    assert out["high"].max() == pytest.approx(df["high"].max())


@pytest.fixture
def fake_akshare(monkeypatch):
    """东方财富接口连不上、新浪可用（不联网）"""
    import akshare as ak
    from simplequant.data import akshare_src

    def em_down(**kw):
        raise ConnectionError("Remote end closed connection without response")
    sina = pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=5).strftime("%Y-%m-%d"),
                         "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05, "volume": 1e6})
    monkeypatch.setattr(ak, "fund_etf_hist_em", em_down)
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol: sina)
    # 新浪累计分红：2024-01-04 除息 0.1（之前还有一次，累计 0.3 → 0.4）
    monkeypatch.setattr(ak, "fund_etf_dividend_sina", lambda symbol: pd.DataFrame(
        {"日期": ["2023-06-01", "2024-01-04"], "累计分红": [0.3, 0.4]}))
    monkeypatch.setattr(akshare_src, "with_retry",
                        lambda fn, retries=3, wait=1.5: _retry_no_sleep(fn, retries))


def _retry_no_sleep(fn, retries):
    err = None
    for _ in range(retries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            err = e
    raise RuntimeError(str(err)) from err


def test_akshare_sina_fallback_rebuilds_adjusted_prices(fake_akshare):
    """东方财富连不上：ETF 改用新浪行情 + 新浪累计分红，按东方财富口径（加法）算复权价；成交量换算成手"""
    from simplequant.data import AKShareSource
    src = AKShareSource()
    raw = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="", asset="etf")
    assert len(raw) == 5 and raw.attrs["provider"] == "sina"
    assert raw["volume"].iloc[0] == pytest.approx(1e4)                       # 1e6 股 = 1e4 手
    hfq = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf")
    assert hfq.attrs["provider"] == "sina+dividends"
    # 1/2、1/3 只经历过第一次分红（+0.3）；1/4 除息当天起累计 +0.4
    assert hfq["close"].round(3).tolist() == [4.35, 4.35, 4.45, 4.45, 4.45]
    qfq = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf")
    assert qfq["close"].round(3).tolist() == [3.95, 3.95, 4.05, 4.05, 4.05]  # 最新价格等于真实价格
    assert (hfq["close"] - qfq["close"]).round(6).nunique() == 1             # 两种复权只差一个常数


def test_akshare_both_sources_down(fake_akshare, monkeypatch):
    import akshare as ak
    from simplequant.data import AKShareSource

    def down(**kw):
        raise ConnectionError("sina down")
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol: down())
    with pytest.raises(RuntimeError, match="都暂时连不上"):
        AKShareSource().fetch("510300", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf")


def test_library_records_provider(fake_akshare, tmp_path, monkeypatch):
    from simplequant.data import fetch_to_library
    monkeypatch.setattr(library, "LIB_DIR", tmp_path)
    monkeypatch.setattr(library.save, "__defaults__", (tmp_path,))
    monkeypatch.setattr(library.exists, "__defaults__", (tmp_path,))
    monkeypatch.setattr(library.list_datasets, "__defaults__", (tmp_path,))
    meta = fetch_to_library("akshare", "510300", "2024-01-01", "2024-01-31", adjust="", asset="etf")
    assert meta.extra["provider"] == "sina"


def test_download_drops_unfinished_intraday_bar(tmp_path, monkeypatch):
    """交易时间内下载日线：数据源返回了当天未走完的 K 线，存进数据库前要去掉"""
    from simplequant.data import SOURCES, fetch_to_library
    from simplequant.paper import calendar as cal_mod
    today = pd.Timestamp("2026-09-29")
    df = make_prices(n=30, start=pd.bdate_range(end=today, periods=30)[0])

    class Fake:
        def fetch(self, symbol, start, end, **kw):
            out = df.copy()
            out.attrs["provider"] = "eastmoney"
            return out
    monkeypatch.setitem(SOURCES, "akshare", Fake())
    monkeypatch.setattr(cal_mod, "load_calendar", lambda *a, **k: df.index)
    monkeypatch.setattr(cal_mod, "latest_expected_day", lambda cal: pd.Timestamp("2026-09-28"))
    for f in (library.save, library.exists, library.list_datasets):
        monkeypatch.setattr(f, "__defaults__", (tmp_path,))
    meta = fetch_to_library("akshare", "510300", "2026-08-01", "2026-09-29", adjust="hfq", asset="etf")
    assert meta.end == "2026-09-28" and meta.rows == 29
    assert library.load(meta.id, tmp_path).index[-1] == pd.Timestamp("2026-09-28")


def test_library_roundtrip(tmp_path):
    df = make_prices(n=20)
    meta = library.DatasetMeta(id=library.make_id("csv", "x", "1d", "", "a", "b"), name="X",
                               symbol="x", source="csv", freq="1d")
    library.save(df, meta, tmp_path)
    assert library.list_datasets(tmp_path)[0].rows == 20
    pd.testing.assert_frame_equal(library.load(meta.id, tmp_path), df, check_freq=False)
    library.delete(meta.id, tmp_path)
    assert library.list_datasets(tmp_path) == []


def test_validate_rule():
    assert validate({"buy": {"conditions": []}}) == ["买入条件至少需要一条"]
    assert validate({"buy": {"conditions": []}}, "en") == ["At least one buy condition is required"]
    bad = {"buy": {"conditions": [{"left": {"ind": "pnl_pct"}, "op": "cross_above", "right": {"value": 1}}]}}
    assert any("持仓收益率" in e for e in validate(bad))
    bad2 = {"buy": {"conditions": [{"left": {"value": 1}, "op": "foo", "right": {"ind": "nope"}}]}}
    assert len(validate(bad2)) == 3


def test_describe_rule():
    rule = {"buy": {"logic": "all", "conditions": [
        {"left": make_ind("macd"), "op": "cross_above", "right": make_ind("macd", ) | {"line": "dea"}}]},
        "sell": {"logic": "any", "conditions": []}, "position_pct": 80}
    text = describe_rule(rule)
    assert "MACD(12,26,9)·DIF 上穿 MACD(12,26,9)·DEA" in text
    assert "仓位】80%" in text


def test_strategy_store(tmp_path):
    spec = {"kind": "template", "template": "sma_cross", "params": {"fast": 10}}
    strategies.save_strategy("我的/均线", spec, tmp_path)
    assert strategies.list_strategies(tmp_path) == {"我的/均线": spec}
    strategies.delete_strategy("我的/均线", tmp_path)
    assert strategies.list_strategies(tmp_path) == {}
