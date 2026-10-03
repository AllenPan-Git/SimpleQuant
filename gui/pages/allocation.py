"""第 7 页：资产配置（风险测评 → 配置候选 → 参考配置与组合回测）"""

import uuid

import pandas as pd
from nicegui import run, ui

from simplequant import allocation as A, strategies
from simplequant.data import library
from simplequant.strategies import TEMPLATES
from gui import state
from gui.common import t, p, lang
from gui.components import credibility_box
from gui.layout import frame, page_title
from gui.widgets import section, plot, notice
from ui.charts import equity_chart, corr_heatmap
from ui.shared import pct, num

DENSE = "outlined dense options-dense"
GRID = "display: grid; align-items: center; column-gap: 12px; row-gap: 6px; min-width: 760px"


def page():
    with frame("/allocation"):
        page_title("/allocation", t("al.intro"))
        AL = state.STATE.setdefault("al", {"answers": {}})
        ui.label(t("al.disclaimer")).classes("sq-muted text-xs")

        @ui.refreshable
        def quiz():
            _quiz(AL, lambda: redraw())
        quiz()

        @ui.refreshable
        def sleeves():
            _sleeves(AL, lambda: redraw(), lambda: do_run())
        sleeves()

        running = ui.row().classes("items-center gap-2")
        with running:
            ui.spinner(size="sm")
            ui.label(t("al.running")).classes("sq-muted text-sm")
        running.set_visibility(False)

        @ui.refreshable
        def results():
            if AL.get("result"):
                _results(AL, lambda w: rerun(w))
        results()

        def redraw():
            quiz.refresh()
            sleeves.refresh()
            results.refresh()

        async def do_run():
            prof, cfg = A.load_profile(), A.load_config()
            if prof is None:
                ui.notify(t("al.no_profile"), type="warning")
                return
            chosen = [s for s in A.all_sleeves(cfg) if s["id"] in cfg["enabled"]]
            if not chosen:
                ui.notify(t("al.need_sleeves"), type="warning")
                return
            running.set_visibility(True)
            try:
                R, errors = await run.io_bound(A.load_returns, chosen)
                used = [s for s in chosen if s["id"] in R.columns]
                sug = await run.io_bound(A.suggest, prof.level, prof.max_dd, used, R, cfg["rebalance"])
                AL["result"] = dict(R=R, sleeves=used, errors=errors, profile=prof, rebalance=cfg["rebalance"],
                                    suggestion=sug, weights=dict(sug["weights"]), res=sug["result"],
                                    shifted=sug["shifted"], manual=False)
            except Exception as e:  # noqa: BLE001
                AL.pop("result", None)
                ui.notify(t("bt.failed", e=e), type="negative", multi_line=True)
            finally:
                running.set_visibility(False)
            results.refresh()

        async def rerun(weights: dict | None):
            """weights 为 None 时恢复参考配置"""
            r = AL["result"]
            if weights is None:
                r.update(weights=dict(r["suggestion"]["weights"]), res=r["suggestion"]["result"],
                         shifted=r["suggestion"]["shifted"], manual=False)
            else:
                if sum(weights.values()) <= 0:
                    ui.notify(t("al.need_sleeves"), type="warning")
                    return
                res = await run.io_bound(A.backtest, r["R"], weights, r["rebalance"])
                r.update(weights=weights, res=res, shifted=0.0, manual=True)
            results.refresh()


