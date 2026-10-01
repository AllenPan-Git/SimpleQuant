"""
Backtrader 原生图表（matplotlib）：K 线 + 成交量 + 买卖点 + 策略用到的全部指标 + 资金曲线 + 每笔交易盈亏

与回测使用同一套设置（build_cerebro），只额外加上绘图用的观察器。
注意：cerebro.plot() 会弹出窗口并阻塞，这里改用底层 Plot().plot(strategy) 只生成图，输出 PNG。
"""

import io

import matplotlib
import backtrader as bt
from backtrader import plot as btplot   # 注意：这个模块被导入时会把后端强制设成 TkAgg（会弹窗）……
import matplotlib.pyplot as plt

matplotlib.use("Agg", force=True)       # ……所以必须在它之后切回 Agg：网页里使用，不弹窗、线程安全
plt.switch_backend("agg")

import pandas as pd                     # noqa: E402

from ..i18n import L, pick              # noqa: E402
from .runner import build_cerebro, BrokerConfig   # noqa: E402

UP, DOWN = "#e34948", "#1baf7a"          # A 股习惯：红涨绿跌、红买绿卖（与软件其它图表一致）
MAX_BARS = 1500                          # 超过这个数量图会挤成一团，请缩小区间


def _observers(lang: str):
    # _plotlabel 返回空：图例标题只显示名称，不附带 "(None)"、"(True, 0.02)" 这类参数值
    class Broker(bt.observers.Broker):
        plotinfo = dict(plot=True, subplot=True, plotname=pick(L("资金", "Broker"), lang))
        plotlines = dict(cash=dict(color="#8a8984", _name=pick(L("现金", "cash"), lang)),
                         value=dict(color="#2a78d6", _name=pick(L("总资产", "value"), lang)))

        def _plotlabel(self):
            return []

    class Trades(bt.observers.Trades):
        plotinfo = dict(plot=True, subplot=True, plotname=pick(L("每笔交易盈亏", "Trade P&L"), lang),
                        plothlines=[0.0], plotymargin=0.10)
        plotlines = dict(pnlplus=dict(color=UP, _name=pick(L("盈利", "profit"), lang)),
                         pnlminus=dict(color=DOWN, _name=pick(L("亏损", "loss"), lang)))

        def _plotlabel(self):
            return []

    class BuySell(bt.observers.BuySell):
        params = (("barplot", True), ("bardist", 0.02))
        plotinfo = dict(plot=True, subplot=False, plotlinelabels=True, plotname=pick(L("买卖点", "Trades"), lang))

        def _plotlabel(self):
            return []
        plotlines = dict(buy=dict(marker="^", markersize=9.0, color=UP, fillstyle="full",
                                  _name=pick(L("买入", "buy"), lang)),
                         sell=dict(marker="v", markersize=9.0, color=DOWN, fillstyle="full",
                                   _name=pick(L("卖出", "sell"), lang)))
    return Broker, Trades, BuySell


def render(prices: dict[str, pd.DataFrame], strategy_cls, params: dict | None = None,
           broker: BrokerConfig | None = None, start=None, end=None, lang: str = "zh",
           size=(16, 10), dpi: int = 110) -> bytes:
    """
    跑一次回测并把 [start, end] 区间画成 PNG（回测仍从数据开头运行，保证指标和持仓状态正确）
    """
    first = max(df.index[0] for df in prices.values())
    last = min(df.index[-1] for df in prices.values())
    start = pd.Timestamp(start) if start is not None else first
    end = pd.Timestamp(end) if end is not None else last
    n = max(len(df.loc[start:end]) for df in prices.values())
    if n > MAX_BARS:
        raise ValueError(f"{n} bars is too many to read; pick a range under {MAX_BARS} / "
                         f"区间内有 {n} 根 K 线，图会挤成一团，请缩小到 {MAX_BARS} 根以内")

    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
                                              "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    cerebro = build_cerebro(prices, strategy_cls, params, broker)
    for obs in _observers(lang):
        cerebro.addobserver(obs)
    strat = cerebro.run()[0]

    from matplotlib.colors import to_rgb
    if matplotlib.get_backend().lower() != "agg":   # 防止被其它代码改回会弹窗的后端
        plt.switch_backend("agg")
    # Backtrader 在新版 matplotlib 下给十六进制颜色做明暗变换会出错（voltrans 同理），K 线/成交量改用 RGB 数值
    up, down = to_rgb(UP), to_rgb(DOWN)
    idx = next(iter(prices.values())).index
    daily = len(idx) < 2 or (idx[1:] - idx[:-1]).median() >= pd.Timedelta(hours=20)
    plotter = btplot.Plot(style="candle", barup=up, bardown=down, volup=up, voldown=down, grid=True,
                          fmt_x_ticks="%Y-%m-%d" if daily else "%m-%d %H:%M",
                          fmt_x_data="%Y-%m-%d" if daily else "%Y-%m-%d %H:%M")
    figs = plotter.plot(strat, figid=0, numfigs=1, iplot=False, start=start.date(), end=end.date())
    fig = figs[0]
    fig.set_size_inches(*size)
    # 新版 matplotlib 下 Backtrader 的日期自动排版会把所有子图（含最下面一个）的日期标签都隐藏：
    # 让最下面的子图重新显示日期
    bottom = fig.axes[-1]
    bottom.tick_params(axis="x", which="both", labelbottom=True, labelrotation=0)
    for label in bottom.get_xticklabels():
        label.set_visible(True)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close("all")                    # 释放内存，网页里会反复生成
    return buf.getvalue()
