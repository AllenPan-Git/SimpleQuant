"""股票池与区间 + 面板载入：选股页与因子实验室共用（界面重构计划 4.3.1）

股票池、区间存在 sp_state() 里（SP["universe"]、SP["ranges"]），面板缓存在 gui/sel_panel.py，
所以两页看到的是同一个股票池和区间，换页不会重复载入同一个面板。
"""

import datetime as dt
from types import SimpleNamespace

import pandas as pd
from nicegui import run, ui

from simplequant.stocks import UNIVERSES
from simplequant.stocks import universe as U
from gui import state
from gui.common import t, p
from gui.sel_panel import get_panel, store, universe_ready
from gui.widgets import hint, notice
from .common import DENSE, WARMUP_DAYS, switch_kind


def universe_bar(SP, ctx, on_reload):
    """画「股票池 + 开始 / 结束日」一块；换股票池或改日期后调用 on_reload()（由页面重新载入面板）。

    返回 .sync()：把下拉框和日期框同步到 SP，并算好 ctx 的 lo / hi / start / end / panel_start；
    返回 False = 这个股票池还没有数据"""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start gap-4"):
            def on_universe(e):
                if e.value == SP["universe"]:      # 载入策略时由程序同步下拉框，不必再载入一次
                    return
                old = U.kind(SP["universe"])
                SP["universe"] = e.value
                switch_kind(SP, old, U.kind(e.value))
                on_reload()
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
        on_reload()

    def sync():
        uni_sel.value = SP["universe"]        # 载入的策略换了股票池时，下拉框跟着变
        return set_range_inputs()

    start_in.on_value_change(lambda e: on_date(e, 0))
    end_in.on_value_change(lambda e: on_date(e, 1))
    return SimpleNamespace(sync=sync)


def panel_loader(SP, ctx, builders, on_empty=None):
    """返回 load_panel()：在后台载入当前股票池与区间的面板，期间各区块显示「正在整理数据…」，载入后逐个重画。

    builders()：[(区块, 画法 build(SP, ctx)), ...]，每次载入时取一次；
    on_empty(区块)：没有数据时，在「暂无数据」提示下面补画的东西（例如导入方案按钮）"""
    async def load_panel():
        pairs = builders()
        for box, _ in pairs:
            box.clear()
            with box:
                if ctx.lo is None:
                    notice(t("sp.no_data"), "info")
                    if on_empty:
                        on_empty(box)
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
            for box, _ in pairs:
                box.clear()
                with box:
                    notice(f"{type(e).__name__}: {e}", "error", "error")
            return
        if ctx.loading != key or panel is None:     # 期间又换了区间，以最新的为准；None = 程序退出时被取消
            return
        ctx.panel = panel
        state.STATE["sp_panel_key"] = key
        for box, build in pairs:
            box.clear()
            with box:
                build(SP, ctx)
    return load_panel
