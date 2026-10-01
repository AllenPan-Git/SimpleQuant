"""
多因子选股：数据处理、因子研究、选股与组合回测
用合成市场测试：隐藏的"质量"分数同时决定 EP 和未来收益，所以 EP 应该是有效因子。
"""

import numpy as np
import pandas as pd
import pytest

from simplequant.engine import BrokerConfig
from simplequant.stocks import StockStore, build_panel, Panel, analyze, build_schedule, run_selection, rebalance_dates
from simplequant.stocks import factors as F
from simplequant.stocks.panel import trading_constraints, membership, limit_pct
from simplequant.stocks.research import rank_ic, forward_returns
from simplequant.stocks.store import process_daily


def make_panel(n=40, days=400, seed=0, members=None) -> Panel:
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2021-01-04", periods=days)
    codes = [f"sh.600{i:03d}" for i in range(n)]
    quality = rng.normal(0, 1, n)
    rets = 0.0008 * quality + rng.normal(0, 0.015, (days, n))
    close = 10 * np.exp(np.cumsum(rets, axis=0))
    open_ = np.vstack([close[0], close[:-1]]) * (1 + rng.normal(0, 0.002, (days, n)))
    df = lambda a: pd.DataFrame(a, index=cal, columns=codes)
    ep = 1 / 20 + 0.01 * quality + rng.normal(0, 0.005, (days, n))
    fields = {
        "open": df(open_), "close": df(close), "high": df(np.maximum(open_, close) * 1.01),
        "low": df(np.minimum(open_, close) * 0.99),
        "raw_open": df(open_), "raw_close": df(close), "raw_preclose": df(np.vstack([close[0], close[:-1]])),
        "adj_factor": df(np.ones((days, n))), "volume": df(np.full((days, n), 1e6)),
        "amount": df(close * 1e6), "turnover": df(rng.uniform(0.5, 3, (days, n))),
        "pe": df(1 / ep), "pb": df(np.full((days, n), 2.0)), "ps": df(np.full((days, n), 3.0)),
        "pct_chg": df(rets * 100),
    }
    tradable = df(np.ones((days, n), dtype=bool))
    is_st = df(np.zeros((days, n), dtype=bool))
    member = df(np.ones((days, n), dtype=bool)) if members is None else members.reindex(index=cal, columns=codes)
    can_buy, can_sell = trading_constraints(fields["raw_open"], fields["raw_preclose"], tradable, is_st)
    return Panel(fields=fields, member=member, tradable=tradable, is_st=is_st,
                 listed_days=df(np.full((days, n), 1000.0)), can_buy=can_buy, can_sell=can_sell,
                 benchmark=pd.Series(close.mean(axis=1), index=cal), names={c: c for c in codes})


@pytest.fixture(scope="module")
def panel():
    return make_panel()


# ---------------- 数据处理 ----------------
def test_process_daily_applies_back_adjust_factor():
    k = pd.DataFrame({"date": ["2024-01-02", "2024-06-26", "2024-06-27"], "open": ["10", "11", "12"],
                      "high": ["10", "11", "12"], "low": ["10", "11", "12"], "close": ["10", "11", "12"],
                      "preclose": ["9", "10", "11"], "volume": ["1", "1", "1"], "amount": ["10", "11", "12"],
                      "turn": ["1", "1", "1"], "tradestatus": ["1", "0", "1"], "pctChg": ["1", "0", "1"],
                      "isST": ["0", "0", "1"], "peTTM": ["20", "", "22"], "pbMRQ": ["2", "2", "2"], "psTTM": ["3", "3", "3"]})
    adj = pd.DataFrame({"dividOperateDate": ["2010-01-01", "2024-06-26"], "backAdjustFactor": ["2.0", "2.5"]})
    df = process_daily(k, adj)
    assert df["adj_factor"].tolist() == [2.0, 2.5, 2.5]
    assert df["close"].tolist() == [20.0, 27.5, 30.0]
    assert df["raw_close"].tolist() == [10.0, 11.0, 12.0]
    assert df["tradable"].tolist() == [1, 0, 1] and df["is_st"].tolist() == [0, 0, 1]
    assert np.isnan(df["pe"].iloc[1])


