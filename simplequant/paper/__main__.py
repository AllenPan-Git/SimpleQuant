"""
命令行：更新数据并推进所有模拟账户（供定时任务调用）
    .venv\\Scripts\\python -m simplequant.paper
日志追加写入 data_cache/paper/daily.log
"""

import datetime as dt
import sys

from .account import PAPER_ROOT, list_accounts
from .runner import run_all


def main() -> int:
    PAPER_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = PAPER_ROOT / "daily.log"
    with open(log_path, "a", encoding="utf-8") as f:
        def log(msg):
            line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
            print(line, flush=True)
            f.write(line + "\n")
            f.flush()
        accounts = list_accounts()
        log(f"run start: {len(accounts)} account(s)")
        results = run_all(accounts, log=log)
        failed = [k for k, v in results.items() if v]
        log(f"run end: {len(results) - len(failed)} ok, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
