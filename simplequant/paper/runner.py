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
from dataclasses import asdict

import pandas as pd

from .. import strategies
from ..data import SOURCES
from ..engine import run_backtest, BrokerConfig
from ..engine.base_strategy import ORDER_COLUMNS
from .account import PaperAccount, NAV_COLUMNS, new_id
from .calendar import load_calendar, next_trading_day, next_trading_days, latest_expected_day

HISTORY_DAYS = 800        # 单标的：开始日之前取多少天历史用于预热指标
PANEL_WARMUP_DAYS = 400   # 选股：同上


# ---------------- 开户 ----------------
def create_account(name: str, spec: dict, broker: BrokerConfig, start: str, assets: list | None = None,
                   universe: str = "") -> PaperAccount:
    kind = "selection" if spec.get("kind") == "selection" else "single"
    acc = PaperAccount(id=new_id(name), name=name, kind=kind, spec=spec, broker=asdict(broker), start=start,
                       assets=assets or [], universe=universe or spec.get("universe", ""))
    acc.save()
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
        kw = {"asset": a["asset"]} if a.get("asset") else {}
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


def refresh_selection_data(acc: PaperAccount, store, done: set | None = None, workers: int = 4,
                           calendar: pd.DatetimeIndex | None = None) -> None:
    """选股账户：增量更新股票池数据（同一次运行中同一个股票池只更新一次）"""
    from ..stocks import UNIVERSES, FACTORS, universe
    done = done if done is not None else set()
    if acc.universe in done:
        return
    # 只更新到"此刻应有数据"的最新交易日：数据已到这天的股票会被跳过，周末/盘中重复运行很快
    today = latest_expected_day(calendar if calendar is not None else load_calendar()).date().isoformat()
    if universe.kind(acc.universe) == "cb":          # 可转债：列表、条款、指数、日线一起增量更新
        first = (pd.Timestamp(acc.start) - pd.Timedelta(days=PANEL_WARMUP_DAYS)).date().isoformat()
        universe.update(acc.universe, first, today, workers=workers)
        done.add(acc.universe)
        return
    first =store.load_index(UNIVERSES[acc.universe]["index"]).index[0].date().isoformat() \
        if (store.root / "index" / f"{UNIVERSES[acc.universe]['index']}.parquet").exists() else acc.start
    uni = store.update_universe(acc.universe, first, today)
    store.update_index(UNIVERSES[acc.universe]["index"], first, today)
    codes = sorted(uni["code"].unique())
    store.update(codes, first, today, workers=workers)
    if any(FACTORS.get(f["key"], {}).get("requires_fin") for f in acc.spec["factors"]) or \
            (acc.spec.get("neutralize") or {}).get("size"):
        store.update_fundamentals(codes, int(first[:4]) - 1, workers=workers)
    if (acc.spec.get("neutralize") or {}).get("industry"):
        store.update_industry()
    if acc.spec.get("dividend") == "cash":
        store.update_dividends(codes, int(first[:4]) - 1, workers=workers)
    done.add(acc.universe)


def load_selection_panel(acc: PaperAccount, store):
    from ..stocks import universe
    idx = universe.load_benchmark(acc.universe, store)
    start = max(idx.index[0], pd.Timestamp(acc.start) - pd.Timedelta(days=PANEL_WARMUP_DAYS))
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
    broker = BrokerConfig(**acc.broker)
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
    eq = res.equity[NAV_COLUMNS]
    new_nav = eq[eq.index > through] if through is not None else eq
    nav = pd.concat([nav, new_nav]) if len(nav) else new_nav.copy()
    acc.save_nav(nav[~nav.index.duplicated(keep="first")].sort_index())

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


def run_all(accounts: list[PaperAccount], store=None, refresh: bool = True, log=print) -> dict:
    """更新数据并推进所有启用中的账户；返回 {账户id: 错误信息或空}"""
    from ..stocks import StockStore
    store = store or StockStore()
    calendar = load_calendar()
    done, results = set(), {}
    for acc in accounts:
        if acc.status != "active":
            continue
        try:
            log(f"[{acc.name}] updating data…")
            if acc.kind == "selection":
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
