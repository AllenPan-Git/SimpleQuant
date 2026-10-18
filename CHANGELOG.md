# 更新记录 / Changelog

每个版本的更新内容。`tools/release.py` 发版时从这里取对应版本的两节，填进 GitHub 发布说明；
发版前把「未发布」改成版本号和日期。

Changes in each release. `tools/release.py` copies the matching section into the GitHub release notes.

## 未发布

### 更新内容
- 改进：多因子选股（沪深300、中证500、上证50）的基准改用全收益指数（H00300、H00905、H00016，中证指数官网，分红再投资）。之前用的是价格指数，不含分红，而策略收益含分红再投资，超额收益每年约多算一个股息率（沪深300 约 2%～3%）。**超额收益会变小**：例如沪深300 中按市盈率倒数选 10 只、每月调仓，2022 年起基准由 −11.4% 变为 +0.4%，超额收益由 +69.3% 变为 +57.5%；中证500 同样做法 2020 年起超额收益由 +13.4% 变为 −2.1%。策略本身的收益不变。全收益指数在更新数据时一起下载，已有数据第一次载入时自动补下载；没下载到时退回价格指数并提示。模拟盘资产曲线中已记录的基准一并改为全收益指数。可转债的基准仍为中证转债指数

### Changes
- Improved: the benchmark for multi-factor selection (CSI 300, CSI 500, SSE 50) is now the total return index (H00300, H00905, H00016 from the CSI website, dividends reinvested). It used to be the price index, which excludes dividends while the strategy reinvests them, so excess return was overstated by roughly the dividend yield each year (about 2–3% for CSI 300). **Excess returns get smaller**: for example, the top 10 CSI 300 stocks by earnings yield, rebalanced monthly from 2022, now have a benchmark of +0.4% instead of −11.4% and an excess return of +57.5% instead of +69.3%; the same strategy on CSI 500 from 2020 goes from +13.4% to −2.1% excess. Strategy returns themselves are unchanged. The total return index is downloaded with data updates, and fetched automatically the first time existing data is loaded; if it cannot be downloaded, the price index is used with a note. Benchmarks already recorded in paper-trading equity curves are switched to the total return index as well. Convertible bonds still use the CSI Convertible Bond Index

## 1.4.0（2026-10-17）

### 更新内容
- 改进：择时规则用到「市盈率 / 市净率历史百分位」时，所选回测区间开始前的数据只用于计算指标、不交易，收益与交易仍从区间开始日计算。之前百分位只用区间内的数据计算，第 1 年没有百分位，回看年数也凑不满；例如 510300「百分位低于 20 买入、高于 80 卖出」，2020 年起的收益由 +12.9% 变为 +48.8%。回测、批量回测、参数优化（含样本外检验）与滚动优化都按此计算；区间开始前的估值数据不足回看年数时会提示。没有用到估值百分位的策略，结果不变
- 新增：可信度检查加入「调仓日检验」。定期调仓的策略（动量轮动、定比再平衡、多因子选股）结果可能取决于调仓恰好落在哪一天：规则与区间不变，把调仓日依次错开 1 ~ N−1 个交易日重新回测（N 为调仓间隔，最多 20 种），列出总收益与最大回撤的范围、中位数和当前结果的排名；当前结果明显高于中位数时警告。例如 510300 + 510500 动量轮动（回看 20、每 5 天调仓），2014 年起当前结果 +267%，错开调仓日后为 +131%～+224%，中位数 +141%。择时回测在报告显示后自动进行；选股回测较慢，在可信度检查中点「调仓日检验」进行，可随时停止

### Changes
- Improved: when a timing rule uses the P/E or P/B percentile, data before the chosen backtest start is used only to compute indicators, with no trading; returns and trades still count from the start date. Previously the percentile used only data inside the range, so there was none in the first year and the lookback was never full; for example, 510300 "buy below the 20th percentile, sell above the 80th" from 2020 goes from +12.9% to +48.8%. Single and batch backtests, optimization (including the out-of-sample check) and walk-forward all work this way, with a note when the valuation data before the start is shorter than the lookback. Results of strategies that do not use valuation percentiles are unchanged
- New: a "rebalance-day test" in the credibility assessment. Strategies that rebalance on a schedule (momentum rotation, fixed-weight rebalancing, multi-factor selection) can depend on which day the rebalance happens to fall: with the same rules and period, the rebalance days are shifted by 1 to N−1 trading days (N = the rebalance interval, at most 20 variants) and re-run, showing the range and median of total return and maximum drawdown and where the current result ranks, with a warning when it is well above the median. For example, 510300 + 510500 momentum rotation (20-bar lookback, every 5 bars) from 2014 returns +267%, versus +131% to +224% (median +141%) with shifted rebalance days. Timing backtests run the test automatically after the report appears; selection backtests are slower, so click "Rebalance-day test" in the credibility assessment (it can be stopped at any time)

