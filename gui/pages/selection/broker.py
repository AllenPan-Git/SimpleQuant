"""选股页的资金与费率设置（股票与可转债分开记）"""

from gui import state


def broker_key(kind: str) -> str:
    """股票与可转债的资金和费率分开记（可转债默认用债券费率）"""
    return "sp_cb" if kind == "cb" else "sp"


def broker_default(kind: str) -> dict:
    """页面还没记过资金与费率时的初始值（与回测标签页的 broker_settings 一致）"""
    return {"cash": 1_000_000, "preset": "bond" if kind == "cb" else "stock", "t1": True, "slippage": 5.0, "custom": {}}


def _sp_broker(kind: str = "stock"):
    """滚动优化用选股回测标签页里的资金与费率设置"""
    from simplequant.engine import BrokerConfig, COST_PRESETS
    b = state.STATE.get(f"{broker_key(kind)}_broker") or \
        {"cash": 1_000_000, "preset": "bond" if kind == "cb" else "stock", "slippage": 5.0, "custom": {}}
    c = COST_PRESETS[b["preset"]]
    mine = b.get("custom", {}).get(b["preset"], {})
    return BrokerConfig(cash=float(b["cash"]), commission=mine.get("commission", c["commission"] * 1e4) / 1e4,
                        min_commission=float(mine.get("min_commission", c["min_commission"])),
                        stamp_duty=mine.get("stamp_duty", c["stamp_duty"] * 1e4) / 1e4,
                        slippage=b.get("slippage", 5.0) / 1e4, t_plus_1=True)
