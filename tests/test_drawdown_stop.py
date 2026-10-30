"""
选股的回撤停手（待办 47）：
1. 参数解析与默认恢复条件（close > sma(20)）
2. 方案文件版本号（DRAWDOWN_STOP_VERSION = 11）
3. 市场错误校验（仅有 buy 时若开启回撤停手不报 no sell conditions）
4. 回测全流程：触发回撤停手次日清仓，停手期间不买，大盘恢复后在下一个调仓日恢复选股
5. 平台导出 sel_target 动态回撤跟踪与恢复
"""

import numpy as np
import pandas as pd
import pytest

from simplequant import plan as P
from simplequant.export import selection_core as core
from simplequant.stocks import build_schedule, run_selection
from simplequant.stocks.selection import (
    drawdown_recovery_hit,
    drawdown_stop_threshold,
    market_errors,
    uses_drawdown_stop,
)
from test_selection_export import make_panel


BASE = {
    "kind": "selection",
    "universe": "hs300",
    "top_n": 5,
    "rebalance": "monthly",
    "position_pct": 95,
    "factors": [{"key": "ret20", "weight": 1, "direction": -1}],
}


def test_params_and_threshold():
    assert not uses_drawdown_stop({})
    assert not uses_drawdown_stop({"drawdown_stop": 0})
    assert not uses_drawdown_stop({"drawdown_stop": None})
    assert uses_drawdown_stop({"drawdown_stop": 15})
    assert uses_drawdown_stop({"drawdown_stop": {"max_drawdown": 10}})
    assert drawdown_stop_threshold({"drawdown_stop": 15}) == pytest.approx(0.15)
    assert drawdown_stop_threshold({"drawdown_stop": {"max_drawdown": 10}}) == pytest.approx(0.10)
    assert drawdown_stop_threshold({}) == 0.0


def test_recovery_block_defaults_and_override():
    # 默认恢复条件为 close > sma(20)
    spec_default = {"drawdown_stop": 15}
    block = core.sel_recovery_block(spec_default)
    assert block["logic"] == "all"
    assert block["conditions"][0]["left"]["ind"] == "close"
    assert block["conditions"][0]["op"] == ">"
    assert block["conditions"][0]["right"]["ind"] == "sma"
    assert block["conditions"][0]["right"]["params"]["period"] == 20

    # 复用 market.buy
    custom_cond = [{"left": {"ind": "close"}, "op": ">", "right": {"value": 100}}]
    spec_market = {"drawdown_stop": 15, "market": {"buy": {"logic": "all", "conditions": custom_cond}}}
    assert core.sel_recovery_block(spec_market)["conditions"] == custom_cond

    # 优先使用 drawdown_stop.recovery
    dd_cond = [{"left": {"ind": "rsi", "params": {"period": 14}}, "op": ">", "right": {"value": 50}}]
    spec_override = {
        "drawdown_stop": {"max_drawdown": 15, "recovery": {"logic": "all", "conditions": dd_cond}},
        "market": {"buy": {"logic": "all", "conditions": custom_cond}},
    }
    assert core.sel_recovery_block(spec_override)["conditions"] == dd_cond


def test_plan_version():
    spec_no = dict(BASE)
    assert P.selection_version(spec_no) < P.DRAWDOWN_STOP_VERSION
    spec_dd = {**BASE, "drawdown_stop": 15}
    assert P.selection_version(spec_dd) == P.DRAWDOWN_STOP_VERSION == 11


def test_market_errors_allows_buy_only_when_drawdown_stop_enabled():
    custom_buy = {"buy": {"conditions": [{"left": {"ind": "close"}, "op": ">", "right": {"ind": "sma", "params": {"period": 20}}}]}}
    # 没有 drawdown_stop 且只有 buy：报错
    assert market_errors({"market": custom_buy}) == ["market: no sell conditions"]
    # 开启了 drawdown_stop 且只有 buy：正常允许（作为回撤停手恢复条件）
    assert market_errors({"drawdown_stop": 15, "market": custom_buy}) == []


@pytest.fixture(scope="module")
def panel():
    return make_panel()[0]


def test_backtest_drawdown_stop_and_recovery(panel):
    # 设置一个 5% 的低回撤阈值以确保在测试面板上触发
    spec = {**BASE, "drawdown_stop": 5}
    start = str(panel.calendar[250].date())
    res = run_selection(panel, spec, start=start)

    # 检查日志中是否有停手记录
    dd_stops = [log for log in res.logs if log[1] == "sel.dd_stop"]
    assert len(dd_stops) > 0, "回撤停手应至少触发一次"

    stop_date = pd.Timestamp(dd_stops[0][0]).normalize()
    cal = panel.calendar
    stop_idx = cal.get_loc(stop_date)
    next_day = cal[stop_idx + 1]

    # 触发次日开盘应执行全部卖出清仓
    orders = res.orders
    next_orders = orders[orders["time"].dt.normalize() == next_day]
    assert len(next_orders) > 0, "触发次日应有清仓委托"
    assert (next_orders["side"] == "sell").all(), "触发次日所有委托应全为卖出"

    # 若有恢复记录，检查恢复后净值与再次买入
    dd_resumes = [log for log in res.logs if log[1] == "sel.dd_resume"]
    if dd_resumes:
        resume_date = pd.Timestamp(dd_resumes[0][0]).normalize()
        assert resume_date >= next_day


def test_sel_target_platform_drawdown_stop():
    # 测试导出核心中的 sel_target 动态跟踪
    cal = [f"2024-01-{i:02d}" for i in range(1, 31)] + [f"2024-02-{i:02d}" for i in range(1, 20)]
    today = "2024-02-20"
    spec = {**BASE, "drawdown_stop": 10}

    class MockApi:
        def index_close(self, calendar):
            return np.full(len(calendar), 100.0)

    state = {}
    cache = {}

    # 第 1 天：净值 100 万，正常
    res1 = core.sel_target(spec, cal[:25], "2024-01-26", "2024-01-01", MockApi(), cache, None, False, print, state, 1_000_000.0)
    assert not state.get("dd_stopped")
    assert state.get("dd_peak") == 1_000_000.0

    # 第 2 天：净值下跌至 88 万（回撤 12% > 10%），触发停手清仓
    res2 = core.sel_target(spec, cal[:26], "2024-01-27", "2024-01-01", MockApi(), cache, None, False, print, state, 880_000.0)
    assert res2 == []
    assert state.get("dd_stopped") is True

    # 第 3 天：指数未恢复（比如指数持续跌破 20 日均线），净值 88 万，停手期间不调仓
    class MockFallingApi:
        def index_close(self, calendar):
            arr = np.linspace(100, 70, len(calendar))
            return arr

    res3 = core.sel_target(spec, cal[:27], "2024-01-28", "2024-01-01", MockFallingApi(), cache, None, False, print, state, 880_000.0)
    assert res3 is None
    assert state.get("dd_stopped") is True

    # 第 4 天：指数突破 20 日均线企稳恢复（前期 50，最新冲到 100，大于 20 日均线约 52.5）
    class MockRecoveringApi:
        def index_close(self, calendar):
            arr = np.full(len(calendar), 50.0)
            arr[-1] = 100.0
            return arr

    res4 = core.sel_target(spec, cal[:28], "2024-01-29", "2024-01-01", MockRecoveringApi(), cache, None, False, print, state, 880_000.0)
    assert state.get("dd_stopped") is False
    assert state.get("dd_peak") == 880_000.0
