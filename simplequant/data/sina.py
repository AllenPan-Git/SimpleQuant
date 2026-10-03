"""
新浪财经日线（东方财富连不上时的备用；指数为首选）

所有数据都在 finance.sina.com.cn/realstock/company/<sh600519>/ 下：
    hisdata_klc2/klc_kl.js   股票 / ETF 不复权日线（加密，用 akshare 带的解码脚本在 V8 里解开）
    hisdata/klc_kl.js        指数日线
    hfq.js                   复权表：股票为后复权因子 f；ETF 的 f 恒为 1，u 为累计每份分红
另有流通股本表（计算换手率）。每只标的的日线都是一次返回全部历史。

不直接用 akshare 的对应函数：它们不设超时、不检查 HTTP 状态（被封时无法识别），并用 eval 解析复权表。
复权口径：
- 股票：等比复权（不复权价 × 后复权因子），与 BaoStock 一致（600519、300750 逐日收益率一致）
- ETF：加法复权（不复权价 + 累计分红），与东方财富一致（510300 共 1635 个交易日，误差不超过 0.001）
成交量换算成「手」、换手率为百分比，与东方财富一致。
"""

import json
import threading
import time

import pandas as pd

from . import net
from .base import exchange_prefix

BASE = "https://finance.sina.com.cn/realstock/company/{}/"
SHARES_URL = ("https://stock.finance.sina.com.cn/stock/api/jsonp.php/var%20KKE_ShareAmount_{0}=/"
              "StockService.getAmountBySymbol?_=20&symbol={0}")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36",
           "Referer": "https://finance.sina.com.cn/"}
TIMEOUT = 20
CACHE_SECONDS = 10 * 60          # 同一文件 10 分钟内不重复下载（如模拟盘先取前复权、再取不复权）

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def get(url: str) -> str:
    """一次请求（限速、暂停与重试在 net.call 里）"""
    import requests
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code in (403, 456):
        raise net.Blocked(f"Sina HTTP {r.status_code}")
    if r.status_code == 404:
        raise net.NotFound(url)
    r.raise_for_status()
    return r.text


def fetch_text(url: str) -> str:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    text = net.call("sina", lambda: get(url), retries=3, wait=2.0)
    with _cache_lock:
        _cache[url] = (time.time(), text)
    return text


def clear_cache():
    with _cache_lock:
        _cache.clear()


def symbol_of(symbol: str, asset: str) -> str:
    """6 位代码 → 新浪代码。指数：399 开头为深市、899 开头为北交所，其余（000 / 93 等）为沪市"""
    if asset == "index":
        return ("sz" if symbol.startswith("399") else "bj" if symbol.startswith("899") else "sh") + symbol
    return exchange_prefix(symbol) + symbol


def decode_kline(text: str) -> pd.DataFrame:
    import py_mini_racer
    from akshare.stock.cons import hk_js_decode
    try:
        payload = text.split("=", 1)[1].split(";")[0].replace('"', "")
    except IndexError:
        raise ValueError("unexpected response from Sina / 新浪返回的内容无法解析") from None
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(hk_js_decode)
    df = pd.DataFrame(ctx.call("d", payload))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    df = df.dropna(subset=["date"]).drop_duplicates("date", keep="last").set_index("date").sort_index()
    return df[[c for c in ("open", "high", "low", "close", "volume", "amount") if c in df]].apply(
        pd.to_numeric, errors="coerce")


def _json_between(text: str, left: str, right: str):
    try:
        return json.loads(text[text.index(left): text.rindex(right) + 1])
    except ValueError:
        raise ValueError("unexpected response from Sina / 新浪返回的内容无法解析") from None


def kline(sym: str, index: bool = False) -> pd.DataFrame:
    path = "hisdata/klc_kl.js" if index else "hisdata_klc2/klc_kl.js"
    try:
        text = fetch_text(BASE.format(sym) + path)
    except net.NotFound:
        raise ValueError(f"Sina has no data for {sym} / 新浪无 {sym} 的行情") from None
    return decode_kline(text)


