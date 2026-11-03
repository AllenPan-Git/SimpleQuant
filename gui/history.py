"""
最近的回测报告（首页「继续上次的工作」「最近的报告」用）
存在 app.storage.general（data_cache/gui_storage/），重启程序后保留；只存摘要和重新运行所需的设置，不存结果本身。
"""

import datetime as dt
import json
import math

from nicegui import app

MAX_ITEMS = 8


def recent() -> list[dict]:
    return list(app.storage.general.get("recent", []))


def _clean(v):
    return None if isinstance(v, float) and math.isnan(v) else v


def add(kind: str, title: str, sub: str, metrics: dict, restore: dict | None = None, **extra):
    """kind: "bt"（择时回测）/ "sel"（选股回测）；restore：重新打开时要恢复的界面状态（必须能存成 JSON）"""
    entry = {"kind": kind, "title": title, "sub": sub, "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "ret": _clean(metrics.get("total_return")), "sharpe": _clean(metrics.get("sharpe")),
             "dd": _clean(metrics.get("max_drawdown")), **extra}
    if restore is not None:
        try:
            json.dumps(restore)
            entry["restore"] = restore
        except (TypeError, ValueError):
            pass
    same = (kind, title, sub)
    items = [e for e in recent() if (e.get("kind"), e.get("title"), e.get("sub")) != same]
    app.storage.general["recent"] = [entry] + items[:MAX_ITEMS - 1]


def mark_verify(kind: str, spec: dict, **done: bool):
    """记下这个策略做过的检验（如 optimize=True），记在最近一条同类、同规则的报告上；
    首页「继续上次的工作」重启后据此显示进度的「验证」一步。没有对应的报告时不记"""
    from ui.shared import spec_key
    key, items = spec_key(spec), recent()
    for i, e in enumerate(items):
        rs = (e.get("restore") or {}).get("spec")
        if e.get("kind") == kind and rs and spec_key(rs) == key:
            items[i] = {**e, "verify": {**(e.get("verify") or {}), **done}}
            app.storage.general["recent"] = items
            return


def clear():
    app.storage.general["recent"] = []
