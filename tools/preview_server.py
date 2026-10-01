"""界面样式预览（开发用）：python tools/preview_server.py light|dark 端口；独立存储目录，预先放入一次回测结果和历史，不影响真实偏好 / 历史"""
import os
import sys
import datetime as dt

SP = os.path.join(os.environ.get("TEMP", "."), "sq_preview")
os.environ["NICEGUI_STORAGE_PATH"] = os.path.join(SP, "storage")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from nicegui import app  # noqa: E402
from gui.app import run  # noqa: E402
from gui import state, history  # noqa: E402
from simplequant import strategies  # noqa: E402
from simplequant.data import library  # noqa: E402
from simplequant.engine import run_backtest, BrokerConfig  # noqa: E402
from ui import shared  # noqa: E402

DARK = sys.argv[1] if len(sys.argv) > 1 else "light"
ID = "akshare_510300_1d_hfq_2020-01-01_2026-09-30"
rng = (dt.date(2024, 8, 1), dt.date(2026, 9, 29))
prices = shared.slice_prices({"510300": library.load(ID)}, rng)
spec = {"kind": "template", "template": "sma_cross", "params": {}}
cls, params = strategies.resolve(spec)
broker = BrokerConfig(cash=100000, commission=0.00005, min_commission=0, stamp_duty=0, slippage=0.0005)
res = run_backtest(prices, cls, params, broker, with_panels=True)
state.STATE["bt"] = dict(result=res, title=("模板：双均线", ["510300"], "1d"), spec=spec, broker=broker,
                         name="双均线交叉", time="2026-09-30 21:14")
state.STATE["bt_ids"] = [ID]
state.STATE["bt_range"] = rng
state.STATE["bt_strategy"] = "tpl:sma_cross"


@app.on_startup
def seed():
    app.storage.general["dark"] = {"light": False, "dark": True}[DARK]
    app.storage.general["recent"] = []
    history.add("sel", "沪深300 低估值 + 反转", "沪深300 · 2020-01-02 ~ 2026-09-28",
                {"total_return": 0.216, "sharpe": 0.94}, data="沪深300", strategy="沪深300 低估值 + 反转")
    history.add("bt", "双均线交叉", "510300 · 日线 · 2024-08-01 ~ 2026-09-29", res.metrics,
                data="510300", strategy="双均线交叉")


run(native=False, port=int(sys.argv[2]) if len(sys.argv) > 2 else 8766, show=False)
