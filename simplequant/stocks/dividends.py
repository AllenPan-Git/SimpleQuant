"""
分红送转数据（BaoStock query_dividend_data），用于「现金分红并扣红利税」的选股回测

存储：data_cache/stocks/div/<code>.parquet，每行一次已实施的分红送转：
    ex_date 除权除息日 / cash 每股现金分红（税前，元）/ bonus 每股送股（来自未分配利润，按面值 1 元计税）
    / reserve 每股转增（来自资本公积，不计税）
没有分红记录的股票保存空表（与"还没下载"区分）。

现金分红模式（spec["dividend"] == "cash"）：
    - 行情改用「只按送转复权」的价格：不复权价 × 累计 (1 + 送股 + 转增)。除息日股价照常下跳，现金另行到账
    - 除息日开盘前，按前一天收盘时的持股数把税前现金分红计入账户（与登记日持股一致）
    - 红利税按 A 股差别化政策在卖出时扣：持股 ≤1 个月 20%、1 个月 ~ 1 年 10%、超过 1 年免税；
      应税所得 = 现金分红 + 送股股数 × 1 元面值；回测结束时仍持有的股票不扣（实际也是卖出时才扣）
    - 没有分红数据的股票仍用后复权价（分红视同再投资），避免把送转当成暴跌

BaoStock 分红表会漏记一部分分红（实测：特别分红如贵州茅台 2022-12、2023-12，宁德时代 2024-04 的 3 元；
中期分红如云南白药 2024 ~ 2026 年的几次），复权因子里却有。missing_events 用复权因子找出这些除权日，
按交易所的除权参考价（BaoStock 的 preclose）算出漏掉的金额：
    - 一般当作现金分红（照常扣红利税）
    - 配股（东方财富配股表，akshare stock_pg_em）、当天是 ST 的（实测全是破产重整的资本公积转增，如海南机场
      2021-12、方正科技 2022-12）、或"分红"超过前收一半的：视同全额参与配股 / 获得转增，
      股数按比例增加，不产生现金、不扣税，总资产与后复权口径一致
    - 复权因子变了但价格没变（BaoStock 的因子修正，如万科 2020-11）：不用管，现金分红模式用的是不复权价
"""

import datetime as dt

import numpy as np
import pandas as pd

COLUMNS = ["ex_date", "cash", "bonus", "reserve"]


def process_dividends(raw: pd.DataFrame) -> pd.DataFrame:
    """BaoStock 原始分红表 → 存储格式（只保留已实施、有除权除息日的记录）"""
    if raw is None or raw.empty:
        return pd.DataFrame({"ex_date": pd.Series(dtype="datetime64[ns]"),
                             **{c: pd.Series(dtype=float) for c in COLUMNS[1:]}})
    df = pd.DataFrame({
        "ex_date": pd.to_datetime(raw["dividOperateDate"], errors="coerce"),
        "cash": pd.to_numeric(raw["dividCashPsBeforeTax"], errors="coerce"),
        "bonus": pd.to_numeric(raw["dividStocksPs"], errors="coerce"),
        "reserve": pd.to_numeric(raw["dividReserveToStockPs"], errors="coerce"),
    })
    df = df.dropna(subset=["ex_date"])
    df[["cash", "bonus", "reserve"]] = df[["cash", "bonus", "reserve"]].fillna(0.0)
    df = df[(df["cash"] > 0) | (df["bonus"] > 0) | (df["reserve"] > 0)]
    # BaoStock 常把同一次分红返回两条完全相同的记录（实测约三成股票）：先去重，否则分红会算成两倍
    df = df.drop_duplicates()
    # 同一天不同内容的记录（如现金和送转分开公告）：合并
    df = df.groupby("ex_date", as_index=False)[["cash", "bonus", "reserve"]].sum()
    return df.sort_values("ex_date").reset_index(drop=True)


def fetch_dividends(code: str, years: list[int]) -> tuple[str, pd.DataFrame | None, str]:
    """下载一只股票若干年（按除权除息日所在年份查询）；任一年出错则整只返回错误（之后重试）"""
    import baostock as bs
    from .store import _query
    try:
        parts = [_query(bs.query_dividend_data, code=code, year=str(y), yearType="operate") for y in years]
        parts = [p for p in parts if len(p)]
        raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return code, process_dividends(raw), ""
    except Exception as e:  # noqa: BLE001
        return code, None, f"{type(e).__name__}: {e}"


