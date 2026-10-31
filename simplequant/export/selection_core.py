"""
SimpleQuant 多因子选股核心：与平台无关的纯 Python 实现（只依赖 numpy，兼容 Python 3.6）

导出到聚宽 / 掘金 / QMT 时，这个文件的正文原样嵌入生成的策略里；各平台只负责取数据（成分股、行情、估值、ST、
上市日期）、查持仓、下单。算法逐项对照 simplequant/stocks（factors.py、selection.py）：
- 因子计算、按日 MAD 去极值 + z-score、市值中性化、手动 / IC / ICIR 加权合成、取前 top_n
- 调仓日：每月 / 每周最后一个交易日，或从数据起点起每 N 个交易日
- 大盘条件：股票池指数的收盘价逐日判断空仓 / 恢复（指标用 signal_core，与择时策略相同）
- 调仓执行：先重试之前卖不出的，再卖出落选的，最后用释放的资金等额买入新入选的（整手、预留 1% 资金）
测试保证：给定与 SimpleQuant 相同的数据，每个调仓日选出的名单完全相同（tests/test_selection_export.py）。

所有矩阵都是「日期 × 股票」的 numpy 二维数组，缺失为 NaN；最后一行是信号日 T（调仓日当天收盘）。

注意：这个文件会在 Python 3.6 上运行，不要使用 3.6 之后的语法。
"""

import datetime
import math
import warnings

import numpy as np

from .signal_core import _CMP, _Lines, _line_mp, _operand_line   # 导出时 signal_core 的正文另外嵌入在前面

NAN = float("nan")
LOT = 100
CASH_BUFFER = 0.01

# 因子的默认方向（与 simplequant/stocks/factors.py 一致）；需要的数据字段
SEL_FACTORS = {
    "ep": (1, ["pe"]), "bp": (1, ["pb"]), "sp": (1, ["ps"]), "size": (-1, ["mcap"]),
    "ret5": (-1, ["close"]), "ret20": (-1, ["close"]), "ret60": (-1, ["close"]), "mom_120_20": (1, ["close"]),
    "vol60": (-1, ["close"]), "turn20": (-1, ["turnover"]), "amihud20": (1, ["close", "amount"]),
}
SEL_WARMUP = 190          # 因子最长回看 120 个交易日（中期动量）+ 余量


def sel_horizon(spec):
    """IC 加权用的持有期：与调仓间隔一致"""
    rule = spec.get("rebalance", "monthly")
    return {"monthly": 20, "weekly": 5}.get(rule) or int(rule)


def sel_uses_ic(spec):
    return spec.get("weighting", "manual") in ("ic", "icir")


def sel_window(spec):
    """计算一次选股需要的历史交易日数"""
    n = SEL_WARMUP
    if sel_uses_ic(spec):
        n += int(spec.get("ic_lookback", 252)) + sel_horizon(spec) + 2
    return n


def sel_fields(spec):
    """需要的数据字段"""
    out = {"close", "volume"}
    for f in spec["factors"]:
        out.update(SEL_FACTORS[f["key"]][1])
    if (spec.get("neutralize") or {}).get("size"):
        out.add("mcap")
    if sel_uses_ic(spec):
        out.add("open")
    return out


# ============================== 调仓日 ==============================
def _monday(day):
    d = datetime.date(int(day[:4]), int(day[5:7]), int(day[8:10]))
    return d - datetime.timedelta(days=d.weekday())


def sel_skip_months(spec):
    """空仓月份（与 SimpleQuant stocks/selection.skip_months 一致）"""
    return set(int(m) for m in spec.get("skip_months") or () if 1 <= int(m) <= 12)


def sel_is_rebalance(spec, calendar, today, start):
    """
    calendar：从数据起点（SimpleQuant 的面板起点）到信号日 T 的交易日（'YYYY-MM-DD'），T = calendar[-1]
    today：T 之后的下一个交易日（执行日）；start：回测开始日，之前不调仓
    """
    t = calendar[-1]
    if t < start:
        return False
    skip = sel_skip_months(spec)
    if skip and t[:7] != today[:7] and (int(t[5:7]) in skip or int(today[5:7]) in skip):
        return True                  # 空仓月份前后的月份交界：不论调仓频率都调仓
    rule = spec.get("rebalance", "monthly")
    if rule == "monthly":
        return t[:7] != today[:7]
    if rule == "weekly":
        return _monday(t) != _monday(today)
    return (len(calendar) - 1) % int(rule) == 0


