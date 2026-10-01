"""
本地 CSV 数据源
1. K 线文件：自动识别日期列和 OHLCV 列（中英文列名均可）
2. 快照/Tick 文件（含 last 最新价 + 当日累计成交量）：重采样成 N 秒/N 分钟 K 线
"""

import pandas as pd

from ..i18n import L
from .base import DataSource, normalize, clip_dates, COLUMN_ALIASES

# 界面上可选的重采样周期
RESAMPLE_RULES = {
    "10s": L("10秒", "10 sec"), "30s": L("30秒", "30 sec"), "1min": L("1分钟", "1 min"),
    "5min": L("5分钟", "5 min"), "15min": L("15分钟", "15 min"), "30min": L("30分钟", "30 min"),
    "60min": L("60分钟", "60 min"),
}


def read_csv_any(path_or_buffer) -> pd.DataFrame:
    """兼容 utf-8(带 BOM) 与 gbk 编码，并去掉 pandas 导出时的无名索引列"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            if hasattr(path_or_buffer, "seek"):
                path_or_buffer.seek(0)
            df = pd.read_csv(path_or_buffer, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Unrecognized CSV encoding (tried utf-8 / gbk) / 无法识别 CSV 编码")
    return df.loc[:, [not str(c).startswith("Unnamed") and str(c).strip() != "" for c in df.columns]]


def is_snapshot(df: pd.DataFrame) -> bool:
    """有 last(最新价) 列就当作快照/Tick 数据"""
    return "last" in [str(c).strip().lower() for c in df.columns]


def snapshot_to_bars(df: pd.DataFrame, rule: str = "1min") -> pd.DataFrame:
    """
    快照 -> K 线
    - 价格用 last（最新价），剔除 last<=0 的无效快照（开盘前/停牌）
    - volume 为当日累计成交量，按交易日差分得到区间成交量
    - K 线时间戳取区间结束时刻（label='right'），避免在回测里提前看到数据
    """
    df = df.rename(columns={c: COLUMN_ALIASES.get(str(c).strip(), str(c).strip().lower()) for c in df.columns})
    if "datetime" not in df.columns:
        raise KeyError("Snapshot data has no time column (trade_time / datetime / 时间) / 快照数据缺少时间列")

    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime")
    df = df[pd.to_numeric(df["last"], errors="coerce") > 0].copy()
    if df.empty:
        raise ValueError("No valid prices in snapshot data (last is all 0) / 快照数据中没有有效成交价")
    df["last"] = pd.to_numeric(df["last"])

    if "volume" in df.columns:
        cum = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        day = df["datetime"].dt.normalize()
        df["vol_delta"] = cum.groupby(day).diff().fillna(cum).clip(lower=0)
    else:
        df["vol_delta"] = 0.0

    s = df.set_index("datetime")
    grouped = s.resample(rule, closed="left", label="right")
    bars = grouped["last"].ohlc()
    bars["volume"] = grouped["vol_delta"].sum()
    bars = bars.dropna(subset=["close"])  # 没有快照的空 bar（夜间）直接丢弃
    if "volume" in df.columns:
        # 午休、收盘后、开盘前的快照只是重复最新价，没有成交的 bar 也无法撮合，一并丢弃
        bars = bars[bars["volume"] > 0]
    return normalize(bars)


class CSVSource(DataSource):
    key = "csv"
    label = L("本地 CSV 文件（K 线 / 快照 / Tick）", "Local CSV (bars / snapshots / ticks)")
    freqs = ("1d", "60m", "30m", "15m", "5m", "1m")
    description = L("导入本地数据文件。快照/Tick 数据（含 last 列）将重采样为 K 线。",
                    "Import your own data. Snapshot/tick data (with a 'last' column) is resampled into bars.")

    def fetch(self, symbol, start=None, end=None, freq="1d", adjust="", resample=None, **kwargs):
        """symbol 为文件路径（或上传得到的文件对象）"""
        raw = read_csv_any(symbol)
        if is_snapshot(raw):
            df = snapshot_to_bars(raw, resample or "1min")
        else:
            df = normalize(raw)
            if resample:
                df = resample_bars(df, resample)
        return clip_dates(df, start, end)


def resample_bars(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """把细粒度 K 线合成为粗粒度 K 线"""
    g = df.resample(rule, closed="left", label="right")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    })
    return normalize(out.dropna(subset=["close"]))
