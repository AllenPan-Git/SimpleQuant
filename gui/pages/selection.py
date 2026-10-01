"""第 5 页：多因子选股（数据 / 因子研究 / 自定义因子 / 选股回测 / 滚动优化）"""

import datetime as dt
import os
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant import llm, strategies
from simplequant.engine.optimize import value_range, default_workers
from simplequant.engine.walkforward import make_windows
from simplequant.stocks import (StockStore, UNIVERSES, FACTORS, GROUPS, REBALANCE, WEIGHTING, DIVIDEND,
                                DEFAULT_FILTERS, analyze, compute, run_selection, today, factor_zscores,
                                factor_correlation, sample_dates)
from simplequant.stocks import custom_factors, universe as U, factors_for
from simplequant.bonds.panel import CB_FILTERS, CB_DEFAULT_FILTERS
from simplequant.stocks.walkforward import (walk_forward_selection, selection_tunables, selection_grid,
                                            REBALANCE_CHOICES, TARGET_METRICS as WF_TARGETS, MAX_COMBOS as WF_MAX)
from gui import state, history
from gui.common import t, p, lang
from gui.components import broker_settings, metric_tiles, metric_tile, walkforward_results
from gui.layout import frame, page_title
from gui.widgets import section, df_table, fmt_table, plot, notice, download_btn, Progress, code_editor
from ui.charts import ic_chart, quantile_chart, group_bar, equity_chart, corr_heatmap
from ui.shared import export_filename, default_range, fmt_metric, pct, num, orders_table, logs_table

DENSE = "outlined dense options-dense"
WARMUP_DAYS = 400          # 面板往前多取约 400 天，只用来预热因子；研究与回测从所选开始日起算
SEL_DEFAULT = [{"key": "ep", "weight": 1.0, "direction": 1}, {"key": "ret20", "weight": 1.0, "direction": -1}]
CB_SEL_DEFAULT = [{"key": "cb_double_low", "weight": 1.0, "direction": -1}]       # 可转债「双低」
# 股票与可转债各自记住的表单项（切换股票池时互换）
KIND_KEYS = ("factors", "excl_st", "min_list", "dividend", "ni", "ns")

_PANELS: OrderedDict = OrderedDict()    # (股票池, 起, 止, 数据版本) → 面板；最多留 3 个


def store() -> StockStore:
    return StockStore()


def sp_state() -> dict:
    sp = state.STATE.setdefault("sp", {
        "universe": "hs300", "ranges": {},
        # 选股回测表单
        "factors": [dict(f) for f in SEL_DEFAULT], "weighting": "manual", "lookback": 252, "ni": False, "ns": False,
        "top_n": 10, "reb": "monthly", "reb_n": 20, "excl_st": True,
        "min_list": DEFAULT_FILTERS["min_list_days"], "pos": 95, "name": "", "follow": False, "dividend": "reinvest",
        "ai_text": "", "ai": None, "result": None,
        # 因子研究
        "res": {"fkey": "ep", "horizon": 20, "groups": 5, "min_days": DEFAULT_FILTERS["min_list_days"],
                "ni": False, "ns": False},
        "report": None, "overview": None, "corr": None, "corr_keys": None,
        # 下载
        "dl": {"start": "2019-01-01", "workers": 4, "with_fin": True, "with_div": True}, "pack": None,
        # 滚动优化
        "wf_cfg": {"chosen": ["top_n", "rebalance"], "ranges": {}, "reb": None, "metric": WF_TARGETS[0],
                   "workers": default_workers(), "train": 24, "test": 6, "window": "rolling"},
        "wf": None,
        # 自定义因子编辑区（key 为空 = 新建）
        "cf": {"key": None, "name": "", "code": "", "direction": 1, "desc": ""}, "cf_trial": None,
    })
    # 可转债的选股条件（价格上限等；None = 不限）；旧版保存的界面状态里没有这两项
    sp.setdefault("cbf", {k: v for k, v in CB_DEFAULT_FILTERS.items() if k in CB_FILTERS})
    sp.setdefault("forms", {})
    sp["dl"].setdefault("cb_start", "2017-01-01")
    return sp


def kind_defaults(kind: str) -> dict:
    if kind == "cb":
        return {"factors": [dict(f) for f in CB_SEL_DEFAULT], "excl_st": False,
                "min_list": CB_DEFAULT_FILTERS["min_list_days"], "dividend": "reinvest", "ni": False, "ns": False}
    return {"factors": [dict(f) for f in SEL_DEFAULT], "excl_st": True, "min_list": DEFAULT_FILTERS["min_list_days"],
            "dividend": "reinvest", "ni": False, "ns": False}


def switch_kind(SP, old: str, new: str):
    """股票池在股票与可转债之间切换：各自的因子、条件分开记住"""
    if old == new:
        return
    SP.setdefault("forms", {})[old] = {k: SP[k] for k in KIND_KEYS if k in SP}
    SP.update(SP["forms"].get(new) or kind_defaults(new))
    usable = factors_for(new)
    if SP["res"]["fkey"] not in usable:
        SP["res"]["fkey"] = usable[0]
    SP["res"]["min_days"] = SP["min_list"]
    SP["corr_keys"] = None


def factor_label(k: str) -> str:
    return f"{p(GROUPS[FACTORS[k]['group']])} · {p(FACTORS[k]['label'])}"


def reb_label(r) -> str:
    return p(REBALANCE[r]) if r in REBALANCE else t("sel.every_n", n=r)


def data_version(st: StockStore, universe: str) -> str:
    """数据有更新时让缓存的面板失效"""
    files = U.data_files(universe, st)
    return "|".join(str(f.stat().st_mtime) if f.exists() else "-" for f in files)


def get_panel(universe: str, start: str, end: str):
    st = store()
    key = (universe, start, end, data_version(st, universe))
    if key in _PANELS:
        _PANELS.move_to_end(key)
        return _PANELS[key]
    panel = U.build(universe, start, end, st)
    _PANELS[key] = panel
    while len(_PANELS) > 3:
        _PANELS.popitem(last=False)
    return panel


def universe_ready(universe: str) -> bool:
    return U.ready(universe, store())


class Ctx:
    """页面内共享：当前面板、区间、各标签页的重画函数"""
    panel = None
    start = end = panel_start = None
    lo = hi = None


def page():
    SP = sp_state()
    ctx = Ctx()
    with frame("/selection"):
        page_title("/selection", t("sp.intro"))

        # ---------------- 股票池与区间 ----------------
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-start gap-4"):
                def on_universe(e):
                    old = U.kind(SP["universe"])
                    SP["universe"] = e.value
                    switch_kind(SP, old, U.kind(e.value))
                    reload_all()
                ui.select({u: p(UNIVERSES[u]["label"]) for u in UNIVERSES}, label=t("sp.universe"),
                          value=SP["universe"], on_change=on_universe).props(DENSE).classes("w-48").mark("sp_universe")
                start_in = ui.input(t("data.start")).props("outlined dense type=date").classes("w-44") \
                    .mark("sp_start")
                end_in = ui.input(t("data.end")).props("outlined dense type=date").classes("w-44").mark("sp_end")
                span = ui.label().classes("sq-muted text-sm self-center")
            warm = ui.label(t("sp.warmup_note")).classes("sq-muted text-xs")

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
            tab_bt = ui.tab("bt", t("sp.tab_backtest"), icon="query_stats")
            tab_wf = ui.tab("wf", t("sp.tab_wf"), icon="timeline")
        with ui.tab_panels(tabs, value=state.STATE.get("sp_tab", "bt")).classes("w-full bg-transparent") as panels:
            with ui.tab_panel(tab_data).classes("px-0 gap-4"):
                data_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_res).classes("px-0 gap-4"):
                res_box = ui.column().classes("w-full gap-4")
            with ui.tab_panel(tab_cf).classes("px-0 gap-4"):
                cf_box = ui.column().classes("w-full gap-4")
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
            return (res_box, cf_box, bt_box, wf_box)

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

        async def load_panel():
            for box in need_panel_boxes():
                box.clear()
                with box:
                    if ctx.lo is None:
                        notice(t("sp.no_data"), "info")
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
            if ctx.loading != key:          # 期间又换了区间，以最新的为准
                return
            ctx.panel = panel
            state.STATE["sp_panel_key"] = key
            for box, build in ((res_box, _research), (cf_box, _custom_factors), (bt_box, _backtest),
                               (wf_box, _walkforward)):
                box.clear()
                with box:
                    build(SP, ctx)

        def reload_all():
            set_range_inputs()
            data_box.clear()
            with data_box:
                _data_tab(SP, ctx, reload_all)
            background_tasks.create(load_panel(), name="sp load panel")
        ctx.reload_all = reload_all

        start_in.on_value_change(lambda e: on_date(e, 0))
        end_in.on_value_change(lambda e: on_date(e, 1))
        reload_all()


