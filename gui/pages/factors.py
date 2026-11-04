"""因子实验室（新版界面，界面重构计划 4.3.1）：因子研究、自定义因子、AI 因子挖掘

三个标签直接用选股包里的构件；股票池、区间、面板缓存与选股页共用（selection/universe_bar.py、gui/sel_panel.py），
界面状态也沿用 sp_state()（SP["res"]、SP["cf"]、SP["mine"]），所以两页之间换来换去，看到的是同一份研究结果。
自定义因子改动后，选股页的因子列表在下次打开选股页时重新读取（页面每次打开都会重画）。
"""

from nicegui import background_tasks, ui

from gui import state
from gui.common import t
from gui.layout import frame, page_title
from gui.pages.selection.common import Ctx, sp_state, prune_factors
from gui.pages.selection.universe_bar import universe_bar, panel_loader
from gui.pages.selection.research import _research
from gui.pages.selection.custom import _custom_factors
from gui.pages.selection.mining import _mining

TABS = ("res", "cf", "mine")


def _go_download():
    """数据中心（4.4.1）做好之前，选股数据在选股页的「数据」标签下载"""
    state.STATE["sp_tab"] = "data"
    ui.navigate.to("/selection")


def page():
    SP = sp_state()
    ctx = Ctx()
    cur = state.STATE.get("lab_tab")
    if cur not in TABS:
        cur = TABS[0]
    with frame("/factors"):
        page_title("/factors", t("lab.intro_short"), t("lab.intro"))
        bar = universe_bar(SP, ctx, lambda: reload_all())

        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_res = ui.tab("res", t("sp.tab_research"), icon="science")
            tab_cf = ui.tab("cf", t("sp.tab_custom"), icon="functions")
            tab_mine = ui.tab("mine", t("sp.tab_mine"), icon="auto_awesome")
        with ui.tab_panels(tabs, value=cur).classes("w-full bg-transparent").mark("lab_tabs") as panels:
            with ui.tab_panel(tab_res).classes("px-0 gap-4"):
                res_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_cf).classes("px-0 gap-4"):
                cf_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_mine).classes("px-0 gap-4"):
                mine_box = ui.column().classes("w-full gap-4")
        panels.on_value_change(lambda e: state.STATE.__setitem__("lab_tab", e.value))

        def rebuild(box, build):
            if ctx.panel is None:
                return
            box.clear()
            with box:
                build(SP, ctx)

        def factors_changed():
            """自定义因子保存 / 删除后：因子研究的下拉框重画；选股回测的因子列表去掉已删除的"""
            prune_factors(SP)
            rebuild(res_box, _research)
        ctx.factors_changed = factors_changed
        ctx.reload_cf = lambda: rebuild(cf_box, _custom_factors)     # AI 因子挖掘保存了因子

        def on_empty(box):
            ui.button(t("lab.go_download"), icon="download", on_click=_go_download) \
                .props("outline no-caps").classes("self-start").mark("lab_go_download")
        load_panel = panel_loader(SP, ctx, lambda: [(res_box, _research), (cf_box, _custom_factors),
                                                    (mine_box, _mining)], on_empty)

        def reload_all():
            bar.sync()
            background_tasks.create(load_panel(), name="lab load panel")

        reload_all()
