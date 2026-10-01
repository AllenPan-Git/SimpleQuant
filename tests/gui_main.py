"""界面测试用的入口（nicegui.testing 通过 runpy 执行它）"""
from gui.app import run

run(native=False, show=False, check_updates=False)
