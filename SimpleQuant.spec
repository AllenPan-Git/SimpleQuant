# -*- mode: python ; coding: utf-8 -*-
r"""
PyInstaller 打包配置（Windows / macOS / Linux 共用；PyInstaller 不能交叉编译，各系统在自己的机器上打包）
    Windows：双击 build.bat → dist\windows\SimpleQuant\（运行其中的 SimpleQuant.exe）
    macOS / Linux：sh tools/build_unix.sh（GitHub Actions 里也用它）→ dist/macos/SimpleQuant.app、dist/linux/SimpleQuant/
    输出目录由命令行 --distpath dist/<系统> --workpath build/<系统> 指定

- 单文件夹模式（onedir）：启动快，不用每次解压
- simplequant / gui / ui 只以 .py 源码形式放在 _internal 里（不进可执行文件内的 PYZ）：
  导出 Python 脚本时用 inspect.getsource 读取策略基类等源码；自动更新时只改了自己代码的版本只需下载这几个 .py，
  可执行文件只在依赖库变化时才变（PYZ 在里面，约 40 MB）。打包时要固定 PYTHONHASHSEED、SOURCE_DATE_EPOCH（build.bat、
  tools/build_unix.sh、tools/release.py 已设置），否则时间戳、base_library.zip 里的文件顺序每次不同
- 桌面窗口（pywebview）：Windows 用系统自带的 WebView2，macOS 用系统自带的 WebKit，
  Linux 打包 Qt WebEngine（PyQt6；打包系统的 GTK / WebKitGTK 在别的发行版上容易出问题）
- 不打包：tests、tools 等（只收集程序实际 import 的模块）；排除 streamlit、pytest、LiteLLM（体积太大，界面里会提示改用 OpenAI 兼容接口）、torch 等无关的大库
"""

import json
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from simplequant import __version__  # noqa: E402

WINDOWS, MACOS, LINUX = sys.platform == "win32", sys.platform == "darwin", sys.platform.startswith("linux")

# 记录源码目录：打包版第一次运行时据此找到旧数据（simplequant/migrate.py）
(ROOT / "simplequant" / "_build_info.json").write_text(
    json.dumps({"source_dir": str(ROOT)}, ensure_ascii=False), encoding="utf-8")

datas = [
    (str(ROOT / "gui" / "static"), "gui/static"),
    (str(ROOT / "simplequant" / "_build_info.json"), "simplequant"),
    # 自动更新替换文件的脚本（Windows 用 .ps1，macOS / Linux 用 .sh）
    (str(ROOT / "simplequant" / "update" / ("apply_update.ps1" if WINDOWS else "apply_update.sh")), "simplequant/update"),
]
for pkg in ("nicegui", "akshare", "mootdx", "plotly", "baostock"):
    datas += collect_data_files(pkg)

# 新浪行情解码用的 V8 引擎（akshare → py_mini_racer）；打包后它在 _MEIPASS 根目录找
# mini_racer.dll / libmini_racer.glibc.so / libmini_racer.dylib / armlibmini_racer.dylib（Apple 芯片，来自 akracer）
# 只带本平台用的那一个（与 py_mini_racer._get_lib_path 的选择规则一致）
import platform
import py_mini_racer
_arm = platform.machine().lower() in ("arm64", "aarch64")
MINI_RACER = ("mini_racer.dll" if WINDOWS else
              ("armlib" if _arm else "lib") + "mini_racer" + (".dylib" if MACOS else ".glibc.so"))
_mr = Path(py_mini_racer.__file__).parent
print("py_mini_racer:", _mr, sorted(f.name for f in _mr.iterdir()))
if (_mr / MINI_RACER).exists():
    binaries = [(str(_mr / MINI_RACER), ".")]
else:
    # 其他版本的包（如新的 mini-racer）文件名不同、按自己目录查找：整个目录的非 .py 文件原样带上
    binaries = [(str(f), "py_mini_racer") for f in _mr.iterdir()
                if f.is_file() and f.suffix not in (".py", ".pyc")]

