"""每页共用的外框：报头（标志、择时 / 选股切换、步骤条、语言、主题）"""

from contextlib import contextmanager
from html import escape

from nicegui import app, ui

from simplequant.i18n import LANGS, tr
from gui import theme, state, update
from gui.common import t, lang, set_lang, dark, set_dark

# (路由, 文字键, 图标)：全部页面（注册路由、页面标题用）
PAGES = [
    ("/", "nav.home", "home"),
    ("/data", "nav.data", "storage"),
    ("/strategy", "nav.strategy", "tune"),
    ("/backtest", "nav.backtest", "query_stats"),
    ("/optimize", "nav.optimize", "grid_on"),
    ("/selection", "nav.selection", "filter_list"),
    ("/paper", "nav.paper", "account_balance_wallet"),
    ("/allocation", "nav.allocation", "pie_chart"),
    ("/settings", "nav.settings", "smart_toy"),
]

# 三条工作流程，顶部步骤条按当前流程显示
FLOWS = {
    "timing": ["/data", "/strategy", "/backtest", "/optimize", "/paper"],
    "selection": ["/data", "/selection", "/paper"],
    "allocation": ["/data", "/allocation"],
}
FLOW_HOME = {"timing": "/strategy", "selection": "/selection", "allocation": "/allocation"}
STEP_KEY = {"/data": "step.data", "/strategy": "step.strategy", "/backtest": "step.backtest",
            "/optimize": "step.optimize", "/selection": "step.selection", "/paper": "step.paper",
            "/allocation": "step.allocation"}

_THEMES = [(None, "brightness_auto"), (False, "light_mode"), (True, "dark_mode")]


def flow_of(route: str) -> str:
    """当前页面属于哪条流程；数据、模拟盘等两条流程共用的页面沿用上次的流程"""
    if route == "/selection":
        flow = "selection"
    elif route == "/allocation":
        flow = "allocation"
    elif route in ("/strategy", "/backtest", "/optimize"):
        flow = "timing"
    else:
        flow = app.storage.general.get("flow", "timing")
    app.storage.general["flow"] = flow
    return flow


def step_info(route: str) -> tuple[bool, str]:
    """(是否已完成, 步骤下方的小字)"""
    from simplequant import strategies
    from simplequant.data import library
    from simplequant.paper import list_accounts
    from ui.shared import pct
    S = state.STATE
    try:
        if route == "/data":
            n = len(library.list_datasets())
            return n > 0, t("step.n_data", n=n) if n else t("step.not_started")
        if route == "/strategy":
            if S.get("current_spec"):
                return True, S["current_spec"].get("name", "")
            n = len(strategies.list_strategies())
            return n > 0, t("step.n_saved", n=n) if n else t("step.not_started")
        if route == "/backtest":
            res = S.get("bt", {}).get("result")
            return (True, pct(res.metrics["total_return"])) if res is not None else (False, t("step.not_started"))
        if route == "/optimize":
            o = S.get("opt", {})
            done = bool(o.get("result") or o.get("wf"))
            return done, t("step.done") if done else t("step.not_started")
        if route == "/selection":
            res = (S.get("sp") or {}).get("result")
            return (True, pct(res[0].metrics["total_return"])) if res else (False, t("step.not_started"))
        if route == "/allocation":
            from simplequant.allocation import load_profile, LEVELS
            from simplequant.i18n import pick
            prof = load_profile()
            return (True, f"C{prof.level} {pick(LEVELS[prof.level], lang())}") if prof else (False, t("step.not_started"))
        if route == "/paper":
            n = len(list_accounts())
            return n > 0, t("step.n_accounts", n=n) if n else t("step.not_started")
    except Exception:  # noqa: BLE001 - 步骤条只是提示，读不到就不显示
        pass
    return False, ""


def _steps(route: str, flow: str):
    routes = FLOWS[flow]
    with ui.row().classes("sq-steps items-center gap-0 no-wrap mx-auto"):
        for i, r in enumerate(routes, 1):
            done, sub = step_info(r)
            cls = "sq-step" + (" sq-step-cur" if r == route else " sq-step-done" if done else "")
            with ui.element("div").classes(cls).on("click", lambda r=r: ui.navigate.to(r)).mark(f"nav:{r}"):
                ui.html(f"<span class='n'>{i:02d}</span>")
                ui.html(f"<div class='t'><span>{escape(t(STEP_KEY[r]))}</span>"
                        + (f"<small>{escape(sub)}</small>" if sub else "") + "</div>")
            if i < len(routes):
                ui.element("div").classes("sq-link" + (" sq-link-done" if done else ""))


@contextmanager
def frame(route: str):
    """用法：with frame("/data"): ...页面内容..."""
    theme.apply(dark())
    flow = flow_of(route)

    with ui.header(elevated=False).classes("sq-header items-center gap-x-6 gap-y-2 px-8 py-3"):
        with ui.element("div").classes("sq-logo").on("click", lambda: ui.navigate.to("/")).mark("logo"):
            ui.html("Simple<span>Quant</span>")
        with ui.row().classes("items-center gap-4 no-wrap"):
            for f in FLOWS:
                on = f == flow and route != "/"
                ui.label(t(f"flow.{f}")).classes("sq-mode" + (" sq-mode-on" if on else "")) \
                    .on("click", lambda f=f: (app.storage.general.__setitem__("flow", f),
                                              ui.navigate.to(FLOW_HOME[f]))).mark(f"flow:{f}")
        _steps(route, flow)

        with ui.row().classes("sq-top-right items-center gap-4 no-wrap"):
            ui.label(t("top.settings")).classes("sq-top-link" + (" on" if route == "/settings" else "")) \
                .on("click", lambda: ui.navigate.to("/settings")).mark("nav:/settings")

            def on_lang(lg):
                if lg != lang():
                    set_lang(lg)
                    ui.navigate.reload()
            with ui.row().classes("items-center gap-1 no-wrap"):
                for i, lg in enumerate(LANGS):
                    if i:
                        ui.label("·").classes("sq-faint text-xs")
                    ui.label({"zh": "中", "en": "EN"}.get(lg, lg)).classes("sq-top-link" + (" on" if lg == lang() else "")) \
                        .on("click", lambda lg=lg: on_lang(lg))

            cur = next(i for i, (v, _) in enumerate(_THEMES) if v == dark())

            def cycle_theme():
                set_dark(_THEMES[(cur + 1) % len(_THEMES)][0])
                ui.navigate.reload()
            ui.button(icon=_THEMES[cur][1], on_click=cycle_theme).props("flat round dense size=sm") \
                .classes("sq-muted").tooltip(t("gui.theme"))

    with ui.column().classes("sq-main w-full max-w-[1200px] mx-auto px-8 pt-8 pb-24 gap-5"):
        update.banner(route)     # 新版本提示、上次更新的结果
        yield


def page_title(route: str, subtitle: str | None = None):
    """页面标题：朱红小字（流程 · 第几步）+ 宋体大标题 + 可选说明"""
    key = next(k for pth, k, _ in PAGES if pth == route)
    flow = app.storage.general.get("flow", "timing")
    steps = FLOWS.get(flow, [])
    eyebrow = t("step.eyebrow", flow=t(f"flow.{flow}"), n=steps.index(route) + 1) if route in steps \
        else tr(key, "en").split(". ", 1)[-1].upper()
    with ui.column().classes("gap-1 pb-1"):
        ui.label(eyebrow).classes("sq-eyebrow")
        ui.label(t(key).split(". ", 1)[-1]).classes("sq-title")
        if subtitle:
            ui.label(subtitle).classes("sq-subtitle")
