"""NiceGUI 界面测试"""

import asyncio
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from nicegui import ui
from nicegui.testing import User

from simplequant import strategies
from simplequant.data import library
from gui import state
from gui.layout import PAGES
from conftest import make_prices

pytestmark = pytest.mark.nicegui_main_file(str(Path(__file__).with_name("gui_main.py")))


def patch_page(monkeypatch, route: str, name: str, value):
    """替换某个页面模块里的全局名字。
    NiceGUI 的测试工具在每个测试后会把页面模块从 sys.modules 删掉，测试里 import 到的可能是新的一份，
    而 gui.app 注册的页面函数还用着旧的那份；所以按路由找到页面函数实际使用的全局变量来改。"""
    monkeypatch.setitem(routes()[route].__globals__, name, value)


def routes() -> dict:
    """gui.app 里注册的路由 → 页面函数（gui 包被重新导入后没有 app 属性，所以从 sys.modules 取）"""
    import sys
    import gui.app  # noqa: F401  首次导入
    return sys.modules["gui.app"].ROUTES


async def settle():
    """ui.refreshable 的 refresh() 在下一轮事件循环才重画；点击后先等它画完再操作新控件"""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """每个测试：界面状态清空；保存的策略写到临时目录"""
    state.reset()
    d = tmp_path / "strategies"
    for fn in (strategies.save_strategy, strategies.list_strategies, strategies.delete_strategy):
        monkeypatch.setattr(fn, "__defaults__", (d,))
    from simplequant.stocks import custom_factors
    monkeypatch.setattr(custom_factors, "FACTOR_DIR", tmp_path / "factors")
    from simplequant import allocation
    monkeypatch.setattr(allocation, "ROOT", tmp_path / "allocation")      # 风险测评与配置设置
    custom_factors.load_all()
    yield
    state.reset()
    monkeypatch.undo()
    custom_factors.load_all()


@pytest.fixture
def lib_dir(tmp_path, monkeypatch):
    """本地数据库改到临时目录（lib_dir 是默认参数，只能改函数的默认值）"""
    d = tmp_path / "lib"
    for fn in (library.save, library.load, library.exists, library.list_datasets, library.delete):
        monkeypatch.setattr(fn, "__defaults__", (d,))
    return d


def test_every_gui_text_key_exists():
    """新界面代码里 t("…") 用到的每个键都必须有中英文"""
    import re
    from simplequant.i18n import TEXT
    root = Path(__file__).resolve().parents[1] / "gui"
    keys = set()
    for f in root.rglob("*.py"):
        keys |= set(re.findall(r'\bt\("([\w.]+)"', f.read_text(encoding="utf-8")))
    assert len(keys) > 100
    missing = sorted(k for k in keys if k not in TEXT or not TEXT[k].get("en") or not TEXT[k].get("zh"))
    assert not missing, missing


async def test_home_migrate_card(user: User, tmp_path, monkeypatch):
    """打包版第一次运行：首页提示导入旧数据，点「导入」后复制并不再提示"""
    from simplequant import migrate
    src, dst = tmp_path / "old", tmp_path / "new"
    (src / "data_cache").mkdir(parents=True)
    (src / "data_cache" / "a.json").write_text("{}")
    monkeypatch.setattr(migrate, "DATA_ROOT", dst)
    monkeypatch.setattr(migrate, "MARK", dst / ".migrated")
    monkeypatch.setattr(migrate, "FROZEN", True)
    monkeypatch.setattr(migrate, "candidates", lambda: [src])
    await user.open("/")
    await user.should_see("导入旧版数据")
    user.find(marker="mig_go").click()
    assert await _wait(lambda: (dst / "data_cache" / "a.json").exists() and migrate.MARK.exists())
    await user.open("/")
    await user.should_not_see("导入旧版数据")


async def test_home(user: User, lib_dir, monkeypatch):
    from gui import history
    history.clear()
    patch_page(monkeypatch, "/", "list_accounts", lambda: [])
    await user.open("/")
    await user.should_see("开始新的工作")
    await user.should_see("本地尚无数据")
    await user.should_see("先获取数据")             # 第一次使用：介绍 + 获取数据
    await user.should_see("尚未创建模拟账户")
    user.find(marker="start:code").click()
    await user.should_see(marker="mode")
    assert state.strategy()["mode"] == "code"


@pytest.mark.parametrize("route", [r for r, _, _ in PAGES])
async def test_every_route_opens(user: User, route: str):
    await user.open(route)
    await user.should_see(marker="logo")


async def test_nav_goes_to_page(user: User):
    await user.open("/")
    user.find(marker="nav:/data").click()
    await user.should_see("在线下载")
    user.find(marker="nav:/settings").click()
    await user.should_see(marker="llm_preset")


async def test_flow_switch_changes_steps(user: User):
    await user.open("/")
    user.find(marker="flow:selection").click()
    await user.should_see(marker="nav:/selection")
    await user.should_not_see(marker="nav:/optimize")
    user.find(marker="flow:timing").click()
    await user.should_see(marker="nav:/optimize")


# ---------------- 数据页 ----------------
async def test_data_empty_library(user: User, lib_dir):
    await user.open("/data")
    await user.should_see("暂无数据")


async def test_data_import_csv_from_path(user: User, lib_dir, tmp_path):
    f = tmp_path / "demo_1d.csv"
    make_prices(n=60).reset_index().to_csv(f, index=False)
    await user.open("/data")
    user.find(marker="csv-mode").elements.pop().set_value("path")
    user.find("文件或文件夹路径").type(str(f)).trigger("keydown.enter")
    await user.should_see("识别为")            # 预览：识别出数据类型
    user.find("导入至本地数据库").click()
    await user.should_see("已导入 60 根 K 线")
    metas = library.list_datasets()
    assert [m.name for m in metas] == ["demo"] and metas[0].rows == 60


async def test_data_download_uses_selected_options(user: User, lib_dir, monkeypatch):
    calls = []

    def fake_fetch(src, sym, start, end, **kw):
        calls.append((src, sym, start, end, kw))
        df = make_prices(n=20)
        return library.save(df, library.DatasetMeta(id=f"x_{sym}", name=kw["name"], symbol=sym, source=src, freq="1d"))
    patch_page(monkeypatch, "/data", "fetch_to_library", fake_fetch)

    await user.open("/data")
    user.find("其他代码（6 位，以逗号分隔）").type("159915，510300")
    user.find("下载至本地数据库").click()
    await user.should_see("159915：20 根 K 线")
    assert [c[1] for c in calls] == ["510300", "159915"]          # 常用 + 手填，去重保序
    assert calls[0][0] == "akshare" and calls[0][4]["adjust"] == "hfq" and calls[0][4]["asset"]
    assert len(library.list_datasets()) == 2