# ============================== 大盘条件 ==============================
# 只用指数收盘价就能算的指标（指数数据只有开盘、收盘价）
MARKET_INDS = ("close", "sma", "ema", "rsi", "roc", "ma_slope", "bias", "macd", "boll", "volatility")
DEFAULT_RECOVERY = {"logic": "all", "conditions": [{"left": {"ind": "close"}, "op": ">", "right": {"ind": "sma", "params": {"period": 20}}}]}


def uses_drawdown_stop(spec):
    ds = (spec or {}).get("drawdown_stop")
    if isinstance(ds, (int, float)):
        return float(ds) > 0
    if isinstance(ds, dict):
        return float(ds.get("max_drawdown", 0)) > 0
    return False


def sel_recovery_block(spec):
    ds = (spec or {}).get("drawdown_stop")
    if isinstance(ds, dict) and (ds.get("recovery") or {}).get("conditions"):
        return ds["recovery"]
    market = (spec or {}).get("market") or {}
    if (market.get("buy") or {}).get("conditions"):
        return market["buy"]
    return DEFAULT_RECOVERY


def sel_recovery_hit(spec, close):
    close = np.asarray(close, dtype=float)
    n = len(close)
    lines = _Lines({"close": close})
    block = sel_recovery_block(spec)
    hit = _block_series(block, lines, n)
    return np.zeros(n, dtype=bool) if hit is None else hit


def sel_market_on(spec):
    return bool(((spec.get("market") or {}).get("sell") or {}).get("conditions"))


def _cross_series(a, b):
    """每根 K 线的 signal_core.sq_cross（+1 / -1 / 0），一次算完"""
    out = np.zeros(len(a), dtype=int)
    start = max(_line_mp(a), _line_mp(b)) - 1
    if start + 1 >= len(a):
        return out
    nzd = a[start] - b[start]
    for i in range(start + 1, len(a)):
        if nzd < 0 and a[i] > b[i]:
            out[i] = 1
        elif nzd > 0 and a[i] < b[i]:
            out[i] = -1
        d = a[i] - b[i]
        nzd = d if d else nzd
    return out


def _cond_series(cond, lines, n):
    """一条条件逐日是否成立（与 signal_core._check 在每一天的结果相同）"""
    op = cond["op"]
    a, _ = _operand_line(lines, cond["left"], n)
    b, _ = _operand_line(lines, cond["right"], n)
    if op in ("cross_above", "cross_below"):
        return _cross_series(a, b) == (1 if op == "cross_above" else -1)
    with np.errstate(invalid="ignore"):
        hit = _CMP[op](a, b) & ~np.isnan(a) & ~np.isnan(b)
    out = hit.copy()
    for k in range(1, int(cond.get("days", 1))):     # 连续 days 根 K 线都满足
        out[k:] &= hit[:-k]
        out[:k] = False
    return out


def _block_series(block, lines, n):
    conds = (block or {}).get("conditions") or []
    if not conds:
        return None
    hits = [_cond_series(c, lines, n) for c in conds]
    return np.logical_and.reduce(hits) if (block.get("logic") or "all") == "all" else np.logical_or.reduce(hits)


