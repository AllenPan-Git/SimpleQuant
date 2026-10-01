"""
与界面框架无关的页面逻辑（这里不能 import nicegui，便于单独测试）
格式化、表格整理、导出、示例规则、CSV 导入等；需要当前语言的函数都显式传入 lg。
"""

import copy
import math
import re
import uuid

import pandas as pd

from simplequant.data import library
from simplequant.data.base import freq_label
from simplequant.i18n import L, render, tr
from simplequant.rules import make_ind
from ui import texts  # noqa: F401  注册界面文字

SOURCE_KEYS = ("akshare", "baostock", "tdx_local", "csv")

# 常用标的（宽基 ETF）
POPULAR = {
    "510300": L("沪深300ETF", "CSI 300 ETF"), "510500": L("中证500ETF", "CSI 500 ETF"),
    "159915": L("创业板ETF", "ChiNext ETF"), "588000": L("科创50ETF", "STAR 50 ETF"),
    "510050": L("上证50ETF", "SSE 50 ETF"), "512100": L("中证1000ETF", "CSI 1000 ETF"),
    "511130": L("30年国债ETF", "30Y Treasury ETF"), "511090": L("30年国债ETF(鹏扬)", "30Y Treasury ETF (Pengyang)"),
    "518880": L("黄金ETF", "Gold ETF"), "513100": L("纳指ETF", "Nasdaq 100 ETF"),
}
ADJUST = {"hfq": "adj.hfq", "qfq": "adj.qfq", "": "adj.none"}


# ---------------- 格式化 ----------------
def dataset_label(m: library.DatasetMeta, lg: str) -> str:
    return f"{m.name} · {freq_label(m.freq, lg)} · {m.start}~{m.end} · {tr('src.' + m.source, lg)}"


def pct(v, digits=2) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:.{digits}f}%"


def num(v, digits=2) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:,.{digits}f}"


PCT_METRICS = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "benchmark_return", "excess_return"}


def fmt_metric(key: str, v) -> str:
    return pct(v) if key in PCT_METRICS else num(v, 0 if key in ("final_value", "initial_cash", "trades") else 2)


# 结果页的指标卡片：两行，各 5 个
TILE_ROWS = [["total_return", "cagr", "max_drawdown", "sharpe", "final_value"],
             ["benchmark_return", "volatility", "trades", "win_rate", "profit_factor"]]
TILE_HELP = {"benchmark_return": "help.benchmark", "trades": "help.trades", "profit_factor": "help.profit_factor"}


# ---------------- 表格 ----------------
def orders_table(orders: pd.DataFrame, lg: str) -> pd.DataFrame:
    df = orders.copy()
    df["side"] = df["side"].map(lambda s: tr(f"side.{s}", lg))
    return df.rename(columns={c: tr(f"col.{c}", lg) for c in df.columns})


def trades_table(trades: pd.DataFrame, lg: str) -> pd.DataFrame:
    return trades.rename(columns={c: tr(f"col.{c}", lg) for c in trades.columns})


def logs_table(logs: list, lg: str) -> pd.DataFrame:
    return pd.DataFrame([(dt, render(key, kw, lg)) for dt, key, kw in logs],
                        columns=[tr("col.time", lg), tr("col.event", lg)])


# ---------------- 数据 ----------------
def parse_symbols(picks: list, extra: str) -> list[str]:
    """常用标的 + 手填代码（中英文逗号分隔），去重保序"""
    return list(dict.fromkeys(picks + [s.strip() for s in extra.replace("，", ",").split(",") if s.strip()]))


def slice_prices(frames: dict[str, pd.DataFrame], date_range) -> dict[str, pd.DataFrame]:
    """按日期区间截取（含结束日当天全部 K 线）"""
    start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
    return {name: df[(df.index >= start) & (df.index < end)] for name, df in frames.items()}


def import_csv(files: list, rule, name: str) -> library.DatasetMeta:
    """把一个或多个 CSV（路径或文件对象）合并、去重后存入本地数据库"""
    from simplequant.data.base import infer_freq
    from simplequant.data.csv_src import CSVSource
    parts = [CSVSource().fetch(f, resample=rule) for f in files]
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    freq_code = infer_freq(df)
    ds_id = library.make_id("csv", name, freq_code, "", df.index[0].strftime("%Y%m%d"), df.index[-1].strftime("%Y%m%d"))
    return library.save(df, library.DatasetMeta(id=ds_id, name=name, symbol=name, source="csv", freq=freq_code))


