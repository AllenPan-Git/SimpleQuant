"""数据源健康检查：每个外部接口只取一只标的的少量数据，确认还能取到、字段还对得上。

GitHub Actions 每周运行一次（.github/workflows/sources.yml），失败时开 issue，以便比用户更早发现接口变化。
本机运行：.venv\\Scripts\\python -X utf8 tools\\source_check.py [--markdown 结果.md]
走的是程序里实际使用的函数（含解析），因此字段改名也能发现。退出码：没有失败项为 0，否则为 1
（WARN_ONLY 里的项失败只警告，不影响退出码）。
"""
import argparse
import datetime as dt
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

TODAY = dt.date.today()
START = (TODAY - dt.timedelta(days=30)).isoformat()
END = TODAY.isoformat()


def need(df, columns=(), min_rows=1):
    """检查行数与列；返回一句说明"""
    if df is None or len(df) < min_rows:
        raise AssertionError(f"rows: {0 if df is None else len(df)} < {min_rows}")
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise AssertionError(f"missing columns: {missing}; got {list(df.columns)[:20]}")
    return f"{len(df)} rows"


# ---------------- 东方财富 ----------------
# 日线直接调用各个接口（AKShareSource.fetch 在一个接口失败时会静默改用备用接口，测不出来）
def em_etf():
    from simplequant.data.akshare_src import eastmoney
    from simplequant.data.base import normalize
    return need(normalize(eastmoney("510300", START, END, "hfq", "etf")), ["open", "high", "low", "close", "volume"])


def em_stock():
    from simplequant.data.akshare_src import eastmoney
    from simplequant.data.base import normalize
    return need(normalize(eastmoney("600519", START, END, "hfq", "stock")), ["close", "volume", "turnover"])


def em_rights():
    from simplequant.stocks.dividends import fetch_rights
    return need(fetch_rights(), ["code", "record_date"], min_rows=100)


def em_cb_list():
    from simplequant.bonds.store import fetch_list
    return need(fetch_list(), ["code", "stock_code", "listing_date", "issue_size"], min_rows=100)


def em_cb_value():
    from simplequant.bonds.store import fetch_value, VALUE_FIELDS
    df = fetch_value(CB_CODE, since=(TODAY - dt.timedelta(days=60)).isoformat())
    return need(df, VALUE_FIELDS.split(","))


def em_cb_info():
    from simplequant.bonds.store import fetch_info
    info = fetch_info(CB_CODE)
    if not info:
        raise AssertionError("empty terms")
    return f"{len(info)} fields"


# ---------------- 新浪 ----------------
def sina_etf():
    from simplequant.data import sina
    return need(sina.etf("510300", "hfq"), ["open", "high", "low", "close", "volume"], min_rows=100)


def sina_stock():
    from simplequant.data import sina
    return need(sina.stock("600519", "hfq"), ["open", "high", "low", "close", "volume", "turnover"], min_rows=100)


def sina_index():
    from simplequant.data import sina
    return need(sina.index("000300"), ["open", "high", "low", "close", "volume"], min_rows=100)


def sina_cb_daily():
    from simplequant.bonds.store import fetch_daily
    return need(fetch_daily(CB_CODE), ["date", "close", "volume"], min_rows=20)


# ---------------- BaoStock（一次登录，查程序用到的各个接口） ----------------
def baostock():
    import baostock as bs
    from simplequant.stocks.store import DAILY_FIELDS, _login, _query
    _login()
    try:
        code = "sh.600519"
        checks = {
            "k_data": (lambda: _query(bs.query_history_k_data_plus, code, DAILY_FIELDS, start_date=START,
                                      end_date=END, frequency="d", adjustflag="3"), ["close", "peTTM", "isST"]),
            "adjust_factor": (lambda: _query(bs.query_adjust_factor, code=code, start_date="2015-01-01",
                                             end_date=END), ["dividOperateDate"]),
            "hs300": (lambda: _query(bs.query_hs300_stocks, date=END), ["code"]),
            "zz500": (lambda: _query(bs.query_zz500_stocks, date=END), ["code"]),
            "industry": (lambda: _query(bs.query_stock_industry, code=code), ["industry"]),
            "stock_basic": (lambda: _query(bs.query_stock_basic, code=code), ["ipoDate"]),
            "trade_dates": (lambda: _query(bs.query_trade_dates, start_date=START, end_date=END),
                            ["is_trading_day"]),
            "dividend": (lambda: _query(bs.query_dividend_data, code=code, year=str(TODAY.year - 1),
                                        yearType="operate"), ["dividCashPsBeforeTax"]),
            "profit": (lambda: _query(bs.query_profit_data, code=code, year=TODAY.year - 1, quarter=4),
                       ["roeAvg", "pubDate"]),
        }
        parts = []
        for name, (fn, cols) in checks.items():
            try:
                parts.append(f"{name} {need(fn(), cols)}")
            except Exception as e:  # noqa: BLE001
                raise AssertionError(f"{name}: {e}") from e
        return "; ".join(parts)
    finally:
        bs.logout()


