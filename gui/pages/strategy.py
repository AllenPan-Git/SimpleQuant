"""第 2 页：策略（模板调参 / 条件积木 / 一句话生成 / 自己写代码）"""

from nicegui import run, ui

from simplequant import llm, strategies
from simplequant.data import library
from simplequant.rules import INDICATORS, GROUPS, OPS, CROSS_OPS, LOGICS, validate, make_ind
from simplequant.strategies import TEMPLATES, code_strategy
from gui import state
from gui.common import t, p, lang
from gui.layout import frame, page_title
from gui.widgets import section, code_editor
from ui.shared import PRESETS, cond, clean, with_ids

GROUP_ORDER = list(GROUPS)
IND_ORDER = sorted(INDICATORS, key=lambda k: GROUP_ORDER.index(INDICATORS[k]["group"]))
AI_EXAMPLES = ["ai.ex1", "ai.ex2", "ai.ex3", "ai.ex4"]
DENSE = "outlined dense options-dense"


class Ctx:
    """编辑器各部分之间的回调"""
    changed = reload = set_name = staticmethod(lambda *a: None)


def page():
    S = state.strategy()
    ctx = Ctx()
    with frame("/strategy"):
        page_title("/strategy")

        def set_mode(e):
            if S["mode"] != e.value:
                S["mode"] = e.value
                editor.refresh()
                changed()
        mode_toggle = ui.toggle({m: t(f"strat.mode_{m}") for m in ("template", "rule", "ai", "code")}, value=S["mode"],
                                on_change=set_mode).props("no-caps unelevated rounded toggle-color=primary")             .mark("mode")

        @ui.refreshable
        def editor():
            {"template": _template_editor, "rule": _rule_editor, "ai": _ai_editor, "code": _code_editor}[S["mode"]](S, ctx)

        def reload():
            """整体换了内容（载入示例 / 已保存的策略 / AI 结果转积木）"""
            mode_toggle.value = S["mode"]
            editor.refresh()
            changed()
        ctx.reload = reload

        editor()

        # ---------------- 预览 / 保存 ----------------
        with section(t("strat.preview"), "visibility"):
            preview = ui.column().classes("w-full gap-1")
            code_btn = ui.button(t("strat.to_code"), icon="code", on_click=lambda: to_code())                 .props("flat no-caps dense color=primary").classes("self-start").tooltip(t("strat.to_code_help"))
            with ui.row().classes("w-full items-end gap-3"):
                name = ui.input(t("strat.name"), value=S["name"], placeholder=t("strat.name_ph")) \
                    .props("outlined dense").classes("grow")
                save_btn = ui.button(t("strat.save"), icon="save", on_click=lambda: save()).props("outline no-caps")
                go_btn = ui.button(t("strat.to_backtest"), icon="play_arrow", on_click=lambda: to_backtest()) \
                    .props("unelevated no-caps")

        def current():
            """(策略描述, 错误列表)"""
            lg = lang()
            if S["mode"] == "code":
                spec = {"kind": "code", "code": S["code"]}
                return spec, code_strategy.check_syntax(S["code"], lg)
            if S["mode"] == "template":
                key = S["tpl_key"] or next(iter(TEMPLATES))
                spec = {"kind": "template", "template": key, "params": _tpl_values(S, key)}
                return spec, []
            if S["mode"] == "ai":
                res = S["ai_result"]
                if not res:
                    return {"kind": "rule", "rule": {"buy": {"conditions": []}}}, [t("ai.no_result")]
                return res.spec, validate(res.spec["rule"], lg)
            spec = {"kind": "rule", "rule": clean(S["rule"])}
            return spec, validate(spec["rule"], lg)

        def changed():
            spec, errors = current()
            preview.clear()
            with preview:
                if S["mode"] == "ai" and not S["ai_result"]:
                    ui.label(errors[0]).classes("sq-muted text-sm")
                elif errors:
                    for e in errors:
                        with ui.row().classes("items-center gap-2 text-negative no-wrap"):
                            ui.icon("error_outline")
                            ui.label(e)
                else:
                    ui.label(strategies.describe(spec, lang())).classes("sq-code w-full")
            code_btn.set_visibility(S["mode"] in ("template", "rule") and not errors)
            save_btn.set_enabled(not errors and bool((name.value or "").strip()))
            go_btn.set_enabled(not errors)

        def on_name(e):
            S["name"] = e.value or ""
            changed()
        name.on_value_change(on_name)
        ctx.changed, ctx.set_name = changed, name.set_value

        def full_check() -> bool:
            """代码策略保存 / 回测前真正执行一次定义部分，确认 Strategy 类可用"""
            if S["mode"] != "code":
                return True
            errors = code_strategy.check(S["code"], lang())
            if errors:
                ui.notify(errors[0], type="negative", multi_line=True)
            return not errors

        def to_code():
            spec, errors = current()
            if errors:
                return
            S["code"] = code_strategy.to_code(spec, lang())
            S["mode"] = "code"
            reload()

        def save():
            spec, errors = current()
            if errors or not S["name"].strip() or not full_check():
                return
            strategies.save_strategy(S["name"].strip(), spec)
            ui.notify(t("strat.saved", name=S["name"].strip()), type="positive")
            saved_list.refresh()

        def to_backtest():
            spec, errors = current()
            if errors or not full_check():
                return
            state.STATE["current_spec"] = {"name": S["name"].strip() or t("strat.unnamed"), **spec}
            state.STATE["bt_strategy"] = "__current__"
            # AI 识别出的标的若已在本地数据库里，回测页自动选中（在条件积木里修改过也保留）
            if S["mode"] in ("ai", "rule") and S["ai_symbols"]:
                ids = [m.id for m in library.list_datasets() if m.symbol in S["ai_symbols"]]
                if ids:
                    state.STATE["bt_ids"] = ids
            ui.navigate.to("/backtest")

        # ---------------- 已保存的策略 ----------------
        @ui.refreshable
        def saved_list():
            saved = {n: s for n, s in strategies.list_strategies().items() if strategies.runnable_on_single_assets(s)}
            if not saved:
                return
            with ui.expansion(t("strat.saved_list", n=len(saved)), icon="folder_open").classes("w-full q-card"):
                for n, s in saved.items():
                    with ui.row().classes("w-full items-center gap-3 no-wrap py-2"):
                        with ui.column().classes("grow gap-0"):
                            ui.label(n).classes("font-semibold")
                            ui.label(strategies.describe(s, lang())).classes("sq-muted text-sm whitespace-pre-wrap")
                        ui.button(t("strat.load"), on_click=lambda n=n: load_saved(n)).props("flat no-caps dense")
                        ui.button(t("strat.delete"), on_click=lambda n=n: delete_saved(n)) \
                            .props("flat no-caps dense color=negative")

        def load_saved(n):
            spec = strategies.list_strategies()[n]
            S["name"] = n
            name.value = n
            S["ai_symbols"] = None
            if spec["kind"] == "code":
                S["mode"] = "code"
                S["code"] = code_strategy.materialize(spec)
            elif spec["kind"] == "rule":
                S["mode"] = "rule"
                S["rule"] = with_ids(spec["rule"])
                S["rule"].setdefault("position_pct", 95)
            else:
                S["mode"] = "template"
                S["tpl_key"] = spec["template"]
                S["tpl_params"][spec["template"]] = dict(spec.get("params") or {})
            reload()

        def delete_saved(n):
            strategies.delete_strategy(n)
            saved_list.refresh()

        saved_list()
        changed()


