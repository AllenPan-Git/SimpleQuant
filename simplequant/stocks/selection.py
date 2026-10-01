"""
多因子选股策略

策略描述（可存成 JSON，也是以后自然语言的接口）：
{
  "kind": "selection",
  "universe": "hs300",
  "factors": [{"key": "ep", "weight": 1, "direction": 1}, {"key": "ret20", "weight": 1, "direction": -1}],
  "top_n": 10,
  "rebalance": "monthly",          # monthly / weekly / 整数（每 N 个交易日）
  "filters": {"exclude_st": true, "min_list_days": 250},
  "position_pct": 95,
  "weighting": "manual",           # manual / ic / icir（按过去一段时间的 IC 自动定权重和方向）
  "ic_lookback": 252,              # IC 加权回看的交易日数
  "neutralize": {"industry": false, "size": false},  # 行业 / 市值中性化
  "dividend": "reinvest"           # reinvest：后复权（分红全额再投资、不扣税）/ cash：现金分红并扣红利税
}

流程：
1. build_schedule：每个调仓日 T 收盘，在可选股票中按合成因子打分，取前 top_n（只用 T 日及以前的数据）
2. run_selection：Backtrader 组合回测，T+1 开盘执行（cheat-on-open，开盘价已知）
   - 先卖出不在新名单里的持仓，再用释放的资金等额买入新名单里没持有的股票；继续入选的持仓不动（减少换手）
   - 停牌或开盘跌停 → 卖不出，之后每天开盘重试；停牌或开盘涨停 → 买不进，本期放弃
   - 数量按不复权价取 100 股整手；手续费、印花税沿用 A 股成本模型
   - 分红：默认后复权（分红全额再投资、不扣税）；"dividend": "cash" 时现金到账并在卖出时扣红利税，
     规则见 dividends.py
"""

from dataclasses import dataclass, field

import backtrader as bt
import numpy as np
import pandas as pd

from ..engine.commission import AShareCommission
from ..engine.results import EquityRecorder, BacktestResult, compute_metrics
from ..engine.runner import BrokerConfig
from ..engine.base_strategy import ORDER_COLUMNS, TRADE_COLUMNS
from ..i18n import L
from . import factors as F
from .dividends import split_factor, tax_rate

REBALANCE = {"monthly": L("每月", "Monthly"), "weekly": L("每周", "Weekly")}
DIVIDEND = {"reinvest": L("分红再投资（后复权）", "Reinvest dividends (back-adjusted)"),
            "cash": L("现金分红并扣红利税", "Cash dividends, taxed")}
DEFAULT_FILTERS = {"exclude_st": True, "min_list_days": 250}


def rebalance_dates(calendar: pd.DatetimeIndex, rule) -> pd.DatetimeIndex:
    s = pd.Series(calendar, index=calendar)
    if rule == "monthly":
        return pd.DatetimeIndex(s.groupby(calendar.to_period("M")).last().values)
    if rule == "weekly":
        return pd.DatetimeIndex(s.groupby(calendar.to_period("W")).last().values)
    n = int(rule)
    return calendar[::n]


def rebalance_horizon(rule) -> int:
    """IC 加权时用的持有期：与调仓间隔一致"""
    return {"monthly": 20, "weekly": 5}.get(rule, None) or int(rule)


WEIGHTING = {"manual": L("手动权重", "Manual weights"), "ic": L("按 IC 加权", "IC-weighted"),
             "icir": L("按 ICIR 加权", "ICIR-weighted")}


@dataclass
class Schedule:
    picks: dict                     # 调仓日 -> [代码]（按得分从高到低）
    scores: dict                    # 调仓日 -> {代码: 得分}
    skipped: list = field(default_factory=list)   # 可选股票不足而跳过的调仓日


