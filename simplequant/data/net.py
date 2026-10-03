"""
外部行情网站的访问控制：限速与暂停

- 限速：新浪对频繁抓取会封 IP，所有线程合计每秒最多 4 次（可转债日线、ETF / 股票 / 指数日线共用）
- 暂停：某个网站重试后仍连不上（东方财富会按 IP 直接断开连接，新浪封 IP 时返回 456），
  之后一段时间内不再访问，直接用备用接口。既省去每次重试的等待，也避免持续请求延长封禁
"""

import threading
import time
from http.client import HTTPException

from ..i18n import L, pick

INTERVAL = {"sina": 0.25}
PAUSE = 15 * 60
SITE_NAMES = {"eastmoney": L("东方财富", "Eastmoney"), "sina": L("新浪财经", "Sina Finance"),
              "csindex": L("中证指数官网", "CSI website")}

_lock = threading.Lock()
_last: dict[str, float] = {}
_paused: dict[str, tuple[float, str]] = {}      # 网站 -> (恢复时间, 原因)


class Blocked(ConnectionError):
    """网站拒绝访问（如新浪的 HTTP 456），不必重试"""


class NotFound(LookupError):
    """网站上没有这个代码（HTTP 404），不必重试"""


class Paused(RuntimeError):
    """网站近期连不上，暂停访问中"""

    def __init__(self, site: str, until: float, reason: str):
        self.site, self.until, self.reason = site, until, reason
        minutes = max(1, round((until - time.time()) / 60))
        name = SITE_NAMES.get(site, {"zh": site, "en": site})
        super().__init__(f"{pick(name, 'en')} is unreachable; paused for about {minutes} min / "
                         f"{pick(name, 'zh')}近期无法连接，约 {minutes} 分钟内暂停访问（{reason}）")


def throttle(site: str):
    gap = INTERVAL.get(site)
    if not gap:
        return
    with _lock:
        wait = _last.get(site, 0.0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[site] = time.time()


def paused(site: str) -> Paused | None:
    with _lock:
        until, reason = _paused.get(site, (0.0, ""))
    return Paused(site, until, reason) if until > time.time() else None


def pause(site: str, reason: str, seconds: float = PAUSE):
    with _lock:
        _paused[site] = (time.time() + seconds, reason[:200])


def reset(site: str | None = None):
    with _lock:
        if site:
            _paused.pop(site, None)
        else:
            _paused.clear()


def is_network_error(err: BaseException | None) -> bool:
    """连接被断开 / 超时 / 服务器错误。数据解析失败、代码不存在等不算（不重试，也不应暂停整个网站）"""
    import requests
    if isinstance(err, requests.HTTPError):
        return err.response is None or err.response.status_code >= 500
    return isinstance(err, (OSError, HTTPException))      # requests 的其他异常都是 OSError 的子类


def call(site: str, fn, retries: int = 3, wait: float = 1.5):
    """
    访问一个网站：暂停中直接抛 Paused；每次请求前限速；只有网络错误才重试，重试后仍失败或被封则暂停该网站。
    其他错误（代码不存在、返回内容无法解析）原样抛出
    """
    if p := paused(site):
        raise p
    err = None
    for i in range(retries):
        throttle(site)
        try:
            return fn()
        except Blocked as e:            # 已被封：重试只会延长封禁
            pause(site, str(e))
            raise
        except Exception as e:  # noqa: BLE001 - 数据接口的异常类型五花八门
            if not is_network_error(e):
                raise
            err = e
            if i < retries - 1:
                time.sleep(wait)
    pause(site, str(err))
    raise RuntimeError(f"Download failed after {retries} retries / 数据获取失败（已重试 {retries} 次）: {err}") from err
