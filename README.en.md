<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/banner-en-dark.png">
    <img src="docs/images/banner-en-light.png" alt="SimpleQuant" width="100%">
  </picture>
</p>

<p align="center"><a href="README.md">简体中文</a> · <b>English</b></p>

<p align="center">Windows desktop app · Backtrader engine · Open source under GPLv3</p>

SimpleQuant is a low-code quant backtesting tool for China A-shares. You can go from data to strategy, backtest, parameter optimization and paper trading without writing code. If you prefer, you can also write Python strategies, run multi-factor stock selection, or export strategies to JoinQuant, MyQuant (掘金) and QMT for cross-checking.

> **Disclaimer**: This software is for learning and research only and is not investment advice. Backtest and paper-trading results do not guarantee future returns; any live trading based on them is at your own risk.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/backtest-en-dark.png">
  <img src="docs/images/backtest-en.png" alt="Backtest report">
</picture>

## Two workflows

The step bar at the top of the app follows these two workflows, and each step suggests what to do next.

| Workflow | Steps |
|---|---|
| **Timing** | 01 Data — 02 Strategy — 03 Backtest — 04 Optimize — 05 Paper |
| **Stock selection** | 01 Data — 02 Selection — 03 Paper |

<img src="docs/images/home-en.png" alt="Home">

### 01 · Data
Download into a local library with one click: AKShare daily bars (Eastmoney, with ETFs falling back to Sina when Eastmoney is unreachable), BaoStock 5–60 minute bars and stock daily bars (with PE, PB, PS and turnover), and local TDX (通达信) 1/5-minute bars. You can also import CSV files (bars, or snapshot/tick data with a `last` column, resampled to N-second/N-minute bars).

### 02 · Strategy
Four ways to build a strategy:
- **Templates**: buy & hold, moving-average cross, RSI, Bollinger bands, momentum rotation and more — just tune the parameters.
- **Rule builder**: combine indicators such as MA, EMA, MACD, RSI, KDJ, Bollinger bands, ATR, volume ratio, valuation and position return with crosses above/below and comparisons; stop-losses supported.
- **Describe it in words (AI)**: e.g. "buy when MACD crosses up and RSI is below 70; sell on a cross down or an 8% loss". A large language model turns this into rules (rules only, never code) and lists anything it cannot express.
- **Write Python**: write a strategy class; board lots, T+1 and trading costs still apply, and its parameters can be optimized.

### 03 · Backtest
Multiple assets (capital split equally), custom date range and costs, and a choice of dividend handling (reinvest, or cash dividends with dividend tax). The report opens with a plain-language summary, followed by return, CAGR, Sharpe, drawdown, win rate and profit factor, plus the equity curve, trades on the price chart, P&L per trade and the full fill list; the native Backtrader chart is also available.

A-share rules: 100-share board lots, T+1 (can be turned off for bond/cross-border ETFs), stamp duty on sells, minimum commission and slippage. Signals form at a bar's close and fill at the next bar's open.

### 04 · Optimize
Grid search over any numeric parameter (up to 3 parameters and 500 combinations, run in parallel), with a heatmap of how results vary; the earlier part of the data picks parameters and the later part checks them out of sample, with an overfitting warning. Walk-forward optimization stitches together a fully out-of-sample equity curve.

<img src="docs/images/optimize-en.png" alt="Optimization">

### 05 · Paper trading
Open a simulated account for any saved strategy — no brokerage account needed. After each trading day's close it updates data, advances the account with **the same code as the backtest**, and lists the trades for the next open. A Windows scheduled task can run it automatically.

<img src="docs/images/paper-en.png" alt="Paper trading">

### Multi-factor stock selection
Using free BaoStock data, score the **historical constituents** of the CSI 300 and CSI 500 by value, momentum, low volatility, quality, growth and other factors, and rebalance on a schedule. Includes factor research (Rank IC, quantile backtests, factor correlation), industry/size neutralization, IC/ICIR weighting, custom factors and walk-forward optimization; selection strategies can be paper traded as well.

<img src="docs/images/selection-en.png" alt="Stock selection">

### Export
- **Python script**: runs on its own, with fills identical to the SimpleQuant backtest.
- **JoinQuant / MyQuant / QMT**: strategy files (timing and selection) ready to import into each platform, for cross-checking results elsewhere.

The interface is available in **Chinese and English**, with light and dark themes.

## Installation

