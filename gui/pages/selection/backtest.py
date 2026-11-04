"""选股页「选股回测」标签页，以及运行导出的选股脚本"""

import calendar
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from nicegui import run, ui

from simplequant import llm, strategies
from simplequant.engine import COST_PRESETS
from simplequant.engine.credibility import check_backtest, check_ml, check_phase
from simplequant.engine.phase import run_selection_phases, selection_cycle, phases
from simplequant.stocks import UNIVERSES, FACTORS, REBALANCE, WEIGHTING, DIVIDEND, DEFAULT_FILTERS, run_selection
from simplequant.stocks import universe as U, factors_for
from simplequant.stocks import ml as ML
from simplequant.stocks.selection import ic_history_short, skip_months, needs_fin, needs_risk, MARKET_INDS
from simplequant.stocks.panel import STOCK_FILTERS, RISK_FILTERS
from simplequant.bonds.panel import CB_FILTERS, CB_DEFAULT_FILTERS
from simplequant.system import open_folder
from gui import state, history, support
from gui.common import t, p, lang, new_ui
from gui.missing_data import stock_bar
from gui.components import broker_settings, metric_tiles, metric_tile, credibility_box, bench_label
from gui.pages.stock_overview import picks_dialog
from gui.plan_io import import_button, export_selection_bytes, plan_filename, plan_check_notice, restore_broker
from simplequant import plan as plan_mod
from gui.widgets import section, df_table, fmt_table, plot, notice, hint
from ui.charts import equity_chart
from ui.shared import export_filename, num, orders_table, logs_table, with_ids
from .common import (DENSE, POS_DEFAULT, factor_label, kind_defaults, market_spec, reb_label, spec_panel,
                     spec_panel_start, switch_kind)
from .broker import broker_default, broker_key


# ================= 选股回测 =================
# 过滤条件分组：(SP 里的键, 条件表, 组名, 提示后缀)。值为 None = 不限，只为设了值的条件画一行
FILTER_GROUPS = {
    "stock": [("stockf", STOCK_FILTERS, "sp.filter_group_stock", "sp.stock_filter_note"),
              ("riskf", RISK_FILTERS, "sp.filter_group_risk", "sp.risk_filter_note")],
    "cb": [("cbf", CB_FILTERS, "cb.filter_group", None)],
}
# 「添加条件」时先填的建议值，用户再改
FILTER_SUGGEST = {
    "stockf": {"max_price": 50.0, "mcap_rank_min": 800.0, "mcap_rank_max": 1800.0, "min_mcap_pct": 10.0},
    "riskf": {"max_debt_ratio": 70.0, "min_current_ratio": 1.0, "min_cfo_np": 0.5},
    "cbf": {"max_price": 130.0, "min_amount": 1000.0, "min_remain_years": 0.5, "max_premium": 30.0,
            "max_call_days": 10.0},
}


def filter_editor(SP, kind: str, changed):
    """过滤条件：设了值的条件各占一行（可删除），其余从「＋添加条件」菜单里加"""
    groups = FILTER_GROUPS.get(kind, [])
    mark_prefix = "cb_" if kind == "cb" else "sp_"
    box = ui.column().classes("w-full gap-1")

    def draw():
        box.clear()
        with box:
            ui.label(t("sp.filters")).classes("text-xs sq-muted")
            any_set = False
            for sk, table, _, note in groups:
                vals = SP[sk]
                for k, m in table.items():
                    if vals.get(k) is None:
                        continue
                    any_set = True
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.label(p(m["label"])).classes("text-sm w-64")

                        def on_val(e, vals=vals, k=k):
                            # 清空输入框 = 不限；这一行先留着，下次重画时消失（不在输入中途重画，免得丢焦点）
                            vals[k] = float(e.value) if e.value not in (None, "") else None
                            changed()
                        ui.number(value=vals[k], on_change=on_val).props("outlined dense").classes("w-32") \
                            .mark(f"{mark_prefix}{k}")
                        ui.icon("help_outline").classes("sq-muted cursor-help") \
                            .tooltip(p(m["help"]) + (t(note) if note else ""))

                        def on_del(vals=vals, k=k):
                            vals[k] = None
                            draw()
                            changed()
                        ui.button(icon="close", on_click=on_del).props("flat round dense size=sm") \
                            .tooltip(t("sp.del_filter")).mark(f"sp_del_filter:{k}")
            if not any_set:
                ui.label(t("sp.filters_empty")).classes("sq-muted text-sm")

            missing = [(sk, gk, [(k, m) for k, m in table.items() if SP[sk].get(k) is None])
                       for sk, table, gk, _ in groups]
            missing = [g for g in missing if g[2]]
            with ui.button(t("sp.add_filter"), icon="add").props("flat no-caps dense color=primary") \
                    .classes("self-start").mark("sp_add_filter") as add_btn:
                with ui.menu():
                    for i, (sk, gk, items) in enumerate(missing):
                        if i:
                            ui.separator()
                        ui.item_label(t(gk)).props("header").classes("text-xs sq-muted")
                        for k, m in items:
                            def on_add(sk=sk, k=k):
                                SP[sk][k] = FILTER_SUGGEST.get(sk, {}).get(k, 0.0)
                                draw()
                                changed()
                            ui.menu_item(p(m["label"]), on_click=on_add).mark(f"sp_add_filter:{k}")
            add_btn.set_enabled(bool(missing))
    draw()


def advanced_box(SP, open_key: str, mark: str, items: list[str]):
    """内层「高级设置」折叠区：标题后面用小字列出改过默认值的项；有改过的项时默认展开（导入方案后能看到）。
    返回 (折叠区, 更新标题小字的函数)"""
    exp = ui.expansion(value=bool(items) or bool(SP.get(open_key))).props("dense") \
        .classes("w-full sq-adv").mark(mark)
    with exp.add_slot("header"), ui.row().classes("items-center gap-2 no-wrap min-w-0 py-1"):
        ui.icon("tune").classes("sq-muted")
        ui.label(t("sp.advanced")).classes("whitespace-nowrap")
        summary = ui.label().classes("sq-muted text-xs ellipsis").mark(f"{mark}_summary")
    exp.on_value_change(lambda e: SP.__setitem__(open_key, bool(e.value)))

    def update(items: list[str]):
        summary.text = "· " + ("、" if lang() == "zh" else ", ").join(items) if items else ""
    update(items)
    return exp, update


def _months_text(months, lg: str) -> str:
    return ("、" if lg == "zh" else ", ").join(str(m) if lg == "zh" else calendar.month_abbr[m] for m in sorted(months))


def _adv_rule_items(SP, kind: str, lg: str) -> list[str]:
    """「选股与调仓」高级设置里改过默认值的项（默认值以 sp_state / kind_defaults 为准）"""
    d = kind_defaults(kind)
    out = []
    if SP.get("keep_n"):
        out.append(t("sp.chip_keep", m=int(SP["keep_n"])))
    if SP.get("skip"):
        out.append(t("sp.chip_skip", months=_months_text(SP["skip"], lg)))
    if int(SP["min_list"]) != d["min_list"]:
        out.append(t("sp.adv_min_days", n=int(SP["min_list"])))
    if int(SP["pos"]) != POS_DEFAULT:
        out.append(t("sp.adv_pos", v=int(SP["pos"])))
    if kind == "stock" and SP.get("dividend", "reinvest") != d["dividend"]:
        out.append(t("sp.adv_dividend_cash"))
    return out


