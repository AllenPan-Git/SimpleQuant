"""
自定义选股因子：用户在选股页「自定义因子」标签里写的 Python 函数

    def factor(p):
        return p["close"] / p["close"].shift(20) - 1      # 返回 DataFrame（日期 × 股票）

保存在 FACTOR_DIR/<键>.json：{"key", "name", "code", "direction", "desc"}。
启动时（导入 simplequant.stocks）登记进 FACTORS，分组为「自定义」，之后因子研究、选股回测、滚动优化、
模拟盘都和内置因子一样使用。T 日的值只能用 T 日收盘及以前的数据——用 shift(-n) 看未来会让回测失真。
代码在本机运行，权限与普通 Python 程序相同。
"""

import ast
import hashlib
import json
import linecache
import math
import re
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from ..i18n import L, pick
from ..paths import FACTOR_DIR
from .factors import FACTORS, GROUPS
from .fundamentals import FIELDS as FIN_FIELDS

KEY_PREFIX = "u_"
FILENAME = "<factor-code>"
GROUPS.setdefault("custom", L("自定义", "Custom"))
# 用到这些字段就需要财务数据
FIN_WORDS = set(FIN_FIELDS) | {"mcap"}
DIV_WORDS = {"div_cash", "div_bonus", "div_reserve"}    # 用到这些字段就需要分红数据
STOCK_WORDS = {"pe", "pb", "ps", "turnover"} | DIV_WORDS    # 只有股票有
CB_WORDS = {"premium", "bond_premium", "double_low", "conv_value", "bond_value", "conv_price", "stock_close",
            "remain_years", "issue_size", "remain_size", "rating_score"}     # 只有可转债有


class FactorCodeError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


def _msg(zh: str, en: str, lang: str) -> str:
    return pick(L(zh, en), lang)


def _user_line(tb) -> int | None:
    line = None
    for frame in traceback.extract_tb(tb):
        if frame.filename == FILENAME:
            line = frame.lineno
    return line


def _where(line, lang):
    return _msg(f"第 {line} 行：", f"Line {line}: ", lang) if line else ""


def namespace() -> dict:
    return {"np": np, "pd": pd, "math": math}


def check_syntax(code: str, lang: str = "zh") -> list[str]:
    if not code.strip():
        return [_msg("代码是空的", "The code is empty", lang)]
    try:
        tree = ast.parse(code, FILENAME)
    except SyntaxError as e:
        return [_where(e.lineno, lang) + _msg(f"语法错误（{e.msg}）", f"syntax error ({e.msg})", lang)]
    if not any(isinstance(n, ast.FunctionDef) and n.name == "factor" for n in tree.body):
        return [_msg("代码里要定义函数 def factor(p)", "The code must define a function def factor(p)", lang)]
    return []


def _uses(code: str, words: set) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Constant) and n.value in words for n in ast.walk(tree))


def uses_fin(code: str) -> bool:
    """代码里以字符串形式用到了财务字段（p["roe"]、p["mcap"] 等）"""
    return _uses(code, FIN_WORDS)


def uses_div(code: str) -> bool:
    """代码里用到了分红送转字段（p["div_cash"] 等）"""
    return _uses(code, DIV_WORDS)


def assets_of(code: str) -> tuple:
    """适用的品种：用到财务 / 估值字段的只适用于股票，用到转债字段的只适用于可转债，其余两者都可以"""
    if _uses(code, FIN_WORDS | STOCK_WORDS):
        return ("stock",)
    if _uses(code, CB_WORDS):
        return ("cb",)
    return ("stock", "cb")


def known_fields() -> set:
    from .panel import PRICE_FIELDS, OTHER_FIELDS
    return set(PRICE_FIELDS) | set(OTHER_FIELDS) | FIN_WORDS | {"total_share", "raw_high", "raw_low"} | DIV_WORDS | CB_WORDS


LOOKAHEAD_CALLS = {"shift", "pct_change", "diff"}      # 参数为负数时用到未来数据
BACKFILL_CALLS = {"bfill", "backfill"}                 # 用后面的值往前填
UNSAFE_NAMES = {"open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals",
                "vars", "input", "breakpoint", "exit", "quit"}
IO_ATTRS = {"to_csv", "to_excel", "to_pickle", "to_parquet", "to_json", "to_sql", "to_hdf", "to_feather", "to_html",
            "to_clipboard", "tofile", "save", "savez", "load", "loadtxt", "fromfile", "genfromtxt", "system", "popen"}


def _negative(node) -> bool:
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float))
            and node.operand.value > 0)


