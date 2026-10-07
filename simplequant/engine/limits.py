"""
择时回测的涨跌停与停牌（只对日线生效），规则与多因子选股相同（见 stocks/panel.py）：

- 开盘即跌停：卖单不成交，委托保留，此后每个交易日开盘重试
- 开盘即涨停：买单作废（与选股的「本期跳过」相同）；买入条件仍成立时，策略会在下一根 K 线重新下单
- 停牌（全天一个价格，成交量为 0；或成交量缺失且价格与前收盘相同）：买卖都不成交，处理方式同上

涨跌停幅度按代码判断：沪深主板 10%（名称带 ST 的 5%）、创业板（2020-08-24 起）和科创板 20%、北交所 30%、
可转债 2022-08-01 起 20%；ETF 10%（名称带「科创」「创业」的 20%）。
行情一般是复权价，开盘涨跌幅按「开盘价 / 前一根 K 线收盘价」计算，复权不改变这个比例。
另外，全天只有一个价格且涨跌超过 4.5% 的一字板（如未识别出的 ST 股票、无代码的 CSV 数据）同样视为封板。
不复权数据在除权日会出现超过涨跌停幅度的跳空，这种情况不视为封板。

这个文件只用标准库，导出的独立 Python 脚本会原样嵌入（见 export/python_script.py）。
"""

ONE_PRICE_MOVE = 0.045      # 一字板：全天一个价格，且涨跌至少这么多
LIMIT_TOLERANCE = 0.003     # 涨跌停价按分取整，开盘涨跌幅与理论幅度的允许误差


def _code6(name):
    """名称里的 6 位代码（如 "510300 沪深300ETF"）；没有则返回空串"""
    for token in str(name or "").replace("_", " ").replace(".", " ").split():
        if len(token) == 6 and token.isdigit():
            return token
    return ""


def limit_pct(name, day):
    """该标的在 day（"YYYY-MM-DD"）的涨跌停幅度；不确定时返回 None（只按一字板判断）"""
    code = _code6(name)
    if not code:
        return None
    name = str(name)
    if code.startswith(("11", "12")):                          # 可转债
        return 0.20 if day >= "2022-08-01" else None
    if code.startswith(("01", "10")):                          # 国债、地方债：不设涨跌停
        return None
    if code.startswith(("5", "15", "16")):                     # ETF、LOF
        return 0.20 if code.startswith("588") or "科创" in name or "创业" in name else 0.10
    if code.startswith(("4", "8", "92")):                      # 北交所
        return 0.30
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("300", "301")):
        return 0.20 if day >= "2020-08-24" else 0.10
    if code.startswith(("60", "00")):
        return 0.05 if "ST" in name.upper() else 0.10
    return None


def blocked(name, day, prev_close, open_, high, low, volume, is_buy):
    """
    这根日线开盘时能否成交：返回 None（可以成交）、"suspended"、"limit_up" 或 "limit_down"
    prev_close：前一根 K 线的收盘价（第一根 K 线传 None）
    """
    one_price = high - low <= 1e-9 * max(abs(high), 1.0)
    no_volume = volume is None or volume != volume or volume <= 0          # 停牌日的成交量为 0 或缺失
    if one_price and volume is not None and volume == volume and volume <= 0:
        return "suspended"
    if not prev_close or prev_close <= 0 or open_ <= 0:
        return None
    if one_price and no_volume and abs(open_ / prev_close - 1) < 1e-9:      # 一字、价格不变、无成交
        return "suspended"
    move = open_ / prev_close - 1
    pct = limit_pct(name, day)
    if is_buy:
        hit = pct is not None and pct - LIMIT_TOLERANCE <= move <= pct + 0.01
        hit = hit or (one_price and ONE_PRICE_MOVE <= move <= 0.31)
        return "limit_up" if hit else None
    hit = pct is not None and -pct - 0.01 <= move <= -pct + LIMIT_TOLERANCE
    hit = hit or (one_price and -0.31 <= move <= -ONE_PRICE_MOVE)
    return "limit_down" if hit else None


class LimitFiller(object):
    """
    Backtrader 的成交量回调（broker.set_filler）：返回 0 表示这根 K 线不成交、委托留到下一根再撮合。
    买单被涨停或停牌挡住时直接作废（cancel + 通知策略）。被挡住的情况记在 events 里：(时间, 名称, 原因, 是否买单)
    """

    def __init__(self):
        self.broker = None
        self.events = []

    def __call__(self, order, price, ago):
        data = order.data
        size = abs(order.executed.remsize)
        if data._timeframe < 5 or len(data) < 1:           # 5 = bt.TimeFrame.Days；分钟线不判断
            return size
        prev = data.close[-1] if len(data) > 1 else None
        vol = data.volume[0]
        why = blocked(data._name, data.datetime.date(0).isoformat(), prev,
                      data.open[0], data.high[0], data.low[0], vol, order.isbuy())
        if why is None:
            return size
        self.events.append((data.datetime.datetime(0), data._name, why, order.isbuy()))
        if order.isbuy() and self.broker is not None:
            order.addinfo(blocked=why)
            order.cancel()
            self.broker.notify(order)
        return 0