### Installer (recommended; no Python needed)
1. Download `SimpleQuant-<version>-Setup.exe` from this repository's **Releases** page.
2. Run it. SimpleQuant installs to `%LOCALAPPDATA%\Programs\SimpleQuant` without administrator rights and adds a Start menu shortcut (desktop shortcut optional).
3. On first launch, Windows SmartScreen may say "Windows protected your PC": click "More info → Run anyway".
4. Download the data you need on the "01 Data" page (AKShare and BaoStock are free).

Notes:
- **Automatic updates** (from 0.1.1): after startup SimpleQuant checks GitHub Releases in the background and usually downloads only the files that changed. When the download is ready it asks you to restart; the files are replaced and the app reopens. If the full installer is needed (over 30 MB) it asks first. Updates are verified by signature and SHA256, and a failed replacement keeps the previous version. Check manually or turn off automatic checks on the Settings page; if GitHub cannot be reached, download the installer from Releases and install over the old version. Version 0.1.0 needs one manual install of a newer version.
- Data is stored in `%LOCALAPPDATA%\SimpleQuant` (market data, stock data, paper accounts, saved strategies, AI settings). Upgrading (automatically or by installing a newer version over the old one) and uninstalling leave it untouched.
- The error log is at `%LOCALAPPDATA%\SimpleQuant\logs\app.log`.
- Command line: `SimpleQuant.exe --paper` advances all paper accounts without opening the window (the scheduled task uses this); `SimpleQuant.exe --run-script picks.py` runs an exported selection script.
- The installed version does not include LiteLLM; for AI features use Claude, OpenAI or an OpenAI-compatible service (DeepSeek, Qwen, Kimi, Zhipu, Gemini, local Ollama, etc.).

