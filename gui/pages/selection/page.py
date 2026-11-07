"""选股页入口（界面重构计划 4.3.2）：顶部是这个策略的四步进度，股票池与区间，「选股回测 / 验证」两个标签

研究类（因子研究、自定义因子、AI 因子挖掘）在因子实验室，滚动优化与调仓日检验在「验证」里（verify.py），
数据在数据中心（/data 的「选股数据」「可转债」标签）。
"""

from nicegui import background_tasks, ui

from gui import routes, state
from gui.common import t
from gui.components import strategy_progress
from gui.layout import frame, page_title
from gui.plan_io import import_button
from .common import Ctx, sp_state, prune_factors
from .universe_bar import universe_bar, panel_loader
from .backtest import _backtest
from .verify import verify_tab, progress as progress_steps

TABS = [("bt", "sp.tab_backtest", "query_stats"), ("verify", "timing.tab_verify", "fact_check")]


def _accounts():
    try:
        from simplequant.paper import list_accounts
        return list_accounts()
    except Exception:  # noqa: BLE001 - 进度只是提示
        return []


def _start_tab() -> str:
    cur = state.STATE.get("sp_tab", "bt")
    return cur if cur in (k for k, _, _ in TABS) else "bt"


def page():
    SP = sp_state()
    ctx = Ctx()
    cur = _start_tab()
    with frame("/selection"):
        page_title("/selection", t("sp.intro_short"), t("sp.intro"))
        msg = state.STATE.pop("sp_notify", None)       # 其他页面带来的提示（因子实验室「用这个因子回测」）
        if msg:
            ui.notify(msg[0], type=msg[1])
        accounts = _accounts()

        @ui.refreshable
        def progress():
            strategy_progress(progress_steps(SP, ctx, accounts), on_click=lambda k: go_step(k), mark="prog")
        progress()

        # ---------------- 股票池与区间 ----------------
        bar = universe_bar(SP, ctx, lambda: reload_all())

        # ---------------- 标签页 ----------------
        boxes = {}
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_els = {k: ui.tab(k, t(key), icon=icon) for k, key, icon in TABS}
        with ui.tab_panels(tabs, value=cur).classes("w-full bg-transparent").mark("sp_tabs") as panels:
            for k, _, _ in TABS:
                with ui.tab_panel(tab_els[k]).classes("px-0 gap-4"):
                    boxes[k] = ui.column().classes("w-full gap-4")
        bt_box = boxes["bt"]
        vf = {"tab": None}               # 「验证」标签（面板载入后才画）

        def on_tab(e):
            state.STATE["sp_tab"] = e.value
            if e.value == "verify":     # 滚动优化总是用选股回测标签页里的最新设置
                reload_wf()
        panels.on_value_change(on_tab)

        def reload_wf():
            if ctx.panel is not None and vf["tab"]:
                vf["tab"].refresh(force=True)
        ctx.reload_wf = reload_wf

        def build_verify(SP_, ctx_):
            vf["tab"] = verify_tab(SP_, ctx_, lambda: tabs.set_value("bt"))

        def on_progress():
            progress.refresh()
            if vf["tab"]:
                vf["tab"].refresh()
        ctx.on_progress = on_progress

        def go_step(k):
            """点进度的一步：设置 → 选股回测标签并展开设置区；回测 → 选股回测标签；验证 → 验证标签；模拟盘 → 模拟盘页"""
            if k == "paper":
                ui.navigate.to("/paper")
                return
            if k == "setup" and SP.get("result") and not SP.get("setup_open", True) and ctx.panel is not None:
                SP["setup_open"] = True
                ctx.rebuild_bt()
            tabs.set_value("verify" if k == "verify" else "bt")

        def factors_changed():
            """自定义因子保存 / 删除后：回测里的因子列表重画（面板不用重新载入）"""
            prune_factors(SP)
            if ctx.panel is None:
                return
            bt_box.clear()
            with bt_box:
                _backtest(SP, ctx)
        ctx.factors_changed = factors_changed

        def on_empty(box):
            ui.button(t("lab.go_download"), icon="download", on_click=lambda: ui.navigate.to(routes.download())) \
                .props("outline no-caps").classes("self-start").mark("sp_go_download")
            if box is bt_box:            # 没有数据也能导入方案（导入时下载）
                import_button().classes("self-start")
        pairs = [(bt_box, _backtest), (boxes["verify"], build_verify)]
        load_panel = panel_loader(SP, ctx, lambda: pairs, on_empty)

        def reload_all():
            bar.sync()
            background_tasks.create(load_panel(), name="sp load panel")
        ctx.reload_all = reload_all

        reload_all()
