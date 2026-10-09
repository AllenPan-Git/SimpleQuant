"""回测方案文件（simplequant/plan.py）的导入对话框与导出（回测页、多因子选股页使用）"""

import copy
import datetime as dt
import time
from dataclasses import asdict

from nicegui import run, ui

from simplequant import plan as P, strategies
from simplequant.data import fetch_to_library, library
from simplequant.engine import COST_PRESETS
from gui import state
from gui.common import t, p, lang
from gui.widgets import notice, Progress
from ui.shared import pct


def export_bytes(name: str, spec: dict, metas: list, date_range, broker, mode: str, result: dict | None) -> bytes:
    plan = P.make_plan(name, spec, metas, date_range[0], date_range[1], broker, mode, result)
    return P.dumps(plan).encode("utf-8")


def export_selection_bytes(name: str, spec: dict, start, end, warmup_start, broker, result: dict | None) -> bytes:
    plan = P.make_selection_plan(name, spec, start, end, warmup_start, broker, result)
    return P.dumps(plan).encode("utf-8")


def plan_check_notice(status, ctx: dict, metrics: dict):
    """导入方案后的第一次回测：与方案文件记录的结果核对"""
    rec = ctx.get("result") or {}
    if status == "same":
        notice(t("plan.check_same", name=ctx["name"], ret=pct(rec["total_return"]), n=rec.get("trades", "?")),
               "verified", "info")
    elif status == "differs":
        notice(t("plan.check_differs", name=ctx["name"], ret=pct(rec["total_return"]), n=rec.get("trades", "?"),
                 now=pct(metrics["total_return"]), m=metrics["trades"])
               + (t("plan.check_provider") if ctx.get("provider") else ""), "rule", "warning")
    else:
        notice(t("plan.check_none", name=ctx["name"]), "description", "info")


def plan_filename(name: str) -> str:
    import re
    return (re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "plan") + P.SUFFIX


def import_button():
    """「导入方案」按钮：选择文件 → 核对内容与数据 → 下载缺少的数据 → 还原设置并回测"""
    def pick():
        with ui.dialog() as dlg, ui.card().classes("w-[480px] max-w-full"):
            ui.label(t("plan.import_title")).classes("sq-serif text-lg")
            ui.label(t("plan.import_intro")).classes("sq-muted text-sm")

            async def on_upload(e):
                data = await e.file.read() if hasattr(e, "file") else e.content.read()
                dlg.close()
                try:
                    plan = P.loads(data)
                except P.PlanError as err:
                    ui.notify(t(err.key, **err.kw), type="negative", multi_line=True)
                    return
                (review_selection if P.is_selection(plan) else review)(plan)
            ui.upload(label=t("plan.choose"), auto_upload=True, on_upload=on_upload) \
                .props(f"accept={P.SUFFIX},.json flat bordered").classes("w-full").mark("plan_upload")
            ui.button(t("gui.cancel"), on_click=dlg.close).props("flat no-caps").classes("self-end")
        dlg.open()
    return ui.button(t("plan.import"), icon="file_open", on_click=pick).props("outline no-caps").mark("plan_import")


