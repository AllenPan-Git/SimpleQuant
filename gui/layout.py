"""每页共用的外框：经典界面是报头（标志、择时 / 选股切换、步骤条、语言、主题），新版界面是左侧栏"""

from contextlib import contextmanager
from html import escape

from nicegui import app, ui

from simplequant.i18n import LANGS, tr
from gui import theme, state, update
from gui.common import t, p, lang, set_lang, dark, set_dark, new_ui

# (路由, 文字键, 图标)：全部页面（注册路由、页面标题用）
PAGES = [
    ("/", "nav.home", "home"),
    ("/data", "nav.data", "storage"),
    ("/strategy", "nav.strategy", "tune"),
    ("/backtest", "nav.backtest", "query_stats"),
    ("/optimize", "nav.optimize", "grid_on"),
    ("/timing", "nav.timing", "show_chart"),          # 新版界面的择时工作台（经典界面不链接它）
    ("/selection", "nav.selection", "filter_list"),
    ("/factors", "nav.factors", "science"),           # 新版界面的因子实验室（经典界面不链接它）
    ("/paper", "nav.paper", "account_balance_wallet"),
    ("/allocation", "nav.allocation", "pie_chart"),
    ("/tools", "nav.tools", "build"),
    ("/settings", "nav.settings", "settings"),
]

# 三条工作流程，顶部步骤条按当前流程显示
FLOWS = {
    "timing": ["/data", "/strategy", "/backtest", "/optimize", "/paper"],
    "selection": ["/data", "/selection", "/paper"],
    "allocation": ["/data", "/allocation", "/paper"],
}
FLOW_HOME = {"timing": "/strategy", "selection": "/selection", "allocation": "/allocation"}
STEP_KEY = {"/data": "step.data", "/strategy": "step.strategy", "/backtest": "step.backtest",
            "/optimize": "step.optimize", "/selection": "step.selection", "/paper": "step.paper",
            "/allocation": "step.allocation"}

_THEMES = [(None, "brightness_auto"), (False, "light_mode"), (True, "dark_mode")]

# 新版界面的侧栏（界面重构计划 4.1）：(路由, 文字键, 图标, 分组, 也算本栏目的其他路由)
# 分组变化处画一条细线；"bottom" 组贴在侧栏底部。还没做的新页面先不放；
# 经典的策略、回测、优化页在新版界面下仍能打开（作为兜底），也算「择时策略」
NAV = [
    ("/", "side.today", "today", "today", ()),
    ("/timing", "side.timing", "show_chart", "work", ("/strategy", "/backtest", "/optimize")),
    ("/selection", "side.selection", "filter_list", "work", ()),
    ("/allocation", "side.allocation", "pie_chart", "work", ()),
    ("/factors", "side.factors", "science", "work", ()),
    ("/paper", "side.paper", "account_balance_wallet", "paper", ()),
    ("/data", "side.data", "storage", "data", ()),
    ("/tools", "side.tools", "build", "data", ()),
    ("/settings", "side.settings", "settings", "bottom", ()),
]


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


def step_info(route: str, flow: str | None = None) -> tuple[bool, str]:
    """(是否已完成, 步骤下方的小字)"""
    from simplequant import strategies
    from simplequant.data import library
    from simplequant.paper import list_accounts
    from ui.shared import pct
    S = state.STATE
    try:
        if route == "/data" and flow == "selection":      # 选股流程：当前股票池的选股数据是否已下载
            from simplequant.stocks import UNIVERSES, universe as U
            u = (S.get("sp") or {}).get("universe", "hs300")
            ok = U.ready(u)
            return ok, t("step.uni_ready" if ok else "step.uni_missing", u=p(UNIVERSES[u]["label"]))
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


def _go_step(route: str, flow: str):
    """选股流程的「数据」指向选股页的数据标签（数据页下载不了选股数据）；过渡做法，3.0 的数据中心会取代它。
    已在选股页时也重新打开一次：页面重画，面板取缓存（gui/sel_panel.py），不会重新载入"""
    if flow == "selection" and route == "/data":
        state.STATE["sp_tab"] = "data"
        route = "/selection"
    ui.navigate.to(route)


