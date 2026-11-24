"""工作台最近几次运行（界面重构计划 4.5）：报告顶部「与上次相比」与「最近 n 次运行」弹窗

择时、选股两个工作台各记各的（kind = "timing" / "sel"），只存在 state 里，重启程序清空。每次运行有一个编号 no，
报告记下自己的编号；只有正在显示的报告就是最近一次运行时才显示对比（分别回测没有编号）。
"""

import datetime as dt

from nicegui import background_tasks, run, ui

from simplequant import llm, strategies, telemetry
from gui import state, cloud_ui
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
    prev, curr = rs[1], rs[0]
    lines = run_diff(prev, curr, lg)
    cfg = llm.load_config() or llm.LLMConfig()
    with ui.element("div").classes("sq-run-diff w-full").mark("run_diff"):
        with ui.row().classes("w-full items-baseline justify-between no-wrap gap-3"):
            ui.label(t("diff.title", time=prev["time"][:5])).classes("sq-eyebrow")
            with ui.row().classes("items-center gap-2 shrink-0"):
                ai_btn = None
                if cfg and cfg.ready:
                    ai_btn = ui.button(t("diff.ai_btn"), icon="auto_awesome") \
                        .props("flat dense no-caps").classes("sq-red text-xs").mark("run_diff_ai")
                ui.button(t("diff.history", n=len(rs)), on_click=lambda: _history(rs)) \
                    .props("flat dense no-caps").classes("sq-run-diff-more").mark("run_history")
        for line in lines:
            ui.label(line).classes("text-sm sq-run-diff-line")
        if ai_btn is not None:
            ai_box = ui.column().classes("w-full gap-1 pt-2").mark("run_diff_ai_box")
            key = (prev["no"], curr["no"])
            cache = state.STATE.setdefault("run_diff_ai", {})

            def show():
                if ai_box.is_deleted:
                    return
                ai_box.clear()
                if cache.get(key):
                    with ai_box, ui.column().classes("w-full gap-1 sq-note py-2"):
                        ui.markdown(cache[key]).classes("text-sm")
                        ui.label(t("diff.ai_note")).classes("sq-muted text-xs")

            async def go():
                c = llm.load_config() or llm.LLMConfig()
                if not (c and c.ready):
                    return
                ai_btn.disable()
                ai_box.clear()
                with ai_box, ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm")
                    ui.label(t("ai.thinking")).classes("sq-muted text-xs")
                telemetry.ai("compare_runs")
                try:
                    prev_desc = strategies.describe(prev["spec"], lg)
                    curr_desc = strategies.describe(curr["spec"], lg)
                    cache[key] = await run.io_bound(
                        llm.compare_runs, llm.get_provider(c), prev, curr, lines, prev_desc, curr_desc, lg,
                    )
                    cloud_ui.invalidate_quota()
                except Exception as e:  # noqa: BLE001
                    if not ai_box.is_deleted:
                        cloud_ui.handle_ai_error(e, ai_box, on_retry=lambda: background_tasks.create(go()))
                    return
                finally:
                    if not ai_btn.is_deleted:
                        ai_btn.enable()
                show()

            ai_btn.on_click(go)
            show()


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
