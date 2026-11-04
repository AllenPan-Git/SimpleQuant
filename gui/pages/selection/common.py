"""选股页各标签页共用的状态与常量（面板缓存在 gui/sel_panel.py，与工具页共用）"""

import pandas as pd
from nicegui import run

from simplequant.engine.optimize import default_workers
from simplequant.stocks import FACTORS, GROUPS, REBALANCE, DEFAULT_FILTERS, factors_for
from simplequant.stocks import ml as ML
from simplequant.stocks import universe as U
from simplequant.stocks.selection import panel_warmup_days
from simplequant.stocks.panel import STOCK_FILTERS, RISK_FILTERS
from simplequant.bonds.panel import CB_FILTERS, CB_DEFAULT_FILTERS
from simplequant.stocks.walkforward import TARGET_METRICS as WF_TARGETS
from gui import state
from gui.sel_panel import get_panel
from gui.common import t, p
from ui.shared import clean

DENSE = "outlined dense options-dense"
WARMUP_DAYS = 400          # 面板往前多取约 400 天，只用来预热因子；研究与回测从所选开始日起算（机器学习另取更长的面板）
SEL_DEFAULT = [{"key": "ep", "weight": 1.0, "direction": 1}, {"key": "ret20", "weight": 1.0, "direction": -1}]
CB_SEL_DEFAULT = [{"key": "cb_double_low", "weight": 1.0, "direction": -1}]       # 可转债「双低」
# 股票与可转债各自记住的表单项（切换股票池时互换）
KIND_KEYS = ("factors", "excl_st", "min_list", "dividend", "ni", "ns")
POS_DEFAULT = 95           # 买入资金比例（%）的默认值


def sp_state() -> dict:
    sp = state.STATE.setdefault("sp", {
        "universe": "hs300", "ranges": {},
        # 选股回测表单
        "factors": [dict(f) for f in SEL_DEFAULT], "weighting": "manual", "lookback": 252, "ni": False, "ns": False,
        "top_n": 10, "reb": "monthly", "reb_n": 20, "skip": [], "excl_st": True,
        "min_list": DEFAULT_FILTERS["min_list_days"], "pos": POS_DEFAULT, "name": "", "follow": False, "dividend": "reinvest",
        "ai_text": "", "ai": None, "ai_base": False, "result": None, "explanation": None,
        # 因子研究
        "res": {"fkey": "ep", "horizon": 20, "groups": 5, "min_days": DEFAULT_FILTERS["min_list_days"],
                "ni": False, "ns": False},
        "report": None, "overview": None, "corr": None, "corr_keys": None,
        # 下载
        "dl": {"start": "2019-01-01", "workers": 4, "with_fin": True, "with_div": True}, "pack": None,
        # 滚动优化
        "wf_cfg": {"chosen": ["top_n", "rebalance"], "ranges": {}, "reb": None, "metric": WF_TARGETS[0],
                   "workers": default_workers(), "train": 24, "test": 6, "window": "rolling"},
        "wf": None,
        # 自定义因子编辑区（key 为空 = 新建）
        "cf": {"key": None, "name": "", "code": "", "direction": 1, "desc": ""}, "cf_trial": None,
    })
    sp.setdefault("cf_ai", {"text": "", "base": False, "result": None})
    sp.setdefault("keep_n", None)                   # 调仓缓冲区：保留前 M 名（None = 不设）
    sp.setdefault("market", empty_market())         # 大盘条件（界面里的条件带 _id）
    sp.setdefault("dd_stop", 0)                     # 净值回撤停手阈值（%）
    # 可转债的选股条件（价格上限等；None = 不限）；旧版保存的界面状态里没有这两项
    sp.setdefault("cbf", {k: v for k, v in CB_DEFAULT_FILTERS.items() if k in CB_FILTERS})
    sp.setdefault("stockf", {k: None for k in STOCK_FILTERS})    # 股票的价格/市值条件（None = 不限）
    sp.setdefault("riskf", {k: None for k in RISK_FILTERS})      # 股票的排雷条件（None = 不限）
    sp["dl"].setdefault("with_risk", False)
    sp.setdefault("forms", {})
    sp.setdefault("ml_model", ML.DEFAULT_MODEL)
    sp.setdefault("ml_window", ML.DEFAULT_WINDOW)
    sp.setdefault("ml_params", {m: dict(v) for m, v in ML.DEFAULTS.items()})
    sp.setdefault("ai_base", False)
    sp.setdefault("explanation", None)
    sp["dl"].setdefault("cb_start", "2017-01-01")
    return sp


