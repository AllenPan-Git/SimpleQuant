"""
模拟盘运行

每次运行（通常在每个交易日收盘后）：
1. 更新数据
2. 用和回测完全相同的代码，把策略从开始日重放到最新数据
3. 重放中新产生的成交追加到账本；已记录的成交不改写。若重放与账本不一致（数据被修订等），记录提示
4. 最后一根 K 线上发出、尚未成交的委托 = 下一个交易日开盘要执行的"今日信号"
错过的交易日（电脑没开、几天没运行）会在下次运行时自动补上。
"""

import datetime as dt
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict

import pandas as pd

from .. import strategies, telemetry
from ..data import SOURCES
from ..engine import run_backtest, BrokerConfig
from ..engine.base_strategy import ORDER_COLUMNS
from . import account as _account
from .account import PaperAccount, NAV_COLUMNS, new_id, load_account
from .calendar import load_calendar, next_trading_day, next_trading_days, latest_expected_day

HISTORY_DAYS = 800        # 单标的：开始日之前取多少天历史用于预热指标
PANEL_WARMUP_DAYS = 400   # 选股：同上（机器学习、IC 回看期超过 252 天时更长，见 stocks.selection.panel_warmup_days）


# ---------------- 开户 ----------------
def create_account(name: str, spec: dict, broker: BrokerConfig, start: str, assets: list | None = None,
                   universe: str = "") -> PaperAccount:
    kind = "selection" if spec.get("kind") == "selection" else "single"
    acc = PaperAccount(id=new_id(name), name=name, kind=kind, spec=spec, broker=asdict(broker), start=start,
                       assets=assets or [], universe=universe or spec.get("universe", ""), adjust_method="ratio")
    acc.save()
    telemetry.count(f"btn:paper_{kind}")
    telemetry.milestone("paper")
    return acc


