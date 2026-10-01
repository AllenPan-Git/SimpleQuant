"""
择时回测的「现金分红」模式（选股回测的同名选项见 stocks/dividends.py）

默认用后复权数据：分红全额再投资、不扣税。现金分红模式把每个标的的行情换成
    S = 不复权价 × 累计送转因子 G × 累计分红因子 A（等比复权；第一根 K 线的 S 等于不复权价）
S 在除权除息日是连续的，策略的指标和信号不受除息影响；账户则按现金分红记账：
    - 除息日开盘前，持仓乘以 keep = A(前一日) / A(当日)，缩掉的部分就是分红；税前现金同时到账，
      每「行情股」的现金 = 每股现金 × G × A（取前一日的值，即登记日的持股）
    - 股票卖出时按持股期限扣红利税，应税所得 = (每股现金 + 每股送股 × 1 元面值) × G × A；ETF 等基金分红个人免税
    - 现金到账后不再自动买回，要等策略下次买入时才用上
行情上多出三列，标在除息日的第一根 K 线上：div_keep（其余为 1）、div_cash、div_taxable（其余为 0）。
有这三列的标的由回测引擎按上面的规则处理（engine/base_strategy.py）。

数据：
    - 不复权行情：同一数据源、同一周期重新下载，缓存在 data_cache/unadjusted/；本身就是不复权的数据直接用
    - 分红：股票用 BaoStock（与选股共用 data_cache/stocks/div/）；ETF / LOF 用新浪的累计分红（data_cache/fund_div/）
分红数据漏记的：股票按 BaoStock 复权因子和交易所除权参考价补上（与选股相同，配股视同全额参与），
其余（ETF，或股票补完仍对不上的）按原复权价推算补上；结果页列出补上的日期。
以下情况保留原数据（仍按分红再投资），结果页会列出：CSV、指数、下载失败、
与原复权数据对不上（如 ETF 份额折算、股票配股，这两类不在分红数据里）
"""

import datetime as dt

import numpy as np
import pandas as pd

from ..paths import CACHE_DIR
from . import library
from .base import exchange_prefix, with_retry

UNADJ_DIR = CACHE_DIR / "unadjusted"
FUND_DIV_DIR = CACHE_DIR / "fund_div"
EMPTY = pd.DataFrame({"ex_date": pd.Series(dtype="datetime64[ns]"),
                      "cash": pd.Series(dtype=float), "bonus": pd.Series(dtype=float), "reserve": pd.Series(dtype=float)})
COLUMNS = ("div_keep", "div_cash", "div_taxable")
# 与原复权数据的单日涨跌幅相差超过这么多：有分红数据以外的除权（漏记的分红、份额折算、配股）。
# BaoStock 是等比复权，正常只差舍入误差；东方财富是加法复权（复权价 = 不复权价 + 累计分红），
# 涨跌幅本身就会被压缩一点（510300 实测单日最多差 1.4%），只能放宽
MAX_GAP_ADJUSTED = 0.03
GAP_TOL = {"baostock": 0.003}
MAX_MOVE_RAW = 0.25          # 原数据不复权、没法对比时：单日涨跌超过这么多视为未知的除权


class Unsupported(Exception):
    """这个标的不能用现金分红模式；args[0] 是原因代码（界面里是 cd.reason_<代码>）"""


# ---------------- 品种 ----------------
def asset_kind(meta: library.DatasetMeta) -> str | None:
    """stock 股票 / fund ETF、LOF 等场内基金 / index 指数 / None 不认识（CSV 等）"""
    if meta.source == "csv":
        return None
    asset = (meta.extra or {}).get("asset")
    if asset == "index":
        return "index"
    if asset == "etf":
        return "fund"
    if asset == "stock":
        return "stock"
    s = str(meta.symbol).strip()
    if len(s) != 6 or not s.isdigit():
        return None
    if s[:2] in ("50", "51", "52", "56", "58", "15", "16", "18"):
        return "fund"
    if s[:2] in ("60", "68", "00", "30"):
        return "stock"
    if s[:2] in ("39", "88", "99"):
        return "index"
    return None


