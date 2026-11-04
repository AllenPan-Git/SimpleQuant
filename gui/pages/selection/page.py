"""选股页入口：股票池与区间、标签页"""

from nicegui import background_tasks, ui

from gui import state
from gui.common import t
from gui.layout import frame, page_title
from gui.plan_io import import_button
from .common import Ctx, sp_state, prune_factors
from .universe_bar import universe_bar, panel_loader
from .data_tab import _data_tab
from .research import _research
from .custom import _custom_factors
from .mining import _mining
from .backtest import _backtest
from .walkforward import _walkforward


def page():
    SP = sp_state()
    ctx = Ctx()
    with frame("/selection"):
        page_title("/selection", t("sp.intro_short"), t("sp.intro"))

        # ---------------- 股票池与区间 ----------------
        bar = universe_bar(SP, ctx, lambda: reload_all())

        # ---------------- 标签页 ----------------
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_data = ui.tab("data", t("sp.tab_data"), icon="storage")
            tab_res = ui.tab("res", t("sp.tab_research"), icon="science")
            tab_cf = ui.tab("cf", t("sp.tab_custom"), icon="functions")
            tab_mine = ui.tab("mine", t("sp.tab_mine"), icon="auto_awesome")
            tab_bt = ui.tab("bt", t("sp.tab_backtest"), icon="query_stats")
            tab_wf = ui.tab("wf", t("sp.tab_wf"), icon="timeline")
        with ui.tab_panels(tabs, value=state.STATE.get("sp_tab", "bt")).classes("w-full bg-transparent") \
                .mark("sp_tabs") as panels:
            with ui.tab_panel(tab_data).classes("px-0 gap-4"):
                data_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_res).classes("px-0 gap-4"):
                res_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_cf).classes("px-0 gap-4"):
                cf_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_mine).classes("px-0 gap-4"):
                mine_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_bt).classes("px-0 gap-4"):
                bt_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_wf).classes("px-0 gap-4"):
                wf_box = ui.column().classes("w-full gap-4")
        def on_tab(e):
            state.STATE["sp_tab"] = e.value
            if e.value == "wf":          # 滚动优化总是用选股回测标签页里的最新设置
                reload_wf()
        panels.on_value_change(on_tab)

        def reload_wf():
            if ctx.panel is None:
                return
            wf_box.clear()
            with wf_box:
                _walkforward(SP, ctx)
        ctx.reload_wf = reload_wf

        def factors_changed():
            """自定义因子保存 / 删除后：研究、回测、滚动优化里的因子列表重画（面板不用重新载入）"""
            prune_factors(SP)
            if ctx.panel is None:
                return
            for box, build in ((res_box, _research), (bt_box, _backtest), (wf_box, _walkforward)):
                box.clear()
                with box:
                    build(SP, ctx)
        ctx.factors_changed = factors_changed

        def reload_cf():
            """其他标签页保存了自定义因子（AI 因子挖掘）：自定义因子标签页的列表跟着变"""
            if ctx.panel is None:
                return
            cf_box.clear()
            with cf_box:
                _custom_factors(SP, ctx)
        ctx.reload_cf = reload_cf

        def on_empty(box):
            if box is bt_box:            # 没有数据也能导入方案（导入时下载）
                import_button().classes("self-start")
        load_panel = panel_loader(SP, ctx, lambda: [(res_box, _research), (cf_box, _custom_factors),
                                                    (mine_box, _mining), (bt_box, _backtest),
                                                    (wf_box, _walkforward)], on_empty)

        def reload_all():
            bar.sync()
            data_box.clear()
            with data_box:
                _data_tab(SP, ctx, reload_all)
            background_tasks.create(load_panel(), name="sp load panel")
        ctx.reload_all = reload_all

        reload_all()
