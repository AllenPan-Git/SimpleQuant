"""
把个股日线拼成"日期 × 股票"的宽表（Panel），并计算每天开盘时能否买入/卖出

- 日历：以基准指数的交易日为准
- 成分股：每个交易日取"不晚于当天"的最近一次成分股快照
- 停牌/退市后：价格沿用最后一个值、tradable=0；上市前为 NaN
- 退市（delisting）：最后交易日距退市日不超过 EXIT_GAP_DAYS 的（退市整理期结束、吸收合并停牌换股，最后交易日事先公告）
  在最后一个交易日开盘必须卖出（exit）；更久的（交易类退市没有整理期，停牌后才宣布）当时无从预知，不强制卖出。
  卖不出、挂到退市日的，困境退市（ST、股价低于 1 元、简称带「退」——BaoStock 在退市整理期把 isST 标成 0）按 0 计价，
  吸收合并、主动退市按最后价格折为现金（delist）。
  2020–2026 实测：间隔 ≤ 15 天的 106 只困境退市股都叫「退市××」、105 只最后一天开盘能卖；> 15 天的 74 只只有 7 只能卖
- 退市整理期（delisting_period）：困境退市股最后一次标 ST 的次日到最后交易日（2021 起 118 只「退」股里 115 只
  ST 到整理期前一天，之后交易 15 天，改革前 30 天）；没有 ST 记录的取最后 ENDGAME_DAYS（30）个交易日。
  还没摘牌、现在简称已带「退」的同样处理（当天的简称，不算前视）。排除 ST 时一并排除
- 涨跌停（按不复权前收盘价）：沪深主板 10%、创业板（2020-08-24 起）和科创板 20%、北交所 30%、ST 5%
  开盘即涨停 → 买不进；开盘即跌停 → 卖不出；停牌 → 都不行
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import universe as universe_mod
from .store import StockStore, UNIVERSES
from ..i18n import L

PRICE_FIELDS = ["open", "high", "low", "close", "raw_open", "raw_close", "raw_preclose", "adj_factor"]
OTHER_FIELDS = ["volume", "amount", "turnover", "pe", "pb", "ps", "pct_chg"]
CHINEXT_20PCT_FROM = pd.Timestamp("2020-08-24")
EXIT_GAP_DAYS = 15            # 最后交易日到退市日超过这么多自然日：停牌后才宣布退市，不在最后交易日强制卖出
DELIST_ZERO, DELIST_CASH = 1, 2
ENDGAME_DAYS = 30             # 没有 ST 记录、推不出整理期起点时：取最后这么多个交易日（整理期 2020 年改革前 30 天、之后 15 天；
                              # 新规下多出的 15 天股票本是 *ST，排除不误伤。例：天翔退 2021 年整理期 30 天、BaoStock 无 ST 记录）


# 排雷条件（选股条件里留空 = 不限；没有数据的股票一律不选）。见 selection.eligible_mask
RISK_FILTERS = {
    "max_debt_ratio": {"label": L("资产负债率上限（%）", "Max debt ratio (%)"),
                       "help": L("最近一期财报的资产负债率高于该值的不选。银行、保险、券商普遍在 80% 以上，设置后基本都会被排除",
                                 "Skips stocks whose latest debt-to-assets ratio exceeds this. Banks, insurers and "
                                 "brokers are usually above 80% and will mostly be excluded"),
                       "fn": lambda p, v: p["debt_ratio"] <= v},
    "min_current_ratio": {"label": L("流动比率下限", "Min current ratio"),
                          "help": L("流动资产 ÷ 流动负债，低于该值的不选（短期偿债压力大）。银行、保险没有流动比率，设置后会被排除",
                                    "Current assets ÷ current liabilities; lower is skipped. Banks and insurers have "
                                    "none and are excluded"),
                          "fn": lambda p, v: p["current_ratio"] >= v},
    "min_cfo_np": {"label": L("经营现金流 / 净利润下限", "Min operating cash flow / net profit"),
                   "help": L("年初至最近一期的经营现金流 ÷ 净利润，低于该值的不选（利润没有变成现金）。"
                             "净利润为负的比值没有意义，设置后一并排除；一季度的现金流季节性强，数值波动大",
                             "Year-to-date operating cash flow ÷ net profit; lower is skipped (profits not turning "
                             "into cash). Loss-makers are excluded as the ratio is meaningless; Q1 cash flow is "
                             "seasonal and noisy"),
                   "fn": lambda p, v: p["cfo_np"] >= v},
}


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
    has_risk: bool = False        # 是否已加载排雷数据（资产负债率等，字段见 fundamentals.RISK_FIELDS）
    div_codes: frozenset = frozenset()   # 有分红送转数据的股票（字段 div_cash / div_bonus / div_reserve）
    div_patched: pd.DataFrame | None = None   # 分红表漏记、按复权因子补上的除权除息（code + 分红表各列）
    # ---- 品种差异（可转债见 simplequant/bonds/panel.py） ----
    kind: str = "stock"           # stock / cb
    lot_size: int = 100           # 每手数量：股票 100 股，可转债 10 张
    size_factor: str = "size"     # 市值中性化用的规模因子
    exit: pd.DataFrame | None = None     # 当天开盘必须卖出（可转债强赎公告后、退市前），bool
    exit_dates: dict = field(default_factory=dict)   # 代码 -> 必须卖出的第一天（模拟盘判断明天要不要卖）
    delist: dict = field(default_factory=dict)       # 代码 -> (结算日, DELIST_ZERO / DELIST_CASH)：退市时仍未卖出的持仓
    delisting_period: pd.DataFrame | None = None      # 当天在退市整理期（bool）；BaoStock 这段 isST = 0，排除 ST 时一并排除
    extra_filters: dict = field(default_factory=dict)   # 选股条件名 -> fn(panel, 值) -> bool DataFrame
    bench_total_return: bool = False     # 股票池基准是全收益指数（False：价格指数，不含分红，超额收益偏高）
    index_close: pd.Series | None = None  # 股票池指数（价格指数）收盘价：选股的大盘条件用

    @property
    def calendar(self) -> pd.DatetimeIndex:
        return self.member.index

    @property
    def codes(self) -> list[str]:
        return list(self.member.columns)

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.fields[name]

    def eligible(self, exclude_st: bool = True, min_list_days: int = 0) -> pd.DataFrame:
        """可以参与选股的股票：当天是成分股、正常交易、（可选）非 ST 且不在退市整理期、上市满 N 天"""
        ok = self.member & self.tradable
        if exclude_st:
            ok &= ~self.is_st
            if self.delisting_period is not None:
                ok &= ~self.delisting_period
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


def delisting(raw_close: pd.DataFrame, tradable: pd.DataFrame, is_st: pd.DataFrame, out_date: pd.Series,
              names: dict) -> tuple[dict, dict]:
    """
    退市股：(代码 -> 必须卖出日, 代码 -> (结算日, 方式))。
    out_date：代码 -> 退市日（没退市为 NaT）；退市日在面板区间之后的还在退市整理期或没到摘牌，不处理
    """
    cal = raw_close.index
    exit_dates, delist = {}, {}
    for c, out in out_date.dropna().items():
        if c not in raw_close.columns or out > cal[-1]:
            continue
        traded = tradable[c][tradable[c]]
        if traded.empty:
            continue
        last = traded.index[-1]
        if (out - last).days <= EXIT_GAP_DAYS:
            exit_dates[c] = last
        i = cal.searchsorted(out)
        if i >= len(cal):
            continue
        distressed = bool(is_st.at[last, c]) or raw_close.at[last, c] < 1 or "退" in str(names.get(c, ""))
        delist[c] = (cal[i], DELIST_ZERO if distressed else DELIST_CASH)
    return exit_dates, delist


def delisting_period(tradable: pd.DataFrame, is_st: pd.DataFrame, ends: dict) -> pd.DataFrame | None:
    """代码 -> 最后交易日 → 每天是否在退市整理期：最后一次标 ST 的次日起；没有 ST 记录的取最后 ENDGAME_DAYS 个交易日"""
    if not ends:
        return None
    out = pd.DataFrame(False, index=tradable.index, columns=tradable.columns)
    for c, end in ends.items():
        traded = tradable[c].loc[:end]
        days = traded.index[traded.values]
        if days.empty:
            continue
        st = is_st[c].loc[:end]
        st_days = st.index[st.values]
        start = days[days > st_days[-1]][:1] if len(st_days) else days[-ENDGAME_DAYS:][:1]
        if len(start):
            out.loc[start[0]:end, c] = True
    return out


def exit_frame(exit_dates: dict, calendar: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    out = pd.DataFrame(False, index=calendar, columns=codes)
    for c, d in exit_dates.items():
        if c in out.columns:
            out.iloc[calendar.searchsorted(d):, out.columns.get_loc(c)] = True
    return out


def build_panel(store: StockStore, universe: str, start: str, end: str) -> Panel:
    uni = store.load_universe(universe)
    bench = store.load_index(UNIVERSES[universe]["index"])
    calendar = bench.loc[start:end].index
    tr = universe_mod.total_return(universe, calendar, store)
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
        out_date = pd.to_datetime(basics["out_date"].reindex(codes))
    except FileNotFoundError:
        ipo, names, out_date = pd.Series(first_dates), {}, pd.Series(dtype="datetime64[ns]")
    delist_names = dict(names)        # 判断困境退市用 BaoStock 的最新简称（如「退市西水」），不被成分股快照里的旧简称覆盖
    names.update(uni.drop_duplicates("code", keep="last").set_index("code")["name"].to_dict())
    ipo = pd.to_datetime(ipo).reindex(codes)
    days = (calendar.values[:, None] - ipo.values[None, :]).astype("timedelta64[D]").astype(float)
    listed_days = pd.DataFrame(days, index=calendar, columns=codes)

    can_buy, can_sell = trading_constraints(frames["raw_open"], frames["raw_preclose"], tradable, is_st)
    exit_dates, delist = delisting(frames["raw_close"], tradable, is_st, out_date, delist_names)
    exit_df = exit_frame(exit_dates, calendar, codes)
    ends = {c: d for c, d in exit_dates.items() if delist.get(c, (None, 0))[1] == DELIST_ZERO}
    for c, name in delist_names.items():         # 还没摘牌、已经进入整理期的
        if "退" in str(name) and c not in delist and tradable[c].any():
            ends[c] = tradable.index[tradable[c].values][-1]
    can_buy &= ~exit_df
    has_fin = add_fundamentals(frames, store, codes, calendar)
    has_risk = add_risk(frames, store, codes, calendar)
    div_codes, div_patched = add_dividends(frames, store, codes, calendar, is_st)
    return Panel(fields=frames, member=membership(uni, calendar, codes), tradable=tradable, is_st=is_st,
                 listed_days=listed_days, can_buy=can_buy, can_sell=can_sell,
                 benchmark=bench.loc[calendar, "close"] if tr is None else tr, names=names,
                 index_close=bench.loc[calendar, "close"],
                 industry=store.load_industry().reindex(codes), has_fin=has_fin, div_codes=div_codes,
                 div_patched=div_patched, bench_total_return=tr is not None, has_risk=has_risk,
                 extra_filters={k: m["fn"] for k, m in RISK_FILTERS.items()} if has_risk else {},
                 exit=exit_df if exit_dates else None, exit_dates=exit_dates, delist=delist,
                 delisting_period=delisting_period(tradable, is_st, ends))


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


def add_risk(frames: dict, store: StockStore, codes: list[str], calendar: pd.DatetimeIndex) -> bool:
    """排雷字段（资产负债率、流动比率、经营现金流 / 净利润），同财务数据按公告日对齐"""
    from .fundamentals import RISK_FIELDS, align_all, derive_risk
    have = [c for c in codes if store.has_risk(c)]
    if not have:
        return False
    vals = np.full((len(RISK_FIELDS), len(calendar), len(codes)), np.nan)
    col = {c: i for i, c in enumerate(codes)}
    for c in have:
        vals[:, :, col[c]] = align_all(derive_risk(store.load_risk(c)), calendar, RISK_FIELDS).T
    for k, f in enumerate(RISK_FIELDS):
        frames[f] = pd.DataFrame(vals[k], index=calendar, columns=codes)
    return True


def add_fundamentals(frames: dict, store: StockStore, codes: list[str], calendar: pd.DatetimeIndex) -> bool:
    """按公告日对齐财务字段，并计算总市值（不复权收盘价 × 当时已公告的总股本）"""
    from .fundamentals import FIELDS, align_all, derive
    have = [c for c in codes if store.has_fin(c)]
    if not have:
        return False
    vals = np.full((len(FIELDS), len(calendar), len(codes)), np.nan)
    col = {c: i for i, c in enumerate(codes)}
    for c in have:
        vals[:, :, col[c]] = align_all(derive(store.load_fin(c)), calendar, FIELDS).T
    for k, f in enumerate(FIELDS):
        frames[f] = pd.DataFrame(vals[k], index=calendar, columns=codes)
    frames["mcap"] = frames["raw_close"] * frames["total_share"]
    return True