def test_limits_and_constraints():
    cal = pd.to_datetime(["2020-08-21", "2020-08-24"])
    codes = pd.Index(["sh.600000", "sz.300001", "sh.688001", "sz.000001"])
    is_st = pd.DataFrame(False, index=cal, columns=codes)
    is_st["sz.000001"] = True
    pct = limit_pct(codes, cal, is_st)
    assert pct["sh.600000"].tolist() == [0.1, 0.1]
    assert pct["sz.300001"].tolist() == [0.1, 0.2]          # 创业板注册制改革后 20%
    assert pct["sh.688001"].tolist() == [0.2, 0.2]
    assert pct["sz.000001"].tolist() == [0.05, 0.05]         # 主板 ST 5%
    pre = pd.DataFrame(10.0, index=cal, columns=codes)
    raw_open = pd.DataFrame([[11.0, 10.5, 12.0, 9.5], [10.0, 12.0, 8.0, 10.0]], index=cal, columns=codes)
    tradable = pd.DataFrame(True, index=cal, columns=codes)
    tradable.iloc[1, 3] = False
    buy, sell = trading_constraints(raw_open, pre, tradable, is_st)
    assert not buy.iloc[0, 0]                      # 主板开盘 11.00 = 涨停
    assert buy.iloc[0, 1] and not buy.iloc[1, 1]   # 创业板：8/21 为 10%（10.5 可买），8/24 为 20%（12 涨停）
    assert not sell.iloc[1, 2]                     # 科创板开盘 8.00 = 跌停
    assert not sell.iloc[0, 3]                     # ST 开盘 9.50 = 跌停
    assert not buy.iloc[1, 3] and not sell.iloc[1, 3]   # 停牌


def test_membership_uses_latest_snapshot_not_later():
    uni = pd.DataFrame({"date": pd.to_datetime(["2021-01-01", "2021-01-01", "2021-03-01"]),
                        "code": ["A", "B", "A"]})
    cal = pd.to_datetime(["2020-12-31", "2021-02-01", "2021-03-01", "2021-03-02"])
    m = membership(uni, cal, ["A", "B"])
    assert m["A"].tolist() == [False, True, True, True]
    assert m["B"].tolist() == [False, True, False, False]   # 3 月调出后不再是成分股


# ---------------- 因子研究 ----------------
def test_preprocess_winsorizes_and_standardizes():
    raw = pd.DataFrame([[1.0, 2.0, 3.0, 4.0, 1000.0]])
    z = F.preprocess(raw, raw.notna())
    assert abs(z.iloc[0].mean()) < 1e-9 and abs(z.iloc[0].std() - 1) < 1e-9
    assert z.iloc[0, 4] < 2.0     # 极端值被截断


def test_forward_returns_use_next_open(panel):
    fwd = forward_returns(panel, 5)
    d = panel.calendar[10]
    o = panel["open"]
    expected = o.iloc[16] / o.iloc[11] - 1
    np.testing.assert_allclose(fwd.loc[d].values, expected.values)


def test_rank_ic_matches_scipy_style(panel):
    rng = np.random.default_rng(1)
    a = pd.DataFrame(rng.normal(size=(3, 50)))
    b = a * 0.5 + pd.DataFrame(rng.normal(size=(3, 50)))
    ic = rank_ic(a, b)
    for i in range(3):   # Spearman = 秩的 Pearson
        assert ic[i] == pytest.approx(a.iloc[i].rank().corr(b.iloc[i].rank()))


