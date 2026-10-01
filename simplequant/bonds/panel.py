"""
可转债面板：把各转债日线拼成"日期 × 代码"的宽表（与股票共用 stocks.panel.Panel，选股、因子研究、回测直接复用）

- 日历：中证转债指数的交易日
- 可选范围：已上市、当天有新浪成交数据、还没到「必须卖出日」
- 必须卖出日（exit）：
    强赎或到期赎回（东方财富 EXECUTE_REASON_SH = 4 / 5）：赎回公告日的下一个交易日起，持有的在开盘卖出；
    公告当天收盘起不再入选。公告日在当时就已知，模拟盘逐日运行与一次性回测一致
    到期但赎回公告晚于最后交易日的：最后交易日开盘卖出（到期日在条款里事先可知）
    停牌、违约、正股退市等意外停止交易的：不卖出，持仓按最后价格挂着卖不出（当时无法预知，与实际一致）
  股票退市后持仓一直挂着卖不出；转债大多以强赎退市，必须在退市前卖掉，否则回测会失真
- 复权：转债按全价交易，付息日价格下跳（光大转债 2022-03-17 实测）。按条款里的各年票面利率生成付息事件，
  按个人投资者税后（扣 20%）计入后复权因子，与股票后复权的口径相同（利息再投资）；最后一年的利息含在到期赎回价里
- 涨跌幅：2022-08-01 起 ±20%，上市首日 +57.3% / −43.3%（相对面值 100）；之前没有涨跌幅限制（只有临时停牌）
- 每手 10 张；成交额 = 成交量 × 收盘价（新浪没有成交额，作为近似）
"""

import numpy as np
import pandas as pd

from ..i18n import L
from ..stocks.panel import Panel
from .store import CBStore, REDEEM_CALL, REDEEM_MATURITY

LIMIT_FROM = pd.Timestamp("2022-08-01")
LIMIT_PCT = 0.20
FIRST_DAY_UP, FIRST_DAY_DOWN = 157.3, 56.7
COUPON_TAX = 0.20
LOT = 10
STOCK_INDUSTRY_PREFIX = {"6": "sh.", "9": "sh.", "0": "sz.", "2": "sz.", "3": "sz.", "4": "bj.", "8": "bj."}


def coupon_events(info_row, calendar: pd.DatetimeIndex, last_trade) -> list[tuple[pd.Timestamp, float]]:
    """[(除息日, 每张税后利息)]：付息日（起息日的周年日）当天或之后的第一个交易日；只算还在交易的年份"""
    coupons = [float(x) for x in str(info_row.get("coupons") or "").split(",") if x]
    value_date = info_row.get("value_date")
    if not coupons or value_date is None or pd.isna(value_date) or last_trade is None:
        return []
    out = []
    for k, rate in enumerate(coupons[:-1], start=1):          # 最后一年的利息含在到期赎回价里
        pay = pd.Timestamp(value_date) + pd.DateOffset(years=k)
        if pay > last_trade:
            break
        if pay <= calendar[0]:              # 面板开始之前的付息（复权因子从面板第一天起算）
            continue
        i = calendar.searchsorted(pay)
        if i < len(calendar) and calendar[i] <= last_trade:
            out.append((calendar[i], rate * (1 - COUPON_TAX)))
    return out


def adj_factor(raw_close: pd.Series, events) -> pd.Series:
    """后复权因子：除息日起乘以 前收 / (前收 − 税后利息)"""
    f = pd.Series(1.0, index=raw_close.index)
    prev = raw_close.ffill().shift(1)
    for day, cash in events:
        pc = prev.get(day)
        if pc is None or not np.isfinite(pc) or pc <= cash:
            continue
        f.loc[day:] *= pc / (pc - cash)
    return f


RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-"]


def rating_score(r) -> float:
    """AAA = 10 … BBB- = 1，更低或缺失为 0"""
    r = str(r or "").strip().upper()
    return float(len(RATINGS) - RATINGS.index(r)) if r in RATINGS else 0.0