def review(plan: dict):
    """导入前的核对：策略、标的（本机是否已有数据）、区间、资金；确认后下载缺少的数据并回测"""
    lg = lang()
    rows = P.check_assets(plan, library.list_datasets())
    broker = P.broker_of(plan)
    with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-full gap-3").mark("plan_review"):
        ui.label(t("plan.review_title", name=plan["name"])).classes("sq-serif text-lg")
        ui.label(t("plan.review_meta", created=plan.get("created", "?"), app=plan.get("app", "?"))) \
            .classes("sq-muted text-xs")
        ui.label(strategies.describe(plan["strategy"], lg)).classes("sq-code w-full")
        with ui.column().classes("w-full gap-1"):
            ui.label(t("plan.assets")).classes("text-sm")
            status_labels = []
            for r in rows:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    icon = ui.icon("check_circle" if r["status"] == "ok" else
                                   "download" if r["status"] == "download" else "block") \
                        .classes("text-positive" if r["status"] == "ok" else
                                 "sq-muted" if r["status"] == "download" else "text-negative")
                    ui.label(r["asset"].get("name") or r["asset"]["symbol"]).classes("text-sm")
                    lab = ui.label({"ok": t("plan.st_ok"), "download": t("plan.st_download"),
                                    "manual": t("plan.st_manual")}[r["status"]]).classes("sq-muted text-xs")
                    status_labels.append((icon, lab))
        mode = t("bt.mode_batch") if plan["mode"] == "batch" else t("bt.mode_portfolio")
        ui.label(t("plan.summary", start=plan["range"][0], end=plan["range"][1], cash=f"{broker.cash / 1e4:g}",
                   comm=f"{broker.commission * 1e4:g}", stamp=f"{broker.stamp_duty * 1e4:g}",
                   slip=f"{broker.slippage * 1e4:g}")
                 + (f" · {mode}" if len(rows) > 1 else "")).classes("text-sm")
        rec = plan.get("result")
        if rec and "total_return" in rec:
            ui.label(t("plan.recorded", ret=pct(rec["total_return"]), n=rec.get("trades", "?"))).classes("text-sm")
        manual = [r for r in rows if r["status"] == "manual"]
        if manual:
            ui.label(t("plan.manual_note")).classes("text-negative text-sm")
        msg = ui.label().classes("text-sm")

        async def go():
            btn.disable()
            try:
                for (icon, lab), r in zip(status_labels, rows):
                    if r["status"] != "download":
                        continue
                    a = r["asset"]
                    lab.text = t("plan.st_fetching")
                    kwargs = {"asset": a["asset"]} if a.get("source") == "akshare" and a.get("asset") else {}
                    try:
                        r["meta"] = await run.io_bound(
                            fetch_to_library, a["source"], a["symbol"], plan["range"][0], plan["range"][1],
                            freq=a.get("freq", "1d"), adjust=a.get("adjust", ""), name=a.get("name") or a["symbol"],
                            **kwargs)
                    except Exception as e:  # noqa: BLE001
                        icon.name, lab.text = "error", t("plan.st_failed", e=str(e)[:120])
                        icon.classes(replace="text-negative")
                        msg.text = t("plan.fetch_failed")
                        return
                    r["status"] = "ok"
                    icon.name, lab.text = "check_circle", t("plan.st_ok")
                    icon.classes(replace="text-positive")
                    if P.provider_differs(a, r["meta"]):
                        lab.text = t("plan.st_provider")
                apply(plan, [r["meta"] for r in rows])
                dlg.close()
            finally:
                btn.enable()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(t("gui.cancel"), on_click=dlg.close).props("flat no-caps")
            need = any(r["status"] == "download" for r in rows)
            btn = ui.button(t("plan.go_download" if need else "plan.go"), icon="play_arrow", on_click=go) \
                .props("unelevated no-caps").mark("plan_go")
            if manual:
                btn.disable()
    dlg.open()


def _unique_name(name: str, spec: dict) -> str:
    """同名策略内容相同就沿用；内容不同则另起名字，不覆盖用户自己的策略"""
    saved = strategies.list_strategies()
    cand, i = name, 1
    while cand in saved and saved[cand] != spec:
        i += 1
        cand = t("plan.dup_name", name=name, n=i)
    return cand


def _restore_broker(key: str, plan: dict, default: dict):
    """把方案里的资金与费率写进页面记住的设置（state.STATE[f"{key}_broker"]，见 components.broker_settings）"""
    b = plan["broker"]
    saved = state.STATE.setdefault(f"{key}_broker", default)
    saved.setdefault("custom", {})
    preset = next((k for k, c in COST_PRESETS.items()
                   if abs(c["commission"] - b.get("commission", -1)) < 1e-12
                   and abs(c["min_commission"] - b.get("min_commission", -1)) < 1e-9
                   and abs(c["stamp_duty"] - b.get("stamp_duty", -1)) < 1e-12), saved.get("preset", default["preset"]))
    full = asdict(P.broker_of(plan))
    saved.update(cash=full["cash"], preset=preset, t1=full["t_plus_1"], slippage=round(full["slippage"] * 1e4, 4),
                 dividend=full["dividend"], limit=full["price_limit"])
    saved["custom"][preset] = {"commission": round(full["commission"] * 1e4, 4),
                               "min_commission": full["min_commission"],
                               "stamp_duty": round(full["stamp_duty"] * 1e4, 4)}