# ---------------- 风险测评 ----------------
def _quiz(AL, redraw):
    prof = A.load_profile()
    answers = AL["answers"]
    if prof and not answers:
        answers.update(prof.answers)
    with ui.expansion(t("al.quiz"), icon="assignment", value=prof is None or AL.get("quiz_open", False)) \
            .classes("w-full q-card").mark("al_quiz") as exp:
        if prof:
            with exp.add_slot("header"):
                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    ui.icon("assignment")
                    ui.label(t("al.quiz")).classes("font-medium")
                    ui.label(f"C{prof.level} {p(A.LEVELS[prof.level])}").classes("sq-muted text-sm")
        exp.on_value_change(lambda e: AL.__setitem__("quiz_open", e.value))
        with ui.column().classes("w-full gap-4 pt-2"):
            ui.label(t("al.quiz_help", n=len(A.QUESTIONS))).classes("sq-muted text-sm")
            for i, q in enumerate(A.QUESTIONS, 1):
                with ui.column().classes("w-full gap-1"):
                    ui.label(f"{i}. {p(q['text'])}").classes("font-medium")
                    ui.radio({j: p(o["text"]) for j, o in enumerate(q["options"])}, value=answers.get(q["key"]),
                             on_change=lambda e, k=q["key"]: answers.__setitem__(k, e.value)) \
                        .props("inline dense").mark(f"al_q:{q['key']}")

            def submit():
                left = sum(1 for q in A.QUESTIONS if answers.get(q["key"]) is None)
                if left:
                    ui.notify(t("al.quiz_incomplete", n=left), type="warning")
                    return
                A.save_profile(A.evaluate(answers))
                AL["quiz_open"] = False
                AL.pop("result", None)
                redraw()
            ui.button(t("al.quiz_submit"), icon="check", on_click=submit).props("unelevated no-caps") \
                .mark("al_submit")
    if prof:
        _profile_card(prof)


def _profile_card(prof):
    with ui.column().classes("w-full gap-1 sq-note py-3 px-4").mark("al_profile"):
        ui.label(t("al.level_title", c=f"C{prof.level}", name=p(A.LEVELS[prof.level]))).classes("sq-serif text-lg")
        ui.label(p(A.LEVEL_DESC[prof.level])).classes("text-sm")
        ui.label(t("al.level_detail", score=prof.score, min=A.MIN_SCORE, max=A.MAX_SCORE,
                   dd=pct(prof.max_dd, 0), years=prof.years, time=prof.time)).classes("sq-muted text-xs")
        if prof.score_level != prof.level:
            ui.label(t("al.level_capped", s=f"C{prof.score_level}", c=f"C{prof.level}")).classes("sq-muted text-xs")


# ---------------- 配置候选 ----------------
def _data_label(s: dict) -> str:
    if s["kind"] in ("cash", "selection"):
        return t("al.data_none") if s["kind"] == "cash" else "—"
    m = A.find_dataset(s["symbol"])
    return t("al.data_local", start=m.start, end=m.end) if m else t("al.data_missing")


