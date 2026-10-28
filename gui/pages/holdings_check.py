"""选股页「持仓诊断」：输入持仓（逐只添加、粘贴、读取模拟账户），与所选股票池对照（核心见 simplequant/stocks/diagnosis.py）"""

import numpy as np
import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant.stocks import StockStore, UNIVERSES, today
from simplequant.stocks import diagnosis as D
from simplequant.stocks import overview as O
from simplequant.stocks import universe as U
from gui.common import t, p, lang
from gui.components import metric_tile
from gui.widgets import df_table, fmt_table, plot, notice
from gui.pages.stock_overview import code6, _num, overview_dialog
from ui.charts import corr_heatmap, equity_chart

DENSE = "outlined dense"


def _industry(s: str) -> str:
    """BaoStock 行业带门类字母和两位代码（如 J66货币金融服务），只显示名称（同个股概览）"""
    return s[3:] if s[:1].isalpha() and s[1:3].isdigit() else (s or "—")


def _names(basics) -> dict:
    return {} if basics is None else dict(zip(basics["code"], basics["name"]))


def _basics():
    try:
        return StockStore().load_basics()
    except (OSError, ValueError):
        return None


def diag_tab(SP, get_panel):
    """get_panel(股票池, 起, 止)：选股页共用的面板缓存"""
    hd = SP.setdefault("hd", {"holdings": D.load_holdings(), "pool": None, "result": None})
    stock_pools = [u for u in UNIVERSES if U.kind(u) == "stock"]
    if hd["pool"] not in stock_pools:
        hd["pool"] = SP["universe"] if SP["universe"] in stock_pools else "hs300"
    basics = _basics()
    names = _names(basics)

    ui.label(t("hd.intro")).classes("sq-muted text-sm")
    ui.label(t("hd.sec_input")).classes("sq-serif text-base font-semibold mt-2")
    with ui.row().classes("w-full items-center gap-3"):
        q = ui.input(t("hd.add")).props(DENSE).classes("w-48").mark("hd_query")
        n = ui.number(t("hd.add_shares"), value=100, min=0, step=100, format="%.0f").props(DENSE).classes("w-32") \
            .mark("hd_shares")
        add_btn = ui.button(t("hd.add_btn"), icon="add").props("unelevated no-caps").mark("hd_add")
        paste_btn = ui.button(t("hd.paste"), icon="content_paste").props("outline no-caps").mark("hd_paste")
        acc_btn = ui.button(t("hd.from_account"), icon="account_balance_wallet").props("outline no-caps") \
            .mark("hd_account")
    choices = ui.row().classes("w-full items-center gap-2")
    list_box = ui.column().classes("w-full gap-1")
    with ui.row().classes("w-full items-center gap-3 mt-2"):
        pool = ui.select({u: p(UNIVERSES[u]["label"]) for u in stock_pools}, value=hd["pool"],
                         label=t("hd.pool")).props(DENSE).classes("w-44").mark("hd_pool")
        run_btn = ui.button(t("hd.run"), icon="fact_check").props("unelevated no-caps").mark("hd_run")
    out = ui.column().classes("w-full gap-4")

    def save():
        D.save_holdings(hd["holdings"])

    def set_holdings(items: list[dict], append: bool = False):
        cur = {h["code"]: h["shares"] for h in hd["holdings"]} if append else {}
        for h in items:
            cur[h["code"]] = cur.get(h["code"], 0) + h["shares"] if append else h["shares"]
        hd["holdings"] = [{"code": c, "shares": s} for c, s in cur.items() if s > 0]
        save()
        draw_list()

    def draw_list():
        list_box.clear()
        with list_box:
            if not hd["holdings"]:
                ui.label(t("hd.empty")).classes("sq-muted text-sm")
                return
            for h in hd["holdings"]:
                with ui.row().classes("items-center gap-3"):
                    ui.label(f"{names.get(h['code'], h['code'])}（{code6(h['code'])}）" if lang() == "zh" else
                             f"{names.get(h['code'], h['code'])} ({code6(h['code'])})").classes("w-48")
                    ui.number(t("hd.col_shares"), value=h["shares"], min=0, step=100, format="%.0f") \
                        .props(DENSE).classes("w-32") \
                        .on_value_change(lambda e, h=h: (h.update(shares=float(e.value or 0)), save()))
                    ui.button(icon="close", on_click=lambda c=h["code"]: remove(c)).props("flat round dense")
            ui.button(t("hd.clear"), icon="delete_sweep", on_click=lambda: set_holdings([])) \
                .props("flat dense no-caps").mark("hd_clear")

    def remove(code):
        hd["holdings"] = [h for h in hd["holdings"] if h["code"] != code]
        save()
        draw_list()

    def add_code(code):
        choices.clear()
        set_holdings([{"code": code, "shares": float(n.value or 0)}], append=True)
        q.value = ""

    def add():
        choices.clear()
        text = (q.value or "").strip()
        if not text or not (n.value or 0) > 0:
            return
        hits = O.find_stocks(basics, text)
        if hits.empty:
            ui.notify(t("hd.not_found", q=text), type="warning")
            return
        exact = hits.iloc[0]["code"].endswith(text) or hits.iloc[0]["name"] == text
        if len(hits) > 1 and not exact:
            with choices:
                for r in hits.itertuples():
                    ui.button(f"{r.name}({code6(r.code)})", on_click=lambda c=r.code: add_code(c)) \
                        .props("flat dense no-caps")
            return
        add_code(hits.iloc[0]["code"])

    def paste_dialog():
        with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-full gap-3").mark("hd_paste_dialog"):
            ui.label(t("hd.paste_help")).classes("sq-muted text-sm")
            box = ui.textarea().props("outlined autogrow").classes("w-full").mark("hd_paste_text")
            msg = ui.column().classes("w-full")

            def apply(append):
                items, bad = D.parse_text(box.value, basics)
                msg.clear()
                if not items:
                    with msg:
                        notice(t("hd.paste_none"), "info")
                    return
                set_holdings(items, append=append)
                if bad:
                    ui.notify(t("hd.paste_bad", n=len(bad)) + " " + "；".join(bad[:5]), type="warning", multi_line=True)
                dlg.close()
            with ui.row().classes("gap-2"):
                ui.button(t("hd.paste_replace"), on_click=lambda: apply(False)).props("unelevated no-caps") \
                    .mark("hd_paste_replace")
                ui.button(t("hd.paste_append"), on_click=lambda: apply(True)).props("outline no-caps")
        dlg.open()

    def account_dialog():
        from simplequant.paper.account import list_accounts
        accs = [(a, D.from_account(a.state())) for a in list_accounts()]
        accs = [(a, h) for a, h in accs if h]
        with ui.dialog() as dlg, ui.card().classes("w-[480px] max-w-full gap-3").mark("hd_account_dialog"):
            if not accs:
                ui.label(t("hd.account_none")).classes("sq-muted")
            for a, h in accs:
                ui.button(f"{a.name}（{len(h)}）" if lang() == "zh" else f"{a.name} ({len(h)})",
                          on_click=lambda h=h: (set_holdings(h), dlg.close())).props("flat no-caps")
            ui.label(t("hd.account_help")).classes("sq-caption")
        dlg.open()

    q.on("keydown.enter", add)
    add_btn.on_click(add)
    paste_btn.on_click(paste_dialog)
    acc_btn.on_click(account_dialog)
    pool.on_value_change(lambda e: hd.update(pool=e.value))
    run_btn.on_click(lambda: run_check(hd, out, get_panel, SP))
    draw_list()
    if hd.get("result") is not None:
        with out:
            _render(hd["result"], hd["result_pool"], SP, lambda: run_check(hd, out, get_panel, SP))


