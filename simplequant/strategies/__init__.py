"""
策略入口
策略统一用一个"策略描述"字典表示，既能保存成 JSON，也能直接拿去回测：
    {"kind": "template", "template": "sma_cross", "params": {"fast": 5, "slow": 20, "position_pct": 95}}
    {"kind": "rule", "rule": {...}}
    {"kind": "code", "code": "class Strategy(PerAsset): ...", "params": {...}}   （见 code_strategy.py）
"""

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .registry import TEMPLATES, Template, Param
from . import templates as _templates  # noqa: F401  注册内置模板
from .rule_strategy import RuleStrategy
from . import code_strategy
from ..i18n import pick, tr
from ..rules import describe_rule, describe_ind, INDICATORS
from ..paths import STRATEGY_DIR

STORE_DIR = STRATEGY_DIR


def resolve(spec: dict):
    """策略描述 -> (策略类, 参数)"""
    if spec.get("kind") == "code":
        return code_strategy.resolve(spec)
    if spec.get("kind") == "rule":
        rule = spec["rule"]
        return RuleStrategy, {"rule": rule, "position_pct": rule.get("position_pct", 95)}
    tpl = TEMPLATES[spec["template"]]
    return tpl.cls, {**tpl.defaults(), **(spec.get("params") or {})}


def describe(spec: dict, lang: str = "zh") -> str:
    if spec.get("kind") == "code":
        return code_strategy.describe(spec, lang)
    if spec.get("kind") == "rule":
        return describe_rule(spec["rule"], lang)
    if spec.get("kind") == "selection":
        return describe_selection(spec, lang)
    tpl = TEMPLATES[spec["template"]]
    params = {**tpl.defaults(), **(spec.get("params") or {})}
    sep = "，" if lang == "zh" else ", "
    return (f"【{tr('rule.template', lang)}】{pick(tpl.label, lang)}\n"
            + sep.join(f"{pick(p.label, lang)}={params[p.name]:g}" for p in tpl.params))


def describe_selection(spec: dict, lang: str = "zh") -> str:
    from ..stocks import FACTORS, UNIVERSES, REBALANCE, WEIGHTING   # 延迟导入，避免循环依赖
    reb = spec.get("rebalance", "monthly")
    reb_text = pick(REBALANCE[reb], lang) if reb in REBALANCE else tr("sel.every_n", lang, n=reb)
    sep = "、" if lang == "zh" else ", "
    weighting = spec.get("weighting", "manual")
    if weighting == "manual":
        facs = sep.join(
            tr("sel.factor_item", lang, name=pick(FACTORS.get(f["key"], {}).get("label", f["key"]), lang),
               dir=tr("sel.dir_up" if f.get("direction", 1) > 0 else "sel.dir_down", lang), w=f"{f.get('weight', 1):g}")
            for f in spec["factors"])
    else:   # IC 加权：方向和权重由历史 IC 自动决定，只列因子名
        names = sep.join(pick(FACTORS.get(f["key"], {}).get("label", f["key"]), lang) for f in spec["factors"])
        how = pick(WEIGHTING[weighting], lang)
        facs = f"{names}（{how}）" if lang == "zh" else f"{names} ({how})"
    neutral = [tr(f"sel.neutral_{k}", lang) for k, v in (spec.get("neutralize") or {}).items() if v]
    text = (f"【{tr('sel.kind', lang)}】{pick(UNIVERSES[spec['universe']]['label'], lang)} · {reb_text} · "
            + tr("sel.top_n", lang, n=spec["top_n"]) + f"\n【{tr('sel.factors', lang)}】{facs}")
    if neutral:
        text += f"\n【{tr('sel.neutral', lang)}】" + sep.join(neutral)
    if spec.get("dividend") == "cash":
        text += " · " + tr("sel.div_cash", lang)
    return text


def runnable_on_single_assets(spec: dict) -> bool:
    """模板/规则/代码策略可以在单标的回测和参数优化里用；选股策略只能在选股页用"""
    return spec.get("kind") in ("rule", "template", "code")


