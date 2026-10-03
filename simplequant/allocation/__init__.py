"""
风险偏好与资产配置：风险测评 → 参考配置 → 组合回测

    data_cache/allocation/profile.json   最近一次风险测评结果
    data_cache/allocation/config.json    自定义候选、选用的候选、再平衡方式
"""

import json

import pandas as pd

from ..paths import CACHE_DIR
from .questionnaire import QUESTIONS, LEVELS, LEVEL_DESC, MIN_SCORE, MAX_SCORE, Profile, evaluate, preset_profile
from .sleeves import (CLASSES, KINDS, DEFAULT_SLEEVES, sleeve_returns, risk_stats, risk_grade, missing_data,
                      download, find_dataset)
from .portfolio import REBALANCE, PortfolioResult, backtest, common_returns
from .allocate import CLASS_WEIGHTS, suggest, checks, sleeve_table

ROOT = CACHE_DIR / "allocation"


def load_profile(root=None) -> Profile | None:
    p = (root or ROOT) / "profile.json"
    try:
        return Profile(**json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save_profile(profile: Profile, root=None):
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True)
    (root / "profile.json").write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(root=None) -> dict:
    p = (root or ROOT) / "config.json"
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    cfg.setdefault("custom", [])                                   # 用户添加的候选
    cfg.setdefault("enabled", [s["id"] for s in DEFAULT_SLEEVES])  # 选用的候选 id
    cfg.setdefault("rebalance", "quarterly")
    return cfg


def save_config(cfg: dict, root=None):
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def all_sleeves(cfg: dict) -> list[dict]:
    return DEFAULT_SLEEVES + list(cfg.get("custom", []))


def load_returns(sleeves: list[dict], progress=None) -> tuple[pd.DataFrame, dict]:
    """各候选的收益率，对齐到共同区间；返回 (收益率表, {候选 id: 错误信息})，出错的候选不参与"""
    series, errors = {}, {}
    others = [s for s in sleeves if s["kind"] != "cash"]
    for i, s in enumerate(others, 1):
        try:
            series[s["id"]] = sleeve_returns(s)
        except Exception as e:  # noqa: BLE001 - 单个候选失败不影响其他候选
            errors[s["id"]] = str(e)
        if progress:
            progress(i, len(others))
    if series:
        calendar = pd.DatetimeIndex(sorted(set().union(*[set(x.index) for x in series.values()])))
        calendar = calendar.insert(0, calendar[0] - pd.Timedelta(days=1))
    else:
        from ..paper.calendar import load_calendar
        calendar = load_calendar()
        calendar = calendar[calendar <= pd.Timestamp.today()][-5 * 252:]
    for s in sleeves:
        if s["kind"] == "cash":
            series[s["id"]] = sleeve_returns(s, calendar)
    if not series:
        raise ValueError("no usable sleeves / 没有可用的配置成分")
    return common_returns(series), errors
