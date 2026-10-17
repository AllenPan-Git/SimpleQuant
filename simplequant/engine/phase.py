"""
调仓日检验（可信度检查的一项）：定期调仓的策略，换一个调仓日结果可能差很多

动量轮动、定比再平衡每隔 N 根 K 线调仓，选股每 N 个交易日 / 每周 / 每月调仓。调仓落在周期里的哪一天（相位）
本是随意的，却可能决定结果：二八轮动 20/5、2014 年起，相位 0 收益 +267%，相位 2、3 只有 +131%。
这里区间和数据都不变，只把调仓日依次错开 1 ~ N−1 个交易日重新回测（不挪起点：挪起点还会少掉开头几天的行情），
N 个相位正好覆盖所有可能；周期超过 MAX_PHASES 时均匀抽取。结果交给 credibility.check_phase。
"""

import pandas as pd

from .optimize import optimize
from .runner import BrokerConfig

MAX_PHASES = 20          # 最多回测这么多个相位（含当前结果）
PHASED = ("momentum_rotation", "fixed_weight")     # 有固定调仓间隔的择时模板
CYCLE = {"monthly": 20, "weekly": 5}               # 选股每月 / 每周调仓按多少个交易日一个周期


def phases(cycle: int) -> list[int]:
    """要重跑的相位（不含 0 = 当前结果）"""
    if cycle <= 1:
        return []
    if cycle <= MAX_PHASES:
        return list(range(1, cycle))
    return sorted({round(i * cycle / MAX_PHASES) for i in range(1, MAX_PHASES)})


def timing_cycle(spec: dict) -> int:
    """择时策略的调仓周期（K 线数）；没有固定周期的策略返回 0"""
    if spec.get("kind") != "template" or spec.get("template") not in PHASED:
        return 0
    from ..strategies import TEMPLATES
    tpl = TEMPLATES[spec["template"]]
    return int({**tpl.defaults(), **(spec.get("params") or {})}["rebalance_bars"])


def selection_cycle(spec: dict) -> int:
    rule = spec.get("rebalance", "monthly")
    return CYCLE.get(rule) or int(rule)


def run_timing(prices: dict[str, pd.DataFrame], spec: dict, broker: BrokerConfig | None = None,
               trade_start=None, progress=None) -> pd.DataFrame:
    """择时：每个相位一行（phase, total_return, max_drawdown），出错的相位丢掉"""
    ks = phases(timing_cycle(spec))
    if not ks:
        return pd.DataFrame(columns=["phase", "total_return", "max_drawdown"])
    df = optimize(prices, spec, {"params.phase": ks}, broker, progress=progress, trade_start=trade_start)
    df = df.rename(columns={"params.phase": "phase"})
    if "error" in df:
        df = df[df["error"].isna()]
    return df[["phase", "total_return", "max_drawdown"]].reset_index(drop=True)


def run_selection_phases(panel, spec: dict, broker: BrokerConfig | None = None, start=None,
                         progress=None, stop=None) -> pd.DataFrame:
    """
    选股：同上。因子得分与调仓日无关，各相位共用缓存只算一次；每个相位仍要完整跑一遍回测，所以较慢。
    stop()：返回 True 时停止，已完成的相位照常返回
    """
    from ..stocks.selection import build_schedule, run_selection
    ks, rows, cache = phases(selection_cycle(spec)), [], {}
    for i, k in enumerate(ks):
        if stop and stop():
            break
        s = {**spec, "phase": k}
        try:
            res = run_selection(panel, spec, broker, build_schedule(panel, s, start, cache=cache), start)
            rows.append({"phase": k, "total_return": res.metrics["total_return"],
                         "max_drawdown": res.metrics["max_drawdown"]})
        except ValueError:           # 这个相位没有一个调仓日选得出股票
            pass
        if progress:
            progress(i + 1, len(ks))
    return pd.DataFrame(rows, columns=["phase", "total_return", "max_drawdown"])
