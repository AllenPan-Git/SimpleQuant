"""
中英双语支持
- 带多语言的元数据（指标名、模板名等）直接写成 {"zh": ..., "en": ...}，用 pick() 取值
- 其余文字（错误、日志、指标名、列名……）集中在 TEXT 表中，用 tr(key, lang, **格式化参数) 取值
界面层的文字在 ui/texts.py，会合并进 TEXT。
"""

LANGS = {"zh": "中文", "en": "English"}
DEFAULT_LANG = "zh"


def pick(obj, lang: str = DEFAULT_LANG):
    if isinstance(obj, dict) and ("zh" in obj or "en" in obj):
        return obj.get(lang) or obj.get(DEFAULT_LANG) or ""
    return obj


def tr(key: str, lang: str = DEFAULT_LANG, **kw) -> str:
    entry = TEXT.get(key)
    s = pick(entry, lang) if entry is not None else key
    return s.format(**kw) if kw else s


def render(key: str, kw: dict | None = None, lang: str = DEFAULT_LANG) -> str:
    """渲染结构化消息；参数值可以是嵌套的 (文本键, 参数) 元组，例如日志里的下单理由"""
    if not key:
        return ""
    args = {k: render(*v, lang=lang) if isinstance(v, tuple) else v for k, v in (kw or {}).items()}
    return tr(key, lang, **args).strip()


def L(zh: str, en: str) -> dict:
    return {"zh": zh, "en": en}


