"""
组合模拟账户：按资产配置的权重持有多个成分，按再平衡规则调整

    account.spec = {"kind": "portfolio", "sleeves": [候选], "weights": {候选 id: 权重},
                    "rebalance": "quarterly", "cost": 0.0005}
候选在开户时存入策略描述（sleeve["spec"]）和数据来源，之后改动同名策略不影响账户。

每次运行：
1. 各成分从开始日重放：现金类按固定收益率逐日计息；买入持有用前复权收盘价；
   择时、选股策略用回测代码（与单独的模拟账户相同，资金按 100 万计算，再按成分实际金额折算）
2. 用组合回测的同一个函数（allocation.portfolio.simulate）按权重合成、按规则再平衡；
   最后一天收盘若到了再平衡时点，同样再平衡，调整金额作为下一个交易日开盘的委托
3. 资产曲线、再平衡记录只追加；已记录的部分与重放不一致时提示（数据被修订等）
"""

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd

from .. import strategies
from ..allocation import sleeves as SL
from ..allocation.portfolio import COST, simulate
from ..data import SOURCES
from ..engine import run_backtest
from ..engine.results import compute_metrics, TRADING_DAYS
from .account import PaperAccount, NAV_COLUMNS, new_id
from .calendar import load_calendar, next_trading_day, next_trading_days, latest_expected_day
from .runner import HISTORY_DAYS, refresh_selection_data, load_selection_panel, with_rates

REBALANCE_COLUMNS = ["time", "sleeve", "amount", "cost"]
LOT = 100
DIVERGENCE_TOL = 1e-4     # 已记录的资产与重放结果相对差超过这个值视为不一致


# ---------------- 开户 ----------------
def create_account(name: str, sleeves: list[dict], weights: dict[str, float], rebalance: str, cash: float,
                   start: str) -> PaperAccount:
    """sleeves：资产配置的候选；权重为 0 的候选不进入账户"""
    used = [s for s in sleeves if weights.get(s["id"], 0) > 0]
    if not used:
        raise ValueError("all weights are zero / 全部权重为零")
    total = sum(weights[s["id"]] for s in used)
    frozen = []
    for s in used:
        s = dict(s)
        if s["kind"] in ("timing", "selection"):
            s["spec"] = SL.strategy_spec(s)
        if s["kind"] in ("hold", "timing"):
            meta = SL.find_dataset(s["symbol"])
            s["source"] = meta.source if meta and meta.source in ("akshare", "baostock") else "akshare"
            s["asset"] = (meta.extra.get("asset", "") if meta else "") or ("etf" if s["source"] == "akshare" else "")
        frozen.append(s)
    spec = {"kind": "portfolio", "sleeves": frozen, "weights": {s["id"]: weights[s["id"]] / total for s in frozen},
            "rebalance": rebalance, "cost": COST}
    acc = PaperAccount(id=new_id(name), name=name, kind="portfolio", spec=spec, broker={"cash": float(cash)},
                       start=start)
    acc.save()
    return acc


def _sub(acc: PaperAccount, s: dict) -> SimpleNamespace:
    """选股成分当作一个单独账户更新数据、构建面板（复用单独选股账户的函数）"""
    return SimpleNamespace(universe=s["spec"]["universe"], start=acc.start, spec=s["spec"])