# ================= 数据 =================
def _data_tab(SP, ctx, reload_all):
    universe = SP["universe"]
    if U.kind(universe) == "cb":
        _cb_data_tab(SP, ctx, reload_all)
        return
    st = store()
    if ctx.lo is not None:
        uni = st.load_universe(universe)
        codes = sorted(uni["code"].unique())
        have = sum(st.has(c) for c in codes)
        have_fin = sum(st.has_fin(c) for c in codes)
        have_div = sum(st.has_div(c) for c in codes)
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
            metric_tile(t("sp.snapshots"), str(uni["date"].nunique()))
            metric_tile(t("sp.codes"), str(len(codes)), help_text=t("sp.codes_help"))
            metric_tile(t("sp.downloaded"), f"{have}/{len(codes)}")
            metric_tile(t("sp.fin_downloaded"), f"{have_fin}/{len(codes)}", help_text=t("sp.fin_help"))
            metric_tile(t("sp.div_downloaded"), f"{have_div}/{len(codes)}", help_text=t("sp.div_help"))
    else:
        notice(t("sp.no_data"), "info")

    D = SP["dl"]
    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-4"):
            d_start = ui.input(t("data.start"), value=D["start"]).props("outlined dense type=date").classes("w-44") \
                .tooltip(t("sp.dl_start_help"))
            d_end = ui.input(t("data.end"), value=str(dt.date.today())).props("outlined dense type=date") \
                .classes("w-44")
            workers = ui.number(t("sp.workers"), value=D["workers"], min=1, max=8, precision=0) \
                .props("outlined dense").classes("w-32").tooltip(t("sp.workers_help"))
            with_fin = ui.switch(t("sp.with_fin"), value=D["with_fin"]).tooltip(t("sp.fin_help"))
            with_div = ui.switch(t("sp.with_div"), value=D.get("with_div", True)).tooltip(t("sp.div_help"))
        ui.label(t("sp.download_note")).classes("sq-muted text-sm")
        prog = Progress()
        out = ui.column().classes("w-full")

        async def download():
            D.update(start=d_start.value, workers=int(workers.value or 4), with_fin=with_fin.value,
                     with_div=with_div.value)
            s, e = d_start.value, min(d_end.value, today())
            w = int(workers.value or 4)
            btn.disable()
            out.clear()
            prog.start()
            t0 = time.time()

            fin, div = with_fin.value, with_div.value

            def work():
                # 进度文字：步骤 · 进度 · 用时（后台线程只写进度条的数值，界面由定时器刷新）
                def step(label, t_start):
                    return lambda i, n: prog.set(i / n, f"{label} · {i}/{n} · {time.time() - t_start:.0f}s")
                prog.set(0, t("sp.step_universe"))
                u = st.update_universe(universe, s, e, progress=step(t("sp.step_universe"), t0))
                prog.set(0, t("sp.step_index"))
                st.update_index(UNIVERSES[universe]["index"], s, e)
                st.update_basics()
                codes = sorted(u["code"].unique())
                errs = st.update(codes, s, e, workers=w, progress=step(t("sp.step_stocks", n=len(codes)), time.time()))
                if fin:
                    prog.set(0, t("sp.step_fin", n=len(codes)))
                    st.update_industry()
                    errs.update(st.update_fundamentals(codes, int(s[:4]) - 1, workers=w,
                                                       progress=step(t("sp.step_fin", n=len(codes)), time.time())))
                if div:
                    prog.set(0, t("sp.step_div", n=len(codes)))
                    errs.update(st.update_dividends(codes, int(s[:4]) - 1, workers=w,
                                                    progress=step(t("sp.step_div", n=len(codes)), time.time())))
                return codes, errs
            try:
                codes, errs = await run.io_bound(work)
                ui.notify(t("sp.download_done", n=len(codes)), type="positive")
                if errs:
                    with out:
                        notice(t("sp.download_errors", n=len(errs)) + "\n\n" +
                               "\n".join(f"- {c}: {m}" for c, m in list(errs.items())[:10]), "warning", "warning")
                    return
            except Exception as ex:  # noqa: BLE001
                with out:
                    notice(t("data.fetch_failed", sym=universe) + f"：{type(ex).__name__}: {ex}", "error", "error")
                return
            finally:
                prog.stop()
                btn.enable()
            reload_all()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("sp_download")

    with ui.expansion(t("sp.pack"), icon="folder_zip").classes("w-full q-card"):
        ui.label(t("sp.pack_note")).classes("sq-muted text-sm")
        with ui.row().classes("w-full items-start gap-6"):
            with ui.column().classes("gap-2"):
                pack_box = ui.column()

                async def build_pack():
                    b.disable()
                    try:
                        SP["pack"] = await run.io_bound(st.export_zip)
                    finally:
                        b.enable()
                    show_pack()

                def show_pack():
                    pack_box.clear()
                    if SP.get("pack"):
                        with pack_box:
                            download_btn(t("sp.pack_download", mb=f"{len(SP['pack']) / 1e6:.0f}"), lambda: SP["pack"],
                                         f"simplequant_stocks_{today()}.zip", "application/zip")
                b = ui.button(t("sp.pack_build"), icon="archive", on_click=build_pack).props("outline no-caps")
                show_pack()
            with ui.column().classes("gap-2 grow"):
                async def on_zip(e):
                    import io
                    try:
                        n = st.import_zip(io.BytesIO(await e.file.read()))
                        ui.notify(t("sp.pack_imported", n=n), type="positive")
                        reload_all()
                    except Exception as ex:  # noqa: BLE001
                        ui.notify(f"{type(ex).__name__}: {ex}", type="negative", multi_line=True)
                ui.upload(label=t("sp.pack_import"), auto_upload=True, on_upload=on_zip) \
                    .props("accept=.zip flat bordered").classes("w-full")


def _cb_data_tab(SP, ctx, reload_all):
    """可转债全市场：列表、条款、日线、中证转债指数"""
    from simplequant.bonds import CBStore
    st = CBStore()
    if ctx.lo is not None:
        lst, info = st.load_list(), st.load_info()
        have = sum(st.has(c) for c in lst["code"])
        now = pd.Timestamp(dt.date.today())
        trading = sum(1 for c in lst["code"] if st.has(c) and c in info.index
                      and (pd.isna(info.at[c, "delist_date"]) or info.at[c, "delist_date"] > now))
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            metric_tile(t("cb.total"), str(len(lst)), help_text=t("cb.total_help"))
            metric_tile(t("cb.downloaded"), str(have), help_text=t("cb.downloaded_help"))
            metric_tile(t("cb.trading"), str(trading))
            metric_tile(t("cb.as_of"), str(ctx.hi))
    else:
        notice(t("sp.no_data"), "info")

    D = SP["dl"]
    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-4"):
            d_start = ui.input(t("data.start"), value=D["cb_start"]).props("outlined dense type=date") \
                .classes("w-44").tooltip(t("cb.start_help"))
            workers = ui.number(t("sp.workers"), value=min(int(D["workers"]), 3), min=1, max=4, precision=0) \
                .props("outlined dense").classes("w-32").tooltip(t("cb.workers_help"))
        ui.label(t("cb.download_note")).classes("sq-muted text-sm")
        prog = Progress()
        out = ui.column().classes("w-full")
        steps = {"list": t("cb.step_list"), "info": t("cb.step_info"), "index": t("cb.step_index"),
                 "daily": t("cb.step_daily")}

        async def download():
            D["cb_start"] = d_start.value
            s, e, w = d_start.value, today(), int(workers.value or 3)
            btn.disable()
            out.clear()
            prog.start()
            t0 = time.time()

            def progress(step, i, n):
                prog.set(i / n if n else 0, f"{steps[step]} · {i}/{n} · {time.time() - t0:.0f}s")
            try:
                errs = await run.io_bound(st.update_all, s, e, w, progress)
                ui.notify(t("cb.download_done"), type="positive")
                if errs:
                    with out:
                        notice(t("sp.download_errors", n=len(errs)) + "\n\n" +
                               "\n".join(f"- {c}: {m}" for c, m in list(errs.items())[:10]), "warning", "warning")
                    return
            except Exception as ex:  # noqa: BLE001
                with out:
                    notice(t("data.fetch_failed", sym=p(UNIVERSES["cb"]["label"])) + f"：{type(ex).__name__}: {ex}",
                           "error", "error")
                return
            finally:
                prog.stop()
                btn.enable()
            reload_all()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("cb_download")

    with ui.expansion(t("cb.rules"), icon="rule").classes("w-full q-card"):
        ui.markdown(t("cb.rules_text")).classes("text-sm")