def review(code: str, lang: str = "zh", strict: bool = False) -> list[str]:
    """
    静态检查（不运行代码）：偷看未来（shift(-n)、bfill、center=True）、p[...] 用了不存在的字段。
    strict=True 时再检查 import、open / eval 等和 __ 开头的属性（AI 生成的代码用；自己写的代码可以 import）
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "factor"), None)
    arg = fn.args.args[0].arg if fn and fn.args.args else None
    fields = known_fields()
    out = []

    def add(node, zh, en):
        item = (getattr(node, "lineno", 0), _where(getattr(node, "lineno", None), lang) + _msg(zh, en, lang))
        if item not in out:
            out.append(item)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            name = n.func.attr
            if name in LOOKAHEAD_CALLS:
                periods = n.args[0] if n.args else next((k.value for k in n.keywords if k.arg == "periods"), None)
                if periods is not None and _negative(periods):
                    add(n, f"{name}(负数) 会用到未来的数据，回测会失真；只能往回看（正数）",
                        f"{name}(negative) uses future data and distorts the backtest; only look back (positive)")
            if name in BACKFILL_CALLS or (name == "fillna" and any(
                    k.arg == "method" and isinstance(k.value, ast.Constant) and k.value.value in BACKFILL_CALLS
                    for k in n.keywords)):
                add(n, "bfill 用后面的值往前填，会用到未来的数据；可以用 ffill",
                    "bfill fills from later values and uses future data; use ffill instead")
            if name == "rolling" and any(k.arg == "center" and isinstance(k.value, ast.Constant) and k.value.value
                                         for k in n.keywords):
                add(n, "rolling(center=True) 会用到未来的数据", "rolling(center=True) uses future data")
        if (arg and isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == arg
                and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)
                and n.slice.value not in fields):
            add(n, f"没有字段 {arg}[\"{n.slice.value}\"]", f"There is no field {arg}[\"{n.slice.value}\"]")
        if not strict:
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            add(n, "不要 import：np、pd、math 已经可以直接使用", "No imports: np, pd and math are already available")
        elif isinstance(n, ast.Name) and n.id in UNSAFE_NAMES:
            add(n, f"不允许使用 {n.id}", f"{n.id} is not allowed")
        elif isinstance(n, ast.Attribute) and (n.attr.startswith(("__", "read_")) or n.attr in IO_ATTRS):
            add(n, f"不允许使用 {n.attr}", f"{n.attr} is not allowed")
    return [m for _, m in sorted(out, key=lambda x: x[0])]


_CACHE: dict[str, object] = {}


def compile_factor(code: str, lang: str = "zh"):
    """返回用户写的 factor 函数（同一段代码只编译一次）"""
    if code in _CACHE:
        return _CACHE[code]
    errors = check_syntax(code, lang)
    if errors:
        raise FactorCodeError(errors[0])
    ns = namespace()
    linecache.cache[FILENAME] = (len(code), None, code.splitlines(True), FILENAME)
    try:
        exec(compile(code, FILENAME, "exec"), ns)  # noqa: S102 - 用户在本机运行自己的因子代码
    except Exception as e:  # noqa: BLE001
        line = _user_line(e.__traceback__)
        raise FactorCodeError(_where(line, lang) + f"{type(e).__name__}: {e}", line) from e
    fn = ns.get("factor")
    if not callable(fn):
        raise FactorCodeError(_msg("factor 必须是函数", "factor must be a function", lang))
    _CACHE[code] = fn
    return fn


def evaluate(code: str, panel, lang: str = "zh") -> pd.DataFrame:
    """在面板上计算因子；结果对齐成 日期 × 股票 的浮点表"""
    fn = compile_factor(code, lang)
    try:
        out = fn(panel)
    except FactorCodeError:
        raise
    except Exception as e:  # noqa: BLE001
        line = _user_line(e.__traceback__)
        src = linecache.getline(FILENAME, line).strip() if line else ""
        raise FactorCodeError(_where(line, lang) + f"{type(e).__name__}: {e}" + (f"\n    {src}" if src else ""),
                              line) from e
    if not isinstance(out, pd.DataFrame):
        raise FactorCodeError(_msg(f"factor 要返回 DataFrame（日期 × 股票），实际返回了 {type(out).__name__}",
                                   f"factor must return a DataFrame (dates × stocks), got {type(out).__name__}", lang))
    ref = panel["close"]
    if not out.index.equals(ref.index) and out.index.intersection(ref.index).empty:
        raise FactorCodeError(_msg("返回表的行不是交易日（应与 p[\"close\"] 相同）",
                                   "Rows of the result are not the trading days (should match p[\"close\"])", lang))
    try:
        return out.reindex(index=ref.index, columns=ref.columns).astype(float)
    except (TypeError, ValueError) as e:
        raise FactorCodeError(_msg(f"返回值不是数字：{e}", f"Result is not numeric: {e}", lang)) from e


# ---------------- 保存与登记 ----------------
def new_key(name: str) -> str:
    return KEY_PREFIX + hashlib.sha1(f"{name}".encode("utf-8")).hexdigest()[:8]


def is_custom(key: str) -> bool:
    return key.startswith(KEY_PREFIX)


def _entry(d: dict) -> dict:
    code = d["code"]
    return {"label": L(d["name"], d["name"]), "group": "custom", "direction": 1 if d.get("direction", 1) > 0 else -1,
            "fn": lambda p, code=code: evaluate(code, p), "desc": L(d.get("desc", ""), d.get("desc", "")),
            "requires_fin": uses_fin(code), "requires_div": uses_div(code), "custom": True, "code": code, "name": d["name"],
            "assets": assets_of(code)}


def list_factors(store_dir: Path | None = None) -> dict[str, dict]:
    """{键: {"key", "name", "code", "direction", "desc"}}"""
    store_dir = Path(store_dir or FACTOR_DIR)
    out = {}
    if store_dir.exists():
        for f in sorted(store_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if is_custom(d.get("key", "")) and d.get("name") and isinstance(d.get("code"), str):
                    out[d["key"]] = d
            except (json.JSONDecodeError, OSError):
                continue
    return out


def load_all(store_dir: Path | None = None) -> None:
    """把保存的自定义因子登记进 FACTORS（先移除旧的登记，删除/改名后不会残留）"""
    for k in [k for k in FACTORS if is_custom(k)]:
        del FACTORS[k]
    for k, d in list_factors(store_dir).items():
        FACTORS[k] = _entry(d)


def save_factor(name: str, code: str, direction: int = 1, desc: str = "", key: str | None = None,
                store_dir: Path | None = None) -> str:
    """保存并登记，返回因子的键；key 为空时按名字生成（名字已存在则覆盖那一个）"""
    name = name.strip()
    if not name:
        raise ValueError("name required / 需要名称")
    existing = {d["name"]: k for k, d in list_factors(store_dir).items()}
    key = key or existing.get(name) or new_key(name)
    if not re.fullmatch(r"u_[0-9a-f]{8}", key):
        raise ValueError(f"bad key {key!r}")
    store_dir = Path(store_dir or FACTOR_DIR)
    store_dir.mkdir(parents=True, exist_ok=True)
    d = {"key": key, "name": name, "code": code, "direction": 1 if direction > 0 else -1, "desc": desc.strip()}
    (store_dir / f"{key}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    load_all(store_dir)
    return key


def delete_factor(key: str, store_dir: Path | None = None) -> None:
    if not re.fullmatch(r"u_[0-9a-f]{8}", key):
        return
    (Path(store_dir or FACTOR_DIR) / f"{key}.json").unlink(missing_ok=True)
    load_all(store_dir)


SKELETON = {
    "zh": '''def factor(p):
    """20 日均线乖离率：收盘价比 20 日均线高出多少（越低越超跌）"""
    # p["close"] 是 日期 × 股票 的表（后复权收盘价），每一列是一只股票
    # 可用字段：open high low close volume amount turnover pe pb ps pct_chg raw_close（不复权收盘价）
    #          下载财务数据后还有：roe gross_margin net_margin np_yoy rev_yoy mcap
    #          下载分红数据后还有：div_cash（每股现金分红，税前）div_bonus（每股送股）div_reserve（每股转增），
    #          只在除权除息日有值，其余为 0
    # T 日的值只能用 T 日及以前的数据：可以 shift(5)（往回看），不要 shift(-5)（偷看未来）
    close = p["close"]
    ma = close.rolling(20, min_periods=15).mean()
    return close / ma - 1
''',
    "en": '''def factor(p):
    """20-day MA deviation: how far the close is above its 20-day average (low = oversold)"""
    # p["close"] is a dates × stocks table (back-adjusted closes), one column per stock
    # Fields: open high low close volume amount turnover pe pb ps pct_chg raw_close (unadjusted close)
    #         with financial data downloaded also: roe gross_margin net_margin np_yoy rev_yoy mcap
    #         with dividend data downloaded also: div_cash (cash per share, pre-tax) div_bonus (bonus shares
    #         per share) div_reserve (capitalization shares per share), non-zero only on ex-dates
    # Day T may only use data up to day T: shift(5) looks back, never shift(-5) (peeks into the future)
    close = p["close"]
    ma = close.rolling(20, min_periods=15).mean()
    return close / ma - 1
''',
}


def skeleton(lang: str = "zh") -> str:
    return SKELETON.get(lang, SKELETON["zh"])