async def test_data_delete_asks_first(user: User, lib_dir):
    library.save(make_prices(n=20), library.DatasetMeta(id="x_1", name="甲", symbol="1", source="csv", freq="1d"))
    await user.open("/data")
    user.find("删除此数据").click()
    await user.should_see("取消")
    user.find("取消").click()
    await user.should_not_see("取消")
    assert len(library.list_datasets()) == 1
    user.find("删除此数据").click()
    await user.should_see(marker="confirm-delete")
    user.find(marker="confirm-delete").click()
    await user.should_see("暂无数据")
    assert library.list_datasets() == []


async def test_switch_language(user: User):
    await user.open("/")
    user.find("EN").click()
    await user.open("/")
    await user.should_see("Start something new")
    await user.should_not_see("开始新的工作")
    await user.open("/data")
    await user.should_see("Timing · Step 1")
    user.find("中").click()


async def test_theme_cycles_and_persists(user: User):
    from gui.common import dark
    await user.open("/")
    assert dark() is None                       # 默认跟随系统
    user.find("brightness_auto").click()
    assert dark() is False
    await user.open("/")
    user.find("light_mode").click()
    assert dark() is True


# ---------------- 策略页 ----------------
async def test_strategy_template_save_and_load(user: User):
    await user.open("/strategy")
    await user.should_see("买入持有")                     # 默认模板及其描述预览
    save = user.find("保存").elements.pop()
    assert not save.enabled                               # 没有名字不能保存
    user.find("策略名称").type("我的均线")
    assert save.enabled
    user.find("保存").click()
    assert "我的均线" in strategies.list_strategies()
    await user.should_see("已保存的策略（1）")

    state.reset()
    await user.open("/strategy")
    user.find("载入").click()
    assert state.strategy()["name"] == "我的均线"


async def test_strategy_rule_builder(user: User, lib_dir):
    _save_prices()                                         # 回测页有数据时才显示「运行」按钮
    await user.open("/strategy")
    user.find(marker="mode").elements.pop().set_value("rule")
    await settle()
    S = state.strategy()
    for preset in ["macd", "breakout", "boll"]:
        user.find(marker="preset").elements.pop().set_value(preset)
        user.find("载入示例").click()
        await settle()
    n = len(S["rule"]["buy"]["conditions"])
    user.find(marker="add:buy").click()
    assert len(S["rule"]["buy"]["conditions"]) == n + 1
    cid = S["rule"]["buy"]["conditions"][-1]["_id"]
    user.find(marker=f"del:{cid}").click()
    assert len(S["rule"]["buy"]["conditions"]) == n
    user.find("使用此策略回测").click()
    await user.should_see(marker="run")
    assert state.STATE["current_spec"]["kind"] == "rule"


async def test_strategy_rule_invalid_blocks_backtest(user: User):
    await user.open("/strategy")
    user.find(marker="mode").elements.pop().set_value("rule")
    await settle()
    S = state.strategy()
    for c in list(S["rule"]["buy"]["conditions"]):
        user.find(marker=f"del:{c['_id']}").click()
    assert not user.find("使用此策略回测").elements.pop().enabled


async def test_strategy_template_to_code_save_and_load(user: User):
    await user.open("/strategy")
    S = state.strategy()
    S["tpl_key"], S["tpl_params"] = "sma_cross", {"sma_cross": {"fast": 7}}
    await user.open("/strategy")
    user.find("转换为代码编辑").click()
    await settle()
    assert S["mode"] == "code" and "class Strategy(PerAsset)" in S["code"] and "('fast', 7)" in S["code"]
    await user.should_see("【自定义代码】")
    user.find("策略名称").type("代码均线")
    user.find("保存").click()
    saved = strategies.list_strategies()["代码均线"]
    assert saved["kind"] == "code" and saved["code"] == S["code"]

    state.reset()
    await user.open("/strategy")
    user.find("载入").click()
    await settle()
    assert state.strategy()["mode"] == "code" and state.strategy()["code"] == saved["code"]


async def test_strategy_code_errors_block_save(user: User):
    S = state.strategy()
    S["mode"] = "code"
    await user.open("/strategy")
    assert "class Strategy(PerAsset)" in S["code"]                   # 第一次打开放入带注释的示例
    user.find("策略名称").type("坏代码")
    editor = user.find(marker="code").elements.pop()
    editor.set_value("class Strategy(PerAsset)\n    pass\n")
    await user.should_see("第 1 行")
    assert not user.find("保存").elements.pop().enabled
    assert not user.find("使用此策略回测").elements.pop().enabled

    editor.set_value("class Strategy(PerAsset):\n    x = no_such_name\n")   # 语法对，但执行定义时出错
    assert user.find("保存").elements.pop().enabled
    user.find("保存").click()
    await user.should_see("NameError")
    assert "坏代码" not in strategies.list_strategies()


async def test_strategy_code_start_from_template(user: User):
    S = state.strategy()
    S["mode"] = "code"
    await user.open("/strategy")
    user.find(marker="code_start").elements.pop().set_value("tpl:boll_breakout")
    user.find(marker="code_start_load").click()
    assert "BollingerBands" in S["code"]
    assert user.find(marker="code").elements.pop().value == S["code"]
    user.find(marker="code_check").click()
    await user.should_see("已识别的参数")


async def test_backtest_code_strategy_and_runtime_error(user: User, lib_dir):
    from simplequant.strategies import code_strategy
    _save_prices()
    good = code_strategy.skeleton()
    state.STATE["current_spec"] = {"name": "代码", "kind": "code", "code": good}
    state.STATE["bt_strategy"] = "__current__"
    await user.open("/backtest")
    user.find(marker="run").click()
    assert await _wait(lambda: state.STATE.get("bt", {}).get("result") is not None)
    assert not state.STATE["bt"]["result"].orders.empty

    state.STATE["current_spec"]["code"] = good.replace("holding = self.getposition(d).size > 0", "holding = 1 / 0")
    await user.open("/backtest")
    user.find(marker="run").click()
    await user.should_see("策略代码第")
    user.find("导出策略").click()
    await user.should_see("导出 Python 脚本")


AI_REPLY = {
    "name": "放量突破", "understood": "收盘价突破20日新高且量比>2买入；跌破10日最低或亏损5%卖出。",
    "symbols": ["510300"], "unsupported": ["ROE 大于 15%"],
    "buy": {"logic": "all", "conditions": [
        {"left": {"kind": "indicator", "ind": "close", "line": "", "params": [], "value": 0}, "op": ">",
         "right": {"kind": "indicator", "ind": "highest", "line": "", "params": [{"name": "period", "value": 20}], "value": 0}},
        {"left": {"kind": "indicator", "ind": "vol_ratio", "line": "", "params": [], "value": 0}, "op": ">",
         "right": {"kind": "value", "ind": "close", "line": "", "params": [], "value": 2}}]},
    "sell": {"logic": "any", "conditions": [
        {"left": {"kind": "indicator", "ind": "close", "line": "", "params": [], "value": 0}, "op": "<",
         "right": {"kind": "indicator", "ind": "lowest", "line": "", "params": [{"name": "period", "value": 10}], "value": 0}},
        {"left": {"kind": "indicator", "ind": "pnl_pct", "line": "", "params": [], "value": 0}, "op": "<",
         "right": {"kind": "value", "ind": "close", "line": "", "params": [], "value": -5}}]},
    "position_pct": 90}