def run_check(hd, out, get_panel, SP):
    out.clear()
    if not hd["holdings"]:
        return
    universe = hd["pool"]
    st = StockStore()
    if not U.ready(universe, st):
        with out:
            notice(t("hd.pool_missing", u=p(UNIVERSES[universe]["label"])), "info")
        return
    with out:
        with ui.row().classes("items-center gap-2") as wait:
            ui.spinner(size="sm")
            ui.label(t("hd.loading")).classes("sq-muted")

    def work():
        end = U.load_benchmark(universe, st).index[-1]
        start, e = D.panel_start(end), str(end.date())
        pool_panel = get_panel(universe, start, e)
        codes = [h["code"] for h in hd["holdings"]]
        return D.diagnose(hd["holdings"], pool_panel, D.build_hold_panel(st, universe, codes, start, e))

    async def load():
        try:
            res = await run.io_bound(work)
        except Exception as ex:  # noqa: BLE001 - 数据文件损坏等：说明原因
            wait.delete()
            with out:
                notice(f"{type(ex).__name__}: {ex}", "error", "error")
            return
        hd["result"], hd["result_pool"] = res, universe
        out.clear()
        with out:
            _render(res, universe, SP, lambda: run_check(hd, out, get_panel, SP))
    background_tasks.create(load(), name="holdings check")


