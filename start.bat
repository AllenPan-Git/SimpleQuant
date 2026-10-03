@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建虚拟环境，请稍候……
    python -m venv .venv
)
rem requirements.txt 或 constraints.txt 与上次安装时不同（包括第一次运行）才安装依赖；装好后各存一份到 .venv 里用来比较
fc /b requirements.txt .venv\requirements.txt >nul 2>&1 && fc /b constraints.txt .venv\constraints.txt >nul 2>&1 || (
    echo 正在安装依赖，请稍候……
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -c constraints.txt && copy /y requirements.txt .venv\ >nul && copy /y constraints.txt .venv\ >nul
)
rem 打开 SimpleQuant（桌面窗口）；关掉窗口即退出
".venv\Scripts\python.exe" main.py
