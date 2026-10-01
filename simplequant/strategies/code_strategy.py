"""
代码策略：用户在策略页的代码编辑区自己写（或由模板/规则转成代码后修改）的 Backtrader 策略

策略描述：{"kind": "code", "code": "...", "params": {参数: 值}}
- code 里必须定义 class Strategy，继承 BaseStrategy 或 PerAsset（整手、T+1、成交记录照常工作）
- params 是对代码里默认参数的覆盖（参数优化、「套用最优参数」时产生）；为空时用代码里写的默认值
- 代码里可以直接用：bt、np、pd、math、BaseStrategy、PerAsset、KDJ、OBV
代码在本机运行，权限与普通 Python 程序相同。
"""

import ast
import hashlib
import inspect
import linecache
import math
import re
import sys
import traceback
import types

import backtrader as bt
import numpy as np
import pandas as pd

from ..engine.base_strategy import BaseStrategy
from ..i18n import L, pick
from .factors import KDJ, OBV
from .templates import _PerAsset

FILENAME = "<strategy-code>"
BASE_PARAMS = set(BaseStrategy.params._getkeys())        # 引擎内部参数，不给用户调
PerAsset = _PerAsset


class CodeError(Exception):
    """代码有错：message 已是给用户看的文字，line 为出错行号（可能为 None）"""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


def _msg(zh: str, en: str, lang: str) -> str:
    return pick(L(zh, en), lang)


def _where(line: int | None, lang: str) -> str:
    return _msg(f"第 {line} 行：", f"Line {line}: ", lang) if line else ""


def namespace() -> dict:
    """代码里可以直接用的名字"""
    return {"bt": bt, "np": np, "pd": pd, "math": math, "BaseStrategy": BaseStrategy,
            "PerAsset": _PerAsset, "_PerAsset": _PerAsset, "KDJ": KDJ, "OBV": OBV}


# ---------------- 编译 ----------------
_CACHE: dict[str, type] = {}


def _user_line(tb) -> int | None:
    """traceback 中最后一个位于用户代码里的行号"""
    line = None
    for frame in traceback.extract_tb(tb):
        if frame.filename == FILENAME:
            line = frame.lineno
    return line


def check_syntax(code: str, lang: str = "zh") -> list[str]:
    """只做语法检查（不执行代码），编辑时实时调用"""
    if not code.strip():
        return [_msg("代码是空的", "The code is empty", lang)]
    try:
        tree = ast.parse(code, FILENAME)
    except SyntaxError as e:
        return [_where(e.lineno, lang) + _msg(f"语法错误（{e.msg}）", f"syntax error ({e.msg})", lang)]
    if _strategy_class(tree) is None and not any(
            isinstance(n, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id == "Strategy" for tg in n.targets)
            for n in tree.body):
        return [_msg("代码里要定义 class Strategy（继承 PerAsset 或 BaseStrategy）",
                     "The code must define class Strategy (subclass of PerAsset or BaseStrategy)", lang)]
    return []


def compile_strategy(code: str, lang: str = "zh") -> type:
    """执行代码，返回其中的 Strategy 类（同一段代码只编译一次）"""
    if code in _CACHE:
        return _CACHE[code]
    errors = check_syntax(code, lang)
    if errors:
        raise CodeError(errors[0])
    # 以独立模块运行：Backtrader 的元类会按 __module__ 查找模块；linecache 让出错信息能显示源码行
    mod_name = "sq_user_strategy_" + hashlib.sha1(code.encode("utf-8")).hexdigest()[:12]
    module = types.ModuleType(mod_name)
    module.__dict__.update(namespace())
    sys.modules[mod_name] = module
    linecache.cache[FILENAME] = (len(code), None, code.splitlines(True), FILENAME)
    try:
        exec(compile(code, FILENAME, "exec"), module.__dict__)  # noqa: S102 - 用户在本机运行自己的策略代码
    except Exception as e:  # noqa: BLE001
        sys.modules.pop(mod_name, None)
        raise CodeError(_where(_user_line(e.__traceback__), lang) + f"{type(e).__name__}: {e}",
                        _user_line(e.__traceback__)) from e
    cls = module.__dict__.get("Strategy")
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        sys.modules.pop(mod_name, None)
        raise CodeError(_msg("Strategy 必须是继承 PerAsset 或 BaseStrategy 的类",
                             "Strategy must be a class derived from PerAsset or BaseStrategy", lang))
    _CACHE[code] = cls
    return cls


