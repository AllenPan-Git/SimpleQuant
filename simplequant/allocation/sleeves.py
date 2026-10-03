"""
配置候选（「组合成分」）：每个候选给出一条日收益率序列

    {"id": "...", "name": "国债ETF", "class": "bond", "kind": "hold", "symbol": "511010"}
    {"id": "...", "name": "...", "class": "equity", "kind": "timing", "symbol": "510300", "strategy": "双均线"}
    {"id": "...", "name": "...", "class": "alt", "kind": "selection", "strategy": "转债双低"}
    {"id": "cash", "name": "现金", "class": "cash", "kind": "cash", "rate": 0.015}

kind:
- cash      现金类：按固定年化收益率逐日计息，无需数据
- hold      买入持有某只 ETF：直接用本地行情库中该代码最新的一份日线（后复权）计算收益
- timing    择时策略 + 标的：用回测引擎运行，取每日资产的收益
- selection 已保存的选股策略：在本地选股数据上运行
class（大类）：cash 现金类 / bond 债券类 / equity 权益类 / alt 其他（黄金、可转债等）

风险等级 R1~R5 按回测得到的年化波动率和最大回撤划分（取两者中较高的一级），与人为标注无关。
"""

import numpy as np
import pandas as pd

from ..i18n import L
from ..data import library
from ..engine import run_backtest, BrokerConfig, COST_PRESETS
from ..engine.results import TRADING_DAYS

CLASSES = {
    "cash": L("现金类", "Cash"),
    "bond": L("债券类", "Bonds"),
    "equity": L("权益类", "Equities"),
    "alt": L("其他（黄金、可转债等）", "Other (gold, convertibles, etc.)"),
}
KINDS = {
    "cash": L("现金类（固定收益率）", "Cash (fixed rate)"),
    "hold": L("买入持有", "Buy and hold"),
    "timing": L("择时策略", "Timing strategy"),
    "selection": L("选股策略", "Stock selection strategy"),
}
RISK_LABELS = {1: "R1", 2: "R2", 3: "R3", 4: "R4", 5: "R5"}
# 风险等级划分：年化波动率、最大回撤（绝对值）的上限；超过 R4 上限即为 R5。
# 回撤分档与风险测评中「可承受的最大亏损」一致（3% / 10% / 20% / 35%），使 R 级与 C 级含义对应
VOL_CUTS = (0.02, 0.06, 0.15, 0.25)
DD_CUTS = (0.03, 0.10, 0.20, 0.35)

DEFAULT_DATA_START = "2013-01-01"
CASH_RATE = 0.015

# 内置候选：覆盖从低到高的风险梯度；ETF 均选取上市较早的品种，便于获得较长的共同区间
DEFAULT_SLEEVES = [
    {"id": "cash", "name": L("现金类（年化 1.5%）", "Cash (1.5% p.a.)"), "class": "cash", "kind": "cash", "rate": CASH_RATE},
    {"id": "hold_511010", "name": L("国债ETF", "Treasury ETF"), "class": "bond", "kind": "hold", "symbol": "511010"},
    {"id": "hold_511260", "name": L("十年国债ETF", "10Y Treasury ETF"), "class": "bond", "kind": "hold", "symbol": "511260"},
    {"id": "hold_510300", "name": L("沪深300ETF", "CSI 300 ETF"), "class": "equity", "kind": "hold", "symbol": "510300"},
    {"id": "hold_510500", "name": L("中证500ETF", "CSI 500 ETF"), "class": "equity", "kind": "hold", "symbol": "510500"},
    {"id": "hold_518880", "name": L("黄金ETF", "Gold ETF"), "class": "alt", "kind": "hold", "symbol": "518880"},
]


def find_dataset(symbol: str) -> library.DatasetMeta | None:
    """本地行情库中该代码的日线：优先后复权、其次结束日期最新、再次起始日期最早"""
    metas = [m for m in library.list_datasets() if m.symbol == symbol and m.freq == "1d"]
    if not metas:
        return None
    return sorted(metas, key=lambda m: (m.adjust == "hfq", m.end, -int(m.start.replace("-", "") or 0)))[-1]


def missing_data(sleeves: list[dict]) -> list[str]:
    """需要行情但本地没有的代码"""
    return sorted({s["symbol"] for s in sleeves if s["kind"] in ("hold", "timing") and not find_dataset(s["symbol"])})


def download(symbol: str, end: str, start: str = DEFAULT_DATA_START, name: str = ""):
    """下载 ETF 日线（后复权）到本地行情库"""
    from ..data import fetch_to_library
    return fetch_to_library("akshare", symbol, start, end, freq="1d", adjust="hfq", name=name or symbol, asset="etf")


