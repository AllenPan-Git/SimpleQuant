"""回测页和参数优化页共用的设置区块（对应 ui/components.py）"""

import datetime as dt
from dataclasses import dataclass, field

from nicegui import ui

from simplequant import strategies
from simplequant.data import library
from simplequant.engine import BrokerConfig, COST_PRESETS
from simplequant.rules import INDICATORS, required_columns, macro_columns
from simplequant.strategies import TEMPLATES
from gui import state
from gui.common import t, p, lang
from gui.widgets import section, notice
from ui import shared
from ui.shared import TILE_ROWS, TILE_HELP, fmt_metric, dataset_label

DENSE = "outlined dense options-dense"


def require_data() -> dict | None:
    """本地数据库为空时给出提示并返回 None"""
    metas = library.list_datasets()
    if not metas:
        notice(t("bt.no_data"), "info")
        ui.button(t("nav.go_data"), on_click=lambda: ui.navigate.to("/data")) \
            .props("flat no-caps dense color=primary icon-right=arrow_forward")
        return None
    return {m.id: m for m in metas}


# ---------------- 数据 ----------------
@dataclass
class DataPick:
    """数据选择区块的当前值（控件改动时原地更新，并调用 on_change）"""
    ids: list = field(default_factory=list)
    start: dt.date | None = None
    end: dt.date | None = None
    error: str = ""
    freqs: set = field(default_factory=set)

    @property
    def date_range(self):
        return (self.start, self.end) if self.ids and not self.error and self.start and self.end else None


def data_selector(by_id: dict, key: str, on_change) -> DataPick:
    """选数据 + 日期区间；key 区分页面（其他页面可在 state.STATE[f"{key}_ids"] 预选数据）"""
    lg = lang()
    ids = [i for i in state.STATE.get(f"{key}_ids", [next(iter(by_id))]) if i in by_id]
    pick = DataPick(ids=ids)

    with section(t("bt.data"), "storage"):
        sel = ui.select({i: dataset_label(m, lg) for i, m in by_id.items()}, label=t("bt.pick_assets"),
                        value=ids, multiple=True).props(DENSE + " use-chips").classes("w-full").mark(f"{key}_ids")
        with ui.row().classes("w-full items-start gap-3"):
            start = ui.input(t("data.start")).props("outlined dense type=date").classes("w-44")
            end = ui.input(t("data.end")).props("outlined dense type=date").classes("w-44")
            span = ui.label().classes("sq-muted text-sm self-center")
        err = ui.label().classes("text-negative text-sm")

    saved_range = state.STATE.get(f"{key}_range")

    def bounds():
        if not pick.ids:
            return None, None
        return (max(dt.date.fromisoformat(by_id[i].start) for i in pick.ids),
                min(dt.date.fromisoformat(by_id[i].end) for i in pick.ids))

    def refresh(reset_range: bool):
        lo, hi = bounds()
        pick.freqs = {by_id[i].freq for i in pick.ids}
        pick.error = ""
        if len(pick.freqs) > 1:
            pick.error = t("bt.mixed_freq")
        elif pick.ids and lo >= hi:
            pick.error = t("bt.no_overlap")
        if lo and hi and lo < hi:
            if reset_range or not pick.start:
                pick.start, pick.end = lo, hi
            pick.start, pick.end = max(pick.start, lo), min(pick.end, hi)
            start.props(f"min={lo} max={hi}")
            end.props(f"min={lo} max={hi}")
            start.value, end.value = str(pick.start), str(pick.end)
            span.text = t("gui.available", lo=lo, hi=hi)
        err.text = pick.error
        err.set_visibility(bool(pick.error))
        state.STATE[f"{key}_ids"] = list(pick.ids)
        if pick.start:
            state.STATE[f"{key}_range"] = (pick.start, pick.end)

    def on_ids(e):
        pick.ids = list(e.value or [])
        refresh(reset_range=True)
        on_change()

    def on_date(e, which):
        try:
            d = dt.date.fromisoformat(e.value)
        except (TypeError, ValueError):
            return
        lo, hi = bounds()
        if lo and hi:
            d = min(max(d, lo), hi)
        setattr(pick, which, d)
        if pick.start and pick.end and pick.start > pick.end:
            pick.error = t("bt.no_overlap")
        state.STATE[f"{key}_range"] = (pick.start, pick.end)
        on_change()

    if saved_range and pick.ids:
        pick.start, pick.end = saved_range
    refresh(reset_range=not (saved_range and pick.ids))
    sel.on_value_change(on_ids)
    start.on_value_change(lambda e: on_date(e, "start"))
    end.on_value_change(lambda e: on_date(e, "end"))
    return pick


def load_prices(by_id: dict, pick: DataPick) -> dict:
    return shared.slice_prices({by_id[i].name: library.load(i) for i in pick.ids}, pick.date_range)


