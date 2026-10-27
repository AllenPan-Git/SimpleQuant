"""选股页「个股概览」：标签页里按代码 / 名称查看，或在选股结果里点开入选股票（核心见 simplequant/stocks/overview.py）"""

import numpy as np
import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant.stocks import StockStore, today
from simplequant.stocks import overview as O
from simplequant.stocks.selection import pick_details
from gui.common import t, p, lang
from gui.components import metric_tile
from gui.widgets import df_table, fmt_table, plot, notice
from ui.charts import valuation_chart

DENSE = "outlined dense"


def code6(code: str) -> str:
    return code.split(".")[-1]


def _num(v, f="{:.2f}"):
    return "" if v is None or not np.isfinite(v) else f.format(v)


def _period(r) -> str:
    y, m = r.stat_date.year, r.stat_date.month
    return t("ov.annual" if m == 12 else {3: "ov.q1", 6: "ov.q2", 9: "ov.q3"}.get(m, "ov.annual"), y=y)


# ================= 标签页 =================
def overview_tab(SP):
    ui.label(t("ov.intro")).classes("sq-muted text-sm")
    with ui.row().classes("w-full items-center gap-3"):
        q = ui.input(t("ov.search"), value=SP.get("ov_query", "")).props(DENSE).classes("w-56").mark("ov_query")
        btn = ui.button(t("ov.show"), icon="search").props("unelevated no-caps").mark("ov_show")
    choices = ui.row().classes("w-full items-center gap-2")
    box = ui.column().classes("w-full gap-4")

    def search():
        SP["ov_query"] = (q.value or "").strip()
        choices.clear()
        if not SP["ov_query"]:
            return
        try:
            basics = StockStore().load_basics()
        except (OSError, ValueError):
            basics = None
        hits = O.find_stocks(basics, SP["ov_query"])
        if hits.empty:
            box.clear()
            with box:
                notice(t("ov.not_found", q=SP["ov_query"]) + " " + t("ov.empty"), "info")
            return
        if len(hits) > 1 and not (hits.iloc[0]["code"].endswith(SP["ov_query"]) or hits.iloc[0]["name"] == SP["ov_query"]):
            with choices:
                ui.label(t("ov.choose")).classes("sq-muted text-sm")
                for r in hits.itertuples():
                    ui.button(f"{r.name}({code6(r.code)})", on_click=lambda c=r.code: open_code(c)) \
                        .props("flat dense no-caps")
            return
        open_code(hits.iloc[0]["code"])

    def open_code(code):
        SP["ov_code"] = code
        render_into(box, code, SP)

    q.on("keydown.enter", search)
    btn.on_click(search)
    if SP.get("ov_code"):
        render_into(box, SP["ov_code"], SP)
    else:
        with box:
            ui.label(t("ov.empty")).classes("sq-muted text-sm")


def overview_dialog(code: str, SP):
    """选股结果里点开某只股票"""
    with ui.dialog() as dlg, ui.card().classes("w-[1000px] max-w-full gap-3").mark("ov_dialog"):
        with ui.row().classes("w-full justify-end"):
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")
        box = ui.column().classes("w-full gap-4")
    render_into(box, code, SP)
    dlg.open()


def render_into(box, code: str, SP):
    """后台读数据，读完画到 box 里"""
    box.clear()
    with box:
        with ui.row().classes("items-center gap-2"):
            ui.spinner(size="sm")
            ui.label(t("ov.loading")).classes("sq-muted")

    async def load():
        ov = await run.io_bound(O.build, StockStore(), code)
        box.clear()
        with box:
            _render(ov, SP, lambda: render_into(box, code, SP))
    background_tasks.create(load(), name="stock overview")


# ================= 内容 =================
def _render(ov: O.Overview, SP, reload):
    lg = lang()
    with ui.element("div").classes("sq-sec"):
        ui.label(f"{ov.name or code6(ov.code)}（{code6(ov.code)}）" if lg == "zh" else
                 f"{ov.name or code6(ov.code)} ({code6(ov.code)})").classes("sq-h2").mark("ov_title")
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-5"):
        metric_tile(t("ov.industry"), ov.industry[3:] if ov.industry[:1].isalpha() and ov.industry[1:3].isdigit()
                    else (ov.industry or "—"))
        metric_tile(t("ov.listed"), str(ov.ipo_date.date()) if ov.ipo_date is not None and pd.notna(ov.ipo_date) else "—")
        if ov.out_date is not None:
            metric_tile(t("ov.delisted"), str(ov.out_date.date()))
        if ov.last_date is not None:
            metric_tile(t("ov.last"), str(ov.last_date.date()))
            metric_tile(t("ov.close"), _num(ov.close))
        if np.isfinite(ov.mcap):
            metric_tile(t("ov.mcap"), t("ov.mcap_value", v=ov.mcap / 1e8, w=ov.mcap / 1e9))
    if ov.is_st:
        notice(t("ov.st"), "warning", "warning")
    if ov.missing:
        _missing(ov, SP, reload)

    if ov.valuation is not None:
        _valuation(ov, lg)
    if ov.fin is not None and len(ov.fin):
        _financials(ov)
    if ov.dividends is not None:
        _dividends(ov)
    if ov.risk is not None and len(ov.risk):
        _risk(ov)