# ================= 因子研究 =================
def _research(SP, ctx):
    lg = lang()
    panel, R = ctx.panel, SP["res"]
    kinds = factors_for(panel.kind)
    if R["fkey"] not in kinds:
        R["fkey"] = kinds[0]
    usable = [k for k in kinds if panel.has_fin or not FACTORS[k].get("requires_fin")]

    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-start gap-3"):
            def set_r(k, cast=None):
                def f(e):
                    if e.value is not None:
                        R[k] = cast(e.value) if cast else e.value
                        hints()
                return f
            fsel = ui.select({k: factor_label(k) for k in kinds}, label=t("sp.factor"), value=R["fkey"],
                             with_input=True, on_change=set_r("fkey")).props(DENSE).classes("grow min-w-[260px]") \
                .mark("sp_res_factor")
            ui.select({h: str(h) for h in (5, 10, 20, 60)}, label=t("sp.horizon"), value=R["horizon"],
                      on_change=set_r("horizon")).props(DENSE).classes("w-32").tooltip(t("sp.horizon_help"))
            ui.select({g: str(g) for g in (5, 10)}, label=t("sp.groups"), value=R["groups"],
                      on_change=set_r("groups")).props(DENSE).classes("w-28")
            ui.number(t("sp.min_days"), value=R["min_days"], min=0, max=1000, step=50, precision=0,
                      on_change=set_r("min_days", int)).props("outlined dense").classes("w-36") \
                .tooltip(t("sp.min_days_help"))
        with ui.row().classes("items-center gap-6"):
            ni = ui.switch(t("sp.neutral_industry"), value=R["ni"], on_change=set_r("ni")) \
                .tooltip(t("sp.neutral_industry_help"))
            ni.set_enabled(not panel.industry.dropna().empty)
            ns = ui.switch(t("sp.neutral_size"), value=R["ns"], on_change=set_r("ns")).tooltip(t("sp.neutral_size_help"))
            ns.set_enabled(panel.has_fin or panel.kind == "cb")
        if not panel.has_fin and panel.kind == "stock":
            ui.label(t("sp.no_fin")).classes("sq-muted text-sm")
        hint_box = ui.column().classes("w-full")

        def hints():
            hint_box.clear()
            fsel.tooltip(p(FACTORS[R["fkey"]].get("desc", "")) or "")
            if FACTORS[R["fkey"]].get("requires_fin") and not panel.has_fin:
                with hint_box:
                    notice(t("sp.need_fin"), "warning", "warning")
        hints()

        def factor_values(k, mask):
            neutral = {"industry": R["ni"], "size": R["ns"]}
            return factor_zscores(panel, k, mask, neutral) if (R["ni"] or R["ns"]) else compute(panel, k)

        prog = Progress()

        async def do_analyze():
            b1.disable()
            k, h, g = R["fkey"], R["horizon"], R["groups"]
            try:
                def work():
                    mask = panel.eligible(True, int(R["min_days"]))
                    return analyze(panel, factor_values(k, mask), mask, h, g, start=ctx.start)
                SP["report"] = (k, h, await run.io_bound(work))
            except Exception as e:  # noqa: BLE001
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                b1.enable()
            report.refresh()

        async def do_overview():
            b2.disable()
            h, g = R["horizon"], R["groups"]
            prog.start()

            def work():
                mask = panel.eligible(True, int(R["min_days"]))
                rows = []
                for i, k in enumerate(usable, 1):
                    r = analyze(panel, factor_values(k, mask), mask, h, g, start=ctx.start)
                    s = r.summary
                    rows.append({"key": k, t("sp.factor"): factor_label(k), t("sp.ic_mean"): s.mean,
                                 t("sp.icir"): s.ir_annual, t("sp.t_stat"): s.t_stat, t("sp.ic_pos"): s.positive,
                                 t("sp.mono"): r.monotonicity,
                                 t("sp.suggest"): t("sel.dir_up") if s.mean > 0 else t("sel.dir_down")})
                    prog.set(i / len(usable), f"{i}/{len(usable)}")
                return pd.DataFrame(rows)
            try:
                SP["overview"] = (h, await run.io_bound(work))
            except Exception as e:  # noqa: BLE001
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                prog.stop()
                b2.enable()
            overview.refresh()

        with ui.row().classes("gap-3"):
            b1 = ui.button(t("sp.analyze"), icon="science", on_click=do_analyze).props("unelevated no-caps") \
                .mark("sp_analyze")
            b2 = ui.button(t("sp.overview"), icon="table_chart", on_click=do_overview).props("outline no-caps") \
                .tooltip(t("sp.overview_help")).mark("sp_overview")

    @ui.refreshable
    def overview():
        if not SP.get("overview"):
            return
        h, table = SP["overview"]
        with ui.card().classes("w-full gap-2"):
            ui.label(t("sp.overview_title", h=h)).classes("font-semibold")
            show = fmt_table(table.drop(columns="key"), {t("sp.ic_mean"): "{:.3f}", t("sp.icir"): "{:.2f}",
                                                         t("sp.t_stat"): "{:.2f}", t("sp.ic_pos"): "{:.0%}",
                                                         t("sp.mono"): "{:.2f}"})
            df_table(show)
            ui.label(t("sp.overview_note")).classes("sq-muted text-xs")
    overview()

    @ui.refreshable
    def report():
        if not SP.get("report"):
            return
        k, h, rep = SP["report"]
        s = rep.summary
        ui.label(f"{factor_label(k)} · " + t("sp.horizon_n", h=h)).classes("text-lg font-semibold")
        with ui.grid().classes("w-full gap-3 grid-cols-3 md:grid-cols-6"):
            metric_tile(t("sp.ic_mean"), num(s.mean, 3), help_text=t("sp.ic_help"))
            metric_tile(t("sp.icir"), num(s.ir_annual), help_text=t("sp.icir_help"))
            metric_tile(t("sp.t_stat"), num(s.t_stat), help_text=t("sp.t_help"))
            metric_tile(t("sp.ic_pos"), pct(s.positive, 0))
            metric_tile(t("sp.mono"), num(rep.monotonicity), help_text=t("sp.mono_help"))
            metric_tile(t("sp.top_turnover"), pct(rep.top_turnover, 0), help_text=t("sp.top_turnover_help"))
        strong = abs(s.t_stat) > 2 and abs(rep.monotonicity) >= 0.8
        verdict = ("sp.verdict_up" if s.mean > 0 else "sp.verdict_down") if strong else "sp.verdict_weak"
        notice(t(verdict, n=s.n), "lightbulb", "info" if strong else "warning")
        with ui.row().classes("w-full gap-4 no-wrap max-md:flex-wrap"):
            with ui.card().classes("p-2").style("flex: 3; min-width: 320px"):
                plot(quantile_chart(rep.cumulative, lg))
            with ui.card().classes("p-2").style("flex: 2; min-width: 280px"):
                plot(group_bar(rep.annual, lg))
        with ui.card().classes("w-full p-2"):
            plot(ic_chart(rep.ic, lg))
    report()

    with ui.expansion(t("sp.corr"), icon="grid_view").classes("w-full q-card"):
        ui.label(t("sp.corr_note")).classes("sq-muted text-sm")
        default = [k for k in ("ep", "bp", "roe", "np_yoy", "vol60", "turn20", "ret20", "size", "cb_double_low",
                               "cb_price", "cb_premium", "cb_bond_premium", "cb_issue_size") if k in usable]
        keys = [k for k in (SP.get("corr_keys") or default) if k in usable]

        def on_keys(e):
            SP["corr_keys"] = list(e.value or [])
            cb.set_enabled(len(SP["corr_keys"]) >= 2)
        ui.select({k: factor_label(k) for k in usable}, label=t("sp.pick_factors"), value=keys, multiple=True,
                  on_change=on_keys).props(DENSE + " use-chips").classes("w-full")
        SP["corr_keys"] = keys

        async def do_corr():
            cb.disable()
            try:
                def work():
                    mask = panel.eligible(True, int(R["min_days"]))
                    dates = sample_dates(panel.calendar[panel.calendar >= pd.Timestamp(ctx.start)], 20)
                    return factor_correlation(panel, SP["corr_keys"], mask, dates)
                SP["corr"] = await run.io_bound(work)
            finally:
                cb.enable()
            corr.refresh()
        cb = ui.button(t("sp.corr_run"), icon="grid_view", on_click=do_corr).props("outline no-caps") \
            .classes("self-start")
        cb.set_enabled(len(keys) >= 2)

        @ui.refreshable
        def corr():
            if SP.get("corr") is not None:
                plot(corr_heatmap(SP["corr"], {k: p(FACTORS[k]["label"]) for k in FACTORS}, lg))
        corr()