# ---------------- 模板 ----------------
def _tpl_values(S, key) -> dict:
    tpl = TEMPLATES[key]
    vals = S["tpl_params"].setdefault(key, {})
    out = {}
    for prm in tpl.params:
        cast = int if prm.is_int else float
        out[prm.name] = cast(vals.get(prm.name, prm.default))
    return out


def _template_editor(S, ctx):
    key = S["tpl_key"] or next(iter(TEMPLATES))
    tpl = TEMPLATES[key]
    with ui.card().classes("w-full gap-3"):
        def pick(e):
            S["tpl_key"] = e.value
            ctx.reload()
        ui.select({k: p(v.label) for k, v in TEMPLATES.items()}, label=t("strat.pick_template"), value=key,
                  on_change=pick).props(DENSE).classes("w-full md:w-1/2")
        with ui.row().classes("w-full items-center gap-2 no-wrap sq-note py-2"):
            ui.icon("info").classes("text-primary")
            ui.label(p(tpl.description)).classes("text-sm")
        vals = _tpl_values(S, key)
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            for prm in tpl.params:
                def on_val(e, prm=prm):
                    if e.value is not None:
                        S["tpl_params"][key][prm.name] = (int if prm.is_int else float)(e.value)
                        ctx.changed()
                num = ui.number(p(prm.label), value=vals[prm.name], min=prm.min, max=prm.max, step=prm.step,
                                precision=0 if prm.is_int else None, on_change=on_val).props("outlined dense")
                if prm.help:
                    num.tooltip(p(prm.help))