def _sleeves(AL, redraw, do_run):
    cfg = A.load_config()
    items = A.all_sleeves(cfg)
    custom_ids = {s["id"] for s in cfg["custom"]}
    with section(t("al.sleeves"), "category"):
        ui.label(t("al.sleeves_help")).classes("sq-muted text-xs")
        with ui.element("div").classes("w-full overflow-x-auto"):
            with ui.element("div").style(GRID + "; grid-template-columns: 40px 2fr 1.4fr 1.2fr 1.6fr 60px"):
                for k in ("", "name", "class", "kind", "data", ""):
                    ui.label(t(f"al.col_{k}") if k else "").classes("sq-muted text-xs")
                for s in items:
                    def toggle(e, sid=s["id"]):
                        c = A.load_config()
                        on = set(c["enabled"])
                        (on.add if e.value else on.discard)(sid)
                        c["enabled"] = [x["id"] for x in A.all_sleeves(c) if x["id"] in on]
                        A.save_config(c)
                    ui.checkbox(value=s["id"] in cfg["enabled"], on_change=toggle).props("dense") \
                        .mark(f"al_use:{s['id']}")
                    ui.label(p(s["name"])).classes("text-sm")
                    ui.label(p(A.CLASSES[s["class"]])).classes("text-sm")
                    ui.label(p(A.KINDS[s["kind"]]) + (f" · {s['strategy']}" if s.get("strategy") else "")) \
                        .classes("text-sm")
                    ui.label(_data_label(s)).classes("text-sm sq-num")
                    if s["id"] in custom_ids:
                        def delete(sid=s["id"]):
                            c = A.load_config()
                            c["custom"] = [x for x in c["custom"] if x["id"] != sid]
                            c["enabled"] = [x for x in c["enabled"] if x != sid]
                            A.save_config(c)
                            redraw()
                        ui.button(icon="delete", on_click=delete).props("flat dense round size=sm") \
                            .tooltip(t("al.delete"))
                    else:
                        ui.label("")

        missing = A.missing_data([s for s in items if s["id"] in cfg["enabled"]])
        if missing:
            log = ui.column().classes("w-full gap-1")

            async def download():
                btn.disable()
                end = pd.Timestamp.today().date().isoformat()
                names = {s["symbol"]: p(s["name"]) for s in items if s.get("symbol")}
                failed = False
                for sym in missing:
                    with log:
                        row = ui.label(t("al.downloading", sym=sym)).classes("sq-muted text-sm")
                    try:
                        await run.io_bound(A.download, sym, end, name=f"{sym} {names.get(sym, '')}".strip())
                        row.delete()
                    except Exception as e:  # noqa: BLE001
                        row.text = t("al.download_failed", sym=sym, e=e)
                        row.classes(replace="text-negative text-sm")
                        failed = True
                btn.enable()
                if not failed:
                    redraw()
            btn = ui.button(t("al.download", n=len(missing)), icon="download", on_click=download) \
                .props("outline no-caps").classes("self-start").mark("al_download")

        _add_form(redraw)

        with ui.row().classes("w-full items-center gap-4"):
            def on_reb(e):
                c = A.load_config()
                c["rebalance"] = e.value
                A.save_config(c)
            ui.select({r: t(f"al.reb_{r}") for r in A.REBALANCE}, label=t("al.rebalance"), value=cfg["rebalance"],
                      on_change=on_reb).props(DENSE).classes("w-56").tooltip(t("al.rebalance_help")) \
                .mark("al_rebalance")
            ui.button(t("al.run"), icon="play_arrow", on_click=do_run).props("unelevated no-caps").mark("al_run")


