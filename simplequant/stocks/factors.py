"""
横截面因子库（选股用）

每个因子是 Panel → DataFrame(日期 × 股票) 的函数，T 日的值只用 T 日收盘及以前的数据。
direction：默认方向，+1 表示"越大越好"，-1 表示"越小越好"（研究页可以验证、选股时可以改）。
预处理：按日在"可选股票"内做 MAD 去极值 + z-score 标准化。
"""

import numpy as np
import pandas as pd

from ..i18n import L

GROUPS = {
    "value": L("价值", "Value"),
    "quality": L("质量", "Quality"),
    "growth": L("成长", "Growth"),
    "size": L("规模", "Size"),
    "momentum": L("动量/反转", "Momentum / reversal"),
    "risk": L("风险", "Risk"),
    "liquidity": L("流动性", "Liquidity"),
    "cb_value": L("转债估值", "CB valuation"),
    "cb_terms": L("转债规模与期限", "CB size & term"),
    "cb_stock": L("正股", "Underlying stock"),
}


def _field(name):
    """财务字段：面板里没有（未下载财务数据）时返回全 NaN"""
    def fn(p):
        f = p.fields.get(name)
        return f if f is not None else p["close"] * np.nan
    return fn


def _inv(x: pd.DataFrame, positive_only: bool) -> pd.DataFrame:
    x = x.where(x != 0)
    if positive_only:
        x = x.where(x > 0)
    return 1 / x


def _ret(p, n):
    return p["close"] / p["close"].shift(n) - 1


FACTORS = {
    "ep": {"label": L("盈利收益率 EP (1/PE)", "Earnings yield (1/PE)"), "group": "value", "direction": 1,
           "fn": lambda p: _inv(p["pe"], positive_only=False),
           "desc": L("用 1/PE 而不是 PE，亏损股为负、排在最后", "1/PE rather than PE, so loss-makers rank last")},
    "bp": {"label": L("账面市值比 BP (1/PB)", "Book-to-price (1/PB)"), "group": "value", "direction": 1,
           "fn": lambda p: _inv(p["pb"], positive_only=True)},
    "sp": {"label": L("营收市值比 SP (1/PS)", "Sales-to-price (1/PS)"), "group": "value", "direction": 1,
           "fn": lambda p: _inv(p["ps"], positive_only=True)},
    # ---- 财务因子（需要下载财务数据；按首次公告日对齐） ----
    "roe": {"label": L("ROE（年化）", "ROE (annualized)"), "group": "quality", "direction": 1, "fn": _field("roe"),
            "requires_fin": True, "desc": L("累计 ROE 按季度折算成年化值", "year-to-date ROE scaled to a full year")},
    "gross_margin": {"label": L("毛利率", "Gross margin"), "group": "quality", "direction": 1,
                     "fn": _field("gross_margin"), "requires_fin": True,
                     "desc": L("银行、保险公司无毛利率，使用该因子时将被排除", "banks and insurers have none and are excluded")},
    "net_margin": {"label": L("净利率", "Net margin"), "group": "quality", "direction": 1,
                   "fn": _field("net_margin"), "requires_fin": True},
    "np_yoy": {"label": L("净利润同比增长", "Net profit growth (YoY)"), "group": "growth", "direction": 1,
               "fn": _field("np_yoy"), "requires_fin": True},
    "rev_yoy": {"label": L("营收同比增长", "Revenue growth (YoY)"), "group": "growth", "direction": 1,
                "fn": _field("rev_yoy"), "requires_fin": True,
                "desc": L("营收只在半年报和年报披露", "revenue is only disclosed in interim and annual reports")},
    "size": {"label": L("总市值（对数）", "Market cap (log)"), "group": "size", "direction": -1,
             "fn": lambda p: np.log(_field("mcap")(p).where(_field("mcap")(p) > 0)), "requires_fin": True,
             "desc": L("不复权价 × 当时已公告的总股本；A 股长期有小市值效应", "raw price × shares outstanding as last reported; A-shares have a small-cap effect")},
    "ret5": {"label": L("5日收益", "5-day return"), "group": "momentum", "direction": -1, "fn": lambda p: _ret(p, 5),
             "desc": L("A 股短期常见反转效应，默认越小越好", "A-shares often show short-term reversal; lower is better by default")},
    "ret20": {"label": L("20日收益", "20-day return"), "group": "momentum", "direction": -1, "fn": lambda p: _ret(p, 20)},
    "ret60": {"label": L("60日收益", "60-day return"), "group": "momentum", "direction": -1, "fn": lambda p: _ret(p, 60),
              "desc": L("沪深300 2020–2026 实测 IC 为负（偏反转，但不显著）", "negative (reversal) IC on CSI 300 2020–2026, not significant")},
    "mom_120_20": {"label": L("中期动量（120日，跳过最近20日）", "Medium-term momentum (120d, skip last 20d)"),
                   "group": "momentum", "direction": 1,
                   "fn": lambda p: p["close"].shift(20) / p["close"].shift(120) - 1},
    "vol60": {"label": L("60日波动率", "60-day volatility"), "group": "risk", "direction": -1,
              "fn": lambda p: p["close"].pct_change(fill_method=None).rolling(60, min_periods=40).std(),
              "desc": L("低波动异象：波动小的股票长期表现往往更好", "low-volatility anomaly")},
    "turn20": {"label": L("20日平均换手率", "20-day avg turnover"), "group": "liquidity", "direction": -1,
               "fn": lambda p: p["turnover"].rolling(20, min_periods=15).mean(),
               "desc": L("A 股低换手往往对应更好的后续收益", "low turnover tends to do better in A-shares")},
    "amihud20": {"label": L("非流动性 Amihud（20日）", "Amihud illiquidity (20d)"), "group": "liquidity", "direction": 1,
                 "fn": lambda p: (p["close"].pct_change(fill_method=None).abs() / p["amount"].where(p["amount"] > 0))
                 .rolling(20, min_periods=15).mean() * 1e8,
                 "desc": L("每 1 亿元成交带来的价格变动；越大越不流动", "price impact per 100M CNY traded; higher = less liquid")},
}