## 1.3.0（2026-10-15）

### 更新内容
- 新增：可转债「强赎触发天数」——近 30 个交易日中转股价值不低于 130（正股收盘价不低于转股价的 130%）的天数。强赎提示性公告没有历史数据，以此近似：多数转债满 15 天即可强赎，2018–2026 年的强赎公告中约 97% 在公告日已达到 15 天。可作为因子（「强赎触发天数（近 30 日）」），也可作为选股条件「强赎触发天数上限」（默认不限，原有结果不变）；自定义因子可用字段 `call_days`
- 用到强赎触发天数的选股方案文件，1.2.1 及以前的版本导入时会提示先更新软件

### Changes
- New: "call-trigger days" for convertible bonds — the number of the last 30 trading days on which the conversion value was 130 or more (stock close at least 130% of the conversion price). Call warnings have no history, so this approximates them: most bonds can be called after 15 such days, and about 97% of 2018–2026 call notices had reached 15 on the notice date. Available as a factor ("Call-trigger days (last 30)") and as the filter "Max call-trigger days" (no limit by default, so existing results are unchanged); custom factors can use the field `call_days`
- Selection plan files that use call-trigger days ask version 1.2.1 and earlier to update first

## 1.2.1（2026-10-13）

### 更新内容
- 改进：多因子选股页载入更快。建立选股数据（股票池全部成分股的价格、财务与分红）的计算加快一倍以上（沪深300 2022 年起：约 27 秒 → 约 11 秒）；建好的数据保存在数据目录的 `data_cache/panel_cache` 中，之后重新打开软件、进入选股页约 1 秒即可载入。数据更新或软件升级后自动重建；最多保留最近使用的 3 份（每份约 40～80 MB）
- 改进：择时规则用到「市盈率 / 市净率历史百分位」时，如果所选回测区间开始前还有估值数据，回测页与参数优化页会提示：百分位仅使用区间内的数据计算（第 1 年没有百分位），并列出区间内的年数、回看年数与未参与计算的年数；将开始日期提前即可按完整的回看年数计算

### Changes
- Improved: the stock selection page loads faster. Building the selection data (prices, financials and dividends of every constituent) is more than twice as fast (CSI 300 from 2022: about 27 s → about 11 s), and the result is saved in `data_cache/panel_cache` in the data folder, so after restarting the app the selection page loads in about a second. It is rebuilt automatically after a data update or an upgrade; the 3 most recently used copies are kept (about 40–80 MB each)
- Improved: when a timing rule uses the P/E or P/B percentile and valuation data exists before the chosen backtest start, the backtest and optimization pages note that the percentile is computed only from data inside the range (none in the first year), listing the years inside the range, the lookback and the years left unused; move the start date earlier to use the full lookback

## 1.2.0（2026-10-13）

### 更新内容
- 新增：多因子选股的「AI 因子挖掘」标签页：AI 每轮提出一批因子，软件只用区间前段的「挖掘期」评估（IC、t 值、与已有因子的相关性），把结果反馈给 AI 改进或换方向；全部轮次结束后，才对入选因子计算一次区间末段「检验期」的表现，并与现有最好的几个因子、两者的等权合成并排比较。可设轮数、每轮因子数、检验期比例、入选门槛和挖掘方向；入选因子可一键保存为自定义因子
- 新增：AI 因子挖掘的可信度检查：列出一共试了多少个因子及相应的多重检验门槛，入选因子是否通过检验期（IC 同号且不低于挖掘期的一半），以及是否胜过现有因子
- AI 因子挖掘中 AI 写的代码会在本机自动运行：运行前做严格检查（不允许 import、读写文件等），并在截到较早日期的数据上重算一遍，用到未来数据的因子不会入选
- 新增：AI 因子挖掘的「导出挖掘记录」（Markdown 文件：设置、两段区间、可信度检查、全部候选的结果与代码、模型每轮的说明）；入选因子表增加「达到门槛」（挖掘期 |t| 达到多重检验门槛）和「通过检验期」两列；判为「覆盖不足」的因子会写明实际覆盖率，下一轮 AI 据此修改代码
- 改进：桌面窗口里的文字（说明、表格、代码等）可以选中复制
- 改进：界面字体统一：标题仍为宋体，正文、表格与普通数字统一为无衬线字体，等宽字体仅用于代码；部分说明与提示改为更正式的表述
- 修复：DeepSeek 等服务繁忙时会一直保持连接而不回复，AI 功能可能卡住很久。现在每次调用 AI 最多等 5 分钟（设置里的超时更长时按设置），超时提示稍后再试；AI 因子挖掘在等待时显示已等秒数，点「停止」立即生效

