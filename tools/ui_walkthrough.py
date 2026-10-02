"""
打包版的界面自动测试（由 gui/uitest.py 在真正的桌面窗口里运行；tools/smoke_test.py --ui 调用）
按 docs/mac-test.md 的清单走一遍：首页、语言与主题、下载数据、策略 → 回测、原生图、设置页检查更新、
定时任务创建与删除、模拟账户。每步截图。依赖网络的步骤失败只记 WARN（GitHub 的机器在国外，国内数据源未必连得上），
此时用随机生成的行情继续后面的步骤。
"""

import asyncio
import datetime as dt
import subprocess
import sys

import numpy as np
import pandas as pd

from gui import state
from gui.common import set_dark, set_lang
from simplequant.data import library
from simplequant.paper import list_accounts, schedule


def fake_prices(n=600) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=dt.date.today() - dt.timedelta(days=1), periods=n)
    close = 4 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    return pd.DataFrame({"open": close * (1 + rng.normal(0, 0.002, n)), "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": rng.integers(1e6, 5e6, n)}, index=idx)


async def main(d):
    async def home():
        d.one(marker="logo")
        await d.see("开始新的工作")
        await d.shot("home")

    async def lang_theme():
        set_lang("en")
        await d.reload()
        await d.see("Paper trading", timeout=15)
        await d.shot("home-en")
        set_lang("zh")
        set_dark(True)
        await d.reload()
        await d.shot("home-dark")
        set_dark(None)
        await d.reload()

    async def download():
        await d.goto("/data")
        await d.click(content="下载至本地数据库")
        await d.until(lambda: library.list_datasets(), timeout=240, what="510300 downloaded")
        await d.shot("data-downloaded")
        return f"{len(library.list_datasets())} dataset(s)"

    await d.step("首页", home)
    await d.step("语言与深色主题", lang_theme)
    downloaded = await d.step("下载 510300（AKShare，需联网）", download, required=False)
    if not downloaded:
        await d.step("改用模拟行情", _fake)

    async def strategy():
        s = state.strategy()
        s["mode"], s["tpl_key"] = "template", "sma_cross"
        await d.goto("/strategy")
        await d.shot("strategy")
        old = {c.id for c in type(d.client).instances.values()}
        await d.click(content="使用此策略回测")
        await d.wait_client("/backtest", exclude=old)

    async def backtest():
        d.one(marker="bt_strategy").set_value("tpl:sma_cross")
        await asyncio.sleep(1)
        await d.click(marker="run")
        await d.until(lambda: state.STATE.get("bt", {}).get("result") is not None, timeout=120, what="backtest")
        await d.see("总收益率")
        await d.shot("backtest")
        res = state.STATE["bt"]["result"]
        return f"{len(res.orders)} orders"

    async def native_chart():
        await d.click(marker="native_tab")          # 「绘制」按钮在这个标签页里，没打开时不在窗口中
        await asyncio.sleep(1)
        await d.click(marker="native")
        await d.until(lambda: state.STATE["bt"].get("native_png"), timeout=120, what="native chart")
        d._js('window.scrollTo(0, document.body.scrollHeight)')
        await d.shot("native-chart")

    await d.step("策略页 → 回测", strategy)
    if await d.step("回测", backtest):
        await d.step("Backtrader 原生图（检查中文）", native_chart)

    async def settings():
        from simplequant.update.client import UPDATER
        await d.goto("/settings")
        await d.see("版本")
        await d.click(marker="update_check")
        await d.until(lambda: UPDATER.status.state not in ("idle", "checking"), timeout=60, what="update check")
        await d.shot("settings")
        return UPDATER.status.state

    await d.step("设置页：版本与检查更新", settings, required=False)

    async def task():
        if schedule.task_exists():
            # 这台电脑上已有真实的模拟盘定时任务（任务名是固定的）：不能创建再删除，否则会把它删掉
            raise RuntimeError("已有 SimpleQuant 定时任务，跳过以免覆盖")
        await d.goto("/paper")
        d.one(marker="pp_schedule").open()
        await asyncio.sleep(1)
        await d.click(marker="pp_task_create")
        await d.until(schedule.task_exists, timeout=30, what="task created")
        await d.see("已设置每日自动运行")
        await d.shot("task-created")
        note = schedule.backend()
        if sys.platform == "darwin":
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
            note += ", launchctl list: " + ("found" if schedule.LAUNCHD_LABEL in out else "NOT found")
        await d.click(marker="pp_task_delete")
        await d.until(lambda: not schedule.task_exists(), timeout=30, what="task deleted")
        await d.see("尚未设置自动运行")
        return note

    await d.step("模拟盘定时任务：创建与删除", task, required=not schedule.task_exists())

    async def account():
        ds = library.list_datasets()[0]
        await d.goto("/paper")
        d.one(marker="pp_strategy").set_value("tpl:sma_cross")
        await asyncio.sleep(1)
        d.one(marker="pp_assets").set_value([ds.id])
        await asyncio.sleep(1)
        d.one(marker="pp_mode").set_value("past")
        await asyncio.sleep(1)
        d.one(marker="pp_past").set_value(str(dt.date.today() - dt.timedelta(days=120)))
        d.one(marker="pp_name").set_value("自动测试账户")
        await d.click(marker="pp_create")
        await d.until(lambda: list_accounts() and list_accounts()[0].last_run, timeout=300,
                      what="paper account run")
        await d.see("总资产", timeout=60)
        await d.shot("paper-account")

    if downloaded:          # 模拟账户每天会重新下载行情；没联网时跳过
        await d.step("新建模拟账户（需联网）", account, required=False)


async def _fake():
    library.save(fake_prices(), library.DatasetMeta(id="fake_510300", name="沪深300ETF（模拟行情）", symbol="510300",
                                                    source="akshare", freq="1d"))
    return "saved fake prices"
