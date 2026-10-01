"""首页：继续上次的工作 / 第一次使用时的介绍 + 模拟账户 + 开始新的工作 + 最近的报告"""

import datetime as dt
from html import escape

from nicegui import app, run, ui

from simplequant import migrate
from simplequant.data import library
from simplequant.paper import list_accounts
from simplequant.strategies import list_strategies
from gui import history, state
from gui.common import t
from gui.layout import frame
from ui.shared import pct, num


def page():
    with frame("/"):
        legacy = migrate.pending()
        if legacy:
            _migrate_card(legacy)
        metas = library.list_datasets()
        recent = history.recent()

        hour = dt.datetime.now().hour
        greet = "home.greet_morning" if hour < 12 else "home.greet_afternoon" if hour < 18 else "home.greet_evening"
        with ui.row().classes("w-full items-baseline justify-between pt-2"):
            ui.label(t(greet)).classes("sq-title")
            ui.label(t("home.status", d=max(m.end for m in metas)[:10], n=len(metas), s=len(list_strategies()))
                     if metas else t("home.status_empty")).classes("sq-muted text-sm")

        with ui.element("div").classes("sq-home-top w-full grid gap-10").style("grid-template-columns: minmax(0, 1fr) 320px"):
            if recent:
                _continue(recent[0])
            else:
                _hero(bool(metas))
            _paper_panel()

        _section(t("home.new_work"), t("home.new_work_note"))
        with ui.element("div").classes("sq-starts w-full grid").style("grid-template-columns: repeat(4, minmax(0, 1fr))"):
            starts = [("template", "/strategy"), ("ai", "/strategy"), ("code", "/strategy"), ("selection", "/selection")]
            for i, (key, route) in enumerate(starts, 1):
                with ui.column().classes("gap-0 cursor-pointer sq-start").on("click", lambda k=key, r=route: _start(k, r)) \
                        .mark(f"start:{key}"):
                    ui.label(f"{i:02d}").classes("sq-fig sq-red text-4xl leading-none")
                    ui.label(t(f"home.w_{key}")).classes("sq-serif text-lg mt-3 mb-1 sq-hover-title")
                    ui.label(t(f"home.w_{key}_body")).classes("text-sm").style("color: var(--sq-text2)")
                    ui.label(t(f"home.w_{key}_more") + " →").classes("sq-more mt-3")

        if recent:
            _section(t("home.recent"), t("home.recent_note"))
            with ui.column().classes("w-full gap-0"):
                for e in recent:
                    _recent_row(e)

        with ui.expansion(t("home.sources"), icon="help_outline").classes("w-full q-card mt-8"):
            ui.markdown(t("home.sources_table"))


def _migrate_card(src):
    """打包版第一次运行：从源码版目录导入数据"""
    from gui.widgets import Progress
    with ui.card().classes("w-full gap-3") as card:
        ui.label(t("mig.title")).classes("sq-h2")
        ui.label(t("mig.body", mb=f"{migrate.size_mb(src):.0f}")).classes("text-sm")
        path = ui.input(t("mig.dir"), value=str(src)).props("outlined dense").classes("w-full")
        prog = Progress()

        async def go():
            btns.set_visibility(False)
            prog.start()
            try:
                n = await run.io_bound(migrate.import_from, path.value.strip(),
                                       lambda i, k: prog.set(i / k, f"{i}/{k}"))
            except Exception as e:  # noqa: BLE001
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
                btns.set_visibility(True)
                return
            finally:
                prog.stop()
            from simplequant.stocks import custom_factors
            custom_factors.load_all()
            ui.notify(t("mig.done", n=n), type="positive")
            ui.navigate.reload()

        def skip():
            migrate.dismiss()
            card.delete()
        with ui.row().classes("gap-3") as btns:
            ui.button(t("mig.go"), icon="drive_file_move", on_click=go).props("unelevated no-caps").mark("mig_go")
            ui.button(t("mig.skip"), on_click=skip).props("flat no-caps")