### Changes
- New: an "AI factor mining" tab in multi-factor selection: each round the AI proposes a batch of factors, the software evaluates them on the early "mining period" only (IC, t-statistic, correlation with existing factors) and feeds the results back so the AI can refine them or change direction. Only after the last round are the accepted factors measured once on the held-out "test period" at the end of the range, side by side with the best existing factors and equal-weight composites of both. Rounds, factors per round, test share, acceptance thresholds and a search direction can be set; accepted factors can be saved as custom factors with one click
- New: a credibility assessment for factor mining: how many factors were tried and the matching multiple-testing threshold, whether the accepted factors held up in the test period (same sign and at least half of the mining IC), and whether they beat the existing factors
- Factor code written by the AI during mining runs automatically on this computer: it is strictly checked first (no imports, no file access, etc.) and recomputed on data cut off at earlier dates; factors that use future data are rejected
- New: "Export mining record" for factor mining (a Markdown file with the settings, both periods, the credibility assessment, every candidate's results and code, and the model's notes); the accepted-factor table gains "Passes threshold" (mining |t| reaches the multiple-testing threshold) and "Held up in test" columns; factors rejected for low coverage now state their actual coverage, so the AI can fix the code in the next round
- Improved: unified typography: headings stay serif; body text, tables and ordinary numbers use one sans-serif font, and monospace is used only for code. Some descriptions and hints were reworded more formally
- Improved: text in the desktop window (notes, tables, code) can now be selected and copied
- Fixed: services such as DeepSeek keep the connection open without replying when busy, so AI features could hang for a long time. Each AI call now waits at most 5 minutes (or the timeout in Settings if longer) and then asks you to try again later; factor mining shows how long it has been waiting, and Stop takes effect immediately

## 1.1.0（2026-10-11）

### 更新内容
- 新增：条件组件的「交易节奏」：每个大于 / 小于条件可以设「连续 K 线数」（连续 N 根 K 线都满足才成立，可过滤一两天的假突破）；可设「卖出后多少根 K 线内不再买入」；可开启「分批建仓」：仓位分成几份，满足买入条件时先买一份，之后在比上次买入时上涨或下跌一定幅度、或买入条件再次满足时再加一份。这些设置可用于参数优化，回测、模拟盘、方案文件、Python 脚本以及导出到聚宽 / 掘金 / QMT 的结果逐笔一致；用一句话生成策略（AI）时也能识别「连续 3 天」「卖出后 10 天内不买」「越跌越买」等说法。用到这些设置的方案文件需要本版本或更新的版本才能导入
- 新增：估值百分位条件「市盈率历史百分位」「市净率历史百分位」：当前估值在过去 N 年里的位置（0 = 最低，100 = 最高）。AKShare 下载沪深300、中证500、上证50 等常见宽基 ETF / 指数时，附带所跟踪指数的市盈率（TTM）和市净率（乐咕乐股，每月更新），数据库中标为「含指数估值」；BaoStock 股票日线逐日提供。已下载的 ETF / 指数需重新下载才会带上估值
- 新增：回测「可信度检查」的随机买卖对照：把已平仓交易的买卖时机随机打乱 1000 次（笔数、每笔持有时间不变），看策略好于多少比例的随机结果，判断收益来自买卖时机还是持有期间的整体涨跌
- 新增：参数优化结果的「最优参数逐年排名」：最优参数在每个自然年的收益排在所有组合的第几名、当年中位数和最好的组合；可信度检查在最优参数多数年份排不进前一半时警告
- 新增：用一句话生成策略（AI）的结果逐条标出「状态」条件（大于 / 小于，每根 K 线都检查）和「事件」条件（上穿 / 下穿，只在穿越当天成立）并说明区别；描述里说「上穿 / 金叉」但生成的是大于 / 小于（或反过来）时提醒；同一段描述再次生成的规则与上一次不同时，提示描述可能有歧义并列出上一次的规则
- 改进：回测报告注明数据来源（如「东方财富 · 后复权」）；下载时东方财富无法连接、改用新浪财经的，数据页会说明；股票数据改用新浪（等比复权，与东方财富的加法复权口径不同）时，回测与参数优化页会提示
- 改进：设置页不再直接显示本机路径（AI 设置文件、数据目录），改为「打开所在文件夹」按钮，鼠标悬停时显示路径，录屏和截图时不会露出
- 修复：点击「运行回测」后，新结果出来前会闪现上一份报告
- 修复：参数优化图的悬停提示把参数值显示成「-8.00000」这样的 6 位有效数字

