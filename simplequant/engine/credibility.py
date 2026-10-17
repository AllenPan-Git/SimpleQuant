"""
回测可信度检查：回测结果页、参数优化结果页自动给出的提示

- 交易次数太少：几笔交易的胜负很可能是偶然
- 收益集中：去掉最好的 3 笔交易后还剩多少收益
- 按交易重抽样（bootstrap）：把已平仓交易有放回地抽 N 笔、求和，重复多次，看累计收益的区间和亏损的概率
- 随机买卖对照：同样的笔数和持有时间、随机的买卖时机，看策略的已平仓交易排在第几
- 期末未平仓：胜率、盈亏比只统计已平仓交易；期末持仓浮亏较大，或收益多半是还没兑现的浮盈时提示
- 调仓日检验：定期调仓的策略把调仓日错开 1 ~ N−1 天重跑（engine/phase.py），当前结果明显高于中位数时警告
- 参数优化：试的组数多时最好的一组可能是运气；多数组合亏损；样本外明显比样本内差；
  最优参数逐年排名（多数年份排不进前一半 = 全段第一靠的是个别年份）
- 机器学习合成因子：样本外 IC 是否为正、比训练集 IC 低多少（过拟合）、是否不如 ICIR 合成

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
RANDOM_MIN = 5           # 随机买卖对照：已平仓交易少于这个数不做
RANDOM_RUNS = 1000
RANDOM_GOOD = 0.95       # 好于这么多比例的随机买卖：买卖时机有优势
YEARS_MIN = 3           # 至少这么多个自然年才看最优参数的逐年排名
ML_FEW = 12              # 机器学习样本外不足这么多期：IC 均值误差大
ML_OVERFIT = 0.5         # 样本外 IC 不到训练集 IC 的这个比例：过拟合
MINE_FEW = 12            # AI 因子挖掘：检验期不足这么多期，IC 均值误差大
PHASE_MIN = 3            # 调仓日检验：连同当前结果至少这么多个相位才下结论
PHASE_FLAT = 0.05        # 期末资产最高与最低相差不到这个比例：调仓日影响不大
PHASE_HIGH = 0.15        # 当前结果的期末资产比中位数高（低）这个比例：明显偏高（偏低）


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


def random_rank(prices: dict, trades: pd.DataFrame, runs: int = RANDOM_RUNS, seed: int = 0) -> dict | None:
    """
    随机买卖对照：每个标的保留同样的交易笔数和每笔的持有 K 线数，只把买卖时机随机打乱（不重叠），
    比较已平仓交易的对数收益之和（都按成交那根 K 线的开盘价算；笔数相同，交易成本相同，不计）。
    :return: {"n": 交易笔数, "runs": 次数, "beat": 策略好于随机的比例（相等算一半）}；算不了时 None
    """
    if trades is None or len(trades) < RANDOM_MIN or not prices:
        return None
    rng = np.random.default_rng(seed)
    mine, rand, n = 0.0, np.zeros(runs), 0
    for sym, g in trades.groupby("symbol"):
        df = prices.get(sym)
        if df is None or len(df) < 2:
            return None
        px = (df["open"].where(df["open"] > 0, df["close"]) if "open" in df else df["close"]).to_numpy(float)
        if not np.isfinite(px).all() or (px <= 0).any():
            return None
        lp = np.log(px)
        idx = df.index
        a = np.clip(idx.searchsorted(pd.DatetimeIndex(pd.to_datetime(g["open_time"]))), 0, len(idx) - 1)
        b = np.clip(idx.searchsorted(pd.DatetimeIndex(pd.to_datetime(g["close_time"]))), 0, len(idx) - 1)
        lens = np.maximum(b - a, 0)
        slack = len(idx) - 1 - int(lens.sum())
        if slack < 0:                    # 持仓重叠（不应出现），无法不重叠地重新摆放
            return None
        mine += float((lp[b] - lp[a]).sum())
        k = len(lens)
        # 随机顺序 + 随机间隔：k + 1 段空档之和 = slack（k 个有序均匀点把 [0, slack] 分开）
        order = np.argsort(rng.random((runs, k)), axis=1)
        L = lens[order]
        cuts = np.sort(rng.integers(0, slack + 1, size=(runs, k)), axis=1)
        starts = cuts + np.concatenate([np.zeros((runs, 1), int), np.cumsum(L, axis=1)[:, :-1]], axis=1)
        rand += (lp[starts + L] - lp[starts]).sum(axis=1)
        n += k
    beat = float(((rand < mine - 1e-12).sum() + 0.5 * (np.abs(rand - mine) <= 1e-12).sum()) / runs)
    return {"n": n, "runs": runs, "beat": beat}


def open_pnl(positions: list | None, initial_cash: float) -> float:
    """期末持仓按最后收盘价计的浮动盈亏，占初始资金的比例（成本不含买入手续费）"""
    if not positions or not initial_cash:
        return 0.0
    return float(sum((p["price"] - p["cost"]) * p["size"] for p in positions) / initial_cash)


def check_backtest(metrics: dict, trades: pd.DataFrame, tried: int = 0, positions: list | None = None,
                   prices: dict | None = None) -> list[dict]:
    """
    :param tried: 这组参数是从多少组参数里挑出来的（从参数优化页带过来时），0 表示不知道 / 没优化过
    :param positions: 期末持仓（BacktestResult.positions），用来提示未平仓的浮盈亏
    :param prices: 回测用的行情（BacktestResult.prices），有了才做随机买卖对照
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

    if prices and (r := random_rank(prices, trades)):
        args = dict(n=r["n"], runs=r["runs"], beat=r["beat"])
        if r["beat"] >= RANDOM_GOOD:
            out.append(_f("random_ok", "ok", **args))
        elif r["beat"] >= 0.5:
            out.append(_f("random_mid", "note", **args))
        else:
            out.append(_f("random_bad", "warn", **args))

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


