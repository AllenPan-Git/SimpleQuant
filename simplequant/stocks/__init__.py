"""多因子选股：数据、因子、研究、选股回测"""

from .store import StockStore, UNIVERSES, today
from .panel import Panel, build_panel
from .factors import (FACTORS, GROUPS, compute, preprocess, composite, factor_zscores, neutralize,
                      factor_correlation)
from . import custom_factors
from .research import analyze, FactorReport, sample_dates
from .selection import (build_schedule, run_selection, rebalance_dates, rebalance_horizon, REBALANCE, WEIGHTING,
                        DIVIDEND, DEFAULT_FILTERS, SelectionResult)

custom_factors.load_all()          # 用户保存的自定义因子登记进 FACTORS
