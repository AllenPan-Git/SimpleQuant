"""第 1 页：数据（在线下载 / 导入 CSV / 本地数据库）"""

import datetime as dt
import io
import os

import pandas as pd
from nicegui import run, ui

from simplequant.data import SOURCES, ASSET_TYPES, RESAMPLE_RULES, fetch_to_library, library
from simplequant.data.base import freq_label, EXTRA_COLUMNS
from simplequant.data.csv_src import read_csv_any, is_snapshot
from simplequant.data.tdx_local_src import DEFAULT_TDX_DIR
from simplequant.rules import INDICATORS
from gui.common import t, p, lang
from gui.layout import frame, page_title
from gui.widgets import df_table, plot, Progress
from ui.charts import preview_chart
from ui.shared import POPULAR, ADJUST, parse_symbols, import_csv, dataset_label


def page():
    with frame("/data"):
        page_title("/data")
        with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
            tab_dl = ui.tab("dl", t("data.tab_download"), icon="download")
            tab_csv = ui.tab("csv", t("data.tab_csv"), icon="upload_file")
            tab_lib = ui.tab("lib", t("data.tab_library"), icon="inventory_2")
        with ui.tab_panels(tabs, value=tab_dl).classes("w-full bg-transparent"):
            with ui.tab_panel(tab_dl).classes("px-0"):
                _download_tab(on_change=lambda: refresh_lib())
            with ui.tab_panel(tab_csv).classes("px-0"):
                _csv_tab(on_change=lambda: refresh_lib())
            with ui.tab_panel(tab_lib).classes("px-0"):
                lib_box = ui.column().classes("w-full gap-2")

        def refresh_lib():
            """本页的数据库列表重画（每次打开页面各自一份，不用模块级 refreshable）"""
            lib_box.clear()
            with lib_box:
                _library_view(refresh_lib)
        refresh_lib()


# ---------------- 在线下载 ----------------
def _download_tab(on_change):
    lg = lang()
    online = {k: s for k, s in SOURCES.items() if k != "csv"}
    today = dt.date.today()

    with ui.card().classes("w-full gap-3"):
        src_key = ui.radio({k: p(s.label) for k, s in online.items()}, value=next(iter(online))).props("inline")
        desc = ui.label().classes("text-sm sq-muted -mt-2")

        with ui.row().classes("w-full items-start gap-4 no-wrap"):
            picks = ui.select({c: f"{c} {p(n)}" for c, n in POPULAR.items()}, label=t("data.popular"),
                              value=["510300"], multiple=True).props("use-chips outlined dense").classes("grow")
            extra = ui.input(t("data.other_codes"), placeholder=t("data.other_codes_ph")) \
                .props("outlined dense").classes("w-72")

        with ui.row().classes("w-full items-start gap-4"):
            freq = ui.select({}, label=t("data.freq")).props("outlined dense").classes("w-40")
            adjust = ui.select({a: t(k) for a, k in ADJUST.items()}, label=t("data.adjust"), value="hfq") \
                .props("outlined dense").classes("w-40").tooltip(t("data.adjust_help"))
            start = ui.input(t("data.start"), value="2020-01-01").props("outlined dense type=date").classes("w-44")
            end = ui.input(t("data.end"), value=str(today)).props("outlined dense type=date").classes("w-44")
            asset = ui.select({a: p(v) for a, v in ASSET_TYPES.items()}, label=t("data.asset"),
                              value=next(iter(ASSET_TYPES))).props("outlined dense").classes("w-40")
        tdx_dir = ui.input(t("data.tdx_dir"), value=DEFAULT_TDX_DIR).props("outlined dense").classes("w-full")
        force = ui.checkbox(t("data.force"))

        def on_source():
            s = online[src_key.value]
            desc.text = p(s.description)
            freq.options = {f: freq_label(f, lg) for f in s.freqs}
            freq.value = s.freqs[0]
            freq.update()
            asset.set_visibility(src_key.value == "akshare")
            tdx_dir.set_visibility(src_key.value == "tdx_local")
            adjust.set_enabled(src_key.value != "tdx_local")
        src_key.on_value_change(on_source)
        on_source()

        results = ui.column().classes("w-full gap-2")

        async def download():
            symbols = parse_symbols(picks.value or [], extra.value or "")
            if not symbols:
                ui.notify(t("data.other_codes_ph"), type="warning")
                return
            btn.disable()
            results.clear()
            key = src_key.value
            kwargs = {"asset": asset.value} if key == "akshare" else {}
            if key == "tdx_local":
                kwargs["tdx_dir"] = tdx_dir.value
            adj = "" if key == "tdx_local" else adjust.value
            try:
                for sym in symbols:
                    with results:
                        row = ui.row().classes("items-center gap-2")
                        with row:
                            ui.spinner(size="sm")
                            ui.label(t("data.fetching", sym=sym))
                    name = f"{sym} {p(POPULAR[sym])}" if sym in POPULAR else sym
                    try:
                        meta = await run.io_bound(fetch_to_library, key, sym, start.value, end.value, freq=freq.value,
                                                  adjust=adj, name=name, force=force.value, **kwargs)
                        _status(row, "check_circle", "text-positive",
                                t("data.fetched", sym=sym, n=meta.rows, start=meta.start, end=meta.end))
                    except Exception as e:  # noqa: BLE001
                        _status(row, "error", "text-negative", t("data.fetch_failed", sym=sym), str(e))
            finally:
                btn.enable()
                on_change()

        btn = ui.button(t("data.download"), icon="download", on_click=download).props("unelevated no-caps")

    _rates_card()


