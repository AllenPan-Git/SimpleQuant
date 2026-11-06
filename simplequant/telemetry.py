"""
匿名使用统计 + 反馈（待办 57）。接收端：SimpleQuant-site/worker（https://api.simplequant.org）

- 本机按天累计到 data_cache/telemetry.json；用户同意后，启动时最多每天发一次（联网失败留到下次）
- 只收：随机安装编号、版本、系统、界面语言、新版 / 经典界面；里程碑（距安装天数）；按天的计数；报错类型与位置
- 一律不收：策略内容、股票代码、资金、回测结果、文件路径、API key。计数键、报错位置在这里和服务器各校验一次
- 没调用 activate() 时（界面测试、截图、--paper、打包版自动测试）所有记录都是空操作，也不会联网

同意状态 consent：None = 还没问过（只在本机累计，不发送）；True = 同意；False = 不同意（清空已累计的，之后不再记录）
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import traceback
import uuid
from pathlib import Path

from simplequant import __version__
from simplequant.paths import CACHE_DIR
from simplequant.system import SYSTEM

ENDPOINT = os.environ.get("SIMPLEQUANT_TELEMETRY_URL", "https://api.simplequant.org")
PATH = CACHE_DIR / "telemetry.json"
TIMEOUT = 10
MAX_DAYS = 30          # 与服务器 LIMITS 一致
MAX_KEYS = 200
MAX_ERRORS = 50
FEEDBACK_CHARS = 2000

# 与服务器 validate.js 相同的格式：路径、中文、空格都进不来
KEY_RE = re.compile(r"^[a-z0-9_:/.\-]{1,64}$")
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
ERR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
LOC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
OUR_PACKAGES = ("simplequant", "gui", "ui")

_lock = threading.Lock()
_state: dict | None = None      # None = 未启用
_ui_info = {"lang": "zh", "ui": "new"}


def _today() -> str:
    return dt.date.today().isoformat()


# ---------------- 状态与存储 ----------------
def activate(existing_user: bool = False, path: Path | None = None, disabled: bool | None = None) -> bool:
    """gui.app.run 启动真实界面时调用一次。existing_user：本机已有数据或报告（升级上来的老用户），
    里程碑记 upgrade 而不是 first_launch，免得把老用户算成新用户"""
    global _state, PATH
    if disabled is None:
        disabled = os.environ.get("SIMPLEQUANT_TELEMETRY") == "0" or bool(os.environ.get("SIMPLEQUANT_UI_SCRIPT"))
    if disabled:
        return False
    if path is not None:
        PATH = path
    with _lock:
        _state = _load()
        if not _state.get("install_id"):
            _state = {"install_id": str(uuid.uuid4()), "installed": _today(), "consent": None,
                      "milestones": {}, "days": {}, "last_sent": None}
            _state["milestones"]["upgrade" if existing_user else "first_launch"] = 0
            _save()
    return True


def deactivate():
    """测试用"""
    global _state
    _state = None


def active() -> bool:
    return _state is not None


def _load() -> dict:
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save():
    """先写临时文件再替换，写到一半关机也不会留下坏文件"""
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PATH)
    except OSError:
        pass


def consent() -> bool | None:
    return _state.get("consent") if _state else None


def set_consent(value: bool):
    """首页询问或设置页开关。不同意时清空本机已累计的计数和报错（里程碑也清掉）"""
    if not _state:
        return
    with _lock:
        _state["consent"] = bool(value)
        if not value:
            _state["days"] = {}
            _state["milestones"] = {}
        _save()


def set_ui_info(lang: str, ui: str):
    """界面语言、新版 / 经典（页面打开时更新；上报时用最近一次的）"""
    if lang in ("zh", "en"):
        _ui_info["lang"] = lang
    if ui in ("new", "classic"):
        _ui_info["ui"] = ui


# ---------------- 记录 ----------------
def _recording() -> bool:
    return _state is not None and _state.get("consent") is not False


def _day() -> dict:
    return _state["days"].setdefault(_today(), {"counts": {}, "errors": {}})


def count(key: str, n: int = 1):
    """按天计数，如 page:/timing、btn:backtest。键不合规时忽略（不报错，不影响使用）"""
    if not _recording() or not KEY_RE.match(key):
        return
    with _lock:
        c = _day()["counts"]
        if key not in c and len(c) >= MAX_KEYS:
            return
        c[key] = c.get(key, 0) + n
        _save()


def milestone(name: str):
    """第一次做某件事：记距安装的天数，之后不再改"""
    if not _recording() or not NAME_RE.match(name) or name in _state["milestones"]:
        return
    with _lock:
        installed = dt.date.fromisoformat(_state["installed"])
        _state["milestones"][name] = max(0, (dt.date.fromisoformat(_today()) - installed).days)
        _save()


def ai(kind: str):
    """用了一次 AI 功能：strategy / selection / factor / mining / explain"""
    count(f"ai:{kind}")
    milestone("ai")


def _clean_ident(s: str, pattern: re.Pattern) -> str | None:
    s = re.sub(r"[^A-Za-z0-9_.]", "_", s).strip("_.")
    return s if s and pattern.match(s) else None


def error_location(exc: BaseException) -> str:
    """出错位置：调用栈里最深的一层本项目代码（模块.函数）；不含文件路径和行号"""
    loc = "unknown"
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        mod = frame.f_globals.get("__name__", "")
        if mod.split(".")[0] in OUR_PACKAGES:
            loc = f"{mod}.{frame.f_code.co_name}"
    return _clean_ident(loc, LOC_RE) or "unknown"


def error(exc: BaseException):
    """记一次报错：只有异常类名和出错位置，不记错误消息（消息里可能有路径、股票代码）"""
    if not _recording():
        return
    etype = _clean_ident(type(exc).__name__, ERR_TYPE_RE) or "Exception"
    key = f"{etype}|{error_location(exc)}"
    with _lock:
        e = _day()["errors"]
        if key not in e and len(e) >= MAX_ERRORS:
            return
        e[key] = e.get(key, 0) + 1
        _save()


# ---------------- 上报 ----------------
def payload() -> dict | None:
    """要发送的内容（设置页「查看会发送什么」显示的就是它）。发送最近 MAX_DAYS 天里还没发完的日子：
    上次发送那天当时只发了一部分，这次连同完整数字重发一遍，服务器按天覆盖，不会重复计数"""
    if not _state:
        return None
    last = _state.get("last_sent") or ""
    days = sorted(d for d in _state["days"] if d >= last)[-MAX_DAYS:] or [_today()]
    out_days = []
    for d in days:
        rec = _state["days"].get(d, {"counts": {}, "errors": {}})
        errors = [{"type": k.split("|")[0], "location": k.split("|")[1], "n": n} for k, n in rec["errors"].items()]
        out_days.append({"day": d, "counts": dict(rec["counts"]), "errors": errors})
    return {"install_id": _state["install_id"], "version": __version__, "os": SYSTEM,
            "lang": _ui_info["lang"], "ui": _ui_info["ui"],
            "milestones": dict(_state["milestones"]), "days": out_days}


def due() -> bool:
    return bool(_state) and _state.get("consent") is True and _state.get("last_sent") != _today()


def send(post=None) -> bool:
    """同意了且今天还没发过时发送一次；成功后删掉今天以前的日子（已按完整数字发出）。失败什么都不改，下次再发"""
    if not due():
        return False
    body = payload()
    if post is None:
        import requests
        post = requests.post
    try:
        r = post(f"{ENDPOINT}/v1/ping", json=body, timeout=TIMEOUT,
                 headers={"User-Agent": f"SimpleQuant/{__version__}"})
        ok = r.status_code == 204
    except Exception:  # noqa: BLE001  网络问题一律静默
        return False
    if ok:
        today = _today()
        with _lock:
            _state["last_sent"] = today
            _state["days"] = {d: v for d, v in _state["days"].items() if d >= today}
            _save()
    return ok


def send_async(delay: float = 5):
    """启动后几秒在后台发送，不拖慢打开界面"""
    t = threading.Timer(delay, send)
    t.daemon = True
    t.start()


def send_feedback(text: str, post=None) -> str:
    """提交反馈，与是否同意统计无关（是用户主动发的）。不附安装编号、不收联系方式。
    返回 "ok" / "empty" / "too_long" / "rate_limited" / "failed\""""
    text = (text or "").strip()
    if not text:
        return "empty"
    if len(text) > FEEDBACK_CHARS:
        return "too_long"
    body = {"version": __version__, "os": SYSTEM, "lang": _ui_info["lang"], "ui": _ui_info["ui"], "text": text}
    if post is None:
        import requests
        post = requests.post
    try:
        r = post(f"{ENDPOINT}/v1/feedback", json=body, timeout=TIMEOUT,
                 headers={"User-Agent": f"SimpleQuant/{__version__}"})
    except Exception:  # noqa: BLE001
        return "failed"
    return "ok" if r.status_code == 204 else "rate_limited" if r.status_code == 429 else "failed"