def _steps(route: str, flow: str):
    routes = FLOWS[flow]
    with ui.row().classes("sq-steps items-center gap-0 no-wrap mx-auto"):
        for i, r in enumerate(routes, 1):
            done, sub = step_info(r, flow)
            cls = "sq-step" + (" sq-step-cur" if r == route else " sq-step-done" if done else "")
            with ui.element("div").classes(cls).on("click", lambda r=r: _go_step(r, flow)).mark(f"nav:{r}"):
                ui.html(f"<span class='n'>{i:02d}</span>")
                ui.html(f"<div class='t'><span>{escape(t(STEP_KEY[r]))}</span>"
                        + (f"<small title='{escape(sub)}'>{escape(sub)}</small>" if sub else "") + "</div>")
            if i < len(routes):
                ui.element("div").classes("sq-link" + (" sq-link-done" if done else ""))


@contextmanager
def frame(route: str):
    """用法：with frame("/data"): ...页面内容..."""
    theme.apply(dark())
    if new_ui():
        # 新版界面的样式都写在 body.sq-new 下，经典界面不受影响；sq-side-open：窄窗口下侧栏保持展开
        ui.query("body").classes("sq-new" + (" sq-side-open" if side_open() else ""))
        _sidebar(route)
    else:
        _header(route)

    with ui.column().classes("sq-main w-full max-w-[1200px] mx-auto px-8 pt-8 pb-24 gap-5"):
        update.banner(route)     # 新版本提示、上次更新的结果
        yield


def _lang_switch():
    """中 · EN"""
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


def _theme_button():
    """跟随系统 → 浅色 → 深色"""
    cur = next(i for i, (v, _) in enumerate(_THEMES) if v == dark())

    def cycle_theme():
        set_dark(_THEMES[(cur + 1) % len(_THEMES)][0])
        ui.navigate.reload()
    ui.button(icon=_THEMES[cur][1], on_click=cycle_theme).props("flat round dense size=sm") \
        .classes("sq-muted").tooltip(t("gui.theme"))


def nav_section(route: str) -> str | None:
    """新版侧栏里 route 属于哪个栏目（文字键）；不在侧栏里时为 None"""
    return next((key for r, key, _, _, also in NAV if route == r or route in also), None)


def side_open() -> bool:
    """窄窗口下侧栏是否保持展开（用户点过「展开侧栏」）；宽窗口一直显示文字，不看这个设置"""
    return bool(app.storage.general.get("side_open"))


def _paper_pending() -> int:
    """侧栏「模拟盘」旁的数字：全部模拟账户的待执行交易合计；读不出时为 0（不显示）"""
    try:
        from simplequant.paper import list_accounts
        from ui.shared import pending_trades
        return pending_trades(list_accounts())
    except Exception:  # noqa: BLE001 - 侧栏只是提示，读坏了不影响页面
        return 0


def _sidebar(route: str):
    """新版界面：左侧栏。窗口窄于 theme.SIDE_MINI_BELOW 时折成图标列、悬停显示名称（纯 CSS，见 theme.py 的说明）；
    窄窗口下点底部的箭头可以展开回原宽度，记在 app.storage.general["side_open"]"""
    pending = _paper_pending()
    cur = nav_section(route)
    with ui.left_drawer(value=True, fixed=True, bordered=True).props(f"width={theme.SIDE_W} behavior=desktop") \
            .classes("sq-side column no-wrap").mark("sq_sidebar"):
        with ui.element("div").classes("sq-logo").on("click", lambda: ui.navigate.to("/")).mark("logo"):
            ui.html("<div class='sq-logo-full'>Simple<span>Quant</span></div><div class='sq-logo-mini'>S<span>Q</span></div>")
        ui.label(_masthead_date()).classes("sq-side-date").mark("side_date")
        nav = ui.element("nav").classes("sq-side-nav")
        nav.props["aria-label"] = t("side.nav")
        with nav:
            for i, (r, key, icon, group, _) in enumerate(NAV):
                if i and group != NAV[i - 1][3]:
                    if group == "bottom":
                        ui.element("div").classes("sq-side-space")
                    else:   # 分组名；图标列下 CSS 把它变回一条细线
                        with ui.element("div").classes("sq-side-sep"):
                            ui.label(t(f"side.group_{group}")).classes("sq-side-group")
                _side_item(r, t(key), icon, on=key == cur, n=pending if r == "/paper" else 0)
        with ui.element("div").classes("sq-side-foot"):
            _lang_switch()
            _theme_button()
            _side_toggle()


