"""
模拟盘异常检查（首页提示用）：有启用中的账户时，检查
- 没有定时任务：模拟盘不会每天自动运行
- 上次运行失败（error.json）
- 落后超过 1 个交易日：定时任务没跑（电脑没开、任务被删）或数据源一直没更新
落后正好 1 个交易日不提示：当天数据发布后到定时任务运行前（例如 18:00~19:00）本来就是这样
"""

import datetime as dt

import pandas as pd

from .account import PaperAccount
from .calendar import latest_expected_day

STALE_DAYS = 2      # 落后几个交易日起提示


def lag_days(acc: PaperAccount, calendar: pd.DatetimeIndex, now: dt.datetime | None = None) -> int:
    """数据截至日之后，到此刻应有数据的最新交易日，共有几个交易日没处理；从未运行过时返回 0"""
    if not acc.data_through:
        return 0
    expected = latest_expected_day(calendar, now)
    through = pd.Timestamp(acc.data_through)
    return int(((calendar > through) & (calendar <= expected)).sum())


def issues(accounts: list[PaperAccount], calendar: pd.DatetimeIndex, task_on: bool,
           now: dt.datetime | None = None) -> list[tuple[str, dict]]:
    """返回 [(提示类型, 参数)]，类型为 no_task / failed / stale；暂停的账户不检查"""
    active = [a for a in accounts if a.status == "active"]
    if not active:
        return []
    out = [] if task_on else [("no_task", {})]
    for a in active:
        err = a.error()
        if err:
            out.append(("failed", {"name": a.name, "time": err.get("time", ""), "message": err.get("message", "")}))
            continue
        n = lag_days(a, calendar, now)
        if n >= STALE_DAYS:
            out.append(("stale", {"name": a.name, "through": a.data_through, "n": n}))
    return out