def _missing(ov, SP, reload):
    sep = "、" if lang() == "zh" else ", "
    parts = sep.join(t(f"ov.part_{k}") for k in ov.missing)
    start, end = SP["dl"]["start"], today()
    with ui.column().classes("w-full gap-2"):
        notice(t("ov.missing", parts=parts), "info")
        with ui.row().classes("items-center gap-3"):
            btn = ui.button(t("ov.download"), icon="download").props("outline no-caps").mark("ov_download")
            hint = ui.label().classes("sq-muted text-xs")
        out = ui.column().classes("w-full")

    async def estimate():
        n = await run.io_bound(O.requests_needed, StockStore(), ov.code, start, end)
        hint.text = t("ov.download_help", start=start, n=n)
    background_tasks.create(estimate(), name="overview estimate")

    async def download():
        btn.disable()
        hint.text = t("ov.downloading")
        try:
            errs = await run.io_bound(O.download, StockStore(), ov.code, start, end)
        except Exception as e:  # noqa: BLE001 - 额度用完、IP 被暂停等：说明原因，已下载的保留
            btn.enable()
            with out:
                notice(f"{type(e).__name__}: {e}", "error", "error")
            return
        if errs:
            ui.notify(t("ov.dl_errors", e="; ".join(f"{c}: {m}" for c, m in errs.items())), type="warning")
        reload()
    btn.on_click(download)


def _valuation(ov, lg):
    ui.label(t("ov.sec_val")).classes("sq-serif text-base font-semibold mt-2")
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
        for k in ("pe", "pb"):
            info = ov.pct.get(k) or {}
            v = info.get("value", np.nan)
            if np.isfinite(info.get("pct", np.nan)):
                delta = t("ov.pct", since=info["since"].date(), pct=info["pct"])
            elif np.isfinite(v) and v <= 0:
                delta = t("ov.pct_loss")
            else:
                delta = t("ov.pct_short")
            metric_tile(t(f"ov.{k}"), _num(v), delta=delta)
    with ui.card().classes("w-full p-2"):
        plot(valuation_chart(ov.valuation, lg))
    since = (ov.pct.get("pe") or {}).get("since")
    ui.label(t("ov.val_note", since=since.date() if since is not None else "—")).classes("sq-caption")


def _financials(ov):
    ui.label(t("ov.sec_fin")).classes("sq-serif text-base font-semibold mt-2")
    f = ov.fin
    df = pd.DataFrame({t("ov.col_period"): [_period(r) for r in f.itertuples()],
                       t("ov.col_pub"): f["pub_date"].dt.date,
                       t("ov.col_roe"): f["roe"], t("ov.col_gm"): f["gross_margin"], t("ov.col_nm"): f["net_margin"],
                       t("ov.col_np"): f["np_yoy"], t("ov.col_rev"): f["rev_yoy"]}).iloc[::-1]
    cols = [t(f"ov.col_{k}") for k in ("roe", "gm", "nm", "np", "rev")]
    df_table(fmt_table(df, {c: "{:.1f}" for c in cols})).mark("ov_fin")
    ui.label(t("ov.fin_note")).classes("sq-caption")


def _dividends(ov):
    ui.label(t("ov.sec_div")).classes("sq-serif text-base font-semibold mt-2")
    d = ov.dividends
    if d.empty:
        ui.label(t("ov.div_none")).classes("sq-muted text-sm")
        return
    df = pd.DataFrame({t("ov.col_year"): d["year"], t("ov.col_times"): d["times"], t("ov.col_cash"): d["cash"],
                       t("ov.col_bonus"): d["bonus"], t("ov.col_reserve"): d["reserve"],
                       t("ov.col_yield"): d["yield"]}).iloc[::-1]
    df_table(fmt_table(df, {t("ov.col_cash"): "{:.4g}", t("ov.col_bonus"): "{:.4g}", t("ov.col_reserve"): "{:.4g}",
                            t("ov.col_yield"): "{:.2f}"})).mark("ov_div")
    ui.label(t("ov.div_note")).classes("sq-caption")
    if ov.div_patched:
        ui.label(t("ov.div_patched", n=ov.div_patched)).classes("sq-caption")


