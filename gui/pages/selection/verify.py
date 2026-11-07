"""选股页的「验证」标签与顶部进度（界面重构计划 4.3.2）

布局与择时工作台的「验证」相同：左边是检验方法及状态，右边是选中的一项。
- 可信度检查：随选股回测完成，列出结果
- 调仓日检验：选股回测较慢，回测后点按钮才做（与回测结果里的按钮是同一个，状态在 SP["phase"]）
- 滚动优化：原来的「滚动优化」标签（walkforward.py）
随机买卖对照不适用于选股（待办 54 的随机选股对照以后放这里）。选中的一项记在 state.STATE["sp_vf"]。
"""

from types import SimpleNamespace

from nicegui import ui

from simplequant import strategies
from gui import state
from gui.common import t, lang
from gui.components import credibility_box
from gui.widgets import notice
from ui.shared import progress_steps, saved_name, paper_count, spec_key
from .backtest import current_spec, base_checks, phase_keys, _sel_checks
from .walkforward import _walkforward

VF_ITEMS = ("credibility", "phase", "walkforward")


def same_result(SP, ctx) -> bool:
    """回测结果是不是用当前设置、当前区间跑的"""
    r = SP.get("result")
    return bool(r) and r[1] == current_spec(SP) and tuple((SP.get("result_range") or ())[:2]) == (ctx.start, ctx.end)


def _phase(SP):
    """当前回测结果的调仓日检验状态；没做过为 None"""
    ph = SP.get("phase")
    return ph if ph and SP.get("result") and ph["res"] is SP["result"][0] else None


def vf_status(k: str, SP, ctx) -> tuple[bool, str]:
    """(是否已完成, 状态小字)；与当前设置不同的回测、滚动优化都不算"""
    if k == "walkforward":
        wf = SP.get("wf")
        ok = bool(wf) and wf["spec"] == current_spec(SP) and wf["range"] == (ctx.start, ctx.end)
        return ok, t("vf.st_wf", n=len(wf["res"].windows)) if ok else t("vf.st_not_run")
    if not same_result(SP, ctx):
        return False, t("vf.st_need_run" if k == "credibility" else "spv.st_after_run")
    if k == "credibility":
        return True, t("vf.st_findings", n=sum(c.get("level") != "ok" for c in base_checks(SP["result"][0])))
    if not phase_keys(SP["result"][1]):
        return False, t("spv.st_na")
    ph = _phase(SP)
    if ph is None:
        return False, t("vf.st_not_run")
    return ("found" in ph), t("vf.st_done") if "found" in ph else t("vf.st_running")


def verify_map(SP, ctx) -> dict | None:
    """进度条「验证」一步：适用的各项 → 是否已做；回测和滚动优化都没有时为 None（显示「—」）"""
    v = {k: vf_status(k, SP, ctx)[0] for k in VF_ITEMS}
    if not same_result(SP, ctx) and not v["walkforward"]:
        return None
    if same_result(SP, ctx) and not phase_keys(SP["result"][1]):
        v.pop("phase")
    return v


def progress(SP, ctx, accounts) -> list[dict]:
    """顶部四步进度：已保存 = 与某个已保存的选股策略同规则；有改动未保存 = 名称是已保存的策略但规则改过"""
    spec = current_spec(SP)
    name = (SP.get("name") or "").strip()
    try:
        saved_all = {n: s for n, s in strategies.list_strategies().items() if s.get("kind") == "selection"}
    except Exception:  # noqa: BLE001
        saved_all = {}
    saved = saved_name({**spec, "name": name}, saved_all)
    dirty = bool(name) and name in saved_all and spec_key(saved_all[name]) != spec_key(spec)
    ret = SP["result"][0].metrics["total_return"] if same_result(SP, ctx) else None
    return progress_steps(lang(), saved, dirty, ret, verify_map(SP, ctx), paper_count(spec, accounts))


def verify_tab(SP, ctx, go_backtest) -> SimpleNamespace:
    """画「验证」标签（面板载入后调用）；go_backtest()：切到选股回测标签。返回 .refresh()（重画状态与右侧）、.select(k)"""
    sel = {"k": state.STATE.get("sp_vf") if state.STATE.get("sp_vf") in VF_ITEMS else "credibility"}
    with ui.element("div").classes("sq-verify w-full grid gap-8"):
        methods = ui.column().classes("gap-0 min-w-0").mark("vf_methods")
        with ui.column().classes("gap-3 min-w-0"):
            head = ui.column().classes("gap-1 w-full")
            body = ui.column().classes("w-full gap-4").mark("vf_body")
    shown = {"k": None}

    def select(k: str):
        sel["k"] = state.STATE["sp_vf"] = k
        draw(force=True)

    def draw(force: bool = False):
        """左列每次都重画（状态会变）；右侧只在换了一项或 force 时重画（滚动优化填到一半的参数不被冲掉）"""
        if methods.is_deleted:
            return
        k = sel["k"]
        methods.clear()
        with methods:
            ui.label(t("vf.methods")).classes("sq-muted text-xs sq-vf-head")
            for item in VF_ITEMS:
                done, sub = vf_status(item, SP, ctx)
                with ui.element("div").classes("sq-vf-item" + (" on" if item == k else "")) \
                        .on("click", lambda item=item: select(item)).mark(f"vf:{item}"):
                    ui.label(t(f"vf.{item}")).classes("sq-vf-name")
                    ui.label(sub).classes("sq-vf-sub" + (" ok" if done else ""))
        if not force and shown["k"] == (k, _body_key(k)):
            return
        shown["k"] = (k, _body_key(k))
        head.clear()
        with head:
            ui.label(t(f"vf.{k}")).classes("sq-serif text-xl")
            if k != "walkforward":                    # 滚动优化自己有说明
                ui.label(t(f"spv.{k}_body")).classes("sq-muted text-sm")
        body.clear()
        with body:
            if k == "walkforward":
                _walkforward(SP, ctx)
            elif not same_result(SP, ctx):
                notice(t("vf.need_run") if k == "credibility" else t("spv.need_run"), "info")
                ui.button(t("spv.go_backtest"), icon="arrow_back", on_click=go_backtest) \
                    .props("outline no-caps").classes("self-start").mark("spv_go_backtest")
            elif k == "credibility":
                credibility_box(base_checks(SP["result"][0]))
            elif not phase_keys(SP["result"][1]):
                notice(t("spv.phase_na"), "info")
            else:
                _sel_checks(SP, [], ctx)

    def _body_key(k):
        """右侧内容依赖的东西：回测结果换了要重画；滚动优化的面板自己会刷新结果，不跟着重画"""
        if k == "walkforward":
            return None
        r = SP.get("result")
        return (id(r[0]) if r else None, same_result(SP, ctx))

    draw()
    return SimpleNamespace(refresh=draw, select=select)
