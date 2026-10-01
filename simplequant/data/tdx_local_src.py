"""
通达信本地数据源：读取通达信客户端下载的盘后数据（离线、免费）
使用前：打开通达信 → 选项 → 盘后数据下载 → 勾选"1分钟线/5分钟线/日线"并下载。
数据目录一般是通达信安装目录，如 C:\\new_tdx（其中含 vipdoc 子目录）。
"""

import os

from ..i18n import L
from .base import DataSource, normalize, clip_dates

# 默认数据目录可通过环境变量 SIMPLEQUANT_TDX_DIR 指定
DEFAULT_TDX_DIR = os.environ.get("SIMPLEQUANT_TDX_DIR", r"C:\new_tdx")


class TdxLocalSource(DataSource):
    key = "tdx_local"
    label = L("通达信本地文件（免费，日线/1分钟/5分钟）", "TDX local files (free, daily / 1 min / 5 min)")
    freqs = ("1d", "5m", "1m")
    description = L("读取通达信客户端下载的盘后数据，不复权；需要先在通达信里下载数据。",
                    "Reads after-hours data downloaded by the TDX client (unadjusted). Download the data in TDX first.")

    def fetch(self, symbol, start, end, freq="1d", adjust="", tdx_dir=None, **kwargs):
        from mootdx.reader import Reader

        tdx_dir = tdx_dir or DEFAULT_TDX_DIR
        if not os.path.isdir(os.path.join(tdx_dir, "vipdoc")):
            raise FileNotFoundError(f"No vipdoc folder under {tdx_dir}; check the TDX install folder / 在该目录下没找到 vipdoc，请确认通达信安装目录")

        reader = Reader.factory(market="std", tdxdir=tdx_dir)
        if freq == "1d":
            raw = reader.daily(symbol=symbol)
        elif freq == "1m":
            raw = reader.minute(symbol=symbol)
        elif freq == "5m":
            raw = reader.fzline(symbol=symbol)
        else:
            raise ValueError(f"TDX local data does not support / 通达信本地数据不支持频率 {freq}")

        if raw is None or len(raw) == 0:
            raise ValueError(f"No {freq} data for {symbol}; download after-hours data in TDX first / 没有找到数据，请先在通达信里下载盘后数据")
        return clip_dates(normalize(raw.copy()), start, end)
