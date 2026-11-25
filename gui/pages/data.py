"""数据中心（界面重构计划 4.4.1）：行情（本地数据库 / 在线下载 / 导入 CSV）/ 选股数据 / 可转债 / 利率四个标签，
选股数据与可转债用选股包的下载构件（selection/data_tab.py）。"""

import asyncio
import contextlib
import datetime as dt
import io
import os
from urllib.parse import parse_qs, urlsplit

import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant.data import names as sec_names
from simplequant.data import (SOURCES, ASSET_TYPES, RESAMPLE_RULES, fetch_to_library, library, has_merge_candidates,
                              merge_library, can_update, update_dataset, update_library)
from simplequant.data.base import freq_label, EXTRA_COLUMNS
from simplequant.data.ratio_adjust import is_additive
from simplequant.data.csv_src import read_csv_any, is_snapshot
from simplequant.data.tdx_local_src import DEFAULT_TDX_DIR
from simplequant.i18n import TEXT
from simplequant.rules import INDICATORS
from gui import state, data_location_ui
from gui.paper_job import JOB
from gui.common import t, p, lang
from gui.layout import frame, page_title
from gui.widgets import df_table, plot, Progress, hint
from ui.charts import preview_chart
from ui.shared import POPULAR, ADJUST, parse_symbols, import_csv, dataset_label, sina_fallback


_NAMES_LOCK = asyncio.Lock()
_BENCH_RANGE_CACHE: dict[tuple[str, str, int], tuple[dt.date, dt.date]] = {}
_RATES_TEXT_CACHE: dict[tuple[str, int, str], str] = {}

CENTER_TABS = (("market", "data.tab_market", "candlestick_chart"), ("stocks", "data.tab_stocks", "filter_list"),
               ("cb", "data.tab_cb", "receipt_long"), ("rates", "data.tab_rates", "percent"))


def page():
    with frame("/data"):
        page_title("/data")
        data_location_ui.maybe_ask()
        _center()


# ---------------- 数据中心 ----------------
def _center():
    """按数据类型分标签：行情 / 选股数据 / 可转债 / 利率；当前标签记在 state.STATE["data_tab"]，未打开的标签按需懒加载"""
    keys = [k for k, _, _ in CENTER_TABS]
    q_tab = parse_qs(urlsplit(ui.context.client.sub_pages_router.current_path).query).get("tab", [None])[0]
    if q_tab in keys:
        state.STATE["data_tab"] = q_tab
    cur = state.STATE.get("data_tab") if state.STATE.get("data_tab") in keys else "market"
    builders = {
        "market": _market_blocks,
        "stocks": _stock_data,
        "cb": lambda: _universe_data("cb"),
        "rates": _rates_card,
    }
    tab_panels_map = {}
    built: set[str] = set()

    def ensure_tab(k: str):
        tp = tab_panels_map.get(k)
        if k in built or tp is None or tp.is_deleted or k not in builders:
            return
        built.add(k)
        with tp:
            builders[k]()

    with ui.tabs().classes("w-full").props("align=left no-caps") as tabs:
        for k, key, icon in CENTER_TABS:
            ui.tab(k, t(key), icon=icon)
    with ui.tab_panels(tabs, value=cur).classes("w-full bg-transparent").mark("data_tabs") as panels:
        for k, _, _ in CENTER_TABS:
            with ui.tab_panel(k).classes("px-0 gap-4") as tp:
                tab_panels_map[k] = tp
    ensure_tab(cur)

    def on_tab(e):
        state.STATE["data_tab"] = e.value
        ensure_tab(e.value)
    panels.on_value_change(on_tab)


def _market_blocks():
    """「行情」标签：不再套一层标签，自上而下是本地数据库 → 在线下载 → 导入 CSV（用得少，折叠）"""
    lib_head = _block_head(t("data.tab_library"), mark="data_lib_head", first=True)
    lib_box = ui.column().classes("w-full gap-2").mark("data_lib")

    def refresh_lib(metas: list | None = None):
        metas = library.list_datasets() if metas is None else metas
        lib_box.clear()
        lib_head.text = t("data.lib_count", n=len(metas))
        with lib_box:
            _library_view(refresh_lib, fold=True, metas=metas)
        return metas

    async def fill_names(metas: list):
        """名称只有代码的数据在后台补上简称（第一次要拉 BaoStock 证券列表，约 40 秒），补到了就刷新列表"""
        if not any(sec_names.needs_name(m) for m in metas):
            return
        async with _NAMES_LOCK:             # 打开页面时和下载完成后各补一次，不同时拉列表
            try:
                n = await run.io_bound(sec_names.fill_library, online=not JOB.running)
            except Exception:  # noqa: BLE001 - 网络问题：名称先留代码，下次打开再补
                return
        if n and not lib_box.is_deleted:
            refresh_lib()

    def changed():
        metas = refresh_lib()
        background_tasks.create(fill_names(metas), name="fill security names")

    _block_head(t("data.tab_download"))
    _download_tab(on_change=changed)
    with ui.expansion(t("data.tab_csv"), icon="upload_file").classes("w-full mt-4").mark("data_csv"):
        _csv_tab(on_change=refresh_lib)
    changed()


