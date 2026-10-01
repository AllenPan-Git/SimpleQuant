"""第 3 页：回测（结果按研究报告排版：结论段落 → 指标 → 图 → 明细，右侧给出下一步）"""

import datetime as dt

import pandas as pd
from nicegui import run, ui

from simplequant import llm, strategies
from simplequant.strategies.code_strategy import explain_error
from simplequant.data.base import freq_label
from simplequant.engine import run_backtest, COST_PRESETS
from simplequant.engine.native_plot import render as render_native, MAX_BARS as NATIVE_MAX_BARS
from simplequant.export import PLATFORMS
from gui import state, history
from gui.common import t, p, lang
from gui.components import (require_data, data_selector, strategy_selector, broker_settings, prepare_prices,
                            metric_tiles, metric_tile, missing_factor_warnings, dividend_notices,
                            unadjusted_warning)
from gui.layout import frame, page_title
from gui.widgets import df_table, fmt_table, plot, notice, download_btn
from ui.charts import equity_chart, strategy_chart
from ui.shared import (export_script, export_filename, platform_export, orders_table, trades_table,
                       logs_table, pct, num)


def page():
    with frame("/backtest"):
        page_title("/backtest")
        by_id = require_data()
        if by_id is None:
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
        with setup:
            with ui.column().classes("w-full gap-4 pt-3"):
                pick = data_selector(by_id, "bt", lambda: check())
                strat = strategy_selector("bt", lambda: check())
                make_broker = broker_settings("bt", show_dividend=True)
        setup.on_value_change(lambda _: draw_chips())
        warnings = ui.column().classes("w-full gap-2")
        running = ui.row().classes("items-center gap-2")
        with running:
            ui.spinner(size="sm")
            ui.label(t("bt.running")).classes("sq-muted text-sm")
        running.set_visibility(False)

        def draw_chips():
            chips.clear()
            b = state.STATE.get("bt_broker", {})
            preset = COST_PRESETS.get(b.get("preset"), {})
            items = [
                ("data", " · ".join(by_id[i].name for i in pick.ids) or "—"),
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
            return bool(pick.ids) and len(pick.freqs) == 1 and pick.date_range is not None and len(pick.ids) >= need

        def check():
            warnings.clear()
            need = strategies.min_assets(strat["spec"])
            with warnings:
                if pick.ids and len(pick.ids) < need:
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

        async def do_run():
            if not ready():
                return
            spec, broker = strat["spec"], make_broker()
            run_btn.disable()
            running.set_visibility(True)
            try:
                prices, skipped, patched = await run.io_bound(prepare_prices, by_id, pick, broker)
                cls, params = strategies.resolve(spec)
                res = await run.io_bound(run_backtest, prices, cls, params, broker, with_panels=True)
                BT.clear()
                BT.update(result=res, title=(strat["label"], list(prices), next(iter(pick.freqs))), spec=spec,
                          broker=broker, name=strat.get("name", ""), div_notes=(skipped, patched))
                _remember(BT, by_id, pick, strat)
                setup.value = False
            except Exception as e:  # noqa: BLE001
                BT.clear()
                ui.notify(t("bt.failed", e=explain_error(e, lang())), type="negative", multi_line=True)
            finally:
                run_btn.enable()
                running.set_visibility(False)
            results.refresh()

        check()

        @ui.refreshable
        def results():
            if BT.get("result") is not None:
                _results(BT, exp_menu)
        results()

        # 从首页「最近的报告」打开：设置已恢复，自动重新运行
        if state.STATE.pop("bt_autorun", None) and ready():
            ui.timer(0.05, do_run, once=True)


def _remember(BT: dict, by_id: dict, pick, strat: dict):
    """记入首页「最近的报告」（只存摘要和重新运行所需的设置）"""
    res = BT["result"]
    name = strat.get("name") or strat["label"]
    data = " · ".join(by_id[i].name for i in pick.ids)
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
        tab_n = ui.tab("n", t("nat.tab"))
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