# ================= 结果 =================
def _render(d: D.Diagnosis, universe: str, SP, rerun):
    lg = lang()
    uname = p(UNIVERSES[universe]["label"])
    names = dict(zip(d.holdings["code"], d.holdings["name"]))
    label = lambda c: f"{names.get(c, c)}（{code6(c)}）" if lg == "zh" else f"{names.get(c, c)} ({code6(c)})"
    sep = "、" if lg == "zh" else ", "
    _problems(d, SP, rerun, sep)
    if d.holdings.empty:
        return
    h = d.holdings
    ui.label(t("hd.asof", d=d.asof.date(), u=uname, n=d.pool_size)).classes("sq-caption")

    c = d.concentration
    with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-6"):
        metric_tile(t("hd.total"), f"{h['value'].sum():,.0f}")
        metric_tile(t("hd.n"), str(c["n"]))
        metric_tile(t("hd.top1"), f"{c['top1']:.1%}")
        metric_tile(t("hd.top3"), f"{c['top3']:.1%}")
        metric_tile(t("hd.eff_n"), f"{c['effective_n']:.1f}", help_text=t("hd.eff_n_help"))
        metric_tile(t("hd.n_ind"), str(c["industries"]))

    # 各股明细：点击一行打开个股概览
    ui.label(t("hd.sec_hold")).classes("sq-serif text-base font-semibold mt-2")
    labels = {label(r.code): r.code for r in h.itertuples()}
    pct = lambda v, x: f"{_num(v)}（{_num(x, '{:.0f}')}）" if np.isfinite(v) else ""
    df = pd.DataFrame({
        t("hd.col_stock"): [label(r.code) for r in h.itertuples()],
        t("hd.col_shares"): h["shares"].map("{:,.0f}".format),
        t("hd.col_price"): h["price"].map(_num),
        t("hd.col_value"): h["value"].map("{:,.0f}".format),
        t("hd.col_weight"): (h["weight"] * 100).map("{:.1f}".format),
        t("hd.col_industry"): h["industry"].map(_industry),
        t("hd.style_mcap"): [t("hd.mcap_value", v=v / 1e8, w=v / 1e9) + f"（{_num(x, '{:.0f}')}）"
                             if np.isfinite(v) else "" for v, x in zip(h["mcap"], h["mcap_pct"])],
        **{t(f"hd.style_{k}"): [pct(v, x) for v, x in zip(h[k], h[f"{k}_pct"])] for k in ("pe", "pb", "dy", "roe")},
    })
    tbl = df_table(df).props("wrap-cells").classes("cursor-pointer").mark("hd_table")
    tbl.on("rowClick", lambda e: overview_dialog(labels[e.args[1][t("hd.col_stock")]], SP))
    ui.label(t("sp.pick_open")).classes("sq-muted text-xs")

    # 风格
    ui.label(t("hd.sec_style")).classes("sq-serif text-base font-semibold mt-2")
    rows = []
    for k in D.STYLE:
        med = d.pool_median[k]
        med_s = (t("hd.mcap_value", v=med / 1e8, w=med / 1e9) if k == "mcap" else _num(med)) if np.isfinite(med) else ""
        rows.append({"": t(f"hd.style_{k}"), t("hd.style_pct"): _num(d.style_pct[k], "{:.0f}"),
                     t("hd.style_median"): med_s, t("hd.style_cover"): f"{d.style_cover[k] * 100:.0f}"})
    df_table(pd.DataFrame(rows)).mark("hd_style")
    ui.label(t("hd.style_note")).classes("sq-caption")

    # 行业
    ui.label(t("hd.sec_ind")).classes("sq-serif text-base font-semibold mt-2")
    ind = d.industries
    df_table(pd.DataFrame({t("hd.col_industry"): ind["industry"].map(_industry),
                           t("hd.col_weight"): (ind["weight"] * 100).map("{:.1f}".format),
                           t("hd.col_pool_w"): (ind["pool_weight"] * 100).map("{:.1f}".format)})).mark("hd_ind")
    ui.label(t("hd.ind_note")).classes("sq-caption")

    # 相关性
    ui.label(t("hd.sec_corr")).classes("sq-serif text-base font-semibold mt-2")
    if d.corr is None:
        ui.label(t("hd.corr_one")).classes("sq-muted text-sm")
    else:
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            metric_tile(t("hd.avg_corr"), _num(d.avg_corr))
        if d.top_pairs:
            pairs = sep.join(f"{a} × {b} {v:.2f}" for a, b, v in d.top_pairs)
            ui.label(t("hd.top_pairs", pairs=pairs)).classes("text-sm")
        with ui.card().classes("w-full p-2"):
            plot(corr_heatmap(d.corr, {}, lg))
        ui.label(t("hd.corr_note", n=D.LOOKBACK_DAYS, m=D.MIN_CORR_DAYS)).classes("sq-caption")

    # 假设持有一年
    if d.curve is not None:
        ui.label(t("hd.sec_perf")).classes("sq-serif text-base font-semibold mt-2")
        bench = t("hd.bench_tr" if d.bench_total_return else "hd.bench_price", u=uname)
        pf, bm = d.perf["portfolio"], d.perf["benchmark"]
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            for k, key in (("total_return", "hd.ret"), ("volatility", "hd.vol"), ("max_drawdown", "hd.mdd")):
                metric_tile(t(key), f"{pf[k]:.1%}", delta=t("hd.bench_delta", v=f"{bm[k]:.1%}"))
            metric_tile(t("hd.beta"), _num(d.perf["beta"]))
        with ui.card().classes("w-full p-2"):
            plot(equity_chart(d.curve, lg, benchmark_label=bench, strategy_label=t("hd.holdings_label")))
        ui.label(t("hd.perf_note", n=D.LOOKBACK_DAYS, b=bench)).classes("sq-caption")
        if d.short_history:
            ui.label(t("hd.perf_short", d=d.curve.index[0].date())).classes("sq-caption")