def build_schedule(panel, spec: dict, start=None, next_days=None, cache: dict | None = None) -> Schedule:
    """
    start：从这天起才调仓（之前的数据只用来预热因子，如 60 日波动率）
    next_days：数据之后的未来交易日（模拟盘用）。没有它时，数据的最后一天总会被当成"月末/周末"；
               有了它才能正确判断"今天是不是本月最后一个交易日"
    cache：同一面板上反复选股时共用（参数优化），因子只算一次
    """
    filters = {**DEFAULT_FILTERS, **(spec.get("filters") or {})}
    fkey = ("filters", bool(filters["exclude_st"]), int(filters["min_list_days"]))
    sub = cache.setdefault(fkey, {}) if cache is not None else {}
    if "mask" not in sub:
        sub["mask"] = panel.eligible(filters["exclude_st"], filters["min_list_days"])
    mask = sub["mask"]
    score = F.composite(panel, spec["factors"], mask, weighting=spec.get("weighting", "manual"),
                        neutral=spec.get("neutralize"), horizon=rebalance_horizon(spec.get("rebalance", "monthly")),
                        lookback=int(spec.get("ic_lookback", 252)), cache=sub)
    top_n = int(spec["top_n"])
    picks, scores, skipped = {}, {}, []
    cal = panel.calendar
    if next_days is not None and len(next_days):
        cal = cal.union(pd.DatetimeIndex(next_days)[pd.DatetimeIndex(next_days) > cal[-1]])
    dates = rebalance_dates(cal, spec.get("rebalance", "monthly"))
    dates = dates[dates <= panel.calendar[-1]]
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    for d in dates:
        row = score.loc[d].dropna()
        if len(row) < top_n:
            skipped.append(d)
            continue
        best = row.sort_values(ascending=False).head(top_n)
        picks[d] = list(best.index)
        scores[d] = best.to_dict()
    return Schedule(picks=picks, scores=scores, skipped=skipped)


# ---------------- Backtrader ----------------
class SelectionFeed(bt.feeds.PandasData):
    lines = ("can_buy", "can_sell", "ratio")          # ratio = 不复权价 / 后复权价
    params = (("can_buy", "can_buy"), ("can_sell", "can_sell"), ("ratio", "ratio"),
              ("openinterest", -1), ("datetime", None), ("dtnums", None))

    def preload(self):
        """
        整列一次性写入行缓冲（结果与逐行 _load 相同）。
        PandasData 逐格 iloc 读取，几百只股票 × 几年时占回测总时间的八成以上
        """
        df = self.p.dataname
        n = len(df)
        dtnums = self.p.dtnums if self.p.dtnums is not None else \
            [bt.date2num(t.to_pydatetime()) for t in df.index]
        for alias in self.getlinealiases():
            line = getattr(self.lines, alias)
            if alias == "datetime":
                line.array.extend(dtnums)
                continue
            col = self._colmapping[alias]
            line.array.extend(df.iloc[:, col].to_numpy(dtype=float) if col is not None else [float("nan")] * n)
        self._idx = n - 1
        self.home()

    # 每根 K 线 Backtrader 都要给所有行（含 can_buy 等）设置 tick_xxx，几百只股票时很慢；
    # 券商撮合只用到开高低收，只维护这几个
    def _tick_nullify(self):
        self.tick_open = self.tick_high = self.tick_low = self.tick_close = self.tick_volume = None
        self.tick_last = None

    def _tick_fill(self, force=False):
        if force or self.tick_close is None:
            lines = self.lines
            self.tick_open, self.tick_high, self.tick_low = lines.open[0], lines.high[0], lines.low[0]
            self.tick_close, self.tick_volume = lines.close[0], lines.volume[0]
            self.tick_last = self.tick_close


