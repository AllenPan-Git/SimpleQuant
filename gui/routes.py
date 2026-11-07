"""择时策略各步、选股数据下载去哪一页（界面重构计划 4.2.3 / 4.4.1；经典界面已在 4.8 移除）"""

from gui import state


def timing(step: str = "backtest", spec: dict | None = None, mode: str | None = None) -> str:
    """
    返回择时策略某一步的路由（都是择时工作台 /timing）。step：strategy（展开设置区）/ backtest（回测报告）/ verify（「验证」标签）。
    spec（带 name）放进工作台的编辑器；mode 切换编写方式（template / rule / ai / code）
    """
    S = state.STATE
    if spec:
        S["timing_spec"] = spec
    if mode:
        state.strategy()["mode"] = mode
    if step == "strategy":
        S["timing_open"] = True
    S["timing_tab"] = "verify" if step == "verify" else "report"
    return "/timing"


def download(universe: str | None = None) -> str:
    """下载某个股票池的选股数据：数据中心 /data 的「选股数据」或「可转债」标签（并选好这个股票池）。
    universe 为空时用选股页当前的股票池"""
    from simplequant.stocks import universe as U
    S = state.STATE
    u = universe or (S.get("sp") or {}).get("universe", "hs300")
    if U.kind(u) == "cb":
        S["data_tab"] = "cb"
    else:
        S["data_tab"], S["data_uni"] = "stocks", u
    return "/data"
