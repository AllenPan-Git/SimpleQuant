"""
AKShare 数据源：ETF / 股票 / 指数日线

- ETF、股票：东方财富优先；连不上时改用新浪（data/sina.py），且之后 15 分钟内直接用新浪（data/net.py）
- 指数：新浪优先，中证官网备用。东方财富的指数接口每次要先分页下载整张指数列表，经常失败，不再使用
"""

import pandas as pd

from ..i18n import L, pick
from . import net, sina
from .base import DataSource, normalize, clip_dates

ASSET_TYPES = {"etf": L("ETF基金", "ETF"), "stock": L("A股股票", "A-share stock"), "index": L("指数", "Index")}


class AKShareSource(DataSource):
    key = "akshare"
    label = L("AKShare（免费，日线）", "AKShare (free, daily)")
    freqs = ("1d",)
    description = L("ETF 及股票优先使用东方财富日线（支持复权）；东方财富无法连接时自动改用新浪财经，并在随后 15 分钟内直接使用新浪。"
                    "新浪 ETF 复权价由行情与累计分红计算，与东方财富逐日一致；新浪股票复权为等比复权，与 BaoStock 一致。"
                    "指数优先使用新浪财经，中证指数官网备用。数据库中记录每份数据实际使用的接口。",
                    "ETFs and stocks use Eastmoney daily bars (with price adjustment) first. If Eastmoney is unreachable, "
                    "Sina Finance is used instead, and directly for the next 15 minutes. Sina ETF adjusted prices are rebuilt "
                    "from prices plus cumulative dividends and match Eastmoney day by day; Sina stock adjustment is proportional, "
                    "matching BaoStock. Indices use Sina Finance first, with the CSI website as a fallback. "
                    "The library records which interface each dataset came from.")

    def fetch(self, symbol, start, end, freq="1d", adjust="hfq", asset="etf", **kwargs):
        if freq != "1d":
            raise ValueError("AKShare provides daily bars only; use BaoStock / TDX / CSV for intraday / AKShare 数据源只提供日线，分钟数据请用 BaoStock / 通达信 / CSV")
        if asset == "etf":
            raw, provider = first_available([
                ("eastmoney", lambda: eastmoney(symbol, start, end, adjust, asset)),
                ("sina" + ("+dividends" if adjust else ""), lambda: sina.etf(symbol, adjust)),
            ])
        elif asset == "stock":
            raw, provider = first_available([
                ("eastmoney", lambda: eastmoney(symbol, start, end, adjust, asset)),
                ("sina" + ("+factors" if adjust else ""), lambda: sina.stock(symbol, adjust)),
            ])
        elif asset == "index":
            raw, provider = first_available([
                ("sina", lambda: sina.index(symbol)),
                ("csindex", lambda: csindex(symbol, start, end)),
            ])
        else:
            raise ValueError(f"Unknown asset type / 未知品种类型: {asset}")

        df = clip_dates(normalize(pd.DataFrame(raw)), start, end)
        if df.empty:
            raise ValueError(f"No data for {symbol} in {start} ~ {end} / {symbol} 在该区间内无数据")
        df.attrs["provider"] = provider          # 实际使用的接口，存进数据库元数据供界面显示
        return df


def first_available(tries):
    """依次尝试各个接口，返回第一个有数据的 (数据, 接口名)；都失败时汇总各接口的原因"""
    errors = []
    for name, fn in tries:
        site = name.split("+")[0]
        label = f"{pick(net.SITE_NAMES[site], 'zh')} / {pick(net.SITE_NAMES[site], 'en')}"
        try:
            raw = fn()
        except Exception as e:  # noqa: BLE001 - 换下一个接口
            errors.append(f"[{label}] {e}")
            continue
        if raw is not None and len(raw):
            return raw, name
        errors.append(f"[{label}] no data / 无数据")
    raise RuntimeError("All data interfaces failed; please retry later / 各数据接口均无法获取数据，请稍后重试。\n"
                       + "\n".join(errors))


def eastmoney(symbol, start, end, adjust, asset):
    import akshare as ak
    s, e = start.replace("-", ""), end.replace("-", "")
    get = ak.fund_etf_hist_em if asset == "etf" else ak.stock_zh_a_hist
    return net.call("eastmoney", lambda: get(symbol=symbol, period="daily", start_date=s, end_date=e, adjust=adjust),
                    retries=3, wait=2.0)


def csindex(symbol, start, end):
    """中证指数官网（只有中证系列指数，如 000300、000905、000852、93 开头的）"""
    import akshare as ak
    raw = net.call("csindex", lambda: ak.stock_zh_index_hist_csindex(
        symbol=symbol, start_date=start.replace("-", ""), end_date=end.replace("-", "")), retries=2, wait=2.0)
    raw = pd.DataFrame(raw)
    if raw.empty:
        return raw
    out = raw.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
                              "成交量": "volume"})[["date", "open", "high", "low", "close", "volume"]]
    out = out.apply(lambda c: c if c.name == "date" else pd.to_numeric(c, errors="coerce"))
    out["volume"] = out["volume"] / 100       # 股 → 手，与新浪、东方财富一致
    # 起始日为节假日时，会多出一行与下一交易日相同的数据
    out = out.drop_duplicates(subset=["close", "volume"], keep="last")
    for c in ("open", "high", "low"):        # 部分指数早期只有收盘价
        out[c] = out[c].fillna(out["close"])
    return out