# 价格类因子股票和可转债通用；其余只适用于股票（"assets" 未写时）
BOTH = ("stock", "cb")
for _k in ("ret5", "ret20", "ret60", "mom_120_20", "vol60", "amihud20"):
    FACTORS[_k]["assets"] = BOTH

FACTORS.update({
    "cb_double_low": {"label": L("双低（价格 + 转股溢价率）", "Double-low (price + premium)"), "group": "cb_value",
                      "direction": -1, "fn": _field("double_low"), "assets": ("cb",),
                      "desc": L("不复权收盘价 + 转股溢价率（%），越低越好：价格低有债底保护，溢价率低跟涨正股。"
                                "2018–2026 实测 20 日 IC −0.058（t = −3.3），五组年化收益从低到高单调",
                                "raw close + conversion premium (%); lower = bond floor plus equity upside. 2018–2026: "
                                "20-day IC −0.058 (t = −3.3), quintile returns perfectly monotonic")},
    "cb_price": {"label": L("转债价格", "Bond price"), "group": "cb_value", "direction": -1,
                 "fn": _field("raw_close"), "assets": ("cb",), "desc": L("不复权收盘价", "raw close")},
    "cb_premium": {"label": L("转股溢价率（%）", "Conversion premium (%)"), "group": "cb_value", "direction": -1,
                   "fn": _field("premium"), "assets": ("cb",),
                   "desc": L("转债价格 / 转股价值 − 1；越低越接近正股", "price / conversion value − 1")},
    "cb_bond_premium": {"label": L("纯债溢价率（%）", "Premium over bond floor (%)"), "group": "cb_value",
                        "direction": -1, "fn": _field("bond_premium"), "assets": ("cb",),
                        "desc": L("转债价格 / 纯债价值 − 1；越低下跌空间越小", "price / bond floor − 1")},
    "cb_conv_value": {"label": L("转股价值", "Conversion value"), "group": "cb_value", "direction": 1,
                      "fn": _field("conv_value"), "assets": ("cb",),
                      "desc": L("100 / 转股价 × 正股价", "100 / conversion price × stock price")},
    "cb_issue_size": {"label": L("发行规模（对数）", "Issue size (log)"), "group": "cb_terms", "direction": -1,
                      "fn": lambda p: np.log(_field("issue_size")(p).where(_field("issue_size")(p) > 0)),
                      "assets": ("cb",),
                      "desc": L("发行时的规模；剩余规模只有部分转债有历史数据，因此用发行规模",
                                "size at issue (remaining size history is incomplete)")},
    "cb_remain_years": {"label": L("剩余期限（年）", "Years to maturity"), "group": "cb_terms", "direction": 1,
                        "fn": _field("remain_years"), "assets": ("cb",)},
    "cb_stock_ret20": {"label": L("正股 20 日涨幅", "Stock 20-day return"), "group": "cb_stock", "direction": 1,
                       "fn": lambda p: _field("stock_close")(p) / _field("stock_close")(p).shift(20) - 1,
                       "assets": ("cb",),
                       "desc": L("正股价由转股价值 × 转股价 / 100 推出（不复权）",
                                 "stock price implied by conversion value × conversion price / 100 (unadjusted)")},
    "cb_stock_vol60": {"label": L("正股 60 日波动率", "Stock 60-day volatility"), "group": "cb_stock",
                       "direction": -1, "assets": ("cb",),
                       "fn": lambda p: _field("stock_close")(p).pct_change(fill_method=None)
                       .rolling(60, min_periods=40).std(),
                       "desc": L("正股波动大，转债的期权价值高；但 2018–2026 实测 IC 为负（−0.071，t = −3.0），默认越小越好",
                                 "more volatility means more option value, yet 2018–2026 IC is negative (−0.071, t = −3.0); "
                                 "lower is better by default")},
})


