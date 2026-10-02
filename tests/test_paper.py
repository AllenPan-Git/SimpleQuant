"""
本地模拟盘
核心：逐日推进（每次只给到当天为止的数据）的结果，必须与一次性回测完全一致
"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_prices
from test_stocks import make_panel, SPEC
from simplequant import strategies
from simplequant.engine import run_backtest, BrokerConfig
from simplequant.paper import account as account_mod
from simplequant.paper import create_account, run_account, list_accounts, load_account, delete_account
from simplequant.paper.calendar import next_trading_days
from simplequant.stocks import run_selection


@pytest.fixture(autouse=True)
def tmp_paper_root(tmp_path, monkeypatch):
    monkeypatch.setattr(account_mod, "PAPER_ROOT", tmp_path / "paper")


def truncate_panel(panel, d):
    import copy
    p = copy.copy(panel)
    p.fields = {k: v.loc[:d] for k, v in panel.fields.items()}
    for attr in ("member", "tradable", "is_st", "listed_days", "can_buy", "can_sell"):
        setattr(p, attr, getattr(panel, attr).loc[:d])
    p.benchmark = panel.benchmark.loc[:d]
    return p


def keys(df):
    return [(pd.Timestamp(r.time), r.symbol, r.side, round(float(r.size), 2)) for r in df.itertuples()]


# ---------------- 多因子选股 ----------------
def test_selection_daily_progression_equals_full_backtest():
    panel = make_panel(days=300)
    cal = panel.calendar.append(pd.bdate_range(panel.calendar[-1] + pd.Timedelta(days=1), periods=40))
    start = panel.calendar[120].date().isoformat()
    broker = BrokerConfig(commission=0.00025, stamp_duty=0.0005)
    full = run_selection(panel, SPEC, broker, start=start, next_days=next_trading_days(cal, panel.calendar[-1]))

    acc = create_account("选股测试", SPEC, broker, start, universe="test")
    states = []
    cuts = list(range(120, 300, 7)) + [299]
    for i in cuts:
        states.append(run_account(acc, cal, panel=truncate_panel(panel, panel.calendar[i])))
    acc = load_account(acc.id)
    assert not any(s["divergence"] for s in states)
    assert keys(acc.fills()) == keys(full.orders) and len(full.orders) > 20
    nav = acc.nav()
    np.testing.assert_allclose(nav["value"].values, full.equity["value"].values, rtol=1e-9)
    assert acc.data_through == panel.calendar[-1].date().isoformat()


def test_selection_signal_on_month_end_is_executed_next_day():
    panel = make_panel(days=300)
    cal = panel.calendar.append(pd.bdate_range(panel.calendar[-1] + pd.Timedelta(days=1), periods=40))
    start = panel.calendar[120].date().isoformat()
    acc = create_account("信号测试", SPEC, BrokerConfig(), start, universe="test")
    month_ends = pd.Series(panel.calendar, index=panel.calendar).groupby(panel.calendar.to_period("M")).last()
    d = next(x for x in month_ends if x > panel.calendar[150])
    i = panel.calendar.get_loc(d)
    run_account(acc, cal, panel=truncate_panel(panel, panel.calendar[i - 1]))      # 月末前一天：没有新信号
    s0 = acc.state()
    s = run_account(acc, cal, panel=truncate_panel(panel, d))                     # 月末收盘：生成调仓信号
    assert s["execute_on"] == panel.calendar[i + 1].date().isoformat()
    assert len(s["signals"]) > len(s0["signals"])
    buys = {x["symbol"] for x in s["signals"] if x["side"] == "buy"}
    assert all(x["size"] % 100 == 0 and x["reason"][0].startswith("sel.") for x in s["signals"])
    run_account(acc, cal, panel=truncate_panel(panel, panel.calendar[i + 1]))     # 次日：按信号成交
    fills = acc.fills()
    next_day = fills[pd.to_datetime(fills["time"]) == panel.calendar[i + 1]]
    assert buys and buys == set(next_day.loc[next_day["side"] == "buy", "symbol"])


def test_selection_before_first_rebalance_is_flat():
    panel = make_panel(days=300)
    cal = panel.calendar.append(pd.bdate_range(panel.calendar[-1] + pd.Timedelta(days=1), periods=40))
    mid_month = next(d for d in panel.calendar[200:] if d.day == 10 or d.day == 11)
    acc = create_account("新开户", SPEC, BrokerConfig(cash=500_000), mid_month.date().isoformat(), universe="test")
    s = run_account(acc, cal, panel=truncate_panel(panel, mid_month))
    assert s["signals"] == [] and s["value"] == pytest.approx(500_000) and acc.fills().empty


# ---------------- 单标的策略 ----------------
def test_single_asset_daily_progression_equals_full_backtest():
    prices = {"510300": make_prices(n=400)}
    cal = prices["510300"].index.append(pd.bdate_range(prices["510300"].index[-1] + pd.Timedelta(days=1), periods=40))
    spec = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}
    start = prices["510300"].index[150].date().isoformat()
    full = run_backtest(prices, *strategies.resolve(spec), BrokerConfig(), trade_start=start)
    assert (pd.to_datetime(full.orders["time"]) > pd.Timestamp(start)).all()

    acc = create_account("均线测试", spec, BrokerConfig(), start,
                         assets=[{"source": "akshare", "symbol": "510300", "asset": "etf", "name": "510300"}])
    df = prices["510300"]
    for i in list(range(150, 400, 9)) + [399]:
        s = run_account(acc, cal, prices={"510300": df.iloc[:i + 1]})
        assert not s["divergence"]
    assert keys(acc.fills()) == keys(full.orders) and len(full.orders) > 4
    np.testing.assert_allclose(acc.nav()["value"].values, full.equity["value"].values, rtol=1e-9)


def test_single_asset_signal_and_divergence_detection():
    df = make_prices(n=300)
    cal = df.index.append(pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=40))
    spec = {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20}}
    acc = create_account("信号", spec, BrokerConfig(), df.index[100].date().isoformat(),
                         assets=[{"source": "akshare", "symbol": "X", "asset": "etf", "name": "X"}])
    # 找到一个会产生信号的日子
    signal_day = None
    for i in range(100, 300):
        s = run_account(acc, cal, prices={"X": df.iloc[:i + 1]})
        if s["signals"]:
            signal_day = i
            break
    assert signal_day is not None
    sig = s["signals"][0]
    assert sig["reason"][0] in ("reason.ma_up", "reason.ma_down") and sig["size"] > 0
    run_account(acc, cal, prices={"X": df.iloc[:signal_day + 2]})
    last_fill = acc.fills().iloc[-1]
    assert pd.Timestamp(last_fill["time"]) == df.index[signal_day + 1] and last_fill["side"] == sig["side"]

    # 数据被修订（如复权口径变化：已成交那天的价格变了）：已记录的账本不改写，但给出不一致提示
    ledger_before = acc.fills().copy()
    revised = df.copy()
    revised.iloc[:signal_day + 2, :4] *= 1.3
    s3 = run_account(acc, cal, prices={"X": revised.iloc[:signal_day + 5]})
    assert s3["divergence"] == "paper.divergence"
    pd.testing.assert_frame_equal(acc.fills().iloc[:len(ledger_before)].reset_index(drop=True),
                                  ledger_before.reset_index(drop=True))


# ---------------- 其它 ----------------
def test_account_storage():
    acc = create_account("甲", {"kind": "template", "template": "buy_hold", "params": {}}, BrokerConfig(), "2024-01-02",
                         assets=[{"source": "akshare", "symbol": "510300", "asset": "etf", "name": "510300"}])
    assert [a.id for a in list_accounts()] == [acc.id]
    back = load_account(acc.id)
    assert back.kind == "single" and back.broker["cash"] == 100_000
    delete_account(back)
    assert list_accounts() == []


def test_dividend_does_not_create_odd_lot_shares():
    """持有期间分红（后复权因子变大、真实价格不变）：股数仍是买入时的整手，卖出股数等于买入股数"""
    p = make_panel(days=300)
    for f in ("open", "high", "low", "close"):
        p.fields[f] = p.fields[f].copy()
        p.fields[f].iloc[200:] *= 1.05            # 第 200 天起后复权价 +5%（相当于 5% 的分红）
    res = run_selection(p, SPEC)
    for sym, g in res.orders.groupby("symbol"):
        sizes = g.sort_values("time")[["side", "size"]].values.tolist()
        for (s1, n1), (s2, n2) in zip(sizes, sizes[1:]):
            if s1 == "buy" and s2 == "sell":
                assert n1 == n2, sym
    assert (res.orders["size"] % 100 == 0).all()
    assert all(x["size"] % 100 == 0 for x in res.positions)
    # 市值包含分红：比"股数 × 真实价格"略高
    assert any(x["market_value"] > x["size"] * x["price"] * 1.01 for x in res.positions)


def test_single_refresh_drops_unfinished_intraday_bar(monkeypatch):
    """盘中获取数据：数据源返回了当天尚未走完的 K 线，必须丢掉"""
    from simplequant.paper import runner
    today = pd.Timestamp("2026-09-29")
    df = make_prices(n=60, start=pd.bdate_range(end=today, periods=60)[0])
    assert df.index[-1] == today

    class FakeSource:
        def fetch(self, symbol, start, end, **kw):
            return df
    monkeypatch.setitem(runner.SOURCES, "fake", FakeSource())
    monkeypatch.setattr(runner, "load_calendar", lambda: df.index)
    monkeypatch.setattr(runner, "latest_expected_day", lambda cal: pd.Timestamp("2026-09-28"))
    acc = create_account("盘中", {"kind": "template", "template": "buy_hold", "params": {}}, BrokerConfig(), "2026-08-03",
                         assets=[{"source": "fake", "symbol": "X", "name": "X"}])
    prices = runner.refresh_single_data(acc)
    assert prices["X"].index[-1] == pd.Timestamp("2026-09-28")
    assert runner.load_single_data(acc)["X"].index[-1] == pd.Timestamp("2026-09-28")


def test_latest_expected_day():
    import datetime as dt
    from simplequant.paper.calendar import latest_expected_day
    cal = pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08"])
    assert latest_expected_day(cal, dt.datetime(2026, 9, 29, 14, 0)).date() == dt.date(2026, 9, 28)   # 盘中
    assert latest_expected_day(cal, dt.datetime(2026, 9, 29, 18, 30)).date() == dt.date(2026, 9, 29)  # 收盘后
    assert latest_expected_day(cal, dt.datetime(2026, 10, 3, 10, 0)).date() == dt.date(2026, 9, 30)   # 国庆假期


def test_next_trading_days_skips_holidays_and_falls_back():
    cal = pd.to_datetime(["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"])
    nd = next_trading_days(cal, "2026-09-30", 3)
    assert list(nd.strftime("%m-%d")) == ["10-08", "10-09", "10-12"]      # 日历之外用工作日近似


def test_schedule_commands(monkeypatch):
    from simplequant.paper import schedule
    monkeypatch.setattr(schedule, "SYSTEM", "windows")
    calls = []

    class R:
        returncode, stdout, stderr = 0, "SUCCESS", ""
    monkeypatch.setattr(schedule.subprocess, "run", lambda args, **kw: calls.append(args) or R())
    assert schedule.create_task("18:45")[0]
    args = calls[-1]
    assert args[:3] == ["schtasks", "/Create", "/F"] and "18:45" in args and schedule.TASK_NAME in args
    assert str(schedule.BAT) in args[args.index("/TR") + 1]
    schedule.delete_task()
    assert calls[-1][:2] == ["schtasks", "/Delete"]


class _FakeRun:
    """记录 subprocess.run 的调用；crontab 内容存在 self.cron"""

    def __init__(self, ok=lambda args: True):
        self.calls, self.cron, self.ok = [], None, ok

    def __call__(self, args, **kw):
        self.calls.append(args)
        out = ""
        if args[:2] == ["crontab", "-l"]:
            ok = self.cron is not None
            out = self.cron or ""
        elif args[:2] == ["crontab", "-"]:
            self.cron, ok = kw["input"], True
        else:
            ok = self.ok(args)

        class R:
            returncode, stdout, stderr = (0 if ok else 1), out, ""
        return R()


def test_schedule_launchd(monkeypatch, tmp_path):
    import plistlib
    from simplequant.paper import schedule
    monkeypatch.setattr(schedule, "SYSTEM", "macos")
    monkeypatch.setattr(schedule.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(schedule.os, "getuid", lambda: 501, raising=False)
    fake = _FakeRun()
    monkeypatch.setattr(schedule.subprocess, "run", fake)
    assert schedule.backend() == "launchd" and not schedule.task_exists()
    assert schedule.create_task("18:45") == (True, "")
    plist = plistlib.loads((tmp_path / "Library/LaunchAgents/com.simplequant.paperdaily.plist").read_bytes())
    assert plist["ProgramArguments"] == ["/bin/sh", str(schedule.SH)]
    assert plist["StartCalendarInterval"][0] == {"Weekday": 1, "Hour": 18, "Minute": 45}
    assert len(plist["StartCalendarInterval"]) == 5
    assert ["launchctl", "bootstrap", "gui/501"] == fake.calls[-1][:3]
    assert schedule.task_exists()
    schedule.delete_task()
    assert not schedule.task_exists()


def test_schedule_systemd(monkeypatch, tmp_path):
    from simplequant.paper import schedule
    monkeypatch.setattr(schedule, "SYSTEM", "linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(schedule, "FROZEN", True)
    monkeypatch.setattr(schedule.sys, "executable", "/opt/Simple Quant%/SimpleQuant")
    fake = _FakeRun()
    monkeypatch.setattr(schedule.subprocess, "run", fake)
    assert schedule.backend() == "systemd"
    assert schedule.create_task("19:05") == (True, "")
    d = tmp_path / "systemd" / "user"
    assert 'ExecStart="/opt/Simple Quant%%/SimpleQuant" "--paper"' in (d / "simplequant-paper.service").read_text()
    assert "OnCalendar=Mon..Fri *-*-* 19:05:00" in (d / "simplequant-paper.timer").read_text()
    assert ["systemctl", "--user", "enable", "simplequant-paper.timer"] in fake.calls
    schedule.delete_task()
    assert not (d / "simplequant-paper.timer").exists()


def test_schedule_cron_fallback(monkeypatch):
    from simplequant.paper import schedule
    monkeypatch.setattr(schedule, "SYSTEM", "linux")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/bin/" + name)
    fake = _FakeRun(ok=lambda args: args[0] != "systemctl")       # 没有 systemd 用户实例
    fake.cron = "0 1 * * * other-job\n"
    monkeypatch.setattr(schedule.subprocess, "run", fake)
    assert schedule.backend() == "cron" and not schedule.task_exists()
    assert schedule.create_task("19:00")[0] and schedule.create_task("19:30")[0]     # 第二次替换，不重复
    lines = fake.cron.splitlines()
    assert lines[0] == "0 1 * * * other-job" and len(lines) == 2
    assert lines[1].startswith("30 19 * * 1-5 /bin/sh ") and lines[1].endswith(schedule.CRON_MARK)
    assert schedule.task_exists()
    schedule.delete_task()
    assert fake.cron.splitlines() == ["0 1 * * * other-job"]
    assert not schedule.create_task("7pm")[0]


def _fake_cash_div_setup(monkeypatch, fund_div):
    """不复权 10 元，第 5 天每份分红 0.5 元；前复权价恒为 9.5"""
    from simplequant.paper import runner
    from simplequant.data import cash_dividend as cd
    idx = pd.bdate_range("2026-09-01", periods=15)
    raw = pd.DataFrame({c: [10.0] * 5 + [9.5] * 10 for c in ("open", "high", "low", "close")}, index=idx)
    raw["volume"] = 1e6
    qfq = raw.copy()
    qfq.iloc[:5, :4] = 9.5

    class FakeSource:
        def fetch(self, symbol, start, end, adjust="qfq", **kw):
            return (qfq if adjust else raw).copy()
    monkeypatch.setitem(runner.SOURCES, "fake", FakeSource())
    monkeypatch.setattr(runner, "load_calendar", lambda: idx)
    monkeypatch.setattr(runner, "latest_expected_day", lambda cal: idx[-1])
    monkeypatch.setattr(cd, "fund_dividends", fund_div)
    acc = create_account("现金分红", {"kind": "template", "template": "buy_hold", "params": {}},
                         BrokerConfig(dividend="cash"), idx[0].date().isoformat(),
                         assets=[{"source": "fake", "symbol": "510300", "asset": "etf", "name": "510300"}])
    return runner, acc, idx, raw


def test_single_cash_dividend_account(monkeypatch):
    """现金分红账户：行情换成不复权价 + 分红、最新价格等于真实价格，分红到账"""
    runner, acc, idx, raw = _fake_cash_div_setup(
        monkeypatch, lambda s: pd.DataFrame({"ex_date": [idx[5]], "cash": [0.5], "bonus": [0.0], "reserve": [0.0]}))
    prices = runner.refresh_single_data(acc)
    df = prices["510300"]
    assert "div_keep" in df.columns and df["close"].iloc[-1] == pytest.approx(9.5)
    assert runner.dividend_notes(acc) == {"skipped": {}, "patched": {}}
    s = run_account(acc, idx, prices=runner.load_single_data(acc))
    assert s["metrics"]["dividend_cash"] > 0 and s["metrics"]["dividend_tax"] == 0     # ETF 免税
    pos = s["positions"][0]
    assert pos["size"] % 100 == 0 and pos["price"] == pytest.approx(9.5)


def test_single_cash_dividend_falls_back_to_qfq(monkeypatch):
    from simplequant.data import cash_dividend as cd

    def fail(symbol):
        raise cd.Unsupported("no_dividends")
    runner, acc, idx, raw = _fake_cash_div_setup(monkeypatch, fail)
    df = runner.refresh_single_data(acc)["510300"]
    assert "div_keep" not in df.columns
    assert runner.dividend_notes(acc)["skipped"] == {"510300": "no_dividends"}
    s = run_account(acc, idx, prices=runner.load_single_data(acc))
    assert "dividend_cash" not in s["metrics"]