# ---------------- 分红数据 ----------------
def fund_events_from_cumulative(cum: pd.DataFrame) -> pd.DataFrame:
    """新浪基金累计分红表（日期, 累计每份分红）→ 每次分红的事件表"""
    if cum is None or cum.empty:
        return EMPTY.copy()
    df = cum.iloc[:, :2].set_axis(["ex_date", "cum"], axis=1)
    df["ex_date"] = pd.to_datetime(df["ex_date"], errors="coerce")
    df["cum"] = pd.to_numeric(df["cum"], errors="coerce")
    df = df.dropna().sort_values("ex_date").drop_duplicates("ex_date", keep="last")
    cash = df["cum"].diff().fillna(df["cum"]).round(6)
    out = pd.DataFrame({"ex_date": df["ex_date"].values, "cash": cash.values, "bonus": 0.0, "reserve": 0.0})
    return out[out["cash"] > 0].reset_index(drop=True)


def fund_dividends(symbol: str, today: dt.date | None = None) -> pd.DataFrame:
    """ETF / LOF 的分红（新浪）。每天最多下载一次；下载失败时用上次缓存的"""
    today = today or dt.date.today()
    path = FUND_DIV_DIR / f"{symbol}.parquet"
    if path.exists() and dt.date.fromtimestamp(path.stat().st_mtime) >= today:
        return pd.read_parquet(path)
    try:
        import akshare as ak
        full = exchange_prefix(symbol) + symbol
        df = fund_events_from_cumulative(pd.DataFrame(with_retry(lambda: ak.fund_etf_dividend_sina(symbol=full))))
    except Exception:  # noqa: BLE001 - 网络问题：有缓存就用缓存
        if path.exists():
            return pd.read_parquet(path)
        raise Unsupported("no_dividends")
    FUND_DIV_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df