def check_phase(metrics: dict, rows: pd.DataFrame) -> list[dict]:
    """
    调仓日检验。rows：engine/phase.py 的结果（其他相位的 total_return、max_drawdown），当前结果来自 metrics。
    比较用期末资产（1 + 收益）的比值，长区间收益很大时也不会因为差几十个百分点就报警
    """
    cur = float(metrics.get("total_return", 0.0))
    ret = np.r_[cur, rows["total_return"].dropna().to_numpy(dtype=float)]
    if len(ret) < PHASE_MIN:
        return []
    dd = np.r_[float(metrics.get("max_drawdown", 0.0)), rows["max_drawdown"].dropna().to_numpy(dtype=float)]
    lo, med, hi = float(ret.min()), float(np.median(ret)), float(ret.max())
    args = dict(n=len(ret), m=int(rows["phase"].max()), lo=lo, med=med, hi=hi, cur=cur,
                rank=int((ret > cur + 1e-12).sum()) + 1, dd_lo=float(dd.min()), dd_hi=float(dd.max()))
    gap = (1 + cur) / max(1 + med, 1e-9) - 1
    if (1 + hi) / max(1 + lo, 1e-9) - 1 < PHASE_FLAT:
        return [_f("phase_flat", "ok", **args)]
    if gap >= PHASE_HIGH:
        return [_f("phase_high", "warn", **args)]
    if gap <= -PHASE_HIGH:
        return [_f("phase_low", "note", **args)]
    return [_f("phase_wide", "note", **args)]


def check_grid(df: pd.DataFrame, metric: str, compare: dict, years: pd.DataFrame | None = None) -> list[dict]:
    """
    参数优化结果的检查
    :param df: optimize() 的结果表
    :param compare: {"is_best": 指标, "oos_best": 指标, "oos_orig": 指标}，没有切分样本外时只有 is_best
    :param years: optimize.yearly_ranks() 的结果（最优参数逐年排名）
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
    if years is not None and len(years) >= YEARS_MIN and n >= 4:
        upper = int((years["rank"] <= np.ceil(years["n"] / 2)).sum())
        first = int((years["rank"] == 1).sum())
        args = dict(years=len(years), upper=upper, first=first)
        out.append(_f("years_unstable", "warn", **args) if upper * 2 < len(years) else _f("years_stable", "ok", **args))
    return out


def check_ml(summary: dict) -> list[dict]:
    """机器学习合成因子（stocks/ml.summary 的结果）。IC 以字符串传给界面（不是比例，不显示成百分数）"""
    out = []
    oos = summary.get("oos") or {}
    ml_ic, icir_ic, train = oos.get("ml", np.nan), oos.get("icir", np.nan), summary.get("train_ic", np.nan)
    ic = lambda v: f"{v:.3f}"
    if summary.get("n_fallback"):
        out.append(_f("ml_fallback", "note", n=int(summary["n_fallback"])))
    if not np.isfinite(ml_ic):
        out.append(_f("ml_no_oos", "note"))
        return out
    n = int(summary.get("n_oos", 0))
    if n < ML_FEW:
        out.append(_f("ml_few", "note", n=n))
    if ml_ic <= 0:
        out.append(_f("ml_oos_neg", "warn", oos=ic(ml_ic), n=n))
    elif np.isfinite(train) and train > 0 and ml_ic < train * ML_OVERFIT:
        out.append(_f("ml_overfit", "warn", train=ic(train), oos=ic(ml_ic)))
    else:
        out.append(_f("ml_oos_ok", "ok", train=ic(train) if np.isfinite(train) else "—", oos=ic(ml_ic), n=n))
    if np.isfinite(icir_ic) and ml_ic < icir_ic:
        out.append(_f("ml_below_icir", "note", oos=ic(ml_ic), icir=ic(icir_ic)))
    return out


def check_mining(summary: dict) -> list[dict]:
    """AI 因子挖掘（stocks/mining.Miner.finish 的结果）。IC、t 以字符串传给界面"""
    out = []
    num = lambda v, d=2: f"{v:.{d}f}"
    tried, accepted = int(summary.get("tried", 0)), int(summary.get("accepted", 0))
    out.append(_f("mine_tried", "note", proposed=int(summary.get("proposed", 0)), tried=tried, accepted=accepted,
                  t=num(summary.get("t_needed", np.nan))))
    if not accepted:
        out.append(_f("mine_none", "note"))
        return out
    if summary.get("lucky"):
        out.append(_f("mine_lucky", "warn", n=int(summary["lucky"]), t=num(summary["t_needed"])))
    n_test = int(summary.get("n_test", 0))
    if n_test < MINE_FEW:
        out.append(_f("mine_test_few", "note", n=n_test))
    held = int(summary.get("held", 0))
    if held == 0:
        out.append(_f("mine_test_fail", "warn", n=accepted))
    elif held < accepted:
        out.append(_f("mine_test_some", "note", held=held, n=accepted))
    else:
        out.append(_f("mine_test_ok", "ok", n=accepted))
    combos = summary.get("combos") or {}
    mined, base = combos.get("mined"), combos.get("existing")
    if mined and base and np.isfinite(mined.get("ic", np.nan)) and np.isfinite(base.get("ic", np.nan)) \
            and mined["ic"] < base["ic"]:
        out.append(_f("mine_below_base", "note", mined=num(mined["ic"], 3), base=num(base["ic"], 3),
                      k=int(base.get("factors", 0))))
    return out