TEXT = {
    # ---- 绩效指标 ----
    "m.initial_cash": L("初始资金", "Initial cash"),
    "m.final_value": L("最终资产", "Final value"),
    "m.total_return": L("总收益率", "Total return"),
    "m.cagr": L("年化收益率", "CAGR"),
    "m.volatility": L("年化波动率", "Volatility (ann.)"),
    "m.sharpe": L("夏普比率", "Sharpe ratio"),
    "m.max_drawdown": L("最大回撤", "Max drawdown"),
    "m.calmar": L("卡玛比率", "Calmar ratio"),
    "m.trades": L("交易次数", "Trades"),
    "m.limit_blocked": L("因停牌或涨跌停未成交", "Orders blocked by limits or suspensions"),
    "m.win_rate": L("胜率", "Win rate"),
    "m.profit_factor": L("盈亏比", "Profit factor"),
    "m.benchmark_return": L("基准收益率", "Benchmark return"),
    "m.excess_return": L("超额收益", "Excess return"),
    "m.invested": L("累计投入", "Total invested"),
    "m.contributions": L("追加投入次数", "Contributions"),
    "m.profit": L("累计盈亏", "Profit"),
    "m.xirr": L("年化收益率（XIRR）", "Annualized return (XIRR)"),
    "m.benchmark_xirr": L("基准年化收益率（XIRR）", "Benchmark annualized return (XIRR)"),
    "m.twr": L("时间加权收益率", "Time-weighted return"),

    # ---- 成交 / 交易记录列名 ----
    "col.time": L("时间", "Time"),
    "col.symbol": L("标的", "Symbol"),
    "col.side": L("方向", "Side"),
    "col.size": L("数量", "Shares"),
    "col.price": L("成交价", "Price"),
    "col.value": L("成交额", "Value"),
    "col.commission": L("手续费", "Fees"),
    "col.open_time": L("开仓时间", "Opened"),
    "col.close_time": L("平仓时间", "Closed"),
    "col.bars": L("持仓K线数", "Bars held"),
    "col.pnl": L("毛利润", "Gross P&L"),
    "col.pnl_net": L("净利润", "Net P&L"),
    "col.event": L("事件", "Event"),
    "side.buy": L("买入", "Buy"),
    "side.sell": L("卖出", "Sell"),

    # ---- 运行日志 ----
    "log.buy": L("买入信号 {name} {size}股 {reason}", "Buy signal {name} {size} sh {reason}"),
    "log.sell": L("卖出信号 {name} {reason}", "Sell signal {name} {reason}"),
    "log.lot_too_big": L("{name} 出现买入信号，但分配的资金不足以买入一手（约 {cost:,.0f} 元），未下单",
                         "Buy signal for {name}, but its share of capital cannot cover one lot (about {cost:,.0f}); no order placed"),
    "log.t1": L("T+1 限制：{name} 为当日买入，暂不可卖出", "T+1: {name} was bought today and cannot be sold yet"),
    "log.blocked_buy": L("{name} {why}，买入委托未成交，已作废", "{name} {why}; buy order not filled and canceled"),
    "log.blocked_sell": L("{name} {why}，卖出委托未成交，下一交易日开盘重试",
                          "{name} {why}; sell order not filled, retrying at the next open"),
    "limit.limit_up": L("开盘涨停", "opened limit-up"),
    "limit.limit_down": L("开盘跌停", "opened limit-down"),
    "limit.suspended": L("停牌", "suspended"),
    "log.order_failed": L("订单未成交（{status}）：{name}", "Order not filled ({status}): {name}"),
    "log.dividend": L("{name} 除权除息：现金分红 {cash} 元（税前）到账", "{name} ex-dividend: cash dividend {cash} (pre-tax) received"),
    "log.no_pe_pct": L("{name} 暂无市盈率百分位（没有估值数据或历史不足一年），本期按 1 倍投入",
                       "{name} has no P/E percentile yet (no valuation data or under a year of history); investing 1x"),
    "log.contribution": L("定投资金 {cash} 元到账", "Contribution of {cash} received"),
    "log.dividend_tax": L("{name} 卖出，持股 {days} 天，补扣红利税 {tax} 元",
                          "{name} sold after {days} days; dividend tax {tax} deducted"),
    "status.Canceled": L("已撤销", "canceled"),
    "status.Margin": L("资金不足", "insufficient cash"),
    "status.Rejected": L("被拒绝", "rejected"),
    "status.Expired": L("已过期", "expired"),

    # ---- 下单理由 ----
    "reason.hold": L("建仓并持有", "open and hold"),
    "reason.dca": L("定投", "regular contribution"),
    "reason.value_dca": L("估值定投：市盈率百分位 {v:.0f}%，投入 {m:g} 倍", "valuation DCA: P/E percentile {v:.0f}%, {m:g}x"),
    "reason.ma_up": L("MA{fast} 上穿 MA{slow}", "MA{fast} crossed above MA{slow}"),
    "reason.ma_down": L("MA{fast} 下穿 MA{slow}", "MA{fast} crossed below MA{slow}"),
    "reason.rsi_low": L("RSI={v:.1f} 超卖", "RSI={v:.1f} oversold"),
    "reason.rsi_high": L("RSI={v:.1f} 超买", "RSI={v:.1f} overbought"),
    "reason.boll_up": L("突破布林上轨", "broke above upper band"),
    "reason.boll_mid": L("跌破布林中轨", "fell below middle band"),
    "reason.rot_buy": L("换仓买入", "rotation buy"),
    "reason.rot_all_neg": L("所有标的动量为负，清仓避险", "all momentum negative, going to cash"),
    "reason.rot_switch": L("轮动切换至 {name}", "rotating into {name}"),
    "reason.rot_best": L("动量最强 {v:.2f}%", "strongest momentum {v:.2f}%"),
    "reason.fw_sell": L("超配，减至目标比例 {w:.1f}%", "overweight, trimming to target {w:.1f}%"),
    "reason.fw_buy": L("低配，增至目标比例 {w:.1f}%", "underweight, adding to target {w:.1f}%"),
    "reason.rule_buy": L("满足买入规则", "buy rule met"),
    "reason.rule_sell": L("满足卖出规则", "sell rule met"),
    "reason.rule_add": L("分批建仓，第 {k} 次加仓", "scale in, add #{k}"),

    # ---- 多因子选股 ----
    "m.turnover_annual": L("年化换手率", "Annual turnover"),
    "sel.kind": L("选股", "Stock selection"),
    "sel.factors": L("因子", "Factors"),
    "sel.top_n": L("持有前 {n} 只", "hold top {n}"),
    "sel.every_n": L("每 {n} 个交易日", "every {n} trading days"),
    "sel.skip_months": L("{months} 月空仓", "in cash in months {months}"),
    "sel.market_out": L("大盘空仓条件", "Market exit"),
    "sel.market_back": L("大盘恢复条件", "Market re-entry"),
    "sel.market_back_auto": L("空仓条件不再满足", "when the exit condition no longer holds"),
    "sel.factor_item": L("{name}（{dir}，权重 {w}）", "{name} ({dir}, weight {w})"),
    "sel.neutral": L("中性化", "Neutralized"),
    "sel.filters": L("条件", "Filters"),
    "sel.neutral_industry": L("行业", "industry"),
    "sel.neutral_size": L("市值", "size"),
    "sel.dir_up": L("越大越好", "higher is better"),
    "sel.dir_down": L("越小越好", "lower is better"),
    "sel.reason_picked": L("新入选", "newly selected"),
    "sel.reason_dropped": L("落选", "dropped from selection"),
    "sel.reason_exit": L("强赎公告或即将退市，必须卖出", "called for redemption or about to delist; must sell"),
    "sel.exit_sell": L("{name} 已公告强赎或即将退市，开盘卖出", "{name} is called for redemption or about to delist; sold at the open"),
    "sel.delist_zero": L("{name} 已退市且此前未能卖出（退市前为 ST 或股价低于 1 元），持仓按 0 计价",
                         "{name} delisted before it could be sold (ST or below 1 yuan); position written off"),
    "sel.delist_cash": L("{name} 已退市且此前未能卖出（吸收合并或主动退市），持仓按最后价格 {price} 折为现金",
                         "{name} delisted before it could be sold (merger or voluntary); settled in cash at the last price {price}"),
    "sel.reason_retry": L("此前因停牌或跌停未能卖出，重新尝试", "retry: could not sell earlier (suspended or limit-down)"),
    "sel.buy_blocked": L("{name} 停牌或开盘涨停，无法买入，本期跳过", "{name} suspended or limit-up at open; skipped this period"),
    "sel.sell_blocked": L("{name} 停牌或开盘跌停，无法卖出，此后每个交易日开盘重试",
                          "{name} suspended or limit-down at open; will retry at each open"),
    "sel.dividend": L("{name} 除权除息：持有 {shares} 股，现金分红 {cash} 元（税前），送转 {added} 股",
                      "{name} ex-dividend: {shares} shares held, cash dividend {cash} (pre-tax), {added} bonus shares"),
    "sel.dividend_tax": L("{name} 卖出，持股 {days} 天，补扣红利税 {tax} 元",
                          "{name} sold after {days} days; dividend tax {tax} deducted"),
    "sel.div_cash": L("现金分红并扣红利税", "cash dividends, taxed"),

    # ---- 规则描述 ----
    "rule.buy": L("买入", "Buy"),
    "rule.sell": L("卖出", "Sell"),
    "rule.position": L("仓位", "Position"),
    "rule.none": L("无", "none"),
    "rule.none_sell": L("无（一直持有）", "none (hold to the end)"),
    "rule.and": L(" 并且 ", " AND "),
    "rule.or": L(" 或者 ", " OR "),
    "rule.template": L("模板", "Template"),
    "rule.days": L("（连续 {n} 根 K 线）", " (for {n} bars in a row)"),
    "rule.scale_title": L("分批建仓", "Scale in"),
    "rule.scale": L("仓位分 {parts} 份，首次买入 1 份；{when}时加仓 1 份，最多加 {k} 次",
                    "position split into {parts} parts; buy 1 part first, add 1 part when {when}, up to {k} times"),
    "rule.cooldown_title": L("冷却", "Cooldown"),
    "rule.cooldown": L("卖出后 {n} 根 K 线内不再买入", "no new buy within {n} bars after a sell"),

    # ---- 规则校验 ----
    "err.logic": L("{side}：逻辑必须是 all 或 any", "{side}: logic must be 'all' or 'any'"),
    "err.no_buy": L("买入条件至少需要一条", "At least one buy condition is required"),
    "err.cond": L("{side}条件{i}：", "{side} condition {i}: "),
    "err.op": L("不支持的比较方式 {op}", "unsupported comparison {op}"),
    "err.value_num": L("比较值必须是数字", "value must be a number"),
    "err.unknown_ind": L("未知指标 {ind}", "unknown indicator {ind}"),
    "err.no_line": L("{ind} 没有输出线 {line}", "{ind} has no output line {line}"),
    "err.param_pos": L("{ind} 的参数 {name} 必须为正数", "parameter {name} of {ind} must be positive"),
    "err.left_ind": L("左边必须是指标", "left side must be an indicator"),
    "err.pnl_cross": L("持仓收益率不能用于上穿/下穿，请用大于/小于",
                       "position P&L cannot be used with cross above/below; use > or <"),
    "err.days": L("连续 K 线数必须是 1~{max} 的整数", "bars in a row must be an integer from 1 to {max}"),
    "err.days_cross": L("上穿 / 下穿只在穿越当天成立，不能设置连续 K 线数", "crosses happen on a single bar; bars in a row cannot be set"),
    "err.days_pnl": L("持仓收益率不能设置连续 K 线数", "position P&L cannot be combined with bars in a row"),
    "err.cooldown": L("卖出后不再买入的 K 线数必须是 0~{max} 的整数", "cooldown after a sell must be an integer from 0 to {max} bars"),
    "err.adds": L("加仓次数必须是 1~{max} 的整数", "number of adds must be an integer from 1 to {max}"),
    "err.add_when": L("加仓时机必须是上涨、下跌或买入条件再次满足", "add trigger must be rise, fall or buy conditions met again"),
    "err.add_pct": L("加仓的涨跌幅必须在 0~100% 之间", "add trigger % must be between 0 and 100"),
    "err.sel_no_factor": L("至少需要一个因子", "At least one factor is required"),
    "err.sel_factor_kind": L("因子「{name}」不适用于所选股票池（股票与可转债的因子不同）",
                             "Factor \"{name}\" does not apply to this universe (stocks and convertibles use different factors)"),
    "err.sel_top_n": L("持有股票数必须在 1~100 之间", "Number of stocks must be between 1 and 100"),
    "err.sel_rebalance": L("调仓间隔必须在 1~250 个交易日之间", "Rebalance interval must be 1-250 trading days"),
    "err.pct": L("仓位比例必须在 0~100 之间", "position % must be between 0 and 100"),
}