def factor_assets(key: str) -> tuple:
    return FACTORS.get(key, {}).get("assets", ("stock",))


def factors_for(kind: str) -> list[str]:
    """适用于该品种（stock / cb）的因子"""
    return [k for k in FACTORS if kind in factor_assets(k)]


def compute(panel, key: str) -> pd.DataFrame:
    if key not in FACTORS:
        raise ValueError(f"因子「{key}」不存在（自定义因子可能已被删除） / Unknown factor {key} (a custom factor may have been deleted)")
    return FACTORS[key]["fn"](panel).replace([np.inf, -np.inf], np.nan)


def winsorize_mad(df: pd.DataFrame, k: float = 5.0) -> pd.DataFrame:
    """按行（每天）把超出 中位数 ± k×1.4826×MAD 的值截断"""
    med = df.median(axis=1)
    mad = df.sub(med, axis=0).abs().median(axis=1) * 1.4826
    lo, hi = med - k * mad, med + k * mad
    return df.clip(lower=lo, upper=hi, axis=0)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    std = df.std(axis=1).replace(0, np.nan)
    return df.sub(df.mean(axis=1), axis=0).div(std, axis=0)


def preprocess(raw: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """只在可选股票里去极值、标准化；其余设为 NaN"""
    return zscore(winsorize_mad(raw.where(mask)))


def group_small_industries(industry: pd.Series, min_size: int = 3) -> pd.Series:
    """成员太少的行业合并为"其他"，避免回归时一个行业只有一两只股票"""
    ind = industry.fillna("未分类")
    counts = ind.value_counts()
    return ind.where(ind.map(counts) >= min_size, "其他")


def neutralize(z: pd.DataFrame, industry: pd.Series | None = None, size: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    逐日横截面回归：因子 ~ 行业哑变量 (+ 标准化对数市值)，取残差后重新标准化。
    这样选出来的股票不会只是"某个行业"或"大/小盘"的替身。
    """
    codes = z.columns
    if industry is not None and len(industry.dropna()):
        ind = group_small_industries(industry.reindex(codes))
        dummies = pd.get_dummies(ind).reindex(codes).fillna(False).values.astype(float)
    else:
        dummies = np.ones((len(codes), 1))
    zs = zscore(winsorize_mad(size)).reindex(index=z.index, columns=codes).values if size is not None else None
    Z = z.values
    out = np.full_like(Z, np.nan, dtype=float)
    for i in range(len(Z)):
        y = Z[i]
        ok = np.isfinite(y)
        X = dummies
        if zs is not None:
            ok &= np.isfinite(zs[i])
            X = np.column_stack([dummies, zs[i]])
        if ok.sum() < X.shape[1] + 5:
            continue
        Xo = X[ok]
        Xo = Xo[:, Xo.any(axis=0)]          # 去掉当天没有成员的行业列
        beta, *_ = np.linalg.lstsq(Xo, y[ok], rcond=None)
        out[i, ok] = y[ok] - Xo @ beta
    return zscore(pd.DataFrame(out, index=z.index, columns=codes))


def factor_zscores(panel, key: str, mask: pd.DataFrame, neutral: dict | None = None) -> pd.DataFrame:
    """单个因子：计算 → 去极值、标准化 →（可选）行业/市值中性化"""
    z = preprocess(compute(panel, key), mask)
    neutral = neutral or {}
    if neutral.get("industry") or neutral.get("size"):
        size_key = getattr(panel, "size_factor", "size")
        size = compute(panel, size_key).where(mask) if neutral.get("size") and key != size_key else None
        ind = panel.industry if neutral.get("industry") else None
        if ind is not None or size is not None:
            z = neutralize(z, ind, size)
    return z


def daily_rank_ic(z: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 10) -> pd.Series:
    from .research import rank_ic
    return rank_ic(z, fwd, min_names)


def ic_weights(ic: pd.Series, horizon: int, lookback: int, mode: str) -> pd.Series:
    """
    IC 加权：T 日可用的权重只用 T 日已经完全实现的 IC
    （IC[d] 用到 d+1+h 日开盘价，所以向后平移 h+1 天），取过去 lookback 个交易日的均值（ic）或均值/标准差（icir）
    """
    known = ic.shift(horizon + 1)
    mean = known.rolling(lookback, min_periods=min(60, lookback)).mean()
    if mode == "icir":
        std = known.rolling(lookback, min_periods=min(60, lookback)).std()
        return mean / std.replace(0, np.nan)
    return mean


def composite(panel, factors: list[dict], mask: pd.DataFrame, weighting: str = "manual", neutral: dict | None = None,
              horizon: int = 20, lookback: int = 252, cache: dict | None = None) -> pd.DataFrame:
    """
    多因子合成（任一因子缺失的股票不参与）：
    - manual：Σ 权重 × 方向 × 标准化因子 / Σ|权重|
    - ic / icir：权重 = 该因子过去 lookback 天的平均 IC（或 ICIR），方向由 IC 的符号自动决定；
      历史不足时回退到手动权重和方向
    cache：同一面板、同一 mask 下多次调用时共用（参数优化），标准化因子和每日 IC 只算一次
    """
    from .research import forward_returns
    cache = {} if cache is None else cache
    nkey = (bool((neutral or {}).get("industry")), bool((neutral or {}).get("size")))

    def cached(key, fn):
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    use_ic = weighting in ("ic", "icir")
    total, wsum = None, None
    for f in factors:
        z = cached(("z", f["key"], nkey), lambda: factor_zscores(panel, f["key"], mask, neutral))
        manual = float(f.get("weight", 1.0)) * f.get("direction", FACTORS.get(f["key"], {}).get("direction", 1))
        if use_ic:
            fwd = cached(("fwd", horizon), lambda: forward_returns(panel, horizon))
            ic = cached(("ic", f["key"], nkey, horizon), lambda: daily_rank_ic(z, fwd))
            w = ic_weights(ic, horizon, lookback, weighting).fillna(manual)
        else:
            w = pd.Series(manual, index=z.index)
        term = z.mul(w, axis=0)
        total = term if total is None else total + term
        wsum = w.abs() if wsum is None else wsum + w.abs()
    return total.div(wsum.replace(0, np.nan), axis=0)


def factor_correlation(panel, keys: list[str], mask: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """因子之间的平均横截面秩相关（相关性高的因子叠加意义不大）"""
    ranks = {k: preprocess(compute(panel, k), mask).loc[dates].rank(axis=1) for k in keys}
    out = pd.DataFrame(np.eye(len(keys)), index=keys, columns=keys)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ra, rb = ranks[a], ranks[b]
            both = ra.notna() & rb.notna()
            ra, rb = ra.where(both), rb.where(both)
            ca, cb = ra.sub(ra.mean(axis=1), axis=0), rb.sub(rb.mean(axis=1), axis=0)
            c = (ca * cb).sum(axis=1) / np.sqrt((ca ** 2).sum(axis=1) * (cb ** 2).sum(axis=1))
            out.loc[a, b] = out.loc[b, a] = float(c.mean())
    return out
