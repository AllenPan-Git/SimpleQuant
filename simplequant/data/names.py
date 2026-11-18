"""
证券中文简称：手动输入代码下载的数据，本地数据库里名称只有代码（热门列表里的才带名称）。
用 BaoStock 证券列表（股票、ETF、指数、可转债约 9000 条）补成「600519 贵州茅台」。

列表要分页拉取，一次约 40 秒，所以缓存成文件：缓存里查得到就不联网；查不到且缓存超过一天才重新拉。
缓存放在 data_cache/names/ 子目录：本地数据库会扫描 data_cache 下的 *.json，不能放在同一层；
放在 data_cache 里，设置页搬数据位置时会一起带走。
"""

import re
import time
from pathlib import Path

import pandas as pd

from . import library
from .base import exchange_prefix

REFRESH_AFTER = 24 * 3600       # 有查不到的代码时，缓存超过这么久才重新拉（新上市的证券）
INDEX = "2"                     # BaoStock type：1 股票、2 指数、4 可转债、5 ETF


def fetch() -> pd.DataFrame:
    """从 BaoStock 拉取证券列表：code（如 sh.600519）、name、type"""
    import baostock as bs
    from ..stocks.store import _login, _query
    _login()
    try:
        df = _query(bs.query_stock_basic)
    finally:
        bs.logout()
    return df[["code", "code_name", "type"]].rename(columns={"code_name": "name"})


def cache_file() -> Path:
    """data_cache/names/securities.parquet（调用时才读目录：测试会把本地数据库换到临时目录）"""
    return library.LIB_DIR / "names" / "securities.parquet"


def load(path: Path | None = None) -> pd.DataFrame | None:
    path = path or cache_file()
    try:
        return pd.read_parquet(path)
    except (OSError, ValueError):
        return None


def refresh(path: Path | None = None, fetcher=None) -> pd.DataFrame:
    path = path or cache_file()
    df = (fetcher or fetch)()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df


def lookup(table: pd.DataFrame, symbol: str, asset: str = "") -> str:
    """6 位代码 → 简称；查不到返回空串。
    同一代码可能既是指数又是股票（sh.000001 上证综指 / sz.000001 平安银行）：
    AKShare 下载时选了「指数」就找指数，否则找非指数、交易所按代码推断的那一条"""
    rows = table[table["code"].str[3:] == symbol]
    if rows.empty:
        return ""
    is_index = rows["type"] == INDEX
    if asset == "index":
        rows = rows[is_index] if is_index.any() else rows
    elif (~is_index).any():
        rows = rows[~is_index]
    if len(rows) > 1:
        same = rows[rows["code"].str[:2] == exchange_prefix(symbol)]
        rows = same if not same.empty else rows
    return str(rows["name"].iloc[0])


def needs_name(m: library.DatasetMeta) -> bool:
    """名称只有代码的在线数据（CSV 导入的名称是用户起的，不动）"""
    return m.source != "csv" and m.name == m.symbol and bool(re.fullmatch(r"\d{6}", m.symbol or ""))


def fill_library(lib_dir: Path | None = None, path: Path | None = None, fetcher=None, online: bool = True) -> int:
    """给本地数据库里名称只有代码的数据补上简称，返回补了几份。
    online=False 只用缓存，不联网（模拟盘正在更新时，免得抢 BaoStock 连接）"""
    lib_dir = lib_dir or library.LIB_DIR
    path = path or cache_file()
    metas = [m for m in library.list_datasets(lib_dir) if needs_name(m)]
    if not metas:
        return 0
    table = load(path)

    def found(tb):
        return {m.id: lookup(tb, m.symbol, (m.extra or {}).get("asset", "")) for m in metas} if tb is not None else {}

    names = found(table)
    stale = table is None or time.time() - path.stat().st_mtime > REFRESH_AFTER
    if online and stale and not all(names.get(m.id) for m in metas):    # 没有缓存时 names 是空的
        names = found(refresh(path, fetcher))
    n = 0
    for m in metas:
        if names.get(m.id):
            m.name = f"{m.symbol} {names[m.id]}"
            library.save_meta(m, lib_dir)
            n += 1
    return n
