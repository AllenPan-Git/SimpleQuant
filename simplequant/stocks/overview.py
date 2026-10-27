"""
个股概览（选股页「个股概览」标签、选股结果里点开某只股票）：只整理这只股票自己的数据，不打分、不下结论

    - 估值：市盈率（TTM）/ 市净率逐日走势，当前值在本地全部历史里的百分位（与择时条件「市盈率历史百分位」同一算法；
      只用正值：亏损时市盈率为负，没有百分位的意义）
    - 财务：年报的 ROE、毛利率、净利率、净利润 / 营收同比，加最近一期非年报（ROE 按季度年化，同选股因子）
    - 分红：逐年的每股现金分红、送转与股息率；BaoStock 分红表漏记的按复权因子补上（同选股回测，见 dividends.py）
    - 排雷：资产负债率、流动比率、经营现金流 / 净利润（需要单独下载排雷数据）
数据全部来自本地 StockStore；缺哪一部分就在那一部分说明，不影响其他部分
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .dividends import with_missing
from .fundamentals import derive, derive_risk

MIN_PCT_DAYS = 244          # 有效值不到约一年时不算百分位（同 strategies.factors.Percentile 的 min_count）
FIN_COLUMNS = ["roe", "gross_margin", "net_margin", "np_yoy", "rev_yoy"]
RISK_COLUMNS = ["debt_ratio", "current_ratio", "cfo_np"]


@dataclass
class Overview:
    code: str
    name: str = ""
    industry: str = ""
    ipo_date: pd.Timestamp | None = None
    out_date: pd.Timestamp | None = None
    last_date: pd.Timestamp | None = None     # 日线最后一天；None = 没有日线
    close: float = np.nan                     # 不复权收盘价
    is_st: bool = False
    mcap: float = np.nan                      # 总市值（元）= 收盘价 × 最近一期总股本
    valuation: pd.DataFrame | None = None     # index=日期，pe / pb
    pct: dict = field(default_factory=dict)   # "pe" / "pb" → {"value", "pct", "since", "days"}；没有百分位时 pct 为 NaN
    fin: pd.DataFrame | None = None           # 每行一个报告期：stat_date / pub_date / annual / FIN_COLUMNS
    dividends: pd.DataFrame | None = None     # 每年一行：year / times / cash / bonus / reserve / yield
    div_patched: int = 0                      # 其中按复权因子补记的次数
    risk: pd.DataFrame | None = None          # 同 fin：stat_date / pub_date / annual / RISK_COLUMNS
    missing: list = field(default_factory=list)   # 本地没有的部分："daily" / "fin" / "div" / "risk"


def find_stocks(basics: pd.DataFrame, text: str, limit: int = 10) -> pd.DataFrame:
    """按代码（600519、sh.600519）或名称（含部分）查找；精确匹配排在前面"""
    text = (text or "").strip()
    if not text or basics is None or basics.empty:
        return basics.iloc[:0] if basics is not None else pd.DataFrame(columns=["code", "name"])
    low = text.lower()
    code6 = basics["code"].str.split(".").str[-1]
    if "." in low:
        hit = basics[basics["code"].str.lower() == low]
    elif low.isdigit():
        hit = basics[code6.str.startswith(low)]
        hit = hit.assign(_k=(code6[hit.index] != low)).sort_values(["_k", "code"]).drop(columns="_k")
    else:
        names = basics["name"].fillna("")
        hit = basics[names.str.contains(text, regex=False)]
        hit = hit.assign(_k=(names[hit.index] != text)).sort_values(["_k", "code"]).drop(columns="_k")
    return hit.head(limit)


def percentile(series: pd.Series) -> dict:
    """当前值（最后一个有效值）在全部历史正值里的百分位：(更小的个数 + 相等个数 / 2) / 有效个数 × 100"""
    s = series.dropna()
    out = {"value": float(s.iloc[-1]) if len(s) else np.nan, "pct": np.nan, "since": None, "days": 0}
    pos = s[s > 0]
    out["days"] = len(pos)
    if len(pos):
        out["since"] = pos.index[0]
    cur = out["value"]
    if len(pos) >= MIN_PCT_DAYS and cur > 0:
        v = pos.to_numpy()
        out["pct"] = float(((v < cur).sum() + (v == cur).sum() / 2) / len(v) * 100)
    return out


def _reports(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """年报 + 比最后一期年报更新的那一期（如三季报）；annual 标明是否年报"""
    if df is None or df.empty:
        return pd.DataFrame(columns=["stat_date", "pub_date", "annual"] + columns)
    df = df.sort_values("stat_date")
    annual = df["stat_date"].dt.month == 12
    keep = annual.copy()
    if not annual.iloc[-1]:
        keep.iloc[-1] = True
    out = df.loc[keep, ["stat_date", "pub_date"] + columns].copy()
    out.insert(2, "annual", annual[keep].to_numpy())
    return out.reset_index(drop=True)


def financials(fin: pd.DataFrame) -> pd.DataFrame:
    return _reports(derive(fin) if fin is not None and len(fin) else None, FIN_COLUMNS)


def risk_reports(risk: pd.DataFrame) -> pd.DataFrame:
    return _reports(derive_risk(risk) if risk is not None and len(risk) else None, RISK_COLUMNS)


def dividend_years(div: pd.DataFrame, daily: pd.DataFrame | None, rights: list | None) -> tuple[pd.DataFrame, int]:
    """
    逐年汇总（按除权除息日所在年份）。股息率 = Σ 每次的每股现金 / 除息前一个交易日的不复权收盘价
    （送转前后股本不同，所以逐次算再相加）；除息日早于日线的记空。返回 (表, 补记的次数)
    """
    patched = 0
    if daily is not None and len(daily):
        div, extra = with_missing(div, daily, rights)
        patched = len(extra)
    cols = ["year", "times", "cash", "bonus", "reserve", "yield"]
    if div is None or div.empty:
        return pd.DataFrame(columns=cols), patched
    ev = div.sort_values("ex_date").copy()
    ev["yield"] = np.nan
    if daily is not None and len(daily):
        close = daily["raw_close"].dropna()
        pos = close.index.searchsorted(pd.DatetimeIndex(ev["ex_date"]), side="left") - 1   # 除息前一个交易日
        ok = (pos >= 0) & (pd.DatetimeIndex(ev["ex_date"]) > close.index[0])
        ev.loc[ok, "yield"] = ev.loc[ok, "cash"].to_numpy() / close.to_numpy()[pos[ok]] * 100
    ev["year"] = ev["ex_date"].dt.year
    g = ev.groupby("year")
    out = pd.DataFrame({"times": g.size(), "cash": g["cash"].sum(), "bonus": g["bonus"].sum(),
                        "reserve": g["reserve"].sum(),
                        "yield": g["yield"].sum(min_count=1).where(g["yield"].count() == g.size())})
    return out.reset_index()[cols], patched


def build(store, code: str) -> Overview:
    """读本地数据，整理成 Overview；任何一部分缺失只记入 missing"""
    ov = Overview(code=code)
    try:
        basics = store.load_basics()
    except (OSError, ValueError):
        basics = None
    if basics is not None:
        row = basics[basics["code"] == code]
        if len(row):
            r = row.iloc[0]
            ov.name, ov.ipo_date = str(r["name"]), r["ipo_date"]
            ov.out_date = r["out_date"] if pd.notna(r["out_date"]) else None
    ind = store.load_industry()
    ov.industry = str(ind.get(code, "")) if len(ind) else ""

    daily = None
    if store.has(code):
        daily = store.load(code)
        ov.last_date = daily.index[-1]
        last = daily.iloc[-1]
        ov.close, ov.is_st = float(last["raw_close"]), bool(last.get("is_st", 0))
        ov.valuation = daily[["pe", "pb"]]
        ov.pct = {k: percentile(daily[k]) for k in ("pe", "pb")}
    else:
        ov.missing.append("daily")

    if store.has_fin(code):
        fin = store.load_fin(code)
        ov.fin = financials(fin)
        share = fin.dropna(subset=["total_share"]).sort_values("stat_date")["total_share"]
        if len(share) and np.isfinite(ov.close):
            ov.mcap = ov.close * float(share.iloc[-1])
    else:
        ov.missing.append("fin")

    if store.has_div(code):
        rights = store.load_rights()
        ov.dividends, ov.div_patched = dividend_years(store.load_div(code), daily,
                                                      None if rights is None else rights.get(code, []))
    else:
        ov.missing.append("div")

    if store.has_risk(code):
        ov.risk = risk_reports(store.load_risk(code))
    else:
        ov.missing.append("risk")
    return ov


def requests_needed(store, code: str, start: str, end: str) -> int:
    """download 大约要多少次 BaoStock 请求（日线每只 2 次、财务每季度 1 次、分红每年 1 次、排雷每季度 2 次）"""
    from .fundamentals import RISK_REQUESTS_PER_QUARTER
    year = int(start[:4]) - 1
    n = 2 if store.plan([code], start, end) else 0
    n += sum(len(j[0]) for j in store.plan_fundamentals([code], year).values())
    n += sum(len(j[0]) for j in store.plan_dividends([code], year).values())
    n += sum(len(j[0]) for j in store.plan_risk([code], year).values()) * RISK_REQUESTS_PER_QUARTER
    return n


def download(store, code: str, start: str, end: str, with_risk: bool = True) -> dict[str, str]:
    """只下载这一只（日线 + 财务 + 分红，可选排雷）；财务从开始日的前一年起（同数据标签页）。返回出错的部分"""
    year = int(start[:4]) - 1
    errors = dict(store.update([code], start, end))
    errors.update(store.update_fundamentals([code], year))
    errors.update(store.update_dividends([code], year))
    if with_risk:
        errors.update(store.update_risk([code], year))
    return errors
