"""回测方案文件（simplequant/plan.py）的导入对话框与导出（回测页使用）"""

import datetime as dt
from dataclasses import asdict

from nicegui import run, ui

from simplequant import plan as P, strategies
from simplequant.data import fetch_to_library, library
from simplequant.engine import COST_PRESETS
from gui import state
from gui.common import t, lang
from ui.shared import pct


def export_bytes(name: str, spec: dict, metas: list, date_range, broker, mode: str, result: dict | None) -> bytes:
    plan = P.make_plan(name, spec, metas, date_range[0], date_range[1], broker, mode, result)
    return P.dumps(plan).encode("utf-8")


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
                review(plan)
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


def apply(plan: dict, metas: list):
    """还原回测页的设置并自动回测"""
    spec = plan["strategy"]
    name = _unique_name(plan["name"], spec)
    strategies.save_strategy(name, spec)
    b = plan["broker"]
    S = state.STATE
    saved = S.setdefault("bt_broker", {"cash": 100_000, "preset": "etf", "t1": True, "slippage": 5.0, "custom": {}})
    saved.setdefault("custom", {})
    preset = next((k for k, c in COST_PRESETS.items()
                   if abs(c["commission"] - b.get("commission", -1)) < 1e-12
                   and abs(c["min_commission"] - b.get("min_commission", -1)) < 1e-9
                   and abs(c["stamp_duty"] - b.get("stamp_duty", -1)) < 1e-12), saved.get("preset", "etf"))
    full = asdict(P.broker_of(plan))
    saved.update(cash=full["cash"], preset=preset, t1=full["t_plus_1"], slippage=round(full["slippage"] * 1e4, 4),
                 dividend=full["dividend"], limit=full["price_limit"])
    saved["custom"][preset] = {"commission": round(full["commission"] * 1e4, 4),
                               "min_commission": full["min_commission"],
                               "stamp_duty": round(full["stamp_duty"] * 1e4, 4)}
    S["bt_ids"] = [m.id for m in metas]
    S["bt_range"] = tuple(dt.date.fromisoformat(d) for d in plan["range"])
    S["bt_strategy"] = f"saved:{name}"
    S["bt_mode"] = plan["mode"]
    S["bt_plan"] = {"name": plan["name"], "result": plan.get("result"),
                    "provider": any(P.provider_differs(a, m) for a, m in zip(plan["assets"], metas))}
    S["bt_autorun"] = True
    S.pop("bt", None)
    ui.navigate.to("/backtest")