### Run from source
Requires Windows and Python 3.10 or later (developed and tested on 3.14).
- Double-click `start.bat`: the first run creates a virtual environment and installs dependencies, then opens the desktop window.
- Or manually: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python main.py` (add `--browser` to open in a browser).
- The source version keeps its data in `data_cache`, `user_strategies` and `user_factors` inside the project folder, separate from the installed version.

### Build
1. Install the build tools: `.venv\Scripts\pip install pyinstaller`; the installer also needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).
2. Set the version `__version__` in `simplequant/__init__.py`.
3. Double-click `build.bat`: it builds `dist\SimpleQuant\` (PyInstaller, see `SimpleQuant.spec`) and, if Inno Setup is installed, `dist\SimpleQuant-<version>-Setup.exe` (see `installer/SimpleQuant.iss`).
   To publish a release, use `tools/release.py`: it builds, writes the update manifest (SHA256 of every file, Ed25519-signed) and patch packages, tags the commit and uploads a draft GitHub release; see the top of the file. The signing key stays on the publisher's machine; the app contains only the public key.
4. The icon `gui/static/icon.ico` is generated by `tools/make_icon.py`; the README banners and screenshots by `tools/readme_assets.py` (both need Microsoft Edge).

## Features in detail

<details>
<summary><b>Multi-factor stock selection</b></summary>

- **Data**: free BaoStock data. Index constituents are queried month by month (avoiding survivorship bias); each stock's unadjusted prices and adjustment factors are downloaded (returns use back-adjusted prices, board lots use actual prices), along with suspensions, ST status, price limits, PE/PB/PS and turnover. Parallel, resumable, incremental; the first CSI 300 download takes about 8–10 minutes and 80 MB. Data packs can be exported and imported (check the data source's terms before sharing).
- **Factors**: EP, BP, SP, 5/20/60-day returns, medium-term momentum (120 days, skipping the latest 20), 60-day volatility, 20-day turnover, Amihud illiquidity, and fundamentals such as ROE, gross margin, net margin, profit/revenue growth and market cap. Daily MAD winsorization and z-scoring; multiple factors are combined by direction and weight.
- **Fundamentals**: BaoStock quarterly data, effective from the day after the first announcement; late filings for older periods never overwrite newer ones; data older than 15 months counts as missing. AKShare's earnings tables are not used because their "latest announcement date" changes with later filings, which would leak future information.
- **Factor research**: Rank IC (mean, annualized ICIR, t-stat, share of IC > 0), quantile backtests (group equity, annual returns, long-short, monotonicity, top-group turnover), an overview of all factors and a correlation matrix.
- **Neutralization**: daily cross-sectional regression removes industry (CSRC classification) and/or size exposure before standardizing.
- **IC/ICIR weighting**: each factor's weight and direction come from its average Rank IC (or IC / std) over the past N trading days, using only ICs already fully realized at the time.
- **Selection backtest**: monthly, weekly or every N days, stocks are scored at the rebalance-day close and the top N are traded at the next open; dropped stocks are sold first, then new picks are bought in equal amounts. Suspended or limit-down stocks cannot be sold (retried daily) and suspended or limit-up stocks cannot be bought; the benchmark is the corresponding index.
- **Dividends**: "reinvest" (back-adjusted) by default. "Cash dividends, taxed" pays cash on the ex-date, adds bonus shares, and deducts dividend tax on sale by holding period (≤1 month 20%, 1 month–1 year 10%, over 1 year exempt), matching JoinQuant and live trading. Special and interim dividends missing from BaoStock's dividend table are filled in from the exchange's ex-dividend reference prices.
- Also: describing a selection in words (AI), industry breakdown of each period's picks, and custom factors (write `def factor(p)` and preview coverage, IC and group returns).
</details>

<details>
<summary><b>Backtest charts</b></summary>

- **Interactive strategy chart**: mirrors the native Backtrader chart but supports zoom, pan and linked hover: P&L per trade, price (with indicators and trades), volume and indicator panels share one time axis; quick ranges 1M/3M/6M/1Y/All; non-trading days removed. Rule strategies plot the indicator lines actually used by the rules, plus threshold reference lines.
- **Native Backtrader chart**: Backtrader's own matplotlib chart, with a selectable range and PNG download; it shares the backtest's setup, and tests ensure its fills match the backtest exactly.
</details>

<details>
<summary><b>Walk-forward optimization</b></summary>

- Splits the data into training/testing windows (rolling: fixed training length; anchored: growing from the earliest data). Each window picks parameters on its training period only and is tested on the period right after it, producing a fully out-of-sample equity curve compared with "original parameters throughout" and buy & hold.
- When parameters change: **carry positions over** (as if the new parameters had always been running) or **restart each segment** (start each segment flat and pass the capital on; more conservative).
- Reports out-of-sample return, CAGR, Sharpe, drawdown, walk-forward efficiency (WFE), a parameter-stability chart and per-window details. Works for timing and selection strategies.
</details>

<details>
<summary><b>Paper trading</b></summary>

- Each run: update data → replay the strategy from the start date with the backtest code → append new fills to the ledger (append-only) → orders placed on the last bar but not yet filled are the "signals" for the next open. Tests ensure day-by-day progress matches a single full backtest exactly.
- Uses only complete daily bars that "should have been published by now", so unfinished intraday bars are never used; missed trading days are caught up on the next run; the trading calendar includes holidays.
- Single-asset accounts can use cash dividends, with signal share counts based on actual prices; selection accounts follow the selection strategy's dividend setting.
- Signals and positions export to CSV for manual orders at a broker; you are warned if revised data makes the replay differ from the ledger.
- Scheduled task: Monday to Friday, 19:00 by default (BaoStock usually publishes the day's data by 18:40); the source version runs `paper_daily.bat`, the installed version `SimpleQuant.exe --paper`.
</details>

<details>
<summary><b>Exporting strategies</b></summary>

- **Timing → Python script**: a standalone `.py` (needs only `backtrader pandas akshare baostock`) with the data setup, strategy and costs; tests ensure every fill matches the SimpleQuant backtest.
- **Timing → JoinQuant / MyQuant / QMT**: embeds a platform-independent signal core, `simplequant/export/signal_core.py` (pure numpy, Python 3.6 compatible, indicators and warm-up checked item by item against Backtrader), plus a thin adapter per platform. Same timing as SimpleQuant: signals at the previous close, fills at the next open. Verified on JoinQuant: trade dates match SimpleQuant.
- **Selection → export**: a Python script (update data → backtest → save the latest picks to `picks.csv`; can be scheduled); "computed on the platform" versions for JoinQuant / MyQuant / QMT (the platform's own data, same algorithm) and "follow SimpleQuant's list" versions (each period's picks written into the file). Verified on JoinQuant: fills match one for one.
- JoinQuant and MyQuant backtests only need a free account; QMT requires a brokerage account. Not yet supported: valuation/turnover factors in timing rules, and minute bars.
</details>

<details>
<summary><b>Timing indicators</b></summary>

Price: close/open/high/low, N-day high/low; trend: MA, EMA, MACD, MA slope, BIAS, DMI/ADX, N-day return; oscillators: RSI, KDJ, CCI, Williams %R; volume: volume, average volume, volume ratio, OBV; volatility: Bollinger bands, ATR, return volatility; valuation/turnover: turnover, PE (TTM), PB, PS (TTM) (needs BaoStock stock daily bars); position: position return (take-profit/stop-loss). KDJ and OBV follow the formulas used by Chinese trading software and are checked value by value against independent implementations.
</details>

<details>
<summary><b>About the data sources</b></summary>

- AKShare ETF daily bars come from Eastmoney. If Eastmoney is unreachable, ETFs switch to Sina prices plus Sina's cumulative dividends to rebuild adjusted prices (matching Eastmoney's back-adjusted prices day by day); adjusted stock prices still require Eastmoney.
- Eastmoney's back-adjustment is additive (adjusted price = raw price + cumulative dividends), which slightly understates long-run returns compared with proportional adjustment; BaoStock uses proportional adjustment. Keep this in mind when comparing results across sources.
- BaoStock publishes the day's bars after the close (by 18:40 in our tests).
</details>

<details>
<summary><b>Project layout and extending</b></summary>

```
main.py                    entry point (desktop window / --browser / --paper / --run-script)
gui/                       UI (NiceGUI): app (routes), layout (header and step bar), pages/, components, widgets, theme
ui/                        framework-independent parts: texts (UI strings), charts, shared (page logic)
simplequant/data/          data sources (akshare / baostock / tdx_local / csv), local library, cash-dividend prices
simplequant/engine/        backtest runner, A-share cost model, strategy base (lots / T+1 / dividends), metrics, optimization, walk-forward
simplequant/strategies/    templates, rule strategies, code strategies
simplequant/rules/         rule JSON schema, validation and descriptions
simplequant/stocks/        stock selection: downloads, panels, factors, research, selection backtest, dividends
simplequant/paper/         paper trading: accounts, replay and ledger, trading calendar, scheduled task
simplequant/export/        export: Python scripts, JoinQuant / MyQuant / QMT
simplequant/llm/           LLM access: presets and config, Claude / OpenAI / LiteLLM adapters, natural language to rules
simplequant/update/        automatic updates: check and download, signed manifest verification, file replacement script
installer/                 installer script (Inno Setup)
tools/                     release script (release.py), icon, README images, UI preview and other helper scripts
tests/                     pytest
```

- **New data source**: subclass `simplequant.data.base.DataSource`, implement `fetch()` returning a standard OHLCV DataFrame, and add it to `SOURCES` in `simplequant/data/__init__.py`.
- **New template**: subclass `_PerAsset` or `BaseStrategy` in `simplequant/strategies/templates.py` and register it with `register(Template(...))`; the UI builds the parameter form automatically.
- **New indicator**: describe it in `INDICATORS` in `rules/schema.py` and implement it in `_build_line` in `strategies/rule_strategy.py`.
- **UI strings**: UI text lives in `ui/texts.py` and core text (indicator names, logs, errors) in `simplequant/i18n.py`, written as `L("中文", "English")`; tests check that every key in use has both languages.
- **Tests**: `.venv\Scripts\python -m pytest -q tests`
</details>

## License

SimpleQuant is released under the [GNU General Public License v3.0](LICENSE) (GPLv3): you may use, modify and redistribute it freely, but any redistribution (including modified versions or packaged builds) must provide the complete source under the same license. GPLv3 is used because the backtest engine, Backtrader, is licensed under GPLv3.

## Third-party components

- Main dependencies: [Backtrader](https://github.com/mementum/backtrader) (GPLv3+), [NiceGUI](https://nicegui.io) (MIT), [pywebview](https://pywebview.flowrl.com) (BSD), [AKShare](https://github.com/akfamily/akshare) (MIT), [BaoStock](http://baostock.com) (BSD), [mootdx](https://github.com/mootdx/mootdx) (MIT), pandas / NumPy (BSD), Plotly (MIT), PyArrow (Apache-2.0), Anthropic / OpenAI SDKs (MIT / Apache-2.0). Their licenses ship with each package.
- Fonts: Inter, JetBrains Mono and Noto Serif SC (GB2312 subset) are under the SIL Open Font License; license texts are in `gui/static/fonts/`.
- The installer is built with [Inno Setup](https://jrsoftware.org/isinfo.php); the Simplified Chinese messages file `installer/ChineseSimplified.isl` is an unofficial translation from the Inno Setup repository.
- Data: market data from AKShare, BaoStock, TDX and others belongs to the respective providers. This repository contains no market data; check each source's terms before exporting or sharing data packs.