def _adv_factor_items(SP) -> list[str]:
    """「因子与权重」高级设置里打开了的中性化"""
    return [t(k) for k, on in (("sp.neutral_industry", SP["ni"]), ("sp.neutral_size", SP["ns"])) if on]


def current_spec(SP) -> dict:
    cb = U.kind(SP["universe"]) == "cb"
    filters = {"exclude_st": bool(SP["excl_st"]) and not cb, "min_list_days": int(SP["min_list"])}
    if cb:
        filters.update({k: SP["cbf"].get(k) for k in CB_FILTERS})
    else:                                   # 只写设了值的，没用排雷/价格/市值条件的策略与原来一字不差
        filters.update({k: v for k, v in (SP.get("stockf") or {}).items() if v is not None})
        filters.update({k: v for k, v in (SP.get("riskf") or {}).items() if v is not None})
    top_n = int(SP["top_n"])
    keep_n = int(SP["keep_n"]) if SP.get("keep_n") not in (None, "", 0) else 0
    return {"kind": "selection", "universe": SP["universe"],
            "factors": [dict(f) for f in SP["factors"]], "top_n": top_n,
            **({"keep_n": keep_n} if keep_n > top_n else {}),
            "rebalance": SP["reb"] if SP["reb"] != "n" else int(SP["reb_n"]),
            **({"skip_months": sorted(SP["skip"])} if SP.get("skip") else {}),
            **({"market": m} if (m := market_spec(SP)) else {}),
            **({"drawdown_stop": float(SP["dd_stop"])} if float(SP.get("dd_stop") or 0) > 0 else {}),
            "filters": filters,
            "position_pct": int(SP["pos"]), "weighting": SP["weighting"], "ic_lookback": int(SP["lookback"]),
            **({"ml": {"model": SP["ml_model"], "window": int(SP["ml_window"]), **SP["ml_params"][SP["ml_model"]]}}
               if SP["weighting"] == "ml" else {}),
            "neutralize": {"industry": bool(SP["ni"]), "size": bool(SP["ns"])},
            **({"dividend": "cash"} if SP.get("dividend") == "cash" and not cb else {})}


def apply_spec(SP, spec: dict, name: str = ""):
    """把选股策略描述填进表单（载入已保存的策略、AI 生成的策略、滚动优化的最新参数都用它）"""
    old = U.kind(SP["universe"])
    SP["universe"] = spec.get("universe", SP["universe"])
    kind = U.kind(SP["universe"])
    switch_kind(SP, old, kind)
    SP["factors"] = [{"key": f["key"], "weight": float(f.get("weight", 1)), "direction": int(f.get("direction", 1))}
                     for f in spec["factors"] if f["key"] in factors_for(kind)]   # 已删除的自定义因子跳过
    SP["top_n"] = int(spec["top_n"])
    keep_n = int(spec["keep_n"]) if spec.get("keep_n") not in (None, "", 0) else 0
    SP["keep_n"] = keep_n if keep_n > SP["top_n"] else None
    reb = spec.get("rebalance", "monthly")
    SP["reb"] = reb if reb in REBALANCE else "n"
    if reb not in REBALANCE:
        SP["reb_n"] = int(reb)
    SP["skip"] = skip_months(spec)
    SP["dd_stop"] = float(spec.get("drawdown_stop") or 0)
    market = spec.get("market") or {}
    SP["market"] = with_ids({"buy": market.get("buy") or {"logic": "all", "conditions": []},
                             "sell": market.get("sell") or {"logic": "any", "conditions": []}})
    flt = {**(CB_DEFAULT_FILTERS if kind == "cb" else DEFAULT_FILTERS), **(spec.get("filters") or {})}
    SP["excl_st"], SP["min_list"] = bool(flt["exclude_st"]), int(flt["min_list_days"])
    if kind == "cb":
        SP["cbf"] = {k: flt.get(k) for k in CB_FILTERS}
    else:
        SP["stockf"] = {k: flt.get(k) for k in STOCK_FILTERS}
        SP["riskf"] = {k: flt.get(k) for k in RISK_FILTERS}
    SP["pos"] = int(spec.get("position_pct", POS_DEFAULT))
    SP["weighting"] = spec.get("weighting", "manual")
    SP["lookback"] = int(spec.get("ic_lookback", 252))
    if spec.get("weighting") == "ml" and spec.get("ml"):     # AI 生成的策略不带模型设置：沿用表单里的
        cfg = ML.config(spec)
        SP["ml_model"], SP["ml_window"] = cfg["model"], cfg["window"]
        SP["ml_params"][cfg["model"]] = {k: v for k, v in cfg.items() if k not in ("model", "window")}
    neutral = spec.get("neutralize") or {}
    SP["ni"], SP["ns"] = bool(neutral.get("industry")), bool(neutral.get("size"))
    SP["dividend"] = spec.get("dividend", "reinvest")
    SP["name"] = name


def _backtest(SP, ctx):
    box = ui.column().classes("w-full gap-4")

    def rebuild():
        """表单整体换了内容（载入策略 / AI / 滚动优化结果）；股票池变了要重新载入面板"""
        if SP["universe"] != state.STATE.get("sp_panel_key", (None,))[0]:
            ctx.reload_all()
            return
        box.clear()
        with box:
            _backtest_form(SP, ctx, rebuild)
    ctx.rebuild_bt = rebuild
    with box:
        _backtest_form(SP, ctx, rebuild)


