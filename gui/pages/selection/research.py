"""选股页「因子研究」标签页"""

import pandas as pd
from nicegui import run, ui

from simplequant.stocks import FACTORS, analyze, compute, factor_zscores, factor_correlation, sample_dates
from simplequant.stocks import factors_for
from gui.common import t, p, lang
from gui.missing_data import stock_bar
from gui.components import metric_tile
from gui.widgets import df_table, fmt_table, plot, notice, Progress
from ui.charts import ic_chart, quantile_chart, group_bar, corr_heatmap
from ui.shared import pct, num
from .common import DENSE, factor_label


# ================= 因子研究 =================
def _research(SP, ctx):
    lg = lang()
    panel, R = ctx.panel, SP["res"]
    kinds = factors_for(panel.kind)
    if R["fkey"] not in kinds:
        R["fkey"] = kinds[0]
    usable = [k for k in kinds if (panel.has_fin or not FACTORS[k].get("requires_fin"))
              and (panel.has_risk or not FACTORS[k].get("requires_risk"))]

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
        shown = {}

        def hints():
            fsel.tooltip(p(FACTORS[R["fkey"]].get("desc", "")) or "")
            f = FACTORS[R["fkey"]]
            parts = [k for k, need in (("fin", f.get("requires_fin") and not panel.has_fin),
                                       ("div", f.get("requires_div") and not panel.div_codes),
                                       ("risk", f.get("requires_risk") and not panel.has_risk)) if need]
            if parts == shown.get("parts"):      # 没变：不重画（下载中的提示条不被冲掉）
                return
            shown["parts"] = parts
            hint_box.clear()
            with hint_box:
                if panel.kind == "stock" and ctx.lo is not None and ctx.reload_all:
                    for part in parts:           # 就地下载（gui/missing_data.py）
                        stock_bar(part, SP["universe"], ctx.lo.year - 1, "factor", on_done=ctx.reload_all)
                    return
                for part in parts:
                    notice(t({"fin": "sp.need_fin", "div": "sp.need_div_factor", "risk": "sp.need_risk"}[part]),
                           "warning", "warning")
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
            if ctx.to_backtest:          # 因子实验室：回到选股页用这个因子回测
                ui.button(t("lab.use_factor"), icon="query_stats", on_click=lambda: ctx.to_backtest(R["fkey"])) \
                    .props("flat no-caps color=primary").tooltip(t("lab.use_factor_help")).mark("lab_use_factor")

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