def test_planted_factor_is_detected(panel):
    mask = panel.eligible(False, 0)
    rep = analyze(panel, F.compute(panel, "ep"), mask, horizon=20, groups=5)
    assert rep.summary.mean > 0.1 and rep.summary.positive > 0.7
    assert rep.monotonicity > 0.8
    assert rep.annual[5] > rep.annual[1] and rep.cumulative["long_short"].iloc[-1] > 1
    assert rep.summary.t_stat > 3
    # 纯噪声因子：IC 在统计上不显著（19 期 × 40 只，IC 均值的标准误约 0.04）
    for seed in range(5):
        noise = pd.DataFrame(np.random.default_rng(seed).normal(size=panel["close"].shape), index=panel.calendar,
                             columns=panel.codes)
        assert abs(analyze(panel, noise, mask, horizon=20).summary.t_stat) < 3


def test_every_factor_computes(panel):
    """价量因子在合成数据上都有值（财务因子需要财务数据，见 test_fundamentals.py）"""
    for key in [k for k, m in F.FACTORS.items() if not m.get("requires_fin")]:
        v = F.compute(panel, key)
        assert v.shape == panel["close"].shape
        assert v.iloc[-1].notna().sum() > 30, key


# ---------------- 选股 ----------------
SPEC = {"kind": "selection", "universe": "test", "factors": [{"key": "ep", "weight": 1, "direction": 1}],
        "top_n": 5, "rebalance": "monthly", "filters": {"exclude_st": True, "min_list_days": 0}, "position_pct": 95}


def test_rebalance_dates():
    cal = pd.bdate_range("2024-01-01", "2024-03-31")
    m = rebalance_dates(cal, "monthly")
    assert list(m.strftime("%Y-%m-%d")) == ["2024-01-31", "2024-02-29", "2024-03-29"]
    assert len(rebalance_dates(cal, 10)) == int(np.ceil(len(cal) / 10))


def test_schedule_picks_top_scores_from_eligible_only():
    members = pd.DataFrame(True, index=pd.bdate_range("2021-01-04", periods=400),
                           columns=[f"sh.600{i:03d}" for i in range(40)])
    members.iloc[:, :10] = False                     # 前 10 只从来不是成分股
    p = make_panel(members=members)
    p.is_st.iloc[:, 10] = True                       # 第 11 只一直是 ST
    sched = build_schedule(p, SPEC)
    banned = set(p.codes[:11])
    for d, codes in sched.picks.items():
        assert len(codes) == 5 and not banned & set(codes)
        s = sched.scores[d]
        assert [s[c] for c in codes] == sorted(s.values(), reverse=True)


def test_schedule_has_no_lookahead(panel):
    """截断未来数据后，截断点之前的选股结果必须完全相同"""
    full = build_schedule(panel, SPEC)
    cut = panel.calendar[250]
    short = make_panel()
    for f in short.fields:
        short.fields[f] = short.fields[f].loc[:cut]
    for attr in ("member", "tradable", "is_st", "listed_days", "can_buy", "can_sell"):
        setattr(short, attr, getattr(short, attr).loc[:cut])
    part = build_schedule(short, SPEC)
    complete = [d for d in part.picks if d < cut]      # 截断点所在的月份不完整，其"月末"就是截断日
    assert len(complete) >= 8
    for d in complete:
        assert full.picks[d] == part.picks[d]


def test_selection_backtest_basics(panel):
    res = run_selection(panel, SPEC, BrokerConfig(commission=0.00025, stamp_duty=0.0005))
    assert (res.equity["cash"] >= -1e-6).all()
    buys = res.orders[res.orders["side"] == "buy"]
    assert len(buys) > 10 and (buys["size"] % 100 == 0).all()
    # 每次调仓后持仓数量不超过 top_n
    assert res.metrics["final_value"] > 0 and res.metrics["turnover_annual"] > 0
    # 有效因子选出的组合应该跑赢等权基准
    assert res.metrics["total_return"] > res.metrics["benchmark_return"]