def _backtest_form(SP, ctx, rebuild):
    lg = lang()
    panel = ctx.panel

    # ---- 顶行：设置折叠区（标题行是一行摘要标签）+ 右侧操作按钮（表单很长，按钮吸顶） ----
    # 没有结果时总是展开；有结果时沿用上次的展开 / 收起（SP["setup_open"]，回测成功后收起）
    with ui.row().classes("sq-setup-row sq-setup-sticky w-full items-start gap-3 no-wrap") as setup_row:
        setup = ui.expansion(value=SP.get("setup_open", True) if SP.get("result") else True) \
            .classes("grow min-w-0 sq-setup").mark("sp_setup")
        with setup.add_slot("header"):
            chips = ui.row().classes("sq-chip-row sq-chip-row-wrap w-full items-stretch gap-0")
        with ui.column().classes("sq-setup-actions gap-2 pt-2"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                run_btn = ui.button(t("bt.run"), icon="play_arrow", on_click=lambda: do_run()) \
                    .props("unelevated no-caps").mark("sp_run")
                with ui.button(t("exp.menu"), icon="ios_share").props("outline no-caps") as exp_btn:
                    exp_menu = ui.menu()
                exp_menu.on("before-show", lambda: build_export_menu())
                import_button()
            running = ui.row().classes("items-center gap-2 no-wrap")
            with running:
                ui.spinner(size="sm")
                ui.label(t("sp.running")).classes("sq-muted text-sm")
            running.set_visibility(False)

    def on_setup(e):
        SP["setup_open"] = bool(e.value)
        draw_chips()
    setup.on_value_change(on_setup)

    with setup, ui.column().classes("w-full gap-4 pt-3"):
        # ---- 用一句话描述（AI） ----
        cfg = llm.load_config()
        ready = bool(cfg and cfg.ready)
        with ui.expansion(t("sp.ai"), icon="auto_awesome", value=bool(SP.get("ai"))).classes("w-full q-card"):
            if not ready:
                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    notice(t("ai.need_config"), "key")
                    ui.button(t("nav.go_settings"), on_click=lambda: ui.navigate.to("/settings")) \
                        .props("flat no-caps dense color=primary icon-right=arrow_forward")
            def placeholder():
                return t("sp.ai_base_placeholder") if SP["ai_base"] else t("sp.ai_placeholder")
            ai_text = ui.textarea(t("ai.describe"), value=SP["ai_text"], placeholder=placeholder()) \
                .props("outlined autogrow").classes("w-full").mark("sp_ai_text")

            def on_base(e):
                SP["ai_base"] = bool(e.value)
                ai_text.props(f'placeholder="{placeholder()}"')

            async def ai_go():
                SP["ai_text"] = ai_text.value or ""
                ai_btn.disable()
                try:
                    SP["ai"] = await run.io_bound(llm.translate_selection, SP["ai_text"], llm.get_provider(cfg), lg,
                                                  base=current_spec(SP) if SP["ai_base"] else None)
                except llm.LLMError as e:
                    SP["ai"] = None
                    ui.notify(str(e), type="negative", multi_line=True)
                except Exception as e:  # noqa: BLE001
                    SP["ai"] = None
                    ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
                finally:
                    ai_btn.enable()
                ai_result.refresh()
            with ui.row().classes("items-center gap-4"):
                ai_btn = ui.button(t("ai.generate"), icon="auto_awesome", on_click=ai_go).props("outline no-caps") \
                    .mark("sp_ai_go")
                ui.checkbox(t("sp.ai_base"), value=SP["ai_base"], on_change=on_base).tooltip(t("sp.ai_base_help")) \
                    .mark("sp_ai_base")
            ai_btn.set_enabled(ready and bool(SP["ai_text"].strip()))
            ai_text.on_value_change(lambda e: ai_btn.set_enabled(ready and bool((e.value or "").strip())))

            @ui.refreshable
            def ai_result():
                res = SP.get("ai")
                if not res:
                    return
                notice(res.understood or "—", "psychology")
                if res.unsupported:
                    notice(t("ai.unsupported") + "\n\n" + "\n".join(f"- {u}" for u in res.unsupported), "block",
                           "warning")
                for e in res.errors:
                    notice(e, "error", "error")
                if res.ok:
                    ui.label(strategies.describe(res.spec, lg)).classes("sq-code w-full")

                    def apply_ai():
                        apply_spec(SP, res.spec, res.name)
                        rebuild()
                    ui.button(t("sp.ai_apply"), icon="check", on_click=apply_ai).props("unelevated no-caps") \
                        .classes("self-start").mark("sp_ai_apply")
            ai_result()

        saved = {n: s for n, s in strategies.list_strategies().items() if s.get("kind") == "selection"}
        if saved:
            with ui.row().classes("w-full items-end gap-3"):
                pick_saved = ui.select(list(saved), label=t("sp.load_saved"), value=next(iter(saved))) \
                    .props(DENSE).classes("grow")

                def load_saved():
                    name = pick_saved.value
                    apply_spec(SP, saved[name], name)
                    broker = strategies.strategy_brokers().get(name)
                    if broker:                      # 较早保存的策略没记资金与费率：保持当前设置
                        kind = U.kind(SP["universe"])
                        restore_broker(broker_key(kind), {"broker": broker}, broker_default(kind))
                    rebuild()
                ui.button(t("strat.load"), on_click=load_saved).props("outline no-caps").mark("sp_load_saved")

        # ---- 因子 ----
        with section(t("sp.factor_setup"), "functions"):
            def on_factors(e):
                keys = list(e.value or [])
                old = {f["key"]: f for f in SP["factors"]}
                SP["factors"] = [old.get(k) or {"key": k, "weight": 1.0, "direction": FACTORS[k]["direction"]}
                                 for k in keys]
                factor_rows.refresh()
                changed()
            ui.select({k: factor_label(k) for k in factors_for(panel.kind)}, label=t("sp.pick_factors"),
                      value=[f["key"] for f in SP["factors"]], multiple=True, with_input=True, on_change=on_factors) \
                .props(DENSE + " use-chips").classes("w-full").mark("sp_factors")
            fin_warn = ui.column().classes("w-full")
            warn_shown = {}                  # 新版：上次画的缺数据提示条（种类没变时不重画）
            with ui.row().classes("w-full items-center gap-4"):
                def on_weighting(e):
                    SP["weighting"] = e.value
                    lookback.set_enabled(e.value in ("ic", "icir"))
                    ml_box.set_visibility(e.value == "ml")
                    factor_note.text = note_text()
                    changed()
                with ui.column().classes("gap-0"):
                    ui.label(t("sp.weighting")).classes("text-xs sq-muted").tooltip(t("sp.weighting_help"))
                    ui.radio({w: p(WEIGHTING[w]) for w in WEIGHTING}, value=SP["weighting"], on_change=on_weighting) \
                        .props("inline dense").mark("sp_weighting")
                lookback = ui.number(t("sp.lookback"), value=SP["lookback"], min=60, max=1000, step=21, precision=0,
                                     on_change=lambda e: set_sp("lookback", e.value, int)) \
                    .props("outlined dense").classes("w-36").tooltip(t("sp.lookback_help"))
                lookback.set_enabled(SP["weighting"] in ("ic", "icir"))
            ml_box = ui.column().classes("w-full gap-1").mark("sp_ml_box")
            with ml_box:
                _ml_form(SP, lambda: changed())
            ml_box.set_visibility(SP["weighting"] == "ml")

            @ui.refreshable
            def factor_rows():
                for f in SP["factors"]:
                    with ui.row().classes("w-full items-center gap-4 no-wrap"):
                        ui.label(factor_label(f["key"])).classes("grow font-medium")

                        def on_w(e, f=f):
                            if e.value is not None:
                                f["weight"] = float(e.value)
                                changed()

                        def on_d(e, f=f):
                            f["direction"] = int(e.value)
                            changed()
                        ui.number(t("sp.weight"), value=f["weight"], min=0, max=10, step=0.5, on_change=on_w) \
                            .props("outlined dense").classes("w-28")
                        ui.toggle({1: t("sel.dir_up"), -1: t("sel.dir_down")}, value=int(f["direction"]),
                                  on_change=on_d).props("dense no-caps unelevated toggle-color=primary")
                        if new_ui():         # 因子研究在因子实验室（另一页）
                            ui.button(icon="science", on_click=lambda k=f["key"]: to_lab(k)) \
                                .props("flat round dense size=sm").classes("sq-muted") \
                                .tooltip(t("sp.to_lab")).mark(f"sp_to_lab:{f['key']}")
            factor_rows()

            def to_lab(k):
                """去因子实验室检验这个因子：打开因子研究并选好它"""
                SP["res"]["fkey"] = k
                state.STATE["lab_tab"] = "res"
                ui.navigate.to("/factors")

            def note_text():
                if SP["weighting"] == "ml":
                    return t("sp.ml_note")
                return t("sp.factor_note") + ("" if SP["weighting"] == "manual" else " " + t("sp.ic_fallback_note"))
            factor_note = ui.label(note_text()).classes("sq-muted text-xs")
            factor_adv, factor_adv_update = advanced_box(SP, "factor_adv_open", "sp_factor_advanced",
                                                         _adv_factor_items(SP))
            with factor_adv, ui.row().classes("items-center gap-6 pb-2"):
                ni = ui.switch(t("sp.neutral_industry"), value=SP["ni"], on_change=lambda e: set_sp("ni", e.value)) \
                    .tooltip(t("sp.neutral_industry_help")).mark("sp_ni")
                ni.set_enabled(not panel.industry.dropna().empty)
                ns = ui.switch(t("sp.neutral_size"), value=SP["ns"], on_change=lambda e: set_sp("ns", e.value)) \
                    .tooltip(t("cb.neutral_size_help") if panel.kind == "cb" else t("sp.neutral_size_help"))
                ns.set_enabled(panel.has_fin or panel.kind == "cb")

        # ---- 选股规则 ----
        with section(t("sp.rules"), "filter_alt"):
            with ui.row().classes("w-full items-center gap-4"):
                ui.number(t("cb.top_n" if panel.kind == "cb" else "sp.top_n"), value=SP["top_n"], min=1, max=100, step=1, precision=0,
                          on_change=lambda e: set_sp("top_n", e.value, int)).props("outlined dense").classes("w-32") \
                    .mark("sp_top_n")

                def on_reb(e):
                    SP["reb"] = e.value
                    reb_n.set_enabled(e.value == "n")
                    changed()
                ui.select({**{r: p(REBALANCE[r]) for r in REBALANCE}, "n": t("sp.every_n")}, label=t("sp.rebalance"),
                          value=SP["reb"], on_change=on_reb).props(DENSE).classes("w-40")
                reb_n = ui.number(t("sp.every_n_days"), value=SP["reb_n"], min=1, max=250, precision=0,
                                  on_change=lambda e: set_sp("reb_n", e.value, int)).props("outlined dense").classes("w-36")
                reb_n.set_enabled(SP["reb"] == "n")
                if panel.kind == "stock":
                    ui.switch(t("sp.exclude_st"), value=SP["excl_st"], on_change=lambda e: set_sp("excl_st", e.value))
            filter_editor(SP, panel.kind, lambda: changed())
            if panel.kind == "cb":
                ui.label(t("cb.coupon_note")).classes("sq-muted text-sm")

            # 次要选项：保留前 M 名、空仓月份、最短上市天数、买入资金比例、分红处理
            rules_adv, rules_adv_update = advanced_box(SP, "rules_adv_open", "sp_advanced",
                                                       _adv_rule_items(SP, panel.kind, lg))
            with rules_adv, ui.column().classes("w-full gap-3 pb-2"):
                with ui.row().classes("w-full items-center gap-4"):
                    def on_keep(e):
                        SP["keep_n"] = int(e.value) if e.value not in (None, "") else None
                        changed()
                    ui.number(t("sp.keep_n"), value=SP.get("keep_n"), min=1, max=200, step=1, precision=0,
                              on_change=on_keep).props("outlined dense clearable").classes("w-36") \
                        .tooltip(t("sp.keep_n_help")).mark("sp_keep_n")
                    ui.select({m: t("sp.month", m=m) for m in range(1, 13)}, label=t("sp.skip_months"), multiple=True,
                              value=SP.get("skip", []), on_change=lambda e: set_sp("skip", sorted(e.value or []))) \
                        .props(DENSE + " use-chips clearable").classes("w-56").tooltip(t("sp.skip_months_help")) \
                        .mark("sp_skip")
                    ui.number(t("sp.min_days"), value=SP["min_list"], min=0, max=2000, step=50, precision=0,
                              on_change=lambda e: set_sp("min_list", e.value, int)).props("outlined dense") \
                        .classes("w-36").tooltip(t("sp.min_days_help"))
                with ui.row().classes("w-full items-center gap-8"):
                    with ui.column().classes("gap-0 w-64"):
                        with ui.row().classes("w-full justify-between"):
                            ui.label(t("strat.position")).classes("text-xs sq-muted")
                            pos_lbl = ui.label(f"{SP['pos']}%").classes("sq-num text-xs text-primary")

                        def on_pos(e):
                            pos_lbl.text = f"{int(e.value)}%"
                            set_sp("pos", e.value, int)
                        ui.slider(min=10, max=100, step=5, value=SP["pos"], on_change=on_pos)
                    with ui.row().classes("items-center gap-4") as div_row:
                        with ui.column().classes("gap-0"):
                            ui.label(t("sp.dividend")).classes("text-xs sq-muted").tooltip(t("sp.dividend_help"))
                            div_radio = ui.radio({k: p(v) for k, v in DIVIDEND.items()},
                                                 value=SP.get("dividend", "reinvest"),
                                                 on_change=lambda e: set_sp("dividend", e.value)) \
                                .props("inline dense").mark("sp_dividend")
                            div_radio.tooltip(t("sp.dividend_help"))
                    div_row.set_visibility(panel.kind == "stock")
                div_warn = ui.column()

        # ---- 大盘条件 ----
        from gui.pages.strategy import side_editor      # 与择时策略页共用条件积木

        def dd_stop_editor(SP, on_changed):
            box = ui.column().classes("w-full gap-3")

            def draw():
                box.clear()
                val = float(SP.get("dd_stop") or 0)
                with box:
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-2"):
                            with ui.element("div").classes("sq-icon-chip"):
                                ui.icon("shield")
                            ui.label(t("sp.dd_stop_title")).classes("text-lg font-semibold")

                    if val > 0:
                        with ui.card().classes("w-full p-3").props("flat"):
                            with ui.row().classes("w-full items-center justify-between gap-3"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(t("sp.dd_stop_cond_prefix")).classes("text-sm")
                                    def on_val(e):
                                        if e.value is not None:
                                            SP["dd_stop"] = max(0.0, float(e.value))
                                            on_changed()
                                    ui.number(value=val, min=1, max=90, step=1,
                                              on_change=on_val) \
                                        .props("outlined dense suffix=%").classes("w-28").mark("sp_dd_stop")
                                    ui.label(t("sp.dd_stop_cond_suffix")).classes("text-sm")
                                def on_del():
                                    SP["dd_stop"] = 0
                                    draw()
                                    on_changed()
                                ui.button(icon="delete", on_click=on_del) \
                                    .props("flat round dense color=negative").tooltip(t("sp.dd_stop_del")).mark("del:dd_stop")
                        ui.label(t("sp.dd_stop_help")).classes("sq-muted text-sm")
                    else:
                        ui.label(t("sp.dd_stop_empty")).classes("sq-muted text-sm")
                        def on_add():
                            SP["dd_stop"] = 15
                            draw()
                            on_changed()
                        ui.button(t("sp.dd_stop_add"), icon="add", on_click=on_add) \
                            .props("flat no-caps dense color=primary").mark("add:dd_stop")

            draw()

        with section(t("sp.market"), "show_chart"):
            index_name = t(f"sp.market_index_{SP['universe']}") if SP["universe"] in ("all", "cb") \
                else p(UNIVERSES[SP["universe"]]["label"])
            hint(t("sp.market_help_short", index=index_name), t("sp.market_help", index=index_name), mark="sp_market_hint")
            with ui.card().classes("w-full gap-3"):
                side_editor(SP, "sell", t("sp.market_out"), "trending_down", lambda: changed(), options=MARKET_INDS,
                            empty_text=t("sp.market_out_empty"), key="market")
            with ui.card().classes("w-full gap-3"):
                dd_stop_editor(SP, lambda: changed())
            with ui.card().classes("w-full gap-3"):
                side_editor(SP, "buy", t("sp.market_back"), "trending_up", lambda: changed(), options=MARKET_INDS,
                            empty_text=t("sp.market_back_empty"), key="market")

        make_broker = broker_settings(broker_key(panel.kind), default_preset="bond" if panel.kind == "cb" else "stock",
                                      default_cash=1_000_000, show_t1=False, show_contribution=True,
                                      contribution_help="sp.contribution_help", on_change=lambda: draw_chips())

        # ---- 策略预览、名称与保存（与择时回测页一样放在设置区最下面） ----
        preview = ui.label().classes("sq-code w-full")
        with ui.row().classes("w-full items-end gap-3"):
            ui.input(t("strat.name"), value=SP["name"], placeholder=t("sp.name_ph"),
                     on_change=lambda e: set_sp("name", e.value or "")).props("outlined dense").classes("grow") \
                .mark("sp_name")
            save_btn = ui.button(t("strat.save"), icon="save", on_click=lambda: save()).props("outline no-caps") \
                .mark("sp_save")

    def draw_chips():
        """设置区的标题行：因子 · 持有 · 调仓 · 条件 · 资金与费率（点任一处展开设置区）"""
        chips.clear()
        with chips:
            for k, label, v in _chip_items(SP, panel.kind, lg):
                with ui.element("div").classes("sq-chip").mark(f"sp_chip:{k}"):
                    ui.label(label).classes("k")
                    ui.label(v).classes("v")
            ui.space()
            ui.label(t("bt.edit_setup")).classes("sq-muted text-xs self-center pr-2 whitespace-nowrap")
        ctx.changed()

    def set_sp(k, v, cast=None):
        if v is None:
            return
        SP[k] = cast(v) if cast else v
        changed()

    def changed():
        spec = current_spec(SP)
        preview.text = strategies.describe(spec, lg)
        has = bool(SP["factors"])
        save_btn.set_enabled(has and bool(SP["name"].strip()))
        run_btn.set_enabled(has)
        exp_btn.set_enabled(has)
        need_fin_ = not panel.has_fin and needs_fin(spec)
        need_div_ = not panel.div_codes and any(FACTORS[f["key"]].get("requires_div") for f in SP["factors"])
        need_risk_ = panel.kind == "stock" and not panel.has_risk and needs_risk(spec)
        need_cash_ = SP.get("dividend") == "cash" and not panel.div_codes and panel.kind == "stock"
        if new_ui() and panel.kind == "stock" and ctx.lo is not None:
            # 新版：缺什么就地下载（gui/missing_data.py）；缺的种类没变时不重画，下载中的提示条不被冲掉
            want = ([("fin", "factor")] * need_fin_ + [("div", "factor")] * need_div_
                    + [("risk", "screen")] * need_risk_, need_cash_)
            if want != warn_shown.get("want"):
                warn_shown["want"] = want
                fin_warn.clear()
                with fin_warn:
                    for part, reason in want[0]:
                        stock_bar(part, SP["universe"], ctx.lo.year - 1, reason, on_done=ctx.reload_all)
                div_warn.clear()
                if need_cash_:
                    with div_warn:
                        stock_bar("div", SP["universe"], ctx.lo.year - 1, "cash", on_done=ctx.reload_all)
        else:
            fin_warn.clear()
            if need_fin_:
                with fin_warn:
                    notice(t("sp.need_fin"), "warning", "warning")
            if need_div_:
                with fin_warn:
                    notice(t("sp.need_div_factor"), "warning", "warning")
            if need_risk_:
                with fin_warn:
                    notice(t("sp.need_risk"), "warning", "warning")
            div_warn.clear()
            if need_cash_:
                with div_warn:
                    notice(t("sp.need_div"), "warning", "warning")
        rules_adv_update(_adv_rule_items(SP, panel.kind, lg))
        factor_adv_update(_adv_factor_items(SP))
        draw_chips()

    def save():
        strategies.save_strategy(SP["name"].strip(), {**current_spec(SP), "broker": asdict(make_broker())})
        ui.notify(t("strat.saved", name=SP["name"].strip()), type="positive")
        ctx.changed()

    async def do_run():
        spec, broker = current_spec(SP), make_broker()
        plan_ctx = SP.pop("autorun", None)         # 刚导入的方案：回测后与其中记录的结果核对
        run_btn.disable()
        running.set_visibility(True)
        ok = False
        try:
            t0 = time.time()
            run_panel, ps = await spec_panel(SP, ctx, spec)
            res = await run.io_bound(run_selection, run_panel, spec, broker, start=ctx.start)
            SP["result"] = (res, spec, time.time() - t0)
            SP["explanation"] = None
            SP["result_range"] = (ctx.start, ctx.end, ps)
            SP["result_broker"] = broker
            SP["result_panel"] = run_panel          # 调仓日检验要用同一份面板重跑
            SP["plan_check"] = (plan_mod.compare(plan_ctx.get("result"), res.metrics), plan_ctx) if plan_ctx else None
            uni = p(UNIVERSES[spec["universe"]]["label"])
            title = SP["name"].strip() or t("sel.kind")
            # restore 只记 spec（首页的策略进度用它比对已保存的策略、模拟账户）；选股重新打开不恢复设置
            history.add("sel", title, f"{uni} · {ctx.start} ~ {ctx.end}", res.metrics, restore={"spec": spec},
                        data=uni, strategy=title,
                        verify={"credibility": True, "phase": False, "walkforward": False})   # 之后做了检验再补记
            support.after_run()
            ok = True
        except Exception as ex:  # noqa: BLE001
            SP["result"] = None
            ui.notify(t("bt.failed", e=ex), type="negative", multi_line=True)
        finally:
            run_btn.enable()
            running.set_visibility(False)
        ctx.changed()
        if setup.is_deleted:                # 回测期间表单被重画（换了股票池 / 载入了策略）
            return
        setup.value = not ok                # 成功：收起设置看结果；出错：展开，方便改设置
        results.refresh()
        build_export_menu()                 # 导出方案时附带刚得到的结果
        if ok:      # 等折叠动画（0.3 秒）结束；摘要条不在视野上半部分时滚过去：摘要条在顶部，结果紧接在下面
            ui.run_javascript(f"""setTimeout(() => {{
                const el = getHtmlElement({setup_row.id});
                if (!el) return;
                const top = el.getBoundingClientRect().top;
                const head = document.querySelector('.sq-header')?.offsetHeight || 0;
                if (top < head || top > window.innerHeight * 0.5) el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
            }}, 400)""")

    def build_export_menu():
        """每次打开菜单时按当前设置生成（刚跑完的回测名单才能写进文件）"""
        from simplequant.paths import PROJECT_DIR, FROZEN
        from simplequant.export import selection_script, selection_platform_script, encode_script, PLATFORMS
        spec, broker = current_spec(SP), make_broker()
        title = SP["name"].strip() or t("sel.kind")
        stem = export_filename(title)[:-3]
        res = SP.get("result")
        panel_start = spec_panel_start(SP, ctx, spec)
        same = res and res[1] == spec and SP.get("result_range") == (ctx.start, ctx.end, panel_start)
        expected = res[0].schedule.picks if same and res[0].schedule else None
        exp_menu.clear()

        def export_plan():
            """当前设置刚回测过（策略、区间、资金都没改）时附带结果，导入方回测后核对"""
            result = res[0].metrics if same and SP.get("result_broker") == broker else None
            try:
                data = export_selection_bytes(title, spec, ctx.start, ctx.end, panel_start, broker, result)
            except plan_mod.PlanError as e:
                ui.notify(t(e.key, **e.kw), type="warning")
                return
            ui.download.content(data, plan_filename(title), "application/json")
            if result is None:
                ui.notify(t("plan.exported_no_result"), multi_line=True)
        with exp_menu, ui.column().classes("gap-1 p-2 w-[380px]"):
            ui.button(t("plan.export"), icon="description", on_click=export_plan) \
                .props("flat no-caps align=left").classes("w-full").tooltip(t("plan.export_help_sel")) \
                .mark("sp_exp_plan")
            ui.separator()
            ui.button(t("exp.button"), icon="code", on_click=lambda: ui.download.content(
                selection_script(spec, asdict(broker), ctx.start, str(PROJECT_DIR), title=title, lang=lg,
                                 exe=sys.executable if FROZEN else None),
                f"{stem}.py", "text/x-python")).props("flat no-caps align=left").classes("w-full") \
                .tooltip(t("exp.help_selection"))
            ui.button(t("exp.run_script"), icon="play_circle", on_click=run_script_dialog) \
                .props("flat no-caps align=left").classes("w-full").tooltip(t("exp.run_script_help")) \
                .mark("sp_run_script")
            ui.separator()
            ui.label(t("exp.sel_platform_note")).classes("sq-muted text-xs px-2")
            if expected is None:
                ui.label(t("exp.sel_need_run")).classes("text-warning text-xs px-2")
            follow = ui.switch(t("exp.sel_follow"), value=SP["follow"] and expected is not None) \
                .tooltip(t("exp.sel_follow_help")).mark("sp_exp_follow")
            follow.set_enabled(expected is not None)
            follow.on_value_change(lambda e: SP.__setitem__("follow", e.value))
            for key, meta in PLATFORMS.items():
                def make(key=key):
                    code = selection_platform_script(key, spec, asdict(broker), ctx.start, ctx.end, panel_start,
                                                     expected=expected, follow=follow.value and expected is not None,
                                                     title=title, lang=lg)
                    return encode_script(code, key)
                try:
                    make()
                    why = None
                except ValueError as e:
                    why = str(e)
                b = ui.button(p(meta["label"]), icon="upload_file",
                              on_click=lambda make=make, key=key: ui.download.content(
                                  make(), f"{stem}_{key}.py", "text/x-python")) \
                    .props("flat no-caps align=left").classes("w-full").mark(f"sp_exp_{key}")
                b.tooltip(why or t(f"exp.help_{key}"))
                if why:
                    b.disable()
                    ui.label(why).classes("text-warning text-xs px-2")

    changed()
    build_export_menu()

    @ui.refreshable
    def results():
        if SP.get("result"):
            _selection_results(SP, ctx)
    results()
    if SP.get("autorun") and SP["factors"]:          # 导入方案后自动回测一次
        ui.timer(0.1, do_run, once=True)


def _chip_items(SP, kind: str, lg: str) -> list[tuple[str, str, str]]:
    """设置摘要：[(marker 后缀, 标签, 值)]——因子 · 持有 · 调仓 · 条件 · 资金与费率"""
    sep = "、" if lg == "zh" else ", "
    fs = SP["factors"]
    factors = sep.join(p(FACTORS[f["key"]]["label"]) for f in fs) if len(fs) <= 2 \
        else t("sp.chip_n_factors", n=len(fs))

    hold = t("sp.chip_top", n=int(SP["top_n"]))
    if SP.get("keep_n") and int(SP["keep_n"]) > int(SP["top_n"]):
        hold += " · " + t("sp.chip_keep", m=int(SP["keep_n"]))

    reb = reb_label(SP["reb"] if SP["reb"] != "n" else int(SP["reb_n"]))
    if SP.get("skip"):
        reb += " · " + t("sp.chip_skip", months=_months_text(SP["skip"], lg))

    # 条件：设了值的过滤条件 + 实际生效的大盘条件 + 回撤停手（排除 ST、上市天数是常设项，不计）
    if kind == "cb":
        n_filters = sum(SP["cbf"].get(k) is not None for k in CB_FILTERS)
    else:
        n_filters = sum(v is not None for d in (SP.get("stockf") or {}, SP.get("riskf") or {}) for v in d.values())
    market = market_spec(SP) or {}
    n_market = sum(len((market.get(s) or {}).get("conditions") or []) for s in ("sell", "buy"))
    dd = float(SP.get("dd_stop") or 0)
    parts = []
    if n_filters:
        parts.append(t("sp.chip_filters", n=n_filters))
    if n_market:
        parts.append(t("sp.chip_market", n=n_market))
    if dd > 0:
        parts.append(t("sp.chip_dd", v=f"{dd:g}"))
    cond = " · ".join(parts) or t("sp.chip_none")

    b = state.STATE.get(f"{broker_key(kind)}_broker") or {}
    preset = COST_PRESETS.get(b.get("preset"))
    money = t("bt.cash_wan", v=f"{(b.get('cash') or 0) / 1e4:g}") + (" · " + p(preset["label"]) if preset else "")
    if b.get("contribution"):
        money += " · " + t("bt.chip_contribution", v=f"{b['contribution']:,.0f}",
                           freq=t("bt.contrib_weekly") if b.get("contribution_freq") == "weekly"
                           else t("bt.contrib_monthly"))
    return [("factors", t("sp.chip_factors"), factors or "—"), ("hold", t("sp.chip_hold"), hold),
            ("reb", t("sp.chip_reb"), reb), ("cond", t("sp.chip_cond"), cond),
            ("broker", t("bt.chip_broker"), money)]


def base_checks(res) -> list[dict]:
    """选股回测随回测完成的可信度检查（不含调仓日检验）"""
    ml_info = res.schedule.ml if res.schedule else None
    return check_backtest(res.metrics, res.trades, positions=res.positions) + (check_ml(ml_info["summary"]) if ml_info else [])


def phase_keys(spec: dict) -> list:
    """调仓日检验要错开的各个相位；空 = 不适用（例如每天调仓）"""
    return phases(selection_cycle(spec))


def _sel_checks(SP, base: list[dict], ctx=None):
    """可信度检查 + 调仓日检验（选股回测较慢，点按钮才做）。SP["phase"]：{res, prog, stop, found}；
    检验进行时每 0.5 秒重画一次"""
    res, rspec, _ = SP["result"]
    ks = phase_keys(rspec)
    box = ui.column().classes("w-full gap-2")

    def draw():
        if box.is_deleted:
            return
        box.clear()
        ph = SP.get("phase")
        ph = ph if ph and ph["res"] is res else None
        running = ph is not None and "found" not in ph
        with box:
            credibility_box(base + (ph.get("found", []) if ph else []),
                            t("cred.phase_running", **ph["prog"]) if running else None)
            if running:
                ui.button(t("cred.phase_stop"), icon="stop", on_click=lambda: ph.update(stop=True))                     .props("flat dense no-caps").mark("phase_stop")
                ui.timer(0.5, draw, once=True)
            elif ks and ph is None:
                ui.button(t("cred.phase_btn"), icon="event_repeat", on_click=lambda: start())                     .props("outline no-caps").mark("phase_run").tooltip(t("cred.phase_btn_help", m=max(ks), k=len(ks)))

    async def start():
        ph = SP["phase"] = {"res": res, "prog": {"n": 0, "of": len(ks)}, "stop": False}
        draw()
        try:
            rows = await run.io_bound(run_selection_phases, SP["result_panel"], rspec, SP["result_broker"],
                                      SP["result_range"][0], lambda n, of: ph["prog"].update(n=n, of=of),
                                      lambda: ph["stop"])
            ph["found"] = check_phase(res.metrics, rows)
            if ph["found"]:
                history.mark_verify("sel", rspec, phase=True)
        except Exception as e:  # noqa: BLE001
            ph["found"] = [{"level": "note", "key": "cred.phase_failed", "args": {"e": str(e)}}]
        if not ph["found"] and SP.get("phase") is ph:      # 停得太早，相位不够下结论：恢复按钮
            SP.pop("phase", None)
        draw()
        if ctx:
            ctx.changed()
    draw()


def _selection_results(SP, ctx):
    lg = lang()
    panel = ctx.panel
    res, rspec, secs = SP["result"]
    with ui.element("div").classes("sq-sec"):
        ui.label(t("bt.results")).classes("sq-h2")
    with ui.column().classes("gap-1"):
        ui.label(strategies.describe(rspec, lg).replace("\n", " · ") + " · " + t("sp.took", s=f"{secs:.1f}")) \
            .classes("sq-muted text-sm")
    if SP.get("plan_check") and SP["plan_check"][1]:
        plan_check_notice(*SP["plan_check"], res.metrics)
    metric_tiles(res.metrics)
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
        metric_tile(t("m.turnover_annual"), f"{res.metrics['turnover_annual']:.1f}×", help_text=t("sp.turnover_help"))
        metric_tile(t("sp.n_rebalances"), str(len(res.schedule.picks)))
        if "dividend_cash" in res.metrics:
            metric_tile(t("m.dividend_cash"), f"{res.metrics['dividend_cash']:,.0f}", help_text=t("sp.dividend_help"))
            metric_tile(t("m.dividend_tax"), f"{res.metrics['dividend_tax']:,.0f}", help_text=t("sp.dividend_tax_help"))
    if res.div_missing:
        shown = "、".join(f"{res.names.get(c, c)}({c.split('.')[-1]})" for c in res.div_missing[:10])
        notice(t("sp.div_missing", n=len(res.div_missing), codes=shown + ("…" if len(res.div_missing) > 10 else "")),
               "info", "warning")
    if res.div_patched is not None:
        pt = res.div_patched
        cash, rights = pt[pt["cash"] > 0], pt[pt["cash"] <= 0]
        sep = "、" if lg == "zh" else ", "
        eg = sep.join(f"{res.names.get(r.code, r.code)} {r.ex_date:%Y-%m-%d} {r.cash:g}" for r in cash.head(5).itertuples())
        notice(t("sp.div_patched", n=len(cash), m=len(rights), eg=eg + ("…" if len(cash) > 5 else "")), "info", "info")
    ml_info = res.schedule.ml if res.schedule else None
    _sel_checks(SP, base_checks(res), ctx)
    _explain(SP, rspec, res)
    if ml_info:
        _ml_results(ml_info, rspec)
    run_panel = SP.get("result_panel") or panel
    if res.schedule:
        n, known = ic_history_short(run_panel.calendar, rspec, list(res.schedule.picks) + res.schedule.skipped)
        if n:
            notice(t("sp.ic_short", n=n, lookback=int(rspec.get("ic_lookback", 252)), known=known,
                     lo=run_panel.calendar[0].date()), "info", "warning")
    tr = run_panel.bench_total_return
    if U.kind(rspec["universe"]) == "stock" and not tr:
        notice(t("sp.bench_price_note"), "info", "warning")
    dca = "invested" in res.equity
    bench = bench_label(rspec["universe"], tr)
    with ui.card().classes("w-full p-2"):
        plot(equity_chart(res.equity, lg, t("sp.bench_dca", name=bench) if dca else bench))
    if dca:
        ui.label(t("bt.fig1_caption_dca")).classes("sq-caption")

    with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
        tab_h = ui.tab("h", t("sp.tab_holdings"))
        tab_o = ui.tab("o", t("bt.tab_orders"))
        tab_l = ui.tab("l", t("bt.tab_logs"))
    with ui.tab_panels(tabs, value=tab_h).classes("w-full bg-transparent"):
        with ui.tab_panel(tab_h).classes("px-0"):
            def industry_mix(codes):
                counts = panel.industry.reindex(codes).fillna("—").map(
                    lambda s: s[3:] if s[:1].isalpha() and s[1:3].isdigit() else s).value_counts()
                return "、".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items())
            rows = [{t("col.time"): d.date(),
                     t("sp.picked"): "、".join(f"{res.names.get(c, c)}({c.split('.')[-1]})" for c in codes)
                     or t("sp.cash_period"),
                     t("sp.industry_mix"): industry_mix(codes)}
                    for d, codes in sorted(res.schedule.picks.items(), reverse=True)]
            ui.label(t("sp.pick_hint")).classes("sq-muted text-xs")
            tbl = df_table(pd.DataFrame(rows), rows_per_page=20).props("wrap-cells").classes("cursor-pointer")                 .mark("sp_holdings")
            tbl.on("rowClick", lambda e: picks_dialog(SP, run_panel, rspec, res,
                                                      pd.Timestamp(e.args[1][t("col.time")])))
            if res.schedule.skipped:
                ui.label(t("sp.skipped", n=len(res.schedule.skipped))).classes("sq-muted text-xs")
        with ui.tab_panel(tab_o).classes("px-0"):
            if not res.orders.empty:
                o = res.orders.copy()
                o["symbol"] = o["symbol"].map(lambda c: f"{res.names.get(c, c)}({c.split('.')[-1]})")
                df_table(fmt_table(orders_table(o, lg), {t("col.price"): "{:.2f}", t("col.value"): "{:,.0f}",
                                                         t("col.commission"): "{:.2f}"}), rows_per_page=30)
        with ui.tab_panel(tab_l).classes("px-0"):
            def named(kw):          # 日志里的代码换成「名称(代码)」，与成交记录一致
                c = kw.get("name")
                return {**kw, "name": f"{res.names.get(c, c)}({c.split('.')[-1]})"} if isinstance(c, str) else kw
            df_table(logs_table([(d, k, named(kw)) for d, k, kw in res.logs], lg), rows_per_page=50)


def _ml_form(SP, changed):
    """机器学习：模型、训练窗口与该模型的参数"""
    @ui.refreshable
    def params():
        m = SP["ml_model"]
        with ui.row().classes("w-full items-center gap-4"):
            ui.number(p(ML.PARAM_LABELS["window"]), value=SP["ml_window"], min=120, max=2520, step=63, precision=0,
                      on_change=lambda e: set_v("ml_window", e.value, int)) \
                .props("outlined dense").classes("w-44").tooltip(t("sp.ml_window_help")).mark("sp_ml_window")
            for k, v in SP["ml_params"][m].items():
                lo, hi, is_int = ML.PARAM_RANGES[k]
                ui.number(p(ML.PARAM_LABELS[k]), value=v, min=lo, max=hi, precision=0 if is_int else None,
                          step=1 if is_int else (0.01 if k == "lr" else 0.1),
                          on_change=lambda e, k=k: set_param(k, e.value)) \
                    .props("outlined dense").classes("w-36").tooltip(t(f"sp.ml_{k}_help")).mark(f"sp_ml_{k}")
        ui.label(t(f"sp.ml_about_{m}")).classes("sq-muted text-xs")

    def set_v(k, v, cast):
        if v is not None:
            SP[k] = cast(v)
            changed()

    def set_param(k, v):
        if v is None:
            return
        is_int = ML.PARAM_RANGES[k][2]
        SP["ml_params"][SP["ml_model"]][k] = int(v) if is_int else float(v)
        changed()

    def on_model(e):
        SP["ml_model"] = e.value
        params.refresh()
        changed()
    ui.select({m: p(v) for m, v in ML.MODELS.items()}, label=t("sp.ml_model"), value=SP["ml_model"],
              on_change=on_model).props(DENSE).classes("w-48").mark("sp_ml_model")
    params()


def _ml_results(info: dict, rspec: dict):
    """机器学习：训练与样本外 IC 对比、因子重要性"""
    sm = info["summary"]
    model = p(ML.MODELS[info["model"]])
    with ui.element("div").classes("sq-sec"):
        ui.label(t("sp.ml_title", model=model)).classes("sq-h2")
    ui.label(t("sp.ml_caption")).classes("sq-muted text-sm")
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
        metric_tile(t("sp.ml_trainings"), str(sm["n_train"]))
        metric_tile(t("sp.ml_train_ic"), num(sm["train_ic"], 3), help_text=t("sp.ml_train_ic_help"))
        metric_tile(t("sp.ml_oos_ic"), num(sm["oos"].get("ml", float("nan")), 3), help_text=t("sp.ml_oos_ic_help"))
        metric_tile(t("sp.ml_icir_ic"), num(sm["oos"].get("icir", float("nan")), 3),
                    help_text=t("sp.ml_icir_ic_help"))
    ic = info["ic"]
    names = {"ml": t("sp.ml_row_model", model=model), "icir": t("sp.ml_row_icir")}
    for f in rspec["factors"]:
        names[f["key"]] = t("sp.ml_row_factor", name=factor_label(f["key"]))
    rows = [{t("sp.ml_col_method"): names.get(c, c), t("sp.ml_col_ic"): ic[c].mean(),
             t("sp.ml_col_pos"): float((ic[c].dropna() > 0).mean()) if ic[c].notna().any() else float("nan"),
             t("sp.ml_col_n"): int(ic[c].notna().sum())} for c in ic.columns]
    with ui.row().classes("w-full gap-6 items-start"):
        with ui.column().classes("grow gap-1"):
            ui.label(t("sp.ml_ic_table")).classes("font-semibold")
            df_table(fmt_table(pd.DataFrame(rows), {t("sp.ml_col_ic"): "{:.3f}", t("sp.ml_col_pos"): "{:.0%}"})) \
                .mark("sp_ml_ic")
        if info.get("importance"):
            with ui.column().classes("gap-1"):
                ui.label(t("sp.ml_importance")).classes("font-semibold")
                imp = sorted(info["importance"].items(), key=lambda kv: -kv[1])
                df_table(fmt_table(pd.DataFrame([{t("sp.ml_col_factor"): factor_label(k), t("sp.ml_col_share"): v}
                                                 for k, v in imp]), {t("sp.ml_col_share"): "{:.0%}"})) \
                    .mark("sp_ml_imp")
                ui.label(t(f"sp.ml_importance_help_{'ridge' if info['model'] == 'ridge' else 'tree'}")) \
                    .classes("sq-muted text-xs max-w-sm")


def _explain(SP, rspec: dict, res):
    """AI 解读本次选股回测结果（未配置大模型时不显示）"""
    cfg = llm.load_config()
    if not (cfg and cfg.ready):
        return

    async def explain():
        btn.disable()
        try:
            SP["explanation"] = await run.io_bound(
                llm.explain_result, llm.get_provider(cfg), strategies.describe(rspec, lang()), res.metrics, lang(),
                llm.trade_details(res, res.names))
        except llm.LLMError as e:
            ui.notify(str(e), type="negative", multi_line=True)
        except Exception as e:  # noqa: BLE001
            ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
        finally:
            btn.enable()
        show.refresh()

    btn = ui.button(t("ai.explain"), icon="auto_awesome", on_click=explain).props("outline no-caps") \
        .classes("self-start").mark("sp_explain")

    @ui.refreshable
    def show():
        if SP.get("explanation"):
            with ui.column().classes("w-full gap-1 sq-note py-3"):
                ui.markdown(SP["explanation"]).classes("text-sm")
                ui.label(t("ai.explain_note")).classes("sq-muted text-xs")
    show()


# ================= 运行导出的选股脚本 =================
SCRIPT_MARKS = ("from simplequant.stocks import StockStore, run_selection, universe",
                "from simplequant.stocks import StockStore, UNIVERSES, build_panel, run_selection")   # 旧版导出的


def run_script_dialog():
    """选择导出的选股脚本 → 复制到数据目录下的 scripts/<名字>/ → 子进程运行，实时显示日志"""
    from simplequant.export.run_script import command, log_path
    from simplequant.paths import DATA_ROOT

    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full gap-3"):
        ui.label(t("exp.run_title")).classes("sq-h2")
        ui.label(t("exp.run_script_help")).classes("sq-muted text-sm")
        status = ui.column().classes("w-full")
        logbox = ui.label("").classes("sq-code w-full text-xs whitespace-pre-wrap max-h-80 overflow-auto")
        logbox.set_visibility(False)

        async def on_upload(e):
            name = Path(e.file.name).name
            data = await e.file.read()
            text = data.decode("utf-8", errors="replace")
            status.clear()
            if not any(m in text for m in SCRIPT_MARKS):
                with status:
                    notice(t("exp.run_bad"), "error", "error")
                return
            folder = DATA_ROOT / "scripts" / Path(name).stem
            folder.mkdir(parents=True, exist_ok=True)
            script = folder / name
            script.write_bytes(data)
            log = log_path(script)
            log.unlink(missing_ok=True)
            up.set_visibility(False)
            with status:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm")
                    ui.label(t("exp.run_running", name=name)).classes("text-sm")
            logbox.set_visibility(True)

            def tail():
                if log.exists():
                    logbox.text = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
            timer = ui.timer(0.5, tail)
            try:
                code = await run.io_bound(lambda: subprocess.run(
                    command(script), cwd=str(folder), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode)
            finally:
                timer.cancel()
                tail()
                up.set_visibility(True)
                up.reset()
            status.clear()
            with status:
                if code == 0:
                    with ui.row().classes("w-full items-center gap-3 no-wrap"):
                        notice(t("exp.run_done") + f" `{folder}`", "check_circle")
                        ui.button(t("exp.open_folder"), icon="folder_open",
                                  on_click=lambda: open_folder(folder)).props("outline no-caps")
                else:
                    notice(t("exp.run_failed"), "error", "error")

        up = ui.upload(label=t("exp.run_pick"), auto_upload=True, on_upload=on_upload) \
            .props('accept=.py flat bordered').classes("w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button(t("gui.close"), on_click=dlg.close) \
                .props("flat no-caps")
    dlg.open()
