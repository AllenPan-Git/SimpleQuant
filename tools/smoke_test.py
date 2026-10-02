"""
打包后的冒烟测试（GitHub Actions 里在 tools/build_unix.sh 之后运行；本机也可以用）

    python tools/smoke_test.py <可执行文件> [--screenshot 截图.png] [--ui tools/ui_walkthrough.py --ui-out 目录]

1. 用临时数据目录启动桌面窗口版，等本机界面服务能打开首页，界面渲染一会儿后截图，确认进程还活着，再结束它
2. 运行一次 --paper（没有模拟账户，应正常退出并写日志）
3. 给了 --ui 时：再启动一次，由程序在窗口里自动走一遍界面测试（gui/uitest.py），截图和 result.json 写到 --ui-out；
   有 FAIL 的步骤则失败（依赖网络的步骤只记 WARN）
Linux 上要在有显示的环境里运行（Actions 里用 xvfb-run）。
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, proc: subprocess.Popen, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"程序提前退出，返回码 {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode("utf-8", "replace")
        except OSError:
            time.sleep(1)
    raise SystemExit(f"{timeout} 秒内没能打开 {url}")


def screenshot(path: Path):
    if sys.platform == "darwin":
        cmd = ["screencapture", "-x", str(path)]
    elif shutil.which("import"):
        cmd = ["import", "-window", "root", str(path)]          # ImageMagick
    else:
        print("（没有截图工具，跳过截图）")
        return
    subprocess.run(cmd, check=False, timeout=60)
    print(f"截图：{path}" if path.exists() else "截图失败")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("--screenshot", type=Path)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--ui", type=Path, help="界面自动测试脚本")
    ap.add_argument("--ui-out", type=Path, default=Path("ui-results"))
    args = ap.parse_args()

    data = Path(tempfile.mkdtemp(prefix="sq-smoke-"))
    env = {**os.environ, "SIMPLEQUANT_DATA": str(data)}
    port = free_port()
    log = open(data / "stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen([args.exe, "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        html = wait_http(f"http://127.0.0.1:{port}/", proc, args.timeout)
        assert "SimpleQuant" in html, "首页内容不对"
        print(f"界面服务正常（端口 {port}）")
        time.sleep(15)                                       # 等窗口把页面渲染出来
        if args.screenshot:
            screenshot(args.screenshot)
        if proc.poll() is not None:
            raise SystemExit(f"窗口打开后程序退出了，返回码 {proc.returncode}")
        print("窗口运行 15 秒正常")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()
        app_log = data / "logs" / "app.log"
        for f in (data / "stdout.log", app_log):
            if f.exists() and f.stat().st_size:
                print(f"----- {f.name} -----\n{f.read_text(encoding='utf-8', errors='replace')[-4000:]}")

    r = subprocess.run([args.exe, "--paper"], env=env, timeout=300)
    daily = data / "data_cache" / "paper" / "daily.log"
    if r.returncode != 0 or not daily.exists():
        raise SystemExit(f"--paper 失败（返回码 {r.returncode}）")
    print("--paper 正常：" + daily.read_text(encoding="utf-8").strip().splitlines()[-1])
    if args.ui:
        ui_walkthrough(args.exe, args.ui, args.ui_out)


def ui_walkthrough(exe: str, script: Path, out: Path, timeout: float = 1200):
    data = Path(tempfile.mkdtemp(prefix="sq-ui-"))
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SIMPLEQUANT_DATA": str(data), "SIMPLEQUANT_UI_SCRIPT": str(script.resolve()),
           "SIMPLEQUANT_UI_OUT": str(out.resolve())}
    print(f"界面自动测试：{script} → {out}")
    with open(out / "stdout.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen([exe], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            print(f"{timeout} 秒内没有走完")
    app_log = data / "logs" / "app.log"
    if app_log.exists():
        shutil.copy(app_log, out / "app.log")
    res = out / "result.json"
    if not res.exists():
        print((out / "stdout.log").read_text(encoding="utf-8", errors="replace")[-4000:])
        raise SystemExit("界面自动测试没有产生结果")
    import json
    results = json.loads(res.read_text(encoding="utf-8"))
    lines = [f"[{r['status']}] {r['step']}  {r['note']}  ({r['sec']} 秒)" for r in results]
    for line in lines:
        print("  " + line)
    if os.environ.get("GITHUB_ACTIONS"):      # 每步结果写成公开注释：完整日志要登录才能看，注释不用
        print("::notice title=UI walkthrough::" + "%0A".join(x.replace("%", "%25") for x in lines))
    if any(r["status"] == "FAIL" for r in results):
        print((out / "steps.log").read_text(encoding="utf-8", errors="replace")[-4000:])
        raise SystemExit("界面自动测试有失败的步骤")


if __name__ == "__main__":
    main()