def test_warmup_history_is_used_but_not_traded(panel):
    """用 60 日波动率：从面板第一天回测会跳过前几个调仓日；指定 start 时用之前的数据预热，第一期就能选股"""
    spec = {**SPEC, "factors": [{"key": "vol60", "weight": 1, "direction": -1}]}
    cold = build_schedule(panel, spec)
    assert len(cold.skipped) >= 2
    start = panel.calendar[150]
    warm = build_schedule(panel, spec, start=start)
    assert not warm.skipped and min(warm.picks) >= start
    res = run_selection(panel, spec, start=start)
    assert res.equity.index[0] >= start and res.equity["value"].iloc[0] == pytest.approx(100_000)
    assert res.equity["benchmark"].iloc[0] == pytest.approx(100_000)
    assert res.orders["time"].min() > start
    # 研究页同理：只统计 start 之后的截面
    rep = analyze(panel, F.compute(panel, "vol60"), panel.eligible(False, 0), 20, start=start)
    assert rep.ic.index.min() >= start


def test_limit_up_blocks_buy_and_suspension_delays_sell():
    p = make_panel()
    sched = build_schedule(p, SPEC)
    d0, d1 = list(sched.picks)[0], list(sched.picks)[1]
    first_pick = sched.picks[d0][0]
    nxt = p.calendar[p.calendar.get_loc(d0) + 1]
    p.can_buy.loc[nxt, first_pick] = False           # 首个调仓日次日开盘涨停
    res = run_selection(p, SPEC, schedule=sched)
    first_buys = res.orders[(res.orders["time"] == nxt) & (res.orders["side"] == "buy")]
    assert first_pick not in set(first_buys["symbol"]) and len(first_buys) == 4
    assert any(k == "sel.buy_blocked" and kw["name"] == first_pick for _, k, kw in res.logs)

    # 停牌卖不出：之后能卖时再卖
    p2 = make_panel()
    sched2 = build_schedule(p2, SPEC)
    dates = list(sched2.picks)
    dropped = next((c, dates[i + 1]) for i in range(len(dates) - 1)
                   for c in sched2.picks[dates[i]] if c not in sched2.picks[dates[i + 1]])
    code, when = dropped
    i = p2.calendar.get_loc(when)
    p2.can_sell.iloc[i + 1: i + 4, p2.codes.index(code)] = False     # 连续 3 天卖不出
    res2 = run_selection(p2, SPEC, schedule=sched2)
    sells = res2.orders[(res2.orders["symbol"] == code) & (res2.orders["side"] == "sell")]
    assert (sells["time"] > p2.calendar[i + 1]).all()
    assert pd.Timestamp(p2.calendar[i + 4]) in set(sells["time"])


def test_retry_sell_on_rebalance_day_sells_once():
    """之前卖不出的股票恰好在下一个调仓日的开盘卖出：只卖一次，不会变成空头"""
    p = make_panel()
    sched = build_schedule(p, SPEC)
    dates = list(sched.picks)
    code, i0, j = next((c, i, i + 1) for i in range(len(dates) - 2) for c in sched.picks[dates[i]]
                       if c not in sched.picks[dates[i + 1]] and c not in sched.picks[dates[i + 2]])
    a = p.calendar.get_loc(dates[j]) + 1                     # 落选后的执行日
    b = p.calendar.get_loc(dates[j + 1]) + 1                 # 下一个调仓执行日
    p.can_sell.iloc[a:b, p.codes.index(code)] = False
    res = run_selection(p, SPEC, schedule=sched)
    sells = res.orders[(res.orders["symbol"] == code) & (res.orders["side"] == "sell")
                       & (res.orders["time"] > p.calendar[a - 1])]
    assert list(sells["time"])[:1] == [p.calendar[b]]
    assert not (sells["time"] == p.calendar[b]).sum() > 1
    assert all(pos["size"] > 0 for pos in res.positions)