def _risk(ov):
    ui.label(t("ov.sec_risk")).classes("sq-serif text-base font-semibold mt-2")
    r = ov.risk
    df = pd.DataFrame({t("ov.col_period"): [_period(x) for x in r.itertuples()],
                       t("ov.col_pub"): r["pub_date"].dt.date,
                       t("ov.col_debt"): r["debt_ratio"], t("ov.col_cr"): r["current_ratio"],
                       t("ov.col_cfo"): r["cfo_np"]}).iloc[::-1]
    df_table(fmt_table(df, {t("ov.col_debt"): "{:.1f}", t("ov.col_cr"): "{:.2f}", t("ov.col_cfo"): "{:.2f}"})) \
        .mark("ov_risk")
    ui.label(t("ov.risk_note")).classes("sq-caption")


# ================= 选股结果：某一期入选的股票 =================
def picks_dialog(SP, panel, rspec: dict, res, date: pd.Timestamp):
    """各入选股票的因子原始值与标准分；股票池为股票时点某一行打开个股概览"""
    from simplequant.stocks import FACTORS, universe as U
    codes = res.schedule.picks.get(date, [])
    is_stock = U.kind(rspec["universe"]) == "stock"
    with ui.dialog() as dlg, ui.card().classes("w-[1000px] max-w-full gap-3").mark("pick_dialog"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(t("sp.pick_title", d=date.date())).classes("sq-serif text-base font-semibold mt-2")
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")
        box = ui.column().classes("w-full gap-2")
    dlg.open()
    with box:
        if not codes:
            ui.label(t("sp.pick_cash")).classes("sq-muted")
            return
        with ui.row().classes("items-center gap-2") as wait:
            ui.spinner(size="sm")
            ui.label(t("sp.pick_loading")).classes("sq-muted")

    # 同一次回测结果里多次打开时共用因子计算
    cache = SP.setdefault("pick_cache", {})
    if cache.get("res") is not res:
        cache.clear()
        cache["res"], cache["data"] = res, {}

    async def load():
        try:
            info = await run.io_bound(pick_details, panel, rspec, date, codes, cache["data"])
        except Exception as e:  # noqa: BLE001 - 自定义因子被删除等
            wait.delete()
            with box:
                notice(f"{type(e).__name__}: {e}", "error", "error")
            return
        wait.delete()
        weighting = rspec.get("weighting", "manual")
        heads = {}
        for f in rspec["factors"]:
            name = p(FACTORS[f["key"]]["label"]) if f["key"] in FACTORS else f["key"]
            if weighting == "manual":
                name += "（" + t("sel.dir_up" if f.get("direction", 1) > 0 else "sel.dir_down") + "）" \
                    if lang() == "zh" else f" ({t('sel.dir_up' if f.get('direction', 1) > 0 else 'sel.dir_down')})"
            heads[f["key"]] = name
        scores = res.schedule.scores.get(date, {})
        labels = {}
        rows = []
        for i, c in enumerate(codes, 1):
            label = f"{res.names.get(c, c)}({code6(c)})"
            labels[label] = c
            row = {t("sp.pick_rank"): i, t("sp.pick_stock"): label, t("sp.pick_score"): _num(scores.get(c, np.nan))}
            for k, h in heads.items():
                raw, z = info[c][k]
                row[h] = f"{_num(raw, '{:.4g}')}（{_num(z, '{:+.2f}')}）" if np.isfinite(raw) else ""
            rows.append(row)
        with box:
            tbl = df_table(pd.DataFrame(rows)).props("wrap-cells").mark("pick_table")
            note = t("sp.pick_note") + " " + (t("sp.pick_ml") if weighting == "ml" else
                                               t("sp.pick_ic", n=int(rspec.get("ic_lookback", 252)))
                                               if weighting in ("ic", "icir") else t("sp.pick_manual"))
            ui.label(note).classes("sq-caption")
            if is_stock:
                ui.label(t("sp.pick_open")).classes("sq-muted text-xs")
                tbl.classes("cursor-pointer")
                tbl.on("rowClick", lambda e: overview_dialog(labels[e.args[1][t("sp.pick_stock")]], SP))
    background_tasks.create(load(), name="stock overview")