# ================= 自定义因子 =================
def _custom_factors(SP, ctx):
    lg = lang()
    panel, C = ctx.panel, SP["cf"]
    if not C["code"].strip():
        C["code"] = custom_factors.skeleton(lg)

    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2 no-wrap sq-note py-2"):
            ui.icon("info").classes("text-primary")
            ui.label(t("cf.intro")).classes("text-sm")

        @ui.refreshable
        def saved_list():
            saved = custom_factors.list_factors()
            if not saved:
                ui.label(t("cf.none")).classes("sq-muted text-sm")
                return
            ui.label(t("cf.saved", n=len(saved))).classes("font-semibold")
            for k, d in saved.items():
                with ui.row().classes("w-full items-center gap-3 no-wrap py-1"):
                    ui.icon("functions").classes("text-primary")
                    with ui.column().classes("grow gap-0"):
                        ui.label(d["name"]).classes("font-medium")
                        how = t("sel.dir_up" if d.get("direction", 1) > 0 else "sel.dir_down")
                        ui.label(" · ".join(x for x in (how, d.get("desc", "")) if x)).classes("sq-muted text-xs")
                    ui.button(t("cf.edit"), on_click=lambda k=k: edit(k)).props("flat no-caps dense") \
                        .mark(f"cf_edit:{k}")
                    ui.button(t("strat.delete"), on_click=lambda k=k: delete(k)) \
                        .props("flat no-caps dense color=negative").mark(f"cf_del:{k}")
        saved_list()

    with ui.card().classes("w-full gap-3"):
        editing = ui.label().classes("font-semibold")
        with ui.row().classes("w-full items-end gap-3"):
            name = ui.input(t("cf.name"), value=C["name"], placeholder=t("cf.name_ph")).props("outlined dense") \
                .classes("w-64").mark("cf_name")
            direction = ui.toggle({1: t("sel.dir_up"), -1: t("sel.dir_down")}, value=C["direction"]) \
                .props("dense no-caps unelevated rounded toggle-color=primary").tooltip(t("cf.direction_help"))
            desc = ui.input(t("cf.desc"), value=C["desc"]).props("outlined dense").classes("grow")
            ui.button(t("cf.new"), icon="add", on_click=lambda: edit(None)) \
                .props("flat no-caps dense color=primary").mark("cf_new")

        def on_code(e):
            C["code"] = e.value or ""
            errs = custom_factors.check_syntax(C["code"], lg)
            err_label.text = errs[0] if errs else ""
            err_label.set_visibility(bool(errs))
        editor = code_editor(C["code"], on_code, height=360).mark("cf_code")
        err_label = ui.label().classes("text-negative text-sm")
        err_label.set_visibility(False)
        with ui.expansion(t("code.cheatsheet"), icon="menu_book").classes("w-full").props("dense"):
            ui.label(t("cf.cheatsheet")).classes("sq-code w-full whitespace-pre text-xs overflow-x-auto")
        with ui.row().classes("items-center gap-3"):
            trial_btn = ui.button(t("cf.trial"), icon="science", on_click=lambda: trial()) \
                .props("outline no-caps").mark("cf_trial").tooltip(t("cf.trial_help"))
            ui.button(t("strat.save"), icon="save", on_click=lambda: save()).props("unelevated no-caps") \
                .mark("cf_save")

    def sync():
        C["name"], C["direction"], C["desc"] = (name.value or "").strip(), int(direction.value or 1), desc.value or ""
        editing.text = t("cf.editing", name=C["name"]) if C["key"] else t("cf.creating")
    for el in (name, direction, desc):
        el.on_value_change(lambda _: sync())
    sync()

    def edit(key):
        d = custom_factors.list_factors().get(key) if key else None
        C.update(key=key if d else None, name=d["name"] if d else "",
                 code=d["code"] if d else custom_factors.skeleton(lg),
                 direction=d.get("direction", 1) if d else 1, desc=d.get("desc", "") if d else "")
        SP["cf_trial"] = None
        name.value, direction.value, desc.value, editor.value = C["name"], C["direction"], C["desc"], C["code"]
        sync()
        trial_box.refresh()

    def save():
        sync()
        if not C["name"]:
            ui.notify(t("cf.need_name"), type="warning")
            return
        try:
            custom_factors.compile_factor(C["code"], lg)
        except custom_factors.FactorCodeError as e:
            ui.notify(str(e), type="negative", multi_line=True)
            return
        C["key"] = custom_factors.save_factor(C["name"], C["code"], C["direction"], C["desc"], key=C["key"])
        ui.notify(t("strat.saved", name=C["name"]), type="positive")
        sync()
        saved_list.refresh()
        ctx.factors_changed()

    async def delete(key):
        d = custom_factors.list_factors().get(key)
        if not d:
            return
        with ui.dialog() as dlg, ui.card():
            ui.label(t("cf.delete_confirm", name=d["name"]))
            with ui.row().classes("w-full justify-end"):
                ui.button(t("gui.cancel"), on_click=lambda: dlg.submit(False)).props("flat no-caps")
                ui.button(t("strat.delete"), on_click=lambda: dlg.submit(True)) \
                    .props("unelevated no-caps color=negative").mark("confirm-delete")
        ok = await dlg
        dlg.delete()
        if not ok:
            return
        custom_factors.delete_factor(key)
        if C["key"] == key:
            C["key"] = None
            sync()
        saved_list.refresh()
        ctx.factors_changed()

    async def trial():
        """在当前面板上算一遍：覆盖率 + 20 日 IC 与分组收益，保存前先看看因子有没有用"""
        code = C["code"]
        trial_btn.disable()
        try:
            def work():
                raw = custom_factors.evaluate(code, panel, lg)
                mask = panel.eligible(True, int(SP["res"]["min_days"]))
                period = raw.index >= pd.Timestamp(ctx.start)
                m = mask[period]
                cover = float(raw[period].where(m).notna().sum().sum() / max(int(m.sum().sum()), 1))
                return cover, analyze(panel, raw, mask, 20, 5, start=ctx.start)
            SP["cf_trial"] = await run.io_bound(work)
        except custom_factors.FactorCodeError as e:
            SP["cf_trial"] = None
            ui.notify(str(e), type="negative", multi_line=True)
        except Exception as e:  # noqa: BLE001
            SP["cf_trial"] = None
            ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
        finally:
            trial_btn.enable()
        trial_box.refresh()

    @ui.refreshable
    def trial_box():
        if not SP.get("cf_trial"):
            return
        cover, rep = SP["cf_trial"]
        s = rep.summary
        with ui.card().classes("w-full gap-3"):
            ui.label(t("cf.trial_title")).classes("font-semibold")
            with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
                metric_tile(t("cf.coverage"), pct(cover, 0), help_text=t("cf.coverage_help"))
                metric_tile(t("sp.ic_mean"), num(s.mean, 3), help_text=t("sp.ic_help"))
                metric_tile(t("sp.icir"), num(s.ir_annual), help_text=t("sp.icir_help"))
                metric_tile(t("sp.t_stat"), num(s.t_stat), help_text=t("sp.t_help"))
                metric_tile(t("sp.mono"), num(rep.monotonicity), help_text=t("sp.mono_help"))
            if cover < 0.5:
                notice(t("cf.low_coverage"), "warning", "warning")
            if (s.mean < 0 < C["direction"]) or (s.mean > 0 > C["direction"]):
                notice(t("cf.direction_hint"), "lightbulb", "info")
            with ui.row().classes("w-full gap-4 no-wrap max-md:flex-wrap"):
                with ui.card().classes("p-2").style("flex: 3; min-width: 320px"):
                    plot(quantile_chart(rep.cumulative, lg))
                with ui.card().classes("p-2").style("flex: 2; min-width: 280px"):
                    plot(group_bar(rep.annual, lg))
    trial_box()


# ================= 选股回测 =================
def current_spec(SP) -> dict:
    cb = U.kind(SP["universe"]) == "cb"
    filters = {"exclude_st": bool(SP["excl_st"]) and not cb, "min_list_days": int(SP["min_list"])}
    if cb:
        filters.update({k: SP["cbf"].get(k) for k in CB_FILTERS})
    return {"kind": "selection", "universe": SP["universe"],
            "factors": [dict(f) for f in SP["factors"]], "top_n": int(SP["top_n"]),
            "rebalance": SP["reb"] if SP["reb"] != "n" else int(SP["reb_n"]),
            "filters": filters,
            "position_pct": int(SP["pos"]), "weighting": SP["weighting"], "ic_lookback": int(SP["lookback"]),
            "neutralize": {"industry": bool(SP["ni"]), "size": bool(SP["ns"])},
            **({"dividend": "cash"} if SP.get("dividend") == "cash" and not cb else {})}