@pytest.fixture
def fake_llm(monkeypatch):
    import json
    import simplequant.llm as llm

    class Fake:
        calls = 0

        def chat(self, system, messages, schema=None):
            Fake.calls += 1
            return json.dumps(AI_REPLY, ensure_ascii=False)
    monkeypatch.setattr(llm, "load_config", lambda: llm.LLMConfig.from_preset("deepseek", api_key="x"))
    monkeypatch.setattr(llm, "get_provider", lambda cfg: Fake())
    return Fake


async def test_strategy_ai_flow(user: User, fake_llm, lib_dir):
    ds = library.save(make_prices(n=20), library.DatasetMeta(id="x_510300", name="沪深300ETF", symbol="510300",
                                                             source="akshare", freq="1d"))
    await user.open("/strategy")
    user.find(marker="mode").elements.pop().set_value("ai")
    await settle()
    user.find("描述买卖规则").type("放量突破20日新高买入，跌破10日低点或亏5%卖出，要求ROE>15%")
    user.find(marker="generate").click()
    await user.should_see("ROE 大于 15%")                  # 不支持的要求被明确列出
    await user.should_see("量比")                          # 规则预览
    assert fake_llm.calls == 1
    assert user.find("策略名称").elements.pop().value == "放量突破"
    user.find("在条件组件中编辑").click()
    await settle()
    S = state.strategy()
    assert S["mode"] == "rule" and len(S["rule"]["buy"]["conditions"]) == 2
    user.find("使用此策略回测").click()
    await user.should_see(marker="run")
    assert state.STATE["bt_ids"] == [ds.id]               # AI 识别出的 510300 被预选
    assert user.find(marker="bt_ids").elements.pop().value == [ds.id]
    await user.should_see("当前策略：放量突破")            # 策略下拉框选中刚才的策略


async def test_strategy_ai_without_config(user: User, monkeypatch):
    import simplequant.llm as llm
    monkeypatch.setattr(llm, "load_config", lambda: None)
    await user.open("/strategy")
    user.find(marker="mode").elements.pop().set_value("ai")
    await settle()
    await user.should_see("前往模型设置")
    user.find("描述买卖规则").type("均线金叉买入")
    assert not user.find(marker="generate").elements.pop().enabled


# ---------------- 回测页 ----------------
def _save_prices(sym="510300", n=400, name="沪深300ETF"):
    return library.save(make_prices(n=n), library.DatasetMeta(id=f"x_{sym}", name=name, symbol=sym,
                                                               source="akshare", freq="1d"))


async def test_backtest_needs_data(user: User, lib_dir):
    await user.open("/backtest")
    await user.should_see("前往数据页")


async def test_backtest_run_and_results(user: User, lib_dir):
    _save_prices()
    await user.open("/backtest")
    user.find(marker="bt_strategy").elements.pop().set_value("tpl:sma_cross")
    user.find(marker="run").click()
    for _ in range(100):                                  # 回测在后台线程里跑
        if state.STATE.get("bt", {}).get("result") is not None:
            break
        await asyncio.sleep(0.05)
    await user.should_see("总收益率")
    await user.should_see("成交记录")
    await user.should_see("可信度检查")
    res = state.STATE["bt"]["result"]
    assert not res.orders.empty
    assert any(c["key"].startswith("cred.") for c in state.STATE["bt"]["checks"])

    user.find(marker="native").click()
    for _ in range(200):
        if state.STATE["bt"].get("native_png"):
            break
        await asyncio.sleep(0.05)
    assert state.STATE["bt"]["native_png"][1:4] == b"PNG"             # PNG 文件头


async def test_backtest_cash_dividend(user: User, lib_dir, monkeypatch):
    """回测页选「现金分红」：分红到账、显示分红指标；不支持的 CSV 数据保留原样并提示"""
    from simplequant.data import cash_dividend
    df = make_prices()
    monkeypatch.setattr(cash_dividend, "fund_dividends",
                        lambda s: pd.DataFrame({"ex_date": [df.index[200]], "cash": [0.2], "bonus": [0.0],
                                                "reserve": [0.0]}))
    _save_prices()
    library.save(make_prices(seed=3), library.DatasetMeta(id="x_csv", name="自导入", symbol="自导入", source="csv",
                                                          freq="1d"))
    state.STATE["bt_ids"] = ["x_510300", "x_csv"]
    await user.open("/backtest")
    await user.should_see("为不复权数据")                  # 测试数据都是不复权的：提示假信号
    user.find(marker="bt_strategy").elements.pop().set_value("tpl:buy_hold")
    user.find(marker="bt_dividend").elements.pop().set_value("cash")
    await settle()
    assert state.STATE["bt_broker"]["dividend"] == "cash"
    user.find(marker="run").click()
    assert await _wait(lambda: state.STATE.get("bt", {}).get("result") is not None)
    res = state.STATE["bt"]["result"]
    assert res.metrics["dividend_cash"] > 0
    await user.should_see("收到现金分红")
    await user.should_see("无法使用现金分红")


async def test_optimize_cash_dividend(user: User, lib_dir, monkeypatch):
    from simplequant.data import cash_dividend
    monkeypatch.setattr(cash_dividend, "fund_dividends", lambda s: cash_dividend.EMPTY)
    _save_prices(n=700)
    _opt_setup()
    state.STATE["opt_broker"] = {"cash": 100_000, "preset": "etf", "t1": True, "slippage": 5.0, "custom": {},
                                 "dividend": "cash"}
    await user.open("/optimize")
    user.find(marker="opt_run").click()
    assert await _wait(lambda: state.STATE["opt"].get("result"))
    assert state.STATE["opt"]["result"]["compare"]["is_best"]["dividend_cash"] == 0