def _block_head(title: str, mark: str | None = None, first: bool = False):
    """区块标题（.sq-sec：宋体标题 + 右侧灰色小字），返回小字标签；first：紧接标签栏，不留上边距"""
    with ui.element("div").classes("sq-sec").style("margin-top: 4px" if first else ""):
        ui.label(title).classes("sq-h2")
        note = ui.label().classes("sq-muted text-sm")
    if mark:
        note.mark(mark)
    return note


def _stock_data():
    """选股数据：先选股票池（单独记在 state.STATE["data_uni"]，不改选股页当前的股票池），下面是下载与数据包"""
    from simplequant.stocks import UNIVERSES, universe as U
    stock = [u for u in UNIVERSES if U.kind(u) == "stock"]
    if state.STATE.get("data_uni") not in stock:
        sp_uni = (state.STATE.get("sp") or {}).get("universe")
        state.STATE["data_uni"] = sp_uni if sp_uni in stock else stock[0]

    sel = ui.select({u: p(UNIVERSES[u]["label"]) for u in stock}, label=t("sp.universe"),
                    value=state.STATE["data_uni"]).props("outlined dense options-dense").classes("w-48").mark("data_uni")

    box = ui.column().classes("w-full gap-4")
    body = _universe_data(lambda: state.STATE["data_uni"], box)

    def on_uni(e):
        state.STATE["data_uni"] = e.value
        body()
    sel.on_value_change(on_uni)


def _benchmark_bounds(u: str, st) -> tuple[dt.date, dt.date]:
    from simplequant.stocks import UNIVERSES, universe as U
    if U.kind(u) == "cb":
        from simplequant.bonds import CBStore
        p_idx = CBStore().root / "index.parquet"
    else:
        p_idx = st.root / "index" / f"{UNIVERSES[u]['index']}.parquet"
    mtime = p_idx.stat().st_mtime_ns if p_idx.exists() else -1
    ck = (str(p_idx), u, mtime)
    if ck not in _BENCH_RANGE_CACHE:
        idx = U.load_benchmark(u, st)
        _BENCH_RANGE_CACHE[ck] = (idx.index[0].date(), idx.index[-1].date())
    return _BENCH_RANGE_CACHE[ck]


def _universe_data(universe, box=None):
    """一个股票池的数据状态与下载（选股包的 _data_tab）。universe：股票池或返回股票池的函数；
    返回重画函数：下载、导入数据包完成后（以及换股票池时）整块重画，重新判断有没有数据"""
    from gui.pages.selection import sp_state
    from gui.pages.selection.common import Ctx
    from gui.pages.selection.data_tab import _data_tab
    from gui.sel_panel import store, universe_ready
    box = box or ui.column().classes("w-full gap-4")

    def draw():
        if box.is_deleted:
            return
        u = universe() if callable(universe) else universe
        ctx = Ctx()
        if universe_ready(u):
            ctx.lo, ctx.hi = _benchmark_bounds(u, store())
        box.clear()
        with box:
            _data_tab(sp_state(), ctx, draw, universe=u)
    draw()
    return draw


