"""
生成程序图标 gui/static/icon.ico（打包时 SimpleQuant.spec 会用上）和 icon.png（窗口 / 浏览器标签页图标）
    .venv\\Scripts\\python tools\\make_icon.py

图案在 tools/icon_preview.py 里（定稿为 H3D）：48 像素及以上用 H3（SQ + 收益曲线），
32 像素及以下用 D（只有收益曲线，小尺寸更清楚）。每个图案用 Edge 无头模式在 1024 像素下渲染（透明背景），
再用 Pillow 缩小到各尺寸，写进同一个 .ico
"""

import io
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_preview import FONT, ROOT, body_at, svg  # noqa: E402

CHOICE = "H3D"
SIZES = [256, 128, 64, 48, 32, 24, 16]
RENDER = 1024
OUT = ROOT / "gui" / "static"
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]


def render(body: str, tmp: Path) -> Image.Image:
    """用 Edge 把 SVG 渲染成 RENDER × RENDER 的透明 PNG"""
    import base64
    font = base64.b64encode(FONT.read_bytes()).decode()
    page = tmp / "icon.html"
    page.write_text(
        f'<!doctype html><meta charset="utf-8"><style>@font-face {{ font-family: SQSerif; '
        f'src: url(data:font/woff;base64,{font}) format("woff"); }} html, body {{ margin: 0; background: transparent; }}'
        f' svg {{ display: block; }}</style>{svg(body, RENDER)}', encoding="utf-8")
    png = tmp / "icon.png"
    png.unlink(missing_ok=True)
    edge = next(p for p in EDGE if p.exists())
    subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--force-device-scale-factor=1", "--hide-scrollbars",
                    "--default-background-color=00000000", f"--window-size={RENDER},{RENDER}",
                    f"--screenshot={png}", page.as_uri()], check=True, capture_output=True, timeout=60)
    return Image.open(png).convert("RGBA").copy()


def write_ico(images: dict[int, Image.Image], path: Path):
    """每个尺寸各存一张 PNG（Vista 起支持），这样不同尺寸可以用不同的图"""
    blobs = []
    for size, im in sorted(images.items(), reverse=True):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        blobs.append((size, buf.getvalue()))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for size, blob in blobs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    path.write_bytes(header + entries + data)


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        big, small = render(body_at(CHOICE, 256), tmp), render(body_at(CHOICE, 16), tmp)
    images = {s: (big if s >= 48 else small).resize((s, s), Image.LANCZOS) for s in SIZES}
    OUT.mkdir(parents=True, exist_ok=True)
    write_ico(images, OUT / "icon.ico")
    images[256].save(OUT / "icon.png")
    print(OUT / "icon.ico")


if __name__ == "__main__":
    main()