def align(div: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """
    把分红事件放到交易日历上（除权除息日恰逢停牌时顺延到下一个交易日）。
    返回 index=calendar，列 cash / bonus / reserve（无事件为 0）
    """
    out = pd.DataFrame(0.0, index=calendar, columns=COLUMNS[1:])
    if div is None or div.empty or not len(calendar):
        return out
    div = div[div["ex_date"] >= calendar[0]]                     # 区间开始前的事件与回测无关
    idx = calendar.searchsorted(pd.DatetimeIndex(div["ex_date"]), side="left")
    for i, row in zip(idx, div.itertuples(index=False)):
        if i < len(calendar):
            out.iloc[i] += [row.cash, row.bonus, row.reserve]
    return out


def split_factor(bonus: pd.DataFrame, reserve: pd.DataFrame) -> pd.DataFrame:
    """只按送转计算的累计复权因子（日期 × 代码）：除权日起乘以 (1 + 送股 + 转增)"""
    return np.log1p(bonus + reserve).cumsum().pipe(np.exp)


def tax_rate(buy_date, sell_date) -> float:
    """
    A 股个人红利税（差别化）：持股 ≤1 个月 20%，1 个月以上 ~ 1 年 10%，超过 1 年免税
    持股期限从买入之日起算至卖出交割日前一日
    """
    buy, sell = pd.Timestamp(buy_date), pd.Timestamp(sell_date) - pd.Timedelta(days=1)
    if sell <= buy + pd.DateOffset(months=1):
        return 0.20
    if sell <= buy + pd.DateOffset(years=1):
        return 0.10
    return 0.0


def years(start_year: int, today: dt.date | None = None) -> list[int]:
    today = today or dt.date.today()
    return list(range(start_year, today.year + 1))


GAP_TOL = 0.003          # 分红表推算的除权比例与复权因子相差超过 0.3% 才算漏记
RIGHTS_WINDOW = pd.Timedelta(days=45)    # 配股股权登记日之后多少天内的除权视为配股（缴款结束后才除权）


def process_rights(raw: pd.DataFrame) -> pd.DataFrame:
    """东方财富配股表 → 代码（sh.600030 格式）、股权登记日"""
    from ..data.base import exchange_prefix
    if raw is None or raw.empty:
        return pd.DataFrame({"code": pd.Series(dtype=str), "record_date": pd.Series(dtype="datetime64[ns]")})
    sym = raw["股票代码"].astype(str).str.zfill(6)
    df = pd.DataFrame({"code": [f"{exchange_prefix(x)}.{x}" for x in sym],
                       "record_date": pd.to_datetime(raw["股权登记日"], errors="coerce")})
    return df.dropna().drop_duplicates().reset_index(drop=True)


def fetch_rights() -> pd.DataFrame:
    import akshare as ak
    return process_rights(pd.DataFrame(ak.stock_pg_em()))


def missing_events(daily: pd.DataFrame, div: pd.DataFrame | None, rights: list | None) -> pd.DataFrame:
    """
    分红表漏记的除权除息（格式同分红表：ex_date / cash / bonus / reserve，每股、不复权）
    :param daily:  该股日线，至少有 raw_close / raw_preclose / adj_factor，可选 is_st（停牌日可以有也可以没有）
    :param div:    分红表
    :param rights: 配股的股权登记日列表；None 表示没有配股数据（漏记的一律当作现金分红）
    """
    d = daily[["raw_close", "raw_preclose", "adj_factor"]].dropna()
    st = daily["is_st"].reindex(d.index).fillna(0).astype(bool) if "is_st" in daily else pd.Series(False, index=d.index)
    known = (div.groupby("ex_date")[["cash", "bonus", "reserve"]].sum() if div is not None and len(div)
             else pd.DataFrame(columns=["cash", "bonus", "reserve"], dtype=float))
    ratio = d["adj_factor"] / d["adj_factor"].shift(1)
    prev = d["raw_close"].shift(1)
    rights = [pd.Timestamp(x) for x in (rights or [])] if rights is not None else None
    rows = []
    for t in ratio.index[(ratio - 1).abs() > 1e-4]:
        u, x = float(prev[t]), float(d.at[t, "raw_preclose"])
        cash, split = (float(known.at[t, "cash"]), float(known.at[t, "bonus"] + known.at[t, "reserve"]))             if t in known.index else (0.0, 0.0)
        if not (u > cash >= 0 and x > 0):
            continue
        if abs(ratio[t] / (u * (1 + split) / (u - cash)) - 1) <= GAP_TOL:
            continue                                   # 分红表已经解释了
        extra = u - cash - x * (1 + split)             # 交易所除权参考价里多扣掉的部分
        if extra <= GAP_TOL * u:
            continue                                   # 因子修正（价格没变）或分红表与交易所口径的小差异
        is_rights = rights is not None and any(r <= t <= r + RIGHTS_WINDOW for r in rights)
        if is_rights or st[t] or extra >= 0.5 * u:
            j = (u - cash) / (u - cash - extra)        # 股数按比例增加，参考价不变
            rows.append({"ex_date": t, "cash": 0.0, "bonus": 0.0, "reserve": (1 + split) * (j - 1)})
        else:
            rows.append({"ex_date": t, "cash": round(extra, 4), "bonus": 0.0, "reserve": 0.0})
    out = pd.DataFrame(rows, columns=COLUMNS)
    return out.astype({"ex_date": "datetime64[ns]", "cash": float, "bonus": float, "reserve": float})


def with_missing(div: pd.DataFrame | None, daily: pd.DataFrame, rights: list | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """分红表 + 漏记的除权除息；返回 (合并后的表, 补上的部分)"""
    extra = missing_events(daily, div, rights)
    if div is None or div.empty:
        return extra, extra
    if extra.empty:
        return div, extra
    return pd.concat([div, extra], ignore_index=True).sort_values("ex_date").reset_index(drop=True), extra