def apply_spec(SP, spec: dict, name: str = ""):
    """把选股策略描述填进表单（载入已保存的策略、AI 生成的策略、滚动优化的最新参数都用它）"""
    old = U.kind(SP["universe"])
    SP["universe"] = spec.get("universe", SP["universe"])
    kind = U.kind(SP["universe"])
    switch_kind(SP, old, kind)
    SP["factors"] = [{"key": f["key"], "weight": float(f.get("weight", 1)), "direction": int(f.get("direction", 1))}
                     for f in spec["factors"] if f["key"] in factors_for(kind)]   # 已删除的自定义因子跳过
    SP["top_n"] = int(spec["top_n"])
    reb = spec.get("rebalance", "monthly")
    SP["reb"] = reb if reb in REBALANCE else "n"
    if reb not in REBALANCE:
        SP["reb_n"] = int(reb)
    flt = {**(CB_DEFAULT_FILTERS if kind == "cb" else DEFAULT_FILTERS), **(spec.get("filters") or {})}
    SP["excl_st"], SP["min_list"] = bool(flt["exclude_st"]), int(flt["min_list_days"])
    if kind == "cb":
        SP["cbf"] = {k: flt.get(k) for k in CB_FILTERS}
    SP["pos"] = int(spec.get("position_pct", 95))
    SP["weighting"] = spec.get("weighting", "manual")
    SP["lookback"] = int(spec.get("ic_lookback", 252))
    neutral = spec.get("neutralize") or {}
    SP["ni"], SP["ns"] = bool(neutral.get("industry")), bool(neutral.get("size"))
    SP["dividend"] = spec.get("dividend", "reinvest")
    SP["name"] = name


def _backtest(SP, ctx):
    box = ui.column().classes("w-full gap-4")

    def rebuild():
        """表单整体换了内容（载入策略 / AI / 滚动优化结果）；股票池变了要重新载入面板"""
        if SP["universe"] != state.STATE.get("sp_panel_key", (None,))[0]:
            ctx.reload_all()
            return
        box.clear()
        with box:
            _backtest_form(SP, ctx, rebuild)
    ctx.rebuild_bt = rebuild
    with box:
        _backtest_form(SP, ctx, rebuild)


