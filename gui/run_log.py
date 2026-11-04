"""工作台最近几次运行（新版界面，界面重构计划 4.5）：报告顶部「与上次相比」与「最近 n 次运行」弹窗

择时、选股两个工作台各记各的（kind = "timing" / "sel"），只存在 state 里，重启程序清空。每次运行有一个编号 no，
报告记下自己的编号；只有正在显示的报告就是最近一次运行时才显示对比（经典页面跑出的报告、分别回测都没有编号）。
"""

import datetime as dt

from nicegui import ui

from gui import state
from gui.common import t, lang
from ui.shared import push_run, run_diff, pct, DIFF_METRICS

_KEEP_METRICS = ("total_return",) + DIFF_METRICS


def runs(kind: str) -> list[dict]:
    return state.STATE.setdefault("runs", {}).setdefault(kind, [])


def record(kind: str, name: str, spec: dict, metrics: dict, setup: dict) -> int:
    """记下一次运行，返回它的编号。setup：策略之外的设置 {键: 摘要文字}，键的名称取 bt.chip_{键}"""
    store = state.STATE.setdefault("runs", {})
    no = store["n"] = store.get("n", 0) + 1
    push_run(runs(kind), {"no": no, "time": dt.datetime.now().strftime("%H:%M:%S"), "name": name,
                          "spec": {k: v for k, v in spec.items() if k != "name"},
                          "metrics": {k: metrics.get(k) for k in _KEEP_METRICS}, "setup": dict(setup)})
    return no


def compare(kind: str, no: int | None):
    """报告顶部的一块：与上次相比改了什么、指标变了多少；右边「最近 n 次运行」打开弹窗。
    no 不是最近一次运行（或只运行过一次）时什么都不画"""
    rs = runs(kind)
    if no is None or len(rs) < 2 or rs[0]["no"] != no:
        return
    lg = lang()
    lines = run_diff(rs[1], rs[0], lg)
    with ui.element("div").classes("sq-run-diff w-full").mark("run_diff"):
        with ui.row().classes("w-full items-baseline justify-between no-wrap gap-3"):
            ui.label(t("diff.title", time=rs[1]["time"][:5])).classes("sq-eyebrow")
            ui.button(t("diff.history", n=len(rs)), on_click=lambda: _history(rs)) \
                .props("flat dense no-caps").classes("sq-run-diff-more").mark("run_history")
        for line in lines:
            ui.label(line).classes("text-sm sq-run-diff-line")


def _history(rs: list[dict]):
    """最近几次运行：最新的在上；每次写指标，以及与它前一次相比改了什么"""
    lg = lang()
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full gap-0").mark("run_history_dialog"):
        ui.label(t("diff.history_title", n=len(rs))).classes("sq-serif text-lg pb-2 w-full") \
            .style("border-bottom: 1px solid var(--sq-text)")
        for i, r in enumerate(rs):
            m = r["metrics"]
            with ui.column().classes("w-full gap-1 py-3 sq-run-item").mark(f"run_item:{i}"):
                with ui.row().classes("w-full items-baseline justify-between no-wrap gap-3"):
                    ui.label(f"{r['time']} · {r['name']}").classes("text-sm font-medium min-w-0 truncate")
                    ui.label(" · ".join(f"{t(f'm.{k}')} {pct(m.get(k))}" for k in _KEEP_METRICS)) \
                        .classes("sq-num text-xs sq-muted whitespace-nowrap")
                if i + 1 < len(rs):
                    for line in run_diff(rs[i + 1], r, lg, metrics=False):
                        ui.label(line).classes("text-xs sq-muted")
                else:
                    ui.label(t("diff.first")).classes("text-xs sq-muted")
        with ui.row().classes("w-full justify-end pt-2"):
            ui.button(t("gui.close"), on_click=dlg.close).props("flat no-caps")
    dlg.open()