def sel_market_cash(spec, close, first):
    """
    大盘条件：每天收盘后是否空仓（True = 空仓，下一个交易日开盘全卖、不买）
    close：指数收盘价，从数据起点到信号日；first：回测开始日在其中的位置，之前不算（按持仓）
    持仓时空仓条件成立 → 空仓；空仓时恢复条件成立 → 恢复（没设恢复条件时：空仓条件不再成立就恢复）
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    market = spec.get("market") or {}
    lines = _Lines({"close": close})
    out_hit = _block_series(market.get("sell"), lines, n)
    back_hit = _block_series(market.get("buy"), lines, n)
    cash = np.zeros(n, dtype=bool)
    if out_hit is None:
        return cash
    state = False
    for i in range(max(0, int(first)), n):
        if not state:
            state = bool(out_hit[i])
        else:
            state = not back_hit[i] if back_hit is not None else bool(out_hit[i])
        cash[i] = state
    return cash


# ============================== 矩阵工具（与 pandas 行为一致） ==============================
def _ffill(a):
    """按列向前填充（停牌日沿用最后价格）"""
    a = np.array(a, dtype=float)
    for i in range(1, len(a)):
        miss = np.isnan(a[i])
        a[i, miss] = a[i - 1, miss]
    return a


def _mshift(a, k):
    out = np.full(a.shape, NAN)
    if k >= 0 and k < len(a):
        out[k:] = a[:len(a) - k]
    elif k < 0 and -k < len(a):
        out[:k] = a[-k:]
    return out


def _mrolling(a, n, min_periods, fn):
    """pandas rolling(n, min_periods)：窗口内有效值个数不少于 min_periods 才计算（跳过 NaN）"""
    out = np.full(a.shape, NAN)
    for i in range(len(a)):
        win = a[max(0, i - n + 1):i + 1]
        cnt = np.sum(~np.isnan(win), axis=0)
        ok = cnt >= min_periods
        if ok.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                out[i, ok] = fn(win[:, ok])
    return out


def _mean(w):
    return np.nanmean(w, axis=0)


def _std(w):
    return np.nanstd(w, axis=0, ddof=1)


def _pct(close):
    return close / _mshift(close, 1) - 1


def _inv(x, positive_only):
    x = np.where(x != 0, x, NAN)
    if positive_only:
        x = np.where(x > 0, x, NAN)
    return 1 / x


def sel_factor(key, data):
    """原始因子值（与 simplequant/stocks/factors.py 的 FACTORS 一致）"""
    with np.errstate(divide="ignore", invalid="ignore"):
        if key == "ep":
            v = _inv(data["pe"], False)
        elif key == "bp":
            v = _inv(data["pb"], True)
        elif key == "sp":
            v = _inv(data["ps"], True)
        elif key == "size":
            m = data["mcap"]
            v = np.log(np.where(m > 0, m, NAN))
        elif key in ("ret5", "ret20", "ret60"):
            n = int(key[3:])
            v = data["close"] / _mshift(data["close"], n) - 1
        elif key == "mom_120_20":
            v = _mshift(data["close"], 20) / _mshift(data["close"], 120) - 1
        elif key == "vol60":
            v = _mrolling(_pct(data["close"]), 60, 40, _std)
        elif key == "turn20":
            v = _mrolling(data["turnover"], 20, 15, _mean)
        elif key == "amihud20":
            amt = data["amount"]
            v = _mrolling(np.abs(_pct(data["close"])) / np.where(amt > 0, amt, NAN), 20, 15, _mean) * 1e8
        else:
            raise ValueError("unsupported factor: " + key)
    v = np.array(v, dtype=float)
    v[np.isinf(v)] = NAN
    return v


def _winsorize(row, k=5.0):
    """超出 中位数 ± k×1.4826×MAD 的值截断"""
    if np.isnan(row).all():
        return row
    med = np.nanmedian(row)
    mad = np.nanmedian(np.abs(row - med)) * 1.4826
    return np.clip(row, med - k * mad, med + k * mad)


def _zscore(row):
    n = np.sum(~np.isnan(row))
    if n < 2:
        return np.full(row.shape, NAN)
    std = np.nanstd(row, ddof=1)
    if std == 0 or np.isnan(std):
        return np.full(row.shape, NAN)
    return (row - np.nanmean(row)) / std


def _preprocess(raw, mask, rows):
    """只在可选股票里去极值、标准化；rows 之外的行不计算（NaN）"""
    out = np.full(raw.shape, NAN)
    for i in rows:
        out[i] = _zscore(_winsorize(np.where(mask[i], raw[i], NAN)))
    return out


def _neutralize_size(z, size_z, rows):
    """逐日回归 因子 ~ 常数 + 标准化对数市值，取残差后重新标准化"""
    out = np.full(z.shape, NAN)
    for i in rows:
        y, s = z[i], size_z[i]
        ok = np.isfinite(y) & np.isfinite(s)
        if ok.sum() < 2 + 5:
            continue
        x = np.column_stack([np.ones(ok.sum()), s[ok]])
        x = x[:, x.any(axis=0)]
        beta = np.linalg.lstsq(x, y[ok], rcond=None)[0]
        res = np.full(y.shape, NAN)
        res[ok] = y[ok] - x.dot(beta)
        out[i] = _zscore(res)
    return out


def _rank(v):
    """平均秩（与 pandas rank 默认方式一致），从 1 开始"""
    order = np.argsort(v, kind="mergesort")
    sv = v[order]
    ranks = np.empty(len(v))
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return ranks


def _rank_ic(z, fwd, rows, min_names=10):
    ic = np.full(len(z), NAN)
    for i in rows:
        both = np.isfinite(z[i]) & np.isfinite(fwd[i])
        if both.sum() < min_names:
            continue
        a, b = _rank(z[i][both]), _rank(fwd[i][both])
        a, b = a - a.mean(), b - b.mean()
        den = math.sqrt(float((a * a).sum() * (b * b).sum()))
        if den > 0:
            ic[i] = float((a * b).sum()) / den
    return ic


def _rolling_1d(x, n, min_periods, fn):
    return _mrolling(np.asarray(x, dtype=float).reshape(-1, 1), n, min_periods, fn)[:, 0]


def sel_mask(data, member, is_st, ipo, dates, filters):
    """可选股票：当天是成分股、正常交易（成交量 > 0）、（可选）非 ST、上市满 N 个自然日"""
    vol = data["volume"]
    ok = np.asarray(member, dtype=bool) & np.where(np.isnan(vol), False, vol > 0)
    if filters.get("exclude_st", True):
        ok &= ~np.asarray(is_st, dtype=bool)
    days = int(filters.get("min_list_days", 250))
    if days:
        d = [datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10])) for s in dates]
        for j, s in enumerate(ipo):
            if s:
                born = datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
                ok[:, j] &= np.array([(x - born).days >= days for x in d])
    return ok


def sel_scores(spec, data, mask):
    """合成得分（只返回最后一行，即信号日 T）；任一因子缺失的股票为 NaN"""
    n = len(mask)
    ic_mode = sel_uses_ic(spec)
    rows = range(n) if ic_mode else [n - 1]
    neutral = spec.get("neutralize") or {}
    if neutral.get("industry"):
        raise ValueError("industry neutralization is not supported on platforms")
    size_z = None
    if neutral.get("size"):
        size_raw = np.where(mask, sel_factor("size", data), NAN)
        size_z = np.full(size_raw.shape, NAN)
        for i in rows:
            size_z[i] = _zscore(_winsorize(size_raw[i]))
    fwd = None
    if ic_mode:
        h = sel_horizon(spec)
        o = data["open"]
        fwd = _mshift(o, -(1 + h)) / _mshift(o, -1) - 1
        lookback = int(spec.get("ic_lookback", 252))
    total, wsum = None, None
    for f in spec["factors"]:
        z = _preprocess(sel_factor(f["key"], data), mask, rows)
        if size_z is not None and f["key"] != "size":
            z = _neutralize_size(z, size_z, rows)
        manual = float(f.get("weight", 1.0)) * f.get("direction", SEL_FACTORS[f["key"]][0])
        if ic_mode:
            known = _mshift(_rank_ic(z, fwd, rows).reshape(-1, 1), h + 1)[:, 0]
            minp = min(60, lookback)
            w = _rolling_1d(known, lookback, minp, _mean)
            if spec.get("weighting") == "icir":
                sd = _rolling_1d(known, lookback, minp, _std)
                w = w / np.where(sd == 0, NAN, sd)
            w = float(w[-1]) if not np.isnan(w[-1]) else manual
        else:
            w = manual
        term = z[-1] * w
        total = term if total is None else total + term
        wsum = abs(w) if wsum is None else wsum + abs(w)
    return total / wsum if wsum else np.full(mask.shape[1], NAN)


def sel_pick(spec, data, mask, codes, prev_picks=None):
    """信号日 T 选出的股票（得分从高到低）；可选股票不足 top_n 时返回 None（本期不调仓）"""
    score = sel_scores(spec, data, mask)
    top_n = int(spec["top_n"])
    keep = spec.get("keep_n")
    keep_n = max(top_n, int(keep)) if keep not in (None, "", 0) else top_n
    valid = [j for j in range(len(codes)) if np.isfinite(score[j])]
    if len(valid) < top_n:
        return None
    valid.sort(key=lambda j: -score[j])
    if keep_n > top_n and prev_picks:
        held = set(prev_picks)
        pool = [codes[j] for j in valid[:keep_n]]
        kept = [c for c in pool if c in held][:top_n]
        new = [c for c in pool if c not in held][:top_n - len(kept)]
        rank_pos = dict((c, i) for i, c in enumerate(pool))
        return sorted(kept + new, key=lambda c: rank_pos[c])
    return [codes[j] for j in valid[:top_n]]


# ============================== 调仓执行 ==============================
def sel_orders(target, book, market, state, position_pct):
    """
    执行日开盘的委托（与 SimpleQuant SelectionStrategy.next_open 相同）
    target：本次要调仓到的名单（None 表示今天不调仓，只重试之前卖不出的）
    book：{"cash": 可用资金, "value": 总资产, "pos": {代码: 股数}}
    market：{代码: {"open": 不复权开盘价, "can_buy": bool, "can_sell": bool}}
    state：跨交易日保存 {"retry": [卖不出、待重试的代码]}
    返回 [(代码, 股数变化, 原因)]，按执行顺序
    """
    out = []
    retry = set(state.get("retry", []))
    closing = set()
    for code in sorted(retry):
        if book["pos"].get(code, 0) <= 0:          # 已经不在持仓里
            retry.discard(code)
        elif market[code]["can_sell"]:
            out.append((code, -book["pos"][code], "重试卖出 retry"))
            retry.discard(code)
            closing.add(code)
    if target is not None:
        held = set(c for c, s in book["pos"].items() if s > 0) - closing
        freed = 0.0
        for code in sorted(held - set(target)):
            if code in retry:
                continue
            if market[code]["can_sell"]:
                freed += book["pos"][code] * market[code]["open"]
                out.append((code, -book["pos"][code], "落选 dropped"))
            else:
                retry.add(code)
        new = [c for c in target if c not in held]
        if new:
            per_name = book["value"] * position_pct / 100.0 / len(target)
            cash = book["cash"] + freed
            for code in new:
                if not market[code]["can_buy"]:
                    continue
                price = market[code]["open"]
                alloc = min(per_name, cash / (1 + CASH_BUFFER))
                lots = int(alloc / (price * (1 + CASH_BUFFER)) / LOT)
                if lots < 1:
                    continue
                out.append((code, lots * LOT, "入选 picked"))
                cash -= lots * LOT * price * (1 + CASH_BUFFER)
    state["retry"] = sorted(retry)
    return out


def sel_overlap(picks, expected):
    """与 SimpleQuant 名单的重合只数"""
    return len(set(picks) & set(expected)) if expected is not None else None


# ============================== 取数与选股流程（平台只提供 api） ==============================
def sel_select(spec, calendar, api, cache, prev_picks=None):
    """
    在信号日 T = calendar[-1] 收盘选股。api 由平台对接代码提供：
      api.members(day) -> 当天的指数成分股（平台代码）
      api.bars(codes, dates, fields, full) -> {字段: 二维数组(len(dates) × len(codes))}，价格为前复权，缺失为 NaN
                                           （full=False 时估值类字段只需要最后 25 行）
      api.st(codes, dates, full) -> 二维 bool（full=False 时只需要最后一行）
      api.ipo(codes) -> 上市日期列表（'YYYY-MM-DD'，未知为 None）
      api.index_close(dates) -> 股票池指数的收盘价（一维数组，用了大盘条件时才调用）
    cache：跨调仓日保存成分股查询结果（{日期: [代码]}）
    """
    dates = calendar[-sel_window(spec):]
    full = sel_uses_ic(spec)
    rows = dates if full else dates[-1:]
    for d in list(cache):
        if d < dates[0]:
            del cache[d]
    for d in rows:
        if d not in cache:
            cache[d] = sorted(api.members(d))
    codes = sorted(set(c for d in rows for c in cache[d]))
    if not codes:
        return None
    col = dict((c, j) for j, c in enumerate(codes))
    member = np.zeros((len(dates), len(codes)), dtype=bool)
    first = len(dates) - len(rows)
    for i, d in enumerate(rows):
        for c in cache[d]:
            member[first + i, col[c]] = True
    data = api.bars(codes, dates, sorted(sel_fields(spec)), full)
    for f in ("open", "close"):          # 有的平台停牌日没有数据：价格沿用停牌前的（与 SimpleQuant 面板相同）
        if f in data:
            data[f] = _ffill(data[f])
    filters = spec.get("filters") or {}
    is_st = np.zeros(member.shape, dtype=bool)
    if filters.get("exclude_st", True):
        is_st = np.asarray(api.st(codes, dates, full), dtype=bool)
    ipo = api.ipo(codes) if int(filters.get("min_list_days", 250)) else [None] * len(codes)
    mask = sel_mask(data, member, is_st, ipo, dates, filters)
    return sel_pick(spec, data, mask, codes, prev_picks=prev_picks)


def sel_target(spec, calendar, today, start, api, cache, expected, follow, log, state=None, total_value=None):
    """
    执行日 today 开盘前调用：返回要调仓到的名单，今天不调仓返回 None
    follow=True：不计算因子，直接按 SimpleQuant 回测的名单（expected）调仓
    """
    t = calendar[-1]
    st = state if isinstance(state, dict) else {}
    if follow:
        picks = expected.get(t)
        if picks:
            log("%s 按 SimpleQuant 名单调仓 / following SimpleQuant picks: %s" % (t, ", ".join(picks)))
        return picks
    back = False
    if sel_market_on(spec) and t >= start:
        first = sum(1 for d in calendar if d < start)
        cash = sel_market_cash(spec, api.index_close(calendar), first)
        was = len(calendar) > 1 and cash[-2]
        if cash[-1]:
            if was:
                return None              # 空仓期间不调仓（只重试之前卖不出的）
            st["last_picks"] = []
            log("%s 满足大盘空仓条件，清仓 / market condition: selling all" % t)
            return []
        back = bool(was)
        if back:
            log("%s 满足大盘恢复条件，重新选股 / market condition: back in" % t)

    # 净值回撤停手
    if uses_drawdown_stop(spec) and total_value is not None and t >= start:
        ds = spec.get("drawdown_stop")
        max_dd = (float(ds) / 100.0) if not isinstance(ds, dict) else (float(ds.get("max_drawdown", 0)) / 100.0)
        peak = st.get("dd_peak", 0.0)
        stopped = st.get("dd_stopped", False)
        if peak <= 0:
            peak = total_value
        elif total_value > peak and not stopped:
            peak = total_value
        st["dd_peak"] = peak
        dd = (peak - total_value) / peak if peak > 0 else 0.0
        if not stopped:
            if dd >= max_dd:
                st["dd_stopped"] = True
                st["last_picks"] = []
                log("%s 净值回撤达到 %.1f%%，触发回撤停手清仓 / drawdown stop-loss triggered" % (t, dd * 100))
                return []
        else:
            rec_hit = sel_recovery_hit(spec, api.index_close(calendar))
            if rec_hit[-1]:
                st["dd_stopped"] = False
                st["dd_peak"] = total_value
                log("%s 大盘满足恢复条件，解除回撤停手 / recovery condition met, stop-loss lifted" % t)
            else:
                return None              # 停手期间不调仓

    if st.get("dd_stopped"):
        return None                      # 仍处于回撤停手状态，不调仓

    if not back and not sel_is_rebalance(spec, calendar, today, start):
        return None
    if int(today[5:7]) in sel_skip_months(spec):
        st["last_picks"] = []
        log("%s 下个交易日在空仓月份，清仓 / skip month, selling all" % t)
        return []
    picks = sel_select(spec, calendar, api, cache, prev_picks=st.get("last_picks"))
    if picks is None:
        log("%s 可选股票不足 %d 只，本期不调仓 / not enough eligible stocks" % (t, int(spec["top_n"])))
        return None
    st["last_picks"] = list(picks)
    exp = expected.get(t)
    note = "" if exp is None else "；与 SimpleQuant 重合 %d/%d 只 (overlap)" % (sel_overlap(picks, exp), len(exp))
    log("%s 选股 / picks: %s%s" % (t, ", ".join(picks), note))
    return picks


def sel_fill(out, rows, date_key, code_key, mapping, pos, col):
    """把平台返回的长表（每行一个 日期 × 代码）填进二维数组；mapping: {我们的字段: 平台字段}"""
    for r in rows:
        i = pos.get(str(r[date_key])[:10])
        j = col.get(r[code_key])
        if i is None or j is None:
            continue
        for ours, theirs in mapping.items():
            v = r[theirs]
            if v is not None:
                try:
                    out[ours][i, j] = float(v)
                except (TypeError, ValueError):
                    pass
