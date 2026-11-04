"""选股页入口：股票池与区间、标签页

经典界面六个标签（数据 / 因子研究 / 自定义因子 / AI 因子挖掘 / 选股回测 / 滚动优化）；
新版界面（界面重构计划 4.3.2）顶部是这个策略的四步进度，只留「选股回测 / 验证 / 数据」：研究类三个在因子实验室，
滚动优化与调仓日检验在「验证」里（verify.py）；「数据」等 4.4.1 数据中心做好后移走。
"""

from nicegui import background_tasks, ui

from gui import state
from gui.common import t, new_ui
from gui.components import strategy_progress
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
from .verify import verify_tab, progress as progress_steps

CLASSIC_TABS = [("data", "sp.tab_data", "storage"), ("res", "sp.tab_research", "science"),
                ("cf", "sp.tab_custom", "functions"), ("mine", "sp.tab_mine", "auto_awesome"),
                ("bt", "sp.tab_backtest", "query_stats"), ("wf", "sp.tab_wf", "timeline")]
NEW_TABS = [("bt", "sp.tab_backtest", "query_stats"), ("verify", "timing.tab_verify", "fact_check"),
            ("data", "sp.tab_data", "storage")]


def _accounts():
    try:
        from simplequant.paper import list_accounts
        return list_accounts()
    except Exception:  # noqa: BLE001 - 进度只是提示
        return []


def _start_tab(new: bool) -> str:
    cur = state.STATE.get("sp_tab", "bt")
    if not new:
        return cur
    if cur == "wf":                 # 经典界面的「滚动优化」（首页进度的「验证」一步也用它）→ 验证标签的滚动优化
        cur, state.STATE["sp_vf"] = "verify", "walkforward"
    return cur if cur in (k for k, _, _ in NEW_TABS) else "bt"


def page():
    SP = sp_state()
    ctx = Ctx()
    new = new_ui()
    cur = _start_tab(new)
    with frame("/selection"):
        page_title("/selection", t("sp.intro_short"), t("sp.intro"))
        if new:
            accounts = _accounts()

            @ui.refreshable
            def progress():
                strategy_progress(progress_steps(SP, ctx, accounts), on_click=lambda k: go_step(k), mark="prog")
            progress()

        # ---------------- 股票池与区间 ----------------
        bar = universe_bar(SP, ctx, lambda: reload_all())

        # ---------------- 标签页 ----------------
        tab_defs = NEW_TABS if new else CLASSIC_TABS
        boxes = {}
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_els = {k: ui.tab(k, t(key), icon=icon) for k, key, icon in tab_defs}
        with ui.tab_panels(tabs, value=cur).classes("w-full bg-transparent").mark("sp_tabs") as panels:
            for k, _, _ in tab_defs:
                with ui.tab_panel(tab_els[k]).classes("px-0 gap-4"):
                    boxes[k] = ui.column().classes("w-full gap-4")
        data_box, bt_box = boxes["data"], boxes["bt"]
        res_box, cf_box, mine_box, wf_box = (boxes.get(k) for k in ("res", "cf", "mine", "wf"))
        vf = {"tab": None}               # 新版的「验证」标签（面板载入后才画）

        def on_tab(e):
            state.STATE["sp_tab"] = e.value
            if e.value in ("wf", "verify"):     # 滚动优化总是用选股回测标签页里的最新设置
                reload_wf()
        panels.on_value_change(on_tab)

        def reload_wf():
            if ctx.panel is None:
                return
            if new:
                if vf["tab"]:
                    vf["tab"].refresh(force=True)
                return
            wf_box.clear()
            with wf_box:
                _walkforward(SP, ctx)
        ctx.reload_wf = reload_wf

        def build_verify(SP_, ctx_):
            vf["tab"] = verify_tab(SP_, ctx_, lambda: tabs.set_value("bt"))

        if new:
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
            """自定义因子保存 / 删除后：研究、回测、滚动优化里的因子列表重画（面板不用重新载入）"""
            prune_factors(SP)
            if ctx.panel is None:
                return
            for box, build in ((res_box, _research), (bt_box, _backtest), (wf_box, _walkforward)):
                if box is None:          # 新版界面没有这个标签
                    continue
                box.clear()
                with box:
                    build(SP, ctx)
        ctx.factors_changed = factors_changed

        def reload_cf():
            """其他标签页保存了自定义因子（AI 因子挖掘）：自定义因子标签页的列表跟着变"""
            if ctx.panel is None or cf_box is None:
                return
            cf_box.clear()
            with cf_box:
                _custom_factors(SP, ctx)
        ctx.reload_cf = reload_cf

        def on_empty(box):
            if box is bt_box:            # 没有数据也能导入方案（导入时下载）
                import_button().classes("self-start")
        if new:
            pairs = [(bt_box, _backtest), (boxes["verify"], build_verify)]
        else:
            pairs = [(res_box, _research), (cf_box, _custom_factors), (mine_box, _mining), (bt_box, _backtest),
                     (wf_box, _walkforward)]
        load_panel = panel_loader(SP, ctx, lambda: pairs, on_empty)

        def reload_all():
            bar.sync()
            data_box.clear()
            with data_box:
                _data_tab(SP, ctx, reload_all)
            background_tasks.create(load_panel(), name="sp load panel")
        ctx.reload_all = reload_all

        reload_all()