def _rates_card():
    """利率与信用利差（中债收益率曲线）：择时规则的「利率/信用利差」条件用"""
    from simplequant.bonds.rates import RatesStore, FIRST_YEAR
    st = RatesStore()
    with ui.card().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("percent").classes("text-primary")
            ui.label(t("rates.title")).classes("font-semibold")
        ui.label(t("rates.note", year=FIRST_YEAR)).classes("text-sm sq-muted")
        status = ui.label().classes("text-sm")
        prog = Progress()

        def show():
            if st.ready():
                m = st.load().dropna()
                last = m.iloc[-1] if len(m) else None
                status.text = t("rates.status", start=m.index[0].date(), end=m.index[-1].date(),
                                cgb=f"{last['cgb10y']:.2f}", term=f"{last['term_spread']:.0f}",
                                credit=f"{last['credit_spread']:.0f}") if last is not None else ""
            else:
                status.text = t("rates.none")
        show()

        async def update():
            b.disable()
            prog.start()
            try:
                await run.io_bound(st.update, FIRST_YEAR, None, lambda i, n: prog.set(i / n, f"{i}/{n}"))
                ui.notify(t("rates.done"), type="positive")
            except Exception as e:  # noqa: BLE001
                ui.notify(f"{type(e).__name__}: {e}", type="negative", multi_line=True)
            finally:
                prog.stop()
                b.enable()
            show()
        b = ui.button(t("rates.update"), icon="download", on_click=update).props("outline no-caps") \
            .classes("self-start").mark("rates_update")


def _status(row, icon: str, color: str, text: str, detail: str = ""):
    row.clear()
    with row:
        ui.icon(icon).classes(color)
        ui.label(text)
        if detail:
            ui.label(detail).classes("text-sm text-negative")


