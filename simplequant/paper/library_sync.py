"""
模拟盘更新之后，顺便把本地数据库里在线下载的数据更新到最新（见 data.update_library）。
定时任务与程序窗口里的「立即更新」都走 runner.run_all，在它的文件锁里做，两处不会同时写数据库。
设置页可关，默认开；开关存在 paper/options.json（定时任务没有界面，也要能读到）。
"""

import json

from . import account as _account

OPTIONS = "options.json"


def _path():
    return _account.PAPER_ROOT / OPTIONS


def _read() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def enabled() -> bool:
    return bool(_read().get("update_library", True))


def set_enabled(on: bool) -> None:
    opts = {**_read(), "update_library": bool(on)}
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(opts, ensure_ascii=False, indent=2), encoding="utf-8")


def sync(log=print, progress=None) -> list | None:
    """
    开关打开时更新本地数据库，结果写进日志；关着时返回 None。出错只记日志，不影响模拟盘的结果。
    progress(完成数, 总数)
    """
    if not enabled():
        return None
    from ..data import update_library
    log("updating local database… / 正在更新本地数据库…")
    try:
        res = update_library(progress=(lambda i, n, m: progress(i, n)) if progress else None)
    except Exception as e:  # noqa: BLE001
        log(f"local database update FAILED: {type(e).__name__}: {e} / 本地数据库更新失败")
        return []
    count = {k: sum(s == k for _, s, _ in res) for k in ("updated", "latest", "mismatch", "failed")}
    log(f"local database: {count['updated']} updated, {count['latest']} already up to date, "
        f"{count['mismatch']} not matching, {count['failed']} failed / 本地数据库：更新 {count['updated']} 份，"
        f"已为最新 {count['latest']} 份，价格不一致 {count['mismatch']} 份，失败 {count['failed']} 份")
    for m, status, new in res:
        if status == "mismatch":
            log(f"[{m.name}] prices on {m.end} do not match; not updated, re-download the whole period / "
                f"{m.end} 的新旧价格不一致，未予更新，请重新下载完整区间")
        elif status == "failed":
            log(f"[{m.name}] update FAILED: {new} / 更新失败")
    return res