def _section(title: str, note: str):
    with ui.element("div").classes("sq-sec"):
        ui.label(title).classes("sq-h2")
        ui.label(note).classes("sq-muted text-sm")


def _start(key: str, route: str):
    if key in ("template", "ai", "code"):
        state.strategy()["mode"] = key
    ui.navigate.to(route)


# ---------------- 继续上次的工作 / 第一次使用 ----------------
def _hero(has_data: bool):
    with ui.column().classes("gap-4 py-6"):
        ui.label(t("home.hero_eyebrow")).classes("sq-eyebrow")
        ui.html("<br>".join(escape(x) for x in t("home.hero_title").split("|"))) \
            .classes("sq-serif").style("font-size: 44px; line-height: 1.3")
        ui.label(t("home.hero_body")).classes("max-w-[34em]").style("color: var(--sq-text2)")
        with ui.row().classes("gap-3 pt-2"):
            if has_data:
                ui.button(t("home.start_first"), on_click=lambda: _start("template", "/strategy")) \
                    .props("unelevated no-caps color=primary").classes("px-5 py-2")
            else:
                ui.button(t("home.get_data_first"), on_click=lambda: ui.navigate.to("/data")) \
                    .props("unelevated no-caps color=primary").classes("px-5 py-2")
            ui.button(t("nav.go_data"), on_click=lambda: ui.navigate.to("/data")) \
                .props("outline no-caps").classes("px-5 py-2")


def _continue(e: dict):
    sel = e.get("kind") == "sel"
    with ui.column().classes("sq-sheet gap-0 px-8 py-6 min-w-0"):
        ui.label(t("home.continue")).classes("sq-eyebrow")
        ui.label(e["title"]).classes("sq-serif text-2xl mt-1 mb-5")
        ret = e.get("ret")
        cells = [(t("step.data"), e.get("data") or e.get("sub", ""), "ok"),
                 (t("step.selection" if sel else "step.strategy"), e.get("strategy") or e["title"], "ok"),
                 (t("step.backtest"), pct(ret) if ret is not None else "—", "ok"),
                 (t("home.suggest_next"), t("step.paper" if sel else "step.optimize"), "next")]
        if not sel:
            cells.append((t("step.paper"), "—", ""))
        with ui.element("div").classes("sq-pipe").style(f"grid-template-columns: repeat({len(cells)}, minmax(0, 1fr))"):
            for k, v, cls in cells:
                with ui.element("div").classes(cls):
                    ui.label(k).classes("k")
                    lbl = ui.label(v).classes("v")
                    if k == t("step.backtest") and ret is not None:
                        lbl.classes("sq-fig " + ("sq-up" if ret >= 0 else "sq-down"))
        with ui.row().classes("gap-3 pt-5"):
            ui.button(t("home.open_report"), on_click=lambda: reopen(e)).props("unelevated no-caps color=primary") \
                .classes("px-5").mark("open_report")
            if sel:
                ui.button(t("home.to_paper"), on_click=lambda: ui.navigate.to("/paper")).props("outline no-caps")
            else:
                ui.button(t("home.try_optimize"), on_click=lambda: (reopen(e, go=False), ui.navigate.to("/optimize"))) \
                    .props("outline no-caps")


def reopen(e: dict, go: bool = True):
    """恢复这份报告的设置；回到回测页时自动重新运行（通常不到 1 秒）"""
    r = e.get("restore")
    if e.get("kind") == "sel" or not r:
        if go:
            ui.navigate.to("/selection" if e.get("kind") == "sel" else "/backtest")
        return
    S = state.STATE
    same = S.get("bt", {}).get("time") == e["time"]
    S["current_spec"] = r["spec"]
    for key in ("bt", "opt"):
        S[f"{key}_strategy"] = "__current__"
        S[f"{key}_ids"] = list(r["ids"])
        S[f"{key}_range"] = tuple(dt.date.fromisoformat(x) for x in r["range"])
    S["bt_broker"] = dict(r["broker"])
    if not same:
        S["bt_autorun"] = e["time"]
    if go:
        ui.navigate.to("/backtest")