def _backtest_form(SP, ctx, rebuild):
    lg = lang()
    panel = ctx.panel

    # ---- 用一句话描述（AI） ----
    cfg = llm.load_config()
    ready = bool(cfg and cfg.ready)
    with ui.expansion(t("sp.ai"), icon="auto_awesome", value=bool(SP.get("ai"))).classes("w-full q-card"):
        if not ready:
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                notice(t("ai.need_config"), "key")
                ui.button(t("nav.go_settings"), on_click=lambda: ui.navigate.to("/settings")) \
                    .props("flat no-caps dense color=primary icon-right=arrow_forward")
        ai_text = ui.textarea(t("ai.describe"), value=SP["ai_text"], placeholder=t("sp.ai_placeholder")) \
            .props("outlined autogrow").classes("w-full").mark("sp_ai_text")

        async def ai_go():
            SP["ai_text"] = ai_text.value or ""
            ai_btn.disable()
            try:
                SP["ai"] = await run.io_bound(llm.translate_selection, SP["ai_text"], llm.get_provider(cfg), lg)
            except llm.LLMError as e:
                SP["ai"] = None
                ui.notify(str(e), type="negative", multi_line=True)
            except Exception as e:  # noqa: BLE001
                SP["ai"] = None
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                ai_btn.enable()
            ai_result.refresh()
        ai_btn = ui.button(t("ai.generate"), icon="auto_awesome", on_click=ai_go).props("outline no-caps") \
            .classes("self-start").mark("sp_ai_go")
        ai_btn.set_enabled(ready and bool(SP["ai_text"].strip()))
        ai_text.on_value_change(lambda e: ai_btn.set_enabled(ready and bool((e.value or "").strip())))

        @ui.refreshable
        def ai_result():
            res = SP.get("ai")
            if not res:
                return
            notice(res.understood or "—", "psychology")
            if res.unsupported:
                notice(t("ai.unsupported") + "\n\n" + "\n".join(f"- {u}" for u in res.unsupported), "block",
                       "warning")
            for e in res.errors:
                notice(e, "error", "error")
            if res.ok:
                ui.label(strategies.describe(res.spec, lg)).classes("sq-code w-full")

                def apply_ai():
                    apply_spec(SP, res.spec, res.name)
                    rebuild()
                ui.button(t("sp.ai_apply"), icon="check", on_click=apply_ai).props("unelevated no-caps") \
                    .classes("self-start").mark("sp_ai_apply")
        ai_result()

    saved = {n: s for n, s in strategies.list_strategies().items() if s.get("kind") == "selection"}
    if saved:
        with ui.row().classes("w-full items-end gap-3"):
            pick_saved = ui.select(list(saved), label=t("sp.load_saved"), value=next(iter(saved))) \
                .props(DENSE).classes("grow")

            def load_saved():
                apply_spec(SP, saved[pick_saved.value], pick_saved.value)
                rebuild()
            ui.button(t("strat.load"), on_click=load_saved).props("outline no-caps")

    # ---- 因子 ----
    with section(t("sp.factor_setup"), "functions"):
        def on_factors(e):
            keys = list(e.value or [])
            old = {f["key"]: f for f in SP["factors"]}
            SP["factors"] = [old.get(k) or {"key": k, "weight": 1.0, "direction": FACTORS[k]["direction"]}
                             for k in keys]
            factor_rows.refresh()
            changed()
        ui.select({k: factor_label(k) for k in factors_for(panel.kind)}, label=t("sp.pick_factors"),
                  value=[f["key"] for f in SP["factors"]], multiple=True, with_input=True, on_change=on_factors) \
            .props(DENSE + " use-chips").classes("w-full").mark("sp_factors")
        fin_warn = ui.column().classes("w-full")
        with ui.row().classes("w-full items-center gap-4"):
            def on_weighting(e):
                SP["weighting"] = e.value
                lookback.set_enabled(e.value != "manual")
                factor_note.text = note_text()
                changed()
            with ui.column().classes("gap-0"):
                ui.label(t("sp.weighting")).classes("text-xs sq-muted").tooltip(t("sp.weighting_help"))
                ui.radio({w: p(WEIGHTING[w]) for w in WEIGHTING}, value=SP["weighting"], on_change=on_weighting) \
                    .props("inline dense").mark("sp_weighting")
            lookback = ui.number(t("sp.lookback"), value=SP["lookback"], min=60, max=1000, step=21, precision=0,
                                 on_change=lambda e: set_sp("lookback", e.value, int)) \
                .props("outlined dense").classes("w-36").tooltip(t("sp.lookback_help"))
            lookback.set_enabled(SP["weighting"] != "manual")

        @ui.refreshable
        def factor_rows():
            for f in SP["factors"]:
                with ui.row().classes("w-full items-center gap-4 no-wrap"):
                    ui.label(factor_label(f["key"])).classes("grow font-medium")

                    def on_w(e, f=f):
                        if e.value is not None:
                            f["weight"] = float(e.value)
                            changed()

                    def on_d(e, f=f):
                        f["direction"] = int(e.value)
                        changed()
                    ui.number(t("sp.weight"), value=f["weight"], min=0, max=10, step=0.5, on_change=on_w) \
                        .props("outlined dense").classes("w-28")
                    ui.toggle({1: t("sel.dir_up"), -1: t("sel.dir_down")}, value=int(f["direction"]),
                              on_change=on_d).props("dense no-caps unelevated toggle-color=primary")
        factor_rows()

        def note_text():
            return t("sp.factor_note") + ("" if SP["weighting"] == "manual" else " " + t("sp.ic_fallback_note"))
        factor_note = ui.label(note_text()).classes("sq-muted text-xs")
        with ui.row().classes("items-center gap-6"):
            ni = ui.switch(t("sp.neutral_industry"), value=SP["ni"], on_change=lambda e: set_sp("ni", e.value)) \
                .tooltip(t("sp.neutral_industry_help")).mark("sp_ni")
            ni.set_enabled(not panel.industry.dropna().empty)
            ns = ui.switch(t("sp.neutral_size"), value=SP["ns"], on_change=lambda e: set_sp("ns", e.value)) \
                .tooltip(t("cb.neutral_size_help") if panel.kind == "cb" else t("sp.neutral_size_help"))
            ns.set_enabled(panel.has_fin or panel.kind == "cb")

    # ---- 选股规则 ----
    with section(t("sp.rules"), "filter_alt"):
        with ui.row().classes("w-full items-center gap-4"):
            ui.number(t("cb.top_n" if panel.kind == "cb" else "sp.top_n"), value=SP["top_n"], min=1, max=100, step=1, precision=0,
                      on_change=lambda e: set_sp("top_n", e.value, int)).props("outlined dense").classes("w-32") \
                .mark("sp_top_n")

            def on_reb(e):
                SP["reb"] = e.value
                reb_n.set_enabled(e.value == "n")
                changed()
            ui.select({**{r: p(REBALANCE[r]) for r in REBALANCE}, "n": t("sp.every_n")}, label=t("sp.rebalance"),
                      value=SP["reb"], on_change=on_reb).props(DENSE).classes("w-40")
            reb_n = ui.number(t("sp.every_n_days"), value=SP["reb_n"], min=1, max=250, precision=0,
                              on_change=lambda e: set_sp("reb_n", e.value, int)).props("outlined dense").classes("w-36")
            reb_n.set_enabled(SP["reb"] == "n")
        if panel.kind == "cb":
            with ui.row().classes("w-full items-center gap-4"):
                for k, m in CB_FILTERS.items():
                    def on_f(e, k=k):
                        SP["cbf"][k] = float(e.value) if e.value not in (None, "") else None
                        changed()
                    ui.number(p(m["label"]), value=SP["cbf"].get(k), on_change=on_f) \
                        .props("outlined dense clearable").classes("w-60") \
                        .tooltip(p(m["help"]) + t("cb.empty_no_limit")).mark(f"cb_{k}")
        with ui.row().classes("w-full items-center gap-4"):
            if panel.kind == "stock":
                ui.switch(t("sp.exclude_st"), value=SP["excl_st"], on_change=lambda e: set_sp("excl_st", e.value))
            ui.number(t("sp.min_days"), value=SP["min_list"], min=0, max=2000, step=50, precision=0,
                      on_change=lambda e: set_sp("min_list", e.value, int)).props("outlined dense").classes("w-36") \
                .tooltip(t("sp.min_days_help"))
            with ui.column().classes("gap-0 w-64"):
                with ui.row().classes("w-full justify-between"):
                    ui.label(t("strat.position")).classes("text-xs sq-muted")
                    pos_lbl = ui.label(f"{SP['pos']}%").classes("sq-num text-xs text-primary")

                def on_pos(e):
                    pos_lbl.text = f"{int(e.value)}%"
                    set_sp("pos", e.value, int)
                ui.slider(min=10, max=100, step=5, value=SP["pos"], on_change=on_pos)
        if panel.kind == "cb":
            ui.label(t("cb.coupon_note")).classes("sq-muted text-sm")
        with ui.row().classes("w-full items-center gap-4") as div_row:
            with ui.column().classes("gap-0"):
                ui.label(t("sp.dividend")).classes("text-xs sq-muted").tooltip(t("sp.dividend_help"))
                div_radio = ui.radio({k: p(v) for k, v in DIVIDEND.items()}, value=SP.get("dividend", "reinvest"),
                                     on_change=lambda e: set_sp("dividend", e.value)) \
                    .props("inline dense").mark("sp_dividend")
                div_radio.tooltip(t("sp.dividend_help"))
            div_warn = ui.column()
        div_row.set_visibility(panel.kind == "stock")

    make_broker = broker_settings(broker_key(panel.kind), default_preset="bond" if panel.kind == "cb" else "stock",
                                  default_cash=1_000_000, show_t1=False)

    preview = ui.label().classes("sq-code w-full")
    with ui.row().classes("w-full items-end gap-3"):
        ui.input(t("strat.name"), value=SP["name"], placeholder=t("sp.name_ph"),
                        on_change=lambda e: set_sp("name", e.value or "")).props("outlined dense").classes("grow")
        save_btn = ui.button(t("strat.save"), icon="save", on_click=lambda: save()).props("outline no-caps")
        run_btn = ui.button(t("bt.run"), icon="play_arrow", on_click=lambda: do_run()).props("unelevated no-caps") \
            .mark("sp_run")
        with ui.button(t("exp.menu"), icon="ios_share").props("outline no-caps") as exp_btn:
            exp_menu = ui.menu()
        exp_menu.on("before-show", lambda: build_export_menu())
    running = ui.row().classes("items-center gap-2")
    with running:
        ui.spinner(size="sm")
        ui.label(t("sp.running")).classes("sq-muted text-sm")
    running.set_visibility(False)

    def set_sp(k, v, cast=None):
        if v is None:
            return
        SP[k] = cast(v) if cast else v
        changed()

    def changed():
        spec = current_spec(SP)
        preview.text = strategies.describe(spec, lg)
        has = bool(SP["factors"])
        save_btn.set_enabled(has and bool(SP["name"].strip()))
        run_btn.set_enabled(has)
        exp_btn.set_enabled(has)
        fin_warn.clear()
        if not panel.has_fin and any(FACTORS[f["key"]].get("requires_fin") for f in SP["factors"]):
            with fin_warn:
                notice(t("sp.need_fin"), "warning", "warning")
        div_warn.clear()
        if SP.get("dividend") == "cash" and not panel.div_codes and panel.kind == "stock":
            with div_warn:
                notice(t("sp.need_div"), "warning", "warning")

    def save():
        strategies.save_strategy(SP["name"].strip(), current_spec(SP))
        ui.notify(t("strat.saved", name=SP["name"].strip()), type="positive")

    async def do_run():
        spec, broker = current_spec(SP), make_broker()
        run_btn.disable()
        running.set_visibility(True)
        try:
            t0 = time.time()
            res = await run.io_bound(run_selection, panel, spec, broker, start=ctx.start)
            SP["result"] = (res, spec, time.time() - t0)
            SP["result_range"] = (ctx.start, ctx.end, ctx.panel_start)
            uni = p(UNIVERSES[spec["universe"]]["label"])
            title = SP["name"].strip() or t("sel.kind")
            history.add("sel", title, f"{uni} · {ctx.start} ~ {ctx.end}", res.metrics, data=uni, strategy=title)
        except Exception as ex:  # noqa: BLE001
            SP["result"] = None
            ui.notify(t("bt.failed", e=ex), type="negative", multi_line=True)
        finally:
            run_btn.enable()
            running.set_visibility(False)
        results.refresh()

    def build_export_menu():
        """每次打开菜单时按当前设置生成（刚跑完的回测名单才能写进文件）"""
        from simplequant.paths import PROJECT_DIR, FROZEN
        from simplequant.export import selection_script, selection_platform_script, encode_script, PLATFORMS
        spec, broker = current_spec(SP), make_broker()
        title = SP["name"].strip() or t("sel.kind")
        stem = export_filename(title)[:-3]
        res = SP.get("result")
        same = res and res[1] == spec and SP.get("result_range") == (ctx.start, ctx.end, ctx.panel_start)
        expected = res[0].schedule.picks if same and res[0].schedule else None
        exp_menu.clear()
        with exp_menu, ui.column().classes("gap-1 p-2 w-[380px]"):
            ui.button(t("exp.button"), icon="code", on_click=lambda: ui.download.content(
                selection_script(spec, asdict(broker), ctx.start, str(PROJECT_DIR), title=title, lang=lg,
                                 exe=sys.executable if FROZEN else None),
                f"{stem}.py", "text/x-python")).props("flat no-caps align=left").classes("w-full") \
                .tooltip(t("exp.help_selection"))
            ui.button(t("exp.run_script"), icon="play_circle", on_click=run_script_dialog) \
                .props("flat no-caps align=left").classes("w-full").tooltip(t("exp.run_script_help")) \
                .mark("sp_run_script")
            ui.separator()
            ui.label(t("exp.sel_platform_note")).classes("sq-muted text-xs px-2")
            if expected is None:
                ui.label(t("exp.sel_need_run")).classes("text-warning text-xs px-2")
            follow = ui.switch(t("exp.sel_follow"), value=SP["follow"] and expected is not None) \
                .tooltip(t("exp.sel_follow_help")).mark("sp_exp_follow")
            follow.set_enabled(expected is not None)
            follow.on_value_change(lambda e: SP.__setitem__("follow", e.value))
            for key, meta in PLATFORMS.items():
                def make(key=key):
                    code = selection_platform_script(key, spec, asdict(broker), ctx.start, ctx.end, ctx.panel_start,
                                                     expected=expected, follow=follow.value and expected is not None,
                                                     title=title, lang=lg)
                    return encode_script(code, key)
                try:
                    make()
                    why = None
                except ValueError as e:
                    why = str(e)
                b = ui.button(p(meta["label"]), icon="upload_file",
                              on_click=lambda make=make, key=key: ui.download.content(
                                  make(), f"{stem}_{key}.py", "text/x-python")) \
                    .props("flat no-caps align=left").classes("w-full").mark(f"sp_exp_{key}")
                b.tooltip(why or t(f"exp.help_{key}"))
                if why:
                    b.disable()
                    ui.label(why).classes("text-warning text-xs px-2")

    changed()

    @ui.refreshable
    def results():
        if SP.get("result"):
            _selection_results(SP, ctx)
    results()


