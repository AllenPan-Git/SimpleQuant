from .base import DataSource, FREQ_LABELS, normalize
from .akshare_src import AKShareSource, ASSET_TYPES
from .baostock_src import BaoStockSource
from .tdx_local_src import TdxLocalSource
from .csv_src import CSVSource, RESAMPLE_RULES, snapshot_to_bars, resample_bars
from . import library
from .ratio_adjust import is_additive

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
    for k in ("provider", "valuation", "adjust_method"):   # 实际使用的接口；附上的估值来自哪个指数；复权算法
        if df.attrs.get(k):
            extra[k] = df.attrs[k]
    meta = library.DatasetMeta(id=ds_id, name=name or symbol, symbol=symbol, source=source_key,
                               freq=freq, adjust=adjust, extra=extra)
    meta = library.save(df, meta)
    supersede(meta)
    return meta


def supersede(meta, lib_dir=None):
    """
    同一数据源、代码、周期、复权的旧数据，区间完全在新数据之内的删掉（重新下载、更新到最新时不留重复的旧数据，
    回测页记住的旧数据自动改用新的）；新数据是加法复权而旧的不是时保留旧的。返回删掉的 id
    """
    lib_dir = lib_dir or library.LIB_DIR
    gone = []
    for m in library.list_datasets(lib_dir):
        if (m.id != meta.id and (m.source, m.symbol, m.freq, m.adjust) == (meta.source, meta.symbol, meta.freq, meta.adjust)
                and meta.start <= m.start and m.end <= meta.end and not (is_additive(meta) and not is_additive(m))):
            library.replace(m.id, meta.id, lib_dir)
            gone.append(m.id)
    return gone