# ---------------- 导出 ----------------
def export_filename(label: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", label.split("：")[-1].split(": ")[-1]).strip("_")
    return f"{name or 'strategy'}.py"


def export_script(spec: dict, label: str, by_id: dict, ids: list, date_range, broker, lg: str) -> str:
    """回测页当前的数据、策略和费率 → 独立 Python 脚本"""
    from dataclasses import asdict
    from simplequant.export import single_asset_script
    data = []
    for i in ids:
        m = by_id[i]
        if m.source in ("akshare", "baostock"):
            data.append({"name": m.name, "source": m.source, "symbol": m.symbol, "asset": m.extra.get("asset", ""),
                         "adjust": m.adjust, "start": str(date_range[0]), "end": str(date_range[1])})
        else:   # 本地数据：脚本读取同名 CSV（在数据页「下载 CSV」）
            data.append({"name": m.name, "source": "csv", "path": f"{m.symbol}.csv"})
    return single_asset_script(spec, data, asdict(broker), title=label, lang=lg, filename=export_filename(label))


def platform_export(platform: str, spec: dict, label: str, by_id: dict, ids: list, date_range, broker,
                    lg: str) -> tuple[bytes | None, str | None]:
    """聚宽 / 掘金 / QMT 策略文件；不能导出时返回 (None, 原因)"""
    from dataclasses import asdict
    from simplequant.export import platform_script, encode_script
    if any(by_id[i].freq != "1d" for i in ids):
        return None, tr("exp.daily_only", lg)
    items = [{"name": by_id[i].name, "symbol": by_id[i].symbol, "asset": by_id[i].extra.get("asset", "")} for i in ids]
    try:
        code = platform_script(platform, spec, items, asdict(broker), str(date_range[0]), str(date_range[1]),
                               title=label, lang=lg)
    except ValueError as e:
        return None, str(e)
    return encode_script(code, platform), None


# ---------------- 参数优化 ----------------
def default_range(tb, n_steps: int = 8):
    """参数优化的默认范围：当前值的一半到两倍（整数）或 ±50%（小数），约 n_steps 步"""
    v = tb.value
    if tb.is_int:
        a, b = max(tb.min or 1, round(v * 0.5)), round(v * 2) if v > 0 else round(v * 0.5)
        a, b = min(a, b), max(a, b)
        if tb.max is not None:
            b = min(b, tb.max)
        return a, b, max(1, round((b - a) / n_steps))
    a, b = (v * 0.5, v * 1.5) if v >= 0 else (v * 1.5, v * 0.5)
    if a == b:
        a, b = a - 1, b + 1
    if tb.min is not None:
        a = max(a, tb.min)
    return round(a, 4), round(b, 4), round((b - a) / n_steps, 4) or 0.1


# ---------------- 条件积木 ----------------
def cond(left, op, right):
    """一条条件；_id 只给界面区分控件用，保存前由 clean() 去掉"""
    return {"_id": uuid.uuid4().hex[:8], "left": left, "op": op, "right": right}


def val(v):
    return {"value": v}


# 示例规则（名称见 ui/texts.py 的 preset.*）
PRESETS = {
    "ma": lambda: {
        "buy": {"logic": "all", "conditions": [cond(make_ind("sma", period=5), "cross_above", make_ind("sma", period=20))]},
        "sell": {"logic": "any", "conditions": [cond(make_ind("sma", period=5), "cross_below", make_ind("sma", period=20))]},
        "position_pct": 95},
    "macd": lambda: {
        "buy": {"logic": "all", "conditions": [cond(make_ind("macd"), "cross_above", {**make_ind("macd"), "line": "dea"})]},
        "sell": {"logic": "any", "conditions": [cond(make_ind("macd"), "cross_below", {**make_ind("macd"), "line": "dea"}),
                                                cond({"ind": "pnl_pct"}, "<", val(-8))]},
        "position_pct": 95},
    "breakout": lambda: {
        "buy": {"logic": "all", "conditions": [cond(make_ind("close"), ">", make_ind("highest", period=20))]},
        "sell": {"logic": "any", "conditions": [cond(make_ind("close"), "<", make_ind("lowest", period=10))]},
        "position_pct": 95},
    "rsi": lambda: {
        "buy": {"logic": "all", "conditions": [cond(make_ind("rsi", period=14), "cross_above", val(30))]},
        "sell": {"logic": "any", "conditions": [cond(make_ind("rsi", period=14), ">", val(70)),
                                                cond({"ind": "pnl_pct"}, "<", val(-5))]},
        "position_pct": 95},
    "boll": lambda: {
        "buy": {"logic": "all", "conditions": [cond(make_ind("close"), "<", {**make_ind("boll"), "line": "lower"})]},
        "sell": {"logic": "any", "conditions": [cond(make_ind("close"), ">", {**make_ind("boll"), "line": "mid"})]},
        "position_pct": 95},
}


def clean(rule: dict) -> dict:
    """去掉界面用的 _id，得到可保存/回测的规则"""
    r = copy.deepcopy(rule)
    for side in ("buy", "sell"):
        for c in r[side]["conditions"]:
            c.pop("_id", None)
    return r


def with_ids(rule: dict) -> dict:
    r = copy.deepcopy(rule)
    for side in ("buy", "sell"):
        r.setdefault(side, {"logic": "any", "conditions": []})
        for c in r[side]["conditions"]:
            c["_id"] = uuid.uuid4().hex[:8]
    return r
