"""第 4 页：参数优化（网格搜索 / 滚动优化）"""

import pandas as pd
from nicegui import run, ui

from simplequant import strategies
from simplequant.strategies.code_strategy import explain_error
from simplequant.engine import run_backtest
from simplequant.engine.optimize import (optimize, value_range, build_grid, split_prices, best_row, apply_combo,
                                         TARGET_METRICS, MAX_COMBOS, default_workers)
from simplequant.engine.walkforward import (walk_forward, make_windows, count_backtests, STITCH_MODES,
                                            MAX_BACKTESTS as WF_MAX)
from gui import state
from gui.common import t, lang
from gui.components import (require_data, data_selector, strategy_selector, broker_settings, load_prices,
                            prepare_prices, metric_tile, walkforward_results, dividend_notices,
                            unadjusted_warning)
from gui.layout import frame, page_title
from gui.widgets import section, df_table, plot, notice, Progress
from ui.charts import heatmap, param_curve
from ui.shared import default_range, fmt_metric

DENSE = "outlined dense options-dense"


def page():
    with frame("/optimize"):
        page_title("/optimize", t("opt.intro"))
        by_id = require_data()
        if by_id is None:
            return
        O = state.STATE.setdefault("opt", {"mode": "grid", "chosen": {}, "ranges": {}, "metric": TARGET_METRICS[0],
                                           "oos": 30, "workers": default_workers(), "train": 24, "test": 6,
                                           "window": "rolling", "stitch": next(iter(STITCH_MODES))})
        lg = lang()

        pick = data_selector(by_id, "opt", lambda: update())
        strat = strategy_selector("opt", lambda: (params_box.refresh(), update()))
        make_broker = broker_settings("opt", show_dividend=True)

        def set_mode(e):
            O["mode"] = e.value
            mode_views()
            update()
        with ui.row().classes("items-center gap-2"):
            ui.toggle({m: t(f"wf.mode_{m}") for m in ("grid", "wf")}, value=O["mode"], on_change=set_mode) \
                .props("no-caps unelevated rounded toggle-color=primary").mark("opt_mode")
            ui.icon("help_outline").classes("sq-muted").tooltip(t("wf.mode_help"))

        # ---------------- 参数范围 ----------------
        cur = {}          # 当前的 axes / is_int / labels，由 params_box 填写

        def tunables():
            """当前策略的可调参数 → (tbs, spec_key, 已选参数, 各参数范围)；不依赖界面是否已重画"""
            tbs = {tb.path: tb for tb in strategies.tunables(strat["spec"], lg)}
            spec_key = str(sorted(tbs))
            chosen = O["chosen"].setdefault(spec_key, list(tbs)[:2])
            ranges = O["ranges"].setdefault(spec_key, {})
            for path in chosen:
                ranges.setdefault(path, default_range(tbs[path]))
            return tbs, spec_key, chosen, ranges

        @ui.refreshable
        def params_box():
            tbs, spec_key, chosen, ranges = tunables()

            with section(t("opt.params"), "grid_on"):
                def on_chosen(e):
                    v = list(e.value or [])[:3]
                    O["chosen"][spec_key] = v
                    tunables()
                    rows.refresh()
                    update()
                ui.select({k: tb.label for k, tb in tbs.items()}, label=t("opt.pick_params"), value=chosen,
                          multiple=True, on_change=on_chosen).props(DENSE + " use-chips max-values=3") \
                    .classes("w-full").mark("opt_params")

                @ui.refreshable
                def rows():
                    for path in O["chosen"][spec_key]:
                        tb = tbs[path]
                        a, b, s = ranges[path]
                        with ui.row().classes("w-full items-center gap-3 no-wrap"):
                            with ui.column().classes("gap-0 grow"):
                                ui.label(tb.label).classes("font-medium")
                                ui.label(t("opt.current", v=f"{tb.value:g}")).classes("sq-muted text-xs")
                            kw = dict(precision=0) if tb.is_int else dict(format="%.4g")
                            for i, (lab, v) in enumerate(((t("opt.from"), a), (t("opt.to"), b), (t("opt.step"), s))):
                                def on_v(e, path=path, i=i):
                                    if e.value is not None:
                                        r = list(ranges[path])
                                        r[i] = e.value
                                        ranges[path] = tuple(r)
                                        update()
                                ui.number(lab, value=v, min=(1 if tb.is_int else 0.0001) if i == 2 else None,
                                          on_change=on_v, **kw).props("outlined dense").classes("w-28") \
                                    .mark(f"rng:{path}:{i}")
                rows()

                with ui.row().classes("w-full items-center gap-4"):
                    def set_o(k):
                        def f(e):
                            if e.value is not None:
                                O[k] = e.value
                                update()
                        return f
                    ui.select({m: t(f"m.{m}") for m in TARGET_METRICS}, label=t("opt.target"), value=O["metric"],
                              on_change=set_o("metric")).props(DENSE).classes("w-44")
                    with ui.column().classes("gap-0 w-56") as oos_box:
                        ui.label(t("opt.oos")).classes("text-xs sq-muted").tooltip(t("opt.oos_help"))
                        ui.slider(min=0, max=50, step=5, value=O["oos"], on_change=set_o("oos")).props("label")
                    oos_box.set_visibility(O["mode"] == "grid")
                    cur["oos_box"] = oos_box
                    ui.number(t("opt.workers"), value=O["workers"], min=1, max=32, precision=0,
                              on_change=set_o("workers")).props("outlined dense").classes("w-36") \
                        .tooltip(t("opt.workers_help"))
        params_box()
        combos = ui.label().classes("text-sm")

        def axes():
            tbs, _, chosen, ranges = tunables()
            ax, is_int = {}, {}
            for path in chosen:
                tb = tbs[path]
                a, b, s = ranges[path]
                ax[path], is_int[path] = value_range(a, b, s, tb.is_int), tb.is_int
            return ax, is_int, {p_: tbs[p_].label for p_ in ax}

        # ---------------- 滚动优化设置 ----------------
        with section(t("wf.settings"), "timeline") as wf_box:
            with ui.row().classes("w-full items-start gap-4"):
                def set_w(k):
                    def f(e):
                        if e.value is not None:
                            O[k] = e.value
                            update()
                    return f
                ui.number(t("wf.train"), value=O["train"], min=3, max=120, step=3, precision=0,
                          on_change=set_w("train")).props("outlined dense").classes("w-36").tooltip(t("wf.train_help"))
                ui.number(t("wf.test"), value=O["test"], min=1, max=36, step=1, precision=0,
                          on_change=set_w("test")).props("outlined dense").classes("w-36").tooltip(t("wf.test_help"))
                with ui.column().classes("gap-0"):
                    ui.label(t("wf.window")).classes("text-xs sq-muted").tooltip(t("wf.window_help"))
                    ui.radio({w: t(f"wf.window_{w}") for w in ("rolling", "anchored")}, value=O["window"],
                             on_change=set_w("window")).props("inline dense")
                with ui.column().classes("gap-0"):
                    ui.label(t("wf.stitch")).classes("text-xs sq-muted").tooltip(t("wf.stitch_help"))
                    ui.radio({s: t(f"wf.stitch_{s}") for s in STITCH_MODES}, value=O["stitch"],
                             on_change=set_w("stitch")).props("inline dense")
            wf_msg = ui.label().classes("text-sm")

        warn = ui.column().classes("w-full")
        with ui.row().classes("items-center gap-3"):
            run_btn = ui.button(t("opt.run"), icon="play_arrow", on_click=lambda: do_run()) \
                .props("unelevated no-caps").mark("opt_run")
            wf_btn = ui.button(t("wf.run"), icon="play_arrow", on_click=lambda: do_wf()) \
                .props("unelevated no-caps").mark("wf_run")
        prog = Progress()

        def mode_views():
            grid = O["mode"] == "grid"
            wf_box.set_visibility(not grid)
            run_btn.set_visibility(grid)
            wf_btn.set_visibility(not grid)
            if "oos_box" in cur and not cur["oos_box"].is_deleted:
                cur["oos_box"].set_visibility(grid)
            results.refresh()

        def base_ready(n_combos):
            spec = strat["spec"]
            need = strategies.min_assets(spec)
            return (bool(pick.ids) and len(pick.freqs) == 1 and pick.date_range is not None
                    and len(pick.ids) >= need and 0 < n_combos <= MAX_COMBOS)

        def update():
            spec = strat["spec"]
            ax, _, _ = axes()
            n_combos = len(build_grid(spec, ax)) if ax else 0
            if n_combos > MAX_COMBOS:
                combos.text = t("opt.too_many", n=n_combos, max=MAX_COMBOS)
                combos.classes(replace="text-sm text-negative")
            else:
                combos.text = t("opt.combos", n=n_combos) if O["mode"] == "grid" else ""
                combos.classes(replace="text-sm sq-muted")
            warn.clear()
            need = strategies.min_assets(spec)
            with warn:
                if pick.ids and len(pick.ids) < need:
                    notice(t("bt.need_assets", n=need), "warning", "warning")
                if w := unadjusted_warning(by_id, pick.ids):
                    notice(w, "warning", "warning")
            ok = base_ready(n_combos)
            run_btn.set_enabled(ok)
            # 滚动优化：估算窗口数和回测次数
            wf_ok = False
            wf_msg.text = ""
            if ok and O["mode"] == "wf":
                n_windows = len(make_windows(_common_index(by_id, pick), int(O["train"]), int(O["test"]),
                                             O["window"] == "anchored"))
                total = count_backtests(spec, ax, n_windows)
                if n_windows == 0:
                    wf_msg.text, color = t("wf.no_windows"), "text-negative"
                elif total > WF_MAX:
                    wf_msg.text, color = t("wf.too_many", n=total, max=WF_MAX), "text-negative"
                else:
                    wf_msg.text, color, wf_ok = t("wf.estimate", w=n_windows, n=total), "sq-muted", True
                wf_msg.classes(replace=f"text-sm {color}")
            wf_btn.set_enabled(wf_ok)

        async def do_run():
            spec, broker = strat["spec"], make_broker()
            ax, is_int, labels = axes()
            oos = O["oos"] / 100
            n = len(build_grid(spec, ax))
            run_btn.disable()
            prog.start(t("opt.progress", done=0, n=n))

            def work():
                prices, skipped, patched = prepare_prices(by_id, pick, broker)
                ins, outs, cut = split_prices(prices, oos) if oos > 0 else (prices, None, None)
                df = optimize(ins, spec, ax, broker, workers=int(O["workers"]),
                              progress=lambda d, n_: prog.set(d / n_, t("opt.progress", done=d, n=n_)))
                best = best_row(df, O["metric"])
                best_spec = apply_combo(spec, best, is_int)
                compare = {"is_best": run_backtest(ins, *strategies.resolve(best_spec), broker).metrics}
                if outs is not None:
                    compare["oos_best"] = run_backtest(outs, *strategies.resolve(best_spec), broker).metrics
                    compare["oos_orig"] = run_backtest(outs, *strategies.resolve(spec), broker).metrics
                return dict(df=df, axes=list(ax), is_int=is_int, labels=labels, metric=O["metric"], spec=spec,
                            spec_label=strat["label"], best_spec=best_spec, compare=compare, cut=cut,
                            start=min(d.index[0] for d in prices.values()),
                            end=max(d.index[-1] for d in prices.values()), div_notes=(skipped, patched))
            try:
                O["result"] = await run.io_bound(work)
            except Exception as e:  # noqa: BLE001
                O.pop("result", None)
                ui.notify(t("bt.failed", e=explain_error(e, lang())), type="negative", multi_line=True)
            finally:
                prog.stop()
                run_btn.enable()
            results.refresh()

        async def do_wf():
            spec, broker = strat["spec"], make_broker()
            ax, is_int, labels = axes()
            wf_btn.disable()
            prog.start(t("wf.progress", w=0, n="…"))

            def progress(w, n, d, m):
                prog.set(((w - 1) + d / m) / n, t("wf.progress", w=w, n=n))
            try:
                prices, skipped, patched = await run.io_bound(prepare_prices, by_id, pick, broker)
                res = await run.io_bound(walk_forward, prices, spec, ax, is_int, broker, O["metric"], int(O["train"]),
                                         int(O["test"]), O["window"] == "anchored", int(O["workers"]), progress,
                                         O["stitch"])
                O["wf"] = (res, labels, O["metric"], O["stitch"])
                O["wf_div_notes"] = (skipped, patched)
            except Exception as e:  # noqa: BLE001
                O.pop("wf", None)
                ui.notify(t("bt.failed", e=explain_error(e, lang())), type="negative", multi_line=True)
            finally:
                prog.stop()
                wf_btn.enable()
            results.refresh()

        @ui.refreshable
        def results():
            if O["mode"] == "wf":
                if O.get("wf"):
                    res, labels, metric, stitch = O["wf"]
                    dividend_notices(*O.get("wf_div_notes", ({}, {})))
                    walkforward_results(res, labels, metric, t(f"wf.stitch_{stitch}"), t(f"wf.note_{stitch}"))
            elif O.get("result"):
                _grid_results(O["result"])

        results()
        mode_views()
        update()


