"""把当前环境里 requirements.txt 各依赖的版本写成 constraints.txt。

用法：升级 .venv 里的依赖并跑完测试后，运行  .venv\\Scripts\\python tools\\pin_versions.py
start.bat / start.sh / CI 安装时都带 -c constraints.txt，因此从源码运行的人装到的就是测试过的版本；
requirements.txt 本身只写下限，手动 pip install -r requirements.txt 到自己环境里的人不会被强制降级。
"""
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 固定的版本里要求 Python 最高的那个（numpy 2.5 要 3.12）决定这里的下限；更旧的 Python 不套用固定版本
MIN_PYTHON = "3.12"

HEADER = f"""\
# 由 tools/pin_versions.py 根据开发环境生成，是测试通过的版本。不要手动改，重新生成即可
# start.bat / start.sh / CI 安装时使用：pip install -r requirements.txt -c constraints.txt
# Python {MIN_PYTHON} 以下装不了其中部分版本，因此不套用（按 requirements.txt 的下限安装最新版）
"""


# 不在 requirements.txt 里、但需要另行安装时也用固定版本的包（见 requirements.txt 末尾的说明）
OPTIONAL = ["litellm"]


def names():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line.split("#")[0])
        if m:
            yield m.group(1)
    yield from OPTIONAL


def main():
    lines, missing = [], []
    for name in names():
        try:
            lines.append(f'{name}=={version(name)}; python_version >= "{MIN_PYTHON}"')
        except PackageNotFoundError:
            missing.append(name)
    if missing:
        sys.exit("当前环境缺少：" + "、".join(missing))
    (ROOT / "constraints.txt").write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
