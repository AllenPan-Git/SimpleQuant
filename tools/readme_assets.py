"""
生成 README 用的图片：顶部横幅（中英文 × 浅色 / 深色）和软件截图（中英文界面）
    .venv\\Scripts\\python tools\\readme_assets.py [banner|shots [截图名称…]]   （不给参数则全部生成）
    例：readme_assets.py shots selection-cb   只重拍可转债选股的中英文截图
输出到 docs/images/。截图通过 tools/readme_server.py 启动界面（不动你的偏好和模拟账户），需要 Microsoft Edge。
"""

import asyncio
import base64
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_preview import FONT, ROOT, body_at, svg  # noqa: E402
from screenshot import shoot  # noqa: E402

OUT = ROOT / "docs" / "images"
PY = sys.executable
PORT = 8780

# 与界面主题一致（gui/theme.py）
THEMES = {
    "light": {"bg": "#F5F1EA", "text": "#1D1B18", "muted": "#77716A", "faint": "#A39B8F", "red": "#B42318",
              "rule": "#1D1B18"},
    "dark": {"bg": "#191714", "text": "#EDE6DA", "muted": "#9B9387", "faint": "#6F685E", "red": "#E0584A",
             "rule": "#3A352E"},
}
TAGLINE = {"zh": "低代码的 A 股量化回测工具", "en": "Low-code quant backtesting for China A-shares"}
STEPS = {"zh": ["数据", "策略", "回测", "优化", "模拟盘"], "en": ["Data", "Strategy", "Backtest", "Optimize", "Paper"]}


# ---------------- 横幅 ----------------
def banner_html(lang: str, theme: str) -> str:
    c = THEMES[theme]
    font = base64.b64encode(FONT.read_bytes()).decode()
    steps = "".join(
        (f'<span class="dash"></span>' if i else "") + f'<span class="step"><b>0{i + 1}</b>{name}</span>'
        for i, name in enumerate(STEPS[lang]))
    return f"""<!doctype html><meta charset="utf-8"><style>
@font-face {{ font-family: SQSerif; src: url(data:font/woff;base64,{font}) format("woff"); }}
html, body {{ margin: 0; background: {c['bg']}; }}
.wrap {{ width: 1280px; height: 300px; box-sizing: border-box; padding: 0 72px; display: flex; align-items: center;
  gap: 44px; border-bottom: 2px solid {c['rule']}; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }}
.logo {{ font: 600 76px/1 SQSerif, Georgia, serif; color: {c['text']}; letter-spacing: .01em; }}
.logo span {{ color: {c['red']}; }}
.tag {{ margin-top: 18px; font-size: 25px; color: {c['muted']}; }}
.steps {{ margin-top: 30px; display: flex; align-items: baseline; gap: 14px; font-size: 17px; color: {c['muted']}; }}
.step b {{ font: 400 24px Georgia, serif; color: {c['text']}; margin-right: 8px; }}
.step:nth-child(5) b {{ color: {c['red']}; }}
.dash {{ width: 30px; height: 1px; background: {c['faint']}; align-self: center; }}
</style><div class="wrap">{svg(body_at("H3D", 256), 168)}
<div><div class="logo">Simple<span>Quant</span></div><div class="tag">{TAGLINE[lang]}</div>
<div class="steps">{steps}</div></div></div>"""


async def banners():
    tmp = OUT / "_tmp.html"
    for lang in ("zh", "en"):
        for theme in THEMES:
            tmp.write_text(banner_html(lang, theme), encoding="utf-8")
            await shoot(tmp.as_uri(), OUT / f"banner-{lang}-{theme}.png", 1280, 1.5, 300, scale=2)
    tmp.unlink()


