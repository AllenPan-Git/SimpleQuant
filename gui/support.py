"""
「支持 SimpleQuant」：用过一阵（完成 FIRST_ASK 次回测）后弹一次，请用户去 GitHub 打星或在爱发电支持
- 选「以后再说」：再完成 LATER_ASK 次回测后再问一次；点了任一链接或「不再提示」：以后不再弹
- 计数存在 app.storage.general（"runs"、"support_next"、"support_done"），重启后保留
- 设置页「关于」里一直有这两个链接（links()）
"""

from nicegui import app, ui

from simplequant.update import HOME_PAGE, SPONSOR_PAGE
from gui.common import t

FIRST_ASK = 10
LATER_ASK = 50


def _done():
    app.storage.general["support_done"] = True


def _targets() -> list[tuple[str, str, str]]:
    """(文字键, 图标, 地址)：GitHub 打星、爱发电；没设爱发电地址时只有前一个"""
    return [("sup.star", "star_outline", HOME_PAGE)] + \
        ([("sup.sponsor", "favorite_border", SPONSOR_PAGE)] if SPONSOR_PAGE else [])


def after_run():
    """每次回测成功后调用（择时、选股）；到次数就弹窗"""
    g = app.storage.general
    g["runs"] = runs = g.get("runs", 0) + 1
    if g.get("support_done") or runs < g.get("support_next", FIRST_ASK):
        return
    g["support_next"] = runs + LATER_ASK          # 先记下，关掉窗口（点外面、按 Esc）也算「以后再说」
    # 版式同首页「继续上次的工作」：叠纸卡片、朱红小标题、宋体标题；主按钮墨色实心、次按钮描边
    with ui.context.client.layout, ui.dialog() as dlg, \
            ui.column().classes("sq-sheet gap-0 px-8 pt-6 pb-3 w-[480px] max-w-full"):
        ui.label(t("sup.eyebrow", n=runs)).classes("sq-eyebrow")
        ui.label(t("sup.title")).classes("sq-serif text-2xl mt-1 mb-3")
        ui.label(t("sup.body")).classes("text-sm leading-relaxed").style("color: var(--sq-text2)")
        with ui.row().classes("gap-3 py-5"):
            for i, (key, _, url) in enumerate(_targets()):      # 带 href 的按钮就是链接：桌面窗口里交给系统浏览器
                style = "unelevated color=primary" if i == 0 else "outline"
                ui.button(t(key), on_click=lambda: (_done(), dlg.close())) \
                    .props(f'no-caps href="{url}" target="_blank" {style}').classes("px-5").mark(f"link:{key}")
        with ui.row().classes("w-full justify-between pt-2").style("border-top: 1px solid var(--sq-border2)"):
            for key, close in (("sup.never", lambda: (_done(), dlg.close())), ("sup.later", dlg.close)):
                ui.button(t(key), on_click=close, color=None).props("flat dense no-caps").classes("text-[13px] px-1") \
                    .style("color: var(--sq-muted)").mark("support_" + key.split(".")[1])
    dlg.on_value_change(lambda e: e.value or dlg.delete())     # 关掉后（按钮、Esc、点外面）删掉
    dlg.open()


def links():
    """设置页「关于」里的两个链接（原生窗口中由系统浏览器打开）"""
    for key, icon, url in _targets():
        with ui.link(target=url, new_tab=True).classes("flex items-center gap-1 text-sm").mark(f"link:{key}"):
            ui.icon(icon).classes("text-base")
            ui.label(t(key))
