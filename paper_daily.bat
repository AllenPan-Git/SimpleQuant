@echo off
rem 模拟盘每日运行：更新数据、推进所有模拟账户、生成明日信号（日志见 data_cache\paper\daily.log）
cd /d "%~dp0"
".venv\Scripts\python.exe" -m simplequant.paper
