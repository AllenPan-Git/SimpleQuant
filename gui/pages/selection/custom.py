"""选股页「自定义因子」标签页"""

import pandas as pd
from nicegui import run, ui

from simplequant import telemetry
from simplequant import llm
from simplequant.stocks import analyze
from simplequant.stocks import custom_factors, universe as U
from gui.common import t, lang
from gui.components import metric_tile
from gui.widgets import plot, notice, code_editor
from ui.charts import quantile_chart, group_bar
from ui.shared import pct, num


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

    _custom_factor_ai(SP, lambda res: fill_from_ai(res))

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
            check_code()
        editor = code_editor(C["code"], on_code, height=360).mark("cf_code")
        err_label = ui.label().classes("text-negative text-sm")
        warn_label = ui.label().classes("text-warning text-sm whitespace-pre-line").mark("cf_warn")

        def check_code():
            errs = custom_factors.check_syntax(C["code"], lg)
            warns = [] if errs else custom_factors.review(C["code"], lg)     # 偷看未来、不存在的字段
            err_label.text = errs[0] if errs else ""
            err_label.set_visibility(bool(errs))
            warn_label.text = "\n".join(warns)
            warn_label.set_visibility(bool(warns))
        check_code()
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
        check_code()
        trial_box.refresh()

    def fill_from_ai(res):
        """AI 生成的代码填进编辑器（不运行）；「在现有代码基础上修改」时仍在编辑原来那个因子"""
        if not SP["cf_ai"]["base"]:
            C["key"] = None
        C.update(code=res.spec["code"], direction=res.spec["direction"], desc=res.spec["desc"] or C["desc"],
                 name=C["name"] if SP["cf_ai"]["base"] and C["name"] else res.name)
        SP["cf_trial"] = None
        name.value, direction.value, desc.value, editor.value = C["name"], C["direction"], C["desc"], C["code"]
        sync()
        check_code()
        trial_box.refresh()
        ui.notify(t("cf.ai_filled"), type="info")

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


def _custom_factor_ai(SP, fill):
    """用一句话描述因子 → AI 写 factor(p) 代码；只做静态检查，填进编辑器后由用户试算"""
    lg = lang()
    A = SP["cf_ai"]
    cfg = llm.load_config()
    ready = bool(cfg and cfg.ready)
    with ui.expansion(t("cf.ai"), icon="auto_awesome", value=bool(A["result"])).classes("w-full q-card"):
        if not ready:
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                notice(t("ai.need_config"), "key")
                ui.button(t("nav.go_settings"), on_click=lambda: ui.navigate.to("/settings")) \
                    .props("flat no-caps dense color=primary icon-right=arrow_forward")

        def placeholder():
            return t("cf.ai_base_placeholder") if A["base"] else t("cf.ai_placeholder")
        ai_text = ui.textarea(t("cf.ai_describe"), value=A["text"], placeholder=placeholder()) \
            .props("outlined autogrow").classes("w-full").mark("cf_ai_text")

        def on_base(e):
            A["base"] = bool(e.value)
            ai_text.props(f'placeholder="{placeholder()}"')

        async def ai_go():
            A["text"] = ai_text.value or ""
            ai_btn.disable()
            telemetry.ai("factor")
            try:
                A["result"] = await run.io_bound(llm.generate_factor, A["text"], llm.get_provider(cfg), lg,
                                                 U.kind(SP["universe"]),
                                                 base_code=SP["cf"]["code"] if A["base"] else None)
            except llm.LLMError as e:
                A["result"] = None
                ui.notify(str(e), type="negative", multi_line=True)
            except Exception as e:  # noqa: BLE001
                A["result"] = None
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                ai_btn.enable()
            ai_result.refresh()
        with ui.row().classes("items-center gap-4"):
            ai_btn = ui.button(t("cf.ai_generate"), icon="auto_awesome", on_click=ai_go).props("outline no-caps") \
                .mark("cf_ai_go")
            ui.checkbox(t("cf.ai_base"), value=A["base"], on_change=on_base).tooltip(t("cf.ai_base_help")) \
                .mark("cf_ai_base")
        ai_btn.set_enabled(ready and bool(A["text"].strip()))
        ai_text.on_value_change(lambda e: ai_btn.set_enabled(ready and bool((e.value or "").strip())))

        @ui.refreshable
        def ai_result():
            res = A.get("result")
            if not res:
                return
            notice(res.understood or "—", "psychology")
            if res.unsupported:
                notice(t("ai.unsupported") + "\n\n" + "\n".join(f"- {u}" for u in res.unsupported), "block",
                       "warning")
            if not res.spec["code"]:
                notice(t("cf.ai_empty"), "block", "warning")
                return
            for e in res.errors:
                notice(e, "error", "error")
            notice(t("cf.ai_review"), "visibility", "info")
            ui.button(t("cf.ai_apply"), icon="input", on_click=lambda: fill(res)).props("unelevated no-caps") \
                .classes("self-start").mark("cf_ai_apply")
        ai_result()
