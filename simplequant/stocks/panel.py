"""
把个股日线拼成"日期 × 股票"的宽表（Panel），并计算每天开盘时能否买入/卖出

- 日历：以基准指数的交易日为准
- 成分股：每个交易日取"不晚于当天"的最近一次成分股快照
- 停牌/退市后：价格沿用最后一个值、tradable=0；上市前为 NaN
- 涨跌停（按不复权前收盘价）：沪深主板 10%、创业板（2020-08-24 起）和科创板 20%、北交所 30%、ST 5%
  开盘即涨停 → 买不进；开盘即跌停 → 卖不出；停牌 → 都不行
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .store import StockStore, UNIVERSES

PRICE_FIELDS = ["open", "high", "low", "close", "raw_open", "raw_close", "raw_preclose", "adj_factor"]
OTHER_FIELDS = ["volume", "amount", "turnover", "pe", "pb", "ps", "pct_chg"]
CHINEXT_20PCT_FROM = pd.Timestamp("2020-08-24")


@dataclass
class Panel:
    fields: dict                  # 字段名 -> DataFrame(日期 × 代码)
    member: pd.DataFrame          # 当天是否是指数成分股（bool）
    tradable: pd.DataFrame        # 当天是否正常交易（bool）
    is_st: pd.DataFrame           # bool
    listed_days: pd.DataFrame     # 上市天数（自然日）
    can_buy: pd.DataFrame         # 当天开盘能否买入
    can_sell: pd.DataFrame        # 当天开盘能否卖出
    benchmark: pd.Series          # 基准指数收盘价
    names: dict = field(default_factory=dict)
    industry: pd.Series = field(default_factory=lambda: pd.Series(dtype=str))   # 代码 -> 行业（当前分类）
    has_fin: bool = False         # 是否已加载财务数据
    div_codes: frozenset = frozenset()   # 有分红送转数据的股票（字段 div_cash / div_bonus / div_reserve）
    div_patched: pd.DataFrame | None = None   # 分红表漏记、按复权因子补上的除权除息（code + 分红表各列）

    @property
    def calendar(self) -> pd.DatetimeIndex:
        return self.member.index

    @property
    def codes(self) -> list[str]:
        return list(self.member.columns)

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.fields[name]

    def eligible(self, exclude_st: bool = True, min_list_days: int = 0) -> pd.DataFrame:
        """可以参与选股的股票：当天是成分股、正常交易、（可选）非 ST、上市满 N 天"""
        ok = self.member & self.tradable
        if exclude_st:
            ok &= ~self.is_st
        if min_list_days:
            ok &= self.listed_days >= min_list_days
        return ok


def limit_pct(codes: pd.Index, dates: pd.DatetimeIndex, is_st: pd.DataFrame) -> pd.DataFrame:
    """每只股票每天的涨跌停幅度"""
    base = pd.DataFrame(0.10, index=dates, columns=codes)
    for c in codes:
        num = c.split(".")[-1]
        if c.startswith("bj.") or num[:1] in ("4", "8"):
            base[c] = 0.30
        elif num.startswith("688"):
            base[c] = 0.20
        elif num.startswith(("300", "301")):
            base.loc[dates >= CHINEXT_20PCT_FROM, c] = 0.20
    # ST（主板）5%；创业板/科创板的 ST 仍为 20%
    st_main = is_st & (base == 0.10)
    return base.mask(st_main, 0.05)


def trading_constraints(raw_open, raw_preclose, tradable, is_st):
    pct = limit_pct(raw_open.columns, raw_open.index, is_st)
    up = (raw_preclose * (1 + pct)).round(2)
    down = (raw_preclose * (1 - pct)).round(2)
    can_buy = tradable & (raw_open < up - 0.001)
    can_sell = tradable & (raw_open > down + 0.001)
    return can_buy.fillna(False), can_sell.fillna(False)


def membership(universe: pd.DataFrame, calendar: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    """成分股快照 → 每个交易日的成员矩阵"""
    snaps = universe.assign(v=True).pivot_table(index="date", columns="code", values="v", aggfunc="any")
    snaps = snaps.reindex(columns=codes).fillna(False).astype(bool)
    idx = snaps.index.searchsorted(calendar, side="right") - 1          # 不晚于当天的最近快照
    out = pd.DataFrame(False, index=calendar, columns=codes)
    valid = idx >= 0
    out.iloc[valid] = snaps.values[idx[valid]]
    return out


def build_panel(store: StockStore, universe: str, start: str, end: str) -> Panel:
    uni = store.load_universe(universe)
    bench = store.load_index(UNIVERSES[universe]["index"])
    calendar = bench.loc[start:end].index
    codes = sorted(uni.loc[uni["date"] <= pd.Timestamp(end), "code"].unique())
    codes = [c for c in codes if store.has(c)]

    cols = {f: {} for f in PRICE_FIELDS + OTHER_FIELDS + ["tradable", "is_st"]}
    first_dates = {}
    for c in codes:
        df = store.load(c)
        first_dates[c] = df.index[0]
        df = df.reindex(calendar)
        last_known = df["raw_close"].last_valid_index()
        for f in PRICE_FIELDS:
            s = df[f]
            cols[f][c] = s.ffill() if last_known is not None else s
        for f in OTHER_FIELDS:
            cols[f][c] = df[f]
        cols["tradable"][c] = df["tradable"].fillna(0).astype(bool) & df["raw_close"].notna()
        cols["is_st"][c] = df["is_st"].fillna(0).astype(bool)

    frames = {f: pd.DataFrame(v, index=calendar, columns=codes) for f, v in cols.items()}
    tradable, is_st = frames.pop("tradable").astype(bool), frames.pop("is_st").astype(bool)

    try:
        basics = store.load_basics().set_index("code")
        ipo = pd.Series({c: basics["ipo_date"].get(c, first_dates[c]) for c in codes})
        names = basics["name"].reindex(codes).dropna().to_dict()
    except FileNotFoundError:
        ipo, names = pd.Series(first_dates), {}
    names.update(uni.drop_duplicates("code", keep="last").set_index("code")["name"].to_dict())
    ipo = pd.to_datetime(ipo).reindex(codes)
    days = (calendar.values[:, None] - ipo.values[None, :]).astype("timedelta64[D]").astype(float)
    listed_days = pd.DataFrame(days, index=calendar, columns=codes)

    can_buy, can_sell = trading_constraints(frames["raw_open"], frames["raw_preclose"], tradable, is_st)
    has_fin = add_fundamentals(frames, store, codes, calendar)
    div_codes, div_patched = add_dividends(frames, store, codes, calendar, is_st)
    return Panel(fields=frames, member=membership(uni, calendar, codes), tradable=tradable, is_st=is_st,
                 listed_days=listed_days, can_buy=can_buy, can_sell=can_sell,
                 benchmark=bench.loc[calendar, "close"], names=names,
                 industry=store.load_industry().reindex(codes), has_fin=has_fin, div_codes=div_codes,
                 div_patched=div_patched)


def add_dividends(frames: dict, store: StockStore, codes: list[str], calendar: pd.DatetimeIndex,
                  is_st: pd.DataFrame | None = None) -> tuple[frozenset, pd.DataFrame | None]:
    """
    分红送转事件（日期 × 代码，无事件为 0），含分红表漏记、按复权因子补上的（见 dividends.missing_events）。
    返回 (有分红数据的股票, 补上的事件)
    """
    from .dividends import align, with_missing
    have = [c for c in codes if store.has_div(c)]
    if not have:
        return frozenset(), None
    rights = store.load_rights()
    cols, patched = {"cash": {}, "bonus": {}, "reserve": {}}, []
    for c in have:
        daily = pd.DataFrame({k: frames[k][c] for k in ("raw_close", "raw_preclose", "adj_factor")})
        if is_st is not None:
            daily["is_st"] = is_st[c]
        div, extra = with_missing(store.load_div(c), daily, None if rights is None else rights.get(c, []))
        if len(extra):
            patched.append(extra.assign(code=c))
        ev = align(div, calendar)
        for k in cols:
            cols[k][c] = ev[k]
    for k, v in cols.items():
        frames[f"div_{k}"] = pd.DataFrame(v, index=calendar).reindex(columns=codes).fillna(0.0)
    return frozenset(have), (pd.concat(patched, ignore_index=True) if patched else None)


def add_fundamentals(frames: dict, store: StockStore, codes: list[str], calendar: pd.DatetimeIndex) -> bool:
    """按公告日对齐财务字段，并计算总市值（不复权收盘价 × 当时已公告的总股本）"""
    from .fundamentals import FIELDS, align, derive
    have = [c for c in codes if store.has_fin(c)]
    if not have:
        return False
    cols = {f: {} for f in FIELDS}
    for c in have:
        fin = derive(store.load_fin(c))
        for f in FIELDS:
            cols[f][c] = align(fin, calendar, f)
    for f in FIELDS:
        frames[f] = pd.DataFrame(cols[f], index=calendar).reindex(columns=codes)
    frames["mcap"] = frames["raw_close"] * frames["total_share"]
    return True