def stock_dividends(symbol: str, start_year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    股票的分红送转（BaoStock，与选股共用存储），加上分红表漏记、按复权因子补上的（stocks/dividends.missing_events，
    要用该股的 BaoStock 日线，没有时顺带下载）。返回 (全部事件, 补上的部分)
    """
    from ..stocks.store import StockStore
    from ..stocks.dividends import with_missing
    store, code = StockStore(), f"{exchange_prefix(symbol)}.{symbol}"
    try:
        store.update_dividends([code], start_year, workers=1)
    except Exception:  # noqa: BLE001 - 下载失败：有旧数据就用旧的
        pass
    if not store.has_div(code):
        raise Unsupported("no_dividends")
    div = store.load_div(code)
    try:
        store.update([code], f"{start_year}-01-01", dt.date.today().isoformat(), workers=1)
    except Exception:  # noqa: BLE001 - 补不了漏记的分红：后面还会按原复权价推算
        pass
    if not store.has(code):
        return div, EMPTY.copy()
    rights = store.load_rights()
    return with_missing(div, store.load(code), None if rights is None else rights.get(code, []))


def unadjusted(meta: library.DatasetMeta) -> pd.DataFrame:
    """同一数据源、同一周期、同一区间的不复权行情（缓存）"""
    from . import SOURCES
    ds_id = library.make_id(meta.source, meta.symbol, meta.freq, "", meta.start, meta.end)
    if library.exists(ds_id, UNADJ_DIR):
        return library.load(ds_id, UNADJ_DIR)
    kwargs = {k: v for k, v in (meta.extra or {}).items() if k != "provider"}
    try:
        df = SOURCES[meta.source].fetch(meta.symbol, meta.start, meta.end, freq=meta.freq, adjust="", **kwargs)
    except Exception:  # noqa: BLE001
        raise Unsupported("no_unadjusted") from None
    library.save(df, library.DatasetMeta(id=ds_id, name=meta.name, symbol=meta.symbol, source=meta.source,
                                         freq=meta.freq, extra=kwargs), UNADJ_DIR)
    return df


# ---------------- 计算（纯函数，便于测试） ----------------
def build(orig: pd.DataFrame, raw: pd.DataFrame, events: pd.DataFrame, taxed: bool) -> pd.DataFrame:
    """
    :param orig:   回测原来要用的行情（决定 K 线时间；成交量和因子列沿用它的）
    :param raw:    同一标的的不复权行情
    :param events: 分红事件 ex_date / cash / bonus / reserve（每股，不复权口径）
    :param taxed:  是否扣红利税（股票是，基金否）
    """
    from ..stocks.dividends import align           # stocks 依赖引擎、引擎依赖本模块：延迟导入
    prices = ["open", "high", "low", "close"]
    raw = raw[prices].reindex(orig.index)
    if raw["close"].isna().any():
        raise Unsupported("no_unadjusted")
    day = orig.index.normalize()
    cal = pd.DatetimeIndex(day.unique())
    ev = align(events, cal)
    first = ~day.duplicated()
    close = raw["close"].groupby(day).last()                            # 每天最后一根 K 线的收盘
    g = np.exp(np.log1p(ev["bonus"] + ev["reserve"]).cumsum())          # 累计送转因子
    g_prev = g.shift(1, fill_value=1.0)
    p_prev = (close * g).shift(1)                                       # 前一日收盘（按送转复权）
    d = ev["cash"] * g_prev                                             # 每股现金（按送转复权）
    hit = (d > 0) & p_prev.notna()
    if ((p_prev - d)[hit] <= 0).any():
        raise Unsupported("mismatch")
    jump = pd.Series(1.0, index=cal)
    jump[hit] = p_prev[hit] / (p_prev[hit] - d[hit])
    a = jump.cumprod()
    a_prev = a.shift(1, fill_value=1.0)
    per_share = g_prev * a_prev                                         # 前一日一「行情股」= 多少真实股
    started = pd.Series(np.arange(len(cal)) > 0, index=cal)
    daily = pd.DataFrame({
        "factor": g * a,
        "div_keep": a_prev / a,
        "div_cash": (ev["cash"] * per_share).where(started, 0.0),
        "div_taxable": ((ev["cash"] + ev["bonus"]) * per_share).where(started, 0.0) if taxed else 0.0,
    })
    on_bar = daily.reindex(day)
    on_bar.index = orig.index
    out = orig.copy()
    for c in prices:
        out[c] = raw[c] * on_bar["factor"]
    out["div_keep"] = on_bar["div_keep"].where(first, 1.0)
    for c in ("div_cash", "div_taxable"):
        out[c] = on_bar[c].where(first, 0.0)
    return out


def unexplained(new: pd.DataFrame, orig: pd.DataFrame, orig_adjusted: bool, tol: float = MAX_GAP_ADJUSTED
                ) -> list[pd.Timestamp]:
    """分红数据解释不了的跳空（漏记的分红、份额折算、配股等）所在的日期"""
    day = new.index.normalize()
    ret = new["close"].groupby(day).last().pct_change()
    if orig_adjusted:
        gap = (ret - orig["close"].groupby(day).last().pct_change()).abs()
        bad = gap > tol
    else:
        bad = ret.abs() > MAX_MOVE_RAW
        bad.iloc[:6] = False                      # 新股上市前几天没有涨跌幅限制
    return list(ret.index[bad.fillna(False)])


def implied_events(orig: pd.DataFrame, raw: pd.DataFrame, events: pd.DataFrame, days: list) -> pd.DataFrame:
    """
    复权价里有、分红数据里漏掉的现金分红（如 BaoStock 的分红表没有贵州茅台 2022、2023 年的特别分红）：
    按当天复权价的涨跌推算 每股现金 = 前收 × 复权价涨跌 − 收盘 × (1 + 送转) − 已记录的现金。
    推算出来不像现金分红的（≤0，如配股；或超过前收一半，如份额折算）不补，留给 unexplained 处理
    """
    from ..stocks.dividends import align
    day = orig.index.normalize()
    u = raw["close"].reindex(orig.index).groupby(day).last()
    h = orig["close"].groupby(day).last()
    ev = align(events, pd.DatetimeIndex(u.index))
    rows = []
    for t in days:
        i = u.index.get_loc(t)
        if i == 0:
            continue
        cash = (u.iloc[i - 1] * h.iloc[i] / h.iloc[i - 1]
                - u.iloc[i] * (1 + ev["bonus"].iloc[i] + ev["reserve"].iloc[i]) - ev["cash"].iloc[i])
        if 0 < cash < 0.5 * u.iloc[i - 1]:
            rows.append({"ex_date": t, "cash": round(float(cash), 4), "bonus": 0.0, "reserve": 0.0})
    return pd.DataFrame(rows, columns=EMPTY.columns).astype(EMPTY.dtypes.to_dict())


def events_of(df: pd.DataFrame) -> dict:
    """行情上的分红列 → {日期: (keep, 每行情股现金, 每行情股应税所得)}（引擎用）"""
    hit = (df["div_keep"] != 1.0) | (df["div_cash"] > 0) | (df["div_taxable"] > 0)
    rows = df.loc[hit, list(COLUMNS)]
    return {ts.date(): tuple(map(float, r)) for ts, r in zip(rows.index, rows.itertuples(index=False))}


def has_columns(df: pd.DataFrame) -> bool:
    return "div_keep" in df.columns


def rebase_to_last(out: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    """
    整体缩放，使最后一根 K 线的价格等于不复权价（模拟盘用：最新的一「行情股」= 一股真实股票，
    信号的股数可以直接照着下单；和前复权的道理一样）。价格和每行情股的现金、应税所得同比例缩放，持仓保留比例不变
    """
    c = float(out["close"].iloc[-1] / raw["close"].reindex(out.index).iloc[-1])
    out = out.copy()
    for col in ("open", "high", "low", "close", "div_cash", "div_taxable"):
        out[col] = out[col] / c
    return out


# ---------------- 入口 ----------------
def prepare_one(meta: library.DatasetMeta, df: pd.DataFrame, raw: pd.DataFrame | None = None
                ) -> tuple[pd.DataFrame, list]:
    """
    把一个标的的行情换成现金分红模式；返回 (行情, 按复权价推算补上的分红日期)。
    raw：已经取好的不复权行情（模拟盘每天现取，不进缓存）；不给时按 meta 下载并缓存。
    不支持时抛 Unsupported（原因代码）
    """
    kind = asset_kind(meta)
    if kind is None:
        raise Unsupported("source")
    if kind == "index":
        raise Unsupported("index")
    start_year = int(meta.start[:4]) - 1 if meta.start else 2000
    if kind == "stock":
        events, extra = stock_dividends(meta.symbol, start_year)
    else:
        events, extra = fund_dividends(meta.symbol), EMPTY
    adjusted, taxed = bool(meta.adjust), kind == "stock"
    tol = GAP_TOL.get(meta.source, MAX_GAP_ADJUSTED)
    if raw is None:
        raw = unadjusted(meta) if adjusted else df
    out = build(df, raw, events, taxed)
    patched = [t.date() for t in extra["ex_date"] if df.index[0] <= t <= df.index[-1]]
    bad = unexplained(out, df, adjusted, tol)
    if bad and adjusted:
        extra = implied_events(df, raw, events, bad)
        if len(extra):
            out = build(df, raw, pd.concat([events, extra], ignore_index=True), taxed)
            patched += [t.date() for t in extra["ex_date"]]
            bad = unexplained(out, df, adjusted, tol)
    if bad:
        raise Unsupported("mismatch")
    return out, patched


def prepare(items: list[tuple[str, library.DatasetMeta, pd.DataFrame]]) -> tuple[dict, dict, dict]:
    """
    items：(名称, 数据元信息, 已按回测区间截取的行情)
    返回 (名称 -> 行情, 名称 -> 没能改用现金分红的原因代码, 名称 -> 推算补上的分红日期)；
    不支持的标的保留原行情
    """
    prices, skipped, patched = {}, {}, {}
    for name, meta, df in items:
        try:
            prices[name], dates = prepare_one(meta, df)
            if dates:
                patched[name] = dates
        except Unsupported as e:
            prices[name], skipped[name] = df, e.args[0]
    return prices, skipped, patched
