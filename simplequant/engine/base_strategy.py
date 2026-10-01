"""
策略基类
在 Backtrader 订单通知的基础上增加：
- 按目标仓位下单，并按 100 股整手取整
- T+1：当天买入的持仓当天不能卖出
- 订单、成交、平仓交易和日志都记录下来，供结果页展示
- 现金分红模式（行情带 div_* 列，见 data/cash_dividend.py）：除息日开盘前缩减持仓、现金到账，卖出时扣红利税

记录用语言无关的键；日志存成 (时间, 文本键, 参数)，由界面按当前语言翻译（见 simplequant/i18n.py）。
下单理由 reason 同样是 (文本键, 参数) 元组。
"""

import backtrader as bt

ORDER_COLUMNS = ["time", "symbol", "side", "size", "price", "value", "commission"]
TRADE_COLUMNS = ["symbol", "open_time", "close_time", "bars", "pnl", "pnl_net"]


class BaseStrategy(bt.Strategy):
    params = (
        ("t_plus_1", True),
        ("lot_size", 100),
        ("cash_buffer", 0.01),   # 买入时预留 1% 资金，覆盖滑点、手续费和开盘跳空
        ("trade_start", None),   # 这天之前只计算指标、不下单（模拟盘/回测的预热期）
        ("dividends", None),     # 现金分红模式：{标的名: {日期: (持仓保留比例, 每股现金, 每股应税所得)}}
    )

    def __init__(self):
        self.order_records = []    # 每笔成交
        self.trade_records = []    # 每笔完整交易（开仓→平仓）
        self.logs = []             # (时间, 文本键, 参数)
        self._buy_date = {}        # data -> 最近一次买入成交日期
        self._pending = {}         # data -> 未完成订单
        self._lot_warned = set()
        self.lot_too_big = set()   # 出现过"资金不足一手"的标的名
        self._div = self.p.dividends or {}
        self._div_done = set()     # (标的, 日期)：分钟线一天有多根 K 线，只在第一根处理
        self._hold_since = {}      # data -> 本轮持仓的建仓日期（红利税的持股期限）
        self._div_taxable = {}     # data -> 持有期间累计的应税分红所得
        self.div_cash = self.div_tax = 0.0

    # ---------- 工具 ----------
    def log(self, key: str, data=None, **kw):
        d = data or self.datas[0]
        self.logs.append((d.datetime.datetime(0), key, kw))

    def can_sell(self, data) -> bool:
        if not self.p.t_plus_1:
            return True
        return self._buy_date.get(data) != data.datetime.date(0)

    def has_pending(self, data) -> bool:
        return data in self._pending

    def order_target_pct(self, data, pct: float, reason: tuple = ("", {})):
        """把 data 的持仓调整到账户总资产的 pct（0~1），数量按整手取整"""
        if self.has_pending(data):
            return None
        if self.p.trade_start is not None and data.datetime.date(0) < self.p.trade_start:
            return None
        price = data.close[0]
        if price <= 0:
            return None
        lot = self.p.lot_size
        target = int(self.broker.getvalue() * pct / price / lot) * lot
        current = self.getposition(data).size
        delta = target - current

        if pct > 0 and current == 0 and target < lot:
            # 分到的资金连一手都不够（常见于高价股，或后复权抬高了价格）：不下单，只提示一次
            if data not in self._lot_warned:
                self._lot_warned.add(data)
                self.lot_too_big.add(data._name)
                self.log("log.lot_too_big", data, name=data._name, cost=price * lot)
            return None

        if delta > 0:
            affordable = int(self.broker.getcash() / (price * (1 + self.p.cash_buffer)) / lot) * lot
            delta = min(delta, affordable)
            if delta >= lot:
                self.log("log.buy", data, name=data._name, size=delta, reason=reason)
                return self._track(data, self.buy(data=data, size=delta))
        elif delta < 0 and current > 0:
            if not self.can_sell(data):
                self.log("log.t1", data, name=data._name)
                return None
            self.log("log.sell", data, name=data._name, reason=reason)
            if target <= 0:
                return self._track(data, self.close(data=data))
            return self._track(data, self.sell(data=data, size=-delta))
        return None

    def _track(self, data, order):
        if order is not None:
            self._pending[data] = order
        return order

    # ---------- 现金分红 ----------
    def prenext_open(self):
        self._pay_dividends()

    def next_open(self):
        self._pay_dividends()

    def _pay_dividends(self):
        """
        除息日开盘前（还没撮合今天的委托）：持仓按 keep 缩减，缩掉的部分作为税前现金到账。
        平均成本相应调低，这样平仓盈亏包含持有期间收到的分红
        """
        for d in self.datas:
            events = self._div.get(d._name)
            if not events:
                continue
            day = d.datetime.date(0)
            if day not in events or (d, day) in self._div_done:
                continue
            self._div_done.add((d, day))
            keep, cash, taxable = events[day]
            pos = self.broker.getposition(d)
            size = pos.size
            if size <= 0:
                continue
            amount = size * cash
            new_size = size * keep
            self.broker.add_cash(amount)
            self.div_cash += amount
            if taxable:
                self._div_taxable[d] = self._div_taxable.get(d, 0.0) + size * taxable
            pos.size = new_size
            pos.price = (pos.price * size - amount) / new_size
            for trade in self._trades[d][0]:          # 未平仓的交易同步调整，否则卖光后它不会被当作已平仓
                if trade.isopen:
                    trade.price = (trade.price * trade.size - amount) / new_size
                    trade.size = new_size
            if amount > 0:
                self.log("log.dividend", d, name=d._name, cash=f"{amount:,.2f}")

    def _dividend_tax(self, order):
        """卖出成交后按卖出比例、持股期限补扣红利税（规则见 stocks/dividends.py 的 tax_rate）"""
        d = order.data
        left = self.getposition(d).size
        taxable = self._div_taxable.get(d, 0.0)
        since = self._hold_since.get(d)
        if left <= 0:
            self._hold_since.pop(d, None)
            self._div_taxable.pop(d, None)
        if not taxable or since is None:
            return
        sold = abs(order.executed.size)
        part = taxable * sold / (sold + max(left, 0.0))
        if left > 0:
            self._div_taxable[d] = taxable - part
        from ..stocks.dividends import tax_rate
        tax = part * tax_rate(since, d.datetime.date(0))
        if tax > 0:
            self.broker.add_cash(-tax)
            self.div_tax += tax
            self.log("log.dividend_tax", d, name=d._name, tax=f"{tax:,.2f}",
                     days=(d.datetime.date(0) - since).days)

    # ---------- 回调 ----------
    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        self._pending.pop(order.data, None)
        dt = order.data.datetime.datetime(0)
        if order.status == order.Completed:
            if order.isbuy():
                self._buy_date[order.data] = order.data.datetime.date(0)
                self._hold_since.setdefault(order.data, order.data.datetime.date(0))
            elif self._div:
                self._dividend_tax(order)
            size = abs(order.executed.size)
            self.order_records.append(dict(zip(ORDER_COLUMNS, (
                dt, order.data._name, "buy" if order.isbuy() else "sell", size,
                order.executed.price, size * order.executed.price, order.executed.comm))))
        else:
            self.logs.append((dt, "log.order_failed", {"name": order.data._name,
                                                        "status": ("status." + order.getstatusname(), {})}))

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.trade_records.append(dict(zip(TRADE_COLUMNS, (
            trade.data._name, bt.num2date(trade.dtopen), bt.num2date(trade.dtclose),
            trade.barlen, trade.pnl, trade.pnlcomm))))
