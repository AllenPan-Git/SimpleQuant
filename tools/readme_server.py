"""
README 截图用的界面服务：python tools/readme_server.py zh|en light|dark 端口 [cb|up]
- 界面偏好、模拟账户都放在临时目录，不读写你自己的偏好、历史和模拟账户
- 预先放入：一次 RSI 回测（回测页、首页「继续上次的工作」）、两条最近的报告、一个运行了一段时间的模拟账户、
  一次资产配置（风险测评 C3、默认候选）
- 加 cb：选股页的股票池改为「可转债（全市场）」，使用可转债的默认因子与条件
- 加 up：模拟账户改用 RSI 超买超卖（截至 2026-09-30 为盈利），并视为已设置每日自动运行（首页不显示提示）
- 行情数据读本机数据库（只读）：需要 510300 后复权日线、沪深300选股数据，以及资产配置默认候选的 ETF 日线；
  加 cb 时还需要可转债数据
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

LANG = sys.argv[1] if len(sys.argv) > 1 else "zh"
THEME = sys.argv[2] if len(sys.argv) > 2 else "light"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8780
CB = len(sys.argv) > 4 and sys.argv[4] == "cb"
UP = len(sys.argv) > 4 and sys.argv[4] == "up"
TMP = Path(tempfile.mkdtemp(prefix="sq_readme_"))
os.environ["NICEGUI_STORAGE_PATH"] = str(TMP / "storage")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from nicegui import app  # noqa: E402
from gui.app import run  # noqa: E402
from gui import state, history  # noqa: E402
from simplequant import strategies  # noqa: E402
from simplequant.data import library  # noqa: E402
from simplequant.engine import run_backtest, BrokerConfig  # noqa: E402
from simplequant.engine.credibility import check_backtest  # noqa: E402
from simplequant.paper import account as account_mod  # noqa: E402
from simplequant.paper import create_account, run_account, load_calendar  # noqa: E402
from ui import shared  # noqa: E402

account_mod.PAPER_ROOT = TMP / "paper"
ZH = LANG == "zh"
ID = next(m.id for m in sorted(library.list_datasets(), key=lambda m: m.end, reverse=True)
          if m.symbol == "510300" and m.source == "akshare" and m.freq == "1d" and m.adjust)
DF = library.load(ID)

# ---- 回测：RSI 超买超卖（默认参数） ----
rng = (dt.date(2020, 1, 2), dt.date(2026, 9, 29))     # 区间内 RSI 跑赢买入持有
prices = shared.slice_prices({"510300": DF}, rng)
spec = {"kind": "template", "template": "rsi_reversion", "params": {}}
cls, params = strategies.resolve(spec)
broker = BrokerConfig(cash=100000, commission=0.00005, min_commission=0, stamp_duty=0, slippage=0.0005)
res = run_backtest(prices, cls, params, broker, with_panels=True)
name = "RSI 超买超卖" if ZH else "RSI mean reversion"
state.STATE["bt"] = dict(result=res, title=("模板：RSI 超买超卖" if ZH else "Template: RSI mean reversion", ["510300"], "1d"),
                         spec=spec, broker=broker, name=name, time="2026-09-30 21:14",
                         checks=check_backtest(res.metrics, res.trades))
for key in ("bt", "opt"):
    state.STATE[f"{key}_ids"] = [ID]
    state.STATE[f"{key}_range"] = rng
    state.STATE[f"{key}_strategy"] = "tpl:rsi_reversion"

# ---- 模拟盘：从 2025-10 起运行的账户（默认双均线，加 up 为 RSI） ----
cal = load_calendar(refresh_if_stale=False)
if UP:
    acc_name, acc_spec = "沪深300ETF RSI" if ZH else "CSI 300 ETF · RSI", {"kind": "template", "template": "rsi_reversion",
                                                                          "params": {}}
    from simplequant.paper import schedule  # noqa: E402
    schedule.task_exists = lambda: True
else:
    acc_name, acc_spec = "沪深300ETF 双均线" if ZH else "CSI 300 ETF · SMA", {"kind": "template", "template": "sma_cross",
                                                                            "params": {"fast": 10, "slow": 30}}
acc = create_account(acc_name, acc_spec, BrokerConfig(cash=100000), "2025-10-09",
                     assets=[{"source": "akshare", "symbol": "510300", "asset": "etf", "name": "510300"}])
history_df = DF.loc[:"2026-09-30"]
for end in ("2026-03-31", "2026-06-30", "2026-09-30"):        # 分几次推进，和每天运行的效果一样
    run_account(acc, cal, prices={"510300": history_df.loc[:end]})


# ---- 资产配置：平衡型（可承受回撤 10%），默认候选 ----
from simplequant import allocation  # noqa: E402
allocation.ROOT = TMP / "allocation"
answers = {q["key"]: min(2, len(q["options"]) - 1) for q in allocation.QUESTIONS}
answers["loss"] = 1
prof = allocation.evaluate(answers)
allocation.save_profile(prof)
al_cfg = allocation.load_config()
al_sleeves = [s for s in allocation.all_sleeves(al_cfg) if s["id"] in al_cfg["enabled"]]
al_R, al_err = allocation.load_returns(al_sleeves)
al_used = [s for s in al_sleeves if s["id"] in al_R.columns]
al_sug = allocation.suggest(prof.level, prof.max_dd, al_used, al_R, al_cfg["rebalance"])
state.STATE["al"] = {"answers": dict(answers), "result": dict(
    R=al_R, sleeves=al_used, errors=al_err, profile=prof, rebalance=al_cfg["rebalance"], suggestion=al_sug,
    weights=dict(al_sug["weights"]), res=al_sug["result"], shifted=al_sug["shifted"], manual=False)}


# ---- 选股页：可转债（全市场） ----
if CB:
    import gui.pages.selection as selection_page  # noqa: E402
    sp = selection_page.sp_state()
    selection_page.switch_kind(sp, "stock", "cb")
    sp["universe"] = "cb"


# 数据库里的行情只到某一天：让模拟盘页以这天为「最新应有数据」，不显示「数据未更新」的提示
import pandas as pd  # noqa: E402
import gui.pages.paper as paper_page  # noqa: E402
paper_page.latest_expected_day = lambda calendar: pd.Timestamp(acc.data_through)


@app.on_startup
def seed():
    app.storage.general["lang"] = LANG
    app.storage.general["dark"] = THEME == "dark"
    app.storage.general["recent"] = []
    history.add("sel", "沪深300 低估值 + 反转" if ZH else "CSI 300 value + reversal",
                ("沪深300" if ZH else "CSI 300") + " · 2020-01-02 ~ 2026-09-28",
                {"total_return": 0.216, "sharpe": 0.94}, data="沪深300" if ZH else "CSI 300", strategy="")
    history.add("bt", name, "510300 · " + ("日线" if ZH else "Daily") + " · 2020-01-02 ~ 2026-09-29", res.metrics,
                data="510300", strategy=name)


run(native=False, port=PORT, show=False, check_updates=False)