def kind_defaults(kind: str) -> dict:
    if kind == "cb":
        return {"factors": [dict(f) for f in CB_SEL_DEFAULT], "excl_st": False,
                "min_list": CB_DEFAULT_FILTERS["min_list_days"], "dividend": "reinvest", "ni": False, "ns": False}
    return {"factors": [dict(f) for f in SEL_DEFAULT], "excl_st": True, "min_list": DEFAULT_FILTERS["min_list_days"],
            "dividend": "reinvest", "ni": False, "ns": False}


def empty_market() -> dict:
    return {"sell": {"logic": "any", "conditions": []}, "buy": {"logic": "all", "conditions": []}}


def market_spec(SP) -> dict | None:
    """表单里的大盘条件；没有空仓条件且未设回撤停手恢复时为 None"""
    m = SP.get("market") or {}
    has_sell = bool((m.get("sell") or {}).get("conditions"))
    has_buy = bool((m.get("buy") or {}).get("conditions"))
    has_dd = float(SP.get("dd_stop") or 0) > 0
    if not has_sell and not (has_buy and has_dd):
        return None
    out = clean({"buy": m.get("buy") or {"logic": "all", "conditions": []},
                 "sell": m.get("sell") or {"logic": "any", "conditions": []}})
    res = {}
    if out["sell"]["conditions"]:
        res["sell"] = out["sell"]
    if out["buy"]["conditions"]:
        res["buy"] = out["buy"]
    return res or None


def switch_kind(SP, old: str, new: str):
    """股票池在股票与可转债之间切换：各自的因子、条件分开记住"""
    if old == new:
        return
    SP.setdefault("forms", {})[old] = {k: SP[k] for k in KIND_KEYS if k in SP}
    SP.update(SP["forms"].get(new) or kind_defaults(new))
    usable = factors_for(new)
    if SP["res"]["fkey"] not in usable:
        SP["res"]["fkey"] = usable[0]
    SP["res"]["min_days"] = SP["min_list"]
    SP["corr_keys"] = None


def prune_factors(SP):
    """自定义因子保存 / 删除后：回测因子列表、因子研究里已不存在的因子去掉（都没了就回到默认因子）"""
    kind = U.kind(SP["universe"])
    SP["factors"] = [f for f in SP["factors"] if f["key"] in factors_for(kind)] or kind_defaults(kind)["factors"]
    if SP["res"]["fkey"] not in factors_for(kind):
        SP["res"]["fkey"] = factors_for(kind)[0]


def factor_label(k: str) -> str:
    return f"{p(GROUPS[FACTORS[k]['group']])} · {p(FACTORS[k]['label'])}"


def reb_label(r) -> str:
    return p(REBALANCE[r]) if r in REBALANCE else t("sel.every_n", n=r)


def spec_panel_start(SP, ctx, spec: dict) -> str:
    """
    回测这个策略用的面板起始日：一般就是 ctx.panel_start；
    机器学习要容下训练窗口、IC 加权的回看期超过 252 天时，往前取得更多（有数据的话），导入的方案按方案记录的预热起始日
    """
    if panel_warmup_days(spec) == WARMUP_DAYS:
        return ctx.panel_start
    pw = SP.get("plan_warm")
    if pw and pw[0] == SP["universe"] and pw[1] == ctx.start:
        return max(str(ctx.lo), pw[2])
    return max(str(ctx.lo), str((pd.Timestamp(ctx.start) - pd.Timedelta(days=panel_warmup_days(spec))).date()))


async def spec_panel(SP, ctx, spec: dict):
    ps = spec_panel_start(SP, ctx, spec)
    if ps == ctx.panel_start:
        return ctx.panel, ps
    return await run.io_bound(get_panel, SP["universe"], ps, ctx.end), ps


class Ctx:
    """页面内共享：当前面板、区间、各标签页的重画函数"""
    panel = None
    start = end = panel_start = None
    lo = hi = None