# ---------------- 截图 ----------------
# 名称: (路径, 点击, 裁剪(起始文字, 结束文字, 上留白, 下留白)) ；文字按语言给出。
# 模拟盘要在选股之前截：打开选股页后报头会切到「多因子选股」流程
SHOTS = {
    "home": ("/", [], {"zh": (None, "免费数据源对比", 0, -24), "en": (None, "Free data sources compared", 0, -24)}),
    "backtest": ("/backtest", [], {"zh": (None, "图 1", 0, 40), "en": (None, "Figure 1", 0, 40)}),
    "optimize": ("/optimize", [{"zh": "开始优化", "en": "Run optimization"}],
                 {"zh": ("优化结果", "前 20 名参数组合", 32, -16), "en": ("Optimization results", "Top 20 combinations", 32, -16)}),
    "paper": ("/paper", [{"zh": "资产曲线", "en": "Equity"}],
              {"zh": (None, "每日自动运行", 0, -24), "en": (None, "Run automatically every day", 0, -24)}),
    "selection": ("/selection", [{"zh": "开始回测", "en": "Run backtest"}],
                  {"zh": ("回测结果", "每期选股", 32, -16), "en": ("Results", "Picks per period", 32, -16)}),
    "allocation": ("/allocation", [],
                   {"zh": ("参考配置", "权重与贡献", 32, -16), "en": ("Reference allocation", "Weights and contributions", 32, -16)}),
    # 组合模拟账户：单独启动一次服务（readme_server.py 加 pf），模拟盘页打开按参考配置开设的组合账户
    "paper-portfolio": ("/paper", [{"zh": "持仓", "en": "Positions"}],
                        {"zh": (None, "每日自动运行", 0, -24), "en": (None, "Run automatically every day", 0, -24)}),
    # 可转债选股：单独启动一次服务（readme_server.py 加 cb），选股页的股票池为「可转债（全市场）」
    "selection-cb": ("/selection", [{"zh": "开始回测", "en": "Run backtest"}],
                     {"zh": ("回测结果", "每期选股", 32, -16), "en": ("Results", "Picks per period", 32, -16)}),
}
# 按截图名称：打开页面后、点击后各等几秒
WAIT = {"selection": 30, "selection-cb": 45, "optimize": 8, "allocation": 8}
CLICK_WAIT = {"optimize": 45, "selection": 40, "selection-cb": 60, "paper": 3, "paper-portfolio": 3}
# 每次启动服务截哪几张：(主题, 服务的附加参数, 截图名称)
# 首页单独启动一次服务（加 up）：模拟账户盈利、已设置每日自动运行；组合模拟账户加 pf
RUNS = [("light", [], [n for n in SHOTS if n not in ("selection-cb", "home", "paper-portfolio")]),
        ("dark", [], ["backtest"]), ("light", ["cb"], ["selection-cb"]), ("light", ["up"], ["home"]),
        ("light", ["pf"], ["paper-portfolio"])]


def start_server(lang: str, theme: str, extra: list | None = None) -> subprocess.Popen:
    try:                                   # 端口上已有别的服务：截到的会是它的页面
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
        raise RuntimeError(f"port {PORT} is already in use; stop the other server first")
    except OSError:
        pass
    proc = subprocess.Popen([PY, "-X", "utf8", str(ROOT / "tools" / "readme_server.py"), lang, theme, str(PORT),
                             *(extra or [])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            return proc
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(1)
    proc.kill()
    raise RuntimeError("readme_server did not start")


async def screenshots(only: set | None = None):
    base = f"http://127.0.0.1:{PORT}"
    for lang in ("zh", "en"):
        for theme, extra, names in RUNS:
            names = [n for n in names if not only or n in only]
            if not names:
                continue
            proc = start_server(lang, theme, extra)
            try:
                for name in names:
                    path, clicks, crop = SHOTS[name]
                    suffix = "" if theme == "light" else "-dark"
                    await shoot(base + path, OUT / f"{name}-{lang}{suffix}.png", 1440, WAIT.get(name, 6),
                                clicks=[(c[lang], CLICK_WAIT[name]) for c in clicks], crop=crop[lang])
                    print(name, lang, theme)
            finally:
                proc.kill()
                time.sleep(2)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "banner"):
        asyncio.run(banners())
    if what in ("all", "shots"):
        asyncio.run(screenshots(set(sys.argv[2:]) or None))
