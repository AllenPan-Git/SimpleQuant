"""
AI 生成规则后的提示（与界面框架无关）
- 每个条件是「状态」（大于 / 小于：每根 K 线都检查，成立就满足）还是「事件」（上穿 / 下穿：只在穿越的那根 K 线成立）
- 用户的描述说「上穿 / 金叉」规则里却全是大于 / 小于（或反过来说「站上 / 高于」规则里却是上穿）时提醒
- 同一段描述多次生成的规则不同：说明描述有歧义
"""

import json

from .schema import CROSS_OPS, describe_condition

# 只收「穿越当天」的说法；「突破 N 日新高」按提示词本来就译成大于，不算
CROSS_WORDS = ("上穿", "下穿", "金叉", "死叉", "交叉", "穿越", "cross", "golden", "death")
STATE_WORDS = ("站上", "站在", "位于", "高于", "低于", "大于", "小于", "之上", "之下", "以上", "以下",
               "above", "below", "greater", "less", "higher than", "lower than")


def _conditions(rule: dict):
    for side in ("buy", "sell"):
        for c in (rule.get(side) or {}).get("conditions") or []:
            yield side, c


def _compares_lines(c: dict) -> bool:
    """两边都是指标（不是和固定数值比）：这时「大于」和「上穿」的区别才明显"""
    return "value" not in c.get("right", {}) and "value" not in c.get("left", {})


def condition_notes(rule: dict, lang: str = "zh") -> list[dict]:
    """每个条件一行：side（buy / sell）、text（条件）、kind（event / state）"""
    return [{"side": side, "text": describe_condition(c, lang), "kind": "event" if c.get("op") in CROSS_OPS else "state"}
            for side, c in _conditions(rule)]


def wording_hints(rule: dict, text: str) -> list[str]:
    """描述的说法与规则用的比较方式对不上时的提示（返回界面文字键）"""
    low = (text or "").lower()
    conds = [c for _, c in _conditions(rule)]
    has_cross = any(c.get("op") in CROSS_OPS for c in conds)
    state_lines = any(c.get("op") not in CROSS_OPS and _compares_lines(c) for c in conds)
    out = []
    if any(w in low for w in CROSS_WORDS) and not has_cross and state_lines:
        out.append("ai.hint_cross_as_state")
    if any(w in low for w in STATE_WORDS) and has_cross and not any(w in low for w in CROSS_WORDS):
        out.append("ai.hint_state_as_cross")
    return out


def rule_signature(rule: dict) -> str:
    """比较两次生成的规则是否相同（忽略界面加的 id 和条件顺序）"""
    def side(b):
        b = b or {}
        conds = sorted(json.dumps({k: v for k, v in c.items() if k not in ("id", "_id")}, sort_keys=True)
                       for c in b.get("conditions") or [])
        return {"logic": b.get("logic", "all"), "conditions": conds}
    return json.dumps({"buy": side(rule.get("buy")), "sell": side(rule.get("sell")),
                       "position_pct": rule.get("position_pct", 95)}, sort_keys=True)
