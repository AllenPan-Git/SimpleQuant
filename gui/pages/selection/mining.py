"""选股页「AI 因子挖掘」标签页"""

from dataclasses import asdict

import pandas as pd
from nicegui import run, ui

from simplequant import llm
from simplequant.engine.credibility import check_mining
from simplequant.stocks import UNIVERSES
from simplequant.stocks import custom_factors
from simplequant.stocks import mining as MN
from gui.common import t, p, lang
from gui.components import metric_tile, credibility_box
from gui.widgets import df_table, notice, download_btn, Progress, LogBox
from ui.shared import num
from .common import DENSE


# ================= AI 因子挖掘 =================
def _mining(SP, ctx):
    """AI 提出因子 → 只在挖掘期评估 → 反馈 → 再提；结束后看一次检验期（simplequant/stocks/mining.py）"""
    lg = lang()
    panel = ctx.panel
    M = SP.setdefault("mine", {"cfg": asdict(MN.MineConfig()), "miner": None, "saved": {}, "running": False})
    M.setdefault("log", [])
    cfg = llm.load_config()
    ready = bool(cfg and cfg.ready)

    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2 no-wrap sq-note py-2"):
            ui.icon("info").classes("text-primary")
            ui.label(t("mine.intro")).classes("text-sm")
        if panel.kind != "stock":
            notice(t("mine.cb_only_stock"), "block", "warning")
            return
        if not ready:
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                notice(t("ai.need_config"), "key")
                ui.button(t("nav.go_settings"), on_click=lambda: ui.navigate.to("/settings")) \
                    .props("flat no-caps dense color=primary icon-right=arrow_forward")
        C = M["cfg"]

        def set_c(k, cast):
            def f(e):
                if e.value is not None:
                    C[k] = cast(e.value)
                    plan_text()
            return f
        with ui.row().classes("w-full items-start gap-3"):
            ui.number(t("mine.rounds"), value=C["rounds"], min=1, max=20, step=1, precision=0,
                      on_change=set_c("rounds", int)).props("outlined dense").classes("w-28") \
                .tooltip(t("mine.rounds_help")).mark("mine_rounds")
            ui.number(t("mine.per_round"), value=C["per_round"], min=1, max=8, step=1, precision=0,
                      on_change=set_c("per_round", int)).props("outlined dense").classes("w-32").mark("mine_per_round")
            ui.select({h: str(h) for h in (5, 10, 20, 60)}, label=t("sp.horizon"), value=C["horizon"],
                      on_change=set_c("horizon", int)).props(DENSE).classes("w-32").tooltip(t("sp.horizon_help"))
            ui.select({0.2: "20%", 0.3: "30%", 0.4: "40%"}, label=t("mine.test_frac"), value=C["test_frac"],
                      on_change=set_c("test_frac", float)).props(DENSE).classes("w-32") \
                .tooltip(t("mine.test_frac_help"))
            ui.number(t("mine.min_t"), value=C["min_t"], min=1, max=5, step=0.5, precision=1,
                      on_change=set_c("min_t", float)).props("outlined dense").classes("w-32") \
                .tooltip(t("mine.min_t_help"))
            ui.number(t("mine.max_corr"), value=C["max_corr"], min=0.3, max=0.95, step=0.05, precision=2,
                      on_change=set_c("max_corr", float)).props("outlined dense").classes("w-32") \
                .tooltip(t("mine.max_corr_help"))
        idea = ui.textarea(t("mine.idea"), value=C["idea"], placeholder=t("mine.idea_ph")) \
            .props("outlined autogrow").classes("w-full").mark("mine_idea")
        idea.on_value_change(lambda e: C.update(idea=e.value or ""))
        split = ui.label().classes("sq-muted text-sm")
        notice(t("mine.auto_run"), "warning", "warning")

        prog = Progress()
        logs = LogBox()
        # 挖掘在后台线程里跑，进度和日志写进 M（不直接写界面元素）；页面用定时器去读。
        # 这样离开选股页再回来，新页面能接着显示，结束后也能刷新结果
        seen = {"running": False}

        def show_running():
            seen["running"] = True
            go.disable()
            stop_btn.set_visibility(True)
            prog.start()
            logs.start()
            logs.lines = M["log"]

        def watch():
            if M.get("running"):
                if not seen["running"]:
                    show_running()
                prog.set(*M.get("prog", (0.0, "")))
            elif seen["running"]:
                seen["running"] = False
                prog.stop()
                logs.stop()
                stop_btn.set_visibility(False)
                plan_text()
                results.refresh()
        ui.timer(0.3, watch)

        async def start():
            try:
                miner = MN.Miner(panel, ctx.start, MN.MineConfig(**C), lg)
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return
            M.update(running=True, stop=False, log=[], prog=(0.0, ""), miner=None, saved={})
            show_running()
            try:
                await run.io_bound(miner.run, llm.get_provider(cfg), M["log"].append,
                                   lambda v, s="": M.__setitem__("prog", (v, s)), lambda: M.get("stop"))
                M["miner"] = miner
            except Exception as e:  # noqa: BLE001
                M["log"].append(f"{type(e).__name__}: {e}")
            finally:
                M["running"] = False

        def stop():
            M["stop"] = True
            ui.notify(t("mine.stopping"), type="info")
        with ui.row().classes("items-center gap-3"):
            go = ui.button(t("mine.start"), icon="auto_awesome", on_click=start).props("unelevated no-caps") \
                .mark("mine_start")
            stop_btn = ui.button(t("mine.stop"), icon="stop", on_click=stop).props("outline no-caps color=negative")
            stop_btn.set_visibility(False)

        def plan_text():
            C["min_days"] = int(SP["res"]["min_days"])
            cal = panel.calendar[panel.calendar >= pd.Timestamp(ctx.start)]
            k = int(len(cal) * (1 - C["test_frac"]))
            h = C["horizon"]
            ok = k >= 3 * (h + 1) and len(cal) - k >= 3 * (h + 1)
            split.text = (t("mine.split", a=cal[0].date(), b=cal[k - 1].date(), c=cal[k].date(), d=cal[-1].date())
                          + " · " + t("mine.calls", n=C["rounds"], m=C["rounds"] * C["per_round"])) if ok \
                else t("mine.too_short", h=h)
            go.set_enabled(ready and ok and not M["running"])
        plan_text()

    @ui.refreshable
    def results():
        miner = M.get("miner")
        if miner is None or miner.result is None:
            return
        _mining_results(SP, ctx, miner, results.refresh)
    results()