class SelectionStrategy(bt.Strategy):
    params = (("schedule", None), ("position_pct", 95), ("lot_size", 100), ("cash_buffer", 0.01),
              ("dividends", None))        # 现金分红模式：{日期: [(代码, 每股现金, 每股送股, 每股转增)]}

    def __init__(self):
        self.by_name = {d._name: d for d in self.datas}
        self.target = None             # 下一个开盘要执行的名单
        self.retry_sell = set()        # 卖不出、待重试的持仓
        # 买入时的真实股数。持有期间分红会改变后复权因子，若按当前因子折算，分红会变成"多出来的股票"；
        # 实际账户里股数不变、分红是现金，所以持仓和卖出都用买入时的股数（市值/成交额仍包含分红）
        self.raw_shares = {}
        self.order_records, self.trade_records, self.logs = [], [], []
        self.picks = self.p.schedule.picks
        self.div_events = {dte: [(self.by_name[c], *v) for c, *v in evs if c in self.by_name]
                           for dte, evs in (self.p.dividends or {}).items()}
        self.buy_date = {}             # 持仓的买入日期（算红利税的持股期限）
        self.div_taxable = {}          # 持有期间累计的应税分红所得（卖出时按持股期限扣税）
        self.div_cash = self.div_tax = 0.0
        self.div_applied = set()       # (代码, 日期)：实际碰上了的除权除息

    def log(self, key, **kw):
        self.logs.append((self.datas[0].datetime.datetime(0), key, kw))

    def next(self):
        d = pd.Timestamp(self.datas[0].datetime.date(0))
        if d in self.picks:
            self.target = self.picks[d]

    def _held(self):
        return {d for d in self.datas if self.getposition(d).size > 0}

    def _dividends(self):
        """除权除息日开盘前：按前一天收盘的持股派发现金、增加送转的股数（行情已是只按送转复权的价格）"""
        today = pd.Timestamp(self.datas[0].datetime.date(0))
        for d, cash, bonus, reserve in self.div_events.get(today, ()):
            if self.getposition(d).size <= 0 or d not in self.raw_shares:
                continue
            shares = self.raw_shares[d]
            self.div_applied.add((d._name, today))
            amount = shares * cash
            if amount > 0:
                self.broker.add_cash(amount)
                self.div_cash += amount
            self.div_taxable[d] = self.div_taxable.get(d, 0.0) + shares * (cash + bonus * 1.0)   # 送股按面值计税
            added = round(shares * (bonus + reserve))
            self.raw_shares[d] = shares + added
            self.log("sel.dividend", name=d._name, shares=shares, cash=f"{amount:,.2f}", added=added)

    def next_open(self):
        if self.div_events:
            self._dividends()
        # 1. 重试之前卖不出的（按代码排序，保证每次重放的下单顺序一致）
        closing = set()
        for d in sorted(self.retry_sell, key=lambda x: x._name):
            if d.can_sell[0] > 0:
                self.close(data=d)
                self.retry_sell.discard(d)
                closing.add(d)
        if self.target is None:
            return
        target = [self.by_name[c] for c in self.target]
        self.target = None
        # 刚提交卖出的持仓在成交前仍显示为持有：排除掉，否则会被当作落选再卖一次（变成空头）
        held = self._held() - closing

        # 2. 卖出落选的持仓
        freed = 0.0
        for d in sorted(held - set(target), key=lambda x: x._name):
            if d in self.retry_sell:
                continue
            if d.can_sell[0] > 0:
                freed += self.getposition(d).size * d.open[0]
                self.close(data=d)
            else:
                self.retry_sell.add(d)
                self.log("sel.sell_blocked", name=d._name)

        # 3. 等额买入新入选的股票
        new = [d for d in target if d not in held]
        if not new:
            return
        per_name = self.broker.getvalue() * self.p.position_pct / 100 / len(target)
        cash = self.broker.getcash() + freed
        for d in new:
            if d.can_buy[0] <= 0:
                self.log("sel.buy_blocked", name=d._name)
                continue
            raw_price = d.open[0] * d.ratio[0]
            alloc = min(per_name, cash / (1 + self.p.cash_buffer))
            lots = int(alloc / (raw_price * (1 + self.p.cash_buffer)) / self.p.lot_size)
            if lots < 1:
                self.log("log.lot_too_big", name=d._name, cost=raw_price * self.p.lot_size)
                continue
            raw_shares = lots * self.p.lot_size
            self.buy(data=d, size=raw_shares * d.ratio[0])   # Backtrader 里用后复权价，数量相应换算
            cash -= raw_shares * raw_price * (1 + self.p.cash_buffer)

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        d = order.data
        if order.status == order.Completed:
            ratio = d.ratio[0]
            raw_price = order.executed.price * ratio
            if order.isbuy():
                shares = round(abs(order.executed.size) / ratio)
                self.raw_shares[d] = self.raw_shares.get(d, 0) + shares
                self.buy_date.setdefault(d, pd.Timestamp(d.datetime.date(0)))
            else:
                shares = self.raw_shares.pop(d, round(abs(order.executed.size) / ratio))
                self._dividend_tax(d)
            self.order_records.append(dict(zip(ORDER_COLUMNS, (
                d.datetime.datetime(0), d._name, "buy" if order.isbuy() else "sell", shares,
                raw_price, abs(order.executed.size) * order.executed.price, order.executed.comm))))
        else:
            self.logs.append((d.datetime.datetime(0), "log.order_failed",
                              {"name": d._name, "status": ("status." + order.getstatusname(), {})}))

    def _dividend_tax(self, d):
        """卖出时按持股期限补扣红利税"""
        bought = self.buy_date.pop(d, None)
        taxable = self.div_taxable.pop(d, 0.0)
        if not taxable or bought is None:
            return
        tax = taxable * tax_rate(bought, d.datetime.date(0))
        if tax > 0:
            self.broker.add_cash(-tax)
            self.div_tax += tax
            self.log("sel.dividend_tax", name=d._name, tax=f"{tax:,.2f}",
                     days=(pd.Timestamp(d.datetime.date(0)) - bought).days)

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trade_records.append(dict(zip(TRADE_COLUMNS, (
                trade.data._name, bt.num2date(trade.dtopen), bt.num2date(trade.dtclose), trade.barlen,
                trade.pnl, trade.pnlcomm))))


