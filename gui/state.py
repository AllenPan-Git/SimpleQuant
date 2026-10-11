"""
跨页面的界面状态（刚搭建的策略、回测结果等；重启程序清空）
桌面程序只有一个用户，放在进程内存里即可；切换页面后保留，重启程序后清空（与旧版一致）。
"""

from ui.shared import PRESETS

STATE: dict = {}


def strategy() -> dict:
    """策略页正在编辑的内容"""
    return STATE.setdefault("strategy", {
        "mode": "template",             # template / rule / ai / code
        "tpl_key": None,                # None = 第一个模板
        "tpl_params": {},               # {模板: {参数: 值}}
        "rule": PRESETS["ma"](),
        "preset": "ma",
        "name": "",
        "ai_text": "",
        "ai_result": None,
        "ai_prev": {},                  # 描述 -> 上一次生成的规则（rule_signature, 规则说明），同一描述再次生成结果不同时提醒
        "ai_changed": None,             # 这一次与上一次不同时：上一次的规则说明
        "ai_symbols": None,             # AI 识别出的标的；去回测时自动选中本地已有的
        "code": "",                     # 代码编辑区的内容；空 = 第一次打开时放入带注释的示例
    })


def reset():
    """测试用：清空全部状态"""
    STATE.clear()