def min_assets(spec: dict) -> int:
    if spec.get("kind") == "code":
        return code_strategy.min_assets(spec)
    return TEMPLATES[spec["template"]].min_assets if spec.get("kind") == "template" else 1


def is_valid_combo(spec: dict) -> bool:
    """参数组合是否有意义（模板声明的约束）"""
    if spec.get("kind") != "template":
        return True
    tpl = TEMPLATES[spec["template"]]
    return tpl.constraint is None or tpl.constraint({**tpl.defaults(), **(spec.get("params") or {})})


# ---------- 可调参数（参数优化用） ----------
@dataclass
class Tunable:
    path: str            # 在策略描述里的位置，如 "params.fast" / "rule.buy.conditions.0.left.params.period"
    label: str
    value: float
    is_int: bool
    min: float | None = None
    max: float | None = None


def tunables(spec: dict, lang: str = "zh") -> list[Tunable]:
    out = []
    if spec.get("kind") == "template":
        tpl = TEMPLATES[spec["template"]]
        params = {**tpl.defaults(), **(spec.get("params") or {})}
        for p in tpl.params:
            out.append(Tunable(f"params.{p.name}", pick(p.label, lang), params[p.name], p.is_int, p.min, p.max))
        return out

    if spec.get("kind") == "code":       # 代码里 params 声明的数值参数
        cls, params = code_strategy.resolve(spec)
        for k, v in params.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = (10, 100) if k == "position_pct" else (None, None)
                out.append(Tunable(f"params.{k}", k, v, isinstance(v, int), lo, hi))
        return out

    rule = spec["rule"]
    for side in ("buy", "sell"):
        for i, c in enumerate((rule.get(side) or {}).get("conditions", [])):
            prefix = f"{tr('rule.' + side, lang)}{'' if lang == 'zh' else ' #'}{i + 1}"
            for pos in ("left", "right"):
                s = c.get(pos) or {}
                base = f"rule.{side}.conditions.{i}.{pos}"
                if "value" in s:
                    what = "数值" if lang == "zh" else "value"
                    out.append(Tunable(f"{base}.value", f"{prefix} · {what}", s["value"], False))
                    continue
                for name, plabel, default in INDICATORS.get(s.get("ind"), {}).get("params", []):
                    v = (s.get("params") or {}).get(name, default)
                    out.append(Tunable(f"{base}.params.{name}",
                                       f"{prefix} · {describe_ind(s, lang)} · {pick(plabel, lang)}",
                                       v, isinstance(default, int), min=1 if isinstance(default, int) else 0.1))
    out.append(Tunable("rule.position_pct", tr("rule.position", lang) + " (%)",
                       rule.get("position_pct", 95), True, 10, 100))
    return out


def set_path(spec: dict, path: str, value) -> dict:
    """返回修改了 path 处数值的新策略描述"""
    new = copy.deepcopy(spec)
    keys = path.split(".")
    node = new
    for k in keys[:-1]:
        node = node[int(k)] if isinstance(node, list) else node.setdefault(k, {})
    node[keys[-1]] = value
    return new


# ---------- 保存的策略 ----------
def _path(name: str, store_dir: Path) -> Path:
    return store_dir / (re.sub(r'[\\/:*?"<>|]+', "_", name.strip()) + ".json")


def save_strategy(name: str, spec: dict, store_dir: Path = STORE_DIR) -> Path:
    store_dir.mkdir(parents=True, exist_ok=True)
    path = _path(name, store_dir)
    path.write_text(json.dumps({"name": name, **spec}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_strategies(store_dir: Path = STORE_DIR) -> dict[str, dict]:
    if not store_dir.exists():
        return {}
    out = {}
    for p in sorted(store_dir.glob("*.json")):
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
            out[spec.pop("name", p.stem)] = spec
        except json.JSONDecodeError:
            continue
    return out


def delete_strategy(name: str, store_dir: Path = STORE_DIR) -> None:
    _path(name, store_dir).unlink(missing_ok=True)