async def test_backtest_recorded_and_reopened_from_home(user: User, lib_dir):
    """跑完的回测出现在首页「继续上次的工作」和「最近的报告」；重启后点开会恢复设置并自动重新运行"""
    from gui import history
    history.clear()
    _save_prices()
    await user.open("/backtest")
    user.find(marker="bt_strategy").elements.pop().set_value("tpl:sma_cross")
    user.find(marker="run").click()
    await _wait(lambda: state.STATE.get("bt", {}).get("result") is not None)
    await user.should_see("下一步")
    first = state.STATE["bt"]["result"].metrics["total_return"]
    assert history.recent()[0]["kind"] == "bt" and history.recent()[0]["ret"] == pytest.approx(first)

    state.reset()                                        # 相当于重启程序：界面状态清空，历史还在
    await user.open("/")
    await user.should_see("继续上次的工作")
    await user.should_see(marker="recent")
    user.find(marker="open_report").click()
    await _wait(lambda: state.STATE.get("bt", {}).get("result") is not None)
    assert state.STATE["bt"]["result"].metrics["total_return"] == pytest.approx(first)
    assert len(history.recent()) == 1                    # 同一份报告不重复记录


async def test_backtest_export_menu(user: User, lib_dir):
    _save_prices()
    await user.open("/backtest")
    user.find(marker="bt_strategy").elements.pop().set_value("tpl:sma_cross")
    await settle()
    user.find("导出策略").click()
    await user.should_see("聚宽 JoinQuant")
    await user.should_see("迅投 QMT")
    user.find("导出 Python 脚本").click()
    user.download.http_responses  # noqa: B018  下载已触发即可


async def test_backtest_mixed_freq_blocks_run(user: User, lib_dir):
    _save_prices()
    library.save(make_prices(n=300, freq="min"), library.DatasetMeta(id="x_m", name="分钟", symbol="m",
                                                                       source="csv", freq="1m"))
    await user.open("/backtest")
    user.find(marker="bt_ids").elements.pop().set_value(["x_510300", "x_m"])
    await user.should_see("K 线周期不一致")
    assert not user.find(marker="run").elements.pop().enabled


async def test_backtest_ai_explain(user: User, lib_dir, monkeypatch):
    import simplequant.llm as llm

    class Fake:
        def chat(self, system, messages, schema=None):
            return "- 表现一般\n- 回撤可控"
    monkeypatch.setattr(llm, "load_config", lambda: llm.LLMConfig.from_preset("deepseek", api_key="x"))
    monkeypatch.setattr(llm, "get_provider", lambda cfg: Fake())
    _save_prices()
    await user.open("/backtest")
    user.find(marker="run").click()
    for _ in range(100):
        if state.STATE.get("bt", {}).get("result") is not None:
            break
        await asyncio.sleep(0.05)
    await user.should_see(marker="explain")
    user.find(marker="explain").click()
    await user.should_see("回撤可控")


# ---------------- 参数优化页 ----------------
async def _wait(cond, n=400):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def _opt_setup():
    """单进程、小网格，测试跑得快"""
    state.STATE["opt"] = {"mode": "grid", "chosen": {}, "ranges": {}, "metric": "sharpe", "oos": 30, "workers": 1,
                          "train": 12, "test": 6, "window": "rolling", "stitch": "continuous"}
    state.STATE["opt_strategy"] = "tpl:sma_cross"


async def test_optimize_grid_flow(user: User, lib_dir):
    _save_prices(n=700)
    _opt_setup()
    await user.open("/optimize")
    await user.should_see("组参数")                      # 组合数估算
    user.find(marker="opt_run").click()
    assert await _wait(lambda: state.STATE["opt"].get("result"))
    await user.should_see("优化结果")
    await user.should_see("样本外检验")
    await user.should_see("可信度检查")
    res = state.STATE["opt"]["result"]
    assert len(res["axes"]) == 2 and not res["df"].empty
    assert any(c["key"] in ("cred.oos_ok", "cred.oos_worse") for c in res["checks"])
    user.find(marker="use_best").click()
    await user.should_see(marker="run")
    assert state.STATE["current_spec"]["template"] == "sma_cross"
    await user.should_see("当前策略：")
    # 用最优参数回测：提示这组参数是从多少组里挑出来的
    user.find(marker="run").click()
    assert await _wait(lambda: state.STATE.get("bt", {}).get("result") is not None)
    tuned = [c for c in state.STATE["bt"]["checks"] if c["key"] == "cred.tuned"]
    assert tuned and tuned[0]["args"]["n"] == res["tried"] > 1
    await user.should_see("组组合中的最优者")


async def test_optimize_param_pick_limits_and_strategy_change(user: User, lib_dir):
    _save_prices(n=300)
    _opt_setup()
    await user.open("/optimize")
    sel = user.find(marker="opt_params").elements.pop()
    sel.set_value([])
    await settle()
    assert not user.find(marker="opt_run").elements.pop().enabled     # 没选参数不能跑
    user.find(marker="opt_strategy").elements.pop().set_value("tpl:buy_hold")
    await settle()
    assert user.find(marker="opt_params").elements                     # 换策略后参数区重画、不报错


async def test_optimize_walkforward_flow(user: User, lib_dir):
    _save_prices(n=900)
    _opt_setup()
    O = state.STATE["opt"]
    O["mode"] = "wf"
    tbs_key = None
    await user.open("/optimize")
    await user.should_see("个窗口")                      # 窗口数和回测次数估算
    # 把网格缩小到每个参数 2 个取值
    for k, ranges in O["ranges"].items():
        tbs_key = k
        for path, (a, b, s) in list(ranges.items()):
            ranges[path] = (a, a + s, s)
    assert tbs_key
    user.find(marker="wf_run").click()
    assert await _wait(lambda: O.get("wf"))
    await user.should_see("滚动优化结果")


# ---------------- 多因子选股（用本机已下载的沪深300数据，只读） ----------------
def _hs300_ready():
    from simplequant.stocks import StockStore
    st_ = StockStore()
    return st_.has_universe("hs300") and len(list((st_.root / "daily").glob("*.parquet"))) > 200


needs_stocks = pytest.mark.skipif(not _hs300_ready(), reason="沪深300数据未下载")


async def _open_selection(user: User, tab="bt"):
    """从 2022 年开始，打开选股页并等面板载入完成"""
    import datetime as dt
    from gui.pages.selection import sp_state
    SP = sp_state()
    SP["ranges"]["hs300"] = (dt.date(2022, 1, 4), dt.date(2100, 1, 1))     # 结束日会被截到数据末尾
    state.STATE["sp_tab"] = tab
    await user.open("/selection")
    assert await _wait(lambda: state.STATE.get("sp_panel_key") is not None, n=1200), "面板没有载入"
    await settle()
    return SP


@needs_stocks
async def test_selection_research(user: User):
    SP = await _open_selection(user, "res")
    user.find(marker="sp_res_factor").elements.pop().set_value("ret20")
    user.find(marker="sp_analyze").click()
    assert await _wait(lambda: SP.get("report"), n=1200)
    await user.should_see("IC 均值")
    user.find(marker="sp_overview").click()
    assert await _wait(lambda: SP.get("overview"), n=2400)
    assert len(SP["overview"][1]) >= 10


