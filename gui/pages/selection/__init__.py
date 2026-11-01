"""第 5 页：多因子选股（数据 / 因子研究 / 自定义因子 / 选股回测 / 滚动优化）"""

from .common import sp_state, switch_kind, get_panel
from .page import page
from .backtest import current_spec, apply_spec, run_script_dialog, SCRIPT_MARKS
from .broker import broker_key, broker_default

__all__ = ["page", "sp_state", "switch_kind", "current_spec", "apply_spec", "broker_key", "broker_default",
           "run_script_dialog", "SCRIPT_MARKS", "get_panel"]