def _add_form(redraw):
    F = {"kind": "hold", "class": "equity", "symbol": None, "strategy": None, "name": ""}
    with ui.expansion(t("al.add"), icon="add").classes("w-full").mark("al_add"):
        box = ui.column().classes("w-full gap-3")

        def draw():
            box.clear()
            saved = strategies.list_strategies()
            with box:
                with ui.row().classes("w-full items-center gap-3"):
                    ui.select({k: p(v) for k, v in A.KINDS.items() if k != "cash"}, label=t("al.add_kind"),
                              value=F["kind"], on_change=lambda e: (F.update(kind=e.value, strategy=None), draw())) \
                        .props(DENSE).classes("w-48").mark("al_add_kind")
                    ui.select({k: p(v) for k, v in A.CLASSES.items()}, label=t("al.col_class"), value=F["class"],
                              on_change=lambda e: F.update({"class": e.value})).props(DENSE).classes("w-56") \
                        .mark("al_add_class")
                if F["kind"] in ("hold", "timing"):
                    metas = {}
                    for m in library.list_datasets():
                        if m.freq == "1d" and m.symbol not in metas:
                            metas[m.symbol] = m.name
                    ui.select(metas, label=t("al.add_symbol"), value=F["symbol"],
                              on_change=lambda e: F.update(symbol=e.value)).props(DENSE).classes("w-full") \
                        .mark("al_add_symbol")
                if F["kind"] == "timing":
                    opts = {n: n for n, s in saved.items() if strategies.runnable_on_single_assets(s)}
                    opts.update({f"tpl:{k}": t("bt.opt_template", name=p(v.label)) for k, v in TEMPLATES.items()})
                    ui.select(opts, label=t("al.add_strategy"), value=F["strategy"],
                              on_change=lambda e: F.update(strategy=e.value)).props(DENSE).classes("w-full") \
                        .mark("al_add_strategy")
                if F["kind"] == "selection":
                    opts = [n for n, s in saved.items() if s.get("kind") == "selection"]
                    if not opts:
                        notice(t("al.no_selection_strategy"), "info")
                    ui.select(opts, label=t("al.add_strategy"), value=F["strategy"],
                              on_change=lambda e: F.update(strategy=e.value)).props(DENSE).classes("w-full") \
                        .mark("al_add_strategy")
                ui.input(t("al.add_name"), value=F["name"], on_change=lambda e: F.update(name=e.value or "")) \
                    .props("outlined dense").classes("w-full").mark("al_add_name")
                ui.button(t("al.add_btn"), icon="add", on_click=add).props("outline no-caps").mark("al_add_btn")

        def add():
            need_symbol = F["kind"] in ("hold", "timing")
            need_strategy = F["kind"] in ("timing", "selection")
            if (need_symbol and not F["symbol"]) or (need_strategy and not F["strategy"]):
                ui.notify(t("al.add_need"), type="warning")
                return
            sleeve = {"id": f"custom_{uuid.uuid4().hex[:8]}", "class": F["class"], "kind": F["kind"]}
            if need_symbol:
                sleeve["symbol"] = F["symbol"]
            if need_strategy:
                sleeve["strategy"] = F["strategy"]
            auto = " · ".join(x for x in (F["symbol"] if need_symbol else "",
                                          (F["strategy"] or "").replace("tpl:", "") if need_strategy else "") if x)
            sleeve["name"] = F["name"].strip() or auto
            c = A.load_config()
            c["custom"].append(sleeve)
            c["enabled"].append(sleeve["id"])
            A.save_config(c)
            redraw()
        draw()


# ---------------- 结果 ----------------
def _results(AL, rerun):
    lg = lang()
    r = AL["result"]
    prof, res, R = r["profile"], r["res"], r["R"]
    names = {s["id"]: p(s["name"]) for s in r["sleeves"]}
    classes = {s["id"]: s["class"] for s in r["sleeves"]}
    m = res.metrics
    level = dict(c=f"C{prof.level}", name=p(A.LEVELS[prof.level]))

    with ui.element("div").classes("sq-sec"):
        ui.label(t("al.result")).classes("sq-h2")
    ui.label(t("al.result_caption", **level, start=f"{R.index[0]:%Y-%m-%d}", end=f"{R.index[-1]:%Y-%m-%d}",
               reb=t(f"al.reb_{r['rebalance']}"))).classes("sq-muted text-sm")
    for sid, e in r["errors"].items():
        name = next((p(s["name"]) for s in A.all_sleeves(A.load_config()) if s["id"] == sid), sid)
        notice(t("al.sleeve_failed", name=name, e=e), "warning", "warning")
    bench_cagr = A.risk_stats(res.equity["benchmark"].pct_change().dropna())["cagr"]
    args = dict(cagr=pct(m["cagr"]), vol=pct(m["volatility"]), dd=pct(m["max_drawdown"]), limit=pct(-prof.max_dd, 0),
                bench=pct(bench_cagr))
    ui.html(t("al.lede_manual", **args) if r["manual"] else t("al.lede", **level, **args)).classes("sq-lede my-3")

    cells = [(t("m.cagr"), pct(m["cagr"]), "sq-up" if m["cagr"] >= 0 else "sq-down"),
             (t("m.volatility"), pct(m["volatility"]), ""),
             (t("m.max_drawdown"), pct(m["max_drawdown"]), "sq-down"),
             (t("m.sharpe"), num(m["sharpe"]), ""),
             (t("al.port_risk"), f"R{res.stats['risk']}", ""),
             (t("al.rebalances"), str(res.n_rebalances), "")]
    with ui.element("div").classes("sq-figs").style(f"grid-template-columns: repeat({len(cells)}, minmax(0, 1fr))"):
        for k, v, cls in cells:
            with ui.element("div"):
                ui.label(k).classes("k")
                ui.label(v).classes("v " + cls)
    mix = {}
    for sid, w in r["weights"].items():
        mix[classes[sid]] = mix.get(classes[sid], 0) + w
    tot = sum(mix.values()) or 1
    sep = "，" if lg == "zh" else ", "
    ui.label(t("al.class_mix", mix=sep.join(f"{p(A.CLASSES[c])} {pct(v / tot, 0)}" for c, v in mix.items() if v > 0))) \
        .classes("text-sm pt-2")

    with ui.column().classes("w-full pt-4"):
        credibility_box(A.checks(prof.level, prof.max_dd, res, r["shifted"], names))

    with ui.column().classes("w-full gap-1 pt-4"):
        plot(equity_chart(res.equity, lg, t("al.chart_bench"), t("al.chart_portfolio")))
        ui.label(t("al.fig_caption")).classes("sq-caption")

    _weights_table(r, names, rerun)

    corr = R.loc[:, R.std() > 1e-9].corr()
    if corr.shape[0] > 1:
        ui.label(t("al.corr_title")).classes("font-semibold pt-4")
        plot(corr_heatmap(corr, names, lg))