# ---------------- 数据 ----------------
def refresh_single_data(acc: PaperAccount, today: str | None = None) -> dict[str, pd.DataFrame]:
    """
    单标的账户：重新获取前复权日线（最新价格与真实价格一致，便于按真实股数下单）。
    现金分红账户再换成「不复权价 + 分红」计算的行情（data/cash_dividend.py），同样缩放到最新价格等于真实价格；
    没能换的标的仍按前复权（分红再投资），原因和补上的分红日期记在 prices/dividend_notes.json
    """
    today = today or dt.date.today().isoformat()
    hist_start = (pd.Timestamp(acc.start) - pd.Timedelta(days=HISTORY_DAYS)).date().isoformat()
    # 盘中 AKShare 会返回当天尚未走完的 K 线：只保留到"此刻应有完整数据"的交易日，避免用盘中价格生成信号
    complete_through = latest_expected_day(load_calendar())
    (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
    cash_div = acc.broker.get("dividend") == "cash"
    notes = {"skipped": {}, "patched": {}}
    out = {}
    for a in acc.assets:
        src = SOURCES[a.get("source", "akshare")]
        kw = acc.fetch_kwargs(a.get("asset", ""), a.get("source", "akshare"))
        df = src.fetch(a["symbol"], hist_start, today, freq="1d", adjust="qfq", **kw)
        df = df.loc[:complete_through]
        if cash_div:
            df = _cash_dividend_prices(acc, a, src, kw, df, notes)
        df.to_parquet(acc.dir / "prices" / f"{a['symbol']}.parquet")
        out[a["name"]] = df
    path = acc.dir / "prices" / "dividend_notes.json"
    if cash_div:
        path.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    return out


def _cash_dividend_prices(acc: PaperAccount, a: dict, src, kw: dict, df: pd.DataFrame, notes: dict) -> pd.DataFrame:
    from ..data import cash_dividend as cd
    from ..data.library import DatasetMeta
    first, last = df.index[0].date().isoformat(), df.index[-1].date().isoformat()
    meta = DatasetMeta(id="", name=a["name"], symbol=a["symbol"], source=a.get("source", "akshare"), freq="1d",
                       adjust="qfq", start=first, end=last, extra=dict(kw))
    try:
        try:
            raw = src.fetch(a["symbol"], first, last, freq="1d", adjust="", **kw)
        except Exception:  # noqa: BLE001 - 网络问题：这个标的仍按前复权
            raise cd.Unsupported("no_unadjusted") from None
        new, patched = cd.prepare_one(meta, df, raw=raw)
    except cd.Unsupported as e:
        notes["skipped"][a["name"]] = e.args[0]
        return df
    patched = [str(d) for d in patched if str(d) >= acc.start]      # 开始日之前的不影响账户
    if patched:
        notes["patched"][a["name"]] = patched
    return cd.rebase_to_last(new, raw)


def dividend_notes(acc: PaperAccount) -> dict:
    """单标的现金分红账户最近一次更新数据时的说明：{"skipped": {名称: 原因}, "patched": {名称: [日期]}}"""
    path = acc.dir / "prices" / "dividend_notes.json"
    if acc.broker.get("dividend") != "cash" or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_single_data(acc: PaperAccount) -> dict[str, pd.DataFrame]:
    return {a["name"]: pd.read_parquet(acc.dir / "prices" / f"{a['symbol']}.parquet") for a in acc.assets}


def _panel_since(acc) -> pd.Timestamp:
    """选股账户的面板从哪天开始：开始日减去预热期"""
    from ..stocks.selection import panel_warmup_days
    return pd.Timestamp(acc.start) - pd.Timedelta(days=panel_warmup_days(acc.spec))


def selection_codes(uni: pd.DataFrame, since: pd.Timestamp) -> list[str]:
    """
    模拟盘用得到的股票：since 那天（按不晚于它的最近一次成分股快照）及以后在股票池里的。
    更早就调出的股票在面板区间里从来不是成分股：选股的打分、中性化、IC、机器学习都只看当天的成分股，
    也不会买进它们，所以不用每天更新（沪深300 从 2022 年起进过指数的约 490 只，开始日附近用得到的约 330 只）
    """
    dates = uni["date"]
    snap = dates[dates <= since].max()
    return sorted(uni.loc[dates >= (snap if pd.notna(snap) else since), "code"].unique())


def refresh_selection_data(acc: PaperAccount, store, done: set | None = None, workers: int = 4,
                           calendar: pd.DatetimeIndex | None = None) -> None:
    """选股账户：增量更新股票池里用得到的股票（见 selection_codes）；同一次运行中同样的范围只更新一次"""
    from ..stocks import UNIVERSES, FACTORS, universe
    from ..stocks.selection import panel_warmup_days, needs_fin, needs_risk
    done = done if done is not None else set()
    since = _panel_since(acc)
    key = acc.universe if universe.kind(acc.universe) == "cb" else (acc.universe, since.date().isoformat())
    if key in done:
        return
    # 只更新到"此刻应有数据"的最新交易日：数据已到这天的股票会被跳过，周末/盘中重复运行很快
    today = latest_expected_day(calendar if calendar is not None else load_calendar()).date().isoformat()
    if universe.kind(acc.universe) == "cb":          # 可转债：列表、条款、指数、日线一起增量更新
        first = (pd.Timestamp(acc.start) - pd.Timedelta(days=panel_warmup_days(acc.spec))).date().isoformat()
        universe.update(acc.universe, first, today, workers=workers)
        done.add(acc.universe)
        return
    first = store.load_index(UNIVERSES[acc.universe]["index"]).index[0].date().isoformat() \
        if (store.root / "index" / f"{UNIVERSES[acc.universe]['index']}.parquet").exists() else acc.start
    uni = store.update_universe(acc.universe, first, today)
    store.update_index(UNIVERSES[acc.universe]["index"], first, today)
    codes = selection_codes(uni, since)
    store.update(codes, first, today, workers=workers)
    if needs_fin(acc.spec):
        store.update_fundamentals(codes, int(first[:4]) - 1, workers=workers)
    if (acc.spec.get("neutralize") or {}).get("industry"):
        store.update_industry()
    if acc.spec.get("dividend") == "cash" or \
            any(FACTORS.get(f["key"], {}).get("requires_div") for f in acc.spec["factors"]):
        store.update_dividends(codes, int(first[:4]) - 1, workers=workers)
    if needs_risk(acc.spec):
        store.update_risk(codes, int(first[:4]) - 1, workers=workers)
    done.add(key)


def load_selection_panel(acc: PaperAccount, store):
    """
    选股账户的面板。仍放全部进过股票池的股票（包括 refresh_selection_data 不再更新的、早已调出的）：
    它们在面板区间里不是成分股，各种计算先按成分股筛掉，数据旧了不影响结果；少放几列反而会改变横截面统计的
    浮点运算顺序，分数几乎相同的股票可能换了先后，与账本里已记录的成交对不上
    """
    from ..stocks import universe
    idx = universe.load_benchmark(acc.universe, store)
    start = max(idx.index[0], _panel_since(acc))
    return universe.build(acc.universe, start.date().isoformat(), idx.index[-1].date().isoformat(), store)


def _reason(r):
    """下单理由存成 [文本键, 参数]；代码策略可以直接写一句话"""
    if isinstance(r, tuple):
        return list(r)
    return [r, {}] if isinstance(r, str) and r else None


def with_rates(acc: PaperAccount, prices: dict, refresh: bool = False) -> dict:
    """规则用到利率 / 信用利差时并入利率数据；refresh 时本地数据旧了先更新（当年部分约 12 秒）"""
    from ..rules import macro_columns
    cols = macro_columns(acc.spec["rule"]) if acc.spec.get("kind") == "rule" else set()
    if not cols:
        return prices
    from ..bonds.rates import RatesStore, attach
    st = RatesStore()
    if refresh and st.stale():
        st.update()
    if not st.ready():
        raise ValueError("rates data missing; download it on the Data page / 缺少利率数据，请先在「数据」页下载")
    return attach(prices, cols, st.load())


# ---------------- 重放 ----------------
def replay(acc: PaperAccount, calendar: pd.DatetimeIndex, prices: dict | None = None, panel=None):
    """用回测代码把策略从开始日重放到最新数据；返回 (回测结果, 名称映射, 最新数据日期)"""
    # 0.2.4 之前开设的账户没有 price_limit：保持当时的成交规则，不改写已有的成交
    broker = BrokerConfig(**{"price_limit": False, **acc.broker})
    if acc.kind == "selection":
        from ..stocks import run_selection
        last = panel.calendar[-1]
        res = run_selection(panel, acc.spec, broker, start=acc.start, allow_empty=True,
                            next_days=next_trading_days(calendar, last))
        return res, panel.names, last
    cls, params = strategies.resolve(acc.spec)
    res = run_backtest(prices, cls, params, broker, trade_start=acc.start)
    last = max(df.index[-1] for df in prices.values())
    return res, {n: n for n in prices}, last


def _key(df: pd.DataFrame) -> list:
    return [(pd.Timestamp(r.time), r.symbol, r.side, round(float(r.size), 2)) for r in df.itertuples()]


def run_account(acc: PaperAccount, calendar: pd.DatetimeIndex | None = None, prices: dict | None = None,
                panel=None) -> dict:
    """在已准备好的数据上推进账户；返回新的 state"""
    calendar = calendar if calendar is not None else load_calendar()
    res, names, last = replay(acc, calendar, prices, panel)
    through = pd.Timestamp(acc.data_through) if acc.data_through else None

    # 1. 账本：只追加新成交；检查重放与已记录部分是否一致
    ledger = acc.fills()
    replay_fills = res.orders.copy()
    divergence = ""
    if through is not None:
        old_part = replay_fills[pd.to_datetime(replay_fills["time"]) <= through]
        if _key(old_part) != _key(ledger):
            divergence = "paper.divergence"
        new = replay_fills[pd.to_datetime(replay_fills["time"]) > through]
    else:
        new = replay_fills
    if len(new):
        ledger = pd.concat([ledger, new[ORDER_COLUMNS]], ignore_index=True) if len(ledger) else new[ORDER_COLUMNS].copy()
    acc.save_fills(ledger.reset_index(drop=True))

    # 2. 资产曲线：只追加新日期
    nav = acc.nav()
    dca = bool(acc.broker.get("contribution"))
    eq = res.equity[NAV_COLUMNS + (["invested"] if "invested" in res.equity else [])]
    if dca and "invested" not in eq:            # 定投账户还没到第一次追加：累计投入 = 初始资金
        eq = eq.assign(invested=float(acc.broker["cash"]))
    new_nav = eq[eq.index > through] if through is not None else eq
    nav = pd.concat([nav, new_nav]) if len(nav) else new_nav.copy()
    nav = nav[~nav.index.duplicated(keep="first")].sort_index()
    if acc.kind == "selection":
        # 基准只取决于指数数据，按重放结果整列重写（股票池基准改成全收益指数之前，记录的是价格指数）
        nav.loc[nav.index.isin(eq.index), "benchmark"] = eq["benchmark"]
    acc.save_nav(nav)

    # 3. 今日信号与持仓
    execute_on = next_trading_day(calendar, last)
    value = float(res.equity["value"].iloc[-1])
    # 理由存成 [文本键, 参数]，由界面按当前语言显示
    signals = [{**o, "name": names.get(o["symbol"], o["symbol"]), "est_value": o["size"] * o["ref_price"],
                "reason": _reason(o.get("reason"))}
               for o in res.pending]
    positions = []
    for p in res.positions:
        mv = p.get("market_value", p["size"] * p["price"])      # 选股持仓的市值含持有期间分红
        positions.append({**p, "name": names.get(p["symbol"], p["symbol"]), "value": mv,
                          "weight": mv / value if value else 0.0})
    acc.data_through = last.date().isoformat()
    acc.last_run = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    acc.save()
    state = {"as_of": acc.data_through, "execute_on": execute_on.date().isoformat(), "signals": signals,
             "positions": positions, "value": value, "cash": float(res.equity["cash"].iloc[-1]),
             "metrics": {k: (None if v != v else v) for k, v in res.metrics.items()} if res.metrics else {},
             "divergence": divergence, "started": acc.data_through >= acc.start}
    acc.save_state(state)
    return state


# ---------------- 同一时间只运行一次 ----------------
LOCK_WAIT_LIMIT = 3 * 3600    # 另一处的更新超过这么久仍未结束时放弃本次（正常一次几分钟到几十分钟）
LOCK_POLL = 1.0


class RunLockTimeout(Exception):
    pass


def _try_lock(f) -> bool:
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f):
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(log=print):
    """
    同一时间只让一次模拟盘更新运行：定时任务、程序窗口里的按钮（离开页面后仍在后台跑的那次也算）共用这把锁。
    两次同时跑时，两边都把数据完整下载一遍、抢 BaoStock 的连接，一边重写数据文件时另一边还可能读到空文件。
    用操作系统的文件锁（Windows msvcrt.locking，其它系统 flock，同一进程里两次打开也互斥）：
    持有它的进程退出（包括崩溃、被结束）时自动释放，不会留下锁死的文件。
    yield 是否等过另一处（等过时账户文件可能已被改写，调用方要重新读取）
    """
    _account.PAPER_ROOT.mkdir(parents=True, exist_ok=True)
    with open(_account.PAPER_ROOT / "run.lock", "a+") as f:
        waited, deadline = False, time.monotonic() + LOCK_WAIT_LIMIT
        while not _try_lock(f):
            if not waited:
                log("another update is running (scheduled task or the app window); waiting for it to finish / "
                    "另一处正在更新模拟账户（定时任务或程序窗口），等它完成后继续…")
                waited = True
            if time.monotonic() > deadline:
                raise RunLockTimeout()
            time.sleep(LOCK_POLL)
        try:
            yield waited
        finally:
            _unlock(f)