# ---------------- 数据 ----------------
def refresh_data(acc: PaperAccount, store=None, done: set | None = None, calendar: pd.DatetimeIndex | None = None,
                 selection: bool = True) -> None:
    """
    买入持有、择时成分：重新获取前复权日线（最新价格等于真实价格，便于按真实股数下单）；
    选股成分：增量更新股票池数据。selection=False 时只更新行情（开户时选股成分直接用本机已有数据）
    """
    calendar = calendar if calendar is not None else load_calendar()
    today = dt.date.today().isoformat()
    hist_start = (pd.Timestamp(acc.start) - pd.Timedelta(days=HISTORY_DAYS)).date().isoformat()
    complete_through = latest_expected_day(calendar)
    (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
    fetched = set()
    for s in acc.spec["sleeves"]:
        if s["kind"] in ("hold", "timing") and s["symbol"] not in fetched:
            kw = {"asset": s["asset"]} if s.get("asset") else {}
            df = SOURCES[s.get("source", "akshare")].fetch(s["symbol"], hist_start, today, freq="1d", adjust="qfq", **kw)
            df.loc[:complete_through].to_parquet(acc.dir / "prices" / f"{s['symbol']}.parquet")
            fetched.add(s["symbol"])
    if selection and any(s["kind"] == "selection" for s in acc.spec["sleeves"]):
        from ..stocks import StockStore
        store = store or StockStore()
        for s in acc.spec["sleeves"]:
            if s["kind"] == "selection":
                refresh_selection_data(_sub(acc, s), store, done if done is not None else set(), calendar=calendar)
    for s in acc.spec["sleeves"]:
        if s["kind"] == "timing":
            with_rates(SimpleNamespace(spec=s["spec"]), {}, refresh=True)   # 规则用到利率时顺带更新


def _prices(acc: PaperAccount, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(acc.dir / "prices" / f"{symbol}.parquet")


def data_last(acc: PaperAccount, store=None) -> pd.Timestamp | None:
    """各成分本地数据共同的最新日期（最早结束的那个）；只有现金类时为 None"""
    lasts = []
    for s in acc.spec["sleeves"]:
        if s["kind"] in ("hold", "timing"):
            lasts.append(_prices(acc, s["symbol"]).index[-1])
        elif s["kind"] == "selection":
            from ..stocks import StockStore, universe
            lasts.append(universe.load_benchmark(s["spec"]["universe"], store or StockStore()).index[-1])
    return min(lasts) if lasts else None


# ---------------- 各成分重放 ----------------
def _replay_sleeve(acc: PaperAccount, s: dict, calendar: pd.DatetimeIndex, store) -> dict:
    """
    返回 {"returns": 日收益率, "last": 数据最新日期, "value": 子账户资产, "positions": [...], "pending": [...],
          "names": {代码: 名称}, "lot": 每手数量}；持仓与委托按子账户计，之后按成分金额 / 子账户资产折算
    """
    kind, start = s["kind"], pd.Timestamp(acc.start)
    if kind == "hold":
        df = _prices(acc, s["symbol"])
        close = df["close"]
        price = float(close.iloc[-1])
        # 子账户 = 持有 1 份，资产等于最新价格
        return {"returns": close.pct_change().loc[close.index > start].dropna(), "last": close.index[-1],
                "value": price, "positions": [{"symbol": s["symbol"], "size": 1.0, "price": price}], "pending": [],
                "names": {s["symbol"]: s["name"]}, "lot": LOT}
    if kind == "timing":
        prices = with_rates(SimpleNamespace(spec=s["spec"]), {s["symbol"]: _prices(acc, s["symbol"])})
        cls, params = strategies.resolve(s["spec"])
        res = run_backtest(prices, cls, params, SL.broker_for(s["class"]), trade_start=acc.start)
        value = res.equity["value"]
        return {"returns": value.pct_change().loc[value.index > start].dropna(), "last": value.index[-1],
                "value": float(value.iloc[-1]), "positions": res.positions, "pending": res.pending,
                "names": {s["symbol"]: s["name"]}, "lot": LOT}
    if kind == "selection":
        from ..stocks import StockStore, run_selection
        panel = load_selection_panel(_sub(acc, s), store or StockStore())
        last = panel.calendar[-1]
        res = run_selection(panel, s["spec"], SL.selection_broker(s["spec"]), start=acc.start, allow_empty=True,
                            next_days=next_trading_days(calendar, last))
        value = res.equity["value"]
        return {"returns": value.pct_change().loc[value.index > start].dropna(), "last": last,
                "value": float(value.iloc[-1]), "positions": res.positions, "pending": res.pending,
                "names": panel.names, "lot": getattr(panel, "lot_size", LOT)}
    raise ValueError(f"unknown sleeve kind {kind}")


def _cash_returns(s: dict, days: pd.DatetimeIndex) -> pd.Series:
    daily = (1 + float(s.get("rate", SL.CASH_RATE))) ** (1 / TRADING_DAYS) - 1
    return pd.Series(daily, index=days)


def next_rebalance(rebalance: str, calendar: pd.DatetimeIndex, after) -> pd.Timestamp | None:
    """定期再平衡：after 之后（含 after）下一次再平衡的收盘日；按偏离或不再平衡时为 None"""
    if rebalance not in ("monthly", "quarterly", "yearly"):
        return None
    from ..allocation.portfolio import _period_key
    days = calendar[calendar >= pd.Timestamp(after)]
    key = _period_key(days, rebalance)
    hit = np.nonzero(key[1:] != key[:-1])[0]
    return days[hit[0]] if len(hit) else None


# ---------------- 推进 ----------------
def run_portfolio(acc: PaperAccount, calendar: pd.DatetimeIndex | None = None, store=None) -> dict:
    """在已准备好的数据上推进组合账户；返回新的 state"""
    calendar = calendar if calendar is not None else load_calendar()
    sleeves = acc.spec["sleeves"]
    ids = [s["id"] for s in sleeves]
    w = np.array([acc.spec["weights"][i] for i in ids], dtype=float)
    w = w / w.sum()
    cash0 = float(acc.broker["cash"])
    start = pd.Timestamp(acc.start)

    subs = {s["id"]: _replay_sleeve(acc, s, calendar, store) for s in sleeves if s["kind"] != "cash"}
    last = min(x["last"] for x in subs.values()) if subs else latest_expected_day(calendar)
    days = calendar[(calendar > start) & (calendar <= last)]
    R = pd.DataFrame({s["id"]: _cash_returns(s, days) if s["kind"] == "cash" else
                      subs[s["id"]]["returns"].reindex(days).fillna(0.0) for s in sleeves}, index=days)[ids]
    execute_on = next_trading_day(calendar, max(last, start))
    rule, cost = acc.spec["rebalance"], float(acc.spec.get("cost", COST))
    sim = simulate(R.to_numpy(), w, days, rule, cost, cash0, next_day=execute_on)
    bench = simulate(R.to_numpy(), np.full(len(ids), 1 / len(ids)), days, rule, cost, cash0, next_day=execute_on)

    # 1. 资产曲线：开始日为初始资金；只追加新日期，已有部分检查是否一致
    is_cash = np.array([s["kind"] == "cash" for s in sleeves])
    cash_col = (sim.weights * sim.values[:, None])[:, is_cash].sum(axis=1) if len(days) else np.empty(0)
    eq = pd.DataFrame({"value": np.r_[cash0, sim.values], "cash": np.r_[cash0 * w[is_cash].sum(), cash_col],
                       "benchmark": np.r_[cash0, bench.values]}, index=days.insert(0, start))[NAV_COLUMNS]
    through = pd.Timestamp(acc.data_through) if acc.data_through else None
    nav = acc.nav()
    divergence = ""
    if through is not None and len(nav):
        old = nav["value"]
        new_old = eq["value"].reindex(old.index)
        if new_old.isna().any() or (np.abs(new_old / old - 1) > DIVERGENCE_TOL).any():
            divergence = "paper.divergence"
        new_nav = eq[eq.index > through]
    else:
        new_nav = eq
    nav = pd.concat([nav, new_nav]) if len(nav) else new_nav.copy()
    acc.save_nav(nav[~nav.index.duplicated(keep="first")].sort_index())

    # 2. 再平衡记录（只追加）。最后一天收盘的再平衡在下一个交易日开盘执行
    # 成本按各成分调整金额分摊
    rows = [{"time": days[i], "sleeve": ids[j], "amount": float(d[j]), "cost": float(c * abs(d[j]) / np.abs(d).sum())}
            for i, d, c in sim.events for j in range(len(ids)) if abs(d[j]) >= 0.005]
    rebal = pd.DataFrame(rows, columns=REBALANCE_COLUMNS)
    ledger = acc.rebalances()
    new = rebal[rebal["time"] > through] if through is not None else rebal
    if len(new):
        ledger = pd.concat([ledger, new], ignore_index=True) if len(ledger) else new.copy()
    acc.save_rebalances(ledger.reset_index(drop=True))

    # 3. 持仓：各成分金额 × 子账户的持仓比例
    value = float(sim.values[-1]) if len(days) else cash0
    hold = sim.hold                                      # 最后一天收盘（再平衡后）各成分金额
    positions, sleeve_rows, cash_total = [], [], 0.0
    for j, s in enumerate(sleeves):
        sid, amount = s["id"], float(hold[j])
        sleeve_rows.append({"id": sid, "name": s["name"], "kind": s["kind"], "class": s["class"],
                            "target": float(w[j]), "weight": amount / value if value else 0.0, "value": amount})
        if s["kind"] == "cash":
            cash_total += amount
            continue
        sub = subs[sid]
        f = amount / sub["value"] if sub["value"] else 0.0
        invested = 0.0
        for p in sub["positions"]:
            mv = p.get("market_value", p["size"] * p["price"]) * f
            invested += mv
            positions.append({"sleeve": sid, "symbol": p["symbol"], "name": sub["names"].get(p["symbol"], p["symbol"]),
                              "size": p["size"] * f, "price": p["price"], "value": mv,
                              "weight": mv / value if value else 0.0, "lot": sub["lot"]})
        cash_total += amount - invested

    # 4. 下一个交易日的委托：成分自身策略的信号（按成分金额折算）+ 再平衡（或开户建仓）的调整
    signals = []
    for j, s in enumerate(sleeves):
        if s["kind"] == "cash":
            continue
        sub = subs[s["id"]]
        f = float(hold[j]) / sub["value"] if sub["value"] else 0.0
        for o in sub["pending"]:
            signals.append(_signal(s, sub, o["symbol"], o["side"], o["size"] * f, o["ref_price"], o.get("reason")))
    delta, reason = None, ""
    if sim.events and sim.events[-1][0] == len(days) - 1:
        delta, reason = sim.events[-1][1], "pf.reason_rebalance"
    elif not len(days):
        delta, reason = hold, "pf.reason_build"          # 刚开户：按权重买入
    if delta is not None:
        for j, s in enumerate(sleeves):
            sub = subs.get(s["id"])
            if sub is None or not sub["value"] or abs(delta[j]) < 0.005:
                continue
            g = float(delta[j]) / sub["value"]
            for p in sub["positions"]:
                side = "buy" if g > 0 else "sell"
                signals.append(_signal(s, sub, p["symbol"], side, abs(p["size"] * g), p["price"], (reason, {})))

    metrics = {}
    if len(days):
        m = compute_metrics(eq["value"], eq["benchmark"], pd.DataFrame(columns=["pnl_net"]), cash0)
        metrics = {k: (None if v != v else v) for k, v in m.items()}
    acc.data_through = last.date().isoformat()
    acc.last_run = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    acc.save()
    nxt = next_rebalance(rule, calendar, execute_on)
    state = {"as_of": acc.data_through, "execute_on": execute_on.date().isoformat(), "signals": signals,
             "positions": positions, "sleeves": sleeve_rows, "value": value, "cash": cash_total, "metrics": metrics,
             "divergence": divergence, "started": acc.data_through >= acc.start,
             "rebalance_due": reason == "pf.reason_rebalance",
             "next_rebalance": nxt.date().isoformat() if nxt is not None else None}
    acc.save_state(state)
    return state


def _signal(s: dict, sub: dict, symbol: str, side: str, size: float, price: float, reason) -> dict:
    from .runner import _reason
    return {"sleeve": s["id"], "symbol": symbol, "name": sub["names"].get(symbol, symbol), "side": side,
            "size": float(size), "ref_price": float(price), "est_value": float(size * price), "lot": sub["lot"],
            "reason": _reason(reason)}
