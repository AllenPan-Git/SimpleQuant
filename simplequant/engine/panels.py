"""
从跑完的 Backtrader 策略里取出指标数值，供交互式图表使用

按 Backtrader 自己的绘图规则（plotinfo）：
- plot=False 的不画（如规则策略里的交叉信号）
- subplot=False 的叠加在 K 线上（均线、布林带），True 的单独占一栏（MACD、RSI……）
- plotlines 里 _plotskip 的线不画，_name 作为线名；plothlines / plotyhlines 作为参考线（如 RSI 的 30/70）
"""

import backtrader as bt
import numpy as np
import pandas as pd


def _root_data(obj, depth: int = 0):
    """指标是算在哪个标的上的（沿着 data / _owner / _clock 往上找到数据源）"""
    if isinstance(obj, bt.AbstractDataBase):
        return obj
    if depth > 12:
        return None
    for attr in ("data", "_owner", "_clock"):
        nxt = getattr(obj, attr, None)
        if nxt is not None and nxt is not obj:
            found = _root_data(nxt, depth + 1)
            if found is not None:
                return found
    return None


def _index(data) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([bt.num2date(x) for x in data.datetime.array])


def _array(obj) -> np.ndarray:
    buf = obj if isinstance(obj, bt.linebuffer.LineBuffer) else obj.lines[0]
    return np.asarray(buf.array, dtype=float)


def _rule_panels(strat) -> dict[str, list[dict]]:
    """规则策略：画规则里实际用到的线（名称由界面按规则描述生成，见 spec / line 字段）"""
    out = {}
    for d in strat.datas:
        idx = _index(d)
        panels = []
        for g in strat.chart_groups():
            lines = {}
            for key, spec in g["lines"].items():
                arr = _array(strat._line(d, spec))     # 策略初始化时已建好，这里取的是缓存
                n = min(len(arr), len(idx))
                lines[key] = pd.Series(arr[:n], index=idx[:n])
            panels.append({"name": None, "spec": g["spec"], "overlay": g["overlay"], "lines": lines,
                           "hlines": sorted(g["hlines"])})
        out[d._name] = panels
    return out


LEVELS = {"RSI": [30, 70], "RSI_Safe": [30, 70]}      # 模板策略的常用参考线


def collect_panels(strat) -> dict[str, list[dict]]:
    """
    :return: {标的名: [{"name", "spec", "overlay", "lines": {线名: Series}, "hlines": [...]}, ...]}
    规则策略画规则里用到的线；其它策略按 Backtrader 的 plotinfo 规则取指标
    """
    if hasattr(strat, "chart_groups"):
        return _rule_panels(strat)
    out = {d._name: [] for d in strat.datas}
    indexes = {}
    for ind in strat.getindicators():
        info = getattr(ind, "plotinfo", None)
        if info is None or not info.plot or getattr(info, "plotskip", False):
            continue
        data = _root_data(ind)
        if data is None or data._name not in out:
            continue
        idx = indexes.setdefault(data._name, _index(data))
        lines = {}
        for i, alias in enumerate(ind.lines.getlinealiases()):
            style = getattr(ind.plotlines, alias, None)
            if style is not None and getattr(style, "_plotskip", False):
                continue
            arr = np.asarray(ind.lines[i].array, dtype=float)
            n = min(len(arr), len(idx))
            if n == 0:
                continue
            name = getattr(style, "_name", None) or alias
            lines[name] = pd.Series(arr[:n], index=idx[:n])
        if not lines:
            continue
        hlines = list(getattr(info, "plothlines", None) or getattr(info, "plotyhlines", None) or
                      LEVELS.get(type(ind).__name__, []))
        out[data._name].append({"name": ind.plotlabel(), "spec": None, "overlay": not info.subplot,
                                "lines": lines, "hlines": hlines})
    return out
