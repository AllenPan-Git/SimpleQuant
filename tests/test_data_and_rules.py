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
    """东方财富接口连不上、新浪可用（不联网）；返回东方财富被调用的次数"""
    import akshare as ak
    from simplequant.data import net, sina

    em_calls = []

    def em_down(**kw):
        em_calls.append(kw)
        raise ConnectionError("Remote end closed connection without response")
    bars = pd.DataFrame({"open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05, "volume": 1e6},
                        index=pd.bdate_range("2024-01-02", periods=5))
    monkeypatch.setattr(ak, "fund_etf_hist_em", em_down)
    monkeypatch.setattr(ak, "stock_zh_a_hist", em_down)
    monkeypatch.setattr(sina, "kline", lambda sym, index=False: bars.copy())
    # 新浪复权表：ETF 的 f 恒为 1，u 为累计分红：2024-01-04 除息 0.1（之前还有一次，累计 0.3 → 0.4）
    monkeypatch.setattr(sina, "adjust_table", lambda sym: pd.DataFrame(
        {"f": [1.0, 1.0], "u": [0.3, 0.4]}, index=pd.to_datetime(["2023-06-01", "2024-01-04"])))
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    return em_calls


def test_akshare_sina_fallback_rebuilds_adjusted_prices(fake_akshare):
    """东方财富连不上：ETF 改用新浪行情 + 新浪累计分红，等比复权（ratio=False 时按东方财富口径加法）；成交量换算成手"""
    from simplequant.data import AKShareSource
    src = AKShareSource()
    raw = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="", asset="etf")
    assert len(raw) == 5 and raw.attrs["provider"] == "sina"
    assert raw["volume"].iloc[0] == pytest.approx(1e4)                       # 1e6 股 = 1e4 手
    hfq = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf")
    assert hfq.attrs["provider"] == "sina+dividends" and hfq.attrs["adjust_method"] == "ratio"
    # 1/4 除息 0.1：因子乘以 前收 / (前收 − 0.1) = 4.05 / 3.95（上市前那次分红在数据之前，不影响）
    assert hfq["close"].round(4).tolist() == [4.05, 4.05, 4.1525, 4.1525, 4.1525]
    qfq = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf")
    assert qfq["close"].round(3).tolist() == [3.95, 3.95, 4.05, 4.05, 4.05]  # 最新价格等于真实价格
    assert (hfq["close"] / qfq["close"]).round(9).nunique() == 1             # 两种复权只差一个比例

    # 加法（改为等比之前开设的模拟盘账户）：1/2、1/3 只经历过第一次分红（+0.3）；1/4 除息当天起累计 +0.4
    add = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf", ratio=False)
    assert add.attrs["adjust_method"] == "additive"
    assert add["close"].round(3).tolist() == [4.35, 4.35, 4.45, 4.45, 4.45]
    add_q = src.fetch("510300", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf", ratio=False)
    assert (add["close"] - add_q["close"]).round(6).nunique() == 1           # 两种复权只差一个常数


def test_sina_etf_share_split(fake_akshare, monkeypatch):
    """新浪 ETF 份额拆分：复权表里累计分红不变的一行，比例按收盘价跳空推算；拆分后的分红按拆分前的份额折算"""
    from simplequant.data import AKShareSource, sina
    bars = pd.DataFrame({"open": 4.0, "high": 4.0, "low": 4.0, "close": [4.0, 4.0, 2.0, 2.0, 1.9],
                         "volume": 1e6}, index=pd.bdate_range("2024-01-02", periods=5))
    monkeypatch.setattr(sina, "kline", lambda sym, index=False: bars.copy())
    # 1/4 一拆二（u 不变），1/8 除息每份 0.1
    monkeypatch.setattr(sina, "adjust_table", lambda sym: pd.DataFrame(
        {"f": 1.0, "u": [0.0, 0.1]}, index=pd.to_datetime(["2024-01-04", "2024-01-08"])))
    src = AKShareSource()
    hfq = src.fetch("510500", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf")
    # 拆分日因子 ×2；1/8 除息因子再 × 2 / 1.9：1.9 × 2 × 2 / 1.9 = 4.0（价格没动，复权价不变）
    assert hfq["close"].round(3).tolist() == [4.0, 4.0, 4.0, 4.0, 4.0]
    qfq = src.fetch("510500", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf")
    assert qfq["close"].round(3).tolist() == [1.9, 1.9, 1.9, 1.9, 1.9]
    # 复权表没有记录的跳空：拒绝这份数据
    monkeypatch.setattr(sina, "adjust_table", lambda sym: pd.DataFrame(columns=["f", "u"]))
    with pytest.raises(RuntimeError, match="跳空"):
        src.fetch("510500", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf")
    assert src.fetch("510500", "2024-01-01", "2024-01-31", adjust="", asset="etf")["close"].iloc[2] == 2.0


def test_eastmoney_paused_after_failure(fake_akshare):
    """东方财富重试后仍连不上：之后直接用新浪，不再每次先等东方财富失败"""
    from simplequant.data import AKShareSource, net
    src = AKShareSource()
    src.fetch("510300", "2024-01-01", "2024-01-31", adjust="", asset="etf")
    assert len(fake_akshare) == 3                                            # 第一次：重试 3 次
    assert net.paused("eastmoney") is not None
    src.fetch("510300", "2024-01-01", "2024-01-31", adjust="hfq", asset="etf")
    src.fetch("600519", "2024-01-01", "2024-01-31", adjust="", asset="stock")
    assert len(fake_akshare) == 3                                            # 暂停期间不再访问
    net.reset("eastmoney")
    src.fetch("510300", "2024-01-01", "2024-01-31", adjust="", asset="etf")
    assert len(fake_akshare) == 6                                            # 恢复后重新优先东方财富


def test_sina_stock_proportional_adjustment(fake_akshare, monkeypatch):
    """新浪股票：等比复权（不复权价 × 后复权因子）；前复权最新价等于真实价；换手率按流通股本算成百分比"""
    from simplequant.data import AKShareSource, sina
    monkeypatch.setattr(sina, "adjust_table", lambda sym: pd.DataFrame(
        {"f": [1.0, 2.0]}, index=pd.to_datetime(["2000-01-01", "2024-01-04"])))
    monkeypatch.setattr(sina, "float_shares", lambda sym: pd.Series([1e8], index=pd.to_datetime(["2000-01-01"])))
    src = AKShareSource()
    hfq = src.fetch("600519", "2024-01-01", "2024-01-31", adjust="hfq", asset="stock")
    assert hfq.attrs["provider"] == "sina+factors"
    assert hfq["close"].round(2).tolist() == [4.05, 4.05, 8.1, 8.1, 8.1]
    qfq = src.fetch("600519", "2024-01-01", "2024-01-31", adjust="qfq", asset="stock")
    assert qfq["close"].round(3).tolist() == [2.025, 2.025, 4.05, 4.05, 4.05]
    assert qfq["volume"].iloc[0] == pytest.approx(1e4)
    assert qfq["turnover"].iloc[0] == pytest.approx(1.0)                     # 1e6 / 1e8 = 1%


def test_index_sina_then_csindex(fake_akshare, monkeypatch):
    """指数：新浪优先；新浪没有的（如部分中证指数）用中证官网，节假日起始的重复行去掉、缺失的开高低用收盘价"""
    import akshare as ak
    from simplequant.data import AKShareSource, sina
    src = AKShareSource()
    assert src.fetch("000300", "2024-01-01", "2024-01-31", adjust="", asset="index").attrs["provider"] == "sina"
    assert fake_akshare == []                                                # 指数不访问东方财富

    def no_sina(sym, index=False):
        raise ValueError("Sina has no data")
    monkeypatch.setattr(sina, "kline", no_sina)
    monkeypatch.setattr(ak, "stock_zh_index_hist_csindex", lambda **kw: pd.DataFrame({
        "日期": ["2024-01-01", "2024-01-02", "2024-01-03"], "开盘": [None, None, 10.2], "最高": [None, None, 10.3],
        "最低": [None, None, 10.0], "收盘": [10.1, 10.1, 10.2], "成交量": [5e6, 5e6, 6e6]}))
    df = src.fetch("931151", "2024-01-01", "2024-01-31", adjust="", asset="index")
    assert df.attrs["provider"] == "csindex"
    assert df.index.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-03"]
    assert df["open"].iloc[0] == 10.1 and df["volume"].iloc[1] == pytest.approx(6e4)


def test_akshare_all_sources_down(fake_akshare, monkeypatch):
    from simplequant.data import AKShareSource, sina

    def down(sym, index=False):
        raise ConnectionError("sina down")
    monkeypatch.setattr(sina, "kline", down)
    with pytest.raises(RuntimeError, match="各数据接口均无法获取数据") as e:
        AKShareSource().fetch("510300", "2024-01-01", "2024-01-31", adjust="qfq", asset="etf")
    assert "东方财富" in str(e.value) and "新浪财经" in str(e.value)


def test_net_call_retry_and_pause(monkeypatch):
    """只有网络错误才重试；重试后仍失败、或被封（456）时暂停该网站；数据问题不重试也不暂停"""
    from simplequant.data import net
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return "ok"
    assert net.call("x", flaky) == "ok" and len(calls) == 3 and net.paused("x") is None

    def bad_data():
        calls.append(1)
        raise ValueError("cannot parse")
    calls.clear()
    with pytest.raises(ValueError):
        net.call("x", bad_data)
    assert len(calls) == 1 and net.paused("x") is None

    def blocked():
        calls.append(1)
        raise net.Blocked("Sina HTTP 456")
    calls.clear()
    with pytest.raises(net.Blocked):
        net.call("sina", blocked)
    assert len(calls) == 1                                                   # 被封：不重试
    with pytest.raises(net.Paused, match="新浪财经近期无法连接"):
        net.call("sina", lambda: "never")


def test_sina_http_status(monkeypatch):
    """新浪：456 / 403 视为被封，404 视为没有这个代码"""
    import requests
    from simplequant.data import net, sina

    class Resp:
        def __init__(self, code):
            self.status_code, self.text = code, "var x={}"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)
    for code, exc in ((456, net.Blocked), (403, net.Blocked), (404, net.NotFound)):
        monkeypatch.setattr(requests, "get", lambda url, **kw: Resp(code))
        with pytest.raises(exc):
            sina.get("https://example.com")
    assert not net.is_network_error(requests.HTTPError(response=Resp(404)))
    assert net.is_network_error(requests.HTTPError(response=Resp(502)))
    assert sina.symbol_of("000300", "index") == "sh000300" and sina.symbol_of("399006", "index") == "sz399006"
    assert sina.symbol_of("000001", "stock") == "sz000001"


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


def test_validate_and_describe_pacing():
    """连续 N 根 K 线、冷却期、分批建仓的校验与说明"""
    def rule(**extra):
        c = {"left": make_ind("close"), "op": ">", "right": make_ind("sma", period=20)}
        c.update(extra.pop("cond", {}))
        return {"buy": {"logic": "all", "conditions": [c]}, "sell": {"conditions": []}, "position_pct": 90, **extra}
    assert validate(rule(cond={"days": 3}, cooldown=5, scale={"adds": 2, "when": "gain", "pct": 3})) == []
    assert validate(rule(scale={"adds": 1, "when": "signal"})) == []
    assert validate(rule(cond={"days": 0}))
    assert validate(rule(cond={"days": 2.5}))
    assert validate(rule(cond={"days": 2, "op": "cross_above"}))              # 上穿不能连续
    assert validate(rule(cond={"days": 2, "left": {"ind": "pnl_pct"}, "right": {"value": 5}}))
    assert validate(rule(cooldown=-1)) and validate(rule(cooldown=1000))
    assert validate(rule(scale={"adds": 0, "when": "gain", "pct": 3}))
    assert validate(rule(scale={"adds": 2, "when": "later"}))
    assert validate(rule(scale={"adds": 2, "when": "drop"}))                  # 下跌加仓要给幅度
    text = describe_rule(rule(cond={"days": 3}, cooldown=5, scale={"adds": 2, "when": "drop", "pct": 4}))
    assert "（连续 3 根 K 线）" in text and "卖出后 5 根 K 线内不再买入" in text
    assert "仓位分 3 份" in text and "比上次买入时下跌 4%" in text
    en = describe_rule(rule(cond={"days": 3}, scale={"adds": 1, "when": "signal"}), "en")
    assert "for 3 bars in a row" in en and "buy conditions are met again" in en


def test_tunables_include_pacing():
    spec = {"kind": "rule", "rule": {
        "buy": {"logic": "all", "conditions": [{"left": make_ind("close"), "op": ">", "right": make_ind("sma"), "days": 3}]},
        "sell": {"conditions": []}, "position_pct": 90, "cooldown": 5, "scale": {"adds": 2, "when": "gain", "pct": 3}}}
    paths = {tb.path: tb for tb in strategies.tunables(spec)}
    assert paths["rule.buy.conditions.0.days"].is_int and paths["rule.cooldown"].value == 5
    assert paths["rule.scale.adds"].max == 5 and not paths["rule.scale.pct"].is_int
    plain = {"kind": "rule", "rule": {**spec["rule"], "cooldown": 0, "scale": None}}
    plain["rule"]["buy"]["conditions"][0]["days"] = 1
    assert not {"rule.cooldown", "rule.scale.adds", "rule.buy.conditions.0.days"} & {tb.path for tb in strategies.tunables(plain)}


# ---------------- 等比复权（东方财富不复权 × 复权因子） ----------------
def _em_frame(close, adjust_offset=0.0):
    days = pd.bdate_range("2024-01-02", periods=len(close))
    return pd.DataFrame({"日期": days.strftime("%Y-%m-%d"), "开盘": close, "最高": close, "最低": close,
                         "收盘": [c + adjust_offset for c in close], "成交量": 1e4})


def test_eastmoney_raw_times_factors(monkeypatch):
    """东方财富可用：股票 = 不复权 × BaoStock 后复权因子，ETF = 不复权 + 新浪累计分红；都取不到因子时退回东方财富的加法复权并标记"""
    import akshare as ak
    from simplequant.data import AKShareSource, net, sina, ratio_adjust
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    close = [10.0, 10.0, 9.0, 9.0, 9.9]
    calls = []

    def em(symbol, period, start_date, end_date, adjust):
        calls.append((symbol, start_date, adjust))
        return _em_frame([c + 0.1 for c in close] if adjust else close)     # 复权价：加法，统一 +0.1
    monkeypatch.setattr(ak, "stock_zh_a_hist", em)
    monkeypatch.setattr(ak, "fund_etf_hist_em", em)
    days = pd.bdate_range("2024-01-02", periods=5)
    monkeypatch.setattr(ratio_adjust, "stock_factor", lambda sym, dates: pd.Series([1, 1, 1.1, 1.1, 1.1], index=dates[:5]))
    src = AKShareSource()
    hfq = src.fetch("600519", "2024-01-01", "2024-01-31", adjust="hfq", asset="stock")
    assert hfq.attrs["provider"] == "eastmoney+factors" and hfq.attrs["adjust_method"] == "ratio"
    assert hfq["close"].round(2).tolist() == [10.0, 10.0, 9.9, 9.9, 10.89]
    assert calls[-1] == ("600519", "20240101", "")                           # 取的是不复权行情

    # ETF：不复权行情取全部历史（因子从上市第一天起算）；1/4 除息 1.0：因子 × 10 / 9
    monkeypatch.setattr(sina, "adjust_table", lambda sym: pd.DataFrame({"f": [1.0], "u": [1.0]},
                                                                     index=pd.to_datetime(["2024-01-04"])))
    etf = src.fetch("510880", "2024-01-03", "2024-01-31", adjust="hfq", asset="etf")
    assert etf.attrs["provider"] == "eastmoney+dividends" and calls[-1][1] == "19900101"
    assert etf.index[0] == days[1]                                           # 结果仍按所选区间截取
    assert etf["close"].round(3).tolist() == [10.0, 10.0, 10.0, 11.0]

    # BaoStock 与新浪都取不到：退回东方财富的加法复权
    def down(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(ratio_adjust, "stock_factor", down)
    monkeypatch.setattr(sina, "kline", down)
    add = src.fetch("600519", "2024-01-01", "2024-01-31", adjust="hfq", asset="stock")
    assert add.attrs["provider"] == "eastmoney" and add.attrs["adjust_method"] == "additive"
    assert add["close"].round(2).tolist() == [10.1, 10.1, 9.1, 9.1, 10.0]


def test_ratio_factor_matches_true_returns():
    """等比因子：每天复权价的涨跌 = 真实涨跌（拆分当天连续，除息当天按除息参考价再投资）"""
    from simplequant.data.ratio_adjust import etf_factor, apply
    raw = pd.DataFrame({c: [2.0, 2.2, 1.1, 1.0, 1.05] for c in ("open", "high", "low", "close")},
                       index=pd.bdate_range("2024-01-02", periods=5))
    table = pd.DataFrame({"f": 1.0, "u": [0.0, 0.05]}, index=pd.to_datetime(["2024-01-04", "2024-01-06"]))
    f = etf_factor(raw, table)                                                # 1/4 一拆二；1/6 是周六，1/8 除息 0.05
    out = apply(raw, f, "hfq")["close"]
    assert out.pct_change().tolist()[1:] == pytest.approx([0.1, 0.0, 1.0 / 1.1 - 1, 1.05 / (1.0 - 0.05) - 1])
    assert apply(raw, f, "qfq")["close"].iloc[-1] == pytest.approx(1.05)


def test_is_additive():
    from simplequant.data.library import DatasetMeta
    from simplequant.data.ratio_adjust import is_additive

    def m(adjust="hfq", source="akshare", **extra):
        return DatasetMeta(id="x", name="x", symbol="510300", source=source, freq="1d", adjust=adjust,
                           extra={"asset": "etf", **extra})
    assert is_additive(m(provider="eastmoney"))                              # 旧数据
    assert is_additive(m(provider="sina+dividends"))                         # 旧的新浪 ETF 也是加法
    assert not is_additive(m(provider="sina+factors", asset="stock"))        # 新浪股票本来就是等比
    assert not is_additive(m(provider="eastmoney+dividends", adjust_method="ratio"))
    assert is_additive(m(provider="eastmoney", adjust_method="additive"))    # 新版取不到因子
    assert not is_additive(m(adjust=""))                                     # 不复权
    assert not is_additive(m(source="baostock"))
    assert not is_additive(m(asset="index"))
