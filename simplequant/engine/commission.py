"""
A 股交易成本模型
- 佣金：按成交额比例，单笔最低 min_commission 元
- 印花税：仅卖出收取（股票 0.05%，ETF/债券 0）
"""

import backtrader as bt

from ..i18n import L

# 界面里的预设
COST_PRESETS = {
    "etf": {"label": L("ETF / 基金", "ETF / fund"),
            "commission": 0.0002, "min_commission": 5.0, "stamp_duty": 0.0},
    "stock": {"label": L("A股股票", "A-share stock"),
              "commission": 0.00025, "min_commission": 5.0, "stamp_duty": 0.0005},
    "bond": {"label": L("债券 / 可转债", "Bond / convertible"),
             "commission": 0.00005, "min_commission": 0.0, "stamp_duty": 0.0},
    "zero": {"label": L("零成本（理想情况）", "Zero cost (ideal)"),
             "commission": 0.0, "min_commission": 0.0, "stamp_duty": 0.0},
}


class AShareCommission(bt.CommInfoBase):
    params = (
        ("commission", 0.0002),
        ("min_commission", 5.0),
        ("stamp_duty", 0.0),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),   # commission 直接写小数（0.0002 = 万二）
    )

    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        comm = max(value * self.p.commission, self.p.min_commission) if value > 0 else 0.0
        if size < 0:  # 卖出
            comm += value * self.p.stamp_duty
        return comm
