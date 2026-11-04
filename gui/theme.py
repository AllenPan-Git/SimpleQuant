"""
视觉主题：研究报告风格（米白纸张、墨色文字、朱红点缀、细线分栏；2026-09-30 用户选定样稿 D）
- 字体只分三种角色（10-12 用户说正文字体杂乱后统一）：
  标题用思源宋体（Noto Serif SC，GB2312 常用字子集，随程序打包），大号指标数字和步骤编号用 Georgia（标题级）；
  报告的结论段落（.sq-lede）也属于这一类：中文宋体 + 数字 Georgia（用户 10-12 确认保留）；
  正文、表格、普通数字一律用无衬线：西文与数字 Segoe UI（Windows；Mac / Linux 用随程序打包的 Inter），中文微软雅黑 / 苹方，数字等宽；
  等宽字体 JetBrains Mono 只用于代码
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
SANS_PLOT = "Segoe UI, Inter, Microsoft YaHei UI, PingFang SC, sans-serif"

# 新版界面的侧栏：宽度、图标列宽度；窗口窄于 SIDE_MINI_BELOW 时折成图标列
SIDE_W, SIDE_MINI_W, SIDE_MINI_BELOW = 200, 64, 1100
_MINI = "body.sq-new:not(.sq-side-open)"      # 窄窗口、没有手动展开时 = 图标列

# 内容区变窄时的规则（报头的规则不在这里）：经典界面按窗口宽度（@media），新版界面按内容区宽度（@container），共用这三段
_NARROW_1150 = """\
  .sq-main { padding-left: 20px !important; padding-right: 20px !important; }
  .sq-report { grid-template-columns: minmax(0, 1fr) !important; }
  .sq-home-top { grid-template-columns: minmax(0, 1fr) !important; }
  .sq-starts { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .sq-starts .sq-start:nth-child(3) { padding-left: 0; border-left: 0; }
  .sq-starts .sq-start:nth-child(n+3) { border-top: 1px solid var(--sq-border); }
  .sq-chip-row { flex-wrap: wrap; }
  /* 吸顶按钮换到设置区上方，整行铺底色（吸顶时免得和下面的表单叠在一起），表单不再被按钮列挤窄 */
  .sq-setup-sticky { flex-wrap: wrap; }
  .sq-setup-sticky > .sq-setup-actions { order: -1; width: 100%; top: var(--sq-header-h, 64px); padding-bottom: 8px;
    background: var(--sq-page); }
  .sq-setup-btns { flex-direction: row; flex-wrap: wrap; }
"""
_NARROW_860 = """\
  .sq-title { font-size: 1.7rem; }
  .sq-setup-row { flex-wrap: wrap; }
  .sq-recent-row { grid-template-columns: minmax(0, 1fr) 90px !important; }
  .sq-recent-row > :nth-child(2), .sq-recent-row > :nth-child(3) { display: none; }
  /* 模拟账户一览：只留账户、待执行交易、状态 */
  .sq-acct-row { grid-template-columns: minmax(0, 1fr) 120px 120px !important; }
  .sq-acct-row > :nth-child(2), .sq-acct-row > :nth-child(3) { display: none; }
  .sq-acct-row > :nth-child(4) { padding-left: 0; }
  .sq-figs .v { font-size: 22px; }
  .sq-figs > div { padding-right: 10px; } .sq-figs > div + div { padding-left: 10px; }
  .sq-pipe .v { font-size: 13.5px; }
  .sq-verify { grid-template-columns: minmax(0, 1fr) !important; }
"""
_NARROW_640 = """\
  .sq-starts { grid-template-columns: minmax(0, 1fr) !important; }
  .sq-starts .sq-start { padding-left: 0 !important; border-left: 0 !important; border-top: 1px solid var(--sq-border); }
  .sq-figs { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .sq-pipe { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
"""

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
  --sq-sans: 'Segoe UI Variable Text', 'Segoe UI', 'Inter', 'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Hiragino Sans GB',
    'Noto Sans CJK SC', 'Source Han Sans SC', 'WenQuanYi Micro Hei', system-ui, sans-serif;
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
body {{ background: var(--sq-page); color: var(--sq-text); font-size: 14.5px; -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums; }}
.q-page-container {{ background: var(--sq-page); }}

/* 字体角色 */
.sq-serif {{ font-family: var(--sq-serif); font-weight: 600; }}
.sq-fig {{ font-family: var(--sq-fig); font-variant-numeric: lining-nums tabular-nums; font-weight: 400; }}
.sq-num, .q-table td {{ font-family: var(--sq-sans); font-variant-numeric: tabular-nums; }}
code, pre {{ font-family: var(--sq-mono); }}
.q-table td {{ font-size: 13px; }}
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
/* 模拟盘页：一览表里当前显示详情的账户 */
.sq-acct-sel {{ background: var(--sq-red-soft); box-shadow: inset 3px 0 0 var(--sq-red); padding-left: 12px; }}
.sq-acct-sel .sq-hover-title {{ color: var(--sq-red); }}
.sq-icon-chip {{ width: 30px; height: 30px; display: grid; place-items: center; color: var(--sq-red);
  border: 1px solid var(--sq-border); background: var(--sq-card); }}

/* 首页：继续上次的工作（流程格）、开始新的工作 */
.sq-pipe {{ display: grid; width: 100%; border-top: 1px solid var(--sq-text); border-bottom: 1px solid var(--sq-border); }}
.sq-pipe > div {{ padding: 10px 12px 12px 0; min-width: 0; }}
.sq-pipe > div + div {{ padding-left: 14px; border-left: 1px solid var(--sq-border); }}
.sq-pipe .k {{ font-size: 12px; color: var(--sq-muted); }}
.sq-pipe .v {{ font-weight: 500; font-size: 15px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
.sq-pipe .v.sq-fig {{ font-family: var(--sq-sans); font-weight: 500; font-size: 15px; }}
.sq-pipe .ok .k::before {{ content: "✓ "; color: var(--sq-down); }}
.sq-pipe .next {{ background: var(--sq-red-soft); box-shadow: inset 0 3px 0 var(--sq-red); }}
/* 第一格平时贴齐页面左边；铺了底色时、或每格都能点（工作台的策略进度，看起来是一排按钮）时，第一格与其余各格留同样的左边距 */
.sq-pipe > .next:first-child, .sq-progress > div:first-child {{ padding-left: 14px; }}
.sq-pipe .next .k {{ color: var(--sq-red); letter-spacing: .04em; }}
/* 新版界面的策略进度（components.strategy_progress）：格子可点时悬停给下划线 */
.sq-progress-link {{ cursor: pointer; }}
.sq-progress-link:hover .v {{ text-decoration: underline; text-decoration-color: var(--sq-red); text-underline-offset: 4px; }}
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
.sq-chip-row-wrap {{ flex-wrap: wrap; }}
.sq-setup-row {{ scroll-margin-top: 12px; }}

/* 设置区右侧的操作按钮：设置展开、表单很长时吸在报头下面（报头高度由 HEAD_JS 写进 --sq-header-h，窄窗口会换行变高） */
.sq-setup-actions {{ position: sticky; top: calc(var(--sq-header-h, 64px) + 12px); z-index: 5; flex-shrink: 0; }}
/* 标签页里的吸顶：Quasar 的标签页容器 .q-panel-parent 是 overflow: hidden，每页外面的 .q-panel.scroll 是 overflow: auto，
   都会让吸顶失效。容器改 clip（同样裁掉切换动画，但不算滚动容器）；.q-panel 高度随内容，本来就不出滚动条，改 visible */
.q-panel-parent:has(.sq-setup-actions) {{ overflow: clip; }}
.q-panel-parent:has(.sq-setup-actions) > .q-panel {{ overflow: visible; }}
/* 择时工作台：设置展开时按钮竖排成一列，给表单多留宽度；收起时横排，与摘要条同高（竖排会比摘要条高出一截）；
   内容区变窄、按钮移到上方时也横排（_NARROW_1150） */
.sq-setup-btns {{ display: flex; flex-direction: column; align-items: stretch; gap: 8px; }}
.sq-setup-row:has(> .sq-setup.q-expansion-item--collapsed) .sq-setup-btns {{ flex-direction: row; }}
.sq-setup-btns > .q-btn .q-btn__content {{ justify-content: flex-start; }}
/* 择时工作台的「回测报告 / 验证」标签与验证标签：左列检验方法（选中的浅红底 + 左侧红条，同 .sq-next-rec），右边是选中的一项 */
.sq-bench-tabs {{ border-bottom: 1px solid var(--sq-border); }}
.sq-verify {{ grid-template-columns: 230px minmax(0, 1fr); align-items: start; }}
.sq-vf-head {{ align-self: stretch; padding-bottom: 6px; border-bottom: 1px solid var(--sq-text); }}
.sq-vf-item {{ align-self: stretch; display: flex; flex-direction: column; gap: 1px; padding: 10px 12px; cursor: pointer;
  border-bottom: 1px solid var(--sq-border2); box-shadow: inset 3px 0 0 transparent; }}
.sq-vf-item:hover .sq-vf-name {{ text-decoration: underline; text-underline-offset: 4px; }}
.sq-vf-item.on {{ background: var(--sq-red-soft); box-shadow: inset 3px 0 0 var(--sq-red); }}
.sq-vf-item.on .sq-vf-name {{ color: var(--sq-red); }}
.sq-vf-name {{ font-weight: 600; font-size: 14px; }}
.sq-vf-sub {{ font-size: 12px; color: var(--sq-muted); }}
.sq-vf-sub.ok {{ color: var(--sq-down); }}

/* 新版界面（body.sq-new）：左侧栏。守住研报风格：纸色底、细线分组、宋体标志，当前栏目浅红底 + 左侧红条（同 .sq-next-rec）。
   没有顶栏，吸顶元素的 top 取 0。
   窄窗口折成图标列不用 Quasar 抽屉的 mini 属性：它只能由 Python 设定，Python 不知道窗口多宽，要等浏览器回报，
   每次换页都会先宽后窄闪一下。改用 @media 盖掉抽屉和内容区的内联宽度（!important 优先于内联样式）；
   用户在窄窗口点「展开侧栏」后 body 加 sq-side-open，恢复原宽度，内容区跟着让开 */
body.sq-new {{ --sq-header-h: 0px; }}
/* 像报纸的目录：与正文同一张纸（不用深一档的底色），标志下一条墨线（同经典界面报头）；
   宽窗口不显示图标、栏目名用宋体；当前栏目墨色加粗 + 左侧朱红细条，不填色块；待执行笔数是朱红 Georgia 数字 */
.sq-new .q-drawer {{ background: var(--sq-page); border-color: var(--sq-border) !important; }}
.sq-new .sq-side {{ background: var(--sq-page); padding: 26px 18px 12px; overflow-x: hidden; align-items: stretch; }}
.sq-new .sq-side .sq-logo {{ padding: 0 0 12px; font-size: 22px; border-bottom: 1px solid var(--sq-text); }}
.sq-new .sq-side-date {{ padding: 7px 0 8px; margin-bottom: 10px; border-bottom: 1px solid var(--sq-border);
  font-family: var(--sq-serif); font-size: 12.5px; color: var(--sq-muted); white-space: nowrap; }}
.sq-new .sq-logo-mini {{ display: none; }}
.sq-new .sq-side-nav {{ flex: 1 0 auto; display: flex; flex-direction: column; }}
.sq-new .sq-side-item {{ display: flex; align-items: center; gap: 12px; min-height: 40px; padding: 0 0 0 14px; white-space: nowrap;
  font-family: var(--sq-serif); font-size: 15.5px; font-weight: 500; color: var(--sq-text2); text-decoration: none;
  border-left: 2px solid transparent; }}
.sq-new .sq-side-item > .q-icon {{ display: none; }}
.sq-new .sq-side-item:hover {{ color: var(--sq-text); }}
.sq-new .sq-side-item:hover .sq-side-label {{ text-decoration: underline; text-decoration-color: var(--sq-red);
  text-underline-offset: 5px; }}
.sq-new .sq-side-item:focus-visible {{ outline: 1px solid var(--sq-text); outline-offset: -1px; }}
.sq-new .sq-side-item.on {{ color: var(--sq-text); border-left-color: var(--sq-red); font-weight: 700; }}
.sq-new .sq-side-item.on:hover .sq-side-label {{ text-decoration: none; }}
.sq-new .sq-side-label {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; }}
.sq-new .sq-side-badge {{ margin-left: auto; font: 400 15px/1 var(--sq-fig); font-variant-numeric: lining-nums tabular-nums;
  color: var(--sq-red); }}
/* 分组头像报纸的栏目标题：宋体灰字、字距放宽，细线接在名字后面拉到右边；朱红只留给当前栏目 */
.sq-new .sq-side-sep {{ flex-shrink: 0; display: flex; align-items: center; gap: 10px; margin: 16px 0 4px; padding-left: 16px; }}
.sq-new .sq-side-sep::after {{ content: ""; flex: 1; height: 1px; background: var(--sq-border); }}
.sq-new .sq-side-group {{ font-family: var(--sq-serif); font-size: 13px; font-weight: 600; letter-spacing: .14em;
  color: var(--sq-muted); white-space: nowrap; }}
.sq-new .sq-side-space {{ flex-grow: 1; min-height: 20px; }}
.sq-new .sq-side-foot {{ display: flex; align-items: center; gap: 4px; padding: 10px 0 0; border-top: 1px solid var(--sq-border); }}
.sq-new .sq-side-foot > :first-child {{ margin-right: auto; }}
.sq-new .sq-side-toggle {{ display: none; }}
.q-tooltip.sq-tip {{ background: var(--sq-text); color: var(--sq-page); border-radius: 2px; font-size: 12px; padding: 4px 10px; }}
@media (min-width: {SIDE_MINI_BELOW}px) {{ .sq-side-tip {{ display: none; }} }}
body.sq-side-open .sq-side-tip {{ display: none; }}
@media (max-width: {SIDE_MINI_BELOW - .02}px) {{
  .sq-new .sq-side-toggle {{ display: inline-flex; }}
  {_MINI} .q-drawer {{ width: {SIDE_MINI_W}px !important; }}
  {_MINI} .q-page-container {{ padding-left: {SIDE_MINI_W}px !important; }}
  {_MINI} .sq-side {{ padding: 18px 8px 10px; }}
  {_MINI} .sq-side .sq-logo {{ padding: 0 0 12px; margin-bottom: 10px; text-align: center; }}
  {_MINI} .sq-logo-full {{ display: none; }}
  {_MINI} .sq-logo-mini {{ display: block; }}
  {_MINI} .sq-side-item {{ position: relative; justify-content: center; padding: 0; color: var(--sq-muted); }}
  {_MINI} .sq-side-item > .q-icon {{ display: inline-flex; }}
  {_MINI} .sq-side-item:hover, {_MINI} .sq-side-item.on {{ color: var(--sq-text); }}
  {_MINI} .sq-side-item.on > .q-icon {{ color: var(--sq-red); }}
  {_MINI} .sq-side-label {{ display: none; }}
  {_MINI} .sq-side-badge {{ position: absolute; top: 9px; right: 9px; width: 6px; height: 6px; border-radius: 50%;
    background: var(--sq-red); font-size: 0; }}
  {_MINI} .sq-side-date, {_MINI} .sq-side-group {{ display: none; }}
  {_MINI} .sq-side-sep {{ display: block; height: 1px; margin: 8px 4px; padding: 0; background: var(--sq-border); }}
  {_MINI} .sq-side-sep::after {{ display: none; }}
  {_MINI} .sq-side-foot {{ flex-direction: column; padding: 8px 0 0; }}
  {_MINI} .sq-side-foot > :first-child {{ margin-right: 0; }}
}}

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
/* 表单里的「高级设置」：内层折叠区，标题用正文字体，不加底色 */
.sq-adv > .q-expansion-item__container > .q-item {{ padding: 0; min-height: 0; font-family: var(--sq-sans); font-weight: 500;
  font-size: 14px; }}
.sq-adv > .q-expansion-item__container > .q-item:hover {{ background: transparent; }}
.sq-adv .q-focus-helper {{ display: none; }}
/* 收成一行的说明（widgets.hint）：「详细说明」紧跟在短句后面 */
.sq-hint-toggle {{ margin-left: .5em; color: var(--sq-red); cursor: pointer; white-space: nowrap; }}
.sq-hint-toggle:hover {{ text-decoration: underline; }}
.q-linear-progress {{ color: var(--sq-red) !important; border-radius: 0; }}
.sq-code {{ font-family: var(--sq-mono); font-size: 13px; line-height: 1.7; white-space: pre-wrap; padding: 12px 14px;
  border: 1px solid var(--sq-border); background: var(--sq-sunk); }}
.sq-md-tight p, .sq-md-tight ul {{ margin: 0; }}
.sq-md-tight ul {{ padding-left: 1.2em; }}
.sq-note {{ padding: 10px 14px; line-height: 1.6; border-left: 3px solid var(--sq-text); background: var(--sq-sunk); }}
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
{_NARROW_1150}}}
@media (max-width: 860px) {{
  .sq-step small {{ display: none; }}
  .sq-mode {{ font-size: 12.5px; }}
{_NARROW_860}}}
@media (max-width: 640px) {{
{_NARROW_640}}}

/* 新版界面：内容区 = 窗口减去侧栏，上面按窗口宽度的规则会该生效时没生效（不会误生效：内容区总比窗口窄），
   所以把内容区设成查询容器，同样的规则再按内容区宽度来一遍。不支持容器查询的老内核（Safari 15 及以前）忽略这一段，
   退回按窗口宽度，和加侧栏之前一样 */
.sq-new .q-page-container {{ container: sqpage / inline-size; }}
@container sqpage (max-width: 1150px) {{
{_NARROW_1150}  .sq-setup-sticky > .sq-setup-actions {{ padding-top: 10px; }}     /* 没有报头，吸顶时贴着窗口上沿，留一点白 */
}}
@container sqpage (max-width: 860px) {{
{_NARROW_860}}}
@container sqpage (max-width: 640px) {{
{_NARROW_640}}}
"""

# 报头高度写进 CSS 变量 --sq-header-h（吸顶元素用）。报头在窄窗口会换行变高，所以用 ResizeObserver 跟着改；
# 报头由 Vue 挂载，脚本运行时可能还没有，先每 0.1 秒找一次
HEAD_JS = """<script>
(function () {
  var seen = null;
  function sync() {
    var h = document.querySelector('.sq-header');
    if (!h) return;
    document.documentElement.style.setProperty('--sq-header-h', h.offsetHeight + 'px');
    if (h !== seen && window.ResizeObserver) { seen = h; new ResizeObserver(sync).observe(h); }
  }
  var tries = 0, timer = setInterval(function () { sync(); if (seen || ++tries > 100) clearInterval(timer); }, 100);
  window.addEventListener('resize', sync);
})();
</script>"""


def install():
    """程序启动时调用一次：字体等静态文件 + 全局样式"""
    app.add_static_files("/static", STATIC)
    ui.add_css(CSS, shared=True)
    ui.add_head_html(HEAD_JS, shared=True)


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
                      font=dict(family=SANS_PLOT, color=text, size=12),
                      hoverlabel=dict(font_family=SANS_PLOT))
    fig.update_xaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
    fig.update_yaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
    for ax in fig.select_xaxes():
        if ax.rangeselector and ax.rangeselector.buttons:
            ax.rangeselector.update(bgcolor="rgba(138, 130, 118, .12)", activecolor="rgba(180, 35, 24, .30)",
                                    bordercolor=grid, borderwidth=1, font=dict(color=text, size=11))
    return fig

