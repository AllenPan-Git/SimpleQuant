#!/bin/sh
# 打开 SimpleQuant（macOS / Linux 源码版）；第一次运行时创建虚拟环境并安装依赖
# 用法：在终端里运行  sh start.sh  （或 chmod +x start.sh 后 ./start.sh）；关掉窗口即退出
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    PY=""
    for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PY="$c"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "需要 Python 3.10 或更高版本。macOS 可从 python.org 下载或 brew install python；Linux 请用系统的包管理器安装。"
        echo "Python 3.10 or newer is required."
        exit 1
    fi
    echo "首次运行，正在创建虚拟环境并安装依赖，请稍候……（$PY）"
    # Linux 的桌面窗口用系统的 GTK 绑定（python3-gi），它不能用 pip 直接装；系统里有时让虚拟环境能用到它
    SITE=""
    if [ "$(uname)" = "Linux" ] && "$PY" -c 'import gi' 2>/dev/null; then
        SITE="--system-site-packages"
    fi
    if ! "$PY" -m venv $SITE .venv; then
        echo "创建虚拟环境失败。Debian / Ubuntu 请先安装：sudo apt install python3-venv"
        rm -rf .venv
        exit 1
    fi
    .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python main.py "$@"