@dataclass
class SelectionResult(BacktestResult):
    schedule: Schedule | None = None
    names: dict = field(default_factory=dict)
    div_missing: list = field(default_factory=list)   # 现金分红模式下缺分红数据、仍按后复权计算的股票
    div_patched: pd.DataFrame | None = None           # 回测里用到的、分红表漏记而按复权因子补上的除权除息


def _feed_frame(panel, code: str, cash_dividend: bool = False) -> pd.DataFrame:
    """
    默认用后复权价；现金分红模式改用只按送转复权的价格（不复权价 × 累计送转因子），
    除息日价格照常下跳，现金由策略另行计入
    """
    prices = ("open", "high", "low", "close")
    if cash_dividend:
        scale = split_factor(panel["div_bonus"][code], panel["div_reserve"][code]) / panel["adj_factor"][code]
        df = pd.DataFrame({f: panel[f][code] * scale for f in prices})
    else:
        df = pd.DataFrame({f: panel[f][code] for f in prices})
    df["volume"] = panel["volume"][code]
    df["ratio"] = panel["raw_close"][code] / df["close"]
    df["can_buy"] = panel.can_buy[code].astype(float)
    df["can_sell"] = panel.can_sell[code].astype(float)
    # 上市前没有价格：用第一个有效值占位（此时 can_buy=0，不会被交易）
    df = df.bfill().ffill()
    df["volume"] = df["volume"].fillna(0)
    return df


def run_selection(panel, spec: dict, broker: BrokerConfig | None = None, schedule: Schedule | None = None,
                  start=None, next_days=None, allow_empty: bool = False) -> SelectionResult:
    """start：回测起点；面板里更早的数据只用来预热因子，权益曲线与指标从 start 起算"""
    broker = broker or BrokerConfig(commission=0.00025, stamp_duty=0.0005)
    schedule = schedule or build_schedule(panel, spec, start, next_days)
    if not schedule.picks:
        if allow_empty:            # 模拟盘刚开户、还没到第一个调仓日：资产不变，没有信号
            return _flat_result(panel, broker, start, schedule)
        raise ValueError("no rebalance date had enough eligible stocks / 没有任何调仓日有足够的可选股票")
    codes = sorted({c for p in schedule.picks.values() for c in p})
    cash_div = spec.get("dividend") == "cash"
    div_codes = {c for c in codes if c in panel.div_codes} if cash_div else set()

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.broker.set_checksubmit(False)          # 先卖后买：资金在成交时检查
    cerebro.broker.setcash(broker.cash)
    cerebro.broker.addcommissioninfo(AShareCommission(
        commission=broker.commission, min_commission=broker.min_commission, stamp_duty=broker.stamp_duty))
    if broker.slippage > 0:
        cerebro.broker.set_slippage_perc(perc=broker.slippage)
    dtnums = [bt.date2num(t.to_pydatetime()) for t in panel.calendar]
    for c in codes:
        cerebro.adddata(SelectionFeed(dataname=_feed_frame(panel, c, c in div_codes), name=c, dtnums=dtnums))
    cerebro.addstrategy(SelectionStrategy, schedule=schedule, position_pct=spec.get("position_pct", 95),
                        dividends=_dividend_events(panel, div_codes) if cash_div else None)
    cerebro.addanalyzer(EquityRecorder, _name="equity")
    strat = cerebro.run()[0]

    equity = strat.analyzers.equity.get_analysis()
    if start is not None:          # 预热期内没有交易，资产恒为初始资金，直接截掉
        equity = equity.loc[pd.Timestamp(start):].copy()
    bench = panel.benchmark.reindex(equity.index).ffill()
    equity["benchmark"] = bench / bench.iloc[0] * broker.cash
    equity["drawdown"] = equity["value"] / equity["value"].cummax() - 1
    orders = pd.DataFrame(strat.order_records, columns=ORDER_COLUMNS)
    trades = pd.DataFrame(strat.trade_records, columns=TRADE_COLUMNS)
    metrics = compute_metrics(equity["value"], equity["benchmark"], trades, broker.cash, broker.risk_free)
    metrics["turnover_annual"] = _annual_turnover(orders, equity)
    if cash_div:
        metrics["dividend_cash"], metrics["dividend_tax"] = strat.div_cash, strat.div_tax
    return SelectionResult(equity=equity, orders=orders, trades=trades, metrics=metrics, logs=strat.logs,
                           prices={}, schedule=schedule, names=panel.names,
                           pending=_selection_pending(strat), positions=_selection_positions(strat),
                           div_missing=sorted(set(codes) - div_codes) if cash_div else [],
                           div_patched=_patched_in(panel.div_patched, strat.div_applied) if cash_div else None)