def _selection_results(SP, ctx):
    lg = lang()
    panel = ctx.panel
    res, rspec, secs = SP["result"]
    with ui.element("div").classes("sq-sec"):
        ui.label(t("bt.results")).classes("sq-h2")
    with ui.column().classes("gap-1"):
        ui.label(strategies.describe(rspec, lg).replace("\n", " · ") + " · " + t("sp.took", s=f"{secs:.1f}")) \
            .classes("sq-muted text-sm")
    metric_tiles(res.metrics)
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
        metric_tile(t("m.turnover_annual"), f"{res.metrics['turnover_annual']:.1f}×", help_text=t("sp.turnover_help"))
        metric_tile(t("sp.n_rebalances"), str(len(res.schedule.picks)))
        if "dividend_cash" in res.metrics:
            metric_tile(t("m.dividend_cash"), f"{res.metrics['dividend_cash']:,.0f}", help_text=t("sp.dividend_help"))
            metric_tile(t("m.dividend_tax"), f"{res.metrics['dividend_tax']:,.0f}", help_text=t("sp.dividend_tax_help"))
    if res.div_missing:
        shown = "、".join(f"{res.names.get(c, c)}({c.split('.')[-1]})" for c in res.div_missing[:10])
        notice(t("sp.div_missing", n=len(res.div_missing), codes=shown + ("…" if len(res.div_missing) > 10 else "")),
               "info", "warning")
    if res.div_patched is not None:
        pt = res.div_patched
        cash, rights = pt[pt["cash"] > 0], pt[pt["cash"] <= 0]
        sep = "、" if lg == "zh" else ", "
        eg = sep.join(f"{res.names.get(r.code, r.code)} {r.ex_date:%Y-%m-%d} {r.cash:g}" for r in cash.head(5).itertuples())
        notice(t("sp.div_patched", n=len(cash), m=len(rights), eg=eg + ("…" if len(cash) > 5 else "")), "info", "info")
    bench_label = t("cb.bench_label") if U.kind(rspec["universe"]) == "cb" else \
        t("sp.bench_label", name=p(UNIVERSES[rspec["universe"]]["label"]))
    with ui.card().classes("w-full p-2"):
        plot(equity_chart(res.equity, lg, bench_label))

    with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
        tab_h = ui.tab("h", t("sp.tab_holdings"))
        tab_o = ui.tab("o", t("bt.tab_orders"))
        tab_l = ui.tab("l", t("bt.tab_logs"))
    with ui.tab_panels(tabs, value=tab_h).classes("w-full bg-transparent"):
        with ui.tab_panel(tab_h).classes("px-0"):
            def industry_mix(codes):
                counts = panel.industry.reindex(codes).fillna("—").map(
                    lambda s: s[3:] if s[:1].isalpha() and s[1:3].isdigit() else s).value_counts()
                return "、".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items())
            rows = [{t("col.time"): d.date(),
                     t("sp.picked"): "、".join(f"{res.names.get(c, c)}({c.split('.')[-1]})" for c in codes),
                     t("sp.industry_mix"): industry_mix(codes)}
                    for d, codes in sorted(res.schedule.picks.items(), reverse=True)]
            df_table(pd.DataFrame(rows), rows_per_page=20).props("wrap-cells")
            if res.schedule.skipped:
                ui.label(t("sp.skipped", n=len(res.schedule.skipped))).classes("sq-muted text-xs")
        with ui.tab_panel(tab_o).classes("px-0"):
            if not res.orders.empty:
                o = res.orders.copy()
                o["symbol"] = o["symbol"].map(lambda c: f"{res.names.get(c, c)}({c.split('.')[-1]})")
                df_table(fmt_table(orders_table(o, lg), {t("col.price"): "{:.2f}", t("col.value"): "{:,.0f}",
                                                         t("col.commission"): "{:.2f}"}), rows_per_page=30)
        with ui.tab_panel(tab_l).classes("px-0"):
            df_table(logs_table(res.logs, lg), rows_per_page=50)


# ================= 运行导出的选股脚本 =================
SCRIPT_MARKS = ("from simplequant.stocks import StockStore, run_selection, universe",
                "from simplequant.stocks import StockStore, UNIVERSES, build_panel, run_selection")   # 旧版导出的


