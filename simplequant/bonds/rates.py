"""
利率与信用利差（中债收益率曲线，AKShare bond_china_yield；用于择时规则的条件）

接口实测（2026-10-01）：每次最多约 1 年、约 12 秒；只有三条曲线：中债国债、中债中短期票据（AAA）、
中债商业银行普通债（AAA），期限 3 月 ~ 30 年。没有企业债曲线，所以信用利差用「中短期票据 AAA − 国债」。

规则里可用的列（与行情的列一样按日期对齐）：
    cgb10y         10 年期国债收益率（%）
    term_spread    期限利差：10 年 − 1 年国债（BP）
    credit_spread  信用利差：3 年中短期票据 AAA − 3 年国债（BP）
收益率曲线在交易日傍晚发布；日线策略在收盘产生信号、下一个开盘成交，用当天的数值不涉及未来数据。
分钟线用前一天的数值。

文件：<数据目录>/data_cache/rates.parquet（日期 × 原始期限点）
"""

import datetime as dt
from pathlib import Path

import pandas as pd

from ..data.base import with_retry
from ..paths import CACHE_DIR

PATH = CACHE_DIR / "rates.parquet"
CURVES = {"中债国债收益率曲线": "cgb", "中债中短期票据收益率曲线(AAA)": "mtn"}
TENORS = {"1年": "1y", "3年": "3y", "10年": "10y"}
MACRO_COLUMNS = ["cgb10y", "term_spread", "credit_spread"]
FIRST_YEAR = 2015


def fetch_year(year: int, today: dt.date | None = None) -> pd.DataFrame:
    """一年的原始收益率（日期索引；列 cgb_1y、cgb_3y、cgb_10y、mtn_1y …）"""
    import akshare as ak
    today = today or dt.date.today()
    start, end = f"{year}0101", min(dt.date(year, 12, 31), today).strftime("%Y%m%d")
    raw = with_retry(lambda: ak.bond_china_yield(start_date=start, end_date=end), retries=3, wait=3.0)
    return process(pd.DataFrame(raw))


def process(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or not len(raw):
        return pd.DataFrame()
    raw = raw[raw["曲线名称"].isin(CURVES)].copy()
    raw["date"] = pd.to_datetime(raw["日期"])
    parts = []
    for name, prefix in CURVES.items():
        sub = raw[raw["曲线名称"] == name].set_index("date")
        cols = {k: f"{prefix}_{v}" for k, v in TENORS.items() if k in sub.columns}
        parts.append(sub[list(cols)].rename(columns=cols).apply(pd.to_numeric, errors="coerce"))
    df = pd.concat(parts, axis=1).sort_index()
    return df[~df.index.duplicated(keep="last")]


def to_macro(raw: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    out["cgb10y"] = raw.get("cgb_10y")
    out["term_spread"] = (raw.get("cgb_10y") - raw.get("cgb_1y")) * 100
    out["credit_spread"] = (raw.get("mtn_3y") - raw.get("cgb_3y")) * 100
    return out.dropna(how="all")


class RatesStore:
    def __init__(self, path: Path = PATH, fetch=fetch_year):
        self.path, self.fetch = Path(path), fetch

    def ready(self) -> bool:
        return self.path.exists()

    def load_raw(self) -> pd.DataFrame:
        return pd.read_parquet(self.path) if self.path.exists() else pd.DataFrame()

    def load(self) -> pd.DataFrame:
        """规则用的三列（日期索引）"""
        raw = self.load_raw()
        return to_macro(raw) if len(raw) else pd.DataFrame(columns=MACRO_COLUMNS)

    def update(self, first_year: int = FIRST_YEAR, today: dt.date | None = None, progress=None) -> pd.DataFrame:
        """缺的年份整年下载；最近一年（及数据不到年底的年份）重新下载。返回原始数据"""
        today = today or dt.date.today()
        have = self.load_raw()
        years = list(range(first_year, today.year + 1))
        todo = []
        for y in years:
            got = have[have.index.year == y] if len(have) else have
            complete = len(got) and (y < today.year and got.index[-1] >= pd.Timestamp(y, 12, 20))
            if not complete:
                todo.append(y)
        parts = [have[~have.index.year.isin(todo)]] if len(have) else []
        for i, y in enumerate(todo, 1):
            parts.append(self.fetch(y, today))
            if progress:
                progress(i, len(todo))
        df = pd.concat([p for p in parts if len(p)]).sort_index() if parts else pd.DataFrame()
        df = df[~df.index.duplicated(keep="last")]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path)
        return df

    def stale(self, today: dt.date | None = None, max_days: int = 3) -> bool:
        """本地数据比最近的交易日早了好几天（模拟盘每天先更新）"""
        if not self.ready():
            return True
        last = self.load_raw().index[-1].date()
        return ((today or dt.date.today()) - last).days > max_days


def needed(cols) -> list[str]:
    """规则需要的列里属于利率数据的"""
    return [c for c in MACRO_COLUMNS if c in set(cols)]


def attach(prices: dict, cols, macro: pd.DataFrame) -> dict:
    """
    把利率列并入各标的行情（不改原表）。按日期取当天及以前最近的值；分钟线取前一天的值
    """
    cols = needed(cols)
    if not cols:
        return prices
    m = macro[cols].sort_index()
    out = {}
    for name, df in prices.items():
        idx = pd.DatetimeIndex(df.index)
        intraday = bool(idx.normalize().duplicated().any())          # 同一天有多根 K 线
        key = idx.normalize() - (pd.Timedelta(days=1) if intraday else pd.Timedelta(0))
        vals = m.reindex(m.index.union(key.unique())).ffill().reindex(key)      # 分钟线同一天有多根
        df = df.copy()
        for c in cols:
            df[c] = vals[c].values
        out[name] = df
    return out