def apply(plan: dict, metas: list):
    """还原回测页的设置并自动回测"""
    spec = plan["strategy"]
    name = _unique_name(plan["name"], spec)
    strategies.save_strategy(name, spec)
    S = state.STATE
    _restore_broker("bt", plan, {"cash": 100_000, "preset": "etf", "t1": True, "slippage": 5.0, "custom": {}})
    S["bt_ids"] = [m.id for m in metas]
    S["bt_range"] = tuple(dt.date.fromisoformat(d) for d in plan["range"])
    S["bt_strategy"] = f"saved:{name}"
    S["bt_mode"] = plan["mode"]
    S["bt_plan"] = {"name": plan["name"], "result": plan.get("result"),
                    "provider": any(P.provider_differs(a, m) for a, m in zip(plan["assets"], metas))}
    S["bt_autorun"] = True
    S.pop("bt", None)
    ui.navigate.to("/backtest")


# ---------------- 多因子选股方案 ----------------
def _display_spec(plan: dict) -> dict:
    """描述策略用：自定义因子尚未在本机登记，显示方案里的名称"""
    spec = copy.deepcopy(plan["strategy"])
    for f in spec["factors"]:
        d = (plan.get("custom_factors") or {}).get(f["key"])
        if d:
            f["key"] = d["name"]
    return spec


def review_selection(plan: dict):
    """导入选股方案前的核对：策略、股票池数据、区间与资金、自定义因子代码（须确认）；确认后下载缺少的数据并回测"""
    from simplequant.stocks import UNIVERSES, universe as U
    lg = lang()
    spec, custom = plan["strategy"], plan.get("custom_factors") or {}
    uni = spec["universe"]
    missing = P.selection_missing(plan)
    broker = P.broker_of(plan)
    with ui.dialog() as dlg, ui.card().classes("w-[720px] max-w-full gap-3").mark("plan_review"):
        ui.label(t("plan.sel_review_title", name=plan["name"])).classes("sq-serif text-lg")
        ui.label(t("plan.review_meta", created=plan.get("created", "?"), app=plan.get("app", "?"))) \
            .classes("sq-muted text-xs")
        ui.label(strategies.describe(_display_spec(plan), lg)).classes("sq-code w-full")
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ok = not missing
            ui.icon("check_circle" if ok else "download").classes("text-positive" if ok else "sq-muted")
            ui.label(p(UNIVERSES[uni]["label"])).classes("text-sm")
            ui.label(t("plan.st_ok") if ok else t("plan.sel_need", what="、".join(
                t(f"plan.sel_miss_{m}") for m in missing) if lg == "zh" else ", ".join(
                t(f"plan.sel_miss_{m}") for m in missing))).classes("sq-muted text-xs")
        ui.label(t("plan.summary", start=plan["range"][0], end=plan["range"][1], cash=f"{broker.cash / 1e4:g}",
                   comm=f"{broker.commission * 1e4:g}", stamp=f"{broker.stamp_duty * 1e4:g}",
                   slip=f"{broker.slippage * 1e4:g}")).classes("text-sm")
        if plan["warmup_start"] < plan["range"][0]:
            ui.label(t("plan.sel_warmup", d=plan["warmup_start"])).classes("sq-muted text-xs")
        rec = plan.get("result")
        if rec and "total_return" in rec:
            ui.label(t("plan.recorded", ret=pct(rec["total_return"]), n=rec.get("trades", "?"))).classes("text-sm")
        trust = None
        if custom:
            notice(t("plan.sel_code_warning", n=len(custom)), "code", "warning")
            for d in custom.values():
                ui.label(d["name"] + (f" · {d['desc']}" if d.get("desc") else "")).classes("text-sm font-medium")
                ui.code(d["code"], language="python").classes("w-full text-xs")
            trust = ui.checkbox(t("plan.sel_trust")).mark("plan_trust")
        if missing:
            ui.label(t("plan.sel_download_note") if U.kind(uni) == "stock" else t("plan.sel_download_note_cb")) \
                .classes("sq-muted text-xs")
        prog = Progress()
        msg = ui.column().classes("w-full")

        async def go():
            btn.disable()
            try:
                if missing:
                    steps = {"universe": t("sp.step_universe"), "index": t("sp.step_index"),
                             "stocks": t("plan.sel_step_stocks"), "fin": t("plan.sel_step_fin"),
                             "div": t("plan.sel_step_div"), "list": t("cb.step_list"), "info": t("cb.step_info"),
                             "daily": t("cb.step_daily")}
                    if U.kind(uni) == "cb":
                        steps["index"] = t("cb.step_index")
                    t0 = time.time()

                    def progress(step, i, n):
                        prog.set(i / n if n else 0, f"{steps.get(step, step)} · {i}/{n} · {time.time() - t0:.0f}s")
                    prog.start()
                    try:
                        errs = await run.io_bound(P.download_selection, plan, 4, progress)
                    except Exception as e:  # noqa: BLE001
                        with msg:
                            notice(t("plan.fetch_failed") + f"\n\n{type(e).__name__}: {e}", "error", "error")
                        return
                    finally:
                        prog.stop()
                    left = P.selection_missing(plan)
                    if left:
                        with msg:
                            notice(t("plan.fetch_failed") + (f"（{len(errs)}）" if errs else ""), "error", "error")
                        return
                    if errs:
                        ui.notify(t("sp.download_errors", n=len(errs)), type="warning", multi_line=True)
                apply_selection(plan)
                dlg.close()
            finally:
                btn.enable()
                if trust is not None:
                    btn.set_enabled(trust.value)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(t("gui.cancel"), on_click=dlg.close).props("flat no-caps")
            btn = ui.button(t("plan.go_download" if missing else "plan.go"), icon="play_arrow", on_click=go) \
                .props("unelevated no-caps").mark("plan_go")
            if trust is not None:
                btn.disable()
                trust.on_value_change(lambda e: btn.set_enabled(bool(e.value)))
    dlg.open()