# ---------------- 导入 CSV ----------------
def _csv_tab(on_change):
    files: list[tuple[str, object]] = []      # (文件名, 路径 或 bytes)

    def opened(src):
        return io.BytesIO(src) if isinstance(src, bytes) else src

    with ui.card().classes("w-full gap-3"):
        ui.markdown(t("data.csv_intro"))
        mode = ui.radio({"upload": t("data.file_upload"), "path": t("data.file_path")}, value="upload") \
            .props("inline").mark("csv-mode")
        uploader = ui.upload(label=t("data.choose_csv"), multiple=True, auto_upload=True,
                             on_multi_upload=lambda e: _on_upload(e)).props("accept=.csv flat bordered") \
            .classes("w-full")
        path = ui.input(t("data.path"), placeholder=r"D:\data\snapshots\019742") \
            .props("outlined dense clearable").classes("w-full")
        path_note = ui.label().classes("text-sm")
        preview = ui.column().classes("w-full gap-3")

        def on_mode():
            uploader.set_visibility(mode.value == "upload")
            path.set_visibility(mode.value == "path")
            path_note.set_visibility(mode.value == "path")
            files.clear()
            preview.clear()
        mode.on_value_change(on_mode)

        async def _on_upload(e):
            files.clear()
            for f in e.files:
                files.append((f.name, await f.read()))
            show_preview()

        def on_path():
            files.clear()
            v = (path.value or "").strip().strip('"')
            path_note.classes(remove="text-negative")
            if v and os.path.isdir(v):
                files.extend((n, os.path.join(v, n)) for n in sorted(os.listdir(v)) if n.lower().endswith(".csv"))
                path_note.text = t("data.folder_files", n=len(files))
            elif v and os.path.isfile(v):
                files.append((os.path.basename(v), v))
                path_note.text = ""
            else:
                path_note.text = t("data.path_missing") if v else ""
                path_note.classes(add="text-negative")
            show_preview()
        path.on("blur", on_path)
        path.on("keydown.enter", on_path)
        on_mode()

        def show_preview():
            preview.clear()
            if not files:
                return
            try:
                head = read_csv_any(opened(files[0][1]))
            except Exception as e:  # noqa: BLE001
                with preview:
                    ui.label(t("data.import_failed", e=e)).classes("text-negative")
                return
            snapshot = is_snapshot(head)
            with preview:
                df_table(head.head(5))
                with ui.row().classes("w-full items-start gap-4"):
                    ui.markdown(t("data.is_snapshot" if snapshot else "data.is_kline")).classes("text-sm sq-note")
                    if snapshot:
                        rule = ui.select({r: p(v) for r, v in RESAMPLE_RULES.items()}, label=t("data.resample_to"),
                                         value=list(RESAMPLE_RULES)[2])
                    else:
                        rule = ui.select({"": t("data.keep"), **{r: p(v) for r, v in RESAMPLE_RULES.items()}},
                                         label=t("data.resample_opt"), value="")
                    rule.props("outlined dense").classes("w-48")
                    name = ui.input(t("data.name"), value=os.path.splitext(files[0][0])[0].split("_")[0]) \
                        .props("outlined dense").classes("w-48")
                msg = ui.label()

                async def do_import():
                    btn.disable()
                    msg.text = t("data.importing")
                    msg.classes(replace="")
                    try:
                        meta = await run.io_bound(import_csv, [opened(f) for _, f in files], rule.value or None,
                                                  name.value)
                        msg.text = t("data.imported", n=meta.rows, start=meta.start, end=meta.end)
                        msg.classes(replace="text-positive")
                        on_change()
                    except Exception as e:  # noqa: BLE001
                        msg.text = t("data.import_failed", e=e)
                        msg.classes(replace="text-negative")
                    finally:
                        btn.enable()
                btn = ui.button(t("data.import"), icon="upload", on_click=do_import).props("unelevated no-caps")


# ---------------- 本地数据库 ----------------
def _library_view(refresh):
    lg = lang()
    metas = library.list_datasets()
    if not metas:
        ui.label(t("data.empty")).classes("sq-muted")
        return
    df_table(pd.DataFrame([{
        t("lib.name"): m.name, t("lib.freq"): freq_label(m.freq, lg),
        t("lib.source"): t(f"src.{m.source}") + (f"（{t('prov.' + m.extra['provider'])}）"
                                                 if m.extra.get("provider") else ""),
        t("lib.adjust"): t(ADJUST.get(m.adjust, "adj.none")), t("lib.start"): m.start, t("lib.end"): m.end,
        t("lib.rows"): m.rows, t("lib.updated"): m.created} for m in metas]))

    by_id = {m.id: m for m in metas}
    sel = ui.select({i: dataset_label(m, lg) for i, m in by_id.items()}, label=t("lib.preview"),
                    value=next(iter(by_id))).props("outlined dense").classes("w-full")
    detail = ui.column().classes("w-full gap-2")

    def show():
        detail.clear()
        m = by_id[sel.value]
        df = library.load(sel.value)
        factor_cols = [c for c in EXTRA_COLUMNS if c in df.columns]
        with detail:
            ui.label(t("lib.factors", cols=", ".join(p(INDICATORS[c]["label"]) for c in factor_cols))
                     if factor_cols else t("lib.no_factors")).classes("text-sm sq-muted")
            plot(preview_chart(df, m.name, lg))
            with ui.row().classes("gap-2"):
                ui.button(t("lib.download_csv"), icon="download", on_click=lambda: ui.download.content(
                    df.rename_axis("date").reset_index().to_csv(index=False).encode("utf-8-sig"),
                    f"{m.symbol}.csv", "text/csv")).props("outline no-caps")
                ui.button(t("lib.delete"), icon="delete", on_click=lambda: confirm_delete(m, refresh)) \
                    .props("outline no-caps color=negative")
    sel.on_value_change(show)
    show()


async def confirm_delete(m, refresh):
    with ui.dialog() as dlg, ui.card():
        ui.label(t("lib.delete") + f"：{m.name}？")
        with ui.row().classes("w-full justify-end"):
            ui.button(t("gui.cancel"), on_click=lambda: dlg.submit(False)).props("flat no-caps")
            ui.button(t("lib.delete"), on_click=lambda: dlg.submit(True)) \
                .props("unelevated no-caps color=negative").mark("confirm-delete")
    ok = await dlg
    dlg.delete()
    if ok:
        library.delete(m.id)
        refresh()