def prepare_prices(by_id: dict, pick: DataPick, broker: BrokerConfig, spec: dict | None = None) -> tuple[dict, dict, dict]:
    """
    回测用的行情；现金分红模式下换成「不复权价 + 分红」计算的行情（可能要联网下载，放在后台线程调用）。
    规则用到利率条件时并入利率数据。
    返回 (行情, 没能改用现金分红的 {名称: 原因}, 按复权价补上分红的 {名称: 日期})
    """
    prices = with_rates(load_prices(by_id, pick), spec)
    if broker.dividend != "cash":
        return prices, {}, {}
    from simplequant.data import cash_dividend
    prices, skipped, patched = cash_dividend.prepare([(by_id[i].name, by_id[i], prices[by_id[i].name])
                                                      for i in pick.ids])
    return with_rates(prices, spec), skipped, patched


def rates_needed(spec: dict | None) -> set:
    return macro_columns(spec["rule"]) if spec and spec.get("kind") == "rule" else set()


def with_rates(prices: dict, spec: dict | None) -> dict:
    """规则用到利率 / 信用利差时，把它们并入各标的行情；本机没有利率数据时报错（请先在数据页下载）"""
    cols = rates_needed(spec)
    if not cols:
        return prices
    from simplequant.bonds.rates import RatesStore, attach
    st = RatesStore()
    if not st.ready():
        raise ValueError(t("rates.need"))
    return attach(prices, cols, st.load())


def dividend_notices(skipped: dict, patched: dict):
    """现金分红模式下：哪些标的仍按分红再投资、哪些补了分红数据"""
    zh = lang() == "zh"
    sep, lst = ("；", "、") if zh else ("; ", ", ")
    if skipped:
        items = sep.join(f"{n}（{t('cd.reason_' + r)}）" if zh else f"{n} ({t('cd.reason_' + r)})"
                         for n, r in skipped.items())
        notice(t("cd.skipped", items=items), "info", "warning")
    if patched:
        items = sep.join(f"{n}{'：' if zh else ': '}{lst.join(map(str, ds))}" for n, ds in patched.items())
        notice(t("cd.patched", items=items), "info", "info")


# ---------------- 策略 ----------------
def strategy_selector(key: str, on_change) -> dict:
    """返回 {"spec": 策略描述, "label": 显示名}（原地更新）"""
    lg = lang()
    options = {}
    cur = state.STATE.get("current_spec")
    if cur:
        options["__current__"] = t("bt.opt_current", name=cur["name"])
    saved = strategies.list_strategies()
    for n, s in saved.items():
        if strategies.runnable_on_single_assets(s):
            options[f"saved:{n}"] = t("bt.opt_saved", name=n)
    for k, tpl in TEMPLATES.items():
        options[f"tpl:{k}"] = t("bt.opt_template", name=p(tpl.label))
    choice = state.STATE.get(f"{key}_strategy")
    if choice not in options:
        choice = next(iter(options))
    out = {}

    def resolve(c):
        if c == "__current__":
            spec, name = {k: v for k, v in cur.items() if k != "name"}, cur["name"]
        elif c.startswith("saved:"):
            spec, name = saved[c[6:]], c[6:]
        else:
            spec, name = {"kind": "template", "template": c[4:], "params": {}}, p(TEMPLATES[c[4:]].label)
        out.update(spec=spec, label=options[c], name=name)
        desc.text = strategies.describe(spec, lg)

    with section(t("bt.strategy"), "tune"):
        def on_pick(e):
            state.STATE[f"{key}_strategy"] = e.value
            resolve(e.value)
            on_change()
        ui.select(options, label=t("bt.pick_strategy"), value=choice, on_change=on_pick) \
            .props(DENSE).classes("w-full").mark(f"{key}_strategy")
        desc = ui.label().classes("sq-code w-full")
        ui.button(t("bt.edit_strategy"), icon="edit", on_click=lambda: ui.navigate.to("/strategy")) \
            .props("flat no-caps dense color=primary").classes("self-start")
    resolve(choice)
    return out


