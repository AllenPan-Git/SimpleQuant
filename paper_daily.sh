#!/bin/sh
# 模拟盘每日运行：更新数据、推进所有模拟账户、生成明日信号（日志见 data_cache/paper/daily.log）
cd "$(dirname "$0")" || exit 1
PYTHONIOENCODING=utf-8 exec .venv/bin/python -m simplequant.paper
