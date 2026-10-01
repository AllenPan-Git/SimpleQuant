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


def clear():
    app.storage.general["recent"] = []
