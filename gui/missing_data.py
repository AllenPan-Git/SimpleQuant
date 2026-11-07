"""缺数据时就地提示与下载（界面重构计划 4.4.2）

工作台、实验室发现缺某类数据时，不再让用户去数据页，而是在原地显示「缺少沪深300的财务数据 [下载]」，点了就在这里下载并显示进度。

- 下载在后台线程里跑，状态记在模块级的 _JOBS 里（后台只写数值，页面用定时器去读，与 AI 因子挖掘相同）：
  离开页面再回来，新页面接着显示进度；下载期间页面被重画（例如改了设置）也不会重复开始。
- BaoStock 只能单连接：同一时间只允许一项下载，另一项在进行时按钮变灰并说明。
- 下载完成后调用 on_done()（页面还在时），由页面重新载入数据（选股的面板按数据文件的修改时间自动失效）。
"""

import time

from nicegui import background_tasks, run, ui

from gui.common import t, p
from gui.widgets import notice, Progress

_JOBS: dict[str, dict] = {}     # 键 → {"running", "prog": (进度, 文字), "result": None | (结果, 内容), "seen"}


def busy() -> str | None:
    """正在进行的下载的键；没有为 None"""
    return next((k for k, j in _JOBS.items() if j.get("running")), None)


def start(key: str, work) -> bool:
    """在后台开始一项下载：work(progress) 在线程里跑，progress(进度 0～1, 文字)；返回 False = 另一项正在进行"""
    if busy():
        return False
    job = _JOBS[key] = {"running": True, "prog": (0.0, ""), "result": None}

    def progress(v, text=""):
        job["prog"] = (v, text)

    async def go():
        try:
            out = await run.io_bound(work, progress)
            job["result"] = ("ok", out)
        except Exception as e:  # noqa: BLE001 - 失败原因显示在提示条里
            job["result"] = ("error", f"{type(e).__name__}: {e}")
        finally:
            job["running"] = False
    background_tasks.create(go(), name=f"download {key}")
    return True


def download_bar(key: str, text: str, button: str, work, on_done=None, note: str | None = None,
                 mark: str = "md_bar"):
    """缺数据提示条：说明 + 下载按钮；下载中显示进度，结束后显示结果并调用 on_done()（下载成功时）。
    work(progress) → 出错的条目 {名称: 原因}（可为空 / None）；note：按钮下面的小字（例如排雷数据要几天）"""
    with ui.column().classes("w-full gap-2").mark(mark):
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            with ui.element("div").classes("grow min-w-0"):
                notice(text, "warning", "warning")
            btn = ui.button(button, icon="download", on_click=lambda: go()).props("unelevated no-caps") \
                .classes("shrink-0").mark(f"{mark}_go")
        if note:
            ui.label(note).classes("sq-muted text-xs")
        other = ui.label(t("md.busy")).classes("sq-muted text-xs")
        prog = Progress()
        result = ui.column().classes("w-full")
    seen = {"running": False}

    def show_result():
        job = _JOBS.get(key) or {}
        res = job.get("result")
        result.clear()
        if not res:
            return
        with result:
            if res[0] == "error":
                notice(t("md.failed", e=res[1]), "error", "error")
            elif res[1]:
                errs = res[1]
                notice(t("sp.download_errors", n=len(errs)) + "\n\n" +
                       "\n".join(f"- {c}: {m}" for c, m in list(errs.items())[:10]), "warning", "warning")

    def watch():
        if btn.is_deleted:
            return
        job = _JOBS.get(key) or {}
        running_other = bool(busy()) and busy() != key
        if job.get("running"):
            if not seen["running"]:
                seen["running"] = True
                prog.start()
            prog.set(*job["prog"])
            btn.disable()
        else:
            btn.set_enabled(not running_other)
            if seen["running"]:                   # 这一页看着它结束的
                seen["running"] = False
                prog.stop()
                show_result()
                if job.get("result", ("",))[0] == "ok" and on_done:
                    on_done()
        other.set_visibility(running_other)
    ui.timer(0.3, watch)

    def go():
        if not start(key, work):
            ui.notify(t("md.busy"), type="warning")
            return
        result.clear()
        watch()

    if not (_JOBS.get(key) or {}).get("running"):
        show_result()                 # 上次下载的结果（失败原因、出错的股票）在页面重画后仍然看得到
    watch()


