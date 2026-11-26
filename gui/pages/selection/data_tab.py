"""选股页「数据」标签页（股票 / 可转债）"""

import datetime as dt
import os
import tempfile
import time
from pathlib import Path

import pandas as pd
from nicegui import background_tasks, run, ui

from simplequant import llm
from simplequant.stocks import UNIVERSES, today
from simplequant.stocks import universe as U
from simplequant.stocks.store import DAILY_LIMIT
from gui.common import t, p, lang
from gui.components import metric_tile
from gui.widgets import hint, notice, Progress
from gui import sel_panel


# 按目录 / 元数据 mtime 缓存统计结果：切回数据页或切换股票池时无需重复扫盘，目录内文件增删改时自动失效
_STOCK_STATS_CACHE: dict[tuple, tuple] = {}
_CB_STATS_CACHE: dict[tuple, tuple] = {}
_RISK_EST_CACHE: dict[tuple, int] = {}


def clear_stats_cache():
    _STOCK_STATS_CACHE.clear()
    _CB_STATS_CACHE.clear()
    _RISK_EST_CACHE.clear()


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _stock_stats(st, universe: str) -> tuple[int, int, int, int, int, int]:
    sig = (str(st.root), universe,
           _mtime(st.root / "universe" / f"{universe}.parquet"),
           _mtime(st.root / "daily"), _mtime(st.root / "fin"),
           _mtime(st.root / "div"), _mtime(st.root / "risk"))
    hit = _STOCK_STATS_CACHE.get(sig)
    if hit is not None:
        return hit
    uni = st.load_universe(universe)
    codes = set(uni["code"].unique())

    def _count_dir(sub: str) -> int:
        d = st.root / sub
        if not d.exists():
            return 0
        return sum(1 for name in os.listdir(d) if name.endswith(".parquet") and name[:-8] in codes)

    res = (int(uni["date"].nunique()), len(codes),
           _count_dir("daily"), _count_dir("fin"), _count_dir("div"), _count_dir("risk"))
    _STOCK_STATS_CACHE[sig] = res
    return res


def _cb_stats(st) -> tuple[int, int, int]:
    today_d = dt.date.today()
    sig = (str(st.root), today_d.isoformat(),
           _mtime(st.root / "list.parquet"), _mtime(st.root / "info.parquet"), _mtime(st.root / "daily"))
    hit = _CB_STATS_CACHE.get(sig)
    if hit is not None:
        return hit
    lst, info = st.load_list(), st.load_info()
    daily_dir = st.root / "daily"
    existing = {n[:-8] for n in os.listdir(daily_dir) if n.endswith(".parquet")} if daily_dir.exists() else set()
    have_codes = [c for c in lst["code"] if c in existing]
    now = pd.Timestamp(today_d)
    sub = info.loc[info.index.intersection(have_codes), "delist_date"]
    trading = int((sub.isna() | (sub > now)).sum())
    res = (len(lst), len(have_codes), trading)
    _CB_STATS_CACHE[sig] = res
    return res


def _risk_requests_cached(st, universe: str, year: int) -> int:
    sig = (str(st.root), universe, year, dt.date.today().isoformat(),
           _mtime(st.root / "universe" / f"{universe}.parquet"),
           _mtime(st.root / "risk_manifest.json"), _mtime(st.root / "risk"))
    hit = _RISK_EST_CACHE.get(sig)
    if hit is not None:
        return hit
    codes = sorted(st.load_universe(universe)["code"].unique())
    n = st.risk_requests(codes, year)
    _RISK_EST_CACHE[sig] = n
    return n