# ---------------- 在线下载 ----------------
def _download_tab(on_change):
    lg = lang()
    online = {k: s for k, s in SOURCES.items() if k != "csv"}
    today = dt.date.today()

    with ui.card().classes("w-full gap-3"):
        src_key = ui.radio({k: p(s.label) for k, s in online.items()}, value=next(iter(online))).props("inline")
        desc = ui.column().classes("w-full gap-0 -mt-2")

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
            desc.clear()
            with desc:      # 长说明收成一行（只有写了短句的来源）
                if f"data.src_short_{src_key.value}" in TEXT:
                    hint(t(f"data.src_short_{src_key.value}"), p(s.description), classes="text-sm sq-muted",
                         mark="data_src_hint")
                else:
                    ui.label(p(s.description)).classes("text-sm sq-muted")
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
                    row = None
                    if not results.is_deleted:
                        with results:
                            row = ui.row().classes("items-center gap-2")
                            with row:
                                ui.spinner(size="sm")
                                ui.label(t("data.fetching", sym=sym))
                    name = f"{sym} {p(POPULAR[sym])}" if sym in POPULAR else sym
                    try:
                        meta = await run.io_bound(fetch_to_library, key, sym, start.value, end.value, freq=freq.value,
                                                  adjust=adj, name=name, force=force.value, **kwargs)
                        text = t("data.fetched", sym=sym, n=meta.rows, start=meta.start, end=meta.end)
                        notes, warn = [], False
                        if is_additive(meta):          # 取不到复权因子，退回了东方财富的加法复权
                            notes.append(t("data.additive"))
                            warn = True
                        elif sina_fallback(meta):      # 东方财富连不上，改用了新浪
                            notes.append(t("data.fallback", prov=t(f"prov.{meta.extra['provider']}")))
                            warn = True
                        sep = "、" if lang() == "zh" else ", "
                        span = lambda ms: sep.join(f"{m.start} ~ {m.end}" for m in ms)   # noqa: E731
                        if getattr(meta, "merged", None):      # 与分段下载的旧数据拼成了一份
                            notes.append(t("data.merged", ranges=span(meta.merged)))
                        for reason in ("mismatch", "gap"):
                            ms = [m for m, r in getattr(meta, "unmerged", []) if r == reason]
                            if ms:
                                notes.append(t(f"data.unmerged_{reason}", ranges=span(ms)))
                                warn = True
                        if notes:
                            _status(row, "check_circle", "text-warning" if warn else "text-positive", text,
                                    " ".join(notes), "text-sm sq-muted")
                        else:
                            _status(row, "check_circle", "text-positive", text)
                    except Exception as e:  # noqa: BLE001
                        _status(row, "error", "text-negative", t("data.fetch_failed", sym=sym), str(e))
            finally:
                if not btn.is_deleted:          # 下载途中离开了页面：数据已存好，不再画（3.2.3 线上报错 refresh_lib）
                    btn.enable()
                    on_change()

        btn = ui.button(t("data.download"), icon="download", on_click=download).props("unelevated no-caps")



def _rates_card():
    """利率与信用利差（中债收益率曲线）：择时规则的「利率/利差」条件用"""
    from simplequant.bonds.rates import RatesStore, FIRST_YEAR
    st = RatesStore()
    with ui.card().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("percent").classes("text-primary")
            ui.label(t("rates.title")).classes("font-semibold")
        hint(t("rates.note_short"), t("rates.note", year=FIRST_YEAR), classes="text-sm sq-muted", mark="rates_hint")
        status = ui.label().classes("text-sm")
        prog = Progress()

        def show():
            if st.ready():
                mtime = st.path.stat().st_mtime_ns if st.path.exists() else -1
                ck = (str(st.path), mtime, lang())
                if ck not in _RATES_TEXT_CACHE:
                    m = st.load().dropna()
                    last = m.iloc[-1] if len(m) else None
                    _RATES_TEXT_CACHE[ck] = (
                        t("rates.status", start=m.index[0].date(), end=m.index[-1].date(),
                          cgb=f"{last['cgb10y']:.2f}", term=f"{last['term_spread']:.0f}",
                          credit=f"{last['credit_spread']:.0f}") if last is not None else ""
                    )
                status.text = _RATES_TEXT_CACHE[ck]
            else:
                status.text = t("rates.none")
        show()

        async def update():
            b.disable()
            prog.start()
            try:
                await run.io_bound(st.update, FIRST_YEAR, None, progress)
                error = None
            except Exception as e:  # noqa: BLE001
                error = e
            if b.is_deleted:                    # 更新途中离开了页面：不再提示、不再画（3.2.3 线上报错 update）
                return
            prog.stop()
            b.enable()
            if error is None:
                ui.notify(t("rates.done"), type="positive")
            else:
                ui.notify(f"{type(error).__name__}: {error}", type="negative", multi_line=True)
            show()

        def progress(i, n):
            if not b.is_deleted:                # 离开页面后继续下载，只是不再画进度
                prog.set(i / n, f"{i}/{n}")
        b = ui.button(t("rates.update"), icon="download", on_click=update).props("outline no-caps") \
            .classes("self-start").mark("rates_update")


