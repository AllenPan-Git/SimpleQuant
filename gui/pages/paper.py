"""第 6 页：模拟盘"""

import datetime as dt

import pandas as pd
from nicegui import run, ui

from simplequant import strategies
from simplequant.data import library
from simplequant.i18n import render
from simplequant.paper import list_accounts, load_account, delete_account, create_account, run_all, load_calendar
from simplequant.paper import account as account_mod
from simplequant.paper import runner, schedule
from simplequant.paper.calendar import latest_expected_day
from simplequant.stocks import UNIVERSES, universe as U
from simplequant.strategies import TEMPLATES
from simplequant.strategies.code_strategy import explain_error
from gui import state
from gui.common import t, p, lang
from gui.components import broker_settings, metric_tile, dividend_notices
from gui.layout import frame, page_title
from gui.widgets import df_table, fmt_table, plot, notice, download_btn, LogBox
from ui.charts import equity_chart
from ui.shared import pct, num

DENSE = "outlined dense options-dense"


def label(symbol: str) -> str:
    return symbol if "." not in symbol else symbol.split(".")[-1]


def page():
    PP = state.STATE.setdefault("pp", {"kind": "single", "strategy": None, "assets": [], "sel": None,
                                       "mode": "now", "past": str(dt.date.today() - dt.timedelta(days=180)),
                                       "name": "", "account": None, "task_time": "19:00"})
    with frame("/paper"):
        page_title("/paper", t("pp.intro"))
        logs = None

        async def run_accounts(accounts, refresh=True):
            logs.start()
            try:
                results = await run.io_bound(run_all, accounts, refresh=refresh, log=logs.append)
                failed = [k for k, v in results.items() if v]
                ui.notify(t("pp.run_done", ok=len(results) - len(failed), failed=len(failed)),
                          type="negative" if failed else "positive")
            finally:
                logs.stop()

        # ---------------- 新建账户 ----------------
        @ui.refreshable
        def new_account():
            accounts = list_accounts()
            with ui.expansion(t("pp.new"), icon="add_circle", value=not accounts).classes("w-full q-card"):
                _new_account_form(PP, run_accounts, lambda: (new_account.refresh(), body.refresh()))
        new_account()

        logs = LogBox()

        @ui.refreshable
        def body():
            _account_view(PP, run_accounts, lambda: (new_account.refresh(), body.refresh()))
        body()

        _schedule_box(PP)


