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
    from .store import BaoStockStopped, _query
    try:
        parts = [_query(bs.query_profit_data, code=code, year=y, quarter=q) for y, q in qs]
        raw = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()
        return code, process_profit(raw), ""
    except BaoStockStopped:
        raise
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


# ---------------- 排雷数据（资产负债表 + 现金流量表，单独下载） ----------------
# 每个季度两次请求（query_balance_data、query_cash_flow_data），全 A 2018 年起约 33 万次，按每天 4.5 万次约 8 天，
# 所以和盈利数据分开存（data_cache/stocks/risk/<code>.parquet），用到时才下载。
# 商誉、应收账款 BaoStock 没有。比例都是年初至报告期的累计值（一季度的现金流波动大）。
# 资产负债率不用 liabilityToAsset：10-26 实测它有一段被多除了 100（茅台 2025Q3 为 0.00128，2026-04 以后的公告又是 0.164），
# 改用权益乘数 assetToEquity 换算（1 − 1 / 权益乘数），各期都与正确的负债率一致
BALANCE_COLUMNS = {"statDate": "stat_date", "pubDate": "pub_date", "liabilityToAsset": "liability_to_asset",
                   "assetToEquity": "asset_to_equity", "currentRatio": "current_ratio"}
CASH_COLUMNS = {"statDate": "stat_date", "pubDate": "pub_date", "CFOToNP": "cfo_to_np", "CFOToOR": "cfo_to_or"}
RISK_RAW = ["liability_to_asset", "asset_to_equity", "current_ratio", "cfo_to_np", "cfo_to_or"]
RISK_FIELDS = ["debt_ratio", "current_ratio", "cfo_np"]
RISK_REQUESTS_PER_QUARTER = 2


def _tidy(raw: pd.DataFrame, columns: dict) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=list(columns.values()))
    df = raw.rename(columns=columns)[list(columns.values())].copy()
    for c in ("stat_date", "pub_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    num = [c for c in df.columns if c not in ("stat_date", "pub_date")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["stat_date", "pub_date"]).drop_duplicates("stat_date", keep="last")


def process_risk(balance: pd.DataFrame, cash: pd.DataFrame) -> pd.DataFrame:
    """两张表按报告期合并；公告日取较晚的（一般相同）"""
    b, c = _tidy(balance, BALANCE_COLUMNS), _tidy(cash, CASH_COLUMNS)
    df = b.merge(c, on="stat_date", how="outer", suffixes=("", "_c"))
    df["pub_date"] = df[["pub_date", "pub_date_c"]].max(axis=1)
    df = df.drop(columns="pub_date_c").dropna(subset=["pub_date"])
    return df[["stat_date", "pub_date"] + RISK_RAW].sort_values("stat_date").reset_index(drop=True)


def fetch_risk(code: str, qs: list[tuple[int, int]]) -> tuple[str, pd.DataFrame | None, str]:
    """同 fetch_profit：任一季度出错则整只返回错误"""
    import baostock as bs
    from .store import BaoStockStopped, _query
    try:
        parts = {"b": [], "c": []}
        for y, q in qs:
            parts["b"].append(_query(bs.query_balance_data, code=code, year=y, quarter=q))
            parts["c"].append(_query(bs.query_cash_flow_data, code=code, year=y, quarter=q))
        cat = {k: pd.concat([p for p in v if len(p)], ignore_index=True) if any(len(p) for p in v) else pd.DataFrame()
               for k, v in parts.items()}
        return code, process_risk(cat["b"], cat["c"]), ""
    except BaoStockStopped:
        raise
    except Exception as e:  # noqa: BLE001
        return code, None, f"{type(e).__name__}: {e}"


def derive_risk(raw: pd.DataFrame) -> pd.DataFrame:
    """
    debt_ratio 资产负债率（%）/ current_ratio 流动比率 / cfo_np 经营现金流 ÷ 净利润。
    净利润为负时 cfo_np 没有意义（现金流也为负时比值反而是正的），记为空：
    经营现金流 ÷ 营收的符号就是现金流的符号，两个比值符号相反说明净利润为负
    """
    df = raw.sort_values("stat_date").copy()
    ate = df["asset_to_equity"]
    df["debt_ratio"] = ((1 - 1 / ate) * 100).where(ate.notna() & (ate != 0))   # 资不抵债时权益乘数为负，负债率 > 100%
    loss = np.sign(df["cfo_to_np"]) * np.sign(df["cfo_to_or"]) < 0
    df["cfo_np"] = df["cfo_to_np"].where(~loss)
    return df


def align(fin: pd.DataFrame, calendar: pd.DatetimeIndex, field: str) -> pd.Series:
    """把一只股票的某个字段按公告日对齐到交易日历"""
    return pd.Series(align_all(fin, calendar, [field])[:, 0], index=calendar)


def align_all(fin: pd.DataFrame, calendar: pd.DatetimeIndex, fields: list[str]) -> np.ndarray:
    """
    一次对齐多个字段（面板载入时每只股票调用一次，全部用 numpy，比逐字段用 pandas 快很多）
    返回 (交易日数 × 字段数) 的数组；规则同 align：公告日之后的第一个交易日起可用，迟到的旧报告期不覆盖新的，
    报告期超过 STALE_DAYS 天视为缺失
    """
    cal = calendar.values.astype("datetime64[ns]").view("i8")
    out = np.full((len(cal), len(fields)), np.nan)
    if not len(cal) or fin.empty:
        return out
    stat_all = fin["stat_date"].values.astype("datetime64[ns]").view("i8")
    pub_all = fin["pub_date"].values.astype("datetime64[ns]").view("i8")
    nat = np.iinfo(np.int64).min
    stale_ns = np.timedelta64(STALE_DAYS, "D").astype("timedelta64[ns]").view("i8")
    day = np.timedelta64(1, "D").astype("timedelta64[ns]").view("i8")
    for j, f in enumerate(fields):
        val = fin[f].values.astype(float)
        ok = (stat_all != nat) & (pub_all != nat) & ~np.isnan(val)
        if not ok.any():
            continue
        stat, pub, val = stat_all[ok], pub_all[ok], val[ok]
        order = np.lexsort((stat, pub))                           # 按公告日、再按报告期排序
        stat, pub, val = stat[order], pub[order], val[order]
        keep = stat >= np.maximum.accumulate(stat)                # 迟到的旧报告期不覆盖新的
        stat, pub, val = stat[keep], pub[keep], val[keep]
        idx = np.searchsorted(cal, pub + day, side="left")        # 公告日之后的第一个交易日起可用
        inside = idx < len(cal)
        stat, val, idx = stat[inside], val[inside], idx[inside]
        if not len(idx):
            continue
        last = np.r_[idx[1:] != idx[:-1], True]                   # 同一天有几条时以排在最后的为准
        pos = np.full(len(cal), -1)
        pos[idx[last]] = np.flatnonzero(last)
        pos = np.maximum.accumulate(pos)                          # 向前填充
        has = pos >= 0
        p = pos[has]
        fresh = (cal[has] - stat[p]) <= stale_ns
        col = out[:, j]
        col[np.flatnonzero(has)[fresh]] = val[p][fresh]
    return out