# ---------------- 条件积木 ----------------
def ind_label(k: str) -> str:
    m = INDICATORS[k]
    return f"{p(GROUPS[m['group']])} · {p(m['label'])}"


def _normalize(spec: dict, options: list) -> dict:
    """补齐参数和线；指标不在可选范围内时换成第一个"""
    ind = spec.get("ind") if spec.get("ind") in options else options[0]
    if ind != spec.get("ind"):
        spec.clear()
        spec.update(make_ind(ind))
    meta = INDICATORS[ind]
    spec["params"] = {n: (spec.get("params") or {}).get(n, d) for n, _, d in meta["params"]}
    lines = meta.get("lines")
    if lines and spec.get("line") not in lines:
        spec["line"] = next(iter(lines))
    return spec


def ind_editor(spec: dict, allow_position: bool, changed, restructure):
    """编辑一个指标（原地修改 spec）；换指标时调用 restructure() 重画所在条件"""
    options = [k for k in IND_ORDER if allow_position or not INDICATORS[k].get("position")]
    _normalize(spec, options)
    ind = spec["ind"]
    meta = INDICATORS[ind]

    def on_ind(e):
        spec.clear()
        spec.update(make_ind(e.value))
        restructure()
        changed()
    sel = ui.select({k: ind_label(k) for k in options}, value=ind, with_input=True, on_change=on_ind) \
        .props(DENSE).classes("w-full")
    if meta.get("desc"):
        sel.tooltip(p(meta["desc"]))

    lines, params = meta.get("lines"), meta["params"]
    if lines or params:
        with ui.row().classes("w-full gap-2 no-wrap"):
            if lines:
                def on_line(e):
                    spec["line"] = e.value
                    changed()
                ui.select({n: p(v) for n, v in lines.items()}, label=t("strat.line"), value=spec["line"],
                          on_change=on_line).props(DENSE).classes("grow min-w-[90px]")
            for pname, label, default in params:
                is_float = isinstance(default, float)

                def on_param(e, pname=pname, is_float=is_float):
                    if e.value is not None:
                        spec["params"][pname] = float(e.value) if is_float else int(e.value)
                        changed()
                ui.number(p(label), value=spec["params"][pname], min=0.1 if is_float else 1,
                          step=0.1 if is_float else 1, precision=None if is_float else 0, on_change=on_param) \
                    .props("outlined dense").classes("grow min-w-[80px]")


def cond_editor(side_block: dict, c: dict, changed, redraw_side):
    card = ui.card().classes("w-full p-3").props("flat")

    def draw():
        card.clear()
        with card, ui.row().classes("w-full items-start gap-3 no-wrap"):
            with ui.column().classes("gap-2").style("flex: 5"):
                ind_editor(c["left"], True, changed, draw)
            position = INDICATORS[c["left"]["ind"]].get("position")
            ops = [o for o in OPS if not (position and o in CROSS_OPS)]
            if c["op"] not in ops:
                c["op"] = ops[0]

            def on_op(e):
                c["op"] = e.value
                changed()
            ui.select({o: p(OPS[o]) for o in ops}, value=c["op"], on_change=on_op).props(DENSE) \
                .classes("w-32").mark("op")

            with ui.column().classes("gap-2").style("flex: 5"):
                is_value = "value" in c["right"]

                def on_kind(e):
                    c["right"] = {"value": 0.0} if e.value == "value" else make_ind("sma")
                    draw()
                    changed()
                ui.toggle({"ind": t("strat.right_ind"), "value": t("strat.right_value")},
                          value="value" if is_value else "ind", on_change=on_kind) \
                    .props("dense no-caps unelevated size=sm toggle-color=primary")
                if is_value:
                    def on_value(e):
                        if e.value is not None:
                            c["right"]["value"] = float(e.value)
                            changed()
                    ui.number(value=float(c["right"].get("value", 0)), on_change=on_value) \
                        .props("outlined dense").classes("w-full")
                else:
                    ind_editor(c["right"], False, changed, draw)

            def delete():
                side_block["conditions"] = [x for x in side_block["conditions"] if x["_id"] != c["_id"]]
                redraw_side()
                changed()
            ui.button(icon="delete_outline", on_click=delete).props("flat round dense color=grey") \
                .tooltip(t("strat.delete_cond")).mark(f"del:{c['_id']}")
    draw()


