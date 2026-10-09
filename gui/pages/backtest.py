"""第 3 页：回测（结果按研究报告排版：结论段落 → 指标 → 图 → 明细，右侧给出下一步）"""

import datetime as dt

import pandas as pd
from nicegui import run, ui

from simplequant import llm, strategies
from simplequant.strategies.code_strategy import explain_error
from simplequant.data.base import freq_label
from simplequant.engine import run_backtest, COST_PRESETS
from simplequant.engine.batch import run_batch, summarize
from simplequant.engine.credibility import check_backtest
from simplequant.engine.native_plot import render as render_native, MAX_BARS as NATIVE_MAX_BARS
from simplequant.export import PLATFORMS
from gui import state, history
from gui.plan_io import import_button, export_bytes, plan_filename, plan_check_notice
from simplequant import plan as plan_mod
from gui.common import t, p, lang
from gui.components import (require_data, data_selector, strategy_selector, broker_settings, prepare_prices,
                            metric_tiles, metric_tile, missing_factor_warnings, dividend_notices,
                            unadjusted_warning, credibility_box)
from gui.layout import frame, page_title
from gui.widgets import df_table, fmt_table, plot, notice, download_btn
from ui.charts import equity_chart, strategy_chart, batch_scatter
from ui.shared import (export_script, export_filename, platform_export, orders_table, trades_table,
                       logs_table, pct, num, asset_names)


