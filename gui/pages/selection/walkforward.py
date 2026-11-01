"""选股页「滚动优化」标签页"""

import time

import pandas as pd
from nicegui import run, ui

from simplequant import strategies
from simplequant.engine.optimize import value_range
from simplequant.engine.walkforward import make_windows
from simplequant.stocks.walkforward import (walk_forward_selection, selection_tunables, selection_grid,
                                            REBALANCE_CHOICES, TARGET_METRICS as WF_TARGETS, MAX_COMBOS as WF_MAX)
from gui import state
from gui.common import t, lang
from gui.components import walkforward_results
from gui.widgets import section, df_table, notice, Progress
from ui.shared import default_range, fmt_metric
from .common import DENSE, reb_label, spec_panel
from .backtest import apply_spec, current_spec
from .broker import broker_key, _sp_broker


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
    if (state.STATE.get(f"{broker_key(ctx.panel.kind)}_broker") or {}).get("contribution"):
        notice(t("spwf.no_contribution"), "info", "info")

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
            wf_panel, _ = await spec_panel(SP, ctx, spec)
            res = await run.io_bound(walk_forward_selection, wf_panel, spec, ax, broker, W["metric"], int(W["train"]),
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