def _mining_results(SP, ctx, miner, refresh):
    lg = lang()
    M, r = SP["mine"], miner.result
    ic3 = lambda v: num(v, 3)
    with ui.card().classes("w-full gap-3"):
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            metric_tile(t("mine.proposed"), str(r["proposed"]))
            metric_tile(t("mine.accepted"), str(r["accepted"]))
            metric_tile(t("mine.calls_used"), str(r["calls"]))
            metric_tile(t("mine.t_needed"), num(r["t_needed"]), help_text=t("mine.t_needed_help"))
        findings = check_mining(r)
        credibility_box(findings)

        def record():
            uni = p(UNIVERSES[SP["universe"]]["label"])
            return MN.to_markdown(miner, lg, uni, [t(f["key"], **f["args"]) for f in findings])
        stamp = f"{miner.start.date():%Y%m%d}-{miner.end.date():%Y%m%d}"
        download_btn(t("mine.export"), record, f"{t('mine.export_name')}_{SP['universe']}_{stamp}.md",
                     "text/markdown", props="dense").classes("self-start").tooltip(t("mine.export_help"))             .mark("mine_export")

    async def save(c):
        if not c.held:
            with ui.dialog() as dlg, ui.card():
                ui.label(t("mine.save_warn"))
                with ui.row().classes("w-full justify-end"):
                    ui.button(t("gui.cancel"), on_click=lambda: dlg.submit(False)).props("flat no-caps")
                    ui.button(t("strat.save"), on_click=lambda: dlg.submit(True)).props("unelevated no-caps") \
                        .mark("confirm-save")
            ok = await dlg
            dlg.delete()
            if not ok:
                return
        names = {d["name"] for d in custom_factors.list_factors().values()}
        name, i = c.name, 2
        while name in names:             # 同名会覆盖已保存的因子
            name, i = f"{c.name} {i}", i + 1
        custom_factors.save_factor(name, c.code, c.direction, c.desc or c.idea)
        M["saved"][c.name] = name
        ui.notify(t("mine.saved", name=name), type="positive")
        ctx.factors_changed()
        ctx.reload_cf()
        refresh()

    acc = miner.accepted
    if acc:
        with ui.card().classes("w-full gap-3"):
            ui.label(t("mine.accepted_title")).classes("font-semibold")
            df_table(pd.DataFrame([{
                t("mine.col_name"): c.name, t("mine.col_dir"): t("sel.dir_up" if c.direction > 0 else "sel.dir_down"),
                t("mine.col_mine_ic"): ic3(abs(c.ic)), t("mine.col_mine_t"): num(abs(c.t)),
                t("mine.col_pass"): t("mine.yes") if MN.passes(c, r) else t("mine.no"),
                t("mine.col_test_ic"): ic3(c.test.get("ic")), t("mine.col_held"): t("mine.yes") if c.held else t("mine.no"), t("mine.col_test_t"): num(c.test.get("t")),
                t("mine.col_corr"): f"{c.corr_with} {num(c.corr)}" if c.corr_with else "—"} for c in acc]))
            ui.label(t("mine.ic_note")).classes("sq-muted text-xs")
            for c in acc:
                with ui.expansion(c.name, icon="check_circle" if c.held else "help_outline").classes("w-full") \
                        .props("dense"):
                    if c.idea:
                        ui.label(t("mine.idea_label", idea=c.idea)).classes("text-sm")
                    ui.label(c.code).classes("sq-code w-full whitespace-pre text-xs overflow-x-auto")
                    saved = M["saved"].get(c.name)
                    btn = ui.button(t("mine.saved_btn") if saved else t("mine.save"), icon="save",
                                    on_click=lambda c=c: save(c)).props("outline no-caps").mark(f"mine_save:{c.name}")
                    btn.set_enabled(not saved)

        rows = [{t("mine.col_name"): b["name"], t("mine.col_mine_ic"): ic3(b["mine_ic"]),
                 t("mine.col_mine_t"): num(b["mine_t"]), t("mine.col_test_ic"): ic3(b["ic"]),
                 t("mine.col_test_t"): num(b["t"])} for b in r["bench"]]
        for key, label in (("mined", "mine.combo_mined"), ("existing", "mine.combo_existing")):
            cmb = r["combos"].get(key)
            if cmb:
                rows.append({t("mine.col_name"): t(label, n=cmb["factors"]), t("mine.col_mine_ic"): ic3(cmb["mine_ic"]),
                             t("mine.col_mine_t"): num(cmb["mine_t"]), t("mine.col_test_ic"): ic3(cmb["ic"]),
                             t("mine.col_test_t"): num(cmb["t"])})
        with ui.card().classes("w-full gap-2"):
            ui.label(t("mine.bench_title")).classes("font-semibold")
            df_table(pd.DataFrame(rows))
            ui.label(t("mine.bench_note")).classes("sq-muted text-xs")

    with ui.expansion(t("mine.all_title", n=len(miner.candidates)), icon="list").classes("w-full q-card"):
        df_table(pd.DataFrame([{
            t("mine.col_round"): c.round, t("mine.col_name"): c.name, t("mine.col_result"): MN.status_text(c.status, lg),
            t("mine.col_mine_ic"): ic3(c.ic), t("mine.col_mine_t"): num(c.t),
            t("mine.col_corr"): f"{c.corr_with} {num(c.corr)}" if c.corr_with else "—",
            t("mine.col_reason"): (c.reason.splitlines()[0] if c.reason else c.idea)} for c in miner.candidates]))             .props("wrap-cells")
    if miner.notes:
        with ui.expansion(t("mine.notes_title"), icon="psychology").classes("w-full q-card"):
            for i, n in enumerate(miner.notes, 1):
                ui.label(f"{i}. {n}").classes("text-sm")