def _status(row, icon: str, color: str, text: str, detail: str = "", detail_cls: str = "text-sm text-negative"):
    if row is None or row.is_deleted:          # 下载途中离开了页面：不再画（3.5.1 线上报错 _status）
        return
    row.clear()
    with row:
        ui.icon(icon).classes(color)
        ui.label(text)
        if detail:
            ui.label(detail).classes(detail_cls).style("white-space: pre-line")   # 各接口的原因分行显示


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
def _library_view(refresh, fold: bool = False, metas: list | None = None):
    """fold：预览（图表、下载 CSV、删除）收进折叠区（新版「行情」标签：免得图表把下面的在线下载挤远，且展开时才加载 Parquet 绘图）"""
    lg = lang()
    metas = library.list_datasets() if metas is None else metas
    if not metas:
        ui.label(t("data.empty")).classes("sq-muted")
        return
    with ui.row().classes("items-center gap-2"):
        if any(can_update(m) for m in metas):
            ubtn = ui.button(t("lib.update_all"), icon="update", on_click=lambda: update_latest(ubtn, prog, refresh)) \
                .props("outline no-caps").tooltip(t("lib.update_tip")).mark("data_lib_update")
        if has_merge_candidates(metas):     # 有同一标的、区间重叠或相接的几份数据时才显示
            btn = ui.button(t("lib.merge"), icon="merge", on_click=lambda: merge_split(btn, refresh)) \
                .props("outline no-caps").tooltip(t("lib.merge_tip")).mark("data_lib_merge")
    prog = Progress()
    df_table(pd.DataFrame([{
        t("lib.name"): m.name, t("lib.freq"): freq_label(m.freq, lg),
        t("lib.source"): t(f"src.{m.source}") + (f"（{t('prov.' + m.extra['provider'])}）"
                                                 if m.extra.get("provider") else "")
        + (t("lib.valuation") if m.extra.get("valuation") else ""),
        t("lib.adjust"): t(ADJUST.get(m.adjust, "adj.none")), t("lib.start"): m.start, t("lib.end"): m.end,
        t("lib.rows"): m.rows, t("lib.updated"): m.created} for m in metas]))

    by_id = {m.id: m for m in metas}
    exp = ui.expansion(t("lib.preview_fold"), icon="show_chart").props("dense").classes("w-full sq-adv") \
        .mark("data_lib_preview") if fold else None
    with (exp if exp is not None else contextlib.nullcontext()):
        sel = ui.select({i: dataset_label(m, lg) for i, m in by_id.items()}, label=t("lib.preview"),
                        value=next(iter(by_id))).props("outlined dense").classes("w-full")
        chart_box = ui.column().classes("w-full gap-2")
        act_box = ui.row().classes("gap-2")

    chart_state = {"id": None, "seq": 0}

    def _build_preview(mid: str, name: str):
        df = library.load(mid)
        factor_cols = [c for c in EXTRA_COLUMNS if c in df.columns]
        return factor_cols, preview_chart(df, name, lg)

    async def draw_chart():
        if chart_box.is_deleted:
            return
        mid = sel.value
        if chart_state["id"] == mid:
            return
        chart_state["seq"] += 1
        seq = chart_state["seq"]
        m = by_id[mid]
        chart_box.clear()
        with chart_box:
            with ui.row().classes("items-center gap-2 py-2"):
                ui.spinner(size="sm")
                ui.label(t("sp.loading_panel")).classes("sq-muted text-sm")
        try:
            factor_cols, fig = await run.io_bound(_build_preview, mid, m.name)
        except Exception as e:  # noqa: BLE001
            if not chart_box.is_deleted and chart_state["seq"] == seq:
                chart_box.clear()
                with chart_box:
                    ui.label(f"{type(e).__name__}: {e}").classes("text-negative text-sm")
            return
        if chart_box.is_deleted or chart_state["seq"] != seq or sel.value != mid:
            return
        chart_state["id"] = mid
        chart_box.clear()
        with chart_box:
            ui.label(t("lib.factors", cols=", ".join(p(INDICATORS[c]["label"]) for c in factor_cols))
                     if factor_cols else t("lib.no_factors")).classes("text-sm sq-muted")
            plot(fig)

    def draw_actions():
        act_box.clear()
        m = by_id[sel.value]
        with act_box:
            ui.button(t("lib.download_csv"), icon="download", on_click=lambda m=m: ui.download.content(
                library.load(m.id).rename_axis("date").reset_index().to_csv(index=False).encode("utf-8-sig"),
                f"{m.symbol}.csv", "text/csv")).props("outline no-caps")
            if can_update(m):
                one = ui.button(t("lib.update_one"), icon="update",
                                on_click=lambda m=m: update_latest(one, prog, refresh, m)) \
                    .props("outline no-caps").tooltip(t("lib.update_tip")).mark("data_lib_update_one")
            ui.button(t("lib.delete"), icon="delete", on_click=lambda m=m: confirm_delete(m, refresh)) \
                .props("outline no-caps color=negative")

    def on_select():
        draw_actions()
        if exp is None or exp.value:
            background_tasks.create(draw_chart(), name="preview dataset chart")

    sel.on_value_change(lambda _: on_select())
    if exp is not None:
        exp.on_value_change(lambda e: background_tasks.create(draw_chart(), name="preview dataset chart") if e.value else None)
    on_select()