def side_editor(S, side: str, title: str, icon: str, changed):
    box = ui.column().classes("w-full gap-3")

    def draw():
        block = S["rule"][side]
        block.setdefault("logic", "all")
        box.clear()
        with box:
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-2"):
                    with ui.element("div").classes("sq-icon-chip"):
                        ui.icon(icon)
                    ui.label(title).classes("text-lg font-semibold")

                def on_logic(e):
                    block["logic"] = e.value
                    changed()
                ui.toggle({k: p(v) for k, v in LOGICS.items()}, value=block["logic"], on_change=on_logic) \
                    .props("dense no-caps unelevated rounded toggle-color=primary")
            for c in block["conditions"]:
                cond_editor(block, c, changed, draw)
            if side == "sell" and not block["conditions"]:
                ui.label(t("strat.no_sell")).classes("sq-muted text-sm")

            def add():
                block["conditions"].append(
                    cond(make_ind("close"), ">" if side == "buy" else "<", make_ind("sma", period=20)))
                draw()
                changed()
            ui.button(t("strat.add_cond"), icon="add", on_click=add).props("flat no-caps dense color=primary") \
                .mark(f"add:{side}")
    draw()


def _rule_editor(S, ctx):
    def changed():                  # 晚绑定：首次绘制时预览区还没建好
        ctx.changed()
    S["rule"].setdefault("position_pct", 95)
    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-end gap-3"):
            preset = ui.select({k: t(f"preset.{k}") for k in PRESETS}, label=t("strat.preset"), value=S["preset"]) \
                .props(DENSE).classes("grow").mark("preset")

            def load_preset():
                S["preset"] = preset.value
                S["rule"] = PRESETS[preset.value]()
                S["ai_symbols"] = None
                ctx.reload()
            ui.button(t("strat.load_preset"), icon="download_done", on_click=load_preset).props("outline no-caps")

    with ui.card().classes("w-full gap-3"):
        side_editor(S, "buy", t("strat.buy_title"), "trending_up", changed)
    with ui.card().classes("w-full gap-3"):
        side_editor(S, "sell", t("strat.sell_title"), "trending_down", changed)

    with ui.card().classes("w-full gap-1"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(t("strat.position")).classes("font-medium").tooltip(t("strat.position_help"))
            pct = ui.label(f"{S['rule']['position_pct']}%").classes("sq-num text-primary font-semibold")

        def on_pct(e):
            S["rule"]["position_pct"] = int(e.value)
            pct.text = f"{int(e.value)}%"
            changed()
        ui.slider(min=10, max=100, step=5, value=S["rule"]["position_pct"], on_change=on_pct).props("label")


# ---------------- 自己写代码 ----------------
def _code_editor(S, ctx):
    lg = lang()
    if not S["code"].strip():
        S["code"] = code_strategy.skeleton(lg)
    starts = {"skeleton": t("code.start_skeleton")}
    starts.update({f"tpl:{k}": t("code.start_template", name=p(v.label)) for k, v in TEMPLATES.items()})
    rule = clean(S["rule"])
    if not validate(rule, lg):
        starts["rule"] = t("code.start_rule")

    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2 no-wrap sq-note py-2"):
            ui.icon("info").classes("text-primary")
            ui.label(t("code.intro")).classes("text-sm")
        with ui.row().classes("w-full items-end gap-3"):
            start = ui.select(starts, label=t("code.start_from"), value="skeleton").props(DENSE).classes("grow")                 .mark("code_start")

            def load_start():
                v = start.value
                if v == "skeleton":
                    code = code_strategy.skeleton(lg)
                elif v == "rule":
                    code = code_strategy.rule_code(rule, lg)
                else:
                    code = code_strategy.template_code(v[4:], {})
                S["code"] = code
                editor.value = code
                ctx.changed()
            ui.button(t("code.load"), icon="download_done", on_click=load_start).props("outline no-caps").mark("code_start_load")

        def on_code(e):
            S["code"] = e.value or ""
            ctx.changed()
        editor = code_editor(S["code"], on_code).mark("code")

        def check():
            errors = code_strategy.check(S["code"], lang())
            if errors:
                ui.notify(errors[0], type="negative", multi_line=True)
                return
            cls = code_strategy.compile_strategy(S["code"])
            names = ", ".join(code_strategy.code_params(cls))
            ui.notify(t("code.check_ok") + ("。" if lg == "zh" else ". ")
                      + (t("code.params", names=names) if names else t("code.no_params")), type="positive",
                      multi_line=True)
        with ui.row().classes("w-full items-center gap-3"):
            ui.button(t("code.check"), icon="fact_check", on_click=check).props("outline no-caps").mark("code_check")
        with ui.expansion(t("code.cheatsheet"), icon="menu_book").classes("w-full").props("dense"):
            ui.label(code_strategy.CHEATSHEET.get(lg, code_strategy.CHEATSHEET["zh"]))                 .classes("sq-code w-full whitespace-pre text-xs overflow-x-auto")


# ---------------- 一句话生成 ----------------
def _ai_editor(S, ctx):
    cfg = llm.load_config()
    ready = bool(cfg and cfg.ready)
    with ui.card().classes("w-full gap-3"):
        if not ready:
            with ui.row().classes("w-full items-center gap-3 sq-note py-2 no-wrap"):
                ui.icon("key").classes("text-primary")
                ui.label(t("ai.need_config")).classes("grow text-sm")
                ui.button(t("nav.go_settings"), on_click=lambda: ui.navigate.to("/settings")) \
                    .props("flat no-caps dense color=primary icon-right=arrow_forward")
        with ui.row().classes("items-center gap-2"):
            ui.label(t("ai.examples")).classes("sq-muted text-sm")
            for ex in AI_EXAMPLES:
                ui.chip(t(ex + "_short"), on_click=lambda ex=ex: text.set_value(t(ex))) \
                    .props("outline clickable color=primary")
        text = ui.textarea(t("ai.describe"), value=S["ai_text"], placeholder=t("ai.placeholder")) \
            .props("outlined autogrow").classes("w-full")

        async def generate():
            S["ai_text"] = text.value or ""
            if not S["ai_text"].strip():
                return
            btn.disable()
            spinner.set_visibility(True)
            try:
                res = await run.io_bound(llm.translate, S["ai_text"], llm.get_provider(cfg), lang())
                S["ai_result"], S["ai_symbols"], S["name"] = res, res.symbols, res.name
                ctx.set_name(res.name)
            except llm.LLMError as e:
                S["ai_result"] = None
                ui.notify(str(e), type="negative", multi_line=True)
            except Exception as e:  # noqa: BLE001 - 缺少依赖、网络等
                S["ai_result"] = None
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                btn.enable()
                spinner.set_visibility(False)
            result.refresh()
            ctx.changed()

        with ui.row().classes("items-center gap-3"):
            btn = ui.button(t("ai.generate"), icon="auto_awesome", on_click=generate).props("unelevated no-caps").mark("generate")
            spinner = ui.row().classes("items-center gap-2")
            with spinner:
                ui.spinner(size="sm")
                ui.label(t("ai.thinking")).classes("sq-muted text-sm")
            spinner.set_visibility(False)
        btn.set_enabled(ready and bool((S["ai_text"] or "").strip()))

        def on_text(e):
            S["ai_text"] = e.value or ""
            btn.set_enabled(ready and bool(S["ai_text"].strip()))
        text.on_value_change(on_text)

    @ui.refreshable
    def result():
        res = S["ai_result"]
        if not res:
            return
        with ui.card().classes("w-full gap-3"):
            ui.label(t("ai.understood")).classes("font-semibold")
            with ui.row().classes("w-full items-start gap-2 no-wrap sq-note py-2"):
                ui.icon("psychology").classes("text-primary")
                ui.label(res.understood or "—").classes("text-sm")
            if res.unsupported:
                with ui.column().classes("w-full gap-1 rounded-lg px-3 py-2").style(
                        "border: 1px solid rgba(245,158,11,.4); background: rgba(245,158,11,.08)"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("block").classes("text-warning")
                        ui.label(t("ai.unsupported")).classes("text-sm font-medium")
                    for u in res.unsupported:
                        ui.label(f"• {u}").classes("text-sm")
            if res.symbols:
                have = {m.symbol for m in library.list_datasets()}
                missing = [s for s in res.symbols if s not in have]
                ui.label(t("ai.symbols", codes="、".join(res.symbols) if lang() == "zh" else ", ".join(res.symbols))
                         + ("  " + t("ai.symbols_missing", codes=", ".join(missing)) if missing else "")) \
                    .classes("sq-muted text-sm")
            if res.ok:
                def to_builder():
                    S["rule"] = with_ids(res.spec["rule"])
                    S["rule"]["position_pct"] = int(res.spec["rule"]["position_pct"])
                    S["mode"] = "rule"
                    ctx.reload()
                ui.button(t("ai.edit_in_builder"), icon="tune", on_click=to_builder).props("outline no-caps")
    result()

