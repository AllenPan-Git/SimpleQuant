"""
SimpleQuant 桌面版入口（NiceGUI；打包成 exe 时也用这个文件）
    python main.py                     打开桌面窗口
    python main.py --browser           在浏览器里打开（开发、测试用）
    python main.py --paper             不开界面，更新数据并推进所有模拟账户（供 Windows 定时任务调用）
    python main.py --run-script x.py   运行导出的选股脚本（输出同时写入 x.log）
"""

import argparse
import multiprocessing
import os
import sys


def _ensure_streams():
    """
    打包成窗口程序后 sys.stdout / stderr 为 None，部分库直接调用 .write 会报错；
    改写到 <数据目录>/logs/app.log（出问题时可以看这里的报错），超过 5 MB 先清空
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from simplequant.paths import DATA_ROOT
        log = DATA_ROOT / "logs" / "app.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 5e6:
            log.unlink()
        f = open(log, "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="SimpleQuant")
    ap.add_argument("--browser", action="store_true", help="open in a browser tab instead of a desktop window")
    ap.add_argument("--port", type=int, default=0, help="local port (0 = pick a free one)")
    ap.add_argument("--paper", action="store_true", help="run the daily paper-trading update and exit")
    ap.add_argument("--run-script", metavar="FILE", help="run an exported selection script and exit")
    args = ap.parse_args(argv)

    if args.run_script:
        from simplequant.export.run_script import run as run_script
        return run_script(args.run_script)

    if args.paper:
        from simplequant.paper.__main__ import main as paper_main
        return paper_main()

    try:
        from gui.app import run
    except ModuleNotFoundError as e:
        if e.name in ("nicegui", "webview"):
            print(f"缺少依赖 {e.name}，请先安装：pip install -r requirements.txt / missing dependency: {e.name}")
            return 1
        raise
    run(native=not args.browser, port=args.port)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()   # 打包后参数优化、数据下载的子进程需要它，必须在最前面
    _ensure_streams()
    sys.exit(main())