def _patched_in(patched, applied: set) -> pd.DataFrame | None:
    """补上的除权除息里，回测中确实碰上了（当时持有）的"""
    if patched is None or patched.empty:
        return None
    hit = patched[[(c, d) in applied for c, d in zip(patched["code"], patched["ex_date"])]]
    return hit.sort_values(["ex_date", "code"]).reset_index(drop=True) if len(hit) else None


def _dividend_events(panel, codes) -> dict:
    """{日期: [(代码, 每股现金, 每股送股, 每股转增)]}"""
    out = {}
    for c in sorted(codes):
        cash, bonus, reserve = panel["div_cash"][c], panel["div_bonus"][c], panel["div_reserve"][c]
        hit = (cash > 0) | (bonus > 0) | (reserve > 0)
        for dte in cash.index[hit]:
            out.setdefault(dte, []).append((c, float(cash[dte]), float(bonus[dte]), float(reserve[dte])))
    return out


def _selection_positions(strat) -> list[dict]:
    """持仓：股数为买入时的真实股数，价格为不复权价；market_value 为含分红的实际市值"""
    out = []
    for d in strat.datas:
        pos = strat.getposition(d)
        if pos.size:
            ratio = d.ratio[0]
            shares = strat.raw_shares.get(d, round(pos.size / ratio))
            out.append({"symbol": d._name, "size": shares, "price": float(d.close[0] * ratio),
                        "cost": float(pos.price * pos.size / shares) if shares else 0.0,
                        "market_value": float(pos.size * d.close[0])})
    return out


def _selection_pending(strat) -> list[dict]:
    """
    最后一天收盘后要在下一个开盘执行的交易：卖出落选（及此前卖不出的）持仓，等额买入新入选的股票。
    买入股数按最新收盘价估算，实际以开盘价计算（与回测一致）。
    """
    held = {d for d in strat.datas if strat.getposition(d).size > 0}
    def shares(d):
        return float(strat.raw_shares.get(d, round(strat.getposition(d).size / d.ratio[0])))

    out = [{"symbol": d._name, "side": "sell", "size": shares(d),
            "ref_price": float(d.close[0] * d.ratio[0]), "reason": ("sel.reason_retry", {})}
           for d in sorted(strat.retry_sell, key=lambda x: x._name) if d in held]
    if strat.target is None:
        return out
    target = [strat.by_name[c] for c in strat.target]
    for d in sorted(held - set(target) - strat.retry_sell, key=lambda x: x._name):
        out.append({"symbol": d._name, "side": "sell", "size": shares(d),
                    "ref_price": float(d.close[0] * d.ratio[0]), "reason": ("sel.reason_dropped", {})})
    per_name = strat.broker.getvalue() * strat.p.position_pct / 100 / len(target)
    for d in target:
        if d in held:
            continue
        raw_price = d.close[0] * d.ratio[0]
        lots = int(per_name / (raw_price * (1 + strat.p.cash_buffer)) / strat.p.lot_size)
        out.append({"symbol": d._name, "side": "buy", "size": float(lots * strat.p.lot_size),
                    "ref_price": float(raw_price), "reason": ("sel.reason_picked", {})})
    return out


def _flat_result(panel, broker, start, schedule) -> "SelectionResult":
    idx = panel.calendar if start is None else panel.calendar[panel.calendar >= pd.Timestamp(start)]
    equity = pd.DataFrame({"value": broker.cash, "cash": broker.cash}, index=idx)
    bench = panel.benchmark.reindex(idx).ffill()
    equity["benchmark"] = bench / bench.iloc[0] * broker.cash if len(bench) else broker.cash
    equity["drawdown"] = 0.0
    orders, trades = pd.DataFrame(columns=ORDER_COLUMNS), pd.DataFrame(columns=TRADE_COLUMNS)
    metrics = compute_metrics(equity["value"], equity["benchmark"], trades, broker.cash, broker.risk_free) \
        if len(idx) > 1 else {}
    return SelectionResult(equity=equity, orders=orders, trades=trades, metrics=metrics, schedule=schedule,
                           names=panel.names)


def _annual_turnover(orders: pd.DataFrame, equity: pd.DataFrame) -> float:
    """年化单边换手率：卖出金额合计 / 平均资产 / 年数"""
    if orders.empty:
        return 0.0
    sells = orders.loc[orders["side"] == "sell", "value"].sum()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    return float(sells / equity["value"].mean() / years)
