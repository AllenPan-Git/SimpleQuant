"""
视觉主题：研究报告风格（米白纸张、墨色文字、朱红点缀、细线分栏；2026-09-30 用户选定样稿 D）
- 字体：标题用思源宋体（Noto Serif SC，GB2312 常用字子集，随程序打包）；正文用系统无衬线（Segoe UI / 微软雅黑）；
  大号数字用 Georgia（Windows 自带）；代码、表格数字用 JetBrains Mono
- 卡片是直角细边框、无阴影；强调卡片（.sq-sheet）右下角有一层「纸张叠放」的错位阴影
- 深色模式是暖墨色底 + 米色字，不是冷色调
- 图表：style_fig() 把 ui/charts.py 生成的 plotly 图改成透明底、跟随主题的文字和网格颜色
"""

from pathlib import Path

from nicegui import app, ui

STATIC = Path(__file__).with_name("static")

INK, INK_DARK = "#1D1B18", "#EDE6DA"          # 主色 = 墨色（深色模式下换成米色）
RED, RED_DARK = "#B42318", "#E0584A"          # 点缀色（朱红）
GREEN, GREEN_DARK = "#1E7A4C", "#4DB380"
GOLD = "#9A6B1F"
PAPER, DARK_PAGE = "#F5F1EA", "#191714"
DARK_CARD = "#211E1A"

