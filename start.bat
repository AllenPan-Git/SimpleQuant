@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建虚拟环境并安装依赖，请稍候……
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
rem 打开 SimpleQuant（桌面窗口）；关掉窗口即退出
".venv\Scripts\python.exe" main.py
