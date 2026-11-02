"""选股页入口：股票池与区间、标签页"""

import datetime as dt

import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant.stocks import UNIVERSES
from simplequant.stocks import universe as U, factors_for
from gui import state
from gui.common import t, p
from gui.layout import frame, page_title
from gui.plan_io import import_button
from gui.sel_panel import get_panel, store, universe_ready
from gui.widgets import hint, notice
from .common import Ctx, DENSE, kind_defaults, sp_state, switch_kind, WARMUP_DAYS
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
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-start gap-4"):
                def on_universe(e):
                    if e.value == SP["universe"]:      # 载入策略时由程序同步下拉框，不必再载入一次
                        return
                    old = U.kind(SP["universe"])
                    SP["universe"] = e.value
                    switch_kind(SP, old, U.kind(e.value))
                    reload_all()
                uni_sel = ui.select({u: p(UNIVERSES[u]["label"]) for u in UNIVERSES}, label=t("sp.universe"),
                                    value=SP["universe"], on_change=on_universe).props(DENSE).classes("w-48") \
                    .mark("sp_universe")
                start_in = ui.input(t("data.start")).props("outlined dense type=date").classes("w-44") \
                    .mark("sp_start")
                end_in = ui.input(t("data.end")).props("outlined dense type=date").classes("w-44").mark("sp_end")
                span = ui.label().classes("sq-muted text-sm self-center")
            warm = hint(t("sp.warmup_note_short"), t("sp.warmup_note"), classes="sq-muted text-xs", mark="sp_warmup")

        def set_range_inputs():
            u = SP["universe"]
            ok = universe_ready(u)
            for el in (start_in, end_in, span):
                el.set_visibility(ok)
            if not ok:
                ctx.lo = ctx.hi = None
                warm.set_visibility(False)
                return False
            idx = U.load_benchmark(u, store())
            ctx.lo, ctx.hi = idx.index[0].date(), idx.index[-1].date()
            a, b = SP["ranges"].get(u, (ctx.lo, ctx.hi))
            a, b = max(a, ctx.lo), min(b, ctx.hi)
            SP["ranges"][u] = (a, b)
            for el in (start_in, end_in):
                el.props(f"min={ctx.lo} max={ctx.hi}")
            start_in.value, end_in.value = str(a), str(b)
            span.text = t("gui.available", lo=ctx.lo, hi=ctx.hi)
            ctx.start, ctx.end = str(a), str(b)
            ctx.panel_start = max(str(ctx.lo), str((pd.Timestamp(ctx.start) - pd.Timedelta(days=WARMUP_DAYS)).date()))
            pw = SP.get("plan_warm")          # 导入的方案：按方案的预热起始日取面板，因子值才与导出方一致
            if pw and pw[0] == u and pw[1] == ctx.start:
                ctx.panel_start = max(str(ctx.lo), pw[2])
            warm.set_visibility(ctx.panel_start == ctx.start and str(ctx.lo) == ctx.start)
            return True

        def on_date(e, i):
            try:
                d = dt.date.fromisoformat(e.value)
            except (TypeError, ValueError):
                return
            u = SP["universe"]
            r = list(SP["ranges"].get(u, (ctx.lo, ctx.hi)))
            if r[i] == d:
                return
            r[i] = d
            if r[0] >= r[1]:
                return
            SP["ranges"][u] = tuple(r)
            reload_all()

        # ---------------- 标签页 ----------------
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_data = ui.tab("data", t("sp.tab_data"), icon="storage")
            tab_res = ui.tab("res", t("sp.tab_research"), icon="science")
            tab_cf = ui.tab("cf", t("sp.tab_custom"), icon="functions")
            tab_mine = ui.tab("mine", t("sp.tab_mine"), icon="auto_awesome")
            tab_bt = ui.tab("bt", t("sp.tab_backtest"), icon="query_stats")
            tab_wf = ui.tab("wf", t("sp.tab_wf"), icon="timeline")
        # 过渡提示（2.4.0 起，保留两个版本）：个股概览、持仓诊断移到了工具页
        ui.label(t("sp.moved_tools")).classes("sq-muted text-xs cursor-pointer") \
            .on("click", lambda: ui.navigate.to("/tools")).mark("sp_moved_tools")
        with ui.tab_panels(tabs, value=state.STATE.get("sp_tab", "bt")).classes("w-full bg-transparent") as panels:
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

        def need_panel_boxes():
            return (res_box, cf_box, mine_box, bt_box, wf_box)

        def factors_changed():
            """自定义因子保存 / 删除后：研究、回测、滚动优化里的因子列表重画（面板不用重新载入）"""
            kind = U.kind(SP["universe"])
            SP["factors"] = [f for f in SP["factors"] if f["key"] in factors_for(kind)] or \
                kind_defaults(kind)["factors"]
            if SP["res"]["fkey"] not in factors_for(kind):
                SP["res"]["fkey"] = factors_for(kind)[0]
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

        async def load_panel():
            for box in need_panel_boxes():
                box.clear()
                with box:
                    if ctx.lo is None:
                        notice(t("sp.no_data"), "info")
                        if box is bt_box:            # 没有数据也能导入方案（导入时下载）
                            import_button().classes("self-start")
                    else:
                        with ui.row().classes("items-center gap-2"):
                            ui.spinner(size="sm")
                            ui.label(t("sp.loading_panel")).classes("sq-muted")
            if ctx.lo is None:
                ctx.panel = None
                return
            key = (SP["universe"], ctx.panel_start, ctx.end)
            ctx.loading = key
            try:
                panel = await run.io_bound(get_panel, *key)
            except Exception as e:  # noqa: BLE001
                for box in need_panel_boxes():
                    box.clear()
                    with box:
                        notice(f"{type(e).__name__}: {e}", "error", "error")
                return
            if ctx.loading != key or panel is None:     # 期间又换了区间，以最新的为准；None = 程序退出时被取消
                return
            ctx.panel = panel
            state.STATE["sp_panel_key"] = key
            for box, build in ((res_box, _research), (cf_box, _custom_factors), (mine_box, _mining),
                               (bt_box, _backtest), (wf_box, _walkforward)):
                box.clear()
                with box:
                    build(SP, ctx)

        def reload_all():
            uni_sel.value = SP["universe"]        # 载入的策略换了股票池时，下拉框跟着变
            set_range_inputs()
            data_box.clear()
            with data_box:
                _data_tab(SP, ctx, reload_all)
            background_tasks.create(load_panel(), name="sp load panel")
        ctx.reload_all = reload_all

        start_in.on_value_change(lambda e: on_date(e, 0))
        end_in.on_value_change(lambda e: on_date(e, 1))
        reload_all()