CSS = f"""
@font-face {{ font-family: 'Inter'; font-weight: 100 900; font-display: swap;
  src: url('/static/fonts/inter-latin-wght-normal.woff2') format('woff2'); }}
@font-face {{ font-family: 'JetBrains Mono'; font-weight: 100 800; font-display: swap;
  src: url('/static/fonts/jetbrains-mono-latin-wght-normal.woff2') format('woff2'); }}
@font-face {{ font-family: 'SQ Serif'; font-weight: 500 700; font-display: swap;
  src: url('/static/fonts/noto-serif-sc-600-gb2312.woff') format('woff'); }}

:root {{
  --sq-page: {PAPER}; --sq-card: #FBF9F5; --sq-sunk: #F0EADF; --sq-text: {INK}; --sq-text2: #3C3833;
  --sq-muted: #77716A; --sq-faint: #A39B8F; --sq-border: #D9D2C6; --sq-border2: #E8E2D8; --sq-shadow: #ECE6DC;
  --sq-red: {RED}; --sq-red-soft: rgba(180, 35, 24, .07); --sq-up: {RED}; --sq-down: {GREEN}; --sq-gold: {GOLD};
  --sq-serif: 'SQ Serif', 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'STSong', 'SimSun', Georgia, serif;
  --sq-sans: 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', system-ui, sans-serif;
  --sq-fig: Georgia, 'Times New Roman', serif;
  --sq-mono: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace;
}}
.body--dark {{
  --sq-page: {DARK_PAGE}; --sq-card: {DARK_CARD}; --sq-sunk: #2A2621; --sq-text: {INK_DARK}; --sq-text2: #CFC7BA;
  --sq-muted: #9B9387; --sq-faint: #6F685E; --sq-border: #3A352E; --sq-border2: #2E2A25; --sq-shadow: #141210;
  --sq-red: {RED_DARK}; --sq-red-soft: rgba(224, 88, 74, .10); --sq-up: {RED_DARK}; --sq-down: {GREEN_DARK}; --sq-gold: #C9A05A;
  --q-primary: {INK_DARK} !important; --q-positive: {GREEN_DARK} !important; --q-negative: {RED_DARK} !important;
}}

body, .q-field, .q-btn, .q-item, .q-tab, .q-menu {{ font-family: var(--sq-sans); }}
body {{ background: var(--sq-page); color: var(--sq-text); font-size: 14.5px; -webkit-font-smoothing: antialiased; }}
.q-page-container {{ background: var(--sq-page); }}

/* 字体角色 */
.sq-serif {{ font-family: var(--sq-serif); font-weight: 600; }}
.sq-fig {{ font-family: var(--sq-fig); font-variant-numeric: lining-nums tabular-nums; font-weight: 400; }}
.sq-num, code, pre, .q-table td {{ font-family: var(--sq-mono); font-variant-numeric: tabular-nums; }}
.q-table td {{ font-size: 12.5px; }}
.sq-up {{ color: var(--sq-up); }} .sq-down {{ color: var(--sq-down); }}
.sq-muted {{ color: var(--sq-muted); }} .sq-faint {{ color: var(--sq-faint); }} .sq-red {{ color: var(--sq-red); }}

/* 标题 */
.sq-eyebrow {{ font-size: 12.5px; letter-spacing: .12em; color: var(--sq-red); }}
.sq-title {{ font-family: var(--sq-serif); font-weight: 600; font-size: 2.1rem; line-height: 1.3; }}
.sq-subtitle {{ color: var(--sq-text2); max-width: 46em; }}
.sq-h2 {{ font-family: var(--sq-serif); font-weight: 600; font-size: 1.45rem; }}
.sq-sec {{ width: 100%; display: flex; align-items: baseline; justify-content: space-between;
  padding-bottom: 8px; border-bottom: 1px solid var(--sq-border); margin-top: 28px; }}
.sq-rule-top {{ border-top: 1px solid var(--sq-text); }}
.sq-rule-bottom {{ border-bottom: 1px solid var(--sq-border); }}

/* 顶栏：报头 + 步骤条 */
.sq-header {{ background: var(--sq-page) !important; color: var(--sq-text) !important;
  border-bottom: 1px solid var(--sq-text); }}
.sq-logo {{ font-family: var(--sq-serif); font-weight: 600; font-size: 21px; letter-spacing: .01em; cursor: pointer; white-space: nowrap; }}
.sq-logo span {{ color: var(--sq-red); }}
.sq-mode {{ font-size: 13.5px; color: var(--sq-muted); cursor: pointer; white-space: nowrap; }}
.sq-mode:hover {{ color: var(--sq-text); }}
.sq-mode-on {{ color: var(--sq-text); text-decoration: underline; text-underline-offset: 6px;
  text-decoration-color: var(--sq-red); }}
.sq-step {{ display: flex; align-items: baseline; gap: 8px; padding: 2px 6px; cursor: pointer; color: var(--sq-muted); }}
.sq-step .n {{ font: 400 20px/1 var(--sq-fig); color: var(--sq-faint); }}
.sq-step .t {{ font-size: 14px; white-space: nowrap; line-height: 1.35; }}
.sq-step small {{ display: block; font-size: 11.5px; color: var(--sq-faint); line-height: 1.3; max-width: 9em;
  overflow: hidden; text-overflow: ellipsis; }}
.sq-step:hover .t {{ color: var(--sq-text); }}
.sq-step-done .n {{ color: var(--sq-text); }}
.sq-step-done .t {{ color: var(--sq-text2); }}
.sq-step-cur .n {{ color: var(--sq-red); }}
.sq-step-cur .t > span {{ color: var(--sq-text); font-weight: 600; text-decoration: underline; text-underline-offset: 3px;
  text-decoration-color: var(--sq-red); text-decoration-thickness: 2px; }}
.sq-step .t small {{ margin-top: 3px; }}
.sq-link {{ width: 26px; height: 1px; background: var(--sq-border); margin: 0 4px; }}
.sq-link-done {{ background: var(--sq-text); }}
.sq-top-link {{ font-size: 13px; color: var(--sq-muted); cursor: pointer; white-space: nowrap; }}
.sq-top-link:hover, .sq-top-link.on {{ color: var(--sq-text); }}
.sq-top-link.on {{ text-decoration: underline; text-underline-offset: 6px; text-decoration-color: var(--sq-red); }}

/* 卡片：直角细线；.sq-sheet 是带叠纸阴影的强调卡片 */
.q-card {{ background: var(--sq-card); border: 1px solid var(--sq-border); border-radius: 2px; box-shadow: none; }}
.sq-sheet {{ background: var(--sq-card); border: 1px solid var(--sq-border);
  box-shadow: 0 1px 0 var(--sq-border), 12px 14px 0 -4px var(--sq-shadow), 12px 14px 0 -3px var(--sq-border); }}
.sq-plain {{ background: transparent !important; border: 0 !important; }}
.sq-hover-row {{ cursor: pointer; }}
.sq-hover-row:hover .sq-hover-title {{ color: var(--sq-red); }}
.sq-icon-chip {{ width: 30px; height: 30px; display: grid; place-items: center; color: var(--sq-red);
  border: 1px solid var(--sq-border); background: var(--sq-card); }}

/* 首页：继续上次的工作（流程格）、开始新的工作 */
.sq-pipe {{ display: grid; width: 100%; border-top: 1px solid var(--sq-text); border-bottom: 1px solid var(--sq-border); }}
.sq-pipe > div {{ padding: 10px 12px 12px 0; min-width: 0; }}
.sq-pipe > div + div {{ padding-left: 14px; border-left: 1px solid var(--sq-border); }}
.sq-pipe .k {{ font-size: 12px; color: var(--sq-muted); }}
.sq-pipe .v {{ font-family: var(--sq-serif); font-weight: 600; font-size: 15px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
.sq-pipe .v.sq-fig {{ font-family: var(--sq-fig); font-weight: 400; font-size: 17px; }}
.sq-pipe .ok .k::before {{ content: "✓ "; color: var(--sq-down); }}
.sq-pipe .next {{ background: var(--sq-red-soft); box-shadow: inset 0 3px 0 var(--sq-red); }}
.sq-pipe .next .k {{ color: var(--sq-red); letter-spacing: .04em; }}
.sq-start {{ padding: 22px 24px 20px 0; min-width: 0; }}
.sq-start + .sq-start {{ padding-left: 24px; border-left: 1px solid var(--sq-border); }}
.sq-more {{ font-size: 13px; color: var(--sq-muted); border-bottom: 1px solid var(--sq-border); align-self: flex-start; }}
.sq-start:hover .sq-more {{ color: var(--sq-red); border-color: var(--sq-red); }}
.sq-start:hover .sq-hover-title {{ text-decoration: underline; text-decoration-color: var(--sq-red); text-underline-offset: 5px; }}

/* 大号数字（「图表上方的指标行」） */
.sq-figs {{ display: grid; width: 100%; border-top: 1px solid var(--sq-text); border-bottom: 1px solid var(--sq-border); }}
.sq-figs > div {{ padding: 12px 18px 10px 0; min-width: 0; }}
.sq-figs > div + div {{ padding-left: 18px; border-left: 1px solid var(--sq-border); }}
.sq-figs-sub {{ border-top: 0; }}
.sq-tile {{ border-top: 1px solid var(--sq-text); padding-top: 8px; }}
.sq-figs .k {{ font-size: 12.5px; color: var(--sq-muted); display: block; }}
.sq-figs .v {{ font: 400 30px/1.2 var(--sq-fig); font-variant-numeric: lining-nums tabular-nums; }}
.sq-figs-sub .v {{ font-size: 20px; }}
.sq-figs .d {{ font-size: 12.5px; color: var(--sq-muted); }}

/* 结论段落、解读提示、图注 */
.sq-lede {{ font-family: var(--sq-serif); font-weight: 500; font-size: 18px; line-height: 1.85; color: var(--sq-text2); max-width: 44em; }}
.sq-lede em {{ font-style: normal; font-family: var(--sq-fig); color: var(--sq-text);
  border-bottom: 2px solid rgba(180, 35, 24, .35); }}
.sq-caption {{ font-size: 13px; color: var(--sq-muted); }}
.sq-caption b {{ color: var(--sq-text); font-weight: 600; margin-right: 8px; }}

/* 右侧「下一步」 */
.sq-next {{ display: grid; grid-template-columns: 30px 1fr; width: 100%; padding: 12px 0; border-bottom: 1px solid var(--sq-border);
  cursor: pointer; }}
.sq-next i {{ font: normal 400 20px/1.2 var(--sq-fig); color: var(--sq-faint); }}
.sq-next-title {{ font-family: var(--sq-serif); font-weight: 600; font-size: 15.5px; }}
.sq-next-body {{ font-size: 13px; color: var(--sq-muted); line-height: 1.55; }}
.sq-next:hover .sq-next-title {{ text-decoration: underline; text-underline-offset: 4px; }}
.sq-next-rec {{ background: var(--sq-red-soft); box-shadow: inset 3px 0 0 var(--sq-red); padding-left: 12px; margin-left: -12px;
  width: calc(100% + 12px); }}
.sq-next-rec i, .sq-next-rec .sq-next-title {{ color: var(--sq-red); }}

/* 回测设置折叠区：标题行就是摘要条 */
.sq-setup > .q-expansion-item__container > .q-item {{ padding: 0; min-height: 0; font-family: var(--sq-sans); font-weight: 400;
  border-top: 1px solid var(--sq-text); border-bottom: 1px solid var(--sq-border); }}
.sq-setup > .q-expansion-item__container > .q-item:hover {{ background: transparent; }}
.sq-setup .q-focus-helper {{ display: none; }}

/* 设置摘要条（回测页顶部：数据 · 策略 · 区间 · 资金） */
.sq-chips {{ display: flex; flex-wrap: wrap; width: 100%; border-top: 1px solid var(--sq-text); border-bottom: 1px solid var(--sq-border); }}
.sq-chip {{ padding: 8px 18px 9px 0; cursor: pointer; min-width: 0; }}
.sq-chip + .sq-chip {{ padding-left: 18px; border-left: 1px solid var(--sq-border); }}
.sq-chip .k {{ display: block; font-size: 12px; color: var(--sq-muted); }}
.sq-chip .v {{ font-size: 14px; border-bottom: 1px dotted var(--sq-faint); }}
.sq-chip:hover .v {{ border-bottom: 1px solid var(--sq-red); color: var(--sq-red); }}

/* 按钮：直角；主按钮墨色实底 */
.q-btn {{ border-radius: 2px; letter-spacing: 0; }}
.q-btn.bg-primary, .q-btn.bg-primary .q-btn__content {{ color: {PAPER} !important; }}
body.body--dark .q-btn.bg-primary, body.body--dark .q-btn.bg-primary .q-btn__content, body.body--dark .q-btn.bg-primary .q-icon {{
  color: {DARK_PAGE} !important; }}
.q-btn--outline:before {{ border-color: var(--sq-text); }}
.q-btn--outline.text-primary {{ color: var(--sq-text) !important; }}
.sq-btn-red.q-btn {{ background: var(--sq-red) !important; color: #fff !important; }}
.q-btn-group, .q-btn-toggle {{ border-radius: 2px; }}
.q-btn-toggle {{ border: 1px solid var(--sq-border); }}
.q-toggle__inner--truthy, .q-checkbox__inner--truthy, .q-radio__inner--truthy {{ color: var(--sq-red) !important; }}

/* 表格：学术报告式三线表 */
.q-table__container {{ border-radius: 0; background: transparent; box-shadow: none; border: 0 !important;
  border-top: 2px solid var(--sq-text) !important; border-bottom: 2px solid var(--sq-text) !important; }}
.q-table thead tr {{ border-bottom: 1px solid var(--sq-text); }}
.q-table th {{ font-weight: 500; color: var(--sq-text); text-transform: none; border-bottom-color: var(--sq-text) !important; }}
.q-table td, .q-table tbody tr {{ border-color: var(--sq-border2) !important; }}
.q-table__bottom {{ border-top-color: var(--sq-border) !important; }}

/* 标签页：墨色文字 + 朱红下划线 */
.q-tab {{ text-transform: none; color: var(--sq-muted); }}
.q-tab--active {{ color: var(--sq-text); }}
.q-tab__indicator {{ background: var(--sq-red) !important; height: 2px; }}
.q-tabs {{ border-bottom: 1px solid var(--sq-border); }}

/* 输入框 */
.q-field--outlined .q-field__control {{ border-radius: 2px; }}
.q-field--outlined .q-field__control:before {{ border-color: var(--sq-border); }}
.q-field--outlined.q-field--highlighted .q-field__control:after {{ border-color: var(--sq-text); border-width: 1px; }}
.q-field--dark .q-field__control:before {{ border-color: var(--sq-border); }}
.q-menu, .q-dialog .q-card {{ background: var(--sq-card); border: 1px solid var(--sq-border); border-radius: 2px; }}

/* 折叠面板、进度条、提示条 */
.q-expansion-item {{ border-radius: 2px; overflow: hidden; }}
.q-expansion-item__container > .q-item {{ font-family: var(--sq-serif); font-weight: 600; font-size: 15.5px; }}
.q-linear-progress {{ color: var(--sq-red) !important; border-radius: 0; }}
.sq-code {{ font-family: var(--sq-mono); font-size: 13px; line-height: 1.7; white-space: pre-wrap; padding: 12px 14px;
  border: 1px solid var(--sq-border); background: var(--sq-sunk); }}
.sq-md-tight p, .sq-md-tight ul {{ margin: 0; }}
.sq-md-tight ul {{ padding-left: 1.2em; }}
.sq-note {{ padding: 0 14px; border-left: 3px solid var(--sq-text); background: var(--sq-sunk); }}
.sq-note .q-icon {{ color: var(--sq-text) !important; }}
.sq-note-warning {{ border-left-color: var(--sq-gold); }}
.sq-note-warning .q-icon {{ color: var(--sq-gold) !important; }}
.sq-note-error {{ border-left-color: var(--sq-red); }}
.sq-note-error .q-icon {{ color: var(--sq-red) !important; }}
.q-notification {{ border-radius: 2px; }}

/* plotly：深色模式下把墨色 / 朱红 / 绿色线条和点换成深色版（Python 端在「跟随系统」时不知道明暗，统一靠这几条规则） */
.body--dark .js-plotly-plot [style*="stroke: rgb(29, 27, 24)"] {{ stroke: {INK_DARK} !important; }}
.body--dark .js-plotly-plot [style*="fill: rgb(29, 27, 24)"] {{ fill: {INK_DARK} !important; }}
.body--dark .js-plotly-plot [style*="stroke: rgb(180, 35, 24)"] {{ stroke: {RED_DARK} !important; }}
.body--dark .js-plotly-plot [style*="fill: rgb(180, 35, 24)"] {{ fill: {RED_DARK} !important; }}
.body--dark .js-plotly-plot [style*="stroke: rgb(30, 122, 76)"] {{ stroke: {GREEN_DARK} !important; }}
.body--dark .js-plotly-plot [style*="fill: rgb(30, 122, 76)"] {{ fill: {GREEN_DARK} !important; }}
.js-plotly-plot .modebar-btn path {{ fill: var(--sq-faint) !important; }}

/* ---------------- 窗口变窄 ---------------- */
@media (max-width: 1320px) {{
  .sq-step small {{ display: none; }}
  .sq-link {{ width: 16px; }}
}}
@media (max-width: 1150px) {{
  .sq-header {{ flex-wrap: wrap; row-gap: 4px; padding-left: 20px !important; padding-right: 20px !important; }}
  .sq-top-right {{ margin-left: auto; }}
  .sq-steps {{ order: 3; width: 100%; justify-content: center; padding-top: 6px; border-top: 1px solid var(--sq-border2); }}
  .sq-step small {{ display: block; }}
  .sq-main {{ padding-left: 20px !important; padding-right: 20px !important; }}
  .sq-report {{ grid-template-columns: minmax(0, 1fr) !important; }}
  .sq-home-top {{ grid-template-columns: minmax(0, 1fr) !important; }}
  .sq-starts {{ grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }}
  .sq-starts .sq-start:nth-child(3) {{ padding-left: 0; border-left: 0; }}
  .sq-starts .sq-start:nth-child(n+3) {{ border-top: 1px solid var(--sq-border); }}
  .sq-chip-row {{ flex-wrap: wrap; }}
}}
@media (max-width: 860px) {{
  .sq-step small {{ display: none; }}
  .sq-mode {{ font-size: 12.5px; }}
  .sq-title {{ font-size: 1.7rem; }}
  .sq-setup-row {{ flex-wrap: wrap; }}
  .sq-recent-row {{ grid-template-columns: minmax(0, 1fr) 90px !important; }}
  .sq-recent-row > :nth-child(2), .sq-recent-row > :nth-child(3) {{ display: none; }}
  .sq-figs .v {{ font-size: 22px; }}
  .sq-figs > div {{ padding-right: 10px; }} .sq-figs > div + div {{ padding-left: 10px; }}
  .sq-pipe .v {{ font-size: 13.5px; }}
}}
@media (max-width: 640px) {{
  .sq-starts {{ grid-template-columns: minmax(0, 1fr) !important; }}
  .sq-starts .sq-start {{ padding-left: 0 !important; border-left: 0 !important; border-top: 1px solid var(--sq-border); }}
  .sq-figs {{ grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }}
  .sq-pipe {{ grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }}
}}
"""


