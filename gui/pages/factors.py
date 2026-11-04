"""因子实验室（新版界面，界面重构计划 4.3.1）：因子研究、自定义因子、AI 因子挖掘

三个标签直接用选股包里的构件；股票池、区间、面板缓存与选股页共用（selection/universe_bar.py、gui/sel_panel.py），
界面状态也沿用 sp_state()（SP["res"]、SP["cf"]、SP["mine"]），所以两页之间换来换去，看到的是同一份研究结果。
自定义因子改动后，选股页的因子列表在下次打开选股页时重新读取（页面每次打开都会重画）。
"""

from nicegui import background_tasks, ui

from gui import state
from gui.common import t
from gui.layout import frame, page_title
from simplequant.stocks import FACTORS
from gui.pages.selection.common import Ctx, sp_state, prune_factors, factor_label
from gui.pages.selection.universe_bar import universe_bar, panel_loader
from gui.pages.selection.research import _research
from gui.pages.selection.custom import _custom_factors
from gui.pages.selection.mining import _mining

TABS = ("res", "cf", "mine")


def _go_download():
    """数据中心（4.4.1）做好之前，选股数据在选股页的「数据」标签下载"""
    state.STATE["sp_tab"] = "data"
    ui.navigate.to("/selection")


def add_factor(SP, key: str) -> bool:
    """把因子加进选股回测的因子列表（已有的不重复加，返回 False）。方向：因子研究刚分析过这个因子时按 IC 的正负，
    否则用因子的默认方向；权重 1"""
    if any(f["key"] == key for f in SP["factors"]):
        return False
    rep = SP.get("report")
    if rep and rep[0] == key:
        direction = 1 if rep[2].summary.mean > 0 else -1
    else:
        direction = FACTORS[key]["direction"]
    SP["factors"].append({"key": key, "weight": 1.0, "direction": direction})
    return True


def _to_backtest(SP, key: str):
    """「用这个因子回测」：加进因子列表，回到选股页的选股回测标签（设置区展开，看得到新加的因子）"""
    added = add_factor(SP, key)          # 提示由选股页打开后显示（跳转后这一页的提示会消失）
    state.STATE["sp_notify"] = (t("lab.factor_added" if added else "lab.factor_exists", name=factor_label(key)),
                                "positive" if added else "info")
    state.STATE["sp_tab"] = "bt"
    SP["setup_open"] = True
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
        ctx.to_backtest = lambda key: _to_backtest(SP, key)

        def on_empty(box):
            ui.button(t("lab.go_download"), icon="download", on_click=_go_download) \
                .props("outline no-caps").classes("self-start").mark("lab_go_download")
        load_panel = panel_loader(SP, ctx, lambda: [(res_box, _research), (cf_box, _custom_factors),
                                                    (mine_box, _mining)], on_empty)

        def reload_all():
            bar.sync()
            background_tasks.create(load_panel(), name="lab load panel")

        reload_all()