# ---------------- 模拟账户 ----------------
def _spark(values: list[float]) -> str:
    """净值小图（内联 SVG，颜色跟随主题）"""
    if len(values) < 2:
        return ""
    w, h = 300, 110
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = [(i * w / (len(values) - 1), 6 + (hi - v) * (h - 12) / span) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{h} " + line + f" {w},{h}"
    return (f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' style='width:100%;height:{h}px;display:block'>"
            f"<polygon points='{area}' style='fill: var(--sq-red-soft)'/>"
            f"<polyline points='{line}' style='fill:none; stroke: var(--sq-red); stroke-width: 1.6' "
            f"vector-effect='non-scaling-stroke'/></svg>")


def _paper_panel():
    with ui.column().classes("gap-0 sq-rule-top pt-3 min-w-0"):
        try:
            accounts = [a for a in list_accounts() if a.status == "active"] or list_accounts()
        except Exception:  # noqa: BLE001
            accounts = []
        if not accounts:
            ui.label(t("nav.paper").split(". ", 1)[-1]).classes("sq-muted text-sm")
            ui.label(t("home.paper_none")).classes("text-sm py-3").style("color: var(--sq-text2)")
            ui.label(t("home.paper_create")).classes("sq-red text-sm cursor-pointer") \
                .on("click", lambda: ui.navigate.to("/paper"))
            return
        acc = accounts[0]
        ui.label(t("home.paper_title", name=acc.name)).classes("sq-muted text-sm")
        st, nav = acc.state(), acc.nav()
        if st and "value" in st:
            ret = st["value"] / acc.broker["cash"] - 1
            with ui.row().classes("items-baseline gap-2 no-wrap"):
                ui.label(num(st["value"], 0)).classes("sq-fig").style("font-size: 32px")
                ui.label(("+" if ret >= 0 else "") + pct(ret)).classes("sq-fig " + ("sq-up" if ret >= 0 else "sq-down"))
            ui.html(_spark([float(v) for v in nav["value"].tolist()[-250:]])).classes("w-full my-2")
            through = t("home.paper_through", d=st.get("as_of", acc.data_through or "—"))
        else:
            ui.label("—").classes("sq-fig").style("font-size: 32px")
            through = t("home.paper_waiting")
        with ui.row().classes("w-full justify-between items-baseline sq-rule-top pt-2 mt-1 no-wrap") \
                .style("border-top-color: var(--sq-border)"):
            ui.label(through + (" · " + t("home.paper_more", n=len(accounts) - 1) if len(accounts) > 1 else "")) \
                .classes("text-sm").style("color: var(--sq-text2)")
            ui.label(t("home.paper_view")).classes("sq-red text-sm cursor-pointer whitespace-nowrap") \
                .on("click", lambda: ui.navigate.to("/paper"))


# ---------------- 最近的报告 ----------------
def _recent_row(e: dict):
    ret, sharpe = e.get("ret"), e.get("sharpe")
    with ui.element("div").classes("sq-recent-row w-full grid items-baseline py-4 sq-rule-bottom sq-hover-row") \
            .style("grid-template-columns: minmax(0, 1fr) 150px 110px 110px").on("click", lambda: reopen(e)) \
            .mark("recent"):
        with ui.column().classes("gap-0 min-w-0"):
            ui.label(e["title"]).classes("sq-serif text-base sq-hover-title")
            ui.label(e.get("sub", "")).classes("sq-muted text-xs")
        ui.label(e.get("time", "")).classes("sq-muted text-xs")
        ui.label(t("home.sharpe", v=num(sharpe)) if sharpe is not None else "").classes("sq-muted text-xs")
        ui.label(pct(ret, 1) if ret is not None else "—") \
            .classes("sq-fig text-xl text-right " + ("" if ret is None else "sq-up" if ret >= 0 else "sq-down"))