def check(code: str, lang: str = "zh") -> list[str]:
    """完整检查（会执行代码的定义部分）：保存、回测前调用"""
    try:
        compile_strategy(code, lang)
    except CodeError as e:
        return [str(e)]
    return []


def explain_error(exc: BaseException, lang: str = "zh") -> str:
    """回测时出错：若错误来自用户代码，指出行号和那一行的内容"""
    line = _user_line(exc.__traceback__)
    if line is None:
        return str(exc)
    src = linecache.getline(FILENAME, line).strip()
    head = _msg(f"策略代码第 {line} 行出错", f"Error in strategy code, line {line}", lang)
    return f"{head}: {type(exc).__name__}: {exc}" + (f"\n    {src}" if src else "")


# ---------------- 参数 ----------------
def code_params(cls: type) -> dict:
    """代码里声明的参数及默认值（不含引擎内部参数）"""
    return {k: v for k, v in cls.params._getpairs().items() if k not in BASE_PARAMS}


def resolve(spec: dict):
    cls = compile_strategy(spec["code"])
    return cls, {**code_params(cls), **(spec.get("params") or {})}


def min_assets(spec: dict) -> int:
    try:
        return int(getattr(compile_strategy(spec["code"]), "MIN_ASSETS", 1))
    except CodeError:
        return 1


def _strategy_class(tree: ast.Module) -> ast.ClassDef | None:
    return next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Strategy"), None)


def _params_node(cls_node: ast.ClassDef) -> ast.Assign | None:
    return next((n for n in cls_node.body if isinstance(n, ast.Assign)
                 and any(isinstance(tg, ast.Name) and tg.id == "params" for tg in n.targets)), None)


def literal_params(code: str) -> dict:
    """不执行代码，从 class Strategy 的 params = (...) 里读出参数（写法不是字面量时返回空）"""
    try:
        cls_node = _strategy_class(ast.parse(code))
        node = cls_node and _params_node(cls_node)
        if node is None:
            return {}
        value = ast.literal_eval(node.value)
        return dict(value.items() if isinstance(value, dict) else value)
    except (SyntaxError, ValueError, TypeError):
        return {}


def set_params(code: str, values: dict) -> str:
    """把 class Strategy 的 params 默认值改成 values（没有的参数追加），返回新代码"""
    if not values:
        return code
    tree = ast.parse(code)
    cls_node = _strategy_class(tree)
    if cls_node is None:
        raise ValueError("no class Strategy")
    node = _params_node(cls_node)
    current = literal_params(code) if node is not None else {}
    merged = {k: v.item() if isinstance(v, np.generic) else v for k, v in {**current, **values}.items()}
    indent = " " * (cls_node.body[0].col_offset if cls_node.body else cls_node.col_offset + 4)
    text = indent + "params = (" + ", ".join(f"({k!r}, {v!r})" for k, v in merged.items()) + \
        ("," if len(merged) == 1 else "") + ")"
    lines = code.splitlines()
    if node is not None:
        lines[node.lineno - 1:node.end_lineno] = [text]
    else:
        first = cls_node.body[0]
        is_doc = isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
            and isinstance(first.value.value, str)
        at = first.end_lineno if is_doc else cls_node.body[0].lineno - 1
        lines[at:at] = [text]
    return "\n".join(lines) + ("\n" if code.endswith("\n") else "")


def materialize(spec: dict) -> str:
    """把 params 覆盖写回代码（载入到编辑器时用）"""
    params = {k: v for k, v in (spec.get("params") or {}).items() if k not in BASE_PARAMS}
    return set_params(spec["code"], params) if params else spec["code"]