def run_script_dialog():
    """选择导出的选股脚本 → 复制到数据目录下的 scripts/<名字>/ → 子进程运行，实时显示日志"""
    from simplequant.export.run_script import command, log_path
    from simplequant.paths import DATA_ROOT

    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full gap-3"):
        ui.label(t("exp.run_title")).classes("sq-h2")
        ui.label(t("exp.run_script_help")).classes("sq-muted text-sm")
        status = ui.column().classes("w-full")
        logbox = ui.label("").classes("sq-code w-full text-xs whitespace-pre-wrap max-h-80 overflow-auto")
        logbox.set_visibility(False)

        async def on_upload(e):
            name = Path(e.file.name).name
            data = await e.file.read()
            text = data.decode("utf-8", errors="replace")
            status.clear()
            if not any(m in text for m in SCRIPT_MARKS):
                with status:
                    notice(t("exp.run_bad"), "error", "error")
                return
            folder = DATA_ROOT / "scripts" / Path(name).stem
            folder.mkdir(parents=True, exist_ok=True)
            script = folder / name
            script.write_bytes(data)
            log = log_path(script)
            log.unlink(missing_ok=True)
            up.set_visibility(False)
            with status:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm")
                    ui.label(t("exp.run_running", name=name)).classes("text-sm")
            logbox.set_visibility(True)

            def tail():
                if log.exists():
                    logbox.text = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
            timer = ui.timer(0.5, tail)
            try:
                code = await run.io_bound(lambda: subprocess.run(
                    command(script), cwd=str(folder), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode)
            finally:
                timer.cancel()
                tail()
                up.set_visibility(True)
                up.reset()
            status.clear()
            with status:
                if code == 0:
                    with ui.row().classes("w-full items-center gap-3 no-wrap"):
                        notice(t("exp.run_done") + f" `{folder}`", "check_circle")
                        ui.button(t("exp.open_folder"), icon="folder_open",
                                  on_click=lambda: os.startfile(folder)).props("outline no-caps")  # noqa: S606
                else:
                    notice(t("exp.run_failed"), "error", "error")

        up = ui.upload(label=t("exp.run_pick"), auto_upload=True, on_upload=on_upload) \
            .props('accept=.py flat bordered').classes("w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button(t("gui.close"), on_click=dlg.close) \
                .props("flat no-caps")
    dlg.open()


# ================= 滚动优化 =================
def tuned_spec(base: dict, row: pd.Series, paths: list, is_int: dict) -> dict:
    """把某个窗口选出的参数写回策略描述"""
    out = base
    for path in paths:
        v = row[path]
        if path == "rebalance":
            v = v if isinstance(v, str) else int(v)
        else:
            v = int(round(float(v))) if is_int.get(path) else float(v)
        out = strategies.set_path(out, path, v)
    return out


def _walkforward(SP, ctx):
    lg = lang()
    panel, W = ctx.panel, SP["wf_cfg"]
    ui.label(t("spwf.intro")).classes("sq-muted text-sm")
    spec = current_spec(SP)
    if not spec["factors"]:
        notice(t("spwf.need_factors"), "info")
        return
    ui.label(t("spwf.using")).classes("font-semibold")
    ui.label(strategies.describe(spec, lg)).classes("sq-code w-full")

    tbs = {tb.path: tb for tb in selection_tunables(spec, lg)}
    labels_all = {"rebalance": t("sp.rebalance"), **{k: tb.label for k, tb in tbs.items()}}
    spec_key = str(list(labels_all.values()))          # 因子或加权方式变化时重置选择
    if W.get("spec_key") != spec_key:
        W.update(spec_key=spec_key, chosen=[k for k in ("top_n", "rebalance") if k in labels_all], ranges={},
                 reb=None)
    cur_reb = spec["rebalance"]
    if W["reb"] is None:
        W["reb"] = list(dict.fromkeys([cur_reb, "weekly", "monthly"]))

    def axes():
        ax, is_int = {}, {}
        for path in W["chosen"]:
            if path == "rebalance":
                ax[path] = list(W["reb"])
                continue
            tb = tbs[path]
            a, b, s = W["ranges"].setdefault(path, default_range(tb, 4))
            ax[path], is_int[path] = value_range(a, b, s, tb.is_int), tb.is_int
        return ax, is_int

    with section(t("opt.params"), "grid_on"):
        def on_chosen(e):
            W["chosen"] = list(e.value or [])[:3]
            rows.refresh()
            update()
        ui.select(labels_all, label=t("opt.pick_params"), value=W["chosen"], multiple=True, on_change=on_chosen) \
            .props(DENSE + " use-chips").classes("w-full").mark("spwf_params")

        @ui.refreshable
        def rows():
            for path in W["chosen"]:
                if path == "rebalance":
                    def on_reb(e):
                        W["reb"] = list(e.value or [])
                        update()
                    choices = list(dict.fromkeys(REBALANCE_CHOICES + [cur_reb]))
                    ui.select({r: reb_label(r) for r in choices}, label=labels_all[path], value=W["reb"],
                              multiple=True, on_change=on_reb).props(DENSE + " use-chips").classes("w-full") \
                        .mark("spwf_reb")
                    continue
                tb = tbs[path]
                a, b, s = W["ranges"].setdefault(path, default_range(tb, 4))
                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    with ui.column().classes("gap-0 grow"):
                        ui.label(tb.label).classes("font-medium")
                        ui.label(t("opt.current", v=f"{tb.value:g}")).classes("sq-muted text-xs")
                    kw = dict(precision=0) if tb.is_int else dict(format="%.4g")
                    for i, (lab, v) in enumerate(((t("opt.from"), a), (t("opt.to"), b), (t("opt.step"), s))):
                        def on_v(e, path=path, i=i):
                            if e.value is not None:
                                r = list(W["ranges"][path])
                                r[i] = e.value
                                W["ranges"][path] = tuple(r)
                                update()
                        lo = (1 if tb.is_int else 0.0001) if i == 2 else (tb.min if tb.is_int else 0.0)
                        ui.number(lab, value=v, min=lo, max=tb.max if tb.is_int and i < 2 else None,
                                  on_change=on_v, **kw).props("outlined dense").classes("w-28") \
                            .mark(f"spwf:{path}:{i}")
        rows()
        reb_err = ui.label(t("spwf.need_reb")).classes("text-negative text-sm")
        with ui.row().classes("w-full items-center gap-4"):
            def set_w(k, cast=None):
                def f(e):
                    if e.value is not None:
                        W[k] = cast(e.value) if cast else e.value
                        update()
                return f
            ui.select({m: t(f"m.{m}") for m in WF_TARGETS}, label=t("opt.target"), value=W["metric"],
                      on_change=set_w("metric")).props(DENSE).classes("w-44")
            ui.number(t("opt.workers"), value=W["workers"], min=1, max=32, precision=0,
                      on_change=set_w("workers", int)).props("outlined dense").classes("w-36") \
                .tooltip(t("opt.workers_help")).mark("spwf_workers")

    with section(t("wf.settings"), "timeline"):
        with ui.row().classes("w-full items-start gap-4"):
            ui.number(t("wf.train"), value=W["train"], min=3, max=120, step=3, precision=0,
                      on_change=set_w("train", int)).props("outlined dense").classes("w-36") \
                .tooltip(t("wf.train_help")).mark("spwf_train")
            ui.number(t("wf.test"), value=W["test"], min=1, max=36, step=1, precision=0,
                      on_change=set_w("test", int)).props("outlined dense").classes("w-36") \
                .tooltip(t("wf.test_help")).mark("spwf_test")
            with ui.column().classes("gap-0"):
                ui.label(t("wf.window")).classes("text-xs sq-muted").tooltip(t("wf.window_help"))
                ui.radio({w: t(f"wf.window_{w}") for w in ("rolling", "anchored")}, value=W["window"],
                         on_change=set_w("window")).props("inline dense")
        est = ui.label().classes("text-sm")

    run_btn = ui.button(t("wf.run"), icon="play_arrow", on_click=lambda: do_wf()).props("unelevated no-caps") \
        .classes("self-start").mark("spwf_run")
    prog = Progress()

    def update():
        ax, _ = axes()
        reb_err.set_visibility("rebalance" in ax and not ax["rebalance"])
        n_combos = len(selection_grid(spec, ax)) if ax and all(ax.values()) else 0
        cal = panel.calendar[panel.calendar >= pd.Timestamp(ctx.start)]
        n_windows = len(make_windows(cal, int(W["train"]), int(W["test"]), W["window"] == "anchored")) if len(cal) else 0
        if n_windows == 0:
            est.text, color = t("wf.no_windows"), "text-negative"
        elif n_combos > WF_MAX:
            est.text, color = t("opt.too_many", n=n_combos, max=WF_MAX), "text-negative"
        elif n_combos:
            est.text, color = t("spwf.estimate", w=n_windows, n=n_combos), "sq-muted"
        else:
            est.text, color = "", "sq-muted"
        est.classes(replace=f"text-sm {color}")
        run_btn.set_enabled(n_windows > 0 and 0 < n_combos <= WF_MAX)

    async def do_wf():
        ax, is_int = axes()
        broker = _sp_broker(ctx.panel.kind)
        n_combos = len(selection_grid(spec, ax))
        run_btn.disable()
        prog.start(t("spwf.progress_select", d=0, n=n_combos))

        def progress(phase, d, n):
            if phase == "select":
                prog.set(0.1 * d / n, t("spwf.progress_select", d=d, n=n))
            else:
                prog.set(0.1 + 0.9 * d / n, t("spwf.progress_bt", d=d, n=n))
        try:
            t0 = time.time()
            res = await run.io_bound(walk_forward_selection, panel, spec, ax, broker, W["metric"], int(W["train"]),
                                     int(W["test"]), W["window"] == "anchored", ctx.start, int(W["workers"]), progress)
            SP["wf"] = dict(res=res, labels={k: labels_all[k] for k in ax}, is_int=is_int, metric=W["metric"],
                            spec=spec, range=(ctx.start, ctx.end), secs=time.time() - t0)
        except Exception as ex:  # noqa: BLE001
            SP["wf"] = None
            ui.notify(t("bt.failed", e=ex), type="negative", multi_line=True)
        finally:
            prog.stop()
            run_btn.enable()
        wf_results.refresh()

    @ui.refreshable
    def wf_results():
        wf = SP.get("wf")
        if not wf:
            return
        if wf["spec"] != current_spec(SP) or wf["range"] != (ctx.start, ctx.end):
            notice(t("spwf.stale"), "history", "info")
        res, labels = wf["res"], wf["labels"]

        def fmt(c, v):
            return reb_label(v) if c == "rebalance" else v

        walkforward_results(res, labels, wf["metric"], t("wf.stitch_continuous"),
                            t("spwf.note") + " " + t("sp.took", s=f"{wf['secs']:.0f}"), fmt_param=fmt)
        last = res.windows.iloc[-1]
        if all(pd.notna(last[k]) for k in labels):
            latest = tuned_spec(wf["spec"], last, list(labels), wf["is_int"])
            ui.label(t("spwf.latest")).classes("font-semibold")
            ui.label(t("spwf.latest_note")).classes("sq-muted text-xs")
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                ui.label(strategies.describe(latest, lg)).classes("sq-code grow")

                def use_tuned():
                    apply_spec(SP, latest, SP.get("name", ""))
                    ui.notify(t("spwf.applied"), type="positive")
                    ctx.rebuild_bt()
                    ctx.reload_wf()
                ui.button(t("spwf.apply"), icon="input", on_click=use_tuned).props("outline no-caps") \
                    .mark("spwf_apply")
        with ui.expansion(t("spwf.full")).classes("w-full q-card"):
            show = ["total_return", "cagr", "sharpe", "max_drawdown", "turnover_annual"]
            g = res.grid
            g = g[g["sharpe"].notna()] if "sharpe" in g else g.iloc[0:0]
            top = g.sort_values(wf["metric"], ascending=False)[list(labels) + show].copy()
            for c in labels:
                top[c] = top[c].map(lambda v, c=c: fmt(c, v))
            for m_ in show[:-1]:
                top[m_] = top[m_].map(lambda v, m_=m_: fmt_metric(m_, v))
            top["turnover_annual"] = top["turnover_annual"].map(lambda v: f"{v:.1f}×")
            df_table(top.rename(columns={**labels, **{m_: t(f"m.{m_}") for m_ in show}}))

    update()
    wf_results()


def broker_key(kind: str) -> str:
    """股票与可转债的资金和费率分开记（可转债默认用债券费率）"""
    return "sp_cb" if kind == "cb" else "sp"


def _sp_broker(kind: str = "stock"):
    """滚动优化用选股回测标签页里的资金与费率设置"""
    from simplequant.engine import BrokerConfig, COST_PRESETS
    b = state.STATE.get(f"{broker_key(kind)}_broker") or \
        {"cash": 1_000_000, "preset": "bond" if kind == "cb" else "stock", "slippage": 5.0, "custom": {}}
    c = COST_PRESETS[b["preset"]]
    mine = b.get("custom", {}).get(b["preset"], {})
    return BrokerConfig(cash=float(b["cash"]), commission=mine.get("commission", c["commission"] * 1e4) / 1e4,
                        min_commission=float(mine.get("min_commission", c["min_commission"])),
                        stamp_duty=mine.get("stamp_duty", c["stamp_duty"] * 1e4) / 1e4,
                        slippage=b.get("slippage", 5.0) / 1e4, t_plus_1=True)