async def update_latest(btn, prog, refresh, meta=None):
    """本地数据库更新到最新：meta 为空时更新全部在线下载的数据，结果在对话框里逐份列出"""
    if JOB.running:                     # 模拟盘也在用 BaoStock / 东方财富，错开
        ui.notify(t("lib.update_busy"), type="warning")
        return
    btn.disable()
    prog.start(t("lib.updating"))

    def one():
        try:
            return [(meta, *update_dataset(meta))]
        except Exception as e:  # noqa: BLE001
            return [(meta, "failed", str(e))]
    try:
        if meta is None:
            res = await run.io_bound(update_library, progress=lambda i, n, m: prog.set(i / n, f"{i + 1}/{n}  {m.name}"))
        else:
            res = await run.io_bound(one)
    finally:
        if not btn.is_deleted:
            btn.enable()
            prog.stop()
    if btn.is_deleted:                  # 已离开页面：数据已存好，不再画
        return
    lines = []
    for m, status, new in res:
        if status == "updated":
            lines.append(t("lib.update_done", name=m.name, a=f"{m.start} ~ {m.end}", b=f"{new.start} ~ {new.end}"))
        elif status == "latest":
            lines.append(t("lib.update_latest", name=m.name, end=m.end))
        elif status == "mismatch":
            lines.append(t("lib.update_mismatch", name=m.name, end=m.end))
        else:
            lines.append(t("lib.update_failed", name=m.name, e=new))
    with ui.dialog() as dlg, ui.card().classes("max-w-xl"):
        ui.label(t("lib.update_one" if meta else "lib.update_all")).classes("sq-h2")
        for line in lines:
            ui.label(line).classes("text-sm").style("white-space: pre-line")
        with ui.row().classes("w-full justify-end"):
            ui.button(t("gui.close"), on_click=dlg.close).props("unelevated no-caps").mark("update-ok")
    dlg.on_value_change(lambda e: None if e.value else refresh())
    dlg.open()


async def merge_split(btn, refresh):
    """合并数据库里已有的分段数据，结果（合并了哪些、哪些没合并及原因）在对话框里列出"""
    btn.disable()
    try:
        merged, problems = await run.io_bound(merge_library)
    finally:
        btn.enable()
    span = lambda m: f"{m.start} ~ {m.end}"     # noqa: E731
    sep = "、" if lang() == "zh" else ", "
    lines = [t("lib.merge_done", name=out.name, parts=sep.join(span(m) for m in parts), start=out.start, end=out.end)
             for out, parts in merged]
    for x, y, reason in problems:
        a, b = sorted((x, y), key=lambda m: m.start)      # 早的一段写在前面
        lines.append(t(f"lib.merge_{reason}", name=a.name, a=span(a), b=span(b)))
    with ui.dialog() as dlg, ui.card().classes("max-w-xl"):
        ui.label(t("lib.merge")).classes("sq-h2")
        for line in lines or [t("lib.merge_none")]:
            ui.label(line).classes("text-sm")
        with ui.row().classes("w-full justify-end"):
            ui.button(t("gui.close"), on_click=dlg.close).props("unelevated no-caps").mark("merge-ok")
    # 对话框建在数据库区块里，刷新会把它一起清掉：关掉之后再刷新
    dlg.on_value_change(lambda e: None if e.value else refresh())
    dlg.open()


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
