"""
模拟盘更新任务：放在页面之外（进程里只有一个），换页、刷新后仍能看到进度。

以前更新跟着模拟盘页面走：进度只在发起的那个页面里，离开再回来就看不到了，按钮也能再点一次。
现在任务本身在后台跑（background_tasks），页面只是看它：
- 每个页面的 frame() 里有一个 watch()：更新结束时在当前页面弹「完成：成功 n 个……」；
- 新版侧栏「模拟盘」旁换成百分比和一条细进度线（side_indicator）；
- 模拟盘页的 Panel 显示进度条、当前账户和步骤、运行日志，结束时重画页面。
后台线程只写数值，界面由各页面自己的定时器读（NiceGUI 元素不宜跨线程直接改，与 widgets.Progress 相同）。
"""

import time

from nicegui import background_tasks, run, ui

from gui.common import t


class Job:
    def __init__(self):
        self.running = False
        self.serial = 0                 # 第几次更新：页面据此认出「我看着开始的那次」已经结束
        self.total = 0                  # 账户数（只算启用中的）
        self.done = 0.0                 # 已完成的账户数，含当前账户已完成的比例
        self.name, self.step, self.i, self.n = "", "", 0, 0
        self.started = self.ended = 0.0
        self.lines: list[str] = []
        self.results: dict | None = None
        self.error = ""

    # ---------- 后台线程里调用 ----------
    def log(self, line):
        self.lines.append(str(line))

    def progress(self, done, total, name, step, i, n):
        if step != "wait":
            self.done, self.total = done, total
        self.name, self.step, self.i, self.n = name, step, i, n

    # ---------- 界面里调用 ----------
    def start(self, run_all, accounts, refresh: bool = True) -> bool:
        """开始更新；已经在更新时什么都不做，返回 False。run_all 由页面传入（测试里会替换页面模块的 run_all）"""
        if self.running:
            return False
        self.running, self.serial = True, self.serial + 1
        self.total, self.done = sum(a.status == "active" for a in accounts), 0.0
        self.name, self.step, self.i, self.n = "", "", 0, 0
        self.started, self.ended = time.time(), 0.0
        self.lines, self.results, self.error = [], None, ""

        async def go():
            try:
                self.results = await run.io_bound(run_all, accounts, refresh=refresh, log=self.log,
                                                  progress=self.progress)
            except Exception as e:  # noqa: BLE001 - 一般不会到这里（run_all 自己接住每个账户的错误）
                self.results, self.error = {}, f"{type(e).__name__}: {e}"
            finally:
                self.ended, self.running = time.time(), False
        background_tasks.create(go(), name="paper update")
        return True

    def fraction(self) -> float:
        return max(0.0, min(1.0, self.done / self.total)) if self.total else 0.0

    def elapsed(self) -> str:
        s = int((self.ended or time.time()) - self.started)
        return f"{s // 60}:{s % 60:02d}"

    def step_text(self) -> str:
        """「下载日线 · 120 / 332」；还没开始报进度时为「准备中…」"""
        if self.step == "wait":
            return t("pp.prog_wait")
        if not self.step:
            return t("pp.prog_start")
        text = t(f"pp.step_{self.step}")
        return f"{text} · {self.i} / {self.n}" if self.n else text

    def account_text(self) -> str:
        if not self.name or not self.total:
            return ""
        return t("pp.prog_acct", k=min(int(self.done) + 1, self.total), n=self.total, name=self.name)

    def summary(self) -> str:
        if self.error:
            return t("pp.prog_error", err=self.error)
        res = self.results or {}
        failed = sum(1 for v in res.values() if v)
        return t("pp.run_done", ok=len(res) - failed, failed=failed)


JOB = Job()


def while_idle(btn):
    """「更新」按钮：更新中变灰，做完恢复（立即按当前状态设一次，之后由定时器跟着变）。返回 btn。
    不用 bind_enabled_from：对普通对象的绑定靠 NiceGUI 的轮询，界面测试环境里没有它"""
    def tick():
        btn.enabled = not JOB.running
    tick()
    with btn:
        ui.timer(0.3, tick)
    return btn