hiddenimports = (
    collect_submodules("simplequant") + collect_submodules("gui") + ["ui.shared", "ui.charts", "ui.texts"]
    + collect_submodules("nicegui") + ["webview", "anthropic", "openai", "pyarrow", "matplotlib.backends.backend_agg"]
)
if LINUX:
    hiddenimports += ["webview.platforms.qt", "qtpy", "qtpy.QtWebEngineWidgets", "qtpy.QtWebEngineCore",
                      "qtpy.QtWebChannel", "qtpy.QtNetwork", "PyQt6.QtWebEngineWidgets"]
elif MACOS:
    hiddenimports += ["webview.platforms.cocoa"]

excludes = ["streamlit", "pytest", "_pytest", "litellm", "torch", "torchvision", "tensorflow", "IPython", "jupyter",
            "notebook", "jupyter_client", "ipykernel", "sklearn", "PyQt5", "PySide2", "PySide6",
            "boto3", "botocore"]   # anthropic 的 AWS Bedrock 支持用不到
if not LINUX:
    excludes += ["PyQt6", "qtpy"]
if LINUX:
    excludes += ["gi", "webview.platforms.gtk",     # 只用打包进去的 Qt，不碰系统的 GTK
                 "pyarrow.flight", "pyarrow._flight", "pyarrow.substrait", "pyarrow._substrait", "pyarrow.gandiva"]

ICON = ROOT / "gui" / "static" / "icon.ico"         # macOS 上 PyInstaller 用 Pillow 自动转成 .icns

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    module_collection_mode={"simplequant": "py", "gui": "py", "ui": "py"},
)
if LINUX:
    # Qt 的钩子会带上全部 QML 模块（Quick3D、Pdf、Multimedia……）和全部语言的翻译，整个包接近 1 GB。
    # 去掉用不到的目录和插件，再只保留实际被链接到的 Qt / pyarrow 动态库（ldd 列出的已含间接依赖）
    import subprocess

    QT = "PyQt6/Qt6/"
    KEEP_LOCALES = ("qtwebengine_locales/en-US.pak", "qtwebengine_locales/zh-CN.pak")
    DROP_PLUGINS = ("platforms/libqvnc", "platforms/libqlinuxfb", "platforms/libqeglfs", "platforms/libqvkkhrdisplay",
                    "platforms/libqminimalegl", "egldeviceintegrations/", "position/", "printsupport/",
                    "platformthemes/libqgtk3")

    def _drop(dest: str) -> bool:
        d = dest.replace("\\", "/")
        if d.startswith(QT + "qml/"):
            return True
        if d.startswith(QT + "translations/"):
            return not d.endswith(KEEP_LOCALES)
        return d.startswith(QT + "plugins/") and any(x in d for x in DROP_PLUGINS)

    a.binaries = [e for e in a.binaries if not _drop(e[0])]
    a.datas = [e for e in a.datas if not _drop(e[0])]

    def _prunable(dest: str) -> bool:
        d = dest.replace("\\", "/")
        return d.startswith(QT + "lib/") or (d.startswith("pyarrow/") and "/" not in d[8:] and ".so" in d
                                               and not d.endswith(".abi3.so") and "cpython" not in d)

    needed = set()
    for dest, src, _ in a.binaries:
        if not _prunable(dest):
            out = subprocess.run(["ldd", src], capture_output=True, text=True).stdout
            needed.update(line.split()[0] for line in out.splitlines() if "=>" in line)
    a.binaries = [e for e in a.binaries if not _prunable(e[0]) or Path(e[0]).name in needed]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SimpleQuant",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON) if ICON.exists() and not LINUX else None,
)
# 不 strip：Ubuntu 22.04 的 strip 会弄坏 numpy 自带的 OpenBLAS（ELF load command not page-aligned），macOS 上还会破坏签名
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SimpleQuant")

if MACOS:
    app = BUNDLE(
        coll,
        name="SimpleQuant.app",
        icon=str(ICON) if ICON.exists() else None,
        bundle_identifier="io.github.allenpan-git.simplequant",
        version=__version__,
        info_plist={
            "CFBundleName": "SimpleQuant",
            "CFBundleDisplayName": "SimpleQuant",
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.finance",
            "NSHumanReadableCopyright": "GPLv3",
        },
    )