def describe(spec: dict, lang: str = "zh") -> str:
    """不执行代码的简短说明：Strategy 文档字符串的第一行 + 参数"""
    code = spec.get("code", "")
    title = ""
    try:
        cls_node = _strategy_class(ast.parse(code))
        if cls_node is not None:
            title = (ast.get_docstring(cls_node) or "").strip().splitlines()[0:1]
            title = title[0] if title else ""
    except SyntaxError:
        pass
    n = len(code.strip().splitlines())
    head = _msg("【自定义代码】", "[Custom code] ", lang) + (title or _msg(f"{n} 行", f"{n} lines", lang))
    params = {**literal_params(code), **(spec.get("params") or {})}
    params = {k: v for k, v in params.items() if k not in BASE_PARAMS}
    if not params:
        return head
    sep = "，" if lang == "zh" else ", "
    return head + "\n" + sep.join(f"{k}={v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool)
                                  else f"{k}={v!r}" for k, v in params.items())


# ---------------- 模板 / 规则 → 代码 ----------------
def template_code(key: str, params: dict) -> str:
    """模板策略类的源码，类名改成 Strategy，参数默认值换成当前值"""
    from .registry import TEMPLATES
    tpl = TEMPLATES[key]
    src = inspect.getsource(tpl.cls)
    src = re.sub(rf"^class {tpl.cls.__name__}\((\w+)\):",
                 lambda m: f"class Strategy({'PerAsset' if m.group(1) == '_PerAsset' else m.group(1)}):", src,
                 count=1, flags=re.M)
    src = re.sub(r"[（(]迁移自[^）)]*[）)]", "", src)      # 内部来源说明对用户没有意义
    values = {**tpl.defaults(), **{k: v for k, v in params.items() if k not in BASE_PARAMS}}
    src = set_params(src, values)
    if tpl.min_assets > 1:
        src = src.replace("    params = (", f"    MIN_ASSETS = {tpl.min_assets}      # 至少需要几个标的\n    params = (", 1)
    return src


def rule_code(rule: dict, lang: str = "zh") -> str:
    from ..export.python_script import rule_strategy_code
    return rule_strategy_code(rule, lang).replace("(_PerAsset)", "(PerAsset)", 1)


def to_code(spec: dict, lang: str = "zh") -> str:
    """模板 / 规则 / 代码策略 → 可编辑的代码"""
    kind = spec.get("kind")
    if kind == "code":
        return materialize(spec)
    if kind == "rule":
        return rule_code(spec["rule"], lang)
    if kind == "template":
        return template_code(spec["template"], spec.get("params") or {})
    raise ValueError(f"cannot convert {kind!r} to code")


SKELETON = {
    "zh": '''class Strategy(PerAsset):
    """我的策略：快线在慢线上方时持有"""
    # 可调参数：写在这里的数字会出现在「参数优化」页，代码里用 self.p.名字 读取
    params = (("position_pct", 95), ("fast", 5), ("slow", 20))

    def __init__(self):
        super().__init__()
        # 给每个标的准备指标；self.datas 是回测时选的所有标的
        self.fast_ma = {d: bt.ind.SMA(d.close, period=int(self.p.fast)) for d in self.datas}
        self.slow_ma = {d: bt.ind.SMA(d.close, period=int(self.p.slow)) for d in self.datas}

    def next(self):
        # 每根 K 线收盘后调用一次；委托在下一根 K 线开盘成交
        # [0] 是当前这根 K 线的值，[-1] 是上一根
        for d in self.datas:
            holding = self.getposition(d).size > 0
            if not holding and self.fast_ma[d][0] > self.slow_ma[d][0]:
                # 调整到总资产的 slot_pct（= 仓位比例 / 标的数），自动按 100 股取整
                self.order_target_pct(d, self.slot_pct, "快线在慢线上方")
            elif holding and self.fast_ma[d][0] < self.slow_ma[d][0]:
                self.order_target_pct(d, 0, "快线跌破慢线")
''',
    "en": '''class Strategy(PerAsset):
    """My strategy: hold while the fast MA is above the slow MA"""
    # Tunable parameters: numbers here show up on the Optimize page; read them as self.p.name
    params = (("position_pct", 95), ("fast", 5), ("slow", 20))

    def __init__(self):
        super().__init__()
        # Indicators for each asset; self.datas holds every asset selected for the backtest
        self.fast_ma = {d: bt.ind.SMA(d.close, period=int(self.p.fast)) for d in self.datas}
        self.slow_ma = {d: bt.ind.SMA(d.close, period=int(self.p.slow)) for d in self.datas}

    def next(self):
        # Called once after each bar closes; orders fill at the next bar's open
        # [0] is the current bar's value, [-1] the previous one
        for d in self.datas:
            holding = self.getposition(d).size > 0
            if not holding and self.fast_ma[d][0] > self.slow_ma[d][0]:
                # Target slot_pct of total equity (= position % / number of assets), rounded to 100-share lots
                self.order_target_pct(d, self.slot_pct, "fast MA above slow MA")
            elif holding and self.fast_ma[d][0] < self.slow_ma[d][0]:
                self.order_target_pct(d, 0, "fast MA fell below slow MA")
''',
}