def adjust_table(sym: str) -> pd.DataFrame:
    """复权表：index 为日期（当天起生效），列 f（后复权因子）、u（累计每份分红，仅基金有）"""
    try:
        data = _json_between(fetch_text(BASE.format(sym) + "hfq.js"), "{", "}").get("data") or []
    except net.NotFound:            # 从未分红 / 除权的品种可能没有这个文件
        data = []
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["f", "u"], index=pd.DatetimeIndex([], name="date"))
    df["d"] = pd.to_datetime(df["d"], errors="coerce")
    df = df[df["d"] > pd.Timestamp("1901-01-01")].dropna(subset=["d"])     # 去掉 1900-01-01 占位行
    df = df.set_index("d").rename_axis("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[[c for c in ("f", "u") if c in df]].apply(pd.to_numeric, errors="coerce")


def float_shares(sym: str) -> pd.Series:
    """流通股本（股），index 为变动日期"""
    rows = _json_between(fetch_text(SHARES_URL.format(sym)), "[", "]")
    s = pd.DataFrame(rows)
    s["date"] = pd.to_datetime(s["date"], errors="coerce")
    s = s.dropna(subset=["date"]).set_index("date").sort_index()["amount"].astype(float) * 1e4
    return s[~s.index.duplicated(keep="last")]


def as_of(table: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """每个交易日适用的值（当天及以前最近一次变动）；第一次变动之前为 NaN"""
    return table.reindex(dates.union(table.index)).ffill().reindex(dates)


def fund_dividends(symbol: str) -> pd.DataFrame:
    """ETF / LOF 累计分红表（日期, 累计每份分红），供 cash_dividend 使用"""
    table = adjust_table(symbol_of(symbol, "etf"))
    if "u" not in table:
        return pd.DataFrame(columns=["date", "cum"])
    return pd.DataFrame({"date": table.index, "cum": table["u"].values})


def etf(symbol: str, adjust: str = "") -> pd.DataFrame:
    """
    ETF 日线。复权按东方财富的口径（加法调整）：
    - 后复权 = 不复权价 + 截至当天（含除息日）的累计分红
    - 前复权 = 不复权价 − 当天之后还会发生的分红
    """
    sym = symbol_of(symbol, "etf")
    df = kline(sym)
    if df.empty:
        return df
    df["volume"] = df["volume"] / 100
    if not adjust:
        return df
    _check_adjust(adjust)
    table = adjust_table(sym)
    if "u" in table and table["u"].notna().any():
        cum = as_of(table["u"], df.index).fillna(0.0)
        total = float(table["u"].dropna().iloc[-1])
    else:
        cum, total = pd.Series(0.0, index=df.index), 0.0
    shift = cum if adjust == "hfq" else cum - total
    for c in ("open", "high", "low", "close"):
        df[c] = (df[c] + shift).round(3)
    return df


def stock(symbol: str, adjust: str = "") -> pd.DataFrame:
    """A 股日线，等比复权；带换手率（%，按流通股本）"""
    sym = symbol_of(symbol, "stock")
    df = kline(sym)
    if df.empty:
        return df
    try:
        df["turnover"] = df["volume"] / as_of(float_shares(sym), df.index) * 100
    except Exception:  # noqa: BLE001 - 换手率只是附带的因子，取不到不影响行情
        pass
    df["volume"] = df["volume"] / 100
    if not adjust:
        return df
    _check_adjust(adjust)
    table = adjust_table(sym)
    if "f" not in table or table["f"].dropna().empty:
        raise ValueError(f"No adjustment factors for {symbol} from Sina / 新浪无 {symbol} 的复权因子")
    f = as_of(table["f"], df.index)
    if adjust == "qfq":
        f = f / float(table["f"].dropna().iloc[-1])           # 最新价格等于真实价格
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * f
    return df.dropna(subset=["close"])


def index(symbol: str) -> pd.DataFrame:
    df = kline(symbol_of(symbol, "index"), index=True)
    if not df.empty:
        df["volume"] = df["volume"] / 100
    return df


def _check_adjust(adjust: str):
    if adjust not in ("qfq", "hfq"):
        raise ValueError(f"Unknown adjust / 未知复权方式: {adjust}")
