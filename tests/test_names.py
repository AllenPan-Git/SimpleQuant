"""证券简称：本地数据库里名称只有代码的数据补上中文简称（simplequant/data/names.py）"""

import os
import time

import pandas as pd

from simplequant.data import library, names

TABLE = pd.DataFrame({
    "code": ["sh.600519", "sh.000001", "sz.000001", "sh.510300", "sh.000300", "sz.399006"],
    "name": ["贵州茅台", "上证综合指数", "平安银行", "华泰柏瑞沪深300ETF", "沪深300指数", "创业板指数(价格)"],
    "type": ["1", "2", "1", "5", "2", "2"],
})


def _add(lib_dir, symbol, name=None, source="akshare", asset="stock"):
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    meta = library.DatasetMeta(id=f"{source}_{symbol}_{asset}", name=name or symbol, symbol=symbol, source=source,
                               freq="1d", adjust="hfq", extra={"asset": asset} if source == "akshare" else {})
    return library.save(df, meta, lib_dir)


def _names(lib_dir):
    return {m.id: m.name for m in library.list_datasets(lib_dir)}


def test_lookup_tells_index_from_stock():
    assert names.lookup(TABLE, "000001", "index") == "上证综合指数"
    assert names.lookup(TABLE, "000001", "stock") == "平安银行"
    assert names.lookup(TABLE, "000001") == "平安银行"              # BaoStock 数据源下载的没有 asset：按股票
    assert names.lookup(TABLE, "000300", "etf") == "沪深300指数"     # 只有指数这一条：选错类型也照样给
    assert names.lookup(TABLE, "510300", "etf") == "华泰柏瑞沪深300ETF"
    assert names.lookup(TABLE, "123456") == ""


def test_fill_library_downloads_once_then_uses_cache(tmp_path):
    lib, cache = tmp_path / "lib", tmp_path / "names" / "s.parquet"
    _add(lib, "600519")
    _add(lib, "000001", asset="index")
    _add(lib, "510300", name="510300 沪深300ETF", asset="etf")       # 热门列表里的，已有名称
    _add(lib, "600519", source="csv")                                # CSV 的名称是用户起的
    calls = []

    def fetcher():
        calls.append(1)
        return TABLE

    assert names.fill_library(lib, cache, fetcher) == 2
    got = _names(lib)
    assert got["akshare_600519_stock"] == "600519 贵州茅台"
    assert got["akshare_000001_index"] == "000001 上证综合指数"
    assert got["akshare_510300_etf"] == "510300 沪深300ETF"
    assert got["csv_600519_stock"] == "600519"
    assert library.load("akshare_600519_stock", lib)["close"].tolist() == [1.0, 2.0]    # 行情文件不动

    _add(lib, "399006", source="baostock")          # 缓存里有：不联网
    assert names.fill_library(lib, cache, fetcher) == 1
    assert _names(lib)["baostock_399006_stock"] == "399006 创业板指数(价格)"
    assert len(calls) == 1


def test_missing_code_refetches_only_when_cache_is_old(tmp_path):
    lib, cache = tmp_path / "lib", tmp_path / "s.parquet"
    names.refresh(cache, lambda: TABLE)
    _add(lib, "688981")                             # 缓存里没有（新上市）
    newer = pd.concat([TABLE, pd.DataFrame({"code": ["sh.688981"], "name": ["中芯国际"], "type": ["1"]})])
    calls = []

    def fetcher():
        calls.append(1)
        return newer

    assert names.fill_library(lib, cache, fetcher) == 0        # 缓存还新：不反复拉 40 秒的列表
    assert calls == []
    old = time.time() - names.REFRESH_AFTER - 60
    os.utime(cache, (old, old))
    assert names.fill_library(lib, cache, fetcher, online=False) == 0      # 模拟盘在更新：不联网
    assert names.fill_library(lib, cache, fetcher) == 1
    assert _names(lib)["akshare_688981_stock"] == "688981 中芯国际"


def test_nothing_to_fill_does_not_touch_network(tmp_path):
    lib = tmp_path / "lib"
    _add(lib, "510300", name="510300 沪深300ETF", asset="etf")

    def fetcher():
        raise AssertionError("should not fetch")

    assert names.fill_library(lib, tmp_path / "s.parquet", fetcher) == 0
