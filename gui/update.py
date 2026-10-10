"""
自动更新的界面（检查、下载在 simplequant/update/client.py 的后台线程里，这里只读状态显示）
- banner()：每页顶部的提示条（发现新版本、下载中、已下载好、上次更新的结果），frame() 调用
- about()：设置页「关于 SimpleQuant」：版本、检查更新按钮、启动时自动检查的开关、项目主页等链接
"""

from nicegui import app, ui

from simplequant import __version__
from simplequant.paths import DATA_ROOT
from simplequant.update import HOME_PAGE, ISSUES_PAGE, CHANGELOG_PAGE
from simplequant.update.client import UPDATER
from gui.common import t
from gui.widgets import notice

_dismissed: set[str] = set()      # 本次运行中点了「稍后」的版本


def auto_check() -> bool:
    return app.storage.general.get("auto_update", True)


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f}"


def _message(s) -> tuple[str, str, str]:
    """(文字, 图标, 类型)；空文字表示不显示"""
    if s.state == "checking":
        return t("upd.checking"), "sync", "info"
    if s.state == "latest":
        return t("upd.latest", v=__version__), "check_circle", "info"
    if s.state == "available":
        if not UPDATER.installable:
            return t("upd.available_source", v=s.version), "new_releases", "info"
        return t("upd.available_full" if s.full else "upd.available", v=s.version, mb=_mb(s.size)), \
            "new_releases", "info"
    if s.state == "downloading":
        return t("upd.downloading", v=s.version, done=_mb(s.done), total=_mb(s.size)), "downloading", "info"
    if s.state == "ready":
        return t("upd.ready", v=s.version), "system_update_alt", "info"
    if s.state == "error":
        if s.error == "platform":
            return t("upd.err_platform"), "info", "info"
        key = {"network": "upd.err_network", "verify": "upd.err_verify"}.get(s.error, "upd.err_other")
        return t(key, detail=s.detail), "error_outline", "warning"
    return "", "", ""


def _quit():
    """退出程序，更新脚本等到所有进程结束后才开始替换文件"""
    if app.native.main_window:
        # 与用户关闭窗口相同：窗口进程结束后 NiceGUI 直接退出。不用 app.shutdown()：它在关窗口的同时
        # 让 uvicorn 关闭连接，正在断开的 websocket 会报 LocalProtocolError，打包版弹出错误框、进程不退出
        app.native.main_window.destroy()
    else:
        app.shutdown()


def _restart():
    if UPDATER.apply():
        ui.notify(t("upd.restarting"))
        ui.timer(1.5, _quit, once=True)


def _render(s, dismiss=None):
    text, icon, kind = _message(s)
    if not text:
        return
    notice(text, icon, kind).mark("update_status")
    with ui.row().classes("items-center gap-3"):
        if s.state == "available" and UPDATER.installable and not UPDATER.busy:
            ui.button(t("upd.download"), icon="download", on_click=UPDATER.download_async) \
                .props("unelevated no-caps").mark("update_download")
        if s.state == "ready":
            ui.button(t("upd.restart"), icon="restart_alt", on_click=_restart) \
                .props("unelevated no-caps").mark("update_restart")
        if s.state in ("available", "ready", "error"):
            ui.link(t("upd.page"), s.page, new_tab=True).classes("text-sm")
        if dismiss:
            ui.button(t("upd.later"), on_click=lambda: dismiss(s.version)) \
                .props("flat dense no-caps").classes("sq-muted").mark("update_later")
    if s.notes and s.state in ("available", "downloading", "ready"):
        with ui.expansion(t("upd.notes")).classes("w-full text-sm").props("dense"):
            ui.markdown(s.notes).classes("text-sm")


class _Live:
    """按状态重画；状态在后台线程里变化，用定时器检查（NiceGUI 元素不宜跨线程直接改）"""

    def __init__(self, draw):
        self.draw, self.key = draw, None
        self.box = ui.column().classes("w-full gap-2")
        self.refresh()
        ui.timer(1.0, self.tick)

    def tick(self):
        s = UPDATER.status
        if (s.state, s.version, s.done, UPDATER.busy) != self.key:
            self.refresh()

    def refresh(self):
        s = UPDATER.status
        self.key = (s.state, s.version, s.done, UPDATER.busy)
        self.box.clear()
        with self.box:
            self.draw(s)


def banner(route: str):
    """每页顶部：发现新版本 / 下载中 / 已下载好（安装版），以及上次更新的结果（只显示一次）"""
    last = UPDATER.status.last
    if last:
        UPDATER.status.last = None
        if last.get("ok"):
            notice(t("upd.done", v=last.get("version", "")), "check_circle").mark("update_result")
        else:
            msg = last.get("message", "")
            notice(t("upd.failed_busy") if msg == "busy" else t("upd.failed", detail=msg), "error_outline",
                   "warning").mark("update_result")
    if route == "/settings" or not UPDATER.installable:
        return

    def dismiss(version):
        _dismissed.add(version)
        live.refresh()

    def draw(s):
        if s.state in ("available", "downloading", "ready") and s.version not in _dismissed:
            _render(s, dismiss)
    live = _Live(draw)


def about():
    """设置页「关于 SimpleQuant」"""
    ui.label(t("set.about")).classes("font-semibold")
    ui.label(t("set.version", v=__version__)).mark("version")
    ui.label(t("set.data_dir", path=str(DATA_ROOT))).classes("sq-muted text-xs")
    with ui.row().classes("items-center gap-4 text-sm"):    # 原生窗口中 new_tab 链接由系统浏览器打开
        for key, url in (("set.home_page", HOME_PAGE), ("set.issues", ISSUES_PAGE), ("set.changelog", CHANGELOG_PAGE)):
            ui.link(t(key), url, new_tab=True).mark(f"link:{key}")
    with ui.row().classes("items-center gap-4"):
        ui.button(t("upd.check"), icon="update", on_click=UPDATER.check_async) \
            .props("outline no-caps").mark("update_check")
        ui.switch(t("upd.auto"), value=auto_check(),
                  on_change=lambda e: app.storage.general.__setitem__("auto_update", bool(e.value))) \
            .mark("update_auto")
    _Live(_render)