### Changes
- New: "Trade pacing" in condition blocks: each > / < condition can require several bars in a row (filters out one- or two-day false breakouts); a cooldown blocks new buys for N bars after a sell; and "Scale in" splits the position into parts, buying one part when the buy conditions are met and adding one part after the price rises or falls by a set % from the last buy, or when the buy conditions are met again. These settings can be optimized, and backtests, paper trading, plan files, Python scripts and JoinQuant / MyQuant / QMT exports match trade for trade; the AI rule generator understands phrases such as "3 days in a row", "no buying for 10 days after a sell" and "buy more as it falls". Plan files using these settings need this version or later
- New: valuation percentile conditions "P/E percentile" and "P/B percentile": where the current valuation sits within the past N years (0 = lowest, 100 = highest). AKShare downloads of common broad-index ETFs / indices such as CSI 300, CSI 500 and SSE 50 now include the tracked index's P/E (TTM) and P/B (Legulegu, updated monthly), marked "with index valuation" in the library; BaoStock stock data has them daily. Re-download existing ETF / index data to get the valuation
- New: random-timing comparison in the backtest credibility assessment: the timing of the closed trades is shuffled 1,000 times (same number of trades and holding periods) to show what share of random results the strategy beats, i.e. whether returns come from timing or simply from the market move while holding
- New: "Best parameters, year by year" in optimization results: the rank of the best parameters among all combinations in each calendar year, with that year's median and best; the credibility assessment warns when they miss the top half in most years
- New: the AI rule generator now marks each condition as a "state" (> / <, checked on every bar) or an "event" (crosses above / below, true only on the day of the cross) and explains the difference; it warns when the description says "crosses" but the rule uses > / < (or the reverse), and when the same description produces a different rule than last time, showing the previous rule
- Improved: backtest reports state the data source (e.g. "Eastmoney · back-adjusted"); the Data page notes when Eastmoney was unreachable and Sina Finance was used; backtest and optimization pages warn when stock data came from Sina (proportional adjustment, unlike Eastmoney's additive adjustment)
- Improved: Settings no longer shows local paths (AI settings file, data folder) directly; an "Open folder" button shows the path on hover, so it stays out of screen recordings and screenshots
- Fixed: the previous report flashed briefly after clicking Run backtest, before the new results appeared
- Fixed: optimization chart tooltips showed parameter values padded to six significant digits, such as "-8.00000"

## 1.0.0（2026-10-11）

### 更新内容
- 新增：多因子选股的「因子加权方式」增加「机器学习」：以所选因子为特征、未来持有期收益的排名为目标，在每个调仓日只用当时已知的数据训练模型（默认 3 年滚动窗口，最多每 20 个交易日重新训练），以模型的预测值选股。可选岭回归、K 近邻、随机森林、梯度提升树四种模型，均在本机计算，无需额外安装，结果可复现。结果页新增样本外 IC 对比（模型、ICIR 加权与各单因子）和因子重要性；可信度检查提示样本外 IC 不为正、训练集 IC 远高于样本外 IC（过拟合）、模型不如 ICIR 加权等情况。机器学习的参数可用于滚动优化，策略可开设模拟账户、导出方案文件与 Python 脚本（导出到聚宽 / 掘金 / QMT 时请用「按名单调仓」）。含机器学习设置的方案文件需要本版本或更新的版本才能导入
- 修复：东方财富无法连接、ETF 改用新浪财经下载时，复权价没有处理份额拆分与折算（如 510500 在 2015-04-15 单日 +249%、2022-08-29 单日 -13%）。现在按新浪复权表标出的日期和当天价格跳空推算比例；仍有无法解释的跳空时不使用新浪数据并提示稍后重试。此前用新浪下载过拆分过的 ETF 复权数据的，请在数据页重新下载
- 修复：自定义因子或代码策略运行出错时，错误信息里附带的那一行代码有时是另一段代码的（先后试算过几段代码后，较早那段出错时显示了后来那段的同一行号）；行号本身是对的

### Changes
- New: "Machine learning" as a factor weighting method in stock selection. The chosen factors are features and the rank of the next holding-period return is the target; on every rebalance date a model is trained only on data known at the time (3-year rolling window by default, retrained at most every 20 trading days) and its predictions pick the stocks. Choose ridge regression, k-nearest neighbors, random forest or gradient boosting; everything runs locally with no extra install and is reproducible. Results add an out-of-sample IC comparison (model, ICIR weighting, single factors) and factor importance; the credibility check flags non-positive out-of-sample IC, overfitting (training IC far above out-of-sample IC) and models that do worse than ICIR weighting. Model parameters can be walk-forward optimized, and such strategies can be paper traded and exported as plan files or Python scripts (use "follow picks" for JoinQuant / MyQuant / QMT). Plan files with machine-learning settings need this version or later
- Fixed: when Eastmoney is unreachable and ETF data comes from Sina Finance, adjusted prices ignored share splits (e.g. 510500 showed +249% on 2015-04-15 and -13% on 2022-08-29). Split ratios are now inferred from the price gap on the dates in Sina's adjustment table; if an unexplained gap remains, the Sina data is rejected with a message to retry later. Re-download adjusted ETF data previously fetched from Sina for funds that had splits
- Fixed: when a custom factor or code strategy failed, the source line quoted in the error could come from different code (after several snippets had been run, an error in an earlier one showed the same line number of a later one); the line number itself was right

## 0.3.0（2026-10-10）

### 更新内容
- 不再提供 Intel 芯片 Mac 的安装包：0.2.5 是最后一个支持 Intel Mac 的版本，已安装的可以继续使用。以后的版本在新版本没有本系统安装包时，设置页的「检查更新」会说明情况，不再显示为出错
- 改进：多因子选股回测的「运行日志」显示股票或转债名称（如「搜特转债(128100) 停牌或开盘跌停，无法卖出」），与成交记录一致；此前只显示代码
- 新增：用一句话描述自定义因子（AI）：在选股页「自定义因子」中描述计算思路（如「近 20 天上涨日成交额占总成交额的比例」），由大模型写出 factor(p) 代码，并列出无法用现有数据计算的部分；可勾选「在编辑器中的代码基础上修改」。生成的代码不会自动运行，填入编辑器、阅读后点击「试算」才在本机运行；生成时会检查偷看未来（shift(-n)、bfill 等）、不存在的字段、import 与读写文件，发现问题先让模型修正
- 改进：自定义因子编辑器在代码用到未来数据（shift(-n)、bfill、rolling(center=True)）或不存在的字段时，在编辑器下方提示
- 改进：AI 解读回测结果（择时回测与多因子选股回测）时，除汇总指标外还参考交易明细：最好与最差的几笔交易、按标的的盈亏、去掉最好几笔后的收益与重抽样区间，以及期末未平仓持仓的浮动盈亏（胜率、盈亏比只统计已平仓交易，期末浮亏较大时会指出）；并说明回测已扣除所设的手续费、印花税和滑点
- 改进：回测「可信度检查」新增期末未平仓提示（择时回测、多因子选股回测、参数优化结果）：期末仍持有的仓位按最后收盘价计浮亏达到初始资金的 3% 时警告，并列出已平仓交易的合计收益（胜率与盈亏比只统计已平仓交易）；收益一半以上是尚未兑现的浮盈时也会说明

### Changes
- Intel Macs are no longer supported: 0.2.5 is the last version built for them, and installed copies keep working. From now on, when a newer release has no package for your system, "Check for updates" in Settings explains this instead of reporting an error
- Improved: the Log tab of the selection backtest shows stock and bond names (as in Fills) instead of codes only
- New: describe a custom factor in plain language (AI): on the Custom factors tab of the Selection page, describe how it is computed (e.g. "the share of the last 20 days' trading value that came on up days") and the model writes the factor(p) code and lists anything the available data cannot compute; tick "Modify the code in the editor" to change existing code. Generated code never runs by itself: it is placed in the editor and runs on this computer only when you click Trial run. Generation checks for look-ahead (shift(-n), bfill and similar), unknown fields, imports and file access, and asks the model to fix them first
- Improved: the custom factor editor warns below the code when it uses future data (shift(-n), bfill, rolling(center=True)) or a field that does not exist
- Improved: the AI explanation of backtest results (timing and multi-factor selection) now looks beyond the summary metrics at the trades: the best and worst trades, P&L by security, the return without the top trades and the bootstrap range, and the unrealized P&L of positions still open at the end (win rate and profit factor cover closed trades only, so a large open loss is pointed out); it also knows the results are already net of the commission, stamp duty and slippage you set
- Improved: the credibility assessment (timing and selection backtests, optimization results) now covers positions still open at the end: it warns when their unrealized loss at the last close reaches 3% of starting capital and shows what the closed trades made (win rate and profit factor cover closed trades only); it also points out when more than half of the return is an unrealized gain

## 0.2.5（2026-10-09）

### 更新内容
- 新增：多因子选股方案文件：在选股回测的「导出」菜单保存股票池、因子与权重、选股规则、区间与费率（可附带回测结果），在另一台电脑上「导入方案」即可还原设置、下载缺少的选股数据（含所需的财务、分红数据）并重新回测，回测后与方案记录的结果核对。策略用到的自定义因子连同代码一起保存；导入时显示代码，确认后才会保存和运行。回测页与选股页均可导入两种方案
- 新增：内置「股息率」因子（价值类）：最近一年的每股现金分红（年报与中期分红合计，送股、转增已折算）除以不复权收盘价，需先下载分红数据
- 改进：自定义因子编辑器与速查说明列出分红字段（div_cash、div_bonus、div_reserve）和股票的不复权收盘价 raw_close；用到分红字段的因子只适用于股票，选股、模拟盘与导出脚本会自动下载所需的分红数据
- 改进：选择标的与数据库预览的下拉框中，不复权、前复权的在线数据标出复权方式，同一代码的多份数据可以区分
- 修复：组合回测或分别回测同时选择同一代码的两份数据时，只回测了其中一份；现在两份分别计算，名称后标出复权方式
- 修复：选股数据「下载 / 更新」时指数行情按本次区间整体覆盖，下载较短区间后更早的交易日被删除；现在与已有数据合并

### Changes
- New: multi-factor selection plan files: save the universe, factors and weights, selection rules, period and costs (optionally with the backtest result) from the Export menu of the selection backtest, then use Import plan on another computer to restore the settings, download any missing selection data (including the financial or dividend data needed), rerun the backtest and verify the result against the one recorded in the plan. Custom factors used by the strategy are saved with their code; on import the code is shown and is saved and run only after confirmation. Both kinds of plans can be imported on the Backtest and Selection pages
- New: built-in Dividend yield factor (Value): cash dividends per share over the last year (annual plus interim, adjusted for bonus and capitalization shares) divided by the unadjusted close; requires dividend data
- Improved: the custom factor editor and cheat sheet list the dividend fields (div_cash, div_bonus, div_reserve) and the unadjusted stock close raw_close; factors using dividend fields apply to stocks only, and selection, paper trading and exported scripts download the dividend data they need
- Improved: unadjusted and forward-adjusted online data are labeled with their adjustment in the asset selector and the database preview, so several datasets of the same code can be told apart
- Fixed: portfolio or separate backtests that included two datasets of the same code ran only one of them; both are now computed, with the adjustment shown after the name
- Fixed: updating selection data overwrote the index prices with the requested period, so a shorter download removed earlier trading days; new data is now merged with what is already there

## 0.2.4（2026-10-07）

### 更新内容
- 新增：回测页选择多个标的时，可选择「分别回测」：同一策略在每个标的上单独回测，报告给出跑赢买入持有的比例、收益中位数、最差 10% 标的的收益与回撤对比，附策略收益对买入持有收益的散点图和逐个标的的明细（可下载 CSV、可打开任一标的的完整回测）
- 新增：择时回测模拟涨跌停与停牌（日线，默认开启，可在「资金与交易成本」中关闭）：开盘即跌停或停牌时卖出委托顺延至下一交易日开盘，开盘即涨停或停牌时买入委托作废，规则与多因子选股相同；可信度检查会提示受影响的委托次数。导出的 Python 脚本使用相同规则。此前开设的模拟账户保持原有规则
- 新增：回测方案文件（.sqplan）：在回测页「导出」菜单保存策略、标的、区间与费率（可附带回测结果），在另一台电脑的回测页「导入方案」即可还原设置、下载缺少的数据并重新回测，回测后与方案记录的结果核对。方案文件只含设置，不含行情与代码，代码策略请通过导出 Python 脚本分享
- 改进：程序内的更新说明不再显示发布页开头的「下载」一节

### Changes
- New: when several assets are selected on the Backtest page, they can be backtested separately: the same strategy runs on each asset on its own, and the report shows the share of assets beating buy-and-hold, the median return, the bottom-decile return and a drawdown comparison, with a scatter plot of strategy versus buy-and-hold returns and a per-asset table (downloadable as CSV; any asset can be opened as a full backtest)
- New: timing backtests now simulate price limits and suspensions (daily bars, on by default, can be turned off under Capital & trading costs): a sell order is carried over to the next open when the asset opens limit-down or is suspended, and a buy order is canceled when it opens limit-up or is suspended, following the same rules as factor selection; the credibility assessment reports how many orders were affected. Exported Python scripts use the same rules. Paper accounts opened earlier keep their original rules
- New: backtest plan files (.sqplan): save the strategy, assets, period and costs (optionally with the backtest result) from the Export menu on the Backtest page, then use Import plan on another computer to restore the settings, download any missing data, rerun the backtest and verify the result against the one recorded in the plan. Plan files contain settings only, with no market data or code; share code strategies as exported Python scripts
- Improved: the in-app release notes no longer show the "Download" section at the top of the release page

## 0.2.3（2026-10-06）

### 更新内容
- 修复：参数优化页的滚动优化完成后，结果区域（资产曲线、参数变化图、各窗口表格）整块不显示
- 修复：导出的 Python 脚本中残留一处包内相对导入（`from ..stocks.dividends import tax_rate`），改为在脚本中附带该函数源码，脚本可独立运行

### Changes
- Fixed: after a walk-forward optimization on the Optimize page, the whole results area (equity curve, parameter chart and window table) was not displayed
- Fixed: exported Python scripts contained a leftover package-relative import (`from ..stocks.dividends import tax_rate`); the function's source is now included so the script runs on its own

## 0.2.2（2026-10-04）

### 更新内容
- 组合模拟账户：在「资产配置」页完成组合回测后，可按当前权重与再平衡方式开设模拟账户。账户每个交易日与其他模拟账户一同推进，各成分（现金类、ETF 买入持有、择时策略、选股策略）按与回测相同的方式计算，到再平衡时点给出下一交易日开盘的调整交易；模拟盘页显示各成分的目标与当前权重、对应持仓、资产曲线（与各成分等权配置对比）及再平衡记录
- 资产配置流程的步骤条增加第 3 步「模拟盘」

### Changes
- Portfolio paper accounts: after a portfolio backtest on the Asset allocation page, open a paper account with its current weights and rebalancing rule. The account advances with the other paper accounts every trading day; each component (cash, buy-and-hold ETFs, timing and selection strategies) is computed exactly as in the backtest, and rebalancing trades are listed for the next open when due. The Paper trading page shows target and actual component weights, underlying holdings, the equity curve (against an equal-weight mix of the components) and the rebalancing history
- The Allocation workflow's step bar gains a third step, Paper trading

## 0.2.1（2026-10-04）

### 更新内容
- 从源码运行：依赖按经过测试的版本安装（`constraints.txt`，Python 3.12 及以上适用），依赖文件更新后，启动脚本自动重新安装
- 从源码运行：LiteLLM 改为可选依赖（`pip install litellm -c constraints.txt`）。此前它与 mootdx 的依赖版本冲突，可能导致首次安装失败
- AKShare 数据源：东方财富无法连接时，股票日线也自动改用新浪财经（等比复权，与 BaoStock 一致；此前只有 ETF 有备用接口）；东方财富失败后 15 分钟内直接使用新浪，无需每次等待重试
- AKShare 数据源：指数日线改为新浪财经优先、中证指数官网备用（东方财富指数接口经常无法获取数据）
- 新浪财经访问统一限速，被限制访问（HTTP 456）时立即暂停访问，不再反复重试；各接口均失败时分别列出原因
- 顶部「AI 模型」入口更名为「设置」，页面分为「AI 模型」与「关于 SimpleQuant」两部分；「关于」中新增项目主页、问题反馈、更新记录链接

### Changes
- Running from source: dependencies are installed at tested versions (`constraints.txt`, Python 3.12 and later); the start scripts reinstall them when the dependency files change
- Running from source: LiteLLM is now optional (`pip install litellm -c constraints.txt`); its dependency conflict with mootdx could make the first install fail
- AKShare source: stock daily bars now also fall back to Sina Finance when Eastmoney is unreachable (proportional adjustment, matching BaoStock; previously only ETFs had a fallback); after an Eastmoney failure, Sina is used directly for 15 minutes instead of retrying each time
- AKShare source: index daily bars now come from Sina Finance first, with the CSI website as a fallback (Eastmoney's index interface often failed)
- Requests to Sina Finance share one rate limit; when Sina restricts access (HTTP 456), requests pause immediately instead of retrying, and when every interface fails each reason is listed
- The "AI model" link at the top is renamed "Settings"; the page has an AI model section and an About section, which now links to the project page, issue tracker and changelog

## 0.2.0（2026-10-03）

### 更新内容
- 自然语言选股新增「在当前方案基础上修改」：例如「加入低波动因子，持股改为 30 只」，只调整提到的部分，其余设置保持不变
- 选股回测结果新增「AI 解读结果」
- AI 解读更客观：先与无风险收益比较绝对收益，参照夏普、卡玛比率的常用区间评价，基准下跌时说明超额收益的来源

### Changes
- Plain-language stock selection can now modify the current settings (e.g. "add a low-volatility factor, hold 30 stocks"); only the parts you mention change
- "Explain with AI" for stock-selection backtest results
- More candid AI explanations: absolute return vs. the risk-free rate, standard Sharpe / Calmar yardsticks, and how much excess return comes from a falling benchmark

## 0.1.4（2026-10-03）

### 更新内容
- **新增「资产配置」**（报头第三条流程）：
  - 参照证券公司适当性管理问卷进行风险测评，评定 C1 保守型 ~ C5 进取型及可承受的最大回撤；
  - 在现金、国债 ETF、宽基 ETF、黄金 ETF 及自定义的择时 / 选股策略之间给出参考配置（大类比例 + 风险平价，并按可承受回撤自动调整）；
  - 组合回测支持按月、季、年或偏离阈值再平衡，展示收益贡献、风险贡献与相关系数，权重可手动调整后重新回测
- **回测可信度检查**：回测、选股回测与参数优化的结果页自动提示交易次数不足、收益集中于少数交易、重抽样收益区间、参数优化组数过多及样本外表现明显下降等问题
- **模拟盘异常提示**：首页提示尚未设置每日自动运行、上次运行失败或数据多日未更新，模拟盘页显示失败原因

### Changes
- **New Asset allocation workflow**: a risk assessment modelled on broker suitability questionnaires (C1 conservative to C5 aggressive, with a maximum tolerable drawdown); a reference allocation across cash, Treasury ETFs, broad-market ETFs, a gold ETF and your own timing or selection strategies (class mix plus risk parity, adjusted to the drawdown limit); portfolio backtests with monthly, quarterly, yearly or threshold rebalancing, return and risk contributions, correlations and editable weights
- **Backtest credibility assessment**: backtest, stock-selection and optimization results now flag too few trades, returns concentrated in a few trades, the bootstrap range of returns, large numbers of parameter combinations tested and markedly weaker out-of-sample performance
- **Paper-trading alerts**: the home page reports a missing daily schedule, a failed last run or data that has not been updated for several days; the paper-trading page shows the cause of a failure

## 0.1.3（2026-10-03）

### 更新内容
- **新增 macOS 版与 Linux 版**（Apple 芯片 / Intel 芯片的 dmg，Linux x86_64 的 tar.gz），同样支持自动更新
- 模拟盘「每日自动运行」在 macOS / Linux 上分别使用 launchd、systemd 用户定时器（或 crontab）
- 修复：安装版进入模拟盘页、设置定时任务时会闪一下命令行窗口

### Changes
- **New macOS and Linux builds** (dmg for Apple silicon / Intel, tar.gz for Linux x86_64), with automatic updates
- The paper-trading daily schedule uses launchd on macOS and a systemd user timer (or crontab) on Linux
- Fix: the installed app briefly flashed a console window on the paper-trading page and when scheduling the daily run

## 0.1.2（2026-10-02）

### 更新内容
- **可转债多因子选股**：选股页股票池新增「可转债（全市场）」，含已退市转债（2017 年起），避免幸存者偏差
  - 因子：双低、转债价格、转股溢价率、纯债溢价率、转股价值、发行规模、剩余期限、正股涨幅与波动率等
  - 选股条件：价格上限、日均成交额、剩余期限、转股溢价率上限
  - 回测规则：每手 10 张、T+0；2022-08-01 起涨跌幅 ±20%（上市首日 +57.3% / −43.3%）；付息按税后利息计入；强赎或到期赎回公告后的下一个交易日卖出
  - 支持因子研究、选股回测、滚动优化、模拟盘、导出独立 Python 脚本和自然语言选股
- **利率与信用利差**：数据页可下载中债收益率曲线；规则条件新增 10 年期国债收益率、期限利差、信用利差，可用于择时
- **债券 ETF**：T+1 改为按品种执行，债券 / 货币 / 黄金 / 跨境 ETF 及可转债按 T+0 处理；新增「定比再平衡」模板（如股债 60/40）；常用标的增加国债、十年国债、短融、可转债 ETF
- 修正：沪市 111 开头的可转债被识别为深市

### Changes
- **Convertible-bond multi-factor selection**: new "Convertible bonds (all)" universe including delisted bonds since 2017 (no survivorship bias)
  - Factors: double-low, price, conversion premium, premium over bond floor, conversion value, issue size, years to maturity, underlying-stock return and volatility
  - Filters: max price, average turnover, years to maturity, max conversion premium
  - Rules: lots of 10, T+0; ±20% price limits from 2022-08-01 (+57.3% / −43.3% on the first day); after-tax coupons; holdings sold the day after a call or maturity notice
  - Works with factor research, backtests, walk-forward, paper trading, standalone Python export and natural-language selection
- **Rates and credit spreads**: download ChinaBond yield curves on the Data page; rule conditions now include the 10-year yield, term spread and credit spread for market timing
- **Bond ETFs**: T+1 now applies by product type (bond, money-market, gold and cross-border ETFs and convertibles are T+0); new "Fixed-weight rebalancing" template (e.g. 60/40 stocks/bonds)
- Fix: Shanghai convertibles with codes starting 111 were treated as Shenzhen

## 0.1.1（2026-10-01）

### 更新内容
- 新增自动更新：启动时检查新版本，仅下载有变化的文件；更新文件经数字签名校验
- 设置页新增"关于"区域，可查看当前版本、手动检查更新，并可关闭启动时自动检查
- 更新失败时保留原版本，并提示原因
- 0.1.0 用户需手动下载本版安装包覆盖安装一次，此后即可自动更新

### Changes
- Automatic updates: checks for new versions at startup and downloads only the changed files; update files are verified by a digital signature
- New "About" section in Settings: view the current version, check for updates manually, or turn off the startup check
- If an update fails, the previous version is kept and the reason is shown
- 0.1.0 users need to install this version manually once; later versions update automatically

## 0.1.0（2026-10-01）

### 更新内容
- 首个公开版本（Windows）
- 数据：AKShare、BaoStock、通达信本地数据，CSV 导入
- 策略：模板、条件组件、自然语言描述（AI）、Python 代码
- 回测：A 股交易规则（整手、T+1、印花税、最低佣金），分红再投资或现金分红并扣红利税
- 参数优化：网格搜索热力图、样本外检验、滚动优化（Walk-forward）
- 多因子选股：沪深300 / 中证500 历史成分股，因子研究、中性化、IC 加权
- 本地模拟盘：与回测相同的代码逐日推进，可定时自动运行
- 导出：独立 Python 脚本，聚宽 / 掘金 / QMT 策略文件

### Changes
- First public release (Windows): data import, strategies, A-share backtesting, parameter optimization, multi-factor stock selection, local paper trading and export
