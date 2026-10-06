"""
回测与参数优化图表（plotly）
- 权益曲线与回撤分成上下两张图，各自一个纵轴，共用时间轴
- 沿用 A 股习惯：红涨绿跌；买卖点用 ▲/▼ 形状 + 悬浮文字区分，不只靠颜色
- 参数热力图：以 0 为中点的发散色（红 = 正，绿 = 负，中点为中性灰），每格标注数值，最优格加框
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from simplequant.i18n import tr
from ui import texts  # noqa: F401  图表标题、图例用到界面文字表

STRATEGY = "#1D1B18"     # 策略：墨色（新界面深色模式下由 CSS 换成米色，见 gui/theme.py）
BENCHMARK = "#A39B8F"    # 基准：暖灰，作为参考线
DRAWDOWN = "#1E7A4C"     # 回撤：A 股习惯，跌为绿
UP, DOWN = "#B42318", "#1E7A4C"
BUY, SELL = "#B42318", "#1E7A4C"
NEUTRAL = "#e8e7e3"

MAX_CANDLES = 3000       # 超过这个数量改画收盘价折线，避免浏览器卡顿
PCT_METRICS = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "excess_return"}
# 发散色的中点：高于中点偏红（好），低于中点偏绿（差）
MIDPOINTS = {"sharpe": 0, "total_return": 0, "cagr": 0, "calmar": 0, "excess_return": 0,
             "win_rate": 0.5, "profit_factor": 1}


def _layout(fig, height):
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridwidth=0.5)
    return fig


def _intraday(index: pd.DatetimeIndex) -> bool:
    return len(index) > 1 and (index[1:] - index[:-1]).median() < pd.Timedelta(hours=20)


def _x(times, intraday: bool):
    """分钟线用"类别轴"（去掉午休、夜间、节假日空白），横坐标转成易读的文字"""
    times = pd.DatetimeIndex(times)
    return times.strftime("%Y-%m-%d %H:%M") if intraday else times


def _gapless(fig, index: pd.DatetimeIndex, intraday: bool):
    if intraday:
        fig.update_xaxes(type="category", nticks=8, tickangle=0)
    else:
        # 日线：隐藏周末和节假日（区间内没有数据的日期）
        missing = pd.date_range(index[0], index[-1], freq="D").difference(index.normalize())
        fig.update_xaxes(rangebreaks=[dict(values=missing.strftime("%Y-%m-%d").tolist())])


def equity_chart(equity: pd.DataFrame, lang: str = "zh", benchmark_label: str | None = None,
                 strategy_label: str | None = None) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.06,
                        subplot_titles=(tr("chart.equity", lang), tr("chart.drawdown", lang)))
    intraday = _intraday(equity.index)
    x = _x(equity.index, intraday)
    fig.add_trace(go.Scatter(x=x, y=equity["value"], name=strategy_label or tr("chart.strategy", lang),
                             line=dict(color=STRATEGY, width=2), hovertemplate="%{y:,.0f}"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=equity["benchmark"], name=benchmark_label or tr("chart.benchmark", lang),
                             line=dict(color=BENCHMARK, width=1.5, dash="dot"), hovertemplate="%{y:,.0f}"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=equity["drawdown"], name=tr("chart.drawdown", lang), fill="tozeroy",
                             line=dict(color=DRAWDOWN, width=1), fillcolor="rgba(30,122,76,0.14)",
                             hovertemplate="%{y:.2%}", showlegend=False), row=2, col=1)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_yaxes(tickformat=",.0f", row=1, col=1)
    _gapless(fig, equity.index, intraday)
    return _layout(fig, 520)


def price_chart(df: pd.DataFrame, orders: pd.DataFrame, name: str, lang: str = "zh") -> go.Figure:
    fig = go.Figure()
    intraday = _intraday(df.index)
    x = _x(df.index, intraday)
    if len(df) <= MAX_CANDLES:
        fig.add_trace(go.Candlestick(x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                     name=name, increasing_line_color=UP, decreasing_line_color=DOWN,
                                     increasing_fillcolor=UP, decreasing_fillcolor=DOWN))
    else:
        fig.add_trace(go.Scatter(x=x, y=df["close"], name=f"{name} {tr('chart.close', lang)}",
                                 line=dict(color=BENCHMARK, width=1.5)))

    if not orders.empty:
        mine = orders[orders["symbol"] == name]
        for side, symbol, color in (("buy", "triangle-up", BUY), ("sell", "triangle-down", SELL)):
            o = mine[mine["side"] == side]
            if o.empty:
                continue
            label = tr(f"side.{side}", lang)
            fig.add_trace(go.Scatter(
                x=_x(o["time"], intraday), y=o["price"], mode="markers", name=label,
                marker=dict(symbol=symbol, size=11, color=color, line=dict(width=1.5, color="white")),
                customdata=o[["size"]].values,
                hovertemplate=label + " %{y:.3f} × %{customdata[0]:,.0f}<extra></extra>"))

    fig.update_layout(xaxis_rangeslider_visible=False)
    _gapless(fig, df.index, intraday)
    return _layout(fig, 460)


# 指标线的颜色：固定顺序；不用红/绿，避免与 K 线涨跌色混淆
LINE_COLORS = ["#2a78d6", "#eb6834", "#eda100", "#e87ba4", "#4a3aa7", "#199e70", "#8a8984"]


def _panel_title(p: dict, lang: str) -> str:
    """规则策略按条件积木里的名称（如"MACD(12,26,9)"）；模板策略用 Backtrader 的名称（如"SMA (20)"）"""
    if p.get("name"):
        return p["name"]
    from simplequant.i18n import pick
    from simplequant.rules import INDICATORS
    spec = p["spec"]
    meta = INDICATORS[spec["ind"]]
    params = spec.get("params") or {}
    args = ",".join(f"{params.get(n, d):g}" for n, _, d in meta["params"])
    return pick(meta["label"], lang) + (f"({args})" if args else "")


def _line_label(p: dict, key: str, lang: str) -> str:
    if p.get("name"):
        return key if len(p["lines"]) > 1 else p["name"]
    from simplequant.i18n import pick
    from simplequant.rules import INDICATORS
    lines = INDICATORS[p["spec"]["ind"]].get("lines")
    title = _panel_title(p, lang)
    return f"{title}·{pick(lines[key], lang)}" if lines and key in lines else title


def strategy_chart(df: pd.DataFrame, orders: pd.DataFrame, trades: pd.DataFrame, panels: list[dict], name: str,
                   lang: str = "zh") -> go.Figure:
    """
    交互式策略图（内容对应 Backtrader 原生图）：
    每笔交易盈亏 / K 线 + 叠加指标 + 买卖点 / 成交量 / 各指标栏；所有栏共用时间轴，悬停同时显示同一天的数值
    """
    overlays = [p for p in panels if p["overlay"]]
    subs = [p for p in panels if not p["overlay"]]
    titles = [tr("chart.trade_pnl", lang), name, tr("chart.volume", lang)] + [_panel_title(p, lang) for p in subs]
    heights = [0.10, 0.44, 0.10] + [0.16] * len(subs)
    fig = make_subplots(rows=len(titles), cols=1, shared_xaxes=True, vertical_spacing=0.035,
                        row_heights=[h / sum(heights) for h in heights], subplot_titles=titles)
    intraday = _intraday(df.index)
    x = _x(df.index, intraday)

    # 1. 每笔交易盈亏（按平仓时间）
    mine = trades[trades["symbol"] == name] if len(trades) else trades
    if len(mine):
        pnl = mine["pnl_net"].astype(float)
        fig.add_trace(go.Scatter(
            x=_x(pd.to_datetime(mine["close_time"]), intraday), y=pnl, mode="markers", name=tr("chart.trade_pnl", lang),
            marker=dict(size=10, color=[UP if v > 0 else DOWN for v in pnl], line=dict(width=1, color="white")),
            hovertemplate="%{y:,.0f}<extra></extra>", showlegend=False), row=1, col=1)
    fig.add_hline(y=0, line_width=1, line_color=BENCHMARK, row=1, col=1)

    # 2. K 线 + 叠加指标 + 买卖点
    if len(df) <= MAX_CANDLES:
        fig.add_trace(go.Candlestick(x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name=name,
                                     increasing_line_color=UP, decreasing_line_color=DOWN,
                                     increasing_fillcolor=UP, decreasing_fillcolor=DOWN, showlegend=False), row=2, col=1)
    else:
        fig.add_trace(go.Scatter(x=x, y=df["close"], name=name, line=dict(color=BENCHMARK, width=1.5),
                                 showlegend=False), row=2, col=1)
    color_i = 0
    for p in overlays:
        for key, s in p["lines"].items():
            s = s.reindex(df.index)
            fig.add_trace(go.Scatter(x=x, y=s.values, name=_line_label(p, key, lang), mode="lines",
                                     line=dict(color=LINE_COLORS[color_i % len(LINE_COLORS)], width=1.5),
                                     hovertemplate="%{y:.3f}"), row=2, col=1)
            color_i += 1
    if len(orders):
        mo = orders[orders["symbol"] == name]
        for side, symbol, color in (("buy", "triangle-up", BUY), ("sell", "triangle-down", SELL)):
            o = mo[mo["side"] == side]
            if len(o):
                label = tr(f"side.{side}", lang)
                fig.add_trace(go.Scatter(
                    x=_x(o["time"], intraday), y=o["price"], mode="markers", name=label,
                    marker=dict(symbol=symbol, size=11, color=color, line=dict(width=1.5, color="white")),
                    customdata=o[["size"]].values,
                    hovertemplate=label + " %{y:.3f} × %{customdata[0]:,.0f}<extra></extra>"), row=2, col=1)

    # 3. 成交量
    vol_colors = [UP if c >= o else DOWN for o, c in zip(df["open"], df["close"])]
    fig.add_trace(go.Bar(x=x, y=df["volume"], marker_color=vol_colors, marker_line_width=0, opacity=0.7,
                         name=tr("chart.volume", lang), showlegend=False, hovertemplate="%{y:,.0f}"), row=3, col=1)

    # 4. 各指标栏（每栏内线条按固定顺序配色）
    for r, p in enumerate(subs, start=4):
        for i, (key, s) in enumerate(p["lines"].items()):
            s = s.reindex(df.index)
            fig.add_trace(go.Scatter(x=x, y=s.values, name=_line_label(p, key, lang), mode="lines",
                                     line=dict(color=LINE_COLORS[i % len(LINE_COLORS)], width=1.5),
                                     hovertemplate="%{y:.3f}"), row=r, col=1)
        for h in p["hlines"]:
            fig.add_hline(y=h, line_width=1, line_dash="dot", line_color=BENCHMARK, row=r, col=1)

    # 图例放在底部，快捷区间按钮放在右上角，互不遮挡
    fig.update_layout(height=620 + 170 * len(subs), hovermode="x unified", hoversubplots="axis",
                      margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", yanchor="top", y=-0.03, x=0))
    # K 线图默认会给自己的时间轴加缩略图滑块（把整张价格图缩小复制在下方，压住成交量等栏）：全部关掉
    fig.update_xaxes(rangeslider_visible=False, showgrid=False, showspikes=True, spikemode="across",
                     spikethickness=1, spikedash="dot", spikecolor=BENCHMARK)
    fig.update_yaxes(gridwidth=0.5)
    _gapless(fig, df.index, intraday)
    if not intraday:
        fig.update_xaxes(rangeselector=dict(buttons=[
            dict(count=1, label=tr("chart.r1m", lang), step="month", stepmode="backward"),
            dict(count=3, label=tr("chart.r3m", lang), step="month", stepmode="backward"),
            dict(count=6, label=tr("chart.r6m", lang), step="month", stepmode="backward"),
            dict(count=1, label=tr("chart.r1y", lang), step="year", stepmode="backward"),
            dict(step="all", label=tr("chart.rall", lang))], x=1, xanchor="right", y=1.0, yanchor="bottom"),
            row=1, col=1)
    _single_time_axis(fig, len(titles))
    return fig


def _single_time_axis(fig: go.Figure, n_rows: int) -> None:
    """
    make_subplots 给每栏一条各自的时间轴（x、x2…，用 matches 联动）；但 hoversubplots 只把
    挂在【同一条】x 轴上的栏算作一组。所以把所有栏的曲线都挂到 x 上，各栏只保留自己的 y 轴，
    悬停价格图时成交量、MACD 等各栏同一天的数值才会一起显示
    """
    for tr_ in fig.data:
        tr_.xaxis = "x"
    for shape in fig.layout.shapes:
        if shape.xref and shape.xref.startswith("x"):
            shape.xref = "x domain" if shape.xref.endswith("domain") else "x"
    for i in range(1, n_rows + 1):
        fig.layout[f"yaxis{'' if i == 1 else i}"].anchor = "x"
    for i in range(2, n_rows + 1):
        fig.layout[f"xaxis{i}"] = None
    # 唯一的时间轴画在最底一栏下方
    fig.layout.xaxis.update(anchor=f"y{n_rows}", matches=None, showticklabels=True)


def preview_chart(df: pd.DataFrame, name: str, lang: str = "zh") -> go.Figure:
    return price_chart(df, pd.DataFrame(), name, lang)


# ---------------- 因子研究 ----------------
# 分组是有序的（第 1 组因子值最小 → 第 N 组最大），用同一色相由浅到深
GROUP_RAMP = ["#E3D3BD", "#D1B08A", "#B9855A", "#9C5A3A", "#7A3524", "#521A12"]
LONG_SHORT = "#5c5b57"


def _ramp(n: int) -> list[str]:
    idx = np.linspace(0, len(GROUP_RAMP) - 1, n).round().astype(int)
    return [GROUP_RAMP[i] for i in idx]


def ic_chart(ic: pd.Series, lang: str = "zh", window: int = 6) -> go.Figure:
    ic = ic.dropna()
    colors = [UP if v > 0 else DOWN for v in ic]
    fig = go.Figure(go.Bar(x=ic.index, y=ic.values, name="IC", marker_color=colors,
                           hovertemplate="%{x|%Y-%m-%d}  IC=%{y:.3f}<extra></extra>"))
    roll = ic.rolling(window, min_periods=1).mean()
    fig.add_trace(go.Scatter(x=roll.index, y=roll.values, name=tr("chart.ic_roll", lang, n=window),
                             line=dict(color=STRATEGY, width=2), hovertemplate="%{y:.3f}<extra></extra>"))
    fig.add_hline(y=0, line_width=1, line_color=BENCHMARK)
    fig.update_yaxes(title="Rank IC", tickformat=".2f")
    fig.update_layout(bargap=0.15)
    return _layout(fig, 340)


def quantile_chart(cum: pd.DataFrame, lang: str = "zh") -> go.Figure:
    groups = [c for c in cum.columns if c != "long_short"]
    fig = go.Figure()
    for g, color in zip(groups, _ramp(len(groups))):
        label = tr("chart.group", lang, g=g)
        if g == groups[0]:
            label += tr("chart.group_low", lang)
        elif g == groups[-1]:
            label += tr("chart.group_high", lang)
        fig.add_trace(go.Scatter(x=cum.index, y=cum[g], name=label, line=dict(color=color, width=2),
                                 hovertemplate="%{y:.3f}"))
    if "long_short" in cum:
        fig.add_trace(go.Scatter(x=cum.index, y=cum["long_short"], name=tr("chart.long_short", lang),
                                 line=dict(color=LONG_SHORT, width=1.5, dash="dash"), hovertemplate="%{y:.3f}"))
    fig.update_yaxes(title=tr("chart.nav", lang), tickformat=".2f")
    return _layout(fig, 420)


def group_bar(annual: pd.Series, lang: str = "zh") -> go.Figure:
    a = annual.drop("long_short", errors="ignore")
    labels = [tr("chart.group", lang, g=g) for g in a.index]
    fig = go.Figure(go.Bar(x=labels, y=a.values, marker_color=_ramp(len(a)),
                           text=[f"{v:.1%}" for v in a.values], textposition="outside",
                           hovertemplate="%{x}: %{y:.2%}<extra></extra>"))
    fig.add_hline(y=0, line_width=1, line_color=BENCHMARK)
    fig.update_yaxes(tickformat=".0%", title=tr("chart.annual", lang))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
    return fig


def corr_heatmap(corr: pd.DataFrame, labels: dict, lang: str = "zh") -> go.Figure:
    """因子相关性：以 0 为中点的发散色，数值标注在格子里"""
    names = [labels.get(k, k) for k in corr.columns]
    fig = go.Figure(go.Heatmap(
        z=corr.values, x=names, y=names, zmin=-1, zmax=1, zmid=0, xgap=2, ygap=2,
        colorscale=[[0, DOWN], [0.5, NEUTRAL], [1, UP]], texttemplate="%{z:.2f}", textfont=dict(size=12),
        colorbar=dict(title=tr("chart.corr", lang), thickness=12),
        hovertemplate="%{y} × %{x}: %{z:.2f}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=False, tickangle=-30)
    fig.update_layout(height=max(320, 46 * len(names) + 120), margin=dict(l=10, r=10, t=20, b=10))
    return fig


# ---------------- 滚动优化 ----------------
def walkforward_chart(equity: pd.DataFrame, windows: pd.DataFrame, lang: str = "zh") -> go.Figure:
    """样本外拼接曲线 vs 原参数 vs 买入持有；灰色竖线标出每次换参数的时点"""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.06,
                        subplot_titles=(tr("wf.chart_equity", lang), tr("chart.drawdown", lang)))
    fig.add_trace(go.Scatter(x=equity.index, y=equity["value"], name=tr("wf.line_wf", lang),
                             line=dict(color=STRATEGY, width=2), hovertemplate="%{y:,.0f}"), row=1, col=1)
    fig.add_trace(go.Scatter(x=equity.index, y=equity["baseline"], name=tr("wf.line_base", lang),
                             line=dict(color=LONG_SHORT, width=1.5, dash="dash"), hovertemplate="%{y:,.0f}"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=equity.index, y=equity["benchmark"], name=tr("chart.benchmark", lang),
                             line=dict(color=BENCHMARK, width=1.5, dash="dot"), hovertemplate="%{y:,.0f}"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=equity.index, y=equity["drawdown"], fill="tozeroy", showlegend=False,
                             line=dict(color=DRAWDOWN, width=1), fillcolor="rgba(30,122,76,0.14)",
                             hovertemplate="%{y:.2%}"), row=2, col=1)
    for d in windows["test_start"].iloc[1:]:
        fig.add_vline(x=pd.Timestamp(d).to_pydatetime(), line_width=1, line_dash="dot", line_color=BENCHMARK, opacity=0.5)
    fig.update_yaxes(tickformat=",.0f", row=1, col=1)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return _layout(fig, 520)


def param_stability_chart(params: pd.DataFrame, labels: dict, lang: str = "zh") -> go.Figure:
    """每个窗口选出的参数（阶梯线）；每个参数一张小图，各自的纵轴"""
    cols = [c for c in params.columns if c != "test_start"]
    fig = make_subplots(rows=len(cols), cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=[labels.get(c, c) for c in cols])
    for i, c in enumerate(cols, 1):
        numeric = pd.api.types.is_numeric_dtype(params[c])      # 调仓频率等离散参数显示为文字
        fig.add_trace(go.Scatter(x=params["test_start"], y=params[c], mode="lines+markers", line_shape="hv",
                                 line=dict(color=STRATEGY, width=2), marker=dict(size=8), name=labels.get(c, c),
                                 hovertemplate="%{x|%Y-%m-%d}: %{y" + (":g" if numeric else "") + "}<extra></extra>",
                                 showlegend=False),
                      row=i, col=1)
    fig.update_layout(height=150 + 170 * len(cols), margin=dict(l=10, r=10, t=30, b=10))
    fig.update_xaxes(showgrid=False)
    return fig


# ---------------- 参数优化 ----------------
def _fmt(metric: str) -> tuple[str, str]:
    """(plotly 数值格式, 悬浮格式)"""
    return (".1%", ".2%") if metric in PCT_METRICS else (".2f", ".3f")


def _colorscale(metric: str, values: np.ndarray):
    """发散色：以指标的自然中点为中心；最大回撤全为负，用单一绿色由浅到深"""
    finite = values[np.isfinite(values)]
    if metric == "max_drawdown" or not len(finite):
        return [[0, DOWN], [1, NEUTRAL]], {}
    mid = MIDPOINTS.get(metric, float(np.median(finite)))
    span = max(abs(finite.max() - mid), abs(finite.min() - mid)) or 1
    return [[0, DOWN], [0.5, NEUTRAL], [1, UP]], dict(zmid=mid, zmin=mid - span, zmax=mid + span)


def heatmap(pivot: pd.DataFrame, metric: str, x_label: str, y_label: str, lang: str = "zh") -> go.Figure:
    """pivot: 行 = y 参数取值，列 = x 参数取值，值 = 指标"""
    z = pivot.values.astype(float)
    colorscale, zrange = _colorscale(metric, z)
    text_fmt, hover_fmt = _fmt(metric)
    xs = [f"{v:g}" for v in pivot.columns]
    ys = [f"{v:g}" for v in pivot.index]
    m_label = tr(f"m.{metric}", lang)
    fig = go.Figure(go.Heatmap(
        z=z, x=xs, y=ys, colorscale=colorscale, **zrange, xgap=2, ygap=2,
        texttemplate="%{z:" + text_fmt + "}" if z.size <= 225 else None, textfont=dict(size=11),
        colorbar=dict(title=m_label, tickformat=text_fmt, thickness=12),
        hovertemplate=f"{x_label}=%{{x}}<br>{y_label}=%{{y}}<br>{m_label}=%{{z:{hover_fmt}}}<extra></extra>"))
    if np.isfinite(z).any():
        iy, ix = np.unravel_index(np.nanargmax(z), z.shape)
        fig.add_shape(type="rect", x0=ix - 0.5, x1=ix + 0.5, y0=iy - 0.5, y1=iy + 0.5,
                      line=dict(color=STRATEGY, width=3))
    fig.update_xaxes(title=x_label, type="category", showgrid=False)
    fig.update_yaxes(title=y_label, type="category", showgrid=False)
    fig.update_layout(height=max(360, 34 * len(ys) + 120), margin=dict(l=10, r=10, t=20, b=10))
    return fig


def param_curve(df: pd.DataFrame, param: str, metric: str, x_label: str, lang: str = "zh") -> go.Figure:
    df = df.sort_values(param)
    _, hover_fmt = _fmt(metric)
    m_label = tr(f"m.{metric}", lang)
    fig = go.Figure(go.Scatter(x=df[param], y=df[metric], mode="lines+markers", name=m_label,
                               line=dict(color=STRATEGY, width=2), marker=dict(size=8),
                               hovertemplate=f"{x_label}=%{{x:g}}<br>{m_label}=%{{y:{hover_fmt}}}<extra></extra>"))
    best = df.loc[df[metric].idxmax()]
    fig.add_trace(go.Scatter(x=[best[param]], y=[best[metric]], mode="markers", name=tr("opt.best", lang),
                             marker=dict(size=14, color=STRATEGY, symbol="star", line=dict(width=1.5, color="white")),
                             hoverinfo="skip"))
    fig.update_xaxes(title=x_label, showgrid=False)
    fig.update_yaxes(title=m_label, tickformat=_fmt(metric)[0], gridwidth=0.5)
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
    return fig
