"""
界面截图（README 用）：通过 Edge 的调试接口（Chrome DevTools Protocol）打开页面，等后加载的内容出来后再截图。
直接用 msedge --screenshot 会在页面刚加载完时就截，NiceGUI 通过 websocket 后加载的内容是空白的。

    先启动预览服务：.venv\\Scripts\\python tools\\preview_server.py light 8766
    再截图：        .venv\\Scripts\\python tools\\screenshot.py http://127.0.0.1:8766/ 输出.png [宽度] [等待秒数] [高度]
高度不给时截整页。
"""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
PORT = 9333


class CDP:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def __call__(self, method, **params):
        self.n += 1
        my = self.n
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})


CLICK_JS = """(() => {
  const want = %s;
  const el = [...document.querySelectorAll('button, .q-tab, [role=tab]')].find(b => b.innerText.trim().includes(want));
  if (!el) return false;
  el.scrollIntoView({block: 'center'}); el.click(); return true;
})()"""


FIND_JS = """(() => {
  // 文字等于（或以它开头）的最内层可见元素
  const want = %s;
  let best = null, bestLen = Infinity;
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || '').trim();
    if ((t === want || t.startsWith(want)) && t.length <= bestLen && el.getBoundingClientRect().height > 0) {
      best = el; bestLen = t.length;
    }
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  return [r.top + window.scrollY, r.bottom + window.scrollY, r.left];
})()"""


async def shoot(url: str, out: Path, width: int = 1440, wait: float = 6.0, height: int | None = None,
                scale: float = 1.0, clicks: list[tuple[str, float]] = (), crop: tuple | None = None,
                reload: bool = False):
    """
    clicks：截图前依次点击含这些文字的按钮 / 标签页，每次点完等待若干秒
    reload：点完后重新打开一次页面再截（结果存在界面状态里，新页面照样显示）。新版因子实验室挖掘完后，
            在点击的那个标签页里截整页会一直等不到结果，重新打开就正常
    crop：给了起始文字时，左边也从它所在元素的左边（留 32px）开始，去掉新版界面的侧栏；
    crop：(起始文字, 结束文字, 上边留白, 下边留白)，截整页后只保留「起始文字所在元素的顶部」到「结束文字所在元素的顶部」；
          文字为 None 表示页面顶部 / 底部
    """
    edge = next(p for p in EDGE if p.exists())
    profile = tempfile.mkdtemp(prefix="sq_shot_")
    proc = subprocess.Popen([str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                ws_url = next(p["webSocketDebuggerUrl"] for p in pages if p.get("type") == "page")
                break
            except Exception:  # noqa: BLE001 - 浏览器还没起来
                time.sleep(0.2)
        async with websockets.connect(ws_url, max_size=None) as ws:
            cdp = CDP(ws)
            await cdp("Page.enable")
            await cdp("Emulation.setDeviceMetricsOverride", width=width, height=height or 900,
                      deviceScaleFactor=scale, mobile=False)
            await cdp("Page.navigate", url=url)
            await asyncio.sleep(wait)
            for text, pause in clicks:
                ok = (await cdp("Runtime.evaluate", expression=CLICK_JS % json.dumps(text),
                                returnByValue=True))["result"].get("value")
                if not ok:
                    raise RuntimeError(f"button not found: {text}")
                await asyncio.sleep(pause)
            if reload and clicks:
                await cdp("Page.navigate", url=url)
                await asyncio.sleep(wait)
            await cdp("Runtime.evaluate", expression="window.scrollTo(0, 0)")
            if height is None:
                h = (await cdp("Runtime.evaluate", expression="document.documentElement.scrollHeight",
                               returnByValue=True))["result"]["value"]
                await cdp("Emulation.setDeviceMetricsOverride", width=width, height=int(h),
                          deviceScaleFactor=scale, mobile=False)
                await asyncio.sleep(1.0)
            box = None
            if crop:
                start, end, pad_top, pad_bottom = crop
                hit = None if start is None else await find(cdp, start)
                y0 = 0 if hit is None else hit[0] - pad_top
                x0 = 0 if hit is None else max(int(hit[2]) - 32, 0)     # 从页面中间截时，左边的侧栏 / 列表也裁掉
                y1 = None if end is None else (await find(cdp, end))[0] + pad_bottom
                box = (max(int(y0), 0), None if y1 is None else int(y1), x0)
            shot = await cdp("Page.captureScreenshot", format="png", captureBeyondViewport=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            if box:
                from PIL import Image
                im = Image.open(out)
                im.crop((int(box[2] * scale), int(box[0] * scale), im.width,
                         im.height if box[1] is None else int(box[1] * scale))).save(out)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        remove_profile(profile)


def remove_profile(profile: str):
    """删掉这次用的 Edge 临时配置目录（每个 0.1～0.3 GB，不删会一直堆在 Temp 里）。
    主进程退出后，渲染、GPU 等子进程还会占着文件一小会儿，所以删不干净就稍等再试"""
    for _ in range(20):
        shutil.rmtree(profile, ignore_errors=True)
        if not Path(profile).exists():
            return
        time.sleep(0.5)


async def find(cdp, text):
    r = (await cdp("Runtime.evaluate", expression=FIND_JS % json.dumps(text), returnByValue=True))["result"].get("value")
    if not r:
        raise RuntimeError(f"text not found: {text}")
    return r


if __name__ == "__main__":
    a = sys.argv[1:]
    asyncio.run(shoot(a[0], Path(a[1]), int(a[2]) if len(a) > 2 else 1440, float(a[3]) if len(a) > 3 else 6.0,
                      int(a[4]) if len(a) > 4 else None))
    print(a[1])