def _weights_table(r, names, rerun):
    res, R = r["res"], r["R"]
    stats = {row["id"]: row for row in A.sleeve_table(R).to_dict("records")}
    contrib = {row["id"]: row for row in res.contrib.to_dict("records")}
    total = sum(r["weights"].values()) or 1
    edit = {sid: round(w / total * 100, 1) for sid, w in r["weights"].items()}
    ui.label(t("al.weights_title")).classes("font-semibold pt-4")
    ui.label(t("al.weights_help")).classes("sq-muted text-xs")
    cols = ["name", "class", "risk", "weight", "cagr", "volatility", "max_drawdown", "return_contrib", "risk_contrib"]
    with ui.element("div").classes("w-full overflow-x-auto"):
        with ui.element("div").style(GRID + "; grid-template-columns: 2fr 1.2fr 0.7fr 110px repeat(5, 1fr)") \
                .mark("al_weights"):
            for c in cols:
                ui.label(t(f"m.{c}") if c in ("cagr", "volatility", "max_drawdown") else t(f"al.col_{c}")) \
                    .classes("sq-muted text-xs")
            for s in r["sleeves"]:
                sid, st, ct = s["id"], stats[s["id"]], contrib.get(s["id"], {})
                ui.label(names[sid]).classes("text-sm")
                ui.label(p(A.CLASSES[s["class"]])).classes("text-sm")
                ui.label(f"R{st['risk']}").classes("text-sm sq-num")
                ui.number(value=edit.get(sid, 0.0), min=0, max=100, step=1, format="%.1f",
                          on_change=lambda e, sid=sid: edit.__setitem__(sid, e.value or 0.0)) \
                    .props("outlined dense").classes("w-24").mark(f"al_w:{sid}")
                for v in (st["cagr"], st["volatility"], st["max_drawdown"],
                          ct.get("return_contrib"), ct.get("risk_contrib")):
                    ui.label(pct(v) if v is not None else "—").classes("text-sm sq-num")
    with ui.row().classes("gap-3 pt-2"):
        ui.button(t("al.rerun"), icon="refresh", on_click=lambda: rerun({k: v / 100 for k, v in edit.items()})) \
            .props("outline no-caps").mark("al_rerun")
        if r["manual"]:
            ui.button(t("al.reset"), icon="undo", on_click=lambda: rerun(None)).props("flat no-caps") \
                .mark("al_reset")
