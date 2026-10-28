"""
持仓诊断（选股页「持仓诊断」标签）：用户输入一组股票持仓，整理它和一个股票池（对照）相比的样子，不打分、不给建议

    - 集中度：各股市值占比、最大一只 / 前三只合计、有效持仓数 = 1 / Σ占比²（N 只等权 = N）、行业占比（当前行业分类）
    - 风格：总市值、市盈率（TTM）、市净率、股息率、ROE 在股票池当天成分股里的百分位，组合按市值占比加权平均；
      数值与选股因子同一口径（市值 = 不复权收盘价 × 已公告总股本，股息率同因子「股息率」）
    - 相关性：过去一年日收益（后复权，停牌日不计）的相关系数矩阵、两两平均、最高的几对
    - 假设持有一年：现在这些股数原样持有过去一年（后复权，分红再投资）的市值走势，与股票池指数比较；只说明组合的波动，
      不是真实收益（真实持仓期间买卖过、成本也不同）
面板：股票池面板（选股页共用，含当天全部成分股）只用来做横截面对照；持仓另建一个只含持仓股票的小面板（不必是成分股）
"""

import json
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .factors import dividend_yield
from ..engine.results import TRADING_DAYS
from ..paths import CACHE_DIR

HOLDINGS_FILE = CACHE_DIR / "holdings.json"
LOOKBACK_DAYS = 244          # 相关性、假设持有一年用的交易日数
MIN_CORR_DAYS = 120          # 两只股票共同交易日少于这么多不算相关系数
PANEL_YEARS = 2              # 面板往前取的年数：股息率要看最近 300 个交易日，相关性再要一年
STYLE = ["mcap", "pe", "pb", "dy", "roe"]


@dataclass
class Diagnosis:
    asof: pd.Timestamp
    holdings: pd.DataFrame          # 每只一行：code / name / shares / price / value / weight / industry / STYLE / STYLE_pct / stale
    pool_median: dict               # STYLE → 股票池当天的中位数
    style_pct: dict                 # STYLE → 组合按市值占比加权的平均百分位（只计有值的，占比重新归一）
    style_cover: dict               # STYLE → 有值的持仓占总市值的比例
    concentration: dict             # top1 / top3 / effective_n / n / industries
    industries: pd.DataFrame        # industry / weight / pool_weight（股票池按总市值）
    corr: pd.DataFrame | None       # 持仓两两相关系数（名称为行列）；不足两只时 None
    avg_corr: float
    top_pairs: list                 # [(名称a, 名称b, 相关系数)]，最高的三对
    curve: pd.DataFrame | None      # index=日期，value（持仓市值，元）/ benchmark（同起点的指数）/ drawdown
    perf: dict                      # portfolio / benchmark → {total_return, volatility, max_drawdown}；beta / corr
    pool_size: int = 0              # 对照日股票池里正常交易的成分股数
    bench_total_return: bool = False   # 基准是全收益指数（False：价格指数）
    short_history: bool = False     # 有持仓上市不满一年，假设持有的走势从最晚上市那只开始
    missing: list = field(default_factory=list)       # 本地没有日线的代码
    unpriced: list = field(default_factory=list)      # 有日线但截至对照日没有价格的（如对照日之后才上市）