# ================= 数据 =================
def _data_tab(SP, ctx, reload_all, universe: str | None = None):
    """universe：下载哪个股票池（新版数据中心自己选）；为空时用选股页当前的股票池。ctx 只用 lo / hi（是否已有数据）"""
    universe = universe or SP["universe"]
    if U.kind(universe) == "cb":
        _cb_data_tab(SP, ctx, reload_all)
        return
    st = sel_panel.store()
    def _reload():
        clear_stats_cache()
        reload_all()
    if ctx.lo is not None:
        n_snap, n_codes, have, have_fin, have_div, have_risk = _stock_stats(st, universe)
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-6"):
            metric_tile(t("sp.snapshots"), str(n_snap))
            metric_tile(t("sp.codes"), str(n_codes), help_text=t("sp.codes_help"))
            metric_tile(t("sp.downloaded"), f"{have}/{n_codes}")
            metric_tile(t("sp.fin_downloaded"), f"{have_fin}/{n_codes}", help_text=t("sp.fin_help"))
            metric_tile(t("sp.div_downloaded"), f"{have_div}/{n_codes}", help_text=t("sp.div_help"))
            metric_tile(t("sp.risk_downloaded"), f"{have_risk}/{n_codes}", help_text=t("sp.risk_help"))
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

        est_seq = {"n": 0}

        def estimate():
            """排雷数据要的请求多（每只每季度 2 次），后台估算还要几天，避免同步读数百份 Parquet 卡住界面"""
            risk_est.set_visibility(bool(with_risk.value) and ctx.lo is not None)
            if not risk_est.visible:
                return
            try:
                year = int(str(d_start.value)[:4]) - 1
            except ValueError:
                return
            est_seq["n"] += 1
            seq = est_seq["n"]

            async def _calc():
                try:
                    n = await run.io_bound(_risk_requests_cached, st, universe, year)
                except Exception:  # noqa: BLE001
                    return
                if risk_est.is_deleted or est_seq["n"] != seq:
                    return
                risk_est.text = t("sp.risk_estimate", n=f"{n:,}", days=max(1, -(-n // DAILY_LIMIT))) if n \
                    else t("sp.risk_estimate_none")

            background_tasks.create(_calc(), name="estimate risk requests")
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
            _reload()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("sp_download")

    _pack_section(SP, st, "stocks", _reload, default_open=(ctx.lo is None))


def _pack_section(SP, st, pack_name: str, reload_all, default_open: bool = False):
    """云端数据包一键下载导入 + 本地 zip 导入（股票 / 可转债共用）"""
    from gui import cloud_ui, state

    cfg = llm.load_config()
    saved_code = ""
    if cfg:
        saved_code = (cfg.preset_keys or {}).get("hosted", "") or (cfg.api_key if cfg.preset == "hosted" else "")
    local_rec = llm.get_imported_cloud_pack(pack_name)
    force_open = bool(state.STATE.pop("open_pack", False))
    exp_open = default_open or bool(saved_code) or force_open

    with ui.expansion(t("sp.pack"), icon="folder_zip", value=exp_open) \
            .classes("w-full q-card").mark(f"pack_exp_{pack_name}"):
        ui.label(t("sp.cloud_pack_title")).classes("font-medium text-sm")
        ui.label(t("sp.cloud_pack_note")).classes("sq-muted text-sm")
        if local_rec and (local_rec.get("uploaded") or local_rec.get("imported_at")):
            ver_date = str(local_rec.get("uploaded") or local_rec.get("imported_at"))[:10]
            ui.label(t("sp.cloud_pack_local_ver", date=ver_date)).classes("sq-muted text-xs")
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            code_in = ui.input(t("sp.cloud_pack_code"), value=saved_code, placeholder="sq-...") \
                .props("outlined dense").classes("w-64").mark(f"cloud_pack_code_{pack_name}")
            c_btn = ui.button(t("sp.cloud_pack_btn"), icon="cloud_download").props("unelevated no-caps") \
                .mark(f"cloud_pack_btn_{pack_name}")
            ui.link(t("set.cloud_link"), "https://simplequant.org/#cloud", new_tab=True) \
                .classes("text-xs sq-muted").mark(f"link:cloud_{pack_name}")
        c_prog = Progress()
        c_out = ui.column().classes("w-full")

        async def download_cloud(force: bool = False):
            code = (code_in.value or "").strip()
            c_out.clear()
            if not code:
                msg = t("sp.cloud_pack_need_code")
                ui.notify(msg, type="warning")
                with c_out:
                    notice(msg, "warning", "warning")
                code_in.run_method("focus")
                return
            c_btn.disable()
            c_prog.start()

            def on_prog(done, total):
                if total > 0:
                    c_prog.set(done / total,
                               t("sp.cloud_pack_downloading", done=f"{done / 1e6:.1f}", total=f"{total / 1e6:.1f}"))
                else:
                    c_prog.set(0, t("sp.cloud_pack_downloading_unknown", done=f"{done / 1e6:.1f}"))

            # 先核对云端版本：若本机已导入同一版本且非强制重下，直接提示无需重复下载
            remote_info: dict = {}
            try:
                packs = await run.io_bound(llm.list_cloud_packs, 8.0, lang())
                if isinstance(packs, dict) and isinstance(packs.get(pack_name), dict):
                    remote_info = packs[pack_name]
            except Exception:  # noqa: BLE001 - 版本查询失败不阻断实际下载
                remote_info = {}

            local_now = llm.get_imported_cloud_pack(pack_name) or {}
            local_ver = (local_now.get("version") or "").strip()
            remote_ver = (remote_info.get("version") or "").strip()
            remote_uploaded = (remote_info.get("uploaded") or "").strip()

            if not force and local_ver and remote_ver and local_ver == remote_ver:
                c_prog.stop()
                if not c_btn.is_deleted:
                    c_btn.enable()
                if not c_out.is_deleted:
                    date_str = remote_uploaded[:10] if remote_uploaded else ""
                    info_txt = (t("sp.cloud_pack_uptodate", date=date_str) if date_str
                                else t("sp.cloud_pack_uptodate_nodate"))
                    with c_out:
                        notice(info_txt, "info", "info")
                        ui.button(t("sp.cloud_pack_redl_btn"), icon="refresh",
                                  on_click=lambda: download_cloud(force=True)) \
                            .props("outline dense no-caps").classes("mt-1 self-start") \
                            .mark(f"cloud_pack_redl_{pack_name}")
                return

            meta: dict = {}

            def work():
                with tempfile.TemporaryDirectory(prefix="sq_pack_") as td:
                    zpath = Path(td) / f"{pack_name}.zip"
                    _, left = llm.download_cloud_pack(
                        code, pack_name, zpath, on_progress=on_prog, lang=lang(), meta_out=meta
                    )
                    c_prog.set(1.0, t("sp.cloud_pack_importing"))
                    n = st.import_zip(zpath)
                    ver = meta.get("version") or remote_ver
                    if ver or remote_uploaded:
                        llm.record_imported_cloud_pack(pack_name, version=ver, uploaded=remote_uploaded)
                    return n, left

            try:
                n, left = await run.io_bound(work)
                cloud_ui.invalidate_quota()
                if c_out.is_deleted:
                    return
                deducted = meta.get("deducted", True)
                if left is not None and not deducted:
                    msg = t("sp.cloud_pack_done_redl", n=n, left=left)
                elif left is not None:
                    msg = t("sp.cloud_pack_done_left", n=n, left=left)
                else:
                    msg = t("sp.cloud_pack_done", n=n)
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

        c_btn.on_click(lambda: download_cloud(force=False))

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
    def _reload():
        clear_stats_cache()
        reload_all()
    if ctx.lo is not None:
        total, have, trading = _cb_stats(st)
        with ui.grid().classes("w-full gap-3 grid-cols-2 md:grid-cols-4"):
            metric_tile(t("cb.total"), str(total), help_text=t("cb.total_help"))
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
            _reload()

        btn = ui.button(t("sp.download"), icon="download", on_click=download).props("unelevated no-caps") \
            .classes("self-start").mark("cb_download")

    _pack_section(SP, st, "cb", _reload, default_open=(ctx.lo is None))

    with ui.expansion(t("cb.rules"), icon="rule").classes("w-full q-card") as rules_exp:
        rules_box = ui.column().classes("w-full")
        rules_drawn = {"ok": False}

        def _draw_rules(e):
            if e.value and not rules_drawn["ok"] and not rules_box.is_deleted:
                rules_drawn["ok"] = True
                with rules_box:
                    ui.markdown(t("cb.rules_text")).classes("text-sm")
        rules_exp.on_value_change(_draw_rules)