# ---------------- 资金与费率 ----------------
def broker_settings(key: str, default_preset: str = "etf", default_cash: int = 100_000, show_t1: bool = True,
                    show_dividend: bool = False):
    """返回一个函数，调用时按当前控件值生成 BrokerConfig"""
    saved = state.STATE.setdefault(f"{key}_broker", {"cash": default_cash, "preset": default_preset, "t1": True,
                                                    "slippage": 5.0, "custom": {}})
    saved.setdefault("dividend", "reinvest")
    presets = list(COST_PRESETS)
    with section(t("bt.costs"), "account_balance"):
        with ui.row().classes("w-full items-center gap-3"):
            cash = ui.number(t("bt.cash"), value=saved["cash"], min=10_000, max=100_000_000, step=10_000,
                             precision=0, format="%.0f").props("outlined dense").classes("w-48")
            preset = ui.select({k: p(COST_PRESETS[k]["label"]) for k in presets}, label=t("bt.cost_preset"),
                               value=saved["preset"]).props(DENSE).classes("w-48")
            t1 = ui.switch(t("bt.t1"), value=saved["t1"]).tooltip(t("bt.t1_help")) if show_t1 else None
        div = None
        if show_dividend:
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(t("bt.dividend")).classes("text-sm")
                div = ui.radio({"reinvest": t("bt.div_reinvest"), "cash": t("bt.div_cash")}, value=saved["dividend"])                     .props("inline dense").mark(f"{key}_dividend")
                ui.icon("help_outline").classes("sq-muted").tooltip(t("bt.dividend_help"))                     .props("max-width=420px")
        with ui.expansion(t("bt.cost_detail")).classes("w-full").props("dense"):
            with ui.row().classes("w-full gap-3"):
                comm = ui.number(t("bt.commission"), min=0, max=30, step=0.1).props("outlined dense").classes("w-40")
                minc = ui.number(t("bt.min_comm"), min=0, max=20, step=1).props("outlined dense").classes("w-40")
                stamp = ui.number(t("bt.stamp"), min=0, max=20, step=0.5).props("outlined dense").classes("w-40")
                slip = ui.number(t("bt.slippage"), value=saved["slippage"], min=0, max=100, step=1) \
                    .props("outlined dense").classes("w-40")

    def fill_costs():
        """换费率预设时填入该预设的默认值（改过的保留在 custom[预设] 里）"""
        c = COST_PRESETS[preset.value]
        mine = saved["custom"].get(preset.value, {})
        comm.value = mine.get("commission", round(c["commission"] * 1e4, 4))
        minc.value = mine.get("min_commission", c["min_commission"])
        stamp.value = mine.get("stamp_duty", round(c["stamp_duty"] * 1e4, 4))

    def remember(_=None):
        saved.update(cash=cash.value or default_cash, preset=preset.value, slippage=slip.value or 0,
                     t1=t1.value if t1 else True, dividend=div.value if div else "reinvest")
        saved["custom"][preset.value] = {"commission": comm.value or 0, "min_commission": minc.value or 0,
                                         "stamp_duty": stamp.value or 0}

    fill_costs()
    preset.on_value_change(lambda e: (fill_costs(), remember()))
    for el in (cash, comm, minc, stamp, slip) + ((t1,) if t1 else ()) + ((div,) if div else ()):
        el.on_value_change(remember)

    def make() -> BrokerConfig:
        return BrokerConfig(cash=float(cash.value or default_cash), commission=(comm.value or 0) / 1e4,
                            min_commission=float(minc.value or 0), stamp_duty=(stamp.value or 0) / 1e4,
                            slippage=(slip.value or 0) / 1e4, t_plus_1=t1.value if t1 else True,
                            dividend=div.value if div else "reinvest")
    return make


def unadjusted_warning(by_id: dict, ids: list) -> str | None:
    """选了不复权数据：除权日价格下跳会产生假信号（聚宽实测 510300 多出两次假卖出）"""
    names = [by_id[i].name for i in ids if not by_id[i].adjust]
    return t("bt.unadjusted", names="、".join(names) if lang() == "zh" else ", ".join(names)) if names else None


def missing_factor_warnings(by_id: dict, ids: list, spec: dict) -> list[str]:
    """规则用到估值/换手因子，但所选数据没有对应列时的提示"""
    if spec.get("kind") != "rule":
        return []
    need, out = required_columns(spec["rule"]) - macro_columns(spec["rule"]), []
    if macro_columns(spec["rule"]):
        from simplequant.bonds.rates import RatesStore
        if not RatesStore().ready():
            out.append(t("rates.need"))
    for i in ids:
        lacking = sorted(need - set(library.load(i).columns))
        if lacking:
            names = ", ".join(p(INDICATORS[c]["label"]) for c in lacking)
            out.append(t("bt.missing_factor", name=by_id[i].name, cols=names))
    return out


# ---------------- 结果 ----------------
def metric_tile(label: str, value: str, delta: str | None = None, help_text: str | None = None,
                delta_good: bool | None = None):
    """单个指标：上方一条细线 + 小字标签 + 大号数字（研究报告风格，不用卡片）"""
    with ui.column().classes("sq-tile gap-0 min-w-0") as card:
        ui.label(label).classes("sq-muted text-xs")
        ui.label(value).classes("sq-fig text-2xl")
        if delta:
            # A 股习惯：涨红跌绿，与图表一致
            ui.label(delta).classes("text-xs " + ("sq-muted" if delta_good is None else
                                                  "sq-up" if delta_good else "sq-down"))
    if help_text:
        card.tooltip(help_text)
    return card


