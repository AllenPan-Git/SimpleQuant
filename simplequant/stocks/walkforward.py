"""
选股策略的滚动（walk-forward）优化

与择时策略的滚动优化（engine/walkforward.py）思路相同：每个窗口只用训练期挑参数，在紧接着的测试期检验，
把各测试期拼起来得到完全样本外的资产曲线。

做法上利用了选股回测的特点：每组参数在 T 日的选股和持仓只取决于 T 日及以前的数据，
所以每组参数只需从头到尾回测一次，各窗口的训练期指标、测试期收益都从这条资产曲线上截取，
不必每个窗口重跑。这正是"延续持仓"的拼接方式——选出的参数视为一直在运行，
换参数时的调仓成本略有少算。

可调的参数由用户在页面上选择：持股数、调仓频率、各因子权重、IC 回看期、仓位等。
"""

from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..engine.optimize import build_grid, default_workers
from ..engine.results import compute_metrics
from ..engine.walkforward import make_windows, WalkForwardResult
from ..i18n import L, pick
from ..strategies import Tunable
from .factors import FACTORS
from .panel import Panel
from .selection import build_schedule, run_selection

MAX_COMBOS = 200
# 选股策略的优化目标（均为越大越好；最大回撤是负数，越接近 0 越好）
TARGET_METRICS = ["sharpe", "total_return", "cagr", "calmar", "max_drawdown", "excess_return"]
# 可选的调仓频率（离散参数，页面上多选）
REBALANCE_CHOICES = ["weekly", "monthly", 5, 10, 20, 40, 60]


def selection_tunables(spec: dict, lang: str = "zh") -> list[Tunable]:
    """选股策略里可做网格搜索的数值参数（调仓频率是离散的，单独处理）"""
    count = L("持有转债数", "Number of bonds") if spec.get("universe") == "cb" else L("持有股票数", "Number of stocks")
    out = [Tunable("top_n", pick(count, lang), int(spec["top_n"]), True, 1, 100)]
    if spec.get("weighting", "manual") == "manual":
        if len(spec["factors"]) > 1:            # 只有一个因子时权重不影响排序
            for i, f in enumerate(spec["factors"]):
                out.append(Tunable(f"factors.{i}.weight",
                                   pick(L("权重", "Weight"), lang) + " · " + pick(FACTORS.get(f["key"], {}).get("label", f["key"]), lang),
                                   float(f.get("weight", 1.0)), False, 0.0, 10.0))
    else:
        out.append(Tunable("ic_lookback", pick(L("IC 回看（交易日）", "IC lookback (days)"), lang),
                           int(spec.get("ic_lookback", 252)), True, 60, 1000))
    out.append(Tunable("position_pct", pick(L("仓位(%)", "Position (%)"), lang), int(spec.get("position_pct", 95)),
                       True, 10, 100))
    return out


def selection_grid(spec: dict, axes: dict[str, list]) -> list[tuple[dict, dict]]:
    """[(参数组合, 策略描述)]；剔除所有因子权重都为 0 的组合"""
    return [(c, s) for c, s in build_grid(spec, axes)
            if s.get("weighting", "manual") != "manual" or any(float(f.get("weight", 1)) for f in s["factors"])]


@dataclass
class SelectionWalkForwardResult(WalkForwardResult):
    grid: pd.DataFrame = field(default_factory=pd.DataFrame)   # 每组参数在整个区间的表现（仅供参考，非样本外）


# ---------- 工作进程 ----------
_G: dict = {}


def _init(panel, broker, start):
    _G.update(panel=panel, broker=broker, start=start)


def _run(spec, schedule):
    try:
        r = run_selection(_G["panel"], spec, _G["broker"], schedule=schedule, start=_G["start"])
        return r.equity[["value", "benchmark"]], r.trades, r.metrics
    except Exception as e:  # noqa: BLE001 - 单个组合失败不影响整体
        return str(e)


