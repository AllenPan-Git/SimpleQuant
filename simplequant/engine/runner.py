"""
回测执行器
输入：若干行情 DataFrame + 策略类与参数 + 交易成本设置
输出：BacktestResult
"""

from dataclasses import dataclass

import backtrader as bt
import pandas as pd

from ..data.base import EXTRA_COLUMNS
from ..data.cash_dividend import has_columns, events_of
from .base_strategy import ORDER_COLUMNS, TRADE_COLUMNS
from .commission import AShareCommission
from .market import t0_names
from .results import EquityRecorder, BacktestResult, compute_metrics, build_benchmark


@dataclass
class BrokerConfig:
    cash: float = 100_000.0
    commission: float = 0.0002
    min_commission: float = 5.0
    stamp_duty: float = 0.0
    slippage: float = 0.0005
    t_plus_1: bool = True          # 按品种执行 T+1（债券 ETF、可转债等 T+0 品种除外）；False = 全部按 T+0
    risk_free: float = 0.02
    dividend: str = "reinvest"     # reinvest 分红再投资（后复权）/ cash 现金分红（行情由 data/cash_dividend.prepare 换好）


class FactorPandasData(bt.feeds.PandasData):
    """在 OHLCV 之外带上可选的因子列；数据里没有的列为 NaN"""
    lines = tuple(EXTRA_COLUMNS)
    params = tuple((c, -1) for c in EXTRA_COLUMNS)


def _feed(df: pd.DataFrame, name: str) -> bt.feeds.PandasData:
    """根据 K 线间隔自动设定 timeframe"""
    step = df.index.to_series().diff().median() if len(df) > 1 else pd.Timedelta(days=1)
    if step >= pd.Timedelta(hours=20):
        tf, comp = bt.TimeFrame.Days, 1
    elif step >= pd.Timedelta(minutes=1):
        tf, comp = bt.TimeFrame.Minutes, max(int(step / pd.Timedelta(minutes=1)), 1)
    else:
        tf, comp = bt.TimeFrame.Seconds, max(int(step / pd.Timedelta(seconds=1)), 1)
    extras = {c: c for c in EXTRA_COLUMNS if c in df.columns}
    return FactorPandasData(dataname=df, name=name, timeframe=tf, compression=comp,
                            datetime=None, openinterest=-1, **extras)


def build_cerebro(prices: dict[str, pd.DataFrame], strategy_cls, strategy_params: dict | None = None,
                  broker: BrokerConfig | None = None, trade_start=None) -> bt.Cerebro:
    """回测与原生绘图共用：资金、A 股成本、滑点、数据、策略参数都在这里设置"""
    if not prices:
        raise ValueError("Select data for at least one asset / 请至少选择一个标的的数据")
    broker = broker or BrokerConfig()
    params = dict(strategy_params or {})
    params["t_plus_1"] = broker.t_plus_1
    t0 = t0_names(prices) if broker.t_plus_1 else ()
    if t0:
        params["t0_names"] = t0
    if trade_start is not None:
        params["trade_start"] = pd.Timestamp(trade_start).date()

    dividends = {name: events_of(df) for name, df in prices.items() if has_columns(df)}
    if dividends:
        # 用开盘前的回调处理除权除息；broker_coo=False：委托的撮合方式与平时完全一样
        params["dividends"] = dividends
        cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True, broker_coo=False)
    else:
        cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(broker.cash)
    cerebro.broker.addcommissioninfo(AShareCommission(
        commission=broker.commission, min_commission=broker.min_commission, stamp_duty=broker.stamp_duty))
    if broker.slippage > 0:
        cerebro.broker.set_slippage_perc(perc=broker.slippage)
    for name, df in prices.items():
        cerebro.adddata(_feed(df, name), name=name)
    cerebro.addstrategy(strategy_cls, **params)
    return cerebro


def run_backtest(prices: dict[str, pd.DataFrame], strategy_cls, strategy_params: dict | None = None,
                 broker: BrokerConfig | None = None, trade_start=None, with_panels: bool = False) -> BacktestResult:
    """
    trade_start：这天之前只计算指标、不下单；资产曲线和指标从这天起算（模拟盘用它从开户日开始交易）
    with_panels：同时取出策略用到的各指标数值（交互式策略图用；参数优化时不需要）
    """
    broker = broker or BrokerConfig()
    cerebro = build_cerebro(prices, strategy_cls, strategy_params, broker, trade_start)
    cerebro.addanalyzer(EquityRecorder, _name="equity")

    strat = cerebro.run()[0]

    equity = strat.analyzers.equity.get_analysis()
    if trade_start is not None:
        equity = equity.loc[pd.Timestamp(trade_start):].copy()
    if equity.empty:
        raise ValueError("The backtest produced no data; check the date range / 回测没有产生任何数据，请检查数据区间")
    equity["benchmark"] = build_benchmark({k: v.loc[equity.index[0]:] for k, v in prices.items()},
                                          equity.index, broker.cash)
    equity["drawdown"] = equity["value"] / equity["value"].cummax() - 1

    orders = pd.DataFrame(strat.order_records, columns=ORDER_COLUMNS)
    trades = pd.DataFrame(strat.trade_records, columns=TRADE_COLUMNS)
    metrics = compute_metrics(equity["value"], equity["benchmark"], trades, broker.cash, broker.risk_free)
    if strat.p.dividends:
        metrics["dividend_cash"], metrics["dividend_tax"] = strat.div_cash, strat.div_tax
    panels = {}
    if with_panels:
        from .panels import collect_panels
        panels = collect_panels(strat)
    return BacktestResult(equity=equity, orders=orders, trades=trades, metrics=metrics,
                          logs=strat.logs, prices=prices, lot_too_big=sorted(strat.lot_too_big),
                          t0=list(getattr(strat.p, "t0_names", ())),
                          pending=_pending_orders(strat), positions=_positions(strat), panels=panels)


def _pending_orders(strat) -> list[dict]:
    """最后一根 K 线上已发出、尚未成交的委托 = 下一个开盘要执行的交易（模拟盘的"今日信号"）"""
    out = []
    for d, order in strat._pending.items():
        reason = next((kw.get("reason") for _, key, kw in reversed(strat.logs)
                       if key in ("log.buy", "log.sell") and kw.get("name") == d._name), None)
        out.append({"symbol": d._name, "side": "buy" if order.isbuy() else "sell", "size": float(abs(order.created.size)),
                    "ref_price": float(d.close[0]), "reason": reason})
    return out


def _positions(strat) -> list[dict]:
    return [{"symbol": d._name, "size": float(strat.getposition(d).size), "price": float(d.close[0]),
             "cost": float(strat.getposition(d).price)}
            for d in strat.datas if strat.getposition(d).size]
