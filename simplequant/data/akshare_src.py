"""
AKShare 数据源：ETF / 股票 / 指数日线（东方财富连不上时 ETF 改用新浪）
"""

import pandas as pd

from ..i18n import L
from .base import DataSource, normalize, with_retry, exchange_prefix, clip_dates

ASSET_TYPES = {"etf": L("ETF基金", "ETF"), "stock": L("A股股票", "A-share stock"), "index": L("指数", "Index")}


class AKShareSource(DataSource):
    key = "akshare"
    label = L("AKShare（免费，日线）", "AKShare (free, daily)")
    freqs = ("1d",)
    description = L("东方财富日线，ETF 及股票支持复权。东方财富接口无法连接时，ETF 自动切换至新浪：不复权直接使用新浪行情；复权数据由新浪行情加新浪累计分红计算，与东方财富的复权价逐日一致（510300 实测 1635 个交易日，误差不超过 0.001）。股票复权请求仍需东方财富。",
                    "Eastmoney daily bars with price adjustment for ETFs/stocks. If Eastmoney is unreachable, ETFs fall back to Sina: unadjusted bars are used as-is, and adjusted bars are rebuilt from Sina prices plus Sina's cumulative dividends, matching Eastmoney's adjusted prices day by day (checked on 510300: 1,635 trading days, within 0.001). Adjusted stock data still requires Eastmoney.")

    def fetch(self, symbol, start, end, freq="1d", adjust="hfq", asset="etf", **kwargs):
        import akshare as ak

        if freq != "1d":
            raise ValueError("AKShare provides daily bars only; use BaoStock / TDX / CSV for intraday / AKShare 数据源只提供日线，分钟数据请用 BaoStock / 通达信 / CSV")
        s, e = start.replace("-", ""), end.replace("-", "")

        provider = "eastmoney"
        if asset == "etf":
            try:
                raw = with_retry(lambda: ak.fund_etf_hist_em(
                    symbol=symbol, period="daily", start_date=s, end_date=e, adjust=adjust), retries=4, wait=2.0)
            except RuntimeError as err:
                try:
                    raw = sina_etf(symbol, adjust)
                except Exception as err2:  # noqa: BLE001
                    raise RuntimeError(
                        f"Eastmoney and Sina are both unreachable; retry later / 东方财富和新浪接口都暂时连不上"
                        f"（已重试），请稍后再试。({err}; {err2})") from err
                provider = "sina" + ("+dividends" if adjust else "")
        elif asset == "stock":
            raw = with_retry(lambda: ak.stock_zh_a_hist(
                symbol=symbol, period="daily", start_date=s, end_date=e, adjust=adjust))
        elif asset == "index":
            raw = with_retry(lambda: ak.index_zh_a_hist(
                symbol=symbol, period="daily", start_date=s, end_date=e))
        else:
            raise ValueError(f"Unknown asset type / 未知品种类型: {asset}")

        if raw is None or raw.empty:
            raise ValueError(f"No data for {symbol} in {start} ~ {end} / {symbol} 在该区间内无数据")
        df = clip_dates(normalize(pd.DataFrame(raw)), start, end)
        df.attrs["provider"] = provider          # 实际使用的接口，存进数据库元数据供界面显示
        return df


def sina_etf(symbol: str, adjust: str = "") -> pd.DataFrame:
    """新浪 ETF 日线（东方财富连不上时的备用）。
    成交量换算成「手」，与东方财富一致。复权用新浪的累计分红表按东方财富的口径计算（加法调整）：
    - 后复权 = 不复权价 + 截至当天（含除息日）的累计分红
    - 前复权 = 不复权价 − 当天之后还会发生的分红
    """
    import akshare as ak
    full = exchange_prefix(symbol) + symbol
    raw = pd.DataFrame(with_retry(lambda: ak.fund_etf_hist_sina(symbol=full))).copy()
    if raw.empty:
        return raw
    if "volume" in raw:
        raw["volume"] = raw["volume"] / 100
    if not adjust:
        return raw
    if adjust not in ("qfq", "hfq"):
        raise ValueError(f"Unknown adjust / 未知复权方式: {adjust}")
    div = pd.DataFrame(with_retry(lambda: ak.fund_etf_dividend_sina(symbol=full)))
    dates = pd.to_datetime(raw["date"])
    if div.empty:
        cum = pd.Series(0.0, index=raw.index)
        total = 0.0
    else:
        div = div.iloc[:, :2].set_axis(["date", "cum"], axis=1)
        div["date"] = pd.to_datetime(div["date"])
        div = div.sort_values("date")
        idx = div["date"].searchsorted(dates, side="right") - 1          # 当天及以前最近一次除息
        cum = pd.Series([float(div["cum"].iloc[i]) if i >= 0 else 0.0 for i in idx], index=raw.index)
        total = float(div["cum"].iloc[-1])
    shift = cum if adjust == "hfq" else cum - total
    for c in ("open", "high", "low", "close"):
        raw[c] = (raw[c].astype(float) + shift).round(3)
    return raw
