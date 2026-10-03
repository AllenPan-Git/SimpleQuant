"""
参考配置：风险等级 → 大类比例 → 大类内部按风险平价分配 → 按可承受回撤调整

1. 大类比例：按风险等级查表（CLASS_WEIGHTS）；候选中缺少的大类，其比例按其余大类原比例分摊
2. 大类内部：按波动率倒数分配（简化的风险平价）。不使用均值-方差优化：它对预期收益的估计误差极为敏感
3. 回撤约束：组合在历史区间的最大回撤超过可承受回撤时，每次将 5% 的权重从风险最高的一档
   （权益类与其他）移到更稳健的一档（债券类，其次现金类），直到满足约束或无法再调整
"""

import numpy as np
import pandas as pd

from . import portfolio as pf
from .sleeves import risk_stats

CLASS_WEIGHTS = {
    1: {"cash": 0.40, "bond": 0.60, "equity": 0.00, "alt": 0.00},
    2: {"cash": 0.20, "bond": 0.55, "equity": 0.15, "alt": 0.10},
    3: {"cash": 0.10, "bond": 0.45, "equity": 0.30, "alt": 0.15},
    4: {"cash": 0.05, "bond": 0.30, "equity": 0.45, "alt": 0.20},
    5: {"cash": 0.00, "bond": 0.15, "equity": 0.65, "alt": 0.20},
}
TIERS = (("equity", "alt"), ("bond",), ("cash",))     # 由高风险到低风险
STEP = 0.05
MAX_STEPS = 60


def class_weights(level: int, available: set[str]) -> dict[str, float]:
    base = CLASS_WEIGHTS[level]
    have = {c: v for c, v in base.items() if c in available}
    total = sum(have.values())
    if total <= 0:          # 例如保守型但只有权益类候选：各大类均分
        return {c: 1 / len(available) for c in available}
    return {c: v / total for c, v in have.items()}


def inner_weights(returns: pd.DataFrame, sleeves: list[dict]) -> dict[str, float]:
    """每个成分在所属大类中的比例（波动率倒数）"""
    vol = returns.std()
    out = {}
    for cls in {s["class"] for s in sleeves}:
        ids = [s["id"] for s in sleeves if s["class"] == cls]
        inv = {i: 1 / max(float(vol[i]), 1e-6) if vol[i] == vol[i] else 1.0 for i in ids}
        tot = sum(inv.values())
        out.update({i: v / tot for i, v in inv.items()})
    return out


def combine(cw: dict[str, float], inner: dict[str, float], sleeves: list[dict]) -> dict[str, float]:
    return {s["id"]: cw.get(s["class"], 0.0) * inner[s["id"]] for s in sleeves}


def _shift(cw: dict[str, float], base: dict[str, float]) -> float:
    """把至多 STEP 的权重从有权重的最高风险档移到更低风险的档；返回移动的权重（无法移动时为 0）"""
    for i, tier in enumerate(TIERS):
        src = {c: cw[c] for c in tier if cw.get(c, 0) > 1e-9}
        if not src or not any(c in cw for t in TIERS[i + 1:] for c in t):
            continue
        amount = min(STEP, sum(src.values()))
        tot = sum(src.values())
        for c, v in src.items():
            cw[c] = v - amount * v / tot
        # 先移入紧邻的更低一档；按该档各大类的基础比例分摊（基础比例为 0 时均分）
        nxt = next([c for c in t if c in cw] for t in TIERS[i + 1:] if any(c in cw for c in t))
        weights = {c: base.get(c, 0.0) for c in nxt}
        wt = sum(weights.values())
        for c in nxt:
            cw[c] += amount * (weights[c] / wt if wt > 0 else 1 / len(nxt))
        return amount
    return 0.0


def suggest(level: int, max_dd: float, sleeves: list[dict], returns: pd.DataFrame,
            rebalance: str = "quarterly") -> dict:
    """
    :return: {"weights": {成分 id: 权重}, "class_weights": ..., "base_class_weights": ..., "shifted": 移动的权重合计,
              "result": PortfolioResult, "within_limit": 是否满足回撤约束}
    """
    available = {s["class"] for s in sleeves}
    base = class_weights(level, available)
    cw = dict(base)
    inner = inner_weights(returns, sleeves)
    res = pf.backtest(returns, combine(cw, inner, sleeves), rebalance)
    shifted = 0.0
    for _ in range(MAX_STEPS):
        if abs(res.metrics["max_drawdown"]) <= max_dd:
            break
        moved = _shift(cw, base)
        if not moved:
            break
        shifted += moved
        res = pf.backtest(returns, combine(cw, inner, sleeves), rebalance)
    return {"weights": combine(cw, inner, sleeves), "class_weights": cw, "base_class_weights": base,
            "shifted": round(shifted, 4), "result": res,
            "within_limit": abs(res.metrics["max_drawdown"]) <= max_dd}


def checks(profile_level: int, max_dd: float, res: pf.PortfolioResult, shifted: float = 0.0,
           names: dict | None = None) -> list[dict]:
    """组合层面的提示，格式与 engine/credibility.py 相同（args 中的浮点数为比例）；names: {成分 id: 显示名}"""
    out = []
    eq = res.equity
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    if years < 3:
        out.append({"level": "warn", "key": "alloc.short_period", "args": {"years": f"{years:.1f}"}})
    elif years < 7:
        out.append({"level": "note", "key": "alloc.medium_period", "args": {"years": f"{years:.1f}"}})
    else:
        out.append({"level": "ok", "key": "alloc.long_period", "args": {"years": f"{years:.1f}"}})
    dd = abs(res.metrics["max_drawdown"])
    if dd > max_dd:
        out.append({"level": "warn", "key": "alloc.dd_over", "args": {"dd": dd, "limit": max_dd}})
    else:
        out.append({"level": "ok", "key": "alloc.dd_within", "args": {"dd": dd, "limit": max_dd}})
    if shifted > 0:
        out.append({"level": "note", "key": "alloc.shifted", "args": {"w": shifted}})
    risk = res.stats.get("risk", 0)
    if risk > profile_level:
        out.append({"level": "warn", "key": "alloc.risk_over", "args": {"r": f"R{risk}", "c": f"C{profile_level}"}})
    top = res.contrib.loc[res.contrib["risk_contrib"].idxmax()] if len(res.contrib) else None
    if top is not None and len(res.contrib) > 1 and top["risk_contrib"] > 0.6:
        out.append({"level": "note", "key": "alloc.risk_concentrated",
                    "args": {"name": (names or {}).get(top["id"], top["id"]), "w": float(top["weight"]), "rc": float(top["risk_contrib"])}})
    out.append({"level": "note", "key": "alloc.hindsight", "args": {}})
    return out


def sleeve_table(returns: pd.DataFrame) -> pd.DataFrame:
    """各候选在共同区间内的指标"""
    return pd.DataFrame([{"id": c, **risk_stats(returns[c])} for c in returns.columns])
