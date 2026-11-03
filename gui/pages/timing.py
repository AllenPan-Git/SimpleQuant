"""择时工作台（新版界面，界面重构计划 4.2）：策略编辑与回测合在一页

顶部是这个策略的四步进度；设置区（数据、策略编辑器、资金与费率、名称与保存）收起时是一行摘要，右边的按钮跟着页面滚动；
下面是回测报告。设置区和报告各自重画：点「开始回测」只重画报告，编辑到一半的内容不受影响（编辑器的内容在
state.strategy() 里，数据、资金等在 state.STATE 的 bt_* 里，与经典的策略页、回测页共用）。运行不自动保存。
"""

from nicegui import ui

from simplequant import strategies
from simplequant.data import library
from gui import state
from gui.common import t, lang
from gui.components import require_data, strategy_progress
from gui.layout import frame, page_title
from gui.plan_io import import_button
from gui.pages.strategy import Ctx, strategy_editor, editor_spec, code_ok, saved_strategies, load_spec
from gui.pages.backtest import setup_bar, report
from ui.shared import progress_steps, saved_name, paper_count, spec_key, timing_verify


def page():
    S = state.strategy()
    with frame("/timing"):
        page_title("/timing", t("timing.subtitle"))
        _take_handoff(S)
        by_id = require_data()
        if by_id is None:            # 没有数据也能先写策略、保存
            import_button().classes("mt-2")
            _editor_only(S)
            return
        _workbench(S, by_id)


def _take_handoff(S):
    """其他页面带过来的策略（state.STATE["timing_spec"]，见 gui/routes.py）：放进编辑器"""
    spec = state.STATE.pop("timing_spec", None)
    if spec:
        load_spec(S, {k: v for k, v in spec.items() if k != "name"}, spec.get("name", ""))


def _workbench(S, by_id: dict):
    BT = state.STATE.setdefault("bt", {})
    ctx = Ctx()
    accounts = _accounts()
    seen = {"ai": S.get("ai_result")}           # 已处理过的 AI 结果（新生成的才预选它识别出的标的）

    @ui.refreshable
    def progress():
        strategy_progress(_steps(S, BT, accounts), on_click=lambda k: go_step(k), mark="prog")
    progress()

    def part(on_change, on_broker):
        return _strategy_part(S, ctx, on_change, on_broker)
    bar = setup_bar(by_id, BT, part, sticky=True, open_=True if state.STATE.pop("timing_open", None) else None)

    @ui.refreshable
    def results():
        report(BT, bar.exp_menu)
    with ui.column().classes("w-full gap-0").mark("timing_report") as report_box:
        results()
    bar.report = results.refresh

    def on_name(v: str):
        if v != S["name"]:
            S["name"] = v
            ctx.changed()

    def on_saved(name: str):
        S["name"] = name
        bar.strat["saved_list"].refresh()
        progress.refresh()

    def on_check():
        # AI 新生成的策略：本地数据库里有它识别出的标的时自动选中（与经典策略页「使用此策略回测」相同）
        if S.get("ai_result") is not seen["ai"]:
            seen["ai"] = S.get("ai_result")
            ids = [m.id for m in library.list_datasets() if m.symbol in (S.get("ai_symbols") or [])]
            if ids and set(ids) != set(bar.pick.ids):
                bar.pick.set_ids(ids)            # 会再次调用 check()
                return
        progress.refresh()

    def on_ran(ok: bool):
        if bar.setup.is_deleted:
            return
        if ok:
            _scroll_into_view(bar.setup_row, away_only=True)       # 设置已收起，摘要条下面就是报告
        else:
            bar.setup.value = True                                 # 出错：展开设置，方便修改
        progress.refresh()

    bar.on_name, bar.on_saved, bar.on_check, bar.on_ran, bar.on_phase = on_name, on_saved, on_check, on_ran, progress.refresh

    async def go_step(key: str):
        """点进度的一步：设置 → 展开设置区；回测 → 已有结果时看报告，否则运行；验证 → 参数优化；模拟盘 → 模拟盘页"""
        if key == "setup":
            bar.setup.value = True
            _scroll_into_view(bar.setup_row)
        elif key == "backtest":
            if _same_result(S, BT):
                _scroll_into_view(report_box)
            elif bar.ready():
                await bar.run()
        elif key == "verify":
            _to_verify(bar)
        else:
            ui.navigate.to("/paper")

    ctx.changed()          # 画出预览，并按编辑器的内容检查设置（提示、按钮、摘要条）
    # 从首页「最近的报告」、导入方案等打开：设置已恢复，自动重新运行
    if state.STATE.pop("bt_autorun", None) and bar.ready():
        ui.timer(0.05, bar.run, once=True)