def _amount_ma(p, n=20):
    return p["amount"].rolling(n, min_periods=max(5, n // 2)).mean()


# 选股条件（值为空表示不限）
CB_FILTERS = {
    "max_price": {"label": L("价格上限（元）", "Max price (CNY)"), "default": 130.0,
                  "help": L("收盘价高于该值的不选。高价转债往往已触发或临近强赎",
                            "Bonds closing above this are skipped; high prices usually mean a call is near"),
                  "fn": lambda p, v: p["raw_close"] <= v},
    "min_amount": {"label": L("日均成交额下限（万元，20 日）", "Min avg turnover (10k CNY, 20d)"), "default": 1000.0,
                   "help": L("过去 20 个交易日平均成交额低于该值的不选，避免流动性太差",
                             "Skips bonds whose 20-day average turnover is below this"),
                   "fn": lambda p, v: _amount_ma(p) >= v * 1e4},
    "min_remain_years": {"label": L("剩余期限下限（年）", "Min years to maturity"), "default": 0.5,
                         "help": L("临近到期的转债价格贴近赎回价，弹性很小",
                                   "Bonds close to maturity trade near the redemption price"),
                         "fn": lambda p, v: p["remain_years"] >= v},
    "max_premium": {"label": L("转股溢价率上限（%）", "Max conversion premium (%)"), "default": None,
                    "help": L("转股溢价率高于该值的不选", "Skips bonds whose conversion premium exceeds this"),
                    "fn": lambda p, v: p["premium"] <= v},
}
CB_DEFAULT_FILTERS = {"exclude_st": False, "min_list_days": 0,
                      **{k: m["default"] for k, m in CB_FILTERS.items()}}


def cb_filters(filters: dict | None) -> dict:
    """可转债选股条件：补上默认值"""
    return {**CB_DEFAULT_FILTERS, **(filters or {})}


def _limits(raw_open, raw_preclose, first_day):
    """开盘能否买 / 卖（涨跌停）：2022-08-01 起 ±20%，首日 157.3 / 56.7"""
    dates = raw_open.index
    limited = pd.DataFrame(np.repeat((dates >= LIMIT_FROM)[:, None], raw_open.shape[1], axis=1),
                           index=dates, columns=raw_open.columns)
    up = (raw_preclose * (1 + LIMIT_PCT)).round(3).where(~first_day, FIRST_DAY_UP)
    down = (raw_preclose * (1 - LIMIT_PCT)).round(3).where(~first_day, FIRST_DAY_DOWN)
    ok_buy = ~limited | up.isna() | (raw_open < up - 0.001)
    ok_sell = ~limited | down.isna() | (raw_open > down + 0.001)
    return ok_buy, ok_sell


def _stock_industry(stock_codes: pd.Series) -> pd.Series:
    """用正股的证监会行业（选股数据里的 industry.parquet，全市场）；没下载过时为空"""
    try:
        from ..stocks import StockStore
        ind = StockStore().load_industry()
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=str)
    if not len(ind):
        return pd.Series(dtype=str)
    full = stock_codes.map(lambda s: STOCK_INDUSTRY_PREFIX.get(str(s)[:1], "sz.") + str(s))
    return full.map(ind).dropna()


def build_cb_panel(store: CBStore, start: str, end: str) -> Panel:
    bench = store.load_index()
    calendar = bench.loc[start:end].index
    if not len(calendar):
        raise ValueError("no index data in range / 区间内没有中证转债指数数据")
    info = store.load_info()
    lst = store.load_list().set_index("code")
    codes = [c for c in store.wanted(start) if store.has(c)]
    last_day = calendar[-1]

    raw = {f: {} for f in ("raw_open", "raw_high", "raw_low", "raw_close", "volume", "bond_value", "conv_value",
                           "conv_price", "remain_size")}
    tradable, adj, exit_dates, listing, names, first_day = {}, {}, {}, {}, {}, {}
    for c in codes:
        full = store.load(c)
        traded = full.index[(full["sina"] > 0) & full["raw_close"].notna()]
        if not len(traded) or traded[0] > last_day:
            continue
        last_trade = traded[-1]
        df = full.reindex(calendar)
        known = df["raw_close"].notna()
        if not known.any():
            continue
        row = info.loc[c] if c in info.index else pd.Series(dtype=object)
        for f in ("raw_open", "raw_high", "raw_low", "raw_close"):
            raw[f][c] = df[f].ffill()                      # 停牌 / 退市后沿用最后的价格
        for f in ("volume", "bond_value", "conv_value", "conv_price", "remain_size"):
            raw[f][c] = df[f]
        tradable[c] = (df["sina"] > 0) & df["raw_close"].notna() & (df["volume"].fillna(0) > 0)
        adj[c] = adj_factor(raw["raw_close"][c], coupon_events(row, calendar, last_trade))

        # 必须卖出日
        exit_day = None
        notice = row.get("redeem_notice")
        redeem = str(row.get("redeem_reason", "")) in (REDEEM_CALL, REDEEM_MATURITY)
        if redeem and notice is not None and pd.notna(notice):
            i = calendar.searchsorted(pd.Timestamp(notice), side="right")
            if i < len(calendar):
                exit_day = calendar[i]
            elif notice <= last_day + pd.Timedelta(days=1):
                exit_day = last_day + pd.Timedelta(days=1)     # 公告在最后一天：明天开盘卖（模拟盘）
        # 到期：最后交易日在条款里事先可知，当天开盘卖出（个别转债的到期赎回公告晚于最后交易日）。
        # 停牌、违约、正股退市等意外停止交易的不卖：当时无法预知，持仓按最后价格挂着卖不出，与实际一致
        maturity = row.get("maturity")
        matured = maturity is not None and pd.notna(maturity) and \
            last_trade >= pd.Timestamp(maturity) - pd.Timedelta(days=45)
        if matured and last_trade < last_day and last_trade in calendar:
            exit_day = min(exit_day, last_trade) if exit_day is not None else last_trade
        if exit_day is not None:
            exit_dates[c] = exit_day
        listing[c] = row.get("listing_date") if pd.notna(row.get("listing_date", pd.NaT)) else traded[0]
        names[c] = row.get("name") if isinstance(row.get("name"), str) else lst["name"].get(c, c)
        first_day[c] = pd.Series(calendar == traded[0], index=calendar)

    codes = sorted(tradable)
    if not codes:
        raise ValueError("no convertible bond data in range / 区间内没有可转债数据")

    def frame(d):
        return pd.DataFrame(d, index=calendar, columns=codes)

    fields = {f: frame(v) for f, v in raw.items()}
    trad = frame(tradable).fillna(False).astype(bool)
    factor = frame(adj).fillna(1.0)
    fields["adj_factor"] = factor
    for f in ("open", "high", "low", "close"):
        fields[f] = fields[f"raw_{f}"] * factor
    rc = fields["raw_close"]
    fields["raw_preclose"] = rc.shift(1)
    fields["amount"] = fields["volume"] * rc
    fields["turnover"] = rc * np.nan
    fields["pct_chg"] = (rc / fields["raw_preclose"] - 1) * 100
    fields["premium"] = (rc / fields["conv_value"] - 1) * 100
    fields["bond_premium"] = (rc / fields["bond_value"] - 1) * 100
    fields["double_low"] = rc + fields["premium"]
    stock_price = fields["conv_value"] * fields["conv_price"] / 100       # 转股价值 = 100 / 转股价 × 正股价
    fields["stock_close"] = stock_price.where(stock_price > 0)
    info_r = info.reindex(codes)
    maturity = pd.to_datetime(info_r["maturity"]) if "maturity" in info_r else pd.Series(pd.NaT, index=codes)
    days_left = (maturity.values[None, :] - calendar.values[:, None]).astype("timedelta64[D]").astype(float)
    fields["remain_years"] = pd.DataFrame(days_left / 365.25, index=calendar, columns=codes)
    issue = info_r["issue_size"].fillna(lst["issue_size"].reindex(codes)).astype(float)
    fields["issue_size"] = pd.DataFrame(np.repeat(issue.values[None, :], len(calendar), axis=0),
                                        index=calendar, columns=codes)
    rating = info_r["rating"].fillna(lst["rating"].reindex(codes)).map(rating_score)
    fields["rating_score"] = pd.DataFrame(np.repeat(rating.values[None, :], len(calendar), axis=0),
                                          index=calendar, columns=codes)

    exit_df = pd.DataFrame(False, index=calendar, columns=codes)
    no_pick = exit_df.copy()              # 必须卖出日的前一个交易日（强赎公告当天）收盘起就不再入选
    for c, d in exit_dates.items():
        if c not in exit_df.columns:
            continue
        i = calendar.searchsorted(d)
        exit_df.iloc[i:, exit_df.columns.get_loc(c)] = True
        no_pick.iloc[max(i - 1, 0):, no_pick.columns.get_loc(c)] = True
    first = frame(first_day).fillna(False).astype(bool)
    ok_buy, ok_sell = _limits(fields["raw_open"], fields["raw_preclose"], first)
    can_buy = (trad & ok_buy & ~exit_df).fillna(False).astype(bool)
    can_sell = (trad & ok_sell).fillna(False).astype(bool)
    member = (fields["raw_close"].notna() & ~no_pick).astype(bool)
    lst_dates = pd.to_datetime(pd.Series(listing)).reindex(codes)
    listed_days = pd.DataFrame((calendar.values[:, None] - lst_dates.values[None, :]).astype("timedelta64[D]")
                               .astype(float), index=calendar, columns=codes)
    stock_codes = info_r["stock_code"].fillna(lst["stock_code"].reindex(codes))
    return Panel(fields=fields, member=member, tradable=trad, is_st=pd.DataFrame(False, index=calendar, columns=codes),
                 listed_days=listed_days, can_buy=can_buy, can_sell=can_sell,
                 benchmark=bench.loc[calendar, "close"], names=names, industry=_stock_industry(stock_codes),
                 kind="cb", lot_size=LOT, size_factor="cb_issue_size", exit=exit_df, exit_dates=exit_dates,
                 extra_filters={k: m["fn"] for k, m in CB_FILTERS.items()})