@needs_stocks
async def test_selection_backtest(user: User):
    SP = await _open_selection(user)
    user.find(marker="sp_run").click()
    assert await _wait(lambda: SP.get("result"), n=1200)
    res = SP["result"][0]
    assert len(res.schedule.picks) >= 20 and not res.orders.empty
    buys = res.orders[res.orders["side"] == "buy"]
    assert (buys["size"] % 100 == 0).all()
    await user.should_see("调仓次数")
    await user.should_see("可信度检查")


@needs_stocks
async def test_selection_cash_dividend(user: User):
    SP = await _open_selection(user)
    user.find(marker="sp_dividend").elements.pop().set_value("cash")
    await settle()
    assert SP["dividend"] == "cash"
    user.find(marker="sp_run").click()
    assert await _wait(lambda: SP.get("result"), n=1200)
    res, spec, _ = SP["result"]
    assert spec["dividend"] == "cash" and "dividend_cash" in res.metrics
    await user.should_see("收到现金分红")
    SP["dividend"] = "reinvest"


@needs_stocks
async def test_selection_run_script_menu(user: User):
    await _open_selection(user)
    from gui.pages.selection import run_script_dialog
    with user:
        run_script_dialog()      # 对话框能正常打开（上传后的运行流程见 tests/test_packaging.py）
    await user.should_see("选择导出的 .py 文件")


@needs_stocks
async def test_selection_custom_factor(user: User):
    from simplequant.stocks import FACTORS, custom_factors
    SP = await _open_selection(user, tab="cf")
    assert "def factor(p)" in SP["cf"]["code"]                      # 第一次打开放入带注释的示例
    user.find(marker="cf_trial").click()
    assert await _wait(lambda: SP.get("cf_trial"), n=600)
    await user.should_see("覆盖率")
    assert SP["cf_trial"][0] > 0.9

    user.find(marker="cf_save").click()                              # 没有名字不能保存
    assert not custom_factors.list_factors()
    user.find(marker="cf_name").type("乖离率")
    user.find(marker="cf_save").click()
    (key, d), = custom_factors.list_factors().items()
    assert d["name"] == "乖离率" and key in FACTORS
    await settle()
    SP["factors"] = [{"key": key, "weight": 1.0, "direction": 1}]
    SP["res"]["fkey"] = key

    user.find(marker="cf_code").elements.pop().set_value("def factor(p):\n    return p['nope']\n")
    user.find(marker="cf_trial").click()
    await user.should_see("第 2 行")

    user.find(marker=f"cf_del:{key}").click()
    await user.should_see(marker="confirm-delete")
    user.find(marker="confirm-delete").click()
    assert await _wait(lambda: key not in FACTORS)
    await settle()
    assert all(f["key"] in FACTORS for f in SP["factors"]) and SP["res"]["fkey"] == "ep"


SEL_AI_REPLY = {"name": "低估值高质量", "understood": "每月选 15 只 EP 高、ROE 高的股票，行业中性。",
                "unsupported": ["股息率"], "universe": "hs300",
                "factors": [{"key": "ep", "direction": "higher", "weight": 1}, {"key": "roe", "direction": "higher", "weight": 1}],
                "weighting": "manual", "top_n": 15, "rebalance": "monthly", "rebalance_days": 20, "exclude_st": True,
                "min_list_days": 250, "neutralize_industry": True, "neutralize_size": False, "position_pct": 95}


@needs_stocks
async def test_selection_ai_and_ic_weighting(user: User, monkeypatch):
    import json
    import simplequant.llm as llm

    class Fake:
        sent = []

        def chat(self, system, messages, schema=None):
            if schema is None:
                return "- 跑赢基准\n- 换手偏高"
            Fake.sent.append(messages[-1]["content"])
            return json.dumps(SEL_AI_REPLY, ensure_ascii=False)
    monkeypatch.setattr(llm, "load_config", lambda: llm.LLMConfig.from_preset("deepseek", api_key="x"))
    monkeypatch.setattr(llm, "get_provider", lambda cfg: Fake())
    SP = await _open_selection(user)
    user.find(marker="sp_ai_text").type("每月选15只便宜、ROE高的，行业分散，最好高股息")
    user.find(marker="sp_ai_go").click()
    assert await _wait(lambda: SP.get("ai"))
    await user.should_see("股息率")
    user.find(marker="sp_ai_apply").click()
    await settle()
    assert [f["key"] for f in SP["factors"]] == ["ep", "roe"] and SP["top_n"] == 15 and SP["ni"] is True
    # 在当前方案基础上修改：表单里的方案随描述一起发给模型
    user.find(marker="sp_ai_base").elements.pop().set_value(True)
    user.find(marker="sp_ai_go").click()
    assert await _wait(lambda: len(Fake.sent) == 2)
    assert Fake.sent[-1].startswith("Current strategy:") and '"roe"' in Fake.sent[-1]
    user.find(marker="sp_weighting").elements.pop().set_value("ic")
    user.find(marker="sp_run").click()
    assert await _wait(lambda: SP.get("result"), n=1200)
    res, spec, _ = SP["result"]
    assert spec["weighting"] == "ic" and spec["neutralize"]["industry"]
    await user.should_see(marker="sp_explain")                     # AI 解读选股回测结果
    user.find(marker="sp_explain").click()
    assert await _wait(lambda: SP.get("explanation"))
    from simplequant.stocks import StockStore
    ind = StockStore().load_industry()
    for codes in list(res.schedule.picks.values())[-5:]:
        assert ind.reindex(codes).nunique() >= 5                       # 每期分散在多个行业


def _cb_ready():
    from simplequant.stocks import universe
    return universe.ready("cb")


needs_cb = pytest.mark.skipif(not _cb_ready(), reason="可转债数据未下载")


async def _open_cb(user: User, tab="bt"):
    """切换到「可转债（全市场）」，从 2021 年开始"""
    import datetime as dt
    from gui.pages.selection import sp_state
    SP = sp_state()
    SP["ranges"]["cb"] = (dt.date(2021, 1, 4), dt.date(2100, 1, 1))
    state.STATE["sp_tab"] = tab
    await user.open("/selection")
    assert await _wait(lambda: state.STATE.get("sp_panel_key") is not None, n=1200)
    await settle()
    state.STATE["sp_panel_key"] = None
    user.find(marker="sp_universe").elements.pop().set_value("cb")
    assert await _wait(lambda: (state.STATE.get("sp_panel_key") or (None,))[0] == "cb", n=1200), "转债面板没有载入"
    await settle()
    return SP


