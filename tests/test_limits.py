"""择时回测的涨跌停与停牌（engine/limits.py）"""

import pandas as pd

from simplequant.engine import BaseStrategy, BrokerConfig, run_backtest
from simplequant.engine.credibility import check_backtest
from simplequant.engine.limits import blocked, limit_pct


def test_limit_pct_by_board():
    assert limit_pct("600000 浦发银行", "2023-01-03") == 0.10
    assert limit_pct("600000 ST浦发", "2023-01-03") == 0.05
    assert limit_pct("300750 宁德时代", "2020-08-21") == 0.10
    assert limit_pct("300750 宁德时代", "2020-08-24") == 0.20
    assert limit_pct("688981 中芯国际", "2023-01-03") == 0.20
    assert limit_pct("830799 艾融软件", "2023-01-03") == 0.30
    assert limit_pct("113052 兴业转债", "2022-07-29") is None
    assert limit_pct("113052 兴业转债", "2022-08-01") == 0.20
    assert limit_pct("510300 沪深300ETF", "2023-01-03") == 0.10
    assert limit_pct("588000 科创50ETF", "2023-01-03") == 0.20
    assert limit_pct("my_data", "2023-01-03") is None


def test_blocked_rules():
    day = "2023-01-03"
    # 开盘即跌停：卖不出；不在跌停价开盘的大跌可以卖
    assert blocked("600000 浦发", day, 10.0, 9.0, 9.5, 9.0, 1000, False) == "limit_down"
    assert blocked("600000 浦发", day, 10.0, 9.2, 9.5, 9.0, 1000, False) is None
    assert blocked("600000 浦发", day, 10.0, 9.0, 9.5, 9.0, 1000, True) is None         # 跌停时可以买
    assert blocked("600000 浦发", day, 10.0, 11.0, 11.0, 11.0, 1000, True) == "limit_up"
    # 名称里没有 ST 的 ST 股票、无代码数据：一字板也算封板
    assert blocked("002751 易尚", day, 10.0, 9.5, 9.5, 9.5, 10, False) == "limit_down"
    assert blocked("my_data", day, 10.0, 9.5, 9.5, 9.5, 10, False) == "limit_down"
    assert blocked("my_data", day, 10.0, 9.8, 9.8, 9.8, 10, False) is None
    # 停牌：成交量为 0 的一字 K 线
    assert blocked("600000 浦发", day, 10.0, 10.0, 10.0, 10.0, 0, True) == "suspended"
    assert blocked("600000 浦发", day, 10.0, 10.0, 10.0, 10.0, float("nan"), False) == "suspended"   # BaoStock 停牌日
    assert blocked("600000 浦发", day, 10.0, 10.0, 10.0, 10.0, 500, False) is None       # 有成交的一字平盘
    # 不复权数据除权日的跳空超过涨跌停幅度：不算封板
    assert blocked("600000 浦发", day, 10.0, 5.0, 5.2, 5.0, 1000, False) is None


class BuyThenSell(BaseStrategy):
    params = (("buy_at", 1), ("sell_at", 3))

    def next(self):
        n = len(self)
        if n == self.p.buy_at:
            self.order_target_pct(self.data, 0.9)
        elif n >= self.p.sell_at and self.getposition(self.data).size:
            self.order_target_pct(self.data, 0.0)


def _prices(rows):
    idx = pd.bdate_range("2023-01-02", periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def test_sell_waits_through_limit_down():
    # 第 3 根收盘发出卖出信号；第 4、5 根一字跌停，第 6 根才卖出
    df = _prices([(10, 10, 10, 10, 1e4), (10, 10.2, 9.9, 10, 1e4), (10, 10.1, 9.9, 10, 1e4),
                  (9, 9, 9, 9, 100), (8.1, 8.1, 8.1, 8.1, 100), (7.8, 8.0, 7.5, 7.9, 1e5), (7.9, 8, 7.8, 7.9, 1e4)])
    broker = BrokerConfig(commission=0, min_commission=0, slippage=0)
    res = run_backtest({"600000 浦发银行": df}, BuyThenSell, broker=broker)
    sells = res.orders[res.orders["side"] == "sell"]
    assert len(sells) == 1 and sells["time"].iloc[0] == df.index[5] and sells["price"].iloc[0] == 7.8
    assert res.metrics["limit_blocked"] == 2
    assert sum(1 for _, k, _ in res.logs if k == "log.blocked_sell") == 2
    assert any(c["key"] == "cred.limit_blocked" for c in check_backtest(res.metrics, res.trades))

    off = run_backtest({"600000 浦发银行": df}, BuyThenSell, broker=BrokerConfig(
        commission=0, min_commission=0, slippage=0, price_limit=False))
    assert off.orders[off.orders["side"] == "sell"]["price"].iloc[0] == 9          # 关闭时按跌停价卖出
    assert off.metrics["limit_blocked"] == 0


def test_buy_canceled_at_limit_up():
    df = _prices([(10, 10, 10, 10, 1e4), (11, 11, 11, 11, 100), (11.5, 12, 11.2, 11.8, 1e4), (12, 12, 12, 12, 1e4)])
    res = run_backtest({"600000 浦发银行": df}, BuyThenSell, {"sell_at": 99},
                       BrokerConfig(commission=0, min_commission=0, slippage=0))
    assert res.orders.empty                                   # 买单作废，不会在之后的开盘追高成交
    keys = [k for _, k, _ in res.logs]
    assert "log.blocked_buy" in keys and "log.order_failed" not in keys