def _problems(d: D.Diagnosis, SP, rerun, sep):
    """缺数据、数据旧、对照日没有价格：提示，缺的可以只下载这几只"""
    stale = d.holdings.loc[d.holdings["stale"], "code"].tolist() if len(d.holdings) else []
    if d.unpriced:
        notice(t("hd.unpriced", d=d.asof.date(), codes=sep.join(code6(c) for c in d.unpriced)), "info")
    need = d.missing + stale
    if not need:
        return
    if d.missing:
        notice(t("hd.missing", codes=sep.join(code6(c) for c in d.missing)), "info")
    if stale:
        notice(t("hd.stale", codes=sep.join(code6(c) for c in stale)), "warning", "warning")
    start, end = SP["dl"]["start"], today()
    with ui.row().classes("items-center gap-3"):
        btn = ui.button(t("hd.download"), icon="download").props("outline no-caps").mark("hd_download")
        hint = ui.label().classes("sq-muted text-xs")

    async def estimate():
        n = await run.io_bound(D.requests_needed, StockStore(), need, start, end)
        hint.text = t("hd.download_help", start=start, n=n)
    background_tasks.create(estimate(), name="holdings estimate")

    async def download():
        btn.disable()
        hint.text = t("hd.downloading")
        try:
            errs = await run.io_bound(D.download, StockStore(), need, start, end)
        except Exception as e:  # noqa: BLE001 - 额度用完、IP 被暂停等：说明原因，已下载的保留
            btn.enable()
            notice(f"{type(e).__name__}: {e}", "error", "error")
            return
        if errs:
            ui.notify(t("ov.dl_errors", e="; ".join(f"{c}: {m}" for c, m in errs.items())), type="warning")
        rerun()
    btn.on_click(download)
