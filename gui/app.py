"""
NiceGUI 应用：注册页面并启动。由 main.py 调用。
只监听 127.0.0.1（局域网其他设备打不开）。
"""

import os
from pathlib import Path

from simplequant.paths import CACHE_DIR

# 界面偏好（语言、主题）存到数据目录；必须在 import nicegui 之前设置
os.environ.setdefault("NICEGUI_STORAGE_PATH", str(CACHE_DIR / "gui_storage"))
# 第一次运行打包版时数据目录还不存在，NiceGUI 只建最后一级目录，会报错
os.makedirs(os.environ["NICEGUI_STORAGE_PATH"], exist_ok=True)

from nicegui import app, ui  # noqa: E402

from simplequant.data import library  # noqa: E402
from gui import theme  # noqa: E402
from gui.layout import PAGES  # noqa: E402
from gui.pages import (home, data, strategy, backtest, optimize, selection, paper, settings, allocation, tools,  # noqa: E402
                       timing, factors)

ICON = Path(__file__).with_name("static") / "icon.png"

# 路由 → 页面函数
ROUTES = {
    "/": home.page,
    "/data": data.page,
    "/strategy": strategy.page,
    "/backtest": backtest.page,
    "/optimize": optimize.page,
    "/timing": timing.page,
    "/selection": selection.page,
    "/factors": factors.page,
    "/paper": paper.page,
    "/settings": settings.page,
    "/allocation": allocation.page,
    "/tools": tools.page,
}


def register():
    """注册全部路由（显式调用而不用装饰器，界面测试重置 NiceGUI 后可以重新注册）"""
    theme.install()
    for route, _, _ in PAGES:
        ui.page(route)(ROUTES[route])


def _start_update_check():
    """启动后台检查更新：先读上次更新的结果；开启了自动检查时，等界面打开后再联网"""
    import threading
    from gui.update import auto_check
    from simplequant.update.client import UPDATER
    UPDATER.startup()
    if auto_check():
        threading.Timer(5, UPDATER.check_async).start()


def _start_telemetry():
    """匿名使用统计（待办 57）：启用本机累计；同意过的话几秒后在后台发送一次。
    报错计数接 NiceGUI 的未处理异常；导出、下载计数接 ui.download.content（项目里所有下载都走它）"""
    from simplequant import telemetry
    from simplequant.paper import list_accounts
    from gui.history import recent
    existing = bool(library.list_datasets() or recent() or list_accounts())
    if not telemetry.activate(existing_user=existing):
        return
    app.on_exception(telemetry.error)
    content = ui.download.content

    def counted(data, filename=None, media_type=""):
        ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else "other"
        telemetry.count(f"dl:{ext if ext.isalnum() and len(ext) <= 8 else 'other'}")
        telemetry.milestone("export")
        return content(data, filename, media_type)
    ui.download.content = counted
    if telemetry.due():
        telemetry.send_async()


def run(native: bool = True, port: int = 0, show: bool = True, check_updates: bool = True):
    """check_updates=False：界面测试、截图用的服务器不联网检查更新，也不启用使用统计"""
    register()
    if check_updates:
        app.on_startup(_start_update_check)
        app.on_startup(_start_telemetry)
    from gui import uitest
    uitest.install()             # 只有设置了 SIMPLEQUANT_UI_SCRIPT 才生效（打包后的自动测试）
    # 桌面窗口（pywebview）默认会静默取消所有下载；打开后点下载会弹出「另存为」对话框
    app.native.settings["ALLOW_DOWNLOADS"] = True
    # pywebview 默认禁止选中文字（text_select=False），界面上的说明、表格、代码都复制不了
    app.native.window_args["text_select"] = True
    ui.run(
        host="127.0.0.1",
        port=port or None,           # None：原生模式自动找空闲端口，浏览器模式用 8080
        title="SimpleQuant",
        favicon=ICON if ICON.exists() else "📈",   # 浏览器标签页图标；图标由 tools/make_icon.py 生成
        native=native,
        window_size=(1400, 900) if native else None,
        reload=False,                # 打包后不能用自动重载
        show=show,
        show_welcome_message=False,
    )
