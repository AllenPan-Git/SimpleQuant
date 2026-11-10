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
    return merge_overlaps(meta)


SEAM_GAP_DAYS = 10      # 两段之间隔这么多自然日以内、又没有重叠，算「本该接上的」，提示用户让区间重叠一天


def _same_series(a, b) -> bool:
    """同一标的的两份数据是否能拼成一份：同数据源、代码、周期、复权，且同为加法 / 等比复权"""
    return ((a.source, a.symbol, a.freq, a.adjust) == (b.source, b.symbol, b.freq, b.adjust)
            and is_additive(a) == is_additive(b))


def _seam_matches(new_df, old_df) -> bool | None:
    """重叠的日子两边价格是否一致；没有重叠返回 None。前复权中间有过分红、数据源不同时会对不上"""
    import numpy as np
    common = new_df.index.intersection(old_df.index)
    cols = [c for c in ("open", "high", "low", "close") if c in new_df and c in old_df]
    if common.empty or not cols:
        return None
    a = new_df.loc[common, cols].to_numpy(float)
    b = old_df.loc[common, cols].to_numpy(float)
    return bool(np.allclose(a, b, rtol=1e-4, atol=1e-6, equal_nan=True))


def merge_overlaps(meta, lib_dir=None):
    """
    同一标的分段下载的数据拼成一份：与新数据区间有重叠、且重叠日价格一致的旧数据合并进来（重叠处用新数据），
    存成新的一份，旧的几份删掉并指向它（界面记住的旧 id 自动改用合并后的）。
    返回最终的 meta，附两个不存盘的属性：merged = 合并进来的旧数据，unmerged = 没能合并的 (旧数据, 原因)，
    原因为 "mismatch"（重叠日价格对不上）或 "gap"（首尾相接但没有重叠的日子，无法核对）
    """
    lib_dir = lib_dir or library.LIB_DIR
    df = library.load(meta.id, lib_dir)
    merged, parts = [], [meta.id]
    while True:                         # 合并一份后区间变长，原来首尾相接的可能变成有重叠，再看一遍
        changed, unmerged = False, []
        for m in library.list_datasets(lib_dir):
            if m.id in parts or not _same_series(meta, m):
                continue
            old = library.load(m.id, lib_dir)
            ok = _seam_matches(df, old)
            if ok:
                df = df.combine_first(old)[list(dict.fromkeys([*df.columns, *old.columns]))]
                merged.append(m)
                parts.append(m.id)
                changed = True
            elif ok is False:
                unmerged.append((m, "mismatch"))
            else:
                gap = min(abs((old.index[0] - df.index[-1]).days), abs((df.index[0] - old.index[-1]).days))
                if gap <= SEAM_GAP_DAYS:
                    unmerged.append((m, "gap"))
        if not changed:
            break
    if merged:
        start, end = df.index[0].strftime("%Y-%m-%d"), df.index[-1].strftime("%Y-%m-%d")
        new_id = library.make_id(meta.source, meta.symbol, meta.freq, meta.adjust, start, end)
        meta = library.save(df, library.DatasetMeta(id=new_id, name=meta.name, symbol=meta.symbol, source=meta.source,
                                                    freq=meta.freq, adjust=meta.adjust, extra=meta.extra), lib_dir)
        for old_id in parts:
            if old_id != new_id:
                library.replace(old_id, new_id, lib_dir)
    meta.merged, meta.unmerged = merged, unmerged
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