def _reload(acc: PaperAccount) -> PaperAccount | None:
    """重新读取账户（等待期间可能被另一处更新、被暂停或被删除）；已删除时为 None"""
    path = acc.dir / "account.json"
    return load_account(acc.id, acc.dir.parent) if path.exists() else None


def run_all(accounts: list[PaperAccount], store=None, refresh: bool = True, log=print) -> dict:
    """更新数据并推进所有启用中的账户；返回 {账户id: 错误信息或空}。另一处正在更新时先等它做完（见 run_lock）"""
    try:
        with run_lock(log) as waited:
            if waited:        # 另一处刚更新完：数据已是最新，这次只需几秒；账户以文件里的为准
                accounts = [a for a in map(_reload, accounts) if a is not None]
            return _run_all(accounts, store, refresh, log)
    except RunLockTimeout:
        log(f"another update is still running after {LOCK_WAIT_LIMIT // 3600} hours; skipped / "
            f"另一处的更新超过 {LOCK_WAIT_LIMIT // 3600} 小时仍未结束，本次跳过")
        return {}


def _run_all(accounts: list[PaperAccount], store, refresh: bool, log) -> dict:
    from ..stocks import StockStore
    store = store or StockStore()
    calendar = load_calendar()
    done, results = set(), {}
    for acc in accounts:
        if acc.status != "active":
            continue
        try:
            log(f"[{acc.name}] updating data…")
            if acc.kind == "portfolio":
                from . import portfolio
                if refresh:
                    portfolio.refresh_data(acc, store, done, calendar)
                state = portfolio.run_portfolio(acc, calendar, store)
            elif acc.kind == "selection":
                if refresh:
                    refresh_selection_data(acc, store, done, calendar=calendar)
                state = run_account(acc, calendar, panel=load_selection_panel(acc, store))
            else:
                prices = refresh_single_data(acc) if refresh else load_single_data(acc)
                state = run_account(acc, calendar, prices=with_rates(acc, prices, refresh))
            log(f"[{acc.name}] data through {state['as_of']}, value {state['value']:,.0f}, "
                f"{len(state['signals'])} signal(s) for {state['execute_on']}")
            expected = latest_expected_day(calendar).date().isoformat()
            if state["as_of"] < expected:
                # 数据源还没发布当天数据：信号仍基于前一天，需要稍后再运行一次
                log(f"[{acc.name}] WARNING: data for {expected} not published yet (have {state['as_of']}); "
                    f"run again later / 当天数据尚未发布，请稍后再运行")
                state["data_pending"] = expected
                acc.save_state(state)
            results[acc.id] = ""
            acc.clear_error()
        except Exception as e:  # noqa: BLE001 - 一个账户失败不影响其它账户
            log(f"[{acc.name}] FAILED: {type(e).__name__}: {e}")
            results[acc.id] = f"{type(e).__name__}: {e}"
            acc.save_error(results[acc.id])        # 首页、模拟盘页据此提示
    return results