def _masthead_date(today=None) -> str:
    """侧栏报头的日期：「2026年11月3日　星期二」/「Tuesday, Nov. 3, 2026」"""
    import datetime as dt
    d = today or dt.date.today()
    return t("side.date", y=d.year, m=d.month, d=d.day, w=t("side.weekdays").split(",")[d.weekday()],
             mon=t("side.months").split(",")[d.month - 1])


def _side_item(route: str, name: str, icon: str, on: bool, n: int = 0):
    """侧栏的一个栏目：真正的链接（键盘 Tab 能选中）；n > 0 时名称后面显示数字，图标列下变成小红点"""
    label = f"{name} · {t('side.pending', n=n)}" if n else name
    link = ui.link(target=route).classes(remove="nicegui-link") \
        .classes("sq-side-item" + (" on" if on else "")).mark(f"nav:{route}")
    link.props["aria-label"] = label
    if on:
        link.props["aria-current"] = "page"
    with link:
        ui.icon(icon, size="18px")
        ui.label(name).classes("sq-side-label")
        if n:
            ui.label(str(n)).classes("sq-side-badge").mark("nav_badge")
        # 只在图标列下显示（宽窗口、窄窗口展开时由 CSS 隐藏）
        ui.tooltip(label).classes("sq-tip sq-side-tip").props('anchor="center right" self="center left" :offset="[8, 0]"')


def _side_toggle():
    """窄窗口下展开 / 收起侧栏（宽窗口不显示）。只改 body 的 class，不重画页面"""
    def flip():
        is_open = not side_open()
        app.storage.general["side_open"] = is_open
        if is_open:
            ui.query("body").classes(add="sq-side-open")
        else:
            ui.query("body").classes(remove="sq-side-open")
        btn.set_icon("chevron_left" if is_open else "chevron_right")
        tip.set_text(t("side.collapse" if is_open else "side.expand"))
    is_open = side_open()
    btn = ui.button(icon="chevron_left" if is_open else "chevron_right", on_click=flip) \
        .props("flat round dense size=sm").classes("sq-muted sq-side-toggle").mark("nav_collapse")
    with btn:
        tip = ui.tooltip(t("side.collapse" if is_open else "side.expand")).classes("sq-tip")


def _header(route: str):
    """经典界面：报头（标志、流程切换、步骤条、工具 / 设置、语言、主题）"""
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
            ui.label(t("top.tools")).classes("sq-top-link" + (" on" if route == "/tools" else "")) \
                .on("click", lambda: ui.navigate.to("/tools")).mark("nav:/tools")
            ui.label(t("top.settings")).classes("sq-top-link" + (" on" if route == "/settings" else "")) \
                .on("click", lambda: ui.navigate.to("/settings")).mark("nav:/settings")
            _lang_switch()
            _theme_button()


def page_title(route: str, subtitle: str | None = None, subtitle_full: str | None = None):
    """页面标题：朱红小字（流程 · 第几步）+ 宋体大标题 + 可选说明

    给了 subtitle_full 时，说明只显示 subtitle 一行，点「详细说明」看全文（widgets.hint）"""
    key = next(k for pth, k, _ in PAGES if pth == route)
    title = t(key).split(". ", 1)[-1]
    decor = tr(key, "en").split(". ", 1)[-1].upper()      # 不在流程里的页面：英文大写作装饰
    if new_ui():
        # 新版：眉标是所在栏目（回测页 → 择时策略）；栏目名和标题相同时（模拟盘、设置等）用英文大写，免得上下重复
        sec = nav_section(route)
        eyebrow = t(sec) if sec and t(sec) != title else decor
    else:
        flow = app.storage.general.get("flow", "timing")
        steps = FLOWS.get(flow, [])
        eyebrow = t("step.eyebrow", flow=t(f"flow.{flow}"), n=steps.index(route) + 1) if route in steps else decor
    with ui.column().classes("gap-1 pb-1"):
        ui.label(eyebrow).classes("sq-eyebrow").mark("page_eyebrow")
        ui.label(title).classes("sq-title")
        if subtitle and subtitle_full:
            from gui.widgets import hint
            hint(subtitle, subtitle_full, classes="sq-subtitle", mark="page_hint")
        elif subtitle:
            ui.label(subtitle).classes("sq-subtitle")