def install():
    """程序启动时调用一次：字体等静态文件 + 全局样式"""
    app.add_static_files("/static", STATIC)
    ui.add_css(CSS, shared=True)


def apply(dark: bool | None):
    """每页开头调用：Quasar 配色 + 明暗模式（深色下的主色等在 CSS 里用 --q-* 变量覆盖）"""
    ui.colors(primary=INK, accent=RED, secondary="#77716A", dark=DARK_CARD, dark_page=DARK_PAGE,
              positive=GREEN, negative=RED, info="#77716A", warning=GOLD)
    ui.dark_mode(dark)


def style_fig(fig, dark: bool | None):
    """plotly 图：透明底、主题字体、跟随明暗的文字和网格（跟随系统时用深浅两种背景上都清楚的暖灰）"""
    text, grid = {True: ("#CFC7BA", "rgba(237, 230, 218, .10)"), False: ("#3C3833", "rgba(29, 27, 24, .08)"),
                  None: ("#8A8276", "rgba(138, 130, 118, .20)")}[dark]
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Segoe UI, Microsoft YaHei UI, sans-serif", color=text, size=12),
                      hoverlabel=dict(font_family="JetBrains Mono, Consolas, monospace"))
    fig.update_xaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
    fig.update_yaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
    for ax in fig.select_xaxes():
        if ax.rangeselector and ax.rangeselector.buttons:
            ax.rangeselector.update(bgcolor="rgba(138, 130, 118, .12)", activecolor="rgba(180, 35, 24, .30)",
                                    bordercolor=grid, borderwidth=1, font=dict(color=text, size=11))
    return fig