# ================= 持仓输入 =================
def parse_text(text: str, basics: pd.DataFrame | None) -> tuple[list[dict], list[str]]:
    """
    从粘贴的文字里识别持仓（如券商软件复制的持仓表）：每行取第一个 6 位数代码（或股票名称），之后的第一个数是股数。
    没有代码也没有名称的行（表头等）跳过；有代码 / 名称却没有股数、或代码不在股票列表里的行返回给界面提示。
    同一只出现多次时股数相加
    """
    if basics is None or basics.empty:
        return [], [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    by6 = dict(zip(basics["code"].str.split(".").str[-1], basics["code"]))
    names = basics.dropna(subset=["name"]).sort_values("name", key=lambda s: -s.str.len())   # 先匹配长名称
    found, bad = {}, []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        code, rest = None, line
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", line)
        if m:
            code, rest = by6.get(m.group(1)), line[m.end():]
            if code is None:
                bad.append(line)
                continue
        else:
            for r in names.itertuples():
                pos = line.find(r.name)
                if pos >= 0:
                    code, rest = r.code, line[pos + len(r.name):]
                    break
        if code is None:
            continue
        if m:                                       # 代码后面紧跟名称时，名称里可能带数字（如「中证500ETF」）：先去掉
            nm = basics.loc[basics["code"] == code, "name"]
            if len(nm) and str(nm.iloc[0]) in rest:
                rest = rest[rest.find(str(nm.iloc[0])) + len(str(nm.iloc[0])):]
        num = re.search(r"\d[\d,]*(?:\.\d+)?", rest)
        shares = float(num.group(0).replace(",", "")) if num else 0
        if shares <= 0:
            bad.append(line)
            continue
        found[code] = found.get(code, 0) + shares
    return [{"code": c, "shares": s} for c, s in found.items()], bad


def from_account(state: dict) -> list[dict]:
    """模拟账户 state.json 的当前持仓里的股票（sh. / sz. / bj. 开头）；其他品种（ETF、可转债）不计"""
    out = {}
    for p in state.get("positions") or []:
        code, size = str(p.get("symbol", "")), float(p.get("size") or 0)
        if re.fullmatch(r"(sh|sz|bj)\.\d{6}", code) and size > 0:
            out[code] = out.get(code, 0) + size
    return [{"code": c, "shares": s} for c, s in out.items()]


def load_holdings() -> list[dict]:
    try:
        data = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        return [{"code": str(h["code"]), "shares": float(h["shares"])} for h in data if float(h["shares"]) > 0]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_holdings(holdings: list[dict]):
    HOLDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLDINGS_FILE.write_text(json.dumps(holdings, ensure_ascii=False, indent=1), encoding="utf-8")


# ================= 计算 =================
def panel_start(end: pd.Timestamp) -> str:
    return str((end - pd.DateOffset(years=PANEL_YEARS)).date())


def percentile(values: pd.Series, pool: pd.Series) -> pd.Series:
    """每个值在 pool（去掉空值）里的百分位：(更小的个数 + 相等个数 / 2) / 个数 × 100（同个股概览）"""
    v = np.sort(pool.dropna().to_numpy())
    if not len(v):
        return pd.Series(np.nan, index=values.index)
    lo = np.searchsorted(v, values.to_numpy(), side="left")
    hi = np.searchsorted(v, values.to_numpy(), side="right")
    return pd.Series(np.where(values.notna(), (lo + (hi - lo) / 2) / len(v) * 100, np.nan), index=values.index)


def _style_frame(panel, date) -> pd.DataFrame:
    """某天各股票的风格数值；没有财务 / 分红数据时那一列为空"""
    def last(name):
        f = panel.fields.get(name)
        return f.loc[date] if f is not None else pd.Series(np.nan, index=panel.codes)
    dy = dividend_yield(panel).loc[date] * 100
    return pd.DataFrame({"mcap": last("mcap"), "pe": last("pe"), "pb": last("pb"), "dy": dy, "roe": last("roe")})


def _style_pct(hold: pd.DataFrame, pool: pd.DataFrame) -> pd.DataFrame:
    """
    百分位都是「数值越大越高」。市盈率按盈利收益率（1 / 市盈率）倒过来排：亏损股（市盈率为负）算作最贵；
    市净率只比正值（净资产为负没有意义）
    """
    out = {}
    for k in ("mcap", "dy", "roe"):
        out[k] = percentile(hold[k], pool[k])
    ep = lambda s: 1 / s.where(s != 0)
    out["pe"] = 100 - percentile(ep(hold["pe"]), ep(pool["pe"]))
    pos = lambda s: s.where(s > 0)
    out["pb"] = percentile(pos(hold["pb"]), pos(pool["pb"]))
    return pd.DataFrame(out)


def _drawdown(s: pd.Series) -> float:
    return float((s / s.cummax() - 1).min())


def diagnose(holdings: list[dict], pool_panel, hold_panel) -> Diagnosis:
    """pool_panel：股票池面板（对照）；hold_panel：只含持仓股票的面板（build_panel(..., codes=)，同一交易日历）"""
    asof = pool_panel.calendar[-1]
    shares = pd.Series({h["code"]: float(h["shares"]) for h in holdings})
    have = [c for c in shares.index if hold_panel is not None and c in hold_panel.codes]
    missing = [c for c in shares.index if c not in have]
    names = dict(pool_panel.names)
    if hold_panel is not None:
        names.update(hold_panel.names)

    price = hold_panel["raw_close"].loc[asof, have] if have else pd.Series(dtype=float)
    unpriced = [c for c in have if not np.isfinite(price.get(c, np.nan))]
    have = [c for c in have if c not in unpriced]
    price = price[have]
    value = shares[have] * price
    total = float(value.sum())
    weight = value / total if total > 0 else value * np.nan

    # 最后一个交易日早于对照日：多半是这只没更新（也可能是长期停牌），提示更新
    traded = {c: hold_panel.tradable[c] for c in have}
    last_trade = {c: (s.index[s.to_numpy()][-1] if s.any() else None) for c, s in traded.items()}
    stale = {c: bool(not s.iloc[-20:].any()) for c, s in traded.items()}

    pool_codes = pool_panel.member.loc[asof]
    pool_codes = pool_codes[pool_codes & pool_panel.tradable.loc[asof]].index
    pool_style = _style_frame(pool_panel, asof).loc[pool_codes]
    hold_style = _style_frame(hold_panel, asof).loc[have] if have else pd.DataFrame(columns=STYLE)
    pct = _style_pct(hold_style, pool_style) if have else pd.DataFrame(columns=STYLE)

    industry = hold_panel.industry.reindex(have).fillna("") if have else pd.Series(dtype=str)
    table = pd.DataFrame({"code": have, "name": [names.get(c, c) for c in have], "shares": shares[have].values,
                          "price": price.values, "value": value.values, "weight": weight.values,
                          "industry": industry.values, "stale": [stale[c] for c in have],
                          "last_trade": [last_trade[c] for c in have]})
    for k in STYLE:
        table[k] = hold_style[k].values if have else []
        table[f"{k}_pct"] = pct[k].values if have else []
    table = table.sort_values("value", ascending=False).reset_index(drop=True)

    style_pct, style_cover = {}, {}
    for k in STYLE:
        ok = table[f"{k}_pct"].notna()
        w = table.loc[ok, "weight"]
        style_cover[k] = float(w.sum()) if len(table) else 0.0
        style_pct[k] = float((table.loc[ok, f"{k}_pct"] * w).sum() / w.sum()) if w.sum() > 0 else np.nan

    w = table["weight"]
    ind = table.groupby("industry")["weight"].sum().sort_values(ascending=False)
    pool_ind = pool_panel.industry.reindex(pool_codes).fillna("")
    pool_cap = pool_style["mcap"]
    pool_w = (pool_cap.groupby(pool_ind).sum() / pool_cap.sum()) if pool_cap.notna().any() and pool_cap.sum() > 0 \
        else pool_ind.value_counts(normalize=True)
    industries = pd.DataFrame({"industry": ind.index, "weight": ind.values,
                               "pool_weight": pool_w.reindex(ind.index).fillna(0).values})
    concentration = {"n": len(table), "top1": float(w.iloc[:1].sum()), "top3": float(w.iloc[:3].sum()),
                     "effective_n": float(1 / (w ** 2).sum()) if len(w) else np.nan,
                     "industries": int((ind > 0).sum())}

    corr, avg_corr, pairs = None, np.nan, []
    curve, perf, short = None, {}, False
    if have:
        win = hold_panel.calendar[-LOOKBACK_DAYS - 1:]
        close = hold_panel["close"].loc[win, have]
        rets = close.pct_change().where(hold_panel.tradable.loc[win, have]).iloc[1:]
        if len(have) >= 2:
            c = rets.corr(min_periods=MIN_CORR_DAYS)
            label = {code: names.get(code, code) for code in have}
            corr = c.rename(index=label, columns=label).loc[table["name"], table["name"]]
            iu = np.triu_indices(len(have), 1)
            vals = c.to_numpy()[iu]
            avg_corr = float(np.nanmean(vals)) if np.isfinite(vals).any() else np.nan
            order = [i for i in np.argsort(-np.nan_to_num(vals, nan=-9)) if np.isfinite(vals[i])][:3]
            pairs = [(label[have[iu[0][i]]], label[have[iu[1][i]]], float(vals[i])) for i in order]

        # 假设现在的股数一直持有：每只 = 今天的市值 × 后复权价 / 今天的后复权价；从所有持仓都有价格的那天开始
        start = close.apply(lambda s: s.first_valid_index()).max()
        short = start > close.index[0]
        if start is not None and start < asof:
            sub = close.loc[start:].ffill()
            port = (sub / sub.iloc[-1] * value[have]).sum(axis=1)
            bench = pool_panel.benchmark.reindex(sub.index).ffill()
            curve = pd.DataFrame({"value": port, "benchmark": bench / bench.iloc[0] * port.iloc[0]})
            curve["drawdown"] = curve["value"] / curve["value"].cummax() - 1
            for k, col in (("portfolio", "value"), ("benchmark", "benchmark")):
                s = curve[col]
                perf[k] = {"total_return": float(s.iloc[-1] / s.iloc[0] - 1),
                           "volatility": float(s.pct_change().std() * np.sqrt(TRADING_DAYS)),
                           "max_drawdown": _drawdown(s)}
            rp, rb = curve["value"].pct_change().dropna(), curve["benchmark"].pct_change().dropna()
            perf["beta"] = float(rp.cov(rb) / rb.var()) if rb.var() > 0 else np.nan
            perf["corr"] = float(rp.corr(rb))

    return Diagnosis(asof=asof, holdings=table, pool_median={k: float(pool_style[k].median()) for k in STYLE},
                     style_pct=style_pct, style_cover=style_cover, concentration=concentration,
                     industries=industries, corr=corr, avg_corr=avg_corr, top_pairs=pairs, curve=curve,
                     perf=perf, pool_size=len(pool_codes), bench_total_return=pool_panel.bench_total_return,
                     short_history=bool(short), missing=missing, unpriced=unpriced)


def build_hold_panel(store, universe: str, codes: list[str], start: str, end: str):
    from .panel import build_panel
    have = [c for c in codes if store.has(c)]
    return build_panel(store, universe, start, end, codes=have) if have else None


def download(store, codes: list[str], start: str, end: str) -> dict[str, str]:
    """只下载这几只的日线、财务、分红（不含排雷数据）；财务从开始日的前一年起。返回出错的部分"""
    year = int(start[:4]) - 1
    errors = dict(store.update(codes, start, end))
    errors.update(store.update_fundamentals(codes, year))
    errors.update(store.update_dividends(codes, year))
    return errors


def requests_needed(store, codes: list[str], start: str, end: str) -> int:
    """download 大约要多少次 BaoStock 请求（同 overview.requests_needed，不含排雷）"""
    year = int(start[:4]) - 1
    n = 2 * len(store.plan(codes, start, end))
    n += sum(len(j[0]) for j in store.plan_fundamentals(codes, year).values())
    n += sum(len(j[0]) for j in store.plan_dividends(codes, year).values())
    return n