def watch(on_finish, interval: float = 0.5):
    """当前页面看着更新：页面打开时正在进行的、或之后开始的那次结束时调用 on_finish(job)（每次只调一次）。
    按序号判断，不靠「看到过它在跑」：很快做完的更新可能在两次检查之间就开始又结束了。返回定时器"""
    handled = {"serial": JOB.serial - 1 if JOB.running else JOB.serial}    # 打开页面前就结束的不算

    def tick():
        if not JOB.running and JOB.serial > handled["serial"]:
            handled["serial"] = JOB.serial
            on_finish(JOB)
    return ui.timer(interval, tick)


def notify_done(job: Job):
    """每个页面都有：更新结束时弹一句结果"""
    res = job.results or {}
    ui.notify(job.summary(), type="negative" if job.error or any(res.values()) else "positive")


def side_indicator(hide=None):
    """新版侧栏「模拟盘」一项里：更新中显示百分比（图标列下是小红点）和底部一条细进度线，
    同时藏起 hide（待执行交易笔数）；不在更新时反过来"""
    pct = ui.label().classes("sq-side-badge sq-side-pct").mark("nav_progress")
    with ui.element("div").classes("sq-side-prog") as track:
        fill = ui.element("i")

    def tick():
        on = JOB.running
        pct.set_visibility(on)
        track.set_visibility(on)
        if hide is not None:
            hide.set_visibility(not on)
        if on:
            v = round(JOB.fraction() * 100)
            pct.text = f"{v}%"
            fill.style(f"width: {v}%")
    tick()
    ui.timer(0.5, tick)
    return pct


class Panel:
    """模拟盘页：进度条、当前账户和步骤、运行日志。更新中或这一页看着它结束后显示；on_finish 在结束时调用"""

    def __init__(self, on_finish=None):
        self._on_finish = on_finish
        self._serial, self._shown = None, 0
        with ui.column().classes("sq-prog w-full gap-1").mark("pp_progress") as self.box:
            with ui.row().classes("w-full items-baseline justify-between no-wrap gap-4"):
                self.head = ui.label().classes("sq-prog-head").mark("pp_progress_head")
                with ui.row().classes("items-baseline gap-3 no-wrap"):
                    self.time = ui.label().classes("sq-muted text-sm sq-num")
                    self.pct = ui.label().classes("sq-prog-pct sq-fig").mark("pp_progress_pct")
            self.bar = ui.linear_progress(value=0, show_value=False, color=None).props("size=2px track-color=transparent") \
                .classes("sq-prog-bar")
            self.step = ui.label().classes("sq-muted text-sm sq-num").mark("pp_progress_step")
            with ui.expansion(t("pp.prog_log")).classes("sq-adv w-full").mark("pp_progress_log"):
                self.logs = ui.column().classes("w-full gap-0 sq-code max-h-72 overflow-auto")
        self.box.set_visibility(JOB.running)
        if JOB.running:
            self._tick()
        ui.timer(0.3, self._tick)
        watch(self._finished, interval=0.3)

    def _draw_logs(self):
        new = JOB.lines[self._shown:]
        self._shown += len(new)
        with self.logs:
            for line in new:
                ui.label(line).classes("text-xs whitespace-pre-wrap")

    def _tick(self):
        if not JOB.running:
            return
        if JOB.serial != self._serial:          # 这一页上开始了新的一次：清掉上次的日志
            self._serial, self._shown = JOB.serial, 0
            self.logs.clear()
            self.bar.set_visibility(True)
            self.step.set_visibility(True)
        self.box.set_visibility(True)
        acct = JOB.account_text()
        self.head.text = t("pp.prog_head") + (f"　{acct}" if acct else "")
        self.pct.text = f"{round(JOB.fraction() * 100)}%"
        self.time.text = t("pp.prog_elapsed", t=JOB.elapsed())
        self.bar.value = JOB.fraction()
        self.step.text = JOB.step_text()
        self._draw_logs()

    def _finished(self, job: Job):
        if job.serial != self._serial:          # 很快就做完了，_tick 没赶上
            self._serial, self._shown = job.serial, 0
            self.logs.clear()
        self.box.set_visibility(True)
        self._draw_logs()
        self.head.text = job.summary()
        self.pct.text = ""
        self.time.text = t("pp.prog_elapsed", t=job.elapsed())
        self.bar.set_visibility(False)
        self.step.set_visibility(False)
        if self._on_finish:
            self._on_finish(job)
