"""
财务数据（BaoStock 季频盈利能力数据），严格按首次公告日对齐，避免未来数据

为什么不用 AKShare 业绩报表：实测其"最新公告日期"会随后续报告更新（沪深300 各期 100% 晚于法定截止日），
数值也可能是后来修订过的，用于回测会引入未来信息。BaoStock 的 pubDate 是首次公告日。

存储：data_cache/stocks/fin/<code>.parquet，每行一个报告期：
    stat_date 报告期 / pub_date 公告日 / roe_avg / gp_margin / np_margin / net_profit / revenue / eps_ttm / total_share
对齐规则：
    - 公告日的下一个自然日起可用（公告常在盘后发布），再对齐到交易日历（取不晚于当天的最新值）
    - 每个字段单独对齐：用"已公告、且该字段有值"的最新报告期（营收只有半年报/年报才有）
    - 旧报告期若在新报告期之后才公告（补发/更正），不会覆盖较新的报告期
    - 报告期距今超过 15 个月视为过期（NaN）
"""

import datetime as dt

import numpy as np
import pandas as pd

PROFIT_COLUMNS = {"statDate": "stat_date", "pubDate": "pub_date", "roeAvg": "roe_avg", "gpMargin": "gp_margin",
                  "npMargin": "np_margin", "netProfit": "net_profit", "MBRevenue": "revenue", "epsTTM": "eps_ttm",
                  "totalShare": "total_share"}
STALE_DAYS = 460


def quarters(start_year: int, today: dt.date | None = None) -> list[tuple[int, int]]:
    """从 start_year Q1 到当前已结束的最近一个季度"""
    today = today or dt.date.today()
    last_q = (today.month - 1) // 3          # 当前季度之前的完整季度数
    end = (today.year, last_q) if last_q else (today.year - 1, 4)
    return [(y, q) for y in range(start_year, end[0] + 1) for q in (1, 2, 3, 4) if (y, q) <= end]


def process_profit(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=list(PROFIT_COLUMNS.values()))
    df = raw.rename(columns=PROFIT_COLUMNS)[list(PROFIT_COLUMNS.values())].copy()
    for c in ("stat_date", "pub_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    num = [c for c in df.columns if c not in ("stat_date", "pub_date")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["stat_date", "pub_date"]).drop_duplicates("stat_date", keep="last") \
        .sort_values("stat_date").reset_index(drop=True)


def fetch_profit(code: str, qs: list[tuple[int, int]]) -> tuple[str, pd.DataFrame | None, str]:
    """下载一只股票若干季度；任一季度出错则整只返回错误（之后重试），空结果视为该季度尚未披露"""
    import baostock as bs
    from .store import _query
    try:
        parts = [_query(bs.query_profit_data, code=code, year=y, quarter=q) for y, q in qs]
        raw = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()
        return code, process_profit(raw), ""
    except Exception as e:  # noqa: BLE001
        return code, None, f"{type(e).__name__}: {e}"


def derive(fin: pd.DataFrame) -> pd.DataFrame:
    """由原始报告期数据计算因子字段（每行仍对应一个报告期，可用日期 = 该行公告日）"""
    df = fin.sort_values("stat_date").copy()
    q = df["stat_date"].dt.quarter
    df["roe"] = df["roe_avg"] * 4 / q * 100                   # 年化 ROE（%）：累计值按季度折算
    df["gross_margin"] = df["gp_margin"] * 100
    df["net_margin"] = df["np_margin"] * 100
    # 同比：与上一年同一报告期比较（上一年的数据一定更早公告，不涉及未来信息）
    prev = df.set_index("stat_date")
    last_year = df["stat_date"] - pd.DateOffset(years=1)
    for col, out in (("net_profit", "np_yoy"), ("revenue", "rev_yoy")):
        base = prev[col].reindex(last_year).values
        df[out] = (df[col].values - base) / np.abs(base) * 100
        df.loc[~np.isfinite(df[out]), out] = np.nan
    return df


FIELDS = ["roe", "gross_margin", "net_margin", "np_yoy", "rev_yoy", "total_share"]


def align(fin: pd.DataFrame, calendar: pd.DatetimeIndex, field: str) -> pd.Series:
    """把一只股票的某个字段按公告日对齐到交易日历"""
    df = fin[["stat_date", "pub_date", field]].dropna()
    if df.empty:
        return pd.Series(np.nan, index=calendar)
    df = df.sort_values(["pub_date", "stat_date"])
    df = df[df["stat_date"] >= df["stat_date"].cummax()]         # 迟到的旧报告期不覆盖新的
    avail = df["pub_date"] + pd.Timedelta(days=1)
    idx = calendar.searchsorted(avail.values, side="left")        # 公告日之后的第一个交易日起可用
    s = pd.Series(np.nan, index=calendar)
    stat = pd.Series(pd.NaT, index=calendar)
    for i, v, sd in zip(idx, df[field].values, df["stat_date"].values):
        if i < len(calendar):
            s.iloc[i] = v
            stat.iloc[i] = sd
    s, stat = s.ffill(), stat.ffill()
    stale = (calendar.values - stat.values) > np.timedelta64(STALE_DAYS, "D")
    return s.mask(stale | stat.isna())