def _fig_cell(k: str, m: dict):
    delta = cls = None
    if k == "total_return":
        ex = m.get("excess_return")
        delta = t("bt.excess", v=fmt_metric("excess_return", ex))
    v = m.get(k)
    if k in ("total_return", "cagr", "excess_return") and v == v and v is not None:
        cls = "sq-up" if v >= 0 else "sq-down"
    elif k == "max_drawdown":
        cls = "sq-down"
    with ui.element("div") as cell:
        ui.label(t(f"m.{k}")).classes("k")
        ui.label(fmt_metric(k, v)).classes("v " + (cls or ""))
        if delta:
            ui.label(delta).classes("d")
    if k in TILE_HELP:
        cell.tooltip(t(TILE_HELP[k]))


def metric_tiles(m: dict):
    """结果指标：两行细线分栏，第一行大号数字"""
    for i, row in enumerate(TILE_ROWS):
        with ui.element("div").classes("sq-figs" + (" sq-figs-sub" if i else ""))                 .style(f"grid-template-columns: repeat({len(row)}, minmax(0, 1fr))"):
            for k in row:
                _fig_cell(k, m)


def walkforward_results(res, labels: dict, metric: str, stitch_label: str, note: str, fmt_param=None):
    """滚动优化结果（参数优化页和选股页共用）；fmt_param(列名, 值) 把离散参数显示成文字"""
    from ui.charts import walkforward_chart, param_stability_chart
    from gui.widgets import plot, df_table
    lg = lang()
    m, bm = res.metrics, res.baseline_metrics
    ui.separator().classes("my-2")
    with ui.column().classes("gap-1"):
        ui.label(t("wf.results")).classes("text-xl font-semibold")
        ui.label(t("wf.caption", n=len(res.windows), start=f"{res.windows['test_start'].iloc[0]:%Y-%m-%d}",
                   end=f"{res.windows['test_end'].iloc[-1]:%Y-%m-%d}", stitch=stitch_label)).classes("sq-muted text-sm")
    diff = m["total_return"] - bm["total_return"]
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
        metric_tile(t("wf.oos_return"), fmt_metric("total_return", m["total_return"]),
                    t("wf.vs_base", v=fmt_metric("total_return", diff)), None, diff >= 0)
        metric_tile(t("m.cagr"), fmt_metric("cagr", m["cagr"]))
        metric_tile(t("m.sharpe"), fmt_metric("sharpe", m["sharpe"]))
        metric_tile(t("m.max_drawdown"), fmt_metric("max_drawdown", m["max_drawdown"]))
        metric_tile(t("wf.wfe"), "—" if res.wfe != res.wfe else f"{res.wfe:.0%}", None, t("wf.wfe_help"))
    good = res.wfe == res.wfe and res.wfe >= 0.5 and m["total_return"] >= bm["total_return"]
    notice(t("wf.verdict_good" if good else "wf.verdict_bad"), "lightbulb", "info" if good else "warning")
    with ui.card().classes("w-full p-2 gap-1"):
        plot(walkforward_chart(res.equity, res.windows, lg))
        ui.label(note).classes("sq-muted text-xs px-2")

    params, windows = res.params.copy(), res.windows.copy()
    if fmt_param:
        for c in labels:
            params[c] = params[c].map(lambda v, c=c: fmt_param(c, v))
            windows[c] = windows[c].map(lambda v, c=c: fmt_param(c, v))
    with ui.row().classes("w-full gap-4 items-start no-wrap max-md:flex-wrap"):
        with ui.card().classes("gap-1 p-3").style("flex: 2; min-width: 320px"):
            ui.label(t("wf.stability")).classes("font-semibold")
            plot(param_stability_chart(params, labels, lg))
            ui.label(t("wf.stability_note")).classes("sq-muted text-xs")
        with ui.card().classes("gap-2 p-3").style("flex: 3; min-width: 360px"):
            ui.label(t("wf.windows")).classes("font-semibold")
            tbl = windows
            for c in ("train_start", "train_end", "test_start", "test_end"):
                tbl[c] = tbl[c].dt.date
            tbl = tbl.drop(columns=["window", "train_cagr"])
            for c in ("test_return", "test_cagr", "test_max_drawdown"):
                tbl[c] = tbl[c].map(lambda v: fmt_metric("total_return", v))
            tbl["train_metric"] = tbl["train_metric"].map(lambda v: fmt_metric(metric, v))
            df_table(tbl.rename(columns={**labels, **{c: t(f"wf.col_{c}") for c in tbl.columns if c not in labels},
                                         "train_metric": t("wf.col_train_metric", m=t(f"m.{metric}"))}))
    for n in res.notes:
        ui.label(n).classes("sq-muted text-xs")