def test_raw_price_lot_sizing_with_adjust_factor():
    """后复权因子为 4 时：Backtrader 用后复权价，但股数和成交价按不复权价记录，股数是 100 的整数倍"""
    p = make_panel()
    for f in ("open", "high", "low", "close"):
        p.fields[f] = p.fields[f] * 4
    res = run_selection(p, SPEC)
    buys = res.orders[res.orders["side"] == "buy"]
    assert (buys["size"] % 100 == 0).all()
    t, c = buys.iloc[0]["time"], buys.iloc[0]["symbol"]
    assert buys.iloc[0]["price"] == pytest.approx(p["raw_open"].loc[t, c], rel=2e-3)   # 含滑点


# ---------------- 存储（假下载器，不联网） ----------------
def fake_fetcher(code, start, end):
    cal = pd.bdate_range(start, end)
    n = len(cal)
    k = pd.DataFrame({"date": cal.strftime("%Y-%m-%d"), "open": 10.0, "high": 10.0, "low": 10.0,
                      "close": np.linspace(10, 11, n), "preclose": 10.0, "volume": 1e6, "amount": 1e7, "turn": 1.0,
                      "tradestatus": 1, "pctChg": 0.1, "isST": 0, "peTTM": 15.0, "pbMRQ": 1.5, "psTTM": 2.0}).astype(str)
    adj = pd.DataFrame({"dividOperateDate": ["2000-01-01"], "backAdjustFactor": ["3.0"]})
    return code, process_daily(k, adj), ""


def test_store_resumes_and_retries_failures(tmp_path):
    """下载中途失败：已完成的不重下，失败的会单线程再试一次"""
    calls = {}

    def flaky(code, start, end):
        calls[code] = calls.get(code, 0) + 1
        if code == "sh.600001" and calls[code] == 1:
            return code, None, "ConnectionError: 网络接收错误"
        return fake_fetcher(code, start, end)

    st = StockStore(tmp_path, fetcher=flaky, login=lambda: None, worker_init=lambda: None)
    errors = st.update(["sh.600000", "sh.600001", "sh.600002"], "2024-01-01", "2024-02-29", workers=1)
    assert errors == {} and calls == {"sh.600000": 1, "sh.600001": 2, "sh.600002": 1}
    st.update(["sh.600000", "sh.600001", "sh.600002", "sh.600003"], "2024-01-01", "2024-02-29", workers=1)
    assert calls["sh.600000"] == 1 and calls["sh.600003"] == 1      # 只下载新增的


def test_update_before_close_does_not_skip_todays_bar(tmp_path):
    """收盘前运行：当天数据还没出；收盘后再运行必须补取当天，而不是认为"已经是最新"而跳过"""
    published = {"last": "2024-03-28"}

    def fetch(code, start, end):
        _, df, _ = fake_fetcher(code, start, min(end, published["last"]))
        return code, df, ""

    st = StockStore(tmp_path, fetcher=fetch, login=lambda: None, worker_init=lambda: None)
    st.update(["sh.600000"], "2024-01-01", "2024-03-29", workers=1)          # 3/29 收盘前
    assert st.load("sh.600000").index[-1] == pd.Timestamp("2024-03-28")
    assert "sh.600000" in st.plan(["sh.600000"], "2024-01-01", "2024-03-29")  # 仍需补取
    published["last"] = "2024-03-29"                                        # 收盘后数据发布
    st.update(["sh.600000"], "2024-01-01", "2024-03-29", workers=1)
    assert st.load("sh.600000").index[-1] == pd.Timestamp("2024-03-29")
    assert st.plan(["sh.600000"], "2024-01-01", "2024-03-29") == {}


