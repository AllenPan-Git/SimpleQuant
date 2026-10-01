"""
交易日历（BaoStock，含已公布的未来交易日，如国庆休市）
模拟盘用它判断"今天收盘后生成的信号在哪天执行"、"今天是不是本月最后一个交易日"。
"""

import datetime as dt
from pathlib import Path

import pandas as pd

from ..stocks.store import ROOT as STOCK_ROOT, _login, _query

CAL_PATH = STOCK_ROOT.parent / "trade_calendar.parquet"


def refresh_calendar(path: Path = CAL_PATH) -> pd.DatetimeIndex:
    import baostock as bs
    _login()
    end = dt.date(dt.date.today().year + 1, 12, 31)
    df = _query(bs.query_trade_dates, start_date="2015-01-01", end_date=end.isoformat())
    days = pd.to_datetime(df.loc[df["is_trading_day"] == "1", "calendar_date"])
    pd.DataFrame({"date": days}).to_parquet(path)
    return pd.DatetimeIndex(days)


def load_calendar(path: Path = CAL_PATH, refresh_if_stale: bool = True) -> pd.DatetimeIndex:
    """本地缓存超过 7 天或覆盖不到未来 20 天时，重新获取"""
    if path.exists():
        cal = pd.DatetimeIndex(pd.read_parquet(path)["date"])
        fresh = (dt.datetime.now().timestamp() - path.stat().st_mtime) < 7 * 86400
        covers = len(cal) and cal[-1] >= pd.Timestamp(dt.date.today() + dt.timedelta(days=20))
        if fresh and covers or not refresh_if_stale:
            return cal
    try:
        return refresh_calendar(path)
    except Exception:  # noqa: BLE001 - 取不到时退回缓存或工作日近似
        if path.exists():
            return pd.DatetimeIndex(pd.read_parquet(path)["date"])
        return pd.bdate_range("2015-01-01", dt.date.today() + dt.timedelta(days=60))


def next_trading_days(calendar: pd.DatetimeIndex, after, n: int = 40) -> pd.DatetimeIndex:
    after = pd.Timestamp(after)
    future = calendar[calendar > after][:n]
    if len(future) < n:   # 日历还没公布到那么远：用工作日近似补齐
        last = future[-1] if len(future) else after
        future = future.append(pd.bdate_range(last + pd.Timedelta(days=1), periods=n - len(future)))
    return future


# 当天日线在收盘后几小时发布；这个时间之前以上一个交易日为准。
# 实测（2026-09-29）：18:40 时 BaoStock 当天日线、估值、换手率、指数均已发布；更早的发布时刻未确认
PUBLISH_HOUR = 18


def latest_expected_day(calendar: pd.DatetimeIndex, now: dt.datetime | None = None) -> pd.Timestamp:
    """此刻"应该已经有数据"的最新交易日。数据已到这天时就不必再下载（避免周末、盘中反复重查）"""
    now = now or dt.datetime.now()
    cutoff = pd.Timestamp(now.date()) - (pd.Timedelta(0) if now.hour >= PUBLISH_HOUR else pd.Timedelta(days=1))
    past = calendar[calendar <= cutoff]
    return past[-1] if len(past) else cutoff


def next_trading_day(calendar: pd.DatetimeIndex, after) -> pd.Timestamp:
    return next_trading_days(calendar, after, 1)[0]