# ---------------- 中证指数官网、中债 ----------------
def csindex():
    from simplequant.bonds.store import fetch_index
    return need(fetch_index(START, END), ["open", "close"])


def chinabond():
    import akshare as ak
    from simplequant.bonds.rates import process
    raw = ak.bond_china_yield(start_date=START.replace("-", ""), end_date=END.replace("-", ""))
    return need(process(pd.DataFrame(raw)), ["cgb_10y", "mtn_1y"])


CB_CODE = "113052"   # 兴业转债（规模大、存续期长）

CHECKS = [
    ("东方财富 ETF 日线", em_etf),
    ("东方财富 股票日线", em_stock),
    ("东方财富 配股", em_rights),
    ("东方财富 可转债列表", em_cb_list),
    ("东方财富 可转债价值分析", em_cb_value),
    ("东方财富 可转债条款", em_cb_info),
    ("新浪 ETF 日线与分红", sina_etf),
    ("新浪 股票日线、复权因子与流通股本", sina_stock),
    ("新浪 指数日线", sina_index),
    ("新浪 可转债日线", sina_cb_daily),
    ("BaoStock", baostock),
    ("中证指数官网 中证转债指数", csindex),
    ("中债 收益率曲线", chinabond),
]

# 失败只警告、不算失败的项：程序在这些接口失败时会改用备用接口（ETF / 股票日线 → 新浪），
# 且东方财富自 10 月起经常断开 K 线接口（Actions 上也是），算作失败会让 issue 一直开着
WARN_ONLY = {"东方财富 ETF 日线", "东方财富 股票日线"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", help="把结果表写进这个文件（Actions 用作 issue 正文和运行摘要）")
    ap.add_argument("--again", type=int, default=0, metavar="秒",
                    help="失败的项等这么多秒后再查一次，仍失败才算失败（东方财富偶尔会断开连接）")
    args = ap.parse_args()

    results = {name: run(fn) for name, fn in CHECKS}
    if args.again and not all(ok for ok, _, _ in results.values()):
        print(f"{args.again} 秒后重查失败的项……", flush=True)
        time.sleep(args.again)
        for name, fn in CHECKS:
            if not results[name][0]:
                results[name] = run(fn)
    status = {name: "ok" if ok else "warn" if name in WARN_ONLY else "fail"
              for name, (ok, _, _) in results.items()}
    label = {"ok": ("OK  ", "✅"), "warn": ("WARN", "⚠️"), "fail": ("FAIL", "❌")}
    for name, (ok, secs, detail) in results.items():
        print(f"{label[status[name]][0]} {name}  ({secs:.1f}s)  {detail}", flush=True)
        if status[name] == "warn" and os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title={name}::{detail}", flush=True)   # 显示在 Actions 运行页的注释里
    rows = [f"| {label[status[name]][1]} | {name} | {secs:.1f}s | {detail.replace('|', '/')} |"
            for name, (ok, secs, detail) in results.items()]
    failed = sum(s == "fail" for s in status.values())
    warned = sum(s == "warn" for s in status.values())

    if args.markdown:
        head = f"数据源检查（{dt.datetime.now():%Y-%m-%d %H:%M}，akshare {_version('akshare')}，" \
               f"baostock {_version('baostock')}）：{len(CHECKS) - failed - warned} 项通过，" \
               f"{warned} 项警告（有备用接口），{failed} 项失败\n\n"
        table = "| | 接口 | 用时 | 结果 |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
        Path(args.markdown).write_text(head + table, encoding="utf-8")
    sys.exit(1 if failed else 0)


def run(fn):
    """返回 (是否通过, 用时, 说明)；失败时把异常栈打到 stderr"""
    from simplequant.data import net, sina
    net.reset()           # 每项单独检查：不受前一项失败后「暂停访问」的影响
    sina.clear_cache()
    t0 = time.time()
    try:
        detail, ok = fn(), True
    except Exception as e:  # noqa: BLE001
        detail, ok = f"{type(e).__name__}: {e}".replace("\n", " ")[:300], False
        traceback.print_exc()
    return ok, time.time() - t0, detail


def _version(name):
    from importlib.metadata import version
    try:
        return version(name)
    except Exception:  # noqa: BLE001
        return "?"


if __name__ == "__main__":
    main()
