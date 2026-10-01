from .base import DataSource, FREQ_LABELS, normalize
from .akshare_src import AKShareSource, ASSET_TYPES
from .baostock_src import BaoStockSource
from .tdx_local_src import TdxLocalSource
from .csv_src import CSVSource, RESAMPLE_RULES, snapshot_to_bars, resample_bars
from . import library

SOURCES: dict[str, DataSource] = {s.key: s for s in (AKShareSource(), BaoStockSource(), TdxLocalSource(), CSVSource())}


ONLINE_DAILY = ("akshare", "baostock")


def complete_bars_only(df):
    """
    去掉尚未走完的日 K 线：交易时间内 AKShare 会返回当天的盘中数据。
    只保留到"此刻应已发布完整数据"的交易日（与模拟盘相同的规则，见 paper/calendar.py）。
    """
    from ..paper.calendar import load_calendar, latest_expected_day   # 延迟导入，避免循环依赖
    cutoff = latest_expected_day(load_calendar())
    trimmed = df.loc[:cutoff]
    if trimmed.empty:
        raise ValueError("no completed daily bars yet / 还没有已收盘的日线数据")
    return trimmed


def fetch_to_library(source_key: str, symbol: str, start: str, end: str, freq: str = "1d",
                     adjust: str = "hfq", name: str | None = None, force: bool = False, **kwargs):
    """从网络/本地数据源获取行情并存入本地数据库；已存在则直接返回（除非 force）"""
    ds_id = library.make_id(source_key, symbol, freq, adjust, start, end)
    if not force and library.exists(ds_id):
        return next(m for m in library.list_datasets() if m.id == ds_id)
    df = SOURCES[source_key].fetch(symbol, start, end, freq=freq, adjust=adjust, **kwargs)
    if freq == "1d" and source_key in ONLINE_DAILY:
        df = complete_bars_only(df)
    extra = {k: v for k, v in kwargs.items() if isinstance(v, str)}
    if df.attrs.get("provider"):
        extra["provider"] = df.attrs["provider"]
    meta = library.DatasetMeta(id=ds_id, name=name or symbol, symbol=symbol, source=source_key,
                               freq=freq, adjust=adjust, extra=extra)
    return library.save(df, meta)
