"""数据保存位置的界面：设置页一节、首页询问一次、位置打不开时的提示、移动对话框。
逻辑在 simplequant/data_location.py。完整路径只放在悬停提示里（与 folder_btn 相同），录屏、截图时不露出本机用户名"""

import sys
from pathlib import Path

from nicegui import app, run, ui

from simplequant import data_location as loc, paths
from simplequant.system import WINDOWS, relaunch_after_exit
from gui.common import t
from gui.widgets import Progress, folder_btn, notice

ASKED = "data_loc_asked"     # app.storage.general：首页问过了


def size_text(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{max(n, 0) / 1e6:.0f} MB"


def code(s) -> str:
    """notice 按 Markdown 显示：路径放进代码格式，下划线、反斜杠不会被当成格式符号"""
    return "`" + str(s).replace("`", "'") + "`"


def place(root: Path) -> str:
    """「C 盘」；macOS / Linux 用 ~ 代替家目录"""
    root = Path(root)
    if WINDOWS and root.drive:
        return t("loc.drive", d=root.drive.rstrip(":").upper())
    home = str(Path.home())
    s = str(root)
    return "~" + s[len(home):] if s.startswith(home) else s


def summary(root: Path) -> str:
    """耗时（要遍历所有文件），在后台线程里调用"""
    free = loc.free_bytes(root)
    return t("loc.where", place=place(root), size=size_text(loc.size_bytes(root)),
             free=size_text(free) if free is not None else "?")


def pending_target() -> Path | None:
    """已经换了位置、还没重启时返回新位置"""
    if not loc.enabled() or paths.LOCATION_MISSING:
        return None
    target = Path(paths.read_pointer().get("data_root") or loc.user_data_dir())
    return None if loc._same(target, paths.DATA_ROOT) else target


# ---------------- 首页 ----------------

def missing_note():
    if paths.LOCATION_MISSING:
        notice(t("loc.missing", path=code(paths.LOCATION_MISSING)), "warning", "warning").mark("home_loc_missing")


def should_ask() -> bool:
    """首页问一次：Windows 打包版、数据在系统盘、没问过；新装和升级上来的老用户都问"""
    return (loc.enabled() and not app.storage.general.get(ASKED) and pending_target() is None
            and paths.LOCATION_MISSING is None and loc.on_system_drive(paths.DATA_ROOT))


async def ask_dialog(existing: bool):
    root = paths.DATA_ROOT
    size = size_text(await run.io_bound(loc.size_bytes, root)) if existing else ""

    def answered():
        app.storage.general[ASKED] = True
        dlg.close()

    def change():
        answered()
        move_dialog()

    with ui.dialog().props("persistent") as dlg, ui.card().classes("w-[560px] max-w-full gap-3").mark("loc_ask"):
        ui.label(t("loc.ask_title")).classes("sq-h2")
        ui.label(t("loc.ask_old" if existing else "loc.ask_new", place=place(root), size=size)).classes("text-sm")
        with ui.row().classes("gap-3 self-end"):
            ui.button(t("loc.keep"), on_click=answered).props("flat no-caps").mark("loc_keep")
            ui.button(t("loc.change"), icon="drive_file_move", on_click=change).props("unelevated no-caps") \
                .mark("loc_ask_change")
    dlg.open()


# ---------------- 设置页 ----------------

def settings_section():
    with ui.column().classes("gap-2 w-full").mark("set_loc"):
        ui.label(t("loc.title")).classes("font-semibold")
        ui.label(t("loc.help")).classes("sq-muted text-sm")
        with ui.row().classes("items-center gap-2"):
            where = ui.label(t("loc.measuring")).classes("text-sm sq-num").mark("loc_where")
            with where:
                ui.tooltip(str(paths.DATA_ROOT))
            folder_btn(t("set.open_folder"), paths.DATA_ROOT)
        if (target := pending_target()) is not None:
            notice(t("loc.pending", place=place(target)), "schedule").mark("loc_pending")
        if loc.enabled():
            with ui.row().classes("gap-3"):
                ui.button(t("loc.change"), icon="drive_file_move", on_click=lambda: move_dialog()) \
                    .props("outline no-caps").mark("loc_change")
                default = loc.user_data_dir()
                if not loc._same(paths.DATA_ROOT, default):
                    ui.button(t("loc.reset"), on_click=lambda: move_dialog(str(default))) \
                        .props("flat no-caps").mark("loc_reset")
        else:
            ui.label(t("loc.fixed")).classes("sq-muted text-xs")

    async def measure():
        text = await run.io_bound(summary, paths.DATA_ROOT)
        if not where.is_deleted:
            where.text = text
    ui.timer(0.1, measure, once=True)


# ---------------- 移动对话框 ----------------

def _error_text(e: loc.LocationError) -> str:
    kw = dict(e.kw)
    for k in ("need", "free"):
        if k in kw:
            kw[k] = size_text(kw[k])
    if "detail" in kw:
        kw["detail"] = code(kw["detail"])
    return t(f"loc.err_{e.code}", **kw)


def restart():
    from gui.update import _quit
    if not paths.FROZEN:
        ui.notify(t("loc.restart_manual"))
        return
    relaunch_after_exit([sys.executable, *sys.argv[1:]])
    ui.timer(0.5, _quit, once=True)


def move_dialog(initial: str = ""):
    state = {"plan": None}

    with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-full gap-3").mark("loc_dialog"):
        ui.label(t("loc.pick_title")).classes("sq-h2")
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            path = ui.input(t("loc.pick_label"), value=initial).props("outlined dense").classes("grow") \
                .mark("loc_path")
            if app.native.main_window:
                ui.button(t("loc.browse"), on_click=lambda: browse()).props("outline no-caps")
        ui.label(t("loc.pick_hint")).classes("sq-muted text-xs")
        msg = ui.column().classes("w-full gap-2")
        prog = Progress()
        with ui.row().classes("gap-3 self-end") as btns:
            ui.button(t("gui.cancel"), on_click=dlg.close).props("flat no-caps")
            ui.button(t("loc.check"), on_click=lambda: check()).props("outline no-caps").mark("loc_check")
            go_btn = ui.button(t("loc.go"), icon="drive_file_move", on_click=lambda: go()) \
                .props("unelevated no-caps").mark("loc_go")
            go_btn.set_visibility(False)

    def say(text, icon="info", kind="info"):
        msg.clear()
        with msg:
            notice(text, icon, kind).mark("loc_msg")

    async def browse():
        import webview
        # pywebview 6 的 FOLDER_DIALOG 是「已弃用」代理对象，传给窗口进程时 pickle 失败、请求永远不返回（按钮没反应）；
        # 用 FileDialog.FOLDER（与 NiceGUI 自己的写法一致）
        folder = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        res = await app.native.main_window.create_file_dialog(dialog_type=folder)
        if res:
            path.value = res[0] if isinstance(res, (list, tuple)) else str(res)
            await check()

    async def check():
        state["plan"] = None
        go_btn.set_visibility(False)
        try:
            p = await run.io_bound(loc.plan, path.value)
        except loc.LocationError as e:
            say(_error_text(e), "error", "error")
            return
        state["plan"] = p
        if p.existing:
            say(t("loc.plan_existing", target=code(p.target)))
            go_btn.text = t("loc.use")
        else:
            say(t("loc.plan_copy", size=size_text(p.need), target=code(p.target)))
            go_btn.text = t("loc.go")
        go_btn.set_visibility(True)

    path.on("keydown.enter", check)
    path.on_value_change(lambda: (go_btn.set_visibility(False), msg.clear()))   # 改了路径要重新检查

    async def go():
        p = state["plan"]
        if p is None:
            return
        btns.set_visibility(False)
        path.disable()
        try:
            if not p.existing:
                prog.start()
                await run.io_bound(loc.copy_data, p.target,
                                   progress=lambda f, i, n: prog.set(f, t("loc.copying", i=i, n=n)))
            loc.switch(p.target, cleanup=not p.existing)
        except loc.Busy:
            say(t("loc.busy"), "warning", "warning")
            btns.set_visibility(True)
            path.enable()
            return
        except OSError as e:
            say(t("loc.failed", detail=code(f"{type(e).__name__}: {e}")), "error", "error")
            btns.set_visibility(True)
            path.enable()
            return
        finally:
            prog.stop()
        say(t("loc.done"), "check_circle")
        with msg:
            with ui.row().classes("gap-3 self-end"):
                ui.button(t("tele.close"), on_click=dlg.close).props("flat no-caps")
                ui.button(t("loc.restart"), icon="restart_alt", on_click=restart).props("unelevated no-caps") \
                    .mark("loc_restart")

    dlg.open()
    if initial:
        ui.timer(0.1, check, once=True)
    return dlg