@needs_cb
async def test_selection_cb_backtest(user: User):
    SP = await _open_cb(user)
    assert [f["key"] for f in SP["factors"]] == ["cb_double_low"]      # 切换后默认「双低」
    assert SP["excl_st"] is False and SP["min_list"] == 0
    user.find(marker="cb_max_price").elements.pop().set_value(125)
    await settle()
    assert SP["cbf"]["max_price"] == 125
    user.find(marker="sp_run").click()
    assert await _wait(lambda: SP.get("result"), n=1200)
    res, spec, _ = SP["result"]
    assert spec["universe"] == "cb" and spec["filters"]["max_price"] == 125 and "dividend" not in spec
    assert (res.orders["size"] % 10 == 0).all() and (res.orders["size"] % 100 != 0).any()
    await user.should_see("中证转债指数")
    # 切回股票池：股票的因子和条件还在
    if _hs300_ready():
        user.find(marker="sp_universe").elements.pop().set_value("hs300")
        await settle()
        assert SP["excl_st"] is True and "cb_double_low" not in [f["key"] for f in SP["factors"]]


@needs_cb
@needs_stocks
async def test_selection_load_saved_cb_strategy_switches_universe(user: User):
    """在沪深300下载入可转债策略：股票池下拉框、面板、因子和条件都换成可转债的"""
    strategies.save_strategy("转债双低", {
        "kind": "selection", "universe": "cb", "factors": [{"key": "cb_double_low", "weight": 1, "direction": -1}],
        "top_n": 15, "rebalance": "monthly", "position_pct": 95,
        "filters": {"exclude_st": False, "min_list_days": 0, "max_price": 120.0, "min_amount": None}})
    SP = await _open_selection(user)
    user.find(marker="sp_load_saved").click()
    assert await _wait(lambda: (state.STATE.get("sp_panel_key") or (None,))[0] == "cb", n=1200)
    await settle()
    assert user.find(marker="sp_universe").elements.pop().value == "cb"
    assert SP["top_n"] == 15 and SP["cbf"]["max_price"] == 120 and SP["cbf"]["min_amount"] is None
    assert [f["key"] for f in SP["factors"]] == ["cb_double_low"]


@needs_cb
async def test_selection_cb_research_and_data_tab(user: User):
    SP = await _open_cb(user, "res")
    assert SP["res"]["fkey"].startswith("cb_") or SP["res"]["fkey"] in ("ret5", "ret20")
    user.find(marker="sp_res_factor").elements.pop().set_value("cb_double_low")
    user.find(marker="sp_analyze").click()
    assert await _wait(lambda: SP.get("report"), n=1200)
    await user.should_see("IC 均值")
    state.STATE["sp_tab"] = "data"
    await user.open("/selection")
    await user.should_see("可转债总数")
    await user.should_see("回测规则说明")


@needs_stocks
async def test_selection_walkforward(user: User):
    from gui.pages.selection import sp_state
    SP = sp_state()
    SP["wf_cfg"].update(workers=1, train=12, test=12)
    SP = await _open_selection(user, "wf")
    W = SP["wf_cfg"]
    W["ranges"]["top_n"] = (5, 13, 8)                  # 缩小网格：持股数 5 / 13，只按月调仓
    W["reb"] = ["monthly"]
    ctx_reload = user.find(marker="spwf_workers").elements.pop()
    ctx_reload.set_value(1)                            # 触发重新估算
    await user.should_see("每组仅需完整回测一次")
    user.find(marker="spwf_run").click()
    assert await _wait(lambda: SP.get("wf"), n=3000)
    res = SP["wf"]["res"]
    assert len(res.windows) >= 2 and len(res.grid) == 2
    await user.should_see("滚动效率 WFE")
    user.find(marker="spwf_apply").click()
    await settle()
    assert SP["top_n"] == int(res.windows["top_n"].iloc[-1])


# ---------------- 设置 ----------------
async def test_settings_preset_and_save(user: User, monkeypatch, tmp_path):
    import simplequant.llm as llm
    from simplequant.llm.config import load_config as real_load
    real_save = llm.save_config
    monkeypatch.setattr(llm, "load_config", lambda: None)
    monkeypatch.setattr(llm, "save_config", lambda cfg: real_save(cfg, tmp_path / "llm.json"))
    await user.open("/settings")
    assert user.find(marker="llm_model").elements.pop().value == "claude-opus-5-5"
    user.find(marker="llm_preset").elements.pop().set_value("deepseek")
    assert user.find(marker="llm_base_url").elements.pop().value == "https://api.deepseek.com"
    assert user.find(marker="llm_json_mode").elements.pop().value == "json_object"
    user.find(marker="llm_api_key").elements.pop().set_value("sk-test")
    user.find(marker="llm_save").click()
    await user.should_see("llm.json")
    cfg = real_load(tmp_path / "llm.json")
    assert cfg.provider == "openai" and cfg.model == "deepseek-chat" and cfg.api_key == "sk-test"


async def test_settings_update_section_and_banner(user: User, monkeypatch):
    """设置页：版本、检查更新；安装版下载好新版本后，其他页面顶部显示提示条，「稍后」可隐藏"""
    from simplequant.update.client import UPDATER, Status
    monkeypatch.setattr(UPDATER, "status", Status(state="latest"))
    checks, applied = [], []
    monkeypatch.setattr(UPDATER, "check_async", lambda: checks.append(1))
    await user.open("/settings")
    await user.should_see(marker="version")
    await user.should_see("当前已是最新版本")
    assert user.find(marker="link:set.issues").elements.pop().props["href"].endswith("/issues")
    user.find(marker="update_check").click()
    assert checks

    monkeypatch.setattr(UPDATER, "frozen", True)
    monkeypatch.setattr(UPDATER, "system", "windows")          # macOS / Linux 还要检查程序目录能否写入
    monkeypatch.setattr(UPDATER, "status", Status(state="ready", version="9.9.9", notes="- 修复"))
    monkeypatch.setattr(UPDATER, "apply", lambda: applied.append(1) or False)
    await user.open("/")
    await user.should_see("新版本 9.9.9 已下载完成")
    user.find(marker="update_restart").click()
    assert applied
    user.find(marker="update_later").click()
    await settle()
    await user.should_not_see(marker="update_restart")


# ---------------- 模拟盘 ----------------
@pytest.fixture
def paper_dir(tmp_path, monkeypatch):
    """模拟账户写到临时目录；不查询、不创建真实的 Windows 定时任务"""
    from simplequant.paper import account as account_mod, schedule
    monkeypatch.setattr(account_mod, "PAPER_ROOT", tmp_path / "paper")
    monkeypatch.setattr(schedule, "task_exists", lambda: False)
    monkeypatch.setattr(schedule, "create_task", lambda at="18:30": (True, "ok"))
    return tmp_path / "paper"