def _slim(panel: Panel, codes: list[str]) -> Panel:
    """回测只用到被选中过的股票和少数几列：传给子进程的数据小很多"""
    fields = {f: panel[f][codes] for f in ("open", "high", "low", "close", "volume", "raw_close")}
    return Panel(fields=fields, member=panel.member[codes], tradable=panel.tradable[codes], is_st=panel.is_st[codes],
                 listed_days=panel.listed_days[codes], can_buy=panel.can_buy[codes], can_sell=panel.can_sell[codes],
                 benchmark=panel.benchmark, names={})


def _run_all(panel, tasks, broker, start, workers, progress):
    codes = sorted({c for _, sched in tasks if sched is not None for p in sched.picks.values() for c in p})
    slim = _slim(panel, codes)
    results = [None] * len(tasks)
    todo = [i for i, (_, sched) in enumerate(tasks) if sched is not None]
    for i, (_, sched) in enumerate(tasks):
        if sched is None:
            results[i] = "no rebalance date had enough eligible stocks / 没有任何调仓日有足够的可选股票"

    def sequential():
        _init(slim, broker, start)
        for k, i in enumerate(todo, 1):
            results[i] = _run(*tasks[i])
            if progress:
                progress(k, len(todo))

    if workers <= 1 or len(todo) < 4:
        sequential()
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(slim, broker, start)) as ex:
                futures = {ex.submit(_run, *tasks[i]): i for i in todo}
                for k, f in enumerate(as_completed(futures), 1):
                    results[futures[f]] = f.result()
                    if progress:
                        progress(k, len(todo))
        except (BrokenProcessPool, OSError):
            sequential()
    return results


def _window_metrics(value: pd.Series, bench: pd.Series, trades: pd.DataFrame, lo, hi, risk_free) -> dict:
    """资产曲线上 [lo, hi] 这一段的指标：以 lo 前一天的资产为起点"""
    i0, i1 = value.index.searchsorted(lo), value.index.searchsorted(hi, side="right")
    v, b = value.iloc[max(i0 - 1, 0):i1], bench.iloc[max(i0 - 1, 0):i1]
    closed = pd.to_datetime(trades["close_time"]) if len(trades) else pd.Series(dtype="datetime64[ns]")
    tr_ = trades[(closed >= lo) & (closed <= hi)] if len(trades) else trades
    return compute_metrics(v, b, tr_, float(v.iloc[0]), risk_free)