# ---------------- 选股数据：财务 / 分红 / 排雷 ----------------
STOCK_PARTS = ("fin", "div", "risk")


def stock_bar(part: str, universe: str, start_year: int, reason: str, on_done=None, store=None):
    """选股数据缺一类（fin 财务 / div 分红 / risk 排雷）：只补下载这一类（不重下日线）。
    reason：为什么需要（文字键的后缀，例如 factor / cash / screen）；store：测试时传假的 StockStore"""
    from simplequant.stocks import UNIVERSES
    from simplequant.stocks.store import DAILY_LIMIT
    st = store or _store()
    u = p(UNIVERSES[universe]["label"])
    note = None
    if part == "risk":           # 排雷数据请求多，按当前进度估计要几天
        try:
            n = st.risk_requests(_codes(st, universe), start_year)
            note = t("sp.risk_estimate", n=f"{n:,}", days=max(1, -(-n // DAILY_LIMIT))) if n else None
        except Exception:  # noqa: BLE001 - 估计不出来就不显示
            note = None

    def work(progress):
        codes = _codes(st, universe)
        label = t(f"sp.step_{part}", n=len(codes))
        t0 = time.time()

        def step(i, n):
            progress(i / max(n, 1), f"{label} · {i}/{n} · {time.time() - t0:.0f}s")
        progress(0.0, label)
        if part == "fin":
            st.update_industry()
            return st.update_fundamentals(codes, start_year, progress=step)
        if part == "div":
            return st.update_dividends(codes, start_year, progress=step)
        return st.update_risk(codes, start_year, progress=step)
    download_bar(f"stock:{universe}:{part}", t(f"md.{part}_{reason}", u=u), t("md.download"), work, on_done,
                 note=note, mark=f"md:{part}")


def _store():
    """下载用的 StockStore（与选股面板同一个目录；测试替换这个函数）"""
    from gui.sel_panel import store
    return store()


def _codes(st, universe: str) -> list[str]:
    return sorted(st.load_universe(universe)["code"].unique())


# ---------------- 择时：行情、利率 ----------------
SAMPLE = ("510300", "akshare", "etf")      # 本地没有行情时的示例：沪深300ETF 日线（后复权）


def market_bar(on_done=None):
    """本地还没有任何行情数据：就地下载 510300 日线（2015 年起，后复权），或去数据中心下载其他标的"""
    import datetime as dt
    from simplequant.data import fetch_to_library
    from ui.shared import POPULAR
    sym, src, asset = SAMPLE

    def work(progress):
        progress(0.2, t("data.fetching", sym=sym))
        fetch_to_library(src, sym, "2015-01-01", str(dt.date.today()), freq="1d", adjust="hfq",
                         name=f"{sym} {p(POPULAR[sym])}", asset=asset)
        return None
    download_bar("market:sample", t("md.market"), t("md.market_go", sym=sym), work, on_done, mark="md:market")
    ui.button(t("nav.go_data"), on_click=lambda: ui.navigate.to("/data")) \
        .props("flat no-caps dense color=primary icon-right=arrow_forward").classes("self-start")


def rates_bar(on_done=None):
    """规则用到利率 / 利差条件，但还没有中债利率数据：就地更新"""
    from simplequant.bonds.rates import RatesStore, FIRST_YEAR

    def work(progress):
        RatesStore().update(FIRST_YEAR, None, lambda i, n: progress(i / max(n, 1), f"{i}/{n}"))
        return None
    download_bar("rates", t("md.rates"), t("rates.update"), work, on_done, mark="md:rates")
