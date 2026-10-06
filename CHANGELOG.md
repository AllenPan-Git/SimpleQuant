# 更新记录 / Changelog

每个版本的更新内容。`tools/release.py` 发版时从这里取对应版本的两节，填进 GitHub 发布说明；
发版前把「未发布」改成版本号和日期。

Changes in each release. `tools/release.py` copies the matching section into the GitHub release notes.

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
