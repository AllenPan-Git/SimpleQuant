"""
数据源抽象与标准化工具

所有数据源最终都输出同一种格式的 DataFrame：
    index   : DatetimeIndex，名为 "datetime"，升序、无重复
    columns : open, high, low, close, volume（float）
              以及可选的 EXTRA_COLUMNS：turnover 换手率(%)、pe 市盈率TTM、pb 市净率、ps 市销率TTM
"""

from abc import ABC, abstractmethod
import time

import pandas as pd

from ..i18n import L, pick

STD_COLUMNS = ["open", "high", "low", "close", "volume"]
EXTRA_COLUMNS = ["turnover", "pe", "pb", "ps"]

# 频率代码 -> 名称
FREQ_LABELS = {
    "1d": L("日线", "Daily"),
    "60m": L("60分钟", "60 min"),
    "30m": L("30分钟", "30 min"),
    "15m": L("15分钟", "15 min"),
    "5m": L("5分钟", "5 min"),
    "1m": L("1分钟", "1 min"),
    "30s": L("30秒", "30 sec"),
    "10s": L("10秒", "10 sec"),
}


def freq_label(freq: str, lang: str = "zh") -> str:
    return pick(FREQ_LABELS.get(freq, freq), lang)


def infer_freq(df: pd.DataFrame) -> str:
    """根据相邻 K 线的时间间隔推断周期代码"""
    if len(df) < 2:
        return "1d"
    step = df.index.to_series().diff().median()
    if step >= pd.Timedelta(hours=20):
        return "1d"
    if step >= pd.Timedelta(minutes=1):
        return f"{round(step / pd.Timedelta(minutes=1))}m"
    return f"{max(round(step / pd.Timedelta(seconds=1)), 1)}s"

# 常用中文/英文列名 -> 标准列名
COLUMN_ALIASES = {
    "日期": "datetime", "时间": "datetime", "date": "datetime", "time": "datetime",
    "trade_time": "datetime", "datetime": "datetime", "trade_date": "datetime",
    "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
    "成交量": "volume", "vol": "volume",
    "成交额": "amount",
    # 可选因子列（AKShare 的 换手率；BaoStock 的 turn / peTTM / pbMRQ / psTTM）
    "换手率": "turnover", "turn": "turnover",
    "市盈率": "pe", "peTTM": "pe", "pe_ttm": "pe",
    "市净率": "pb", "pbMRQ": "pb",
    "市销率": "ps", "psTTM": "ps", "ps_ttm": "ps",
}


class DataSource(ABC):
    """数据源基类。子类声明 key/label/freqs，并实现 fetch()。"""

    key: str = ""
    label: dict = {}          # {"zh": ..., "en": ...}
    freqs: tuple = ("1d",)
    description: dict = {}

    @abstractmethod
    def fetch(self, symbol: str, start: str, end: str, freq: str = "1d",
              adjust: str = "hfq", **kwargs) -> pd.DataFrame:
        """
        :param symbol: 6 位代码，如 '510300'（CSV 数据源为文件路径）
        :param start / end: 'YYYY-MM-DD'
        :param freq: FREQ_LABELS 中的键
        :param adjust: 'hfq' 后复权 / 'qfq' 前复权 / '' 不复权
        """


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """把任意来源的行情表整理成标准格式。"""
    df = df.rename(columns={c: COLUMN_ALIASES.get(str(c).strip(), str(c).strip()) for c in df.columns})
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df.index = pd.to_datetime(df.index)
    df.index.name = "datetime"

    missing = [c for c in STD_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"Missing required columns / 数据缺失关键列: {missing}")

    extras = [c for c in EXTRA_COLUMNS if c in df.columns]
    df = df[STD_COLUMNS + extras].apply(pd.to_numeric, errors="coerce")
    df = df.drop(columns=[c for c in extras if df[c].isna().all()])   # 整列为空的因子视为没有
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.ffill()
    if df.empty:
        raise ValueError("No data left after cleaning / 整理后数据为空")
    return df.astype(float)


def with_retry(fn, retries: int = 3, wait: float = 1.5):
    """网络请求重试"""
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 数据接口的异常类型五花八门
            last_err = e
            if i < retries - 1:
                time.sleep(wait)
    raise RuntimeError(f"Download failed after {retries} retries / 数据获取失败（已重试 {retries} 次）: {last_err}") from last_err


def exchange_prefix(symbol: str) -> str:
    """根据 6 位代码推断交易所：沪市 'sh' / 深市 'sz' / 北交所 'bj'"""
    s = symbol.strip()
    if s[:1] in ("6", "5", "9") or s[:3] in ("110", "111", "113", "118", "019"):
        return "sh"
    if s[:1] in ("4", "8"):
        return "bj"
    return "sz"


def clip_dates(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df