def page():
    with frame("/backtest"):
        page_title("/backtest")
        by_id = require_data()
        if by_id is None:
            import_button().classes("mt-2")
            return
        BT = state.STATE.setdefault("bt", {})
        lg = lang()

        # ---------------- 设置：收起时显示为一行摘要（数据 · 策略 · 区间 · 资金） ----------------
        with ui.row().classes("sq-setup-row w-full items-start gap-3 no-wrap"):
            setup = ui.expansion(value=BT.get("result") is None).classes("grow min-w-0 sq-setup")
            with setup.add_slot("header"):
                chips = ui.row().classes("sq-chip-row w-full items-stretch gap-0 no-wrap")
            with ui.row().classes("items-center gap-2 no-wrap pt-2"):
                run_btn = ui.button(t("bt.run"), icon="play_arrow", on_click=lambda: do_run()) \
                    .props("unelevated no-caps").mark("run")
                with ui.button(t("exp.menu"), icon="ios_share").props("outline no-caps") as exp_btn:
                    exp_menu = ui.menu().props("auto-close")
                import_button()
        with setup:
            with ui.column().classes("w-full gap-4 pt-3"):
                pick = data_selector(by_id, "bt", lambda: check())
                mode_row = ui.row().classes("w-full items-center gap-3 px-1")
                with mode_row:
                    ui.label(t("bt.mode")).classes("text-sm")
                    ui.radio({"portfolio": t("bt.mode_portfolio"), "batch": t("bt.mode_batch")},
                             value=state.STATE.get("bt_mode", "portfolio"), on_change=lambda e: on_mode(e.value)) \
                        .props("inline dense").mark("bt_mode")
                    ui.icon("help_outline").classes("sq-muted").tooltip(t("bt.mode_help"))
                strat = strategy_selector("bt", lambda: check())
                make_broker = broker_settings("bt", show_dividend=True)
        setup.on_value_change(lambda _: draw_chips())
        warnings = ui.column().classes("w-full gap-2")
        running = ui.row().classes("items-center gap-2")
        with running:
            ui.spinner(size="sm")
            running_text = ui.label(t("bt.running")).classes("sq-muted text-sm")
        running.set_visibility(False)

        def batch() -> bool:
            """分别回测：选了两个及以上标的，并选择了「分别回测」"""
            return state.STATE.get("bt_mode") == "batch" and len(pick.ids) > 1

        def on_mode(v):
            state.STATE["bt_mode"] = v
            check()

        def draw_chips():
            chips.clear()
            b = state.STATE.get("bt_broker", {})
            preset = COST_PRESETS.get(b.get("preset"), {})
            items = [
                ("data", t("bt.chip_batch", n=len(pick.ids)) if batch()
                 else " · ".join(asset_names(by_id, pick.ids, lang()).values()) or "—"),
                ("strategy", strat.get("name", "")),
                ("range", f"{pick.start} ~ {pick.end}" if pick.date_range else "—"),
                ("broker", t("bt.cash_wan", v=f"{(b.get('cash') or 0) / 1e4:g}")
                 + (" · " + p(preset["label"]) if preset else "") + (" · T+1" if b.get("t1", True) else "")
                 + (" · " + t("bt.chip_cash_div") if b.get("dividend") == "cash" else "")),
            ]
            with chips:
                for k, v in items:
                    with ui.element("div").classes("sq-chip"):
                        ui.label(t(f"bt.chip_{k}")).classes("k")
                        ui.label(v).classes("v")
                ui.space()
                ui.label(t("bt.edit_setup")).classes("sq-muted text-xs self-center pr-2 whitespace-nowrap")

        def ready() -> bool:
            need = strategies.min_assets(strat["spec"])
            if batch():
                return need <= 1 and len(pick.freqs) == 1 and pick.date_range is not None
            return bool(pick.ids) and len(pick.freqs) == 1 and pick.date_range is not None and len(pick.ids) >= need

        def check():
            warnings.clear()
            mode_row.set_visibility(len(pick.ids) > 1)
            if pick.union != batch():
                pick.union = batch()
                pick.refresh()               # 重新计算可选区间，并再次调用 check()
                return
            need = strategies.min_assets(strat["spec"])
            with warnings:
                if batch() and need > 1:
                    notice(t("bt.batch_need_single", n=need), "warning", "warning")
                elif pick.ids and len(pick.ids) < need:
                    notice(t("bt.need_assets", n=need), "warning", "warning")
                for w in missing_factor_warnings(by_id, pick.ids, strat["spec"]):
                    notice(w, "warning", "warning")
                if w := unadjusted_warning(by_id, pick.ids):
                    notice(w, "warning", "warning")
            ok = ready()
            run_btn.set_enabled(ok)
            exp_btn.set_enabled(ok)
            build_export_menu()
            draw_chips()

        def build_export_menu():
            exp_menu.clear()
            if not ready():
                return
            spec, label = strat["spec"], strat["label"]
            stem = export_filename(label)[:-3]
            with exp_menu:
                if spec.get("kind") in ("template", "rule"):
                    with ui.menu_item(on_click=export_plan).mark("plan_export"):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.icon("description")
                            with ui.column().classes("gap-0"):
                                ui.label(t("plan.export"))
                                ui.label(t("plan.export_help")).classes("sq-muted text-xs max-w-[320px]")
                    ui.separator()
                if batch():
                    ui.label(t("plan.batch_only")).classes("sq-muted text-xs px-4 py-2 max-w-[360px]")
                    return
                with ui.menu_item(on_click=lambda: ui.download.content(
                        export_script(spec, label, by_id, pick.ids, pick.date_range, make_broker(), lg),
                        export_filename(label), "text/x-python")):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("code")
                        ui.label(t("exp.button"))
                ui.separator()
                ui.label(t("exp.platform_note")).classes("sq-muted text-xs px-4 py-2 max-w-[360px]")
                for key, meta in PLATFORMS.items():
                    data, why = platform_export(key, spec, label, by_id, pick.ids, pick.date_range, make_broker(), lg)
                    item = ui.menu_item(on_click=lambda d=data, k=key: ui.download.content(
                        d, f"{stem}_{k}.py", "text/x-python"))
                    with item:
                        with ui.column().classes("gap-0"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.icon("upload_file")
                                ui.label(p(meta["label"]))
                            if why:
                                ui.label(why).classes("text-warning text-xs max-w-[320px]")
                    if data is None:
                        item.disable()
                    item.tooltip(why or t(f"exp.help_{key}"))

        def signature():
            """当前设置；导出方案时，回测结果与当前设置一致才附带结果"""
            return (strat["spec"], tuple(pick.ids), pick.date_range, make_broker())

        def export_plan():
            name = strat.get("name") or strat["label"].split("：")[-1].split(": ")[-1]
            res = BT.get("result")
            result = res.metrics if res is not None and BT.get("sig") == signature() else None
            try:
                data = export_bytes(name, strat["spec"], [by_id[i] for i in pick.ids], pick.date_range,
                                    make_broker(), "batch" if batch() else "portfolio", result)
            except plan_mod.PlanError as e:
                ui.notify(t(e.key, **e.kw), type="warning")
                return
            ui.download.content(data, plan_filename(name), "application/json")
            if result is None:
                ui.notify(t("plan.exported_no_result"), multi_line=True)

        async def do_run():
            if not ready():
                return
            spec, broker = strat["spec"], make_broker()
            plan_ctx = state.STATE.pop("bt_plan", None)        # 刚导入的方案：回测后与其中记录的结果核对
            sig = signature()
            run_btn.disable()
            running.set_visibility(True)
            if batch():
                await do_batch(spec, broker)
                return
            try:
                prices, skipped, patched = await run.io_bound(prepare_prices, by_id, pick, broker, spec)
                cls, params = strategies.resolve(spec)
                res = await run.io_bound(run_backtest, prices, cls, params, broker, with_panels=True)
                tuned = state.STATE.get("tuned") or {}          # 参数优化页「用最优参数回测」带过来的
                tried = tuned["n"] if tuned.get("spec") == spec else 0
                BT.clear()
                BT.update(result=res, title=(strat["label"], list(prices), next(iter(pick.freqs))), spec=spec,
                          broker=broker, name=strat.get("name", ""), div_notes=(skipped, patched),
                          checks=check_backtest(res.metrics, res.trades, tried), sig=sig)
                if plan_ctx:
                    BT["plan_check"] = (plan_mod.compare(plan_ctx.get("result"), res.metrics), plan_ctx)
                _remember(BT, by_id, pick, strat)
                setup.value = False
            except Exception as e:  # noqa: BLE001
                BT.clear()
                ui.notify(t("bt.failed", e=explain_error(e, lang())), type="negative", multi_line=True)
            finally:
                run_btn.enable()
                running.set_visibility(False)
            results.refresh()

        async def do_batch(spec, broker):
            done = {"n": 0, "of": len(pick.ids)}
            tick = ui.timer(0.3, lambda: running_text.set_text(t("bt.batch_running", n=done["n"], of=done["of"])))
            try:
                prices, skipped, patched = await run.io_bound(prepare_prices, by_id, pick, broker, spec)
                df = await run.io_bound(run_batch, prices, spec, broker, None,
                                        lambda n, of: done.update(n=n, of=of))
                BT.clear()
                BT.update(batch=df, summary=summarize(df), spec=spec, broker=broker,
                          name=strat.get("name", "") or strat["label"], freq=next(iter(pick.freqs)),
                          range=(pick.start, pick.end), div_notes=(skipped, patched),
                          ids={n: i for i, n in asset_names(by_id, pick.ids, lang()).items()},
                          time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
                setup.value = False
            except Exception as e:  # noqa: BLE001
                BT.clear()
                ui.notify(t("bt.failed", e=explain_error(e, lang())), type="negative", multi_line=True)
            finally:
                tick.cancel()
                running_text.set_text(t("bt.running"))
                run_btn.enable()
                running.set_visibility(False)
            results.refresh()

        check()

        @ui.refreshable
        def results():
            if BT.get("batch") is not None:
                _batch_results(BT)
            elif BT.get("result") is not None:
                _results(BT, exp_menu)
        results()

        # 从首页「最近的报告」打开：设置已恢复，自动重新运行
        if state.STATE.pop("bt_autorun", None) and ready():
            ui.timer(0.05, do_run, once=True)


def _batch_results(BT: dict):
    """批量回测的报告：结论段落 → 分布指标 → 散点图 → 逐个标的的明细"""
    lg = lang()
    df, s = BT["batch"], BT["summary"]
    with ui.column().classes("sq-report w-full gap-0 pt-2"):
        ui.label(t("bt.report_eyebrow", time=BT["time"])).classes("sq-eyebrow")
        ui.label(BT["name"]).classes("sq-title mt-1")
        ui.label(f"{t('bt.batch_title', n=s['n'])} · {BT['range'][0]} ~ {BT['range'][1]} · "
                 f"{freq_label(BT['freq'], lg)}").classes("sq-muted text-sm")
        if not s.get("ok"):
            with ui.column().classes("w-full pt-4"):
                notice(t("bt.batch_all_failed"), "error", "warning")
        else:
            ui.html(t("bt.batch_lede", n=s["ok"], beat=s["beat"], share=pct(s["beat_share"], 1),
                      med=pct(s["median_return"]), bmed=pct(s["median_benchmark"]),
                      low=pct(s["worst_decile"]), high=pct(s["best_decile"]))
                    + ("" if lg == "zh" else " ")
                    + t("bt.batch_lede_dd", share=pct(s["smaller_drawdown_share"], 1), dd=pct(s["median_drawdown"]),
                        bdd=pct(s["median_benchmark_drawdown"]))).classes("sq-lede my-5")
            with ui.row().classes("w-full gap-3"):
                metric_tile(t("bt.batch_beat"), pct(s["beat_share"], 1), f"{s['beat']} / {s['ok']}",
                            help_text=t("bt.batch_beat_help"))
                metric_tile(t("bt.batch_profit"), pct(s["profit_share"], 1),
                            t("bt.batch_vs_hold", v=pct(s["bench_profit_share"], 1)))
                metric_tile(t("bt.batch_median"), pct(s["median_return"]),
                            t("bt.batch_vs_hold", v=pct(s["median_benchmark"])))
                metric_tile(t("bt.batch_excess"), pct(s["median_excess"]), help_text=t("bt.batch_excess_help"))
                metric_tile(t("bt.batch_worst"), pct(s["worst_decile"]), help_text=t("bt.batch_worst_help"))
                metric_tile(t("bt.batch_dd"), pct(s["median_drawdown"]),
                            t("bt.batch_vs_hold", v=pct(s["median_benchmark_drawdown"])))
            with ui.column().classes("w-full pt-4 gap-2"):
                if any(BT.get("div_notes", ({}, {}))):
                    dividend_notices(*BT["div_notes"])
                if s["failed"]:
                    notice(t("bt.batch_failed", n=s["failed"]), "error_outline", "warning")
                if s["no_trades"]:
                    notice(t("bt.batch_no_trades", n=s["no_trades"]), "info")
                if s["lot_too_big"]:
                    notice(t("bt.batch_lot", n=s["lot_too_big"]), "savings", "warning")
                if s["holding"]:
                    notice(t("bt.batch_holding", n=s["holding"]), "info")
                notice(t("bt.batch_note"), "lightbulb")
            with ui.column().classes("w-full gap-1 pt-6"):
                plot(batch_scatter(df, lg))
                ui.html(f"<b>{t('bt.fig1')}</b>{t('bt.batch_fig')}").classes("sq-caption")

    with ui.element("div").classes("sq-sec"):
        ui.label(t("bt.details")).classes("sq-h2")
    names = list(df.loc[df["error"].isna(), "symbol"])
    if names:
        with ui.row().classes("w-full items-center gap-3"):
            one = ui.select(names, label=t("bt.batch_open"), value=names[0]).props("outlined dense").classes("w-72")
            ui.button(t("bt.batch_open_btn"), icon="open_in_new", on_click=lambda: _open_single(BT, one.value)) \
                .props("outline no-caps").mark("batch_open")
    tbl = _batch_table(df)
    df_table(tbl, rows_per_page=20)
    download_btn(t("bt.batch_download"), lambda: tbl.to_csv(index=False).encode("utf-8-sig"), "batch.csv", "text/csv")


def _batch_table(df: pd.DataFrame) -> pd.DataFrame:
    """按超额收益从高到低排列；出错的标的排在最后，只有备注"""
    df = df.assign(_x=df["excess_return"].astype(float)).sort_values("_x", ascending=False, na_position="last")

    def p2(v):
        return "" if pd.isna(v) else f"{v * 100:.2f}%"

    def span(a, b):
        return f"{pd.Timestamp(a):%Y-%m-%d} ~ {pd.Timestamp(b):%Y-%m-%d}" if pd.notna(a) and pd.notna(b) else ""
    return pd.DataFrame({
        t("bt.batch_col_symbol"): df["symbol"],
        t("bt.batch_col_range"): [span(a, b) for a, b in zip(df["start"], df["end"])],
        t("bt.batch_col_ret"): df["total_return"].map(p2),
        t("bt.batch_col_hold"): df["benchmark_return"].map(p2),
        t("bt.batch_col_excess"): df["excess_return"].map(p2),
        t("bt.batch_col_dd"): df["max_drawdown"].map(p2),
        t("bt.batch_col_hold_dd"): df["benchmark_max_drawdown"].map(p2),
        t("bt.batch_col_trades"): df["trades"].map(lambda v: "" if pd.isna(v) else str(int(v))),
        t("bt.batch_col_win"): df["win_rate"].map(p2),
        t("bt.batch_col_holding"): df["holding"].map(lambda v: t("bt.batch_yes") if v is True else ""),
        t("bt.batch_col_note"): df["error"].fillna(""),
    })


def _open_single(BT: dict, name: str | None):
    """对其中一个标的运行完整回测（区间、策略、费率沿用当前设置）"""
    if not name or name not in BT["ids"]:
        return
    S = state.STATE
    S["bt_ids"], S["bt_mode"], S["bt_autorun"] = [BT["ids"][name]], "portfolio", True
    S.pop("bt_range", None)
    BT.clear()
    ui.navigate.to("/backtest")


def _remember(BT: dict, by_id: dict, pick, strat: dict):
    """记入首页「最近的报告」（只存摘要和重新运行所需的设置）"""
    res = BT["result"]
    name = strat.get("name") or strat["label"]
    data = " · ".join(asset_names(by_id, pick.ids, lang()).values())
    freq = next(iter(pick.freqs))
    history.add("bt", name, f"{data} · {freq_label(freq, lang())} · {pick.start} ~ {pick.end}", res.metrics,
                restore={"spec": {"name": name, **strat["spec"]}, "ids": list(pick.ids),
                         "range": [str(pick.start), str(pick.end)],
                         "broker": dict(state.STATE.get("bt_broker", {}))},
                data=data, strategy=name)
    BT["time"] = history.recent()[0]["time"]


def _span(index) -> str:
    days = (index[-1] - index[0]).days
    months = round(days / 30.44)
    y, m = divmod(months, 12)
    if months < 1:
        return t("bt.span_d", d=days)
    if y == 0:
        return t("bt.span_m", m=m)
    return t("bt.span_y", y=y) if m == 0 else t("bt.span_ym", y=y, m=m)


def lede(res) -> str:
    """结论段落：先讲结果，再和买入持有比较（数字全部来自本次回测）"""
    m, eq = res.metrics, res.equity
    bench_dd = float((eq["benchmark"] / eq["benchmark"].cummax() - 1).min())
    text = t("bt.lede", span=_span(eq.index), ret=pct(m["total_return"]), cagr=pct(m["cagr"]),
             bench=pct(m["benchmark_return"]), dd=pct(m["max_drawdown"]), bdd=pct(bench_dd), n=m["trades"])
    diff = (m["total_return"] - m["benchmark_return"]) * 100
    parts = [text, t("bt.lede_beat" if diff >= 0 else "bt.lede_lag", v=f"{abs(diff):.1f}")]
    if m["max_drawdown"] > bench_dd + 0.005:
        parts.append(t("bt.lede_calmer"))
    elif m["max_drawdown"] < bench_dd - 0.005:
        parts.append(t("bt.lede_rougher"))
    return ("" if lang() == "zh" else " ").join(parts)      # 英文句子之间要空格


def _rule_title(text: str, top: bool = False):
    ui.label(text).classes("sq-serif text-lg pb-2 w-full" + (" pt-8" if top else "")) \
        .style("border-bottom: 1px solid var(--sq-text)")


def _results(BT: dict, exp_menu):
    lg = lang()
    res = BT["result"]
    label, names, freq = BT["title"]
    symbols = list(res.prices)
    with ui.element("div").classes("sq-report w-full grid gap-12 pt-2").style("grid-template-columns: minmax(0, 1fr) 280px"):
        # ---------------- 报告正文 ----------------
        with ui.column().classes("gap-0 min-w-0"):
            ui.label(t("bt.report_eyebrow", time=BT.get("time") or dt.datetime.now().strftime("%Y-%m-%d %H:%M"))) \
                .classes("sq-eyebrow")
            ui.label(BT.get("name") or label).classes("sq-title mt-1")
            ui.label(f"{t('bt.results')} · {', '.join(names)} · {freq_label(freq, lg)}").classes("sq-muted text-sm")
            ui.html(lede(res)).classes("sq-lede my-5")
            metric_tiles(res.metrics)
            if "dividend_cash" in res.metrics:
                with ui.row().classes("w-full gap-3 pt-4"):
                    metric_tile(t("m.dividend_cash"), f"{res.metrics['dividend_cash']:,.0f}", help_text=t("bt.dividend_help"))
                    metric_tile(t("m.dividend_tax"), f"{res.metrics['dividend_tax']:,.0f}",
                                help_text=t("sp.dividend_tax_help"))
            if any(BT.get("div_notes", ({}, {}))):
                with ui.column().classes("w-full pt-4 gap-2"):
                    dividend_notices(*BT["div_notes"])
            if res.t0:
                with ui.column().classes("w-full pt-4"):
                    notice(t("bt.t0_assets", names=", ".join(res.t0)), "swap_horiz")
            if res.lot_too_big:
                with ui.column().classes("w-full pt-4"):
                    notice(t("bt.lot_too_big", names=", ".join(res.lot_too_big)), "savings", "warning")
            if BT.get("plan_check"):
                with ui.column().classes("w-full pt-4"):
                    plan_check_notice(*BT["plan_check"], res.metrics)
            with ui.column().classes("w-full pt-6"):
                credibility_box(BT.get("checks", []))
            explain_out = ui.column().classes("w-full pt-4")

            with ui.column().classes("w-full gap-1 pt-6"):
                plot(equity_chart(res.equity, lg))
                ui.html(f"<b>{t('bt.fig1')}</b>{t('bt.fig1_caption')}").classes("sq-caption")

            with ui.column().classes("w-full gap-1 pt-8"):
                chart_box = ui.column().classes("w-full")

                def draw(sel):
                    chart_box.clear()
                    with chart_box:
                        plot(strategy_chart(res.prices[sel], res.orders, res.trades, res.panels.get(sel, []), sel, lg))
                if len(symbols) > 1:
                    ui.select(symbols, label=t("bt.show_trades_for"), value=symbols[0],
                              on_change=lambda e: draw(e.value)).props("outlined dense").classes("w-72") \
                        .move(target_index=0)
                draw(symbols[0])
                ui.html(f"<b>{t('bt.fig2')}</b>{t('bt.fig2_caption')} {t('bt.strategy_chart_note')}") \
                    .classes("sq-caption")

        # ---------------- 右侧：下一步 + 最近平仓 ----------------
        with ui.column().classes("gap-0 min-w-0"):
            _rule_title(t("bt.next"))
            _next("04", t("bt.next_opt"), t("bt.next_opt_body"), _to_optimize, rec=True).mark("next_opt")
            _next("05", t("bt.next_paper"), t("bt.next_paper_body"), lambda: ui.navigate.to("/paper"))
            _next("↗", t("bt.next_export"), t("bt.next_export_body"), exp_menu.open)
            _explain(BT, explain_out)

            _rule_title(t("bt.recent_trades"), top=True)
            if res.trades.empty:
                ui.label(t("bt.no_closed")).classes("sq-muted text-sm py-2")
            for r in res.trades.sort_values("close_time", ascending=False).head(6).itertuples():
                with ui.row().classes("w-full justify-between py-2 no-wrap") \
                        .style("border-bottom: 1px solid var(--sq-border2)"):
                    ui.label(t("bt.trade_line", d=f"{pd.Timestamp(r.close_time):%Y-%m-%d}")
                             + (f" · {r.symbol}" if len(symbols) > 1 else "")).classes("text-sm")
                    ui.label(("+" if r.pnl_net >= 0 else "") + num(r.pnl_net, 0)) \
                        .classes("sq-num text-sm " + ("sq-up" if r.pnl_net >= 0 else "sq-down"))

    # ---------------- 明细 ----------------
    with ui.element("div").classes("sq-sec"):
        ui.label(t("bt.details")).classes("sq-h2")
    with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
        tab_o = ui.tab("o", t("bt.tab_orders"))
        tab_t = ui.tab("t", t("bt.tab_trades"))
        tab_l = ui.tab("l", t("bt.tab_logs"))
        tab_n = ui.tab("n", t("nat.tab")).mark("native_tab")
    with ui.tab_panels(tabs, value=tab_o).classes("w-full bg-transparent"):
        with ui.tab_panel(tab_o).classes("px-0 gap-2"):
            if res.orders.empty:
                ui.label(t("bt.no_orders")).classes("sq-muted")
            else:
                tbl = orders_table(res.orders, lg)
                df_table(fmt_table(tbl, {t("col.price"): "{:.3f}", t("col.value"): "{:,.2f}",
                                         t("col.commission"): "{:.2f}"}), rows_per_page=20)
                download_btn(t("bt.download_orders"), lambda: tbl.to_csv(index=False).encode("utf-8-sig"),
                             "orders.csv", "text/csv")
        with ui.tab_panel(tab_t).classes("px-0"):
            df_table(fmt_table(trades_table(res.trades, lg), {t("col.pnl"): "{:,.2f}", t("col.pnl_net"): "{:,.2f}"}),
                     rows_per_page=20)
        with ui.tab_panel(tab_l).classes("px-0"):
            df_table(logs_table(res.logs, lg), rows_per_page=50)
        with ui.tab_panel(tab_n).classes("px-0 gap-3"):
            _native(BT)


def _next(no: str, title: str, body: str, on_click, rec: bool = False):
    """右侧「下一步」的一项：编号 + 宋体标题 + 一行说明"""
    with ui.element("div").classes("sq-next" + (" sq-next-rec" if rec else "")).on("click", on_click) as el:
        ui.html(f"<i>{no}</i>")
        with ui.element("div"):
            ui.label(title).classes("sq-next-title")
            ui.label(body).classes("sq-next-body")
    return el


def _to_optimize():
    """带着当前的数据、区间和策略去参数优化页"""
    S = state.STATE
    for k in ("ids", "range", "strategy"):
        if f"bt_{k}" in S:
            S[f"opt_{k}"] = S[f"bt_{k}"]
    ui.navigate.to("/optimize")


def _explain(BT: dict, out):
    cfg = llm.load_config()
    if not (cfg and cfg.ready):
        return

    async def explain():
        el.classes(add="opacity-50")
        try:
            BT["explanation"] = await run.io_bound(
                llm.explain_result, llm.get_provider(cfg), strategies.describe(BT["spec"], lang()),
                BT["result"].metrics, lang())
        except llm.LLMError as e:
            ui.notify(str(e), type="negative", multi_line=True)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
        finally:
            el.classes(remove="opacity-50")
        show()

    def show():
        out.clear()
        if BT.get("explanation"):
            with out, ui.column().classes("w-full gap-1 sq-note py-3"):
                ui.markdown(BT["explanation"]).classes("text-sm")
                ui.label(t("ai.explain_note")).classes("sq-muted text-xs")

    el = _next("AI", t("ai.explain"), t("bt.next_ai_body"), explain).mark("explain")
    show()


def _native(BT: dict):
    res = BT["result"]
    ui.label(t("nat.note")).classes("sq-muted text-sm")
    idx = next(iter(res.prices.values())).index
    daily = len(idx) < 2 or (idx[1:] - idx[:-1]).median() >= pd.Timedelta(hours=20)
    default_bars = 250 if daily else 960            # 默认最近约一年（日线）或约一周（分钟线）
    lo, hi = idx[0].date(), idx[-1].date()
    rng = BT.setdefault("native_range", (idx[max(0, len(idx) - default_bars)].date(), hi))
    with ui.row().classes("items-center gap-3"):
        start = ui.input(t("data.start"), value=str(rng[0])).props(f"outlined dense type=date min={lo} max={hi}") \
            .classes("w-44")
        end = ui.input(t("data.end"), value=str(rng[1])).props(f"outlined dense type=date min={lo} max={hi}") \
            .classes("w-44")
        ui.icon("help_outline").classes("sq-muted").tooltip(t("nat.range_help", n=NATIVE_MAX_BARS))
    img_box = ui.column().classes("w-full gap-2")

    def show():
        img_box.clear()
        png = BT.get("native_png")
        if png:
            import base64
            with img_box:
                ui.image("data:image/png;base64," + base64.b64encode(png).decode()).classes("w-full")
                download_btn(t("nat.download"), lambda: png, "backtrader_chart.png", "image/png")

    async def draw():
        try:
            a, b = dt.date.fromisoformat(start.value), dt.date.fromisoformat(end.value)
        except (TypeError, ValueError):
            return
        BT["native_range"] = (a, b)
        btn.disable()
        try:
            cls, params = strategies.resolve(BT["spec"])
            BT["native_png"] = await run.io_bound(render_native, res.prices, cls, params, BT.get("broker"),
                                                  start=a, end=b, lang=lang())
        except ValueError as e:
            BT.pop("native_png", None)
            ui.notify(str(e), type="negative", multi_line=True)
        finally:
            btn.enable()
        show()

    btn = ui.button(t("nat.draw"), icon="draw", on_click=draw).props("outline no-caps").classes("self-start") \
        .mark("native")
    show()