def skeleton(lang: str = "zh") -> str:
    return SKELETON.get(lang, SKELETON["zh"])


CHEATSHEET = {
    "zh": """# 行情（d 是一个标的，[0] 当前 K 线、[-1] 上一根）
d.close[0]  d.open[0]  d.high[0]  d.low[0]  d.volume[0]
d.turnover[0]  d.pe[0]  d.pb[0]  d.ps[0]      # BaoStock 股票日线才有，否则是 NaN
d._name                                         # 标的名称
self.datas                                      # 回测选的所有标的

# 指标（在 __init__ 里创建，next 里用 [0] 取值）
bt.ind.SMA(d.close, period=20)   bt.ind.EMA(...)   bt.ind.RSI(d.close, period=14)
bt.ind.MACD(d.close).macd / .signal              bt.ind.BollingerBands(d.close).top / .mid / .bot
bt.ind.ATR(d, period=14)   bt.ind.Highest(d.high, period=20)   bt.ind.CrossOver(a, b)  # >0 上穿、<0 下穿
KDJ(d).k / .d / .j      OBV(d).obv

# 持仓与下单
self.getposition(d).size / .price               # 持股数、持仓均价
self.broker.getvalue()   self.broker.getcash()  # 总资产、可用资金
self.order_target_pct(d, 0.5, "理由")            # 调到总资产的 50%（0 = 清仓），自动整手、T+1
self.slot_pct                                   # PerAsset：position_pct / 标的数
self.can_sell(d)   self.has_pending(d)          # T+1 是否可卖、是否有未成交委托

# 参数与其他
params = (("period", 20),)   →  self.p.period    # 数字参数会出现在参数优化页
MIN_ASSETS = 2                                  # 至少需要几个标的（写在类里）
np  pd  math                                    # 可直接使用""",
    "en": """# Prices (d is one asset; [0] = current bar, [-1] = previous)
d.close[0]  d.open[0]  d.high[0]  d.low[0]  d.volume[0]
d.turnover[0]  d.pe[0]  d.pb[0]  d.ps[0]      # BaoStock stock daily data only, NaN otherwise
d._name                                         # asset name
self.datas                                      # every asset in the backtest

# Indicators (create in __init__, read with [0] in next)
bt.ind.SMA(d.close, period=20)   bt.ind.EMA(...)   bt.ind.RSI(d.close, period=14)
bt.ind.MACD(d.close).macd / .signal              bt.ind.BollingerBands(d.close).top / .mid / .bot
bt.ind.ATR(d, period=14)   bt.ind.Highest(d.high, period=20)   bt.ind.CrossOver(a, b)  # >0 up, <0 down
KDJ(d).k / .d / .j      OBV(d).obv

# Positions and orders
self.getposition(d).size / .price               # shares held, average cost
self.broker.getvalue()   self.broker.getcash()  # equity, cash
self.order_target_pct(d, 0.5, "reason")         # target 50% of equity (0 = exit); lots and T+1 handled
self.slot_pct                                   # PerAsset: position_pct / number of assets
self.can_sell(d)   self.has_pending(d)          # sellable under T+1? unfilled order?

# Parameters and more
params = (("period", 20),)   ->  self.p.period   # numeric params appear on the Optimize page
MIN_ASSETS = 2                                  # minimum number of assets (class attribute)
np  pd  math                                    # available directly""",
}