def _import_factors(plan: dict) -> dict:
    """保存方案里的自定义因子，返回 {方案里的键: 本机的键}；本机已有相同代码的因子时直接使用，同名但代码不同的另起名字"""
    from simplequant.stocks import custom_factors as cf
    mapping = {}
    for key, d in (plan.get("custom_factors") or {}).items():
        saved = cf.list_factors()
        same = next((k for k, e in saved.items() if e["code"] == d["code"]), None)
        if same:
            mapping[key] = same
            continue
        names = {e["name"] for e in saved.values()}
        name, i = d["name"].strip(), 1
        while name in names:
            i += 1
            name = t("plan.dup_name", name=d["name"].strip(), n=i)
        mapping[key] = cf.save_factor(name, d["code"], int(d.get("direction", 1) or 1), str(d.get("desc") or ""))
    return mapping


def apply_selection(plan: dict):
    """还原多因子选股页的设置并自动回测"""
    from gui.pages.selection import sp_state, apply_spec, broker_key
    from simplequant.stocks import universe as U
    mapping = _import_factors(plan)
    spec = copy.deepcopy(plan["strategy"])
    for f in spec["factors"]:
        f["key"] = mapping.get(f["key"], f["key"])
    name = _unique_name(plan["name"], spec)
    strategies.save_strategy(name, spec)
    SP = sp_state()
    apply_spec(SP, spec, name)
    uni, (start, end) = spec["universe"], plan["range"]
    SP["ranges"][uni] = (dt.date.fromisoformat(start), dt.date.fromisoformat(end))
    SP["plan_warm"] = (uni, start, plan["warmup_start"])
    kind = U.kind(uni)
    _restore_broker(broker_key(kind), plan, {"cash": 1_000_000, "preset": "bond" if kind == "cb" else "stock",
                                             "t1": True, "slippage": 5.0, "custom": {}})
    SP["result"] = None
    SP["plan_check"] = None
    SP["autorun"] = {"name": plan["name"], "result": plan.get("result")}
    state.STATE["sp_tab"] = "bt"
    ui.navigate.to("/selection")