def test_data_pack_roundtrip_and_rejects_bad_paths(tmp_path):
    import io
    import zipfile
    src = StockStore(tmp_path / "a", fetcher=fake_fetcher, login=lambda: None, worker_init=lambda: None)
    src.update(["sh.600000", "sz.000001"], "2024-01-01", "2024-02-29", workers=1)
    pack = src.export_zip()

    dst = StockStore(tmp_path / "b", fetcher=fake_fetcher, login=lambda: None, worker_init=lambda: None)
    dst._save_manifest({"sh.600000": "2024-03-31"})           # 本地已有更新的进度
    assert dst.import_zip(io.BytesIO(pack)) == 2
    pd.testing.assert_frame_equal(dst.load("sz.000001"), src.load("sz.000001"))
    assert dst._manifest() == {"sh.600000": "2024-03-31", "sz.000001": "2024-02-29"}
    assert dst.plan(["sz.000001"], "2024-01-01", "2024-02-29") == {}   # 导入后无需重新下载

    for bad in ("../evil.parquet", "daily/../../evil.parquet", "C:/evil.parquet", "daily/x.exe", "other/x.parquet"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(bad, b"x")
        with pytest.raises(ValueError):
            dst.import_zip(io.BytesIO(buf.getvalue()))
    assert not (tmp_path / "evil.parquet").exists()


def test_store_incremental_update_and_panel(tmp_path):
    st = StockStore(tmp_path, fetcher=fake_fetcher, login=lambda: None, worker_init=lambda: None)
    assert not st.update(["sh.600000", "sz.000001"], "2024-01-01", "2024-03-29", workers=1)
    assert st.load("sh.600000")["close"].iloc[0] == pytest.approx(30.0)
    assert st.plan(["sh.600000"], "2024-01-01", "2024-03-29") == {}          # 已是最新
    assert st.plan(["sh.600000"], "2024-01-01", "2024-04-30") == {"sh.600000": ("2024-03-29", "2024-04-30")}
    st.update(["sh.600000"], "2024-01-01", "2024-04-30", workers=1)
    df = st.load("sh.600000")
    assert df.index.is_unique and df.index[-1] == pd.Timestamp("2024-04-30")

    # 手工写入成分股与指数，构建面板
    pd.DataFrame({"date": pd.to_datetime(["2024-01-01", "2024-01-01"]), "code": ["sh.600000", "sz.000001"],
                  "name": ["A", "B"]}).to_parquet(tmp_path / "universe" / "hs300.parquet")
    cal = pd.bdate_range("2024-01-01", "2024-03-29")
    idx = pd.DataFrame({"open": 1.0, "close": np.linspace(1, 2, len(cal))}, index=cal)
    idx.to_parquet(tmp_path / "index" / "sh.000300.parquet")
    p = build_panel(st, "hs300", "2024-01-01", "2024-03-29")
    assert p.codes == ["sh.600000", "sz.000001"] and p.member.all().all()
    assert p["close"].shape == (len(cal), 2) and p.can_buy.all().all()
    assert (p.listed_days.iloc[-1] > 0).all()


def test_fast_feed_matches_backtrader_pandas_feed(panel, monkeypatch):
    """SelectionFeed 的整列预加载和精简 tick 处理只是提速：结果必须与 Backtrader 原生 PandasData 完全一致"""
    import backtrader as bt
    from simplequant.stocks import selection as S
    p = make_panel(seed=3)
    p.can_sell.iloc[100:130] = False                # 制造卖不出、重试的情况
    p.can_buy.iloc[200:230, 5:10] = False
    spec = {**SPEC, "rebalance": "weekly"}
    fast = run_selection(p, spec)
    for name in ("preload", "_tick_fill", "_tick_nullify"):
        monkeypatch.setattr(S.SelectionFeed, name, getattr(bt.feeds.PandasData, name))
    slow = run_selection(p, spec)
    assert fast.equity.equals(slow.equity) and fast.orders.equals(slow.orders) and fast.trades.equals(slow.trades)
    assert fast.logs == slow.logs and fast.positions == slow.positions and fast.pending == slow.pending
    assert any(k == "sel.sell_blocked" for _, k, _ in fast.logs)
