"""选股页「数据」标签页（股票 / 可转债）"""

import datetime as dt
import tempfile
import time
from pathlib import Path

import pandas as pd
from nicegui import run, ui

from simplequant import llm
from simplequant.stocks import UNIVERSES, today
from simplequant.stocks import universe as U
from simplequant.stocks.store import DAILY_LIMIT
from gui.common import t, p, lang
from gui.components import metric_tile
from gui.widgets import hint, notice, Progress
from gui import sel_panel


# ================= 数据 =================
def _data_tab(SP, ctx, reload_all, universe: str | None = None):
    """universe：下载哪个股票池（新版数据中心自己选）；为空时用选股页当前的股票池。ctx 只用 lo / hi（是否已有数据）"""
    universe = universe or SP["universe"]
    if U.kind(universe) == "cb":
        _cb_data_tab(SP, ctx, reload_all)
        return
    st = sel_panel.store()
    if ctx.lo is not None:
        uni = st.load_universe(universe)
        codes = set(uni["code"].unique())

        def _count_dir(sub: str) -> int:
            d = st.root / sub
            return sum(1 for p in d.glob("*.parquet") if p.stem in codes) if d.exists() else 0

        have = _count_dir("daily")
        have_fin = _count_dir("fin")
        have_div = _count_dir("div")
        have_risk = _count_dir("risk")
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-6"):
            metric_tile(t("sp.snapshots"), str(uni["date"].nunique()))
            metric_tile(t("sp.codes"), str(len(codes)), help_text=t("sp.codes_help"))
            metric_tile(t("sp.downloaded"), f"{have}/{len(codes)}")
            metric_tile(t("sp.fin_downloaded"), f"{have_fin}/{len(codes)}", help_text=t("sp.fin_help"))
            metric_tile(t("sp.div_downloaded"), f"{have_div}/{len(codes)}", help_text=t("sp.div_help"))
            metric_tile(t("sp.risk_downloaded"), f"{have_risk}/{len(codes)}", help_text=t("sp.risk_help"))
    else:
        notice(t("sp.no_data"), "info")

    D = SP["dl"]
    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-4"):
            d_start = ui.input(t("data.start"), value=D["start"]).props("outlined dense type=date").classes("w-44") \
                .tooltip(t("sp.dl_start_help"))
            d_end = ui.input(t("data.end"), value=str(dt.date.today())).props("outlined dense type=date") \
                .classes("w-44")
            with_fin = ui.switch(t("sp.with_fin"), value=D["with_fin"]).tooltip(t("sp.fin_help"))
            with_div = ui.switch(t("sp.with_div"), value=D.get("with_div", True)).tooltip(t("sp.div_help"))
            with_risk = ui.switch(t("sp.with_risk"), value=D["with_risk"]).tooltip(t("sp.risk_help"))                 .mark("sp_with_risk")
        risk_est = ui.label().classes("text-sm")

        def estimate():
            """排雷数据要的请求多（每只每季度 2 次），按当前进度估计还要几天，免得以为卡住了"""
            risk_est.set_visibility(bool(with_risk.value) and ctx.lo is not None)
            if not risk_est.visible:
                return
            try:
                year = int(str(d_start.value)[:4]) - 1
            except ValueError:
                return
            codes = sorted(st.load_universe(universe)["code"].unique())
            n = st.risk_requests(codes, year)
            risk_est.text = t("sp.risk_estimate", n=f"{n:,}", days=max(1, -(-n // DAILY_LIMIT))) if n                 else t("sp.risk_estimate_none")
        with_risk.on_value_change(lambda _: estimate())
        d_start.on_value_change(lambda _: estimate())
        estimate()
        hint(t("sp.download_note_short"), t("sp.download_note"), mark="sp_download_hint")
        prog = Progress()
        out = ui.column().classes("w-full")

        async def download():
            D.update(start=d_start.value, with_fin=with_fin.value, with_div=with_div.value, with_risk=with_risk.value)
            s, e = d_start.value, min(d_end.value, today())
            btn.disable()
            out.clear()
            prog.start()
            t0 = time.time()

            fin, div, risk = with_fin.value, with_div.value, with_risk.value

            def work():
                # 进度文字：步骤 · 进度 · 用时（后台线程只写进度条的数值，界面由定时器刷新）
                def step(label, t_start):
                    return lambda i, n: prog.set(i / n, f"{label} · {i}/{n} · {time.time() - t_start:.0f}s")
                prog.set(0, t("sp.step_universe"))
                u = st.update_universe(universe, s, e, progress=step(t("sp.step_universe"), t0))
                prog.set(0, t("sp.step_index"))
                st.update_index(UNIVERSES[universe]["index"], s, e)
                st.update_basics()
                codes = sorted(u["code"].unique())
                errs = st.update(codes, s, e, progress=step(t("sp.step_stocks", n=len(codes)), time.time()))
                if fin:
                    prog.set(0, t("sp.step_fin", n=len(codes)))
                    st.update_industry()
                    errs.update(st.update_fundamentals(codes, int(s[:4]) - 1,
                                                       progress=step(t("sp.step_fin", n=len(codes)), time.time())))
                if div:
                    prog.set(0, t("sp.step_div", n=len(codes)))
                    errs.update(st.update_dividends(codes, int(s[:4]) - 1,
                                                    progress=step(t("sp.step_div", n=len(codes)), time.time())))
                if risk:                    # 最慢，放在最后：额度用完时前面的都已下完
                    prog.set(0, t("sp.step_risk", n=len(codes)))
                    errs.update(st.update_risk(codes, int(s[:4]) - 1,
                                               progress=step(t("sp.step_risk", n=len(codes)), time.time())))
                return codes, errs
            try:
                codes, errs = await run.io_bound(work)
                if out.is_deleted:          # 下载途中离开了页面：数据已保存，这一页不在了，不用再画
                    return
                ui.notify(t("sp.download_done", n=len(codes)), type="positive")
                if errs:
                    with out:
                        notice(t("sp.download_errors", n=len(errs)) + "\n\n" +
                               "\n".join(f"- {c}: {m}" for c, m in list(errs.items())[:10]), "warning", "warning")
                    return
            except Exception as ex:  # noqa: BLE001
                if out.is_deleted:          # 同上：失败原因画不出来
                    return
                with out:
                    notice(t("data.fetch_failed", sym=universe) + f"：{type(ex).__name__}: {ex}", "error", "error")
                return
            finally:
                prog.stop()
                btn.enable()
            reload_all()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("sp_download")

    _pack_section(SP, st, "stocks", reload_all, default_open=(ctx.lo is None))


def _pack_section(SP, st, pack_name: str, reload_all, default_open: bool = False):
    """云端数据包一键下载导入 + 本地 zip 导入（股票 / 可转债共用）"""
    with ui.expansion(t("sp.pack"), icon="folder_zip", value=default_open) \
            .classes("w-full q-card").mark(f"pack_exp_{pack_name}"):
        ui.label(t("sp.cloud_pack_title")).classes("font-medium text-sm")
        ui.label(t("sp.cloud_pack_note")).classes("sq-muted text-sm")
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            code_in = ui.input(t("sp.cloud_pack_code"), value="", placeholder="sq-...") \
                .props("outlined dense").classes("w-64").mark(f"cloud_pack_code_{pack_name}")
            c_btn = ui.button(t("sp.cloud_pack_btn"), icon="cloud_download").props("unelevated no-caps") \
                .mark(f"cloud_pack_btn_{pack_name}")
            ui.link(t("set.cloud_link"), "https://simplequant.org/#cloud", new_tab=True) \
                .classes("text-xs sq-muted").mark(f"link:cloud_{pack_name}")
        c_prog = Progress()
        c_out = ui.column().classes("w-full")

        async def download_cloud():
            code = (code_in.value or "").strip()
            c_btn.disable()
            c_out.clear()
            c_prog.start()

            def on_prog(done, total):
                if total > 0:
                    c_prog.set(done / total,
                               t("sp.cloud_pack_downloading", done=f"{done / 1e6:.1f}", total=f"{total / 1e6:.1f}"))
                else:
                    c_prog.set(0, t("sp.cloud_pack_downloading_unknown", done=f"{done / 1e6:.1f}"))

            def work():
                with tempfile.TemporaryDirectory(prefix="sq_pack_") as td:
                    zpath = Path(td) / f"{pack_name}.zip"
                    _, left = llm.download_cloud_pack(code, pack_name, zpath, on_progress=on_prog, lang=lang())
                    c_prog.set(1.0, t("sp.cloud_pack_importing"))
                    n = st.import_zip(zpath)
                    return n, left

            try:
                n, left = await run.io_bound(work)
                if c_out.is_deleted:
                    return
                msg = (t("sp.cloud_pack_done_left", n=n, left=left) if left is not None
                       else t("sp.cloud_pack_done", n=n))
                ui.notify(msg, type="positive")
            except llm.HostedError as ex:
                if c_out.is_deleted:
                    return
                with c_out:
                    notice(str(ex), "error", "error")
                return
            except Exception as ex:  # noqa: BLE001
                if c_out.is_deleted:
                    return
                with c_out:
                    notice(f"{type(ex).__name__}: {ex}", "error", "error")
                return
            finally:
                c_prog.stop()
                if not c_btn.is_deleted:
                    c_btn.enable()
            reload_all()

        c_btn.on_click(download_cloud)

        ui.separator().classes("q-my-sm")
        with ui.column().classes("gap-2 w-full"):
            async def on_zip(e):
                import io
                try:
                    n = st.import_zip(io.BytesIO(await e.file.read()))
                    ui.notify(t("sp.pack_imported", n=n), type="positive")
                    reload_all()
                except Exception as ex:  # noqa: BLE001
                    ui.notify(f"{type(ex).__name__}: {ex}", type="negative", multi_line=True)
            ui.upload(label=t("sp.pack_import"), auto_upload=True, on_upload=on_zip) \
                .props("accept=.zip flat bordered").classes("w-full")


def _cb_data_tab(SP, ctx, reload_all):
    """可转债全市场：列表、条款、日线、中证转债指数"""
    from simplequant.bonds import CBStore
    st = CBStore()
    if ctx.lo is not None:
        lst, info = st.load_list(), st.load_info()
        have = sum(st.has(c) for c in lst["code"])
        now = pd.Timestamp(dt.date.today())
        trading = sum(1 for c in lst["code"] if st.has(c) and c in info.index
                      and (pd.isna(info.at[c, "delist_date"]) or info.at[c, "delist_date"] > now))
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            metric_tile(t("cb.total"), str(len(lst)), help_text=t("cb.total_help"))
            metric_tile(t("cb.downloaded"), str(have), help_text=t("cb.downloaded_help"))
            metric_tile(t("cb.trading"), str(trading))
            metric_tile(t("cb.as_of"), str(ctx.hi))
    else:
        notice(t("sp.no_data"), "info")

    D = SP["dl"]
    with ui.card().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-4"):
            d_start = ui.input(t("data.start"), value=D["cb_start"]).props("outlined dense type=date") \
                .classes("w-44").tooltip(t("cb.start_help"))
            workers = ui.number(t("sp.workers"), value=min(int(D["workers"]), 3), min=1, max=4, precision=0) \
                .props("outlined dense").classes("w-32").tooltip(t("cb.workers_help"))
        hint(t("cb.download_note_short"), t("cb.download_note"), mark="cb_download_hint")
        prog = Progress()
        out = ui.column().classes("w-full")
        steps = {"list": t("cb.step_list"), "info": t("cb.step_info"), "index": t("cb.step_index"),
                 "daily": t("cb.step_daily")}

        async def download():
            D["cb_start"] = d_start.value
            s, e, w = d_start.value, today(), int(workers.value or 3)
            btn.disable()
            out.clear()
            prog.start()
            t0 = time.time()

            def progress(step, i, n):
                prog.set(i / n if n else 0, f"{steps[step]} · {i}/{n} · {time.time() - t0:.0f}s")
            try:
                errs = await run.io_bound(st.update_all, s, e, w, progress)
                if out.is_deleted:          # 下载途中离开了页面：数据已保存，这一页不在了，不用再画
                    return
                ui.notify(t("cb.download_done"), type="positive")
                if errs:
                    with out:
                        notice(t("sp.download_errors", n=len(errs)) + "\n\n" +
                               "\n".join(f"- {c}: {m}" for c, m in list(errs.items())[:10]), "warning", "warning")
                    return
            except Exception as ex:  # noqa: BLE001
                if out.is_deleted:          # 同上：失败原因画不出来
                    return
                with out:
                    notice(t("data.fetch_failed", sym=p(UNIVERSES["cb"]["label"])) + f"：{type(ex).__name__}: {ex}",
                           "error", "error")
                return
            finally:
                prog.stop()
                btn.enable()
            reload_all()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("cb_download")

    _pack_section(SP, st, "cb", reload_all, default_open=(ctx.lo is None))

    with ui.expansion(t("cb.rules"), icon="rule").classes("w-full q-card"):
        ui.markdown(t("cb.rules_text")).classes("text-sm")