def _new_account_form(PP, run_accounts, redraw):
    lg = lang()
    saved = strategies.list_strategies()
    form = ui.column().classes("w-full gap-3")
    info = {}

    def draw():
        form.clear()
        with form:
            ui.radio({k: t(f"pp.kind_{k}") for k in ("single", "selection")}, value=PP["kind"],
                     on_change=lambda e: (PP.__setitem__("kind", e.value), draw())).props("inline").mark("pp_kind")
            spec, universe, assets, ok = None, "", [], True
            if PP["kind"] == "single":
                opts = {f"saved:{n}": t("bt.opt_saved", name=n) for n, s in saved.items()
                        if strategies.runnable_on_single_assets(s)}
                opts.update({f"tpl:{k}": t("bt.opt_template", name=p(v.label)) for k, v in TEMPLATES.items()})
                if PP["strategy"] not in opts:
                    PP["strategy"] = next(iter(opts))
                choice = PP["strategy"]
                spec = saved[choice[6:]] if choice.startswith("saved:") else \
                    {"kind": "template", "template": choice[4:], "params": {}}
                ui.select(opts, label=t("bt.pick_strategy"), value=choice,
                          on_change=lambda e: (PP.__setitem__("strategy", e.value), draw())) \
                    .props(DENSE).classes("w-full").mark("pp_strategy")
                metas = [m for m in library.list_datasets() if m.freq == "1d" and m.source in ("akshare", "baostock")]
                by_id = {m.id: m for m in metas}
                PP["assets"] = [i for i in PP["assets"] if i in by_id]
                ui.select({i: f"{m.name} · {m.source}" for i, m in by_id.items()}, label=t("pp.assets"),
                          value=PP["assets"], multiple=True,
                          on_change=lambda e: (PP.__setitem__("assets", list(e.value or [])), draw())) \
                    .props(DENSE + " use-chips").classes("w-full").tooltip(t("pp.assets_help")).mark("pp_assets")
                assets = [{"source": by_id[i].source, "symbol": by_id[i].symbol,
                           "asset": by_id[i].extra.get("asset", ""), "name": by_id[i].name} for i in PP["assets"]]
                assets = list({(a["source"], a["symbol"]): a for a in reversed(assets)}.values())[::-1]  # 同一标的只算一次
                if not metas:
                    notice(t("pp.no_daily_data"), "info")
                ok = bool(assets) and len(assets) >= strategies.min_assets(spec)
            else:
                sel = {n: s for n, s in saved.items() if s.get("kind") == "selection"}
                if not sel:
                    notice(t("pp.no_selection"), "info")
                    ui.button(t("nav.go_selection"), on_click=lambda: ui.navigate.to("/selection")) \
                        .props("flat no-caps dense color=primary icon-right=arrow_forward").classes("self-start")
                    ok = False
                else:
                    if PP["sel"] not in sel:
                        PP["sel"] = next(iter(sel))
                    ui.select(list(sel), label=t("pp.selection_strategy"), value=PP["sel"],
                              on_change=lambda e: (PP.__setitem__("sel", e.value), draw())) \
                        .props(DENSE).classes("w-full").mark("pp_sel")
                    spec = sel[PP["sel"]]
                    universe = spec["universe"]
                    if not U.ready(universe):
                        notice(t("pp.need_universe", name=p(UNIVERSES[universe]["label"])), "warning", "warning")
                        ok = False
            if spec:
                ui.label(strategies.describe(spec, lg)).classes("sq-code w-full")

            with ui.row().classes("w-full items-center gap-6"):
                with ui.column().classes("gap-0"):
                    ui.label(t("pp.start_mode")).classes("text-xs sq-muted")

                    def on_mode(e):
                        PP["mode"] = e.value
                        past.set_enabled(e.value == "past")
                    ui.radio({m: t(f"pp.start_{m}") for m in ("now", "past")}, value=PP["mode"],
                             on_change=on_mode).props("inline dense").mark("pp_mode")
                past = ui.input(t("pp.start_date"), value=PP["past"],
                                on_change=lambda e: PP.__setitem__("past", e.value)) \
                    .props("outlined dense type=date").classes("w-44").tooltip(t("pp.start_date_help")).mark("pp_past")
                past.set_enabled(PP["mode"] == "past")
            single = PP["kind"] == "single"
            cb = not single and U.kind(universe) == "cb"
            make_broker = broker_settings("pp_" + PP["kind"] + ("_cb" if cb else ""),
                                          default_preset="etf" if single else "bond" if cb else "stock",
                                          default_cash=100_000 if single else 1_000_000, show_t1=single,
                                          show_dividend=single)   # 选股账户的分红处理跟随选股策略
            with ui.row().classes("w-full items-end gap-3"):
                name = ui.input(t("pp.name"), value=PP["name"], placeholder=t("pp.name_ph")) \
                    .props("outlined dense").classes("grow").mark("pp_name")
                btn = ui.button(t("pp.create"), icon="add", on_click=lambda: create()).props("unelevated no-caps") \
                    .mark("pp_create")

            def can():
                btn.set_enabled(bool(ok and spec and (name.value or "").strip()))
            name.on_value_change(lambda e: (PP.__setitem__("name", e.value or ""), can()))
            can()
            info.update(spec=spec, universe=universe, assets=assets, make_broker=make_broker, btn=btn)

    async def create():
        spec, universe, assets = info["spec"], info["universe"], info["assets"]
        broker, kind, mode = info["make_broker"](), PP["kind"], PP["mode"]
        info["btn"].disable()
        try:
            if mode == "past":
                start = PP["past"]
            elif kind == "selection":
                start = U.load_benchmark(universe).index[-1].date().isoformat()
            else:
                start = dt.date.today().isoformat()   # 先占位：下载数据后改成最新数据日
            acc = create_account(PP["name"].strip(), spec, broker, start, assets=assets, universe=universe)
            if mode == "now" and kind == "single":
                prices = await run.io_bound(runner.refresh_single_data, acc, today=dt.date.today().isoformat())
                acc.start = max(df.index[-1] for df in prices.values()).date().isoformat()
                acc.save()
            # 选股账户直接用本机已有的股票池数据；单标的"从现在开始"时刚下载过，不再重复下载
            await run_accounts([acc], refresh=kind == "single" and mode == "past")
            PP["account"] = acc.id
            PP["name"] = ""
        except Exception as e:  # noqa: BLE001
            msg = explain_error(e, lang())
            ui.notify(msg if msg != str(e) else f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            info["btn"].enable()
            return
        redraw()

    draw()


def _account_view(PP, run_accounts, redraw):
    lg = lang()
    accounts = list_accounts()
    if not accounts:
        return
    ids = [a.id for a in accounts]
    if PP["account"] not in ids:
        PP["account"] = ids[0]

    def acc_label(a):
        return f"{a.name} · {t('pp.kind_' + a.kind)}" + ("" if a.status == "active" else f" · {t('pp.paused')}")
    ui.select({a.id: acc_label(a) for a in accounts}, label=t("pp.account"), value=PP["account"],
              on_change=lambda e: (PP.__setitem__("account", e.value), redraw())) \
        .props(DENSE).classes("w-full md:w-1/2").mark("pp_account")
    acc = load_account(PP["account"])
    st = acc.state()
    nav = acc.nav()
    calendar = load_calendar(refresh_if_stale=False)
    expected = latest_expected_day(calendar)
    stale = not acc.data_through or pd.Timestamp(acc.data_through) < expected

    async def run_this():
        b1.disable()
        await run_accounts([acc])
        redraw()

    async def run_every():
        b2.disable()
        await run_accounts(list_accounts())
        redraw()

    def toggle_pause():
        acc.status = "active" if acc.status != "active" else "paused"
        acc.save()
        redraw()

    async def ask_delete():
        with ui.dialog() as dlg, ui.card():
            ui.label(t("pp.delete_confirm", name=acc.name))
            with ui.row().classes("w-full justify-end"):
                ui.button(t("gui.cancel"), on_click=lambda: dlg.submit(False)).props("flat no-caps")
                ui.button(t("pp.delete_yes"), on_click=lambda: dlg.submit(True)) \
                    .props("unelevated no-caps color=negative").mark("pp_delete_yes")
        ok = await dlg
        dlg.delete()
        if ok:
            delete_account(acc)
            PP["account"] = None
            redraw()

    with ui.row().classes("items-center gap-3"):
        b1 = ui.button(t("pp.run_this"), icon="refresh", on_click=run_this).props("unelevated no-caps") \
            .mark("pp_run_this")
        b2 = ui.button(t("pp.run_all"), icon="sync", on_click=run_every).props("outline no-caps")
        paused = acc.status != "active"
        ui.button(t("pp.resume") if paused else t("pp.pause"), icon="play_circle" if paused else "pause_circle",
                  on_click=toggle_pause).props("outline no-caps").mark("pp_pause")
        ui.button(t("pp.delete"), icon="delete", on_click=ask_delete).props("outline no-caps color=negative") \
            .mark("pp_delete")

    if stale:
        notice(t("pp.stale", through=acc.data_through or "—", expected=expected.date()), "update", "warning")
    if st.get("divergence"):
        notice(t("pp.divergence"), "rule", "warning")
    if not st:
        notice(t("pp.not_run"), "info")
        return

    # ---------------- 概况 ----------------
    cash0 = acc.broker["cash"]
    value = st["value"]
    day_pnl = float(nav["value"].iloc[-1] - nav["value"].iloc[-2]) if len(nav) > 1 else 0.0
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-6"):
        metric_tile(t("pp.value"), num(value, 0))
        ret = value / cash0 - 1
        metric_tile(t("pp.total_return"), pct(ret))
        metric_tile(t("pp.day_pnl"), num(day_pnl, 0))
        metric_tile(t("pp.n_positions"), str(len(st["positions"])))
        metric_tile(t("pp.data_through"), st["as_of"])
        metric_tile(t("pp.execute_on"), st["execute_on"], help_text=t("pp.execute_on_help"))
    cash_div = acc.broker.get("dividend") == "cash" or acc.spec.get("dividend") == "cash"
    ui.label(t("pp.meta", start=acc.start, created=acc.created, last_run=acc.last_run or "—")
             + (" · " + t("bt.chip_cash_div") if cash_div else "")
             + ("" if st.get("started") else " · " + t("pp.not_started"))).classes("sq-muted text-xs")
    m = st.get("metrics") or {}
    if "dividend_cash" in m:
        ui.label(t("pp.dividends", cash=num(m["dividend_cash"], 0), tax=num(m["dividend_tax"], 0)))             .classes("sq-muted text-xs").mark("pp_dividends")
    notes = runner.dividend_notes(acc)
    if notes:
        dividend_notices(notes.get("skipped", {}), notes.get("patched", {}))

    with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
        tab_sig = ui.tab("sig", t("pp.tab_signals"), icon="notifications_active")
        tab_pos = ui.tab("pos", t("pp.tab_positions"), icon="account_balance_wallet")
        tab_nav = ui.tab("nav", t("pp.tab_nav"), icon="show_chart")
        tab_fill = ui.tab("fill", t("pp.tab_fills"), icon="receipt_long")
    with ui.tab_panels(tabs, value=tab_sig).classes("w-full bg-transparent"):
        with ui.tab_panel(tab_sig).classes("px-0 gap-2"):
            sig = st["signals"]
            ui.label(t("pp.signals_title", date=st["execute_on"])).classes("font-semibold")
            if not sig:
                ui.label(t("pp.no_signals")).classes("sq-muted")
            else:
                df = pd.DataFrame([{
                    t("pp.col_code"): label(s["symbol"]), t("pp.col_name"): s["name"],
                    t("col.side"): t(f"side.{s['side']}"), t("pp.col_shares"): int(round(s["size"])),
                    t("pp.col_ref_price"): s["ref_price"], t("pp.col_est_value"): s["est_value"],
                    t("pp.col_reason"): render(*s["reason"], lang=lg) if s.get("reason") else ""} for s in sig])
                df_table(fmt_table(df, {t("pp.col_ref_price"): "{:.3f}", t("pp.col_est_value"): "{:,.0f}"}))
                download_btn(t("pp.download_signals"), lambda: df.to_csv(index=False).encode("utf-8-sig"),
                             f"signals_{acc.name}_{st['execute_on']}.csv", "text/csv")
            ui.label(t("pp.signals_note")).classes("sq-muted text-xs")
        with ui.tab_panel(tab_pos).classes("px-0 gap-2"):
            pos = st["positions"]
            if not pos:
                ui.label(t("pp.no_positions")).classes("sq-muted")
            else:
                dfp = pd.DataFrame([{
                    t("pp.col_code"): label(x["symbol"]), t("pp.col_name"): x["name"],
                    t("pp.col_shares"): int(round(x["size"])), t("pp.col_cost"): x["cost"],
                    t("pp.col_price"): x["price"], t("pp.col_value"): x["value"],
                    t("pp.col_pnl"): x["price"] / x["cost"] - 1 if x["cost"] else None,
                    t("pp.col_weight"): x["weight"]} for x in sorted(pos, key=lambda x: -x["value"])])
                df_table(fmt_table(dfp, {t("pp.col_cost"): "{:.3f}", t("pp.col_price"): "{:.3f}",
                                         t("pp.col_value"): "{:,.0f}", t("pp.col_pnl"): "{:.2%}",
                                         t("pp.col_weight"): "{:.1%}"}))
                download_btn(t("pp.download_positions"), lambda: dfp.to_csv(index=False).encode("utf-8-sig"),
                             f"positions_{acc.name}_{st['as_of']}.csv", "text/csv")
            ui.label(t("pp.cash", cash=num(st["cash"], 0))).classes("sq-muted text-xs")
        with ui.tab_panel(tab_nav).classes("px-0"):
            if len(nav) >= 2:
                eq = nav.copy()
                eq["drawdown"] = eq["value"] / eq["value"].cummax() - 1
                bench = None
                if acc.kind == "selection":
                    bench = t("cb.bench_label") if U.kind(acc.universe) == "cb" else \
                        t("sp.bench_label", name=p(UNIVERSES[acc.universe]["label"]))
                with ui.card().classes("w-full p-2"):
                    plot(equity_chart(eq, lg, bench))
            else:
                ui.label(t("pp.nav_soon")).classes("sq-muted")
        with ui.tab_panel(tab_fill).classes("px-0"):
            fills = acc.fills()
            if fills.empty:
                ui.label(t("pp.no_fills")).classes("sq-muted")
            else:
                f = fills.sort_values("time", ascending=False).copy()
                f["side"] = f["side"].map(lambda s: t(f"side.{s}"))
                f["symbol"] = f["symbol"].map(label)
                f = f.rename(columns={c: t(f"col.{c}") for c in f.columns})
                df_table(fmt_table(f, {t("col.price"): "{:.3f}", t("col.value"): "{:,.0f}",
                                       t("col.commission"): "{:.2f}"}), rows_per_page=30)


def _schedule_box(PP):
    with ui.expansion(t("pp.schedule"), icon="schedule").classes("w-full q-card"):
        ui.label(t("pp.schedule_note")).classes("sq-muted text-sm")

        @ui.refreshable
        def box():
            exists = schedule.task_exists()
            ui.label(t("pp.task_on") if exists else t("pp.task_off")).classes("font-medium")
            with ui.row().classes("items-center gap-3"):
                at = ui.input(t("pp.task_time"), value=PP["task_time"],
                              on_change=lambda e: PP.__setitem__("task_time", e.value)) \
                    .props("outlined dense type=time").classes("w-36")

                def create():
                    ok, msg = schedule.create_task(at.value or "19:00")
                    ui.notify(msg, type="positive" if ok else "negative", multi_line=True)
                    box.refresh()

                def delete():
                    ok, msg = schedule.delete_task()
                    ui.notify(msg, type="positive" if ok else "negative", multi_line=True)
                    box.refresh()
                ui.button(t("pp.task_create") if not exists else t("pp.task_update"), icon="alarm_add",
                          on_click=create).props("outline no-caps")
                if exists:
                    ui.button(t("pp.task_delete"), icon="alarm_off", on_click=delete).props("outline no-caps")
            ui.label(schedule.task_command()).classes("sq-code w-full")
            log = account_mod.PAPER_ROOT / "daily.log"
            if log.exists():
                ui.label(t("pp.log")).classes("font-medium text-sm")
                ui.label("\n".join(log.read_text(encoding="utf-8").splitlines()[-20:])).classes("sq-code w-full")
        box()
