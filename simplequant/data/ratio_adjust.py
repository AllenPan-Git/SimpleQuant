"""
等比复权（分红再投资）：复权价 = 不复权价 × 复权因子，每次除权除息时因子乘以 前收 / 除权参考价

东方财富给的复权价是加法复权（复权价 = 不复权价 + 上市以来的累计分红）：持有不动的总收益相当于
分红留作现金，但每天的涨跌幅被压缩成 真实涨跌 × 不复权价 / (不复权价 + 累计分红)，分红越多、越老的标的
压缩越多（九安医疗 2020 年起约为真实涨跌的 98%），按百分比的止损、持仓收益率、波动率都会失真。
所以 AKShare 数据源改为下载不复权行情，自己乘以等比复权因子：
- 股票：BaoStock 的后复权因子（与选股、新浪股票、BaoStock 数据源一致）
- ETF：新浪的累计每份分红 + 份额拆分 / 折算（比例按当天价格跳空推算，见 sina.etf_splits）
因子从上市第一天起算（第一天为 1），所以同一标的不论下载哪段区间，后复权价都相同；前复权 = 后复权 / 最新因子
"""

import pandas as pd

from .base import exchange_prefix

PRICES = ["open", "high", "low", "close"]


def apply(df: pd.DataFrame, factor: pd.Series, adjust: str) -> pd.DataFrame:
    """不复权行情（标准列名）× 因子；adjust = hfq / qfq"""
    f = factor.reindex(df.index)
    if f.isna().any():
        raise ValueError("adjustment factor does not cover the data / 复权因子没有覆盖全部日期")
    if adjust == "qfq":
        f = f / float(f.iloc[-1])                 # 最新价格等于真实价格
    out = df.copy()
    for c in PRICES:
        out[c] = df[c] * f
    return out


def step_factor(steps: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """因子变动表（生效日 → 因子）→ 每个交易日适用的因子；第一次变动之前为 1"""
    steps = steps.dropna().sort_index()
    steps = steps[~steps.index.duplicated(keep="last")]
    return steps.reindex(dates.union(steps.index)).ffill().reindex(dates).fillna(1.0)


def stock_factor(symbol: str, dates: pd.DatetimeIndex) -> pd.Series:
    """BaoStock 后复权因子"""
    import baostock as bs
    from ..stocks.store import _login, _query
    _login()
    end = dates[-1].strftime("%Y-%m-%d")
    adj = _query(bs.query_adjust_factor, code=f"{exchange_prefix(symbol)}.{symbol}", start_date="1990-01-01",
                 end_date=end)
    if adj.empty:                                  # 从没有除权除息过
        return pd.Series(1.0, index=dates)
    steps = pd.Series(pd.to_numeric(adj["backAdjustFactor"], errors="coerce").values,
                      index=pd.to_datetime(adj["dividOperateDate"]))
    return step_factor(steps, dates)


def etf_factor(raw: pd.DataFrame, table: pd.DataFrame) -> pd.Series:
    """
    ETF 的等比因子。raw：不复权日线（从上市起）；table：新浪复权表（列 u = 累计每份分红，日期为除息日）。
    拆分日因子乘以拆分比例；除息日乘以 前收 / (前收 − 每份分红)，与交易所除息参考价、BaoStock 的算法相同
    """
    from .sina import etf_splits
    close = raw["close"]
    jump = pd.Series(1.0, index=close.index)
    for d, ratio in etf_splits(raw, table).items():
        jump[d] *= ratio
    if "u" in table and table["u"].notna().any():
        u = table["u"].dropna().sort_index()
        cash = u.diff().fillna(u)
        prev = close.shift(1)
        for d, c in cash[cash > 1e-9].items():
            i = close.index.searchsorted(d)        # 除息日当天或之后的第一个交易日
            if i == 0 or i >= len(close):
                continue
            p = prev.iloc[i]
            if not p > c:
                raise ValueError(f"dividend {c} on {d:%Y-%m-%d} exceeds the previous close / 分红大于前收")
            jump.iloc[i] *= p / (p - c)
    return jump.cumprod()


def is_additive(meta) -> bool:
    """
    这份数据是加法复权：AKShare 的股票 / ETF 复权数据中，改为等比复权之前下载的（没有 adjust_method，
    新浪股票本来就是等比除外），以及取不到复权因子、退回东方财富复权价的
    """
    extra = meta.extra or {}
    if meta.source != "akshare" or not meta.adjust or extra.get("asset") not in ("etf", "stock"):
        return False
    if extra.get("adjust_method"):
        return extra["adjust_method"] == "additive"
    return extra.get("provider", "eastmoney") != "sina+factors"