def _common_index(by_id, pick) -> pd.DatetimeIndex:
    prices = load_prices(by_id, pick)
    return pd.DatetimeIndex(sorted(set.intersection(*[set(d.index) for d in prices.values()])))


def _grid_results(res: dict):
    lg = lang()
    df, paths, labels, metric = res["df"], res["axes"], res["labels"], res["metric"]
    ok = df[df[metric].notna()]
    best = best_row(df, metric)

    ui.separator().classes("my-2")
    dividend_notices(*res.get("div_notes", ({}, {})))
    with ui.column().classes("gap-1"):
        ui.label(t("opt.results")).classes("text-xl font-semibold")
        if res["cut"] is not None:
            ui.label(t("opt.split_caption", start=f"{res['start']:%Y-%m-%d}", cut=f"{res['cut']:%Y-%m-%d}",
                       end=f"{res['end']:%Y-%m-%d}")).classes("sq-muted text-sm")
    failed = int(df["error"].notna().sum()) if "error" in df else 0
    if failed:
        notice(t("opt.failed_combos", n=failed), "warning", "warning")

    with ui.grid(columns=len(paths) + 1).classes("w-full gap-3"):
        for p_ in paths:
            metric_tile(labels[p_], f"{best[p_]:g}")
        metric_tile(t(f"m.{metric}") + " · " + t("opt.in_sample"), fmt_metric(metric, best[metric]))

    cmp = res["compare"]
    if "oos_best" in cmp:
        rows = ["total_return", "cagr", "sharpe", "max_drawdown", "win_rate", "trades"]
        table = pd.DataFrame({t(f"opt.col_{k}"): [fmt_metric(r, cmp[k][r]) for r in rows] for k in cmp})
        table.insert(0, "", [t(f"m.{r}") for r in rows])
        ui.label(t("opt.robustness")).classes("font-semibold")
        df_table(table)
        is_v, oos_v = cmp["is_best"]["sharpe"], cmp["oos_best"]["sharpe"]
        if is_v > 0 and not oos_v > is_v * 0.5:
            notice(t("opt.overfit"), "warning", "warning")
        else:
            notice(t("opt.robust_ok"), "check_circle", "info")

    ui.label(t("opt.landscape")).classes("font-semibold")
    with ui.card().classes("w-full p-3 gap-2"):
        if len(paths) == 1:
            plot(param_curve(ok, paths[0], metric, labels[paths[0]], lg))
        else:
            view = {"x": paths[0], "y": paths[1], "fix": {o: best[o] for o in paths[2:]}}
            ctl = ui.row().classes("w-full items-center gap-3")
            chart = ui.column().classes("w-full")

            def draw():
                chart.clear()
                sl = ok
                for other in [p_ for p_ in paths if p_ not in (view["x"], view["y"])]:
                    sl = sl[sl[other] == view["fix"].get(other, best[other])]
                pivot = sl.pivot_table(index=view["y"], columns=view["x"], values=metric, aggfunc="first") \
                    .sort_index(ascending=False)
                with chart:
                    plot(heatmap(pivot, metric, labels[view["x"]], labels[view["y"]], lg))

            def controls():
                ctl.clear()
                with ctl:
                    def set_axis(k):
                        def f(e):
                            view[k] = e.value
                            if view["x"] == view["y"]:
                                view["y" if k == "x" else "x"] = next(p_ for p_ in paths if p_ != e.value)
                            controls()
                            draw()
                        return f
                    ui.select({p_: labels[p_] for p_ in paths}, label=t("opt.x_axis"), value=view["x"],
                              on_change=set_axis("x")).props(DENSE).classes("w-48")
                    ui.select({p_: labels[p_] for p_ in paths if p_ != view["x"]}, label=t("opt.y_axis"),
                              value=view["y"], on_change=set_axis("y")).props(DENSE).classes("w-48")
                    for other in [p_ for p_ in paths if p_ not in (view["x"], view["y"])]:
                        vals = sorted(ok[other].unique())

                        def on_fix(e, other=other):
                            view["fix"][other] = e.value
                            draw()
                        ui.select({v: f"{v:g}" for v in vals}, label=t("opt.fix", name=labels[other]),
                                  value=view["fix"].get(other, best[other]), on_change=on_fix) \
                            .props(DENSE).classes("w-48")
            controls()
            draw()
            ui.label(t("opt.heatmap_hint")).classes("sq-muted text-xs")

    show = ["total_return", "cagr", "sharpe", "max_drawdown", "win_rate", "trades"]
    top = ok.sort_values(metric, ascending=False).head(20)[paths + show].copy()
    for m_ in show:
        top[m_] = top[m_].map(lambda v, m_=m_: fmt_metric(m_, v))
    with ui.expansion(t("opt.top", n=min(20, len(ok))), icon="leaderboard").classes("w-full q-card"):
        df_table(top.rename(columns={**labels, **{m_: t(f"m.{m_}") for m_ in show}}))

    with ui.row().classes("w-full items-end gap-3"):
        name = ui.input(t("opt.save_name"),
                        value=f"{res['spec_label'].split('：')[-1].split(': ')[-1]} ({t('opt.tuned')})") \
            .props("outlined dense").classes("grow")

        def save():
            if (name.value or "").strip():
                strategies.save_strategy(name.value.strip(), res["best_spec"])
                ui.notify(t("strat.saved", name=name.value.strip()), type="positive")

        def use_best():
            state.STATE["current_spec"] = {"name": (name.value or "").strip() or t("opt.tuned"), **res["best_spec"]}
            state.STATE["bt_strategy"] = "__current__"
            ui.navigate.to("/backtest")
        ui.button(t("strat.save"), icon="save", on_click=save).props("outline no-caps")
        ui.button(t("opt.use_best"), icon="play_arrow", on_click=use_best).props("unelevated no-caps") \
            .mark("use_best")
