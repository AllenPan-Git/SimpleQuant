"""
BaoStock 数据源：免注册，股票日线 + 5/15/30/60 分钟线（2000 年起）
注意：ETF 的分钟线只有近期数据。
"""

import pandas as pd

from ..i18n import L
from .base import DataSource, normalize, with_retry, exchange_prefix, clip_dates

_FREQ_MAP = {"1d": "d", "60m": "60", "30m": "30", "15m": "15", "5m": "5"}
_ADJUST_MAP = {"hfq": "1", "qfq": "2", "": "3"}


class BaoStockSource(DataSource):
    key = "baostock"
    label = L("BaoStock（免费，日线/5~60分钟）", "BaoStock (free, daily / 5–60 min)")
    freqs = ("1d", "60m", "30m", "15m", "5m")
    description = L("股票分钟线历史较长（约 2000 年起）；ETF 分钟线只有近期。不提供 1 分钟。股票日线自带换手率、PE、PB、PS 因子。",
                    "Long intraday history for stocks (from ~2000); ETF intraday data is recent only. No 1-minute bars. "
                    "Stock daily bars include turnover, P/E, P/B and P/S.")

    def fetch(self, symbol, start, end, freq="1d", adjust="hfq", **kwargs):
        import baostock as bs

        if freq not in _FREQ_MAP:
            raise ValueError(f"BaoStock does not support / 不支持频率 {freq}")
        code = f"{exchange_prefix(symbol)}.{symbol}"
        minute = freq != "1d"
        basic = "date,time,open,high,low,close,volume" if minute else "date,open,high,low,close,volume"
        # 股票日线可带换手率与估值；ETF 请求这些字段会返回空表，此时退回基础字段
        with_factors = None if minute else basic + ",turn,peTTM,pbMRQ,psTTM"

        def _query(fields):
            lg = bs.login()
            if lg.error_code != "0":
                raise ConnectionError(f"BaoStock login failed / 登录失败: {lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(code, fields, start_date=start, end_date=end,
                                                  frequency=_FREQ_MAP[freq],
                                                  adjustflag=_ADJUST_MAP.get(adjust, "3"))
                if rs.error_code != "0":
                    raise ConnectionError(f"BaoStock query failed / 查询失败: {rs.error_msg}")
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                return pd.DataFrame(rows, columns=rs.fields)
            finally:
                bs.logout()

        raw = with_retry(lambda: _query(with_factors)) if with_factors else pd.DataFrame()
        if raw.empty:
            raw = with_retry(lambda: _query(basic))
        if raw.empty:
            raise ValueError(f"No {freq} data for {code} in {start} ~ {end} / 该区间内无数据")

        if minute:
            # time 字段形如 20200102093500000（bar 结束时间）
            raw["datetime"] = pd.to_datetime(raw["time"].str[:14], format="%Y%m%d%H%M%S")
            raw = raw.drop(columns=["date", "time"])
        else:
            raw = raw.rename(columns={"date": "datetime"})
        return clip_dates(normalize(raw), start, end)
