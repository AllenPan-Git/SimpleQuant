"""
界面自动操作（只供打包后的测试用）：设置环境变量 SIMPLEQUANT_UI_SCRIPT=<脚本路径> 时，程序启动后在真正的桌面窗口里
按脚本操作（跳转页面、点按钮、等文字出现、截图），结果写入 SIMPLEQUANT_UI_OUT 目录（steps.log、result.json、截图），
最后关闭窗口。GitHub Actions 的 macOS / Linux 机器没有人操作，用它把测试清单的大部分步骤自动走一遍。

脚本里定义 async def main(d: Driver)，例子见 tools/ui_walkthrough.py。
"""

import asyncio
import json
import os
import runpy
import subprocess
import sys
import time
import traceback
from pathlib import Path

from nicegui import Client, ElementFilter, app, background_tasks, ui


class StepFailed(Exception):
    pass


class Driver:
    def __init__(self, out: Path):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        self.client: Client | None = None
        self._n = 0

    # ---------- 记录 ----------
    def log(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(self.out / "steps.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def step(self, name: str, fn, required: bool = True):
        """运行一步：成功 ok；失败时 required 记 FAIL，否则记 WARN（例如依赖网络的下载）"""
        t0 = time.time()
        try:
            note = await fn()
            status = "ok"
        except Exception as e:  # noqa: BLE001
            note = f"{type(e).__name__}: {e}"
            status = "FAIL" if required else "WARN"
            self.log(traceback.format_exc())
        self.results.append({"step": name, "status": status, "note": note or "", "sec": round(time.time() - t0, 1)})
        self.log(f"[{status}] {name} {note or ''}")
        (self.out / "result.json").write_text(json.dumps(self.results, ensure_ascii=False, indent=1), encoding="utf-8")
        return status == "ok"

    # ---------- 页面 ----------
    def _connected(self, path: str | None = None) -> list[Client]:
        out = []
        for c in list(Client.instances.values()):
            try:
                if c.has_socket_connection and (path is None or c.request.url.path == path):
                    out.append(c)
            except AttributeError:
                pass
        return out

    async def wait_client(self, path: str | None = None, exclude: set | None = None, timeout: float = 60) -> Client:
        deadline = time.time() + timeout
        while time.time() < deadline:
            cs = [c for c in self._connected(path) if not exclude or c.id not in exclude]
            if cs:
                self.client = cs[-1]
                await asyncio.sleep(1.0)          # 等页面画完
                return self.client
            await asyncio.sleep(0.3)
        raise StepFailed(f"no window connected to {path or 'any page'}")

    async def goto(self, path: str, timeout: float = 60) -> Client:
        old = {c.id for c in Client.instances.values()}
        with self.client:
            ui.navigate.to(path)
        return await self.wait_client(path, exclude=old, timeout=timeout)

    async def reload(self) -> Client:
        path = self.client.request.url.path
        old = {c.id for c in Client.instances.values()}
        self._js("location.reload()")
        return await self.wait_client(path, exclude=old)

    def _js(self, code: str):
        with self.client:
            ui.run_javascript(code)

    def find(self, marker: str | None = None, content: str | None = None) -> list:
        with self.client:
            return list(ElementFilter(marker=marker, content=content))

    def one(self, marker: str | None = None, content: str | None = None):
        els = self.find(marker, content)
        if not els:
            raise StepFailed(f"element not found: marker={marker} content={content}")
        return els[0]

    async def click(self, marker: str | None = None, content: str | None = None):
        el = self.one(marker, content)
        self._js(f'document.getElementById("c{el.id}").click()')      # 在窗口里真正点一下
        await asyncio.sleep(0.8)

    async def see(self, text: str, timeout: float = 30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.find(content=text):
                return
            await asyncio.sleep(0.5)
        raise StepFailed(f"text not shown within {timeout}s: {text}")

    async def until(self, cond, timeout: float = 60, what: str = "condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            await asyncio.sleep(0.5)
        raise StepFailed(f"timeout waiting for {what}")

    # ---------- 截图 ----------
    async def shot(self, name: str):
        await asyncio.sleep(1.5)
        self._n += 1
        path = self.out / f"{self._n:02d}-{name}.png"
        try:
            if sys.platform == "darwin":
                cmd = ["screencapture", "-x", str(path)]
            elif sys.platform.startswith("linux"):
                cmd = ["import", "-window", "root", str(path)]
            else:
                from PIL import ImageGrab
                ImageGrab.grab().save(path)
                return
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 60)
        except Exception as e:  # noqa: BLE001
            self.log(f"screenshot failed: {e}")

    # ---------- 结束 ----------
    def quit(self):
        if app.native.main_window:
            app.native.main_window.destroy()       # 与用户关窗口相同的退出路径（见 gui/update.py _quit）
        else:
            app.shutdown()


def install():
    """gui.app.run 调用：设置了 SIMPLEQUANT_UI_SCRIPT 时，启动后在后台运行脚本"""
    script = os.environ.get("SIMPLEQUANT_UI_SCRIPT")
    if not script:
        return
    out = Path(os.environ.get("SIMPLEQUANT_UI_OUT") or Path(script).with_name("ui-results"))

    async def run():
        d = Driver(out)
        try:
            await d.wait_client(timeout=120)
            main = runpy.run_path(script)["main"]
            await main(d)
        except Exception:  # noqa: BLE001
            d.log(traceback.format_exc())
            d.results.append({"step": "script", "status": "FAIL", "note": "crashed", "sec": 0})
            (out / "result.json").write_text(json.dumps(d.results, ensure_ascii=False, indent=1), encoding="utf-8")
        finally:
            d.log("done")
            await asyncio.sleep(1)
            d.quit()

    app.on_startup(lambda: background_tasks.create(run(), name="uitest"))
