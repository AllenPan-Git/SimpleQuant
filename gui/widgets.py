"""各页共用的小控件"""

import datetime as dt
import math

import pandas as pd
from nicegui import ui


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, (pd.Timestamp, dt.date, dt.datetime)):
        return str(v.date()) if isinstance(v, pd.Timestamp) and v == v.normalize() else str(v)
    if hasattr(v, "item"):          # numpy 标量 → Python 标量（JSON 可序列化）
        return v.item()
    return v


def df_table(df: pd.DataFrame, rows_per_page: int = 0, dense: bool = True) -> ui.table:
    """DataFrame → 表格（列名即表头；rows_per_page=0 不分页）"""
    cols = [{"name": str(c), "label": str(c), "field": str(c), "align": "left", "sortable": True} for c in df.columns]
    rows = [{str(c): _cell(v) for c, v in zip(df.columns, r)} for r in df.itertuples(index=False)]
    tbl = ui.table(columns=cols, rows=rows, pagination=rows_per_page or None).classes("w-full")
    tbl.props("flat bordered" + (" dense" if dense else ""))
    return tbl


def section(title: str, icon: str):
    """带标题的卡片"""
    card = ui.card().classes("w-full")
    with card:
        with ui.row().classes("items-center gap-2"):
            ui.icon(icon, size="18px").classes("sq-red")
            ui.label(title).classes("sq-serif text-base")
    return card


def plot(fig) -> ui.plotly:
    """plotly 图：套用主题配色，容器高度跟随图自身设定的高度"""
    from gui import theme
    from gui.common import dark
    h = fig.layout.height or 450
    return ui.plotly(theme.style_fig(fig, dark())).classes("w-full").style(f"height: {h}px")


def fmt_table(df: pd.DataFrame, formats: dict) -> pd.DataFrame:
    """按列格式化数字，如 {"成交价": "{:.3f}"}；空值保持为空"""
    df = df.copy()
    for c, f in formats.items():
        if c in df.columns:
            df[c] = df[c].map(lambda v, f=f: "" if v is None or (isinstance(v, float) and math.isnan(v)) else f.format(v))
    return df


def notice(text: str, icon: str = "info", kind: str = "info"):
    """提示条：info / warning / error"""
    with ui.row().classes(f"w-full items-start gap-2 no-wrap sq-note sq-note-{kind} py-2") as row:
        ui.icon(icon).classes("mt-[2px]")
        ui.markdown(text).classes("text-sm grow sq-md-tight")
    return row


def download_btn(label: str, make, filename: str, media_type: str = "", icon: str = "download", props: str = ""):
    """点击时才生成内容的下载按钮（make() 返回 bytes / str）"""
    return ui.button(label, icon=icon, on_click=lambda: ui.download.content(make(), filename, media_type)) \
        .props(("outline no-caps " + props).strip())


class Progress:
    """进度条：后台线程里的回调只记下数值，界面由定时器每 0.2 秒刷新（NiceGUI 元素不宜跨线程直接改）"""

    def __init__(self):
        self.value, self.text = 0.0, ""
        with ui.column().classes("w-full gap-1") as self.box:
            self.bar = ui.linear_progress(value=0, show_value=False).props("size=4px")
            self.label = ui.label().classes("sq-muted text-sm sq-num")
        self.box.set_visibility(False)
        self.timer = ui.timer(0.2, self._tick, active=False)

    def _tick(self):
        self.bar.value = self.value
        self.label.text = self.text

    def start(self, text: str = ""):
        self.value, self.text = 0.0, text
        self._tick()
        self.box.set_visibility(True)
        self.timer.activate()

    def set(self, value: float, text: str = ""):
        self.value, self.text = max(0.0, min(1.0, value)), text

    def stop(self):
        self.timer.deactivate()
        self.box.set_visibility(False)


class LogBox:
    """运行日志：后台线程 append()，界面由定时器每 0.3 秒刷新"""

    def __init__(self):
        self.lines: list[str] = []
        self._shown = 0
        with ui.column().classes("w-full gap-0 sq-code max-h-72 overflow-auto") as self.box:
            pass
        self.box.set_visibility(False)
        self.timer = ui.timer(0.3, self._tick, active=False)

    def _tick(self):
        new = self.lines[self._shown:]
        self._shown += len(new)
        with self.box:
            for line in new:
                ui.label(line).classes("text-xs whitespace-pre-wrap")

    def start(self):
        self.lines.clear()
        self._shown = 0
        self.box.clear()
        self.box.set_visibility(True)
        self.timer.activate()

    def append(self, line: str):
        self.lines.append(str(line))

    def stop(self):
        self._tick()
        self.timer.deactivate()


# ---------------- 代码编辑区 ----------------
def code_editor(value: str, on_change, height: int = 460):
    """Python 代码编辑区（行号、高亮）；配色跟随界面明暗"""
    editor = ui.codemirror(value, language="Python", theme="vscodeLight", on_change=on_change) \
        .classes("w-full rounded-lg overflow-hidden").style(f"height: {height}px; font-size: 13px")

    async def match_theme():
        # 跟随系统时明暗由浏览器决定，所以直接问页面
        try:
            is_dark = await ui.run_javascript("document.body.classList.contains('body--dark')", timeout=3)
        except Exception:  # noqa: BLE001 - 页面已关闭、测试环境没有浏览器等
            return
        editor.theme = "vscodeDark" if is_dark else "vscodeLight"
    ui.timer(0.1, match_theme, once=True)
    return editor