def walk_forward_selection(panel: Panel, spec: dict, axes: dict[str, list], broker, metric: str = "sharpe",
                           train_months: int = 24, test_months: int = 6, anchored: bool = False, start=None,
                           workers: int | None = None, progress=None) -> SelectionWalkForwardResult:
    """
    :param axes: {参数路径: 取值列表}，如 {"top_n": [5, 10, 20], "rebalance": ["weekly", "monthly"]}
    :param start: 回测起点；面板里更早的数据只用来预热因子
    :param progress: progress(阶段, 已完成, 总数)，阶段为 "select"（计算各组参数的选股名单）或 "backtest"
    """
    grid = selection_grid(spec, axes)
    if not grid:
        raise ValueError("no valid parameter combinations / 没有有效的参数组合")
    if len(grid) > MAX_COMBOS:
        raise ValueError(f"too many combinations ({len(grid)} > {MAX_COMBOS}) / 组合数过多")
    cal = panel.calendar if start is None else panel.calendar[panel.calendar >= pd.Timestamp(start)]
    windows = make_windows(cal, train_months, test_months, anchored)
    if not windows:
        raise ValueError("not enough data for one train+test window / 数据不够一个训练期加测试期")

    # 1. 每组参数的选股名单（因子只算一次）；原参数不在网格里时另加一组作为参照
    specs = [s for _, s in grid]
    base_i = next((i for i, s in enumerate(specs) if s == spec), None)
    if base_i is None:
        specs.append(spec)
        base_i = len(specs) - 1
    cache, tasks = {}, []
    for i, s in enumerate(specs, 1):
        sched = build_schedule(panel, s, start, cache=cache)
        tasks.append((s, sched if sched.picks else None))
        if progress:
            progress("select", i, len(specs))

    # 2. 每组参数从头到尾回测一次
    runs = _run_all(panel, tasks, broker, start, workers or default_workers(),
                    (lambda d, n: progress("backtest", d, n)) if progress else None)
    if isinstance(runs[base_i], str):
        raise ValueError(runs[base_i])
    ok = [i for i in range(len(grid)) if not isinstance(runs[i], str)]
    if not ok:
        raise ValueError("all combinations failed / 所有组合都失败了")
    base_eq, base_trades, _ = runs[base_i]
    bench = base_eq["benchmark"]
    values = {i: runs[i][0]["value"] for i in ok}

    # 3. 每个窗口：按训练期指标挑参数，取它在测试期的收益
    rows, param_rows, segments, test_trades, notes = [], [], [], [], []
    for w_i, w in enumerate(windows, 1):
        scores = {}
        for i in ok:
            m = _window_metrics(values[i], bench, runs[i][1], w.train_start, w.train_end, broker.risk_free)
            if np.isfinite(m[metric]):
                scores[i] = m
        if scores:
            best = max(scores, key=lambda i: scores[i][metric])
            combo, train_m = grid[best][0], scores[best]
        else:                               # 训练期所有组合的指标都无效：沿用原参数
            best, combo, train_m = base_i, {p: np.nan for p in axes}, {metric: np.nan, "cagr": np.nan}
            notes.append(f"window {w_i}: no valid {metric} in training period, original parameters used")
        v = runs[best][0]["value"]
        seg = v.loc[w.train_end:w.test_end].pct_change().dropna()
        segments.append(seg)
        trades = runs[best][1]
        if len(trades):
            closed = pd.to_datetime(trades["close_time"])
            test_trades.append(trades[(closed >= w.test_start) & (closed <= w.test_end)])
        seg_value = (1 + seg).prod() - 1
        years = max((w.test_end - w.test_start).days / 365.25, 1 / 365.25)
        cum = (1 + seg).cumprod()
        rows.append({"window": w_i, "train_start": w.train_start, "train_end": w.train_end,
                     "test_start": w.test_start, "test_end": w.test_end, **combo,
                     "train_metric": float(train_m[metric]), "train_cagr": float(train_m["cagr"]),
                     "test_return": seg_value, "test_cagr": (1 + seg_value) ** (1 / years) - 1,
                     "test_max_drawdown": float((cum / cum.cummax() - 1).min()) if len(cum) else 0.0})
        param_rows.append({"test_start": w.test_start, **combo})

    # 4. 拼接样本外曲线；参照：原参数一直运行、基准指数
    returns = pd.concat(segments)
    returns = returns[~returns.index.duplicated(keep="first")].sort_index()
    cash = broker.cash
    value = pd.concat([pd.Series([cash], index=[windows[0].train_end]), cash * (1 + returns).cumprod()])
    base_ret = base_eq["value"].pct_change().reindex(value.index).fillna(0)
    base_ret.iloc[0] = 0.0
    b = bench.reindex(value.index).ffill()
    equity = pd.DataFrame({"value": value, "baseline": cash * (1 + base_ret).cumprod(), "benchmark": b / b.iloc[0] * cash})
    equity["drawdown"] = equity["value"] / equity["value"].cummax() - 1

    oos_start = windows[0].test_start
    trades_all = pd.concat(test_trades) if test_trades else base_trades.iloc[0:0]
    metrics = compute_metrics(equity["value"], equity["benchmark"], trades_all, cash, broker.risk_free)
    closed = pd.to_datetime(base_trades["close_time"]) if len(base_trades) else None
    base_metrics = compute_metrics(equity["baseline"], equity["benchmark"],
                                   base_trades[closed >= oos_start] if closed is not None else base_trades,
                                   cash, broker.risk_free)
    table = pd.DataFrame(rows)
    is_cagr = table["train_cagr"].mean()
    wfe = metrics["cagr"] / is_cagr if is_cagr and is_cagr > 0 else np.nan

    full = pd.DataFrame([{**grid[i][0], **(runs[i][2] if i in ok else {"error": runs[i]})} for i in range(len(grid))])
    failed = len(grid) - len(ok)
    if failed:
        notes.append(f"{failed} combination(s) failed and were skipped")
    return SelectionWalkForwardResult(windows=table, equity=equity, metrics=metrics, baseline_metrics=base_metrics,
                                      params=pd.DataFrame(param_rows), wfe=float(wfe), notes=notes, grid=full)
