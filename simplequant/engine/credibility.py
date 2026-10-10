"""
回测可信度检查：回测结果页、参数优化结果页自动给出的提示

- 交易次数太少：几笔交易的胜负很可能是偶然
- 收益集中：去掉最好的 3 笔交易后还剩多少收益
- 按交易重抽样（bootstrap）：把已平仓交易有放回地抽 N 笔、求和，重复多次，看累计收益的区间和亏损的概率
- 期末未平仓：胜率、盈亏比只统计已平仓交易；期末持仓浮亏较大，或收益多半是还没兑现的浮盈时提示
- 参数优化：试的组数多时最好的一组可能是运气；多数组合亏损；样本外明显比样本内差

每条结果是 {"level": "warn" / "note" / "ok", "key": 文本键, "args": 参数}；
args 里的浮点数一律是比例（界面显示成百分数），整数原样显示。
"""

import numpy as np
import pandas as pd

FEW_TRADES = 10          # 少于这个数：结果基本不可信
SOME_TRADES = 30         # 少于这个数：结果仍有较大偶然性
TOP_N = 3                # 收益集中：去掉最好的几笔
BOOT_MIN = 5             # 少于这个数不做重抽样
BOOT_RUNS = 2000
BOOT_CI = (0.05, 0.95)
LOSS_PROB_WARN = 0.25    # 重抽样中亏损的比例达到这个数时警告
OPEN_PNL = 0.03          # 期末持仓浮盈亏达到初始资金的这个比例才提示
MANY_COMBOS = 20         # 参数优化试了这么多组起提示「最好的一组可能是运气」


def _f(key: str, level: str, **args) -> dict:
    return {"level": level, "key": f"cred.{key}", "args": args}


def trade_stats(trades: pd.DataFrame, initial_cash: float, total_return: float, seed: int = 0) -> dict:
    """已平仓交易的统计：笔数、最好 N 笔的收益、去掉它们后的收益、重抽样区间与亏损概率"""
    pnl = trades["pnl_net"].astype(float).to_numpy() if len(trades) else np.zeros(0)
    n = len(pnl)
    out = {"n": n, "closed_return": float(pnl.sum() / initial_cash) if initial_cash else 0.0}
    if n == 0 or not initial_cash:
        return out
    # 未平仓部分（以及现金分红等不属于某笔交易的收益）= 总收益 − 已平仓交易的收益，各种算法里都保持不变
    other = total_return - pnl.sum() / initial_cash
    top = np.sort(pnl)[::-1][:TOP_N]
    top = top[top > 0]
    out.update(top_n=len(top), top_return=float(top.sum() / initial_cash),
               ex_top_return=float(total_return - top.sum() / initial_cash))
    if n >= BOOT_MIN:
        rng = np.random.default_rng(seed)
        sums = pnl[rng.integers(0, n, size=(BOOT_RUNS, n))].sum(axis=1) / initial_cash + other
        lo, hi = np.quantile(sums, BOOT_CI)
        out.update(boot_lo=float(lo), boot_hi=float(hi), boot_loss=float((sums < 0).mean()))
    return out


def open_pnl(positions: list | None, initial_cash: float) -> float:
    """期末持仓按最后收盘价计的浮动盈亏，占初始资金的比例（成本不含买入手续费）"""
    if not positions or not initial_cash:
        return 0.0
    return float(sum((p["price"] - p["cost"]) * p["size"] for p in positions) / initial_cash)


def check_backtest(metrics: dict, trades: pd.DataFrame, tried: int = 0, positions: list | None = None) -> list[dict]:
    """
    :param tried: 这组参数是从多少组参数里挑出来的（从参数优化页带过来时），0 表示不知道 / 没优化过
    :param positions: 期末持仓（BacktestResult.positions），用来提示未平仓的浮盈亏
    """
    total = float(metrics.get("total_return", 0.0))
    s = trade_stats(trades, float(metrics.get("initial_cash", 0.0)), total)
    n, out = s["n"], []
    if n == 0:
        out.append(_f("no_trades", "warn"))
    elif n < FEW_TRADES:
        out.append(_f("few_trades", "warn", n=n))
    elif n < SOME_TRADES:
        out.append(_f("some_trades", "note", n=n))
    else:
        out.append(_f("enough_trades", "ok", n=n))

    if n >= BOOT_MIN and total > 0 and s["top_n"]:
        args = dict(k=s["top_n"], top=s["top_return"], rest=s["ex_top_return"], total=total)
        if s["ex_top_return"] <= 0:
            out.append(_f("concentrated", "warn", **args))
        elif s["top_return"] > total / 2:
            out.append(_f("top_heavy", "note", **args))
        else:
            out.append(_f("spread", "ok", **args))

    if "boot_lo" in s:
        args = dict(lo=s["boot_lo"], hi=s["boot_hi"], loss=s["boot_loss"], runs=BOOT_RUNS)
        if total <= 0:                   # 回测本身就亏损：只给出区间
            out.append(_f("boot_neg", "note", **args))
        elif s["boot_loss"] >= LOSS_PROB_WARN:
            out.append(_f("boot_bad", "warn", **args))
        elif s["boot_lo"] < 0:
            out.append(_f("boot_mixed", "note", **args))
        else:
            out.append(_f("boot_ok", "ok", **args))

    if positions and n:          # 没有已平仓交易时 no_trades 已经说明收益全来自持仓
        cash = float(metrics.get("initial_cash", 0.0))
        u = open_pnl(positions, cash)
        args = dict(k=len(positions), open=u, closed=float(s["closed_return"]), total=total)
        if u <= -OPEN_PNL:
            out.append(_f("open_loss", "warn", **args))
        elif u >= OPEN_PNL and total > 0 and u > total / 2:
            out.append(_f("open_gain", "note", **args))

    if metrics.get("limit_blocked"):
        out.append(_f("limit_blocked", "note", n=int(metrics["limit_blocked"])))

    if tried > 1:
        out.append(_f("tuned", "warn" if tried >= MANY_COMBOS else "note", n=tried))
    return out


def check_grid(df: pd.DataFrame, metric: str, compare: dict) -> list[dict]:
    """
    参数优化结果的检查
    :param df: optimize() 的结果表
    :param compare: {"is_best": 指标, "oos_best": 指标, "oos_orig": 指标}，没有切分样本外时只有 is_best
    """
    out = []
    ok = df[df[metric].notna()] if metric in df else df.iloc[0:0]
    n = len(ok)
    if n and "total_return" in ok:
        win = int((ok["total_return"] > 0).sum())
        args = dict(n=n, win=win, share=win / n)
        if win / n < 0.5:
            out.append(_f("grid_mostly_lose", "warn", **args))
        elif n >= MANY_COMBOS:
            out.append(_f("grid_many", "note", **args))
    if "oos_best" in compare:
        is_m, oos_m = compare["is_best"], compare["oos_best"]
        is_v, oos_v = is_m.get("sharpe", np.nan), oos_m.get("sharpe", np.nan)
        args = dict(is_cagr=is_m.get("cagr", 0.0), oos_cagr=oos_m.get("cagr", 0.0))
        if is_v > 0 and not oos_v > is_v * 0.5:
            out.append(_f("oos_worse", "warn", **args))
        else:
            out.append(_f("oos_ok", "ok", **args))
    elif n >= MANY_COMBOS:
        out.append(_f("no_oos", "note"))
    return out
