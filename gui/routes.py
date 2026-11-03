"""按当前界面决定去哪一页（两套界面并存期间，界面重构计划 4.2.3）：
新版界面去择时工作台 /timing，经典界面去原来的策略 / 回测 / 参数优化页"""

from gui import state
from gui.common import new_ui

CLASSIC = {"strategy": "/strategy", "backtest": "/backtest", "verify": "/optimize"}


def timing(step: str = "backtest", spec: dict | None = None, mode: str | None = None) -> str:
    """
    返回择时策略某一步的路由。step：strategy（编辑策略）/ backtest（回测报告）/ verify（验证，经典界面是参数优化页）。
    新版界面下：spec（带 name）放进工作台的编辑器；mode 切换编写方式（template / rule / ai / code）；
    strategy 一步展开设置区，verify 一步打开「验证」标签。经典界面下 spec、mode 不处理（各入口照旧自己设好状态）
    """
    if not new_ui():
        return CLASSIC[step]
    S = state.STATE
    if spec:
        S["timing_spec"] = spec
    if mode:
        state.strategy()["mode"] = mode
    if step == "strategy":
        S["timing_open"] = True
    S["timing_tab"] = "verify" if step == "verify" else "report"
    return "/timing"