def _strategy_part(S, ctx: Ctx, on_change, on_broker) -> dict:
    """设置区里的「策略」一块：完整的编辑器（四种编写方式）+ 已保存的策略；返回给 setup_bar 的 strat（原地更新）"""
    strat = {"full_check": lambda: code_ok(S)}

    def sync():
        spec, errors = editor_spec(S)
        name = S["name"] if S["name"].strip() else ""        # 名称原样保留（输入到一半的空格不被删掉）
        strat.update(spec=spec, errors=errors, name=name, label=name.strip() or t("strat.unnamed"))

    def on_load(n: str):
        """载入已保存的策略：连同保存时的资金与费率一起恢复（与经典回测页选中它时相同）"""
        broker = strategies.strategy_brokers().get(n)
        if broker:
            on_broker(broker)

    with ui.column().classes("w-full gap-3").mark("timing_strategy"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("tune", size="18px").classes("sq-red")
            ui.label(t("bt.strategy")).classes("sq-serif text-base")
        strategy_editor(S, ctx)
        strat["saved_list"] = saved_strategies(S, ctx, on_load=on_load)
    ctx.on_change = lambda errors: (sync(), on_change())
    sync()
    return strat


def _editor_only(S):
    """本地还没有数据：只显示编辑器与保存（数据下载后再回测）"""
    ctx = Ctx()
    box = strategy_editor(S, ctx)
    with box, ui.row().classes("w-full items-end gap-3"):
        name = ui.input(t("bt.save_name"), value=S["name"], placeholder=t("strat.name_ph")) \
            .props("outlined dense").classes("grow").mark("bt_save_name")
        save_btn = ui.button(t("bt.save_strategy"), icon="save", on_click=lambda: save()) \
            .props("outline no-caps").mark("bt_save")

    def on_name(e):
        S["name"] = e.value or ""
        ctx.changed()
    name.on_value_change(on_name)
    ctx.on_change = lambda errors: save_btn.set_enabled(not errors and bool(S["name"].strip()))
    ctx.set_name = name.set_value

    def save():
        spec, errors = editor_spec(S)
        if errors or not S["name"].strip() or not code_ok(S):
            return
        strategies.save_strategy(S["name"].strip(), spec)
        ui.notify(t("strat.saved", name=S["name"].strip()), type="positive")
        saved_list.refresh()

    saved_list = saved_strategies(S, ctx)
    ctx.changed()


def _accounts() -> list:
    """全部模拟账户（进度的「模拟盘」一步用）；读不出时为空"""
    try:
        from simplequant.paper import list_accounts
        return list_accounts()
    except Exception:  # noqa: BLE001
        return []


def _same_result(S, BT: dict) -> bool:
    """报告里的回测与编辑器当前的策略规则相同"""
    return BT.get("result") is not None and spec_key(BT.get("spec")) == spec_key(editor_spec(S)[0])


def _steps(S, BT: dict, accounts: list) -> list:
    """工作台顶部的四步进度：已保存（与某个已保存策略规则相同）/ 有改动未保存（名称是已保存的策略，规则改过）；
    回测、验证只算与当前规则相同的那一次"""
    spec, errors = editor_spec(S)
    name = S["name"].strip()
    try:
        saved_all = strategies.list_strategies()
    except Exception:  # noqa: BLE001
        saved_all = {}
    saved = None if errors else saved_name({"name": name, **spec}, saved_all)
    dirty = saved is None and name in saved_all
    ret = BT["result"].metrics.get("total_return") if _same_result(S, BT) else None
    verify = timing_verify(spec, BT, state.STATE.get("opt") or {})
    return progress_steps(lang(), saved, dirty, ret, verify, paper_count(spec, accounts))


def _to_verify(bar):
    """去验证（参数优化）：工作台的「验证」标签做好前，先带着当前设置去经典的参数优化页"""
    S = state.STATE
    spec, errors = editor_spec(state.strategy())
    if not errors:
        S["current_spec"] = {"name": bar.strat["label"], **spec}
        S["opt_strategy"] = "__current__"
    for k in ("ids", "range"):
        if f"bt_{k}" in S:
            S[f"opt_{k}"] = S[f"bt_{k}"]
    ui.navigate.to("/optimize")


def _scroll_into_view(el, away_only: bool = False):
    """等折叠动画（0.3 秒）结束后滚到 el；away_only：el 已在窗口上半部分时不滚"""
    cond = "if (top >= 0 && top < window.innerHeight * 0.5) return;" if away_only else ""
    ui.run_javascript(f"""setTimeout(() => {{
        const el = getHtmlElement({el.id});
        if (!el) return;
        const top = el.getBoundingClientRect().top;
        {cond}
        el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
    }}, 400)""")
