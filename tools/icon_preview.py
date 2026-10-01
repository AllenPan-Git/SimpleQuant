"""
图标候选预览：生成 HTML（各候选在 256 ~ 16 像素、浅色背景 / 任务栏 / 桌面上的效果），并用 Edge 无头截图
    .venv\\Scripts\\python tools\\icon_preview.py [输出目录] [只看哪几个，如 C,D,H]
"""

import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INK, RED, PAPER = "#1D1B18", "#B42318", "#F5F1EA"
FONT = ROOT / "gui" / "static" / "fonts" / "noto-serif-sc-600-gb2312.woff"

# 所有候选都画在 256×256 的画布上
CANDIDATES = {
    "A": ("朱红印章「量」", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{RED}"/>
        <rect x="26" y="26" width="204" height="204" rx="26" fill="none" stroke="{PAPER}" stroke-width="7" opacity=".85"/>
        <text x="128" y="133" font-family="SQSerif" font-size="168" fill="{PAPER}" text-anchor="middle"
              dominant-baseline="central">量</text>"""),
    "B": ("墨色底 · 上涨 K 线", f"""
        <rect x="11" y="11" width="234" height="234" rx="38" fill="{INK}" stroke="#5A544B" stroke-width="6"/>
        <g stroke-linecap="round">
          <line x1="72" y1="128" x2="72" y2="214" stroke="{PAPER}" stroke-width="8"/>
          <rect x="52" y="148" width="40" height="52" rx="4" fill="{PAPER}"/>
          <line x1="128" y1="88" x2="128" y2="184" stroke="{PAPER}" stroke-width="8"/>
          <rect x="108" y="104" width="40" height="64" rx="4" fill="{PAPER}"/>
          <line x1="184" y1="40" x2="184" y2="148" stroke="{RED}" stroke-width="8"/>
          <rect x="164" y="56" width="40" height="76" rx="4" fill="{RED}"/>
        </g>"""),
    "C": ("SQ 字母（与界面标志一致）", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="84" y="134" font-family="Georgia, serif" font-weight="700" font-size="132" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <text x="170" y="134" font-family="Georgia, serif" font-weight="700" font-size="132" fill="{RED}"
              text-anchor="middle" dominant-baseline="central">Q</text>"""),
    "D": ("米白底 · 收益曲线", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <polyline points="44,192 92,148 128,168 176,104 206,78" fill="none" stroke="{INK}" stroke-width="20"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="206" cy="78" r="22" fill="{RED}"/>"""),
    # C + D 结合
    "E": ("SQ · Q 的尾巴是收益曲线", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="80" y="128" font-family="Georgia, serif" font-weight="700" font-size="128" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="168" cy="122" r="44" fill="none" stroke="{RED}" stroke-width="20"/>
        <polyline points="162,150 190,180 224,138" fill="none" stroke="{INK}" stroke-width="16"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="224" cy="138" r="13" fill="{RED}"/>"""),
    "F": ("SQ · 浅色收益曲线衬底", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <polyline points="30,206 92,160 128,180 180,112 226,74" fill="none" stroke="#DED4C3" stroke-width="26"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <text x="84" y="134" font-family="Georgia, serif" font-weight="700" font-size="132" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <text x="170" y="134" font-family="Georgia, serif" font-weight="700" font-size="132" fill="{RED}"
              text-anchor="middle" dominant-baseline="central">Q</text>"""),
    "G": ("SQ 在上 · 收益曲线在下", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="94" y="92" font-family="Georgia, serif" font-weight="700" font-size="104" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <text x="162" y="92" font-family="Georgia, serif" font-weight="700" font-size="104" fill="{RED}"
              text-anchor="middle" dominant-baseline="central">Q</text>
        <polyline points="42,214 92,186 128,200 176,172 214,164" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="214" cy="164" r="15" fill="{RED}"/>"""),
    "E2": ("SQ · Q 的尾巴是折线（避免像对勾）", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="78" y="128" font-family="Georgia, serif" font-weight="700" font-size="128" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="160" cy="118" r="44" fill="none" stroke="{RED}" stroke-width="20"/>
        <polyline points="158,150 180,176 200,162 230,122" fill="none" stroke="{INK}" stroke-width="14"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="230" cy="122" r="12" fill="{RED}"/>"""),
    "E3": ("SQ · 收益曲线从 S 下方升起、穿过 Q 当作尾巴", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="80" y="118" font-family="Georgia, serif" font-weight="700" font-size="120" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="166" cy="110" r="42" fill="none" stroke="{RED}" stroke-width="19"/>
        <polyline points="38,212 84,188 114,202 152,172 226,96" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="226" cy="96" r="14" fill="{RED}"/>"""),
    "E4": ("SQ · 短折线穿过 Q 当作尾巴", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="76" y="128" font-family="Georgia, serif" font-weight="700" font-size="128" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="166" cy="112" r="44" fill="none" stroke="{RED}" stroke-width="20"/>
        <polyline points="118,206 144,184 160,196 230,100" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="230" cy="100" r="14" fill="{RED}"/>"""),
    "G2": ("SQ 在上（略小）· 更陡的收益曲线", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="84" y="84" font-family="Georgia, serif" font-weight="700" font-size="96" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <text x="148" y="84" font-family="Georgia, serif" font-weight="700" font-size="96" fill="{RED}"
              text-anchor="middle" dominant-baseline="central">Q</text>
        <polyline points="40,214 88,186 122,200 172,158 214,134" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="214" cy="134" r="15" fill="{RED}"/>"""),
    "H3": ("SQ 居中 · 对称的收益曲线", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="128" y="80" font-family="Georgia, serif" font-weight="700" font-size="96" text-anchor="middle"
              dominant-baseline="central"><tspan fill="{INK}">S</tspan><tspan fill="{RED}">Q</tspan></text>
        <polyline points="46,212 92,186 126,200 172,166 210,152" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="210" cy="152" r="15" fill="{RED}"/>"""),
    "E5": ("SQ · 收益曲线升起、连到 Q", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <polyline points="34,214 72,188 98,202 140,150" fill="none" stroke="{INK}" stroke-width="15"
                  stroke-linecap="round" stroke-linejoin="round"/>
        <text x="128" y="98" font-family="Georgia, serif" font-weight="700" font-size="116" text-anchor="middle"
              dominant-baseline="central"><tspan fill="{INK}">S</tspan><tspan fill="{RED}">Q</tspan></text>"""),
    "E6": ("SQ · Q 的尾巴向上扬起", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="80" y="122" font-family="Georgia, serif" font-weight="700" font-size="128" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="164" cy="114" r="42" fill="none" stroke="{RED}" stroke-width="20"/>
        <path d="M160,152 Q186,200 230,128" fill="none" stroke="{RED}" stroke-width="16" stroke-linecap="round"/>
        <circle cx="230" cy="128" r="12" fill="{INK}"/>"""),
    "E6b": ("SQ · Q 的尾巴向上扬起（无圆点）", f"""
        <rect x="8" y="8" width="240" height="240" rx="40" fill="{PAPER}" stroke="{INK}" stroke-width="10"/>
        <text x="80" y="122" font-family="Georgia, serif" font-weight="700" font-size="128" fill="{INK}"
              text-anchor="middle" dominant-baseline="central">S</text>
        <circle cx="164" cy="114" r="42" fill="none" stroke="{RED}" stroke-width="20"/>
        <path d="M160,152 Q188,204 234,118" fill="none" stroke="{RED}" stroke-width="17" stroke-linecap="round"/>"""),
    # 多尺寸：.ico 里每个尺寸可以放不同的图
    "E6D": ("E6b（48 像素及以上）+ D（32 像素及以下）", "E6b", "D", 48),
    "H3D": ("H3（48 像素及以上）+ D（32 像素及以下）", "H3", "D", 48),
    "H": ("G（48 像素及以上）+ D（32 像素及以下）", "G", "D", 48),
    "H2": ("G2（48 像素及以上）+ D（32 像素及以下）", "G2", "D", 48),
    "E3H": ("E3（48 像素及以上）+ D（32 像素及以下）", "E3", "D", 48),
}
SIZES = [256, 128, 64, 48, 32, 24, 16]


def svg(body: str, size: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="{size}" height="{size}">{body}</svg>'


def body_at(key: str, size: int) -> str:
    """某个候选在某个尺寸下的图（组合候选按尺寸选用不同的图）"""
    _, *rest = CANDIDATES[key]
    if len(rest) == 1:
        return rest[0]
    big, small, threshold = rest
    return body_at(big if size >= threshold else small, size)


def html(keys=None) -> str:
    font = base64.b64encode(FONT.read_bytes()).decode()
    rows = []
    for key, (name, *_) in CANDIDATES.items():
        if keys and key not in keys:
            continue
        sizes = "".join(f'<div class="cell">{svg(body_at(key, s), s)}<small>{s}</small></div>' for s in SIZES)
        bar = "".join(f'<div class="cell">{svg(body_at(key, s), s)}<small>{s}</small></div>' for s in (32, 24, 16))
        desk = f'<div class="desk">{svg(body_at(key, 48), 48)}<span>SimpleQuant</span></div>'
        rows.append(f'<section><h2>{key}　{name}</h2><div class="row"><div class="paper">{sizes}</div>'
                    f'<div class="bar">{bar}</div>{desk}</div></section>')
    return f"""<!doctype html><meta charset="utf-8"><style>
@font-face {{ font-family: SQSerif; src: url(data:font/woff;base64,{font}) format("woff"); font-weight: 600; }}
body {{ margin: 0; padding: 24px 28px; background: #E9E4DA; font: 14px "Microsoft YaHei", sans-serif; color: {INK}; }}
h1 {{ font: 600 22px SQSerif; margin: 0 0 4px; }} p {{ margin: 0 0 18px; color: #77716A; }}
h2 {{ font: 600 17px SQSerif; margin: 0 0 8px; }}
section {{ margin-bottom: 22px; }}
.row {{ display: flex; gap: 18px; align-items: stretch; }}
.paper, .bar {{ display: flex; gap: 18px; align-items: flex-end; padding: 14px 18px; border-radius: 6px; }}
.paper {{ background: #FFFFFF; }}
.bar {{ background: #202020; color: #9A9A9A; }}
.cell {{ display: flex; flex-direction: column; align-items: center; gap: 6px; }}
.cell small {{ font-size: 11px; color: #999; }}
.desk {{ width: 104px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
  border-radius: 6px; background: linear-gradient(135deg, #3B6E9E, #22466B); }}
.desk span {{ color: #fff; font-size: 12px; text-shadow: 0 1px 2px #000; }}
</style><h1>SimpleQuant 图标候选</h1>
<p>每行：白底上的各尺寸（256 ~ 16 像素）· 深色任务栏（32 / 24 / 16）· 桌面快捷方式（48）</p>
{''.join(rows)}"""


def main(out_dir: Path, keys=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / "icon_preview.html"
    page.write_text(html(keys), encoding="utf-8")
    n = len([k for k in CANDIDATES if not keys or k in keys])
    edge = next((p for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                             Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) if p.exists()), None)
    if edge:
        png = out_dir / "icon_preview.png"
        subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--force-device-scale-factor=1",
                        "--hide-scrollbars", f"--window-size=1180,{120 + 360 * n}", f"--screenshot={png}", page.as_uri()],
                       check=False, capture_output=True, timeout=60)
        print(png)
    print(page)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools" / "icon_preview",
         sys.argv[2].split(",") if len(sys.argv) > 2 else None)