async def test_paper_single_account_flow(user: User, lib_dir, paper_dir, monkeypatch):
    """从过去某天开始的单标的模拟账户：创建 → 运行 → 信号/持仓/资产曲线（行情用本地数据库代替下载）"""
    from simplequant.paper import runner, list_accounts
    meta = _save_prices(n=700)

    def fake_refresh(acc, today=None):
        (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
        out = {}
        for a in acc.assets:
            df = library.load(meta.id)
            df.to_parquet(acc.dir / "prices" / f"{a['symbol']}.parquet")
            out[a["name"]] = df
        return out
    monkeypatch.setattr(runner, "refresh_single_data", fake_refresh)
    runs = []
    real_run_all = routes()["/paper"].__globals__["run_all"]

    def counting_run_all(*a, **kw):
        out = real_run_all(*a, **kw)
        runs.append(out)
        return out
    patch_page(monkeypatch, "/paper", "run_all", counting_run_all)

    await user.open("/paper")
    user.find(marker="pp_strategy").elements.pop().set_value("tpl:sma_cross")
    await settle()
    user.find(marker="pp_assets").elements.pop().set_value([meta.id])
    await settle()
    user.find(marker="pp_mode").elements.pop().set_value("past")
    user.find(marker="pp_past").elements.pop().set_value(str(meta.start if hasattr(meta, "start") else "2022-01-03"))
    user.find(marker="pp_name").elements.pop().set_value("均线模拟")
    user.find(marker="pp_create").click()
    assert await _wait(lambda: any("完成" in m for m in user.notify.messages), n=600)
    accs = list_accounts()
    assert len(accs) == 1 and not accs[0].fills().empty
    await user.should_see("总资产")
    await user.should_see("信号执行日")
    await settle()
    # 再运行一次（没有新数据）：账本不变、无不一致提示
    n = len(accs[0].fills())
    user.find(marker="pp_run_this").click()
    assert await _wait(lambda: len(runs) >= 2), (len(runs), user.notify.messages)
    assert not any(runs[-1].values())                    # 没有失败的账户
    assert len(list_accounts()[0].fills()) == n and not list_accounts()[0].state()["divergence"]
    # 暂停、删除（先确认）
    await settle()
    user.find(marker="pp_pause").click()
    await settle()
    assert list_accounts()[0].status == "paused"
    user.find(marker="pp_delete").click()
    await user.should_see(marker="pp_delete_yes")
    user.find(marker="pp_delete_yes").click()
    assert await _wait(lambda: not list_accounts())


async def test_home_paper_warnings(user: User, paper_dir, monkeypatch):
    """首页模拟账户卡片：没有定时任务、上次运行失败、多日未更新时提示；模拟盘页显示失败原因"""
    from simplequant.engine import BrokerConfig
    from simplequant.paper import create_account
    spec = {"kind": "template", "template": "buy_hold", "params": {}}
    asset = [{"source": "akshare", "symbol": "510300", "asset": "etf", "name": "510300"}]
    old = create_account("旧账户", spec, BrokerConfig(), "2026-01-05", assets=asset)
    old.data_through = "2026-01-05"
    old.save()
    bad = create_account("出错账户", spec, BrokerConfig(), "2026-01-05", assets=asset)
    bad.data_through = "2026-01-05"
    bad.save()
    bad.save_error("ValueError: 数据源断开")
    cal = pd.bdate_range("2026-01-01", "2026-01-31")
    from simplequant.paper import health, schedule
    monkeypatch.setattr(health, "latest_expected_day", lambda c, now=None: pd.Timestamp("2026-01-20"))
    patch_page(monkeypatch, "/", "load_calendar", lambda refresh_if_stale=True: cal)
    await user.open("/")
    await user.should_see(marker="paper_warn:no_task")
    await user.should_see("「旧账户」已有 11 个交易日未更新")
    await user.should_see("「出错账户」上次运行失败")
    await user.should_see("数据源断开")
    # 有定时任务、没有异常时不提示
    monkeypatch.setattr(schedule, "task_exists", lambda: True)
    bad.clear_error()
    old.data_through = bad.data_through = "2026-01-20"
    old.save()
    bad.save()
    await user.open("/")
    await user.should_see(marker="paper_warn")
    await asyncio.sleep(0.3)
    await user.should_not_see(marker="paper_warn:no_task")
    await user.should_not_see(marker="paper_warn:stale")
    # 模拟盘页：失败原因
    bad.save_error("ValueError: 数据源断开")
    state.STATE.setdefault("pp", {"kind": "single", "strategy": None, "assets": [], "sel": None, "mode": "now",
                                  "past": "2026-01-05", "name": "", "account": bad.id, "task_time": "19:00"})
    patch_page(monkeypatch, "/paper", "load_calendar", lambda refresh_if_stale=True: cal)
    await user.open("/paper")
    await user.should_see(marker="pp_failed")


async def test_paper_single_cash_dividend_account(user: User, lib_dir, paper_dir, monkeypatch):
    """单标的模拟账户选「现金分红」：账户记住设置，页面显示累计分红和没能改用现金分红的标的"""
    import json
    from simplequant.data import cash_dividend as cd
    from simplequant.paper import runner, list_accounts
    meta = _save_prices(n=700)

    def fake_refresh(acc, today=None):
        (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
        df = library.load(meta.id)
        ev = pd.DataFrame({"ex_date": [df.index[650]], "cash": [0.3], "bonus": [0.0], "reserve": [0.0]})
        df = cd.build(df, df, ev, taxed=False)
        out = {}
        for a in acc.assets:
            df.to_parquet(acc.dir / "prices" / f"{a['symbol']}.parquet")
            out[a["name"]] = df
        (acc.dir / "prices" / "dividend_notes.json").write_text(
            json.dumps({"skipped": {"某指数": "index"}, "patched": {}}), encoding="utf-8")
        return out
    monkeypatch.setattr(runner, "refresh_single_data", fake_refresh)

    await user.open("/paper")
    user.find(marker="pp_strategy").elements.pop().set_value("tpl:buy_hold")
    await settle()
    user.find(marker="pp_assets").elements.pop().set_value([meta.id])
    await settle()
    user.find(marker="pp_single_dividend").elements.pop().set_value("cash")
    user.find(marker="pp_mode").elements.pop().set_value("past")
    user.find(marker="pp_past").elements.pop().set_value(str(library.load(meta.id).index[600].date()))
    user.find(marker="pp_name").elements.pop().set_value("现金分红模拟")
    user.find(marker="pp_create").click()
    assert await _wait(lambda: any("完成" in m for m in user.notify.messages), n=600)
    acc = list_accounts()[0]
    assert acc.broker["dividend"] == "cash" and acc.state()["metrics"]["dividend_cash"] > 0
    await user.should_see(marker="pp_dividends")
    await user.should_see("指数不分红")


@needs_stocks
async def test_paper_selection_account_flow(user: User, paper_dir):
    from simplequant.paper import list_accounts
    spec = {"kind": "selection", "universe": "hs300", "factors": [{"key": "ep", "weight": 1, "direction": 1}],
            "top_n": 10, "rebalance": "monthly", "filters": {"exclude_st": True, "min_list_days": 250},
            "position_pct": 95}
    strategies.save_strategy("__test_sel__", spec)
    state.STATE["pp"] = {"kind": "selection", "strategy": None, "assets": [], "sel": "__test_sel__", "mode": "past",
                         "past": "2025-06-03", "name": "sel", "account": None, "task_time": "19:00"}
    await user.open("/paper")
    user.find(marker="pp_create").click()
    # 等运行结束的提示再读账本（运行中去读，可能读到写了一半的文件）
    assert await _wait(lambda: any("完成" in m for m in user.notify.messages), n=2400)
    acc = list_accounts()[0]
    fills = acc.fills()
    assert (fills.loc[fills["side"] == "buy", "size"] % 100 == 0).all()
    assert acc.state()["positions"]


# ---------------- 资产配置 ----------------
async def test_allocation_flow(user: User, lib_dir):
    from simplequant import allocation as A
    _save_prices("511010", n=600, name="国债ETF")
    _save_prices("510300", n=600, name="沪深300ETF")
    cfg = A.load_config()
    cfg["enabled"] = ["cash", "hold_511010", "hold_510300"]
    A.save_config(cfg)

    await user.open("/allocation")
    await user.should_see("风险测评")
    user.find(marker="al_submit").click()
    await user.should_see("尚有 10 题未作答")                    # 未答完不能提交
    for q in A.QUESTIONS:
        user.find(marker=f"al_q:{q['key']}").elements.pop().set_value(2)
    user.find(marker="al_submit").click()
    await settle()
    prof = A.load_profile()
    assert prof is not None and prof.level == A.evaluate({q["key"]: 2 for q in A.QUESTIONS}).level
    await user.should_see("风险承受能力：")

    await user.should_not_see(marker="al_download")            # 选用的候选数据都在本地
    user.find(marker="al_run").click()
    AL = state.STATE["al"]
    assert await _wait(lambda: AL.get("result"))
    r = AL["result"]
    assert set(r["R"].columns) == {"cash", "hold_511010", "hold_510300"} and not r["errors"]
    assert np.isclose(sum(r["weights"].values()), 1)
    await user.should_see("参考配置")
    await user.should_see("可信度检查")
    await user.should_see("权重与贡献")

    # 手动调整权重后重新回测，再恢复参考配置
    user.find(marker="al_w:hold_510300").elements.pop().set_value(100)
    user.find(marker="al_w:hold_511010").elements.pop().set_value(0)
    user.find(marker="al_w:cash").elements.pop().set_value(0)
    user.find(marker="al_rerun").click()
    assert await _wait(lambda: AL["result"]["manual"])
    assert AL["result"]["weights"]["hold_510300"] == 1.0
    await user.should_see("手动调整后的权重")
    user.find(marker="al_reset").click()
    assert await _wait(lambda: not AL["result"]["manual"])


async def test_allocation_to_portfolio_paper_account(user: User, lib_dir, paper_dir, monkeypatch):
    """资产配置页按当前权重开设组合模拟账户 → 模拟盘页显示各成分、再平衡（行情用本地数据库代替下载）"""
    import simplequant.paper as P
    from simplequant import allocation as A
    from simplequant.paper import portfolio as PF, runner, calendar as C, list_accounts
    _save_prices("511010", n=600, name="国债ETF")
    _save_prices("510300", n=600, name="沪深300ETF")
    cfg = A.load_config()
    cfg["enabled"] = ["cash", "hold_511010", "hold_510300"]
    A.save_config(cfg)
    A.save_profile(A.preset_profile(3))
    idx = library.load("x_510300").index
    cal = idx.append(pd.bdate_range(idx[-1] + pd.Timedelta(days=1), periods=60))

    def fake_refresh(acc, store=None, done=None, calendar=None, selection=True):
        (acc.dir / "prices").mkdir(parents=True, exist_ok=True)
        for s in acc.spec["sleeves"]:
            if s.get("symbol"):
                library.load(f"x_{s['symbol']}").to_parquet(acc.dir / "prices" / f"{s['symbol']}.parquet")
    monkeypatch.setattr(PF, "refresh_data", fake_refresh)
    monkeypatch.setattr(P, "load_calendar", lambda *a, **kw: cal)
    monkeypatch.setattr(runner, "load_calendar", lambda: cal)
    monkeypatch.setattr(C, "latest_expected_day", lambda c, now=None: idx[-1])

    await user.open("/allocation")
    user.find(marker="al_run").click()
    assert await _wait(lambda: state.STATE["al"].get("result"))
    await settle()
    user.find(marker="al_paper").elements.pop().set_value(True)
    await settle()
    user.find(marker="al_pf_mode").elements.pop().set_value("past")
    user.find(marker="al_pf_past").elements.pop().set_value(str(idx[300].date()))
    user.find(marker="al_pf_name").elements.pop().set_value("稳健组合")
    user.find(marker="al_pf_create").click()
    assert await _wait(lambda: any("已创建组合模拟账户" in m for m in user.notify.messages), n=600), \
        user.notify.messages
    acc = list_accounts()[0]
    st = acc.state()
    assert acc.kind == "portfolio" and acc.start == str(idx[300].date()) and st["as_of"] == str(idx[-1].date())
    assert not acc.rebalances().empty and {r["id"] for r in st["sleeves"]} <= {"cash", "hold_511010", "hold_510300"}
    weights = state.STATE["al"]["result"]["weights"]
    assert acc.spec["weights"] == pytest.approx({k: v / sum(weights.values()) for k, v in weights.items() if v > 0})

    await user.open("/paper")
    await user.should_see("资产配置组合")
    await user.should_see("再平衡方式：每季度")
    await user.should_see("开盘待执行交易")
    # 新建账户选「资产配置组合」时引导到资产配置页
    user.find(marker="pp_kind").elements.pop().set_value("portfolio")
    await user.should_see(marker="pp_go_allocation")


async def test_allocation_add_candidate_and_missing_data(user: User, lib_dir):
    from simplequant import allocation as A
    _save_prices("510300", n=300)
    A.save_profile(A.preset_profile(3))
    await user.open("/allocation")
    await user.should_see("下载缺少的行情")                      # 默认候选中有本地没有的 ETF
    user.find(marker="al_add").elements.pop().set_value(True)
    await settle()
    user.find(marker="al_add_kind").elements.pop().set_value("timing")
    await settle()
    user.find(marker="al_add_btn").click()
    await user.should_see("请选择标的与策略")
    user.find(marker="al_add_symbol").elements.pop().set_value("510300")
    user.find(marker="al_add_strategy").elements.pop().set_value("tpl:sma_cross")
    user.find(marker="al_add_btn").click()
    await settle()
    custom = A.load_config()["custom"]
    assert len(custom) == 1 and custom[0]["kind"] == "timing" and custom[0]["symbol"] == "510300"
    assert custom[0]["id"] in A.load_config()["enabled"]
