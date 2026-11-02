"""工具页：个股概览、持仓诊断

两项都不属于选股流程，只是用到同一份本地选股数据：界面状态沿用选股页的 sp_state()（ov_code、hd 等），
面板缓存与选股页共用（gui/sel_panel.py）。
"""

from nicegui import ui

from gui import state
from gui.common import t
from gui.layout import frame, page_title
from gui.sel_panel import get_panel
from gui.pages.selection import sp_state
from gui.pages.stock_overview import overview_tab
from gui.pages.holdings_check import diag_tab

TABS = ("ov", "hd")


def page():
    SP = sp_state()
    cur = state.STATE.get("tools_tab")
    if cur not in TABS:
        cur = TABS[0]
    with frame("/tools"):
        page_title("/tools", t("tools.intro"))
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_ov = ui.tab("ov", t("sp.tab_overview"), icon="manage_search")
            tab_hd = ui.tab("hd", t("sp.tab_diag"), icon="fact_check")
        with ui.tab_panels(tabs, value=cur).classes("w-full bg-transparent").mark("tools_tabs") as panels:
            with ui.tab_panel(tab_ov).classes("px-0 gap-4"):        # 只读本地数据，不需要面板
                overview_tab(SP)
            with ui.tab_panel(tab_hd).classes("px-0 gap-4"):        # 自己按所选股票池取面板
                diag_tab(SP, get_panel)
        panels.on_value_change(lambda e: state.STATE.__setitem__("tools_tab", e.value))