def _broker_for(cls: str) -> BrokerConfig:
    preset = COST_PRESETS["bond" if cls == "bond" else "etf"]
    # 资金取得较大，减少整手限制带来的现金拖累，使收益率主要反映策略本身
    return BrokerConfig(cash=1_000_000, commission=preset["commission"], min_commission=preset["min_commission"],
                        stamp_duty=preset["stamp_duty"])


def sleeve_returns(sleeve: dict, calendar: pd.DatetimeIndex | None = None) -> pd.Series:
    """候选的日收益率序列（索引为交易日）；calendar 仅用于现金类"""
    kind = sleeve["kind"]
    if kind == "cash":
        if calendar is None or not len(calendar):
            raise ValueError("cash sleeve needs a calendar / 现金类需要交易日历")
        daily = (1 + float(sleeve.get("rate", CASH_RATE))) ** (1 / TRADING_DAYS) - 1
        return pd.Series(daily, index=pd.DatetimeIndex(calendar)).iloc[1:].rename(sleeve["id"])
    if kind in ("hold", "timing"):
        meta = find_dataset(sleeve["symbol"])
        if meta is None:
            raise ValueError(f"no local data for {sleeve['symbol']} / 本地缺少 {sleeve['symbol']} 的行情数据")
        df = library.load(meta.id)
        if kind == "hold":
            return df["close"].pct_change().dropna().rename(sleeve["id"])
        from .. import strategies
        spec = _strategy(sleeve)
        res = run_backtest({sleeve["symbol"]: df}, *strategies.resolve(spec), _broker_for(sleeve["class"]))
        return res.equity["value"].pct_change().dropna().rename(sleeve["id"])
    if kind == "selection":
        return _selection_returns(sleeve).rename(sleeve["id"])
    raise ValueError(f"unknown sleeve kind {kind}")


def _strategy(sleeve: dict) -> dict:
    from .. import strategies
    name = sleeve["strategy"]
    if name.startswith("tpl:"):
        return {"kind": "template", "template": name[4:], "params": {}}
    saved = strategies.list_strategies()
    if name not in saved:
        raise ValueError(f"strategy not found: {name} / 未找到策略「{name}」")
    return saved[name]


def _selection_returns(sleeve: dict) -> pd.Series:
    from ..stocks import StockStore, run_selection, universe
    from ..paper.runner import PANEL_WARMUP_DAYS
    spec = _strategy(sleeve)
    if spec.get("kind") != "selection":
        raise ValueError(f"not a selection strategy: {sleeve['strategy']} / 「{sleeve['strategy']}」不是选股策略")
    store = StockStore()
    if not universe.ready(spec["universe"], store):
        raise ValueError(f"stock data not downloaded for {spec['universe']} / 尚未下载该股票池的选股数据")
    idx = universe.load_benchmark(spec["universe"], store).index
    panel = universe.build(spec["universe"], idx[0].date().isoformat(), idx[-1].date().isoformat(), store)
    # 前 PANEL_WARMUP_DAYS 天只用于预热因子
    cal = panel.calendar
    start = cal[min(len(cal) - 1, cal.searchsorted(cal[0] + pd.Timedelta(days=PANEL_WARMUP_DAYS)))]
    preset = COST_PRESETS["bond" if universe.kind(spec["universe"]) == "cb" else "stock"]
    broker = BrokerConfig(cash=1_000_000, commission=preset["commission"], min_commission=preset["min_commission"],
                          stamp_duty=preset["stamp_duty"], dividend=spec.get("dividend", "reinvest"))
    res = run_selection(panel, spec, broker, start=start)
    return res.equity["value"].pct_change().dropna()


def risk_stats(returns: pd.Series) -> dict:
    """年化收益、年化波动率、最大回撤、风险等级"""
    r = returns.dropna()
    if len(r) < 2:
        return {"cagr": np.nan, "volatility": np.nan, "max_drawdown": np.nan, "risk": 0}
    nav = (1 + r).cumprod()
    years = max(len(r) / TRADING_DAYS, 1 / TRADING_DAYS)
    cagr = float(nav.iloc[-1] ** (1 / years) - 1)
    vol = float(r.std() * np.sqrt(TRADING_DAYS))
    mdd = float((nav / nav.cummax().clip(lower=1.0) - 1).min())
    return {"cagr": cagr, "volatility": vol, "max_drawdown": mdd, "risk": risk_grade(vol, mdd)}


def risk_grade(vol: float, max_dd: float) -> int:
    g_vol = 1 + sum(vol > c for c in VOL_CUTS)
    g_dd = 1 + sum(abs(max_dd) > c for c in DD_CUTS)
    return int(max(g_vol, g_dd))
